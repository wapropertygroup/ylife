"""
Unit tests for ``ystocker.market_context`` — the pure render function behind
``agents.build_market_context``.

No app, no network, no DynamoDB: every input here is a hand-built dict shaped
like the real ``peek()`` payload from ``valuation.py``/``breadth.py``/
``cta.py``/``fedwatch.py``. The point of this module is that a missing or
malformed source degrades to one fewer bullet, never a crash and never a
fabricated number.
"""

from __future__ import annotations

import unittest

from ystocker import market_context


class TestAllSourcesMissing(unittest.TestCase):
    def test_all_none_yields_empty(self):
        self.assertEqual(market_context.render_market_block(), "")
        self.assertEqual(
            market_context.render_market_block(None, None, None, None), "")

    def test_all_empty_dicts_yield_empty(self):
        self.assertEqual(
            market_context.render_market_block({}, {}, {}, {}), "")

    def test_malformed_types_do_not_raise(self):
        # A shape that drifted from what peek() promises must degrade, not crash
        # a run that has already been charged for.
        self.assertEqual(
            market_context.render_market_block("oops", ["nope"], 5, object()),
            "")


class TestValuationLines(unittest.TestCase):
    VALUATION = {
        "headline": {
            "spx_trailing_pe": {"value": 24.3, "as_of": "2026-08-15"},
            "spx_pe_percentile": {"value": 78.0, "since": "1871-01-01"},
            "spy_forward_pe": {"value": 22.1, "coverage_pct": 91.0},
            "qqq_forward_pe": {"value": 27.4, "coverage_pct": 88.0},
        }
    }

    def test_trailing_pe_and_percentile_appear(self):
        block = market_context.render_market_block(valuation=self.VALUATION)
        self.assertIn("S&P 500 trailing P/E: 24.3x", block)
        self.assertIn("78.0th percentile", block)
        self.assertIn("since 1871-01-01", block)
        self.assertIn("2026-08-15", block)

    def test_forward_pe_entries_are_collected(self):
        block = market_context.render_market_block(valuation=self.VALUATION)
        self.assertIn("SPY 22.1x (coverage 91.0%)", block)
        self.assertIn("QQQ 27.4x (coverage 88.0%)", block)

    def test_missing_headline_yields_no_valuation_line(self):
        block = market_context.render_market_block(valuation={"as_of": "x"})
        self.assertEqual(block, "")

    def test_partial_headline_only_renders_whats_present(self):
        block = market_context.render_market_block(
            valuation={"headline": {"spy_forward_pe": {"value": 20.0}}})
        self.assertIn("SPY 20.0x", block)
        self.assertNotIn("trailing P/E", block)

    def test_trailer_is_always_appended_when_non_empty(self):
        block = market_context.render_market_block(valuation=self.VALUATION)
        self.assertIn("not as a signal about this ticker", block)


class TestBreadthLines(unittest.TestCase):
    def test_latest_values_render_sorted_numerically_not_lexically(self):
        # "20" before "200" before "50" would be the lexical (wrong) order.
        breadth = {"pct_above_ma": {
            "200": {"values": [10, 54]},
            "20": {"values": [10, 71]},
            "50": {"values": [10, 61]},
        }, "asof": "2026-08-28"}
        block = market_context.render_market_block(breadth=breadth)
        pos20 = block.index("71% above its 20-day")
        pos50 = block.index("61% above its 50-day")
        pos200 = block.index("54% above its 200-day")
        self.assertLess(pos20, pos50)
        self.assertLess(pos50, pos200)
        self.assertIn("as of 2026-08-28", block)

    def test_stale_baseline_is_labelled(self):
        breadth = {"pct_above_ma": {"50": {"values": [61]}}, "stale": True}
        block = market_context.render_market_block(breadth=breadth)
        self.assertIn("stale", block.lower())

    def test_fresh_data_says_nothing_about_staleness(self):
        breadth = {"pct_above_ma": {"50": {"values": [61]}}}
        block = market_context.render_market_block(breadth=breadth)
        self.assertNotIn("stale", block.lower())

    def test_missing_values_key_is_skipped_not_a_crash(self):
        breadth = {"pct_above_ma": {"50": {"dates": ["2026-08-28"]}}}
        self.assertEqual(market_context.render_market_block(breadth=breadth), "")

    def test_empty_pct_above_ma_yields_no_breadth_line(self):
        self.assertEqual(
            market_context.render_market_block(breadth={"pct_above_ma": {}}), "")


class TestCtaLines(unittest.TestCase):
    LATEST = {
        "latest": {
            "report_date": "2026-07-28",
            "spx_triggers": {"short": 7455.0, "medium": 7204.0, "long": 6765.0},
            "flows_1w_global_bn": {"up": -0.275, "flat": -7.48, "down": -31.46},
        }
    }

    def test_triggers_and_flows_appear(self):
        block = market_context.render_market_block(cta=self.LATEST)
        self.assertIn("long 6765.0", block)
        self.assertIn("medium 7204.0", block)
        self.assertIn("short 7455.0", block)
        self.assertIn("up $-0.275bn", block)
        self.assertIn("2026-07-28", block)

    def test_not_a_live_feed_is_disclosed(self):
        block = market_context.render_market_block(cta=self.LATEST)
        self.assertIn("not a live feed", block)

    def test_empty_latest_yields_nothing(self):
        self.assertEqual(market_context.render_market_block(cta={"latest": {}}), "")

    def test_none_values_in_triggers_are_dropped(self):
        block = market_context.render_market_block(cta={
            "latest": {"spx_triggers": {"short": 7455.0, "medium": None}}})
        self.assertIn("short 7455.0", block)
        self.assertNotIn("medium None", block)


class TestFedwatchLines(unittest.TestCase):
    PAYLOAD = {
        "as_of": "2026-08-20",
        "current": {"label": "4.25–4.50"},
        "meetings": [{"label": "Dec 2026", "cut_prob": 62.0,
                      "hold_prob": 35.0, "hike_prob": 3.0}],
    }

    def test_target_range_and_odds_appear(self):
        block = market_context.render_market_block(fedwatch=self.PAYLOAD)
        self.assertIn("Fed funds target range: 4.25–4.50%", block)
        self.assertIn("Dec 2026", block)
        self.assertIn("cut 62.0%", block)
        self.assertIn("hold 35.0%", block)
        self.assertIn("hike 3.0%", block)

    def test_no_current_range_yields_nothing(self):
        self.assertEqual(
            market_context.render_market_block(fedwatch={"meetings": []}), "")

    def test_no_meetings_still_renders_the_range(self):
        block = market_context.render_market_block(
            fedwatch={"current": {"label": "4.25-4.50"}})
        self.assertIn("Fed funds target range", block)
        self.assertNotIn("Market-implied odds", block)


class TestCombined(unittest.TestCase):
    def test_all_four_sources_combine_into_one_block(self):
        block = market_context.render_market_block(
            valuation=TestValuationLines.VALUATION,
            breadth={"pct_above_ma": {"50": {"values": [61]}}, "asof": "2026-08-28"},
            cta=TestCtaLines.LATEST,
            fedwatch=TestFedwatchLines.PAYLOAD,
        )
        for marker in ("trailing P/E", "breadth", "CTA positioning",
                       "Fed funds target range"):
            self.assertIn(marker, block)

    def test_output_is_ascii_safe_english(self):
        # Model input, not user-visible text -- deliberately not localised.
        block = market_context.render_market_block(
            valuation=TestValuationLines.VALUATION)
        self.assertIn("percentile", block)


if __name__ == "__main__":
    unittest.main()
