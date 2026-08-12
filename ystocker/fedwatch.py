"""
ystocker.fedwatch
~~~~~~~~~~~~~~~~~
CME FedWatch-style FOMC rate-move probabilities, derived from 30-Day Fed Funds
futures (CME product ZQ).

Why we compute this instead of reading it off cmegroup.com
----------------------------------------------------------
The published FedWatch tool is not fetchable. www.cmegroup.com sits behind an
Akamai bot wall that answers a plain client User-Agent with a silent
ReadTimeout and a spoofed browser UA with an explicit HTTP 403 ("This IP
address is blocked due to suspected web scraping activity"). There is no free
CME API for it either. So we rebuild the number from the same inputs CME uses:
the ZQ futures curve plus the FOMC calendar.

Inputs
------
1. ZQ futures settlement prices (Yahoo Finance, batched via yfinance).
   Symbol format: ``ZQ{month_code}{YY}.CBT`` — e.g. ``ZQZ26.CBT`` is the
   December 2026 contract. A ZQ contract settles to 100 minus the *arithmetic
   average daily EFFR over its delivery month*, so:

       implied average EFFR for month M = 100 - price(M)

   Verified 2026-08-12: ZQQ26 (Aug 2026) priced 96.37 -> 3.63%, which is
   exactly the EFFR print for that day. That identity is the calibration test
   for the symbol->month mapping; if a future yfinance change shifts the month
   letters, the front-month contract stops matching EFFR and
   ``_sanity_check_front_month()`` logs it.

2. The FOMC meeting calendar (federalreserve.gov, scraped; static fallback).
3. The current target range and EFFR (FRED: DFEDTARL / DFEDTARU / EFFR).

Method
------
Step 1 — back out the expected EFFR *after* each meeting.
  A new target range takes effect the day *after* the decision, so a delivery
  month containing a meeting averages the old and new rate pro rata:

      R_M = (d-1)/N * r_before + (N-d+1)/N * r_after

  where N is the days in month M and d is the first day the new rate applies.
  Solving for r_after is only well conditioned when enough days sit on the far
  side of the meeting. For a meeting late in the month (Oct 28 leaves 3 days)
  a 1bp error in R_M is amplified ~10x, so we prefer the *next* month's
  contract whenever no rate change lands inside it — that month prices a
  single constant rate and needs no algebra. This is the same
  next-clean-month-else-weighted-average rule CME documents.

Step 2 — turn the expected path into a probability distribution.
  Each meeting moves in 25bp increments, so an expected change of, say, +10.7bp
  is read as a 10.7/25 = 42.9% chance of a 25bp hike and 57.1% chance of a
  hold. Chaining that across meetings builds a probability tree: node = target
  range expressed as 25bp steps from today's, and each meeting branches every
  surviving node by its own expected change. The distribution therefore fans
  out over time (2 outcomes at the next meeting, 6+ a year out) and each
  meeting's outcomes always sum to 100%.

Baseline note: r_before for the first meeting is EFFR, not the target-range
midpoint. EFFR is what the futures actually reference, so using it keeps the
implied changes self-consistent; the small EFFR-vs-midpoint basis (0.5bp on
2026-08-12) is constant and cancels out of every difference.

Cache TTL: 4 hours — the futures curve reprices all session, unlike the weekly
H.4.1 data in fed.py.
"""
from __future__ import annotations

import calendar
import json
import logging
import math
import re
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).parent.parent / "cache" / "fedwatch_cache.json"
_CACHE_TTL = 4 * 60 * 60  # 4 hours

# Futures month codes: Jan..Dec
_MONTH_CODES = "FGHJKMNQUVXZ"

# Size of one Fed move, in percentage points.
_STEP = 0.25

# How many upcoming FOMC meetings to project.
_MAX_MEETINGS = 8

# Outcomes below this probability are pruned from the tail (percent). Set high
# enough that the surviving outcome count stays inside the 5-step colour ramps
# in fedwatch.html — a 0.2%-probability seventh range costs a whole new colour
# and tells the reader nothing.
_PRUNE_PCT = 0.5

# Minimum days on the far side of a meeting for the intra-month formula to be
# trustworthy. Below this we still compute, but flag it.
_MIN_TAIL_DAYS = 4

_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Plain client UA. A spoofed browser UA is silently blackholed by FRED's Akamai
# bot detection — see the FRED_USER_AGENT comment block in fed.py.
from ystocker.fed import FRED_USER_AGENT

_HEADERS = {
    "User-Agent": FRED_USER_AGENT,
    "Accept": "text/csv,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_SESSION = requests.Session()
_SESSION.trust_env = False  # system proxies cause silent timeouts
_SESSION.headers.update(_HEADERS)

# ---------------------------------------------------------------------------
# FOMC calendar
# ---------------------------------------------------------------------------

# Used when federalreserve.gov is unreachable. Decision day = the last day of
# each meeting (that is when the target range is announced). Keep in sync with
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm — 2027 dates
# are tentative until confirmed at the preceding meeting.
_FALLBACK_MEETINGS: list[str] = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}

# <div class="...fomc-meeting__month..."><strong>Apr/May</strong></div>
# <div class="...fomc-meeting__date...">30-1</div>
_MEETING_RE = re.compile(
    r'fomc-meeting__month[^>]*>\s*(?:<strong>)?(.*?)(?:</strong>)?\s*</div>'
    r'.*?fomc-meeting__date[^>]*>\s*(.*?)\s*</div>',
    re.S,
)
_YEAR_PANEL_RE = re.compile(r'<a id="\d+">(\d{4})\s+FOMC Meetings</a>')


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def fetch_fomc_meetings() -> list[date]:
    """Scrape scheduled FOMC decision dates from federalreserve.gov.

    Returns the *decision* day of each meeting (the second day of a two-day
    meeting), sorted ascending. Falls back to ``_FALLBACK_MEETINGS`` on any
    failure so the page still renders.

    Handles the two irregular shapes the Fed's own markup uses:
      * cross-month meetings — month "Apr/May" with date "30-1" means the
        decision is May 1, not April 1.
      * notation votes — "22 (notation vote)" is not a scheduled rate decision
        and is skipped (CME's FedWatch excludes them too).
    """
    try:
        resp = _SESSION.get(_FOMC_URL, timeout=25)
        resp.raise_for_status()
        html = resp.text

        meetings: set[date] = set()
        parts = _YEAR_PANEL_RE.split(html)
        # parts = [preamble, year, body, year, body, ...]
        for i in range(1, len(parts) - 1, 2):
            year = int(parts[i])
            body = parts[i + 1]
            for raw_month, raw_date in _MEETING_RE.findall(body):
                month_txt = _strip_tags(raw_month)
                date_txt = _strip_tags(raw_date)
                if "notation" in date_txt.lower():
                    continue

                # "17-18*" -> ["17", "18"];  "22" -> ["22"]
                days = re.findall(r"\d+", date_txt)
                if not days:
                    continue
                decision_day = int(days[-1])

                # "Apr/May" -> the decision falls in the second month
                month_tokens = [
                    t for t in re.split(r"[/\s\-–]+", month_txt.lower()) if t
                ]
                month_nums = [
                    _MONTH_NAMES[t] for t in month_tokens if t in _MONTH_NAMES
                ]
                if not month_nums:
                    continue
                month = month_nums[-1] if len(days) > 1 else month_nums[0]

                # A cross-month meeting rolls the year at Dec/Jan.
                yr = year
                if len(month_nums) > 1 and month_nums[-1] < month_nums[0]:
                    yr = year + 1
                try:
                    meetings.add(date(yr, month, decision_day))
                except ValueError:
                    log.warning("FedWatch: bad FOMC date %s %s", month_txt, date_txt)

        if len(meetings) < 8:
            raise ValueError(f"only parsed {len(meetings)} meetings")

        out = sorted(meetings)
        log.info("FedWatch: parsed %d FOMC meetings (%s … %s)",
                 len(out), out[0], out[-1])
        return out
    except Exception as exc:
        log.warning("FedWatch: FOMC calendar fetch failed (%s) — using fallback", exc)
        return [date.fromisoformat(d) for d in _FALLBACK_MEETINGS]


# ---------------------------------------------------------------------------
# Current policy rate
# ---------------------------------------------------------------------------

def _latest_fred_value(series_id: str) -> Optional[float]:
    """Return the most recent non-empty observation of a FRED series."""
    try:
        resp = _SESSION.get(_FRED_CSV.format(series=series_id), timeout=30)
        resp.raise_for_status()
        for line in reversed(resp.text.strip().splitlines()):
            parts = line.split(",")
            if len(parts) < 2:
                continue
            val = parts[1].strip()
            if val and val not in (".", "ND", "N/A"):
                try:
                    return float(val)
                except ValueError:
                    continue
    except Exception as exc:
        log.warning("FedWatch: FRED %s fetch failed: %s", series_id, exc)
    return None


def _fetch_current_rate() -> dict[str, Optional[float]]:
    """Current target range and effective fed funds rate."""
    lower = _latest_fred_value("DFEDTARL")
    upper = _latest_fred_value("DFEDTARU")
    effr = _latest_fred_value("EFFR")
    mid = round((lower + upper) / 2, 4) if lower is not None and upper is not None else None
    return {"lower": lower, "upper": upper, "mid": mid, "effr": effr}


# ---------------------------------------------------------------------------
# ZQ futures curve
# ---------------------------------------------------------------------------

def _zq_symbol(year: int, month: int) -> str:
    return f"ZQ{_MONTH_CODES[month - 1]}{year % 100:02d}.CBT"


def _month_add(year: int, month: int, n: int = 1) -> tuple[int, int]:
    idx = (year * 12 + month - 1) + n
    return idx // 12, idx % 12 + 1


def _fetch_zq_curve(months: list[tuple[int, int]]) -> dict[tuple[int, int], dict[str, Any]]:
    """Fetch ZQ settlement prices for *months* in one batched request.

    Returns {(year, month): {"symbol", "price", "implied", "date"}} for every
    contract Yahoo returned data for. Missing contracts are simply absent — the
    caller drops any meeting that depends on one.
    """
    import warnings

    symbols = {_zq_symbol(y, m): (y, m) for y, m in months}
    out: dict[tuple[int, int], dict[str, Any]] = {}
    if not symbols:
        return out

    try:
        import yfinance as yf

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(
                list(symbols),
                period="10d",
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=True,
            )
    except Exception as exc:
        log.error("FedWatch: yfinance download failed: %s", exc)
        return out

    if df is None or df.empty:
        log.error("FedWatch: yfinance returned no data for %d contracts", len(symbols))
        return out

    try:
        closes = df["Close"]
    except Exception:
        log.error("FedWatch: unexpected yfinance frame shape: %s", list(df.columns)[:5])
        return out

    for sym, ym in symbols.items():
        try:
            # A single-symbol download collapses the column level away.
            series = closes[sym] if sym in getattr(closes, "columns", []) else closes
            series = series.dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            # A ZQ price is 100 - rate, so anything outside ~[80, 100] is not a
            # fed funds future and must not silently become a -20% rate.
            if not (80.0 <= price <= 100.0):
                log.warning("FedWatch: implausible %s price %.4f — skipped", sym, price)
                continue
            out[ym] = {
                "symbol": sym,
                "price": round(price, 4),
                "implied": round(100.0 - price, 4),
                "date": str(series.index[-1].date()),
            }
        except Exception as exc:
            log.warning("FedWatch: could not read %s: %s", sym, exc)

    log.info("FedWatch: got %d/%d ZQ contracts", len(out), len(symbols))
    return out


def _sanity_check_front_month(
    curve: dict[tuple[int, int], dict[str, Any]],
    effr: Optional[float],
    today: date,
) -> None:
    """Warn if the front contract's implied rate has drifted from EFFR.

    The current delivery month has no meeting behind it most of the time, so
    100 - price should sit within a few bp of EFFR. A large gap means either the
    symbol->month mapping broke or we are mid-month with a meeting already
    priced — both worth a log line, neither worth failing the page over.
    """
    front = curve.get((today.year, today.month))
    if not front or effr is None:
        return
    drift_bp = abs(front["implied"] - effr) * 100
    if drift_bp > 12:
        log.warning(
            "FedWatch: front month %s implies %.3f%% but EFFR is %.3f%% "
            "(%.1fbp drift) — check the ZQ symbol mapping",
            front["symbol"], front["implied"], effr, drift_bp,
        )


# ---------------------------------------------------------------------------
# Step 1 — expected rate after each meeting
# ---------------------------------------------------------------------------

def _expected_path(
    meetings: list[date],
    curve: dict[tuple[int, int], dict[str, Any]],
    baseline: float,
    all_meetings: Optional[list[date]] = None,
) -> list[dict[str, Any]]:
    """Back out the expected EFFR after each meeting from the futures curve.

    Walks the meetings in order, carrying r_before forward. Stops at the first
    meeting whose contracts are missing, since every later meeting is
    conditional on it.

    *all_meetings* is the full FOMC calendar, used only to decide which months
    price a single constant rate. It must not be limited to the meetings being
    projected: the month after the last projected meeting is often dirtied by
    the *next* meeting, and judging cleanliness from the truncated list would
    silently treat that contract as a clean read of the post-meeting rate.
    """
    # Month in which each decision's new rate takes effect (the next day).
    effective = {m: m + timedelta(days=1) for m in meetings}
    # Months where the rate changes partway through, i.e. the contract does NOT
    # price a single constant rate. An effective date on the 1st is fine — the
    # whole month sits at the new rate.
    dirty_months: set[tuple[int, int]] = set()
    for m in (all_meetings or meetings):
        eff = m + timedelta(days=1)
        if eff.day > 1:
            dirty_months.add((eff.year, eff.month))

    path: list[dict[str, Any]] = []
    r_before = baseline

    for decision in meetings:
        eff = effective[decision]
        em = (eff.year, eff.month)
        note = ""

        if eff.day == 1:
            # Cross-month meeting (e.g. Apr 30 - May 1): the whole of the
            # effective month is at the new rate, no algebra needed.
            if em not in curve:
                break
            r_after = curve[em]["implied"]
            method = "next-month"
        else:
            nxt = _month_add(*em, 1)
            if nxt not in dirty_months and nxt in curve:
                # Preferred: the following month prices one constant rate.
                r_after = curve[nxt]["implied"]
                method = "next-month"
            elif em in curve:
                # Fall back to pro-rating within the meeting's own month.
                days = calendar.monthrange(em[0], em[1])[1]
                old_days = eff.day - 1
                new_days = days - old_days
                if new_days <= 0:
                    break
                r_after = (curve[em]["implied"] * days - old_days * r_before) / new_days
                method = "intra-month"
                if new_days < _MIN_TAIL_DAYS:
                    note = f"only {new_days} days after the meeting — noisy"
            else:
                break

        path.append({
            "decision": decision,
            "effective": eff,
            "r_before": round(r_before, 4),
            "r_after": round(r_after, 4),
            "change_bp": round((r_after - r_before) * 100, 1),
            "method": method,
            "note": note,
        })
        r_before = r_after

    return path


# ---------------------------------------------------------------------------
# Step 2 — probability tree
# ---------------------------------------------------------------------------

def _probability_tree(path: list[dict[str, Any]]) -> list[dict[int, float]]:
    """Distribution over target-range nodes after each meeting.

    A node is an integer count of 25bp steps away from today's target range.
    Each meeting's expected change is split between the two 25bp outcomes that
    bracket it, then applied to every surviving node — so uncertainty compounds
    and the distribution widens with the horizon.
    """
    dist: dict[int, float] = {0: 1.0}
    out: list[dict[int, float]] = []

    for leg in path:
        steps = (leg["r_after"] - leg["r_before"]) / _STEP
        low = math.floor(steps)
        frac = steps - low

        nxt: dict[int, float] = {}
        for node, prob in dist.items():
            if frac > 1e-9:
                nxt[node + low] = nxt.get(node + low, 0.0) + prob * (1 - frac)
                nxt[node + low + 1] = nxt.get(node + low + 1, 0.0) + prob * frac
            else:
                nxt[node + low] = nxt.get(node + low, 0.0) + prob

        # Prune the negligible tail, then renormalise so the bar still sums
        # to exactly 100%.
        kept = {n: p for n, p in nxt.items() if p * 100 >= _PRUNE_PCT}
        total = sum(kept.values())
        dist = {n: p / total for n, p in kept.items()} if total > 0 else nxt
        out.append(dist)

    return out


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

def _build_payload() -> dict[str, Any]:
    """Fetch every input and compute the full FedWatch payload."""
    today = date.today()
    current = _fetch_current_rate()

    baseline = current.get("effr") or current.get("mid")
    if baseline is None:
        log.error("FedWatch: no EFFR or target midpoint — cannot compute")
        return {"_ts": time.time(), "error": "policy rate unavailable", "meetings": []}

    lower = current.get("lower")
    upper = current.get("upper")
    if lower is None or upper is None:
        lower, upper = baseline - 0.125, baseline + 0.125

    # Upcoming meetings only. A decision made today is already history for the
    # futures curve, so require the decision to be in the future.
    scheduled = fetch_fomc_meetings()
    upcoming = [m for m in scheduled if m > today][:_MAX_MEETINGS]
    if not upcoming:
        log.error("FedWatch: no upcoming FOMC meetings found")
        return {"_ts": time.time(), "error": "no upcoming meetings", "meetings": []}

    # Contracts needed: the current month (for the EFFR sanity check) plus each
    # meeting's effective month and the month after it.
    months: set[tuple[int, int]] = {(today.year, today.month)}
    for m in upcoming:
        eff = m + timedelta(days=1)
        months.add((eff.year, eff.month))
        months.add(_month_add(eff.year, eff.month, 1))

    curve = _fetch_zq_curve(sorted(months))
    _sanity_check_front_month(curve, current.get("effr"), today)

    path = _expected_path(upcoming, curve, baseline, all_meetings=scheduled)
    if not path:
        log.error("FedWatch: futures curve too sparse to derive a path")
        return {"_ts": time.time(), "error": "futures data unavailable", "meetings": []}
    if len(path) < len(upcoming):
        log.info("FedWatch: projecting %d of %d meetings (curve ran out)",
                 len(path), len(upcoming))

    tree = _probability_tree(path)

    meetings_out: list[dict[str, Any]] = []
    all_nodes: set[int] = set()

    for leg, dist in zip(path, tree):
        all_nodes.update(dist)
        outcomes = []
        for node in sorted(dist):
            outcomes.append({
                "steps": node,
                "lower": round(lower + node * _STEP, 2),
                "upper": round(upper + node * _STEP, 2),
                "prob": round(dist[node] * 100, 1),
            })
        meetings_out.append({
            "date": leg["decision"].isoformat(),
            "label": leg["decision"].strftime("%b %Y"),
            "effective": leg["effective"].isoformat(),
            "implied_rate": leg["r_after"],
            "change_bp": leg["change_bp"],
            "method": leg["method"],
            "note": leg["note"],
            "outcomes": outcomes,
            # Direction totals, relative to *today's* range.
            "cut_prob": round(sum(p for n, p in dist.items() if n < 0) * 100, 1),
            "hold_prob": round(dist.get(0, 0.0) * 100, 1),
            "hike_prob": round(sum(p for n, p in dist.items() if n > 0) * 100, 1),
        })

    ranges = [
        {
            "steps": n,
            "lower": round(lower + n * _STEP, 2),
            "upper": round(upper + n * _STEP, 2),
            "label": f"{lower + n * _STEP:.2f}–{upper + n * _STEP:.2f}",
        }
        for n in sorted(all_nodes)
    ]

    as_of = max(
        (c["date"] for c in curve.values()),
        default=today.isoformat(),
    )

    return {
        "_ts": time.time(),
        "as_of": as_of,
        "current": {
            "lower": lower,
            "upper": upper,
            "mid": current.get("mid"),
            "effr": current.get("effr"),
            "label": f"{lower:.2f}–{upper:.2f}",
        },
        "meetings": meetings_out,
        "ranges": ranges,
        "contracts": [
            {"month": f"{y}-{m:02d}", **curve[(y, m)]}
            for (y, m) in sorted(curve)
        ],
    }


# ---------------------------------------------------------------------------
# Cache — mirrors fed.py: memory -> disk -> network, lock never held over IO
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_data: Optional[dict[str, Any]] = None
_cache_ts: Optional[float] = None

_warming = False
_warming_lock = threading.Lock()

_fetch_in_progress = threading.Event()


def _load_disk_cache() -> Optional[dict[str, Any]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        if time.time() - payload.get("_ts", 0) >= _CACHE_TTL:
            return None
        if not payload.get("meetings"):
            return None
        return payload
    except Exception as exc:
        log.warning("FedWatch: failed to read disk cache: %s", exc)
    return None


def _save_disk_cache(data: dict[str, Any]) -> None:
    """Atomically write cache to disk using temp file + rename."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_CACHE_FILE.parent, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(_CACHE_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:
        log.warning("FedWatch: failed to write disk cache: %s", exc)


def get_fedwatch_data(force: bool = False) -> dict[str, Any]:
    """Return cached FedWatch probabilities. memory -> disk -> network.

    The cache lock is never held across the network fetch, so a request that
    arrives mid-refresh gets stale data immediately instead of blocking a
    gunicorn worker.
    """
    global _cache_data, _cache_ts

    with _cache_lock:
        now = time.time()
        if not force and _cache_data and _cache_ts and (now - _cache_ts) < _CACHE_TTL:
            return _cache_data

    if not force:
        disk = _load_disk_cache()
        if disk:
            with _cache_lock:
                _cache_data = disk
                _cache_ts = disk.get("_ts", time.time())
            return disk

    if not force and _fetch_in_progress.is_set():
        log.info("FedWatch: fetch in progress — returning stale/warming payload")
        with _cache_lock:
            if _cache_data:
                return _cache_data
        return {"_warming": True, "_ts": None, "meetings": []}

    _fetch_in_progress.set()
    try:
        log.info("FedWatch: computing fresh probabilities")
        fresh = _build_payload()
        if fresh.get("meetings"):
            with _cache_lock:
                _cache_data = fresh
                _cache_ts = fresh["_ts"]
            _save_disk_cache(fresh)
        else:
            # Don't cache a failure — serve stale data if we have any.
            with _cache_lock:
                if _cache_data:
                    log.warning("FedWatch: build failed — keeping previous cache")
                    return _cache_data
        return fresh
    finally:
        _fetch_in_progress.clear()


def get_cache_ts() -> Optional[float]:
    with _cache_lock:
        if _cache_ts:
            return _cache_ts
    disk = _load_disk_cache()
    return disk.get("_ts") if disk else None


def is_cache_fresh() -> bool:
    """True when we hold a non-stale payload that actually has meetings."""
    with _cache_lock:
        data = _cache_data if _cache_data else _load_disk_cache()
    if not data:
        return False
    ts = data.get("_ts")
    if not ts or (time.time() - ts) >= _CACHE_TTL:
        return False
    return bool(data.get("meetings"))


def is_warming() -> bool:
    with _warming_lock:
        return _warming


def refresh_cache() -> None:
    """Force a refresh, ignoring the TTL."""
    global _warming
    with _warming_lock:
        _warming = True
    try:
        get_fedwatch_data(force=True)
    finally:
        with _warming_lock:
            _warming = False


def start_background_thread() -> None:
    """Warm the cache on startup, then refresh every TTL.

    Under gunicorn --preload this runs only in the master (see CLAUDE.md), so
    forked workers inherit a warm snapshot and never pay for a cold fetch.
    """

    def _loop() -> None:
        try:
            disk = _load_disk_cache()
            if disk:
                global _cache_data, _cache_ts
                with _cache_lock:
                    _cache_data = disk
                    _cache_ts = disk.get("_ts", time.time())
                log.info("FedWatch background: memory cache warmed from disk (%d meetings)",
                         len(disk.get("meetings", [])))
            else:
                log.info("FedWatch background: no disk cache — computing now")
                refresh_cache()
        except Exception as exc:
            log.warning("FedWatch background: startup warm failed: %s", exc)

        while True:
            time.sleep(_CACHE_TTL)
            try:
                log.info("FedWatch background: TTL elapsed — refreshing")
                refresh_cache()
            except Exception as exc:
                log.warning("FedWatch background: refresh failed: %s", exc)

    t = threading.Thread(target=_loop, name="fedwatch-background-refresh", daemon=True)
    t.start()
    log.info("FedWatch: background refresh thread started")
