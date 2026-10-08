"""
End-to-end check of the earnings calendar through Flask's test client:
``/api/earnings`` and ``/earnings``.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic, as the other endpoint checks are -- no background thread, no secret,
no AWS, no network. The cache is a temporary directory holding rows cut from
Nasdaq's real answers; "today" is pinned; the fetch queue is a stub that counts
what it was asked for.

What it pins:

* a week with every day cached is a 200, Monday to Friday, with the bounds and
  the neighbours the page navigates by;
* a cold week is a 202 that queues exactly its missing days, with the days that
  are in already served;
* ``followed`` and ``held`` name only the week's own tickers, and a position
  store that cannot be read costs the highlight, not the page;
* a week far outside the calendar's range is clamped and says so, and a
  malformed ``?week=`` is a 400;
* the page carries its controls, marks its own tab as current, and every
  string it composes exists in both languages.

Run:  venv/bin/python -m tests.check_earnings_endpoints
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-earnings-secret")
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
from ystocker import earnings_calendar as ec              # noqa: E402
from ystocker import routes                               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_earnings_calendar import BANKS, REPORTED, SMALL, _payload   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "ystocker" / "static" / "i18n.js"
TODAY = dt.date(2026, 10, 7)          # a Wednesday
MONDAY = dt.date(2026, 10, 5)


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class EarningsEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.kick = mock.Mock(return_value=0)
        self.patches = [
            mock.patch.object(ec, "CACHE_DIR", self.dir),
            mock.patch.object(ec, "kick", self.kick),
            mock.patch.object(ec, "today_et", lambda now=None: TODAY),
            mock.patch.object(routes, "_cache", {"Banks": {"JPM": {}, "GS": {}}, "Tech": {"MSFT": {}}}),
        ]
        for p in self.patches:
            p.start()
        ec._mem.clear()
        ec._failed.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        ec._mem.clear()
        ec._failed.clear()
        self.tmp.cleanup()

    def _cache(self, day, rows, age=60):
        (self.dir / f"{day.isoformat()}.json").write_text(json.dumps(
            {"date": day.isoformat(), "_ts": time.time() - age, "rows": ec.parse_day(_payload(rows))}))

    def _fill_week(self, monday=MONDAY):
        for i, rows in enumerate([SMALL, REPORTED, [], BANKS, []]):
            self._cache(monday + dt.timedelta(days=i), rows)

    def test_a_cached_week_is_served_whole(self):
        self._fill_week()
        resp = self.client.get("/api/earnings")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["week"]["start"], "2026-10-05")
        self.assertEqual(body["week"]["end"], "2026-10-09")
        self.assertEqual(body["week"]["prev"], "2026-09-28")
        self.assertEqual(body["week"]["next"], "2026-10-12")
        self.assertEqual(body["week"]["current"], "2026-10-05")
        self.assertEqual([d["status"] for d in body["days"]], ["ok"] * 5)
        self.assertEqual(body["days"][3]["rows"][0]["t"], "JPM")
        self.assertEqual(body["followed"], ["GS", "JPM", "MSFT"])
        self.assertEqual(body["held"], [])
        self.kick.assert_not_called()

    def test_a_cold_week_is_a_202_that_queues_its_missing_days(self):
        self._cache(dt.date(2026, 10, 13), BANKS)
        resp = self.client.get("/api/earnings?week=2026-10-14")
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertEqual(body["status"], "warming")
        self.assertEqual(body["week"]["start"], "2026-10-12")
        self.assertEqual([d["status"] for d in body["days"]],
                         ["pending", "ok", "pending", "pending", "pending"])
        self.assertEqual(len(body["days"][1]["rows"]), 3)
        queued = [d.isoformat() for d in self.kick.call_args[0][0]]
        self.assertEqual(queued, ["2026-10-12", "2026-10-14", "2026-10-15", "2026-10-16"])

    def test_holdings_mark_only_this_weeks_tickers(self):
        self._fill_week()
        with self.client.session_transaction() as sess:
            sess["user_email"] = "reader@example.com"
        positions = [{"symbol": "UNH"}, {"symbol": "NVDA"}]
        with mock.patch("ystocker.portfolio.load", return_value=positions):
            body = self.client.get("/api/earnings").get_json()
        self.assertEqual(body["held"], ["UNH"])

    def test_an_unreadable_position_store_costs_the_highlight_not_the_page(self):
        self._fill_week()
        with self.client.session_transaction() as sess:
            sess["user_email"] = "reader@example.com"
        from ystocker import portfolio
        with mock.patch("ystocker.portfolio.load", side_effect=portfolio.StoreUnavailable("down")):
            resp = self.client.get("/api/earnings")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["held"], [])

    def test_far_weeks_are_clamped_and_bad_dates_refused(self):
        body = self.client.get("/api/earnings?week=1999-01-04").get_json()
        lo, _hi = ec.week_bounds(TODAY)
        self.assertTrue(body["week"]["clamped"])
        self.assertEqual(body["week"]["start"], lo.isoformat())
        self.assertIsNone(body["week"]["prev"])
        self.assertEqual(self.client.get("/api/earnings?week=next-tuesday").status_code, 400)

    def test_the_page_carries_its_controls_and_marks_its_tab(self):
        html = self.client.get("/earnings").get_data(as_text=True)
        for needle in ('id="ecDays"', 'id="ecPrev"', 'id="ecNext"', 'data-ec-cap="2e9"',
                       'id="ecFollowed"', 'id="ecHeld"', "/api/earnings"):
            self.assertIn(needle, html, needle)
        nav = re.search(r'<nav data-nav="desktop".*?</nav>', html, re.S).group(0)
        current = re.findall(r'href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current, ["/earnings"])

    def test_every_composed_string_exists_in_both_languages(self):
        tpl = (ROOT / "ystocker/templates/earnings.html").read_text()
        keys = set(re.findall(r"tr\('(earnings\.[a-z0-9_]+)'", tpl))
        keys |= set(re.findall(r'data-i18n(?:-title|-placeholder)?="(earnings\.[a-z0-9_]+)"', tpl))
        keys |= {"nav.earnings", "markets.earnings_full"}
        self.assertGreater(len(keys), 30)
        i18n = I18N.read_text()
        for key in sorted(keys):
            self.assertRegex(i18n, r"'%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % re.escape(key), key)


if __name__ == "__main__":
    unittest.main()
