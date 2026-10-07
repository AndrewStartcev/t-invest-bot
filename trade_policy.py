"""Opt-in requirements, separate from existing monetary limits."""
from decimal import Decimal, InvalidOperation

DEFAULT_POLICY = {"enabled": False, "position_cap_enabled": False, "position_percent": 10,
                  "confirm_existing": True, "confirm_remaining": True, "confirm_low_profit": True, "min_profit_percent": "0.15",
                  "yield_unit": "unknown", "short_simulation": False, "telegram_controls": False}


def validate_policy(value):
    if not isinstance(value, dict):
        raise ValueError("Некорректные правила подтверждения")
    result = {**DEFAULT_POLICY, **{key: value[key] for key in DEFAULT_POLICY if key in value}}
    for key in ("enabled", "position_cap_enabled", "confirm_existing", "confirm_remaining", "confirm_low_profit", "short_simulation", "telegram_controls"):
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
    # The client's displayed-zero exception does not prove zero actual holdings.
    if signal["side"] == "buy" and policy["confirm_existing"]:
        if not isinstance(position, dict) or position.get("status") != "verified":
            reasons.append("Портфель автора не прочитан или актив скрыт; требуется подтверждение покупки")
        elif position.get("present") is not False:
            detail = f"; видимая доля {position['percent']}%" if position.get("percent") is not None else ""
            reasons.append("Актив уже есть в портфеле инвестора" + detail)
    if signal["side"] == "sell" and policy.get("confirm_remaining", True):
        if not isinstance(position, dict) or position.get("status") != "verified":
            reasons.append("Остаток позиции автора после продажи неизвестен; требуется подтверждение")
        elif position.get("present") is not False:
            if position.get("weight_change") == "decreased":
                detail = f"; доля снизилась с {position['previous_percent']}% до {position['percent']}%"
            else:
                detail = "; уменьшение доли не подтверждено"
            reasons.append("После продажи актив остался в портфеле автора" + detail)
    if policy["confirm_low_profit"]:
        # Apply the threshold to both BUY and SELL; never guess a number's units.
        try:
            profit = Decimal(str(signal.get("relative_yield")))
            unit = signal.get("yield_unit", policy["yield_unit"])
            if not profit.is_finite() or unit not in {"percent", "fraction"}:
                raise InvalidOperation()
            if unit == "fraction":
                profit *= 100
            if profit < Decimal(policy["min_profit_percent"]):
                reasons.append(f"Прибыль инвестора {profit}% ниже {policy['min_profit_percent']}%")
        except (InvalidOperation, ValueError, TypeError):
            reasons.append("Доходность сделки автора не удалось подтвердить; требуется решение оператора")
    return reasons


def plan_identity(plan):
    return tuple(plan.get(key) for key in ("account_id", "instrument_uid", "side", "quantity", "price",
                                          "estimated_rub", "price_type", "position_effect"))
