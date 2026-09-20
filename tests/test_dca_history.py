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
from datetime import date, timedelta

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

    def test_the_forward_pfcf_is_banked_beside_the_trailing_one(self):
        """The series cannot be backfilled, so a forward factor that is not
        banked today can never be ranked on its own basis later."""
        row = dh.snapshot_row("MSFT", self.RECORD, stamp="2026-09-11")
        self.assertIn("forward_pfcf", row)
        # Growth is implied by PE_ttm / PE_fwd, so the forward multiple is the
        # trailing one divided by it — cheaper for a company expected to grow.
        self.assertLess(row["forward_pfcf"], row["pfcf"])

    def test_a_cash_burner_banks_no_forward_pfcf(self):
        """`fcf.estimate` refuses a negative trailing FCF rather than scaling
        it, and a refusal must not be banked as a number."""
        record = dict(self.RECORD, **{"FCF ($B)": -4.0})
        row = dh.snapshot_row("X", record, stamp="2026-09-11")
        self.assertNotIn("forward_pfcf", row)
        self.assertNotIn("pfcf", row)


class ForwardBasis(unittest.TestCase):
    """Scoring a forward multiple against forward history, or not at all.

    The fallback is the whole design: ranking today's forward P/E against the
    *trailing* reconstruction moved the percentile by a measured median of −14.6
    points across the live universe, so a factor with no forward distribution
    must stay on the reconstruction rather than borrow it.
    """

    PAYLOAD = {
        "forward_context": {"forwardPE": 9.0, "trailingPE": 14.0,
                            "marketCap": 100e9},
        "vintages": [
            {"kind": "annual", "period_end": "2024-11-29", "fcf": 8.0e9},
            {"kind": "annual", "period_end": "2025-11-28", "fcf": 10.0e9},
            # build() copies the annual figure onto quarterly vintages, so a
            # naive "last vintage" would read this and learn nothing new.
            {"kind": "quarterly", "period_end": "2026-05-29", "fcf": 10.0e9},
        ],
        "series": {"pe": [("2026-09-14", 14.24)], "pfcf": [("2026-09-14", 10.4)]},
        "percentiles": {"pe": 2.1, "pfcf": 8.6},
    }

    def test_forward_multiples_read_only_what_build_stored(self):
        out = dh.forward_multiples(self.PAYLOAD)
        self.assertAlmostEqual(out["pe"], 9.0)
        # 100e9 / (10e9 * 14/9) = 6.43x, cheaper than the trailing 10x.
        self.assertAlmostEqual(out["pfcf"], 6.43, places=1)

    def test_the_annual_vintage_is_used_not_the_copied_quarterly_one(self):
        """Same trap `dcf_inputs` documents: the quarterly rows carry a copy of
        the annual FCF, so taking the last of any kind repeats one year."""
        payload = dict(self.PAYLOAD)
        payload["vintages"] = [*self.PAYLOAD["vintages"],
                               {"kind": "quarterly", "period_end": "2026-08-29",
                                "fcf": 99.0e9}]
        self.assertAlmostEqual(dh.forward_multiples(payload)["pfcf"],
                               dh.forward_multiples(self.PAYLOAD)["pfcf"], places=6)

    def test_no_forward_pe_yields_no_forward_factor(self):
        payload = dict(self.PAYLOAD, forward_context={"marketCap": 100e9})
        self.assertNotIn("pe", dh.forward_multiples(payload))

    def test_only_genuinely_forward_banked_fields_become_distributions(self):
        """The banked `pfcf` divides by *trailing* FCF and `pe_ttm` is trailing
        by name; admitting either would store the basis error."""
        rows = [{"date": "2026-09-12", "pe": 9.1, "pe_ttm": 14.1,
                 "pfcf": 10.4, "forward_pfcf": 6.4, "peg": 0.6}]
        dists = dh.forward_distributions(rows)
        self.assertEqual(sorted(dists), ["pe", "pfcf"])
        self.assertEqual(dists["pfcf"], [6.4])      # the forward one, not 10.4
        self.assertEqual(dists["pe"], [9.1])

    def test_a_short_distribution_falls_back_rather_than_ranking(self):
        """Eight rows can only return eight answers. Silence here is the
        fallback working, not a failure."""
        dists = {"pe": [9.0] * 8}
        self.assertEqual(
            dh.forward_basis(self.PAYLOAD, dists, minimum=60), {})

    def test_a_long_distribution_ranks_forward_against_forward(self):
        dists = {"pe": [float(v) for v in range(5, 65)]}     # 60 points, 5..64
        out = dh.forward_basis(self.PAYLOAD, dists, minimum=60)
        self.assertIn("pe", out)
        self.assertEqual(out["pe"]["observations"], 60)
        self.assertAlmostEqual(out["pe"]["value"], 9.0)
        # 9.0 sits low in 5..64, so it ranks cheap on its own basis.
        self.assertLess(out["pe"]["percentile"], 15.0)
        # What it replaced travels with it, so the switch is visible.
        self.assertEqual(out["pe"]["trailing_percentile"], 2.1)

    def test_a_factor_without_a_forward_distribution_is_simply_absent(self):
        """Per factor, not per ticker: P/E can switch while P/FCF waits."""
        dists = {"pe": [float(v) for v in range(5, 65)]}
        out = dh.forward_basis(self.PAYLOAD, dists, minimum=60)
        self.assertNotIn("pfcf", out)

    def test_context_reports_both_multiples_even_when_nothing_can_rank(self):
        """A reader must be able to tell "the engine has no forward number"
        from "it has one and is declining to rank it"."""
        ctx = dh.forward_context_values(self.PAYLOAD)
        self.assertAlmostEqual(ctx["pe"]["forward"], 9.0)
        self.assertAlmostEqual(ctx["pe"]["trailing"], 14.24)


class ForwardReconstruction(unittest.TestCase):
    """Price over the earnings of the year *ahead*, for the historical half.

    The look-ahead is deliberate and is the only way to rank a forward multiple
    against forward history before the banked series matures. It is kept out of
    `reconstruct()` precisely so nobody inherits it by accident.
    """

    def _vintages(self):
        V = dh.Vintage
        return [
            # period_end / effective are ~90 days apart, as ANNUAL_LAG_DAYS.
            V(period_end="2022-12-31", effective="2023-03-31", kind="annual",
              eps=10.0, fcf=1000.0, shares=100.0),
            V(period_end="2023-12-31", effective="2024-03-31", kind="annual",
              eps=12.0, fcf=1200.0, shares=100.0),
            V(period_end="2024-12-31", effective="2025-03-31", kind="annual",
              eps=15.0, fcf=1500.0, shares=100.0),
        ]

    def test_it_divides_by_the_year_ahead_not_the_year_reported(self):
        out = dh.reconstruct_forward([("2023-06-30", 120.0)], self._vintages())
        # At 2023-06-30 the 2022 book is public and the market is estimating
        # FY2023, whose EPS turned out to be 12.0 → 120/12 = 10.0, not 120/10.
        self.assertAlmostEqual(out["pe"][0][1], 10.0)

    def test_it_keys_on_publication_not_period_end(self):
        """Between a year closing and its 10-K landing, the forward figure still
        refers to the next year — `effective` is what captures that."""
        # 2024-01-15: FY2023 has *ended* but not been filed, so the year under
        # estimate is still FY2023 (eps 12.0), not FY2024.
        out = dh.reconstruct_forward([("2024-01-15", 120.0)], self._vintages())
        self.assertAlmostEqual(out["pe"][0][1], 10.0)

    def test_weeks_with_no_reported_year_ahead_drop_out(self):
        """The last ~1y of history has no outturn yet. Dropping those points is
        right; inventing a denominator for them is not."""
        out = dh.reconstruct_forward([("2026-06-30", 120.0)], self._vintages())
        self.assertEqual(out, {})

    def test_the_forward_series_is_cheaper_than_the_trailing_one(self):
        """The whole reason the bases cannot be mixed: for anything growing, the
        forward multiple is lower, every week."""
        prices = [("2023-06-30", 120.0), ("2024-06-30", 150.0)]
        vintages = self._vintages()
        fwd = dict(dh.reconstruct_forward(prices, vintages))
        ttm = dict(dh.reconstruct(prices, vintages))
        for (stamp, f), (_s, t) in zip(fwd["pe"], ttm["pe"]):
            self.assertLess(f, t, f"forward should be cheaper at {stamp}")

    def test_pfcf_uses_the_share_count_of_the_day(self):
        """A forward multiple has a forward *denominator*, not a forward share
        count — using the future count folds in a buyback nobody had seen."""
        out = dh.reconstruct_forward([("2023-06-30", 120.0)], self._vintages())
        # cap = 120 * 100 shares = 12000; FY2023 FCF = 1200 → 10.0x
        self.assertAlmostEqual(out["pfcf"][0][1], 10.0)

    def test_quarterly_vintages_are_ignored(self):
        """`build()` copies the annual FCF onto every quarterly TTM vintage, so
        admitting them would repeat one year's figures as if they were new."""
        V = dh.Vintage
        noisy = [*self._vintages(),
                 V(period_end="2023-06-30", effective="2023-08-14",
                   kind="quarterly", eps=99.0, fcf=9900.0, shares=100.0)]
        clean = dh.reconstruct_forward([("2023-06-30", 120.0)], self._vintages())
        self.assertEqual(dh.reconstruct_forward([("2023-06-30", 120.0)], noisy),
                         clean)

    def test_forward_basis_prefers_banked_over_reconstructed(self):
        """Banked is what the market actually thought on the day; reconstructed
        divides by an outturn nobody had. When both can rank, banked wins."""
        payload = {
            "forward_context": {"forwardPE": 9.0, "trailingPE": 14.0},
            "vintages": [], "series": {}, "percentiles": {},
            "forward_series": {"pe": [(f"2024-01-{i:02d}", 50.0)
                                      for i in range(1, 29)] * 3},
        }
        banked = {"pe": [float(v) for v in range(5, 65)]}
        out = dh.forward_basis(payload, banked, minimum=60)
        self.assertEqual(out["pe"]["source"], "banked")

    def test_it_falls_back_to_the_reconstruction_when_nothing_is_banked(self):
        payload = {
            "forward_context": {"forwardPE": 9.0},
            "vintages": [], "series": {}, "percentiles": {"pe": 2.1},
            "forward_series": {"pe": [("2024-01-01", float(v))
                                      for v in range(5, 70)]},
        }
        out = dh.forward_basis(payload, {}, minimum=60)
        self.assertEqual(out["pe"]["source"], "reconstructed_forward")
        self.assertEqual(out["pe"]["observations"], 65)
        self.assertEqual(out["pe"]["trailing_percentile"], 2.1)

    def test_the_kill_switch_drops_only_the_look_ahead_tier(self):
        import os

        payload = {
            "forward_context": {"forwardPE": 9.0},
            "vintages": [], "series": {}, "percentiles": {},
            "forward_series": {"pe": [("2024-01-01", float(v))
                                      for v in range(5, 70)]},
        }
        os.environ["DCA_FORWARD_RECONSTRUCTED"] = "0"
        try:
            self.assertEqual(dh.forward_basis(payload, {}, minimum=60), {})
            # Banked still ranks — only the reconstructed tier is off.
            banked = {"pe": [float(v) for v in range(5, 65)]}
            self.assertEqual(
                dh.forward_basis(payload, banked, minimum=60)["pe"]["source"],
                "banked")
        finally:
            os.environ.pop("DCA_FORWARD_RECONSTRUCTED", None)

    # ── deriving it for payloads built before it existed ──────────────────
    #
    # Without this the feature reaches a ticker only when its 24h TTL happens to
    # expire, so for days half the registry scores on one basis and half on the
    # other — and a CACHE_VER bump, the obvious alternative, is ~360 Yahoo reads
    # in one sweep.
    def _stored_payload(self):
        V = dh.Vintage
        vintages = [
            V(period_end="2022-12-31", effective="2023-03-31", kind="annual",
              eps=10.0, fcf=1000.0, shares=100.0),
            V(period_end="2023-12-31", effective="2024-03-31", kind="annual",
              eps=12.0, fcf=1200.0, shares=100.0),
        ]
        return {
            "ticker": "OLDX",
            "prices": [("2023-06-30", 120.0), ("2023-09-30", 132.0)],
            "vintages": [v.as_dict() for v in vintages],
        }

    def test_a_payload_without_the_series_derives_it_from_what_it_has(self):
        payload = self._stored_payload()
        self.assertNotIn("forward_series", payload)
        out = dh.forward_series_for(payload)
        self.assertEqual(len(out["pe"]), 2)
        self.assertAlmostEqual(out["pe"][0][1], 10.0)      # 120 / FY2023 eps 12

    def test_the_derived_series_is_written_back_so_it_is_computed_once(self):
        payload = self._stored_payload()
        dh.forward_series_for(payload)
        self.assertIn("forward_series", payload)
        # Second call must return the stored object, not recompute it.
        payload["forward_series"] = {"pe": [("sentinel", 1.0)]}
        self.assertEqual(dh.forward_series_for(payload),
                         {"pe": [("sentinel", 1.0)]})

    def test_a_stored_empty_series_is_not_recomputed_every_request(self):
        """An ETF has no statements, so the honest answer is {} — and `{}` must
        not read as "not derived yet" or every request pays for the walk."""
        payload = {"ticker": "GDX", "prices": [], "vintages": []}
        self.assertEqual(dh.forward_series_for(payload), {})
        self.assertEqual(payload["forward_series"], {})

    def test_forward_basis_ranks_from_a_derived_series(self):
        """End to end: an old payload with no forward_series still switches."""
        payload = {
            "forward_context": {"forwardPE": 9.0},
            "percentiles": {"pe": 2.1},
            "prices": [(f"2023-{m:02d}-01", 100.0 + m) for m in range(1, 13)] * 6,
            "vintages": [
                dh.Vintage(period_end="2022-12-31", effective="2023-03-31",
                           kind="annual", eps=10.0, shares=100.0).as_dict(),
                dh.Vintage(period_end="2023-12-31", effective="2024-03-31",
                           kind="annual", eps=12.0, shares=100.0).as_dict(),
            ],
        }
        out = dh.forward_basis(payload, {}, minimum=60)
        self.assertEqual(out["pe"]["source"], "reconstructed_forward")
        self.assertEqual(out["pe"]["trailing_percentile"], 2.1)


class DcfInputs(unittest.TestCase):
    """Assembling the absolute branch's inputs from a built payload.

    Pure — it reads only what ``build`` already stored, which is what makes the
    DCF branch cost no extra Yahoo call.
    """

    def _payload(self, **over):
        base = {
            "vintages": [
                {"kind": "annual", "period_end": "2022-12-31", "fcf": 1.0e9,
                 "debt": 5.0e9, "cash": 2.0e9, "shares": 1.0e9},
                {"kind": "annual", "period_end": "2023-12-31", "fcf": 1.2e9,
                 "debt": 5.5e9, "cash": 2.5e9, "shares": 0.98e9},
                {"kind": "annual", "period_end": "2024-12-31", "fcf": 1.5e9,
                 "debt": 6.0e9, "cash": 3.0e9, "shares": 0.96e9},
            ],
            "prices": [["2026-09-04", 98.0], ["2026-09-11", 101.5]],
            "forward_context": {"beta": 1.15},
        }
        base.update(over)
        return base

    def test_only_annual_vintages_contribute_the_cash_flow_series(self):
        """``build`` copies the annual FCF onto every quarterly TTM vintage, so
        including them would repeat the same figure three or four times: the
        observed growth between those points is exactly zero and the dispersion
        collapses. The series would still look the right length."""
        payload = self._payload()
        payload["vintages"] = payload["vintages"] + [
            {"kind": "quarterly", "period_end": "2025-03-31", "fcf": 1.5e9,
             "debt": 6.0e9, "cash": 3.0e9, "shares": 0.96e9},
            {"kind": "quarterly", "period_end": "2025-06-30", "fcf": 1.5e9,
             "debt": 6.0e9, "cash": 3.0e9, "shares": 0.96e9},
        ]
        out = dh.dcf_inputs(payload)
        self.assertEqual(out["fcf_series"], [1.0e9, 1.2e9, 1.5e9])
        self.assertEqual(out["annual_vintages"], 3)

    def test_the_series_is_ordered_oldest_first(self):
        payload = self._payload()
        payload["vintages"] = list(reversed(payload["vintages"]))
        self.assertEqual(dh.dcf_inputs(payload)["fcf_series"],
                         [1.0e9, 1.2e9, 1.5e9])

    def test_balance_sheet_items_take_the_latest_reading(self):
        """Debt, cash and shares are *stocks*. Summing or averaging them would
        report several times the company — the same distinction
        ``build_quarterly_ttm`` turns on."""
        out = dh.dcf_inputs(self._payload())
        self.assertEqual(out["debt"], 6.0e9)
        self.assertEqual(out["cash"], 3.0e9)
        self.assertEqual(out["shares"], 0.96e9)

    def test_the_price_is_the_last_weekly_close_of_the_reconstruction(self):
        """V_DCF and V_REL must describe one price. The relative branch ranks
        multiples computed at this close, so measuring the DCF upside against a
        live quote would make two halves of one score refer to two moments."""
        out = dh.dcf_inputs(self._payload())
        self.assertEqual(out["price"], 101.5)
        self.assertEqual(out["price_date"], "2026-09-11")

    def test_a_payload_with_no_prices_yields_no_price(self):
        out = dh.dcf_inputs(self._payload(prices=[]))
        self.assertIsNone(out["price"])
        self.assertIsNone(out["price_date"])

    def test_beta_comes_from_the_forward_context_and_is_optional(self):
        self.assertEqual(dh.dcf_inputs(self._payload())["beta"], 1.15)
        self.assertIsNone(dh.dcf_inputs(self._payload(forward_context={}))["beta"])

    def test_a_payload_written_before_beta_was_collected_still_works(self):
        """No CACHE_VER bump was taken for the beta addition, so old payloads
        must degrade rather than fail — see the note on _FORWARD_KEYS."""
        payload = self._payload()
        payload.pop("forward_context")
        out = dh.dcf_inputs(payload)
        self.assertIsNone(out["beta"])
        self.assertEqual(out["fcf_series"], [1.0e9, 1.2e9, 1.5e9])

    def test_an_empty_payload_does_not_raise(self):
        out = dh.dcf_inputs({})
        self.assertEqual(out["fcf_series"], [])
        self.assertIsNone(out["shares"])


class DcfFor(unittest.TestCase):
    """Choosing between the derived model and a stored override."""

    def _payload(self):
        vintages = []
        fcf = 4.0e9
        for year in range(2019, 2025):
            vintages.append({"kind": "annual", "period_end": f"{year}-12-31",
                             "fcf": fcf, "debt": 5.0e9, "cash": 8.0e9,
                             "shares": 1.0e9})
            fcf *= 1.09
        return {"vintages": vintages,
                "prices": [["2026-09-11", 60.0]],
                "forward_context": {"beta": 1.0}}

    def test_the_derived_path_scores_a_compounder(self):
        out = dh.dcf_for("MSFT", self._payload(), model="compounder",
                         as_of="2026-09-12")
        self.assertFalse(out["refused"], out.get("reason"))
        self.assertEqual(out["source"], "derived")
        self.assertEqual(out["basis"], "three_scenario")

    def test_a_bank_is_refused_by_form_with_no_override(self):
        out = dh.dcf_for("JPM", self._payload(), model="bank", as_of="2026-09-12")
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "form_not_applicable")
        self.assertIsNone(out["V"])

    def test_an_override_scores_even_a_form_the_model_refuses(self):
        """§10 says *we* cannot value a bank with FCFF, not that a bank cannot
        be valued. Somebody who built an excess-return model by hand is exactly
        who the override exists for."""
        out = dh.dcf_for("JPM", self._payload(), model="bank",
                         override={"base": 75.0, "valuation_date": "2026-09-01"},
                         as_of="2026-09-12")
        self.assertFalse(out["refused"])
        self.assertEqual(out["source"], "override")

    def test_a_mid_cycle_template_starts_from_the_mean(self):
        out = dh.dcf_for("NVDA", self._payload(), model="semiconductor",
                         as_of="2026-09-12")
        if not out["refused"]:
            self.assertTrue(out["model"]["mid_cycle"])
            self.assertIn("mid_cycle_base", out["notes"])

    def test_an_override_wacc_is_used_and_reported(self):
        plain = dh.dcf_for("MSFT", self._payload(), model="compounder",
                           as_of="2026-09-12")
        forced = dh.dcf_for("MSFT", self._payload(), model="compounder",
                            override={"wacc": 0.12}, as_of="2026-09-12")
        self.assertFalse(forced["refused"], forced.get("reason"))
        self.assertAlmostEqual(forced["model"]["capital"]["wacc"], 0.12, places=6)
        # A higher discount rate is a lower fair value, so a lower score.
        self.assertLess(forced["V"], plain["V"])

    def test_the_override_metadata_never_carries_the_authors_address(self):
        """``/dca`` is public. The address is masked the way
        ``share.public_payload`` masks a sharer's."""
        out = dh.dcf_for("MSFT", self._payload(), model="compounder",
                         override={"base": 90.0, "author": "alice@example.com",
                                   "valuation_date": "2026-09-01"},
                         as_of="2026-09-12")
        meta = out["override"]
        self.assertEqual(meta["by"], "alice@…")
        self.assertNotIn("example.com", str(meta))

    def test_a_stale_override_refuses_rather_than_silently_reverting(self):
        out = dh.dcf_for("MSFT", self._payload(), model="compounder",
                         override={"base": 90.0, "valuation_date": "2024-01-01"},
                         as_of="2026-09-12")
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "valuation_stale")

    def test_a_refusal_is_never_a_score_of_fifty(self):
        for model in ("bank", "reit", "utility"):
            out = dh.dcf_for("X", self._payload(), model=model, as_of="2026-09-12")
            self.assertIsNone(out["V"], msg=model)
            self.assertEqual(out["w_dcf_hint"], 0.0, msg=model)


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


class ListingBasisIntegrationTests(unittest.TestCase):
    """``build()`` on an ADR, with the six Yahoo reads stubbed out.

    The unit tests in ``test_listing.py`` prove the arithmetic; this proves it is
    actually *wired in*, which is a different question and the one that has bitten
    this repo before — a module can be perfect and never called. It also covers
    the part no pure test reaches: that the vintages ``dcf_inputs`` reads back are
    the converted ones, so the DCF's per-share fair value is struck in the
    currency the price is quoted in.

    ``build()`` writes nothing (``get()`` owns the disk cache), so stubbing the
    fetch is enough to keep this free of network and free of side effects.
    """

    YEARS = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
    NET_INCOME = [5.97e11, 1.017e12, 8.38e11, 1.17e12, 1.698e12]   # TWD

    def _raw(self, *, adr=True, eps_per_adr=True, fx=True):
        """A TSM-shaped payload. ``adr=False`` models an ordinary US listing,
        which differs on *both* axes at once — a domestic filer has neither a
        foreign reporting currency nor a depositary ratio, and varying only the
        currency would describe a company that does not exist.
        """
        # Yahoo's EPS row is per-ADR for TSM; the fixture can produce either
        # basis so the cross-check has something to distinguish.
        per_ordinary = [ni / 25_932_524_521 for ni in self.NET_INCOME]
        ratio = 5.0 if adr else 1.0
        eps = [e * ratio for e in per_ordinary] if eps_per_adr else per_ordinary
        inc = {y: {"Total Revenue": 1.6e12 + i * 4e11,
                   "Net Income": self.NET_INCOME[i],
                   "Diluted EPS": eps[i],
                   "EBITDA": self.NET_INCOME[i] * 1.6}
               for i, y in enumerate(self.YEARS)}
        bal = {y: {"Ordinary Shares Number": 25_932_524_521,
                   "Total Debt": 9.0e11,
                   "Cash And Cash Equivalents": 1.3e12,
                   "Tangible Book Value": 2.6e12 + i * 3e11}
               for i, y in enumerate(self.YEARS)}
        cfs = {y: {"Free Cash Flow": self.NET_INCOME[i] * 0.45,
                   "Depreciation And Amortization": 4.0e11}
               for i, y in enumerate(self.YEARS)}

        prices, rates = [], []
        day = date(2021, 1, 3)
        while day < date(2026, 9, 14):
            age = (day - date(2021, 1, 3)).days
            prices.append((day.isoformat(), 120.0 + age * 0.15))
            rates.append((day.isoformat(), 1 / 28.0 + (age / 2080) * (1 / 32.21 - 1 / 28.0)))
            day += timedelta(days=7)

        # trailingEps is what the cross-check measures against: the last annual
        # EPS on the quoted basis, converted at the final rate.
        rate = rates[-1][1] if adr else 1.0
        trailing = per_ordinary[-1] * ratio * rate
        return {
            "info": {"currency": "USD",
                     "financialCurrency": "TWD" if adr else "USD",
                     "sharesOutstanding": 25_932_524_521 / ratio,
                     "trailingEps": trailing,
                     "shortName": "TSMC", "sector": "Technology",
                     "industry": "Semiconductors", "quoteType": "EQUITY"},
            "annual_income": inc, "annual_balance": bal, "annual_cashflow": cfs,
            "quarterly_income": {}, "prices": prices,
            "fx": rates if (fx and adr) else [],
        }

    def _build(self, **kwargs):
        raw = self._raw(**kwargs)
        original = dh._fetch_raw
        dh._fetch_raw = lambda _t: raw
        try:
            return dh.build("TSM")
        finally:
            dh._fetch_raw = original

    def test_the_basis_is_detected_and_recorded(self):
        basis = self._build().get("listing_basis")
        self.assertIsNotNone(basis)
        self.assertEqual(basis["statement_currency"], "TWD")
        self.assertEqual(basis["price_currency"], "USD")
        self.assertAlmostEqual(basis["share_ratio"], 5.0, places=3)
        self.assertEqual(basis["fx_source"], "series")

    def test_the_pe_is_no_longer_a_currency_ratio(self):
        """The reported symptom. Unconverted, ``close / eps`` on an ADR lands
        near 1 because a TWD earnings figure and a USD price are the same order
        of magnitude — which is the whole reason it looked like a data glitch
        rather than a unit error."""
        payload = self._build()
        pe = payload["series"]["pe"][-1][1]
        self.assertGreater(pe, 10.0, "P/E still looks like an FX ratio")

    def test_stored_vintages_are_converted_for_the_dcf(self):
        """``dcf_inputs`` reads the stored vintages and compares its per-share
        fair value against the stored price. Leaving the vintages in the filer's
        currency there would value a USD quote against TWD cash flows."""
        payload = self._build()
        inputs = dh.dcf_inputs(payload)
        self.assertAlmostEqual(inputs["shares"] / 5_186_474_013, 1.0, places=4)
        # FCF is 45% of net income; the last year's is ~1.7e12 TWD, which is
        # tens of billions of dollars, not trillions of anything.
        self.assertLess(max(inputs["fcf_series"]), 1e11)
        self.assertGreater(max(inputs["fcf_series"]), 1e10)

    def test_an_ordinary_basis_eps_row_is_detected_and_rebased(self):
        """The reason the EPS basis is measured rather than assumed from TSM."""
        basis = self._build(eps_per_adr=False)["listing_basis"]
        self.assertAlmostEqual(basis["eps_scale"], 5.0, places=3)
        self.assertIn("eps_rebased_to_quoted_shares", basis["notes"])

    def test_either_eps_basis_reaches_the_same_pe(self):
        """The two fixtures describe one company whose filer happens to quote
        EPS differently. If the correction works, the multiple is the same — and
        if it silently did nothing, the two would differ by five."""
        with_adr = self._build()["series"]["pe"][-1][1]
        with_ordinary = self._build(eps_per_adr=False)["series"]["pe"][-1][1]
        self.assertAlmostEqual(with_adr, with_ordinary, places=3)

    def test_the_fx_read_short_circuits_before_touching_yfinance(self):
        """The seventh Yahoo read must fire only for a real currency mismatch —
        that is the whole cost claim, and the ~59 of 60 tracked tickers that file
        in the currency they trade in have to keep paying nothing. The guard sits
        above the ``import yfinance`` for that reason, so this runs with the
        module absent as well as present.

        It does **not** prove ``_fetch_raw`` calls this; the integration tests
        above stub that out wholesale, and mutating the call away leaves them
        green. That path is covered by checking a live ADR payload reports
        ``fx_source: series``.
        """
        self.assertEqual(dh._fetch_fx_series("USD", "USD"), [])
        self.assertEqual(dh._fetch_fx_series(None, None), [])
        self.assertEqual(dh._fetch_fx_series("TWD", None), [])

    def test_a_us_filer_is_left_completely_alone(self):
        """No basis block at all, so an ordinary listing's payload is what it
        was before this existed. Asserted on the key set rather than the payload
        so a failure prints a diff instead of five years of weekly series."""
        self.assertNotIn("listing_basis", sorted(self._build(adr=False)))

    def test_no_rate_at_all_refuses_rather_than_publishes(self):
        """With the currencies differing and neither a series nor a spot rate,
        the only honest output is none. ``usd_rate`` is stubbed to fail the way
        a blocked or delisted pair does."""
        from ystocker import data as ydata
        original = ydata.usd_rate
        ydata.usd_rate = lambda _c: None
        try:
            payload = self._build(fx=False)
        finally:
            ydata.usd_rate = original
        self.assertEqual(payload.get("unavailable"), "currency_unreconciled")
        self.assertEqual(payload["series"], {})

    def test_spot_is_used_and_labelled_when_the_series_is_missing(self):
        """Degrading visibly beats refusing outright — but the payload has to
        say which rate it used, because the historical points carry the drift."""
        from ystocker import data as ydata
        original = ydata.usd_rate
        ydata.usd_rate = lambda _c: 1 / 32.21
        try:
            payload = self._build(fx=False)
        finally:
            ydata.usd_rate = original
        self.assertEqual(payload["listing_basis"]["fx_source"], "spot")
        self.assertGreater(payload["series"]["pe"][-1][1], 10.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
