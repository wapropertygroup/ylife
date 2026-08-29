"""Regression tests for the /assets pending-versus-warming state."""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
