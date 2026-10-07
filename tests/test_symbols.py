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

And, for the Fundamentals tab's compare box (asked 2026-10-06: "TSMC ticker
should have auto complete"): a name's initials and its punctuation-free words
in the local lists, Yahoo's own answer for "TSMC" holding no TSM (which is why
the box asks the local lists first), companies only, and one cached answer
serving both boxes, with a daily budget spent only on a real call.

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


def _tickers(found):
    return [f["ticker"] for f in found]


class CompareBoxTests(unittest.TestCase):
    """What the compare box finds without asking anyone, and what Yahoo adds."""

    ROWS = [
        ("TSM", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "NYSE"),
        ("TSLA", "Tesla, Inc.", "Nasdaq"),
        ("KO", "COCA COLA CO", "NYSE"),
        ("T", "AT&T INC.", "NYSE"),
        ("BMY", "BRISTOL MYERS SQUIBB CO", "NYSE"),
    ]

    def test_an_abbreviation_finds_its_company_by_initials(self):
        # No ticker starts with TSMC and no name contains it.
        self.assertEqual(_tickers(symbols.local_matches("TSMC", self.ROWS)), ["TSM"])
        self.assertEqual(_tickers(symbols.local_matches("tsmc", self.ROWS)), ["TSM"])
        self.assertEqual(_tickers(symbols.local_matches("BMS", self.ROWS)), ["BMY"])

    def test_a_cut_short_name_is_matched_by_its_full_one(self):
        # Production's ticker cache names TSM by Yahoo's short name, cut at 31
        # characters: initials TSM. Shown, but SEC's full name is what matches.
        rows = [("TSM", "Taiwan Semiconductor Manufactur", "NYSE",
                 ["TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"])]
        found = symbols.local_matches("TSMC", rows)
        self.assertEqual([(f["ticker"], f["name"]) for f in found],
                         [("TSM", "Taiwan Semiconductor Manufactur")])
        self.assertEqual(symbols.local_matches("TSMC", [rows[0][:3]]), [])

    def test_initials_need_three_letters_in_one_word(self):
        # "TS" is two tickers' start, not an abbreviation.
        self.assertEqual(_tickers(symbols.local_matches("TS", self.ROWS)), ["TSM", "TSLA"])
        self.assertEqual(symbols.local_matches("T S M", self.ROWS), [])

    def test_tickers_then_initials_then_names(self):
        rows = [("ZZZ", "The ABC Company", ""), ("YYY", "Alpha Beta Capital", ""),
                ("ABCX", "Zeta Holdings", "")]
        self.assertEqual(_tickers(symbols.local_matches("abc", rows)), ["ABCX", "YYY", "ZZZ"])

    def test_punctuation_is_not_part_of_a_name(self):
        self.assertEqual(_tickers(symbols.local_matches("coca-cola", self.ROWS)), ["KO"])
        self.assertEqual(_tickers(symbols.local_matches("Coca Cola", self.ROWS)), ["KO"])
        self.assertEqual(_tickers(symbols.local_matches("at&t", self.ROWS)), ["T"])
        # Nothing but punctuation would otherwise be found in every name.
        self.assertEqual(symbols.local_matches("&", self.ROWS), [])
        self.assertEqual(symbols.local_matches("-", self.ROWS), [])

    def test_yahoo_does_not_know_tsmc_is_tsm(self):
        # Its whole answer from the box: receipts in São Paulo and Buenos Aires,
        # Tesmec (another company), three crypto tokens and a Korean ETF.
        found = symbols.parse_search(_fixture("search_TSMC")[1], lambda t: True,
                                     types=symbols.COMPANY_TYPES)
        self.assertEqual(_tickers(found), ["TSMC34.SA", "TSMC.BA", "TSMCF"])
        self.assertEqual(found[2]["name"], "Tesmec S.p.A.")

    def test_only_companies_are_offered_for_comparison(self):
        google = symbols.parse_search(_fixture("search_google")[1], lambda t: True,
                                      types=symbols.COMPANY_TYPES)
        nvidia = symbols.parse_search(_fixture("search_nvidia")[1], lambda t: True,
                                      types=symbols.COMPANY_TYPES)
        self.assertEqual(_tickers(google), ["GOOG"])           # "google" is Alphabet
        self.assertEqual(_tickers(nvidia), ["NVDA", "NVDA.TO", "NVD.DE"])
        # The run form still takes a fund.
        self.assertIn("NVDX", _tickers(symbols.parse_search(_fixture("search_nvidia")[1], lambda t: True)))

    def test_merge_keeps_the_first_list_first(self):
        a = [{"ticker": "TSM"}, {"ticker": "TSMC34.SA"}]
        b = [{"ticker": "TSMC34.SA"}, {"ticker": "TSMC.BA"}, {"ticker": "TSMCF"}]
        self.assertEqual(_tickers(symbols.merge(a, b)), ["TSM", "TSMC34.SA", "TSMC.BA", "TSMCF"])
        self.assertEqual(_tickers(symbols.merge(a, b, limit=3)), ["TSM", "TSMC34.SA", "TSMC.BA"])


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

    def test_one_answer_serves_both_boxes(self):
        self.answers = [_fixture("search_nvidia")]
        run_form, source = symbols.search("nvidia")
        answer = symbols.quotes("NVIDIA")
        self.assertEqual((source, len(self.calls)), ("yahoo", 1))
        self.assertIn("NVDX", _tickers(run_form))
        companies = symbols.parse_search({"quotes": answer}, lambda t: True, types=symbols.COMPANY_TYPES)
        self.assertEqual(_tickers(companies), ["NVDA", "NVDA.TO", "NVD.DE"])

    def test_a_cached_answer_keeps_only_what_is_read(self):
        self.answers = [_fixture("search_TSMC")]
        answer = symbols.quotes("TSMC")
        self.assertEqual(len(answer), 7)
        self.assertTrue(all(set(q) <= set(symbols._QUOTE_FIELDS) for q in answer))

    def test_the_budget_is_spent_only_on_a_call(self):
        spent = []
        budget = lambda: spent.append(1) or True              # noqa: E731
        self.answers = [_fixture("search_TSMC")]
        symbols.quotes("TSMC", budget=budget)
        symbols.quotes(" tsmc ", budget=budget)
        self.assertEqual((len(self.calls), len(spent)), (1, 1))

    def test_a_spent_budget_sends_nothing(self):
        self.assertIsNone(symbols.quotes("TSMC", budget=lambda: False))
        self.assertEqual(self.calls, [])

    def test_quotes_is_none_when_yahoo_cannot_be_asked(self):
        self.answers = [RuntimeError("timeout"), (503, None)]
        self.assertIsNone(symbols.quotes("intel"))
        self.assertIsNone(symbols.quotes("amd"))
        self.assertIsNone(symbols.quotes("贵州茅台"))           # not sent at all
        self.assertEqual(len(self.calls), 2)
        # A failure is not remembered: the next ask goes out again.
        self.answers = [_fixture("search_nvidia")]
        self.assertEqual(len(symbols.quotes("intel")), 7)


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
