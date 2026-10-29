"""Read-only bridge for browser extension snapshots. Never submits broker orders.

The bank session stays inside the user's browser. Only allowlisted, normalized
Pulse counters and trades reach this module. Each customer has its own pairing
key and persisted watermarks. This is an observational channel: its events are
not inputs to the trading engine until separately audited.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from client_accounts import atomic_text
from pulse_live import canonical_profile_url

VALID_STATUS = {"ok", "restricted", "login_required", "unavailable", "rate_limited",
                "background_throttled", "stopped"}
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
MAX_INSTRUMENTS = 6000
MAX_EVENTS = 500
STALE_SECONDS = 30


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def _instrument_key(ticker, class_code):
    if not isinstance(ticker, str) or not KEY_RE.fullmatch(ticker):
        raise ValueError("Некорректный тикер")
    if not isinstance(class_code, str) or not KEY_RE.fullmatch(class_code):
        raise ValueError("Некорректный classCode")
    return ticker + ":" + class_code


def _trade(value):
    if not isinstance(value, dict):
        raise ValueError("Некорректная операция")
    ticker, class_code = value.get("ticker"), value.get("classCode")
    _instrument_key(ticker, class_code)
    if value.get("action") not in {"buy", "sell"}:
        raise ValueError("Неизвестное направление сделки")
    date = value.get("tradeDateTime")
    if not isinstance(date, str) or not 10 <= len(date) <= 40:
        raise ValueError("Некорректное время сделки")
    try:
        if datetime.fromisoformat(date).tzinfo is None:
            raise ValueError("Нужен часовой пояс сделки")
    except ValueError as error:
        raise ValueError("Некорректная дата сделки") from error
    price = str(value.get("averagePrice", ""))
    if not 1 <= len(price) <= 64 or not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", price):
        raise ValueError("Некорректная цена")
    return {"ticker": ticker, "classCode": class_code, "action": value["action"],
            "tradeDateTime": date, "averagePrice": price}


class ExtensionStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.key_path = self.root / "extension-pairing-key.txt"
        self.path = self.root / "extension-pulse-state.json"
        self.lock = threading.RLock()

    def pairing_key(self):
        with self.lock:
            if not self.key_path.exists():
                atomic_text(self.key_path, secrets.token_urlsafe(36) + "\n")
            return self.key_path.read_text(encoding="utf-8").strip()

    def authorized(self, provided):
        # Invalid inputs never create a pairing credential.
        if not isinstance(provided, str) or len(provided) < 32 or not self.key_path.exists():
            return False
        expected = self.key_path.read_text(encoding="utf-8").strip()
        return len(expected) >= 32 and hmac.compare_digest(provided, expected)

    def _read(self):
        if not self.path.exists():
            return {"counts": {}, "last_status": "never", "total_events": 0,
                    "failures": 0, "incident": None, "active": False, "recent": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("counts"), dict):
            raise ValueError("Повреждено состояние расширения")
        return data

    def _save(self, data):
        atomic_text(self.path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def public_status(self):
        with self.lock:
            data = self._read()
        result = {key: data.get(key) for key in ("last_status", "last_report",
                  "last_ok", "last_error", "total_events", "incident", "active", "recent")}
        seen = data.get("last_report")
        if seen and data.get("active"):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(seen)).total_seconds()
                if age > STALE_SECONDS:
                    result["last_status"] = "offline"
            except (TypeError, ValueError):
                result["last_status"] = "unavailable"
        return result

    def accept(self, report, expected_profile, emit=None, alert=None):
        """Validate complete deltas, atomically commit watermarks, then notify.

        Missing history is an error, not a reason to advance counters.
        """
        if not isinstance(report, dict):
            raise ValueError("Нужен объект JSON")
        status = report.get("status")
        if status == "test":
            # Credentials were validated by the HTTP handler. The server's
            # persisted counters are the only baseline for an authenticated
            # extension, including after profile changes and browser restarts.
            profile = canonical_profile_url(expected_profile)
            with self.lock:
                current = self._read()
                same = current.get("profile_url") == profile
                counts = current.get("counts", {}) if same else {}
            return {"ok": True, "read_only": True, "profile_url": profile,
                    "counts": counts, "reset_baseline": not same or not counts}
        if status not in VALID_STATUS:
            raise ValueError("Неизвестный статус")
        profile_url = canonical_profile_url(report.get("profile_url"))
        if profile_url != canonical_profile_url(expected_profile):
            raise ValueError("Профиль расширения не совпадает с выбранным автором в кабинете")
        error = str(report.get("error") or "")[:180]
        normalized = []
        fresh = []
        counts = {}
        if status == "ok":
            instruments = report.get("instruments")
            events = report.get("events", [])
            if (not isinstance(instruments, list) or len(instruments) > MAX_INSTRUMENTS
                    or not isinstance(events, list) or len(events) > MAX_EVENTS):
                raise ValueError("Снимок слишком большой или неполный")
            for item in instruments:
                if not isinstance(item, dict):
                    raise ValueError("Некорректный инструмент")
                key = _instrument_key(item.get("ticker"), item.get("classCode"))
                count = item.get("totalOperationsCount")
                if key in counts or type(count) is not int or not 0 <= count <= 1000000000:
                    raise ValueError("Некорректные счётчики инструментов")
                counts[key] = count
            normalized = [_trade(event) for event in events]
        messages = []
        with self.lock:
            data = self._read()
            before = data.get("counts", {})
            if data.get("profile_url") not in (None, profile_url):
                # A new monitored author must start at baseline, never replay old trades.
                before = {}
                data["counts"] = {}
                data["recent"] = []
            if status == "ok":
                if not counts and before:
                    raise ValueError("Пустой список инструментов при ранее сохранённой истории")
                pending = {}
                for key, value in counts.items():
                    previous = before.get(key)
                    if previous is not None and (type(previous) is not int or value < previous):
                        raise ValueError("Счётчик сделок уменьшился; нужна ручная сверка")
                    if previous is not None and value > previous:
                        pending[key] = value - previous
                # Replayed HTTP request after a lost ACK: accept only if all
                # previously committed events match persisted recent history.
                # No second journal entry / Telegram notification is emitted.
                if normalized and not pending and all(event in data.get("recent", []) for event in normalized):
                    normalized = []
                received = {}
                for event in normalized:
                    key = _instrument_key(event["ticker"], event["classCode"])
                    if key not in pending:
                        raise ValueError("Расширение прислало неподтверждённую операцию")
                    received.setdefault(key, []).append(event)
                if any(len(received.get(key, [])) != delta for key, delta in pending.items()):
                    raise ValueError("Неполная история: счётчики не обновлены")
                fresh = normalized
                data["counts"] = {**before, **counts}
                data["last_ok"] = iso_now()
                data["failures"] = 0
                data["last_error"] = ""
                if data.get("incident"):
                    messages.append(("recovered", "✅ Пульс: доступ к профилю восстановлен."))
                data["incident"] = None
                data["total_events"] = int(data.get("total_events", 0)) + len(fresh)
                data["recent"] = (fresh + data.get("recent", []))[:50]
            elif status == "stopped":
                data["active"] = False
                data["failures"] = 0
                data["incident"] = None
                data["last_error"] = ""
            else:
                if status == data.get("last_status"):
                    data["failures"] = int(data.get("failures", 0)) + 1
                else:
                    data["failures"] = 1
                data["last_error"] = error
                required = 2 if status in {"restricted", "login_required"} else 3
                if data["failures"] >= required and data.get("incident") != status:
                    data["incident"] = status
                    description = {
                        "restricted": "Профиль закрыт или доступ ограничен",
                        "login_required": "Требуется повторный вход в банк в браузере",
                        "unavailable": "Список сделок недоступен",
                        "rate_limited": "Банк ограничил частоту запросов",
                        "background_throttled": "Браузер приостановил быстрые проверки",
                    }[status]
                    messages.append(("incident", "⚠️ Пульс: " + description + ". " + error))
            data["profile_url"] = profile_url
            data["last_status"] = status
            data["last_report"] = iso_now()
            if status != "stopped":
                data["active"] = True
            self._save(data)
        # Do not call bank or brokerage APIs; callbacks only record and notify.
        if emit:
            for event in fresh:
                emit(event)
        if alert:
            for kind, message in messages:
                alert(kind, message)
        return {"ok": True, "read_only": True, "accepted": len(fresh),
                "last_report": data["last_report"]}

    def tick(self, alert=None):
        """Detect a disappeared tab/extension even when no new reports arrive."""
        message = None
        with self.lock:
            data = self._read()
            stamp = data.get("last_report")
            if stamp and data.get("active") and data.get("incident") != "offline":
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
                except (TypeError, ValueError):
                    age = 0
                if age >= STALE_SECONDS:
                    data["incident"] = "offline"
                    self._save(data)
                    message = "⚠️ Пульс: расширение не отправляет данные более 30 секунд. Проверь браузер и вкладку."
        if message and alert:
            alert("incident", message)
        return bool(message)
