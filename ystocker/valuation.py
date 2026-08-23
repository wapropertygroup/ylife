"""
ystocker.valuation
~~~~~~~~~~~~~~~~~~
Index P/E multiples: a real long-history trailing P/E for the S&P 500, plus a
genuine cap-weighted *forward* P/E for SPY and QQQ that accumulates daily.

Why it is built this way
------------------------
A historical forward P/E series for SPY or QQQ is not obtainable from free
sources, and pretending otherwise is the trap here:

* Yahoo returns ``forwardPE``, ``forwardEps`` and ``trailingEps`` as ``None``
  for both ETFs. Only ``trailingPE`` comes back. So the ``price / forwardEps``
  construction used per-ticker in routes.py cannot even run for SPY/QQQ.
* That construction is misleading anyway. Holding consensus EPS constant makes
  the resulting "P/E history" mathematically identical to the price chart, so
  it implies earnings expectations never moved — 2020 prices divided by 2026
  earnings produce a badly wrong historical multiple.

So this module does three honest things instead, and never mixes them:

1. **Trailing P/E and EPS for the S&P 500** from multpl.com — monthly back to
   1871, built on actual reported earnings, so the shape is real history rather
   than a rescaled price line. Clearly labelled *trailing*.

2. **Consensus forward P/E for SPY and QQQ**, computed bottom-up from
   constituents each day and appended to a persisted series. Truly
   forward-looking and truly per-ETF, but it necessarily starts on the first
   snapshot and fills in at one point per day. Shipped as ``forward`` /
   ``forward_history``, headlined as ``{etf}_forward_pe``.

3. **Realized forward P/E for the S&P 500** — index level over the earnings
   that actually arrived in the following twelve months (see
   :func:`_realized_forward_pe`). Shipped as ``fwd_realized``, headlined as
   ``spx_fwd_realized``.

4. **Published consensus forward P/E history** for the S&P 500 and the
   Nasdaq-100, from :mod:`ystocker.consensus_pe` — FactSet's stated forward
   12-month P/E back to 2017 and Siblis' month-end NDX figures. This is the
   history (2) cannot have, on someone else's basis rather than ours. Shipped as
   ``spx_consensus_fwd`` / ``ndx_consensus_fwd`` and deliberately *not* merged
   into ``forward_history``: see that module for why splicing them would invent
   moves that never happened.

(2) and (3) are both "forward P/E" and they are *not* comparable, which is the
single easiest thing to get wrong on the page that consumes this. (2) divides
by analyst estimates and is dated today; (3) divides by realized earnings and
necessarily stops twelve months short of today, so its latest point carries a
year-old index level. In Aug 2026 they read 19.8x and 24.5x respectively — the
same index, neither figure wrong. Every label that surfaces one of these must
say which basis it is on and as of when, or the two get read as a contradiction.
(4) is a third basis again: same question as (2), different answerer.

Aggregation
-----------
A cap-weighted index P/E is total market value over total earnings, which for
per-name P/Es is the cap-weighted *harmonic* mean:

    index P/E = Σ(mktCap_i) / Σ(mktCap_i / PE_i)

Averaging P/Es arithmetically would overweight expensive names and is simply
the wrong statistic. Market caps supply the weights, so no index weight file is
needed — which is why this does not scrape holdings. Names with a missing or
non-positive forward P/E are excluded, and what the aggregate actually covered
is reported two ways: ``coverage_pct``, a share of the constituent *count*, and
``market_cap_b``, the absolute market cap that went in. Coverage is deliberately
*not* expressed as a percentage of index market cap, because the caps of the
excluded names are precisely what is unavailable — a ratio over the names we do
have reads a meaningless 100% (that was the original bug; see
``_MIN_CONSTITUENTS``). The count and the dollar figure together are the honest
statement, and they can differ a lot: 120 of 503 names is 24% by count but
~$50T of cap, because the misses are overwhelmingly small caps.

Cost: no per-constituent network calls at all. Fetching ``Ticker.info`` for
500+ names got this machine hard-blocked by Yahoo during development (a first
run returned 500/503; later runs returned 0/503 with HTTP 401 "Invalid Crumb"),
and doing that daily would throttle every other Yahoo-dependent feature in the
app. The multiple is therefore computed from ``cache/ticker_cache.json``, which
the rolling refresher already maintains.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).parent.parent / "cache" / "valuation_cache.json"
_CACHE_TTL = 24 * 60 * 60  # multpl updates daily; constituents once a day is plenty
# Payload schema version. Bump whenever a key is added, removed or renamed.
# A TTL alone does not protect against a shape change: after a deploy that adds
# a field, an existing cache still looks fresh, so the API happily serves a
# payload the new page cannot read and charts render empty with no explanation.
# Same idea as _YIELD_CURVE_CACHE_VER in routes.py.
_CACHE_VER = "v3"

# Browser UA: multpl.com serves a plain client fine, but its CDN is happier
# with a normal UA and this host is not FRED (see fed.py for why FRED differs).
_MULTPL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

MULTPL_SERIES: dict[str, dict[str, str]] = {
    "spx_pe":  {"url": "https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
                "label": "S&P 500 Trailing P/E", "unit": "x"},
    # Nominal index level. Needed to derive nominal EPS, and never shipped —
    # the page plots multiples, not the index.
    "spx_price": {"url": "https://www.multpl.com/s-p-500-historical-prices/table/by-month",
                  "label": "S&P 500 Price", "unit": "usd", "ship": False},
    # Shiller CAPE: price over the 10-year inflation-adjusted earnings average.
    # Worth carrying alongside the plain trailing P/E because trailing P/E goes
    # useless exactly when it matters most — in 2009 collapsing earnings sent it
    # above 120x, implying record expensiveness at the century's best entry
    # point. CAPE's smoothed denominator does not invert like that.
    "spx_cape": {"url": "https://www.multpl.com/shiller-pe/table/by-month",
                 "label": "S&P 500 Shiller CAPE", "unit": "x"},
}

# Nasdaq-100 constituents. NDX reconstitutes annually (announced mid-December,
# effective the third Friday), so this list needs a yearly look. A stale name
# only drops that holding from the aggregate and is reported in `missing`
# rather than producing a silently wrong multiple — ANSS and WBA were removed
# after Yahoo started 404ing them (acquired / taken private).
#
# Names with negative forward estimates (an unprofitable MRNA, say) are also
# excluded by the aggregation: they have no meaningful P/E, and including a
# negative one would drag the index multiple toward nonsense. That is the
# standard treatment for an index P/E, not a data gap.
NDX100: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "AVGO", "META", "GOOGL", "GOOG", "TSLA", "COST",
    "NFLX", "AMD", "PEP", "ADBE", "LIN", "TMUS", "CSCO", "QCOM", "INTU", "TXN",
    "AMGN", "ISRG", "AMAT", "BKNG", "CMCSA", "HON", "VRTX", "PANW", "ADP", "GILD",
    "MU", "ADI", "LRCX", "REGN", "MELI", "SBUX", "KLAC", "SNPS", "CDNS", "CRWD",
    "MAR", "CTAS", "ORLY", "CSX", "ASML", "ABNB", "PYPL", "MNST", "WDAY", "FTNT",
    "ADSK", "NXPI", "PCAR", "ROP", "CPRT", "MRVL", "AEP", "DASH", "CHTR", "PAYX",
    "ODFL", "MCHP", "KDP", "AZN", "FAST", "EXC", "IDXX", "CTSH", "VRSK", "EA",
    "GEHC", "CCEP", "XEL", "DDOG", "TTD", "LULU", "KHC", "CSGP", "ZS",
    "TEAM", "ON", "DXCM", "FANG", "BIIB", "GFS", "CDW", "WBD", "ILMN", "MDB",
    "MRNA", "SIRI", "SMCI", "ARM", "PDD", "BKR", "TTWO", "ALGN",
)

# Which ETF maps to which constituent universe.
INDEX_UNIVERSE: dict[str, dict[str, Any]] = {
    "SPY": {"label": "SPY (S&P 500)",    "source": "sp500"},
    "QQQ": {"label": "QQQ (Nasdaq-100)", "source": "ndx100"},
}

# Sanity band for an index-level forward P/E. Anything outside this is a data
# problem, not a market event, and must not be charted or snapshotted.
_PE_MIN, _PE_MAX = 5.0, 80.0
# Minimum number of constituents before an aggregate is trustworthy enough to
# record. Guarding on share-of-matched-market-cap instead was the original bug:
# names that fail leave the denominator as well as the numerator, so coverage
# read a perfect 100% while only 195 of 503 names had actually been priced.
_MIN_CONSTITUENTS = 40

_SESSION = requests.Session()
_SESSION.trust_env = False  # system proxies cause silent timeouts
_SESSION.headers.update({"User-Agent": _MULTPL_UA, "Accept": "text/html,*/*"})


# ---------------------------------------------------------------------------
# multpl.com — real trailing history
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

_TABLE_RE = re.compile(r'<table[^>]*id="datatable".*?</table>', re.S)
_ROW_RE = re.compile(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", re.S)


def _clean_cell(raw: str) -> str:
    """Strip tags, HTML entities and multpl's estimate dagger from a cell.

    Commas are deliberately preserved: a comma separates "Aug 14, 2026" as well
    as acting as a thousands separator in values, and stripping it here silently
    broke every date match and yielded zero rows. Numeric parsing drops commas
    at the point of use instead.
    """
    txt = re.sub(r"<[^>]+>", "", raw)
    # Rows carry &#x2002; (en-space) or a † marking an estimate.
    txt = txt.replace("&#x2002;", " ").replace(" ", " ").replace("†", " ")
    return txt.strip()


def _fetch_multpl(key: str, meta: dict[str, str]) -> Optional[dict[str, Any]]:
    """Scrape one multpl.com monthly table into {dates, values}.

    Returns dates as ``YYYY-MM-DD`` ascending. The newest row is often an
    as-of-today estimate rather than a month end; that is kept, because it is
    the current reading the page headlines.
    """
    try:
        resp = _SESSION.get(meta["url"], timeout=40)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Valuation: multpl %s fetch failed: %s", key, exc)
        return None

    table = _TABLE_RE.search(resp.text)
    if not table:
        log.warning("Valuation: multpl %s — datatable not found (markup changed?)", key)
        return None

    points: list[tuple[str, float]] = []
    for raw_date, raw_val in _ROW_RE.findall(table.group(0)):
        d = _clean_cell(raw_date)
        v = _clean_cell(raw_val)
        m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", d)
        if not m or not v:
            continue
        try:
            iso = f"{int(m.group(3)):04d}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
            points.append((iso, float(v.replace(",", ""))))
        except (ValueError, KeyError):
            continue

    if len(points) < 100:
        log.warning("Valuation: multpl %s only yielded %d rows — treating as failure",
                    key, len(points))
        return None

    points.sort(key=lambda p: p[0])
    log.info("Valuation: multpl %s — %d rows (%s … %s), latest %.2f",
             key, len(points), points[0][0], points[-1][0], points[-1][1])
    return {
        "dates":  [p[0] for p in points],
        "values": [p[1] for p in points],
        "label":  meta["label"],
        "unit":   meta["unit"],
        "source": "multpl.com",
    }


# ---------------------------------------------------------------------------
# Derived from multpl's nominal series
# ---------------------------------------------------------------------------

def _by_month(block: Optional[dict[str, Any]]) -> dict[str, float]:
    if not block:
        return {}
    return {d[:7]: v for d, v in zip(block["dates"], block["values"])}


def _plus_12m(month: str) -> str:
    return f"{int(month[:4]) + 1:04d}-{month[5:7]}"


def _nominal_eps(multpl: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Nominal trailing EPS, derived as index level / P/E.

    multpl publishes earnings **inflation-adjusted to current dollars** while its
    price and P/E tables are nominal. Charting that earnings table as "trailing
    EPS" overstates history badly — 1871 reads $10.72 instead of $0.40 — and
    dividing a nominal price by a real EPS produces a meaningless ratio. Since
    P/E and price are both nominal, their quotient recovers nominal EPS exactly:
    verified at 0.40 (1871-01), 1.53 (1929-08), 2.34 (1950-01), 50.94 (2000-03)
    and 261.68 (2026-03), all matching the published record.
    """
    price, pe = _by_month(multpl.get("spx_price")), _by_month(multpl.get("spx_pe"))
    months = sorted(m for m in price if m in pe and pe[m])
    if len(months) < 100:
        return None
    return {
        "dates": [f"{m}-01" for m in months],
        "values": [round(price[m] / pe[m], 2) for m in months],
        "label": "S&P 500 Trailing EPS (as reported, nominal)",
        "unit": "usd", "source": "multpl.com (derived)",
    }


def _realized_forward_pe(multpl: dict[str, Any],
                         eps_nominal: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Forward P/E on the earnings that actually arrived.

    This is the one forward-P/E *time series* that free data supports. For each
    month it divides the index level by the trailing EPS twelve months later —
    which is precisely the earnings of the following twelve months:

        forward P/E(t) = price(t) / EPS(t + 12m)

    It is real history, not a rescaled price line, because the denominator moves
    independently. What it is *not* is the consensus forward P/E of the day: it
    uses hindsight, so it answers "what multiple did buyers actually pay for the
    earnings they got" rather than "what did analysts expect". June 2008 reads
    178x for exactly that reason — nobody forecast the collapse that followed.
    The series necessarily stops twelve months short of today, since the next
    year of earnings is not yet known; today's expectations live in the
    consensus SPY/QQQ figures instead.
    """
    price = _by_month(multpl.get("spx_price"))
    eps = _by_month(eps_nominal)
    months = [m for m in sorted(price)
              if _plus_12m(m) in eps and eps[_plus_12m(m)] > 0]
    if len(months) < 100:
        return None
    values = [round(price[m] / eps[_plus_12m(m)], 2) for m in months]
    ranked = sorted(values)
    log.info("Valuation: realized forward P/E — %d months (%s … %s), latest %.1fx, "
             "median %.1fx", len(months), months[0], months[-1], values[-1],
             ranked[len(ranked) // 2])
    return {
        "dates": [f"{m}-01" for m in months],
        "values": values,
        "label": "S&P 500 Forward P/E (realized earnings)",
        "unit": "x", "source": "multpl.com (derived)",
        "median": round(ranked[len(ranked) // 2], 2),
        "percentile": round(sum(1 for v in ranked if v <= values[-1]) / len(ranked) * 100, 1),
    }


# ---------------------------------------------------------------------------
# Bottom-up forward P/E
# ---------------------------------------------------------------------------

def _constituents(source: str) -> tuple[str, ...]:
    if source == "ndx100":
        # dict.fromkeys dedupes while preserving order (the static list can
        # briefly contain a duplicate across a reconstitution edit).
        return tuple(dict.fromkeys(NDX100))
    from ystocker.breadth import SP500_UNIVERSE
    return tuple(dict.fromkeys(SP500_UNIVERSE))


def _cached_fundamentals() -> dict[str, dict[str, Any]]:
    """Ticker -> record from the app's own ticker_cache.json.

    The cache is populated by the rolling refresher in routes.py on a schedule
    Yahoo tolerates, and already carries "PE (Forward)" and "Market Cap ($B)".
    Reading it costs zero extra requests.
    """
    path = Path(__file__).parent.parent / "cache" / "ticker_cache.json"
    try:
        blob = json.loads(path.read_text())
    except Exception as exc:
        log.warning("Valuation: could not read ticker cache: %s", exc)
        return {}

    out: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if isinstance(val, dict) and "PE (Forward)" in val:
                    out[key] = val
                elif isinstance(val, (dict, list)):
                    walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)

    walk(blob.get("data", {}))
    return out


def _fetch_forward_pe(etf: str, meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Cap-weighted forward P/E for one ETF, from cached constituent fundamentals.

    Deliberately does NOT call Yahoo per constituent. Fetching ``.info`` for
    500+ names got this machine hard-blocked mid-development: a first run
    returned 500/503, later runs returned 0/503 with HTTP 401 "Invalid Crumb".
    Doing that daily in production would throttle the box for *every* other
    Yahoo-dependent feature — quotes, breadth, the heatmap — so the multiple is
    computed from fundamentals the app has already fetched instead.

    The trade-off is coverage: only index members the app happens to track are
    included, so this is an estimate over the largest holdings rather than the
    full index. Measured against a clean full-universe run it came out ~3% off
    (SPY 20.05 vs 19.49 over 120/503 names; QQQ 20.81 vs 21.44 over 45/97), so
    the level is representative but not exact — which is why every consumer
    reports ``constituents_used`` and the page labels it an estimate.
    """
    universe = _constituents(meta["source"])
    if not universe:
        return None
    recs = _cached_fundamentals()
    if not recs:
        return None

    cap_used = 0.0
    earnings_used = 0.0
    used = 0
    missing: list[str] = []

    for sym in universe:
        rec = recs.get(sym)
        if not rec:
            missing.append(sym)
            continue
        pe = rec.get("PE (Forward)")
        cap = rec.get("Market Cap ($B)")
        if not isinstance(pe, (int, float)) or pe <= 0:
            continue
        if not isinstance(cap, (int, float)) or cap <= 0:
            continue
        cap_used += cap
        earnings_used += cap / pe
        used += 1

    if earnings_used <= 0:
        log.warning("Valuation: %s — no usable cached constituent earnings", etf)
        return None

    # Cap-weighted harmonic mean: total market value / total forward earnings.
    fwd_pe = cap_used / earnings_used
    coverage = used / len(universe) * 100.0

    if not (_PE_MIN <= fwd_pe <= _PE_MAX):
        log.warning("Valuation: %s forward P/E %.2f outside sanity band %.0f-%.0f — "
                    "discarding rather than charting it", etf, fwd_pe, _PE_MIN, _PE_MAX)
        return None
    if used < _MIN_CONSTITUENTS:
        log.warning("Valuation: %s only %d cached constituents (min %d) — discarding",
                    etf, used, _MIN_CONSTITUENTS)
        return None

    log.info("Valuation: %s forward P/E %.2f from %d/%d constituents (%.0f%% by count, "
             "$%.0fB cap)", etf, fwd_pe, used, len(universe), coverage, cap_used)
    return {
        "etf": etf,
        "label": meta["label"],
        "forward_pe": round(fwd_pe, 2),
        "coverage_pct": round(coverage, 1),
        "constituents_used": used,
        "constituents_total": len(universe),
        "market_cap_b": round(cap_used, 0),
    }


# ---------------------------------------------------------------------------
# Snapshot history — the accumulating forward series
# ---------------------------------------------------------------------------

def _merge_snapshots(previous: list[dict[str, Any]],
                     today: dict[str, float]) -> list[dict[str, Any]]:
    """Append today's forward P/Es to the persisted series.

    This is accumulated state, not derived data: it is the only record of what
    forward multiples were on past days, so a rebuild must merge into it rather
    than replace it. One row per calendar day; a same-day refresh overwrites
    that row instead of adding a duplicate.
    """
    stamp = date.today().isoformat()
    merged = {row["date"]: dict(row) for row in previous if row.get("date")}
    row = merged.get(stamp, {"date": stamp})
    row.update(today)
    merged[stamp] = row
    return [merged[d] for d in sorted(merged)]


# The series is observed data, not derived data: it is the only record of what
# forward multiples were on past days and it cannot be recomputed from anything.
# Keeping it solely in the cache file was the flaw -- when the EC2 instance was
# replaced the file went with it and the chart reset to a single point, which is
# exactly what a reader kept seeing. It now also goes to DynamoDB, following the
# same pattern the fear-greed and put/call series already use here, so it
# survives instance replacement, a cache-schema bump and a disk wipe alike.
_HIST_TABLE_NAME = "ystocker-valuation-history"
_hist_table = None
_hist_unavail_until = 0.0
_HIST_LOCK = threading.Lock()


def _get_hist_table():
    """DynamoDB table for the snapshot series, or None when unavailable.

    Absence is not an error: local dev has no AWS credentials, and the disk copy
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

            ddb = boto3.resource("dynamodb",
                                 region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(_HIST_TABLE_NAME)
            tbl.load()
            _hist_table = tbl
            log.info("Valuation: DynamoDB history table connected: %s", _HIST_TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - degrade to disk-only
            log.warning("Valuation: DynamoDB history unavailable: %s", exc)
            _hist_table = None
            _hist_unavail_until = time.time() + 300
        return _hist_table


def _hist_load_rows() -> list[dict[str, Any]]:
    """Every stored snapshot row from DynamoDB, oldest first."""
    table = _get_hist_table()
    if table is None:
        return []
    try:
        rows: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                stamp = item.get("date")
                if not stamp:
                    continue
                row: dict[str, Any] = {"date": stamp}
                for etf in INDEX_UNIVERSE:
                    val = item.get(etf)
                    if val is None:
                        continue
                    try:
                        row[etf] = float(val)
                    except (TypeError, ValueError):
                        continue
                rows.append(row)
            # A scan is paginated; without this the series silently stops at the
            # first page once there is more than 1 MB of history.
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        rows.sort(key=lambda r: r["date"])
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("Valuation: DynamoDB history load failed: %s", exc)
        return []


def _hist_save_row(row: dict[str, Any]) -> None:
    """Persist one day's row. Values go as strings, since DynamoDB rejects float."""
    table = _get_hist_table()
    if table is None or not row.get("date"):
        return
    try:
        item = {"date": row["date"]}
        for etf in INDEX_UNIVERSE:
            val = row.get(etf)
            if val is not None:
                item[etf] = str(round(float(val), 2))
        if len(item) > 1:
            table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        log.warning("Valuation: DynamoDB history save failed for %s: %s",
                    row.get("date"), exc)


def _previous_snapshots() -> list[dict[str, Any]]:
    """The snapshot series so far, from DynamoDB and disk together.

    Both are read and unioned by date rather than one being preferred: the disk
    copy can hold a row written while DynamoDB was briefly unreachable, and
    DynamoDB holds everything that predates the current instance. Reading the
    file deliberately bypasses the TTL and the schema version -- an expired or
    stale payload still holds real observations, and discarding them would reset
    the series to a single point on every rebuild.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in _hist_load_rows():
        if row.get("date"):
            merged[row["date"]] = dict(row)
    try:
        if _CACHE_FILE.exists():
            payload = json.loads(_CACHE_FILE.read_text())
            hist = payload.get("forward_history")
            if isinstance(hist, list):
                for row in hist:
                    stamp = row.get("date")
                    if not stamp:
                        continue
                    merged.setdefault(stamp, {}).update(row)
    except Exception as exc:  # noqa: BLE001
        log.warning("Valuation: could not read prior snapshots: %s", exc)
    return [merged[d] for d in sorted(merged)]


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

def _build_payload() -> dict[str, Any]:
    import concurrent.futures as cf

    from ystocker import consensus_pe

    multpl: dict[str, Any] = {}
    forward: dict[str, Any] = {}
    consensus: dict[str, Any] = {}

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        # Published consensus history is independent of multpl, so it overlaps
        # with those fetches instead of adding its own latency.
        c_fut = pool.submit(consensus_pe.get_consensus_series)
        m_futs = {pool.submit(_fetch_multpl, k, v): k for k, v in MULTPL_SERIES.items()}
        for fut in cf.as_completed(list(m_futs)):
            key = m_futs[fut]
            try:
                data = fut.result()
            except Exception as exc:
                log.warning("Valuation: multpl %s raised: %s", key, exc)
                continue
            if data:
                multpl[key] = data
        try:
            consensus = {k: v for k, v in c_fut.result().items() if v}
        except Exception as exc:  # noqa: BLE001 - the page survives without it
            log.warning("Valuation: consensus history raised: %s", exc)

    # Sequential: each ETF already fans out to 8 workers internally.
    for etf, meta in INDEX_UNIVERSE.items():
        try:
            got = _fetch_forward_pe(etf, meta)
        except Exception as exc:
            log.warning("Valuation: %s raised: %s", etf, exc)
            got = None
        if got:
            forward[etf] = got

    if not multpl and not forward and not consensus:
        log.error("Valuation: every source failed")
        return {"_ts": time.time(), "error": "valuation data unavailable"}

    eps_nominal = _nominal_eps(multpl)
    fwd_realized = _realized_forward_pe(multpl, eps_nominal)

    today = {etf: blk["forward_pe"] for etf, blk in forward.items()}
    history = _merge_snapshots(_previous_snapshots(), today) if today \
        else _previous_snapshots()
    if today and history:
        # Write the row through to durable storage now, not at shutdown: the
        # process is a web worker that may be recycled at any point.
        _hist_save_row(history[-1])

    spx_pe = multpl.get("spx_pe")
    headline: dict[str, Any] = {}
    if spx_pe:
        vals = spx_pe["values"]
        headline["spx_trailing_pe"] = {
            "value": vals[-1],
            "yoy": round(vals[-1] - vals[-13], 2) if len(vals) > 13 else None,
            "unit": "x", "source": "multpl.com",
            "as_of": spx_pe["dates"][-1],
        }
        # Where today's multiple sits in its own recorded history is the only
        # thing that makes a P/E level interpretable.
        ranked = sorted(vals)
        pos = sum(1 for v in ranked if v <= vals[-1]) / len(ranked) * 100
        headline["spx_pe_percentile"] = {
            "value": round(pos, 1), "unit": "pct_rank",
            "source": "multpl.com", "as_of": spx_pe["dates"][-1],
            "since": spx_pe["dates"][0],
        }
    cape = multpl.get("spx_cape")
    if cape:
        vals = cape["values"]
        ranked = sorted(vals)
        headline["spx_cape"] = {
            "value": vals[-1],
            "yoy": round(vals[-1] - vals[-13], 2) if len(vals) > 13 else None,
            "unit": "x", "source": "multpl.com", "as_of": cape["dates"][-1],
        }
        headline["spx_cape_percentile"] = {
            "value": round(sum(1 for v in ranked if v <= vals[-1]) / len(ranked) * 100, 1),
            "unit": "pct_rank", "source": "multpl.com",
            "as_of": cape["dates"][-1], "since": cape["dates"][0],
        }

    spx_eps = eps_nominal
    if spx_eps:
        vals = spx_eps["values"]
        headline["spx_trailing_eps"] = {
            "value": vals[-1],
            "yoy": round((vals[-1] / vals[-13] - 1) * 100, 1) if len(vals) > 13 and vals[-13] else None,
            "unit": "usd", "source": "multpl.com",
            "as_of": spx_eps["dates"][-1],
        }
    if fwd_realized:
        headline["spx_fwd_realized"] = {
            "value": fwd_realized["values"][-1], "yoy": None, "unit": "x",
            "source": "multpl.com (derived)", "as_of": fwd_realized["dates"][-1],
            "median": fwd_realized["median"],
        }

    for etf, blk in forward.items():
        headline[f"{etf.lower()}_forward_pe"] = {
            "value": blk["forward_pe"], "yoy": None, "unit": "x",
            "source": "computed", "as_of": date.today().isoformat(),
            "coverage_pct": blk["coverage_pct"],
            "constituents": f"{blk['constituents_used']}/{blk['constituents_total']}",
            "market_cap_b": blk.get("market_cap_b"),
        }

    log.info("Valuation: payload built — %d multpl series, %d forward P/Es, "
             "%d snapshot days, %d consensus series", len(multpl), len(forward),
             len(history), len(consensus))
    return {
        "_ts": time.time(),
        "_ver": _CACHE_VER,
        "as_of": date.today().isoformat(),
        "headline": headline,
        # spx_price is a derivation input only (nominal EPS and the realized
        # forward P/E); the page plots multiples, so it never goes over the wire.
        "multpl": {k: v for k, v in multpl.items()
                   if MULTPL_SERIES.get(k, {}).get("ship", True)},
        "forward": forward,
        "forward_history": history,
        "eps_nominal": eps_nominal,
        "fwd_realized": fwd_realized,
        # Published consensus history. Separate keys, never folded into
        # forward_history — different aggregators on different bases.
        **consensus,
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


def _has_content(payload: Optional[dict[str, Any]]) -> bool:
    return bool(payload and (payload.get("multpl") or payload.get("forward")
                             or payload.get("spx_consensus_fwd")))


def _load_disk_cache() -> Optional[dict[str, Any]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        if payload.get("_ver") != _CACHE_VER:
            log.info("%s: cache schema %s != %s — rebuilding",
                     __name__, payload.get("_ver"), _CACHE_VER)
            return None
        if time.time() - payload.get("_ts", 0) >= _CACHE_TTL:
            return None
        if not _has_content(payload):
            return None
        return payload
    except Exception as exc:
        log.warning("Valuation: failed to read disk cache: %s", exc)
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
        log.warning("Valuation: failed to write disk cache: %s", exc)


def get_valuation_data(force: bool = False) -> dict[str, Any]:
    """Return cached valuation data. memory -> disk -> network."""
    global _cache_data, _cache_ts

    with _cache_lock:
        if not force and _cache_data and _cache_ts and (time.time() - _cache_ts) < _CACHE_TTL:
            return _cache_data

    if not force:
        disk = _load_disk_cache()
        if disk:
            with _cache_lock:
                _cache_data = disk
                _cache_ts = disk.get("_ts", time.time())
            return disk

    if not force and _fetch_in_progress.is_set():
        log.info("Valuation: fetch in progress — returning stale/warming payload")
        with _cache_lock:
            if _cache_data:
                return _cache_data
        return {"_warming": True, "_ts": None, "multpl": {}, "forward": {}}

    _fetch_in_progress.set()
    try:
        log.info("Valuation: rebuilding (multpl history + constituent forward P/Es)")
        fresh = _build_payload()
        if _has_content(fresh):
            with _cache_lock:
                _cache_data = fresh
                _cache_ts = fresh["_ts"]
            _save_disk_cache(fresh)
        else:
            with _cache_lock:
                if _cache_data:
                    log.warning("Valuation: build failed — keeping previous cache")
                    return _cache_data
        return fresh
    finally:
        _fetch_in_progress.clear()


def get_cache_ts() -> Optional[float]:
    """Timestamp of the payload we hold, fresh or not.

    Not freshness-filtered on purpose — it is the honest "data as of" value.
    Never branch page layout on it alone; pair it with :func:`is_cache_fresh`,
    or a worker holding an expired payload renders a confident header above
    empty charts (the bug fixed in 77d41db).
    """
    with _cache_lock:
        if _cache_ts:
            return _cache_ts
    disk = _load_disk_cache()
    return disk.get("_ts") if disk else None


def is_cache_fresh() -> bool:
    with _cache_lock:
        data = _cache_data if _cache_data else _load_disk_cache()
    if not data:
        return False
    if data.get("_ver") != _CACHE_VER:
        return False
    ts = data.get("_ts")
    if not ts or (time.time() - ts) >= _CACHE_TTL:
        return False
    return _has_content(data)


def is_warming() -> bool:
    with _warming_lock:
        return _warming


def refresh_cache() -> None:
    global _warming
    with _warming_lock:
        _warming = True
    try:
        get_valuation_data(force=True)
    finally:
        with _warming_lock:
            _warming = False


def start_background_thread() -> None:
    """Warm on startup, then refresh near expiry.

    Under gunicorn --preload this runs only in the master (see CLAUDE.md), so
    the ~600 constituent lookups happen once per deploy rather than per worker.
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
                log.info("Valuation background: memory cache warmed from disk "
                         "(%d snapshot days)", len(disk.get("forward_history", [])))
            else:
                log.info("Valuation background: no disk cache — building now")
                with warmup.cold_build('valuation'):
                    refresh_cache()
        except Exception as exc:
            log.warning("Valuation background: startup warm failed: %s", exc)

        while True:
            # Sleep until the payload we hold expires, not a full TTL from
            # startup: a deploy restarts this thread, so a fixed sleep meant
            # frequent deploys could stop the refresh ever firing (77d41db).
            with _cache_lock:
                ts = _cache_ts
            age = (time.time() - ts) if ts else _CACHE_TTL
            sleep_for = max(300.0, _CACHE_TTL - age)
            log.info("Valuation background: next refresh in %.0f min (cache age %.0f min)",
                     sleep_for / 60, age / 60)
            time.sleep(sleep_for)
            try:
                log.info("Valuation background: refreshing")
                with warmup.cold_build('valuation'):
                    refresh_cache()
            except Exception as exc:
                log.warning("Valuation background: refresh failed: %s", exc)

    threading.Thread(target=_loop, name="valuation-background-refresh", daemon=True).start()
    log.info("Valuation: background refresh thread started")
