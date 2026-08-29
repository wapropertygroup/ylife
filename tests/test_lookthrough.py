"""
Unit tests for ystocker.lookthrough — the 穿透 engine.

No app, no network, no cache: the fund resolver is injected, which is the whole
reason the module takes one. The most important test here is
:meth:`TestInvariant.test_partition_is_closed_for_every_ordering` plus
:meth:`TestInvariant.test_dollars_are_conserved` — if the partition ever stops
summing to the portfolio total then every percentage the page shows is wrong at
once, and it would be wrong *quietly*.
"""

from __future__ import annotations

import unittest

from ystocker.lookthrough import (
    DEFAULT_MAX_DEPTH,
    LEAF_EQUITY,
    LEAF_PENDING,
    LEAF_TRUNCATED,
    LEAF_UNRESOLVED,
    Position,
    _partition,
    analyse,
)

# ---------------------------------------------------------------------------
# A fake universe modelled on the real Yahoo payloads probed while designing
# this: VOO's top ten really do sum to 37.62% against a 99.87% equity sleeve,
# BND really does report zero holdings with a 98.36% bond sleeve, and VTTSX
# really does hold VSMPX at 54.15%.
# ---------------------------------------------------------------------------

def _fund(symbol, name, holdings, stock=1.0, bond=0.0, cash=0.0):
    return {"symbol": symbol, "name": name, "kind": "fund",
            "holdings": [{"symbol": s, "name": n, "weight": w} for s, n, w in holdings],
            "asset_classes": {"stock": stock, "bond": bond, "cash": cash,
                              "preferred": 0.0, "convertible": 0.0, "other": 0.0}}


def _equity(symbol, name=""):
    return {"symbol": symbol, "name": name or symbol, "kind": "equity",
            "holdings": [], "asset_classes": {}}


UNIVERSE = {
    # visible 0.40, equity sleeve 1.0 -> 0.60 undisclosed equity
    "VOO": _fund("VOO", "Vanguard S&P 500", [
        ("NVDA", "NVIDIA Corp", 0.08),
        ("AAPL", "Apple Inc", 0.07),
        ("MSFT", "Microsoft Corp", 0.06),
        ("AMZN", "Amazon.com Inc", 0.19),
    ]),
    # visible 0.50, equity sleeve 1.0 -> 0.50 undisclosed equity
    "QQQ": _fund("QQQ", "Invesco QQQ", [
        ("NVDA", "NVIDIA Corp", 0.10),
        ("AAPL", "Apple Inc", 0.10),
        ("MSFT", "Microsoft Corp", 0.30),
    ]),
    # A bond fund: discloses nothing, and reports itself as no equity at all.
    "BND": _fund("BND", "Vanguard Total Bond", [], stock=0.0, bond=0.9836, cash=0.0164),
    # Fund of funds -> reaches companies only at depth 2.
    "VTTSX": _fund("VTTSX", "Vanguard Target 2060", [
        ("VSMPX", "Vanguard Total Stock Mkt", 0.60),
        ("VTBIX", "Vanguard Total Bond II", 0.40),
    ]),
    "VSMPX": _fund("VSMPX", "Vanguard Total Stock Mkt", [
        ("NVDA", "NVIDIA Corp", 0.10),
        ("AAPL", "Apple Inc", 0.10),
    ]),
    "VTBIX": _fund("VTBIX", "Vanguard Total Bond II", [], stock=0.0, bond=1.0),
    # Holds a symbol Yahoo 404s on, exactly like XTSLA inside AOR.
    "AOR": _fund("AOR", "iShares Core Growth", [
        ("NVDA", "NVIDIA Corp", 0.30),
        ("XTSLA", "BlackRock Cash Funds", 0.10),
    ]),
    # No asset-class block at all -> residual is unclassified, not equity.
    "MYST": {"symbol": "MYST", "name": "Mystery Fund", "kind": "fund",
             "holdings": [{"symbol": "AAPL", "name": "Apple Inc", "weight": 0.25}],
             "asset_classes": {}},
    # Vendor glitch: weights sum to 1.30.
    "BADW": _fund("BADW", "Bad Weights", [
        ("AAPL", "Apple Inc", 0.65),
        ("MSFT", "Microsoft Corp", 0.65),
    ]),
    # Mutually recursive pair.
    "CYC1": _fund("CYC1", "Cycle One", [("CYC2", "Cycle Two", 1.0)]),
    "CYC2": _fund("CYC2", "Cycle Two", [("CYC1", "Cycle One", 1.0)]),
    "NVDA": _equity("NVDA", "NVIDIA Corp"),
    "AAPL": _equity("AAPL", "Apple Inc"),
    "MSFT": _equity("MSFT", "Microsoft Corp"),
    "AMZN": _equity("AMZN", "Amazon.com Inc"),
}


def resolver(symbol):
    """Stand-in for funddata.peek: None means genuinely unresolvable."""
    return UNIVERSE.get(symbol.upper())


def pending_resolver(pending_set):
    def _r(symbol):
        if symbol.upper() in pending_set:
            return {"symbol": symbol, "name": symbol, "kind": "pending",
                    "holdings": [], "asset_classes": {}}
        return UNIVERSE.get(symbol.upper())
    return _r


def _exp(result, symbol):
    for e in result.exposures:
        if e.symbol == symbol:
            return e
    raise AssertionError(f"no exposure for {symbol}")


class TestPartition(unittest.TestCase):
    """The three shares must sum to exactly 1.0 under every input ordering."""

    def test_partition_is_closed_for_every_ordering(self):
        for visible in (0.0, 0.13, 0.376, 0.5, 0.9836, 1.0):
            for stock in (0.0, 0.1, 0.376, 0.5, 0.9987, 1.0):
                vis, und, non = _partition(visible, stock)
                self.assertAlmostEqual(vis + und + non, 1.0, places=9,
                                       msg=f"visible={visible} stock={stock}")
                for name, share in (("visible", vis), ("undisclosed", und),
                                    ("non_equity", non)):
                    self.assertGreaterEqual(share, 0.0, f"{name} went negative")

    def test_bond_fund_residual_is_not_called_hidden_equity(self):
        # BND: nothing disclosed, no equity. The naive `1 - visible` would report
        # the whole fund as equity we cannot see.
        vis, und, non = _partition(0.0, 0.0)
        self.assertEqual(vis, 0.0)
        self.assertEqual(und, 0.0)
        self.assertAlmostEqual(non, 1.0)

    def test_equity_fund_residual_is_hidden_equity(self):
        vis, und, non = _partition(0.376, 0.9987)
        self.assertAlmostEqual(und, 0.6227, places=4)
        self.assertAlmostEqual(non, 0.0013, places=4)

    def test_disclosed_beyond_equity_sleeve_does_not_go_negative(self):
        # An allocation fund whose top ten already span its bond sleeve.
        vis, und, non = _partition(0.80, 0.60)
        self.assertEqual(und, 0.0)
        self.assertAlmostEqual(non, 0.20)

    def test_missing_asset_classes_signals_unclassified(self):
        vis, und, non = _partition(0.25, None)
        self.assertEqual((und, non), (0.0, 0.0))


class TestInvariant(unittest.TestCase):

    PORTFOLIOS = {
        "plain etf":      [Position("VOO", 10_000.0)],
        "bond fund":      [Position("BND", 5_000.0)],
        "fund of funds":  [Position("VTTSX", 20_000.0)],
        "unresolvable":   [Position("AOR", 8_000.0)],
        "unclassified":   [Position("MYST", 1_000.0)],
        "bad weights":    [Position("BADW", 1_000.0)],
        "cycle":          [Position("CYC1", 1_000.0)],
        "direct equity":  [Position("AAPL", 2_500.0)],
        "mixed":          [Position("VOO", 10_000.0), Position("QQQ", 6_000.0),
                           Position("BND", 4_000.0), Position("AAPL", 2_000.0),
                           Position("VTTSX", 8_000.0), Position("AOR", 3_000.0),
                           Position("MYST", 1_000.0)],
    }

    def test_dollars_are_conserved(self):
        """seen + every residual bucket == portfolio total, for every shape."""
        for label, positions in self.PORTFOLIOS.items():
            with self.subTest(label):
                r = analyse(positions, resolver)
                self.assertAlmostEqual(
                    r.seen_value + r.residual.total(), r.total_value, places=6,
                    msg=f"{label}: {r.seen_value} + {r.residual.total()} "
                        f"!= {r.total_value}")

    def test_per_position_residuals_also_close(self):
        r = analyse(self.PORTFOLIOS["mixed"], resolver)
        for row in r.per_position:
            with self.subTest(row["symbol"]):
                self.assertAlmostEqual(
                    row["seen_value"] + row["residual"]["total"]["value"],
                    row["value"], places=2)

    def test_coverage_never_exceeds_100(self):
        for label, positions in self.PORTFOLIOS.items():
            with self.subTest(label):
                r = analyse(positions, resolver)
                self.assertLessEqual(r.coverage_pct, 100.0 + 1e-9)
                self.assertGreaterEqual(r.coverage_pct, 0.0)


class TestLookThrough(unittest.TestCase):

    def test_single_etf_allocates_by_weight(self):
        r = analyse([Position("VOO", 10_000.0)], resolver)
        self.assertAlmostEqual(_exp(r, "NVDA").value, 800.0)
        self.assertAlmostEqual(r.coverage_pct, 40.0)
        # The 60% we cannot see is hidden equity, and is reported as such.
        self.assertAlmostEqual(r.residual.undisclosed_equity, 6_000.0)
        self.assertAlmostEqual(r.residual.non_equity, 0.0)

    def test_bond_fund_yields_no_companies_and_no_hidden_equity(self):
        r = analyse([Position("BND", 5_000.0)], resolver)
        self.assertEqual(r.coverage_pct, 0.0)
        self.assertAlmostEqual(r.residual.non_equity, 5_000.0)
        self.assertAlmostEqual(r.residual.undisclosed_equity, 0.0)
        self.assertAlmostEqual(r.residual.unclassified, 0.0)

    def test_fund_of_funds_reaches_real_companies(self):
        # VTTSX -> VSMPX (60%) -> NVDA (10%) = 6% of the position.
        r = analyse([Position("VTTSX", 20_000.0)], resolver)
        self.assertAlmostEqual(_exp(r, "NVDA").value, 1_200.0)
        chain = _exp(r, "NVDA").routes[0]["chain"]
        self.assertEqual(list(chain), ["VTTSX", "VSMPX"])
        # The bond sleeve inside must not be called hidden equity.
        self.assertAlmostEqual(r.residual.non_equity, 8_000.0)

    def test_one_level_would_have_reported_a_fund_as_a_holding(self):
        """Guards the reason recursion exists at all."""
        shallow = analyse([Position("VTTSX", 20_000.0)], resolver, max_depth=1)
        self.assertEqual(_exp(shallow, "VSMPX").kind, LEAF_TRUNCATED)
        deep = analyse([Position("VTTSX", 20_000.0)], resolver, max_depth=3)
        self.assertNotIn("VSMPX", [e.symbol for e in deep.exposures
                                   if e.kind == LEAF_EQUITY])

    def test_unresolvable_child_is_kept_as_a_named_leaf(self):
        r = analyse([Position("AOR", 8_000.0)], resolver)
        xtsla = _exp(r, "XTSLA")
        self.assertEqual(xtsla.kind, LEAF_UNRESOLVED)
        self.assertAlmostEqual(xtsla.value, 800.0)
        # It counts against coverage rather than vanishing from the total.
        self.assertAlmostEqual(r.residual.unresolved, 800.0)
        self.assertNotIn("XTSLA", [e.symbol for e in r.exposures
                                   if e.kind == LEAF_EQUITY])

    def test_missing_asset_classes_becomes_unclassified_not_equity(self):
        r = analyse([Position("MYST", 1_000.0)], resolver)
        self.assertAlmostEqual(r.residual.unclassified, 750.0)
        self.assertAlmostEqual(r.residual.undisclosed_equity, 0.0)

    def test_cycle_is_broken(self):
        r = analyse([Position("CYC1", 1_000.0)], resolver)
        self.assertTrue(any("cycle" in n for n in r.notes), r.notes)
        self.assertAlmostEqual(r.seen_value + r.residual.total(), 1_000.0, places=6)

    def test_overweight_vendor_weights_are_renormalised_not_multiplied(self):
        r = analyse([Position("BADW", 1_000.0)], resolver)
        self.assertAlmostEqual(_exp(r, "AAPL").value + _exp(r, "MSFT").value,
                               1_000.0, places=6)
        self.assertTrue(any("renormalised" in n for n in r.notes), r.notes)

    def test_pending_is_distinct_from_unresolved(self):
        r = analyse([Position("VOO", 10_000.0)], pending_resolver({"NVDA"}))
        self.assertEqual(_exp(r, "NVDA").kind, LEAF_PENDING)
        self.assertEqual(r.pending_symbols, ["NVDA"])
        self.assertAlmostEqual(r.residual.pending, 800.0)
        self.assertAlmostEqual(r.residual.unresolved, 0.0)

    def test_negative_and_zero_positions_are_excluded_and_noted(self):
        r = analyse([Position("AAPL", 1_000.0), Position("MSFT", 0.0),
                     Position("NVDA", -500.0)], resolver)
        self.assertAlmostEqual(r.total_value, 1_000.0)
        self.assertTrue(any("negative" in n for n in r.notes), r.notes)

    def test_unknown_top_level_symbol_does_not_void_the_portfolio(self):
        r = analyse([Position("AAPL", 1_000.0), Position("ZZZZ", 500.0)], resolver)
        self.assertAlmostEqual(r.total_value, 1_500.0)
        self.assertEqual(_exp(r, "ZZZZ").kind, LEAF_UNRESOLVED)

    def test_resolver_exception_is_contained(self):
        def boom(symbol):
            if symbol == "NVDA":
                raise RuntimeError("vendor exploded")
            return UNIVERSE.get(symbol)
        r = analyse([Position("VOO", 10_000.0)], boom)
        self.assertEqual(_exp(r, "NVDA").kind, LEAF_UNRESOLVED)
        self.assertAlmostEqual(r.seen_value + r.residual.total(), 10_000.0, places=6)


class TestOverlap(unittest.TestCase):
    """The feature 穿透 exists for: concentration you cannot see on a statement."""

    PORTFOLIO = [Position("VOO", 10_000.0), Position("QQQ", 10_000.0),
                 Position("AAPL", 1_000.0)]

    def test_same_company_through_several_holdings_is_merged(self):
        r = analyse(self.PORTFOLIO, resolver)
        nvda = _exp(r, "NVDA")
        # 8% of VOO + 10% of QQQ
        self.assertAlmostEqual(nvda.value, 800.0 + 1_000.0)
        self.assertEqual(nvda.route_count, 2)
        self.assertAlmostEqual(nvda.direct_value, 0.0)

    def test_direct_and_indirect_are_split(self):
        r = analyse(self.PORTFOLIO, resolver)
        aapl = _exp(r, "AAPL")
        # 1000 direct + 7% of VOO + 10% of QQQ
        self.assertAlmostEqual(aapl.direct_value, 1_000.0)
        self.assertAlmostEqual(aapl.indirect_value, 700.0 + 1_000.0)
        self.assertAlmostEqual(aapl.value, 2_700.0)
        self.assertEqual(aapl.route_count, 3)

    def test_overlaps_lists_only_multi_route_names(self):
        r = analyse(self.PORTFOLIO, resolver)
        symbols = [e.symbol for e in r.overlaps()]
        self.assertIn("NVDA", symbols)
        self.assertIn("AAPL", symbols)
        # AMZN is only in VOO, so it is not an overlap.
        self.assertNotIn("AMZN", symbols)

    def test_exposures_are_sorted_largest_first(self):
        r = analyse(self.PORTFOLIO, resolver)
        values = [e.value for e in r.exposures]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_as_dict_percentages_are_relative_to_the_whole_portfolio(self):
        r = analyse(self.PORTFOLIO, resolver)
        payload = r.as_dict()
        self.assertAlmostEqual(payload["total_value"], 21_000.0)
        nvda = next(e for e in payload["exposures"] if e["symbol"] == "NVDA")
        self.assertAlmostEqual(nvda["pct"], 1_800.0 / 21_000.0 * 100, places=4)
        self.assertAlmostEqual(nvda["direct_pct"] + nvda["indirect_pct"],
                               nvda["pct"], places=4)

    def test_route_count_counts_roots_not_paths(self):
        # Both sleeves of VTTSX reach nothing twice, but AOR + VOO both reach NVDA.
        r = analyse([Position("VOO", 1_000.0), Position("AOR", 1_000.0)], resolver)
        self.assertEqual(_exp(r, "NVDA").route_count, 2)


class TestDefaults(unittest.TestCase):

    def test_default_depth_reaches_through_a_fund_of_funds(self):
        self.assertGreaterEqual(DEFAULT_MAX_DEPTH, 2)
        r = analyse([Position("VTTSX", 1_000.0)], resolver)
        self.assertIn("NVDA", [e.symbol for e in r.exposures
                               if e.kind == LEAF_EQUITY])


if __name__ == "__main__":
    unittest.main()
