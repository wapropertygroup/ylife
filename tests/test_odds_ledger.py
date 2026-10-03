"""The AI read's lifecycle and its ledger, end to end on disk.

Exercises ``odds_ai`` and ``odds_ledger`` together with a stub model, a
temporary cache directory and DynamoDB switched off -- no network, no app, no
Gemini key. What it guards:

* a read is recorded in the ledger the moment it exists, with the market's
  price from the *same* snapshot the model was shown (a price re-read after
  the fact would let the market's later move flatter or punish the read);
* two requests for one event start one read, not two -- the claim is a marker
  file because two gunicorn workers share nothing else;
* a crashed run's marker expires instead of blocking the event for ever;
* an unusable answer becomes an ``error`` the reader can retry, not a stored
  forecast;
* settlement scores a row only when every outcome has resolved, and a settled
  copy is never reverted by an older open one.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ystocker import odds, odds_ai, odds_ledger

EVENT = {
    "key": "pm:fed-decision-in-october",
    "platform": "polymarket",
    "id": "fed-decision-in-october",
    "title": "Fed Decision in October?",
    "subtitle": "",
    "url": "https://polymarket.com/event/fed-decision-in-october",
    "topic": "rates",
    "kind": "exclusive",
    "numeric": True,
    "closes": "2026-10-29T03:59:00Z",
    "volume": 24391674.9, "volume_24h": 181358.1, "liquidity": 3159786.6,
    "n_outcomes": 3,
    "rules": "Resolves to the FOMC's announced decision.",
    "outcomes": [
        {"label": "25 bps decrease", "p": 0.0045, "id": "m1", "order": 1, "d1": None, "w1": None},
        {"label": "No change", "p": 0.825, "id": "m2", "order": 2, "d1": None, "w1": 0.49},
        {"label": "25 bps increase", "p": 0.175, "id": "m3", "order": 3, "d1": None, "w1": -0.47},
    ],
}


def _answer(hold=70, hike=29, cut=1):
    body = {"probabilities": {"25 bps decrease": cut, "No change": hold, "25 bps increase": hike},
            "confidence": "medium", "evidence_date": "2026-10-02",
            "en": {"summary": "A hold, with a live hike tail.", "drivers": ["Core CPI 0.4%"],
                   "watch": ["September CPI on Oct 14"]},
            "zh": {"summary": "大概率按兵不动，但加息尾部风险仍在。", "drivers": ["核心CPI 0.4%"],
                   "watch": ["10月14日公布的9月CPI"]}}
    return "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"


def _stub(text=None, sources=None, queries=None):
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return (text if text is not None else _answer(),
                sources if sources is not None else [{"title": "federalreserve.gov", "uri": "https://x"}],
                queries if queries is not None else ["fomc october 2026 expectations"])

    generate.calls = calls
    return generate


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(odds_ai, "READ_DIR", root / "reads"),
            mock.patch.object(odds_ledger, "DISK_PATH", root / "ledger.json"),
            mock.patch.object(odds_ledger, "_get_table", lambda: None),
            mock.patch.object(odds_ledger, "_SETTLE_SPACING_SECONDS", 0),
        ]
        for p in self.patches:
            p.start()
        odds_ledger._memo = (0.0, [])

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        odds_ledger._memo = (0.0, [])
        self.tmp.cleanup()


class ReadLifecycleTests(_Isolated):
    def test_a_read_is_stored_with_the_market_beside_it(self):
        gen = _stub()
        self.assertEqual(odds_ai.start(EVENT, market_as_of=1791000000.0, generate=gen,
                                       background=False), "started")
        st = odds_ai.status(EVENT["key"])
        self.assertEqual(st["status"], "done")
        self.assertTrue(st["fresh"])
        read = st["read"]
        hold = next(o for o in read["outcomes"] if o["label"] == "No change")
        self.assertAlmostEqual(hold["ai_p"], 0.70, places=3)
        self.assertEqual(hold["market_p"], 0.825)
        self.assertEqual(hold["gap_pp"], -12.5)
        self.assertEqual(read["zh"]["summary"], "大概率按兵不动，但加息尾部风险仍在。")
        self.assertEqual(read["sources"][0]["title"], "federalreserve.gov")
        self.assertEqual(read["queries"], ["fomc october 2026 expectations"])
        self.assertEqual(read["market_as_of"], 1791000000.0)
        self.assertEqual(len(gen.calls), 1)
        self.assertIn('"No change"', gen.calls[0])

    def test_the_read_lands_in_the_ledger_at_once(self):
        odds_ai.start(EVENT, generate=_stub(), background=False)
        rows = odds_ledger.rows(fresh=True)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["key"], EVENT["key"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["bucket"], row["created_at"][:7])
        self.assertTrue(row["sk"].endswith("#" + EVENT["key"]))
        hold = next(o for o in row["outcomes"] if o["label"] == "No change")
        self.assertEqual((hold["market_p"], hold["ai_p"]), (0.825, 0.7))
        self.assertNotIn("en", row)          # the prose stays in the read file

    def test_the_price_is_frozen_at_the_moment_of_asking(self):
        """The event dict the caller holds may be the live cache object; a
        refresh mutating it mid-read must not change the recorded price."""
        live = json.loads(json.dumps(EVENT))

        def generate(prompt):
            live["outcomes"][1]["p"] = 0.10          # the market "moves" during the call
            return _stub()(prompt)

        odds_ai.start(live, generate=generate, background=False)
        hold = next(o for o in odds_ai.latest(EVENT["key"])["outcomes"] if o["label"] == "No change")
        self.assertEqual(hold["market_p"], 0.825)

    def test_a_fresh_read_is_reused_not_regenerated(self):
        gen = _stub()
        odds_ai.start(EVENT, generate=gen, background=False)
        self.assertEqual(odds_ai.start(EVENT, generate=gen, background=False), "fresh")
        self.assertEqual(len(gen.calls), 1)

    def test_a_second_request_while_running_does_not_start_another(self):
        self.assertTrue(odds_ai._claim(EVENT["key"]))
        self.assertEqual(odds_ai.start(EVENT, generate=_stub(), background=False), "running")
        self.assertEqual(odds_ai.status(EVENT["key"])["status"], "running")

    def test_a_dead_runs_marker_expires(self):
        self.assertTrue(odds_ai._claim(EVENT["key"]))
        marker = odds_ai._path(EVENT["key"], ".running")
        old = time.time() - odds_ai.RUN_STALE_SECONDS - 5
        os.utime(marker, (old, old))
        self.assertEqual(odds_ai.status(EVENT["key"])["status"], "none")
        self.assertEqual(odds_ai.start(EVENT, generate=_stub(), background=False), "started")

    def test_an_unusable_answer_is_an_error_not_a_forecast(self):
        odds_ai.start(EVENT, generate=_stub(text="I'd rather not say."), background=False)
        st = odds_ai.status(EVENT["key"])
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["error"], "no_json")
        self.assertIsNone(odds_ai.latest(EVENT["key"]))
        self.assertEqual(odds_ledger.rows(fresh=True), [])
        self.assertIsNone(odds_ai._running_since(EVENT["key"]))   # released

    def test_a_model_failure_is_reported_as_such(self):
        def boom(prompt):
            raise RuntimeError("quota exhausted")

        odds_ai.start(EVENT, generate=boom, background=False)
        self.assertEqual(odds_ai.status(EVENT["key"])["error"], "model")

    def test_an_old_read_is_shown_dated_while_a_new_one_is_offered(self):
        odds_ai.start(EVENT, generate=_stub(), background=False)
        path = odds_ai._path(EVENT["key"], ".json")
        data = json.loads(path.read_text())
        data["created_ts"] -= odds_ai.READ_TTL + 60
        path.write_text(json.dumps(data))
        st = odds_ai.status(EVENT["key"])
        self.assertEqual((st["status"], st["fresh"]), ("done", False))
        self.assertEqual(odds_ai.fresh_keys([EVENT["key"]]), set())

    def test_kill_switch_and_missing_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "PREDICTIONS_AI": "0"}):
            self.assertFalse(odds_ai.enabled())
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "PREDICTIONS_AI": "1"}):
            self.assertFalse(odds_ai.enabled())
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "PREDICTIONS_AI": "1"}):
            self.assertTrue(odds_ai.enabled())


class SettlementTests(_Isolated):
    def _recorded(self, closes="2026-10-01T00:00:00Z"):
        event = {**EVENT, "closes": closes}
        odds_ai.start(event, generate=_stub(), background=False)
        return odds_ledger.rows(fresh=True)[0]

    def test_nothing_is_asked_before_the_market_closes(self):
        self._recorded(closes="2099-01-01T00:00:00Z")
        asked = []
        counts = odds_ledger.settle_pass(fetch_polymarket=lambda mid: asked.append(mid),
                                         fetch_kalshi=lambda t: None)
        self.assertEqual(asked, [])
        self.assertEqual(counts["checked"], 0)

    def test_a_partly_resolved_row_keeps_what_it_learned_and_stays_open(self):
        self._recorded()
        answers = {"m1": {"closed": True, "outcomePrices": '["0","1"]'},
                   "m2": {"closed": False, "outcomePrices": '["0.9","0.1"]'},
                   "m3": {"closed": True, "outcomePrices": '["0","1"]'}}
        odds_ledger.settle_pass(fetch_polymarket=answers.get, fetch_kalshi=lambda t: None)
        row = odds_ledger.rows(fresh=True)[0]
        self.assertEqual(row["status"], "open")
        self.assertEqual({o["label"]: o.get("result") for o in row["outcomes"]},
                         {"25 bps decrease": 0, "No change": None, "25 bps increase": 0})

    def test_a_resolved_row_is_scored_against_the_market(self):
        self._recorded()
        answers = {"m1": {"closed": True, "outcomePrices": '["0","1"]'},
                   "m2": {"closed": True, "outcomePrices": '["1","0"]'},
                   "m3": {"closed": True, "outcomePrices": '["0","1"]'}}
        counts = odds_ledger.settle_pass(fetch_polymarket=answers.get, fetch_kalshi=lambda t: None)
        self.assertEqual(counts["settled"], 1)
        row = odds_ledger.rows(fresh=True)[0]
        self.assertEqual(row["status"], "settled")
        # AI: (0.01-0)², (0.70-1)², (0.29-0)²  → mean 0.0601 (after rescaling to 100)
        ai = {o["label"]: o["ai_p"] for o in row["outcomes"]}
        want_ai = ((ai["25 bps decrease"]) ** 2 + (ai["No change"] - 1) ** 2
                   + (ai["25 bps increase"]) ** 2) / 3
        self.assertAlmostEqual(row["brier_ai"], want_ai, places=5)
        want_mk = (0.0045 ** 2 + (0.825 - 1) ** 2 + 0.175 ** 2) / 3
        self.assertAlmostEqual(row["brier_market"], want_mk, places=5)
        summary = odds_ledger.summary()
        self.assertEqual(summary["settled"], 1)
        self.assertEqual(summary["market_better"], 1)

    def test_a_settled_copy_beats_an_older_open_one(self):
        open_row = {"sk": "2026-10-03T00:00:00Z#k", "status": "open"}
        settled = {"sk": "2026-10-03T00:00:00Z#k", "status": "settled", "brier_ai": 0.1}
        self.assertEqual(odds_ledger._merge([open_row], [settled])[0]["status"], "settled")
        self.assertEqual(odds_ledger._merge([settled], [open_row])[0]["status"], "settled")

    def test_buckets_walk_back_across_a_year_end(self):
        got = odds_ledger._buckets(datetime(2027, 1, 15, tzinfo=timezone.utc))
        self.assertEqual(got[:3], ["2027-01", "2026-12", "2026-11"])
        self.assertEqual(len(got), odds_ledger.MAX_BUCKETS)


class QuotaTests(unittest.TestCase):
    """The read allowance lives in quota.py's locked daily file."""

    def setUp(self):
        from ystocker import quota

        self.quota = quota
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(quota, "QUOTA_DIR", root),
                        mock.patch.object(quota, "_LOCK_PATH", root / "quota.lock")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_per_user_then_global_then_refund(self):
        env = {"PREDICTIONS_AI_DAILY_LIMIT": "2", "PREDICTIONS_AI_GLOBAL_DAILY_LIMIT": "3",
               "AGENTS_VIP_EMAILS": ""}
        with mock.patch.dict(os.environ, env):
            q = self.quota
            self.assertTrue(q.try_consume_odds_read("a@x.com")[0])
            self.assertTrue(q.try_consume_odds_read("a@x.com")[0])
            ok, reason, info = q.try_consume_odds_read("a@x.com")
            self.assertEqual((ok, reason, info["remaining"]), (False, "user", 0))
            self.assertTrue(q.try_consume_odds_read("b@x.com")[0])
            self.assertEqual(q.try_consume_odds_read("c@x.com")[1], "global")
            q.refund_odds_read("a@x.com")
            self.assertTrue(q.try_consume_odds_read("c@x.com")[0])
            self.assertEqual(q.try_consume_odds_read("")[1], "auth")

    def test_reads_never_touch_the_run_allowance(self):
        # Read the counter file directly: quota.usage() also asks the credits
        # ledger for a balance, which is a DynamoDB call this test must not make.
        with mock.patch.dict(os.environ, {"AGENTS_VIP_EMAILS": ""}):
            self.quota.try_consume_odds_read("a@x.com")
            data = self.quota._read(self.quota.today())
            self.assertEqual(data["users"].get("a@x.com", 0), 0)
            self.assertEqual(int(data.get("total", 0)), 0)
            self.assertEqual(data["odds_read"]["a@x.com"], 1)


if __name__ == "__main__":
    unittest.main()
