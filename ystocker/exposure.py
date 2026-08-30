"""
ystocker.exposure
~~~~~~~~~~~~~~~~~
Turning 穿透 output into a *decision* — does this trade breach a stated limit?

Why this exists
---------------
:mod:`ystocker.lookthrough` answers "how much NVDA do I actually own". This
module answers the next question, which is the one a portfolio-aware agent has to
ask: "given a limit of 8% per name, may I buy more?" Those look like the same
question and are not, because every figure look-through produces is a **floor**.

The trap this module exists to close
------------------------------------
Yahoo discloses a fund's top ten holdings only — 37.6% of VOO by weight. So a
reported 5.4% NVDA is a lower bound, and :attr:`lookthrough.Result.coverage_pct`
says so in as many words: *a 40% coverage means a reported 6% NVDA position is
consistent with anything from 6% to a great deal more*.

Check that floor against a ceiling limit with a plain ``>`` and the constraint
**fails silently in the dangerous direction**. It fires only once the position is
already badly breached, and — worse — anything downstream reading "NVDA 5.4%,
limit 8%" will compute a headroom of 2.6% that the data does not support. That is
a fabricated number produced at the exact moment somebody is making a
concentration decision, which is the one thing ``lookthrough`` was written to
avoid. Handing the floor to an LLM and asking "is there room?" re-introduces it
one layer up.

So a check here is **three-valued**, and only one of the three is a pass:

    floor            >  limit   ->  BREACH         (already over, no ambiguity)
    floor + unknown  >  limit   ->  INDETERMINATE  (data cannot rule out a breach)
    floor + unknown  <= limit   ->  PASS           (verified, the only pass)

``INDETERMINATE`` is a real answer, not a failure to compute one. For a portfolio
of broad index funds it is usually the *correct* answer: you genuinely cannot
verify a single-name limit from top-ten disclosures, and a system that reported
PASS there would be asserting something it cannot know.

Where the band comes from
-------------------------
Nothing new is estimated. ``lookthrough``'s partition is closed by construction::

    seen + undisclosed_equity + non_equity + unclassified
         + unresolved + truncated + pending  ==  total_value

Of those residual buckets exactly one is *known not to be a company*:
``non_equity`` is measured bonds and cash. Every other bucket could, as far as the
data goes, be the name in question:

* ``undisclosed_equity`` — measured equity, unidentified. The 62% of VOO.
* ``unclassified``       — a fund reporting no asset classes at all. Unknown.
* ``pending``            — nobody has fetched it yet. Says nothing about the security.
* ``truncated``          — depth cap, cycle or node budget stopped the walk. Still a wrapper.
* ``unresolved``         — a named leaf that 404s. XTSLA is a cash sweep, but the
  resolver returned None, so *we do not know that*. Counted toward the band
  because the conservative direction is the honest one here.

Summed, that gives :func:`unknown_value` — how much of the portfolio is
unidentified. It is the honest headline but a poor band width, because it allows
all of the unknown dollars to be one company, which no real fund does.

The band is narrowed by *deduction*, not estimation
---------------------------------------------------
A fund's top-ten disclosure is **ordered by weight**, and that yields two facts
rather than guesses:

1. A **disclosed** name's weight in that fund is **exact**. AAPL at 7% of VOO
   cannot also be hiding in VOO's undisclosed 60%, because the undisclosed part is
   by construction the holdings *outside* the top ten. A disclosed name therefore
   draws zero band width from that fund.
2. An **undisclosed** name's weight in that fund is at most ``min(disclosed
   weights)`` — anything heavier would have made the top ten.

:class:`lookthrough.Pocket` carries the per-fund metadata this needs and does the
arithmetic; :func:`unknown_for` sums it per name. The effect is large: a megacap
disclosed by every fund holding it comes out **verified** — floor equals ceiling —
inside a portfolio that is otherwise 35% unidentified.

Both facts need the undisclosed tail to consist of holdings each listed once,
which fails when the tail may contain *funds*: a company can hide inside several
of them at once. ``Pocket.flat`` records whether any disclosed holding of that
fund was itself a fund, and a non-flat pocket falls back to its full
``hidden_value``. That is why a fund-of-funds like VTTSX gets no narrowing and an
index fund gets a great deal.

Two things deliberately stay loose. ``unresolved``, ``truncated`` and ``pending``
dollars stopped at something the walk never opened, so there is no disclosure to
reason from. And the narrowing must remain a deduction — the moment it becomes
"the invisible 62% probably resembles the visible 38%", it is the extrapolation
the whole design refuses, reintroduced at the exact point a concentration decision
is made.

One assumption is worth knowing: the vendor really returned *top N by weight* and
not an arbitrary subset. ``lookthrough`` renormalising weights that sum past
tolerance is a standing signal that payloads are not always clean, so
:func:`unknown_for` is never allowed to exceed :func:`unknown_value` — the loose
bound remains a ceiling on the tight one, asserted directly by the tests.

What is deliberately absent
---------------------------
There is no ``headroom`` field, and no "suggested position size". Both require
committing to a point estimate inside the band, which is the fabrication above
wearing a different hat. Callers get the floor, the ceiling, and the verdict.

Pure and network-free, on the same terms as ``lookthrough``: the fund resolver is
injected and this module imports nothing but the stdlib and ``lookthrough``
itself. That is what lets the arithmetic be pinned with no app, no cache and no
Yahoo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from ystocker import lookthrough
from ystocker.lookthrough import Position, Result

log = logging.getLogger(__name__)

#: Verdicts. Only :data:`VERDICT_PASS` means the limit was verified to hold.
VERDICT_PASS = "pass"
VERDICT_INDETERMINATE = "indeterminate"
VERDICT_BREACH = "breach"

#: Holding-type tags. Free-form beyond these two is allowed — they are the
#: caller's labels and this module only carries them through — but these are the
#: pair the UI offers, kept here so a consumer has something to compare against.
HOLDING_CORE = "core"
HOLDING_TACTICAL = "tactical"

#: How many names a rendered block lists before truncating. A report has to fit
#: in a prompt alongside everything else; the tail of a long-tail exposure list
#: is noise for a concentration question.
DEFAULT_TOP_NAMES = 15


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PortfolioPolicy:
    """The limits, which are *stated by the user*, not derived from the holdings.

    This is the part of a portfolio context that cannot be computed. Look-through
    measures what is held; only the holder can say what the ceiling is meant to
    be, how much cash is available, or which lines are long-term core positions
    versus tactical ones. Keeping it in its own frozen object is what stops a
    measured quantity and a declared one from being confused later.

    ``max_single_name_pct`` and ``max_issuer_pct`` are percentages of portfolio
    value (8.0 means 8%), or None for "no limit stated", in which case no check is
    emitted at all — an absent limit must not read as a satisfied one.
    """
    max_single_name_pct: Optional[float] = None
    max_issuer_pct: Optional[float] = None
    #: Investable cash, USD. Used only by the liquidity check.
    cash: float = 0.0
    #: symbol -> free-form tag, e.g. ``{"AAPL": "core", "SOXQ": "tactical"}``.
    holding_types: Mapping[str, str] = field(default_factory=dict)

    def type_of(self, symbol: str) -> str:
        return str(self.holding_types.get(str(symbol).upper(), "") or "")


@dataclass(frozen=True)
class Trade:
    """A proposed change to one line, in dollars. Positive buys, negative sells."""
    symbol: str
    delta_value: float

    @property
    def is_buy(self) -> bool:
        return self.delta_value > 0


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

@dataclass
class Band:
    """What the data supports about one name's true exposure: a floor and a ceiling.

    ``floor`` is measured. ``ceiling`` is ``floor`` plus every unknown dollar in
    the portfolio, so the interval is where the truth provably lies — not a
    confidence interval, and not centred on anything.
    """
    symbol: str
    name: str
    floor_value: float
    unknown_value: float
    total_value: float

    @property
    def ceiling_value(self) -> float:
        return self.floor_value + self.unknown_value

    @property
    def floor_pct(self) -> float:
        return _pct_of(self.floor_value, self.total_value)

    @property
    def ceiling_pct(self) -> float:
        return _pct_of(self.ceiling_value, self.total_value)

    @property
    def is_verified(self) -> bool:
        """True when the band has collapsed — the floor *is* the exposure."""
        return self.unknown_value <= 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name or self.symbol,
            "floor_value": round(self.floor_value, 2),
            "floor_pct": round(self.floor_pct, 4),
            "ceiling_value": round(self.ceiling_value, 2),
            "ceiling_pct": round(self.ceiling_pct, 4),
            "unknown_value": round(self.unknown_value, 2),
            "verified": self.is_verified,
        }


@dataclass
class Check:
    """One limit, evaluated against one name, after the proposed trade."""
    kind: str
    symbol: str
    name: str
    limit_pct: float
    before: Band
    after: Band
    verdict: str

    @property
    def floor_delta_pct(self) -> float:
        return self.after.floor_pct - self.before.floor_pct

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "name": self.name or self.symbol,
            "limit_pct": round(self.limit_pct, 4),
            "verdict": self.verdict,
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "floor_delta_pct": round(self.floor_delta_pct, 4),
        }


@dataclass
class LiquidityCheck:
    """Whether stated cash covers a proposed buy. Purely declared-vs-declared."""
    required_value: float
    cash: float
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {"required_value": round(self.required_value, 2),
                "cash": round(self.cash, 2),
                "verdict": self.verdict}


@dataclass
class Assessment:
    """The whole answer: portfolio bands before and after, and every verdict."""
    trade: Optional[Trade]
    total_before: float
    total_after: float
    #: Dollars that could belong to any company, after the trade. The single
    #: number that decides whether a PASS is achievable at all.
    unknown_value: float
    #: Dollars measured as bonds/cash, after the trade.
    known_non_equity_value: float
    #: Dollars resolved all the way to a named company, after the trade.
    attributed_value: float
    coverage_before_pct: float
    coverage_after_pct: float
    checks: list[Check]
    liquidity: Optional[LiquidityCheck]
    notes: list[str]

    @property
    def unknown_pct(self) -> float:
        return _pct_of(self.unknown_value, self.total_after)

    @property
    def verdict(self) -> str:
        """The worst verdict among all checks — BREACH beats INDETERMINATE beats PASS.

        A caller asking one question ("may I place this trade?") needs one answer,
        and the safe fold is the pessimistic one.
        """
        order = (VERDICT_BREACH, VERDICT_INDETERMINATE, VERDICT_PASS)
        found = {c.verdict for c in self.checks}
        if self.liquidity is not None:
            found.add(self.liquidity.verdict)
        for v in order:
            if v in found:
                return v
        return VERDICT_PASS

    @property
    def breaches(self) -> list[Check]:
        """Checks whose *measured floor* already exceeds the limit — unambiguous."""
        return [c for c in self.checks if c.verdict == VERDICT_BREACH]

    def as_dict(self, top: Optional[int] = DEFAULT_TOP_NAMES) -> dict[str, Any]:
        checks = self.checks[:top] if top else self.checks
        return {
            "trade": ({"symbol": self.trade.symbol,
                       "delta_value": round(self.trade.delta_value, 2)}
                      if self.trade else None),
            "verdict": self.verdict,
            "total_before": round(self.total_before, 2),
            "total_after": round(self.total_after, 2),
            "attributed_value": round(self.attributed_value, 2),
            "unknown_value": round(self.unknown_value, 2),
            "unknown_pct": round(self.unknown_pct, 4),
            "known_non_equity_value": round(self.known_non_equity_value, 2),
            "coverage_before_pct": round(self.coverage_before_pct, 2),
            "coverage_after_pct": round(self.coverage_after_pct, 2),
            "checks": [c.as_dict() for c in checks],
            "check_count": len(self.checks),
            "breach_count": len(self.breaches),
            "liquidity": self.liquidity.as_dict() if self.liquidity else None,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# The arithmetic — separated because these two functions are the whole claim,
# and the tests assert them directly rather than through ``assess``.
# ---------------------------------------------------------------------------

def unknown_value(result: Result) -> float:
    """Portfolio-level dollars that could belong to *some* company.

    Every residual bucket except ``non_equity``. ``non_equity`` is the one that
    was *measured* to not be a company — folding it in would report a bond sleeve
    as possible hidden concentration, which is the same mistake ``_partition``
    exists to avoid one level down.

    This is the **loose** bound, and it is a headline figure rather than a band
    width: it says how much of the portfolio is unidentified, not how much of it
    could be one particular name. For that use :func:`unknown_for`, which is never
    larger — :meth:`tests.test_exposure.TestInvariant` asserts the ordering.
    """
    r = result.residual
    return max(0.0, r.total() - r.non_equity)


def unknown_for(result: Result, symbols: Iterable[str]) -> float:
    """Dollars that could belong to *symbols*, bounded by what each fund disclosed.

    The tightened band width, and the reason it is much narrower than
    :func:`unknown_value`: a fund's top-ten disclosure is ordered by weight, so a
    disclosed name's weight in that fund is exact and an undisclosed name's is at
    most the smallest disclosed weight. :class:`lookthrough.Pocket` carries the
    per-fund metadata and does that arithmetic; this sums it and adds back the
    buckets no disclosure can bound.

    ``unresolved``, ``truncated`` and ``pending`` stay fully loose on purpose.
    Those dollars stopped at something the walk never opened — a 404, a depth cap,
    an unfetched symbol — so there is no disclosure to reason from, and narrowing
    them would be a guess dressed as a deduction.
    """
    wanted = [str(s).upper().strip() for s in symbols if str(s).strip()]
    if not wanted:
        return 0.0
    structured = sum(p.ceiling_for_any(wanted) for p in result.pockets)
    r = result.residual
    return max(0.0, structured + r.unresolved + r.truncated + r.pending)


def verdict_for(floor_value: float, unknown: float, total: float,
                limit_pct: float) -> str:
    """The three-valued check. See the module docstring for why it is not a ``>``.

    A limit of 0 or less is treated as "any exposure breaches", which is the
    literal reading and the useful one for an exclusion list.
    """
    if total <= 0:
        # Nothing held breaches no concentration limit. Not an error, and not
        # indeterminate either: the answer is known.
        return VERDICT_PASS
    limit_value = total * (limit_pct / 100.0)
    if floor_value > limit_value:
        return VERDICT_BREACH
    if floor_value + max(0.0, unknown) > limit_value:
        return VERDICT_INDETERMINATE
    return VERDICT_PASS


def band_for(result: Result, symbol: str, name: str = "") -> Band:
    """The floor/ceiling band for one name in *result*."""
    sym = str(symbol).upper().strip()
    floor = 0.0
    found_name = name
    for exp in result.exposures:
        if exp.symbol == sym and exp.kind == lookthrough.LEAF_EQUITY:
            floor = exp.value
            found_name = found_name or exp.name
            break
    return Band(symbol=sym, name=found_name or sym, floor_value=floor,
                unknown_value=unknown_for(result, (sym,)),
                total_value=result.total_value)


# ---------------------------------------------------------------------------
# Applying a trade
# ---------------------------------------------------------------------------

def apply_trade(positions: Iterable[Position], trade: Optional[Trade],
                ) -> tuple[list[Position], list[str]]:
    """Positions as they would stand after *trade*, plus any notes.

    A sell larger than the line is clamped to zero rather than allowed to go
    short: look-through has no meaning for a negative holding (``analyse`` drops
    such lines and notes them), so silently passing one through would shrink the
    portfolio total and make every percentage wrong at once.
    """
    out = [Position(symbol=p.symbol, value=p.value, name=p.name, account=p.account)
           for p in positions]
    notes: list[str] = []
    if trade is None or not trade.symbol:
        return out, notes

    sym = str(trade.symbol).upper().strip()
    delta = float(trade.delta_value)
    if delta == 0:
        return out, notes

    for pos in out:
        if str(pos.symbol).upper().strip() == sym:
            new_value = pos.value + delta
            if new_value < 0:
                notes.append(
                    f"{sym}: sell of {abs(delta):,.0f} exceeds the {pos.value:,.0f} "
                    f"held — clamped to a full exit")
                new_value = 0.0
            pos.value = new_value
            return out, notes

    # Not currently held.
    if delta < 0:
        notes.append(f"{sym}: sell proposed but nothing is held — ignored")
        return out, notes
    out.append(Position(symbol=sym, value=delta, name=""))
    return out, notes


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def review(result: Result, policy: PortfolioPolicy,
           issuer_groups: Optional[Mapping[str, Iterable[str]]] = None,
           ) -> Assessment:
    """Evaluate *policy* against an **already computed** look-through result.

    The no-trade path, and the one the request handler uses. ``/api/assets`` has
    already walked the portfolio by the time it wants a verdict, and
    :func:`assess` would walk it twice more — which on a cold cache is the
    hundred-Yahoo-call problem ``assets`` exists to avoid, and even warm is a
    recursion over every fund for no new information. With no trade the before and
    after states are the same object by construction, so there is nothing to
    recompute.
    """
    return _build(before=result, after=result, policy=policy, trade=None,
                  issuer_groups=issuer_groups, notes=[])


def assess(positions: Iterable[Position],
           policy: PortfolioPolicy,
           resolve: Callable[[str], Optional[dict[str, Any]]],
           trade: Optional[Trade] = None,
           max_depth: int = lookthrough.DEFAULT_MAX_DEPTH,
           max_nodes: int = lookthrough.DEFAULT_MAX_NODES,
           issuer_groups: Optional[Mapping[str, Iterable[str]]] = None,
           ) -> Assessment:
    """Evaluate *policy* against *positions*, optionally after *trade*.

    ``resolve`` is handed straight to :func:`lookthrough.analyse` and carries the
    same contract — including that a ``peek``-style resolver may return
    ``kind="pending"``, which widens the band rather than being mistaken for a
    statement about the security. That is why a cold cache produces
    INDETERMINATE and not a confident PASS.

    ``issuer_groups`` maps an issuer label to its symbols, for the share-class
    case (``{"Alphabet": ["GOOG", "GOOGL"]}``). Supplied by the caller rather than
    inferred, because grouping by name similarity will occasionally merge two
    different companies — ``assets._issuer_groups`` reports it as a hint for
    exactly that reason, and a *limit* must not be evaluated against a guess.

    With a trade, look-through runs **twice**, before and after. That is
    deliberate rather than patching the first result: a trade can change which
    funds are held at all, and incrementally adjusting an exposure table would have
    to re-derive the residual partition by hand — the one piece of arithmetic that
    must stay exactly closed. Without a trade it runs once; see :func:`review`.
    """
    before_positions = [p for p in positions]
    before = lookthrough.analyse(before_positions, resolve,
                                 max_depth=max_depth, max_nodes=max_nodes)
    if trade is None or not trade.symbol or not trade.delta_value:
        # Nothing moves, so the after state is the before state. Walking again
        # would produce an identical Result at full cost.
        return _build(before=before, after=before, policy=policy, trade=trade,
                      issuer_groups=issuer_groups, notes=[])

    after_positions, notes = apply_trade(before_positions, trade)
    after = lookthrough.analyse(after_positions, resolve,
                                max_depth=max_depth, max_nodes=max_nodes)
    return _build(before=before, after=after, policy=policy, trade=trade,
                  issuer_groups=issuer_groups, notes=notes)


def _build(*, before: Result, after: Result, policy: PortfolioPolicy,
           trade: Optional[Trade],
           issuer_groups: Optional[Mapping[str, Iterable[str]]],
           notes: list[str]) -> Assessment:
    """Assemble the verdicts. Shared by :func:`assess` and :func:`review`."""
    checks: list[Check] = []
    if policy.max_single_name_pct is not None:
        checks.extend(_name_checks(before, after, policy.max_single_name_pct,
                                   traded=trade.symbol if trade else ""))
    if policy.max_issuer_pct is not None and issuer_groups:
        checks.extend(_issuer_checks(before, after, policy.max_issuer_pct,
                                     issuer_groups))

    # Worst first: a caller rendering a truncated list must not lose a breach to
    # a long tail of larger-but-passing names.
    _rank = {VERDICT_BREACH: 0, VERDICT_INDETERMINATE: 1, VERDICT_PASS: 2}
    checks.sort(key=lambda c: (_rank.get(c.verdict, 3), -c.after.floor_value))

    liquidity = None
    if trade is not None and trade.is_buy:
        liquidity = LiquidityCheck(
            required_value=trade.delta_value, cash=policy.cash,
            verdict=(VERDICT_PASS if trade.delta_value <= policy.cash
                     else VERDICT_BREACH))

    out_notes = list(notes)
    out_notes.extend(n for n in after.notes if n not in out_notes)

    return Assessment(
        trade=trade,
        total_before=before.total_value,
        total_after=after.total_value,
        unknown_value=unknown_value(after),
        known_non_equity_value=after.residual.non_equity,
        attributed_value=after.seen_value,
        coverage_before_pct=before.coverage_pct,
        coverage_after_pct=after.coverage_pct,
        checks=checks,
        liquidity=liquidity,
        notes=out_notes,
    )


def policy_from_stored(stored: Mapping[str, Any]) -> PortfolioPolicy:
    """A :class:`PortfolioPolicy` from ``portfolio.load_policy``'s dict.

    Kept here rather than in ``portfolio`` so the store stays free of any
    dependency on this module: ``portfolio`` persists a validated dict and knows
    nothing about bands or verdicts.
    """
    return PortfolioPolicy(
        max_single_name_pct=stored.get("max_single_name_pct"),
        max_issuer_pct=stored.get("max_issuer_pct"),
        cash=float(stored.get("cash") or 0.0),
        holding_types=dict(stored.get("holding_types") or {}),
    )


def _name_checks(before: Result, after: Result, limit_pct: float,
                 traded: str = "") -> list[Check]:
    """One check per named exposure, plus the traded symbol even at zero."""
    symbols: list[str] = [e.symbol for e in after.exposures
                          if e.kind == lookthrough.LEAF_EQUITY]
    traded_sym = str(traded).upper().strip()
    if traded_sym and traded_sym not in symbols:
        # A buy whose symbol never resolves to a company still needs a row, or the
        # one name the caller asked about is the one missing from the answer.
        symbols.append(traded_sym)

    out: list[Check] = []
    for sym in symbols:
        b_before = band_for(before, sym)
        b_after = band_for(after, sym)
        out.append(Check(
            kind="single_name", symbol=sym, name=b_after.name,
            limit_pct=limit_pct, before=b_before, after=b_after,
            # The band width is per name, not the portfolio scalar: a name every
            # fund discloses has an exact exposure even in a portfolio that is
            # mostly unidentified.
            verdict=verdict_for(b_after.floor_value, b_after.unknown_value,
                                after.total_value, limit_pct)))
    return out


def _issuer_checks(before: Result, after: Result, limit_pct: float,
                   groups: Mapping[str, Iterable[str]]) -> list[Check]:
    """Checks for caller-declared share-class groups (GOOG + GOOGL)."""
    out: list[Check] = []
    for issuer, members in groups.items():
        syms = [str(s).upper().strip() for s in members if str(s).strip()]
        if len(syms) < 2:
            continue
        b_before = _summed_band(before, issuer, syms)
        b_after = _summed_band(after, issuer, syms)
        out.append(Check(
            kind="issuer", symbol=issuer, name=issuer, limit_pct=limit_pct,
            before=b_before, after=b_after,
            verdict=verdict_for(b_after.floor_value, b_after.unknown_value,
                                after.total_value, limit_pct)))
    return out


def _summed_band(result: Result, label: str, symbols: Iterable[str]) -> Band:
    """A band whose floor is several names added together.

    The width is computed for the group as a whole rather than summed per member.
    Two things would go wrong otherwise: the same unidentified dollar would be
    counted once for GOOG and again for GOOGL, and a pocket that discloses one
    member but not the other has a bound that depends on *which* members are
    missing. :meth:`lookthrough.Pocket.ceiling_for_any` handles both.
    """
    floor = 0.0
    wanted = {str(s).upper().strip() for s in symbols}
    for exp in result.exposures:
        if exp.symbol in wanted and exp.kind == lookthrough.LEAF_EQUITY:
            floor += exp.value
    return Band(symbol=label, name=label, floor_value=floor,
                unknown_value=unknown_for(result, wanted),
                total_value=result.total_value)


# ---------------------------------------------------------------------------
# Rendering for a prompt
# ---------------------------------------------------------------------------

def render_block(assessment: Assessment, top: int = DEFAULT_TOP_NAMES) -> str:
    """The assessment as a text block for injection into a model prompt.

    Every number here is computed, never asked for: an LLN handed a floor and a
    limit will produce a headroom figure, and an LLM handed a table of before/after
    percentages will occasionally get the subtraction wrong in a way that reads
    exactly like getting it right. So the arithmetic is done here and the block is
    framed as given facts.

    English only, deliberately. This is model *input*, not user-visible text, and
    TradingAgents already controls output language separately via its own language
    instruction — so localising it would add a translation-parity burden for a
    string no human reads. Contrast ``assets.build_ai_prompt``, whose output the
    reader does see.
    """
    a = assessment
    lines: list[str] = ["<start_of_portfolio_constraints>"]

    if a.trade is not None:
        verb = "BUY" if a.trade.is_buy else "SELL"
        lines.append(f"PROPOSED TRADE: {verb} {abs(a.trade.delta_value):,.0f} USD "
                     f"of {a.trade.symbol.upper()}")
    else:
        lines.append("PROPOSED TRADE: none — this is a portfolio state review")

    lines.append(f"OVERALL VERDICT: {a.verdict.upper()}")
    lines.append("")
    lines.append("HOW TO READ THESE NUMBERS")
    lines.append(
        "  Fund providers disclose only their top ten holdings, so a company-level"
        " exposure is a measured FLOOR, not an estimate. Each name below therefore"
        " carries a floor and a ceiling; the true value provably lies between them."
        " Do NOT compute headroom against the floor, and do NOT treat the midpoint"
        " as an estimate.")
    lines.append(
        f"  Portfolio value after trade: ${a.total_after:,.0f}. "
        f"Resolved to named companies: {a.coverage_after_pct:.1f}%. "
        f"Unidentified across the whole portfolio: {a.unknown_pct:.1f}%.")
    lines.append(
        "  Each band below is narrower than that portfolio figure, and"
        " legitimately so: a fund's disclosure is ordered by weight, so a name the"
        " fund discloses cannot also be hiding in its undisclosed tail, and a name"
        " it does not disclose cannot weigh more than the smallest holding it did"
        " disclose. A band shown as verified has no unidentified dollars that could"
        " be that name at all.")
    lines.append(
        "  VERDICT vocabulary: BREACH = the measured floor alone exceeds the limit."
        " INDETERMINATE = the floor is under the limit but the disclosed data"
        " cannot rule out a breach. PASS = the ceiling is under the limit, so the"
        " limit is verified to hold. Only PASS is a pass.")
    lines.append("")

    if a.checks:
        lines.append(f"LIMIT CHECKS (worst first, {len(a.checks)} total)")
        for c in a.checks[:top]:
            delta = c.floor_delta_pct
            arrow = (f"{c.before.floor_pct:.2f}% -> {c.after.floor_pct:.2f}%"
                     if abs(delta) >= 0.005 else f"{c.after.floor_pct:.2f}% (unchanged)")
            lines.append(
                f"  [{c.verdict.upper()}] {c.symbol} ({c.kind}, limit "
                f"{c.limit_pct:.1f}%): floor {arrow}; "
                f"band after trade at least {c.after.floor_pct:.2f}% and at most "
                f"{c.after.ceiling_pct:.2f}%"
                + ("; band is verified (nothing unidentified)"
                   if c.after.is_verified else ""))
        if len(a.checks) > top:
            extra = len(a.checks) - top
            lines.append(f"  ... {extra} further "
                         f"{'name' if extra == 1 else 'names'} not shown "
                         f"(all ranked below the above)")
    else:
        lines.append("LIMIT CHECKS: none — no limits were stated in the policy. "
                     "An absent limit is not a satisfied limit; do not infer one.")
    lines.append("")

    if a.liquidity is not None:
        lines.append(
            f"CASH: buy needs ${a.liquidity.required_value:,.0f} against "
            f"${a.liquidity.cash:,.0f} stated available "
            f"[{a.liquidity.verdict.upper()}]")
        lines.append("")

    if a.notes:
        lines.append("DATA NOTES")
        for n in a.notes[:8]:
            lines.append(f"  {n}")
        lines.append("")

    lines.append("<end_of_portfolio_constraints>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct_of(value: float, total: float) -> float:
    return (value / total * 100.0) if total else 0.0
