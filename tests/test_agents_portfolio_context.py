"""
Unit tests for the portfolio-context handoff into TradingAgents.

No app, no subprocess, no LLM: this covers the parent side of the bridge —
building the block, staging it for the child, the child's argv contract, and the
two disclosure guards that keep a portfolio-bearing run off a public URL.

The tests that matter most are the disclosure ones. ``build_portfolio_context``
failing open (returning "") is deliberate and safe, but a *report* built from that
block reaching ``/agents/shared/<token>`` or the anonymous showcase is not
recoverable — once the mail is out, the holdings are out.
"""

from __future__ import annotations

import unittest
from unittest import mock

from ystocker import agents


class TestRunnerContract(unittest.TestCase):
    """The child is an inline `-c` program; its argv contract is a real interface."""

    def test_child_reads_the_block_from_argv_six(self):
        # A file, not argv or an env var: the block is a few KB and both of those
        # have OS length limits whose failure is an opaque E2BIG at exec time.
        self.assertIn("PORTFOLIO_PATH = sys.argv[6]", agents._RUNNER)

    def test_child_source_is_valid_python(self):
        import ast

        ast.parse(agents._RUNNER)

    def test_child_passes_the_kwarg_only_when_non_empty(self):
        self.assertIn('if PORTFOLIO_CONTEXT.strip():', agents._RUNNER)
        self.assertIn('kwargs["portfolio_context"] = PORTFOLIO_CONTEXT',
                      agents._RUNNER)

    def test_child_survives_a_checkout_without_the_kwarg(self):
        # deploy.sh repoints the checkout, but a stale box must lose one feature
        # rather than fail every run the user has already been charged for.
        self.assertIn("except TypeError", agents._RUNNER)
        self.assertIn('kwargs.pop("portfolio_context", None)', agents._RUNNER)

    def test_child_tolerates_an_unreadable_block(self):
        self.assertIn("portfolio context unreadable", agents._RUNNER)

    def test_sidecar_is_pruned_with_the_job(self):
        import inspect

        source = inspect.getsource(agents._prune)
        self.assertIn(".portfolio.txt", source)

    def test_portfolio_path_sits_beside_the_other_sidecars(self):
        path = agents._portfolio_path("abc123")
        self.assertEqual(path.parent, agents.JOB_DIR)
        self.assertTrue(path.name.endswith(".portfolio.txt"))


class TestBuildPortfolioContext(unittest.TestCase):
    """Every failure path must yield "", never a placeholder and never a raise."""

    def test_no_email_yields_empty(self):
        self.assertEqual(agents.build_portfolio_context(""), "")
        self.assertEqual(agents.build_portfolio_context(None), "")

    def test_kill_switch_yields_empty(self):
        for value in ("0", "false", "no"):
            with mock.patch.dict("os.environ",
                                 {"AGENTS_PORTFOLIO_CONTEXT": value}):
                self.assertEqual(agents.build_portfolio_context("a@b.com"), "")

    def test_store_outage_yields_empty_not_a_raise(self):
        # A run must not fail because a portfolio could not be read. The block is
        # an input to a judgement; losing it costs specificity, not correctness.
        from ystocker import portfolio

        with mock.patch.object(portfolio, "load",
                               side_effect=portfolio.StoreUnavailable("down")):
            self.assertEqual(agents.build_portfolio_context("a@b.com"), "")

    def test_no_positions_yields_empty(self):
        from ystocker import portfolio

        with mock.patch.object(portfolio, "load", return_value=[]):
            self.assertEqual(agents.build_portfolio_context("a@b.com"), "")

    def test_unexpected_error_yields_empty(self):
        from ystocker import portfolio

        with mock.patch.object(portfolio, "load",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(agents.build_portfolio_context("a@b.com"), "")

    def test_a_real_portfolio_produces_a_delimited_block(self):
        from ystocker import funddata, portfolio

        universe = {
            "AAPL": {"symbol": "AAPL", "name": "Apple Inc", "kind": "equity",
                     "holdings": [], "asset_classes": {}, "price": 100.0},
            "MSFT": {"symbol": "MSFT", "name": "Microsoft", "kind": "equity",
                     "holdings": [], "asset_classes": {}, "price": 100.0},
        }
        with mock.patch.object(portfolio, "load", return_value=[
                    {"symbol": "AAPL", "value": 9000.0},
                    {"symbol": "MSFT", "value": 1000.0}]), \
             mock.patch.object(portfolio, "load_policy", return_value={
                    "max_single_name_pct": 8.0, "max_issuer_pct": None,
                    "cash": 0.0, "holding_types": {}}), \
             mock.patch.object(funddata, "peek",
                               side_effect=lambda s, **kw: universe.get(s.upper())):
            block = agents.build_portfolio_context("a@b.com")

        self.assertTrue(block.startswith("<start_of_portfolio_constraints>"))
        self.assertIn("AAPL", block)
        self.assertIn("BREACH", block)          # 90% against an 8% limit
        self.assertIn("Only PASS is a pass", block)
        self.assertNotIn("headroom", block.lower().replace(
            "do not compute headroom", ""))     # the instruction is allowed

    def test_no_stated_limits_still_produces_a_holdings_block(self):
        # Unlike /api/assets, which returns no constraints without a limit, the
        # agents still benefit from knowing what is held.
        from ystocker import funddata, portfolio

        with mock.patch.object(portfolio, "load", return_value=[
                    {"symbol": "AAPL", "value": 1000.0}]), \
             mock.patch.object(portfolio, "load_policy", return_value={
                    "max_single_name_pct": None, "max_issuer_pct": None,
                    "cash": 0.0, "holding_types": {}}), \
             mock.patch.object(funddata, "peek", side_effect=lambda s, **kw: {
                    "symbol": "AAPL", "name": "Apple", "kind": "equity",
                    "holdings": [], "asset_classes": {}, "price": 10.0}):
            block = agents.build_portfolio_context("a@b.com")

        self.assertIn("<start_of_portfolio_constraints>", block)
        self.assertIn("no limits were stated", block)


class TestDisclosureGuards(unittest.TestCase):
    """A portfolio-bearing run must never become readable without sign-in."""

    def _job(self, **over):
        job = {"id": "j1", "user": "owner@example.com", "status": "done",
               "report": "# Report\n\nbody", "ticker": "NVDA", "lang": "en"}
        job.update(over)
        return job

    def test_showcase_excludes_a_portfolio_run(self):
        # The riskier of the two paths: the showcase needs no token at all.
        with mock.patch.object(agents, "showcase_enabled", return_value=True), \
             mock.patch.object(agents, "showcase_emails", return_value=set()):
            self.assertTrue(agents._is_showcase(self._job()))
            self.assertFalse(
                agents._is_showcase(self._job(portfolio_context=True)))

    def test_showcase_exclusion_hides_it_from_the_listing_too(self):
        # Checked in _is_showcase rather than _publishable, so the run does not
        # appear as a title whose body then 404s.
        import inspect

        self.assertIn("portfolio_context",
                      inspect.getsource(agents._is_showcase))

    def test_share_refuses_a_portfolio_run(self):
        from ystocker import share

        with mock.patch.object(share, "_get_table", return_value=object()):
            self.assertIsNone(share.create(self._job(portfolio_context=True),
                                           sharer="owner@example.com",
                                           recipient="friend@example.com"))

    def test_share_refusal_happens_before_the_row_is_written(self):
        # The row is the share. Writing it and then refusing would leave a live
        # capability behind.
        from ystocker import share

        table = mock.Mock()
        with mock.patch.object(share, "_get_table", return_value=table):
            share.create(self._job(portfolio_context=True),
                         sharer="owner@example.com",
                         recipient="friend@example.com")
        table.put_item.assert_not_called()

    def test_portfolio_flag_is_not_in_the_public_field_allowlist(self):
        # It would only leak a boolean, but the allowlist is the design and a
        # field that says "this user has a portfolio here" is not a visitor's
        # business.
        self.assertNotIn("portfolio_context", agents._PUBLIC_FIELDS)

    def test_a_normal_run_is_still_shareable_and_showable(self):
        # The guard must not be so broad that it disables the features.
        from ystocker import share

        with mock.patch.object(agents, "showcase_enabled", return_value=True), \
             mock.patch.object(agents, "showcase_emails", return_value=set()):
            self.assertTrue(agents._is_showcase(self._job()))
        table = mock.Mock()
        table.put_item.return_value = None
        with mock.patch.object(share, "_get_table", return_value=table):
            row = share.create(self._job(), sharer="owner@example.com",
                               recipient="friend@example.com")
        self.assertIsNotNone(row)
        table.put_item.assert_called_once()


if __name__ == "__main__":
    unittest.main()
