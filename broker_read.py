"""Read-only T-Invest REST access. No order methods are exposed here."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
METHODS = {
    "accounts": "UsersService/GetAccounts",
    "positions": "OperationsService/GetPositions",
    "portfolio": "OperationsService/GetPortfolio",
}


class BrokerError(Exception):
    pass


def call(method: str, token: str, payload: dict, opener=urlopen) -> dict:
    if method not in METHODS:
        raise BrokerError("Этот метод API недоступен")
    if not token or any(char.isspace() for char in token):
        raise BrokerError("Укажи корректный токен T-Invest")
    request = Request(BASE + METHODS[method], json.dumps(payload).encode("utf-8"), {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with opener(request, timeout=12) as response:
            data = json.load(response)
    except HTTPError as error:
        if error.code in (401, 403):
            raise BrokerError("Токен отклонён T-Invest. Проверь его срок и доступ к счёту") from None
        if error.code == 429:
            raise BrokerError("T-Invest ограничил частоту запросов. Повтори позже") from None
        raise BrokerError(f"T-Invest API вернул ошибку HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise BrokerError("Нет связи с T-Invest API. Проверь интернет и повтори") from None
    except (ValueError, UnicodeError) as error:
        raise BrokerError("T-Invest API вернул некорректный ответ") from None
    if not isinstance(data, dict):
        raise BrokerError("T-Invest API вернул некорректный ответ")
    return data


def read_only_accounts(token: str, opener=urlopen) -> list[dict]:
    response = call("accounts", token, {}, opener)
    raw = response.get("accounts")
    if not isinstance(raw, list):
        raise BrokerError("Не удалось получить список брокерских счетов")
    if any(isinstance(item, dict) and item.get("accessLevel") in ("ACCOUNT_ACCESS_LEVEL_FULL_ACCESS", 1)
           for item in raw):
        raise BrokerError("Нужен токен только для чтения: полный доступ к счёту запрещён")
    accounts = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        access = item.get("accessLevel")
        if access not in ("ACCOUNT_ACCESS_LEVEL_READ_ONLY", 2):
            continue
        account_id = item.get("id")
        if isinstance(account_id, str) and account_id:
            accounts.append({"id": account_id, "name": str(item.get("name") or "Брокерский счёт"),
                             "type": str(item.get("type") or ""), "status": str(item.get("status") or "")})
    if not accounts:
        raise BrokerError("Нужен токен только для чтения с доступом к брокерскому счёту")
    return accounts


def money(value: dict | None) -> Decimal:
    if not isinstance(value, dict):
        return Decimal(0)
    try:
        result = Decimal(str(value.get("units", 0))) + Decimal(str(value.get("nano", 0))) / Decimal(1_000_000_000)
        return result if result.is_finite() else Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def account_snapshot(token: str, account_id: str, opener=urlopen) -> dict:
    positions = call("positions", token, {"accountId": account_id}, opener)
    portfolio = call("portfolio", token, {"accountId": account_id, "currency": "RUB"}, opener)
    if not isinstance(positions.get("money", []), list):
        raise BrokerError("Не удалось прочитать позиции счёта")
    cash = sum((money(item) for item in positions.get("money", []) if isinstance(item, dict) and item.get("currency", "").lower() == "rub"), Decimal(0))
    assets = []
    for kind in ("securities", "futures", "options"):
        for item in positions.get(kind, []):
            if not isinstance(item, dict):
                continue
            try:
                quantity = int(item.get("balance", 0))
                blocked = int(item.get("blocked", 0))
            except (ValueError, TypeError):
                continue
            if quantity or blocked:
                assets.append({"ticker": str(item.get("ticker") or item.get("instrumentUid") or "—"),
                               "instrument_uid": str(item.get("instrumentUid") or ""),
                               "class_code": str(item.get("classCode") or ""), "kind": kind,
                               "available": quantity, "blocked": blocked})
    total = portfolio.get("totalAmountPortfolio")
    total_rub = money(total) if isinstance(total, dict) and total.get("currency", "").lower() == "rub" else None
    portfolio_items = portfolio.get("positions", [])
    if not isinstance(portfolio_items, list):
        raise BrokerError("Не удалось прочитать состав портфеля")
    by_uid = {item.get("instrumentUid"): item for item in portfolio_items
              if isinstance(item, dict) and item.get("instrumentUid")}
    for asset in assets:
        position = by_uid.get(asset["instrument_uid"])
        # GetPositions normally returns a UID, but test/legacy responses can include a ticker.
        if position is None:
            position = next((item for item in portfolio_items if isinstance(item, dict)
                             and item.get("ticker") == asset["ticker"]
                             and (not asset["class_code"] or item.get("classCode") == asset["class_code"])), None)
        value = None
        if position:
            asset["ticker"] = str(position.get("ticker") or asset["ticker"])
            asset["instrument_uid"] = str(position.get("instrumentUid") or "")
            asset["class_code"] = str(position.get("classCode") or asset["class_code"])
            price = position.get("currentPrice")
            nkd = position.get("currentNkd")
            # Futures are derivatives: price × quantity is not their share of NAV.
            # Foreign-currency instruments require a conversion rate; don't invent one.
            if (asset["kind"] == "securities" and isinstance(price, dict)
                    and price.get("currency", "").lower() == "rub"
                    and (not nkd or isinstance(nkd, dict) and nkd.get("currency", "").lower() == "rub")
                    and isinstance(position.get("quantity"), dict)):
                value = (money(price) + money(nkd)) * money(position["quantity"])
                if not value.is_finite():
                    value = None
        asset["value_rub"] = str(value.quantize(Decimal("0.01"))) if value is not None else None
        asset["weight_percent"] = (str((value / total_rub * 100).quantize(Decimal("0.01")))
                                   if value is not None and total_rub is not None and total_rub.is_finite() and total_rub > 0 else None)
    return {"cash_rub": str(cash), "total_rub": str(total_rub) if total_rub is not None else None,
            "positions": assets, "limits_loading": bool(positions.get("limitsLoadingInProgress"))}
