import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import demo_admin
from demo_engine import DEFAULT_RULES, simulate, validated_rules


class DemoEngineTests(unittest.TestCase):
    def test_stock_buy_sell_and_absent_position(self):
        buy = demo_admin.SCENARIOS["stock_buy"]
        sell = demo_admin.SCENARIOS["stock_sell"]
        missing, unchanged = simulate(sell, DEFAULT_RULES, {})
        self.assertEqual(missing["status"], "blocked")
        self.assertEqual(unchanged, {})
        bought, positions = simulate(buy, DEFAULT_RULES, {})
        self.assertEqual((bought["quantity"], bought["amount_rub"]), (10, "5000.00"))
        sold, positions = simulate(sell, DEFAULT_RULES, positions)
        self.assertEqual((sold["status"], sold["quantity"], positions), ("executed", 10, {}))

    def test_one_expensive_lot_is_allowed_but_position_cap_blocks_more(self):
        signal = demo_admin.SCENARIOS["expensive_stock"]
        first, positions = simulate(signal, DEFAULT_RULES, {})
        self.assertEqual((first["status"], first["quantity"], first["amount_rub"]),
                         ("executed", 1, "8000.00"))
        rules = json.loads(json.dumps(DEFAULT_RULES))
        rules["stock"]["position_cap"] = 15000
        blocked, unchanged = simulate(signal, rules, positions)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(unchanged, positions)

    def test_futures_need_known_margin_and_respect_total_budget(self):
        high, empty = simulate(demo_admin.SCENARIOS["future_over_limit"], DEFAULT_RULES, {})
        self.assertEqual(high["status"], "blocked")
        self.assertEqual(empty, {})
        first, positions = simulate(demo_admin.SCENARIOS["future_buy"], DEFAULT_RULES, {})
        self.assertEqual((first["status"], first["quantity"]), ("executed", 1))
        second, unchanged = simulate(demo_admin.SCENARIOS["future_buy"], DEFAULT_RULES, positions)
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(unchanged, positions)
        unknown, _ = simulate({**demo_admin.SCENARIOS["future_buy"], "margin_rub": None}, DEFAULT_RULES, {})
        self.assertEqual(unknown["status"], "blocked")

    def test_bond_and_fund_use_separate_budgets(self):
        bond, positions = simulate(demo_admin.SCENARIOS["bond_buy"], DEFAULT_RULES, {})
        fund, positions = simulate(demo_admin.SCENARIOS["fund_buy"], DEFAULT_RULES, positions)
        self.assertEqual((bond["quantity"], bond["amount_rub"]), (10, "10000.00"))
        self.assertEqual((fund["quantity"], fund["amount_rub"]), (20, "5000.00"))
        self.assertEqual(len(positions), 2)

    def test_rules_reject_invalid_types(self):
        rules = json.loads(json.dumps(DEFAULT_RULES))
        rules["future"]["max_contracts"] = True
        with self.assertRaises(ValueError):
            validated_rules(rules)

    def test_scenarios_work_without_pulse_login_and_persist_portfolio(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(demo_admin, "AUTH", {"status": "required", "message": "Нужен вход"}), \
                patch.object(demo_admin, "EVENTS", []), patch.object(demo_admin, "POSITIONS", {}), \
                patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), \
                patch.object(demo_admin, "POSITIONS_PATH", Path(folder) / "positions.json"), \
                patch.object(demo_admin, "load_settings", return_value={**demo_admin.DEFAULTS, "chat_id": ""}), \
                patch.object(demo_admin, "notify", side_effect=lambda event, settings, message: event.update(notification="не отправлено: ID не указан")):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/demo/scenario"
                def post(name):
                    request = Request(url, data=json.dumps({"scenario": name}).encode(),
                                      headers={"Content-Type": "application/json"})
                    with urlopen(request, timeout=2) as response:
                        return json.load(response)["event"]
                self.assertEqual(post("stock_sell")["trade"], "ДЕМО: пропущено")
                self.assertEqual(post("stock_buy")["quantity"], 10)
                self.assertEqual(json.loads((Path(folder) / "positions.json").read_text())["DEMO-R:TQBR"]["quantity"], 10)
                self.assertEqual(post("stock_sell")["trade"], "ДЕМО: продано")
                self.assertEqual(demo_admin.POSITIONS, {})
                self.assertEqual(len(demo_admin.EVENTS), 3)
                reset = Request(f"http://127.0.0.1:{server.server_port}/api/demo/reset",
                                data=b"{}", headers={"Content-Type": "application/json"})
                with urlopen(reset, timeout=2) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(demo_admin.EVENTS, [])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
