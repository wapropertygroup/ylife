"""
Unit tests for the market-context / relative-strength handoff into
TradingAgents — the sibling bridge to ``test_agents_portfolio_context.py``.

No app, no subprocess, no LLM: this covers the parent side only — building
the two blocks, staging them for the child, the child's argv contract, and
that the sidecar is cleaned up with the rest of a pruned job's files.

Unlike the portfolio bridge, there is no disclosure guard to test here: both
blocks describe public market data and a ticker's public peer group, never
anything about the requesting user, so both fields are meant to be public
(see the comment beside them in ``_PUBLIC_FIELDS``).
"""

from __future__ import annotations

import unittest
from unittest import mock

from ystocker import agents


class TestRunnerContract(unittest.TestCase):
    """The child is an inline `-c` program; its argv contract is a real interface."""

    def test_child_reads_a_json_envelope_from_argv_seven(self):
        self.assertIn("CONTEXT_PATH = sys.argv[7]", agents._RUNNER)
        self.assertIn("_cbundle = json.loads(_craw)", agents._RUNNER)

    def test_child_extracts_both_keys_from_the_bundle(self):
        self.assertIn('MARKET_CONTEXT = _cbundle.get("market")', agents._RUNNER)
        self.assertIn(
            'RELATIVE_STRENGTH_CONTEXT = _cbundle.get("relative_strength")',
            agents._RUNNER)

    def test_child_source_is_valid_python(self):
        import ast

        ast.parse(agents._RUNNER)

    def test_child_passes_each_kwarg_only_when_non_empty(self):
        self.assertIn("if MARKET_CONTEXT.strip():", agents._RUNNER)
        self.assertIn('kwargs["market_context"] = MARKET_CONTEXT', agents._RUNNER)
        self.assertIn("if RELATIVE_STRENGTH_CONTEXT.strip():", agents._RUNNER)
        self.assertIn(
            'kwargs["relative_strength_context"] = RELATIVE_STRENGTH_CONTEXT',
            agents._RUNNER)

    def test_child_survives_a_checkout_without_either_kwarg(self):
        # Same one-retry fallback as portfolio_context/portfolio_data, extended
        # to cover all four optional kwargs rather than isolating exactly one.
        self.assertIn("except TypeError", agents._RUNNER)
        self.assertIn('kwargs.pop("market_context", None)', agents._RUNNER)
        self.assertIn('kwargs.pop("relative_strength_context", None)',
                      agents._RUNNER)

    def test_child_tolerates_an_unreadable_bundle(self):
        self.assertIn("market/relative-strength context unreadable", agents._RUNNER)

    def test_child_tolerates_a_non_json_bundle(self):
        self.assertIn("_cbundle = {}", agents._RUNNER)

    def test_sidecar_is_pruned_with_the_job(self):
        import inspect

        source = inspect.getsource(agents._prune)
        self.assertIn(".context.txt", source)

    def test_context_path_sits_beside_the_other_sidecars(self):
        path = agents._context_path("abc123")
        self.assertEqual(path.parent, agents.JOB_DIR)
        self.assertTrue(path.name.endswith(".context.txt"))

    def test_context_path_is_distinct_from_portfolio_path(self):
        # Different lifecycles: portfolio is user-gated, this is ticker-gated
        # and always attempted -- conflating the files would tangle them.
        self.assertNotEqual(agents._context_path("j1"), agents._portfolio_path("j1"))


class TestBuildMarketContext(unittest.TestCase):
    """Every failure path must yield "", never a placeholder and never a raise."""

    def test_kill_switch_yields_empty(self):
        for value in ("0", "false", "no"):
            with mock.patch.dict("os.environ", {"AGENTS_MARKET_CONTEXT": value}):
                self.assertEqual(agents.build_market_context(), "")

    def test_all_sources_cold_yields_empty(self):
        from ystocker import breadth, cta, fedwatch, valuation

        with mock.patch.object(valuation, "peek", return_value=None), \
             mock.patch.object(breadth, "peek", return_value=None), \
             mock.patch.object(cta, "get_cta_positioning", return_value={}), \
             mock.patch.object(fedwatch, "peek", return_value=None):
            self.assertEqual(agents.build_market_context(), "")

    def test_a_source_raising_yields_empty_not_a_raise(self):
        from ystocker import valuation

        with mock.patch.object(valuation, "peek", side_effect=RuntimeError("boom")):
            self.assertEqual(agents.build_market_context(), "")

    def test_a_real_snapshot_produces_a_non_empty_block(self):
        from ystocker import breadth, cta, fedwatch, valuation

        with mock.patch.object(valuation, "peek", return_value={
                    "headline": {"spx_pe_percentile": {"value": 78.0,
                                                        "since": "1871-01-01"}}}), \
             mock.patch.object(breadth, "peek", return_value=None), \
             mock.patch.object(cta, "get_cta_positioning", return_value={}), \
             mock.patch.object(fedwatch, "peek", return_value=None):
            block = agents.build_market_context()
        self.assertIn("78.0th percentile", block)

    def test_never_calls_the_fetching_forms(self):
        # get()/get_valuation_data()/refresh_cache() etc. can take minutes cold;
        # this runs on the submit path and must only ever peek.
        import inspect

        source = inspect.getsource(agents.build_market_context)
        for forbidden in ("valuation.get_valuation_data", "breadth.get_breadth",
                         "cta.refresh_cache", "fedwatch.get_fedwatch_data"):
            self.assertNotIn(forbidden, source)


class TestBuildRelativeStrengthContext(unittest.TestCase):
    def test_no_ticker_yields_empty(self):
        self.assertEqual(agents.build_relative_strength_context(""), "")
        self.assertEqual(agents.build_relative_strength_context(None), "")

    def test_kill_switch_yields_empty(self):
        for value in ("0", "false", "no"):
            with mock.patch.dict("os.environ",
                                 {"AGENTS_RELATIVE_STRENGTH_CONTEXT": value}):
                self.assertEqual(agents.build_relative_strength_context("NVDA"), "")

    def test_cold_cache_yields_empty(self):
        from ystocker import analyst

        with mock.patch.object(analyst, "peek", return_value=None):
            self.assertEqual(agents.build_relative_strength_context("NVDA"), "")

    def test_unexpected_error_yields_empty(self):
        from ystocker import analyst

        with mock.patch.object(analyst, "peek", side_effect=RuntimeError("boom")):
            self.assertEqual(agents.build_relative_strength_context("NVDA"), "")

    def test_a_real_payload_produces_a_non_empty_block(self):
        from ystocker import analyst

        payload = {"tickers": {
            "NVDA": {"eps_trend": {"+1y": {"chg30_pct": 3.2}}},
            "AVGO": {"eps_trend": {"+1y": {"chg30_pct": 1.1}}},
        }, "asof": "2026-08-28"}
        with mock.patch.object(analyst, "peek", return_value=payload), \
             mock.patch("ystocker.PEER_GROUPS", {"Semiconductors": ["NVDA", "AVGO"]}):
            block = agents.build_relative_strength_context("NVDA")
        self.assertIn("NVDA", block)
        self.assertIn("AVGO", block)

    def test_never_calls_the_fetching_form(self):
        import inspect

        source = inspect.getsource(agents.build_relative_strength_context)
        self.assertNotIn("analyst.get(", source)


class TestPublicFieldAllowlist(unittest.TestCase):
    """Unlike portfolio_context, both flags here are meant to be public."""

    def test_both_flags_are_in_the_allowlist(self):
        self.assertIn("market_context", agents._PUBLIC_FIELDS)
        self.assertIn("relative_strength_context", agents._PUBLIC_FIELDS)

    def test_portfolio_context_is_still_excluded(self):
        # Regression guard: adding these two must not have loosened the
        # portfolio guard by editing the wrong tuple.
        self.assertNotIn("portfolio_context", agents._PUBLIC_FIELDS)


class TestRunWiring(unittest.TestCase):
    """The pieces above are only real if ``_run`` actually calls them."""

    def test_run_calls_both_builders(self):
        import inspect

        source = inspect.getsource(agents._run)
        self.assertIn("build_market_context()", source)
        self.assertIn("build_relative_strength_context(", source)

    def test_run_appends_context_arg_to_the_command(self):
        import inspect

        source = inspect.getsource(agents._run)
        self.assertIn("portfolio_arg, context_arg", source)


if __name__ == "__main__":
    unittest.main()
