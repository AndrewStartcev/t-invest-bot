"""Pure read-only bridge tests; no browser, brokerage or Telegram requests."""
import tempfile
import unittest

from extension_source import ExtensionStore

PROFILE = "https://www.tbank.ru/invest/social/profile/LinMath/"


def sample(count, events=(), status="ok"):
    return {"profile_url": PROFILE, "status": status,
            "instruments": [{"ticker": "BSPB", "classCode": "TQBR", "totalOperationsCount": count}],
            "events": list(events)}


def trade():
    return {"ticker": "BSPB", "classCode": "TQBR", "action": "buy",
            "tradeDateTime": "2026-10-29T12:30:00+03:00", "averagePrice": "123.45"}


class ExtensionSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = ExtensionStore(self.tmp.name)

    def test_pairing_is_unguessable_and_private(self):
        self.assertFalse(self.source.authorized("x" * 36))
        key = self.source.pairing_key()
        self.assertTrue(self.source.authorized(key))
        self.assertFalse(self.source.authorized(key[:-1] + "x"))
        self.assertEqual(0o600, self.source.key_path.stat().st_mode & 0o777)

    def test_baseline_and_exactly_once(self):
        events = []
        self.assertEqual(0, self.source.accept(sample(7), PROFILE, emit=events.append)["accepted"])
        self.assertEqual(1, self.source.accept(sample(8, [trade()]), PROFILE, emit=events.append)["accepted"])
        self.assertEqual(0, self.source.accept(sample(8), PROFILE, emit=events.append)["accepted"])
        self.assertEqual(0, self.source.accept(sample(8, [trade()]), PROFILE, emit=events.append)["accepted"])
        self.assertEqual(1, len(events))
        self.assertEqual(1, self.source.public_status()["total_events"])

    def test_incomplete_history_does_not_advance(self):
        self.source.accept(sample(7), PROFILE)
        with self.assertRaisesRegex(ValueError, "Неполная история"):
            self.source.accept(sample(8), PROFILE)
        self.assertEqual(1, self.source.accept(sample(8, [trade()]), PROFILE)["accepted"])

    def test_missing_auth_alerts_then_recovers_once(self):
        messages = []
        self.source.accept(sample(7), PROFILE, alert=lambda *x: messages.append(x))
        denied = {"profile_url": PROFILE, "status": "restricted", "error": "HTTP 403"}
        self.source.accept(denied, PROFILE, alert=lambda *x: messages.append(x))
        self.assertEqual([], messages)
        self.source.accept(denied, PROFILE, alert=lambda *x: messages.append(x))
        self.assertEqual(1, len(messages))
        self.source.accept(denied, PROFILE, alert=lambda *x: messages.append(x))
        self.assertEqual(1, len(messages))
        self.source.accept(sample(7), PROFILE, alert=lambda *x: messages.append(x))
        self.assertEqual("recovered", messages[-1][0])

    def test_disconnected_tab_alerts_once_then_recovers(self):
        import json
        from datetime import datetime, timedelta, timezone
        warnings = []
        alert = lambda *msg: warnings.append(msg)
        self.source.accept(sample(4), PROFILE, alert=alert)
        data = json.loads(self.source.path.read_text(encoding="utf-8"))
        data["last_report"] = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()
        self.source._save(data)
        self.assertTrue(self.source.tick(alert=alert))
        self.assertFalse(self.source.tick(alert=alert))
        self.assertEqual(1, len(warnings))
        self.source.accept(sample(4), PROFILE, alert=alert)
        self.assertEqual("recovered", warnings[-1][0])

    def test_intentional_stop_has_no_offline_alert(self):
        import json
        from datetime import datetime, timedelta, timezone
        notifications = []
        self.source.accept(sample(4), PROFILE)
        self.source.accept({"profile_url": PROFILE, "status": "stopped"}, PROFILE)
        data = json.loads(self.source.path.read_text(encoding="utf-8"))
        data["last_report"] = (datetime.now(timezone.utc) - timedelta(seconds=50)).isoformat()
        self.source._save(data)
        self.assertFalse(self.source.tick(alert=lambda *v: notifications.append(v)))
        self.assertEqual([], notifications)

    def test_wrong_profile_and_forged_events(self):
        with self.assertRaises(ValueError):
            self.source.accept(sample(5), PROFILE.replace("LinMath", "Other"))
        self.source.accept(sample(1), PROFILE)
        with self.assertRaisesRegex(ValueError, "неподтверждённую"):
            self.source.accept(sample(1, [trade()]), PROFILE)
        self.assertEqual(0, self.source.public_status()["total_events"])


if __name__ == "__main__":
    unittest.main()
