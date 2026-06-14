"""
ystocker.fed
~~~~~~~~~~~~
Fetch Federal Reserve H.4.1 balance-sheet data.

Data source: FRED (Federal Reserve Bank of St. Louis) public CSV endpoint.
No API key required.

Series (weekly, not seasonally adjusted):
  WALCL     — Total assets (all Federal Reserve Banks), millions USD
  TREAST    — U.S. Treasury securities held outright, millions USD
  MBST      — Mortgage-backed securities held outright, millions USD
  WSHOSHO   — U.S. Treasury bills outstanding (market-wide, NOT Fed-held), millions USD
  WRESBAL   — Reserve balances with Federal Reserve Banks, millions USD
  RRPONTSYD — Overnight reverse repurchase agreements (ON RRP), billions USD
  WTREGEN   — U.S. Treasury General Account (TGA) at Fed, millions USD
  WCURCIR   — Currency in circulation, millions USD
  WLCFLPCL  — Loans from Federal Reserve Banks (incl. BTFP), millions USD
  SWPT      — Central Bank Liquidity Swaps, millions USD
  WGCAL     — Gold Certificate Account, millions USD
  WSDRAL    — Special Drawing Rights Certificate Account, millions USD

Series (monthly):
  M2SL      — M2 Money Supply, billions USD (seasonally adjusted)
  M2V       — Velocity of M2 Money Stock, ratio (quarterly, not seasonally adjusted)

Cache TTL: 24 hours (H.4.1 updates once a week, on Thursdays).
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).parent.parent / "cache" / "fed_cache.json"
_CACHE_TTL  = 24 * 60 * 60   # 24 hours

# FRED public CSV endpoint (no API key needed)
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Series IDs and display metadata
SERIES: dict[str, dict[str, str]] = {
    "WALCL":     {"label": "Total Assets",              "color": "#6366f1"},
    "TREAST":    {"label": "Treasury Securities",       "color": "#38bdf8"},
    "MBST":      {"label": "MBS (Mortgage-Backed Sec)", "color": "#34d399"},
    "WSHOSHO":   {"label": "T-Bills Outstanding",       "color": "#818cf8"},
    "WRESBAL":   {"label": "Reserve Balances",          "color": "#f59e0b"},
    "RRPONTSYD": {"label": "Overnight Reverse Repos",   "color": "#fb7185"},
    "WTREGEN":   {"label": "Treasury General Account",  "color": "#facc15"},
    "WCURCIR":   {"label": "Currency in Circulation",   "color": "#94a3b8"},
    "WLCFLPCL":  {"label": "Fed Loans (incl. BTFP)",    "color": "#f97316"},
    "SWPT":      {"label": "Central Bank Liquidity Swaps", "color": "#a78bfa"},
    "WGCAL":     {"label": "Gold Certificate Account",  "color": "#fbbf24"},
    "WSDRAL":    {"label": "SDR Certificate Account",   "color": "#facc15"},
    # Monthly series
    "M2SL":      {"label": "M2 Money Supply",            "color": "#22d3ee"},
    "M2V":       {"label": "Velocity of M2",             "color": "#a3e635"},
}

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache_lock    = threading.Lock()
_cache_data: Optional[dict[str, Any]] = None
_cache_ts:   Optional[float]          = None

_warming      = False
_warming_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _load_disk_cache() -> Optional[dict[str, Any]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        if time.time() - payload.get("_ts", 0) >= _CACHE_TTL:
            return None
        # Schema check: if SERIES expanded since cache was written, force a refetch
        cached_series = payload.get("series", {})
        if not all(sid in cached_series for sid in SERIES):
            missing = [s for s in SERIES if s not in cached_series]
            log.info("Fed: disk cache missing series %s — will refetch", missing)
            return None
        return payload
    except Exception as exc:
        log.warning("Fed: failed to read disk cache: %s", exc)
    return None


def _save_disk_cache(data: dict[str, Any]) -> None:
    """Atomically write cache to disk using temp file + rename."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_CACHE_FILE.parent, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(_CACHE_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:
        log.warning("Fed: failed to write disk cache: %s", exc)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    "Accept": "text/csv,text/plain,*/*",
}


# Series already in billions USD (no /1000 conversion needed)
_SERIES_ALREADY_BILLIONS = {"RRPONTSYD", "M2SL"}

# Series that are dimensionless ratios (stored as-is, no unit conversion)
_SERIES_RAW_RATIO = {"M2V"}


def _fetch_series(series_id: str) -> Optional[dict[str, Any]]:
    """
    Fetch a single FRED series CSV and return
    {"dates": [...], "values": [...]} with values in billions USD.
    Returns None on error.
    """
    url = _FRED_CSV.format(series=series_id)
    already_billions = series_id in _SERIES_ALREADY_BILLIONS
    raw_ratio        = series_id in _SERIES_RAW_RATIO
    log.info("Fed: fetching %s from FRED", series_id)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        log.error("Fed: HTTP error for %s: %s", series_id, exc)
        return None

    # FRED CSV format:
    #   observation_date,<SERIES_ID>
    #   2002-12-18,629397
    #   ...
    # Values are in millions USD; convert to billions.

    dates:  list[str]           = []
    values: list[Optional[float]] = []

    lines = text.strip().splitlines()
    if not lines:
        log.warning("Fed: empty response for %s", series_id)
        return None

    # Skip header row
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date_str = parts[0].strip()
        val_str  = parts[1].strip()
        if len(date_str) != 10 or date_str[4] != "-":
            continue
        dates.append(date_str)
        if val_str in ("", ".", "ND", "N/A"):
            values.append(None)
        else:
            try:
                raw = float(val_str)
                if raw_ratio:
                    values.append(round(raw, 4))          # dimensionless ratio, store as-is
                else:
                    values.append(round(raw if already_billions else raw / 1000, 2))  # millions → billions
            except ValueError:
                values.append(None)

    if not dates:
        log.warning("Fed: no data rows parsed for %s", series_id)
        return None

    log.info("Fed: %s — %d obs (%s … %s), latest %s",
             series_id, len(dates), dates[0], dates[-1],
             f"{values[-1]:.4f}" if raw_ratio else f"${values[-1] or 0:.1f}B")
    return {"dates": dates, "values": values}


def _build_cache() -> dict[str, Any]:
    """Fetch all series in parallel and return the full cache payload.

    Uses ThreadPoolExecutor so all 14 FRED HTTP calls run concurrently.
    Total wall-clock time ~3-5 s instead of 35-40 s for sequential fetches.
    """
    import concurrent.futures as _cf

    result: dict[str, Any] = {"_ts": time.time(), "series": {}}

    def _fetch_one(sid: str) -> tuple[str, Optional[dict[str, Any]]]:
        return sid, _fetch_series(sid)

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, sid): sid for sid in SERIES}
        try:
            for fut in _cf.as_completed(futures, timeout=60):
                try:
                    sid, data = fut.result()
                except Exception as exc:
                    sid = futures[fut]
                    log.warning("Fed: parallel fetch failed for %s: %s", sid, exc)
                    data = None
                result["series"][sid] = (
                    data if data else {"dates": [], "values": [], "error": True}
                )
        except _cf.TimeoutError:
            log.warning("Fed: _build_cache() timed out after 60s — returning partial results (%d/%d series fetched)",
                        len(result["series"]), len(SERIES))
            for sid in SERIES:
                if sid not in result["series"]:
                    result["series"][sid] = {"dates": [], "values": [], "error": True}

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_fetch_in_progress = threading.Event()  # set while a network fetch is running


def get_fed_data(force: bool = False) -> dict[str, Any]:
    """Return cached H.4.1 data.  Loads memory → disk → network as needed.

    Critically, the cache lock is NOT held during the network fetch so that
    concurrent requests (e.g. the AJAX call on the /fed page while a background
    refresh is already running) can return stale data immediately instead of
    blocking until the fetch finishes.
    """
    global _cache_data, _cache_ts

    # ── Fast path: in-memory cache is fresh ──────────────────────────────
    with _cache_lock:
        now = time.time()
        if not force and _cache_data and _cache_ts and (now - _cache_ts) < _CACHE_TTL:
            return _cache_data

    # ── Try disk cache ────────────────────────────────────────────────────
    if not force:
        disk = _load_disk_cache()
        if disk:
            with _cache_lock:
                _cache_data = disk
                _cache_ts   = disk.get("_ts", time.time())
            return disk

    # ── If another fetch is already in progress, return stale data or warming indicator ──
    if not force and _fetch_in_progress.is_set():
        log.info("Fed: fetch in progress — returning stale/empty cache to avoid blocking worker")
        with _cache_lock:
            if _cache_data:
                return _cache_data
        # No stale data at all — return warming indicator so client can poll
        return {"_warming": True, "_ts": None, "series": {}}

    # ── Fetch from network (without holding _cache_lock) ─────────────────
    _fetch_in_progress.set()
    try:
        log.info("Fed: fetching fresh data from FRED (parallel)")
        fresh = _build_cache()
        with _cache_lock:
            _cache_data = fresh
            _cache_ts   = fresh["_ts"]
        _save_disk_cache(fresh)
        return fresh
    finally:
        _fetch_in_progress.clear()


def get_cache_ts() -> Optional[float]:
    with _cache_lock:
        if _cache_ts:
            return _cache_ts
    disk = _load_disk_cache()
    return disk.get("_ts") if disk else None


def is_cache_fresh() -> bool:
    ts = get_cache_ts()
    return bool(ts and (time.time() - ts) < _CACHE_TTL)


def is_warming() -> bool:
    with _warming_lock:
        return _warming


def refresh_cache() -> None:
    """Force a background refresh (ignores TTL)."""
    global _warming
    with _warming_lock:
        _warming = True
    try:
        get_fed_data(force=True)
    finally:
        with _warming_lock:
            _warming = False


def start_background_thread() -> None:
    """Warm the Fed cache on startup and refresh it daily in the background.

    - On startup: loads disk cache into memory immediately (fast path) or
      triggers a background fetch if the disk cache is absent/stale.
    - Daily: re-fetches FRED data every 24 hours so the first visitor after
      TTL expiry never waits for a cold fetch.
    """

    def _loop() -> None:
        # ── Startup: warm memory cache from disk if available ──────────────
        try:
            disk = _load_disk_cache()
            if disk:
                global _cache_data, _cache_ts
                with _cache_lock:
                    _cache_data = disk
                    _cache_ts   = disk.get("_ts", time.time())
                log.info("Fed background: memory cache warmed from disk (%d series)",
                         len(disk.get("series", {})))
            else:
                log.info("Fed background: no disk cache — fetching FRED data now")
                refresh_cache()
        except Exception as exc:
            log.warning("Fed background: startup warm failed: %s", exc)

        # ── Daily refresh loop ─────────────────────────────────────────────
        while True:
            time.sleep(_CACHE_TTL)
            try:
                log.info("Fed background: 24h TTL elapsed — refreshing FRED data")
                refresh_cache()
                log.info("Fed background: daily refresh complete")
            except Exception as exc:
                log.warning("Fed background: daily refresh failed: %s", exc)

    t = threading.Thread(target=_loop, name="fed-background-refresh", daemon=True)
    t.start()
    log.info("Fed: background refresh thread started")
