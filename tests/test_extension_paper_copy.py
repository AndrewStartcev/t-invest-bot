"""Safe restoration of the existing paper-copy flow for browser events."""
import copy
import tempfile
import unittest
from unittest.mock import patch

import demo_admin
from extension_source import ExtensionStore
from extension_portfolio import ExtensionPortfolioStore

PROFILE="https://www.tbank.ru/invest/social/profile/LinMath/"
EVENT={"ticker":"BSPB","classCode":"TQBR","action":"buy",
       "tradeDateTime":"2026-10-29T18:00:00+00:00","averagePrice":"120"}
INSTRUMENT={"ticker":"BSPB","classCode":"TQBR","showName":"БСПБ",
            "type":"stock","totalOperationsCount":1}


class ExtensionPaperCopyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.trades=ExtensionStore(temp.name)
        self.portfolio=ExtensionPortfolioStore(temp.name)
        self.settings=copy.deepcopy(demo_admin.DEFAULTS)
        self.settings.update(profile_url=PROFILE,monitoring_enabled=True,auto_demo_buy=True)
        self.trades.accept({"status":"ok","profile_url":PROFILE,
                            "instruments":[INSTRUMENT],"events":[]},PROFILE)

    def send_new(self, count=2):
        updated={**INSTRUMENT,"totalOperationsCount":count}
        return self.trades.accept({"status":"ok","profile_url":PROFILE,
                                   "instruments":[updated],"events":[EVENT]},
                                  PROFILE,emit=demo_admin.extension_record_trade)

    def test_new_browser_trade_reuses_paper_copy_limits(self):
        with patch.object(demo_admin,"EXTENSION_TRADES",self.trades), \
             patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"apply_demo") as demo, \
             patch.object(demo_admin,"place_real") as real, \
             patch.object(demo_admin,"notify"), \
             patch.object(demo_admin,"add_event"):
            self.assertEqual(1,self.send_new()["accepted"])
            demo.assert_called_once()
            signal=demo.call_args.args[1]
            self.assertEqual("stock",signal["asset_type"])
            self.assertEqual("buy",signal["side"])
            self.assertEqual("unavailable",signal["investor_position"]["status"])
            real.assert_not_called()
            self.assertEqual(0,self.send_new()["accepted"])
            demo.assert_called_once()

    def test_existing_real_auto_setting_must_never_submit_extension_orders(self):
        self.settings["real_mode"]="auto"
        with patch.object(demo_admin,"EXTENSION_TRADES",self.trades), \
             patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"apply_demo") as demo, \
             patch.object(demo_admin,"place_real") as real, \
             patch.object(demo_admin,"queue_approval") as approval, \
             patch.object(demo_admin,"notify"), \
             patch.object(demo_admin,"add_event") as journal:
            self.assertEqual(1,self.send_new()["accepted"])
            demo.assert_not_called()
            approval.assert_not_called()
            real.assert_not_called()
            self.assertEqual("blocked",journal.call_args.args[0]["real_status"])

    def test_no_paper_copy_without_explicit_setting(self):
        self.settings["auto_demo_buy"]=False
        with patch.object(demo_admin,"EXTENSION_TRADES",self.trades), \
             patch.object(demo_admin,"EXTENSION_PORTFOLIO",self.portfolio), \
             patch.object(demo_admin,"load_settings",return_value=self.settings), \
             patch.object(demo_admin,"apply_demo") as demo, \
             patch.object(demo_admin,"place_real") as real, \
             patch.object(demo_admin,"notify"), \
             patch.object(demo_admin,"add_event"):
            self.assertEqual(1,self.send_new()["accepted"])
            demo.assert_not_called()
            real.assert_not_called()


if __name__=="__main__":
    unittest.main()
