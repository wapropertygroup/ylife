"""``agents.ticker_runs`` -- the TradeAgents card on /history/<ticker>.

Written for the NBIS page on 2026-10-01. Its one finished run (08-27) sat behind
182 newer records, and the window every other agent listing reads through
(``_records``' MAX_JOBS, the 60 newest across *all* tickers) never reached it.
So the card would have said "no analysis yet" about a ticker that had one -- the
first test pins the wider read.

The rest pin the card's contract: an exact ticker (not a search's prefix), every
status (a running job is news), the owner-or-VIP rule, an allowlist rather than
the record, and the Portfolio Manager's turn attached once, not per row.

No app, no AWS: ``_records`` is replaced with a list.
"""
from __future__ import annotations

import unittest

from ystocker import agents

REPORT = """# Trading Analysis Report: NBIS

## V. Portfolio Manager Decision

### Portfolio Manager
**Rating: Underweight** — trim into strength.
"""


def _job(i, ticker="NBIS", user="owner@example.com", status="done", report=REPORT,
         decision="Underweight", **extra):
    job = {
        "id": f"{i:016x}", "ticker": ticker, "user": user, "status": status,
        "date": "2026-08-27", "decision": decision, "report": report,
        "created_at": f"2026-08-{27 - i % 20:02d}T14:03:11+00:00",
        "finished_at": "2026-08-27T14:30:00+00:00", "elapsed_sec": 1500,
        "lang": "zh", "pid": 754431, "log": "stderr…", "quota_day": "2026-08-27",
        "chat": [{"role": "user", "text": "private follow-up"}],
        "portfolio_context": True,
    }
    job.update(extra)
    return job


class TickerRunsTests(unittest.TestCase):
    def setUp(self):
        self._records = agents._records
        self.calls = []
        self.jobs = []

        def fake(*, user=None, status=None, limit=agents.MAX_JOBS):
            self.calls.append({"user": user, "limit": limit})
            return list(self.jobs)

        agents._records = fake

    def tearDown(self):
        agents._records = self._records

    def runs(self, user="owner@example.com", all_users=False, **kw):
        return agents.ticker_runs("nbis", user=user, all_users=all_users, **kw)

    def test_it_reads_past_the_sixty_run_window(self):
        self.runs()
        self.assertGreater(self.calls[0]["limit"], agents.MAX_JOBS,
                           "an older run of this ticker would be invisible")

    def test_exact_ticker_only(self):
        self.jobs = [_job(1, ticker="NBISX"), _job(2, ticker="NBI"), _job(3)]
        out = self.runs()
        self.assertEqual([r["ticker"] for r in out["runs"]], ["NBIS"])
        self.assertEqual(out["found"], 1)

    def test_every_status_is_kept(self):
        self.jobs = [_job(1, status="running", report=None, decision=None),
                     _job(2, status="error", report=None, decision=None), _job(3)]
        self.assertEqual([r["status"] for r in self.runs()["runs"]],
                         ["running", "error", "done"])

    def test_a_reader_sees_only_their_own_runs(self):
        self.jobs = [_job(1, user="someone@else.com"), _job(2), _job(3, user="")]
        out = self.runs()
        self.assertEqual([r["id"] for r in out["runs"]], [_job(2)["id"]])
        self.assertTrue(out["runs"][0]["mine"])
        self.assertEqual(self.calls[0]["user"], "owner@example.com")

    def test_no_viewer_means_no_runs(self):
        self.jobs = [_job(1)]
        self.assertEqual(self.runs(user=None)["runs"], [])

    def test_a_vip_sees_everyones_with_the_owner_masked(self):
        self.jobs = [_job(1, user="someone@else.com"), _job(2)]
        out = self.runs(all_users=True)
        self.assertEqual(len(out["runs"]), 2)
        theirs = out["runs"][0]
        self.assertFalse(theirs["mine"])
        self.assertEqual(theirs["owner"], "someone@…")
        self.assertNotIn("owner", out["runs"][1])
        self.assertIsNone(self.calls[0]["user"])

    def test_the_entry_is_an_allowlist_not_the_record(self):
        self.jobs = [_job(1)]
        entry = self.runs()["runs"][0]
        for private in ("user", "pid", "log", "chat", "report", "quota_day",
                        "portfolio_context"):
            self.assertNotIn(private, entry)
        for public in ("id", "ticker", "date", "status", "decision", "lang",
                       "has_report", "tone", "mine"):
            self.assertIn(public, entry)

    def test_the_rating_carries_the_pdfs_tone(self):
        self.jobs = [_job(1, decision="Underweight"), _job(2, decision="**Buy**"),
                     _job(3, decision="Hold"), _job(4, decision=None, status="error")]
        self.assertEqual([r["tone"] for r in self.runs()["runs"]],
                         ["sell", "buy", "hold", ""])

    def test_only_the_newest_readable_run_carries_the_manager(self):
        self.jobs = [_job(1, status="running", report=None, decision=None),
                     _job(2), _job(3)]
        runs = self.runs()["runs"]
        self.assertNotIn("portfolio", runs[0])
        self.assertEqual(runs[1]["portfolio"]["key"], "portfolio")
        self.assertIn("Underweight", runs[1]["portfolio"]["body"])
        self.assertNotIn("portfolio", runs[2])

    def test_found_counts_past_the_limit(self):
        self.jobs = [_job(i) for i in range(1, 15)]
        out = self.runs(limit=10)
        self.assertEqual(len(out["runs"]), 10)
        self.assertEqual(out["found"], 14)


if __name__ == "__main__":
    unittest.main()
