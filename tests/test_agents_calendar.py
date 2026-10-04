"""``agents.calendar_runs`` and ``agents.rating_level`` -- /agents' decision calendar.

Asked for on 2026-10-04: a calendar on the run page, "so a reader can see the
same ticker's decision on each day". What these pin:

* the rating keeps all five steps of the scale the reports use. Overweight ->
  Buy is the change a ticker's calendar exists to show, and report_pdf's
  three-tone fold cannot show it;
* the first rating word in the text wins ("Hold -- upgrade to Buy above $210"
  is a Hold), and no rating is ``None``, never a Hold;
* a run is placed by the trade date it analysed, and a record without one is
  left out rather than guessed;
* the privacy rule every agent read follows: the viewer's own runs, a VIP
  everyone's with the owner masked, and no viewer means no runs. The calendar
  reads past the 60-run window every listing uses, or an older run would be
  missing from a month it belongs in.

No app, no AWS: ``_records`` is replaced with a list.
"""
from __future__ import annotations

import unittest

from ystocker import agents


def _job(i, ticker="MSFT", user="owner@example.com", date="2026-10-02",
         decision="Underweight", status="done", **extra):
    job = {"id": f"{i:016x}", "ticker": ticker, "user": user, "date": date,
           "decision": decision, "status": status,
           "created_at": f"2026-10-0{1 + i % 8}T10:00:00+00:00",
           "report": "x" * 50, "log": "stderr", "pid": 4242, "chat": [{"text": "private"}]}
    job.update(extra)
    return job


class RatingLevelTests(unittest.TestCase):
    def test_the_five_steps(self):
        for text, level in (("Buy", 2), ("Overweight", 1), ("Hold", 0), ("Underweight", -1),
                            ("Sell", -2), ("Strong Buy", 2), ("Strong Sell", -2)):
            self.assertEqual(agents.rating_level(text), level, text)

    def test_the_chinese_reports(self):
        for text, level in (("买入", 2), ("增持", 1), ("持有", 0), ("减持", -1), ("卖出", -2)):
            self.assertEqual(agents.rating_level(text), level, text)

    def test_the_first_rating_word_wins(self):
        self.assertEqual(agents.rating_level("Hold -- upgrade to Buy above $210"), 0)
        self.assertEqual(agents.rating_level("Neutral, short-term risk"), 0)
        self.assertEqual(agents.rating_level("Underweight"), -1)      # not "weight"-anything else

    def test_no_rating_is_none_not_hold(self):
        for text in ("", "   ", None, "pending", "see report"):
            self.assertIsNone(agents.rating_level(text), repr(text))


class CalendarRunsTests(unittest.TestCase):
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

    def test_own_runs_compact_and_rated(self):
        self.jobs = [_job(1), _job(2, ticker="nflx", decision="Overweight", date="2026-09-15")]
        out = agents.calendar_runs("owner@example.com")
        self.assertEqual([r["ticker"] for r in out["runs"]], ["MSFT", "NFLX"])
        first = out["runs"][0]
        self.assertEqual((first["date"], first["level"], first["decision"], first["mine"]),
                         ("2026-10-02", -1, "Underweight", True))
        for private in ("report", "log", "pid", "chat", "user"):
            self.assertNotIn(private, first, private)

    def test_it_reads_past_the_sixty_run_window(self):
        agents.calendar_runs("owner@example.com")
        self.assertGreater(self.calls[0]["limit"], agents.MAX_JOBS)

    def test_someone_elses_runs_are_not_listed(self):
        self.jobs = [_job(1), _job(2, user="other@example.com")]
        out = agents.calendar_runs("owner@example.com")
        self.assertEqual(len(out["runs"]), 1)

    def test_a_vip_sees_everyone_with_owners_masked(self):
        self.jobs = [_job(1), _job(2, user="other@example.com")]
        out = agents.calendar_runs("owner@example.com", all_users=True)
        theirs = [r for r in out["runs"] if not r["mine"]][0]
        self.assertNotIn("other@example.com", str(theirs))
        self.assertTrue(theirs["owner"].startswith("other"))
        self.assertIsNone(self.calls[0]["user"])

    def test_no_viewer_no_runs(self):
        self.jobs = [_job(1)]
        self.assertEqual(agents.calendar_runs(None), {"runs": [], "tickers": []})

    def test_a_run_without_a_trade_date_is_left_out(self):
        self.jobs = [_job(1, date=""), _job(2, date="yesterday"), _job(3)]
        self.assertEqual(len(agents.calendar_runs("owner@example.com")["runs"]), 1)

    def test_unrated_runs_stay_unrated(self):
        self.jobs = [_job(1, status="running", decision=""), _job(2, status="error", decision="")]
        self.assertEqual([r["level"] for r in agents.calendar_runs("owner@example.com")["runs"]],
                         [None, None])

    def test_tickers_are_counted_busiest_first(self):
        self.jobs = [_job(1, ticker="NFLX"), _job(2), _job(3), _job(4, ticker="AMZN")]
        tickers = agents.calendar_runs("owner@example.com")["tickers"]
        self.assertEqual(tickers, [{"ticker": "MSFT", "runs": 2}, {"ticker": "AMZN", "runs": 1},
                                   {"ticker": "NFLX", "runs": 1}])

    def test_the_cap(self):
        self.jobs = [_job(i) for i in range(30)]
        self.assertEqual(len(agents.calendar_runs("owner@example.com", limit=10)["runs"]), 10)


if __name__ == "__main__":
    unittest.main()
