"""Explicit paper-only short cycle. Never calls the broker or changes its margin permissions."""
from decimal import Decimal, InvalidOperation

from demo_engine import simulate, sale_quantity


def simulate_with_short(signal, rules, positions, enabled=False):
    key = signal.get("ticker", "") + ":" + signal.get("classCode", "")
    current = positions.get(key, {})
    held = current.get("quantity", 0)
    if type(held) is not int:
        raise ValueError("Повреждено количество условной позиции")
    if (not enabled and held >= 0) or (held >= 0 and not (signal["side"] == "sell" and held == 0)):
        return simulate(signal, rules, positions)
    asset = signal.get("asset_type")
    result = {"status": "blocked", "reason": "", "quantity": 0, "amount_rub": "0.00", "asset_type": asset}
    updated = dict(positions)
    try:
        unit = Decimal(str(signal.get("margin_rub") if asset == "future" else signal.get("price")))
        lot = signal.get("lot_size")
        if asset != "future":
            if str(signal.get("currency", "")).lower() not in {"rub", "rubles", "₽"} or type(lot) is not int or lot < 1:
                raise ValueError("Нет рублёвой стоимости условного лота")
            unit *= lot
        if not unit.is_finite() or unit <= 0:
            raise ValueError("Нет стоимости лота / ГО для условной короткой позиции")
        if asset not in {"stock", "bond", "fund", "future"}:
            raise ValueError("Класс инструмента не поддерживается")
        if signal["side"] == "buy":
            quantity = sale_quantity(asset, rules, -held, -held, unit)
            remaining = held + quantity
            if remaining:
                updated[key] = {**current, "quantity": remaining}
            else:
                updated.pop(key, None)
            result["reason"] = "Условная покупка для закрытия короткой позиции"
        else:
            if held < 0:
                raise ValueError("Повторное увеличение условной короткой позиции заблокировано")
            cap = rules[asset]
            if asset == "future":
                quantity = min(cap["max_contracts"], int(Decimal(cap["margin_budget"]) // unit))
            else:
                if unit > cap["single_lot_cap"]:
                    raise ValueError("Лот выше лимита условной короткой позиции")
                quantity = min(max(1, int(Decimal(cap["budget"]) // unit)), int(Decimal(cap["position_cap"]) // unit))
            if quantity < 1:
                raise ValueError("Лимит условной короткой позиции исчерпан")
            sale = rules["sales"][asset]
            sale_budget = sale["margin_budget"] if asset == "future" else sale["budget"]
            sale_lot_cap = sale_budget if asset == "future" else sale["single_lot_cap"]
            if unit > sale_lot_cap:
                raise ValueError("Лот / ГО выше лимита продажи")
            quantity = min(quantity, int(Decimal(sale_budget) // unit))
            if asset == "future":
                quantity = min(quantity, sale["max_contracts"])
            if quantity < 1:
                raise ValueError("Бюджет продажи не позволяет открыть условную короткую позицию")
            updated[key] = {"quantity": -quantity, "asset_type": asset, "name": signal.get("name", key),
                            "entry_price": str(signal.get("price", "")), "margin_rub": str(unit), "short": True}
            result["reason"] = "Условная продажа для открытия короткой позиции"
        result.update(status="executed", quantity=quantity, amount_rub=f"{unit * quantity:.2f}")
    except (ValueError, InvalidOperation, TypeError) as error:
        result["reason"] = str(error)
    return result, updated
