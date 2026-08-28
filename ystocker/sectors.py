"""
ystocker.sectors
~~~~~~~~~~~~~~~~
Yahoo's own sector aggregates: size, market weight, sub-industries, leaders.

Why this exists: /evaluation groups tickers into 26 hand-maintained peer groups
and computes medians across them. That answers "how is this basket priced"; it
cannot answer "how big is this sector, and what fraction of the market is it".
Yahoo publishes both, per sector, along with a sub-industry breakdown and the
top companies by weight.

The two views are complementary rather than duplicative, and worth keeping
distinct in the UI: a peer group is *our* definition — "AI / Robotics" is not a
GICS sector and deliberately mixes NVDA with SMCI and ARM — whereas these are
Yahoo's eleven canonical sectors. Presenting them as the same thing would imply
the medians on the page are sector medians, which they are not.

Cost is eleven requests, so this is a cached module with a background thread like
the rest, and ``peek()`` never fetches.
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

CACHE_PATH = Path(__file__).parent.parent / "cache" / "sectors_cache.json"
TTL_SECONDS = 24 * 3600

#: Bump when the payload shape changes; a cache from an older build is then
#: refetched instead of being served into code expecting something else.
CACHE_VER = "v1"

#: Yahoo's sector keys with labels, in descending market-weight order as of
#: writing. The order here is only the initial one — the payload is sorted by the
#: weight Yahoo actually returns — but keeping it roughly right means the table
#: looks sane even on a partial fetch.
SECTORS: list[tuple[str, str, str]] = [
    ("technology",             "Technology",             "科技"),
    ("financial-services",     "Financial Services",     "金融"),
    ("consumer-cyclical",      "Consumer Cyclical",      "消费周期"),
    ("healthcare",             "Healthcare",             "医疗保健"),
    ("communication-services", "Communication Services", "通信服务"),
    ("industrials",            "Industrials",            "工业"),
    ("consumer-defensive",     "Consumer Defensive",     "消费防御"),
    ("energy",                 "Energy",                 "能源"),
    ("utilities",              "Utilities",              "公用事业"),
    ("real-estate",            "Real Estate",            "房地产"),
    ("basic-materials",        "Basic Materials",        "基础材料"),
]

MAX_TOP_COMPANIES = 10
MAX_INDUSTRIES = 8

_lock = threading.Lock()
_mem: dict[str, Any] = {}
_mem_at: float = 0.0


def _f(v: Any) -> Optional[float]:
    """A float, or None. See the note in analyst._f: numpy int64 is not an int,
    so this converts rather than type-checks."""
    if v is None or isinstance(v, (bool, str, bytes)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN would break json.dump


def _pct(v: Any) -> Optional[float]:
    """Yahoo returns market weights as decimals (0.322 for 32.2%)."""
    f = _f(v)
    return None if f is None else round(f * 100, 2)


def _int(v: Any) -> Optional[int]:
    """Counts arrive as strings often enough to be worth coercing here."""
    if isinstance(v, bool):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _one(key: str) -> dict[str, Any]:
    import yfinance as yf

    s = yf.Sector(key)
    out: dict[str, Any] = {"key": key}

    try:
        ov = s.overview or {}
        out.update({
            "name":            str(s.name or key),
            "companies":       _int(ov.get("companies_count")),
            "industries":      _int(ov.get("industries_count")),
            "market_cap":      _f(ov.get("market_cap")),
            "market_weight":   _pct(ov.get("market_weight")),
            "employees":       _int(ov.get("employee_count")),
        })
    except Exception as exc:  # noqa: BLE001
        log.debug("sectors: %s overview failed: %s", key, exc)

    try:
        df = s.top_companies
        if df is not None and not df.empty:
            out["top_companies"] = [
                {"ticker": str(sym),
                 "name":   str(row.get("name") or ""),
                 "rating": str(row.get("rating") or ""),
                 "weight": _pct(row.get("market weight"))}
                for sym, row in df.head(MAX_TOP_COMPANIES).iterrows()
            ]
    except Exception as exc:  # noqa: BLE001
        log.debug("sectors: %s top_companies failed: %s", key, exc)

    try:
        df = s.industries
        if df is not None and not df.empty:
            out["sub_industries"] = [
                {"key":    str(idx),
                 "name":   str(row.get("name") or ""),
                 "weight": _pct(row.get("market weight"))}
                for idx, row in df.head(MAX_INDUSTRIES).iterrows()
            ]
    except Exception as exc:  # noqa: BLE001
        log.debug("sectors: %s industries failed: %s", key, exc)

    return out


def _fetch() -> dict[str, Any]:
    from ystocker import fetchguard
    from ystocker.data import PROVIDER

    rows, failed, stopped = [], [], None
    for key, _en, _zh in SECTORS:
        try:
            fetchguard.guard(PROVIDER)
        except fetchguard.CooldownActive as exc:
            stopped = str(exc)
            log.warning("sectors: stopping at %s — %s", key, exc)
            break
        try:
            rows.append(_one(key))
        except Exception as exc:  # noqa: BLE001
            failed.append(key)
            log.warning("sectors: %s failed: %s", key, exc)

    if not rows:
        raise RuntimeError("no sector data available")

    # Sorted by the weight Yahoo returned, not by the order in SECTORS, so the
    # table reflects reality if the market has reshuffled. Emitted as a *list* —
    # Flask's jsonify sorts dict keys, which would silently alphabetise it.
    rows.sort(key=lambda r: r.get("market_weight") or -1, reverse=True)

    payload: dict[str, Any] = {
        "sectors":    rows,
        "ver":        CACHE_VER,
        "asof":       date.today().isoformat(),
        "fetched_at": time.time(),
        "covered":    len(rows),
        "expected":   len(SECTORS),
    }
    if failed:
        payload["failed"] = failed
    if stopped:
        payload["stopped_early"] = stopped
    return payload


def _read_disk() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("sectors"):
            return None
        if data.get("ver") != CACHE_VER:
            log.info("sectors: cache is %s, want %s — refetching",
                     data.get("ver"), CACHE_VER)
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("sectors: unreadable cache (%s)", exc)
        return None


def _write_disk(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, allow_nan=False)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning("sectors: could not write cache: %s", exc)


def get(force: bool = False) -> dict[str, Any]:
    """Sector aggregates, from cache when fresh. Never raises."""
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
        log.warning("sectors: fetch failed (%s); serving cache", exc)
        stale = _mem or _read_disk()
        if stale:
            out = dict(stale)
            out["stale"] = True
            return out
        return {"sectors": [], "asof": "", "error": str(exc)}

    with _lock:
        _mem, _mem_at = fresh, now
    _write_disk(fresh)
    log.info("sectors: refreshed (%d/%d sectors)", fresh["covered"], fresh["expected"])
    return fresh


def peek() -> Optional[dict[str, Any]]:
    """An already-available payload, or None. Never fetches."""
    with _lock:
        if _mem:
            return _mem
    return _read_disk()


def start_background_thread() -> None:
    def _loop() -> None:
        time.sleep(60)          # after the heavier warmers, before the analyst sweep
        while True:
            try:
                get()
            except Exception:   # pragma: no cover - defensive
                log.exception("sectors: refresh failed")
            time.sleep(TTL_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="sectors").start()
    log.info("sectors: background thread started (TTL %dh)", TTL_SECONDS // 3600)
