"""Local, repeatable demonstration. No live Pulse polling or brokerage orders."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from telegram_notify import NotificationError, send_notification


ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / ".local" / "demo-settings.json"
DEFAULTS = {
    "profile_url": "https://www.tbank.ru/invest/social/profile/LinMath/",
    "poll_seconds": 15,
    "chat_id": "",
    "paused": False,
}
EVENTS: list[dict] = []


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
    if type(seconds) is not int or not 5 <= seconds <= 3600:
        raise ValueError("Интервал должен быть от 5 до 3600 секунд")
    chat_id = str(data.get("chat_id", "")).strip()
    if chat_id and (not chat_id.isascii() or not chat_id.isdecimal() or chat_id.startswith("0")):
        raise ValueError("Telegram ID должен быть положительным числом")
    paused = data.get("paused")
    if type(paused) is not bool:
        raise ValueError("Некорректное значение паузы")
    settings = {"profile_url": url, "poll_seconds": seconds, "chat_id": chat_id, "paused": paused}
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, SETTINGS_PATH)
    return settings


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
            self.respond(200, {"settings": load_settings(), "token_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")), "events": EVENTS})
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
                }
                if settings["paused"]:
                    event["notification"] = "пауза: событие сохранено, отправки нет"
                else:
                    message = f"ДЕМО · {event['profile']} · DEMO · {'Покупка' if side == 'buy' else 'Продажа'} по 100 ₽. Реальной заявки нет."
                    try:
                        result = send_notification(os.environ.get("TELEGRAM_BOT_TOKEN", ""), settings["chat_id"], message)
                        event["notification"] = {
                            "skipped_no_chat_id": "не отправлено: ID не указан",
                            "skipped_no_token": "не отправлено: токен не задан",
                            "sent": "отправлено",
                        }[result]
                    except NotificationError as error:
                        event["notification"] = str(error)
                EVENTS.insert(0, event)
                del EVENTS[30:]
                self.respond(200, {"event": event, "events": EVENTS})
            else:
                self.respond(404, {"error": "Не найдено"})
        except (ValueError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Демо-админка: http://127.0.0.1:{server.server_port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
