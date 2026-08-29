"""Regression tests for the /assets pending-versus-warming state."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from ystocker import assets


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


if __name__ == "__main__":
    unittest.main()
