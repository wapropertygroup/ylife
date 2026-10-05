"""The ticker box's suggestions and the no-prices check (ystocker.symbols).

Asked 2026-10-05: "股票代码 should have auto complete and stop analyze if the
ticker is not found". The first two free runs died on "NIFTY50" (an index) and
"TCS" (Tata Consultancy is TCS.NS). These pin:

* the parsers, on Yahoo's answers as served to the box that day
  (tests/fixtures/symbols/yahoo.json): only equities and ETFs the run form
  accepts are suggested, and a 404 or a chart with no rows is "missing";
* the rules around them: only "missing" stops a run, a check that cannot be
  made is "unknown", A-share codes are not checked, answers are cached, and
  Yahoo being unreachable falls back to the local lists;
* agents.public_log, which keeps the traceback off the run page.

No app, no network: Yahoo is the fixture, and the HTTP call is a stub.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ystocker import agents, symbols  # noqa: E402

FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "symbols" / "yahoo.json").read_text())


def _fixture(name):
    got = FIXTURES[name]
    return got["status"], got["body"]


class ParseTests(unittest.TestCase):

    def test_tcs_suggests_tata_consultancy_first(self):
        found = symbols.parse_search(_fixture("search_TCS")[1], agents.valid_ticker)
        self.assertEqual(found[0]["ticker"], "TCS.NS")
        self.assertEqual(found[0]["exchange"], "NSE")
        self.assertIn("TATA CONSULTANCY", found[0]["name"].upper())
        # Mutual funds are dropped; an ETF stays.
        self.assertEqual({f["type"] for f in found}, {"EQUITY", "ETF"})
        self.assertNotIn("TCSHX", [f["ticker"] for f in found])

    def test_nothing_the_form_would_refuse_is_offered(self):
        # NIFTY50's answers are indexes, an 11-character symbol and a digit-first
        # Korean ETF: none of them can be typed into the form, so none is offered.
        found = symbols.parse_search(_fixture("search_NIFTY50")[1], agents.valid_ticker)
        self.assertEqual(found, [])

    def test_the_chart_tells_a_listing_from_a_typo(self):
        self.assertEqual(symbols.parse_chart(*_fixture("chart_TCS")), symbols.MISSING)
        self.assertEqual(symbols.parse_chart(*_fixture("chart_TCS.NS")), symbols.FOUND)

    def test_anything_else_is_unknown(self):
        self.assertEqual(symbols.parse_chart(500, None), symbols.UNKNOWN)
        self.assertEqual(symbols.parse_chart(429, {}), symbols.UNKNOWN)
        self.assertEqual(symbols.parse_chart(200, "not json"), symbols.UNKNOWN)
        self.assertEqual(symbols.parse_chart(200, {"chart": {"result": [{}]}}), symbols.MISSING)

    def test_local_matches_put_tickers_before_names(self):
        rows = [("AAPL", "Apple Inc.", "Nasdaq"), ("APLE", "Apple Hospitality REIT", "NYSE"),
                ("MSFT", "Microsoft", "Nasdaq")]
        # APLE starts with "AP"; AAPL only has it in its name, so it comes second.
        self.assertEqual([r["ticker"] for r in symbols.local_matches("ap", rows)], ["APLE", "AAPL"])
        self.assertEqual([r["ticker"] for r in symbols.local_matches("micro", rows)], ["MSFT"])
        self.assertEqual(symbols.local_matches(" ", rows), [])


class _Stubbed(unittest.TestCase):
    """Yahoo replaced by a list of answers, and the caches emptied."""

    def setUp(self):
        self.calls = []
        self.answers = []
        self.enterContext(mock.patch.object(symbols, "_searches", {}))
        self.enterContext(mock.patch.object(symbols, "_verdicts", {}))
        self.enterContext(mock.patch.object(symbols, "_get", self._get))

    def _get(self, url, params):
        self.calls.append((url, params))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class CheckTests(_Stubbed):

    def test_missing_is_remembered_for_half_an_hour(self):
        self.answers = [_fixture("chart_TCS")]
        self.assertEqual(symbols.check("tcs"), symbols.MISSING)
        self.assertEqual(symbols.check("TCS"), symbols.MISSING)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("/TCS", self.calls[0][0])
        with mock.patch.object(symbols.time, "time", return_value=symbols.time.time() + 31 * 60):
            self.answers = [_fixture("chart_TCS.NS")]
            self.assertEqual(symbols.check("TCS"), symbols.FOUND)

    def test_found_is_remembered_for_a_day(self):
        self.answers = [_fixture("chart_TCS.NS")]
        self.assertEqual(symbols.check("TCS.NS"), symbols.FOUND)
        with mock.patch.object(symbols.time, "time", return_value=symbols.time.time() + 23 * 3600):
            self.assertEqual(symbols.check("TCS.NS"), symbols.FOUND)
        self.assertEqual(len(self.calls), 1)

    def test_a_check_that_cannot_be_made_lets_the_run_go(self):
        self.answers = [RuntimeError("yahoo-lookup cool-down active"), (503, None)]
        self.assertEqual(symbols.check("AAPL"), symbols.UNKNOWN)
        self.assertEqual(symbols.check("AAPL"), symbols.UNKNOWN)
        self.assertEqual(len(self.calls), 2)          # an unknown is not cached

    def test_a_shares_and_malformed_symbols_are_not_checked(self):
        for raw in ("600519", "SH600519", "600519.SS", "", "^NSEI", "TOO-LONG-SYMBOL"):
            self.assertEqual(symbols.check(raw), symbols.UNKNOWN, raw)
        self.assertEqual(self.calls, [])

    def test_the_symbol_is_quoted_into_the_path(self):
        self.answers = [_fixture("chart_TCS.NS")]
        symbols.check("BRK-B")
        self.assertTrue(self.calls[0][0].endswith("/chart/BRK-B"))
        self.assertEqual(self.calls[0][1], {"range": "1mo", "interval": "1d"})


class SearchTests(_Stubbed):

    def test_yahoo_answers_are_cached_per_query(self):
        self.answers = [_fixture("search_TCS")]
        found, source = symbols.search("tcs")
        self.assertEqual((found[0]["ticker"], source), ("TCS.NS", "yahoo"))
        self.assertEqual(symbols.search(" TCS ")[0], found)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["q"], "tcs")
        self.assertNotIn("enableFuzzyQuery", self.calls[0][1])

    def test_unreachable_yahoo_falls_back_to_the_local_lists(self):
        self.answers = [RuntimeError("timeout")]
        followed = lambda: [("NVDA", "NVIDIA Corporation", ""), ("7203.T", "Toyota", "")]
        with mock.patch("ystocker.directory.peek", return_value={"rows": [
                {"t": "NVO", "n": "Novo Nordisk", "x": "NYSE", "also": [], "cik": 1}]}):
            found, source = symbols.search("NV", followed=followed)
        self.assertEqual(source, "local")
        # 7203.T is digit-first, so the form would refuse it.
        self.assertEqual([f["ticker"] for f in found], ["NVDA", "NVO"])

    def test_a_query_not_worth_sending_is_not_sent(self):
        # 贵州茅台: Yahoo's search gave no answer for it from the box.
        for raw in ("", "   ", "a" * 41, "AAPL;DROP", "<script>", "贵州茅台"):
            self.assertEqual(symbols.search(raw), ([], "none"), raw)
        self.assertEqual(self.calls, [])

    def test_a_company_name_is_sent(self):
        self.answers = [(200, {"quotes": []})]
        self.assertEqual(symbols.search("Johnson & Johnson"), ([], "yahoo"))
        self.assertEqual(self.calls[0][1]["q"], "Johnson & Johnson")


class PublicLogTests(unittest.TestCase):
    """The run page printed a traceback under every failed run (2026-10-05)."""

    def test_the_stderr_tail_is_cut(self):
        log = "Analysing TCS...\n" + agents.STDERR_MARK + "Traceback (most recent call last):\n  ..."
        self.assertEqual(agents.public_log(log), "Analysing TCS...")

    def test_a_log_that_is_only_stderr_becomes_empty(self):
        # _run strips the log, so a run that printed nothing starts at the marker.
        stored = (agents.STDERR_MARK + "Traceback ...").strip()
        self.assertEqual(agents.public_log(stored), "")

    def test_a_log_without_stderr_is_unchanged(self):
        self.assertEqual(agents.public_log("all good\n"), "all good")
        self.assertEqual(agents.public_log(None), "")

    def test_the_marker_only_counts_as_a_whole_line(self):
        self.assertEqual(agents.public_log("printed [stderr] inline"), "printed [stderr] inline")


if __name__ == "__main__":
    unittest.main()
