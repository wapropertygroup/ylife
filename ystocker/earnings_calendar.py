"""
ystocker.earnings_calendar
~~~~~~~~~~~~~~~~~~~~~~~~~~
Who reports when, across the US market: the data behind /earnings.

``earnings.py`` answers "when does each of the ~308 companies this site follows
report next", from fields Yahoo already sends, for /markets' card. This answers
the question a calendar is for: everything reporting in a given week, the
consensus each has to beat, and -- once it has reported -- whether it did.

Source: Nasdaq's public calendar API, one keyless request per day::

    https://api.nasdaq.com/api/calendar/earnings?date=2026-10-29

Measured from the box on 2026-10-04: 307 companies that day, 15 on 2026-10-13
(the banks), 475 on 2026-08-05, none on a weekend. It answers a plain
``requests`` client only when the request carries a browser User-Agent -- with
none the connection hangs until the read timeout -- and even then slowly, 2-3 s
a request, where curl_cffi with Chrome's TLS fingerprint (already installed for
yfinance) is answered in 0.1 s. So :func:`_http` prefers that, and
:data:`_HEADERS` is not decoration.

The two shapes of a day
-----------------------
A day not yet reached carries the consensus EPS estimate (Zacks, via Nasdaq),
the number of estimates, last year's report date and EPS, and the time of day:
before the open, after the close, or not supplied. Once the day has passed, the
*same request* returns the reported EPS and the surprise against the consensus,
and drops the time of day and last year's figures. So a past day is not a
superset of the same day seen in advance: :func:`parse_day` takes whichever
fields are present rather than assuming one shape.

Money arrives as text: ``"$5.88"``, ``"($0.05)"`` for a loss, ``"$0"`` for a
real zero, ``""`` or ``"N/A"`` for nothing. A parenthesised figure is negative
and a blank is absent. Reading ``"($0.05)"`` as 0.05 turns a loss into a profit
on exactly the row where the sign is the story, and reading ``""`` as 0 invents
a breakeven forecast for a company nobody covers.

Freshness
---------
One file per day in ``cache/earnings_calendar/``. A future day is re-read every
six hours, since dates move and companies are added; the three days up to and
including today hourly, since that is when reported figures and surprises land;
an older day is final once it has been read three days after the fact.

The request path never fetches. A missing or stale day in the week asked for is
queued for one background worker, paced a second apart, and the page polls --
the pattern every cold path here follows. A day whose fetch just failed is not
queued again for ten minutes, so a polling page cannot turn one failure into a
request every four seconds. And the weeks a request can ask for are bounded
(:data:`MAX_WEEKS_BACK`, :data:`MAX_WEEKS_AHEAD`): a link walking ``?week=``
back to 1990 is clamped, not fetched.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

URL = "https://api.nasdaq.com/api/calendar/earnings"
#: Breaker identity in fetchguard, so a Nasdaq cool-down stalls nothing else.
PROVIDER = "nasdaq"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "earnings_calendar"
SOURCE = "Nasdaq"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"),
}

FUTURE_TTL_SECONDS = 6 * 3600
RECENT_TTL_SECONDS = 3600
#: Today and the days just before it: when reported figures arrive.
RECENT_DAYS = 3
#: A past day read at least this many days after the fact is final.
FINAL_AFTER_DAYS = 3
MAX_WEEKS_BACK = 8
MAX_WEEKS_AHEAD = 8
FETCH_SPACING_SECONDS = 1.0
FAILURE_PAUSE_SECONDS = 10 * 60
#: The background warm keeps last week, this week and three ahead fresh.
WARM_WEEKS_BACK = 1
WARM_WEEKS_AHEAD = 3
WARM_INTERVAL_SECONDS = 30 * 60
#: An answer this much smaller than the copy it would replace is held suspect.
_MIN_ROWS_TO_DISTRUST_EMPTY = 5

_BLANK = frozenset({"", "N/A", "NA", "--", "-"})
_WHEN = {"time-pre-market": "bmo", "time-after-hours": "amc"}


# ── Parsing (pure) ──────────────────────────────────────────────────────────

def _money(text: Any) -> Optional[float]:
    """``"$5.88"`` -> 5.88, ``"($0.05)"`` -> -0.05, ``"$0"`` -> 0.0, blank -> None.

    Also reads the bare numbers the same payload uses elsewhere: a market cap
    (``"$885,654,490,000"``), a surprise (``"-83.72"``), a count (``"6"``).
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if s.upper() in _BLANK:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    if s.startswith("-"):
        neg, s = True, s[1:].strip()
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if neg else value


def _count(text: Any) -> Optional[int]:
    value = _money(text)
    return int(value) if value is not None and value >= 0 else None


def _mdy(text: Any) -> Optional[str]:
    """``"10/14/2025"`` -> ``"2025-10-14"``; anything else -> None."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", str(text or ""))
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def growth(estimate: Optional[float], last_year: Optional[float]) -> Optional[float]:
    """The year-on-year EPS growth the consensus implies, in percent.

    ``None`` when last year was a loss or zero: growth from a negative base is
    not a percentage anyone can read, and a figure like +200% for a company
    going from -$0.05 to +$0.05 would sort to the top of a column it has no
    business leading.
    """
    if estimate is None or last_year is None or last_year <= 0:
        return None
    return round((estimate - last_year) / last_year * 100.0, 1)


def beat(reported: Optional[float], estimate: Optional[float]) -> Optional[int]:
    """+1 beat, -1 miss, 0 in line (within half a cent), None when unknowable."""
    if reported is None or estimate is None:
        return None
    diff = reported - estimate
    if abs(diff) < 0.005:
        return 0
    return 1 if diff > 0 else -1


def _symbol(raw: Any) -> str:
    """Nasdaq's ``GEF.B`` as Yahoo spells it, ``GEF-B`` -- which is what /history
    resolves. The share-class rule is portfolio_csv's, narrow on purpose."""
    from ystocker.portfolio_csv import normalise_symbol

    return normalise_symbol(raw)


def parse_day(payload: Any) -> list[dict[str, Any]]:
    """One day's rows from Nasdaq's JSON, largest company first.

    Compact keys, because a peak week is two thousand rows on the wire:
    ``t`` ticker, ``n`` name, ``when`` "bmo"/"amc"/"", ``cap`` market cap,
    ``fq`` fiscal quarter, ``est`` consensus EPS, ``ests`` how many estimates,
    ``ly`` last year's EPS, ``ly_date`` its report date, ``eps`` reported EPS,
    ``surp`` Nasdaq's surprise %, ``g`` implied growth % and ``beat``.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        sym = _symbol(raw.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        est = _money(raw.get("epsForecast"))
        last = _money(raw.get("lastYearEPS"))
        reported = _money(raw.get("eps"))
        out.append({
            "t": sym,
            "n": str(raw.get("name") or "").strip() or sym,
            "when": _WHEN.get(str(raw.get("time") or "").strip(), ""),
            "cap": _money(raw.get("marketCap")),
            "fq": str(raw.get("fiscalQuarterEnding") or "").strip(),
            "est": est,
            "ests": _count(raw.get("noOfEsts")),
            "ly": last,
            "ly_date": _mdy(raw.get("lastYearRptDt")),
            "eps": reported,
            "surp": _money(raw.get("surprise")),
            "g": growth(est, last),
            "beat": beat(reported, est),
        })
    out.sort(key=lambda r: -(r["cap"] or 0.0))
    return out


# ── Dates (pure) ────────────────────────────────────────────────────────────

def today_et(now: Optional[float] = None) -> dt.date:
    """The market's date. A calendar keyed to UTC turns over at 8pm in New
    York, so an after-the-close report would move to "yesterday" mid-evening."""
    from zoneinfo import ZoneInfo

    stamp = time.time() if now is None else now
    return dt.datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).date()


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def default_week(today: dt.date) -> dt.date:
    """The week a reader means by "this week": the current one -- except at the
    weekend, when the week just ended has nothing left to report and the coming
    one is the question."""
    if today.weekday() >= 5:
        return monday_of(today) + dt.timedelta(days=7)
    return monday_of(today)


def week_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    base = monday_of(today)
    return (base - dt.timedelta(weeks=MAX_WEEKS_BACK),
            base + dt.timedelta(weeks=MAX_WEEKS_AHEAD))


def clamp_week(monday: dt.date, today: dt.date) -> tuple[dt.date, bool]:
    lo, hi = week_bounds(today)
    if monday < lo:
        return lo, True
    if monday > hi:
        return hi, True
    return monday, False


def weekdays(monday: dt.date) -> list[dt.date]:
    return [monday + dt.timedelta(days=i) for i in range(5)]


def is_stale(day: dt.date, fetched_at: float, now: float, today: dt.date) -> bool:
    """Whether a cached day is due to be read again. See the module docstring."""
    age = now - fetched_at
    if day > today:
        return age > FUTURE_TTL_SECONDS
    if (today - day).days < RECENT_DAYS:
        return age > RECENT_TTL_SECONDS
    if (today_et(fetched_at) - day).days >= FINAL_AFTER_DAYS:
        return False
    return age > RECENT_TTL_SECONDS


def warm_window(today: dt.date) -> list[dt.date]:
    start = monday_of(today) - dt.timedelta(weeks=WARM_WEEKS_BACK)
    span = (WARM_WEEKS_BACK + WARM_WEEKS_AHEAD + 1) * 7
    days = (start + dt.timedelta(days=i) for i in range(span))
    return [d for d in days if d.weekday() < 5]


# ── Cache ───────────────────────────────────────────────────────────────────

_mem: dict[str, tuple[float, dict[str, Any]]] = {}
_mem_lock = threading.Lock()


def _path(day: dt.date) -> Path:
    return CACHE_DIR / f"{day.isoformat()}.json"


def peek_day(day: dt.date) -> Optional[dict[str, Any]]:
    """The cached day -- ``{"date", "_ts", "rows"}`` -- or ``None``. Never fetches.

    Re-read from disk when the file's mtime moves: under ``--preload`` the warm
    thread lives in the master, and a worker must see what it wrote.
    """
    path = _path(day)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = day.isoformat()
    with _mem_lock:
        hit = _mem.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return None
    with _mem_lock:
        _mem[key] = (mtime, payload)
    return payload


def _write(day: dt.date, rows: list[dict[str, Any]], ts: float) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"date": day.isoformat(), "_ts": ts, "rows": rows}
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, _path(day))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_session: Any = None


def _http() -> Any:
    """One session for the process: curl_cffi impersonating Chrome where it
    imports, else plain requests. Only the one worker thread uses it."""
    global _session
    if _session is None:
        try:
            from curl_cffi import requests as curl_requests

            _session = curl_requests.Session(impersonate="chrome")
        except Exception:  # noqa: BLE001 - any import trouble means plain requests
            import requests

            _session = requests.Session()
    return _session


def fetch_day(day: dt.date) -> list[dict[str, Any]]:
    """One request to Nasdaq for one day. Raises on anything but an answer."""
    from ystocker import fetchguard

    resp = fetchguard.request(PROVIDER, URL, session=_http(), params={"date": day.isoformat()},
                              headers=_HEADERS, timeout=20)
    payload = resp.json()
    status = (payload or {}).get("status") if isinstance(payload, dict) else None
    code = (status or {}).get("rCode") if isinstance(status, dict) else None
    if code not in (None, 200):
        raise ValueError(f"Nasdaq answered rCode={code}")
    return parse_day(payload)


_failed: dict[str, float] = {}


def recently_failed(day: dt.date, now: Optional[float] = None) -> bool:
    stamp = _failed.get(day.isoformat())
    return stamp is not None and (time.time() if now is None else now) - stamp < FAILURE_PAUSE_SECONDS


def refresh_day(day: dt.date) -> bool:
    """Fetch and store one day. A failure keeps the last copy, however old.

    So does an empty answer for a day that had companies: Nasdaq's "no rows" is
    indistinguishable from a glitch, and a calendar that loses a busy day reads
    as a quiet one, which is the one wrong answer it can give.
    """
    try:
        rows = fetch_day(day)
    except Exception as exc:  # noqa: BLE001 - any failure keeps the last copy
        _failed[day.isoformat()] = time.time()
        log.warning("earnings_calendar: %s not refreshed: %s", day, exc)
        return False
    previous = peek_day(day)
    if not rows and previous and len(previous.get("rows") or []) >= _MIN_ROWS_TO_DISTRUST_EMPTY:
        _failed[day.isoformat()] = time.time()
        log.warning("earnings_calendar: %s came back empty; keeping %d rows",
                    day, len(previous["rows"]))
        return False
    _write(day, rows, time.time())
    _failed.pop(day.isoformat(), None)
    log.info("earnings_calendar: %s -> %d companies", day, len(rows))
    return True


# ── The worker ──────────────────────────────────────────────────────────────

_queue: list[dt.date] = []
_queue_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


def queued(day: dt.date) -> bool:
    with _queue_lock:
        return day in _queue or day == _current


_current: Optional[dt.date] = None


def kick(days: Iterable[dt.date]) -> int:
    """Queue days for the one background worker, starting it if idle.

    Returns how many were newly queued. The worker clears ``_worker`` under the
    same lock before it exits, so a day queued as it finishes is never stranded.
    """
    global _worker
    added = 0
    with _queue_lock:
        for day in days:
            if day not in _queue and day != _current:
                _queue.append(day)
                added += 1
        if _queue and _worker is None:
            _worker = threading.Thread(target=_drain, name="earnings-calendar", daemon=True)
            _worker.start()
    return added


def _drain() -> None:
    global _worker, _current
    while True:
        with _queue_lock:
            if not _queue:
                _worker = None
                _current = None
                return
            day = _queue.pop(0)
            _current = day
        try:
            refresh_day(day)
        finally:
            time.sleep(FETCH_SPACING_SECONDS)


# ── Views ───────────────────────────────────────────────────────────────────

def week_view(monday: dt.date, *, now: Optional[float] = None,
              start_fetches: bool = True) -> dict[str, Any]:
    """Monday to Friday of one week, from the cache, queueing what is missing.

    Each day says ``ok`` (rows are the cached copy, possibly being refreshed),
    ``pending`` (not cached yet; queued) or ``failed`` (not cached, and the last
    attempt failed within :data:`FAILURE_PAUSE_SECONDS`).
    """
    stamp = time.time() if now is None else now
    today = today_et(stamp)
    days: list[dict[str, Any]] = []
    todo: list[dt.date] = []
    for day in weekdays(monday):
        got = peek_day(day)
        stale = got is None or is_stale(day, float(got.get("_ts") or 0), stamp, today)
        if stale and not recently_failed(day, stamp):
            todo.append(day)
        if got is not None:
            status = "ok"
        elif recently_failed(day, stamp):
            status = "failed"
        else:
            status = "pending"
        days.append({
            "date": day.isoformat(),
            "status": status,
            "as_of": got.get("_ts") if got else None,
            "rows": got["rows"] if got else [],
        })
    if todo and start_fetches:
        kick(todo)
    return {"monday": monday.isoformat(), "today": today.isoformat(), "days": days}


def start_background_thread() -> None:
    """Keep last week, this week and the next three fresh, every half hour.

    About twenty requests on a cold start and a handful an hour after that.
    Waits a minute after boot so a deploy does not open with a burst.
    """
    def _loop() -> None:
        time.sleep(60)
        while True:
            try:
                now = time.time()
                today = today_et(now)
                todo = []
                for day in warm_window(today):
                    got = peek_day(day)
                    if got is None or is_stale(day, float(got.get("_ts") or 0), now, today):
                        if not recently_failed(day, now):
                            todo.append(day)
                if todo:
                    kick(todo)
            except Exception:  # noqa: BLE001 - the loop must outlive any one pass
                log.exception("earnings_calendar: warm pass failed")
            time.sleep(WARM_INTERVAL_SECONDS)

    threading.Thread(target=_loop, name="earnings-calendar-warm", daemon=True).start()
    log.info("earnings_calendar: warm thread started (cache: %s)", CACHE_DIR)
