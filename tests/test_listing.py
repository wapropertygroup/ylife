"""Tests for :mod:`ystocker.listing`.

The module reconciles two things Yahoo hands back from one ``Ticker`` and never
reconciles itself: the currency and share basis a stock is *quoted* in, and the
currency and share basis its filer *reports* in. Everything here is a case where
getting it wrong produces a multiple that renders exactly like a real one.

The anchor is TSM, which is what sent me here: ``/dca/TSM`` reported a P/E of
1.01 against a true 32.46. Those figures are measured, not invented — see the
docstrings below for what each one came from — and the conversion is checked
against ``trailingEps``, ``sharesOutstanding`` and ``marketCap``, which Yahoo
publishes independently on the quoted basis.

Pure: no app, no network, no clock.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import listing  # noqa: E402

# --- TSM, measured on the box 2026-09-19 -----------------------------------
# yf.Ticker("TSM"): info, income_stmt, balance_sheet.
TSM_INFO = {
    "currency": "USD",              # the ADR trades in dollars
    "financialCurrency": "TWD",     # the company files in New Taiwan dollars
    "sharesOutstanding": 5_186_474_013,
    "trailingEps": 13.39,           # USD per ADR, Yahoo's own
    "marketCap": 2_254_404_845_568,
}
TSM_ORDINARY_SHARES = 25_932_524_521      # "Ordinary Shares Number"
TSM_TTM_EPS_TWD = 431.35                  # "Diluted EPS", summed to TTM
TSM_TTM_FCF_TWD = 992_378_400_000
TSM_PRICE_USD = 434.67
TSM_RATE = 1 / 32.21                      # USD per TWD


class TheRegressionItself(unittest.TestCase):
    """The bug as reported: TSM's P/E showing 1.01."""

    def _basis(self):
        return listing.detect(TSM_INFO,
                              statement_shares=TSM_ORDINARY_SHARES,
                              statement_eps=TSM_TTM_EPS_TWD,
                              rate=TSM_RATE, fx_source="series")

    def test_the_broken_arithmetic_is_what_it_was(self):
        """Guards the fixture. If the raw numbers stopped producing 1.01 the
        tests below would be proving a fix for a bug they no longer describe."""
        self.assertAlmostEqual(TSM_PRICE_USD / TSM_TTM_EPS_TWD, 1.008, places=2)

    def test_the_adr_ratio_falls_out_of_the_two_share_counts(self):
        """Five ordinary shares per ADR, derived rather than looked up in a
        table of known ratios — which is what makes it need no maintenance."""
        self.assertAlmostEqual(self._basis().share_ratio, 5.0, places=3)

    def test_eps_is_already_per_adr_so_it_is_not_rebased(self):
        """Yahoo's ``Diluted EPS`` row for TSM is 5.06x the figure implied by
        ``Net Income / Ordinary Shares Number`` — it is already per receipt.
        Applying the share ratio here as well would divide the P/E by five."""
        self.assertEqual(self._basis().eps_scale, 1.0)

    def test_converted_eps_equals_yahoos_own_trailing_eps(self):
        """The cross-check the whole module rests on: the statement EPS
        converted at the FX rate must land on ``info.trailingEps``, which Yahoo
        publishes independently on the quoted basis in the quoted currency."""
        basis = self._basis()
        out = listing.convert({"eps": TSM_TTM_EPS_TWD}, rate=TSM_RATE,
                              ratio=basis.share_ratio, scale=basis.eps_scale)
        self.assertAlmostEqual(out["eps"], TSM_INFO["trailingEps"], places=2)

    def test_the_pe_comes_out_at_the_published_figure(self):
        basis = self._basis()
        out = listing.convert({"eps": TSM_TTM_EPS_TWD}, rate=TSM_RATE,
                              ratio=basis.share_ratio, scale=basis.eps_scale)
        self.assertAlmostEqual(TSM_PRICE_USD / out["eps"], 32.46, places=1)

    def test_shares_come_out_at_the_published_count(self):
        """`close * shares` is the market cap behind P/FCF, EV/EBITDA and P/TBV.
        Converting the currency and leaving the count on the ordinary basis
        would fix P/E and leave those four wrong by a factor of five — which is
        worse than fixing nothing, because a reader cannot tell which columns
        were repaired."""
        basis = self._basis()
        out = listing.convert({"shares": TSM_ORDINARY_SHARES}, rate=TSM_RATE,
                              ratio=basis.share_ratio, scale=basis.eps_scale)
        self.assertAlmostEqual(out["shares"] / TSM_INFO["sharesOutstanding"],
                               1.0, places=4)

    def test_market_cap_reconciles_with_yahoos(self):
        basis = self._basis()
        out = listing.convert({"shares": TSM_ORDINARY_SHARES}, rate=TSM_RATE,
                              ratio=basis.share_ratio, scale=basis.eps_scale)
        cap = TSM_PRICE_USD * out["shares"]
        self.assertAlmostEqual(cap / TSM_INFO["marketCap"], 1.0, places=3)

    def test_price_to_fcf_moves_from_absurd_to_plausible(self):
        """11.4 was the page's answer and it reads as a cash machine going
        cheap. TSM's free cash flow is about half its net income because of
        capex, so the truth is in the seventies."""
        basis = self._basis()
        out = listing.convert({"shares": TSM_ORDINARY_SHARES, "fcf": TSM_TTM_FCF_TWD},
                              rate=TSM_RATE, ratio=basis.share_ratio,
                              scale=basis.eps_scale)
        pfcf = (TSM_PRICE_USD * out["shares"]) / out["fcf"]
        self.assertGreater(pfcf, 60.0)
        self.assertLess(pfcf, 90.0)


class AlignmentTests(unittest.TestCase):
    """The common cases must cost nothing and change nothing."""

    def test_a_us_listing_is_aligned(self):
        basis = listing.detect(
            {"currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 14_594_180_000, "trailingEps": 7.5},
            statement_shares=14_594_180_000, statement_eps=7.5, rate=1.0)
        self.assertTrue(basis.aligned)
        self.assertTrue(basis.usable)
        self.assertIsNone(basis.fx_source)

    def test_a_local_foreign_listing_is_aligned(self):
        """Itochu files in yen and trades in yen, so price and statements
        already share a unit. Measured: 8001.T reconstructs to a P/E of 18.0 and
        was never part of this bug — only ADRs were."""
        basis = listing.detect(
            {"currency": "JPY", "financialCurrency": "JPY",
             "sharesOutstanding": 6_989_912_686, "trailingEps": 128.0},
            statement_shares=6_989_912_686, statement_eps=128.0, rate=1.0)
        self.assertTrue(basis.aligned)

    def test_an_aligned_basis_skips_the_eps_check_entirely(self):
        """The cross-check has nothing to choose between when there is no
        correction on offer, so running it would only let ordinary TTM-vintage
        drift raise a basis note on healthy US tickers."""
        basis = listing.detect(
            {"currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 1_000, "trailingEps": 2.0},
            statement_shares=1_000, statement_eps=99.0, rate=1.0)
        self.assertTrue(basis.aligned)
        self.assertNotIn("eps_basis_unexplained", basis.notes)

    def test_missing_currency_metadata_is_treated_as_aligned(self):
        """Yahoo publishes no sector, industry or usable name for the Korean
        listings, so assuming the currency fields are always present is exactly
        the assumption this repo has already been bitten by."""
        basis = listing.detect({"sharesOutstanding": 100},
                               statement_shares=100, statement_eps=1.0, rate=None)
        self.assertTrue(basis.aligned)
        self.assertTrue(basis.usable)


class UsabilityTests(unittest.TestCase):
    def test_a_mismatch_with_no_rate_is_refused(self):
        """The one state where publishing would mean publishing the 32x error."""
        basis = listing.detect(TSM_INFO, statement_shares=TSM_ORDINARY_SHARES,
                               statement_eps=TSM_TTM_EPS_TWD,
                               rate=None, fx_source=None)
        self.assertTrue(basis.needs_fx)
        self.assertFalse(basis.usable)

    def test_a_mismatch_with_a_rate_is_usable(self):
        basis = listing.detect(TSM_INFO, statement_shares=TSM_ORDINARY_SHARES,
                               statement_eps=TSM_TTM_EPS_TWD,
                               rate=TSM_RATE, fx_source="spot")
        self.assertTrue(basis.usable)
        self.assertEqual(basis.fx_source, "spot")

    def test_the_source_is_not_recorded_when_no_conversion_is_needed(self):
        """A caller passing a stale `fx_source` for a USD filer must not make
        the payload claim a conversion happened."""
        basis = listing.detect(
            {"currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 10, "trailingEps": 1.0},
            statement_shares=10, statement_eps=1.0, rate=1.0, fx_source="series")
        self.assertIsNone(basis.fx_source)

    def test_a_share_mismatch_alone_does_not_record_an_fx_source(self):
        """Reaches the same guard by the other route. A dual-class US listing
        can need the share correction with the currencies already matching, and
        that path skips the early return — so without this the gate is only
        proven on a case that never evaluates it, which mutating the line out
        showed: the suite stayed green."""
        basis = listing.detect(
            {"currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 1_000, "trailingEps": 2.0},
            statement_shares=5_000, statement_eps=2.0, rate=1.0, fx_source="series")
        self.assertFalse(basis.needs_fx)
        self.assertAlmostEqual(basis.share_ratio, 5.0)
        self.assertIsNone(basis.fx_source)
        self.assertTrue(basis.usable)


class ShareRatioTests(unittest.TestCase):
    def test_a_small_difference_is_not_a_depositary_ratio(self):
        """Treasury stock and basic-versus-diluted move the two counts a per
        cent or two apart. Scaling by that adds noise to every cap in the
        history for no gain."""
        ratio, note = listing.share_ratio(1_020, 1_000)
        self.assertEqual(ratio, 1.0)
        self.assertIsNone(note)

    def test_the_band_separates_noise_from_a_real_ratio(self):
        """Just inside is treasury/diluted drift and must be ignored; well
        outside is a depositary ratio and must be applied. The exact edge is
        deliberately not asserted — it is a float comparison, not a property
        anybody depends on."""
        self.assertEqual(listing.share_ratio(1_040, 1_000)[0], 1.0)
        self.assertAlmostEqual(listing.share_ratio(1_060, 1_000)[0], 1.06)

    def test_an_implausible_ratio_is_refused_not_applied(self):
        """Past this, the two numbers are not a ratio — one of them is in the
        wrong unit, and scaling by it would be a new error rather than a fix."""
        ratio, note = listing.share_ratio(1e9, 1.0)
        self.assertEqual(ratio, 1.0)
        self.assertEqual(note, "share_ratio_implausible")

    def test_missing_counts_report_why(self):
        self.assertEqual(listing.share_ratio(None, 1_000), (1.0, "share_counts_unavailable"))
        self.assertEqual(listing.share_ratio(1_000, None), (1.0, "share_counts_unavailable"))
        self.assertEqual(listing.share_ratio(0, 1_000), (1.0, "share_counts_unavailable"))

    def test_booleans_are_not_share_counts(self):
        """`True` is an int in Python and would pass as a count of one, making
        every ratio astronomically large."""
        self.assertEqual(listing.share_ratio(True, 1_000), (1.0, "share_counts_unavailable"))

    def test_a_ratio_below_one_is_carried_not_clamped(self):
        """Some receipts bundle the other way — one ADR to several ordinary
        shares is common, but the inverse exists and is equally real."""
        ratio, note = listing.share_ratio(1_000, 4_000)
        self.assertAlmostEqual(ratio, 0.25)
        self.assertIsNone(note)


class EpsScaleTests(unittest.TestCase):
    def test_an_already_quoted_basis_needs_no_scaling(self):
        scale, note = listing.eps_scale(431.35, 13.39, rate=TSM_RATE, ratio=5.0)
        self.assertEqual(scale, 1.0)
        self.assertIsNone(note)

    def test_an_ordinary_basis_row_is_rebased(self):
        """The case TSM is *not*, and the reason the basis is measured rather
        than assumed. Here the filer quotes EPS per ordinary share, so the
        converted figure comes out five times too small and the share ratio is
        what reconciles it."""
        scale, note = listing.eps_scale(431.35 / 5.0, 13.39, rate=TSM_RATE, ratio=5.0)
        self.assertAlmostEqual(scale, 5.0)
        self.assertEqual(note, "eps_rebased_to_quoted_shares")

    def test_a_mismatch_neither_basis_explains_is_named_not_split(self):
        scale, note = listing.eps_scale(431.35 * 3, 13.39, rate=TSM_RATE, ratio=5.0)
        self.assertEqual(scale, 1.0)
        self.assertEqual(note, "eps_basis_unexplained")

    def test_no_reference_means_unverified_rather_than_corrected(self):
        scale, note = listing.eps_scale(431.35, None, rate=TSM_RATE, ratio=5.0)
        self.assertEqual(scale, 1.0)
        self.assertEqual(note, "eps_basis_unverified")

    def test_a_missing_rate_cannot_verify_anything(self):
        self.assertEqual(listing.eps_scale(431.35, 13.39, rate=None, ratio=5.0),
                         (1.0, "eps_basis_unverified"))

    def test_the_ratio_is_only_tried_when_it_would_help(self):
        """With no depositary ratio on the table there is no second candidate,
        so a mismatch can only be reported."""
        scale, note = listing.eps_scale(100.0, 1.0, rate=1.0, ratio=1.0)
        self.assertEqual(scale, 1.0)
        self.assertEqual(note, "eps_basis_unexplained")

    def test_timing_drift_inside_the_tolerance_is_accepted(self):
        """The reconstruction's TTM can end a quarter before Yahoo's trailing
        figure, so the two legitimately differ. The band is wide because the
        alternative basis differs by the ADR ratio — a factor of five, not a
        few per cent — so there is no risk of the two candidates overlapping."""
        scale, note = listing.eps_scale(431.35 * 1.2, 13.39, rate=TSM_RATE, ratio=5.0)
        self.assertEqual(scale, 1.0)
        self.assertIsNone(note)


class RateAtTests(unittest.TestCase):
    SERIES = [("2021-01-04", 0.0357), ("2023-06-05", 0.0325), ("2026-09-14", 0.0310)]

    def test_the_rate_in_force_is_the_last_one_on_or_before(self):
        self.assertAlmostEqual(listing.rate_at(self.SERIES, "2023-08-01"), 0.0325)

    def test_an_exact_match_uses_that_week(self):
        self.assertAlmostEqual(listing.rate_at(self.SERIES, "2023-06-05"), 0.0325)

    def test_after_the_last_observation_the_last_rate_holds(self):
        self.assertAlmostEqual(listing.rate_at(self.SERIES, "2026-09-19"), 0.0310)

    def test_before_the_series_starts_the_earliest_rate_is_used(self):
        """Not the fallback. A 2020 statement converted at today's rate carries
        the whole five-year drift; converted at the first rate on file it
        carries only the gap to that week, which is smaller by construction."""
        self.assertAlmostEqual(
            listing.rate_at(self.SERIES, "2019-01-01", fallback=0.99), 0.0357)

    def test_an_empty_series_falls_back(self):
        self.assertAlmostEqual(listing.rate_at([], "2026-09-19", fallback=0.031), 0.031)

    def test_an_empty_series_with_no_fallback_is_none(self):
        self.assertIsNone(listing.rate_at([], "2026-09-19"))


class ConvertTests(unittest.TestCase):
    def test_money_is_scaled_and_share_counts_are_not(self):
        out = listing.convert({"revenue": 100.0, "shares": 50.0},
                              rate=2.0, ratio=5.0)
        self.assertEqual(out["revenue"], 200.0)
        self.assertEqual(out["shares"], 10.0)

    def test_per_share_money_takes_both_corrections(self):
        out = listing.convert({"eps": 10.0}, rate=2.0, ratio=5.0, scale=5.0)
        self.assertEqual(out["eps"], 100.0)

    def test_per_share_money_takes_only_the_rate_when_scale_is_neutral(self):
        self.assertEqual(listing.convert({"eps": 10.0}, rate=2.0, ratio=5.0)["eps"], 20.0)

    def test_unclassified_keys_pass_through_untouched(self):
        """A field added to the Vintage slots and not classified here goes
        unconverted rather than missing — which a summation check catches and a
        silent drop does not."""
        out = listing.convert({"period_end": "2026-06-30", "kind": "annual",
                               "effective": "2026-08-14", "widget": 3.0},
                              rate=2.0, ratio=5.0)
        self.assertEqual(out["period_end"], "2026-06-30")
        self.assertEqual(out["kind"], "annual")
        self.assertEqual(out["widget"], 3.0)

    def test_none_survives_conversion(self):
        """An absent statement row must stay absent. Coercing it to zero would
        turn 'we do not know this company's debt' into 'this company has none',
        which flatters enterprise value."""
        self.assertIsNone(listing.convert({"debt": None}, rate=2.0)["debt"])

    def test_a_null_rate_leaves_money_alone(self):
        self.assertEqual(listing.convert({"revenue": 100.0}, rate=None)["revenue"], 100.0)

    def test_a_neutral_ratio_leaves_shares_alone(self):
        self.assertEqual(listing.convert({"shares": 50.0}, rate=2.0, ratio=1.0)["shares"], 50.0)

    def test_every_money_field_is_actually_converted(self):
        """Guards the classification. A field missing from MONEY_FIELDS would
        stay in the filer's currency while its neighbours moved, which is the
        half-converted state that makes one column right and the next wrong."""
        fields = {name: 10.0 for name in listing.MONEY_FIELDS}
        out = listing.convert(fields, rate=3.0)
        self.assertTrue(all(value == 30.0 for value in out.values()), out)

    def test_the_money_and_share_sets_do_not_overlap(self):
        self.assertEqual(listing.MONEY_FIELDS & listing.SHARE_FIELDS, frozenset())

    def test_per_share_fields_are_a_subset_of_money_fields(self):
        self.assertTrue(listing.PER_SHARE_FIELDS <= listing.MONEY_FIELDS)


class InvarianceTests(unittest.TestCase):
    """Every factor in the reconstruction is a ratio of a price to a statement
    figure, which is what makes converting one side equivalent to converting the
    other. The module converts the statements; this pins that the choice is free
    of consequence for the multiples, so the reasoning in its docstring holds."""

    def test_converting_the_statements_equals_converting_the_price(self):
        rate, ratio, price = TSM_RATE, 5.0, TSM_PRICE_USD
        converted = listing.convert(
            {"eps": TSM_TTM_EPS_TWD, "fcf": TSM_TTM_FCF_TWD,
             "shares": TSM_ORDINARY_SHARES}, rate=rate, ratio=ratio)

        statements_side_pe = price / converted["eps"]
        price_side_pe = (price / rate) / TSM_TTM_EPS_TWD
        self.assertAlmostEqual(statements_side_pe, price_side_pe, places=6)

        statements_side_pfcf = (price * converted["shares"]) / converted["fcf"]
        price_side_pfcf = ((price / rate) * (TSM_ORDINARY_SHARES / ratio)) / TSM_TTM_FCF_TWD
        self.assertAlmostEqual(statements_side_pfcf, price_side_pfcf, places=6)

    def test_enterprise_value_survives_the_same_swap(self):
        """EV adds a price-derived term to two statement-derived ones, so it is
        the factor where a half-applied conversion would not cancel."""
        rate, ratio, price = TSM_RATE, 5.0, TSM_PRICE_USD
        raw = {"shares": TSM_ORDINARY_SHARES, "debt": 9.0e11,
               "cash": 2.4e12, "ebitda": 3.1e12}
        c = listing.convert(raw, rate=rate, ratio=ratio)
        statements_side = (price * c["shares"] + c["debt"] - c["cash"]) / c["ebitda"]
        price_side = ((price / rate) * (raw["shares"] / ratio)
                      + raw["debt"] - raw["cash"]) / raw["ebitda"]
        self.assertAlmostEqual(statements_side, price_side, places=6)


class FxPairTests(unittest.TestCase):
    def test_the_pair_is_the_direct_multiplier(self):
        """``TWDUSD=X`` quotes TWD->USD, which is the number wanted. The inverse
        (``TWD=X`` is USD/TWD) reads backwards at the call site and is the same
        direction trap ``data.usd_rate`` documents."""
        self.assertEqual(listing.fx_pair("TWD", "USD"), "TWDUSD=X")

    def test_matching_currencies_need_no_pair(self):
        self.assertIsNone(listing.fx_pair("USD", "USD"))

    def test_case_and_whitespace_are_normalised(self):
        self.assertEqual(listing.fx_pair(" twd ", "usd"), "TWDUSD=X")
        self.assertIsNone(listing.fx_pair("usd", " USD "))

    def test_missing_metadata_yields_no_pair(self):
        self.assertIsNone(listing.fx_pair(None, "USD"))
        self.assertIsNone(listing.fx_pair("TWD", None))
        self.assertIsNone(listing.fx_pair("", ""))


class SerialisationTests(unittest.TestCase):
    def test_the_payload_shape_is_json_safe_and_complete(self):
        basis = listing.detect(TSM_INFO, statement_shares=TSM_ORDINARY_SHARES,
                               statement_eps=TSM_TTM_EPS_TWD,
                               rate=TSM_RATE, fx_source="series")
        out = basis.as_dict()
        self.assertEqual(
            set(out), {"statement_currency", "price_currency", "share_ratio",
                       "eps_scale", "fx_source", "notes"})
        self.assertEqual(out["statement_currency"], "TWD")
        self.assertEqual(out["price_currency"], "USD")
        self.assertIsInstance(out["notes"], list)


if __name__ == "__main__":
    unittest.main()
