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
from datetime import datetime, timezone, timedelta
from pathlib import Path

from client_accounts import atomic_text
from pulse_live import canonical_profile_url

VALID_STATUS = {"ok", "restricted", "login_required", "unavailable", "rate_limited",
                "background_throttled", "stopped"}
# Pulse can expose derivatives, FX pairs and synthetic symbols with /, +,
# @, colons, non-Latin letters or identifiers longer than 32 characters.
# Treat ticker as opaque display/API data; never rewrite it into another symbol.
# The class code cannot contain ":" because persisted watermarks use ticker:class.
FORBIDDEN_SYMBOL_CHARS = frozenset('<>"\\\\' + chr(39))
MAX_TICKER_LENGTH = 128
MAX_CLASS_CODE_LENGTH = 64
MAX_INSTRUMENTS = 6000
MAX_EVENTS = 500
STALE_SECONDS = 180


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def _valid_symbol(value, limit, *, allow_colon):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return False
    if not value.isprintable() or value != value.strip():
        return False
    if any(char in FORBIDDEN_SYMBOL_CHARS for char in value):
        return False
    if not allow_colon and ":" in value:
        return False
    return True


def _instrument_key(ticker, class_code):
    if not _valid_symbol(ticker, MAX_TICKER_LENGTH, allow_colon=True):
        raise ValueError("Некорректный тикер")
    if not _valid_symbol(class_code, MAX_CLASS_CODE_LENGTH, allow_colon=False):
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

    def history_progress(self, expected_profile):
        profile = canonical_profile_url(expected_profile)
        with self.lock:
            state = self._read()
        if state.get("profile_url") != profile:
            return {"done": [], "target_total": 0, "complete": False,
                    "loaded": 0, "status": "waiting"}
        done = state.get("history_done", [])
        instruments = state.get("instruments", [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        targets = set()
        for instrument in instruments:
            try:
                if int(instrument.get("totalOperationsCount", 0)) <= 0:
                    continue
                when = instrument.get("maxTradeDateTime")
                if when:
                    stamp = datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if stamp.tzinfo is not None and stamp < cutoff:
                        continue
                targets.add(_instrument_key(instrument["ticker"], instrument["classCode"]))
            except (TypeError, ValueError, KeyError):
                # An unrecognized timestamp must not suppress a potential trade.
                if isinstance(instrument, dict) and "ticker" in instrument and "classCode" in instrument:
                    targets.add(_instrument_key(instrument["ticker"], instrument["classCode"]))
        completed = len(targets.intersection(done))
        return {"done": done, "target_total": len(targets), "completed": completed,
                "complete": len(targets) == completed, "loaded": len(state.get("history_entries", [])),
                "status": "ready" if len(targets) == completed else "loading"}

    def accept_history(self, report, expected_profile):
        """Save a read-only 24h history batch; never advance trade watermarks.

        A repeated batch is idempotent and historic trades NEVER trigger broker
        signals, demo orders or Telegram notifications.
        """
        if not isinstance(report, dict) or report.get("kind") != "history":
            raise ValueError("Некорректный отчёт истории")
        if canonical_profile_url(report.get("profile_url")) != canonical_profile_url(expected_profile):
            raise ValueError("История принадлежит другому автору")
        ticker, code = report.get("ticker"), report.get("classCode")
        key = _instrument_key(ticker, code)
        raw = report.get("events")
        if not isinstance(raw, list) or len(raw) > 500:
            raise ValueError("Один пакет истории не может содержать более 500 операций")
        parsed = [_trade(row) for row in raw]
        if any(row["ticker"] != ticker or row["classCode"] != code for row in parsed):
            raise ValueError("История содержит операции другого инструмента")
        now = datetime.now(timezone.utc)
        # The browser scans a rolling 24h window. Allow one hour of clock
        # drift/retry latency in storage; the dashboard still shows only 24h.
        cutoff = now - timedelta(hours=25)
        for row in parsed:
            stamp = datetime.fromisoformat(row["tradeDateTime"].replace("Z", "+00:00"))
            if not cutoff <= stamp <= now + timedelta(minutes=5):
                raise ValueError("Историческая операция вне последних 24 часов")
        with self.lock:
            state = self._read()
            profile = canonical_profile_url(expected_profile)
            if state.get("profile_url") != profile or state.get("last_status") != "ok":
                raise ValueError("Сначала нужно получить полный снимок выбранного автора")
            instrument_keys = {
                _instrument_key(item["ticker"], item["classCode"])
                for item in state.get("instruments", [])
            }
            if key not in instrument_keys:
                raise ValueError("Инструмент отсутствует в подтверждённом снимке автора")
            done = state.setdefault("history_done", [])
            if key in done:
                return {"ok": True, "read_only": True, "duplicate": True, "accepted": 0}
            old = state.get("history_entries", [])
            filtered = [row for row in old if row["ticker"] != ticker or row["classCode"] != code]
            # Preserve multiplicity for indistinguishable same-second trades.
            filtered.extend(parsed)
            filtered.sort(key=lambda row: row["tradeDateTime"], reverse=True)
            state["history_entries"] = filtered[:5000]
            state["history_truncated"] = bool(state.get("history_truncated")) or len(filtered) > 5000
            done.append(key)
            self._save(state)
        return {"ok": True, "read_only": True, "duplicate": False, "accepted": len(parsed)}

    def public_snapshot(self, expected_profile):
        profile = canonical_profile_url(expected_profile)
        with self.lock:
            current = self._read()
        if current.get("profile_url") != profile:
            return {"last_status": "never", "instruments": [], "recent": [],
                    "history_entries": [], "history": {"status":"waiting","target_total":0,
                    "completed":0,"complete":False,"loaded":0},
                    "total_events": 0, "active": False}
        return {**self.public_status(), "instruments": current.get("instruments", []),
                "recent": current.get("recent", []),
                "history_entries": current.get("history_entries", []),
                "history": self.history_progress(expected_profile),
                "history_truncated": current.get("history_truncated", False)}

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
                    "counts": counts, "history_done": current.get("history_done", []) if same else [],
                    "reset_baseline": not same or not counts}
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
            for index, item in enumerate(instruments, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"Некорректный инструмент №{index}")
                try:
                    key = _instrument_key(item.get("ticker"), item.get("classCode"))
                except ValueError as exc:
                    # Symbols are public identifiers, but escape control chars
                    # and cap diagnostic length before returning to the UI.
                    sample = ascii(item.get("ticker"))[:80]
                    raise ValueError(f"{exc} у инструмента №{index}: {sample}") from exc
                count = item.get("totalOperationsCount")
                if key in counts or type(count) is not int or not 0 <= count <= 1000000000:
                    raise ValueError("Некорректные счётчики инструментов")
                counts[key] = count
            details = [{"ticker": item["ticker"], "classCode": item["classCode"],
                        "showName": str(item.get("showName") or item["ticker"])[:160],
                        "type": str(item.get("type") or "")[:32],
                        "totalOperationsCount": item["totalOperationsCount"],
                        "maxTradeDateTime": str(item.get("maxTradeDateTime") or "")[:40]}
                       for item in instruments]
            normalized = [_trade(event) for event in events]
        messages = []
        with self.lock:
            data = self._read()
            before = data.get("counts", {})
            if data.get("profile_url") not in (None, profile_url):
                # A new monitored author must start at baseline, never replay old trades.
                before = {}
                data["counts"] = {}
                data["instruments"] = []
                data["history_done"] = []
                data["history_entries"] = []
                data["history_truncated"] = False
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
                data["instruments"] = details
                data["last_ok"] = iso_now()
                data["failures"] = 0
                data["last_error"] = ""
                if data.get("incident"):
                    messages.append(("recovered", "✅ Пульс: доступ к профилю восстановлен."))
                data["incident"] = None
                data["total_events"] = int(data.get("total_events", 0)) + len(fresh)
                data["recent"] = (fresh + data.get("recent", []))[:500]
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
                    message = "⚠️ Пульс: расширение не отправляет данные более 3 минут. Проверь браузер и вкладку."
        if message and alert:
            alert("incident", message)
        return bool(message)
