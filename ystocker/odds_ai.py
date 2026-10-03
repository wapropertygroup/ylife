"""
ystocker.odds_ai
~~~~~~~~~~~~~~~~
The AI read on /predictions: one grounded Gemini forecast of one event, set
beside the market's own price so the gap between them is the thing on screen,
and written into the ledger (``odds_ledger``) the moment it exists.

Why beside the price, and why the ledger
----------------------------------------
A probability with no price next to it is half a thought: 65% means one thing
when the market says 52% and another when it says 78%. So a read never stands
alone -- every outcome carries the market's figure from the snapshot the read
was asked against, and the difference in points. The model itself is *not*
shown those prices (see ``odds.build_read_prompt``): shown them, it copies
them, and a gap of zero is no information. And a read that is never checked is
an opinion with decimals, so each one is recorded before its market resolves
and scored against the market when it does. Nothing on the page claims the
reads are good; the ledger is where that is found out.

Cost, and what bounds it
------------------------
One read is one ``gemini-2.5-flash`` call with Google Search grounding:
a few thousand tokens in, a couple of thousand out. It is generated only when a
signed-in reader asks (``quota.try_consume_odds_read``: a small per-user daily
allowance and a global ceiling), and a fresh read of an event is reused by
everyone for :data:`READ_TTL` -- it is about public market data and holds
nothing about who asked. ``PREDICTIONS_AI=0`` is the kill switch.

The read is generated once and in both languages: the model returns one JSON
block with an English and a Chinese summary, so a Chinese reader gets the same
forecast in their language without a second grounded call -- and without two
forecasts of one event that could disagree.

Requests never wait on the model
--------------------------------
A grounded call takes ten to forty seconds, and gunicorn's ``--timeout 120``
would take a worker's other requests down with it if one stalled. So the POST
claims the event, starts a thread and answers 202; the page polls. The claim is
an ``O_CREAT|O_EXCL`` marker file, the same guard ``report_email`` uses, because
two workers each holding an in-memory "already running" set would both start
the same read -- and a marker older than :data:`RUN_STALE_SECONDS` is a crashed
run, not a running one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ystocker import odds

log = logging.getLogger(__name__)

READ_DIR = Path(__file__).parent.parent / "cache" / "predictions" / "reads"

#: How long one read of an event is served to everyone before a new one may
#: be requested. Long enough that a busy page costs one call per event per
#: half-day, short enough that a read is never older than the news it reflects
#: by more than a session.
READ_TTL = 12 * 3600

#: A running marker older than this belongs to a run that died.
RUN_STALE_SECONDS = 240

#: An error is shown, then forgotten, so the reader can try again.
ERROR_TTL = 15 * 60

MODEL = os.environ.get("PREDICTIONS_AI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

#: Under gunicorn's 120s, though the call runs in a thread: a stalled grounded
#: request should end as an error the reader can retry, not hang a thread.
_TIMEOUT_MS = 90_000

#: Sources and searches kept per read.
_MAX_SOURCES = 10
_MAX_QUERIES = 8

Generator = Callable[[str], tuple[str, list[dict[str, str]], list[str]]]


def enabled() -> bool:
    """On unless switched off, and only with a key to call the model with."""
    if os.environ.get("PREDICTIONS_AI", "1").strip() == "0":
        return False
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _stem(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def _path(key: str, suffix: str) -> Path:
    return READ_DIR / f"{_stem(key)}{suffix}"


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def latest(key: str) -> Optional[dict[str, Any]]:
    """The most recent read of *key*, whatever its age, or None."""
    data = _read_json(_path(key, ".json"))
    return data if data and data.get("key") == key else None


def is_fresh(read: Optional[Mapping[str, Any]], now: Optional[float] = None) -> bool:
    if not read:
        return False
    return (now or time.time()) - float(read.get("created_ts") or 0) < READ_TTL


def _running_since(key: str) -> Optional[float]:
    try:
        mtime = _path(key, ".running").stat().st_mtime
    except OSError:
        return None
    return mtime if time.time() - mtime < RUN_STALE_SECONDS else None


def status(key: str) -> dict[str, Any]:
    """What the page should show for *key*: a read (``done``), a read being
    made (``running``), a failed attempt (``error``) or nothing yet (``none``).

    A fresh read wins over everything; a stale one is still returned under
    ``done`` with ``fresh: false`` so the page can show it, dated, while
    offering a new one."""
    read = latest(key)
    if read and is_fresh(read):
        return {"status": "done", "fresh": True, "read": read}
    since = _running_since(key)
    if since is not None:
        out: dict[str, Any] = {"status": "running", "since": since}
        if read:
            out["read"] = read
        return out
    err = _read_json(_path(key, ".error"))
    if err and time.time() - float(err.get("at") or 0) < ERROR_TTL:
        out = {"status": "error", "error": err.get("reason") or "failed", "at": err.get("at")}
        if read:
            out["read"] = read
        return out
    if read:
        return {"status": "done", "fresh": False, "read": read}
    return {"status": "none"}


def fresh_keys(keys) -> set[str]:
    """Which of *keys* have a fresh read -- one ``stat`` and read per key, for
    the board's "AI read available" marks."""
    out = set()
    now = time.time()
    for key in keys:
        read = latest(key)
        if is_fresh(read, now):
            out.add(key)
    return out


def _claim(key: str) -> bool:
    path = _path(key, ".running")
    try:
        if time.time() - path.stat().st_mtime >= RUN_STALE_SECONDS:
            path.unlink()
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _release(key: str) -> None:
    try:
        _path(key, ".running").unlink()
    except OSError:
        pass


def start(event: Mapping[str, Any], *, market_as_of: Optional[float] = None,
          generate: Optional[Generator] = None, background: bool = True) -> str:
    """Begin a read of *event*. Returns ``fresh`` (one exists; nothing started),
    ``running`` (another request is already making it) or ``started``.

    The caller has already checked ``enabled()`` and spent the reader's quota;
    ``fresh`` and ``running`` are the cases where it should refund it.
    """
    key = str(event.get("key") or "")
    if not key:
        raise ValueError("event has no key")
    if is_fresh(latest(key)):
        return "fresh"
    if not _claim(key):
        return "running"
    try:
        _path(key, ".error").unlink()
    except OSError:
        pass
    snapshot = json.loads(json.dumps(event))     # the price the read is set beside, frozen
    if background:
        threading.Thread(target=_run, args=(snapshot, market_as_of, generate),
                         daemon=True, name=f"odds-read-{_stem(key)[:8]}").start()
    else:
        _run(snapshot, market_as_of, generate)
    return "started"


def build_read(event: Mapping[str, Any], parsed: Mapping[str, Any], *,
               sources: list[dict[str, str]], queries: list[str],
               market_as_of: Optional[float], now: Optional[datetime] = None,
               model: str = MODEL) -> dict[str, Any]:
    """The stored read: the event it was asked about, its probabilities, the
    market's from the same snapshot, the gap between them, and what it read."""
    now = now or datetime.now(timezone.utc)
    outcomes = []
    for o in odds.read_outcomes(event):
        ai_p = parsed["ai"].get(o["label"])
        if ai_p is None:
            continue
        outcomes.append({
            "label": o["label"],
            "id": o.get("id") or "",
            "market_p": o["p"],
            "ai_p": ai_p,
            "gap_pp": round((ai_p - o["p"]) * 100, 1),
        })
    return {
        "key": event.get("key"),
        "platform": event.get("platform"),
        "event_id": event.get("id"),
        "title": event.get("title"),
        "subtitle": event.get("subtitle") or "",
        "url": event.get("url"),
        "kind": event.get("kind"),
        "topic": event.get("topic"),
        "closes": event.get("closes"),
        "n_outcomes": event.get("n_outcomes"),
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_ts": now.timestamp(),
        "model": model,
        "market_as_of": market_as_of,
        "outcomes": outcomes,
        "confidence": parsed.get("confidence"),
        "evidence_date": parsed.get("evidence_date"),
        "en": parsed.get("en") or {},
        "zh": parsed.get("zh") or {},
        "sources": sources[:_MAX_SOURCES],
        "queries": queries[:_MAX_QUERIES],
    }


def _run(event: Mapping[str, Any], market_as_of: Optional[float],
         generate: Optional[Generator]) -> None:
    key = str(event.get("key"))
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = odds.build_read_prompt(event, today=today)
        text, sources, queries = (generate or _gemini)(prompt)
        parsed = odds.parse_read(text, event)
        read = build_read(event, parsed, sources=sources, queries=queries,
                          market_as_of=market_as_of)
        if not read["outcomes"]:
            raise odds.ReadParseError("missing", "no outcome survived")
        _write_json(_path(key, ".json"), read)
        log.info("odds_ai: read %s (%s, %d sources)", key, read["confidence"], len(read["sources"]))
        try:
            from ystocker import odds_ledger

            odds_ledger.record(read)
        except Exception:  # noqa: BLE001 - the read stands even if the record fails
            log.exception("odds_ai: could not record %s in the ledger", key)
    except odds.ReadParseError as exc:
        log.warning("odds_ai: unusable answer for %s: %s", key, exc)
        _fail(key, exc.reason)
    except Exception as exc:  # noqa: BLE001 - surfaced to the reader as "error"
        log.warning("odds_ai: read failed for %s: %s", key, exc)
        _fail(key, "model")
    finally:
        _release(key)


def _fail(key: str, reason: str) -> None:
    try:
        _write_json(_path(key, ".error"), {"key": key, "reason": reason, "at": time.time()})
    except OSError:
        log.warning("odds_ai: could not record the failure for %s", key)


def _gemini(prompt: str) -> tuple[str, list[dict[str, str]], list[str]]:
    """One grounded call. Returns the text, the web sources it cited and the
    searches it ran. Retries once on an empty answer, which the grounded
    endpoint produces intermittently (see ``routes.api_history_research``);
    never falls back to an ungrounded call, because a forecast from training
    data alone is exactly the stale answer this feature must not give."""
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""),
                          http_options=genai_types.HttpOptions(timeout=_TIMEOUT_MS))
    config = genai_types.GenerateContentConfig(
        tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
        temperature=0.2,
        max_output_tokens=8192,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=2048),
    )
    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("odds_ai: Gemini attempt %d failed: %s", attempt, exc)
            continue
        text = resp.text or ""
        if not text.strip():
            log.warning("odds_ai: empty grounded answer (attempt %d)", attempt)
            continue
        sources: list[dict[str, str]] = []
        queries: list[str] = []
        for cand in resp.candidates or []:
            meta = getattr(cand, "grounding_metadata", None)
            for q in getattr(meta, "web_search_queries", None) or []:
                if isinstance(q, str) and q.strip() and q not in queries:
                    queries.append(q.strip())
            for chunk in getattr(meta, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                title = (getattr(web, "title", None) or "").strip() if web else ""
                uri = (getattr(web, "uri", None) or "").strip() if web else ""
                if title and all(s["title"] != title for s in sources):
                    sources.append({"title": title, "uri": uri if uri.startswith("https://") else ""})
        return text, sources, queries
    raise RuntimeError(f"no answer from {MODEL}") from last_error
