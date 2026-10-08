"""Durable, single-use operator decisions bound to account, rules and recipient."""
import hashlib
import json
import secrets
import threading
import time

from pulse_replay import write_state
from pulse_live import canonical_profile_url, PulseError


def fingerprint(settings):
    keys = ("profile_url", "real_mode", "monitoring_enabled", "auto_demo_buy", "rules", "policy", "schedule")
    return hashlib.sha256(json.dumps({key: settings.get(key) for key in keys}, sort_keys=True).encode()).hexdigest()


class Approvals:
    def __init__(self, root):
        self.path, self.consents_path = root / "trade-approvals.json", root / "asset-consents.json"
        self.lock = threading.RLock()
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.consents = json.loads(self.consents_path.read_text()) if self.consents_path.exists() else {}
        # Domain changes must not discard an operator's author/account/asset consent.
        normalized = {}
        for key, enabled in self.consents.items():
            try:
                parts = json.loads(key)
                if not isinstance(parts, list) or len(parts) != 4:
                    raise ValueError("Invalid consent key")
                parts[0] = canonical_profile_url(parts[0])
                key = json.dumps(parts)
            except (ValueError, TypeError, PulseError):
                pass
            normalized[key] = enabled if key not in normalized else normalized[key] is True and enabled is True
        self.consents = normalized

    def consent_key(self, signal, settings, account):
        return json.dumps([canonical_profile_url(settings["profile_url"]), account, signal["ticker"], signal["classCode"]])

    def trusted(self, signal, settings, account):
        with self.lock:
            return self.consents.get(self.consent_key(signal, settings, account)) is True

    def set_trusted(self, signal, settings, account, enabled):
        with self.lock:
            key = self.consent_key(signal, settings, account)
            if enabled:
                self.consents[key] = True
            else:
                self.consents.pop(key, None)
            write_state(self.consents_path, self.consents)

    def create(self, signal, settings, plan, source_key, token, mode, reasons):
        with self.lock:
            for row in self.rows.values():
                if row["source_key"] == source_key and row["status"] in {"pending", "processing", "done"}:
                    raise ValueError("Для сигнала уже есть подтверждение или результат")
            self.rows = {key: row for key, row in self.rows.items()
                         if row["status"] == "processing" or row["expires"] > time.time() - 86400}
            if len(self.rows) >= 1000:
                raise ValueError("Слишком много ожидающих решений; проверь журнал")
            nonce = secrets.token_hex(16)
            row = {"id": nonce, "signal": signal, "plan": plan, "source_key": source_key,
                   "mode": mode, "reasons": reasons, "status": "pending", "expires": time.time() + 300,
                   "recipient": settings["chat_id"], "fingerprint": fingerprint(settings),
                   "token_hash": hashlib.sha256(token.encode()).hexdigest()}
            self.rows[nonce] = row
            write_state(self.path, self.rows)
            return row.copy()

    def claim(self, nonce, action, settings, token):
        with self.lock:
            row = self.rows.get(nonce)
            if not row:
                return None
            if row["status"] != "pending":
                raise ValueError("Решение уже обработано; повторная заявка не создаётся")
            if (row["expires"] < time.time() or row["fingerprint"] != fingerprint(settings)
                    or row["recipient"] != settings["chat_id"]
                    or row["token_hash"] != hashlib.sha256(token.encode()).hexdigest()):
                self.finish(nonce, "expired")
                raise ValueError("Подтверждение устарело или настройки изменились")
            self.finish(nonce, "rejected" if action == "reject" else "processing")
            return row.copy()

    def finish(self, nonce, status):
        with self.lock:
            self.rows[nonce]["status"] = status
            write_state(self.path, self.rows)

    def public(self):
        with self.lock:
            rows = []
            for row in reversed(list(self.rows.values())):
                if row["status"] in {"done", "rejected", "replaced"} or row["expires"] < time.time() - 86400:
                    continue
                public = {key: row[key] for key in ("id", "signal", "plan", "source_key", "mode", "reasons", "status", "expires")}
                if public["status"] == "pending" and public["expires"] < time.time():
                    public["status"] = "expired"
                rows.append(public)
            return rows[:40]
