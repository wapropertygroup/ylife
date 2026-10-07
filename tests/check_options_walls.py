"""
An options wall is open interest, and ``/api/options`` says how much.

Through Flask's test client, with ``yfinance.Ticker`` replaced by a fake option
chain. The call (put) wall is the strike where the most call (put) contracts
are still open, summed over the nearest ``_OPTIONS_MAX_EXPIRATIONS`` expiries,
and the payload carries that count as ``call_wall_oi`` / ``put_wall_oi`` so the
page can show it. A strike with no contracts open is not a wall: when Yahoo
reported zero open interest everywhere, ``max()`` named the first strike it saw.

Asked 2026-10-07 ("call wall and put wall should be 未平仓的期权吧"). The
walls always were open interest, but the Chinese page called them 认购墙/认沽墙
and said "across all expirations", so these pin what the copy now claims.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic -- no thread, no secret, no AWS, no network.

Run:  venv/bin/python -m tests.check_options_walls
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-options-walls-secret")
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
from ystocker import routes                               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None


class FakeTicker:
    """An option chain: {expiry: ([(strike, call OI)], [(strike, put OI)])}."""

    chains: dict[str, tuple[list, list]] = {}

    def __init__(self, symbol, *a, **k):
        self.symbol = symbol

    @property
    def options(self):
        return tuple(sorted(self.chains))          # Yahoo lists nearest first

    def option_chain(self, exp):
        calls, puts = self.chains[exp]
        frame = lambda rows: pd.DataFrame(rows, columns=["strike", "openInterest"])
        return types.SimpleNamespace(calls=frame(calls), puts=frame(puts))


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class OptionsWalls(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        routes._OPTIONS_CACHE.clear()
        patcher = mock.patch("yfinance.Ticker", FakeTicker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _walls(self, chains: dict) -> dict:
        FakeTicker.chains = chains
        resp = self.client.get("/api/options/WALLS")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_the_wall_is_the_strike_with_the_most_contracts_open(self):
        """Summed across expiries, not the biggest single row: 120 holds the
        largest call row (800) but 130 holds the most calls in all (1,100)."""
        body = self._walls({
            "2026-10-09": ([(120.0, 800), (130.0, 500)], [(80.0, 600), (100.0, 700)]),
            "2026-10-16": ([(130.0, 600), (140.0, 100)], [(80.0, 650), (100.0, 100)]),
        })
        self.assertEqual((body["call_wall"], body["call_wall_oi"]), (130.0, 1100))
        self.assertEqual((body["put_wall"], body["put_wall_oi"]), (80.0, 1250))

    def test_only_the_nearest_expiries_count(self):
        """The copy says "nearest 12 expiries"; an expiry past the cap holding
        far more contracts must not move either wall."""
        cap = routes._OPTIONS_MAX_EXPIRATIONS
        chains = {f"2026-11-{i + 1:02d}": ([(130.0, 10)], [(80.0, 10)])
                  for i in range(cap)}
        chains["2027-06-18"] = ([(200.0, 10_000)], [(20.0, 10_000)])
        body = self._walls(chains)
        self.assertEqual((body["call_wall"], body["call_wall_oi"]), (130.0, 10 * cap))
        self.assertEqual((body["put_wall"], body["put_wall_oi"]), (80.0, 10 * cap))
        self.assertEqual(len(body["pc_by_expiry"]), cap)

    def test_no_contracts_open_is_no_wall(self):
        """A side with zero open interest everywhere has no wall, rather than
        the first strike in the chain drawn as one."""
        body = self._walls({
            "2026-10-09": ([(120.0, 0), (130.0, 0)], [(80.0, 0), (100.0, 0)]),
        })
        for key in ("call_wall", "call_wall_oi", "put_wall", "put_wall_oi"):
            self.assertIsNone(body[key], key)

        routes._OPTIONS_CACHE.clear()
        body = self._walls({
            "2026-10-09": ([(120.0, 0), (130.0, 40)], [(80.0, 0), (100.0, 0)]),
        })
        self.assertEqual((body["call_wall"], body["call_wall_oi"]), (130.0, 40))
        self.assertIsNone(body["put_wall"])
        self.assertIsNone(body["put_wall_oi"])


if __name__ == "__main__":
    unittest.main()
