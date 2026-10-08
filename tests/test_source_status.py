import json
import os
import queue
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

import demo_admin


@contextmanager
def client_server():
    with patch.dict(os.environ, {"TINVEST_ROLE": "client", "TINVEST_BACKEND_KEY": ""}), \
            patch.object(demo_admin, "shared_source", return_value=True):
        server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


class SourceStatusTests(unittest.TestCase):
    def test_diagnostic_is_persisted_once_and_repeats_after_recovery(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder, patch.object(demo_admin, "EVENTS", []), \
                patch.object(demo_admin, "PULSE_FAILURES", {}), \
                patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), \
                patch.object(demo_admin, "notify") as notify:
            for _ in range(3):
                demo_admin.pulse_failure("LinMath", "history", "История недоступна")
            stored = json.loads(demo_admin.EVENTS_PATH.read_text())
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["source"], "pulse_error")
            self.assertIn("Загрузка сделок", stored[0]["reason"])
            self.assertNotIn("real_status", stored[0])
            self.assertTrue(stored[0]["time"])
            notify.assert_not_called()
            demo_admin.pulse_recovered("LinMath", "history")
            demo_admin.pulse_failure("LinMath", "history", "История недоступна")
            self.assertEqual(len(demo_admin.EVENTS), 2)

    def test_new_author_can_be_verified_while_source_recovers_from_old_author(self):
        from unittest.mock import Mock
        payload={"action":"snapshot", "profile_url":"https://www.tbank.ru/invest/social/profile/SpaceForcez/", "counts":{}}
        browser=Mock()
        with patch.object(demo_admin,"AUTH",{"status":"waiting"}), \
                patch.object(demo_admin,"read_source",return_value={"profile":"SpaceForcez","instruments":[]}) as read:
            demo_admin.verified_source_read(browser,payload)
            read.assert_called_once_with(browser,payload)
            browser.confirm_session.assert_called_once()
            self.assertEqual(demo_admin.AUTH["status"],"authenticated")
        with patch.object(demo_admin,"AUTH",{"status":"waiting"}), \
                patch.object(demo_admin,"read_source",side_effect=demo_admin.PulseError("HTTP 401")):
            browser.reset_mock()
            with self.assertRaises(demo_admin.PulseError):demo_admin.verified_source_read(browser,payload)
            self.assertEqual(demo_admin.AUTH["status"],"waiting")
            browser.confirm_session.assert_not_called()

    def test_author_reset_discards_displayed_data_but_preserves_journal(self):
        old_event={"profile":"Old","trade":"ДЕМО: куплено"}
        with patch.object(demo_admin,"AUTH",{"status":"required"}),patch.object(demo_admin,"MONITOR",{"status":"error","message":"old error"}), \
                patch.object(demo_admin,"TODAY",[{"profile":"Old"}]),patch.object(demo_admin,"MONTH",{}), \
                patch.object(demo_admin,"INVESTOR_PORTFOLIO",{"profile_url":"Old","rows":[{}]}), \
                patch.object(demo_admin,"EVENTS",[old_event]),patch.object(demo_admin,"PULSE_RETRIES",{"New":{"attempts":10}}):
            demo_admin.reset_author_view("New")
            self.assertEqual(demo_admin.MONITOR["status"],"checking")
            self.assertNotIn("old error",demo_admin.MONITOR["message"])
            self.assertEqual(demo_admin.TODAY,[])
            self.assertEqual(demo_admin.INVESTOR_PORTFOLIO["profile_url"],"New")
            self.assertEqual(demo_admin.EVENTS,[old_event])
            self.assertNotIn("New",demo_admin.PULSE_RETRIES)

    def test_partial_history_is_one_notice_instead_of_repeated_errors(self):
        with patch.object(demo_admin, "PULSE_FAILURES", {}), patch.object(demo_admin, "add_event") as record:
            demo_admin.pulse_history_pending("LinMath", 5)
            demo_admin.pulse_history_pending("LinMath", 8)
            record.assert_called_once()
            event = record.call_args.args[0]
            self.assertEqual(event["source"], "pulse_status")
            self.assertEqual(event["severity"], "info")
            self.assertIn("Получено сделок: 5", event["reason"])
            self.assertNotIn("Ошибка", event["trade"])
            demo_admin.pulse_recovered("LinMath", "history_partial")
            demo_admin.pulse_history_pending("LinMath", 1)
            self.assertEqual(record.call_count, 2)

    def test_source_waits_before_reporting_an_incident(self):
        with patch.object(demo_admin, "PULSE_RETRIES", {}), \
                patch.object(demo_admin, "pulse_failure") as failure:
            message = "Общий источник не подключён; вход выполняет владелец"
            first = demo_admin.pulse_retry("LinMath", "instruments", message, now=0)
            self.assertEqual(first["status"], "retrying")
            self.assertEqual(first["phase"], "authorization")
            self.assertNotIn("Ошибка", first["message"])
            for when in (30, 60, 90):
                self.assertEqual(demo_admin.pulse_retry("LinMath", "instruments", message, now=when)["status"], "retrying")
            failure.assert_not_called()
            self.assertEqual(demo_admin.pulse_retry("LinMath", "instruments", message, now=121)["status"], "error")
            failure.assert_called_once()
            demo_admin.PULSE_RETRIES.pop("LinMath")
            self.assertEqual(demo_admin.pulse_retry("LinMath", "instruments", message, now=125)["status"], "retrying")

    def test_failed_read_is_visible_without_exposing_stale_trades(self):
        message = "Общий источник временно недоступен"
        monitor = {**demo_admin.MONITOR, "status": "error", "message": message,
                   "last_check": "2026-10-06T12:00:00+00:00", "instruments": [{"ticker": "OLD"}]}
        with patch.object(demo_admin, "AUTH", {"status": "required", "message": message}), \
                patch.object(demo_admin, "MONITOR", monitor), \
                patch.object(demo_admin, "TODAY", [{"id": "old-trade"}]), client_server() as base:
            with urlopen(base + "/api/state", timeout=2) as response:
                state = json.load(response)
            self.assertEqual(state["monitor"]["status"], "error")
            self.assertEqual(state["monitor"]["message"], message)
            self.assertEqual(state["auth"]["message"], message)
            self.assertEqual(state["monitor"]["last_check"], monitor["last_check"])
            self.assertEqual(state["monitor"]["instruments"], [])
            self.assertEqual(state["today"], [])
            self.assertIsNone(state["browser_ui_url"])

    def test_disconnected_client_can_retry_without_opening_owner_browser(self):
        requests = queue.Queue()
        monitor = {**demo_admin.MONITOR, "status": "error"}
        with patch.object(demo_admin, "AUTH", {"status": "required"}), \
                patch.object(demo_admin, "MONITOR", monitor), \
                patch.object(demo_admin, "HISTORY_REQUESTS", requests), client_server() as base:
            for _ in range(2):
                request = Request(base + "/api/source/refresh", data=b"{}",
                                  headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
            self.assertEqual(requests.get_nowait(), {"action": "source_refresh"})
            self.assertTrue(requests.empty())
            self.assertEqual(demo_admin.AUTH["status"], "required")

    def test_retry_triggers_poll_before_normal_interval_and_respects_schedule(self):
        for is_open in (True, False):
            with self.subTest(schedule_open=is_open), \
                    patch.object(demo_admin, "load_settings", return_value=demo_admin.DEFAULTS), \
                    patch.object(demo_admin, "scheduled_pause", return_value=False), \
                    patch.object(demo_admin, "schedule_open", return_value=is_open), \
                    patch.object(demo_admin, "RemotePulse"), \
                    patch.object(demo_admin, "AUTH", {}), \
                    patch.object(demo_admin, "MONITOR", {}), \
                    patch.object(demo_admin, "MONTH", {}), \
                    patch.object(demo_admin, "TODAY", []), \
                    patch.object(demo_admin, "scheduled_poll", return_value=is_open) as poll, \
                    patch.object(demo_admin.HISTORY_REQUESTS, "get", side_effect=[None, {"action": "source_refresh"}, RuntimeError("stop loop")]):
                with self.assertRaisesRegex(RuntimeError, "stop loop"):
                    demo_admin.remote_monitor_loop()
                self.assertEqual(poll.call_count, 2)

    def test_failed_read_does_not_retry_every_half_second(self):
        with patch.object(demo_admin, "load_settings", return_value=demo_admin.DEFAULTS), \
                patch.object(demo_admin, "scheduled_pause", return_value=False), \
                patch.object(demo_admin, "schedule_open", return_value=True), \
                patch.object(demo_admin, "RemotePulse"), \
                patch.object(demo_admin, "AUTH", {}), \
                patch.object(demo_admin, "MONITOR", {}), \
                patch.object(demo_admin, "MONTH", {}), \
                patch.object(demo_admin, "TODAY", []), \
                patch.object(demo_admin.time, "monotonic", side_effect=[0, 0, 0, 1]), \
                patch.object(demo_admin, "scheduled_poll", side_effect=demo_admin.PulseError("Ошибка чтения")) as poll, \
                patch.object(demo_admin.HISTORY_REQUESTS, "get", side_effect=[None, None, RuntimeError("stop loop")]):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                demo_admin.remote_monitor_loop()
            self.assertEqual(poll.call_count, 1)
            self.assertEqual(demo_admin.MONITOR["status"], "retrying")
            self.assertEqual(demo_admin.AUTH["status"], "checking")


if __name__ == "__main__":
    unittest.main()
