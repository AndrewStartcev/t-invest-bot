import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import demo_admin
from monitor_schedule import schedule_open, validate_schedule


class ScheduleTests(unittest.TestCase):
    def settings(self, days=None, start="09:00", end="19:00"):
        return {**demo_admin.DEFAULTS, "monitoring_enabled": True,
                "schedule": {"enabled": True, "days": days or [0], "start": start, "end": end}}

    def test_moscow_boundaries_and_weekdays(self):
        settings = self.settings()
        for stamp, expected in [("2026-10-05T05:59:59+00:00", False),
                                ("2026-10-05T06:00:00+00:00", True),
                                ("2026-10-05T15:59:59+00:00", True),
                                ("2026-10-05T16:00:00+00:00", False),
                                ("2026-10-06T06:00:00+00:00", False)]:
            self.assertEqual(schedule_open(settings, datetime.fromisoformat(stamp)), expected, stamp)

    def test_overnight_window_belongs_to_start_day_and_wraps_week(self):
        settings = self.settings([6], "22:00", "02:00")
        for stamp, expected in [("2026-10-04T23:00:00+03:00", True),
                                ("2026-10-05T01:59:59+03:00", True),
                                ("2026-10-05T02:00:00+03:00", False),
                                ("2026-10-04T01:00:00+03:00", False)]:
            self.assertEqual(schedule_open(settings, datetime.fromisoformat(stamp)), expected, stamp)

    def test_disabled_schedule_keeps_round_the_clock_behavior(self):
        self.assertTrue(schedule_open(demo_admin.DEFAULTS, datetime(2026, 10, 4, tzinfo=timezone.utc)))

    def test_invalid_values_are_rejected(self):
        for changes in [{"days": []}, {"days": [True]}, {"days": [0, 0]}, {"days": [7]},
                        {"start": "24:00"}, {"end": "9:00"}, {"end": "09:00"}, {"enabled": 1}]:
            with self.assertRaises(ValueError):
                validate_schedule({**self.settings()["schedule"], **changes})

    def test_pause_makes_no_source_request_and_resume_does_not_replay(self):
        with patch.object(demo_admin, "schedule_open", return_value=False), patch.object(demo_admin, "poll_once") as poll:
            was_open = demo_admin.scheduled_poll(object(), self.settings(), True)
            self.assertFalse(was_open)
            poll.assert_not_called()
        with patch.object(demo_admin, "schedule_open", return_value=True), patch.object(demo_admin, "poll_once") as poll:
            browser, settings = object(), self.settings()
            was_open = demo_admin.scheduled_poll(browser, settings, was_open)
            poll.assert_called_once_with(browser, settings, emit_events=False)
            poll.reset_mock()
            demo_admin.scheduled_poll(browser, settings, was_open)
            poll.assert_called_once_with(browser, settings, emit_events=True)

    def test_legacy_settings_default_and_schedule_persisted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "SETTINGS_PATH", Path(folder) / "settings.json"):
            self.assertFalse(demo_admin.load_settings()["schedule"]["enabled"])
            demo_admin.save_settings(self.settings([0, 2, 4], "10:00", "18:30"))
            self.assertEqual(demo_admin.load_settings()["schedule"], self.settings([0, 2, 4], "10:00", "18:30")["schedule"])
