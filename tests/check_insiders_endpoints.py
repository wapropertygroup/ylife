"""
End-to-end check of the insider-trades routes through Flask's test client:
``/api/insiders``, ``/api/insiders/<ticker>`` and ``/insiders``.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic, as the other endpoint checks are -- no background thread, no secret,
no AWS, no network. ``create_app()`` would otherwise start this laptop's
writers with its production credentials (CLAUDE.md, Known Pitfalls), so AWS is
stripped, SSM and dotenv are stubbed and ``Thread.start`` is a no-op while it
runs. The cache is a temporary directory holding records built from real
Form 4s (tests/fixtures/insiders/); "today" is pinned; SEC's ticker map is a
stub; the look-up queue is a mock that counts what it was asked for.

What it pins:

* a cached feed is a 200 with rows, cluster buys and coverage; buys are the
  default, the largest first, and joint filers are one buyer;
* a feed whose universe is not all checked is a 202 that still serves what is
  in, and with no universe at all a 202 with nothing;
* ``followed`` and ``held`` name only the answer's tickers, and a position
  store that cannot be read costs the highlight, not the page;
* one company: a cached one is served with every kind of trade; a cold one
  queues exactly that ticker; a fund, an unknown ticker and a malformed one
  never queue anything; a recent failure and a spent allowance are 503s;
  a stale company outside the followed set is refreshed on view, and a
  followed one is left to the sweep;
* the page carries its controls, marks its own tab as current on both hosts,
  and every string it composes exists in both languages.

Run:  venv/bin/python -m tests.check_insiders_endpoints
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-insiders-secret")
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

import ystocker                                           # noqa: E402
from ystocker import insiders as ins                      # noqa: E402
from ystocker import routes                               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_insiders import (AAPL_NO_LINES, AAPL_PLAN_SALE, GME_ALL,      # noqa: E402
                           LEN_BERKSHIRE, NVDA_STEVENS, NVDA_TETER, record)

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "ystocker" / "static" / "i18n.js"
TEMPLATE = ROOT / "ystocker" / "templates" / "insiders.html"
TODAY = dt.date(2026, 10, 4)
TA = "http://trade-agents.com"
UNIVERSE = [{"cik": 320193, "tickers": ["AAPL"], "name": "Apple Inc."},
            {"cik": 1045810, "tickers": ["NVDA"], "name": "NVIDIA Corporation"},
            {"cik": 920760, "tickers": ["LEN"], "name": "Lennar Corporation"}]
TICKER_MAP = {"AAPL": (320193, "Apple Inc."), "NVDA": (1045810, "NVIDIA CORP"),
              "LEN": (920760, "LENNAR CORP /NEW/"), "GME": (1326380, "GameStop Corp."),
              "MSFT": (789019, "MICROSOFT CORP"), "SPY": (884394, "SPDR S&P 500 ETF TRUST")}


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class InsidersEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.kick = mock.Mock(return_value="queued")
        self.patches = [
            mock.patch.object(ins, "CACHE_DIR", self.dir),
            mock.patch.object(ins, "kick", self.kick),
            mock.patch.object(ins, "today_et", lambda now=None: TODAY),
            mock.patch.object(ins, "_map_peek", lambda: TICKER_MAP),
            mock.patch.object(routes, "_cache", {
                "Tech": {"AAPL": {"Quote Type": "EQUITY"}, "NVDA": {"Quote Type": "EQUITY"}},
                "Homebuilders & Construction": {"LEN": {"Quote Type": "EQUITY"}},
                "US Broad ETFs": {"SPY": {"Quote Type": "ETF"}},
            }),
        ]
        for p in self.patches:
            p.start()
        self._clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._clear()
        self.tmp.cleanup()

    def _clear(self):
        ins._digests.clear()
        ins._universe_memo.update(mtime=None, value=None, ciks=set(), by_ticker={})
        ins._failed.clear()
        ins._no_cik.clear()

    def _universe(self, issuers=UNIVERSE):
        (self.dir / ins.UNIVERSE_FILE).write_text(json.dumps({"ts": time.time(), "issuers": issuers}))

    def _store(self, cik, accs, ticker, name, age=60):
        (self.dir / f"{cik}.json").write_text(json.dumps(
            record(cik, accs, ticker=ticker, name=name, checked=time.time() - age, covered="2026-07-07")))

    def _fill(self):
        self._universe()
        self._store(320193, [AAPL_PLAN_SALE, AAPL_NO_LINES], "AAPL", "Apple Inc.")
        self._store(1045810, [NVDA_TETER, NVDA_STEVENS], "NVDA", "NVIDIA Corporation")
        self._store(920760, [LEN_BERKSHIRE], "LEN", "Lennar Corporation")

    # ── the feed ───────────────────────────────────────────────────────────

    def test_a_cached_feed_is_served_whole(self):
        self._fill()
        resp = self.client.get("/api/insiders?days=30&kind=all&sort=value")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual((body["days"], body["kind"], body["sort"], body["since"]), (30, "all", "value", "2026-09-05"))
        self.assertEqual([r["a"] for r in body["rows"]][:1], [NVDA_STEVENS])     # ~$300M, largest first
        self.assertEqual(sorted(r["k"] for r in body["rows"]), ["buy", "buy", "sell", "sell", "sell"])
        self.assertEqual(body["total"], 5)
        self.assertFalse(body["truncated"])
        self.assertEqual(body["clusters"], [])            # Berkshire and Buffett are one buyer
        cov = body["coverage"]
        self.assertEqual((cov["universe"], cov["checked"], cov["pending"], cov["extra"]), (3, 3, 0, 0))
        self.assertEqual(body["followed"], ["AAPL", "LEN", "NVDA"])
        self.assertEqual(body["held"], [])
        self.assertNotIn("present", body)
        self.assertFalse(any(k.startswith("_") for r in body["rows"] for k in r))
        self.kick.assert_not_called()

    def test_buys_are_the_default_and_cluster_buys_ride_along(self):
        self._fill()
        self._store(1326380, GME_ALL, "GME", "GameStop Corp.")      # a company a reader looked up
        body = self.client.get("/api/insiders").get_json()
        self.assertEqual((body["days"], body["kind"], body["sort"]), (30, "buy", "value"))
        self.assertEqual({r["t"] for r in body["rows"]}, {"GME", "LEN"})
        (cluster,) = body["clusters"]
        self.assertEqual((cluster["t"], cluster["insiders"]), ("GME", 5))
        self.assertEqual(body["coverage"]["extra"], 1)
        self.assertEqual(body["followed"], ["LEN"])                  # GME is not followed here

    def test_a_feed_not_yet_whole_is_a_202_with_what_is_in(self):
        self._fill()
        self._universe(UNIVERSE + [{"cik": 789019, "tickers": ["MSFT"], "name": "Microsoft"}])
        resp = self.client.get("/api/insiders?kind=all")
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertEqual(body["status"], "warming")
        self.assertEqual((body["coverage"]["checked"], body["coverage"]["pending"]), (3, 1))
        self.assertEqual(body["total"], 5)
        self.kick.assert_not_called()                     # the sweep's job, not the request's

    def test_no_universe_yet_is_a_202_with_nothing(self):
        resp = self.client.get("/api/insiders")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["rows"], [])

    def test_bad_parameters_are_refused_and_long_windows_clamped(self):
        for q in ("days=abc", "kind=bought", "sort=biggest"):
            self.assertEqual(self.client.get("/api/insiders?" + q).status_code, 400, q)
        self._fill()
        self.assertEqual(self.client.get("/api/insiders?days=1000").get_json()["days"], 90)

    def test_holdings_mark_only_this_answer_s_tickers(self):
        self._fill()
        with self.client.session_transaction() as sess:
            sess["user_email"] = "reader@example.com"
        with mock.patch("ystocker.portfolio.load", return_value=[{"symbol": "NVDA"}, {"symbol": "TSLA"}]):
            body = self.client.get("/api/insiders?kind=all").get_json()
        self.assertEqual(body["held"], ["NVDA"])

    def test_an_unreadable_position_store_costs_the_highlight_not_the_page(self):
        self._fill()
        with self.client.session_transaction() as sess:
            sess["user_email"] = "reader@example.com"
        from ystocker import portfolio
        with mock.patch("ystocker.portfolio.load", side_effect=portfolio.StoreUnavailable("down")):
            resp = self.client.get("/api/insiders?kind=all")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["held"], [])

    # ── one company ────────────────────────────────────────────────────────

    def test_a_cached_company_has_every_kind(self):
        self._fill()
        resp = self.client.get("/api/insiders/NVDA")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual((body["status"], body["ticker"], body["cik"], body["days"]), ("ok", "NVDA", 1045810, 90))
        self.assertEqual([r["a"] for r in body["rows"]], [NVDA_TETER, NVDA_STEVENS])   # newest first
        self.assertTrue(body["followed"])
        self.assertFalse(body["refreshing"])
        self.kick.assert_not_called()

    def test_a_cold_company_queues_just_that_issuer(self):
        self._fill()
        resp = self.client.get("/api/insiders/msft")
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertEqual((body["status"], body["ticker"], body["queued"]), ("pending", "MSFT", True))
        self.kick.assert_called_once_with("MSFT")

    def test_funds_unknown_and_malformed_tickers_queue_nothing(self):
        self._fill()
        self.assertEqual(self.client.get("/api/insiders/SPY").get_json()["status"], "not_a_company")
        self.assertEqual(self.client.get("/api/insiders/ZZZZQ").get_json()["status"], "not_sec_filer")
        self.assertEqual(self.client.get("/api/insiders/a%20b").status_code, 400)
        self.assertEqual(self.client.get("/api/insiders/AAPL?days=x").status_code, 400)
        self.kick.assert_not_called()

    def test_a_recent_failure_and_a_spent_allowance_are_503s(self):
        self._fill()
        ins._failed["MSFT"] = time.time()
        resp = self.client.get("/api/insiders/MSFT")
        self.assertEqual((resp.status_code, resp.get_json()["status"]), (503, "failed"))
        self.kick.assert_not_called()
        self.assertEqual(self.client.get("/api/insiders/MSFT?retry=1").status_code, 202)
        self.kick.return_value = "capped"
        ins._failed.clear()
        resp = self.client.get("/api/insiders/MSFT")
        self.assertEqual((resp.status_code, resp.get_json()["reason"]), (503, "daily_cap"))

    def test_a_stale_look_up_is_refreshed_and_a_followed_company_left_to_the_sweep(self):
        self._fill()
        self._store(1326380, GME_ALL, "GME", "GameStop Corp.", age=7 * 3600)
        self._store(1045810, [NVDA_TETER, NVDA_STEVENS], "NVDA", "NVIDIA Corporation", age=7 * 3600)
        body = self.client.get("/api/insiders/GME").get_json()
        self.assertEqual((body["status"], body["followed"], body["stale"], body["refreshing"]), ("ok", False, True, True))
        self.kick.assert_called_once_with("GME")
        self.kick.reset_mock()
        body = self.client.get("/api/insiders/NVDA").get_json()
        self.assertEqual((body["stale"], body["refreshing"]), (True, False))
        self.kick.assert_not_called()

    # ── the page ───────────────────────────────────────────────────────────

    def test_the_page_carries_its_controls_and_marks_companies(self):
        html = self.client.get("/insiders").get_data(as_text=True)
        for needle in ('id="inSummary"', 'id="inClusters"', 'id="inList"', 'id="inLookup"',
                       'data-in-kind="buy"', 'data-in-kind="sell"', 'data-in-kind="all"',
                       'data-in-days="7"', 'data-in-days="30"', 'data-in-days="90"',
                       'data-in-sort="value"', 'data-in-sort="newest"',
                       'id="inFollowed"', 'id="inHeld"', 'id="inSearch"', "/api/insiders"):
            self.assertIn(needle, html, needle)
        nav = re.search(r'<nav data-nav="desktop".*?</nav>', html, re.S).group(0)
        current = re.findall(r'href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current, ["/insiders"])
        self.assertGreaterEqual(html.count('href="/insiders"'), 2)       # the drawer and the footer

    def test_on_trade_agents_com_the_markets_bar_marks_its_tab(self):
        html = self.client.get("/insiders", base_url=TA).get_data(as_text=True)
        bar = re.search(r'<nav class="w-sub".*?</nav>', html, re.S).group(0)
        self.assertEqual(re.findall(r'is-current" href="([^"]+)"', bar), ["/insiders"])
        footer = re.search(r'<footer class="w-footer">.*?</footer>', html, re.S).group(0)
        self.assertIn('href="/insiders"', footer)

    def test_every_composed_string_exists_in_both_languages(self):
        tpl = TEMPLATE.read_text()
        keys = set(re.findall(r"'(insiders\.[a-z0-9_]+)'", tpl))
        keys |= set(re.findall(r'data-i18n(?:-title|-placeholder)?="(insiders\.[a-z0-9_]+)"', tpl))
        keys |= set(re.findall(r"\{% block title_key %\}(insiders\.[a-z0-9_]+)", tpl))
        keys |= {"nav.insiders"}
        self.assertGreater(len(keys), 60)
        i18n = I18N.read_text()
        for key in sorted(keys):
            self.assertRegex(i18n, r"'%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % re.escape(key), key)


if __name__ == "__main__":
    unittest.main()
