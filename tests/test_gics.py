"""Tests for ystocker.gics — the S&P 500's GICS sectors and industry groups.

No app, no network, no Yahoo. The method was validated against S&P's own
official indices on the box (tests/check_gics_official.py, which needs Yahoo);
what is pinned here is everything that can be proven without a market:

* the hierarchy is the whole of GICS and hangs together;
* the committed snapshot agrees with it, name by name;
* the snapshot builder refuses each of its three failure modes instead of
  writing a plausible-looking file with a row quietly wrong;
* the arithmetic — cap weighting, the YTD base, date-gating, the partial-bar
  guard, the end-date coverage floor and split invariance — on prices small
  enough to check by hand;
* breadth actually calls it, on the union of tickers, and survives it failing.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
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
        cls.snap = gics.load_snapshot()

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

    def test_breadth_downloads_the_union_and_publishes_the_block(self):
        from ystocker import breadth
        members = {"ZZZZ": _member("Semiconductors", 5.0), "AAPL": _member(
            "Technology Hardware, Storage & Peripherals", 7.0)}
        requested: list = []
        with mock.patch.dict(sys.modules, {"yfinance": self._fake_yf(requested)}), \
             mock.patch.object(gics, "load_snapshot", return_value=_snap(members)):
            data = breadth._build_cache()
        self.assertIn("ZZZZ", requested, "a snapshot name outside SP500_UNIVERSE was not fetched")
        self.assertEqual(requested.count("AAPL"), 1, "a name in both lists was fetched twice")
        self.assertTrue(data["gics"] and data["gics"]["sectors"])
        self.assertEqual(data["gics"]["coverage"]["priced"], 2)

    def test_a_failing_breakdown_costs_only_itself(self):
        from ystocker import breadth
        with mock.patch.dict(sys.modules, {"yfinance": self._fake_yf([])}), \
             mock.patch.object(gics, "load_snapshot", return_value=_snap({"ZZZZ": _member("Semiconductors", 5.0)})), \
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
