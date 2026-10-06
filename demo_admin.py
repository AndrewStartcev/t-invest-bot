"""Local Pulse monitor, paper portfolio and explicitly enabled brokerage orders."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from broker_http import normalize_token
from demo_engine import DEFAULT_RULES, asset_type, simulate, validated_rules
from broker_read import BrokerError, account_snapshot, read_only_accounts
from broker_trade import TradeError, full_access_accounts, order_state, prepare_order, submit_order
from telegram_notify import NotificationError, send_notification
from server_config import public_origin, server_mode
from pulse_live import PulseBrowser, PulseError, canonical_profile_url, operations_url
from pulse_replay import write_state


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TINVEST_DATA_DIR", str(ROOT / ".local"))).expanduser().resolve()
SETTINGS_PATH = DATA_DIR / "demo-settings.json"
STATE_PATH = DATA_DIR / "pulse-live-state.json"
EVENTS_PATH = DATA_DIR / "events.json"
POSITIONS_PATH = DATA_DIR / "demo-positions.json"
TOKEN_PATH = DATA_DIR / "telegram-token.txt"
BROKER_TOKEN_PATH = DATA_DIR / "broker-read-token.txt"
BROKER_ACCOUNT_PATH = DATA_DIR / "broker-account.txt"
TRADE_TOKEN_PATH = DATA_DIR / "broker-trade-token.txt"
TRADE_ACCOUNT_PATH = DATA_DIR / "trade-account.txt"
REAL_ORDERS_PATH = DATA_DIR / "real-orders.json"
DEFAULTS = {
    "profile_url": "https://www.tbank-online.com/invest/social/profile/LinMath/",
    "poll_seconds": 30,
    "chat_id": "",
    "paused": False,
    "monitoring_enabled": False,
    "auto_demo_buy": False,
    "real_mode": "off",
    "rules": DEFAULT_RULES,
}
EVENTS: list[dict] = json.loads(EVENTS_PATH.read_text(encoding="utf-8")) if EVENTS_PATH.exists() else []
POSITIONS: dict = json.loads(POSITIONS_PATH.read_text(encoding="utf-8")) if POSITIONS_PATH.exists() else {}
LOCK = threading.RLock()
TRADE_FLOW_LOCK = threading.Lock()
BROKER_REFRESH_LOCK = threading.Lock()
HISTORY_REQUESTS = queue.Queue()
MONITOR = {"status": "stopped", "message": "Мониторинг выключен", "last_check": None, "profile": "", "instrument_count": 0, "instruments": []}
AUTH = {"status": "checking", "message": "Проверяем сохранённый вход в Пульс"}
TODAY: list[dict] = []
MONTH = {"status": "idle", "items": [], "processed": 0, "total": 0, "message": "", "loaded_at": None}
BROKER = {"status": "disconnected", "message": "Подключи токен T-Invest только для чтения", "accounts": [],
          "selected_account_id": "", "snapshot": None, "last_check": None}
TRADING = {"status": "disconnected", "message": "Торговый токен не подключён", "accounts": [],
           "selected_account_id": ""}
REAL_ORDERS: dict[str, dict] = json.loads(REAL_ORDERS_PATH.read_text(encoding="utf-8")) if REAL_ORDERS_PATH.exists() else {}
PREVIEWS: dict[str, dict] = {}
SCENARIOS = {
    "stock_buy": {"ticker": "DEMO-R", "classCode": "TQBR", "name": "Демо · акция", "asset_type": "stock",
                  "side": "buy", "price": 500, "lot_size": 1, "currency": "rub"},
    "stock_sell": {"ticker": "DEMO-R", "classCode": "TQBR", "name": "Демо · акция", "asset_type": "stock",
                   "side": "sell", "price": 530, "lot_size": 1, "currency": "rub"},
    "expensive_stock": {"ticker": "DEMO-H", "classCode": "TQBR", "name": "Демо · дорогая акция", "asset_type": "stock",
                        "side": "buy", "price": 8000, "lot_size": 1, "currency": "rub"},
    "bond_buy": {"ticker": "DEMO-B", "classCode": "TQOB", "name": "Демо · облигация", "asset_type": "bond",
                 "side": "buy", "price": 1000, "lot_size": 1, "currency": "rub"},
    "fund_buy": {"ticker": "DEMO-ETF", "classCode": "TQTF", "name": "Демо · фонд", "asset_type": "fund",
                 "side": "buy", "price": 250, "lot_size": 1, "currency": "rub"},
    "future_buy": {"ticker": "DEMO-F", "classCode": "SPBFUT", "name": "Демо · фьючерс", "asset_type": "future",
                   "side": "buy", "margin_rub": 9000, "currency": "rub"},
    "future_sell": {"ticker": "DEMO-F", "classCode": "SPBFUT", "name": "Демо · фьючерс", "asset_type": "future",
                    "side": "sell", "margin_rub": 9000, "currency": "rub"},
    "future_over_limit": {"ticker": "DEMO-X", "classCode": "SPBFUT", "name": "Демо · фьючерс 27 000 ₽",
                          "asset_type": "future", "side": "buy", "margin_rub": 27000, "currency": "rub"},
}


def telegram_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or (TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else "")


def broker_token() -> str:
    return (BROKER_TOKEN_PATH.read_text(encoding="utf-8").strip() if BROKER_TOKEN_PATH.exists() else "") or os.environ.get("TINVEST_READ_TOKEN", "")


def trade_token() -> str:
    return TRADE_TOKEN_PATH.read_text(encoding="utf-8").strip() if TRADE_TOKEN_PATH.exists() else ""


def connect_trading(candidate: str) -> dict:
    token = normalize_token(candidate if candidate else trade_token())
    if not token or len(token) > 1000 or any(char.isspace() for char in token):
        raise ValueError("Введи торговый токен T-Invest")
    accounts = full_access_accounts(token)
    selected = TRADE_ACCOUNT_PATH.read_text(encoding="utf-8").strip() if TRADE_ACCOUNT_PATH.exists() else ""
    if selected not in {account["id"] for account in accounts}:
        selected = accounts[0]["id"]
    if candidate:
        disarm_real_trading()
        save_private_text(TRADE_TOKEN_PATH, token)
    save_private_text(TRADE_ACCOUNT_PATH, selected)
    with LOCK:
        TRADING.update(status="connected", message="Торговый токен проверен", accounts=accounts,
                       selected_account_id=selected)
        PREVIEWS.clear()
    return TRADING.copy()


def select_trade_account(account_id: str) -> dict:
    with LOCK:
        if account_id not in {account["id"] for account in TRADING["accounts"]}:
            raise ValueError("Выбери торговый счёт из списка")
    disarm_real_trading()
    with LOCK:
        save_private_text(TRADE_ACCOUNT_PATH, account_id)
        TRADING["selected_account_id"] = account_id
        PREVIEWS.clear()
        result = TRADING.copy()
    return result


def selected_trade_account() -> tuple[str, str]:
    token = trade_token()
    if not token:
        raise TradeError("Торговый токен не подключён")
    with LOCK:
        account_id = TRADING["selected_account_id"]
        accounts = TRADING["accounts"]
    if not account_id or account_id not in {account["id"] for account in accounts}:
        connect_trading("")
        with LOCK:
            account_id = TRADING["selected_account_id"]
    return token, account_id


def copied_lots(account_id: str, ticker: str, class_code: str, side: str) -> int:
    with LOCK:
        rows = [row for row in REAL_ORDERS.values() if row["account_id"] == account_id
                and row["ticker"] == ticker and row["class_code"] == class_code]
        filled = sum((1 if row["side"] == "buy" else -1) * int(row.get("filled_lots", 0)) for row in rows)
        pending = sum(int(row["quantity"]) - int(row.get("filled_lots", 0))
                      for row in rows if row["status"] in {"intent", "uncertain", "submitted", "partial"}
                      and row["side"] == side)
        return max(0, filled + (pending if side == "buy" else -pending))


def persist_real_orders() -> None:
    write_state(REAL_ORDERS_PATH, REAL_ORDERS)


def disarm_real_trading() -> None:
    settings = load_settings()
    if settings.get("real_mode") != "off":
        settings["real_mode"] = "off"
        save_settings(settings)


def save_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(value, encoding="utf-8")
    os.replace(temp, path)


def connect_broker(candidate: str) -> dict:
    token = normalize_token(candidate if candidate else broker_token())
    if not token or len(token) > 1000 or any(char.isspace() for char in token):
        raise ValueError("Введи токен T-Invest только для чтения")
    accounts = read_only_accounts(token)
    selected = BROKER_ACCOUNT_PATH.read_text(encoding="utf-8").strip() if BROKER_ACCOUNT_PATH.exists() else ""
    if selected not in {account["id"] for account in accounts}:
        selected = accounts[0]["id"]
    if candidate:
        save_private_text(BROKER_TOKEN_PATH, token)
    save_private_text(BROKER_ACCOUNT_PATH, selected)
    with LOCK:
        BROKER.update(status="connected", message="Доступ только для чтения", accounts=accounts,
                      selected_account_id=selected, snapshot=None, last_check=None)
    return BROKER.copy()


def select_broker_account(account_id: str) -> dict:
    with LOCK:
        if account_id not in {account["id"] for account in BROKER["accounts"]}:
            raise ValueError("Выбери счёт из списка")
        save_private_text(BROKER_ACCOUNT_PATH, account_id)
        BROKER.update(selected_account_id=account_id, snapshot=None, last_check=None)
    return refresh_broker()


def refresh_broker() -> dict:
    with BROKER_REFRESH_LOCK:
        return _refresh_broker()


def _refresh_broker() -> dict:
    token = broker_token()
    if not token:
        raise ValueError("Сначала подключи токен T-Invest")
    with LOCK:
        account_id = BROKER["selected_account_id"]
        accounts = BROKER["accounts"]
    if not account_id or account_id not in {account["id"] for account in accounts}:
        connect_broker("")
        with LOCK:
            account_id = BROKER["selected_account_id"]
    snapshot = account_snapshot(token, account_id)
    with LOCK:
        if BROKER["selected_account_id"] != account_id or broker_token() != token:
            return BROKER.copy()
        BROKER.update(status="connected", message="Счёт обновлён · доступ только для чтения",
                      snapshot=snapshot, last_check=datetime.now(timezone.utc).isoformat())
        return BROKER.copy()


def broker_refresh_once() -> None:
    """Refresh the own account even while Pulse monitoring is stopped."""
    if not broker_token():
        return
    try:
        refresh_broker()
    except (BrokerError, ValueError) as error:
        with LOCK:
            BROKER.update(status="error", message=str(error))


def broker_monitor_loop() -> None:
    while True:
        try:
            broker_refresh_once()
        except Exception as error:
            print(f"Не удалось обновить портфель: {type(error).__name__}")
        time.sleep(30)


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
        return json.loads(json.dumps(DEFAULTS))
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    settings = {**DEFAULTS, **{key: data[key] for key in DEFAULTS if key in data}}
    # Existing installations may still have the .ru profile in .local.
    # Navigate through the working mirror without deleting any saved settings.
    settings["profile_url"] = canonical_profile_url(settings["profile_url"])
    settings["rules"] = validated_rules(settings["rules"])
    return settings


def save_settings(data: dict) -> dict:
    url = str(data.get("profile_url", "")).strip()
    try:
        url = canonical_profile_url(url)
    except PulseError as error:
        raise ValueError(str(error)) from error
    seconds = data.get("poll_seconds")
    if type(seconds) is not int or not 30 <= seconds <= 3600:
        raise ValueError("Интервал должен быть от 30 до 3600 секунд")
    chat_id = str(data.get("chat_id", "")).strip()
    if chat_id and (len(chat_id) > 20 or not chat_id.isascii() or not chat_id.isdecimal() or chat_id.startswith("0")):
        raise ValueError("Telegram ID должен быть положительным числом")
    paused = data.get("paused")
    if type(paused) is not bool:
        raise ValueError("Некорректное значение паузы")
    enabled = data.get("monitoring_enabled")
    auto_buy = data.get("auto_demo_buy")
    if type(enabled) is not bool or type(auto_buy) is not bool:
        raise ValueError("Некорректное значение режима")
    if auto_buy and not enabled:
        raise ValueError("Для автокопирования включи мониторинг")
    real_mode = data.get("real_mode", "off")
    if real_mode not in {"off", "confirm", "auto"}:
        raise ValueError("Неизвестный режим реальной торговли")
    if real_mode != "off" and (not enabled or auto_buy):
        raise ValueError("Для реальной торговли включи мониторинг и выключи симуляцию")
    if real_mode != "off" and (not trade_token() or not TRADE_ACCOUNT_PATH.exists()):
        raise ValueError("Сначала подключи торговый токен и выбери счёт")
    rules = validated_rules(data.get("rules", DEFAULT_RULES))
    if "telegram_token" in data:
        if not isinstance(data["telegram_token"], str):
            raise ValueError("Некорректный токен Telegram")
        save_telegram_token(data["telegram_token"])
    settings = {"profile_url": url, "poll_seconds": seconds, "chat_id": chat_id, "paused": paused,
                "monitoring_enabled": enabled, "auto_demo_buy": auto_buy, "real_mode": real_mode, "rules": rules}
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


def apply_demo(event: dict, signal: dict, settings: dict) -> None:
    """Apply a paper decision and persist its own portfolio. No broker calls."""
    with LOCK:
        result, positions = simulate(signal, settings.get("rules", DEFAULT_RULES), POSITIONS)
        if result["status"] == "executed":
            write_state(POSITIONS_PATH, positions)
            POSITIONS.clear()
            POSITIONS.update(positions)
        event.update(demo_status=result["status"], reason=result["reason"],
                     quantity=result["quantity"], amount_rub=result["amount_rub"],
                     asset_type=result["asset_type"])
        event["trade"] = ("ДЕМО: куплено" if signal["side"] == "buy" else "ДЕМО: продано") \
            if result["status"] == "executed" else "ДЕМО: пропущено"


def notify(event: dict, settings: dict, message: str, *, urgent: bool = False) -> None:
    if settings["paused"] and not urgent:
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


def prepare_real(signal: dict, settings: dict) -> tuple[dict, str]:
    token, account_id = selected_trade_account()
    held = copied_lots(account_id, signal["ticker"], signal["classCode"], signal["side"])
    return prepare_order(signal, settings["rules"], token, account_id, held), token


def place_real(plan: dict, source_key: str, token: str) -> dict:
    with LOCK:
        if any(row["account_id"] == plan["account_id"] and row["source_key"] == source_key
               for row in REAL_ORDERS.values()):
            raise TradeError("Эта сделка уже отправлялась брокеру; проверь её статус")
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, plan["account_id"] + ":" + source_key))
        row = {**plan, "source_key": source_key, "request_id": request_id, "status": "intent",
               "filled_lots": 0, "created_at": datetime.now(timezone.utc).isoformat(), "broker_order_id": ""}
        REAL_ORDERS[request_id] = row
        persist_real_orders()
    try:
        response = submit_order(plan, token, request_id)
    except TradeError as error:
        with LOCK:
            row.update(status="uncertain", message=str(error))
            persist_real_orders()
        raise TradeError(str(error) + "; повторная отправка заблокирована до сверки") from None
    with LOCK:
        row.update(status="submitted", broker_order_id=str(response.get("orderId") or ""),
                   message=str(response.get("message") or "Заявка отправлена"))
        persist_real_orders()
    return row.copy()


def update_real_order_state(request_id: str) -> dict:
    with LOCK:
        row = REAL_ORDERS.get(request_id)
        if not row:
            raise TradeError("Заявка не найдена")
        row = row.copy()
    state = order_state(trade_token(), row["account_id"], request_id, row.get("price_type", "PRICE_TYPE_UNSPECIFIED"))
    broker_status = str(state.get("executionReportStatus") or "")
    known = {"EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
             "EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_CANCELLED",
             "EXECUTION_REPORT_STATUS_REJECTED"}
    if broker_status not in known:
        raise TradeError("Брокер вернул неизвестный статус заявки; нужна ручная сверка")
    try:
        filled = int(state.get("lotsExecuted", 0))
    except (ValueError, TypeError):
        raise TradeError("Брокер вернул некорректное количество исполненных лотов") from None
    if not 0 <= filled <= row["quantity"]:
        raise TradeError("Брокер вернул некорректное количество исполненных лотов")
    if broker_status == "EXECUTION_REPORT_STATUS_FILL" and filled != row["quantity"]:
        raise TradeError("Количество исполненных лотов не совпало со статусом брокера")
    status = ("filled" if broker_status == "EXECUTION_REPORT_STATUS_FILL" else
              "rejected" if broker_status == "EXECUTION_REPORT_STATUS_REJECTED" else
              "cancelled" if broker_status == "EXECUTION_REPORT_STATUS_CANCELLED" else
              "partial" if filled else "submitted")
    with LOCK:
        current = REAL_ORDERS[request_id]
        old_status, old_filled = current["status"], int(current["filled_lots"])
        current.update(status=status, filled_lots=max(old_filled, filled),
                       broker_status=broker_status, last_check=datetime.now(timezone.utc).isoformat())
        persist_real_orders()
        updated = current.copy()
    if status != old_status or filled > old_filled:
        settings = load_settings()
        event = {"time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                 "profile": "Брокер", "instrument": row["ticker"], "side": row["side"],
                 "price": row["price"] + (" пунктов" if row.get("price_type") == "PRICE_TYPE_POINT" else " ₽"), "source": "broker_status", "source_key": row["source_key"],
                 "trade": "РЕАЛЬНО: " + status, "reason": f"Исполнено {updated['filled_lots']} из {row['quantity']} лот(ов)",
                 "real_status": status, "quantity": updated["filled_lots"]}
        notify(event, settings, f"Брокер · {row['ticker']} · {row['side']} · {status}. "
              f"Исполнено {updated['filled_lots']} из {row['quantity']} лот(ов).",
              urgent=status in {"rejected", "cancelled"})
        add_event(event)
    return updated


def reconcile_real_orders() -> None:
    with LOCK:
        pending = [row["request_id"] for row in REAL_ORDERS.values()
                   if row["status"] in {"intent", "uncertain", "submitted", "partial"}]
    for request_id in pending:
        try:
            update_real_order_state(request_id)
        except TradeError:
            continue


def reconcile_loop() -> None:
    while True:
        try:
            if trade_token():
                reconcile_real_orders()
        except Exception as error:
            print(f"Не удалось сверить брокерские заявки: {type(error).__name__}")
        time.sleep(15)


def real_signal(source_id: str) -> tuple[dict, str, dict | None]:
    with LOCK:
        candidate = next((item.copy() for item in TODAY if item["id"] == source_id), None)
        event = next((item for item in EVENTS if item.get("source_key") == source_id
                      and item.get("source") == "pulse"), None)
    if candidate:
        if candidate["action"] != "buy" or not candidate["can_demo_buy"]:
            raise TradeError("Для прошлой сделки доступна только покупка поддерживаемого инструмента")
        if datetime.fromisoformat(candidate["tradeDateTime"]) < datetime.now(timezone.utc) - timedelta(days=1):
            raise TradeError("Сделка старше 24 часов")
        return ({"ticker": candidate["ticker"], "classCode": candidate["classCode"],
                 "asset_type": candidate["asset_type"], "side": "buy"},
                "historic:" + source_id, None)
    if event:
        if event.get("real_status") not in {"awaiting_approval", "blocked"}:
            raise TradeError("Эта сделка уже обработана")
        return ({"ticker": event["instrument"], "classCode": event["classCode"],
                 "asset_type": event["asset_type"], "side": event["side"]}, source_id, event)
    raise TradeError("Сделка больше не доступна")


def alert_trade_failure(source_key: str, signal: dict, message: str) -> None:
    settings = load_settings()
    event = {"time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "profile": "Брокер", "instrument": signal["ticker"], "side": signal["side"],
             "price": "—", "source": "broker_error", "source_key": source_key,
             "trade": "РЕАЛЬНО: заявка не отправлена", "reason": message, "real_status": "blocked"}
    notify(event, settings, f"Ошибка реальной сделки · {signal['ticker']} · {signal['side']}: {message}", urgent=True)
    add_event(event)


def create_real_preview(source_id: str) -> dict:
    settings = load_settings()
    if settings["real_mode"] == "off":
        raise TradeError("Реальная торговля выключена")
    with LOCK:
        if AUTH["status"] != "authenticated":
            raise TradeError("Нет подтверждённого входа в Пульс")
    signal, source_key, _ = real_signal(source_id)
    try:
        plan, _ = prepare_real(signal, settings)
    except TradeError as error:
        alert_trade_failure(source_key, signal, str(error))
        raise
    preview_id = str(uuid.uuid4())
    with LOCK:
        PREVIEWS.clear()
        PREVIEWS[preview_id] = {"plan": plan, "signal": signal, "source_key": source_key,
                                "created": time.monotonic()}
    return {"id": preview_id, "plan": plan}


def confirm_real_preview(preview_id: str) -> dict:
    settings = load_settings()
    if settings["real_mode"] == "off":
        raise TradeError("Реальная торговля выключена")
    with LOCK:
        if AUTH["status"] != "authenticated":
            raise TradeError("Нет подтверждённого входа в Пульс")
    with LOCK:
        preview = PREVIEWS.pop(preview_id, None)
    if not preview or time.monotonic() - preview["created"] > 30:
        raise TradeError("Подтверждение устарело; подготовь заявку заново")
    signal, source_key = preview["signal"], preview["source_key"]
    try:
        with TRADE_FLOW_LOCK:
            plan, token = prepare_real(signal, settings)
            original = preview["plan"]
            if any(plan[key] != original[key] for key in ("account_id", "instrument_uid", "side", "quantity", "price")):
                raise TradeError("Цена или доступное количество изменились; проверь заявку заново")
            order = place_real(plan, source_key, token)
    except TradeError as error:
        alert_trade_failure(source_key, signal, str(error))
        raise
    event = {"time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "profile": "Брокер", "instrument": signal["ticker"], "side": signal["side"],
             "price": plan["price"] + (" пунктов" if plan["price_type"] == "PRICE_TYPE_POINT" else " ₽"), "source": "broker_order", "source_key": source_key,
             "trade": "РЕАЛЬНО: заявка отправлена", "real_status": "submitted",
             "quantity": plan["quantity"], "amount_rub": plan["estimated_rub"],
             "reason": "Лимитная заявка; исполнение проверяется отдельно"}
    notify(event, settings, f"Реальная заявка · {signal['ticker']} · {signal['side']} · "
          f"{plan['quantity']} лот(ов) по {event['price']}. Статус исполнения проверяется.")
    add_event(event)
    return order


def append_history_page(found: list[dict], page: dict, item: dict, profile: str,
                        cutoff: datetime, occurrences: dict) -> bool:
    older = False
    for trade in page["items"]:
        try:
            trade_time = datetime.fromisoformat(trade["tradeDateTime"])
            if trade_time.tzinfo is None:
                raise ValueError("no timezone")
        except (KeyError, TypeError, ValueError):
            raise PulseError(f"Неизвестная дата сделки {item['ticker']}") from None
        if trade_time < cutoff:
            older = True
            continue
        signature = (trade["tradeDateTime"], trade.get("action"), str(trade.get("averagePrice")))
        occurrence = occurrences.get(signature, 0)
        occurrences[signature] = occurrence + 1
        identity = f"{profile}:{item['ticker']}:{item['classCode']}:{signature}:{occurrence}"
        kind = asset_type(item.get("type", ""), item["classCode"])
        found.append({
            "id": hashlib.sha256(identity.encode()).hexdigest()[:24], "profile": profile,
            "ticker": item["ticker"], "classCode": item["classCode"], "name": item["showName"],
            "asset_type": kind, "action": trade.get("action"), "tradeDateTime": trade["tradeDateTime"],
            "price": trade.get("averagePrice"), "currency": trade.get("currency"),
            "relative_yield": trade.get("relativeYield"),
            "can_demo_buy": kind in {"stock", "bond", "fund"} and trade.get("action") == "buy"
                            and trade_time >= datetime.now(timezone.utc) - timedelta(days=1),
        })
    return older


def next_history_cursor(page: dict, seen_cursors: set, ticker: str) -> str | int:
    cursor = page.get("nextCursor")
    if type(cursor) not in (str, int) or cursor == "" or cursor in seen_cursors:
        raise PulseError(f"Не удалось прочитать всю историю {ticker}")
    seen_cursors.add(cursor)
    return cursor


def recent_profile_trades(browser: PulseBrowser, profile: str, instruments: list[dict], *,
                          cutoff: datetime | None = None, max_pages: int = 100) -> list[dict]:
    cutoff = cutoff or datetime.now(timezone.utc) - timedelta(days=1)
    found = []
    for item in month_targets(instruments, cutoff):
        cursor = None
        seen_cursors = set()
        occurrences = {}
        for _ in range(max_pages):
            page = browser.history(item["ticker"], item["classCode"], cursor)
            older = append_history_page(found, page, item, profile, cutoff, occurrences)
            if older or not page.get("hasNext"):
                break
            cursor = next_history_cursor(page, seen_cursors, item["ticker"])
        else:
            raise PulseError(f"Слишком много страниц истории {item['ticker']}")
    return sorted(found, key=lambda trade: trade["tradeDateTime"], reverse=True)


def month_targets(instruments: list[dict], cutoff: datetime) -> list[dict]:
    result = []
    for item in instruments:
        latest = item.get("maxTradeDateTime")
        if not latest:
            continue
        try:
            latest_time = datetime.fromisoformat(latest)
            if latest_time.tzinfo is None:
                raise ValueError("no timezone")
            if latest_time >= cutoff:
                result.append(item.copy())
        except (KeyError, TypeError, ValueError):
            raise PulseError(f"Неизвестная дата последней сделки {item['ticker']}") from None
    return result


def advance_month_scan(browser: PulseBrowser, scan: dict, batch_size: int = 1) -> bool:
    """Read a few history pages in the monitor thread between regular polls."""
    for _ in range(batch_size):
        if scan["index"] >= len(scan["targets"]):
            skipped = scan.get("skipped", [])
            with LOCK:
                MONTH.update(status="ready", items=sorted(scan["found"], key=lambda item: item["tradeDateTime"], reverse=True),
                             processed=scan["index"], skipped=skipped,
                             message=(f"История загружена частично: не удалось прочитать {len(skipped)} инструмент(ов)"
                                      if skipped else "История за 30 дней загружена"),
                             loaded_at=datetime.now(timezone.utc).isoformat())
            return True
        item = scan["targets"][scan["index"]]
        try:
            page = browser.history(item["ticker"], item["classCode"], scan["cursor"])
        except PulseError as error:
            if not (str(error) == "История инструмента недоступна" or "HTTP 404" in str(error)):
                raise
            scan.setdefault("skipped", []).append(item["ticker"])
            scan["index"] += 1
            scan["cursor"] = None
            scan["seen_cursors"] = set()
            scan["occurrences"] = {}
            scan["pages"] = 0
            with LOCK:
                MONTH.update(processed=scan["index"],
                             message=f"Проверено инструментов: {scan['index']} из {len(scan['targets'])}; часть истории недоступна")
            continue
        scan["pages"] += 1
        if scan["pages"] > 500:
            raise PulseError(f"Слишком много страниц истории {item['ticker']}")
        older = append_history_page(scan["found"], page, item, scan["profile"], scan["cutoff"], scan["occurrences"])
        if older or not page.get("hasNext"):
            scan["index"] += 1
            scan["cursor"] = None
            scan["seen_cursors"] = set()
            scan["occurrences"] = {}
            scan["pages"] = 0
        else:
            scan["cursor"] = next_history_cursor(page, scan["seen_cursors"], item["ticker"])
        with LOCK:
            MONTH.update(processed=scan["index"], message=f"Проверено инструментов: {scan['index']} из {len(scan['targets'])}; страниц текущего: {scan['pages']}")
    return False


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
            for sequence, trade in enumerate(reversed(item["history"][:delta]), start=old + 1):
                if trade.get("action") not in {"buy", "sell"} or not trade.get("tradeDateTime"):
                    raise PulseError(f"Сделка {key} имеет неизвестный формат")
                fresh.append((item, trade, sequence))
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
        MONITOR.update(status="running", message=recent_message, last_check=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), profile=profile, instrument_count=len(instruments),
                       instruments=[{key: item.get(key) for key in ("ticker", "classCode", "showName", "type", "totalOperationsCount", "maxTradeDateTime")} for item in instruments])
        TODAY[:] = recent
        AUTH.update(status="authenticated", message="Вход в Пульс подтверждён")
    if not emit_events:
        return
    matched_ids = set()
    for item, trade, sequence in sorted(fresh, key=lambda pair: pair[1]["tradeDateTime"]):
        side = trade["action"]
        source_key = f"{profile}:{item['ticker']}:{item['classCode']}:{sequence}"
        event = {
            "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "trade_time": trade["tradeDateTime"], "profile": profile,
            "instrument": item["ticker"], "instrument_name": item["showName"],
            "classCode": item["classCode"], "asset_type": asset_type(item.get("type", ""), item["classCode"]),
            "side": side, "price": f"{trade.get('averagePrice', '—')} {trade.get('currency', '')}",
            "source": "pulse", "source_key": source_key, "trade": "не выставлялась",
            "relative_yield": trade.get("relativeYield"),
        }
        if settings.get("real_mode") == "confirm":
            event.update(real_status="awaiting_approval", trade="Ожидает подтверждения",
                         reason="Перед отправкой будут заново проверены цена, остаток и лимиты")
        elif settings.get("real_mode") == "auto":
            signal = {"ticker": item["ticker"], "classCode": item["classCode"],
                      "asset_type": event["asset_type"], "side": side}
            try:
                with TRADE_FLOW_LOCK:
                    plan, token = prepare_real(signal, settings)
                    placed = place_real(plan, source_key, token)
                unit = "пунктов" if plan["price_type"] == "PRICE_TYPE_POINT" else "₽"
                event.update(real_status=placed["status"], trade="РЕАЛЬНО: заявка отправлена",
                             reason=f"{plan['quantity']} лот(ов) по лимитной цене {plan['price']} {unit}",
                             quantity=plan["quantity"], amount_rub=plan["estimated_rub"])
            except TradeError as error:
                event.update(real_status="blocked", trade="РЕАЛЬНО: пропущено", reason=str(error))
        elif settings.get("auto_demo_buy"):
            apply_demo(event, {"ticker": item["ticker"], "classCode": item["classCode"],
                               "name": item["showName"], "asset_type": asset_type(item.get("type", ""), item["classCode"]),
                               "side": side, "price": trade.get("averagePrice"), "currency": trade.get("currency"),
                               "lot_size": 1 if asset_type(item.get("type", ""), item["classCode"]) in {"stock", "bond", "fund"} else None}, settings)
            if event["asset_type"] in {"stock", "bond", "fund"}:
                event["assumption"] = "Условный лот 1 единица; реальный размер лота не проверен"
        match = next((candidate for candidate in recent
                      if candidate["id"] not in matched_ids and candidate["ticker"] == item["ticker"]
                      and candidate["classCode"] == item["classCode"] and candidate["action"] == side
                      and candidate["tradeDateTime"] == trade["tradeDateTime"]
                      and str(candidate["price"]) == str(trade.get("averagePrice"))), None)
        if match:
            event["source_event_id"] = match["id"]
            matched_ids.add(match["id"])
        message = f"Пульс · {profile} · {item['ticker']} · {'Покупка' if side == 'buy' else 'Продажа'} · {event['price']}. {event['trade']}. {event.get('reason', '')}"
        if settings.get("real_mode", "off") == "off":
            message += " Реальной заявки нет."
        notify(event, settings, message, urgent=event.get("real_status") == "blocked")
        add_event(event)


def handle_poll_error(browser: PulseBrowser, error: Exception, profile_url: str) -> bool:
    message = str(error) if isinstance(error, PulseError) else f"Ошибка браузера ({type(error).__name__})"
    login_needed = isinstance(error, PulseError) and any(
        marker in message for marker in ("Сделки не загрузились", "HTTP 401", "HTTP 403", "Заверши вход")
    )
    if "Сделки не загрузились" in message and not browser.headless:
        try:
            if browser.visible_trades_page(profile_url):
                message = "Вход в Пульс виден, но список сделок не ответил. Повторяем загрузку автоматически"
        except Exception:
            pass
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
    month_scan = None
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
            month_scan = None
            last_profile = profile
            next_poll = 0
            next_reconnect = 0
            reconnect_delay = 3.0
            with LOCK:
                MONTH.update(status="idle", items=[], processed=0, total=0, message="", loaded_at=None)
                if AUTH["status"] == "authenticated":
                    MONITOR.update(status="checking", message="Загружаем сделки выбранного профиля")
                    MONITOR.update(profile="", instrument_count=0, instruments=[])
                    TODAY.clear()
                else:
                    AUTH.update(status="checking", message="Проверяем сохранённый вход в Пульс")

        try:
            request = HISTORY_REQUESTS.get(timeout=0.1 if month_scan else 0.5)
        except queue.Empty:
            request = None
        if request:
            try:
                if request["action"] in {"auth_start", "show"}:
                    if browser is None or browser.headless or not browser.recover_page():
                        previous_browser, browser = browser, None
                        if previous_browser:
                            previous_browser.close()
                        new_browser = PulseBrowser(DATA_DIR / "pulse-browser", headless=False)
                        new_browser.open(settings["profile_url"])
                        browser = new_browser
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
                elif request["action"] == "month" and browser and AUTH["status"] == "authenticated":
                    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                    with LOCK:
                        targets = month_targets(MONITOR["instruments"], cutoff)
                        MONTH.update(status="loading", items=[], processed=0, total=len(targets), skipped=[],
                                     message="Читаем историю за 30 дней", loaded_at=None)
                    month_scan = {"profile": operations_url(settings["profile_url"])[0], "cutoff": cutoff,
                                  "targets": targets, "index": 0, "found": [], "cursor": None,
                                  "seen_cursors": set(), "occurrences": {}, "pages": 0, "skipped": []}
                elif request["action"] == "month":
                    with LOCK:
                        MONTH.update(status="error", message="Сначала подключи Пульс")
                else:
                    request["error"] = "Сначала авторизуйся в Пульсе"
            except Exception as error:
                if request["action"] == "history":
                    request["error"] = str(error) if isinstance(error, PulseError) else "История не загрузилась"
                elif request["action"] == "month":
                    month_scan = None
                    with LOCK:
                        MONTH.update(status="error", message=str(error) if isinstance(error, PulseError) else "История за месяц не загрузилась")
                else:
                    with LOCK:
                        detail = str(error).splitlines()[0][:160] or type(error).__name__
                        AUTH.update(status="required", message=str(error) if isinstance(error, PulseError) else f"Окно Пульса не открылось: {detail}")
            finally:
                if "ready" in request:
                    request["ready"].set()

        if browser is None and AUTH["status"] == "checking" and not (DATA_DIR / "pulse-browser").exists():
            with LOCK:
                AUTH.update(status="required", message="Первое подключение: нажми «Войти в Т-Банк»")

        if browser is None and AUTH["status"] in {"checking", "authenticated"} and time.monotonic() >= next_reconnect:
            try:
                browser = PulseBrowser(DATA_DIR / "pulse-browser", headless=True)
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
                if not browser.recover_page():
                    raise PulseError("Окно Пульса закрыто")
                browser.page.wait_for_timeout(250)
                if time.monotonic() >= next_ui_probe:
                    if browser.return_to_trades_after_login(settings["profile_url"]):
                        with LOCK:
                            AUTH.update(status="waiting", message="Вход завершён, открываем сделки автора")
                    if browser.visible_trades_page(settings["profile_url"]):
                        # The bank may show trades before its data API responds. Preserve
                        # the browser login now, but confirm access only after a snapshot.
                        browser.save_session()
                    if browser.nickname_url and browser.instrument_urls:
                        browser.resolve_target()
                    if browser.list_url:
                        browser.save_session()
                        with LOCK:
                            AUTH["message"] = "Сделки найдены, загружаем данные профиля"
                        next_poll = 0
                    next_ui_probe = time.monotonic() + 3
            except Exception as error:
                try:
                    recovered = browser.recover_page()
                except Exception:
                    recovered = False
                if not recovered:
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

        if month_scan and browser and AUTH["status"] == "authenticated":
            try:
                if advance_month_scan(browser, month_scan):
                    month_scan = None
            except Exception as error:
                month_scan = None
                with LOCK:
                    MONTH.update(status="error", items=[], message=str(error) if isinstance(error, PulseError)
                                 else "История за месяц не загрузилась; попробуй снова")
        elif month_scan:
            month_scan = None
            with LOCK:
                MONTH.update(status="error", items=[], message="Пульс отключился во время загрузки; попробуй снова")


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
        elif self.path == "/api/health":
            self.respond(200, {"status": "ok", "server_mode": server_mode()})
        elif self.path == "/api/state":
            with LOCK:
                authenticated = AUTH["status"] == "authenticated"
                self.respond(200, {"settings": load_settings(), "token_configured": bool(telegram_token()),
                                   "events": EVENTS, "demo_positions": POSITIONS,
                                   "broker_token_configured": bool(broker_token()), "broker": BROKER.copy(),
                                   "trade_token_configured": bool(trade_token()), "trading": TRADING.copy(),
                                   "real_orders": list(REAL_ORDERS.values())[-30:], "real_order_count": len(REAL_ORDERS),
                                   "monitor": MONITOR if authenticated else {"status": "stopped", "message": "Ожидаем входа в Пульс", "last_check": None, "profile": "", "instrument_count": 0, "instruments": []},
                                   "auth": AUTH, "server_mode": server_mode(),
                                   "browser_ui_url": "/desktop/vnc.html?autoconnect=1&resize=scale&path=desktop/websockify&view_only=false" if server_mode() else None,
                                   "today": TODAY if authenticated else [],
                                   "month": MONTH.copy() if authenticated else {"status": "idle", "items": [], "processed": 0, "total": 0, "message": "", "loaded_at": None}})
        else:
            self.respond(404, {"error": "Не найдено"})

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        expected = public_origin() or f"http://127.0.0.1:{self.server.server_port}"
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
            if not authenticated and self.path not in {"/api/auth/start", "/api/browser/show", "/api/auth/check", "/api/settings", "/api/auto-copy", "/api/demo", "/api/demo/scenario", "/api/demo/reset", "/api/telegram/test", "/api/broker/connect", "/api/broker/select", "/api/broker/refresh", "/api/trade/connect", "/api/trade/select", "/api/trade/preview", "/api/trade/submit", "/api/trade/reconcile"}:
                self.respond(403, {"error": "Сначала авторизуйся в Пульсе"})
                return
            if self.path == "/api/settings":
                self.respond(200, {"settings": save_settings(data)})
            elif self.path == "/api/auto-copy":
                enabled = data.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Укажи состояние автокопирования")
                settings = load_settings()
                settings["auto_demo_buy"] = enabled
                if enabled:
                    settings["monitoring_enabled"] = True
                self.respond(200, {"settings": save_settings(settings)})
            elif self.path == "/api/month":
                with LOCK:
                    if not MONITOR["instruments"] or MONITOR.get("profile", "").casefold() != operations_url(load_settings()["profile_url"])[0].casefold():
                        raise ValueError("Сначала дождись списка инструментов Пульса")
                    if MONTH["status"] != "loading":
                        MONTH.update(status="loading", items=[], processed=0, total=0,
                                     message="Готовим историю за 30 дней", loaded_at=None)
                        HISTORY_REQUESTS.put({"action": "month"})
                self.respond(202, {"ok": True})
            elif self.path == "/api/broker/connect":
                candidate = data.get("token", "")
                if not isinstance(candidate, str):
                    raise ValueError("Некорректный токен T-Invest")
                connect_broker(candidate)
                self.respond(200, {"broker": refresh_broker()})
            elif self.path == "/api/broker/select":
                account_id = data.get("account_id")
                if not isinstance(account_id, str):
                    raise ValueError("Выбери счёт из списка")
                self.respond(200, {"broker": select_broker_account(account_id)})
            elif self.path == "/api/broker/refresh":
                self.respond(200, {"broker": refresh_broker()})
            elif self.path == "/api/trade/connect":
                candidate = data.get("token", "")
                if not isinstance(candidate, str):
                    raise ValueError("Некорректный торговый токен")
                self.respond(200, {"trading": connect_trading(candidate)})
            elif self.path == "/api/trade/select":
                account_id = data.get("account_id")
                if not isinstance(account_id, str):
                    raise ValueError("Выбери торговый счёт")
                self.respond(200, {"trading": select_trade_account(account_id)})
            elif self.path == "/api/trade/preview":
                source_id = data.get("source_id")
                if not isinstance(source_id, str) or len(source_id) > 200:
                    raise ValueError("Некорректный сигнал")
                self.respond(200, {"preview": create_real_preview(source_id)})
            elif self.path == "/api/trade/submit":
                preview_id = data.get("preview_id")
                if not isinstance(preview_id, str) or len(preview_id) > 40:
                    raise ValueError("Некорректное подтверждение")
                self.respond(200, {"order": confirm_real_preview(preview_id)})
            elif self.path == "/api/trade/reconcile":
                reconcile_real_orders()
                self.respond(200, {"ok": True})
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
            elif self.path == "/api/demo/scenario":
                name = data.get("scenario")
                if not isinstance(name, str) or name not in SCENARIOS:
                    raise ValueError("Неизвестный демо-сценарий")
                signal = SCENARIOS[name]
                settings = load_settings()
                event = {
                    "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "profile": "Демо", "instrument": signal["ticker"], "instrument_name": signal["name"],
                    "side": signal["side"], "price": f"{signal.get('price') or 'ГО ' + str(signal.get('margin_rub'))} ₽",
                    "source": "scenario", "scenario": name,
                }
                apply_demo(event, signal, settings)
                notify(event, settings, f"ДЕМО · {signal['name']} · {event['trade']} · {event['reason']}. Реальной заявки нет.")
                add_event(event)
                self.respond(200, {"event": event})
            elif self.path == "/api/demo/reset":
                with LOCK:
                    write_state(POSITIONS_PATH, {})
                    POSITIONS.clear()
                    EVENTS[:] = [event for event in EVENTS if event.get("source") not in {"scenario", "manual_demo"}]
                    write_state(EVENTS_PATH, EVENTS)
                self.respond(200, {"ok": True})
            elif self.path == "/api/telegram/test":
                settings = load_settings()
                event = {"time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                         "profile": "Демо", "instrument": "TELEGRAM", "side": "test", "price": "—",
                         "trade": "проверка уведомления", "source": "telegram_test",
                         "recipient_id": settings["chat_id"]}
                notify(event, settings, "ДЕМО · Проверка уведомлений T-Invest Bot. Реальной заявки нет.")
                add_event(event)
                self.respond(200, {"event": event})
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
                        "source": "historic_demo", "source_event_id": candidate_id,
                    }
                    apply_demo(event, {"ticker": candidate["ticker"], "classCode": candidate["classCode"],
                                       "name": candidate["name"], "asset_type": candidate.get("asset_type") or "stock",
                                       "side": "buy", "price": candidate["price"], "currency": candidate["currency"],
                                       "lot_size": 1}, load_settings())
                    event["assumption"] = "Условный лот 1 единица; реальный размер лота не проверен"
                    add_event(event)
                settings = load_settings()
                notify(event, settings, f"ДЕМО · {event['profile']} · {event['instrument']} · {event['trade']} по {event['price']}. {event['reason']}. Реальной заявки нет.")
                with LOCK:
                    write_state(EVENTS_PATH, EVENTS)
                self.respond(200, {"event": event})
            else:
                self.respond(404, {"error": "Не найдено"})
        except BrokerError as error:
            with LOCK:
                BROKER.update(status="error", message=str(error))
            self.respond(503, {"error": str(error)})
        except TradeError as error:
            self.respond(400, {"error": str(error)})
        except (ValueError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    public_origin()  # Reject malformed server configuration before opening a port.
    os.umask(0o077)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = LocalHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        raise SystemExit(f"Админка уже запущена на порту {args.port} или порт занят") from error
    threading.Thread(target=monitor_loop, name="pulse-monitor", daemon=True).start()
    threading.Thread(target=reconcile_loop, name="broker-reconcile", daemon=True).start()
    threading.Thread(target=broker_monitor_loop, name="broker-portfolio", daemon=True).start()
    print(f"Админка: http://127.0.0.1:{server.server_port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
