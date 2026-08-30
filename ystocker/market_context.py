"""
ystocker.market_context
~~~~~~~~~~~~~~~~~~~~~~~~
Renders a short, English-only "market regime" text block for injection into a
TradingAgents prompt — the same role ``exposure.render_block`` plays for
portfolio constraints, but for market-wide context instead of the holder's
own positions.

Why this exists
----------------
``/agents`` analyses one ticker at a time with no sense of the market it sits
in. Four dashboards already compute exactly the ingredients a market-regime
read needs — index valuation percentile (``valuation.py``), participation
(``breadth.py``), systematic positioning (``cta.py``), and policy-rate odds
(``fedwatch.py``) — and all four already expose a never-fetching ``peek()``
(or, for ``cta``, an equally cheap synchronous read). None of it reaches
``/agents`` today. This module is the rendering half; ``agents.py`` supplies
the orchestration (calling the four ``peek()``s) the same way
``build_portfolio_context`` calls ``lookthrough``/``exposure``.

Design rules, copied from ``exposure.render_block``
----------------------------------------------------
* English only, deliberately: this is model *input*, not user-visible text.
  TradingAgents controls output language separately via its own language
  instruction, so localising this would add a translation-parity burden for a
  string no human reads.
* Missing or unreadable input is omitted, never fabricated. A cold cache on a
  freshly provisioned box must render fewer bullet points, not an invented
  reading standing in for one that failed to load.
* Every number quoted here was computed by the source module, not derived
  here — this function formats, it does not calculate.
"""
from __future__ import annotations

from typing import Any, Optional

#: Appended once, not per bullet: the one instruction that keeps this from
#: being read as a signal about the specific ticker under analysis.
_TRAILER = (
    "These figures describe the broad market, not this specific company. "
    "Weigh them as context for risk appetite and positioning, not as a "
    "signal about this ticker."
)


def _valuation_lines(valuation: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(valuation, dict):
        return []
    headline = valuation.get("headline")
    if not isinstance(headline, dict):
        return []
    lines: list[str] = []

    pe = headline.get("spx_trailing_pe")
    pe = pe if isinstance(pe, dict) else {}
    pct = headline.get("spx_pe_percentile")
    pct = pct if isinstance(pct, dict) else {}
    # Independent, not one gated on the other: today's valuation.py always sets
    # both together, but nothing here should assume that stays true forever.
    if pe.get("value") is not None or pct.get("value") is not None:
        bits = []
        if pe.get("value") is not None:
            bits.append(f"S&P 500 trailing P/E: {pe['value']}x")
        if pct.get("value") is not None:
            bits.append(f"{pct['value']}th percentile of history "
                        f"since {pct.get('since', 'unknown')}")
        line = " — ".join(bits)
        as_of = pe.get("as_of") or pct.get("as_of")
        if as_of:
            line += f", as of {as_of}"
        lines.append(line)

    forward_bits = []
    for key, block in headline.items():
        if not key.endswith("_forward_pe") or not isinstance(block, dict):
            continue
        value = block.get("value")
        if value is None:
            continue
        etf = key[: -len("_forward_pe")].upper()
        bit = f"{etf} {value}x"
        if block.get("coverage_pct") is not None:
            bit += f" (coverage {block['coverage_pct']}%)"
        forward_bits.append(bit)
    if forward_bits:
        lines.append("Forward P/E (computed bottom-up from constituents): "
                     + ", ".join(sorted(forward_bits)) + ".")

    return lines


def _breadth_lines(breadth: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(breadth, dict):
        return []
    pct_above_ma = breadth.get("pct_above_ma")
    if not isinstance(pct_above_ma, dict) or not pct_above_ma:
        return []
    bits = []
    for period in sorted(pct_above_ma, key=lambda p: (len(p), p)):
        series = pct_above_ma.get(period)
        if not isinstance(series, dict):
            continue
        values = series.get("values")
        if not values:
            continue
        try:
            latest = values[-1]
        except (IndexError, TypeError):
            continue
        if latest is None:
            continue
        bits.append(f"{latest}% above its {period}-day average")
    if not bits:
        return []
    line = "Market breadth (S&P 500): " + ", ".join(bits)
    asof = breadth.get("asof")
    if asof:
        line += f", as of {asof}"
    if breadth.get("stale"):
        line += " (stale — serving a committed baseline, not a fresh read)"
    return [line + "."]


def _cta_lines(cta: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(cta, dict):
        return []
    latest = cta.get("latest")
    if not isinstance(latest, dict) or not latest:
        return []
    bits = []
    triggers = latest.get("spx_triggers")
    if isinstance(triggers, dict) and triggers:
        pieces = ", ".join(f"{label} {value}" for label, value in
                            sorted(triggers.items()) if value is not None)
        if pieces:
            bits.append(f"systematic-trend S&P trigger levels: {pieces}")
    flows = latest.get("flows_1w_global_bn")
    if isinstance(flows, dict) and flows:
        pieces = ", ".join(f"{label} ${value}bn" for label, value in flows.items()
                            if value is not None)
        if pieces:
            bits.append(f"projected 1-week global flow by scenario: {pieces}")
    if not bits:
        return []
    line = "CTA positioning (Goldman, via public reporting; not a live feed): " \
        + "; ".join(bits)
    if latest.get("report_date"):
        line += f", as of {latest['report_date']}"
    return [line + "."]


def _fedwatch_lines(fedwatch: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(fedwatch, dict):
        return []
    current = fedwatch.get("current")
    meetings = fedwatch.get("meetings")
    if not isinstance(current, dict) or not current.get("label"):
        return []
    line = f"Fed funds target range: {current['label']}%"
    if fedwatch.get("as_of"):
        line += f" (curve as of {fedwatch['as_of']})"
    if isinstance(meetings, list) and meetings:
        nxt = meetings[0]
        if isinstance(nxt, dict) and nxt.get("label"):
            odds = ", ".join(
                f"{name} {nxt[key]}%" for key, name in
                (("cut_prob", "cut"), ("hold_prob", "hold"), ("hike_prob", "hike"))
                if nxt.get(key) is not None
            )
            if odds:
                line += f". Market-implied odds for {nxt['label']}: {odds}"
    return [line + "."]


def render_market_block(
    valuation: Optional[dict[str, Any]] = None,
    breadth: Optional[dict[str, Any]] = None,
    cta: Optional[dict[str, Any]] = None,
    fedwatch: Optional[dict[str, Any]] = None,
) -> str:
    """A market-regime text block, or "" when nothing is available.

    Every bullet is independent: a cold cache for one source drops one line
    rather than the whole block, and a completely cold set of caches (a
    freshly provisioned box, or every source mid-fetch) returns "" so the
    caller writes nothing rather than an empty heading.
    """
    lines: list[str] = []
    lines.extend(_valuation_lines(valuation))
    lines.extend(_breadth_lines(breadth))
    lines.extend(_fedwatch_lines(fedwatch))
    lines.extend(_cta_lines(cta))
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return f"{body}\n\n{_TRAILER}"
