"""Browser source durable outbox and money-safe failure/ack behaviour."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import demo_admin
from extension_source import ExtensionStore
from extension_portfolio import ExtensionPortfolioStore

PROFILE = "https://www.tbank.ru/invest/social/profile/LinMath/"
INSTRUMENT = {"ticker":"BSPB","classCode":"TQBR","showName":"БСПБ",
              "type":"stock","totalOperationsCount":2,
              "maxTradeDateTime":datetime.now(timezone.utc).isoformat()}
EVENT = {"ticker":"BSPB","classCode":"TQBR","action":"buy",
         "averagePrice":"124","tradeDateTime":datetime.now(timezone.utc).isoformat()}


class DurableDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ExtensionStore(self.temp.name)
        self.store.accept({"status":"ok","profile_url":PROFILE,
                           "instruments":[INSTRUMENT],"events":[]}, PROFILE)

    def incoming(self, profile=PROFILE):
        return {"status":"ok","profile_url":profile,
                "instruments":[{**INSTRUMENT,"totalOperationsCount":3}],
                "events":[EVENT]}

    def test_loss_of_http_ack_never_resends_committed_broker_signal(self):
        self.assertEqual(1,self.store.accept(self.incoming(),PROFILE)["accepted"])
        signals=[]
        self.assertEqual(1,self.store.drain_outbox(lambda trade: signals.append(trade)))
        self.assertEqual(1,len(signals))
        self.assertTrue(signals[0]["_source_key"].startswith("pulse:"))
        self.assertEqual(0,self.store.accept(self.incoming(),PROFILE)["accepted"])
        recreated=ExtensionStore(self.temp.name)
        self.assertEqual(0,recreated.drain_outbox(lambda t: signals.append(t)))
        self.assertEqual(1,len(signals))

    def test_crash_mid_callback_requires_manual_review_not_retry(self):
        self.store.accept(self.incoming(),PROFILE)
        with self.assertRaisesRegex(RuntimeError,"uncertain"):
            self.store.drain_outbox(lambda _: (_ for _ in ()).throw(RuntimeError("uncertain")))
        status=self.store.outbox_status()
        self.assertEqual(1,status.get("manual_review"))
        self.assertEqual(0,self.store.drain_outbox(lambda _: self.fail("double execution")))

    def test_author_change_expires_pending_before_any_side_effect(self):
        self.store.accept(self.incoming(),PROFILE)
        another=PROFILE.replace("LinMath","AnotherAuthor")
        self.store.accept({"status":"ok","profile_url":another,"events":[],
                           "instruments":[INSTRUMENT]},another)
        self.assertEqual(0,self.store.drain_outbox(lambda _: self.fail("wrong author")))
        self.assertEqual(1,self.store.outbox_status().get("expired"))

    def test_signal_expiration_prevents_late_order(self):
        self.store.accept(self.incoming(),PROFILE)
        with self.store.lock:
            state=self.store._read()
            state["outbox"][0]["created_at"]=(datetime.now(timezone.utc)-
                timedelta(minutes=8)).isoformat()
            self.store._save(state)
        self.assertEqual(0,self.store.drain_outbox(lambda _: self.fail("stale signal")))
        self.assertEqual(1,self.store.outbox_status().get("expired"))


class BrowserLiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trades=ExtensionStore(self.temp.name)
        self.portfolio=ExtensionPortfolioStore(self.temp.name)
        self.settings=copy.deepcopy(demo_admin.DEFAULTS)
        self.settings.update(profile_url=PROFILE,monitoring_enabled=True,real_mode="confirm")
        self.trades.accept({"status":"ok","profile_url":PROFILE,
                            "instruments":[INSTRUMENT],"events":[]},PROFILE)
        self.portfolio.accept({"kind":"portfolio","profile_url":PROFILE,
               "screen":{"url":PROFILE,"heading":"LinMath. Портфель",
                  "portfolio":True,
                  "rows":[{"text":"БСПБ 10%","links":[]}]}
               },PROFILE,[INSTRUMENT])
        self.trade={**EVENT,"_source_key":"pulse:verified-test-1"}

    def test_existing_mode_is_not_enough_to_enable_real_orders(self):
        with patch.object(demo_admin,"EXTENSION_TRADES",self.trades), \
             patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"queue_approval") as approval, \
             patch.object(demo_admin,"place_real") as real, \
             patch.object(demo_admin,"notify"), patch.object(demo_admin,"add_event") as journal:
            demo_admin.extension_record_trade(self.trade)
            real.assert_not_called()
            approval.assert_not_called()
            self.assertEqual("blocked",journal.call_args.args[0]["real_status"])
            self.assertFalse(demo_admin.extension_live_source_ready(self.settings))

    def test_real_confirmation_requires_explicit_opt_in_and_fresh_portfolio(self):
        self.settings["extension_live_enabled"]=True
        from trade_approvals import fingerprint
        self.trade["_delivery_context"]={"live_enabled":True,"settings_hash":fingerprint(self.settings)}
        with patch.dict("os.environ", {"TINVEST_ALLOW_EXTENSION_REAL":"1"}), \
             patch.object(demo_admin,"EXTENSION_TRADES",self.trades), \
             patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"queue_approval", return_value={
                 "id":"test-nonce","reasons":["Подтверждение реальной сделки"]}) as approval, \
             patch.object(demo_admin,"place_real") as real, \
             patch.object(demo_admin,"notify"), patch.object(demo_admin,"add_event"):
            self.assertTrue(demo_admin.extension_live_source_ready(self.settings))
            demo_admin.extension_record_trade(self.trade)
            approval.assert_called_once()
            self.assertEqual(self.trade["_source_key"],approval.call_args.args[3])
            real.assert_not_called()
            stale={**self.trade,"_source_key":"pulse:stale"}
            with self.portfolio.lock:
                state=self.portfolio._read()
                state["checked_at"]=(datetime.now(timezone.utc)-
                    timedelta(minutes=10)).isoformat()
                from client_accounts import atomic_text
                import json
                atomic_text(self.portfolio.path,json.dumps(state))
            demo_admin.extension_record_trade(stale)
            self.assertEqual(1,approval.call_count)
            real.assert_not_called()


if __name__=="__main__":
    unittest.main()
