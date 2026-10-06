"""Private, read-only RPC to the single owner-operated Pulse browser."""
import http.client
import json
import os
from urllib.parse import urlparse

from pulse_live import PulseError, canonical_profile_url, operations_url


class RemotePulse:
    def __init__(self, profile_url):
        target = urlparse(os.environ["TINVEST_SOURCE_RPC_URL"])
        if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port or target.path:
            raise ValueError("Некорректный адрес внутреннего источника")
        self.port = target.port
        self.key = os.environ["TINVEST_SOURCE_RPC_KEY"]
        self.profile_url = canonical_profile_url(profile_url)

    def call(self, action, **values):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            body = json.dumps({"action": action, "profile_url": self.profile_url, **values}).encode()
            connection.request("POST", "/_source/read", body, {
                "Content-Type": "application/json", "X-TInvest-Backend-Key": self.key})
            response = connection.getresponse()
            data = json.loads(response.read(8 * 1024 * 1024))
            if response.status != 200:
                raise PulseError(data.get("error", "Общий источник временно недоступен"))
            return data
        except (OSError, ValueError, http.client.HTTPException):
            raise PulseError("Общий источник временно недоступен; повторим проверку") from None
        finally:
            connection.close()

    def snapshot(self, profile_url, counts):
        self.profile_url = canonical_profile_url(profile_url)
        data = self.call("snapshot", counts=counts)
        return data["profile"], data["instruments"]

    def history(self, ticker, class_code, cursor=None):
        return self.call("history", ticker=ticker, class_code=class_code, cursor=cursor)

    def portfolio(self, profile_url, instruments=None):
        self.profile_url = canonical_profile_url(profile_url)
        return self.call("portfolio")


def read_source(browser, request):
    profile_url = canonical_profile_url(request["profile_url"])
    profile, _ = operations_url(profile_url)
    if browser.profile_name.casefold() != profile.casefold():
        browser.refresh(profile_url)
    if request["action"] == "snapshot":
        counts = request.get("counts", {})
        if not isinstance(counts, dict) or len(counts) > 3000 or any(
                not isinstance(key, str) or len(key) > 260 or type(value) is not int or value < 0
                for key, value in counts.items()):
            raise PulseError("Некорректные счётчики источника")
        name, instruments = browser.snapshot(profile_url, counts)
        return {"profile": name, "instruments": instruments}
    if request["action"] == "portfolio":
        return browser.portfolio(profile_url)
    if request["action"] == "history":
        if not browser.list_url:
            browser.snapshot(profile_url, {})
        cursor = request.get("cursor")
        if cursor is not None and (type(cursor) not in (str, int) or len(str(cursor)) > 256):
            raise PulseError("Некорректный курсор истории")
        return browser.history(request.get("ticker"), request.get("class_code"), cursor)
    raise PulseError("Неизвестное действие источника")
