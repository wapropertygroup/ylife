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
RUN_TIMEOUT = float(os.environ.get("TRADINGAGENTS_TIMEOUT", "1500"))

# Keep at most this many job records on disk.
MAX_JOBS = 60

_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# One run at a time; further submissions queue behind it.
_slot = threading.Semaphore(1)
_io_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def allowed_emails() -> set[str]:
    """Emails permitted to start a run.

    Each run spends real API credits and yStocker is otherwise public, so an
    empty allowlist denies everyone rather than defaulting open.
    """
    raw = os.environ.get("AGENTS_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_allowed(email: Optional[str]) -> bool:
    return bool(email) and email.strip().lower() in allowed_emails()


# ---------------------------------------------------------------------------
# The child program
# ---------------------------------------------------------------------------

# Printed around the JSON payload because TradingAgents streams a lot of its own
# output on stdout when debug is on; without delimiters the result is
# unparseable in among the debate transcript.
_BEGIN = "<<<YSTOCKER_RESULT_BEGIN>>>"
_END = "<<<YSTOCKER_RESULT_END>>>"

_RUNNER = r'''
import json, sys, os
BEGIN, END = sys.argv[3], sys.argv[4]
ticker, day = sys.argv[1], sys.argv[2]

def emit(payload):
    sys.stdout.write("\n" + BEGIN + "\n" + json.dumps(payload) + "\n" + END + "\n")
    sys.stdout.flush()

# Plumbing check that never touches an LLM, so the job lifecycle can be tested
# without spending credits.
if os.environ.get("YSTOCKER_AGENT_SELFTEST") == "1":
    emit({"ok": True, "selftest": True, "ticker": ticker, "date": day,
          "decision": "HOLD (selftest — no LLM call was made)",
          "report": "Self-test run: subprocess, argument passing, result "
                    "delimiting and JSON parsing all exercised."})
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

    report = ""
    if isinstance(state, dict):
        for key in ("final_trade_decision", "trader_investment_plan",
                    "investment_plan", "market_report"):
            val = state.get(key)
            if isinstance(val, str) and val.strip():
                report += "## %s\n\n%s\n\n" % (key.replace("_", " ").title(), val.strip())
    emit({"ok": True, "ticker": ticker, "date": day,
          "decision": decision if isinstance(decision, str) else json.dumps(decision, default=str),
          "report": report.strip()})
except Exception as exc:
    emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
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


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{8,36}", job_id or ""):
        return None
    try:
        return json.loads(_job_path(job_id).read_text())
    except Exception:
        return None


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    if not JOB_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(JOB_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            j = json.loads(p.read_text())
            # The transcript can be long; a listing does not need it.
            j.pop("log", None)
            j.pop("report", None)
            out.append(j)
        except Exception:
            continue
    return out


def _prune() -> None:
    try:
        files = sorted(JOB_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        for p in files[MAX_JOBS:]:
            p.unlink(missing_ok=True)
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
    }
    _write(job)
    _prune()

    threading.Thread(target=_run, args=(job_id,), daemon=True,
                     name=f"agent-{job_id}").start()
    log.info("agents: queued %s %s@%s (selftest=%s) for %s",
             job_id, ticker, day, selftest, user)
    return job_id, None


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
        return

    try:
        job.update(status="running",
                   started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _write(job)

        env = os.environ.copy()
        if job.get("selftest"):
            env["YSTOCKER_AGENT_SELFTEST"] = "1"
        # Unbuffered so the transcript reaches us as it is produced.
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [_interpreter(), "-c", _RUNNER, job["ticker"], job["date"], _BEGIN, _END]
        started = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=TA_DIR, env=env, capture_output=True, text=True,
                timeout=RUN_TIMEOUT,
            )
            out, err, rc = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired:
            job.update(status="error",
                       error=f"Run exceeded {RUN_TIMEOUT:.0f}s and was killed")
            out, err, rc = "", "", -9
        except FileNotFoundError:
            job.update(status="error",
                       error=(f"Interpreter not found: {_interpreter()}. Set "
                              "TRADINGAGENTS_PYTHON to a python that can import "
                              "tradingagents."))
            out, err, rc = "", "", -1
        except Exception as exc:
            job.update(status="error", error=f"{type(exc).__name__}: {exc}")
            out, err, rc = "", "", -1

        elapsed = round(time.time() - started, 1)
        job["elapsed_sec"] = elapsed
        job["returncode"] = rc

        # Keep the tail: a debate transcript can be very large.
        tail = (out[-6000:] if out else "") + (("\n[stderr]\n" + err[-3000:]) if err else "")
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
        log.info("agents: %s finished status=%s rc=%s in %.1fs",
                 job_id, job.get("status"), rc, elapsed)
    finally:
        _slot.release()


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
    return {
        "today": _date.today().isoformat(),
        "ta_dir": TA_DIR,
        "ta_dir_exists": ta_dir_ok,
        "interpreter": interp,
        "interpreter_exists": interp_ok,
        "timeout_sec": RUN_TIMEOUT,
        "allowlist_size": len(allowed_emails()),
        "ready": ta_dir_ok and interp_ok and bool(allowed_emails()),
    }
