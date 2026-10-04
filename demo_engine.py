"""Deterministic paper trading rules. No broker API or real orders."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


DEFAULT_RULES = {
    "stock": {"budget": 5000, "single_lot_cap": 15000, "position_cap": 30000},
    "bond": {"budget": 10000, "single_lot_cap": 10000, "position_cap": 30000},
    "fund": {"budget": 5000, "single_lot_cap": 10000, "position_cap": 30000},
    "future": {"margin_budget": 10000, "max_contracts": 2},
    "sell_percent": 100,
}


def validated_rules(value: object) -> dict:
    """Validate all fields together; unknown or fractional limits are rejected."""
    if not isinstance(value, dict):
        raise ValueError("Некорректные торговые лимиты")
    result = {}
    for asset in ("stock", "bond", "fund"):
        section = value.get(asset)
        if not isinstance(section, dict):
            raise ValueError(f"Укажи лимиты для {asset}")
        result[asset] = {}
        for key in ("budget", "single_lot_cap", "position_cap"):
            number = section.get(key)
            if type(number) is not int or not 1 <= number <= 10_000_000:
                raise ValueError(f"Лимит {asset}.{key} должен быть целым числом от 1 до 10 000 000")
            result[asset][key] = number
        if section["position_cap"] < section["single_lot_cap"]:
            raise ValueError(f"Предел позиции {asset} меньше цены одного лота")
    future = value.get("future")
    if not isinstance(future, dict):
        raise ValueError("Укажи лимиты для фьючерсов")
    margin = future.get("margin_budget")
    contracts = future.get("max_contracts")
    if type(margin) is not int or not 1 <= margin <= 10_000_000:
        raise ValueError("Лимит обеспечения должен быть целым числом от 1 до 10 000 000")
    if type(contracts) is not int or not 1 <= contracts <= 100:
        raise ValueError("Количество фьючерсов должно быть от 1 до 100")
    result["future"] = {"margin_budget": margin, "max_contracts": contracts}
    percent = value.get("sell_percent")
    if type(percent) is not int or not 1 <= percent <= 100:
        raise ValueError("Доля продажи должна быть от 1 до 100%")
    result["sell_percent"] = percent
    return result


def asset_type(source_type: str, class_code: str) -> str | None:
    source = (source_type or "").lower()
    code = (class_code or "").upper()
    if source in {"stock", "share", "shares"} or code in {"TQBR", "TQBS"}:
        return "stock"
    if source in {"bond", "bonds"} or code in {"TQOB", "TQCB"}:
        return "bond"
    if source in {"fund", "etf"} or code in {"TQTF", "TQIF"}:
        return "fund"
    if source in {"future", "futures"} or code == "SPBFUT":
        return "future"
    return None


def _money(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(0)
    return result if result.is_finite() and result > 0 else Decimal(0)


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def simulate(signal: dict, rules: dict, positions: dict) -> tuple[dict, dict]:
    """Return a paper result and updated positions. A blocked signal never changes positions."""
    asset = signal.get("asset_type")
    side = signal.get("side")
    ticker = signal.get("ticker", "")
    code = signal.get("classCode", "")
    key = f"{ticker}:{code}"
    current = positions.get(key, {})
    held = current.get("quantity", 0)
    if type(held) is not int or held < 0:
        raise ValueError("Повреждено состояние демо-портфеля")
    result = {"status": "blocked", "reason": "", "quantity": 0, "amount_rub": "0.00", "asset_type": asset}
    updated = dict(positions)

    def block(message: str) -> tuple[dict, dict]:
        result["reason"] = message
        return result, updated

    if asset not in {"stock", "bond", "fund", "future"}:
        return block("Класс инструмента не определён")
    if side not in {"buy", "sell"}:
        return block("Направление сделки не определено")
    if side == "sell":
        if held == 0:
            return block("Нет своей демо-позиции — продажа пропущена")
        percent = rules["sell_percent"]
        quantity = held if percent == 100 else held * percent // 100
        if quantity < 1:
            return block("Доля продажи меньше одного лота/контракта")
        result.update(status="executed", reason=f"Закрыто {percent}% своей демо-позиции", quantity=quantity)
        remaining = held - quantity
        if remaining:
            updated[key] = {**current, "quantity": remaining}
        else:
            updated.pop(key, None)
        price = _money(signal.get("price"))
        lot = signal.get("lot_size", 1)
        if asset != "future" and type(lot) is int and lot > 0 and price:
            result["amount_rub"] = _fmt(price * lot * quantity)
        elif asset == "future":
            result["amount_rub"] = _fmt(_money(current.get("margin_rub")) * quantity)
        return result, updated

    if asset == "future":
        margin = _money(signal.get("margin_rub"))
        if not margin:
            return block("Нет данных о гарантийном обеспечении фьючерса")
        limit = rules["future"]
        if margin > limit["margin_budget"]:
            return block("Обеспечение одного контракта выше лимита")
        used_margin = _money(current.get("margin_rub", margin)) * held
        available = min(limit["max_contracts"] - held,
                        int((Decimal(limit["margin_budget"]) - used_margin) // margin))
        if available < 1:
            return block("Лимит обеспечения или количества контрактов исчерпан")
        quantity = available
        amount = margin * quantity
    else:
        price = _money(signal.get("price"))
        lot = signal.get("lot_size")
        if not price or type(lot) is not int or lot < 1:
            return block("Нет цены или размера лота для расчёта")
        if str(signal.get("currency") or "").lower() not in {"rub", "rubles", "₽"}:
            return block("Цена не в рублях")
        cost = price * lot
        limit = rules[asset]
        if cost > limit["single_lot_cap"]:
            return block("Цена одного лота выше лимита")
        by_budget = int(Decimal(limit["budget"]) // cost)
        quantity = max(1, by_budget)  # One expensive lot is allowed up to single_lot_cap.
        by_position = int((Decimal(limit["position_cap"]) - price * lot * held) // cost)
        quantity = min(quantity, by_position)
        if quantity < 1:
            return block("Превышен лимит своей позиции")
        amount = cost * quantity
    result.update(status="executed", reason="Тестовая операция по заданным лимитам", quantity=quantity,
                  amount_rub=_fmt(amount))
    updated[key] = {"quantity": held + quantity, "asset_type": asset,
                    "name": signal.get("name") or ticker, "ticker": ticker, "classCode": code}
    if asset == "future":
        updated[key]["margin_rub"] = _fmt(margin)
    return result, updated
