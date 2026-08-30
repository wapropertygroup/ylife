"""
Unit tests for ``ystocker.relative_strength`` — the pure render function
behind ``agents.build_relative_strength_context``.

No app, no network: every input is a hand-built payload shaped like
``analyst.peek()``'s real return value (``{"tickers": {...}, "asof": ...}``)
and a small ``PEER_GROUPS``-shaped dict. The point of this module is refusing
to render a "comparison" that isn't one -- a ticker with no covered peers
gets "", not a one-row table.
"""

from __future__ import annotations

import unittest

from ystocker import relative_strength as rs

NVDA_ROW = {
    "ticker": "NVDA",
    "eps_trend": {"+1y": {"chg30_pct": 3.2}},
    "eps_revisions": {"+1y": {"net30": 16, "up30": 18, "down30": 2}},
    "recommendations": [
        {"period": "0m", "strong_buy": 20, "buy": 12, "hold": 5, "sell": 1, "strong_sell": 1},
        {"period": "-3m", "strong_buy": 18, "buy": 10, "hold": 7, "sell": 2, "strong_sell": 1},
    ],
    "price_target": {"upside_pct": 14.1},
}
AVGO_ROW = {
    "ticker": "AVGO",
    "eps_trend": {"+1y": {"chg30_pct": 1.1}},
    "eps_revisions": {"+1y": {"net30": 5, "up30": 9, "down30": 4}},
    "recommendations": [
        {"strong_buy": 15, "buy": 9, "hold": 8, "sell": 1, "strong_sell": 0},
    ],
    "price_target": {"upside_pct": 9.8},
}


def _payload(**tickers):
    return {"tickers": tickers, "asof": "2026-08-28"}


class TestGating(unittest.TestCase):
    """Every path that cannot produce a real comparison must yield ""."""

    def test_no_ticker_yields_empty(self):
        self.assertEqual(rs.render_relative_strength_block("", _payload(), {}), "")
        self.assertEqual(rs.render_relative_strength_block(None, _payload(), {}), "")

    def test_non_mapping_payload_yields_empty(self):
        self.assertEqual(rs.render_relative_strength_block("NVDA", None, {}), "")
        self.assertEqual(rs.render_relative_strength_block("NVDA", "oops", {}), "")

    def test_ticker_not_covered_by_yahoo_yields_empty(self):
        payload = _payload(AVGO=AVGO_ROW)
        groups = {"Semiconductors": ["NVDA", "AVGO"]}
        self.assertEqual(
            rs.render_relative_strength_block("NVDA", payload, groups), "")

    def test_ticker_in_no_peer_group_yields_empty(self):
        payload = _payload(NVDA=NVDA_ROW)
        self.assertEqual(
            rs.render_relative_strength_block("NVDA", payload, {"Retail": ["WMT"]}), "")

    def test_peer_group_with_zero_covered_peers_yields_empty(self):
        # NVDA is covered and in a group, but every peer in that group is not.
        payload = _payload(NVDA=NVDA_ROW)
        groups = {"Semiconductors": ["NVDA", "AVGO", "AMD"]}
        self.assertEqual(
            rs.render_relative_strength_block("NVDA", payload, groups), "")

    def test_malformed_tickers_key_yields_empty(self):
        self.assertEqual(
            rs.render_relative_strength_block(
                "NVDA", {"tickers": "oops"}, {"X": ["NVDA"]}), "")


class TestNormalCase(unittest.TestCase):
    def setUp(self):
        self.payload = _payload(NVDA=NVDA_ROW, AVGO=AVGO_ROW)
        self.groups = {"Semiconductors": ["NVDA", "AVGO"],
                       "Tech": ["NVDA", "AVGO", "MSFT", "AAPL"]}

    def test_smallest_matching_group_is_chosen(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn('"Semiconductors"', block)
        self.assertNotIn('"Tech"', block)

    def test_both_rows_present(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn("| NVDA |", block)
        self.assertIn("| AVGO |", block)

    def test_eps_revision_cell_content(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn("+3.2% est. Δ30d", block)
        self.assertIn("net +16 revisions (18 up / 2 down)", block)

    def test_recs_shift_now_vs_prior(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        # now: buy=32 (20+12), hold=5, sell=2 (1+1); prior: buy=28 (18+10), hold=7, sell=3 (2+1)
        self.assertIn("Buy 28→32, Hold 7→5, Sell 3→2", block)

    def test_single_period_recs_says_no_prior(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn("(no prior period)", block)  # AVGO's row has one period

    def test_price_target_upside_cell(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn("+14.1%", block)
        self.assertIn("+9.8%", block)

    def test_asof_and_attribution_present(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn("2026-08-28", block)
        self.assertIn("Yahoo analyst-estimate data", block)

    def test_trailer_present(self):
        block = rs.render_relative_strength_block("NVDA", self.payload, self.groups)
        self.assertIn("not a ranking judgment", block)

    def test_missing_asof_omits_the_clause_not_the_block(self):
        payload = {"tickers": {"NVDA": NVDA_ROW, "AVGO": AVGO_ROW}}
        block = rs.render_relative_strength_block("NVDA", payload, self.groups)
        self.assertIn('"Semiconductors"', block)
        self.assertNotIn("Yahoo analyst-estimate data", block)


class TestPeerCap(unittest.TestCase):
    def test_max_peers_is_respected(self):
        tickers = {"NVDA": NVDA_ROW}
        group = ["NVDA"]
        for i in range(10):
            sym = f"PEER{i}"
            tickers[sym] = {**AVGO_ROW, "ticker": sym}
            group.append(sym)
        payload = {"tickers": tickers, "asof": "2026-08-28"}
        block = rs.render_relative_strength_block(
            "NVDA", payload, {"Big": group}, max_peers=3)
        # header + separator + ticker row + at most 3 peer rows
        row_lines = [l for l in block.splitlines() if l.startswith("| PEER")]
        self.assertEqual(len(row_lines), 3)


class TestMissingData(unittest.TestCase):
    def test_row_with_no_estimate_data_still_renders_em_dashes(self):
        payload = _payload(NVDA={"ticker": "NVDA"}, AVGO=AVGO_ROW)
        groups = {"Semiconductors": ["NVDA", "AVGO"]}
        block = rs.render_relative_strength_block("NVDA", payload, groups)
        self.assertIn("| NVDA | — | — | — |", block)

    def test_stale_target_is_labelled_not_hidden(self):
        row = {**AVGO_ROW, "price_target": {"upside_suspect": True}}
        payload = _payload(NVDA=NVDA_ROW, AVGO=row)
        groups = {"Semiconductors": ["NVDA", "AVGO"]}
        block = rs.render_relative_strength_block("NVDA", payload, groups)
        self.assertIn("n/a (stale target filtered)", block)


if __name__ == "__main__":
    unittest.main()
