import io
import json
import unittest

from telegram_notify import send_notification


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class TelegramNotificationTests(unittest.TestCase):
    def test_missing_id_never_calls_network_even_with_token(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("network call with missing ID")

        self.assertEqual(send_notification("fake-token", "", "demo", opener=forbidden), "skipped_no_chat_id")

    def test_missing_token_does_not_call_network(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("network call with missing token")

        self.assertEqual(send_notification("", "12345", "demo", opener=forbidden), "skipped_no_token")

    def test_only_selected_user_receives_message(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse(b'{"ok":true,"result":{}}')

        self.assertEqual(send_notification("fake-token", "12345", "DEMO", opener=opener), "sent")
        self.assertEqual(captured["body"], {"chat_id": 12345, "text": "DEMO"})
        self.assertEqual(captured["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
