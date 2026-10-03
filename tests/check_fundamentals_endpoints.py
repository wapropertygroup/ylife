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

    def tearDown(self):
        fundamentals.kick = self._kick
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

    def test_page_carries_the_tab_panel_and_script(self):
        html = self.client.get("/history/NVDA").get_data(as_text=True)
        self.assertIn('id="tabFundBtn"', html)
        self.assertIn('id="panelFund"', html)
        self.assertIn("fundamentals.js", html)
        self.assertIn("_initFundPanel", html)
        self.assertIn("_tab === 'fundamentals'", html)
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


if __name__ == "__main__":
    unittest.main()
