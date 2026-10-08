"""Tests for ystocker.sector_map -- the /sectors payload -- and the breadth
rebuild schedule that keeps it a close behind at most.

No app, no network, no Yahoo: prices small enough to check by hand, plus the
committed GICS snapshot for the name tables. What is pinned:

* a sector's figures are exactly the GICS table's (gics.performance) for the
  same frame, so /sectors and /markets cannot disagree;
* every level is date-gated the same way: a company that joined mid-window is
  in the 1D figure and not the 1W one;
* the quadrant is the rotation map's rule on 1M and 1W against the index;
* breadth, the day's leader and laggard, and dollar volume against its own
  20-session average;
* names: every GICS sub-industry has a Chinese name and a unique id;
* the cache file round-trips, ignores another schema, and is re-read when it
  changes;
* breadth calls the builder and survives it failing;
* next_build_at: the first weekday close after a build, a day at most.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from ystocker import breadth, gics, sector_map

NY = dt.timezone(dt.timedelta(hours=-4))   # EDT, which every date below falls in
END = "2026-06-30"                          # a Tuesday


def _member(sub: str, w: float, added: str | None = "2000-01-01") -> dict:
    return {"sector": gics.SECTORS[gics.group_of(sub)[:2]], "sub": sub, "added": added, "w": w}


def _steps(days: pd.DatetimeIndex, *steps: tuple[str, float]) -> pd.Series:
    """A price that is each step's value from that date on."""
    s = pd.Series(float("nan"), index=days)
    for start, price in steps:
        s[s.index >= pd.Timestamp(start)] = float(price)
    return s


def _frame():
    """Six companies, weights as of the end session, so a weight is its share.

    Bases from 2026-06-30: 1D 06-29, 1W 06-23, 1M 05-29, YTD 2025-12-31.
    """
    days = pd.bdate_range("2025-06-02", END)
    closes = pd.DataFrame({
        # +33.1% over the month, +21% over the week, +10% on the day.
        "NVDA": _steps(days, ("2025-06-02", 100), ("2026-06-01", 110), ("2026-06-24", 121), (END, 133.1)),
        "AMD": _steps(days, ("2025-06-02", 50), (END, 45)),           # -10% on the day
        "AMAT": _steps(days, ("2025-06-02", 80)),                      # flat
        "XOM": _steps(days, ("2025-06-02", 100), ("2026-06-01", 90)),  # -10% over the month
        "CVX": _steps(days, ("2025-06-02", 100), ("2026-06-24", 105)), # +5% over the week
        # Joined 06-25: inside the 1D window, after the 1W base.
        "NEW": _steps(days, ("2025-06-02", 10), (END, 12)),
    })
    snap = {"schema": 1, "weights_asof": END, "members_asof": END, "members": {
        "NVDA": _member("Semiconductors", 6),
        "AMD": _member("Semiconductors", 2),
        "AMAT": _member("Semiconductor Materials & Equipment", 2),
        "XOM": _member("Integrated Oil & Gas", 10),
        "CVX": _member("Integrated Oil & Gas", 5),
        "NEW": _member("Semiconductors", 1, added="2026-06-25"),
    }}
    return closes, snap


def _row(payload: dict, level: str, key: str) -> dict:
    for r in payload["levels"][level]:
        if r["id"] == key:
            return r
    raise KeyError(key)


class NameTableTests(unittest.TestCase):

    def test_every_sub_industry_has_a_chinese_name(self):
        self.assertEqual(set(gics.SUB_INDUSTRY_ZH), set(gics.SUB_INDUSTRY_GROUP))
        for name, zh in gics.SUB_INDUSTRY_ZH.items():
            self.assertTrue(zh.strip(), name)
            self.assertNotEqual(zh, name, name)

    def test_ids_are_unique_and_stable(self):
        ids = [sector_map.slug(n) for n in gics.SUB_INDUSTRY_GROUP]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sector_map.slug("Hotels, Resorts & Cruise Lines"), "hotels-resorts-cruise-lines")
        self.assertEqual(sector_map.slug("IT Consulting & Other Services"), "it-consulting-other-services")


class QuadrantTests(unittest.TestCase):

    def test_the_rotation_maps_rule(self):
        self.assertEqual(sector_map.quadrant(2.0, 1.0), "lead")
        self.assertEqual(sector_map.quadrant(2.0, -1.0), "weak")
        self.assertEqual(sector_map.quadrant(-2.0, -1.0), "lag")
        self.assertEqual(sector_map.quadrant(-2.0, 1.0), "improve")
        # Zero is "not behind", as on /markets (x >= 0, y >= 0).
        self.assertEqual(sector_map.quadrant(0.0, 0.0), "lead")

    def test_a_missing_window_is_no_quadrant(self):
        self.assertIsNone(sector_map.quadrant(None, 1.0))
        self.assertIsNone(sector_map.quadrant(1.0, None))


class BuildTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.closes, cls.snap = _frame()
        cls.payload = sector_map.build(cls.closes, cls.snap)

    def test_dates_and_windows(self):
        p = self.payload
        self.assertEqual(p["asof"], END)
        self.assertEqual(p["schema"], sector_map.SCHEMA)
        self.assertEqual(p["periods"], ["1D", "1W", "1M", "3M", "YTD"])
        self.assertEqual(p["base_dates"]["1D"], "2026-06-29")
        self.assertEqual(p["base_dates"]["1W"], "2026-06-23")
        self.assertEqual(p["base_dates"]["1M"], "2026-05-29")
        self.assertEqual(p["base_dates"]["YTD"], "2025-12-31")
        self.assertEqual((p["trend"], p["momentum"]), ("1M", "1W"))

    def test_sectors_agree_with_the_gics_table(self):
        perf = gics.performance(self.closes, self.snap)
        table = {s["code"]: s for s in perf["sectors"]}
        groups = {g["code"]: g for s in perf["sectors"] for g in s["groups"]}
        for level, ref in (("sector", table), ("group", groups)):
            for row in self.payload["levels"][level]:
                want = ref[row["id"]]
                for p in self.payload["periods"]:
                    self.assertEqual(row["returns"][p], want["returns"][p], (level, row["id"], p))
                    self.assertEqual(row["rel"][p], want["rel"][p], (level, row["id"], p))
        self.assertEqual(self.payload["index"]["returns"],
                         {p: perf["index"]["returns"][p] for p in self.payload["periods"]})

    def test_a_one_company_group_moves_with_its_company(self):
        amat = _row(self.payload, "sub", "semiconductor-materials-equipment")
        self.assertEqual(amat["n"], 1)
        self.assertEqual(amat["returns"]["1D"], 0.0)
        self.assertEqual(amat["returns"]["1M"], 0.0)
        oil = _row(self.payload, "sub", "integrated-oil-gas")
        # XOM -10%, CVX +5%, weighted 10:5 by value at the month's start
        # (XOM 10/90*100 = 11.11, CVX 5/105*100 = 4.76).
        v0 = 10 / 90 * 100 + 5 / 105 * 100
        self.assertAlmostEqual(oil["returns"]["1M"], (15 / v0 - 1) * 100, places=2)

    def test_a_company_that_joined_mid_window_counts_only_from_then(self):
        semis = _row(self.payload, "sub", "semiconductors")
        # 1W: NVDA (110 -> 133.1) and AMD (50 -> 45) only; NEW joined 06-25.
        nvda_q, amd_q = 6 / 133.1, 2 / 45
        v0 = nvda_q * 110 + amd_q * 50
        self.assertAlmostEqual(semis["returns"]["1W"], ((6 + 2) / v0 - 1) * 100, places=2)
        # 1D includes it.
        new_q = 1 / 12
        v0d = nvda_q * 121 + amd_q * 50 + new_q * 10
        self.assertAlmostEqual(semis["returns"]["1D"], ((6 + 2 + 1) / v0d - 1) * 100, places=2)
        # Its own move is still reported, with the date it joined.
        new = next(m for m in self.payload["members"] if m["t"] == "NEW")
        self.assertAlmostEqual(new["r"]["1W"], 20.0, places=2)
        self.assertEqual(new["added"], "2026-06-25")
        nvda = next(m for m in self.payload["members"] if m["t"] == "NVDA")
        self.assertNotIn("added", nvda)

    def test_quadrants_follow_the_relative_figures(self):
        for level in sector_map.LEVELS:
            for row in self.payload["levels"][level]:
                self.assertEqual(row["quad"], sector_map.quadrant(row["rel"]["1M"], row["rel"]["1W"]),
                                 (level, row["id"]))
        self.assertEqual(_row(self.payload, "sub", "semiconductors")["quad"], "lead")

    def test_breadth_leader_and_laggard(self):
        semis = _row(self.payload, "sub", "semiconductors")
        self.assertEqual((semis["adv"], semis["dec"], semis["flat"]), (2, 1, 0))
        self.assertEqual(semis["leader"], {"t": "NEW", "r": 20.0})
        self.assertEqual(semis["laggard"], {"t": "AMD", "r": -10.0})
        idx = self.payload["index"]
        self.assertEqual((idx["adv"], idx["dec"], idx["flat"]), (2, 1, 3))

    def test_weights_add_up_and_rows_are_heaviest_first(self):
        for level in sector_map.LEVELS:
            rows = self.payload["levels"][level]
            self.assertAlmostEqual(sum(r["weight"] for r in rows), 100.0, places=1)
            self.assertEqual([r["weight"] for r in rows], sorted((r["weight"] for r in rows), reverse=True))
        self.assertAlmostEqual(sum(m["w"] for m in self.payload["members"]), 100.0, places=1)
        semis = _row(self.payload, "sub", "semiconductors")
        self.assertEqual(semis["top"][0], {"t": "NVDA", "w": 66.7})

    def test_sub_industry_rows_carry_both_names_and_their_parents(self):
        semis = _row(self.payload, "sub", "semiconductors")
        self.assertEqual(semis["name"], "Semiconductors")
        self.assertEqual(semis["name_zh"], "半导体")
        self.assertEqual((semis["group"], semis["sector"]), ("4530", "45"))
        grp = _row(self.payload, "group", "4530")
        self.assertEqual(grp["sector"], "45")
        self.assertEqual(_row(self.payload, "sector", "10")["name"], "Energy")

    def test_no_volume_frame_leaves_volume_figures_empty(self):
        self.assertIsNone(_row(self.payload, "sub", "semiconductors")["vr"])
        self.assertEqual(self.payload["coverage"]["volume"], 0)
        self.assertNotIn("vr", self.payload["index"])

    def test_dollar_volume_against_its_twenty_session_average(self):
        volumes = pd.DataFrame(1000.0, index=self.closes.index, columns=self.closes.columns)
        volumes.loc[pd.Timestamp(END), "NVDA"] = 3000.0
        payload = sector_map.build(self.closes, self.snap, volumes)
        nvda = next(m for m in payload["members"] if m["t"] == "NVDA")
        # The 20 sessions before the end are 06-02..06-29: sixteen at 110 and
        # four (06-24..06-29) at 121, each on 1,000 shares; 133.1 x 3,000 on the day.
        avg = (16 * 110 + 4 * 121) * 1000 / 20
        self.assertAlmostEqual(nvda["vr"], 133.1 * 3000 / avg, places=2)
        amat = _row(payload, "sub", "semiconductor-materials-equipment")
        self.assertAlmostEqual(amat["vr"], 1.0, places=2)
        self.assertEqual(payload["coverage"]["volume"], 6)
        self.assertIn("vr", payload["index"])

    def test_a_member_short_of_volume_history_is_left_out_of_both_sums(self):
        volumes = pd.DataFrame(1000.0, index=self.closes.index, columns=self.closes.columns)
        volumes.loc[volumes.index[-12:-1], "AMD"] = float("nan")
        payload = sector_map.build(self.closes, self.snap, volumes)
        amd = next(m for m in payload["members"] if m["t"] == "AMD")
        self.assertNotIn("vr", amd)
        self.assertEqual(payload["coverage"]["volume"], 5)

    def test_nothing_to_end_on_raises(self):
        empty = self.closes.iloc[:0]
        with self.assertRaises((ValueError, IndexError, KeyError)):
            sector_map.build(empty, self.snap)

    def test_the_payload_is_json(self):
        json.dumps(self.payload)


class CliffTests(unittest.TestCase):
    """A one-session break of more than half (or a doubling) is a split or a
    spin-off the history was not re-based for: CTVA, 2026-10-01, -84%."""

    @classmethod
    def setUpClass(cls):
        closes, snap = _frame()
        # XOM halves and then some on 06-26: inside the 1W window, before the 1D one.
        closes["XOM"] = _steps(closes.index, ("2025-06-02", 100), ("2026-06-01", 90), ("2026-06-26", 15))
        cls.closes, cls.snap = closes, snap
        cls.payload = sector_map.build(closes, snap)

    def test_the_member_has_no_figure_for_a_window_spanning_it(self):
        xom = next(m for m in self.payload["members"] if m["t"] == "XOM")
        self.assertNotIn("1W", xom["r"])
        self.assertNotIn("1M", xom["r"])
        self.assertEqual(xom["r"]["1D"], 0.0)       # 06-29 -> 06-30, after the break
        self.assertEqual(xom["cliff"], {"t": "XOM", "d": "2026-06-26", "r": round((15 / 90 - 1) * 100, 2)})

    def test_its_group_counts_only_the_rest(self):
        oil = _row(self.payload, "sub", "integrated-oil-gas")
        self.assertAlmostEqual(oil["returns"]["1W"], 5.0, places=2)   # CVX alone, 100 -> 105
        self.assertAlmostEqual(oil["returns"]["1M"], 5.0, places=2)
        self.assertEqual(oil["returns"]["1D"], 0.0)

    def test_the_payload_names_it(self):
        self.assertEqual([c["t"] for c in self.payload["coverage"]["cliffs"]], ["XOM"])

    def test_the_gics_table_leaves_it_out_too(self):
        perf = gics.performance(self.closes, self.snap)
        energy = next(s for s in perf["sectors"] if s["code"] == "10")
        self.assertAlmostEqual(energy["returns"]["1W"], 5.0, places=2)
        self.assertEqual([c["t"] for c in perf["coverage"]["cliffs"]], ["XOM"])

    def test_an_ordinary_move_is_not_a_cliff(self):
        # NVDA's +10% and AMD's -10% in one session are trades.
        self.assertEqual(sector_map.build(*_frame())["coverage"]["cliffs"], [])

    def test_a_missing_day_neither_hides_nor_invents_one(self):
        closes, snap = _frame()
        closes.loc[pd.Timestamp("2026-06-25"), "CVX"] = float("nan")
        self.assertEqual(sector_map.build(closes, snap)["coverage"]["cliffs"], [])
        closes["XOM"] = _steps(closes.index, ("2025-06-02", 100), ("2026-06-26", 15))
        closes.loc[pd.Timestamp("2026-06-26"), "XOM"] = float("nan")   # the break lands after a gap
        cl = sector_map.build(closes, snap)["coverage"]["cliffs"]
        self.assertEqual([(c["t"], c["d"]) for c in cl], [("XOM", "2026-06-29")])


class CacheTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sector_map.json"

    def test_round_trip(self):
        self.assertIsNone(sector_map.peek(self.path))
        self.assertFalse(sector_map.exists(self.path))
        sector_map.save({"schema": sector_map.SCHEMA, "asof": END, "levels": {}}, self.path)
        self.assertTrue(sector_map.exists(self.path))
        self.assertEqual(sector_map.peek(self.path)["asof"], END)

    def test_another_schema_is_ignored(self):
        self.path.write_text(json.dumps({"schema": sector_map.SCHEMA + 1, "asof": END}))
        self.assertIsNone(sector_map.peek(self.path))

    def test_garbage_is_ignored(self):
        self.path.write_text("{not json")
        with self.assertLogs("ystocker.sector_map", "WARNING"):
            self.assertIsNone(sector_map.peek(self.path))

    def test_the_default_file_is_re_read_when_it_changes(self):
        with mock.patch.object(sector_map, "CACHE_FILE", self.path), \
             mock.patch.object(sector_map, "_mem", None), \
             mock.patch.object(sector_map, "_mem_mtime", None):
            sector_map.save({"schema": sector_map.SCHEMA, "asof": "2026-06-29"})
            self.assertEqual(sector_map.peek()["asof"], "2026-06-29")
            sector_map.save({"schema": sector_map.SCHEMA, "asof": END})
            later = time.time() + 5
            os.utime(self.path, (later, later))
            self.assertEqual(sector_map.peek()["asof"], END)


class BreadthHookTests(unittest.TestCase):

    def test_the_build_is_saved_with_the_volume_frame(self):
        closes, snap = _frame()
        volumes = pd.DataFrame(1000.0, index=closes.index, columns=closes.columns)
        df = pd.concat({"Close": closes, "Volume": volumes}, axis=1)
        saved = []
        with mock.patch.object(sector_map, "save", side_effect=saved.append):
            breadth._sector_map_block(closes, df, snap)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["coverage"]["volume"], 6)

    def test_a_failure_costs_the_map_not_the_build(self):
        closes, snap = _frame()
        df = pd.concat({"Close": closes}, axis=1)
        with mock.patch.object(sector_map, "build", side_effect=RuntimeError("boom")), \
             mock.patch.object(sector_map, "save") as save, \
             self.assertLogs("ystocker.breadth", "WARNING"):
            breadth._sector_map_block(closes, df, snap)
        save.assert_not_called()

    def test_no_snapshot_builds_nothing(self):
        with mock.patch.object(sector_map, "build") as build:
            breadth._sector_map_block(None, None, None)
        build.assert_not_called()

    def test_the_build_calls_the_hook(self):
        self.assertIn("_sector_map_block(closes, df, snap)", Path(breadth.__file__).read_text())


class PageTests(unittest.TestCase):
    """The page composes its strings in JS, where I18n.apply() cannot reach
    them, so a key missing a language is an English word on a Chinese page."""

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls):
        cls.src = (cls.ROOT / "ystocker" / "static" / "i18n.js").read_text()
        cls.tpl = (cls.ROOT / "ystocker" / "templates" / "sectors.html").read_text()

    def _entry(self, key: str) -> None:
        q = r"'((?:[^'\\]|\\.)*)'"
        m = re.search(r"'" + re.escape(key) + r"'\s*:\s*\{\s*en:\s*" + q + r"\s*,\s*zh:\s*" + q + r"\s*\}",
                      self.src, re.S)
        self.assertIsNotNone(m, f"missing i18n key {key}, or it lacks en/zh")
        self.assertTrue(m.group(1) and m.group(2), f"empty string for {key}")

    def test_every_key_the_page_uses_exists_in_both_languages(self):
        keys = set(re.findall(r"(?:sectors|markets)\.[A-Za-z0-9_]+(?=')", self.tpl))
        keys |= set(re.findall(r'data-i18n(?:-placeholder)?="([^"]+)"', self.tpl))
        self.assertGreater(len(keys), 60)
        for key in sorted(keys):
            self._entry(key)
        self._entry("nav.sectors")

    def test_no_key_is_built_from_a_prefix(self):
        # A key assembled at run time escapes the scan above.
        self.assertNotRegex(self.tpl, r"'sectors\.[a-z_]*' \+")

    def test_every_sector_and_group_code_has_a_name(self):
        for code in {**gics.SECTORS, **gics.INDUSTRY_GROUPS}:
            self._entry(f"gics.{code}")

    def test_the_page_is_reachable_from_the_shared_templates(self):
        for name in ("base.html", "_ta_markets_bar.html", "_ta_footer.html", "markets.html"):
            text = (self.ROOT / "ystocker" / "templates" / name).read_text()
            self.assertIn("main.sectors_page", text, name)


class ScheduleTests(unittest.TestCase):

    @staticmethod
    def _at(y, m, d, hh, mm=0):
        return dt.datetime(y, m, d, hh, mm, tzinfo=NY).timestamp()

    def _due(self, built):
        return dt.datetime.fromtimestamp(breadth.next_build_at(built), NY)

    def test_a_morning_build_is_redone_after_that_days_close(self):
        self.assertEqual(self._due(self._at(2026, 6, 30, 10)), dt.datetime(2026, 6, 30, 16, 45, tzinfo=NY))

    def test_an_evening_build_waits_for_the_next_close(self):
        self.assertEqual(self._due(self._at(2026, 6, 30, 17)), dt.datetime(2026, 7, 1, 16, 45, tzinfo=NY))

    def test_a_friday_evening_build_is_capped_at_a_day(self):
        # The next close is Monday's; the 24-hour ceiling comes first.
        built = self._at(2026, 6, 26, 17)
        self.assertEqual(breadth.next_build_at(built), built + breadth._CACHE_TTL)

    def test_a_weekend_build_waits_for_mondays_close_or_a_day(self):
        built = self._at(2026, 6, 27, 9)   # Saturday
        self.assertEqual(breadth.next_build_at(built), built + breadth._CACHE_TTL)
        built = self._at(2026, 6, 28, 20)  # Sunday evening: Monday's close is sooner
        self.assertEqual(self._due(built), dt.datetime(2026, 6, 29, 16, 45, tzinfo=NY))

    def test_the_rebuild_time_is_new_york_time_across_a_clock_change(self):
        # 2026-11-02 is the Monday after DST ends; 16:45 there is 21:45 UTC.
        built = dt.datetime(2026, 11, 2, 14, 0, tzinfo=dt.timezone.utc).timestamp()
        due = dt.datetime.fromtimestamp(breadth.next_build_at(built), dt.timezone.utc)
        self.assertEqual((due.hour, due.minute), (21, 45))


if __name__ == "__main__":
    unittest.main()
