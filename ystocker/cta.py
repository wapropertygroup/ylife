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
import re
import threading
import time
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
    """Return the best available snapshot.

    Precedence, highest first:

    1. ``GOLDMAN_CTA_DATA_JSON`` from SSM — the manual override. It stays on top
       so a human can always correct the fetcher rather than fight it.
    2. The auto-fetched report on disk, written only after passing ``_validate``.
    3. The built-in payload, as the floor.

    ``source_mode`` says which one answered, so a reader is never guessing.
    """
    data = copy.deepcopy(_PUBLIC_DATA)

    # Tier 2 before the override, so the override can still win.
    fetched = _read_fetched()
    if fetched:
        candidate = _latest_snapshot(fetched.get("latest"), data["latest"])
        # Only adopt it if it is actually newer, so a stale file cannot pull the
        # card backwards after someone has set the SSM value by hand.
        if (candidate.get("report_date") or "") > (data["latest"].get("report_date") or ""):
            data["latest"] = candidate
            data["source_mode"] = "fetched"
            data["fetched_from"] = fetched.get("fetched_from", "")

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
            data.setdefault("source_mode", "built_in")
    else:
        data.setdefault("source_mode", "built_in")

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

# ---------------------------------------------------------------------------
# Automatic report pickup
# ---------------------------------------------------------------------------
# Goldman's model has no API, but the public write-up this module already cites
# is machine-readable, and its host permits it: nashnova's robots.txt carries an
# explicit `Allow: /feed/special/`, which is where these pieces live.
#
# Measured before building this, because the first answer I gave was that it
# needed a human:
#
#   * All 50 items in that RSS feed are /feed/special/ articles — the same
#     content type as the CTA Corner piece, so these do flow through it.
#   * The feed window is only ~5.5 hours for 50 items, so a *daily* poll would
#     miss a weekly report most weeks. Hourly gives roughly five chances.
#   * The article states the levels unambiguously: "short-term 7,455 ,
#     medium-term 7,204 , long-term 6,765". Those are also the only three
#     6000-9000 numbers on the page.
#   * The articles are in no sitemap, so the feed is the only discovery route.
#
# The danger was never the parse, it was publishing an *invented* Goldman number
# after a phrasing change. So the parser is not trusted: `_validate` gates it on
# invariants of what the data has to be, and anything that fails leaves the
# previous snapshot in place. Failing closed is the whole design.

REPORT_RSS_URL = "https://www.nashnova.com/rss.xml"
_FETCH_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "cache", "cta_fetched.json")
_HTTP_TIMEOUT = 20
_UA = "Mozilla/5.0 (compatible; ystocker/1.0; +https://stock.li-family.us)"

#: A trigger must sit within this fraction of the live S&P 500 to be believed.
#: This is the gate that makes a mis-parse harmless: "7" or "7.46" or a stray
#: year like 2026 cannot pass it, and no plausible phrasing change can either.
SPX_TOLERANCE = 0.30

#: Sanity band for the weekly flow figures, in billions. Goldman's worst-case
#: numbers run to a couple of hundred billion; a thousand is a parse error.
MAX_FLOW_BN = 600.0

_TITLE_RE = re.compile(r"\bCTA|\bCTAs\b|Goldman", re.I)
#: The separator between the three labelled levels varies — the live article uses
#: " , ", and stripped markup can leave "&" or a bullet. Rather than enumerate
#: punctuation, allow a bounded run of non-digits: `[^0-9]{0,15}` cannot cross
#: another number, so it can never reach past the value it is meant to skip, and
#: the three labels still anchor the whole match.
_TRIGGER_RE = re.compile(
    r"short[- ]term\s*([0-9][0-9,]{2,})[^0-9]{0,15}"
    r"medium[- ]term\s*([0-9][0-9,]{2,})[^0-9]{0,15}"
    r"long[- ]term\s*([0-9][0-9,]{2,})",
    re.I)


def _http_get(url: str, attempts: int = 3) -> str:
    """Fetch a URL, retrying a transient failure.

    Measured need: the article URL returned 503 from the box, then 200 six times
    in a row minutes later with two different User-Agents. So the source throws
    occasional 5xx that has nothing to do with us — and without a retry a weekly
    report could be missed entirely on a 503, since the feed window is only a few
    hours wide and the next poll may find the item already gone.
    """
    import urllib.error
    import urllib.request

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last = exc
            # 4xx is a decision, not a hiccup — retrying it just adds load.
            if exc.code < 500:
                raise
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < attempts:
            time.sleep(2 * attempt)
            log.debug("cta: retrying %s after %s", url[:60], last)
    raise last if last else RuntimeError("fetch failed")


def _pubdate_to_iso(raw: str | None, today: date | None = None) -> str | None:
    """An RSS ``pubDate`` as an ISO date, or None if it will not parse.

    This is load-bearing for two separate reasons, and the first version of the
    fetcher had neither because it just stamped ``date.today()``:

    * **Honesty.** The card reports how old the snapshot is. Stamping today makes
      that age unfalsifiable — every pickup reads "fresh, 0 days" no matter when
      the report was actually written, which defeats the entire point of the
      staleness work this fetcher was built alongside.
    * **Stability.** ``report_date`` is also the newness guard. A date that moves
      every time the same article is re-read lets one unchanging article ratchet
      the date forward on every poll, so identical numbers would show as freshly
      published indefinitely. A pubDate is a property of the article, so
      re-reading it is a no-op.

    A date in the future is a feed or clock error rather than information, so it
    is refused here and the caller falls back — deliberately *not* passed through
    to ``_staleness``, which would render it ``unknown`` and hide a good parse
    behind a bad timestamp.
    """
    if not raw or not isinstance(raw, str):
        return None
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    iso = parsed.date().isoformat()
    if date.fromisoformat(iso) > (today or date.today()):
        return None
    return iso


def _visible_text(html: str) -> str:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = body.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", body).strip()


def _num_from_text(raw: str) -> float | None:
    return _number(raw.replace(",", "").replace("$", ""))


#: Money in the article prose, with its unit. The unit is **not** optional and
#: not assumed, because the same paragraph mixes them: the 1-week up scenario is
#: "$275 mn" while everything around it is "bn". Reading 275 as billions is a
#: 1000x error that sails straight through MAX_FLOW_BN, so there is no
#: independent gate to catch it — the unit has to be read.
_MONEY_RE = re.compile(r"\$\s?([0-9][0-9.,]*)\s*(bn|billion|mn|million)\b", re.I)
_UNIT_BN = {"bn": 1.0, "billion": 1.0, "mn": 1e-3, "million": 1e-3}

#: The 1-week scenario labels, and the 1-month ones. Goldman's write-ups label
#: these consistently even when the surrounding prose changes.
_SCEN_1W_RE = re.compile(r"\b(Flat|Up|Down)\s+scenario", re.I)
_SCEN_1M_RE = re.compile(r"one[- ]month\s+(downside|upside)\s+scenario", re.I)
_1M_NAME = {"downside": "down", "upside": "up"}


def _money_bn(segment: str) -> list[float]:
    """Every dollar figure in a span, normalised to billions."""
    out: list[float] = []
    for m in _MONEY_RE.finditer(segment):
        value = _num_from_text(m.group(1))
        if value is None:
            continue
        out.append(round(value * _UNIT_BN[m.group(2).lower()], 4))
    return out


def _direction(segment: str) -> int:
    """-1 selling, +1 buying, 0 when the prose does not say.

    Zero is a refusal, not a default. The 1-week down scenario reads "Down
    scenario: ~$31.46 bn global ( ~$15.1 bn US)" with no verb at all — the fact
    that it is selling lives in an earlier sentence. Inferring it from the
    neighbouring scenario would be reading adjacency as meaning, and the cost of
    being wrong is a flow whose sign is inverted: net buying displayed as net
    selling, which inverts what the card is for. Dropping the number is the
    cheaper mistake.
    """
    sell = re.search(r"net\s+sell(?:ing|er)|sell(?:ing|-off)", segment, re.I)
    buy = re.search(r"net\s+buy(?:ing|er)|buying", segment, re.I)
    if sell and buy:
        return -1 if sell.start() < buy.start() else 1
    if sell:
        return -1
    if buy:
        return 1
    return 0


def _scenario_flows(text: str, label_re: re.Pattern[str],
                    rename: dict[str, str] | None = None,
                    window: int = 240) -> tuple[dict[str, float], dict[str, float]]:
    """Signed global and US flows per scenario, in billions.

    Each label's span runs to the next label so one scenario cannot borrow a
    figure from its neighbour. Within a span the first dollar figure is global and
    a later one is US only when the span actually says so.
    """
    marks = [(m.group(1).lower(), m.start(), m.end())
             for m in label_re.finditer(text)]
    glob: dict[str, float] = {}
    us: dict[str, float] = {}
    for i, (name, _start, end) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else min(len(text), end + window)
        # Also stop at the next bullet. Without this the span bleeds into the
        # commentary that follows, and the 1-week down scenario — "~$31.46 bn
        # global ( ~$15.1 bn US). • This means → even a rally won't trigger fresh
        # buying." — picked up "buying" from the *next sentence* and recorded a
        # net purchase of 31bn where the report means a sale. An inverted sign is
        # the worst possible outcome here, worse than no number, and it is exactly
        # the adjacency-as-meaning error _direction is written to avoid.
        bullet = text.find("•", end)
        if 0 <= bullet < stop:
            stop = bullet
        segment = text[end:stop]
        key = (rename or {}).get(name, name)
        sign = _direction(segment)
        if not sign:
            log.debug("cta: %r scenario states no direction — skipped", name)
            continue
        amounts = _money_bn(segment)
        if not amounts:
            continue
        glob[key] = round(sign * abs(amounts[0]), 4)
        if len(amounts) > 1 and re.search(r"\bU\.?S\.?\b", segment):
            us[key] = round(sign * abs(amounts[1]), 4)
    return glob, us


def parse_article(html: str) -> dict[str, Any] | None:
    """Pull the trigger levels out of an article. No validation here."""
    text = _visible_text(html)
    m = _TRIGGER_RE.search(text)
    if not m:
        return None
    triggers = {}
    for key, group in (("short", 1), ("medium", 2), ("long", 3)):
        value = _num_from_text(m.group(group))
        if value is None:
            return None
        triggers[key] = value

    out: dict[str, Any] = {"spx_triggers": triggers}

    # The 1-week and 1-month scenario tables. Every one of these is optional and
    # extracted independently, so a phrasing change costs that one number rather
    # than the whole report — the triggers are the part with an external
    # cross-check and must not be held hostage to the flows.
    w_glob, w_us = _scenario_flows(text, _SCEN_1W_RE)
    m_glob, m_us = _scenario_flows(text, _SCEN_1M_RE, _1M_NAME)
    if w_glob:
        out["flows_1w_global_bn"] = w_glob
    if w_us:
        out["flows_1w_us_bn"] = w_us
    if m_us:
        out["flows_1m_us_bn"] = m_us

    flows = dict(m_glob)
    if "down" not in flows:
        # Fallback for the headline figure, which is often stated outside any
        # labelled scenario ("$184.3 billion worst-case selling wave").
        worst = re.search(r"\$([0-9][0-9.,]*)\s*billion[^.]{0,60}(?:selling|sell|downside)",
                          text, re.I)
        if worst:
            value = _num_from_text(worst.group(1))
            if value is not None:
                flows["down"] = -abs(value)
    out["flows_1m_global_bn"] = flows
    return out


def _validate(parsed: dict[str, Any], spx_ref: float | None) -> tuple[bool, str]:
    """Gate a parse on invariants of the data. Reasons are returned, not raised."""
    triggers = (parsed or {}).get("spx_triggers") or {}
    if set(triggers) != {"short", "medium", "long"}:
        return False, "expected exactly short/medium/long triggers, got %s" % sorted(triggers)
    short, medium, long_ = triggers["short"], triggers["medium"], triggers["long"]
    if not all(isinstance(v, float) and v > 0 for v in (short, medium, long_)):
        return False, "non-positive trigger"
    # Goldman's own ordering. A parse that scrambles the labels breaks it.
    if not short > medium > long_:
        return False, "triggers not ordered short>medium>long: %s" % triggers
    if spx_ref:
        for name, value in (("short", short), ("medium", medium), ("long", long_)):
            if abs(value - spx_ref) / spx_ref > SPX_TOLERANCE:
                return False, ("%s trigger %.0f is >%.0f%% from S&P %.0f"
                               % (name, value, SPX_TOLERANCE * 100, spx_ref))
    else:
        # Without a reference the proximity gate cannot run, so require the levels
        # to at least be in the range an index trades in rather than a year or a
        # share price.
        if not all(1000 <= v <= 20000 for v in (short, medium, long_)):
            return False, "triggers outside a plausible index range: %s" % triggers
    # Every flow bucket, not just the 1-month global one. The unit trap ("$275
    # mn" read as billions) lands inside this band rather than outside it, so
    # this bound is a backstop against absurdity, not a substitute for reading
    # the unit — see _MONEY_RE.
    for bucket in ("flows_1w_global_bn", "flows_1w_us_bn",
                   "flows_1m_global_bn", "flows_1m_us_bn"):
        for scenario, value in ((parsed.get(bucket) or {}).items()):
            if not isinstance(value, float):
                return False, "%s.%s is not a number" % (bucket, scenario)
            if abs(value) > MAX_FLOW_BN:
                return False, "flow %s.%s=%.1fbn exceeds %.0fbn" % (
                    bucket, scenario, value, MAX_FLOW_BN)
    # There is deliberately no "US cannot exceed global" check here. I wrote one,
    # called it arithmetic rather than a heuristic, and the live article disproved
    # it on the first run: the 1-week up scenario is "$275 mn global net selling
    # ( ~$2.03 bn in US stocks)". Global net is a *sum of regional nets that
    # offset*, so US selling of 2bn against 1.75bn of buying elsewhere nets to
    # 0.275bn globally. |US| > |global| is normal, and the check rejected a
    # correct parse of the only real article I had.
    return True, "ok"


def distance_to_triggers(spx: float | None,
                         triggers: dict[str, float] | None = None) -> dict[str, Any]:
    """How far the index sits above each CTA trigger, in percent.

    This is the more actionable of the two views: a net-length reading says how
    much there is to sell, whereas the distance to the next threshold says how
    close the mechanical selling is to actually starting. A -3% gap to the
    short-term trigger with modest positioning matters more than a large position
    that is 12% clear of anything.

    ``distance_pct`` is signed: positive means the index is above the level, so
    negative means already through it. ``next_trigger`` is the highest level still
    below the index — the one that fires next — and is None once every level has
    been breached, which is a different state from "no data" and worth
    distinguishing in the UI.
    """
    if triggers is None:
        triggers = ((get_cta_positioning().get("latest") or {}).get("spx_triggers") or {})
    price = _number(spx)
    out: dict[str, Any] = {"spx": price, "levels": []}
    if not price or price <= 0:
        return out

    breached, pending = [], []
    for key in ("short", "medium", "long"):
        level = _number((triggers or {}).get(key))
        if not level or level <= 0:
            continue
        row = {
            "key": key,
            "trigger": level,
            "distance_pct": round((price - level) / level * 100, 2),
            "breached": price < level,
        }
        out["levels"].append(row)
        (breached if row["breached"] else pending).append(row)

    out["breached"] = [r["key"] for r in breached]
    # Highest level still below the price: the next one a decline would cross.
    nxt = max(pending, key=lambda r: r["trigger"], default=None)
    if nxt:
        out["next_trigger"] = nxt["key"]
        out["next_trigger_level"] = nxt["trigger"]
        out["next_trigger_distance_pct"] = nxt["distance_pct"]
    return out


# ---------------------------------------------------------------------------
# The tracker: a durable daily series, not a cache
# ---------------------------------------------------------------------------
#
# Distance-to-trigger is only interesting as a *series*. One reading says the S&P
# is 3.4% above the short-term level; a month of readings says whether it is
# converging on it. That series cannot be recomputed after the fact, because it
# depends on which report was in force on each past day — Goldman's triggers
# change weekly and nothing publishes their history. So this is an observed
# series in the sense CLAUDE.md means, and keeping it only in `cache/` would lose
# it whenever the instance is replaced. That is not hypothetical: it is exactly
# how the SPY/QQQ valuation chart reset to a single point.
#
# Same shape as ystocker-valuation-history / -fear-greed / -pcr-history: a `date`
# string hash key, PAY_PER_REQUEST, values stored as strings because DynamoDB
# rejects float. Absence of the table is not an error — local dev has no AWS
# credentials and the disk mirror still works.

_HIST_TABLE_NAME = "ystocker-cta-history"
_HIST_PATH = os.path.join(os.path.dirname(_FETCH_CACHE), "cta_history.json")

#: Sentinel key for the in-force report snapshot, which is not an observation.
#: Sharing one table avoids a second thing to provision; the series readers skip
#: any key that is not an ISO date, so the two cannot be confused.
_REPORT_KEY = "_latest_report"

_HIST_NUMERIC = ("spx", "t_short", "t_medium", "t_long",
                 "d_short", "d_medium", "d_long")

_hist_table = None
_hist_unavail_until = 0.0
_HIST_LOCK = threading.Lock()


def _get_hist_table():
    """DynamoDB table for the tracker series, or None when unavailable.

    The 5-minute backoff stops every pass paying a connection timeout when the
    table genuinely is not there, which is the normal case in local dev.
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
            log.info("cta: DynamoDB tracker table connected: %s", _HIST_TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - degrade to disk-only
            log.warning("cta: DynamoDB tracker unavailable: %s", exc)
            _hist_table = None
            _hist_unavail_until = time.time() + 300
        return _hist_table


def _row_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    stamp = _valid_date(item.get("date"))
    if not stamp:
        return None                      # the sentinel row, or junk
    row: dict[str, Any] = {"date": stamp}
    for key in _HIST_NUMERIC:
        value = _number(item.get(key))
        if value is not None:
            row[key] = value
    report = _valid_date(item.get("report_date"))
    if report:
        row["report_date"] = report
    return row if len(row) > 1 else None


def _hist_load_ddb() -> list[dict[str, Any]]:
    table = _get_hist_table()
    if table is None:
        return []
    try:
        rows, kwargs = [], {}
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                row = _row_from_item(item)
                if row:
                    rows.append(row)
            # A scan is paginated; without this the series silently stops at the
            # first page once there is more than 1 MB of history.
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: tracker load failed: %s", exc)
        return []


def _hist_save_ddb(row: dict[str, Any]) -> None:
    table = _get_hist_table()
    if table is None or not row.get("date"):
        return
    try:
        item: dict[str, Any] = {"date": row["date"]}
        for key in _HIST_NUMERIC:
            if row.get(key) is not None:
                item[key] = str(row[key])       # DynamoDB rejects float
        if row.get("report_date"):
            item["report_date"] = row["report_date"]
        table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: tracker save failed for %s: %s", row.get("date"), exc)


def _hist_load_disk() -> list[dict[str, Any]]:
    try:
        with open(_HIST_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows") if isinstance(data, dict) else data
        out = []
        for raw in rows or []:
            row = _row_from_item(raw) if isinstance(raw, dict) else None
            if row:
                out.append(row)
        return out
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: unreadable tracker file (%s)", exc)
        return []


def _hist_save_disk(rows: list[dict[str, Any]]) -> None:
    import tempfile
    try:
        os.makedirs(os.path.dirname(_HIST_PATH), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_HIST_PATH), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows}, fh, allow_nan=False)
        os.replace(tmp, _HIST_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: could not write tracker file: %s", exc)


def history(limit: int | None = None) -> list[dict[str, Any]]:
    """The tracker series, oldest first.

    DynamoDB and disk are unioned by date rather than one being preferred: disk
    can hold a row written while DynamoDB was briefly unreachable, and DynamoDB
    holds everything that predates the current instance. Later writes for a date
    win, which is what makes a same-day re-record an update rather than a
    duplicate.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in _hist_load_ddb() + _hist_load_disk():
        merged[row["date"]] = {**merged.get(row["date"], {}), **row}
    rows = sorted(merged.values(), key=lambda r: r["date"])
    return rows[-limit:] if limit else rows


def record_observation(spx: float | None,
                       today: date | None = None) -> dict[str, Any] | None:
    """Record where the index sat relative to the in-force triggers today.

    One row per calendar day, overwritten if called again the same day, so the
    hourly poller converges on the latest reading rather than accumulating
    intraday noise. Returns the row, or None when there is nothing to record.
    """
    dist = distance_to_triggers(spx)
    if not dist.get("levels"):
        return None
    stamp = (today or date.today()).isoformat()
    latest = get_cta_positioning().get("latest") or {}
    row: dict[str, Any] = {"date": stamp, "spx": dist["spx"]}
    if _valid_date(latest.get("report_date")):
        row["report_date"] = latest["report_date"]
    for level in dist["levels"]:
        row["t_%s" % level["key"]] = level["trigger"]
        row["d_%s" % level["key"]] = level["distance_pct"]

    rows = [r for r in history() if r["date"] != stamp] + [row]
    rows.sort(key=lambda r: r["date"])
    _hist_save_disk(rows)
    _hist_save_ddb(row)
    return row


def _report_to_ddb(snapshot: dict[str, Any]) -> None:
    """Mirror a fetched report into DynamoDB so it survives the instance.

    Without this the fetcher's own output lived only in ``cache/``, so every
    report it ever picked up would vanish when the box was replaced and the card
    would silently revert to the built-in snapshot from months earlier.
    """
    table = _get_hist_table()
    if table is None:
        return
    try:
        table.put_item(Item={"date": _REPORT_KEY,
                             "payload": json.dumps(snapshot, allow_nan=False)})
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: could not mirror report to DynamoDB: %s", exc)


def _report_from_ddb() -> dict[str, Any] | None:
    table = _get_hist_table()
    if table is None:
        return None
    try:
        item = (table.get_item(Key={"date": _REPORT_KEY}) or {}).get("Item") or {}
        payload = item.get("payload")
        if not payload:
            return None
        data = json.loads(payload)
        return data if isinstance(data, dict) and data.get("latest") else None
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: could not read report from DynamoDB: %s", exc)
        return None


def _read_fetched() -> dict[str, Any] | None:
    try:
        with open(_FETCH_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("latest"):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: unreadable fetched cache (%s)", exc)
    # Disk is gone or unusable — fall back to the DynamoDB mirror. This is the
    # path a replaced instance takes, and without it the box would come up having
    # forgotten every report the fetcher had picked up.
    return _report_from_ddb()


def _write_fetched(payload: dict[str, Any]) -> None:
    import tempfile
    try:
        os.makedirs(os.path.dirname(_FETCH_CACHE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_FETCH_CACHE), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, allow_nan=False)
        os.replace(tmp, _FETCH_CACHE)
    except Exception as exc:  # noqa: BLE001
        log.warning("cta: could not write fetched cache: %s", exc)
    # Independent of the disk write: a full disk must not also cost the durable
    # copy, and the durable copy is the one that survives this instance.
    _report_to_ddb(payload)


def fetch_latest_report(spx_ref: float | None = None) -> dict[str, Any] | None:
    """Look for a newer CTA report and store it if it validates.

    Returns the stored snapshot, or None when there is nothing new or nothing
    trustworthy. Never raises: this runs on a timer and a bad week upstream must
    leave the existing card alone rather than break it.
    """
    try:
        rss = _http_get(REPORT_RSS_URL)
    except Exception as exc:  # noqa: BLE001
        # Warning, not info. A source that is unreachable looks exactly like a
        # source with nothing new, and the second is normal — so the first has to
        # be loud or the fetcher can be broken for weeks while the card just sits
        # there quietly going stale.
        log.warning("cta: feed unreachable after retries (%s) — no update this pass", exc)
        return None

    items = re.findall(r"<item>(.*?)</item>", rss, re.S)
    candidates: list[tuple[str, str, str | None]] = []
    for item in items:
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        link_m = re.search(r"<link>(.*?)</link>", item, re.S)
        if not title_m or not link_m:
            continue
        title = title_m.group(1).strip()
        # Both terms required: "Goldman" alone matches unrelated bank notes, and
        # "CTA" alone is a common enough abbreviation to catch noise.
        if _TITLE_RE.search(title) and re.search(r"\bCTA", title, re.I):
            pub_m = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
            candidates.append((title, link_m.group(1).strip(),
                               pub_m.group(1) if pub_m else None))

    if not candidates:
        log.debug("cta: no CTA item in the %d-item feed window", len(items))
        return None

    current = get_cta_positioning()
    current_date = (current.get("latest") or {}).get("report_date") or ""

    for title, url, pub_raw in candidates:
        try:
            parsed = parse_article(_http_get(url))
        except Exception as exc:  # noqa: BLE001
            log.info("cta: could not read %s (%s)", url, exc)
            continue
        if not parsed:
            log.info("cta: no trigger levels found in %r", title[:70])
            continue
        ok, reason = _validate(parsed, spx_ref)
        if not ok:
            # The important branch. A rejected parse is logged loudly and changes
            # nothing, so the card keeps its previous (honestly dated) snapshot.
            log.warning("cta: REJECTED a parse of %r — %s", title[:70], reason)
            continue

        # The feed's own publication date, not today. See _pubdate_to_iso: using
        # today would both overstate freshness and let one article ratchet its
        # date forward on every poll. The fallback is logged so that a feed which
        # stops carrying pubDate degrades visibly rather than silently.
        report_date = _pubdate_to_iso(pub_raw)
        if not report_date:
            report_date = date.today().isoformat()
            log.warning("cta: %r has no usable pubDate (%r) — dating it today, "
                        "which may overstate freshness", title[:70], pub_raw)
        if report_date <= current_date:
            log.debug("cta: parsed report (%s) is not newer than %s",
                      report_date, current_date)
            return None
        snapshot = {
            "latest": {
                "report_date": report_date,
                "spx_triggers": parsed["spx_triggers"],
                "flows_1m_global_bn": parsed.get("flows_1m_global_bn") or {},
                "source_title": title[:120],
                "source_url": _safe_url(url),
            },
            "fetched_at": date.today().isoformat(),
            "fetched_from": url,
        }
        _write_fetched(snapshot)
        log.info("cta: stored a new report — %s triggers %s (validated against S&P %s)",
                 report_date, parsed["spx_triggers"], spx_ref)
        return snapshot

    return None
