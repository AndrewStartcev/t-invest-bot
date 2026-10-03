"""Small Telegram notification gateway with an explicit recipient guard."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request


class NotificationError(Exception):
    pass


def send_notification(token: str, chat_id: str, message: str, *, opener=urllib.request.urlopen) -> str:
    """Send to one private user. A missing chat ID never reaches the network."""
    recipient = str(chat_id or "").strip()
    if not recipient:
        return "skipped_no_chat_id"
    if not re.fullmatch(r"[1-9][0-9]*", recipient):
        raise ValueError("Telegram user ID must be a positive number")
    if not token:
        return "skipped_no_token"
    payload = json.dumps({"chat_id": int(recipient), "text": message}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=10) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise NotificationError(f"Telegram returned HTTP {error.code}") from None
    except urllib.error.URLError:
        raise NotificationError("Telegram is unreachable") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise NotificationError("Telegram did not confirm delivery")
    return "sent"
