"""Tests for ystocker.xbrl -- SEC companyfacts into quarterly/TTM/annual series.

No network, no app. The NVIDIA figures below are copied from its actual filings
(companyfacts for CIK 1045810, fetched 2026-10-03): fiscal 2025, the year the
10-for-1 split landed in the middle of. They pin the three derivations that do
the real work -- Q4 as the year minus nine months, a cash-flow quarter as the
difference of two year-to-date figures, and a per-share figure re-based by the
split that came after it was filed -- against numbers NVIDIA reported, rather
than against a snapshot of this module's own output.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import xbrl  # noqa: E402

D = date.fromisoformat
B = 1_000_000_000
SPLITS = [(D("2021-07-19"), 4.0), (D("2024-06-10"), 10.0)]


def row(start, end, val, filed, form="10-Q", fy=None, fp=None):
    out = {"end": end, "val": val, "filed": filed, "form": form, "fy": fy, "fp": fp,
           "accn": f"{filed}-{form}"}
    if start:
        out["start"] = start
    return out


def cf(taxonomy="us-gaap", **concepts):
    """A companyfacts document: concept=(unit, [rows])."""
    return {"facts": {taxonomy: {name: {"units": {unit: rows}}
                                 for name, (unit, rows) in concepts.items()}}}


# NVIDIA fiscal 2025 (ended 2025-01-26), plus the first quarter of fiscal 2026
# and fiscal 2024's year, exactly as filed -- including the comparatives a year
# later, which is where the split re-basing shows up.
FY25 = ("2024-01-29", "2025-01-26")
Q1 = ("2024-01-29", "2024-04-28")
H1 = ("2024-01-29", "2024-07-28")
Q2 = ("2024-04-29", "2024-07-28")
M9 = ("2024-01-29", "2024-10-27")
Q3 = ("2024-07-29", "2024-10-27")
Q1N = ("2025-01-27", "2025-04-27")
FY24 = ("2023-01-30", "2024-01-28")


def _flow(fy24, q1, h1, q2, m9, q3, fy25, q1n):
    return [
        row(*FY24, fy24, "2024-02-21", "10-K", 2024, "FY"),
        row(*FY24, fy24, "2025-02-26", "10-K", 2025, "FY"),
        row(*Q1, q1, "2024-05-29", "10-Q", 2025, "Q1"),
        row(*Q1, q1, "2025-05-28", "10-Q", 2026, "Q1"),
        row(*H1, h1, "2024-08-28", "10-Q", 2025, "Q2"),
        *([row(*Q2, q2, "2024-08-28", "10-Q", 2025, "Q2")] if q2 is not None else []),
        row(*M9, m9, "2024-11-20", "10-Q", 2025, "Q3"),
        *([row(*Q3, q3, "2024-11-20", "10-Q", 2025, "Q3")] if q3 is not None else []),
        row(*FY25, fy25, "2025-02-26", "10-K", 2025, "FY"),
        row(*Q1N, q1n, "2025-05-28", "10-Q", 2026, "Q1"),
    ]


# NVIDIA's operating income as filed (FY2025: $81,453M).
NVDA_OPERATING_INCOME = _flow(32_972_000_000, 16_909_000_000, 35_551_000_000, 18_642_000_000,
                              57_420_000_000, 21_869_000_000, 81_453_000_000, 21_638_000_000)


def nvda(**overrides):
    concepts = {
        "Revenues": ("USD", _flow(60_922_000_000, 26_044_000_000, 56_084_000_000, 30_040_000_000,
                                  91_166_000_000, 35_082_000_000, 130_497_000_000, 44_062_000_000)),
        "GrossProfit": ("USD", _flow(44_301_000_000, 20_406_000_000, 42_979_000_000, 22_574_000_000,
                                     69_135_000_000, 26_156_000_000, 97_858_000_000, 26_668_000_000)),
        "NetIncomeLoss": ("USD", _flow(29_760_000_000, 14_881_000_000, 31_480_000_000, 16_599_000_000,
                                       50_789_000_000, 19_309_000_000, 72_880_000_000, 18_775_000_000)),
        # Cash flow: year-to-date only, as a 10-Q files it.
        "NetCashProvidedByUsedInOperatingActivities": ("USD", _flow(
            28_090_000_000, 15_345_000_000, 29_833_000_000, None,
            47_460_000_000, None, 64_089_000_000, 27_414_000_000)),
        "PaymentsToAcquireProductiveAssets": ("USD", _flow(
            1_069_000_000, 369_000_000, 1_346_000_000, None,
            2_159_000_000, None, 3_236_000_000, 1_227_000_000)),
        # EPS around the split: Q1 FY2025 was filed at $5.98 on 2024-05-29,
        # twelve days before the 10-for-1, and restated at $0.60 a year later.
        "EarningsPerShareDiluted": ("USD/shares", [
            row(*FY24, 11.93, "2024-02-21", "10-K", 2024, "FY"),
            row(*FY24, 1.19, "2025-02-26", "10-K", 2025, "FY"),
            row(*Q1, 5.98, "2024-05-29", "10-Q", 2025, "Q1"),
            row(*Q1, 0.60, "2025-05-28", "10-Q", 2026, "Q1"),
            row(*H1, 1.27, "2024-08-28", "10-Q", 2025, "Q2"),
            row(*Q2, 0.67, "2024-08-28", "10-Q", 2025, "Q2"),
            row(*M9, 2.04, "2024-11-20", "10-Q", 2025, "Q3"),
            row(*Q3, 0.78, "2024-11-20", "10-Q", 2025, "Q3"),
            row(*FY25, 2.94, "2025-02-26", "10-K", 2025, "FY"),
            row(*Q1N, 0.76, "2025-05-28", "10-Q", 2026, "Q1"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": ("shares", [
            row(*FY24, 2_494_000_000, "2024-02-21", "10-K", 2024, "FY"),
            row(*FY24, 24_940_000_000, "2025-02-26", "10-K", 2025, "FY"),
            row(*Q1, 2_489_000_000, "2024-05-29", "10-Q", 2025, "Q1"),
            row(*Q1, 24_890_000_000, "2025-05-28", "10-Q", 2026, "Q1"),
            row(*H1, 24_869_000_000, "2024-08-28", "10-Q", 2025, "Q2"),
            row(*Q2, 24_848_000_000, "2024-08-28", "10-Q", 2025, "Q2"),
            row(*M9, 24_837_000_000, "2024-11-20", "10-Q", 2025, "Q3"),
            row(*Q3, 24_774_000_000, "2024-11-20", "10-Q", 2025, "Q3"),
            row(*FY25, 24_804_000_000, "2025-02-26", "10-K", 2025, "FY"),
            row(*Q1N, 24_611_000_000, "2025-05-28", "10-Q", 2026, "Q1"),
        ]),
        "LongTermDebt": ("USD", [
            row(None, "2024-01-28", 9_709_000_000, "2024-02-21", "10-K", 2024, "FY"),
            row(None, "2024-04-28", 9_710_000_000, "2024-05-29", "10-Q", 2025, "Q1"),
            row(None, "2024-07-28", 8_461_000_000, "2024-08-28", "10-Q", 2025, "Q2"),
            row(None, "2024-10-27", 8_462_000_000, "2024-11-20", "10-Q", 2025, "Q3"),
            row(None, "2025-01-26", 8_463_000_000, "2025-02-26", "10-K", 2025, "FY"),
            row(None, "2025-04-27", 8_464_000_000, "2025-05-28", "10-Q", 2026, "Q1"),
        ]),
        "CashAndCashEquivalentsAtCarryingValue": ("USD", [
            row(None, "2024-01-28", 7_280_000_000, "2024-02-21", "10-K", 2024, "FY"),
            row(None, "2024-04-28", 7_587_000_000, "2024-05-29", "10-Q", 2025, "Q1"),
            row(None, "2024-07-28", 8_563_000_000, "2024-08-28", "10-Q", 2025, "Q2"),
            row(None, "2024-10-27", 9_107_000_000, "2024-11-20", "10-Q", 2025, "Q3"),
            row(None, "2025-01-26", 8_589_000_000, "2025-02-26", "10-K", 2025, "FY"),
            row(None, "2025-04-27", 15_234_000_000, "2025-05-28", "10-Q", 2026, "Q1"),
        ]),
    }
    concepts.update(overrides)
    return cf(**{k: v for k, v in concepts.items() if v is not None})


PRICES = [(D("2025-01-20"), 142.62), (D("2025-04-21"), 106.43), (D("2025-09-29"), 186.58)]


def _idx(payload, end):
    return payload["quarterly"]["end"].index(end)


class ParseTests(unittest.TestCase):

    def test_only_statement_forms_are_read(self):
        # The proxy's pay-versus-performance table tags NetIncomeLoss too; left
        # in, it would be the "newest filing" of a year it only quotes.
        node = {"units": {"USD": [
            row(*FY25, 72_880_000_000, "2025-02-26", "10-K", 2025, "FY"),
            row(*FY25, 72_880_000_000, "2025-05-09", "DEF 14A"),
            row(*FY25, 70_000_000_000, "2025-03-01", "8-K"),
        ]}}
        facts = xbrl.parse_facts(node, "USD")
        self.assertEqual([f.form for f in facts], ["10-K"])

    def test_malformed_rows_are_skipped_not_raised(self):
        node = {"units": {"USD": [
            {"end": "2025-01-26", "val": "n/a", "filed": "2025-02-26", "form": "10-K"},
            {"end": "garbage", "val": 1, "filed": "2025-02-26", "form": "10-K"},
            {"end": "2025-01-26", "val": float("nan"), "filed": "2025-02-26", "form": "10-K"},
            {"end": "2025-01-26", "val": 5, "filed": "2025-02-26", "form": "10-K"},
        ]}}
        self.assertEqual([f.val for f in xbrl.parse_facts(node, "USD")], [5.0])

    def test_wrong_unit_reads_nothing(self):
        node = {"units": {"EUR": [row(*FY25, 1, "2025-02-26", "10-K")]}}
        self.assertEqual(xbrl.parse_facts(node, "USD"), [])


class SplitTests(unittest.TestCase):

    def test_factor_counts_only_splits_after_the_filing(self):
        self.assertEqual(xbrl.split_factor(D("2020-05-21"), SPLITS), 40.0)
        self.assertEqual(xbrl.split_factor(D("2024-05-29"), SPLITS), 10.0)
        self.assertEqual(xbrl.split_factor(D("2024-08-28"), SPLITS), 1.0)

    def test_reverse_split_composes(self):
        self.assertAlmostEqual(xbrl.split_factor(D("2020-01-01"), [(D("2021-01-01"), 0.1)]), 0.1)

    def test_rebase_divides_per_share_and_multiplies_counts(self):
        eps = xbrl.Fact(D("2024-01-29"), D("2024-04-28"), 5.98, D("2024-05-29"), "10-Q")
        shares = xbrl.Fact(D("2024-01-29"), D("2024-04-28"), 2.489 * B, D("2024-05-29"), "10-Q")
        self.assertAlmostEqual(xbrl.rebase([eps], SPLITS, "per_share")[0].val, 0.598)
        self.assertAlmostEqual(xbrl.rebase([shares], SPLITS, "average")[0].val, 24.89 * B)
        # A dollar amount is never re-based.
        self.assertEqual(xbrl.rebase([eps], SPLITS, "flow")[0].val, 5.98)


class QuarterlyFlowTests(unittest.TestCase):

    def facts(self, concept="Revenues"):
        unit, rows = nvda()["facts"]["us-gaap"][concept]["units"].copy().popitem()
        return xbrl.parse_facts({"units": {unit: rows}}, unit)

    def test_q4_is_the_year_minus_nine_months(self):
        q = xbrl.quarterly_flows(self.facts())
        p = q[D("2025-01-26")]
        self.assertEqual(p.val, 130_497_000_000 - 91_166_000_000)   # $39.331B, NVIDIA's Q4
        self.assertEqual(p.how, "q4")
        self.assertEqual(p.start, D("2024-10-28"))

    def test_filed_quarters_are_not_flagged(self):
        q = xbrl.quarterly_flows(self.facts())
        self.assertEqual(q[D("2024-07-28")].val, 30_040_000_000)
        self.assertIsNone(q[D("2024-07-28")].how)

    def test_cash_flow_quarters_come_from_year_to_date_differences(self):
        q = xbrl.quarterly_flows(self.facts("NetCashProvidedByUsedInOperatingActivities"))
        self.assertEqual(q[D("2024-04-28")].val, 15_345_000_000)
        self.assertIsNone(q[D("2024-04-28")].how)
        self.assertEqual((q[D("2024-07-28")].val, q[D("2024-07-28")].how), (14_488_000_000, "ytd"))
        self.assertEqual((q[D("2024-10-27")].val, q[D("2024-10-27")].how), (17_627_000_000, "ytd"))
        self.assertEqual((q[D("2025-01-26")].val, q[D("2025-01-26")].how), (16_629_000_000, "q4"))

    def test_year_minus_three_quarters_when_no_nine_month_figure(self):
        facts = [f for f in self.facts() if f.days not in (181, 272)]   # drop H1 and 9M
        p = xbrl.quarterly_flows(facts)[D("2025-01-26")]
        self.assertEqual(p.val, 130_497_000_000 - (26_044_000_000 + 30_040_000_000 + 35_082_000_000))
        self.assertEqual(p.how, "fyq")

    def test_trailing_twelve_month_columns_add_no_quarters(self):
        # Amazon's 10-Qs carry a twelve-months-ended column at every quarter.
        # It shares its start with a three-month figure nine months shorter,
        # never a quarter shorter, so it must not mint a quarter.
        facts = self.facts() + [xbrl.Fact(D("2024-04-29"), D("2025-04-27"), 148e9, D("2025-05-28"), "10-Q")]
        q = xbrl.quarterly_flows(facts)
        self.assertEqual(sorted(q), [D("2024-04-28"), D("2024-07-28"), D("2024-10-27"),
                                     D("2025-01-26"), D("2025-04-27")])
        self.assertEqual(q[D("2025-04-27")].val, 44_062_000_000)

    def test_sixteen_week_quarter_is_a_quarter(self):
        # Costco: 12-12-12-16 weeks. The fourth quarter is 112 days.
        s = "2024-09-02"
        facts = [
            xbrl.Fact(D(s), D("2024-11-24"), 62e9, D("2024-12-18"), "10-Q"),
            xbrl.Fact(D(s), D("2025-02-16"), 126e9, D("2025-03-12"), "10-Q"),
            xbrl.Fact(D(s), D("2025-05-11"), 189e9, D("2025-06-04"), "10-Q"),
            xbrl.Fact(D(s), D("2025-08-31"), 275e9, D("2025-10-08"), "10-K"),
        ]
        q = xbrl.quarterly_flows(facts)
        self.assertEqual(q[D("2025-08-31")].val, 86e9)
        self.assertEqual(q[D("2025-08-31")].how, "q4")

    def test_newest_filing_of_a_period_wins(self):
        facts = [
            xbrl.Fact(D("2024-01-29"), D("2024-04-28"), 26e9, D("2024-05-29"), "10-Q"),
            xbrl.Fact(D("2024-01-29"), D("2024-04-28"), 25e9, D("2025-05-28"), "10-Q"),
        ]
        self.assertEqual(xbrl.quarterly_flows(facts)[D("2024-04-28")].val, 25e9)


class AverageTests(unittest.TestCase):

    def test_q4_share_count_from_the_full_year_average(self):
        unit, rows = nvda()["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"].popitem()
        facts = xbrl.rebase(xbrl.parse_facts({"units": {unit: rows}}, unit), SPLITS, "average")
        q = xbrl.quarterly_averages(facts)
        p = q[D("2025-01-26")]
        self.assertEqual(p.how, "avg4")
        self.assertAlmostEqual(p.val, 4 * 24_804_000_000 - (24_890_000_000 + 24_848_000_000 + 24_774_000_000))
        # Averages are never differenced: H1 and 9M add no quarters of their own.
        self.assertEqual(q[D("2024-07-28")].val, 24_848_000_000)

    def test_implausible_identity_falls_back_to_the_year(self):
        s = "2024-01-01"
        facts = [
            xbrl.Fact(D(s), D("2024-03-31"), 100, D("2024-05-01"), "10-Q"),
            xbrl.Fact(D("2024-04-01"), D("2024-06-30"), 100, D("2024-08-01"), "10-Q"),
            xbrl.Fact(D("2024-07-01"), D("2024-09-30"), 100, D("2024-11-01"), "10-Q"),
            xbrl.Fact(D(s), D("2024-12-31"), 30, D("2025-02-01"), "10-K"),   # restated basis
        ]
        p = xbrl.quarterly_averages(facts)[D("2024-12-31")]
        self.assertEqual((p.how, p.val), ("fyavg", 30))


class SpliceTests(unittest.TestCase):

    @staticmethod
    def series(pairs):
        return {D(d): xbrl.Point(D(d), v) for d, v in pairs}

    def test_tag_switch_with_overlap_is_spliced(self):
        # Apple: SalesRevenueNet until 2018, the ASC 606 concept from 2017 on.
        new = self.series([("2017-12-30", 88.3), ("2018-03-31", 61.1), ("2018-06-30", 53.3)])
        old = self.series([("2017-09-30", 52.6), ("2017-12-30", 88.3), ("2018-03-31", 61.1)])
        merged, used = xbrl.splice([("SalesRevenueNet", old), ("RFC", new)])
        self.assertEqual(sorted(merged), [D("2017-09-30"), D("2017-12-30"), D("2018-03-31"), D("2018-06-30")])
        self.assertEqual(used[0], "RFC")       # the current tag is the base

    def test_component_is_never_stitched_onto_a_total(self):
        total = self.series([("2024-03-31", 21.3), ("2024-06-30", 25.5), ("2024-09-30", 25.2)])
        component = self.series([("2023-12-31", 21.6), ("2024-03-31", 17.4), ("2024-06-30", 19.9)])
        merged, used = xbrl.splice([("Revenues", total), ("Automotive", component)])
        self.assertNotIn(D("2023-12-31"), merged)
        self.assertEqual(used, ["Revenues"])

    def test_adjacent_without_overlap_needs_a_plausible_boundary(self):
        base = self.series([("2025-01-26", 39.0), ("2025-04-27", 41.0)])
        ok = self.series([("2024-07-28", 30.0), ("2024-10-27", 35.0)])
        bad = self.series([("2024-07-28", 3.0), ("2024-10-27", 3.5)])
        self.assertIn(D("2024-10-27"), xbrl.splice([("new", base), ("old", ok)])[0])
        self.assertNotIn(D("2024-10-27"), xbrl.splice([("new", base), ("old", bad)])[0])

    def test_a_later_splice_can_unlock_an_earlier_candidate(self):
        # Microsoft: Revenues (2007-10) only overlaps SalesRevenueNet (2009-18),
        # which only overlaps the ASC 606 concept (2016-).
        rev = self.series([("2008-03-31", 14.5), ("2009-06-30", 13.1)])
        srn = self.series([("2009-06-30", 13.1), ("2016-06-30", 22.6)])
        rfc = self.series([("2016-06-30", 22.6), ("2026-06-30", 90.0)])
        merged, used = xbrl.splice([("Revenues", rev), ("RFC", rfc), ("SalesRevenueNet", srn)])
        self.assertIn(D("2008-03-31"), merged)
        self.assertEqual(used, ["RFC", "SalesRevenueNet", "Revenues"])

    def test_restated_newest_overlap_does_not_block_the_older_fill(self):
        # Coca-Cola: the old tag agrees on the oldest shared quarters and not on
        # the newest, which a later filing restated. What is being filled is the
        # history before the overlap, so the oldest shared periods decide.
        new = self.series([("2017-09-29", 9.08), ("2018-03-30", 7.63), ("2018-06-29", 9.42),
                           ("2018-09-28", 8.78), ("2019-03-29", 8.69)])
        old = self.series([("2016-04-01", 10.28), ("2017-09-29", 9.08), ("2018-03-30", 7.63),
                           ("2018-06-29", 8.93), ("2018-09-28", 8.24)])
        merged, _ = xbrl.splice([("Revenues", new), ("SalesRevenueGoodsNet", old)])
        self.assertEqual(merged[D("2016-04-01")].val, 10.28)
        self.assertEqual(merged[D("2018-06-29")].val, 9.42)    # the base's copy is kept


class TrailingTests(unittest.TestCase):

    ENDS = [D("2024-04-28"), D("2024-07-28"), D("2024-10-27"), D("2025-01-26"), D("2025-04-27")]

    def test_sums_four_consecutive_quarters(self):
        out = xbrl.trailing(self.ENDS, [26.0, 30.0, 35.0, 39.0, 44.0])
        self.assertEqual(out, [None, None, None, 130.0, 148.0])

    def test_a_missing_quarter_breaks_the_window(self):
        self.assertEqual(xbrl.trailing(self.ENDS, [26.0, None, 35.0, 39.0, 44.0])[3:], [None, None])

    def test_a_gap_in_the_dates_breaks_the_window(self):
        ends = [D("2023-04-30"), D("2024-07-28"), D("2024-10-27"), D("2025-01-26")]
        self.assertEqual(xbrl.trailing(ends, [1.0, 1.0, 1.0, 1.0]), [None, None, None, None])


class BuildTests(unittest.TestCase):

    def setUp(self):
        self.out = xbrl.build(nvda(), splits=SPLITS, prices=PRICES)
        self.q = self.out["quarterly"]

    def test_periods_and_labels(self):
        self.assertEqual(self.q["end"], ["2024-04-28", "2024-07-28", "2024-10-27", "2025-01-26", "2025-04-27"])
        # From the filing that first reported each quarter, not the comparative
        # a year later (which carries the next fiscal year's fy).
        self.assertEqual(self.q["label"], ["Q1 FY2025", "Q2 FY2025", "Q3 FY2025", "Q4 FY2025", "Q1 FY2026"])
        self.assertEqual(self.out["annual"]["label"], ["FY2024", "FY2025"])

    def test_q4_values_and_flags(self):
        i = _idx(self.out, "2025-01-26")
        v = self.q["values"]
        self.assertEqual(v["revenue"][i], 39_331_000_000)
        self.assertEqual(v["net_income"][i], 22_091_000_000)
        self.assertAlmostEqual(v["eps"][i], 0.90)
        self.assertEqual(self.q["how"]["revenue"][str(i)], "q4")
        self.assertEqual(self.q["how"]["shares"][str(i)], "avg4")

    def test_eps_is_on_todays_share_basis(self):
        i = _idx(self.out, "2024-04-28")
        self.assertAlmostEqual(self.q["values"]["eps"][i], 0.60)
        self.assertEqual(self.q["values"]["shares"][i], 24_890_000_000)
        self.assertAlmostEqual(self.out["annual"]["values"]["eps"][0], 1.19)

    def test_original_pre_split_filing_alone_is_rebased(self):
        # Drop the post-split comparatives: the $5.98 as filed must come out at
        # $0.598, not $5.98 beside quarters of $0.67 and $0.78.
        rows = [r for r in nvda()["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
                if not (r["filed"] == "2025-05-28" and r["end"] == "2024-04-28")]
        out = xbrl.build(nvda(EarningsPerShareDiluted=("USD/shares", rows)), splits=SPLITS, prices=PRICES)
        self.assertAlmostEqual(out["quarterly"]["values"]["eps"][0], 0.598)

    def test_free_cash_flow_and_margins(self):
        i = _idx(self.out, "2025-01-26")
        v = self.q["values"]
        self.assertEqual(v["capex"][i], 1_077_000_000)
        self.assertEqual(v["fcf"][i], 16_629_000_000 - 1_077_000_000)
        self.assertNotIn("fcf", self.q["how"])
        self.assertAlmostEqual(v["gross_margin"][i], round((97_858 - 69_135) / 39_331, 4))
        self.assertEqual(v["debt"][i], 8_463_000_000)
        self.assertEqual(v["cash"][i], 8_589_000_000)

    def test_ttm_sums_and_ttm_margins(self):
        ttm = self.out["ttm"]["values"]
        self.assertEqual(ttm["revenue"][3], 130_497_000_000)   # Q1..Q4 sum to the year
        self.assertEqual(ttm["revenue"][4], 148_515_000_000)
        self.assertAlmostEqual(ttm["eps"][3], 2.95)
        self.assertIsNone(ttm["revenue"][2])
        self.assertAlmostEqual(ttm["net_margin"][3], round(72_880 / 130_497, 4))

    def test_valuation_on_trailing_figures(self):
        val = self.out["valuation"]
        i = _idx(self.out, "2025-01-26")
        self.assertEqual(val["price"][i], 142.62)
        self.assertEqual(val["pe"][i], round(142.62 / 2.95, 2))
        shares = self.q["values"]["shares"][i]
        self.assertEqual(val["ps"][i], round(142.62 * shares / 130_497_000_000, 2))
        self.assertIsNone(val["pe"][0])                         # no four quarters yet
        self.assertEqual(val["now"]["date"], "2025-09-29")
        self.assertEqual(val["now"]["basis_end"], "2025-04-27")

    def test_fcf_margin_in_every_view(self):
        i = _idx(self.out, "2025-01-26")
        v = self.q["values"]
        self.assertAlmostEqual(v["fcf_margin"][i], round((16_629 - 1_077) / 39_331, 4))
        ttm = self.out["ttm"]["values"]
        self.assertAlmostEqual(ttm["fcf_margin"][3], round(ttm["fcf"][3] / ttm["revenue"][3], 4))
        self.assertIn("fcf_margin", self.out["annual"]["values"])

    def test_enterprise_value_and_its_multiples(self):
        """EV = the quarter's market cap + debt - cash, over TTM revenue and
        TTM operating income."""
        out = xbrl.build(nvda(OperatingIncomeLoss=("USD", NVDA_OPERATING_INCOME)),
                         splits=SPLITS, prices=PRICES)
        val = out["valuation"]
        i = _idx(out, "2025-01-26")
        v, ttm = out["quarterly"]["values"], out["ttm"]["values"]
        self.assertEqual(ttm["operating_income"][i], 81_453_000_000)
        ev = 142.62 * v["shares"][i] + 8_463_000_000 - 8_589_000_000
        self.assertEqual(val["ev"][i], float(round(ev)))
        self.assertEqual(val["ev_sales"][i], round(ev / ttm["revenue"][i], 2))
        self.assertEqual(val["ev_ebit"][i], round(ev / ttm["operating_income"][i], 2))
        self.assertIsNone(val["ev_sales"][0])                   # no four quarters yet
        self.assertFalse(val["debt_assumed_zero"])
        self.assertIsNotNone(val["now"]["ev_sales"])

    def test_an_operating_loss_has_no_ev_ebit(self):
        rows = [dict(r, val=-abs(r["val"])) for r in NVDA_OPERATING_INCOME]
        out = xbrl.build(nvda(OperatingIncomeLoss=("USD", rows)), splits=SPLITS, prices=PRICES)
        self.assertTrue(all(v is None for v in out["valuation"]["ev_ebit"]))
        self.assertTrue(any(v is not None for v in out["valuation"]["ev_sales"]))

    def test_a_company_that_never_filed_debt_has_none(self):
        doc = nvda()
        for concept in list(doc["facts"]["us-gaap"]):
            if "Debt" in concept or "Borrowing" in concept:
                del doc["facts"]["us-gaap"][concept]
        out = xbrl.build(doc, splits=SPLITS, prices=PRICES)
        val = out["valuation"]
        self.assertTrue(val["debt_assumed_zero"])
        i = _idx(out, "2025-01-26")
        shares = out["quarterly"]["values"]["shares"][i]
        self.assertEqual(val["ev"][i], float(round(142.62 * shares - 8_589_000_000)))

    def test_negative_ttm_eps_has_no_pe(self):
        rows = [dict(r, val=-abs(r["val"])) for r in
                nvda()["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]]
        out = xbrl.build(nvda(EarningsPerShareDiluted=("USD/shares", rows)), splits=SPLITS, prices=PRICES)
        self.assertTrue(all(v is None for v in out["valuation"]["pe"]))

    def test_unknown_splits_withhold_per_share_lines(self):
        out = xbrl.build(nvda(), splits=None, prices=None)
        self.assertIn("splits_unknown", out["notes"])
        self.assertIn("valuation_no_prices", out["notes"])
        self.assertTrue(all(v is None for v in out["quarterly"]["values"]["eps"]))
        self.assertTrue(all(v is None for v in out["quarterly"]["values"]["shares"]))
        self.assertIsNone(out["valuation"])
        # Dollar lines do not depend on the split history.
        self.assertEqual(out["quarterly"]["values"]["revenue"][3], 39_331_000_000)

    def test_payload_is_json_and_every_flag_is_worded(self):
        json.dumps(self.out)
        for view in ("quarterly", "annual"):
            for flags in self.out[view]["how"].values():
                for code in flags.values():
                    self.assertIn(code, xbrl.HOW)

    def test_gross_profit_is_computed_where_none_is_filed(self):
        rev = nvda()["facts"]["us-gaap"]["Revenues"]
        gp = nvda()["facts"]["us-gaap"]["GrossProfit"]["units"]["USD"]
        cost = [dict(r, val=rv["val"] - g["val"]) for r, rv, g in
                zip(gp, rev["units"]["USD"], gp)]
        out = xbrl.build(nvda(GrossProfit=None, CostOfRevenue=("USD", cost)), splits=SPLITS, prices=PRICES)
        i = _idx(out, "2025-01-26")
        self.assertEqual(out["quarterly"]["values"]["gross_profit"][i], 28_723_000_000)
        self.assertEqual(out["quarterly"]["how"]["gross_profit"][str(i)], "calc")
        self.assertIn("revenue-cost", out["concepts"]["gross_profit"])

    def test_debt_from_parts_when_no_total_is_filed(self):
        nc = [row(None, "2025-01-26", 8_000_000_000, "2025-02-26", "10-K", 2025, "FY")]
        out = xbrl.build(nvda(LongTermDebt=None, LongTermDebtNoncurrent=("USD", nc)),
                         splits=SPLITS, prices=PRICES)
        i = _idx(out, "2025-01-26")
        # No current-portion line filed means none outstanding, not unknown.
        self.assertEqual(out["quarterly"]["values"]["debt"][i], 8_000_000_000)
        self.assertEqual(out["quarterly"]["how"]["debt"][str(i)], "calc")

    def test_derived_negative_revenue_is_dropped(self):
        rows = nvda()["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        rows = [dict(r, val=1_000_000) if r["end"] == "2025-01-26" else r for r in rows]
        out = xbrl.build(nvda(Revenues=("USD", rows)), splits=SPLITS, prices=PRICES)
        self.assertIsNone(out["quarterly"]["values"]["revenue"][_idx(out, "2025-01-26")])

    def test_an_insurer_gets_no_computed_gross_profit(self):
        # UnitedHealth files its pharmacy's cost of products as
        # CostOfGoodsAndServicesSold; revenue less that read as an 88% margin.
        rev = nvda()["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        cost = [dict(r, val=r["val"] * 0.12) for r in rev]
        premiums = [dict(r, val=r["val"] * 0.8) for r in rev]
        out = xbrl.build(nvda(GrossProfit=None, CostOfGoodsAndServicesSold=("USD", cost),
                              PremiumsEarnedNet=("USD", premiums)), splits=SPLITS, prices=PRICES)
        self.assertTrue(all(v is None for v in out["quarterly"]["values"]["gross_profit"]))
        self.assertTrue(all(v is None for v in out["quarterly"]["values"]["gross_margin"]))

    def test_a_component_cost_line_gets_no_computed_gross_profit(self):
        # McDonald's CostOfGoodsAndServicesSold is 18% of its total costs;
        # revenue less it read as a 90% gross margin.
        rev = nvda()["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        cost = [dict(r, val=r["val"] * 0.09) for r in rev]
        total = [dict(r, val=r["val"] * 0.5) for r in rev]
        out = xbrl.build(nvda(GrossProfit=None, CostOfGoodsAndServicesSold=("USD", cost),
                              CostsAndExpenses=("USD", total)), splits=SPLITS, prices=PRICES)
        self.assertTrue(all(v is None for v in out["quarterly"]["values"]["gross_profit"]))
        # ...while a cost line that is most of total costs is the real thing.
        cost = [dict(r, val=r["val"] * 0.3) for r in rev]
        out = xbrl.build(nvda(GrossProfit=None, CostOfGoodsAndServicesSold=("USD", cost),
                              CostsAndExpenses=("USD", total)), splits=SPLITS, prices=PRICES)
        self.assertAlmostEqual(out["quarterly"]["values"]["gross_margin"][0], 0.7)

    def test_debt_filed_as_notes_payable(self):
        # Oracle: LongTermNotesAndLoans in its 10-Qs, NotesPayableCurrent beside it.
        nc = [row(None, "2025-04-27", 99_980_000_000, "2025-05-28", "10-Q", 2026, "Q1")]
        cur = [row(None, "2025-04-27", 8_090_000_000, "2025-05-28", "10-Q", 2026, "Q1")]
        out = xbrl.build(nvda(LongTermDebt=None, LongTermNotesAndLoans=("USD", nc),
                              NotesPayableCurrent=("USD", cur)), splits=SPLITS, prices=PRICES)
        self.assertEqual(out["quarterly"]["values"]["debt"][_idx(out, "2025-04-27")], 108_070_000_000)

    def test_a_stray_total_cannot_override_the_parts(self):
        # Oracle again: one LongTermDebt fact of zero, in 2022, beside a
        # quarterly series of notes. The lone total must not become the series.
        nc = [row(None, e, 90e9, f, "10-Q") for e, f in
              (("2024-04-28", "2024-05-29"), ("2024-07-28", "2024-08-28"), ("2025-04-27", "2025-05-28"))]
        stray = [row(None, "2024-07-28", 0, "2024-08-28", "10-K")]
        out = xbrl.build(nvda(LongTermDebt=("USD", stray), LongTermNotesAndLoans=("USD", nc)),
                         splits=SPLITS, prices=PRICES)
        self.assertEqual(out["quarterly"]["values"]["debt"][_idx(out, "2024-07-28")], 90e9)

    def test_share_count_implied_where_none_is_filed(self):
        # Alphabet: a company-wide diluted count only from 2023, per class before.
        rows = [r for r in nvda()["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]
                ["units"]["shares"] if r["end"] >= "2025-01-01"]
        out = xbrl.build(nvda(WeightedAverageNumberOfDilutedSharesOutstanding=("shares", rows)),
                         splits=SPLITS, prices=PRICES)
        i = _idx(out, "2024-07-28")
        self.assertAlmostEqual(out["quarterly"]["values"]["shares"][i], 16_599_000_000 / 0.67, delta=1)
        self.assertEqual(out["quarterly"]["how"]["shares"][str(i)], "calc")
        self.assertIn("net-income/eps", out["concepts"]["shares"])


class BasisTests(unittest.TestCase):

    def test_convenience_translation_does_not_become_the_currency(self):
        # TSMC files every year in NT$ and the latest one again in US$ at a
        # single fixed rate; the second is not a history.
        rows_twd = [row(f"{y}-01-01", f"{y}-12-31", 1e12 + y, f"{y + 1}-04-17", "20-F", y, "FY")
                    for y in range(2015, 2025)]
        rows_usd = [row("2024-01-01", "2024-12-31", 9e10, "2025-04-17", "20-F", 2024, "FY")]
        doc = {"facts": {"ifrs-full": {"Revenue": {"units": {"TWD": rows_twd, "USD": rows_usd}}}}}
        self.assertEqual(xbrl.choose_basis(doc), ("ifrs-full", "TWD"))

    def test_annual_only_foreign_filer_has_no_valuation(self):
        rows = [row(f"{y}-01-01", f"{y}-12-31", 20e9 + y * 1e6, f"{y + 1}-02-25", "20-F", y, "FY")
                for y in range(2019, 2026)]
        out = xbrl.build(cf(RevenueFromContractWithCustomerExcludingAssessedTax=("EUR", rows)),
                         splits=[], prices=PRICES)
        self.assertIn("annual_only", out["notes"])
        self.assertEqual(out["basis"], {"taxonomy": "us-gaap", "currency": "EUR", "filer": "foreign"})
        self.assertIsNone(out["valuation"])
        self.assertEqual(out["annual"]["label"][-1], "FY2025")

    def test_dollar_20f_filer_still_gets_no_valuation(self):
        # Per-ordinary-share EPS beside a per-ADR price is the TSM P/E of 1.01.
        doc = nvda()
        for concept in doc["facts"]["us-gaap"].values():
            for rows in concept["units"].values():
                for r in rows:
                    r["form"] = "20-F" if r["form"] == "10-K" else "6-K"
        out = xbrl.build(doc, splits=SPLITS, prices=PRICES)
        self.assertIsNone(out["valuation"])

    def test_nothing_to_chart_is_named(self):
        self.assertEqual(xbrl.build({"facts": {}}), {"unavailable": "no_statements"})
        self.assertEqual(xbrl.build(cf(Cash=("USD", [row(None, "2025-01-26", 1, "2025-02-26", "10-K")]))),
                         {"unavailable": "no_statements"})


class HelperTests(unittest.TestCase):

    QE = [D("2024-04-28"), D("2024-07-28"), D("2024-10-27"), D("2025-01-26"), D("2025-04-27")]

    def test_growth_is_on_the_same_quarter_a_year_earlier_by_date(self):
        self.assertEqual(xbrl.growth(self.QE, [26.0, 30.0, 35.0, 39.0, 52.0]),
                         [None, None, None, None, 1.0])
        # The year-earlier quarter missing: no growth, not a neighbour's.
        ends = [D("2024-01-28"), D("2024-07-28"), D("2024-10-27"), D("2025-01-26"), D("2025-04-27")]
        self.assertIsNone(xbrl.growth(ends, [26.0, 30.0, 35.0, 39.0, 52.0])[4])
        # Growth from a loss is not a percentage.
        self.assertIsNone(xbrl.growth(self.QE, [-2.0, 1, 1, 1, 3.0])[4])

    def test_annual_growth_steps_one_year(self):
        ends = [D("2024-01-28"), D("2025-01-26"), D("2026-01-25")]
        self.assertEqual(xbrl.growth(ends, [60.0, 130.0, 260.0], annual=True), [None, 1.1667, 1.0])

    def test_roe_is_trailing_income_on_average_equity(self):
        equity = [40.0, 45.0, 50.0, 55.0, 60.0]
        income = [None, None, None, 80.0, 100.0]
        roe = xbrl.return_on_equity(self.QE, income, equity)
        self.assertEqual(roe[3], round(80.0 / 55.0, 4))  # no year-earlier end: the end alone
        self.assertEqual(roe[4], round(100.0 / ((60.0 + 40.0) / 2), 4))

    def test_negative_equity_has_no_roe(self):
        # McDonald's and Starbucks have bought back past their book value.
        roe = xbrl.return_on_equity(self.QE, [5.0] * 5, [-2.0, -1.8, -1.5, -1.3, -1.0])
        self.assertEqual(roe, [None] * 5)

    def test_dividends_per_share_derive_q4_rebase_and_keep_their_cents(self):
        dps = [row(*Q1, 0.04, "2024-05-29", "10-Q", 2025, "Q1"),            # pre-split, as filed
               row(*H1, 0.05, "2024-08-28", "10-Q", 2025, "Q2"),
               row(*M9, 0.06, "2024-11-20", "10-Q", 2025, "Q3"),
               row(*FY25, 0.07, "2025-02-26", "10-K", 2025, "FY")]
        out = xbrl.build(nvda(CommonStockDividendsPerShareDeclared=("USD/shares", dps)),
                         splits=SPLITS, prices=PRICES)
        q = out["quarterly"]
        self.assertAlmostEqual(q["values"]["dps"][0], 0.004)                # 0.04 / 10
        self.assertAlmostEqual(q["values"]["dps"][3], 0.01)                 # FY - 9M, after the split
        self.assertEqual(q["how"]["dps"]["3"], "q4")
        self.assertIn("dps", xbrl.PER_SHARE_METRICS)

    def test_equity_and_roe_in_a_build(self):
        eq = [row(None, e, v, f, "10-Q") for e, v, f in (
            ("2024-04-28", 60e9, "2024-05-29"), ("2025-01-26", 79e9, "2025-02-26"),
            ("2025-04-27", 83e9, "2025-05-28"))]
        out = xbrl.build(nvda(StockholdersEquity=("USD", eq)), splits=SPLITS, prices=PRICES)
        q = out["quarterly"]
        i = q["end"].index("2025-04-27")
        ttm_ni = out["ttm"]["values"]["net_income"][i]
        self.assertEqual(q["values"]["roe"][i], round(ttm_ni / ((83e9 + 60e9) / 2), 4))
        self.assertEqual(out["ttm"]["values"]["roe"], q["values"]["roe"])
        self.assertEqual(q["values"]["equity"][i], 83e9)

    def test_a_year_seen_only_as_a_comparative_is_not_named_after_its_filing(self):
        # TSMC's first XBRL 20-F is fiscal 2017 and carries 2015 and 2016 as
        # comparatives, every one tagged fy=2017.
        facts = [xbrl.Fact(D(f"{y}-01-01"), D(f"{y}-12-31"), 1.0, D(f), "20-F", fy, "FY", acc)
                 for y, f, fy, acc in ((2015, "2018-04-20", 2017, "a17"), (2016, "2018-04-20", 2017, "a17"),
                                       (2017, "2018-04-20", 2017, "a17"), (2018, "2019-04-20", 2018, "a18"))]
        years = xbrl.fiscal_years([facts])
        self.assertEqual({e.year: fy for e, (_s, fy) in years.items()},
                         {2015: 2015, 2016: 2016, 2017: 2017, 2018: 2018})

    def test_a_company_naming_offset_carries_to_unnamed_years(self):
        # Home Depot: the year to 2026-02-01 is fiscal 2025.
        facts = [xbrl.Fact(D("2024-02-05"), D("2025-02-02"), 1.0, D("2025-03-20"), "10-K", 2024, "FY", "h24"),
                 xbrl.Fact(D("2023-01-30"), D("2024-01-28"), 1.0, D("2025-03-20"), "10-K", 2024, "FY", "h24"),
                 xbrl.Fact(D("2025-02-03"), D("2026-02-01"), 1.0, D("2026-03-19"), "10-K", 2025, "FY", "h25")]
        years = xbrl.fiscal_years([facts])
        self.assertEqual(years[D("2024-01-28")][1], 2023)
        self.assertEqual(years[D("2026-02-01")][1], 2025)

    def test_quarter_labels_follow_the_fiscal_year(self):
        years = {D("2025-08-31"): (D("2024-09-02"), 2025)}      # Costco: 12-12-12-16 weeks
        ends = [D("2024-11-24"), D("2025-02-16"), D("2025-05-11"), D("2025-08-31"),
                D("2025-11-23"), D("2026-02-15")]                # the last two: no 10-K yet
        self.assertEqual(xbrl.quarter_labels(ends, years), {
            D("2024-11-24"): "Q1 FY2025", D("2025-02-16"): "Q2 FY2025", D("2025-05-11"): "Q3 FY2025",
            D("2025-08-31"): "Q4 FY2025", D("2025-11-23"): "Q1 FY2026", D("2026-02-15"): "Q2 FY2026"})

    def test_gap_is_filled_with_empty_quarters(self):
        ends = [D("2020-03-31"), D("2021-03-31")]
        filled = xbrl._fill_gaps(ends)
        self.assertEqual(filled[0], D("2020-03-31"))
        self.assertEqual(filled[-1], D("2021-03-31"))
        self.assertEqual(len(filled), 5)

    def test_price_on_uses_the_last_close_and_refuses_a_stale_one(self):
        self.assertEqual(xbrl.price_on(PRICES, D("2025-01-26")), 142.62)
        self.assertIsNone(xbrl.price_on(PRICES, D("2025-03-31")))
        self.assertIsNone(xbrl.price_on(PRICES, D("2024-12-31")))

    def test_near_duplicate_period_ends_collapse(self):
        a = {D("2024-12-31"): xbrl.Point(D("2024-12-31"), 1.0)}
        b = {D("2024-12-29"): xbrl.Point(D("2024-12-29"), 2.0)}
        ends = xbrl._canonical_ends([a, b])
        self.assertEqual(ends, [D("2024-12-31")])
        self.assertEqual(xbrl._align(b, ends)[D("2024-12-31")].val, 2.0)


if __name__ == "__main__":
    unittest.main()
