"""
ystocker.analyst
~~~~~~~~~~~~~~~~
Analyst estimate revisions for the peer-group universe.

Why this exists: /evaluation shows ``Upside (%)`` — one analyst target price
against the current one — and nothing about which *direction* the estimates are
moving. Revision direction is the more informative of the two: a stock trading
below a target nobody has updated in six months says less than one whose
next-year EPS estimate has been raised four times in the last month.

Yahoo returns that in ``eps_trend`` (the same estimate as it stood 7, 30, 60 and
90 days ago), ``eps_revisions`` (how many analysts moved up or down), and
``recommendations_summary`` (the buy/hold/sell split, and how it has shifted).

Cost and shape
--------------
This sweeps every ticker in ``PEER_GROUPS`` — 210 of them — so it is built like
the other expensive sweeps here, not like a request handler:

* Memory, then disk, then network; a day's TTL; a background thread does the
  fetching and ``peek()`` never does.
* ``data.TICKER_BACKOFF`` is honoured, so a delisted symbol is skipped rather
  than retried on every pass, and the same back-off the 8-hourly ``.info`` warm
  maintains is reused rather than kept twice.
* ``fetchguard.guard`` is checked between tickers, so a Yahoo 429 stops the
  sweep instead of turning into 200 more requests. A partial pass is saved: half
  the universe with revision data beats none of it.

Every field is optional. Yahoo covers estimates unevenly — ETFs have none at
all, and plenty of listed companies have one analyst — so a ticker with nothing
is simply absent from the payload rather than present with zeroes, which would
read as "no revisions" instead of "not covered".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ystocker import fetchguard

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent.parent / "cache" / "analyst_cache.json"
TTL_SECONDS = 24 * 3600

#: Bump when the payload shape changes, so a cache written by an older build is
#: refetched rather than served into code that expects something else. Every
#: cached module here carries one; etf_holdings shipped without it and broke
#: /multiples for a deploy.
CACHE_VER = "v2"   # v2: price_target gained the stale-target filter

#: Yahoo's ``eps_trend`` / ``eps_revisions`` period keys, and what they mean.
#: "+1y" — next fiscal year — is the one worth leading with: current-quarter
#: estimates barely move, and next-year is where analysts actually express a
#: change of mind.
PERIODS: list[tuple[str, str, str]] = [
    ("0q",  "current quarter", "本季度"),
    ("+1q", "next quarter",    "下季度"),
    ("0y",  "current year",    "本年度"),
    ("+1y", "next year",       "下一年度"),
]
LEAD_PERIOD = "+1y"

#: Above this, a price target is treated as stale data rather than a forecast.
#:
#: PARA came back with high == low == mean == median == 42.0 against a price of
#: 1.115 — one analyst's target, 37x the market, left un-updated through a
#: collapse — which computes to +3,666% upside. Printed in a table of
#: single-digit percentages that reads as a broken page, and it would be, because
#: it is not a forecast anyone holds.
#:
#: The bound is deliberately generous: SHEN's +121% has three distinct targets
#: behind it and is merely aggressive, so it survives. What is filtered is the
#: order-of-magnitude nonsense, not the optimism.
IMPLAUSIBLE_UPSIDE_PCT = 300.0

#: Pause between tickers. Not politeness — Yahoo rate-limits, and a 210-ticker
#: sweep that trips the breaker on ticker 30 leaves 180 without data. Slow and
#: complete beats fast and truncated for something that runs once a day.
SLEEP_BETWEEN = fetchguard.env_float("ANALYST_SLEEP_SECONDS", 0.35, 0.0)

_lock = threading.Lock()
_mem: dict[str, Any] = {}
_mem_at: float = 0.0


def _f(v: Any) -> Optional[float]:
    """A float, or None.

    Must not be ``isinstance(v, (int, float))``: pandas hands back numpy scalars,
    and ``np.float64`` subclasses ``float`` while ``np.int64`` does **not**
    subclass ``int``. That asymmetry is why the first version of this silently
    returned None for every integer column — ``eps_trend`` (float64) worked and
    ``eps_revisions`` (int64) came back empty, with no error anywhere.

    So convert instead of type-checking, and reject only the things float() would
    accept but shouldn't: bool (an int in Python), and strings.
    """
    if v is None or isinstance(v, (bool, str, bytes)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN appears in estimate columns, and json.dump would emit a literal NaN,
    # which is invalid JSON and poisons the disk cache on reload.
    return None if f != f else round(f, 4)


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    return None if f is None else int(f)


def _row(df: Any, period: str) -> dict:
    """One row of a period-indexed DataFrame as a plain dict."""
    try:
        if df is None or df.empty or period not in df.index:
            return {}
        return {str(k): v for k, v in df.loc[period].items()}
    except Exception:  # noqa: BLE001 - a missing row is not an error
        return {}


def _one(ticker: str) -> Optional[dict[str, Any]]:
    """Revision data for one ticker, or None if Yahoo covers it with nothing."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    out: dict[str, Any] = {"ticker": ticker}

    # eps_trend: the same estimate as it stood N days ago. The delta is the
    # signal, so compute it here rather than making every consumer subtract.
    trend: dict[str, Any] = {}
    try:
        df = t.eps_trend
        for key, _en, _zh in PERIODS:
            row = _row(df, key)
            cur, d30 = _f(row.get("current")), _f(row.get("30daysAgo"))
            if cur is None:
                continue
            trend[key] = {
                "current":   cur,
                "d7":        _f(row.get("7daysAgo")),
                "d30":       d30,
                "d60":       _f(row.get("60daysAgo")),
                "d90":       _f(row.get("90daysAgo")),
                # Percent change over 30 days. Guarded against a zero or
                # negative base: a company swinging from a loss to a profit makes
                # the percentage meaningless, so it is left None and the absolute
                # move is what gets shown.
                "chg30_pct": (round((cur - d30) / abs(d30) * 100, 2)
                              if d30 not in (None, 0) else None),
                "chg30_abs": (round(cur - d30, 4) if d30 is not None else None),
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("analyst: %s eps_trend failed: %s", ticker, exc)
    if trend:
        out["eps_trend"] = trend

    # eps_revisions: how many analysts moved, and which way.
    revisions: dict[str, Any] = {}
    try:
        df = t.eps_revisions
        for key, _en, _zh in PERIODS:
            row = _row(df, key)
            up7, dn7 = _i(row.get("upLast7days")), _i(row.get("downLast7Days"))
            up30, dn30 = _i(row.get("upLast30days")), _i(row.get("downLast30days"))
            if up30 is None and dn30 is None and up7 is None and dn7 is None:
                continue
            revisions[key] = {"up7": up7, "down7": dn7, "up30": up30, "down30": dn30,
                              "net30": (None if up30 is None and dn30 is None
                                        else (up30 or 0) - (dn30 or 0))}
    except Exception as exc:  # noqa: BLE001
        log.debug("analyst: %s eps_revisions failed: %s", ticker, exc)
    if revisions:
        out["eps_revisions"] = revisions

    # recommendations_summary: the buy/hold/sell split now and three months back,
    # so the *change* in stance is visible and not just the level.
    try:
        df = t.recommendations_summary
        if df is not None and not df.empty:
            recs = []
            for _idx, row in df.head(4).iterrows():
                recs.append({
                    "period":      str(row.get("period") or ""),
                    "strong_buy":  _i(row.get("strongBuy")),
                    "buy":         _i(row.get("buy")),
                    "hold":        _i(row.get("hold")),
                    "sell":        _i(row.get("sell")),
                    "strong_sell": _i(row.get("strongSell")),
                })
            if recs:
                out["recommendations"] = recs
    except Exception as exc:  # noqa: BLE001
        log.debug("analyst: %s recommendations_summary failed: %s", ticker, exc)

    # Price targets: the spread, not just the mean. /evaluation already shows an
    # upside computed from one number; high and low say how much agreement is
    # behind it.
    try:
        pt = t.analyst_price_targets or {}
        cur, mean = _f(pt.get("current")), _f(pt.get("mean"))
        high, low = _f(pt.get("high")), _f(pt.get("low"))
        if mean is not None:
            raw = (round((mean - cur) / cur * 100, 2)
                   if cur not in (None, 0) else None)
            # high == low means a single analyst, which is what makes a stale
            # target dangerous: nothing averages it away. Recorded either way so
            # the reason a cell is blank is visible in the payload rather than
            # being a mystery in the UI.
            single = (high is not None and low is not None and high == low)
            # One-sided on purpose. Upside is unbounded — a stale target can sit
            # any multiple above the price — whereas downside cannot pass -100%,
            # because that is a target of zero. abs() here would imply a symmetry
            # the arithmetic does not have.
            suspect = raw is not None and raw > IMPLAUSIBLE_UPSIDE_PCT
            block = {
                "current": cur, "mean": mean, "high": high, "low": low,
                "median": _f(pt.get("median")),
                "upside_pct": None if suspect else raw,
                "single_analyst": single,
            }
            if suspect:
                block["upside_pct_raw"] = raw
                block["upside_suspect"] = True
                log.debug("analyst: %s upside %.0f%% looks stale "
                          "(price %s, target %s, single=%s) — dropped",
                          ticker, raw, cur, mean, single)
            out["price_target"] = block
    except Exception as exc:  # noqa: BLE001
        log.debug("analyst: %s analyst_price_targets failed: %s", ticker, exc)

    # A ticker Yahoo covers with nothing returns None rather than a dict of
    # empties: absent reads as "not covered", zeroes read as "no revisions".
    return out if len(out) > 1 else None


def _fetch(universe: list[str]) -> dict[str, Any]:
    from ystocker.data import PROVIDER, TICKER_BACKOFF

    ready = TICKER_BACKOFF.filter_ready(universe)
    tickers: dict[str, Any] = {}
    attempted, covered, stopped = [], [], None

    for i, sym in enumerate(ready, 1):
        try:
            fetchguard.guard(PROVIDER)
        except fetchguard.CooldownActive as exc:
            # Stop rather than push 180 more requests at a provider that has just
            # said no. A partial pass is saved and the next one fills the rest.
            stopped = str(exc)
            log.warning("analyst: stopping sweep at %d/%d — %s", i, len(ready), exc)
            break
        attempted.append(sym)
        try:
            got = _one(sym)
            if got:
                tickers[sym] = got
                covered.append(sym)
        except Exception as exc:  # noqa: BLE001 - one bad ticker is not a bad sweep
            log.debug("analyst: %s failed: %s", sym, exc)
        if SLEEP_BETWEEN:
            time.sleep(SLEEP_BETWEEN)

    if attempted:
        TICKER_BACKOFF.record_batch(attempted, covered)

    if not tickers:
        raise RuntimeError(f"no analyst data for any of {len(ready)} tickers")

    payload: dict[str, Any] = {
        "tickers":    tickers,
        "ver":        CACHE_VER,
        "asof":       date.today().isoformat(),
        "fetched_at": time.time(),
        "universe":   len(universe),
        "attempted":  len(attempted),
        "covered":    len(tickers),
        "lead_period": LEAD_PERIOD,
    }
    if stopped:
        payload["stopped_early"] = stopped
    return payload


def _read_disk() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("tickers"):
            return None
        if data.get("ver") != CACHE_VER:
            log.info("analyst: cache is %s, want %s — refetching",
                     data.get("ver"), CACHE_VER)
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("analyst: unreadable cache (%s)", exc)
        return None


def _write_disk(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, allow_nan=False)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:  # noqa: BLE001 - a cache miss is not an error
        log.warning("analyst: could not write cache: %s", exc)


def _universe() -> list[str]:
    from ystocker import PEER_GROUPS
    return sorted({t for group in PEER_GROUPS.values() for t in group})


def get(force: bool = False) -> dict[str, Any]:
    """Revision data for the peer-group universe. Never raises.

    Slow on a cold cache — 200-odd tickers, paced — so only the background thread
    should call it. Request handlers use :func:`peek`.
    """
    global _mem, _mem_at
    now = time.time()
    with _lock:
        if not force and _mem and now - _mem_at < TTL_SECONDS:
            return _mem
        disk = _read_disk()
        if not force and disk and now - float(disk.get("fetched_at", 0)) < TTL_SECONDS:
            _mem, _mem_at = disk, now
            return disk

    try:
        fresh = _fetch(_universe())
    except Exception as exc:  # noqa: BLE001
        log.warning("analyst: sweep failed (%s); serving cache", exc)
        stale = _mem or _read_disk()
        if stale:
            out = dict(stale)
            out["stale"] = True
            return out
        return {"tickers": {}, "asof": "", "error": str(exc)}

    with _lock:
        _mem, _mem_at = fresh, now
    _write_disk(fresh)
    log.info("analyst: refreshed — %d/%d covered of %d in universe%s",
             fresh["covered"], fresh["attempted"], fresh["universe"],
             " (stopped early)" if fresh.get("stopped_early") else "")
    return fresh


def peek() -> Optional[dict[str, Any]]:
    """An already-available payload, or None. Never fetches.

    Mirrors ``breadth.peek()``. The sweep behind ``get()`` takes minutes; a
    request must never be able to trigger it.
    """
    with _lock:
        if _mem:
            return _mem
    return _read_disk()


def start_background_thread() -> None:
    """Warm shortly after startup, then once a day."""
    def _loop() -> None:
        # Behind the ticker-cache warm: that one populates TICKER_BACKOFF, and
        # starting after it means known-dead symbols are skipped on the first pass
        # rather than discovered again here.
        time.sleep(180)
        while True:
            try:
                get()
            except Exception:   # pragma: no cover - defensive
                log.exception("analyst: refresh failed")
            time.sleep(TTL_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="analyst-revisions").start()
    log.info("analyst: background thread started (TTL %dh, %.2fs between tickers)",
             TTL_SECONDS // 3600, SLEEP_BETWEEN)
