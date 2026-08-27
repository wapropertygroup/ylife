"""
ystocker.freshness
~~~~~~~~~~~~~~~~~~
Describes *how old* a payload is, and whether an upstream series has quietly
stopped publishing.

Two different kinds of staleness get conflated constantly, and keeping them
apart is the whole point of this module:

* **Cache age** -- when did *we* last fetch? Cheap to know, and the thing an
  eight-hour TTL is about.
* **Observation lag** -- when did the *vendor* last publish? A series can be
  fetched thirty seconds ago and still be seven years dead.

The second one is the failure mode documented in `fed.py`: `WASDRAL` and `MBST`
kept returning HTTP 200 with well-formed CSV years after they stopped
publishing, so nothing downstream had any way to notice. The fix proposed there
was to sanity-check row counts against `WALCL`, which was never implemented and
would have needed a hand-maintained expectation per series anyway.

:func:`series_health` takes a different route: infer each series' publication
cadence from its own observed dates, then compare the trailing gap against it. A
weekly series whose last observation is 2,983 days old is dead under any
tolerance, and no table needs maintaining -- if FRED changes a series from
weekly to monthly the inference simply follows.

Deliberately no holiday calendar here. `routes.py` owns `_is_us_trading_day`,
and importing it would be circular; for staleness a market holiday reads as
`session_close`, which is the right answer regardless.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ystocker import fetchguard

log = logging.getLogger(__name__)

try:                                            # pragma: no cover
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                               # pragma: no cover
    # Fixed EST. One hour off during EDT, which cannot flip a fresh quote to
    # stale here because every threshold below is measured in hours or days.
    _ET = timezone(timedelta(hours=-5))

#: A quote taken while the market is open goes stale quickly.
_REALTIME_STALE_AFTER_SECONDS = 15 * 60
#: How many multiples of the inferred cadence a series may lag before it is
#: presumed dead, plus a flat grace period.
#:
#: Tunable because the right value is a judgement call and differs by cadence.
#: Cadence is inferred from observation *dates*, but a series also has a
#: publication *lag* that no single snapshot reveals -- Case-Shiller is monthly
#: yet normally trails by about two months, so its healthy lag is already ~2x
#: cadence. Weekly and quarterly series have far more headroom under the same
#: formula. Bias is deliberately toward flagging: a false positive costs one log
#: line, while the failure this exists to catch went unnoticed for years.
_CADENCE_TOLERANCE = fetchguard.env_float("FRESHNESS_CADENCE_TOLERANCE", 3.0, 1.0)
_CADENCE_GRACE_DAYS = fetchguard.env_int("FRESHNESS_CADENCE_GRACE_DAYS", 7, 0)
#: Trailing observations used to infer cadence. Enough to be robust to a couple
#: of irregular gaps, short enough that a cadence *change* is picked up quickly.
_CADENCE_SAMPLE = 12


# ── Cache age ───────────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def age_label(age_seconds: float) -> str:
    """Compact human age: `"just now"`, `"14m ago"`, `"3h ago"`, `"2d ago"`."""
    age = max(0, int(age_seconds))
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{age // 60}m ago"
    if age < 86400:
        return f"{age // 3600}h ago"
    return f"{age // 86400}d ago"


def describe_age(ts: Optional[float], *, ttl_seconds: Optional[float] = None) -> dict[str, Any]:
    """Describe the age of a payload fetched at epoch *ts*.

    Returns `fetched_at` / `age_seconds` / `age_label`, plus `stale` when
    *ttl_seconds* is given. A missing or unusable *ts* is reported as stale with
    a null age rather than as fresh -- an unknown age is not evidence of youth.
    """
    try:
        ts = float(ts) if ts else 0.0
    except (TypeError, ValueError):
        ts = 0.0

    if ts <= 0:
        return {"fetched_at": None, "age_seconds": None, "age_label": "unknown", "stale": True}

    age = max(0.0, time.time() - ts)
    meta: dict[str, Any] = {
        "fetched_at": _iso(ts),
        "age_seconds": int(age),
        "age_label": age_label(age),
    }
    if ttl_seconds:
        meta["stale"] = age >= float(ttl_seconds)
        meta["ttl_seconds"] = int(ttl_seconds)
    return meta


# ── Market-hours-sensitive quotes ───────────────────────────────────────────

def _is_market_open(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 570 <= minutes < 960          # 09:30–16:00 ET


def _last_session_date(now_et: datetime) -> date:
    """Date of the most recent *fully closed* regular session, ignoring holidays.

    Before today's close, today does not count yet -- which is what makes
    Monday-pre-market resolve to Friday rather than to Monday.
    """
    day = now_et.date()
    if not (_is_market_open(now_et) or now_et.hour * 60 + now_et.minute >= 960):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def classify_quote(ts: Optional[float], *, stale_after_seconds: Optional[float] = None) -> dict[str, Any]:
    """Classify a market-data timestamp as realtime / session_close / stale.

    While the market is open, "fresh" means minutes old. While it is closed, the
    last print of the most recent session *is* the correct current value and must
    not be labelled stale -- otherwise every quote reads stale all weekend, which
    is how a freshness indicator trains people to ignore it.
    """
    meta = describe_age(ts)
    if meta["age_seconds"] is None:
        return {**meta, "status": "stale"}

    now_et = datetime.now(_ET)
    quote_et = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_ET)
    threshold = stale_after_seconds or _REALTIME_STALE_AFTER_SECONDS

    if _is_market_open(now_et):
        stale = meta["age_seconds"] >= threshold
        status = "stale" if stale else "realtime"
    else:
        stale = quote_et.date() < _last_session_date(now_et)
        status = "stale" if stale else "session_close"

    return {**meta, "stale": stale, "status": status, "market_open": _is_market_open(now_et)}


# ── Upstream series health ──────────────────────────────────────────────────

def _parse_day(value: Any) -> Optional[date]:
    """Coerce an ISO date string, `date` or `datetime` to a `date`.

    Accepting already-parsed values matters: :func:`series_health` parses once
    and then hands the results to :func:`infer_cadence_days`, and a
    string-only version of this silently returned None for every one of them --
    which surfaced as `cadence_days: null` and a `stale` verdict of "unknown" on
    a series with 300 perfectly good observations.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def infer_cadence_days(dates: Iterable[Any], *, sample: int = _CADENCE_SAMPLE) -> Optional[float]:
    """Median gap in days between the trailing observations of *dates*.

    Median rather than mean because a single revision or backfill gap would drag
    a mean far enough to make a live series look dead (or the reverse).
    """
    parsed = [d for d in (_parse_day(v) for v in dates) if d is not None]
    if len(parsed) < 3:
        return None
    parsed = sorted(parsed)[-max(3, sample):]
    gaps = [
        (b - a).days
        for a, b in zip(parsed, parsed[1:])
        if (b - a).days > 0
    ]
    if not gaps:
        return None
    return float(statistics.median(gaps))


def series_health(dates: Iterable[Any], *, label: Optional[str] = None) -> dict[str, Any]:
    """Report whether a dated series is still publishing.

    Returns `last_observation`, `observation_count`, `cadence_days`, `lag_days`,
    `stale` and `stale_after_days`. `stale` is None -- unknown, not False -- when
    there are too few observations to infer a cadence, so a caller cannot mistake
    "cannot tell" for "healthy".
    """
    parsed = sorted(d for d in (_parse_day(v) for v in dates) if d is not None)
    if not parsed:
        return {
            "last_observation": None,
            "observation_count": 0,
            "cadence_days": None,
            "lag_days": None,
            "stale": True,
            "stale_after_days": None,
        }

    last = parsed[-1]
    lag_days = max(0, (datetime.now(timezone.utc).date() - last).days)
    cadence = infer_cadence_days(parsed)

    if cadence is None:
        stale: Optional[bool] = None
        stale_after: Optional[int] = None
    else:
        stale_after = int(cadence * _CADENCE_TOLERANCE + _CADENCE_GRACE_DAYS)
        stale = lag_days > stale_after
        if stale and label:
            log.warning(
                "freshness: %s looks dead — last observation %s (%dd ago), "
                "cadence ~%.0fd, threshold %dd",
                label, last.isoformat(), lag_days, cadence, stale_after,
            )

    return {
        "last_observation": last.isoformat(),
        "observation_count": len(parsed),
        "cadence_days": round(cadence, 1) if cadence is not None else None,
        "lag_days": lag_days,
        "stale": stale,
        "stale_after_days": stale_after,
    }


def annotate_series(series: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Run :func:`series_health` over a `{id: {"dates": [...]}}` mapping.

    Returns `{id: health}` for every entry that carries dates, so a caller can
    attach it wholesale and let the UI decide what to surface. Entries already
    flagged `error` are reported as stale without cadence inference -- a failed
    fetch is not evidence about the vendor's publication schedule.
    """
    out: dict[str, dict[str, Any]] = {}
    for sid, payload in (series or {}).items():
        if not isinstance(payload, dict):
            continue
        if payload.get("error"):
            out[sid] = {
                "last_observation": None,
                "observation_count": 0,
                "cadence_days": None,
                "lag_days": None,
                "stale": True,
                "stale_after_days": None,
                "fetch_failed": True,
            }
            continue
        out[sid] = series_health(payload.get("dates") or [], label=sid)
    return out


def stale_series_ids(health: dict[str, dict[str, Any]]) -> list[str]:
    """Ids whose health says definitively stale. Excludes the unknown case."""
    return sorted(sid for sid, h in (health or {}).items() if h.get("stale") is True)
