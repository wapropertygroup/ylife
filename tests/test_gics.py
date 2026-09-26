"""Tests for ystocker.gics — the S&P 500's GICS sectors and industry groups.

No app, no network, no Yahoo. The method was validated against S&P's own
official indices on the box (tests/check_gics_official.py, which needs Yahoo);
what is pinned here is everything that can be proven without a market:

* the hierarchy is the whole of GICS and hangs together;
* the committed snapshot agrees with it, name by name;
* the snapshot builder refuses each of its failure modes instead of writing a
  plausible-looking file with a row quietly wrong, and the box's daily refresh
  keeps the last good snapshot through every one of them;
* the arithmetic — cap weighting, the YTD base, date-gating, the partial-bar
  guard, the end-date coverage floor and split invariance — on prices small
  enough to check by hand;
* breadth actually calls it, reads its universe from the same snapshot, and
  survives it failing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from ystocker import gics

ROOT = Path(__file__).resolve().parent.parent
NY = dt.timezone(dt.timedelta(hours=-4))   # EDT, which every date below falls in


def _member(sub: str, w: float, added: str | None = "2000-01-01") -> dict:
    return {"sector": gics.SECTORS[gics.group_of(sub)[:2]], "sub": sub, "added": added, "w": w}


def _snap(members: dict, weights_asof: str = "2026-06-30") -> dict:
    return {"schema": 1, "weights_asof": weights_asof, "members_asof": weights_asof,
            "members": members}


def _days(start: str = "2025-06-02", end: str = "2026-06-30") -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


def _flat(days: pd.DatetimeIndex, **level) -> pd.DataFrame:
    return pd.DataFrame({t: [float(v)] * len(days) for t, v in level.items()}, index=days)


def _find(res: dict, code: str) -> dict:
    for s in res["sectors"]:
        if s["code"] == code:
            return s
        for g in s["groups"]:
            if g["code"] == code:
                return g
    raise KeyError(code)


def _epoch(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(y, m, d, hh, mm, tzinfo=NY).timestamp()


class HierarchyTests(unittest.TestCase):

    def test_it_is_the_whole_of_gics(self):
        self.assertEqual(len(gics.SECTORS), 11)
        self.assertEqual(len(gics.INDUSTRY_GROUPS), 25)
        # All of them, not just the ~126 held today — a reconstitution that
        # brings in a new sub-industry must not fail the rebuild.
        self.assertEqual(len(gics.SUB_INDUSTRY_GROUP), 163)

    def test_every_level_hangs_together(self):
        used = set(gics.SUB_INDUSTRY_GROUP.values())
        self.assertEqual(used, set(gics.INDUSTRY_GROUPS), "a group with no sub-industry, or vice versa")
        for code in gics.INDUSTRY_GROUPS:
            self.assertIn(code[:2], gics.SECTORS, code)

    def test_lookup_forgives_what_hand_editing_changes(self):
        self.assertEqual(gics.group_of("Multi-Line Insurance"), "4030")
        self.assertEqual(gics.group_of("Oil and Gas Drilling"), "1010")
        self.assertEqual(gics.group_of("  semiconductors "), "4530")
        self.assertEqual(gics.sector_of_name("information technology"), "45")

    def test_a_retired_name_is_refused_not_guessed(self):
        # Pre-2023 GICS; its members moved to Broadline Retail. Mapping it
        # anyway would put them in a group that no longer exists.
        self.assertIsNone(gics.group_of("Internet & Direct Marketing Retail"))

    def test_yahoo_symbol(self):
        self.assertEqual(gics.yahoo_symbol("BRK.B"), "BRK-B")
        self.assertEqual(gics.yahoo_symbol(" bf.b "), "BF-B")


class SnapshotFileTests(unittest.TestCase):
    """The committed ystocker/data/gics_sp500.json."""

    @classmethod
    def setUpClass(cls):
        # By path: with no argument load_snapshot() prefers the box's cached
        # refresh, which is not the file under test here.
        cls.snap = gics.load_snapshot(gics.SNAPSHOT_FILE)

    def test_loads(self):
        self.assertIsNotNone(self.snap)
        self.assertEqual(self.snap["schema"], gics.SNAPSHOT_SCHEMA)
        self.assertRegex(self.snap["weights_asof"], r"^\d{4}-\d{2}-\d{2}$")

    def test_is_the_whole_index(self):
        m = self.snap["members"]
        self.assertGreaterEqual(len(m), 490)
        total = sum(v["w"] for v in m.values())
        self.assertTrue(99.0 <= total <= 101.0, total)
        self.assertEqual({gics.group_of(v["sub"])[:2] for v in m.values()}, set(gics.SECTORS))

    def test_every_name_classifies_and_agrees_with_its_sector(self):
        for sym, v in self.snap["members"].items():
            grp = gics.group_of(v["sub"])
            self.assertIsNotNone(grp, f"{sym}: {v['sub']}")
            self.assertEqual(gics.SECTORS[grp[:2]], v["sector"], sym)
            self.assertGreater(v["w"], 0, sym)
            self.assertNotIn(".", sym, "Yahoo symbols use dashes")
            if v["added"] is not None:
                self.assertRegex(v["added"], r"^\d{4}-\d{2}-\d{2}$", sym)

    def test_one_member_per_line(self):
        # So a regeneration diffs as the names that changed.
        text = gics.SNAPSHOT_FILE.read_text()
        self.assertEqual(sum(1 for ln in text.splitlines() if ln.strip().startswith('"') and '"sub"' in ln),
                         len(self.snap["members"]))
        self.assertEqual(json.loads(gics.dumps_snapshot(self.snap)), self.snap)


class BuildSnapshotTests(unittest.TestCase):

    def _rows(self, n: int = 460) -> tuple[list[dict], dict]:
        rows = [{"symbol": f"T{i:03d}", "sector": "Information Technology",
                 "sub_industry": "Semiconductors", "added": "2001-02-03"} for i in range(n)]
        return rows, {r["symbol"]: 0.2 for r in rows}

    def test_happy_path(self):
        rows, w = self._rows()
        rows.append({"symbol": "BRK.B", "sector": "Financials",
                     "sub_industry": "Multi-Sector Holdings", "added": "1976-01-01 (1)"})
        w["BRK-B"] = 1.6
        snap = gics.build_snapshot(rows, w, weights_asof="2026-09-24", members_asof="2026-09-26")
        self.assertIn("BRK-B", snap["members"])
        self.assertIsNone(snap["members"]["BRK-B"]["added"], "a non-ISO date is dropped, not kept")
        self.assertEqual(snap["members"]["T000"]["sector"], "Information Technology")

    def test_refuses_an_unknown_sub_industry(self):
        rows, w = self._rows()
        rows[5]["sub_industry"] = "Internet & Direct Marketing Retail"
        with self.assertRaisesRegex(ValueError, "T005.*unrecognised|unrecognised.*T005"):
            gics.build_snapshot(rows, w, weights_asof="x", members_asof="x")

    def test_refuses_a_sector_that_disagrees(self):
        rows, w = self._rows()
        rows[7]["sector"] = "Health Care"
        with self.assertRaisesRegex(ValueError, "T007"):
            gics.build_snapshot(rows, w, weights_asof="x", members_asof="x")

    def test_refuses_a_member_with_no_weight(self):
        rows, w = self._rows()
        del w["T009"]
        with self.assertRaisesRegex(ValueError, "no SPY weight: T009"):
            gics.build_snapshot(rows, w, weights_asof="x", members_asof="x")

    def test_refuses_a_partial_scrape(self):
        rows, w = self._rows(60)
        with self.assertRaisesRegex(ValueError, "only 60 members"):
            gics.build_snapshot(rows, w, weights_asof="x", members_asof="x")

    def test_refuses_a_list_that_misses_part_of_spys_weight(self):
        # 460 rows clears the count floor; the second source says a slice of
        # the index is missing from the first, which is what a table that lost
        # rows looks like.
        rows, w = self._rows()
        w.update({"LOST1": 3.0, "LOST2": 2.5})
        with self.assertRaisesRegex(ValueError, r"cover only 94\.4% of SPY"):
            gics.build_snapshot(rows, w, weights_asof="x", members_asof="x")


class HoldingsFileTests(unittest.TestCase):
    """SPY's holdings .xlsx, read without openpyxl — and therefore without expat.

    ``pandas.read_excel`` failed on the dev Mac with "No module named expat":
    its Homebrew Python's pyexpat cannot be loaded (built against a newer
    libexpat than the system's). Every test here runs with pyexpat *blocked*, so
    a return to any expat-backed reader fails here rather than on that laptop.
    """

    M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def _xlsx(self, rows: dict[int, list], *, sheet_path="worksheets/holdings.xml", rich=False) -> bytes:
        """A minimal .xlsx: shared strings, numbers, a gap row, the sheet not
        named sheet1.xml, and a decoy sheet1.xml that must not be read."""
        import io
        import zipfile
        from xml.sax.saxutils import escape

        strings: list[str] = []
        def s_idx(v: str) -> int:
            if v not in strings:
                strings.append(v)
            return strings.index(v)
        body = []
        for r, cells in sorted(rows.items()):
            cs = []
            for i, v in enumerate(cells):
                ref = f"{chr(ord('A') + i)}{r}"
                if v is None:
                    continue
                if isinstance(v, str):
                    cs.append(f'<c r="{ref}" t="s"><v>{s_idx(v)}</v></c>')
                else:
                    cs.append(f'<c r="{ref}"><v>{v}</v></c>')
            body.append(f'<row r="{r}">{"".join(cs)}</row>')
        def si(v: str) -> str:
            if rich and " " in v:     # the same text split across two runs
                a, b = v.split(" ", 1)
                return f"<si><r><t>{escape(a)} </t></r><r><t>{escape(b)}</t></r></si>"
            return f"<si><t>{escape(v)}</t></si>"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/workbook.xml",
                       f'<workbook xmlns="{self.M}" xmlns:r="{self.R}"><sheets>'
                       f'<sheet name="holdings" sheetId="1" r:id="rId7"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       f'<Relationship Id="rId7" Target="{sheet_path}"/></Relationships>')
            z.writestr("xl/sharedStrings.xml",
                       f'<sst xmlns="{self.M}">{"".join(si(v) for v in strings)}</sst>')
            z.writestr(f"xl/{sheet_path}", f'<worksheet xmlns="{self.M}"><sheetData>{"".join(body)}</sheetData></worksheet>')
            if sheet_path != "worksheets/sheet1.xml":
                z.writestr("xl/worksheets/sheet1.xml",
                           f'<worksheet xmlns="{self.M}"><sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData></worksheet>')
        return buf.getvalue()

    SPY = {
        1: ["Fund Name:", "State Street SPDR S&P 500 ETF Trust"],
        2: ["Ticker Symbol:", "SPY"],
        3: ["Holdings:", "As of 24-Sep-2026"],
        # row 4 is blank in the real file
        5: ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"],
        6: ["NVIDIA CORP", "NVDA", "67066G104", "2379504", 8.185276, "-", 296315057, "USD"],
        7: ["APPLE INC", "AAPL", "037833100", "2046251", 7.383483, "-", 178696860, "USD"],
        8: ["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "084670702", "2073390", 1.417019, "-", 1, "USD"],
        9: ["US DOLLAR", "-", None, None, 0.011, None, None, None],
        11: ["Before investing in a fund, consider its investment objectives…"],
    }

    def _blocked(self):
        return mock.patch.dict(sys.modules, {"pyexpat": None, "xml.parsers.expat": None})

    def test_parses_the_holdings_without_expat(self):
        with self._blocked():
            asof, w = gics._parse_holdings(self._xlsx(self.SPY))
        self.assertEqual(asof, "2026-09-24")
        self.assertEqual(w["NVDA"], 8.185276)
        self.assertEqual(w["BRK-B"], 1.417019, "BRK.B must arrive in Yahoo form")
        self.assertNotIn("NAN", w, "a blank row is not a ticker")   # the old path's junk key
        self.assertNotIn("NONE", w)

    def test_the_block_is_real(self):
        # If blocking pyexpat did not break the stdlib parser, the test above
        # would prove nothing about avoiding it.
        with self._blocked():
            import xml.etree.ElementTree as ET
            with self.assertRaises(ImportError):
                ET.XMLParser()

    def test_rows_keep_their_positions_and_runs_join(self):
        with self._blocked():
            rows = gics._xlsx_rows(self._xlsx(self.SPY, rich=True))
        self.assertEqual(rows[3], [], "the blank preamble row stays a row")
        self.assertEqual(rows[4][1], "Ticker")
        self.assertEqual(rows[0][1], "State Street SPDR S&P 500 ETF Trust", "rich-text runs rejoin")
        self.assertEqual(rows[5][4], 8.185276)

    def test_the_first_sheet_is_found_through_the_workbook(self):
        # The decoy sheet1.xml holds a single 1; reading it by filename would
        # find no header and refuse.
        with self._blocked():
            asof, _ = gics._parse_holdings(self._xlsx(self.SPY, sheet_path="worksheets/holdings.xml"))
        self.assertEqual(asof, "2026-09-24")

    def test_a_changed_layout_is_refused(self):
        broken = dict(self.SPY)
        broken[5] = ["Security", "Symbol", "Weight"]
        with self._blocked(), self.assertRaisesRegex(ValueError, "changed shape"):
            gics._parse_holdings(self._xlsx(broken))

    def test_column_letters(self):
        self.assertEqual([gics._xlsx_column(r) for r in ("A1", "B7", "Z3", "AA10", "AZ2")],
                         [0, 1, 25, 26, 51])


class RefreshTests(unittest.TestCase):
    """The box refreshes the snapshot itself, once a day, and never breaks on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "gics_sp500.json"
        self._saved = gics._snapshot_cache
        gics._snapshot_cache = None
        self.patches = [mock.patch.object(gics, "CACHE_SNAPSHOT_FILE", self.cache),
                        mock.patch.object(gics, "REFRESH_ENABLED", True)]
        for p in self.patches:
            p.start()
        self.committed = gics.load_snapshot(gics.SNAPSHOT_FILE)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        gics._snapshot_cache = self._saved
        self.tmp.cleanup()

    def _fresh(self, **overrides):
        snap = json.loads(json.dumps(self.committed))
        snap.update({"weights_asof": "2026-10-01", "members_asof": "2026-10-02", **overrides})
        return snap

    def test_a_refresh_is_written_and_used(self):
        new = self._fresh()
        del new["members"]["TTWO"]
        new["members"]["NEWCO"] = _member("Application Software", 0.1, added="2026-10-01")
        with mock.patch.object(gics, "fetch_snapshot", return_value=new), \
             self.assertLogs("ystocker.gics", level="INFO") as logs:
            got = gics.refresh_snapshot()
        self.assertEqual(got["weights_asof"], "2026-10-01")
        self.assertEqual(gics.load_snapshot()["weights_asof"], "2026-10-01", "not the snapshot in force")
        self.assertEqual(gics.load_snapshot(self.cache)["weights_asof"], "2026-10-01", "not saved")
        self.assertTrue(any("joined ['NEWCO']; left ['TTWO']" in m for m in logs.output), logs.output)

    def test_a_recent_refresh_is_not_repeated(self):
        self.cache.write_text(gics.dumps_snapshot(self._fresh()))
        with mock.patch.object(gics, "fetch_snapshot", side_effect=AssertionError("fetched again")):
            self.assertEqual(gics.refresh_snapshot()["weights_asof"], "2026-10-01")

    def test_a_newer_file_beats_an_older_copy_in_memory(self):
        # Another process refreshed the file while this one held the baseline.
        gics._snapshot_cache = self.committed
        self.cache.write_text(gics.dumps_snapshot(self._fresh()))
        with mock.patch.object(gics, "fetch_snapshot", side_effect=AssertionError("fetched again")):
            self.assertEqual(gics.refresh_snapshot()["weights_asof"], "2026-10-01")
        self.assertEqual(gics.load_snapshot()["weights_asof"], "2026-10-01")

    def test_an_old_refresh_is_redone(self):
        self.cache.write_text(gics.dumps_snapshot(self._fresh()))
        old = time.time() - gics.REFRESH_AFTER_SECONDS - 60
        os.utime(self.cache, (old, old))
        newer = self._fresh(weights_asof="2026-10-02")
        with mock.patch.object(gics, "fetch_snapshot", return_value=newer) as fetch:
            self.assertEqual(gics.refresh_snapshot()["weights_asof"], "2026-10-02")
        fetch.assert_called_once()

    def test_a_failed_refresh_keeps_the_snapshot_in_force(self):
        for exc in (gics.SnapshotFetchError("wikipedia down"), ValueError("members cover only 80%")):
            gics._snapshot_cache = None
            with mock.patch.object(gics, "fetch_snapshot", side_effect=exc), \
                 self.assertLogs("ystocker.gics", level="WARNING"):
                got = gics.refresh_snapshot()
            self.assertEqual(got["weights_asof"], self.committed["weights_asof"])
            self.assertFalse(self.cache.exists(), "a failed refresh wrote something")

    def test_the_kill_switch(self):
        with mock.patch.object(gics, "REFRESH_ENABLED", False), \
             mock.patch.object(gics, "fetch_snapshot", side_effect=AssertionError("fetched")):
            self.assertEqual(gics.refresh_snapshot()["weights_asof"], self.committed["weights_asof"])

    def test_a_cut_short_cache_file_loses_to_the_committed_one(self):
        short = self._fresh()
        short["members"] = dict(list(short["members"].items())[:100])
        self.cache.write_text(gics.dumps_snapshot(short))
        with self.assertLogs("ystocker.gics", level="WARNING"):
            self.assertEqual(len(gics.load_snapshot()["members"]), len(self.committed["members"]))

    def test_both_sources_go_through_fetchguard(self):
        import requests

        from ystocker import fetchguard
        for exc in (requests.ConnectionError("refused"), fetchguard.CooldownActive("ssga", 30, "HTTP 503")):
            with mock.patch.object(fetchguard, "request", side_effect=exc) as req, \
                 self.assertRaises(gics.SnapshotFetchError):
                gics.fetch_snapshot()
            self.assertEqual(req.call_args.args[0], "wikipedia")


class PerformanceTests(unittest.TestCase):
    """The arithmetic, on prices small enough to check by hand."""

    MEMBERS = {
        "SEMI": _member("Semiconductors", 6.0),
        "SOFT": _member("Application Software", 3.0),
        "BANK": _member("Diversified Banks", 1.0),
    }

    def _run(self, closes, members=None, now=None, weights_asof="2026-06-30"):
        return gics.performance(closes, _snap(members or self.MEMBERS, weights_asof), now=now)

    def test_cap_weighted_returns_and_weights(self):
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-06-30", ["SEMI", "SOFT"]] = [110.0, 90.0]
        res = self._run(px)

        self.assertEqual(res["asof"], "2026-06-30")
        self.assertEqual(_find(res, "4530")["returns"]["1D"], 10.0)
        self.assertEqual(_find(res, "4510")["returns"]["1D"], -10.0)
        self.assertEqual(_find(res, "4010")["returns"]["1D"], 0.0)

        # Weights are 6:3:1 on the snapshot date, which here is the end date,
        # so yesterday's shares are 6/110 : 3/90 : 1/100 of today's value.
        q = {"SEMI": 6 / 110, "SOFT": 3 / 90, "BANK": 1 / 100}
        v0 = 100 * sum(q.values())
        self.assertAlmostEqual(res["index"]["returns"]["1D"], round((10 / v0 - 1) * 100, 2))
        it0 = 100 * (q["SEMI"] + q["SOFT"])
        self.assertAlmostEqual(_find(res, "45")["returns"]["1D"], round((9 / it0 - 1) * 100, 2))
        self.assertEqual(_find(res, "45")["weight"], 90.0)
        self.assertEqual(_find(res, "4530")["weight"], 60.0)

    def test_relative_and_weight_change_are_the_same_facts(self):
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-03-02":, "SEMI"] = 130.0
        px.loc["2026-05-01":, "SOFT"] = 95.0
        res = self._run(px)
        for p in res["periods"]:
            idx = res["index"]["returns"][p]
            chg = 0.0
            for code in ("4530", "4510", "4010"):
                g = _find(res, code)
                self.assertAlmostEqual(g["rel"][p], g["returns"][p] - idx, delta=0.011)
                chg += g["weight_chg"][p]
            self.assertAlmostEqual(chg, 0.0, delta=0.005, msg=f"{p}: weight moved from nowhere")
        self.assertAlmostEqual(sum(s["weight"] for s in res["sectors"]), 100.0, delta=0.02)

    def test_ytd_starts_from_last_years_close(self):
        # The trap: basing YTD on the first close of the year drops that
        # session's move — here, the whole of it.
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-01-02":] = 110.0
        res = self._run(px)
        self.assertEqual(res["base_dates"]["YTD"], "2025-12-31")
        self.assertEqual(res["index"]["returns"]["YTD"], 10.0)

    def test_base_dates_are_on_or_before_the_anniversary(self):
        days = _days()
        res = self._run(_flat(days, SEMI=100, SOFT=100, BANK=100))
        b = res["base_dates"]
        self.assertEqual(b["1D"], "2026-06-29")
        self.assertEqual(b["1W"], "2026-06-23")
        self.assertEqual(b["1M"], "2026-05-29")    # 30 May is a Saturday
        self.assertEqual(b["1Y"], "2025-06-30")
        self.assertEqual(res["periods"], list(gics.PERIODS))

    def test_a_name_added_mid_window_sits_out_longer_horizons(self):
        # NEWS rose 50% in the spring and joined on 15 May. Crediting its
        # sector with that run is the backcasting bias the date gate exists
        # for: measured, it read the S&P 500's year +18.37% against +17.24%.
        days = _days()
        members = dict(self.MEMBERS, NEWS=_member("Semiconductors", 2.0, added="2026-05-15"))
        px = _flat(days, SEMI=100, SOFT=100, BANK=100, NEWS=100)
        px.loc["2026-03-02":, "NEWS"] = 150.0
        px.loc["2026-06-30", "NEWS"] = 165.0          # +10% on the last day
        res = self._run(px, members)
        semis = _find(res, "4530")
        self.assertEqual(semis["returns"]["1Y"], 0.0, "NEWS's pre-membership run leaked in")
        self.assertEqual(semis["returns"]["YTD"], 0.0)
        self.assertGreater(semis["returns"]["1D"], 0.0, "a member since May counts on 1D")
        self.assertEqual(res["coverage"]["excluded_new"]["1Y"], 1)
        self.assertEqual(res["coverage"]["excluded_new"]["1D"], 0)

    def test_a_name_with_no_known_add_date_is_counted(self):
        members = dict(self.MEMBERS, OLD=_member("Semiconductors", 2.0, added=None))
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100, OLD=100)
        px.loc["2026-06-30", "OLD"] = 120.0
        res = self._run(px, members)
        self.assertEqual(res["coverage"]["excluded_new"]["1Y"], 0)
        self.assertGreater(_find(res, "4530")["returns"]["1Y"], 0.0)

    def test_a_mid_session_bar_is_not_a_close(self):
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-06-30", "SEMI"] = 200.0          # an intraday print
        mid = self._run(px, now=_epoch(2026, 6, 30, 14))
        self.assertEqual(mid["asof"], "2026-06-29")
        self.assertTrue(mid["partial_dropped"])
        self.assertEqual(mid["index"]["returns"]["1D"], 0.0)

        after = self._run(px, now=_epoch(2026, 6, 30, 17))
        self.assertEqual(after["asof"], "2026-06-30")
        self.assertFalse(after["partial_dropped"])

        next_morning = self._run(px, now=_epoch(2026, 7, 1, 10))
        self.assertEqual(next_morning["asof"], "2026-06-30", "yesterday's bar is final")

    def test_a_thin_last_row_is_not_an_end_date(self):
        # Yahoo sometimes omits the newest print for a few names. Ending on a
        # day missing a tenth of the weight would publish a different index
        # under the same heading. 90% coverage clears the base floor (50%) and
        # misses the end floor (95%), so this exercises the end floor alone —
        # with 60% missing, the row never reached it.
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-06-30", "BANK"] = float("nan")
        self.assertEqual(self._run(px)["asof"], "2026-06-29")
        # …and below the base floor it is not even a usable day.
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-06-30", "SEMI"] = float("nan")
        self.assertEqual(self._run(px)["asof"], "2026-06-29")

    def test_rebased_history_changes_nothing(self):
        # Yahoo re-bases an adjusted series after a split. Because the share
        # count is struck against the same series, scaling one name's whole
        # history must leave every figure exactly where it was.
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-02-02":, "SOFT"] = 120.0
        px.loc["2026-06-30", "SEMI"] = 104.0
        a = self._run(px)
        px2 = px.copy()
        px2["SOFT"] = px2["SOFT"] / 10.0
        b = self._run(px2)
        self.assertEqual(a["sectors"], b["sectors"])
        self.assertEqual(a["index"], b["index"])

    def test_a_missing_member_is_reported(self):
        members = dict(self.MEMBERS, GONE=_member("Regional Banks", 0.5))
        res = self._run(_flat(_days(), SEMI=100, SOFT=100, BANK=100), members)
        self.assertEqual(res["coverage"]["missing"], ["GONE"])
        self.assertLess(res["coverage"]["weight_pct"], 100.0)
        self.assertEqual(res["coverage"]["priced"], 3)

    def test_ordering_and_top_names(self):
        members = dict(self.MEMBERS, SEM2=_member("Semiconductor Materials & Equipment", 1.5))
        res = self._run(_flat(_days(), SEMI=100, SOFT=100, BANK=100, SEM2=100), members)
        self.assertEqual([s["code"] for s in res["sectors"]], ["45", "40"])
        self.assertEqual([g["code"] for g in res["sectors"][0]["groups"]], ["4530", "4510"])
        self.assertEqual([x["t"] for x in _find(res, "4530")["top"]], ["SEMI", "SEM2"])
        self.assertEqual(_find(res, "4530")["n"], 2)

    def test_no_usable_session_raises(self):
        px = _flat(_days(), SEMI=100, SOFT=100, BANK=100) * float("nan")
        with self.assertRaises(ValueError):
            self._run(px)


class TrailTests(unittest.TestCase):
    """The rotation map's trails: the table's arithmetic, re-run at past closes."""

    MEMBERS = PerformanceTests.MEMBERS

    def _px(self):
        # Semiconductors climb all year, software slides, banks stand still,
        # so every group's relative return differs from one week to the next.
        days = _days()
        n = len(days)
        return pd.DataFrame({"SEMI": [100 + 0.2 * i for i in range(n)],
                             "SOFT": [100 - 0.1 * i for i in range(n)],
                             "BANK": [100.0] * n}, index=days)

    def _trail(self, px, **kw):
        snap = _snap(self.MEMBERS)
        latest = gics.performance(px, snap)
        return latest, gics.trail(px, snap, latest, **kw)

    def test_the_head_is_the_table(self):
        latest, t = self._trail(self._px())
        self.assertEqual(t["dates"][0], latest["asof"])
        for code in ("4530", "4510", "4010"):
            for p in gics.TRAIL_PERIODS:
                self.assertEqual(t["rel"][code][p][0], _find(latest, code)["rel"].get(p), (code, p))

    def test_each_point_is_the_same_arithmetic_a_week_earlier(self):
        px = self._px()
        latest, t = self._trail(px)
        self.assertEqual(len(t["dates"]), gics.TRAIL_POINTS)
        end = pd.Timestamp(latest["asof"])
        for k, d in enumerate(t["dates"][1:], start=1):
            day = pd.Timestamp(d)
            # The last session on or before each weekly step, never after it.
            self.assertLessEqual(day, end - pd.Timedelta(days=7 * k))
            self.assertGreater(day, end - pd.Timedelta(days=7 * k + 7))
            then = gics.performance(px, _snap(self.MEMBERS), until=d)
            for p in gics.TRAIL_PERIODS:
                self.assertEqual(t["rel"]["4530"][p][k], _find(then, "4530")["rel"].get(p), (d, p))
        # …and the points differ, or this proves nothing about the stepping.
        self.assertEqual(len(set(t["rel"]["4530"]["1M"])), gics.TRAIL_POINTS)

    def test_a_past_end_keeps_the_snapshot_anchor(self):
        # Slicing the frame instead would re-anchor the share counts on the new
        # last row, as if the snapshot had been taken that day. Semis double
        # after 1 June, so on 1 June they were 3 of 3+3+1, not 6 of 10.
        days = _days()
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        px.loc["2026-06-02":, "SEMI"] = 200.0
        snap = _snap(self.MEMBERS)
        self.assertEqual(_find(gics.performance(px, snap, until="2026-06-01"), "4530")["weight"], 42.86)
        self.assertEqual(_find(gics.performance(px.loc[:"2026-06-01"], snap), "4530")["weight"], 60.0,
                         "the trap until= exists to avoid")

    def test_history_that_runs_out_ends_the_trail_early(self):
        px = self._px().loc["2026-06-15":]
        _latest, t = self._trail(px)
        self.assertEqual(t["dates"], ["2026-06-30", "2026-06-23", "2026-06-16"])
        # A window with no base date is a gap in the trail, not a zero.
        self.assertEqual(t["rel"]["4530"]["1Y"], [None, None, None])

    def test_a_gap_longer_than_a_step_ends_the_trail(self):
        days = pd.bdate_range("2026-05-01", "2026-06-30")
        days = days[(days < "2026-06-08") | (days > "2026-06-26")]
        px = _flat(days, SEMI=100, SOFT=100, BANK=100)
        _latest, t = self._trail(px)
        # Two steps back both land on 5 June; the second would repeat the first.
        self.assertEqual(t["dates"], ["2026-06-30", "2026-06-05"])

    def test_breadth_attaches_it_and_survives_it_failing(self):
        from ystocker import breadth
        px, snap = self._px(), _snap(self.MEMBERS)
        block = breadth._gics_block(px, snap)
        self.assertEqual(block["trail"]["dates"][0], block["asof"])
        with mock.patch.object(gics, "trail", side_effect=RuntimeError("boom")), \
             self.assertLogs("ystocker.breadth", level="WARNING"):
            block = breadth._gics_block(px, snap)
        self.assertIsNone(block["trail"], "None records a failed trail, not an old cache")
        self.assertTrue(block["sectors"], "a failed trail must cost only the lines")

    def test_the_disk_cache_schema_check_covers_the_trail(self):
        from ystocker import breadth
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "breadth_cache.json"
            base = {"_ts": dt.datetime.now().timestamp(), "adv_dec": {},
                    "pct_above_ma": {str(p): {"dates": [], "values": []} for p in breadth.MA_PERIODS}}
            with mock.patch.object(breadth, "_CACHE_FILE", path):
                path.write_text(json.dumps({**base, "gics": {"sectors": []}}))
                self.assertIsNone(breadth._load_disk_cache(), "a block from before the trail must rebuild once")
                self.assertIsNotNone(breadth._load_disk_cache(ignore_ttl=True), "…while the stale path serves it")
                path.write_text(json.dumps({**base, "gics": {"sectors": [], "trail": None}}))
                self.assertIsNotNone(breadth._load_disk_cache(), "None means the trail failed; do not refetch")


class I18nTests(unittest.TestCase):
    """Names and labels are composed in JS, where I18n.apply() cannot reach."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "ystocker" / "static" / "i18n.js").read_text()
        cls.tpl = (ROOT / "ystocker" / "templates" / "markets.html").read_text()

    def _entry(self, key: str) -> str:
        # Parsed as two quoted strings rather than "up to the next }": several
        # labels interpolate {date}, whose brace would end the match early.
        q = r"'((?:[^'\\]|\\.)*)'"
        m = re.search(r"'" + re.escape(key) + r"'\s*:\s*\{\s*en:\s*" + q + r"\s*,\s*zh:\s*" + q + r"\s*\}",
                      self.src, re.S)
        self.assertIsNotNone(m, f"missing i18n key {key}, or it lacks en/zh")
        self.assertTrue(m.group(1) and m.group(2), f"empty string for {key}")
        return m.group(0)

    def test_every_sector_and_group_has_a_name_in_both_languages(self):
        for code, name in {**gics.SECTORS, **gics.INDUSTRY_GROUPS}.items():
            body = self._entry(f"gics.{code}")
            en = re.search(r"en:\s*'([^']*)'", body).group(1)
            if code == "6010":
                continue   # shortened to "Equity REITs" for a table cell, on purpose
            self.assertEqual(en, name, code)

    def test_interpolated_labels_keep_their_placeholder(self):
        # The page substitutes {date} into these; a translation that dropped
        # it would silently lose the date the whole line exists to state.
        for key in ("markets.gics_asof", "markets.gics_note_method"):
            entry = self._entry(key)
            self.assertEqual(entry.count("{date}"), 2, f"{key}: en and zh must both carry {{date}}")

    def test_every_label_the_panel_composes_exists(self):
        for p in gics.PERIODS:
            self._entry(f"markets.gics_p_{p}")
        for key in sorted(set(re.findall(r"markets\.gics_[a-z_]+(?=')", self.tpl))):
            if key.endswith("_p_"):
                continue   # the prefix the period labels are built from
            self._entry(key)


class BreadthIntegrationTests(unittest.TestCase):
    """The arithmetic being right and it being *called* are different questions."""

    def _fake_yf(self, requested: list):
        def download(tickers, **_kw):
            requested.extend(tickers)
            days = pd.bdate_range("2025-01-02", "2026-06-30")
            cols = pd.MultiIndex.from_product([["Close"], list(tickers)])
            base = [100.0 + (i % 7) for i in range(len(tickers))]
            data = [[b * (1 + 0.0005 * n) for b in base] for n in range(len(days))]
            return pd.DataFrame(data, index=days, columns=cols)
        return types.SimpleNamespace(download=download)

    def test_breadth_downloads_the_members_and_publishes_the_block(self):
        from ystocker import breadth
        members = {"ZZZZ": _member("Semiconductors", 5.0), "AAPL": _member(
            "Technology Hardware, Storage & Peripherals", 7.0)}
        requested: list = []
        with mock.patch.dict(sys.modules, {"yfinance": self._fake_yf(requested)}), \
             mock.patch.object(breadth, "SP500_UNIVERSE", ("STALE",)), \
             mock.patch.object(gics, "refresh_snapshot", return_value=_snap(members)):
            data = breadth._build_cache()
            universe = breadth.SP500_UNIVERSE
        self.assertEqual(set(universe), set(members), "the build did not take the refreshed membership")
        self.assertNotIn("STALE", requested)
        self.assertEqual(requested.count("ZZZZ"), 1)
        self.assertEqual(requested.count("AAPL"), 1, "a member was fetched twice")
        self.assertTrue(data["gics"] and data["gics"]["sectors"])
        self.assertEqual(data["gics"]["coverage"]["priced"], 2)

    def test_the_breadth_universe_is_the_snapshot(self):
        # One list, so they cannot drift: the hand-edited tuple this replaced
        # was five names out of date while the snapshot beside it was current.
        from ystocker import breadth
        self.assertEqual(set(breadth.SP500_UNIVERSE), set(gics.load_snapshot()["members"]))
        self.assertEqual(len(breadth.SP500_UNIVERSE), len(set(breadth.SP500_UNIVERSE)))

    def test_an_unreadable_snapshot_empties_the_universe_rather_than_the_app(self):
        from ystocker import breadth
        with mock.patch.object(gics, "load_snapshot", return_value=None), \
             self.assertLogs("ystocker.breadth", level="ERROR") as logs:
            self.assertEqual(breadth._load_universe(), ())
        self.assertIn("universe is empty", logs.output[0])

    def test_a_failing_breakdown_costs_only_itself(self):
        from ystocker import breadth
        with mock.patch.dict(sys.modules, {"yfinance": self._fake_yf([])}), \
             mock.patch.object(breadth, "SP500_UNIVERSE", breadth.SP500_UNIVERSE), \
             mock.patch.object(gics, "refresh_snapshot", return_value=_snap({"ZZZZ": _member("Semiconductors", 5.0)})), \
             mock.patch.object(gics, "performance", side_effect=RuntimeError("boom")):
            data = breadth._build_cache()
        self.assertIn("gics", data)
        self.assertIsNone(data["gics"])
        self.assertTrue(data["pct_above_ma"], "breadth itself must survive")

    def test_the_disk_cache_schema_check(self):
        from ystocker import breadth
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "breadth_cache.json"
            base = {"_ts": dt.datetime.now().timestamp(), "adv_dec": {},
                    "pct_above_ma": {str(p): {"dates": [], "values": []} for p in breadth.MA_PERIODS}}
            with mock.patch.object(breadth, "_CACHE_FILE", path):
                path.write_text(json.dumps(base))
                self.assertIsNone(breadth._load_disk_cache(), "an old cache must trigger one rebuild")
                self.assertIsNotNone(breadth._load_disk_cache(ignore_ttl=True),
                                     "…while the stale path keeps serving it")
                path.write_text(json.dumps({**base, "gics": None}))
                self.assertIsNotNone(breadth._load_disk_cache(),
                                     "None means the build ran; refetching cannot fix it")

    def test_the_baseline_does_not_carry_it(self):
        from ystocker import breadth
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "baseline.json"
            with mock.patch.object(breadth, "_BASELINE_FILE", out), \
                 mock.patch.object(breadth, "_build_cache",
                                   return_value={"_ts": 1, "gics": {"x": 1}, "adv_dec": {}, "pct_above_ma": {}}):
                breadth.write_baseline()
            self.assertNotIn("gics", json.loads(out.read_text()))


if __name__ == "__main__":
    unittest.main()
