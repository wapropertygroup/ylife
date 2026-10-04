"""
A fund or an index is not asked for company data it cannot have.

Through Flask's test client, with ``yfinance.Ticker`` replaced by a fake that
records which properties are touched: ``/api/history`` must not ask for an
ETF's earnings dates or insider trades, ``/api/financials`` must not ask for its
income statements or EPS trend, and both must still ask for a company's. The
analyst sweep must skip what the ticker cache records as a fund, and still sweep
a symbol with no recorded type.

Measured on 2026-10-04: about 270 such requests a day, each answered with a 404
("No fundamentals data found for symbol: SPY"), and each one making yfinance
reset the cookie and crumb every thread in the process shares.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic -- no thread, no secret, no AWS, no network.

Run:  venv/bin/python -m tests.check_history_non_equity
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock


class _Any:
    def __getattr__(self, _name): return _Any()
    def __call__(self, *_a, **_k): return _Any()
    def __getitem__(self, _k): return _Any()
    def __setitem__(self, _k, _v): return None
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False
    def update(self, *_a, **_k): return None


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-non-equity-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import pandas as pd                                       # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import analyst, data                        # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

TYPES = {"QQQX": "ETF", "AAPLX": "EQUITY", "SPY": "ETF", "UNKNOWNX": None}
COMPANY_ONLY = {"earnings_dates", "insider_transactions", "income_stmt", "eps_trend",
                "quarterly_income_stmt"}


class FakeTicker:
    touched: list[tuple[str, str]] = []

    def __init__(self, symbol, *a, **k):
        self.symbol = symbol

    @property
    def info(self):
        qt = TYPES.get(self.symbol)
        return {"quoteType": qt, "shortName": self.symbol, "trailingEps": 5.0} if qt else {"shortName": self.symbol}

    def history(self, period=None, interval=None, **_k):
        idx = pd.date_range("2025-10-01", periods=260, freq="B")
        close = pd.Series(range(100, 360), index=idx, dtype="float64")
        return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                             "Volume": 1_000_000}, index=idx)

    @property
    def history_metadata(self):
        return {"shortName": self.symbol}

    def __getattr__(self, name):
        if name in COMPANY_ONLY:
            FakeTicker.touched.append((self.symbol, name))
            return None
        raise AttributeError(name)


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class NonEquityRequests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        FakeTicker.touched = []
        self.patch = mock.patch("yfinance.Ticker", FakeTicker)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _asked(self, symbol):
        return {name for sym, name in FakeTicker.touched if sym == symbol}

    def test_history_skips_an_etfs_company_data(self):
        resp = self.client.get("/api/history/QQQX")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        self.assertEqual(self._asked("QQQX"), set())
        self.assertEqual(resp.get_json()["insider_trades"], [])

    def test_history_still_asks_for_a_companys(self):
        self.assertEqual(self.client.get("/api/history/AAPLX").status_code, 200)
        self.assertEqual(self._asked("AAPLX"), {"earnings_dates", "insider_transactions"})

    def test_an_unknown_type_is_still_asked(self):
        self.client.get("/api/history/UNKNOWNX")
        self.assertIn("earnings_dates", self._asked("UNKNOWNX"))

    def test_financials_skips_an_etfs_statements_but_keeps_its_prices(self):
        resp = self.client.get("/api/financials/QQQX")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._asked("QQQX"), set())
        self.assertTrue(resp.get_json()["price_zscore"])       # the Z-score still drew on history

    def test_financials_still_asks_for_a_companys(self):
        self.client.get("/api/financials/AAPLX")
        self.assertEqual(self._asked("AAPLX"), {"income_stmt", "eps_trend", "quarterly_income_stmt"})


class SweepTests(unittest.TestCase):
    def test_the_sweep_skips_recorded_funds_only(self):
        records = {"SPY": {"Quote Type": "ETF"}, "GLD": {"Quote Type": "ETF"},
                   "AAPL": {"Quote Type": "EQUITY"}, "MSFT": {}}
        with mock.patch("ystocker.valuation._cached_fundamentals", return_value=records):
            self.assertEqual(analyst._companies(["AAPL", "GLD", "MSFT", "NEWCO", "SPY"]),
                             ["AAPL", "MSFT", "NEWCO"])

    def test_an_unreadable_cache_sweeps_everything(self):
        with mock.patch("ystocker.valuation._cached_fundamentals", side_effect=OSError("gone")):
            self.assertEqual(analyst._companies(["AAPL", "SPY"]), ["AAPL", "SPY"])

    def test_the_rule(self):
        for qt in ("ETF", "etf", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "MONEYMARKET", "FUTURE"):
            self.assertTrue(data.is_non_equity(qt), qt)
        for qt in ("EQUITY", None, "", "SOMETHINGNEW"):
            self.assertFalse(data.is_non_equity(qt), qt)


if __name__ == "__main__":
    unittest.main()
