"""
ystocker.odds
~~~~~~~~~~~~~
The arithmetic behind /predictions: Polymarket's and Kalshi's events put into
one shape, sorted into topics, trimmed to what a card can show, ranked by what
moved, compared against the Fed Funds futures, and -- for the AI read -- the
prompt, the parser and the score that settles it.

Pure. No network, no cache, and no clock unless one is passed in, which is what
lets ``tests/test_odds.py`` pin every rule here against payloads captured from
both venues. ``predictions.py`` fetches, ``odds_ai.py`` asks Gemini and
``odds_ledger.py`` keeps the record.

Two venues, two vocabularies
----------------------------
Both list *events* made of binary *markets*: "Fed decision in October?" is five
yes/no contracts, one per outcome. They differ in nearly everything else:

=====================  ===========================  ============================
                       Polymarket (Gamma API)       Kalshi (trade API v2)
=====================  ===========================  ============================
one-of-many outcomes   ``negRisk``                  ``mutually_exclusive``
outcome label          ``groupItemTitle``           ``yes_sub_title``
price                  ``outcomePrices[0]`` (str)   ``yes_bid/ask/last_dollars``
1-day move             ``oneDayPriceChange``        last − ``previous_price``
volume                 USD                          contracts, $1 each at expiry
depth                  ``liquidity`` (USD on book)  ``open_interest`` (contracts)
=====================  ===========================  ============================

The price shown is each venue's own display rule: the bid/ask midpoint while the
spread is at most 10¢, otherwise the last trade. Polymarket's ``outcomePrices``
already *is* that number, so it is used as it comes; Kalshi's is computed with
the same rule, so the two venues are read alike rather than one by its midpoint
and the other by a stale print.

A Kalshi contract pays $1, so its contract count is also its notional in
dollars. That makes the two volume figures comparable enough to rank one board
by, and not comparable enough to quote as if they were the same measurement --
the page labels Kalshi's as contracts.

An absent change field is unknown, not zero
-------------------------------------------
Gamma omits ``oneDayPriceChange`` on many markets that plainly moved (October's
"No change" carried a one-week move of +49pp and no one-day field at all), so a
missing value is ``None`` here and the movers list skips it. Reading it as 0
would hide exactly the markets that moved most.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

# ── Topics ──────────────────────────────────────────────────────────────────

#: Display order of the topic chips on the page.
TOPICS: tuple[str, ...] = (
    "rates", "inflation", "jobs", "growth", "equities",
    "companies", "commodities", "crypto", "policy",
)

#: Polymarket tag slugs (lower-cased) that place an event in a topic.
_PM_TAG_TOPICS: dict[str, frozenset[str]] = {
    "rates": frozenset({
        "fed", "fed-rates", "fomc", "global-rates", "interest-rates", "interest-rate",
        "treasuries", "monetary-policy", "ecb", "boj", "boe", "bank-of-england",
        "european-central-bank", "rba", "rbi", "boi",
    }),
    "inflation": frozenset({"inflation", "cpi", "pce", "core-pce", "cpi-release", "ppi"}),
    "jobs": frozenset({
        "jobs", "jobs-report", "nfp", "nonfarm-payroll", "unemployment", "jolts",
        "job-openings", "labor",
    }),
    "growth": frozenset({"gdp", "recession", "ism", "pmi", "umich", "consumer", "macro-graph"}),
    "companies": frozenset({"ipos", "ipo", "bankruptcy", "acquisitions", "m-and-a"}),
    "equities": frozenset({
        "stocks", "equities", "indicies", "indices", "sp-500", "spx", "nasdaq",
        "earnings", "big-tech",
    }),
    "commodities": frozenset({
        "commodities", "oil", "gold", "silver", "petroleum", "diesel", "natural-gas",
        "hormuz", "strait-of-hormuz", "opec",
    }),
    "crypto": frozenset({"crypto", "bitcoin", "ethereum", "solana", "crypto-prices"}),
    "policy": frozenset({
        "tariffs", "trade", "trade-war", "shutdown", "government-shutdown", "geopolitics",
        "sanctions", "debt-ceiling", "taxes",
    }),
}

#: Which topic wins when an event carries tags from several. Companies precede
#: equities because an IPO market is tagged ``big-tech`` as often as ``ipos``,
#: and "Anthropic IPO by…?" is a question about a company, not about an index.
#: Rates come first because every Fed market is also tagged ``economic-policy``.
_TOPIC_PRECEDENCE: tuple[str, ...] = (
    "rates", "inflation", "jobs", "growth", "companies", "equities",
    "commodities", "crypto", "policy",
)


def classify_tags(tags: Iterable[str]) -> Optional[str]:
    """The topic a set of Polymarket tag slugs belongs to, or None."""
    have = {str(t).strip().lower() for t in tags if t}
    for topic in _TOPIC_PRECEDENCE:
        if have & _PM_TAG_TOPICS[topic]:
            return topic
    return None


# ── Small coercions ─────────────────────────────────────────────────────────

#: Resolution rules kept per event: the page shows them on demand and the AI
#: read quotes them. Long enough for a full Kalshi rule, short of the multi-
#: paragraph Polymarket descriptions that are mostly boilerplate.
RULES_MAX = 900

#: Outcomes kept per event. A card shows a few; the AI read forecasts exactly
#: these, so the number is also the size of what a forecast covers.
MAX_OUTCOMES = 8


def _num(value: Any) -> Optional[float]:
    """A finite float from a number or numeric string, else None.

    Both venues send prices as strings (``"0.825"``, ``"0.8200"``) and counts
    as fixed-point strings (``"225754.87"``); a bool is never a number here.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _prob(value: Any) -> Optional[float]:
    """A probability in [0, 1], else None."""
    out = _num(value)
    if out is None or out < 0 or out > 1:
        return None
    return out


def _change(value: Any) -> Optional[float]:
    """A probability change in [-1, 1], else None (absent is unknown)."""
    out = _num(value)
    if out is None or abs(out) > 1:
        return None
    return round(out, 4)


def _json_list(value: Any) -> list:
    """Gamma encodes ``outcomes`` / ``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except ValueError:
            return []
        return out if isinstance(out, list) else []
    return []


def parse_ts(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from an ISO-8601 string, else None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        # Kalshi sends microseconds with six digits but occasionally fewer
        # ("…18:26.89Z"), which fromisoformat on 3.10 refuses. Seconds suffice.
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
        if not m:
            return None
        try:
            out = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}+00:00")
        except ValueError:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    ts = parse_ts(value)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _clip(text: Any, limit: int) -> str:
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def display_price(bid: Optional[float], ask: Optional[float],
                  last: Optional[float]) -> Optional[float]:
    """The price a venue would show: the midpoint while the spread is at most
    10¢, otherwise the last trade. A one-sided or crossed book falls back to
    the last trade too, since its "midpoint" is not a price anyone offered."""
    if bid is not None and ask is not None and 0 <= bid <= ask <= 1 and ask - bid <= 0.10 + 1e-9:
        return round((bid + ask) / 2, 4)
    return last


def _numeric_labels(labels: Sequence[str]) -> bool:
    """Whether outcome labels are thresholds or buckets ("Above 4.5%",
    "6,800 to 6,999.99", "2 (50 bps)") rather than names ("Alphabet")."""
    if not labels:
        return False
    hits = sum(1 for lab in labels if re.search(r"\d", lab or ""))
    return hits / len(labels) >= 0.6


def _kind(n: int, *, exclusive: bool, numeric: bool) -> str:
    """``binary`` (one market), ``exclusive`` (exactly one outcome resolves
    Yes), ``ladder`` (independent thresholds) or ``multi`` (independent named
    outcomes)."""
    if n == 1:
        return "binary"
    if exclusive:
        return "exclusive"
    return "ladder" if numeric else "multi"


# ── Normalisation ───────────────────────────────────────────────────────────

def normalize_polymarket(event: Mapping[str, Any], *,
                         fallback_topic: str = "growth") -> Optional[dict[str, Any]]:
    """One Gamma ``/events`` item in this module's shape, or None.

    Markets already ``closed`` inside an open event are dropped: "How high will
    inflation get in 2026?" keeps its resolved 3% and 4% rungs listed at a price
    of 1, and showing them as live 100% odds would be a claim about a question
    that has been answered. So are markets not taking orders: a one-of-many
    event carries placeholder slots for outcomes it may add later ("Company F"
    through "Company T" in "Largest Company end of October 2026?"), each
    ``active: false`` with no volume and a price of 0.5 that nobody is quoting.
    Listed, they read as fourteen 50% outcomes beside NVIDIA's 95%.
    """
    if not isinstance(event, Mapping) or event.get("closed") or event.get("archived"):
        return None
    slug = str(event.get("slug") or "").strip()
    title = str(event.get("title") or "").strip()
    if not slug or not title:
        return None

    listed = [m for m in (event.get("markets") or []) if isinstance(m, Mapping)]
    markets = [m for m in listed
               if not m.get("closed")
               and m.get("active") is not False and m.get("acceptingOrders") is not False]
    # Whether this is a one-market question is the *event's* shape, read before
    # filtering: a group whose other slots are placeholders still names its one
    # live outcome "NVIDIA", not "Yes".
    single = len(listed) == 1
    outcomes: list[dict[str, Any]] = []
    for idx, m in enumerate(markets):
        prices = _json_list(m.get("outcomePrices"))
        names = _json_list(m.get("outcomes"))
        bid, ask = _prob(m.get("bestBid")), _prob(m.get("bestAsk"))
        last = _prob(m.get("lastTradePrice"))
        p = _prob(prices[0]) if prices else None
        if p is None:
            p = display_price(bid, ask, last)
        if p is None:
            continue
        if single:
            label = str(names[0]).strip() if names else "Yes"
        else:
            label = str(m.get("groupItemTitle") or m.get("question") or "").strip()
        if not label:
            continue
        order = _num(m.get("groupItemThreshold"))
        outcomes.append({
            "label": label,
            "p": round(p, 4),
            "bid": bid,
            "ask": ask,
            "d1": _change(m.get("oneDayPriceChange")),
            "w1": _change(m.get("oneWeekPriceChange")),
            "id": str(m.get("id") or ""),
            "order": order if order is not None else float(idx),
        })
    if not outcomes:
        return None

    labels = [o["label"] for o in outcomes]
    numeric = _numeric_labels(labels)
    exclusive = bool(event.get("negRisk") or event.get("enableNegRisk"))
    tags = [str(t.get("slug") or "").strip().lower()
            for t in (event.get("tags") or []) if isinstance(t, Mapping)]
    rules = ""
    if markets:
        rules = markets[0].get("description") or ""
    rules = rules or event.get("description") or ""
    return {
        "key": f"pm:{slug}",
        "platform": "polymarket",
        "id": slug,
        "title": title,
        "subtitle": "",
        "url": f"https://polymarket.com/event/{slug}",
        "topic": classify_tags(tags) or fallback_topic,
        "kind": _kind(len(outcomes), exclusive=exclusive, numeric=numeric),
        "numeric": numeric,
        "closes": _iso(event.get("endDate")),
        "volume": _num(event.get("volume")),
        "volume_24h": _num(event.get("volume24hr")),
        "liquidity": _num(event.get("liquidity")),
        "open_interest": _num(event.get("openInterest")),
        "outcomes": outcomes,
        "n_outcomes": len(outcomes),
        "rules": _clip(rules, RULES_MAX),
        "tags": [t for t in tags if t][:12],
    }


#: Market statuses that mean "trading now". Kalshi's v2 API says ``active``;
#: older responses said ``open``. ``initialized`` (listed, not yet trading) and
#: ``closed``/``determined``/``settled``/``finalized`` are all excluded.
_KALSHI_LIVE = frozenset({"active", "open"})


def _kalshi_order(market: Mapping[str, Any], idx: int) -> float:
    """Sort key for a Kalshi rung: its strike, so a ladder reads low to high.

    A ``less`` bucket ("3,999.99 or below") carries only a cap and sorts just
    under it; a ``custom`` strike (the Fed decision's cut/hold/hike) keeps the
    order the API listed it in, which is already cut-first.
    """
    floor = _num(market.get("floor_strike"))
    cap = _num(market.get("cap_strike"))
    stype = str(market.get("strike_type") or "").lower()
    if stype.startswith("less") and cap is not None:
        return cap - 1e-6
    if floor is not None:
        return floor
    if cap is not None:
        return cap - 1e-6
    return float(idx)


def normalize_kalshi(event: Mapping[str, Any], *, topic: str) -> Optional[dict[str, Any]]:
    """One Kalshi ``/events?with_nested_markets=true`` item, or None."""
    if not isinstance(event, Mapping):
        return None
    ticker = str(event.get("event_ticker") or "").strip()
    series = str(event.get("series_ticker") or "").strip()
    title = str(event.get("title") or "").strip()
    if not ticker or not title:
        return None

    markets = [m for m in (event.get("markets") or [])
               if isinstance(m, Mapping)
               and str(m.get("status") or "").lower() in _KALSHI_LIVE]
    outcomes: list[dict[str, Any]] = []
    closes: list[str] = []
    volume = volume_24h = open_interest = 0.0
    for idx, m in enumerate(markets):
        bid, ask = _prob(m.get("yes_bid_dollars")), _prob(m.get("yes_ask_dollars"))
        last, prev = _prob(m.get("last_price_dollars")), _prob(m.get("previous_price_dollars"))
        p = display_price(bid, ask, last)
        if p is None:
            continue
        label = str(m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or "").strip()
        if not label:
            continue
        outcomes.append({
            "label": label,
            "p": round(p, 4),
            "bid": bid,
            "ask": ask,
            "d1": round(last - prev, 4) if last is not None and prev is not None else None,
            "w1": None,
            "id": str(m.get("ticker") or ""),
            "order": _kalshi_order(m, idx),
        })
        volume += _num(m.get("volume_fp")) or 0.0
        volume_24h += _num(m.get("volume_24h_fp")) or 0.0
        open_interest += _num(m.get("open_interest_fp")) or 0.0
        ct = _iso(m.get("close_time"))
        if ct:
            closes.append(ct)
    if not outcomes:
        return None

    labels = [o["label"] for o in outcomes]
    numeric = _numeric_labels(labels)
    rules = markets[0].get("rules_primary") if markets else ""
    return {
        "key": f"ks:{ticker}",
        "platform": "kalshi",
        "id": ticker,
        "series": series,
        "title": title,
        "subtitle": str(event.get("sub_title") or "").strip(),
        "url": f"https://kalshi.com/markets/{series.lower()}/{ticker.lower()}" if series
               else f"https://kalshi.com/markets/{ticker.lower()}",
        "topic": topic,
        "kind": _kind(len(outcomes), exclusive=bool(event.get("mutually_exclusive")),
                      numeric=numeric),
        "numeric": numeric,
        "closes": min(closes) if closes else _iso(event.get("strike_date")),
        "volume": round(volume, 2),
        "volume_24h": round(volume_24h, 2),
        "liquidity": None,
        "open_interest": round(open_interest, 2),
        "outcomes": outcomes,
        "n_outcomes": len(outcomes),
        "rules": _clip(rules, RULES_MAX),
        "tags": [],
    }


# ── What a card shows ───────────────────────────────────────────────────────

def select_outcomes(event: Mapping[str, Any], limit: int = MAX_OUTCOMES) -> list[dict[str, Any]]:
    """The outcomes worth showing, in the order a reader expects them.

    * ``binary`` -- the one market.
    * ``exclusive`` -- the most likely *limit*; buckets ("6,800 to 6,999.99")
      then go back into strike order, names stay in probability order.
    * ``ladder`` -- the run of *limit* consecutive rungs centred where the odds
      cross 50%. A CPI ladder lists fourteen thresholds from -0.4% upward and
      the ones at 99¢ and 1¢ say nothing; the middle is the forecast.
    * ``multi`` -- independent named outcomes, most likely first.
    """
    rows = [o for o in (event.get("outcomes") or []) if o.get("p") is not None]
    if len(rows) <= 1:
        return list(rows)
    kind = event.get("kind")
    if kind == "ladder":
        rows.sort(key=lambda o: o.get("order", 0.0))
        if len(rows) <= limit:
            return rows
        mid = min(range(len(rows)), key=lambda i: abs(rows[i]["p"] - 0.5))
        start = max(0, min(mid - limit // 2, len(rows) - limit))
        return rows[start:start + limit]
    rows.sort(key=lambda o: o["p"], reverse=True)
    rows = rows[:limit]
    if kind == "exclusive" and event.get("numeric"):
        rows.sort(key=lambda o: o.get("order", 0.0))
    return rows


def trim(event: dict[str, Any], limit: int = MAX_OUTCOMES) -> dict[str, Any]:
    """*event* with its outcome list cut to :func:`select_outcomes`.

    ``n_outcomes`` keeps the full count, so the page can say "8 of 27" and the
    AI read knows its probabilities cover part of the field.
    """
    out = dict(event)
    # max(), so trimming an already-trimmed event (a venue's rows carried
    # forward from the last payload) cannot shrink the count it reports.
    out["n_outcomes"] = max(int(event.get("n_outcomes") or 0),
                            len(event.get("outcomes") or []))
    out["outcomes"] = select_outcomes(event, limit)
    return out


# ── The board ───────────────────────────────────────────────────────────────

#: Topics whose markets include rolling daily and weekly price ladders
#: ("Bitcoin above ___ on October 6?", "SPY closes above ___ on October 5?").
#: Those are the busiest contracts on either venue and say nothing about the
#: events this page is for, so a market here must run at least a week to be
#: listed -- which keeps "What price will Bitcoin hit in October?" and drops its
#: three daily cousins. Scheduled macro releases are exempt: a CPI market
#: closing tomorrow is the one worth seeing.
SHORT_DATED_TOPICS = frozenset({"equities", "commodities", "crypto"})
SHORT_DATED_MIN_HOURS = 7 * 24

#: A market this thin is a price nobody has tested. Either floor admits it.
MIN_VOLUME = 10_000.0
MIN_VOLUME_24H = 1_000.0

_UP_OR_DOWN = re.compile(r"\bup or down\b", re.I)


def on_board(event: Mapping[str, Any], now: datetime) -> bool:
    """Whether *event* belongs on the page at *now*."""
    if not event.get("outcomes"):
        return False
    if _UP_OR_DOWN.search(event.get("title") or ""):
        return False
    closes = parse_ts(event.get("closes"))
    if closes is not None:
        if closes <= now:
            return False
        if (event.get("topic") in SHORT_DATED_TOPICS
                and closes - now < timedelta(hours=SHORT_DATED_MIN_HOURS)):
            return False
    volume = event.get("volume") or 0.0
    volume_24h = event.get("volume_24h") or 0.0
    return volume >= MIN_VOLUME or volume_24h >= MIN_VOLUME_24H


def rank_board(events: Iterable[Mapping[str, Any]], *, per_topic: int = 15,
               total: int = 120) -> list[dict[str, Any]]:
    """Busiest first, at most *per_topic* from any one topic.

    The cap is what keeps crypto -- by far the most traded topic on Polymarket
    -- from filling the page on volume alone.
    """
    ordered = sorted(events, key=lambda e: e.get("volume_24h") or 0.0, reverse=True)
    counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for ev in ordered:
        topic = ev.get("topic") or ""
        if counts.get(topic, 0) >= per_topic:
            continue
        counts[topic] = counts.get(topic, 0) + 1
        out.append(dict(ev))
        if len(out) >= total:
            break
    return out


#: A move smaller than this in a day is ordinary churn.
MOVER_MIN_CHANGE = 0.03
#: ...and on a market this quiet a move is a trade or two. Measured on the
#: first live run: at $2k the list filled with Canada's monthly GDP and the
#: rial, each a few thousand dollars deep.
MOVER_MIN_VOLUME_24H = 5_000.0


def movers(events: Iterable[Mapping[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    """The events whose odds moved most in a day, one row per event.

    Each row is the event's single biggest one-day mover, so a ladder whose
    every rung shifted together is listed once rather than eight times.
    """
    rows: list[dict[str, Any]] = []
    for ev in events:
        if (ev.get("volume_24h") or 0.0) < MOVER_MIN_VOLUME_24H:
            continue
        best = None
        for o in ev.get("outcomes") or []:
            d1 = o.get("d1")
            if d1 is None or o.get("p") is None:
                continue
            if best is None or abs(d1) > abs(best["d1"]):
                best = o
        if best is None or abs(best["d1"]) < MOVER_MIN_CHANGE:
            continue
        rows.append({
            "key": ev.get("key"),
            "title": ev.get("title"),
            "platform": ev.get("platform"),
            "topic": ev.get("topic"),
            "url": ev.get("url"),
            "label": best["label"],
            "p": best["p"],
            "d1": best["d1"],
            "w1": best.get("w1"),
            "kind": ev.get("kind"),
        })
    rows.sort(key=lambda r: abs(r["d1"]), reverse=True)
    return rows[:limit]


# ── The Fed, three ways ─────────────────────────────────────────────────────
#
# /fedwatch backs FOMC odds out of the ZQ futures curve. Polymarket and Kalshi
# both list the same decisions as prediction markets. Comparing them needs one
# basis, and the obvious comparison is the wrong one: /fedwatch's grid is
# *cumulative* (the chance the range is still unchanged by December), while both
# venues price the decision *at* each meeting. December read off the grid as
# "14.6% no change" beside Polymarket's "23.5% no change" looks like a 9-point
# disagreement and is two different questions.
#
# The futures side is therefore restated per meeting from `change_bp`, the
# expected move at that meeting alone. /fedwatch's tree already splits exactly
# that between the two 25bp outcomes bracketing it and applies the split at
# every node -- so this is the per-meeting distribution the grid was built from,
# not a new model. It can only ever name two outcomes; the venues price the
# tails too, and showing both side by side is the point.

#: Buckets, in 25bp steps. ±2 is "50bp or more", which is how both venues list
#: the tails ("50+ bps decrease", "Cut >25bps").
FED_BUCKETS: tuple[int, ...] = (-2, -1, 0, 1, 2)

_PM_FED_TITLE = re.compile(r"^\s*fed decision in [a-z]+\??\s*$", re.I)
_KS_FED_SERIES = "KXFEDDECISION"
_KS_FED_SUFFIX = {"C26": -2, "C25": -1, "H0": 0, "H25": 1, "H26": 2}


def is_fed_decision(event: Mapping[str, Any]) -> bool:
    """Whether *event* is one FOMC meeting's decision, on either venue."""
    if event.get("platform") == "kalshi":
        return str(event.get("series") or "").upper() == _KS_FED_SERIES
    if event.get("platform") == "polymarket":
        return bool(_PM_FED_TITLE.match(event.get("title") or ""))
    return False


def fed_step(label: str, market_id: str = "") -> Optional[int]:
    """The 25bp bucket an outcome stands for, or None.

    Kalshi's ticker suffix is authoritative (``…-26OCT-H25``); otherwise the
    label is read: "No change" / "Fed maintains rate" is 0, "25 bps increase" /
    "Hike 25bps" is +1, "50+ bps decrease" / "Cut >25bps" is -2.
    """
    suffix = (market_id or "").rsplit("-", 1)[-1].upper()
    if suffix in _KS_FED_SUFFIX and "-" in (market_id or ""):
        return _KS_FED_SUFFIX[suffix]
    text = (label or "").strip().lower()
    if not text:
        return None
    if re.search(r"no change|maintain|unchanged|\bhold\b|\b0\s*bps?\b", text):
        return 0
    if re.search(r"decrease|\bcut\b|lower", text):
        sign = -1
    elif re.search(r"increase|\bhike\b|raise", text):
        sign = 1
    else:
        return None
    m = re.search(r"(>|\+)?\s*(\d+)\s*(\+)?\s*bps?", text)
    if not m:
        return None
    size = int(m.group(2))
    big = size >= 50 or bool(m.group(1)) or bool(m.group(3))
    return sign * (2 if big else 1)


def _normalise_buckets(raw: Mapping[int, float]) -> tuple[dict[int, float], float]:
    total = sum(raw.values())
    if total <= 0:
        return {}, 0.0
    return {b: raw.get(b, 0.0) / total for b in FED_BUCKETS}, total


def fed_buckets(event: Mapping[str, Any]) -> Optional[tuple[dict[int, float], float]]:
    """A venue's decision distribution for one meeting, summed to 1, plus the
    raw sum of its prices (the overround, shown so a normalised 82% is not
    mistaken for a quoted one). None when an outcome cannot be placed or a
    bucket is listed twice -- a distribution with a hole in it is not one."""
    raw: dict[int, float] = {}
    for o in event.get("outcomes") or []:
        step = fed_step(o.get("label") or "", o.get("id") or "")
        p = o.get("p")
        if step is None or p is None or step in raw:
            return None
        raw[step] = float(p)
    if 0 not in raw or len(raw) < 2:
        return None
    norm, total = _normalise_buckets(raw)
    return (norm, round(total, 4)) if norm else None


def futures_split(change_bp: float) -> dict[int, float]:
    """The futures' decision distribution at one meeting, from its expected move.

    Mirrors ``fedwatch._probability_tree``'s leg: +5bp is 80% hold / 20% +25bp,
    +20.4bp is 18.4% hold / 81.6% +25bp. Steps past ±2 fold into the ±2 bucket.
    """
    steps = float(change_bp) / 25.0
    low = math.floor(steps)
    frac = steps - low
    raw: dict[int, float] = {}

    def add(step: int, p: float) -> None:
        if p <= 1e-9:
            return
        bucket = max(-2, min(2, step))
        raw[bucket] = raw.get(bucket, 0.0) + p

    add(low, 1.0 - frac)
    add(low + 1, frac)
    return {b: round(raw.get(b, 0.0), 4) for b in FED_BUCKETS}


def expected_bp(buckets: Mapping[int, float]) -> float:
    """Expected move in basis points; the ±2 tails count as exactly ±50, so a
    venue's figure is a floor on the size of a tail move, never an invention."""
    return round(sum(p * step * 25 for step, p in buckets.items()), 1)


def _summary(buckets: Mapping[int, float]) -> dict[str, float]:
    return {
        "cut": round(sum(p for b, p in buckets.items() if b < 0), 4),
        "hold": round(buckets.get(0, 0.0), 4),
        "hike": round(sum(p for b, p in buckets.items() if b > 0), 4),
    }


def _meeting_day(closes: Optional[str]) -> Optional[date]:
    """The calendar day a venue's close falls on in New York, near enough.

    Polymarket closes its Fed markets at 23:59 ET (03:59Z the next day) and
    Kalshi at 13:59 ET; five hours back puts both on the decision's own date
    without a timezone database. :func:`fed_crosscheck` allows a day either way.
    """
    ts = parse_ts(closes)
    return (ts - timedelta(hours=5)).date() if ts else None


def fed_crosscheck(fedwatch_payload: Optional[Mapping[str, Any]],
                   fed_events: Iterable[Mapping[str, Any]], *,
                   max_meetings: int = 4) -> list[dict[str, Any]]:
    """Each upcoming FOMC decision as futures, Polymarket and Kalshi price it.

    One row per meeting that at least two of the three sources cover, nearest
    first. Each source carries its distribution over :data:`FED_BUCKETS` (as
    string keys, for JSON), cut/hold/hike totals and the expected move;
    ``spread_pp`` is the widest disagreement on any one bucket, in points.
    """
    meetings: dict[date, dict[str, Any]] = {}
    as_of = (fedwatch_payload or {}).get("as_of")
    for m in (fedwatch_payload or {}).get("meetings") or []:
        try:
            day = date.fromisoformat(str(m.get("date")))
        except ValueError:
            continue
        change = _num(m.get("change_bp"))
        if change is None:
            continue
        buckets = futures_split(change)
        meetings[day] = {"date": day.isoformat(), "label": m.get("label") or "",
                         "sources": [{
                             "source": "futures",
                             "buckets": {str(b): p for b, p in buckets.items()},
                             **_summary(buckets),
                             "expected_bp": round(change, 1),
                             "as_of": as_of,
                         }]}

    venue_rows: list[tuple[date, dict[str, Any]]] = []
    for ev in fed_events:
        if not is_fed_decision(ev):
            continue
        day = _meeting_day(ev.get("closes"))
        got = fed_buckets(ev)
        if day is None or got is None:
            continue
        buckets, total = got
        venue_rows.append((day, {
            "source": ev.get("platform"),
            "buckets": {str(b): round(p, 4) for b, p in buckets.items()},
            **_summary(buckets),
            "expected_bp": expected_bp(buckets),
            "overround": total,
            "volume": ev.get("volume"),
            "volume_24h": ev.get("volume_24h"),
            "url": ev.get("url"),
            "title": ev.get("title"),
        }))

    for day, row in venue_rows:
        target = None
        for known in meetings:
            if abs((known - day).days) <= 1:
                target = known
                break
        if target is None:
            target = day
            meetings[day] = {"date": day.isoformat(),
                             "label": day.strftime("%b %Y"), "sources": []}
        sources = meetings[target]["sources"]
        if any(s["source"] == row["source"] for s in sources):
            continue                       # one listing per venue per meeting
        sources.append(row)

    order = {"futures": 0, "polymarket": 1, "kalshi": 2}
    out: list[dict[str, Any]] = []
    for day in sorted(meetings):
        row = meetings[day]
        if len(row["sources"]) < 2:
            continue
        row["sources"].sort(key=lambda s: order.get(s["source"], 9))
        spread, worst = 0.0, "0"
        for b in FED_BUCKETS:
            vals = [s["buckets"].get(str(b), 0.0) for s in row["sources"]]
            gap = max(vals) - min(vals)
            if gap > spread + 1e-12:
                spread, worst = gap, str(b)
        modal = {s["source"]: max(FED_BUCKETS, key=lambda b: s["buckets"].get(str(b), 0.0))
                 for s in row["sources"]}
        row["spread_pp"] = round(spread * 100, 1)
        row["spread_bucket"] = worst
        row["agree"] = len(set(modal.values())) == 1
        out.append(row)
        if len(out) >= max_meetings:
            break
    return out


# ── The AI read: prompt ─────────────────────────────────────────────────────

def read_outcomes(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The outcomes an AI read covers: the ones the card shows, priced."""
    return [o for o in select_outcomes(event) if o.get("p") is not None]


def build_read_prompt(event: Mapping[str, Any], *, today: str) -> str:
    """The instruction for one AI read of one event.

    The market's prices are withheld from the model, deliberately. The first
    live read, shown the prices, answered "No change 82.5%" to a market at
    82.5% (the format example said 82.5 too -- an accident, since changed, and
    a second way to anchor it): a model given the price anchors on it, and a read that echoes the
    market has no gap to show and gives the ledger nothing to measure. So the
    model sees the question, the outcomes, the rules and the clock -- what a
    forecaster needs -- and none of the prices, volumes or recent moves, which
    are the market's own answer. The page sets the read beside the price
    afterwards, so the gap on screen is a real disagreement, and the ledger
    then says whether such disagreements were worth anything.

    The output is one JSON block carrying both languages, so a single grounded
    call serves the English and the Chinese page.
    """
    outcomes = read_outcomes(event)
    kind = event.get("kind")
    total = event.get("n_outcomes") or len(outcomes)
    partial = kind == "exclusive" and total > len(outcomes)
    closes = parse_ts(event.get("closes"))
    try:
        days = (closes.date() - date.fromisoformat(today)).days if closes else None
    except ValueError:
        days = None

    if kind == "binary":
        shape = "a single yes/no market. Give the probability that it resolves to the listed outcome."
    elif kind == "exclusive" and not partial:
        shape = ("mutually exclusive: exactly one outcome resolves Yes, so your "
                 "probabilities must sum to 100.")
    elif kind == "exclusive":
        shape = (f"mutually exclusive, and these {len(outcomes)} are a selection from "
                 f"{total} listed outcomes. Your probabilities for the listed ones must sum "
                 f"to at most 100; whatever is left belongs to the rest of the field.")
    elif kind == "ladder":
        shape = ("a ladder of separate yes/no thresholds. They need not sum to 100, but "
                 "they must be consistent: a harder threshold can never be more likely "
                 "than an easier one.")
    else:
        shape = "a set of separate yes/no markets; each is judged on its own."

    venue = "Polymarket" if event.get("platform") == "polymarket" else "Kalshi"
    lines = [f'{i}. "{o["label"]}"' for i, o in enumerate(outcomes, start=1)]

    title = event.get("title") or ""
    if event.get("subtitle"):
        title = f"{title} ({event['subtitle']})"
    close_txt = closes.strftime("%Y-%m-%d %H:%M UTC") if closes else "unknown"
    if days is not None:
        close_txt += f" ({days} days from today)"
    example = ", ".join(f'"{o["label"]}": <percent>' for o in outcomes[:3])

    return f"""You are a careful forecaster of prediction markets: calibrated, sceptical of narratives, explicit about base rates, and honest about uncertainty.

Today is {today}. Research the question below with Google Search, then give your own probability for every listed outcome.

MARKET
Venue: {venue}
Question: {title}
Type: {shape}
Closes: {close_txt}
Resolution rules (as published, may be truncated): {event.get("rules") or "not provided"}
Outcomes:
{chr(10).join(lines)}

HOW TO WORK
- Form your own estimate from evidence. The market's current prices are deliberately not given, and you should not look them up: your answer will be set beside them afterwards, and an estimate copied from the market tells the reader nothing.
- Start from the base rate for questions of this kind, then adjust for the latest hard evidence. Prefer primary sources: official data releases and calendars, central-bank statements, exchange notices, company filings. Note how recent your evidence is and ignore anything superseded.
- Read the resolution rules literally. If a scenario could resolve the market in a way a casual reader would not expect, say so.
- Do not give financial advice or trading instructions.

OUTPUT
Reply with exactly one fenced ```json block and nothing outside it. Use this shape, with the outcome names copied exactly and every listed outcome present. Probabilities are in percent from 0 to 100: 37.5 means 37.5%, and half of one percent is written 0.5.
```json
{{"probabilities": {{{example}}},
 "confidence": "low|medium|high",
 "evidence_date": "YYYY-MM-DD",
 "en": {{"summary": "at most three sentences, under 80 words", "drivers": ["up to four short points"], "watch": ["up to three things that would change this view"]}},
 "zh": {{"summary": "the same content in Simplified Chinese", "drivers": ["..."], "watch": ["..."]}}}}
```"""


# ── The AI read: parser ─────────────────────────────────────────────────────

class ReadParseError(ValueError):
    """The model's answer could not be turned into a forecast. Carries a short
    machine reason (``no_json``, ``bad_json``, ``missing``, ``sum``…)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.I)


def _extract_json(text: str) -> dict[str, Any]:
    m = _FENCE.search(text or "")
    blob = m.group(1) if m else None
    if blob is None:
        start, end = (text or "").find("{"), (text or "").rfind("}")
        if start < 0 or end <= start:
            raise ReadParseError("no_json")
        blob = text[start:end + 1]
    # A trailing comma before a closing bracket is the one JSON slip models
    # make often enough to be worth forgiving; anything else is refused.
    blob = re.sub(r",\s*([}\]])", r"\1", blob)
    try:
        out = json.loads(blob)
    except ValueError as exc:
        raise ReadParseError("bad_json", str(exc)) from exc
    if not isinstance(out, dict):
        raise ReadParseError("bad_json", "not an object")
    return out


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _str_list(value: Any, *, items: int, chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clip(v, chars) for v in value if isinstance(v, str) and v.strip()][:items]


def _side(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, Mapping) else {}
    return {
        "summary": _clip(value.get("summary") or "", 1000),
        "drivers": _str_list(value.get("drivers"), items=5, chars=240),
        "watch": _str_list(value.get("watch"), items=4, chars=240),
    }


def parse_read(text: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """The model's answer as ``{"ai": {label: p}, "confidence", "evidence_date",
    "en", "zh"}``, probabilities in [0, 1]. Raises :class:`ReadParseError`.

    Refuses rather than repairs anything that would change what the forecast
    says: a missing outcome, or an exclusive set summing far from 100. Keys
    naming no listed outcome are ignored. Within ±10 points an exclusive set is
    rescaled, because rounding to whole percents across five outcomes
    legitimately lands on 99 or 101.
    """
    data = _extract_json(text)
    raw = data.get("probabilities")
    if not isinstance(raw, Mapping) or not raw:
        raise ReadParseError("missing", "no probabilities")

    wanted = read_outcomes(event)
    by_norm = {_norm_label(k): v for k, v in raw.items()}
    values: dict[str, float] = {}
    missing: list[str] = []
    for o in wanted:
        v = _num(by_norm.get(_norm_label(o["label"])))
        if v is None:
            missing.append(o["label"])
            continue
        values[o["label"]] = v
    if missing:
        raise ReadParseError("missing", ", ".join(missing[:4]))

    # Percent is what was asked for, and it is what is read -- with one
    # exception that cannot be mistaken: a complete one-of-many set whose values
    # are all ≤ 1 and sum to about 1 was written as fractions. Anywhere else a
    # value of 0.5 is half a percent, which is exactly what a model writes for a
    # long shot; reading it as 50% would record a forecast nobody made.
    kind = event.get("kind")
    partial = kind == "exclusive" and (event.get("n_outcomes") or len(wanted)) > len(wanted)
    nums = list(values.values())
    as_fraction = (kind == "exclusive" and not partial
                   and all(0 <= v <= 1 for v in nums) and 0.90 <= sum(nums) <= 1.10)
    scale = 1.0 if as_fraction else 0.01
    probs = {k: v * scale for k, v in values.items()}
    if any(p < 0 or p > 1 for p in probs.values()):
        raise ReadParseError("range", "probability outside 0-100")

    total = sum(probs.values())
    if kind == "exclusive" and not partial:
        if not 0.90 <= total <= 1.10:
            raise ReadParseError("sum", f"exclusive outcomes sum to {total * 100:.0f}")
        probs = {k: p / total for k, p in probs.items()}
    elif kind == "exclusive" and total > 1.10:
        raise ReadParseError("sum", f"listed outcomes sum to {total * 100:.0f}")
    elif kind == "exclusive" and total > 1.0:
        probs = {k: p / total for k, p in probs.items()}

    confidence = str(data.get("confidence") or "").strip().lower()
    evidence = str(data.get("evidence_date") or "").strip()
    return {
        "ai": {k: round(p, 4) for k, p in probs.items()},
        "confidence": confidence if confidence in ("low", "medium", "high") else None,
        "evidence_date": evidence if re.match(r"^\d{4}-\d{2}-\d{2}$", evidence) else None,
        "en": _side(data.get("en")),
        "zh": _side(data.get("zh")),
    }


def gaps(ai: Mapping[str, float], market: Mapping[str, float]) -> dict[str, float]:
    """AI minus market for each outcome, in probability points (×100)."""
    return {k: round((ai[k] - market[k]) * 100, 1) for k in ai if k in market}


# ── Settling a read ─────────────────────────────────────────────────────────

def brier(probs: Mapping[str, float], results: Mapping[str, int]) -> Optional[float]:
    """Mean squared error of *probs* against 0/1 *results*, over the outcomes
    both name. Each outcome is its own binary market on both venues, so this is
    the same score for a ladder, a list or a one-of-many event -- and the AI
    and the market are always scored over exactly the same outcomes."""
    keys = [k for k in probs if k in results and results[k] in (0, 1)]
    if not keys:
        return None
    return round(sum((probs[k] - results[k]) ** 2 for k in keys) / len(keys), 6)


def polymarket_result(market: Mapping[str, Any]) -> Optional[Any]:
    """1 or 0 once a Gamma market has resolved, ``"void"`` if it closed
    without a clean answer (a 50/50 settlement), else None (still open)."""
    if not isinstance(market, Mapping) or not market.get("closed"):
        return None
    prices = [_num(p) for p in _json_list(market.get("outcomePrices"))]
    if not prices or prices[0] is None:
        return None
    if prices[0] >= 0.99:
        return 1
    if prices[0] <= 0.01:
        return 0
    status = str(market.get("umaResolutionStatus") or "").lower()
    if status == "resolved" or 0.4 <= prices[0] <= 0.6:
        return "void"
    return None


def kalshi_result(market: Mapping[str, Any]) -> Optional[Any]:
    """1 or 0 once a Kalshi market has a result, ``"void"`` if it settled to
    anything else, else None."""
    if not isinstance(market, Mapping):
        return None
    result = str(market.get("result") or "").strip().lower()
    if result == "yes":
        return 1
    if result == "no":
        return 0
    status = str(market.get("status") or "").lower()
    if result and status in ("settled", "finalized", "determined"):
        return "void"
    return None


def score_row(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Brier scores for a ledger row whose every outcome has resolved.

    None while any outcome is open. A void outcome is dropped from both scores
    alike; a row with nothing left to score is ``void`` as a whole.
    """
    outcomes = row.get("outcomes") or []
    results: dict[str, int] = {}
    for o in outcomes:
        r = o.get("result")
        if r is None:
            return None
        if r in (0, 1):
            results[o["label"]] = int(r)
    if not results:
        return {"status": "void", "brier_ai": None, "brier_market": None}
    ai = {o["label"]: o["ai_p"] for o in outcomes if o.get("ai_p") is not None}
    mk = {o["label"]: o["market_p"] for o in outcomes if o.get("market_p") is not None}
    b_ai, b_mk = brier(ai, results), brier(mk, results)
    if b_ai is None or b_mk is None:
        return {"status": "void", "brier_ai": None, "brier_market": None}
    return {"status": "settled", "brier_ai": b_ai, "brier_market": b_mk}


def ledger_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """What the record says so far: counts, mean Brier for the AI and for the
    market price it was compared with, and how often the AI scored better.

    Means are over settled rows only, and both are over the same rows, so the
    two numbers are always comparable. With nothing settled they are None --
    never 0, which would read as a perfect score.
    """
    recorded = settled = void = ai_better = market_better = ties = 0
    sum_ai = sum_mk = 0.0
    for row in rows:
        recorded += 1
        status = row.get("status")
        if status == "void":
            void += 1
            continue
        if status != "settled":
            continue
        b_ai, b_mk = _num(row.get("brier_ai")), _num(row.get("brier_market"))
        if b_ai is None or b_mk is None:
            continue
        settled += 1
        sum_ai += b_ai
        sum_mk += b_mk
        if b_ai < b_mk - 1e-9:
            ai_better += 1
        elif b_mk < b_ai - 1e-9:
            market_better += 1
        else:
            ties += 1
    return {
        "recorded": recorded,
        "settled": settled,
        "void": void,
        "open": recorded - settled - void,
        "brier_ai": round(sum_ai / settled, 4) if settled else None,
        "brier_market": round(sum_mk / settled, 4) if settled else None,
        "ai_better": ai_better,
        "market_better": market_better,
        "ties": ties,
    }
