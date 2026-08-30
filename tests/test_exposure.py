"""
Unit tests for ystocker.exposure — the three-valued limit check.

No app, no network, no cache: the fund resolver is injected, exactly as in
``tests/test_lookthrough.py``, because ``exposure`` inherits that contract.

The two tests that matter most are
:meth:`TestInvariant.test_three_way_partition_is_closed` — if attributed +
unknown + non-equity ever stops summing to the portfolio total then every band on
the page is wrong at once, and wrong *quietly* — and
:meth:`TestNaiveCheckWouldBeWrong.test_naive_comparison_passes_where_this_does_not`,
which pins the actual bug this module was written to prevent: comparing a measured
floor against a ceiling limit with a plain ``>`` reports a pass for a portfolio
whose disclosures cannot support one.
"""

from __future__ import annotations

import unittest

from ystocker.exposure import (
    VERDICT_BREACH,
    VERDICT_INDETERMINATE,
    VERDICT_PASS,
    Assessment,
    PortfolioPolicy,
    Trade,
    apply_trade,
    assess,
    band_for,
    render_block,
    unknown_for,
    unknown_value,
    verdict_for,
)
from ystocker.lookthrough import Position, analyse

# ---------------------------------------------------------------------------
# The same fake universe shape as test_lookthrough: VOO's top ten really do sum
# to well under its equity sleeve, BND really does report zero holdings against a
# bond sleeve, and a fund reporting no asset classes at all really exists.
# ---------------------------------------------------------------------------

def _fund(symbol, name, holdings, stock=1.0, bond=0.0, cash=0.0):
    return {"symbol": symbol, "name": name, "kind": "fund",
            "holdings": [{"symbol": s, "name": n, "weight": w} for s, n, w in holdings],
            "asset_classes": {"stock": stock, "bond": bond, "cash": cash,
                              "preferred": 0.0, "convertible": 0.0, "other": 0.0}}


def _equity(symbol, name=""):
    return {"symbol": symbol, "name": name or symbol, "kind": "equity",
            "holdings": [], "asset_classes": {}}


def _no_asset_classes(symbol, name, holdings):
    """A fund disclosing holdings but no asset-class block -> ``unclassified``."""
    return {"symbol": symbol, "name": name, "kind": "fund",
            "holdings": [{"symbol": s, "name": n, "weight": w} for s, n, w in holdings],
            "asset_classes": {}}


UNIVERSE = {
    # visible 0.40 against a 1.0 equity sleeve -> 0.60 undisclosed equity
    "VOO": _fund("VOO", "Vanguard S&P 500", [
        ("NVDA", "NVIDIA Corp", 0.08),
        ("AAPL", "Apple Inc", 0.07),
        ("MSFT", "Microsoft Corp", 0.07),
        ("AMZN", "Amazon.com Inc", 0.18),
    ]),
    # visible 0.50, equity sleeve 1.0 -> 0.50 undisclosed
    "QQQ": _fund("QQQ", "Invesco QQQ", [
        ("NVDA", "NVIDIA Corp", 0.09),
        ("AAPL", "Apple Inc", 0.09),
        ("MSFT", "Microsoft Corp", 0.32),
    ]),
    # nothing disclosed, measured as no equity at all
    "BND": _fund("BND", "Vanguard Total Bond", [], stock=0.0, bond=0.98, cash=0.02),
    "MYSTERY": _no_asset_classes("MYSTERY", "Opaque Fund", [
        ("AAPL", "Apple Inc", 0.25),
    ]),
    "NVDA": _equity("NVDA", "NVIDIA Corp"),
    "AAPL": _equity("AAPL", "Apple Inc"),
    "MSFT": _equity("MSFT", "Microsoft Corp"),
    "AMZN": _equity("AMZN", "Amazon.com Inc"),
    "GOOG": _equity("GOOG", "Alphabet Inc Class C"),
    "GOOGL": _equity("GOOGL", "Alphabet Inc Class A"),
}


def resolver(symbol):
    """Stand-in for funddata.peek: None means genuinely unresolvable."""
    return UNIVERSE.get(symbol.upper())


def pending_resolver(pending):
    """A cold-cache resolver: *pending* symbols report kind="pending"."""
    def _r(symbol):
        if symbol.upper() in pending:
            return {"symbol": symbol, "name": symbol, "kind": "pending",
                    "holdings": [], "asset_classes": {}}
        return UNIVERSE.get(symbol.upper())
    return _r


def missing_resolver(missing):
    """A resolver that 404s for *missing* symbols -> ``unresolved``."""
    def _r(symbol):
        if symbol.upper() in missing:
            return None
        return UNIVERSE.get(symbol.upper())
    return _r


# Portfolio shapes the invariants are asserted across.
PORTFOLIOS = {
    "index fund only": [Position("VOO", 10_000.0)],
    "two overlapping funds": [Position("VOO", 10_000.0), Position("QQQ", 5_000.0)],
    "bond fund only": [Position("BND", 5_000.0)],
    "direct equities only": [Position("AAPL", 1_000.0), Position("MSFT", 1_000.0)],
    "mixed": [Position("VOO", 10_000.0), Position("BND", 5_000.0),
              Position("AAPL", 2_000.0)],
    "unclassified fund": [Position("MYSTERY", 3_000.0)],
    "share classes": [Position("GOOG", 1_000.0), Position("GOOGL", 1_500.0)],
    "empty": [],
}

POLICY_8 = PortfolioPolicy(max_single_name_pct=8.0, cash=100_000.0)


class TestInvariant(unittest.TestCase):
    """The three-way partition must close, or every band is quietly wrong."""

    def test_three_way_partition_is_closed(self):
        for label, positions in PORTFOLIOS.items():
            with self.subTest(label):
                a = assess(positions, POLICY_8, resolver)
                self.assertAlmostEqual(
                    a.attributed_value + a.unknown_value
                    + a.known_non_equity_value,
                    a.total_after, places=6,
                    msg=(f"{label}: {a.attributed_value} + {a.unknown_value} + "
                         f"{a.known_non_equity_value} != {a.total_after}"))

    def test_partition_closes_under_every_position_ordering(self):
        # Floating-point accumulation order must not move the total.
        base = PORTFOLIOS["mixed"]
        for rotation in range(len(base)):
            positions = base[rotation:] + base[:rotation]
            with self.subTest(rotation=rotation):
                a = assess(positions, POLICY_8, resolver)
                self.assertAlmostEqual(
                    a.attributed_value + a.unknown_value
                    + a.known_non_equity_value, a.total_after, places=6)

    def test_unknown_is_never_negative(self):
        for label, positions in PORTFOLIOS.items():
            with self.subTest(label):
                self.assertGreaterEqual(unknown_value(analyse(positions, resolver)),
                                        0.0)

    def test_ceiling_never_below_floor(self):
        for label, positions in PORTFOLIOS.items():
            with self.subTest(label):
                a = assess(positions, POLICY_8, resolver)
                for c in a.checks:
                    self.assertGreaterEqual(c.after.ceiling_value,
                                            c.after.floor_value)
                    self.assertGreaterEqual(c.after.ceiling_pct, c.after.floor_pct)

    def test_floor_never_exceeds_portfolio(self):
        for label, positions in PORTFOLIOS.items():
            with self.subTest(label):
                a = assess(positions, POLICY_8, resolver)
                for c in a.checks:
                    self.assertLessEqual(c.after.floor_pct, 100.0 + 1e-9)


class TestUnknownValue(unittest.TestCase):
    """``unknown`` is every residual bucket except the one measured as non-equity."""

    def test_bond_fund_contributes_no_unknown(self):
        # The whole point: BND was *measured* to hold no equities, so none of it
        # is "a company we cannot see". Folding non_equity in would report a bond
        # sleeve as possible hidden concentration.
        r = analyse([Position("BND", 5_000.0)], resolver)
        self.assertEqual(unknown_value(r), 0.0)
        self.assertAlmostEqual(r.residual.non_equity, 5_000.0, places=6)

    def test_index_fund_undisclosed_equity_is_unknown(self):
        r = analyse([Position("VOO", 10_000.0)], resolver)
        # visible 0.40 of a 1.0 equity sleeve -> $6,000 undisclosed
        self.assertAlmostEqual(r.residual.undisclosed_equity, 6_000.0, places=6)
        self.assertAlmostEqual(unknown_value(r), 6_000.0, places=6)

    def test_unclassified_counts_as_unknown(self):
        # A fund reporting no asset classes has told us nothing, so its residual
        # could be anything -- including more of the name under test.
        r = analyse([Position("MYSTERY", 3_000.0)], resolver)
        self.assertAlmostEqual(r.residual.unclassified, 2_250.0, places=6)
        self.assertAlmostEqual(unknown_value(r), 2_250.0, places=6)

    def test_pending_counts_as_unknown(self):
        # A cold cache must widen the band, not collapse it. This is what stops a
        # freshly warmed portfolio from reporting a confident PASS.
        r = analyse([Position("VOO", 10_000.0)], pending_resolver({"NVDA"}))
        self.assertGreater(r.residual.pending, 0.0)
        self.assertAlmostEqual(unknown_value(r),
                               r.residual.undisclosed_equity + r.residual.pending,
                               places=6)

    def test_unresolved_counts_as_unknown(self):
        # A named leaf that 404s: we do not know what it is, so the conservative
        # direction is the honest one.
        r = analyse([Position("VOO", 10_000.0)], missing_resolver({"AMZN"}))
        self.assertGreater(r.residual.unresolved, 0.0)
        self.assertIn("unresolved", str(r.residual.__dict__.keys()))
        self.assertAlmostEqual(
            unknown_value(r), r.residual.total() - r.residual.non_equity, places=6)

    def test_direct_equities_have_no_unknown(self):
        # A portfolio of nothing but companies has a collapsed band, which is the
        # only way a PASS is reachable.
        r = analyse([Position("AAPL", 1_000.0), Position("MSFT", 1_000.0)], resolver)
        self.assertEqual(unknown_value(r), 0.0)


class TestVerdict(unittest.TestCase):
    """The three-valued check itself, in isolation from look-through."""

    def test_floor_over_limit_is_breach(self):
        self.assertEqual(verdict_for(900.0, 0.0, 10_000.0, 8.0), VERDICT_BREACH)

    def test_floor_over_limit_is_breach_even_with_a_wide_band(self):
        # BREACH must win: the measured floor alone already exceeds the limit, so
        # the uncertainty is irrelevant.
        self.assertEqual(verdict_for(900.0, 5_000.0, 10_000.0, 8.0), VERDICT_BREACH)

    def test_floor_under_limit_but_band_crosses_is_indeterminate(self):
        self.assertEqual(verdict_for(500.0, 5_000.0, 10_000.0, 8.0),
                         VERDICT_INDETERMINATE)

    def test_ceiling_under_limit_is_the_only_pass(self):
        self.assertEqual(verdict_for(500.0, 100.0, 10_000.0, 8.0), VERDICT_PASS)

    def test_collapsed_band_under_limit_passes(self):
        self.assertEqual(verdict_for(500.0, 0.0, 10_000.0, 8.0), VERDICT_PASS)

    def test_floor_exactly_at_limit_is_not_a_breach(self):
        # Equality is not "over". It is still INDETERMINATE if anything is hidden.
        self.assertEqual(verdict_for(800.0, 0.0, 10_000.0, 8.0), VERDICT_PASS)
        self.assertEqual(verdict_for(800.0, 1.0, 10_000.0, 8.0),
                         VERDICT_INDETERMINATE)

    def test_empty_portfolio_passes(self):
        # Nothing held breaches no limit. This is a known answer, not an
        # indeterminate one.
        self.assertEqual(verdict_for(0.0, 0.0, 0.0, 8.0), VERDICT_PASS)

    def test_zero_limit_means_any_exposure_breaches(self):
        self.assertEqual(verdict_for(1.0, 0.0, 10_000.0, 0.0), VERDICT_BREACH)
        self.assertEqual(verdict_for(0.0, 0.0, 10_000.0, 0.0), VERDICT_PASS)

    def test_negative_unknown_is_treated_as_zero(self):
        self.assertEqual(verdict_for(500.0, -100.0, 10_000.0, 8.0), VERDICT_PASS)


class TestTightenedBand(unittest.TestCase):
    """The band is narrowed by deduction from top-N disclosure, never by guessing."""

    def test_tight_bound_never_exceeds_the_loose_one(self):
        # The loose portfolio-level figure must stay a ceiling on every per-name
        # width. If this ever inverts, a vendor payload has been allowed to
        # manufacture allowance the portfolio does not contain.
        for label, positions in PORTFOLIOS.items():
            r = analyse(positions, resolver)
            loose = unknown_value(r)
            for sym in ("NVDA", "AAPL", "MSFT", "AMZN", "GOOG", "ZZZZ"):
                with self.subTest(label=label, symbol=sym):
                    self.assertLessEqual(unknown_for(r, (sym,)), loose + 1e-9)

    def test_disclosed_name_draws_nothing_from_that_fund(self):
        # AAPL is disclosed at 7% of VOO, so it cannot also be in VOO's hidden
        # 60%: the tail is by definition the holdings outside the top ten.
        r = analyse([Position("VOO", 10_000.0)], resolver)
        self.assertEqual(unknown_for(r, ("AAPL",)), 0.0)
        self.assertAlmostEqual(unknown_value(r), 6_000.0, places=6)

    def test_disclosed_name_is_verified_in_a_mostly_hidden_portfolio(self):
        # The headline win: AAPL is disclosed by both funds holding it, so its
        # exposure is exact even though 35% of the portfolio is unidentified.
        positions = [Position("VOO", 40_000.0), Position("QQQ", 25_000.0),
                     Position("BND", 20_000.0), Position("AAPL", 10_000.0)]
        a = assess(positions, POLICY_8, resolver)
        aapl = _check(a, "AAPL")
        self.assertTrue(aapl.after.is_verified)
        self.assertAlmostEqual(aapl.after.floor_pct, aapl.after.ceiling_pct,
                               places=6)
        self.assertGreater(a.unknown_pct, 20.0)   # the portfolio is still murky

    def test_undisclosed_name_is_capped_by_the_smallest_disclosed_weight(self):
        # NVDA is absent from MYSTERY's disclosure, so it can weigh at most as much
        # as the smallest name that made the list (AAPL at 25%) -- not the whole
        # hidden remainder.
        r = analyse([Position("MYSTERY", 3_000.0)], resolver)
        self.assertAlmostEqual(unknown_value(r), 2_250.0, places=6)
        self.assertAlmostEqual(unknown_for(r, ("NVDA",)), 750.0, places=6)

    def test_wrapper_tail_gets_no_narrowing(self):
        # A fund whose disclosed holdings include a fund may hide the same company
        # inside several tail entries, so the "appears once" deduction fails and
        # the full hidden value must be allowed.
        universe = dict(UNIVERSE)
        universe["FOF"] = _fund("FOF", "Fund of Funds", [("VOO", "Vanguard", 0.30)])

        def res(symbol):
            return universe.get(symbol.upper())

        r = analyse([Position("FOF", 10_000.0)], res)
        pockets = {p.fund: p for p in r.pockets}
        self.assertFalse(pockets["FOF"].flat, "FOF discloses a fund, so not flat")
        self.assertTrue(pockets["VOO"].flat, "VOO discloses only companies")
        # FOF's own hidden 70% is unbounded by disclosure...
        self.assertAlmostEqual(pockets["FOF"].ceiling_for("NVDA"),
                               pockets["FOF"].hidden_value, places=6)
        # ...but VOO's hidden sleeve inside it still narrows for a disclosed name.
        self.assertEqual(pockets["VOO"].ceiling_for("AAPL"), 0.0)

    def test_unopened_buckets_stay_fully_loose(self):
        # pending / unresolved / truncated stopped at something never opened, so
        # there is no disclosure to reason from and no narrowing is legitimate.
        r = analyse([Position("VOO", 10_000.0)], pending_resolver({"AAPL"}))
        self.assertGreater(r.residual.pending, 0.0)
        # AAPL is disclosed by VOO, so it draws nothing from VOO's tail -- but the
        # pending dollars are its own and must still count.
        self.assertAlmostEqual(unknown_for(r, ("AAPL",)), r.residual.pending,
                               places=6)

    def test_pockets_reconcile_with_the_residual(self):
        # Pockets carry exactly the two bucketable residuals. A drift here means
        # the narrowing is operating on a different quantity than it reports.
        for label, positions in PORTFOLIOS.items():
            with self.subTest(label):
                r = analyse(positions, resolver)
                self.assertAlmostEqual(
                    sum(p.hidden_value for p in r.pockets),
                    r.residual.undisclosed_equity + r.residual.unclassified,
                    places=6)

    def test_narrowing_can_turn_indeterminate_into_a_real_pass(self):
        # The whole point of doing this: a verdict that was INDETERMINATE only
        # because of a loose bound becomes a legitimate PASS.
        positions = [Position("VOO", 10_000.0)]
        # AAPL sits at 7% of VOO -> 7% of the portfolio, disclosed, so exact.
        a = assess(positions, PortfolioPolicy(max_single_name_pct=8.0), resolver)
        self.assertEqual(_check(a, "AAPL").verdict, VERDICT_PASS)
        # NVDA is 8% of VOO, exactly at the limit, and also disclosed -> exact.
        self.assertEqual(_check(a, "NVDA").verdict, VERDICT_PASS)
        # AMZN is disclosed at 18% -> a genuine breach, unambiguously.
        self.assertEqual(_check(a, "AMZN").verdict, VERDICT_BREACH)


class TestNaiveCheckWouldBeWrong(unittest.TestCase):
    """The bug this module exists to prevent."""

    def test_naive_comparison_passes_where_this_does_not(self):
        # A name that no fund discloses is where the floor/limit comparison is
        # genuinely unsafe: MYSTERY hides 75% of itself and discloses only AAPL,
        # so NVDA's floor is 0 while up to 25% of the fund could be NVDA.
        positions = [Position("MYSTERY", 10_000.0)]
        a = assess(positions, PortfolioPolicy(max_single_name_pct=8.0), resolver,
                   trade=Trade("NVDA", 0.0))
        nvda = _check(a, "NVDA")

        self.assertEqual(nvda.after.floor_pct, 0.0)        # the naive test passes
        self.assertEqual(nvda.verdict, VERDICT_INDETERMINATE)  # this one does not
        self.assertAlmostEqual(nvda.after.ceiling_pct, 25.0, places=6)

    def test_no_headroom_field_is_exposed(self):
        # Headroom against a floor is the fabricated number. It must not be
        # reachable, on the object or in the serialised form.
        a = assess([Position("VOO", 10_000.0)], POLICY_8, resolver)
        check = _check(a, "NVDA")
        for obj in (a, check, check.after):
            self.assertFalse(hasattr(obj, "headroom"),
                             f"{type(obj).__name__} grew a headroom attribute")
        blob = repr(a.as_dict())
        self.assertNotIn("headroom", blob)

    def test_high_coverage_portfolio_can_actually_pass(self):
        # If everything were INDETERMINATE the verdict would carry no information.
        # A direct-equity portfolio has a collapsed band, so PASS is reachable.
        positions = [Position("AAPL", 1_000.0), Position("MSFT", 1_000.0),
                     Position("AMZN", 1_000.0), Position("GOOG", 1_000.0),
                     Position("GOOGL", 1_000.0), Position("NVDA", 1_000.0)]
        a = assess(positions, PortfolioPolicy(max_single_name_pct=20.0), resolver)
        self.assertEqual(a.coverage_after_pct, 100.0)
        self.assertEqual(a.unknown_value, 0.0)
        self.assertEqual(a.verdict, VERDICT_PASS)
        for c in a.checks:
            self.assertTrue(c.after.is_verified)

    def test_bond_portfolio_is_not_flagged_indeterminate(self):
        # Measured non-equity must not read as hidden concentration.
        a = assess([Position("BND", 5_000.0)], POLICY_8, resolver)
        self.assertEqual(a.unknown_value, 0.0)
        self.assertEqual(a.verdict, VERDICT_PASS)


class TestTrade(unittest.TestCase):
    """Applying a proposed trade, and the before/after delta."""

    def test_buy_adds_a_new_line(self):
        after, notes = apply_trade([Position("AAPL", 1_000.0)],
                                   Trade("NVDA", 500.0))
        self.assertEqual(len(after), 2)
        self.assertEqual(after[1].symbol, "NVDA")
        self.assertEqual(after[1].value, 500.0)
        self.assertEqual(notes, [])

    def test_buy_increases_an_existing_line(self):
        after, _ = apply_trade([Position("AAPL", 1_000.0)], Trade("AAPL", 500.0))
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].value, 1_500.0)

    def test_oversized_sell_is_clamped_not_shorted(self):
        # A negative line would be dropped by analyse() and shrink the total,
        # making every percentage wrong at once.
        after, notes = apply_trade([Position("AAPL", 1_000.0)],
                                   Trade("AAPL", -2_500.0))
        self.assertEqual(after[0].value, 0.0)
        self.assertTrue(any("clamped" in n for n in notes))

    def test_sell_of_unheld_symbol_is_noted_not_applied(self):
        after, notes = apply_trade([Position("AAPL", 1_000.0)],
                                   Trade("TSLA", -500.0))
        self.assertEqual(len(after), 1)
        self.assertTrue(any("nothing is held" in n for n in notes))

    def test_apply_trade_does_not_mutate_the_input(self):
        original = [Position("AAPL", 1_000.0)]
        apply_trade(original, Trade("AAPL", 500.0))
        self.assertEqual(original[0].value, 1_000.0)

    def test_symbol_case_is_normalised(self):
        after, _ = apply_trade([Position("AAPL", 1_000.0)], Trade("aapl", 500.0))
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].value, 1_500.0)

    def test_floor_delta_is_reported(self):
        # Buying NVDA directly on top of a VOO position must raise the floor by
        # exactly the traded dollars.
        a = assess([Position("VOO", 10_000.0)], POLICY_8, resolver,
                   trade=Trade("NVDA", 1_000.0))
        nvda = _check(a, "NVDA")
        self.assertAlmostEqual(a.total_after, 11_000.0, places=6)
        self.assertAlmostEqual(nvda.before.floor_value, 800.0, places=6)
        self.assertAlmostEqual(nvda.after.floor_value, 1_800.0, places=6)
        self.assertGreater(nvda.floor_delta_pct, 0.0)

    def test_traded_symbol_always_gets_a_check(self):
        # Even a symbol that resolves to nothing: the one name the caller asked
        # about must not be the one missing from the answer.
        a = assess([Position("AAPL", 1_000.0)], POLICY_8,
                   missing_resolver({"ZZZZ"}), trade=Trade("ZZZZ", 100.0))
        self.assertIsNotNone(_check(a, "ZZZZ"))

    def test_no_trade_is_a_state_review(self):
        a = assess([Position("VOO", 10_000.0)], POLICY_8, resolver)
        self.assertIsNone(a.trade)
        self.assertAlmostEqual(a.total_before, a.total_after, places=6)
        self.assertIsNone(a.liquidity)


class TestLiquidity(unittest.TestCase):

    def test_buy_within_cash_passes(self):
        a = assess([Position("AAPL", 1_000.0)],
                   PortfolioPolicy(cash=5_000.0), resolver,
                   trade=Trade("NVDA", 1_000.0))
        self.assertEqual(a.liquidity.verdict, VERDICT_PASS)

    def test_buy_beyond_cash_breaches(self):
        a = assess([Position("AAPL", 1_000.0)],
                   PortfolioPolicy(cash=500.0), resolver,
                   trade=Trade("NVDA", 1_000.0))
        self.assertEqual(a.liquidity.verdict, VERDICT_BREACH)
        self.assertEqual(a.verdict, VERDICT_BREACH)

    def test_sell_needs_no_cash(self):
        a = assess([Position("AAPL", 1_000.0)], PortfolioPolicy(cash=0.0),
                   resolver, trade=Trade("AAPL", -500.0))
        self.assertIsNone(a.liquidity)


class TestPolicy(unittest.TestCase):

    def test_absent_limit_emits_no_check(self):
        # An absent limit must not read as a satisfied one.
        a = assess([Position("VOO", 10_000.0)], PortfolioPolicy(), resolver)
        self.assertEqual(a.checks, [])
        self.assertIn("absent limit is not a satisfied limit", render_block(a))

    def test_holding_type_is_carried_through(self):
        p = PortfolioPolicy(holding_types={"AAPL": "core", "SOXQ": "tactical"})
        self.assertEqual(p.type_of("aapl"), "core")
        self.assertEqual(p.type_of("SOXQ"), "tactical")
        self.assertEqual(p.type_of("NVDA"), "")

    def test_policy_is_frozen(self):
        with self.assertRaises(Exception):
            POLICY_8.max_single_name_pct = 99.0  # type: ignore[misc]


class TestIssuerGroups(unittest.TestCase):
    """Share classes are checked only against a caller-declared grouping."""

    def test_groups_are_summed(self):
        a = assess([Position("GOOG", 1_000.0), Position("GOOGL", 1_500.0),
                    Position("AAPL", 7_500.0)],
                   PortfolioPolicy(max_single_name_pct=50.0, max_issuer_pct=20.0),
                   resolver, issuer_groups={"Alphabet": ["GOOG", "GOOGL"]})
        alpha = _check(a, "Alphabet")
        self.assertAlmostEqual(alpha.after.floor_value, 2_500.0, places=6)
        self.assertAlmostEqual(alpha.after.floor_pct, 25.0, places=6)
        self.assertEqual(alpha.verdict, VERDICT_BREACH)

    def test_band_width_does_not_double_count_across_members(self):
        # A group's allowance grows with how many members are undisclosed, but the
        # cap at hidden_value is what stops it running away: the same unidentified
        # dollar cannot be counted once for GOOG and again for GOOGL.
        a = assess([Position("VOO", 10_000.0), Position("GOOG", 500.0),
                    Position("GOOGL", 500.0)],
                   PortfolioPolicy(max_single_name_pct=8.0, max_issuer_pct=10.0),
                   resolver, issuer_groups={"Alphabet": ["GOOG", "GOOGL"]})
        alpha = _check(a, "Alphabet")
        # VOO discloses neither, min disclosed weight 0.07 -> 10k * 0.07 * 2
        self.assertAlmostEqual(alpha.after.unknown_value, 1_400.0, places=6)
        self.assertLessEqual(alpha.after.unknown_value, a.unknown_value)
        self.assertLessEqual(alpha.after.ceiling_pct, 100.0 + 1e-9)

    def test_group_allowance_is_capped_by_the_dollars_available(self):
        # Twenty undisclosed members would nominally be allowed 10k * 0.07 * 20 =
        # $14,000 of a $6,000 hidden sleeve. It must clamp to what exists.
        members = [f"ZZ{i:02d}" for i in range(20)]
        a = assess([Position("VOO", 10_000.0)],
                   PortfolioPolicy(max_issuer_pct=10.0), resolver,
                   issuer_groups={"Many": members})
        many = _check(a, "Many")
        self.assertAlmostEqual(many.after.unknown_value, 6_000.0, places=6)
        self.assertLessEqual(many.after.ceiling_pct, 100.0 + 1e-9)

    def test_single_member_group_is_ignored(self):
        a = assess([Position("AAPL", 1_000.0)],
                   PortfolioPolicy(max_issuer_pct=10.0), resolver,
                   issuer_groups={"Apple": ["AAPL"]})
        self.assertEqual([c for c in a.checks if c.kind == "issuer"], [])

    def test_no_groups_means_no_issuer_checks(self):
        a = assess([Position("AAPL", 1_000.0)],
                   PortfolioPolicy(max_issuer_pct=10.0), resolver)
        self.assertEqual([c for c in a.checks if c.kind == "issuer"], [])


class TestOrderingAndFold(unittest.TestCase):

    def test_breaches_sort_before_passes(self):
        a = assess([Position("AAPL", 5_000.0), Position("MSFT", 5_000.0),
                    Position("NVDA", 100.0)],
                   PortfolioPolicy(max_single_name_pct=10.0), resolver)
        verdicts = [c.verdict for c in a.checks]
        self.assertEqual(verdicts[0], VERDICT_BREACH)
        self.assertEqual(verdicts[-1], VERDICT_PASS)

    def test_overall_verdict_is_the_worst(self):
        a = assess([Position("AAPL", 5_000.0), Position("NVDA", 100.0)],
                   PortfolioPolicy(max_single_name_pct=10.0), resolver)
        self.assertEqual(a.verdict, VERDICT_BREACH)
        self.assertTrue(a.breaches)

    def test_truncated_serialisation_keeps_the_breach(self):
        # A caller rendering only the top N must not lose a breach to the tail.
        a = assess([Position("VOO", 10_000.0), Position("AAPL", 9_000.0)],
                   PortfolioPolicy(max_single_name_pct=5.0), resolver)
        blob = a.as_dict(top=1)
        self.assertEqual(blob["checks"][0]["verdict"], VERDICT_BREACH)
        self.assertGreater(blob["check_count"], 1)


class TestRenderBlock(unittest.TestCase):
    """The block is model input; its job is to make a wrong reading hard."""

    def test_block_states_the_floor_caveat_and_forbids_headroom(self):
        a = assess([Position("VOO", 10_000.0)], POLICY_8, resolver,
                   trade=Trade("NVDA", 1_000.0))
        text = render_block(a)
        self.assertIn("FLOOR", text)
        self.assertIn("Do NOT compute headroom", text)
        self.assertIn("Only PASS is a pass", text)

    def test_block_is_delimited(self):
        text = render_block(assess([Position("VOO", 10_000.0)], POLICY_8, resolver))
        self.assertTrue(text.startswith("<start_of_portfolio_constraints>"))
        self.assertTrue(text.rstrip().endswith("<end_of_portfolio_constraints>"))

    def test_block_reports_both_ends_of_the_band(self):
        # An undisclosed name is where the two ends genuinely differ: MYSTERY
        # discloses only AAPL, so NVDA's floor is 0 and its ceiling is the smallest
        # disclosed weight, 25%.
        a = assess([Position("MYSTERY", 10_000.0)], POLICY_8, resolver,
                   trade=Trade("NVDA", 0.0))
        text = render_block(a)
        self.assertIn("at least 0.00%", text)
        self.assertIn("at most 25.00%", text)

    def test_block_marks_a_verified_band(self):
        # A disclosed name in a flat fund has an exact exposure, and the block must
        # say so -- otherwise a reader discounts a number that needs no discount.
        a = assess([Position("VOO", 10_000.0)], POLICY_8, resolver)
        text = render_block(a)
        self.assertIn("band is verified (nothing unidentified)", text)
        self.assertIn("at least 8.00% and at most 8.00%", text)

    def test_block_names_the_trade(self):
        buy = render_block(assess([Position("AAPL", 1_000.0)], POLICY_8, resolver,
                                  trade=Trade("NVDA", 250.0)))
        self.assertIn("BUY 250 USD of NVDA", buy)
        sell = render_block(assess([Position("AAPL", 1_000.0)], POLICY_8, resolver,
                                   trade=Trade("AAPL", -250.0)))
        self.assertIn("SELL 250 USD of AAPL", sell)

    def test_block_truncation_is_announced(self):
        positions = [Position("VOO", 10_000.0), Position("QQQ", 5_000.0)]
        a = assess(positions, POLICY_8, resolver)
        text = render_block(a, top=1)
        self.assertIn("further names not shown", text)

    def test_block_survives_an_empty_portfolio(self):
        text = render_block(assess([], POLICY_8, resolver))
        self.assertIn("OVERALL VERDICT", text)


class TestPurity(unittest.TestCase):
    """``exposure`` must stay reusable with no app, cache or network."""

    def test_imports_only_stdlib_and_lookthrough(self):
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "ystocker" / "exposure.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        banned = {"boto3", "yfinance", "requests", "flask", "curl_cffi",
                  "matplotlib", "pandas", "numpy"}
        local_ok = {"ystocker", "ystocker.lookthrough"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    self.assertNotIn(root, banned, f"exposure imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                self.assertNotIn(root, banned, f"exposure imports {node.module}")
                if root == "ystocker":
                    self.assertIn(node.module, local_ok,
                                  f"exposure grew a sibling import: {node.module}")

    def test_resolver_exception_does_not_void_the_assessment(self):
        def angry(symbol):
            if symbol.upper() == "AMZN":
                raise RuntimeError("vendor on fire")
            return UNIVERSE.get(symbol.upper())

        a = assess([Position("VOO", 10_000.0)], POLICY_8, angry)
        self.assertAlmostEqual(a.total_after, 10_000.0, places=6)
        self.assertAlmostEqual(
            a.attributed_value + a.unknown_value + a.known_non_equity_value,
            a.total_after, places=6)

    def test_assessment_is_serialisable(self):
        import json

        a = assess(PORTFOLIOS["mixed"], POLICY_8, resolver, trade=Trade("NVDA", 500.0))
        json.dumps(a.as_dict())   # must not raise


# ---------------------------------------------------------------------------

def _check(assessment: Assessment, symbol: str):
    """Find one check by key.

    Matched exactly rather than upper-cased: ``Check.symbol`` carries an
    upper-case ticker for a single-name check but a free-form issuer label
    ("Alphabet") for a group, and folding the case here hid a present row.
    """
    for c in assessment.checks:
        if c.symbol == symbol:
            return c
    raise AssertionError(f"no check for {symbol!r}: "
                         f"{[c.symbol for c in assessment.checks]}")


if __name__ == "__main__":
    unittest.main()
