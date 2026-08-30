"""
ystocker.lookthrough
~~~~~~~~~~~~~~~~~~~~
穿透 — resolving a portfolio's funds to the companies actually inside them.

Why this exists
---------------
A holder of VOO, QQQ, VGT and a little AAPL owns four lines and believes they are
diversified. They are not: all four contain AAPL, and the position sizes do not
add up the way the account screen implies. The number that matters — *how much
NVDA do I actually own* — is not on any brokerage statement, because each fund
reports only itself. Computing it means walking each holding down to the
securities underneath, which is what 穿透 (look-through) means.

The recursion is not optional. A target-date or allocation fund holds *other
funds*: VTTSX's largest holding is VSMPX at 54.15%, which is itself a fund whose
largest holding is NVDA at 6.40%. One level of look-through on VTTSX reports a
54% position in something that is not a company. So this walks until it reaches
things that are not funds, with a depth cap and cycle detection.

What is deliberately NOT done: extrapolation
--------------------------------------------
Yahoo discloses a fund's **top ten holdings only**. That is 37.6% of VOO by
weight, 46.3% of QQQ, and 13.0% of VXUS. The tempting move is to assume the
invisible 62% of VOO resembles the visible 38% and gross every weight up by
1/0.376. This module does not do that, for the same reason ``brief.py`` states a
cold source instead of dropping it: a fabricated number is indistinguishable from
a measured one to whoever reads the page, and here it would be fabricated at the
exact moment the reader is making a concentration decision.

So every figure produced here is a **floor**, not an estimate. "At least 6.2%
NVDA" is what the data supports. Conveniently the floor is also the more useful
quantity for the actual question — concentration risk is a "have I got more than
I think" problem, and a lower bound answers it without inventing anything.

The residual is therefore first-class output, not a rounding error, and the
partition below is closed by construction so it can never quietly fail to add up:

    seen  +  undisclosed_equity  +  non_equity  +  unclassified  ==  position value

Held together by:

* ``visible`` is the summed weight of the disclosed holdings, and
  ``stock`` is the fund's own reported equity fraction. ``undisclosed_equity`` is
  ``max(0, stock - visible)`` and ``non_equity`` is ``1 - max(visible, stock)``.
  Written that way the three shares sum to exactly 1 under either ordering of
  ``visible`` and ``stock`` — see :func:`_partition`, which the tests pin. The
  naive ``1 - visible`` for the residual double-counts a bond sleeve as
  "equity we cannot see", which on BND would have reported the entire fund as
  hidden stock.
* A fund with no asset-class data at all lands in ``unclassified`` rather than
  being assumed to be equity. BND returns zero holdings and zero sectors but does
  report asset classes; a fund reporting neither exists and must not be guessed
  at.
* A child symbol that does not resolve becomes a **named leaf** flagged
  ``unresolved``, never a discard. XTSLA (a BlackRock cash sweep inside AOR) 404s
  at Yahoo, and dropping it would silently shrink the portfolio total — the one
  error that makes every percentage on the page wrong at once.
* ``pending`` is distinct from ``unresolved``. The resolver is a ``peek``-style
  callable that may decline to hit the network (see ``funddata.peek``), and
  "nobody has fetched this yet" must not render as "this does not exist". The
  result carries :attr:`Result.pending_symbols` so the caller can warm them and
  recompute.

Pure and network-free: the fund resolver is injected. That is what lets
``tests/test_lookthrough.py`` pin the arithmetic — including the summation
invariant above — with no app, no cache and no Yahoo.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

#: How many times to look through a fund into another fund. 0 would disable
#: look-through entirely; 1 resolves a plain ETF to companies; 2 is the shallowest
#: value that reaches a company through a fund-of-funds (VTTSX -> VSMPX -> NVDA).
#: 3 is the default so a two-layer wrapper still lands on real names, which is one
#: more level than any retail product observed here actually uses.
DEFAULT_MAX_DEPTH = 3

#: Ceiling on nodes expanded across one whole analysis. A fund may disclose 25
#: holdings and the depth cap is 3, so a single position can expand to ~15k nodes
#: and a 100-line portfolio to over a million — enough to put a request-path
#: computation into the seconds, which on this box is how a worker gets SIGKILLed.
#: Hitting the budget stops *expansion* and marks the rest ``truncated``, which
#: keeps the partition closed and reports the shortfall rather than silently
#: producing a partial answer that looks complete.
DEFAULT_MAX_NODES = 200_000

#: Weights that sum to marginally over 1.0 are normal — Yahoo rounds each holding
#: to two decimals, and eleven sector weights on QQQ summed to 100.01%. Anything
#: past this is a vendor bug rather than rounding, and is clamped with a warning
#: rather than allowed to manufacture dollars the portfolio does not contain.
_WEIGHT_SUM_TOLERANCE = 1.02

#: Leaf kinds. ``equity`` is the only one that counts toward coverage: the other
#: three are all "we stopped here", and folding them into the coverage figure
#: would overstate exactly the number this module exists to keep honest.
LEAF_EQUITY = "equity"
LEAF_UNRESOLVED = "unresolved"
LEAF_TRUNCATED = "truncated"
LEAF_PENDING = "pending"

_COUNTS_AS_SEEN = frozenset({LEAF_EQUITY})


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """One line as the user holds it, valued in USD.

    ``value`` is the market value of the line, not a weight: the engine works in
    dollars throughout and converts to percentages only at the end. Weights are
    unit-free fractions of a fund, so no currency conversion enters the recursion
    — only ``value`` needs to already be USD, which is the caller's job.
    """
    symbol: str
    value: float
    name: str = ""
    account: str = ""


@dataclass
class _Leaf:
    """A dollar amount that landed on something we stopped walking."""
    symbol: str
    name: str
    kind: str
    value: float
    root: str
    chain: tuple[str, ...]


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

@dataclass
class Exposure:
    """Aggregate true exposure to one underlying symbol, across every route."""
    symbol: str
    name: str
    kind: str
    value: float = 0.0
    direct_value: float = 0.0
    indirect_value: float = 0.0
    #: One entry per contributing route: {"root", "chain", "value"}.
    routes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def route_count(self) -> int:
        """Distinct top-level positions that contribute to this exposure.

        This is the "you hold NVDA 4 ways" number, so it counts *roots* rather
        than routes: two sleeves of the same fund-of-funds both leading to NVDA
        is one holding a reader can act on, not two.
        """
        return len({r["root"] for r in self.routes})

    def as_dict(self, total: float) -> dict[str, Any]:
        pct = (self.value / total * 100) if total else 0.0
        direct_pct = (self.direct_value / total * 100) if total else 0.0
        return {
            "symbol": self.symbol,
            "name": self.name or self.symbol,
            "kind": self.kind,
            "value": round(self.value, 2),
            "pct": round(pct, 4),
            "direct_value": round(self.direct_value, 2),
            "direct_pct": round(direct_pct, 4),
            "indirect_value": round(self.indirect_value, 2),
            "indirect_pct": round(pct - direct_pct, 4),
            "route_count": self.route_count,
            "routes": [
                {"root": r["root"],
                 "chain": list(r["chain"]),
                 "value": round(r["value"], 2),
                 "pct": round((r["value"] / total * 100) if total else 0.0, 4)}
                for r in sorted(self.routes, key=lambda r: -r["value"])
            ],
        }


@dataclass
class Residual:
    """The part of the portfolio 穿透 could not name, by reason.

    Kept as four separate buckets rather than one "other" because they mean
    completely different things to a reader: ``undisclosed_equity`` is
    concentration risk that might be hiding, whereas ``non_equity`` is bonds and
    cash that are *known* not to be companies. Collapsing them would imply the
    bond sleeve of a balanced fund might secretly be more NVDA.
    """
    undisclosed_equity: float = 0.0
    non_equity: float = 0.0
    unclassified: float = 0.0
    unresolved: float = 0.0
    truncated: float = 0.0
    pending: float = 0.0

    def total(self) -> float:
        return (self.undisclosed_equity + self.non_equity + self.unclassified
                + self.unresolved + self.truncated + self.pending)

    def as_dict(self, total: float) -> dict[str, Any]:
        def pair(v: float) -> dict[str, float]:
            return {"value": round(v, 2),
                    "pct": round((v / total * 100) if total else 0.0, 4)}
        return {
            "undisclosed_equity": pair(self.undisclosed_equity),
            "non_equity": pair(self.non_equity),
            "unclassified": pair(self.unclassified),
            "unresolved": pair(self.unresolved),
            "truncated": pair(self.truncated),
            "pending": pair(self.pending),
            "total": pair(self.total()),
        }


@dataclass
class Pocket:
    """One fund's unnamed dollars, plus what its disclosure bounds them to.

    Exists so a caller can put a *ceiling* on one company's exposure without
    estimating anything. The portfolio-level residual only says "these dollars
    could be any company"; a top-ten disclosure says considerably more, because it
    is ordered by weight:

    * A **disclosed** name's weight in this fund is exact — it cannot also be
      hiding in the undisclosed tail, because the tail is by construction the
      holdings *outside* the top ten. So a disclosed name draws nothing from here.
    * An **undisclosed** name's weight is at most :attr:`min_disclosed_weight`.
      Anything heavier would have made the disclosed set.

    Both deductions need the tail to consist of holdings each listed once, which
    fails if the tail contains *funds* — a company can hide inside several of them
    at once. :attr:`flat` records whether that is safe here, and a caller must fall
    back to the whole of :attr:`hidden_value` when it is False.

    Not included in :meth:`Result.as_dict`: this is in-process metadata for a
    constraint check, and a fund-by-fund breakdown would multiply the size of a
    payload the client has no use for.
    """
    #: The fund these dollars sit inside.
    fund: str
    #: Dollars allocated to this fund node. A fund held twice yields two pockets.
    fund_value: float
    #: Dollars inside it that the walk could not name.
    hidden_value: float
    #: Smallest disclosed weight as a 0..1 fraction, or None if it disclosed none.
    min_disclosed_weight: Optional[float]
    #: Symbols this fund disclosed, upper-cased.
    disclosed: tuple[str, ...] = ()
    #: Which residual bucket :attr:`hidden_value` was added to.
    bucket: str = ""
    #: True when no disclosed holding of this fund is itself a fund. Set after the
    #: walk completes, since it depends on what the children turned out to be.
    flat: bool = True

    def ceiling_for_any(self, symbols: Iterable[str]) -> float:
        """The most that *symbols*, taken together, could be hiding here.

        Several undisclosed names can each be up to
        :attr:`min_disclosed_weight`, so a group's allowance scales with how many
        of its members are undisclosed — but never past :attr:`hidden_value`,
        which is all the dollars there are.
        """
        wanted = {str(s).upper().strip() for s in symbols if str(s).strip()}
        if not wanted:
            return 0.0
        if not self.flat:
            # A wrapper's tail may hold funds, each of which could hold the name.
            return self.hidden_value
        undisclosed = wanted - set(self.disclosed)
        if not undisclosed:
            return 0.0
        if self.min_disclosed_weight is None:
            return self.hidden_value
        allowance = self.fund_value * float(self.min_disclosed_weight) * len(undisclosed)
        return min(self.hidden_value, allowance)

    def ceiling_for(self, symbol: str) -> float:
        """The most of *symbol* that could be hiding in this pocket, in dollars."""
        return self.ceiling_for_any((symbol,))


@dataclass
class Result:
    total_value: float
    exposures: list[Exposure]
    residual: Residual
    per_position: list[dict[str, Any]]
    pending_symbols: list[str]
    notes: list[str]
    #: Per-fund unnamed dollars with their disclosure bounds. See :class:`Pocket`.
    #: Sums to ``residual.undisclosed_equity + residual.unclassified``.
    pockets: list[Pocket] = field(default_factory=list)

    @property
    def seen_value(self) -> float:
        return sum(e.value for e in self.exposures if e.kind in _COUNTS_AS_SEEN)

    @property
    def coverage_pct(self) -> float:
        """Share of the portfolio resolved all the way to a named company.

        The honest headline caveat. Everything else on the page is a floor scaled
        by this: a 40% coverage means a reported 6% NVDA position is consistent
        with anything from 6% to a great deal more.
        """
        return (self.seen_value / self.total_value * 100) if self.total_value else 0.0

    def as_dict(self, top: Optional[int] = None) -> dict[str, Any]:
        named = [e for e in self.exposures if e.kind in _COUNTS_AS_SEEN]
        other = [e for e in self.exposures if e.kind not in _COUNTS_AS_SEEN]
        shown = named[:top] if top else named
        rows = [e.as_dict(self.total_value) for e in shown]
        return {
            "total_value": round(self.total_value, 2),
            "seen_value": round(self.seen_value, 2),
            "coverage_pct": round(self.coverage_pct, 2),
            "exposures": rows,
            "exposure_count": len(named),
            "unnamed": [e.as_dict(self.total_value) for e in other],
            "residual": self.residual.as_dict(self.total_value),
            "per_position": self.per_position,
            "overlaps": [e.as_dict(self.total_value) for e in self.overlaps()],
            "pending_symbols": list(self.pending_symbols),
            "notes": list(self.notes),
        }

    def overlaps(self, min_routes: int = 2) -> list[Exposure]:
        """Exposures reached through more than one holding — the point of 穿透.

        Sorted by value: the reader's question is "what am I most concentrated in
        without realising", and that is the top of this list.
        """
        return [e for e in self.exposures
                if e.kind in _COUNTS_AS_SEEN and e.route_count >= min_routes]


# ---------------------------------------------------------------------------
# The partition — kept separate because it is the one piece of arithmetic that
# must be exactly closed, and the tests assert it directly.
# ---------------------------------------------------------------------------

def _partition(visible: float, stock: Optional[float]) -> tuple[float, float, float]:
    """Split a fund into (visible, undisclosed_equity, non_equity) shares.

    All three are fractions of the fund and sum to exactly 1.0.

    ``visible`` is the summed weight of the disclosed holdings; ``stock`` is the
    fund's own reported equity fraction, or None when it reports none.

    The ordering of the two inputs is not guaranteed and both cases are real:
    a fund may disclose holdings summing to less than its equity sleeve (VOO:
    visible 0.376, stock 0.999 — there is undisclosed equity), or to more than it
    (an allocation fund whose top ten include its bond sleeve, so the disclosed
    weight already spans non-equity). Hence the ``max``: whichever is larger has
    already accounted for everything below it.

    With ``stock`` None the caller must treat the whole residual as unclassified
    — signalled by returning 0.0 for both residual shares, which leaves
    ``1 - visible`` unaccounted for and is the caller's cue.
    """
    visible = max(0.0, min(1.0, visible))
    if stock is None:
        return visible, 0.0, 0.0
    stock = max(0.0, min(1.0, stock))
    undisclosed_equity = max(0.0, stock - visible)
    non_equity = 1.0 - max(visible, stock)
    return visible, undisclosed_equity, max(0.0, non_equity)


def _sum_weights(holdings: Iterable[dict[str, Any]]) -> float:
    total = 0.0
    for h in holdings:
        w = h.get("weight")
        if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0:
            total += float(w)
    return total


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

def analyse(positions: Iterable[Position],
            resolve: Callable[[str], Optional[dict[str, Any]]],
            max_depth: int = DEFAULT_MAX_DEPTH,
            max_nodes: int = DEFAULT_MAX_NODES) -> Result:
    """Look through *positions* to the securities underneath them.

    ``resolve(symbol)`` returns a composition dict, or None when the symbol is
    genuinely unresolvable. The dict is the shape ``funddata`` produces::

        {"symbol", "name", "kind": "fund"|"equity"|"cash"|"pending",
         "holdings": [{"symbol", "name", "weight"}],   # weight a 0..1 fraction
         "asset_classes": {"stock": 0.998, "bond": 0.0, ...}}

    Only ``kind == "fund"`` is walked into. ``kind == "pending"`` means the
    resolver has not fetched this symbol yet and is reported separately from
    unresolvable, because conflating the two turns a cold cache into a claim
    about the security.
    """
    leaves: list[_Leaf] = []
    residual = Residual()
    per_position: list[dict[str, Any]] = []
    pending: set[str] = set()
    notes: list[str] = []
    pockets: list[Pocket] = []
    #: Funds observed to disclose a holding that is itself a fund. Collected during
    #: the walk rather than probed separately: a child frame that finds itself to be
    #: a fund knows its parent is ``chain[-1]``, so this costs no extra resolver
    #: calls. Applied to the pockets afterwards, because a fund's flatness is not
    #: known until its children have been walked.
    wrapper_parents: set[str] = set()
    # A one-element list so the recursion can decrement a shared counter without
    # threading a return value through every frame.
    budget = [max(1, int(max_nodes))]

    total_value = 0.0
    for pos in positions:
        if not pos.symbol or not isinstance(pos.value, (int, float)):
            continue
        value = float(pos.value)
        if value <= 0:
            # A zero or short line has no meaningful look-through weight, and a
            # negative one would subtract from an underlying it does not offset.
            # Recorded in notes so it is visibly excluded rather than missing.
            if value < 0:
                notes.append(f"{pos.symbol}: negative value excluded from 穿透")
            continue
        total_value += value

        before = dataclasses.replace(residual)
        pos_leaves: list[_Leaf] = []
        _walk(symbol=pos.symbol.upper(), name=pos.name, dollars=value,
              root=pos.symbol.upper(), chain=(), depth=0, max_depth=max_depth,
              resolve=resolve, out=pos_leaves, residual=residual,
              pending=pending, notes=notes, budget=budget, pockets=pockets,
              wrapper_parents=wrapper_parents)
        leaves.extend(pos_leaves)

        seen = sum(l.value for l in pos_leaves if l.kind in _COUNTS_AS_SEEN)
        per_position.append({
            "symbol": pos.symbol.upper(),
            "name": pos.name or pos.symbol.upper(),
            "account": pos.account,
            "value": round(value, 2),
            "seen_value": round(seen, 2),
            "coverage_pct": round(seen / value * 100, 2) if value else 0.0,
            "leaf_count": len({l.symbol for l in pos_leaves
                               if l.kind in _COUNTS_AS_SEEN}),
            "residual": Residual(
                undisclosed_equity=residual.undisclosed_equity - before.undisclosed_equity,
                non_equity=residual.non_equity - before.non_equity,
                unclassified=residual.unclassified - before.unclassified,
                unresolved=sum(l.value for l in pos_leaves if l.kind == LEAF_UNRESOLVED),
                truncated=sum(l.value for l in pos_leaves if l.kind == LEAF_TRUNCATED),
                pending=sum(l.value for l in pos_leaves if l.kind == LEAF_PENDING),
            ).as_dict(value),
        })

    # Leaves that are not companies are still named, but they belong in the
    # residual totals too -- otherwise `seen + residual` would not reconstruct
    # the portfolio and the invariant the module promises would be false.
    for leaf in leaves:
        if leaf.kind == LEAF_UNRESOLVED:
            residual.unresolved += leaf.value
        elif leaf.kind == LEAF_TRUNCATED:
            residual.truncated += leaf.value
        elif leaf.kind == LEAF_PENDING:
            residual.pending += leaf.value

    exposures = _aggregate(leaves)
    # A fund's flatness depends on what its disclosed holdings turned out to be,
    # which is only known once the whole walk has finished.
    for pocket in pockets:
        if pocket.fund in wrapper_parents:
            pocket.flat = False
    return Result(total_value=total_value, exposures=exposures, residual=residual,
                  per_position=per_position,
                  pending_symbols=sorted(pending), notes=notes, pockets=pockets)


def _walk(*, symbol: str, name: str, dollars: float, root: str,
          chain: tuple[str, ...], depth: int, max_depth: int,
          resolve: Callable[[str], Optional[dict[str, Any]]],
          out: list[_Leaf], residual: Residual, pending: set[str],
          notes: list[str], budget: list[int],
          pockets: list[Pocket], wrapper_parents: set[str]) -> None:
    """Allocate *dollars* held via *symbol* down to leaves. Appends to *out*."""
    if dollars <= 0:
        return

    budget[0] -= 1
    if budget[0] <= 0:
        # Out of expansion budget. Stopping here keeps the partition closed and
        # names the shortfall, rather than returning a partial answer that reads
        # as complete.
        out.append(_Leaf(symbol, name or symbol, LEAF_TRUNCATED, dollars, root, chain))
        if not any("budget" in n for n in notes):
            notes.append("look-through budget reached — the largest holdings were "
                         "resolved and the rest are reported as unresolved")
        return

    # Cycle guard. A fund holding a fund that holds the first would otherwise
    # recurse until the depth cap, splitting the same dollars across a chain that
    # does not exist. Cheap to check and the failure it prevents is silent.
    if symbol in chain or (symbol == root and depth > 0):
        out.append(_Leaf(symbol, name or symbol, LEAF_TRUNCATED, dollars, root, chain))
        notes.append(f"{root}: cycle at {symbol} — stopped looking through")
        return

    node: Optional[dict[str, Any]]
    try:
        node = resolve(symbol)
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not void a portfolio
        log.warning("lookthrough: resolver failed for %s (%s)", symbol, exc)
        node = None

    if node is None:
        out.append(_Leaf(symbol, name or symbol, LEAF_UNRESOLVED, dollars, root, chain))
        return

    kind = (node.get("kind") or "").lower()
    node_name = node.get("name") or name or symbol

    if kind == LEAF_PENDING:
        pending.add(symbol)
        out.append(_Leaf(symbol, node_name, LEAF_PENDING, dollars, root, chain))
        return

    if kind != "fund":
        # A company, a cash sweep, a bond -- anything that is not a wrapper is
        # where the walk is supposed to stop.
        out.append(_Leaf(symbol, node_name, LEAF_EQUITY, dollars, root, chain))
        return

    # This node is a fund, so whichever fund disclosed it is a wrapper and its own
    # hidden tail may contain funds too -- which breaks the "a name appears once"
    # deduction :class:`Pocket` relies on. Recorded from here because the child is
    # the frame that knows, and this needs no second resolver call.
    if chain:
        wrapper_parents.add(chain[-1])

    if depth >= max_depth:
        out.append(_Leaf(symbol, node_name, LEAF_TRUNCATED, dollars, root, chain))
        notes.append(f"{root}: depth limit at {symbol} — not looked through further")
        return

    holdings = [h for h in (node.get("holdings") or [])
                if h.get("symbol") and isinstance(h.get("weight"), (int, float))
                and not isinstance(h.get("weight"), bool) and float(h["weight"]) > 0]
    visible = _sum_weights(holdings)

    if visible > _WEIGHT_SUM_TOLERANCE:
        # Do not let a vendor glitch mint dollars. Renormalise to 1.0 and say so.
        log.warning("lookthrough: %s holdings sum to %.3f — renormalising", symbol, visible)
        notes.append(f"{symbol}: disclosed weights summed to "
                     f"{visible * 100:.1f}% and were renormalised")
        holdings = [{**h, "weight": float(h["weight"]) / visible} for h in holdings]
        visible = 1.0
    visible = min(visible, 1.0)

    stock = _stock_fraction(node)
    _vis, undisclosed_equity, non_equity = _partition(visible, stock)

    if stock is None:
        # No asset-class data: the residual is real but its nature is unknown, and
        # assuming equity would invent hidden concentration.
        hidden = dollars * (1.0 - visible)
        residual.unclassified += hidden
        bucket = "unclassified"
    else:
        hidden = dollars * undisclosed_equity
        residual.undisclosed_equity += hidden
        residual.non_equity += dollars * non_equity
        bucket = "undisclosed_equity"

    if hidden > 0:
        # The disclosure metadata travels with the dollars it bounds. Weights are
        # read after any renormalisation above, so a vendor payload summing past
        # tolerance cannot inflate the per-name ceiling this supports.
        weights = [float(h["weight"]) for h in holdings]
        pockets.append(Pocket(
            fund=symbol, fund_value=dollars, hidden_value=hidden,
            min_disclosed_weight=(min(weights) if weights else None),
            disclosed=tuple(str(h["symbol"]).upper().strip() for h in holdings),
            bucket=bucket))

    for h in holdings:
        child = str(h["symbol"]).upper().strip()
        if not child:
            continue
        _walk(symbol=child, name=str(h.get("name") or ""),
              dollars=dollars * float(h["weight"]), root=root,
              chain=chain + (symbol,), depth=depth + 1, max_depth=max_depth,
              resolve=resolve, out=out, residual=residual, pending=pending,
              notes=notes, budget=budget, pockets=pockets,
              wrapper_parents=wrapper_parents)


def _stock_fraction(node: dict[str, Any]) -> Optional[float]:
    """The fund's equity share as a 0..1 fraction, or None if it reports none.

    None and 0.0 are different answers and must stay so: BND reports
    ``stockPosition: 0.0``, which is a measurement saying "no equities in here",
    while a fund with no asset-class block at all has told us nothing. The first
    makes the residual ``non_equity``; the second makes it ``unclassified``.
    """
    ac = node.get("asset_classes")
    if not isinstance(ac, dict) or not ac:
        return None
    raw = ac.get("stock")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _aggregate(leaves: Iterable[_Leaf]) -> list[Exposure]:
    """Merge leaves by symbol into per-symbol exposures, largest first."""
    by_symbol: dict[str, Exposure] = {}
    for leaf in leaves:
        exp = by_symbol.get(leaf.symbol)
        if exp is None:
            exp = Exposure(symbol=leaf.symbol, name=leaf.name or leaf.symbol,
                           kind=leaf.kind)
            by_symbol[leaf.symbol] = exp
        elif not exp.name or exp.name == exp.symbol:
            exp.name = leaf.name or exp.name
        # A symbol reached both directly and through a fund keeps the stronger
        # claim: `equity` wins over a truncation, because one route having been
        # walked to the end does identify the security.
        if leaf.kind in _COUNTS_AS_SEEN:
            exp.kind = LEAF_EQUITY
        exp.value += leaf.value
        if leaf.chain:
            exp.indirect_value += leaf.value
        else:
            exp.direct_value += leaf.value
        exp.routes.append({"root": leaf.root, "chain": leaf.chain, "value": leaf.value})

    return sorted(by_symbol.values(), key=lambda e: -e.value)
