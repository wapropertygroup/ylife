"""
ystocker.relative_strength
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Renders a short, English-only peer-comparison text block for injection into a
TradingAgents prompt — "how does this ticker's analyst-estimate momentum
compare to its peer group" — from data ``analyst.py`` already sweeps daily
for the ``/evaluation`` page and exposes via a never-fetching ``peek()``.

Why this exists
----------------
A standalone "is NVDA good" read says nothing about whether capital is better
placed in NVDA, AVGO or TSM right now. ``analyst.py`` already answers the
input half of that question for 210 peer-grouped tickers — EPS-estimate
revision direction, analyst recommendation shifts, price-target spread — and
none of it reaches ``/agents`` today. This module is the rendering half;
``agents.py::build_relative_strength_context`` supplies the orchestration
(resolving the ticker's peer group, calling ``analyst.peek()``) the same way
``build_portfolio_context`` orchestrates ``lookthrough``/``exposure``.

Peer selection
--------------
A ticker can sit in several ``PEER_GROUPS`` (NVDA is in "Tech",
"Semiconductors" and "AI / Robotics"). The *smallest* matching group is used,
on the theory that a smaller, named group is a more specific peer set than a
broad sector bucket — "Semiconductors" (30 names) is a better comparison set
for NVDA than "Tech" (12 names spanning retailers to streaming). Peers are
taken in the group's own listed order, which is already roughly
relevance-ordered in this codebase's ``PEER_GROUPS`` (see its comments), and
filtered down to whichever are actually covered by Yahoo's estimates —
capped at ``max_peers``, never padded.

Design rules, copied from ``exposure.render_block`` / ``market_context``
--------------------------------------------------------------------------
* English only: model input, not user-visible text.
* A ticker outside every peer group, not covered by Yahoo's estimates, or
  with zero *covered* peers produces "" — never a table with one row, which
  would not be a comparison.
* Every figure quoted is Yahoo's own per-name aggregate, copied verbatim —
  this function formats, it does not calculate or rank.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ystocker.analyst import LEAD_PERIOD

#: How many peers to show at most. Chosen to keep the table short enough for
#: a prompt block, not as a claim about how many names are "real" peers.
MAX_PEERS = 5

_TRAILER = (
    "Figures are Yahoo's own analyst-estimate aggregates for each name "
    "individually, not a look-through and not a ranking judgment. A ticker "
    "absent from this table is not covered by Yahoo's analyst estimates, "
    "which is not evidence of weak or flat revisions."
)


def _peer_candidates(ticker: str, peer_groups: Mapping[str, Sequence[str]]) -> list[str]:
    """Every other ticker in the smallest ``PEER_GROUPS`` entry containing it."""
    matches = [group for group in peer_groups.values() if ticker in group]
    if not matches:
        return []
    smallest = min(matches, key=len)
    return [t for t in smallest if t != ticker]


def _eps_revision_cell(row: Mapping[str, Any]) -> str:
    trend = row.get("eps_trend")
    revisions = row.get("eps_revisions")
    bits = []
    if isinstance(trend, dict):
        leg = trend.get(LEAD_PERIOD)
        if isinstance(leg, dict) and leg.get("chg30_pct") is not None:
            bits.append(f"{leg['chg30_pct']:+.1f}% est. Δ30d")
    if isinstance(revisions, dict):
        leg = revisions.get(LEAD_PERIOD)
        if isinstance(leg, dict) and leg.get("net30") is not None:
            up, down = leg.get("up30"), leg.get("down30")
            detail = (f" ({up} up / {down} down)"
                       if up is not None and down is not None else "")
            bits.append(f"net {leg['net30']:+d} revisions{detail}")
    return "; ".join(bits) if bits else "—"


def _recs_shift_cell(row: Mapping[str, Any]) -> str:
    recs = row.get("recommendations")
    if not isinstance(recs, list) or not recs:
        return "—"

    def _bucket(entry: Mapping[str, Any]) -> Optional[tuple[int, int, int]]:
        buy = (entry.get("strong_buy") or 0) + (entry.get("buy") or 0)
        hold = entry.get("hold") or 0
        sell = (entry.get("strong_sell") or 0) + (entry.get("sell") or 0)
        if buy == 0 and hold == 0 and sell == 0:
            return None
        return buy, hold, sell

    now = _bucket(recs[0]) if isinstance(recs[0], dict) else None
    if now is None:
        return "—"
    if len(recs) == 1 or not isinstance(recs[-1], dict):
        return f"Buy {now[0]} / Hold {now[1]} / Sell {now[2]} (no prior period)"
    prior = _bucket(recs[-1])
    if prior is None:
        return f"Buy {now[0]} / Hold {now[1]} / Sell {now[2]} (no prior period)"
    return (f"Buy {prior[0]}→{now[0]}, Hold {prior[1]}→{now[1]}, "
            f"Sell {prior[2]}→{now[2]}")


def _upside_cell(row: Mapping[str, Any]) -> str:
    target = row.get("price_target")
    if not isinstance(target, dict):
        return "—"
    if target.get("upside_suspect"):
        return "n/a (stale target filtered)"
    upside = target.get("upside_pct")
    if upside is None:
        return "—"
    return f"{upside:+.1f}%"


def render_relative_strength_block(
    ticker: str,
    universe_payload: Optional[Mapping[str, Any]],
    peer_groups: Mapping[str, Sequence[str]],
    max_peers: int = MAX_PEERS,
) -> str:
    """A peer-comparison text block for ``ticker``, or "" when not possible."""
    if not ticker or not isinstance(universe_payload, Mapping):
        return ""
    tickers = universe_payload.get("tickers")
    if not isinstance(tickers, Mapping) or ticker not in tickers:
        return ""

    candidates = _peer_candidates(ticker, peer_groups)
    if not candidates:
        return ""
    peers = [t for t in candidates if t in tickers][:max_peers]
    if not peers:
        return ""

    group_name = min(
        (name for name, members in peer_groups.items() if ticker in members),
        key=lambda name: len(peer_groups[name]),
    )

    rows = [ticker] + peers
    header = ("| Ticker | Next-FY EPS estimate | Recs shift (now vs. prior) "
               "| Price-target upside |")
    sep = "|---|---|---|---|"
    lines = [header, sep]
    for sym in rows:
        row = tickers.get(sym) or {}
        lines.append(
            f"| {sym} | {_eps_revision_cell(row)} | {_recs_shift_cell(row)} "
            f"| {_upside_cell(row)} |"
        )

    asof = universe_payload.get("asof")
    preamble = f'Peer group "{group_name}"'
    if asof:
        preamble += f" (Yahoo analyst-estimate data, as of {asof})"
    preamble += ":"

    return preamble + "\n" + "\n".join(lines) + "\n\n" + _TRAILER
