"""Regression tests for the /assets pending-versus-warming state."""
from __future__ import annotations

import time
from pathlib import Path
import unittest
from unittest.mock import patch

from ystocker import assets, funddata


class AssetsWarmingTests(unittest.TestCase):
    def setUp(self) -> None:
        with assets._warm_lock:  # noqa: SLF001 - reset module state for isolation
            assets._warm_queue.clear()  # noqa: SLF001
            assets._warm_active = False  # noqa: SLF001

    def tearDown(self) -> None:
        self.setUp()

    def test_blocked_symbol_is_not_reported_as_active_work(self) -> None:
        with patch("ystocker.assets.funddata.can_warm", return_value=False):
            depth = assets.kick_warm(["STUCK"])
        self.assertEqual(depth, 0)
        self.assertEqual(assets.warm_status(), {"queued": 0, "active": False})

    def test_template_names_the_symbols_being_resolved(self) -> None:
        template = (Path(__file__).parents[1] / "ystocker" / "templates" /
                    "assets.html").read_text(encoding="utf-8")
        self.assertIn('id="asWarmingSymbols"', template)
        self.assertIn("d.pending_symbols", template)

    def test_holdings_table_has_sortable_data_columns(self) -> None:
        template = (Path(__file__).parents[1] / "ystocker" / "templates" /
                    "assets.html").read_text(encoding="utf-8")
        for key in ("symbol", "name", "kind", "quantity", "price", "day_change_value",
                    "value", "weight", "coverage", "gain", "account"):
            self.assertIn(f'data-holding-sort="{key}"', template)
        self.assertIn("function sortedHoldings", template)
        self.assertIn("aria-sort", template)

    def test_exposure_table_has_sortable_data_columns(self) -> None:
        template = (Path(__file__).parents[1] / "ystocker" / "templates" /
                    "assets.html").read_text(encoding="utf-8")
        for key in ("symbol", "name", "pct", "direct_pct", "indirect_pct", "route_count"):
            self.assertIn(f'data-exposure-sort="{key}"', template)
        self.assertIn("function sortedExposures", template)
        self.assertIn('id="asExposuresHead"', template)

    def test_no_progress_clears_queue_and_stops_activity(self) -> None:
        with assets._warm_lock:  # noqa: SLF001
            assets._warm_queue["STUCK"] = 1.0  # noqa: SLF001
            assets._warm_active = True  # noqa: SLF001
        with (patch("ystocker.assets.funddata.warm", return_value=(0, 1)),
              patch("ystocker.assets.funddata.is_known", return_value=False),
              patch("ystocker.assets.funddata.flush")):
            assets._warm_loop()  # noqa: SLF001
        self.assertEqual(assets.warm_status(), {"queued": 0, "active": False})

    def test_nh_529_code_uses_fxaix_only_as_lookthrough_proxy(self) -> None:
        record = funddata.peek("NHFSMKX98", need_quote=True)
        self.assertTrue(record["synthetic"])
        self.assertEqual(record["kind"], funddata.KIND_FUND)
        self.assertEqual(record["proxy_symbol"], "FXAIX")
        self.assertIsNone(record["price"])
        self.assertEqual(record["holdings"], [{
            "symbol": "FXAIX",
            "name": "Fidelity 500 Index Fund",
            "weight": 1.0,
        }])
        self.assertFalse(funddata.can_warm("NHFSMKX98"))

    def test_nh_529_keeps_imported_value_and_explains_proxy(self) -> None:
        priced, rows, warnings = assets.value_positions([{
            "symbol": "NHFSMKX98",
            "quantity": 100,
            "value": 7_834.0,
        }])
        self.assertEqual(priced[0].value, 7_834.0)
        self.assertEqual(rows[0]["value_source"], "imported")
        self.assertEqual(rows[0]["proxy_symbol"], "FXAIX")
        self.assertIn({"code": "analysis_proxy", "symbol": "NHFSMKX98",
                       "proxy": "FXAIX"}, warnings)

    def test_nh_529_resolves_the_proxy_not_the_plan_code(self) -> None:
        original_peek = funddata.peek

        def cold_proxy(symbol: str, *, need_quote: bool = False):
            if symbol == "NHFSMKX98":
                return original_peek(symbol, need_quote=need_quote)
            return None

        with (patch("ystocker.assets.funddata.peek", side_effect=cold_proxy),
              patch("ystocker.assets.funddata.price_of", return_value=None),
              patch("ystocker.assets.funddata.is_known", return_value=False)):
            snapshot = assets.analyse([{
                "symbol": "NHFSMKX98",
                "quantity": 100,
                "value": 7_834.0,
            }])
        self.assertIn("FXAIX", snapshot["pending_symbols"])
        self.assertNotIn("NHFSMKX98", snapshot["pending_symbols"])

    def test_live_row_reports_todays_change(self) -> None:
        fake_record = {"symbol": "VOO", "kind": funddata.KIND_EQUITY,
                       "price": 500.0, "day_change_pct": 1.25,
                       "quote_at": time.time()}

        with (patch("ystocker.assets.funddata.peek", return_value=fake_record),
              patch("ystocker.assets.funddata.price_of", return_value=500.0)):
            _priced, rows, _warnings = assets.value_positions([
                {"symbol": "VOO", "quantity": 10}])

        self.assertEqual(rows[0]["value_source"], "live")
        self.assertEqual(rows[0]["day_change_pct"], 1.25)
        # value is quantity x price = 5000; day_change_value is that times pct/100.
        self.assertAlmostEqual(rows[0]["day_change_value"], 62.5)

    def test_imported_row_with_no_price_has_no_day_change(self) -> None:
        with (patch("ystocker.assets.funddata.peek", return_value=None),
              patch("ystocker.assets.funddata.price_of", return_value=None)):
            _priced, rows, _warnings = assets.value_positions([
                {"symbol": "GME", "quantity": 10, "value": 250.0}])

        self.assertEqual(rows[0]["value_source"], "imported")
        self.assertIsNone(rows[0]["day_change_pct"])
        self.assertIsNone(rows[0]["day_change_value"])

    def test_nh_529_reports_the_proxy_funds_day_change(self) -> None:
        # The plan code itself is a synthetic record with no market data of its
        # own (funddata._synthetic), so today's move has to come from FXAIX, the
        # same underlying _sector_mix already substitutes for composition.
        real_peek = funddata.peek

        def fake_peek(symbol: str, *, need_quote: bool = False):
            if symbol == "FXAIX":
                return {"symbol": "FXAIX", "kind": funddata.KIND_FUND,
                        "price": 200.0, "day_change_pct": -0.5,
                        "quote_at": time.time()}
            return real_peek(symbol, need_quote=need_quote)

        with patch("ystocker.assets.funddata.peek", side_effect=fake_peek):
            _priced, rows, _warnings = assets.value_positions([{
                "symbol": "NHFSMKX98", "quantity": 100, "value": 7_834.0,
            }])

        self.assertEqual(rows[0]["value_source"], "imported")
        self.assertEqual(rows[0]["day_change_pct"], -0.5)
        self.assertAlmostEqual(rows[0]["day_change_value"], round(7_834.0 * -0.5 / 100, 2))

    def test_stale_live_quote_is_queued_without_becoming_pending(self) -> None:
        # A resolved, live-priced holding is not "pending" -- but nothing else
        # ever re-asks funddata for a symbol once it first resolves, so its
        # quote would otherwise freeze forever. analyse() has to notice this
        # itself and queue a re-warm, without perturbing pending_symbols /
        # pending_count / warming, which mean "never resolved", not "aging".
        ancient = time.time() - 999_999
        fake_record = {"symbol": "VOO", "kind": funddata.KIND_EQUITY,
                       "price": 500.0, "day_change_pct": 0.1, "quote_at": ancient}

        with (patch("ystocker.assets.funddata.peek", return_value=fake_record),
              patch("ystocker.assets.funddata.price_of", return_value=500.0)):
            snapshot = assets.analyse([{"symbol": "VOO", "quantity": 10}])

        self.assertIn("VOO", snapshot["stale_quote_symbols"])
        self.assertNotIn("VOO", snapshot["pending_symbols"])
        self.assertEqual(snapshot["pending_count"], 0)
        self.assertFalse(snapshot["warming"])

    def test_fresh_live_quote_is_not_queued_for_a_repeat_fetch(self) -> None:
        fake_record = {"symbol": "VOO", "kind": funddata.KIND_EQUITY,
                       "price": 500.0, "day_change_pct": 0.1,
                       "quote_at": time.time()}

        with (patch("ystocker.assets.funddata.peek", return_value=fake_record),
              patch("ystocker.assets.funddata.price_of", return_value=500.0)):
            snapshot = assets.analyse([{"symbol": "VOO", "quantity": 10}])

        self.assertEqual(snapshot["stale_quote_symbols"], [])

    def test_portfolio_day_change_aggregates_across_positions(self) -> None:
        records = {
            "VOO": {"symbol": "VOO", "kind": funddata.KIND_EQUITY, "price": 500.0,
                    "day_change_pct": 2.0, "quote_at": time.time()},
            "BND": {"symbol": "BND", "kind": funddata.KIND_EQUITY, "price": 70.0,
                    "day_change_pct": -1.0, "quote_at": time.time()},
        }

        def fake_peek(symbol: str, *, need_quote: bool = False):
            return records.get(symbol)

        def fake_price_of(symbol: str):
            rec = records.get(symbol)
            return rec["price"] if rec else None

        with (patch("ystocker.assets.funddata.peek", side_effect=fake_peek),
              patch("ystocker.assets.funddata.price_of", side_effect=fake_price_of)):
            snapshot = assets.analyse([
                {"symbol": "VOO", "quantity": 10},   # value 5000, +2%   -> +100
                {"symbol": "BND", "quantity": 100},  # value 7000, -1%  -> -70
            ])

        self.assertAlmostEqual(snapshot["day_change_value"], 30.0)
        # Against yesterday's close: (5000+7000-30) prior value.
        self.assertAlmostEqual(snapshot["day_change_pct"], round(30.0 / 11_970.0 * 100, 2))


if __name__ == "__main__":
    unittest.main()
