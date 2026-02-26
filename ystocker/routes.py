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
from decimal import Decimal

import pandas as pd
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, Response, has_request_context

from ystocker import PEER_GROUPS, YT_CHANNELS
from ystocker.data import fetch_group
from ystocker import charts

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)


def _flash(en: str, zh: str, category: str = "message") -> None:
    """Flash a message in the user's preferred language (cookie: ystocker_lang)."""
    lang = "en"
    if has_request_context():
        lang = request.cookies.get("ystocker_lang", "en")
    flash(zh if lang == "zh" else en, category)

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
            "ev_ebitda":        _safe(row.get("EV/EBITDA")),
            "ev":               _safe(row.get("EV ($B)")),
            "ebitda":           _safe(row.get("EBITDA ($B)")),
        })
    return json.dumps(rows).replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    """Home page — redirects to markets overview."""
    return redirect(url_for("main.markets"))


@bp.route("/evaluation")
def evaluation():
    """Valuation dashboard — sector overview cards + cross-sector charts."""
    log.info("GET /evaluation")
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

    log.info("Evaluation page rendered - %d groups", len(group_dfs))
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
                  "EPS Growth TTM (%)", "EPS Growth Q (%)", "Day Change (%)", "EV/EBITDA", "EV ($B)", "EBITDA ($B)"]
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
    return redirect(url_for("main.markets"))


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
        _flash("Group name cannot be empty.", "分组名称不能为空。", "error")
    elif name in PEER_GROUPS:
        _flash(f"Group '{name}' already exists.", f'分组\u201c{name}\u201d已存在。', "error")
    else:
        PEER_GROUPS[name] = []
        _save_groups()
        _invalidate_cache()
        log.info("Added new group '%s'", name)
        _flash(f"Group '{name}' created.", f'分组\u201c{name}\u201d已创建。', "success")
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
        _flash(f"Group '{name}' deleted.", f'分组\u201c{name}\u201d已删除。', "success")
    return redirect(url_for("main.groups"))


@bp.route("/groups/add-ticker", methods=["POST"])
def add_ticker():
    """Add a ticker symbol to an existing peer group."""
    group_name = request.form.get("group_name", "").strip()
    ticker     = request.form.get("ticker", "").strip().upper()
    if group_name not in PEER_GROUPS:
        _flash(f"Group '{group_name}' not found.", f'分组\u201c{group_name}\u201d不存在。', "error")
    elif not ticker:
        _flash("Ticker symbol cannot be empty.", "股票代码不能为空。", "error")
    elif ticker in PEER_GROUPS[group_name]:
        _flash(f"{ticker} is already in '{group_name}'.", f'{ticker} 已在\u201c{group_name}\u201d中。', "error")
    else:
        PEER_GROUPS[group_name].append(ticker)
        _save_groups()
        _invalidate_cache()
        log.info("Added ticker %s to group '%s'", ticker, group_name)
        _flash(f"Added {ticker} to '{group_name}'.", f'已将 {ticker} 添加至\u201c{group_name}\u201d。', "success")
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
        _flash(f"Removed {ticker} from '{group_name}'.", f'已从\u201c{group_name}\u201d中移除 {ticker}。', "success")
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

_HISTORY_CACHE: Dict[tuple, dict] = {}
_HISTORY_CACHE_LOCK = threading.Lock()
_HISTORY_CACHE_TTL = 60 * 60   # 1 hour

def _get_institutional_holders(ticker: str) -> list:
    """Return per-fund multi-quarter position data for a ticker."""
    try:
        from ystocker.sec13f import get_all_holdings
        all_holdings = get_all_holdings()
        result = []
        for fund_name, fd in all_holdings.items():
            if fd.get("error"):
                continue
            quarters = fd.get("quarters") or []
            # Build per-quarter snapshot for this ticker
            fund_quarters = []
            for q in quarters:
                for h in q.get("holdings", []):
                    if h.get("ticker") == ticker:
                        fund_quarters.append({
                            "period":           q["period"],
                            "filing_date":      q["filing_date"],
                            "shares":           h["shares"],
                            "value_millions":   h["value_millions"],
                            "pct_portfolio":    h["pct_portfolio"],
                            "change":           h.get("change", "unknown"),
                            "change_pct":       h.get("change_pct"),
                            "rank":             h.get("rank"),
                        })
                        break
            if not fund_quarters:
                continue
            latest_q = fund_quarters[0]
            result.append({
                "fund":             fund_name,
                "rank":             latest_q["rank"],
                "shares":           latest_q["shares"],
                "value_millions":   latest_q["value_millions"],
                "pct_portfolio":    latest_q["pct_portfolio"],
                "change":           latest_q["change"],
                "change_pct":       latest_q.get("change_pct"),
                "quarters":         fund_quarters,   # newest first
            })
        result.sort(key=lambda x: x["value_millions"], reverse=True)
        return result
    except Exception:
        log.exception("Failed to get institutional holders for %s", ticker)
        return []

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

    Query params:
      period: 1mo | 3mo | 6mo | 1y | 2y | 5y  (default: 1y)
    """
    import yfinance as yf
    from flask import request as flask_request
    ticker = ticker.strip().upper()
    VALID_PERIODS = {"1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"}
    period = flask_request.args.get("period", "1y")
    if period not in VALID_PERIODS:
        period = "1y"
    if period in ("1mo", "3mo"):
        interval = "1d"
    elif period in ("6mo", "1y", "2y"):
        interval = "1wk"
    else:  # 5y, 10y
        interval = "1mo"
    log.info("API history: %s period=%s", ticker, period)

    cache_key = (ticker, period)
    with _HISTORY_CACHE_LOCK:
        entry = _HISTORY_CACHE.get(cache_key)
        if entry and time.time() - entry["ts"] < _HISTORY_CACHE_TTL:
            log.debug("History cache hit: %s period=%s", ticker, period)
            return jsonify(entry["data"])

    try:
        tk   = yf.Ticker(ticker)
        info = tk.info
        hist = tk.history(period=period, interval=interval)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    eps     = info.get("trailingEps")
    fwd_eps = info.get("forwardEps")
    name = info.get("shortName", ticker)

    if hist.empty:
        return jsonify({"error": f"No price history for '{ticker}'."}), 404

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

    # Forward PE history: price / forwardEps (constant analyst consensus)
    # Shows how the forward valuation multiple has expanded/compressed over time
    fwd_pe_history = []
    for p in prices:
        if p is not None and fwd_eps and fwd_eps > 0:
            fwd_pe_history.append(round(p / fwd_eps, 2))
        else:
            fwd_pe_history.append(None)

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

    # Options walls: strike with highest aggregate open interest across all expirations
    # Also compute put/call ratio = total put OI / total call OI
    # And per-expiration P/C ratios for the history chart
    call_wall = None
    put_wall  = None
    put_call_ratio = None
    pc_by_expiry: list = []   # [{exp, call_oi, put_oi, ratio}]
    try:
        expirations = tk.options  # tuple of expiration date strings
        call_oi: dict[float, int] = {}
        put_oi:  dict[float, int] = {}
        for exp in expirations:
            chain = tk.option_chain(exp)
            exp_call_oi = int(chain.calls["openInterest"].dropna().sum())
            exp_put_oi  = int(chain.puts["openInterest"].dropna().sum())
            if exp_call_oi > 0 or exp_put_oi > 0:
                pc_by_expiry.append({
                    "exp":      exp,
                    "call_oi":  exp_call_oi,
                    "put_oi":   exp_put_oi,
                    "ratio":    round(exp_put_oi / exp_call_oi, 2) if exp_call_oi > 0 else None,
                })
            for _, row in chain.calls[["strike", "openInterest"]].dropna().iterrows():
                s, oi = float(row["strike"]), int(row["openInterest"])
                call_oi[s] = call_oi.get(s, 0) + oi
            for _, row in chain.puts[["strike", "openInterest"]].dropna().iterrows():
                s, oi = float(row["strike"]), int(row["openInterest"])
                put_oi[s] = put_oi.get(s, 0) + oi
        if call_oi:
            call_wall = max(call_oi, key=call_oi.__getitem__)
        if put_oi:
            put_wall  = max(put_oi,  key=put_oi.__getitem__)
        total_call_oi = sum(call_oi.values())
        total_put_oi  = sum(put_oi.values())
        if total_call_oi > 0:
            put_call_ratio = round(total_put_oi / total_call_oi, 2)
    except Exception:
        log.warning("Could not fetch options walls for %s", ticker)

    result = {
        "ticker":           ticker,
        "name":             name,
        "dates":            dates,
        "prices":           prices,
        "pe_history":       pe_history,
        "fwd_pe_history":   fwd_pe_history,
        "peg_history":      peg_history,
        "current_pe":       current_pe,
        "current_peg":      current_peg,
        "forward_pe":       forward_pe,
        "target_price":     target_price,
        "eps":              _safe(eps),
        "eps_growth_ttm":   _safe(round(earnings_growth_ttm * 100, 1)) if earnings_growth_ttm is not None else None,
        "eps_growth_q":     _safe(round(earnings_growth_q   * 100, 1)) if earnings_growth_q   is not None else None,
        "ev_ebitda":        _safe(round(info.get("enterpriseToEbitda"), 1)) if info.get("enterpriseToEbitda") is not None else None,
        "ev":               _safe(round(info.get("enterpriseValue") / 1e9, 1)) if info.get("enterpriseValue") else None,
        "ebitda":           _safe(round(info.get("ebitda") / 1e9, 1)) if info.get("ebitda") else None,
        "institutional_holders": _get_institutional_holders(ticker),
        "call_wall":        _safe(call_wall),
        "put_wall":         _safe(put_wall),
        "put_call_ratio":   _safe(put_call_ratio),
        "pc_by_expiry":     pc_by_expiry,
    }
    with _HISTORY_CACHE_LOCK:
        _HISTORY_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# Annual financials endpoint (separate from history to avoid slowing PE chart)
# ---------------------------------------------------------------------------
_FINANCIALS_CACHE: Dict[str, dict] = {}
_FINANCIALS_CACHE_LOCK = threading.Lock()
_FINANCIALS_CACHE_TTL  = 6 * 60 * 60   # 6 hours — changes infrequently


@bp.route("/api/financials/<ticker>")
def api_financials(ticker: str):
    """
    Return annual income-statement actuals (3 years) plus forward estimates
    (2 years) for the given ticker. Kept separate from /api/history so the
    heavier income_stmt fetch does not block the PE / price charts.
    """
    import yfinance as yf
    import datetime as _dt
    ticker = ticker.strip().upper()

    with _FINANCIALS_CACHE_LOCK:
        entry = _FINANCIALS_CACHE.get(ticker)
        if entry and time.time() - entry["ts"] < _FINANCIALS_CACHE_TTL:
            return jsonify(entry["data"])

    def _to_b(v):
        try:
            f = float(v)
            return None if (f != f) else round(f / 1e9, 2)
        except Exception:
            return None

    def _to_f(v):
        try:
            f = float(v)
            return None if (f != f) else round(f, 2)
        except Exception:
            return None

    financials_table: list = []
    try:
        tk   = yf.Ticker(ticker)
        info = tk.info

        ROW_MAP = [
            ("Total Revenue",  "revenue"),
            ("Gross Profit",   "gross_profit"),
            ("EBITDA",         "ebitda_is"),
            ("Net Income",     "net_income"),
            ("Basic EPS",      "eps_basic"),
            ("Diluted EPS",    "eps_diluted"),
        ]

        actuals: dict = {}
        try:
            stmt = tk.income_stmt
            if stmt is not None and not stmt.empty:
                for col in list(stmt.columns)[:3]:
                    yr = str(col.year)
                    actuals[yr] = {}
                    for src_row, key in ROW_MAP:
                        if src_row in stmt.index:
                            raw = stmt.loc[src_row, col]
                            actuals[yr][key] = _to_b(raw) if key not in ("eps_basic", "eps_diluted") else _to_f(raw)
        except Exception as exc:
            log.warning("income_stmt fetch failed for %s: %s", ticker, exc)

        # Forward estimates from info dict
        est_eps_cyr     = info.get("epsCurrentYear")
        est_eps_nyr     = info.get("epsNextYear")
        est_rev_cyr     = info.get("revenueEstimatesCurrentYear")
        est_rev_nyr     = info.get("revenueEstimatesNextYear")

        # Fallback via eps_trend
        try:
            et = tk.eps_trend
            if et is not None and not et.empty:
                if est_eps_cyr is None and "current" in et.index and "current" in et.columns:
                    est_eps_cyr = _to_f(et.loc["current", "current"])
                if est_eps_nyr is None and "next" in et.index and "current" in et.columns:
                    est_eps_nyr = _to_f(et.loc["next", "current"])
        except Exception:
            pass

        cur_year  = _dt.date.today().year
        fwd_years = [str(cur_year), str(cur_year + 1)]

        fwd: dict = {}
        for yr, eps_v, rev_v in [(fwd_years[0], est_eps_cyr, est_rev_cyr),
                                  (fwd_years[1], est_eps_nyr, est_rev_nyr)]:
            if eps_v is not None:
                fwd.setdefault(yr, {})["eps_est"] = _to_f(eps_v)
            if rev_v is not None:
                fwd.setdefault(yr, {})["revenue_est"] = _to_b(rev_v) if rev_v > 1e6 else _to_f(rev_v)

        all_years = sorted(actuals.keys(), reverse=True) + [y for y in fwd_years if y not in actuals]
        for yr in all_years:
            row = {"year": yr, "is_estimate": yr in fwd_years and yr not in actuals}
            row.update(actuals.get(yr, {}))
            row.update(fwd.get(yr, {}))
            financials_table.append(row)

    except Exception as exc:
        log.warning("api_financials failed for %s: %s", ticker, exc)

    result = {"ticker": ticker, "financials_table": financials_table}
    with _FINANCIALS_CACHE_LOCK:
        _FINANCIALS_CACHE[ticker] = {"ts": time.time(), "data": result}
    return jsonify(result)

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


@bp.route("/guide")
def guide():
    return render_template("guide.html", peer_groups=list(PEER_GROUPS.keys()))


@bp.route("/videos")
def videos():
    from ystocker import YT_CHANNELS
    return render_template("videos.html", peer_groups=list(PEER_GROUPS.keys()),
                           yt_channels=YT_CHANNELS)


# ---------------------------------------------------------------------------
# Federal Reserve H.4.1 balance-sheet page
# ---------------------------------------------------------------------------

@bp.route("/fed")
def fed():
    """Page showing Federal Reserve balance-sheet (H.4.1) data."""
    log.info("GET /fed")
    from ystocker.fed import get_cache_ts, is_cache_fresh, is_warming as fed_warming_fn, SERIES
    return render_template(
        "fed.html",
        peer_groups=list(PEER_GROUPS.keys()),
        series_meta=SERIES,
        cache_last_updated=get_cache_ts(),
        cache_fresh=is_cache_fresh(),
        warming=fed_warming_fn(),
    )


@bp.route("/fed/refresh")
def fed_refresh():
    """Kick off a background re-fetch of Fed H.4.1 data."""
    from ystocker.fed import refresh_cache
    threading.Thread(target=refresh_cache, daemon=True, name="fed-manual-refresh").start()
    return redirect(url_for("main.fed"))


@bp.route("/api/fed")
def api_fed():
    """JSON API — return Fed H.4.1 balance-sheet time-series data.

    If no cache exists yet, kick off a background fetch and return 202 so the
    page shows a loading state rather than blocking the request thread.
    """
    from ystocker.fed import get_fed_data, is_cache_fresh, is_warming as fed_warming_fn, refresh_cache

    # Fresh cache available — return immediately.
    if is_cache_fresh():
        data = get_fed_data()
        resp = {k: v for k, v in data.items() if not k.startswith("_")}
        return jsonify(resp)

    # A background fetch is already running — tell the client to retry.
    if fed_warming_fn():
        return jsonify({"warming": True}), 202

    # No cache and no fetch in progress — start one in the background.
    threading.Thread(target=refresh_cache, daemon=True, name="fed-auto-warm").start()
    return jsonify({"warming": True}), 202


@bp.route("/api/fed/explain", methods=["POST"])
def api_fed_explain():
    """Stream an AI explanation of a Fed chart's recent data via SSE."""
    import os
    from google import genai

    body = request.get_json(force=True, silent=True) or {}
    chart   = body.get("chart", "")
    dates   = body.get("dates", [])
    values  = body.get("values", [])
    label   = body.get("label", chart)
    lang    = body.get("lang", "en")

    if not dates or not values:
        return jsonify({"error": "No data provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    # Build a compact data summary (last 12 points + overall trend)
    pairs = [(d, v) for d, v in zip(dates, values) if v is not None]
    if not pairs:
        return jsonify({"error": "No valid data points"}), 400

    # For the PCT chart the values are percentages, not billions
    is_pct = (chart == "pct")

    first_date, first_val = pairs[0]
    last_date,  last_val  = pairs[-1]
    recent = pairs[-12:]  # last ~3 months of weekly data
    if is_pct:
        recent_lines = "\n".join(f"  {d}: {v:.1f}%" for d, v in recent)
        period_summary = f"{first_date} ({first_val:.1f}%) → {last_date} ({last_val:.1f}%)\nTotal change: {last_val - first_val:+.1f}pp"
    else:
        recent_lines = "\n".join(f"  {d}: ${v:.1f}B" for d, v in recent)
        period_summary = f"{first_date} (${first_val:.1f}B) → {last_date} (${last_val:.1f}B)\nTotal change: {last_val - first_val:+.1f}B ({(last_val - first_val) / first_val * 100:+.1f}%)"

    chart_descriptions = {
        "treasury":  "U.S. Treasury Securities Held Outright by the Federal Reserve (weekly, billions USD)",
        "bills":     "Short-term Treasury Bills (≤1 year maturity) held by the Federal Reserve (weekly, billions USD)",
        "balance":   "Federal Reserve Balance Sheet Overview — Total Assets, MBS holdings, and Reserve Balances (weekly, billions USD)",
        "pct":       "U.S. Treasury Securities as a percentage of Total Federal Reserve Assets (weekly, %)",
    }
    description = chart_descriptions.get(chart, label)

    prompt = f"""You are a macroeconomic analyst. Explain the following Federal Reserve balance sheet data to a financial market participant in 3-4 concise paragraphs.{"  Respond in Simplified Chinese (中文)." if lang == "zh" else ""}

Chart: {description}
Full period: {period_summary}

Most recent 12 data points:
{recent_lines}

Cover: (1) what the overall trend shows, (2) any notable recent moves, (3) what this means for monetary policy or market conditions. Be specific about the numbers. Do not use headers or bullet points."""

    client = genai.Client(api_key=api_key)

    def generate():
        try:
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash", contents=prompt
            )
            for chunk in stream:
                text = chunk.text
                if text:
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as exc:
            log.error("Fed explain error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# 13F Institutional Holdings
# ---------------------------------------------------------------------------

@bp.route("/13f")
def thirteenf():
    """Page showing latest 13F holdings for top institutional investors."""
    log.info("GET /13f")
    from ystocker.sec13f import (
        get_all_holdings, FUNDS, is_cache_fresh, get_cache_ts, is_warming as sec_warming
    )
    holdings   = get_all_holdings()
    cache_ts   = get_cache_ts()
    warming    = sec_warming()
    return render_template(
        "thirteenf.html",
        peer_groups=list(PEER_GROUPS.keys()),
        funds=FUNDS,
        holdings=holdings,
        cache_last_updated=cache_ts,
        cache_fresh=is_cache_fresh(),
        warming=warming,
    )


@bp.route("/13f/refresh")
def thirteenf_refresh():
    """Kick off a background re-fetch of all 13F holdings."""
    from ystocker.sec13f import refresh_cache
    threading.Thread(target=refresh_cache, daemon=True, name="sec13f-manual-refresh").start()
    return redirect(url_for("main.thirteenf"))


@bp.route("/api/13f/<path:fund_slug>")
def api_thirteenf(fund_slug: str):
    """JSON API — return holdings for a single fund by slug."""
    from ystocker.sec13f import get_all_holdings, FUNDS
    holdings = get_all_holdings()
    name = next(
        (n for n in FUNDS if n.lower().replace(" ", "-") == fund_slug.lower()),
        None
    )
    if not name:
        return jsonify({"error": "Fund not found"}), 404
    return jsonify(holdings.get(name, {}))


@bp.route("/api/13f/ticker/<ticker>")
def api_thirteenf_ticker(ticker: str):
    """JSON API — multi-quarter institutional holdings for a single ticker."""
    ticker = ticker.strip().upper()
    holders = _get_institutional_holders(ticker)
    return jsonify({"ticker": ticker, "holders": holders})


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

_NEWS_CACHE: Dict[str, dict] = {}
_NEWS_CACHE_LOCK = threading.Lock()
_NEWS_CACHE_TTL = 5 * 60   # 5 minutes

# Keywords that indicate important/high-impact news
_IMPORTANT_KEYWORDS = [
    "earnings", "revenue", "guidance", "outlook", "forecast",
    "beats", "misses", "beat", "miss", "eps", "profit", "loss",
    "upgrade", "downgrade", "outperform", "underperform",
    "price target", "target price", "analyst",
    "merger", "acquisition", "buyout", "deal", "takeover",
    "dividend", "buyback", "split",
    "fda", "approval", "approved", "rejected",
    "layoff", "layoffs", "ceo", "cfo", "executive",
    "lawsuit", "investigation", "sec",
    "record", "all-time", "ipo",
]

def _is_important(title: str) -> bool:
    lower = title.lower()
    return any(kw in lower for kw in _IMPORTANT_KEYWORDS)


@bp.route("/api/history/<ticker>/explain", methods=["POST"])
def api_history_explain(ticker: str):
    """Stream a Gemini AI explanation of a history chart via SSE.

    Results are cached to disk (cache/explain/) for 8 hours so repeated
    requests for the same ticker/chart/period/lang are served instantly.
    """
    import os
    from google import genai

    ticker = ticker.strip().upper()
    body   = request.get_json(force=True, silent=True) or {}
    chart  = body.get("chart", "")       # pe | price | peg | fwdpe
    dates  = body.get("dates",  [])
    values = body.get("values", [])
    period = body.get("period", "1y")
    lang   = body.get("lang",   "en")

    if not dates or not values:
        return jsonify({"error": "No data provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    pairs = [(d, v) for d, v in zip(dates, values) if v is not None]
    if not pairs:
        return jsonify({"error": "No valid data points"}), 400

    # ── Disk cache ────────────────────────────────────────────────────────────
    _EXPLAIN_CACHE_DIR = Path(__file__).parent.parent / "cache" / "explain"
    _EXPLAIN_CACHE_TTL = 8 * 60 * 60   # 8 hours, matches main data cache

    safe_ticker = ticker.replace("/", "-")
    cache_file  = _EXPLAIN_CACHE_DIR / f"{safe_ticker}_{chart}_{period}_{lang}.json"

    try:
        if cache_file.exists():
            payload = json.loads(cache_file.read_text())
            age = time.time() - payload.get("ts", 0)
            if age < _EXPLAIN_CACHE_TTL:
                cached_text = payload.get("text", "")
                if cached_text:
                    log.debug("Explain cache hit: %s/%s period=%s lang=%s", ticker, chart, period, lang)

                    def stream_cached():
                        # Emit the full text as a single chunk then DONE
                        yield f"data: {json.dumps({'text': cached_text})}\n\n"
                        yield "data: [DONE]\n\n"

                    return Response(stream_cached(), mimetype="text/event-stream", headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    })
    except Exception:
        log.debug("Explain cache read failed for %s/%s — will re-generate", ticker, chart)

    # ── Build prompt ──────────────────────────────────────────────────────────
    first_date, first_val = pairs[0]
    last_date,  last_val  = pairs[-1]
    recent = pairs[-12:]

    chart_meta = {
        "pe":    ("PE Ratio (TTM)",        "x",  lambda v: f"{v:.1f}x"),
        "fwdpe": ("Forward PE Ratio",      "x",  lambda v: f"{v:.1f}x"),
        "peg":   ("PEG Ratio",             "",   lambda v: f"{v:.2f}"),
        "price": ("Stock Price (USD)",     "$",  lambda v: f"${v:.2f}"),
    }
    label, _unit, fmt = chart_meta.get(chart, (chart, "", lambda v: f"{v:.2f}"))

    recent_lines  = "\n".join(f"  {d}: {fmt(v)}" for d, v in recent)
    period_change = last_val - first_val
    pct_change    = period_change / first_val * 100 if first_val else 0
    period_summary = (
        f"{first_date} ({fmt(first_val)}) → {last_date} ({fmt(last_val)})\n"
        f"Total change: {'+' if period_change >= 0 else ''}{fmt(period_change)} "
        f"({pct_change:+.1f}%)"
    )

    chart_hints = {
        "pe":    "Focus on whether the stock looks cheap or expensive relative to its historical PE range, and what drives the multiple expansion or compression.",
        "fwdpe": "Focus on how the market's forward earnings expectations have repriced over time and what that implies for valuation.",
        "peg":   "Focus on whether the PEG suggests the growth-adjusted valuation is attractive (below 1) or stretched (above 2).",
        "price": "Focus on price trend, key support/resistance levels implied by the data, and momentum.",
    }
    hint = chart_hints.get(chart, "")

    prompt = (
        f"You are a sell-side equity analyst. Explain the following {label} chart for {ticker} "
        f"over the {period} period in 2-3 concise paragraphs."
        f"{'  Respond in Simplified Chinese (中文).' if lang == 'zh' else ''}\n\n"
        f"Chart: {label} for {ticker}\n"
        f"Period: {period}\n"
        f"Full period: {period_summary}\n\n"
        f"Most recent 12 data points:\n{recent_lines}\n\n"
        f"{hint}\n"
        f"Be specific about the numbers. Do not use headers or bullet points."
    )

    client = genai.Client(api_key=api_key)

    def generate():
        accumulated = []
        try:
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash", contents=prompt
            )
            for chunk in stream:
                text = chunk.text
                if text:
                    accumulated.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as exc:
            log.error("History explain error for %s/%s: %s", ticker, chart, exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        else:
            # Persist to disk only on clean completion
            full_text = "".join(accumulated)
            if full_text:
                try:
                    _EXPLAIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = cache_file.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"ts": time.time(), "text": full_text}))
                    tmp.replace(cache_file)
                    log.debug("Explain cached: %s/%s period=%s lang=%s", ticker, chart, period, lang)
                except Exception:
                    log.debug("Failed to write explain cache for %s/%s", ticker, chart)
        finally:
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@bp.route("/api/news/<ticker>")
def api_news(ticker: str):
    """
    JSON API - return recent news articles for a ticker via yfinance.
    Results are sorted newest-first. Cache TTL: 5 minutes.
    """
    import yfinance as yf
    ticker = ticker.strip().upper()
    log.info("API news: %s", ticker)

    from flask import request as flask_request
    force_refresh = flask_request.args.get("force") == "1"
    with _NEWS_CACHE_LOCK:
        entry = _NEWS_CACHE.get(ticker)
        if not force_refresh and entry and time.time() - entry["ts"] < _NEWS_CACHE_TTL:
            log.debug("News cache hit: %s", ticker)
            return jsonify(entry["data"])

    try:
        tk = yf.Ticker(ticker)
        raw_news = tk.news or []
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    articles = []
    for item in raw_news:
        try:
            content = item.get("content") or {}
            if not isinstance(content, dict):
                content = {}
            # yfinance >= 0.2.x nests fields under "content"
            title     = content.get("title") or item.get("title", "")
            pub_date  = content.get("pubDate") or item.get("providerPublishTime")
            provider_obj = content.get("provider") or {}
            if not isinstance(provider_obj, dict):
                provider_obj = {}
            provider  = provider_obj.get("displayName") or item.get("publisher", "")
            canonical = content.get("canonicalUrl") or {}
            if not isinstance(canonical, dict):
                canonical = {}
            link      = canonical.get("url") or item.get("link", "")
            summary   = content.get("summary") or item.get("summary", "")
            thumbnail = None
            thumb_obj = content.get("thumbnail") or {}
            if not isinstance(thumb_obj, dict):
                thumb_obj = {}
            thumb_list = thumb_obj.get("resolutions") or []
            if thumb_list:
                thumbnail = thumb_list[0].get("url")
            elif isinstance(item.get("thumbnail"), dict):
                resolutions = item["thumbnail"].get("resolutions") or []
                if resolutions:
                    thumbnail = resolutions[0].get("url")
        except Exception:
            continue

        # Normalise pub_date to a unix timestamp int
        if isinstance(pub_date, str):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                pub_date = int(dt.timestamp())
            except Exception:
                pub_date = None

        if not title or not link:
            continue

        articles.append({
            "title":     title,
            "publisher": provider,
            "link":      link,
            "published": pub_date,
            "summary":   summary,
            "thumbnail": thumbnail,
            "important": _is_important(title),
        })

    # Sort newest-first
    articles.sort(key=lambda a: a["published"] or 0, reverse=True)

    result = {"ticker": ticker, "articles": articles}
    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE[ticker] = {"ts": time.time(), "data": result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# News translation  (Gemini batch translate)
# ---------------------------------------------------------------------------

# Cache: key = frozenset of article links → translated list
_TRANS_CACHE: dict = {}
_TRANS_CACHE_LOCK = threading.Lock()
_TRANS_CACHE_TTL  = 3600 * 12   # 12 hours — translations don't change

_DYNAMO_TABLE_NAME = "ystocker-news-translations"
_dynamo_table      = None   # boto3 Table resource, lazily created
_DYNAMO_LOCK       = threading.Lock()
_dynamo_unavail_until = 0.0  # retry backoff: don't retry before this timestamp


def _get_dynamo_table():
    """Return a cached boto3 DynamoDB Table resource, or None if unavailable.
    On failure, backs off for 5 minutes before retrying (so a transient error
    at startup doesn't permanently disable DynamoDB for the process lifetime).
    """
    global _dynamo_table, _dynamo_unavail_until
    if _dynamo_table is not None:
        return _dynamo_table
    if time.time() < _dynamo_unavail_until:
        return None   # still in backoff window
    with _DYNAMO_LOCK:
        if _dynamo_table is not None:
            return _dynamo_table
        if time.time() < _dynamo_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            _dynamo_table = ddb.Table(_DYNAMO_TABLE_NAME)
            _dynamo_table.load()   # validates table exists; raises if not
            log.info("DynamoDB translation table connected: %s", _DYNAMO_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB unavailable — translations use memory-only cache: %s", exc)
            _dynamo_table = None
            _dynamo_unavail_until = time.time() + 300  # retry in 5 minutes
        return _dynamo_table


def _ddb_batch_get(links: list) -> dict:
    """Fetch translations from DynamoDB for the given links.
    Returns {link: {title_zh, summary_zh}} for found items.
    """
    table = _get_dynamo_table()
    if not table or not links:
        return {}
    results = {}
    # batch_get_item can handle up to 100 keys per call
    for i in range(0, len(links), 100):
        chunk = links[i:i+100]
        try:
            resp = table.meta.client.batch_get_item(
                RequestItems={
                    _DYNAMO_TABLE_NAME: {
                        "Keys": [{"link": lnk} for lnk in chunk],
                        "ProjectionExpression": "#lk, title_zh, summary_zh",
                        "ExpressionAttributeNames": {"#lk": "link"},
                    }
                }
            )
            for item in resp.get("Responses", {}).get(_DYNAMO_TABLE_NAME, []):
                results[item["link"]] = {
                    "title_zh":   item.get("title_zh"),
                    "summary_zh": item.get("summary_zh"),
                }
        except Exception as exc:
            log.warning("DynamoDB batch_get failed: %s", exc)
    return results


def _ddb_batch_put(items: list) -> None:
    """Write translated articles to DynamoDB. items: [{link, title_zh, summary_zh}]"""
    table = _get_dynamo_table()
    if not table or not items:
        return
    ts = Decimal(str(time.time()))
    try:
        with table.batch_writer() as batch:
            for item in items:
                if not item.get("link"):
                    continue
                record = {"link": item["link"], "title_zh": item["title_zh"], "ts": ts}
                if item.get("summary_zh"):
                    record["summary_zh"] = item["summary_zh"]
                batch.put_item(Item=record)
    except Exception as exc:
        log.warning("DynamoDB batch_put failed: %s", exc)


@bp.route("/api/news/translate", methods=["POST"])
def api_news_translate():
    """
    Batch-translate news article titles and summaries to Chinese using Gemini.

    Request body:
      { "articles": [{"link": str, "title": str, "summary": str|null}, ...],
        "lang": "zh" }

    Response:
      { "translations": [{"link": str, "title_zh": str, "summary_zh": str|null}, ...] }
    """
    from google import genai

    body = request.get_json(force=True, silent=True) or {}
    articles = body.get("articles", [])
    lang     = body.get("lang", "zh")

    if not articles:
        return jsonify({"translations": []})

    if lang != "zh":
        # Only Chinese supported for now
        return jsonify({"translations": [
            {"link": a.get("link"), "title_zh": a.get("title"), "summary_zh": a.get("summary")}
            for a in articles
        ]})

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    # L1: memory cache check
    with _TRANS_CACHE_LOCK:
        cached_map = dict(_TRANS_CACHE)  # link → {title_zh, summary_zh}

    to_translate = [a for a in articles if a.get("link") not in cached_map]

    # L2: DynamoDB check for articles not in memory cache
    if to_translate:
        ddb_links = [a["link"] for a in to_translate if a.get("link")]
        ddb_hits  = _ddb_batch_get(ddb_links)
        if ddb_hits:
            with _TRANS_CACHE_LOCK:
                for lnk, t in ddb_hits.items():
                    _TRANS_CACHE[lnk] = {"title_zh": t["title_zh"], "summary_zh": t["summary_zh"], "ts": time.time()}
            cached_map.update(ddb_hits)
            to_translate = [a for a in to_translate if a.get("link") not in ddb_hits]

    already_done = [
        {"link": a["link"], "title_zh": cached_map[a["link"]]["title_zh"],
         "summary_zh": cached_map[a["link"]]["summary_zh"]}
        for a in articles if a.get("link") in cached_map
    ]

    if not to_translate:
        return jsonify({"translations": already_done})

    # Build a compact numbered list for Gemini to translate in one shot
    lines = []
    for i, a in enumerate(to_translate):
        title   = (a.get("title")   or "").replace("\n", " ").strip()
        summary = (a.get("summary") or "").replace("\n", " ").strip()
        lines.append(f"{i+1}. TITLE: {title}")
        if summary:
            lines.append(f"   SUMMARY: {summary}")

    prompt = (
        "Translate the following financial news headlines and summaries from English to Simplified Chinese (简体中文). "
        "Preserve the original meaning precisely. Use financial terminology naturally. "
        "Return ONLY a JSON array with the same number of objects as the input, in the same order. "
        "Each object must have keys: \"title_zh\" (string) and \"summary_zh\" (string or null if no summary was given). "
        "Output nothing except valid JSON.\n\n"
        + "\n".join(lines)
    )

    try:
        client = genai.Client(api_key=api_key)
        resp   = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        raw = resp.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        translated = json.loads(raw)
    except Exception as exc:
        log.warning("News translation failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    if not isinstance(translated, list) or len(translated) != len(to_translate):
        return jsonify({"error": "Gemini returned unexpected format"}), 500

    # Merge with links and cache
    new_results = []
    with _TRANS_CACHE_LOCK:
        for a, t in zip(to_translate, translated):
            link = a.get("link", "")
            entry = {
                "link":       link,
                "title_zh":   t.get("title_zh")   or a.get("title"),
                "summary_zh": t.get("summary_zh")  or None,
                "ts":         time.time(),
            }
            if link:
                _TRANS_CACHE[link] = entry
            new_results.append({"link": link, "title_zh": entry["title_zh"], "summary_zh": entry["summary_zh"]})

    # Persist new translations to DynamoDB
    _ddb_batch_put(new_results)

    # Merge with already-cached results
    order_map = {a.get("link"): i for i, a in enumerate(articles)}
    all_results = already_done + new_results
    all_results.sort(key=lambda r: order_map.get(r.get("link"), 999))

    return jsonify({"translations": all_results})


# ---------------------------------------------------------------------------
# YouTube videos
# ---------------------------------------------------------------------------

_VIDEOS_CACHE: Dict[str, dict] = {}
_VIDEOS_CACHE_LOCK = threading.Lock()
_VIDEOS_CACHE_TTL = 30 * 60   # 30 minutes


def _iso_duration_to_str(iso: str) -> str:
    """Convert ISO 8601 duration like PT4M33S to '4:33'."""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return ""
    h, mn, s = (int(x or 0) for x in m.groups())
    if h:
        return f"{h}:{mn:02d}:{s:02d}"
    return f"{mn}:{s:02d}"


@bp.route("/api/videos/<ticker>")
def api_videos(ticker: str):
    """Return recent YouTube videos for a ticker from curated channels (past ~7 days).

    Requires YOUTUBE_API_KEY environment variable (YouTube Data API v3).
    Returns {"videos": [...]} or {"videos": [], "note": "..."}.
    """
    import httpx
    from datetime import datetime, timezone, timedelta

    ticker = ticker.strip().upper()

    # Cache check
    with _VIDEOS_CACHE_LOCK:
        cached = _VIDEOS_CACHE.get(ticker)
        if cached and time.time() - cached["ts"] < _VIDEOS_CACHE_TTL:
            return jsonify(cached["data"])

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        result = {"ticker": ticker, "videos": [],
                  "note": "YOUTUBE_API_KEY not set"}
        return jsonify(result)

    # Use httpx which correctly handles the system proxy (unlike urllib which
    # tries a CONNECT tunnel that the local proxy rejects)
    http = httpx.Client(timeout=10)

    published_after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Search each curated channel for recent videos (no ticker filter —
    # these channels discuss stocks in Chinese, not by ticker symbol)
    all_items: list = []
    for _handle, channel_id, _name in YT_CHANNELS:
        try:
            resp = http.get("https://www.googleapis.com/youtube/v3/search", params={
                "part": "snippet",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "publishedAfter": published_after,
                "maxResults": 3,
                "key": api_key,
            })
            resp.raise_for_status()
            all_items.extend(resp.json().get("items", []))
        except Exception as e:
            log.warning("YouTube search failed for channel %s / %s: %s", _handle, ticker, e)

    if not all_items:
        result = {"ticker": ticker, "videos": []}
        with _VIDEOS_CACHE_LOCK:
            _VIDEOS_CACHE[ticker] = {"ts": time.time(), "data": result}
        return jsonify(result)

    # Fetch video durations via videos.list
    video_ids = [it["id"]["videoId"] for it in all_items if it.get("id", {}).get("videoId")]
    duration_map: Dict[str, str] = {}
    if video_ids:
        try:
            resp = http.get("https://www.googleapis.com/youtube/v3/videos", params={
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": api_key,
            })
            resp.raise_for_status()
            for vi in resp.json().get("items", []):
                vid_id = vi["id"]
                iso = vi.get("contentDetails", {}).get("duration", "")
                duration_map[vid_id] = _iso_duration_to_str(iso)
        except Exception as e:
            log.warning("YouTube video details failed for %s: %s", ticker, e)

    seen: set = set()
    videos = []
    for it in all_items:
        vid_id = (it.get("id") or {}).get("videoId") if isinstance(it.get("id"), dict) else (it.get("id") or None)
        if not vid_id or vid_id in seen:
            continue
        seen.add(vid_id)
        snippet = it.get("snippet", {})
        pub_str = snippet.get("publishedAt", "")
        pub_ts: Optional[int] = None
        try:
            dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            pub_ts = int(dt.timestamp())
        except Exception:
            pass
        videos.append({
            "id":        vid_id,
            "title":     snippet.get("title", ""),
            "channel":   snippet.get("channelTitle", ""),
            "published": pub_ts,
            "duration":  duration_map.get(vid_id, ""),
        })

    # Sort newest first
    videos.sort(key=lambda v: v["published"] or 0, reverse=True)

    result = {"ticker": ticker, "videos": videos}
    with _VIDEOS_CACHE_LOCK:
        _VIDEOS_CACHE[ticker] = {"ts": time.time(), "data": result}
    return jsonify(result)


@bp.route("/api/videos/channel/<channel_id>")
def api_videos_channel(channel_id: str):
    """Return recent videos for a single YT channel (standalone videos page)."""
    import httpx
    from datetime import datetime, timezone, timedelta

    cache_key = f"channel:{channel_id}"
    with _VIDEOS_CACHE_LOCK:
        cached = _VIDEOS_CACHE.get(cache_key)
        if cached and time.time() - cached["ts"] < _VIDEOS_CACHE_TTL:
            return jsonify(cached["data"])

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return jsonify({"videos": [], "note": "YOUTUBE_API_KEY not set"})

    http = httpx.Client(timeout=10)
    published_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        resp = http.get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "publishedAfter": published_after,
            "maxResults": 12,
            "key": api_key,
        })
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:
        log.warning("YouTube channel fetch failed for %s: %s", channel_id, e)
        return jsonify({"videos": [], "error": str(e)})

    seen: set = set()
    videos = []
    for it in items:
        vid_id = (it.get("id") or {}).get("videoId") if isinstance(it.get("id"), dict) else (it.get("id") or None)
        if not vid_id or vid_id in seen:
            continue
        seen.add(vid_id)
        snippet = it.get("snippet", {})
        pub_str = snippet.get("publishedAt", "")
        pub_ts = None
        try:
            dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            pub_ts = int(dt.timestamp())
        except Exception:
            pass
        videos.append({
            "id":        vid_id,
            "title":     snippet.get("title", ""),
            "channel":   snippet.get("channelTitle", ""),
            "published": pub_ts,
        })
    videos.sort(key=lambda v: v["published"] or 0, reverse=True)
    result = {"channel_id": channel_id, "videos": videos}
    with _VIDEOS_CACHE_LOCK:
        _VIDEOS_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return jsonify(result)


@bp.route("/api/videos/all")
def api_videos_all():
    """Return recent videos from all curated channels sorted by publish time.

    Preferred channels (first half of YT_CHANNELS list) are fetched first and
    their videos float to the top when publish timestamps are equal.
    """
    import httpx
    from datetime import datetime, timezone, timedelta

    cache_key = "all_channels"
    with _VIDEOS_CACHE_LOCK:
        cached = _VIDEOS_CACHE.get(cache_key)
        if cached and time.time() - cached["ts"] < _VIDEOS_CACHE_TTL:
            return jsonify(cached["data"])

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return jsonify({"videos": [], "note": "YOUTUBE_API_KEY not set"})

    http = httpx.Client(timeout=10)
    published_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Mark preferred channels (first half of the list)
    preferred_ids = {ch[1] for ch in YT_CHANNELS[: len(YT_CHANNELS) // 2 + 1]}

    all_items: list = []
    for _handle, channel_id, _name in YT_CHANNELS:
        try:
            resp = http.get("https://www.googleapis.com/youtube/v3/search", params={
                "part": "snippet",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "publishedAfter": published_after,
                "maxResults": 5,
                "key": api_key,
            })
            resp.raise_for_status()
            items = resp.json().get("items", [])
            for it in items:
                it["_preferred"] = channel_id in preferred_ids
            all_items.extend(items)
        except Exception as e:
            log.warning("YouTube all-channels fetch failed for %s: %s", _handle, e)

    seen: set = set()
    videos = []
    for it in all_items:
        vid_id = (it.get("id") or {}).get("videoId") if isinstance(it.get("id"), dict) else (it.get("id") or None)
        if not vid_id or vid_id in seen:
            continue
        seen.add(vid_id)
        snippet = it.get("snippet", {})
        pub_str = snippet.get("publishedAt", "")
        pub_ts = None
        try:
            dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            pub_ts = int(dt.timestamp())
        except Exception:
            pass
        videos.append({
            "id":        vid_id,
            "title":     snippet.get("title", ""),
            "channel":   snippet.get("channelTitle", ""),
            "published": pub_ts,
            "preferred": it.get("_preferred", False),
        })

    # Sort: primary = publish time (newest first), secondary = preferred channels first
    videos.sort(key=lambda v: (-(v["published"] or 0), not v["preferred"]))

    result = {"videos": videos}
    with _VIDEOS_CACHE_LOCK:
        _VIDEOS_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# Forecast API
# ---------------------------------------------------------------------------

_FORECAST_CACHE: dict = {}
_FORECAST_CACHE_LOCK = threading.Lock()
_FORECAST_CACHE_TTL  = 3600 * 6  # 6 hours — models are slow


@bp.route("/api/forecast/<ticker>")
def api_forecast(ticker: str):
    """Run multi-model price forecast for *ticker*. Results cached 6 h."""
    ticker = ticker.strip().upper()
    with _FORECAST_CACHE_LOCK:
        entry = _FORECAST_CACHE.get(ticker)
        if entry and time.time() - entry["ts"] < _FORECAST_CACHE_TTL:
            log.debug("Forecast cache hit: %s", ticker)
            return jsonify(entry["data"])

    from ystocker.forecast import run_forecast
    log.info("Running forecast for %s", ticker)
    result = run_forecast(ticker)

    if "error" not in result:
        with _FORECAST_CACHE_LOCK:
            _FORECAST_CACHE[ticker] = {"ts": time.time(), "data": result}

    return jsonify(result)


# ---------------------------------------------------------------------------
# Market indices page  (/markets)
# ---------------------------------------------------------------------------

_MARKETS_CACHE: dict = {}
_MARKETS_CACHE_LOCK  = threading.Lock()
_MARKETS_CACHE_TTL   = 300  # 5 minutes

# DynamoDB table for persisting the markets snapshot across restarts
_MARKETS_TABLE_NAME    = "ystocker-markets-cache"
_markets_ddb_table     = None
_markets_ddb_unavail_until = 0.0
_MARKETS_DDB_LOCK      = threading.Lock()


def _get_markets_ddb_table():
    """Return boto3 DynamoDB Table for markets cache, or None if unavailable."""
    global _markets_ddb_table, _markets_ddb_unavail_until
    if _markets_ddb_table is not None:
        return _markets_ddb_table
    if time.time() < _markets_ddb_unavail_until:
        return None
    with _MARKETS_DDB_LOCK:
        if _markets_ddb_table is not None:
            return _markets_ddb_table
        if time.time() < _markets_ddb_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(_MARKETS_TABLE_NAME)
            tbl.load()
            _markets_ddb_table = tbl
            log.info("DynamoDB markets-cache table connected: %s", _MARKETS_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB markets-cache unavailable: %s", exc)
            _markets_ddb_table = None
            _markets_ddb_unavail_until = time.time() + 300
        return _markets_ddb_table


def _markets_load_from_dynamo() -> Optional[dict]:
    """Load the cached markets snapshot from DynamoDB. Returns None if stale/missing."""
    table = _get_markets_ddb_table()
    if not table:
        return None
    try:
        resp = table.get_item(Key={"pk": "snapshot"})
        item = resp.get("Item")
        if not item:
            return None
        ts = float(item.get("ts", 0))
        if time.time() - ts > _MARKETS_CACHE_TTL:
            return None  # stale
        payload = item.get("payload")
        if not payload:
            return None
        return {"ts": ts, "data": json.loads(payload)}
    except Exception as exc:
        log.warning("DynamoDB markets-cache load failed: %s", exc)
        return None


def _markets_save_to_dynamo(result: dict, ts: float) -> None:
    """Persist the markets snapshot to DynamoDB with a TTL of 5 minutes."""
    table = _get_markets_ddb_table()
    if not table:
        return
    try:
        table.put_item(Item={
            "pk":      "snapshot",
            "ts":      Decimal(str(round(ts, 3))),
            "payload": json.dumps(result, default=str),
            "ttl":     int(ts) + _MARKETS_CACHE_TTL + 60,  # DynamoDB native TTL
        })
    except Exception as exc:
        log.warning("DynamoDB markets-cache save failed: %s", exc)

# Yahoo Finance symbols for major indices (US + international)
_INDEX_SYMBOLS = {
    "spx":   "^GSPC",     # S&P 500
    "ixic":  "^IXIC",     # Nasdaq Composite
    "dji":   "^DJI",      # Dow Jones
    "ftse":  "^FTSE",     # FTSE 100
    "n225":  "^N225",     # Nikkei 225
    "sse":   "000001.SS", # Shanghai Composite
    "twii":  "^TWII",     # Taiwan Weighted Index
    "kospi": "^KS11",     # KOSPI
}

# SPDR sector ETFs used for sector performance
_SECTOR_ETFS = {
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Consumer Disc.",
    "XLP": "Consumer Stap.", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate",
}


@bp.route("/markets")
def markets():
    """Market overview page — the application home."""
    return render_template("markets.html",
                           peer_groups=list(PEER_GROUPS.keys()))


@bp.route("/api/markets")
def api_markets():
    """
    JSON snapshot for the markets page.

    Returns live data for ^GSPC, ^IXIC, ^DJI plus:
      - 1-year weekly price history per index
      - 50-day and 200-day moving averages (last value)
      - RSI-14 (last value)
      - VIX snapshot (^VIX)
      - SPDR sector ETF day-change percentages
    """
    import yfinance as yf
    import numpy as np
    from datetime import date

    with _MARKETS_CACHE_LOCK:
        entry = _MARKETS_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _MARKETS_CACHE_TTL:
            return jsonify(entry["data"])

    # Memory miss — try DynamoDB before hitting Yahoo Finance
    ddb_entry = _markets_load_from_dynamo()
    if ddb_entry:
        with _MARKETS_CACHE_LOCK:
            _MARKETS_CACHE["data"] = ddb_entry
        return jsonify(ddb_entry["data"])

    def _rsi(prices: list, period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        arr = [p for p in prices if p is not None]
        deltas = [arr[i] - arr[i - 1] for i in range(1, len(arr))]
        gains  = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]
        avg_g  = sum(gains[:period]) / period
        avg_l  = sum(losses[:period]) / period
        for g, l in zip(gains[period:], losses[period:]):
            avg_g = (avg_g * (period - 1) + g) / period
            avg_l = (avg_l * (period - 1) + l) / period
        if avg_l == 0:
            return 100.0
        return round(100 - 100 / (1 + avg_g / avg_l), 1)

    def _ma(prices: list, n: int) -> Optional[float]:
        vals = [p for p in prices if p is not None]
        if len(vals) < n:
            return None
        return round(sum(vals[-n:]) / n, 2)

    def _fetch_index(symbol: str) -> dict:
        try:
            tk   = yf.Ticker(symbol)
            info = tk.info

            # 3-year weekly for medium-term chart
            hist_wk = tk.history(period="3y", interval="1wk")
            # 1-year daily for MA-50 / MA-200 / RSI-14
            hist_1d = tk.history(period="1y", interval="1d")
            # 5-year monthly for long-term chart
            hist_5y = tk.history(period="5y", interval="1mo")

            prices_wk  = [round(float(p), 2) if not math.isnan(float(p)) else None for p in hist_wk["Close"]]
            dates_wk   = [str(d.date()) for d in hist_wk.index]

            prices_1d  = [round(float(p), 2) if not math.isnan(float(p)) else None for p in hist_1d["Close"]]
            dates_1d   = [str(d.date()) for d in hist_1d.index]

            prices_5y  = [round(float(p), 2) if not math.isnan(float(p)) else None for p in hist_5y["Close"]]
            dates_5y   = [str(d.date()) for d in hist_5y.index]

            current = (info.get("regularMarketPrice")
                       or info.get("currentPrice")
                       or (prices_1d[-1] if prices_1d else None)
                       or (prices_wk[-1] if prices_wk else None))
            prev    = info.get("regularMarketPreviousClose") or info.get("previousClose")
            # Always derive current and prev from daily closes — most reliable for all indices,
            # especially 000001.SS where .info fields are often wrong or fractional.
            valid_1d = [p for p in prices_1d if p is not None]
            if len(valid_1d) >= 2:
                current = valid_1d[-1]
                prev    = valid_1d[-2]
            elif len(valid_1d) == 1:
                current = valid_1d[0]
            day_chg = None
            if current and prev and prev > 0:
                raw_chg = (current - prev) / prev * 100
                # Sanity check: indices don't move >25% in a day
                day_chg = round(raw_chg, 2) if abs(raw_chg) <= 25 else None

            # YTD — find first trading day of this year in the daily history
            ytd = None
            try:
                this_year = str(date.today().year)
                for i, d_str in enumerate(dates_1d):
                    if d_str.startswith(this_year):
                        first_price = prices_1d[i]
                        if first_price and first_price > 0 and current:
                            ytd = round((current - first_price) / first_price * 100, 2)
                        break
            except Exception:
                pass

            ma50  = _ma(prices_1d, 50)
            ma200 = _ma(prices_1d, 200)
            rsi14 = _rsi(prices_1d, 14)

            # 52-week high/low
            hi52 = info.get("fiftyTwoWeekHigh")
            lo52 = info.get("fiftyTwoWeekLow")

            # Volume
            volume = info.get("regularMarketVolume") or info.get("volume")

            # P/E (indices have trailingPE in Yahoo)
            pe = _safe(info.get("trailingPE"))

            return {
                "symbol": symbol,
                "name":   info.get("shortName") or info.get("longName") or symbol,
                "current":  round(float(current), 2) if current else None,
                "day_chg":  day_chg,
                "ytd":      ytd,
                "hi52":     round(float(hi52), 2) if hi52 else None,
                "lo52":     round(float(lo52), 2) if lo52 else None,
                "pe":       pe,
                "volume":   int(volume) if volume else None,
                "ma50":     ma50,
                "ma200":    ma200,
                "rsi14":    rsi14,
                "weekly":   {"dates": dates_wk,  "prices": prices_wk},
                "daily":    {"dates": dates_1d,  "prices": prices_1d},
                "monthly":  {"dates": dates_5y,  "prices": prices_5y},
            }
        except Exception as exc:
            log.warning("Could not fetch index %s: %s", symbol, exc)
            return {"symbol": symbol, "error": str(exc)}

    def _fetch_vix() -> Optional[dict]:
        try:
            tk   = yf.Ticker("^VIX")
            info = tk.info
            hist = tk.history(period="1y", interval="1wk")
            prices = [round(float(p), 2) if not math.isnan(float(p)) else None for p in hist["Close"]]
            dates  = [str(d.date()) for d in hist.index]
            current = info.get("regularMarketPrice") or (prices[-1] if prices else None)
            prev    = info.get("regularMarketPreviousClose")
            day_chg = None
            if current and prev and prev > 0:
                day_chg = round((current - prev) / prev * 100, 2)
            return {
                "current": round(float(current), 2) if current else None,
                "day_chg": day_chg,
                "weekly":  {"dates": dates, "prices": prices},
            }
        except Exception as exc:
            log.warning("Could not fetch VIX: %s", exc)
            return None

    def _fetch_sector_etfs() -> list:
        results = []
        try:
            tickers = yf.download(
                list(_SECTOR_ETFS.keys()), period="2d", interval="1d",
                auto_adjust=True, progress=False
            )["Close"]
            for sym, label in _SECTOR_ETFS.items():
                try:
                    col = tickers[sym] if sym in tickers.columns else tickers.get(sym)
                    if col is None:
                        continue
                    vals = col.dropna().tolist()
                    if len(vals) >= 2:
                        chg = round((vals[-1] - vals[-2]) / vals[-2] * 100, 2)
                    elif len(vals) == 1:
                        chg = 0.0
                    else:
                        chg = None
                    results.append({"ticker": sym, "label": label, "day_chg": chg})
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Sector ETF fetch failed: %s", exc)
        return results

    # Fetch all in sequence (could parallelise but keeps it simple)
    indices = {
        key: _fetch_index(sym)
        for key, sym in _INDEX_SYMBOLS.items()
    }
    vix     = _fetch_vix()
    sectors = _fetch_sector_etfs()

    result = {"indices": indices, "vix": vix, "sectors": sectors}
    ts = time.time()
    with _MARKETS_CACHE_LOCK:
        _MARKETS_CACHE["data"] = {"ts": ts, "data": result}
    # Persist to DynamoDB in background so the response isn't delayed
    threading.Thread(target=_markets_save_to_dynamo, args=(result, ts), daemon=True).start()
    return jsonify(result)


# ---------------------------------------------------------------------------
# CNN Fear & Greed Index  (/api/fear-greed)
# ---------------------------------------------------------------------------

_FG_CACHE: dict = {}
_FG_CACHE_LOCK = threading.Lock()
_FG_CACHE_TTL  = 3600   # 1 hour

# DynamoDB table for persisting daily Fear & Greed history
_FG_TABLE_NAME    = "ystocker-fear-greed"
_fg_table         = None
_FG_TABLE_LOCK    = threading.Lock()
_fg_unavail_until = 0.0


def _get_fg_table():
    """Return boto3 DynamoDB Table for fear-greed history, or None. Retries after 5 min."""
    global _fg_table, _fg_unavail_until
    if _fg_table is not None:
        return _fg_table
    if time.time() < _fg_unavail_until:
        return None
    with _FG_TABLE_LOCK:
        if _fg_table is not None:
            return _fg_table
        if time.time() < _fg_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            _fg_table = ddb.Table(_FG_TABLE_NAME)
            _fg_table.load()
            log.info("DynamoDB fear-greed table connected: %s", _FG_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB fear-greed table unavailable: %s", exc)
            _fg_table = None
            _fg_unavail_until = time.time() + 300
        return _fg_table


def _fg_load_from_dynamo() -> list:
    """Load all stored daily fear-greed records. Returns list of {date, score, rating}."""
    table = _get_fg_table()
    if not table:
        return []
    try:
        items = []
        resp = table.scan(ProjectionExpression="#d, score, rating",
                          ExpressionAttributeNames={"#d": "date"})
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ProjectionExpression="#d, score, rating",
                              ExpressionAttributeNames={"#d": "date"},
                              ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return [{"date": it["date"], "score": float(it["score"]), "rating": it.get("rating")}
                for it in items if it.get("date") and it.get("score") is not None]
    except Exception as exc:
        log.warning("DynamoDB fear-greed scan failed: %s", exc)
        return []


def _fg_save_to_dynamo(history: list) -> None:
    """Batch-write history items [{date, score, rating}] to DynamoDB."""
    table = _get_fg_table()
    if not table or not history:
        return
    try:
        with table.batch_writer() as batch:
            for item in history:
                if not item.get("date") or item.get("score") is None:
                    continue
                batch.put_item(Item={
                    "date":   item["date"],
                    "score":  Decimal(str(round(float(item["score"]), 2))),
                    "rating": item.get("rating") or "",
                })
    except Exception as exc:
        log.warning("DynamoDB fear-greed write failed: %s", exc)


_CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CNN_FG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/",
    "Origin":  "https://edition.cnn.com",
}


@bp.route("/api/fear-greed")
def api_fear_greed():
    """
    Return CNN Fear & Greed Index data, merging DynamoDB history with live fetch.

    Strategy:
      1. Check in-process memory cache (TTL 1h) — return immediately if fresh.
      2. Load stored history from DynamoDB (all dates we've ever saved).
      3. If DynamoDB has a record for today, skip CNN fetch for history.
      4. Fetch from CNN to get current snapshot + any dates not yet in DynamoDB.
      5. Persist only the newly seen dates back to DynamoDB.
      6. Merge DynamoDB + CNN history, deduplicate, sort, return.

    Response:
      {
        "score":    float,          # current score 0–100
        "rating":   str,            # e.g. "Fear"
        "prev_close":  float|null,
        "prev_week":   float|null,
        "prev_month":  float|null,
        "prev_year":   float|null,
        "history": [{"t": int_ms, "y": float, "rating": str}, ...]
      }
    """
    import requests as req_lib
    from datetime import datetime, timezone

    # ── L1: in-process memory cache ──────────────────────────────────────
    with _FG_CACHE_LOCK:
        entry = _FG_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _FG_CACHE_TTL:
            return jsonify(entry["data"])

    def _cap(s):
        return " ".join(w.capitalize() for w in (s or "").split()) if s else s

    # ── L2: load history already in DynamoDB ─────────────────────────────
    ddb_records = _fg_load_from_dynamo()   # [{date, score, rating}, ...]
    ddb_dates   = {r["date"] for r in ddb_records}

    # Convert DynamoDB records to history format (date "YYYY-MM-DD" → ms timestamp)
    def _date_to_ms(date_str):
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None

    ddb_history = []
    for r in ddb_records:
        t = _date_to_ms(r["date"])
        if t is not None:
            ddb_history.append({"t": t, "y": r["score"], "rating": r.get("rating")})

    # ── L3: fetch from CNN ────────────────────────────────────────────────
    raw       = None
    cnn_error = None

    try:
        resp = req_lib.get(_CNN_FG_URL, headers=_CNN_FG_HEADERS, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        log.warning("CNN Fear & Greed fetch failed: %s", exc)
        cnn_error = str(exc)

    if raw is None and not ddb_history:
        return jsonify({"error": cnn_error or "No data available"}), 502

    # Parse CNN response
    fg        = (raw or {}).get("fear_and_greed", {})
    cnn_hist  = (raw or {}).get("fear_and_greed_historical", {}).get("data", [])

    # Convert CNN history to [{date, score, rating}] and find new dates
    cnn_dated = []
    for p in cnn_hist:
        if p.get("x") is None or p.get("y") is None:
            continue
        try:
            dt_str = datetime.fromtimestamp(int(p["x"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            continue
        cnn_dated.append({
            "date":   dt_str,
            "score":  round(float(p["y"]), 2),
            "rating": _cap(p.get("rating")),
        })

    # Persist only dates not already in DynamoDB
    new_records = [r for r in cnn_dated if r["date"] not in ddb_dates]
    if new_records:
        _fg_save_to_dynamo(new_records)
        log.info("Saved %d new Fear & Greed records to DynamoDB", len(new_records))

    # Build merged history: DynamoDB + new CNN records, deduped by ms timestamp
    cnn_history = [
        {"t": _date_to_ms(r["date"]), "y": r["score"], "rating": r["rating"]}
        for r in cnn_dated if _date_to_ms(r["date"]) is not None
    ]
    # Merge: prefer CNN data (more accurate) over DynamoDB when dates overlap
    seen_t = {}
    for h in ddb_history + cnn_history:
        seen_t[h["t"]] = h   # CNN overwrites DDB for same timestamp
    merged_history = sorted(seen_t.values(), key=lambda h: h["t"])

    result = {
        "score":      fg.get("score"),
        "rating":     _cap(fg.get("rating")),
        "prev_close": fg.get("previous_close"),
        "prev_week":  fg.get("previous_1_week"),
        "prev_month": fg.get("previous_1_month"),
        "prev_year":  fg.get("previous_1_year"),
        "history":    merged_history,
    }

    with _FG_CACHE_LOCK:
        _FG_CACHE["data"] = {"ts": time.time(), "data": result}

    return jsonify(result)


# ---------------------------------------------------------------------------
# CBOE Equity Put/Call Ratio  (/api/put-call-ratio)
# ---------------------------------------------------------------------------

_PCR_CACHE: dict = {}
_PCR_CACHE_LOCK = threading.Lock()
_PCR_CACHE_TTL  = 4 * 3600   # 4 hours — daily data

_PCR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.cboe.com/",
}


def _fetch_pcr_cboe() -> dict:
    """Fetch CBOE Equity Put/Call Ratio from CBOE's public CSV (1Y of daily data)."""
    import requests, csv, io
    from datetime import date, timedelta

    url = "https://cdn.cboe.com/resources/options/xcpc_equity_put_call_ratio.csv"
    # Bypass any http_proxy env var — CBOE CDN must be reached directly
    resp = requests.get(url, headers=_PCR_HEADERS, timeout=20,
                        proxies={"http": None, "https": None})
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = []
    for row in reader:
        try:
            d   = row.get("Date", "").strip()
            val = row.get("EQUITY_PC_RATIO", row.get("Equity", "")).strip()
            if d and val:
                rows.append((d, float(val)))
        except (ValueError, KeyError):
            continue

    if not rows:
        raise ValueError("CBOE CSV parsed 0 rows")

    # Keep last 1 year
    cutoff = str(date.today() - timedelta(days=365))
    rows = [(d, v) for d, v in rows if d >= cutoff]
    rows.sort(key=lambda r: r[0])

    dates  = [r[0] for r in rows]
    closes = [round(r[1], 3) for r in rows]
    return dates, closes


@bp.route("/api/put-call-ratio")
def api_put_call_ratio():
    """Return CBOE Equity Put/Call Ratio history with 1Y daily data.
    Primary: CBOE public CSV. Fallback: yfinance ^PCCE."""
    with _PCR_CACHE_LOCK:
        entry = _PCR_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _PCR_CACHE_TTL:
            return jsonify(entry["data"])

    dates, closes = [], []
    try:
        dates, closes = _fetch_pcr_cboe()
        log.info("Put/Call ratio: fetched %d rows from CBOE CSV", len(dates))
    except Exception as exc:
        log.warning("CBOE CSV put/call fetch failed (%s), trying yfinance", exc)
        try:
            import yfinance as yf
            tk   = yf.Ticker("^PCCE")
            hist = tk.history(period="1y", interval="1d")
            if not hist.empty:
                closes = [round(float(v), 3) if not math.isnan(float(v)) else None
                          for v in hist["Close"]]
                dates  = [str(d.date()) for d in hist.index]
        except Exception as exc2:
            log.warning("yfinance put/call fetch also failed: %s", exc2)

    if not closes:
        return jsonify({"error": "Put/Call ratio data unavailable"}), 502

    current = next((v for v in reversed(closes) if v is not None), None)
    prev    = next((v for v in reversed(closes[:-1]) if v is not None), None)
    day_chg = round(current - prev, 3) if current and prev else None
    valid   = [v for v in closes if v is not None]
    ma20    = round(sum(valid[-20:]) / min(len(valid), 20), 3) if valid else None

    result = {
        "current": current,
        "day_chg": day_chg,
        "ma20":    ma20,
        "dates":   dates,
        "closes":  closes,
    }
    with _PCR_CACHE_LOCK:
        _PCR_CACHE["data"] = {"ts": time.time(), "data": result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# AAII Sentiment Survey  (/api/aaii-sentiment)
# ---------------------------------------------------------------------------

_AAII_CACHE: dict = {}
_AAII_CACHE_LOCK = threading.Lock()
_AAII_CACHE_TTL  = 6 * 3600  # 6 hours (published weekly)
_AAII_FILE       = Path(__file__).parent.parent / "cache" / "aaii_cache.json"

# DynamoDB fallback — serves last-known-good data when live XLS is unavailable
_AAII_TABLE_NAME       = "ystocker-aaii-sentiment"
_aaii_ddb_table        = None
_aaii_ddb_unavail_until = 0.0
_AAII_DDB_LOCK         = threading.Lock()


def _get_aaii_ddb_table():
    global _aaii_ddb_table, _aaii_ddb_unavail_until
    if _aaii_ddb_table is not None:
        return _aaii_ddb_table
    if time.time() < _aaii_ddb_unavail_until:
        return None
    with _AAII_DDB_LOCK:
        if _aaii_ddb_table is not None:
            return _aaii_ddb_table
        if time.time() < _aaii_ddb_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(_AAII_TABLE_NAME)
            tbl.load()
            _aaii_ddb_table = tbl
            log.info("DynamoDB AAII table connected: %s", _AAII_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB AAII table unavailable: %s", exc)
            _aaii_ddb_table = None
            _aaii_ddb_unavail_until = time.time() + 300
        return _aaii_ddb_table


def _aaii_load_from_dynamo() -> Optional[dict]:
    table = _get_aaii_ddb_table()
    if not table:
        return None
    try:
        resp = table.get_item(Key={"pk": "latest"})
        item = resp.get("Item")
        if not item or not item.get("payload"):
            return None
        return json.loads(item["payload"])
    except Exception as exc:
        log.warning("DynamoDB AAII load failed: %s", exc)
        return None


def _aaii_save_to_dynamo(result: dict) -> None:
    table = _get_aaii_ddb_table()
    if not table:
        return
    try:
        table.put_item(Item={
            "pk":      "latest",
            "payload": json.dumps(result, default=str),
            "ts":      Decimal(str(round(time.time(), 3))),
        })
    except Exception as exc:
        log.warning("DynamoDB AAII save failed: %s", exc)

_AAII_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.aaii.com/sentimentsurvey/sent_results",
    "Connection": "keep-alive",
}

_AAII_XLS_URL = "https://www.aaii.com/files/surveys/sentiment.xls"


@bp.route("/api/aaii-sentiment")
def api_aaii_sentiment():
    """
    Fetch AAII Investor Sentiment Survey data (weekly bulls/bears/neutral).

    Returns:
      {
        "latest": { "date": "YYYY-MM-DD", "bullish": float, "neutral": float, "bearish": float,
                    "bull_bear_spread": float },
        "history": [{"date": "YYYY-MM-DD", "bullish": float, "neutral": float,
                     "bearish": float, "bull_bear_spread": float}, ...]
      }
    """
    import requests as req_lib
    import io

    with _AAII_CACHE_LOCK:
        entry = _AAII_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _AAII_CACHE_TTL:
            return jsonify(entry["data"])

    try:
        resp = req_lib.get(_AAII_XLS_URL, headers=_AAII_HEADERS, timeout=20)
        resp.raise_for_status()
        # AAII occasionally returns an HTML error page instead of the XLS
        if resp.content[:6] in (b'\xd0\xcf\x11\xe0\xa1\xb1', b'\x09\x08\x10\x00\x00\x06\x05\x00'):
            pass  # valid .xls magic bytes
        elif resp.content[:5] == b'<?xml' or resp.content[:9] == b'<!DOCTYPE' or b'<html' in resp.content[:200].lower():
            raise ValueError("AAII returned HTML instead of XLS — try again later")
        df = pd.read_excel(io.BytesIO(resp.content), header=3, engine="xlrd")

        # Columns: Date, Bullish, Neutral, Bearish, Total, Bull-Bear Spread, ...
        # Normalise column names
        df.columns = [str(c).strip() for c in df.columns]
        # Find key columns (header names may vary slightly)
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if "date" in cl:
                col_map.setdefault("date", c)
            elif "bull" in cl and "bear" not in cl and "spread" not in cl:
                col_map.setdefault("bullish", c)
            elif "neutral" in cl:
                col_map.setdefault("neutral", c)
            elif "bear" in cl and "spread" not in cl:
                col_map.setdefault("bearish", c)
            elif "spread" in cl:
                col_map.setdefault("spread", c)

        required = ["date", "bullish", "neutral", "bearish"]
        if not all(k in col_map for k in required):
            raise ValueError(f"Could not find required columns, got: {list(df.columns)}")

        records = []
        for _, row in df.iterrows():
            try:
                raw_date = row[col_map["date"]]
                if pd.isna(raw_date):
                    continue
                if hasattr(raw_date, "strftime"):
                    date_str = raw_date.strftime("%Y-%m-%d")
                else:
                    date_str = str(raw_date)[:10]
                # Must look like a valid date
                if len(date_str) < 8 or not date_str[0].isdigit():
                    continue

                def _pct(val):
                    if pd.isna(val):
                        return None
                    v = float(val)
                    # Already a fraction (0.xx) → convert to %
                    return round(v * 100 if v < 2 else v, 1)

                bull = _pct(row[col_map["bullish"]])
                neu  = _pct(row[col_map["neutral"]])
                bear = _pct(row[col_map["bearish"]])
                spread_col = col_map.get("spread")
                spread = None
                if spread_col:
                    spread = _pct(row[spread_col])
                if spread is None and bull is not None and bear is not None:
                    spread = round(bull - bear, 1)

                records.append({
                    "date": date_str,
                    "bullish": bull,
                    "neutral": neu,
                    "bearish": bear,
                    "bull_bear_spread": spread,
                })
            except Exception:
                continue

        # Sort ascending and take last 104 weeks (2 years) for chart
        records.sort(key=lambda r: r["date"])
        history = records[-104:] if len(records) > 104 else records
        latest  = records[-1] if records else None

        result = {"latest": latest, "history": history}

    except Exception as exc:
        log.warning("AAII sentiment fetch failed: %s", exc)
        # Fallback priority: 1) stale in-memory  2) local file  3) DynamoDB
        with _AAII_CACHE_LOCK:
            stale = _AAII_CACHE.get("data")
        fallback = stale["data"] if stale else None
        if fallback is None:
            try:
                if _AAII_FILE.exists():
                    fallback = json.loads(_AAII_FILE.read_text())
                    log.info("AAII: serving file cache as fallback")
            except Exception:
                pass
        if fallback is None:
            fallback = _aaii_load_from_dynamo()
            if fallback:
                log.info("AAII: serving DynamoDB cache as fallback")
        if fallback:
            fallback["_stale"] = True
            with _AAII_CACHE_LOCK:
                _AAII_CACHE["data"] = {"ts": time.time() - _AAII_CACHE_TTL + 300, "data": fallback}
            return jsonify(fallback)
        return jsonify({"error": str(exc)}), 502

    with _AAII_CACHE_LOCK:
        _AAII_CACHE["data"] = {"ts": time.time(), "data": result}
    # Persist to file and DynamoDB in background
    def _persist_aaii():
        try:
            _AAII_FILE.parent.mkdir(parents=True, exist_ok=True)
            _AAII_FILE.write_text(json.dumps(result, default=str))
        except Exception:
            pass
        _aaii_save_to_dynamo(result)
    threading.Thread(target=_persist_aaii, daemon=True).start()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Economic Events Calendar  (/api/economic-events)
# ---------------------------------------------------------------------------

_ECON_TABLE_NAME    = "ystocker-economic-events"
_econ_table         = None
_ECON_TABLE_LOCK    = threading.Lock()
_econ_unavail_until = 0.0

_ECON_CACHE: dict = {}
_ECON_CACHE_LOCK = threading.Lock()
_ECON_CACHE_TTL  = 3600   # 1 hour

_ECON_CAL_URL = "https://tradingeconomics.com/calendar"
_ECON_CAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tradingeconomics.com/calendar",
    "X-Requested-With": "XMLHttpRequest",
}


def _get_econ_table():
    """Return boto3 DynamoDB Table for economic events, or None."""
    global _econ_table, _econ_unavail_until
    if _econ_table is not None:
        return _econ_table
    if time.time() < _econ_unavail_until:
        return None
    with _ECON_TABLE_LOCK:
        if _econ_table is not None:
            return _econ_table
        if time.time() < _econ_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            _econ_table = ddb.Table(_ECON_TABLE_NAME)
            _econ_table.load()
            log.info("DynamoDB economic-events table connected: %s", _ECON_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB economic-events table unavailable: %s", exc)
            _econ_table = None
            _econ_unavail_until = time.time() + 300
        return _econ_table


def _econ_load_from_dynamo(date_str: str) -> list:
    """Load economic events for a given date (YYYY-MM-DD) from DynamoDB."""
    table = _get_econ_table()
    if not table:
        return []
    try:
        from boto3.dynamodb.conditions import Key
        resp = table.query(KeyConditionExpression=Key("date").eq(date_str))
        return resp.get("Items", [])
    except Exception as exc:
        log.warning("DynamoDB economic-events query failed: %s", exc)
        return []


def _econ_save_to_dynamo(events: list) -> None:
    """Batch-write economic event items to DynamoDB.

    Only persists stable identity/translation fields — NOT actual, forecast,
    or previous, which are live values that must always come from the scrape.
    """
    table = _get_econ_table()
    if not table or not events:
        return
    _STABLE_FIELDS = {"date", "event_id", "time", "event", "country", "impact", "url", "zh"}
    try:
        with table.batch_writer() as batch:
            for ev in events:
                if not ev.get("date") or not ev.get("event_id"):
                    continue
                item = {k: v for k, v in ev.items()
                        if k in _STABLE_FIELDS and v is not None}
                batch.put_item(Item=item)
    except Exception as exc:
        log.warning("DynamoDB economic-events write failed: %s", exc)


def _fetch_econ_calendar() -> list:
    """
    Fetch economic calendar from tradingeconomics.com by scraping the HTML page.

    Returns list of dicts: {date, event_id, time, event, country, impact,
                             actual, forecast, previous, url, zh}
    """
    import requests as req_lib
    import re as _re
    import hashlib
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    date_from = today.strftime("%Y-%m-%d")
    date_to   = (today + timedelta(days=7)).strftime("%Y-%m-%d")

    url = (
        f"https://tradingeconomics.com/calendar/country/all"
        f"/{date_from}/{date_to}/importance:1,2,3"
    )
    resp = req_lib.get(url, headers=_ECON_CAL_HEADERS, timeout=15)
    resp.raise_for_status()
    html = resp.text

    impact_map = {"1": "Low", "2": "Medium", "3": "High"}
    events = []

    # Split on TR blocks that carry data-event attribute
    for block in html.split("<tr "):
        if 'data-event=' not in block:
            continue

        # Outer TR attributes
        attr_end = block.find(">")
        attrs = block[:attr_end]

        country_m  = _re.search(r'data-country="([^"]+)"', attrs)
        event_m    = _re.search(r'data-event="([^"]+)"', attrs)
        data_url_m = _re.search(r'data-url="([^"]+)"', attrs)
        if not event_m:
            continue

        # Date: td class attribute contains YYYY-MM-DD
        date_m  = _re.search(r"class='[^']*(\d{4}-\d{2}-\d{2})[^']*'", block)
        if not date_m:
            date_m = _re.search(r'class="[^"]*(\d{4}-\d{2}-\d{2})[^"]*"', block)

        # Time: first AM/PM string in the block
        time_m  = _re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", block)

        # Impact from calendar-date-N class (1=low 2=med 3=high)
        impact_m = _re.search(r"calendar-date-(\d)", block)

        # Values use single-quote ids: id='actual', id='previous', id='consensus'
        actual_m   = _re.search(r"id='actual'>([^<]+)<", block)
        previous_m = _re.search(r"id='previous'>([^<]+)<", block)
        forecast_m = _re.search(r"id='consensus'[^>]*>([^<]+)<", block)

        def _v(m):
            if not m:
                return None
            s = m.group(1).strip()
            return s if s and s not in ("-", "") else None

        date_str    = date_m.group(1) if date_m else None
        event_name  = event_m.group(1).title()
        country     = country_m.group(1).title() if country_m else None
        event_link  = data_url_m.group(1) if data_url_m else None

        if not date_str:
            continue

        event_id = hashlib.md5(
            f"{date_str}:{_v(time_m)}:{event_name}:{country}".encode()
        ).hexdigest()[:16]

        events.append({
            "date":     date_str,
            "event_id": event_id,
            "time":     _v(time_m),
            "event":    event_name,
            "country":  country,
            "impact":   impact_map.get(impact_m.group(1)) if impact_m else None,
            "actual":   _v(actual_m),
            "forecast": _v(forecast_m),
            "previous": _v(previous_m),
            "url":      f"https://tradingeconomics.com{event_link}" if event_link else None,
            "zh":       None,
        })

    events.sort(key=lambda e: (e["date"] or "", e["time"] or ""))
    return events


@bp.route("/api/economic-events")
def api_economic_events():
    """
    Return economic calendar events.

    Query params:
      date  - YYYY-MM-DD (default: today)
      days  - how many days to fetch (default: 7)

    Response:
      { "events": [ {date, time, event, country, impact, actual, forecast, previous, zh}, ... ] }
    """
    with _ECON_CACHE_LOCK:
        entry = _ECON_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _ECON_CACHE_TTL:
            return jsonify(entry["data"])

    try:
        raw_events = _fetch_econ_calendar()
    except Exception as exc:
        log.warning("Economic calendar fetch failed: %s", exc)
        raw_events = []

    # Load any stored translations from DynamoDB
    if raw_events:
        dates = list({ev["date"] for ev in raw_events})
        stored: dict = {}
        for d in dates:
            for rec in _econ_load_from_dynamo(d):
                eid = rec.get("event_id")
                if eid:
                    stored[eid] = rec

        # Merge stored translations
        for ev in raw_events:
            eid = ev.get("event_id")
            if eid and eid in stored:
                ev["zh"] = stored[eid].get("zh")

        # Save new events that aren't in DB yet
        new_evs = [ev for ev in raw_events if ev.get("event_id") not in stored]
        if new_evs:
            _econ_save_to_dynamo(new_evs)

    result = {"events": raw_events}
    with _ECON_CACHE_LOCK:
        _ECON_CACHE["data"] = {"ts": time.time(), "data": result}

    return jsonify(result)


@bp.route("/api/economic-events/translate", methods=["POST"])
def api_economic_events_translate():
    """
    Translate economic event names to Chinese using Gemini AI.

    Request body: { "events": [{"event_id": str, "event": str}, ...] }
    Response:     { "translations": {"event_id": "zh_text", ...} }
    """
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        return jsonify({"error": "AI translation not configured"}), 503

    body = request.get_json(force=True) or {}
    events_to_translate = body.get("events", [])
    if not events_to_translate:
        return jsonify({"translations": {}})

    # Build prompt
    lines = "\n".join(
        f'{ev["event_id"]}: {ev["event"]}'
        for ev in events_to_translate
        if ev.get("event_id") and ev.get("event")
    )
    prompt = (
        "You are a financial translator. Translate the following economic event names "
        "from English to Simplified Chinese. Return ONLY a JSON object mapping each ID "
        "to its Chinese translation. Do not add any explanation.\n\n"
        + lines
    )

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = resp.text.strip()
        # Strip markdown code blocks if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        translations = json.loads(text)
    except Exception as exc:
        log.warning("Economic events translation failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    # Persist translations to DynamoDB and patch the in-memory cache
    if translations:
        table = _get_econ_table()
        if table:
            for ev in events_to_translate:
                eid = ev.get("event_id")
                zh  = translations.get(eid)
                if not eid or not zh:
                    continue
                item = {
                    "date":     ev.get("date") or "unknown",
                    "event_id": eid,
                    "event":    ev.get("event") or "",
                    "zh":       zh,
                }
                # DynamoDB rejects empty string attribute values
                item = {k: v for k, v in item.items() if v != ""}
                try:
                    table.put_item(Item=item)
                except Exception as exc:
                    log.warning("DynamoDB econ translation save failed for %s: %s", eid, exc)

        # Patch in-memory cache so the next /api/economic-events hit returns
        # zh values without waiting for cache expiry + re-fetch from DynamoDB
        with _ECON_CACHE_LOCK:
            entry = _ECON_CACHE.get("data")
            if entry:
                zh_map = {ev.get("event_id"): translations[ev.get("event_id")]
                          for ev in events_to_translate
                          if ev.get("event_id") in translations}
                for ev in entry["data"].get("events", []):
                    eid = ev.get("event_id")
                    if eid and eid in zh_map and not ev.get("zh"):
                        ev["zh"] = zh_map[eid]

    return jsonify({"translations": translations})


# ---------------------------------------------------------------------------
# Sector Heatmap  (/heatmap)
# ---------------------------------------------------------------------------

_HEATMAP_TABLE_NAME    = "ystocker-heatmap-snapshots"
_heatmap_table         = None
_HEATMAP_LOCK          = threading.Lock()
_heatmap_unavail_until = 0.0

_HEATMAP_CACHE: dict = {}
_HEATMAP_CACHE_LOCK = threading.Lock()
_HEATMAP_CACHE_TTL  = 15 * 60   # 15 minutes


def _get_heatmap_table():
    """Return a cached boto3 DynamoDB Table resource for the heatmap table, or None.
    Backs off 5 minutes after failure before retrying."""
    global _heatmap_table, _heatmap_unavail_until
    if _heatmap_table is not None:
        return _heatmap_table
    if time.time() < _heatmap_unavail_until:
        return None
    with _HEATMAP_LOCK:
        if _heatmap_table is not None:
            return _heatmap_table
        if time.time() < _heatmap_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            _heatmap_table = ddb.Table(_HEATMAP_TABLE_NAME)
            _heatmap_table.load()
            log.info("DynamoDB heatmap table connected: %s", _HEATMAP_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB heatmap table unavailable: %s", exc)
            _heatmap_table = None
            _heatmap_unavail_until = time.time() + 300
        return _heatmap_table


def _heatmap_fetch_from_dynamo(date_str: str) -> Optional[list]:
    """Query all stock items for date_str. Returns list or None if unavailable/empty."""
    from boto3.dynamodb.conditions import Key as DKey
    table = _get_heatmap_table()
    if not table:
        return None
    try:
        resp  = table.query(KeyConditionExpression=DKey("date").eq(date_str))
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp  = table.query(
                KeyConditionExpression=DKey("date").eq(date_str),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        if not items:
            return None
        stocks = []
        for item in items:
            stocks.append({
                "ticker":  item["ticker"],
                "name":    item.get("name", item["ticker"]),
                "sector":  item.get("sector", ""),
                "price":   float(item["price"])    if item.get("price")    else None,
                "day_chg": float(item["day_chg"])  if item.get("day_chg") is not None else None,
                "mkt_cap": float(item["mkt_cap_b"]) if item.get("mkt_cap_b") else None,
            })
        return stocks
    except Exception as exc:
        log.warning("DynamoDB heatmap query failed for %s: %s", date_str, exc)
        return None


def _heatmap_save_to_dynamo(date_str: str, stocks: list) -> None:
    """Batch-write all stock items for date_str to DynamoDB."""
    table = _get_heatmap_table()
    if not table or not stocks:
        return
    ttl_epoch = int(time.time()) + 90 * 24 * 3600
    try:
        with table.batch_writer() as batch:
            for s in stocks:
                item = {
                    "date":    date_str,
                    "ticker":  s["ticker"],
                    "name":    s.get("name", s["ticker"]),
                    "sector":  s.get("sector", ""),
                    "ts":      Decimal(str(int(time.time()))),
                    "ttl":     ttl_epoch,
                }
                if s.get("price") is not None:
                    item["price"]     = Decimal(str(round(s["price"], 4)))
                if s.get("day_chg") is not None:
                    item["day_chg"]   = Decimal(str(round(s["day_chg"], 4)))
                if s.get("mkt_cap") is not None:
                    item["mkt_cap_b"] = Decimal(str(round(s["mkt_cap"], 2)))
                batch.put_item(Item=item)
        log.info("Heatmap snapshot saved to DynamoDB: %s (%d stocks)", date_str, len(stocks))
    except Exception as exc:
        log.warning("DynamoDB heatmap batch_write failed for %s: %s", date_str, exc)


def _heatmap_fetch_live() -> list:
    """Fetch live price + day_chg for all heatmap tickers via yfinance batch download."""
    import yfinance as yf
    from ystocker.heatmap_meta import HEATMAP_META

    tickers_list = list(HEATMAP_META.keys())
    stocks = []
    try:
        data   = yf.download(
            tickers_list, period="2d", interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
        closes = data["Close"]
        for ticker in tickers_list:
            meta = HEATMAP_META[ticker]
            try:
                col = closes[ticker] if ticker in closes.columns else None
                if col is None:
                    continue
                vals    = col.dropna().tolist()
                price   = round(float(vals[-1]), 2) if vals else None
                day_chg = round((vals[-1] - vals[-2]) / vals[-2] * 100, 2) if len(vals) >= 2 else None
            except Exception:
                price, day_chg = None, None
            stocks.append({
                "ticker":  ticker,
                "name":    meta["name"],
                "sector":  meta["sector"],
                "price":   price,
                "day_chg": day_chg,
                "mkt_cap": meta.get("mkt_cap_b"),  # use static approximate value
            })
    except Exception as exc:
        log.warning("Heatmap yf.download failed: %s", exc)
        return []
    return stocks


@bp.route("/heatmap")
def heatmap():
    """Sector heatmap page."""
    return render_template("heatmap.html", peer_groups=list(PEER_GROUPS.keys()))


@bp.route("/api/heatmap")
def api_heatmap():
    """
    Return sector heatmap data for the requested date.

    Query params:
      date: YYYY-MM-DD  (default: today)
    """
    import datetime as _dt

    today_str = str(_dt.date.today())
    date_str  = request.args.get("date", today_str)

    try:
        _dt.date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": f"Invalid date '{date_str}'. Use YYYY-MM-DD."}), 400

    # L1: memory cache
    with _HEATMAP_CACHE_LOCK:
        entry = _HEATMAP_CACHE.get(date_str)
        if entry and time.time() - entry["ts"] < _HEATMAP_CACHE_TTL:
            return jsonify({"date": date_str, "stocks": entry["stocks"]})

    # L2: DynamoDB
    stocks = _heatmap_fetch_from_dynamo(date_str)
    if stocks:
        log.info("Heatmap served from DynamoDB: %s (%d stocks)", date_str, len(stocks))
        with _HEATMAP_CACHE_LOCK:
            _HEATMAP_CACHE[date_str] = {"ts": time.time(), "stocks": stocks}
        return jsonify({"date": date_str, "stocks": stocks})

    # L3: live fetch — only for today
    if date_str != today_str:
        return jsonify({
            "error": f"No snapshot found for {date_str}.",
            "date": date_str, "stocks": [],
        }), 404

    log.info("Heatmap: no DynamoDB data for %s — fetching live", date_str)
    stocks = _heatmap_fetch_live()
    if not stocks:
        return jsonify({"error": "Live fetch failed."}), 502

    # Save to DynamoDB in background, cache immediately
    threading.Thread(
        target=_heatmap_save_to_dynamo, args=(date_str, stocks),
        daemon=True, name="heatmap-ddb-write",
    ).start()
    with _HEATMAP_CACHE_LOCK:
        _HEATMAP_CACHE[date_str] = {"ts": time.time(), "stocks": stocks}

    return jsonify({"date": date_str, "stocks": stocks})


@bp.route("/api/heatmap/snapshot", methods=["POST"])
def api_heatmap_snapshot():
    """
    Trigger a fresh heatmap snapshot for today and persist to DynamoDB.
    Optional protection: set HEATMAP_SNAPSHOT_SECRET env var.
    """
    import datetime as _dt

    secret = os.environ.get("HEATMAP_SNAPSHOT_SECRET")
    if secret and request.headers.get("X-Snapshot-Secret", "") != secret:
        return jsonify({"error": "Unauthorized"}), 403

    today_str = str(_dt.date.today())
    stocks    = _heatmap_fetch_live()
    if not stocks:
        return jsonify({"error": "Live fetch failed — nothing written."}), 502

    _heatmap_save_to_dynamo(today_str, stocks)

    with _HEATMAP_CACHE_LOCK:
        _HEATMAP_CACHE.pop(today_str, None)

    return jsonify({"date": today_str, "saved": len(stocks)})


# ---------------------------------------------------------------------------
# Heatmap daily auto-snapshot scheduler
# ---------------------------------------------------------------------------

def _heatmap_scheduler_loop() -> None:
    """
    Background thread: every weekday at 16:30 US/Eastern (after market close),
    fetch live prices and persist a snapshot to DynamoDB.

    On startup it calculates the seconds until the next 16:30 ET window,
    sleeps until then, runs the snapshot, then repeats every 24 h.
    Weekends are skipped — yfinance returns stale data on Sat/Sun anyway.
    """
    import datetime as _dt

    ET_OFFSET_HOURS = -5   # EST (UTC-5); during EDT (summer) this is -4.
                            # Use -5 conservatively — 16:30 EST = 21:30 UTC,
                            # which is after 16:00 EDT close either way.

    SNAPSHOT_HOUR   = 16
    SNAPSHOT_MINUTE = 30

    def _seconds_until_next_snapshot() -> float:
        now_utc  = _dt.datetime.utcnow()
        now_et   = now_utc + _dt.timedelta(hours=ET_OFFSET_HOURS)
        target   = now_et.replace(hour=SNAPSHOT_HOUR, minute=SNAPSHOT_MINUTE, second=0, microsecond=0)
        if now_et >= target:
            target += _dt.timedelta(days=1)
        # Skip weekend targets (0=Mon … 6=Sun)
        while target.weekday() >= 5:
            target += _dt.timedelta(days=1)
        return (target - now_et).total_seconds()

    log.info("Heatmap scheduler started — daily snapshot at %02d:%02d ET on weekdays",
             SNAPSHOT_HOUR, SNAPSHOT_MINUTE)

    while True:
        sleep_secs = _seconds_until_next_snapshot()
        log.info("Heatmap scheduler: next snapshot in %.1f h", sleep_secs / 3600)
        time.sleep(sleep_secs)

        now_et = _dt.datetime.utcnow() + _dt.timedelta(hours=ET_OFFSET_HOURS)
        if now_et.weekday() >= 5:
            log.info("Heatmap scheduler: skipping weekend snapshot")
            continue

        date_str = str(now_et.date())
        log.info("Heatmap scheduler: taking snapshot for %s", date_str)
        try:
            stocks = _heatmap_fetch_live()
            if stocks:
                _heatmap_save_to_dynamo(date_str, stocks)
                with _HEATMAP_CACHE_LOCK:
                    _HEATMAP_CACHE.pop(date_str, None)
                log.info("Heatmap scheduler: saved %d stocks for %s", len(stocks), date_str)
            else:
                log.warning("Heatmap scheduler: live fetch returned no data for %s", date_str)
        except Exception:
            log.exception("Heatmap scheduler: unhandled error during snapshot for %s", date_str)

        # Sleep ~23 h so we wake up slightly before the next 16:30 window
        # (the loop will recalculate the exact sleep at the top)
        time.sleep(23 * 3600)


def _start_heatmap_scheduler() -> None:
    t = threading.Thread(target=_heatmap_scheduler_loop, daemon=True, name="heatmap-scheduler")
    t.start()
