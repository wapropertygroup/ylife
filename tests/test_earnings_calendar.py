"""ystocker/earnings_calendar.py -- the week's earnings calendar, from Nasdaq.

The rows below are cut from Nasdaq's real answers, fetched from the box on
2026-10-04: the banks' week (2026-10-13), a day already reported (2026-07-29,
which carries the actual EPS and the surprise and drops the time of day), and
the small caps of 2026-10-05, whose blanks and losses are the traps:

* ``"($0.05)"`` is a loss, not 0.05 -- the sign is the story on that row;
* ``""`` is no estimate, not a breakeven forecast;
* growth from a loss is not a percentage, so it is absent rather than +200%;
* Nasdaq's ``GEF.B`` is Yahoo's ``GEF-B``, which is what /history resolves;
* a day that had companies and comes back empty keeps its copy: "no rows" from
  the vendor is indistinguishable from a glitch, and a calendar that loses a
  busy day reads as a quiet one.

No network, no app: the cache tests use a temporary directory and a stubbed
fetch.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ystocker import earnings_calendar as ec


def _payload(rows, headers=None):
    return {"data": {"asOf": "Tue, Oct 13, 2026", "headers": headers or {}, "rows": rows},
            "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


BANKS = [  # 2026-10-13, as served
    {"lastYearRptDt": "10/14/2025", "lastYearEPS": "$12.25", "time": "time-pre-market", "symbol": "GS",
     "name": "The Goldman Sachs Group, Inc.", "marketCap": "$261,084,660,000", "fiscalQuarterEnding": "Sep/2026",
     "epsForecast": "$14.59", "noOfEsts": "7"},
    {"lastYearRptDt": "10/14/2025", "lastYearEPS": "$5.07", "time": "time-pre-market", "symbol": "JPM",
     "name": "J P Morgan Chase & Co", "marketCap": "$885,654,490,000", "fiscalQuarterEnding": "Sep/2026",
     "epsForecast": "$5.88", "noOfEsts": "6"},
    {"lastYearRptDt": "10/28/2025", "lastYearEPS": "$2.92", "time": "time-pre-market", "symbol": "UNH",
     "name": "UnitedHealth Group Incorporated", "marketCap": "$327,801,635,000", "fiscalQuarterEnding": "Sep/2026",
     "epsForecast": "$4.12", "noOfEsts": "8"},
]
REPORTED = [  # 2026-07-29, after the fact
    {"eps": "$4.74", "surprise": "12.59", "time": "time-not-supplied", "symbol": "MSFT", "name": "Microsoft Corporation",
     "marketCap": "$3,807,819,860,000", "fiscalQuarterEnding": "Jun/2026", "epsForecast": "$4.21", "noOfEsts": "15"},
    {"eps": "$6.18", "surprise": "-12.96", "time": "time-not-supplied", "symbol": "META", "name": "Meta Platforms, Inc.",
     "marketCap": "$1,849,311,230,000", "fiscalQuarterEnding": "Jun/2026", "epsForecast": "$7.10", "noOfEsts": "13"},
    {"eps": "$0", "surprise": "N/A", "time": "time-not-supplied", "symbol": "TMQ", "name": "Trilogy Metals Inc.",
     "marketCap": "$528,601,664", "fiscalQuarterEnding": "Aug/2026", "epsForecast": "($0.01)", "noOfEsts": "1"},
]
SMALL = [  # 2026-10-05
    {"lastYearRptDt": "10/06/2025", "lastYearEPS": "($0.05)", "time": "time-not-supplied", "symbol": "AEHR",
     "name": "Aehr Test Systems", "marketCap": "$3,318,209,790", "fiscalQuarterEnding": "Aug/2026",
     "epsForecast": "$0.05", "noOfEsts": "2"},
    {"lastYearRptDt": "N/A", "lastYearEPS": "($1.27)", "time": "time-not-supplied", "symbol": "NCPL",
     "name": "Netcapital Inc.", "marketCap": "$12,164,243", "fiscalQuarterEnding": "Jul/2026",
     "epsForecast": "", "noOfEsts": "2"},
    {"lastYearRptDt": "9/22/2025", "lastYearEPS": "$2", "time": "time-not-supplied", "symbol": "MSS",
     "name": "Maison Solutions Inc.", "marketCap": "$1,377,096", "fiscalQuarterEnding": "Jul/2026",
     "epsForecast": "$0.57", "noOfEsts": "2"},
]


class MoneyTests(unittest.TestCase):
    def test_the_shapes_nasdaq_sends(self):
        self.assertEqual(ec._money("$5.88"), 5.88)
        self.assertEqual(ec._money("($0.05)"), -0.05)
        self.assertEqual(ec._money("$0"), 0.0)          # a real zero, not absent
        self.assertEqual(ec._money("-$0.05"), -0.05)
        self.assertEqual(ec._money("$3,318,209,790"), 3318209790.0)
        self.assertEqual(ec._money("-83.72"), -83.72)
        self.assertEqual(ec._money("12.59"), 12.59)

    def test_blank_is_absent_not_zero(self):
        for blank in ("", "N/A", " ", None, "--", True):
            self.assertIsNone(ec._money(blank), blank)

    def test_counts_and_dates(self):
        self.assertEqual(ec._count("6"), 6)
        self.assertIsNone(ec._count(""))
        self.assertEqual(ec._mdy("10/14/2025"), "2025-10-14")
        self.assertEqual(ec._mdy("9/22/2025"), "2025-09-22")
        self.assertIsNone(ec._mdy("N/A"))
        self.assertIsNone(ec._mdy("13/40/2025"))


class ParseTests(unittest.TestCase):
    def test_a_day_ahead_largest_first(self):
        rows = ec.parse_day(_payload(BANKS))
        self.assertEqual([r["t"] for r in rows], ["JPM", "UNH", "GS"])
        jpm = rows[0]
        self.assertEqual((jpm["when"], jpm["est"], jpm["ests"], jpm["ly"], jpm["ly_date"]),
                         ("bmo", 5.88, 6, 5.07, "2025-10-14"))
        self.assertEqual(jpm["g"], 16.0)                 # (5.88 - 5.07) / 5.07
        self.assertIsNone(jpm["eps"])
        self.assertIsNone(jpm["beat"])
        self.assertEqual(jpm["fq"], "Sep/2026")

    def test_a_day_reported(self):
        rows = {r["t"]: r for r in ec.parse_day(_payload(REPORTED))}
        self.assertEqual((rows["MSFT"]["eps"], rows["MSFT"]["surp"], rows["MSFT"]["beat"]), (4.74, 12.59, 1))
        self.assertEqual((rows["META"]["beat"], rows["META"]["surp"]), (-1, -12.96))
        self.assertEqual(rows["MSFT"]["when"], "")       # a past day loses its time of day
        # $0 against a forecast loss of a cent is a beat, and a real zero.
        self.assertEqual((rows["TMQ"]["eps"], rows["TMQ"]["est"], rows["TMQ"]["beat"]), (0.0, -0.01, 1))
        self.assertIsNone(rows["TMQ"]["surp"])

    def test_losses_and_blanks(self):
        rows = {r["t"]: r for r in ec.parse_day(_payload(SMALL))}
        self.assertEqual(rows["AEHR"]["ly"], -0.05)      # a loss, not 0.05
        self.assertIsNone(rows["AEHR"]["g"])             # growth from a loss is not a percentage
        self.assertIsNone(rows["NCPL"]["est"])           # nobody's forecast is not breakeven
        self.assertIsNone(rows["NCPL"]["ly_date"])
        self.assertEqual(rows["MSS"]["ly"], 2.0)
        self.assertEqual(rows["MSS"]["g"], -71.5)

    def test_share_classes_take_yahoos_spelling(self):
        rows = ec.parse_day(_payload([dict(BANKS[0], symbol="GEF.B"), dict(BANKS[1], symbol="MOG.A")]))
        self.assertEqual(sorted(r["t"] for r in rows), ["GEF-B", "MOG-A"])

    def test_broken_rows_and_garbage(self):
        rows = ec.parse_day(_payload(BANKS + [BANKS[1], {"symbol": ""}, "not a row", None]))
        self.assertEqual(len(rows), 3)                   # the duplicate JPM went too
        self.assertEqual(ec.parse_day(None), [])
        self.assertEqual(ec.parse_day({"data": None}), [])   # a weekend
        self.assertEqual(ec.parse_day({"data": {"rows": None}}), [])

    def test_beat_and_growth_edges(self):
        self.assertEqual(ec.beat(1.004, 1.0), 0)         # within half a cent is in line
        self.assertIsNone(ec.beat(None, 1.0))
        self.assertIsNone(ec.growth(1.0, 0.0))
        self.assertIsNone(ec.growth(None, 1.0))


class DateTests(unittest.TestCase):
    def test_this_week_and_the_weekend(self):
        wed = dt.date(2026, 10, 7)
        self.assertEqual(ec.default_week(wed), dt.date(2026, 10, 5))
        self.assertEqual(ec.default_week(dt.date(2026, 10, 3)), dt.date(2026, 10, 5))   # Saturday
        self.assertEqual(ec.default_week(dt.date(2026, 10, 4)), dt.date(2026, 10, 5))   # Sunday
        self.assertEqual([d.weekday() for d in ec.weekdays(dt.date(2026, 10, 5))], [0, 1, 2, 3, 4])

    def test_weeks_are_clamped(self):
        today = dt.date(2026, 10, 7)
        lo, hi = ec.week_bounds(today)
        self.assertEqual(ec.clamp_week(dt.date(1990, 1, 1), today), (lo, True))
        self.assertEqual(ec.clamp_week(dt.date(2030, 1, 7), today), (hi, True))
        self.assertEqual(ec.clamp_week(dt.date(2026, 10, 12), today), (dt.date(2026, 10, 12), False))

    def test_the_market_date_is_new_yorks(self):
        # 02:00 UTC on Oct 14 is 22:00 on Oct 13 in New York.
        stamp = dt.datetime(2026, 10, 14, 2, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(ec.today_et(stamp), dt.date(2026, 10, 13))

    def test_freshness(self):
        today = dt.date(2026, 10, 7)
        now = dt.datetime(2026, 10, 7, 16, tzinfo=dt.timezone.utc).timestamp()
        future = dt.date(2026, 10, 20)
        self.assertFalse(ec.is_stale(future, now - 5 * 3600, now, today))
        self.assertTrue(ec.is_stale(future, now - 7 * 3600, now, today))
        self.assertTrue(ec.is_stale(today, now - 2 * 3600, now, today))      # actuals landing
        self.assertFalse(ec.is_stale(today, now - 600, now, today))
        old = dt.date(2026, 9, 1)
        read_later = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp()
        read_same_day = dt.datetime(2026, 9, 1, 18, tzinfo=dt.timezone.utc).timestamp()
        self.assertFalse(ec.is_stale(old, read_later, now, today))           # final
        self.assertTrue(ec.is_stale(old, read_same_day, now, today))         # read too early

    def test_the_warm_window_is_weekdays_only(self):
        days = ec.warm_window(dt.date(2026, 10, 7))
        self.assertEqual(days[0], dt.date(2026, 9, 28))
        self.assertEqual(len(days), 25)
        self.assertTrue(all(d.weekday() < 5 for d in days))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(ec, "CACHE_DIR", Path(self.tmp.name)),
                        mock.patch.object(ec, "FETCH_SPACING_SECONDS", 0)]
        for p in self.patches:
            p.start()
        ec._mem.clear()
        ec._failed.clear()
        self.day = dt.date(2026, 10, 13)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        ec._mem.clear()
        ec._failed.clear()
        self.tmp.cleanup()

    def test_refresh_then_peek(self):
        with mock.patch.object(ec, "fetch_day", return_value=ec.parse_day(_payload(BANKS))):
            self.assertTrue(ec.refresh_day(self.day))
        got = ec.peek_day(self.day)
        self.assertEqual([r["t"] for r in got["rows"]], ["JPM", "UNH", "GS"])
        self.assertTrue((Path(self.tmp.name) / "2026-10-13.json").exists())

    def test_another_worker_sees_the_masters_file(self):
        (Path(self.tmp.name) / "2026-10-13.json").write_text(
            json.dumps({"date": "2026-10-13", "_ts": time.time(), "rows": ec.parse_day(_payload(BANKS))}))
        self.assertEqual(len(ec.peek_day(self.day)["rows"]), 3)

    def test_a_failure_keeps_the_last_copy_and_pauses(self):
        with mock.patch.object(ec, "fetch_day", return_value=ec.parse_day(_payload(BANKS * 2 + SMALL))):
            ec.refresh_day(self.day)
        with mock.patch.object(ec, "fetch_day", side_effect=RuntimeError("cool-down")):
            self.assertFalse(ec.refresh_day(self.day))
        self.assertEqual(len(ec.peek_day(self.day)["rows"]), 6)
        self.assertTrue(ec.recently_failed(self.day))

    def test_a_busy_day_coming_back_empty_keeps_its_copy(self):
        with mock.patch.object(ec, "fetch_day", return_value=ec.parse_day(_payload(BANKS + SMALL))):
            ec.refresh_day(self.day)
        with mock.patch.object(ec, "fetch_day", return_value=[]):
            self.assertFalse(ec.refresh_day(self.day))
        self.assertEqual(len(ec.peek_day(self.day)["rows"]), 6)

    def test_a_quiet_day_may_be_empty(self):
        with mock.patch.object(ec, "fetch_day", return_value=[]):
            self.assertTrue(ec.refresh_day(dt.date(2026, 10, 3)))
        self.assertEqual(ec.peek_day(dt.date(2026, 10, 3))["rows"], [])

    def test_week_view_queues_only_what_is_due(self):
        monday = dt.date(2026, 10, 12)
        now = dt.datetime(2026, 10, 7, 16, tzinfo=dt.timezone.utc).timestamp()
        (Path(self.tmp.name) / "2026-10-13.json").write_text(
            json.dumps({"date": "2026-10-13", "_ts": now - 60, "rows": ec.parse_day(_payload(BANKS))}))
        ec._failed["2026-10-14"] = now - 30
        with mock.patch.object(ec, "kick") as kick:
            view = ec.week_view(monday, now=now)
        statuses = [d["status"] for d in view["days"]]
        self.assertEqual(statuses, ["pending", "ok", "failed", "pending", "pending"])
        kick.assert_called_once()
        self.assertEqual([d.isoformat() for d in kick.call_args[0][0]],
                         ["2026-10-12", "2026-10-15", "2026-10-16"])

    def test_the_worker_drains_the_queue_and_stands_down(self):
        done = []
        with mock.patch.object(ec, "refresh_day", side_effect=lambda d: done.append(d) or True):
            self.assertEqual(ec.kick([self.day, self.day, dt.date(2026, 10, 14)]), 2)
            for _ in range(200):
                if ec._worker is None:
                    break
                time.sleep(0.01)
        self.assertEqual(done, [self.day, dt.date(2026, 10, 14)])
        self.assertIsNone(ec._worker)


if __name__ == "__main__":
    unittest.main()
