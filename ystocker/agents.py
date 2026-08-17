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

import json
import logging
import os
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

# Keep at most this many job records on disk.
MAX_JOBS = 60

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
DEFAULT_LANGUAGE = os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE", "Simplified Chinese (简体中文)")


def _child_env() -> dict[str, str]:
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
    env.setdefault("TRADINGAGENTS_OUTPUT_LANGUAGE", DEFAULT_LANGUAGE)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def has_llm_key() -> bool:
    """Whether some usable provider credential is present."""
    return any(os.environ.get(k, "").strip() for k in
               ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))

_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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

# Plumbing check that never touches an LLM, so the job lifecycle can be tested
# without spending credits.
if os.environ.get("YSTOCKER_AGENT_SELFTEST") == "1":
    emit({"ok": True, "selftest": True, "ticker": ticker, "date": day,
          "decision": "HOLD (selftest — no LLM call was made)",
          "report": "## Self-test\n\nSubprocess, argument passing, result "
                    "delimiting and JSON parsing all exercised.\n\n"
                    "### Markdown coverage\n\n- **bold** and *italic* text\n"
                    "- a ratio written as P/E < 20 & a stray > character\n"
                    "- curly quotes \u201clike this\u201d and an em dash \u2014 here\n"
                    "\n| col | value |\n| --- | --- |\n| a | 1 |\n"
                    # Real runs default to Chinese, so the free plumbing
                    # check exercises the CJK font path in the PDF too --
                    # otherwise the only thing catching a font regression
                    # is a run that costs money.
                    "\n### 中文渲染检查\n\n"
                    "验证 PDF 中文字体（STSong-Light）与换行是否正常；"
                    "中英文混排：NVDA 同比增长 94%。\n"})
    raise SystemExit(0)

try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG
except Exception as exc:
    emit({"ok": False, "error": "import failed: %s: %s" % (type(exc).__name__, exc)})
    raise SystemExit(2)

try:
    cfg = DEFAULT_CONFIG.copy()
    # TradingAgents reads TRADINGAGENTS_* itself, so anything exported by the
    # parent already applies; only override what we pass explicitly.
    graph = TradingAgentsGraph(debug=False, config=cfg)
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
# Job store — plain files, so a worker recycle cannot lose a run
# ---------------------------------------------------------------------------

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
        return _reap(json.loads(_job_path(job_id).read_text()))
    except Exception:
        return None


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    if not JOB_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    # Exclude the children's sidecar result files, which are not job records.
    records = (p for p in JOB_DIR.glob("*.json") if not p.name.endswith(".result.json"))
    for p in sorted(records, key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            j = _reap(json.loads(p.read_text()))
            # The transcript can be long; a listing does not need it.
            j.pop("log", None)
            j.pop("report", None)
            out.append(j)
        except Exception:
            continue
    return out


def _prune() -> None:
    try:
        files = sorted((p for p in JOB_DIR.glob("*.json")
                        if not p.name.endswith(".result.json")),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        for p in files[MAX_JOBS:]:
            p.unlink(missing_ok=True)
            # Drop the child's sidecar with its job, else it leaks forever.
            p.with_suffix("").with_suffix(".result.json").unlink(missing_ok=True)
    except Exception as exc:
        log.debug("agents: prune failed: %s", exc)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def submit(ticker: str, day: str, user: str, selftest: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Validate and queue a run. Returns (job_id, error)."""
    ticker = (ticker or "").strip().upper()
    day = (day or "").strip() or _date.today().isoformat()

    # Validated even though the child is invoked as an argv list rather than a
    # shell string: these land in a filename and in a report header.
    if not _TICKER_RE.match(ticker):
        return None, "Invalid ticker"
    if not _DATE_RE.match(day):
        return None, "Invalid date (expected YYYY-MM-DD)"
    try:
        _date.fromisoformat(day)
    except ValueError:
        return None, "Invalid date"

    job_id = uuid.uuid4().hex[:16]
    job = {
        "id": job_id,
        "ticker": ticker,
        "date": day,
        "user": user,
        "selftest": bool(selftest),
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
    }
    _write(job)
    _prune()

    threading.Thread(target=_run, args=(job_id,), daemon=True,
                     name=f"agent-{job_id}").start()
    log.info("agents: queued %s %s@%s (selftest=%s) for %s",
             job_id, ticker, day, selftest, user)
    return job_id, None


def _quota_day() -> Optional[str]:
    try:
        from ystocker import quota

        return quota.today()
    except Exception:  # noqa: BLE001 - quota is not essential to running
        return None


def _refund_preflight(job: dict[str, Any], why: str) -> None:
    """Return the quota for a run that never reached an LLM.

    Only called on failures that provably happened before the child could make
    a request: no slot, no interpreter, or an import error inside the child. A
    failure after that point may already have spent an unknown number of calls,
    so it is not refunded -- silently handing quota back for those would let a
    run that burns credits and then dies be repeated for free.
    """
    if job.get("selftest"):
        return          # never charged
    try:
        from ystocker import quota

        quota.refund(job.get("user"), job.get("quota_day"))
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

        env = _child_env()
        if job.get("selftest"):
            env["YSTOCKER_AGENT_SELFTEST"] = "1"

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
            elif not payload.get("ok"):
                job.update(status="error", error=str(payload.get("error", "unknown error")))
            else:
                job.update(status="done", decision=payload.get("decision"),
                           report=payload.get("report") or "")
        job["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write(job)
        # Exit code 2 is the runner's "could not import tradingagents", raised
        # before any client is constructed, so nothing was spent.
        if rc == 2:
            _refund_preflight(job, "child could not import tradingagents")
        log.info("agents: %s finished status=%s rc=%s in %.1fs",
                 job_id, job.get("status"), rc, elapsed)
    finally:
        _slot.release()


# Third-party chatter that appears on every run and means nothing to whoever is
# reading the report. Each is a *known* benign condition, not a class of error:
# leaving real warnings visible is the point of showing a log at all.
_NOISE = (
    # google-genai, once per client. See _child_env: both key names held the
    # same value; the alias is now removed, so this is belt and braces.
    "Both GOOGLE_API_KEY and GEMINI_API_KEY are set",
    # google-genai style advice about its own API, not a problem with the run.
    "Direct use of automatic function calling (AFC)",
    # FRED is an optional data source and no key is configured; TradingAgents
    # already falls through to the next vendor by design.
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
        "allowlist_size": len(allowed_emails()),
        "provider": DEFAULT_PROVIDER,
        "deep_model": DEFAULT_DEEP_MODEL,
        "quick_model": DEFAULT_QUICK_MODEL,
        "thinking": DEFAULT_THINKING,
        "debate_rounds": DEFAULT_DEBATE_ROUNDS,
        "risk_rounds": DEFAULT_RISK_ROUNDS,
        "language": DEFAULT_LANGUAGE,
        "has_key": key_ok,
        "ready": ta_dir_ok and interp_ok and key_ok and bool(allowed_emails()),
    }
