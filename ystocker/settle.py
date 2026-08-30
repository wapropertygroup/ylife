"""
ystocker.settle
~~~~~~~~~~~~~~~
Filling in what happened after each ``/agents`` decision.

Split from :mod:`ystocker.decisions` on the line the rest of this repo uses: that
module is the store and the arithmetic and is testable with no network, this one is
the part that fetches. ``lookthrough`` / ``assets`` and ``exposure`` / ``agents``
are separated the same way, and the reason is the same — the arithmetic is what
must be pinned by tests, and it cannot be if reaching it requires Yahoo.

Two things this must not do
---------------------------
**Never run on a request path.** A settlement pass fetches one price history per
distinct ticker with unsettled rows, which on a busy week is tens of calls at
``fetch_group``'s spacing. ``CLAUDE.md`` records what that does under gunicorn's
``--timeout 120``.

**Never fabricate a return.** A horizon whose sessions have not happened stays
absent. The pull to fill it with zero, or with the last available close, is exactly
what would make the ledger's own numbers untrustworthy — and a ledger nobody trusts
is worse than no ledger, because it gets quoted.

Runs once a day. Returns only change when a session closes, so a tighter loop would
re-fetch the same history to write the same rows; the ~84 calendar days a 60-session
horizon spans means a row stays in the queue for about three months either way.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ystocker import decisions

log = logging.getLogger(__name__)

#: One pass a day. See the module docstring.
INTERVAL_SECONDS = float(os.environ.get("AGENT_SETTLE_INTERVAL_SECONDS",
                                        str(24 * 3600)))

#: Enough history to cover the longest horizon plus the lookback window
#: ``decisions.unsettled`` walks. "1y" is the cheapest period that always does.
_PERIOD = "1y"

#: Cool-down to ask ``fetchguard`` for after a failed history call. Short, because
#: a settlement pass is daily and a long window would push a row past its horizon
#: for no reason; the breaker extends rather than shortens an existing window, so a
#: real rate limit already in force is unaffected.
_COOLDOWN_SECONDS = 60.0

#: Spacing between tickers, matching ``data.fetch_group``. A settlement pass is not
#: urgent and Yahoo's rate limit is the binding constraint on every other fetcher
#: in this app.
_SPACING_SECONDS = 0.5


def _closes(ticker: str) -> dict[str, float]:
    """Daily closes for *ticker* as ``{YYYY-MM-DD: close}``, or ``{}``.

    Goes through the same guarded yfinance path as everything else here: the
    provider circuit breaker in ``fetchguard`` is per-provider precisely so a
    settlement pass that trips Yahoo cannot stall the dashboards.
    """
    from ystocker import fetchguard

    try:
        import yfinance as yf

        # guard() *raises* CooldownActive rather than returning a boolean. Treating
        # it as a predicate would invert the check and hammer a provider that had
        # just rate-limited us.
        fetchguard.guard("yahoo")
        hist = yf.Ticker(ticker).history(period=_PERIOD, interval="1d")
    except fetchguard.CooldownActive as exc:
        log.info("settle: Yahoo cooling down, deferring %s (%s)", ticker, exc)
        return {}
    except Exception as exc:  # noqa: BLE001 - one bad ticker must not end the pass
        log.warning("settle: history failed for %s: %s", ticker, exc)
        fetchguard.trip("yahoo", _COOLDOWN_SECONDS, f"settle {ticker}: {exc}")
        return {}

    out: dict[str, float] = {}
    try:
        for stamp, close in hist["Close"].items():
            if close is None:
                continue
            value = float(close)
            # A zero or negative close is a vendor artefact, not a price. Dropping
            # it leaves the session absent, which reads as "not settled yet" —
            # the honest outcome — rather than producing a -100% return.
            if value > 0:
                out[stamp.date().isoformat()] = value
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("settle: unparseable history for %s: %s", ticker, exc)
        return {}
    return out


def run_once(before: Optional[str] = None) -> dict[str, int]:
    """One settlement pass. Returns a small summary for the log.

    Groups by ticker so a day with eight runs on the same name costs one fetch, and
    fetches the benchmark once for the whole pass rather than once per row.
    """
    rows = decisions.unsettled(before=before)
    if not rows:
        return {"rows": 0, "tickers": 0, "settled": 0, "completed": 0}

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            by_ticker.setdefault(ticker, []).append(row)

    bench = _closes(decisions.BENCHMARK)
    if not bench:
        # Not fatal: the instrument's own returns are still worth recording, and a
        # benchmark column that fills in on a later pass is exactly what
        # ``settle_row``'s per-horizon idempotence is for.
        log.info("settle: no benchmark history this pass; recording raw returns only")

    settled = completed = 0
    for i, (ticker, group) in enumerate(sorted(by_ticker.items())):
        if i:
            time.sleep(_SPACING_SECONDS)
        closes = _closes(ticker)
        if not closes:
            continue
        for row in group:
            try:
                updated = decisions.settle_row(row, closes, bench)
            except Exception as exc:  # noqa: BLE001
                log.warning("settle: %s/%s failed: %s", row.get("date"),
                            row.get("job_id"), exc)
                continue
            if updated == dict(row):
                continue                    # nothing matured since the last pass
            decisions.save_settled(updated)
            settled += 1
            if decisions.is_fully_settled(updated):
                completed += 1

    summary = {"rows": len(rows), "tickers": len(by_ticker),
               "settled": settled, "completed": completed}
    log.info("settle: %d unsettled rows across %d tickers — %d updated, "
             "%d now complete", summary["rows"], summary["tickers"],
             summary["settled"], summary["completed"])
    return summary


def _loop() -> None:
    # A short delay before the first pass so process start is not also a burst of
    # Yahoo calls competing with the cache warmer, which runs at import.
    time.sleep(120)
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001 - a daemon that dies stops settling forever
            log.exception("settle: pass failed")
        time.sleep(INTERVAL_SECONDS)


def start() -> None:
    """Start the daily settlement thread. Daemon, like every other worker here."""
    thread = threading.Thread(target=_loop, daemon=True, name="agent-settle")
    thread.start()
    log.info("Decision-ledger settlement started (every %.1fh, horizons %s sessions)",
             INTERVAL_SECONDS / 3600.0,
             "/".join(str(h) for h in decisions.HORIZONS))
