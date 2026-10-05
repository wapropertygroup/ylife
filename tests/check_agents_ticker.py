"""
End-to-end check of the run form's ticker handling on /agents, through Flask's
test client (asked 2026-10-05: "股票代码 should have auto complete and stop
analyze if the ticker is not found", and "you don't have to output the error
stack trace").

ystocker.symbols is tested on its own in ``tests/test_symbols.py``; this pins
the routes around it:
* a ticker with no prices is refused before the quota is touched, with Yahoo's
  listings for it, and a dotted symbol with none of its own falls back to its
  base ("TCS.NY" offers TCS.NS);
* a check that cannot be made, and a malformed ticker, still reach the quota
  (and so submit(), which owns the format refusal);
* the job API serves the runner's output without the traceback the record keeps;
* the suggestions route is signed in only and hands symbols the followed list;
* the page carries the combobox and the refusal's wiring.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Built hermetically, as ``check_signup_route`` is -- no background thread, no
secret, no AWS, no network. Yahoo, the quota and the job store are stubs.

Run:  venv/bin/python -m tests.check_agents_ticker
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-agents-ticker-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS", "SES_FROM_EMAIL"):
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
from ystocker import agents, quota, symbols               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

READER = "reader@example.com"
TCS_NS = {"ticker": "TCS.NS", "name": "Tata Consultancy Services Limited",
          "exchange": "NSE", "type": "EQUITY"}


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class AgentsTicker(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_email"] = READER
        self.checked, self.searched, self.consumed = [], [], []
        self.verdict = symbols.UNKNOWN
        self.listings = {}
        self.enterContext(mock.patch.object(symbols, "check", self._check))
        self.enterContext(mock.patch.object(symbols, "search", self._search))
        self.enterContext(mock.patch.object(quota, "try_consume", self._consume))

    def _check(self, ticker):
        self.checked.append(ticker)
        return self.verdict

    def _search(self, query, followed=None):
        self.searched.append((query, followed))
        return self.listings.get(query, []), "yahoo"

    def _consume(self, email):
        self.consumed.append(email)
        # A global refusal: proves the request got past the check without
        # running submit().
        return False, "global", {"limit": 60, "tz": "America/Los_Angeles"}

    def _run(self, ticker):
        return self.client.post("/api/agents/run", json={"ticker": ticker, "lang": "en"})

    def test_a_ticker_with_no_prices_is_refused_before_the_quota(self):
        self.verdict = symbols.MISSING
        self.listings = {"TCS": [TCS_NS]}
        resp = self._run("tcs")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual((body["reason"], body["ticker"]), ("ticker_not_found", "TCS"))
        self.assertEqual([s["ticker"] for s in body["suggestions"]], ["TCS.NS"])
        self.assertIn("Nothing was charged", body["error"])
        self.assertEqual(self.checked, ["TCS"])
        self.assertEqual(self.consumed, [])
        self.assertIsNotNone(self.searched[0][1])           # the followed list rides along

    def test_a_dotted_symbol_with_no_listings_falls_back_to_its_base(self):
        self.verdict = symbols.MISSING
        self.listings = {"TCS": [TCS_NS]}
        body = self._run("TCS.NY").get_json()
        self.assertEqual([q for q, _ in self.searched], ["TCS.NY", "TCS"])
        self.assertEqual([s["ticker"] for s in body["suggestions"]], ["TCS.NS"])

    def test_the_refused_ticker_is_never_offered_back(self):
        self.verdict = symbols.MISSING
        self.listings = {"ZZZZ": [dict(TCS_NS, ticker="ZZZZ")]}
        self.assertEqual(self._run("ZZZZ").get_json()["suggestions"], [])

    def test_a_check_that_cannot_be_made_reaches_the_quota(self):
        self.verdict = symbols.UNKNOWN
        self.assertEqual(self._run("AAPL").status_code, 429)
        self.assertEqual(self.consumed, [READER])

    def test_a_malformed_ticker_is_left_to_submit(self):
        self._run("apple inc")
        self.assertEqual(self.checked, [])
        self.assertEqual(self.consumed, [READER])

    def test_the_job_api_drops_the_traceback(self):
        job = {"id": "abc123", "user": READER, "ticker": "TCS", "status": "error",
               "error": "NoMarketDataError: No market data for 'TCS': no price rows",
               "log": "started\n" + agents.STDERR_MARK + "Traceback (most recent call last):\n  File x"}
        with mock.patch.object(agents, "get_job", return_value=job), \
             mock.patch.object(agents, "read_events", return_value=[]):
            body = self.client.get("/api/agents/job/abc123").get_json()
        self.assertEqual(body["log"], "started")
        self.assertNotIn("Traceback", str(body))
        self.assertIn("NoMarketDataError", body["error"])
        self.assertIn("Traceback", job["log"])                # the record keeps it

    def test_suggestions_are_for_signed_in_readers(self):
        self.listings = {"tcs": [TCS_NS]}
        resp = self.client.get("/api/agents/symbols?q=tcs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"results": [TCS_NS], "source": "yahoo"})
        self.assertIsNotNone(self.searched[0][1])
        anon = self.app.test_client().get("/api/agents/symbols?q=tcs")
        self.assertEqual(anon.status_code, 401)

    def test_the_page_carries_the_combobox(self):
        html = self.client.get("/agents").get_data(as_text=True)
        self.assertIn('id="agTickerList"', html)
        self.assertIn('role="combobox"', html)
        self.assertIn('aria-controls="agTickerList"', html)
        self.assertIn("/api/agents/symbols?q=", html)
        self.assertIn("ticker_not_found", html)


if __name__ == "__main__":
    unittest.main()
