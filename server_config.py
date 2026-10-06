"""Explicit public origin; never trust arbitrary forwarded headers."""
import os
from urllib.parse import urlparse


def public_origin() -> str | None:
    value = os.environ.get("TINVEST_PUBLIC_ORIGIN", "").rstrip("/")
    if not value:
        return None
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError("TINVEST_PUBLIC_ORIGIN должен быть HTTPS-адресом без пути, например https://invest.argokov.ru")
    return value


def server_mode() -> bool:
    return bool(public_origin())
