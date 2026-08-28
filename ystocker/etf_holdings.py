"""
ystocker.etf_holdings
~~~~~~~~~~~~~~~~~~~~~
What is actually inside SPY and QQQ: top holdings, sector weights, asset mix.

Why this exists: /multiples reports a bottom-up forward P/E for both ETFs,
computed from constituent estimates at roughly 24% coverage by count. That number
is the page's headline and the coverage figure is the caveat printed beside it —
but neither says *what* the covered quarter is, or how concentrated the index has
become. Sector weights and a top-ten list answer both, and Yahoo returns them in
one call per ETF.

Deliberately its own module rather than part of ``valuation.py``, which owns
/multiples. Adding a key to that payload means bumping its ``_CACHE_VER``, which
invalidates the disk cache and triggers a rebuild of ~600 constituent lookups —
and until that finishes ``peek()`` refuses the old copy, so /multiples and the AI
brief's multiples section both go dark. Two Yahoo calls do not need to ride a
ten-minute rebuild. Same memory-then-disk-then-network shape as
``etf_returns.py``, so it behaves like every other cached module here.

Holdings are published monthly at best, so the TTL is a day and a stale copy is
served in preference to nothing.
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

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent.parent / "cache" / "etf_holdings.json"
TTL_SECONDS = 24 * 3600

#: The two ETFs /multiples computes a forward P/E for. Kept to those two on
#: purpose: this exists to explain that number, not to be a fund browser.
ETFS: list[tuple[str, str, str]] = [
    ("SPY", "S&P 500",    "标普500"),
    ("QQQ", "Nasdaq 100", "纳斯达克100"),
]

#: Yahoo's sector keys, in the order the table should read, with labels. Yahoo
#: returns them in an arbitrary dict order, and a table whose rows move between
#: refreshes is unreadable — so the order is fixed here.
#:
#: It is emitted as an ordered *list* rather than a dict, because Flask's jsonify
#: sets sort_keys=True: a dict's insertion order does not survive the response,
#: and the first version of this shipped with the rows alphabetised. Order that
#: matters has to be encoded as data, not as dict ordering.
SECTORS: list[tuple[str, str, str]] = [
    ("technology",             "Technology",             "科技"),
    ("communication_services", "Communication Services", "通信服务"),
    ("consumer_cyclical",      "Consumer Cyclical",      "消费周期"),
    ("consumer_defensive",     "Consumer Defensive",     "消费防御"),
    ("financial_services",     "Financial Services",     "金融"),
    ("healthcare",             "Healthcare",             "医疗保健"),
    ("industrials",            "Industrials",            "工业"),
    ("energy",                 "Energy",                 "能源"),
    ("basic_materials",        "Basic Materials",        "基础材料"),
    ("utilities",              "Utilities",              "公用事业"),
    ("realestate",             "Real Estate",            "房地产"),
]

MAX_HOLDINGS = 10

_lock = threading.Lock()
_mem: dict[str, Any] = {}
_mem_at: float = 0.0


def _pct(v: Any) -> Optional[float]:
    """Yahoo returns weights as decimals (0.374 for 37.4%). Store percent."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(float(v) * 100, 2)


def _one(symbol: str) -> dict[str, Any]:
    """Composition for one ETF. Raises so the caller can serve a stale copy."""
    import yfinance as yf

    fd = yf.Ticker(symbol).funds_data

    holdings: list[dict[str, Any]] = []
    try:
        top = fd.top_holdings
        if top is not None and not top.empty:
            # Indexed by symbol; columns are "Name" and "Holding Percent".
            for sym, row in top.head(MAX_HOLDINGS).iterrows():
                holdings.append({
                    "ticker": str(sym),
                    "name":   str(row.get("Name") or ""),
                    "weight": _pct(row.get("Holding Percent")),
                })
    except Exception as exc:  # noqa: BLE001 - one missing block, not a dead ETF
        log.warning("etf_holdings: %s top_holdings failed: %s", symbol, exc)

    weights: list[dict[str, Any]] = []
    try:
        raw = fd.sector_weightings or {}
        # A sector Yahoo omits is kept with weight None rather than dropped, so
        # the SPY and QQQ columns stay row-aligned in the table.
        weights = [{"key": key, "weight": _pct(raw.get(key))}
                   for key, _en, _zh in SECTORS]
    except Exception as exc:  # noqa: BLE001
        log.warning("etf_holdings: %s sector_weightings failed: %s", symbol, exc)

    assets: dict[str, Optional[float]] = {}
    overview: dict[str, str] = {}
    try:
        assets = {k: _pct(v) for k, v in (fd.asset_classes or {}).items()}
    except Exception as exc:  # noqa: BLE001
        log.warning("etf_holdings: %s asset_classes failed: %s", symbol, exc)
    try:
        overview = {k: str(v) for k, v in (fd.fund_overview or {}).items()}
    except Exception as exc:  # noqa: BLE001
        log.warning("etf_holdings: %s fund_overview failed: %s", symbol, exc)

    # Concentration is the reading this is for, so compute it rather than making
    # every consumer sum the same column.
    top_n = [h["weight"] for h in holdings if isinstance(h.get("weight"), (int, float))]
    return {
        "etf":            symbol,
        "holdings":       holdings,
        "sector_weights": weights,
        "asset_classes":  assets,
        "overview":       overview,
        "top10_weight":   round(sum(top_n), 2) if top_n else None,
        "top1_weight":    top_n[0] if top_n else None,
    }


def _fetch() -> dict[str, Any]:
    out: dict[str, Any] = {"etfs": {}, "asof": date.today().isoformat(),
                           "fetched_at": time.time()}
    errors = []
    for symbol, _en, _zh in ETFS:
        try:
            out["etfs"][symbol] = _one(symbol)
        except Exception as exc:  # noqa: BLE001
            errors.append(symbol)
            log.warning("etf_holdings: %s failed entirely: %s", symbol, exc)
    if not out["etfs"]:
        raise RuntimeError(f"no ETF composition available (tried {len(ETFS)})")
    if errors:
        out["partial"] = errors
    return out


def _read_disk() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("etfs") else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("etf_holdings: unreadable cache (%s)", exc)
        return None


def _write_disk(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:  # noqa: BLE001 - a cache miss is not an error
        log.warning("etf_holdings: could not write cache: %s", exc)


def get(force: bool = False) -> dict[str, Any]:
    """Composition for both ETFs, from cache when fresh. Never raises.

    A stale copy is served in preference to nothing, flagged ``stale``, because
    holdings are published monthly at best — last week's are still the right
    answer, whereas an empty card is not.
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
        fresh = _fetch()
    except Exception as exc:  # noqa: BLE001
        log.warning("etf_holdings: fetch failed (%s); serving cache", exc)
        stale = _mem or _read_disk()
        if stale:
            out = dict(stale)
            out["stale"] = True
            return out
        return {"etfs": {}, "asof": "", "error": str(exc)}

    with _lock:
        _mem, _mem_at = fresh, now
    _write_disk(fresh)
    log.info("etf_holdings: refreshed (%s)", ", ".join(sorted(fresh["etfs"])))
    return fresh


def peek() -> Optional[dict[str, Any]]:
    """An already-available payload, or None. Never fetches.

    Mirrors ``breadth.peek()``. This is what a request handler should use: the
    two Yahoo calls behind ``get()`` are fast but they are still Yahoo calls, and
    /multiples must not acquire a dependency on them being up.
    """
    with _lock:
        if _mem:
            return _mem
    return _read_disk()


def start_background_thread() -> None:
    """Warm on startup, then once a day."""
    def _loop() -> None:
        time.sleep(20)          # let the heavier warmers go first
        while True:
            try:
                get()
            except Exception:   # pragma: no cover - defensive
                log.exception("etf_holdings: refresh failed")
            time.sleep(TTL_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="etf-holdings").start()
    log.info("etf_holdings: background thread started (TTL %dh)", TTL_SECONDS // 3600)
