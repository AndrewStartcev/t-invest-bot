"""Legacy manual trade buttons work when Pulse data is supplied by the extension."""
import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

import demo_admin
from extension_source import ExtensionStore

PROFILE="https://www.tbank.ru/invest/social/profile/LinMath/"


class ManualBrowserTradingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source=ExtensionStore(temporary.name)
        self.settings=copy.deepcopy(demo_admin.DEFAULTS)
        self.settings.update(profile_url=PROFILE,monitoring_enabled=True)
        self.when=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
        self.trade={"ticker":"BSPB","classCode":"TQBR","action":"buy",
                    "tradeDateTime":self.when,"averagePrice":"124"}
        self.source.accept({"status":"ok","profile_url":PROFILE,"events":[],
            "instruments":[{"ticker":"BSPB","classCode":"TQBR","showName":"БСПБ",
                            "type":"stock","totalOperationsCount":2,
                            "maxTradeDateTime":self.when}]},PROFILE)
        self.source.accept_history({"kind":"history","profile_url":PROFILE,
            "ticker":"BSPB","classCode":"TQBR","events":[self.trade]},PROFILE)

    def test_historical_ids_are_stable_and_eligible_for_manual_copy(self):
        with patch.object(demo_admin,"EXTENSION_TRADES",self.source), \
             patch.object(demo_admin,"load_settings",return_value=self.settings):
            first=demo_admin.extension_dashboard()
            second=demo_admin.extension_dashboard()
            self.assertEqual(1,len(first["today"]))
            row=first["today"][0]
            self.assertTrue(row["id"].startswith("extension:"))
            self.assertEqual(row["id"],second["today"][0]["id"])
            self.assertTrue(row["can_demo_buy"])
            self.assertEqual("stock",row["asset_type"])
            signal,key,_=demo_admin.real_signal(row["id"])
            self.assertEqual("browser-historic:"+row["id"],key)
            self.assertEqual("BSPB",signal["ticker"])

    def test_existing_manual_real_preview_and_submit_via_browser_session(self):
        self.settings["real_mode"]="confirm"
        plan={"account_id":"test-account","instrument_uid":"test-uid","ticker":"BSPB",
              "quantity":1,"side":"buy","lot_size":1,"price":"124",
              "estimated_rub":"124","price_type":"PRICE_TYPE_LIMIT",
              "position_effect":"increase"}
        with patch.object(demo_admin,"EXTENSION_TRADES",self.source), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"prepare_real",return_value=(plan,"stub-token")) as prepare, \
             patch.object(demo_admin,"place_real",return_value={"status":"submitted"}) as place, \
             patch.object(demo_admin,"notify"), patch.object(demo_admin,"add_event"):
            row=demo_admin.extension_dashboard()["today"][0]
            preview=demo_admin.create_real_preview(row["id"])
            self.assertEqual(plan,preview["plan"])
            self.assertEqual("submitted",demo_admin.confirm_real_preview(preview["id"])["status"])
            self.assertEqual(2,prepare.call_count)
            place.assert_called_once()
            self.assertEqual("browser-historic:"+row["id"],place.call_args.args[1])

    def test_existing_demo_button_works_without_server_playwright_auth(self):
        with patch.object(demo_admin,"EXTENSION_TRADES",self.source), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"apply_demo") as demo, \
             patch.object(demo_admin,"notify"), patch.object(demo_admin,"add_event"), \
             patch.object(demo_admin,"write_state"), \
             patch.dict(os.environ,{"TINVEST_BACKEND_KEY":"","TINVEST_MULTIUSER":"0"}):
            def paper(event,signal,settings):
                event.update(trade="ДЕМО: куплено",reason="Проверка",demo_status="executed")
            demo.side_effect=paper
            row=demo_admin.extension_dashboard()["today"][0]
            server=ThreadingHTTPServer(("127.0.0.1",0),demo_admin.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                request=Request(f"http://127.0.0.1:{server.server_port}/api/demo/buy",
                    data=json.dumps({"id":row["id"]}).encode(),
                    headers={"Content-Type":"application/json"})
                with urlopen(request,timeout=5) as response:
                    result=json.load(response)
                self.assertEqual("executed",result["event"]["demo_status"])
                demo.assert_called_once()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__=="__main__":
    unittest.main()
