"""Unit tests for ystocker.data's pure Yahoo `info`-dict field helpers.

No network, no Flask app: `day_change_pct` only reads a plain dict, mirroring
the existing (untested) `latest_price`/`dividend_yield_pct`/`ps_ratio` helpers
it now sits alongside.
"""
from __future__ import annotations

import unittest

from ystocker.data import day_change_pct


class DayChangePctTests(unittest.TestCase):
    def test_prefers_yahoos_own_percentage(self) -> None:
        info = {"regularMarketChangePercent": 1.234, "regularMarketPrice": 101.0,
                "previousClose": 100.0}
        # Yahoo's own figure wins even though it disagrees with (101-100)/100.
        self.assertEqual(day_change_pct(info), 1.23)

    def test_falls_back_to_price_versus_previous_close(self) -> None:
        info = {"regularMarketPreviousClose": 100.0}
        self.assertEqual(day_change_pct(info, price=105.0), 5.0)

    def test_falls_back_to_previous_close_when_regular_market_previous_close_missing(self) -> None:
        info = {"previousClose": 50.0}
        self.assertEqual(day_change_pct(info, price=49.0), -2.0)

    def test_derives_price_from_info_when_not_given(self) -> None:
        info = {"currentPrice": 110.0, "previousClose": 100.0}
        self.assertEqual(day_change_pct(info), 10.0)

    def test_none_when_neither_percentage_nor_previous_close_available(self) -> None:
        self.assertIsNone(day_change_pct({"currentPrice": 110.0}))

    def test_none_when_previous_close_is_zero(self) -> None:
        # A zero previous close is not a legitimate price; dividing by it would
        # raise, and the field should read as "unknown" rather than crash.
        info = {"regularMarketPreviousClose": 0.0}
        self.assertIsNone(day_change_pct(info, price=10.0))

    def test_empty_info_returns_none(self) -> None:
        self.assertIsNone(day_change_pct({}))


if __name__ == "__main__":
    unittest.main()
