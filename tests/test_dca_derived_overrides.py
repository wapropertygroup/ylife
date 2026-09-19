"""Tests for :func:`ystocker.dca.derive_overrides`.

Overriding a multiple by hand has to carry to whatever the reconstruction
derived from it. A hand-set P/E of 20 beside a PEG still computed from the
measured P/E of 30 is two numbers about one company that cannot both be true,
and the page renders them identically.

The interesting cases are the refusals. A propagated value that is merely
plausible is worse than an absent one, because it arrives in the same column, in
the same font, as a measured multiple.

Pure: no app, no network, no cache.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import dca  # noqa: E402


class RatioDerivationTests(unittest.TestCase):
    """PEG and EV/Sales-over-growth: the divisor is measured, not assumed."""

    # Measured: P/E 30 with PEG 1.5 implies realised growth of 20%.
    NOW = {"pe": 30.0, "peg": 1.5, "ev_sales": 8.0, "ev_sales_growth": 0.4}

    def test_peg_follows_pe(self):
        out = dca.derive_overrides({"pe": 20.0}, self.NOW)
        self.assertAlmostEqual(out["peg"], 1.0, places=4)

    def test_the_implied_growth_rate_is_held_fixed(self):
        """The whole point: the override restates the multiple, not the growth.
        30/1.5 = 20% growth; 20/1.0 must still be 20%."""
        out = dca.derive_overrides({"pe": 20.0}, self.NOW)
        implied_before = self.NOW["pe"] / self.NOW["peg"]
        implied_after = 20.0 / out["peg"]
        self.assertAlmostEqual(implied_before, implied_after, places=6)

    def test_ev_sales_growth_follows_ev_sales(self):
        out = dca.derive_overrides({"ev_sales": 4.0}, self.NOW)
        self.assertAlmostEqual(out["ev_sales_growth"], 0.2, places=4)

    def test_the_divisor_comes_from_the_data_not_a_constant(self):
        """Two companies with different measured growth must derive differently.

        Written after a mutation slipped through: the fixture above implies
        exactly 20% growth (30/1.5), so replacing the recovered divisor with a
        hardcoded 20 produced identical output and the suite stayed green. These
        imply 30% and 8%, so no single constant can satisfy both.
        """
        fast = dca.derive_overrides({"pe": 18.0}, {"pe": 36.0, "peg": 1.2})
        slow = dca.derive_overrides({"pe": 18.0}, {"pe": 24.0, "peg": 3.0})
        self.assertAlmostEqual(fast["peg"], 0.6, places=4)   # 18 / 30%
        self.assertAlmostEqual(slow["peg"], 2.25, places=4)  # 18 / 8%
        self.assertNotAlmostEqual(fast["peg"], slow["peg"], places=4)

    def test_refuses_when_the_measured_pair_is_incomplete(self):
        """Without both points the divisor is unrecoverable. Assuming one would
        put a fabricated growth rate behind a rendered multiple."""
        self.assertNotIn("peg", dca.derive_overrides({"pe": 20.0}, {"pe": 30.0}))
        self.assertNotIn("peg", dca.derive_overrides({"pe": 20.0}, {"peg": 1.5}))
        self.assertNotIn("peg", dca.derive_overrides({"pe": 20.0}, {}))

    def test_refuses_on_a_non_positive_measured_pair(self):
        for now in ({"pe": 0.0, "peg": 1.5}, {"pe": 30.0, "peg": 0.0},
                    {"pe": -30.0, "peg": 1.5}):
            with self.subTest(now=now):
                self.assertNotIn("peg", dca.derive_overrides({"pe": 20.0}, now))


class InverseDerivationTests(unittest.TestCase):
    """FCF yield is 100/(P/FCF) by construction — an exact identity."""

    def test_fcf_yield_follows_pfcf(self):
        out = dca.derive_overrides({"pfcf": 25.0}, {})
        self.assertAlmostEqual(out["fcf_yield"], 4.0, places=4)

    def test_pfcf_follows_fcf_yield(self):
        out = dca.derive_overrides({"fcf_yield": 5.0}, {})
        self.assertAlmostEqual(out["pfcf"], 20.0, places=4)

    def test_needs_no_measured_values(self):
        """Unlike the ratio kind: there is no hidden term to recover."""
        self.assertIn("fcf_yield", dca.derive_overrides({"pfcf": 25.0}, {}))

    def test_the_pair_does_not_bounce(self):
        """pfcf -> fcf_yield -> pfcf would be a cycle. Deriving only from the
        explicit set means one pass and no feedback."""
        out = dca.derive_overrides({"pfcf": 25.0}, {})
        self.assertEqual(sorted(out), ["fcf_yield"])

    def test_round_trip_is_stable(self):
        out = dca.derive_overrides({"pfcf": 25.0}, {})
        back = dca.derive_overrides({"fcf_yield": out["fcf_yield"]}, {})
        self.assertAlmostEqual(back["pfcf"], 25.0, places=4)


class SameValueTests(unittest.TestCase):
    """mid_cycle and cycle_adjusted are one variable assigned to two keys."""

    def test_each_follows_the_other(self):
        self.assertEqual(dca.derive_overrides({"cycle_adjusted": 18.0}, {})["mid_cycle"], 18.0)
        self.assertEqual(dca.derive_overrides({"mid_cycle": 18.0}, {})["cycle_adjusted"], 18.0)

    def test_they_do_not_bounce_either(self):
        out = dca.derive_overrides({"mid_cycle": 18.0}, {})
        self.assertEqual(sorted(out), ["cycle_adjusted"])


class PrecedenceTests(unittest.TestCase):
    NOW = {"pe": 30.0, "peg": 1.5}

    def test_an_explicit_override_is_never_replaced(self):
        """A value the reader typed is their claim and outranks one inferred
        from another of their claims."""
        out = dca.derive_overrides({"pe": 20.0, "peg": 99.0}, self.NOW)
        self.assertNotIn("peg", out)

    def test_untouched_factors_are_not_derived(self):
        out = dca.derive_overrides({"pe": 20.0}, self.NOW)
        self.assertNotIn("ev_sales_growth", out)
        self.assertNotIn("fcf_yield", out)

    def test_nothing_overridden_derives_nothing(self):
        self.assertEqual(dca.derive_overrides({}, self.NOW), {})

    def test_a_factor_with_no_dependent_derives_nothing(self):
        """Most factors have none. ptbv, pffo, normalized_margin and ev_ebitda
        stand alone — propagating from them would mean inventing a relationship
        the reconstruction does not contain."""
        for factor in ("ptbv", "pffo", "normalized_margin", "ev_ebitda", "peer"):
            with self.subTest(factor=factor):
                self.assertEqual(dca.derive_overrides({factor: 5.0}, {}), {})

    def test_a_non_positive_override_derives_nothing(self):
        self.assertEqual(dca.derive_overrides({"pe": 0.0}, self.NOW), {})
        self.assertEqual(dca.derive_overrides({"pfcf": -3.0}, {}), {})

    def test_several_sources_at_once(self):
        out = dca.derive_overrides(
            {"pe": 20.0, "pfcf": 25.0, "cycle_adjusted": 18.0},
            {"pe": 30.0, "peg": 1.5})
        self.assertAlmostEqual(out["peg"], 1.0, places=4)
        self.assertAlmostEqual(out["fcf_yield"], 4.0, places=4)
        self.assertEqual(out["mid_cycle"], 18.0)


class TableIntegrityTests(unittest.TestCase):
    def test_every_participant_is_a_known_factor(self):
        """A typo here would silently never fire."""
        for dependent, (source, kind) in dca.DERIVED_FACTORS.items():
            with self.subTest(dependent=dependent):
                self.assertIn(dependent, dca.DIRECTION)
                self.assertIn(source, dca.DIRECTION)
                self.assertIn(kind, ("ratio", "inverse", "same"))

    def test_every_participant_is_actually_reconstructed(self):
        """Deriving a value for a factor the engine never produces would create
        a percentile for something that has no history to rank against."""
        from ystocker.dca_history import RECONSTRUCTED

        for dependent, (source, _kind) in dca.DERIVED_FACTORS.items():
            with self.subTest(dependent=dependent):
                self.assertIn(dependent, RECONSTRUCTED)
                self.assertIn(source, RECONSTRUCTED)

    def test_no_factor_derives_from_itself(self):
        for dependent, (source, _kind) in dca.DERIVED_FACTORS.items():
            self.assertNotEqual(dependent, source)

    def test_the_symmetric_kinds_are_declared_in_both_directions(self):
        """`inverse` and `same` are mutual relationships. Declaring only one way
        would make the propagation depend on which box the reader typed in."""
        for dependent, (source, kind) in dca.DERIVED_FACTORS.items():
            if kind in ("inverse", "same"):
                with self.subTest(pair=(dependent, source)):
                    self.assertEqual(dca.DERIVED_FACTORS.get(source), (dependent, kind))

    def test_ratio_kinds_are_one_way(self):
        """PEG follows P/E; P/E must not follow PEG. Back-solving a multiple
        from its own derivative asserts a growth rate as fact."""
        for dependent, (source, kind) in dca.DERIVED_FACTORS.items():
            if kind == "ratio":
                with self.subTest(pair=(dependent, source)):
                    self.assertNotIn(source, dca.DERIVED_FACTORS)


if __name__ == "__main__":
    unittest.main()
