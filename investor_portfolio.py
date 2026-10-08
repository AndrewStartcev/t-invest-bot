"""Read only visible holdings; missing or aggregated rows never prove absence."""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlparse

from pulse_live import PULSE_PROFILE_HOSTS, operations_url
from demo_engine import asset_type


def unavailable(profile_url, message="Портфель автора не удалось прочитать; требуется подтверждение"):
    return {"status": "unavailable", "profile_url": profile_url, "checked_at": datetime.now(timezone.utc).isoformat(),
            "message": message, "positions": {}, "rows": []}


def normalize_name(value):
    return re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip().casefold()


def parse_screen(screen, profile_url, instruments):
    result = unavailable(profile_url)
    name, _ = operations_url(profile_url)
    page = urlparse(str(screen.get("url", "")))
    match = re.fullmatch(r"/invest/(?:social|pulse)/profile/([^/]+)(?:/portfolio|/operations)?/?", page.path)
    heading = normalize_name(screen.get("heading", ""))
    named_heading = re.fullmatch(re.escape(normalize_name(unquote(name))) + r"[.\s:—–-]+портфель", heading)
    split_heading = heading == "портфель" and normalize_name(screen.get("author", "")) == normalize_name(unquote(name))
    if (page.hostname not in PULSE_PROFILE_HOSTS or not match
            or unquote(match.group(1)).casefold() != unquote(name).casefold()
            or not (named_heading or split_heading)
            or screen.get("portfolio") is not True):
        return result
    rows = screen.get("rows")
    if not isinstance(rows, list) or len(rows) > 3000:
        return result
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text", ""))
        percentages = re.findall(r"(?<![\d.,])(-?\d+(?:[.,]\d+)?)\s*%", text)
        # Never mistake price changes / several percentages for portfolio allocation.
        if len(percentages) != 1:
            continue
        try:
            percent = Decimal(percentages[0].replace(",", "."))
        except InvalidOperation:
            continue
        if not percent.is_finite() or abs(percent) > 100:
            continue
        label = normalize_name(re.sub(r"-?\d+(?:[.,]\d+)?\s*%", "", text))
        if not label or label in {"другое", "прочее", "валюта и металлы"}:
            continue
        links = []
        for href in (row.get("links") if isinstance(row.get("links"), list) else [])[:30]:
            parsed = urlparse(str(href))
            instrument = re.fullmatch(r"/invest/(stocks|bonds|etfs|futures)/([^/]+)/?", parsed.path)
            if parsed.hostname in PULSE_PROFILE_HOSTS and instrument:
                links.append((unquote(instrument.group(2)), instrument.group(1)))
        clean.append({"name": label, "percent": str(percent), "links": links})
    positions = {}
    classes = {"stock": "stocks", "bond": "bonds", "fund": "etfs", "future": "futures"}
    for instrument in instruments:
        ticker, class_code = instrument.get("ticker"), instrument.get("classCode")
        if not isinstance(ticker, str) or not isinstance(class_code, str):
            continue
        wanted_name = normalize_name(instrument.get("showName", ticker))
        kind = classes.get(asset_type(instrument.get("type", ""), class_code))
        matched = []
        for row in clean:
            exact_link = any(symbol.casefold() == ticker.casefold() and linked_kind == kind
                             for symbol, linked_kind in row["links"])
            # Company's displayed name can establish presence, but never absence of a security.
            exact_name = wanted_name == row["name"] and not row["links"]
            if exact_link or exact_name:
                matched.append((row, exact_link))
        if matched:
            nonzero = any(Decimal(row["percent"]) != 0 for row, _ in matched)
            visible_row = next((row for row, _ in matched if Decimal(row["percent"]) != 0), matched[0][0])
            positions[ticker + ":" + class_code] = {
                "status": "verified", "present": nonzero, "percent": visible_row["percent"],
                "displayed_zero": not nonzero,
                "evidence": "instrument_link" if any(link for _, link in matched) else "portfolio_name",
                "checked_at": result["checked_at"], "profile_url": profile_url}
    if clean:
        result.update(status="partial", positions=positions,
                      rows=[{"name": row["name"], "percent": row["percent"]} for row in clean],
                      message=f"Портфель автора прочитан: {len(clean)} строк. Видимые 0% исключены из проверки наличия; скрытые позиции требуют подтверждения")
    return result


def compare_positions(current, previous):
    """Compare displayed weights only; a weight change is not a quantity change."""
    old_positions = previous.get("positions", {}) if isinstance(previous, dict) and previous.get("profile_url") == current.get("profile_url") else {}
    for key, position in current.get("positions", {}).items():
        position["weight_change"] = "unknown"
        position.pop("previous_percent", None)
        old = old_positions.get(key)
        if not isinstance(old, dict) or old.get("status") != "verified" or position.get("status") != "verified":
            continue
        try:
            before, after = Decimal(str(old.get("percent"))), Decimal(str(position.get("percent")))
            if not before.is_finite() or not after.is_finite():
                continue
        except (InvalidOperation, ValueError, TypeError):
            continue
        position["previous_percent"] = str(before)
        position["weight_change"] = "decreased" if after < before else "increased" if after > before else "unchanged"
    return current


# Runs inside the existing authenticated Playwright context, never in client browsers.
SCREEN_SCRIPT = r"""() => {
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const headings = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
        .filter(e => visible(e) && /портфель/i.test(e.innerText));
    const heading = headings.find(e => /[.\s:—–-]+портфель/i.test(e.innerText)) || headings[0];
    if (!heading) return {url:location.href,portfolio:false,rows:[]};
    let panel = heading.parentElement;
    for (let i=0; panel && i<6; i++,panel=panel.parentElement) {
        if (/компании/i.test(panel.innerText) && /активы/i.test(panel.innerText) && /%/.test(panel.innerText)) break;
    }
    if (!panel || panel===document.body || panel===document.documentElement
        || !/компании/i.test(panel.innerText) || !/активы/i.test(panel.innerText)
        || [...panel.querySelectorAll('[role=tab],button')].some(e=>/^(Посты|Ролики)$/.test(e.innerText.trim())))
        return {url:location.href,portfolio:false,rows:[]};
    const rows=[],seen=new Set();
    for (const node of panel.querySelectorAll('*')) {
        if (!visible(node) || node.children.length || !/%/.test(node.innerText)) continue;
        let row=node.parentElement;
        for (let i=0; row && row!==panel && i<4; i++,row=row.parentElement) {
            const text=row.innerText.trim();
            if (text.length>240 || (text.match(/%/g)||[]).length!==1) break;
            if (/\p{L}/u.test(text) && !seen.has(text)) {
                seen.add(text);rows.push({text,links:[...row.querySelectorAll('a[href]')].map(a=>a.href)});break;
            }
        }
    }
    const nickname=decodeURIComponent(location.pathname.match(/\/profile\/([^/]+)/)?.[1]||'');
    const author=[...document.querySelectorAll('h1,h2,h3,[role="heading"]')].find(e=>visible(e)&&e.innerText.trim().toLocaleLowerCase()===nickname.toLocaleLowerCase());
    return {url:location.href,heading:heading.innerText.trim(),author:author?.innerText.trim()||'',portfolio:true,rows};
}"""


def read_visible_portfolio(browser, profile_url, instruments):
    from pulse_live import canonical_profile_url
    page = browser.context.new_page()
    stage = "открытие профиля"
    try:
        # Open the verified profile route; follow its visible portfolio control, no API URL guessing.
        page.goto(canonical_profile_url(profile_url), wait_until="domcontentloaded", timeout=20000)
        stage = "поиск кнопки портфеля"
        control = page.get_by_text("Портфель", exact=True).filter(visible=True)
        control.first.wait_for(state="visible", timeout=8000)
        if control.count() != 1:
            return unavailable(profile_url, "Неоднозначная кнопка портфеля автора; требуется подтверждение")
        stage = "открытие портфеля"
        control.click(timeout=5000)
        stage = "выбор раздела компаний"
        companies = page.get_by_text("Компании", exact=True).filter(visible=True)
        companies.first.wait_for(state="visible", timeout=8000)
        if companies.count() != 1:
            return unavailable(profile_url, "Неоднозначный раздел компаний портфеля; повторим проверку")
        companies.click(timeout=5000)
        stage = "загрузка долей компаний"
        # Wait for visible allocations, not a fixed delay.
        page.wait_for_function("() => [...document.querySelectorAll('h1,h2,h3,[role=heading]')].some(e => /портфель/i.test(e.innerText)) && /\\d+[.,]?\\d*\\s*%/.test(document.body.innerText)", timeout=8000)
        result = parse_screen(page.evaluate(SCREEN_SCRIPT), profile_url, instruments)
        if result["status"] == "unavailable":
            result["message"] = "Пульс открыл портфель, но строки с долями не распознаны; повторим проверку"
        return result
    except Exception:
        # No bank body, URL parameters, cookies or credentials in errors.
        return unavailable(profile_url, "Портфель пока не прочитан: " + stage + "; повторим проверку")
    finally:
        try:
            page.close()
        except Exception:
            pass
