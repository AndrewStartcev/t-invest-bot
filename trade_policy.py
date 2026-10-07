"""Opt-in requirements, separate from existing monetary limits."""
from decimal import Decimal, InvalidOperation

DEFAULT_POLICY = {"enabled": False, "position_cap_enabled": False, "position_percent": 10,
                  "confirm_existing": True, "confirm_low_profit": True, "min_profit_percent": "0.15",
                  "yield_unit": "unknown", "short_simulation": False, "telegram_controls": False}


def validate_policy(value):
    if not isinstance(value, dict):
        raise ValueError("Некорректные правила подтверждения")
    result = {**DEFAULT_POLICY, **{key: value[key] for key in DEFAULT_POLICY if key in value}}
    for key in ("enabled", "position_cap_enabled", "confirm_existing", "confirm_low_profit", "short_simulation", "telegram_controls"):
        if type(result[key]) is not bool:
            raise ValueError("Некорректный переключатель торгового правила")
    if type(result["position_percent"]) is not int or not 1 <= result["position_percent"] <= 100:
        raise ValueError("Максимальная доля позиции: от 1 до 100%")
    try:
        profit = Decimal(str(result["min_profit_percent"]))
    except InvalidOperation:
        raise ValueError("Некорректный порог прибыли") from None
    if not profit.is_finite() or not 0 <= profit <= 100:
        raise ValueError("Порог прибыли: от 0 до 100%")
    result["min_profit_percent"] = str(profit)
    if result["yield_unit"] not in {"unknown", "percent", "fraction"}:
        raise ValueError("Неизвестные единицы доходности Пульса")
    return result


def approval_reasons(signal, policy):
    if not policy["enabled"]:
        return []
    reasons = []
    position = signal.get("investor_position")
    # Missing/hidden positions never imply an absent short position.
    if signal["side"] == "buy" and policy["confirm_existing"]:
        if not isinstance(position, dict) or position.get("status") != "verified":
            reasons.append("Позиция инвестора неизвестна; покупка может закрывать короткую позицию")
        elif position.get("present") is not False:
            detail = f"; видимая доля {position['percent']}%" if position.get("percent") is not None else ""
            reasons.append("Актив уже есть в портфеле инвестора" + detail)
    if policy["confirm_low_profit"]:
        # BUY can close a short: only a verified empty position is an ordinary opening.
        closing = (signal["side"] == "sell" or not isinstance(position, dict)
                   or position.get("status") != "verified" or position.get("present") is not False)
        if closing:
            try:
                profit = Decimal(str(signal.get("relative_yield")))
                if not profit.is_finite() or policy["yield_unit"] == "unknown":
                    raise InvalidOperation()
                if policy["yield_unit"] == "fraction":
                    profit *= 100
                if profit < Decimal(policy["min_profit_percent"]):
                    reasons.append(f"Прибыль инвестора {profit}% ниже {policy['min_profit_percent']}%")
            except (InvalidOperation, ValueError, TypeError):
                reasons.append("Прибыль закрытия / единицы доходности Пульса не подтверждены")
    return reasons


def plan_identity(plan):
    return tuple(plan.get(key) for key in ("account_id", "instrument_uid", "side", "quantity", "price",
                                          "estimated_rub", "price_type", "position_effect"))
