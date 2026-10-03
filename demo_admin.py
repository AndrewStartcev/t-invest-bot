"""Local Pulse monitor with simulated purchases and no brokerage orders."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from telegram_notify import NotificationError, send_notification
from pulse_live import PulseBrowser, PulseError, operations_url
from pulse_replay import write_state


ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / ".local" / "demo-settings.json"
STATE_PATH = ROOT / ".local" / "pulse-live-state.json"
EVENTS_PATH = ROOT / ".local" / "events.json"
TOKEN_PATH = ROOT / ".local" / "telegram-token.txt"
DEFAULTS = {
    "profile_url": "https://www.tbank.ru/invest/social/profile/LinMath/",
    "poll_seconds": 30,
    "chat_id": "",
    "paused": False,
    "monitoring_enabled": False,
    "auto_demo_buy": False,
}
EVENTS: list[dict] = json.loads(EVENTS_PATH.read_text(encoding="utf-8")) if EVENTS_PATH.exists() else []
LOCK = threading.RLock()
HISTORY_REQUESTS = queue.Queue()
MONITOR = {"status": "stopped", "message": "Мониторинг выключен", "last_check": None, "instrument_count": 0, "instruments": []}
AUTH = {"status": "checking", "message": "Проверяем сохранённый вход в Пульс"}
TODAY: list[dict] = []


def telegram_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or (TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else "")


def save_telegram_token(value: str) -> None:
    token = value.strip()
    if not token:
        return
    if len(token) > 300 or ":" not in token or any(char.isspace() for char in token):
        raise ValueError("Некорректный токен Telegram")
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = TOKEN_PATH.with_suffix(".tmp")
    temp.write_text(token, encoding="utf-8")
    os.replace(temp, TOKEN_PATH)


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return DEFAULTS.copy()
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return {**DEFAULTS, **{key: data[key] for key in DEFAULTS if key in data}}


def save_settings(data: dict) -> dict:
    url = str(data.get("profile_url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"tbank.ru", "www.tbank.ru"} or not parsed.path.startswith("/invest/"):
        raise ValueError("Укажи ссылку на профиль Пульса на tbank.ru")
    seconds = data.get("poll_seconds")
    if type(seconds) is not int or not 30 <= seconds <= 3600:
        raise ValueError("Интервал должен быть от 30 до 3600 секунд")
    chat_id = str(data.get("chat_id", "")).strip()
    if chat_id and (not chat_id.isascii() or not chat_id.isdecimal() or chat_id.startswith("0")):
        raise ValueError("Telegram ID должен быть положительным числом")
    paused = data.get("paused")
    if type(paused) is not bool:
        raise ValueError("Некорректное значение паузы")
    operations_url(url)
    enabled = data.get("monitoring_enabled")
    auto_buy = data.get("auto_demo_buy")
    if type(enabled) is not bool or type(auto_buy) is not bool:
        raise ValueError("Некорректное значение режима")
    if "telegram_token" in data:
        if not isinstance(data["telegram_token"], str):
            raise ValueError("Некорректный токен Telegram")
        save_telegram_token(data["telegram_token"])
    settings = {"profile_url": url, "poll_seconds": seconds, "chat_id": chat_id, "paused": paused,
                "monitoring_enabled": enabled, "auto_demo_buy": auto_buy}
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, SETTINGS_PATH)
    return settings


def add_event(event: dict) -> None:
    with LOCK:
        EVENTS.insert(0, event)
        del EVENTS[200:]
        write_state(EVENTS_PATH, EVENTS)


def notify(event: dict, settings: dict, message: str) -> None:
    if settings["paused"]:
        event["notification"] = "пауза: отправки нет"
        return
    try:
        result = send_notification(telegram_token(), settings["chat_id"], message)
        event["notification"] = {
            "skipped_no_chat_id": "не отправлено: ID не указан",
            "skipped_no_token": "не отправлено: токен не задан",
            "sent": "отправлено",
        }[result]
    except NotificationError as error:
        event["notification"] = str(error)


def recent_profile_trades(browser: PulseBrowser, profile: str, instruments: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    found = []
    for item in instruments:
        latest = item.get("maxTradeDateTime")
        try:
            if not latest or datetime.fromisoformat(latest) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        cursor = None
        occurrences = {}
        for _ in range(10):
            page = browser.history(item["ticker"], item["classCode"], cursor)
            older = False
            for trade in page["items"]:
                try:
                    trade_time = datetime.fromisoformat(trade["tradeDateTime"])
                except (KeyError, TypeError, ValueError):
                    continue
                if trade_time < cutoff:
                    older = True
                    continue
                signature = (trade["tradeDateTime"], trade.get("action"), str(trade.get("averagePrice")))
                occurrence = occurrences.get(signature, 0)
                occurrences[signature] = occurrence + 1
                identity = f"{profile}:{item['ticker']}:{item['classCode']}:{signature}:{occurrence}"
                found.append({
                    "id": hashlib.sha256(identity.encode()).hexdigest()[:24], "profile": profile,
                    "ticker": item["ticker"], "classCode": item["classCode"], "name": item["showName"],
                    "action": trade.get("action"), "tradeDateTime": trade["tradeDateTime"],
                    "price": trade.get("averagePrice"), "currency": trade.get("currency"),
                    "can_demo_buy": item["classCode"] == "TQBR" and trade.get("action") == "buy",
                })
            cursor = page.get("nextCursor")
            if older or not page.get("hasNext") or cursor is None:
                break
    return sorted(found, key=lambda trade: trade["tradeDateTime"], reverse=True)


def poll_once(browser: PulseBrowser, settings: dict, *, emit_events: bool = True) -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    profile, _ = operations_url(settings["profile_url"])
    previous = state.get(profile, {})
    profile, instruments = browser.snapshot(settings["profile_url"], previous)
    session_warning = False
    if isinstance(browser, PulseBrowser):
        try:
            browser.save_session()
        except Exception:
            session_warning = True
    if not isinstance(previous, dict):
        raise PulseError("Файл состояния повреждён")
    updates = {}
    fresh = []
    for item in instruments:
        key = f"{item['ticker']}:{item['classCode']}"
        count = item["totalOperationsCount"]
        old = previous.get(key)
        if old is not None:
            if type(old) is not int or count < old:
                raise PulseError(f"Счётчик {key} уменьшился; требуется проверка")
            delta = count - old
            if delta > len(item["history"]):
                raise PulseError(f"История {key} неполная")
            for trade in reversed(item["history"][:delta]):
                if trade.get("action") not in {"buy", "sell"} or not trade.get("tradeDateTime"):
                    raise PulseError(f"Сделка {key} имеет неизвестный формат")
                fresh.append((item, trade))
        updates[key] = count
    state[profile] = {**previous, **updates}
    # Commit the watermark before side effects: a retry cannot create a second demo purchase.
    write_state(STATE_PATH, state)
    try:
        recent = recent_profile_trades(browser, profile, instruments)
        recent_message = "Данные Пульса получены"
    except Exception:
        recent = []
        recent_message = "Список получен; история за 24 часа временно недоступна"
    if session_warning:
        recent_message += "; браузерную сессию не удалось сохранить"
    with LOCK:
        MONITOR.update(status="running", message=recent_message, last_check=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), instrument_count=len(instruments),
                       instruments=[{key: item.get(key) for key in ("ticker", "classCode", "showName", "type", "totalOperationsCount", "maxTradeDateTime")} for item in instruments])
        TODAY[:] = recent
        AUTH.update(status="authenticated", message="Вход в Пульс подтверждён")
    if not emit_events:
        return
    matched_ids = set()
    for item, trade in sorted(fresh, key=lambda pair: pair[1]["tradeDateTime"]):
        side = trade["action"]
        simulated = side == "buy" and settings["auto_demo_buy"]
        event = {
            "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "trade_time": trade["tradeDateTime"], "profile": profile,
            "instrument": item["ticker"], "instrument_name": item["showName"],
            "side": side, "price": f"{trade.get('averagePrice', '—')} {trade.get('currency', '')}",
            "source": "pulse", "trade": "ДЕМО: куплено" if simulated else "не выставлялась",
        }
        match = next((candidate for candidate in recent
                      if candidate["id"] not in matched_ids and candidate["ticker"] == item["ticker"]
                      and candidate["classCode"] == item["classCode"] and candidate["action"] == side
                      and candidate["tradeDateTime"] == trade["tradeDateTime"]
                      and str(candidate["price"]) == str(trade.get("averagePrice"))), None)
        if match:
            event["source_event_id"] = match["id"]
            matched_ids.add(match["id"])
        message = f"Пульс · {profile} · {item['ticker']} · {'Покупка' if side == 'buy' else 'Продажа'} · {event['price']}. {event['trade']}. Реальной заявки нет."
        notify(event, settings, message)
        add_event(event)


def handle_poll_error(browser: PulseBrowser, error: Exception, profile_url: str) -> bool:
    message = str(error) if isinstance(error, PulseError) else f"Ошибка браузера ({type(error).__name__})"
    login_needed = isinstance(error, PulseError) and any(
        marker in message for marker in ("Сделки не загрузились", "HTTP 401", "HTTP 403", "Заверши вход")
    )
    with LOCK:
        MONITOR.update(status="error", message=message)
        if login_needed:
            AUTH.update(status="required" if browser.headless else "waiting", message=message)
        elif not isinstance(error, PulseError) and AUTH["status"] != "authenticated":
            AUTH.update(status="required" if browser.headless else "waiting", message=message)
        elif AUTH["status"] == "authenticated":
            AUTH["message"] = "Восстанавливаем соединение с Пульсом из сохранённой сессии"
    try:
        recovered = browser.recover_page()
    except Exception:
        recovered = False
    should_close = not recovered or (browser.headless and (login_needed or not isinstance(error, PulseError)))
    if not should_close and (login_needed or not isinstance(error, PulseError)):
        browser.list_url = None
    return should_close


def monitor_loop() -> None:
    browser = None
    last_profile = None
    next_poll = 0.0
    next_ui_probe = 0.0
    next_reconnect = 0.0
    reconnect_delay = 3.0
    while True:
        settings = load_settings()
        profile = operations_url(settings["profile_url"])[0].casefold()
        if last_profile != profile:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            browser = None
            last_profile = profile
            next_poll = 0
            next_reconnect = 0
            reconnect_delay = 3.0
            with LOCK:
                if AUTH["status"] == "authenticated":
                    MONITOR.update(status="checking", message="Загружаем сделки выбранного профиля")
                    MONITOR.update(instrument_count=0, instruments=[])
                    TODAY.clear()
                else:
                    AUTH.update(status="checking", message="Проверяем сохранённый вход в Пульс")

        try:
            request = HISTORY_REQUESTS.get(timeout=0.5)
        except queue.Empty:
            request = None
        if request:
            try:
                if request["action"] in {"auth_start", "show"}:
                    if browser and browser.page.is_closed():
                        browser.recover_page()
                    if browser is None or browser.headless or browser.page.is_closed():
                        if browser:
                            browser.close()
                        browser = PulseBrowser(ROOT / ".local" / "pulse-browser", headless=False)
                        browser.open(settings["profile_url"])
                    browser.show()
                    next_poll = 0
                    with LOCK:
                        AUTH.update(status="waiting", message="Войди в Т-Банк в открытом окне. Проверка выполнится автоматически")
                elif request["action"] == "auth_check":
                    if browser:
                        browser.refresh(settings["profile_url"])
                        next_poll = 0
                    else:
                        with LOCK:
                            AUTH.update(status="checking" if AUTH["status"] != "authenticated" else "authenticated",
                                        message="Восстанавливаем сохранённую сессию Пульса")
                        next_reconnect = 0
                elif request["action"] == "history" and browser and AUTH["status"] == "authenticated":
                    request["result"] = browser.history(request["ticker"], request["class_code"], request["cursor"])
                else:
                    request["error"] = "Сначала авторизуйся в Пульсе"
            except Exception as error:
                if request["action"] == "history":
                    request["error"] = str(error) if isinstance(error, PulseError) else "История не загрузилась"
                else:
                    with LOCK:
                        AUTH.update(status="required", message=str(error) if isinstance(error, PulseError) else f"Окно Пульса не открылось ({type(error).__name__})")
            finally:
                if "ready" in request:
                    request["ready"].set()

        if browser is None and AUTH["status"] in {"checking", "authenticated"} and time.monotonic() >= next_reconnect:
            try:
                browser = PulseBrowser(ROOT / ".local" / "pulse-browser", headless=True)
                browser.open(settings["profile_url"])
                next_poll = 0
            except Exception as error:
                with LOCK:
                    message = str(error) if isinstance(error, PulseError) else f"Не удалось открыть браузер ({type(error).__name__})"
                    if AUTH["status"] == "authenticated":
                        MONITOR.update(status="error", message=message)
                        AUTH["message"] = "Восстанавливаем сохранённую сессию Пульса"
                    else:
                        AUTH.update(status="required", message=message)
                browser = None
                next_reconnect = time.monotonic() + reconnect_delay
                reconnect_delay = min(reconnect_delay * 2, 60)

        if browser and AUTH["status"] in {"waiting", "required"} and not browser.list_url:
            try:
                if browser.page.is_closed() and not browser.recover_page():
                    raise PulseError("Окно Пульса закрыто")
                browser.page.wait_for_timeout(250)
                if time.monotonic() >= next_ui_probe:
                    if browser.nickname_url and browser.instrument_urls:
                        browser.resolve_target()
                    if browser.list_url:
                        with LOCK:
                            AUTH["message"] = "Сделки найдены, загружаем данные профиля"
                        next_poll = 0
                    next_ui_probe = time.monotonic() + 3
            except Exception as error:
                if not browser.recover_page():
                    with LOCK:
                        AUTH.update(status="required", message=f"Окно Пульса закрыто ({type(error).__name__})")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = None
            continue

        if browser and time.monotonic() >= next_poll:
            try:
                poll_once(browser, settings, emit_events=settings["monitoring_enabled"])
                reconnect_delay = 3.0
                if not settings["monitoring_enabled"]:
                    with LOCK:
                        MONITOR.update(status="stopped", message="Живые уведомления выключены; анализ профиля обновлён")
            except Exception as error:
                if handle_poll_error(browser, error, settings["profile_url"]):
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = None
                    next_reconnect = time.monotonic() + reconnect_delay
                    reconnect_delay = min(reconnect_delay * 2, 60)
            next_poll = time.monotonic() + (settings["poll_seconds"] if AUTH["status"] == "authenticated" else 3)


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            body = (ROOT / "demo" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            with LOCK:
                authenticated = AUTH["status"] == "authenticated"
                self.respond(200, {"settings": load_settings(), "token_configured": bool(telegram_token()),
                                   "events": EVENTS,
                                   "monitor": MONITOR if authenticated else {"status": "stopped", "message": "Ожидаем входа в Пульс", "last_check": None, "instrument_count": 0, "instruments": []},
                                   "auth": AUTH, "today": TODAY if authenticated else []})
        else:
            self.respond(404, {"error": "Не найдено"})

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if origin and origin != expected:
            self.respond(403, {"error": "Запрос из другого источника отклонён"})
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.respond(415, {"error": "Нужен JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("Некорректный размер запроса")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Нужен объект JSON")
            with LOCK:
                authenticated = AUTH["status"] == "authenticated"
            if not authenticated and self.path not in {"/api/auth/start", "/api/browser/show", "/api/auth/check", "/api/settings", "/api/demo"}:
                self.respond(403, {"error": "Сначала авторизуйся в Пульсе"})
                return
            if self.path == "/api/settings":
                self.respond(200, {"settings": save_settings(data)})
            elif self.path == "/api/demo":
                side = data.get("side")
                if side not in {"buy", "sell"}:
                    raise ValueError("Выбери покупку или продажу")
                settings = load_settings()
                event = {
                    "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "profile": urlparse(settings["profile_url"]).path.rstrip("/").split("/")[-1],
                    "instrument": "DEMO",
                    "side": side,
                    "price": "100 ₽",
                    "trade": "не выставлялась",
                    "source": "manual_demo",
                }
                message = f"ДЕМО · {event['profile']} · DEMO · {'Покупка' if side == 'buy' else 'Продажа'} по 100 ₽. Реальной заявки нет."
                notify(event, settings, message)
                add_event(event)
                self.respond(200, {"event": event, "events": EVENTS})
            elif self.path == "/api/history":
                ticker, class_code = data.get("ticker"), data.get("classCode")
                cursor = data.get("cursor")
                with LOCK:
                    available = AUTH["status"] == "authenticated" and any(
                        item["ticker"] == ticker and item["classCode"] == class_code for item in MONITOR["instruments"])
                if not available or (cursor is not None and type(cursor) not in (str, int)):
                    raise ValueError("Инструмент недоступен или мониторинг не подключён")
                request = {"action": "history", "ticker": ticker, "class_code": class_code, "cursor": cursor, "ready": threading.Event()}
                HISTORY_REQUESTS.put(request)
                if not request["ready"].wait(25):
                    self.respond(503, {"error": "История пока не ответила"})
                elif "error" in request:
                    self.respond(503, {"error": request["error"]})
                else:
                    self.respond(200, request["result"])
            elif self.path in {"/api/browser/show", "/api/auth/start"}:
                with LOCK:
                    if AUTH["status"] != "opening":
                        AUTH.update(status="opening", message="Открываем окно Пульса…")
                        HISTORY_REQUESTS.put({"action": "auth_start"})
                self.respond(202, {"ok": True})
            elif self.path == "/api/auth/check":
                with LOCK:
                    if AUTH["status"] == "authenticated":
                        MONITOR.update(status="checking", message="Обновляем данные Пульса…")
                        HISTORY_REQUESTS.put({"action": "auth_check"})
                    elif AUTH["status"] != "checking":
                        AUTH.update(status="checking", message="Проверяем сделки Пульса…")
                        HISTORY_REQUESTS.put({"action": "auth_check"})
                self.respond(202, {"ok": True})
            elif self.path == "/api/demo/buy":
                candidate_id = data.get("id")
                with LOCK:
                    candidate = next((item.copy() for item in TODAY if item["id"] == candidate_id), None)
                    duplicate = any(item.get("source_event_id") == candidate_id and item.get("trade") == "ДЕМО: куплено" for item in EVENTS)
                    if not candidate or not candidate["can_demo_buy"] or duplicate:
                        raise ValueError("Сделка недоступна для демо-покупки")
                    if datetime.fromisoformat(candidate["tradeDateTime"]) < datetime.now(timezone.utc) - timedelta(days=1):
                        raise ValueError("Эта сделка старше 24 часов")
                    event = {
                        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                        "trade_time": candidate["tradeDateTime"], "profile": candidate["profile"],
                        "instrument": candidate["ticker"], "instrument_name": candidate["name"],
                        "side": "buy", "price": f"{candidate['price']} {candidate['currency'] or ''}",
                        "source": "historic_demo", "source_event_id": candidate_id, "trade": "ДЕМО: куплено",
                    }
                    add_event(event)
                settings = load_settings()
                notify(event, settings, f"ДЕМО · {event['profile']} · {event['instrument']} · покупка по {event['price']}. Реальной заявки нет.")
                with LOCK:
                    write_state(EVENTS_PATH, EVENTS)
                self.respond(200, {"event": event})
            else:
                self.respond(404, {"error": "Не найдено"})
        except (ValueError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        server = LocalHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        raise SystemExit(f"Админка уже запущена на порту {args.port} или порт занят") from error
    threading.Thread(target=monitor_loop, name="pulse-monitor", daemon=True).start()
    print(f"Админка: http://127.0.0.1:{server.server_port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
