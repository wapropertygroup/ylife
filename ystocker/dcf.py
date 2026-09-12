"""
ystocker.dcf
~~~~~~~~~~~~
The absolute valuation anchor: a discounted-cash-flow fair value, mapped onto the
same 0-100 scale :mod:`ystocker.dca` already scores relative valuation on.

    U_DCF = FV_DCF / P - 1                     upside against today's price
    V_DCF = piecewise_linear(U_DCF)            -40% -> 0, 0% -> 50, +40% -> 100
    V_DCF,raw = 0.25 V_Bear + 0.50 V_Base + 0.25 V_Bull
    V_DCF = 50 + c (V_DCF,raw - 50)            c in [0.5, 1.0]

and then, in :mod:`ystocker.dca`::

    V = w_DCF V_DCF + (1 - w_DCF) V_REL

DCF does **not** replace relative valuation and is not averaged into it as
another percentile. The two answer different questions — "what are these cash
flows worth" versus "what has the market paid for them before" — and the
framework's whole point is that they are combined only after each has been
reduced to a cheapness score on its own terms.

This module is **pure**. No network, no cache, no clock, no Flask. Every input
arrives as an argument, for the reason :mod:`ystocker.lookthrough` and
:mod:`ystocker.dca` are pure: a DCF is almost entirely assumption, so the
arithmetic on top of those assumptions is the one part that can actually be
proven, and it is only cheaply provable if proving it needs no I/O.

Six decisions here are load-bearing. Every one of them is a way this could hand
back a confident fair value that is wrong.

**A refusal is not a score of 50.** The framework says so in as many words —
*"若 DCF 被省略，则 V = V_REL；不要把缺失的 V_DCF 填成 50"* — and it is the single
easiest mistake to make, because 50 is neutral, renders identically to a measured
50, and quietly drags every score toward the middle by ``w_DCF``. A stock at
V_REL=80 with a 30% DCF weight and a filled-in 50 reports 71, which looks like a
real number and is not. So every failure path here returns ``V=None`` with a
``reason``, and :func:`ystocker.dca.blend_v` renormalises the weight onto V_REL
instead of substituting anything.

**WACC <= g is not a low-confidence DCF, it is not a DCF.** The terminal value
``FCFF/(WACC - g)`` goes negative or explodes, and the resulting per-share number
is not large-but-uncertain, it is meaningless. A clamp would hide it behind a
plausible figure. :data:`MIN_WACC_SPREAD` refuses.

**The sensitivity test is run, not reasoned about.** The framework's rule is
*"结果对 0.5% 变化剧烈 → 模型无效"*. That is measurable, so it is measured:
:func:`growth_sensitivity` re-values the company at ``g + 0.5pp`` and compares.
Asserting a proxy for this ("the terminal share looks high") would be a guess
about the thing there is no reason to guess about.

**The price and the fair value must share a date.** ``U_DCF`` is a ratio of two
numbers, and if the fair value is from a statement vintage and the price is from
right now, that is fine; if the *fair value* is months old and the price is
current, §13 says refuse — a DCF reads as cheap simply because the stock fell.
:data:`MAX_VALUATION_AGE_DAYS` bounds it, and callers pass ``price_date`` so the
page can show which close the upside was measured against. The derived path uses
the **same last weekly close** the reconstruction ranks on, so V_DCF and V_REL
describe one price rather than two.

**Growth is clamped, and the clamp is the mid-cycle discipline.** A company whose
free cash flow tripled last year has an observed CAGR that, extrapolated ten
years, values it above the world. §13 calls this out directly — *"周期峰值利润/
商品价格被永久外推"* — so :data:`GROWTH_CEILING` bounds the extrapolation and a
bound that actually bit is reported in ``notes`` rather than silently applied.

**Scenarios come from the company's own dispersion, not from a house ±20%.** Bear
and Bull are Base plus and minus the standard deviation of that company's own
year-over-year free-cash-flow growth. A steady compounder therefore gets a narrow
band and a semiconductor a wide one, which is the difference the confidence
discount exists to express — and it is measured from the same vintages the
relative percentiles are built on rather than asserted.

Two things this deliberately will not do
----------------------------------------
It does not value a **bank** on FCFF. §10 is explicit that debt is raw material
for a bank rather than financing, so enterprise value and free cash flow are not
defined the usual way; :data:`FORM_EQUITY_ONLY` marks those forms and
:func:`from_fundamentals` refuses rather than producing a number that looks like
every other ticker's. The same applies to a REIT's AFFO DCF, which needs
maintenance capex separated from development capex — a split no Yahoo cash-flow
statement exposes, and §12 warns about by name.

It does not model **optionality**. §9's TSLA note is the general rule: a business
line nobody can forecast does not become forecastable by being given a terminal
value. Whatever is not in the projected cash flows is not in the fair value, and
the page says so rather than implying the number is complete.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "UPSIDE_ANCHORS", "SCENARIO_WEIGHTS",
    "CONFIDENCE_FLOOR", "CONFIDENCE_CEILING", "BASE_ONLY_CONFIDENCE_CEILING",
    "MIN_WACC_SPREAD", "MAX_TERMINAL_SHARE", "WARN_TERMINAL_SHARE",
    "SENSITIVITY_PROBE", "MAX_SENSITIVITY", "MAX_VALUATION_AGE_DAYS",
    "EXPLICIT_YEARS", "LONG_EXPLICIT_YEARS", "GROWTH_FLOOR", "GROWTH_CEILING",
    "TERMINAL_GROWTH", "DEFAULT_RISK_FREE", "DEFAULT_ERP", "DEFAULT_TAX_RATE",
    "FORM_FCFF", "FORM_EQUITY_ONLY", "REFUSALS",
    "v_from_upside", "combine_scenarios", "apply_confidence",
    "wacc", "project_flows", "present_value", "terminal_value",
    "fair_value", "growth_sensitivity", "score", "from_fundamentals",
]


# ---------------------------------------------------------------------------
# §3 — the upside -> score map
# ---------------------------------------------------------------------------

#: ``(upside, V_DCF)`` breakpoints, transcribed from §3. Linear between them and
#: **clamped outside**, which the framework states for the top end in as many
#: words: *"≥ +40% → 100 极端低估；不再继续放大"*.
#:
#: The clamp is the interesting half. An unclamped linear extension would hand a
#: stock trading at a tenth of its DCF a score of 400, which after
#: ``M_valuation = 0.5 + V/100`` is a 4.5x contribution — and a DCF that says a
#: liquid large cap is worth ten times its price is overwhelmingly a broken model,
#: not an opportunity. Saturating at 100 says "as cheap as this scale goes", which
#: is the honest reading of an extreme.
UPSIDE_ANCHORS: tuple[tuple[float, float], ...] = (
    (-0.40, 0.0),
    (-0.25, 15.0),
    (-0.10, 35.0),
    (0.00, 50.0),
    (0.10, 65.0),
    (0.25, 85.0),
    (0.40, 100.0),
)

#: §3's scenario weighting. Bear and Bull are deliberately not symmetric with
#: Base: the middle case carries half the weight on its own, so a wide band moves
#: the score much less than replacing the base case would.
SCENARIO_WEIGHTS: dict[str, float] = {"bear": 0.25, "base": 0.50, "bull": 0.25}

#: Bounds on the confidence shrink ``c``. §3 recommends 0.5-1.0.
CONFIDENCE_FLOOR: float = 0.5
CONFIDENCE_CEILING: float = 1.0

#: §3: *"若只提供 Base Case，也可直接映射，但 c 不应高于 0.75"*. One scenario is not
#: a range, and scoring it as though it were is the cheapest possible way to
#: overstate how much is known.
BASE_ONLY_CONFIDENCE_CEILING: float = 0.75


# ---------------------------------------------------------------------------
# Guard rails on the model itself
# ---------------------------------------------------------------------------

#: Least ``WACC - g`` may be before the terminal value stops meaning anything.
#: At 1.5pp the terminal multiple is already ~67x the final flow, which is
#: generous; below it the denominator is mostly rounding error. §13 lists
#: ``WACC <= g`` as *"模型无效"* rather than as a low-confidence case, so this
#: refuses instead of shrinking ``c``.
MIN_WACC_SPREAD: float = 0.015

#: Terminal share of enterprise value above which §13 says to cut ``c`` and
#: ``w_DCF``, and above which there is nothing left to cut.
WARN_TERMINAL_SHARE: float = 0.80
MAX_TERMINAL_SHARE: float = 0.90

#: §13's *"结果对 0.5% 变化剧烈"*, made into an actual measurement: re-value at
#: ``g + 0.5pp`` and refuse if the fair value moves more than a quarter. A
#: quarter is chosen because it is roughly the width of one whole band on the
#: upside map — a model whose answer crosses a band on a half-point assumption
#: change is not discriminating between companies, it is reporting its own
#: assumptions back.
SENSITIVITY_PROBE: float = 0.005
MAX_SENSITIVITY: float = 0.25

#: How old a fair value may be before it is refused rather than scored against a
#: current price (§13, *"DCF 估值日期过旧"*). A quarter plus slack: the inputs are
#: statements, which move quarterly, so anything inside one reporting cycle is
#: still describing the company that exists.
MAX_VALUATION_AGE_DAYS: int = 120

#: Explicit forecast period. Ten years is the conventional length and is long
#: enough that the fade below actually reaches the terminal rate.
EXPLICIT_YEARS: int = 10

#: §8: *"显性预测期宜更长"* for high growth. Used when the measured initial growth
#: exceeds :data:`LONG_EXPLICIT_THRESHOLD`, which keys the decision off what the
#: company's cash flows actually did rather than off a template label — a
#: mislabelled compounder growing at 30% still gets the longer fade.
LONG_EXPLICIT_YEARS: int = 15
LONG_EXPLICIT_THRESHOLD: float = 0.15

#: Bounds on the extrapolated near-term growth rate. The ceiling is the
#: mid-cycle discipline of §13 in numeric form; the floor stops one bad year
#: projecting a company into liquidation.
GROWTH_FLOOR: float = -0.05
GROWTH_CEILING: float = 0.20

#: Terminal growth. Below long-run nominal GDP on purpose: a firm growing at
#: nominal GDP for ever eventually *is* the economy, and the terminal value is
#: where that assumption does its damage invisibly.
TERMINAL_GROWTH: float = 0.025

#: Bounds on the Bear/Bull spread around base growth. The floor stops a company
#: with three near-identical years reporting a scenario band of nothing, which
#: would make ``V_DCF,raw`` equal to ``V_Base`` and the 25/50/25 weighting
#: decorative.
SPREAD_FLOOR: float = 0.03
SPREAD_CEILING: float = 0.15

#: WACC inputs with no per-company source. Stated as constants rather than
#: derived, because a fabricated derivation reads as a measurement — these are
#: assumptions and the page reports them as such.
DEFAULT_RISK_FREE: float = 0.042
DEFAULT_ERP: float = 0.045
DEFAULT_TAX_RATE: float = 0.21
DEFAULT_CREDIT_SPREAD: float = 0.015

#: Beta is noisy and Yahoo occasionally publishes absurd values for thin names.
#: Clamped rather than trusted, and a clamp that bit is reported.
BETA_FLOOR, BETA_CEILING = 0.5, 2.0

#: A discount rate outside this band is almost always a bad beta or a bad capital
#: structure rather than a genuinely extreme cost of capital.
WACC_FLOOR, WACC_CEILING = 0.06, 0.15

#: Fewest annual free-cash-flow observations before a growth rate and a
#: dispersion are worth estimating. Two points give a CAGR with no dispersion at
#: all and three give one computed from two differences.
MIN_FCF_OBSERVATIONS: int = 4

#: Valuation forms. ``FORM_FCFF`` is the ordinary enterprise DCF this module
#: implements. The others are named so a caller can be refused *by name* rather
#: than by producing a number on the wrong basis — see §10 and §12.
FORM_FCFF = "fcff"
FORM_EQUITY_ONLY = ("ddm", "fcfe", "excess_return", "affo", "nav")

#: Every reason this module declines to produce a score. Exported so the page and
#: the tests name the same set, and so a new refusal cannot be added without a
#: translation existing for it.
REFUSALS: tuple[str, ...] = (
    "no_price",
    "no_fair_value",
    "no_shares",
    "no_fcf",
    "negative_fcf",
    "too_few_observations",
    "wacc_not_above_g",
    "terminal_dominates",
    "g_sensitivity",
    "form_not_applicable",
    "valuation_stale",
)


def _finite(value: Any) -> bool:
    """True for a real, finite number. ``bool`` is rejected on purpose."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _pos(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    """A declined DCF.

    ``V`` is ``None`` and never 50 — see the module docstring. ``w_dcf_hint`` of
    zero travels with it so a caller that forgets to check ``V`` still cannot
    weight a missing branch.
    """
    out: dict[str, Any] = {"V": None, "refused": True, "reason": reason,
                           "w_dcf_hint": 0.0}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# §3 — mapping an upside onto 0-100
# ---------------------------------------------------------------------------

def v_from_upside(upside: Optional[float]) -> Optional[float]:
    """Map a fractional upside onto :data:`UPSIDE_ANCHORS`, clamped to 0-100.

    *upside* is ``FV / P - 1``: ``0.25`` is "the DCF says this is worth 25% more
    than it costs". Linear between anchors, flat outside the outermost pair.

    The framework's own check: ``+20%`` sits between ``+10%`` (65) and ``+25%``
    (85), two thirds of the way along, and returns 78.33.
    """
    if not _finite(upside):
        return None
    u = float(upside)
    lo_u, lo_v = UPSIDE_ANCHORS[0]
    hi_u, hi_v = UPSIDE_ANCHORS[-1]
    if u <= lo_u:
        return lo_v
    if u >= hi_u:
        return hi_v
    for (u0, v0), (u1, v1) in zip(UPSIDE_ANCHORS, UPSIDE_ANCHORS[1:]):
        if u0 <= u <= u1:
            if u1 == u0:  # pragma: no cover - anchors are strictly increasing
                return v1
            return round(v0 + (u - u0) / (u1 - u0) * (v1 - v0), 4)
    return None  # pragma: no cover - the loop covers the whole range


def combine_scenarios(bear: Optional[float], base: Optional[float],
                      bull: Optional[float]) -> Optional[float]:
    """``V_DCF,raw`` from three scenario scores, weighted 25/50/25 (§3).

    All three are required. A missing wing is **not** filled with the base case:
    that produces ``0.25 base + 0.50 base + 0.25 bull``, which is a narrower band
    than was actually modelled and therefore overstates confidence in exactly the
    situation where less is known. A caller with only a base case passes it to
    :func:`score` alone and takes the :data:`BASE_ONLY_CONFIDENCE_CEILING`
    instead, which is the framework's own answer for that case.
    """
    if not (_finite(bear) and _finite(base) and _finite(bull)):
        return None
    return round(SCENARIO_WEIGHTS["bear"] * float(bear)
                 + SCENARIO_WEIGHTS["base"] * float(base)
                 + SCENARIO_WEIGHTS["bull"] * float(bull), 4)


def apply_confidence(v_raw: Optional[float], c: float) -> Optional[float]:
    """Shrink *v_raw* toward the neutral 50 by confidence *c* (§3).

    ``V_DCF = 50 + c (V_DCF,raw - 50)``. At ``c=1`` the DCF speaks at full
    volume; at ``c=0.5`` it is halved toward neutral. This is the only lever that
    expresses "the model ran and produced a number we do not fully believe" —
    distinct from a refusal, which says the number should not exist at all.
    """
    if not _finite(v_raw):
        return None
    c = _clamp(float(c), CONFIDENCE_FLOOR, CONFIDENCE_CEILING)
    return round(50.0 + c * (float(v_raw) - 50.0), 4)


# ---------------------------------------------------------------------------
# §2 — the discounted cash flow
# ---------------------------------------------------------------------------

def wacc(*, beta: Optional[float] = None,
         risk_free: float = DEFAULT_RISK_FREE,
         erp: float = DEFAULT_ERP,
         cost_of_debt: Optional[float] = None,
         tax_rate: float = DEFAULT_TAX_RATE,
         equity_value: Optional[float] = None,
         debt: Optional[float] = None) -> dict[str, Any]:
    """Weighted average cost of capital, with every input reported back.

    ``k_e = rf + beta x ERP`` and ``k_d,after-tax = k_d (1 - t)``, weighted by
    market equity and book debt. Book debt for the weight is the usual
    compromise: a market value of debt needs a bond curve per issuer, and the
    error from using book is small next to the error in beta.

    §2 requires the WACC to be *reported*, which is why this returns a dict
    rather than a float — a discount rate is the single assumption that moves a
    DCF most, and one the reader cannot see is one they cannot disagree with.

    Every clamp that bound is named in ``notes``. A clamped beta silently
    applied would make a wildly mispriced input look like a deliberate choice.
    """
    notes: list[str] = []

    raw_beta = float(beta) if _finite(beta) else 1.0
    if not _finite(beta):
        notes.append("beta_assumed")
    use_beta = _clamp(raw_beta, BETA_FLOOR, BETA_CEILING)
    if abs(use_beta - raw_beta) > 1e-9:
        notes.append("beta_clamped")

    rf = float(risk_free) if _finite(risk_free) else DEFAULT_RISK_FREE
    premium = float(erp) if _finite(erp) else DEFAULT_ERP
    cost_equity = rf + use_beta * premium

    k_d = float(cost_of_debt) if _pos(cost_of_debt) else rf + DEFAULT_CREDIT_SPREAD
    if not _pos(cost_of_debt):
        notes.append("cost_of_debt_assumed")
    tax = _clamp(float(tax_rate), 0.0, 0.5) if _finite(tax_rate) else DEFAULT_TAX_RATE
    k_d_after_tax = k_d * (1.0 - tax)

    eq = float(equity_value) if _pos(equity_value) else 0.0
    dt = float(debt) if (_finite(debt) and float(debt) > 0) else 0.0
    total = eq + dt
    if total <= 0:
        # No capital structure to weight with. Cost of equity is the honest
        # answer, not a 50/50 split of two numbers we do not have.
        w_equity = 1.0
        notes.append("structure_assumed")
    else:
        w_equity = eq / total

    raw = w_equity * cost_equity + (1.0 - w_equity) * k_d_after_tax
    value = _clamp(raw, WACC_FLOOR, WACC_CEILING)
    if abs(value - raw) > 1e-9:
        notes.append("wacc_clamped")

    return {
        "wacc": round(value, 6),
        "uncapped": round(raw, 6),
        "cost_equity": round(cost_equity, 6),
        "cost_debt_after_tax": round(k_d_after_tax, 6),
        "beta": round(use_beta, 4),
        "risk_free": round(rf, 6),
        "erp": round(premium, 6),
        "tax_rate": round(tax, 4),
        "weight_equity": round(w_equity, 4),
        "notes": notes,
    }


def project_flows(fcf0: float, initial_growth: float,
                  terminal_growth: float, years: int) -> list[float]:
    """Free cash flow for years 1..*years*, fading linearly to the terminal rate.

    A single constant growth rate to a cliff-edge terminal value is the other
    common shape and is worse: it holds a 20% grower at 20% for a decade and then
    drops it to 2.5% in one step, which puts an implausible discontinuity right
    where most of the value is. Fading spreads the deceleration over the explicit
    period, so the terminal value is taken on a flow that has already slowed to
    the rate it is about to be capitalised at.
    """
    years = max(1, int(years))
    flows: list[float] = []
    level = float(fcf0)
    for year in range(1, years + 1):
        # Year 1 grows at the initial rate; year `years` at the terminal rate.
        fraction = (year - 1) / (years - 1) if years > 1 else 1.0
        rate = initial_growth + (terminal_growth - initial_growth) * fraction
        level *= (1.0 + rate)
        flows.append(level)
    return flows


def present_value(flows: Sequence[float], discount_rate: float) -> float:
    """Discount *flows* (year 1 first) at *discount_rate*."""
    return sum(float(f) / (1.0 + discount_rate) ** (i + 1)
               for i, f in enumerate(flows))


def terminal_value(final_flow: float, discount_rate: float,
                   growth: float) -> Optional[float]:
    """Gordon terminal value ``FCFF_(n+1) / (WACC - g)``, or ``None``.

    ``None`` when the spread is under :data:`MIN_WACC_SPREAD`. Returning a very
    large number instead would be arithmetically faithful and practically a lie:
    the formula's output near the singularity is dominated by the gap between two
    assumptions, not by anything about the company.
    """
    spread = float(discount_rate) - float(growth)
    if spread < MIN_WACC_SPREAD:
        return None
    return float(final_flow) * (1.0 + float(growth)) / spread


def fair_value(*, fcf0: float, initial_growth: float, discount_rate: float,
               terminal_growth: float = TERMINAL_GROWTH,
               years: int = EXPLICIT_YEARS,
               cash: float = 0.0, debt: float = 0.0,
               shares: Optional[float] = None) -> Optional[dict[str, Any]]:
    """One scenario's enterprise value, equity bridge and per-share fair value.

    ``Equity = EV + cash - debt`` (§2's bridge, less the non-operating assets and
    other claims Yahoo does not separate out — which is why the result carries
    ``bridge_partial``: an unrecognised claim makes this an *over*statement, and
    the reader should know the direction of the error).

    Returns ``None`` when the terminal value is undefined or the share count is
    missing, rather than a partial dict a caller might read the enterprise value
    out of and divide themselves.
    """
    if not _pos(shares) or not _finite(fcf0):
        return None
    flows = project_flows(fcf0, initial_growth, terminal_growth, years)
    tv = terminal_value(flows[-1], discount_rate, terminal_growth)
    if tv is None:
        return None

    pv_explicit = present_value(flows, discount_rate)
    pv_terminal = tv / (1.0 + discount_rate) ** len(flows)
    ev = pv_explicit + pv_terminal
    if not ev > 0:
        return None

    equity = ev + (float(cash) if _finite(cash) else 0.0) \
                - (float(debt) if _finite(debt) else 0.0)
    if not equity > 0:
        # Negative equity value after the bridge is a real answer for a
        # distressed balance sheet, and it is not one this scale can express:
        # every upside below -40% already maps to 0, so there is nothing to add.
        return None

    return {
        "per_share": round(equity / float(shares), 6),
        "enterprise_value": round(ev, 2),
        "equity_value": round(equity, 2),
        "pv_explicit": round(pv_explicit, 2),
        "pv_terminal": round(pv_terminal, 2),
        "terminal_share": round(pv_terminal / ev, 4),
        "years": len(flows),
        "initial_growth": round(float(initial_growth), 6),
        "terminal_growth": round(float(terminal_growth), 6),
        "discount_rate": round(float(discount_rate), 6),
        "bridge_partial": True,
    }


def growth_sensitivity(**kwargs: Any) -> Optional[float]:
    """Fractional change in per-share fair value for ``g + 0.5pp`` (§13).

    The framework asks whether the result moves *violently* for a half-point
    change in terminal growth. That is a measurement, so this measures it rather
    than inferring it from the terminal share — the two correlate but are not the
    same thing, and the one the framework names is this one.

    ``None`` when either valuation refuses, which a caller must treat as "could
    not be checked" and not as "passed".
    """
    base = fair_value(**kwargs)
    if base is None:
        return None
    bumped = dict(kwargs)
    bumped["terminal_growth"] = kwargs.get("terminal_growth", TERMINAL_GROWTH) \
        + SENSITIVITY_PROBE
    probe = fair_value(**bumped)
    if probe is None:
        # The bump pushed g through the WACC spread floor. That is itself the
        # answer: the model is sitting on the singularity.
        return float("inf")
    if not base["per_share"] > 0:
        return None
    return round(abs(probe["per_share"] - base["per_share"]) / base["per_share"], 6)


# ---------------------------------------------------------------------------
# The single scoring path
# ---------------------------------------------------------------------------

def score(*, price: Optional[float],
          base: Optional[float],
          bear: Optional[float] = None,
          bull: Optional[float] = None,
          confidence: Optional[float] = None,
          valuation_date: Optional[str] = None,
          as_of: Optional[str] = None,
          price_date: Optional[str] = None,
          source: str = "derived",
          notes: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Per-share fair values in, ``V_DCF`` out. The one mapping path.

    Both the derived DCF (:func:`from_fundamentals`) and a hand-entered override
    come through here, so the two cannot disagree about how an upside becomes a
    score — the same reason ``routes._dca_score`` is shared between the ranked
    row and the ticker page.

    *bear* and *bull* are optional; supplying neither takes the base-only path
    and caps ``c`` at :data:`BASE_ONLY_CONFIDENCE_CEILING` per §3. Supplying only
    one is treated as base-only as well, rather than inventing the missing wing —
    see :func:`combine_scenarios`.

    *valuation_date* is when the fair values were struck and *as_of* is today, so
    the staleness rule can be applied without this module reading a clock.
    """
    note_list = list(notes or [])

    if not _pos(price):
        return _refused("no_price", source=source, notes=note_list)
    if not _pos(base):
        return _refused("no_fair_value", source=source, notes=note_list)

    age_days = _age_days(valuation_date, as_of)
    if age_days is not None and age_days > MAX_VALUATION_AGE_DAYS:
        return _refused("valuation_stale", source=source, notes=note_list,
                        valuation_date=valuation_date, age_days=age_days)

    price = float(price)
    upsides = {"base": float(base) / price - 1.0}
    if _pos(bear):
        upsides["bear"] = float(bear) / price - 1.0
    if _pos(bull):
        upsides["bull"] = float(bull) / price - 1.0

    scores = {name: v_from_upside(u) for name, u in upsides.items()}
    three = combine_scenarios(scores.get("bear"), scores.get("base"),
                              scores.get("bull"))

    if three is not None:
        raw = three
        ceiling = CONFIDENCE_CEILING
        basis = "three_scenario"
    else:
        raw = scores["base"]
        ceiling = BASE_ONLY_CONFIDENCE_CEILING
        basis = "base_only"
        if len(upsides) > 1:
            # One wing without the other. Reported, because a reader looking at
            # a bear case on the page would otherwise assume it was weighted.
            note_list.append("partial_scenarios_ignored")

    c = float(confidence) if _finite(confidence) else ceiling
    c = _clamp(c, CONFIDENCE_FLOOR, ceiling)
    v = apply_confidence(raw, c)

    return {
        "V": v,
        "refused": False,
        "reason": None,
        "raw": raw,
        "confidence": round(c, 4),
        "confidence_ceiling": ceiling,
        "basis": basis,
        "source": source,
        "price": round(price, 4),
        "price_date": price_date,
        "valuation_date": valuation_date,
        "age_days": age_days,
        "scenarios": [
            {"name": name,
             "fair_value": round(float({"bear": bear, "base": base,
                                        "bull": bull}[name]), 4),
             "upside": round(upsides[name], 6),
             "V": scores[name],
             "weight": SCENARIO_WEIGHTS[name] if three is not None else
                       (1.0 if name == "base" else 0.0)}
            for name in ("bear", "base", "bull") if name in upsides
        ],
        "notes": note_list,
    }


def _age_days(valuation_date: Optional[str], as_of: Optional[str]) -> Optional[int]:
    """Whole days between two ISO dates, or ``None`` if either is unusable.

    ``None`` rather than 0 for an unparseable date: "we do not know how old this
    is" must not read as "it is current", which is the failure §13's staleness
    rule exists to prevent.
    """
    left, right = _as_date(valuation_date), _as_date(as_of)
    if left is None or right is None:
        return None
    return (right - left).days


def _as_date(value: Optional[str]) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, bool):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Deriving the scenarios from statements
# ---------------------------------------------------------------------------

def growth_profile(fcf_series: Sequence[float]) -> Optional[dict[str, Any]]:
    """Base growth and a scenario spread, from a company's own FCF history.

    *fcf_series* is oldest-first annual free cash flow. Returns the compound
    growth rate over the whole window as the base case and the standard deviation
    of the year-over-year rates as the half-width of the Bear/Bull band.

    Using the company's own dispersion rather than a house ±20% is the point: a
    utility and a foundry should not get the same scenario band, and the
    difference between them is observable in the series both are scored from.

    A year with non-positive FCF makes the CAGR undefined, so the window is
    trimmed to the longest positive run ending at the most recent year — the
    recent history is the relevant one, and a loss-making year eight years ago
    should not delete a company that has generated cash ever since.
    """
    clean = [float(x) for x in fcf_series if _finite(x)]
    run: list[float] = []
    for value in reversed(clean):
        if value <= 0:
            break
        run.append(value)
    run.reverse()

    if len(run) < MIN_FCF_OBSERVATIONS:
        return None

    periods = len(run) - 1
    cagr = (run[-1] / run[0]) ** (1.0 / periods) - 1.0
    yoy = [run[i] / run[i - 1] - 1.0 for i in range(1, len(run))]
    mean = sum(yoy) / len(yoy)
    variance = sum((r - mean) ** 2 for r in yoy) / len(yoy)
    spread = math.sqrt(variance)

    clamped_base = _clamp(cagr, GROWTH_FLOOR, GROWTH_CEILING)
    clamped_spread = _clamp(spread, SPREAD_FLOOR, SPREAD_CEILING)
    notes: list[str] = []
    if abs(clamped_base - cagr) > 1e-9:
        notes.append("growth_clamped")
    if abs(clamped_spread - spread) > 1e-9:
        notes.append("spread_clamped")

    return {
        "base": round(clamped_base, 6),
        "bear": round(_clamp(clamped_base - clamped_spread,
                             GROWTH_FLOOR - SPREAD_CEILING, GROWTH_CEILING), 6),
        "bull": round(_clamp(clamped_base + clamped_spread,
                             GROWTH_FLOOR, GROWTH_CEILING + SPREAD_CEILING), 6),
        "spread": round(clamped_spread, 6),
        "observed_cagr": round(cagr, 6),
        "observed_spread": round(spread, 6),
        "observations": len(run),
        "notes": notes,
    }


def derive_confidence(*, observations: int, terminal_share: float,
                      spread: float) -> float:
    """``c`` for a derived DCF, from things that were measured.

    Starts at the ceiling and deducts for each source of doubt that is actually
    visible in the inputs: a short history, a terminal value carrying most of the
    valuation, and a wide scenario band. Floors at :data:`CONFIDENCE_FLOOR`.

    Deliberately **not** a judgement about the company. Every term here is a
    property of the evidence, so a reader can check each deduction against the
    numbers on the page — which is what separates a confidence discount from a
    fudge factor.
    """
    c = CONFIDENCE_CEILING
    if observations < 6:
        c -= 0.10
    if observations < 5:
        c -= 0.05
    if terminal_share > WARN_TERMINAL_SHARE:
        # §13: a terminal value carrying more than ~80% means the answer is
        # mostly about the assumption, not the forecast.
        c -= 0.15
    elif terminal_share > 0.70:
        c -= 0.05
    if spread > 0.10:
        c -= 0.10
    elif spread > 0.06:
        c -= 0.05
    return round(_clamp(c, CONFIDENCE_FLOOR, CONFIDENCE_CEILING), 4)


def from_fundamentals(*, price: Optional[float],
                      fcf_series: Sequence[float],
                      shares: Optional[float],
                      cash: Optional[float] = 0.0,
                      debt: Optional[float] = 0.0,
                      beta: Optional[float] = None,
                      risk_free: float = DEFAULT_RISK_FREE,
                      erp: float = DEFAULT_ERP,
                      tax_rate: float = DEFAULT_TAX_RATE,
                      cost_of_debt: Optional[float] = None,
                      wacc_override: Optional[float] = None,
                      form: str = FORM_FCFF,
                      mid_cycle: bool = False,
                      terminal_growth: float = TERMINAL_GROWTH,
                      price_date: Optional[str] = None,
                      as_of: Optional[str] = None) -> dict[str, Any]:
    """A three-scenario FCFF DCF from statement history. The derived path.

    Everything here comes from the annual vintages
    :func:`ystocker.dca_history.build_vintages` already produces, so this costs
    **no extra Yahoo call** — the same property that lets the daily snapshot
    sweep cover the universe for free.

    *mid_cycle* starts the projection from the window's **mean** free cash flow
    rather than the latest year, for the templates §7 and §11 require it on.
    This is a different guard from the growth clamp and both are needed: the
    clamp stops a peak *rate* being extrapolated, but at the top of a cycle the
    starting *level* is itself the peak, and growing a peak slowly for ten years
    values the company as though the peak were the new floor.

    Refuses, loudly and by name, in every case §13 lists. In particular it
    refuses outright for a bank or a REIT rather than running FCFF on a balance
    sheet the form does not describe: §10 and §12 both say the standard
    enterprise DCF should be omitted there, and a number produced anyway would be
    indistinguishable on the page from one produced correctly.
    """
    if form != FORM_FCFF:
        return _refused("form_not_applicable", form=form)
    if not _pos(price):
        return _refused("no_price")
    if not _pos(shares):
        return _refused("no_shares")

    clean = [float(x) for x in fcf_series if _finite(x)]
    if not clean:
        return _refused("no_fcf")
    if clean[-1] <= 0:
        # §13: negative free cash flow with no evidenced path to positive. The
        # relative block still scores the company; this branch simply has
        # nothing to say, which is different from saying it is expensive.
        return _refused("negative_fcf", latest_fcf=round(clean[-1], 2))

    profile = growth_profile(clean)
    if profile is None:
        return _refused("too_few_observations", observations=len(clean),
                        required=MIN_FCF_OBSERVATIONS)

    capital = wacc(beta=beta, risk_free=risk_free, erp=erp,
                   cost_of_debt=cost_of_debt, tax_rate=tax_rate,
                   equity_value=float(price) * float(shares), debt=debt)
    if _pos(wacc_override):
        # A hand-supplied discount rate **replaces** the derived one rather
        # than feeding into it. The obvious shortcut -- set the risk-free rate
        # to the target and zero the ERP -- does not work and fails quietly:
        # the debt weighting still applies, so a requested 12% comes out at
        # 11.9% and the page reports a number the valuation did not use.
        #
        # The CAPM components are dropped rather than kept alongside, because
        # §2 requires the reported assumptions to be the ones in force. A beta
        # and an ERP printed next to a rate they did not produce is worse than
        # no beta at all.
        capital = {"wacc": round(float(wacc_override), 6),
                   "source": "override",
                   "notes": ["wacc_overridden"]}
    rate = capital["wacc"]
    if rate - terminal_growth < MIN_WACC_SPREAD:
        return _refused("wacc_not_above_g", wacc=rate,
                        terminal_growth=terminal_growth, capital=capital)

    years = LONG_EXPLICIT_YEARS if profile["base"] > LONG_EXPLICIT_THRESHOLD \
        else EXPLICIT_YEARS

    # §7/§11: a cyclical is projected from the mean of its own window, not from
    # whatever the last year happened to be. The mean is taken over the same
    # positive run the growth profile measured, so the level and the rate
    # describe one window rather than two.
    run = clean[-profile["observations"]:]
    start = (sum(run) / len(run)) if mid_cycle else clean[-1]

    common = dict(fcf0=start, discount_rate=rate,
                  terminal_growth=terminal_growth, years=years,
                  cash=cash or 0.0, debt=debt or 0.0, shares=shares)

    values = {name: fair_value(initial_growth=profile[name], **common)
              for name in ("bear", "base", "bull")}
    if values["base"] is None:
        return _refused("wacc_not_above_g", wacc=rate,
                        terminal_growth=terminal_growth, capital=capital)

    terminal_share = values["base"]["terminal_share"]
    if terminal_share > MAX_TERMINAL_SHARE:
        return _refused("terminal_dominates", terminal_share=terminal_share,
                        limit=MAX_TERMINAL_SHARE, capital=capital)

    sensitivity = growth_sensitivity(initial_growth=profile["base"], **common)
    if sensitivity is None or sensitivity > MAX_SENSITIVITY:
        return _refused("g_sensitivity",
                        sensitivity=None if sensitivity is None
                        else (None if math.isinf(sensitivity) else sensitivity),
                        limit=MAX_SENSITIVITY, capital=capital)

    confidence = derive_confidence(observations=profile["observations"],
                                   terminal_share=terminal_share,
                                   spread=profile["spread"])

    notes = list(profile["notes"]) + list(capital["notes"])
    if terminal_share > WARN_TERMINAL_SHARE:
        notes.append("terminal_heavy")
    if mid_cycle:
        notes.append("mid_cycle_base")

    out = score(price=price,
                bear=values["bear"]["per_share"] if values["bear"] else None,
                base=values["base"]["per_share"],
                bull=values["bull"]["per_share"] if values["bull"] else None,
                confidence=confidence,
                # Deliberately **no** valuation_date, so §13's staleness gate
                # does not fire on this path. That rule guards a *stored* fair
                # value being scored against a price it was not struck at; here
                # the fair value and the price are computed at the same moment
                # from the same close, so the upside is internally consistent
                # whatever the date.
                #
                # Applying the gate anyway is actively incoherent, which is how
                # this was found: an old reconstruction would refuse the DCF for
                # stale prices while ``V_REL`` went on ranking multiples built
                # from those same stale prices. One branch dropped and the other
                # kept, on identical evidence. How old the whole reconstruction
                # is belongs to the payload's ``stale`` flag and the window
                # block, which the page already shows.
                valuation_date=None, as_of=as_of,
                price_date=price_date, source="derived", notes=notes)

    out["model"] = {
        "form": form,
        "mid_cycle": bool(mid_cycle),
        "years": years,
        "growth": profile,
        "capital": capital,
        "terminal_growth": terminal_growth,
        "terminal_share": terminal_share,
        "sensitivity": None if sensitivity is None or math.isinf(sensitivity)
                       else sensitivity,
        "fcf0": round(start, 2),
        "fcf_latest": round(clean[-1], 2),
        "shares": round(float(shares), 2),
        "cash": round(float(cash), 2) if _finite(cash) else None,
        "debt": round(float(debt), 2) if _finite(debt) else None,
        "scenario_values": {k: v for k, v in values.items() if v is not None},
    }
    return out
