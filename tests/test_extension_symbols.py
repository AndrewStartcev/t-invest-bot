"""Regression coverage for real-world Pulse symbol formats.

The backend must retain bank-supplied ticker/classCode values verbatim
instead of silently dropping unsupported instruments or their trade deltas.
"""
import tempfile
import unittest

from extension_source import ExtensionStore, _instrument_key

PROFILE = "https://www.tbank.ru/invest/social/profile/LinMath/"


class PulseSymbolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ExtensionStore(self.temp.name)

    def snapshot(self, ticker, code="TQBR", count=2, events=None):
        return {"status": "ok", "profile_url": PROFILE,
                "instruments": [{"ticker": ticker, "classCode": code,
                                 "totalOperationsCount": count}],
                "events": events or []}

    def test_extended_symbols_are_preserved_verbatim(self):
        symbols = [
            "USD/RUB", "BRK.B", "S&P500", "A+B", "FUT@SPB",
            "EUR:USD", "ТЕСТ-12.26", "LONG-OPTION-COMPOSITE-ID-202612",
            "SPX 500", "BTC_USDT", "USD=RUB", "RUB#1",
        ]
        for ticker in symbols:
            with self.subTest(ticker=ticker):
                with tempfile.TemporaryDirectory() as root:
                    source = ExtensionStore(root)
                    report = self.snapshot(ticker)
                    source.accept(report, PROFILE)
                    current = source.public_snapshot(PROFILE)
                    self.assertEqual(ticker, current["instruments"][0]["ticker"])
                    self.assertEqual(2, source.accept(
                        {"status":"test"}, PROFILE)["counts"][_instrument_key(ticker, "TQBR")])
                    event = {"ticker": ticker, "classCode": "TQBR", "action": "buy",
                             "tradeDateTime": "2026-10-29T15:00:00+00:00",
                             "averagePrice": "123.45"}
                    self.assertEqual(1, source.accept(
                        self.snapshot(ticker, count=3, events=[event]), PROFILE)["accepted"])
                    self.assertEqual(0, source.accept(
                        self.snapshot(ticker, count=3, events=[event]), PROFILE)["accepted"])
                    self.assertEqual(ticker, source.public_snapshot(PROFILE)["recent"][0]["ticker"])

    def test_invalid_symbol_does_not_save_partial_snapshot(self):
        for ticker in [None, "", " ", " BAD", "BAD ", "A\nB", "<script>",
                       "A'B", "A\\B", "A" * 129]:
            with self.subTest(ticker=ticker):
                with tempfile.TemporaryDirectory() as root:
                    source = ExtensionStore(root)
                    with self.assertRaisesRegex(ValueError, "Некорректный тикер у инструмента №1"):
                        source.accept(self.snapshot(ticker), PROFILE)
                    self.assertFalse(source.path.exists())

    def test_duplicate_keys_rejected_and_class_code_cannot_ambiguously_split(self):
        report = self.snapshot("USD:RUB")
        report["instruments"].append({"ticker": "USD:RUB", "classCode": "TQBR",
                                      "totalOperationsCount": 2})
        with self.assertRaisesRegex(ValueError, "Некорректные счётчики"):
            self.store.accept(report, PROFILE)
        with self.assertRaisesRegex(ValueError, "Некорректный classCode"):
            self.store.accept(self.snapshot("USD", "RUB:TQBR"), PROFILE)
        self.assertFalse(self.store.path.exists())

    def test_existing_simple_watermark_key_is_compatible(self):
        self.assertEqual("BSPB:TQBR", _instrument_key("BSPB", "TQBR"))
        self.store.accept(self.snapshot("BSPB"), PROFILE)
        self.assertEqual({"BSPB:TQBR":2},
            self.store.accept({"status":"test"},PROFILE)["counts"])


if __name__ == "__main__":
    unittest.main()
