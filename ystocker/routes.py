"""
ystocker.routes
~~~~~~~~~~~~~~~
Flask URL routes (views).

GET /                       - home page: sector cards + cross-sector charts
GET /sector/<sector_name>   - per-sector detail: all charts + data table
GET /refresh                - clears the data cache then redirects to /
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import json
import math

import pandas as pd
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify

from ystocker import PEER_GROUPS
from ystocker.data import fetch_group
from ystocker import charts

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Two-layer cache:
#   1. In-memory dict  - zero-latency reads during a running session
#   2. On-disk JSON    - survives server restarts; loaded on startup if fresh
#
# The background thread warms / refreshes both layers every 8 hours.
# Requests never block: they see the warming page until data is ready.
# ---------------------------------------------------------------------------
_CACHE_TTL      = 8 * 60 * 60           # seconds until cache is considered stale
_CACHE_FILE     = Path(__file__).parent.parent / "cache" / "ticker_cache.json"
_GROUPS_FILE    = Path(__file__).parent.parent / "cache" / "peer_groups.json"

_cache: Optional[Dict[str, Dict[str, dict]]] = None
_fetch_errors: List[str] = []
_cache_lock     = threading.Lock()
_cache_warming  = False
_cache_last_updated: Optional[float] = None


# -- Disk helpers -------------------------------------------------------------

def _save_to_disk(data: Dict[str, Dict[str, dict]], errors: List[str], ts: float) -> None:
    """Persist the cache to a JSON file."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": ts, "errors": errors, "data": data}
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        tmp.replace(_CACHE_FILE)          # atomic replace
        log.info("Cache saved to disk: %s", _CACHE_FILE)
    except Exception:
        log.exception("Failed to save cache to disk")


def _load_from_disk() -> bool:
    """
    Load the on-disk cache into memory if it exists and is not stale.
    Returns True if a valid, fresh cache was loaded.
    """
    global _cache, _fetch_errors, _cache_last_updated
    if not _CACHE_FILE.exists():
        return False
    try:
        payload = json.loads(_CACHE_FILE.read_text())
        ts = float(payload["timestamp"])
        age = time.time() - ts
        if age > _CACHE_TTL:
            log.info("Disk cache is stale (%.1f h old) - will re-fetch", age / 3600)
            return False
        with _cache_lock:
            _cache = payload["data"]
            _fetch_errors = payload.get("errors", [])
            _cache_last_updated = ts
        log.info("Loaded disk cache from %s (%.1f h old)", _CACHE_FILE, age / 3600)
        return True
    except Exception:
        log.exception("Failed to read disk cache - will re-fetch")
        return False


# -- Peer-group persistence ----------------------------------------------------

def _save_groups() -> None:
    """Write the current PEER_GROUPS to disk so edits survive restarts."""
    try:
        _GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GROUPS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(PEER_GROUPS, indent=2))
        tmp.replace(_GROUPS_FILE)
        log.info("Peer groups saved to %s", _GROUPS_FILE)
    except Exception:
        log.exception("Failed to save peer groups to disk")


def _load_groups() -> None:
    """Load peer groups from disk, overriding the defaults defined in __init__.py."""
    if not _GROUPS_FILE.exists():
        return
    try:
        saved = json.loads(_GROUPS_FILE.read_text())
        PEER_GROUPS.clear()
        PEER_GROUPS.update(saved)
        log.info("Loaded %d peer groups from %s", len(PEER_GROUPS), _GROUPS_FILE)
    except Exception:
        log.exception("Failed to load peer groups from disk - using defaults")


# -- Fetch / background loop --------------------------------------------------

def _do_fetch() -> None:
    """Fetch all tickers, update in-memory cache, and persist to disk."""
    global _cache, _fetch_errors, _cache_warming, _cache_last_updated
    all_tickers = sorted({t for tickers in PEER_GROUPS.values() for t in tickers})
    log.info("Cache fetch started - %d tickers", len(all_tickers))
    t0 = time.perf_counter()
    raw, errors = fetch_group(all_tickers)
    elapsed = time.perf_counter() - t0
    log.info("Cache fetch done in %.1fs - %d ok, %d failed", elapsed, len(raw), len(errors))
    for err in errors:
        log.warning("Fetch error: %s", err)

    new_cache = {
        group: {t: raw[t] for t in tickers if t in raw}
        for group, tickers in PEER_GROUPS.items()
    }
    ts = time.time()
    with _cache_lock:
        _cache = new_cache
        _fetch_errors = errors
        _cache_warming = False
        _cache_last_updated = ts

    _save_to_disk(new_cache, errors, ts)


def _background_loop() -> None:
    """
    On startup: load saved peer groups, then try the disk cache; fetch from
    Yahoo Finance only if the disk cache is missing or stale.
    Then sleep and repeat every 8 h.
    """
    global _cache_warming
    _load_groups()          # restore any UI edits made before the last restart
    # First iteration: skip fetch if disk cache is still fresh
    disk_ok = _load_from_disk()
    if not disk_ok:
        with _cache_lock:
            _cache_warming = True
        try:
            _do_fetch()
        except Exception:
            log.exception("Unhandled error during background cache fetch")
            with _cache_lock:
                _cache_warming = False

    # Sleep until the cache (disk or fresh) is due to expire, then loop
    while True:
        with _cache_lock:
            last = _cache_last_updated
        sleep_for = _CACHE_TTL - (time.time() - last) if last else _CACHE_TTL
        sleep_for = max(sleep_for, 0)
        log.info("Next cache refresh in %.1f h", sleep_for / 3600)
        time.sleep(sleep_for)

        with _cache_lock:
            _cache_warming = True
        try:
            _do_fetch()
        except Exception:
            log.exception("Unhandled error during background cache refresh")
            with _cache_lock:
                _cache_warming = False


def _start_background_thread() -> None:
    t = threading.Thread(target=_background_loop, daemon=True, name="cache-warmer")
    t.start()
    log.info("Cache warmer started (TTL %dh, file: %s)", _CACHE_TTL // 3600, _CACHE_FILE)


# -- Public accessors ---------------------------------------------------------

def _get_data() -> Optional[Dict[str, Dict[str, dict]]]:
    with _cache_lock:
        return _cache


def _is_warming() -> bool:
    with _cache_lock:
        return _cache_warming


def _raw_to_df(raw: dict) -> pd.DataFrame:
    """Convert a {ticker: data_dict} map into a DataFrame indexed by ticker."""
    if not raw:
        # Return an empty DataFrame with the expected columns so callers don't crash
        cols = ["Name", "Current Price", "Target Price", "Upside (%)",
                "PE (TTM)", "PE (Forward)", "PEG", "Market Cap ($B)"]
        df = pd.DataFrame(columns=cols)
        df.index.name = "Ticker"
        return df
    df = pd.DataFrame(raw.values())
    df = df.set_index("Ticker")
    return df


def _safe(v):
    """Return None for NaN/Inf so json.dumps works cleanly."""
    if v is None:
        return None
    try:
        if math.isnan(v) or math.isinf(v):
            return None
    except TypeError:
        pass
    return v


def _df_to_chartdata(df: pd.DataFrame) -> str:
    """Serialize a group DataFrame to a JSON string for Chart.js templates."""
    rows = []
    for ticker, row in df.iterrows():
        rows.append({
            "ticker":           ticker,
            "name":             str(row.get("Name", ticker)),
            "price":            _safe(row.get("Current Price")),
            "target":           _safe(row.get("Target Price")),
            "upside":           _safe(row.get("Upside (%)")),
            "pe_ttm":           _safe(row.get("PE (TTM)")),
            "pe_fwd":           _safe(row.get("PE (Forward)")),
            "peg":              _safe(row.get("PEG")),
            "market_cap":       _safe(row.get("Market Cap ($B)")),
            "eps_growth_ttm":   _safe(row.get("EPS Growth TTM (%)")),
            "eps_growth_q":     _safe(row.get("EPS Growth Q (%)")),
            "day_change_pct":   _safe(row.get("Day Change (%)")),
        })
    return json.dumps(rows)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    """Home page - sector overview cards + cross-sector charts."""
    log.info("GET /")
    data = _get_data()

    # Cache not ready yet - show a friendly loading page
    if data is None:
        return render_template("warming.html",
                               peer_groups=list(PEER_GROUPS.keys()),
                               fetch_errors=[]), 503

    group_dfs = {}
    for g, raw in data.items():
        try:
            group_dfs[g] = _raw_to_df(raw)
        except Exception:
            log.warning("Skipping group '%s' - could not build DataFrame", g)

    sector_cards = {}
    all_rows = []
    for group, df in group_dfs.items():
        cd = json.loads(_df_to_chartdata(df))
        sector_cards[group] = {
            "tickers":   list(df.index),
            "chartdata": cd,
        }
        for row in cd:
            row["sector"] = group
            all_rows.append(row)

    with _cache_lock:
        errors = _fetch_errors
        last_updated = _cache_last_updated

    log.info("Home page rendered - %d groups", len(group_dfs))
    return render_template(
        "index.html",
        peer_groups=list(PEER_GROUPS.keys()),
        sector_cards=sector_cards,
        all_chartdata=json.dumps(all_rows),
        fetch_errors=errors,
        cache_last_updated=last_updated,
        warming=_is_warming(),
    )


@bp.route("/sector/<path:sector_name>")
def sector(sector_name: str):
    """Detail page for one sector - all charts + full data table."""
    log.info("GET /sector/%s", sector_name)
    data = _get_data()

    if data is None:
        return render_template("warming.html",
                               peer_groups=list(PEER_GROUPS.keys()),
                               fetch_errors=[]), 503

    if sector_name not in data:
        log.warning("Sector '%s' not found", sector_name)
        return render_template("error.html",
                               peer_groups=list(PEER_GROUPS.keys()),
                               error=f"Sector '{sector_name}' not found."), 404

    df = _raw_to_df(data[sector_name])
    log.info("Rendering sector '%s' (%d tickers)", sector_name, len(df))

    chartdata = _df_to_chartdata(df)
    table_cols = ["Name", "Market Cap ($B)", "Current Price",
                  "Target Price", "Upside (%)", "PE (TTM)", "PE (Forward)", "PEG",
                  "EPS Growth TTM (%)", "EPS Growth Q (%)", "Day Change (%)"]
    existing_cols = [c for c in table_cols if c in df.columns]
    table_df = df[existing_cols].copy()

    with _cache_lock:
        errors = _fetch_errors

    return render_template(
        "sector.html",
        sector_name=sector_name,
        peer_groups=list(PEER_GROUPS.keys()),
        chartdata=chartdata,
        table=table_df,
        table_cols=table_cols,
        fetch_errors=errors,
    )


@bp.route("/refresh")
def refresh():
    """Clear the cache and trigger an immediate background re-fetch."""
    _invalidate_cache()
    return redirect(url_for("main.index"))


@bp.route("/api/cache-age")
def api_cache_age():
    """Return seconds since the cache was last updated."""
    with _cache_lock:
        last = _cache_last_updated
    age = int(time.time() - last) if last else None
    return jsonify({"age_seconds": age, "last_updated": last})


# ---------------------------------------------------------------------------
# Interactive peer-group management
# ---------------------------------------------------------------------------

@bp.route("/groups", methods=["GET"])
def groups():
    """Interactive page - view, add, and remove peer groups and tickers."""
    log.info("GET /groups")
    return render_template("groups.html",
                           peer_groups=list(PEER_GROUPS.keys()),
                           groups_data=PEER_GROUPS)


@bp.route("/groups/add-group", methods=["POST"])
def add_group():
    """Create a new empty peer group."""
    name = request.form.get("group_name", "").strip()
    if not name:
        flash("Group name cannot be empty.", "error")
    elif name in PEER_GROUPS:
        flash(f"Group '{name}' already exists.", "error")
    else:
        PEER_GROUPS[name] = []
        _save_groups()
        _invalidate_cache()
        log.info("Added new group '%s'", name)
        flash(f"Group '{name}' created.", "success")
    return redirect(url_for("main.groups"))


@bp.route("/groups/delete-group", methods=["POST"])
def delete_group():
    """Delete an entire peer group."""
    name = request.form.get("group_name", "").strip()
    if name in PEER_GROUPS:
        del PEER_GROUPS[name]
        _save_groups()
        _invalidate_cache()
        log.info("Deleted group '%s'", name)
        flash(f"Group '{name}' deleted.", "success")
    return redirect(url_for("main.groups"))


@bp.route("/groups/add-ticker", methods=["POST"])
def add_ticker():
    """Add a ticker symbol to an existing peer group."""
    group_name = request.form.get("group_name", "").strip()
    ticker     = request.form.get("ticker", "").strip().upper()
    if group_name not in PEER_GROUPS:
        flash(f"Group '{group_name}' not found.", "error")
    elif not ticker:
        flash("Ticker symbol cannot be empty.", "error")
    elif ticker in PEER_GROUPS[group_name]:
        flash(f"{ticker} is already in '{group_name}'.", "error")
    else:
        PEER_GROUPS[group_name].append(ticker)
        _save_groups()
        _invalidate_cache()
        log.info("Added ticker %s to group '%s'", ticker, group_name)
        flash(f"Added {ticker} to '{group_name}'.", "success")
    return redirect(url_for("main.groups"))


@bp.route("/groups/remove-ticker", methods=["POST"])
def remove_ticker():
    """Remove a ticker from a peer group."""
    group_name = request.form.get("group_name", "").strip()
    ticker     = request.form.get("ticker", "").strip().upper()
    if group_name in PEER_GROUPS and ticker in PEER_GROUPS[group_name]:
        PEER_GROUPS[group_name].remove(ticker)
        _save_groups()
        _invalidate_cache()
        log.info("Removed ticker %s from group '%s'", ticker, group_name)
        flash(f"Removed {ticker} from '{group_name}'.", "success")
    return redirect(url_for("main.groups"))


def _invalidate_cache():
    """Clear in-memory + disk cache and kick off a background re-fetch."""
    global _cache, _fetch_errors, _cache_warming
    # Delete disk file so a stale restart doesn't reload old data
    try:
        if _CACHE_FILE.exists():
            _CACHE_FILE.unlink()
            log.info("Disk cache deleted: %s", _CACHE_FILE)
    except Exception:
        log.exception("Could not delete disk cache")
    with _cache_lock:
        already = _cache_warming
        _cache = None
        _fetch_errors = []
        if not already:
            _cache_warming = True
    if not already:
        log.info("Cache invalidated - spawning background re-fetch")
        t = threading.Thread(target=_do_fetch, daemon=True, name="cache-invalidate-refetch")
        t.start()
    else:
        log.info("Cache invalidated (fetch already in progress)")


# ---------------------------------------------------------------------------
# Historical PE page
# ---------------------------------------------------------------------------

@bp.route("/history/<ticker>")
def history(ticker: str):
    """Page showing 1-year historical PE ratio for a single ticker."""
    ticker = ticker.strip().upper()
    log.info("GET /history/%s", ticker)
    return render_template("history.html",
                           ticker=ticker,
                           peer_groups=list(PEER_GROUPS.keys()),
                           fetch_errors=[])


@bp.route("/api/history/<ticker>")
def api_history(ticker: str):
    """
    JSON API - return weekly closing price and estimated PE (TTM) over the past year.

    PE is estimated as:  price / (ttmEPS from latest info)
    because yfinance does not expose historical EPS directly.
    The ttmEPS stays constant so PE tracks price movement -
    useful for visualising valuation vs price trend.
    """
    import yfinance as yf
    ticker = ticker.strip().upper()
    log.info("API history: %s", ticker)
    try:
        tk   = yf.Ticker(ticker)
        info = tk.info
        hist = tk.history(period="1y", interval="1wk")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    eps = info.get("trailingEps")
    name = info.get("shortName", ticker)

    if hist.empty:
        return jsonify({"error": f"No price history for '{ticker}'."}), 404

    dates  = [str(d.date()) for d in hist.index]
    prices = [round(float(p), 2) if not math.isnan(float(p)) else None
              for p in hist["Close"]]

    pe_history = []
    for p in prices:
        if p is not None and eps and eps > 0:
            pe_history.append(round(p / eps, 2))
        else:
            pe_history.append(None)

    # PEG history: PE(week) / (earnings_growth * 100)
    # earnings_growth is a single scalar from yfinance - PEG tracks PE movement
    earnings_growth = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
    peg_history = []
    if earnings_growth and earnings_growth > 0:
        growth_pct = earnings_growth * 100
        for pe in pe_history:
            peg_history.append(round(pe / growth_pct, 2) if pe is not None else None)
    current_peg = _safe(info.get("pegRatio"))

    # Current metrics for reference line
    current_pe   = _safe(info.get("trailingPE"))
    forward_pe   = _safe(info.get("forwardPE"))
    target_price = _safe(info.get("targetMeanPrice"))
    earnings_growth_ttm = info.get("earningsGrowth")
    earnings_growth_q   = info.get("earningsQuarterlyGrowth")

    return jsonify({
        "ticker":           ticker,
        "name":             name,
        "dates":            dates,
        "prices":           prices,
        "pe_history":       pe_history,
        "peg_history":      peg_history,
        "current_pe":       current_pe,
        "current_peg":      current_peg,
        "forward_pe":       forward_pe,
        "target_price":     target_price,
        "eps":              _safe(eps),
        "eps_growth_ttm":   _safe(round(earnings_growth_ttm * 100, 1)) if earnings_growth_ttm is not None else None,
        "eps_growth_q":     _safe(round(earnings_growth_q   * 100, 1)) if earnings_growth_q   is not None else None,
    })


# ---------------------------------------------------------------------------
# Ticker lookup - interactive single-stock analysis
# ---------------------------------------------------------------------------

@bp.route("/lookup")
def lookup():
    """Page with a live ticker search box and sector/industry discovery."""
    log.info("GET /lookup")
    return render_template("lookup.html",
                           peer_groups=list(PEER_GROUPS.keys()),
                           fetch_errors=[])


@bp.route("/api/ticker/<ticker>")
def api_ticker(ticker: str):
    """
    JSON API - fetch metrics for a single ticker.
    Called by the browser via fetch() - no page reload needed.
    The result is also merged into the live cache so subsequent page
    loads reflect the latest data without a full refresh.

    Returns 200 + JSON data dict on success.
    Returns 404 + {"error": "..."} if ticker not found / no data.
    Returns 502 + {"error": "..."} if Yahoo Finance is unreachable.
    """
    ticker = ticker.strip().upper()
    log.info("API ticker lookup: %s", ticker)
    from ystocker.data import fetch_ticker_data, FetchError
    try:
        data = fetch_ticker_data(ticker)
    except FetchError as exc:
        log.warning("API fetch error for %s: %s", ticker, exc)
        return jsonify({"error": str(exc)}), 502

    # If yfinance returned an empty shell (unknown ticker), Name == ticker and
    # all numeric fields are None.
    if data.get("Current Price") is None and data.get("Name") == ticker:
        return jsonify({"error": f"No data found for '{ticker}'. Check the symbol."}), 404

    # Merge into in-memory cache and write through to disk
    snapshot = None
    ts = None
    with _cache_lock:
        if _cache is not None:
            for group, tickers in PEER_GROUPS.items():
                if ticker in tickers:
                    _cache[group][ticker] = data
                    log.debug("Cache updated: %s in group '%s'", ticker, group)
            snapshot = {g: dict(v) for g, v in _cache.items()}
            ts = _cache_last_updated or time.time()

    if snapshot is not None:
        # Write-through to disk in a background thread so the response isn't delayed
        threading.Thread(
            target=_save_to_disk,
            args=(snapshot, _fetch_errors, ts),
            daemon=True,
            name="cache-writeback",
        ).start()

    return jsonify(data)


@bp.route("/api/discover")
def api_discover():
    """
    JSON API - return top companies for a given yfinance Sector or Industry.

    Query params:
      type  = "sector" | "industry"
      name  = e.g. "technology" | "semiconductors"

    Uses yfinance's Sector / Industry classes (yfinance >= 0.2.37).
    Falls back to a curated built-in map if the library call fails.
    """
    import yfinance as yf

    kind = request.args.get("type", "sector").lower()
    name = request.args.get("name", "").strip()
    log.info("API discover: type=%s name=%s", kind, name)

    if not name:
        return jsonify({"error": "Missing 'name' parameter"}), 400

    try:
        obj = yf.Sector(name) if kind == "sector" else yf.Industry(name)
        top = obj.top_companies
        tickers = list(top.index[:20])
        return jsonify({"tickers": tickers, "source": "yfinance"})
    except Exception as exc:
        log.warning("yfinance discover failed (%s), using built-in map: %s", name, exc)

    # ---- Built-in fallback map ------------------------------------------------
    BUILTIN: Dict[str, List[str]] = {
        # Sectors
        "technology":             ["MSFT", "AAPL", "NVDA", "GOOGL", "META", "AVGO", "ORCL", "CSCO", "IBM", "INTC"],
        "healthcare":             ["UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE", "BMY"],
        "financials":             ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK", "SCHW"],
        "consumer discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "TGT", "BKNG", "CMG"],
        "consumer staples":       ["PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "CL", "MDLZ", "EL"],
        "energy":                 ["XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY", "KMI"],
        "industrials":            ["GE", "RTX", "HON", "CAT", "UNP", "BA", "LMT", "DE", "MMM", "FDX"],
        "materials":              ["LIN", "APD", "ECL", "SHW", "FCX", "NEM", "NUE", "VMC", "MLM", "ALB"],
        "utilities":              ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED", "ETR"],
        "real estate":            ["AMT", "PLD", "CCI", "EQIX", "PSA", "O", "DLR", "WELL", "SPG", "AVB"],
        "communication services": ["GOOGL", "META", "VZ", "T", "NFLX", "DIS", "CMCSA", "TMUS", "EA", "TTWO"],
        # Industries
        "semiconductors":         ["NVDA", "AMD", "INTC", "QCOM", "TSM", "AVGO", "TXN", "MU", "AMAT", "LRCX"],
        "software":               ["MSFT", "ORCL", "CRM", "NOW", "ADBE", "INTU", "SNOW", "TEAM", "WDAY", "ZM"],
        "cloud":                  ["AMZN", "MSFT", "GOOGL", "CRM", "NOW", "SNOW", "MDB", "DDOG", "NET", "ZS"],
        "ev":                     ["TSLA", "RIVN", "NIO", "GM", "F", "LCID", "LI", "XPEV", "STLA", "MBGAF"],
        "biotech":                ["AMGN", "GILD", "BIIB", "VRTX", "REGN", "MRNA", "ILMN", "SGEN", "ALNY", "BMRN"],
        "banks":                  ["JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "COF"],
        "insurance":              ["BRK-B", "MET", "PRU", "AFL", "AIG", "CB", "TRV", "ALL", "HIG", "PGR"],
        "retail":                 ["AMZN", "WMT", "COST", "TGT", "HD", "LOW", "TJX", "ROST", "DLTR", "BBY"],
        "airlines":               ["DAL", "UAL", "AAL", "LUV", "ALK", "JBLU", "HA", "SAVE", "SKYW", "MESA"],
        "defense":                ["LMT", "RTX", "NOC", "GD", "BA", "HII", "KTOS", "CACI", "LDOS", "SAIC"],
    }
    tickers = BUILTIN.get(name.lower())
    if tickers:
        return jsonify({"tickers": tickers, "source": "built-in"})
    return jsonify({"error": f"No built-in data for '{name}'. Try a different name."}), 404


@bp.route("/contact")
def contact():
    return render_template("contact.html", peer_groups=list(PEER_GROUPS.keys()))
