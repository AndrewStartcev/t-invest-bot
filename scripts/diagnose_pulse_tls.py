"""Verify real Chromium TLS without saved bank cookies or trading credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulse_live import browser_executable
from playwright.sync_api import sync_playwright


def main():
    try:
        with sync_playwright() as playwright:
            executable = browser_executable()
            options = {"headless": True}
            if executable:
                options["executable_path"] = executable
            browser = playwright.chromium.launch(**options)
            try:
                context = browser.new_context(ignore_https_errors=False)
                page = context.new_page()
                response = page.goto("https://www.tbank.ru/invest/social/profile/LinMath/",
                                     wait_until="commit", timeout=30000)
                print("Chromium: TLS www.tbank.ru подтверждён; HTTP", response.status if response else "—")
                print("Проверка выполнена без банковских cookies, токенов и заявок.")
            finally:
                browser.close()
    except Exception as error:
        detail = "ERR_CERT_AUTHORITY_INVALID" if "ERR_CERT_AUTHORITY_INVALID" in str(error) else type(error).__name__
        raise SystemExit("Chromium: проверка соединения не прошла (" + detail + ")") from None


if __name__ == "__main__":
    main()
