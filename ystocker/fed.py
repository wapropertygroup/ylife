"""
ystocker.fed
~~~~~~~~~~~~
Fetch Federal Reserve H.4.1 balance-sheet data.

Data source: FRED (Federal Reserve Bank of St. Louis) public CSV endpoint.
No API key required.

Series (weekly, not seasonally adjusted):
  WALCL     — Total assets (all Federal Reserve Banks), millions USD
  TREAST    — U.S. Treasury securities held outright, millions USD
  WSHOSHO   — Short-term Treasury bills held outright (≤1 year), millions USD
  MBST      — Mortgage-backed securities held outright, millions USD
  WRESBAL   — Reserve balances with Federal Reserve Banks, millions USD
  RRPONTSYD — Overnight reverse repurchase agreements (ON RRP), billions USD
  WTREGEN   — U.S. Treasury General Account (TGA) at Fed, millions USD
  WCURCIR   — Currency in circulation, millions USD
  WLCFLPCL  — Loans from Federal Reserve Banks (incl. BTFP), millions USD

Cache TTL: 24 hours (H.4.1 updates once a week, on Thursdays).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).parent.parent / "cache" / "fed_cache.json"
_CACHE_TTL  = 24 * 60 * 60   # 24 hours

# FRED public CSV endpoint (no API key needed)
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Series IDs and display metadata
SERIES: Dict[str, Dict[str, str]] = {
    "WALCL":     {"label": "Total Assets",              "color": "#6366f1"},
    "TREAST":    {"label": "Treasury Securities",       "color": "#38bdf8"},
    "WSHOSHO":   {"label": "Bills (Short-Term)",        "color": "#818cf8"},
    "MBST":      {"label": "MBS (Mortgage-Backed Sec)", "color": "#34d399"},
    "WRESBAL":   {"label": "Reserve Balances",          "color": "#f59e0b"},
    "RRPONTSYD": {"label": "Overnight Reverse Repos",   "color": "#fb7185"},
    "WTREGEN":   {"label": "Treasury General Account",  "color": "#facc15"},
    "WCURCIR":   {"label": "Currency in Circulation",   "color": "#94a3b8"},
    "WLCFLPCL":  {"label": "Fed Loans (incl. BTFP)",    "color": "#f97316"},
}

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache_lock    = threading.Lock()
_cache_data: Optional[Dict[str, Any]] = None
_cache_ts:   Optional[float]          = None

_warming      = False
_warming_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _load_disk_cache() -> Optional[Dict[str, Any]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        if time.time() - payload.get("_ts", 0) < _CACHE_TTL:
            return payload
    except Exception as exc:
        log.warning("Fed: failed to read disk cache: %s", exc)
    return None


def _save_disk_cache(data: Dict[str, Any]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data))
    except Exception as exc:
        log.warning("Fed: failed to write disk cache: %s", exc)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": "yStocker/1.0 (https://github.com/15th-Ave-NE/ystocker)",
    "Accept": "text/csv,text/plain,*/*",
}


# Series already in billions USD (no /1000 conversion needed)
_SERIES_ALREADY_BILLIONS = {"RRPONTSYD"}


def _fetch_series(series_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single FRED series CSV and return
    {"dates": [...], "values": [...]} with values in billions USD.
    Returns None on error.
    """
    url = _FRED_CSV.format(series=series_id)
    already_billions = series_id in _SERIES_ALREADY_BILLIONS
    log.info("Fed: fetching %s from FRED", series_id)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
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

    dates:  List[str]           = []
    values: List[Optional[float]] = []

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
                values.append(round(raw if already_billions else raw / 1000, 2))  # millions → billions
            except ValueError:
                values.append(None)

    if not dates:
        log.warning("Fed: no data rows parsed for %s", series_id)
        return None

    log.info("Fed: %s — %d obs (%s … %s), latest $%.1fB",
             series_id, len(dates), dates[0], dates[-1], values[-1] or 0)
    return {"dates": dates, "values": values}


def _build_cache() -> Dict[str, Any]:
    """Fetch all series and return the full cache payload."""
    result: Dict[str, Any] = {"_ts": time.time(), "series": {}}
    for sid in SERIES:
        data = _fetch_series(sid)
        if data:
            result["series"][sid] = data
        else:
            result["series"][sid] = {"dates": [], "values": [], "error": True}
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_fed_data(force: bool = False) -> Dict[str, Any]:
    """Return cached H.4.1 data. Loads memory → disk → network as needed."""
    global _cache_data, _cache_ts

    with _cache_lock:
        now = time.time()

        # 1. In-memory fresh?
        if not force and _cache_data and _cache_ts and (now - _cache_ts) < _CACHE_TTL:
            return _cache_data

        # 2. Disk cache fresh?
        if not force:
            disk = _load_disk_cache()
            if disk:
                _cache_data = disk
                _cache_ts   = disk.get("_ts", now)
                return _cache_data

        # 3. Fetch from network
        log.info("Fed: fetching fresh data from FRED")
        fresh = _build_cache()
        _cache_data = fresh
        _cache_ts   = fresh["_ts"]
        _save_disk_cache(fresh)
        return _cache_data


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
