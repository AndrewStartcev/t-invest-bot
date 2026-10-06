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


def shared_source() -> bool:
    return os.environ.get("TINVEST_PULSE_SOURCE", "personal") == "shared"


def source_key() -> str:
    return os.environ.get("TINVEST_SOURCE_KEY", "")


def validate_source_config() -> None:
    mode = os.environ.get("TINVEST_PULSE_SOURCE", "personal")
    if mode not in {"personal", "shared"}:
        raise ValueError("TINVEST_PULSE_SOURCE: personal или shared")
    if shared_source() and (not server_mode() or len(source_key()) < 32):
        raise ValueError("Общий источник требует HTTPS origin и отдельный TINVEST_SOURCE_KEY")
