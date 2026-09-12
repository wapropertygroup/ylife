"""
Tests for the reconstructed valuation history (``ystocker.dca_history``).

No app, no network, no disk, no clock. Every test builds statement blocks and a
price list by hand, which is what :func:`build_vintages` and :func:`reconstruct`
take plain dicts for.

The failure this file mostly exists to catch is **look-ahead bias**, because it
is invisible and it flatters. Step the denominator on the fiscal period-end and
January is priced on earnings nobody had yet; rank every week against the whole
series and a 2022 trough only registers as a trough because 2024 is in the
denominator. Either mistake makes the V line look prescient, neither changes its
shape enough to notice, and both feed a multiplier on real money.

The others:

* An inner join across the three statements silently shortens the window
  whenever Yahoo returns one of them a year deeper than the others — and it does.
* A negative denominator clamped instead of skipped puts a fabricated point into
  the distribution every other point is ranked against. A company with negative
  earnings has no P/E; it does not have a very high one.
* Yahoo signs capex negative. ``ocf - capex`` on an already-negative number
  reports a company burning cash as generating it, at double the rate.
* A row-label alias that stops matching degrades a factor to absent, which is
  correct — but only if the alias list is actually consulted in order.
"""
from __future__ import annotations

import unittest

from ystocker import dca, dca_history as dh


def _prices(start_year: int, weeks: int, base: float = 100.0,
            step: float = 0.0) -> list[tuple[str, float]]:
    """A weekly price list, ascending, starting on the first Monday of *start_year*."""
    from datetime import date, timedelta

    day = date(start_year, 1, 6)
    out = []
    for i in range(weeks):
        out.append(((day + timedelta(weeks=i)).isoformat(), round(base + step * i, 4)))
    return out


ANNUAL_INC = {
    "2020-12-31": {"Total Revenue": 100.0, "Diluted EPS": 4.0, "Net Income": 10.0, "EBITDA": 20.0},
    "2021-12-31": {"Total Revenue": 110.0, "Diluted EPS": 4.5, "Net Income": 11.0, "EBITDA": 22.0},
    "2022-12-31": {"Total Revenue": 125.0, "Diluted EPS": 5.0, "Net Income": 13.0, "EBITDA": 25.0},
    "2023-12-31": {"Total Revenue": 140.0, "Diluted EPS": 5.8, "Net Income": 15.0, "EBITDA": 28.0},
}
ANNUAL_BAL = {
    year: {"Ordinary Shares Number": 10.0, "Total Debt": 30.0,
           "Cash And Cash Equivalents": 12.0, "Tangible Book Value": 40.0}
    for year in ANNUAL_INC
}
ANNUAL_CFS = {
    "2020-12-31": {"Free Cash Flow": 8.0, "Depreciation And Amortization": 5.0},
    "2021-12-31": {"Free Cash Flow": 9.0, "Depreciation And Amortization": 5.0},
    "2022-12-31": {"Free Cash Flow": 11.0, "Depreciation And Amortization": 6.0},
    "2023-12-31": {"Free Cash Flow": 12.0, "Depreciation And Amortization": 6.0},
}


class PointInTime(unittest.TestCase):
    """A vintage is usable when it was published, not when the period ended."""

    def setUp(self):
        self.vintages = dh.build_vintages(ANNUAL_INC, ANNUAL_BAL, ANNUAL_CFS)

    def test_effective_date_lags_the_period_end(self):
        by_period = {v.period_end: v.effective for v in self.vintages}
        self.assertEqual(by_period["2023-12-31"], "2024-03-30")
        for period, effective in by_period.items():
            self.assertGreater(effective, period)

    def test_january_uses_last_years_filing_not_this_years_close(self):
        """The look-ahead regression, stated as a date.

        On 2024-01-15 the FY2023 10-K does not exist. Using it would price six
        weeks of January on earnings the market could not see.
        """
        picked = dh.vintage_at(self.vintages, "2024-01-15")
        self.assertEqual(picked.period_end, "2022-12-31")

    def test_after_the_lag_the_new_filing_takes_over(self):
        self.assertEqual(dh.vintage_at(self.vintages, "2024-04-01").period_end,
                         "2023-12-31")

    def test_before_the_first_filing_there_is_nothing(self):
        self.assertIsNone(dh.vintage_at(self.vintages, "2019-06-01"))

    def test_quarterly_lag_is_shorter_than_annual(self):
        self.assertLess(dh.QUARTERLY_LAG_DAYS, dh.ANNUAL_LAG_DAYS)
        q = dh.build_vintages({"2024-03-31": {"Diluted EPS": 1.0}}, {}, {},
                              kind="quarterly")
        self.assertEqual(q[0].effective, "2024-05-15")

    def test_an_unparseable_period_is_dropped_not_guessed(self):
        out = dh.build_vintages({"not-a-date": {"Diluted EPS": 1.0}}, {}, {})
        self.assertEqual(out, [])


class StatementUnion(unittest.TestCase):
    """Three statements of different depths must union, not intersect."""

    def test_a_period_missing_from_the_balance_sheet_still_produces_a_vintage(self):
        shallow = {"2023-12-31": ANNUAL_BAL["2023-12-31"]}
        out = dh.build_vintages(ANNUAL_INC, shallow, ANNUAL_CFS)
        self.assertEqual(len(out), 4, "an inner join would silently shorten the window")
        early = next(v for v in out if v.period_end == "2020-12-31")
        self.assertEqual(early.eps, 4.0)
        self.assertIsNone(early.shares, "no balance sheet means no share count, not zero")

    def test_row_aliases_are_tried_in_order(self):
        out = dh.build_vintages({"2023-12-31": {"Basic EPS": 3.0}}, {}, {})
        self.assertEqual(out[0].eps, 3.0)
        both = dh.build_vintages({"2023-12-31": {"Diluted EPS": 2.0, "Basic EPS": 3.0}}, {}, {})
        self.assertEqual(both[0].eps, 2.0, "Diluted is listed first and should win")

    def test_an_unrecognised_label_yields_absence_not_zero(self):
        out = dh.build_vintages({"2023-12-31": {"Some New Yahoo Label": 9.0}}, {}, {})
        self.assertIsNone(out[0].eps)


class FreeCashFlow(unittest.TestCase):
    """The capex sign trap."""

    def test_free_cash_flow_is_used_directly_when_present(self):
        out = dh.build_vintages({}, {}, {"2023-12-31": {"Free Cash Flow": 42.0}})
        self.assertEqual(out[0].fcf, 42.0)

    def test_negative_capex_is_added_not_subtracted(self):
        out = dh.build_vintages({}, {}, {"2023-12-31": {
            "Operating Cash Flow": 100.0, "Capital Expenditure": -30.0}})
        self.assertEqual(out[0].fcf, 70.0,
                         "subtracting an already-negative capex reports 130 and turns "
                         "a cash-consuming business into a generating one")

    def test_positive_capex_is_subtracted(self):
        out = dh.build_vintages({}, {}, {"2023-12-31": {
            "Operating Cash Flow": 100.0, "Capital Expenditure": 30.0}})
        self.assertEqual(out[0].fcf, 70.0)

    def test_ffo_is_net_income_plus_depreciation(self):
        out = dh.build_vintages({"2023-12-31": {"Net Income": 10.0}}, {},
                                {"2023-12-31": {"Depreciation And Amortization": 6.0}})
        self.assertEqual(out[0].ffo, 16.0)

    def test_ffo_needs_both_terms(self):
        out = dh.build_vintages({"2023-12-31": {"Net Income": 10.0}}, {}, {})
        self.assertIsNone(out[0].ffo)


class Reconstruction(unittest.TestCase):

    def setUp(self):
        self.vintages = dh.build_vintages(ANNUAL_INC, ANNUAL_BAL, ANNUAL_CFS)
        self.prices = _prices(2021, 200, base=60.0, step=0.25)
        self.series = dh.reconstruct(self.prices, self.vintages)

    def test_the_denominator_actually_moves(self):
        """The whole point: this must not be a rescaled price chart.

        If EPS were held constant, price/EPS would be perfectly correlated with
        price and every ratio between the two would be identical. Stepping the
        vintage breaks that, and the break is what makes the percentile a
        *valuation* percentile rather than a price one.
        """
        pe = dict(self.series["pe"])
        ratios = {round(pe[stamp] / close, 6)
                  for stamp, close in self.prices if stamp in pe}
        self.assertGreater(len(ratios), 1,
                           "one distinct price/PE ratio means the EPS never changed")

    def test_no_point_precedes_the_first_filing(self):
        first = min(v.effective for v in self.vintages)
        for stamp, _ in self.series["pe"]:
            self.assertGreaterEqual(stamp, first)

    def test_negative_earnings_are_skipped_not_clamped(self):
        loss = dict(ANNUAL_INC)
        loss["2022-12-31"] = {**loss["2022-12-31"], "Diluted EPS": -2.0}
        vintages = dh.build_vintages(loss, ANNUAL_BAL, ANNUAL_CFS)
        series = dh.reconstruct(self.prices, vintages)
        loss_vintage = next(v for v in vintages if v.period_end == "2022-12-31")
        covered = [stamp for stamp, _ in series.get("pe", [])
                   if loss_vintage.effective <= stamp < "2024-03-30"]
        self.assertEqual(covered, [],
                         "a loss-making year has no P/E; emitting one puts an "
                         "invented point in the distribution")

    def test_enterprise_value_uses_debt_and_cash(self):
        stamp, close = next((s, c) for s, c in self.prices if s >= "2024-04-01")
        expected_ev = close * 10.0 + 30.0 - 12.0
        got = dict(self.series["ev_ebitda"])[stamp]
        self.assertAlmostEqual(got, expected_ev / 28.0, places=3)

    def test_fcf_yield_and_pfcf_are_reciprocal(self):
        pfcf = dict(self.series["pfcf"])
        yld = dict(self.series["fcf_yield"])
        stamp = next(iter(pfcf))
        self.assertAlmostEqual(pfcf[stamp] * yld[stamp] / 100.0, 1.0, places=4)

    def test_a_zero_price_is_skipped(self):
        series = dh.reconstruct([("2024-04-01", 0.0)], self.vintages)
        self.assertEqual(series, {})

    def test_no_vintages_means_no_series(self):
        self.assertEqual(dh.reconstruct(self.prices, []), {})

    def test_cycle_adjusted_needs_several_vintages(self):
        """A mini-CAPE over one filing is just that filing."""
        thin = dh.build_vintages({"2023-12-31": ANNUAL_INC["2023-12-31"]},
                                 ANNUAL_BAL, ANNUAL_CFS)
        series = dh.reconstruct(_prices(2024, 60, base=80.0), thin)
        self.assertNotIn("cycle_adjusted", series)

    def test_growth_factors_need_a_prior_vintage(self):
        peg = dict(self.series.get("peg", []))
        first_effective = min(v.effective for v in self.vintages)
        for stamp in peg:
            self.assertGreater(stamp, first_effective)


class NoLookAhead(unittest.TestCase):
    """The percentile at week t must not depend on week t+1."""

    def setUp(self):
        # A series that rises monotonically, so a full-sample rank and an
        # expanding rank disagree loudly.
        self.rows = [(f"2020-{m:02d}-01", float(i))
                     for i, m in enumerate(range(1, 13), start=1)]
        self.rows += [(f"2021-{m:02d}-{d:02d}", float(100 + i))
                      for i, (m, d) in enumerate(
                          [(mm, 1) for mm in range(1, 13)] * 6, start=1)]

    def test_every_expanding_rank_uses_only_the_past(self):
        out = dh.percentile_series(self.rows, minimum=10)
        self.assertTrue(out)
        # On a monotonically rising series each new point is the highest so far,
        # so its expanding rank is always 100. A whole-sample rank would instead
        # walk from near 0 up to 100.
        self.assertTrue(all(pct == 100.0 for _stamp, pct in out),
                        "a rising series ranked point-in-time is always at its own "
                        "high; anything else means the future leaked in")

    def test_points_before_the_minimum_are_omitted(self):
        out = dh.percentile_series(self.rows, minimum=30)
        self.assertEqual(len(out), len(self.rows) - 29)

    def test_a_shorter_series_than_the_minimum_yields_nothing(self):
        self.assertEqual(dh.percentile_series(self.rows[:5], minimum=60), [])

    def test_the_last_expanding_rank_equals_the_headline_rank(self):
        """The line's endpoint and the factor table must agree.

        Two code paths compute "where is this multiple now" -- the last point of
        percentile_series and latest_percentiles -- and a reader sees both on one
        page. If they diverge the page contradicts itself.
        """
        series = {"pe": self.rows}
        line = dh.percentile_series(self.rows, minimum=10)
        latest = dh.latest_percentiles(series, minimum=10)
        self.assertEqual(line[-1][1], latest["pe"])


class VLine(unittest.TestCase):
    """Folding the ranked factors into a score, week by week."""

    def setUp(self):
        self.vintages = dh.build_vintages(ANNUAL_INC, ANNUAL_BAL, ANNUAL_CFS)
        self.series = dh.reconstruct(_prices(2021, 220, base=60.0, step=0.2),
                                     self.vintages)

    def test_the_line_uses_the_same_formula_as_the_headline(self):
        weights = dca.TEMPLATES["compounder"]
        line = dh.v_history(self.series, weights, minimum=20)
        self.assertTrue(line)
        for point in line:
            self.assertGreaterEqual(point["v"], 0.0)
            self.assertLessEqual(point["v"], 100.0)
            self.assertEqual(point["band"], dca.score_band(point["v"]))

    def test_the_peer_factor_is_simply_absent_and_renormalised_away(self):
        """Peer is cross-sectional; the line must not invent a history for it."""
        weights = dca.TEMPLATES["compounder"]
        self.assertIn("peer", weights)
        self.assertNotIn("peer", self.series)
        line = dh.v_history(self.series, weights, minimum=20)
        self.assertTrue(line, "dropping peer must not empty the line")

    def test_no_reconstructable_factor_means_no_line(self):
        self.assertEqual(dh.v_history({}, dca.TEMPLATES["reit"], minimum=20), [])

    def test_window_meta_reports_vintages_as_well_as_observations(self):
        meta = dh.window_meta(self.series, self.vintages)
        self.assertEqual(meta["vintages"], 4)
        self.assertGreater(meta["observations"], meta["vintages"])
        self.assertEqual(meta["basis"], "trailing")


class YearAgoGrowth(unittest.TestCase):
    """PEG and EV/Sales-growth need a *year's* growth, not the neighbour's.

    Once quarterly TTM vintages are interleaved with annual ones the adjacent
    vintage is often only three months back, so a naive ``current / previous``
    measures a quarter's growth and feeds it to PEG as if it were annual —
    ~3.7% where the truth is ~10%, inflating PEG about 2.7x and reading as a far
    more expensive stock. PEG carries 15–25% of the weight in three templates.

    The duplicate case is the other half: a quarterly TTM ending 31 Dec and the
    annual for the same year are the same period with different publication
    dates, and comparing them yields exactly 0% growth.
    """

    def _merged(self):
        inc = {f"{y}-12-31": {"Diluted EPS": e, "Total Revenue": r}
               for y, e, r in [(2021, 4.0, 100.0), (2022, 4.4, 110.0), (2023, 4.84, 121.0)]}
        bal = {k: {"Ordinary Shares Number": 10.0, "Total Debt": 0.0,
                   "Cash And Cash Equivalents": 0.0} for k in inc}
        # Eight quarters, which is what Yahoo actually returns, so the rolling
        # TTM windows span a year and a year-ago comparison exists. EPS compounds
        # at ~10% a year and ~2.4% a quarter, which is what makes a
        # quarter-on-quarter growth rate visibly wrong in the assertions below.
        quarters, eps, rev = {}, 1.0, 25.0
        for year, month_day in [(2022, "03-31"), (2022, "06-30"), (2022, "09-30"),
                                (2022, "12-31"), (2023, "03-31"), (2023, "06-30"),
                                (2023, "09-30"), (2023, "12-31"), (2024, "03-31")]:
            quarters[f"{year}-{month_day}"] = {"Diluted EPS": round(eps, 4),
                                               "Total Revenue": round(rev, 4)}
            eps *= 1.0241
            rev *= 1.0241
        annual = dh.build_vintages(inc, bal, {})
        quarterly = dh.build_quarterly_ttm(quarters)
        for q in quarterly:
            at = dh.vintage_at(annual, q.effective)
            if at is not None:
                for slot in ("debt", "cash", "shares"):
                    setattr(q, slot, getattr(at, slot))
        return sorted([*annual, *quarterly], key=lambda v: v.effective)

    def test_it_picks_the_vintage_a_year_back_not_the_neighbour(self):
        merged = self._merged()
        current = next(v for v in merged
                       if v.period_end == "2024-03-31" and v.kind == "quarterly")
        neighbour = merged[merged.index(current) - 1]
        prior = dh._year_ago(merged, current)
        self.assertIsNotNone(prior)
        self.assertIsNot(prior, neighbour,
                         "the adjacent vintage is one quarter back, not one year")
        gap_years = int(current.period_end[:4]) - int(prior.period_end[:4])
        self.assertEqual(gap_years, 1, "the comparison must span about a year")

    def test_a_same_period_duplicate_is_never_the_comparison(self):
        merged = self._merged()
        quarterly_dec = next(v for v in merged
                             if v.period_end == "2023-12-31" and v.kind == "quarterly")
        prior = dh._year_ago(merged, quarterly_dec)
        self.assertNotEqual(prior.period_end, "2023-12-31",
                            "the annual and quarterly for one year are the same period")
        self.assertEqual(prior.period_end, "2022-12-31")

    def test_no_vintage_in_the_window_yields_none_not_the_nearest(self):
        """Growth over the wrong interval is a different quantity, not a rough one."""
        lone = dh.build_vintages({"2023-12-31": {"Diluted EPS": 5.0}}, {}, {})
        self.assertIsNone(dh._year_ago(lone, lone[0]))

    def test_the_reconstructed_peg_uses_an_annual_growth_rate(self):
        merged = self._merged()
        series = dh.reconstruct(_prices(2022, 170, base=100.0), merged)
        peg = dict(series["peg"])
        pe = dict(series["pe"])
        stamp = max(peg)
        implied_growth = pe[stamp] / peg[stamp]
        # EPS compounds at 10% a year in this fixture; a quarter-on-quarter read
        # would land near 3.7 and a same-period duplicate would be excluded.
        self.assertGreater(implied_growth, 7.0,
                           f"implied growth {implied_growth:.2f}% looks like a "
                           f"quarterly rate, not an annual one")
        self.assertLess(implied_growth, 14.0)


class QuarterlyTTM(unittest.TestCase):
    """Quarterly EPS is a quarter's EPS, and using it raw is a 4x error.

    Yahoo's quarterly income statement reports the quarter, not a trailing twelve
    months. ``price / quarterly_eps`` reports a P/E about four times too high —
    90x where the truth is 23x — and because those points land in the same series
    as the annual ones, the reconstruction grows a sawtooth that reads as real
    multiple expansion and every percentile is then measured against it.
    """

    QUARTERS = {
        "2023-03-31": {"Diluted EPS": 1.0, "Total Revenue": 30.0, "Net Income": 3.0, "EBITDA": 6.0},
        "2023-06-30": {"Diluted EPS": 1.1, "Total Revenue": 32.0, "Net Income": 3.2, "EBITDA": 6.4},
        "2023-09-30": {"Diluted EPS": 1.2, "Total Revenue": 34.0, "Net Income": 3.4, "EBITDA": 6.8},
        "2023-12-31": {"Diluted EPS": 1.4, "Total Revenue": 38.0, "Net Income": 3.8, "EBITDA": 7.6},
        "2024-03-31": {"Diluted EPS": 1.5, "Total Revenue": 40.0, "Net Income": 4.0, "EBITDA": 8.0},
    }

    def test_flows_are_summed_over_four_quarters(self):
        out = dh.build_quarterly_ttm(self.QUARTERS)
        self.assertEqual(len(out), 2, "five quarters give two full TTM windows")
        first = out[0]
        self.assertEqual(first.period_end, "2023-12-31")
        self.assertAlmostEqual(first.eps, 1.0 + 1.1 + 1.2 + 1.4, places=6)
        self.assertAlmostEqual(first.revenue, 30 + 32 + 34 + 38, places=6)
        self.assertAlmostEqual(first.ebitda, 6.0 + 6.4 + 6.8 + 7.6, places=6)

    def test_the_ttm_eps_is_about_four_times_a_single_quarter(self):
        """The regression, stated as the ratio that would be wrong."""
        out = dh.build_quarterly_ttm(self.QUARTERS)
        single = self.QUARTERS["2023-12-31"]["Diluted EPS"]
        self.assertGreater(out[0].eps / single, 3.0)

    def test_stocks_are_not_summed(self):
        """Debt, cash, shares and book are balances; four of them is four companies."""
        out = dh.build_quarterly_ttm(self.QUARTERS)
        for v in out:
            self.assertIsNone(v.debt)
            self.assertIsNone(v.cash)
            self.assertIsNone(v.shares)
            self.assertIsNone(v.tangible_book)

    def test_fewer_than_four_quarters_yields_nothing(self):
        thin = dict(list(self.QUARTERS.items())[:3])
        self.assertEqual(dh.build_quarterly_ttm(thin), [])

    def test_a_gapped_window_is_skipped_rather_than_summed(self):
        gapped = {
            "2021-03-31": {"Diluted EPS": 1.0},
            "2023-06-30": {"Diluted EPS": 1.1},
            "2023-09-30": {"Diluted EPS": 1.2},
            "2023-12-31": {"Diluted EPS": 1.4},
        }
        self.assertEqual(dh.build_quarterly_ttm(gapped), [],
                         "four quarters spanning three years do not make a TTM")

    def test_a_missing_row_yields_absence_not_a_partial_sum(self):
        holey = {k: dict(v) for k, v in self.QUARTERS.items()}
        del holey["2023-06-30"]["Diluted EPS"]
        out = dh.build_quarterly_ttm(holey)
        self.assertIsNone(out[0].eps, "summing three of four quarters understates TTM")
        self.assertIsNotNone(out[0].revenue, "the other rows are still complete")

    def test_the_lag_is_the_quarterly_one(self):
        out = dh.build_quarterly_ttm(self.QUARTERS)
        self.assertEqual(out[0].effective, "2024-02-14")   # 2023-12-31 + 45d

    def test_quarterly_and_annual_eps_are_on_the_same_scale(self):
        """The two vintage kinds share one series, so they must be comparable.

        A quarterly point four times smaller than the annual ones either side of
        it is what produces the sawtooth.
        """
        annual = dh.build_vintages(ANNUAL_INC, ANNUAL_BAL, ANNUAL_CFS)
        quarterly = dh.build_quarterly_ttm(self.QUARTERS)
        annual_eps = [v.eps for v in annual if v.eps]
        for v in quarterly:
            self.assertGreater(v.eps, min(annual_eps) * 0.5)
            self.assertLess(v.eps, max(annual_eps) * 2.0)


class BuildBudget(unittest.TestCase):
    """One global limit on rebuilds, shared by the on-demand and swept paths.

    Per-symbol de-duplication bounds nothing when the symbols differ: a reader
    clicking through ticker pages triggers a fresh six-read burst each time, and
    twenty distinct names in eight minutes was measured on the first afternoon
    this shipped. The budget is what turns that into a queue.
    """

    def setUp(self):
        dh._inflight = 0
        dh._last_build_start = 0.0

    def tearDown(self):
        dh._inflight = 0
        dh._last_build_start = 0.0

    def test_the_first_reservation_succeeds(self):
        self.assertTrue(dh.try_reserve_build())

    def test_a_second_reservation_is_refused_by_the_gap(self):
        self.assertTrue(dh.try_reserve_build())
        self.assertFalse(dh.try_reserve_build(),
                         "two rebuilds must not start back to back")

    def test_the_gap_alone_refuses_even_with_slots_free(self):
        dh.release_build()          # nothing in flight at all
        self.assertTrue(dh.try_reserve_build())
        dh.release_build()
        self.assertFalse(dh.try_reserve_build(),
                         "an empty in-flight count does not license a burst")

    def test_inflight_is_capped_once_the_gap_has_passed(self):
        for _ in range(dh.MAX_INFLIGHT_BUILDS):
            dh._last_build_start = 0.0      # pretend the gap elapsed
            self.assertTrue(dh.try_reserve_build())
        dh._last_build_start = 0.0
        self.assertFalse(dh.try_reserve_build(),
                         f"more than {dh.MAX_INFLIGHT_BUILDS} concurrent rebuilds")

    def test_release_frees_a_slot(self):
        dh._last_build_start = 0.0
        self.assertTrue(dh.try_reserve_build())
        dh.release_build()
        dh._last_build_start = 0.0
        self.assertTrue(dh.try_reserve_build())

    def test_release_never_goes_negative(self):
        """A leaked or doubled release must not manufacture budget.

        A negative counter would silently raise the ceiling for ever, which is
        the failure mode of a limiter nobody would notice until Yahoo blocks.
        """
        for _ in range(5):
            dh.release_build()
        self.assertEqual(dh.build_budget()["inflight"], 0)

    def test_the_budget_is_reportable(self):
        b = dh.build_budget()
        self.assertEqual(b["max_inflight"], dh.MAX_INFLIGHT_BUILDS)
        self.assertEqual(b["min_gap_seconds"], dh.BUILD_MIN_GAP_SECONDS)


class Snapshots(unittest.TestCase):
    """The accumulating forward-basis row, without touching DynamoDB."""

    RECORD = {"PE (Forward)": 21.78, "PE (TTM)": 28.57, "EV/EBITDA": 19.9,
              "PEG": 1.64, "P/B Ratio": 8.62, "P/S Ratio": 11.49,
              "Dividend Yield (%)": 0.71, "Market Cap ($B)": 3813.2, "FCF ($B)": 16.5}

    def test_a_row_carries_the_forward_fields(self):
        row = dh.snapshot_row("MSFT", self.RECORD, stamp="2026-09-11")
        self.assertEqual(row["ticker"], "MSFT")
        self.assertEqual(row["date"], "2026-09-11")
        self.assertEqual(row["pe"], 21.78)
        self.assertEqual(row["dividend_yield"], 0.71)

    def test_derived_fields_are_computed_from_cap_and_fcf(self):
        row = dh.snapshot_row("MSFT", self.RECORD, stamp="2026-09-11")
        self.assertAlmostEqual(row["pfcf"], 3813.2 / 16.5, places=3)
        self.assertAlmostEqual(row["fcf_yield"], 16.5 / 3813.2 * 100, places=3)

    def test_an_empty_record_produces_no_row(self):
        """An empty row still occupies a date and lengthens the series falsely."""
        self.assertEqual(dh.snapshot_row("ZZZZ", {}, stamp="2026-09-11"), {})

    def test_missing_fields_are_omitted_rather_than_zeroed(self):
        row = dh.snapshot_row("X", {"PE (Forward)": 10.0}, stamp="2026-09-11")
        self.assertNotIn("ev_ebitda", row)
        self.assertNotIn("pfcf", row)

    def test_a_non_positive_market_cap_skips_the_derived_pair(self):
        row = dh.snapshot_row("X", {"PE (Forward)": 10.0, "Market Cap ($B)": 0.0,
                                    "FCF ($B)": 5.0}, stamp="2026-09-11")
        self.assertNotIn("pfcf", row)


class Filenames(unittest.TestCase):
    """The per-ticker cache path is built from a URL segment."""

    def test_real_symbols_survive(self):
        for symbol in ("MSFT", "BRK-B", "7203.T", "005930.KQ"):
            self.assertEqual(dh._safe_name(symbol), symbol.upper())

    def test_traversal_characters_are_stripped(self):
        self.assertNotIn("/", dh._safe_name("../../etc/passwd"))
        self.assertNotIn("\\", dh._safe_name("..\\windows"))

    def test_an_empty_name_never_yields_an_empty_path(self):
        self.assertTrue(dh._safe_name("///"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
