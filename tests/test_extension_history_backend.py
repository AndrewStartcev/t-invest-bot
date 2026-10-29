"""Historical published Pulse trades come from Brave, never from Playwright.

Verifies resumable progress, client pairing, exact replays, 24h display,
and that loading historic deals never queues brokerage requests or alerts.
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import demo_admin
from extension_source import ExtensionStore
from pulse_auth_monitor import PulseAuthStore

PROFILE = "https://www.tbank.ru/invest/social/profile/LinMath/"


class ExtensionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ExtensionStore(self.temp.name)
        self.auth = PulseAuthStore(self.temp.name)
        self.key = self.store.pairing_key()

    def test_initial_24h_history_returns_to_cabinet_without_trade_alerts(self):
        date = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        trade = {"ticker":"BSPB","classCode":"TQBR","action":"buy",
                 "tradeDateTime":date,"averagePrice":"100"}
        instruments = [
            {"ticker":"BSPB","classCode":"TQBR","totalOperationsCount":4,
             "maxTradeDateTime":date},
            {"ticker":"SBER","classCode":"TQBR","totalOperationsCount":2,
             "maxTradeDateTime":date}
        ]
        with patch.object(demo_admin,"EXTENSION_TRADES",self.store), \
             patch.object(demo_admin,"EXTENSION_SOURCE",self.auth), \
             patch.object(demo_admin,"load_settings",
                          return_value={**demo_admin.DEFAULTS,"profile_url":PROFILE}), \
             patch.object(demo_admin,"notify") as notify, \
             patch.object(demo_admin,"add_event") as add_event, \
             patch.dict(os.environ,{"TINVEST_BACKEND_KEY":"","TINVEST_MULTIUSER":"0"}):
            server = ThreadingHTTPServer(("127.0.0.1",0),demo_admin.Handler)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def post(report,key=None):
                request=Request(base+"/api/extension/report",
                    data=json.dumps(report).encode("utf-8"),
                    headers={"Content-Type":"application/json",
                             "X-TInvest-Extension-Key":key or self.key,
                             "X-TInvest-Extension-Client":"admin"})
                with urlopen(request,timeout=5) as response:
                    return json.load(response)
            def state():
                with urlopen(base+"/api/state",timeout=5) as response:
                    return json.load(response)
            try:
                post({"kind":"trades","status":"ok","profile_url":PROFILE,
                      "instruments":instruments,"events":[]})
                self.assertEqual(0,len(state()["today"]))
                self.assertEqual(0,state()["history_progress"]["completed"])
                historic = {"kind":"history","profile_url":PROFILE,
                    "ticker":"BSPB","classCode":"TQBR","events":[trade,trade]}
                with self.assertRaises(HTTPError) as denied:
                    post(historic,key="invalid-key")
                self.assertEqual(401,denied.exception.code)
                self.assertEqual(2,post(historic)["accepted"])
                self.assertEqual(0,post(historic)["accepted"])
                result=state()
                self.assertEqual(2,len(result["today"]))
                self.assertEqual(2,result["history_progress"]["target_total"])
                self.assertEqual(1,result["history_progress"]["completed"])
                self.assertEqual(2,result["history_progress"]["loaded"])
                self.assertEqual(["BSPB:TQBR"],post({"kind":"config"})["history_done"])
                self.assertEqual(0,add_event.call_count)
                self.assertEqual(0,notify.call_count)
                with self.assertRaises(HTTPError) as rejected:
                    post({**historic,"ticker":"SBER","events":[trade]})
                self.assertEqual(422,rejected.exception.code)
                self.assertEqual(0,add_event.call_count)
                self.assertEqual(0,post({**historic,"ticker":"SBER","events":[]})["accepted"])
                self.assertTrue(state()["history_progress"]["complete"])
                instruments[0]["totalOperationsCount"]=5
                post({"kind":"trades","status":"ok","profile_url":PROFILE,
                      "instruments":instruments,"events":[trade]})
                self.assertEqual(2,len(state()["today"]),
                    "Overlapping live and historic copies must not inflate totals")
                self.assertEqual(1,add_event.call_count)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_old_or_unknown_instrument_never_advances_history(self):
        old=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        self.store.accept({"status":"ok","profile_url":PROFILE,
            "instruments":[{"ticker":"BSPB","classCode":"TQBR",
                            "totalOperationsCount":10}],"events":[]},PROFILE)
        row={"ticker":"BSPB","classCode":"TQBR","action":"buy",
             "tradeDateTime":old,"averagePrice":"100"}
        with self.assertRaisesRegex(ValueError,"вне последних 24 часов"):
            self.store.accept_history({"kind":"history","profile_url":PROFILE,
                "ticker":"BSPB","classCode":"TQBR","events":[row]},PROFILE)
        with self.assertRaisesRegex(ValueError,"отсутствует"):
            self.store.accept_history({"kind":"history","profile_url":PROFILE,
                "ticker":"FAKE","classCode":"TQBR","events":[]},PROFILE)
        self.assertEqual([],self.store.history_progress(PROFILE)["done"])

    def test_month_page_ingestion_is_resumable_and_read_only(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=2)).isoformat()
        older = (now - timedelta(days=12)).isoformat()
        instruments = [{"ticker":"BSPB","classCode":"TQBR",
                        "totalOperationsCount":5,"maxTradeDateTime":recent}]
        self.store.accept({"status":"ok","profile_url":PROFILE,
            "instruments":instruments,"events":[]}, PROFILE)
        first = {"ticker":"BSPB","classCode":"TQBR","action":"buy",
                 "tradeDateTime":older,"averagePrice":"101"}
        second = {**first, "tradeDateTime":recent, "action":"sell"}
        report={"kind":"month_history","profile_url":PROFILE,
                "ticker":"BSPB","classCode":"TQBR",
                "batch_index":0,"events":[second],"complete":False}
        result=self.store.accept_month_history(report,PROFILE)
        self.assertEqual(1,result["accepted"])
        self.assertFalse(result["complete"])
        self.assertEqual({"BSPB:TQBR":1},self.store.month_progress(PROFILE)["offsets"])
        self.assertEqual(0,self.store.month_progress(PROFILE)["completed"])
        self.assertEqual(0,self.store.accept_month_history(report,PROFILE)["accepted"])
        with self.assertRaisesRegex(ValueError,"Пропущена"):
            self.store.accept_month_history({**report,"batch_index":2},PROFILE)
        self.assertEqual(1,self.store.accept_month_history(
            {**report,"batch_index":1,"events":[first],"complete":True},
            PROFILE)["accepted"])
        progress=self.store.month_progress(PROFILE)
        self.assertTrue(progress["complete"])
        self.assertEqual(2,progress["loaded"])
        self.assertEqual(["BSPB:TQBR"],progress["done"])
        self.assertEqual(0,self.store.accept_month_history(
            {**report,"batch_index":1,"events":[first],"complete":True},PROFILE)["accepted"])
        self.assertEqual(2,len(self.store.month_entries(PROFILE)))

    def test_month_history_isolated_by_author_and_time(self):
        now=datetime.now(timezone.utc)
        self.store.accept({"status":"ok","profile_url":PROFILE,"events":[],
            "instruments":[{"ticker":"BSPB","classCode":"TQBR",
                             "totalOperationsCount":5,"maxTradeDateTime":now.isoformat()}]},PROFILE)
        stale=(now-timedelta(days=33)).isoformat()
        sample={"ticker":"BSPB","classCode":"TQBR","action":"buy",
                "tradeDateTime":stale,"averagePrice":"123"}
        packet={"kind":"month_history","profile_url":PROFILE,
                "ticker":"BSPB","classCode":"TQBR",
                "batch_index":0,"events":[sample],"complete":True}
        with self.assertRaisesRegex(ValueError,"вне месячной"):
            self.store.accept_month_history(packet,PROFILE)
        self.assertEqual([],self.store.month_entries(PROFILE))
        with self.assertRaisesRegex(ValueError,"другого автора"):
            self.store.accept_month_history({**packet,"profile_url":
                PROFILE.replace("LinMath","Different")},PROFILE)
        self.store.accept_month_history({**packet,"events":[]},PROFILE)
        new=PROFILE.replace("LinMath","Different")
        self.store.accept({"status":"ok","profile_url":new,"events":[],
            "instruments":[{"ticker":"BSPB","classCode":"TQBR",
                             "totalOperationsCount":5}]},new)
        self.assertEqual([],self.store.month_progress(new)["done"])
        self.assertEqual([],self.store.month_entries(new))

    def test_month_history_visible_in_cabinet_but_not_24h(self):
        now=datetime.now(timezone.utc)
        old=(now-timedelta(days=7)).isoformat()
        self.store.accept({"status":"ok","profile_url":PROFILE,"events":[],
            "instruments":[{"ticker":"BSPB","classCode":"TQBR",
                             "totalOperationsCount":5,"maxTradeDateTime":old}]},PROFILE)
        packet={"kind":"month_history","profile_url":PROFILE,
                "ticker":"BSPB","classCode":"TQBR","batch_index":0,"complete":True,
                "events":[{"ticker":"BSPB","classCode":"TQBR","action":"buy",
                            "tradeDateTime":old,"averagePrice":"100"}]}
        with patch.object(demo_admin,"EXTENSION_TRADES",self.store), \
             patch.object(demo_admin,"load_settings",return_value={
                 **demo_admin.DEFAULTS,"profile_url":PROFILE}), \
             patch.object(demo_admin,"extension_record_trade") as broker:
            self.store.accept_month_history(packet,PROFILE)
            result=demo_admin.extension_dashboard()
            self.assertEqual([],result["today"])
            self.assertEqual(1,len(result["month"]["items"]))
            self.assertEqual("ready",result["month"]["status"])
            self.assertTrue(result["month"]["items"][0]["can_demo_buy"] is False)
            broker.assert_not_called()

    def test_author_switch_resets_backfill_state(self):
        now=datetime.now(timezone.utc).isoformat()
        self.store.accept({"status":"ok","profile_url":PROFILE,
            "instruments":[{"ticker":"BSPB","classCode":"TQBR",
                            "maxTradeDateTime":now,"totalOperationsCount":1}],"events":[]},PROFILE)
        self.store.accept_history({"kind":"history","profile_url":PROFILE,
            "ticker":"BSPB","classCode":"TQBR","events":[]},PROFILE)
        other=PROFILE.replace("LinMath","AnotherAuthor")
        self.store.accept({"status":"ok","profile_url":other,
            "instruments":[{"ticker":"BSPB","classCode":"TQBR",
                            "maxTradeDateTime":now,"totalOperationsCount":2}],"events":[]},other)
        self.assertEqual([],self.store.history_progress(other)["done"])
        self.assertEqual([],self.store.public_snapshot(other)["history_entries"])
        self.assertEqual([],self.store.public_snapshot(PROFILE)["instruments"])


if __name__ == "__main__":
    unittest.main()
