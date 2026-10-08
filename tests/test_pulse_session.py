import os
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import demo_admin
from pulse_live import PulseBrowser, PulseError


class PulseSessionTests(unittest.TestCase):
    def browser(self, root, url="https://id.tbank-online.com/auth", text="Введите код быстрого доступа"):
        browser = PulseBrowser(Path(root), headless=True)
        page = MagicMock()
        page.url = url
        page.is_closed.return_value = False
        field = MagicMock()
        field.is_visible.return_value = True
        page.locator.return_value.inner_text.return_value = text
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.nth.return_value = field
        browser.context = MagicMock(pages=[page])
        browser.page = page
        return browser, field

    def test_pin_only_once_even_after_browser_restart(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"PULSE_QUICK_PIN": "2468"}):
            browser, field = self.browser(root)
            self.assertTrue(browser.try_quick_login())
            field.press_sequentially.assert_called_once_with("2468", delay=100, timeout=3000)
            restarted, another_field = self.browser(root)
            self.assertFalse(restarted.try_quick_login())
            another_field.press_sequentially.assert_not_called()
            with patch.object(restarted, "save_session"):
                restarted.confirm_session()
            self.assertFalse((Path(root) / "quick-login-attempted").exists())

    def test_does_not_send_pin_to_sms_password_or_foreign_pages(self):
        cases = [("https://id.tbank-online.com/auth", "Введите код из СМС"),
                 ("https://id.tbank-online.com/auth", "Введите пароль"),
                 ("https://id.tbank-online.com/auth", "Код быстрого доступа. Неверный код"),
                 ("https://evil.example/auth", "Введите код быстрого доступа"),
                 ("http://id.tbank-online.com/auth", "Введите код быстрого доступа")]
        for url, text in cases:
            with self.subTest(url=url, text=text), tempfile.TemporaryDirectory() as root, \
                    patch.dict(os.environ, {"PULSE_QUICK_PIN": "2468"}):
                browser, field = self.browser(root, url, text)
                self.assertFalse(browser.try_quick_login())
                field.press_sequentially.assert_not_called()

    def test_maintenance_refreshes_page_without_scanning_trades(self):
        with tempfile.TemporaryDirectory() as root:
            browser, _ = self.browser(root, "https://www.tbank-online.com/invest/")
            with patch.object(browser, "try_quick_login", return_value=False), \
                    patch.object(browser, "refresh") as refresh, patch.object(browser, "save_session"), \
                    patch.object(browser, "snapshot") as snapshot:
                browser.maintain_session(demo_admin.DEFAULTS["profile_url"])
                refresh.assert_called_once()
                snapshot.assert_not_called()
                browser.page.evaluate.assert_called_once()

    def test_headless_unauthorized_response_recovers_instead_of_stopping(self):
        with tempfile.TemporaryDirectory() as root:
            browser, _ = self.browser(root)
            with patch.object(browser, "refresh") as refresh, \
                    patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                    patch.object(demo_admin, "MONITOR", {}):
                self.assertFalse(demo_admin.handle_poll_error(browser, PulseError("HTTP 401"),
                                                             demo_admin.DEFAULTS["profile_url"]))
                self.assertEqual(demo_admin.AUTH["status"], "waiting")
                refresh.assert_called_once()

    def test_maintenance_runs_when_schedule_closed_and_monitoring_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "pulse-browser").mkdir()
            settings = {**demo_admin.DEFAULTS, "monitoring_enabled": False}
            ticks = iter([0])
            with patch.object(demo_admin, "DATA_DIR", Path(root)), \
                    patch.object(demo_admin, "load_settings", side_effect=[settings, KeyboardInterrupt]), \
                    patch.object(demo_admin, "scheduled_pause", return_value=True), \
                    patch.object(demo_admin, "AUTH", {"status": "authenticated"}), \
                    patch.object(demo_admin, "MONITOR", {}), patch.object(demo_admin, "MONTH", {}), \
                    patch.object(demo_admin, "TODAY", []), \
                    patch.dict(os.environ, {"TINVEST_WORKER_ROLE": "source"}), \
                    patch.object(demo_admin.HISTORY_REQUESTS, "get", side_effect=queue.Empty), \
                    patch.object(demo_admin.time, "monotonic", side_effect=lambda: next(ticks, 700)), \
                    patch.object(PulseBrowser, "open"), \
                    patch.object(PulseBrowser, "maintain_session") as maintenance, \
                    patch.object(demo_admin, "scheduled_poll") as scan:
                with self.assertRaises(KeyboardInterrupt):
                    demo_admin.monitor_loop()
                maintenance.assert_called_once_with(settings["profile_url"])
                scan.assert_not_called()

    def test_source_login_does_not_read_history_or_follow_client_schedule(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "pulse-browser").mkdir()
            settings = {**demo_admin.DEFAULTS, "monitoring_enabled": False}
            with patch.object(demo_admin, "DATA_DIR", Path(root)), \
                    patch.object(demo_admin, "load_settings", side_effect=[settings, KeyboardInterrupt]), \
                    patch.object(demo_admin, "scheduled_pause", return_value=True), \
                    patch.object(demo_admin, "AUTH", {"status": "waiting"}), \
                    patch.object(demo_admin, "MONITOR", {}), patch.object(demo_admin, "MONTH", {}), \
                    patch.object(demo_admin, "TODAY", []), \
                    patch.dict(os.environ, {"TINVEST_WORKER_ROLE": "source"}), \
                    patch.object(demo_admin.HISTORY_REQUESTS, "get", side_effect=queue.Empty), \
                    patch.object(PulseBrowser, "open", lambda b, _: setattr(b, "list_url", "found")), \
                    patch.object(PulseBrowser, "snapshot", return_value=("LinMath", [])) as snapshot, \
                    patch.object(PulseBrowser, "confirm_session"), \
                    patch.object(PulseBrowser, "history") as history, \
                    patch.object(demo_admin, "scheduled_poll") as scan:
                with self.assertRaises(KeyboardInterrupt):
                    demo_admin.monitor_loop()
                self.assertEqual(demo_admin.AUTH["status"], "authenticated")
                snapshot.assert_called_once_with(settings["profile_url"], {})
                history.assert_not_called()
                scan.assert_not_called()

    def test_fetch_timeout_remains_a_retryable_pulse_error(self):
        with tempfile.TemporaryDirectory() as root:
            browser, _ = self.browser(root)
            browser.page.evaluate.return_value = {"ok": False, "status": 0, "timedOut": True}
            with self.assertRaisesRegex(PulseError, "10 секунд"):
                browser._fetch("https://www.tbank-online.com/mybank/api/social-api-gateway/test")


if __name__ == "__main__":
    unittest.main()
