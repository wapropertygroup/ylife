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


# Real `info` values from the box, 2026-10-04 (the survey behind PE_BASIS_BAND).
AAPL = {"currency": "USD", "financialCurrency": "USD", "trailingPE": 38.31114, "forwardPE": 34.821583,
        "marketCap": 4869931925504, "netIncomeToCommon": 128929996800, "pegRatio": 2.1,
        "currentPrice": 333.69}
TOKIO_MARINE = {"currency": "JPY", "financialCurrency": "JPY", "trailingPE": 1.8175304,
                "forwardPE": 1.0291672, "marketCap": 14382213890048,
                "netIncomeToCommon": 539595997184, "trailingEps": 279.17, "forwardEps": 493.02,
                "currentPrice": 507.4, "shortName": "TOKIO MARINE HOLDINGS INC"}
ADVANTEST = {"currency": "JPY", "financialCurrency": "JPY", "trailingPE": 74.62715,
             "marketCap": 27889042980864, "netIncomeToCommon": 459953012736}
TSM_ADR = {"currency": "USD", "financialCurrency": "TWD", "trailingPE": 35.334827,
           "marketCap": 2452061159424, "netIncomeToCommon": 2216808415232}
FUBO = {"currency": "USD", "financialCurrency": "USD", "trailingPE": 2.2552083,
        "marketCap": 261569024, "netIncomeToCommon": -55064000}


class PeBasisTests(unittest.TestCase):
    """Yahoo's per-share P/E against cap / net income, which no share count enters."""

    def setUp(self) -> None:
        from ystocker import data
        self.data = data

    def test_agreeing_figures_pass(self) -> None:
        self.assertAlmostEqual(self.data.pe_basis_ratio(AAPL), 1.014, places=3)
        self.assertTrue(self.data.pe_basis_ok(AAPL))

    def test_tokio_marine_is_caught(self) -> None:
        """¥507 a share against a ¥279 EPS from before a split: P/E 1.8, truly ~27."""
        self.assertAlmostEqual(self.data.pe_basis_ratio(TOKIO_MARINE), 0.068, places=3)
        self.assertFalse(self.data.pe_basis_ok(TOKIO_MARINE))

    def test_the_widest_legitimate_gap_surveyed_passes(self) -> None:
        # Advantest's TTM EPS and net income cover slightly different windows.
        self.assertTrue(self.data.pe_basis_ok(ADVANTEST))

    def test_a_split_either_way_lands_outside_the_band(self) -> None:
        for factor in (2.0, 0.5, 3.0, 10.0):
            info = dict(AAPL, trailingPE=AAPL["trailingPE"] * factor)
            self.assertFalse(self.data.pe_basis_ok(info), factor)

    def test_an_adr_is_not_measured(self) -> None:
        """Net income in TWD against a cap in USD would measure the exchange rate."""
        self.assertIsNone(self.data.pe_basis_ratio(TSM_ADR))
        self.assertTrue(self.data.pe_basis_ok(TSM_ADR))

    def test_a_positive_pe_on_negative_earnings_fails(self) -> None:
        self.assertEqual(self.data.pe_basis_ratio(FUBO), 0.0)
        self.assertFalse(self.data.pe_basis_ok(FUBO))

    def test_what_cannot_be_measured_passes(self) -> None:
        for info in ({}, dict(AAPL, trailingPE=None), dict(AAPL, netIncomeToCommon=None),
                     dict(AAPL, financialCurrency=None), dict(AAPL, marketCap=0),
                     dict(AAPL, trailingPE=float("inf")), dict(AAPL, trailingPE=-5.0)):
            self.assertIsNone(self.data.pe_basis_ratio(info), info)
            self.assertTrue(self.data.pe_basis_ok(info))

    def test_currency_codes_compare_case_insensitively(self) -> None:
        self.assertIsNotNone(self.data.pe_basis_ratio(dict(AAPL, financialCurrency="usd ")))


class FetchTickerDataPeBasisTests(unittest.TestCase):
    """The guard as fetch_ticker_data applies it, with Yahoo stubbed."""

    def _fetch(self, info: dict, fx: float) -> dict:
        from unittest import mock
        from ystocker import data

        class _T:
            def __init__(self, _symbol: str) -> None:
                self.info = info

        with mock.patch.object(data.yf, "Ticker", _T), \
             mock.patch.object(data, "usd_rate", lambda _c: fx), \
             mock.patch.object(data.fetchguard, "guard", lambda _p: None):
            return data.fetch_ticker_data("TEST")

    def test_a_disagreeing_listing_loses_its_multiples_and_keeps_its_quote(self) -> None:
        row = self._fetch(TOKIO_MARINE, 0.006335931)
        self.assertIsNone(row["PE (TTM)"])
        self.assertIsNone(row["PE (Forward)"])
        self.assertIsNone(row["PEG"])
        self.assertAlmostEqual(row["Current Price"], 3.2148, places=3)
        self.assertAlmostEqual(row["Market Cap ($B)"], 91.1, places=1)

    def test_an_agreeing_listing_is_untouched(self) -> None:
        row = self._fetch(AAPL, 1.0)
        self.assertEqual(row["PE (TTM)"], AAPL["trailingPE"])
        self.assertEqual(row["PE (Forward)"], AAPL["forwardPE"])
        self.assertEqual(row["PEG"], 2.1)
