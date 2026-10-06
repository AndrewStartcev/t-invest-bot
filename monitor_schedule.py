"""Client monitoring windows in Moscow time, independent of the server timezone."""
import re
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "МСК")
DEFAULT_SCHEDULE = {"enabled": False, "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "19:00"}


def validate_schedule(value):
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("Некорректное расписание мониторинга")
    days = value.get("days")
    if (not isinstance(days, list) or not days or len(days) > 7
            or any(type(day) is not int or day not in range(7) for day in days)
            or len(set(days)) != len(days)):
        raise ValueError("Выбери дни недели для мониторинга")
    for key in ("start", "end"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]):
            raise ValueError("Время расписания должно быть в формате ЧЧ:ММ")
    if value["start"] == value["end"]:
        raise ValueError("Начало и конец расписания должны отличаться")
    return {"enabled": value["enabled"], "days": sorted(days), "start": value["start"], "end": value["end"]}


def schedule_open(settings, now=None):
    schedule = settings.get("schedule", DEFAULT_SCHEDULE)
    if not schedule["enabled"]:
        return True
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    minute = now.hour * 60 + now.minute
    start, end = [int(schedule[key][:2]) * 60 + int(schedule[key][3:]) for key in ("start", "end")]
    if start < end:
        return now.weekday() in schedule["days"] and start <= minute < end
    # A night window belongs to the day on which it starts.
    return ((now.weekday() in schedule["days"] and minute >= start)
            or ((now.weekday() - 1) % 7 in schedule["days"] and minute < end))
