"""
Tests for the DCF branch (``ystocker.dcf``).

No app, no network, no disk, no clock — the module takes its "today" as an
argument precisely so this file can prove the staleness rule without one.

A DCF is mostly assumption, so what is worth testing is not that the discounting
arithmetic is right (it is four lines) but the handful of places where this can
hand back a *plausible* fair value that is wrong. Every one of them ends up
scaling somebody's contribution and none looks broken on the page:

* A refused DCF filled in as 50 is the framework's own headline warning. It is
  in range, it is the neutral value, and it drags every score toward the middle
  by ``w_DCF`` while rendering identically to a measured 50.
* ``WACC <= g`` makes the terminal value negative or enormous. Clamping it
  produces a number; refusing is the only honest answer.
* An unclamped upside map turns a broken model into a 4.5x contribution.
* A growth rate extrapolated from a peak year, or a projection started from a
  peak *level*, values a cyclical as though the peak were the floor — two
  different errors that need two different guards.
* Filling a missing Bear case with the Base case narrows the band and therefore
  raises confidence, in exactly the situation where less is known.
* An unparseable valuation date read as "current" defeats the staleness rule
  that exists to stop an old DCF scoring a new price.
"""
from __future__ import annotations

import math
import unittest

from ystocker import dcf


class UpsideMap(unittest.TestCase):
    """§3 — mapping ``FV / P - 1`` onto 0-100."""

    def test_the_published_anchors_map_exactly(self):
        for upside, expected in dcf.UPSIDE_ANCHORS:
            self.assertAlmostEqual(dcf.v_from_upside(upside), expected, places=6,
                                   msg=f"anchor {upside} should map to {expected}")

    def test_the_frameworks_own_interpolation_check(self):
        """§3 states +20% lands at ~78.3, two thirds between 65 and 85."""
        self.assertAlmostEqual(dcf.v_from_upside(0.20), 78.3333, places=3)

    def test_fair_value_equal_to_price_is_neutral(self):
        self.assertEqual(dcf.v_from_upside(0.0), 50.0)

    def test_the_map_is_monotone(self):
        """A cheaper stock must never score dearer. A non-monotone map would
        invert the whole branch over some range and stay in 0-100 throughout."""
        previous = -1.0
        step = -0.60
        while step <= 0.60:
            value = dcf.v_from_upside(step)
            self.assertGreaterEqual(value, previous, msg=f"fell at u={step:.3f}")
            previous = value
            step += 0.01

    def test_extremes_are_clamped_not_extrapolated(self):
        """A DCF saying a large cap is worth 10x its price is a broken model,
        not a 4.5x contribution. §3 says so for the top end in as many words."""
        self.assertEqual(dcf.v_from_upside(9.0), 100.0)
        self.assertEqual(dcf.v_from_upside(-0.99), 0.0)
        self.assertEqual(dcf.v_from_upside(0.40), 100.0)
        self.assertEqual(dcf.v_from_upside(-0.40), 0.0)

    def test_a_non_number_is_refused(self):
        for bad in (None, float("nan"), float("inf"), "0.2", True):
            self.assertIsNone(dcf.v_from_upside(bad), msg=repr(bad))


class Scenarios(unittest.TestCase):
    """§3 — the 25/50/25 weighting and the confidence shrink."""

    def test_the_published_weights(self):
        self.assertEqual(dcf.SCENARIO_WEIGHTS,
                         {"bear": 0.25, "base": 0.50, "bull": 0.25})
        self.assertAlmostEqual(sum(dcf.SCENARIO_WEIGHTS.values()), 1.0, places=9)

    def test_combining_three_scenarios(self):
        self.assertAlmostEqual(dcf.combine_scenarios(35, 85, 100), 76.25, places=6)

    def test_a_missing_wing_is_not_filled_with_the_base_case(self):
        """Substituting Base for a missing Bear narrows the band, which *raises*
        the implied confidence exactly where less is known. The base-only path
        with its lower ceiling is the framework's answer instead."""
        self.assertIsNone(dcf.combine_scenarios(None, 85, 100))
        self.assertIsNone(dcf.combine_scenarios(35, 85, None))

    def test_confidence_shrinks_toward_neutral(self):
        self.assertAlmostEqual(dcf.apply_confidence(76.25, 0.80), 71.0, places=6)
        self.assertAlmostEqual(dcf.apply_confidence(76.25, 1.0), 76.25, places=6)
        self.assertAlmostEqual(dcf.apply_confidence(20.0, 0.5), 35.0, places=6)

    def test_confidence_is_clamped_to_the_published_band(self):
        self.assertEqual(dcf.apply_confidence(100.0, 5.0),
                         dcf.apply_confidence(100.0, dcf.CONFIDENCE_CEILING))
        self.assertEqual(dcf.apply_confidence(100.0, 0.0),
                         dcf.apply_confidence(100.0, dcf.CONFIDENCE_FLOOR))

    def test_a_neutral_raw_score_is_unmoved_by_any_confidence(self):
        for c in (0.5, 0.75, 1.0):
            self.assertAlmostEqual(dcf.apply_confidence(50.0, c), 50.0, places=9)


class WorkedExample(unittest.TestCase):
    """§15, end to end.

    Price $400; Bear/Base/Bull fair values $360/$500/$560, so upsides of
    -10%/+25%/+40% map to 35/85/100. Weighted 25/50/25 that is 76.25, and at
    c=0.80 the reported V_DCF is 71.0. If any of these move, the engine has
    stopped implementing the document it cites.
    """

    def setUp(self):
        self.out = dcf.score(price=400.0, bear=360.0, base=500.0, bull=560.0,
                             confidence=0.80)

    def test_matches_the_published_arithmetic(self):
        self.assertFalse(self.out["refused"])
        self.assertAlmostEqual(self.out["raw"], 76.25, places=4)
        self.assertAlmostEqual(self.out["V"], 71.0, places=4)
        self.assertEqual(self.out["basis"], "three_scenario")

    def test_each_scenario_is_reported_with_its_own_upside_and_score(self):
        by_name = {s["name"]: s for s in self.out["scenarios"]}
        self.assertAlmostEqual(by_name["bear"]["upside"], -0.10, places=6)
        self.assertAlmostEqual(by_name["base"]["upside"], 0.25, places=6)
        self.assertAlmostEqual(by_name["bull"]["upside"], 0.40, places=6)
        self.assertAlmostEqual(by_name["bear"]["V"], 35.0, places=4)
        self.assertAlmostEqual(by_name["base"]["V"], 85.0, places=4)
        self.assertAlmostEqual(by_name["bull"]["V"], 100.0, places=4)

    def test_the_scenario_weights_reconstruct_the_raw_score(self):
        total = sum(s["weight"] * s["V"] for s in self.out["scenarios"])
        self.assertAlmostEqual(total, self.out["raw"], places=4)


class BaseOnly(unittest.TestCase):
    """§3 — one scenario is not a range."""

    def test_base_only_caps_confidence_at_the_published_ceiling(self):
        out = dcf.score(price=400.0, base=500.0, confidence=1.0)
        self.assertEqual(out["basis"], "base_only")
        self.assertEqual(out["confidence"], dcf.BASE_ONLY_CONFIDENCE_CEILING)
        # 50 + 0.75 * (85 - 50) = 76.25
        self.assertAlmostEqual(out["V"], 76.25, places=4)

    def test_base_only_scores_lower_than_the_same_case_with_wings(self):
        """The shrink must actually cost something, or the ceiling is decorative."""
        alone = dcf.score(price=400.0, base=500.0)
        with_wings = dcf.score(price=400.0, bear=500.0, base=500.0, bull=500.0,
                               confidence=1.0)
        self.assertLess(alone["V"], with_wings["V"])

    def test_one_wing_is_treated_as_base_only_and_says_so(self):
        out = dcf.score(price=400.0, base=500.0, bull=560.0)
        self.assertEqual(out["basis"], "base_only")
        self.assertIn("partial_scenarios_ignored", out["notes"])


class Refusals(unittest.TestCase):
    """Every declined path, and the one thing they must all have in common."""

    def test_a_refusal_is_never_a_fifty(self):
        """The framework's headline warning. A refused branch must be absent,
        not neutral — 50 renders identically to a measured 50 and silently
        pulls every blended score toward the middle."""
        for kwargs in ({"price": None, "base": 100.0},
                       {"price": 100.0, "base": None},
                       {"price": 0.0, "base": 100.0},
                       {"price": 100.0, "base": -5.0}):
            out = dcf.score(**kwargs)
            self.assertTrue(out["refused"], msg=repr(kwargs))
            self.assertIsNone(out["V"], msg=repr(kwargs))
            self.assertEqual(out["w_dcf_hint"], 0.0, msg=repr(kwargs))

    def test_every_refusal_reason_is_declared(self):
        """A reason absent from REFUSALS has no translation, so the page would
        render a raw identifier at the moment it is explaining itself."""
        seen = [
            dcf.score(price=None, base=1.0)["reason"],
            dcf.score(price=1.0, base=None)["reason"],
            dcf.score(price=1.0, base=1.0, valuation_date="2020-01-01",
                      as_of="2026-01-01")["reason"],
            dcf.from_fundamentals(price=1.0, fcf_series=[1.0], shares=None,
                                  form=dcf.FORM_FCFF)["reason"],
            dcf.from_fundamentals(price=1.0, fcf_series=[], shares=1.0)["reason"],
            dcf.from_fundamentals(price=1.0, fcf_series=[-1.0], shares=1.0)["reason"],
            dcf.from_fundamentals(price=1.0, fcf_series=[1.0, 2.0], shares=1.0)["reason"],
            dcf.from_fundamentals(price=1.0, fcf_series=[1.0], shares=1.0,
                                  form="excess_return")["reason"],
        ]
        for reason in seen:
            self.assertIn(reason, dcf.REFUSALS)

    def test_a_stale_valuation_is_refused_rather_than_scored(self):
        """§13. An old DCF against a current price reads as cheap simply
        because the stock fell."""
        fresh = dcf.score(price=400.0, base=500.0,
                          valuation_date="2026-09-01", as_of="2026-09-12")
        stale = dcf.score(price=400.0, base=500.0,
                          valuation_date="2025-01-01", as_of="2026-09-12")
        self.assertFalse(fresh["refused"])
        self.assertTrue(stale["refused"])
        self.assertEqual(stale["reason"], "valuation_stale")

    def test_an_unreadable_date_does_not_read_as_current(self):
        """``None`` age means "we cannot tell", which must not silently pass the
        staleness gate as though it were zero days old."""
        self.assertIsNone(dcf._age_days("not-a-date", "2026-09-12"))
        self.assertIsNone(dcf._age_days("2026-09-12", None))

    def test_a_bank_is_refused_by_form_not_by_arithmetic(self):
        """§10. FCFF on a bank produces a number; the point is that it should
        not exist, so the refusal has to happen before the maths."""
        out = dcf.from_fundamentals(price=100.0, shares=1e9,
                                    fcf_series=[1e9, 1.1e9, 1.2e9, 1.3e9, 1.4e9],
                                    form="excess_return")
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "form_not_applicable")

    def test_negative_free_cash_flow_is_absent_not_expensive(self):
        out = dcf.from_fundamentals(price=100.0, shares=1e9,
                                    fcf_series=[1e9, 1e9, 1e9, -2e8])
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "negative_fcf")
        self.assertIsNone(out["V"])


class TerminalValue(unittest.TestCase):
    """§13 — the two guards on the part of a DCF that carries most of the value."""

    def test_wacc_at_or_below_g_has_no_terminal_value(self):
        self.assertIsNone(dcf.terminal_value(100.0, 0.02, 0.025))
        self.assertIsNone(dcf.terminal_value(100.0, 0.025, 0.025))

    def test_a_spread_under_the_floor_is_refused_rather_than_clamped(self):
        """Just inside the singularity the formula returns a huge finite number,
        which is the dangerous case: it looks like an answer."""
        self.assertIsNone(dcf.terminal_value(100.0, 0.030, 0.025))
        self.assertIsNotNone(dcf.terminal_value(100.0, 0.041, 0.025))

    def test_the_gordon_formula_is_the_published_one(self):
        tv = dcf.terminal_value(100.0, 0.10, 0.02)
        self.assertAlmostEqual(tv, 100.0 * 1.02 / 0.08, places=6)

    def test_sensitivity_is_measured_not_inferred(self):
        """§13 asks whether the result moves violently for +0.5pp on g. That is
        a measurement, so it is measured rather than proxied by terminal share."""
        common = dict(fcf0=1e9, initial_growth=0.05, discount_rate=0.09,
                      terminal_growth=0.025, years=10, shares=1e9)
        measured = dcf.growth_sensitivity(**common)
        base = dcf.fair_value(**common)["per_share"]
        bumped = dcf.fair_value(**{**common, "terminal_growth": 0.030})["per_share"]
        self.assertAlmostEqual(measured, abs(bumped - base) / base, places=6)

    def test_sitting_on_the_singularity_reports_infinite_sensitivity(self):
        """A probe that pushes g through the spread floor has not "passed"."""
        out = dcf.growth_sensitivity(fcf0=1e9, initial_growth=0.03,
                                     discount_rate=0.042, terminal_growth=0.025,
                                     years=10, shares=1e9)
        self.assertTrue(out is None or math.isinf(out) or out > dcf.MAX_SENSITIVITY)


class FairValue(unittest.TestCase):
    """The valuation itself, and the equity bridge."""

    def test_a_zero_growth_perpetuity_matches_the_closed_form(self):
        """With g=0 everywhere the whole thing collapses to FCF / WACC, which is
        an arithmetic identity the projection must reproduce."""
        out = dcf.fair_value(fcf0=100.0, initial_growth=0.0, discount_rate=0.10,
                             terminal_growth=0.0, years=40, shares=1.0)
        self.assertAlmostEqual(out["equity_value"], 100.0 / 0.10, delta=1.0)

    def test_the_equity_bridge_adds_cash_and_subtracts_debt(self):
        common = dict(fcf0=1e9, initial_growth=0.04, discount_rate=0.09,
                      terminal_growth=0.025, years=10, shares=1e8)
        bare = dcf.fair_value(**common)
        bridged = dcf.fair_value(**common, cash=5e9, debt=2e9)
        self.assertAlmostEqual(bridged["equity_value"] - bare["equity_value"],
                               3e9, delta=1.0)

    def test_the_terminal_share_is_reported_and_is_a_fraction_of_ev(self):
        out = dcf.fair_value(fcf0=1e9, initial_growth=0.04, discount_rate=0.09,
                             terminal_growth=0.025, years=10, shares=1e8)
        self.assertAlmostEqual(
            out["pv_explicit"] + out["pv_terminal"], out["enterprise_value"],
            delta=1.0)
        self.assertAlmostEqual(
            out["terminal_share"],
            out["pv_terminal"] / out["enterprise_value"], places=4)

    def test_growth_fades_to_the_terminal_rate_by_the_last_year(self):
        """A constant rate to a cliff-edge terminal value puts a discontinuity
        exactly where most of the value is."""
        flows = dcf.project_flows(100.0, 0.20, 0.02, 10)
        last_step = flows[-1] / flows[-2] - 1.0
        first_step = flows[0] / 100.0 - 1.0
        self.assertAlmostEqual(first_step, 0.20, places=6)
        self.assertAlmostEqual(last_step, 0.02, places=6)

    def test_a_missing_share_count_refuses_rather_than_returning_ev(self):
        """A partial dict invites a caller to divide by a share count of its
        own, which is how two per-share figures come to disagree."""
        self.assertIsNone(dcf.fair_value(fcf0=1e9, initial_growth=0.04,
                                         discount_rate=0.09, shares=None))


class WACC(unittest.TestCase):
    """The discount rate, and the fact that every assumption in it is reported."""

    def test_all_equity_cost_is_capm(self):
        out = dcf.wacc(beta=1.2, risk_free=0.04, erp=0.05, equity_value=1e9, debt=0)
        self.assertAlmostEqual(out["cost_equity"], 0.04 + 1.2 * 0.05, places=9)
        self.assertAlmostEqual(out["wacc"], 0.10, places=9)

    def test_debt_lowers_the_rate_through_the_tax_shield(self):
        levered = dcf.wacc(beta=1.0, risk_free=0.04, erp=0.05,
                           equity_value=1e9, debt=1e9, cost_of_debt=0.05,
                           tax_rate=0.21)
        unlevered = dcf.wacc(beta=1.0, risk_free=0.04, erp=0.05,
                             equity_value=1e9, debt=0)
        self.assertLess(levered["wacc"], unlevered["wacc"])

    def test_an_absurd_beta_is_clamped_and_the_clamp_is_reported(self):
        out = dcf.wacc(beta=7.5, equity_value=1e9, debt=0)
        self.assertEqual(out["beta"], dcf.BETA_CEILING)
        self.assertIn("beta_clamped", out["notes"])

    def test_a_missing_beta_is_assumed_and_says_so(self):
        out = dcf.wacc(beta=None, equity_value=1e9, debt=0)
        self.assertIn("beta_assumed", out["notes"])

    def test_no_capital_structure_falls_back_to_cost_of_equity(self):
        out = dcf.wacc(beta=1.0, equity_value=None, debt=None)
        self.assertEqual(out["weight_equity"], 1.0)
        self.assertIn("structure_assumed", out["notes"])


class GrowthProfile(unittest.TestCase):
    """Scenarios from the company's own dispersion (§4's predictability rule)."""

    def test_a_steady_compounder_gets_a_narrow_band(self):
        steady = dcf.growth_profile([100, 110, 121, 133.1, 146.41])
        self.assertAlmostEqual(steady["observed_cagr"], 0.10, places=4)
        self.assertAlmostEqual(steady["observed_spread"], 0.0, places=6)
        # Floored, so the 25/50/25 weighting is not decorative.
        self.assertEqual(steady["spread"], dcf.SPREAD_FLOOR)

    def test_a_volatile_series_gets_a_wider_band_than_a_steady_one(self):
        steady = dcf.growth_profile([100, 110, 121, 133.1, 146.41])
        choppy = dcf.growth_profile([100, 180, 90, 200, 146.41])
        self.assertGreater(choppy["spread"], steady["spread"])

    def test_a_peak_growth_rate_is_clamped_and_the_clamp_is_reported(self):
        """§13: extrapolating a peak for ever. The clamp must be visible or it
        reads as a deliberate forecast."""
        out = dcf.growth_profile([100, 300, 900, 2700, 8100])
        self.assertEqual(out["base"], dcf.GROWTH_CEILING)
        self.assertIn("growth_clamped", out["notes"])

    def test_the_window_trims_to_the_recent_positive_run(self):
        """A loss eight years ago must not delete a company that has generated
        cash ever since — but it must not be averaged through either."""
        out = dcf.growth_profile([-50, 100, 110, 121, 133.1])
        self.assertEqual(out["observations"], 4)
        self.assertAlmostEqual(out["observed_cagr"], 0.10, places=4)

    def test_too_short_a_run_returns_nothing(self):
        self.assertIsNone(dcf.growth_profile([100, 110, 121]))
        self.assertIsNone(dcf.growth_profile([100, -5, 110, 121, 133]))


class MidCycle(unittest.TestCase):
    """§7/§11 — the guard the growth clamp does not provide."""

    def test_a_cyclical_is_projected_from_the_mean_not_the_peak(self):
        peaky = [1.0e9, 1.2e9, 0.9e9, 1.1e9, 2.6e9]
        common = dict(price=40.0, shares=1e9, fcf_series=peaky,
                      beta=1.0, cash=0.0, debt=0.0)
        spot = dcf.from_fundamentals(**common, mid_cycle=False)
        mid = dcf.from_fundamentals(**common, mid_cycle=True)
        self.assertFalse(spot["refused"])
        self.assertFalse(mid["refused"])
        self.assertLess(mid["model"]["fcf0"], spot["model"]["fcf0"])
        # Starting lower means a lower fair value, so a lower cheapness score.
        self.assertLess(mid["V"], spot["V"])
        self.assertIn("mid_cycle_base", mid["notes"])

    def test_the_latest_year_is_still_reported_beside_the_mid_cycle_base(self):
        out = dcf.from_fundamentals(price=40.0, shares=1e9, beta=1.0,
                                    fcf_series=[1e9, 1.2e9, 0.9e9, 1.1e9, 2.6e9],
                                    mid_cycle=True)
        self.assertAlmostEqual(out["model"]["fcf_latest"], 2.6e9, places=2)
        self.assertNotAlmostEqual(out["model"]["fcf0"], 2.6e9, places=2)


class DerivedConfidence(unittest.TestCase):
    """``c`` must be a property of the evidence, checkable against the page."""

    def test_every_deduction_lowers_it_and_it_floors(self):
        best = dcf.derive_confidence(observations=10, terminal_share=0.5, spread=0.03)
        worst = dcf.derive_confidence(observations=4, terminal_share=0.85, spread=0.14)
        self.assertEqual(best, dcf.CONFIDENCE_CEILING)
        self.assertGreaterEqual(worst, dcf.CONFIDENCE_FLOOR)
        self.assertLess(worst, best)

    def test_it_never_leaves_the_published_band(self):
        for obs in (1, 4, 5, 6, 20):
            for share in (0.0, 0.5, 0.75, 0.85, 1.0):
                for spread in (0.0, 0.05, 0.08, 0.20):
                    c = dcf.derive_confidence(observations=obs,
                                              terminal_share=share, spread=spread)
                    self.assertGreaterEqual(c, dcf.CONFIDENCE_FLOOR)
                    self.assertLessEqual(c, dcf.CONFIDENCE_CEILING)


class FromFundamentals(unittest.TestCase):
    """The derived path, end to end, on literals."""

    def setUp(self):
        self.out = dcf.from_fundamentals(
            price=100.0, shares=1e9, beta=1.0,
            fcf_series=[5.0e9, 5.5e9, 6.0e9, 6.6e9, 7.2e9],
            cash=10e9, debt=5e9, price_date="2026-09-11", as_of="2026-09-12")

    def test_it_produces_a_three_scenario_score(self):
        self.assertFalse(self.out["refused"], self.out.get("reason"))
        self.assertEqual(self.out["basis"], "three_scenario")
        self.assertEqual(len(self.out["scenarios"]), 3)
        self.assertEqual(self.out["source"], "derived")

    def test_bear_scores_no_higher_than_bull(self):
        by_name = {s["name"]: s for s in self.out["scenarios"]}
        self.assertLessEqual(by_name["bear"]["V"], by_name["base"]["V"])
        self.assertLessEqual(by_name["base"]["V"], by_name["bull"]["V"])

    def test_every_assumption_is_reported(self):
        """§2 requires WACC, terminal growth and the reinvestment path to be
        stated. An assumption the reader cannot see is one they cannot dispute."""
        model = self.out["model"]
        for key in ("form", "years", "growth", "capital", "terminal_growth",
                    "terminal_share", "sensitivity", "fcf0", "shares"):
            self.assertIn(key, model)
        for key in ("wacc", "cost_equity", "beta", "risk_free", "erp", "tax_rate"):
            self.assertIn(key, model["capital"])

    def test_a_cheaper_price_scores_cheaper(self):
        dear = dcf.from_fundamentals(price=400.0, shares=1e9, beta=1.0,
                                     fcf_series=[5.0e9, 5.5e9, 6.0e9, 6.6e9, 7.2e9])
        cheap = dcf.from_fundamentals(price=40.0, shares=1e9, beta=1.0,
                                      fcf_series=[5.0e9, 5.5e9, 6.0e9, 6.6e9, 7.2e9])
        self.assertLess(dear["V"], cheap["V"])

    def test_a_high_growth_name_gets_the_longer_explicit_period(self):
        fast = dcf.from_fundamentals(
            price=100.0, shares=1e9, beta=1.0,
            fcf_series=[1.0e9, 1.4e9, 1.96e9, 2.74e9, 3.84e9])
        self.assertEqual(fast["model"]["years"], dcf.LONG_EXPLICIT_YEARS)

    def test_the_staleness_gate_does_not_fire_on_the_derived_path(self):
        """§13's rule guards a *stored* fair value scored against a price it was
        not struck at. Derived, the two are computed at the same close, so the
        upside is internally consistent whatever the date.

        Applying the gate here would be incoherent rather than merely strict: an
        old reconstruction would drop the DCF for stale prices while ``V_REL``
        went on ranking multiples built from those same stale prices — one
        branch removed and the other kept, on identical evidence.
        """
        out = dcf.from_fundamentals(
            price=100.0, shares=1e9, beta=1.0,
            fcf_series=[5.0e9, 5.5e9, 6.0e9, 6.6e9, 7.2e9],
            price_date="2021-01-04", as_of="2026-09-12")
        self.assertFalse(out["refused"], out.get("reason"))
        self.assertIsNone(out["valuation_date"])
        # The date is still reported, so the page can say how old the data is.
        self.assertEqual(out["price_date"], "2021-01-04")

    def test_the_staleness_gate_still_fires_on_a_stored_valuation(self):
        out = dcf.score(price=100.0, base=140.0,
                        valuation_date="2021-01-04", as_of="2026-09-12",
                        source="override")
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "valuation_stale")


class DerivedAndOverriddenAgree(unittest.TestCase):
    """Both paths must reach V_DCF through one mapping function."""

    def test_feeding_the_derived_fair_values_back_in_reproduces_the_score(self):
        derived = dcf.from_fundamentals(
            price=100.0, shares=1e9, beta=1.0,
            fcf_series=[5.0e9, 5.5e9, 6.0e9, 6.6e9, 7.2e9])
        by_name = {s["name"]: s["fair_value"] for s in derived["scenarios"]}
        replayed = dcf.score(price=100.0, bear=by_name["bear"],
                             base=by_name["base"], bull=by_name["bull"],
                             confidence=derived["confidence"])
        self.assertAlmostEqual(replayed["V"], derived["V"], places=2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
