"""Search-hit shaping tests: the empty-report skip and the inlined PM turn.

``ystocker.agents`` is imported directly rather than through ``create_app()`` --
the app factory pulls in ``routes`` and therefore matplotlib, which a test of
list filtering has no use for.

``_records`` is monkeypatched throughout. What is under test is how ``search_jobs``
filters and shapes hits, not whether DynamoDB and the local cache merge correctly,
and a test that reached the real store would depend on whatever the developer
happens to have run today.
"""

import unittest

from ystocker import agents


def _job(job_id, ticker, status, report="", **extra):
    job = {
        "id": job_id, "ticker": ticker, "status": status, "report": report,
        "user": "owner@example.com", "date": "2026-08-14",
        "created_at": "2026-08-14T00:00:00Z", "log": "runner noise",
    }
    job.update(extra)
    return job


# A report shaped like the real thing: a team divider, an analyst, then the
# decision. The Portfolio Manager's body carries its own lower-level heading,
# which must stay attached to the turn rather than start a new one.
REPORT = """# Trading Analysis Report: NVDA

## I. Analyst Team Reports

### Market Analyst
RSI 54, MACD turning up.

## IV. Portfolio Manager Decision

### Portfolio Manager
FINAL TRANSACTION PROPOSAL: **BUY**

#### 1. Sizing
2% of book, stop at 168.
"""

# What a self-test writes: a body with no role headings anywhere in it.
FIXTURE = "## Self-test\n\nSubprocess and JSON parsing exercised.\n"


class PortfolioSectionTests(unittest.TestCase):
    def test_absent_for_no_report_and_for_a_roleless_fixture(self):
        # All three are ordinary states, not errors: nothing has run yet, and a
        # self-test makes no LLM call so its report names no speaker.
        self.assertIsNone(agents.portfolio_section(""))
        self.assertIsNone(agents.portfolio_section(None))
        self.assertIsNone(agents.portfolio_section(FIXTURE))

    def test_carries_role_metadata_so_callers_need_no_second_cast(self):
        pm = agents.portfolio_section(REPORT)
        self.assertEqual(pm["key"], "portfolio")
        self.assertEqual(pm["name"], "Portfolio Manager")
        self.assertEqual(pm["zh"], "投资组合经理")
        self.assertEqual(pm["team_zh"], "投资组合经理决策")
        self.assertFalse(pm["truncated"])

    def test_body_keeps_the_models_own_subheadings(self):
        body = agents.portfolio_section(REPORT)["body"]
        self.assertIn("FINAL TRANSACTION PROPOSAL", body)
        self.assertIn("#### 1. Sizing", body)
        # The analyst above it is a different speaker's turn.
        self.assertNotIn("RSI 54", body)

    def test_renamed_upstream_role_still_resolves(self):
        # "Risk Manager" is the historical spelling; agent_roles aliases it.
        pm = agents.portfolio_section("# R\n\n### Risk Manager\nCall it a buy.\n")
        self.assertIsNotNone(pm)
        self.assertEqual(pm["key"], "portfolio")

    def test_long_section_is_clipped_and_says_so(self):
        pm = agents.portfolio_section(
            "# R\n\n### Portfolio Manager\n" + "x" * (agents._PM_MAX_CHARS + 500))
        self.assertEqual(len(pm["body"]), agents._PM_MAX_CHARS)
        self.assertTrue(pm["truncated"])


class SearchSkipTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            _job("a", "NVDA", "done", REPORT),
            _job("b", "NVDA", "error"),                 # failed: no report
            _job("c", "NVDA", "queued"),                # not started: no report
            _job("d", "NVDA", "done", FIXTURE, selftest=True),
            _job("e", "AMD", "done", REPORT),
        ]
        real = agents._records
        agents._records = lambda **kw: list(self.records)
        self.addCleanup(setattr, agents, "_records", real)

    def search(self, query="", **kw):
        return agents.search_jobs(query, user="owner@example.com", **kw)

    def test_default_keeps_every_row_and_adds_nothing(self):
        # Off by default so list_jobs and any other caller are unaffected.
        res = self.search()
        self.assertEqual(res["found"], 5)
        self.assertEqual(res["skipped_empty"], 0)
        self.assertFalse(any("portfolio" in j for j in res["jobs"]))

    def test_require_report_drops_only_the_unreadable_rows(self):
        res = self.search("NVDA", require_report=True)
        # The errored and queued runs go; the self-test stays, because it has a
        # body a reader can open even though no role speaks in it.
        self.assertEqual([j["id"] for j in res["jobs"]], ["a", "d"])
        self.assertEqual(res["skipped_empty"], 2)

    def test_hidden_rows_are_counted_even_when_none_survive(self):
        # The case the count exists for: every match was unreadable, so "0 found"
        # alone would read as "you never ran that ticker".
        self.records = [_job("b", "NVDA", "error")]
        res = self.search("NVDA", require_report=True)
        self.assertEqual(res["found"], 0)
        self.assertEqual(res["skipped_empty"], 1)

    def test_count_ignores_rows_that_never_matched_the_query(self):
        # AMD is readable but irrelevant; it must not inflate the hidden count.
        res = self.search("NVDA", require_report=True)
        self.assertEqual(res["skipped_empty"], 2)

    def test_portfolio_attached_per_hit_and_null_where_absent(self):
        by_id = {j["id"]: j for j in self.search(with_portfolio=True)["jobs"]}
        self.assertEqual(by_id["a"]["portfolio"]["zh"], "投资组合经理")
        # Present-but-null rather than missing, so the UI branches on one shape.
        for job_id in ("b", "c", "d"):
            self.assertIn("portfolio", by_id[job_id])
            self.assertIsNone(by_id[job_id]["portfolio"])

    def test_absent_key_and_null_value_mean_different_things(self):
        # The frontend leans on this: a missing key is "the index did not ask for
        # the turn", a null value is "asked, and this report has none". Conflating
        # them captions every row of the unfiltered index with a note claiming its
        # report has no decision section.
        index = {j["id"]: j for j in self.search()["jobs"]}
        searched = {j["id"]: j for j in self.search(with_portfolio=True)["jobs"]}
        self.assertNotIn("portfolio", index["a"])
        self.assertNotIn("portfolio", index["d"])
        self.assertIsNotNone(searched["a"]["portfolio"])
        self.assertIsNone(searched["d"]["portfolio"])

    def test_listing_never_carries_the_report_or_the_transcript(self):
        # The PM turn is the only part of the body that may travel with a hit.
        for job in self.search(with_portfolio=True)["jobs"]:
            self.assertNotIn("report", job)
            self.assertNotIn("log", job)
            self.assertIn("has_report", job)

    def test_extracting_the_turn_does_not_mutate_the_stored_record(self):
        self.search(with_portfolio=True)
        self.assertEqual(self.records[0]["report"], REPORT)
        self.assertIn("log", self.records[0])


if __name__ == "__main__":
    unittest.main()
