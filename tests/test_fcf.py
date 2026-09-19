"""Tests for :mod:`ystocker.fcf`.

The trailing multiples are arithmetic and mostly test themselves. The forward
estimate is the part worth proving, because it is a *derived* number that lands
in the same column as measured ones — so most of what follows is refusals, and
the identity that makes the estimate interpretable at all.

Pure: no app, no network, no cache.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import fcf  # noqa: E402


class TrailingTests(unittest.TestCase):
    def test_pfcf_and_yield_are_reciprocal(self):
        out = fcf.trailing(fcf_ttm=5.0, market_cap=100.0)
        self.assertAlmostEqual(out["pfcf"], 20.0, places=2)
        self.assertAlmostEqual(out["fcf_yield"], 5.0, places=2)
        self.assertAlmostEqual(out["pfcf"] * out["fcf_yield"], 100.0, places=1)

    def test_unit_cancels(self):
        """Both inputs are billions in the caller's records; the ratio must not
        care, or the column would be wrong by 1e9 for anyone passing raw."""
        small = fcf.trailing(fcf_ttm=5.0, market_cap=100.0)
        large = fcf.trailing(fcf_ttm=5e9, market_cap=100e9)
        self.assertAlmostEqual(small["pfcf"], large["pfcf"], places=2)

    def test_negative_fcf_yields_a_yield_but_never_a_multiple(self):
        """A cash-burning company is a fact worth showing, and a negative yield
        shows it. "Price over negative cash flow" is not a multiple — sorted into
        a column headed cheapest it would appear at the top."""
        out = fcf.trailing(fcf_ttm=-4.0, market_cap=100.0)
        self.assertIsNone(out["pfcf"])
        self.assertAlmostEqual(out["fcf_yield"], -4.0, places=2)
        self.assertEqual(out["reason"], "negative_fcf")

    def test_missing_inputs_refuse(self):
        self.assertEqual(fcf.trailing(fcf_ttm=5.0, market_cap=None)["reason"],
                         "no_market_cap")
        self.assertEqual(fcf.trailing(fcf_ttm=None, market_cap=100.0)["reason"],
                         "no_fcf")
        self.assertEqual(fcf.trailing(fcf_ttm=5.0, market_cap=0.0)["reason"],
                         "no_market_cap")


class GrowthFactorTests(unittest.TestCase):
    def test_consensus_is_next_year_over_this_year(self):
        g = fcf.growth_factor(eps_fy0=4.0, eps_fy1=5.0)
        self.assertAlmostEqual(g["factor"], 1.25, places=4)
        self.assertEqual(g["source"], "consensus")

    def test_pe_ratio_is_the_implied_growth(self):
        """Both P/Es share the same price, so their ratio is algebraically the
        expected EPS growth. 30 trailing against 20 forward is 1.5x."""
        g = fcf.growth_factor(pe_ttm=30.0, pe_fwd=20.0)
        self.assertAlmostEqual(g["factor"], 1.5, places=4)
        self.assertEqual(g["source"], "pe_ratio")

    def test_consensus_wins_when_both_are_available(self):
        """The P/E path mixes a trailing denominator with a forward one, and how
        much period that spans depends on where the company sits in its fiscal
        year. Consensus compares two figures struck on the same basis."""
        g = fcf.growth_factor(eps_fy0=4.0, eps_fy1=5.0, pe_ttm=30.0, pe_fwd=10.0)
        self.assertEqual(g["source"], "consensus")
        self.assertAlmostEqual(g["factor"], 1.25, places=4)

    def test_a_loss_year_refuses_rather_than_falling_back(self):
        """A company crossing into profit is the most interesting case and the
        one both paths get wrong in the same direction — the P/E fallback would
        be negative for exactly the same reason. Falling through would turn a
        refusal into a negative forward cash flow."""
        g = fcf.growth_factor(eps_fy0=-1.0, eps_fy1=2.0, pe_ttm=-30.0, pe_fwd=20.0)
        self.assertIsNone(g["factor"])
        self.assertEqual(g["reason"], "negative_earnings")

    def test_a_loss_year_refuses_even_when_the_pe_path_would_succeed(self):
        """The case that actually distinguishes the two branches.

        The test above used a negative `pe_ttm` as well, so both paths refused
        for the same reason and removing the guard still passed — caught by
        mutation. Here the P/Es are positive and would happily yield 1.5x, so
        only a real early return keeps this a refusal. Realistic too: Yahoo's
        trailing and forward P/E are struck on different periods from the
        consensus fiscal years, so a company can show a loss year in one and a
        positive multiple in the other.
        """
        g = fcf.growth_factor(eps_fy0=-1.0, eps_fy1=2.0, pe_ttm=30.0, pe_fwd=20.0)
        self.assertIsNone(g["factor"], "fell through to the P/E path on a loss year")
        self.assertEqual(g["reason"], "negative_earnings")

    def test_negative_pe_refuses(self):
        self.assertEqual(fcf.growth_factor(pe_ttm=-30.0, pe_fwd=20.0)["reason"],
                         "negative_earnings")
        self.assertEqual(fcf.growth_factor(pe_ttm=30.0, pe_fwd=-20.0)["reason"],
                         "negative_earnings")

    def test_no_signal_at_all_refuses(self):
        g = fcf.growth_factor()
        self.assertIsNone(g["factor"])
        self.assertEqual(g["reason"], "no_growth")

    def test_never_substitutes_one(self):
        """1.0 is the neutral value and substituting it feels harmless. It is
        not: it republishes the trailing multiple as a forward one, which reads
        as "the market expects no growth" rather than "we do not know"."""
        for g in (fcf.growth_factor(),
                  fcf.growth_factor(pe_ttm=-1.0, pe_fwd=1.0),
                  fcf.growth_factor(eps_fy0=0.0, eps_fy1=5.0)):
            self.assertIsNone(g["factor"])

    def test_out_of_band_growth_is_refused_not_clamped(self):
        """MRK showed a live consensus factor of 3.48 because its current fiscal
        year carries a charge, not because cash flow is about to treble.
        Clamping to 2.5 would keep a wrong number and hide that it was wrong."""
        g = fcf.growth_factor(eps_fy0=1.0, eps_fy1=3.48)
        self.assertIsNone(g["factor"])
        self.assertEqual(g["reason"], "growth_out_of_band")
        self.assertAlmostEqual(g["rejected"], 3.48, places=2)

    def test_the_band_admits_ordinary_growth(self):
        """Measured on the live universe the median factor is 1.12 and only four
        of 212 fall outside. A band that rejected normal growth would make the
        column mostly empty."""
        for factor in (0.5, 0.9, 1.12, 1.5, 2.4):
            g = fcf.growth_factor(eps_fy0=1.0, eps_fy1=factor)
            self.assertIsNotNone(g["factor"], f"{factor} should be admitted")

    def test_band_edges_are_inclusive(self):
        self.assertIsNotNone(fcf.growth_factor(eps_fy0=1.0, eps_fy1=fcf.GROWTH_MIN)["factor"])
        self.assertIsNotNone(fcf.growth_factor(eps_fy0=1.0, eps_fy1=fcf.GROWTH_MAX)["factor"])


class EstimateTests(unittest.TestCase):
    BASE = {"fcf_ttm": 5.0, "market_cap": 100.0}

    def test_forward_fcf_is_trailing_scaled_by_growth(self):
        out = fcf.estimate(**self.BASE, eps_fy0=4.0, eps_fy1=5.0)
        self.assertAlmostEqual(out["forward_fcf"], 6.25, places=3)
        self.assertAlmostEqual(out["forward_pfcf"], 16.0, places=2)
        self.assertEqual(out["forward_source"], "consensus")

    def test_the_documented_identity_holds(self):
        """forward P/FCF == trailing P/FCF ÷ growth. The whole estimate is this
        one relation, so it is asserted rather than described."""
        out = fcf.estimate(**self.BASE, pe_ttm=30.0, pe_fwd=20.0)
        # Compared at the precision actually published: `forward_pfcf` is stored
        # to 2dp, so asserting the unrounded identity would only ever be testing
        # the rounding.
        self.assertEqual(out["forward_pfcf"], round(out["pfcf"] / out["growth"], 2))

    def test_growth_above_one_makes_the_forward_multiple_cheaper(self):
        """Sanity of direction. Getting this backwards would rank every growing
        company as expensive, and the column would still look plausible."""
        out = fcf.estimate(**self.BASE, eps_fy0=4.0, eps_fy1=5.0)
        self.assertLess(out["forward_pfcf"], out["pfcf"])

    def test_shrinking_earnings_make_it_dearer(self):
        out = fcf.estimate(**self.BASE, eps_fy0=5.0, eps_fy1=4.0)
        self.assertGreater(out["forward_pfcf"], out["pfcf"])

    def test_trailing_survives_a_failed_forward(self):
        """A measured fact must not be discarded to protect a failed guess."""
        out = fcf.estimate(**self.BASE)          # no growth signal at all
        self.assertAlmostEqual(out["pfcf"], 20.0, places=2)
        self.assertIsNone(out["forward_pfcf"])
        self.assertEqual(out["reason"], "no_growth")

    def test_negative_fcf_produces_no_forward_figure(self):
        """Scaling a negative cash flow by a growth factor is not a forecast —
        it asserts the burn continues and grows proportionally."""
        out = fcf.estimate(fcf_ttm=-4.0, market_cap=100.0, eps_fy0=4.0, eps_fy1=5.0)
        self.assertIsNone(out["forward_fcf"])
        self.assertIsNone(out["forward_pfcf"])
        self.assertEqual(out["reason"], "negative_fcf")
        self.assertIsNotNone(out["fcf_yield"])   # the fact is still reported

    def test_out_of_band_reports_what_it_rejected(self):
        out = fcf.estimate(**self.BASE, eps_fy0=1.0, eps_fy1=3.48)
        self.assertIsNone(out["forward_pfcf"])
        self.assertEqual(out["reason"], "growth_out_of_band")
        self.assertAlmostEqual(out["rejected_growth"], 3.48, places=2)

    def test_reason_and_forward_are_mutually_exclusive(self):
        """Either there is a forward figure or there is a reason there is not.
        Both at once would let the page render an estimate under a caveat that
        says it could not be produced."""
        cases = [
            dict(**self.BASE, eps_fy0=4.0, eps_fy1=5.0),
            dict(**self.BASE),
            dict(fcf_ttm=-4.0, market_cap=100.0, eps_fy0=4.0, eps_fy1=5.0),
            dict(fcf_ttm=5.0, market_cap=None, eps_fy0=4.0, eps_fy1=5.0),
            dict(**self.BASE, eps_fy0=1.0, eps_fy1=9.0),
        ]
        for kwargs in cases:
            out = fcf.estimate(**kwargs)
            with self.subTest(kwargs=kwargs):
                self.assertNotEqual(out["forward_pfcf"] is None,
                                    out.get("reason") is None,
                                    "exactly one of forward_pfcf / reason must be set")

    def test_every_reason_is_declared(self):
        """A reason string the page has no translation for renders as a raw
        identifier under a number somebody is about to act on."""
        cases = [
            dict(fcf_ttm=None, market_cap=100.0),
            dict(fcf_ttm=-1.0, market_cap=100.0),
            dict(fcf_ttm=5.0, market_cap=None),
            dict(fcf_ttm=5.0, market_cap=100.0),
            dict(fcf_ttm=5.0, market_cap=100.0, pe_ttm=-3.0, pe_fwd=2.0),
            dict(fcf_ttm=5.0, market_cap=100.0, eps_fy0=1.0, eps_fy1=9.0),
        ]
        seen = {fcf.estimate(**c).get("reason") for c in cases}
        seen.discard(None)
        self.assertTrue(seen <= set(fcf.REFUSALS), f"undeclared: {seen - set(fcf.REFUSALS)}")
        # And the table is not carrying entries nothing can produce.
        self.assertEqual(seen, set(fcf.REFUSALS))


if __name__ == "__main__":
    unittest.main()


class TranslationParityTests(unittest.TestCase):
    """Every refusal and growth source the module can emit must have copy.

    These reach the page as `fcf.why_<reason>` and `fcf.src_<source>` lookups
    with the raw identifier as the fallback, so a missing key does not fail —
    it renders `growth_out_of_band` in a tooltip under a number somebody is
    about to act on, in both languages.
    """

    def _i18n(self) -> str:
        return (Path(__file__).resolve().parent.parent
                / "ystocker" / "static" / "i18n.js").read_text()

    def test_every_refusal_has_a_string(self):
        js = self._i18n()
        missing = [r for r in fcf.REFUSALS if f"'fcf.why_{r}'" not in js]
        self.assertEqual(missing, [], f"no i18n key for: {missing}")

    def test_every_growth_source_has_a_string(self):
        js = self._i18n()
        sources = {
            fcf.growth_factor(eps_fy0=4.0, eps_fy1=5.0)["source"],
            fcf.growth_factor(pe_ttm=30.0, pe_fwd=20.0)["source"],
        }
        missing = [s for s in sources if f"'fcf.src_{s}'" not in js]
        self.assertEqual(missing, [], f"no i18n key for: {missing}")

    def test_both_languages_are_present(self):
        """A key with only `en:` silently serves English on the Chinese page."""
        import re
        js = self._i18n()
        for key in [f"fcf.why_{r}" for r in fcf.REFUSALS] + ["th.pfcf", "th.fwd_pfcf"]:
            m = re.search(r"'" + re.escape(key) + r"':\s*\{(.*?)\}", js, re.S)
            self.assertIsNotNone(m, f"{key} not found")
            body = m.group(1)
            self.assertIn("en:", body, f"{key} has no en")
            self.assertIn("zh:", body, f"{key} has no zh")
