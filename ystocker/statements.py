"""
ystocker.statements
~~~~~~~~~~~~~~~~~~~
Yahoo Finance's statement tables, turned into the same payload
:func:`ystocker.xbrl.build` produces, for the listings SEC has no filings for.

Why there is a second source
----------------------------
EDGAR is the better source wherever it exists: a decade and more of quarters,
each figure traceable to the filing that stated it. But it exists only for SEC
registrants, and this site's tickers include Tokyo, Seoul, Hong Kong and
Shanghai listings -- Toyota, Samsung, Tencent, Moutai -- that file nothing in
Washington. For those, Yahoo's own statement tables (income, cash flow,
balance sheet; annual and quarterly) are what exists: four or five fiscal
years and the last four to six quarters. Shorter, and still the company's
reported figures rather than nothing.

It is a fallback only. A company SEC does cover is never mixed with Yahoo, even
where EDGAR has only annual figures: Yahoo states TSMC's EPS per ADR and EDGAR
per ordinary share, and one chart drawing both would show a five-fold jump
that is a unit change.

Pure, like :mod:`ystocker.xbrl`: plain dicts in -- ``{row: {iso date: value}}``
per table, as :func:`ystocker.fundamentals._yahoo_frames` flattens them -- and
the payload out, through :func:`ystocker.xbrl.assemble`, so the views, the
trailing sums, the margins and the valuation are computed exactly one way.

Two things that differ from the EDGAR path:

* **No split re-basing.** Yahoo restates its per-share history for splits
  (Toyota's fiscal-2023 EPS reads 179.47 yen, the post-split figure), so
  re-basing again would divide twice.
* **Valuation needs one currency.** Tencent is quoted in Hong Kong dollars and
  reports in renminbi; a P/E across the two is wrong by an exchange rate. P/E
  and P/S are computed only when the quote currency is the reporting currency.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Mapping, Optional, Sequence

from ystocker import xbrl

#: metric -> (table, Yahoo row names in order of preference, sign). Cash
#: outflows are negative in Yahoo's cash-flow table and positive payments here.
ROWS: dict[str, tuple[str, tuple[str, ...], int]] = {
    "revenue": ("income", ("Total Revenue", "Operating Revenue"), 1),
    "gross_profit": ("income", ("Gross Profit",), 1),
    "operating_income": ("income", ("Operating Income",), 1),
    "net_income": ("income", ("Net Income Common Stockholders", "Net Income"), 1),
    "eps": ("income", ("Diluted EPS", "Basic EPS"), 1),
    "shares": ("income", ("Diluted Average Shares", "Basic Average Shares"), 1),
    "rnd": ("income", ("Research And Development",), 1),
    "ocf": ("cash", ("Operating Cash Flow",), 1),
    "capex": ("cash", ("Capital Expenditure",), -1),
    "buybacks": ("cash", ("Repurchase Of Capital Stock",), -1),
    "dividends": ("cash", ("Cash Dividends Paid",), -1),
    "sbc": ("cash", ("Stock Based Compensation",), 1),
    "cash": ("balance", ("Cash And Cash Equivalents",), 1),
    "debt": ("balance", ("Total Debt",), 1),
}

#: Yahoo quote types that have no statements at all.
NOT_COMPANIES = frozenset({"ETF", "INDEX", "MUTUALFUND", "CRYPTOCURRENCY", "CURRENCY",
                           "FUTURE", "OPTION", "MONEYMARKET"})


def _series(frames: Mapping[str, Mapping[str, Mapping[str, Any]]], table: str,
            rows: Sequence[str], sign: int) -> tuple[dict[date, xbrl.Point], list[str]]:
    """One metric from one table: the first row that has a value, per date."""
    block = frames.get(table) or {}
    out: dict[date, xbrl.Point] = {}
    used: list[str] = []
    for name in rows:
        for iso, raw in (block.get(name) or {}).items():
            try:
                val = float(raw)
                when = date.fromisoformat(str(iso)[:10])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(val) or when in out:
                continue
            out[when] = xbrl.Point(when, sign * val)
            if name not in used:
                used.append(name)
    return out, used


#: How far away a share count may be borrowed from to fill a missing EPS.
SHARES_REACH_DAYS = 200


def _fill_eps(eps: dict[date, xbrl.Point], income: Mapping[date, xbrl.Point],
              shares: Mapping[date, xbrl.Point]) -> dict[date, xbrl.Point]:
    """Fill the quarters where Yahoo has net income but no EPS.

    Yahoo's quarterly tables drop EPS and the share count together in some
    quarters (Toyota: three of the last five), which leaves no four-quarter
    window for a trailing P/E at all. A diluted count moves by a percent or so a
    quarter, so the nearest one within :data:`SHARES_REACH_DAYS` stands in, and
    every figure filled this way is flagged as computed.
    """
    out = dict(eps)
    counts = sorted((d, p.val) for d, p in shares.items() if p.val > 0)
    for d, p in income.items():
        if d in out or not counts:
            continue
        near = min(counts, key=lambda c: abs((c[0] - d).days))
        if abs((near[0] - d).days) <= SHARES_REACH_DAYS:
            out[d] = xbrl.Point(d, p.val / near[1], None, "calc")
    return out


def build(frames: Mapping[str, Mapping[str, Mapping[str, Any]]], info: Mapping[str, Any], *,
          prices: Optional[Sequence[tuple[date, float]]] = None) -> dict[str, Any]:
    """The payload for one listing from Yahoo's statement tables.

    ``frames`` keys are ``income_q``, ``income_a``, ``cash_q``, ``cash_a``,
    ``balance_q`` and ``balance_a``. ``info`` supplies the quote type and the
    two currencies.
    """
    quote_type = str(info.get("quoteType") or "").upper()
    if quote_type in NOT_COMPANIES:
        return {"unavailable": "not_a_company"}
    currency = info.get("financialCurrency") or info.get("currency")
    if not currency:
        return {"unavailable": "no_statements"}

    quarterly: dict[str, dict[date, xbrl.Point]] = {}
    annual: dict[str, dict[date, xbrl.Point]] = {}
    used: dict[str, list[str]] = {}
    for metric, (table, rows, sign) in ROWS.items():
        quarterly[metric], names_q = _series(frames, f"{table}_q", rows, sign)
        annual[metric], names_a = _series(frames, f"{table}_a", rows, sign)
        used[metric] = list(dict.fromkeys(names_q + names_a))
    for view in (quarterly, annual):
        view["fcf"] = xbrl.combine(view["ocf"], view["capex"], lambda x, y: x - y, how=None)
        view["eps"] = _fill_eps(view["eps"], view["net_income"], view["shares"])
    used["fcf"] = used["ocf"] + used["capex"]

    # Yahoo names no fiscal years; the year a period ends in is the label most
    # companies use (Toyota's year to March 2026 is its fiscal 2026).
    years = {d: (d - timedelta(days=364), d.year) for m in xbrl.ANCHORS for d in annual[m]}
    quote = info.get("currency")
    block = "valuation_currency" if quote and quote != currency else None
    ends = [d for m in xbrl.ANCHORS for d in list(quarterly[m]) + list(annual[m])]
    payload = xbrl.assemble(
        quarterly, annual, years=years, labels={}, prices=prices, splits_known=True,
        valuation_block=block, used=used,
        basis={"taxonomy": "yahoo", "currency": currency, "filer": "yahoo",
               "quote_currency": quote},
        latest={"end": max(ends).isoformat()} if ends else None)
    return payload
