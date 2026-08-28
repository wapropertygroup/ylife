"""etf_holdings tests — no network.

The module is thin; what matters is the unit conversion and the shape it hands
/multiples. Yahoo returns weights as decimals (0.374 for 37.4%), and getting that
wrong would print a 37% technology weight as 0.37% — plausible-looking and wrong,
which is the failure mode this whole codebase keeps meeting.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).parents[1] / "ystocker" / "etf_holdings.py"
SPEC = importlib.util.spec_from_file_location("etf_holdings_under_test", PATH)
eh = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(eh)


class FakeDF:
    """Just enough DataFrame for _one(): .empty, .head(), .iterrows()."""

    def __init__(self, rows):
        self._rows = rows

    @property
    def empty(self):
        return not self._rows

    def head(self, n):
        return FakeDF(self._rows[:n])

    def iterrows(self):
        return iter(self._rows)


class FakeFunds:
    def __init__(self, **kw):
        self._kw = kw

    def __getattr__(self, name):
        if name in self._kw:
            v = self._kw[name]
            if isinstance(v, Exception):
                raise v
            return v
        raise AttributeError(name)


SPY_TOP = FakeDF([
    ("NVDA", {"Name": "NVIDIA Corp", "Holding Percent": 0.075494}),
    ("AAPL", {"Name": "Apple Inc", "Holding Percent": 0.070445}),
    ("MSFT", {"Name": "Microsoft Corp", "Holding Percent": 0.053599}),
])
SPY_WEIGHTS = {"technology": 0.374, "financial_services": 0.1224,
               "healthcare": 0.091000006, "realestate": 0.0188}


class UnitConversion(unittest.TestCase):
    def test_decimal_weights_become_percent(self):
        self.assertEqual(eh._pct(0.374), 37.4)
        self.assertEqual(eh._pct(0.075494), 7.55)
        self.assertEqual(eh._pct(0.0), 0.0)

    def test_non_numbers_are_none(self):
        for bad in (None, "", "0.3", {}, [], True, False):
            self.assertIsNone(eh._pct(bad), repr(bad))


class Composition(unittest.TestCase):
    def _one(self, **funds):
        fake = FakeFunds(**funds)
        ticker = mock.MagicMock()
        ticker.funds_data = fake
        with mock.patch.dict("sys.modules", {"yfinance": mock.MagicMock(
                Ticker=mock.MagicMock(return_value=ticker))}):
            return eh._one("SPY")

    def test_holdings_and_weights(self):
        out = self._one(top_holdings=SPY_TOP, sector_weightings=SPY_WEIGHTS,
                        asset_classes={"stockPosition": 0.9996, "cashPosition": 0.0005},
                        fund_overview={"categoryName": "Large Blend"})
        self.assertEqual(out["etf"], "SPY")
        self.assertEqual([h["ticker"] for h in out["holdings"]], ["NVDA", "AAPL", "MSFT"])
        self.assertEqual(out["holdings"][0]["weight"], 7.55)
        self.assertEqual(out["holdings"][0]["name"], "NVIDIA Corp")
        weights = {w["key"]: w["weight"] for w in out["sector_weights"]}
        self.assertEqual(weights["technology"], 37.4)
        self.assertEqual(out["asset_classes"]["stockPosition"], 99.96)
        self.assertEqual(out["overview"]["categoryName"], "Large Blend")

    def test_sector_weights_are_an_ordered_list(self):
        """Order must be data, not dict insertion order.

        Flask's jsonify sets sort_keys=True, so a dict comes back alphabetised —
        which is how the first version of this shipped with the rows in the wrong
        order. A list survives the response.
        """
        out = self._one(top_holdings=SPY_TOP, sector_weightings=SPY_WEIGHTS,
                        asset_classes={}, fund_overview={})
        self.assertIsInstance(out["sector_weights"], list)
        self.assertEqual([w["key"] for w in out["sector_weights"]],
                         [k for k, _en, _zh in eh.SECTORS])
        # Not alphabetical — that is the bug this guards.
        self.assertNotEqual([w["key"] for w in out["sector_weights"]],
                            sorted(k for k, _en, _zh in eh.SECTORS))
        # A sector Yahoo omitted is present with weight None, not dropped, so the
        # SPY and QQQ columns stay row-aligned.
        omitted = [w for w in out["sector_weights"] if w["key"] == "energy"]
        self.assertEqual(len(omitted), 1)
        self.assertIsNone(omitted[0]["weight"])

    def test_ordered_list_survives_jsonify(self):
        """The actual regression: round-trip through Flask's serialiser."""
        from flask import Flask
        out = self._one(top_holdings=SPY_TOP, sector_weightings=SPY_WEIGHTS,
                        asset_classes={}, fund_overview={})
        app = Flask(__name__)
        self.assertTrue(app.json.sort_keys, "assumption changed: jsonify no longer sorts")
        with app.app_context():
            from flask import jsonify
            body = jsonify({"composition": {"etfs": {"SPY": out}}}).get_json()
        keys = [w["key"] for w in body["composition"]["etfs"]["SPY"]["sector_weights"]]
        self.assertEqual(keys, [k for k, _en, _zh in eh.SECTORS])

    def test_concentration_is_computed(self):
        out = self._one(top_holdings=SPY_TOP, sector_weightings=SPY_WEIGHTS,
                        asset_classes={}, fund_overview={})
        self.assertAlmostEqual(out["top10_weight"], 7.55 + 7.04 + 5.36, places=2)
        self.assertEqual(out["top1_weight"], 7.55)

    def test_one_dead_block_does_not_lose_the_others(self):
        out = self._one(top_holdings=RuntimeError("boom"),
                        sector_weightings=SPY_WEIGHTS,
                        asset_classes={}, fund_overview={})
        self.assertEqual(out["holdings"], [])
        self.assertIsNone(out["top10_weight"])
        weights = {w["key"]: w["weight"] for w in out["sector_weights"]}
        self.assertEqual(weights["technology"], 37.4)

    def test_empty_holdings_frame(self):
        out = self._one(top_holdings=FakeDF([]), sector_weightings={},
                        asset_classes={}, fund_overview={})
        self.assertEqual(out["holdings"], [])
        self.assertIsNone(out["top1_weight"])


class CacheVersioning(unittest.TestCase):
    """A cache written by an older build must be refetched, not served.

    The v1 payload stored sector_weights as a dict; v2 stores an ordered list.
    For one deploy /multiples read a v1 cache and iterated the dict, getting its
    keys as strings. A version marker is what stops that.
    """

    def test_fetch_stamps_the_version(self):
        with mock.patch.object(eh, "_one", return_value={"etf": "X"}):
            out = eh._fetch()
        self.assertEqual(out["ver"], eh.CACHE_VER)

    def test_disk_cache_of_a_wrong_version_is_rejected(self):
        old = {"etfs": {"SPY": {}}, "ver": "v1", "fetched_at": 9e9}
        with mock.patch.object(pathlib.Path, "read_text",
                               return_value=__import__("json").dumps(old)):
            self.assertIsNone(eh._read_disk())

    def test_unversioned_disk_cache_is_rejected(self):
        old = {"etfs": {"SPY": {}}, "fetched_at": 9e9}
        with mock.patch.object(pathlib.Path, "read_text",
                               return_value=__import__("json").dumps(old)):
            self.assertIsNone(eh._read_disk())

    def test_current_version_is_accepted(self):
        cur = {"etfs": {"SPY": {}}, "ver": eh.CACHE_VER, "fetched_at": 9e9}
        with mock.patch.object(pathlib.Path, "read_text",
                               return_value=__import__("json").dumps(cur)):
            self.assertIsNotNone(eh._read_disk())


class Contract(unittest.TestCase):
    def test_module_surface(self):
        for name in ("get", "peek", "start_background_thread", "ETFS", "SECTORS"):
            self.assertTrue(hasattr(eh, name), name)

    def test_etfs_match_what_multiples_computes(self):
        self.assertEqual([e for e, _en, _zh in eh.ETFS], ["SPY", "QQQ"])

    def test_eleven_sectors_no_duplicates(self):
        keys = [k for k, _en, _zh in eh.SECTORS]
        self.assertEqual(len(keys), 11)
        self.assertEqual(len(keys), len(set(keys)))

    def test_peek_never_fetches(self):
        """peek() must not touch yfinance — /multiples depends on that."""
        with mock.patch.object(eh, "_fetch", side_effect=AssertionError("fetched!")):
            eh._mem = {}
            with mock.patch.object(eh, "_read_disk", return_value=None):
                self.assertIsNone(eh.peek())

    def test_get_serves_stale_on_failure(self):
        eh._mem, eh._mem_at = {}, 0.0
        cached = {"etfs": {"SPY": {"etf": "SPY"}}, "asof": "2026-08-01",
                  "fetched_at": 0.0}
        with mock.patch.object(eh, "_fetch", side_effect=RuntimeError("yahoo down")), \
             mock.patch.object(eh, "_read_disk", return_value=cached):
            out = eh.get()
        self.assertTrue(out.get("stale"))
        self.assertIn("SPY", out["etfs"])

    def test_get_returns_error_shape_when_nothing_cached(self):
        eh._mem, eh._mem_at = {}, 0.0
        with mock.patch.object(eh, "_fetch", side_effect=RuntimeError("yahoo down")), \
             mock.patch.object(eh, "_read_disk", return_value=None):
            out = eh.get()
        self.assertEqual(out["etfs"], {})
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
