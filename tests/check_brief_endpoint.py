"""Exercise POST /api/market-brief through a Flask test client.

Covers the production entry point: routing, JSON shape, the memory-cache hit,
force_refresh bypassing that cache, and the 503 when no key is configured.
Seeds the cache so the happy paths cost no Gemini calls; the one generation path
is covered by check_brief_live.py.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_boot = (Path(__file__).parent / "check_brief_collector.py").read_text()
exec(_boot.split("FAILURES: list[str] = []")[0], {"__file__": str(Path(__file__))})

from flask import Flask  # noqa: E402

from ystocker import routes  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


app = Flask(__name__)
app.register_blueprint(routes.bp)
client = app.test_client()

print("=== route registration ===")
rules = sorted(str(r) for r in app.url_map.iter_rules() if "market-brief" in str(r))
check("/api/market-brief registered", "/api/market-brief" in rules, str(rules))
methods = {m for r in app.url_map.iter_rules()
           if str(r) == "/api/market-brief" for m in r.methods}
check("accepts POST", "POST" in methods)
check("does not accept GET", "GET" not in methods, str(sorted(methods)))

print()
print("=== 503 with no API key ===")
saved = os.environ.pop("GEMINI_API_KEY", None)
resp = client.post("/api/market-brief", json={"lang": "en"})
check("503 when unconfigured", resp.status_code == 503, str(resp.status_code))
check("error body", "error" in resp.get_json())
if saved is not None:
    os.environ["GEMINI_API_KEY"] = saved

print()
print("=== cached read path (no Gemini call) ===")
SEEDED = {
    "brief": "## 1. Indices\n\n| Index | Last |\n|---|---|\n| S&P 500 | 6,812.34 |\n",
    "generated_at": "2026-08-27 09:14 UTC",
    "sources_used": ["markets", "fed"],
    "sources_cold": ["housing"],
    "sources_stale": ["fed"],
}
with routes._BRIEF_CACHE_LOCK:
    routes._BRIEF_CACHE["en"] = {"ts": time.time(), "data": SEEDED}
    routes._BRIEF_CACHE["zh"] = {"ts": time.time(), "data": dict(SEEDED, brief="## 一、指数")}

resp = client.post("/api/market-brief", json={"lang": "en"})
body = resp.get_json()
check("200 from cache", resp.status_code == 200, str(resp.status_code))
check("returns 'brief' key", "brief" in body, str(sorted(body)))
check("markdown table survives JSON", "|---|---|" in body["brief"])
check("generated_at present", body.get("generated_at") == SEEDED["generated_at"])
check("sources_cold present", body.get("sources_cold") == ["housing"])
check("sources_stale present", body.get("sources_stale") == ["fed"])

print()
print("=== stored-row shaper carries the source lists ===")
shaped = routes._brief_from_ddb_item({
    "summary": "## x", "generated_at": "t",
    "sources_used": ["markets"], "sources_cold": ["housing"], "sources_stale": []})
check("brief mapped from 'summary'", shaped["brief"] == "## x")
check("sources_used carried", shaped["sources_used"] == ["markets"])
check("sources_cold carried", shaped["sources_cold"] == ["housing"])
check("from_cache flagged", shaped["from_cache"] is True)
missing = routes._brief_from_ddb_item({"summary": "## y"})
check("absent lists degrade to []", missing["sources_used"] == [] and missing["sources_cold"] == [])

resp_zh = client.post("/api/market-brief", json={"lang": "zh"})
check("zh served from its own cache slot",
      resp_zh.get_json()["brief"].startswith("## 一、指数"))

print()
print("=== lang validation ===")
resp = client.post("/api/market-brief", json={"lang": "klingon"})
check("unknown lang falls back to en",
      resp.get_json().get("brief") == SEEDED["brief"])
resp = client.post("/api/market-brief", json={})
check("missing lang defaults to en",
      resp.get_json().get("brief") == SEEDED["brief"])
resp = client.post("/api/market-brief", data="not json",
                   content_type="application/json")
check("malformed body does not 500", resp.status_code == 200, str(resp.status_code))

print()
print("=== market scoping ===")
CN_SEEDED = dict(SEEDED, brief="## 1. 亚太股指", market="cn")
with routes._BRIEF_CACHE_LOCK:
    routes._BRIEF_CACHE[routes._brief_mem_key("zh", "cn")] = {
        "ts": time.time(), "data": CN_SEEDED}
body = client.post("/api/market-brief", json={"lang": "zh", "market": "cn"}).get_json()
check("cn has its own cache slot", body["brief"] == "## 1. 亚太股指")
check("cn does not collide with us",
      client.post("/api/market-brief", json={"lang": "zh"}).get_json()["brief"]
      == "## 一、指数")
check("us ddb key keeps its original shape",
      routes._brief_ddb_key("zh") == "zh_brief_v1", routes._brief_ddb_key("zh"))
check("cn ddb key is distinct",
      routes._brief_ddb_key("zh", "cn") == "zh_cn_brief_v1",
      routes._brief_ddb_key("zh", "cn"))
check("mem keys distinct",
      routes._brief_mem_key("zh") != routes._brief_mem_key("zh", "cn"))
body = client.post("/api/market-brief", json={"lang": "zh", "market": "kr"}).get_json()
check("unknown market falls back to us", body["brief"] == "## 一、指数")

print()
print("=== force_refresh bypasses the cache ===")
calls: list[tuple] = []


def _fake_generate(lang, warm=False, app=None, market="us", sources=None):
    calls.append((lang, warm, market))
    return {"brief": "## regenerated", "generated_at": "now", "market": market,
            "sources_used": [], "sources_cold": [], "sources_stale": []}


real_generate, real_store = routes._generate_market_brief, routes._store_market_brief
routes._generate_market_brief = _fake_generate
# _store_market_brief returns the brief to serve — normally what it was given,
# but the stored one when that was built from more sources. The endpoint jsonifies
# its return value, so a stub returning None would 500.
routes._store_market_brief = lambda lang, result, today, market="us": result
try:
    resp = client.post("/api/market-brief", json={"lang": "en", "force_refresh": True})
    check("force_refresh regenerates", resp.get_json()["brief"] == "## regenerated")
    check("generator called exactly once", len(calls) == 1, str(calls))
    check("request path never warms", calls and calls[0][1] is False, str(calls))
    check("defaults to the us market", calls and calls[0][2] == "us", str(calls))

    calls.clear()
    client.post("/api/market-brief", json={"lang": "en"})
    check("without force_refresh the cache is used again", not calls, str(calls))

    # A worker with cold caches must not replace a richer stored brief.
    calls.clear()
    routes._store_market_brief = lambda lang, result, today, market="us": {
        "brief": "## the richer stored one", "generated_at": "earlier",
        "sources_used": ["a", "b", "c"], "sources_cold": [], "sources_stale": [],
        "from_cache": True, "superseded_by_cache": True}
    body = client.post("/api/market-brief",
                       json={"lang": "en", "force_refresh": True}).get_json()
    check("a thinner regeneration serves the stored brief",
          body["brief"] == "## the richer stored one")
    check("and says so", body.get("superseded_by_cache") is True)
finally:
    routes._generate_market_brief = real_generate
    routes._store_market_brief = real_store

print()
print("=== generator failure surfaces as 500, not a crash ===")


def _boom(lang, warm=False, app=None, market="us", sources=None):
    raise RuntimeError("gemini exploded")


routes._generate_market_brief = _boom
try:
    resp = client.post("/api/market-brief", json={"lang": "en", "force_refresh": True})
    check("500 on generator failure", resp.status_code == 500, str(resp.status_code))
    check("error message forwarded", "gemini exploded" in json.dumps(resp.get_json()))
finally:
    routes._generate_market_brief = real_generate

print()
if FAILURES:
    print(f"RESULT: FAIL — {len(FAILURES)}: {', '.join(FAILURES)}")
    sys.exit(1)
print("RESULT: OK")
