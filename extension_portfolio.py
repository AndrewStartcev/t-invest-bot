"""Read-only, per-client published Pulse portfolio cache supplied by browser.

Never store bank cookies, raw HTML, session URLs, or client-provided positions.
Only the existing strict visible-row parser may derive verified allocations.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from client_accounts import atomic_text
from investor_portfolio import parse_screen, unavailable, compare_positions
from pulse_live import canonical_profile_url

# Browser refreshes the visible portfolio about once per minute. Three
# minutes gives one delayed delivery grace period, never unlimited validity.
PORTFOLIO_MAX_AGE_SECONDS = 180

FAILURE_REASONS = {
    "not_visible": "Раздел «Портфель → Компании» не открыт или ещё загружается",
    "unrecognized": "Доли портфеля не распознаны; данные не подтверждены",
    "wrong_author": "Открыт портфель другого автора",
    "bank_unavailable": "Портфель недоступен в текущей вкладке банка",
}


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

    def public_snapshot(self, profile_url, now=None):
        """Never expose cached positions as verified after expiry or a read failure."""
        expected = canonical_profile_url(profile_url)
        with self.lock:
            saved = self._read()
        if saved.get("profile_url") != expected:
            return {**unavailable(expected, "Открой портфель выбранного автора: раздел «Компании»"),
                    "fresh": False, "last_success_at": None}
        checked = saved.get("checked_at")
        age = None
        try:
            stamp = datetime.fromisoformat(checked)
            reference = now or datetime.now(timezone.utc)
            if stamp.tzinfo is not None:
                age = (reference - stamp).total_seconds()
        except (TypeError, ValueError):
            pass
        if saved.get("availability") == "error":
            result = unavailable(expected, saved.get("last_error") or "Не удалось прочитать портфель")
            result.update(status="error", fresh=False, last_success_at=checked)
            return result
        if (saved.get("status") != "partial" or age is None or age < 0
                or age > PORTFOLIO_MAX_AGE_SECONDS):
            result = unavailable(expected, "Портфель устарел. Обнови раздел «Компании» в браузере")
            result.update(status="stale" if saved.get("status") == "partial" else "unavailable",
                          fresh=False, last_success_at=checked, age_seconds=age)
            return result
        return {**saved, "fresh": True, "last_success_at": checked, "age_seconds": int(age)}

    def mark_unavailable(self, report, selected_profile):
        if not isinstance(report, dict) or report.get("kind") != "portfolio_status":
            raise ValueError("Некорректный статус портфеля")
        expected = canonical_profile_url(selected_profile)
        if canonical_profile_url(report.get("profile_url")) != expected:
            raise ValueError("Статус портфеля другого автора")
        reason = report.get("reason")
        if reason not in FAILURE_REASONS:
            raise ValueError("Неизвестная причина недоступности портфеля")
        with self.lock:
            current = self._read()
            if current.get("profile_url") != expected:
                current = {"profile_url": expected}
            if current.get("availability") != "error" or current.get("failure_reason") != reason:
                current.update(availability="error", failure_reason=reason,
                               last_error=FAILURE_REASONS[reason],
                               failure_at=datetime.now(timezone.utc).isoformat())
                atomic_text(self.path, json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        return {"ok": True, "read_only": True, "status": "error", "reason": reason}

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
            parsed["availability"] = "ok"
            parsed.pop("last_error", None)
            parsed.pop("failure_reason", None)
            parsed.pop("failure_at", None)
            atomic_text(self.path, json.dumps(parsed, ensure_ascii=False, separators=(",", ":")))
        return {"ok": True, "read_only": True,
                "status": parsed["status"], "positions": len(parsed["positions"]),
                "rows": len(parsed["rows"]), "checked_at": parsed["checked_at"]}
