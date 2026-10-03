"""
End-to-end check of /predictions and its APIs through Flask's test client: the
board, the Fed cross-check, the AI read's whole round trip, the ledger, and the
card on /fedwatch.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app. Built
hermetically, as ``check_research_endpoints`` is -- no background thread, no
secret, no AWS, no network. The odds are the real 2026-10-03 payloads from
``tests/test_odds.py``, the model is a stub that counts calls, and the quota
counter, read files and ledger mirror live in a temporary directory.

What it pins:

* the page renders on both hosts, wears the Markets bar on trade-agents.com
  with its own link marked current, and is walled there for a signed-out reader
  only;
* the board API serves the cached payload without fetching, leaves the
  resolution text out of the list, and answers a cold cache with 202 while it
  kicks one refresh;
* the Fed cross-check reaches /api/predictions/fed with the futures restated
  per meeting (December's 18.4% hold, not the grid's cumulative 14.6%);
* an AI read is refused signed out (401), switched off (503) and past the
  daily allowance (429); otherwise it runs once, lands in the ledger with the
  market's price from the same snapshot, and a second request for the same
  market is answered from the stored read without spending the allowance;
* GET of a read carries the market's resolution rules and works signed out;
* /predictions/refresh honours its one-minute floor;
* /fedwatch carries the venues card, registered with DeferLoad.

Run:  venv/bin/python -m tests.check_predictions_endpoints
"""
from __future__ import annotations

import json
import os
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-predictions-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
os.environ["AGENTS_VIP_EMAILS"] = "vip@example.com"
os.environ["GEMINI_API_KEY"] = "check-not-a-real-key"
os.environ["PREDICTIONS_AI_DAILY_LIMIT"] = "2"
os.environ["PREDICTIONS_AI_GLOBAL_DAILY_LIMIT"] = "50"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import fedwatch, odds, odds_ai, odds_ledger, predictions, quota  # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_odds as fx                                    # noqa: E402

READER = "reader@example.com"
#: The wall's own markup. Not the body class: "w-walled" is spelled in the
#: shell's CSS on every trade-agents.com page, walled or not.
WALL_MARK = '<div class="w-wall" data-wall>'


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


def _payload() -> dict:
    """The feed's payload, assembled from the captured events exactly as
    predictions._build would, minus the fetching."""
    pm = [fx._pm(e) for e in (fx.PM_FED_OCT, fx.PM_FED_DEC, fx.PM_INFLATION, fx.PM_RECESSION)]
    ks = [fx._ks(fx.KS_FED_OCT), fx._ks(fx.KS_FED_DEC), fx._ks(fx.KS_CPI, "inflation"),
          fx._ks(fx.KS_SPX, "equities")]
    # The fixtures were captured on 2026-10-03; judge the board as of then.
    events = pm + ks
    board = odds.rank_board(odds.trim(e) for e in events if odds.on_board(e, fx.NOW))
    return {"ver": predictions.CACHE_VER, "fetched_at": time.time(), "events": board,
            "fed_events": [odds.trim(e) for e in events if odds.is_fed_decision(e)],
            "sources": {"polymarket": {"ok": True}, "kalshi": {"ok": True}},
            "counts": {}}


def _answer(prompt: str):
    """The stub model: a well-formed read of whatever market it was shown,
    giving every listed outcome an equal share."""
    import re

    labels = re.findall(r'^\d+\. "(.+?)"\s*$', prompt, re.M)
    share = round(100 / max(1, len(labels)), 2)
    body = {"probabilities": {lab: share for lab in labels}, "confidence": "low",
            "evidence_date": "2026-10-02",
            "en": {"summary": "Stub read.", "drivers": ["a"], "watch": ["b"]},
            "zh": {"summary": "测试解读。", "drivers": ["甲"], "watch": ["乙"]}}
    _answer.calls += 1
    return ("```json\n" + json.dumps(body, ensure_ascii=False) + "\n```",
            [{"title": "federalreserve.gov", "uri": "https://www.federalreserve.gov/"}],
            ["fomc october 2026"])


_answer.calls = 0


class PredictionsEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(predictions, "CACHE_PATH", root / "predictions_cache.json"),
            mock.patch.object(predictions, "_LOCK_PATH", root / "predictions.refresh"),
            mock.patch.object(predictions, "kick", mock.Mock(return_value=True)),
            mock.patch.object(fedwatch, "peek", lambda: fx.FEDWATCH),
            mock.patch.object(odds_ai, "READ_DIR", root / "reads"),
            mock.patch.object(odds_ai, "_gemini", _answer),
            mock.patch.object(odds_ledger, "DISK_PATH", root / "ledger.json"),
            mock.patch.object(odds_ledger, "_get_table", lambda: None),
            mock.patch.object(quota, "QUOTA_DIR", root / "quota"),
            mock.patch.object(quota, "_LOCK_PATH", root / "quota" / "quota.lock"),
        ]
        for p in self.patches:
            p.start()
        self.kick = predictions.kick
        predictions._mem, predictions._mem_mtime = _payload(), time.time()
        odds_ledger._memo = (0.0, [])
        _answer.calls = 0
        self.sign_in(None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        predictions._mem, predictions._mem_mtime = None, 0.0
        odds_ledger._memo = (0.0, [])
        self.tmp.cleanup()

    def sign_in(self, email, base_url=None):
        # The test client keeps cookies per host, so a session for
        # trade-agents.com has to be set on trade-agents.com.
        kw = {"base_url": base_url} if base_url else {}
        with self.client.session_transaction(**kw) as sess:
            sess.clear()
            if email:
                sess["user_email"] = email

    def wait_done(self, key, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            st = self.client.get("/api/predictions/read", query_string={"key": key}).get_json()
            if st["status"] != "running":
                return st
            time.sleep(0.05)
        self.fail(f"read of {key} still running after {seconds}s")

    # ── the page ──────────────────────────────────────────────────────────
    def test_the_page_renders_on_the_yStocker_host(self):
        r = self.client.get("/predictions")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('id="pmBoard"', html)
        self.assertIn("predictions.js", html)
        self.assertIn("predictions.css", html)
        self.assertIn('data-i18n="pm.title"', html)
        self.assertIn('href="/predictions"', html)           # the nav links to it
        self.assertNotIn(WALL_MARK, html)

    def test_trade_agents_wears_the_markets_bar_and_walls_signed_out(self):
        r = self.client.get("/predictions", base_url="https://trade-agents.com")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("data-w-sub", html)
        self.assertRegex(html, r'class="w-sub-link is-current" href="/predictions"\s+aria-current="page"')
        self.assertIn(WALL_MARK, html)
        self.sign_in(READER, base_url="https://trade-agents.com")
        try:
            html = self.client.get("/predictions", base_url="https://trade-agents.com").get_data(as_text=True)
            self.assertNotIn(WALL_MARK, html)
        finally:
            self.sign_in(None, base_url="https://trade-agents.com")

    # ── the board ─────────────────────────────────────────────────────────
    def test_the_board_serves_the_cache_without_its_rules(self):
        body = self.client.get("/api/predictions").get_json()
        self.assertEqual(body["status"], "ok")
        keys = [e["key"] for e in body["events"]]
        self.assertIn("ks:KXFEDDECISION-26OCT", keys)
        self.assertIn("pm:how-high-will-inflation-get-in-2026", keys)
        self.assertTrue(all("rules" not in e for e in body["events"]))
        self.assertEqual(body["topics"][0], "rates")
        self.assertFalse(body["ai"]["signed_in"])
        self.assertTrue(body["ai"]["enabled"])
        self.assertEqual(body["reads"], [])
        self.kick.assert_not_called()

    def test_a_cold_cache_answers_202_and_kicks_once(self):
        predictions._mem = None
        r = self.client.get("/api/predictions")
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.get_json()["warming"])
        self.kick.assert_called_once()

    def test_a_stale_payload_is_served_and_labelled(self):
        # Just past the TTL the master's own refresh is about to land, so a
        # worker serves it, labelled, and leaves the refresh to the thread.
        predictions._mem["fetched_at"] = time.time() - predictions.TTL_SECONDS - 5
        body = self.client.get("/api/predictions").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["stale"])
        self.kick.assert_not_called()

    def test_a_payload_the_thread_abandoned_is_refreshed_behind(self):
        predictions._mem["fetched_at"] = time.time() - 2 * predictions.TTL_SECONDS - 5
        body = self.client.get("/api/predictions").get_json()
        self.assertTrue(body["stale"])
        self.kick.assert_called_once()

    def test_the_fed_crosscheck_is_per_meeting(self):
        for url in ("/api/predictions", "/api/predictions/fed"):
            body = self.client.get(url).get_json()
            rows = body["fed"] if "fed" in body else body["rows"]
            dec = next(r for r in rows if r["date"] == "2026-12-09")
            fut = next(s for s in dec["sources"] if s["source"] == "futures")
            self.assertAlmostEqual(fut["hold"], 0.184, places=3)
            self.assertEqual([s["source"] for s in rows[0]["sources"]],
                             ["futures", "polymarket", "kalshi"])

    def test_refresh_honours_its_floor(self):
        r = self.client.get("/predictions/refresh")
        self.assertEqual(r.status_code, 302)
        self.kick.assert_not_called()                       # odds are seconds old
        predictions._mem["fetched_at"] = time.time() - 120
        self.client.get("/predictions/refresh")
        self.kick.assert_called_once()

    # ── the AI read ───────────────────────────────────────────────────────
    def test_reading_a_read_needs_no_session_and_carries_the_rules(self):
        st = self.client.get("/api/predictions/read",
                             query_string={"key": "ks:KXFEDDECISION-26OCT"}).get_json()
        self.assertEqual(st["status"], "none")
        self.assertIn("resolves to Yes", st["rules"])
        r = self.client.get("/api/predictions/read", query_string={"key": "pm:nope"})
        self.assertEqual(r.status_code, 404)

    def test_asking_needs_a_session(self):
        r = self.client.post("/api/predictions/read", json={"key": "ks:KXFEDDECISION-26OCT"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(_answer.calls, 0)

    def test_the_kill_switch(self):
        self.sign_in(READER)
        with mock.patch.dict(os.environ, {"PREDICTIONS_AI": "0"}):
            r = self.client.post("/api/predictions/read", json={"key": "ks:KXFEDDECISION-26OCT"})
        self.assertEqual(r.status_code, 503)

    def test_an_unknown_market_cannot_be_read(self):
        self.sign_in(READER)
        r = self.client.post("/api/predictions/read", json={"key": "pm:not-on-the-board"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(_answer.calls, 0)

    def test_a_read_round_trip(self):
        self.sign_in(READER)
        key = "pm:fed-decision-in-october-20260617190323537"
        r = self.client.post("/api/predictions/read", json={"key": key})
        self.assertIn(r.status_code, (200, 202), r.get_data(as_text=True))
        self.assertEqual(r.get_json()["quota"]["used"], 1)
        st = self.wait_done(key)
        self.assertEqual(st["status"], "done")
        read = st["read"]
        hold = next(o for o in read["outcomes"] if o["label"] == "No change")
        self.assertEqual(hold["market_p"], 0.825)              # the snapshot's price
        self.assertAlmostEqual(hold["ai_p"], 0.2, places=3)    # the stub's 20 each
        self.assertEqual(read["zh"]["summary"], "测试解读。")
        self.assertEqual(read["sources"][0]["uri"], "https://www.federalreserve.gov/")
        self.assertNotIn(READER, json.dumps(read))              # nothing about who asked

        # The board now marks it, and the ledger has it.
        self.assertIn(key, self.client.get("/api/predictions").get_json()["reads"])
        ledger = self.client.get("/api/predictions/ledger").get_json()
        self.assertEqual(ledger["summary"]["recorded"], 1)
        self.assertEqual(ledger["summary"]["open"], 1)
        self.assertIsNone(ledger["summary"]["brier_ai"])
        self.assertEqual(ledger["rows"][0]["key"], key)
        self.assertNotIn("en", ledger["rows"][0])

        # Asking again is answered from the stored read and spends nothing.
        again = self.client.post("/api/predictions/read", json={"key": key})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["status"], "done")
        self.assertEqual(_answer.calls, 1)
        day = quota._read(quota.today())
        self.assertEqual(day["odds_read"][READER], 1)

    def test_the_daily_allowance_is_enforced(self):
        self.sign_in(READER)
        keys = ["ks:KXFEDDECISION-26OCT", "ks:KXFEDDECISION-26DEC", "ks:KXCPI-26SEP"]
        for key in keys[:2]:
            r = self.client.post("/api/predictions/read", json={"key": key})
            self.assertIn(r.status_code, (200, 202))
            self.wait_done(key)
        r = self.client.post("/api/predictions/read", json={"key": keys[2]})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["reason"], "user")
        self.assertEqual(_answer.calls, 2)

    def test_a_vip_has_a_larger_allowance(self):
        self.assertGreater(quota.limit_odds_read("vip@example.com"), quota.limit_odds_read(READER))

    # ── /fedwatch ─────────────────────────────────────────────────────────
    def test_fedwatch_carries_the_venues_card(self):
        with mock.patch.object(fedwatch, "get_cache_ts", lambda: time.time()), \
             mock.patch.object(fedwatch, "is_cache_fresh", lambda: True), \
             mock.patch.object(fedwatch, "is_warming", lambda: False):
            html = self.client.get("/fedwatch").get_data(as_text=True)
        self.assertIn('id="fwVenuesLoading"', html)
        self.assertIn("DeferLoad.when('#fwVenuesLoading'", html)
        self.assertIn("predictions.js", html)
        self.assertIn('data-i18n="pm.fw_card_title"', html)


if __name__ == "__main__":
    unittest.main()
