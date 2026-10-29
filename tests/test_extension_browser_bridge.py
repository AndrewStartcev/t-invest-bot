"""Integration: a paired browser is the only Pulse data source for a client."""
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import demo_admin
from extension_source import ExtensionStore
from pulse_auth_monitor import PulseAuthStore

PROFILE = "https://www.tbank.ru/invest/social/profile/LinMath/"


class BrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trades = ExtensionStore(self.tmp.name)
        self.auth = PulseAuthStore(self.tmp.name)
        self.key = self.trades.pairing_key()

    def test_browser_snapshots_show_in_client_cabinet_without_server_browser(self):
        with patch.object(demo_admin, "EXTENSION_TRADES", self.trades), \
             patch.object(demo_admin, "EXTENSION_SOURCE", self.auth), \
             patch.object(demo_admin, "load_settings", return_value={**demo_admin.DEFAULTS, "profile_url":PROFILE}), \
             patch.object(demo_admin, "notify"), \
             patch.object(demo_admin, "add_event") as record, \
             patch.dict(os.environ, {"TINVEST_BACKEND_KEY":"", "TINVEST_MULTIUSER":"0"}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), demo_admin.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def report(data, key=None):
                req = Request(base + "/api/extension/report",
                    data=json.dumps(data).encode("utf-8"),
                    headers={"Content-Type":"application/json",
                             "X-TInvest-Extension-Key":key or self.key,
                             "X-TInvest-Extension-Client":"admin"})
                with urlopen(req,timeout=5) as result:
                    return json.load(result)
            try:
                with self.assertRaises(HTTPError) as unauth:
                    report({"kind":"config"}, key="wrong")
                self.assertEqual(401, unauth.exception.code)
                config = report({"kind":"config"})
                self.assertEqual(PROFILE, config["profile_url"])
                self.assertEqual({}, config["counts"])
                instruments = [{"ticker":"T"+str(n), "classCode":"TQBR",
                                "showName":"Бумага "+str(n),"totalOperationsCount":4}
                               for n in range(520)]
                first = report({"kind":"trades","status":"ok","profile_url":PROFILE,
                                "instruments":instruments,"events":[]})
                self.assertEqual(0, first["accepted"])
                with urlopen(base+"/api/state",timeout=5) as answer:
                    state=json.load(answer)
                self.assertTrue(state["extension_mode"])
                self.assertEqual("authenticated", state["auth"]["status"])
                self.assertEqual(520, len(state["monitor"]["instruments"]))
                self.assertEqual("T10",state["monitor"]["instruments"][10]["ticker"])
                self.assertEqual([],state["today"])
                event={"ticker":"T10","classCode":"TQBR","action":"buy",
                       "tradeDateTime":"2026-10-29T15:00:00+00:00","averagePrice":"123.45"}
                instruments[10]["totalOperationsCount"]=5
                second = report({"kind":"trades","status":"ok","profile_url":PROFILE,
                                 "instruments":instruments,"events":[event]})
                self.assertEqual(1, second["accepted"])
                self.assertEqual(1, record.call_count)
                repeated = report({"kind":"trades","status":"ok","profile_url":PROFILE,
                                   "instruments":instruments,"events":[event]})
                self.assertEqual(0, repeated["accepted"])
                self.assertEqual(1, record.call_count)
                other={**event,"ticker":"FAKE"}
                with self.assertRaises(HTTPError) as invalid:
                    report({"kind":"trades","status":"ok","profile_url":PROFILE,
                            "instruments":instruments,"events":[other]})
                self.assertEqual(422,invalid.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_new_author_starts_a_new_baseline(self):
        self.trades.accept({"status":"ok","profile_url":PROFILE,
                            "instruments":[{"ticker":"T1","classCode":"TQBR",
                                            "totalOperationsCount":15}],"events":[]},PROFILE)
        other=PROFILE.replace("LinMath","NewAuthor")
        result=self.trades.accept({"status":"test"},other)
        self.assertEqual({}, result["counts"])
        self.trades.accept({"status":"ok","profile_url":other,
                            "instruments":[{"ticker":"T1","classCode":"TQBR",
                                            "totalOperationsCount":100}],"events":[]},other)
        self.assertEqual([],self.trades.public_snapshot(other)["recent"])
        self.assertEqual([],self.trades.public_snapshot(PROFILE)["instruments"])


if __name__ == "__main__":
    unittest.main()
