"""Patch only the existing Nginx Pulse extension ingress location.

Preserves all other vhosts, custom includes, TLS/auth settings and comments.
Output goes to stdout for the caller to install transactionally after nginx -t.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


LOCATION = re.compile(
    r"(?m)^(?P<indent>[ \t]*)location[ \t]*=[ \t]*/api/extension/report[ \t]*\{[ \t]*$"
)


def patch_extension_location(text: str) -> str:
    matches = list(LOCATION.finditer(text))
    if len(matches) != 1:
        raise ValueError("Expected exactly one Nginx extension report location")
    match = matches[0]
    indentation = match.group("indent")
    close_regex = re.compile(r"(?m)^" + re.escape(indentation) + r"\}[ \t]*$")
    end = close_regex.search(text, match.end())
    if end is None:
        raise ValueError("Unterminated Nginx extension report location")
    body = text[match.end():end.start()]
    # Nested location blocks must not be silently rewritten.
    if "{" in body or "}" in body:
        raise ValueError("Unexpected nested block in extension report location")
    required = (
        ("client_max_body_size", "2m"),
        ("proxy_request_buffering", "off"),
        ("proxy_http_version", "1.1"),
    )
    for key, value in required:
        regex = re.compile(
            r"(?m)^([ \t]*)" + key + r"[ \t]+[^;\n]+;[ \t]*$"
        )
        found = list(regex.finditer(body))
        if len(found) > 1:
            raise ValueError("Duplicate " + key + " in extension report location")
        if found:
            body = regex.sub(lambda x: x.group(1) + key + " " + value + ";", body, count=1)
        else:
            body = "\n" + indentation + "    " + key + " " + value + ";" + body
    return text[:match.end()] + body + text[end.start():]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: patch_extension_ingress.py /path/to/nginx-vhost.conf")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        result = patch_extension_location(source)
    except ValueError as exc:
        raise SystemExit("Nginx extension ingress patch refused: " + str(exc)) from exc
    sys.stdout.write(result)


if __name__ == "__main__":
    main()
