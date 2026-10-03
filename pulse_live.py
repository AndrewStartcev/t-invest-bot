"""Read visible Pulse operations through an operator-authenticated browser session."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse


class PulseError(Exception):
    pass


def is_tbank_cookie(cookie: dict) -> bool:
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    return domain == "tbank.ru" or domain.endswith(".tbank.ru")


def browser_executable() -> str | None:
    configured = os.environ.get("PULSE_BROWSER_PATH")
    candidates = [configured] if configured else [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def operations_url(profile_url: str) -> tuple[str, str]:
    parsed = urlparse(profile_url)
    if parsed.scheme != "https" or parsed.hostname not in {"tbank.ru", "www.tbank.ru"}:
        raise PulseError("Укажи HTTPS-ссылку на профиль tbank.ru")
    match = re.fullmatch(r"/invest/(?:social|pulse)/profile/([^/]+)(?:/operations)?/?", parsed.path)
    if not match:
        raise PulseError("Нужна ссылка вида /invest/social/profile/Имя/")
    name = match.group(1)
    return name, f"https://www.tbank.ru/invest/pulse/profile/{quote(name)}/operations/"


def is_operations_page(page_url: str, profile_url: str) -> bool:
    name, _ = operations_url(profile_url)
    parsed = urlparse(page_url)
    match = re.fullmatch(r"/invest/(?:social|pulse)/profile/([^/]+)/operations/?", parsed.path)
    return (parsed.hostname in {"tbank.ru", "www.tbank.ru"} and match is not None
            and unquote(match.group(1)).casefold() == unquote(name).casefold())


def with_cursor(url: str, cursor: str | int) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "nextCursor"]
    query.append(("nextCursor", cursor))
    return urlunparse(parsed._replace(query=urlencode(query)))


def without_cursor(url: str) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "nextCursor"]
    return urlunparse(parsed._replace(query=urlencode(query)))


def payload(response) -> dict:
    if not response["ok"]:
        raise PulseError(f"Пульс вернул HTTP {response['status']}; проверь вход в браузере")
    data = response["data"]
    result = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise PulseError("Пульс изменил формат ответа или требуется повторный вход")
    return result


class PulseBrowser:
    def __init__(self, profile_dir: Path, *, headless: bool = False):
        self.profile_dir = profile_dir
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None
        self.list_url = None
        self.nickname_url = None
        self.instrument_urls = {}
        self.target_profile_id = None
        self.profile_name = None
        self.request_headers = {}
        self.observed_api = set()

    def open(self, profile_url: str) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise PulseError("Установи зависимости: python -m pip install -r requirements.txt") from error
        executable = browser_executable()
        if not executable:
            raise PulseError("Brave, Edge или Chrome не найден. Задай PULSE_BROWSER_PATH")
        self.profile_name, url = operations_url(profile_url)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile_dir), executable_path=executable, headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
            session_path = self.profile_dir / "pulse-session.json"
            if session_path.exists():
                try:
                    saved = json.loads(session_path.read_text(encoding="utf-8"))
                    cookies = [cookie for cookie in saved.get("cookies", [])
                               if isinstance(cookie, dict) and is_tbank_cookie(cookie)]
                    if cookies:
                        self.context.add_cookies(cookies)
                except Exception:
                    pass
            self.context.on("response", self._observe_response)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.goto(url, wait_until="commit", timeout=45000)
        except Exception:
            self.close()
            raise

    def save_session(self) -> None:
        """Keep session cookies in the ignored local profile for the next browser run."""
        cookies = [cookie for cookie in self.context.cookies() if is_tbank_cookie(cookie)]
        if not cookies:
            return
        session_path = self.profile_dir / "pulse-session.json"
        temporary = session_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")
        os.replace(temporary, session_path)

    def _observe_response(self, response) -> None:
        path = urlparse(response.url).path
        if "/social-api-gateway/" in path:
            self.observed_api.add((tuple(path.rsplit("/", 2)[-2:]), response.status))
        if (re.fullmatch(r"/mybank/api/social-api-gateway/social/v1/profile/nickname/[^/]+", path)
                and unquote(path.rsplit("/", 1)[-1]).casefold() == self.profile_name.casefold()):
            self.nickname_url = response.url
        if re.fullmatch(r"/mybank/api/social-api-gateway/social/v1/profile/[^/]+/instrument", path):
            profile_id = path.rsplit("/", 2)[-2]
            self.instrument_urls[profile_id] = without_cursor(response.url)
            if profile_id == self.target_profile_id:
                self.list_url = self.instrument_urls[profile_id]
            headers = response.request.headers
            self.request_headers = {key: value for key, value in headers.items()
                                    if key.lower() in {"x-app-name", "x-app-version", "x-platform", "referer"}}

    def recover_page(self) -> bool:
        if not self.context:
            return False
        pages = [page for page in self.context.pages if not page.is_closed()]
        if not pages:
            return False
        self.page = next((page for page in reversed(pages) if "/operations/" in page.url.split("?")[0]), pages[-1])
        return True

    def visible_trades_page(self, profile_url: str) -> bool:
        if not self.recover_page() or not is_operations_page(self.page.url, profile_url):
            return False
        try:
            text = self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        return "Сделки" in text and "Создать профиль" not in text

    def _fetch(self, url: str) -> dict:
        if self.page.is_closed() and not self.recover_page():
            raise PulseError("Окно Пульса закрыто")
        headers = {key: value for key, value in self.request_headers.items() if key.lower() != "referer"}
        return self.page.evaluate("""async ({url, headers}) => {
            const response = await fetch(url, {credentials: 'include', headers});
            let data = null;
            try { data = await response.json(); } catch (_) {}
            return {ok: response.ok, status: response.status, data};
        }""", {"url": url, "headers": headers})

    def resolve_target(self) -> bool:
        if self.target_profile_id is None:
            if not self.nickname_url:
                return False
            nickname_response = self._fetch(self.nickname_url)
            target = (nickname_response.get("data") or {}).get("payload") or {}
            if not nickname_response["ok"] or not isinstance(target, dict) or not isinstance(target.get("id"), str):
                raise PulseError("Пульс не вернул профиль автора")
            self.target_profile_id = target["id"]
        self.list_url = self.instrument_urls.get(self.target_profile_id)
        return self.list_url is not None

    def refresh(self, profile_url: str) -> None:
        _, url = operations_url(profile_url)
        if not self.recover_page():
            self.page = self.context.new_page()
        self.list_url = None
        self.nickname_url = None
        self.instrument_urls.clear()
        self.target_profile_id = None
        self.observed_api.clear()
        self.page.goto(url, wait_until="commit", timeout=45000)

    def snapshot(self, profile_url: str, counts: dict[str, int]) -> tuple[str, list[dict]]:
        name, url = operations_url(profile_url)
        if self.page is None:
            self.open(profile_url)
        elif self.page.is_closed() and not self.recover_page():
            raise PulseError("Окно Пульса закрыто. Открой его снова")
        for page in self.context.pages:
            if not page.is_closed() and is_operations_page(page.url, profile_url):
                self.page = page
                break
        if not is_operations_page(self.page.url, profile_url):
            raise PulseError("Заверши вход в Т-Банк и вернись на страницу «Сделки» в окне мониторинга")
        deadline = time.monotonic() + 15
        while (not self.nickname_url or not self.instrument_urls) and time.monotonic() < deadline:
            if self.page.is_closed():
                raise PulseError("Окно Пульса закрыто. Выключи и снова включи мониторинг")
            self.page.wait_for_timeout(250)
        if self.nickname_url:
            self.resolve_target()
        while not self.list_url and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
            self.resolve_target()
        if not self.list_url:
            seen = len(self.observed_api)
            raise PulseError(f"Сделки не загрузились (ответов API Пульса: {seen}). Открой раздел «Сделки» в окне Пульса")
        items = []
        seen = set()
        cursor = None
        list_url = self.list_url
        for _ in range(100):
            response = self._fetch(with_cursor(list_url, cursor) if cursor is not None else list_url)
            page = payload(response)
            items.extend(page["items"])
            cursor = page.get("nextCursor")
            if not page.get("hasNext"):
                break
            if type(cursor) not in (str, int) or cursor == "" or cursor in seen:
                raise PulseError("Не удалось прочитать все страницы списка инструментов")
            seen.add(cursor)
        else:
            raise PulseError("Слишком много страниц списка инструментов")
        if not items:
            raise PulseError("Список инструментов пуст или закрыт настройками профиля")
        instruments = []
        list_path = urlparse(list_url).path
        base = list_url.replace(list_path, list_path.rsplit("/instrument", 1)[0] + "/operation/instrument/{ticker}/{classCode}", 1)
        for item in items:
            ticker, class_code = item.get("ticker"), item.get("classCode")
            stats = item.get("statistics") or {}
            count = stats.get("totalOperationsCount")
            if not isinstance(ticker, str) or not isinstance(class_code, str) or type(count) is not int:
                raise PulseError("Неожиданные поля инструмента в ответе Пульса")
            key = f"{ticker}:{class_code}"
            previous = counts.get(key)
            history = []
            if previous is not None and count > previous:
                history_url = base.replace("{ticker}", quote(ticker, safe="")).replace("{classCode}", quote(class_code, safe=""))
                needed = count - previous
                next_cursor = None
                visited = set()
                while len(history) < needed:
                    page = payload(self._fetch(with_cursor(history_url, next_cursor) if next_cursor else history_url))
                    history.extend(page["items"])
                    if len(history) >= needed:
                        break
                    next_cursor = page.get("nextCursor")
                    if not page.get("hasNext") or type(next_cursor) not in (str, int) or next_cursor in visited:
                        raise PulseError(f"История {ticker} неполная: требуется {needed}, получено {len(history)}")
                    visited.add(next_cursor)
            instruments.append({
                "ticker": ticker, "classCode": class_code, "showName": item.get("showName") or ticker,
                "type": item.get("type") or "", "totalOperationsCount": count,
                "maxTradeDateTime": stats.get("maxTradeDateTime"), "history": history,
            })
        return name, instruments

    def history(self, ticker: str, class_code: str, cursor: str | int | None = None) -> dict:
        if not self.list_url or not re.fullmatch(r"[A-Za-z0-9._-]+", ticker) or not re.fullmatch(r"[A-Za-z0-9._-]+", class_code):
            raise PulseError("История инструмента недоступна")
        parsed = urlparse(self.list_url)
        path = parsed.path.rsplit("/instrument", 1)[0] + f"/operation/instrument/{quote(ticker)}/{quote(class_code)}"
        url = urlunparse(parsed._replace(path=path))
        data = payload(self._fetch(with_cursor(url, cursor) if cursor is not None else url))
        return {"items": [{key: item.get(key) for key in ("tradeDateTime", "action", "currency", "averagePrice")}
                          for item in data["items"]], "hasNext": bool(data.get("hasNext")), "nextCursor": data.get("nextCursor")}

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            try:
                if self.playwright:
                    self.playwright.stop()
            finally:
                self.context = self.playwright = self.page = None
