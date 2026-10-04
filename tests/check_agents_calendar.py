"""
The decision calendar on /agents, through Flask's test client:
``/api/agents/calendar`` and the card on the run page.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic, as the other endpoint checks are -- no background thread, no secret,
no AWS, no network; ``agents._records`` is a list.

What it pins:

* signed out is a 401, like every agent read, and the answer is never cached;
* a reader gets their own runs, compact, each with its trade date and rating
  level, and nothing of anyone else's;
* the card is on the run page for a signed-in reader and nowhere else -- not on
  the landing a visitor sees, not in the launcher's small embedded frame;
* every string the card composes exists in both languages.

Run:  venv/bin/python -m tests.check_agents_calendar
"""
from __future__ import annotations

import os
import re
import sys
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-agents-calendar-secret")
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
from ystocker import agents                               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "ystocker" / "static" / "i18n.js"
ME = "reader@example.com"
JOBS = [
    {"id": "a" * 16, "ticker": "MSFT", "user": ME, "date": "2026-10-02", "decision": "Underweight",
     "status": "done", "created_at": "2026-10-04T10:00:00+00:00", "report": "body"},
    {"id": "b" * 16, "ticker": "MSFT", "user": ME, "date": "2026-09-29", "decision": "Overweight",
     "status": "done", "created_at": "2026-09-30T10:00:00+00:00", "report": "body"},
    {"id": "c" * 16, "ticker": "NFLX", "user": "someone@example.com", "date": "2026-09-15",
     "decision": "Hold", "status": "done", "created_at": "2026-09-15T10:00:00+00:00", "report": "body"},
]


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class AgentsCalendar(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        self.patches = [mock.patch.object(agents, "_records", lambda **kw: list(JOBS)),
                        mock.patch("ystocker.quota.is_vip", return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _sign_in(self):
        with self.client.session_transaction() as sess:
            sess["user_email"] = ME

    def test_signed_out_is_a_401(self):
        self.assertEqual(self.client.get("/api/agents/calendar").status_code, 401)

    def test_a_reader_gets_their_own_runs(self):
        self._sign_in()
        resp = self.client.get("/api/agents/calendar")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))
        body = resp.get_json()
        self.assertEqual([(r["ticker"], r["date"], r["level"]) for r in body["runs"]],
                         [("MSFT", "2026-10-02", -1), ("MSFT", "2026-09-29", 1)])
        self.assertEqual(body["tickers"], [{"ticker": "MSFT", "runs": 2}])
        self.assertNotIn("someone@example.com", resp.get_data(as_text=True))
        self.assertFalse(body["vip"])

    def test_the_card_is_on_the_run_page_for_a_signed_in_reader(self):
        self._sign_in()
        html = self.client.get("/agents").get_data(as_text=True)
        for needle in ('id="agCal"', 'id="agCalGrid"', 'id="agCalPrev"', 'id="agCalTickers"',
                       "agent_calendar.js", "/api/agents/calendar", "window.agOpenJob = openJob"):
            self.assertIn(needle, html, needle)

    def test_not_for_a_visitor_nor_in_the_embedded_frame(self):
        self.assertNotIn('id="agCal"', self.client.get("/home").get_data(as_text=True))
        self._sign_in()
        self.assertNotIn('id="agCal"', self.client.get("/agents?embed=1").get_data(as_text=True))

    def test_every_composed_string_exists_in_both_languages(self):
        tpl = (ROOT / "ystocker/templates/agents.html").read_text()
        card = tpl[tpl.index('id="agCal"'):tpl.index("{% endif %}", tpl.index('id="agCal"'))]
        keys = set(re.findall(r"T\('(agents\.cal_[a-z0-9_]+)'", card))
        keys |= set(re.findall(r"\['(agents\.cal_[a-z0-9_]+)'", card))
        keys |= set(re.findall(r'data-i18n(?:-title)?="(agents\.cal_[a-z0-9_]+)"', card))
        keys |= {"agents.cal_up", "agents.cal_down", "agents.cal_same", "agents.cal_first"}   # built as 'agents.cal_' + change
        self.assertGreater(len(keys), 20)
        i18n = I18N.read_text()
        for key in sorted(keys):
            self.assertRegex(i18n, r"'%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % re.escape(key), key)


if __name__ == "__main__":
    unittest.main()
