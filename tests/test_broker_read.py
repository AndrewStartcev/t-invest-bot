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


if __name__ == "__main__":
    unittest.main()
