import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import demo_admin
from broker_http import APIError, normalize_token, request_json
from pulse_live import PulseBrowser, PulseError, browser_executable
from scripts.render_nginx import render
from server_config import public_origin


class ServerTests(unittest.TestCase):
    def test_token_copy_artifacts_are_removed_without_altering_token_body(self):
        self.assertEqual(normalize_token(' \ufeff"Bearer private-token"\u200b '), "private-token")
        for token in ["private token", "private\n-token", "токен", "", "abc\x00def"]:
            with self.subTest(token=repr(token)), self.assertRaises(ValueError):
                normalize_token(token)

    def test_api_error_exposes_numeric_code_but_never_reflected_secrets(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 400, "Bad Request", {},
                            io.BytesIO(b'{"code":"40003","message":"private-token cookie phone"}'))
        with self.assertRaisesRegex(APIError, "40003") as failure:
            request_json("UsersService/GetAccounts", "private-token", {}, opener)
        self.assertNotIn("private-token", str(failure.exception))
        self.assertNotIn("cookie", str(failure.exception))
        self.assertEqual(len(calls), 1)

    def test_dns_tls_and_timeouts_have_distinct_diagnostics_and_no_retries(self):
        for error, message in [(socket.gaierror("dns"), "DNS"),
                               (ssl.SSLCertVerificationError("cert"), "TLS"),
                               (TimeoutError(), "12 секунд")]:
            with self.subTest(error=error):
                calls = []
                def opener(request, timeout):
                    calls.append(request)
                    raise URLError(error)
                with self.assertRaisesRegex(APIError, message):
                    request_json("OrdersService/PostOrder", "private-token", {}, opener)
                self.assertEqual(len(calls), 1)

    def test_subdomain_post_allowed_but_other_origins_and_forwarded_spoof_rejected(self):
        with patch.dict(os.environ, {"TINVEST_PUBLIC_ORIGIN": "https://invest.argokov.ru"}), \
                patch.object(demo_admin, "connect_broker") as connect, \
                patch.object(demo_admin, "refresh_broker", return_value={"status": "connected"}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}/api/broker/connect"
                request = Request(endpoint, data=b'{"token":"private-token"}',
                                  headers={"Content-Type": "application/json", "Origin": "https://invest.argokov.ru"})
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                connect.assert_called_once_with("private-token")
                for origin in ["https://evil.example", "http://invest.argokov.ru", "https://invest.argokov.ru.evil.example"]:
                    request = Request(endpoint, data=b'{"token":"private-token"}', headers={
                        "Content-Type": "application/json", "Origin": origin,
                        "X-Forwarded-Host": "invest.argokov.ru", "X-Forwarded-Proto": "https"})
                    with self.assertRaises(HTTPError) as failure:
                        urlopen(request)
                    self.assertEqual(failure.exception.code, 403)
                self.assertEqual(connect.call_count, 1)
                with urlopen(endpoint.replace("/api/broker/connect", "/api/health")) as response:
                    self.assertEqual(json.load(response), {"status": "ok", "server_mode": True})
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_public_origin_configuration_rejects_non_https_and_paths(self):
        for origin in ["http://invest.argokov.ru", "https://user:password@invest.argokov.ru", "https://invest.argokov.ru/path", "https://invest.argokov.ru?x=1"]:
            with patch.dict(os.environ, {"TINVEST_PUBLIC_ORIGIN": origin}), self.assertRaises(ValueError):
                public_origin()

    def test_runtime_data_directory_is_independent_of_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "TINVEST_DATA_DIR": folder}
            output = subprocess.check_output([sys.executable, "-c", "import demo_admin; print(demo_admin.TRADE_TOKEN_PATH)"],
                                             env=env, text=True)
            self.assertEqual(output.strip(), str(Path(folder) / "broker-trade-token.txt"))

    def test_nginx_never_exposes_application_over_plain_http(self):
        initial = render("invest.argokov.ru")
        self.assertIn("return 503", initial)
        self.assertNotIn("proxy_pass", initial)
        secure = render("invest.argokov.ru", True)
        self.assertIn("auth_basic_user_file /etc/nginx/t-invest-bot.htpasswd", secure)
        self.assertIn("location /desktop/", secure)
        self.assertIn('proxy_set_header Connection "upgrade"', secure)
        self.assertIn("return 301 https://invest.argokov.ru$request_uri", secure)
        with self.assertRaises(ValueError):
            render("invest.argokov.ru; injected")

    @unittest.skipIf(os.name == "nt", "Linux browser selection")
    def test_server_prefers_provisioned_browser_over_system_snap_wrapper(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/opt/t-invest-bot/.playwright", "PULSE_BROWSER_PATH": ""}), \
                patch("pulse_live.shutil.which", return_value="/bin/true"):
            self.assertIsNone(browser_executable())

    @unittest.skipIf(os.name == "nt", "Linux browser selection")
    def test_linux_uses_bundled_chromium_with_persistent_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "chrome"
            executable.touch()
            launched = []
            page = SimpleNamespace(goto=lambda *a, **kw: None)
            context = SimpleNamespace(pages=[page], on=lambda *a: None, close=lambda: None)
            def launch(profile, **kwargs):
                launched.append((profile, kwargs))
                return context
            playwright = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(executable),
                                         launch_persistent_context=launch), stop=lambda: None)
            package = ModuleType("playwright")
            module = ModuleType("playwright.sync_api")
            module.sync_playwright = lambda: SimpleNamespace(start=lambda: playwright)
            with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": module}), \
                    patch.dict(os.environ, {"PULSE_BROWSER_PATH": ""}), \
                    patch("pulse_live.browser_executable", return_value=None):
                profile = Path(folder) / "profile"
                browser = PulseBrowser(profile, headless=True)
                browser.open(demo_admin.DEFAULTS["profile_url"])
                self.assertEqual(launched[0][0], str(profile))
                self.assertEqual(launched[0][1]["executable_path"], str(executable))
                self.assertNotIn("ignore_https_errors", launched[0][1])
                browser.close()


if __name__ == "__main__":
    unittest.main()
