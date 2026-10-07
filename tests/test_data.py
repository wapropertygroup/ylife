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


# ---------------------------------------------------------------------------
# Margins and EV multiples on one currency basis (statement_metrics)
# ---------------------------------------------------------------------------
# Yahoo's own answers, read on the box on 2026-10-06. TSM, ASML and BABA quote
# in USD and file in TWD, EUR and CNY; Yahoo's EV and EV ratios for them divide
# one currency by the other (ASML: EV/Revenue 1,123, EV/EBITDA 2,942).
AAPL_INFO = {"quoteType": "EQUITY", "currency": "USD", "financialCurrency": "USD", "marketCap": 4869056364544, "enterpriseValue": 4880201678848, "totalRevenue": 466822987776, "enterpriseToRevenue": 10.454, "enterpriseToEbitda": 29.056, "ebitda": 167959003136, "grossMargins": 0.48653, "operatingMargins": 0.32623002, "freeCashflow": 107721875456, "totalDebt": 84343996416, "totalCash": 62399000576, "industry": "Consumer Electronics"}
TSM_INFO = {"quoteType": "EQUITY", "currency": "USD", "financialCurrency": "TWD", "marketCap": 2501436243968, "enterpriseValue": 17749214494720, "totalRevenue": 4440492343296, "enterpriseToRevenue": 3.997, "enterpriseToEbitda": 5.604, "ebitda": 3167467077632, "grossMargins": 0.6423, "operatingMargins": 0.60344005, "freeCashflow": 730826014720, "totalDebt": 1068558516224, "totalCash": 3518010228736, "industry": "Semiconductors"}
ASML_INFO = {"quoteType": "EQUITY", "currency": "USD", "financialCurrency": "EUR", "marketCap": 704477790208, "enterpriseValue": 39681748107264, "totalRevenue": 35327500288, "enterpriseToRevenue": 1123.254, "enterpriseToEbitda": 2941.917, "ebitda": 13488400384, "grossMargins": 0.52733004, "operatingMargins": 0.37057, "freeCashflow": 8437675008, "totalDebt": 1984400000, "totalCash": 7581499904, "industry": "Semiconductor Equipment & Materials"}
JPM_INFO = {"quoteType": "EQUITY", "currency": "USD", "financialCurrency": "USD", "marketCap": 880603955200, "enterpriseValue": 721455939584, "totalRevenue": 186328006656, "enterpriseToRevenue": 3.872, "enterpriseToEbitda": None, "ebitda": None, "grossMargins": 0.0, "operatingMargins": 0.50394, "freeCashflow": None, "totalDebt": 1343306989568, "totalCash": 1526419030016, "industry": "Banks - Diversified"}
SNOW_INFO = {"quoteType": "EQUITY", "currency": "USD", "financialCurrency": "USD", "marketCap": 118523166720, "enterpriseValue": 119885635584, "totalRevenue": 5434647040, "enterpriseToRevenue": 22.06, "enterpriseToEbitda": -115.688, "ebitda": -1036284992, "grossMargins": 0.6703, "operatingMargins": -0.17001, "freeCashflow": 1739526656, "totalDebt": 2763814912, "totalCash": 2344695040, "industry": "Software - Application"}
SPY_INFO = {"quoteType": "ETF", "currency": "USD", "financialCurrency": "USD", "marketCap": None, "enterpriseValue": None, "totalRevenue": None, "enterpriseToRevenue": None, "enterpriseToEbitda": None, "ebitda": None, "grossMargins": None, "operatingMargins": None, "freeCashflow": None, "totalDebt": None, "totalCash": None, "industry": None}
TWD, EUR = 0.0329, 1.16


class StatementMetricsTests(unittest.TestCase):

    def setUp(self) -> None:
        from ystocker import data

        self.m = data.statement_metrics

    def test_a_us_filer_keeps_yahoos_figures(self) -> None:
        m = self.m(AAPL_INFO, 1.0, 1.0)
        self.assertEqual(m["ev_sales"], 10.45)               # Yahoo's 10.454
        self.assertEqual(m["ev_ebitda"], 29.1)               # Yahoo's 29.056
        self.assertEqual(m["ev_b"], 4880.2)
        self.assertEqual(m["gross_margin"], 48.7)
        self.assertEqual(m["operating_margin"], 32.6)
        self.assertEqual(m["fcf_margin"], 23.1)
        # EV over operating income: 4.880e12 / (0.32623 x 4.668e11).
        self.assertEqual(m["ev_ebit"], 32.0)

    def test_an_adr_is_put_on_one_basis(self) -> None:
        """TSM: USD cap, TWD statements. Yahoo's EV/Revenue was 3.997 and its
        EBITDA, FCF and the P/S fallback were TWD read as dollars."""
        m = self.m(TSM_INFO, 1.0, TWD)
        # EV = 2.501e12 + (1.069e12 - 3.518e12) x 0.0329 = 2.4208e12 USD.
        self.assertEqual(m["ev_b"], 2420.8)
        self.assertEqual(m["ev_sales"], 16.57)
        self.assertEqual(m["ev_ebitda"], 23.2)
        self.assertEqual(m["ev_ebit"], 27.5)
        self.assertEqual(m["ps"], 17.12)
        self.assertEqual(m["fcf_b"], 24.0)                   # not 730.8
        self.assertEqual(m["ebitda_b"], 104.2)               # not 3167.5
        # Margins are ratios within one currency and need no rate.
        self.assertEqual(m["gross_margin"], 64.2)
        self.assertEqual(m["fcf_margin"], 16.5)

    def test_asml_no_longer_reads_a_thousand_times_sales(self) -> None:
        m = self.m(ASML_INFO, 1.0, EUR)
        self.assertEqual(m["ev_sales"], 17.03)
        self.assertEqual(m["ev_ebitda"], 44.6)
        self.assertLess(m["ev_ebit"], 100)

    def test_without_a_rate_an_adr_has_no_ev_multiple_rather_than_a_mixed_one(self) -> None:
        m = self.m(TSM_INFO, 1.0, None)
        for key in ("ev_b", "ev_sales", "ev_ebitda", "ev_ebit", "ps", "fcf_b", "ebitda_b"):
            self.assertIsNone(m[key], key)
        self.assertEqual(m["fcf_margin"], 16.5)

    def test_a_same_currency_listing_needs_no_rate_for_its_ratios(self) -> None:
        """A failed JPY lookup costs the $B figures, not EV/Sales in yen."""
        m = self.m(dict(AAPL_INFO, currency="JPY", financialCurrency="JPY"), None, None)
        self.assertEqual(m["ev_sales"], 10.45)
        self.assertEqual(m["ev_ebit"], 32.0)
        self.assertIsNone(m["ev_b"])

    def test_a_loss_has_no_ebit_or_ebitda_multiple(self) -> None:
        """Yahoo gives SNOW EV/EBITDA -115.7, which sorts as the cheapest."""
        m = self.m(SNOW_INFO, 1.0, 1.0)
        self.assertIsNone(m["ev_ebitda"])
        self.assertIsNone(m["ev_ebit"])
        self.assertEqual(m["ev_sales"], 22.06)
        self.assertEqual(m["operating_margin"], -17.0)

    def test_a_bank_has_no_ev_multiple_and_no_gross_margin(self) -> None:
        m = self.m(JPM_INFO, 1.0, 1.0)
        for key in ("ev_sales", "ev_ebitda", "ev_ebit", "gross_margin", "fcf_margin"):
            self.assertIsNone(m[key], key)
        self.assertEqual(m["ev_b"], 721.5)                   # the figure, not a multiple
        self.assertEqual(m["operating_margin"], 50.4)

    def test_a_negative_ev_is_a_figure_not_a_multiple(self) -> None:
        m = self.m(dict(AAPL_INFO, enterpriseValue=-5e9), 1.0, 1.0)
        self.assertEqual(m["ev_b"], -5.0)
        self.assertIsNone(m["ev_sales"])
        self.assertIsNone(m["ev_ebit"])

    def test_a_fund_has_none_of_it(self) -> None:
        self.assertTrue(all(v is None for v in self.m(SPY_INFO, 1.0, 1.0).values()))

    def test_ps_ratio_refuses_a_mixed_currency_fallback(self) -> None:
        from ystocker import data

        self.assertIsNone(data.ps_ratio(TSM_INFO))           # was 0.56
        self.assertEqual(data.ps_ratio(AAPL_INFO), 10.43)


class FetchTickerDataStatementTests(unittest.TestCase):
    """The ticker record carries the one-basis figures."""

    def _fetch(self, info: dict, rates: dict) -> dict:
        from unittest import mock
        from ystocker import data

        class _T:
            def __init__(self, _symbol: str) -> None:
                self.info = info

        with mock.patch.object(data.yf, "Ticker", _T), \
             mock.patch.object(data, "usd_rate", lambda c: rates.get((c or "USD").upper())), \
             mock.patch.object(data.fetchguard, "guard", lambda _p: None):
            return data.fetch_ticker_data("TEST")

    def test_an_adr_row(self) -> None:
        row = self._fetch(TSM_INFO, {"USD": 1.0, "TWD": TWD})
        self.assertEqual(row["EV/Sales"], 16.57)
        self.assertEqual(row["EV/EBIT"], 27.5)
        self.assertEqual(row["EV/EBITDA"], 23.2)
        self.assertEqual(row["P/S Ratio"], 17.12)
        self.assertEqual(row["FCF ($B)"], 24.0)
        self.assertEqual(row["Gross Margin (%)"], 64.2)
        self.assertEqual(row["Operating Margin (%)"], 60.3)
        self.assertEqual(row["FCF Margin (%)"], 16.5)

    def test_a_us_row_is_unchanged_where_it_was_right(self) -> None:
        row = self._fetch(AAPL_INFO, {"USD": 1.0})
        self.assertEqual(row["EV/EBITDA"], 29.1)
        self.assertEqual(row["EV ($B)"], 4880.2)
        self.assertEqual(row["FCF ($B)"], 107.7)
        self.assertEqual(row["P/S Ratio"], 10.43)
