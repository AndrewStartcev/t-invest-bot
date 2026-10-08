import unittest
from pathlib import Path
from unittest.mock import Mock

from investor_portfolio import parse_screen, read_visible_portfolio, compare_positions
from pulse_live import PulseBrowser, canonical_profile_url
from shared_pulse import read_source, RemotePulse


PROFILE = canonical_profile_url("https://www.tbank.ru/invest/social/profile/LinMath/")
INSTRUMENTS = [{"ticker": "ROSN", "classCode": "TQBR", "showName": "Роснефть", "type": "Share"},
               {"ticker": "GAZP", "classCode": "TQBR", "showName": "Газпром", "type": "Share"}]


class InvestorPortfolioTests(unittest.TestCase):
    def screen(self, text="Роснефть 0,02%", links=None):
        return {"url": PROFILE, "heading": "LinMath. Портфель", "portfolio": True, "rows": [{"text": text, "links": links or []}]}

    def test_separate_author_heading_requires_verified_author_identity(self):
        screen={**self.screen(), "heading":"Портфель", "author":"LinMath"}
        self.assertEqual(parse_screen(screen, PROFILE, INSTRUMENTS)["status"],"partial")
        screen["author"]="Other"
        self.assertEqual(parse_screen(screen, PROFILE, INSTRUMENTS)["status"],"unavailable")
        screen.pop("author")
        self.assertEqual(parse_screen(screen, PROFILE, INSTRUMENTS)["status"],"unavailable")
        self.assertEqual(parse_screen({**self.screen(), "heading":"LinMath — Портфель"}, PROFILE, INSTRUMENTS)["status"],"partial")

    def test_portfolio_display_loads_with_policy_off_and_policy_on_reads_fresh(self):
        import copy, tempfile
        from datetime import datetime, timezone
        from unittest.mock import patch
        import demo_admin
        browser=Mock()
        browser.snapshot.return_value=("LinMath", [])
        browser.portfolio.return_value={"status":"partial", "profile_url":PROFILE, "positions":{},
                                        "rows":[{"name":"Роснефть","percent":"1"}],
                                        "checked_at":datetime.now(timezone.utc).isoformat()}
        settings=copy.deepcopy(demo_admin.DEFAULTS);settings["profile_url"]=PROFILE;settings["policy"]["enabled"]=False
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(demo_admin,"STATE_PATH",Path(folder)/"state.json"), \
                patch.object(demo_admin,"INVESTOR_PORTFOLIO",{}), \
                patch.object(demo_admin,"AUTH",{}),patch.object(demo_admin,"MONITOR",{}), \
                patch.object(demo_admin,"TODAY",[]),patch.object(demo_admin,"recent_profile_trades",return_value=[]), \
                patch.object(demo_admin,"prepare_real") as real:
            demo_admin.poll_once(browser,settings,emit_events=False)
            self.assertEqual(demo_admin.INVESTOR_PORTFOLIO["rows"][0]["name"],"Роснефть")
            demo_admin.poll_once(browser,settings,emit_events=False)
            self.assertEqual(browser.portfolio.call_count,1)
            settings["policy"]["enabled"]=True
            demo_admin.poll_once(browser,settings,emit_events=False)
            self.assertEqual(browser.portfolio.call_count,2)
            real.assert_not_called()

    def test_visible_company_establishes_presence_not_absence(self):
        snapshot = parse_screen(self.screen(), PROFILE, INSTRUMENTS)
        self.assertEqual(snapshot["status"], "partial")
        self.assertTrue(snapshot["positions"]["ROSN:TQBR"]["present"])
        self.assertEqual(snapshot["positions"]["ROSN:TQBR"]["percent"], "0.02")
        self.assertNotIn("GAZP:TQBR", snapshot["positions"])

    def test_instrument_link_matches_ticker_when_name_differs(self):
        snapshot = parse_screen(self.screen("ПАО НК Роснефть 1,5%", ["https://www.tbank-online.com/invest/stocks/ROSN/"]), PROFILE, INSTRUMENTS)
        self.assertEqual(snapshot["positions"]["ROSN:TQBR"]["evidence"], "instrument_link")
        wrong = parse_screen(self.screen("Другой актив 1%", ["https://www.tbank-online.com/invest/futures/ROSN/"]), PROFILE, INSTRUMENTS)
        self.assertEqual(wrong["positions"], {})

    def test_hidden_zero_ambiguous_and_post_rows_are_not_holdings(self):
        for text in ("Другое 20,07%", "Роснефть 1% +2%", "Роснефть 101%", "Роснефть"):
            with self.subTest(text=text):
                self.assertEqual(parse_screen(self.screen(text), PROFILE, INSTRUMENTS)["positions"], {})
        screen = self.screen()
        screen["portfolio"] = False
        self.assertEqual(parse_screen(screen, PROFILE, INSTRUMENTS)["status"], "unavailable")

    def test_displayed_zero_is_excluded_but_missing_position_is_unknown(self):
        snapshot = parse_screen(self.screen("Роснефть 0%"), PROFILE, INSTRUMENTS)
        self.assertFalse(snapshot["positions"]["ROSN:TQBR"]["present"])
        self.assertTrue(snapshot["positions"]["ROSN:TQBR"]["displayed_zero"])
        self.assertNotIn("GAZP:TQBR", snapshot["positions"])

    def test_compare_displayed_weights_does_not_mix_authors(self):
        before = parse_screen(self.screen("Роснефть 10%"), PROFILE, INSTRUMENTS)
        current = parse_screen(self.screen("Роснефть 5%"), PROFILE, INSTRUMENTS)
        compare_positions(current, before)
        self.assertEqual(current["positions"]["ROSN:TQBR"]["weight_change"], "decreased")
        self.assertEqual(current["positions"]["ROSN:TQBR"]["previous_percent"], "10")
        compare_positions(current, {**before, "profile_url": PROFILE.replace("LinMath", "Other")})
        self.assertEqual(current["positions"]["ROSN:TQBR"]["weight_change"], "unknown")

    def test_wrong_author_host_and_route_are_rejected(self):
        for url in (PROFILE.replace("LinMath", "Other"), PROFILE.replace("www.tbank.ru", "evil.example"),
                    PROFILE + "post/123/"):
            screen = {**self.screen(), "url": url}
            self.assertEqual(parse_screen(screen, PROFILE, INSTRUMENTS)["positions"], {})
        screen = {**self.screen(), "heading": "Other. Портфель"}
        self.assertEqual(parse_screen(screen, PROFILE, INSTRUMENTS)["positions"], {})

    def test_parser_does_not_export_raw_links_or_percentless_secrets(self):
        snapshot = parse_screen(self.screen("Роснефть 1%", ["https://evil.example/secret"]), PROFILE, INSTRUMENTS)
        self.assertNotIn("secret", str(snapshot))
        self.assertNotIn("links", str(snapshot["rows"]))

    def test_reader_uses_separate_page_and_closes_on_failure(self):
        browser = Mock()
        page = browser.context.new_page.return_value
        page.goto.side_effect = RuntimeError("secret session body")
        result = read_visible_portfolio(browser, PROFILE, INSTRUMENTS)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret", str(result))
        page.close.assert_called_once()
        browser.page.goto.assert_not_called()

    def test_source_rpc_binds_portfolio_to_requested_author(self):
        browser = Mock(profile_name="Other")
        browser.portfolio.return_value = parse_screen(self.screen(), PROFILE, INSTRUMENTS)
        result = read_source(browser, {"action": "portfolio", "profile_url": PROFILE})
        browser.refresh.assert_called_once_with(PROFILE)
        browser.portfolio.assert_called_once_with(PROFILE)
        self.assertEqual(result["profile_url"], PROFILE)

    def test_browser_reads_current_author_instruments_not_client_supplied_rows(self):
        from unittest.mock import patch
        browser = PulseBrowser(Path("unused"))
        browser.snapshot = Mock(return_value=("LinMath", INSTRUMENTS))
        with patch("investor_portfolio.read_visible_portfolio", return_value={"status": "partial"}) as reader:
            browser.portfolio(PROFILE)
            browser.snapshot.assert_called_once_with(PROFILE, {})
            reader.assert_called_once_with(browser, PROFILE, INSTRUMENTS)

    def test_live_poll_uses_fresh_author_position_in_confirmation(self):
        import copy
        import json
        import tempfile
        import demo_admin
        from unittest.mock import patch
        from trade_policy import approval_reasons
        browser = Mock()
        item = {**INSTRUMENTS[0], "type": "stock", "totalOperationsCount": 2,
                "history": [{"action": "buy", "tradeDateTime": "2026-10-06T10:00:00+03:00",
                             "averagePrice": 100, "currency": "rub"}]}
        browser.snapshot.return_value = ("LinMath", [item])
        browser.portfolio.return_value = parse_screen(self.screen(), PROFILE, INSTRUMENTS)
        settings = {**copy.deepcopy(demo_admin.DEFAULTS), "profile_url": PROFILE,
                    "monitoring_enabled": True, "auto_demo_buy": True}
        settings["policy"].update(enabled=True, confirm_low_profit=False)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            state.write_text(json.dumps({"LinMath": {"ROSN:TQBR": 1}}))
            with patch.object(demo_admin, "STATE_PATH", state), patch.object(demo_admin, "INVESTOR_PORTFOLIO", {}), \
                 patch.object(demo_admin, "recent_profile_trades", return_value=[]), \
                 patch.object(demo_admin, "queue_approval", side_effect=ValueError("test preview")) as queue, \
                 patch.object(demo_admin, "notify"), patch.object(demo_admin, "add_event"), \
                 patch.object(demo_admin, "apply_demo") as execute:
                demo_admin.poll_once(browser, settings)
                signal = queue.call_args.args[1]
                self.assertTrue(signal["investor_position"]["present"])
                self.assertIn("0.02%", approval_reasons(signal, settings["policy"])[0])
                browser.portfolio.assert_called_once_with(PROFILE, [item])
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
