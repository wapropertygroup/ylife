"""Tests for :mod:`ystocker.earnings`.

The selection rule is the whole module: pick the earliest candidate still in the
future, from whichever field happens to hold it. Everything here is a case where
trusting the field name, or a unit, would produce a calendar that is quietly
wrong rather than visibly broken.

Pure: no app, no network, no clock.
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import earnings  # noqa: E402

TODAY = dt.date(2026, 9, 19)


def ts(date_str: str) -> int:
    """Unix seconds for a date, as Yahoo sends them."""
    d = dt.date.fromisoformat(date_str)
    return int(dt.datetime(d.year, d.month, d.day, 20, 0,
                           tzinfo=dt.timezone.utc).timestamp())


class NextEarningsTests(unittest.TestCase):
    def test_the_real_msft_shape(self):
        """Measured on 2026-09-19. `earningsTimestamp` is the *last* report and
        `earningsTimestampStart` the next — reading either by name gets it wrong
        for some ticker."""
        info = {
            "earningsTimestamp": ts("2026-07-29"),
            "earningsTimestampStart": ts("2026-10-28"),
            "earningsCallTimestampStart": ts("2026-07-29"),
        }
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-10-28")

    def test_the_earliest_future_date_wins_whatever_field_holds_it(self):
        """The reverse arrangement — the sooner date under the name that sounds
        like a past one. A key-based rule returns the wrong date here."""
        info = {
            "earningsTimestampStart": ts("2026-12-01"),
            "earningsTimestamp": ts("2026-10-02"),
        }
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-10-02")

    def test_past_dates_are_never_returned(self):
        info = {"earningsTimestamp": ts("2026-07-29"),
                "earningsCallTimestampStart": ts("2026-06-01")}
        self.assertIsNone(earnings.next_earnings(info, today=TODAY))

    def test_today_counts_as_upcoming(self):
        """A company reporting this morning has not stopped being today's news."""
        info = {"earningsTimestampStart": ts("2026-09-19")}
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-09-19")

    def test_no_fields_yields_none(self):
        self.assertIsNone(earnings.next_earnings({}, today=TODAY))

    def test_a_list_of_timestamps_is_accepted(self):
        info = {"earningsTimestamps": [ts("2026-07-01"), ts("2026-11-04")]}
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-11-04")

    def test_iso_strings_are_accepted(self):
        info = {"earningsDate": "2026-10-15"}
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-10-15")

    def test_date_and_datetime_objects_are_accepted(self):
        self.assertEqual(
            earnings.next_earnings({"earningsDate": dt.date(2026, 10, 9)}, today=TODAY),
            "2026-10-09")
        self.assertEqual(
            earnings.next_earnings(
                {"earningsDate": dt.datetime(2026, 10, 9, 13, 30)}, today=TODAY),
            "2026-10-09")

    def test_millisecond_timestamps_are_rescaled(self):
        """A millisecond value read as seconds lands in the year 58000 and sits
        at the top of a calendar for ever, sorted first."""
        info = {"earningsTimestampStart": ts("2026-10-28") * 1000}
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-10-28")

    def test_zero_and_negative_timestamps_are_ignored(self):
        """A 0 resolves to 1970, which is merely discarded as past — but it must
        not crash, and a negative must not resolve to something plausible."""
        info = {"earningsTimestamp": 0, "earningsTimestampStart": -1,
                "earningsDate": ts("2026-10-20")}
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-10-20")

    def test_junk_is_skipped_not_crashed(self):
        """Runs over ~308 tickers including ADRs and foreign listings."""
        info = {"earningsDate": "not a date", "earningsTimestamp": "n/a",
                "earningsTimestampStart": ts("2026-10-20")}
        self.assertEqual(earnings.next_earnings(info, today=TODAY), "2026-10-20")

    def test_booleans_are_not_treated_as_timestamps(self):
        """`True` is an int in Python and would resolve to 1970-01-01."""
        self.assertIsNone(earnings.next_earnings(
            {"earningsTimestamp": True}, today=TODAY))


class UpcomingTests(unittest.TestCase):
    def _rec(self, date_str, cap=100.0, name=None):
        return {"Earnings Date": date_str, "Name": name, "Market Cap ($B)": cap}

    def test_sorted_by_date_then_size(self):
        """Within a day the biggest reporter leads, rather than whichever ticker
        sorted first alphabetically."""
        rows = earnings.upcoming([
            ("ZZZ", self._rec("2026-09-25", cap=3000.0)),
            ("AAA", self._rec("2026-09-25", cap=10.0)),
            ("MMM", self._rec("2026-09-22", cap=50.0)),
        ], today=TODAY)
        self.assertEqual([r["ticker"] for r in rows], ["MMM", "ZZZ", "AAA"])

    def test_the_horizon_is_respected_at_both_ends(self):
        rows = earnings.upcoming([
            ("PAST", self._rec("2026-09-18")),
            ("EDGE", self._rec("2026-10-10")),      # exactly 21 days
            ("FAR",  self._rec("2026-10-11")),
        ], within_days=21, today=TODAY)
        self.assertEqual([r["ticker"] for r in rows], ["EDGE"])

    def test_days_away_is_computed(self):
        rows = earnings.upcoming([("X", self._rec("2026-09-22"))], today=TODAY)
        self.assertEqual(rows[0]["days_away"], 3)

    def test_missing_or_malformed_dates_are_skipped(self):
        rows = earnings.upcoming([
            ("A", {"Name": "A"}),
            ("B", self._rec("garbage")),
            ("C", self._rec("2026-09-21")),
        ], today=TODAY)
        self.assertEqual([r["ticker"] for r in rows], ["C"])

    def test_the_limit_is_applied_after_sorting(self):
        """Truncating before the sort would return an arbitrary subset rather
        than the soonest."""
        recs = [(f"T{i}", self._rec("2026-09-%02d" % (20 + i % 5))) for i in range(30)]
        rows = earnings.upcoming(recs, today=TODAY, limit=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["date"] == "2026-09-20" for r in rows))

    def test_name_falls_back_to_the_ticker(self):
        rows = earnings.upcoming([("X", self._rec("2026-09-21", name=None))], today=TODAY)
        self.assertEqual(rows[0]["name"], "X")

    def test_a_missing_market_cap_does_not_break_the_sort(self):
        rows = earnings.upcoming([
            ("A", {"Earnings Date": "2026-09-21", "Market Cap ($B)": None}),
            ("B", self._rec("2026-09-21", cap=5.0)),
        ], today=TODAY)
        self.assertEqual([r["ticker"] for r in rows], ["B", "A"])


if __name__ == "__main__":
    unittest.main()
