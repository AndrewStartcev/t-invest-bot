import tempfile
import threading
import unittest
import json
import queue
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import demo_admin
from pulse_live import PulseBrowser, operations_url, with_cursor


class FakeBrowser:
    def __init__(self):
        self.count = 12

    def snapshot(self, url, counts):
        history = [
            {"tradeDateTime": "2026-10-03T10:33:00+03:00", "action": "buy", "averagePrice": 100, "currency": "rub"},
            {"tradeDateTime": "2026-10-03T10:33:00+03:00", "action": "buy", "averagePrice": 100, "currency": "rub"},
        ] if counts.get("DEMO:TQBR", 0) < self.count else []
        return "LinMath", [{"ticker": "DEMO", "classCode": "TQBR", "showName": "Тест", "type": "stock",
                            "totalOperationsCount": self.count, "maxTradeDateTime": "2026-10-03T10:33:00+03:00", "history": history}]

    def history(self, ticker, class_code, cursor=None):
        return {"items": [], "hasNext": False, "nextCursor": None}


class LiveMonitorTests(unittest.TestCase):
    def test_recent_stock_buy_is_demo_only_and_cannot_repeat(self):
        trade_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        candidate = {"id": "test-buy", "profile": "LinMath", "ticker": "TEST", "classCode": "TQBR",
                     "name": "Тест", "action": "buy", "tradeDateTime": trade_time, "price": 100,
                     "currency": "rub", "can_demo_buy": True}
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                patch.object(demo_admin, "TODAY", [candidate]), patch.object(demo_admin, "EVENTS", []), \
                patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), \
                patch.object(demo_admin, "load_settings", return_value={"profile_url": "https://www.tbank.ru/invest/social/profile/LinMath/", "paused": False, "chat_id": ""}), \
                patch.object(demo_admin, "notify", side_effect=lambda event, settings, message: event.update(notification="not sent")):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/demo/buy"
                request = Request(url, data=b'{"id":"test-buy"}', headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(json.load(response)["event"]["trade"], "ДЕМО: куплено")
                self.assertEqual(len(demo_admin.EVENTS), 1)
                with self.assertRaises(HTTPError) as repeated:
                    urlopen(request, timeout=2)
                self.assertEqual(repeated.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_closed_login_tab_recovers_open_trades_tab(self):
        class Page:
            def __init__(self, url, closed=False):
                self.url = url
                self.closed = closed

            def is_closed(self):
                return self.closed

        browser = PulseBrowser(Path("unused"))
        login = Page("https://www.tbank.ru/auth/", closed=True)
        trades = Page("https://www.tbank.ru/invest/pulse/profile/LinMath/operations/")
        browser.page = login
        browser.context = type("Context", (), {"pages": [login, trades]})()
        self.assertTrue(browser.recover_page())
        self.assertIs(browser.page, trades)

    def test_browser_read_error_keeps_visible_login_window(self):
        class VisibleBrowser:
            headless = False
            list_url = "https://example.invalid/instrument"

            def recover_page(self):
                return True

            def visible_trades_page(self, profile_url):
                return True

            class page:
                @staticmethod
                def is_closed():
                    return False

        browser = VisibleBrowser()
        with patch.object(demo_admin, "AUTH", {"status": "authenticated", "message": ""}), patch.object(
            demo_admin, "MONITOR", {"status": "running", "message": ""}
        ):
            self.assertFalse(demo_admin.handle_poll_error(browser, TimeoutError(), "https://www.tbank.ru/invest/social/profile/LinMath/"))
            self.assertIsNone(browser.list_url)
            self.assertEqual(demo_admin.AUTH["status"], "authenticated")
            self.assertIn("TimeoutError", demo_admin.MONITOR["message"])

    def test_auth_button_queues_one_open_without_waiting_for_browser(self):
        requests = queue.Queue()
        auth = {"status": "required", "message": "Нужен вход"}
        with patch.object(demo_admin, "HISTORY_REQUESTS", requests), patch.object(demo_admin, "AUTH", auth):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/auth/start"
                request = Request(url, data=b"{}", headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                    self.assertTrue(json.load(response)["ok"])
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(requests.qsize(), 1)
                self.assertEqual(auth["status"], "opening")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_profile_and_cursor(self):
        self.assertEqual(operations_url("https://www.tbank.ru/invest/social/profile/LinMath/")[0], "LinMath")
        self.assertIn("nextCursor=next", with_cursor("https://www.tbank.ru/example?sessionId=secret", "next"))
        with self.assertRaises(Exception):
            operations_url("https://example.com/invest/social/profile/LinMath/")

    def test_baseline_new_buys_and_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "STATE_PATH", Path(folder) / "state.json"), patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), patch.object(demo_admin, "EVENTS", []):
            browser = FakeBrowser()
            settings = {"profile_url": "https://www.tbank.ru/invest/social/profile/LinMath/", "auto_demo_buy": True,
                        "paused": False, "chat_id": ""}
            demo_admin.poll_once(browser, settings)
            self.assertEqual(demo_admin.EVENTS, [])
            browser.count = 14
            demo_admin.poll_once(browser, settings)
            self.assertEqual(len(demo_admin.EVENTS), 2)
            self.assertTrue(all(item["trade"] == "ДЕМО: куплено" for item in demo_admin.EVENTS))
            self.assertTrue(all(item["notification"] == "не отправлено: ID не указан" for item in demo_admin.EVENTS))
            demo_admin.poll_once(browser, settings)
            self.assertEqual(len(demo_admin.EVENTS), 2)

    def test_recent_trades_only_last_day_and_stock_buys(self):
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(hours=2)).isoformat()
        old = (now - timedelta(days=2)).isoformat()

        class HistoryBrowser:
            def history(self, ticker, class_code, cursor=None):
                return {"items": [
                    {"tradeDateTime": fresh, "action": "buy", "averagePrice": 101, "currency": "rub"},
                    {"tradeDateTime": fresh, "action": "sell", "averagePrice": 102, "currency": "rub"},
                    {"tradeDateTime": old, "action": "buy", "averagePrice": 99, "currency": "rub"},
                ], "hasNext": False, "nextCursor": None}

        instruments = [{"ticker": "TEST", "classCode": "TQBR", "showName": "Акция", "maxTradeDateTime": fresh}]
        recent = demo_admin.recent_profile_trades(HistoryBrowser(), "LinMath", instruments)
        self.assertEqual(len(recent), 2)
        self.assertEqual([item["can_demo_buy"] for item in recent], [True, False])


if __name__ == "__main__":
    unittest.main()
