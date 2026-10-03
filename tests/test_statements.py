"""Tests for ystocker.statements -- Yahoo's statement tables as a fallback payload.

No network. The Toyota and Tencent figures are Yahoo's own, read on the box on
2026-10-03, so the tests describe the tables as Yahoo actually serves them:
a placeholder column of nothing at the end of every annual table, quarterly EPS
missing in some quarters, cash outflows negative, and a quote currency that is
not always the reporting one.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import statements, xbrl  # noqa: E402

D = date.fromisoformat
T = 1e12

Q = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
A = ["2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31"]


def cols(dates, values):
    return {d: v for d, v in zip(dates, values) if v is not None}


def toyota():
    frames = {
        "income_q": {
            "Total Revenue": cols(Q, [12.253326 * T, 12.377427 * T, 13.456852 * T, 12.597347 * T, 13.5254 * T]),
            "Gross Profit": cols(Q, [2.195813 * T, 1.968838 * T, 2.391405 * T, 1.907684 * T, 2.438247 * T]),
            "Operating Income": cols(Q, [1.166140 * T, 0.839550 * T, 1.191030 * T, 0.569490 * T, 1.063473 * T]),
            "Net Income Common Stockholders": cols(Q, [0.841350 * T, 0.932080 * T, 1.257460 * T, 0.817210 * T, 1.477040 * T]),
            "Diluted EPS": cols(Q, [64.56, None, None, None, 120.69]),
            "Diluted Average Shares": cols(Q, [13.03e9, None, None, None, 12.24e9]),
        },
        "income_a": {
            # Yahoo's annual tables end in a column of nothing (2022 here).
            "Total Revenue": cols(A, [None, 37.1543 * T, 45.09532 * T, 48.0367 * T, 50.68495 * T]),
            "Net Income Common Stockholders": cols(A, [None, 2.45132 * T, 4.94493 * T, 4.76509 * T, 3.8481 * T]),
            "Diluted EPS": cols(A, [None, 179.47, 365.94, 359.56, 295.25]),
        },
        "cash_q": {
            "Operating Cash Flow": cols(Q, [1.87648 * T, 1.06813 * T, 0.82513 * T, 1.70318 * T, 0.53655 * T]),
            "Capital Expenditure": cols(Q, [-1.24292 * T, -1.27221 * T, -1.31403 * T, -1.46419 * T, -1.30553 * T]),
            "Cash Dividends Paid": cols(Q, [-0.65245 * T, 0.0, -0.58653 * T, 0.0, -0.6517 * T]),
        },
        "cash_a": {
            "Operating Cash Flow": cols(A[1:], [2.95508 * T, 4.20637 * T, 3.69693 * T, 5.47292 * T]),
            "Capital Expenditure": cols(A[1:], [-3.70583 * T, -5.04839 * T, -5.25793 * T, -5.29335 * T]),
        },
        "balance_q": {
            "Cash And Cash Equivalents": cols(Q, [8.21086 * T, 8.11292 * T, 7.91891 * T, 12.65962 * T, 10.34308 * T]),
            "Total Debt": cols(Q, [38.44378 * T, 39.86428 * T, 42.12768 * T, 43.20547 * T, 43.95317 * T]),
        },
        "balance_a": {},
    }
    info = {"quoteType": "EQUITY", "currency": "JPY", "financialCurrency": "JPY",
            "longName": "Toyota Motor Corporation"}
    return frames, info


PRICES = [(D("2026-06-29"), 2950.0), (D("2026-09-28"), 3100.0)]


class ToyotaTests(unittest.TestCase):

    def setUp(self):
        frames, info = toyota()
        self.out = statements.build(frames, info, prices=PRICES)

    def test_basis_and_periods(self):
        self.assertEqual(self.out["basis"]["taxonomy"], "yahoo")
        self.assertEqual(self.out["basis"]["currency"], "JPY")
        self.assertEqual(self.out["quarterly"]["end"], Q)
        # The placeholder column carries no anchor value, so it is not a year.
        self.assertEqual(self.out["annual"]["end"], A[1:])
        self.assertEqual(self.out["annual"]["label"], ["FY2023", "FY2024", "FY2025", "FY2026"])

    def test_outflows_are_positive_payments(self):
        v = self.out["quarterly"]["values"]
        self.assertEqual(v["capex"][-1], 1.30553 * T)
        self.assertEqual(v["fcf"][-1], 0.53655 * T - 1.30553 * T)
        self.assertEqual(v["dividends"][1], 0.0)

    def test_ttm_and_margins(self):
        ttm = self.out["ttm"]["values"]
        self.assertAlmostEqual(ttm["revenue"][3], (12.253326 + 12.377427 + 13.456852 + 12.597347) * T, delta=1)
        self.assertIsNone(ttm["revenue"][2])
        self.assertAlmostEqual(self.out["quarterly"]["values"]["gross_margin"][-1],
                               round(2.438247 / 13.5254, 4))

    def test_missing_quarterly_eps_is_filled_from_the_nearest_share_count(self):
        # Yahoo drops EPS and the share count together in three of Toyota's
        # last five quarters; the nearest count fills them, flagged as computed.
        q = self.out["quarterly"]
        self.assertAlmostEqual(q["values"]["eps"][1], round(0.93208 * T / 13.03e9, 4))
        self.assertEqual(q["how"]["eps"]["1"], "calc")
        self.assertNotIn("0", q["how"]["eps"])          # filed EPS is untouched
        self.assertEqual(q["values"]["eps"][0], 64.56)

    def test_valuation_in_one_currency(self):
        val = self.out["valuation"]
        self.assertIsNotNone(val)
        ttm_eps = self.out["ttm"]["values"]["eps"][-1]
        self.assertIsNotNone(ttm_eps)
        self.assertEqual(val["pe"][-1], round(2950.0 / ttm_eps, 2))

    def test_no_split_rebasing(self):
        # Yahoo restates for splits itself (Toyota's 5-for-1 of 2021).
        self.assertEqual(self.out["annual"]["values"]["eps"][0], 179.47)

    def test_json(self):
        json.dumps(self.out)


class FallbackRuleTests(unittest.TestCase):

    def test_quote_currency_differs_from_reporting_currency(self):
        # Tencent: quoted in Hong Kong dollars, reports in renminbi.
        frames, _ = toyota()
        out = statements.build(frames, {"quoteType": "EQUITY", "currency": "HKD",
                                        "financialCurrency": "CNY"}, prices=PRICES)
        self.assertIsNone(out["valuation"])
        self.assertIn("valuation_currency", out["notes"])
        self.assertEqual(out["basis"]["currency"], "CNY")

    def test_an_etf_is_not_a_company(self):
        self.assertEqual(statements.build({}, {"quoteType": "ETF", "currency": "USD"}),
                         {"unavailable": "not_a_company"})

    def test_net_income_falls_back_to_the_plain_row(self):
        frames, info = toyota()
        frames["income_q"]["Net Income"] = frames["income_q"].pop("Net Income Common Stockholders")
        out = statements.build(frames, info, prices=PRICES)
        self.assertEqual(out["quarterly"]["values"]["net_income"][-1], 1.47704 * T)
        # The annual table still has the preferred row; provenance lists both.
        self.assertEqual(out["concepts"]["net_income"], ["Net Income", "Net Income Common Stockholders"])

    def test_empty_tables_are_named(self):
        out = statements.build({}, {"quoteType": "EQUITY", "currency": "JPY", "financialCurrency": "JPY"})
        self.assertEqual(out, {"unavailable": "no_statements"})

    def test_same_assembly_as_the_edgar_path(self):
        # One formula, one implementation: the TTM and margin arithmetic is
        # xbrl.assemble's whichever source fed it.
        self.assertIs(statements.xbrl.assemble, xbrl.assemble)


if __name__ == "__main__":
    unittest.main()
