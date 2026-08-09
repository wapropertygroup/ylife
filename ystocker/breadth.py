"""
ystocker.breadth
~~~~~~~~~~~~~~~~
Market breadth: the share of S&P 500 constituents trading above each of their
key moving averages, plus the equal-weight / cap-weight concentration ratio.

Why this is computed locally
----------------------------
The canonical breadth symbols ($SPXA20R / $SPXA50R / $SPXA100R / $SPXA150R /
$SPXA200R) are StockCharts index symbols. They are NOT available on Yahoo
Finance -- ``^SPXA50R`` and ``^SPXA200R`` return HTTP 404 ("Quote not found"),
so any chart sourced from them renders empty. Instead we download daily closes
for the whole index universe and compute the diffusion indices ourselves, which
also gives us every MA period rather than just 50/200.

Method
------
1. Download ~11 years of daily adjusted closes for all S&P 500 constituents
   (one batched yfinance call; the extra year is warm-up so the 200-day MA is
   already defined at the start of the 10-year display window).
2. For each MA period P, per trading day: ``% = count(close > MA_P) / count(
   tickers with both a close and a defined MA_P)``. Days with fewer than
   ``_MIN_VALID`` participating tickers are dropped so a thin tail can never
   produce a wild reading.
3. Resample to weekly (last observation per ISO week) to keep the payload small
   -- the UI plots 1Y-10Y windows, where weekly resolution is indistinguishable
   from daily.

Caveats
-------
* Survivorship bias: the universe is today's constituents applied to history,
  the standard simplification for this chart. Absolute levels in older periods
  are therefore slightly optimistic; the shape and turning points are intact.
* Percentages are of *participating* tickers, not a fixed 503, so a name that
  IPO'd mid-window dilutes nothing before it existed.

Cache TTL: 24 hours (this is a weekly-resolution chart; intraday refresh is
pointless and the download costs ~25s).
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).parent.parent / "cache" / "breadth_cache.json"
_CACHE_TTL  = 24 * 60 * 60   # 24 hours

# Moving-average periods (trading days) charted as breadth diffusion indices.
# Ordered short -> long; the UI colours them as an ordinal ramp in this order.
MA_PERIODS: tuple[int, ...] = (20, 50, 100, 150, 200)

# History pulled from Yahoo. 11y = 10y display window + 1y warm-up so the
# 200-day MA is defined on the first displayed day.
_HISTORY_PERIOD = "11y"

# Minimum number of participating tickers for a day to be published.
_MIN_VALID = 50

# Concentration proxy: equal-weight S&P 500 vs cap-weighted S&P 500.
_RSP, _SPY = "RSP", "SPY"

# ---------------------------------------------------------------------------
# S&P 500 universe (Yahoo ticker format: dots -> dashes, e.g. BRK.B -> BRK-B)
#
# Intentionally static, like heatmap_meta.py. Index membership changes ~20
# names/year, which moves a 500-name diffusion index by well under a point, so
# a static list is refreshed on a code change rather than at runtime (avoiding
# a hard dependency on a constituent-list scraper in the request path).
# ---------------------------------------------------------------------------
SP500_UNIVERSE: tuple[str, ...] = (
    # ── Industrials (83) ──────────────────────────────────────────────
    "ADP", "ALLE", "AME", "AOS", "AXON", "BA", "BLDR", "BR",
    "CARR", "CAT", "CHRW", "CMI", "CPRT", "CSX", "CTAS", "DAL",
    "DD", "DE", "DOV", "EFX", "EME", "EMR", "ETN", "EXPD",
    "FAST", "FDX", "FDXF", "FERG", "FIX", "FTV", "GD", "GE",
    "GEV", "GNRC", "GWW", "HII", "HON", "HONA", "HUBB", "HWM",
    "IEX", "IR", "ITW", "J", "JBHT", "JCI", "LDOS", "LHX",
    "LII", "LMT", "LUV", "MAS", "MMM", "NDSN", "NOC", "NSC",
    "ODFL", "OTIS", "PAYX", "PCAR", "PH", "PNR", "PWR", "ROK",
    "ROL", "RSG", "RTX", "SNA", "SWK", "TDG", "TT", "TXT",
    "UAL", "UBER", "UNP", "UPS", "URI", "VLTO", "VRSK", "VRT",
    "WAB", "WM", "XYL",
    # ── Financials (76) ───────────────────────────────────────────────
    "ACGL", "AFL", "AIG", "AIZ", "AJG", "ALL", "AMP", "AON",
    "APO", "ARES", "AXP", "BAC", "BEN", "BLK", "BNY", "BRK-B",
    "BRO", "BX", "C", "CB", "CBOE", "CFG", "CINF", "CME",
    "COF", "COIN", "CPAY", "EG", "ERIE", "FDS", "FIS", "FISV",
    "FITB", "GL", "GPN", "GS", "HBAN", "HIG", "HOOD", "IBKR",
    "ICE", "IVZ", "JKHY", "JPM", "KEY", "KKR", "L", "MA",
    "MCO", "MET", "MRSH", "MS", "MSCI", "MTB", "NDAQ", "NTRS",
    "PFG", "PGR", "PNC", "PRU", "PYPL", "RF", "RJF", "SCHW",
    "SPGI", "STT", "SYF", "TFC", "TROW", "TRV", "USB", "V",
    "WFC", "WRB", "WTW", "XYZ",
    # ── Information Technology (73) ───────────────────────────────────
    "AAPL", "ACN", "ADBE", "ADI", "ADSK", "AKAM", "AMAT", "AMD",
    "ANET", "APH", "AVGO", "CDNS", "CDW", "CIEN", "COHR", "CRM",
    "CRWD", "CSCO", "CTSH", "DDOG", "DELL", "FFIV", "FICO", "FLEX",
    "FSLR", "FTNT", "GDDY", "GEN", "GLW", "HPE", "HPQ", "IBM",
    "INTC", "INTU", "IT", "JBL", "KEYS", "KLAC", "LITE", "LRCX",
    "MCHP", "MPWR", "MRVL", "MSFT", "MSI", "MU", "NOW", "NTAP",
    "NVDA", "NXPI", "ON", "ORCL", "PANW", "PLTR", "PTC", "Q",
    "QCOM", "ROP", "SMCI", "SNDK", "SNPS", "STX", "SWKS", "TDY",
    "TEL", "TER", "TRMB", "TXN", "TYL", "VRSN", "WDAY", "WDC",
    "ZBRA",
    # ── Health Care (59) ──────────────────────────────────────────────
    "A", "ABBV", "ABT", "ALGN", "AMGN", "BAX", "BDX", "BIIB",
    "BMY", "BSX", "CAH", "CI", "CNC", "COO", "COR", "CRL",
    "CVS", "DGX", "DHR", "DVA", "DXCM", "ELV", "EW", "GEHC",
    "GILD", "HCA", "HSIC", "HUM", "IDXX", "INCY", "IQV", "ISRG",
    "JNJ", "LH", "LLY", "MCK", "MDT", "MRK", "MRNA", "MTD",
    "PFE", "PODD", "REGN", "RMD", "RVTY", "SOLV", "STE", "SYK",
    "TECH", "TMO", "UHS", "UNH", "VEEV", "VRTX", "VTRS", "WAT",
    "WST", "ZBH", "ZTS",
    # ── Consumer Discretionary (47) ───────────────────────────────────
    "ABNB", "AMZN", "APTV", "AZO", "BBY", "BKNG", "CCL", "CMG",
    "CVNA", "DASH", "DECK", "DHI", "DPZ", "DRI", "EBAY", "EXPE",
    "F", "GM", "GPC", "GRMN", "HAS", "HD", "HLT", "LEN",
    "LOW", "LULU", "LVS", "MAR", "MCD", "MGM", "NCLH", "NKE",
    "NVR", "ORLY", "PHM", "RCL", "RL", "ROST", "SBUX", "TJX",
    "TPR", "TSCO", "TSLA", "ULTA", "WSM", "WYNN", "YUM",
    # ── Consumer Staples (34) ─────────────────────────────────────────
    "ADM", "BF-B", "BG", "CASY", "CHD", "CL", "CLX", "COST",
    "DG", "DLTR", "EL", "GIS", "HRL", "HSY", "KDP", "KHC",
    "KMB", "KO", "KR", "KVUE", "MDLZ", "MKC", "MNST", "MO",
    "PEP", "PG", "PM", "SJM", "STZ", "SYY", "TAP", "TGT",
    "TSN", "WMT",
    # ── Utilities (31) ────────────────────────────────────────────────
    "AEE", "AEP", "AES", "ATO", "AWK", "CEG", "CMS", "CNP",
    "D", "DTE", "DUK", "ED", "EIX", "ES", "ETR", "EVRG",
    "EXC", "FE", "LNT", "NEE", "NI", "NRG", "PCG", "PEG",
    "PNW", "PPL", "SO", "SRE", "VST", "WEC", "XEL",
    # ── Real Estate (31) ──────────────────────────────────────────────
    "AMT", "ARE", "AVB", "BXP", "CBRE", "CCI", "CPT", "CSGP",
    "DLR", "DOC", "EQIX", "EQR", "ESS", "EXR", "FRT", "HST",
    "INVH", "IRM", "KIM", "MAA", "O", "PLD", "PSA", "REG",
    "SBAC", "SPG", "UDR", "VICI", "VTR", "WELL", "WY",
    # ── Materials (25) ────────────────────────────────────────────────
    "ALB", "AMCR", "APD", "AVY", "BALL", "CF", "CRH", "CTVA",
    "DOW", "ECL", "FCX", "IFF", "IP", "LIN", "LYB", "MLM",
    "MOS", "NEM", "NUE", "PKG", "PPG", "SHW", "STLD", "SW",
    "VMC",
    # ── Communication Services (23) ───────────────────────────────────
    "APP", "CHTR", "CMCSA", "DIS", "ECHO", "FOX", "FOXA", "GOOG",
    "GOOGL", "LYV", "META", "NFLX", "NWS", "NWSA", "OMC", "PSKY",
    "T", "TKO", "TMUS", "TTD", "TTWO", "VZ", "WBD",
    # ── Energy (21) ───────────────────────────────────────────────────
    "APA", "BKR", "COP", "CVX", "DVN", "EOG", "EQT", "EXE",
    "FANG", "HAL", "KMI", "MPC", "OKE", "OXY", "PSX", "SLB",
    "TPL", "TRGP", "VLO", "WMB", "XOM",
)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache_data: Optional[dict[str, Any]] = None
_cache_ts: Optional[float] = None
_cache_lock = threading.Lock()

# Serialises rebuilds so concurrent requests can never stack 500-ticker
# downloads on top of each other, and throttles retries after a failure: a
# broken upstream must not turn every page view into a 20s blocking fetch
# (that is exactly how this endpoint used to time out behind nginx).
_build_lock = threading.Lock()
_last_build_attempt: float = 0.0
_BUILD_RETRY_COOLDOWN = 10 * 60   # 10 minutes

_warming = False
_warming_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _load_disk_cache(ignore_ttl: bool = False) -> Optional[dict[str, Any]]:
    """Read the on-disk payload. ``ignore_ttl`` returns stale data for fallback."""
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        if not ignore_ttl and time.time() - payload.get("_ts", 0) >= _CACHE_TTL:
            return None
        # Schema check: a cache written before MA_PERIODS expanded is unusable.
        cached = payload.get("pct_above_ma", {})
        missing = [p for p in MA_PERIODS if str(p) not in cached]
        if missing:
            log.info("Breadth: disk cache missing MA periods %s — will recompute", missing)
            return None
        return payload
    except Exception as exc:
        log.warning("Breadth: failed to read disk cache: %s", exc)
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
        log.warning("Breadth: failed to write disk cache: %s", exc)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def _weekly_last(series, digits: int = 2) -> dict[str, list]:
    """Down-sample a daily pandas Series to the last observation of each week.

    ``digits`` must match the precision the UI renders: the RSP/SPY ratio sits
    around 0.28, so 2 decimals would flatten it into a staircase.
    """
    s = series.dropna()
    if s.empty:
        return {"dates": [], "values": []}
    weekly = s.resample("W-FRI").last().dropna()
    return {
        "dates":  [str(d.date()) for d in weekly.index],
        "values": [round(float(v), digits) for v in weekly],
    }


def _build_cache() -> dict[str, Any]:
    """Download the universe and compute every breadth series. Slow (~25s)."""
    import pandas as pd
    import yfinance as yf

    tickers = list(SP500_UNIVERSE) + [_RSP, _SPY]
    t0 = time.time()
    df = yf.download(tickers, period=_HISTORY_PERIOD, interval="1d",
                     auto_adjust=True, progress=False, threads=True)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no data for the S&P 500 universe")
    closes = df["Close"]
    log.info("Breadth: downloaded %d/%d tickers in %.1fs",
             int(closes.notna().any().sum()), len(tickers), time.time() - t0)

    # Constituent closes only — RSP/SPY are index proxies, not members.
    members = [t for t in SP500_UNIVERSE if t in closes.columns]
    px = closes[members]
    live = px.notna()

    pct_above_ma: dict[str, dict[str, list]] = {}
    latest: dict[str, Optional[float]] = {}
    for period in MA_PERIODS:
        ma      = px.rolling(period, min_periods=period).mean()
        counted = live & ma.notna()
        valid   = counted.sum(axis=1)
        above   = (px > ma) & counted
        pct     = above.sum(axis=1).where(valid > 0) / valid.where(valid > 0) * 100
        pct     = pct.where(valid >= _MIN_VALID)
        series  = _weekly_last(pct)
        pct_above_ma[str(period)] = series
        latest[str(period)] = series["values"][-1] if series["values"] else None

    # ── Concentration: equal-weight / cap-weight ratio ──────────────────────
    def _col(sym):
        return closes[sym].dropna() if sym in closes.columns else None

    rsp, spy = _col(_RSP), _col(_SPY)
    if rsp is not None and spy is not None and not rsp.empty and not spy.empty:
        ratio = (rsp / spy).replace([float("inf"), float("-inf")], pd.NA).dropna()
        rsp_spy = _weekly_last(ratio, digits=4)
    else:
        log.warning("Breadth: RSP/SPY unavailable — concentration ratio skipped")
        rsp_spy = {"dates": [], "values": []}

    universe_used = int(live.any().sum())
    asof = pct_above_ma[str(MA_PERIODS[0])]["dates"][-1:] or [""]
    return {
        "_ts": time.time(),
        "ma_periods":   list(MA_PERIODS),
        "pct_above_ma": pct_above_ma,
        "latest":       latest,
        "rsp_spy":      rsp_spy,
        "universe":     universe_used,
        "asof":         asof[0],
        # Back-compat aliases for any older client still reading these keys.
        "pct_above_50ma":  pct_above_ma.get("50",  {"dates": [], "values": []}),
        "pct_above_200ma": pct_above_ma.get("200", {"dates": [], "values": []}),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _serve_stale() -> Optional[dict[str, Any]]:
    """Return an expired disk cache, flagged, or None if there is nothing."""
    global _cache_data
    stale = _load_disk_cache(ignore_ttl=True)
    if not stale:
        return None
    stale = {**stale, "stale": True}
    with _cache_lock:
        # Publish to memory but leave _cache_ts alone so a later call still
        # knows the data is expired and will retry once the cooldown lapses.
        _cache_data = stale
    return stale


def get_breadth(force: bool = False) -> dict[str, Any]:
    """Return the breadth payload, using memory -> disk -> rebuild in order.

    A caller that has *any* data available never gets an exception: if the
    rebuild fails, an expired disk cache is served instead, because a day-old
    diffusion index is far more useful than an empty chart. Rebuilds are
    serialised and rate-limited so a cold or broken cache cannot pile 20-second
    downloads onto every in-flight request.
    """
    global _cache_data, _cache_ts, _last_build_attempt
    now = time.time()

    with _cache_lock:
        if not force and _cache_data and _cache_ts and (now - _cache_ts) < _CACHE_TTL:
            return _cache_data

    if not force:
        disk = _load_disk_cache()
        if disk:
            with _cache_lock:
                _cache_data, _cache_ts = disk, disk.get("_ts", now)
            return disk

    with _build_lock:
        # Another thread may have finished a rebuild while we waited here.
        with _cache_lock:
            if not force and _cache_data and _cache_ts and (time.time() - _cache_ts) < _CACHE_TTL:
                return _cache_data

        since = time.time() - _last_build_attempt
        if not force and _last_build_attempt and since < _BUILD_RETRY_COOLDOWN:
            stale = _serve_stale()
            if stale:
                log.info("Breadth: rebuild on cooldown (%.0fs left) — serving stale cache",
                         _BUILD_RETRY_COOLDOWN - since)
                return stale

        _last_build_attempt = time.time()
        try:
            data = _build_cache()
        except Exception as exc:
            log.warning("Breadth: rebuild failed (%s) — falling back to stale cache", exc)
            stale = _serve_stale()
            if stale:
                return stale
            raise

        # Publish while still holding _build_lock: a waiter re-checks the memory
        # cache the moment it acquires the lock, so releasing before this point
        # would let it start a second, redundant download.
        _save_disk_cache(data)
        with _cache_lock:
            _cache_data, _cache_ts = data, data["_ts"]
        return data


def get_cache_ts() -> Optional[float]:
    with _cache_lock:
        return _cache_ts


def is_warming() -> bool:
    with _warming_lock:
        return _warming


def refresh_cache() -> None:
    """Force a rebuild (ignores TTL)."""
    global _warming
    with _warming_lock:
        _warming = True
    try:
        get_breadth(force=True)
    finally:
        with _warming_lock:
            _warming = False


def start_background_thread() -> None:
    """Warm breadth on startup, then rebuild daily.

    The rebuild downloads 500+ tickers and takes ~25s, far longer than the
    nginx/Gunicorn request budget — so it must never happen inside a request.
    This thread guarantees the first visitor always hits a warm cache.
    """

    def _loop() -> None:
        time.sleep(10)  # let Gunicorn finish booting before a 500-ticker fetch
        try:
            disk = _load_disk_cache()
            if disk:
                global _cache_data, _cache_ts
                with _cache_lock:
                    _cache_data = disk
                    _cache_ts   = disk.get("_ts", time.time())
                log.info("Breadth background: memory cache warmed from disk (asof %s)",
                         disk.get("asof"))
            else:
                log.info("Breadth background: no fresh disk cache — computing now")
                refresh_cache()
        except Exception as exc:
            log.warning("Breadth background: startup warm failed: %s", exc)

        while True:
            time.sleep(_CACHE_TTL)
            try:
                log.info("Breadth background: 24h TTL elapsed — recomputing")
                refresh_cache()
                log.info("Breadth background: daily refresh complete")
            except Exception as exc:
                log.warning("Breadth background: daily refresh failed: %s", exc)

    t = threading.Thread(target=_loop, name="breadth-background-refresh", daemon=True)
    t.start()
    log.info("Breadth: background refresh thread started (%d tickers, MA %s)",
             len(SP500_UNIVERSE), ", ".join(str(p) for p in MA_PERIODS))
