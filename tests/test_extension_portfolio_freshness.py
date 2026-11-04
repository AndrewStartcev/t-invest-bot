"""Bank portfolio freshness is mandatory for decisions that inspect author holdings."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import demo_admin
from extension_portfolio import ExtensionPortfolioStore, PORTFOLIO_MAX_AGE_SECONDS
from extension_source import ExtensionStore
from broker_trade import TradeError

PROFILE = "https://www.tbank.ru/invest/social/profile/LinMath/"
OTHER = PROFILE.replace("LinMath", "OtherAuthor")
INSTRUMENTS = [
    {"ticker":"ROSN", "classCode":"TQBR", "showName":"Роснефть",
     "type":"stock","totalOperationsCount":2},
    {"ticker":"GAZP", "classCode":"TQBR", "showName":"Газпром",
     "type":"stock","totalOperationsCount":2},
]


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.portfolio=ExtensionPortfolioStore(tmp.name)
        self.screen={"url": PROFILE,"heading":"LinMath. Портфель",
                     "portfolio":True,"rows":[{
                         "text":"Роснефть 5%",
                         "links":["https://www.tbank.ru/invest/stocks/ROSN/"]}]}
        self.payload={"kind":"portfolio","profile_url":PROFILE,"screen":self.screen}
        self.settings=copy.deepcopy(demo_admin.DEFAULTS)
        self.settings["profile_url"]=PROFILE
        self.settings["policy"].update(enabled=True, confirm_existing=True,
                                        confirm_remaining=True, confirm_low_profit=False)

    def accept(self):
        self.portfolio.accept(self.payload,PROFILE,INSTRUMENTS)
        return self.portfolio.public_snapshot(PROFILE)

    def signal(self,side="buy",name="ROSN"):
        return {"ticker":name,"classCode":"TQBR","side":side,
                "_browser_source":True,
                "investor_position":self.portfolio.public_snapshot(PROFILE).get(
                    "positions",{}).get(name+":TQBR",{"status":"unavailable"})}

    def test_fresh_accepted_then_expired_without_disguising_timestamp(self):
        snap=self.accept()
        self.assertEqual("partial",snap["status"])
        self.assertTrue(snap["fresh"])
        old=datetime.fromisoformat(snap["checked_at"])
        expired=self.portfolio.public_snapshot(PROFILE,now=old+timedelta(
            seconds=PORTFOLIO_MAX_AGE_SECONDS+1))
        self.assertEqual("stale",expired["status"])
        self.assertFalse(expired["fresh"])
        self.assertEqual({},expired["positions"])
        self.assertEqual([],expired["rows"])
        self.assertEqual(snap["checked_at"],expired["last_success_at"])
        self.assertEqual(snap["checked_at"],expired["checked_at"])

    def test_failure_invalidates_positions_and_recovery_restores(self):
        first=self.accept()
        status=self.portfolio.mark_unavailable({
            "kind":"portfolio_status","profile_url":PROFILE,
            "reason":"unrecognized"},PROFILE)
        self.assertEqual("error",status["status"])
        bad=self.portfolio.public_snapshot(PROFILE)
        self.assertEqual("error",bad["status"])
        self.assertFalse(bad["fresh"])
        self.assertEqual({},bad["positions"])
        self.assertEqual(first["checked_at"],bad["last_success_at"])
        with self.assertRaisesRegex(ValueError,"другого автора"):
            self.portfolio.mark_unavailable({
                "kind":"portfolio_status","profile_url":OTHER,
                "reason":"not_visible"},PROFILE)
        with self.assertRaisesRegex(ValueError,"Неизвестная причина"):
            self.portfolio.mark_unavailable({
                "kind":"portfolio_status","profile_url":PROFILE,
                "reason":"arbitrary bank html"},PROFILE)
        self.assertTrue(self.accept()["fresh"])

    def test_missing_unverified_and_changed_positions_fail_closed(self):
        self.accept()
        with patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio):
            signal=self.signal()
            demo_admin.require_current_author_portfolio(signal,self.settings)
            # A displayed asset never proves absence of other assets.
            with self.assertRaisesRegex(TradeError,"не подтверждена"):
                demo_admin.require_current_author_portfolio(
                    self.signal(name="GAZP"),self.settings)
            self.portfolio.accept({**self.payload,"screen":{
                **self.screen,"rows":[{"text":"Роснефть 7%","links":self.screen["rows"][0]["links"]}]}},
                PROFILE,INSTRUMENTS)
            with self.assertRaisesRegex(TradeError,"изменилась"):
                demo_admin.require_current_author_portfolio(signal,self.settings)

    def test_stale_or_failed_portfolio_blocks_real_and_demo_decisions(self):
        self.accept()
        valid_signal=self.signal()
        with patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio):
            with self.portfolio.lock:
                current=self.portfolio._read()
                current["checked_at"]=(datetime.now(timezone.utc)-
                                        timedelta(minutes=4)).isoformat()
                from client_accounts import atomic_text
                atomic_text(self.portfolio.path,json.dumps(current,ensure_ascii=False))
            with self.assertRaisesRegex(TradeError,"устарел"):
                demo_admin.require_current_author_portfolio(valid_signal,self.settings)
            with patch.object(demo_admin,"selected_trade_account") as broker:
                with self.assertRaisesRegex(TradeError,"устарел"):
                    demo_admin.prepare_real(valid_signal,self.settings)
                broker.assert_not_called()
            # Unrelated rules do not change the client's selected trading mode.
            self.settings["policy"]["enabled"]=False
            demo_admin.require_current_author_portfolio(valid_signal,self.settings)

    def test_only_selected_author_portfolio_can_be_used(self):
        self.accept()
        with patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio):
            self.settings["profile_url"]=OTHER
            with self.assertRaisesRegex(TradeError,"устарел"):
                demo_admin.require_current_author_portfolio(
                    {**self.signal(),"_browser_source":True},self.settings)


if __name__=="__main__":
    unittest.main()
