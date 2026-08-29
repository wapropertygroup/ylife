"""
ystocker.agents
~~~~~~~~~~~~~~~
Runs the TradingAgents multi-agent analysis as a short-lived subprocess and
exposes the result as a polled job.

Why a subprocess per run, not a resident service
------------------------------------------------
Measured on the production box before building this: 3839 MB total with 1289 MB
available, and ystocker alone already at 1637 MB of its 1800 MB MemoryMax, on
2 vCPUs shared by eight web apps. A long-lived TradingAgents service would hold
langchain plus six provider SDKs in that headroom permanently and grow during a
debate, and because the kernel picks its OOM victim globally it would not only
kill itself.

This reuses the pattern ``forecast.py:run_forecast_isolated`` already
established here for exactly the same reason — see its docstring for the nine
OOM kills in 48 h that motivated it. A separate process returns every byte on
exit, unconditionally. It is ``subprocess`` and not ``multiprocessing`` for the
same two reasons given there: ``fork`` would inherit module-level cache locks
held by this app's background threads, and ``spawn`` re-imports the parent's
``__main__``, which under gunicorn is the venv launcher script.

Driven through the programmatic API rather than the CLI: ``cli/main.py`` uses
``typer.prompt`` and ``questionary``, so it blocks on stdin and cannot be
automated. ``TradingAgentsGraph(...).propagate(ticker, date)`` is the documented
non-interactive entry point.

Concurrency is capped at one run. Two vCPUs also serve eight apps, and a debate
is IO-bound on LLM calls but its process is not free; queued jobs wait.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

JOB_DIR = Path(__file__).parent.parent / "cache" / "agents"
TA_RUNTIME_DIR = JOB_DIR.parent / "tradingagents"
JOBS_TABLE_NAME = "ystocker-agent-jobs"
JOBS_USER_INDEX = "user-created-at-index"
JOBS_STATUS_INDEX = "status-created-at-index"
_DDB_MAX_PAYLOAD_BYTES = 380_000

# Sidecar marking a job whose report has been emailed. Declared here rather than
# in report_email because this module owns the directory layout and is what
# prunes it; report_email reads the name from here so the two cannot drift and
# start leaking a file per run. Deliberately not a ``.json`` suffix: _record_paths
# globs those and would read a marker as a bogus job.
EMAIL_MARKER_SUFFIX = ".emailed"

# Where the other repo lives and which interpreter can import it. ystocker's own
# venv has none of langchain, so this must point at TradingAgents' environment.
TA_DIR = os.environ.get("TRADINGAGENTS_DIR", str(Path.home() / "workspace" / "TradingAgents"))
TA_PYTHON = os.environ.get("TRADINGAGENTS_PYTHON", "")

# A debate with default settings runs for minutes. Past this we kill it rather
# than let a wedged run hold the single slot forever.
# Pro on both roles with 3 debate and 3 risk rounds runs far longer than the
# package's 1-round Flash default — tens of minutes rather than a few. The old
# 25-minute ceiling would have killed good runs mid-debate.
RUN_TIMEOUT = float(os.environ.get("TRADINGAGENTS_TIMEOUT", "5400"))

# Keep this many recent jobs in listings and in the local runtime cache.
# DynamoDB retains the durable history beyond this window.
MAX_JOBS = 60

# How many finished reports the /agents page shows a visitor who is not signed
# in. Also the hard ceiling on that endpoint's ``limit``: the showcase is a
# sample, and letting a caller ask for 60 would turn it into a way to walk every
# report on disk.
SHOWCASE_SIZE = 10

# Default to Gemini because this app already holds a Gemini key: SSM parameter
# /ystocker/GEMINI_API_KEY is loaded into os.environ by _load_secrets_from_ssm()
# at startup, so a run needs no extra secret in production.
#
# The name has to be bridged, though. TradingAgents resolves a provider's key
# through llm_clients/api_key_env.py, which maps "google" -> GOOGLE_API_KEY and
# has no knowledge of GEMINI_API_KEY. Without the alias below the child would
# import cleanly and then fail at the first LLM call with a missing-credentials
# error, which is a confusing way to discover a naming mismatch.
DEFAULT_PROVIDER = os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "google")
# Ids taken from tradingagents/llm_clients/model_catalog.py, not invented: an
# unknown model id fails deep inside the provider SDK.
#
# Pro on both roles, not just the deep one. quick_think handles tool calls and
# summarisation, so Flash there is the usual cost/latency trade — running Pro
# everywhere is deliberately the expensive, highest-quality setting.
DEFAULT_DEEP_MODEL = os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "gemini-3.1-pro-preview")
DEFAULT_QUICK_MODEL = os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "gemini-3.1-pro-preview")

# Gemini 3.x takes a string thinking_level; google_client.py shows Pro accepts
# low/high (Flash also takes minimal/medium).
DEFAULT_THINKING = os.environ.get("TRADINGAGENTS_GOOGLE_THINKING_LEVEL", "high")

# Depth. The package ships 1 round each; more rounds mean the bull/bear and risk
# debates actually go back and forth instead of each side speaking once.
DEFAULT_DEBATE_ROUNDS = os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "3")
DEFAULT_RISK_ROUNDS = os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS", "3")

# Report language. agent_utils appends "Write your entire response in {lang}."
# to the report prompts; the internal debate stays English by the package's own
# design, for reasoning quality, so this changes the deliverable and not the
# reasoning.
#
# It follows the UI language of whoever asked for the run rather than being a
# server-wide constant. The report is the *only* thing a run produces, and it
# costs minutes and real credits, so handing an English-mode reader a Chinese
# one wastes the entire run. ``submit`` resolves the caller's language once and
# records it on the job, so ``_run`` reproduces the same choice even if the
# request that queued it is long gone.
LANGUAGES = {"en": "English", "zh": "Simplified Chinese (简体中文)"}

# Deployment pin: set TRADINGAGENTS_OUTPUT_LANGUAGE to force every run to one
# language whatever the UI says -- including a language this app has no UI for.
# Unset, which is the normal case, means follow the caller. The pin keeps the
# name TradingAgents itself reads so it can be set from the unit file alone.
FORCED_LANGUAGE = os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE", "").strip()


def resolve_language(code: Optional[str]) -> str:
    """The report language for a UI language code (``en`` / ``zh``).

    Anything unrecognised -- including a caller that sent no code at all, such
    as a bare API client -- resolves to English, matching the default in
    ``static/i18n.js``.
    """
    if FORCED_LANGUAGE:
        return FORCED_LANGUAGE
    return LANGUAGES.get((code or "").strip().lower(), LANGUAGES["en"])


def language_code(language: str) -> str:
    """The UI language a resolved report language corresponds to, ``""`` if none.

    Recorded alongside the language itself so the page can tell a reader when a
    finished report is not in the language they are now reading in. Empty for a
    pinned language outside :data:`LANGUAGES`, which is also a mismatch worth
    surfacing rather than a value to guess at.
    """
    for code, name in LANGUAGES.items():
        if name == language:
            return code
    return ""


def _child_env(language: str = "") -> dict[str, str]:
    """Environment for the run, with the Gemini key aliased for TradingAgents."""
    env = os.environ.copy()
    gemini = env.get("GEMINI_API_KEY", "").strip()
    if gemini and not env.get("GOOGLE_API_KEY", "").strip():
        env["GOOGLE_API_KEY"] = gemini
        # Drop the original rather than leaving both set. google-genai warns
        # "Both GOOGLE_API_KEY and GEMINI_API_KEY are set" on every client it
        # builds, which is once per agent -- pure noise in the run log, and
        # the two hold the same value here anyway.
        env.pop("GEMINI_API_KEY", None)
    # TradingAgents reads these itself, so setting them here is enough to steer
    # the run without patching its config.
    env.setdefault("TRADINGAGENTS_LLM_PROVIDER", DEFAULT_PROVIDER)
    env.setdefault("TRADINGAGENTS_DEEP_THINK_LLM", DEFAULT_DEEP_MODEL)
    env.setdefault("TRADINGAGENTS_QUICK_THINK_LLM", DEFAULT_QUICK_MODEL)
    env.setdefault("TRADINGAGENTS_GOOGLE_THINKING_LEVEL", DEFAULT_THINKING)
    env.setdefault("TRADINGAGENTS_MAX_DEBATE_ROUNDS", DEFAULT_DEBATE_ROUNDS)
    env.setdefault("TRADINGAGENTS_MAX_RISK_ROUNDS", DEFAULT_RISK_ROUNDS)
    # Assigned, not setdefault: this is the run's own choice and must win over
    # whatever this process inherited -- and what it inherited is precisely
    # FORCED_LANGUAGE, which would otherwise pin every run to one language and
    # reintroduce the bug this parameter exists to fix.
    env["TRADINGAGENTS_OUTPUT_LANGUAGE"] = language or resolve_language(None)
    env.setdefault("TRADINGAGENTS_CACHE_DIR", str(TA_RUNTIME_DIR / "cache"))
    env.setdefault("TRADINGAGENTS_RESULTS_DIR", str(TA_RUNTIME_DIR / "logs"))
    env.setdefault(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        str(TA_RUNTIME_DIR / "memory" / "trading_memory.md"),
    )
    env["PYTHONUNBUFFERED"] = "1"
    return env


def has_llm_key() -> bool:
    """Whether some usable provider credential is present."""
    return any(os.environ.get(k, "").strip() for k in
               ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))

_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
# A-share codes are all digits, so the letter-initial pattern above rejects every
# one of them -- 002185 came back "Invalid ticker" even though the TradingAgents
# side handles 沪深京 codes. Accepts the decorated forms people actually type,
# matching tradingagents.dataflows.a_stock.normalize_code: 600519, SH600519,
# 600519.SS, 600519.SH.
#
# Deliberately not checking the exchange prefix (60/00/30/68/43/83...) even though
# a_stock.is_a_share does. This regex exists for safety, not market correctness --
# the value lands in a filename and a report header -- and an unlisted US ticker
# like ZZZZ is likewise accepted here and fails later in the run. Prefix-checking
# in two places would also drift as 北交所 ranges are added.
_ASHARE_RE = re.compile(r"^(?:(?:SH|SZ|BJ)\.?)?\d{6}(?:\.(?:SS|SZ|SH|BJ))?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BASE_ANALYSTS = ("market", "social", "news", "fundamentals")
ASTOCK_ANALYSTS = BASE_ANALYSTS + ("policy", "hot_money", "lockup")


def valid_ticker(ticker: str) -> bool:
    """Whether a symbol is safe to interpolate into a path and a header."""
    t = (ticker or "").strip().upper()
    return bool(_TICKER_RE.match(t) or _ASHARE_RE.match(t))


def analysts_for_ticker(ticker: str) -> tuple[str, ...]:
    """Seven analysts for A shares; the established four for other markets."""
    return ASTOCK_ANALYSTS if _ASHARE_RE.match((ticker or "").strip().upper()) else BASE_ANALYSTS

# One run at a time; further submissions queue behind it.
_slot = threading.Semaphore(1)
_io_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

# Parsed allowlist, cached against the raw string so the warning below is
# emitted when the value changes rather than on every request.
_allow_cache: tuple[str, frozenset[str]] = ("", frozenset())
_allow_lock = threading.Lock()

# Comma is the documented separator, but semicolons, spaces and newlines are all
# plausible ways to write a list. Accepting them matters because the failure mode
# is silent: "a@x.com b@y.com" parsed on commas alone yields one entry that
# matches nobody, so everyone is denied with no hint as to why.
_SPLIT_RE = re.compile(r"[,;\s]+")


def allowed_emails() -> set[str]:
    """The optional allowlist override, empty when unset.

    Empty means open: access is governed by the daily quotas in ``quota.py``,
    not by membership. See :func:`is_allowed`. A non-empty value restricts runs
    to those addresses, as an emergency brake.
    """
    global _allow_cache
    raw = os.environ.get("AGENTS_ALLOWED_EMAILS", "")
    cached_raw, cached_set = _allow_cache
    if raw == cached_raw:
        return set(cached_set)

    entries = [e.strip().lower() for e in _SPLIT_RE.split(raw) if e.strip()]
    # An entry without an "@" is a typo, not an address. Dropping it silently is
    # how an allowlist ends up denying the person it was meant to admit, so say
    # so once.
    good = {e for e in entries if "@" in e and "." in e.split("@")[-1]}
    bad = [e for e in entries if e not in good]

    with _allow_lock:
        if bad:
            log.warning("agents: ignoring %d malformed allowlist entr%s: %s",
                        len(bad), "y" if len(bad) == 1 else "ies", ", ".join(bad[:5]))
        if not good and raw.strip():
            log.warning("agents: AGENTS_ALLOWED_EMAILS is set but no valid address "
                        "parsed — everyone will be denied")
        _allow_cache = (raw, frozenset(good))
    return set(good)


def is_allowed(email: Optional[str]) -> bool:
    """Whether this signed-in address may run at all.

    /agents is open to any signed-in user, bounded by the daily quotas in
    ``quota.py`` rather than by membership. ``AGENTS_ALLOWED_EMAILS`` is kept as
    an *optional* override: when it is non-empty only those addresses may run,
    which is a usable kill switch if the quotas ever prove insufficient. Empty
    (the default) means open.
    """
    if not email:
        return False
    allow = allowed_emails()
    return (not allow) or email.strip().lower() in allow


# ---------------------------------------------------------------------------
# The child program
# ---------------------------------------------------------------------------

# Printed around the JSON payload because TradingAgents streams a lot of its own
# output on stdout when debug is on; without delimiters the result is
# unparseable in among the debate transcript.
_BEGIN = "<<<YSTOCKER_RESULT_BEGIN>>>"
_END = "<<<YSTOCKER_RESULT_END>>>"

_RUNNER = r'''
import json, sys, os, tempfile
BEGIN, END = sys.argv[3], sys.argv[4]
ticker, day = sys.argv[1], sys.argv[2]
RESULT_PATH = sys.argv[5] if len(sys.argv) > 5 else ""

def emit(payload):
    # Written to a file as well as stdout. stdout only reaches the parent while
    # the parent is alive, and this process deliberately outlives it (gunicorn
    # recycles the worker that launched us), so the file is the durable channel.
    if RESULT_PATH:
        try:
            d = os.path.dirname(RESULT_PATH)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, RESULT_PATH)
        except Exception:
            pass          # stdout may still get through; never fail the run here
    sys.stdout.write("\n" + BEGIN + "\n" + json.dumps(payload) + "\n" + END + "\n")
    sys.stdout.flush()

try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG
except Exception as exc:
    emit({"ok": False, "error": "import failed: %s: %s" % (type(exc).__name__, exc)})
    raise SystemExit(2)

try:
    cfg = DEFAULT_CONFIG.copy()

    # Live progress. The graph streams full state snapshots (stream_mode
    # "values"), so each agent's output can be published the moment it exists
    # instead of the page showing nothing for ten minutes. Written as one JSON
    # object per line to a sidecar file, for the same reason the result is: this
    # process outlives the web worker that started it, so a file is the only
    # channel that survives.
    EVENTS_PATH = RESULT_PATH.replace(".result.json", ".events.jsonl") if RESULT_PATH else ""

    # state key -> (role key, whether it lives inside a debate sub-dict)
    PLAIN = [
        ("market_report", "market"),
        ("sentiment_report", "sentiment"),
        ("news_report", "news"),
        ("fundamentals_report", "fundamentals"),
        ("policy_report", "policy"),
        ("hot_money_report", "hot_money"),
        ("lockup_report", "lockup"),
        ("trader_investment_plan", "trader"),
    ]
    DEBATES = [
        ("investment_debate_state", [("bull_history", "bull"),
                                     ("bear_history", "bear"),
                                     ("judge_decision", "research_mgr")]),
        ("risk_debate_state", [("aggressive_history", "aggressive"),
                               ("conservative_history", "conservative"),
                               ("neutral_history", "neutral"),
                               ("judge_decision", "portfolio")]),
    ]

    _seen = {}
    _seq = [0]

    def publish(role, text):
        """Append one event, if this role's text actually changed."""
        text = (text or "").strip()
        if not text or _seen.get(role) == text:
            return
        _seen[role] = text
        _seq[0] += 1
        if not EVENTS_PATH:
            return
        try:
            # Append-only and line-oriented: a reader can tail it safely while
            # this keeps writing, and a torn final line is simply skipped.
            with open(EVENTS_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": _seq[0], "role": role,
                                     "chars": len(text), "body": text},
                                    ensure_ascii=False) + "\n")
                fh.flush()
        except Exception:
            pass          # progress reporting must never break a run

    def on_progress(chunk):
        if not isinstance(chunk, dict):
            return
        for key, role in PLAIN:
            publish(role, chunk.get(key))
        for key, fields in DEBATES:
            sub = chunk.get(key)
            if isinstance(sub, dict):
                for field, role in fields:
                    publish(role, sub.get(field))

    # TradingAgents reads TRADINGAGENTS_* itself, so anything exported by the
    # parent already applies; only override what we pass explicitly.
    # The parent picks the roster from the ticker's market and passes it down; an
    # empty or absent variable means an older parent, so fall back to the four
    # that every market supports rather than guessing at A-share specialists.
    selected = tuple(
        part.strip() for part in
        os.environ.get("YSTOCKER_SELECTED_ANALYSTS", "").split(",")
        if part.strip()
    )
    graph = TradingAgentsGraph(selected_analysts=selected or BASE_ANALYSTS,
                               debug=False, config=cfg,
                               progress_callback=on_progress)
    state, decision = graph.propagate(ticker, day)

    # Prefer the package's own report builder: reporting.write_report_tree
    # assembles every section (all four analysts, the bull/bear/manager debate,
    # the trader plan, risk and portfolio) into complete_report.md, and its
    # docstring says it exists for exactly this headless case. Hand-picking a
    # few state keys, as this did first, silently dropped most of the analysis.
    report = ""
    try:
        import tempfile as _tf
        from tradingagents.reporting import write_report_tree
        with _tf.TemporaryDirectory() as td:
            complete = write_report_tree(state, ticker, td)
            if complete and os.path.exists(complete):
                report = open(complete, encoding="utf-8").read()
    except Exception as _exc:
        sys.stderr.write("report_tree unavailable (%s); falling back to state keys\n" % _exc)

    if not report.strip() and isinstance(state, dict):
        for key in ("final_trade_decision", "trader_investment_plan",
                    "investment_plan", "market_report"):
            val = state.get(key)
            if isinstance(val, str) and val.strip():
                report += "## %s\n\n%s\n\n" % (key.replace("_", " ").title(), val.strip())
    emit({"ok": True, "ticker": ticker, "date": day,
          "decision": decision if isinstance(decision, str) else json.dumps(decision, default=str),
          "report": report.strip()})
except Exception as exc:
    # A traceback here is the only diagnosis available: the message alone can be
    # useless. pandas raises EmptyDataError("No columns to parse from file")
    # without naming the file, and the whole point of the log is to say which.
    import traceback as _tb
    _trace = _tb.format_exc()
    sys.stderr.write(_trace)

    detail = "%s: %s" % (type(exc).__name__, exc)

    # A zero-byte cache file poisons every later run: stockstats_utils reads it
    # with pd.read_csv before its own `cached.empty` guard can see it, so the
    # crash repeats until someone deletes the file by hand. The upstream write
    # is a plain to_csv, so any kill mid-write leaves exactly this. Clear the
    # unusable files and say so, rather than making the user debug a cache they
    # do not know exists.
    if isinstance(exc, Exception) and "No columns to parse" in str(exc):
        removed = []
        try:
            from tradingagents.default_config import DEFAULT_CONFIG as _C
            _dir = _C.get("data_cache_dir", "")
            if _dir and os.path.isdir(_dir):
                for _n in os.listdir(_dir):
                    _p = os.path.join(_dir, _n)
                    try:
                        if os.path.isfile(_p) and os.path.getsize(_p) == 0:
                            os.unlink(_p)
                            removed.append(_n)
                    except OSError:
                        pass
        except Exception:
            pass
        if removed:
            detail += (" -- cleared %d empty cache file(s): %s. "
                       "Re-run to refetch." % (len(removed), ", ".join(removed[:5])))
        else:
            detail += (" -- a data file could not be parsed. If it recurs, "
                       "clear the TradingAgents data cache and re-run.")

    emit({"ok": False, "error": detail})
    raise SystemExit(3)
'''


def _interpreter() -> str:
    """Python that can import tradingagents.

    Never ystocker's own interpreter by default — it has none of langchain, so
    falling back to it would turn a misconfiguration into a confusing
    ImportError inside the child.
    """
    if TA_PYTHON:
        return TA_PYTHON
    for candidate in (Path(TA_DIR) / "venv" / "bin" / "python",
                      Path(TA_DIR) / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return "python3"


# ---------------------------------------------------------------------------
# Job store — DynamoDB for durable history, local files for live sidecars
# ---------------------------------------------------------------------------

_jobs_table = None
_jobs_ddb_unavailable_until = 0.0
_jobs_ddb_lock = threading.Lock()
_ddb_backfilled_ids: set[str] = set()


def _mark_jobs_ddb_unavailable() -> None:
    global _jobs_table, _jobs_ddb_unavailable_until
    _jobs_table = None
    _jobs_ddb_unavailable_until = time.time() + 60


def _get_jobs_table():
    """Return the agent-history table, with a short retry backoff."""
    global _jobs_table, _jobs_ddb_unavailable_until
    if _jobs_table is not None:
        return _jobs_table
    if time.time() < _jobs_ddb_unavailable_until:
        return None
    with _jobs_ddb_lock:
        if _jobs_table is not None:
            return _jobs_table
        if time.time() < _jobs_ddb_unavailable_until:
            return None
        try:
            import boto3

            ddb = boto3.resource(
                "dynamodb",
                region_name=os.environ.get("AWS_REGION", "us-west-2"),
            )
            table = ddb.Table(JOBS_TABLE_NAME)
            table.load()
            _jobs_table = table
            log.info("agents: DynamoDB history connected: %s", JOBS_TABLE_NAME)
        except Exception as exc:
            log.warning("agents: DynamoDB history unavailable: %s", exc)
            _mark_jobs_ddb_unavailable()
        return _jobs_table


def _encode_job(job: dict[str, Any]) -> bytes:
    raw = json.dumps(
        job,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload = gzip.compress(raw, compresslevel=6)
    if len(payload) > _DDB_MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"compressed job is {len(payload)} bytes; DynamoDB limit is 400 KB"
        )
    return payload


def _decode_job(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    payload = item.get("payload")
    if payload is None:
        return None
    try:
        if isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = gzip.decompress(bytes(payload))
        job = json.loads(raw)
        return job if isinstance(job, dict) else None
    except (EOFError, OSError, TypeError, ValueError) as exc:
        log.warning("agents: unreadable DynamoDB job %s: %s", item.get("id"), exc)
        return None


def _ddb_write(job: dict[str, Any]) -> bool:
    table = _get_jobs_table()
    if table is None:
        return False
    try:
        item = {
            "id": job["id"],
            "payload": _encode_job(job),
        }
        owner = (job.get("user") or "").strip().lower()
        created_at = str(job.get("created_at") or "").strip()
        status = str(job.get("status") or "").strip().lower()
        if owner:
            item["user"] = owner
        if created_at:
            item["created_at"] = created_at
        if status:
            item["status"] = status
        table.put_item(Item=item)
        return True
    except ValueError as exc:
        log.error("agents: job %s is too large for DynamoDB: %s", job.get("id"), exc)
    except Exception as exc:
        log.warning("agents: DynamoDB write failed for %s: %s", job.get("id"), exc)
        _mark_jobs_ddb_unavailable()
    return False


def _ddb_get(job_id: str) -> Optional[dict[str, Any]]:
    table = _get_jobs_table()
    if table is None:
        return None
    try:
        item = table.get_item(Key={"id": job_id}).get("Item")
        return _decode_job(item) if item else None
    except Exception as exc:
        log.warning("agents: DynamoDB read failed for %s: %s", job_id, exc)
        _mark_jobs_ddb_unavailable()
        return None


def _ddb_jobs(
    *,
    user: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = MAX_JOBS,
) -> tuple[list[dict[str, Any]], bool]:
    table = _get_jobs_table()
    if table is None or limit <= 0:
        return [], False
    try:
        items: list[dict[str, Any]] = []
        if user or status:
            from boto3.dynamodb.conditions import Key

            if user:
                index_name = JOBS_USER_INDEX
                condition = Key("user").eq(user.strip().lower())
            else:
                index_name = JOBS_STATUS_INDEX
                condition = Key("status").eq((status or "").strip().lower())
            request: dict[str, Any] = {
                "IndexName": index_name,
                "KeyConditionExpression": condition,
                "ScanIndexForward": False,
                "Limit": limit,
            }
            while len(items) < limit:
                response = table.query(**request)
                items.extend(response.get("Items", []))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                request["ExclusiveStartKey"] = last_key
                request["Limit"] = limit - len(items)
        else:
            request = {}
            while True:
                response = table.scan(**request)
                items.extend(response.get("Items", []))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                request["ExclusiveStartKey"] = last_key

        jobs = [job for item in items if (job := _decode_job(item)) is not None]
        jobs.sort(key=lambda job: job.get("created_at") or "", reverse=True)
        return jobs[:limit], True
    except Exception as exc:
        log.warning("agents: DynamoDB history query failed: %s", exc)
        _mark_jobs_ddb_unavailable()
        return [], False

def _events_path(job_id: str) -> Path:
    """Where the child appends its progress events."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    return JOB_DIR / f"{job_id}.events.jsonl"


def read_events(job_id: str, since: int = 0) -> list[dict[str, Any]]:
    """Progress events with ``seq`` greater than ``since``.

    Tolerant of a torn last line: the child appends while this reads, so the
    final line can be half-written. A malformed line is skipped rather than
    failing the poll -- it will be complete on the next one.
    """
    path = _events_path(job_id)
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if int(ev.get("seq", 0)) > since:
                    out.append(ev)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - progress is decoration
        log.debug("agents: unreadable events for %s: %s", job_id, exc)
    return out


def _result_path(job_id: str) -> Path:
    """Where the detached child writes its result, independent of stdout."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    return JOB_DIR / f"{job_id}.result.json"


def _job_path(job_id: str) -> Path:
    return JOB_DIR / f"{job_id}.json"


def _write(job: dict[str, Any]) -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    path = _job_path(job["id"])
    with _io_lock:
        fd, tmp = tempfile.mkstemp(dir=JOB_DIR, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(job, f)
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    _ddb_write(job)


def salvage_from_events(job_id: str) -> tuple[str, Optional[str]]:
    """Rebuild a report from the streamed events, for a run that never wrote one.

    A run publishes each agent's output as it lands, so when the child dies after
    the debate but before writing its result, the whole analysis is still sitting
    in the events file. Discarding it and telling the user to run again would
    throw away roughly twenty Gemini Pro calls they have already paid for.

    Emits the same ``### <Role Name>`` headings the package's own report builder
    uses, so the result parses through ``agent_roles.split_sections`` exactly like
    a real report and renders identically on the page and in the PDF.
    """
    from ystocker.agent_roles import ROLES

    latest: dict[str, str] = {}
    for ev in read_events(job_id):
        role, body = ev.get("role"), (ev.get("body") or "").strip()
        if role and body:
            latest[role] = body        # last write per role wins

    parts, decision = [], None
    for role in ROLES:                 # the cast's own order, not arrival order
        body = latest.get(role["key"])
        if not body:
            continue
        parts.append(f"### {role['name']}\n\n{body}\n")
        if role["key"] == "portfolio":
            decision = body.splitlines()[0].strip() if body.strip() else None
    return "\n".join(parts), decision


def _reap(job: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Settle a job whose supervising thread died, from durable evidence.

    The thread that supervises a run is a daemon inside a gunicorn worker, and
    the worker is recycled every ~200 requests -- a threshold a long run's own
    status polling blows straight through. The child is detached so it keeps
    going, but nothing is left to record what it produced.

    So this checks, in order of authority: the result file the child writes
    itself, then whether its pid is still alive, and only then the clock. Doing
    it on read keeps it correct across workers with no extra thread, since every
    worker reads the same three facts and reaches the same verdict.
    """
    if not job or job.get("status") not in ("queued", "running"):
        return job

    # 1. The child may have finished after its supervisor died. Its result file
    #    is authoritative, so adopt it rather than declaring the run lost.
    try:
        rp = _result_path(job["id"])
        if rp.exists():
            payload = json.loads(rp.read_text(encoding="utf-8"))
            if payload.get("ok"):
                job.update(status="done", decision=payload.get("decision"),
                           report=payload.get("report") or "")
            else:
                job.update(status="error",
                           error=str(payload.get("error", "unknown error")))
            job["finished_at"] = datetime.fromtimestamp(
                rp.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            job.setdefault("orphaned", True)
            try:
                _write(job)
            except Exception:
                pass
            # This is the path that matters most for the email: the supervising
            # thread died, so nothing else will ever notice this run finished.
            # background=True because _reap runs inside a request.
            _email_report(job, background=True)
            return job
    except Exception:
        pass   # fall through to the liveness and clock checks

    # 2. Is the detached child still alive? Checked before the clock, but not
    #    trusted past the timeout: pids are recycled by the OS, so "alive" for
    #    longer than a run can possibly take means we are looking at some other
    #    process that inherited the number.
    pid, alive = job.get("pid"), False
    if pid:
        try:
            os.kill(int(pid), 0)
            alive = True
        except (ProcessLookupError, ValueError):
            alive = False     # gone, and it left no result
        except PermissionError:
            alive = True      # exists, owned by another uid

    stamp = job.get("started_at") or job.get("created_at")
    if not stamp:
        return job
    try:
        began = datetime.fromisoformat(stamp)
    except ValueError:
        return job
    if began.tzinfo is None:
        began = began.replace(tzinfo=timezone.utc)
    # A launched-but-dead pid that left no result is finished now, whatever the
    # clock says. A live one gets the full timeout plus grace, after which it is
    # wedged or the pid has been reused -- either way it is not coming back.
    # With no pid at all (killed before launch) fall back to the timeout, and
    # allow a queued job one full run's wait for the single slot.
    if pid and not alive:
        limit = 0.0
    elif job.get("started_at"):
        limit = RUN_TIMEOUT + 300
    else:
        limit = 2 * RUN_TIMEOUT + 300
    if (datetime.now(timezone.utc) - began).total_seconds() < limit:
        return job
    # Last chance before writing this run off: the streamed events may already
    # contain the entire analysis.
    try:
        recovered, decision = salvage_from_events(job["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("agents: salvage failed for %s: %s", job.get("id"), exc)
        recovered, decision = "", None

    if recovered.strip():
        job.update(status="done", report=recovered, recovered=True,
                   finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if decision and not job.get("decision"):
            job["decision"] = decision
        log.warning("agents: %s recovered %d chars from its stream after the "
                    "runner died", job.get("id"), len(recovered))
        try:
            _write(job)
        except Exception:
            pass
        _email_report(job, background=True)
        return job

    job.update(status="error",
               error="Runner did not report a result (the server process was "
                     "most likely restarted mid-run). Please run it again.",
               finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        _write(job)
    except Exception:
        pass   # a stale record is not worth failing the read over
    return job


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{8,36}", job_id or ""):
        return None
    try:
        job = json.loads(_job_path(job_id).read_text())
    except Exception:
        job = _ddb_get(job_id)
    return _reap(job)


def owns(job: Optional[dict[str, Any]], email: Optional[str]) -> bool:
    """Whether this address owns this run.

    A report is private to whoever paid for it: these transcripts state position
    sizes and entry levels, and /agents is open to any signed-in user. A job with
    no recorded owner belongs to nobody and is readable by nobody, which is the
    safe direction for records written before ownership was enforced.
    """
    if not job or not email:
        return False
    owner = (job.get("user") or "").strip().lower()
    return bool(owner) and owner == email.strip().lower()


def can_read(job: Optional[dict[str, Any]], email: Optional[str]) -> bool:
    """Whether this address may read this run.

    Owners always may. VIPs may read anyone's, which is the one deliberate hole
    in the privacy rule ``owns`` implements -- the VIP list is the site owner,
    who pays for the API calls and needs to see what the box actually produced.

    This also lets a VIP read the pre-ownership records ``owns`` hides from
    everybody. That is the intended consequence rather than an oversight: those
    were hidden because there was no way to tell whose they were and therefore no
    safe person to show them to, and a reader allowed to see every owner's reports
    is trivially allowed to see an unowned one.

    Write paths deliberately do NOT use this. Adding a follow-up question mutates
    the job record, so a VIP asking questions on someone else's run would put
    turns the owner never wrote into the owner's own view of it.
    """
    if owns(job, email):
        return True
    if not job or not email:
        return False
    from ystocker.quota import is_vip

    return is_vip(email)


def _record_paths() -> list[Path]:
    """Job record files, newest first.

    The children's sidecars (``.result.json``, ``.events.jsonl``) live in the
    same directory but are not job records, and reading one as a job yields a
    bogus entry. Excluded in one place here rather than in each caller, which is
    what let the listing and the pruner drift apart previously.

    Each mtime is read once, up front, rather than inside the sort's key: a run
    finishing calls ``_prune()`` and deletes records while a search is walking
    them, and a ``stat()`` on a file that vanished mid-sort raises
    FileNotFoundError out of ``sorted()``. That race widened once the history
    box began searching on every keystroke instead of once per page load.
    """
    if not JOB_DIR.exists():
        return []
    stamped: list[tuple[float, Path]] = []
    for p in JOB_DIR.glob("*.json"):
        if p.name.endswith(".result.json") or p.name.endswith(".events.jsonl"):
            continue
        try:
            stamped.append((p.stat().st_mtime, p))
        except OSError:
            continue   # pruned between the glob and here
    stamped.sort(key=lambda sp: sp[0], reverse=True)
    return [p for _, p in stamped]


def _local_jobs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for path in _record_paths():
        try:
            job = json.loads(path.read_text())
            if isinstance(job, dict) and job.get("id"):
                jobs.append(job)
        except (OSError, TypeError, ValueError):
            continue
    return jobs


def _records(
    *,
    user: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = MAX_JOBS,
) -> list[dict[str, Any]]:
    durable, ddb_available = _ddb_jobs(user=user, status=status, limit=limit)
    durable = [
        job for job in durable
        if (user is None or owns(job, user))
        and (not status or (job.get("status") or "").lower() == status.lower())
    ]
    durable_ids = {job.get("id") for job in durable}
    merged = {job["id"]: job for job in durable if job.get("id")}

    for job in _local_jobs():
        if user is not None and not owns(job, user):
            continue
        if status and (job.get("status") or "").lower() != status.lower():
            continue
        job_id = job["id"]
        if (
            ddb_available
            and job_id not in durable_ids
            and job_id not in _ddb_backfilled_ids
            and _ddb_write(job)
        ):
            _ddb_backfilled_ids.add(job_id)
        merged[job_id] = job

    records: list[dict[str, Any]] = []
    for job in merged.values():
        reaped = _reap(job)
        if reaped is not None:
            records.append(reaped)
    records.sort(key=lambda job: job.get("created_at") or "", reverse=True)
    return records[:limit]


# The one section a search hit may carry in full. A search result is something
# the reader wants to *read*, and the Portfolio Manager's turn is where the call
# is actually stated, so it travels with the listing instead of costing a second
# request per row.
#
# Capped because a search returns up to 60 hits. The section measured 610 bytes
# on a real 39 KB report, but it is LLM-authored and nothing upstream bounds its
# length, so one unlucky run must not turn a single search into a megabyte of
# JSON. The clip is reported rather than hidden -- see ``truncated`` -- so the UI
# can say so and point at the full report rather than quietly ending mid-argument.
_PM_MAX_CHARS = 4000


def portfolio_section(report: str) -> Optional[dict[str, Any]]:
    """The Portfolio Manager's turn from ``report``, or None if it has none.

    The role's own metadata (name, Chinese name, icon, colour) is returned beside
    the body so a caller renders the speaker exactly as the conversation view
    does, rather than keeping a second copy of the cast that can drift from
    ``agent_roles.ROLES``.

    None is not an error and every caller must handle it: a run that has not
    produced a report yet, a body that carries no role headings at all, and a
    real report that stopped before the decision all reach it.
    """
    body = (report or "").strip()
    if not body:
        return None
    from ystocker.agent_roles import split_sections

    for sec in split_sections(body):
        role = sec.get("role")
        if not role or role.get("key") != "portfolio":
            continue
        text = sec.get("body") or ""
        clipped = len(text) > _PM_MAX_CHARS
        return {
            "key": role["key"],
            "name": role["name"],
            "zh": role["zh"],
            "icon": role["icon"],
            "color": role["color"],
            "team": sec.get("team"),
            "team_zh": sec.get("team_zh"),
            "body": text[:_PM_MAX_CHARS] if clipped else text,
            "truncated": clipped,
        }
    return None


def _listing_entry(job: dict[str, Any],
                   with_portfolio: bool = False) -> dict[str, Any]:
    """Trim a job record down to what a listing or search hit needs.

    ``has_report`` is recorded before the body is dropped: a run can finish with
    an empty report, and the UI offers a PDF link only where there is something
    to render. Deciding that from ``status == 'done'`` alone produced links that
    404'd.

    ``with_portfolio`` attaches the Portfolio Manager's turn -- extracted here,
    while the body is still in hand, which is the whole point: the caller renders
    the decision without a second fetch. Off by default so the plain history and
    every other listing keep their previous size.
    """
    entry = dict(job)
    entry["has_report"] = bool((entry.get("report") or "").strip())
    if with_portfolio:
        entry["portfolio"] = portfolio_section(entry.get("report") or "")
    # The transcript can be long; a listing does not need it.
    entry.pop("log", None)
    entry.pop("report", None)
    return entry


def list_jobs(limit: int = 20, user: Optional[str] = None,
              all_users: bool = False) -> list[dict[str, Any]]:
    """Recent runs, newest first, restricted to ``user`` when given.

    Filtering happens before the limit, not after: slicing the newest N records
    and then discarding other people's would show a user an empty history
    whenever N busier runs happened to come first.

    ``all_users`` lifts the restriction for a VIP viewer. It is a separate flag
    rather than "pass user=None", so that forgetting to pass the viewer's email
    fails closed to an empty list instead of publishing everyone's history.
    """
    scope = None if all_users else user
    out: list[dict[str, Any]] = []
    for job in _records(user=scope):
        if len(out) >= limit:
            break
        if not all_users and (user is None or not owns(job, user)):
            continue
        out.append(_listing_entry(job))
    return out


# Search ranking tiers, best first. Kept as named constants because the tier
# number is also what orders the results.
_RANK_TICKER_EXACT = 0
_RANK_TICKER_PREFIX = 1
_RANK_DATE = 2
_RANK_TICKER_SUBSTR = 3


def _match_rank(job: dict[str, Any], q: str) -> Optional[int]:
    """How well ``job`` matches query ``q``, or None if it does not.

    A ticker is the common case, so an exact symbol ranks above a prefix and a
    prefix above a mid-string hit -- searching "T" should not bury T under
    TSM, TSLA and MSFT. A query that looks like a date matches the analysis
    date instead, so the same box finds "everything I ran for 2026-08-14"
    without a second control.

    The ticker checks must stay *above* the date check, and the order is the only
    thing keeping the two apart: an A-share code is all digits ("002384.SZ",
    "515050.SH"), so "looks like a date" cannot be decided from the first
    character. Reversing them would make every Shanghai and Shenzhen symbol
    unsearchable -- including from the history list, where clicking a ticker
    submits exactly this query.
    """
    ticker = (job.get("ticker") or "").upper()
    if ticker:
        if ticker == q:
            return _RANK_TICKER_EXACT
        if ticker.startswith(q):
            return _RANK_TICKER_PREFIX
    # Nothing matched as a symbol, so a leading digit means a date is being
    # typed. Prefix-only: a date is entered left to right.
    if q[0].isdigit() and (job.get("date") or "").startswith(q):
        return _RANK_DATE
    if ticker and q in ticker:
        return _RANK_TICKER_SUBSTR
    return None


def search_jobs(query: str = "", user: Optional[str] = None,
                status: Optional[str] = None,
                limit: int = 50, all_users: bool = False,
                require_report: bool = False,
                with_portfolio: bool = False) -> dict[str, Any]:
    """Search analysis reports by ticker or analysis date.

    Returns ``{"jobs": [...], "found": int, "scanned": int, "truncated": bool,
    "skipped_empty": int}`` where ``found`` counts every match and ``jobs`` holds
    at most ``limit`` of them, so the UI can say "showing 50 of 63" instead of
    silently dropping the tail.

    ``require_report`` drops hits with no report body, so every row returned is
    something there is text to read. ``skipped_empty`` reports how many went that
    way rather than leaving them to vanish unaccounted for -- a search that
    quietly hides half its matches reads as a search that found nothing.

    ``with_portfolio`` attaches each hit's Portfolio Manager turn (see
    ``portfolio_section``). Both default off so ``list_jobs`` and any other
    caller keep the previous payload and the previous row set.

    Ownership is enforced per record via ``owns()``: these transcripts carry
    position sizes, so a query must not become a way to probe what other people
    ran. ``all_users`` lifts that for a VIP viewer, matching ``can_read``, since a
    reader allowed to open every report gains nothing from being unable to find
    one. It is a separate flag rather than "pass user=None" so that omitting the
    viewer's email fails closed to no results instead of searching everything.
    """
    q = (query or "").strip().upper()
    status = (status or "").strip().lower() or None

    hits: list[tuple[int, int, dict[str, Any]]] = []
    scanned = 0
    skipped_empty = 0
    for idx, job in enumerate(_records(user=None if all_users else user)):
        if not all_users and not owns(job, user):
            continue
        scanned += 1
        if status and (job.get("status") or "").lower() != status:
            continue
        # An empty query lists everything the filters allow, which is what
        # makes the search box degrade into the plain history view.
        rank = _match_rank(job, q) if q else _RANK_TICKER_EXACT
        if rank is None:
            continue
        # Counted after the match, not before: "hidden" should mean "matched your
        # query but had nothing to read", not "exists somewhere on disk".
        if require_report and not (job.get("report") or "").strip():
            skipped_empty += 1
            continue
        # idx is the newest-first position, so it breaks ties within a tier
        # by recency without a second sort key.
        hits.append((rank, idx, _listing_entry(job, with_portfolio=with_portfolio)))

    hits.sort(key=lambda h: (h[0], h[1]))
    found = len(hits)
    log.info("agents: search q=%r status=%s -> %d/%d owned records (%d empty skipped)",
             q, status or "any", found, scanned, skipped_empty)
    return {
        "jobs": [h[2] for h in hits[:limit]],
        "found": found,
        "scanned": scanned,
        "truncated": found > limit,
        "skipped_empty": skipped_empty,
    }


# ---------------------------------------------------------------------------
# Public showcase — finished reports shown to visitors who are not signed in
# ---------------------------------------------------------------------------
#
# Every other agent read is owner-only (see ``owns``), because a report states
# entry levels and position sizes and the person who ran it paid for it. This is
# the one deliberate exception: /agents is otherwise a dead end for a visitor who
# has not signed in, and a sample of real output is what tells them what the page
# produces. The exception is made safe by *anonymising* rather than authorising —
# nothing here reveals who ran a report, and the sample never carries a body
# except through ``showcase_job``.
#
# How many are shown is ``SHOWCASE_SIZE``, declared beside ``MAX_JOBS`` because
# the two together decide what fraction of recent work a visitor sees.

# What a visitor who is not signed in may see of a run. An allowlist and not a
# denylist on purpose: a job record also carries the owner's address, the
# runner's stderr, its pid and its quota day. Naming the publishable fields means
# a field added to the record later is private until someone decides otherwise,
# instead of public until someone notices. ``report`` is absent by design —
# ``showcase_job`` adds it for the caller to split, and the listing never has it.
_PUBLIC_FIELDS = (
    "id", "ticker", "date", "status", "decision",
    "created_at", "started_at", "finished_at", "elapsed_sec",
    # Kept so a sampled report cannot pass itself off as better than it is.
    "degraded", "fallback_models", "recovered",
)


def showcase_enabled() -> bool:
    """Whether finished runs may be sampled for visitors who are not signed in.

    On by default: the sample is the only thing that tells a first-time visitor
    what this page does. ``AGENTS_SHOWCASE=0`` turns it off without a deploy.
    """
    return os.environ.get("AGENTS_SHOWCASE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def showcase_emails() -> set[str]:
    """Owners whose finished runs may be sampled; empty means any owner.

    Set ``AGENTS_SHOWCASE_EMAILS`` to publish only a demo account's runs and keep
    every other user's reports out of the sample entirely. Empty (the default)
    samples anyone's finished work, which is acceptable only because the sample is
    anonymous: see ``_PUBLIC_FIELDS``.
    """
    raw = os.environ.get("AGENTS_SHOWCASE_EMAILS", "")
    return {e.strip().lower() for e in _SPLIT_RE.split(raw) if e.strip()}


def _is_showcase(job: Optional[dict[str, Any]]) -> bool:
    """Whether this run may appear in the public sample.

    Finished and non-empty: a queued or errored run says nothing about what the
    agents produce. Ownership is deliberately *not* checked — that is the point
    of the sample — so every caller must go through ``_publishable`` rather than
    hand a raw record out.
    """
    if not job or not showcase_enabled():
        return False
    if job.get("status") != "done":
        return False
    if not (job.get("report") or "").strip():
        return False
    only = showcase_emails()
    if only and (job.get("user") or "").strip().lower() not in only:
        return False
    return True


def _publishable(job: dict[str, Any]) -> dict[str, Any]:
    """A run reduced to the fields a visitor who is not signed in may see."""
    out = {k: job[k] for k in _PUBLIC_FIELDS if k in job}
    out["has_report"] = bool((job.get("report") or "").strip())
    return out


# Pool cache. Building the pool loads up to ``MAX_JOBS`` durable job records
# whose report bodies run to tens of KB each, and unlike
# ``search_jobs`` this feeds an *unauthenticated* endpoint, so rebuilding it per
# request would let anyone outside turn a page load into megabytes of JSON
# parsing on a box with two vCPUs shared by eight apps. The cached pool holds no
# report bodies (``_PUBLIC_FIELDS`` excludes them), so it stays small.
_SHOWCASE_TTL = 60.0
_showcase_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
_showcase_lock = threading.Lock()


def _showcase_pool() -> list[dict[str, Any]]:
    """Every publishable run, newest first, cached for ``_SHOWCASE_TTL`` seconds.

    A run that has just finished takes up to a minute to join the pool, which is
    a fair trade for not re-reading the whole directory on every anonymous hit.
    """
    global _showcase_cache
    ts, pool = _showcase_cache
    now = time.monotonic()
    # Keyed on the timestamp rather than on ``pool`` being non-empty, so a site
    # with no finished reports yet caches the empty answer too instead of
    # rescanning the directory on every request.
    if ts and now - ts < _SHOWCASE_TTL:
        return pool

    fresh: list[dict[str, Any]] = []
    for job in _records(status="done"):
        if _is_showcase(job):
            fresh.append(_publishable(job))

    with _showcase_lock:
        _showcase_cache = (now, fresh)
    return fresh


def showcase_jobs(limit: int = SHOWCASE_SIZE) -> list[dict[str, Any]]:
    """A random sample of finished reports, anonymised, newest first.

    Sampled rather than truncated because that is the honest impression to give:
    always showing the newest ten would hide everything older, and a visitor who
    reloads learns there is more here than one screenful. The durable query
    returns the latest ``MAX_JOBS`` completed records, so the pool is a rolling
    window over recent work.

    The draw is stable for an hour rather than fresh per request -- see the seed
    below for why.
    """
    pool = _showcase_pool()
    if len(pool) <= limit:
        sample = list(pool)
    else:
        # Seeded with the current UTC hour rather than drawn from the global RNG,
        # so the ten hold still for an hour. Re-rolling per request meant a
        # visitor who opened one report and came back to the list found it
        # replaced by a fresh draw, and it defeats any cache in front of this
        # endpoint. An hour still re-rolls often enough that the page is not a
        # fixed advertisement for the same ten tickers.
        #
        # Stable given a stable pool, which is the honest guarantee: a run
        # finishing shifts the indices this seed picks, so the sample can change
        # within the hour when new work lands. That is preferable to seeding on
        # the pool's contents, which would re-roll on every finished run.
        rng = random.Random(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"))
        # Sampled by index and re-sorted so the newest-first order of
        # the durable query survives into the result; ``random.sample`` on the
        # records themselves would also shuffle them, and the page groups by date.
        sample = [pool[i] for i in sorted(rng.sample(range(len(pool)), limit))]
    log.info("agents: showcase served %d of %d eligible reports", len(sample), len(pool))
    return sample


def showcase_job(job_id: str) -> Optional[dict[str, Any]]:
    """One sampled report with its body, anonymised, or None if it may not be shown.

    Eligibility is re-checked against the durable record rather than trusted
    from a listing: the id is the only thing the caller supplies, and a run
    whose owner has since been dropped from ``AGENTS_SHOWCASE_EMAILS`` must stop
    being readable at the same moment it stops being listed.
    """
    job = get_job(job_id)
    if not _is_showcase(job):
        return None
    out = _publishable(job)
    out["report"] = job.get("report") or ""
    return out


def _prune() -> None:
    try:
        for p in _record_paths()[MAX_JOBS:]:
            p.unlink(missing_ok=True)
            # Drop the child's sidecars with its job, else they leak forever.
            stem = p.with_suffix("")
            stem.with_suffix(".result.json").unlink(missing_ok=True)
            stem.with_suffix(".events.jsonl").unlink(missing_ok=True)
            # The send-once marker for the report email. Safe to drop with the
            # record: notify() needs a job whose status is done, and the record
            # it would have to read is what this loop just deleted.
            stem.with_suffix(EMAIL_MARKER_SUFFIX).unlink(missing_ok=True)
    except Exception as exc:
        log.debug("agents: prune failed: %s", exc)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def submit(ticker: str, day: str, user: str,
           lang: str = "", paid: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Validate and queue a run. Returns (job_id, error).

    ``lang`` is the caller's UI language code, which selects the language the
    report is written in -- see :data:`LANGUAGES`.
    """
    ticker = (ticker or "").strip().upper()
    day = (day or "").strip() or _date.today().isoformat()

    # Validated even though the child is invoked as an argv list rather than a
    # shell string: these land in a filename and in a report header.
    if not valid_ticker(ticker):
        return None, "Invalid ticker"
    if not _DATE_RE.match(day):
        return None, "Invalid date (expected YYYY-MM-DD)"
    try:
        _date.fromisoformat(day)
    except ValueError:
        return None, "Invalid date"

    job_id = uuid.uuid4().hex[:16]
    language = resolve_language(lang)
    job = {
        "id": job_id,
        "ticker": ticker,
        "date": day,
        "user": user,
        # Frozen at submit time, not read at run time: the reader may toggle the
        # page to the other language while this sits in the queue, and a report
        # half-written in each would be worse than either.
        "language": language,
        "lang": language_code(language),
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "decision": None,
        "report": None,
        "error": None,
        "log": "",
        "queue_depth": max(0, 1 - _slot._value),  # informational only
        # The day the run was charged against, recorded here rather than derived
        # later: a run that starts at 23:59 and fails after midnight must be
        # refunded to the day it was taken from.
        "quota_day": _quota_day(),
        # True when this run was funded by a purchased credit rather than the
        # free daily allowance. Read by _refund_preflight.
        "quota_paid": bool(paid),
    }
    _write(job)
    _prune()

    threading.Thread(target=_run, args=(job_id,), daemon=True,
                     name=f"agent-{job_id}").start()
    log.info("agents: queued %s %s@%s (language=%s) for %s",
             job_id, ticker, day, language, user)
    return job_id, None


# Emitted by tradingagents.llm_clients.google_client when it switches models.
_FALLBACK_RE = re.compile(r"Retrying on ([A-Za-z0-9.\-]+) after ([A-Za-z0-9.\-]+) ran out")


def _fallback_models_used(stderr: str) -> list[str]:
    """Models the child fell back to, in first-seen order."""
    seen: list[str] = []
    for m in _FALLBACK_RE.finditer(stderr or ""):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _quota_day() -> Optional[str]:
    try:
        from ystocker import quota

        return quota.today()
    except Exception:  # noqa: BLE001 - quota is not essential to running
        return None


# ---------------------------------------------------------------------------
# Follow-up questions
#
# Once a run is finished, the analysis exists. A follow-up question does not need
# another run: re-running the graph would cost ~22 Gemini Pro calls and ten
# minutes to answer "why 3-6 months and not 12". So a question is one grounded
# call against the finished report instead.
#
# Deliberately on Flash rather than the deep model. Pro's *daily* cap is the
# binding constraint on how many analyses can be produced (250/day, about eleven
# runs), so spending Pro calls on conversation would directly cost the user
# analyses. Flash is a separate quota bucket, answers in seconds, and the hard
# reasoning has already been done and is sitting in the context.
# ---------------------------------------------------------------------------

CHAT_MODEL = os.environ.get("AGENTS_CHAT_MODEL", "gemini-2.5-flash")
CHAT_MAX_TURNS = 60            # per job, so one report cannot grow without bound
CHAT_QUESTION_MAX = 1200       # characters
# How much of the report to put in context. The whole thing is ~55 KB, which is
# well within Flash's window, so it is sent entire rather than retrieved from --
# a chunk-picking step here could silently drop the section a question is about.
CHAT_REPORT_MAX = 120_000

_CHAT_LOCK = threading.Lock()


def chat_turns(job: dict[str, Any]) -> list[dict[str, Any]]:
    turns = job.get("chat")
    return turns if isinstance(turns, list) else []


def append_chat(job_id: str, turns: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Append turns to a job's conversation and persist.

    Read-modify-write under a lock: two questions posted at once would otherwise
    each read the same list and the first answer would vanish. _write is atomic,
    so the file itself can never be torn.
    """
    with _CHAT_LOCK:
        job = get_job(job_id)
        if not job:
            return None
        existing = chat_turns(job)
        job["chat"] = (existing + turns)[-CHAT_MAX_TURNS:]
        _write(job)
        return job


def _chat_prompt(job: dict[str, Any], question: str) -> str:
    """Build the grounded prompt for a follow-up question."""
    report = (job.get("report") or "")[:CHAT_REPORT_MAX]
    decision = job.get("decision") or "(none recorded)"
    ticker, day = job.get("ticker", "?"), job.get("date", "?")

    prior = ""
    for turn in chat_turns(job)[-10:]:      # recent context only, to stay cheap
        who = "User" if turn.get("role") == "user" else "You"
        prior += f"\n{who}: {turn.get('text','')}"

    return (
        "You are the Portfolio Manager who signed off the following equity "
        f"analysis of {ticker} for {day}. The user is asking a follow-up "
        "question about your own decision.\n\n"
        "Rules you must follow:\n"
        "1. Answer from the analysis below. It is the only evidence you have.\n"
        "2. If the analysis does not address the question, say so plainly and "
        "briefly say what would be needed to answer it. Do not fill the gap "
        "with a plausible guess, and never invent a number, date or source.\n"
        "3. Do not silently change the recorded decision. If the question "
        "raises something that would genuinely alter it, say what and why.\n"
        "4. Reply in the same language as the analysis.\n"
        "5. Be direct and short — a few sentences to a short paragraph unless "
        "the question truly needs more. No preamble.\n"
        "6. This is analysis, not investment advice.\n\n"
        f"=== RECORDED DECISION ===\n{decision}\n\n"
        f"=== FULL ANALYSIS ===\n{report}\n\n"
        + (f"=== CONVERSATION SO FAR ==={prior}\n\n" if prior else "")
        + f"=== USER'S QUESTION ===\n{question}\n"
    )


def ask_manager(job: dict[str, Any], question: str) -> tuple[Optional[str], Optional[str]]:
    """Ask the Portfolio Manager a follow-up. Returns (answer, error)."""
    if not (job.get("report") or "").strip():
        return None, "This run has no report to discuss."
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        return None, "No LLM credential is configured."
    try:
        from google import genai

        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=CHAT_MODEL, contents=_chat_prompt(job, question))
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            return None, "The model returned an empty answer."
        return text, None
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        log.error("agents: follow-up failed for %s: %s", job.get("id"), exc)
        return None, f"{type(exc).__name__}: {exc}"


def _try_salvage(job: dict[str, Any], why: str) -> bool:
    """Turn a failed run into a recovered one when its stream holds the analysis.

    The child's own error is preserved in ``failed_with`` rather than discarded:
    the report is usable, but whatever broke still needs diagnosing, and a job
    that silently reports success would hide it.
    """
    try:
        recovered, decision = salvage_from_events(job["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("agents: salvage failed for %s: %s", job.get("id"), exc)
        return False
    if not recovered.strip():
        return False
    job["failed_with"] = job.get("error") or why
    job.update(status="done", report=recovered, recovered=True, error=None)
    if decision and not job.get("decision"):
        job["decision"] = decision
    log.warning("agents: %s failed (%s) but %d chars were recovered from its "
                "stream", job.get("id"), why, len(recovered))
    return True


def _refund_preflight(job: dict[str, Any], why: str) -> None:
    """Return the quota for a run that never reached an LLM.

    Only called on failures that provably happened before the child could make
    a request: no slot, no interpreter, or an import error inside the child. A
    failure after that point may already have spent an unknown number of calls,
    so it is not refunded -- silently handing quota back for those would let a
    run that burns credits and then dies be repeated for free.
    """
    if job.get("quota_refunded"):
        return          # never charged
    try:
        from ystocker import quota

        # paid=True refunds a purchased credit; paid=False decrements the free
        # daily counter. Refunding the wrong one either gives away a free run or
        # keeps money for an analysis that never ran.
        quota.refund(job.get("user"), job.get("quota_day"),
                     paid=bool(job.get("quota_paid")))
        job["quota_refunded"] = True
        job["quota_refund_reason"] = why
        _write(job)
        log.info("agents: refunded %s (%s)", job.get("id"), why)
    except Exception as exc:  # noqa: BLE001
        log.warning("agents: refund failed for %s: %s", job.get("id"), exc)


def _run(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    # Queue rather than run in parallel: one slot.
    acquired = _slot.acquire(timeout=RUN_TIMEOUT)
    if not acquired:
        job.update(status="error", error="Timed out waiting for a free run slot",
                   finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _write(job)
        _refund_preflight(job, "never got a run slot")
        return

    try:
        job.update(status="running",
                   started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _write(job)

        env = _child_env(job.get("language") or "")
        env["YSTOCKER_SELECTED_ANALYSTS"] = ",".join(
            analysts_for_ticker(job.get("ticker") or "")
        )

        result_path = str(_result_path(job_id))
        cmd = [_interpreter(), "-c", _RUNNER, job["ticker"], job["date"],
               _BEGIN, _END, result_path]
        started = time.time()
        try:
            # start_new_session detaches the child into its own process group so
            # it survives this worker. The run thread is a daemon inside a
            # gunicorn worker that is recycled every ~200 requests, and a long
            # run's own 4s status polling generates far more than that on its
            # own -- roughly 1350 requests over 90 minutes, against a 200-request
            # budget shared by 2 workers. Without this the run reliably kills the
            # worker that owns it, losing the analysis and the API spend with it.
            proc = subprocess.Popen(
                cmd, cwd=TA_DIR, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            job["pid"] = proc.pid
            _write(job)
            out, err = proc.communicate(timeout=RUN_TIMEOUT)
            out, err, rc = out or "", err or "", proc.returncode
        except subprocess.TimeoutExpired:
            # Kill the whole group: the child spawns its own workers.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=30)
            job.update(status="error",
                       error=f"Run exceeded {RUN_TIMEOUT:.0f}s and was killed")
            out, err, rc = "", "", -9
        except FileNotFoundError:
            job.update(status="error",
                       error=(f"Interpreter not found: {_interpreter()}. Set "
                              "TRADINGAGENTS_PYTHON to a python that can import "
                              "tradingagents."))
            out, err, rc = "", "", -1
            _refund_preflight(job, "interpreter missing")
        except Exception as exc:
            job.update(status="error", error=f"{type(exc).__name__}: {exc}")
            out, err, rc = "", "", -1

        elapsed = round(time.time() - started, 1)
        job["elapsed_sec"] = elapsed
        job["returncode"] = rc

        # Which models actually answered. When the configured model runs out of
        # its daily quota the child continues on a weaker one, and a report that
        # was mostly written by Flash must not be presented as though it came
        # from Pro. Scanned from the untruncated stderr, because the log tail
        # keeps only the last few KB and the notice can fall outside it.
        used = _fallback_models_used(err)
        if used:
            job["fallback_models"] = used
            job["degraded"] = True
            log.warning("agents: %s degraded to %s (daily quota)",
                        job_id, ", ".join(used))

        # Keep the tail: a debate transcript can be very large. stdout also
        # carries the result block, which the page renders separately, so only
        # the part before it is useful as a log.
        visible_out = out.split(_BEGIN)[0] if _BEGIN in out else out
        tail = (visible_out[-6000:] if visible_out.strip() else "")
        clean_err = _denoise(err)
        if clean_err:
            tail += "\n[stderr]\n" + clean_err[-3000:]
        job["log"] = tail.strip()

        if job.get("status") != "error":
            payload = _extract(out)
            if payload is None:
                job.update(status="error",
                           error=("The run produced no result block. Exit code "
                                  f"{rc}. See the log below."))
                _try_salvage(job, f"no result block (exit {rc})")
            elif not payload.get("ok"):
                job.update(status="error", error=str(payload.get("error", "unknown error")))
                # A failure at the very end still leaves a finished analysis in
                # the stream. This is not hypothetical: a bug of mine let three
                # runs debate to completion and then die in the package's own
                # bookkeeping, and all three were fully recoverable from their
                # events. Twenty-odd Pro calls each is too much to discard over
                # failed bookkeeping.
                _try_salvage(job, str(payload.get("error", ""))[:120])
            else:
                job.update(status="done", decision=payload.get("decision"),
                           report=payload.get("report") or "")
        job["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write(job)
        # Exit code 2 is the runner's "could not import tradingagents", raised
        # before any client is constructed, so nothing was spent.
        if rc == 2:
            _refund_preflight(job, "child could not import tradingagents")
        elif (rc == 3
              and job.get("status") == "error"
              and "PermissionError" in (job.get("error") or "")
              and ".tradingagents" in (job.get("error") or "")):
            _refund_preflight(job, "TradingAgents runtime path was not writable")
        # A run killed by the provider's daily cap returns no report at all, and
        # no amount of retrying today will change that. Charging the user for a
        # capacity failure they cannot influence, and got nothing from, is not
        # defensible -- so this one is refunded even though calls were spent.
        elif (job.get("status") == "error"
              and "RESOURCE_EXHAUSTED" in (job.get("error") or "")):
            _refund_preflight(job, "provider daily quota exhausted")
        log.info("agents: %s finished status=%s rc=%s in %.1fs",
                 job_id, job.get("status"), rc, elapsed)
    finally:
        _slot.release()

    # Mail the report, if it produced one. Deliberately *after* the slot is
    # released: SES can take seconds, and the single run slot is what a queued
    # run is blocked on -- holding it for a notification would delay somebody
    # else's analysis. Sent inline rather than on another thread because this is
    # already a background thread with nothing left to do.
    _email_report(job)


def _email_report(job: Optional[dict[str, Any]], background: bool = False) -> None:
    """Hand a finished job to the report mailer, tolerating its absence.

    Wrapped so that neither an import problem nor a bug in the renderer can
    reach the bookkeeping around it: by the time this is called the report is
    already durable, and losing the notification is a far smaller failure than
    losing the record of a run that cost twenty Pro calls.
    """
    try:
        from ystocker import report_email

        report_email.notify(job, background=background)
    except Exception as exc:  # noqa: BLE001
        log.warning("agents: report email failed for %s: %s",
                    (job or {}).get("id"), exc)


# Third-party chatter that appears on every run and means nothing to whoever is
# reading the report. Each is a *known* benign condition, not a class of error:
# leaving real warnings visible is the point of showing a log at all.
_NOISE = (
    # google-genai, once per client. See _child_env: both key names held the
    # same value; the alias is now removed, so this is belt and braces.
    "Both GOOGLE_API_KEY and GEMINI_API_KEY are set",
    # google-genai style advice about its own API, not a problem with the run.
    "Direct use of automatic function calling (AFC)",
    # FRED is an optional data source; TradingAgents falls through to the next
    # vendor if its key is unavailable.
    "Vendor 'fred' not configured",
    "Optional macro_data unavailable",
    "FRED_API_KEY environment variable is not set",
    # Reddit rate-limits anonymous RSS constantly; the fetcher retries and the
    # analysis proceeds without it.
    "Reddit RSS 429",
    "Reddit RSS fetch failed",
)


def _denoise(text: str) -> str:
    """Strip recurring third-party warnings from captured stderr.

    Whitelist-based on purpose: an unrecognised line is always kept, so a new
    failure mode shows up in the log instead of being silently swallowed.
    """
    if not text:
        return ""
    kept = [ln for ln in text.splitlines()
            if not any(n in ln for n in _NOISE)]
    # Collapse runs of blank lines left behind by the removals.
    out, blank = [], False
    for ln in kept:
        if ln.strip():
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _extract(stdout: str) -> Optional[dict[str, Any]]:
    """Pull the JSON payload out from between the sentinels."""
    if not stdout or _BEGIN not in stdout or _END not in stdout:
        return None
    try:
        chunk = stdout.rsplit(_BEGIN, 1)[1].split(_END, 1)[0].strip()
        return json.loads(chunk)
    except Exception as exc:
        log.warning("agents: result block unparseable: %s", exc)
        return None


def environment_report() -> dict[str, Any]:
    """What the page shows when a run cannot work, instead of failing opaquely."""
    interp = _interpreter()
    ta_dir_ok = Path(TA_DIR).is_dir()
    interp_ok = Path(interp).exists() or interp == "python3"
    key_ok = has_llm_key()
    return {
        "today": _date.today().isoformat(),
        "ta_dir": TA_DIR,
        "ta_dir_exists": ta_dir_ok,
        "interpreter": interp,
        "interpreter_exists": interp_ok,
        "timeout_sec": RUN_TIMEOUT,
        # Reported for transparency, not as a health signal: an empty allowlist
        # is the normal, open configuration.
        "allowlist_size": len(allowed_emails()),
        "allowlist_active": bool(allowed_emails()),
        "provider": DEFAULT_PROVIDER,
        "deep_model": DEFAULT_DEEP_MODEL,
        "quick_model": DEFAULT_QUICK_MODEL,
        "thinking": DEFAULT_THINKING,
        "debate_rounds": DEFAULT_DEBATE_ROUNDS,
        "risk_rounds": DEFAULT_RISK_ROUNDS,
        # Only set when a deployment pinned one language for everybody. Normally
        # empty, because the language is per-run and the page fills it in from
        # the reader's own toggle -- a server-rendered value would be wrong for
        # anyone whose language came from localStorage rather than the URL.
        "language": FORCED_LANGUAGE,
        "language_pinned": bool(FORCED_LANGUAGE),
        "has_key": key_ok,
        "a_share_analysts": list(ASTOCK_ANALYSTS),
        # Whether a run *can execute*, which is a question about the checkout,
        # the interpreter and the credential -- not about who is permitted to
        # start one. It used to include a non-empty allowlist, which became
        # wrong the moment an empty allowlist started meaning "open to any
        # signed-in user": the page then reported the runner as misconfigured
        # while listing no actual fault.
        "ready": ta_dir_ok and interp_ok and key_ok,
    }
