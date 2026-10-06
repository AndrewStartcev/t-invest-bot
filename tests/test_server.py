import io
import json
import os
import queue
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
from server_config import public_origin, validate_source_config


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

    def test_shared_source_blocks_client_bank_controls_and_keeps_owner_state_separate(self):
        key = "a" * 64
        env = {"TINVEST_PUBLIC_ORIGIN": "https://invest.argokov.ru",
               "TINVEST_PULSE_SOURCE": "shared", "TINVEST_SOURCE_KEY": key}
        requests = queue.Queue()
        with patch.dict(os.environ, env), \
                patch.object(demo_admin, "AUTH", {"status": "required", "message": "owner-only-detail"}), \
                patch.object(demo_admin, "HISTORY_REQUESTS", requests), \
                patch.object(demo_admin, "load_settings", return_value=demo_admin.DEFAULTS.copy()), \
                patch.object(demo_admin, "connect_broker") as connect, \
                patch.object(demo_admin, "refresh_broker", return_value={"status": "connected"}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def request(path, post=False, secret=None, origin="https://invest.argokov.ru"):
                headers = {"Content-Type": "application/json", "Origin": origin}
                if secret is not None:
                    headers["X-TInvest-Source-Key"] = secret
                return urlopen(Request(base + path, data=b'{"token":"client-api-token"}' if post else None,
                                       headers=headers))
            try:
                for path in ["/api/auth/start", "/api/auth/check", "/api/browser/show"]:
                    # Even a forged role header cannot enable the original public routes.
                    with self.subTest(path=path), self.assertRaises(HTTPError) as failure:
                        request(path, True, key)
                    self.assertEqual(failure.exception.code, 403)
                for path, post in [("/source-admin/", False), ("/source-admin/api/state", False),
                                   ("/source-admin/api/start", True)]:
                    for secret in [None, "wrong"]:
                        with self.subTest(path=path, secret=secret), self.assertRaises(HTTPError) as failure:
                            request(path, post, secret)
                        self.assertEqual(failure.exception.code, 403)
                self.assertTrue(requests.empty())
                with request("/api/state") as response:
                    client_state = json.load(response)
                self.assertTrue(client_state["shared_source"])
                self.assertIsNone(client_state["browser_ui_url"])
                self.assertNotIn("owner-only-detail", json.dumps(client_state))
                with request("/source-admin/api/state", secret=key) as response:
                    owner_state = json.load(response)
                self.assertEqual(set(owner_state), {"auth", "status", "last_check"})
                self.assertEqual(owner_state["auth"]["message"], "owner-only-detail")
                with self.assertRaises(HTTPError) as failure:
                    request("/source-admin/api/start", True, key, "https://evil.example")
                self.assertEqual(failure.exception.code, 403)
                self.assertTrue(requests.empty())
                with request("/source-admin/api/start", True, key) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(requests.get_nowait()["action"], "auth_start")
                with self.assertRaises(HTTPError) as failure:
                    request("/source-admin/api/broker/connect", True, key)
                self.assertEqual(failure.exception.code, 404)
                with request("/api/broker/connect", True) as response:
                    self.assertEqual(response.status, 200)
                connect.assert_called_once_with("client-api-token")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_shared_source_configuration_fails_closed_without_secret_or_https(self):
        for origin, key in [("", "a" * 64), ("https://invest.argokov.ru", ""),
                            ("https://invest.argokov.ru", "short")]:
            with patch.dict(os.environ, {"TINVEST_PULSE_SOURCE": "shared", "TINVEST_PUBLIC_ORIGIN": origin,
                                         "TINVEST_SOURCE_KEY": key}), self.assertRaises(ValueError):
                validate_source_config()

    def test_shared_nginx_overwrites_client_role_and_protects_desktop_with_owner_password(self):
        config = render("invest.argokov.ru", True, True)
        self.assertIn('proxy_set_header X-TInvest-Source-Key "";', config)
        self.assertEqual(config.count("auth_basic_user_file /etc/nginx/t-invest-source.htpasswd;"), 3)
        self.assertIn("include /etc/t-invest-bot/source-proxy.conf;", config)
        self.assertNotIn("source-proxy.conf", render("invest.argokov.ru", True, False))

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
