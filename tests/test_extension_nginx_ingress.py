"""Nginx extension ingress regression tests: no global chmod or whole-vhost rewrite."""
import unittest

from scripts.patch_extension_ingress import patch_extension_location
from scripts.render_nginx import render


class ExtensionIngressTests(unittest.TestCase):
    def setUp(self):
        self.config = render("invest.argokov.ru", https=True, multi_user=True, shared_source=True)

    def test_streaming_is_present_in_new_install(self):
        self.assertIn("location = /api/extension/report {", self.config)
        block = self.config.split("location = /api/extension/report {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("client_max_body_size 2m;", block)
        self.assertIn("proxy_request_buffering off;", block)
        self.assertIn("proxy_http_version 1.1;", block)
        self.assertIn("auth_basic off;", block)
        self.assertIn("include /etc/t-invest-bot/client-proxy.conf;", block)

    def test_existing_route_is_patched_only_and_idempotent(self):
        outdated = self.config.replace("proxy_request_buffering off;", "").replace(
            "proxy_http_version 1.1;", "proxy_http_version 1.0;", 1
        ).replace("client_max_body_size 2m;", "client_max_body_size 128k;", 1)
        outdated += "\n# unrelated local override kept intact\n"
        updated = patch_extension_location(outdated)
        self.assertIn("proxy_request_buffering off;", updated)
        self.assertIn("proxy_http_version 1.1;", updated)
        self.assertIn("client_max_body_size 2m;", updated)
        self.assertIn("# unrelated local override kept intact", updated)
        self.assertIn("auth_basic_user_file /var/lib/t-invest-auth/clients.htpasswd;", updated)
        self.assertIn("include /etc/t-invest-bot/source-proxy.conf;", updated)
        self.assertEqual(updated, patch_extension_location(updated))
        # Prefix and suffix beyond the exact extension location remain byte-for-byte.
        before = outdated.split("location = /api/extension/report {", 1)[0]
        after = outdated.split("\n    location = /desktop/websockify {", 1)[1]
        self.assertTrue(updated.startswith(before))
        self.assertTrue(updated.endswith("\n    location = /desktop/websockify {" + after))

    def test_existing_plain_vhost_keeps_other_routes_unchanged(self):
        text = self.config.replace("proxy_request_buffering off;", "")
        updated = patch_extension_location(text)
        self.assertEqual(text.split("location = /api/extension/report {")[0],
                         updated.split("location = /api/extension/report {")[0])

    def test_missing_or_duplicate_ingress_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            patch_extension_location("server {\n listen 443 ssl;\n}\n")
        two = self.config + "\n" + self.config
        with self.assertRaisesRegex(ValueError, "exactly one"):
            patch_extension_location(two)


if __name__ == "__main__":
    unittest.main()
