"""Shared verified HTTPS transport. No automatic retry of brokerage requests."""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."


class APIError(Exception):
    pass


def normalize_token(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Токен должен быть текстом")
    token = value.strip(" \t\r\n\ufeff\u200b\u2060")
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or len(token) > 1000 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("Токен содержит пробелы или посторонние символы. Скопируй API-токен целиком из настроек Т-Инвестиций")
    return token


def http_error_message(error: HTTPError) -> str:
    code = None
    try:
        data = json.loads(error.read(8192))
        candidate = str(data.get("code", "")) if isinstance(data, dict) else ""
        if re.fullmatch(r"[0-9]{1,6}", candidate):
            code = candidate
    except (ValueError, UnicodeError, OSError):
        pass
    suffix = f" (HTTP {error.code}" + (f", код API {code}" if code else "") + ")"
    if code == "40003" or error.code == 401:
        return "Токен неактивен, отозван или неверен. Выпусти новый токен на tbank.ru/invest/settings/api/" + suffix
    if error.code == 403:
        return "T-Invest запретил доступ. Проверь права токена и доступ к счёту; при сохранении ошибки обратись в поддержку банка" + suffix
    if error.code == 429:
        return "T-Invest ограничил частоту запросов. Повтори позже" + suffix
    return "T-Invest отклонил запрос" + suffix


def broker_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    extra_ca = os.environ.get("TINVEST_BROKER_CA_FILE", "")
    if extra_ca:
        try:
            context.load_verify_locations(cafile=extra_ca)
        except (OSError, ssl.SSLError):
            raise APIError("Не удалось загрузить сертификаты T-Invest из TINVEST_BROKER_CA_FILE. Запусти scripts/install_broker_ca.sh на сервере") from None
    return context


def network_error_message(error: Exception) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        code = getattr(reason, "verify_code", None)
        suffix = f" (код проверки {code})" if isinstance(code, int) else ""
        return "Ошибка проверки TLS-сертификата T-Invest" + suffix + ". Для API на .ru нужны сертификаты НУЦ Минцифры: запусти scripts/install_broker_ca.sh. Также проверь дату сервера; проверка TLS остаётся включённой"
    if isinstance(reason, socket.gaierror):
        return "DNS не разрешает invest-public-api.tbank.ru. Проверь DNS сервера"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "T-Invest не ответил за 12 секунд. Проверь исходящий HTTPS, прокси и доступность API"
    return "Нет связи с T-Invest API. Проверь исходящий порт 443, сеть и настройки прокси"


def request_json(method: str, token: str, payload: dict, opener=urlopen) -> dict:
    try:
        token = normalize_token(token)
    except ValueError as error:
        raise APIError(str(error)) from None
    request = Request(BASE + method, json.dumps(payload).encode("utf-8"), {
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "t-invest-bot/1.0",
    }, method="POST")
    try:
        options = {"timeout": 12}
        if os.environ.get("TINVEST_BROKER_CA_FILE"):
            options["context"] = broker_tls_context()
        with opener(request, **options) as response:
            result = json.load(response)
    except HTTPError as error:
        raise APIError(http_error_message(error)) from None
    except (URLError, TimeoutError, OSError) as error:
        raise APIError(network_error_message(error)) from None
    except (ValueError, UnicodeError):
        raise APIError("T-Invest вернул некорректный JSON-ответ") from None
    if not isinstance(result, dict):
        raise APIError("T-Invest вернул некорректный JSON-ответ")
    return result
