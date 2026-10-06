import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import demo_admin
from broker_trade import prepare_order, TradeError
from test_broker_trade import fake_api
from trade_approvals import Approvals
from trade_policy import DEFAULT_POLICY, approval_reasons, validate_policy
from telegram_router import run_router
from short_simulation import simulate_with_short


class TradeWorkflowTests(unittest.TestCase):
    def settings(self):
        return {**copy.deepcopy(demo_admin.DEFAULTS), "chat_id": "12345", "monitoring_enabled": True, "real_mode": "confirm"}

    def signal(self):
        return {"ticker": "ROSN", "classCode": "TQBR", "asset_type": "stock", "side": "buy"}

    def plan(self):
        return {"account_id": "account", "instrument_uid": "uid", "side": "buy", "quantity": 1,
                "price": "100", "estimated_rub": "100", "price_type": "PRICE_TYPE_CURRENCY"}

    def test_existing_position_unknown_position_and_profit_threshold(self):
        policy = {**DEFAULT_POLICY, "enabled": True, "yield_unit": "percent"}
        signal = self.signal()
        self.assertEqual(approval_reasons(signal, DEFAULT_POLICY), [])
        self.assertTrue(approval_reasons(signal, policy))
        signal["investor_position"] = {"status": "verified", "present": False}
        self.assertEqual(approval_reasons(signal, policy), [])
        signal.update(side="sell", relative_yield="0.14")
        self.assertIn("ниже", approval_reasons(signal, policy)[0])
        signal["relative_yield"] = "0.15"
        self.assertEqual(approval_reasons(signal, policy), [])
        signal.update(side="buy", investor_position={"status": "verified", "present": True}, relative_yield="-1")
        self.assertEqual(len(approval_reasons(signal, policy)), 2)

    def test_unconfirmed_yield_units_do_not_allow_automatic_sale(self):
        policy = {**DEFAULT_POLICY, "enabled": True}
        self.assertTrue(approval_reasons({**self.signal(), "side": "sell", "relative_yield": "10"}, policy))
        policy["yield_unit"] = "fraction"
        self.assertEqual(approval_reasons({**self.signal(), "side": "sell", "relative_yield": "0.0015"}, policy), [])
        for value in ("NaN", "Infinity", None):
            self.assertTrue(approval_reasons({**self.signal(), "side": "sell", "relative_yield": value}, policy))

    def test_invalid_rules_rejected(self):
        for change in [{"position_percent": 0}, {"position_percent": True}, {"min_profit_percent": "NaN"},
                       {"yield_unit": "guess"}, {"enabled": 1}]:
            with self.assertRaises(ValueError):
                validate_policy({**DEFAULT_POLICY, **change})

    def test_paper_short_cycle_does_not_change_legacy_mode(self):
        signal = {**self.signal(), "side": "sell", "price": 100, "currency": "rub", "lot_size": 1}
        legacy, positions = simulate_with_short(signal, demo_admin.DEFAULTS["rules"], {})
        self.assertEqual(legacy["status"], "blocked")
        self.assertEqual(positions, {})
        opening, positions = simulate_with_short(signal, demo_admin.DEFAULTS["rules"], {}, True)
        self.assertEqual(opening["status"], "executed")
        self.assertLess(positions["ROSN:TQBR"]["quantity"], 0)
        closing, positions = simulate_with_short({**signal, "side": "buy", "price": 95}, demo_admin.DEFAULTS["rules"], positions, False)
        self.assertEqual(closing["status"], "executed")
        self.assertNotIn("ROSN:TQBR", positions)

    def test_paper_short_respects_sale_budget(self):
        import copy
        rules = copy.deepcopy(demo_admin.DEFAULTS["rules"])
        rules["sales"]["stock"]["budget"] = 250
        signal = {**self.signal(), "side": "sell", "price": 100, "currency": "rub", "lot_size": 1}
        result, positions = simulate_with_short(signal, rules, {}, True)
        self.assertEqual(result["quantity"], 2)
        self.assertEqual(positions["ROSN:TQBR"]["quantity"], -2)

    def test_unverified_empty_source_position_still_requires_confirmation(self):
        policy = {**DEFAULT_POLICY, "enabled": True, "confirm_existing": False}
        signal = {**self.signal(), "investor_position": {"status": "unavailable", "present": False}}
        self.assertTrue(approval_reasons(signal, policy))

    def test_percent_cap_accounts_for_existing_and_reserved_positions(self):
        original, _ = fake_api(balance="10")
        def api(method, token, payload):
            if method == "portfolio":
                return {"accountId": "account", "totalAmountPortfolio": {"currency": "rub", "units": "30000"}}
            return original(method, token, payload)
        policy = {**DEFAULT_POLICY, "position_cap_enabled": True}
        plan = prepare_order(self.signal(), demo_admin.DEFAULTS["rules"], "token", "account", 2, api, policy=policy)
        self.assertEqual(plan["quantity"], 1)  # 3,000 cap less two reserved/held lots at 1,000 each.
        with self.assertRaises(TradeError):
            prepare_order(self.signal(), demo_admin.DEFAULTS["rules"], "token", "account", 3, api, policy=policy)

    def test_unknown_portfolio_currency_blocks_percentage_buy(self):
        original, _ = fake_api()
        def api(method, token, payload):
            return {"totalAmountPortfolio": {"currency": "usd", "units": "30000"}} if method == "portfolio" else original(method, token, payload)
        with self.assertRaises(TradeError):
            prepare_order(self.signal(), demo_admin.DEFAULTS["rules"], "token", "account", 0, api,
                          policy={**DEFAULT_POLICY, "position_cap_enabled": True})

    def test_short_account_is_not_mistaken_for_ordinary_buy(self):
        api, _ = fake_api(balance="-10")
        with self.assertRaisesRegex(TradeError, "короткая"):
            prepare_order(self.signal(), demo_admin.DEFAULTS["rules"], "token", "account", 0, api)

    def test_approval_survives_restart_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            approvals = Approvals(root)
            row = approvals.create(self.signal(), self.settings(), self.plan(), "event", "token", "real", ["reason"])
            restored = Approvals(root)
            self.assertEqual(restored.claim(row["id"], "approve", self.settings(), "token")["status"], "processing")
            with self.assertRaises(ValueError):
                restored.claim(row["id"], "approve", self.settings(), "token")
            self.assertNotIn('"token"', approvals.path.read_text())

    def test_changed_token_recipient_rules_and_expiry_invalidate_decision(self):
        for variant in ("token", "recipient", "rules", "expired"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                approvals = Approvals(Path(folder))
                settings = self.settings()
                row = approvals.create(self.signal(), settings, self.plan(), "event", "token", "real", [])
                token = "token"
                if variant == "token": token = "other"
                if variant == "recipient": settings["chat_id"] = "77777"
                if variant == "rules": settings["rules"]["stock"]["budget"] += 1
                if variant == "expired": approvals.rows[row["id"]]["expires"] = time.time() - 1
                with self.assertRaises(ValueError): approvals.claim(row["id"], "approve", settings, token)

    def test_consent_is_scoped_to_author_account_and_client_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = Approvals(root / "one"), Approvals(root / "two")
            first.set_trusted(self.signal(), self.settings(), "account", True)
            self.assertTrue(Approvals(root / "one").trusted(self.signal(), self.settings(), "account"))
            self.assertFalse(first.trusted(self.signal(), self.settings(), "another-account"))
            self.assertFalse(second.trusted(self.signal(), self.settings(), "account"))
            first.set_trusted(self.signal(), self.settings(), "account", False)
            self.assertFalse(first.trusted(self.signal(), self.settings(), "account"))

    def run_decision(self, action="approve", changed=False, changed_token=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            approvals, settings = Approvals(root), self.settings()
            row = approvals.create(self.signal(), settings, self.plan(), "event", "token", "real", [])
            fresh = self.plan()
            if changed: fresh["price"] = "101"
            with patch.object(demo_admin, "APPROVALS", approvals), patch.object(demo_admin, "load_settings", return_value=settings), \
                 patch.object(demo_admin, "trade_token", side_effect=["token", "new-token"] if changed_token else None, return_value="token"), patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                 patch.object(demo_admin, "prepare_real", return_value=(fresh, "new-token" if changed_token else "token")), \
                 patch.object(demo_admin, "place_real", return_value={"status": "submitted"}) as submit, \
                 patch.object(demo_admin, "notify"), patch.object(demo_admin, "EVENTS", []), \
                 patch.object(demo_admin, "EVENTS_PATH", root / "events.json"):
                if changed or changed_token:
                    with self.assertRaisesRegex(ValueError, "изменились"): demo_admin.decide_approval(row["id"], action)
                    submit.assert_not_called()
                else:
                    demo_admin.decide_approval(row["id"], action)
                    with self.assertRaises(ValueError): demo_admin.decide_approval(row["id"], action)
                    self.assertEqual(submit.call_count, 0 if action == "reject" else 1)
                    self.assertEqual(approvals.trusted(self.signal(), settings, "account"), action == "trust")

    def test_confirm_reject_trust_and_changed_price(self):
        for action in ("approve", "reject", "trust"):
            with self.subTest(action=action): self.run_decision(action)
        self.run_decision(changed=True)
        self.run_decision(changed_token=True)

    def test_other_telegram_user_and_group_cannot_execute(self):
        query = {"from": {"id": 77777}, "message": {"chat": {"id": 12345, "type": "private"}}, "data": "trade:approve:fake"}
        with patch.object(demo_admin, "load_settings", return_value=self.settings()), patch.object(demo_admin, "decide_approval") as decide:
            self.assertFalse(demo_admin.telegram_callback(query)["matched"])
            query["from"]["id"] = 12345
            query["message"]["chat"]["type"] = "group"
            self.assertFalse(demo_admin.telegram_callback(query)["matched"])
            decide.assert_not_called()

    def test_router_has_one_consumer_for_shared_bot_and_private_recipient_routing(self):
        stop, calls, routed = threading.Event(), [], []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "offsets.json"
            def call(token, method, data):
                calls.append((token, method, data))
                if method == "getUpdates":
                    return [{"update_id": 9, "callback_query": {"id": "query", "from": {"id": 22222}}}]
                return True
            def dispatch(target, query):
                self.assertEqual(json.loads(path.read_text()).popitem()[1], 10)
                routed.append(target);stop.set()
                return {"matched": True, "message": "accepted"}
            with patch('telegram_router.bot_call', side_effect=call):
                run_router(lambda: [("first", {"token": "shared-secret", "chat_id": "11111"}),
                                    ("second", {"token": "shared-secret", "chat_id": "22222"})], dispatch, path, stop)
            self.assertEqual(routed, ["second"])
            self.assertEqual(len([call for call in calls if call[1] == "getUpdates"]), 1)
            self.assertNotIn("shared-secret", path.read_text())
