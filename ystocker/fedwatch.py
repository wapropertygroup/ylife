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
import os
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
# Payload schema version. Bump whenever a key is added, removed or renamed.
# A TTL alone does not protect against a shape change: after a deploy that adds
# a field, an existing cache still looks fresh, so the API happily serves a
# payload the new page cannot read and charts render empty with no explanation.
# Same idea as _YIELD_CURVE_CACHE_VER in routes.py.
_CACHE_VER = "v2"

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
# Current policy rate, and the target-range history
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fred_series(series_id: str) -> list[tuple[str, float]]:
    """Every non-empty observation of a FRED series, as (ISO date, value).

    Returns ``[]`` on any failure — the caller decides whether that is fatal.
    It is for the current target range; it is not for the history chart, which
    is decoration on a page whose point is the forward curve.
    """
    try:
        resp = _SESSION.get(_FRED_CSV.format(series=series_id), timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("FedWatch: FRED %s fetch failed: %s", series_id, exc)
        return []

    out: list[tuple[str, float]] = []
    # FRED CSV: "observation_date,<SERIES_ID>" then one row per observation.
    for line in resp.text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        stamp, raw = parts[0].strip(), parts[1].strip()
        if not raw or raw in (".", "ND", "N/A"):
            continue
        if not _ISO_DATE_RE.match(stamp):
            continue
        try:
            out.append((stamp, float(raw)))
        except ValueError:
            continue
    return out


def _latest_fred_value(series_id: str) -> Optional[float]:
    """Return the most recent non-empty observation of a FRED series."""
    series = _fred_series(series_id)
    return series[-1][1] if series else None


def _rate_change_points(
    lower: list[tuple[str, float]],
    upper: list[tuple[str, float]],
    legacy: Optional[list[tuple[str, float]]] = None,
) -> list[dict[str, Any]]:
    """Compress the daily target series into one row per *change*.

    DFEDTARL/DFEDTARU are daily and run to ~6,500 rows each since 2008, so
    shipping them raw would add a megabyte of duplicated numbers to a payload
    read on every page load. A policy rate is a step function: every row between
    two moves is redundant, and Chart.js draws the flat segments itself given
    ``stepped: true``. ~40 change points replace ~13,000 observations.

    *legacy* is DFEDTAR, the single target in force before the Fed moved to a
    range in December 2008. Those rows carry ``lower == upper``, which draws as a
    zero-width band and takes the chart back to 1982 instead of 2008. The range
    series wins wherever the two overlap, since DFEDTAR lingers as a
    discontinued series rather than stopping cleanly.
    """
    merged: dict[str, tuple[float, float]] = {}
    for stamp, val in (legacy or []):
        merged[stamp] = (val, val)
    lows = dict(lower)
    for stamp, hi in upper:
        lo = lows.get(stamp)
        if lo is not None:
            merged[stamp] = (lo, hi)

    points: list[dict[str, Any]] = []
    prev: Optional[tuple[float, float]] = None
    for stamp in sorted(merged):
        lo, hi = merged[stamp]
        rounded = (round(lo, 4), round(hi, 4))
        if rounded == prev:
            continue
        points.append({"date": stamp, "lower": rounded[0], "upper": rounded[1]})
        prev = rounded

    # Anchor the final step at the last observation, or the flat segment since
    # the most recent move has zero length and the last change vanishes.
    if points and merged:
        last_stamp = max(merged)
        if last_stamp != points[-1]["date"]:
            lo, hi = merged[last_stamp]
            points.append({"date": last_stamp,
                           "lower": round(lo, 4), "upper": round(hi, 4)})

    log.info("FedWatch: target-range history compressed to %d change points (%s … %s)",
             len(points),
             points[0]["date"] if points else "—",
             points[-1]["date"] if points else "—")
    return points


def _fetch_current_rate() -> dict[str, Any]:
    """Current target range and EFFR, plus the target-range step history.

    The history is free: ``_latest_fred_value`` was already downloading the whole
    DFEDTARL/DFEDTARU CSV and keeping the last row, so this is the same two
    responses parsed rather than discarded. Only DFEDTAR, for the pre-2008 depth,
    is an additional call — and it is optional, so a failure costs earlier
    history and nothing else.
    """
    lower_series = _fred_series("DFEDTARL")
    upper_series = _fred_series("DFEDTARU")

    lower = lower_series[-1][1] if lower_series else None
    upper = upper_series[-1][1] if upper_series else None
    effr = _latest_fred_value("EFFR")
    mid = round((lower + upper) / 2, 4) if lower is not None and upper is not None else None

    history: list[dict[str, Any]] = []
    try:
        history = _rate_change_points(
            lower_series, upper_series, legacy=_fred_series("DFEDTAR"))
    except Exception as exc:  # noqa: BLE001 - the forward curve is the page
        log.warning("FedWatch: target-range history unavailable: %s", exc)

    return {"lower": lower, "upper": upper, "mid": mid, "effr": effr,
            "history": history}


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
        "_ver": _CACHE_VER,
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
        # Past target-range steps, oldest first. Recomputable from FRED at any
        # time, so this is cache and deliberately *not* the DynamoDB series
        # below — snapshotting it daily would start an empty chart today and take
        # years to rebuild what one GET already returns in full.
        "rate_history": current.get("history") or [],
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


def _load_disk_cache(ignore_ttl: bool = False) -> Optional[dict[str, Any]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        # The version check applies even to peek(): a schema mismatch means the
        # keys are wrong, not merely old, and a consumer would misread it.
        if payload.get("_ver") != _CACHE_VER:
            log.info("%s: cache schema %s != %s — rebuilding",
                     __name__, payload.get("_ver"), _CACHE_VER)
            return None
        if not ignore_ttl and time.time() - payload.get("_ts", 0) >= _CACHE_TTL:
            return None
        if not payload.get("meetings"):
            return None
        return payload
    except Exception as exc:
        log.warning("FedWatch: failed to read disk cache: %s", exc)
    return None


def peek() -> Optional[dict[str, Any]]:
    """Return an already-available FedWatch payload, or None. Never fetches.

    Mirrors ``breadth.peek()``. The TTL here is four hours, the shortest of the
    cached modules, so a consumer gated on freshness loses this section often;
    an implied policy path from this morning still answers "what does the market
    expect in December?". Callers can date it from the payload's ``as_of``.
    """
    with _cache_lock:
        if _cache_data:
            return _cache_data
    try:
        return _load_disk_cache(ignore_ttl=True)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("FedWatch: peek failed to read disk cache: %s", exc)
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


# ---------------------------------------------------------------------------
# Observed series — what the market expected, on each past day
# ---------------------------------------------------------------------------
# This is the one thing here that cannot be recomputed. The target-range history
# above is free from FRED forever, but nothing upstream will sell you yesterday's
# ZQ curve, so "in September, what odds did the market give a December cut?" is
# answerable only if we wrote it down at the time. Same reasoning as
# ystocker-cta-history: a lost row is lost permanently.
#
# Keeping it only in cache/ was the flaw that reset the SPY/QQQ valuation chart
# to a single point when the EC2 instance was replaced, so DynamoDB is the
# system of record and the disk copy is a fallback for writes made while
# DynamoDB was briefly unreachable.
#
# The trio below is a near-copy of the one in cta.py and valuation.py. That is
# three copies and wants extracting into a shared HistorySeries helper; done
# deliberately as a copy here to keep this change inside one module.
#
# The table is NOT in deploy/cloudformation.yaml, matching the other four:
# CloudFormation cannot adopt a live table without an import operation, so
# adding it would break the next --full deploy rather than converge it. IAM
# already covers the name via the table/ystocker-* wildcard. Create by hand:
#
#   aws dynamodb create-table --table-name ystocker-fedwatch-history \
#     --region us-west-2 --billing-mode PAY_PER_REQUEST \
#     --attribute-definitions AttributeName=date,AttributeType=S \
#     --key-schema AttributeName=date,KeyType=HASH

_HIST_TABLE_NAME = "ystocker-fedwatch-history"
_HIST_PATH = _CACHE_FILE.parent / "fedwatch_history.json"

#: Scalar columns. The per-meeting detail cannot be one attribute per meeting —
#: the column set would change every time a meeting rolls off — so it travels as
#: a JSON string under "meetings". Nothing ever queries by meeting; the chart
#: always reads the whole series, so queryability buys nothing here.
_HIST_NUMERIC = ("base_lower", "base_upper", "effr")

_hist_table = None
_hist_unavail_until = 0.0
_HIST_LOCK = threading.Lock()

# Memo for history(). Short, because the only writer is this process's own
# refresh and it invalidates explicitly — the TTL is a backstop for the other
# gunicorn worker, which writes nothing but must not serve a stale series
# indefinitely either.
_HIST_MEMO_TTL = 300
_hist_memo: Optional[list[dict[str, Any]]] = None
_hist_memo_ts = 0.0
_HIST_MEMO_LOCK = threading.Lock()


def _get_hist_table():
    """DynamoDB table for the expectations series, or None when unavailable.

    Absence is not an error: local dev has no AWS credentials and the disk copy
    still works. The 5-minute backoff stops every refresh paying a connection
    timeout when the table genuinely is not there.
    """
    global _hist_table, _hist_unavail_until
    if _hist_table is not None:
        return _hist_table
    if time.time() < _hist_unavail_until:
        return None
    with _HIST_LOCK:
        if _hist_table is not None:
            return _hist_table
        if time.time() < _hist_unavail_until:
            return None
        try:
            import boto3

            ddb = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(_HIST_TABLE_NAME)
            tbl.load()
            _hist_table = tbl
            log.info("FedWatch: DynamoDB history table connected: %s", _HIST_TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - degrade to disk-only
            log.warning("FedWatch: DynamoDB history unavailable: %s", exc)
            _hist_table = None
            _hist_unavail_until = time.time() + 300
        return _hist_table


def _valid_date(value: object) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text if _ISO_DATE_RE.match(text) else None


def _number(value: object) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _row_from_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalise one stored row, or None if it is not an observation.

    Rejecting a non-ISO ``date`` is what lets a sentinel key share the table
    later without the series readers picking it up, as cta.py does with
    ``_latest_report``.
    """
    stamp = _valid_date(item.get("date"))
    if not stamp:
        return None
    row: dict[str, Any] = {"date": stamp}
    for key in _HIST_NUMERIC:
        val = _number(item.get(key))
        if val is not None:
            row[key] = val

    raw = item.get("meetings")
    meetings: list[dict[str, Any]] = []
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            log.warning("FedWatch: unreadable meetings blob for %s", stamp)
            raw = None
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        when = _valid_date(entry.get("d"))
        rate = _number(entry.get("r"))
        if not when or rate is None:
            continue
        out: dict[str, Any] = {"date": when, "implied_rate": rate}
        for src, dst in (("c", "cut_prob"), ("h", "hold_prob"), ("k", "hike_prob")):
            val = _number(entry.get(src))
            if val is not None:
                out[dst] = val
        meetings.append(out)
    if meetings:
        row["meetings"] = meetings

    return row if len(row) > 1 else None


def _hist_load_ddb() -> list[dict[str, Any]]:
    table = _get_hist_table()
    if table is None:
        return []
    try:
        rows: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                row = _row_from_item(item)
                if row:
                    rows.append(row)
            # A scan is paginated; without this the series silently stops at the
            # first page once there is more than 1 MB of history. Measured: a
            # full 8-meeting row is ~890 bytes, so the first page fills after
            # ~1,120 rows — about 4.5 years of trading days. Far enough out to be
            # invisible in testing and certain to arrive eventually.
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("FedWatch: history load failed: %s", exc)
        return []


def _item_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Canonical stored form of one row — the exact inverse of _row_from_item.

    DynamoDB and the disk file deliberately hold the *same* shape. An earlier cut
    of this had the disk copy storing the expanded row while DynamoDB stored the
    compact one, and since _row_from_item only understands the compact keys,
    every row read back from disk silently lost its meetings and the fallback
    preserved nothing but the baseline. One shape, one parser, symmetric by
    construction.
    """
    item: dict[str, Any] = {"date": row["date"]}
    for key in _HIST_NUMERIC:
        if row.get(key) is not None:
            item[key] = str(row[key])            # DynamoDB rejects float
    if row.get("meetings"):
        item["meetings"] = json.dumps([
            {"d": m["date"], "r": m["implied_rate"],
             "c": m.get("cut_prob"), "h": m.get("hold_prob"),
             "k": m.get("hike_prob")}
            for m in row["meetings"]
        ], separators=(",", ":"), allow_nan=False)
    return item


def _hist_save_ddb(row: dict[str, Any]) -> None:
    table = _get_hist_table()
    if table is None or not row.get("date"):
        return
    try:
        table.put_item(Item=_item_from_row(row))
    except Exception as exc:  # noqa: BLE001
        log.warning("FedWatch: history save failed for %s: %s", row.get("date"), exc)


def _hist_load_disk() -> list[dict[str, Any]]:
    try:
        payload = json.loads(_HIST_PATH.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        out = []
        for raw in rows or []:
            row = _row_from_item(raw) if isinstance(raw, dict) else None
            if row:
                out.append(row)
        return out
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("FedWatch: unreadable history file (%s)", exc)
        return []


def _hist_save_disk(rows: list[dict[str, Any]]) -> None:
    try:
        _HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_HIST_PATH.parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                json.dump({"rows": [_item_from_row(r) for r in rows]},
                          fh, allow_nan=False)
            Path(tmp).replace(_HIST_PATH)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:  # noqa: BLE001
        log.warning("FedWatch: could not write history file: %s", exc)


def history(limit: Optional[int] = None) -> list[dict[str, Any]]:
    """The expectations series, oldest first. Always reads both stores.

    DynamoDB and disk are unioned by date rather than one being preferred: disk
    can hold a row written while DynamoDB was briefly unreachable, and DynamoDB
    holds everything that predates the current instance. Later writes for a date
    win, which is what makes a same-day re-record an update rather than a
    duplicate.

    Deliberately *not* memoised — it is a pure function of the two stores, which
    is what makes it testable and what callers reading right after a write
    expect. Request paths want :func:`history_cached` instead.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in _hist_load_ddb() + _hist_load_disk():
        merged[row["date"]] = {**merged.get(row["date"], {}), **row}
    rows = sorted(merged.values(), key=lambda r: r["date"])
    return rows[-limit:] if limit else rows


def history_cached(limit: Optional[int] = None) -> list[dict[str, Any]]:
    """:func:`history`, memoised for _HIST_MEMO_TTL. For request paths.

    The reason is billing, not latency. ``/api/fedwatch/history`` is public and
    unauthenticated, and every uncached call is a *full table scan* — on
    PAY_PER_REQUEST that is billed by the volume scanned, so anything hitting the
    endpoint in a loop multiplies read cost without limit as the series grows.
    The memo bounds that to one scan per TTL per worker. (It also happens to save
    ~80ms a page load, which is the part nobody would have noticed.)

    Writes go through ``record_snapshot``, which clears the memo, so a fresh row
    is never hidden behind the TTL. The TTL is the backstop for the *other*
    gunicorn worker, which writes nothing and would otherwise never learn.

    *limit* slices the memoised list rather than keying it, so a mix of limits
    cannot multiply the scans.
    """
    global _hist_memo, _hist_memo_ts

    with _HIST_MEMO_LOCK:
        if _hist_memo is not None and (time.time() - _hist_memo_ts) < _HIST_MEMO_TTL:
            rows = _hist_memo
            return list(rows[-limit:]) if limit else list(rows)

    # The read runs outside the lock: holding it across the network would make
    # concurrent readers queue behind one another, which is the thing the memo
    # exists to avoid.
    rows = history()

    with _HIST_MEMO_LOCK:
        _hist_memo = rows
        _hist_memo_ts = time.time()
    return list(rows[-limit:]) if limit else list(rows)


def record_snapshot(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Store one day of expectations. Returns the row, or None if there is none.

    Keyed by the curve's own ``as_of`` rather than ``date.today()``. The ZQ close
    on a Saturday *is* Friday's, so keying by today would write a phantom weekend
    row holding Friday's numbers a second time; keying by as_of makes a weekend
    refresh overwrite Friday's row with identical values, and the series comes
    out trading-day-only for free.

    Same-day writes overwrite rather than accumulate, so the ~6 refreshes a
    4-hour TTL produces converge on the latest curve instead of storing intraday
    noise — the rationale cta.record_observation documents for the same choice.
    That is also why this needs no scheduler of its own.

    ``implied_rate`` is stored *absolute*, which is the point: the cut/hold/hike
    probabilities are relative to whatever the target range was that day, so once
    the Fed actually moves, a raw cut_prob plotted across the step changes
    meaning mid-line. base_lower/base_upper travel with the row so a reader can
    re-base; implied_rate needs no re-basing at all.
    """
    stamp = _valid_date(payload.get("as_of"))
    meetings_in = payload.get("meetings") or []
    if not stamp or not meetings_in:
        return None

    current = payload.get("current") or {}
    row: dict[str, Any] = {"date": stamp}
    for src, dst in (("lower", "base_lower"), ("upper", "base_upper"),
                     ("effr", "effr")):
        val = _number(current.get(src))
        if val is not None:
            row[dst] = val

    meetings: list[dict[str, Any]] = []
    for m in meetings_in:
        when = _valid_date(m.get("date"))
        rate = _number(m.get("implied_rate"))
        if not when or rate is None:
            continue
        entry: dict[str, Any] = {"date": when, "implied_rate": round(rate, 4)}
        for key in ("cut_prob", "hold_prob", "hike_prob"):
            val = _number(m.get(key))
            if val is not None:
                entry[key] = val
        meetings.append(entry)
    if not meetings:
        return None
    row["meetings"] = meetings

    _hist_save_ddb(row)
    # Union through the disk copy so the file keeps rows DynamoDB has but this
    # process has not loaded, rather than truncating to what we just wrote.
    merged = {r["date"]: r for r in _hist_load_disk()}
    merged[stamp] = row
    _hist_save_disk(sorted(merged.values(), key=lambda r: r["date"]))

    # Drop the memo, or today's row stays invisible for up to _HIST_MEMO_TTL and
    # the chart looks like the write silently failed.
    global _hist_memo, _hist_memo_ts
    with _HIST_MEMO_LOCK:
        _hist_memo = None
        _hist_memo_ts = 0.0

    log.info("FedWatch: recorded expectations for %s (%d meetings)",
             stamp, len(meetings))
    return row


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
            # Never let the series write cost us the payload: every store path
            # below already swallows its own errors, so this guards only against
            # a malformed payload reaching the normaliser.
            try:
                record_snapshot(fresh)
            except Exception as exc:  # noqa: BLE001
                log.warning("FedWatch: could not record expectations: %s", exc)
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
    """Timestamp of the payload we hold, fresh or not.

    Deliberately not freshness-filtered: callers use this to display "data as
    of", and the true age is the honest thing to show. Never branch page
    layout on this alone — pair it with :func:`is_cache_fresh`, or a worker
    holding an expired in-memory payload will render a confident "data as of"
    header above empty charts.
    """
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
    if data.get("_ver") != _CACHE_VER:
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

    from ystocker import warmup

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
                with warmup.cold_build('fedwatch'):
                    refresh_cache()
        except Exception as exc:
            log.warning("FedWatch background: startup warm failed: %s", exc)

        while True:
            # Sleep until the payload we actually hold expires, not a full TTL
            # from startup. Warming from a disk cache that was already 3h old
            # and then sleeping 4h left a ~3h window where every request saw a
            # stale cache, and the page rendered blank charts under a "data as
            # of" header. Re-derived each pass so a failed refresh retries soon
            # rather than waiting another whole TTL.
            with _cache_lock:
                ts = _cache_ts
            age = (time.time() - ts) if ts else _CACHE_TTL
            sleep_for = max(60.0, _CACHE_TTL - age)
            log.info("FedWatch background: next refresh in %.0f min (cache age %.0f min)",
                     sleep_for / 60, age / 60)
            time.sleep(sleep_for)
            try:
                log.info("FedWatch background: refreshing")
                with warmup.cold_build('fedwatch'):
                    refresh_cache()
            except Exception as exc:
                log.warning("FedWatch background: refresh failed: %s", exc)

    t = threading.Thread(target=_loop, name="fedwatch-background-refresh", daemon=True)
    t.start()
    log.info("FedWatch: background refresh thread started")
