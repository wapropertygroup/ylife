"""
ystocker.cta
~~~~~~~~~~~~
Dated Goldman Sachs CTA positioning snapshots reported in public sources.

Goldman's CTA model is proprietary and has no public real-time API. This module
therefore does not manufacture a continuous "Goldman" series from market prices.
It publishes explicitly dated public-report observations and the latest reported
S&P 500 trend thresholds. The markets page overlays those thresholds on live S&P
500 history so their distance can be monitored without mislabeling a proxy as
Goldman data.

Production can replace the built-in payload through the SSM-backed
``GOLDMAN_CTA_DATA_JSON`` environment variable. Expected top-level keys are
``positioning`` and ``latest``; either may be supplied independently.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from datetime import date
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Age thresholds for the latest snapshot, in days.
#:
#: Goldman publishes CTA Corner weekly, so one missed report is unremarkable and
#: three is a card nobody should be reading. There is no API behind this — every
#: value is hand-entered from a public write-up (see the module docstring), which
#: is exactly why it needs an age on it: the failure mode is not a bad fetch, it
#: is a human forgetting, and a month-old positioning number rendered in the same
#: neutral grey as yesterday's is indistinguishable from current.
FRESH_DAYS = 10   # within a report cycle plus slack
STALE_DAYS = 21   # three cycles missed

_PUBLIC_DATA: dict[str, Any] = {
    "positioning": [
        {
            "date": "2026-05-11",
            "global_equity_bn": 95.0,
            "percentile": 64.0,
            "source_title": "Goldman Sachs via Finvaulta",
            "source_url": "https://finvaulta.com/research/goldman-sachs/equity-positioning-and-key-levels-2026-05-11",
        },
        {
            "date": "2026-05-18",
            "global_equity_bn": 95.0,
            "percentile": 64.0,
            "us_equity_bn": 44.0,
            "source_title": "Goldman Sachs via Finvaulta",
            "source_url": "https://finvaulta.com/research/goldman-sachs-co-llc/equity-positioning-and-key-levels-2026-05-20",
        },
        {
            "date": "2026-05-26",
            "global_equity_bn": 90.0,
            "source_title": "Goldman Sachs via Finvaulta",
            "source_url": "https://finvaulta.com/research/goldman-sachs/equity-positioning-and-key-levels-2026-05-26",
        },
        {
            "date": "2026-06-02",
            "global_equity_bn": 93.0,
            "us_equity_bn": 34.0,
            "source_title": "Goldman Sachs via public reporting",
            "source_url": "https://finance.yahoo.com/markets/stocks/articles/cta-positioning-carries-lingering-selloff-110153773.html",
        },
    ],
    "latest": {
        "report_date": "2026-07-28",
        "spx_triggers": {
            "short": 7455.0,
            "medium": 7204.0,
            "long": 6765.0,
        },
        "flows_1w_global_bn": {
            "up": -0.275,
            "flat": -7.48,
            "down": -31.46,
        },
        "flows_1m_global_bn": {
            "down": -184.3,
        },
        "source_title": "Goldman Sachs CTA Corner via public reporting",
        "source_url": "https://www.nashnova.com/feed/special/goldman-sachs-ctas-to-net-sell-across-the-board-next-week-downside-could-trigger-7796",
    },
    "proprietary_model": True,
    "live_goldman_feed": False,
}


def _valid_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 3)


def _safe_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc else ""


def _positioning_points(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    points: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        point_date = _valid_date(raw.get("date"))
        global_equity = _number(raw.get("global_equity_bn"))
        if not point_date or global_equity is None:
            continue
        point: dict[str, Any] = {
            "date": point_date,
            "global_equity_bn": global_equity,
            "source_title": str(raw.get("source_title") or "Goldman Sachs public report")[:120],
            "source_url": _safe_url(raw.get("source_url")),
        }
        for key in ("percentile", "us_equity_bn"):
            number = _number(raw.get(key))
            if number is not None:
                point[key] = number
        points.append(point)
    return sorted(points, key=lambda point: point["date"])


def _latest_snapshot(value: object, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return fallback
    result = copy.deepcopy(fallback)
    report_date = _valid_date(value.get("report_date"))
    if report_date:
        result["report_date"] = report_date

    triggers = value.get("spx_triggers")
    if isinstance(triggers, dict):
        cleaned = {key: _number(triggers.get(key)) for key in ("short", "medium", "long")}
        if all(number is not None and number > 0 for number in cleaned.values()):
            result["spx_triggers"] = cleaned

    for group_name, scenarios in (
        ("flows_1w_global_bn", ("up", "flat", "down")),
        ("flows_1m_global_bn", ("up", "flat", "down")),
    ):
        raw_group = value.get(group_name)
        if not isinstance(raw_group, dict):
            continue
        cleaned_group = {
            scenario: number
            for scenario in scenarios
            if (number := _number(raw_group.get(scenario))) is not None
        }
        if cleaned_group:
            result[group_name] = cleaned_group

    if value.get("source_title"):
        result["source_title"] = str(value["source_title"])[:120]
    source_url = _safe_url(value.get("source_url"))
    if source_url:
        result["source_url"] = source_url
    return result


def _staleness(report_date: object, today: date | None = None) -> dict[str, Any]:
    """Age of the latest snapshot, and what to make of it.

    ``level`` is the thing the UI keys off: ``fresh`` / ``aging`` / ``stale``, or
    ``unknown`` when the date will not parse — which is treated as suspect rather
    than fresh, since an unreadable date is not evidence of currency.
    """
    parsed = _valid_date(report_date)
    if not parsed:
        return {"report_age_days": None, "level": "unknown"}
    age = ((today or date.today()) - date.fromisoformat(parsed)).days
    if age < 0:
        # A future date is a data-entry error, not a fresh report.
        return {"report_age_days": age, "level": "unknown"}
    level = "fresh" if age <= FRESH_DAYS else ("aging" if age <= STALE_DAYS else "stale")
    return {"report_age_days": age, "level": level}


def get_cta_positioning() -> dict[str, Any]:
    """Return validated built-in snapshots, optionally replaced by SSM JSON."""
    data = copy.deepcopy(_PUBLIC_DATA)
    raw_override = os.environ.get("GOLDMAN_CTA_DATA_JSON", "").strip()
    if raw_override:
        try:
            override = json.loads(raw_override)
            if not isinstance(override, dict):
                raise ValueError("top-level JSON value must be an object")
            points = _positioning_points(override.get("positioning"))
            if points:
                data["positioning"] = points
            data["latest"] = _latest_snapshot(override.get("latest"), data["latest"])
            data["source_mode"] = "ssm"
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("GOLDMAN_CTA_DATA_JSON ignored: %s", exc)
            data["source_mode"] = "built_in"
    else:
        data["source_mode"] = "built_in"

    data["positioning"] = _positioning_points(data["positioning"])
    data["latest_positioning"] = data["positioning"][-1] if data["positioning"] else None

    # Age travels with the payload rather than being recomputed by each consumer:
    # the card, the AI brief and /api/cache-age would otherwise each need to know
    # the thresholds, and would drift.
    latest = data.get("latest") or {}
    data["freshness"] = _staleness(latest.get("report_date"))
    data["freshness"]["fresh_days"] = FRESH_DAYS
    data["freshness"]["stale_days"] = STALE_DAYS
    return data


def staleness_line() -> str:
    """One-line status for logs and /api/cache-age. Never raises."""
    try:
        f = get_cta_positioning().get("freshness") or {}
        age, level = f.get("report_age_days"), f.get("level")
        return ("cta: report_date unreadable" if level == "unknown"
                else f"cta: snapshot {age}d old ({level})")
    except Exception as exc:  # noqa: BLE001
        return f"cta: status unavailable ({exc})"
