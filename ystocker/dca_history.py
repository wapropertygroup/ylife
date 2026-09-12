"""
ystocker.dca_history
~~~~~~~~~~~~~~~~~~~~
Where the DCA engine's percentiles come from — and there are two sources here
that must never be mixed into one number.

1. **Reconstructed history** (this module's bulk). A genuine multi-year series of
   *trailing* multiples, built by dividing the weekly price by the fundamentals
   that were actually **published as of that week**. Available on first page
   load, and the only reason ``/dca`` is useful before it has been running for a
   year.

2. **Accumulated snapshots** (``ystocker-dca-history``). One row per ticker per
   day holding the *forward*-basis figures Yahoo publishes today. This is
   observed state on the same terms as ``ystocker-valuation-history``: nothing
   sells back yesterday's consensus forward P/E, so a row not written is gone.
   It starts empty and is worth nothing for months, which is exactly why (1)
   exists.

Why they cannot be averaged, spliced or compared
------------------------------------------------
A forward P/E and a trailing P/E are different numbers about the same company,
and for anything growing the forward one is *lower*. Rank today's forward
multiple against a distribution of trailing ones and the answer is not slightly
off, it is biased in one direction — cheap — every single time, for every
growing company. That bias then flows straight into ``M_valuation`` and sizes a
larger contribution. It would look completely normal on the page.

So :func:`reconstruct` computes today's value **as the last point of its own
series**, on the same trailing basis as every historical point, rather than
reading ``forwardPE`` off Yahoo. The percentile is then a rank within one
consistent series by construction, and there is no place left to make the
mistake. The forward figures Yahoo does publish are carried alongside as
``forward_context`` and are never percentiled here.

This is the same distinction :mod:`ystocker.valuation` draws between its
bottom-up ``forward`` series and its ``fwd_realized`` one — "the same index,
neither figure wrong", and the labels are what stop them reading as a
contradiction.

Point-in-time, or it is not history
-----------------------------------
A fiscal year ending 31 Dec is not *knowable* on 1 Jan; the 10-K lands 60-90
days later. Stepping the denominator on the period-end date therefore prices
January on earnings the market could not see, which is look-ahead bias — the
classic way a backtest flatters itself. :data:`ANNUAL_LAG_DAYS` and
:data:`QUARTERLY_LAG_DAYS` hold the vintage back until it was public, and
:func:`percentile_series` ranks each week against **only the weeks before it**,
so a point's percentile never depends on its own future.

What this deliberately does not reconstruct
-------------------------------------------
``nav_premium`` and ``affo_yield`` need an appraised NAV and an AFFO
reconciliation, neither of which is in a Yahoo statement. They come back absent
rather than approximated, and :func:`ystocker.dca.expensiveness` drops them and
renormalises. A REIT therefore scores on FFO and peers, and the page says which
factors were dropped — see the module docstring of :mod:`ystocker.dca` on why
the renormalisation has a floor under it.

``peer`` is cross-sectional and has no history here at all: we know what NVDA's
peer group looks like today, not what it looked like in 2023. That is why the
page carries a headline V (all factors, today) *and* a history line computed
without the peer factor, each labelled, rather than one line whose factor set
silently changes partway along.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "ANNUAL_LAG_DAYS", "QUARTERLY_LAG_DAYS", "MIN_VINTAGES",
    "Vintage", "build_vintages", "build_quarterly_ttm", "vintage_at", "reconstruct",
    "percentile_series", "latest_percentiles",
    "snapshot_row", "load_series", "save_row",
    "universe", "cached_tickers", "warm_universe", "is_warming",
    "get", "peek", "refresh", "start_background_thread",
]

# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------

#: Days after a fiscal year end before the figures are treated as public. The
#: SEC gives a large accelerated filer 60 days for a 10-K and everyone else 75
#: or 90; 90 is chosen because erring long only costs a little responsiveness at
#: the front of the series, while erring short manufactures foresight.
ANNUAL_LAG_DAYS: int = 90

#: Same for a quarter. A 10-Q is due in 40-45 days.
QUARTERLY_LAG_DAYS: int = 45

#: Fewest distinct fundamental vintages before a reconstructed series is offered
#: at all. Under this the "history" is one or two denominators and a price
#: chart, which is the very construction ``valuation.py`` warns about — near
#: enough to a rescaled price line that calling its rank a *valuation*
#: percentile would be a misdescription.
MIN_VINTAGES: int = 3

#: Disk cache. Per ticker, so one cold symbol does not rewrite the whole file.
CACHE_DIR = Path(__file__).parent.parent / "cache" / "dca"

#: A day. Statements move quarterly; the price leg is weekly. Refetching more
#: often buys nothing and spends the Yahoo budget that ``valuation.py``'s
#: docstring records this box having exhausted once already.
TTL_SECONDS: int = 24 * 3600

#: Bump on any payload shape change. A TTL does not protect against a schema
#: change: an existing file still looks fresh and the page renders empty.
CACHE_VER = "v1"

#: How much price history to pull, and at what interval. Weekly over five years
#: is ~260 points — comfortably past :data:`ystocker.dca.MIN_OBSERVATIONS` while
#: staying one request.
PRICE_PERIOD = "5y"
PRICE_INTERVAL = "1wk"


# ---------------------------------------------------------------------------
# Statement row aliases
# ---------------------------------------------------------------------------
#
# Yahoo's row labels are not stable across companies or filing types, so each
# quantity is looked up through an ordered alias list and the first hit wins --
# the same header-sniffing approach ``portfolio_csv`` uses on broker exports,
# and for the same reason: one code path beats a parser per issuer, and an
# unrecognised label degrades to an absent factor rather than a wrong one.

_ROWS: dict[str, tuple[str, ...]] = {
    "revenue":   ("Total Revenue", "Operating Revenue", "Revenues"),
    "net_income": ("Net Income Common Stockholders", "Net Income",
                   "Net Income From Continuing Operation Net Minority Interest"),
    "eps":       ("Diluted EPS", "Basic EPS"),
    "ebitda":    ("EBITDA", "Normalized EBITDA"),
    "debt":      ("Total Debt", "Total Debt And Capital Lease Obligation"),
    "cash":      ("Cash And Cash Equivalents",
                  "Cash Cash Equivalents And Short Term Investments",
                  "Cash Financial"),
    "tangible_book": ("Tangible Book Value", "Net Tangible Assets"),
    "shares":    ("Ordinary Shares Number", "Share Issued",
                  "Diluted Average Shares", "Basic Average Shares"),
    "dep_amort": ("Depreciation And Amortization",
                  "Depreciation Amortization Depletion",
                  "Reconciled Depreciation"),
    "fcf":       ("Free Cash Flow",),
    "ocf":       ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capex":     ("Capital Expenditure", "Purchase Of PPE"),
}


def _pick(block: Mapping[str, Any], key: str) -> Optional[float]:
    """First aliased row present in *block*, as a float, or ``None``."""
    for alias in _ROWS.get(key, ()):
        val = block.get(alias)
        if val is None:
            continue
        try:
            out = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(out):
            return out
    return None


def _fin(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _pos(value: Any) -> bool:
    return _fin(value) and float(value) > 0


# ---------------------------------------------------------------------------
# Vintages
# ---------------------------------------------------------------------------

class Vintage:
    """One set of fundamentals, and the date the market could first see it.

    ``period_end`` is when the accounting period closed; ``effective`` is when a
    reader could have acted on it. Everything downstream keys off ``effective``,
    which is the whole point of the class existing rather than a bare dict.
    """

    __slots__ = ("period_end", "effective", "kind", "revenue", "net_income",
                 "eps", "ebitda", "debt", "cash", "tangible_book", "shares",
                 "fcf", "ffo")

    def __init__(self, *, period_end: str, effective: str, kind: str,
                 revenue: Optional[float] = None,
                 net_income: Optional[float] = None,
                 eps: Optional[float] = None,
                 ebitda: Optional[float] = None,
                 debt: Optional[float] = None,
                 cash: Optional[float] = None,
                 tangible_book: Optional[float] = None,
                 shares: Optional[float] = None,
                 fcf: Optional[float] = None,
                 ffo: Optional[float] = None) -> None:
        self.period_end = period_end
        self.effective = effective
        self.kind = kind
        self.revenue = revenue
        self.net_income = net_income
        self.eps = eps
        self.ebitda = ebitda
        self.debt = debt
        self.cash = cash
        self.tangible_book = tangible_book
        self.shares = shares
        self.fcf = fcf
        self.ffo = ffo

    def as_dict(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Vintage {self.kind} {self.period_end} eff={self.effective}>"


def _lagged(period_end: str, days: int) -> Optional[str]:
    try:
        end = datetime.strptime(period_end[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (end + timedelta(days=days)).isoformat()


def build_vintages(annual: Mapping[str, Mapping[str, Any]],
                   balance: Mapping[str, Mapping[str, Any]],
                   cashflow: Mapping[str, Mapping[str, Any]],
                   *, kind: str = "annual") -> list[Vintage]:
    """Fold three statement blocks into one ordered list of vintages.

    Each argument maps a period-end date (``YYYY-MM-DD``) to that period's rows.
    Pure: no pandas, no yfinance, no clock — which is what lets
    ``tests/test_dca_history.py`` prove the point-in-time behaviour from literals.

    A period present in the income statement but missing from the balance sheet
    still produces a vintage, carrying whatever it has. The alternative is an
    inner join, which would silently shorten the window whenever Yahoo returns
    one statement a year deeper than another — and it routinely does.
    """
    lag = ANNUAL_LAG_DAYS if kind == "annual" else QUARTERLY_LAG_DAYS
    periods = sorted({*annual.keys(), *balance.keys(), *cashflow.keys()})

    out: list[Vintage] = []
    for period in periods:
        effective = _lagged(period, lag)
        if not effective:
            continue
        inc = annual.get(period) or {}
        bal = balance.get(period) or {}
        cfs = cashflow.get(period) or {}

        fcf = _pick(cfs, "fcf")
        if fcf is None:
            ocf, capex = _pick(cfs, "ocf"), _pick(cfs, "capex")
            if ocf is not None and capex is not None:
                # Yahoo signs capex negative. Adding is right; subtracting an
                # already-negative number doubles it and reports a company
                # burning cash it is in fact generating.
                fcf = ocf + capex if capex < 0 else ocf - capex

        # FFO, the REIT earnings measure: net income plus real-estate
        # depreciation. NAREIT also backs out gains on property sales, which no
        # Yahoo row exposes -- so this is FFO's standard first two terms and is
        # labelled as an approximation wherever it surfaces.
        net_income = _pick(inc, "net_income")
        dep = _pick(cfs, "dep_amort")
        ffo = (net_income + dep) if (net_income is not None and dep is not None) else None

        out.append(Vintage(
            period_end=period, effective=effective, kind=kind,
            revenue=_pick(inc, "revenue"),
            net_income=net_income,
            eps=_pick(inc, "eps"),
            ebitda=_pick(inc, "ebitda"),
            debt=_pick(bal, "debt"),
            cash=_pick(bal, "cash"),
            tangible_book=_pick(bal, "tangible_book"),
            shares=_pick(bal, "shares"),
            fcf=fcf,
            ffo=ffo,
        ))
    return out


def vintage_at(vintages: Sequence[Vintage], when: str) -> Optional[Vintage]:
    """The most recent vintage public on or before *when*, or ``None``.

    ``None`` before the first filing is deliberate and is why the reconstructed
    series starts short of the price series: the alternative is to reach forward
    for the next vintage, which is precisely the look-ahead this module exists
    to avoid.
    """
    best: Optional[Vintage] = None
    for vintage in vintages:
        if vintage.effective <= when and (best is None or vintage.effective > best.effective):
            best = vintage
    return best


#: Bounds on the span between the **first and last period end** in a four-quarter
#: window -- not on the twelve months it covers. Four consecutive quarter-ends are
#: about 273 days apart (31 Mar to 31 Dec), so the band is set around that with
#: room for 52/53-week fiscal calendars and the odd short period. Yahoo does
#: occasionally return quarters with a gap, and four quarters that are really
#: three years apart sum to a "TTM" figure that is simply wrong.
_TTM_SPAN_MIN_DAYS, _TTM_SPAN_MAX_DAYS = 240, 310


def build_quarterly_ttm(quarterly: Mapping[str, Mapping[str, Any]]) -> list[Vintage]:
    """Rolling four-quarter vintages from a quarterly income statement.

    **The flows must be summed and the reason is a factor-of-four error.** Yahoo's
    quarterly income statement reports *that quarter's* EPS, not a trailing
    twelve-month one. Feeding it straight into ``price / eps`` reports a P/E about
    four times too high, which for a mega-cap is 90x instead of 23x -- and because
    it lands in the same series as the annual points, the reconstructed history
    gains a sawtooth that reads as genuine multiple expansion. Every percentile
    downstream would then be measured against it.

    Only flows are summed. Debt, cash, share count and book value are *stocks* --
    balances at an instant -- and summing four of them would report four times the
    company. They are absent here and get filled from the annual vintage in force,
    which is the closest honest reading available at that date.

    A window whose quarters are not actually consecutive is skipped rather than
    summed, and a window missing one row of a measure yields ``None`` for that
    measure rather than a three-quarter sum -- which would understate TTM by a
    quarter and read as a cheap stock.
    """
    periods = sorted(quarterly.keys())
    out: list[Vintage] = []
    for i in range(3, len(periods)):
        window = periods[i - 3:i + 1]
        try:
            start = datetime.strptime(window[0][:10], "%Y-%m-%d").date()
            end = datetime.strptime(window[-1][:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if not (_TTM_SPAN_MIN_DAYS <= (end - start).days <= _TTM_SPAN_MAX_DAYS):
            continue

        totals: dict[str, Optional[float]] = {}
        for key in ("revenue", "net_income", "eps", "ebitda"):
            values = [_pick(quarterly[p] or {}, key) for p in window]
            totals[key] = round(sum(values), 6) if all(v is not None for v in values) else None

        effective = _lagged(window[-1], QUARTERLY_LAG_DAYS)
        if not effective:
            continue
        out.append(Vintage(period_end=window[-1], effective=effective, kind="quarterly",
                           revenue=totals["revenue"], net_income=totals["net_income"],
                           eps=totals["eps"], ebitda=totals["ebitda"]))
    return out


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

#: Factors this module can rebuild from Yahoo statements. Anything in
#: ``dca.DIRECTION`` and absent here is simply never produced, which
#: ``dca.expensiveness`` handles by dropping and renormalising.
RECONSTRUCTED: tuple[str, ...] = (
    "pe", "pfcf", "ev_ebitda", "ev_sales", "ptbv", "pffo",
    "fcf_yield", "peg", "ev_sales_growth",
    "cycle_adjusted", "mid_cycle", "normalized_margin",
)


#: How far apart two period ends may be and still be treated as a year apart,
#: for the growth terms. Wide enough for 52/53-week fiscal calendars, narrow
#: enough to exclude the adjacent quarter.
_YOY_MIN_DAYS, _YOY_MAX_DAYS = 300, 430


def _year_ago(seen: Sequence[Vintage], current: Vintage) -> Optional[Vintage]:
    """The vintage roughly twelve months before *current*, or ``None``.

    **Not** the previous element of the list, and that distinction is a factor of
    four. Once quarterly TTM vintages are interleaved with annual ones the
    neighbour is often only three months back, so ``current.eps / previous.eps``
    measures a quarter's growth and hands it to PEG as if it were annual --
    ~3.7% where the truth is ~10%, which inflates PEG about 2.7x and reads as a
    far more expensive stock. PEG carries 15-25% of the weight in three
    templates, so it moves the score materially and silently.

    The other half is duplicates: a quarterly TTM ending 31 Dec and the annual
    for the same year are the *same period* with different publication dates, so
    an adjacent-pair comparison yields exactly 0% growth.

    Returns ``None`` rather than the nearest available when nothing sits in the
    window. A growth rate over the wrong interval is not a rough answer, it is a
    different quantity.
    """
    try:
        end = datetime.strptime(current.period_end[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

    best: Optional[Vintage] = None
    best_gap = None
    for vintage in seen:
        if vintage is current:
            continue
        try:
            other = datetime.strptime(vintage.period_end[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        gap = (end - other).days
        if not (_YOY_MIN_DAYS <= gap <= _YOY_MAX_DAYS):
            continue
        # Closest to exactly a year wins, so an annual and a quarterly TTM both
        # in the window do not depend on list order.
        distance = abs(gap - 365)
        if best_gap is None or distance < best_gap:
            best, best_gap = vintage, distance
    return best


def reconstruct(prices: Sequence[tuple[str, float]],
                vintages: Sequence[Vintage]) -> dict[str, list[tuple[str, float]]]:
    """Weekly multiples from weekly prices and point-in-time fundamentals.

    *prices* is ``[(iso_date, close), ...]`` ascending. Returns
    ``{factor: [(iso_date, value), ...]}`` holding only the factors that could be
    computed, and only the weeks where the inputs were public, positive and
    economically meaningful.

    Negative and zero denominators are **skipped, never clamped**. A company with
    negative earnings has no P/E — not a very high one and certainly not a very
    low one — and a clamp at either end would put a fabricated point into the
    distribution every other point is ranked against.
    """
    if not vintages:
        return {}

    ordered = sorted(vintages, key=lambda v: v.effective)
    # Mid-cycle and normalized inputs are averages over the window. They use the
    # vintages public *at each point*, expanding, so an early week is not
    # normalised against earnings reported years later.
    out: dict[str, list[tuple[str, float]]] = {key: [] for key in RECONSTRUCTED}

    for stamp, close in prices:
        if not _pos(close):
            continue
        current = vintage_at(ordered, stamp)
        if current is None:
            continue
        seen = [v for v in ordered if v.effective <= stamp]

        shares = current.shares
        cap = close * shares if _pos(shares) else None
        ev = None
        if cap is not None and _fin(current.debt) and _fin(current.cash):
            ev = cap + float(current.debt) - float(current.cash)
            if not ev > 0:
                ev = None

        # --- earnings-based -------------------------------------------------
        if _pos(current.eps):
            pe = close / float(current.eps)
            out["pe"].append((stamp, round(pe, 4)))

            # PEG needs a growth rate, and the only honest one here is realised:
            # this vintage's EPS against the vintage a *year* earlier, not the
            # adjacent one -- see _year_ago on why the neighbour is often one
            # quarter back and inflates PEG about 2.7x.
            prior = _year_ago(seen, current)
            if prior is not None and _pos(prior.eps):
                growth = (float(current.eps) / float(prior.eps) - 1.0) * 100.0
                if growth > 0:
                    out["peg"].append((stamp, round(pe / growth, 4)))

        # A mini-CAPE: price over mean EPS across every vintage public so far.
        eps_seen = [float(v.eps) for v in seen if _pos(v.eps)]
        if len(eps_seen) >= MIN_VINTAGES:
            mean_eps = sum(eps_seen) / len(eps_seen)
            if mean_eps > 0:
                cyclical = round(close / mean_eps, 4)
                out["cycle_adjusted"].append((stamp, cyclical))
                out["mid_cycle"].append((stamp, cyclical))

        # --- cash-flow-based ------------------------------------------------
        if cap is not None and _pos(current.fcf):
            out["pfcf"].append((stamp, round(cap / float(current.fcf), 4)))
            out["fcf_yield"].append((stamp, round(float(current.fcf) / cap * 100.0, 4)))

        # --- enterprise-value-based ------------------------------------------
        if ev is not None and _pos(current.ebitda):
            out["ev_ebitda"].append((stamp, round(ev / float(current.ebitda), 4)))
        if ev is not None and _pos(current.revenue):
            evs = ev / float(current.revenue)
            out["ev_sales"].append((stamp, round(evs, 4)))
            prior = _year_ago(seen, current)
            if prior is not None and _pos(prior.revenue):
                growth = (float(current.revenue) / float(prior.revenue) - 1.0) * 100.0
                if growth > 0:
                    out["ev_sales_growth"].append((stamp, round(evs / growth, 4)))

        # --- book- and FFO-based ---------------------------------------------
        if cap is not None and _pos(current.tangible_book):
            out["ptbv"].append((stamp, round(cap / float(current.tangible_book), 4)))
        if cap is not None and _pos(current.ffo):
            out["pffo"].append((stamp, round(cap / float(current.ffo), 4)))

        # --- margin-normalised -----------------------------------------------
        # Price over what this revenue would earn at the window's median net
        # margin. Catches a company being cheap only because margins are at a
        # peak that will not hold, which is the trap the framework's Healthcare
        # and Consumer template adds this factor for.
        margins = [float(v.net_income) / float(v.revenue)
                   for v in seen if _pos(v.revenue) and _fin(v.net_income)]
        if cap is not None and len(margins) >= MIN_VINTAGES and _pos(current.revenue):
            margins_sorted = sorted(margins)
            median = margins_sorted[len(margins_sorted) // 2]
            if median > 0:
                normalised = float(current.revenue) * median
                out["normalized_margin"].append((stamp, round(cap / normalised, 4)))

    return {key: rows for key, rows in out.items() if rows}


# ---------------------------------------------------------------------------
# Percentiles over a reconstructed series
# ---------------------------------------------------------------------------

def percentile_series(rows: Sequence[tuple[str, float]],
                      *, minimum: int) -> list[tuple[str, float]]:
    """Expanding-window percentile rank, with no look-ahead.

    The rank at week *t* uses observations up to and including *t* and nothing
    after it, so the line is what a reader would have seen on the day rather
    than a restatement of the past in light of the present. Ranking every point
    against the *whole* series is the tempting alternative and is what makes a
    backtest look prescient: a 2022 trough only scores as a trough because 2024
    is in the denominator.

    Points before *minimum* observations accumulate are omitted rather than
    reported against a short window.
    """
    from ystocker.dca import percentile_rank

    out: list[tuple[str, float]] = []
    window: list[float] = []
    for stamp, value in rows:
        window.append(float(value))
        if len(window) < minimum:
            continue
        pct = percentile_rank(value, window, minimum=minimum)
        if pct is not None:
            out.append((stamp, pct))
    return out


def latest_percentiles(series: Mapping[str, Sequence[tuple[str, float]]],
                       *, minimum: int) -> dict[str, Optional[float]]:
    """Each factor's current percentile: the last point ranked in its own series.

    This is where the basis stays consistent. The numerator of the "current"
    reading is the same weekly close and the same published fundamentals that
    produced every historical point, so a rank within the series is a rank
    against like — no forward figure is smuggled in, because there is nowhere to
    put one.
    """
    from ystocker.dca import percentile_rank

    out: dict[str, Optional[float]] = {}
    for factor, rows in series.items():
        values = [float(v) for _, v in rows]
        out[factor] = percentile_rank(values[-1], values, minimum=minimum) if values else None
    return out


def window_meta(series: Mapping[str, Sequence[tuple[str, float]]],
                vintages: Sequence[Vintage]) -> dict[str, Any]:
    """What the distribution is actually made of, for the page to state.

    Observation count alone overstates the evidence: 260 weekly points built on
    four annual filings are four independent denominators and a price line, and
    a reader deciding how much to trust a percentile needs the second number as
    well as the first.
    """
    stamps = sorted({stamp for rows in series.values() for stamp, _ in rows})
    return {
        "start": stamps[0] if stamps else None,
        "end": stamps[-1] if stamps else None,
        "observations": len(stamps),
        "vintages": len(vintages),
        "years": round((len(stamps) / 52.0), 1) if stamps else 0.0,
        "basis": "trailing",
    }


# ---------------------------------------------------------------------------
# Yahoo: five calls, once a day, never on the request path
# ---------------------------------------------------------------------------
#
# Cost is the whole design constraint here. ``valuation.py``'s docstring records
# what happened the last time this box fetched per-symbol fundamentals in bulk:
# "a first run returned 500/503, later runs returned 0/503 with HTTP 401 Invalid
# Crumb", and being throttled takes quotes, breadth and the heatmap down with it.
# So this is five calls for one ticker, only when somebody actually opens its
# page, cached on disk for a day, behind the same provider circuit breaker and
# per-symbol back-off every other Yahoo caller here uses.

def _frame_to_blocks(frame: Any) -> dict[str, dict[str, Any]]:
    """A yfinance statement DataFrame as ``{period_end: {row_label: value}}``.

    Isolated so every caller below stays free of pandas and the tests can hand
    :func:`build_vintages` plain literals.
    """
    out: dict[str, dict[str, Any]] = {}
    if frame is None:
        return out
    try:
        if getattr(frame, "empty", True):
            return out
        for column in frame.columns:
            try:
                stamp = column.date().isoformat()
            except AttributeError:
                stamp = str(column)[:10]
            block: dict[str, Any] = {}
            for label in frame.index:
                value = frame.loc[label, column]
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    block[str(label)] = number
            if block:
                out[stamp] = block
    except Exception as exc:  # noqa: BLE001 - a malformed frame must not 500 the page
        log.warning("dca_history: could not read statement frame: %s", exc)
    return out


def _fetch_raw(ticker: str) -> dict[str, Any]:
    """The six Yahoo reads, as plain dicts. Raises on a hard failure."""
    import yfinance as yf

    from ystocker import data as ydata, fetchguard

    try:
        fetchguard.guard(ydata.PROVIDER)
    except fetchguard.CooldownActive as exc:
        raise ydata.FetchError(str(exc)) from exc

    tk = yf.Ticker(ticker)
    try:
        info = tk.info or {}
        annual_inc = _frame_to_blocks(tk.income_stmt)
        annual_bal = _frame_to_blocks(tk.balance_sheet)
        annual_cfs = _frame_to_blocks(tk.cashflow)
        quarterly_inc = _frame_to_blocks(tk.quarterly_income_stmt)
        hist = tk.history(period=PRICE_PERIOD, interval=PRICE_INTERVAL)
    except Exception as exc:
        if ydata._looks_rate_limited(exc):
            fetchguard.trip(ydata.PROVIDER,
                            fetchguard.FETCH_RATE_LIMIT_COOLDOWN_SECONDS,
                            "yfinance rate limit (dca)")
        raise ydata.FetchError(f"Could not rebuild DCA history for {ticker}: {exc}") from exc

    prices: list[tuple[str, float]] = []
    try:
        if hist is not None and not hist.empty and "Close" in hist:
            for stamp, close in hist["Close"].items():
                try:
                    value = float(close)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    prices.append((stamp.date().isoformat(), round(value, 4)))
    except Exception as exc:  # noqa: BLE001
        log.warning("dca_history: %s price history unreadable: %s", ticker, exc)

    return {
        "info": {k: info.get(k) for k in _FORWARD_KEYS},
        "annual_income": annual_inc,
        "annual_balance": annual_bal,
        "annual_cashflow": annual_cfs,
        "quarterly_income": quarterly_inc,
        "prices": prices,
    }


# ---------------------------------------------------------------------------
# Disk cache, one file per ticker
# ---------------------------------------------------------------------------

_disk_lock = threading.Lock()
_mem: dict[str, tuple[float, dict[str, Any]]] = {}
_mem_lock = threading.Lock()


def _safe_name(ticker: str) -> str:
    """A filename that cannot escape :data:`CACHE_DIR`.

    ``ticker`` reaches this from a URL path segment and Flask does not decode
    ``%2f`` into a separator, but ``BRK-B``, ``7203.T`` and ``005930.KQ`` all
    have to survive intact, so this is an allowlist rather than a blocklist.
    """
    keep = [c for c in ticker.upper() if c.isalnum() or c in "-._"]
    return "".join(keep)[:24] or "_"


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{_safe_name(ticker)}.json"


def _read_disk(ticker: str, *, ignore_ttl: bool = False) -> Optional[dict[str, Any]]:
    path = _cache_path(ticker)
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.debug("dca_history: unreadable cache for %s: %s", ticker, exc)
        return None
    if payload.get("_ver") != CACHE_VER:
        return None
    if not ignore_ttl:
        stamp = payload.get("_ts")
        if not isinstance(stamp, (int, float)) or (time.time() - stamp) > TTL_SECONDS:
            return None
    return payload


def _write_disk(ticker: str, payload: Mapping[str, Any]) -> None:
    """Atomic temp-file + replace, matching every other cache writer here."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _disk_lock:
            fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(payload, handle)
                os.replace(tmp, _cache_path(ticker))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail a page
        log.warning("dca_history: could not persist %s: %s", ticker, exc)


# ---------------------------------------------------------------------------
# The accumulating forward-basis series (DynamoDB)
# ---------------------------------------------------------------------------
#
# Observed state, on the same terms as ``ystocker-valuation-history``: nothing
# upstream sells back yesterday's consensus forward P/E, so a row not written is
# lost permanently. Unlike that table this one is keyed ``ticker`` HASH + ``date``
# RANGE rather than a bare ``date``, so reading one symbol is a Query rather than
# a full-table Scan -- on PAY_PER_REQUEST a scan is billed by volume scanned, and
# a per-ticker page would pay for every other ticker's rows on every load.

TABLE_NAME = os.environ.get("DCA_HISTORY_TABLE", "ystocker-dca-history").strip()

#: The forward-basis fields banked each day, and the ``ticker_cache.json`` key
#: each comes from. Derived fields (``pfcf``, ``fcf_yield``) are computed in
#: :func:`snapshot_row` from market cap and FCF, which that cache already holds.
_SNAPSHOT_FIELDS: dict[str, str] = {
    "pe": "PE (Forward)",
    "pe_ttm": "PE (TTM)",
    "ev_ebitda": "EV/EBITDA",
    "peg": "PEG",
    "pb": "P/B Ratio",
    "ps": "P/S Ratio",
    "dividend_yield": "Dividend Yield (%)",
}

_table = None
_table_unavail_until = 0.0
_TABLE_LOCK = threading.Lock()


def _get_table():
    """The DynamoDB table, or ``None`` when it is not reachable.

    Degrades rather than raising, matching ``valuation._get_hist_table``: local
    dev has no credentials and the reconstructed series -- which is what the page
    actually scores on -- does not depend on this at all. The five-minute
    back-off stops every snapshot pass paying a connection timeout.
    """
    global _table, _table_unavail_until
    if _table is not None:
        return _table
    if time.time() < _table_unavail_until:
        return None
    with _TABLE_LOCK:
        if _table is not None:
            return _table
        if time.time() < _table_unavail_until:
            return None
        try:
            import boto3

            ddb = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(TABLE_NAME)
            tbl.load()
            _table = tbl
            log.info("dca_history: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("dca_history: DynamoDB unavailable: %s", exc)
            _table = None
            _table_unavail_until = time.time() + 300
        return _table


def snapshot_row(ticker: str, record: Mapping[str, Any],
                 *, stamp: Optional[str] = None) -> dict[str, Any]:
    """Today's forward-basis row for *ticker*, from a ``ticker_cache.json`` record.

    Pure, so the field mapping is testable without AWS. Returns ``{}`` when the
    record carries nothing worth banking -- an empty row would still occupy a
    date and make the series look longer than the evidence in it.
    """
    row: dict[str, Any] = {}
    for key, source in _SNAPSHOT_FIELDS.items():
        value = record.get(source)
        if _fin(value):
            row[key] = round(float(value), 4)

    cap = record.get("Market Cap ($B)")
    fcf = record.get("FCF ($B)")
    if _pos(cap) and _pos(fcf):
        row["pfcf"] = round(float(cap) / float(fcf), 4)
        row["fcf_yield"] = round(float(fcf) / float(cap) * 100.0, 4)

    if not row:
        return {}
    row["ticker"] = ticker.strip().upper()
    row["date"] = stamp or date.today().isoformat()
    return row


def save_row(row: Mapping[str, Any]) -> None:
    """Persist one snapshot. Floats go as strings -- DynamoDB rejects float."""
    table = _get_table()
    if table is None or not row.get("ticker") or not row.get("date"):
        return
    try:
        item: dict[str, Any] = {"ticker": row["ticker"], "date": row["date"]}
        for key, value in row.items():
            if key in ("ticker", "date") or not _fin(value):
                continue
            item[key] = str(round(float(value), 4))
        if len(item) > 2:
            table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        log.warning("dca_history: snapshot save failed for %s %s: %s",
                    row.get("ticker"), row.get("date"), exc)


def load_series(ticker: str) -> list[dict[str, Any]]:
    """Every banked snapshot for *ticker*, oldest first.

    A Query on the hash key, paginated. Without the pagination loop the series
    would silently stop at the first megabyte, which on a daily row is years in
    and therefore would not show up until long after the code shipped.
    """
    table = _get_table()
    if table is None:
        return []
    try:
        from boto3.dynamodb.conditions import Key

        rows: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("ticker").eq(ticker.strip().upper())}
        while True:
            resp = table.query(**kwargs)
            for item in resp.get("Items", []):
                row: dict[str, Any] = {"date": item.get("date")}
                if not row["date"]:
                    continue
                for key in (*_SNAPSHOT_FIELDS, "pfcf", "fcf_yield"):
                    raw = item.get(key)
                    if raw is None:
                        continue
                    try:
                        row[key] = float(raw)
                    except (TypeError, ValueError):
                        continue
                rows.append(row)
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        rows.sort(key=lambda r: r["date"])
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("dca_history: series load failed for %s: %s", ticker, exc)
        return []


# ---------------------------------------------------------------------------
# The V-score line
# ---------------------------------------------------------------------------

def v_history(series: Mapping[str, Sequence[tuple[str, float]]],
              weights: Mapping[str, float],
              *, minimum: int) -> list[dict[str, Any]]:
    """The V score week by week, point-in-time.

    Each factor is ranked in its own expanding window first, then the weights are
    folded exactly as :func:`ystocker.dca.expensiveness` folds them today -- same
    function, so the line and the headline cannot drift apart through two
    implementations of one formula.

    ``peer`` is simply absent from *series* (it is cross-sectional and has no
    history), so the adaptive rule drops it and renormalises the rest. That is
    why the last point of this line is **not** the headline V: the headline
    includes the peer factor and this does not. Both are labelled on the page,
    because the alternative -- quietly holding today's peer percentile constant
    back through 2021 -- would draw a line the data cannot support.
    """
    from ystocker.dca import expensiveness, score_band, v_score

    ranked: dict[str, dict[str, float]] = {}
    for factor in weights:
        rows = series.get(factor)
        if not rows:
            continue
        ranked[factor] = {stamp: pct
                          for stamp, pct in percentile_series(rows, minimum=minimum)}
    if not ranked:
        return []

    stamps = sorted({stamp for bydate in ranked.values() for stamp in bydate})
    out: list[dict[str, Any]] = []
    for stamp in stamps:
        percentiles = {factor: bydate.get(stamp) for factor, bydate in ranked.items()}
        fold = expensiveness(percentiles, weights)
        v = v_score(fold["E"])
        if v is None:
            continue
        out.append({"date": stamp, "v": v, "e": fold["E"],
                    "band": score_band(v),
                    "factors": len(fold["factors"])})
    return out


def peer_percentiles(ticker: str,
                     recs: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
    """Where *ticker* sits among its peer group today, on forward P/E.

    Free: ``valuation._cached_fundamentals`` reads ``ticker_cache.json``, which
    the rolling refresher in ``routes.py`` already repopulates every five
    minutes. No Yahoo call is made here at all.

    *recs* lets a caller pass those records in. ``_cached_fundamentals`` re-reads
    and re-parses a ~170 KB file on **every** call, which is invisible for one
    ticker and quadratic for a ranked list -- twenty rows would mean twenty
    parses of the same file inside one request.

    Falls back to trailing P/E when Yahoo publishes no forward one -- but only
    when it can compare like with like, so the peer values fall back together or
    not at all. A forward multiple ranked against a bag of mixed forward and
    trailing peers is the same basis error this module exists to prevent, just
    committed across a cross-section instead of across time.
    """
    from ystocker import PEER_GROUPS
    from ystocker.dca import cross_sectional_rank
    from ystocker.valuation import _cached_fundamentals

    symbol = ticker.strip().upper()
    group = next((name for name, members in PEER_GROUPS.items() if symbol in members), None)
    if not group:
        return {"percentile": None, "group": None, "reason": "no_group"}

    if recs is None:
        recs = _cached_fundamentals()
    mine = recs.get(symbol)
    if not mine:
        return {"percentile": None, "group": group, "reason": "not_cached"}

    for field, basis in (("PE (Forward)", "forward"), ("PE (TTM)", "trailing")):
        value = mine.get(field)
        if not _pos(value):
            continue
        peers = [recs.get(t, {}).get(field) for t in PEER_GROUPS[group] if t != symbol]
        pct = cross_sectional_rank(float(value), peers)
        if pct is not None:
            usable = [p for p in peers if _pos(p)]
            return {"percentile": pct, "group": group, "basis": basis,
                    "value": round(float(value), 2), "peers": len(usable),
                    "median": round(sorted(usable)[len(usable) // 2], 2)}
    return {"percentile": None, "group": group, "reason": "no_multiple"}


def eps_drift(ticker: str) -> dict[str, Any]:
    """Fractional change in the next-year EPS consensus over the longest lookback.

    Read from ``analyst.peek()``, which already sweeps the whole peer universe
    for exactly this and caches it for a day -- so ``M_earnings`` costs nothing
    extra and, more to the point, is a *vendor-reported* revision rather than
    something inferred from our own snapshots. ``+1y`` is the period used
    because, as ``analyst.py`` puts it, current-quarter estimates barely move and
    next year is where analysts express a change of mind.

    Prefers the 90-day base and falls back to 30. Both come back on the payload,
    and the one actually used is reported as ``lookback_days`` -- a drift with no
    stated window is not interpretable, since -3% over a quarter and -3% over a
    month are different situations.

    A negative or zero base is refused rather than divided by: a company crossing
    from a consensus loss to a consensus profit produces a percentage that is
    arithmetically enormous and means nothing, which is the same guard
    ``analyst._one`` puts on ``chg30_pct``.

    An unavailable drift returns ``None`` and :func:`ystocker.dca.earnings_multiplier`
    turns that into a neutral 1.0x, never a penalty.
    """
    try:
        from ystocker import analyst
    except Exception:  # pragma: no cover - defensive
        return {"drift": None, "reason": "unavailable"}

    payload = analyst.peek() or {}
    rec = (payload.get("tickers") or {}).get(ticker.strip().upper())
    if not isinstance(rec, dict):
        return {"drift": None, "reason": "not_covered"}

    trend = (rec.get("eps_trend") or {}).get(analyst.LEAD_PERIOD) or {}
    current = trend.get("current")
    if not _fin(current):
        return {"drift": None, "reason": "no_trend"}

    for key, days in (("d90", 90), ("d30", 30)):
        prior = trend.get(key)
        if _pos(prior):
            return {"drift": round(float(current) / float(prior) - 1.0, 5),
                    "current": current, "prior": prior,
                    "period": analyst.LEAD_PERIOD, "lookback_days": days}
    return {"drift": None, "reason": "no_base"}


# ---------------------------------------------------------------------------
# Building one ticker's payload
# ---------------------------------------------------------------------------

#: ``info`` keys kept as forward-basis context. Shown beside the score and never
#: percentiled -- see this module's docstring on why ranking a forward multiple
#: against a trailing distribution is biased cheap for every growing company.
_FORWARD_KEYS = ("forwardPE", "trailingPE", "forwardEps", "trailingEps",
                 "pegRatio", "enterpriseToEbitda", "priceToBook",
                 "marketCap", "quoteType", "sector", "industry", "shortName")


def build(ticker: str) -> dict[str, Any]:
    """Rebuild everything for one ticker. Network-bound; never call in a request.

    Six Yahoo reads, once a day per symbol, only for symbols somebody opened.
    The result is self-describing: it carries the window it was built over, how
    many fundamental vintages are behind it and which factors could not be
    rebuilt, because a percentile without those three is not checkable.
    """
    from ystocker.dca import MIN_OBSERVATIONS

    symbol = ticker.strip().upper()
    raw = _fetch_raw(symbol)

    vintages = build_vintages(raw["annual_income"], raw["annual_balance"],
                              raw["annual_cashflow"], kind="annual")
    # Quarterly adds finer steps over the last ~2 years, and it has to be summed
    # into trailing-twelve-month figures first -- see build_quarterly_ttm on the
    # factor-of-four error that skipping that produces. It carries no balance
    # sheet, so debt/cash/shares/book are filled from the annual vintage in force
    # at the same date; those are stocks, and the annual reading is the closest
    # honest one available. A quarterly vintage that cannot be given a share
    # count is still useful for P/E, which needs none.
    quarterly = build_quarterly_ttm(raw["quarterly_income"])
    for q in quarterly:
        annual_at = vintage_at(vintages, q.effective)
        if annual_at is not None:
            for slot in ("debt", "cash", "tangible_book", "shares", "fcf", "ffo"):
                setattr(q, slot, getattr(annual_at, slot))

    merged = sorted([*vintages, *quarterly], key=lambda v: v.effective)
    series = reconstruct(raw["prices"], merged) if len(merged) >= MIN_VINTAGES else {}

    info = raw.get("info") or {}
    payload: dict[str, Any] = {
        "_ver": CACHE_VER,
        "_ts": time.time(),
        "ticker": symbol,
        "name": info.get("shortName") or symbol,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "quote_type": info.get("quoteType"),
        "series": {k: v for k, v in series.items()},
        "percentiles": latest_percentiles(series, minimum=MIN_OBSERVATIONS),
        "window": window_meta(series, merged),
        "vintages": [v.as_dict() for v in merged],
        "forward_context": {k: info.get(k) for k in _FORWARD_KEYS if info.get(k) is not None},
        "prices": raw["prices"][-260:],
    }
    if not series:
        payload["unavailable"] = (
            "too_few_vintages" if len(merged) < MIN_VINTAGES else "no_reconstructable_factors")
    return payload


def get(ticker: str, *, force: bool = False) -> dict[str, Any]:
    """Memory, then disk, then network. The only entry point a caller needs.

    Raises ``data.FetchError`` when a cold ticker cannot be built, rather than
    returning an empty payload: the page has to tell the difference between "this
    symbol has no usable statements" and "Yahoo said no just now", and an empty
    dict reads as the first when it is the second.
    """
    symbol = ticker.strip().upper()
    if not force:
        with _mem_lock:
            hit = _mem.get(symbol)
        if hit and (time.time() - hit[0]) < TTL_SECONDS:
            return hit[1]
        disk = _read_disk(symbol)
        if disk:
            with _mem_lock:
                _mem[symbol] = (disk.get("_ts") or time.time(), disk)
            return disk

    payload = build(symbol)
    with _mem_lock:
        _mem[symbol] = (payload["_ts"], payload)
    _write_disk(symbol, payload)
    return payload


def peek(ticker: str) -> Optional[dict[str, Any]]:
    """An already-built payload, or ``None``. Never fetches.

    Mirrors ``breadth.peek()`` / ``valuation.peek()``. The TTL is ignored on the
    disk read for the reason ``valuation._previous_snapshots`` ignores it: a
    day-old reconstruction over five years of weekly data is still a correct
    answer to "how does this multiple compare with its own history", and
    discarding it would leave the page blank while a fetch it must not make runs.
    """
    symbol = ticker.strip().upper()
    with _mem_lock:
        hit = _mem.get(symbol)
    if hit:
        return hit[1]
    return _read_disk(symbol, ignore_ttl=True)


def refresh(ticker: str) -> dict[str, Any]:
    """Force a rebuild. Used by the page's own refresh control."""
    return get(ticker, force=True)


# ---------------------------------------------------------------------------
# The overview universe
# ---------------------------------------------------------------------------

def universe() -> list[str]:
    """The tickers the ``/dca`` overview ranks.

    Derived from ``dca.TICKER_MODELS`` rather than kept as a second list, so the
    set the framework names and the set the page ranks cannot drift apart. That
    map is also the only place where a template was chosen deliberately rather
    than inferred, which is exactly the population worth showing side by side --
    a ranked table is only meaningful if every row was scored on a model somebody
    stands behind.

    ``GOOG`` is dropped in favour of ``GOOGL``: they are share classes of one
    company and would occupy two rows saying the same thing, and only ``GOOGL``
    is in ``PEER_GROUPS``, so ``GOOG`` could never get a peer percentile anyway.
    """
    from ystocker.dca import TICKER_MODELS

    return sorted(set(TICKER_MODELS) - {"GOOG"})


def cached_tickers() -> list[str]:
    """Symbols with a reconstruction already on disk. Never fetches.

    This is what makes the overview safe on the request path: the page ranks
    what has been built, and says how many of the universe that is, rather than
    triggering a fan-out of six Yahoo reads per missing symbol.
    """
    try:
        if not CACHE_DIR.exists():
            return []
        return sorted(p.stem.upper() for p in CACHE_DIR.glob("*.json"))
    except OSError as exc:  # pragma: no cover - defensive
        log.debug("dca_history: could not list cache dir: %s", exc)
        return []


#: Seconds between tickers in a warm sweep. Deliberately large. One ticker is
#: six Yahoo reads back to back, so this is ~5 calls/second at the burst and one
#: ticker every half minute on average -- an order of magnitude gentler than the
#: per-symbol sweep ``valuation.py`` records having got this box hard-blocked,
#: while still covering a 15-name universe inside ten minutes.
WARM_SPACING_SECONDS = 30

# ---------------------------------------------------------------------------
# One global budget for every rebuild, on-demand or swept
# ---------------------------------------------------------------------------
#
# The background sweep paces itself, and that is not enough on its own: a reader
# clicking through ticker pages triggers an *on-demand* rebuild each time, and
# per-symbol de-duplication does not bound anything when the symbols are all
# different. Twenty distinct names in eight minutes is 120 Yahoo reads with
# nothing in between -- observed on the first afternoon this shipped, from one
# person following the DCA link off /history pages. Two readers, or one faster
# one, multiplies it.
#
# So both paths draw on one budget. Refusal is cheap and invisible: the API was
# already answering 202 and the client was already polling, so a refused slot
# just means the rebuild starts on a later poll instead of immediately.

#: Most rebuilds running at once. Each is six Yahoo reads back to back, so two
#: concurrent is already a ~12-read burst.
MAX_INFLIGHT_BUILDS = 2

#: Floor on the gap between *starting* two rebuilds, on-demand or swept.
BUILD_MIN_GAP_SECONDS = 8.0

#: How long a sweep waits for a build slot before abandoning the pass. Longer
#: than the on-demand path would ever hold one, short enough that a leaked
#: counter shows up as a stalled sweep in the log rather than a wedged thread.
_WARM_SLOT_WAIT_SECONDS = 120.0

_budget_lock = threading.Lock()
_inflight = 0
_last_build_start = 0.0


def try_reserve_build() -> bool:
    """Claim a slot for one rebuild, or return ``False``.

    Callers that get ``True`` **must** call :func:`release_build` in a ``finally``
    -- a leaked slot is permanent and would silently stop every future rebuild,
    which looks exactly like Yahoo being down.
    """
    global _inflight, _last_build_start

    now = time.monotonic()
    with _budget_lock:
        if _inflight >= MAX_INFLIGHT_BUILDS:
            return False
        if now - _last_build_start < BUILD_MIN_GAP_SECONDS:
            return False
        _inflight += 1
        _last_build_start = now
        return True


def release_build() -> None:
    """Give back a slot claimed by :func:`try_reserve_build`."""
    global _inflight

    with _budget_lock:
        _inflight = max(0, _inflight - 1)


def build_budget() -> dict[str, Any]:
    """What the limiter currently allows, for the page to explain a wait."""
    with _budget_lock:
        return {"inflight": _inflight, "max_inflight": MAX_INFLIGHT_BUILDS,
                "min_gap_seconds": BUILD_MIN_GAP_SECONDS}


_warm_lock = threading.Lock()
_warming = False


def warm_universe(symbols: Optional[Sequence[str]] = None, *,
                  spacing: float = WARM_SPACING_SECONDS) -> int:
    """Rebuild any universe ticker that has no fresh payload. Returns rows built.

    Skips anything already fresh, so a restart does not re-fetch the world --
    the disk cache survives a deploy and only a genuinely expired entry costs
    anything.

    Draws on the same global budget as an on-demand rebuild, so a reader
    browsing ticker pages while this runs cannot double the request rate. A
    refused slot is waited out rather than skipped: the sweep has nowhere else
    to be, and dropping the ticker would leave the overview permanently short of
    it.

    Stops on a provider cool-down rather than pushing more requests at a vendor
    that has just said no, exactly as ``analyst._fetch`` does. A partial pass is
    kept: half a ranked table beats none of it, and the next pass fills the rest.

    Guarded by a module-level flag so two callers cannot run overlapping sweeps.
    """
    global _warming

    from ystocker import fetchguard
    from ystocker import data as ydata

    with _warm_lock:
        if _warming:
            log.info("dca_history: warm already in progress — skipping")
            return 0
        _warming = True
    try:
        targets = [s.strip().upper() for s in (symbols or universe())]
        built = 0
        for i, symbol in enumerate(targets):
            if _read_disk(symbol) is not None:
                continue
            try:
                fetchguard.guard(ydata.PROVIDER)
            except fetchguard.CooldownActive as exc:
                log.warning("dca_history: warm stopped at %d/%d — %s",
                            i, len(targets), exc)
                break
            if built and spacing:
                time.sleep(spacing)
            # Wait for a slot rather than skipping the ticker: the sweep has
            # nowhere else to be, and dropping it would leave the overview
            # permanently short of that name. Bounded, so a leaked counter
            # cannot hang the sweep for ever.
            waited = 0.0
            while waited < _WARM_SLOT_WAIT_SECONDS and not try_reserve_build():
                time.sleep(2.0)
                waited += 2.0
            if waited >= _WARM_SLOT_WAIT_SECONDS:
                log.warning("dca_history: warm gave up waiting for a build slot")
                break
            try:
                get(symbol, force=True)
                built += 1
            except Exception as exc:  # noqa: BLE001 - one bad ticker is not a bad sweep
                log.info("dca_history: warm failed for %s: %s", symbol, exc)
            finally:
                release_build()
        if built:
            log.info("dca_history: warmed %d ticker(s)", built)
        return built
    finally:
        with _warm_lock:
            _warming = False


def is_warming() -> bool:
    """True while a universe sweep is running, so the page can say so."""
    with _warm_lock:
        return _warming


# ---------------------------------------------------------------------------
# Daily snapshot sweep
# ---------------------------------------------------------------------------

def snapshot_universe() -> int:
    """Bank one forward-basis row per cached ticker. Returns rows written.

    Reads ``ticker_cache.json`` and writes to DynamoDB -- **no Yahoo call at
    all**, because the rolling refresher in ``routes.py`` has already paid for
    this data. That is what makes a daily sweep of ~230 symbols free, and it is
    why the accumulating series covers the whole peer universe rather than only
    the tickers somebody happened to open.
    """
    from ystocker.valuation import _cached_fundamentals

    if _get_table() is None:
        return 0
    stamp = date.today().isoformat()
    written = 0
    for symbol, record in _cached_fundamentals().items():
        row = snapshot_row(symbol, record, stamp=stamp)
        if row:
            save_row(row)
            written += 1
    log.info("dca_history: banked %d forward-basis rows for %s", written, stamp)
    return written


def start_background_thread() -> None:
    """Bank a snapshot shortly after startup, then once a day, and warm the
    overview universe behind it.

    The snapshot sweep costs no Yahoo call at all. The universe warm does -- six
    reads per ticker -- so it runs *after* the snapshot, spaced, and only for
    tickers whose payload has actually expired. It is deliberately limited to
    ``universe()`` (about fifteen names) rather than everything in
    ``PEER_GROUPS`` (about 230): that larger sweep is precisely what
    ``valuation.py`` records having got this box throttled, and a reader who
    wants a name outside the list still gets it on demand by opening its page.

    Under gunicorn ``--preload`` this runs only in the master, like every other
    background thread here.
    """

    def _loop() -> None:
        # Let the ticker cache warm first: a sweep at second zero banks a row of
        # mostly-empty records and that row is then the one on file for the day.
        time.sleep(600)
        while True:
            try:
                snapshot_universe()
            except Exception as exc:  # noqa: BLE001
                log.warning("dca_history: snapshot sweep failed: %s", exc)
            try:
                warm_universe()
            except Exception as exc:  # noqa: BLE001
                log.warning("dca_history: universe warm failed: %s", exc)
            time.sleep(24 * 3600)

    threading.Thread(target=_loop, name="dca-history-snapshot", daemon=True).start()
    log.info("dca_history: snapshot thread started")
