"""
ystocker.fed
~~~~~~~~~~~~
Fetch Federal Reserve H.4.1 balance-sheet data.

Data source: Federal Reserve Data Download Programme (DDP)
  https://www.federalreserve.gov/datadownload/

Series downloaded (weekly, not seasonally adjusted):
  WALCL  — Total assets (all Federal Reserve Banks)
  TREAST — U.S. Treasury securities held outright
  MBST   — Mortgage-backed securities held outright
  WRESBAL — Reserve balances with Federal Reserve Banks

No API key needed; the Fed publishes CSV/ZIP data files freely.
We use the H.4.1 structured data endpoint with filetype=csv.

Cache TTL: 24 hours (data updates once a week, Thursday).
"""
from __future__ import annotations

import csv
import io
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

# Fed DDP series endpoint template.
# obs=  — number of observations (520 ≈ 10 years of weekly data)
# filetype=csv returns a two-header CSV
_FED_DDP = (
    "https://www.federalreserve.gov/datadownload/Output.aspx"
    "?rel=H41&series={series}&lastobs=520&startdate=&enddate="
    "&filetype=csv&label=include&layout=seriescolumn"
)

# Series IDs and human labels
SERIES: Dict[str, Dict[str, str]] = {
    "WALCL":   {"label": "Total Assets",              "color": "#6366f1"},
    "TREAST":  {"label": "Treasury Securities",       "color": "#38bdf8"},
    "MBST":    {"label": "MBS (Mortgage-Backed Sec)", "color": "#34d399"},
    "WRESBAL": {"label": "Reserve Balances",          "color": "#f59e0b"},
}

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache_lock    = threading.Lock()
_cache_data: Optional[Dict[str, Any]] = None
_cache_ts: Optional[float] = None


def _load_disk_cache() -> Optional[Dict[str, Any]]:
    """Return on-disk cache if it exists and is fresh, otherwise None."""
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


def _fetch_series(series_id: str) -> Optional[Dict[str, Any]]:
    """
    Download a single H.4.1 series from the Fed DDP and return
    {"dates": [...], "values": [...]} where values are in billions USD.
    Returns None on error.
    """
    url = _FED_DDP.format(series=series_id)
    log.info("Fed: fetching %s from %s", series_id, url)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        log.error("Fed: HTTP error for %s: %s", series_id, exc)
        return None

    # The Fed CSV has two header rows before the data rows:
    #   Row 0: "Series Description","<long label>"
    #   Row 1: "Unit Multiplier","Millions of Dollars"  (or Billions)
    #   Row 2: "Multiplier","1"
    #   ...metadata rows...
    #   Then a blank line, then:
    #   "Series Description","<label>"
    #   "<date>","<value>"
    #   "<date>","<value>"
    #   ...
    #
    # We skip every row until we find one whose first column looks like a
    # date (YYYY-MM-DD), then treat the first column as date and second as value.

    dates: List[str]  = []
    values: List[Optional[float]] = []

    multiplier = 1.0   # will update if we see "Multiplier" row

    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        cell0 = row[0].strip().strip('"')
        # Look for multiplier metadata
        if cell0.lower() == "multiplier" and len(row) >= 2:
            try:
                multiplier = float(row[1].strip().strip('"'))
            except ValueError:
                pass
            continue
        # Data rows: first cell is YYYY-MM-DD
        if len(row) >= 2 and len(cell0) == 10 and cell0[4] == "-" and cell0[7] == "-":
            val_str = row[1].strip().strip('"')
            if val_str in ("", ".", "ND", "N/A"):
                values.append(None)
            else:
                try:
                    # Fed values are in millions; convert to billions
                    values.append(round(float(val_str) * multiplier / 1000, 2))
                except ValueError:
                    values.append(None)
            dates.append(cell0)

    if not dates:
        log.warning("Fed: no data rows parsed for %s", series_id)
        return None

    log.info("Fed: %s — %d observations (%s … %s)", series_id, len(dates), dates[0], dates[-1])
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
    """
    Return cached H.4.1 data (dict with "series" key).
    Loads from memory → disk → network, refreshing as needed.
    """
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
        log.info("Fed: fetching fresh data from Federal Reserve DDP")
        fresh = _build_cache()
        _cache_data = fresh
        _cache_ts   = fresh["_ts"]
        _save_disk_cache(fresh)
        return _cache_data


def get_cache_ts() -> Optional[float]:
    """Return Unix timestamp of the last successful cache build, or None."""
    global _cache_ts
    with _cache_lock:
        if _cache_ts:
            return _cache_ts
    disk = _load_disk_cache()
    if disk:
        return disk.get("_ts")
    return None


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


def is_cache_fresh() -> bool:
    ts = get_cache_ts()
    return bool(ts and (time.time() - ts) < _CACHE_TTL)


_warming = False
_warming_lock = threading.Lock()


def is_warming() -> bool:
    with _warming_lock:
        return _warming
