"""Conservative real-order preparation and T-Invest REST calls.

Only explicit submit_order() can send a brokerage order. Plans use a fresh
order book, account positions, broker limits and configured monetary caps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from broker_read import BASE, BrokerError, money

METHODS = {
    "accounts": "UsersService/GetAccounts",
    "instrument": "InstrumentsService/GetInstrumentBy",
    "book": "MarketDataService/GetOrderBook",
    "margin": "InstrumentsService/GetFuturesMargin",
    "positions": "OperationsService/GetPositions",
    "order_price": "OrdersService/GetOrderPrice",
    "max_lots": "OrdersService/GetMaxLots",
    "order_state": "OrdersService/GetOrderState",
    "post_order": "OrdersService/PostOrder",
}
KINDS = {"stock": "share", "bond": "bond", "fund": "etf", "future": "futures"}


class TradeError(Exception):
    pass


def api_call(method: str, token: str, payload: dict, opener=urlopen) -> dict:
    if method not in METHODS:
        raise TradeError("Неизвестный метод T-Invest API")
    if not token or any(char.isspace() for char in token):
        raise TradeError("Торговый токен не задан")
    request = Request(BASE + METHODS[method], json.dumps(payload).encode("utf-8"), {
        "Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=12) as response:
            result = json.load(response)
    except HTTPError as error:
        if error.code in (401, 403):
            raise TradeError("Торговый токен отклонён или нет доступа к счёту") from None
        if error.code == 429:
            raise TradeError("T-Invest ограничил частоту запросов") from None
        raise TradeError(f"T-Invest отклонил запрос: HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise TradeError("Нет ответа от T-Invest API; статус заявки требует сверки") from None
    except (ValueError, UnicodeError):
        raise TradeError("Некорректный ответ T-Invest API; статус заявки требует сверки") from None
    if not isinstance(result, dict):
        raise TradeError("Некорректный ответ T-Invest API")
    return result


def full_access_accounts(token: str, call=api_call) -> list[dict]:
    items = call("accounts", token, {}).get("accounts")
    if not isinstance(items, list):
        raise TradeError("Не удалось получить счета для торговли")
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("accessLevel") not in ("ACCOUNT_ACCESS_LEVEL_FULL_ACCESS", 1):
            continue
        if item.get("status") not in ("ACCOUNT_STATUS_OPEN", 2):
            continue
        if item.get("type") not in ("ACCOUNT_TYPE_TINKOFF", "ACCOUNT_TYPE_TINKOFF_IIS", 1, 2):
            continue
        account_id = item.get("id")
        if isinstance(account_id, str) and account_id:
            result.append({"id": account_id, "name": str(item.get("name") or "Брокерский счёт")})
    if not result:
        raise TradeError("Нужен торговый токен с полным доступом к открытому брокерскому счёту")
    return result


def positive(value: object, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TradeError(f"Не удалось определить {name}") from None
    if not amount.is_finite() or amount <= 0:
        raise TradeError(f"Не удалось определить {name}")
    return amount


def quotation(value: Decimal) -> dict:
    if value <= 0 or value.as_tuple().exponent < -9:
        raise TradeError("Некорректная цена заявки")
    units = int(value)
    nano = int((value - units) * 1_000_000_000)
    return {"units": str(units), "nano": nano}


def _book_price(book: dict, side: str) -> Decimal:
    stamp = book.get("orderbookTs")
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - moment).total_seconds()
    except (AttributeError, TypeError, ValueError):
        raise TradeError("Стакан не содержит времени обновления") from None
    if not 0 <= age <= 30:
        raise TradeError("Котировка устарела; заявка не отправлена")
    levels = book.get("asks" if side == "buy" else "bids")
    if not isinstance(levels, list) or not levels or not isinstance(levels[0], dict):
        raise TradeError("Нет встречных заявок в стакане")
    return positive(money(levels[0].get("price")), "актуальную цену")


def _positions(data: dict, kind: str, uid: str, account_id: str) -> tuple[Decimal, int]:
    if data.get("accountId") and data["accountId"] != account_id:
        raise TradeError("Ответ позиций относится к другому счёту")
    if data.get("limitsLoadingInProgress"):
        raise TradeError("Брокер ещё загружает лимиты счёта")
    if not isinstance(data.get("money"), list):
        raise TradeError("Не удалось прочитать доступные деньги счёта")
    cash = sum((money(item) for item in data.get("money", [])
                if isinstance(item, dict) and str(item.get("currency", "")).lower() == "rub"), Decimal(0))
    section = "futures" if kind == "future" else "securities"
    items = data.get(section, [])
    if not isinstance(items, list):
        raise TradeError("Не удалось прочитать позиции счёта")
    try:
        available = sum(int(item.get("balance", 0)) for item in items
                        if isinstance(item, dict) and item.get("instrumentUid") == uid)
    except (ValueError, TypeError):
        raise TradeError("Брокер вернул некорректную позицию") from None
    return cash, available


def prepare_order(signal: dict, rules: dict, token: str, account_id: str,
                  copied_lots: int, call=api_call) -> dict:
    side, kind = signal.get("side"), signal.get("asset_type")
    if side not in {"buy", "sell"} or kind not in KINDS:
        raise TradeError("Направление или класс инструмента не поддерживается")
    ticker, code = signal.get("ticker"), signal.get("classCode")
    if not isinstance(ticker, str) or not isinstance(code, str) or not ticker or not code:
        raise TradeError("Нет точного тикера и класса инструмента")
    instrument = call("instrument", token, {"idType": "INSTRUMENT_ID_TYPE_TICKER",
                                            "id": ticker, "classCode": code}).get("instrument")
    if not isinstance(instrument, dict) or instrument.get("ticker") != ticker or instrument.get("classCode") != code:
        raise TradeError("Инструмент Пульса не совпал с инструментом брокера")
    if instrument.get("instrumentType") != KINDS[kind]:
        raise TradeError("Класс инструмента Пульса не совпал с брокером")
    uid = instrument.get("uid")
    lot = instrument.get("lot")
    if not isinstance(uid, str) or not uid or type(lot) is not int or lot < 1:
        raise TradeError("Брокер не вернул UID или размер лота")
    if str(instrument.get("currency", "")).lower() != "rub":
        raise TradeError("Реальная торговля разрешена только в рублях")
    if instrument.get("apiTradeAvailableFlag") is not True or instrument.get("otcFlag") is True:
        raise TradeError("Инструмент недоступен для биржевой торговли через API")
    if instrument.get("buyAvailableFlag" if side == "buy" else "sellAvailableFlag") is not True:
        raise TradeError("Покупка или продажа инструмента недоступна")
    tick = positive(money(instrument.get("minPriceIncrement")), "минимальный шаг цены")
    book = call("book", token, {"instrumentId": uid, "depth": 1})
    if book.get("instrumentUid") and book["instrumentUid"] != uid:
        raise TradeError("Стакан относится к другому инструменту")
    if book.get("priceCurrency") and str(book["priceCurrency"]).lower() != "rub":
        raise TradeError("Котировка не в рублях")
    price = _book_price(book, side)
    if price % tick:
        raise TradeError("Цена в стакане не кратна шагу инструмента")
    price_type = "PRICE_TYPE_POINT" if kind in {"bond", "future"} else "PRICE_TYPE_CURRENCY"
    quote = quotation(price)
    positions = call("positions", token, {"accountId": account_id})
    cash, available_units = _positions(positions, kind, uid, account_id)
    if type(copied_lots) is not int or copied_lots < 0:
        raise TradeError("Повреждён учёт скопированных позиций")
    # No price: broker computes own-money limits from the current book. Bond
    # GetMaxLots expects currency while its order book is quoted in points.
    limits = call("max_lots", token, {"accountId": account_id, "instrumentId": uid})
    if limits.get("currency") and str(limits["currency"]).lower() != "rub":
        raise TradeError("Лимиты брокера получены не в рублях")
    def limit(section: str, field: str) -> int:
        view = limits.get(section)
        if not isinstance(view, dict):
            raise TradeError("Брокер не вернул лимит доступных лотов")
        try:
            value = int(view[field])
        except (KeyError, TypeError, ValueError):
            raise TradeError("Брокер вернул некорректный лимит лотов") from None
        if value < 0:
            raise TradeError("Брокер вернул некорректный лимит лотов")
        return value
    if side == "sell":
        own_lots = max(0, available_units // lot)
        broker_lots = limit("sellLimits", "sellMaxLots")
        quantity = min(copied_lots, own_lots, broker_lots)
        if quantity < 1:
            raise TradeError("Нет доступной позиции, купленной этим ботом, для продажи")
        percent = rules["sell_percent"]
        quantity = min(quantity, max(1, copied_lots * percent // 100))
        amount = None
    else:
        broker_lots = limit("buyLimits", "buyMaxLots")
        if broker_lots < 1 or cash <= 0:
            raise TradeError("Недостаточно собственных средств для покупки")
        if kind == "future":
            margin_data = call("margin", token, {"instrumentId": uid})
            margin_value = margin_data.get("initialMarginOnBuy")
            if not isinstance(margin_value, dict) or str(margin_value.get("currency", "")).lower() != "rub":
                raise TradeError("Нет рублёвого гарантийного обеспечения")
            unit_cost = positive(money(margin_value), "гарантийное обеспечение")
            cap = rules["future"]["margin_budget"]
            if unit_cost > cap:
                raise TradeError("ГО одного фьючерса выше лимита")
            quantity = min(broker_lots, rules["future"]["max_contracts"] - copied_lots,
                           int(Decimal(cap) // unit_cost), int(cash // unit_cost))
        else:
            preview = call("order_price", token, {"accountId": account_id, "instrumentId": uid,
                                                  "price": quote, "direction": "ORDER_DIRECTION_BUY", "quantity": "1"})
            amount_value = preview.get("totalOrderAmount")
            if not isinstance(amount_value, dict) or str(amount_value.get("currency", "")).lower() != "rub":
                raise TradeError("Брокер не вернул рублёвую стоимость покупки")
            unit_cost = positive(money(amount_value), "стоимость одного лота")
            cap = rules[kind]
            if unit_cost > cap["single_lot_cap"]:
                raise TradeError("Цена лота выше установленного лимита")
            budget_lots = max(1, int(Decimal(cap["budget"]) // unit_cost))
            held_lots = max(0, available_units // lot, copied_lots)
            position_lots = int((Decimal(cap["position_cap"]) - unit_cost * held_lots) // unit_cost)
            quantity = min(broker_lots, budget_lots, position_lots, int(cash // unit_cost))
        if quantity < 1:
            raise TradeError("Недостаточно денег или исчерпан лимит позиции")
        amount = str(unit_cost * quantity)
    return {"account_id": account_id, "ticker": ticker, "class_code": code, "instrument_uid": uid,
            "asset_type": kind, "side": side, "lot_size": lot, "quantity": quantity,
            "price": str(price), "price_type": price_type, "estimated_rub": amount,
            "cash_rub": str(cash), "book_time": book["orderbookTs"]}


def submit_order(plan: dict, token: str, request_id: str, call=api_call) -> dict:
    payload = {"accountId": plan["account_id"], "instrumentId": plan["instrument_uid"],
               "quantity": str(plan["quantity"]), "price": quotation(Decimal(plan["price"])),
               "direction": "ORDER_DIRECTION_BUY" if plan["side"] == "buy" else "ORDER_DIRECTION_SELL",
               "orderType": "ORDER_TYPE_LIMIT", "orderId": request_id,
               "priceType": plan["price_type"], "confirmMarginTrade": False}
    return call("post_order", token, payload)


def order_state(token: str, account_id: str, request_id: str, price_type: str = "PRICE_TYPE_UNSPECIFIED", call=api_call) -> dict:
    return call("order_state", token, {"accountId": account_id, "orderId": request_id,
                                       "orderIdType": "ORDER_ID_TYPE_REQUEST", "priceType": price_type})
