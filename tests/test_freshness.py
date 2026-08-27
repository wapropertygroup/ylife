"""Freshness tests — no network, no Flask app.

Covers ystocker/freshness.py, whose job is to tell two different kinds of
staleness apart: how old *our fetch* is, and whether the *vendor* stopped
publishing.

The second one is the bug this module exists for. `fed.py` documents it: FRED's
`WASDRAL` and `MBST` kept answering HTTP 200 with well-formed CSV for years after
they stopped publishing, so nothing downstream could tell. The load-bearing tests
here are therefore the ones that a dead series is caught while a live series with
a long *publication lag* is not -- a detector that cried wolf on quarterly GDP
would be turned off within a week and catch nothing.

Both clocks are frozen where it matters. `classify_quote` reads `datetime.now`
*and* `time.time`, and mocking only one produces tests that pass for the wrong
reason.
"""

from __future__ import annotations

import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from ystocker import freshness as fr


def _weekly(n: int, end: date) -> list[str]:
    return [(end - timedelta(days=7 * i)).isoformat() for i in range(n)][::-1]


def _every(days: int, n: int, lag_days: int) -> list[str]:
    """*n* observations spaced *days* apart, the newest *lag_days* old."""
    today = date.today()
    return [(today - timedelta(days=days * i + lag_days)).isoformat()
            for i in range(n)][::-1]


class AgeTests(unittest.TestCase):
    def test_describe_age_reports_iso_and_label(self):
        meta = fr.describe_age(time.time() - 3600)
        self.assertEqual(meta["age_label"], "1h ago")
        self.assertAlmostEqual(meta["age_seconds"], 3600, delta=5)
        self.assertTrue(meta["fetched_at"].endswith("+00:00"))

    def test_missing_timestamp_is_stale_not_fresh(self):
        """An unknown age is not evidence of youth."""
        for bad in (None, 0, "", "nonsense"):
            meta = fr.describe_age(bad)
            self.assertIsNone(meta["age_seconds"], bad)
            self.assertTrue(meta["stale"], bad)

    def test_ttl_drives_the_stale_flag(self):
        self.assertFalse(fr.describe_age(time.time() - 10, ttl_seconds=60)["stale"])
        self.assertTrue(fr.describe_age(time.time() - 90, ttl_seconds=60)["stale"])

    def test_age_labels(self):
        self.assertEqual(fr.age_label(5), "just now")
        self.assertEqual(fr.age_label(90), "1m ago")
        self.assertEqual(fr.age_label(7200), "2h ago")
        self.assertEqual(fr.age_label(200000), "2d ago")
        self.assertEqual(fr.age_label(-5), "just now")      # clock skew


class QuoteClassificationTests(unittest.TestCase):
    """2026-08-26 is a Wednesday, 08-28 a Friday, 08-29/30 the weekend."""

    def _classify(self, now_et: datetime, age: timedelta) -> dict:
        quote_ts = (now_et - age).timestamp()
        now_ts = now_et.timestamp()

        class FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now_et.astimezone(tz) if tz else now_et

            @classmethod
            def fromtimestamp(cls, ts, tz=None):
                return datetime.fromtimestamp(ts, tz)

        with patch.object(fr, "datetime", FrozenDT), patch("time.time", lambda: now_ts):
            return fr.classify_quote(quote_ts)

    def _at(self, y, m, d, hh, mm) -> datetime:
        return datetime(y, m, d, hh, mm, tzinfo=fr._ET)

    def test_fresh_quote_during_session_is_realtime(self):
        r = self._classify(self._at(2026, 8, 26, 11, 0), timedelta(minutes=2))
        self.assertEqual(r["status"], "realtime")
        self.assertFalse(r["stale"])
        self.assertTrue(r["market_open"])

    def test_old_quote_during_session_is_stale(self):
        r = self._classify(self._at(2026, 8, 26, 11, 0), timedelta(minutes=45))
        self.assertEqual(r["status"], "stale")

    def test_after_close_the_last_print_is_session_close(self):
        r = self._classify(self._at(2026, 8, 26, 17, 0), timedelta(hours=1))
        self.assertEqual(r["status"], "session_close")
        self.assertFalse(r["stale"])

    def test_weekend_shows_fridays_close_not_stale(self):
        """The regression that makes a freshness badge worth trusting: without
        this every quote reads 'stale' all weekend and users learn to ignore it."""
        for day in (29, 30):                       # Saturday, Sunday
            r = self._classify(self._at(2026, 8, day, 10, 0),
                               timedelta(days=day - 28))
            self.assertEqual(r["status"], "session_close", f"2026-08-{day}")

    def test_monday_premarket_shows_fridays_close(self):
        r = self._classify(self._at(2026, 8, 31, 7, 0), timedelta(days=3))
        self.assertEqual(r["status"], "session_close")

    def test_genuinely_old_quote_is_stale_even_when_closed(self):
        r = self._classify(self._at(2026, 8, 31, 7, 0), timedelta(days=8))
        self.assertEqual(r["status"], "stale")

    def test_unknown_timestamp_is_stale(self):
        self.assertEqual(fr.classify_quote(None)["status"], "stale")


class CadenceTests(unittest.TestCase):
    def test_infers_weekly(self):
        self.assertEqual(fr.infer_cadence_days(_weekly(30, date(2026, 8, 20))), 7.0)

    def test_infers_monthly(self):
        self.assertAlmostEqual(fr.infer_cadence_days(_every(30, 20, 0)), 30.0, delta=1)

    def test_median_resists_one_odd_gap(self):
        """A single backfill gap must not drag the estimate the way a mean would."""
        dates = _weekly(20, date(2026, 8, 20))
        dates.insert(0, "2019-01-01")            # one ancient outlier
        self.assertEqual(fr.infer_cadence_days(dates), 7.0)

    def test_too_few_observations_yields_none(self):
        self.assertIsNone(fr.infer_cadence_days(["2026-08-01"]))
        self.assertIsNone(fr.infer_cadence_days([]))

    def test_accepts_dates_and_datetimes_not_just_strings(self):
        """series_health parses once and passes `date` objects straight in; a
        string-only parser silently returned None for every one of them."""
        objs = [date(2026, 8, 1) + timedelta(days=7 * i) for i in range(10)]
        self.assertEqual(fr.infer_cadence_days(objs), 7.0)
        stamps = [datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=7 * i)
                  for i in range(10)]
        self.assertEqual(fr.infer_cadence_days(stamps), 7.0)


class SeriesHealthTests(unittest.TestCase):
    def test_live_weekly_series_is_healthy(self):
        h = fr.series_health(_weekly(60, date.today() - timedelta(days=3)))
        self.assertFalse(h["stale"])
        self.assertEqual(h["cadence_days"], 7.0)
        self.assertEqual(h["observation_count"], 60)

    def test_dead_weekly_series_is_caught(self):
        """WASDRAL: weekly, last published 2018, still serving HTTP 200."""
        h = fr.series_health(_weekly(300, date(2018, 6, 27)), label="WASDRAL")
        self.assertTrue(h["stale"])
        self.assertEqual(h["last_observation"], "2018-06-27")
        self.assertGreater(h["lag_days"], 2000)

    def test_quarterly_series_with_long_publication_lag_stays_healthy(self):
        """GDP trails ~4 months by design; flagging it would make this useless."""
        h = fr.series_health(_every(91, 20, 120), label="GDP")
        self.assertFalse(h["stale"])

    def test_monthly_series_two_months_late_is_healthy_three_years_is_not(self):
        self.assertFalse(fr.series_health(_every(30, 24, 60))["stale"])
        self.assertTrue(fr.series_health(_every(30, 24, 1095))["stale"])

    def test_unknown_when_cadence_cannot_be_inferred(self):
        """None, not False -- 'cannot tell' must not read as 'healthy'."""
        h = fr.series_health([date.today().isoformat()])
        self.assertIsNone(h["stale"])
        self.assertIsNone(h["cadence_days"])

    def test_empty_series_is_stale(self):
        h = fr.series_health([])
        self.assertTrue(h["stale"])
        self.assertIsNone(h["last_observation"])
        self.assertEqual(h["observation_count"], 0)

    def test_unparseable_dates_are_ignored(self):
        h = fr.series_health(["not-a-date", None, 42,
                              *_weekly(10, date.today() - timedelta(days=2))])
        self.assertEqual(h["observation_count"], 10)
        self.assertFalse(h["stale"])


class AnnotateTests(unittest.TestCase):
    def _payload(self):
        return {
            "WALCL": {"dates": _weekly(60, date.today() - timedelta(days=3))},
            "WASDRAL": {"dates": _weekly(300, date(2018, 6, 27))},
            "BROKEN": {"dates": [], "error": True},
            "junk": "not a dict",
        }

    def test_annotates_each_series_and_skips_non_dicts(self):
        health = fr.annotate_series(self._payload())
        self.assertEqual(set(health), {"WALCL", "WASDRAL", "BROKEN"})

    def test_failed_fetch_is_flagged_without_cadence_inference(self):
        """A failed fetch says nothing about the vendor's schedule."""
        h = fr.annotate_series(self._payload())["BROKEN"]
        self.assertTrue(h["stale"])
        self.assertTrue(h["fetch_failed"])
        self.assertIsNone(h["cadence_days"])

    def test_stale_series_ids_is_sorted_and_excludes_unknown(self):
        health = fr.annotate_series({
            **self._payload(),
            "UNKNOWN": {"dates": [date.today().isoformat()]},
        })
        self.assertEqual(fr.stale_series_ids(health), ["BROKEN", "WASDRAL"])

    def test_handles_empty_and_none_input(self):
        self.assertEqual(fr.annotate_series({}), {})
        self.assertEqual(fr.annotate_series(None), {})
        self.assertEqual(fr.stale_series_ids({}), [])
        self.assertEqual(fr.stale_series_ids(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
