"""Browser-fed author portfolio is isolated and parsed with existing policy."""
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
from extension_portfolio import ExtensionPortfolioStore
from extension_source import ExtensionStore
from pulse_auth_monitor import PulseAuthStore

PROFILE="https://www.tbank.ru/invest/social/profile/LinMath/"
INSTRUMENTS=[
    {"ticker":"ROSN","classCode":"TQBR","showName":"Роснефть",
     "type":"stock","totalOperationsCount":10},
    {"ticker":"GAZP","classCode":"TQBR","showName":"Газпром",
     "type":"stock","totalOperationsCount":10}
]


class ExtensionPortfolioTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.trades=ExtensionStore(tmp.name)
        self.auth=PulseAuthStore(tmp.name)
        self.portfolio=ExtensionPortfolioStore(tmp.name)
        self.key=self.trades.pairing_key()

    def screen(self,percent="0,02%"):
        return {"url":PROFILE, "heading":"LinMath. Портфель",
                "portfolio":True,"rows":[{
                    "text":"Роснефть "+percent,
                    "links":["https://www.tbank.ru/invest/stocks/ROSN/"]}]}

    def test_strict_portfolio_validates_author_and_preserves_unknowns(self):
        report={"kind":"portfolio","profile_url":PROFILE,"screen":self.screen()}
        with self.assertRaisesRegex(ValueError,"Сначала"):
            # Caller must provide an authenticated instrument snapshot; route
            # enforces this, while store never invents the instrument list.
            if self.trades.public_snapshot(PROFILE)["last_status"]!="ok":
                raise ValueError("Сначала нужен снимок автора")
        self.trades.accept({"status":"ok","profile_url":PROFILE,
                            "instruments":INSTRUMENTS,"events":[]},PROFILE)
        result=self.portfolio.accept(report,PROFILE,INSTRUMENTS)
        self.assertEqual("partial",result["status"])
        snapshot=self.portfolio.public_snapshot(PROFILE)
        self.assertTrue(snapshot["positions"]["ROSN:TQBR"]["present"])
        self.assertEqual("0.02",snapshot["positions"]["ROSN:TQBR"]["percent"])
        self.assertNotIn("GAZP:TQBR",snapshot["positions"],
                         "Unknown security must not be treated as absent")
        self.assertNotIn("links",str(snapshot["rows"]))
        self.assertNotIn("sessionId",str(snapshot))
        other=PROFILE.replace("LinMath","SomeoneElse")
        self.assertEqual("unavailable",self.portfolio.public_snapshot(other)["status"])
        with self.assertRaisesRegex(ValueError,"другому автору"):
            self.portfolio.accept({**report,"profile_url":other},PROFILE,INSTRUMENTS)
        with self.assertRaisesRegex(ValueError,"подтвердить"):
            self.portfolio.accept({**report,"screen":{**self.screen(),"heading":"SomeoneElse. Портфель"}},
                                  PROFILE,INSTRUMENTS)

    def test_repeated_portfolio_records_percent_change(self):
        self.portfolio.accept({"kind":"portfolio","profile_url":PROFILE,
                               "screen":self.screen("10%")},PROFILE,INSTRUMENTS)
        self.portfolio.accept({"kind":"portfolio","profile_url":PROFILE,
                               "screen":self.screen("5%")},PROFILE,INSTRUMENTS)
        item=self.portfolio.public_snapshot(PROFILE)["positions"]["ROSN:TQBR"]
        self.assertEqual("decreased",item["weight_change"])
        self.assertEqual("10",item["previous_percent"])

    def test_authenticated_api_reports_portfolio_in_client_dashboard(self):
        self.trades.accept({"status":"ok","profile_url":PROFILE,
                            "instruments":INSTRUMENTS,"events":[]},PROFILE)
        with patch.object(demo_admin,"EXTENSION_TRADES",self.trades), \
             patch.object(demo_admin,"EXTENSION_SOURCE",self.auth), \
             patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio), \
             patch.object(demo_admin,"load_settings",
                          return_value={**demo_admin.DEFAULTS,"profile_url":PROFILE}), \
             patch.dict(os.environ,{"TINVEST_BACKEND_KEY":"","TINVEST_MULTIUSER":"0"}):
            server=ThreadingHTTPServer(("127.0.0.1",0),demo_admin.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            try:
                payload={"kind":"portfolio","profile_url":PROFILE,"screen":self.screen()}
                def request(key):
                    return Request(base+"/api/extension/report",data=json.dumps(payload).encode(),
                                   headers={"Content-Type":"application/json",
                                            "X-TInvest-Extension-Key":key})
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(request("bad"),timeout=5)
                self.assertEqual(401,unauthorized.exception.code)
                with urlopen(request(self.key),timeout=5) as response:
                    self.assertEqual(1,json.load(response)["positions"])
                with urlopen(base+"/api/state",timeout=5) as response:
                    data=json.load(response)
                self.assertEqual("partial",data["investor_portfolio"]["status"])
                self.assertEqual("0.02",data["investor_portfolio"]["positions"]["ROSN:TQBR"]["percent"])
                self.assertEqual(0,data["real_order_count"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__=="__main__":
    unittest.main()
