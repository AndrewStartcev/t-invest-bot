"""Read-only, per-client published Pulse portfolio cache supplied by browser.

Never store bank cookies, raw HTML, session URLs, or client-provided positions.
Only the existing strict visible-row parser may derive verified allocations.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from datetime import datetime, timezone
from client_accounts import atomic_text
from investor_portfolio import parse_screen, unavailable, compare_positions
from pulse_live import canonical_profile_url


class ExtensionPortfolioStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "pulse-extension-portfolio.json"
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Повреждён снимок портфеля")
        return value

    def public_snapshot(self, profile_url):
        expected = canonical_profile_url(profile_url)
        with self.lock:
            saved = self._read()
        if saved.get("profile_url") != expected or saved.get("status") != "partial":
            return unavailable(expected, "Открой портфель выбранного автора в браузере и вкладку «Компании»")
        return saved

    def accept(self, report, selected_profile, instruments):
        if not isinstance(report, dict) or report.get("kind") != "portfolio":
            raise ValueError("Неверный отчёт портфеля")
        expected = canonical_profile_url(selected_profile)
        if canonical_profile_url(report.get("profile_url")) != expected:
            raise ValueError("Портфель относится к другому автору")
        screen = report.get("screen")
        if not isinstance(screen, dict) or set(screen) - {"url", "heading", "author", "portfolio", "rows"}:
            raise ValueError("Передан неподдерживаемый формат портфеля")
        rows = screen.get("rows")
        if not isinstance(rows, list) or not 0 < len(rows) <= 3000:
            raise ValueError("Не найдены видимые строки портфеля")
        clean = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {"text", "links"}:
                raise ValueError("Неверная строка портфеля")
            label = row.get("text")
            links = row.get("links", [])
            if not isinstance(label, str) or len(label) > 240 or not isinstance(links, list) or len(links) > 30:
                raise ValueError("Слишком длинная строка портфеля")
            if not all(isinstance(link, str) and len(link) <= 300 for link in links):
                raise ValueError("Некорректная ссылка в портфеле")
            clean.append({"text": label, "links": links})
        sanitized = {**screen, "rows": clean}
        parsed = parse_screen(sanitized, expected, instruments)
        if parsed["status"] != "partial":
            raise ValueError("Не удалось подтвердить портфель выбранного автора; открой «Компании»")
        with self.lock:
            previous = self._read()
            compare_positions(parsed, previous)
            atomic_text(self.path, json.dumps(parsed, ensure_ascii=False, separators=(",", ":")))
        return {"ok": True, "read_only": True,
                "status": parsed["status"], "positions": len(parsed["positions"]),
                "rows": len(parsed["rows"]), "checked_at": parsed["checked_at"]}
