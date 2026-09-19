"""
ystocker.fcf
~~~~~~~~~~~~
Cash-flow multiples for the valuation dashboard: the measured trailing ones, and
a *stated estimate* of the forward ones.

This module is **pure**. No network, no cache, no clock. Every input arrives as
an argument, for the reason :mod:`ystocker.dca` and :mod:`ystocker.lookthrough`
are pure: the arithmetic is the part that has to be right, and it is only cheaply
provable if proving it needs no I/O.

Why a forward FCF at all
------------------------
``/evaluation`` scores companies on earnings — P/E, forward P/E, PEG, EPS growth
— and had no cash-flow dimension whatsoever, while ``FCF ($B)`` sat in every
cached record, fetched every eight hours and never rendered. The trailing
multiples below therefore cost nothing new: they are arithmetic on data already
in hand.

The forward figure is different in kind and is treated differently. Nobody
publishes a consensus free cash flow the way they publish a consensus EPS, so it
has to be *derived*, and the only honest derivation available here is to grow
trailing FCF by expected earnings growth::

    FCF_fwd ≈ FCF_ttm × g        where g is the expected 1-year earnings growth

That approximation is defensible and it is also wrong in a specific, knowable
way, which the page has to say out loud: **free cash flow and earnings diverge
exactly when the difference matters.** A company entering a capex cycle, unwinding
working capital, or paying much of its staff in stock will have FCF and EPS
moving apart, and those are precisely the companies somebody is looking at cash
flow to understand. So this is an estimate whose error is largest where it is
most consulted, and it must never render like the measured column beside it.

Two sources for ``g``, and the order matters
--------------------------------------------
``consensus``
    ``eps_trend``'s next fiscal year over the current one. A clean
    year-over-year comparison of two figures struck on the same basis.
``pe_ratio``
    ``PE_ttm / PE_fwd``, which is algebraically the implied growth since both
    share the same price. Available for every cached ticker, so it is the
    fallback — but it compares a *trailing twelve month* denominator against a
    *forward* one, and where in its fiscal year a company happens to be changes
    what period that spans. That is the same basis error ``dca_history``
    documents at length, so it is second, never first.

Measured on the live universe: 212 of 222 analyst-covered tickers have both
fiscal years positive and usable, the median growth factor is 1.12, four exceed
2.0 and none fall below 0.5.

Refusals
--------
Five, and each one is a case where a plausible-looking number would render in the
same column, in the same font, as a measured multiple:

``no_fcf`` / ``negative_fcf``
    A negative trailing FCF scaled by a growth factor is not a forecast — it
    asserts the company keeps burning cash and burns proportionally more. And
    "price ÷ negative cash flow" is not a multiple at all; sorted into a column
    headed cheapest it would appear at the top.
``negative_earnings``
    A negative P/E makes ``PE_ttm / PE_fwd`` negative, so a company crossing into
    profit — the single most interesting case — would be handed a negative
    forward FCF. The consensus path has the same hazard when either fiscal year
    is a loss.
``no_growth``
    Neither source available. Substituting 1.0 would silently republish the
    trailing multiple as a forward one.
``growth_out_of_band``
    ``MRK`` currently shows a consensus factor of 3.48 because its current
    fiscal year carries a charge, not because cash flow is about to treble.
    Scaling FCF by a depressed-base artefact produces a number that is wrong by
    the size of the artefact.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "REFUSALS", "GROWTH_MIN", "GROWTH_MAX",
    "growth_factor", "trailing", "estimate",
]

#: Every reason this module declines to produce a forward figure.
REFUSALS: tuple[str, ...] = (
    "no_fcf",
    "negative_fcf",
    "no_market_cap",
    "no_growth",
    "negative_earnings",
    "growth_out_of_band",
)

#: The band a one-year earnings growth factor has to fall in to be used.
#:
#: Not a clamp. A factor outside this range is far more often a depressed or
#: inflated base than a real forecast — a company lapping a one-off charge shows
#: an enormous ratio that says nothing about next year's cash — and clamping it
#: to the edge would keep the wrong number while hiding that it was wrong.
#: Measured on the live universe these bounds exclude four names and admit 208.
GROWTH_MIN = 0.4
GROWTH_MAX = 2.5


def _pos(value: Any) -> bool:
    """True for a finite, strictly positive number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v > 0 and v == v and v not in (float("inf"), float("-inf"))


def growth_factor(*,
                  eps_fy0: Optional[float] = None,
                  eps_fy1: Optional[float] = None,
                  pe_ttm: Optional[float] = None,
                  pe_fwd: Optional[float] = None) -> dict[str, Any]:
    """Expected one-year earnings growth, as a multiplicative factor.

    Consensus first, the P/E ratio second — see the module docstring on why the
    order is not arbitrary.

    Returns ``{"factor", "source"}`` or ``{"factor": None, "reason"}``. Never
    returns 1.0 as a stand-in: "no growth expected" and "growth unknown" are
    different statements and only the first is a forecast.
    """
    # Consensus: next fiscal year over the current one.
    if eps_fy0 is not None and eps_fy1 is not None:
        if _pos(eps_fy0) and _pos(eps_fy1):
            return _banded(float(eps_fy1) / float(eps_fy0), "consensus")
        # A loss year on either side makes the ratio meaningless rather than
        # merely uncertain, so this does not fall through to the P/E path —
        # that path would be negative for the same company, for the same reason.
        return {"factor": None, "reason": "negative_earnings"}

    if pe_ttm is not None and pe_fwd is not None:
        if _pos(pe_ttm) and _pos(pe_fwd):
            return _banded(float(pe_ttm) / float(pe_fwd), "pe_ratio")
        return {"factor": None, "reason": "negative_earnings"}

    return {"factor": None, "reason": "no_growth"}


def _banded(factor: float, source: str) -> dict[str, Any]:
    if not GROWTH_MIN <= factor <= GROWTH_MAX:
        return {"factor": None, "reason": "growth_out_of_band",
                "rejected": round(factor, 4), "source": source}
    return {"factor": round(factor, 6), "source": source}


def trailing(*, fcf_ttm: Optional[float],
             market_cap: Optional[float]) -> dict[str, Any]:
    """Measured P/FCF and FCF yield. No estimation anywhere in here.

    Both are in whatever unit the two inputs share — the caller's records hold
    billions for each, and the ratio cancels the unit.
    """
    if not _pos(market_cap):
        return {"pfcf": None, "fcf_yield": None, "reason": "no_market_cap"}
    if fcf_ttm is None:
        return {"pfcf": None, "fcf_yield": None, "reason": "no_fcf"}
    if float(fcf_ttm) <= 0:
        # Reported, not hidden: a cash-burning company is a fact worth showing.
        # But it is reported as a *yield*, which stays meaningful when negative,
        # and not as a multiple, which does not.
        return {"pfcf": None,
                "fcf_yield": round(float(fcf_ttm) / float(market_cap) * 100, 2),
                "reason": "negative_fcf"}
    return {
        "pfcf": round(float(market_cap) / float(fcf_ttm), 2),
        "fcf_yield": round(float(fcf_ttm) / float(market_cap) * 100, 2),
        "reason": None,
    }


def estimate(*,
             fcf_ttm: Optional[float],
             market_cap: Optional[float],
             eps_fy0: Optional[float] = None,
             eps_fy1: Optional[float] = None,
             pe_ttm: Optional[float] = None,
             pe_fwd: Optional[float] = None) -> dict[str, Any]:
    """Trailing cash-flow multiples, plus a forward estimate where one is honest.

    The trailing block is always attempted and is independent of the forward one:
    a company whose growth cannot be established still has a measured P/FCF, and
    dropping it because the *estimate* failed would discard a fact to protect a
    guess.

    ``forward_source`` names which growth signal was used, so the page can show a
    consensus-derived figure differently from a P/E-derived one. ``reason`` names
    the refusal when there is no forward figure; it is never both.
    """
    out: dict[str, Any] = dict(trailing(fcf_ttm=fcf_ttm, market_cap=market_cap))
    out["forward_fcf"] = None
    out["forward_pfcf"] = None
    out["forward_fcf_yield"] = None
    out["forward_source"] = None
    out["growth"] = None

    # No trailing multiple, no forward one to scale. `reason` already says why.
    if out.get("pfcf") is None:
        return out

    g = growth_factor(eps_fy0=eps_fy0, eps_fy1=eps_fy1,
                      pe_ttm=pe_ttm, pe_fwd=pe_fwd)
    if g.get("factor") is None:
        out["reason"] = g.get("reason")
        if g.get("rejected") is not None:
            out["rejected_growth"] = g["rejected"]
        return out

    factor = float(g["factor"])
    forward = float(fcf_ttm) * factor
    out.update({
        "forward_fcf": round(forward, 4),
        "forward_pfcf": round(float(market_cap) / forward, 2),
        "forward_fcf_yield": round(forward / float(market_cap) * 100, 2),
        "forward_source": g.get("source"),
        "growth": round(factor, 4),
        "reason": None,
    })
    return out
