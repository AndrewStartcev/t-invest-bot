import tempfile
import threading
import unittest
import json
import queue
import sys
from types import ModuleType
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import demo_admin
from pulse_live import PulseBrowser, PulseError, is_operations_page, operations_url, with_cursor


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
    def test_history_accepts_encoded_pulse_ticker(self):
        browser = PulseBrowser(Path("unused"))
        browser.list_url = "https://www.tbank.ru/api/profile/instrument?sessionId=test"
        requested = []

        def fetch(url):
            requested.append(url)
            return {"ok": True, "status": 200, "data": {"payload": {"items": [], "hasNext": False}}}

        browser._fetch = fetch
        self.assertEqual(browser.history("1COV@DE", "SPBXM"),
                         {"items": [], "hasNext": False, "nextCursor": None})
        self.assertIn("/operation/instrument/1COV%40DE/SPBXM", requested[0])
        self.assertIn("sessionId=test", requested[0])
        browser.history("1COV@DE", "SPBXM", 123456789)
        self.assertIn("cursor=123456789", requested[1])
        self.assertNotIn("nextCursor=", requested[1])
        with self.assertRaisesRegex(PulseError, "История инструмента недоступна"):
            browser.history("..", "SPBXM")

    def test_month_history_reads_all_pages_and_keeps_old_trades_read_only(self):
        now = datetime.now(timezone.utc)
        def trade(days, action="buy"):
            return {"tradeDateTime": (now - timedelta(days=days)).isoformat(),
                    "action": action, "averagePrice": 100, "currency": "rub"}
        class Browser:
            def __init__(self):
                self.cursors = []

            def history(self, ticker, class_code, cursor=None):
                self.cursors.append(cursor)
                if cursor is None:
                    return {"items": [trade(2), trade(8)], "hasNext": True, "nextCursor": "second"}
                return {"items": [trade(29), trade(31)], "hasNext": True, "nextCursor": "third"}
        browser = Browser()
        instrument = {"ticker": "ROSN", "classCode": "TQBR", "showName": "Роснефть", "type": "stock",
                      "maxTradeDateTime": (now - timedelta(days=2)).isoformat()}
        cutoff = now - timedelta(days=30)
        self.assertEqual(len(demo_admin.month_targets([instrument], cutoff)), 1)
        with patch.object(demo_admin, "MONTH", {"status": "loading", "items": [], "processed": 0, "total": 1}):
            scan = {"profile": "LinMath", "cutoff": cutoff, "targets": [instrument], "index": 0, "found": [],
                    "cursor": None, "seen_cursors": set(), "occurrences": {}, "pages": 0}
            self.assertFalse(demo_admin.advance_month_scan(browser, scan, batch_size=1))
            self.assertFalse(demo_admin.advance_month_scan(browser, scan, batch_size=1))
            self.assertTrue(demo_admin.advance_month_scan(browser, scan, batch_size=1))
            self.assertEqual(len(demo_admin.MONTH["items"]), 3)
            self.assertTrue(all(not item["can_demo_buy"] for item in demo_admin.MONTH["items"]))
            self.assertEqual(browser.cursors, [None, "second"])

    def test_month_scan_reports_missing_instrument_and_keeps_other_trades(self):
        now = datetime.now(timezone.utc)

        class Browser:
            def history(self, ticker, class_code, cursor=None):
                if ticker == "MISSING":
                    raise PulseError("Пульс вернул HTTP 404; проверь вход в браузере")
                return {"items": [{"tradeDateTime": now.isoformat(), "action": "buy",
                                   "averagePrice": 100, "currency": "rub"}],
                        "hasNext": False, "nextCursor": None}

        targets = [{"ticker": ticker, "classCode": "TQBR", "showName": ticker, "type": "stock"}
                   for ticker in ("MISSING", "ROSN")]
        scan = {"profile": "LinMath", "cutoff": now - timedelta(days=30), "targets": targets,
                "index": 0, "found": [], "cursor": None, "seen_cursors": set(),
                "occurrences": {}, "pages": 0, "skipped": []}
        with patch.object(demo_admin, "MONTH", {"status": "loading", "items": []}):
            self.assertFalse(demo_admin.advance_month_scan(Browser(), scan, batch_size=2))
            self.assertTrue(demo_admin.advance_month_scan(Browser(), scan))
            self.assertEqual([item["ticker"] for item in demo_admin.MONTH["items"]], ["ROSN"])
            self.assertEqual(demo_admin.MONTH["skipped"], ["MISSING"])
            self.assertIn("частично", demo_admin.MONTH["message"])

    def test_month_request_queues_background_scan(self):
        requests = queue.Queue()
        monitor = {"status": "running", "profile": "LinMath", "instruments": [{"ticker": "ROSN"}]}
        month = {"status": "idle", "items": [], "processed": 0, "total": 0, "message": "", "loaded_at": None}
        with patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                patch.object(demo_admin, "MONITOR", monitor), patch.object(demo_admin, "MONTH", month), \
                patch.object(demo_admin, "HISTORY_REQUESTS", requests), \
                patch.object(demo_admin, "load_settings", return_value=demo_admin.DEFAULTS):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                request = Request(f"http://127.0.0.1:{server.server_port}/api/month",
                                  data=b"{}", headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(month["status"], "loading")
                self.assertEqual(requests.get_nowait()["action"], "month")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_second_admin_cannot_bind_same_port(self):
        first = demo_admin.LocalHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
        try:
            with self.assertRaises(OSError):
                second = demo_admin.LocalHTTPServer(("127.0.0.1", first.server_port), demo_admin.Handler)
                second.server_close()
        finally:
            first.server_close()

    def test_saving_settings_keeps_authenticated_admin(self):
        auth = {"status": "authenticated", "message": "Вход подтверждён"}
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "AUTH", auth), \
                patch.object(demo_admin, "SETTINGS_PATH", Path(folder) / "settings.json"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                settings = {**demo_admin.DEFAULTS, "profile_url": demo_admin.DEFAULTS["profile_url"] + ""}
                request = Request(f"http://127.0.0.1:{server.server_port}/api/settings",
                                  data=json.dumps(settings).encode(), headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(json.load(response)["settings"], settings)
                self.assertEqual(auth["status"], "authenticated")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_settings_are_accessible_while_pulse_is_disconnected(self):
        auth = {"status": "required", "message": "Требуется вход"}
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "AUTH", auth), \
                patch.object(demo_admin, "SETTINGS_PATH", Path(folder) / "settings.json"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                settings = demo_admin.DEFAULTS.copy()
                request = Request(f"http://127.0.0.1:{server.server_port}/api/settings",
                                  data=json.dumps(settings).encode(), headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(json.load(response)["settings"], settings)
                self.assertEqual(auth["status"], "required")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_recent_stock_buy_is_demo_only_and_cannot_repeat(self):
        trade_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        candidate = {"id": "test-buy", "profile": "LinMath", "ticker": "TEST", "classCode": "TQBR",
                     "name": "Тест", "action": "buy", "tradeDateTime": trade_time, "price": 100,
                     "currency": "rub", "can_demo_buy": True}
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                patch.object(demo_admin, "TODAY", [candidate]), patch.object(demo_admin, "EVENTS", []), \
                patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), \
                patch.object(demo_admin, "POSITIONS_PATH", Path(folder) / "positions.json"), patch.object(demo_admin, "POSITIONS", {}), \
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

    def test_login_return_opens_trades_automatically_once(self):
        class Page:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def is_closed(self):
                return self.closed

        browser = PulseBrowser(Path("unused"), headless=False)
        login = Page("https://id.tbank.ru/auth/step")
        landing = Page("https://www.tbank.ru/invest/")
        browser.context = type("Context", (), {"pages": [landing, login]})()
        browser.page = landing
        profile = demo_admin.DEFAULTS["profile_url"]
        with patch.object(browser, "refresh") as refresh:
            self.assertFalse(browser.return_to_trades_after_login(profile))
            self.assertTrue(browser.saw_login_page)
            login.closed = True
            self.assertTrue(browser.return_to_trades_after_login(profile))
            refresh.assert_called_once_with(profile)
            self.assertFalse(browser.return_to_trades_after_login(profile))

    def test_visible_trades_without_api_are_reloaded_without_login_prompt(self):
        class Page:
            url = "https://www.tbank.ru/invest/pulse/profile/LinMath/operations/"

            def is_closed(self):
                return False

        browser = PulseBrowser(Path("unused"), headless=False)
        browser.page = Page()
        browser.context = type("Context", (), {"pages": [browser.page]})()
        browser.opened_at -= 10
        with patch.object(browser, "visible_trades_page", return_value=True), patch.object(browser, "refresh") as refresh:
            self.assertTrue(browser.return_to_trades_after_login(demo_admin.DEFAULTS["profile_url"]))
            refresh.assert_called_once()

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

    def test_visible_trade_page_without_api_does_not_claim_login(self):
        class VisibleBrowser:
            headless = False
            list_url = None

            def recover_page(self):
                return True

            def visible_trades_page(self, profile_url):
                return True

        auth = {"status": "waiting", "message": "Ожидаем входа"}
        with patch.object(demo_admin, "AUTH", auth), patch.object(demo_admin, "MONITOR", {}):
            self.assertFalse(demo_admin.handle_poll_error(
                VisibleBrowser(), PulseError("Сделки не загрузились"), demo_admin.DEFAULTS["profile_url"]))
        self.assertEqual(auth["status"], "waiting")
        self.assertIn("Вход в Пульс виден", auth["message"])

    def test_closed_headless_browser_keeps_saved_login_and_retries(self):
        class ClosedBrowser:
            headless = True
            list_url = "https://example.invalid/instrument"

            def recover_page(self):
                raise RuntimeError("browser context closed")

        auth = {"status": "authenticated", "message": "Вход подтверждён"}
        monitor = {"status": "running", "message": "Данные получены"}
        with patch.object(demo_admin, "AUTH", auth), patch.object(demo_admin, "MONITOR", monitor):
            self.assertTrue(demo_admin.handle_poll_error(
                ClosedBrowser(), RuntimeError("TargetClosedError"), demo_admin.DEFAULTS["profile_url"]))
        self.assertEqual(auth["status"], "authenticated")
        self.assertIn("сохранённой сессии", auth["message"])
        self.assertEqual(monitor["status"], "error")

    def test_expired_login_requires_authorization(self):
        class ClosedBrowser:
            headless = True
            list_url = None

            def recover_page(self):
                return False

        auth = {"status": "authenticated", "message": "Вход подтверждён"}
        with patch.object(demo_admin, "AUTH", auth), patch.object(demo_admin, "MONITOR", {}):
            self.assertTrue(demo_admin.handle_poll_error(
                ClosedBrowser(), PulseError("Пульс вернул HTTP 401"), demo_admin.DEFAULTS["profile_url"]))
        self.assertEqual(auth["status"], "required")

    def test_session_cookies_are_saved_locally_without_other_domains(self):
        with tempfile.TemporaryDirectory() as folder:
            browser = PulseBrowser(Path(folder), headless=True)
            browser.context = type("Context", (), {"cookies": lambda self: [
                {"name": "session", "value": "saved", "domain": ".tbank.ru", "path": "/", "expires": -1},
                {"name": "other", "value": "discard", "domain": ".example.com", "path": "/", "expires": -1},
                {"name": "lookalike", "value": "discard", "domain": ".nottbank.ru", "path": "/", "expires": -1},
            ]})()
            browser.save_session()
            saved = json.loads((Path(folder) / "pulse-session.json").read_text(encoding="utf-8"))
            self.assertEqual([cookie["name"] for cookie in saved["cookies"]], ["session"])
            with patch("pulse_live.os.replace") as replace:
                browser.save_session()
                replace.assert_not_called()

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

    def test_failed_browser_open_reports_original_error(self):
        class StopLoop(Exception):
            pass

        class Requests:
            calls = 0

            def get(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"action": "auth_start"}
                raise StopLoop

        class FailedBrowser:
            def __init__(self, *args, **kwargs):
                pass

            def open(self, profile_url):
                raise RuntimeError("launch failed")

        auth = {"status": "required", "message": ""}
        with patch.object(demo_admin, "AUTH", auth), patch.object(demo_admin, "MONTH", {}), \
                patch.object(demo_admin, "HISTORY_REQUESTS", Requests()), \
                patch.object(demo_admin, "PulseBrowser", FailedBrowser):
            with self.assertRaises(StopLoop):
                demo_admin.monitor_loop()
        self.assertEqual(auth["status"], "required")
        self.assertEqual(auth["message"], "Окно Пульса не открылось: launch failed")

    def test_window_raise_failure_does_not_close_browser(self):
        class Page:
            raised = False

            def bring_to_front(self):
                self.raised = True

        browser = PulseBrowser(Path("unused"), headless=False)
        browser.page = Page()
        with patch("pulse_live.visible_browser_windows", side_effect=AttributeError("Win32 unavailable")):
            browser.show()
        self.assertTrue(browser.page.raised)

    def test_certificate_error_stops_login_with_clear_message(self):
        closed = []

        class Page:
            def goto(self, *args, **kwargs):
                raise RuntimeError("Page.goto: net::ERR_CERT_AUTHORITY_INVALID")

        class Context:
            pages = [Page()]

            def on(self, *args):
                pass

            def close(self):
                closed.append(True)

        class Chromium:
            def launch_persistent_context(self, *args, **kwargs):
                return Context()

        class Playwright:
            chromium = Chromium()

            def stop(self):
                pass

        class Manager:
            def start(self):
                return Playwright()

        package = ModuleType("playwright")
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: Manager()
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {
                "playwright": package, "playwright.sync_api": sync_api}), \
                patch("pulse_live.browser_executable", return_value="fake-browser"), \
                patch("pulse_live.visible_browser_windows", return_value={}):
            browser = PulseBrowser(Path(folder))
            with self.assertRaisesRegex(PulseError, "ERR_CERT_AUTHORITY_INVALID"):
                browser.open(demo_admin.DEFAULTS["profile_url"])
        self.assertEqual(closed, [True])

    def test_profile_and_cursor(self):
        self.assertEqual(operations_url("https://www.tbank.ru/invest/social/profile/LinMath/")[0], "LinMath")
        self.assertTrue(is_operations_page("https://www.tbank.ru/invest/social/profile/LinMath/operations?view=all",
                                           "https://www.tbank.ru/invest/social/profile/LinMath/"))
        self.assertFalse(is_operations_page("https://www.tbank.ru/invest/pulse/profile/Another/operations/",
                                            "https://www.tbank.ru/invest/social/profile/LinMath/"))
        self.assertIn("nextCursor=next", with_cursor("https://www.tbank.ru/example?sessionId=secret", "next"))
        self.assertIn("cursor=next", with_cursor("https://www.tbank.ru/example?sessionId=secret", "next", parameter="cursor"))
        with self.assertRaises(Exception):
            operations_url("https://example.com/invest/social/profile/LinMath/")

    def test_check_login_navigates_existing_tab_to_trades(self):
        class Page:
            url = "https://www.tbank.ru/invest/"

            def is_closed(self):
                return False

            def goto(self, url, **kwargs):
                self.url = url

        browser = PulseBrowser(Path("unused"))
        browser.page = Page()
        browser.context = type("Context", (), {"pages": [browser.page]})()
        browser.nickname_url = "old"
        browser.instrument_urls = {"old": "old"}
        browser.target_profile_id = "old"
        browser.refresh(demo_admin.DEFAULTS["profile_url"])
        self.assertTrue(is_operations_page(browser.page.url, demo_admin.DEFAULTS["profile_url"]))
        self.assertIsNone(browser.nickname_url)
        self.assertEqual(browser.instrument_urls, {})

    def test_baseline_new_buys_and_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "STATE_PATH", Path(folder) / "state.json"), patch.object(demo_admin, "EVENTS_PATH", Path(folder) / "events.json"), patch.object(demo_admin, "EVENTS", []), patch.object(demo_admin, "POSITIONS_PATH", Path(folder) / "positions.json"), patch.object(demo_admin, "POSITIONS", {}):
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

    def test_recent_bond_and_fund_buys_can_be_repeated_conditionally(self):
        now = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

        class HistoryBrowser:
            def history(self, ticker, class_code, cursor=None):
                return {"items": [{"tradeDateTime": now, "action": "buy", "averagePrice": 100,
                                   "currency": "rub"}], "hasNext": False, "nextCursor": None}

        instruments = [{"ticker": ticker, "classCode": code, "showName": ticker,
                        "maxTradeDateTime": now} for ticker, code in
                       [("BOND", "TQOB"), ("FUND", "TQTF"), ("FUT", "SPBFUT")]]
        recent = demo_admin.recent_profile_trades(HistoryBrowser(), "LinMath", instruments)
        by_ticker = {item["ticker"]: item["can_demo_buy"] for item in recent}
        self.assertEqual(by_ticker, {"BOND": True, "FUND": True, "FUT": False})

    def test_auto_copy_button_enables_monitoring_without_broker_order(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_admin, "AUTH", {"status": "required"}), \
                patch.object(demo_admin, "SETTINGS_PATH", Path(folder) / "settings.json"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                request = Request(f"http://127.0.0.1:{server.server_port}/api/auto-copy",
                                  data=b'{"enabled":true}', headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    settings = json.load(response)["settings"]
                self.assertTrue(settings["monitoring_enabled"])
                self.assertTrue(settings["auto_demo_buy"])
                self.assertTrue(demo_admin.load_settings()["auto_demo_buy"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
