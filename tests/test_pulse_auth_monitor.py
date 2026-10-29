"""Pulse extension monitors login only: no deals and no bank secrets."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pulse_auth_monitor import PulseAuthStore
from extension_source import ExtensionStore


class AuthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PulseAuthStore(self.temp.name)
        self.alerts = []

    def submit(self, status):
        return self.store.accept_auth({"kind": "auth_status", "status": status},
                                      alert=lambda *message: self.alerts.append(message))

    def test_old_trade_report_rejected(self):
        with self.assertRaisesRegex(ValueError, "только проверку"):
            self.store.accept_auth({"status": "ok", "instruments": [{"ticker": "BSPB"}]})
        self.assertFalse(self.store.auth_path.exists())

    def test_only_expected_keys_accepted(self):
        for report in [
            {"kind": "auth_status", "status": "authenticated", "cookie": "secret"},
            {"kind": "auth_status", "status": "authenticated", "profile_url": "bank"},
            {"kind": "auth_status", "status": "ok"},
        ]:
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.store.accept_auth(report)

    def test_status_and_deduplicated_alert_recovery(self):
        self.submit("authenticated")
        self.assertEqual("authenticated", self.store.public_status()["last_status"])
        self.submit("login_required")
        self.assertEqual(1, len(self.alerts))
        self.assertEqual("incident", self.alerts[0][0])
        self.submit("login_required")
        self.assertEqual(1, len(self.alerts))
        self.submit("authenticated")
        self.assertEqual("recovered", self.alerts[-1][0])
        self.assertEqual(2, len(self.alerts))
        self.assertIsNone(self.store.public_status()["incident"])

    def test_unknown_is_not_proven_logout(self):
        self.submit("authenticated")
        self.submit("unknown")
        self.assertEqual("unknown", self.store.public_status()["last_status"])
        self.assertEqual([], self.alerts)

    def test_tab_disconnect_once_and_stop(self):
        self.submit("authenticated")
        state = json.loads(self.store.auth_path.read_text(encoding="utf-8"))
        state["last_report"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        self.store._auth_save(state)
        self.assertTrue(self.store.tick(alert=lambda *m: self.alerts.append(m)))
        self.assertFalse(self.store.tick(alert=lambda *m: self.alerts.append(m)))
        self.assertEqual("offline", self.store.public_status()["last_status"])
        self.assertEqual(1, len(self.alerts))
        self.submit("authenticated")
        self.assertEqual("recovered", self.alerts[-1][0])
        self.submit("stopped")
        state = json.loads(self.store.auth_path.read_text(encoding="utf-8"))
        state["last_report"] = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
        self.store._auth_save(state)
        self.assertFalse(self.store.tick(alert=lambda *m: self.alerts.append(m)))
        self.assertEqual("stopped", self.store.public_status()["last_status"])

    def test_existing_pairing_key_is_kept(self):
        previous = ExtensionStore(self.temp.name).pairing_key()
        self.assertTrue(self.store.authorized(previous))
        self.assertEqual(previous, self.store.pairing_key())


if __name__ == "__main__":
    unittest.main()
