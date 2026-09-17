"""Tests for the target-range probe in ``ystocker.fedwatch``.

The probe exists because of a real failure on 2026-09-17: the FOMC moved to
3.75–4.00, FRED published the new row, and the page went on showing 3.50–3.75
for hours because the cached payload had been built ninety minutes earlier and
the TTL is four hours.

What is tested here is mostly the *refusals*, because every one of them fails
silently in production:

  * a probe that cannot reach FRED must not look like "unchanged" (it would
    suppress a rebuild) nor like "changed" (it would rebuild every 30 minutes
    for ever);
  * a changed range must invalidate the whole payload rather than be patched
    into it, since `lower`/`upper` are the baseline every implied rate is
    projected from and are written into an unrecoverable history series;
  * float noise off a CSV must not read as a rate change.

No network and no app: every test drives the pure helpers directly.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import fedwatch  # noqa: E402


def _payload(lower=3.50, upper=3.75, schedule=None, ts=1000.0):
    return {
        "_ts": ts,
        "_ver": fedwatch._CACHE_VER,
        "current": {"lower": lower, "upper": upper,
                    "label": f"{lower:.2f}–{upper:.2f}"},
        "meetings": [{"date": "2026-10-27"}],
        "schedule": schedule if schedule is not None else [],
    }


class RangeChangedTests(unittest.TestCase):
    def test_detects_a_real_move(self):
        self.assertTrue(fedwatch.range_changed(_payload(3.50, 3.75), (3.75, 4.00)))

    def test_unchanged_range_does_not_rebuild(self):
        self.assertFalse(fedwatch.range_changed(_payload(3.50, 3.75), (3.50, 3.75)))

    def test_a_failed_probe_is_not_a_change(self):
        """None means "don't know". Treating it as a change would rebuild the
        whole payload — a ZQ curve fetch — on every probe while FRED is down."""
        self.assertFalse(fedwatch.range_changed(_payload(), None))

    def test_no_payload_is_not_a_change(self):
        self.assertFalse(fedwatch.range_changed(None, (3.75, 4.00)))

    def test_payload_without_a_recorded_range_is_not_a_change(self):
        """Already being rebuilt for other reasons; claiming a change here would
        just add a second rebuild."""
        broken = _payload()
        broken["current"] = {}
        self.assertFalse(fedwatch.range_changed(broken, (3.75, 4.00)))

    def test_float_noise_is_not_a_change(self):
        """These arrive as floats parsed from a CSV. Comparing them raw makes
        3.75 != 3.7500000001 a rate change, and the payload rebuilds on every
        probe for ever — a slow, silent Yahoo bill."""
        self.assertFalse(
            fedwatch.range_changed(_payload(3.50, 3.75), (3.5000000001, 3.7499999999)))

    def test_a_quarter_point_apart_is_a_change(self):
        """The smallest move the Fed actually makes must not be rounded away."""
        self.assertTrue(fedwatch.range_changed(_payload(3.50, 3.75), (3.25, 3.50)))

    def test_only_one_side_moving_still_counts(self):
        """A widened or narrowed range is a change even though one edge held."""
        self.assertTrue(fedwatch.range_changed(_payload(3.50, 3.75), (3.50, 4.00)))


class EffectiveWindowTests(unittest.TestCase):
    """The fast cycle runs on the days a change can actually land."""

    def test_decision_day_is_hot(self):
        today = date(2026, 9, 16)
        self.assertTrue(fedwatch._in_effective_window(["2026-09-16"], today=today))

    def test_the_effective_day_after_is_hot(self):
        """The case that actually happened: decision on the 16th, new range
        effective and published on the 17th."""
        self.assertTrue(
            fedwatch._in_effective_window(["2026-09-16"], today=date(2026, 9, 17)))

    def test_stays_hot_across_the_window(self):
        for offset in range(0, fedwatch._HOT_WINDOW_DAYS + 1):
            with self.subTest(offset=offset):
                self.assertTrue(fedwatch._in_effective_window(
                    ["2026-09-16"], today=date(2026, 9, 16) + timedelta(days=offset)))

    def test_cools_off_after_the_window(self):
        past = date(2026, 9, 16) + timedelta(days=fedwatch._HOT_WINDOW_DAYS + 1)
        self.assertFalse(fedwatch._in_effective_window(["2026-09-16"], today=past))

    def test_a_future_meeting_is_not_hot(self):
        """Nothing has been decided yet, so nothing can have changed. Being hot
        here would poll fast for weeks before every meeting."""
        self.assertFalse(
            fedwatch._in_effective_window(["2026-10-27"], today=date(2026, 9, 17)))

    def test_empty_and_missing_schedules_are_cold(self):
        self.assertFalse(fedwatch._in_effective_window([], today=date(2026, 9, 17)))
        self.assertFalse(fedwatch._in_effective_window(None, today=date(2026, 9, 17)))

    def test_a_malformed_entry_is_skipped_not_raised(self):
        """This only picks a polling interval; a bad date must not take down the
        refresh thread. The good entry beside it still registers."""
        self.assertTrue(fedwatch._in_effective_window(
            ["not-a-date", None, "2026-09-16"], today=date(2026, 9, 17)))
        self.assertFalse(fedwatch._in_effective_window(
            ["not-a-date"], today=date(2026, 9, 17)))


class ProbeIntervalTests(unittest.TestCase):
    def test_hot_window_polls_faster(self):
        hot = _payload(schedule=["2026-09-16"])
        self.assertEqual(
            fedwatch._probe_interval(hot, today=date(2026, 9, 17)),
            fedwatch._RANGE_PROBE_INTERVAL_HOT)

    def test_ordinary_day_uses_the_baseline(self):
        cold = _payload(schedule=["2026-10-27"])
        self.assertEqual(
            fedwatch._probe_interval(cold, today=date(2026, 9, 17)),
            fedwatch._RANGE_PROBE_INTERVAL)

    def test_missing_payload_still_yields_an_interval(self):
        """The loop must always get a number; returning None would crash the
        sleep computation and stop the refresh thread outright."""
        self.assertEqual(fedwatch._probe_interval(None),
                         fedwatch._RANGE_PROBE_INTERVAL)

    def test_the_baseline_is_shorter_than_the_full_ttl(self):
        """Otherwise the probe never fires — the TTL always wins the min()."""
        self.assertLess(fedwatch._RANGE_PROBE_INTERVAL, fedwatch._CACHE_TTL)
        self.assertLess(fedwatch._RANGE_PROBE_INTERVAL_HOT,
                        fedwatch._RANGE_PROBE_INTERVAL)


class ProbeTargetRangeTests(unittest.TestCase):
    """The probe reads only DFEDTARL/DFEDTARU — no EFFR, no DFEDTAR history."""

    def test_reads_the_last_observation_of_each_series(self):
        series = {
            "DFEDTARL": [("2026-09-16", 3.50), ("2026-09-17", 3.75)],
            "DFEDTARU": [("2026-09-16", 3.75), ("2026-09-17", 4.00)],
        }
        with mock.patch.object(fedwatch, "_fred_series", side_effect=lambda s: series[s]):
            self.assertEqual(fedwatch.probe_target_range(), (3.75, 4.00))

    def test_does_not_fetch_effr_or_the_step_history(self):
        """Those are the expensive two-thirds of `_fetch_current_rate`, and the
        whole point of a probe is that it is cheap enough to run every 30 min."""
        seen: list[str] = []

        def _record(series_id):
            seen.append(series_id)
            return [("2026-09-17", 3.75 if series_id == "DFEDTARL" else 4.00)]

        with mock.patch.object(fedwatch, "_fred_series", side_effect=_record):
            fedwatch.probe_target_range()
        self.assertEqual(sorted(seen), ["DFEDTARL", "DFEDTARU"])

    def test_an_empty_series_returns_none_rather_than_a_partial_range(self):
        """Half a range is not a range. Returning (3.75, None) here would crash
        the comparison, and defaulting the missing side would invent a move."""
        with mock.patch.object(fedwatch, "_fred_series",
                               side_effect=lambda s: [] if s == "DFEDTARU"
                               else [("2026-09-17", 3.75)]):
            self.assertIsNone(fedwatch.probe_target_range())


class SchedulePayloadTests(unittest.TestCase):
    def test_schedule_is_needed_because_meetings_drops_past_decisions(self):
        """`meetings` holds only *upcoming* decisions, so on the morning a new
        range lands the meeting that caused it is already gone from that list.
        The probe would then never know it was in the hot window — which is
        exactly the day it matters. Hence the separate `schedule` key."""
        payload = _payload(schedule=["2026-09-16", "2026-10-27"])
        upcoming = [m["date"] for m in payload["meetings"]]
        self.assertNotIn("2026-09-16", upcoming)
        self.assertTrue(
            fedwatch._in_effective_window(payload["schedule"], today=date(2026, 9, 17)))

    def test_cache_version_was_bumped_for_the_new_key(self):
        """Adding `schedule` without bumping `_CACHE_VER` leaves every existing
        cache looking fresh while missing the key the probe reads, so the fast
        cycle silently never engages."""
        self.assertEqual(fedwatch._CACHE_VER, "v3")


if __name__ == "__main__":
    unittest.main()


class WorkerPickupTests(unittest.TestCase):
    """A worker must notice a payload the *master* rebuilt.

    This is the piece that makes the probe worth having. Under gunicorn
    ``--preload`` the refresh thread runs only in the master, so each worker
    forks a snapshot of ``_cache_data`` and — before this — served it for the
    entire four-hour TTL without ever looking at disk again. The master could
    detect a Fed move within minutes and rebuild, and every reader would still
    be told the old range until their worker happened to be recycled.
    """

    def setUp(self):
        import json, tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "fedwatch_cache.json"
        self._json = json
        patcher = mock.patch.object(fedwatch, "_CACHE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Leave module state as we found it; these are process globals.
        saved = (fedwatch._cache_data, fedwatch._cache_ts, fedwatch._cache_mtime)

        def _restore():
            fedwatch._cache_data, fedwatch._cache_ts, fedwatch._cache_mtime = saved
        self.addCleanup(_restore)

    def _write(self, payload):
        self.path.write_text(self._json.dumps(payload))

    def _hold(self, payload, mtime):
        fedwatch._cache_data = payload
        fedwatch._cache_ts = payload["_ts"]
        fedwatch._cache_mtime = mtime

    def test_worker_picks_up_a_newer_payload_written_by_the_master(self):
        import time
        old = _payload(3.50, 3.75, ts=time.time() - 60)
        self._hold(old, mtime=1.0)                    # forked snapshot
        new = _payload(3.75, 4.00, ts=time.time())    # master rebuilt
        self._write(new)
        got = fedwatch.get_fedwatch_data()
        self.assertEqual(got["current"]["label"], "3.75–4.00")

    def test_unchanged_disk_is_not_reparsed(self):
        """The gate is one stat() per request. If the file has not been rewritten
        since we loaded it, the JSON must not be parsed again — this runs on
        every request to a public endpoint."""
        import time
        held = _payload(3.50, 3.75, ts=time.time())
        self._write(held)
        self._hold(held, mtime=self.path.stat().st_mtime)
        with mock.patch.object(fedwatch, "_load_disk_cache") as loader:
            got = fedwatch.get_fedwatch_data()
        loader.assert_not_called()
        self.assertIs(got, held)

    def test_an_older_payload_on_disk_is_ignored(self):
        """Ordering is by the payload's own `_ts`, not by mtime alone. A rewrite
        that is somehow *older* — a restored backup, a clock step — must not
        roll a good cache backwards."""
        import time
        now = time.time()
        held = _payload(3.75, 4.00, ts=now)
        self._hold(held, mtime=1.0)
        self._write(_payload(3.50, 3.75, ts=now - 3600))
        got = fedwatch.get_fedwatch_data()
        self.assertEqual(got["current"]["label"], "3.75–4.00")
