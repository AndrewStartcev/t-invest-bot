import io
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import demo_admin
from broker_read import BrokerError, account_snapshot, call, read_only_accounts


class FakeResponse(io.BytesIO):
    pass


class BrokerReadTests(unittest.TestCase):
    def test_only_fixed_read_methods_and_bearer_header(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization"), timeout))
            return FakeResponse(b'{"accounts":[]}')

        call("accounts", "private-token", {}, opener)
        self.assertTrue(seen[0][0].startswith("https://invest-public-api.tbank.ru/rest/"))
        self.assertEqual(seen[0][1], "Bearer private-token")
        with self.assertRaises(BrokerError):
            call("orders", "private-token", {}, opener)

    def test_full_access_account_rejected(self):
        def opener(request, timeout):
            return FakeResponse(json.dumps({"accounts": [{"id": "123", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"}]}).encode())

        with self.assertRaisesRegex(BrokerError, "только для чтения"):
            read_only_accounts("private-token", opener)

    def test_positions_and_money_are_normalized(self):
        def opener(request, timeout):
            if request.full_url.endswith("GetPositions"):
                data = {"money": [{"currency": "rub", "units": "105", "nano": 500000000},
                                  {"currency": "usd", "units": "3", "nano": 0}],
                        "securities": [{"ticker": "ROSN", "classCode": "TQBR", "balance": "2", "blocked": "1"}]}
            else:
                data = {"totalAmountPortfolio": {"currency": "rub", "units": "1200", "nano": 0}}
            return FakeResponse(json.dumps(data).encode())

        result = account_snapshot("private-token", "123", opener)
        self.assertEqual(result["cash_rub"], "105.5")
        self.assertEqual(result["total_rub"], "1200")
        self.assertEqual(result["positions"][0]["available"], 2)
        self.assertEqual(result["positions"][0]["blocked"], 1)

    def test_connection_persists_secret_locally_but_state_excludes_it(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(demo_admin, "BROKER_TOKEN_PATH", Path(directory) / "token.txt"), \
                patch.object(demo_admin, "BROKER_ACCOUNT_PATH", Path(directory) / "account.txt"), \
                patch.object(demo_admin, "BROKER", {"status": "disconnected", "accounts": [], "selected_account_id": "", "snapshot": None}), \
                patch.object(demo_admin, "read_only_accounts", return_value=[{"id": "123", "name": "Основной"}]):
            result = demo_admin.connect_broker("private-token")
            self.assertEqual((Path(directory) / "token.txt").read_text(), "private-token")
            self.assertEqual((Path(directory) / "account.txt").read_text(), "123")
            self.assertNotIn("private-token", json.dumps(result))

    def test_portfolio_matches_uid_and_calculates_ruble_weights_with_bond_nkd(self):
        def opener(request, timeout):
            if request.full_url.endswith("GetPositions"):
                data = {"money": [], "securities": [
                    {"instrumentUid": "stock-uid", "balance": "10", "blocked": "2"},
                    {"instrumentUid": "bond-uid", "balance": "2"},
                    {"instrumentUid": "usd-uid", "balance": "1"}],
                    "futures": [{"instrumentUid": "future-uid", "balance": "1"}]}
            else:
                self.assertEqual(json.loads(request.data)["currency"], "RUB")
                def position(uid, kind, price, quantity, currency="rub", nkd=0):
                    return {"instrumentUid": uid, "instrumentType": kind, "ticker": uid.upper(),
                            "quantity": {"units": str(quantity), "nano": 0},
                            "currentPrice": {"currency": currency, "units": str(price), "nano": 0},
                            "currentNkd": {"currency": currency, "units": str(nkd), "nano": 0}}
                data = {"totalAmountPortfolio": {"currency": "rub", "units": "10000", "nano": 0},
                        "positions": [position("stock-uid", "share", 100, 12),
                                      position("bond-uid", "bond", 1000, 2, nkd=50),
                                      position("usd-uid", "share", 50, 1, "usd"),
                                      position("future-uid", "futures", 5000, 1)]}
            return FakeResponse(json.dumps(data).encode())
        assets = account_snapshot("private-token", "123", opener)["positions"]
        self.assertEqual((assets[0]["ticker"], assets[0]["value_rub"], assets[0]["weight_percent"]),
                         ("STOCK-UID", "1200.00", "12.00"))
        self.assertEqual((assets[1]["value_rub"], assets[1]["weight_percent"]), ("2100.00", "21.00"))
        self.assertIsNone(assets[2]["weight_percent"])
        self.assertIsNone(assets[3]["weight_percent"])

    def test_background_portfolio_refresh_is_independent_of_pulse(self):
        with patch.object(demo_admin, "broker_token", return_value="read-token"), \
                patch.object(demo_admin, "refresh_broker") as refresh, \
                patch.object(demo_admin, "AUTH", {"status": "required"}):
            demo_admin.broker_refresh_once()
            refresh.assert_called_once()

    def test_background_failure_preserves_last_snapshot_and_reports_error(self):
        state = {"status": "connected", "snapshot": {"cash_rub": "100"}, "last_check": "old"}
        with patch.object(demo_admin, "BROKER", state), \
                patch.object(demo_admin, "broker_token", return_value="read-token"), \
                patch.object(demo_admin, "refresh_broker", side_effect=BrokerError("Нет связи")):
            demo_admin.broker_refresh_once()
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["snapshot"]["cash_rub"], "100")
            self.assertEqual(state["last_check"], "old")

    def test_switched_account_does_not_receive_previous_account_snapshot(self):
        state = {"status": "connected", "accounts": [{"id": "first"}, {"id": "second"}],
                 "selected_account_id": "first", "snapshot": None}
        def snapshot(*args):
            state["selected_account_id"] = "second"
            return {"cash_rub": "100"}
        with patch.object(demo_admin, "BROKER", state), \
                patch.object(demo_admin, "broker_token", return_value="read-token"), \
                patch.object(demo_admin, "account_snapshot", side_effect=snapshot):
            demo_admin.refresh_broker()
            self.assertIsNone(state["snapshot"])


if __name__ == "__main__":
    unittest.main()
