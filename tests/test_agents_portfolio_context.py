"""
Unit tests for the portfolio-context handoff into TradingAgents.

No app, no subprocess, no LLM: this covers the parent side of the bridge —
building the block, staging it for the child, the child's argv contract, and
the two disclosure guards that decide who else may ever see a portfolio-bearing
run.

The tests that matter most are the disclosure ones, and the two guards are
deliberately *not* symmetric. ``build_portfolio_context`` failing open
(returning "") is safe on its own, but what happens next to a report built from
that block differs by path: the anonymous showcase publishes automatically, to
strangers, with no owner in the loop at all, so it serves such a run only
through ``agents.portfolio_blind_report`` -- the turns written before the block
was read, never the five seats that were handed it (``TestPortfolioWiring``
reads which seats those are out of the TradingAgents checkout). Explicit sharing
(``share.create``) does not withhold anything -- the account holder may send
their own portfolio-bearing report to somebody they choose, because it is their
report and their call, made by their own signed-in action. Once such a link is
sent it is still not recoverable — the recipient needs no sign-in and the report
text can still quote specific positions — which is why the UI shows a stronger
warning for exactly this case (share.js) rather than treating it like any other
share.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from ystocker import agents


class TestRunnerContract(unittest.TestCase):
    """The child is an inline `-c` program; its argv contract is a real interface."""

    def test_child_reads_a_json_envelope_not_bare_text(self):
        # The sidecar carries the prose block *and* the size ladder, because both
        # come from one computation and must describe the same portfolio.
        self.assertIn("json.loads(_raw)", agents._RUNNER)
        self.assertIn('kwargs["portfolio_data"] = PORTFOLIO_DATA', agents._RUNNER)

    def test_child_tolerates_a_bare_block_from_an_older_parent(self):
        # Losing the ladder costs the gate; losing the block would cost the feature.
        self.assertIn('_bundle = {"block": _raw, "ladder": {}}', agents._RUNNER)

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
        self.assertEqual(agents.build_portfolio_context("")["block"], "")
        self.assertEqual(agents.build_portfolio_context(None)["block"], "")

    def test_kill_switch_yields_empty(self):
        for value in ("0", "false", "no"):
            with mock.patch.dict("os.environ",
                                 {"AGENTS_PORTFOLIO_CONTEXT": value}):
                self.assertEqual(agents.build_portfolio_context("a@b.com")["block"], "")

    def test_store_outage_yields_empty_not_a_raise(self):
        # A run must not fail because a portfolio could not be read. The block is
        # an input to a judgement; losing it costs specificity, not correctness.
        from ystocker import portfolio

        with mock.patch.object(portfolio, "load",
                               side_effect=portfolio.StoreUnavailable("down")):
            self.assertEqual(agents.build_portfolio_context("a@b.com")["block"], "")

    def test_no_positions_yields_empty(self):
        from ystocker import portfolio

        with mock.patch.object(portfolio, "load", return_value=[]):
            self.assertEqual(agents.build_portfolio_context("a@b.com")["block"], "")

    def test_unexpected_error_yields_empty(self):
        from ystocker import portfolio

        with mock.patch.object(portfolio, "load",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(agents.build_portfolio_context("a@b.com")["block"], "")

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
            block = agents.build_portfolio_context("a@b.com")["block"]

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
            block = agents.build_portfolio_context("a@b.com")["block"]

        self.assertIn("<start_of_portfolio_constraints>", block)
        self.assertIn("no limits were stated", block)


class TestDisclosureGuards(unittest.TestCase):
    """The showcase never publishes what a portfolio-bearing run's decision
    seats wrote; sharing it explicitly is the account holder's own call to make.
    """

    def _job(self, **over):
        job = {"id": "j1", "user": "owner@example.com", "status": "done",
               "report": "# Report\n\nbody", "ticker": "NVDA", "lang": "en"}
        job.update(over)
        return job

    def test_showcase_lists_a_portfolio_run(self):
        # It used to be refused outright, and since every run by a holder with
        # positions carries a portfolio, that froze the public sample at the
        # day the feature shipped. Listing it is safe because the body is only
        # ever served through the withholding copy -- see TestShowcaseCopy.
        with mock.patch.object(agents, "showcase_enabled", return_value=True), \
             mock.patch.object(agents, "showcase_emails", return_value=set()):
            self.assertTrue(agents._is_showcase(self._job()))
            self.assertTrue(
                agents._is_showcase(self._job(portfolio_context=True)))

    def test_the_listing_never_carries_a_body_or_the_flag(self):
        listed = agents._publishable(self._job(portfolio_context=True,
                                               pm_levels={"position_size_pct": 20}))
        self.assertNotIn("report", listed)
        self.assertNotIn("portfolio_context", listed)
        self.assertNotIn("pm_levels", listed)

    def test_share_now_allows_a_portfolio_run(self):
        # Reversed from a hard refusal, at the account holder's own request --
        # see share.create()'s docstring for the full reasoning. This is the
        # owner sharing their own report by their own deliberate action, which
        # is a different shape of exposure from the showcase's automatic,
        # ownerless publication tested above -- that guard is unaffected by
        # this one changing.
        from ystocker import share

        table = mock.Mock()
        table.put_item.return_value = None
        with mock.patch.object(share, "_get_table", return_value=table):
            row = share.create(self._job(portfolio_context=True),
                               sharer="owner@example.com",
                               recipient="friend@example.com")
        self.assertIsNotNone(row)
        table.put_item.assert_called_once()

    def test_the_stored_row_never_carries_the_portfolio_flag(self):
        # Allowing the share must not start copying job-level fields into the
        # row beyond what create() already wrote before this change. The flag
        # was never a row field and still is not one, so it cannot leak through
        # a forwarded link either.
        from ystocker import share

        table = mock.Mock()
        table.put_item.return_value = None
        with mock.patch.object(share, "_get_table", return_value=table):
            row = share.create(self._job(portfolio_context=True),
                               sharer="owner@example.com",
                               recipient="friend@example.com")
        self.assertNotIn("portfolio_context", row)

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


class TestStructuredTail(unittest.TestCase):
    """The four fields a decision ledger needs, recorded on both completion paths.

    ``_reap`` settles a run whose supervising thread died and ``_run`` settles a
    normal one. A ledger wired into only one would silently lose every orphaned
    run — which are the long, expensive ones most worth recording.
    """

    PAYLOAD = {
        "ok": True, "decision": "Buy", "report": "# r",
        "pm_levels": {"rating": "Buy", "position_size_pct": 20.0, "flags": []},
        "gate_compliance": {"status": "violated", "violated": True,
                            "final_size_pct": 20.0, "approved_size_pct": 5.0,
                            "reasons": ["size_exceeds_approved"]},
        "trader_levels": {"action": "Buy", "reward_risk": 3.0},
        "risk_gate": {"verdict": "clamped", "approved_size_pct": 5.0},
    }

    def test_all_four_fields_are_copied(self):
        job = {}
        agents._record_structured(job, self.PAYLOAD)
        for key in ("pm_levels", "gate_compliance", "trader_levels", "risk_gate"):
            self.assertIn(key, job, key)

    def test_a_violation_is_lifted_to_a_top_level_flag(self):
        # Nobody should have to read a report to learn the answer.
        job = {}
        agents._record_structured(job, self.PAYLOAD)
        self.assertIs(job["risk_gate_violation"], True)
        self.assertEqual(job["risk_gate_status"], "violated")

    def test_unverified_is_not_recorded_as_a_violation(self):
        job = {}
        agents._record_structured(job, {**self.PAYLOAD, "gate_compliance": {
            "status": "unverifiable", "violated": False, "reasons": ["no_ruling"]}})
        self.assertIs(job["risk_gate_violation"], False)
        self.assertEqual(job["risk_gate_status"], "unverifiable")

    def test_compliant_is_recorded_as_no_violation(self):
        job = {}
        agents._record_structured(job, {**self.PAYLOAD, "gate_compliance": {
            "status": "compliant", "violated": False, "reasons": []}})
        self.assertIs(job["risk_gate_violation"], False)
        self.assertEqual(job["risk_gate_status"], "compliant")

    def test_empty_fields_are_not_written(self):
        # {} means the free-text path or an older TradingAgents; a key present and
        # empty would read as "recorded, and it said nothing".
        job = {}
        agents._record_structured(job, {"ok": True, "pm_levels": {},
                                        "gate_compliance": {}})
        self.assertNotIn("pm_levels", job)
        self.assertNotIn("risk_gate_violation", job)

    def test_non_dict_values_are_ignored(self):
        job = {}
        agents._record_structured(job, {"pm_levels": "oops",
                                        "gate_compliance": ["nope"]})
        self.assertEqual(job, {})

    def test_both_completion_paths_call_it(self):
        import inspect

        for fn in (agents._run, agents._reap):
            self.assertIn("_record_structured", inspect.getsource(fn),
                          f"{fn.__name__} does not record the structured tail")

    def test_ledger_fields_stay_out_of_the_public_allowlist(self):
        # gate_compliance carries sizes derived from the holder's portfolio.
        for key in ("pm_levels", "gate_compliance", "trader_levels", "risk_gate",
                    "risk_gate_violation"):
            self.assertNotIn(key, agents._PUBLIC_FIELDS, key)

    def test_child_emits_the_fields(self):
        for key in ("pm_levels", "gate_compliance", "trader_levels", "risk_gate"):
            self.assertIn(f'"{key}": _plain', agents._RUNNER, key)


# A report in the package's own shape (tradingagents/reporting.py): the title
# and stamp, then five numbered team blocks of ``### Seat`` turns. Every turn
# names its speaker twice -- once before and once after a subheading of its own,
# which must stay inside the turn -- so a test can say exactly who a copy kept.
_TEAMS = {
    "I. Analyst Team Reports": ["Market Analyst", "Sentiment Analyst",
                                "News Analyst", "Quality Analyst", "Valuation Analyst"],
    "II. Research Team Decision": ["Bull Researcher", "Bear Researcher",
                                   "Research Manager"],
    "III. Trading Team Plan": ["Trader"],
    "IV. Risk Management Team Decision": ["Aggressive Analyst",
                                          "Conservative Analyst", "Neutral Analyst"],
    "V. Portfolio Manager Decision": ["Portfolio Manager"],
}
_PUBLISHED = {s for team in list(_TEAMS)[:2] for s in _TEAMS[team]}
_WITHHELD = {s for team in list(_TEAMS)[2:] for s in _TEAMS[team]}


def _report(preamble_extra: str = "") -> str:
    lines = ["# Trading Analysis Report: NVDA", "", "Generated: 2026-09-21 19:16:50", ""]
    if preamble_extra:
        lines += [preamble_extra, ""]
    for team, seats in _TEAMS.items():
        lines += [f"## {team}", ""]
        for seat in seats:
            lines += [f"### {seat}", f"said-by<{seat}>", "",
                      "## 1. A subheading of its own", f"more-from<{seat}>", ""]
    return "\n".join(lines)


class TestShowcaseCopy(unittest.TestCase):
    """What a stranger may read of a run that was handed the holder's portfolio."""

    def test_keeps_every_seat_written_before_the_portfolio_was_read(self):
        copy = agents.portfolio_blind_report(_report(), "en")
        for seat in _PUBLISHED:
            self.assertIn(f"said-by<{seat}>", copy, seat)
            self.assertIn(f"more-from<{seat}>", copy, seat)

    def test_drops_every_seat_that_was_handed_it(self):
        copy = agents.portfolio_blind_report(_report(), "en")
        for seat in _WITHHELD:
            self.assertNotIn(f"<{seat}>", copy, seat)

    def test_reads_back_as_the_same_turns_under_the_same_teams(self):
        # The page and the PDF both split the copy with split_sections, so the
        # copy must re-parse into exactly the kept turns -- subheadings still
        # inside them, team dividers still above them.
        from ystocker.agent_roles import split_sections

        original = [s for s in split_sections(_report())
                    if s["role"] and s["role"]["key"] in agents._PORTFOLIO_BLIND_ROLES]
        copy = [s for s in split_sections(agents.portfolio_blind_report(_report()))
                if s["role"]]
        self.assertEqual([(s["role"]["key"], s["team"], s["body"]) for s in copy],
                         [(s["role"]["key"], s["team"], s["body"]) for s in original])

    def test_says_what_is_missing_in_the_reports_own_language(self):
        from ystocker.agent_roles import split_sections

        for lang, words in (("zh", "本公开版略去了"), ("en", "This public copy omits"),
                            ("", "This public copy omits")):
            with self.subTest(lang=lang):
                sections = split_sections(agents.portfolio_blind_report(_report(), lang))
                # In the preamble, which the page and the PDF both print.
                self.assertIsNone(sections[0]["role"])
                self.assertIn(words, sections[0]["body"])

    def test_the_preamble_keeps_only_its_title_and_stamp(self):
        copy = agents.portfolio_blind_report(
            _report(preamble_extra="Holder exposure: NVDA at least 6.2%"), "en")
        self.assertIn("# Trading Analysis Report: NVDA", copy)
        self.assertIn("Generated: 2026-09-21 19:16:50", copy)
        self.assertNotIn("6.2%", copy)

    def test_a_report_it_cannot_parse_publishes_none_of_its_body(self):
        # Fails closed: no role headings means no turns it can vouch for.
        copy = agents.portfolio_blind_report("Hold 12% of NVDA, cap is 10%.", "en")
        self.assertNotIn("12%", copy)
        self.assertIn("This public copy omits", copy)

    def _served(self, **over):
        job = {"id": "j1", "user": "owner@example.com", "status": "done",
               "ticker": "NVDA", "lang": "en", "decision": "Hold",
               "report": _report(), **over}
        with mock.patch.object(agents, "get_job", return_value=job), \
             mock.patch.object(agents, "showcase_enabled", return_value=True), \
             mock.patch.object(agents, "showcase_emails", return_value=set()):
            return agents.showcase_job("j1")

    def test_showcase_job_serves_a_portfolio_run_only_as_the_copy(self):
        out = self._served(portfolio_context=True)
        for seat in _WITHHELD:
            self.assertNotIn(f"<{seat}>", out["report"], seat)
        for seat in _PUBLISHED:
            self.assertIn(f"said-by<{seat}>", out["report"], seat)
        # The rating still travels: it is the one line the listing always had.
        self.assertEqual(out["decision"], "Hold")
        self.assertNotIn("portfolio_context", out)

    def test_showcase_job_serves_a_run_without_a_portfolio_whole(self):
        out = self._served()
        self.assertEqual(out["report"], _report())

    def test_both_public_routes_take_the_body_from_showcase_job(self):
        # Read, not imported: routes.py pulls in matplotlib, which this
        # checkout's Homebrew pyexpat cannot load. The JSON view and the PDF are
        # the only two ways a showcased body leaves the server, and the filter
        # lives in showcase_job -- a route that read get_job() itself would
        # serve the whole report.
        src = (Path(agents.__file__).parent / "routes.py").read_text(encoding="utf-8")
        for name in ("api_agents_showcase_job", "api_agents_showcase_job_pdf"):
            with self.subTest(route=name):
                body = re.search(rf"def {name}\(.*?(?=\n@bp\.route|\ndef |\Z)", src, re.S)
                self.assertIsNotNone(body, name)
                self.assertIn("showcase_job(job_id)", body.group(0))
                self.assertNotIn("get_job(", body.group(0))


class TestPortfolioWiring(unittest.TestCase):
    """Which seats are handed the holder's block, read out of TradingAgents.

    ``agents._PORTFOLIO_BLIND_ROLES`` is a claim about another repository's
    prompt wiring. A module that starts reading the portfolio -- a new seat, or
    an existing one rewired upstream -- has to fail here rather than publish.
    """

    # The agent modules that may read it, and the seat each one speaks as.
    SPEAKERS = {
        "trader/trader.py": "trader",
        "risk_mgmt/aggressive_debator.py": "aggressive",
        "risk_mgmt/conservative_debator.py": "conservative",
        "risk_mgmt/neutral_debator.py": "neutral",
        "managers/portfolio_manager.py": "portfolio",
    }
    # Where the block and the gate's ruling are defined, declared or computed.
    # None of these writes a turn of the report (tradingagents/reporting.py).
    PLUMBING = {"context.py", "state.py", "risk_mgmt/risk_gate.py"}
    READS = re.compile(r"get_portfolio_block|get_risk_gate_block|portfolio_context"
                       r"|portfolio_data|risk_gate")

    def test_only_the_withheld_seats_read_the_portfolio(self):
        root = Path(agents.TA_DIR) / "tradingagents" / "agents"
        if not root.is_dir():
            self.skipTest(f"no TradingAgents checkout at {agents.TA_DIR}")
        readers = {p.relative_to(root).as_posix() for p in root.rglob("*.py")
                   if self.READS.search(p.read_text(encoding="utf-8"))}
        self.assertTrue(readers, "the pattern found nothing; it has gone stale")
        self.assertEqual(readers - self.PLUMBING - set(self.SPEAKERS), set(),
                         "a module now reads the holder's portfolio: decide which "
                         "side of agents._PORTFOLIO_BLIND_ROLES its seat is on")

    def test_no_seat_that_reads_it_is_published(self):
        for key in self.SPEAKERS.values():
            self.assertNotIn(key, agents._PORTFOLIO_BLIND_ROLES, key)

    def test_every_published_seat_is_a_real_role_ahead_of_every_withheld_one(self):
        # Ahead in the cast's order matters as well as membership: a heading
        # split_sections does not recognise is folded into the turn before it,
        # and with every withheld seat last, anything unrecognised after the
        # Trader is folded into a withheld turn rather than a published one.
        from ystocker.agent_roles import ROLES

        order = [r["key"] for r in ROLES]
        self.assertLessEqual(agents._PORTFOLIO_BLIND_ROLES, set(order))
        self.assertLess(max(order.index(k) for k in agents._PORTFOLIO_BLIND_ROLES),
                        min(order.index(k) for k in self.SPEAKERS.values()))


class TestShowcaseSampling(unittest.TestCase):
    """The newest runs are always on the page; the rest is a stable sample."""

    def _pool(self, n):
        return [{"id": f"j{i:02d}"} for i in range(n)]    # newest first

    def _jobs(self, pool, **kw):
        with mock.patch.object(agents, "_showcase_pool", return_value=pool):
            return agents.showcase_jobs(**kw)

    def test_the_newest_runs_lead_the_list(self):
        pool = self._pool(40)
        got = self._jobs(pool)
        self.assertEqual(len(got), agents.SHOWCASE_SIZE)
        self.assertEqual(got[:agents.SHOWCASE_NEWEST], pool[:agents.SHOWCASE_NEWEST])

    def test_the_rest_is_sampled_in_order_without_repeats(self):
        pool = self._pool(40)
        idx = [pool.index(j) for j in self._jobs(pool)]
        self.assertEqual(idx, sorted(set(idx)))

    def test_the_sample_holds_still_within_the_hour(self):
        pool = self._pool(40)
        self.assertEqual(self._jobs(pool), self._jobs(pool))

    def test_a_small_pool_is_shown_whole(self):
        pool = self._pool(7)
        self.assertEqual(self._jobs(pool), pool)

    def test_a_small_limit_is_all_newest(self):
        pool = self._pool(40)
        self.assertEqual(self._jobs(pool, limit=2), pool[:2])
