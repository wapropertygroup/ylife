"""
Unit tests for the FedWatch history layer — no app, no network, no AWS.

Two separate things are under test and they must not be confused, because the
whole design rests on the distinction:

  * ``_rate_change_points`` compresses the *realised* target range, which FRED
    returns in full on every call. This is cache. It must never lose a move and
    must never invent one.
  * ``record_snapshot`` / ``history`` persist the *market's expectations*, which
    nothing upstream will sell back. This is an observed series. It must be
    idempotent per date, must reject junk rather than store it, and must survive
    a DynamoDB outage by falling back to disk.

DynamoDB is never reached: ``_hist_unavail_until`` is pushed to infinity so
``_get_hist_table`` short-circuits before importing boto3, which is also exactly
what happens in local dev with no credentials.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from ystocker import fedwatch as fw


class RateChangePointsTest(unittest.TestCase):
    """Compressing the daily FRED series into one row per move."""

    def test_collapses_runs_of_identical_days(self):
        lower = [("2020-01-01", 1.5), ("2020-01-02", 1.5), ("2020-01-03", 1.5)]
        upper = [("2020-01-01", 1.75), ("2020-01-02", 1.75), ("2020-01-03", 1.75)]
        pts = fw._rate_change_points(lower, upper)
        # One change point, plus the trailing anchor that gives the step length.
        self.assertEqual([p["date"] for p in pts], ["2020-01-01", "2020-01-03"])
        self.assertEqual(pts[0]["lower"], 1.5)
        self.assertEqual(pts[0]["upper"], 1.75)

    def test_emits_a_point_per_actual_move(self):
        days = ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"]
        lows = [1.5, 1.5, 1.0, 1.0]
        highs = [1.75, 1.75, 1.25, 1.25]
        pts = fw._rate_change_points(list(zip(days, lows)), list(zip(days, highs)))
        self.assertEqual([p["date"] for p in pts],
                         ["2020-01-01", "2020-01-03", "2020-01-04"])
        self.assertEqual(pts[1]["lower"], 1.0)

    def test_trailing_anchor_is_not_duplicated_when_last_day_is_a_move(self):
        days = ["2020-01-01", "2020-01-02"]
        pts = fw._rate_change_points(
            list(zip(days, [1.0, 2.0])), list(zip(days, [1.25, 2.25])))
        self.assertEqual([p["date"] for p in pts], ["2020-01-01", "2020-01-02"])
        # No third row repeating the final observation.
        self.assertEqual(len(pts), 2)

    def test_legacy_single_target_closes_the_band(self):
        """Pre-2008 DFEDTAR has no range, so lower == upper and it draws flat."""
        pts = fw._rate_change_points(
            [("2008-12-16", 0.0)], [("2008-12-16", 0.25)],
            legacy=[("1990-01-01", 8.25), ("2008-12-15", 1.0)])
        self.assertEqual([p["date"] for p in pts],
                         ["1990-01-01", "2008-12-15", "2008-12-16"])
        self.assertEqual(pts[0]["lower"], pts[0]["upper"])
        self.assertEqual(pts[0]["lower"], 8.25)

    def test_range_series_wins_over_legacy_on_a_shared_date(self):
        """DFEDTAR lingers as a discontinued series; the range must take priority."""
        pts = fw._rate_change_points(
            [("2010-01-01", 0.0)], [("2010-01-01", 0.25)],
            legacy=[("2010-01-01", 99.0)])
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["upper"], 0.25)
        self.assertNotEqual(pts[0]["lower"], 99.0)

    def test_unpaired_bound_is_dropped_not_half_stored(self):
        """An upper with no matching lower would otherwise make a nonsense band."""
        self.assertEqual(fw._rate_change_points([], [("2020-01-01", 1.0)]), [])
        self.assertEqual(fw._rate_change_points([("2020-01-01", 1.0)], []), [])

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(fw._rate_change_points([], []), [])
        self.assertEqual(fw._rate_change_points([], [], legacy=[]), [])


class FredSeriesParseTest(unittest.TestCase):
    """The CSV parse that replaced ``_latest_fred_value``'s single-row read."""

    def _parse(self, text):
        """Drive _fred_series with a stubbed HTTP response."""
        class _Resp:
            def __init__(self, t): self.text = t
            def raise_for_status(self): pass

        real = fw._SESSION.get
        fw._SESSION.get = lambda *a, **k: _Resp(text)
        try:
            return fw._fred_series("TEST")
        finally:
            fw._SESSION.get = real

    def test_parses_rows_and_skips_the_header(self):
        rows = self._parse(
            "observation_date,DFEDTARU\n2020-01-01,1.75\n2020-01-02,1.50\n")
        self.assertEqual(rows, [("2020-01-01", 1.75), ("2020-01-02", 1.5)])

    def test_skips_missing_value_markers(self):
        """FRED writes '.' for a gap; float('.') would raise and lose the series."""
        rows = self._parse(
            "observation_date,X\n2020-01-01,.\n2020-01-02,ND\n2020-01-03,2.0\n")
        self.assertEqual(rows, [("2020-01-03", 2.0)])

    def test_rejects_a_non_iso_first_column(self):
        rows = self._parse("observation_date,X\nnot-a-date,1.0\n2020-01-01,2.0\n")
        self.assertEqual(rows, [("2020-01-01", 2.0)])

    def test_latest_value_still_reads_the_last_row(self):
        class _Resp:
            text = "observation_date,X\n2020-01-01,1.0\n2020-01-02,3.5\n"
            def raise_for_status(self): pass

        real = fw._SESSION.get
        fw._SESSION.get = lambda *a, **k: _Resp()
        try:
            self.assertEqual(fw._latest_fred_value("X"), 3.5)
        finally:
            fw._SESSION.get = real


class _DiskOnlyHistoryCase(unittest.TestCase):
    """Base case that isolates the history store to a temp dir, DynamoDB off."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._path, self._unavail = fw._HIST_PATH, fw._hist_unavail_until
        fw._HIST_PATH = self._tmp / "fedwatch_history.json"
        # Short-circuits _get_hist_table before it imports boto3.
        fw._hist_unavail_until = float("inf")

    def tearDown(self):
        fw._HIST_PATH, fw._hist_unavail_until = self._path, self._unavail
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _payload(as_of="2026-08-28", rate=3.40, meeting="2026-09-16"):
        return {
            "as_of": as_of,
            "current": {"lower": 3.5, "upper": 3.75, "effr": 3.62},
            "meetings": [{
                "date": meeting, "implied_rate": rate,
                "cut_prob": 72.5, "hold_prob": 27.5, "hike_prob": 0.0,
            }],
        }


class RecordSnapshotTest(_DiskOnlyHistoryCase):

    def test_round_trips_through_disk(self):
        row = fw.record_snapshot(self._payload())
        self.assertEqual(row["date"], "2026-08-28")
        self.assertEqual(row["base_lower"], 3.5)
        self.assertEqual(row["base_upper"], 3.75)
        self.assertEqual(row["effr"], 3.62)

        rows = fw.history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meetings"][0]["implied_rate"], 3.40)
        self.assertEqual(rows[0]["meetings"][0]["cut_prob"], 72.5)

    def test_is_keyed_by_as_of_not_today(self):
        """A weekend refresh carries Friday's curve and must not add a row.

        The ZQ close on a Saturday *is* Friday's. Keying by date.today() would
        write the same numbers under Sat and Sun and put three identical points
        on the chart every week.
        """
        fw.record_snapshot(self._payload(as_of="2026-08-28"))
        fw.record_snapshot(self._payload(as_of="2026-08-28"))  # Sat refresh
        fw.record_snapshot(self._payload(as_of="2026-08-28"))  # Sun refresh
        self.assertEqual(len(fw.history()), 1)

    def test_same_date_overwrites_rather_than_duplicating(self):
        """~6 refreshes a day must converge on the latest curve, not accumulate."""
        fw.record_snapshot(self._payload(rate=3.40))
        fw.record_snapshot(self._payload(rate=3.35))
        rows = fw.history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meetings"][0]["implied_rate"], 3.35)

    def test_distinct_dates_accumulate_in_order(self):
        for stamp in ("2026-08-31", "2026-08-28", "2026-09-01"):
            fw.record_snapshot(self._payload(as_of=stamp))
        self.assertEqual([r["date"] for r in fw.history()],
                         ["2026-08-28", "2026-08-31", "2026-09-01"])

    def test_limit_returns_the_most_recent_rows(self):
        for stamp in ("2026-08-26", "2026-08-27", "2026-08-28"):
            fw.record_snapshot(self._payload(as_of=stamp))
        self.assertEqual([r["date"] for r in fw.history(limit=2)],
                         ["2026-08-27", "2026-08-28"])

    def test_stores_the_baseline_so_probabilities_stay_interpretable(self):
        """cut/hold/hike are relative to that day's range, so it must travel.

        Without base_lower/base_upper a September row and a December row stop
        being comparable the moment the Fed actually moves, and a cut_prob line
        drawn across the step silently changes meaning.
        """
        row = fw.record_snapshot(self._payload())
        self.assertIn("base_lower", row)
        self.assertIn("base_upper", row)

    def test_implied_rate_is_absolute(self):
        """The plotted series must need no re-basing across a real rate change."""
        row = fw.record_snapshot(self._payload(rate=3.40))
        self.assertEqual(row["meetings"][0]["implied_rate"], 3.40)


class RecordSnapshotRejectionTest(_DiskOnlyHistoryCase):
    """Junk must be refused, not stored — a bad row corrupts the series forever."""

    def test_rejects_a_non_iso_as_of(self):
        self.assertIsNone(fw.record_snapshot(self._payload(as_of="not-a-date")))
        self.assertEqual(fw.history(), [])

    def test_rejects_a_missing_as_of(self):
        p = self._payload()
        del p["as_of"]
        self.assertIsNone(fw.record_snapshot(p))
        self.assertEqual(fw.history(), [])

    def test_rejects_an_empty_meeting_list(self):
        p = self._payload()
        p["meetings"] = []
        self.assertIsNone(fw.record_snapshot(p))
        self.assertEqual(fw.history(), [])

    def test_rejects_a_meeting_with_no_usable_rate(self):
        p = self._payload()
        p["meetings"] = [{"date": "2026-09-16", "implied_rate": None}]
        self.assertIsNone(fw.record_snapshot(p))
        self.assertEqual(fw.history(), [])

    def test_rejects_an_error_payload(self):
        """_build_payload returns {"error": ...} with no meetings on failure."""
        self.assertIsNone(fw.record_snapshot(
            {"error": "futures data unavailable", "meetings": []}))
        self.assertEqual(fw.history(), [])

    def test_drops_only_the_bad_meeting_not_the_whole_row(self):
        p = self._payload()
        p["meetings"] = [
            {"date": "bad", "implied_rate": 1.0},
            {"date": "2026-09-16", "implied_rate": 3.4},
        ]
        row = fw.record_snapshot(p)
        self.assertEqual(len(row["meetings"]), 1)
        self.assertEqual(row["meetings"][0]["date"], "2026-09-16")


class NumberGuardTest(unittest.TestCase):
    """_number feeds json.dump(allow_nan=False), which raises on NaN."""

    def test_rejects_nan_and_inf(self):
        self.assertIsNone(fw._number(float("nan")))
        self.assertIsNone(fw._number(float("inf")))
        self.assertIsNone(fw._number(float("-inf")))

    def test_rejects_bool(self):
        """True would otherwise be stored as 1.0 and read back as a rate."""
        self.assertIsNone(fw._number(True))
        self.assertIsNone(fw._number(False))

    def test_accepts_numeric_strings_as_dynamodb_returns_them(self):
        self.assertEqual(fw._number("3.5"), 3.5)

    def test_rejects_junk(self):
        self.assertIsNone(fw._number("abc"))
        self.assertIsNone(fw._number(None))


class RowFromItemTest(unittest.TestCase):
    """The normaliser both DynamoDB and the disk file are read through."""

    def test_skips_a_non_iso_key_so_a_sentinel_can_share_the_table(self):
        """cta.py keeps _latest_report in its series table on this guarantee."""
        self.assertIsNone(fw._row_from_item({"date": "_latest_report"}))

    def test_parses_a_json_string_meetings_blob(self):
        item = {
            "date": "2026-08-28", "base_lower": "3.5",
            "meetings": json.dumps([
                {"d": "2026-09-16", "r": 3.4, "c": 70.0, "h": 30.0, "k": 0.0}]),
        }
        row = fw._row_from_item(item)
        self.assertEqual(row["base_lower"], 3.5)
        self.assertEqual(row["meetings"][0]["date"], "2026-09-16")
        self.assertEqual(row["meetings"][0]["implied_rate"], 3.4)
        self.assertEqual(row["meetings"][0]["cut_prob"], 70.0)

    def test_survives_an_unreadable_blob(self):
        row = fw._row_from_item({"date": "2026-08-28", "meetings": "{not json"})
        self.assertIsNone(row)

    def test_drops_a_row_with_no_meetings_at_all(self):
        self.assertIsNone(fw._row_from_item({"date": "2026-08-28"}))


class DiskFallbackTest(_DiskOnlyHistoryCase):
    """The file exists so a write made during a DynamoDB outage is not lost."""

    def test_unreadable_file_yields_an_empty_series_not_a_crash(self):
        fw._HIST_PATH.write_text("{ this is not json")
        self.assertEqual(fw.history(), [])

    def test_absent_file_yields_an_empty_series(self):
        self.assertFalse(fw._HIST_PATH.exists())
        self.assertEqual(fw.history(), [])

    def test_a_file_of_junk_rows_is_filtered_not_served(self):
        fw._HIST_PATH.write_text(json.dumps({"rows": [
            {"date": "nope"}, {"not": "a row"},
            {"date": "2026-08-28", "meetings": [{"d": "2026-09-16", "r": 3.4}]},
        ]}))
        rows = fw.history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-08-28")


class HistoryMemoTest(_DiskOnlyHistoryCase):
    """The memo in front of history(), which exists to bound DynamoDB spend.

    /api/fedwatch/history is public and unauthenticated and every uncached call
    is a full table scan, so the memo is a billing control, not a nicety. These
    pin the two properties that make it safe: a write must be visible
    immediately, and history() itself must stay pure so it remains testable.
    """

    def setUp(self):
        super().setUp()
        self._memo, self._memo_ts = fw._hist_memo, fw._hist_memo_ts
        fw._hist_memo, fw._hist_memo_ts = None, 0.0

    def tearDown(self):
        fw._hist_memo, fw._hist_memo_ts = self._memo, self._memo_ts
        super().tearDown()

    def test_second_call_does_not_re_read_the_store(self):
        fw.record_snapshot(self._payload())
        self.assertEqual(len(fw.history_cached()), 1)

        # Delete the file out from under it: a memoised second call must not
        # notice, which is what proves it did not read.
        fw._HIST_PATH.unlink()
        self.assertEqual(len(fw.history_cached()), 1)

    def test_history_itself_stays_pure(self):
        fw.record_snapshot(self._payload())
        fw.history_cached()                      # prime the memo
        fw._HIST_PATH.unlink()
        # The uncached entry point is a function of the stores, always.
        self.assertEqual(fw.history(), [])

    def test_a_write_invalidates_the_memo(self):
        fw.record_snapshot(self._payload(as_of="2026-08-28"))
        self.assertEqual([r["date"] for r in fw.history_cached()], ["2026-08-28"])

        # Without invalidation this row would stay invisible for the whole TTL
        # and the chart would look like the write silently failed.
        fw.record_snapshot(self._payload(as_of="2026-08-31"))
        self.assertIsNone(fw._hist_memo)
        self.assertEqual([r["date"] for r in fw.history_cached()],
                         ["2026-08-28", "2026-08-31"])

    def test_expiry_forces_a_re_read(self):
        fw.record_snapshot(self._payload())
        fw.history_cached()
        # Backdate past the TTL rather than sleeping through it.
        fw._hist_memo_ts = time.time() - fw._HIST_MEMO_TTL - 1
        fw._HIST_PATH.unlink()
        self.assertEqual(fw.history_cached(), [])

    def test_limit_slices_the_memo_without_re_keying_it(self):
        for stamp in ("2026-08-26", "2026-08-27", "2026-08-28"):
            fw.record_snapshot(self._payload(as_of=stamp))
        fw.history_cached()
        cached = fw._hist_memo
        # A mix of limits must not multiply the scans, so every limited call has
        # to come off the one memoised list.
        self.assertEqual(len(fw.history_cached(limit=1)), 1)
        self.assertEqual(len(fw.history_cached(limit=2)), 2)
        self.assertEqual(len(fw.history_cached()), 3)
        self.assertIs(fw._hist_memo, cached)

    def test_a_caller_cannot_mutate_the_memo(self):
        fw.record_snapshot(self._payload())
        rows = fw.history_cached()
        rows.clear()                             # a consumer doing consumer things
        self.assertEqual(len(fw.history_cached()), 1)


if __name__ == "__main__":
    unittest.main()
