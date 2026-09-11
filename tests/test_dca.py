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

    def _assert_key(self, key: str):
        pattern = re.compile(r"['\"]" + re.escape(key) + r"['\"]\s*:\s*\{([^}]*)\}")
        match = pattern.search(self.js)
        self.assertIsNotNone(match, f"i18n.js has no entry for {key!r}")
        body = match.group(1)
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

    def test_every_model_reason_has_a_label(self):
        for reason in ("ticker", "industry", "sector", "default", "explicit"):
            self._assert_key(f"dca.why_{reason}")

    def test_the_python_factor_labels_cover_every_direction_entry(self):
        """The server-side fallback, used when a translation is missing."""
        self.assertEqual(set(dca.FACTOR_LABELS), set(dca.DIRECTION))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
