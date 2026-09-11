"""
ystocker.dca
~~~~~~~~~~~~
The DCA Valuation Engine: per-ticker valuation percentiles folded into a single
0-100 ``V`` score, and from that a multiplier on a recurring contribution.

    E = sum(weight_f * P_f)          expensiveness, 0 = very cheap, 100 = very dear
    V = 100 - E                      the score the page headlines
    M_valuation = 0.5 + V / 100      so V=0 -> 0.50x, V=50 -> 1.00x, V=100 -> 1.50x
    DCA = Base * M_valuation * M_earnings * M_portfolio

Valuation sets the odds; the earnings overlay stops a collapsing forecast reading
as a bargain; the portfolio overlay stops a good score adding to a position that
is already too large. Nothing here decides *whether* to buy — it only moves the
size of a contribution that was going to happen anyway.

This module is **pure**. No network, no cache, no clock, no Flask. Every input
arrives as an argument and every resolver is injected, for the reason
:mod:`ystocker.lookthrough` is pure: the arithmetic is the part that has to be
right, and it is only cheaply testable if proving it needs no I/O.

Three decisions here are load-bearing, and each one is a way this could report a
confident number that is wrong.

**Direction lives on the factor, never in a formula.** The source framework
writes the software template as ``0.30 P_EVSales + ... + 0.25 (100 - P_FCFYield)``
— the inversion is inline, because a high FCF yield is *cheap* while a high
EV/Sales is *dear*. Copying that shape into code invites applying the flip twice
(once in the shared "which way does this metric point" table, once in the
template) and a double inversion is silent: the number stays in 0-100, still
looks like a percentile, and now says the opposite of what it means. So
:data:`DIRECTION` owns it, exactly once, and a template is nothing but weights.
:data:`TEMPLATES` therefore has no ``100 -`` anywhere in it, by construction.

**A dropped factor renormalises, but only so far.** The framework's adaptive rule
is right — a negative P/E is not "very cheap", it is *not a measurement*, and
filling it with a number would be an invention — so a distorted factor is removed
and the survivors are rescaled to sum to 1. Taken literally that rule has no
floor, and one surviving factor rescaled to 100% produces a V score that looks
exactly like a five-factor one. :data:`MIN_SURVIVING_WEIGHT` refuses instead:
below it :func:`expensiveness` returns ``None`` and the caller has to say so.

**A percentile needs a distribution, and a short one is not a small problem.**
``percentile_rank`` over eleven observations can only return eleven distinct
answers, all of them spaced 9 points apart, and it will happily return 100.0
for the highest of them — which reads as "dearer than it has ever been" and
means "dearest of the eleven days we have watched". :data:`MIN_OBSERVATIONS`
is the floor, and :func:`percentile_rank` returns ``None`` under it rather than
a number the page cannot distinguish from a real one.

See :mod:`ystocker.dca_history` for where the distributions come from and why
there are two of them that must never be mixed.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "DIRECTION", "TEMPLATES", "TICKER_MODELS", "SECTOR_MODELS", "FACTOR_LABELS",
    "MIN_OBSERVATIONS", "MIN_SURVIVING_WEIGHT", "MAX_TOTAL_MULTIPLIER",
    "percentile_rank", "pick_model", "expensiveness", "v_score",
    "valuation_multiplier", "score_band", "earnings_multiplier",
    "portfolio_multiplier", "combine", "evaluate",
]

# ---------------------------------------------------------------------------
# Which way does each metric point?
# ---------------------------------------------------------------------------

#: ``True`` when a *higher* raw value means a *more expensive* stock, so the raw
#: percentile is the expensiveness percentile. ``False`` for the yields, where a
#: higher value means cheaper and the percentile is flipped once, here.
#:
#: Every factor a template may name must appear in this table --
#: :func:`expensiveness` raises on one that does not, rather than guessing a
#: direction. A guess is a coin flip on the sign of the whole score.
DIRECTION: dict[str, bool] = {
    # Multiples: higher = dearer.
    "pe":                True,   # forward P/E (NTM / FY1 consensus)
    "pfcf":              True,   # forward price / free cash flow
    "ev_ebitda":         True,   # forward EV / EBITDA
    "peg":               True,   # P/E over expected EPS growth
    "ev_sales":          True,
    "ev_sales_growth":   True,   # (EV/Sales) / expected revenue growth %
    "ptbv":              True,   # price / tangible book value
    "ptbv_rotce":        True,   # (P/TBV) / ROTCE
    "pffo":              True,   # price / funds from operations
    "nav_premium":       True,   # premium to net asset value
    "cycle_adjusted":    True,   # price / normalized EPS, EV / mid-cycle EBITDA
    "mid_cycle":         True,   # same idea, cyclicals template
    "normalized_margin": True,   # price / earnings at a sustainable margin
    "peer":              True,   # premium to the peer group's multiple
    # Yields: higher = cheaper. The single place the flip happens.
    "fcf_yield":         False,
    "affo_yield":        False,
    "dividend_yield":    False,
}

#: Human labels, English only. The page translates via ``dca.f_*`` i18n keys and
#: uses these purely as the fallback when a key is missing, so that a factor
#: added here without a translation degrades to a readable name rather than to
#: a raw identifier.
FACTOR_LABELS: dict[str, str] = {
    "pe":                "Forward P/E",
    "pfcf":              "Forward P/FCF",
    "ev_ebitda":         "Forward EV/EBITDA",
    "peg":               "PEG",
    "ev_sales":          "EV/Sales",
    "ev_sales_growth":   "EV/Sales / Growth",
    "ptbv":              "P/TBV",
    "ptbv_rotce":        "P/TBV / ROTCE",
    "pffo":              "P/FFO",
    "nav_premium":       "NAV premium",
    "cycle_adjusted":    "Cycle-adjusted multiple",
    "mid_cycle":         "Mid-cycle multiple",
    "normalized_margin": "Normalized-margin multiple",
    "peer":              "Peer relative",
    "fcf_yield":         "FCF yield",
    "affo_yield":        "AFFO yield",
    "dividend_yield":    "Dividend yield",
}

# ---------------------------------------------------------------------------
# The templates
# ---------------------------------------------------------------------------

#: One weight map per business model. Weights are transcribed from the source
#: framework and each map sums to 1.0 -- asserted by the tests, because a
#: template that sums to 0.95 still produces a plausible-looking score, just a
#: systematically cheap one.
#:
#: There is deliberately no "one size fits all" default in here. A bank scored on
#: EV/EBITDA and a cyclical scored on spot P/E are the two textbook ways to be
#: confidently wrong about value, so :func:`pick_model` would rather return
#: ``compounder`` for a profitable large cap it recognises than invent a blend.
TEMPLATES: dict[str, dict[str, float]] = {
    # 2. Mature earners / mega-cap compounders -- MSFT, AAPL, META, GOOGL, ORCL.
    "compounder": {
        "pe": 0.35, "pfcf": 0.20, "ev_ebitda": 0.15, "peg": 0.15, "peer": 0.15,
    },
    # 3. Healthcare / consumer brand -- UNH, NKE. Normalized margin replaces PEG
    #    so a margin trough or peak is not read as a permanent state.
    "healthcare_consumer": {
        "pe": 0.30, "pfcf": 0.20, "ev_ebitda": 0.20,
        "normalized_margin": 0.15, "peer": 0.15,
    },
    # 4. Semiconductor / AI infrastructure -- NVDA, AVGO, TSM, QCOM. A falling
    #    forward P/E on peak-cycle EPS is the trap cycle_adjusted exists to catch.
    "semiconductor": {
        "pe": 0.30, "peg": 0.25, "pfcf": 0.15, "ev_ebitda": 0.15,
        "cycle_adjusted": 0.15,
    },
    # 5. High-growth software -- PLTR-style, where P/E is not the anchor.
    "high_growth_software": {
        "ev_sales": 0.30, "ev_sales_growth": 0.25, "fcf_yield": 0.25, "peer": 0.20,
    },
    # 6a. AMZN: GAAP P/E is distorted by retail margin, AWS mix and reinvestment.
    "amzn": {
        "ev_ebitda": 0.25, "pfcf": 0.25, "ev_sales": 0.20,
        "ev_sales_growth": 0.15, "peer": 0.15,
    },
    # 6b. TSLA: autos plus software plus storage plus optionality; no single
    #     multiple carries it, so growth-adjusted and cash flow are weighted up.
    "tsla": {
        "pe": 0.25, "ev_ebitda": 0.20, "pfcf": 0.20, "peg": 0.20, "peer": 0.15,
    },
    # 7. Banks -- JPM. EV/EBITDA is meaningless for a bank; tangible book and the
    #    return earned on it are the anchors.
    "bank": {
        "ptbv": 0.40, "pe": 0.25, "ptbv_rotce": 0.20, "peer": 0.15,
    },
    # 8. Cyclicals / energy / materials. Spot P/E is at its *lowest* at the
    #    earnings peak, so it is weighted down hard and mid-cycle weighted up.
    "cyclical": {
        "pe": 0.15, "ev_ebitda": 0.20, "mid_cycle": 0.25,
        "fcf_yield": 0.20, "peer": 0.20,
    },
    # 9a. REITs.
    "reit": {
        "pffo": 0.35, "nav_premium": 0.25, "affo_yield": 0.20, "peer": 0.20,
    },
    # 9b. Utilities.
    "utility": {
        "pe": 0.35, "ev_ebitda": 0.25, "dividend_yield": 0.20, "peer": 0.20,
    },
}

#: Explicit per-ticker assignment, from the framework's own mapping table. These
#: beat the sector fallback because the whole point of the AMZN and TSLA rows is
#: that their sector label ("Consumer Cyclical" for both, on Yahoo) picks a
#: template that misreads them.
TICKER_MODELS: dict[str, str] = {
    "MSFT": "compounder",
    "AAPL": "compounder",
    "META": "compounder",
    "GOOG": "compounder",
    "GOOGL": "compounder",
    "ORCL": "compounder",
    "NVDA": "semiconductor",
    "AVGO": "semiconductor",
    "TSM":  "semiconductor",
    "QCOM": "semiconductor",
    "AMZN": "amzn",
    "PLTR": "high_growth_software",
    "TSLA": "tsla",
    "JPM":  "bank",
    "UNH":  "healthcare_consumer",
    "NKE":  "healthcare_consumer",
}

#: Fallback by Yahoo ``sector``. Keys are lowercased and stripped of spaces so
#: that "Financial Services", "financial services" and "FinancialServices" all
#: land on the same row -- Yahoo is not consistent about this between the
#: ``info`` blob and the sector-weightings block, which
#: ``assets._SECTOR_ALIASES`` already had to reconcile once.
SECTOR_MODELS: dict[str, str] = {
    "technology":            "compounder",
    "communicationservices": "compounder",
    "consumerdefensive":     "healthcare_consumer",
    "healthcare":            "healthcare_consumer",
    "consumercyclical":      "healthcare_consumer",
    "financialservices":     "bank",
    "financial":             "bank",
    "energy":                "cyclical",
    "basicmaterials":        "cyclical",
    "industrials":           "cyclical",
    "realestate":            "reit",
    "utilities":             "utility",
}

#: Sub-industry overrides applied before :data:`SECTOR_MODELS`, matched as a
#: lowercase substring of Yahoo's ``industry``. Semiconductors sit under
#: "Technology", which would otherwise score a foundry at the top of its cycle
#: on the compounder template and miss the cycle adjustment entirely.
INDUSTRY_MODELS: tuple[tuple[str, str], ...] = (
    ("semiconductor", "semiconductor"),
    ("software",      "compounder"),
    ("reit",          "reit"),
    ("bank",          "bank"),
    ("capital markets", "bank"),
    ("insurance",     "bank"),
    ("oil & gas",     "cyclical"),
    ("mining",        "cyclical"),
    ("steel",         "cyclical"),
    ("utilities",     "utility"),
)

# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

#: Fewest observations a distribution may hold before a percentile is reported.
#: Two years of month-ends, or roughly a year of trading days sampled weekly.
#: Under it :func:`percentile_rank` returns ``None``: the arithmetic still works
#: on four points, and that is the danger -- it returns 0, 33.3, 66.7 or 100.0,
#: every one of which renders identically to a percentile computed from a decade.
MIN_OBSERVATIONS: int = 60

#: Fraction of a template's original weight that must survive the adaptive drop
#: for the remainder to be worth renormalising. At 0.5 a compounder that has lost
#: P/E (0.35) still scores; one that has also lost P/FCF (0.55 gone) does not.
MIN_SURVIVING_WEIGHT: float = 0.5

#: Ceiling on the *product* of every multiplier. The valuation multiplier alone
#: tops out at 1.50x, but a very cheap score times an upgrade cycle (1.10x) is
#: 1.65x, and the framework's stated constraint is on the final number, not on
#: the valuation term. Without this the cap silently does not exist in exactly
#: the case it was written for.
MAX_TOTAL_MULTIPLIER: float = 1.5

#: Score bands, as ``(exclusive upper bound, key)``. The key is an i18n lookup,
#: not a phrase, so the page can render it in either language.
_BANDS: tuple[tuple[float, str], ...] = (
    (20.0, "very_expensive"),
    (40.0, "expensive"),
    (60.0, "fair"),
    (80.0, "cheap"),
    (100.1, "very_cheap"),
)

#: Earnings-revision multipliers, as ``(exclusive upper bound on the drift, key,
#: multiplier)``. Drift is the fractional change in the forward EPS consensus
#: over the lookback -- ``-0.03`` is a 3% cut. The bands are the framework's;
#: the thresholds that map a continuous drift onto them are ours, and they are
#: deliberately wide, because consensus EPS moves by rounding alone.
_EARNINGS_BANDS: tuple[tuple[float, str, float], ...] = (
    (-0.05, "cut_hard",  0.70),
    (-0.01, "cut_mild",  0.90),
    (0.01,  "stable",    1.00),
    (float("inf"), "raised", 1.08),
)

#: Portfolio multipliers, as ``(exclusive upper bound on the position's weight in
#: percent, key, multiplier)``. Weights come from the look-through, so an ETF
#: sleeve counts toward the name -- which is the point, since the concentration
#: this guards against is mostly reached through funds rather than through the
#: line itself.
_PORTFOLIO_BANDS: tuple[tuple[float, str, float], ...] = (
    (4.0,  "light",       1.00),
    (7.0,  "moderate",    0.85),
    (10.0, "heavy",       0.60),
    (float("inf"), "at_limit", 0.25),
)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

def percentile_rank(value: Optional[float],
                    distribution: Sequence[float],
                    *, minimum: int = MIN_OBSERVATIONS) -> Optional[float]:
    """Where *value* sits in *distribution*, as 0-100, or ``None``.

    The "weak" definition -- the share of observations at or below *value* -- so
    a reading equal to every past one scores 100 rather than 50. That matters at
    the top of the range, where the question being asked is "has it ever been
    this dear", and the answer wanted is yes-or-no rather than an average of the
    ties.

    ``None`` comes back for a missing value, a distribution shorter than
    *minimum*, or one that is entirely flat. The flat case is the subtle one: a
    constant series makes every percentile 100.0, which is arithmetically true
    and, read as "dearer than it has ever been", false.
    """
    if value is None or not _finite(value):
        return None
    clean = [float(x) for x in distribution if x is not None and _finite(x)]
    if len(clean) < max(2, int(minimum)):
        return None
    lo, hi = min(clean), max(clean)
    if not hi > lo:
        return None
    at_or_below = sum(1 for x in clean if x <= value)
    return round(100.0 * at_or_below / len(clean), 2)


def _finite(value: Any) -> bool:
    """True for a real, finite number. ``bool`` is rejected on purpose."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def cross_sectional_rank(value: Optional[float],
                         peers: Iterable[Optional[float]]) -> Optional[float]:
    """The peer-relative percentile: where *value* sits among its peer group today.

    Separate from :func:`percentile_rank` because it takes no minimum-observation
    floor -- a peer group is a dozen names by construction and will never reach
    :data:`MIN_OBSERVATIONS`, so applying that guard here would delete the peer
    factor from every template that uses it. The floor exists to stop a *time*
    series being read as a long history; a cross-section of twelve names is not
    claiming to be one.

    It does need at least four peers, though. Below that the answer is mostly
    determined by which handful of names happen to be in the group.
    """
    if value is None or not _finite(value):
        return None
    clean = [float(x) for x in peers if x is not None and _finite(x) and x > 0]
    if len(clean) < 4:
        return None
    lo, hi = min(clean), max(clean)
    if not hi > lo:
        return None
    at_or_below = sum(1 for x in clean if x <= value)
    return round(100.0 * at_or_below / len(clean), 2)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

def _squash(text: Optional[str]) -> str:
    return "".join((text or "").lower().split())


def pick_model(ticker: str,
               sector: Optional[str] = None,
               industry: Optional[str] = None) -> tuple[str, str]:
    """Choose a template for *ticker*. Returns ``(template_key, why)``.

    Precedence is ticker, then industry, then sector, then ``compounder``. The
    *why* comes back with it because the page states which model produced the
    score: two readers comparing NVDA and MSFT are looking at different weight
    maps, and a score is not comparable across templates without saying so.
    """
    key = (ticker or "").strip().upper()
    if key in TICKER_MODELS:
        return TICKER_MODELS[key], "ticker"

    industry_l = (industry or "").lower()
    for needle, model in INDUSTRY_MODELS:
        if needle in industry_l:
            return model, "industry"

    by_sector = SECTOR_MODELS.get(_squash(sector))
    if by_sector:
        return by_sector, "sector"

    return "compounder", "default"


# ---------------------------------------------------------------------------
# E, V and the multipliers
# ---------------------------------------------------------------------------

def expensiveness(percentiles: Mapping[str, Optional[float]],
                  weights: Mapping[str, float]) -> dict[str, Any]:
    """Fold per-factor percentiles into one expensiveness score.

    *percentiles* are **raw** percentiles of the metric itself -- the caller does
    not pre-invert the yields, :data:`DIRECTION` does that here. A factor whose
    value is ``None`` is dropped and the survivors are rescaled to sum to 1,
    which is the framework's adaptive rule and the right one: a negative P/E is
    an absence of a measurement, not a cheap one, and substituting anything for
    it invents the answer at the moment it matters most.

    Returns a dict carrying ``E``, the surviving ``weight`` mass before
    rescaling, and a ``factors`` list holding each factor's raw percentile, the
    oriented percentile that actually entered the sum, its rescaled weight and
    its contribution -- everything the page needs to show the equation with real
    numbers in it rather than a bare total.

    ``E`` is ``None`` when too little weight survived; the other fields still
    come back so the page can say which factors were missing.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    surviving = 0.0
    total = 0.0

    for factor, weight in weights.items():
        if factor not in DIRECTION:
            raise KeyError(f"dca: factor {factor!r} has no DIRECTION entry")
        total += float(weight)
        raw = percentiles.get(factor)
        if raw is None or not _finite(raw):
            dropped.append(factor)
            continue
        raw = max(0.0, min(100.0, float(raw)))
        # The one and only inversion. A high FCF yield is cheap, so its
        # expensiveness percentile is the complement of its own.
        oriented = raw if DIRECTION[factor] else 100.0 - raw
        surviving += float(weight)
        kept.append({"factor": factor, "raw_pct": round(raw, 2),
                     "oriented_pct": round(oriented, 2),
                     "base_weight": round(float(weight), 4)})

    share = (surviving / total) if total > 0 else 0.0
    enough = bool(kept) and share >= MIN_SURVIVING_WEIGHT

    e_score: Optional[float] = None
    if enough:
        e_score = 0.0
        for row in kept:
            scaled = row["base_weight"] / surviving
            row["weight"] = round(scaled, 4)
            row["contribution"] = round(scaled * row["oriented_pct"], 3)
            e_score += scaled * row["oriented_pct"]
        e_score = round(min(100.0, max(0.0, e_score)), 2)
    else:
        for row in kept:
            row["weight"] = None
            row["contribution"] = None

    return {
        "E": e_score,
        "factors": kept,
        "dropped": dropped,
        "surviving_weight": round(share, 4),
        "renormalised": bool(kept) and surviving < total - 1e-9,
    }


def v_score(e_score: Optional[float]) -> Optional[float]:
    """``V = 100 - E``. Higher is cheaper."""
    if e_score is None or not _finite(e_score):
        return None
    return round(100.0 - float(e_score), 2)


def valuation_multiplier(v: Optional[float]) -> Optional[float]:
    """``M_valuation = 0.5 + V / 100``, so 0.50x at V=0 and 1.50x at V=100."""
    if v is None or not _finite(v):
        return None
    return round(0.5 + max(0.0, min(100.0, float(v))) / 100.0, 4)


def score_band(v: Optional[float]) -> Optional[str]:
    """The band key for *v* -- ``very_expensive`` .. ``very_cheap``."""
    if v is None or not _finite(v):
        return None
    v = max(0.0, min(100.0, float(v)))
    for upper, key in _BANDS:
        if v < upper:
            return key
    return _BANDS[-1][1]


def earnings_multiplier(drift: Optional[float]) -> tuple[float, str]:
    """``M_earnings`` from the fractional drift in the forward EPS consensus.

    Returns ``(multiplier, band_key)``. An unknown drift is ``(1.0, "unknown")``
    and never a penalty: a missing revision feed is a gap in *our* data, and
    docking the contribution for it would silently make the engine's answer
    depend on vendor uptime.
    """
    if drift is None or not _finite(drift):
        return 1.0, "unknown"
    for upper, key, mult in _EARNINGS_BANDS:
        if float(drift) < upper:
            return mult, key
    return 1.0, "stable"


def portfolio_multiplier(weight_pct: Optional[float]) -> tuple[float, str]:
    """``M_portfolio`` from the name's look-through weight in the portfolio.

    Returns ``(multiplier, band_key)``. Unknown is ``(1.0, "unknown")`` — the
    reader is signed out, or holds nothing, and neither is evidence of
    concentration.
    """
    if weight_pct is None or not _finite(weight_pct):
        return 1.0, "unknown"
    for upper, key, mult in _PORTFOLIO_BANDS:
        if float(weight_pct) < upper:
            return mult, key
    return 0.25, "at_limit"


def combine(base: float,
            m_valuation: Optional[float],
            m_earnings: float = 1.0,
            m_portfolio: float = 1.0) -> dict[str, Any]:
    """The final contribution, and whether the 1.5x ceiling bit.

    ``capped`` is reported rather than folded away because a reader who set out
    to add 1.65x and is being handed 1.50x should be told which constraint did
    it, not shown a number that looks like the formula's own output.
    """
    if m_valuation is None or not _finite(m_valuation) or not _finite(base):
        return {"amount": None, "multiplier": None, "capped": False,
                "uncapped_multiplier": None}
    raw = float(m_valuation) * float(m_earnings) * float(m_portfolio)
    capped = raw > MAX_TOTAL_MULTIPLIER
    effective = MAX_TOTAL_MULTIPLIER if capped else raw
    return {
        "amount": round(float(base) * effective, 2),
        "multiplier": round(effective, 4),
        "uncapped_multiplier": round(raw, 4),
        "capped": capped,
    }


def evaluate(*, ticker: str,
             percentiles: Mapping[str, Optional[float]],
             model: Optional[str] = None,
             sector: Optional[str] = None,
             industry: Optional[str] = None,
             base_dca: float = 1000.0,
             eps_drift: Optional[float] = None,
             position_pct: Optional[float] = None) -> dict[str, Any]:
    """One ticker, end to end: percentiles in, a sized contribution out.

    The whole chain in one call so the page and the tests exercise the same path.
    *model* forces a template; otherwise :func:`pick_model` chooses and reports
    why. Everything needed to render the equation with numbers substituted comes
    back in the result.
    """
    if model and model in TEMPLATES:
        model_key, reason = model, "explicit"
    else:
        model_key, reason = pick_model(ticker, sector, industry)

    weights = TEMPLATES[model_key]
    breakdown = expensiveness(percentiles, weights)
    v = v_score(breakdown["E"])
    m_val = valuation_multiplier(v)
    m_earn, earn_band = earnings_multiplier(eps_drift)
    m_port, port_band = portfolio_multiplier(position_pct)
    sized = combine(base_dca, m_val, m_earn, m_port)

    return {
        "ticker": (ticker or "").strip().upper(),
        "model": model_key,
        "model_reason": reason,
        "E": breakdown["E"],
        "V": v,
        "band": score_band(v),
        "factors": breakdown["factors"],
        "dropped": breakdown["dropped"],
        "surviving_weight": breakdown["surviving_weight"],
        "renormalised": breakdown["renormalised"],
        "m_valuation": m_val,
        "m_earnings": m_earn,
        "earnings_band": earn_band,
        "eps_drift": eps_drift,
        "m_portfolio": m_port,
        "portfolio_band": port_band,
        "position_pct": position_pct,
        "base_dca": round(float(base_dca), 2) if _finite(base_dca) else None,
        **sized,
    }
