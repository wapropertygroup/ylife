"""
Tests for the DCA Valuation Engine's pure core (``ystocker.dca``).

No app, no network, no disk, no clock.

What is worth testing here is not that ``0.35 * 80`` is 28 — it is the handful of
ways this module can return a *plausible* number that is wrong, because every one
of them ends up scaling somebody's contribution and none of them looks broken on
the page:

* A template whose weights do not sum to 1 still produces a 0-100 score. If they
  sum to 0.95 every company it covers reads 5 points cheaper, forever.
* A yield inverted twice is still in 0-100 and still renders as a percentile —
  it just says the opposite of what it means. This is why ``DIRECTION`` owns the
  flip and ``TEMPLATES`` contains no ``100 -``.
* Renormalising after dropping factors is correct, and without a floor it turns
  one surviving factor into a five-factor-looking score.
* The 1.5x ceiling is on the *product*. ``M_valuation`` alone cannot exceed it,
  so a cap applied to that term only is invisible until a cheap stock also has
  estimates being raised — the exact case the ceiling was written for.
* A percentile over a handful of observations, or over a flat series, returns a
  number indistinguishable from one computed over a decade.
* An absent overlay must be neutral. Docking a contribution because a vendor
  feed was down makes the answer depend on Yahoo's uptime.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from ystocker import dca

ROOT = Path(__file__).resolve().parent.parent


class Templates(unittest.TestCase):
    """The weight maps themselves."""

    def test_every_template_sums_to_one(self):
        for name, weights in dca.TEMPLATES.items():
            self.assertAlmostEqual(
                sum(weights.values()), 1.0, places=9,
                msg=f"{name} sums to {sum(weights.values())}; a template that does "
                    f"not sum to 1 biases every company it covers in one direction")

    def test_every_weighted_factor_has_a_direction(self):
        for name, weights in dca.TEMPLATES.items():
            for factor in weights:
                self.assertIn(factor, dca.DIRECTION,
                              f"{name} weights {factor!r}, which has no DIRECTION "
                              f"entry — the sign of its contribution would be a guess")

    def test_no_template_encodes_an_inversion(self):
        """Direction belongs to the factor, never to a formula.

        A template naming both ``fcf_yield`` and something like ``fcf_yield_inv``
        would be the source framework's inline ``100 - P`` creeping back in, and
        a double flip is silent.
        """
        for name, weights in dca.TEMPLATES.items():
            for factor in weights:
                self.assertNotIn("inv", factor,
                                 f"{name}: {factor!r} looks like a pre-inverted "
                                 f"factor; DIRECTION is the only place that flips")

    def test_every_template_is_reachable(self):
        """A template nothing routes to is dead weight that reads as coverage."""
        routed = set(dca.TICKER_MODELS.values()) | set(dca.SECTOR_MODELS.values())
        routed |= {model for _needle, model in dca.INDUSTRY_MODELS}
        routed.add("compounder")  # the documented default
        self.assertEqual(set(dca.TEMPLATES) - routed, set())

    def test_every_routed_template_exists(self):
        for source in (dca.TICKER_MODELS, dca.SECTOR_MODELS):
            for key, model in source.items():
                self.assertIn(model, dca.TEMPLATES, f"{key} routes to unknown {model!r}")
        for needle, model in dca.INDUSTRY_MODELS:
            self.assertIn(model, dca.TEMPLATES, f"{needle} routes to unknown {model!r}")


class Direction(unittest.TestCase):
    """The single inversion, and that it happens exactly once."""

    def test_a_high_yield_reads_cheap(self):
        weights = {"fcf_yield": 1.0}
        dear = dca.expensiveness({"fcf_yield": 0.0}, weights)
        cheap = dca.expensiveness({"fcf_yield": 100.0}, weights)
        self.assertEqual(dear["E"], 100.0, "lowest FCF yield must be the dearest")
        self.assertEqual(cheap["E"], 0.0, "highest FCF yield must be the cheapest")

    def test_a_high_multiple_reads_dear(self):
        weights = {"pe": 1.0}
        self.assertEqual(dca.expensiveness({"pe": 100.0}, weights)["E"], 100.0)
        self.assertEqual(dca.expensiveness({"pe": 0.0}, weights)["E"], 0.0)

    def test_yield_and_multiple_point_opposite_ways(self):
        """The regression a double inversion would produce."""
        same = 90.0
        pe = dca.expensiveness({"pe": same}, {"pe": 1.0})["E"]
        fcf = dca.expensiveness({"fcf_yield": same}, {"fcf_yield": 1.0})["E"]
        self.assertAlmostEqual(pe + fcf, 100.0, places=6)


class AdaptiveRenormalisation(unittest.TestCase):
    """Dropping a distorted factor, and the floor under how far that can go."""

    def test_a_missing_factor_is_dropped_and_the_rest_rescaled(self):
        weights = {"pe": 0.5, "pfcf": 0.5}
        out = dca.expensiveness({"pe": 80.0, "pfcf": None}, weights)
        self.assertEqual(out["E"], 80.0, "the survivor should carry the whole score")
        self.assertEqual(out["dropped"], ["pfcf"])
        self.assertTrue(out["renormalised"])
        self.assertAlmostEqual(out["factors"][0]["weight"], 1.0)

    def test_weights_still_sum_to_one_after_a_drop(self):
        weights = dca.TEMPLATES["compounder"]
        out = dca.expensiveness({"pe": 50.0, "pfcf": 50.0, "ev_ebitda": 50.0,
                                 "peg": None, "peer": None}, weights)
        self.assertAlmostEqual(sum(r["weight"] for r in out["factors"]), 1.0, places=6)

    def test_too_little_surviving_weight_refuses_rather_than_guesses(self):
        """One factor rescaled to 100% must not render as a full score."""
        weights = dca.TEMPLATES["compounder"]          # pe .35 pfcf .20 ... peer .15
        out = dca.expensiveness({"peer": 90.0}, weights)   # 0.15 of 1.0 survives
        self.assertIsNone(out["E"])
        self.assertLess(out["surviving_weight"], dca.MIN_SURVIVING_WEIGHT)

    def test_exactly_at_the_floor_is_accepted(self):
        """The 0.5 boundary is inclusive, so half a template still scores."""
        weights = {"pe": 0.5, "pfcf": 0.5}
        out = dca.expensiveness({"pe": 60.0, "pfcf": None}, weights)
        self.assertEqual(out["surviving_weight"], 0.5)
        self.assertIsNotNone(out["E"])

    def test_nothing_measurable_yields_no_score(self):
        out = dca.expensiveness({}, dca.TEMPLATES["bank"])
        self.assertIsNone(out["E"])
        self.assertIsNone(dca.v_score(out["E"]))

    def test_an_unknown_factor_raises_rather_than_being_ignored(self):
        with self.assertRaises(KeyError):
            dca.expensiveness({"nonsense": 50.0}, {"nonsense": 1.0})

    def test_a_percentile_outside_the_range_is_clamped_not_trusted(self):
        out = dca.expensiveness({"pe": 140.0}, {"pe": 1.0})
        self.assertEqual(out["E"], 100.0)


class Percentiles(unittest.TestCase):
    """Where a rank is honest and where it must refuse."""

    def test_a_short_distribution_returns_none(self):
        self.assertIsNone(dca.percentile_rank(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]))

    def test_a_long_enough_distribution_ranks(self):
        dist = [float(i) for i in range(100)]
        self.assertEqual(dca.percentile_rank(99.0, dist), 100.0)
        self.assertEqual(dca.percentile_rank(0.0, dist), 1.0)
        self.assertEqual(dca.percentile_rank(49.0, dist), 50.0)

    def test_a_flat_distribution_returns_none(self):
        """Constant series: every rank is arithmetically 100 and means nothing."""
        self.assertIsNone(dca.percentile_rank(7.0, [7.0] * 200))

    def test_a_missing_value_returns_none(self):
        self.assertIsNone(dca.percentile_rank(None, [float(i) for i in range(100)]))

    def test_nan_and_inf_are_refused(self):
        dist = [float(i) for i in range(100)]
        self.assertIsNone(dca.percentile_rank(float("nan"), dist))
        self.assertIsNone(dca.percentile_rank(float("inf"), dist))

    def test_booleans_are_not_numbers(self):
        """``True`` is an int in Python and would rank as 1.0 without the guard."""
        self.assertIsNone(dca.percentile_rank(True, [float(i) for i in range(100)]))

    def test_cross_section_needs_no_long_history_but_needs_peers(self):
        """A peer group is a dozen names and must not be held to MIN_OBSERVATIONS."""
        self.assertIsNotNone(dca.cross_sectional_rank(20.0, [10, 15, 25, 30, 35]))
        self.assertIsNone(dca.cross_sectional_rank(20.0, [10, 15, 25]))

    def test_cross_section_ignores_non_positive_peers(self):
        self.assertIsNone(dca.cross_sectional_rank(20.0, [10, 15, 25, None, -4]))


class ModelSelection(unittest.TestCase):

    def test_named_tickers_beat_their_sector(self):
        """AMZN and TSLA are both 'Consumer Cyclical' on Yahoo.

        Letting the sector decide is the single most consequential misroute in the
        table: it would score AMZN on a P/E its business model makes meaningless.
        """
        for ticker, expected in (("AMZN", "amzn"), ("TSLA", "tsla")):
            model, why = dca.pick_model(ticker, sector="Consumer Cyclical")
            self.assertEqual(model, expected)
            self.assertEqual(why, "ticker")

    def test_industry_beats_sector(self):
        """Semiconductors sit under Technology and need the cycle adjustment."""
        model, why = dca.pick_model("ASML", sector="Technology",
                                    industry="Semiconductor Equipment & Materials")
        self.assertEqual((model, why), ("semiconductor", "industry"))

    def test_sector_is_the_next_fallback(self):
        self.assertEqual(dca.pick_model("XYZ", sector="Utilities"), ("utility", "sector"))

    def test_sector_matching_ignores_case_and_spacing(self):
        for spelling in ("Financial Services", "financial services", "FinancialServices"):
            self.assertEqual(dca.pick_model("XYZ", sector=spelling)[0], "bank")

    def test_an_unknown_symbol_still_gets_a_model_and_says_so(self):
        model, why = dca.pick_model("ZZZZ")
        self.assertEqual((model, why), ("compounder", "default"))


class Multipliers(unittest.TestCase):

    def test_the_valuation_multiplier_matches_the_stated_formula(self):
        for v, expected in ((0, 0.5), (20, 0.7), (50, 1.0), (80, 1.3), (100, 1.5)):
            self.assertAlmostEqual(dca.valuation_multiplier(v), expected, places=6)

    def test_band_edges_agree_with_the_multiplier_table(self):
        """The page prints both; they must be two views of one formula."""
        self.assertEqual(dca.score_band(0), "very_expensive")
        self.assertEqual(dca.score_band(19.9), "very_expensive")
        self.assertEqual(dca.score_band(20), "expensive")
        self.assertEqual(dca.score_band(50), "fair")
        self.assertEqual(dca.score_band(79.9), "cheap")
        self.assertEqual(dca.score_band(80), "very_cheap")
        self.assertEqual(dca.score_band(100), "very_cheap")

    def test_an_unknown_earnings_trend_is_neutral_never_a_penalty(self):
        self.assertEqual(dca.earnings_multiplier(None), (1.0, "unknown"))

    def test_earnings_bands(self):
        self.assertEqual(dca.earnings_multiplier(-0.20)[1], "cut_hard")
        self.assertEqual(dca.earnings_multiplier(-0.03)[1], "cut_mild")
        self.assertEqual(dca.earnings_multiplier(0.0)[1], "stable")
        self.assertEqual(dca.earnings_multiplier(0.06)[1], "raised")

    def test_an_unknown_position_is_neutral(self):
        self.assertEqual(dca.portfolio_multiplier(None), (1.0, "unknown"))

    def test_portfolio_bands_tighten_as_concentration_rises(self):
        seen = [dca.portfolio_multiplier(p)[0] for p in (1.0, 5.0, 8.0, 12.0)]
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertEqual(dca.portfolio_multiplier(12.0)[1], "at_limit")

    def test_zero_exposure_is_a_measurement_not_a_gap(self):
        self.assertEqual(dca.portfolio_multiplier(0.0), (1.0, "light"))


class Ceiling(unittest.TestCase):
    """The 1.5x cap is on the product, not on the valuation term."""

    def test_the_cap_binds_on_the_product(self):
        out = dca.combine(1000.0, 1.5, 1.08, 1.0)
        self.assertTrue(out["capped"])
        self.assertEqual(out["multiplier"], dca.MAX_TOTAL_MULTIPLIER)
        self.assertEqual(out["amount"], 1500.0)
        self.assertGreater(out["uncapped_multiplier"], dca.MAX_TOTAL_MULTIPLIER)

    def test_an_uncapped_result_reports_so(self):
        out = dca.combine(1000.0, 1.0, 1.0, 1.0)
        self.assertFalse(out["capped"])
        self.assertEqual(out["amount"], 1000.0)

    def test_overlays_can_take_the_contribution_well_below_base(self):
        out = dca.combine(1000.0, 0.5, 0.70, 0.25)
        self.assertAlmostEqual(out["amount"], 87.5, places=2)
        self.assertFalse(out["capped"])

    def test_no_score_means_no_amount(self):
        out = dca.combine(1000.0, None)
        self.assertIsNone(out["amount"])
        self.assertIsNone(out["multiplier"])


class WorkedExample(unittest.TestCase):
    """The source framework's own worked example, end to end.

    P/E=80, P/FCF=75, EV/EBITDA=82, PEG=65, Peer=70 on the compounder template
    gives E=75.55, V=24.45, M=0.7445 and, on a $5,000 base, $3,722.50. If any
    weight, the direction table or the rounding drifts, this is what says so.
    """

    def test_matches_the_published_arithmetic(self):
        out = dca.evaluate(
            ticker="MSFT", base_dca=5000.0,
            percentiles={"pe": 80, "pfcf": 75, "ev_ebitda": 82, "peg": 65, "peer": 70})
        self.assertEqual(out["model"], "compounder")
        self.assertEqual(out["E"], 75.55)
        self.assertEqual(out["V"], 24.45)
        self.assertEqual(out["m_valuation"], 0.7445)
        self.assertEqual(out["amount"], 3722.50)
        self.assertEqual(out["band"], "expensive")

    def test_the_breakdown_reconstructs_the_total(self):
        """The page shows the terms; they must add up to the headline."""
        out = dca.evaluate(
            ticker="MSFT",
            percentiles={"pe": 80, "pfcf": 75, "ev_ebitda": 82, "peg": 65, "peer": 70})
        self.assertAlmostEqual(sum(f["contribution"] for f in out["factors"]),
                               out["E"], places=2)

    def test_missing_overlays_leave_the_valuation_term_alone(self):
        out = dca.evaluate(ticker="MSFT", base_dca=1000.0,
                           percentiles={"pe": 50, "pfcf": 50, "ev_ebitda": 50,
                                        "peg": 50, "peer": 50})
        self.assertEqual(out["m_earnings"], 1.0)
        self.assertEqual(out["m_portfolio"], 1.0)
        self.assertEqual(out["amount"], 1000.0)

    def test_the_relative_only_example_is_unchanged_by_the_dcf_branch(self):
        """The DCF edition left every relative template alone, so the original
        worked example must still hold when no DCF is supplied. If adding the
        branch moved this, the branch is not additive."""
        out = dca.evaluate(
            ticker="MSFT", base_dca=5000.0,
            percentiles={"pe": 80, "pfcf": 75, "ev_ebitda": 82, "peg": 65, "peer": 70})
        self.assertEqual(out["V"], out["V_rel"])
        self.assertEqual(out["w_dcf"], 0.0)
        self.assertFalse(out["blended"])
        self.assertEqual(out["blend_reason"], "no_dcf")


class DcfWeights(unittest.TestCase):
    """§4-§12 — the weight the absolute branch carries in each template."""

    def test_every_template_has_a_dcf_weight_and_a_form(self):
        """A template with no entry would silently score at w_DCF=0, which is a
        policy decision ("this business is not DCF-able") wearing the costume of
        an oversight."""
        for name in dca.TEMPLATES:
            self.assertIn(name, dca.DCF_WEIGHTS, msg=name)
            self.assertIn(name, dca.DCF_FORMS, msg=name)

    def test_no_weight_exceeds_the_published_ceiling(self):
        """The framework never sanctions more than 30%: the DCF is an anchor,
        never the whole answer."""
        for name, weight in dca.DCF_WEIGHTS.items():
            self.assertGreaterEqual(weight, 0.0, msg=name)
            self.assertLessEqual(weight, dca.MAX_DCF_WEIGHT, msg=name)

    def test_the_published_point_values(self):
        """Transcribed from the §5-§12 section equations, not from §4's ranges."""
        self.assertEqual(dca.DCF_WEIGHTS["compounder"], 0.30)
        self.assertEqual(dca.DCF_WEIGHTS["healthcare_consumer"], 0.25)
        self.assertEqual(dca.DCF_WEIGHTS["semiconductor"], 0.20)
        self.assertEqual(dca.DCF_WEIGHTS["high_growth_software"], 0.15)
        self.assertEqual(dca.DCF_WEIGHTS["amzn"], 0.25)
        self.assertEqual(dca.DCF_WEIGHTS["tsla"], 0.15)
        self.assertEqual(dca.DCF_WEIGHTS["bank"], 0.10)
        self.assertEqual(dca.DCF_WEIGHTS["cyclical"], 0.15)
        self.assertEqual(dca.DCF_WEIGHTS["reit"], 0.15)
        self.assertEqual(dca.DCF_WEIGHTS["utility"], 0.25)

    def test_the_forms_we_cannot_build_are_named_not_approximated(self):
        """§10 and §12. Running FCFF on a bank or a REIT produces a per-share
        number that renders exactly like a valid one."""
        from ystocker import dcf

        self.assertEqual(dca.DCF_FORMS["bank"], "excess_return")
        self.assertEqual(dca.DCF_FORMS["reit"], "affo")
        self.assertEqual(dca.DCF_FORMS["utility"], "fcfe")
        for name in ("bank", "reit", "utility"):
            self.assertIn(dca.DCF_FORMS[name], dcf.FORM_EQUITY_ONLY, msg=name)

    def test_mid_cycle_templates_are_the_cyclical_ones(self):
        """§7/§11. Both are named in the framework as mid-cycle DCFs."""
        self.assertEqual(dca.DCF_MID_CYCLE, frozenset({"semiconductor", "cyclical"}))
        for name in dca.DCF_MID_CYCLE:
            self.assertIn(name, dca.TEMPLATES, msg=name)


class BlendingTheTwoBranches(unittest.TestCase):
    """§13's omission rule, which is the whole reason this is not a sixth factor."""

    def test_the_published_blend(self):
        out = dca.blend_v(44.0, 71.0, 0.30)
        self.assertAlmostEqual(out["V"], 52.1, places=4)
        self.assertTrue(out["blended"])

    def test_a_missing_dcf_renormalises_onto_the_relative_score(self):
        """Never 50. The framework's own emphasis: filling the gap with the
        neutral value silently dilutes the information that *is* there."""
        out = dca.blend_v(80.0, None, 0.30)
        self.assertEqual(out["V"], 80.0)
        self.assertEqual(out["w_dcf"], 0.0)
        self.assertFalse(out["blended"])
        self.assertEqual(out["reason"], "no_dcf")
        # The value a filled-in 50 would have produced, which must not appear.
        self.assertNotAlmostEqual(out["V"], 0.30 * 50 + 0.70 * 80, places=4)

    def test_a_missing_relative_score_is_fatal_even_with_a_dcf(self):
        """A 100%-DCF score is not on the same scale as a 30%-blended one and
        would sit in a ranked column beside scores it cannot be compared to."""
        out = dca.blend_v(None, 71.0, 0.30)
        self.assertIsNone(out["V"])
        self.assertEqual(out["w_dcf"], 0.0)
        self.assertEqual(out["reason"], "no_relative_score")

    def test_a_zero_weight_leaves_the_relative_score_untouched(self):
        out = dca.blend_v(44.0, 99.0, 0.0)
        self.assertEqual(out["V"], 44.0)
        self.assertFalse(out["blended"])
        self.assertEqual(out["reason"], "zero_weight")

    def test_the_weight_cannot_exceed_the_ceiling(self):
        capped = dca.blend_v(40.0, 100.0, 0.95)
        expected = dca.MAX_DCF_WEIGHT * 100.0 + (1 - dca.MAX_DCF_WEIGHT) * 40.0
        self.assertAlmostEqual(capped["V"], round(expected, 2), places=2)
        self.assertEqual(capped["w_dcf"], dca.MAX_DCF_WEIGHT)

    def test_the_blend_is_bounded_by_its_two_branches(self):
        """A weighted average of two numbers in 0-100 cannot leave the interval
        between them. If it does, a sign or a weight is inverted."""
        for v_rel in (0.0, 12.5, 50.0, 88.0, 100.0):
            for v_dcf in (0.0, 33.0, 50.0, 71.0, 100.0):
                out = dca.blend_v(v_rel, v_dcf, 0.30)
                self.assertGreaterEqual(out["V"], min(v_rel, v_dcf) - 1e-9)
                self.assertLessEqual(out["V"], max(v_rel, v_dcf) + 1e-9)

    def test_evaluate_uses_the_templates_weight_by_default(self):
        out = dca.evaluate(
            ticker="MSFT",
            percentiles={"pe": 50, "pfcf": 50, "ev_ebitda": 50, "peg": 50, "peer": 50},
            dcf={"V": 100.0})
        self.assertEqual(out["w_dcf"], dca.DCF_WEIGHTS["compounder"])
        self.assertAlmostEqual(out["V"], 0.30 * 100.0 + 0.70 * 50.0, places=2)

    def test_an_explicit_weight_overrides_the_template(self):
        """§4's dynamic down-weighting needs this: 'lower w_DCF by 5-15 points
        and reallocate proportionally to the relative factors'."""
        out = dca.evaluate(
            ticker="MSFT",
            percentiles={"pe": 50, "pfcf": 50, "ev_ebitda": 50, "peg": 50, "peer": 50},
            dcf={"V": 100.0}, w_dcf=0.15)
        self.assertEqual(out["w_dcf"], 0.15)

    def test_a_refused_dcf_payload_scores_as_relative_only(self):
        """The shape ``ystocker.dcf`` actually returns when it declines."""
        from ystocker import dcf as dcf_mod

        refused = dcf_mod.score(price=100.0, base=None)
        out = dca.evaluate(
            ticker="MSFT",
            percentiles={"pe": 50, "pfcf": 50, "ev_ebitda": 50, "peg": 50, "peer": 50},
            dcf=refused)
        self.assertEqual(out["V"], 50.0)
        self.assertEqual(out["w_dcf"], 0.0)
        self.assertFalse(out["blended"])


class DcfWorkedExample(unittest.TestCase):
    """§15, end to end: both branches, both overlays, one contribution.

    A mature tech company at $400 with Bear/Base/Bull DCF fair values of
    $360/$500/$560 and c=0.80 gives V_DCF=71.0. Relative percentiles of
    P/E=60, P/FCF=55, EV/EBITDA=65, PEG=45, Peer=50 give E_REL=56.0 and
    V_REL=44.0. Blended at 30%, V=52.1 and M_valuation=1.021. A mild earnings
    cut (0.90) and moderate existing exposure (0.85) take the final multiplier
    to 0.781 and, on a $5,000 base, about $3,905.

    This is the test that fails if the two branches are ever averaged, if the
    weight drifts, or if a refusal starts scoring as 50.
    """

    def setUp(self):
        from ystocker import dcf

        self.dcf = dcf.score(price=400.0, bear=360.0, base=500.0, bull=560.0,
                             confidence=0.80)
        self.out = dca.evaluate(
            ticker="MSFT", base_dca=5000.0,
            percentiles={"pe": 60, "pfcf": 55, "ev_ebitda": 65,
                         "peg": 45, "peer": 50},
            dcf=self.dcf, eps_drift=-0.03, position_pct=5.0)

    def test_the_dcf_branch_matches(self):
        self.assertAlmostEqual(self.dcf["raw"], 76.25, places=4)
        self.assertAlmostEqual(self.dcf["V"], 71.0, places=4)

    def test_the_relative_branch_matches(self):
        self.assertEqual(self.out["E"], 56.0)
        self.assertEqual(self.out["V_rel"], 44.0)

    def test_the_blended_score_matches(self):
        self.assertEqual(self.out["w_dcf"], 0.30)
        self.assertAlmostEqual(self.out["V"], 52.1, places=2)
        self.assertEqual(self.out["m_valuation"], 1.021)
        self.assertEqual(self.out["band"], "fair")

    def test_the_overlays_and_the_final_contribution_match(self):
        self.assertEqual(self.out["m_earnings"], 0.90)
        self.assertEqual(self.out["m_portfolio"], 0.85)
        self.assertAlmostEqual(self.out["multiplier"], 0.7811, places=3)
        # The document rounds the multiplier to three places before multiplying
        # and reports $3,905. ``combine`` carries full precision and rounds once
        # at the end, so the cent is not expected to agree — the dollar is.
        self.assertEqual(round(self.out["amount"]), 3905)

    def test_a_dcf_upside_does_not_by_itself_raise_the_contribution(self):
        """§15's own reading: 'DCF 有 upside' does not automatically mean buy
        more. The final multiplier is below 1.0 despite a cheap DCF."""
        self.assertGreater(self.dcf["V"], 50.0)
        self.assertLess(self.out["multiplier"], 1.0)

    def test_the_blend_moved_the_score(self):
        """Guards against the branch being plumbed in but never applied — the
        failure that looks like everything working."""
        without = dca.evaluate(
            ticker="MSFT", base_dca=5000.0,
            percentiles={"pe": 60, "pfcf": 55, "ev_ebitda": 65,
                         "peg": 45, "peer": 50},
            eps_drift=-0.03, position_pct=5.0)
        self.assertEqual(without["V"], 44.0)
        self.assertGreater(self.out["V"], without["V"])


class RoutingCorrections(unittest.TestCase):
    """Templates for companies the label fallback gets wrong.

    A template is not cosmetic: it decides which five multiples the score is
    built from, so a misrouted company is not slightly off, it is measured
    against the wrong yardstick. Each case below was observed on the live
    ranked table.
    """

    def test_a_card_network_is_not_a_bank(self):
        """40% of the bank template is P/TBV, and a payment network carries
        almost no tangible book. The factor came back as noise or not at all,
        and losing it dropped the template under MIN_SURVIVING_WEIGHT — so Visa
        and Mastercard were not merely mis-scored, they were unscorable."""
        for ticker in ("V", "MA"):
            model, why = dca.pick_model(ticker, "Financial Services",
                                        "Credit Services")
            self.assertEqual(model, "compounder", msg=ticker)
            self.assertEqual(why, "industry", msg=ticker)

    def test_real_banks_still_route_to_the_bank_template(self):
        """The credit-services row sits before the bank rows, so it must not
        shadow them."""
        for ticker in ("JPM", "MUFG", "GS"):
            self.assertEqual(
                dca.pick_model(ticker, "Financial Services",
                               "Banks - Diversified")[0], "bank", msg=ticker)

    def test_the_korean_listings_are_semiconductors(self):
        """Yahoo publishes no usable sector or industry for these — the same
        metadata failure that reports them as MUTUALFUND with a Morningstar id
        for a name. They fell through to `compounder`, the one template with no
        cycle adjustment, for two of the largest memory makers in the world."""
        for ticker in ("005930.KQ", "000660.KQ"):
            model, why = dca.pick_model(ticker, None, None)
            self.assertEqual(model, "semiconductor", msg=ticker)
            self.assertEqual(why, "ticker", msg=ticker)

    def test_high_growth_software_is_named_because_no_label_separates_it(self):
        """Yahoo files Microsoft and Cloudflare under the same
        "Software - Infrastructure", so the industry row cannot route one to
        each. The few that matter are named; the label keeps the mature
        reading for everything else."""
        for ticker in ("NET", "CRWD", "MDB"):
            self.assertEqual(
                dca.pick_model(ticker, "Technology", "Software - Infrastructure")[0],
                "high_growth_software", msg=ticker)
        for ticker in ("MSFT", "ORCL"):
            self.assertEqual(
                dca.pick_model(ticker, "Technology", "Software - Infrastructure")[0],
                "compounder", msg=ticker)

    def test_naming_a_ticker_does_not_move_its_whole_industry(self):
        """SNDK and LITE are routed by ticker precisely because "Computer
        Hardware" and "Communication Equipment" also cover Dell and Cisco."""
        self.assertEqual(dca.pick_model("SNDK", "Technology", "Computer Hardware")[0],
                         "semiconductor")
        self.assertEqual(dca.pick_model("DELL", "Technology", "Computer Hardware")[0],
                         "compounder")
        self.assertEqual(dca.pick_model("LITE", "Technology", "Communication Equipment")[0],
                         "semiconductor")
        self.assertEqual(dca.pick_model("CSCO", "Technology", "Communication Equipment")[0],
                         "compounder")

    def test_leveraged_media_is_named_rather_than_routed_by_label(self):
        """"Entertainment" also covers Netflix and Disney, both of which score
        sensibly as compounders, so the label must stay put."""
        self.assertEqual(
            dca.pick_model("WBD", "Communication Services", "Entertainment")[0],
            "cyclical")
        for ticker in ("NFLX", "DIS"):
            self.assertEqual(
                dca.pick_model(ticker, "Communication Services", "Entertainment")[0],
                "compounder", msg=ticker)

    def test_every_named_ticker_points_at_a_real_template(self):
        for ticker, model in dca.TICKER_MODELS.items():
            self.assertIn(model, dca.TEMPLATES, msg=f"{ticker} -> {model}")

    def test_every_industry_row_points_at_a_real_template(self):
        for needle, model in dca.INDUSTRY_MODELS:
            self.assertIn(model, dca.TEMPLATES, msg=f"{needle} -> {model}")


class Translations(unittest.TestCase):
    """Every key the page composes in JS must exist in both languages.

    ``I18n.apply()`` cannot reach a string set from JavaScript, so a band, model
    or factor name with no entry falls back to its raw identifier and an English
    label ends up on a Chinese page. These are exactly the keys built by string
    concatenation (``'dca.band_' + key``), which no static scan of the template
    would catch.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "ystocker" / "static" / "i18n.js").read_text(encoding="utf-8")

    def _entry_body(self, key: str) -> str:
        """The ``{ en: …, zh: … }`` body for *key*, brace-balanced.

        Not a single regex. The obvious ``\\{([^}]*)\\}`` stops at the first
        closing brace, which is fine until a *value* contains one — and
        ``'dca.doc_title'`` legitimately does, since ``{arg}`` is the ticker
        substitution slot. The naive form truncated its body to ``" en: '{arg"``
        and reported the Chinese string as missing when it was right there,
        which is a test that fails on correct code: the worst kind, because the
        obvious response is to "fix" the key.
        """
        opener = re.compile(r"['\"]" + re.escape(key) + r"['\"]\s*:\s*\{")
        match = opener.search(self.js)
        self.assertIsNotNone(match, f"i18n.js has no entry for {key!r}")
        start = match.end()
        depth = 1
        for i in range(start, len(self.js)):
            char = self.js[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i]
        self.fail(f"i18n.js entry for {key!r} is not brace-balanced")

    def _assert_key(self, key: str):
        body = self._entry_body(key)
        self.assertIn("en:", body, f"{key!r} has no English string")
        self.assertIn("zh:", body, f"{key!r} has no Chinese string")

    def test_every_factor_has_a_label(self):
        for factor in dca.DIRECTION:
            self._assert_key(f"dca.f_{factor}")

    def test_every_model_has_a_label(self):
        for model in dca.TEMPLATES:
            self._assert_key(f"dca.model_{model}")

    def test_every_band_has_a_label_and_a_meaning(self):
        for _upper, key in dca._BANDS:
            self._assert_key(f"dca.band_{key}")
            self._assert_key(f"dca.bandmean_{key}")

    def test_every_overlay_band_has_a_label(self):
        for _upper, key, _mult in dca._EARNINGS_BANDS:
            self._assert_key(f"dca.earn_{key}")
        self._assert_key("dca.earn_unknown")
        for _upper, key, _mult in dca._PORTFOLIO_BANDS:
            self._assert_key(f"dca.port_{key}")
        self._assert_key("dca.port_unknown")

    def test_every_dcf_refusal_has_an_explanation(self):
        """Composed as ``'dca.dcf_r_' + reason``. A refusal is the *normal* path
        for three of the ten templates, so a missing key would put a raw
        identifier on the page at the moment it is explaining itself."""
        from ystocker import dcf

        for reason in dcf.REFUSALS:
            self._assert_key(f"dca.dcf_r_{reason}")

    def test_every_dcf_note_has_a_label(self):
        """``'dca.dcf_n_' + note``. These are the clamps and assumptions the
        model is required to disclose (§2), so an untranslated one defeats the
        disclosure."""
        for note in ("growth_clamped", "spread_clamped", "beta_assumed",
                     "beta_clamped", "cost_of_debt_assumed", "structure_assumed",
                     "wacc_clamped", "wacc_overridden", "terminal_heavy",
                     "mid_cycle_base", "partial_scenarios_ignored"):
            self._assert_key(f"dca.dcf_n_{note}")

    def test_every_dcf_scenario_and_basis_has_a_label(self):
        from ystocker import dcf

        for name in dcf.SCENARIO_WEIGHTS:
            self._assert_key(f"dca.dcf_{name}")
        for basis in ("three_scenario", "base_only"):
            self._assert_key(f"dca.dcf_basis_{basis}")
        for source in ("derived", "override"):
            self._assert_key(f"dca.dcf_src_{source}")

    def test_every_figure_on_the_dcf_card_has_an_explanation(self):
        """The card shows eight numbers. A reader who cannot tell which are
        measured and which are assumed cannot weigh any of them, so each one
        carries a tooltip — and a tooltip that falls back to its raw key is
        worse than none, because it looks like a bug rather than a gap."""
        for field in ("v", "weight", "conf", "price", "wacc", "g", "tv",
                      "sens", "years", "growth", "case", "fv", "up", "scenv"):
            self._assert_key(f"dca.dcf_h_{field}")
        self._assert_key("dca.dcf_help")
        self._assert_key("dca.dcf_help_body")

    def test_the_three_multipliers_explain_how_they_are_calculated(self):
        """The tiles showed 1.21× / 0.90× / 0.85× and a band name, with nothing
        saying where any of them came from."""
        for field in ("val", "earn", "port"):
            self._assert_key(f"dca.m_h_{field}")
        self._assert_key("dca.m_help")
        self._assert_key("dca.m_help_body")

    def test_the_browser_tab_keys_exist_in_both_languages(self):
        """``<title>`` is server-rendered Jinja, so ``I18n.apply()`` cannot
        reach it; base.html emits a meta tag the client reads instead. A missing
        key leaves the tab in English on a Chinese page, which is what it did."""
        self._assert_key("dcx.title")
        self._assert_key("dca.doc_title")

    def test_the_ticker_tab_title_carries_its_substitution_slot(self):
        """``{arg}`` is replaced with the ticker by ``I18n.apply()``. Without it
        every ticker's tab would read the same."""
        body = self._entry_body("dca.doc_title")
        self.assertEqual(body.count("{arg}"), 2,
                         "both languages need the substitution slot")

    def test_every_model_reason_has_a_label(self):
        for reason in ("ticker", "industry", "sector", "default", "explicit"):
            self._assert_key(f"dca.why_{reason}")

    def test_the_python_factor_labels_cover_every_direction_entry(self):
        """The server-side fallback, used when a translation is missing."""
        self.assertEqual(set(dca.FACTOR_LABELS), set(dca.DIRECTION))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
