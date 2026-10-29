"""Session-only Pulse authorization guard for the existing paired browser extension.

This checker does not read profiles, instruments, trades, or bank credentials.
It has independent state from the legacy extension trade collector.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from client_accounts import atomic_text
from extension_source import ExtensionStore

STATE_NAME = "pulse-auth-status.json"
STALE_SECONDS = 300
ALLOWED = {"authenticated", "login_required", "unknown", "stopped"}


def now_utc():
    return datetime.now(timezone.utc)


def age_seconds(value, now=None):
    if not value:
        return float("inf")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return float("inf")
        return max(0.0, ((now or now_utc()) - stamp).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


class PulseAuthStore(ExtensionStore):
    """Reuse existing pairing key but NEVER reuse trade counts or events."""

    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.auth_path = Path(data_dir) / STATE_NAME

    def _auth_read(self):
        if not self.auth_path.exists():
            return {"last_status": "never", "last_report": None, "last_ok": None,
                    "incident": None, "failures": 0, "active": False, "last_error": ""}
        data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Неверный формат состояния авторизации")
        return data

    def _auth_save(self, state):
        atomic_text(self.auth_path, json.dumps(state, ensure_ascii=False, separators=(",", ":")))

    def public_status(self):
        with self.lock:
            state = self._auth_read()
        status = state.get("last_status", "never")
        if state.get("active") and age_seconds(state.get("last_report")) > STALE_SECONDS:
            status = "offline"
        return {"last_status": status, "last_report": state.get("last_report"),
                "last_ok": state.get("last_ok"), "last_error": state.get("last_error", ""),
                "active": state.get("active", False), "incident": state.get("incident"),
                "total_events": 0}

    def accept_auth(self, report, alert=None):
        if not isinstance(report, dict) or report.get("kind") != "auth_status":
            raise ValueError("Расширение поддерживает только проверку авторизации")
        status = report.get("status")
        if status not in ALLOWED:
            raise ValueError("Неизвестный статус проверки")
        # No page HTML, cookies or bank headers are allowed to be sent.
        if set(report) - {"kind", "status"}:
            raise ValueError("Не передавай в отчёте личные данные или адреса страниц")
        messages = []
        with self.lock:
            state = self._auth_read()
            previous = state.get("last_status")
            previous_incident = state.get("incident")
            now = now_utc().isoformat()
            if status == "authenticated":
                state["failures"] = 0
                state["last_ok"] = now
                state["last_error"] = ""
                if previous_incident:
                    messages.append(("recovered", "✅ Пульс: авторизация в браузере восстановлена."))
                state["incident"] = None
                state["active"] = True
            elif status == "stopped":
                state["failures"] = 0
                state["incident"] = None
                state["last_error"] = ""
                state["active"] = False
            elif status == "login_required":
                state["failures"] = int(state.get("failures", 0)) + 1 if previous == status else 1
                state["last_error"] = "Банк запрашивает повторный вход"
                state["active"] = True
                if state.get("incident") != "login_required":
                    state["incident"] = "login_required"
                    messages.append(("incident", "⚠️ Пульс: авторизация пропала. Открой Brave и войди в Т-Банк повторно."))
            else:
                # Ambiguous HTML, network failure or tab throttling must not
                # be interpreted as proven logout.
                state["failures"] = 0
                state["last_error"] = "Не удалось подтвердить авторизацию"
                state["active"] = True
            state["last_report"] = now
            state["last_status"] = status
            self._auth_save(state)
        if alert:
            for kind, message in messages:
                alert(kind, message)
        return {"ok": True, "read_only": True, "status": status, "last_report": now}

    def tick(self, alert=None):
        message = None
        with self.lock:
            state = self._auth_read()
            if state.get("active") and age_seconds(state.get("last_report")) >= STALE_SECONDS:
                if state.get("incident") != "offline":
                    state["incident"] = "offline"
                    self._auth_save(state)
                    message = ("⚠️ Пульс: нет подтверждения авторизации более 5 минут. "
                               "Проверь Brave, расширение и вход в Т-Банк.")
        if message and alert:
            alert("incident", message)
        return bool(message)
