import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import broker_trade
import demo_admin
from demo_engine import DEFAULT_RULES, simulate, validated_rules
from test_broker_trade import fake_api


class SaleLimitsTests(unittest.TestCase):
    def rules(self, asset="stock", **changes):
        result = copy.deepcopy(DEFAULT_RULES)
        result["sales"][asset].update(changes)
        return result

    def real_sell(self, rules, *, kind="stock", balance="100", copied=10):
        call, calls = fake_api(balance=balance, sell_max="100", kind=kind)
        if kind == "future":
            original = call
            def call(method, token, payload):
                result = original(method, token, payload)
                if method == "positions":
                    result["futures"] = [{"instrumentUid": "uid-rosn", "balance": balance}]
                    result["securities"] = []
                return result
        plan = broker_trade.prepare_order({"ticker": "ROSN", "classCode": "TQBR",
                                           "asset_type": kind, "side": "sell"},
                                          rules, "token", "account", copied, call)
        self.assertFalse(any(method == "post_order" for method, _ in calls))
        return plan

    def test_amount_limit_even_for_full_close(self):
        plan = self.real_sell(self.rules(budget=2000))
        self.assertEqual((plan["quantity"], plan["estimated_rub"]), (2, "1980"))
        self.assertEqual(self.real_sell(self.rules(mode="amount", percent=25, budget=4000))["quantity"], 4)

    def test_each_close_percentage_and_no_rounding_up(self):
        for percent, lots in [(25, 2), (50, 5), (75, 7), (100, 10)]:
            self.assertEqual(self.real_sell(self.rules(percent=percent))["quantity"], lots)
        with self.assertRaisesRegex(broker_trade.TradeError, "меньше одного"):
            self.real_sell(self.rules(percent=25), copied=1)

    def test_broker_availability_and_bot_ownership_remain_caps(self):
        self.assertEqual(self.real_sell(self.rules(), balance="10", copied=20)["quantity"], 1)
        self.assertEqual(self.real_sell(self.rules(), copied=2)["quantity"], 2)

    def test_expensive_lot_and_insufficient_budget_block_sale(self):
        for changes in [{"single_lot_cap": 900}, {"budget": 900}]:
            with self.subTest(changes=changes), self.assertRaises(broker_trade.TradeError):
                self.real_sell(self.rules(**changes))

    def test_bonds_include_nkd_and_funds_use_their_own_caps(self):
        plan = self.real_sell(self.rules("bond", budget=1999), kind="bond")
        self.assertEqual((plan["quantity"], plan["estimated_rub"]), (1, "1000"))
        plan = self.real_sell(self.rules("fund", budget=3000), kind="fund")
        self.assertEqual(plan["quantity"], 3)

    def test_futures_limit_released_collateral_and_contracts(self):
        rules = self.rules("future", margin_budget=30000, max_contracts=2)
        plan = self.real_sell(rules, kind="future")
        self.assertEqual((plan["quantity"], plan["estimated_rub"]), (2, "18000"))
        rules["sales"]["future"]["margin_budget"] = 8000
        with self.assertRaisesRegex(broker_trade.TradeError, "выше лимита"):
            self.real_sell(rules, kind="future")

    def test_paper_sale_uses_same_caps_and_never_mutates_on_block(self):
        signal = demo_admin.SCENARIOS["stock_sell"]
        positions = {"DEMO-R:TQBR": {"quantity": 10}}
        result, remaining = simulate(signal, self.rules(budget=1100), positions)
        self.assertEqual((result["quantity"], result["amount_rub"], remaining["DEMO-R:TQBR"]["quantity"]),
                         (2, "1060.00", 8))
        blocked, unchanged = simulate(signal, self.rules(budget=500), positions)
        self.assertEqual((blocked["status"], unchanged), ("blocked", positions))
        self.assertEqual(positions["DEMO-R:TQBR"]["quantity"], 10)

    def test_old_settings_keep_percent_and_acquire_separate_sale_caps(self):
        legacy = copy.deepcopy(DEFAULT_RULES)
        del legacy["sales"]
        legacy["sell_percent"] = 50
        migrated = validated_rules(legacy)
        self.assertEqual({s["percent"] for s in migrated["sales"].values()}, {50})
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "SETTINGS_PATH", Path(folder) / "settings.json"):
            demo_admin.SETTINGS_PATH.write_text(json.dumps({**demo_admin.DEFAULTS, "rules": legacy}))
            loaded = demo_admin.load_settings()
            saved = demo_admin.save_settings(loaded)
            self.assertEqual(saved["rules"]["sales"], migrated["sales"])

    def test_invalid_sales_are_rejected(self):
        for field, value in [("mode", "anything"), ("budget", True), ("budget", 0),
                             ("percent", 101), ("percent", 25.5), ("single_lot_cap", -1)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validated_rules(self.rules(**{field: value}))


if __name__ == "__main__":
    unittest.main()
