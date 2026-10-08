"""
End-to-end check of /sectors, /smart-money and /history's 聪明钱 tab through
Flask's test client: ``/api/sectors``, ``/api/smart-money``,
``/api/smart-money/<ticker>`` and the three pages.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic, as the other endpoint checks are -- no background thread, no secret,
no AWS, no network: AWS is stripped, SSM and dotenv are stubbed and
``Thread.start`` is a no-op while ``create_app()`` runs (CLAUDE.md, Known
Pitfalls). The three caches the routes read are stubs.

What it pins:

* /api/sectors is a 202 before the first build and the payload after it, with
  members' names from the ticker cache, five minutes of browser cache, and a
  403 for a declared crawler like every /api/ route;
* /api/smart-money reads the 13F, Form 4 and House caches and fetches none;
  with nothing in any of them it is a 202; the same day's payload is built
  once while its caches hold still;
* one stock's dossier names the next earnings date only when it is ahead;
  a malformed ticker is a 400;
* the pages mark their sections (Markets for /sectors, 13F for /smart-money)
  on both hosts, and /history carries the tab and its panel.

Run:  venv/bin/python -m tests.check_sectors_money_endpoints
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-sectors-money-secret")
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
from ystocker import congress, insiders, routes, sec13f, sector_map   # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_smart_money import HOLDINGS, HOUSE, INSIDERS      # noqa: E402

TA = "http://trade-agents.com"
TODAY = dt.date.today()


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        future = (TODAY + dt.timedelta(days=20)).isoformat()
        cache = {"Tech": {"NVDA": {"Name": "NVIDIA Corporation", "Quote Type": "EQUITY", "Earnings Date": future},
                          "AAPL": {"Name": "Apple Inc.", "Quote Type": "EQUITY",
                                   "Earnings Date": (TODAY - dt.timedelta(days=5)).isoformat()}}}
        for p in (mock.patch.object(routes, "_cache", cache),
                  mock.patch.object(sector_map, "CACHE_FILE", Path(self.tmp.name) / "sector_map.json"),
                  mock.patch.object(sector_map, "_mem", None), mock.patch.object(sector_map, "_mem_mtime", None),
                  mock.patch("ystocker.directory.peek", return_value=None),
                  mock.patch.dict(routes._SMART, {"key": None, "payload": None})):
            p.start()
            self.addCleanup(p.stop)
        self.future = future


class SectorsEndpoints(_Base):

    PAYLOAD = {"schema": sector_map.SCHEMA, "asof": "2026-10-06", "levels": {"sector": [], "group": [], "sub": []},
               "members": [{"t": "NVDA", "s": "semiconductors", "w": 8.6, "r": {"1D": 1.0}},
                           {"t": "ZZZZ", "s": "semiconductors", "w": 0.1, "r": {}}]}

    def test_before_the_first_build_it_is_a_202(self):
        resp = self.client.get("/api/sectors")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["status"], "warming")

    def test_after_it_the_payload_with_names(self):
        sector_map.save(self.PAYLOAD)
        resp = self.client.get("/api/sectors")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual((body["status"], body["asof"]), ("ok", "2026-10-06"))
        self.assertEqual(body["names"], {"NVDA": "NVIDIA Corporation"})   # an unknown ticker has none
        self.assertIn("max-age=300", resp.headers.get("Cache-Control", ""))

    def test_a_declared_crawler_is_refused(self):
        sector_map.save(self.PAYLOAD)
        resp = self.client.get("/api/sectors", headers={"User-Agent": "meta-externalagent/1.1"})
        self.assertEqual(resp.status_code, 403)

    def test_the_page_marks_markets_on_both_hosts(self):
        html = self.client.get("/sectors").get_data(as_text=True)
        self.assertIn('id="smChart"', html)
        self.assertIn("sector_map.js", html)
        nav = re.search(r'<nav data-nav="desktop".*?</nav>', html, re.S).group(0)
        self.assertEqual(re.findall(r'href="([^"]+)"[^>]*aria-current="page"', nav), ["/markets"])
        ta = self.client.get("/sectors", base_url=TA).get_data(as_text=True)
        bar = re.search(r'<nav class="w-sub".*?</nav>', ta, re.S).group(0)
        self.assertEqual(re.findall(r'is-current" href="([^"]+)"', bar), ["/markets"])

    def test_markets_links_to_it(self):
        self.assertIn('href="/sectors"', self.client.get("/markets").get_data(as_text=True))


class SmartMoneyEndpoints(_Base):

    def _inputs(self, holdings=HOLDINGS, insider_rows=INSIDERS, house=HOUSE):
        view = {"rows": insider_rows, "coverage": {"checked": 3, "universe": 3, "newest_check": 1.0}}
        feed = {"rows": house, "counts": {"filings": 5, "paper": 0, "pending": 0}, "latest_filed": "2026-10-02",
                "built": 1.0} if house else None
        return (mock.patch.object(sec13f, "get_all_holdings", return_value=holdings),
                mock.patch.object(insiders, "feed_view", return_value=view),
                mock.patch.object(congress, "peek", return_value=feed))

    def test_the_payload(self):
        p1, p2, p3 = self._inputs()
        with p1, p2, p3:
            resp = self.client.get("/api/smart-money")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(any(r["t"] == "NVDA" for r in body["tickers"]))
        self.assertEqual(body["names"].get("NVDA"), "NVIDIA Corporation")
        self.assertIn("fund", {p["kind"] for p in body["people"]})

    def test_nothing_in_any_cache_is_a_202(self):
        p1, p2, p3 = self._inputs(holdings={}, insider_rows=[], house=None)
        with p1, p2, p3:
            resp = self.client.get("/api/smart-money")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["status"], "warming")

    def test_the_same_inputs_are_built_once(self):
        p1, p2, p3 = self._inputs()
        with p1, p2, p3, mock.patch("ystocker.smart_money.build", wraps=__import__("ystocker.smart_money").smart_money.build) as build:
            self.client.get("/api/smart-money")
            self.client.get("/api/smart-money")
        self.assertEqual(build.call_count, 1)

    def test_one_stock(self):
        p1, p2, p3 = self._inputs()
        with p1, p2, p3:
            body = self.client.get("/api/smart-money/nvda").get_json()
        self.assertEqual(body["ticker"], "NVDA")
        self.assertEqual(body["name"], "NVIDIA Corporation")
        self.assertEqual(body["next_earnings"], self.future)
        self.assertEqual([f["fund"] for f in body["funds"]][:2], ["Pershing Square", "Berkshire Hathaway"])
        self.assertIn("insiders", body["sources"])

    def test_a_past_earnings_date_is_not_the_next(self):
        p1, p2, p3 = self._inputs()
        with p1, p2, p3:
            body = self.client.get("/api/smart-money/AAPL").get_json()
        self.assertIsNone(body["next_earnings"])

    def test_a_malformed_ticker_is_a_400(self):
        p1, p2, p3 = self._inputs()
        with p1, p2, p3:
            self.assertEqual(self.client.get("/api/smart-money/%3Cscript%3E").status_code, 400)

    def test_the_page_marks_13f_on_both_hosts(self):
        html = self.client.get("/smart-money").get_data(as_text=True)
        self.assertIn('id="moRows"', html)
        nav = re.search(r'<nav data-nav="desktop".*?</nav>', html, re.S).group(0)
        self.assertEqual(re.findall(r'href="([^"]+)"[^>]*aria-current="page"', nav), ["/13f"])
        ta = self.client.get("/smart-money", base_url=TA).get_data(as_text=True)
        bar = re.search(r'<nav class="w-sub".*?</nav>', ta, re.S).group(0)
        self.assertEqual(re.findall(r'is-current" href="([^"]+)"', bar), ["/13f"])

    def test_13f_and_insiders_link_to_it(self):
        with mock.patch.object(sec13f, "get_all_holdings", return_value={}):
            self.assertIn('href="/smart-money"', self.client.get("/13f").get_data(as_text=True))
        self.assertIn('href="/smart-money"', self.client.get("/insiders").get_data(as_text=True))


class HistoryTab(_Base):

    def test_the_tab_and_its_panel(self):
        html = self.client.get("/history/NVDA").get_data(as_text=True)
        self.assertIn('id="tabMoneyBtn" onclick="switchTab(\'money\')"', html)
        self.assertIn('id="panelMoney"', html)
        self.assertRegex(html, r"const HISTORY_TABS = \[[^\]]*'money'")
        self.assertIn("/api/smart-money/${encodeURIComponent(TICKER)}", html)


class StringsExist(unittest.TestCase):
    """Every key the three pages compose exists in both languages."""

    def test_keys(self):
        root = Path(__file__).resolve().parent.parent
        src = (root / "ystocker" / "static" / "i18n.js").read_text()
        q = r"'((?:[^'\\]|\\.)*)'"
        keys = set()
        for name, prefix in (("smart_money.html", "smart"), ("sectors.html", "sectors"), ("history.html", "history.money")):
            tpl = (root / "ystocker" / "templates" / name).read_text()
            keys |= set(re.findall(rf"(?:{re.escape(prefix)}|smart)\.[A-Za-z0-9_]+(?=')", tpl))
            keys |= set(k for k in re.findall(r'data-i18n(?:-placeholder|-html)?="([^"]+)"', tpl)
                        if k.startswith(("smart.", "sectors.", "history.money", "history.tab_money")))
        keys |= {"nav.smart_money", "nav.sectors", "history.tab_money"}
        self.assertGreater(len(keys), 120)
        for key in sorted(keys):
            m = re.search(r"'" + re.escape(key) + r"'\s*:\s*\{\s*en:\s*" + q + r"\s*,\s*zh:\s*" + q + r"\s*\}", src, re.S)
            self.assertIsNotNone(m, f"missing i18n key {key}, or it lacks en/zh")
            self.assertTrue(m.group(1) and m.group(2), key)


if __name__ == "__main__":
    unittest.main()
