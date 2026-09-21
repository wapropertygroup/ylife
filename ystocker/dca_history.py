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
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence

from ystocker import listing

log = logging.getLogger(__name__)

__all__ = [
    "ANNUAL_LAG_DAYS", "QUARTERLY_LAG_DAYS", "MIN_VINTAGES",
    "Vintage", "build_vintages", "build_quarterly_ttm", "vintage_at", "reconstruct",
    "percentile_series", "latest_percentiles",
    "snapshot_row", "load_series", "save_row",
    "dcf_inputs", "dcf_for",
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


def reconstruct_forward(prices: Sequence[tuple[str, float]],
                        vintages: Sequence[Vintage]) -> dict[str, list[tuple[str, float]]]:
    """A *forward*-basis history: price over the earnings of the year ahead.

    Separate from :func:`reconstruct` rather than a branch inside it, because it
    breaks that function's central rule on purpose. ``reconstruct`` divides by
    what was **public** at each week; this divides by what the year ahead
    **turned out to be**, which is knowledge nobody had at the time. Putting
    both in one function would make the look-ahead an easy thing to inherit by
    accident, and the no-look-ahead guarantee is the reason the trailing series
    can be trusted at all.

    The look-ahead is the point, and it is not a backtest. The question a
    forward multiple asks is "is 9x next year's earnings cheap *for this
    company*", and the only honest denominator for the historical half of that
    comparison is what next year's earnings actually were. Ranking today's
    consensus-based forward multiple against a distribution of *trailing* ones
    instead is what biases the answer cheap for everything that grows —
    measured at a median of 14.6 percentile points across this universe, which
    is what this function exists to avoid.

    What it does not remove: consensus is systematically more optimistic than
    outturn, so today's point has a slightly larger denominator than the
    historical ones and still reads a little cheap. That residual is the gap
    between estimate and result, not the gap between two different measures of
    earnings, and it is perhaps a tenth the size. It is named in the payload
    (``basis: reconstructed_forward``) rather than hidden.

    The last ~1 year of weeks drop out, because no fiscal year after them has
    reported yet. A distribution does not need to be contiguous, but it does
    mean the most recent regime is absent from it.
    """
    annual = sorted([v for v in vintages if v.kind == "annual"
                     and v.period_end and v.effective],
                    key=lambda v: v.period_end)
    if not annual:
        return {}

    out: dict[str, list[tuple[str, float]]] = {"pe": [], "pfcf": []}
    for stamp, close in prices:
        if not _pos(close):
            continue
        # The fiscal year the market was estimating at `stamp`: the earliest one
        # that had not yet been reported. Keying on `effective` (when the filing
        # landed) rather than `period_end` is what makes this the year under
        # estimate rather than the year already in the bag -- in the months
        # between a period closing and its 10-K, the forward figure still refers
        # to the *next* year, which is exactly what `effective` captures.
        ahead = next((v for v in annual if v.effective > stamp), None)
        if ahead is None:
            continue
        if _pos(ahead.eps):
            out["pe"].append((stamp, round(close / float(ahead.eps), 4)))
        # Shares from the vintage in force at the time: a forward multiple is a
        # forward *denominator*, not a forward share count, and using the future
        # count would fold a buyback nobody had seen into the price side.
        current = vintage_at(annual, stamp)
        shares = current.shares if current is not None else None
        if _pos(shares) and _pos(ahead.fcf):
            out["pfcf"].append((stamp, round(close * float(shares)
                                             / float(ahead.fcf), 4)))

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
        "fx": _fetch_fx_series(info.get("financialCurrency"), info.get("currency")),
    }


def _fetch_fx_series(statement_currency: Any,
                     price_currency: Any) -> list[tuple[str, float]]:
    """Weekly history for the filer's currency against the listing's.

    A **seventh** Yahoo read, and it fires only when the two currencies actually
    differ — so the ~59 of 60 tracked tickers that file in the currency they
    trade in cost exactly what they did before, and an ADR costs one more read a
    day. That is the whole price of not reporting TSM's P/E as 1.01.

    Spot would have been free (``data.usd_rate`` is already cached and shared)
    and is wrong in the specific way this engine is least able to absorb: a
    single rate applied across five years converts a 2021 statement at a 2026
    rate, which shifts that week's multiple by the whole intervening drift and
    then ranks today's multiple against the result. That is the forward-versus-
    trailing basis error in another costume, and this module's docstring already
    explains why it is fatal rather than approximate.

    Returns ``[]`` rather than raising. A missing series falls back to spot at
    the call site and is reported as such; both together failing is what makes
    the reconstruction refuse.
    """
    pair = listing.fx_pair(statement_currency, price_currency)
    if not pair:
        return []
    import yfinance as yf

    out: list[tuple[str, float]] = []
    try:
        frame = yf.Ticker(pair).history(period=PRICE_PERIOD, interval=PRICE_INTERVAL)
        if frame is not None and not frame.empty and "Close" in frame:
            for stamp, close in frame["Close"].items():
                try:
                    value = float(close)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    out.append((stamp.date().isoformat(), value))
    except Exception as exc:  # noqa: BLE001 - spot is the documented fallback
        log.warning("dca_history: FX history %s unavailable: %s", pair, exc)
    return out


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
#: each comes from. Derived fields (``pfcf``, ``fcf_yield``, ``forward_pfcf``)
#: are computed in :func:`snapshot_row` from market cap and FCF, which that
#: cache already holds.
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


#: Banked fields that are genuinely a *forward* multiple, mapped to the factor
#: they may be used to rank. Everything else in :data:`_SNAPSHOT_FIELDS` is a
#: trailing figure banked for context, and must never rank a forward value —
#: that is the bias this module's docstring opens with.
#:
#: ``pe`` was forward from the first row. ``forward_pfcf`` was not banked at all
#: until it was added here, which is the whole reason it is worth adding on its
#: own: a row not written is gone, so the series cannot start retroactively and
#: every day without it is a day permanently missing from a distribution that
#: needs :data:`ystocker.dca.MIN_OBSERVATIONS` of them.
_FORWARD_BANKED: dict[str, str] = {"pe": "pe", "forward_pfcf": "pfcf"}


def _forward_pfcf(record: Mapping[str, Any]) -> Optional[float]:
    """Today's *forward* P/FCF for a ``ticker_cache.json`` record, or ``None``.

    Split out and pure so the banked value and the one ``/evaluation`` renders
    come from one implementation. :mod:`ystocker.fcf` owns the arithmetic and
    every refusal in it — a negative trailing FCF scaled by a growth factor is
    not a forecast, and a negative P/E makes the P/E-derived growth negative —
    so nothing is recomputed here, only fed.
    """
    from ystocker import fcf as _fcf

    est = _fcf.estimate(
        fcf_ttm=record.get("FCF ($B)"),
        market_cap=record.get("Market Cap ($B)"),
        pe_ttm=record.get("PE (TTM)"),
        pe_fwd=record.get("PE (Forward)"),
    )
    value = est.get("forward_pfcf")
    return float(value) if _pos(value) else None


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

    # The forward counterpart, and the only banked field besides `pe` that can
    # rank a forward value. `pfcf` above divides by *trailing* FCF, so ranking
    # today's forward multiple against a history of those is the same basis
    # error in stored form.
    fwd_pfcf = _forward_pfcf(record)
    if fwd_pfcf is not None:
        row["forward_pfcf"] = round(fwd_pfcf, 4)

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
                for key in (*_SNAPSHOT_FIELDS, "pfcf", "fcf_yield", "forward_pfcf"):
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
# The forward basis
# ---------------------------------------------------------------------------
#
# Everything above ranks a *trailing* multiple against a trailing history, and
# the module docstring says why that is not negotiable. This section is the only
# honest way to score on forward figures: rank a forward multiple against a
# distribution of forward multiples, both on the same basis, and fall back to the
# reconstruction when there is no such distribution yet.
#
# Measured on 31 tickers of the live universe on 2026-09-19, ranking today's
# forward P/E against the *trailing* reconstruction instead moved the percentile
# by a median of −14.6 points and a mean of −17.8, cheaper in 25 of 31 — and the
# spread ran from −99.1 (8001.T) to +45.7 (TSM), so it is not even a constant
# anyone could calibrate out. P/E carries 35% of the compounder template and PEG
# derives from it for another 15%, which puts roughly half the relative score on
# a number biased one way. That is the measurement this section exists to avoid
# having to make again.

def forward_multiples(payload: Mapping[str, Any]) -> dict[str, float]:
    """Today's forward-basis multiple per factor, from a built payload.

    Pure, and reads only what :func:`build` already stored — no fetch, for the
    same reason ``dcf_inputs`` does not: this runs on the request path.

    ``pe`` is Yahoo's own ``forwardPE``. ``pfcf`` is derived by
    :mod:`ystocker.fcf` from the latest annual FCF and the P/E-implied growth,
    which is the same estimate ``/evaluation`` renders — and it inherits every
    refusal in that module rather than reproducing any of them.

    Deliberately not here: ``ev_ebitda`` (Yahoo publishes only a trailing
    ``enterpriseToEbitda``), ``peer`` (cross-sectional, no basis to speak of) and
    ``peg``. PEG is not omitted for want of a number — ``pegRatio`` exists — but
    because its growth denominator is on neither basis consistently, so a forward
    PEG ranked against banked PEGs would mix two conventions inside one ratio.
    :func:`ystocker.dca.derive_overrides` already carries a hand-set P/E through
    to PEG, which is the path that keeps the two describing one company.
    """
    ctx = payload.get("forward_context") or {}
    out: dict[str, float] = {}

    fwd_pe = ctx.get("forwardPE")
    if _pos(fwd_pe):
        out["pe"] = float(fwd_pe)

    # Latest *annual* FCF, matching `dcf_inputs`: `build()` copies the annual
    # figure onto every quarterly TTM vintage, so taking the last vintage of any
    # kind would repeat one year's cash flow and say nothing new.
    fcf_ttm = None
    for vintage in reversed(payload.get("vintages") or []):
        if vintage.get("kind") == "annual" and _fin(vintage.get("fcf")):
            fcf_ttm = float(vintage["fcf"])
            break

    if fcf_ttm is not None and _pos(ctx.get("marketCap")):
        from ystocker import fcf as _fcf

        est = _fcf.estimate(
            # `fcf.estimate` is unit-agnostic — it only ever divides one by the
            # other — so passing both in raw currency is fine, where
            # `snapshot_row` passes both in $B.
            fcf_ttm=fcf_ttm,
            market_cap=float(ctx["marketCap"]),
            pe_ttm=ctx.get("trailingPE"),
            pe_fwd=ctx.get("forwardPE"),
        )
        if _pos(est.get("forward_pfcf")):
            out["pfcf"] = float(est["forward_pfcf"])

    return out


def forward_distributions(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[float]]:
    """Banked forward-basis distributions, keyed by the factor they rank.

    Pure. Only :data:`_FORWARD_BANKED` is admitted: the banked ``pfcf`` divides
    by trailing FCF and the banked ``pe_ttm`` is trailing by name, so letting
    either through here would store the very mistake the split exists to prevent.
    """
    out: dict[str, list[float]] = {}
    for row in rows:
        for banked_key, factor in _FORWARD_BANKED.items():
            value = row.get(banked_key)
            if _pos(value):
                out.setdefault(factor, []).append(float(value))
    return out


#: One banked read per ticker per hour rather than per request. `_dca_score` is
#: the single scoring path and the overview scores sixty rows, so an uncached
#: Query here would be sixty of them on every page load — on PAY_PER_REQUEST
#: that is billed by volume, the same reason `fedwatch.history_cached` exists.
#: An hour is generous against a series that gains one row a day.
_BANKED_TTL_SECONDS = 3600.0
_banked_memo: dict[str, tuple[float, dict[str, list[float]]]] = {}
_BANKED_MEMO_LOCK = threading.Lock()


def banked_distributions(ticker: str) -> dict[str, list[float]]:
    """:func:`forward_distributions` for *ticker*, memoised.

    Degrades to ``{}`` exactly as :func:`load_series` does — with no table the
    forward basis is simply unavailable and every factor falls back to the
    reconstruction, which is a complete answer rather than a broken one.
    """
    symbol = ticker.strip().upper()
    now = time.time()
    with _BANKED_MEMO_LOCK:
        hit = _banked_memo.get(symbol)
        if hit and now - hit[0] < _BANKED_TTL_SECONDS:
            return hit[1]
    dists = forward_distributions(load_series(symbol))
    with _BANKED_MEMO_LOCK:
        _banked_memo[symbol] = (now, dists)
    return dists


def forward_series_for(payload: MutableMapping[str, Any]) -> dict[str, list[tuple[str, float]]]:
    """The forward-basis history for *payload*, deriving it if it is not stored.

    Every payload built before :func:`reconstruct_forward` existed has no
    ``forward_series``, and without a :data:`CACHE_VER` bump — which would be
    ~360 Yahoo reads in one sweep — those would sit on the trailing basis until
    each ticker's 24h TTL happened to expire. That is days of the registry
    scoring on one basis while whichever names were rebuilt score on another.

    It does not need a rebuild. ``reconstruct_forward`` takes prices and
    vintages, and **both are already in the payload** — the same reason
    ``dcf_inputs`` costs no extra call. So derive it here, once, and write it
    back into the cached dict so the next request reuses it. No network, a few
    hundred divisions, safe on the request path.

    The payload is mutated rather than copied deliberately: ``get()`` hands back
    the object held in ``_mem``, so storing the result there is what makes this
    once-per-process rather than once-per-request.
    """
    existing = payload.get("forward_series")
    if existing is not None:
        return existing

    prices = payload.get("prices") or []
    raw_vintages = payload.get("vintages") or []
    if not prices or not raw_vintages:
        payload["forward_series"] = {}
        return {}

    slots = Vintage.__slots__
    vintages = [Vintage(**{k: v.get(k) for k in slots})
                for v in raw_vintages
                if v.get("period_end") and v.get("effective")]
    derived = reconstruct_forward([(s, c) for s, c in prices], vintages)
    payload["forward_series"] = derived
    if derived:
        log.debug("dca_history: derived forward series for %s (%s)",
                  payload.get("ticker"),
                  ", ".join(f"{k}={len(v)}" for k, v in derived.items()))
    return derived


def forward_basis(payload: Mapping[str, Any],
                  distributions: Mapping[str, Sequence[float]],
                  *, minimum: int) -> dict[str, dict[str, Any]]:
    """Which factors can be scored forward-against-forward today.

    Two sources of forward history, tried in that order, and a factor takes the
    first that can rank it:

    ``banked``
        :data:`TABLE_NAME`, one genuinely point-in-time row per day. What the
        market actually thought, on the day. Worth the wait and starts empty.
    ``reconstructed_forward``
        :func:`reconstruct_forward` -- price over the earnings the year ahead
        turned out to deliver. Available on first page load, at the cost of a
        denominator nobody could have known at the time.

    A factor with neither stays on the trailing reconstruction and is simply
    absent from the result. That is the fallback the whole design rests on.

    ``DCA_FORWARD_RECONSTRUCTED=0`` drops the second tier, leaving the
    conservative behaviour: forward multiples shown, never ranked until the
    banked series matures.

    The ``minimum`` is :data:`ystocker.dca.MIN_OBSERVATIONS`, the same floor the
    trailing series answers to: a rank over eleven points can only return eleven
    answers and will happily say 100.0.
    """
    from ystocker.dca import percentile_rank

    values = forward_multiples(payload)
    trailing = payload.get("percentiles") or {}
    reconstructed = (forward_series_for(payload)   # type: ignore[arg-type]
                     if _forward_reconstruction_enabled() else {})
    out: dict[str, dict[str, Any]] = {}
    for factor, value in values.items():
        for source, dist in (
                ("banked", [float(v) for v in (distributions.get(factor) or [])]),
                ("reconstructed_forward",
                 [v for _stamp, v in (reconstructed.get(factor) or [])]),
        ):
            pct = percentile_rank(value, dist, minimum=minimum)
            if pct is None:
                continue
            out[factor] = {
                "percentile": pct,
                "value": value,
                "observations": len(dist),
                "source": source,
                # What the trailing reconstruction said, kept so the page can
                # show the size of the switch rather than only its result.
                "trailing_percentile": trailing.get(factor),
            }
            break
    return out


def _forward_reconstruction_enabled() -> bool:
    """Kill switch for the look-ahead tier.

    Off, the engine scores exactly as it did before :func:`reconstruct_forward`
    existed: forward multiples rendered, ranked only once the banked series is
    long enough. Separate from any other flag because this is the one tier whose
    denominator was not knowable at the time, and a reader who does not want
    that in their score should be able to say so without losing the rest.
    """
    return os.environ.get("DCA_FORWARD_RECONSTRUCTED", "1").strip() not in ("0", "false", "False")


def forward_context_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Display-only companion to :func:`forward_basis`.

    Every forward multiple that exists today, whether or not anything can be
    ranked against it — so a reader three months from the first rankable
    distribution still sees "forward 9.0x vs trailing 14.2x" beside the score,
    and can tell that the engine knows the number and is declining to rank it
    rather than not having it.
    """
    values = forward_multiples(payload)
    series = payload.get("series") or {}
    out: dict[str, Any] = {}
    for factor, value in values.items():
        points = series.get(factor) or []
        out[factor] = {
            "forward": value,
            "trailing": points[-1][1] if points else None,
        }
    return out


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


def history_coverage(series: Mapping[str, Sequence[tuple[str, float]]],
                     weights: Mapping[str, float],
                     *, minimum: int) -> dict[str, Any]:
    """Why the V-history line is short or absent, factor by factor.

    :func:`v_history` returns an empty list whenever too little of the template
    survives, and the page could only say "not enough history" — which collapses
    two very different situations. NEM on 2026-09-19 is the case that motivated
    this: its P/E series had **57** weekly observations against a floor of 60,
    because Newmont reported losses until early 2025 and the reconstruction emits
    no P/E for a loss quarter. Three weeks from working, and indistinguishable on
    the page from a permanent refusal.

    Reports the observation count against the floor for every factor the template
    names, and the weight that survives. ``peer`` is listed as having no history
    at all rather than as short — it is cross-sectional, so it is *never* going to
    accumulate any, and that is the reason the headline V and this line can
    legitimately differ.
    """
    from ystocker.dca import MIN_SURVIVING_WEIGHT

    factors: list[dict[str, Any]] = []
    surviving = 0.0
    for factor, weight in sorted(weights.items(), key=lambda kv: -kv[1]):
        rows = series.get(factor)
        count = len(rows) if rows else 0
        # A factor with no series at all and no prospect of one is a different
        # statement from one that is merely short.
        historyless = factor not in RECONSTRUCTED
        usable = (not historyless) and count >= minimum
        if usable:
            surviving += float(weight)
        factors.append({
            "factor": factor,
            "weight": round(float(weight), 4),
            "observations": count,
            "needed": minimum,
            "usable": usable,
            "reason": None if usable else ("no_history" if historyless else "too_few"),
            # How many more weekly points would admit it. Only meaningful for a
            # factor that can accumulate them.
            "short_by": None if usable or historyless else minimum - count,
        })

    return {
        "factors": factors,
        "surviving_weight": round(surviving, 4),
        "minimum_weight": MIN_SURVIVING_WEIGHT,
        "sufficient": surviving >= MIN_SURVIVING_WEIGHT,
    }


#: Funds that sit in an equity-named peer group, where nothing about the group
#: gives them away.
#:
#: :func:`fund_symbols` gets everything else structurally — a ticker in any
#: group whose *name* says ETF — and that covers 48 of the 49, including ``XTL``,
#: which is filed under "Telecom" first but also appears in "Sector ETFs". Only
#: ``IGV`` is in an equity group and nowhere else.
#:
#: A name missing from here fails exactly the way the whole set did before it
#: existed: the fund is offered, the reader waits a minute, and the build comes
#: back ``unavailable``. That is the pre-existing behaviour, not a new one — so
#: the cost of this list falling behind is bounded, which is why a hand-kept
#: list is acceptable at all.
FUNDS_IN_EQUITY_GROUPS: frozenset[str] = frozenset({"IGV"})


def fund_symbols() -> frozenset[str]:
    """Every ``PEER_GROUPS`` ticker this engine can never score.

    Yahoo publishes no income statement, balance sheet or cash-flow statement
    for a fund, so a DCA reconstruction of one cannot exist — ``build()`` spends
    six reads and returns ``unavailable``. Offering such a name in the ``/dca``
    search is offering a minute of waiting for a guaranteed dead end, and the
    page's own search box was doing exactly that for ``SPY`` and ``XTL``.

    Structural and cheap on purpose: no disk read, no ``peek()``, no network.
    The tempting alternative is to ask the DCA cache, which knows for certain
    because it has the ``unavailable`` payload — but that is a per-symbol JSON
    parse behind an autocomplete that fires on every keystroke, and it knows
    nothing at all about a name nobody has opened yet, which is the case that
    matters.

    Membership of *any* ETF-named group, not just the first one
    :func:`peer_group` would return. ``XTL`` is in "Telecom" and "Sector ETFs",
    and reading only the first would let it through wearing an equity label —
    which is precisely how it reached the suggestion list.

    Recomputed rather than cached at import: ``routes._load_groups()`` replaces
    ``PEER_GROUPS`` wholesale at startup and the ``/groups`` UI edits it at
    runtime, so a set frozen at import would describe a configuration the site
    is no longer running.
    """
    from ystocker import PEER_GROUPS

    return frozenset(FUNDS_IN_EQUITY_GROUPS).union(
        t for name, members in PEER_GROUPS.items()
        if "ETF" in name.upper() for t in members)


def peer_group(ticker: str) -> Optional[str]:
    """The ``PEER_GROUPS`` entry *ticker* is scored against, or ``None``.

    First match in insertion order, not the smallest match -- deliberately
    different from ``relative_strength._peer_candidates``, and extracted here so
    that :func:`peer_percentiles` and the peer comparison table behind
    ``/api/dca/<t>/peers`` cannot name two different groups for one company. A
    page that ranks NVDA against "Semiconductors" in one card and lists the
    "Tech" members in the next is not showing two views; it is contradicting
    itself, and neither number tells the reader which group produced it.
    """
    from ystocker import PEER_GROUPS

    symbol = (ticker or "").strip().upper()
    return next((name for name, members in PEER_GROUPS.items()
                 if symbol in members), None)


def peer_multiples(group: Optional[str],
                   recs: Optional[Mapping[str, Mapping[str, Any]]] = None,
                   *, basis: Optional[str] = None) -> dict[str, Any]:
    """Every member of *group* on **one** P/E basis, for a side-by-side table.

    Free, for the reason :func:`peer_percentiles` is free: ``ticker_cache.json``
    is already maintained by the rolling refresher and no Yahoo call happens
    here. *recs* is passed in for the same reason too.

    The basis is chosen once for the whole group and never per member. Filling a
    missing forward P/E with a member's trailing one would put a column of
    mixed-basis numbers under a single heading -- and since a forward multiple
    is the lower of the two for anything growing, the members that fell back
    would read as systematically *dearer* than their peers on nothing but a data
    gap. That is the cross-sectional form of the basis error this whole module
    exists to prevent.

    *basis* pins the choice to whatever :func:`peer_percentiles` actually ranked
    on, so the table and the ``peer`` factor above it describe one measurement.
    Unpinned, it prefers forward and falls back to trailing for the group.
    """
    from ystocker import PEER_GROUPS
    from ystocker.valuation import _cached_fundamentals

    members = list(PEER_GROUPS.get(group or "", []))
    if not members:
        return {"basis": None, "field": None, "values": {}, "members": []}

    if recs is None:
        recs = _cached_fundamentals()

    choices = [("PE (Forward)", "forward"), ("PE (TTM)", "trailing")]
    if basis:
        choices = [c for c in choices if c[1] == basis] or choices

    for field, name in choices:
        values = {t: round(float((recs.get(t) or {}).get(field)), 2)
                  for t in members if _pos((recs.get(t) or {}).get(field))}
        if values:
            return {"basis": name, "field": field, "values": values,
                    "members": members}
    return {"basis": None, "field": None, "values": {}, "members": members}


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
    group = peer_group(symbol)
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
# The absolute branch's inputs
# ---------------------------------------------------------------------------

def dcf_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Everything :func:`ystocker.dcf.from_fundamentals` needs, from one payload.

    Pure, and reads only what :func:`build` already stored, so the DCF branch
    costs **no extra Yahoo call at all** — the same property that makes the daily
    snapshot sweep free.

    **Only annual vintages contribute the cash-flow series, and that is not a
    detail.** :func:`build` fills each quarterly TTM vintage's ``fcf`` from the
    annual vintage in force at the same date, which is right for computing a
    weekly P/FCF and catastrophic here: the same free-cash-flow figure would
    appear three or four times in a row, making the observed growth between those
    points exactly zero and the dispersion far too narrow. A ten-year CAGR would
    be computed over a series that is mostly repeats. Filtering on ``kind`` is
    the whole fix, and the failure it prevents is silent — the series would still
    have the right length and the right magnitudes.

    The balance-sheet items are **stocks**, so the latest reading wins rather
    than anything being summed or averaged; the cash-flow item is a **flow**, so
    the whole series is kept. That is the same distinction
    :func:`build_quarterly_ttm` turns on.

    The price is the **last weekly close of the reconstruction**, not a live
    quote. That keeps ``V_DCF`` and ``V_REL`` describing one price: the relative
    branch ranks multiples computed at that close, so measuring the DCF upside
    against a different one would make the two halves of a single score refer to
    two different moments.
    """
    vintages = [v for v in (payload.get("vintages") or [])
                if isinstance(v, Mapping) and v.get("kind") == "annual"]
    vintages.sort(key=lambda v: str(v.get("period_end") or ""))

    fcf_series = [float(v["fcf"]) for v in vintages if _fin(v.get("fcf"))]

    latest: dict[str, Optional[float]] = {"debt": None, "cash": None, "shares": None}
    for vintage in vintages:
        for slot in latest:
            if _fin(vintage.get(slot)):
                latest[slot] = float(vintage[slot])

    prices = payload.get("prices") or []
    price = price_date = None
    if prices:
        last = prices[-1]
        try:
            price_date, price = str(last[0]), float(last[1])
        except (IndexError, TypeError, ValueError):
            price = price_date = None

    context = payload.get("forward_context") or {}
    return {
        "fcf_series": fcf_series,
        "shares": latest["shares"],
        "cash": latest["cash"] or 0.0,
        "debt": latest["debt"] or 0.0,
        "beta": context.get("beta") if _fin(context.get("beta")) else None,
        "price": price,
        "price_date": price_date,
        "annual_vintages": len(vintages),
    }


def dcf_for(ticker: str, payload: Mapping[str, Any], *,
            model: str,
            override: Optional[Mapping[str, Any]] = None,
            as_of: Optional[str] = None) -> dict[str, Any]:
    """The ``V_DCF`` payload for one ticker: override if there is one, else derived.

    *override* is injected rather than looked up, for the reason ``_dca_score``
    takes ``recs`` and ``exposure`` as arguments: ``dcf_store.all_rows()`` is a
    DynamoDB Scan, and calling it once per row would make a twenty-row table
    twenty scans for one answer.

    An override supplying **fair values** replaces the model's output and goes
    straight to :func:`ystocker.dcf.score`; one supplying only **assumptions**
    replaces its inputs and lets it run. Both end at the same mapping function,
    so the two cannot disagree about how an upside becomes a score.

    A refusal comes back as a payload with ``V`` of ``None``, never as an
    exception and never as 50 — :func:`ystocker.dca.blend_v` then moves the whole
    weight onto the relative branch.
    """
    from ystocker import dca, dcf

    form = dca.DCF_FORMS.get(model, dcf.FORM_FCFF)
    mid_cycle = model in dca.DCF_MID_CYCLE
    inputs = dcf_inputs(payload)
    override = dict(override or {})

    # A stored fair value is a claim about the company, so it is honoured even
    # for a form the derived model refuses: §10 does not say a bank cannot be
    # valued, it says *we* cannot value one with FCFF. Somebody who has built an
    # excess-return model is exactly the case the override exists for.
    if override.get("mode") == "dcf_store_values" or override.get("base") is not None:
        out = dcf.score(price=inputs["price"],
                        bear=override.get("bear"),
                        base=override.get("base"),
                        bull=override.get("bull"),
                        confidence=override.get("confidence"),
                        valuation_date=override.get("valuation_date"),
                        as_of=as_of,
                        price_date=inputs["price_date"],
                        source="override")
        out["override"] = _override_meta(override)
        return out

    if form != dcf.FORM_FCFF:
        out = dcf._refused("form_not_applicable", form=form, source="derived")
        if override:
            out["override"] = _override_meta(override)
        return out

    out = dcf.from_fundamentals(
        price=inputs["price"],
        fcf_series=inputs["fcf_series"],
        shares=inputs["shares"],
        cash=inputs["cash"],
        debt=inputs["debt"],
        beta=inputs["beta"],
        # An overridden WACC replaces the derived one rather than being blended
        # with it. There is no sensible average of two discount rates, and a
        # blend would mean the reported WACC was not the one used.
        wacc_override=override.get("wacc"),
        form=form,
        mid_cycle=mid_cycle,
        terminal_growth=override.get("terminal_growth", dcf.TERMINAL_GROWTH),
        price_date=inputs["price_date"],
        as_of=as_of,
    )
    if override:
        out["override"] = _override_meta(override)
    return out


def _override_meta(override: Mapping[str, Any]) -> dict[str, Any]:
    """What the page shows about a stored override. Never the author's address.

    ``/dca`` is public, so the e-mail on the row is masked the same way
    ``share.public_payload`` masks a sharer's: enough to say a person stands
    behind the number, not enough to publish who.
    """
    author = str(override.get("author") or "")
    return {
        "mode": override.get("mode"),
        "valuation_date": override.get("valuation_date"),
        "note": override.get("note"),
        "w_dcf": override.get("w_dcf"),
        "wacc": override.get("wacc"),
        "terminal_growth": override.get("terminal_growth"),
        "by": (author.split("@")[0] + "@…") if "@" in author else None,
        "updated_at": override.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Building one ticker's payload
# ---------------------------------------------------------------------------

#: ``info`` keys kept as forward-basis context. Shown beside the score and never
#: percentiled -- see this module's docstring on why ranking a forward multiple
#: against a trailing distribution is biased cheap for every growing company.
#:
#: ``beta`` is the exception and is not context at all: it is a DCF input, used
#: by ``dcf.wacc``. It is added here rather than fetched separately because
#: ``info`` is already one of the six reads.
#:
#: Note this list grew **without** a :data:`CACHE_VER` bump, deliberately. A bump
#: invalidates every reconstruction on disk at once, which at the registry cap is
#: ~360 Yahoo reads in one sweep -- the shape of request burst this module's whole
#: budget exists to prevent. A payload written before ``beta`` was collected
#: simply has no beta, ``dcf.wacc`` assumes 1.0 and says so in its ``notes``, and
#: the next daily rebuild fixes it. Degrading visibly for a day beats a refetch
#: storm on deploy.
_FORWARD_KEYS = ("forwardPE", "trailingPE", "forwardEps", "trailingEps",
                 "pegRatio", "enterpriseToEbitda", "priceToBook",
                 "marketCap", "quoteType", "sector", "industry", "shortName",
                 "beta",
                 # The three the listing basis is derived from. `currency` and
                 # `financialCurrency` say whether a conversion is needed at all;
                 # `sharesOutstanding` is the quoted-basis count the filer's
                 # ordinary count is divided against to recover the ADR ratio.
                 "currency", "financialCurrency", "sharesOutstanding")


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

    info = raw.get("info") or {}
    # Restate the statements into the currency and share basis the price is
    # quoted in, *before* anything divides one by the other. Doing it here rather
    # than inside reconstruct() means the stored vintages are converted too, so
    # dcf_inputs' per-share fair value comes out in the currency the reader sees
    # on the ticker rather than the filer's.
    basis = _apply_listing_basis(merged, raw.get("fx") or [], info)

    buildable = len(merged) >= MIN_VINTAGES and basis.usable
    series = reconstruct(raw["prices"], merged) if buildable else {}
    # The forward-basis companion. Costs no extra fetch -- same prices, same
    # vintages, different denominator -- and is what lets a forward multiple be
    # ranked against forward history years before the banked series is long
    # enough to do it properly.
    #
    # Added **without** a CACHE_VER bump, matching the `beta` precedent: a bump
    # invalidates every reconstruction at once, which at the registry cap is
    # ~360 Yahoo reads in one sweep -- the exact burst this module's budget
    # exists to prevent. An older payload simply has no `forward_series`, so
    # `forward_basis` finds nothing to rank against and the factor stays on the
    # trailing reconstruction, which is a complete answer rather than a broken
    # one. The daily rebuild fills it in, and `/dca/<ticker>`'s Rebuild button
    # forces it for one name now.
    forward_series = (reconstruct_forward(raw["prices"], merged)
                      if buildable else {})

    payload: dict[str, Any] = {
        "_ver": CACHE_VER,
        "_ts": time.time(),
        "ticker": symbol,
        "name": info.get("shortName") or symbol,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "quote_type": info.get("quoteType"),
        "series": {k: v for k, v in series.items()},
        "forward_series": {k: v for k, v in forward_series.items()},
        "percentiles": latest_percentiles(series, minimum=MIN_OBSERVATIONS),
        "window": window_meta(series, merged),
        "vintages": [v.as_dict() for v in merged],
        "forward_context": {k: info.get(k) for k in _FORWARD_KEYS if info.get(k) is not None},
        "prices": raw["prices"][-260:],
    }
    # Only carried when something actually had to be reconciled, so the payload
    # of an ordinary US listing is byte-for-byte what it was before.
    if not basis.aligned:
        payload["listing_basis"] = basis.as_dict()
    if not series:
        payload["unavailable"] = (
            "currency_unreconciled" if not basis.usable else
            "too_few_vintages" if len(merged) < MIN_VINTAGES else
            "no_reconstructable_factors")
    return payload


def _apply_listing_basis(vintages: Sequence[Any],
                         fx_series: Sequence[tuple[str, float]],
                         info: Mapping[str, Any]) -> listing.Basis:
    """Restate *vintages* in place into the quoted currency and share basis.

    Returns what was done, which the payload carries so the page can say it.

    **Each vintage is converted at the rate in force when it became public**, not
    at today's. A single rate across the window is the tempting implementation
    and is wrong in the way this engine notices least: it cancels out of a
    percentile rank, so every check would pass, while the *levels* the page
    prints beside the rank drift by the whole five-year FX move.

    Refusal is the third outcome and it is deliberate. With the currencies
    differing and no rate obtainable from either the series or spot, the choice
    is between publishing multiples wrong by an exchange rate and publishing
    none, and this engine's output is a cheapness score — the failure lands on
    the "extraordinarily cheap" side every time.
    """
    statement_currency = (info.get("financialCurrency") or "").strip().upper()
    price_currency = (info.get("currency") or "").strip().upper()
    needs_fx = bool(statement_currency and price_currency
                    and statement_currency != price_currency)

    # The most recent counts and earnings on file — paired against today's
    # `sharesOutstanding` and `trailingEps`, which are also the latest.
    statement_shares = next((v.shares for v in reversed(vintages) if _pos(v.shares)), None)
    statement_eps = next((v.eps for v in reversed(vintages) if _pos(v.eps)), None)

    spot: Optional[float] = None
    source: Optional[str] = None
    if needs_fx:
        if fx_series:
            spot, source = float(fx_series[-1][1]), "series"
        elif price_currency == "USD":
            # `data.usd_rate` only ever converts *to* USD, so it can stand in
            # here and nowhere else. A non-USD listing of a foreign filer gets no
            # fallback and is refused rather than approximated.
            from ystocker import data as ydata
            rate = ydata.usd_rate(statement_currency)
            if _pos(rate):
                spot, source = float(rate), "spot"

    basis = listing.detect(info,
                           statement_shares=statement_shares,
                           statement_eps=statement_eps,
                           rate=spot,
                           fx_source=source)
    if basis.aligned or not basis.usable:
        return basis

    for vintage in vintages:
        rate = listing.rate_at(fx_series, vintage.effective,
                               fallback=spot) if needs_fx else None
        for slot, value in listing.convert(vintage.as_dict(), rate=rate,
                                           ratio=basis.share_ratio,
                                           scale=basis.eps_scale).items():
            setattr(vintage, slot, value)
    return basis


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

    # Register only what actually scored. An ETF publishes no statements, so it
    # builds to an `unavailable` payload -- putting those in a list headed "all
    # scored names" would fill it with rows that can never carry a score while
    # still costing six Yahoo reads a day each to re-confirm it. Ten of the
    # twenty names opened on this feature's first afternoon were exactly that.
    if not payload.get("unavailable") and payload.get("series"):
        try:
            from ystocker import dca_universe

            dca_universe.remember(symbol)
        except Exception as exc:  # noqa: BLE001 - the registry is a convenience
            log.info("dca_history: could not register %s: %s", symbol, exc)
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

    Delegates to :mod:`ystocker.dca_universe`, which is the persistent registry:
    the framework's named companies as an unevictable seed, plus every ticker
    somebody has opened that actually scored, capped and refreshed daily. Kept
    as a thin accessor here so the warm and the routes have one name for it.
    """
    from ystocker import dca_universe

    return dca_universe.all_tickers()


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
