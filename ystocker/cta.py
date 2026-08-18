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
    return data
