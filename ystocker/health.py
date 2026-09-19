"""
ystocker.health
~~~~~~~~~~~~~~~
Balance-sheet strength and cash conversion: whether a company can survive, and
whether its reported profit is real money.

This module is **pure**. Every input arrives as an argument, for the reason
:mod:`ystocker.dca` and :mod:`ystocker.fcf` are pure — the arithmetic is the part
that has to be right.

Why this exists
---------------
The site could tell a reader a stock was cheap and nothing about whether the
company was fragile. ``/history`` carried twenty-two charts of valuation,
momentum and sentiment; ``/evaluation`` ranked on earnings multiples; ``/dca``
scored cheapness against a company's own history. None of them touched leverage,
liquidity, or the gap between reported profit and cash — which is the dimension
every comparable screener leads with, and the one that decides whether a low
multiple is an opportunity or a warning.

It costs no fetch. ``totalDebt``, ``totalCash``, ``currentRatio``, ``quickRatio``,
``debtToEquity``, ``operatingCashflow``, ``freeCashflow`` and ``netIncomeToCommon``
are all in the ``info`` dict ``data.fetch_ticker_data`` already pulls and
discards.

The two figures that carry the most
-----------------------------------
``net_debt_ebitda``
    How many years of operating earnings the debt represents. The standard
    leverage measure and the one covenants are written against.
``cash_conversion``
    Free cash flow over net income. Reported profit is an accounting opinion;
    cash is not. Persistently below 1 means the earnings the P/E is built on are
    not arriving as money, which is exactly the case a cheap-looking multiple
    hides — and it is the balance-sheet counterpart to the forward-FCF caveat in
    :mod:`ystocker.fcf`.

Refusals
--------
Named, never approximated, and the two that matter are sector-shaped rather than
arithmetic:

``financial_sector``
    Leverage ratios are meaningless for a bank or an insurer. Their debt is raw
    material, not financing — the same reason :mod:`ystocker.dcf` declines a
    standard DCF for them (§10). A bank shows net debt/EBITDA in the tens and
    would be flagged as distressed by any threshold that is right for an
    industrial.
``negative_ebitda`` / ``negative_income``
    Dividing by a loss gives a number with the wrong sign that still renders in
    the same column. A company with negative EBITDA is not "zero times levered".
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "REFUSALS", "FINANCIAL_SECTORS", "LEVERAGE_BANDS", "CONVERSION_BANDS",
    "assess", "leverage_band", "conversion_band",
]

REFUSALS: tuple[str, ...] = (
    "financial_sector",
    "negative_ebitda",
    "negative_income",
    "no_data",
)

#: Sectors where leverage ratios do not mean what they mean elsewhere.
#: Matched case-insensitively against Yahoo's ``sector``.
FINANCIAL_SECTORS: frozenset[str] = frozenset({
    "financial services", "financials", "financial",
})

#: Net debt / EBITDA bands. Conventional rather than invented: 3x is the usual
#: investment-grade comfort line and 4x the point most covenants start to bite.
LEVERAGE_BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "net_cash"),
    (1.0, "low"),
    (3.0, "moderate"),
    (4.0, "elevated"),
)

#: FCF / net income. Around 1.0 the reported profit is arriving as cash; well
#: below it persistently is the signal worth acting on.
CONVERSION_BANDS: tuple[tuple[float, str], ...] = (
    (0.5, "poor"),
    (0.8, "weak"),
    (1.2, "healthy"),
)


def _num(value: Any) -> Optional[float]:
    """A finite float, or None. Yahoo returns strings and NaNs in places."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def leverage_band(ratio: Optional[float]) -> Optional[str]:
    """Name the leverage bucket. ``None`` in, ``None`` out — never a default."""
    if ratio is None:
        return None
    for threshold, name in LEVERAGE_BANDS:
        if ratio < threshold:
            return name
    return "high"


def conversion_band(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    for threshold, name in CONVERSION_BANDS:
        if ratio < threshold:
            return name
    return "strong"


def assess(info: Mapping[str, Any], *, sector: Optional[str] = None) -> dict[str, Any]:
    """Balance-sheet and cash-conversion figures from one Yahoo ``info`` dict.

    Everything is optional and absent keys simply produce absent figures — this
    runs over every cached ticker including ADRs and foreign listings with thin
    coverage, and one missing field must not cost the whole block.

    ``leverage_reason`` names why there is no leverage ratio when there is not;
    it and ``net_debt_ebitda`` are never both set.
    """
    out: dict[str, Any] = {}

    debt = _num(info.get("totalDebt"))
    cash = _num(info.get("totalCash"))
    ebitda = _num(info.get("ebitda"))
    revenue = _num(info.get("totalRevenue"))
    fcf = _num(info.get("freeCashflow"))
    ocf = _num(info.get("operatingCashflow"))
    income = _num(info.get("netIncomeToCommon"))

    # --- leverage ----------------------------------------------------------
    if debt is not None and cash is not None:
        net_debt = debt - cash
        out["net_debt"] = round(net_debt, 2)
        # Reported as a positive "net cash" figure rather than a negative debt,
        # because a reader scanning a debt column reads -60 as a typo.
        out["net_cash"] = round(-net_debt, 2) if net_debt < 0 else None

        reason = None
        if sector and sector.strip().lower() in FINANCIAL_SECTORS:
            reason = "financial_sector"
        elif ebitda is None:
            reason = "no_data"
        elif ebitda <= 0:
            reason = "negative_ebitda"
        if reason:
            out["leverage_reason"] = reason
        else:
            ratio = net_debt / ebitda
            out["net_debt_ebitda"] = round(ratio, 2)
            out["leverage_band"] = leverage_band(ratio)

    for key, field in (("current_ratio", "currentRatio"),
                       ("quick_ratio", "quickRatio"),
                       ("debt_to_equity", "debtToEquity"),
                       ("payout_ratio", "payoutRatio")):
        value = _num(info.get(field))
        if value is not None:
            out[key] = round(value, 3)

    # --- cash conversion ---------------------------------------------------
    # Reported profit is an accounting opinion; cash is not. This is the gap.
    if fcf is not None and income is not None:
        if income > 0:
            out["cash_conversion"] = round(fcf / income, 3)
            out["conversion_band"] = conversion_band(fcf / income)
        else:
            # Dividing by a loss produces a number with the wrong sign that
            # renders in the same column as a healthy one.
            out["conversion_reason"] = "negative_income"

    if fcf is not None and revenue is not None and revenue > 0:
        out["fcf_margin"] = round(fcf / revenue * 100, 2)
    if fcf is not None and ocf is not None and ocf > 0:
        # How much of the cash the business generates survives capex.
        out["capex_intensity"] = round((1 - fcf / ocf) * 100, 2)

    for key, field in (("roa", "returnOnAssets"),
                       ("operating_margin", "operatingMargins")):
        value = _num(info.get(field))
        if value is not None:
            out[key] = round(value * 100, 2)

    return out
