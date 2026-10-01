"""
End-to-end check of /history's Research tab endpoints, through Flask's test
client: saving a deep-research report, reading it back, and the TradeAgents card.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app. Built
hermetically, as ``check_wiki_pages`` is -- no background thread, no secret, no
AWS -- because create_app() would otherwise start the daily email broadcast and
writers to production DynamoDB with this machine's credentials, and
``agents._records`` backfills local job files into the production jobs table
even on a GET. The store is an in-memory table, Gemini a stub that counts calls.

What it pins (the request behind it, 2026-10-01: "the deep research on
/history/NBIS must be saved in the database, and the agents' work shown there"):

* a signed-in reader's report is saved, and is their cache from then on -- the
  same Generate within 8 h is answered from their own row, not by Gemini;
* a signed-in reader is never handed the shared disk cache, which is keyed on a
  fingerprint and filled by whoever POSTed first, and would otherwise be saved
  permanently into their account;
* a signed-out reader still gets a report, unsaved, and is told so; an outage
  of the store degrades to the old disk cache and says "not saved";
* a run that died mid-stream is not saved;
* reads are the reader's own: another reader's ref is a 404 to GET and DELETE;
  an unreachable store is a 503, never an empty list;
* the TradeAgents card lists exactly this ticker's runs, the reader's own (a
  VIP's: everyone's, owner masked), and answers an index with no runs, not 400;
* ``/api/agents/job/<id>?events=0`` leaves the progress replay out;
* the page knows whether a session exists, and renders the card.

Run:  venv/bin/python -m tests.check_research_endpoints
"""
from __future__ import annotations

import json
import os
import sys
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-research-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
os.environ["AGENTS_VIP_EMAILS"] = "vip@example.com"
os.environ["GEMINI_API_KEY"] = "check-not-a-real-key"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import google.genai                                       # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import agents, research_store               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_research_store import _Table                    # noqa: E402

TICKER = "ZZRSCHK"      # never a real symbol, so its disk cache files are ours
CACHE_DIR = Path(ystocker.__file__).resolve().parent.parent / "cache" / "research"
REPORT = "## 1. 基本信息\n股票代码: ZZRSCHK\n\n## 17. 最终评分卡\n数据缺失。"


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class _Chunk:
    def __init__(self, text, finish=None):
        self.text = text
        self.candidates = [types.SimpleNamespace(grounding_metadata=None,
                                                 finish_reason=finish)]


class _Gemini:
    """genai.Client's shape, as far as the research route uses it."""
    calls = 0
    mode = "ok"          # ok | die

    def __init__(self, *a, **k):
        self.models = self

    def generate_content_stream(self, **_kw):
        type(self).calls += 1
        if type(self).mode == "die":
            def dying():
                yield _Chunk("## 1. 基本信息\n半截")
                raise RuntimeError("upstream reset")
            return dying()
        half = len(REPORT) // 2
        return iter([_Chunk(REPORT[:half]), _Chunk(REPORT[half:], finish="STOP")])


def _bundle(**port):
    return {"identity": {"ticker": TICKER, "name": "Check Co", "price": 12.5},
            "valuation": {"eps": 1.25},
            "quarterly": [{"quarter": "2026-06-30"}],
            "portfolio": port}


def _events(resp) -> list[dict]:
    out = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:]))
    return out


def _merged(events) -> dict:
    out = {"text": ""}
    for e in events:
        for k, v in e.items():
            if k == "text":
                out["text"] += v
            else:
                out[k] = v
    return out


class ResearchEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        cls._client_cls = google.genai.Client

    @classmethod
    def tearDownClass(cls):
        google.genai.Client = cls._client_cls

    def setUp(self):
        self.table = _Table()
        self._table, research_store._table = research_store._table, self.table
        google.genai.Client = _Gemini
        _Gemini.calls, _Gemini.mode = 0, "ok"
        self._clear_disk()
        self.sign_in(None)

    def tearDown(self):
        research_store._table = self._table
        self._clear_disk()

    @staticmethod
    def _clear_disk():
        for p in CACHE_DIR.glob(f"{TICKER}_*"):
            p.unlink()

    def sign_in(self, email):
        with self.client.session_transaction() as sess:
            sess.clear()
            if email:
                sess["user_email"] = email

    def research(self, refresh=False, lang="zh", **port):
        r = self.client.post(f"/api/history/{TICKER}/research",
                             json={"bundle": _bundle(**port), "lang": lang,
                                   "refresh": refresh})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        return _merged(_events(r))

    # ── saving ────────────────────────────────────────────────────────────
    def test_a_signed_in_report_is_saved(self):
        self.sign_in("Reader@Example.com")
        out = self.research(account=None)
        self.assertEqual(out["text"], REPORT)
        self.assertTrue(out["save"]["ok"], out["save"])
        self.assertNotIn("existing", out["save"])
        (item,) = self.table.items.values()
        self.assertEqual(item["owner"], "reader@example.com")
        self.assertEqual(item["ticker"], TICKER)
        self.assertEqual(item["lang"], "zh")
        self.assertEqual(item["price"], "12.5")
        self.assertFalse(item["has_portfolio"])
        self.assertEqual(out["save"]["report"]["ref"], item["sk"].split("#", 1)[1])

    def test_a_position_bearing_report_says_so(self):
        self.sign_in("reader@example.com")
        self.research(total_account_value=500000, shares=10)
        (item,) = self.table.items.values()
        self.assertTrue(item["has_portfolio"])

    def test_the_readers_own_report_is_their_cache(self):
        self.sign_in("reader@example.com")
        self.research()
        again = self.research()
        self.assertEqual(_Gemini.calls, 1, "the second Generate called Gemini again")
        self.assertTrue(again["cached"])
        self.assertTrue(again["save"]["existing"])
        self.assertEqual(again["text"], REPORT)
        self.assertEqual(len(self.table.items), 1)

    def test_regenerate_makes_and_saves_a_new_one(self):
        self.sign_in("reader@example.com")
        self.research()
        self.research(refresh=True)
        self.assertEqual(_Gemini.calls, 2)
        self.assertEqual(len(self.table.items), 2)

    def test_another_language_is_another_report(self):
        self.sign_in("reader@example.com")
        self.research(lang="zh")
        self.research(lang="en")
        self.assertEqual(_Gemini.calls, 2)

    def test_a_signed_in_reader_never_gets_the_shared_disk_cache(self):
        self.research()                                     # anonymous: fills disk
        self.assertTrue(list(CACHE_DIR.glob(f"{TICKER}_*")))
        self.sign_in("reader@example.com")
        out = self.research()
        self.assertEqual(_Gemini.calls, 2,
                         "a signed-in reader was served (and would have saved) "
                         "whatever the first anonymous POST produced")
        self.assertNotIn("cached", out)
        self.assertTrue(out["save"]["ok"])

    def test_signed_out_gets_a_report_unsaved_and_is_told(self):
        out = self.research()
        self.assertEqual(out["text"], REPORT)
        self.assertEqual(out["save"], {"ok": False, "reason": "signed_out"})
        self.assertEqual(self.table.items, {})
        cached = self.research()
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["save"]["reason"], "signed_out")
        self.assertEqual(_Gemini.calls, 1)

    def test_a_store_outage_degrades_to_the_disk_cache_and_says_not_saved(self):
        self.research()                                     # anonymous: fills disk
        self.sign_in("reader@example.com")
        self.table.fail = True
        out = self.research()
        self.assertTrue(out["cached"])
        self.assertEqual(out["save"], {"ok": False, "reason": "store"})
        self.assertEqual(_Gemini.calls, 1)

    def test_a_run_that_died_mid_stream_is_not_saved(self):
        self.sign_in("reader@example.com")
        _Gemini.mode = "die"
        out = self.research()
        self.assertEqual(out["save"], {"ok": False, "reason": "incomplete"})
        self.assertIn("upstream reset", out["error"])
        self.assertEqual(self.table.items, {})

    # ── reading back ──────────────────────────────────────────────────────
    def test_saved_reports_need_a_session(self):
        r = self.client.get(f"/api/history/{TICKER}/research/saved")
        self.assertEqual(r.status_code, 401)

    def test_the_list_is_the_readers_own_newest_in_their_language(self):
        self.sign_in("other@example.com")
        self.research()
        self.sign_in("reader@example.com")
        self.research(lang="en")
        self.research(lang="zh", refresh=True)
        self.research(lang="en", refresh=True)
        r = self.client.get(f"/api/history/{TICKER}/research/saved?lang=zh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        body = r.get_json()
        self.assertEqual([m["lang"] for m in body["reports"]], ["en", "zh", "en"])
        self.assertEqual(body["latest"]["lang"], "zh")
        self.assertEqual(body["latest"]["text"], REPORT)
        for m in body["reports"]:
            self.assertNotIn("text", m)
            self.assertNotIn("owner", m)

    def test_an_outage_is_a_503_not_an_empty_list(self):
        self.sign_in("reader@example.com")
        self.table.fail = True
        r = self.client.get(f"/api/history/{TICKER}/research/saved")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "store")

    def test_another_readers_ref_is_a_404_to_read_and_to_delete(self):
        self.sign_in("alice@example.com")
        ref = self.research()["save"]["report"]["ref"]
        url = f"/api/history/{TICKER}/research/saved/{ref}"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.sign_in("bob@example.com")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(len(self.table.items), 1)

    def test_a_reader_can_delete_their_own(self):
        self.sign_in("reader@example.com")
        ref = self.research()["save"]["report"]["ref"]
        url = f"/api/history/{TICKER}/research/saved/{ref}"
        got = self.client.get(url).get_json()
        self.assertEqual(got["text"], REPORT)
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.table.items, {})

    def test_a_malformed_ref_never_reaches_a_key(self):
        self.sign_in("reader@example.com")
        for bad in ("..", "x", "other%40example.com%2320261001T140311Z-0000000000000000"):
            r = self.client.get(f"/api/history/{TICKER}/research/saved/{bad}")
            self.assertEqual(r.status_code, 404, bad)

    # ── the TradeAgents card ──────────────────────────────────────────────
    def _with_jobs(self, jobs):
        real = agents._records
        agents._records = lambda **_kw: list(jobs)
        self.addCleanup(setattr, agents, "_records", real)

    @staticmethod
    def _job(i, ticker="NBIS", user="reader@example.com", status="done"):
        return {"id": f"{i:016x}", "ticker": ticker, "user": user, "status": status,
                "date": "2026-08-27", "decision": "Underweight",
                "report": "### Portfolio Manager\nTrim.", "lang": "zh",
                "created_at": f"2026-08-2{i}T14:03:11+00:00", "pid": 1, "chat": []}

    def test_the_card_needs_a_session(self):
        self.assertEqual(self.client.get("/api/history/NBIS/agents").status_code, 401)

    def test_the_card_lists_this_tickers_runs_the_reader_may_open(self):
        self._with_jobs([self._job(1), self._job(2, ticker="NBISX"),
                         self._job(3, user="someone@else.com")])
        self.sign_in("reader@example.com")
        r = self.client.get("/api/history/nbis/agents")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual([j["id"] for j in body["runs"]], [self._job(1)["id"]])
        self.assertFalse(body["vip"])
        run = body["runs"][0]
        self.assertEqual(run["tone"], "sell")
        self.assertEqual(run["portfolio"]["key"], "portfolio")
        for private in ("user", "pid", "chat", "report"):
            self.assertNotIn(private, run)

    def test_a_vip_sees_everyones_runs_with_the_owner_masked(self):
        self._with_jobs([self._job(1), self._job(3, user="someone@else.com")])
        self.sign_in("vip@example.com")
        body = self.client.get("/api/history/NBIS/agents").get_json()
        self.assertTrue(body["vip"])
        self.assertEqual(sorted(j.get("owner", "") for j in body["runs"]),
                         ["reader@…", "someone@…"])

    def test_an_index_has_no_runs_rather_than_an_error(self):
        self._with_jobs([self._job(1)])
        self.sign_in("reader@example.com")
        r = self.client.get("/api/history/%5EGSPC/agents")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["runs"], [])

    def test_a_finished_run_can_be_opened_without_its_progress_replay(self):
        job = self._job(1)
        real_get, real_events = agents.get_job, agents.read_events
        agents.get_job = lambda _id: dict(job)
        agents.read_events = lambda *_a: [{"seq": 1, "role": "market", "text": "x" * 500}]
        self.addCleanup(setattr, agents, "get_job", real_get)
        self.addCleanup(setattr, agents, "read_events", real_events)
        self.sign_in("reader@example.com")
        full = self.client.get(f"/api/agents/job/{job['id']}").get_json()
        lean = self.client.get(f"/api/agents/job/{job['id']}?events=0").get_json()
        self.assertEqual(len(full["events"]), 1)
        self.assertEqual(lean["events"], [])
        self.assertEqual(lean["sections"], full["sections"])
        self.assertTrue(any((s.get("role") or {}).get("key") == "portfolio"
                            for s in lean["sections"]))

    # ── the page ──────────────────────────────────────────────────────────
    def test_the_page_knows_whether_a_session_exists(self):
        page = self.client.get("/history/NBIS").get_data(as_text=True)
        self.assertIn("const SIGNED_IN = false;", page)
        self.assertIn('id="taCard"', page)
        self.assertIn('id="researchSavedSelect"', page)
        self.assertIn("ystime.js", page)
        self.sign_in("reader@example.com")
        page = self.client.get("/history/NBIS").get_data(as_text=True)
        self.assertIn("const SIGNED_IN = true;", page)
        self.assertIn('href="/agents?ticker=NBIS"', page)

    def test_the_research_desk_still_renders_with_the_shared_formatter(self):
        self.sign_in("reader@example.com")
        page = self.client.get("/agents?ticker=NBIS").get_data(as_text=True)
        self.assertIn("ystime.js", page)
        self.assertNotIn("window.YSTime = (function", page)


if __name__ == "__main__":
    unittest.main()
