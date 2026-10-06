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
                patch.object(demo_admin.time, "monotonic", side_effect=[0, 0, 1]), \
                patch.object(demo_admin, "scheduled_poll", side_effect=demo_admin.PulseError("Ошибка чтения")) as poll, \
                patch.object(demo_admin.HISTORY_REQUESTS, "get", side_effect=[None, None, RuntimeError("stop loop")]):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                demo_admin.remote_monitor_loop()
            self.assertEqual(poll.call_count, 1)


if __name__ == "__main__":
    unittest.main()
