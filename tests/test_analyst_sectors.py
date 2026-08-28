"""Tests for ystocker.analyst and ystocker.sectors — no network.

The bug worth pinning here is a subtle one that shipped silently: pandas hands
back numpy scalars, and ``np.float64`` subclasses ``float`` while ``np.int64``
does **not** subclass ``int``. An ``isinstance(v, (int, float))`` guard therefore
passes every float column and rejects every integer one — so ``eps_trend``
(float64) worked while ``eps_revisions`` (int64) came back entirely None, with no
error anywhere to say so. Verified against live Yahoo before and after the fix.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

import numpy as np


def _load(name: str, filename: str):
    path = pathlib.Path(__file__).parents[1] / "ystocker" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


analyst = _load("analyst_under_test", "analyst.py")
sectors = _load("sectors_under_test", "sectors.py")


class FakeSeries(dict):
    def items(self):          # noqa: A003 - mimics pandas Series.items
        return dict.items(self)


class FakeDF:
    """Enough DataFrame for the two modules: .empty, .index, .loc, .head, .iterrows."""

    def __init__(self, rows: dict):
        self._rows = rows

    @property
    def empty(self):
        return not self._rows

    @property
    def index(self):
        return list(self._rows)

    @property
    def loc(self):
        return self

    def __getitem__(self, key):
        return FakeSeries(self._rows[key])

    def head(self, n):
        return FakeDF(dict(list(self._rows.items())[:n]))

    def iterrows(self):
        return iter((k, FakeSeries(v)) for k, v in self._rows.items())


class NumpyScalarHandling(unittest.TestCase):
    """The regression: numpy ints must not be silently dropped."""

    def test_numpy_ints_survive(self):
        for mod in (analyst, sectors):
            with self.subTest(module=mod.__name__):
                self.assertEqual(mod._f(np.int64(5)), 5.0)
                self.assertEqual(mod._f(np.int32(7)), 7.0)
                self.assertEqual(mod._f(np.float64(2.5)), 2.5)

    def test_the_isinstance_guard_would_have_failed(self):
        """Documents why the guard is a conversion, not a type check."""
        self.assertFalse(isinstance(np.int64(5), int))
        self.assertTrue(isinstance(np.float64(5.0), float))

    def test_analyst_i_handles_numpy(self):
        self.assertEqual(analyst._i(np.int64(4)), 4)
        self.assertEqual(analyst._i(np.int64(0)), 0)

    def test_bools_and_strings_rejected(self):
        for mod in (analyst, sectors):
            for bad in (True, False, "3", "", None, {}, [], b"1"):
                with self.subTest(module=mod.__name__, value=bad):
                    self.assertIsNone(mod._f(bad))

    def test_nan_rejected_so_the_cache_stays_valid_json(self):
        for mod in (analyst, sectors):
            self.assertIsNone(mod._f(float("nan")))
            self.assertIsNone(mod._f(np.float64("nan")))


EPS_TREND = FakeDF({
    "0q":  {"current": np.float64(2.34), "7daysAgo": np.float64(2.35),
            "30daysAgo": np.float64(2.34), "60daysAgo": np.float64(2.33),
            "90daysAgo": np.float64(2.33), "currency": "USD"},
    "+1y": {"current": np.float64(13.12), "7daysAgo": np.float64(12.88),
            "30daysAgo": np.float64(12.81), "60daysAgo": np.float64(12.67),
            "90daysAgo": np.float64(12.60), "currency": "USD"},
})
EPS_REVISIONS = FakeDF({
    "+1y": {"upLast7days": np.int64(2), "upLast30days": np.int64(4),
            "downLast30days": np.int64(0), "downLast7Days": np.int64(0),
            "currency": "USD"},
})
RECS = FakeDF({
    0: {"period": "0m", "strongBuy": np.int64(9), "buy": np.int64(49),
        "hold": np.int64(2), "sell": np.int64(1), "strongSell": np.int64(0)},
    1: {"period": "-1m", "strongBuy": np.int64(10), "buy": np.int64(48),
        "hold": np.int64(2), "sell": np.int64(1), "strongSell": np.int64(0)},
})


class AnalystOne(unittest.TestCase):
    def _one(self, **attrs):
        t = mock.MagicMock()
        for k, v in attrs.items():
            setattr(t, k, v)
        with mock.patch.dict("sys.modules", {"yfinance": mock.MagicMock(
                Ticker=mock.MagicMock(return_value=t))}):
            return analyst._one("NVDA")

    def test_full_payload(self):
        out = self._one(eps_trend=EPS_TREND, eps_revisions=EPS_REVISIONS,
                        recommendations_summary=RECS,
                        analyst_price_targets={"current": 216.9, "mean": 305.79,
                                               "high": 500.0, "low": 180.0,
                                               "median": 300.0})
        self.assertEqual(out["ticker"], "NVDA")
        lead = out["eps_trend"]["+1y"]
        self.assertEqual(lead["current"], 13.12)
        self.assertAlmostEqual(lead["chg30_abs"], 0.31, places=2)
        self.assertAlmostEqual(lead["chg30_pct"], 2.42, places=1)
        rev = out["eps_revisions"]["+1y"]
        self.assertEqual((rev["up30"], rev["down30"], rev["net30"]), (4, 0, 4))
        self.assertEqual(out["recommendations"][0]["strong_buy"], 9)
        self.assertEqual(out["recommendations"][0]["period"], "0m")
        self.assertAlmostEqual(out["price_target"]["upside_pct"], 40.98, places=1)

    def test_uncovered_ticker_is_none_not_zeroes(self):
        """Absent must read as 'not covered', not as 'no revisions'."""
        out = self._one(eps_trend=FakeDF({}), eps_revisions=FakeDF({}),
                        recommendations_summary=FakeDF({}),
                        analyst_price_targets={})
        self.assertIsNone(out)

    def test_negative_estimate_base_leaves_pct_none(self):
        """A loss-to-profit swing makes the percentage meaningless."""
        df = FakeDF({"+1y": {"current": np.float64(0.5),
                             "30daysAgo": np.float64(0.0)}})
        out = self._one(eps_trend=df, eps_revisions=FakeDF({}),
                        recommendations_summary=FakeDF({}),
                        analyst_price_targets={})
        lead = out["eps_trend"]["+1y"]
        self.assertIsNone(lead["chg30_pct"])
        self.assertEqual(lead["chg30_abs"], 0.5)

    def test_one_dead_block_keeps_the_others(self):
        t = mock.MagicMock()
        type(t).eps_trend = mock.PropertyMock(side_effect=RuntimeError("boom"))
        t.eps_revisions = EPS_REVISIONS
        t.recommendations_summary = FakeDF({})
        t.analyst_price_targets = {}
        with mock.patch.dict("sys.modules", {"yfinance": mock.MagicMock(
                Ticker=mock.MagicMock(return_value=t))}):
            out = analyst._one("NVDA")
        self.assertNotIn("eps_trend", out)
        self.assertEqual(out["eps_revisions"]["+1y"]["up30"], 4)

    def test_payload_is_valid_json_without_nan(self):
        out = self._one(eps_trend=EPS_TREND, eps_revisions=EPS_REVISIONS,
                        recommendations_summary=RECS, analyst_price_targets={})
        json.dumps(out, allow_nan=False)      # raises if a NaN leaked through


class AnalystSweep(unittest.TestCase):
    def test_cooldown_stops_the_sweep_and_keeps_partial(self):
        """A 429 on ticker three must not become 200 more requests."""
        import ystocker.fetchguard as fg

        calls = []

        def fake_guard(_provider):
            if len(calls) >= 2:
                raise fg.CooldownActive("yahoo", 60.0, "HTTP 429")

        def fake_one(sym):
            calls.append(sym)
            return {"ticker": sym, "price_target": {"mean": 1.0}}

        with mock.patch.object(analyst.fetchguard, "guard", fake_guard), \
             mock.patch.object(analyst, "_one", fake_one), \
             mock.patch.object(analyst, "SLEEP_BETWEEN", 0):
            out = analyst._fetch(["A", "B", "C", "D", "E"])
        self.assertEqual(len(out["tickers"]), 2)
        self.assertIn("stopped_early", out)
        self.assertEqual(out["universe"], 5)

    def test_sweep_with_nothing_covered_raises_so_stale_is_served(self):
        with mock.patch.object(analyst, "_one", return_value=None), \
             mock.patch.object(analyst, "SLEEP_BETWEEN", 0):
            with self.assertRaises(RuntimeError):
                analyst._fetch(["A", "B"])


class SectorsShape(unittest.TestCase):
    def _fetch(self, rows):
        with mock.patch.object(sectors, "_one", side_effect=lambda k: rows[k]):
            return sectors._fetch()

    def test_sorted_by_weight_and_emitted_as_a_list(self):
        rows = {k: {"key": k, "market_weight": w}
                for (k, _e, _z), w in zip(sectors.SECTORS,
                                          [5.0, 30.0, 10.0, 1.0, 2.0, 3.0,
                                           4.0, 6.0, 7.0, 8.0, 9.0])}
        out = self._fetch(rows)
        self.assertIsInstance(out["sectors"], list)
        weights = [r["market_weight"] for r in out["sectors"]]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.assertEqual(out["sectors"][0]["market_weight"], 30.0)

    def test_missing_weight_sorts_last_not_first(self):
        rows = {k: {"key": k, "market_weight": None} for k, _e, _z in sectors.SECTORS}
        rows[sectors.SECTORS[0][0]]["market_weight"] = 1.0
        out = self._fetch(rows)
        self.assertEqual(out["sectors"][0]["market_weight"], 1.0)

    def test_pct_converts_decimals(self):
        self.assertEqual(sectors._pct(np.float64(0.32218152)), 32.22)

    def test_int_coerces_string_counts(self):
        self.assertEqual(sectors._int("848"), 848)
        self.assertEqual(sectors._int(np.int64(848)), 848)
        self.assertIsNone(sectors._int(True))
        self.assertIsNone(sectors._int("many"))


class CacheContracts(unittest.TestCase):
    """Both modules must version their cache and never fetch from peek()."""

    def test_versioned(self):
        for mod, key in ((analyst, "tickers"), (sectors, "sectors")):
            with self.subTest(module=mod.__name__):
                good = {key: {"X": {}} if key == "tickers" else [{"key": "x"}],
                        "ver": mod.CACHE_VER, "fetched_at": 9e9}
                stale_ver = dict(good, ver="v0")
                with mock.patch.object(pathlib.Path, "read_text",
                                       return_value=json.dumps(good)):
                    self.assertIsNotNone(mod._read_disk())
                with mock.patch.object(pathlib.Path, "read_text",
                                       return_value=json.dumps(stale_ver)):
                    self.assertIsNone(mod._read_disk())

    def test_peek_never_fetches(self):
        for mod in (analyst, sectors):
            with self.subTest(module=mod.__name__):
                mod._mem = {}
                with mock.patch.object(mod, "_fetch",
                                       side_effect=AssertionError("fetched!")), \
                     mock.patch.object(mod, "_read_disk", return_value=None):
                    self.assertIsNone(mod.peek())

    def test_get_serves_stale_on_failure(self):
        for mod, key in ((analyst, "tickers"), (sectors, "sectors")):
            with self.subTest(module=mod.__name__):
                mod._mem, mod._mem_at = {}, 0.0
                cached = {key: {"X": {}} if key == "tickers" else [{"key": "x"}],
                          "ver": mod.CACHE_VER, "fetched_at": 0.0}
                with mock.patch.object(mod, "_fetch", side_effect=RuntimeError("down")), \
                     mock.patch.object(mod, "_read_disk", return_value=cached):
                    out = mod.get()
                self.assertTrue(out.get("stale"))

    def test_module_surface(self):
        for mod in (analyst, sectors):
            for name in ("get", "peek", "start_background_thread", "CACHE_VER"):
                self.assertTrue(hasattr(mod, name), f"{mod.__name__}.{name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
