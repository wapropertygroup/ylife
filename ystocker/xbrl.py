"""
ystocker.xbrl
~~~~~~~~~~~~~
A company's own SEC filings, turned into the quarterly, trailing-twelve-month
and annual series the Fundamentals tab on ``/history/<ticker>`` charts.

Why this exists
---------------
The Charts tab's "Annual Financials" card comes from Yahoo, which keeps four
fiscal years of statements and about five quarters. That says whether last
year beat the year before. It cannot show a cycle: a chipmaker's margins
across one upturn, or a retailer's fourth quarter set against its other three.
Every one of those numbers originates in the filer's own XBRL, which EDGAR
serves free and keyless as one JSON document per company ("companyfacts"),
reaching back to 2009-2011 for anyone large enough to have been in the first
XBRL phase-in.

This module is pure: that document goes in, plain dicts come out. Nothing here
touches the network, the disk or the clock, which is what lets
``tests/test_xbrl.py`` pin the arithmetic against NVIDIA's actual filings.
:mod:`ystocker.fundamentals` is the I/O around it.

What the filings do not say directly, and how each gap is closed
---------------------------------------------------------------
* **Nobody files a fourth quarter.** A 10-K reports the year. Q4 is the year
  minus the first nine months, which the third-quarter 10-Q states as a
  year-to-date figure.
* **A 10-Q's cash-flow statement is year-to-date only.** There is no "three
  months ended July" for operating cash flow, so Q2 is H1 - Q1 and Q3 is
  9M - H1. Both gaps fall out of one rule: two cumulative figures that share a
  start date and end a quarter apart differ by exactly that quarter
  (:func:`quarterly_flows`).
* **Each period is filed several times** -- in its own filing, then again as a
  comparative a year or two later. The newest copy wins, as it does in SEC's
  own frames API: it carries restatements and, crucially, the re-basing after a
  stock split.
* **A split re-bases per-share figures only from the next filing on.**
  NVIDIA's first quarter of fiscal 2025 is $5.98 of diluted EPS as filed in May
  2024 and $0.60 as restated a year later, after the 10-for-1. So every
  per-share and share-count fact is re-based by the splits that came *after it
  was filed* (:func:`split_factor`). Re-basing by period date instead would
  divide every post-split restatement a second time.
* **Companies change tags.** ASC 606 moved revenue from ``SalesRevenueNet`` to
  ``RevenueFromContractWithCustomerExcludingAssessedTax`` in 2018, and some
  filers carry a *component* (product sales, service sales) under a concept
  that elsewhere means the total. Candidates are spliced only where they agree
  with what is already there on the periods they share (:func:`splice`), so a
  component can never be stitched onto a total and read as a collapse.
* **Weighted-average share counts do not add.** Q4's is approximated as
  ``4 x FY - (Q1 + Q2 + Q3)``, which is exact when the quarters are equally
  long.

Derived is not measured
-----------------------
Every figure that was not filed as such carries a code saying how it was
derived (:data:`HOW`), and the page puts that in the tooltip. A Q4 computed as
a difference is standard practice, and it is still a difference: a restated
year minus an unrestated nine months renders exactly like a filed number.

What is deliberately not here
-----------------------------
* **Segments.** Companyfacts carries only non-dimensional facts, so revenue by
  segment is not in the document at all.
* **Cash plus marketable securities.** The securities line moves between
  concepts with no overlapping period to splice on -- NVIDIA went from
  ``MarketableSecuritiesCurrent`` to ``DebtSecuritiesCurrent`` in fiscal 2027
  -- so a summed series would drop by tens of billions on a relabel. Cash and
  equivalents is tagged one way everywhere, and the page says that is what it
  shows.
* **Valuation for a foreign filer.** An ADR is priced in one currency and files
  in another, at a depositary ratio XBRL does not state -- the TSM P/E of 1.01
  that ``listing.py`` exists for. P/E and P/S are computed only for a domestic
  (10-K/10-Q) filer reporting in US dollars.
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

#: Forms whose facts are the financial statements. 8-K (recast exhibits) and
#: DEF 14A are absent on purpose: the proxy's pay-versus-performance table tags
#: ``NetIncomeLoss`` too, and would otherwise win as the "newest filing" of a
#: year it only quotes.
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A",
                          "20-F", "20-F/A", "40-F", "40-F/A"})
DOMESTIC_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A", "10-KT", "10-KT/A"})
FORMS = QUARTERLY_FORMS | ANNUAL_FORMS

#: Day counts, end minus start. A fiscal quarter runs twelve to seventeen
#: weeks: Costco's fourth is sixteen (seventeen in a 53-week year) and Kroger's
#: first is sixteen, so "about ninety days" would drop real quarters.
QUARTER_DAYS = (75, 122)
YEAR_DAYS = (350, 380)

#: Two candidate concepts are spliced only if they agree to within this (median
#: relative difference) on the oldest periods both report.
SPLICE_TOLERANCE = 0.02

#: How many of the shared periods the splice test reads, oldest first. The
#: candidate is filling the *older* gap, so the periods next to that gap are the
#: evidence; a later restatement of the newest overlap is not -- Coca-Cola
#: restated its 2018 quarters, and a median over every shared period then
#: refused to splice its whole pre-2017 revenue history.
SPLICE_EVIDENCE = 3

#: Two period ends this close together are one period filed with slightly
#: different dates, not two periods.
SAME_PERIOD_DAYS = 10

#: How a figure that was not filed as such was obtained. The page words these.
HOW = {
    "ytd": "difference of two year-to-date figures",
    "q4": "full year minus the first nine months",
    "fyq": "full year minus the first three quarters",
    "avg4": "four times the full-year average less the first three quarters",
    "fyavg": "the full-year average",
    "calc": "computed from other lines",
}

#: How many quarters / years the payload keeps. The page slices further.
MAX_QUARTERS = 80
MAX_YEARS = 25


# ---------------------------------------------------------------------------
# Facts and points
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """One reported value, as companyfacts lists it."""

    start: Optional[date]
    end: date
    val: float
    filed: date
    form: str
    fy: Optional[int] = None
    fp: Optional[str] = None
    accn: str = ""

    @property
    def days(self) -> Optional[int]:
        return None if self.start is None else (self.end - self.start).days


@dataclass(frozen=True)
class Point:
    """One value of a series, with how it was obtained."""

    end: date
    val: float
    start: Optional[date] = None
    how: Optional[str] = None
    filed: Optional[date] = None


def _date(raw: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _int(raw: Any) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_facts(node: Optional[Mapping[str, Any]], unit: str, *,
                forms: frozenset[str] = FORMS) -> list[Fact]:
    """The facts of one concept in one unit, from the statement forms only."""
    if not node:
        return []
    rows = (node.get("units") or {}).get(unit) or []
    out: list[Fact] = []
    for row in rows:
        form = row.get("form")
        if form not in forms:
            continue
        end, filed = _date(row.get("end")), _date(row.get("filed"))
        if end is None or filed is None:
            continue
        try:
            val = float(row.get("val"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(val):
            continue
        start = _date(row["start"]) if "start" in row else None
        out.append(Fact(start, end, val, filed, form, _int(row.get("fy")),
                        row.get("fp"), str(row.get("accn") or "")))
    return out


def _is_quarter(days: Optional[int]) -> bool:
    return days is not None and QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]


def _is_year(days: Optional[int]) -> bool:
    return days is not None and YEAR_DAYS[0] <= days <= YEAR_DAYS[1]


def _near(series: Mapping[date, Any], when: date) -> bool:
    """Whether *series* already has a period ending within a few days of *when*."""
    return any(abs((d - when).days) <= SAME_PERIOD_DAYS for d in series)


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

def split_factor(filed: date, splits: Sequence[tuple[date, float]]) -> float:
    """How many of today's shares one share was, for a filing made on *filed*.

    The product of every split ratio whose effective date falls after the
    filing. A reverse split is a ratio below one and composes the same way.
    """
    factor = 1.0
    for when, ratio in splits:
        if when > filed and ratio and ratio > 0:
            factor *= ratio
    return factor


def rebase(facts: Iterable[Fact], splits: Sequence[tuple[date, float]],
           kind: str) -> list[Fact]:
    """Put per-share and share-count facts on today's share basis."""
    facts = list(facts)
    if not splits or kind not in ("per_share", "average"):
        return facts
    out = []
    for fact in facts:
        factor = split_factor(fact.filed, splits)
        if factor == 1.0:
            out.append(fact)
        elif kind == "per_share":
            out.append(replace(fact, val=fact.val / factor))
        else:
            out.append(replace(fact, val=fact.val * factor))
    return out


# ---------------------------------------------------------------------------
# One concept -> one series
# ---------------------------------------------------------------------------

def latest(facts: Iterable[Fact]) -> dict[tuple[Optional[date], date], Fact]:
    """The newest filing of each (start, end) period."""
    best: dict[tuple[Optional[date], date], Fact] = {}
    for fact in facts:
        key = (fact.start, fact.end)
        cur = best.get(key)
        if cur is None or (fact.filed, fact.accn) > (cur.filed, cur.accn):
            best[key] = fact
    return best


def _filed_quarters(periods: Mapping[tuple[Optional[date], date], Fact]) -> dict[date, Point]:
    out: dict[date, Point] = {}
    for (start, end), fact in sorted(periods.items(), key=lambda kv: kv[1].filed):
        if start is None or not _is_quarter((end - start).days):
            continue
        # Newest filing wins, and a second quarter "ending" a few days from one
        # already held is the same quarter under a different date.
        for held in [d for d in out if d != end and abs((d - end).days) <= SAME_PERIOD_DAYS]:
            del out[held]
        out[end] = Point(end, fact.val, start, None, fact.filed)
    return out


def _three_before(series: Mapping[date, Point], start: date, end: date) -> Optional[list[Point]]:
    """The first three quarters of the fiscal year (start, end), if contiguous."""
    inside = sorted((p for p in series.values()
                     if p.start is not None and p.start >= start - timedelta(days=4)
                     and p.end < end - timedelta(days=SAME_PERIOD_DAYS)),
                    key=lambda p: p.end)
    if len(inside) != 3:
        return None
    if abs((inside[0].start - start).days) > 4:
        return None
    for a, b in zip(inside, inside[1:]):
        if abs((b.start - a.end).days - 1) > 4:
            return None
    if not _is_quarter((end - inside[-1].end).days):
        return None
    return inside


def quarterly_flows(facts: Iterable[Fact]) -> dict[date, Point]:
    """Quarterly values of an additive line (revenue, cash flow, EPS)."""
    periods = latest(facts)
    out = _filed_quarters(periods)

    # Same start, ends a quarter apart: the later figure minus the earlier is
    # that quarter. Within a fiscal year this yields Q2 from H1 - Q1, Q3 from
    # 9M - H1 and Q4 from FY - 9M; a trailing-twelve-month column (Amazon files
    # them) shares its start with nothing a quarter shorter, so it adds none.
    chains: dict[date, list[Fact]] = {}
    for (start, _end), fact in periods.items():
        if start is not None:
            chains.setdefault(start, []).append(fact)
    for chain in chains.values():
        chain.sort(key=lambda f: f.end)
        for a, b in zip(chain, chain[1:]):
            if not _is_quarter((b.end - a.end).days) or _near(out, b.end):
                continue
            how = "q4" if _is_year(b.days) else "ytd"
            out[b.end] = Point(b.end, b.val - a.val, a.end + timedelta(days=1), how,
                               max(a.filed, b.filed))

    # No nine-month figure to subtract: the year less three contiguous quarters.
    for (start, end), fact in periods.items():
        if start is None or not _is_year((end - start).days) or _near(out, end):
            continue
        three = _three_before(out, start, end)
        if three is None:
            continue
        out[end] = Point(end, fact.val - sum(p.val for p in three),
                         three[-1].end + timedelta(days=1), "fyq",
                         max([fact.filed] + [p.filed for p in three if p.filed]))
    return out


def quarterly_averages(facts: Iterable[Fact]) -> dict[date, Point]:
    """Quarterly values of a weighted-average share count, which do not add."""
    periods = latest(facts)
    out = _filed_quarters(periods)
    for (start, end), fact in periods.items():
        if start is None or not _is_year((end - start).days) or _near(out, end):
            continue
        three = _three_before(out, start, end)
        if three is None:
            continue
        estimate = 4 * fact.val - sum(p.val for p in three)
        # A buyback or an issue moves a count by percent, not by half; outside
        # that the identity has failed (unequal quarters, a restated year) and
        # the full-year average is the honest fallback.
        if three[-1].val > 0 and 0.5 <= estimate / three[-1].val <= 2.0:
            how, val = "avg4", estimate
        else:
            how, val = "fyavg", fact.val
        out[end] = Point(end, val, three[-1].end + timedelta(days=1), how, fact.filed)
    return out


def instants(facts: Iterable[Fact]) -> dict[date, Point]:
    """Balance-sheet values, newest filing per date."""
    out: dict[date, Point] = {}
    for fact in facts:
        if fact.start is not None:
            continue
        cur = out.get(fact.end)
        if cur is None or fact.filed > (cur.filed or date.min):
            out[fact.end] = Point(fact.end, fact.val, None, None, fact.filed)
    return out


def _nominal_year(end: date) -> int:
    """The calendar year a fiscal year sits in before any company's naming.

    A 52/53-week year that closes on the Saturday nearest 31 December can end
    on 2 January; it is still the previous year's.
    """
    return (end - timedelta(days=10)).year


def fiscal_years(fact_lists: Iterable[Iterable[Fact]]) -> dict[date, tuple[date, Optional[int]]]:
    """Fiscal-year end -> (start, the company's own name for the year).

    The name is the ``fy`` of the annual filing whose *own* year it is -- that
    filing's latest one-year period. The earliest filing to mention a year is
    not good enough: TSMC's first XBRL 20-F (fiscal 2017) carries 2015 and 2016
    as comparatives, every one of them tagged fy=2017, and the chart read
    "FY17, FY17, FY17". A year that no filing of its own covers takes the
    company's usual offset between its names and the calendar, which is not
    always zero: Home Depot calls the year to February 2026 fiscal 2025.
    """
    periods: dict[date, date] = {}
    current: dict[str, Fact] = {}
    for facts in fact_lists:
        for fact in facts:
            if fact.form not in ANNUAL_FORMS or not _is_year(fact.days) or fact.start is None:
                continue
            periods.setdefault(fact.end, fact.start)
            key = fact.accn or f"{fact.filed.isoformat()}/{fact.form}"
            held = current.get(key)
            if held is None or fact.end > held.end:
                current[key] = fact
    named: dict[date, int] = {}
    for fact in current.values():
        if fact.fy and fact.fp in (None, "FY"):
            named.setdefault(fact.end, fact.fy)
    offsets = Counter(fy - _nominal_year(end) for end, fy in named.items())
    offset = offsets.most_common(1)[0][0] if offsets else 0
    return {end: (start, named.get(end, _nominal_year(end) + offset))
            for end, start in periods.items()}


def annual_points(facts: Iterable[Fact], years: Mapping[date, tuple[date, Optional[int]]],
                  kind: str) -> dict[date, Point]:
    """One value per fiscal year, keyed by the year's end."""
    facts = list(facts)
    if kind == "instant":
        held = instants(facts)
        return {end: held[end] for end in years if end in held}
    periods = latest(facts)
    by_end: dict[date, list[Fact]] = {}
    for (start, end), fact in periods.items():
        if start is not None:
            by_end.setdefault(end, []).append(fact)
    out: dict[date, Point] = {}
    for end, (start, _fy) in years.items():
        match = periods.get((start, end))
        if match is None:
            # A 52/53-week year is sometimes tagged a few days off its start.
            match = next((f for f in by_end.get(end, ())
                          if abs((f.start - start).days) <= 7), None)
        if match is not None:
            out[end] = Point(end, match.val, match.start, None, match.filed)
    return out


def quarter_labels(ends: Iterable[date],
                   years: Mapping[date, tuple[date, Optional[int]]]) -> dict[date, str]:
    """Quarter end -> "Q2 FY2026", from where the quarter falls in its fiscal year.

    Worked out from the fiscal years rather than read off each 10-Q's own
    ``fp``/``fy``, for the reason :func:`fiscal_years` gives: a quarter that
    first appears as a comparative carries the tags of the filing a year later.
    Quarters past the last annual report run on into the fiscal years after it.
    """
    fys = sorted((end, start, fy) for end, (start, fy) in years.items() if fy)
    labels: dict[date, str] = {}
    if not fys:
        return labels
    for d in ends:
        frame = next(((start, fy) for end, start, fy in fys
                      if start - timedelta(days=4) <= d <= end + timedelta(days=4)), None)
        if frame is None:
            if d > fys[-1][0]:
                start, fy = fys[-1][0] + timedelta(days=1), fys[-1][2] + 1
                while (d - start).days > 375:
                    start, fy = start + timedelta(days=364), fy + 1
            elif d < fys[0][1]:
                start, fy = fys[0][1] - timedelta(days=364), fys[0][2] - 1
                while (start - d).days > 0:
                    start, fy = start - timedelta(days=364), fy - 1
            else:
                continue
            frame = (start, fy)
        start, fy = frame
        q = round((d - start).days / 91.3)
        if 1 <= q <= 4:
            labels[d] = f"Q{q} FY{fy}"
    return labels


# ---------------------------------------------------------------------------
# Several concepts -> one series
# ---------------------------------------------------------------------------

def _quarter_index(d: date) -> int:
    return d.year * 4 + (d.month - 1) // 3


def _disagreement(a: Mapping[date, Point], b: Mapping[date, Point],
                  shared: Sequence[date]) -> float:
    gaps = []
    for d in shared:
        x, y = a[d].val, b[d].val
        scale = max(abs(x), abs(y))
        gaps.append(0.0 if scale == 0 else abs(x - y) / scale)
    return statistics.median(gaps) if gaps else math.inf


def _adjacent(series: Mapping[date, Point], merged: Mapping[date, Point],
              max_gap_days: int) -> bool:
    """A candidate that ends where the merged series begins, at a plausible level.

    The splice of last resort: a tag switch that no filing ever reported both
    ways. It is accepted only when the two sit end to end and the boundary
    values are within a factor of two, which a total against a component
    usually is not.
    """
    last, first = max(series), min(merged)
    if last >= first or (first - last).days > max_gap_days:
        return False
    a, b = series[last].val, merged[first].val
    if a == 0 or b == 0 or (a > 0) != (b > 0):
        return False
    return 0.5 <= a / b <= 2.0


def splice(candidates: Sequence[tuple[str, Mapping[date, Point]]], *,
           tolerance: float = SPLICE_TOLERANCE,
           max_gap_days: int = 125) -> tuple[dict[date, Point], list[str]]:
    """Merge one metric's candidate concepts into one series.

    The base is whichever candidate reaches the most recent period, ties going
    to the earlier candidate -- a company's current tag is the one it means
    now. Others fill only periods the merged series lacks, and only if they
    agree with it on the periods both report; repeated until nothing changes,
    so a concept that overlaps only a later splice still gets its turn.
    """
    usable = [(name, dict(series)) for name, series in candidates if series]
    if not usable:
        return {}, []
    base = min(range(len(usable)),
               key=lambda i: (-_quarter_index(max(usable[i][1])), i))
    merged = dict(usable[base][1])
    used = [usable[base][0]]
    pending = [item for i, item in enumerate(usable) if i != base]
    changed = True
    while changed and pending:
        changed = False
        for item in list(pending):
            name, series = item
            extra = {d: p for d, p in series.items() if not _near(merged, d)}
            if not extra:
                pending.remove(item)
                continue
            shared = sorted(d for d in series if d in merged)[:SPLICE_EVIDENCE]
            if shared:
                ok = _disagreement(series, merged, shared) <= tolerance
            else:
                ok = _adjacent(series, merged, max_gap_days)
            if ok:
                merged.update(extra)
                used.append(name)
                pending.remove(item)
                changed = True
    return merged, used


def combine(a: Mapping[date, Point], b: Mapping[date, Point],
            op: Callable[[float, float], float], *, b_default: Optional[float] = None,
            how: Optional[str] = "calc") -> dict[date, Point]:
    """A line computed from two others, wherever both are known."""
    out: dict[date, Point] = {}
    for d, pa in a.items():
        pb = b.get(d)
        if pb is None and b_default is None:
            continue
        bv = pb.val if pb is not None else b_default
        filed = max(pa.filed or date.min, (pb.filed if pb is not None else None) or date.min)
        out[d] = Point(d, op(pa.val, bv), pa.start, how, filed)
    return out


# ---------------------------------------------------------------------------
# The metric registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """kind: flow | per_share | average | instant."""

    kind: str
    concepts: tuple[str, ...]
    nonneg: bool = False


US_GAAP: dict[str, Spec] = {
    "revenue": Spec("flow", (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "RevenuesNetOfInterestExpense",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
    ), nonneg=True),
    "cost_of_revenue": Spec("flow", (
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "CostOfServices",
    ), nonneg=True),
    "gross_profit": Spec("flow", ("GrossProfit",)),
    # Read only to vet a cost-of-revenue line before computing gross profit.
    "costs_total": Spec("flow", ("CostsAndExpenses",)),
    "operating_income": Spec("flow", ("OperatingIncomeLoss",)),
    "net_income": Spec("flow", (
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    )),
    "eps": Spec("per_share", ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")),
    "ocf": Spec("flow", (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    )),
    "capex": Spec("flow", (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    )),
    "rnd": Spec("flow", (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    )),
    "sbc": Spec("flow", ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense")),
    "buybacks": Spec("flow", ("PaymentsForRepurchaseOfCommonStock",)),
    "dividends": Spec("flow", ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock")),
    "shares": Spec("average", (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    )),
    "cash": Spec("instant", (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    )),
    "debt_total": Spec("instant", ("LongTermDebt", "DebtLongtermAndShorttermCombinedAmount")),
    # Oracle files its bonds as notes payable, not as long-term debt -- and
    # under a different notes concept in its 10-Qs than in its 10-Ks.
    "debt_noncurrent": Spec("instant", ("LongTermDebtNoncurrent", "LongTermNotesPayable",
                                        "LongTermNotesAndLoans",
                                        "LongTermDebtAndCapitalLeaseObligations")),
    "debt_current": Spec("instant", ("LongTermDebtCurrent", "NotesPayableCurrent",
                                     "LongTermDebtAndCapitalLeaseObligationsCurrent")),
}

#: Concepts only an insurer files. Their cost of revenue is claims, which no
#: cost-of-goods concept carries -- UnitedHealth's ``CostOfGoodsAndServicesSold``
#: is the pharmacy's cost of products alone, so revenue less it read as an 88%
#: gross margin. An insurer gets no computed gross profit at all.
INSURER_CONCEPTS = ("PremiumsEarnedNet", "PolicyholderBenefitsAndClaimsIncurredNet")

#: A cost-of-revenue line smaller than this share of total costs and expenses
#: is a component, not the cost of revenue: McDonald's files one at 18% of its
#: costs, so revenue less it read as a 90% gross margin. Meta's real one is 31%,
#: and a filer with no total-costs line is given the benefit of the doubt.
MIN_COST_SHARE = 0.25


def _cost_is_whole(cost: Mapping[date, Point], total: Mapping[date, Point]) -> bool:
    shares = [cost[d].val / total[d].val for d in cost if d in total and total[d].val > 0]
    return not shares or statistics.median(shares) >= MIN_COST_SHARE

#: The IFRS names for the same lines. Foreign private issuers file these in a
#: 20-F once a year, so this side is annual in practice.
IFRS: dict[str, Spec] = {
    "revenue": Spec("flow", ("Revenue", "RevenueFromContractsWithCustomers"), nonneg=True),
    "cost_of_revenue": Spec("flow", ("CostOfSales",), nonneg=True),
    "gross_profit": Spec("flow", ("GrossProfit",)),
    "costs_total": Spec("flow", ()),
    "operating_income": Spec("flow", ("ProfitLossFromOperatingActivities",)),
    "net_income": Spec("flow", ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss")),
    "eps": Spec("per_share", ("DilutedEarningsLossPerShare", "BasicAndDilutedEarningsLossPerShare")),
    "ocf": Spec("flow", ("CashFlowsFromUsedInOperatingActivities",)),
    "capex": Spec("flow", (
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "PurchaseOfPropertyPlantAndEquipment",
    )),
    "rnd": Spec("flow", ("ResearchAndDevelopmentExpense",)),
    "sbc": Spec("flow", ()),
    "buybacks": Spec("flow", ("PaymentsToAcquireOrRedeemEntitysShares",)),
    "dividends": Spec("flow", ("DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid")),
    "shares": Spec("average", ("AdjustedWeightedAverageShares", "WeightedAverageShares")),
    "cash": Spec("instant", ("CashAndCashEquivalents",)),
    "debt_total": Spec("instant", ("Borrowings",)),
    "debt_noncurrent": Spec("instant", ("NoncurrentPortionOfNoncurrentBorrowings",)),
    "debt_current": Spec("instant", ("CurrentPortionOfNoncurrentBorrowings",)),
}

TAXONOMIES: dict[str, dict[str, Spec]] = {"us-gaap": US_GAAP, "ifrs-full": IFRS}

#: The metrics the page draws, in payload order. Flows sum to a TTM; the rest
#: are carried across from the quarter.
FLOW_METRICS = ("revenue", "gross_profit", "operating_income", "net_income", "eps",
                "ocf", "capex", "fcf", "rnd", "sbc", "buybacks", "dividends")
POINT_METRICS = ("shares", "cash", "debt")
RATIO_METRICS = ("gross_margin", "operating_margin", "net_margin")
METRICS = FLOW_METRICS + POINT_METRICS + RATIO_METRICS

#: Lines that define which periods exist. Everything else is aligned to these,
#: so one stray fact under an obscure concept cannot add a column of blanks.
ANCHORS = ("revenue", "net_income", "eps", "ocf")


def choose_basis(companyfacts: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(taxonomy, currency) carrying the most revenue and net-income facts.

    A 20-F filer can report the same year twice, in its own currency and as a
    US-dollar "convenience translation" of the latest year at one fixed rate.
    The unit with the most facts is the real series; the other is not history.
    """
    facts = companyfacts.get("facts") or {}
    best: Optional[tuple[str, str, int]] = None
    for taxonomy, specs in TAXONOMIES.items():
        concepts = facts.get(taxonomy) or {}
        counts: dict[str, int] = {}
        for name in specs["revenue"].concepts + specs["net_income"].concepts:
            units = (concepts.get(name) or {}).get("units") or {}
            for unit, rows in units.items():
                if "/" in unit:
                    continue
                counts[unit] = counts.get(unit, 0) + sum(1 for r in rows if r.get("form") in FORMS)
        if not counts:
            continue
        unit, n = max(counts.items(), key=lambda kv: (kv[1], kv[0] == "USD"))
        if n and (best is None or n > best[2]):
            best = (taxonomy, unit, n)
    return (best[0], best[1]) if best else (None, None)


def _unit(kind: str, currency: str) -> str:
    if kind == "per_share":
        return f"{currency}/shares"
    if kind == "average":
        return "shares"
    return currency


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _canonical_ends(series: Sequence[Mapping[date, Point]]) -> list[date]:
    """One end date per period, taken from the first series that has it.

    Within a company every line of a quarter shares an end date, nearly always.
    The exception -- one concept tagged a day or two off the rest -- would
    otherwise become a second, almost-empty column beside the real one.
    """
    ends: list[date] = []
    for s in series:
        for d in sorted(s):
            i = bisect.bisect_left(ends, d)
            if any(0 <= j < len(ends) and abs((ends[j] - d).days) <= SAME_PERIOD_DAYS
                   for j in (i - 1, i)):
                continue
            ends.insert(i, d)
    return ends


def _align(series: Mapping[date, Point], ends: Sequence[date]) -> dict[date, Point]:
    """Re-key *series* onto *ends*, matching within :data:`SAME_PERIOD_DAYS`."""
    out: dict[date, Point] = {}
    for d, p in series.items():
        i = bisect.bisect_left(ends, d)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ends) and abs((ends[j] - d).days) <= SAME_PERIOD_DAYS:
                if best is None or abs((ends[j] - d).days) < abs((best - d).days):
                    best = ends[j]
        if best is not None and best not in out:
            out[best] = p
    return out


def _fill_gaps(ends: list[date]) -> list[date]:
    """Insert empty quarters into a gap, so a missing year still takes up a year.

    The page draws a category axis, which spaces points evenly: without this a
    company that skipped two years of filings would show them as adjacent bars.
    """
    if not ends:
        return ends
    out = [ends[0]]
    for nxt in ends[1:]:
        while (nxt - out[-1]).days > QUARTER_DAYS[1] + 3:
            out.append(out[-1] + timedelta(days=91))
        out.append(nxt)
    return out


def trailing(ends: Sequence[date], values: Sequence[Optional[float]]) -> list[Optional[float]]:
    """Trailing-twelve-month sums over four consecutive quarters.

    Consecutive means each of the three steps between the four ends is a
    quarter long, so a missing quarter -- even one hidden by a gap the page
    filled -- breaks the window instead of quietly summing three.
    """
    out: list[Optional[float]] = []
    for i in range(len(values)):
        window = values[i - 3:i + 1] if i >= 3 else []
        if len(window) != 4 or any(v is None for v in window):
            out.append(None)
            continue
        steps = [(ends[k] - ends[k - 1]).days for k in range(i - 2, i + 1)]
        if not all(_is_quarter(s) for s in steps):
            out.append(None)
            continue
        out.append(sum(window))  # type: ignore[arg-type]
    return out


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _round(metric: str, value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    if metric == "eps":
        return round(value, 4)
    if metric in RATIO_METRICS:
        return round(value, 4)
    return float(round(value))


def price_on(prices: Sequence[tuple[date, float]], when: date, *,
             max_lag_days: int = 10) -> Optional[float]:
    """The last close on or before *when*, if it is recent enough to stand for it."""
    if not prices:
        return None
    idx = bisect.bisect_right([d for d, _ in prices], when) - 1
    if idx < 0:
        return None
    day, close = prices[idx]
    if (when - day).days > max_lag_days or close <= 0:
        return None
    return close


def build(companyfacts: Mapping[str, Any], *,
          splits: Optional[Sequence[tuple[date, float]]] = None,
          prices: Optional[Sequence[tuple[date, float]]] = None) -> dict[str, Any]:
    """The whole payload for one company, minus the I/O stamps.

    ``splits`` is ``None`` when the split history could not be read, which is
    different from ``[]`` (read, and there were none): with it unknown, the
    per-share and share-count lines are withheld rather than drawn across a
    split as a cliff. ``prices`` are (date, close) on today's share basis and
    not dividend-adjusted.
    """
    taxonomy, currency = choose_basis(companyfacts)
    if taxonomy is None or currency is None:
        return {"unavailable": "no_statements"}
    specs = TAXONOMIES[taxonomy]
    concepts = (companyfacts.get("facts") or {}).get(taxonomy) or {}
    split_list = sorted(splits or [])

    raw: dict[str, list[list[Fact]]] = {}
    for metric, spec in specs.items():
        unit = _unit(spec.kind, currency)
        raw[metric] = [rebase(parse_facts(concepts.get(c), unit), split_list, spec.kind)
                       for c in spec.concepts]

    anchor_facts = [facts for m in ANCHORS for facts in raw.get(m, [])]
    years = fiscal_years(anchor_facts)
    domestic = any(f.form in DOMESTIC_FORMS for facts in anchor_facts for f in facts)

    quarterly: dict[str, dict[date, Point]] = {}
    annual: dict[str, dict[date, Point]] = {}
    used: dict[str, list[str]] = {}
    for metric, spec in specs.items():
        q_cands, a_cands = [], []
        for concept, facts in zip(spec.concepts, raw[metric]):
            if spec.kind == "instant":
                q = instants(facts)
            elif spec.kind == "average":
                q = quarterly_averages(facts)
            else:
                q = quarterly_flows(facts)
            if spec.nonneg:
                # A derived quarter below zero on a line that cannot be negative
                # is a restated year minus an unrestated nine months.
                q = {d: p for d, p in q.items() if p.how is None or p.val >= 0}
            q_cands.append((concept, q))
            a_cands.append((concept, annual_points(facts, years, spec.kind)))
        quarterly[metric], names_q = splice(q_cands)
        annual[metric], names_a = splice(a_cands, max_gap_days=380)
        used[metric] = list(dict.fromkeys(names_q + names_a))

    # Lines computed from others. Filed gross profit wins wherever it exists;
    # Amazon files none after 2009, so there it is revenue less cost of sales.
    insurer = any(parse_facts(concepts.get(c), currency) for c in INSURER_CONCEPTS)
    whole = (_cost_is_whole(quarterly["cost_of_revenue"], quarterly["costs_total"])
             and _cost_is_whole(annual["cost_of_revenue"], annual["costs_total"]))
    for view, gap in ((quarterly, 125), (annual, 380)):
        computed = {} if insurer or not whole else combine(
            view["revenue"], view["cost_of_revenue"], lambda x, y: x - y)
        view["gross_profit"], names = splice(
            [("GrossProfit", view["gross_profit"]), ("revenue-cost", computed)], max_gap_days=gap)
        if "revenue-cost" in names and "revenue-cost" not in used["gross_profit"]:
            used["gross_profit"].append("revenue-cost")
        parts = combine(view["debt_noncurrent"], view["debt_current"], lambda x, y: x + y,
                        b_default=0.0)
        view["debt"], _ = splice([("total", view["debt_total"]), ("parts", parts)],
                                 max_gap_days=gap)
        # Every free-cash-flow figure is a difference by definition, so it is
        # not flagged point by point; the card says what it is.
        view["fcf"] = combine(view["ocf"], view["capex"], lambda x, y: x - y, how=None)
        # Alphabet and others file a company-wide diluted share count only in
        # recent years -- before that it is per share class -- while net income
        # and diluted EPS are company-wide throughout. Their ratio is the share
        # count to within EPS's rounding to the cent; at $0.25 or more that is
        # under 2%, below it the ratio is not trusted. It is spliced like any
        # other candidate, so where a filed count exists the two must agree.
        implied = {d: Point(d, view["net_income"][d].val / p.val, p.start, "calc", p.filed)
                   for d, p in view["eps"].items()
                   if d in view["net_income"] and abs(p.val) >= 0.25
                   and (view["net_income"][d].val > 0) == (p.val > 0)}
        view["shares"], names = splice([("filed", view["shares"]), ("net-income/eps", implied)],
                                       max_gap_days=gap)
        if "net-income/eps" in names and "net-income/eps" not in used["shares"]:
            used["shares"].append("net-income/eps")
    used["debt"] = list(dict.fromkeys(used.pop("debt_total") + used.pop("debt_noncurrent")
                                      + used.pop("debt_current")))
    used["fcf"] = used["ocf"] + used["capex"]
    used.pop("cost_of_revenue", None)
    used.pop("costs_total", None)

    if not domestic:
        block = "valuation_foreign" if currency == "USD" else "valuation_currency"
    else:
        block = None if currency == "USD" else "valuation_currency"
    latest_fact = max((f for facts in anchor_facts for f in facts),
                      key=lambda f: (f.filed, f.end), default=None)
    return assemble(
        quarterly, annual, years=years, labels=None, prices=prices,
        splits_known=splits is not None, valuation_block=block, used=used,
        basis={"taxonomy": taxonomy, "currency": currency,
               "filer": "domestic" if domestic else "foreign"},
        latest=None if latest_fact is None else {
            "filed": latest_fact.filed.isoformat(), "form": latest_fact.form,
            "end": latest_fact.end.isoformat(), "fy": latest_fact.fy, "fp": latest_fact.fp})


def assemble(quarterly: dict[str, dict[date, Point]], annual: dict[str, dict[date, Point]], *,
             years: Mapping[date, tuple[date, Optional[int]]], labels: Optional[Mapping[date, str]],
             prices: Optional[Sequence[tuple[date, float]]], splits_known: bool,
             valuation_block: Optional[str], basis: dict[str, Any],
             used: dict[str, list[str]], latest: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Per-metric series -> the payload the page draws. Shared by every source.

    ``quarterly`` / ``annual`` map each metric to its points; whichever source
    produced them (EDGAR here, Yahoo's statements in :mod:`ystocker.statements`)
    the views, the trailing-twelve-month sums, the margins and the valuation
    are computed one way. ``valuation_block`` names why P/E and P/S must not be
    computed at all (a currency mismatch, a foreign filer), or is ``None``.
    """
    notes: list[str] = []
    if not splits_known:
        notes.append("splits_unknown")
        for view in (quarterly, annual):
            view["eps"], view["shares"] = {}, {}

    q_ends = _fill_gaps(_canonical_ends([quarterly.get(m, {}) for m in ANCHORS]))[-MAX_QUARTERS:]
    a_ends = sorted(d for d in years if any(d in annual.get(m, {}) for m in ANCHORS))[-MAX_YEARS:]
    if not q_ends and not a_ends:
        return {"unavailable": "no_statements"}
    if not q_ends:
        notes.append("annual_only")
    q_aligned = {m: _align(s, q_ends) for m, s in quarterly.items()}
    if labels is None:
        labels = quarter_labels(q_ends, years)

    q_block = _view_block(q_aligned, q_ends, labels.get)
    a_block = _view_block(annual, a_ends,
                          lambda d: f"FY{years[d][1]}" if years.get(d) and years[d][1] else None)

    ttm: dict[str, list[Optional[float]]] = {}
    for metric in FLOW_METRICS:
        ttm[metric] = [_round(metric, v) for v in trailing(q_ends, q_block["values"][metric])]
    for metric in POINT_METRICS:
        ttm[metric] = list(q_block["values"][metric])
    _add_margins(ttm)

    valuation = None
    if valuation_block:
        notes.append(valuation_block)
    elif not q_ends:
        pass
    elif not splits_known or not prices:
        notes.append("valuation_no_prices")
    else:
        valuation = _valuation(q_ends, ttm, prices)

    return {
        "basis": basis,
        "quarterly": q_block,
        "ttm": {"end": q_block["end"], "values": ttm},
        "annual": a_block,
        "valuation": valuation,
        "concepts": used,
        "latest": latest,
        "notes": notes,
    }


def _add_margins(values: dict[str, list[Optional[float]]]) -> None:
    rev = values["revenue"]
    for name, line in (("gross_margin", "gross_profit"), ("operating_margin", "operating_income"),
                       ("net_margin", "net_income")):
        values[name] = [_round(name, _ratio(x, r)) for x, r in zip(values[line], rev)]


def _view_block(series: Mapping[str, Mapping[date, Point]], ends: Sequence[date],
                label_of: Callable[[date], Optional[str]]) -> dict[str, Any]:
    """One view (quarterly or annual) as parallel arrays, plus the derived flags."""
    values: dict[str, list[Optional[float]]] = {}
    how: dict[str, dict[str, str]] = {}
    for metric in FLOW_METRICS + POINT_METRICS:
        col = [series.get(metric, {}).get(d) for d in ends]
        values[metric] = [_round(metric, p.val if p else None) for p in col]
        flags = {str(i): p.how for i, p in enumerate(col) if p is not None and p.how}
        if flags:
            how[metric] = flags
    _add_margins(values)
    starts = []
    for d in ends:
        start = next((series[m][d].start for m in ANCHORS
                      if d in series.get(m, {}) and series[m][d].start), None)
        starts.append(start.isoformat() if start else None)
    return {"end": [d.isoformat() for d in ends], "start": starts,
            "label": [label_of(d) for d in ends], "values": values, "how": how}


def _valuation(ends: Sequence[date], ttm: Mapping[str, Sequence[Optional[float]]],
               prices: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """P/E and P/S at each quarter end, on trailing-twelve-month figures.

    The share count is that quarter's diluted weighted average, the same basis
    the EPS was struck on, so the two ratios describe one company at one
    moment. A non-positive TTM EPS gives no P/E at all: a negative multiple is
    not a cheap one, it is not a measurement.
    """
    prices = sorted(prices)
    price, pe, ps, cap = [], [], [], []
    for i, end in enumerate(ends):
        p = price_on(prices, end)
        eps, rev, shares = ttm["eps"][i], ttm["revenue"][i], ttm["shares"][i]
        price.append(None if p is None else round(p, 2))
        pe.append(round(p / eps, 2) if p and eps and eps > 0 else None)
        mcap = p * shares if p and shares and shares > 0 else None
        cap.append(None if mcap is None else float(round(mcap)))
        ps.append(round(mcap / rev, 2) if mcap and rev and rev > 0 else None)

    now = None
    if prices:
        day, close = prices[-1]
        last = next((i for i in range(len(ends) - 1, -1, -1)
                     if ttm["eps"][i] is not None or ttm["revenue"][i] is not None), None)
        if last is not None and close > 0:
            eps, rev, shares = ttm["eps"][last], ttm["revenue"][last], ttm["shares"][last]
            mcap = close * shares if shares and shares > 0 else None
            now = {"date": day.isoformat(), "price": round(close, 2),
                   "pe": round(close / eps, 2) if eps and eps > 0 else None,
                   "ps": round(mcap / rev, 2) if mcap and rev and rev > 0 else None,
                   "market_cap": None if mcap is None else float(round(mcap)),
                   "basis_end": ends[last].isoformat()}
    return {"price": price, "pe": pe, "ps": ps, "market_cap": cap, "now": now}
