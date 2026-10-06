import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import broker_trade
import demo_admin


RULES = demo_admin.DEFAULTS["rules"]


def fake_api(*, cash="10000", balance="0", buy_max="100", sell_max="0", kind="stock", stale=False):
    calls = []
    uid = "uid-rosn"
    type_name = broker_trade.KINDS[kind]
    lot = 10 if kind == "stock" else 1
    stamp = datetime.now(timezone.utc) - (timedelta(minutes=3) if stale else timedelta(seconds=2))

    def call(method, token, payload):
        calls.append((method, payload))
        if method == "instrument":
            return {"instrument": {"ticker": "ROSN", "classCode": "TQBR", "uid": uid, "lot": lot,
                                   "instrumentType": type_name, "currency": "rub", "apiTradeAvailableFlag": True,
                                   "buyAvailableFlag": True, "sellAvailableFlag": True, "otcFlag": False,
                                   "minPriceIncrement": {"units": "1", "nano": 0}}}
        if method == "book":
            return {"instrumentUid": uid, "priceCurrency": "rub", "orderbookTs": stamp.isoformat(),
                    "asks": [{"price": {"units": "100", "nano": 0}}],
                    "bids": [{"price": {"units": "99", "nano": 0}}]}
        if method == "positions":
            return {"accountId": "account", "money": [{"currency": "rub", "units": cash, "nano": 0}],
                    "securities": [{"instrumentUid": uid, "balance": balance}], "futures": []}
        if method == "max_lots":
            return {"buyLimits": {"buyMaxLots": buy_max}, "sellLimits": {"sellMaxLots": sell_max}}
        if method == "order_price":
            return {"totalOrderAmount": {"currency": "rub", "units": "1000", "nano": 0},
                    "initialOrderAmount": {"currency": "rub", "units": "990", "nano": 0},
                    "extraBond": {"aciValue": {"currency": "rub", "units": "10", "nano": 0}}}
        if method == "margin":
            return {"initialMarginOnBuy": {"currency": "rub", "units": "9000", "nano": 0}}
        raise AssertionError(method)

    return call, calls


class BrokerTradeTests(unittest.TestCase):
    def test_full_access_token_requires_open_broker_account(self):
        def call(method, token, payload):
            return {"accounts": [{"id": "a", "type": "ACCOUNT_TYPE_TINKOFF",
                                  "status": "ACCOUNT_STATUS_OPEN", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"},
                                 {"id": "b", "type": "ACCOUNT_TYPE_TINKOFF",
                                  "status": "ACCOUNT_STATUS_OPEN", "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"}]}
        self.assertEqual([item["id"] for item in broker_trade.full_access_accounts("token", call)], ["a"])

    def test_buy_uses_actual_lot_and_own_money(self):
        call, calls = fake_api()
        plan = broker_trade.prepare_order({"ticker": "ROSN", "classCode": "TQBR",
                                           "asset_type": "stock", "side": "buy"}, RULES, "token", "account", 0, call)
        self.assertEqual(plan["lot_size"], 10)
        self.assertEqual(plan["quantity"], 5)
        self.assertEqual(plan["price"], "100")
        self.assertEqual(plan["estimated_rub"], "5000")
        self.assertFalse(any(method == "post_order" for method, _ in calls))
        self.assertNotIn("price", next(payload for method, payload in calls if method == "max_lots"))

    def test_no_money_or_broker_limit_blocks_buy(self):
        for cash, maximum in [("0", "100"), ("10000", "0")]:
            with self.subTest(cash=cash, maximum=maximum):
                call, _ = fake_api(cash=cash, buy_max=maximum)
                with self.assertRaisesRegex(broker_trade.TradeError, "Недостаточно"):
                    broker_trade.prepare_order({"ticker": "ROSN", "classCode": "TQBR",
                                                "asset_type": "stock", "side": "buy"}, RULES, "token", "account", 0, call)

    def test_sell_requires_bot_bought_and_broker_available_lots(self):
        call, _ = fake_api(balance="20", sell_max="2")
        signal = {"ticker": "ROSN", "classCode": "TQBR", "asset_type": "stock", "side": "sell"}
        with self.assertRaisesRegex(broker_trade.TradeError, "купленной этим ботом"):
            broker_trade.prepare_order(signal, RULES, "token", "account", 0, call)
        plan = broker_trade.prepare_order(signal, RULES, "token", "account", 5, call)
        self.assertEqual(plan["quantity"], 2)

    def test_stale_book_blocks_real_order(self):
        call, _ = fake_api(stale=True)
        with self.assertRaisesRegex(broker_trade.TradeError, "устарела"):
            broker_trade.prepare_order({"ticker": "ROSN", "classCode": "TQBR",
                                        "asset_type": "stock", "side": "buy"}, RULES, "token", "account", 0, call)

    def test_future_margin_limit_and_no_margin_trade(self):
        call, _ = fake_api(kind="future")
        plan = broker_trade.prepare_order({"ticker": "ROSN", "classCode": "TQBR",
                                           "asset_type": "future", "side": "buy"}, RULES, "token", "account", 0, call)
        self.assertEqual(plan["quantity"], 1)
        self.assertEqual(plan["price_type"], "PRICE_TYPE_POINT")
        posted = []

        def submit(method, token, payload):
            posted.append((method, payload))
            return {"orderId": "exchange-id"}

        broker_trade.submit_order(plan, "token", "request-id", submit)
        self.assertEqual(posted[0][0], "post_order")
        self.assertEqual(posted[0][1]["orderType"], "ORDER_TYPE_LIMIT")
        self.assertFalse(posted[0][1]["confirmMarginTrade"])

    def test_uncertain_submission_is_persisted_and_never_retried(self):
        call, _ = fake_api()
        plan = broker_trade.prepare_order({"ticker": "ROSN", "classCode": "TQBR",
                                           "asset_type": "stock", "side": "buy"}, RULES, "token", "account", 0, call)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(demo_admin, "REAL_ORDERS_PATH", Path(folder) / "orders.json"), \
                patch.object(demo_admin, "REAL_ORDERS", {}), \
                patch.object(demo_admin, "submit_order", side_effect=broker_trade.TradeError("Нет ответа")) as submit:
            with self.assertRaisesRegex(broker_trade.TradeError, "повторная отправка заблокирована"):
                demo_admin.place_real(plan, "source-1", "token")
            self.assertEqual(next(iter(demo_admin.REAL_ORDERS.values()))["status"], "uncertain")
            self.assertTrue(json.loads((Path(folder) / "orders.json").read_text()))
            with self.assertRaisesRegex(broker_trade.TradeError, "уже отправлялась"):
                demo_admin.place_real(plan, "source-1", "token")
            self.assertEqual(submit.call_count, 1)

    def test_partial_fill_then_fill_updates_copied_position_once(self):
        row = {"account_id": "account", "ticker": "ROSN", "class_code": "TQBR", "instrument_uid": "uid-rosn",
               "side": "buy", "quantity": 2, "filled_lots": 0, "status": "submitted", "request_id": "req",
               "source_key": "source", "price": "100"}
        states = [
            {"executionReportStatus": "EXECUTION_REPORT_STATUS_PARTIALLYFILL", "lotsExecuted": "1"},
            {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": "2"},
        ]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(demo_admin, "REAL_ORDERS_PATH", Path(folder) / "orders.json"), \
                patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), \
                patch.object(demo_admin, "REAL_ORDERS", {"req": row}), patch.object(demo_admin, "EVENTS", []), \
                patch.object(demo_admin, "trade_token", return_value="token"), \
                patch.object(demo_admin, "order_state", side_effect=states), \
                patch.object(demo_admin, "load_settings", return_value={"paused": False, "chat_id": ""}), \
                patch.object(demo_admin, "notify", side_effect=lambda event, settings, message, **kwargs: event.update(notification="test")):
            demo_admin.update_real_order_state("req")
            self.assertEqual(demo_admin.copied_lots("account", "ROSN", "TQBR", "sell"), 1)
            demo_admin.update_real_order_state("req")
            self.assertEqual(demo_admin.copied_lots("account", "ROSN", "TQBR", "sell"), 2)
            self.assertEqual(len(demo_admin.EVENTS), 2)

    def test_changed_preview_does_not_post_order(self):
        plan = {"account_id": "account", "instrument_uid": "uid-rosn", "side": "buy",
                "quantity": 1, "price": "100"}
        preview = {"created": time.monotonic(), "plan": plan,
                   "signal": {"ticker": "ROSN", "classCode": "TQBR", "side": "buy", "asset_type": "stock"},
                   "source_key": "source"}
        changed = {**plan, "price": "101"}
        with patch.object(demo_admin, "PREVIEWS", {"preview": preview}), \
                patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                patch.object(demo_admin, "load_settings", return_value={"real_mode": "confirm", "rules": RULES}), \
                patch.object(demo_admin, "prepare_real", return_value=(changed, "token")), \
                patch.object(demo_admin, "alert_trade_failure") as alert, \
                patch.object(demo_admin, "place_real") as submit:
            with self.assertRaisesRegex(broker_trade.TradeError, "изменились"):
                demo_admin.confirm_real_preview("preview")
            submit.assert_not_called()
            alert.assert_called_once()

    def test_no_cash_alerts_and_does_not_send_order(self):
        signal = {"ticker": "ROSN", "classCode": "TQBR", "asset_type": "stock", "side": "buy"}
        call, _ = fake_api(cash="0")
        def prepare(*args, **kwargs):
            return broker_trade.prepare_order(signal, RULES, "token", "account", 0, call)
        with patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                patch.object(demo_admin, "load_settings", return_value={"real_mode": "confirm", "rules": RULES}), \
                patch.object(demo_admin, "real_signal", return_value=(signal, "source", None)), \
                patch.object(demo_admin, "prepare_real", side_effect=prepare), \
                patch.object(demo_admin, "alert_trade_failure") as alert, \
                patch.object(demo_admin, "place_real") as submit:
            with self.assertRaisesRegex(broker_trade.TradeError, "Недостаточно"):
                demo_admin.create_real_preview("source")
            submit.assert_not_called()
            alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
