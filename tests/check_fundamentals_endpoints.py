"""
End-to-end check of the Fundamentals tab on /history, through Flask's test
client: ``/api/fundamentals/<ticker>`` and the page that draws it.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app. Built
hermetically, as ``check_research_endpoints`` is -- no background thread, no
secret, no AWS, no network -- because create_app() would otherwise start the
daily email broadcast and writers to production DynamoDB. The cache directory
is a temporary one, and every build is a stub.

What it pins (the request behind it, 2026-10-03: "read alphascope.trade's
company page and upgrade our website ... it should be available to all the
tickers"):

* the request path only reads the cache: a cold ticker is 202 and one build is
  kicked, a refused slot says ``queued``, a stale copy is served *and* rebuilt;
* a recent build failure is a 503 the page can stop polling on, and
  ``?retry=1`` is the reader's way past it;
* an ``unavailable`` answer (not an SEC filer, not a company) is a 200, cached,
  not a failure;
* the build order: EDGAR, then Yahoo's statements only where SEC has nothing,
  and an index or a coin answered without asking either;
* the page carries the tab, the panel, the script and the ``?tab=`` link, and
  every card the script draws has a title in both languages.

Run:  venv/bin/python -m tests.check_fundamentals_endpoints
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-fundamentals-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import fundamentals                         # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "ystocker" / "templates" / "history.html"
I18N = ROOT / "ystocker" / "static" / "i18n.js"


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


def _payload(symbol, **extra):
    body = {"ticker": symbol, "source": "sec", "entity": "Check Co",
            "basis": {"taxonomy": "us-gaap", "currency": "USD", "filer": "domestic"},
            "quarterly": {"end": ["2026-04-26"], "start": [None], "label": ["Q1 FY2027"],
                          "values": {"revenue": [81_615_000_000]}, "how": {}},
            "ttm": {"end": ["2026-04-26"], "values": {"revenue": [None]}},
            "annual": {"end": [], "start": [], "label": [], "values": {}, "how": {}},
            "valuation": None, "concepts": {}, "latest": None, "notes": []}
    body.update(extra)
    return fundamentals._stamped(body, fundamentals.TTL_SECONDS)


class FundamentalsEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._dir, fundamentals.CACHE_DIR = fundamentals.CACHE_DIR, Path(self.tmp.name)
        fundamentals._memo.clear()
        self.kicks = []
        self._kick = fundamentals.kick
        fundamentals.kick = lambda s: (self.kicks.append(s), self.kick_result)[1]
        self.kick_result = True
        self._capped = fundamentals.capped
        self.cap_spent = False
        fundamentals.capped = lambda: self.cap_spent

    def tearDown(self):
        fundamentals.kick = self._kick
        fundamentals.capped = self._capped
        fundamentals.CACHE_DIR = self._dir
        fundamentals._memo.clear()
        self.tmp.cleanup()

    def test_invalid_ticker_is_400(self):
        resp = self.client.get("/api/fundamentals/..%2Fetc")
        self.assertIn(resp.status_code, (400, 404))
        resp = self.client.get("/api/fundamentals/a b")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.kicks, [])

    def test_cold_ticker_is_202_and_kicks_one_build(self):
        resp = self.client.get("/api/fundamentals/nvda")
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertEqual((body["status"], body["queued"]), ("warming", False))
        self.assertEqual(self.kicks, ["NVDA"])

    def test_refused_slot_says_queued(self):
        self.kick_result = False
        body = self.client.get("/api/fundamentals/NVDA").get_json()
        self.assertTrue(body["queued"])

    def test_cached_payload_is_served_without_stamps(self):
        fundamentals._write_json(fundamentals._path("NVDA"), _payload("NVDA"))
        resp = self.client.get("/api/fundamentals/NVDA")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["quarterly"]["values"]["revenue"], [81_615_000_000])
        self.assertFalse(body["stale"])
        self.assertIsInstance(body["as_of"], float)
        self.assertFalse([k for k in body if k.startswith("_")])
        self.assertEqual(self.kicks, [])

    def test_stale_payload_is_served_and_rebuilt(self):
        old = _payload("NVDA")
        old["_ts"] = time.time() - fundamentals.TTL_SECONDS - 60
        fundamentals._write_json(fundamentals._path("NVDA"), old)
        body = self.client.get("/api/fundamentals/NVDA").get_json()
        self.assertTrue(body["stale"])
        self.assertEqual(self.kicks, ["NVDA"])

    def test_a_payload_from_an_older_revision_is_served_and_rebuilt(self):
        # Built before the new metrics existed: drawn as it is, rebuilt behind.
        old = _payload("NVDA")
        old.pop("_rev")
        fundamentals._write_json(fundamentals._path("NVDA"), old)
        resp = self.client.get("/api/fundamentals/NVDA")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["stale"])
        self.assertEqual(self.kicks, ["NVDA"])

    def test_a_fresh_build_carries_the_current_revision(self):
        self.assertEqual(_payload("NVDA")["_rev"], fundamentals.CACHE_REV)
        self.assertFalse(fundamentals.is_stale(_payload("NVDA")))

    def test_an_unavailable_answer_is_not_rebuilt_for_a_new_revision(self):
        # An ETF or a non-filer gains nothing from a new metric.
        old = fundamentals._stamped({"ticker": "SPY", "unavailable": "not_a_company"},
                                    fundamentals.UNAVAILABLE_TTL_SECONDS)
        old.pop("_rev")
        self.assertFalse(fundamentals.is_stale(old))

    def test_a_stale_copy_during_an_outage_does_not_ask_again(self):
        # SEC is down: every view of a stale ticker would otherwise re-send the
        # request that just failed.
        old = _payload("NVDA")
        old["_ts"] = time.time() - fundamentals.TTL_SECONDS - 60
        fundamentals._write_json(fundamentals._path("NVDA"), old)
        fundamentals._write_json(fundamentals._fail_path("NVDA"),
                                 {"ts": time.time(), "reason": "sec_unavailable"})
        resp = self.client.get("/api/fundamentals/NVDA")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["stale"])
        self.assertEqual(self.kicks, [])

    def test_a_payload_from_an_older_shape_is_not_served(self):
        fundamentals._write_json(fundamentals._path("NVDA"), dict(_payload("NVDA"), _ver="v0"))
        self.assertEqual(self.client.get("/api/fundamentals/NVDA").status_code, 202)

    def test_recent_failure_is_503_until_the_reader_retries(self):
        fundamentals._write_json(fundamentals._fail_path("NVDA"),
                                 {"ts": time.time(), "reason": "sec_unavailable"})
        resp = self.client.get("/api/fundamentals/NVDA")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["reason"], "sec_unavailable")
        self.assertEqual(self.kicks, [])
        resp = self.client.get("/api/fundamentals/NVDA?retry=1")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.kicks, ["NVDA"])

    def test_an_old_failure_no_longer_blocks(self):
        fundamentals._write_json(fundamentals._fail_path("NVDA"),
                                 {"ts": time.time() - fundamentals.FAIL_TTL_SECONDS - 5,
                                  "reason": "sec_unavailable"})
        self.assertEqual(self.client.get("/api/fundamentals/NVDA").status_code, 202)

    def test_a_spent_daily_allowance_is_a_message_not_a_spinner(self):
        # Once today's builds are spent, a cold ticker must not be polled for a
        # build that will not start until tomorrow -- and a cached one is still
        # served.
        self.cap_spent = True
        resp = self.client.get("/api/fundamentals/NVDA")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["reason"], "daily_cap")
        self.assertEqual(self.kicks, [])
        fundamentals._write_json(fundamentals._path("AAPL"), _payload("AAPL"))
        self.assertEqual(self.client.get("/api/fundamentals/AAPL").status_code, 200)

    def test_the_cap_is_spent_by_a_build_and_then_refuses(self):
        import tempfile as _tf
        from ystocker import quota
        saved = (quota.QUOTA_DIR, quota._LOCK_PATH, os.environ.get("FUNDAMENTALS_DAILY_BUILDS"))
        quota.QUOTA_DIR = Path(_tf.mkdtemp())
        quota._LOCK_PATH = quota.QUOTA_DIR / ".lock"
        os.environ["FUNDAMENTALS_DAILY_BUILDS"] = "1"
        fundamentals.kick, fundamentals.capped = self._kick, self._capped
        real_start, threading.Thread.start = threading.Thread.start, lambda t: None
        try:
            fundamentals._last_start = 0.0
            self.assertTrue(fundamentals.kick("ZZCAP1"))
            fundamentals._release()
            fundamentals._building.clear()
            fundamentals._last_start = 0.0
            self.assertFalse(fundamentals.kick("ZZCAP2"))       # the one build is spent
            self.assertTrue(fundamentals.capped())
        finally:
            threading.Thread.start = real_start
            fundamentals._building.clear()
            quota.QUOTA_DIR, quota._LOCK_PATH = saved[0], saved[1]
            if saved[2] is None:
                os.environ.pop("FUNDAMENTALS_DAILY_BUILDS", None)
            else:
                os.environ["FUNDAMENTALS_DAILY_BUILDS"] = saved[2]

    def test_unavailable_is_an_answer(self):
        fundamentals._write_json(fundamentals._path("SPY"),
                                 fundamentals._stamped({"ticker": "SPY", "unavailable": "not_a_company"},
                                                       fundamentals.UNAVAILABLE_TTL_SECONDS))
        resp = self.client.get("/api/fundamentals/SPY")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["unavailable"], "not_a_company")


class BuildOrder(unittest.TestCase):
    """EDGAR first, Yahoo only where SEC has nothing, and nothing for a coin."""

    def setUp(self):
        self.calls = []
        self._sec, self._yahoo = fundamentals._build_sec, fundamentals._build_yahoo

    def tearDown(self):
        fundamentals._build_sec, fundamentals._build_yahoo = self._sec, self._yahoo

    def stub(self, sec, yahoo):
        fundamentals._build_sec = lambda s: (self.calls.append("sec"), dict(sec, ticker=s))[1]
        fundamentals._build_yahoo = lambda s: (self.calls.append("yahoo"), dict(yahoo, ticker=s))[1]

    def test_sec_wins_when_it_has_statements(self):
        self.stub({"source": "sec", "quarterly": {}}, {"source": "yahoo"})
        self.assertEqual(fundamentals.build("NVDA")["source"], "sec")
        self.assertEqual(self.calls, ["sec"])

    def test_yahoo_stands_in_for_a_listing_sec_does_not_cover(self):
        self.stub({"unavailable": "not_sec_filer"}, {"source": "yahoo", "quarterly": {}})
        self.assertEqual(fundamentals.build("7203.T")["source"], "yahoo")
        self.assertEqual(self.calls, ["sec", "yahoo"])

    def test_neither_keeps_secs_reason_unless_it_is_not_a_company(self):
        self.stub({"unavailable": "not_sec_filer"}, {"unavailable": "no_statements"})
        self.assertEqual(fundamentals.build("ZZZZ")["unavailable"], "not_sec_filer")
        self.stub({"unavailable": "no_xbrl"}, {"unavailable": "not_a_company"})
        self.assertEqual(fundamentals.build("SPY")["unavailable"], "not_a_company")

    def test_an_index_or_a_coin_asks_nobody(self):
        self.stub({"source": "sec"}, {"source": "yahoo"})
        for symbol in ("^GSPC", "BTC-USD", "EURUSD=X", "ES=F"):
            self.assertEqual(fundamentals.build(symbol)["unavailable"], "not_a_company")
        self.assertEqual(self.calls, [])


class FailureMarker(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._dir, fundamentals.CACHE_DIR = fundamentals.CACHE_DIR, Path(self.tmp.name)
        self._build = fundamentals.build

    def tearDown(self):
        fundamentals.build = self._build
        fundamentals.CACHE_DIR = self._dir
        self.tmp.cleanup()

    def test_failure_is_remembered_and_success_clears_it(self):
        def boom(_s):
            raise fundamentals.BuildError("sec_cooldown", "test")
        fundamentals.build = boom
        with self.assertRaises(fundamentals.BuildError):
            fundamentals.refresh("NVDA")
        self.assertEqual(fundamentals.recent_failure("NVDA")["reason"], "sec_cooldown")
        fundamentals.build = lambda s: _payload(s)
        fundamentals.refresh("NVDA")
        self.assertIsNone(fundamentals.recent_failure("NVDA"))
        self.assertIsNotNone(fundamentals.peek("NVDA"))

    def test_budget_release_floors_at_zero(self):
        fundamentals._release()
        fundamentals._release()
        self.assertGreaterEqual(fundamentals._inflight, 0)


class Page(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def test_companies_directory_is_one_card_per_company_largest_first(self):
        from ystocker import routes
        data = {
            "Tech": {"MSFT": {"Name": "Microsoft", "Current Price": 517.5, "Day Change (%)": 0.9,
                              "Market Cap ($B)": 3842.9, "PE (TTM)": 28.6, "52W Return (%)": -2.1},
                     "AAPL": {"Name": "Apple", "Current Price": 333.7, "Day Change (%)": float("nan"),
                              "Market Cap ($B)": 4910.1}},
            "Software": {"MSFT": {"Name": "Microsoft", "Market Cap ($B)": 3842.9}},
        }
        rows = routes._company_cards(data)
        self.assertEqual([r["t"] for r in rows], ["AAPL", "MSFT"])
        self.assertEqual(rows[1]["g"], ["Tech", "Software"])      # one card, every group
        self.assertIsNone(rows[0]["c"])                            # NaN is not a 0% day
        self.assertEqual(routes._company_cards(None), [])

    def test_companies_page_renders_and_links_into_the_tab(self):
        from ystocker import routes
        saved = routes._cache
        routes._cache = {"Tech": {"MSFT": {"Name": "Microsoft", "Current Price": 517.5,
                                           "Day Change (%)": 0.9, "Market Cap ($B)": 3842.9}}}
        try:
            html = self.client.get("/companies").get_data(as_text=True)
        finally:
            routes._cache = saved
        self.assertIn('"t": "MSFT"', html)
        self.assertIn("?tab=fundamentals", html)
        self.assertIn('data-i18n="companies.title"', html)
        i18n = I18N.read_text()
        for key in re.findall(r"tr\('(companies\.[a-z0-9_]+)'", (ROOT / "ystocker/templates/companies.html").read_text()):
            self.assertRegex(i18n, r"'%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % re.escape(key))

    def test_companies_page_survives_a_cold_cache(self):
        from ystocker import routes
        saved = routes._cache
        routes._cache = None
        try:
            resp = self.client.get("/companies")
        finally:
            routes._cache = saved
        self.assertEqual(resp.status_code, 200)
        self.assertIn("const WARMING = true", resp.get_data(as_text=True))

    def test_page_carries_the_tab_panel_and_script(self):
        html = self.client.get("/history/NVDA").get_data(as_text=True)
        self.assertIn('id="tabFundBtn"', html)
        self.assertIn('id="panelFund"', html)
        self.assertIn("fundamentals.js", html)
        self.assertIn("_initFundPanel", html)
        self.assertRegex(html, r"const HISTORY_TABS = \[[^\]]*'fundamentals'")
        self.assertIn("HISTORY_TABS.includes(_tab)", html)      # ?tab=fundamentals opens it
        # The expand modal sits outside #panelCharts, or a Fundamentals card
        # would open a modal inside a display:none panel.
        charts_end = html.index("end #panelCharts")
        self.assertGreater(html.index('id="chartModal"'), charts_end)

    def test_every_card_has_a_title_in_both_languages(self):
        source = HISTORY.read_text()
        keys = re.findall(r"\{ key: '([a-z_]+)', metrics:", source)
        self.assertGreaterEqual(len(keys), 12)
        i18n = I18N.read_text()
        for key in keys:
            m = re.search(r"'fund\.card_%s':\s*\{\s*en: '([^']+)',\s*zh: '([^']+)'" % key, i18n)
            self.assertIsNotNone(m, f"fund.card_{key} missing or empty")
        metrics = set(re.findall(r"metrics: \[([^\]]+)\]", source))
        for group in metrics:
            for metric in re.findall(r"'([a-z_]+)'", group):
                self.assertRegex(i18n, r"'fund\.s_%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % metric)

    # Comparing companies (asked 2026-10-06): the suggestions route, the row on
    # the page, and its strings in both languages.

    def test_suggestions_come_from_the_followed_companies_and_secs_list(self):
        from unittest import mock

        from ystocker import directory, routes, symbols
        sec = {"rows": [
            {"t": "AMAT", "n": "Applied Materials Inc", "x": "Nasdaq", "also": [], "cik": 1},
            {"t": "MDLZ", "n": "Mondelez International, Inc.", "x": "Nasdaq", "also": [], "cik": 2},
        ]}
        followed = next(t for ts in routes.PEER_GROUPS.values() for t in ts if t.isalpha())
        with mock.patch.object(directory, "peek", return_value=sec), \
                mock.patch.object(symbols, "quotes", return_value=None):
            by_ticker = self.client.get("/api/companies/suggest?q=amat")
            by_name = self.client.get("/api/companies/suggest?q=mondelez").get_json()
            ours = self.client.get(f"/api/companies/suggest?q={followed}").get_json()
            empty = self.client.get("/api/companies/suggest?q=%20").get_json()
        self.assertEqual(by_ticker.status_code, 200)
        self.assertIn("public", by_ticker.headers.get("Cache-Control", ""))
        first = by_ticker.get_json()["results"][0]
        self.assertEqual((first["ticker"], first["exchange"]), ("AMAT", "Nasdaq"))
        self.assertEqual([r["ticker"] for r in by_name["results"]], ["MDLZ"])
        self.assertEqual(by_name["sources"], ["local"])
        self.assertIn(followed, [r["ticker"] for r in ours["results"]])
        self.assertEqual(empty, {"results": [], "sources": []})

    def test_suggestions_survive_a_cold_directory(self):
        from unittest import mock

        from ystocker import directory, symbols
        with mock.patch.object(directory, "peek", return_value=None), \
                mock.patch.object(symbols, "quotes", return_value=None):
            resp = self.client.get("/api/companies/suggest?q=zzzz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"results": [], "sources": ["local"]})

    # "TSMC ticker should have auto complete" (2026-10-06). TSMC is nobody's
    # ticker. SEC's list knows it by initials; Yahoo's answer, as served to the
    # box, holds receipts in São Paulo and Buenos Aires and Tesmec, but no TSM.
    _TSM_SEC = {"rows": [
        {"t": "TSM", "n": "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "x": "NYSE", "also": [], "cik": 1046179},
        {"t": "TSLA", "n": "Tesla, Inc.", "x": "Nasdaq", "also": [], "cik": 1318605},
    ]}

    def _yahoo(self, name):
        import json
        got = json.loads((ROOT / "tests" / "fixtures" / "symbols" / "yahoo.json").read_text())[name]
        return got["status"], got["body"]

    def test_an_abbreviation_finds_its_company_before_yahoo(self):
        from unittest import mock

        from ystocker import directory, quota, symbols
        spent = []
        with mock.patch.object(directory, "peek", return_value=self._TSM_SEC), \
                mock.patch.object(symbols, "_searches", {}), \
                mock.patch.object(symbols, "_get", return_value=self._yahoo("search_TSMC")) as get, \
                mock.patch.object(quota, "try_consume_suggest_search",
                                  side_effect=lambda: spent.append(1) or True):
            got = self.client.get("/api/companies/suggest?q=TSMC").get_json()
            again = self.client.get("/api/companies/suggest?q=tsmc").get_json()
        tickers = [r["ticker"] for r in got["results"]]
        self.assertEqual(tickers[0], "TSM")
        # Then Yahoo's companies: no tokens, no fund.
        self.assertEqual(tickers[1:], ["TSMC34.SA", "TSMC.BA", "TSMCF"])
        self.assertEqual(got["sources"], ["local", "yahoo"])
        # Asked again within six hours: from memory, spending nothing.
        self.assertEqual(again, got)
        self.assertEqual((get.call_count, len(spent)), (1, 1))

    def test_yahoo_is_asked_only_while_the_list_is_short(self):
        from unittest import mock

        from ystocker import directory, symbols
        sec = {"rows": [{"t": f"AB{c}", "n": f"Company {c}", "x": "NYSE", "also": [], "cik": i}
                        for i, c in enumerate("CDEFGHIJKL")]}
        with mock.patch.object(directory, "peek", return_value=sec), \
                mock.patch.object(symbols, "quotes") as quotes:
            got = self.client.get("/api/companies/suggest?q=ab").get_json()
        quotes.assert_not_called()
        self.assertEqual((len(got["results"]), got["sources"]), (8, ["local"]))

    def test_past_the_daily_allowance_the_local_lists_answer_alone(self):
        from unittest import mock

        from ystocker import directory, quota, symbols
        with mock.patch.object(directory, "peek", return_value=self._TSM_SEC), \
                mock.patch.object(symbols, "_searches", {}), \
                mock.patch.object(symbols, "_get") as get, \
                mock.patch.object(quota, "try_consume_suggest_search", return_value=False):
            got = self.client.get("/api/companies/suggest?q=TSMC").get_json()
        get.assert_not_called()
        self.assertEqual(([r["ticker"] for r in got["results"]], got["sources"]), (["TSM"], ["local"]))

    def test_page_carries_the_compare_row(self):
        html = self.client.get("/history/NVDA").get_data(as_text=True)
        self.assertIn('id="fundCompare"', html)
        self.assertIn('aria-controls="fundCmpList"', html)
        self.assertIn("/api/companies/suggest?q=", html)
        self.assertIn(".get('compare')", html)                 # ?compare= is read
        self.assertIn("searchParams.set('compare'", html)      # and written back
        self.assertIn("F.calendarKey", html)                   # periods line up by calendar
        self.assertIn("data-fund-swap", html)                  # a dead symbol offers what it meant
        self.assertIn("offerAlternatives(entry)", html)

    def test_every_compare_string_is_in_both_languages(self):
        source = HISTORY.read_text()
        i18n = I18N.read_text()
        keys = set(re.findall(r"tr\('(fund\.(?:cmp_[a-z_]+|peers|note_compare))'", source))
        keys |= {"fund.compare", "fund.compare_ph",
                 # A margin's compare card is titled by its own key, the legend's
                 # "Gross" being too short to stand alone.
                 "fund.t_gross_margin", "fund.t_operating_margin", "fund.t_net_margin"}
        self.assertGreaterEqual(len(keys), 14)
        for key in keys:
            self.assertRegex(i18n, r"'%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % re.escape(key))


if __name__ == "__main__":
    unittest.main()
