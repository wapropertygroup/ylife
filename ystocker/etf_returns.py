"""
ystocker.etf_returns
~~~~~~~~~~~~~~~~~~~~
Trailing returns for a small, fixed set of stock and bond ETFs, shown next to the
Equity Risk Premium so the reading has something actionable attached to it.

Why total return and not price return
-------------------------------------
``auto_adjust=True``, so the closes are dividend-adjusted. This is not a detail
that can be waved away for this particular table: a bond ETF earns most of its
return as coupon income, so on price alone AGG looks like a years-long loss while
its total return is positive. Charting price-only next to an ERP reading that is
telling the reader to prefer bonds would argue against itself.

Multi-year figures are annualised (CAGR), which is the convention for a fund
table, and the UI says so. 1Y is left as the simple period return because
annualising one year is the same number and calling it "per year" invites the
reader to think it is a forecast.

Periods are located by calendar date, not by counting 252 trading days back. A
year is not a fixed number of sessions -- holidays and the odd exchange closure
move it -- and the drift compounds at the 5Y point, which is where the annualised
figure is most sensitive.

This is a cache, not an observed series: every number here is recomputable from
Yahoo at any time, so unlike the forward-P/E snapshots it does not need DynamoDB.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent.parent / "cache" / "etf_returns.json"
TTL_SECONDS = 24 * 3600

# Deliberately short and boring. These are the vehicles the ERP argument is
# actually about -- broad equity beta on one side, duration and credit on the
# other -- not a screen of interesting funds. Names are held here rather than
# fetched so the table needs one download and cannot half-render.
EQUITY: list[tuple[str, str, str]] = [
    ("SPY",  "S&P 500",              "标普500"),
    ("QQQ",  "Nasdaq 100",           "纳斯达克100"),
    ("VTI",  "Total US market",      "美国全市场"),
    ("SCHD", "US dividend",          "美国高股息"),
]
BOND: list[tuple[str, str, str]] = [
    ("AGG",  "US aggregate bond",    "美国综合债券"),
    ("TLT",  "20+yr Treasury",       "20年期以上国债"),
    ("SHY",  "1-3yr Treasury",       "1-3年期国债"),
    ("LQD",  "IG corporate",         "投资级公司债"),
]
ALL = EQUITY + BOND

_lock = threading.Lock()
_mem: dict[str, Any] = {}
_mem_at: float = 0.0


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------

def _nearest_prior(series, target: date):
    """The last observation on or before ``target``, or None.

    "On or before" rather than nearest in either direction: a return measured
    from a date the market had not reached yet would quietly shorten the window.
    """
    try:
        window = series.loc[:str(target)]
    except Exception:  # noqa: BLE001 - unparseable index
        return None
    window = window.dropna()
    if window.empty:
        return None
    return float(window.iloc[-1])


def _trailing(series, years: int, annualise: bool) -> Optional[float]:
    """Percentage return over ``years``, annualised when asked. None if short.

    None rather than a number computed from a shorter window: SCHD and several
    bond funds have plenty of history, but a newly listed ticker would otherwise
    report a 5Y figure covering two years and flatter itself.
    """
    clean = series.dropna()
    if clean.empty:
        return None
    end_stamp = clean.index[-1]
    end = float(clean.iloc[-1])
    try:
        end_date = end_stamp.date()
    except AttributeError:
        return None

    target = end_date - timedelta(days=round(365.25 * years))
    start = _nearest_prior(clean, target)
    if not start or start <= 0 or end <= 0:
        return None

    # Refuse a window that does not actually reach back far enough. 30 days of
    # slack absorbs a target landing on a long holiday, nothing more.
    first = clean.index[0]
    try:
        if first.date() > target + timedelta(days=30):
            return None
    except AttributeError:
        return None

    total = end / start
    if annualise and years > 1:
        return (total ** (1.0 / years) - 1.0) * 100.0
    return (total - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch() -> dict[str, Any]:
    """Download and compute. Raises on failure; callers fall back to cache."""
    import yfinance as yf

    tickers = [t for t, _, _ in ALL]
    # 6y of daily bars to measure 5y with room for the nearest-prior lookup.
    df = yf.download(tickers, period="6y", interval="1d",
                     auto_adjust=True, progress=False, threads=True)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no rows")

    # yf.download gives a column MultiIndex for several tickers and a flat one
    # for a single ticker; normalise so one code path handles both.
    closes = df["Close"] if "Close" in df.columns.get_level_values(0) else df

    def rows(group: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
        out = []
        for ticker, name, name_zh in group:
            if ticker not in closes.columns:
                log.warning("etf_returns: %s missing from the download", ticker)
                continue
            series = closes[ticker]
            out.append({
                "ticker": ticker,
                "name": name,
                "name_zh": name_zh,
                "r1": _trailing(series, 1, annualise=False),
                "r3": _trailing(series, 3, annualise=True),
                "r5": _trailing(series, 5, annualise=True),
            })
        return out

    equity, bond = rows(EQUITY), rows(BOND)
    if not equity and not bond:
        raise RuntimeError("no usable series")

    asof = ""
    try:
        asof = str(closes.dropna(how="all").index[-1].date())
    except Exception:  # noqa: BLE001
        asof = date.today().isoformat()

    return {
        "asof": asof,
        # Surfaced so the UI can state the basis rather than leaving a reader to
        # assume these are price returns.
        "total_return": True,
        "annualised_from": 3,
        "equity": equity,
        "bond": bond,
        "fetched_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _read_disk() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("equity") else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("etf_returns: unreadable cache (%s)", exc)
        return None


def _write_disk(payload: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:  # noqa: BLE001 - a cache miss is not an error
        log.warning("etf_returns: could not write cache: %s", exc)


def get(force: bool = False) -> dict[str, Any]:
    """Trailing returns, from cache when fresh.

    Never raises. A stale cache is served in preference to nothing, with its own
    ``asof`` date attached, because the alternative is a table that vanishes
    whenever Yahoo rate-limits -- and these numbers move slowly enough that
    yesterday's are still the right shape of answer.
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
        log.warning("etf_returns: fetch failed (%s); serving cache", exc)
        stale = _mem or _read_disk()
        if stale:
            out = dict(stale)
            out["stale"] = True
            return out
        return {"equity": [], "bond": [], "asof": "", "error": str(exc)}

    with _lock:
        _mem, _mem_at = fresh, now
    _write_disk(fresh)
    log.info("etf_returns: refreshed (%d equity, %d bond, asof %s)",
             len(fresh["equity"]), len(fresh["bond"]), fresh["asof"])
    return fresh
