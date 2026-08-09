"""
ystocker.fed
~~~~~~~~~~~~
Fetch Federal Reserve H.4.1 balance-sheet data.

Data source: FRED (Federal Reserve Bank of St. Louis) public CSV endpoint.
No API key required.

Series (weekly, not seasonally adjusted):
  WALCL     — Total assets (all Federal Reserve Banks), millions USD
  TREAST    — U.S. Treasury securities held outright, millions USD
  WSHOMCB   — Mortgage-backed securities held outright (Wednesday level), millions USD
  WSHOSHO   — U.S. Treasury bills outstanding (market-wide, NOT Fed-held), millions USD
  WRESBAL   — Reserve balances with Federal Reserve Banks, millions USD
  RRPONTSYD — Overnight reverse repurchase agreements (ON RRP), billions USD
  WTREGEN   — U.S. Treasury General Account (TGA) at Fed, millions USD
  WCURCIR   — Currency in circulation, millions USD
  WLCFLPCL  — Loans from Federal Reserve Banks (incl. BTFP), millions USD
  SWPT      — Central Bank Liquidity Swaps, millions USD
  WGCAL     — Gold Certificate Account, millions USD

Note: WSDRAL (SDR Certificate Account) is intentionally absent. FRED 404s that
id; the real id is WASDRAL, but that series stopped publishing in June 2018, so
charting it would present 8-year-old data as current. The fed.html SDR card
already renders an "unavailable" note when the series is missing.

Note: MBS holdings use WSHOMCB, not MBST. MBST is the same trap as WASDRAL — it
still returns HTTP 200 with 809 well-formed rows, but stopped publishing on
2018-06-13, so it silently served 8-year-old MBS holdings as current and (worse)
inflated the derived "Other Assets" figure in fed.html by the accumulated drift.
Prefer the Wednesday-level H.4.1 ids (WSHO*) — they share WALCL's release
cadence, so a stale one shows up as a row-count mismatch against WALCL.

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
    "WSHOMCB":   {"label": "MBS (Mortgage-Backed Sec)", "color": "#34d399"},
    "WSHOSHO":   {"label": "T-Bills Outstanding",       "color": "#818cf8"},
    "WRESBAL":   {"label": "Reserve Balances",          "color": "#f59e0b"},
    "RRPONTSYD": {"label": "Overnight Reverse Repos",   "color": "#fb7185"},
    "WTREGEN":   {"label": "Treasury General Account",  "color": "#facc15"},
    "WCURCIR":   {"label": "Currency in Circulation",   "color": "#94a3b8"},
    "WLCFLPCL":  {"label": "Fed Loans (incl. BTFP)",    "color": "#f97316"},
    "SWPT":      {"label": "Central Bank Liquidity Swaps", "color": "#a78bfa"},
    "WGCAL":     {"label": "Gold Certificate Account",  "color": "#fbbf24"},
    # NOTE: no WSDRAL — see the module docstring (FRED 404s it; WASDRAL ended 2018).
    # Monthly series
    "M2SL":      {"label": "M2 Money Supply",            "color": "#22d3ee"},
    "M2V":       {"label": "Velocity of M2",             "color": "#a3e635"},
    # ── Inflation & real rates ──────────────────────────────────────────────
    "DFII10":          {"label": "10Y Real Yield (TIPS)",      "unit": "pct",    "scale": 1.0},
    "T10YIE":          {"label": "10Y Breakeven Inflation",    "unit": "pct",    "scale": 1.0},
    # ── Credit spreads (OAS, basis points) ─────────────────────────────────
    # FRED publishes both ICE BofA OAS series in PERCENT (e.g. 2.71), but every
    # consumer in fed.html labels the axis and tooltip "bps". scale=100 converts
    # at the source so those labels are literally true; without it the HY chart
    # renders "3bps" next to copy that reads ">600bps = distress level".
    "BAMLH0A0HYM2":    {"label": "HY OAS (bps)",               "unit": "bps",    "scale": 100.0},
    "BAMLC0A0CM":      {"label": "IG OAS (bps)",               "unit": "bps",    "scale": 100.0},
    # ── Valuation / business cycle ──────────────────────────────────────────
    # Buffett Indicator numerator. Wilshire pulled every WILL5000* series from
    # FRED, so the old ids (WILL5000, WILL5000IND, WILL5000INDFC, WILL5000PR)
    # all 404. NCBEILQ027S — Nonfinancial Corporate Business; Corporate Equities;
    # Liability Level — is the standard FRED substitute. It is quarterly (not
    # daily) and reported in millions, so it is deliberately NOT listed in
    # _SERIES_ALREADY_BILLIONS: the millions→billions conversion is what keeps it
    # on the same scale as GDP for the ratio in fed.html.
    "NCBEILQ027S":     {"label": "US Corporate Equities ($B)", "unit": "bln",   "scale": 1.0},
    "GDP":             {"label": "US GDP ($B)",                 "unit": "bln",   "scale": 1.0},
    "INDPRO":          {"label": "Industrial Production Index", "unit": "index",  "scale": 1.0},  # replaces NAPM (which returns empty)
    "HOUST":           {"label": "Housing Starts (k units)",   "unit": "k",      "scale": 1.0},
    # ── Recession indicator ─────────────────────────────────────────────────
    "USREC":           {"label": "NBER Recession",              "unit": "binary", "scale": 1.0},
    # ── Consumer & housing leading indicators ───────────────────────────────
    "UMCSENT":         {"label": "Consumer Sentiment (UMich)", "unit": "index",  "scale": 1.0},
    "MORTGAGE30US":    {"label": "30Y Fixed Mortgage Rate",    "unit": "pct",    "scale": 1.0},
    "GDPC1":           {"label": "Real GDP ($B chained)",      "unit": "bln",    "scale": 1.0},
    "CPIAUCSL":        {"label": "CPI (All Urban)",             "unit": "index",  "scale": 1.0},
    "DCOILWTICO":      {"label": "WTI Crude Oil ($/bbl)",       "unit": "usd",    "scale": 1.0},
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


# ── Fetch helpers ───────────────────────────────────────────────────────────
# IMPORTANT: Do NOT send a spoofed browser User-Agent to FRED.
#
# fred.stlouisfed.org is fronted by Akamai Kona (kona-prod.stlouisfed.org), whose
# bot detection compares the claimed client against the actual TLS/header
# fingerprint. A request that claims to be Chrome while presenting a Python/
# requests TLS stack is treated as a bot and silently blackholed: the TCP + TLS
# handshake succeeds, then the connection is dropped with no HTTP response at
# all. In `requests` this surfaces as a ReadTimeout after the full timeout
# elapses (not a 403), which makes it look like a network/FRED outage.
#
# A plain, honest client User-Agent is allowed through and returns 200. Verified
# against FRED from the production host: this UA -> 200, "Mozilla/5.0 ..." ->
# ReadTimeout, empty UA -> ReadTimeout.
FRED_USER_AGENT = "ystocker/1.0 (+https://stock.li-family.us)"

_HEADERS = {
    "User-Agent": FRED_USER_AGENT,
    "Accept": "text/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Shared session for connection pooling
_SESSION = requests.Session()
_SESSION.trust_env = False  # Ignore system proxies which can cause silent timeouts
_SESSION.headers.update(_HEADERS)


# Series already in billions USD (no /1000 conversion needed)
_SERIES_ALREADY_BILLIONS = {
    "RRPONTSYD", "M2SL",
    # New series — already in natural units, no millions→billions conversion
    "DFII10", "T10YIE", "BAMLH0A0HYM2", "BAMLC0A0CM",
    "GDP", "INDPRO", "HOUST", "USREC",
    "UMCSENT", "MORTGAGE30US", "GDPC1", "CPIAUCSL", "DCOILWTICO",
}

# Series that are dimensionless ratios (stored as-is, no unit conversion)
_SERIES_RAW_RATIO = {"M2V", "DFII10", "T10YIE"}


def _fetch_series(series_id: str) -> Optional[dict[str, Any]]:
    """
    Fetch a single FRED series CSV with up to 3 retries + exponential back-off.
    Returns {"dates": [...], "values": [...]} or None on persistent failure.
    """
    url = _FRED_CSV.format(series=series_id)
    already_billions = series_id in _SERIES_ALREADY_BILLIONS
    raw_ratio        = series_id in _SERIES_RAW_RATIO
    meta             = SERIES.get(series_id, {})
    unit             = str(meta.get("unit", "bln"))
    # Applied last, after the millions→billions / ratio branch below, so it
    # expresses "FRED's native unit → the unit fed.html labels the axis with".
    scale            = float(meta.get("scale", 1.0) or 1.0)

    text = None
    for attempt in range(1, 4):
        try:
            log.info("Fed: fetching %s (attempt %d/3)...", series_id, attempt)
            resp = _SESSION.get(url, timeout=30)
            if resp.status_code != 200:
                log.warning("Fed: %s got HTTP %d for %s. Body: %s", 
                            series_id, resp.status_code, url, resp.text[:200])
            resp.raise_for_status()
            text = resp.text
            break
        except Exception as exc:
            if attempt == 3:
                log.error("Fed: final attempt failed for %s (%s). URL: %s", series_id, exc, url)
                return None
            wait = attempt * 3
            log.warning("Fed: %s attempt %d failed (%s) — retry in %ds", series_id, attempt, exc, wait)
            time.sleep(wait)


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
                    values.append(round(raw * scale, 4))  # dimensionless ratio, store as-is
                else:
                    values.append(round((raw if already_billions else raw / 1000) * scale, 2))  # millions → billions
            except ValueError:
                values.append(None)

    if not dates:
        log.warning("Fed: no data rows parsed for %s", series_id)
        return None

    def _fmt_latest():
        v = values[-1]
        if v is None:
            return "N/A"
        if raw_ratio:
            return f"{v:.4f}"
        return f"${v:.1f}B" if unit == "bln" else f"{v:.1f} {unit}"

    log.info("Fed: %s — %d obs (%s … %s), latest %s",
             series_id, len(dates), dates[0], dates[-1], _fmt_latest())
    return {"dates": dates, "values": values}


def _build_cache() -> dict[str, Any]:
    """Fetch all series in parallel and return the full cache payload.

    Uses ThreadPoolExecutor so all 14 FRED HTTP calls run concurrently.
    Total wall-clock time ~3-5 s instead of 35-40 s for sequential fetches.
    """
    import concurrent.futures as _cf

    result: dict[str, Any] = {"_ts": time.time(), "series": {}}

    def _fetch_one(sid: str) -> tuple[str, Optional[dict[str, Any]]]:
        # Small random delay to jitter the requests
        import random
        time.sleep(random.uniform(0.1, 0.5))
        return sid, _fetch_series(sid)

    with _cf.ThreadPoolExecutor(max_workers=2) as pool:  # 2 is safer for FRED
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
    """Check if we have a valid, recent cache.

    Deep validation: returns False if the cache is missing, stale, or contains
    empty/error data for critical series (like WALCL).
    """
    with _cache_lock:
        if _cache_data:
            data = _cache_data
        else:
            data = _load_disk_cache()

    if not data:
        return False

    # Check TTL
    ts = data.get("_ts")
    if not ts or (time.time() - ts) >= _CACHE_TTL:
        return False

    # Deep check: ensure critical series have data
    series = data.get("series", {})
    for sid in ["WALCL", "M2SL"]:  # absolute must-haves
        if not series.get(sid, {}).get("dates"):
            log.warning("Fed: cache for %s is empty/error — forcing refetch", sid)
            return False

    return True


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
