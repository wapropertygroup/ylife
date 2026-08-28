"""
ystocker.brief
~~~~~~~~~~~~~~
Builds the AI Markets Brief: one prompt carrying what all eight dashboard pages
show — /markets, /evaluation, /commodities, /13f, /fedwatch, /housing,
/multiples, /fed — plus the instructions that turn it into a sectioned,
table-heavy daily brief.

Why this is a module and not another prompt inline in ``routes.py``: three copies
of a daily-summary prompt already lived there (the endpoint, the email
broadcast, the overnight pre-generator) and had drifted apart. The worst of the
drift was in the endpoint the /markets card actually calls: it built its
snapshot from ``payload["market_data"]``, which the browser has never sent, so
the model was asked for four paragraphs of commentary on a snapshot containing
one line — the date. The brief has exactly one definition here.

Three rules every section builder follows:

* **Absent is stated, not skipped.** A source whose cache is cold emits an
  explicit "unavailable" line. Dropping the section instead reads to the model
  as "nothing to say about housing" and invites it to fill the gap from
  training data; in a dated, numeric brief an invented number is far worse than
  an admitted hole.
* **Every series is reduced before it is sent.** These payloads run large — the
  housing one is ~10 MB cold, and 13F carries 50 holdings for each of 22 funds.
  What a brief can use is the latest value, its change, and its position in
  range, so that is what each builder emits. Row caps are named constants
  below, not magic numbers buried in slices.
* **Numbers are formatted once, here.** ``_num`` and friends return ``"n/a"``
  for anything non-numeric, so a missing field degrades to a visible gap
  instead of raising and turning the whole brief into a 500. This mirrors the
  defensive formatting in the existing ``_build_housing_prompt``.

The caller owns data collection (see ``routes._collect_brief_sources``), because
the /markets and /commodities payloads live in ``routes.py``-private caches.
This module only formats what it is handed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Row caps ────────────────────────────────────────────────────────────────
# Each cap is the point past which more rows stop changing what the brief can
# say. They are deliberately visible: a silent truncation would let the brief
# imply it had surveyed everything.
MAX_SECTORS       = 12   # all 12 SPDR sector ETFs
MAX_MOVERS        = 5    # per side, matches /api/movers
MAX_COMMODITIES   = 16   # all of _COMMODITY_SYMBOLS
MAX_13F_CONSENSUS = 20   # of 25 consensus positions
MAX_13F_FUNDS     = 8    # largest funds by reported AUM
MAX_13F_HOLDINGS  = 5    # top holdings shown per fund
MAX_FED_MEETINGS  = 6    # ~1 year of FOMC dates
MAX_METROS        = 10   # of 15 metros the housing page ships
MAX_EVAL_SECTORS  = 12   # peer groups on /evaluation
MAX_EVENTS        = 8    # upcoming economic calendar entries
MAX_FED_SERIES    = 14   # balance-sheet + macro series

# Index keys in display order, with the labels used in both languages. The
# /markets payload keys commodities and FX into the same dict as the equity
# indices; the commodity block covers those in more depth, so only DXY is kept
# here — it is the one the equity read actually leans on.
_INDEX_ROWS: list[tuple[str, str, str]] = [
    ("spx",    "S&P 500",            "标普500"),
    ("ixic",   "Nasdaq",             "纳斯达克"),
    ("dji",    "Dow Jones",          "道琼斯"),
    ("ftse",   "FTSE 100",           "英国富时100"),
    ("n225",   "Nikkei 225",         "日经225"),
    ("sse",    "Shanghai Composite", "上证综指"),
    ("csi500", "CSI 500",            "中证500"),
    ("twii",   "Taiwan Weighted",    "台湾加权"),
    ("kospi",  "KOSPI",              "韩国综合"),
    ("dxy",    "US Dollar Index",    "美元指数"),
]

# Balance-sheet and macro series worth a line, in reading order. Anything in
# fed.SERIES not listed here is still fetched by fed.py for its charts; it just
# does not earn room in a brief.
_FED_SERIES_ORDER: list[str] = [
    "WALCL", "TREAST", "WSHOMCB", "WRESBAL", "RRPONTSYD", "WTREGEN",
    "WCURCIR", "WLCFLPCL", "M2SL", "BAMLH0A0HYM2", "BAMLC0A0CM",
    "DFII10", "T10YIE", "UMCSENT",
]

# Housing headline tiles worth a line, in reading order. Checked against the
# live payload — every key here exists in housing.py's `headline` block, and
# every key that block ships is listed, so the brief cannot quietly omit one.
_HOUSING_TILES: list[tuple[str, str, str]] = [
    ("zhvi",                    "Zillow Home Value Index", "Zillow房价指数"),
    ("zori",                    "Zillow Observed Rent",    "Zillow租金指数"),
    ("fred_case_shiller",       "Case-Shiller Index",      "凯斯席勒指数"),
    ("mortgage_rate",           "30Y Mortgage Rate",       "30年房贷利率"),
    ("mortgage_spread",         "Mortgage-10Y Spread",     "房贷与10年期利差"),
    ("redfin_months_of_supply", "Months of Supply",        "库存月数"),
    ("fred_months_supply_new",  "Months of Supply (new)",  "新屋库存月数"),
    ("redfin_inventory",        "Active Listings",         "在售房源"),
    ("redfin_median_dom",       "Median Days on Market",   "中位挂牌天数"),
    ("days_to_pending",         "Days to Pending",         "成交所需天数"),
    ("price_cuts",              "Share With Price Cut",    "降价房源占比"),
    ("fred_starts",             "Housing Starts",          "新屋开工"),
    ("fred_permits",            "Building Permits",        "建筑许可"),
    ("fred_new_home_sales",     "New Home Sales",          "新屋销售"),
    ("fred_existing_sales",     "Existing Home Sales",     "成屋销售"),
    ("fred_homeownership",      "Homeownership Rate",      "自有住房率"),
    ("fred_rental_vacancy",     "Rental Vacancy Rate",     "租赁空置率"),
    ("fred_delinquency",        "Mortgage Delinquency",    "房贷违约率"),
]

# Valuation headline tiles worth a line, in reading order.
_VALUATION_TILES: list[tuple[str, str, str]] = [
    ("spx_trailing_pe",     "S&P 500 trailing P/E",      "标普500静态市盈率"),
    ("spx_pe_percentile",   "trailing P/E percentile",   "静态市盈率历史分位"),
    ("spx_cape",            "Shiller CAPE",              "席勒CAPE"),
    ("spx_cape_percentile", "CAPE percentile",           "CAPE历史分位"),
    ("spx_trailing_eps",    "S&P 500 trailing EPS",      "标普500静态每股收益"),
    ("spx_fwd_realized",    "realized forward P/E",      "已实现前瞻市盈率"),
    ("spy_forward_pe",      "SPY forward P/E",           "SPY前瞻市盈率"),
    ("qqq_forward_pe",      "QQQ forward P/E",           "QQQ前瞻市盈率"),
]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _isnum(v: Any) -> bool:
    """True for a real number. ``bool`` is excluded despite subclassing int."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num(v: Any, nd: int = 2, plus: bool = False, suffix: str = "") -> str:
    """Format a number, or ``n/a`` when there isn't one.

    Never raises: a source that dropped a field must leave a gap in the brief,
    not turn the request into a 500.
    """
    if not _isnum(v):
        return "n/a"
    sign = "+" if plus and v >= 0 else ""
    return f"{sign}{v:,.{nd}f}{suffix}"


def _pct(v: Any, nd: int = 2, plus: bool = True) -> str:
    return _num(v, nd, plus=plus, suffix="%")


def _usd(v: Any, nd: int = 0) -> str:
    if not _isnum(v):
        return "n/a"
    return f"${v:,.{nd}f}"


def _bn(v: Any, nd: int = 1) -> str:
    """US dollars in billions, promoted to trillions past 1000B for readability."""
    if not _isnum(v):
        return "n/a"
    if abs(v) >= 1000:
        return f"${v / 1000:,.2f}T"
    return f"${v:,.{nd}f}B"


def _last(seq: Any) -> Optional[float]:
    """Last numeric value of a series, or None."""
    if not isinstance(seq, (list, tuple)):
        return None
    for v in reversed(seq):
        if _isnum(v):
            return float(v)
    return None


def _last_date(seq: Any) -> str:
    if isinstance(seq, (list, tuple)) and seq:
        return str(seq[-1])
    return "n/a"


def _delta(seq: Any, back: int = 1) -> Optional[float]:
    """Change between the last numeric point and the one ``back`` before it."""
    if not isinstance(seq, (list, tuple)):
        return None
    nums = [float(v) for v in seq if _isnum(v)]
    if len(nums) <= back:
        return None
    return nums[-1] - nums[-1 - back]


def _pos_in_range(v: Any, lo: Any, hi: Any) -> str:
    """Where ``v`` sits between ``lo`` and ``hi``, as a percentage."""
    if not (_isnum(v) and _isnum(lo) and _isnum(hi)) or hi <= lo:
        return "n/a"
    return f"{(v - lo) / (hi - lo) * 100:.0f}%"


def _percentile(values: Any, latest: Any) -> str:
    """Percentile rank of ``latest`` within ``values``."""
    nums = [float(v) for v in (values or []) if _isnum(v)]
    if not nums or not _isnum(latest):
        return "n/a"
    below = sum(1 for v in nums if v <= latest)
    return f"{below / len(nums) * 100:.0f}%"


def _tile(block: Any) -> str:
    """Render one housing/valuation headline tile as ``value (yoy)``.

    Both payloads use the same tile shape: ``{value, yoy, unit, yoy_unit}``.
    The unit vocabulary is the union of what housing.py and valuation.py emit;
    every member is handled explicitly because the fallback prints a bare float,
    which is how ``fred_existing_sales`` came out as "4,060,000.00" instead of
    "4.06M units". A unit this function does not know is logged, not guessed.

    Unit traps worth naming: ``pct_dec`` stores a fraction (0.24 for 24%), and
    ``pp`` is percentage *points*, so rendering it with a % sign would claim
    a 2.01pp mortgage spread was a 2.01% one.
    """
    if not isinstance(block, dict):
        return "n/a"
    val, unit = block.get("value"), block.get("unit")
    if not _isnum(val):
        return "n/a"
    if unit == "usd":
        shown = _usd(val)
    elif unit == "pct_dec":
        shown = f"{val * 100:.2f}%"
    elif unit == "pct":
        shown = f"{val:.2f}%"
    elif unit == "pp":
        shown = f"{val:.2f}pp"
    elif unit == "months":
        shown = f"{val:.1f} months"
    elif unit == "days":
        shown = f"{val:.0f} days"
    elif unit in ("thousands", "k"):
        shown = f"{val:,.0f}k units (annual rate)"
    elif unit in ("units", "count"):
        # Counts run to the millions (existing home sales, active listings);
        # seven digits of precision is noise in a brief.
        shown = f"{val / 1e6:.2f}M" if abs(val) >= 1e6 else f"{val:,.0f}"
    elif unit == "index":
        shown = f"{val:,.2f} (index)"
    elif unit == "x":
        shown = f"{val:.2f}x"
    elif unit == "pct_rank":
        shown = f"{val:.0f}th percentile"
    elif unit == "bln":
        shown = _bn(val)
    else:
        log.info("Brief: unhandled tile unit %r (value %r) — printed raw", unit, val)
        shown = _num(val)
    yoy = block.get("yoy")
    if _isnum(yoy):
        yoy_unit = block.get("yoy_unit") or "%"
        shown += f" ({yoy:+.2f}{'pp' if yoy_unit == 'pp' else '%'} yoy)"
    return shown


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A pipe table. The model is asked to reuse these verbatim where it can."""
    if not rows:
        return []
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def _unavailable(name: str) -> list[str]:
    """The line a cold source emits. See the module docstring."""
    return [f"{name}: DATA UNAVAILABLE — say so or omit; do not estimate."]


# ---------------------------------------------------------------------------
# Section builders — one per source page
# ---------------------------------------------------------------------------

def _sec_markets(markets: Optional[dict], breadth: Optional[dict],
                 movers: Optional[dict], fg: Optional[dict],
                 pcr: Optional[dict], aaii: Optional[dict],
                 skew: Optional[dict]) -> list[str]:
    """Indices, volatility, breadth, sector performance, movers, sentiment."""
    out = ["", "=== 1. INDICES / MARKET OVERVIEW (source: /markets) ==="]
    if not markets:
        return out + _unavailable("Index snapshot")

    idx = markets.get("indices") or {}
    rows = []
    for key, en, zh in _INDEX_ROWS:
        d = idx.get(key) or {}
        if not isinstance(d, dict) or d.get("error") or not _isnum(d.get("current")):
            continue
        rows.append([
            f"{en} / {zh}",
            _num(d.get("current")),
            _pct(d.get("day_chg")),
            _pct(d.get("ytd")),
            _num(d.get("hi52")),
            _num(d.get("lo52")),
            _pos_in_range(d.get("current"), d.get("lo52"), d.get("hi52")),
            _num(d.get("rsi14"), 1),
            _num(d.get("ma50")),
            _num(d.get("ma200")),
            _num(d.get("pe"), 1),
        ])
    if rows:
        out.append("Indices (52w range position = where price sits between the 52-week low and high):")
        out += _table(["Index", "Last", "Day %", "YTD %", "52w High", "52w Low",
                       "52w Range Pos", "RSI14", "MA50", "MA200", "P/E"], rows)
    else:
        out += _unavailable("Index snapshot")

    vix = markets.get("vix") or {}
    if isinstance(vix, dict) and _isnum(vix.get("current")):
        term = vix.get("term_ratio")
        shape = "n/a"
        if _isnum(term):
            shape = "backwardation (stress)" if term > 1 else "contango (calm)"
        out += ["",
                f"VIX: {_num(vix.get('current'))} ({_pct(vix.get('day_chg'))} day) · "
                f"VIX3M {_num(vix.get('vix3m'))} · VVIX {_num(vix.get('vvix'))} · "
                f"VIX/VIX3M term ratio {_num(term)} = {shape}",
                "Zones: <15 calm, 15-25 normal, 25-35 elevated, >35 panic."]
    else:
        out += [""] + _unavailable("VIX")

    if breadth:
        latest = breadth.get("latest") or {}
        b_rows = [[f"{p}-day MA", _pct(latest.get(p), 1, plus=False)]
                  for p in ("20", "50", "100", "150", "200") if _isnum(latest.get(p))]
        if b_rows:
            out += ["",
                    f"Market breadth — % of S&P 500 members above each moving average "
                    f"(universe {breadth.get('universe', 'n/a')} names, as of {breadth.get('asof', 'n/a')}"
                    f"{', STALE cache' if breadth.get('stale') else ''}):"]
            out += _table(["Moving average", "% of members above"], b_rows)
        rsp = _last((breadth.get("rsp_spy") or {}).get("values"))
        if _isnum(rsp):
            out.append(f"RSP/SPY equal-weight ratio: {_num(rsp, 4)} "
                       f"(falling = gains narrowing into the largest names).")
    else:
        out += [""] + _unavailable("Breadth")

    sectors = markets.get("sectors") or []
    s_rows = []
    for s in sorted(sectors, key=lambda x: x.get("day_chg") or 0, reverse=True)[:MAX_SECTORS]:
        s_rows.append([str(s.get("label") or s.get("ticker") or "?"),
                       str(s.get("ticker") or "?"),
                       _pct(s.get("day_chg")),
                       _pct(s.get("week_chg_pct"))])
    if s_rows:
        out += ["", "Sector ETF performance (ranked by day change):"]
        out += _table(["Sector", "ETF", "Day %", "Week %"], s_rows)

    if movers:
        for side, en in (("gainers", "Top gainers"), ("losers", "Top losers")):
            rows_m = [[str(m.get("ticker") or "?"), _usd(m.get("price"), 2),
                       _pct(m.get("day_chg")), _num(m.get("rel_vol"), 2),
                       str(m.get("sector") or "n/a")]
                      for m in (movers.get(side) or [])[:MAX_MOVERS]]
            if rows_m:
                out += ["", f"{en} (rel vol = volume vs its own average):"]
                out += _table(["Ticker", "Price", "Day %", "Rel Vol", "Sector"], rows_m)

    out += ["", "Sentiment gauges:"]
    sent_rows = []
    if fg and _isnum(fg.get("score")):
        sent_rows.append(["CNN Fear & Greed", _num(fg.get("score"), 0),
                          str(fg.get("rating") or "n/a"),
                          f"prev close {_num(fg.get('prev_close'), 0)}, "
                          f"1w {_num(fg.get('prev_week'), 0)}, "
                          f"1m {_num(fg.get('prev_month'), 0)}, "
                          f"1y {_num(fg.get('prev_year'), 0)}"])
    if pcr and _isnum(pcr.get("current")):
        sent_rows.append(["CBOE Put/Call ratio", _num(pcr.get("current")),
                          "high = hedging/fear",
                          f"20d MA {_num(pcr.get('ma20'))}, day {_pct(pcr.get('day_chg'))}"])
    if aaii and _isnum(aaii.get("bullish")):
        sent_rows.append(["AAII survey",
                          f"bull {_num(aaii.get('bullish'), 1)}%",
                          f"bear {_num(aaii.get('bearish'), 1)}%",
                          f"neutral {_num(aaii.get('neutral'), 1)}%, "
                          f"bull-bear spread {_num(aaii.get('bull_bear_spread'), 1, plus=True)}pp, "
                          f"week of {aaii.get('date', 'n/a')}"])
    if skew and isinstance(skew.get("latest"), dict):
        sk = skew["latest"]
        sent_rows.append(["CBOE SKEW", _num(sk.get("skew"), 1),
                          str(sk.get("band") or "n/a"),
                          f"percentile {_num(sk.get('percentile'), 0)}%, as of {sk.get('skew_date', 'n/a')}"])
    if sent_rows:
        out += _table(["Gauge", "Reading", "Level", "Context"], sent_rows)
    else:
        out += _unavailable("Sentiment gauges")
    return out


def _sec_rates(yield_curve: Optional[dict], credit: Optional[dict],
               yield_spread: Optional[dict]) -> list[str]:
    """Treasury curve, curve inversion history, credit spread proxy."""
    out = ["", "=== 2. RATES & CREDIT (source: /markets) ==="]
    if not yield_curve:
        out += _unavailable("Yield curve")
    else:
        us = (yield_curve.get("us") or {}).get("current") or {}
        ladder = ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
        rows = [[t, _num(us.get(t)) + "%"] for t in ladder if _isnum(us.get(t))]
        if rows:
            out.append("US Treasury yield curve:")
            out += _table(["Maturity", "Yield"], rows)
        y10, y2, y3m = us.get("10Y"), us.get("2Y"), us.get("3M")
        if _isnum(y10) and _isnum(y2):
            sp = y10 - y2
            out.append(f"10Y-2Y spread: {sp:+.2f}pp — "
                       f"{'INVERTED, a recession signal' if sp < 0 else 'normal (positively sloped)'}.")
        if _isnum(y10) and _isnum(y3m):
            sp2 = y10 - y3m
            out.append(f"10Y-3M spread: {sp2:+.2f}pp — {'inverted' if sp2 < 0 else 'normal'}.")
        for country, label in (("cn", "China 10Y"), ("jp", "Japan 10Y")):
            c10 = ((yield_curve.get(country) or {}).get("current") or {}).get("10Y")
            if _isnum(c10):
                out.append(f"{label}: {c10:.2f}%")
        if _isnum(yield_curve.get("spx_pe")):
            out.append(f"S&P 500 P/E shown alongside the curve: {_num(yield_curve.get('spx_pe'), 1)}")

    if yield_spread:
        cur = _last(yield_spread.get("spread"))
        if _isnum(cur):
            out.append(f"10Y-2Y spread history: latest {cur:+.2f}pp, "
                       f"{_percentile(yield_spread.get('spread'), cur)} percentile of its own history.")

    if credit:
        cur = _last(credit.get("spread"))
        if _isnum(cur):
            hyg = (credit.get("hyg") or {}).get("price")
            tlt = (credit.get("tlt") or {}).get("price")
            out += ["",
                    f"HY/IG credit proxy (HYG/TLT price ratio, {credit.get('period', 'n/a')} window): "
                    f"{cur:.4f} — {'risk-on, spreads tight' if cur > 0.5 else 'risk-off, spreads wide'}. "
                    f"HYG {_usd(hyg, 2)}, TLT {_usd(tlt, 2)}. "
                    f"Ratio percentile in window: {_percentile(credit.get('spread'), cur)}."]
    else:
        out += _unavailable("Credit spread")
    return out


def _sec_evaluation(eval_data: Optional[dict]) -> list[str]:
    """Sector-by-sector valuation from the peer-group tables on /evaluation."""
    out = ["", "=== 3. SECTOR VALUATION (source: /evaluation) ==="]
    if not eval_data or not eval_data.get("sectors"):
        return out + _unavailable("Sector valuation")

    rows = []
    for s in (eval_data.get("sectors") or [])[:MAX_EVAL_SECTORS]:
        rows.append([
            str(s.get("sector") or "?"),
            str(s.get("count") or "n/a"),
            _num(s.get("median_pe_ttm"), 1),
            _num(s.get("median_pe_fwd"), 1),
            _num(s.get("median_peg"), 2),
            _num(s.get("median_ev_ebitda"), 1),
            _num(s.get("median_upside"), 1) + "%",
            _pct(s.get("median_day_change")),
        ])
    out.append("Median valuation multiples by peer group (upside = mean analyst target vs price):")
    out += _table(["Sector", "Names", "P/E TTM", "P/E Fwd", "PEG",
                   "EV/EBITDA", "Median Upside", "Median Day %"], rows)

    for key, label in (("most_expensive", "Highest forward P/E"),
                       ("cheapest", "Lowest forward P/E"),
                       ("most_upside", "Largest analyst upside")):
        names = eval_data.get(key) or []
        rows_n = [[str(n.get("ticker") or "?"), str(n.get("sector") or "n/a"),
                   _num(n.get("pe_fwd"), 1), _num(n.get("upside"), 1) + "%",
                   _num(n.get("market_cap"), 1) + "B"]
                  for n in names]
        if rows_n:
            out += ["", f"{label}:"]
            out += _table(["Ticker", "Sector", "P/E Fwd", "Upside", "Mkt Cap"], rows_n)
    return out


def _sec_commodities(comm: Optional[dict]) -> list[str]:
    """Metals, energy, agriculture, the dollar, and the cross ratios."""
    out = ["", "=== 4. COMMODITIES & DOLLAR (source: /commodities) ==="]
    if not comm or not comm.get("commodities"):
        return out + _unavailable("Commodities")

    blocks = comm.get("commodities") or {}
    rows = []
    for _key, c in list(blocks.items())[:MAX_COMMODITIES]:
        if not isinstance(c, dict) or not _isnum(c.get("last")):
            continue
        rows.append([
            str(c.get("name") or "?"),
            str(c.get("group") or "n/a"),
            _num(c.get("last")),
            str(c.get("unit") or ""),
            _pct(c.get("day_chg_pct")),
            _pct(c.get("ret_1w")),
            _pct(c.get("ret_1m")),
            _pct(c.get("ret_3m")),
            _pct(c.get("ret_ytd")),
            _pct(c.get("ret_1y")),
            _num(c.get("pos52"), 0) + "%",
            _num(c.get("rsi_14"), 1),
        ])
    if rows:
        out.append("Commodity and FX board (52w pos = position between the 52-week low and high):")
        out += _table(["Contract", "Group", "Last", "Unit", "Day %", "1W %", "1M %",
                       "3M %", "YTD %", "1Y %", "52w Pos", "RSI14"], rows)

    ratios = comm.get("ratios") or {}
    r_rows = []
    for _k, r in ratios.items():
        if not isinstance(r, dict):
            continue
        cur = r.get("current")
        if not _isnum(cur):
            continue
        r_rows.append([str(r.get("label") or "?"), _num(cur),
                       _percentile(r.get("values"), cur),
                       str(r.get("desc") or "")])
    if r_rows:
        out += ["", "Cross-commodity ratios (percentile is within the shown history):"]
        out += _table(["Ratio", "Current", "Percentile", "Meaning"], r_rows)
    return out


def _sec_13f(holdings: Optional[dict], consensus: Optional[list]) -> list[str]:
    """Institutional positioning from the quarterly 13F filings."""
    out = ["", "=== 5. 13F INSTITUTIONAL HOLDINGS (source: /13f) ==="]
    if not holdings:
        return out + _unavailable("13F holdings")

    live = {name: fd for name, fd in holdings.items()
            if isinstance(fd, dict) and not fd.get("error") and fd.get("holdings")}
    if not live:
        return out + _unavailable("13F holdings")

    periods = sorted({str(fd.get("period_of_report") or "") for fd in live.values()} - {""})
    out.append(f"{len(live)} funds reporting; filing periods present: {', '.join(periods) or 'n/a'}. "
               f"13F is quarterly and lags by up to 45 days — it is positioning, not news.")

    if consensus:
        rows = [[str(c.get("ticker") or "?"), str(c.get("fund_count") or "n/a"),
                 _bn((c.get("total_value_m") or 0) / 1000),
                 ", ".join((c.get("fund_names") or [])[:4])]
                for c in consensus[:MAX_13F_CONSENSUS]]
        if rows:
            out += ["", "Consensus positions — tickers held by the most tracked funds:"]
            out += _table(["Ticker", "Funds Holding", "Combined Value", "Examples"], rows)

    ranked = sorted(live.items(),
                    key=lambda kv: kv[1].get("total_value_millions") or 0,
                    reverse=True)[:MAX_13F_FUNDS]
    out += ["", f"Largest {len(ranked)} funds by reported 13F value, with their top holdings:"]
    rows = []
    for name, fd in ranked:
        tops = (fd.get("holdings") or [])[:MAX_13F_HOLDINGS]
        detail = "; ".join(
            f"{h.get('ticker') or '?'} {_num(h.get('pct_portfolio'), 1)}%"
            f" ({h.get('change') or 'unknown'}"
            + (f" {h.get('change_pct'):+.0f}%" if _isnum(h.get("change_pct")) else "")
            + ")"
            for h in tops
        )
        rows.append([name, _bn((fd.get("total_value_millions") or 0) / 1000),
                     str(fd.get("total_holdings") or "n/a"),
                     str(fd.get("period_of_report") or "n/a"),
                     detail or "n/a"])
    out += _table(["Fund", "13F Value", "Positions", "Period",
                   "Top holdings (% of portfolio, qoq change)"], rows)
    return out


def _sec_fedwatch(fw: Optional[dict]) -> list[str]:
    """Market-implied policy path from fed funds futures."""
    out = ["", "=== 6. FED POLICY EXPECTATIONS (source: /fedwatch) ==="]
    if not fw or not fw.get("meetings"):
        return out + _unavailable("FedWatch")

    cur = fw.get("current") or {}
    out.append(f"Current target range: {cur.get('label', 'n/a')}% "
               f"(mid {_num(cur.get('mid'))}%, EFFR {_num(cur.get('effr'))}%). "
               f"Probabilities as of {fw.get('as_of', 'n/a')}, from fed funds futures. "
               f"Cut/hold/hike are relative to today's range, not to the previous meeting.")
    rows = []
    for m in (fw.get("meetings") or [])[:MAX_FED_MEETINGS]:
        if not isinstance(m, dict):
            continue
        rows.append([
            str(m.get("label") or m.get("date") or "?"),
            _num(m.get("implied_rate")) + "%",
            _num(m.get("change_bp"), 1, plus=True) + "bp",
            _num(m.get("cut_prob"), 1, plus=False) + "%",
            _num(m.get("hold_prob"), 1, plus=False) + "%",
            _num(m.get("hike_prob"), 1, plus=False) + "%",
        ])
    out.append("Implied policy path by FOMC meeting:")
    out += _table(["Meeting", "Implied Rate", "Change vs Today",
                   "Cut Prob", "Hold Prob", "Hike Prob"], rows)

    first = (fw.get("meetings") or [{}])[0]
    outcomes = first.get("outcomes") or []
    if outcomes:
        o_rows = [[f"{_num(o.get('lower'))}-{_num(o.get('upper'))}%",
                   _num(o.get("prob"), 1, plus=False) + "%",
                   _num(o.get("steps"), 0, plus=True)]
                  for o in outcomes if isinstance(o, dict)]
        if o_rows:
            out += ["", f"Full outcome distribution for the next meeting "
                        f"({first.get('label', 'n/a')}):"]
            out += _table(["Target range", "Probability", "25bp steps"], o_rows)
    return out


def _sec_housing(h: Optional[dict]) -> list[str]:
    """US housing: prices, rents, supply, affordability, metro detail."""
    out = ["", "=== 7. US HOUSING (source: /housing) ==="]
    if not h or not h.get("headline"):
        return out + _unavailable("Housing")

    head = h.get("headline") or {}
    out.append(f"As of {h.get('as_of', 'n/a')}. Sources: Zillow, Redfin, FRED.")
    rows = [[en, _tile(head.get(key)), str((head.get(key) or {}).get("source") or "n/a")]
            for key, en, _zh in _HOUSING_TILES if isinstance(head.get(key), dict)]
    if rows:
        out.append("Housing headline indicators:")
        out += _table(["Indicator", "Latest (yoy change)", "Source"], rows)

    aff = h.get("affordability") or {}
    pay, hv, rate = _last(aff.get("payment")), _last(aff.get("home_value")), _last(aff.get("rate"))
    if _isnum(pay):
        peak = max((v for v in (aff.get("payment") or []) if _isnum(v)), default=None)
        out += ["", f"Affordability: monthly payment on the median home {_usd(pay)} "
                    f"(home value {_usd(hv)}, mortgage rate {_num(rate)}%, "
                    f"{_num(aff.get('down_pct'), 0)}% down). Peak payment on record: {_usd(peak)}."]

    ptr = h.get("price_to_rent") or {}
    cur_ptr = _last(ptr.get("values"))
    if _isnum(cur_ptr):
        out.append(f"Price-to-rent ratio: {_num(cur_ptr)} "
                   f"(record peak {_num(ptr.get('peak'))} in {ptr.get('peak_date', 'n/a')}).")

    spread = h.get("mortgage_spread") or {}
    cur_sp = _last(spread.get("spread"))
    if _isnum(cur_sp):
        out.append(f"Mortgage-Treasury spread: {_num(cur_sp)}pp "
                   f"(30Y mortgage {_num(_last(spread.get('mortgage')))}%, "
                   f"10Y Treasury {_num(_last(spread.get('treasury')))}%). "
                   f"A wide spread means mortgage rates are not following Treasuries down.")

    rg = h.get("rent_growth") or {}
    mkt, cpi = _last(rg.get("market")), _last(rg.get("cpi"))
    if _isnum(mkt):
        out.append(f"Rent growth: market asking rents {_num(mkt)}% yoy vs CPI shelter {_num(cpi)}% yoy. "
                   f"CPI shelter lags market rents by roughly a year.")

    metros = h.get("metros") or []
    m_rows = [[str(m.get("metro") or "?"), _usd(m.get("zhvi")),
               _num(m.get("zhvi_yoy")) + "%", _usd(m.get("zori")),
               _num(m.get("zori_yoy")) + "%", _num(m.get("days_to_pending"), 0)]
              for m in metros[:MAX_METROS] if isinstance(m, dict)]
    if m_rows:
        out += ["", f"Largest {len(m_rows)} metros by population rank:"]
        out += _table(["Metro", "Home Value", "Value yoy", "Rent", "Rent yoy",
                       "Days to Pending"], m_rows)
    return out


def _sec_multiples(val: Optional[dict]) -> list[str]:
    """Index-level valuation multiples and the forward-P/E series."""
    out = ["", "=== 8. INDEX VALUATION MULTIPLES (source: /multiples) ==="]
    if not val or not val.get("headline"):
        return out + _unavailable("Index multiples")

    head = val.get("headline") or {}
    out.append(f"As of {val.get('as_of', 'n/a')}.")
    rows = []
    for key, en, _zh in _VALUATION_TILES:
        block = head.get(key)
        if not isinstance(block, dict) or not _isnum(block.get("value")):
            continue
        extra = []
        if _isnum(block.get("coverage_pct")):
            extra.append(f"coverage {block['coverage_pct']:.0f}%")
        if block.get("constituents"):
            extra.append(f"{block['constituents']} constituents")
        if block.get("since"):
            extra.append(f"since {block['since']}")
        if _isnum(block.get("median")):
            extra.append(f"median {block['median']:.2f}x")
        rows.append([en, _tile(block), str(block.get("source") or "n/a"),
                     ", ".join(extra) or "—"])
    if rows:
        out.append("Valuation headline metrics:")
        out += _table(["Metric", "Value", "Source", "Notes"], rows)

    fwd = val.get("forward") or {}
    f_rows = []
    for etf in ("SPY", "QQQ"):
        b = fwd.get(etf)
        if not isinstance(b, dict) or not _isnum(b.get("forward_pe")):
            continue
        f_rows.append([etf, _num(b.get("forward_pe")) + "x",
                       _num(b.get("earnings_yield_pct")) + "%",
                       _num(b.get("trailing_pe")) + "x",
                       _num(b.get("growth_pct")) + "%",
                       _num(b.get("coverage_pct"), 0) + "%",
                       _bn(b.get("market_cap_b"))])
    if f_rows:
        out += ["", "Bottom-up forward P/E, computed from constituent estimates:"]
        out += _table(["ETF", "Forward P/E", "Earnings Yield", "Trailing P/E",
                       "Implied Growth", "Coverage", "Market Cap"], f_rows)

    hist = val.get("forward_history") or []
    if len(hist) >= 2:
        first, last = hist[0], hist[-1]
        out += ["", f"Forward P/E history spans {first.get('date', 'n/a')} to "
                    f"{last.get('date', 'n/a')} ({len(hist)} daily observations): "
                    f"SPY {_num(first.get('SPY'))}x -> {_num(last.get('SPY'))}x, "
                    f"QQQ {_num(first.get('QQQ'))}x -> {_num(last.get('QQQ'))}x."]

    for key, label in (("spx_consensus_fwd", "S&P 500 consensus forward P/E (FactSet)"),
                       ("ndx_consensus_fwd", "Nasdaq 100 consensus forward P/E (Siblis)")):
        block = val.get(key)
        if not isinstance(block, dict):
            continue
        cur = _last(block.get("values"))
        if _isnum(cur):
            out.append(f"{label}: {_num(cur)}x as of {_last_date(block.get('dates'))} "
                       f"({_percentile(block.get('values'), cur)} percentile of its series).")

    blocks = val.get("multpl") or {}
    for sub, sub_label in (("spx_pe", "S&P 500 trailing P/E"), ("spx_cape", "Shiller CAPE")):
        b = blocks.get(sub)
        if not isinstance(b, dict):
            continue
        cur = _last(b.get("values"))
        if not _isnum(cur):
            continue
        dates = b.get("dates") or []
        since = str(dates[0])[:4] if dates else "n/a"
        out.append(f"{sub_label} long-run history: {_num(cur)}x, "
                   f"{_percentile(b.get('values'), cur)} percentile since {since}.")
    return out


def _sec_fed(fed_data: Optional[dict], series_meta: Optional[dict]) -> list[str]:
    """Fed balance sheet and the macro series that sit beside it."""
    out = ["", "=== 9. FED BALANCE SHEET & MACRO (source: /fed) ==="]
    if not fed_data or not fed_data.get("series"):
        return out + _unavailable("Fed balance sheet")

    series = fed_data.get("series") or {}
    meta = series_meta or {}
    walcl = series.get("WALCL") or {}
    out.append(f"H.4.1 as of {_last_date(walcl.get('dates'))}. "
               f"Values are US dollars in billions unless the unit says otherwise. "
               f"Weekly change is versus the prior report; 13-week change shows the trend.")

    rows = []
    for sid in _FED_SERIES_ORDER[:MAX_FED_SERIES]:
        block = series.get(sid)
        if not isinstance(block, dict) or block.get("error"):
            continue
        vals = block.get("values")
        cur = _last(vals)
        if not _isnum(cur):
            continue
        info = meta.get(sid) or {}
        unit = info.get("unit")
        if unit == "pct":
            shown, wk, qt = f"{cur:.2f}%", _delta(vals, 1), _delta(vals, 13)
            wk_s = f"{wk:+.2f}pp" if _isnum(wk) else "n/a"
            qt_s = f"{qt:+.2f}pp" if _isnum(qt) else "n/a"
        elif unit == "bps":
            shown, wk, qt = f"{cur:,.0f}bps", _delta(vals, 1), _delta(vals, 13)
            wk_s = f"{wk:+,.0f}bps" if _isnum(wk) else "n/a"
            qt_s = f"{qt:+,.0f}bps" if _isnum(qt) else "n/a"
        elif unit in ("index", "k", "binary", "usd"):
            shown, wk, qt = _num(cur), _delta(vals, 1), _delta(vals, 13)
            wk_s = _num(wk, plus=True) if _isnum(wk) else "n/a"
            qt_s = _num(qt, plus=True) if _isnum(qt) else "n/a"
        else:
            shown = _bn(cur)
            wk, qt = _delta(vals, 1), _delta(vals, 13)
            wk_s = (f"{wk:+,.1f}B" if _isnum(wk) else "n/a")
            qt_s = (f"{qt:+,.1f}B" if _isnum(qt) else "n/a")
        rows.append([str(info.get("label") or sid), sid, shown, wk_s, qt_s,
                     _last_date(block.get("dates"))])
    if rows:
        out += _table(["Series", "FRED ID", "Latest", "1-period change",
                       "13-period change", "As of"], rows)
    else:
        out += _unavailable("Fed balance sheet")

    # Net liquidity is the reading the /fed page exists to support: assets minus
    # the two accounts that sterilise them.
    tot = _last((series.get("WALCL") or {}).get("values"))
    tga = _last((series.get("WTREGEN") or {}).get("values"))
    rrp = _last((series.get("RRPONTSYD") or {}).get("values"))
    if all(_isnum(x) for x in (tot, tga, rrp)):
        out.append("")
        out.append(f"Net liquidity (total assets - TGA - reverse repos): "
                   f"{_bn(tot)} - {_bn(tga)} - {_bn(rrp)} = {_bn(tot - tga - rrp)}.")
    return out


def _sec_events(events: Optional[list]) -> list[str]:
    """The economic calendar, for the forward-looking section."""
    out = ["", "=== 10. UPCOMING ECONOMIC EVENTS (source: /markets) ==="]
    if not events:
        return out + _unavailable("Economic calendar")
    rows = []
    for e in events[:MAX_EVENTS]:
        if not isinstance(e, dict):
            continue
        rows.append([str(e.get("date") or "?"), str(e.get("time") or ""),
                     str(e.get("country") or ""), str(e.get("event") or "?"),
                     str(e.get("impact") or ""), str(e.get("forecast") or "—"),
                     str(e.get("previous") or "—")])
    if not rows:
        return out + _unavailable("Economic calendar")
    out += _table(["Date", "Time", "Country", "Event", "Impact",
                   "Forecast", "Previous"], rows)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Keys in the sources dict that are not themselves a data source: metadata the
# formatters need, and bookkeeping. Excluded from the used/cold accounting so
# the counts a caller reports mean what they say.
_META_KEYS = frozenset({"fed_series_meta", "_stale"})

#: The two report scopes. "us" is the whole-site brief shown on /markets; "cn" is
#: the Asia-Pacific report on /daily.
MARKETS = ("us", "cn")

# Asia-Pacific index rows, in reading order. FTSE is included as the European
# handover, since Asia closes into it.
_INDEX_ROWS_CN: list[tuple[str, str, str]] = [
    ("sse",    "Shanghai Composite", "上证综指"),
    ("csi500", "CSI 500",            "中证500"),
    ("twii",   "Taiwan Weighted",    "台湾加权"),
    ("kospi",  "KOSPI",              "韩国综合"),
    ("n225",   "Nikkei 225",         "日经225"),
    ("ftse",   "FTSE 100",           "英国富时100"),
]

# The US indices, shown in a CN report only as the overnight handover.
_INDEX_ROWS_US_OVERNIGHT: list[tuple[str, str, str]] = [
    ("spx",  "S&P 500",   "标普500"),
    ("ixic", "Nasdaq",    "纳斯达克"),
    ("dji",  "Dow Jones", "道琼斯"),
]

# Sources that are US-only by construction. In a CN report these are *not
# applicable* rather than unavailable — a distinction worth keeping, because
# "DATA UNAVAILABLE" reads as a failed fetch and invites the model to apologise
# for missing US housing figures in a report about Chinese equities.
_US_ONLY_SOURCES = ("fed", "fedwatch", "housing", "valuation", "evaluation",
                    "holdings13f", "consensus13f", "breadth", "fg", "pcr",
                    "aaii", "skew", "movers", "credit_spread")


def _sec_index_table(markets: Optional[dict], rows_spec: list, title: str,
                     source: str) -> list[str]:
    """One index table for an arbitrary set of index keys."""
    out = ["", f"=== {title} (source: {source}) ==="]
    if not markets:
        return out + _unavailable(title)
    idx = markets.get("indices") or {}
    rows = []
    for key, en, zh in rows_spec:
        d = idx.get(key) or {}
        if not isinstance(d, dict) or d.get("error") or not _isnum(d.get("current")):
            continue
        rows.append([
            f"{en} / {zh}",
            _num(d.get("current")),
            _pct(d.get("day_chg")),
            _pct(d.get("ytd")),
            _num(d.get("hi52")),
            _num(d.get("lo52")),
            _pos_in_range(d.get("current"), d.get("lo52"), d.get("hi52")),
            _num(d.get("rsi14"), 1),
            _num(d.get("ma50")),
            _num(d.get("ma200")),
        ])
    if not rows:
        return out + _unavailable(title)
    out.append("52w range position = where price sits between the 52-week low and high:")
    out += _table(["Index", "Last", "Day %", "YTD %", "52w High", "52w Low",
                   "52w Range Pos", "RSI14", "MA50", "MA200"], rows)
    return out


def _sec_asia_rates(yield_curve: Optional[dict]) -> list[str]:
    """CN and JP 10-year yields, with the US curve as the external driver."""
    out = ["", "=== 0. ASIA RATES & THE GLOBAL BACKDROP (source: /markets) ==="]
    if not yield_curve:
        return out + _unavailable("Yield curves")
    rows = []
    for country, label in (("cn", "China 10Y"), ("jp", "Japan 10Y"), ("us", "US 10Y")):
        y = ((yield_curve.get(country) or {}).get("current") or {}).get("10Y")
        if _isnum(y):
            rows.append([label, f"{y:.2f}%"])
    if rows:
        out.append("Ten-year government yields:")
        out += _table(["Market", "Yield"], rows)
    us = (yield_curve.get("us") or {}).get("current") or {}
    y10, y2 = us.get("10Y"), us.get("2Y")
    if _isnum(y10) and _isnum(y2):
        sp = y10 - y2
        out.append(f"US 10Y-2Y spread: {sp:+.2f}pp — "
                   f"{'inverted' if sp < 0 else 'normal'}. The US curve sets the "
                   f"global discount rate and the dollar, so it drives Asian flows "
                   f"regardless of local policy.")
    cn10 = ((yield_curve.get("cn") or {}).get("current") or {}).get("10Y")
    if _isnum(cn10) and _isnum(y10):
        out.append(f"US-China 10Y differential: {y10 - cn10:+.2f}pp "
                   f"(wider favours the dollar and pressures the renminbi).")
    return out


def _sec_asia_events(events: Optional[list]) -> list[str]:
    """The economic calendar, filtered to Asia-Pacific and Europe."""
    out = ["", "=== 0. UPCOMING ASIA / EUROPE ECONOMIC EVENTS (source: /markets) ==="]
    if not events:
        return out + _unavailable("Economic calendar")
    keep = {"CN", "JP", "KR", "TW", "HK", "IN", "AU", "NZ", "SG", "EU", "GB", "DE"}
    rows = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if (e.get("country") or "").upper() not in keep:
            continue
        rows.append([str(e.get("date") or "?"), str(e.get("time") or ""),
                     str(e.get("country") or ""), str(e.get("event") or "?"),
                     str(e.get("impact") or ""), str(e.get("forecast") or "—"),
                     str(e.get("previous") or "—")])
        if len(rows) >= MAX_EVENTS:
            break
    if not rows:
        out.append("No Asia-Pacific or European entries in the calendar window. "
                   "US releases still move Asian markets overnight; the US events "
                   "section is not part of this report.")
        return out
    out += _table(["Date", "Time", "Country", "Event", "Impact",
                   "Forecast", "Previous"], rows)
    return out


def _sec_not_applicable(sources: dict[str, Any]) -> list[str]:
    """State plainly which dashboards do not bear on an Asia-Pacific report."""
    present = [k for k in _US_ONLY_SOURCES if sources.get(k)]
    if not present:
        return []
    return [
        "",
        "=== SOURCES DELIBERATELY EXCLUDED ===",
        "This site also tracks the Federal Reserve balance sheet, FOMC pricing, "
        "US housing, US index multiples, US sector valuation, 13F institutional "
        "holdings and US sentiment gauges. They are available and current, and are "
        "excluded here because this is an Asia-Pacific report, not because they "
        "are missing. Do not describe them as unavailable, and do not report their "
        "figures as though they were Asian data.",
    ]


def _renumber_sections(lines: list[str]) -> list[str]:
    """Renumber ``=== N. TITLE ===`` headers sequentially from 1.

    The section builders are shared between the two reports and carry the US
    report's numbering in their headers, so an Asia-Pacific snapshot that reuses
    the commodities builder came out numbered 1, 2, 4. The numbers are the only
    thing tying a snapshot section to the numbered plan in the prompt, so leaving
    a gap invites the model to invent a third section to fill it.

    Headers with no number — the "sources deliberately excluded" note — are left
    alone; they are commentary, not data sections.
    """
    import re as _re
    numbered = _re.compile(r"^=== (\d+)\.\s*(.*?)\s*===$")
    out, n = [], 0
    for line in lines:
        m = numbered.match(line)
        if m:
            n += 1
            out.append(f"=== {n}. {m.group(2)} ===")
        else:
            out.append(line)
    return out


def build_snapshot(sources: dict[str, Any], today_iso: str,
                   market: str = "us") -> str:
    """Render the collected sources into one plain-text snapshot.

    ``sources`` is what ``routes._collect_brief_sources`` returns; any key may
    be ``None``, which produces an explicit "unavailable" line rather than a
    missing section. ``sources["_stale"]`` names the sources served past their
    TTL, which are used but dated.

    ``market`` selects the scope. ``"us"`` is the whole-site brief. ``"cn"`` is
    Asia-Pacific: the same collected data, but only the parts that bear on Asian
    markets, plus the US indices as the overnight handover — Asia trades off Wall
    Street's close, so omitting it would leave the most important driver out.
    """
    if market not in MARKETS:
        market = "us"
    scope = ("every dashboard on this site" if market == "us"
             else "the Asia-Pacific-relevant parts of this site's dashboards")
    lines: list[str] = [
        f"DATE: {today_iso}",
        f"REPORT SCOPE: {'United States' if market == 'us' else 'Asia-Pacific'}",
        "",
        f"The following is a snapshot of {scope}. "
        "All numbers below are measured; treat them as the only facts you have.",
    ]
    stale = sources.get("_stale") or []
    if stale:
        lines += [
            "",
            f"NOTE — these sources are being served past their refresh window and "
            f"may lag the market: {', '.join(sorted(stale))}. Each section states "
            f"its own as-of date; cite that date rather than implying the figure "
            f"is from today.",
        ]

    if market == "cn":
        lines += _sec_index_table(sources.get("markets"), _INDEX_ROWS_CN,
                                  "1. ASIA-PACIFIC INDICES", "/markets")
        lines += _sec_index_table(sources.get("markets"), _INDEX_ROWS_US_OVERNIGHT,
                                  "2. US OVERNIGHT HANDOVER", "/markets")
        lines += _sec_commodities(sources.get("commodities"))
        lines += _sec_asia_rates(sources.get("yield_curve"))
        lines += _sec_asia_events(sources.get("events"))
        lines = _renumber_sections(lines)
        lines += _sec_not_applicable(sources)
    else:
        lines += _sec_markets(sources.get("markets"), sources.get("breadth"),
                              sources.get("movers"), sources.get("fg"),
                              sources.get("pcr"), sources.get("aaii"),
                              sources.get("skew"))
        lines += _sec_rates(sources.get("yield_curve"), sources.get("credit_spread"),
                            sources.get("yield_spread"))
        lines += _sec_evaluation(sources.get("evaluation"))
        lines += _sec_commodities(sources.get("commodities"))
        lines += _sec_13f(sources.get("holdings13f"), sources.get("consensus13f"))
        lines += _sec_fedwatch(sources.get("fedwatch"))
        lines += _sec_housing(sources.get("housing"))
        lines += _sec_multiples(sources.get("valuation"))
        lines += _sec_fed(sources.get("fed"), sources.get("fed_series_meta"))
        lines += _sec_events(sources.get("events"))

    missing = [k for k, v in sources.items() if not v and k not in _META_KEYS]
    if missing:
        log.info("Market brief (%s): %d/%d sources cold: %s", market,
                 len(missing), len(sources) - len(_META_KEYS), ", ".join(sorted(missing)))
    return "\n".join(lines)


_SECTIONS_US_EN = """1. Indices & Breadth — index levels, day/YTD moves, RSI, breadth, sector performance, top movers
2. Valuation & Multiples — index multiples, forward P/E, percentile ranks, sector medians
3. Commodities & the Dollar — metals, energy, agriculture, DXY, cross ratios
4. Institutional Positioning (13F) — consensus positions, largest funds, notable changes
5. Fed Policy Watch — implied path, cut/hold/hike probabilities by meeting
6. Rates & Credit — the Treasury curve, curve spreads, credit spread proxy
7. US Housing — prices, rents, supply, affordability, metro detail
8. Fed Balance Sheet & Liquidity — assets, TGA, reverse repos, net liquidity, real rates
9. Forward Look & Risks — what to watch, using the economic calendar"""

_SECTIONS_US_ZH = """1. 指数与市场广度 —— 指数点位、日内与年初至今涨跌、RSI、广度、板块表现、涨跌幅前列个股
2. 估值与倍数 —— 指数估值倍数、前瞻市盈率、历史分位、板块中位数
3. 商品与美元 —— 贵金属、能源、农产品、美元指数、交叉比率
4. 机构持仓（13F）—— 共识持仓、最大基金、显著变动
5. 联储政策观察 —— 隐含利率路径、各次会议降息/维持/加息概率
6. 利率与信用 —— 美债收益率曲线、曲线利差、信用利差代理指标
7. 美国房地产 —— 房价、租金、库存、可负担性、都会区明细
8. 联储资产负债表与流动性 —— 总资产、TGA、逆回购、净流动性、实际利率
9. 前瞻与风险 —— 结合经济日历，指出需要关注的事项"""


_SECTIONS_CN_EN = """1. Asia-Pacific Indices — Shanghai, CSI 500, Taiwan, KOSPI, Nikkei, FTSE: levels, day/YTD moves, RSI, position in 52-week range
2. The US Handover — how Wall Street closed and what that sets up for the Asian session
3. Commodities & the Dollar — metals, energy, agriculture, DXY, cross ratios, read for Asian importers and exporters
4. Rates & the Global Backdrop — China and Japan 10-year yields, the US curve, the US-China differential and its currency implication
5. Forward Look & Risks — what to watch in the Asia-Pacific session, using the economic calendar"""

_SECTIONS_CN_ZH = """1. 亚太股指 —— 上证、中证500、台湾加权、韩国综合、日经225、英国富时：点位、日内与年初至今涨跌、RSI、52周区间位置
2. 美股隔夜交接 —— 美股收盘情况，以及对亚洲交易时段的指向
3. 商品与美元 —— 贵金属、能源、农产品、美元指数、交叉比率，并从亚洲进出口国的角度解读
4. 利率与全球环境 —— 中国与日本10年期国债收益率、美债曲线、中美利差及其对汇率的含义
5. 前瞻与风险 —— 结合经济日历，指出亚太时段需要关注的事项"""


def _sections_for(market: str, lang: str) -> str:
    if market == "cn":
        return _SECTIONS_CN_ZH if lang == "zh" else _SECTIONS_CN_EN
    return _SECTIONS_US_ZH if lang == "zh" else _SECTIONS_US_EN


def build_prompt(snapshot: str, lang: str, market: str = "us") -> str:
    """Wrap a snapshot in the brief instructions.

    The output is Markdown on purpose: /markets and /daily both render it through
    ``static/markdown.js``, which supports pipe tables. The instructions insist
    on tables because the whole point of this brief is the numbers — prose
    alone loses the ability to compare two rows at a glance.

    ``market`` picks the section plan and the framing: ``"us"`` is the nine-section
    whole-site brief, ``"cn"`` the five-section Asia-Pacific report. The CN variant
    additionally forbids treating the deliberately-excluded US dashboards as
    missing data, since they are present and current and simply not the subject.
    """
    if market not in MARKETS:
        market = "us"
    zh = lang == "zh"
    sections = _sections_for(market, lang)
    n = len(sections.strip().splitlines())
    tabled = n - 1              # the last section is the forward look: bullets

    if zh:
        return (
            "你是一位资深的市场策略分析师，正在为专业投资者撰写每日市场简报。"
            "请仅依据下方快照中的数据写作。\n\n"
            "输出要求：\n"
            f"- 使用 Markdown。共分为以下 {n} 个部分，每部分以 `## ` 二级标题开头：\n{sections}\n"
            f"- 第 1 至第 {tabled} 部分中，**凡是有数据的部分都必须包含一个 Markdown 管道表格**，"
            "把该部分最关键的数据列出来（表头用中文）。可以直接复用快照中的表格，"
            "但要挑选最重要的行与列，不要照抄全部。\n"
            "- 每个表格后面写 2-3 句解读：这些数字说明了什么、彼此之间是否矛盾。"
            "不要只是把表格用文字重复一遍。\n"
            f"- 第 {n} 部分用 3-5 个要点列出前瞻与风险，并标注对应的数据依据。\n"
            "- 全文控制在 1200-1600 字。\n"
            "- 引用数字时必须与快照完全一致，包括正负号与单位。"
            "**绝对不要编造快照中没有的数字。**\n"
            "- 如果某部分的数据被标注为 DATA UNAVAILABLE，该部分只写一行斜体说明"
            "（例如 `*本节数据暂不可用。*`）后即结束。"
            "**不要为缺失的数据生成表格，不要罗列 DATA UNAVAILABLE 占位行，"
            "也不要用「该指标为何重要」之类的段落充数。**\n"
            + ("- 本报告聚焦亚太市场。快照中「SOURCES DELIBERATELY EXCLUDED」列出的"
               "美国专属数据（联储资产负债表、FOMC定价、美国房地产、美股估值倍数、13F持仓、"
               "美股情绪指标）是**有意不纳入**，不是缺失。不要说它们不可用，"
               "也不要把美国数据当作亚洲数据来引用。美股仅作为隔夜外部驱动来讨论。\n"
               if market == "cn" else "")
            + "- 语气专业、直接、中立。不要免责声明，不要投资建议，不要寒暄。\n\n"
            f"市场快照：\n{snapshot}"
        )
    return (
        "You are a senior market strategist writing the daily brief for professional "
        "investors. Write only from the data in the snapshot below.\n\n"
        "Output requirements:\n"
        f"- Use Markdown. Exactly these {n} sections, each opening with a `## ` heading:\n{sections}\n"
        f"- In sections 1 through {tabled}, **every section that has data must contain a Markdown "
        "pipe table** carrying that section's key numbers. You may reuse the snapshot's "
        "tables, but select the rows and columns that matter — do not copy them wholesale.\n"
        "- After each table, write 2-3 sentences of interpretation: what the numbers mean "
        "and where they disagree with each other. Do not restate the table in prose.\n"
        f"- Section {n} is 3-5 bullets of forward look and risks, each naming the data it rests on.\n"
        "- Keep the whole brief between 1,200 and 1,600 words.\n"
        "- Every number you cite must match the snapshot exactly, including sign and unit. "
        "**Never invent a number that is not in the snapshot.**\n"
        "- Where a section's data is marked DATA UNAVAILABLE, write one italic line for that "
        "section (e.g. `*Data for this section is unavailable.*`) and stop. **Do not build a "
        "table of DATA UNAVAILABLE placeholders, and do not pad the section with a paragraph "
        "on why the missing metric matters.**\n"
        + ("- This is an Asia-Pacific report. The US-only data listed under SOURCES "
           "DELIBERATELY EXCLUDED — Fed balance sheet, FOMC pricing, US housing, US "
           "index multiples, 13F holdings, US sentiment gauges — is excluded **by "
           "choice**, not missing. Do not call it unavailable, and do not cite US "
           "figures as though they were Asian ones. US equities appear only as the "
           "overnight external driver.\n" if market == "cn" else "")
        + "- Tone: professional, direct, neutral. No disclaimers, no investment advice, "
        "no preamble.\n\n"
        f"Market snapshot:\n{snapshot}"
    )
