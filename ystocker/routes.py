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
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import math
from decimal import Decimal

import pandas as pd
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, Response, has_request_context, session

from ystocker import PEER_GROUPS, YT_CHANNELS
from ystocker.data import fetch_group, dividend_yield_pct, ps_ratio
# Per-ticker back-off. Owned by data.py because fetch_group() is what knows which
# tickers it actually attempted; routes only reads it to pre-filter work lists.
from ystocker.data import TICKER_BACKOFF as _ticker_backoff
from ystocker import breadth
from ystocker import charts
from ystocker import fetchguard
from ystocker import freshness
# Plain client UA for every fred.stlouisfed.org request. A spoofed browser UA is
# silently blackholed by FRED's Akamai bot detection — see fed.py for details.
from ystocker.fed import FRED_USER_AGENT as _FRED_UA

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helpers (Google Sign-In) — same pattern as yPlanner / yTracker.
# yStocker stays public, but signed-in users get a personalized header chip
# (and lays the groundwork for future watchlist / alert features).
# ---------------------------------------------------------------------------

def _get_current_user() -> dict:
    """Return the logged-in user dict, or an anonymous placeholder."""
    email = session.get("user_email")
    if email:
        return {
            "email":   email,
            "name":    session.get("user_name", email.split("@")[0]),
            "picture": session.get("user_picture", ""),
        }
    return {"email": "", "name": "Anonymous", "picture": ""}


@bp.route("/login")
def login():
    """Dedicated sign-in page."""
    if session.get("user_email"):
        return redirect(url_for("main.markets"))
    return render_template(
        "login.html",
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    )


@bp.route("/api/auth/google", methods=["POST"])
def auth_google():
    """Verify a Google ID token and create a session."""
    log.info("API auth/google")
    data = request.get_json(force=True, silent=True) or {}
    credential = data.get("credential", "")
    if not credential:
        return jsonify({"error": "No credential provided"}), 400

    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not google_client_id:
        return jsonify({"error": "Google sign-in not configured"}), 503

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        idinfo = id_token.verify_oauth2_token(
            credential, google_requests.Request(), google_client_id
        )

        email   = idinfo.get("email", "")
        name    = idinfo.get("name", email.split("@")[0] if email else "")
        picture = idinfo.get("picture", "")

        if not email:
            return jsonify({"error": "No email in token"}), 400

        session["user_email"]   = email
        session["user_name"]    = name
        session["user_picture"] = picture

        return jsonify({
            "ok": True,
            "user": {"email": email, "name": name, "picture": picture},
        })
    except ValueError as exc:
        log.warning("Google token verification failed: %s", exc)
        return jsonify({"error": "Invalid Google token"}), 401
    except Exception as exc:
        log.exception("Google auth error")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/auth/me")
def auth_me():
    """Return the current user's session info (or anonymous)."""
    return jsonify({"user": _get_current_user()})


@bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Clear the auth session."""
    log.info("API auth/logout")
    session.pop("user_email",   None)
    session.pop("user_name",    None)
    session.pop("user_picture", None)
    return jsonify({"ok": True})


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

    Note: the cache is stored keyed by group name, but PEER_GROUPS may have
    been edited (new sectors added) since the cache was written. We rebuild
    the in-memory structure to match the CURRENT PEER_GROUPS so new sectors
    appear immediately — without waiting for a network refresh.
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
        disk_data = payload["data"]

        # Flatten all known ticker data from any group, then re-distribute
        # according to the current PEER_GROUPS. This lets new sectors that
        # only contain already-cached tickers render immediately.
        ticker_pool: dict = {}
        for group_data in disk_data.values():
            if isinstance(group_data, dict):
                ticker_pool.update(group_data)

        rebuilt = {
            group: {t: ticker_pool[t] for t in tickers if t in ticker_pool}
            for group, tickers in PEER_GROUPS.items()
        }

        # Report any new sectors that need a refresh to populate fully
        missing_groups = [g for g, td in rebuilt.items() if not td]
        partial_groups = [
            g for g, td in rebuilt.items()
            if td and len(td) < len(PEER_GROUPS[g])
        ]
        if missing_groups:
            log.info("Disk cache: %d groups have no cached tickers yet: %s",
                     len(missing_groups), missing_groups)
        if partial_groups:
            log.info("Disk cache: %d groups partially cached (will fill on next refresh)",
                     len(partial_groups))

        with _cache_lock:
            _cache = rebuilt
            _fetch_errors = payload.get("errors", [])
            _cache_last_updated = ts
        log.info("Loaded disk cache from %s (%.1f h old, %d groups, %d unique tickers)",
                 _CACHE_FILE, age / 3600, len(rebuilt), len(ticker_pool))
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

def _do_fetch(force: bool = False) -> None:
    """Fetch all tickers, update in-memory cache, and persist to disk.

    Honours the per-ticker back-off unless *force* is set. This loop used to
    ignore it entirely -- only the 5-minute rolling refresher consulted it -- so
    a permanently-dead symbol was still refetched on every 8-hour warm and on
    every restart, costing a request and a log line each time forever.

    Tickers that are skipped or that fail keep whatever value the cache already
    held. The previous version rebuilt the cache purely from the fetch result, so
    any ticker missing from `raw` silently vanished from the UI; with a back-off
    in play that would have made a single failure hide a symbol for up to an
    hour, which is strictly worse than showing its last known price.
    """
    global _cache, _fetch_errors, _cache_warming, _cache_last_updated
    all_tickers = sorted({t for tickers in PEER_GROUPS.values() for t in tickers})
    if force:
        eligible = all_tickers
    else:
        eligible = _ticker_backoff.filter_ready(all_tickers)
    log.info("Cache fetch started - %d tickers (%d after back-off%s)",
             len(all_tickers), len(eligible), ", forced" if force else "")
    t0 = time.perf_counter()
    raw, errors = fetch_group(eligible)
    elapsed = time.perf_counter() - t0
    log.info("Cache fetch done in %.1fs - %d ok, %d failed", elapsed, len(raw), len(errors))
    for err in errors:
        log.warning("Fetch error: %s", err)

    with _cache_lock:
        previous = {g: dict(gd) for g, gd in (_cache or {}).items()}

    new_cache: Dict[str, Dict[str, dict]] = {}
    carried = 0
    for group, tickers in PEER_GROUPS.items():
        prev_group = previous.get(group, {})
        group_data: Dict[str, dict] = {}
        for t in tickers:
            if t in raw:
                group_data[t] = raw[t]
            elif t in prev_group:
                group_data[t] = prev_group[t]
                carried += 1
        new_cache[group] = group_data
    if carried:
        log.info("Cache fetch: carried forward %d stale ticker entries", carried)

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


# -- Rolling cache refresher --------------------------------------------------
# Keeps data fresh by continuously re-fetching tickers in small batches spread
# evenly across a 5-minute window.  Each batch gets a random jitter so Yahoo
# Finance never sees a sudden spike.  Tickers are patched in-place so the
# cache is never fully cold after the first load.

_ROLLING_PERIOD = 5 * 60   # total refresh window (seconds)
_BATCH_SIZE     = 8         # tickers per mini-fetch
_JITTER_MAX     = 15        # max per-batch jitter (seconds)
# Back-off state lives in _ticker_backoff (declared with the cache globals above)
# and is shared with the full warm in _do_fetch.


def _rolling_refresh_loop() -> None:
    """Continuously refresh all tickers in small batches spread over 5 minutes."""
    global _cache, _fetch_errors, _cache_last_updated
    import random

    # Wait until the initial full fetch has populated the cache.
    log.info("Rolling refresher: waiting for initial cache ...")
    while True:
        with _cache_lock:
            ready = _cache is not None and not _cache_warming
        if ready:
            break
        time.sleep(5)

    log.info(
        "Rolling refresher active (period=%ds, batch=%d, jitter=±%ds)",
        _ROLLING_PERIOD, _BATCH_SIZE, _JITTER_MAX,
    )

    while True:
        all_tickers = sorted({t for tickers in PEER_GROUPS.values() for t in tickers})
        n = len(all_tickers)
        if n == 0:
            time.sleep(_ROLLING_PERIOD)
            continue

        # Skip tickers currently in exponential backoff window
        eligible = _ticker_backoff.filter_ready(all_tickers, log_skipped=False)

        batches = [eligible[i : i + _BATCH_SIZE] for i in range(0, len(eligible), _BATCH_SIZE)]
        num_batches = len(batches) or 1
        base_interval = _ROLLING_PERIOD / num_batches  # ideal seconds per batch slot
        cycle_start = time.time()

        for idx, batch in enumerate(batches):
            # Pause if a full refresh is already in progress.
            with _cache_lock:
                if _cache_warming:
                    log.debug("Rolling refresher: pausing — full refresh in progress")
                    break

            try:
                raw, errs = fetch_group(batch)
                if raw:
                    with _cache_lock:
                        if _cache is not None:
                            for group_data in _cache.values():
                                for ticker, data in raw.items():
                                    if ticker in group_data:
                                        group_data[ticker] = data
                            _cache_last_updated = time.time()
                if errs:
                    log.debug("Rolling refresher: %d error(s) in batch %s", len(errs), batch)
            except Exception:
                log.warning("Rolling refresher: exception fetching batch %s", batch, exc_info=True)

            # Sleep until the next evenly-spaced slot, plus a random jitter.
            next_slot = cycle_start + (idx + 1) * base_interval
            jitter = random.uniform(-_JITTER_MAX, _JITTER_MAX)
            wait = (next_slot + jitter) - time.time()
            if wait > 0:
                time.sleep(wait)

        # Persist the freshened cache to disk once per cycle.
        with _cache_lock:
            snap = {g: dict(gd) for g, gd in _cache.items()} if _cache else None
            errs_snap = list(_fetch_errors)
            ts_snap = _cache_last_updated or time.time()
        if snap:
            _save_to_disk(snap, errs_snap, ts_snap)

        # Sleep out any remaining time in the window so the period stays ~5 min.
        remaining = _ROLLING_PERIOD - (time.time() - cycle_start)
        if remaining > 0:
            time.sleep(remaining)


def _start_rolling_refresh_thread() -> None:
    t = threading.Thread(target=_rolling_refresh_loop, daemon=True, name="cache-roller")
    t.start()
    log.info(
        "Rolling cache refresher started (period=%ds, batch=%d, jitter=±%ds)",
        _ROLLING_PERIOD, _BATCH_SIZE, _JITTER_MAX,
    )


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
            "ps_ratio":         _safe(row.get("P/S Ratio")),
            "pb_ratio":         _safe(row.get("P/B Ratio")),
            "fcf":              _safe(row.get("FCF ($B)")),
            "div_yield":        _safe(row.get("Dividend Yield (%)")),
            "rev_growth":       _safe(row.get("Revenue Growth (%)")),
            "short_float":      _safe(row.get("Short Float (%)")),
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
    seen_tickers: set[str] = set()  # dedup for the cross-sector scatter / heatmap
    for group, df in group_dfs.items():
        cd = json.loads(_df_to_chartdata(df))
        sector_cards[group] = {
            "tickers":   list(df.index),
            "chartdata": cd,
        }
        for row in cd:
            ticker = row.get("ticker") or row.get("Ticker")
            if ticker in seen_tickers:
                # Already added under an earlier sector — skip the duplicate so
                # the Valuation Map plots one point per ticker.
                continue
            row["sector"] = group
            all_rows.append(row)
            if ticker:
                seen_tickers.add(ticker)

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
                  "EPS Growth TTM (%)", "EPS Growth Q (%)", "Day Change (%)", "EV/EBITDA", "EV ($B)", "EBITDA ($B)",
                  "P/S Ratio", "P/B Ratio", "FCF ($B)", "Short Float (%)", "Dividend Yield (%)", "Revenue Growth (%)"]
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
    """Return seconds since the cache was last updated, plus fetch-guard state.

    `providers` and `tickers_backed_off` make the new back-off machinery
    visible. Without them "why has NVDA not moved in an hour" is unanswerable
    from outside the process -- a silently skipped ticker looks exactly like a
    ticker whose price genuinely has not changed.
    """
    with _cache_lock:
        last = _cache_last_updated
    age = int(time.time() - last) if last else None
    log.info("API cache-age: %s seconds", age)
    return jsonify({
        "age_seconds": age,
        "last_updated": last,
        "providers": fetchguard.snapshot(),
        "tickers_backed_off": _ticker_backoff.snapshot(),
    })


def _with_freshness(
    data: dict,
    *,
    series_keys: tuple[str, ...] = (),
) -> dict:
    """Strip internal keys from a cache payload and attach a `meta` block.

    Every cache-backed API here filtered out underscore-prefixed keys on the way
    out, which quietly removed `_ts` -- the one field the UI needed to render
    "data as of". So `/api/fed` and friends returned no age information at all,
    and the only staleness a user could see came from a handful of hand-wired
    client-side checks.

    `meta` carries two independent things, and the distinction is the point:

    * how old *our fetch* is (`fetched_at`, `age_seconds`, `age_label`)
    * whether each upstream *series* has stopped publishing (`series`,
      `stale_series`) -- the `WASDRAL`/`MBST` failure mode, where FRED keeps
      answering 200 with well-formed data years after the series died.

    `meta["series"]` is a flat map. When more than one *series_keys* group is
    inspected its ids are namespaced `group:id`, because the same id legitimately
    appears in two groups (`MORTGAGE30US` is in housing's `fred` block and could
    be in another), and silently overwriting one with the other would report
    health for a series nobody asked about.
    """
    resp = {k: v for k, v in data.items() if not k.startswith("_")}
    meta = freshness.describe_age(data.get("_ts"))

    health: dict[str, dict] = {}
    namespace = len(series_keys) > 1
    for key in series_keys:
        group = data.get(key)
        if not isinstance(group, dict):
            continue
        for sid, h in freshness.annotate_series(group).items():
            health[f"{key}:{sid}" if namespace else sid] = h

    if health:
        meta["series"] = health
        stale_ids = freshness.stale_series_ids(health)
        if stale_ids:
            meta["stale_series"] = stale_ids
    resp["meta"] = meta
    return resp


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
        # force=True: an explicit invalidate is a human saying "try everything
        # again", so it ignores the per-ticker back-off rather than quietly
        # skipping whatever was failing.
        t = threading.Thread(target=_do_fetch, kwargs={"force": True},
                             daemon=True, name="cache-invalidate-refetch")
        t.start()
    else:
        log.info("Cache invalidated (fetch already in progress)")


# ---------------------------------------------------------------------------
# Historical PE page
# ---------------------------------------------------------------------------

_HISTORY_CACHE: Dict[tuple, dict] = {}
_HISTORY_CACHE_LOCK = threading.Lock()
_HISTORY_CACHE_TTL = 60 * 60   # 1 hour

def _get_insider_trades(ticker: str) -> list[dict]:
    """Return the most recent 10 insider transactions for *ticker*."""
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        df = tk.insider_transactions
        if df is None or df.empty:
            return []
        # Normalise column names (yfinance sometimes returns camelCase)
        df = df.head(10).copy()
        rows = []
        for _, row in df.iterrows():
            rows.append({
                "date":        str(row.get("Start Date") or row.get("startDate") or ""),
                "insider":     str(row.get("Name") or row.get("name") or "—"),
                "title":       str(row.get("Title") or row.get("title") or "—"),
                "transaction": str(row.get("Transaction") or row.get("transaction") or "—"),
                "shares":      int(row.get("Shares") or row.get("shares") or 0),
                "value":       int(row.get("Value") or row.get("value") or 0),
            })
        return rows
    except Exception:
        return []


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
                            "change_shares":    h.get("change_shares"),
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
                "change_shares":    latest_q.get("change_shares"),
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
        import concurrent.futures as _cf_h

        # Fetch ticker info, price history, and SPY history in parallel.
        # Sequential calls took 3-5 s; parallel cuts it to ~max(individual calls).
        # A 15 s timeout prevents hung yfinance workers from blocking Gunicorn.
        def _get_info():
            return yf.Ticker(ticker).info

        def _get_hist():
            return yf.Ticker(ticker).history(period=period, interval=interval)

        def _get_spy():
            if ticker == "SPY":
                return None
            return yf.Ticker("SPY").history(period=period, interval=interval)

        with _cf_h.ThreadPoolExecutor(max_workers=3) as _pool:
            _info_fut = _pool.submit(_get_info)
            _hist_fut = _pool.submit(_get_hist)
            _spy_fut  = _pool.submit(_get_spy)

            try:
                info = _info_fut.result(timeout=15)
            except _cf_h.TimeoutError:
                return jsonify({"error": f"Data fetch timed out for {ticker}"}), 504

            try:
                hist = _hist_fut.result(timeout=15)
            except _cf_h.TimeoutError:
                return jsonify({"error": f"Price history timed out for {ticker}"}), 504

            try:
                _spy_result = _spy_fut.result(timeout=15)
            except Exception:
                _spy_result = None

    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    # Quarterly earnings markers (best-effort; many tickers lack this data)
    earnings_markers = []
    try:
        ed = yf.Ticker(ticker).earnings_dates
        if ed is not None and not ed.empty:
            for dt, row in ed.head(12).iterrows():
                try:
                    surprise_pct = float(row.get("Surprise(%)", 0) or 0)
                    reported_eps = row.get("Reported EPS")
                    estimated_eps = row.get("EPS Estimate")
                    earnings_markers.append({
                        "date":          str(dt.date()),
                        "surprise_pct":  round(surprise_pct, 1) if not math.isnan(surprise_pct) else None,
                        "reported_eps":  round(float(reported_eps), 2) if reported_eps is not None and not math.isnan(float(reported_eps)) else None,
                        "estimated_eps": round(float(estimated_eps), 2) if estimated_eps is not None and not math.isnan(float(estimated_eps)) else None,
                    })
                except Exception:
                    continue
    except Exception:
        pass

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
    volumes = [int(v) if not math.isnan(float(v)) else 0
               for v in hist["Volume"]]

    # Relative strength vs SPY: (stock / SPY) normalised to 100 at period start
    spy_prices_list: list = []
    relative_strength: list = []
    if ticker != "SPY":
        try:
            spy_hist = _spy_result  # already fetched in parallel above
            if spy_hist is not None and not spy_hist.empty:
                spy_raw = [round(float(p), 2) if not math.isnan(float(p)) else None
                           for p in spy_hist["Close"]]
                # Align to same length as ticker (they usually match, but can differ by 1)
                n = min(len(prices), len(spy_raw))
                aligned_prices = prices[:n]
                aligned_spy    = spy_raw[:n]
                spy_prices_list = aligned_spy
                # Find first index where both series have non-null values
                base_idx = next((i for i in range(n)
                                 if aligned_prices[i] is not None and aligned_spy[i] is not None), None)
                if base_idx is not None:
                    base_stock = aligned_prices[base_idx]
                    base_spy   = aligned_spy[base_idx]
                    relative_strength = [
                        round(aligned_prices[i] / aligned_spy[i] * (base_spy / base_stock) * 100, 2)
                        if aligned_prices[i] is not None and aligned_spy[i] is not None and aligned_spy[i] > 0
                        else None
                        for i in range(n)
                    ]
        except Exception:
            log.debug("SPY fetch skipped for relative strength on %s", ticker)

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

    def _fmt_ts(val):
        """Format a list or single timestamp to 'Mon DD, YYYY' string."""
        if not val:
            return None
        try:
            import datetime as _dt
            t = val[0] if isinstance(val, (list, tuple)) else val
            if isinstance(t, (int, float)):
                return _dt.datetime.utcfromtimestamp(t).strftime("%b %d, %Y")
            return str(t)[:10]
        except Exception:
            return None

    def _fmt_ts_single(val):
        """Format a single unix timestamp to 'Mon DD, YYYY' string."""
        if not val:
            return None
        try:
            import datetime as _dt
            return _dt.datetime.utcfromtimestamp(int(val)).strftime("%b %d, %Y")
        except Exception:
            return None

    # Options data is fetched separately via /api/options/<ticker> to keep
    # this endpoint fast.  Options chains require one HTTP call per expiration
    # (30+ for large-cap stocks like JPM) which adds 30+ seconds of serial latency.
    call_wall = None
    put_wall  = None
    put_call_ratio = None
    pc_by_expiry: list = []

    result = {
        "ticker":           ticker,
        "name":             name,
        "dates":            dates,
        "prices":           prices,
        "volumes":           volumes,
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
        "short_ratio":       _safe(info.get("shortRatio")),
        "short_float":       _safe(round(info.get("shortPercentOfFloat") * 100, 1)) if info.get("shortPercentOfFloat") else None,
        "held_insiders":     _safe(round(info.get("heldPercentInsiders") * 100, 1)) if info.get("heldPercentInsiders") else None,
        "held_institutions": _safe(round(info.get("heldPercentInstitutions") * 100, 1)) if info.get("heldPercentInstitutions") else None,
        "dividend_yield":    _safe(dividend_yield_pct(info)),
        "dividend_rate":     _safe(info.get("dividendRate")),
        "payout_ratio":      _safe(round(info.get("payoutRatio") * 100, 1)) if info.get("payoutRatio") else None,
        "ps_ratio":          _safe(ps_ratio(info)),
        "pb_ratio":          _safe(round(info.get("priceToBook"), 2)) if info.get("priceToBook") else None,
        "fcf":               _safe(round(info.get("freeCashflow") / 1e9, 1)) if info.get("freeCashflow") else None,
        # ETF-specific
        "quote_type":        info.get("quoteType"),
        "expense_ratio":     _safe(round((info.get("annualReportExpenseRatio") or info.get("expenseRatio") or 0) * 100, 3))
                             if (info.get("annualReportExpenseRatio") or info.get("expenseRatio")) else None,
        "total_assets":      _safe(round(info.get("totalAssets") / 1e9, 1)) if info.get("totalAssets") else None,
        "etf_yield":         _safe(round(info.get("yield") * 100, 2)) if info.get("yield") else None,
        "nav_price":         _safe(info.get("navPrice")),
        "fund_family":       info.get("fundFamily"),
        "category":          info.get("category"),
        # Key dates
        "earnings_date":     _fmt_ts(info.get("earningsTimestamps") or info.get("earningsDate")),
        "ex_dividend_date":  _fmt_ts_single(info.get("exDividendDate")),
        # Profitability & risk
        "gross_margin":      _safe(round(info.get("grossMargins") * 100, 1)) if info.get("grossMargins") else None,
        "net_margin":        _safe(round(info.get("profitMargins") * 100, 1)) if info.get("profitMargins") else None,
        "roe":               _safe(round(info.get("returnOnEquity") * 100, 1)) if info.get("returnOnEquity") else None,
        "debt_equity":       _safe(round(info.get("debtToEquity"), 2)) if info.get("debtToEquity") else None,
        "beta":              _safe(round(info.get("beta"), 2)) if info.get("beta") else None,
        # High/Low series for Stochastic Oscillator
        "highs":             [round(float(v), 2) if not math.isnan(float(v)) else None for v in hist["High"]],
        "lows":              [round(float(v), 2) if not math.isnan(float(v)) else None for v in hist["Low"]],
        # Analyst consensus
        "recommendation":    info.get("recommendationKey"),
        "recommendation_mean": _safe(round(info.get("recommendationMean"), 2)) if info.get("recommendationMean") else None,
        "analyst_count":     info.get("numberOfAnalystOpinions"),
        "target_high":       _safe(info.get("targetHighPrice")),
        "target_low":        _safe(info.get("targetLowPrice")),
        "target_median":     _safe(info.get("targetMedianPrice")),
        # Insider transactions (most recent 10)
        "insider_trades":    _get_insider_trades(ticker),
        # Relative performance vs sector (current day_chg - sector ETF day_chg)
        "revenue_growth":    _safe(round(info.get("revenueGrowth") * 100, 1)) if info.get("revenueGrowth") else None,
        "operating_margin":  _safe(round(info.get("operatingMargins") * 100, 1)) if info.get("operatingMargins") else None,
        "roa":               _safe(round(info.get("returnOnAssets") * 100, 1)) if info.get("returnOnAssets") else None,
        "current_ratio":     _safe(round(info.get("currentRatio"), 2)) if info.get("currentRatio") else None,
        "shares_outstanding":_safe(round(info.get("sharesOutstanding") / 1e9, 2)) if info.get("sharesOutstanding") else None,
        "float_shares":      _safe(round(info.get("floatShares") / 1e9, 2)) if info.get("floatShares") else None,
        "week52_high":       _safe(info.get("fiftyTwoWeekHigh")),
        "week52_low":        _safe(info.get("fiftyTwoWeekLow")),
        "week52_change":     _safe(round(info.get("52WeekChange") * 100, 1)) if info.get("52WeekChange") else None,
        # Balance sheet
        "total_cash":        _safe(round(info.get("totalCash") / 1e9, 1)) if info.get("totalCash") else None,
        "total_debt":        _safe(round(info.get("totalDebt") / 1e9, 1)) if info.get("totalDebt") else None,
        "interest_coverage": _safe(round(info.get("operatingCashflow") / info.get("interestExpense"), 1))
                             if (info.get("operatingCashflow") and info.get("interestExpense") and info.get("interestExpense") != 0) else None,
        "ytd_return":        _safe(round(info.get("ytdReturn") * 100, 1)) if info.get("ytdReturn") else None,
        "avg_volume":        _safe(info.get("averageVolume")),
        # Relative strength vs SPY (normalised to 100 at start)
        "relative_strength": relative_strength,
        "spy_prices":        spy_prices_list,
        "earnings_markers":  earnings_markers,
        # Classification — used by the research agent for sector-relative strength
        "sector":            info.get("sector"),
        "industry":          info.get("industry"),
    }
    with _HISTORY_CACHE_LOCK:
        _HISTORY_CACHE[cache_key] = {"ts": time.time(), "data": result}
    result["cached_at"] = time.time()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Upcoming earnings endpoint  (/api/upcoming-earnings)
# Returns tickers with earnings in the next 7 days from the history cache.
# ---------------------------------------------------------------------------


@bp.route("/api/upcoming-earnings")
def api_upcoming_earnings():
    """Return tickers with earnings in the next 7 days from the cache."""
    import datetime

    results = []
    seen = set()

    # Check all cached tickers for upcoming earnings dates
    with _HISTORY_CACHE_LOCK:
        cached_tickers = list(_HISTORY_CACHE.keys())

    now = datetime.datetime.utcnow()
    cutoff = now + datetime.timedelta(days=7)

    for cache_key in cached_tickers:
        try:
            ticker = cache_key[0] if isinstance(cache_key, tuple) else cache_key
            if ticker in seen:
                continue
            with _HISTORY_CACHE_LOCK:
                entry = _HISTORY_CACHE.get(cache_key)
            if not entry:
                continue
            data = entry.get("data", {})
            ed = data.get("earnings_date")
            if not ed:
                continue
            # Parse "Mon DD, YYYY" format
            try:
                dt = datetime.datetime.strptime(ed, "%b %d, %Y")
            except ValueError:
                continue
            if now <= dt <= cutoff:
                seen.add(ticker)
                results.append({
                    "ticker": ticker,
                    "name": data.get("name", ticker),
                    "earnings_date": ed,
                    "days_away": (dt - now).days,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["days_away"])
    return jsonify({"upcoming": results[:20]})


# ---------------------------------------------------------------------------
# Options walls endpoint  (/api/options/<ticker>)
# Separated from /api/history so the price/stats page loads instantly.
# Uses a ThreadPoolExecutor to fetch all expirations in parallel.
# ---------------------------------------------------------------------------

_OPTIONS_CACHE: Dict[str, dict] = {}
_OPTIONS_CACHE_LOCK = threading.Lock()
_OPTIONS_CACHE_TTL  = 20 * 60          # 20 minutes
_OPTIONS_MAX_EXPIRATIONS = 12          # cap: covers ~3 months of weeklies + monthlies


@bp.route("/api/options/<ticker>")
def api_options(ticker: str):
    """
    Return call/put walls, overall P/C ratio, and per-expiry P/C data.
    Fetches all option chains in parallel (up to _OPTIONS_MAX_EXPIRATIONS)
    to keep wall-clock time under ~5 s even for large-cap stocks.
    """
    import concurrent.futures as _cf
    import yfinance as _yf

    ticker = ticker.strip().upper()
    log.info("API options: %s", ticker)

    with _OPTIONS_CACHE_LOCK:
        entry = _OPTIONS_CACHE.get(ticker)
        if entry and time.time() - entry["ts"] < _OPTIONS_CACHE_TTL:
            log.debug("Options cache hit: %s", ticker)
            return jsonify(entry["data"])

    call_wall      = None
    put_wall       = None
    put_call_ratio = None
    pc_by_expiry: list[dict] = []

    try:
        tk           = _yf.Ticker(ticker)
        expirations  = (tk.options or [])[:_OPTIONS_MAX_EXPIRATIONS]
        call_oi: dict[float, int] = {}
        put_oi:  dict[float, int] = {}

        def _fetch_chain(exp: str):
            return exp, tk.option_chain(exp)

        with _cf.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_chain, exp): exp for exp in expirations}
            for fut in _cf.as_completed(futures, timeout=30):
                try:
                    exp, chain = fut.result()
                    exp_c = int(chain.calls["openInterest"].dropna().sum())
                    exp_p = int(chain.puts["openInterest"].dropna().sum())
                    if exp_c > 0 or exp_p > 0:
                        pc_by_expiry.append({
                            "exp":     exp,
                            "call_oi": exp_c,
                            "put_oi":  exp_p,
                            "ratio":   round(exp_p / exp_c, 2) if exp_c > 0 else None,
                        })
                    for _, row in chain.calls[["strike", "openInterest"]].dropna().iterrows():
                        s, oi = float(row["strike"]), int(row["openInterest"])
                        call_oi[s] = call_oi.get(s, 0) + oi
                    for _, row in chain.puts[["strike", "openInterest"]].dropna().iterrows():
                        s, oi = float(row["strike"]), int(row["openInterest"])
                        put_oi[s] = put_oi.get(s, 0) + oi
                except Exception as exc:
                    log.debug("Options chain fetch failed %s %s: %s",
                              ticker, futures[fut], exc)

        if call_oi:
            call_wall = max(call_oi, key=call_oi.__getitem__)
        if put_oi:
            put_wall  = max(put_oi,  key=put_oi.__getitem__)
        total_c = sum(call_oi.values())
        total_p = sum(put_oi.values())
        if total_c > 0:
            put_call_ratio = round(total_p / total_c, 2)
        pc_by_expiry.sort(key=lambda x: x["exp"])
        log.info("Options: %s — call_wall=%.2f put_wall=%.2f pcr=%.2f exps=%d",
                 ticker,
                 call_wall or 0, put_wall or 0,
                 put_call_ratio or 0, len(pc_by_expiry))

    except Exception as exc:
        log.warning("Options fetch failed for %s: %s", ticker, exc)

    result = {
        "call_wall":      _safe(call_wall),
        "put_wall":       _safe(put_wall),
        "put_call_ratio": _safe(put_call_ratio),
        "pc_by_expiry":   pc_by_expiry,
    }
    with _OPTIONS_CACHE_LOCK:
        _OPTIONS_CACHE[ticker] = {"ts": time.time(), "data": result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# Sector performance comparison endpoint
# ---------------------------------------------------------------------------
_SECTOR_PERF_CACHE: Dict[str, dict] = {}
_SECTOR_PERF_CACHE_LOCK = threading.Lock()
_SECTOR_PERF_CACHE_TTL  = 4 * 60 * 60   # 4 hours


@bp.route("/api/sector-performance/<path:sector_name>")
def api_sector_performance(sector_name: str):
    """
    Return normalised price series (start=100) for every ticker in a peer group.
    Used by the Performance Comparison tab on the sector detail page.
    Query params:
      period: 1y | 6mo | 3mo | 2y  (default: 1y)
    """
    import yfinance as yf
    from flask import request as flask_request
    period = flask_request.args.get("period", "1y")
    VALID = {"3mo", "6mo", "1y", "2y"}
    if period not in VALID:
        period = "1y"

    if sector_name not in PEER_GROUPS:
        return jsonify({"error": f"Unknown sector: {sector_name}"}), 404

    cache_key = f"{sector_name}:{period}"
    with _SECTOR_PERF_CACHE_LOCK:
        entry = _SECTOR_PERF_CACHE.get(cache_key)
        if entry and time.time() - entry["ts"] < _SECTOR_PERF_CACHE_TTL:
            return jsonify(entry["data"])

    tickers = PEER_GROUPS[sector_name]
    if not tickers:
        return jsonify({"error": "No tickers in this sector"}), 404

    interval = "1wk" if period in ("1y", "2y") else "1d"
    dates: list = []
    series: dict[str, list] = {}

    # Always include SPY as market benchmark
    all_tickers = list(tickers) + ["SPY"] if "SPY" not in tickers else list(tickers)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(tk_sym):
        try:
            tk_hist = yf.Ticker(tk_sym).history(period=period, interval=interval)
            if tk_hist.empty:
                return tk_sym, None
            closes = tk_hist["Close"].dropna()
            if len(closes) < 2:
                return tk_sym, None
            base = float(closes.iloc[0])
            if base == 0:
                return tk_sym, None
            norm = [round(float(v) / base * 100, 2) for v in closes]
            return tk_sym, {"prices": norm, "dates": [str(d.date()) for d in closes.index]}
        except Exception:
            return tk_sym, None

    raw_series: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_one, t): t for t in all_tickers}
        for fut in as_completed(futs, timeout=20):
            try:
                tk_sym, data = fut.result(timeout=8)
                if data:
                    raw_series[tk_sym] = data
            except Exception:
                continue

    # Reconstruct dates/series: use the ticker with the most dates as shared x-axis
    if raw_series:
        best = max(raw_series, key=lambda k: len(raw_series[k]["dates"]))
        dates = raw_series[best]["dates"]
        series = {k: v["prices"] for k, v in raw_series.items()}

    if not series:
        return jsonify({"error": "No price data available"}), 502

    result = {"sector": sector_name, "period": period, "dates": dates, "series": series}
    with _SECTOR_PERF_CACHE_LOCK:
        _SECTOR_PERF_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return jsonify(result)


_SECTOR_RANKING_CACHE: dict = {}
_SECTOR_RANKING_LOCK = threading.Lock()


@bp.route("/api/sector-ranking")
def api_sector_ranking():
    """Composite sector ranking by valuation + momentum.

    For each of the 11 SPDR sector ETFs, fetches PE, P/B, YTD + 6M momentum.
    Returns a ranked list with composite value_score and momentum_score.
    """
    import yfinance as yf

    with _SECTOR_RANKING_LOCK:
        entry = _SECTOR_RANKING_CACHE.get("data")
        if entry and time.time() - entry["ts"] < 3600:
            return jsonify(entry["data"])

    ETF_MAP = {
        "XLK": "Technology", "XLV": "Healthcare", "XLF": "Financials",
        "XLY": "Cons. Disc.", "XLP": "Cons. Staples", "XLE": "Energy",
        "XLI": "Industrials", "XLU": "Utilities", "XLB": "Materials",
        "XLRE": "Real Estate", "XLC": "Comm. Svcs",
    }
    results = []
    try:
        tickers = list(ETF_MAP.keys())
        batch = yf.download(tickers, period="1y", interval="1wk",
                            auto_adjust=True, progress=False, group_by="ticker")
        for etf, sector in ETF_MAP.items():
            try:
                if isinstance(batch.columns, __import__("pandas").MultiIndex):
                    closes = batch[etf]["Close"].dropna()
                else:
                    closes = batch["Close"].dropna()
                if len(closes) < 4:
                    continue
                info = yf.Ticker(etf).info
                ytd_ret = closes.iloc[-1] / closes.iloc[0] * 100 - 100
                m6_ret  = closes.iloc[-1] / closes.iloc[max(0, len(closes)-26)] * 100 - 100
                m1_ret  = closes.iloc[-1] / closes.iloc[max(0, len(closes)-4)]  * 100 - 100
                results.append({
                    "ticker":      etf,
                    "sector":      sector,
                    "pe":          _safe(round(info.get("trailingPE"), 1)) if info.get("trailingPE") else None,
                    "pb":          _safe(round(info.get("priceToBook"), 2)) if info.get("priceToBook") else None,
                    "div_yield":   _safe(dividend_yield_pct(info)),
                    "ytd":         round(float(ytd_ret), 1),
                    "m6":          round(float(m6_ret), 1),
                    "m1":          round(float(m1_ret), 1),
                    "price":       round(float(closes.iloc[-1]), 2),
                })
            except Exception:
                continue
        # Compute composite momentum score (higher = stronger momentum)
        for r in results:
            r["momentum_score"] = round((r["m1"] or 0) * 0.3 + (r["m6"] or 0) * 0.7, 1)
    except Exception as exc:
        log.warning("Sector ranking: %s", exc)

    data = sorted(results, key=lambda x: -(x["momentum_score"] or 0))
    with _SECTOR_RANKING_LOCK:
        _SECTOR_RANKING_CACHE["data"] = {"ts": time.time(), "data": data}
    return jsonify(data)


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
            # Compute margin percentages
            if row.get("revenue") and row["revenue"] != 0:
                row["gross_margin_pct"] = round(row.get("gross_profit", 0) / row["revenue"] * 100, 1) if row.get("gross_profit") else None
                row["net_margin_pct"]   = round(row.get("net_income",  0) / row["revenue"] * 100, 1) if row.get("net_income")  else None
            financials_table.append(row)

    except Exception as exc:
        log.warning("api_financials failed for %s: %s", ticker, exc)

    # ── Price Z-Score (statistical) ─────────────────────────────────
    # z = (current_price - mean_52w) / stdev_52w
    # Measures how far the current price is from the 52-week average
    # in standard deviations.  Positive = above mean, negative = below.
    price_zscore: dict = {}   # {z, mean, stdev, current, high_52w, low_52w, weeks}
    try:
        if "tk" not in dir():
            import yfinance as yf
            tk   = yf.Ticker(ticker)
            info = tk.info

        hist = tk.history(period="1y", interval="1wk")
        if hist is not None and len(hist) >= 4:
            closes = hist["Close"].dropna().tolist()
            current = closes[-1] if closes else None
            if current and len(closes) >= 4:
                import statistics
                mean_val  = statistics.mean(closes)
                stdev_val = statistics.stdev(closes)
                if stdev_val > 0:
                    z = round((current - mean_val) / stdev_val, 2)
                    zone = "high" if z > 2.0 else ("elevated" if z > 1.0 else ("low" if z < -2.0 else ("depressed" if z < -1.0 else "normal")))
                    price_zscore = {
                        "z":       z,
                        "zone":    zone,
                        "mean":    round(mean_val, 2),
                        "stdev":   round(stdev_val, 2),
                        "current": round(current, 2),
                        "high_52w": round(max(closes), 2),
                        "low_52w":  round(min(closes), 2),
                        "weeks":   len(closes),
                    }

        # ── Rolling 14-day Z-Score history ─────────────────────────
        # Fetch daily prices for 1 year, compute a 14-day rolling
        # mean / stdev, then z = (close - SMA14) / rolling_stdev_14.
        daily = tk.history(period="1y", interval="1d")
        if daily is not None and len(daily) >= 20:
            import numpy as np
            dc = daily["Close"].dropna()
            sma14  = dc.rolling(window=14, min_periods=14).mean()
            std14  = dc.rolling(window=14, min_periods=14).std()
            zs_raw = (dc - sma14) / std14
            zs_raw = zs_raw.dropna()

            # Also compute a 14-day smoothed Z-Score (moving average of Z)
            zs_ma  = zs_raw.rolling(window=14, min_periods=1).mean()

            # Align price series with the Z-Score dates
            price_aligned = dc.reindex(zs_raw.index)

            zscore_history = []
            for dt, zv, zmv, pv in zip(zs_raw.index, zs_raw.values, zs_ma.values, price_aligned.values):
                zscore_history.append({
                    "date":  str(dt.date()),
                    "z":     round(float(zv), 3),
                    "z_ma":  round(float(zmv), 3),
                    "price": round(float(pv), 2) if not np.isnan(pv) else None,
                })
            price_zscore["history"] = zscore_history
    except Exception as exc:
        log.warning("Price Z-Score failed for %s: %s", ticker, exc)

    # Quarterly revenue + EPS vs estimates (last 8 quarters)
    quarterly_table = []
    try:
        q_stmt = tk.quarterly_income_stmt
        if q_stmt is not None and not q_stmt.empty:
            q_dates = sorted(q_stmt.columns, reverse=True)[:8]
            for col in q_dates:
                def _q_safe(key):
                    try:
                        v = q_stmt.loc[key, col]
                        return round(float(v) / 1e9, 2) if not math.isnan(float(v)) else None
                    except Exception:
                        return None
                quarterly_table.append({
                    "quarter":      str(col.date()) if hasattr(col, "date") else str(col)[:10],
                    "revenue":      _q_safe("Total Revenue"),
                    "gross_profit": _q_safe("Gross Profit"),
                    "net_income":   _q_safe("Net Income"),
                    "eps_basic":    _q_safe("Basic EPS"),
                })
    except Exception as exc:
        log.warning("Quarterly financials: %s", exc)

    result = {
        "ticker":          ticker,
        "financials_table": financials_table,
        "quarterly_table": quarterly_table,
        "price_zscore":    price_zscore,
    }
    with _FINANCIALS_CACHE_LOCK:
        _FINANCIALS_CACHE[ticker] = {"ts": time.time(), "data": result}
    return jsonify(result)


@bp.route("/api/peers/<ticker>")
def api_peers(ticker: str):
    """Return peer group metrics for the given ticker from the in-memory cache."""
    ticker = ticker.strip().upper()
    # Find which group(s) this ticker belongs to
    peer_group = None
    for group_name, tickers in PEER_GROUPS.items():
        if ticker in tickers:
            peer_group = group_name
            break
    if not peer_group:
        return jsonify({"peers": [], "group": None})

    peers_data = []
    with _cache_lock:
        group_cache = (_cache or {}).get(peer_group, {})
        for t in PEER_GROUPS[peer_group]:
            if t == ticker:
                continue
            cached = group_cache.get(t)
            if not cached:
                continue
            peers_data.append({
                "ticker":       t,
                "name":         cached.get("Name", t),
                "pe":           cached.get("PE (TTM)"),
                "peg":          cached.get("PEG"),
                "fwd_pe":       cached.get("PE (Forward)"),
                "ps":           cached.get("P/S Ratio"),
                "upside":       cached.get("Upside (%)"),
                "rev_growth":   cached.get("Revenue Growth (%)"),
                "day_chg":      cached.get("Day Change (%)"),
                "ytd_return":   cached.get("YTD Return (%)"),
                "week52_change":cached.get("52W Return (%)"),
            })
    peers_data.sort(key=lambda x: abs(x.get("pe") or 0), reverse=False)
    return jsonify({"peers": peers_data[:10], "group": peer_group})


@bp.route("/api/dividend-history/<ticker>")
def api_dividend_history(ticker: str):
    """Return annual dividend payment history for the past 10 years."""
    import yfinance as yf
    ticker = ticker.strip().upper()

    try:
        tk = yf.Ticker(ticker)
        divs = tk.dividends
        if divs is None or divs.empty:
            return jsonify({"dividends": [], "ticker": ticker})

        # Aggregate by year
        annual = {}
        for dt, amt in divs.items():
            year = dt.year
            if year not in annual:
                annual[year] = 0.0
            annual[year] += float(amt)

        # Return last 10 years sorted ascending
        sorted_years = sorted(annual.keys())[-10:]
        result = [{"year": str(y), "amount": round(annual[y], 4)} for y in sorted_years]
        return jsonify({"dividends": result, "ticker": ticker})
    except Exception as exc:
        log.warning("Dividend history: %s %s", ticker, exc)
        return jsonify({"dividends": [], "ticker": ticker})


@bp.route("/api/etf-holdings/<ticker>")
def api_etf_holdings(ticker: str):
    """Top holdings and sector weights for an ETF via yfinance."""
    import yfinance as yf
    ticker = ticker.strip().upper()

    # 4-hour cache
    cache_key = f"etf_holdings_{ticker}"
    with _HISTORY_CACHE_LOCK:
        entry = _HISTORY_CACHE.get(cache_key)
        if entry and time.time() - entry["ts"] < 4 * 3600:
            return jsonify(entry["data"])

    try:
        tk = yf.Ticker(ticker)
        holdings = []
        sector_weights = {}

        # Top holdings
        try:
            fd = tk.funds_data
            if fd is not None:
                th = fd.top_holdings
                if th is not None and not th.empty:
                    # yfinance returns Symbol as the DataFrame *index* and the
                    # weight in a "Holding Percent" column (0-1 scale).
                    for idx, row in th.head(15).iterrows():
                        sym = row.get("Symbol") or row.get("symbol") or row.get("Ticker") or idx
                        wt = None
                        for col in ("Holding Percent", "% Assets", "holdingPercent", "weight"):
                            if col in row and row.get(col) is not None:
                                wt = row.get(col)
                                break
                        holdings.append({
                            "ticker": str(sym or "").strip().upper(),
                            "name":   str(row.get("Name") or row.get("name") or ""),
                            "weight": round(float(wt) * 100, 2) if wt is not None else None,
                        })
                # Sector weights — a plain dict in current yfinance, e.g.
                # {"technology": 0.9926, ...}; older versions returned a frame.
                sw = fd.sector_weightings
                if isinstance(sw, dict):
                    for sec, wt in sw.items():
                        if not sec:
                            continue
                        label = str(sec).replace("_", " ").title()
                        sector_weights[label] = round(float(wt) * 100, 2) if wt else 0.0
                elif sw is not None and not sw.empty:
                    for _, row in sw.iterrows():
                        sec = str(row.get("Sector") or row.get("sector") or "")
                        wt  = row.get("Weight (%)") or row.get("sectorWeight") or row.get("weight") or 0
                        if sec:
                            sector_weights[sec] = round(float(wt) * 100, 2) if wt else None
        except Exception as e:
            log.warning("ETF holdings: funds_data failed for %s: %s", ticker, e)

        # Fallback: try direct info
        if not holdings:
            info = tk.info
            top = info.get("holdings", [])
            for h in top[:15]:
                holdings.append({
                    "ticker": h.get("symbol") or h.get("ticker", ""),
                    "name":   h.get("holdingName") or h.get("name", ""),
                    "weight": round(float(h.get("holdingPercent") or 0) * 100, 2),
                })

        result = {"ticker": ticker, "holdings": holdings, "sector_weights": sector_weights}
        with _HISTORY_CACHE_LOCK:
            _HISTORY_CACHE[cache_key] = {"ts": time.time(), "data": result}
        return jsonify(result)
    except Exception as exc:
        log.warning("ETF holdings: %s %s", ticker, exc)
        return jsonify({"ticker": ticker, "holdings": [], "sector_weights": {}}), 200


@bp.route("/api/search")
def api_search():
    """Autocomplete: return matching tickers from the cached peer groups."""
    q = request.args.get("q", "").strip().upper()
    if len(q) < 1:
        return jsonify([])

    results = []
    seen: set[str] = set()

    # Build a flat ticker→group lookup once
    ticker_group: dict[str, str] = {}
    for g, ts in PEER_GROUPS.items():
        for t in ts:
            if t not in ticker_group:
                ticker_group[t] = g

    def _get_cached_name(t: str) -> str:
        g = ticker_group.get(t, "")
        with _cache_lock:
            return ((_cache or {}).get(g, {}).get(t) or {}).get("Name", "")

    # First pass: exact ticker prefix matches
    for t, g in ticker_group.items():
        if t not in seen and t.startswith(q):
            seen.add(t)
            results.append({
                "ticker": t,
                "name":   _get_cached_name(t),
                "group":  g,
            })

    # Second pass: name contains (case-insensitive)
    if len(results) < 8:
        q_lower = q.lower()
        for t, g in ticker_group.items():
            if t not in seen:
                name = _get_cached_name(t)
                if q_lower in name.lower():
                    seen.add(t)
                    results.append({
                        "ticker": t,
                        "name":   name,
                        "group":  g,
                    })

    return jsonify(results[:10])


@bp.route("/api/search/semantic", methods=["POST"])
def api_search_semantic():
    """Gemini-powered semantic stock search — finds tickers matching a natural-language query."""
    import os
    from flask import request as flask_req

    body = flask_req.get_json(force=True, silent=True) or {}
    query = str(body.get("query", "")).strip()[:200]
    lang  = str(body.get("lang", "en"))
    if not query:
        return jsonify([])

    # Build context from cached peer data (compact representation)
    from ystocker import PEER_GROUPS
    rows = []
    with _cache_lock:
        cache_snapshot = dict(_cache or {})
    for group_data in cache_snapshot.values():
        if not isinstance(group_data, dict):
            continue
        for t, d in group_data.items():
            if not isinstance(d, dict):
                continue
            rows.append(
                f"{t}|{d.get('Name','')[:25]}|{d.get('PE (TTM)','')}|"
                f"{d.get('Dividend Yield (%)','')}{d.get('Revenue Growth (%)','')}"
                f"|{d.get('Short Float (%)','')}"
            )
    context = "\n".join(rows[:200])  # limit context size

    try:
        from google import genai
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        zh = lang == "zh"
        prompt = (
            f"Stock screener. Given this stock data (Ticker|Name|PE|DivYield%|RevGrowth%|ShortFloat%):\n"
            f"{context}\n\n"
            f"User query: '{query}'\n\n"
            f"Return the top 5 matching tickers as JSON array: "
            f'[{{"ticker":"XXX","name":"...","reason":"{("原因" if zh else "reason")}..."}}]. '
            f"{'用中文回答原因。' if zh else 'Keep reasons to 10 words max.'} "
            f"Return ONLY valid JSON, no markdown."
        )
        resp = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
        )
        import json as _json
        text = resp.text.strip().lstrip("```json").rstrip("```").strip()
        results = _json.loads(text)
        if isinstance(results, list):
            return jsonify(results[:5])
        return jsonify([])
    except Exception as exc:
        log.warning("Semantic search failed: %s", exc)
        return jsonify([])


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


@bp.route("/tv")
def tv():
    """Full-screen rotating dashboard for a wall display or TV.

    Standalone template on purpose: no nav, no floating launcher, no Tailwind. A
    kiosk has nobody to notice a broken layout, and tailwind.css here is a compiled
    artifact that has silently dropped arbitrary classes before.

    Reads the same public JSON the site already serves, so there is no new backend
    and no new cache to go stale. Query params: ?secs= per slide, ?refresh= minutes
    between fetches, ?slides=indices,sectors,vol,risk to pin one view, ?lang=zh.
    """
    lang = "zh" if request.args.get("lang") == "zh" else "en"
    log.info("GET /tv (lang=%s, slides=%s)", lang, request.args.get("slides") or "all")
    return render_template("tv.html", lang=lang,
                           market_holidays=_tv_holiday_list())


# ── /tv supporting endpoints ─────────────────────────────────────────────────
# The kiosk used to fetch /api/markets, /api/fear-greed and /api/etf-returns
# whole and read a handful of fields out of each. That is 300 KB of JSON for
# about forty numbers, and it cannot show a metric that does not already have a
# page-sized endpoint. These two replace that: a compact digest of everything
# the display actually paints, and a quotes-only route cheap enough to poll.

_TV_DIGEST_CACHE: dict = {}
_TV_DIGEST_LOCK = threading.Lock()
_TV_DIGEST_TTL = 300  # matches _MARKETS_CACHE_TTL; nothing underneath moves faster
# Single-flight. Without it every concurrent poll starts its own build, and with
# only two gunicorn workers two slow builds is the whole site.
_TV_DIGEST_BUILDING = threading.Event()


def _dt_now_iso() -> str:
    import datetime as _dt_mod

    return _dt_mod.date.today().isoformat()


def _tv_holiday_list() -> list:
    """US market holidays for this year and next, as ISO strings.

    Handed to the template because the kiosk decides its own refresh cadence and
    there is no JS holiday calendar in this app. Two years so a display left
    running over New Year does not lose holiday awareness at midnight.
    """
    try:
        import datetime as _dt_mod

        this_year = _dt_mod.date.today().year
        out: list = []
        for yr in (this_year, this_year + 1):
            out.extend(sorted(d.isoformat() for d in _us_market_holidays(yr)))
        return out
    except Exception as exc:  # noqa: BLE001 - a kiosk without holidays still works
        log.warning("TV: holiday list failed: %s", exc)
        return []


def _tv_json(view, label: str = "TV digest"):
    """Call one of this module's own API views and return its JSON body.

    Composing the views rather than the fetchers under them is deliberate: each
    already owns its cache, its TTL and its degradation behaviour, so the digest
    inherits all of that and adds no upstream load. The markets warm-up thread
    already calls api_markets() this way.

    ``label`` only names the caller in the log lines; the market brief reuses
    this helper and would otherwise report its failures as TV digest failures.
    """
    name = getattr(view, "__name__", str(view))
    try:
        rv = view()
    except Exception as exc:  # noqa: BLE001 - one dead source must not kill the digest
        log.warning("%s: %s raised: %s", label, name, exc)
        return None
    body, code = (rv[0], rv[1]) if isinstance(rv, tuple) else (rv, 200)
    if code != 200:
        log.info("%s: %s returned HTTP %s — skipped", label, name, code)
        return None
    try:
        return body.get_json()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: %s body was not JSON: %s", label, name, exc)
        return None


def _tv_markets_cached() -> Optional[dict]:
    """The markets payload, but only if it is already in memory.

    Deliberately not api_markets(): that function will fall through to a full
    Yahoo rebuild of ~30 symbols with a year of history each, which took 120s in
    a request and got the worker SIGKILLed by gunicorn's --timeout. On a box with
    two workers that is half the site gone for two minutes, and the kiosk polls
    this route.

    Staleness is accepted rather than refreshed. A wall display showing
    five-minute-old sector bars is fine; a display showing nothing because it
    triggered a fetch it then died waiting for is not. The markets warm-up thread
    is what keeps this current.
    """
    try:
        with _MARKETS_CACHE_LOCK:
            entry = _MARKETS_CACHE.get("data")
        if not entry:
            log.info("TV digest: markets not in memory yet — section skipped")
            return None
        age = time.time() - entry.get("ts", 0)
        if age > _MARKETS_CACHE_TTL:
            log.info("TV digest: serving markets %.0fs stale rather than refetching", age)
        return entry.get("data")
    except Exception as exc:  # noqa: BLE001
        log.warning("TV digest: markets cache read failed: %s", exc)
        return None


def _tv_spark(values, keep: int = 40, nd: int = 2) -> list:
    """Tail of a series, thinned and rounded — a sparkline, not a dataset.

    A kiosk sparkline is ~120 px wide, so shipping ten years of weekly points
    would be most of the payload for pixels nobody can resolve.
    """
    nums = [v for v in (values or []) if isinstance(v, (int, float))]
    return [round(float(v), nd) for v in nums[-keep:]]


def _tv_pctile(values, latest) -> Optional[float]:
    """Percentile rank of ``latest`` within ``values``."""
    nums = [v for v in (values or []) if isinstance(v, (int, float))]
    if not nums or not isinstance(latest, (int, float)):
        return None
    return round(sum(1 for v in nums if v <= latest) / len(nums) * 100, 1)


def _tv_build_digest() -> dict:
    """Assemble the kiosk digest from data the app has already cached."""
    import datetime as _dt_mod

    out: dict = {"as_of": _dt_mod.date.today().isoformat()}

    # ── Breadth: how much of the index is actually participating ──────────
    br = _tv_json(api_breadth)
    if br:
        latest = br.get("latest") or {}
        series = (br.get("pct_above_200ma") or {}).get("values")
        out["breadth"] = {
            "latest": {k: latest.get(k) for k in ("20", "50", "100", "150", "200")
                       if isinstance(latest.get(k), (int, float))},
            "asof": br.get("asof"),
            "universe": br.get("universe"),
            "spark200": _tv_spark(series, 40, 1),
        }

    # ── Valuation: the level and, more usefully, where it sits historically ──
    try:
        from ystocker import valuation as _val
        vd = _val.get_valuation_data()
        head = (vd or {}).get("headline") or {}
        pe, cape = head.get("spx_trailing_pe") or {}, head.get("spx_cape") or {}
        fwd = (vd or {}).get("spx_consensus_fwd") or {}
        blk = {
            "pe": pe.get("value"),
            "pe_pct": (head.get("spx_pe_percentile") or {}).get("value"),
            "cape": cape.get("value"),
            "cape_pct": (head.get("spx_cape_percentile") or {}).get("value"),
        }
        fv = fwd.get("values") or []
        if fv:
            blk.update({
                "fwd": fv[-1],
                "fwd_date": (fwd.get("dates") or [None])[-1],
                "fwd_lo": min(fv), "fwd_hi": max(fv),
                "fwd_pct": _tv_pctile(fv, fv[-1]),
                "fwd_spark": _tv_spark(fv, 40, 1),
            })
        # Our own bottom-up reading, which is a different basis from FactSet's
        # and is labelled as such on the display.
        for etf in ("SPY", "QQQ"):
            got = ((vd or {}).get("forward") or {}).get(etf) or {}
            if got.get("forward_pe"):
                blk[f"{etf.lower()}_fwd"] = got["forward_pe"]
        if any(v is not None for v in blk.values()):
            out["valuation"] = blk
    except Exception as exc:  # noqa: BLE001
        log.warning("TV digest: valuation failed: %s", exc)

    # ── Macro: the curve and what the market thinks the Fed does next ──────
    ys = _tv_json(api_yield_spread)
    if ys and ys.get("spread"):
        sp = [v for v in ys["spread"] if isinstance(v, (int, float))]
        if sp:
            out.setdefault("macro", {}).update({
                "spread": round(sp[-1], 2),
                "inverted": sp[-1] < 0,
                "spread_spark": _tv_spark(sp, 40, 2),
            })
    fw = _tv_json(api_fedwatch)
    if fw and fw.get("status") == "ok":
        cur = fw.get("current") or {}
        nxt = (fw.get("meetings") or [{}])[0]
        out.setdefault("macro", {}).update({
            "effr": cur.get("effr"), "effr_label": cur.get("label"),
            "next_meeting": {
                "date": nxt.get("date"), "cut": nxt.get("cut_prob"),
                "hold": nxt.get("hold_prob"), "hike": nxt.get("hike_prob"),
                "change_bp": nxt.get("change_bp"),
            },
        })
    try:
        from ystocker import fed as _fed
        fs = ((_fed.get_fed_data() or {}).get("series") or {}).get("WALCL") or {}
        vals = [v for v in (fs.get("values") or []) if isinstance(v, (int, float))]
        if vals:
            # fed.py's module docstring calls WALCL "millions USD", which is
            # FRED's native unit, but what lands in its cache is billions --
            # 6759.95 is $6.76T, not $6.76B. Dividing by 1e6 as the docstring
            # implies renders Fed total assets as 0.01T on the wall.
            out.setdefault("macro", {}).update({
                "fed_assets_t": round(vals[-1] / 1e3, 2),
                # Weekly series, so 13 rows back is about a quarter. Reported in
                # billions because the quarterly move is tens of billions and
                # would round to 0.0 in trillions.
                "fed_chg_13w_b": (round(vals[-1] - vals[-14], 1)
                                  if len(vals) > 14 else None),
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("TV digest: fed assets failed: %s", exc)

    # ── Sectors and cross-asset, ranked. Both come out of the markets cache,
    # which already carries a year of daily closes per series, so the sparklines
    # cost nothing extra upstream.
    mk = _tv_markets_cached()
    if mk:
        secs = []
        for s in (mk.get("sectors") or []):
            if not isinstance(s, dict):
                continue
            secs.append({"t": s.get("ticker"), "label": s.get("label"),
                         "wk": s.get("week_chg_pct"), "day": s.get("day_chg")})
        secs = [s for s in secs if isinstance(s.get("wk"), (int, float))]
        secs.sort(key=lambda s: s["wk"], reverse=True)
        if secs:
            out["sectors"] = secs
        cross = []
        for key in ("gold", "silver", "oil", "brent", "copper", "dxy", "natgas"):
            blk = (mk.get("indices") or {}).get(key) or {}
            if not isinstance(blk, dict):
                continue
            # Field names here are the ones /api/markets actually ships for an
            # index block -- day_chg (already a percent) and ytd. The richer
            # day_chg_pct / ret_ytd names exist elsewhere in this file for a
            # different builder and are absent here, so reading those silently
            # yields a row of Nones.
            row = {"k": key, "cur": blk.get("current"),
                   "day": blk.get("day_chg"), "ytd": blk.get("ytd")}
            if row["cur"] is not None:
                daily = (blk.get("daily") or {})
                row["spark"] = _tv_spark(daily.get("prices") or daily.get("values"), 30, 2)
                cross.append(row)
        if cross:
            out["cross"] = cross
    return out


@bp.route("/api/tv-digest")
def api_tv_digest():
    """Compact metrics digest for /tv — a few KB instead of ~300.

    Never blocks on an upstream and never lets two builds run at once. A kiosk
    polls this on a timer and will keep polling through any outage, so the
    failure mode to avoid is a queue of requests each holding a worker.
    """
    with _TV_DIGEST_LOCK:
        entry = _TV_DIGEST_CACHE.get("data")
    if entry and time.time() - entry["ts"] < _TV_DIGEST_TTL:
        return jsonify(entry["data"])

    # A build is already running: hand back the previous answer rather than
    # starting a second one. Stale beats slow on a display.
    if _TV_DIGEST_BUILDING.is_set():
        if entry:
            return jsonify(entry["data"])
        return jsonify({"as_of": _dt_now_iso(), "warming": True})

    _TV_DIGEST_BUILDING.set()
    try:
        data = _tv_build_digest()
    finally:
        _TV_DIGEST_BUILDING.clear()

    # Only cache something worth serving, so a transient all-sources-down does
    # not get pinned for five minutes. If this build came back empty but a older
    # one is held, the older one is still the better answer.
    if len(data) > 1:
        with _TV_DIGEST_LOCK:
            _TV_DIGEST_CACHE["data"] = {"ts": time.time(), "data": data}
    elif entry:
        log.warning("API tv-digest: build produced nothing — serving previous")
        return jsonify(entry["data"])
    log.info("API tv-digest: built with %s", ", ".join(k for k in data if k != "as_of") or "nothing")
    return jsonify(data)


@bp.route("/guide")
def guide():
    # The pack ladder comes from the same table /agents and yPay use, so the
    # guide cannot quote a price the checkout does not charge. Empty when selling
    # is off, which the template says explicitly rather than showing a bare table.
    return render_template("guide.html", peer_groups=list(PEER_GROUPS.keys()),
                           agent_packs=_agent_packs_for_page())


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


# ---------------------------------------------------------------------------
# FedWatch — FOMC rate-move probabilities from Fed Funds futures
# ---------------------------------------------------------------------------

@bp.route("/fedwatch")
def fedwatch():
    """Page showing implied FOMC rate-move probabilities (CME FedWatch style)."""
    log.info("GET /fedwatch")
    from ystocker.fedwatch import get_cache_ts, is_cache_fresh, is_warming as fw_warming_fn
    return render_template(
        "fedwatch.html",
        peer_groups=list(PEER_GROUPS.keys()),
        cache_last_updated=get_cache_ts(),
        cache_fresh=is_cache_fresh(),
        warming=fw_warming_fn(),
    )


@bp.route("/fedwatch/refresh")
def fedwatch_refresh():
    """Kick off a background re-fetch of the Fed Funds futures curve."""
    from ystocker.fedwatch import refresh_cache
    threading.Thread(target=refresh_cache, daemon=True, name="fedwatch-manual-refresh").start()
    return redirect(url_for("main.fedwatch"))


@bp.route("/api/fedwatch")
def api_fedwatch():
    """JSON API — implied probability of each target range at each FOMC meeting.

    Mirrors /api/fed: serve a fresh cache immediately, otherwise return 202 so
    the page can poll instead of holding a gunicorn worker through a
    yfinance + FRED round trip.
    """
    try:
        from ystocker.fedwatch import (
            get_fedwatch_data,
            is_cache_fresh,
            is_warming as fw_warming_fn,
            refresh_cache,
        )

        if is_cache_fresh():
            data = get_fedwatch_data()
            if not data or not data.get("meetings"):
                log.warning("API fedwatch: fresh check passed but payload has no meetings")
                return jsonify({"status": "stale", "error": "Cache data inconsistent",
                                "warming": True}), 202
            resp = _with_freshness(data)
            resp["status"] = "ok"
            log.info("API fedwatch: served from cache (%d meetings, age %s)",
                     len(resp["meetings"]), resp["meta"]["age_label"])
            return jsonify(resp)

        if fw_warming_fn():
            log.info("API fedwatch: warming in progress, returning 202")
            return jsonify({"status": "warming", "warming": True}), 202

        log.info("API fedwatch: no cache, starting background fetch")
        threading.Thread(target=refresh_cache, daemon=True, name="fedwatch-auto-warm").start()
        return jsonify({"status": "initializing", "warming": True}), 202
    except Exception as exc:
        log.error("API fedwatch: error: %s", exc, exc_info=True)
        return jsonify({"status": "error", "error": str(exc), "warming": True}), 500


# ---------------------------------------------------------------------------
# Trading agents — gated, subprocess-per-run
# ---------------------------------------------------------------------------

def _agent_user() -> Optional[str]:
    """Signed-in email, or None."""
    return session.get("user_email")


def _reader_tz() -> str:
    """The IANA zone the browser reported, or "" .

    Sent as ?tz= on a PDF link because a PDF is rendered server-side and the box
    has no idea where the reader is. Validated only for shape here; report_pdf
    falls back through the configured zone to UTC if it cannot load it.
    """
    tz = (request.args.get("tz") or "").strip()
    # An IANA id, not free text: it is interpolated into a zoneinfo lookup, and
    # 64 characters is far more than "America/Argentina/ComodRivadavia" needs.
    if len(tz) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz or "x"):
        return ""
    return tz


def _agent_packs_for_page():
    """Run packs for the page, or [] if the credits module is unavailable.

    Never raises: the packs are an upsell, and /agents must render without them.
    """
    try:
        from ystocker import credits

        ok, why = credits.selling_enabled()
        if not ok:
            log.warning("agents: not offering run packs — %s", why)
            return []
        return credits.packs_public()
    except Exception as exc:  # noqa: BLE001
        log.warning("agents: packs unavailable: %s", exc)
        return []


def _agent_gate():
    """Return an error Response if the caller may not view agent runs, else None.

    Signing in is the requirement; how much a signed-in user may *spend* is the
    quota's job, checked at submit time rather than here, so reading a finished
    report never consumes anything. 403 remains reachable only through the
    optional ``AGENTS_ALLOWED_EMAILS`` override.
    """
    from ystocker.agents import is_allowed

    email = _agent_user()
    if not email:
        return jsonify({"error": "Sign in required", "reason": "auth"}), 401
    if not is_allowed(email):
        return jsonify({"error": "Agent runs are currently restricted to "
                                 "specific accounts",
                        "reason": "forbidden"}), 403
    return None


@bp.route("/agents")
def agents_page():
    """Page for running the TradingAgents analysis."""
    from ystocker.agents import environment_report, is_allowed, showcase_enabled

    email = _agent_user()
    embedded = request.args.get("embed") == "1"
    log.info("GET /agents (user=%s, embedded=%s)", email or "anon", embedded)
    from ystocker import quota
    from ystocker.agent_roles import roles_json

    return render_template(
        "agents.html",
        peer_groups=list(PEER_GROUPS.keys()),
        agent_env=environment_report(),
        signed_in=bool(email),
        allowed=is_allowed(email),
        user_email=email or "",
        quota=quota.usage(email) if email else None,
        # The ladder is rendered server-side so the prices are visible without a
        # round trip, and come from one table shared with yPay.
        agent_packs=_agent_packs_for_page(),
        agent_roles=roles_json(),
        agent_embedded=embedded,
        # Only consulted by the not-signed-in branch, which shows a sample of
        # finished reports instead of a dead end.
        showcase=showcase_enabled(),
    )


@bp.route("/api/agents/run", methods=["POST"])
def api_agents_run():
    """Queue a run. Returns a job id to poll."""
    gate = _agent_gate()
    if gate:
        return gate
    from ystocker import quota
    from ystocker.agents import submit

    body = request.get_json(force=True, silent=True) or {}
    email = _agent_user() or ""
    selftest = bool(body.get("selftest"))

    # Validate before charging quota, so a typo'd ticker does not cost a run.
    # Self-tests make no LLM call, so they are free and deliberately unmetered.
    charged = False
    paid_run = False
    if not selftest:
        ok, reason, info = quota.try_consume(email)
        if not ok:
            from ystocker import credits

            if reason == "global":
                # Not offered a top-up: this ceiling is about what the box and
                # the upstream model quota can deliver today, so selling a credit
                # would be selling capacity that does not exist.
                msg = ("The site-wide daily limit for agent runs has been "
                       "reached. This protects the shared API budget — please "
                       "try again tomorrow.")
                buy = None
            else:
                msg = (f"You have used your {info['limit']} free runs for today "
                       f"(resets at midnight, {info['tz']}). "
                       f"Buy more runs to keep going.")
                buy = {"url": credits.PAY_URL, "packs": credits.packs_public()}
            log.info("agents: quota denied (%s) for %s: %s", reason, email, info)
            payload = {"error": msg, "reason": "quota", "quota": info}
            if buy:
                payload["buy"] = buy
            return jsonify(payload), 429
        charged = True
        paid_run = bool(info.get("paid"))

    job_id, err = submit(
        ticker=body.get("ticker", ""),
        day=body.get("date", ""),
        user=email,
        selftest=selftest,
        # The report is written in the language the page is being read in. A
        # caller that sends nothing gets English, as everywhere else here.
        lang="zh" if body.get("lang") == "zh" else "en",
        paid=paid_run,
    )
    if err:
        # Rejected before anything was spent, so hand the run back.
        if charged:
            quota.refund(email, paid=paid_run)
        return jsonify({"error": err, "quota": quota.usage(email)}), 400
    return jsonify({"job_id": job_id, "status": "queued",
                    "quota": quota.usage(email)}), 202


@bp.route("/api/agents/job/<job_id>")
def api_agents_job(job_id):
    """Poll one job. Readable by the user who ran it, or by a VIP."""
    gate = _agent_gate()
    if gate:
        return gate
    from ystocker.agents import can_read, get_job, owns

    job = get_job(job_id)
    # 404 rather than 403 for a run the caller may not read: a distinguishable
    # "forbidden" would confirm that a given job id exists, and the ids are the
    # only thing protecting one user's transcript from another's guesses.
    if not job or not can_read(job, _agent_user()):
        return jsonify({"error": "No such job"}), 404

    # Split into role turns here rather than in the browser. The page renders
    # the report as a conversation between the agents, and doing the split
    # server-side keeps one implementation of it (agent_roles.split_sections)
    # instead of a JavaScript copy that can drift on the awkward cases -- the
    # section bodies contain their own markdown headings.
    from ystocker.agent_roles import split_sections

    payload = dict(job)
    report = payload.pop("report", None) or ""
    payload["sections"] = split_sections(report) if report.strip() else []

    # Live progress. A deep run takes ten minutes or more, and the finished
    # report only exists at the very end, so the page would otherwise show a
    # spinner for the whole time. The child publishes each agent's output as it
    # lands; ``since`` is the client's cursor so a poll carries only what is new
    # rather than resending the whole transcript every few seconds.
    #
    # Served over the existing poll on purpose, not Server-Sent Events: this box
    # runs 2 gunicorn workers, and an SSE connection held open for a
    # thirteen-minute run would occupy half the server's capacity per viewer.
    from ystocker import quota
    from ystocker.agents import read_events

    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    events = read_events(job_id, since) if job.get("status") != "done" or since == 0 else []
    payload["events"] = events
    payload["event_cursor"] = max([since] + [int(e.get("seq", 0)) for e in events])
    # The raw markdown is dropped from the response because nothing on the page
    # needs it once it is split, and it would double a 40 KB payload on every
    # poll. The PDF route reads the job from disk, so it is unaffected.
    payload["has_report"] = bool(report.strip())
    payload["chat"] = job.get("chat") or []
    # Whether this viewer may add a follow-up. A VIP can *read* anyone's run but
    # not write to it, so the page must know the difference: without this the chat
    # box would render on someone else's report and every question would 404.
    payload["can_chat"] = owns(job, _agent_user())
    # Remaining follow-ups, so the allowance is visible before the first question
    # rather than appearing only after one has been spent.
    payload["chat_quota"] = {
        "remaining": max(0, quota.limit_chat() - _chat_used_today(_agent_user())),
        "limit": quota.limit_chat(),
    }
    return jsonify(payload)


@bp.route("/api/agents/job/<job_id>/pdf")
def api_agents_job_pdf(job_id):
    """Download a finished run's full report as a PDF."""
    gate = _agent_gate()
    if gate:
        return gate
    from ystocker.agents import can_read, get_job
    from ystocker.report_pdf import build_report_pdf, pdf_filename

    job = get_job(job_id)
    if not job or not can_read(job, _agent_user()):
        return jsonify({"error": "No such job"}), 404
    if job.get("status") != "done":
        return jsonify({"error": f"Run is {job.get('status')}, not done"}), 409

    pdf = build_report_pdf(job, tz=_reader_tz())
    if not pdf:
        return jsonify({"error": "Could not render a PDF for this run"}), 500

    log.info("agents: served PDF for %s (%d bytes)", job_id, len(pdf))
    return Response(pdf, content_type="application/pdf", headers={
        # attachment so the browser downloads rather than previews inline.
        "Content-Disposition": f'attachment; filename="{pdf_filename(job)}"',
        "Content-Length": str(len(pdf)),
        "Cache-Control": "private, max-age=300",
    })


def _chat_used_today(email: Optional[str]) -> int:
    """Follow-up questions this address has already asked today."""
    from ystocker import quota

    try:
        with quota._Guard():
            data = quota._read(quota.today())
        return int((data.get("chat") or {}).get((email or "").strip().lower(), 0))
    except Exception:  # noqa: BLE001 - a counter read must not break the poll
        return 0


@bp.route("/api/agents/job/<job_id>/chat", methods=["POST"])
def api_agents_chat(job_id):
    """Ask the Portfolio Manager a follow-up about a finished run.

    One grounded Flash call against the stored report, not another run: the
    analysis already exists, and re-running the graph to answer a question would
    cost ~22 Pro calls and ten minutes.
    """
    gate = _agent_gate()
    if gate:
        return gate
    from datetime import datetime, timezone

    from ystocker import quota
    from ystocker.agents import (append_chat, ask_manager, chat_turns,
                                 get_job, owns)

    email = _agent_user()
    job = get_job(job_id)
    # 404 for someone else's run, as everywhere else here: a distinguishable
    # 403 would confirm the id exists.
    if not job or not owns(job, email):
        return jsonify({"error": "No such job"}), 404
    if job.get("status") != "done":
        return jsonify({"error": f"Run is {job.get('status')}, not done"}), 409

    body = request.get_json(force=True, silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Ask a question first"}), 400
    from ystocker.agents import CHAT_QUESTION_MAX

    if len(question) > CHAT_QUESTION_MAX:
        return jsonify({"error": f"Question is too long (max {CHAT_QUESTION_MAX} characters)"}), 400

    # Charged against its own daily allowance, never the run quota.
    ok, info = quota.try_consume_chat(email)
    if not ok:
        return jsonify({"error": (f"You have used your {info['limit']} follow-up "
                                  f"questions for today."),
                        "reason": "quota", "chat_quota": info}), 429

    answer, err = ask_manager(job, question)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if err:
        # The question is still recorded, so the user can see what they asked and
        # retry without retyping it.
        append_chat(job_id, [{"role": "user", "text": question, "at": now}])
        return jsonify({"error": err, "chat_quota": info}), 502

    updated = append_chat(job_id, [
        {"role": "user", "text": question, "at": now},
        {"role": "manager", "text": answer, "at": now},
    ])
    log.info("agents: follow-up answered for %s (%d chars)", job_id, len(answer))
    return jsonify({"chat": chat_turns(updated or job), "chat_quota": info})


@bp.route("/api/agents/jobs")
def api_agents_jobs():
    """Recent runs: the caller's own, or everyone's for a VIP."""
    gate = _agent_gate()
    if gate:
        return gate
    from ystocker import quota
    from ystocker.agents import list_jobs

    viewer = _agent_user()
    vip = quota.is_vip(viewer)
    return jsonify({"jobs": list_jobs(20, user=viewer, all_users=vip),
                    # The page labels rows it does not own, which it can only do
                    # if it knows who is looking.
                    "viewer": viewer or "", "vip": vip})


@bp.route("/api/agents/search")
def api_agents_search():
    """Search analysis reports by ticker or analysis date.

    Query params:
      q      = ticker ("NVDA", "NV") or analysis date prefix ("2026-08")
      status = optional exact filter: queued | running | done | error
      limit  = max hits to return (1-60, default 50)

    A call with ``q`` set is a search result, and each hit carries a ``portfolio``
    object -- the Portfolio Manager's turn (投资组合经理), body and role metadata --
    or null where the report has no such section, which a self-test fixture and a
    run that died before the decision both produce. Without ``q`` this is the
    recent-runs index and the key is absent entirely, which is a distinct case
    from null.

    A search by ``q`` also drops runs with no report at all, counting them in
    ``skipped_empty``. The index and an explicit ``status`` still show them, so a
    failed run stays findable.

    Scoped to the caller's own runs by ``search_jobs``, same as every other agent
    read: the reports state entry levels and position sizes. A VIP searches every
    user's, matching what a VIP is allowed to open.
    """
    gate = _agent_gate()
    if gate:
        return gate
    from ystocker import quota
    from ystocker.agents import search_jobs

    q = request.args.get("q", "")[:32]
    status = request.args.get("status", "")
    if status and status not in ("queued", "running", "done", "error"):
        return jsonify({"error": f"Unknown status filter '{status}'"}), 400
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    # Clamped rather than rejected: a bad limit is not worth failing a search
    # over, and an unbounded one would let a query read every record on disk.
    limit = max(1, min(limit, 60))

    viewer = _agent_user()
    vip = quota.is_vip(viewer)
    # This endpoint serves two views, and both extras below belong to the second.
    # With no ``q`` it is the "Recent runs" index — including when the status
    # dropdown narrows it, since almost every record is "done" and that filter
    # therefore narrows very little. Once ``q`` names something, it is a search
    # result: a short list the reader means to read.
    #
    # So a searched hit carries the Portfolio Manager's turn in full and the call
    # is readable without opening the report. Not on the index: that is 50 full
    # sections on every page load, six times the payload, and fifty stacked
    # decisions is a wall of text rather than a list one can scan. Note the JSON
    # is ASCII-escaped, so a Chinese section costs ~6 bytes per character — the
    # reason this is worth gating at all rather than always sending.
    #
    # A searched hit with no report body is skipped as well — no decision, no
    # sections, and a PDF link that 404s. Two exemptions keep failures findable.
    # The index shows everything, because it is the only place a user sees that
    # their own run failed, and most records here are errored runs, so the panel
    # would go nearly blank. And an explicit status filter wins outright: queued,
    # running and error runs have no report *by definition*, so skipping there
    # would make three of the four dropdown choices always return nothing.
    searching = bool(q.strip())
    out = search_jobs(q, user=viewer, status=status, limit=limit, all_users=vip,
                      require_report=searching and not status,
                      with_portfolio=searching)
    out["viewer"] = viewer or ""
    out["vip"] = vip
    return jsonify(out)


# ---------------------------------------------------------------------------
# Public showcase — a sample of finished reports for visitors who are not
# signed in. These two routes deliberately skip ``_agent_gate()``: every other
# agent read is owner-only, and this is the documented exception. They are safe
# because ``agents._publishable`` strips the record down to an allowlist of
# fields, so no owner address, runner stderr or pid leaves the server.
# ---------------------------------------------------------------------------

@bp.route("/api/agents/showcase")
def api_agents_showcase():
    """A random sample of finished reports. No sign-in required."""
    from ystocker.agents import SHOWCASE_SIZE, showcase_enabled, showcase_jobs

    if not showcase_enabled():
        return jsonify({"jobs": [], "enabled": False})

    try:
        limit = int(request.args.get("limit", SHOWCASE_SIZE))
    except (TypeError, ValueError):
        limit = SHOWCASE_SIZE
    # Clamped rather than rejected, and capped at the default: this endpoint is
    # unauthenticated, so an unbounded limit would let a caller pull every
    # finished report on disk in a single request.
    limit = max(1, min(limit, SHOWCASE_SIZE))
    return jsonify({"jobs": showcase_jobs(limit), "enabled": True})


@bp.route("/api/agents/showcase/<job_id>")
def api_agents_showcase_job(job_id):
    """One sampled report, split into agent turns. No sign-in required."""
    from ystocker.agent_roles import split_sections
    from ystocker.agents import showcase_job

    job = showcase_job(job_id)
    # 404 for anything not in the sample, including a perfectly real run that is
    # simply private. A distinguishable 403 would turn this route into a way to
    # test whether a given job id exists, and the ids are the only thing
    # protecting one user's transcript from another's guesses.
    if not job:
        return jsonify({"error": "No such report"}), 404

    # Split server-side for the same reason the owner's poll does it: one
    # implementation of the awkward cases, in ystocker/agent_roles.py.
    report = job.pop("report", None) or ""
    job["sections"] = split_sections(report) if report.strip() else []
    job["has_report"] = bool(report.strip())
    log.info("agents: showcase served report %s (%d sections)",
             job_id, len(job["sections"]))
    return jsonify(job)


# Rendered showcase PDFs, keyed by job id. Building one runs report_charts and
# embeds a CJK font, which is real CPU on a box with two vCPUs shared by eight
# apps -- and unlike the owner's PDF route this one is unauthenticated, so the
# same handful of reports would otherwise be re-rendered for every passer-by.
#
# Bounded twice over: only ids in the current sample are reachable (``_is_showcase``
# re-checks eligibility, and a job id is 16 hex chars so it cannot be guessed),
# and the dict is capped below. Cleared wholesale rather than by LRU because at
# this size tracking recency costs more than the occasional rebuild.
_SHOWCASE_PDF_MAX = 12
_showcase_pdfs: dict[str, bytes] = {}
_showcase_pdf_lock = threading.Lock()


@bp.route("/api/agents/showcase/<job_id>/pdf")
def api_agents_showcase_job_pdf(job_id):
    """A sampled report as a PDF. No sign-in required.

    The anonymised record is what gets rendered, not the record on disk, so the
    owner's address cannot reach the document even if the template later grows a
    byline. ``pdf_filename`` builds from the ticker and date alone.
    """
    from ystocker.agents import showcase_job
    from ystocker.report_pdf import build_report_pdf, pdf_filename

    job = showcase_job(job_id)
    # 404, matching the JSON route: a distinguishable error would confirm that a
    # given job id exists.
    if not job:
        return jsonify({"error": "No such report"}), 404

    tz = _reader_tz()
    cache_key = f"{job_id}|{tz}"
    with _showcase_pdf_lock:
        pdf = _showcase_pdfs.get(cache_key)
    if pdf is None:
        pdf = build_report_pdf(job, tz=tz)
        if not pdf:
            return jsonify({"error": "Could not render a PDF for this report"}), 500
        with _showcase_pdf_lock:
            # Dropped whole when full: the sample re-rolls hourly, so the old
            # entries are the ones no longer listed anyway.
            if len(_showcase_pdfs) >= _SHOWCASE_PDF_MAX:
                _showcase_pdfs.clear()
            _showcase_pdfs[cache_key] = pdf
        log.info("agents: showcase rendered PDF for %s (%d bytes)", job_id, len(pdf))

    return Response(pdf, content_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{pdf_filename(job)}"',
        "Content-Length": str(len(pdf)),
        # public, unlike the owner's "private" copy: this report is deliberately
        # published, so a CDN or browser cache holding it is fine and spares the
        # box a re-render.
        "Cache-Control": "public, max-age=3600",
    })


# ---------------------------------------------------------------------------
# Syndication — iCalendar + RSS
# ---------------------------------------------------------------------------

# Alias first, canonical second: url_for() builds from the last rule registered
# for an endpoint, so this order is what makes the UI link to /calendar.ics
# rather than the alias.
@bp.route("/fomc.ics")
@bp.route("/calendar.ics")
def fomc_calendar():
    """iCalendar feed of FOMC decision dates, for calendar subscriptions.

    Cached for an hour: the FOMC schedule changes a few times a year, and
    calendar clients poll far more often than that. text/calendar is what makes
    Apple Calendar and Google offer to subscribe rather than download.
    """
    from ystocker.feeds import build_fomc_ics

    try:
        body = build_fomc_ics(include_past=request.args.get("past", "1") != "0")
    except Exception as exc:
        log.error("calendar.ics: build failed: %s", exc, exc_info=True)
        return Response("calendar unavailable", status=503, mimetype="text/plain")

    log.info("GET calendar.ics (%d bytes)", len(body))
    return Response(body, content_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": 'inline; filename="ystocker-fomc.ics"',
        "Cache-Control": "public, max-age=3600",
    })


@bp.route("/feed.xml")
@bp.route("/rss.xml")
def rss_feed():
    """RSS 2.0 feed of the daily market commentary."""
    from ystocker.feeds import build_rss

    lang = request.args.get("lang", "en")
    try:
        body = build_rss(lang=lang)
    except Exception as exc:
        log.error("rss.xml: build failed: %s", exc, exc_info=True)
        return Response("feed unavailable", status=503, mimetype="text/plain")

    log.info("GET rss.xml lang=%s (%d bytes)", lang, len(body))
    return Response(body, content_type="application/rss+xml; charset=utf-8", headers={
        "Cache-Control": "public, max-age=1800",
    })


# ---------------------------------------------------------------------------
# Housing — Zillow Research + Redfin Data Center
# ---------------------------------------------------------------------------

@bp.route("/housing")
def housing():
    """Page showing US housing-market data from Zillow and Redfin."""
    log.info("GET /housing")
    from ystocker.housing import get_cache_ts, is_cache_fresh, is_warming as hz_warming_fn
    return render_template(
        "housing.html",
        peer_groups=list(PEER_GROUPS.keys()),
        cache_last_updated=get_cache_ts(),
        cache_fresh=is_cache_fresh(),
        warming=hz_warming_fn(),
    )


@bp.route("/housing/refresh")
def housing_refresh():
    """Kick off a background re-download of the Zillow + Redfin datasets."""
    from ystocker.housing import refresh_cache
    threading.Thread(target=refresh_cache, daemon=True, name="housing-manual-refresh").start()
    return redirect(url_for("main.housing"))


@bp.route("/api/housing")
def api_housing():
    """JSON API — national + metro housing series from Zillow and Redfin.

    Mirrors /api/fed: serve a fresh cache immediately, otherwise 202 so the
    page polls. The underlying download is ~10 MB across 8 files, so it must
    never run inline in a request.
    """
    try:
        from ystocker.housing import (
            get_housing_data,
            is_cache_fresh,
            is_warming as hz_warming_fn,
            refresh_cache,
        )

        if is_cache_fresh():
            data = get_housing_data()
            if not data or not (data.get("zillow") or data.get("redfin")):
                log.warning("API housing: fresh check passed but payload is empty")
                return jsonify({"status": "stale", "error": "Cache data inconsistent",
                                "warming": True}), 202
            resp = _with_freshness(data, series_keys=("zillow", "redfin", "fred", "realtor"))
            resp["status"] = "ok"
            log.info("API housing: served from cache (%d Zillow, %d Redfin series, age %s)",
                     len(resp.get("zillow", {})), len(resp.get("redfin", {})),
                     resp["meta"]["age_label"])
            return jsonify(resp)

        if hz_warming_fn():
            log.info("API housing: warming in progress, returning 202")
            return jsonify({"status": "warming", "warming": True}), 202

        log.info("API housing: no cache, starting background fetch")
        threading.Thread(target=refresh_cache, daemon=True, name="housing-auto-warm").start()
        return jsonify({"status": "initializing", "warming": True}), 202
    except Exception as exc:
        log.error("API housing: error: %s", exc, exc_info=True)
        return jsonify({"status": "error", "error": str(exc), "warming": True}), 500


# ---------------------------------------------------------------------------
# Index P/E multiples — SPY / QQQ forward P/E + S&P 500 trailing history
# ---------------------------------------------------------------------------

@bp.route("/multiples")
def multiples():
    """Page showing index P/E multiples for SPY, QQQ and the S&P 500."""
    log.info("GET /multiples")
    from ystocker.valuation import get_cache_ts, is_cache_fresh, is_warming as vp_warming_fn
    return render_template(
        "multiples.html",
        peer_groups=list(PEER_GROUPS.keys()),
        cache_last_updated=get_cache_ts(),
        cache_fresh=is_cache_fresh(),
        warming=vp_warming_fn(),
    )


@bp.route("/multiples/refresh")
def multiples_refresh():
    """Kick off a background rebuild of the P/E multiples."""
    from ystocker.valuation import refresh_cache
    threading.Thread(target=refresh_cache, daemon=True, name="valuation-manual-refresh").start()
    return redirect(url_for("main.multiples"))


def _adv_dec_cached() -> dict | None:
    """Per-index advancers/decliners, but only if breadth is already cached.

    Same contract as :func:`_breadth_pct50_cached`: ``peek()`` never rebuilds, so
    a caller cannot accidentally inherit breadth's ~25s 500-ticker download. An
    absent return means "not warm yet", which callers render as a hidden row
    rather than as zeros.
    """
    try:
        payload = breadth.peek()
        if not payload:
            return None
        adv = payload.get("adv_dec")
        return adv if adv else None
    except Exception as exc:
        log.debug("adv_dec unavailable: %s", exc)
        return None


@bp.route("/api/multiples")
def api_multiples():
    """JSON API — index P/E multiples. 202 while the cache is being rebuilt."""
    try:
        from ystocker.valuation import (
            get_valuation_data, is_cache_fresh,
            is_warming as vp_warming_fn, refresh_cache,
        )
        if is_cache_fresh():
            data = get_valuation_data()
            if not data or not (data.get("multpl") or data.get("forward")):
                log.warning("API multiples: fresh check passed but payload is empty")
                return jsonify({"status": "stale", "error": "Cache data inconsistent",
                                "warming": True}), 202
            resp = _with_freshness(data, series_keys=("multpl",))
            resp["status"] = "ok"
            # Advance/decline rides along so the multiples page can show it on the
            # SPY-vs-QQQ card without a second request for the 84 KB breadth
            # payload. peek() never rebuilds, so this cannot inherit breadth's
            # ~25s 500-ticker download; if breadth is not warm yet the key is
            # simply absent and the card hides that row.
            adv = _adv_dec_cached()
            if adv:
                resp["adv_dec"] = adv
            log.info("API multiples: served from cache (%d trailing series, %d forward)",
                     len(resp.get("multpl", {})), len(resp.get("forward", {})))
            return jsonify(resp)

        if vp_warming_fn():
            log.info("API multiples: warming in progress, returning 202")
            return jsonify({"status": "warming", "warming": True}), 202

        log.info("API multiples: no cache, starting background rebuild")
        threading.Thread(target=refresh_cache, daemon=True, name="valuation-auto-warm").start()
        return jsonify({"status": "initializing", "warming": True}), 202
    except Exception as exc:
        log.error("API multiples: error: %s", exc, exc_info=True)
        return jsonify({"status": "error", "error": str(exc), "warming": True}), 500


def _build_multiples_prompt(label: str, unit: str, dates: list, values: list, lang: str) -> str:
    """Prompt for one P/E chart, given its visible range."""
    pairs = [(d, v) for d, v in zip(dates, values) if v is not None]
    if not pairs:
        return ""

    def show(v: float) -> str:
        if unit == "x":
            return f"{v:.1f}x"
        if unit == "usd":
            return f"${v:.2f}"
        return f"{v:,.2f}"

    first_d, first_v = pairs[0]
    last_d, last_v = pairs[-1]
    hi = max(pairs, key=lambda p: p[1])
    lo = min(pairs, key=lambda p: p[1])
    ranked = sorted(v for _, v in pairs)
    pctl = sum(1 for v in ranked if v <= last_v) / len(ranked) * 100
    step = max(1, len(pairs) // 20)
    sample = "\n".join(f"  {d}: {show(v)}" for d, v in pairs[::step][-20:])
    lang_instr = "Respond in Simplified Chinese (中文)." if lang == "zh" else ""

    return f"""You are an equity strategist. Explain this valuation series to an investor.

Series: {label}
Window: {first_d} ({show(first_v)}) to {last_d} ({show(last_v)})
High in window: {show(hi[1])} in {hi[0]}
Low in window:  {show(lo[1])} in {lo[0]}
Current reading sits at the {pctl:.0f}th percentile of this window.

Sampled observations:
{sample}

In 2-3 concise paragraphs, cover: what this multiple measures and what drives it;
where the current level sits against its own history and whether that is stretched
or cheap; and what would have to happen to earnings or prices for it to normalise.
If the series is a trailing multiple, note that forward multiples are normally
lower because forward earnings are higher, and that the two are not directly
comparable. Be specific about the numbers given. Do not use bullet points or
markdown headers — just flowing prose paragraphs. {lang_instr}"""


@bp.route("/api/multiples/explain", methods=["POST"])
def api_multiples_explain():
    """Stream an AI explanation of one P/E chart's visible range."""
    import os

    body   = request.get_json(force=True, silent=True) or {}
    label  = body.get("label", body.get("chart", ""))
    unit   = body.get("unit", "")
    lang   = body.get("lang", "en")
    dates  = body.get("dates") or []
    values = body.get("values") or []
    log.info("API multiples/explain: label=%s lang=%s points=%d", label, lang, len(dates))

    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503
    if not dates or not values:
        return jsonify({"error": "No data provided"}), 400

    prompt = _build_multiples_prompt(label, unit, dates, values, lang)
    if not prompt:
        return jsonify({"error": "No valid data points"}), 400
    return _stream_gemini(prompt, "Multiples")


def _build_housing_prompt(snapshot: dict, lang: str) -> str:
    """Build a Gemini prompt that explains the US housing market.

    The snapshot is the ``/api/housing`` payload trimmed by the client:
    headline metrics with their source, the affordability figures, and the
    largest metros.
    """
    head    = snapshot.get("headline") or {}
    afford  = snapshot.get("affordability") or {}
    metros  = snapshot.get("metros") or []

    def _fmt(entry: dict) -> str:
        val, unit = entry.get("value"), entry.get("unit")
        try:
            if unit == "usd":
                shown = f"${float(val):,.0f}"
            elif unit == "pct_dec":
                shown = f"{float(val) * 100:.1f}%"
            elif unit == "pct":
                shown = f"{float(val):.2f}%"
            elif unit == "count":
                shown = f"{float(val):,.0f}"
            else:
                shown = f"{float(val):,.1f}"
        except (TypeError, ValueError):
            shown = "n/a"
        yoy = entry.get("yoy")
        if yoy is None:
            return f"{shown} ({entry.get('source', '?')})"
        suffix = "pp" if entry.get("yoy_unit") == "pp" else "%"
        return f"{shown}, {yoy:+g}{suffix} YoY ({entry.get('source', '?')})"

    head_lines = "\n".join(f"  {k}: {_fmt(v)}" for k, v in head.items()) or "  (unavailable)"

    metro_lines = "\n".join(
        f"  {m.get('metro')}: ${m.get('zhvi'):,.0f} "
        f"({m.get('zhvi_yoy'):+g}% YoY)" if isinstance(m.get("zhvi"), (int, float))
        and isinstance(m.get("zhvi_yoy"), (int, float))
        else f"  {m.get('metro')}: n/a"
        for m in metros
    ) or "  (unavailable)"

    afford_line = "unavailable"
    if afford:
        try:
            afford_line = (
                f"${float(afford['latest_payment']):,.0f}/mo principal+interest on the typical "
                f"${float(afford['home_value']):,.0f} home at {float(afford['latest_rate']):.2f}% "
                f"(20% down, 30-year fixed); peak was ${float(afford['peak_payment']):,.0f}/mo"
            )
        except (KeyError, TypeError, ValueError):
            pass

    extra: list[str] = []
    spread = snapshot.get("mortgage_spread") or {}
    if spread.get("current") is not None:
        try:
            extra.append(
                f"Mortgage spread over the 10-year Treasury: {float(spread['current']):.2f}pp "
                f"(historical range {float(spread['min']):.2f}-{float(spread['max']):.2f}pp)"
            )
        except (TypeError, ValueError):
            pass
    ptr = snapshot.get("price_to_rent") or {}
    if ptr.get("current") is not None:
        try:
            extra.append(
                f"Price-to-rent ratio: {float(ptr['current']):.0f} vs a peak of "
                f"{float(ptr['peak']):.0f} in {ptr.get('peak_date')} (index, 100 = start of history)"
            )
        except (TypeError, ValueError):
            pass
    rg = snapshot.get("rent_growth") or {}
    if rg.get("market") is not None:
        try:
            extra.append(
                f"Rent growth: market asking rents {float(rg['market']):+.1f}% YoY vs "
                f"CPI rent {float(rg['cpi']):+.1f}% YoY"
            )
        except (TypeError, ValueError):
            pass
    extra_block = "\n".join(f"  {line}" for line in extra) or "  (unavailable)"

    lang_instr = "Respond in Simplified Chinese (中文)." if lang == "zh" else ""

    return f"""You are a housing-market analyst. Explain the current state of the US housing market to an investor.

Data as of: {snapshot.get('as_of')}

National headline metrics (source noted per line):
{head_lines}

Cost to buy: {afford_line}

Valuation and financing context:
{extra_block}

Largest metros — typical home value and year-over-year change:
{metro_lines}

In 4-5 concise paragraphs, cover:
(1) Where prices and rents stand nationally, and whether the market is heating or cooling.
(2) Affordability: how the monthly payment compares to its peak, and what is driving it — prices, rates, or both. If the mortgage spread over the 10-year Treasury is wide by historical standards, explain that mortgages can stay expensive even as the Fed eases, because they are priced off the 10-year plus that spread.
(3) Supply and leverage: what inventory, months of supply, days on market, construction and the price-cut share say about whether buyers or sellers hold the advantage.
(4) The rental market: how market asking rents compare with official CPI rent inflation, noting that CPI covers all tenants including mid-lease and therefore turns roughly a year after the market does.
(5) The metro divergence — name specific metros that are rising and falling, and what distinguishes them.

Important: Zillow and Redfin measure different transaction universes, so their price levels are not directly comparable; do not present one as contradicting the other. Be specific about the numbers and use the exact figures provided. Do not use bullet points or markdown headers — just flowing prose paragraphs. {lang_instr}"""


def _stream_gemini(prompt: str, log_label: str) -> Response:
    """Stream a Gemini completion as SSE.

    The Fed and FedWatch explain routes each carry their own copy of this
    plumbing; new endpoints use this helper instead of adding a third.
    """
    import os
    from google import genai

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

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
            log.error("%s explain error: %s", log_label, exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def _build_housing_series_prompt(
    chart: str, label: str, unit: str, dates: list, values: list, lang: str
) -> str:
    """Prompt for a single housing chart, given its visible time series.

    Sends the endpoints, the extremes, and a thinned sample rather than every
    observation: some of these series run to 800 monthly points, and pasting
    them all in wastes context without telling the model anything the shape
    does not already say.
    """
    pairs = [(d, v) for d, v in zip(dates, values) if v is not None]
    if not pairs:
        return ""

    def show(v: float) -> str:
        if unit == "usd":
            return f"${v:,.0f}"
        if unit in ("pct", "pp"):
            return f"{v:.2f}{'pp' if unit == 'pp' else '%'}"
        if unit == "thousands":
            return f"{v:,.0f}k units (annual rate)"
        if unit == "months":
            return f"{v:.1f} months"
        return f"{v:,.2f}"

    first_d, first_v = pairs[0]
    last_d, last_v = pairs[-1]
    hi = max(pairs, key=lambda p: p[1])
    lo = min(pairs, key=lambda p: p[1])

    step = max(1, len(pairs) // 24)
    sample = "\n".join(f"  {d}: {show(v)}" for d, v in pairs[::step][-24:])

    change = ""
    if unit in ("pct", "pp"):
        change = f"net change {last_v - first_v:+.2f}pp"
    elif first_v:
        change = f"net change {(last_v / first_v - 1) * 100:+.1f}%"

    lang_instr = "Respond in Simplified Chinese (中文)." if lang == "zh" else ""

    return f"""You are a housing-market analyst. Explain this US housing indicator to an investor.

Indicator: {label}
Period: {first_d} ({show(first_v)}) to {last_d} ({show(last_v)}); {change}
All-time high in this window: {show(hi[1])} in {hi[0]}
All-time low in this window: {show(lo[1])} in {lo[0]}

Sampled observations:
{sample}

In 2-3 concise paragraphs, cover: what this indicator measures and why it matters for housing; what the trend and the current level say relative to the historical range shown; and what it implies for buyers, sellers, or investors from here. Be specific about the numbers given. Do not use bullet points or markdown headers — just flowing prose paragraphs. {lang_instr}"""


@bp.route("/api/housing/explain", methods=["POST"])
def api_housing_explain():
    """Stream an AI explanation of the housing page or one of its charts.

    Two payload modes, mirroring /api/fed/explain:
      • ``snapshot``            — the whole page (headline + valuation + metros)
      • ``dates`` + ``values``  — a single chart's visible range
    """
    import os

    body   = request.get_json(force=True, silent=True) or {}
    chart  = body.get("chart", "")
    label  = body.get("label", chart)
    unit   = body.get("unit", "")
    lang   = body.get("lang", "en")
    dates  = body.get("dates") or []
    values = body.get("values") or []
    snapshot = body.get("snapshot")

    log.info("API housing/explain: chart=%s lang=%s points=%d snapshot=%s",
             chart, lang, len(dates), bool(snapshot))

    if not os.environ.get("GEMINI_API_KEY"):
        log.warning("API housing/explain: GEMINI_API_KEY not configured")
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    if snapshot:
        prompt = _build_housing_prompt(snapshot, lang)
    else:
        if not dates or not values:
            return jsonify({"error": "No data provided"}), 400
        prompt = _build_housing_series_prompt(chart, label, unit, dates, values, lang)
        if not prompt:
            return jsonify({"error": "No valid data points"}), 400

    return _stream_gemini(prompt, "Housing")


def _build_fedwatch_prompt(snapshot: dict, lang: str) -> str:
    """Build a Gemini prompt that explains the market-implied rate path.

    The snapshot is the ``/api/fedwatch`` payload trimmed by the client: the
    current target range plus, per meeting, the implied rate and the
    probability of each outcome.
    """
    current  = snapshot.get("current") or {}
    meetings = snapshot.get("meetings") or []

    lines = []
    for m in meetings:
        outcomes = " · ".join(
            f"{o.get('lower')}–{o.get('upper')}%: {o.get('prob')}%"
            for o in (m.get("outcomes") or [])
        )
        # The snapshot is client-supplied, so never format a field directly
        # into a numeric spec — a missing change_bp would raise TypeError and
        # turn a bad request into a 500.
        try:
            delta = f"{float(m.get('change_bp')):+.1f}bp"
        except (TypeError, ValueError):
            delta = "n/a"
        lines.append(
            f"  {m.get('label')} (decision {m.get('date')}): "
            f"implied rate {m.get('implied_rate')}% "
            f"({delta} vs the prior meeting) — {outcomes}"
        )
    meeting_block = "\n".join(lines) if lines else "  (no meetings available)"

    lang_instr = "Respond in Simplified Chinese (中文)." if lang == "zh" else ""

    return f"""You are a rates strategist. Explain what the Fed Funds futures market is currently pricing for U.S. monetary policy.

Current target range: {current.get('label')}% (effective fed funds rate {current.get('effr')}%)
Data as of: {snapshot.get('as_of')}

Market-implied probabilities by FOMC meeting (derived from CME 30-Day Fed Funds futures):
{meeting_block}

In 3-4 concise paragraphs, cover:
(1) What the market expects at the very next meeting — is a cut, hold, or hike the base case, and how confident is that pricing?
(2) The shape of the full expected path over the coming year — total basis points of easing or tightening priced in, and how quickly.
(3) How much conviction is really there: note that the distribution widens at longer horizons, so a spread-out set of outcomes means genuine uncertainty rather than a firm forecast.
(4) What this pricing implies for markets — bonds, the dollar, and risk assets — and what data could shift it.

Be specific about the numbers and use the exact figures provided. Note that these are market-implied probabilities, not a forecast. Do not use bullet points or markdown headers — just flowing prose paragraphs. {lang_instr}"""


def _build_balance_snapshot_prompt(snapshot: dict, lang: str) -> str:
    """Build a Gemini prompt that explains the Fed's full balance-sheet identity.

    The snapshot is a dict with ``asOf``, ``assets``, ``liabilities``, and
    ``weekly_changes`` sub-dicts (all values in $ billions). Used by the
    Balance Sheet Identity AI Explain button on the Fed page.
    """
    as_of  = snapshot.get("asOf", "latest")
    assets = snapshot.get("assets") or {}
    liab   = snapshot.get("liabilities") or {}
    chg    = snapshot.get("weekly_changes") or {}

    def _fmt(v):
        if v is None:
            return "n/a"
        if abs(v) >= 1000:
            return f"${v/1000:.2f}T"
        return f"${v:.1f}B"

    def _chg(v):
        if v is None:
            return "n/a"
        sign = "+" if v >= 0 else ""
        if abs(v) >= 1000:
            return f"{sign}${v/1000:.2f}T WoW"
        return f"{sign}${v:.1f}B WoW"

    asset_block = (
        f"  Total Assets:                  {_fmt(assets.get('total'))}    ({_chg(chg.get('total'))})\n"
        f"  • Treasury Securities:         {_fmt(assets.get('treasuries'))}    ({_chg(chg.get('treasuries'))})\n"
        f"  • MBS Holdings:                {_fmt(assets.get('mbs'))}    ({_chg(chg.get('mbs'))})\n"
        f"  • Fed Loans (BTFP, etc.):      {_fmt(assets.get('loans'))}    ({_chg(chg.get('loans'))})\n"
        f"  • Other (repos, swaps, gold…): {_fmt(assets.get('other'))}    ({_chg(chg.get('other_assets'))})\n"
        f"      ↳ Central Bank Liq. Swaps: {_fmt(assets.get('swaps'))}    ({_chg(chg.get('swaps'))})\n"
        f"      ↳ Gold Certificate Acct.:  {_fmt(assets.get('gold'))}    ({_chg(chg.get('gold'))})\n"
        f"      ↳ SDR Certificate Acct.:   {_fmt(assets.get('sdr'))}    ({_chg(chg.get('sdr'))})"
    )
    liab_block = (
        f"  Total Liabilities + Capital:   {_fmt(assets.get('total'))}    (must equal Total Assets)\n"
        f"  • Reserve Balances:            {_fmt(liab.get('reserves'))}    ({_chg(chg.get('reserves'))})\n"
        f"  • Currency in Circulation:     {_fmt(liab.get('currency'))}    ({_chg(chg.get('currency'))})\n"
        f"  • Treasury General Account:    {_fmt(liab.get('tga'))}    ({_chg(chg.get('tga'))})\n"
        f"  • Overnight Reverse Repos:     {_fmt(liab.get('rrp'))}    ({_chg(chg.get('rrp'))})\n"
        f"  • Other liab. + Capital:       {_fmt(liab.get('other'))}"
    )

    lang_instr = "Respond in Simplified Chinese (中文)." if lang == "zh" else ""

    return f"""You are a macroeconomic analyst explaining the Federal Reserve's balance sheet to a financial market participant.

Snapshot as of {as_of} — the full balance sheet identity (Assets = Liabilities + Capital):

ASSETS
{asset_block}

LIABILITIES + CAPITAL
{liab_block}

In 3-5 concise paragraphs, cover:
(1) What the overall size and composition tell us — is the Fed currently in QT or QE? How does the asset mix (Treasuries vs MBS vs other) reflect monetary policy? Notice that Treasuries and MBS together usually dominate assets.
(2) The notable week-over-week moves — which line items are moving the most, and what is driving them? Pay attention to directionally interesting combinations: e.g. shrinking Total Assets while a single line is rising, or unwinding emergency loans.
(3) The liability side — what does the split between Reserve Balances, Currency in Circulation, TGA, and ON RRP say about banking-system liquidity? Are reserves still abundant? Is the Treasury draining or rebuilding TGA? Is RRP nearly empty?
(4) Implications for monetary policy and markets — risk assets, money-market rates, dollar liquidity, and overall financial conditions.

Be specific about the numbers and use the exact figures provided. Do not use bullet points or markdown headers in your response — just flowing prose paragraphs. {lang_instr}"""


@bp.route("/api/fed")
def api_fed():
    """JSON API — return Fed H.4.1 balance-sheet time-series data.

    If no cache exists yet, kick off a background fetch and return 202 so the
    page shows a loading state rather than blocking the request thread.
    """
    try:
        from ystocker.fed import get_fed_data, is_cache_fresh, is_warming as fed_warming_fn, refresh_cache

        # Fresh cache available — return immediately.
        if is_cache_fresh():
            data = get_fed_data()
            if not data or not data.get("series"):
                log.warning("API fed: cache fresh check passed but data is empty or missing 'series'")
                # Don't return None/empty, return a proper status
                return jsonify({"status": "stale", "error": "Cache data inconsistent", "warming": True}), 202

            resp = _with_freshness(data, series_keys=("series",))
            resp["status"] = "ok"
            stale = resp["meta"].get("stale_series")
            log.info("API fed: served from cache (%d series, age %s%s)",
                     len(resp.get("series", {})), resp["meta"]["age_label"],
                     f", {len(stale)} stale: {', '.join(stale[:5])}" if stale else "")
            return jsonify(resp)

        # A background fetch is already running — tell the client to retry.
        if fed_warming_fn():
            log.info("API fed: warming in progress, returning 202")
            return jsonify({"status": "warming", "warming": True}), 202

        # No cache and no fetch in progress — start one in the background.
        log.info("API fed: no cache, starting background fetch")
        threading.Thread(target=refresh_cache, daemon=True, name="fed-auto-warm").start()
        return jsonify({"status": "initializing", "warming": True}), 202
    except Exception as exc:
        log.error("API fed: error: %s", exc, exc_info=True)
        return jsonify({"status": "error", "error": str(exc), "warming": True}), 500


@bp.route("/api/fed/explain", methods=["POST"])
def api_fed_explain():
    """Stream an AI explanation of a Fed chart's recent data via SSE.

    Two payload modes:
      • Time-series mode: ``dates`` + ``values`` arrays (one chart line)
      • Snapshot mode:    ``snapshot`` dict with full balance-sheet
                          identity (assets / liabilities + WoW deltas)
    """
    import os
    from google import genai

    body = request.get_json(force=True, silent=True) or {}
    chart    = body.get("chart", "")
    dates    = body.get("dates", [])
    values   = body.get("values", [])
    label    = body.get("label", chart)
    lang     = body.get("lang", "en")
    snapshot = body.get("snapshot")
    log.info(
        "API fed/explain: chart=%s lang=%s points=%d snapshot=%s",
        chart, lang, len(dates), bool(snapshot),
    )

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("API fed/explain: GEMINI_API_KEY not configured")
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    if snapshot:
        # Snapshot mode is shared by two very different cards, so the chart id
        # picks the prompt builder. Without this branch the FedWatch payload
        # would be fed to the balance-sheet prompt and explained as assets.
        if chart == "fedwatch":
            prompt = _build_fedwatch_prompt(snapshot, lang)
        elif chart == "housing":
            prompt = _build_housing_prompt(snapshot, lang)
        else:
            prompt = _build_balance_snapshot_prompt(snapshot, lang)
    else:
        if not dates or not values:
            return jsonify({"error": "No data provided"}), 400

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
            "liab":      "Federal Reserve Liabilities — ON RRP and Treasury General Account (weekly, billions USD)",
            "currLoan":  "Currency in Circulation and Federal Reserve Emergency Loans incl. BTFP (weekly, billions USD)",
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


@bp.route("/api/13f/aum-explain", methods=["POST"])
def api_13f_aum_explain():
    """Stream an AI explanation of a fund's quarterly AUM trend via SSE."""
    import os
    from google import genai
    from ystocker.sec13f import get_all_holdings

    body = request.get_json(force=True, silent=True) or {}
    fund = (body.get("fund") or "").strip()
    lang = body.get("lang", "en")
    log.info("API 13f/aum-explain: fund=%s lang=%s", fund, lang)

    if not fund:
        return jsonify({"error": "fund name required"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    # Get the fund's quarterly AUM history
    try:
        all_data = get_all_holdings()
        fund_data = all_data.get(fund, {})
        quarters = fund_data.get("quarters", [])
    except Exception as exc:
        log.exception("13f aum-explain: data fetch failed")
        return jsonify({"error": str(exc)}), 500

    if not quarters or len(quarters) < 2:
        return jsonify({"error": "Not enough quarterly history available"}), 400

    # Reverse to chronological order (oldest first)
    chronological = list(reversed(quarters))
    # Format quarterly data
    aum_lines = []
    prev_aum = None
    for q in chronological:
        period = q.get("period", "?")
        aum_m  = float(q.get("total_value_millions") or 0)
        aum_b  = aum_m / 1000
        change_str = ""
        if prev_aum and prev_aum > 0:
            pct = (aum_m - prev_aum) / prev_aum * 100
            change_str = f" ({pct:+.1f}% QoQ)"
        aum_lines.append(f"  {period}: ${aum_b:,.1f}B{change_str}")
        prev_aum = aum_m

    first_aum_b = float(chronological[0].get("total_value_millions", 0)) / 1000
    last_aum_b  = float(chronological[-1].get("total_value_millions", 0)) / 1000
    overall_pct = ((last_aum_b - first_aum_b) / first_aum_b * 100) if first_aum_b else 0

    summary = (
        f"From {chronological[0].get('period')} to {chronological[-1].get('period')}: "
        f"${first_aum_b:,.1f}B → ${last_aum_b:,.1f}B "
        f"({overall_pct:+.1f}% total)"
    )

    aum_block = "\n".join(aum_lines)
    lang_instr = "Respond in Simplified Chinese (中文)." if lang == "zh" else ""

    prompt = f"""You are a hedge-fund analyst explaining 13F AUM (Assets Under Management) trends.

Fund: {fund}
{summary}

Quarterly AUM history (oldest first):
{aum_block}

Explain in 2-3 concise paragraphs:
1. What the trend shows — is AUM growing, shrinking, or volatile? Look at both the overall direction and quarter-by-quarter swings.
2. Why AUM might fluctuate — possible drivers (market gains/losses, investor inflows/outflows, position concentration, rebalancing, redemptions, fund splits/mergers, confidential treatment exemptions hiding holdings).
3. What investors should know — what the trend says about the fund's strategy or scale.

Be specific about the numbers. Do not use headers or bullet points. {lang_instr}"""

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
            log.error("13f aum-explain error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

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

    # Build consensus positions (tickers held by most funds)
    from collections import defaultdict
    ticker_funds: dict[str, list] = defaultdict(list)
    ticker_value: dict[str, float] = defaultdict(float)
    for fund_name, fd in holdings.items():
        if "error" in fd:
            continue
        for h in fd.get("holdings", []):
            t = h.get("ticker")
            if not t:
                continue
            ticker_funds[t].append(fund_name)
            ticker_value[t] += h.get("value_millions", 0) or 0
    consensus_positions = sorted(
        [
            {
                "ticker": t,
                "fund_count": len(fnames),
                "total_value_m": round(ticker_value[t]),
                "fund_names": fnames,
            }
            for t, fnames in ticker_funds.items()
            if len(fnames) >= 2
        ],
        key=lambda x: -x["fund_count"],
    )[:25]

    return render_template(
        "thirteenf.html",
        peer_groups=list(PEER_GROUPS.keys()),
        funds=FUNDS,
        holdings=holdings,
        cache_last_updated=cache_ts,
        cache_fresh=is_cache_fresh(),
        warming=warming,
        consensus_positions=consensus_positions,
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
    log.info("API 13f: fund=%s", fund_slug)
    from ystocker.sec13f import get_all_holdings, FUNDS
    holdings = get_all_holdings()
    name = next(
        (n for n in FUNDS if n.lower().replace(" ", "-") == fund_slug.lower()),
        None
    )
    if not name:
        log.warning("API 13f: fund not found: %s", fund_slug)
        return jsonify({"error": "Fund not found"}), 404
    return jsonify(holdings.get(name, {}))


@bp.route("/api/13f/ticker/<ticker>")
def api_thirteenf_ticker(ticker: str):
    """JSON API — multi-quarter institutional holdings for a single ticker."""
    ticker = ticker.strip().upper()
    log.info("API 13f/ticker: %s", ticker)
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
    log.info("API history/explain: ticker=%s chart=%s period=%s lang=%s", ticker, chart, period, lang)

    if not dates or not values:
        return jsonify({"error": "No data provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    pairs = [(d, v) for d, v in zip(dates, values) if v is not None]
    if not pairs:
        return jsonify({"error": "No valid data points"}), 400

    # ── Disk cache ────────────────────────────────────────────────────────────
    from . import research

    _EXPLAIN_CACHE_DIR = Path(__file__).parent.parent / "cache" / "explain"
    _EXPLAIN_CACHE_TTL = 8 * 60 * 60   # 8 hours, matches main data cache

    safe_ticker = ticker.replace("/", "-")
    # Template version is part of the key so prompt changes invalidate old text.
    cache_file  = (_EXPLAIN_CACHE_DIR /
                   f"{safe_ticker}_{chart}_{period}_{lang}_{research.TEMPLATE_VERSION}.json")

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
    # Anchored to the shared stock-research template so each per-chart note
    # lands in the same framework as the full deep-research report.
    prompt = research.build_chart_prompt(
        ticker=ticker,
        chart=chart,
        period=period,
        lang=lang,
        pairs=pairs,
        context=body.get("context") or {},
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


@bp.route("/api/history/<ticker>/research", methods=["POST"])
def api_history_research(ticker: str):
    """Stream a full deep-research report for a ticker via SSE.

    The frontend assembles a data bundle (fundamentals, technicals, peers, news,
    ownership, and the user's portfolio inputs with computed ETF look-through
    exposure) and POSTs it here; this endpoint renders the shared 17-section
    research template around it and streams Gemini's answer back.

    Google Search grounding is enabled so the model can pull the latest earnings
    result, guidance, announcements and analyst estimates — the in-app figures
    still take precedence, per the system instruction in ``research.py``.

    Request body:
        {"bundle": {...}, "lang": "en"|"zh", "refresh": bool}
    Response:
        SSE stream of ``data: {"text": "..."}`` then ``data: [DONE]``.
    """
    import os
    from google import genai
    from google.genai import types as genai_types

    from . import research

    ticker = ticker.strip().upper()
    body   = request.get_json(force=True, silent=True) or {}
    bundle = body.get("bundle") or {}
    lang   = "zh" if body.get("lang") == "zh" else "en"
    refresh = bool(body.get("refresh"))

    if not isinstance(bundle, dict) or not bundle.get("identity"):
        return jsonify({"error": "No research bundle provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("API history/research: GEMINI_API_KEY not configured")
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    fingerprint = research.bundle_fingerprint(bundle)
    log.info("API history/research: ticker=%s lang=%s fp=%s refresh=%s",
             ticker, lang, fingerprint, refresh)

    # ── Disk cache ────────────────────────────────────────────────────────────
    # Keyed on the portfolio inputs + reporting period, not on live price, so a
    # tick-by-tick price change does not force a 40 s regeneration.
    _RESEARCH_CACHE_DIR = Path(__file__).parent.parent / "cache" / "research"
    _RESEARCH_CACHE_TTL = 8 * 60 * 60   # 8 hours, matches the main data cache

    safe_ticker = ticker.replace("/", "-")
    cache_file  = _RESEARCH_CACHE_DIR / f"{safe_ticker}_{lang}_{fingerprint}.json"

    if not refresh:
        try:
            if cache_file.exists():
                payload = json.loads(cache_file.read_text())
                if time.time() - payload.get("ts", 0) < _RESEARCH_CACHE_TTL:
                    cached_text = payload.get("text", "")
                    if cached_text:
                        log.debug("Research cache hit: %s lang=%s", ticker, lang)

                        def stream_cached():
                            yield f"data: {json.dumps({'cached': True})}\n\n"
                            yield f"data: {json.dumps({'text': cached_text})}\n\n"
                            yield "data: [DONE]\n\n"

                        return Response(stream_cached(), mimetype="text/event-stream", headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                        })
        except (OSError, ValueError):
            log.debug("Research cache read failed for %s — will re-generate", ticker)

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_instruction, prompt = research.build_research_prompt(
        ticker=ticker, bundle=bundle, lang=lang, today=today)

    # The analyst persona/rules go in the user turn rather than the
    # system_instruction field: with Google Search grounding enabled, a Chinese
    # system_instruction makes the upstream stream complete with zero chunks
    # (reproduced 0/3 with the field vs 3/3 when merged). English is unaffected,
    # but one code path is better than two.
    contents = f"{system_instruction}\n\n---\n\n{prompt}"

    # A full report runs ~10k output + ~5k thinking tokens; 24k leaves headroom
    # without letting a runaway generation exceed the Gunicorn request window.
    _MAX_OUTPUT_TOKENS = 24_576
    # Stay under the production Gunicorn --timeout of 120 s.
    client = genai.Client(api_key=api_key,
                          http_options=genai_types.HttpOptions(timeout=100_000))

    def _open_stream(use_search: bool):
        """Start one generation attempt, optionally with Google Search grounding."""
        kwargs: dict = {
            "temperature": 0.3,                    # research, not creative writing
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
        }
        if use_search:
            kwargs["tools"] = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
        return client.models.generate_content_stream(
            model="gemini-2.5-flash", contents=contents,
            config=genai_types.GenerateContentConfig(**kwargs))

    def generate():
        accumulated: list[str] = []
        sources: list[str] = []
        truncated = False
        error: str | None = None
        degraded = False

        # The grounded stream intermittently completes with zero chunks upstream.
        # Retry once, then fall back to a search-free run (markedly more reliable
        # and still complete from the in-app data). Retrying is only safe while
        # nothing has been emitted yet, which is exactly the case we retry on.
        for attempt, use_search in enumerate((True, True, False), start=1):
            error = None
            truncated = False
            try:
                for chunk in _open_stream(use_search):
                    text = chunk.text
                    if text:
                        accumulated.append(text)
                        yield f"data: {json.dumps({'text': text})}\n\n"
                    for cand in (chunk.candidates or []):
                        # Collect grounding sources so the UI can show what was read
                        meta = getattr(cand, "grounding_metadata", None)
                        for gc in (getattr(meta, "grounding_chunks", None) or []):
                            web = getattr(gc, "web", None)
                            title = getattr(web, "title", None) if web else None
                            if title and title not in sources:
                                sources.append(title)
                        if str(getattr(cand, "finish_reason", "") or "").endswith("MAX_TOKENS"):
                            truncated = True
            except Exception as exc:
                error = str(exc)
                log.error("History research error for %s (attempt %d, search=%s): %s",
                          ticker, attempt, use_search, exc)

            if accumulated:
                degraded = not use_search
                break
            log.warning("History research: empty stream for %s (attempt %d, search=%s)",
                        ticker, attempt, use_search)

        full_text = "".join(accumulated)

        if not full_text:
            msg = error or (
                "AI 返回为空，请点「重新生成」重试。" if lang == "zh"
                else "The AI returned an empty response. Press Regenerate to retry.")
            yield f"data: {json.dumps({'error': msg})}\n\n"
            yield "data: [DONE]\n\n"
            return

        if degraded:
            yield f"data: {json.dumps({'degraded': True})}\n\n"
        if truncated:
            yield f"data: {json.dumps({'truncated': True})}\n\n"
        if sources:
            yield f"data: {json.dumps({'sources': sources[:12]})}\n\n"
        if error:
            yield f"data: {json.dumps({'error': error})}\n\n"

        # Cache only a clean, complete report
        if not error and not truncated:
            try:
                _RESEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = cache_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({
                    "ts": time.time(), "text": full_text, "sources": sources[:12],
                }))
                tmp.replace(cache_file)
                log.debug("Research cached: %s lang=%s fp=%s", ticker, lang, fingerprint)
            except OSError:
                log.debug("Failed to write research cache for %s", ticker)
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
        # overwrite_by_pkeys: BatchWriteItem rejects the entire batch if one
        # request repeats a key, so collapse duplicates (last wins) first.
        with table.batch_writer(overwrite_by_pkeys=["link"]) as batch:
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
    log.info("API news/translate: %d articles, lang=%s", len(articles), lang)

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
    log.info("API videos: ticker=%s", ticker)

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
    log.info("API videos/channel: %s", channel_id)

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
    log.info("API videos/all")

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

    from ystocker.forecast import run_forecast_isolated
    log.info("Running forecast for %s (isolated process)", ticker)
    result = run_forecast_isolated(ticker)

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


def _start_spx_history_warmup_thread(app) -> None:
    """Keep the long ^GSPC monthly series warm, off the request path.

    This is the fetch that made the /fed macro charts look broken. It pulls
    period="max" daily bars (~24.7k rows) and resamples, which on a cold cache
    took long enough to exceed gunicorn's 120s timeout and return 504. Every
    long-horizon chart on /fed awaits it in one Promise.all, so a single timeout
    left Consumer Sentiment, Real GDP vs S&P 500, Real GDP vs Industrial
    Production and Business Cycle Indicators all hidden -- indistinguishable, from
    the outside, from data that had stopped updating.

    Runs in the master under --preload, like the other warmers here, so the
    workers inherit a populated cache instead of racing to build it.
    """
    def _loop():
        time.sleep(20)   # after the markets warmer, which visitors hit sooner
        while True:
            try:
                with _SPX_HISTORY_LOCK:
                    entry = _SPX_HISTORY_CACHE.get("data") or _spx_history_load_disk()
                    if entry:
                        _SPX_HISTORY_CACHE.setdefault("data", entry)
                    ts = entry.get("ts", 0) if entry else 0
                age = time.time() - ts
                if age >= _SPX_HISTORY_TTL - 3600:
                    log.info("spx-history warm-up: %.1fh old — refreshing", age / 3600)
                    with app.test_request_context():
                        api_spx_history()   # fills the cache and the disk copy
                else:
                    log.info("spx-history warm-up: fresh (%.1fh old)", age / 3600)
            except Exception as exc:  # noqa: BLE001
                log.warning("spx-history warm-up: failed: %s", exc)
            # Re-check hourly. The series gains one point a month, so this is
            # about surviving a restart, not about latency.
            time.sleep(3600)

    t = threading.Thread(target=_loop, daemon=True, name="spx-history-warmup")
    t.start()


def _start_markets_warmup_thread(app) -> None:
    """Pre-warm the markets cache on startup and refresh every 5 minutes.

    Without this thread, the first visitor after a server restart (or after the
    5-minute TTL expires) waits 20-30 seconds for Yahoo Finance to return data
    for 15+ indices.  This background thread keeps the cache fresh so every
    request is a cache hit.
    """
    def _loop():
        time.sleep(8)  # Let Gunicorn fully initialize before the first fetch
        while True:
            try:
                with _MARKETS_CACHE_LOCK:
                    entry = _MARKETS_CACHE.get("data")
                    ts    = entry.get("ts", 0) if entry else 0
                if time.time() - ts >= _MARKETS_CACHE_TTL - 30:
                    log.info("Markets warm-up: cache stale (%.0fs) — refreshing", time.time() - ts)
                    with app.test_request_context():
                        api_markets()  # populates _MARKETS_CACHE as a side effect
                    log.info("Markets warm-up: cache refreshed")
            except Exception as exc:
                log.warning("Markets warm-up: refresh failed: %s", exc)
            time.sleep(60)  # re-check every minute

    t = threading.Thread(target=_loop, daemon=True, name="markets-warmup")
    t.start()
    log.info("Markets warm-up thread started (TTL=%ds)", _MARKETS_CACHE_TTL)


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

# Yahoo Finance symbols for major indices (US + international) and commodities
_INDEX_SYMBOLS = {
    "spx":    "^GSPC",     # S&P 500
    "ixic":   "^IXIC",     # Nasdaq Composite
    "dji":    "^DJI",      # Dow Jones
    "ftse":   "^FTSE",     # FTSE 100
    "n225":   "^N225",     # Nikkei 225
    "sse":    "000001.SS", # Shanghai Composite
    "csi500": "000905.SS", # CSI 500 (中证500)
    "twii":   "^TWII",     # Taiwan Weighted Index
    "kospi":  "^KS11",     # KOSPI
    # Commodities (continuous front-month futures)
    "gold":   "GC=F",      # COMEX Gold
    "silver": "SI=F",      # COMEX Silver
    "oil":    "CL=F",      # WTI Crude Oil
    "brent":  "BZ=F",      # Brent Crude Oil
    "natgas": "NG=F",      # Henry Hub Natural Gas
    "copper": "HG=F",      # COMEX Copper
    # Macro / dollar
    "dxy":    "DX-Y.NYB",  # US Dollar Index
}

# SPDR sector ETFs used for sector performance
_SECTOR_ETFS = {
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Consumer Disc.",
    "XLP": "Consumer Stap.", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Comm.", "XTL": "Telecom",
}


@bp.route("/markets")
def markets():
    """Market overview page — the application home."""
    return render_template("markets.html",
                           peer_groups=list(PEER_GROUPS.keys()))


@bp.route("/api/cta-positioning")
def api_cta_positioning():
    """Dated Goldman CTA snapshots and the latest reported SPX trigger levels."""
    from ystocker.cta import get_cta_positioning

    return jsonify(get_cta_positioning())


# ---------------------------------------------------------------------------
# Commodities  (/commodities)
# ---------------------------------------------------------------------------

# Symbol map: key → (Yahoo symbol, display name, unit, emoji, group)
_COMMODITY_SYMBOLS: dict[str, tuple[str, str, str, str, str]] = {
    # Precious metals
    "gold":       ("GC=F", "Gold",         "$/oz",    "🪙", "metals"),
    "silver":     ("SI=F", "Silver",       "$/oz",    "🥈", "metals"),
    "platinum":   ("PL=F", "Platinum",     "$/oz",    "💍", "metals"),
    "palladium":  ("PA=F", "Palladium",    "$/oz",    "💎", "metals"),
    # Industrial metals
    "copper":     ("HG=F", "Copper",       "$/lb",    "🟠", "industrial"),
    # Energy
    "oil_wti":    ("CL=F", "WTI Crude",    "$/bbl",   "🛢️", "energy"),
    "oil_brent":  ("BZ=F", "Brent Crude",  "$/bbl",   "🛢️", "energy"),
    "natgas":     ("NG=F", "Natural Gas",  "$/MMBtu", "🔥", "energy"),
    "gasoline":   ("RB=F", "Gasoline",     "$/gal",   "⛽", "energy"),
    "heating":    ("HO=F", "Heating Oil",  "$/gal",   "🔥", "energy"),
    # Agriculture
    "wheat":      ("ZW=F", "Wheat",        "¢/bu",    "🌾", "agri"),
    "corn":       ("ZC=F", "Corn",         "¢/bu",    "🌽", "agri"),
    "soybeans":   ("ZS=F", "Soybeans",     "¢/bu",    "🫘", "agri"),
    "coffee":     ("KC=F", "Coffee",       "¢/lb",    "☕", "agri"),
    "sugar":      ("SB=F", "Sugar",        "¢/lb",    "🍬", "agri"),
    # Macro (fetched for charts, not shown in commodity grid)
    "dxy":        ("DX-Y.NYB", "US Dollar Index", "index", "💵", "macro"),
}

# 15-minute in-memory cache for the commodities snapshot (yfinance is slow)
_COMMODITIES_CACHE: dict[str, dict] = {}
_COMMODITIES_CACHE_LOCK = threading.Lock()
_COMMODITIES_CACHE_TTL  = 15 * 60  # 15 minutes

# Chart windows served to the client, plus the extra history fetched *before*
# each window (`pre_1y` / `pre_5y` in the payload). The lookback is what lets
# the 50/200-day moving averages be defined at the very first plotted point
# instead of only in the final fifth of the chart.
_DAILY_WINDOW  = 252  # ~1 year of trading days (daily chart)
_DAILY_MA_PRE  = 200  # longest daily MA
_WEEKLY_WINDOW = 261  # ~5 years of weeks (weekly chart)
_WEEKLY_MA_PRE = 40   # 40 weeks ≈ 200 trading days


@bp.route("/commodities")
def commodities():
    """Commodities overview page — futures across metals, energy, and ag."""
    return render_template("commodities.html",
                           peer_groups=list(PEER_GROUPS.keys()))


@bp.route("/api/commodities")
def api_commodities():
    """JSON snapshot for the commodities page.

    Returns price history (daily 1y, weekly 5y) plus computed metrics for
    each commodity in ``_COMMODITY_SYMBOLS``. Bulk-downloads via yfinance
    for speed and caches in-memory for 15 minutes.
    """
    import yfinance as yf
    import numpy as np

    with _COMMODITIES_CACHE_LOCK:
        entry = _COMMODITIES_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _COMMODITIES_CACHE_TTL:
            log.info("API commodities: served from memory cache")
            return jsonify(entry["data"])

    log.info("API commodities: fetching fresh data from Yahoo Finance")

    def _rsi(prices: list, period: int = 14) -> Optional[float]:
        arr = [p for p in prices if p is not None]
        if len(arr) < period + 1:
            return None
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
        return round(sum(vals[-n:]) / n, 4)

    def _idx_back(prices: list, days: int) -> Optional[float]:
        """Return the last non-null close N trading days back, or None."""
        if days >= len(prices):
            return None
        v = prices[-(days + 1)]
        return v if v is not None else None

    def _ytd_value(dates: list, prices: list) -> Optional[float]:
        """First non-null close of the current calendar year."""
        from datetime import date
        year = str(date.today().year)
        for d, p in zip(dates, prices):
            if d.startswith(year) and p is not None:
                return p
        return None

    symbols = [v[0] for v in _COMMODITY_SYMBOLS.values()]
    keys    = list(_COMMODITY_SYMBOLS.keys())
    sym_to_key = {v[0]: k for k, v in _COMMODITY_SYMBOLS.items()}

    # Bulk download — 2y daily: the trailing year is charted, the year before
    # it is MA lookback (and gives _ret(252) a real 1-year comparison point).
    try:
        raw_1y = yf.download(symbols, period="2y", interval="1d",
                             group_by="ticker", auto_adjust=False,
                             progress=False, threads=True)
    except Exception as exc:
        log.error("Commodities: yfinance daily download failed: %s", exc)
        return jsonify({"error": "Failed to fetch commodity data"}), 502

    # 10y weekly for the long-term chart — 5y charted + MA lookback before it
    try:
        raw_5y = yf.download(symbols, period="10y", interval="1wk",
                             group_by="ticker", auto_adjust=False,
                             progress=False, threads=True)
    except Exception:
        raw_5y = None

    out: dict[str, Any] = {}

    def _close_series(raw, sym):
        """Pull (dates, prices) from a multi-ticker yfinance frame."""
        try:
            if hasattr(raw.columns, "levels"):
                # Multi-index: ('GC=F','Close')
                if (sym, "Close") in raw.columns:
                    s = raw[(sym, "Close")]
                elif sym in raw.columns.get_level_values(0):
                    sub = raw[sym]
                    if "Close" in sub.columns:
                        s = sub["Close"]
                    else:
                        return [], []
                else:
                    return [], []
            else:
                s = raw["Close"]
        except Exception:
            return [], []
        dates = [str(d.date()) for d in s.index]
        prices = []
        for v in s.tolist():
            try:
                f = float(v)
                prices.append(round(f, 4) if not math.isnan(f) and not math.isinf(f) else None)
            except (TypeError, ValueError):
                prices.append(None)
        return dates, prices

    for sym in symbols:
        key = sym_to_key[sym]
        meta = _COMMODITY_SYMBOLS[key]
        d1, p1 = _close_series(raw_1y, sym)
        if not p1:
            out[key] = {
                "symbol": sym, "name": meta[1], "unit": meta[2],
                "emoji": meta[3], "group": meta[4],
                "error": "No data",
            }
            continue

        # Strip leading None values for cleaner metrics
        valid = [(d, p) for d, p in zip(d1, p1) if p is not None]
        if not valid:
            continue

        last      = valid[-1][1]
        prev      = valid[-2][1] if len(valid) > 1 else None
        day_chg   = (last - prev) if (last is not None and prev is not None) else None
        day_chg_pct = (day_chg / prev * 100) if (day_chg is not None and prev) else None

        win52  = [v for _, v in valid[-252:]]
        high52 = max(win52) if win52 else None
        low52  = min(win52) if win52 else None
        # current position in 52w range as 0-100
        pos52  = None
        if high52 is not None and low52 is not None and high52 > low52:
            pos52 = round((last - low52) / (high52 - low52) * 100, 1)

        # Period-return helpers (pct vs N days back)
        def _ret(days):
            v = _idx_back(p1, days)
            if v is None or v == 0 or last is None:
                return None
            return round((last - v) / v * 100, 2)

        ret_1w  = _ret(5)
        ret_1m  = _ret(21)
        ret_3m  = _ret(63)
        ret_6m  = _ret(126)
        ret_1y  = _ret(252)
        ytd_v   = _ytd_value(d1, p1)
        ret_ytd = (round((last - ytd_v) / ytd_v * 100, 2)
                   if ytd_v is not None and ytd_v != 0 else None)

        ma_50  = _ma(p1, 50)
        ma_200 = _ma(p1, 200)
        rsi_14 = _rsi(p1, 14)

        # 5y weekly history for long-term chart, split into the charted window
        # and the 40 weeks of MA lookback that precede it
        d5, p5 = _close_series(raw_5y, sym) if raw_5y is not None else ([], [])
        d5_win, p5_win = d5[-_WEEKLY_WINDOW:], p5[-_WEEKLY_WINDOW:]
        pre_5y = p5[:-_WEEKLY_WINDOW][-_WEEKLY_MA_PRE:]

        # Same split for the daily series: chart the trailing year, keep the
        # 200 sessions before it so the client's 200-day MA starts at day one
        d1_win, p1_win = d1[-_DAILY_WINDOW:], p1[-_DAILY_WINDOW:]
        pre_1y = p1[:-_DAILY_WINDOW][-_DAILY_MA_PRE:]

        out[key] = {
            "symbol":      sym,
            "name":        meta[1],
            "unit":        meta[2],
            "emoji":       meta[3],
            "group":       meta[4],
            "last":        last,
            "prev":        prev,
            "day_chg":     day_chg,
            "day_chg_pct": day_chg_pct,
            "high52":      high52,
            "low52":       low52,
            "pos52":       pos52,
            "ret_1w":      ret_1w,
            "ret_1m":      ret_1m,
            "ret_3m":      ret_3m,
            "ret_6m":      ret_6m,
            "ret_ytd":     ret_ytd,
            "ret_1y":      ret_1y,
            "ma_50":       ma_50,
            "ma_200":      ma_200,
            "rsi_14":      rsi_14,
            "above_ma_50":  (last is not None and ma_50  is not None and last > ma_50),
            "above_ma_200": (last is not None and ma_200 is not None and last > ma_200),
            "dates_1y":    d1_win,
            "prices_1y":   p1_win,
            "pre_1y":      pre_1y,
            "dates_5y":    d5_win,
            "prices_5y":   p5_win,
            "pre_5y":      pre_5y,
        }

    # ── Cross-commodity ratios (computed from full daily history when present)
    def _ratio_series(num_key: str, den_key: str):
        a = out.get(num_key, {}); b = out.get(den_key, {})
        a_d, a_p = a.get("dates_1y", []), a.get("prices_1y", [])
        b_d, b_p = b.get("dates_1y", []), b.get("prices_1y", [])
        bm = dict(zip(b_d, b_p))
        dates, ratios = [], []
        for d, p in zip(a_d, a_p):
            q = bm.get(d)
            if p is None or q is None or q == 0:
                continue
            dates.append(d); ratios.append(round(p / q, 4))
        return dates, ratios

    ratios = {}
    if "gold" in out and "silver" in out:
        d, r = _ratio_series("gold", "silver")
        ratios["gold_silver"] = {
            "label": "Gold / Silver",
            "desc":  "Ounces of silver per ounce of gold — historic mean ≈55. High = silver cheap relative to gold.",
            "dates": d, "values": r,
            "current": r[-1] if r else None,
        }
    if "gold" in out and "oil_wti" in out:
        d, r = _ratio_series("gold", "oil_wti")
        ratios["gold_oil"] = {
            "label": "Gold / Oil",
            "desc":  "Barrels of WTI crude per ounce of gold — inflation gauge. Historic mean ≈18.",
            "dates": d, "values": r,
            "current": r[-1] if r else None,
        }
    if "gold" in out and "copper" in out:
        d, r = _ratio_series("gold", "copper")
        ratios["gold_copper"] = {
            "label": "Gold / Copper",
            "desc":  "Risk-off indicator. Rising = flight to safety; falling = economic optimism.",
            "dates": d, "values": r,
            "current": r[-1] if r else None,
        }
    if "oil_wti" in out and "natgas" in out:
        d, r = _ratio_series("oil_wti", "natgas")
        ratios["oil_natgas"] = {
            "label": "Oil / Nat Gas",
            "desc":  "Energy spread (WTI per MMBtu). Historic ≈10. Wide = oil rich vs gas.",
            "dates": d, "values": r,
            "current": r[-1] if r else None,
        }

    payload = {
        "commodities": out,
        "ratios":      ratios,
        "ts":          int(time.time()),
    }

    with _COMMODITIES_CACHE_LOCK:
        _COMMODITIES_CACHE["data"] = {"ts": time.time(), "data": payload}

    return jsonify(payload)


# 24-hour cache for seasonality data (expensive 10y monthly fetch)
_SEASONALITY_CACHE: dict = {}
_SEASONALITY_CACHE_LOCK = threading.Lock()


@bp.route("/api/commodities/seasonality")
def api_commodities_seasonality():
    """
    Monthly seasonality for commodities.

    Returns for each commodity the average monthly return (%) over the past
    10 years, grouped by calendar month (Jan–Dec).  Cached for 24 hours.
    """
    import yfinance as yf
    import pandas as pd

    with _SEASONALITY_CACHE_LOCK:
        entry = _SEASONALITY_CACHE.get("data")
        if entry and time.time() - entry["ts"] < 24 * 3600:
            log.info("API seasonality: served from cache")
            return jsonify(entry["data"])

    log.info("API seasonality: fetching 10y monthly data from Yahoo Finance")
    symbols = {k: v[0] for k, v in _COMMODITY_SYMBOLS.items()}
    all_syms = list(symbols.values())

    try:
        raw = yf.download(all_syms, period="10y", interval="1mo",
                          auto_adjust=True, progress=False, group_by="ticker")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    seasonality: dict[str, list[Optional[float]]] = {}
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    for key, sym in symbols.items():
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                col = raw[sym]["Close"] if sym in raw.columns.get_level_values(0) else None
            else:
                col = raw["Close"]

            if col is None or col.dropna().empty:
                continue

            rets = col.dropna().pct_change().dropna() * 100
            monthly_avgs: list[Optional[float]] = []
            for m in range(1, 13):
                vals = rets[rets.index.month == m]
                monthly_avgs.append(round(float(vals.mean()), 2) if len(vals) > 0 else None)
            seasonality[key] = monthly_avgs
        except Exception as exc:
            log.warning("Seasonality: could not compute %s: %s", key, exc)
            continue

    data = {"months": month_names, "seasonality": seasonality}
    with _SEASONALITY_CACHE_LOCK:
        _SEASONALITY_CACHE["data"] = {"ts": time.time(), "data": data}
    return jsonify(data)


@bp.route("/daily")
def daily_report():
    """Daily markets summary report page."""
    return render_template("daily_report.html",
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
            log.info("API markets: served from memory cache")
            # The snapshot timestamp was tracked in _MARKETS_CACHE but never
            # shipped, so the page could not tell a live quote from a Friday
            # close it had been showing all weekend.
            return jsonify({**entry["data"], "meta": freshness.classify_quote(entry["ts"])})

    # Memory miss — try DynamoDB before hitting Yahoo Finance
    ddb_entry = _markets_load_from_dynamo()
    if ddb_entry:
        log.info("API markets: served from DynamoDB cache")
        with _MARKETS_CACHE_LOCK:
            _MARKETS_CACHE["data"] = ddb_entry
        return jsonify({**ddb_entry["data"], "meta": freshness.classify_quote(ddb_entry.get("ts"))})

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
            daily = yf.download("^VIX", period="2y", interval="1d", auto_adjust=True, progress=False)
            if isinstance(daily.columns, pd.MultiIndex):
                daily = daily.xs("^VIX", axis=1, level=1)
            current = round(float(daily["Close"].iloc[-1]), 2) if not daily.empty else None
            prev    = round(float(daily["Close"].iloc[-2]), 2) if len(daily) >= 2 else None
            day_chg = round((current - prev) / prev * 100, 2) if current and prev and prev > 0 else None
            # Resample daily → weekly (last close of each week) for the chart
            weekly  = daily["Close"].resample("W").last().dropna()
            prices  = [round(float(p), 2) for p in weekly]
            dates   = [str(d.date()) for d in weekly.index]

            # Fetch VIX3M and VVIX for term structure
            vix3m_current = None
            vvix_current  = None
            try:
                vix_extra = yf.download(["^VIX3M", "^VVIX"], period="2d", interval="1d",
                                        auto_adjust=True, progress=False)
                if not vix_extra.empty:
                    def _last_val(ticker):
                        try:
                            s = vix_extra["Close"][ticker].dropna()
                            return round(float(s.iloc[-1]), 2) if len(s) > 0 else None
                        except Exception:
                            return None
                    vix3m_current = _last_val("^VIX3M")
                    vvix_current  = _last_val("^VVIX")
            except Exception:
                pass

            term_ratio = round(vix3m_current / current, 2) if vix3m_current and current else None

            return {
                "current":    current,
                "day_chg":    day_chg,
                "vix3m":      vix3m_current,
                "vvix":       vvix_current,
                "term_ratio": term_ratio,
                "weekly":     {"dates": dates, "prices": prices},
            }
        except Exception as exc:
            log.warning("Could not fetch VIX: %s", exc)
            return None

    def _fetch_sector_etfs() -> list:
        results = []
        try:
            tickers = yf.download(
                list(_SECTOR_ETFS.keys()), period="10d", interval="1d",
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
                    last = vals[-1] if vals else None
                    # Week change: last price vs 6 trading days ago
                    try:
                        week_close = float(col.dropna().iloc[-6]) if len(col.dropna()) >= 6 else None
                        week_chg_pct = round((last - week_close) / week_close * 100, 2) if week_close else None
                    except Exception:
                        week_chg_pct = None
                    results.append({"ticker": sym, "label": label, "day_chg": chg, "week_chg_pct": week_chg_pct})
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
    return jsonify({**result, "meta": freshness.classify_quote(ts)})


# ---------------------------------------------------------------------------
# HYG / TLT Credit Spread  (/api/credit-spread)
# ---------------------------------------------------------------------------

_CREDIT_SPREAD_CACHE: dict = {}
_CREDIT_SPREAD_CACHE_LOCK = threading.Lock()
_CREDIT_SPREAD_CACHE_TTL  = 3600 * 4  # 4 hours


@bp.route("/api/credit-spread")
def api_credit_spread():
    """
    Return weekly/monthly HYG and TLT price history plus the HYG/TLT ratio.

    Query params:
      period: 1y | 2y | 3y | 5y | 10y  (default: 1y)
    """
    import yfinance as yf
    from flask import request as _req

    VALID_PERIODS = {"1y", "2y", "3y", "5y", "10y"}
    period = _req.args.get("period", "1y")
    if period not in VALID_PERIODS:
        period = "1y"
    # Use weekly for ≤3y, monthly for longer so the chart stays readable
    interval = "1wk" if period in ("1y", "2y", "3y") else "1mo"

    cache_key = period
    with _CREDIT_SPREAD_CACHE_LOCK:
        entry = _CREDIT_SPREAD_CACHE.get(cache_key)
        if entry and time.time() - entry["ts"] < _CREDIT_SPREAD_CACHE_TTL:
            return jsonify(entry["data"])

    def _fetch_etf(symbol: str) -> dict:
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(period=period, interval=interval)
            prices = [round(float(p), 4) if not math.isnan(float(p)) else None for p in hist["Close"]]
            dates  = [str(d.date()) for d in hist.index]
            valid  = [p for p in prices if p is not None]
            current = valid[-1] if valid else None
            prev    = valid[-2] if len(valid) >= 2 else None
            day_chg = None
            if current and prev and prev > 0:
                raw_chg = (current - prev) / prev * 100
                day_chg = round(raw_chg, 2) if abs(raw_chg) <= 25 else None
            return {
                "price":       current,
                "day_chg_pct": day_chg,
                "dates":       dates,
                "prices":      prices,
            }
        except Exception as exc:
            log.warning("credit-spread: could not fetch %s: %s", symbol, exc)
            return {"price": None, "day_chg_pct": None, "dates": [], "prices": []}

    hyg = _fetch_etf("HYG")
    tlt = _fetch_etf("TLT")

    # Align dates (inner join on date strings) and compute ratio
    hyg_map = dict(zip(hyg["dates"], hyg["prices"]))
    tlt_map = dict(zip(tlt["dates"], tlt["prices"]))
    common_dates = sorted(set(hyg_map) & set(tlt_map))
    spread = []
    for d in common_dates:
        h, t = hyg_map[d], tlt_map[d]
        spread.append(round(h / t, 6) if h is not None and t is not None and t > 0 else None)

    result = {
        "period":       period,
        "interval":     interval,
        "hyg":          hyg,
        "tlt":          tlt,
        "spread_dates": common_dates,
        "spread":       spread,
    }

    ts = time.time()
    with _CREDIT_SPREAD_CACHE_LOCK:
        _CREDIT_SPREAD_CACHE[cache_key] = {"ts": ts, "data": result}

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


def _fg_save_to_dynamo(history: list) -> int:
    """Batch-write history items [{date, score, rating}] to DynamoDB.

    Returns the number of records actually written.

    `date` is the table's partition key and the upstream CNN feed can repeat a
    date, so items are collapsed by date first (last value wins). BatchWriteItem
    rejects the *whole* batch with a ValidationException if one request contains
    duplicate keys, so without this every write would fail.
    """
    table = _get_fg_table()
    if not table or not history:
        return 0
    deduped: dict[str, dict] = {}
    for item in history:
        if not item.get("date") or item.get("score") is None:
            continue
        deduped[item["date"]] = {
            "date":   item["date"],
            "score":  Decimal(str(round(float(item["score"]), 2))),
            "rating": item.get("rating") or "",
        }
    if not deduped:
        return 0
    try:
        with table.batch_writer(overwrite_by_pkeys=["date"]) as batch:
            for record in deduped.values():
                batch.put_item(Item=record)
        return len(deduped)
    except Exception as exc:
        log.warning("DynamoDB fear-greed write failed: %s", exc)
        return 0


_CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CNN_FG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.4 Safari/605.1.15"
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
    log.info("API fear-greed")

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
        # Log what was actually stored, not what we attempted — the previous
        # version reported success even when the batch write raised.
        written = _fg_save_to_dynamo(new_records)
        if written:
            log.info("Saved %d new Fear & Greed records to DynamoDB", written)
        else:
            log.warning("Fear & Greed: %d new records could not be saved",
                        len(new_records))

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
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.4 Safari/605.1.15"
    ),
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.cboe.com",
    "Referer":         "https://www.cboe.com/",
    "X-Requested-With": "XMLHttpRequest",
}

_PCR_TABLE_NAME = "ystocker-pcr-history"
_pcr_ddb_table  = None
_pcr_ddb_unavail_until = 0.0
_PCR_DDB_LOCK   = threading.Lock()


def _get_pcr_ddb_table():
    """Return boto3 DynamoDB Table for PCR history, or None if unavailable."""
    global _pcr_ddb_table, _pcr_ddb_unavail_until
    if _pcr_ddb_table is not None:
        return _pcr_ddb_table
    if time.time() < _pcr_ddb_unavail_until:
        return None
    with _PCR_DDB_LOCK:
        if _pcr_ddb_table is not None:
            return _pcr_ddb_table
        if time.time() < _pcr_ddb_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(_PCR_TABLE_NAME)
            tbl.load()
            _pcr_ddb_table = tbl
            log.info("DynamoDB PCR history table connected: %s", _PCR_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB PCR history unavailable: %s", exc)
            _pcr_ddb_table = None
            _pcr_ddb_unavail_until = time.time() + 300
        return _pcr_ddb_table


def _pcr_load_history() -> dict[str, float]:
    """Load all stored PCR rows from DynamoDB. Returns {date_str: value}."""
    table = _get_pcr_ddb_table()
    if not table:
        return {}
    try:
        from boto3.dynamodb.conditions import Key
        from datetime import date, timedelta
        cutoff = str(date.today() - timedelta(days=366))
        resp = table.scan(
            FilterExpression="#d >= :cutoff",
            ExpressionAttributeNames={"#d": "date"},
            ExpressionAttributeValues={":cutoff": cutoff},
        )
        result = {}
        for item in resp.get("Items", []):
            result[item["date"]] = float(item["equity_pcr"])
        return result
    except Exception as exc:
        log.warning("DynamoDB PCR load failed: %s", exc)
        return {}


def _pcr_save_row(date_str: str, equity_pcr: float) -> None:
    """Persist a single PCR row to DynamoDB."""
    table = _get_pcr_ddb_table()
    if not table:
        return
    try:
        table.put_item(Item={"date": date_str, "equity_pcr": str(round(equity_pcr, 3))})
    except Exception as exc:
        log.warning("DynamoDB PCR save failed for %s: %s", date_str, exc)


def _fetch_pcr_for_date(date_str: str) -> float | None:
    """Fetch the EQUITY PUT/CALL RATIO for a single trading date from CBOE daily endpoint."""
    import requests
    url = f"https://cdn.cboe.com/data/us/options/market_statistics/daily/{date_str}_daily_options"
    try:
        resp = requests.get(url, headers=_PCR_HEADERS, timeout=15,
                            proxies={"http": None, "https": None})
        if resp.status_code == 403 or resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        for r in data.get("ratios", []):
            if r.get("name", "").upper() == "EQUITY PUT/CALL RATIO":
                return round(float(r["value"]), 3)
    except Exception as exc:
        log.debug("PCR fetch for %s failed: %s", date_str, exc)
    return None


def _prev_trading_days(n: int) -> list[str]:
    """Return the last n calendar days going backwards from yesterday (skipping weekends)."""
    from datetime import date, timedelta
    results = []
    d = date.today() - timedelta(days=1)
    while len(results) < n:
        if d.weekday() < 5:  # Mon–Fri
            results.append(str(d))
        d -= timedelta(days=1)
    return results


@bp.route("/api/put-call-ratio")
def api_put_call_ratio():
    """Return CBOE Equity Put/Call Ratio history (1Y).
    Fetches latest day from CBOE daily endpoint, persists to DynamoDB, merges for history."""
    log.info("API put-call-ratio")
    with _PCR_CACHE_LOCK:
        entry = _PCR_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _PCR_CACHE_TTL:
            return jsonify(entry["data"])

    # Load stored history from DynamoDB
    history = _pcr_load_history()

    from datetime import date, timedelta
    # Build the expected set of trading days for the last 1 year
    expected = []
    d = date.today()
    cutoff_date = date.today() - timedelta(days=365)
    while d >= cutoff_date:
        if d.weekday() < 5:
            expected.append(str(d))
        d -= timedelta(days=1)

    # Fetch any missing trading days (up to 5 per request to avoid latency)
    fetched = 0
    for candidate in expected:
        if candidate not in history and fetched < 5:
            val = _fetch_pcr_for_date(candidate)
            if val is not None:
                history[candidate] = val
                _pcr_save_row(candidate, val)
                log.info("PCR: fetched and saved %s = %s", candidate, val)
            fetched += 1

    if not history:
        return jsonify({"error": "Put/Call ratio data unavailable"}), 502

    # Sort by date, keep last 1 year
    cutoff = str(date.today() - timedelta(days=365))
    rows = sorted((d, v) for d, v in history.items() if d >= cutoff)

    dates  = [r[0] for r in rows]
    closes = [r[1] for r in rows]

    current = closes[-1] if closes else None
    prev    = closes[-2] if len(closes) >= 2 else None
    day_chg = round(current - prev, 3) if current is not None and prev is not None else None
    ma20    = round(sum(closes[-20:]) / min(len(closes), 20), 3) if closes else None

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
# CBOE SKEW + VIX history  (/api/skew)
# ---------------------------------------------------------------------------

_SKEW_CACHE: dict = {}
_SKEW_LOCK = threading.Lock()
_SKEW_TTL = 4 * 3600  # 4 hours

# Interpretation bands for the SKEW index. Upper bound is exclusive, so a
# reading sits in the first band whose `hi` it falls under; `None` = open top.
_SKEW_BANDS: tuple[tuple[float | None, str], ...] = (
    (120.0, "weak"),      # < 120        tail-protection demand is soft
    (135.0, "neutral"),   # 120 - 135    normal / neutral
    (145.0, "notable"),   # 135 - 145    demand worth noting
    (155.0, "elevated"),  # 145 - 155    clearly elevated
    (None,  "extreme"),   # > 155        extreme tail-insurance premium
)


def _skew_band(value: float | None) -> str | None:
    """Classify a SKEW reading into one of the five interpretation bands."""
    if value is None:
        return None
    for hi, name in _SKEW_BANDS:
        if hi is None or value < hi:
            return name
    return "extreme"


def _breadth_pct50_cached() -> dict | None:
    """% of S&P 500 above its 50-day MA, but only if it is already cached.

    ``breadth.peek()`` never rebuilds, so /api/skew cannot inherit the ~25s
    500-ticker download that a cold ``get_breadth()`` would trigger. If nothing
    is cached yet the breadth leg reports "unknown" rather than blocking.
    """
    try:
        payload = breadth.peek()
        if not payload:
            return None
        series = payload.get("pct_above_ma", {}).get("50")
        return series if series and series.get("values") else None
    except Exception as exc:
        log.debug("skew: breadth unavailable for divergence check: %s", exc)
        return None


def _build_divergence(skew_s, vix_s, vvix_s, spx_s, ratio_s) -> dict:
    """Score the cross-market tail-risk divergence.

    The signal that matters is not a high SKEW on its own — it is spot equity
    calm *and* ordinary-vol calm *and* the tail-option market refusing to agree.
    Each leg is reported with its live value so a near-miss stays visible
    instead of collapsing into a silent False.
    """
    def _last(s):
        return float(s.iloc[-1]) if s is not None and len(s) else None

    def _mean(s, n):
        return float(s.tail(n).mean()) if s is not None and len(s) >= max(2, n // 2) else None

    conds: list[dict] = []

    def _add(key, met, fmt, now=None, ref=None, near=False):
        # Numbers stay numbers: the client owns formatting so the units and
        # comparison words ("20d avg", "8w ago") can be translated. Baking a
        # display string here would hard-code English into a bilingual page.
        conds.append({"key": key, "met": met, "near": bool(near) and met is not True,
                      "fmt": fmt, "now": now, "ref": ref})

    # 1. S&P 500 at / near a new high -------------------------------------
    spx_last, spx_hi = _last(spx_s), (float(spx_s.max()) if spx_s is not None and len(spx_s) else None)
    if spx_last and spx_hi:
        off = (spx_hi - spx_last) / spx_hi * 100
        _add("spx_high", off <= 1.0, "pct_off_high", round(off, 1), near=off <= 3.0)
    else:
        _add("spx_high", None, "na")

    # 2. VIX genuinely low (13-15 is the sweet spot for a real divergence) --
    vix_last = _last(vix_s)
    if vix_last:
        _add("vix_low", 13.0 <= vix_last <= 15.0, "level1", round(vix_last, 1),
             near=12.0 <= vix_last <= 16.5)
    else:
        _add("vix_low", None, "na")

    # 3. SKEW 150+ ---------------------------------------------------------
    skew_last = _last(skew_s)
    if skew_last:
        _add("skew_high", skew_last >= 150.0, "level0", round(skew_last),
             near=skew_last >= 145.0)
    else:
        _add("skew_high", None, "na")

    # 4. VVIX rising (vol-of-vol bid = hedging the hedges) -----------------
    vvix_last, vvix_20 = _last(vvix_s), _mean(vvix_s, 20)
    if vvix_last and vvix_20:
        _add("vvix_rising", vvix_last > vvix_20, "vs_20d",
             round(vvix_last), round(vvix_20))
    else:
        _add("vvix_rising", None, "na")

    # 5. Put skew still steepening. SKEW *is* the SPX put-skew measure, so
    #    "steepening" is its own short-term trend, not a separate series.
    skew_5, skew_20 = _mean(skew_s, 5), _mean(skew_s, 20)
    if skew_5 and skew_20:
        _add("put_skew_steep", skew_5 > skew_20, "ma5_vs_ma20",
             round(skew_5), round(skew_20))
    else:
        _add("put_skew_steep", None, "na")

    # 6. Breadth narrowing -------------------------------------------------
    b50 = _breadth_pct50_cached()
    vals = [v for v in b50["values"] if v is not None] if b50 else []
    if len(vals) >= 9:
        now_v, then_v = vals[-1], vals[-9]  # weekly series -> ~8 weeks back
        _add("breadth_falling", now_v < then_v, "pct_vs_8w",
             round(now_v), round(then_v))
    else:
        _add("breadth_falling", None, "na")

    # 7. Credit spreads widening. HYG/TLT falling = high yield losing to
    #    duration = compensation for credit risk being repriced upward.
    r_last, r_20, r_60 = _last(ratio_s), _mean(ratio_s, 20), _mean(ratio_s, 60)
    if r_last and r_20:
        _add("credit_widening", r_last < r_20, "ratio_vs_20d",
             round(r_last, 3), round(r_20, 3),
             near=bool(r_60 and r_last < r_60))
    else:
        _add("credit_widening", None, "na")

    met     = sum(1 for c in conds if c["met"] is True)
    known   = sum(1 for c in conds if c["met"] is not None)
    # "Fragile calm" needs both halves: the calm (equity high + low VIX) and the
    # disagreement (tail bid). A high score built only from breadth/credit is
    # ordinary risk-off, not the divergence described here.
    calm    = any(c["key"] == "spx_high" and c["met"] for c in conds) and \
              any(c["key"] == "vix_low" and c["met"] for c in conds)
    tail_bid = any(c["key"] == "skew_high" and c["met"] for c in conds)

    if met >= 6 and calm and tail_bid:
        level = "strong"
    elif met >= 4 and tail_bid:
        level = "building"
    elif met >= 3:
        level = "partial"
    else:
        level = "quiet"

    return {"conditions": conds, "met": met, "known": known,
            "total": len(conds), "level": level}


@bp.route("/api/skew")
def api_skew():
    """CBOE SKEW vs VIX — 2y of daily history plus the cross-market divergence.

    One download covers all six legs of the divergence check. HYG/TLT is pulled
    here at daily resolution rather than reused from /api/credit-spread, which
    only keeps weekly bars for its 1y window — too coarse to tell whether
    spreads started widening this week.
    """
    import yfinance as yf
    with _SKEW_LOCK:
        entry = _SKEW_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _SKEW_TTL:
            return jsonify(entry["data"])
    try:
        df = yf.download(["^SKEW", "^VIX", "^VVIX", "^GSPC", "HYG", "TLT"],
                         period="2y", interval="1d",
                         auto_adjust=True, progress=False)
        closes = df["Close"] if hasattr(df.columns, "levels") else df[["Close"]]

        def _series(sym):
            return closes[sym].dropna() if sym in closes.columns else None

        skew_s = _series("^SKEW")
        vix_s  = _series("^VIX")
        if skew_s is None or vix_s is None or skew_s.empty or vix_s.empty:
            raise RuntimeError("yfinance returned no ^SKEW/^VIX closes")
        vvix_s, spx_s = _series("^VVIX"), _series("^GSPC")
        hyg_s, tlt_s  = _series("HYG"), _series("TLT")
        ratio_s = None
        if hyg_s is not None and tlt_s is not None:
            ratio_s = (hyg_s / tlt_s).dropna()

        # Chart series stay on the SKEW-VIX intersection. The divergence scorecard
        # deliberately does not: CBOE publishes SKEW a day behind VIX, so joining
        # first would judge today's tape on yesterday's VIX.
        common = sorted(set(skew_s.index) & set(vix_s.index))
        dates = [str(d.date()) for d in common]
        skew  = [round(float(skew_s[d]), 2) for d in common]
        vix   = [round(float(vix_s[d]),  2) for d in common]

        last_skew = round(float(skew_s.iloc[-1]), 2)
        pctile = round(float((skew_s < skew_s.iloc[-1]).sum()) / len(skew_s) * 100, 1)

        result = {
            "dates": dates, "skew": skew, "vix": vix,
            "latest": {
                "skew":       last_skew,
                "skew_date":  str(skew_s.index[-1].date()),
                "band":       _skew_band(last_skew),
                "percentile": pctile,
                "vix":        round(float(vix_s.iloc[-1]), 2),
                "vvix":       round(float(vvix_s.iloc[-1]), 2) if vvix_s is not None and len(vvix_s) else None,
            },
            "divergence": _build_divergence(skew_s, vix_s, vvix_s, spx_s, ratio_s),
        }
        with _SKEW_LOCK:
            _SKEW_CACHE["data"] = {"ts": time.time(), "data": result}
        return jsonify(result)
    except Exception as exc:
        log.warning("api_skew failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Market Breadth  (/api/breadth)
# ---------------------------------------------------------------------------

@bp.route("/api/breadth")
def api_breadth():
    """Market breadth: % of S&P 500 above each key MA + RSP/SPY concentration.

    Never builds in the request. This used to call ``get_breadth()``, which falls
    through to the 500-ticker download when the cache is cold -- measured at
    **81.9s** on the box, against an nginx/Gunicorn budget of a few seconds and
    only two workers to lose. One cold request therefore took the whole app down:
    /markets and /api/multiples timed out alongside it, because both workers were
    parked on Yahoo. breadth.py's own docstring already said the rebuild "must
    never happen inside a request"; this endpoint was the one place that did.

    So: serve whatever is already paid for, stale included -- a day-old diffusion
    index still answers "is breadth narrowing?" and ships its own ``asof`` -- and
    otherwise report 202 and let the background thread do the work, the same
    contract /api/multiples has used all along.
    """
    try:
        cached = breadth.peek()
        if cached:
            # peek() ignores TTL, so say when the payload is past it rather than
            # passing week-old data off as current. The client shows the date.
            ts = cached.get("_ts") or 0
            stale = (time.time() - ts) >= breadth.CACHE_TTL if ts else True
            return jsonify({**cached, "stale": stale or bool(cached.get("stale"))})

        if breadth.is_warming():
            log.info("API breadth: build in progress, returning 202")
            return jsonify({"status": "warming", "warming": True}), 202

        log.info("API breadth: no cache, starting background rebuild")
        threading.Thread(target=breadth.refresh_cache, daemon=True,
                         name="breadth-auto-warm").start()
        return jsonify({"status": "initializing", "warming": True}), 202
    except Exception as exc:
        log.warning("api_breadth failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Sector Rotation Grid  (/api/sector-rotation-grid)
# ---------------------------------------------------------------------------

_SECTOR_GRID_CACHE: dict = {}
_SECTOR_GRID_LOCK = threading.Lock()
_SECTOR_GRID_TTL = 4 * 3600  # 4 hours

_SECTOR_GRID_ETFS = {
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Cons. Disc.",
    "XLP": "Cons. Stap.", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Comm.",
}
_SECTOR_PERIODS = ["1W", "1M", "3M", "6M", "YTD", "1Y"]

@bp.route("/api/sector-rotation-grid")
def api_sector_rotation_grid():
    """Multi-period sector performance vs SPY for the rotation heatmap."""
    import yfinance as yf
    import datetime as _dt
    with _SECTOR_GRID_LOCK:
        entry = _SECTOR_GRID_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _SECTOR_GRID_TTL:
            return jsonify(entry["data"])
    try:
        tickers = list(_SECTOR_GRID_ETFS.keys()) + ["SPY"]
        df = yf.download(tickers, period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        closes = df["Close"]
        today = _dt.date.today()
        ytd_start = _dt.date(today.year, 1, 1)
        def _back(days):
            return (today - _dt.timedelta(days=days)).isoformat()
        cutoffs = {
            "1W": _back(7), "1M": _back(31), "3M": _back(92),
            "6M": _back(183), "YTD": ytd_start.isoformat(), "1Y": _back(365),
        }
        def _ret(sym, cutoff):
            s = closes[sym].dropna()
            s = s[s.index.date >= _dt.date.fromisoformat(cutoff)]
            if len(s) < 2: return None
            return round((float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100, 2)
        spy_rets = {p: _ret("SPY", c) for p, c in cutoffs.items()}
        rows = []
        for etf, name in _SECTOR_GRID_ETFS.items():
            rets = {}
            for period, cutoff in cutoffs.items():
                abs_ret = _ret(etf, cutoff)
                spy_ret = spy_rets[period]
                rets[period] = round(abs_ret - spy_ret, 2) if (abs_ret is not None and spy_ret is not None) else None
            rows.append({"etf": etf, "name": name, "returns": rets})
        result = {"rows": rows, "periods": _SECTOR_PERIODS}
        with _SECTOR_GRID_LOCK:
            _SECTOR_GRID_CACHE["data"] = {"ts": time.time(), "data": result}
        return jsonify(result)
    except Exception as exc:
        log.warning("api_sector_rotation_grid failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


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
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.aaii.com/sentimentsurvey/sent_results",
    "Connection": "keep-alive",
}

_AAII_XLS_URL = "https://www.aaii.com/files/surveys/sentiment.xls"


# ---------------------------------------------------------------------------
# Gold / Silver / Copper Ratios  (/api/gold-ratios)
# ---------------------------------------------------------------------------

_GOLD_RATIOS_CACHE: dict = {}
_GOLD_RATIOS_CACHE_LOCK = threading.Lock()
_GOLD_RATIOS_CACHE_TTL  = 3600  # 1 hour


@bp.route("/api/gold-ratios")
def api_gold_ratios():
    """Return Gold/Silver and Gold/Copper ratio history (2Y daily)."""
    log.info("API gold-ratios")
    with _GOLD_RATIOS_CACHE_LOCK:
        entry = _GOLD_RATIOS_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _GOLD_RATIOS_CACHE_TTL:
            return jsonify(entry["data"])

    import yfinance as yf
    import math as _math

    try:
        # GC=F gold ($/troy oz), SI=F silver ($/troy oz), HG=F copper ($/lb)
        raw_full = yf.download(["GC=F", "SI=F", "HG=F"],
                               period="2y", interval="1d",
                               auto_adjust=True, progress=False)

        # Normalise column structure — yfinance may return MultiIndex or flat columns
        import pandas as _pd
        if isinstance(raw_full.columns, _pd.MultiIndex):
            # Typical structure: ('Close','GC=F'), ('Close','HG=F'), etc.
            close_cols = raw_full.xs("Close", axis=1, level=0, drop_level=True) \
                         if "Close" in raw_full.columns.get_level_values(0) \
                         else raw_full.xs("Close", axis=1, level=1, drop_level=True)
        else:
            close_cols = raw_full["Close"] if "Close" in raw_full.columns else raw_full

        def _series(sym):
            try:
                if sym in close_cols.columns:
                    col = close_cols[sym]
                else:
                    # Fallback: individual download for missing symbol
                    single = yf.download(sym, period="2y", interval="1d",
                                         auto_adjust=True, progress=False)
                    col = single["Close"] if "Close" in single.columns else single
                col = col.dropna()
                if len(col) == 0:
                    return [], []
                dates  = [str(d.date()) for d in col.index]
                prices = [round(float(p), 4) if not _math.isnan(float(p)) else None
                          for p in col.values]
                return dates, prices
            except Exception as exc:
                log.warning("Gold ratios _series(%r) failed: %s", sym, exc)
                return [], []

        gold_dates,   gold_prices   = _series("GC=F")
        silver_dates, silver_prices = _series("SI=F")
        copper_dates, copper_prices = _series("HG=F")

        # Build ratio on matching dates (inner join)
        gold_map   = dict(zip(gold_dates,   gold_prices))
        silver_map = dict(zip(silver_dates, silver_prices))
        copper_map = dict(zip(copper_dates, copper_prices))

        all_dates = sorted(set(gold_dates) & set(silver_dates) & set(copper_dates))

        gs_ratios  = []  # gold/silver
        gc_ratios  = []  # gold/copper
        for d in all_dates:
            g, s, c = gold_map.get(d), silver_map.get(d), copper_map.get(d)
            gs_ratios.append(round(g / s, 2) if g and s and s > 0 else None)
            gc_ratios.append(round(g / c, 2) if g and c and c > 0 else None)

        # Current values
        cur_gs = next((v for v in reversed(gs_ratios) if v is not None), None)
        cur_gc = next((v for v in reversed(gc_ratios) if v is not None), None)
        cur_gold   = next((v for v in reversed(gold_prices)   if v is not None), None)
        cur_silver = next((v for v in reversed(silver_prices) if v is not None), None)
        cur_copper = next((v for v in reversed(copper_prices) if v is not None), None)

        # Day change in ratio
        valid_gs = [v for v in gs_ratios if v is not None]
        valid_gc = [v for v in gc_ratios if v is not None]
        gs_chg = round(valid_gs[-1] - valid_gs[-2], 2) if len(valid_gs) >= 2 else None
        gc_chg = round(valid_gc[-1] - valid_gc[-2], 2) if len(valid_gc) >= 2 else None

        # 52-week stats
        gs_52 = [v for v in gs_ratios[-252:] if v is not None]
        gc_52 = [v for v in gc_ratios[-252:] if v is not None]

        result = {
            "dates":         all_dates,
            "gs_ratio":      gs_ratios,   # gold/silver
            "gc_ratio":      gc_ratios,   # gold/copper
            "current_gs":    cur_gs,
            "current_gc":    cur_gc,
            "gs_day_chg":    gs_chg,
            "gc_day_chg":    gc_chg,
            "gs_52wk_hi":    round(max(gs_52), 2) if gs_52 else None,
            "gs_52wk_lo":    round(min(gs_52), 2) if gs_52 else None,
            "gc_52wk_hi":    round(max(gc_52), 2) if gc_52 else None,
            "gc_52wk_lo":    round(min(gc_52), 2) if gc_52 else None,
            "gold_price":    cur_gold,
            "silver_price":  cur_silver,
            "copper_price":  cur_copper,
        }
        ts = time.time()
        with _GOLD_RATIOS_CACHE_LOCK:
            _GOLD_RATIOS_CACHE["data"] = {"ts": ts, "data": result}
        return jsonify(result)

    except Exception as exc:
        log.warning("Gold ratios fetch failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Treasury Yield Curve  (/api/yield-curve)
# ---------------------------------------------------------------------------

_YIELD_CURVE_CACHE: dict = {}
_YIELD_CURVE_CACHE_LOCK = threading.Lock()
_YIELD_CURVE_CACHE_TTL  = 3600  # 1 hour
_YIELD_CURVE_CACHE_VER  = "v4"  # bump when maturity keys change
_YIELD_CURVE_FILE       = Path(__file__).parent.parent / "cache" / "yield_curve_cache.json"


def _yield_curve_load_disk() -> Optional[dict]:
    """Load cached yield curve result from disk if younger than TTL."""
    try:
        if _YIELD_CURVE_FILE.exists():
            payload = json.loads(_YIELD_CURVE_FILE.read_text())
            if payload.get("ver") == _YIELD_CURVE_CACHE_VER and \
               time.time() - payload.get("ts", 0) < _YIELD_CURVE_CACHE_TTL:
                log.info("Yield curve: loaded from disk cache")
                return payload["data"]
    except Exception as exc:
        log.debug("Yield curve disk load failed: %s", exc)
    return None


def _yield_curve_save_disk(result: dict) -> None:
    """Persist yield curve result to disk cache in a background thread."""
    def _write():
        try:
            _YIELD_CURVE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _YIELD_CURVE_FILE.write_text(json.dumps(
                {"ver": _YIELD_CURVE_CACHE_VER, "ts": time.time(), "data": result},
                default=str,
            ))
        except Exception as exc:
            log.debug("Yield curve disk save failed: %s", exc)
    threading.Thread(target=_write, daemon=True).start()


_CN10Y_HISTORY_FILE = Path(__file__).parent.parent / "cache" / "cn_10y_history.json"


def _cn10y_history(value: Optional[float]) -> dict:
    """
    Maintain a forward-accumulating daily series of the CN 10Y government yield.

    There is no queryable free source for Chinese 10Y history:
      * FRED's ``IRLTLT01CNM156N`` (and every OECD/IMF CN long-rate sibling) was
        discontinued and now returns HTTP 404.
      * ChinaBond serves only the *live* curve — its ``workTime`` parameter is
        silently ignored, so every historical date returns today's numbers.

    So we persist one observation per calendar day from the working ChinaBond
    snapshot and let the series build up over time.  Returns the stored series
    oldest→newest, in the same shape as the other ``history_10y`` payloads.
    """
    import datetime as _dt

    store: dict[str, float] = {}
    try:
        if _CN10Y_HISTORY_FILE.exists():
            raw = json.loads(_CN10Y_HISTORY_FILE.read_text())
            for _d, _v in (raw.get("series") or {}).items():
                try:
                    store[str(_d)] = float(_v)
                except (TypeError, ValueError):
                    pass
    except Exception as exc:
        log.debug("CN 10Y history load failed: %s", exc)

    if value is not None:
        today = _dt.date.today().isoformat()
        if store.get(today) != value:
            store[today] = float(value)
            try:
                _CN10Y_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                _tmp = _CN10Y_HISTORY_FILE.with_suffix(".tmp")
                _tmp.write_text(json.dumps({"series": store}))
                _tmp.replace(_CN10Y_HISTORY_FILE)   # atomic
            except Exception as exc:
                log.debug("CN 10Y history save failed: %s", exc)

    ordered = sorted(store.items())
    return {"dates": [d for d, _ in ordered], "values": [v for _, v in ordered]}


@bp.route("/api/etf-returns")
def api_etf_returns():
    """Trailing total returns for the stock/bond ETFs shown beside the ERP card.

    Cached for a day in ``ystocker.etf_returns``: these are five-year windows, so
    an intraday refresh moves the last decimal place and costs a Yahoo call on a
    box that is already rate-limited elsewhere.
    """
    from ystocker import etf_returns

    data = etf_returns.get()
    log.info("API etf-returns (asof=%s, stale=%s)",
             data.get("asof"), bool(data.get("stale")))
    return jsonify(data)


@bp.route("/api/yield-curve")
def api_yield_curve():
    """Return US & CN Treasury yield curve snapshots + historical 10Y comparison."""
    log.info("API yield-curve")
    with _YIELD_CURVE_CACHE_LOCK:
        entry = _YIELD_CURVE_CACHE.get(_YIELD_CURVE_CACHE_VER)
        if entry and time.time() - entry["ts"] < _YIELD_CURVE_CACHE_TTL:
            return jsonify(entry["data"])

    # Try disk cache before hitting external APIs
    disk_data = _yield_curve_load_disk()
    if disk_data is not None:
        with _YIELD_CURVE_CACHE_LOCK:
            _YIELD_CURVE_CACHE[_YIELD_CURVE_CACHE_VER] = {"ts": time.time(), "data": disk_data}
        return jsonify(disk_data)

    import yfinance as yf
    import pandas as _pd
    import math as _math
    import datetime as _dt_mod
    import xml.etree.ElementTree as _ET
    import requests as _req

    # ── US snapshot: US Treasury XML (all maturities) ────────────────────────
    US_MAT_MAP = {
        "BC_3MONTH":  "3M",  "BC_6MONTH": "6M",  "BC_1YEAR":  "1Y",
        "BC_2YEAR":   "2Y",  "BC_3YEAR":  "3Y",  "BC_5YEAR":  "5Y",
        "BC_10YEAR":  "10Y", "BC_20YEAR": "20Y", "BC_30YEAR": "30Y",
    }
    us_current: dict = {}
    try:
        # The month filter param is `field_tdr_date_value_month=YYYYMM`.  Using
        # `field_tdr_date_value=YYYYMM` matches no *year* and returns an empty feed.
        # Try the current month first, then fall back to the previous month so the
        # first days of a month (before the first publication) still resolve.
        _today = _dt_mod.date.today()
        _prev  = (_today.replace(day=1) - _dt_mod.timedelta(days=1))
        for _ym in (_today.strftime("%Y%m"), _prev.strftime("%Y%m")):
            treas_url = (
                "https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/pages/xml?data=daily_treasury_yield_curve"
                f"&field_tdr_date_value_month={_ym}"
            )
            # NOTE: treasury.gov responds in ~0.1s to a browser UA but takes ~8s
            # with the default requests UA — keep the browser UA here.
            tr = _req.get(treas_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if tr.status_code != 200:
                continue
            # `<m:properties>` lives in the *metadata* namespace; only the
            # `BC_*` value elements live in the plain dataservices namespace.
            m_ns = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
            d_ns = "http://schemas.microsoft.com/ado/2007/08/dataservices"
            root = _ET.fromstring(tr.content)
            # Merge ALL entries oldest→newest so the most recent available value
            # for each maturity wins.  This handles partial same-day publications
            # (e.g. treasury publishes 3M/10Y/30Y first, then the rest an hour later)
            # without discarding data from the previous complete day.
            all_entries_merged: dict = {}
            for props in root.iter(f"{{{m_ns}}}properties"):
                entry_vals = {}
                for tag, label in US_MAT_MAP.items():
                    el = props.find(f"{{{d_ns}}}{tag}")
                    if el is not None and el.text:
                        try:
                            entry_vals[label] = round(float(el.text), 3)
                        except ValueError:
                            pass
                if entry_vals:
                    all_entries_merged.update(entry_vals)  # newer entry wins per maturity
            if all_entries_merged:
                us_current = all_entries_merged
                log.info("Yield curve: US Treasury data fetched (%d maturities: %s)",
                         len(us_current), ", ".join(sorted(us_current.keys())))
                break
            log.info("Yield curve: US Treasury feed for %s was empty — trying previous month", _ym)
    except Exception as exc:
        log.warning("Yield curve: US Treasury XML failed: %s", exc)

    # ── US fallback: yfinance for 3M/5Y/10Y/30Y + FRED for 6M/12M/3Y ────────
    us_hist_10y: dict = {"dates": [], "values": []}
    # yfinance tickers that map to standard Treasury maturities
    YF_TICKERS = {"^IRX": "3M", "^FVX": "5Y", "^TNX": "10Y", "^TYX": "30Y"}
    try:
        raw_yf = yf.download(
            list(YF_TICKERS.keys()), period="3y", interval="1d",
            auto_adjust=True, progress=False, group_by="ticker",
        )
        # Extract latest close for each ticker → fill missing maturities
        for ticker, mat in YF_TICKERS.items():
            try:
                if hasattr(raw_yf.columns, "levels"):
                    col = raw_yf[ticker]["Close"].dropna()
                else:
                    col = raw_yf["Close"].dropna()
                if len(col) and mat not in us_current:
                    us_current[mat] = round(float(col.iloc[-1]), 3)
                # Build 10Y history for the existing historical chart
                if mat == "10Y":
                    us_hist_10y = {
                        "dates":  [str(d.date()) for d in col.index],
                        "values": [round(float(v), 3) if not _math.isnan(float(v)) else None
                                   for v in col.values],
                    }
            except Exception as _exc:
                log.debug("Yield curve: yfinance %s failed: %s", ticker, _exc)
    except Exception as exc:
        log.warning("Yield curve: yfinance batch failed: %s", exc)
        # Fallback: try ^TNX alone for 10Y history
        try:
            raw = yf.download("^TNX", period="3y", interval="1d",
                              auto_adjust=True, progress=False)
            col = (raw["Close"] if "Close" in raw.columns else raw).squeeze().dropna()
            us_hist_10y = {
                "dates":  [str(d.date()) for d in col.index],
                "values": [round(float(v), 3) if not _math.isnan(float(v)) else None
                           for v in col.values],
            }
            if "10Y" not in us_current and len(col):
                us_current["10Y"] = round(float(col.iloc[-1]), 3)
        except Exception as exc2:
            log.warning("Yield curve: ^TNX fallback failed: %s", exc2)

    # FRED fallback for any maturity still missing (covers all 7)
    FRED_IDS = {
        "3M":  "DGS3MO", "6M": "DGS6MO", "1Y":  "DGS1",
        "2Y":  "DGS2",   "3Y": "DGS3",   "5Y":  "DGS5",
        "10Y": "DGS10",  "20Y": "DGS20", "30Y": "DGS30",
    }
    missing = [m for m in FRED_IDS if m not in us_current]
    if missing:
        log.info("Yield curve: missing after Treasury+yfinance: %s — trying FRED", ", ".join(missing))
        # Best fallback: try FRED (last 10 observations to handle weekends/holidays)
        # We use a session for better connection handling
        import requests as _req_mod
        session = _req_mod.Session()
        session.trust_env = False
        # Plain client UA — a spoofed browser UA gets blackholed by FRED's Akamai
        # bot detection (see FRED_USER_AGENT in fed.py).
        session.headers.update({"User-Agent": _FRED_UA, "Accept": "text/csv,*/*"})
        
        for mat in missing:
            fred_id = FRED_IDS[mat]
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}"
            try:
                log.info("Yield curve: fetching %s from FRED...", mat)
                fr = session.get(url, timeout=20)
                if fr.status_code == 200:
                    valid_lines = [l for l in fr.text.strip().splitlines()[1:]
                                   if len(l.split(",")) == 2 and l.split(",")[1].strip() not in (".", "")]
                    if valid_lines:
                        us_current[mat] = round(float(valid_lines[-1].split(",")[1].strip()), 3)
                        log.info("Yield curve: FRED CSV filled %s = %s", mat, us_current[mat])
                        import time as _time_mod
                        _time_mod.sleep(0.5) # Be polite
                        continue
            except Exception as exc:
                log.warning("Yield curve: FRED CSV %s failed (%s). URL: %s", mat, exc, url)

            # NOTE: a FRED JSON API fallback used to live here, keyed with
            # `api_key=DEMO_KEY`.  FRED requires a real 32-char lower-case
            # alphanumeric key and rejects that placeholder with HTTP 400, so the
            # block could never contribute data — it only ever burned the request
            # timeout.  Removed; add it back only alongside a real FRED_API_KEY.


    # ── Stale-cache safety net: fill remaining gaps from previous successful fetch ─
    # This is a last resort — if Treasury XML, yfinance, AND FRED all failed for a
    # maturity (network outage, holiday data lag, etc.) use the last cached value so
    # the yield curve never renders with holes.
    _expected_maturities = set(US_MAT_MAP.values())
    _still_missing = _expected_maturities - set(us_current.keys())
    if _still_missing:
        log.info("Yield curve: %d maturities still missing (%s) — trying stale disk cache",
                 len(_still_missing), ", ".join(sorted(_still_missing)))
        try:
            if _YIELD_CURVE_FILE.exists():
                _stale_payload = json.loads(_YIELD_CURVE_FILE.read_text())
                if _stale_payload.get("ver") == _YIELD_CURVE_CACHE_VER:
                    _stale_us = _stale_payload.get("data", {}).get("us", {}).get("current", {})
                    for _mat in list(_still_missing):
                        if _mat in _stale_us:
                            us_current[_mat] = _stale_us[_mat]
                            log.info("Yield curve: filled %s = %.3f%% from stale cache",
                                     _mat, _stale_us[_mat])
        except Exception as _exc:
            log.debug("Yield curve stale-cache fill failed: %s", _exc)

    spread_10y_3m = None
    if "10Y" in us_current and "3M" in us_current:
        spread_10y_3m = round(us_current["10Y"] - us_current["3M"], 3)

    # ── CN snapshot: ChinaBond Government Bond YTM curve (JSON API) ─────────
    # Endpoint returns a continuous [maturity_years, yield] curve.
    # Curve ID 2c9081e50a2f9606010a3068cae70001 = "ChinaBond Government Bond YTM"
    CN_MAT_LABELS = ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "30Y"]
    CN_MAT_YEARS  = [0.25, 0.5,  1.0,  2.0,  3.0,  5.0,  7.0,  10.0,  30.0]
    cn_current: dict = {}
    try:
        import json as _json
        today_str = _dt_mod.date.today().strftime("%Y-%m-%d")
        cb_url = (
            "https://yield.chinabond.com.cn/cbweb-mn/yc/inityc"
            f"?locale=en_US&workTime={today_str}"
            "&ycDefIds=2c9081e50a2f9606010a3068cae70001"
        )
        cb_resp = _req.post(cb_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if cb_resp.status_code == 200:
            cb_data = _json.loads(cb_resp.text)
            series_data = cb_data[1][1][0]["seriesData"]
            for label, target_yr in zip(CN_MAT_LABELS, CN_MAT_YEARS):
                closest = min(series_data, key=lambda p: abs(p[0] - target_yr))
                if abs(closest[0] - target_yr) < 0.15:
                    cn_current[label] = round(closest[1], 3)
            log.info("Yield curve: CN ChinaBond data fetched (%d points)", len(cn_current))
    except Exception as exc:
        log.warning("Yield curve: ChinaBond fetch failed: %s", exc)

    # ── CN historical 10Y: locally accumulated daily series ─────────────────
    # The old FRED series (IRLTLT01CNM156N) was discontinued and returns 404, and
    # ChinaBond cannot be queried for past dates — see _cn10y_history() for detail.
    cn_current_10y = cn_current.get("10Y")
    cn_hist_10y = _cn10y_history(cn_current_10y)

    # ── JP snapshot: Ministry of Finance Japan JGB yield curve ──────────────
    # The old `/english/jgbs/...` path 404s — it moved under `/english/policy/`.
    # `jgbcme.csv` carries the *current month*; the full daily series since 1974
    # lives at `historical/jgbcme_all.csv`, used here only as a fallback for the
    # first days of a month before the new file is published.
    # MoF publishes 15 tenors: 1Y–10Y in yearly steps, then 15/20/25/30/40Y.  We keep
    # the conventional JGB benchmark ladder — enough resolution to show both the
    # BoJ-anchored short end and the steep super-long end without crowding the card.
    # Column headers in the CSV are exactly these labels, so no label→column map.
    JP_MATURITIES = ["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y", "40Y"]
    _MOF_BASE = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate"
    jp_current: dict = {}

    def _parse_mof_csv(text: str) -> dict:
        """Return the newest complete row of a MoF JGB CSV as {label: yield}."""
        lines = text.strip().splitlines()
        # Row 0 is a title ("Interest Rate (August 2026)"); the header is row 1.
        hdr_idx = next((i for i, ln in enumerate(lines[:6])
                        if ln.split(",")[0].strip().strip('"').lower() == "date"), None)
        if hdr_idx is None:
            return {}
        header = [h.strip() for h in lines[hdr_idx].split(",")]
        # Walk newest→oldest; trailing lines are a blank row and a footer note.
        for ln in reversed(lines[hdr_idx + 1:]):
            parts = [p.strip() for p in ln.split(",")]
            if not parts or "/" not in parts[0]:
                continue
            vals: dict = {}
            for label in JP_MATURITIES:
                try:
                    raw = parts[header.index(label)]
                except (ValueError, IndexError):
                    continue
                if raw and raw not in ("-", "N/A"):
                    try:
                        vals[label] = round(float(raw), 3)
                    except ValueError:
                        pass
            if vals:
                return vals
        return {}

    try:
        for _mof_path in ("jgbcme.csv", "historical/jgbcme_all.csv"):
            mof_r = _req.get(f"{_MOF_BASE}/{_mof_path}", timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            if mof_r.status_code != 200:
                # Log it — a silent `continue` here is what previously made the
                # curve collapse to a lone FRED 10Y with no trace of the cause.
                log.warning("Yield curve: JP MoF %s returned HTTP %s",
                            _mof_path, mof_r.status_code)
                continue
            jp_current = _parse_mof_csv(mof_r.text)
            if jp_current:
                break
            log.warning("Yield curve: JP MoF %s parsed 0 maturities (%d bytes, "
                        "first line: %.60s)", _mof_path, len(mof_r.content),
                        mof_r.text.strip().splitlines()[0] if mof_r.text.strip() else "")
        log.info("Yield curve: JP MoF fetched (%d maturities: %s)",
                 len(jp_current), ", ".join(sorted(jp_current.keys())))
    except Exception as exc:
        log.warning("Yield curve: JP MoF fetch failed: %s", exc)

    # ── JP stale-cache safety net (mirrors the US path above) ────────────────
    # MoF is the only source for the full JGB term structure, so a single flaky
    # fetch used to drop the card to one point (the FRED monthly 10Y below).
    # Re-use the last good snapshot for whatever is missing instead.
    _jp_missing = [m for m in JP_MATURITIES if m not in jp_current]
    if _jp_missing:
        log.info("Yield curve: JP missing %d maturities (%s) — trying stale disk cache",
                 len(_jp_missing), ", ".join(_jp_missing))
        try:
            if _YIELD_CURVE_FILE.exists():
                _stale_payload = json.loads(_YIELD_CURVE_FILE.read_text())
                if _stale_payload.get("ver") == _YIELD_CURVE_CACHE_VER:
                    _stale_jp = _stale_payload.get("data", {}).get("jp", {}).get("current", {})
                    for _mat in _jp_missing:
                        if _mat in _stale_jp:
                            jp_current[_mat] = _stale_jp[_mat]
                            log.info("Yield curve: JP filled %s = %.3f%% from stale cache",
                                     _mat, _stale_jp[_mat])
        except Exception as _exc:
            log.debug("Yield curve JP stale-cache fill failed: %s", _exc)

    jp_hist_10y: dict = {"dates": [], "values": []}
    try:
        jp_fred = _req.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=IRLTLT01JPM156N",
            timeout=10, headers={"User-Agent": _FRED_UA},
        )
        if jp_fred.status_code == 200:
            dates, vals = [], []
            for line in jp_fred.text.strip().splitlines()[1:]:
                parts = line.split(",")
                if len(parts) == 2 and parts[1].strip() not in (".", ""):
                    try:
                        dates.append(parts[0].strip())
                        vals.append(round(float(parts[1].strip()), 3))
                    except ValueError:
                        pass
            if dates:
                jp_hist_10y = {"dates": dates, "values": vals}
                if "10Y" not in jp_current:
                    jp_current["10Y"] = vals[-1]
    except Exception as exc:
        log.warning("Yield curve: JP FRED fetch failed: %s", exc)

    result = {
        "us": {
            "current":       us_current,
            "history_10y":   us_hist_10y,
            "spread_10y_3m": spread_10y_3m,
        },
        "cn": {
            "current":     cn_current,
            "history_10y": cn_hist_10y,
        },
        "jp": {
            "current":     jp_current,
            "history_10y": jp_hist_10y,
        },
    }

    # ── S&P 500 P/E for ERP (Equity Risk Premium) calculation ────────────────
    # Yahoo Finance doesn't reliably return trailingPE for the ^GSPC index
    # ticker, but consistently returns it for SPY (the tracking ETF).
    spx_pe = None
    try:
        _spy_info = yf.Ticker("SPY").info
        spx_pe = _spy_info.get("trailingPE") or _spy_info.get("forwardPE")
        if spx_pe:
            spx_pe = round(float(spx_pe), 1)
    except Exception as exc:
        log.debug("Yield curve: SPY PE fetch failed: %s", exc)
    result["spx_pe"] = spx_pe

    with _YIELD_CURVE_CACHE_LOCK:
        _YIELD_CURVE_CACHE[_YIELD_CURVE_CACHE_VER] = {"ts": time.time(), "data": result}
    _yield_curve_save_disk(result)

    return jsonify(result)


# ---------------------------------------------------------------------------
# Yield Spread  (/api/yield-spread)
# ---------------------------------------------------------------------------

# 4-hour cache for yield spread data
_YIELD_SPREAD_CACHE: dict = {}
_YIELD_SPREAD_LOCK = threading.Lock()
_YIELD_SPREAD_TTL = 4 * 3600
_YIELD_SPREAD_FETCH_EVENT = threading.Event()
_YIELD_SPREAD_FETCH_EVENT.set()  # Initially set (meaning "not fetching")


def _yield_spread_window(payload: dict, full: bool) -> dict:
    """Window a full-history spread payload for the requesting page.

    full=False (default) — the trailing 10 years at daily resolution. /markets
    only offers 2Y/5Y/10Y buttons and slices client-side, so it keeps exactly the
    payload it always got.

    full=True — the entire history (DGS2 starts 1976-06) downsampled to one
    observation per month. This is for the /fed page's "Consumer Sentiment vs
    Yield Curve" overlay, which aligns the spread onto monthly UMCSENT dates by
    YYYY-MM key: sending all ~12.5k daily points would be a 440 KB response that
    the client immediately collapses to ~600 monthly values.
    """
    dates     = payload.get("dates", [])
    spread    = payload.get("spread", [])
    recession = payload.get("recession", [])

    if full:
        # Keep the last observation of each month (dates are ascending).
        keep = [i for i, d in enumerate(dates)
                if i + 1 == len(dates) or dates[i + 1][:7] != d[:7]]
        return {
            **payload,
            "dates":     [dates[i] for i in keep],
            "spread":    [spread[i] for i in keep],
            "recession": [recession[i] for i in keep],
        }

    import datetime as _dt

    cutoff = (_dt.date.today() - _dt.timedelta(days=365 * 10)).isoformat()
    start  = next((i for i, d in enumerate(dates) if d >= cutoff), len(dates))
    return {
        **payload,
        "dates":     dates[start:],
        "spread":    spread[start:],
        "recession": recession[start:],
    }


@bp.route("/api/yield-spread")
def api_yield_spread():
    """10Y-2Y Treasury spread + NBER recession bands.

    Query params:
      full: "1" to return the entire history (from 1976-06, where DGS2 begins)
            downsampled to monthly, instead of the default trailing 10-year
            window at daily resolution. See _yield_spread_window().

    Returns:
      dates: list of YYYY-MM-DD
      spread: list of floats (10Y - 2Y, percentage points)
      recession: list of 0/1 (NBER recession indicator)
    """
    full = request.args.get("full") == "1"

    # 1. Check cache first
    with _YIELD_SPREAD_LOCK:
        entry = _YIELD_SPREAD_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _YIELD_SPREAD_TTL:
            return jsonify(_yield_spread_window(entry["data"], full))

    # 2. Cache is empty or stale. Deduplicate concurrent fetches.
    # If an event is NOT set, another thread is already fetching.
    is_first_fetcher = _YIELD_SPREAD_FETCH_EVENT.is_set()
    if not is_first_fetcher:
        # Wait for the other thread to finish fetching (up to 30s)
        log.info("yield-spread: waiting for concurrent fetch to complete...")
        _YIELD_SPREAD_FETCH_EVENT.wait(timeout=30)
        # Check cache again
        with _YIELD_SPREAD_LOCK:
            entry = _YIELD_SPREAD_CACHE.get("data")
            if entry and time.time() - entry["ts"] < _YIELD_SPREAD_TTL:
                return jsonify(_yield_spread_window(entry["data"], full))
        # If we get here, the other thread failed. Fall through to try fetching.

    # 3. We are the designated fetcher
    _YIELD_SPREAD_FETCH_EVENT.clear()
    try:
        # Hardcoded NBER recession periods (start, end) — more reliable than FRED USREC fetch
        # Updated through 2024; add new recessions here as NBER declares them.
        _NBER_RECESSIONS = [
            ("1980-01-01", "1980-07-31"), ("1981-07-01", "1982-11-30"),
            ("1990-07-01", "1991-03-31"), ("2001-03-01", "2001-11-30"),
            ("2007-12-01", "2009-06-30"), ("2020-02-01", "2020-04-30"),
        ]

        def _in_recession(date_str: str) -> int:
            for start, end in _NBER_RECESSIONS:
                if start <= date_str <= end:
                    return 1
            return 0

        try:
            import requests as _req
            import concurrent.futures as _cf_ys
            headers = {
                # Plain client UA — a spoofed browser UA gets blackholed by FRED's
                # Akamai bot detection (see FRED_USER_AGENT in fed.py).
                "User-Agent": _FRED_UA,
                "Accept": "text/csv,*/*",
            }
            # Use a session to avoid proxy/timeout issues
            session = _req.Session()
            session.trust_env = False
            session.headers.update(headers)

            def _fred(sid):
                url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
                r = session.get(url, timeout=20)
                r.raise_for_status()
                rows = [line.split(",") for line in r.text.strip().splitlines()[1:]
                        if "," in line and line.split(",")[1].strip() not in ("", ".")]
                return sid, {row[0]: float(row[1]) for row in rows}

            # Fetch DGS10 and DGS2 in parallel
            with _cf_ys.ThreadPoolExecutor(max_workers=2) as _pool:
                futs = {sid: _pool.submit(_fred, sid) for sid in ("DGS10", "DGS2")}
                results = {}
                for sid, fut in futs.items():
                    try:
                        _, data = fut.result(timeout=25)
                        results[sid] = data
                    except Exception as exc:
                        raise RuntimeError(f"FRED {sid} failed: {exc}") from exc

            dgs10 = results["DGS10"]
            dgs2  = results["DGS2"]

        except Exception as fred_exc:
            # No usable fallback exists for the 2Y series: yfinance has no reliable
            # free 2Y Treasury ticker, and the yield-curve cache only stores a
            # single latest 2Y point (not the 10-year history this chart needs).
            # Rather than 502 on a transient FRED blip, serve the last good payload
            # even if it is past its TTL, flagged so the UI can label it.
            log.warning("yield-spread: FRED failed (%s) — falling back to stale cache", fred_exc)
            with _YIELD_SPREAD_LOCK:
                stale = _YIELD_SPREAD_CACHE.get("data")
            if stale and stale.get("data"):
                age_h = (time.time() - stale["ts"]) / 3600
                log.info("yield-spread: serving stale cache (%.1fh old)", age_h)
                return jsonify({**_yield_spread_window(stale["data"], full),
                                "stale": True,
                                "stale_age_hours": round(age_h, 1)})
            return jsonify({"error": f"FRED unavailable and no cached data: {fred_exc}"}), 502

        if not dgs10 or not dgs2:
            return jsonify({"error": "No data returned from FRED"}), 502

        # Align on dates present in both DGS10 and DGS2
        all_dates = sorted(set(dgs10) & set(dgs2))
        if not all_dates:
            raise RuntimeError("No overlapping dates between DGS10 and DGS2")

        # Cache the FULL series (1976-06 onward, where DGS2 begins); each response
        # is windowed by _yield_spread_window() so one fetch serves both the
        # default 10-year payload and the /fed page's full-history overlay.
        rows = [(d, round(dgs10[d] - dgs2[d], 3), _in_recession(d)) for d in all_dates]

        if not rows:
            raise RuntimeError("No overlapping DGS10/DGS2 observations")

        dates, spread, recession = zip(*rows)
        result = {"dates": list(dates), "spread": list(spread), "recession": list(recession)}

        with _YIELD_SPREAD_LOCK:
            _YIELD_SPREAD_CACHE["data"] = {"ts": time.time(), "data": result}

        log.info("yield-spread: %d obs (%s … %s)", len(dates), dates[0], dates[-1])
        return jsonify(_yield_spread_window(result, full))

    except Exception as exc:
        log.warning("yield-spread: fetch failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    finally:
        # 4. Always signal that the fetch attempt is complete
        _YIELD_SPREAD_FETCH_EVENT.set()


# ---------------------------------------------------------------------------
# Long-horizon S&P 500 history  (/api/spx-history)
# ---------------------------------------------------------------------------

# 24-hour cache: monthly closes only move once a month, and the /fed page
# overlays that consume this already refresh on the 24 h Fed cache TTL.
_SPX_HISTORY_CACHE: dict = {}
_SPX_HISTORY_LOCK = threading.Lock()
_SPX_HISTORY_TTL = 24 * 3600
# Persisted, because the in-memory cache alone made every restart a cliff: the
# first /fed visitor afterwards triggered a period="max" daily ^GSPC download
# (~24.7k rows), which exceeds gunicorn's 120s timeout and returns 504. The /fed
# macro charts all await this in one Promise.all, so they silently stayed hidden
# and looked like they had stopped updating.
_SPX_HISTORY_PATH = Path(__file__).parent.parent / "cache" / "spx_history.json"


def _spx_history_load_disk() -> Optional[dict]:
    """Last persisted payload, or None. Age is checked by the caller."""
    try:
        raw = json.loads(_SPX_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("data", {}).get("dates"):
            return raw
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("spx-history: unreadable disk cache: %s", exc)
    return None


def _spx_history_save_disk(entry: dict) -> None:
    try:
        _SPX_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_SPX_HISTORY_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, _SPX_HISTORY_PATH)
    except Exception as exc:  # noqa: BLE001 - a cache write failure is not fatal
        log.warning("spx-history: could not persist: %s", exc)


@bp.route("/api/spx-history")
def api_spx_history():
    """Monthly S&P 500 (^GSPC) closes back to 1927, for long-horizon macro overlays.

    The /fed page charts FRED series that begin decades before any tradeable S&P
    product existed (GDPC1 → 1947, UMCSENT → 1952). Two traps make the obvious
    sources unusable there:

      * /api/history/SPY tracks the SPY *ETF*, which only launched in 1993.
      * Yahoo silently caps interval="1mo" responses at 500 rows regardless of the
        requested start date, so a monthly ^GSPC request truncates to ~1985 with
        no error — passing an explicit start= does not lift the cap.

    Fetching daily (period="max" returns ~24.7k rows from 1927-12-30) and
    resampling to month-end server-side is the only way to get the full series.
    Dates land on month ends; every consumer aligns on the YYYY-MM prefix, so
    they interoperate with FRED's first-of-month monthly convention.

    Returns: {"dates": ["YYYY-MM-DD", ...], "prices": [float, ...]}
    """
    import yfinance as yf

    with _SPX_HISTORY_LOCK:
        entry = _SPX_HISTORY_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _SPX_HISTORY_TTL:
            return jsonify(entry["data"])
        if entry is None:
            # Adopt the persisted copy before considering a fetch: a fresh worker
            # must not pay for a full download that another process already made.
            disk = _spx_history_load_disk()
            if disk:
                _SPX_HISTORY_CACHE["data"] = disk
                if time.time() - disk["ts"] < _SPX_HISTORY_TTL:
                    return jsonify(disk["data"])

    try:
        hist = yf.Ticker("^GSPC").history(period="max", interval="1d")
        if hist.empty or "Close" not in hist:
            raise RuntimeError("yfinance returned no ^GSPC rows")

        monthly = hist["Close"].resample("ME").last().dropna()
        if monthly.empty:
            raise RuntimeError("no monthly closes after resampling ^GSPC")

        result = {
            "dates":  [d.strftime("%Y-%m-%d") for d in monthly.index],
            "prices": [round(float(v), 2) for v in monthly.values],
        }
    except Exception as exc:
        log.warning("spx-history: fetch failed: %s", exc)
        # Serve the last good payload rather than blanking the charts on a blip.
        with _SPX_HISTORY_LOCK:
            stale = _SPX_HISTORY_CACHE.get("data")
        if stale and stale.get("data"):
            age_h = (time.time() - stale["ts"]) / 3600
            log.info("spx-history: serving stale cache (%.1fh old)", age_h)
            return jsonify({**stale["data"], "stale": True,
                            "stale_age_hours": round(age_h, 1)})
        return jsonify({"error": str(exc)}), 502

    entry = {"ts": time.time(), "data": result}
    with _SPX_HISTORY_LOCK:
        _SPX_HISTORY_CACHE["data"] = entry
    _spx_history_save_disk(entry)

    log.info("spx-history: %d monthly closes (%s … %s)",
             len(result["dates"]), result["dates"][0], result["dates"][-1])
    return jsonify(result)


# ---------------------------------------------------------------------------
# Markets AI Explain  (/api/markets/explain)
# ---------------------------------------------------------------------------

@bp.route("/api/markets/explain", methods=["POST"])
def api_markets_explain():
    """Stream an AI explanation of a markets chart (yield curve snapshot) via SSE."""
    import os
    from google import genai

    body  = request.get_json(force=True, silent=True) or {}
    chart = body.get("chart", "")
    data  = body.get("data", {})
    lang  = body.get("lang", "en")
    zh    = lang == "zh"
    log.info("API markets/explain: chart=%s lang=%s", chart, lang)

    if not data:
        return jsonify({"error": "No data provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    # Per-chart config. `mats` mirrors the maturity ladder each card renders and
    # `spread` is whatever spread that card displays — JP uses 10Y–1Y because MoF
    # publishes no bill tenors, so there is no 3M point to subtract.
    _YIELD_CHARTS = {
        "usYield": {
            "country": "US Treasury",
            "mats": ["3M", "6M", "12M", "3Y", "5Y", "10Y", "30Y"],
            "spread_label": "10Y–3M",
            "extra": "",
        },
        "cnYield": {
            "country": "China Government Bond (CGBs)",
            "mats": ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "30Y"],
            "spread_label": "10Y–3M",
            "extra": "",
        },
        "jpYield": {
            "country": "Japanese Government Bond (JGBs)",
            "mats": ["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y", "40Y"],
            "spread_label": "10Y–1Y",
            "extra": (" Because this is the JGB curve, also address (a) how the Bank of "
                      "Japan's yield-curve-control legacy and policy-normalisation path "
                      "anchor the short end, (b) what the super-long end (20Y/30Y/40Y) "
                      "implies about inflation expectations and fiscal risk premia, and "
                      "(c) the read-across for the yen and JPY-funded carry trades."),
        },
    }

    cfg = _YIELD_CHARTS.get(chart)
    if cfg is None:
        return jsonify({"error": f"Unknown chart: {chart}"}), 400

    current  = data.get("current", {})
    spread   = data.get("spread")
    inverted = isinstance(spread, (int, float)) and spread < 0
    # Guard on type, not just presence: a missing tenor can arrive as null and
    # f"{None:.3f}" would raise, turning a partial curve into a 500.
    yield_lines = "\n".join(
        f"  {m}: {current[m]:.3f}%"
        for m in cfg["mats"] if isinstance(current.get(m), (int, float))
    )
    if not yield_lines:
        return jsonify({"error": "No yield data provided"}), 400
    spread_line = (f"\n{cfg['spread_label']} Spread: {spread:+.3f}% "
                   f"({'INVERTED' if inverted else 'Normal'})"
                   if isinstance(spread, (int, float)) else "")
    prompt = f"""You are a macroeconomic analyst. Analyze the current {cfg['country']} yield curve snapshot for a financial market participant in 3–4 concise paragraphs.{"  Respond in Simplified Chinese (中文)." if zh else ""}

Current yields by maturity:
{yield_lines}{spread_line}

Cover: (1) the curve shape — normal, flat, or inverted — and what it signals, (2) notable features such as where the curve peaks or humps, (3) monetary policy and growth expectations implied by this shape.{cfg['extra']} Be specific about the numbers. Do not use headers or bullet points."""

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
            log.error("Markets explain error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


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
    log.info("API aaii-sentiment")
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
        # Auto-detect XLS vs XLSX by magic bytes (PK = ZIP = xlsx)
        _engine = "openpyxl" if resp.content[:2] == b'PK' else "xlrd"
        df = pd.read_excel(io.BytesIO(resp.content), header=3, engine=_engine)

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
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.4 Safari/605.1.15"
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
        # Composite key (date, event_id) — collapse repeats so one duplicated
        # pair cannot fail the whole batch.
        with table.batch_writer(overwrite_by_pkeys=["date", "event_id"]) as batch:
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
    log.info("API economic-events")
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
    log.info("API economic-events/translate: %d events", len(events_to_translate))
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
        from google.genai import types as _genai_types
        client = genai.Client(api_key=GEMINI_API_KEY,
                              http_options=_genai_types.HttpOptions(timeout=30))
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
# Top Movers  (/api/movers)
# ---------------------------------------------------------------------------

_MOVERS_CACHE: dict = {}
_MOVERS_CACHE_LOCK = threading.Lock()
_MOVERS_CACHE_TTL  = 300   # 5 minutes

# Curated list of large-cap US stocks to scan for movers
_MOVER_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "BRK-B",
    "AVGO", "JPM", "LLY", "V", "UNH", "XOM", "WMT", "MA", "HD", "ORCL",
    "COST", "PG", "JNJ", "ABBV", "BAC", "MRK", "CVX", "AMD", "NFLX",
    "KO", "PEP", "ADBE", "CRM", "QCOM", "TXN", "TMO", "INTC", "GE",
    "DIS", "CSCO", "VZ", "T", "PM", "NKE", "BMY", "MCD", "IBM",
    "SBUX", "GS", "MS", "WFC", "C",
]

_MOVER_SECTOR: dict[str, str] = {
    "AAPL":"Tech","MSFT":"Tech","GOOGL":"Tech","META":"Tech","NVDA":"Tech",
    "AMZN":"Retail","TSLA":"EV","NFLX":"Media","ORCL":"Tech","CRM":"Tech",
    "JPM":"Finance","BAC":"Finance","GS":"Finance","V":"Finance","MA":"Finance",
    "UNH":"Health","JNJ":"Health","LLY":"Health","PFE":"Health","ABBV":"Health",
    "XOM":"Energy","CVX":"Energy","COP":"Energy","SLB":"Energy",
    "WMT":"Retail","COST":"Retail","HD":"Retail","TGT":"Retail",
    "BA":"Defense","RTX":"Defense","LMT":"Defense","CAT":"Industrial",
    "NEE":"Utility","DUK":"Utility","SO":"Utility",
    "AMT":"REIT","PLD":"REIT","EQIX":"REIT",
    "GLD":"Metals","SLV":"Metals","GC=F":"Metals","CL=F":"Energy",
    "SPY":"ETF","QQQ":"ETF","IWM":"ETF","DIA":"ETF",
    "BTC-USD":"Crypto","ETH-USD":"Crypto",
}


@bp.route("/api/movers")
def api_movers():
    """Return top 5 gainers and losers among major US large-cap stocks."""
    import yfinance as yf
    log.info("API movers")

    with _MOVERS_CACHE_LOCK:
        entry = _MOVERS_CACHE.get("data")
        if entry and time.time() - entry["ts"] < _MOVERS_CACHE_TTL:
            return jsonify(entry["data"])

    try:
        tickers_str = " ".join(_MOVER_TICKERS)
        raw = yf.download(tickers_str, period="2d", interval="1d",
                          auto_adjust=True, progress=False)["Close"]

        # Fetch 22 days for relative volume calculation
        rel_vol_map: dict = {}
        try:
            raw_22d = yf.download(tickers_str, period="22d", interval="1d",
                                  auto_adjust=True, progress=False)
            vol_data = raw_22d["Volume"] if "Volume" in raw_22d else None
            if vol_data is not None:
                for sym in _MOVER_TICKERS:
                    try:
                        vs = vol_data[sym].dropna() if sym in vol_data.columns else vol_data.dropna()
                        avg_vol = float(vs.iloc[:-1].mean()) if len(vs) > 1 else None
                        last_vol = float(vs.iloc[-1]) if len(vs) > 0 else None
                        rel_vol_map[sym] = round(last_vol / avg_vol, 1) if (avg_vol and avg_vol > 0 and last_vol) else None
                    except Exception:
                        rel_vol_map[sym] = None
        except Exception:
            pass

        movers = []
        for sym in _MOVER_TICKERS:
            try:
                col = raw[sym] if sym in raw.columns else raw.get(sym)
                if col is None:
                    continue
                vals = col.dropna().tolist()
                if len(vals) < 2:
                    continue
                prev, curr = vals[-2], vals[-1]
                if prev <= 0:
                    continue
                chg = round((curr - prev) / prev * 100, 2)
                movers.append({"ticker": sym, "price": round(curr, 2), "day_chg": chg,
                                "rel_vol": rel_vol_map.get(sym), "sector": _MOVER_SECTOR.get(sym, "")})
            except Exception:
                pass

        movers.sort(key=lambda x: x["day_chg"])
        result = {
            "losers":  movers[:5],
            "gainers": movers[-5:][::-1],
        }
    except Exception as exc:
        log.warning("Movers fetch failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    ts = time.time()
    with _MOVERS_CACHE_LOCK:
        _MOVERS_CACHE["data"] = {"ts": ts, "data": result}
    return jsonify(result)


# ---------------------------------------------------------------------------
# AI Markets Brief  (/api/market-brief)
# ---------------------------------------------------------------------------
# The long-form brief shown on /markets. It is deliberately a separate endpoint
# from /api/daily-summary rather than a mode of it: that one still feeds the
# /daily page and the subscriber email, and the email builder splits its text on
# blank lines into <p> tags, so a brief containing Markdown tables would arrive
# as literal pipes in somebody's inbox. Two consumers, two shapes, two routes.
#
# Formatting lives in ystocker/brief.py; this section only collects the data,
# because the /markets and /commodities payloads live in this module's private
# caches.

_BRIEF_CACHE: dict = {}
_BRIEF_CACHE_LOCK = threading.Lock()
_BRIEF_CACHE_TTL  = 1800   # 30 minutes

# Stored in the same DynamoDB table as the daily summaries, partitioned by a
# suffix on lang_market. Versioned because the brief's shape is part of what is
# cached: bump this and a stale short summary written by an older build stops
# being served as though it were a brief.
_BRIEF_KEY_VER = "brief_v1"

# Ceiling on the Gemini call, in milliseconds. Chosen against gunicorn's
# --timeout 120 (deploy/cloudformation.yaml): the generation is normally 20-40s,
# so 90s leaves headroom for a slow response while still failing before the
# worker is killed. Observed generations run ~14k chars of prompt to ~7k of
# Markdown; if the brief grows much past that, re-measure before raising this.
_BRIEF_GEMINI_TIMEOUT_MS = 90_000


def _brief_evaluation_summary() -> Optional[dict]:
    """Sector-valuation medians for the brief, from the peer-group cache.

    /evaluation renders per-ticker scatter data; a brief cannot use 500 rows, so
    this reduces each peer group to its medians and picks the few individual
    names worth naming. Returns None when the ticker cache is still cold.
    """
    data = _get_data()
    if not data:
        return None

    sectors: list[dict] = []
    all_rows: list[dict] = []
    seen: set[str] = set()
    for group, raw in data.items():
        try:
            df = _raw_to_df(raw)
        except Exception as exc:  # noqa: BLE001 - one bad group must not kill the brief
            log.warning("Brief: peer group %r could not build a DataFrame: %s", group, exc)
            continue
        if df.empty:
            continue

        def _median(col: str) -> Optional[float]:
            if col not in df.columns:
                return None
            ser = pd.to_numeric(df[col], errors="coerce").dropna()
            return round(float(ser.median()), 3) if not ser.empty else None

        sectors.append({
            "sector":            group,
            "count":             int(len(df)),
            "median_pe_ttm":     _median("PE (TTM)"),
            "median_pe_fwd":     _median("PE (Forward)"),
            "median_peg":        _median("PEG"),
            "median_ev_ebitda":  _median("EV/EBITDA"),
            "median_upside":     _median("Upside (%)"),
            # Median, not mean, and named so: a peer group of 18 semis with one
            # 20% mover has a mean day change that describes no member of it.
            "median_day_change": _median("Day Change (%)"),
        })

        for ticker, row in df.iterrows():
            if ticker in seen:
                continue
            seen.add(str(ticker))
            all_rows.append({
                "ticker":     str(ticker),
                "sector":     group,
                "pe_fwd":     _safe(row.get("PE (Forward)")),
                "upside":     _safe(row.get("Upside (%)")),
                "market_cap": _safe(row.get("Market Cap ($B)")),
            })

    if not sectors:
        return None

    sectors.sort(key=lambda s: s.get("median_pe_fwd") or 0, reverse=True)

    def _top(key: str, reverse: bool, limit: int = 5) -> list[dict]:
        rows = [r for r in all_rows if isinstance(r.get(key), (int, float))]
        # Forward P/E below zero is a loss-maker, not a cheap stock; ranking on
        # it would put the most distressed names at the top of "cheapest".
        if key == "pe_fwd":
            rows = [r for r in rows if r[key] > 0]
        rows.sort(key=lambda r: r[key], reverse=reverse)
        return rows[:limit]

    return {
        "sectors":        sectors,
        "most_expensive": _top("pe_fwd", True),
        "cheapest":       _top("pe_fwd", False),
        "most_upside":    _top("upside", True),
    }


def _brief_13f() -> tuple[Optional[dict], Optional[list]]:
    """Latest 13F holdings plus the consensus positions across funds.

    Mirrors the consensus computation in the /13f view rather than importing it,
    since that one is entangled with rendering the page.

    Returns ``(None, None)`` when no fund has usable holdings — the cache is a
    dict of per-fund results, and a run where every SEC fetch failed still
    produces a populated-looking dict of ``{"error": ...}`` entries. Returning
    it would report the source as used while the section renders unavailable.
    """
    try:
        from ystocker.sec13f import get_all_holdings
        holdings = get_all_holdings()
    except Exception as exc:  # noqa: BLE001
        log.warning("Brief: 13F holdings unavailable: %s", exc)
        return None, None
    if not holdings:
        return None, None

    from collections import defaultdict

    ticker_funds: dict[str, list] = defaultdict(list)
    ticker_value: dict[str, float] = defaultdict(float)
    live = 0
    for fund_name, fd in holdings.items():
        if not isinstance(fd, dict) or fd.get("error") or not fd.get("holdings"):
            continue
        live += 1
        for h in fd.get("holdings", []):
            t = h.get("ticker")
            if not t:
                continue
            ticker_funds[t].append(fund_name)
            ticker_value[t] += h.get("value_millions", 0) or 0
    if not live:
        errored = sum(1 for fd in holdings.values()
                      if isinstance(fd, dict) and fd.get("error"))
        log.info("Brief: 13F has %d funds but none usable (%d errored) — "
                 "section marked unavailable", len(holdings), errored)
        return None, None

    consensus = sorted(
        [
            {"ticker": t, "fund_count": len(names),
             "total_value_m": round(ticker_value[t]), "fund_names": names}
            for t, names in ticker_funds.items() if len(names) >= 2
        ],
        key=lambda x: -x["fund_count"],
    )[:25]
    return holdings, consensus


def _collect_brief_sources(warm: bool = False, app=None) -> dict:
    """Gather every dashboard's data for the brief.

    ``warm=False`` (the request path) peeks caches and never rebuilds. This is
    not politeness: api_markets() on a cold cache refetches ~30 symbols with a
    year of history each, which measured 120s and got the worker SIGKILLed by
    gunicorn's --timeout — see the note on _tv_markets_cached. A section the
    model is told is unavailable costs a sentence; a dead worker costs the site.

    ``warm=True`` is for the overnight pre-generator, which runs in a background
    thread where a slow fetch harms nobody. Eight of these caches have no
    warm-up thread of their own and are only ever filled by a browser hitting
    the page, so without this the first brief of a fresh process would be built
    from half a snapshot. ``app`` is required for that path: these are Flask
    views, and calling one outside an app context raises before it can return —
    the same reason _start_markets_warmup_thread holds a test_request_context.
    """
    # breadth is imported at module level; the rest are local to keep import
    # time down, matching how the other routes reach these modules.
    from ystocker import fed as fed_mod, fedwatch, housing, valuation

    src: dict[str, Any] = {}
    # Sources served past their TTL. Named in the snapshot so the model dates
    # them rather than presenting week-old figures as this morning's.
    stale: list[str] = []

    if warm:
        # Populate the caches that nothing else warms, by calling the views the
        # browser would have called. Each is independently guarded inside
        # _tv_json, so one dead upstream costs one section.
        from contextlib import nullcontext
        if has_request_context():
            ctx = nullcontext()
        elif app is not None:
            ctx = app.test_request_context()
        else:
            ctx = None
            log.warning("Brief: warm-up needs an app or request context — "
                        "skipping it and peeking only")
        if ctx is not None:
            with ctx:
                for view in (api_commodities, api_fear_greed, api_put_call_ratio,
                             api_aaii_sentiment, api_economic_events, api_yield_curve,
                             api_credit_spread, api_movers, api_skew, api_yield_spread):
                    _tv_json(view, label="Market brief warm-up")
                # api_markets has its own warm-up thread on a 60s loop, so it is
                # normally already in memory and is deliberately not refreshed
                # here — it is the one view that can take 120s. But a brief
                # missing the index table is barely a brief, so if that thread
                # has not landed yet, pay the cost once rather than ship without.
                with _MARKETS_CACHE_LOCK:
                    have_markets = bool(_MARKETS_CACHE.get("data"))
                if not have_markets:
                    log.info("Brief warm-up: markets cache empty — fetching it "
                             "directly (slow path, background thread only)")
                    _tv_json(api_markets, label="Market brief warm-up")

    def _peek(cache: dict, lock, key: str = "data"):
        """Read a {key: {ts, data}} cache without rebuilding it."""
        try:
            with lock:
                entry = cache.get(key)
            return (entry or {}).get("data")
        except Exception as exc:  # noqa: BLE001
            log.warning("Brief: cache peek failed for %s: %s", key, exc)
            return None

    src["markets"]     = _peek(_MARKETS_CACHE, _MARKETS_CACHE_LOCK)
    src["commodities"] = _peek(_COMMODITIES_CACHE, _COMMODITIES_CACHE_LOCK)
    src["movers"]      = _peek(_MOVERS_CACHE, _MOVERS_CACHE_LOCK)
    src["fg"]          = _peek(_FG_CACHE, _FG_CACHE_LOCK)
    src["pcr"]         = _peek(_PCR_CACHE, _PCR_CACHE_LOCK)
    src["skew"]        = _peek(_SKEW_CACHE, _SKEW_LOCK)
    src["yield_curve"] = _peek(_YIELD_CURVE_CACHE, _YIELD_CURVE_CACHE_LOCK,
                               _YIELD_CURVE_CACHE_VER)
    src["yield_spread"] = _peek(_YIELD_SPREAD_CACHE, _YIELD_SPREAD_LOCK)

    # _CREDIT_SPREAD_CACHE is keyed by period, never by "data" — the daily
    # summary has been reading _CREDIT_SPREAD_CACHE.get("data") since it was
    # written, which is always None, which is why its credit-spread line has
    # never once appeared. "1y" is what /markets requests by default.
    src["credit_spread"] = _peek(_CREDIT_SPREAD_CACHE, _CREDIT_SPREAD_CACHE_LOCK, "1y")

    aaii = _peek(_AAII_CACHE, _AAII_CACHE_LOCK)
    src["aaii"] = (aaii or {}).get("latest")
    econ = _peek(_ECON_CACHE, _ECON_CACHE_LOCK)
    src["events"] = (econ or {}).get("events")

    # breadth.peek() reads memory, then disk, then the committed baseline, and
    # never rebuilds — get_breadth() would block ~82s downloading 518 tickers.
    try:
        src["breadth"] = breadth.peek()
    except Exception as exc:  # noqa: BLE001
        log.warning("Brief: breadth peek failed: %s", exc)
        src["breadth"] = None

    # The four standalone modules are memory-cache-first and warmed at startup.
    # peek() is deliberate: get_*_data() past its TTL falls through to a network
    # rebuild (housing's is ~10 MB, valuation's is ~600 constituent lookups),
    # which must never happen inside a request. Gating on is_cache_fresh()
    # instead would drop the whole section every time a nightly refresh ran
    # late — and these are weekly and monthly series, where a day of staleness
    # changes nothing a brief would say. So take the stale copy and date it.
    for name, mod in (("fed", fed_mod), ("fedwatch", fedwatch),
                      ("housing", housing), ("valuation", valuation)):
        try:
            payload = mod.peek()
            if not payload or payload.get("_warming"):
                log.info("Brief: %s has no cached payload — section marked unavailable", name)
                src[name] = None
                continue
            if not mod.is_cache_fresh():
                log.info("Brief: %s cache is stale — using it anyway, labelled", name)
                stale.append(name)
            src[name] = payload
        except Exception as exc:  # noqa: BLE001
            log.warning("Brief: %s unavailable: %s", name, exc)
            src[name] = None

    src["fed_series_meta"] = getattr(fed_mod, "SERIES", {})
    src["evaluation"] = _brief_evaluation_summary()
    src["holdings13f"], src["consensus13f"] = _brief_13f()
    src["_stale"] = stale
    return src


def _generate_market_brief(lang: str, warm: bool = False, app=None) -> dict:
    """Build the snapshot, call Gemini, and return the result dict.

    Raises on a Gemini failure so the caller decides whether that is a 500 (the
    endpoint) or a logged skip (the pre-generator).
    """
    from datetime import date as _date_cls, datetime as _dt
    from google import genai
    from google.genai import types as genai_types
    from ystocker import brief as brief_mod

    today_iso = _date_cls.today().isoformat()
    sources   = _collect_brief_sources(warm=warm, app=app)
    snapshot  = brief_mod.build_snapshot(sources, today_iso)
    prompt    = brief_mod.build_prompt(snapshot, lang)
    log.info("Market brief: lang=%s snapshot=%d chars prompt=%d chars",
             lang, len(snapshot), len(prompt))

    # Bounded below gunicorn's --timeout 120. This call can happen inside a
    # request (first visit of the day, or the ↻ button), and a worker killed at
    # the 120s wall takes every other request it was serving down with it. A
    # brief that errors at 90s is a retry; a SIGKILLed worker is an outage.
    client = genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        http_options=genai_types.HttpOptions(timeout=_BRIEF_GEMINI_TIMEOUT_MS),
    )
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty brief")

    return {
        "brief":        text,
        "generated_at": _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "sources_used": sorted(k for k, v in sources.items()
                               if v and k not in brief_mod._META_KEYS),
        "sources_cold": sorted(k for k, v in sources.items()
                               if not v and k not in brief_mod._META_KEYS),
        "sources_stale": sorted(sources.get("_stale") or []),
    }


def _brief_ddb_key(lang: str) -> str:
    return f"{lang}_{_BRIEF_KEY_VER}"


@bp.route("/api/market-brief", methods=["POST"])
def api_market_brief():
    """The long-form AI Markets Brief for /markets.

    Request body:  {"lang": "en"|"zh", "force_refresh": bool}
    Response:      {"brief": "<markdown>", "generated_at": "...",
                    "sources_used": [...], "sources_cold": [...]}

    Markdown, not prose: the card renders it through static/markdown.js, which
    does pipe tables. Cached for the day, since it describes a dated snapshot.
    """
    import time as _time
    from datetime import date as _date_cls

    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    payload = request.get_json(silent=True) or {}
    lang    = payload.get("lang", "en")
    if lang not in ("en", "zh"):
        lang = "en"
    # force_refresh was accepted and silently ignored by the endpoint this
    # replaces, so the card's refresh button has never refreshed anything.
    force     = bool(payload.get("force_refresh"))
    today_iso = _date_cls.today().isoformat()
    ddb_key   = _brief_ddb_key(lang)
    log.info("API market-brief: lang=%s force=%s", lang, force)

    if not force:
        with _BRIEF_CACHE_LOCK:
            entry = _BRIEF_CACHE.get(lang)
        if entry and _time.time() - entry["ts"] < _BRIEF_CACHE_TTL:
            return jsonify(entry["data"])

        tbl = _get_summaries_table()
        if tbl:
            try:
                item = tbl.get_item(Key={"date": today_iso,
                                         "lang_market": ddb_key}).get("Item")
                if item and item.get("summary"):
                    result = {"brief": item["summary"],
                              "generated_at": item.get("generated_at", ""),
                              "from_cache": True}
                    with _BRIEF_CACHE_LOCK:
                        _BRIEF_CACHE[lang] = {"ts": _time.time(), "data": result}
                    return jsonify(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("Brief: DynamoDB read failed: %s", exc)

    try:
        result = _generate_market_brief(lang)
    except Exception as exc:  # noqa: BLE001
        log.warning("Brief: generation failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    _store_market_brief(lang, result, today_iso)
    return jsonify(result)


def _store_market_brief(lang: str, result: dict, today_iso: str) -> None:
    """Write a generated brief to the memory cache and DynamoDB."""
    import time as _time

    with _BRIEF_CACHE_LOCK:
        _BRIEF_CACHE[lang] = {"ts": _time.time(), "data": result}
    tbl = _get_summaries_table()
    if not tbl:
        return
    try:
        tbl.put_item(Item={
            "date":         today_iso,
            "lang_market":  _brief_ddb_key(lang),
            "summary":      result["brief"],
            "generated_at": result.get("generated_at", ""),
            "ttl":          int(_time.time()) + 90 * 24 * 3600,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("Brief: DynamoDB write failed: %s", exc)


def _do_pregen_market_briefs(app=None) -> None:
    """Pre-generate both language briefs so the morning's first visit is instant.

    Called from the same scheduler as the daily summaries. Uses warm=True: this
    is a background thread, so filling the eight unwarmed caches here is free,
    and it is the difference between a complete brief and one that opens by
    apologising for four missing sections.
    """
    import time as _time
    from datetime import date as _date_cls

    if not os.environ.get("GEMINI_API_KEY"):
        log.debug("Brief pre-gen: no GEMINI_API_KEY, skipping")
        return

    today_iso = _date_cls.today().isoformat()
    tbl       = _get_summaries_table()

    for i, lang in enumerate(("en", "zh")):
        with _BRIEF_CACHE_LOCK:
            entry = _BRIEF_CACHE.get(lang)
        if entry and _time.time() - entry["ts"] < _BRIEF_CACHE_TTL:
            log.debug("Brief pre-gen: %s already in memory, skipping", lang)
            continue
        if tbl:
            try:
                item = tbl.get_item(Key={"date": today_iso,
                                         "lang_market": _brief_ddb_key(lang)}).get("Item")
                if item and item.get("summary"):
                    with _BRIEF_CACHE_LOCK:
                        _BRIEF_CACHE[lang] = {"ts": _time.time(), "data": {
                            "brief": item["summary"],
                            "generated_at": item.get("generated_at", ""),
                            "from_cache": True}}
                    log.info("Brief pre-gen: %s loaded from DynamoDB", lang)
                    continue
            except Exception as exc:  # noqa: BLE001
                log.warning("Brief pre-gen: DynamoDB read failed (%s): %s", lang, exc)
        try:
            # Only the first language needs the warm-up pass; the second reads
            # the caches the first one filled.
            result = _generate_market_brief(lang, warm=(i == 0), app=app)
            _store_market_brief(lang, result, today_iso)
            log.info("Brief pre-gen: generated %s (%d chars, %d sources cold)",
                     lang, len(result["brief"]), len(result.get("sources_cold", [])))
        except Exception as exc:  # noqa: BLE001
            log.warning("Brief pre-gen: failed for %s: %s", lang, exc)


# ---------------------------------------------------------------------------
# Daily AI Summary  (/api/daily-summary)
# ---------------------------------------------------------------------------

_DAILY_SUMMARY_CACHE: dict = {}
_DAILY_SUMMARY_CACHE_LOCK = threading.Lock()
_DAILY_SUMMARY_CACHE_TTL  = 1800   # 30 minutes

# ---------------------------------------------------------------------------
# Daily Summaries DynamoDB table  (ystocker-daily-summaries)
# ---------------------------------------------------------------------------

_SUMMARIES_TABLE_NAME    = "ystocker-daily-summaries"
_summaries_table         = None
_SUMMARIES_LOCK          = threading.Lock()
_summaries_unavail_until = 0.0


def _get_summaries_table():
    """Return boto3 DynamoDB Table for the daily summaries, or None if unavailable."""
    global _summaries_table, _summaries_unavail_until
    if _summaries_table is not None:
        return _summaries_table
    if time.time() < _summaries_unavail_until:
        return None
    with _SUMMARIES_LOCK:
        if _summaries_table is not None:
            return _summaries_table
        if time.time() < _summaries_unavail_until:
            return None
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(_SUMMARIES_TABLE_NAME)
            tbl.load()
            _summaries_table = tbl
            log.info("DynamoDB summaries table connected: %s", _SUMMARIES_TABLE_NAME)
        except Exception as exc:
            log.warning("DynamoDB summaries unavailable: %s", exc)
            _summaries_table = None
            _summaries_unavail_until = time.time() + 300
        return _summaries_table


@bp.route("/api/daily-summary", methods=["POST"])
def api_daily_summary():
    """
    Generate an AI-written daily markets summary using Gemini.

    Request body: { market_data: {...}, lang: "en"|"zh", market: "us"|"cn" }
    Response:     { summary: "...", generated_at: "..." }
    """
    from datetime import date as _date_cls, datetime as _dt
    import time as _time

    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    payload = request.get_json(silent=True) or {}
    lang    = payload.get("lang", "en")
    market  = payload.get("market", "us")
    if lang not in ("en", "zh"):
        lang = "en"
    if market not in ("us", "cn"):
        market = "us"
    log.info("API daily-summary: lang=%s market=%s", lang, market)

    cache_key  = f"{lang}_{market}"
    today_str  = _date_cls.today().isoformat()

    # ── In-memory cache check ────────────────────────────────────────────────
    with _DAILY_SUMMARY_CACHE_LOCK:
        entry = _DAILY_SUMMARY_CACHE.get(cache_key)
        if entry and _time.time() - entry["ts"] < _DAILY_SUMMARY_CACHE_TTL:
            return jsonify(entry["data"])

    # ── DynamoDB load (today's stored summary) ───────────────────────────────
    tbl = _get_summaries_table()
    if tbl:
        try:
            item = tbl.get_item(Key={"date": today_str, "lang_market": cache_key}).get("Item")
            if item and item.get("summary"):
                result = {
                    "summary":      item["summary"],
                    "generated_at": item.get("generated_at", ""),
                    "from_cache":   True,
                }
                with _DAILY_SUMMARY_CACHE_LOCK:
                    _DAILY_SUMMARY_CACHE[cache_key] = {"ts": _time.time(), "data": result}
                return jsonify(result)
        except Exception as exc:
            log.warning("DynamoDB summaries read failed: %s", exc)

    # ── Build market snapshot ────────────────────────────────────────────────
    md    = payload.get("market_data", {})
    lines = [f"Date: {today_str}"]
    idx   = md.get("indices", {})

    if market == "us":
        for key, label in [("spx","S&P 500"), ("ixic","Nasdaq"), ("dji","Dow Jones")]:
            d = idx.get(key, {})
            if d.get("current"):
                chg = f"{d['day_chg']:+.2f}%" if d.get("day_chg") is not None else "—"
                lines.append(f"{label}: {d['current']:,.2f} ({chg}) YTD {d.get('ytd','—')}%")

        vix = md.get("vix", {})
        if vix.get("current"):
            lines.append(f"VIX: {vix['current']:.2f}")

        fg = md.get("fg", {})
        if fg.get("score"):
            lines.append(f"Fear & Greed: {fg['score']:.0f} ({fg.get('rating','')})")

        pcr = md.get("pcr", {})
        if pcr.get("current"):
            lines.append(f"Put/Call Ratio: {pcr['current']:.2f} (20d MA: {pcr.get('ma20','—')})")

        aaii = md.get("aaii", {})
        if aaii.get("bullish"):
            lines.append(f"AAII: Bull {aaii['bullish']:.1f}% Bear {aaii['bearish']:.1f}% Spread {aaii.get('bull_bear_spread','—')}%")

        sectors = md.get("sectors", [])
        if sectors:
            top = sorted(sectors, key=lambda s: s.get("day_chg", 0) or 0)
            lines.append(f"Sectors — best: {top[-1]['label']} {top[-1].get('day_chg',0):+.2f}%, worst: {top[0]['label']} {top[0].get('day_chg',0):+.2f}%")

        gainers = md.get("gainers", [])
        losers  = md.get("losers", [])
        if gainers:
            g_str = ", ".join(f"{g['ticker']} {g['day_chg']:+.2f}%" for g in gainers[:3])
            lines.append(f"Top gainers: {g_str}")
        if losers:
            l_str = ", ".join(f"{l['ticker']} {l['day_chg']:+.2f}%" for l in losers[:3])
            lines.append(f"Top losers: {l_str}")

        econ_events = md.get("events", [])
        if econ_events:
            us_evts = [e for e in econ_events if e.get("country") in ("US", "")][:5]
            if us_evts:
                ev_str = "; ".join(f"{e.get('event','')}" for e in us_evts)
                lines.append(f"Key US economic events: {ev_str}")

        # Treasury yields
        try:
            with _YIELD_CURVE_CACHE_LOCK:
                yc_entry = _YIELD_CURVE_CACHE.get(_YIELD_CURVE_CACHE_VER)
            if yc_entry:
                yield_data = yc_entry["data"]
                us_yc = yield_data.get("us", {}).get("current", {})
                y10 = us_yc.get("10Y")
                y2  = us_yc.get("2Y")
                if y10 is not None:
                    lines.append(f"10Y Treasury: {y10:.2f}%")
                if y2 is not None:
                    lines.append(f"2Y Treasury: {y2:.2f}%")
                if y10 is not None and y2 is not None:
                    lines.append(f"10Y-2Y Spread: {(y10 - y2):.2f}% ({'INVERTED - recession signal' if y10 < y2 else 'normal'})")
        except Exception:
            pass

        # HY credit spread proxy
        try:
            cs_cache_entry = _CREDIT_SPREAD_CACHE.get("data")
            if cs_cache_entry:
                cs_data = cs_cache_entry.get("data", {})
                cs_spread = cs_data.get("spread", [])
                if cs_spread:
                    latest_cs = cs_spread[-1]
                    lines.append(f"HY/IG Credit Spread (HYG/TLT ratio): {latest_cs:.4f} "
                                f"({'tight, risk-on' if latest_cs > 0.5 else 'wide, risk-off'})")
        except Exception:
            pass

    else:  # market == "cn"
        for key, label in [("sse","Shanghai Composite"), ("csi500","CSI 500 (中证500)"),
                           ("twii","Taiwan TWII"), ("kospi","KOSPI"),
                           ("n225","Nikkei 225"), ("ftse","FTSE 100")]:
            d = idx.get(key, {})
            if d.get("current"):
                chg = f"{d['day_chg']:+.2f}%" if d.get("day_chg") is not None else "—"
                lines.append(f"{label}: {d['current']:,.2f} ({chg}) YTD {d.get('ytd','—')}%")

        econ_events = md.get("events", [])
        if econ_events:
            cn_evts = [e for e in econ_events if e.get("country") in ("CN","JP","KR","TW","AU","EU")][:5]
            if cn_evts:
                ev_str = "; ".join(f"{e.get('country','')} {e.get('event','')}" for e in cn_evts)
                lines.append(f"Key Asia/Europe economic events: {ev_str}")

    snapshot = "\n".join(lines)

    # ── Build prompt ─────────────────────────────────────────────────────────
    if market == "us":
        if lang == "zh":
            prompt = (
                "你是一位简洁的金融分析师。请根据以下数据，用中文撰写一份美国市场每日评论。"
                "请严格按以下结构分为4段：\n"
                "第1段：指数与宏观概述（主要涨跌、趋势）\n"
                "第2段：情绪分析（VIX、恐慌贪婪指数、PCR、AAII、国债收益率曲线形态）\n"
                "第3段：板块轮动与涨跌幅前列个股\n"
                "第4段：前瞻展望——下一交易日的主要风险与催化剂\n"
                "每段2-3句话。语言要直接、有洞察力、保持中立。不要使用Markdown标题或项目符号。\n\n"
                f"市场快照：\n{snapshot}"
            )
        else:
            prompt = (
                "You are a concise financial analyst. Write a daily US markets commentary "
                "based on the snapshot below. "
                "Structure your response as exactly 4 paragraphs:\n"
                "P1: Index & macro overview (key moves, trend)\n"
                "P2: Sentiment context (VIX, Fear&Greed, PCR, AAII, yield curve shape)\n"
                "P3: Sector rotation & top movers\n"
                "P4: Forward look — key risks/catalysts for the next session\n"
                "Keep each paragraph 2-3 sentences. No bullet points. "
                "Be direct, insightful, and neutral. Use plain English, no markdown headers.\n\n"
                f"Market snapshot:\n{snapshot}"
            )
    else:
        if lang == "zh":
            prompt = (
                "你是一位简洁的金融分析师。请根据以下数据，用中文撰写一份简短（3-4段）的亚太市场每日评论。"
                "重点分析上证综指、中证500、台湾加权指数（台指）、韩国综合指数（KOSPI）、日经225、日元汇率、韩元汇率及相关亚洲经济事件。"
                "语言要直接、有洞察力、保持中立。不要使用Markdown标题。\n\n"
                f"市场快照：\n{snapshot}"
            )
        else:
            prompt = (
                "You are a concise financial analyst. Write a brief (3-4 paragraph) daily Asian/Pacific markets commentary "
                "focusing on Shanghai Composite (SSE), CSI 500 (中证500), Taiwan Weighted Index (TWII), "
                "KOSPI (South Korea), Nikkei 225 (Japan), JPY/USD, KRW/USD, and "
                "relevant Asian/European economic events. Be direct, insightful, and neutral. "
                "Use plain English, no markdown headers.\n\n"
                f"Market snapshot:\n{snapshot}"
            )

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        summary = resp.text.strip()
    except Exception as exc:
        log.warning("Daily summary Gemini call failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    generated_at = _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    result = {
        "summary":      summary,
        "generated_at": generated_at,
    }

    # ── Save to DynamoDB ─────────────────────────────────────────────────────
    if tbl:
        try:
            import calendar as _cal
            ttl_val = int(_time.time()) + 90 * 24 * 3600  # 90-day TTL
            tbl.put_item(Item={
                "date":         today_str,
                "lang_market":  cache_key,
                "summary":      summary,
                "generated_at": generated_at,
                "ttl":          ttl_val,
            })
        except Exception as exc:
            log.warning("DynamoDB summaries write failed: %s", exc)

    with _DAILY_SUMMARY_CACHE_LOCK:
        _DAILY_SUMMARY_CACHE[cache_key] = {"ts": _time.time(), "data": result}
    return jsonify(result)


@bp.route("/api/daily-summary/<date>/<lang>", methods=["GET"])
def api_daily_summary_history(date, lang):
    """
    Return stored AI summaries for a given date and language from DynamoDB.

    Response: { us: "...", cn: "...", generated_at: "...", date: "..." }
    """
    log.info("API daily-summary/history: date=%s lang=%s", date, lang)
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "Invalid date format"}), 400
    if lang not in ("en", "zh"):
        lang = "en"

    tbl = _get_summaries_table()
    if not tbl:
        return jsonify({"error": "Summaries service unavailable"}), 503

    result = {"date": date, "us": None, "cn": None}
    for market in ("us", "cn"):
        cache_key = f"{lang}_{market}"
        try:
            item = tbl.get_item(Key={"date": date, "lang_market": cache_key}).get("Item")
            if item and item.get("summary"):
                result[market] = {
                    "summary":      item["summary"],
                    "generated_at": item.get("generated_at", ""),
                }
        except Exception as exc:
            log.warning("DynamoDB summaries history read failed (%s/%s): %s", date, cache_key, exc)

    return jsonify(result)


# ---------------------------------------------------------------------------
# Email Subscribers  (DynamoDB: ystocker-subscribers)
# ---------------------------------------------------------------------------

_SUBSCRIBERS_TABLE_NAME    = "ystocker-subscribers"
_subscribers_table         = None
_SUBSCRIBERS_LOCK          = threading.Lock()
_subscribers_unavail_until = 0.0


def _get_subscribers_table():
    """Return boto3 DynamoDB Table for the subscriber list, or None if unavailable."""
    global _subscribers_table, _subscribers_unavail_until
    if _subscribers_table is not None:
        return _subscribers_table
    if time.time() < _subscribers_unavail_until:
        remaining = int(_subscribers_unavail_until - time.time())
        log.warning("DynamoDB subscribers in backoff, skipping (retry in %ds)", remaining)
        return None
    with _SUBSCRIBERS_LOCK:
        if _subscribers_table is not None:
            return _subscribers_table
        if time.time() < _subscribers_unavail_until:
            return None
        region = os.environ.get("AWS_REGION", "us-west-2")
        log.info("Connecting to DynamoDB subscribers table: %s (region=%s)",
                 _SUBSCRIBERS_TABLE_NAME, region)
        try:
            import boto3
            ddb = boto3.resource("dynamodb", region_name=region)
            tbl = ddb.Table(_SUBSCRIBERS_TABLE_NAME)
            tbl.load()
            _subscribers_table = tbl
            log.info("DynamoDB subscribers table connected: %s (region=%s)",
                     _SUBSCRIBERS_TABLE_NAME, region)
        except Exception as exc:
            log.error("DynamoDB subscribers unavailable (table=%s, region=%s): %s",
                      _SUBSCRIBERS_TABLE_NAME, region, exc, exc_info=True)
            _subscribers_table = None
            _subscribers_unavail_until = time.time() + 300
            log.warning("DynamoDB subscribers backoff set for 300s")
        return _subscribers_table


@bp.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """Subscribe an email to the daily report mailing list."""
    import secrets as _secrets
    from datetime import datetime as _dt

    payload = request.get_json(silent=True) or {}
    email   = (payload.get("email") or "").strip().lower()
    lang    = payload.get("lang", "en")
    if lang not in ("en", "zh"):
        lang = "en"

    log.info("Subscribe request: email=%s lang=%s", email or "(empty)", lang)

    if not email or "@" not in email:
        log.warning("Subscribe rejected: invalid email %r", email)
        return jsonify({"error": "Invalid email address"}), 400

    table = _get_subscribers_table()
    if not table:
        log.error("Subscribe failed: DynamoDB subscribers table unavailable (email=%s)", email)
        return jsonify({"error": "Subscriber service unavailable"}), 503

    try:
        existing = table.get_item(Key={"email": email}).get("Item")
        if existing and existing.get("active"):
            log.info("Subscribe: already active (email=%s)", email)
            return jsonify({"ok": True, "already": True})

        token = _secrets.token_urlsafe(32)
        table.put_item(Item={
            "email":             email,
            "lang":              lang,
            "subscribed_at":     _dt.utcnow().isoformat(),
            "active":            True,
            "unsubscribe_token": token,
        })
        log.info("New subscriber: %s (lang=%s)", email, lang)
        return jsonify({"ok": True, "already": False})
    except Exception as exc:
        log.error("Subscribe failed for %s: %s", email, exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@bp.route("/unsubscribe")
def unsubscribe_page():
    """Unsubscribe a user via their unique token and show a confirmation page."""
    token = request.args.get("token", "").strip()
    if not token:
        return render_template("unsubscribe.html", success=False,
                               message="Invalid unsubscribe link.")

    table = _get_subscribers_table()
    if not table:
        return render_template("unsubscribe.html", success=False,
                               message="Service temporarily unavailable. Please try again later.")

    try:
        from boto3.dynamodb.conditions import Attr as _Attr
        items = table.scan(
            FilterExpression=_Attr("unsubscribe_token").eq(token)
        ).get("Items", [])
        if not items:
            return render_template("unsubscribe.html", success=False,
                                   message="Link not found or already unsubscribed.")
        table.update_item(
            Key={"email": items[0]["email"]},
            UpdateExpression="SET active = :f",
            ExpressionAttributeValues={":f": False},
        )
        log.info("Unsubscribed: %s", items[0]["email"])
        return render_template("unsubscribe.html", success=True, email=items[0]["email"])
    except Exception as exc:
        log.warning("Unsubscribe failed: %s", exc)
        return render_template("unsubscribe.html", success=False,
                               message="Something went wrong. Please try again.")


# ---------------------------------------------------------------------------
# Daily email helpers — shared by the HTTP endpoint and the auto-broadcast
# scheduler so all email rendering logic lives in one place.
# ---------------------------------------------------------------------------

_EMAIL_CELL  = 'style="padding:5px 8px;border-bottom:1px solid #1e293b;font-size:13px;color:#cbd5e1"'
_EMAIL_HDR   = 'style="padding:5px 8px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#64748b;border-bottom:1px solid #334155"'
_EMAIL_TABLE = 'width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:4px"'
_EMAIL_SEC   = 'style="margin:0 0 8px 0;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b"'
_EMAIL_DIV   = '<tr><td colspan="10" style="padding:14px 0"><div style="height:1px;background:#1e293b"></div></td></tr>'


def _email_chg_html(v):
    if v is None:
        return '<span style="color:#94a3b8">—</span>'
    col = '#34d399' if v >= 0 else '#f87171'
    return f'<span style="color:{col}">{("+" if v >= 0 else "")}{v:.2f}%</span>'


def _build_email_sections(
    is_zh: bool, ai_us: str, ai_cn: str,
    indices: dict, sectors: list, vix_data: dict, gold: dict,
    sentiment: dict, events: list, gainers: list, losers: list,
    today_str: str, today_iso: str,
) -> tuple:
    """Return (body_rows_html, text_lines_list) for one language."""
    CELL  = _EMAIL_CELL
    HDR   = _EMAIL_HDR
    TABLE = _EMAIL_TABLE
    SEC   = _EMAIL_SEC
    DIV   = _EMAIL_DIV

    # ── AI Commentary — two side-by-side cards ────────────────────────────
    def _ai_card(title: str, text: str) -> str:
        paras = "".join(
            f'<p style="margin:0 0 12px 0;color:#cbd5e1;line-height:1.7;font-size:13px">{p.strip()}</p>'
            for p in text.split("\n\n") if p.strip()
        )
        return f'<p {SEC}>{title}</p>{paras}' if paras else ''

    us_card = _ai_card("美股市场解读" if is_zh else "US Market Commentary", ai_us)
    cn_card = _ai_card("A股市场解读"  if is_zh else "CN Market Commentary", ai_cn)

    if us_card and cn_card:
        ai_section = (
            f'<table width="100%" cellpadding="0" cellspacing="8">'
            f'<tr>'
            f'<td width="49%" valign="top" style="background:#263348;border-radius:8px;padding:12px 14px">{us_card}</td>'
            f'<td width="2%"></td>'
            f'<td width="49%" valign="top" style="background:#263348;border-radius:8px;padding:12px 14px">{cn_card}</td>'
            f'</tr>'
            f'</table>'
        )
    elif us_card or cn_card:
        ai_section = us_card or cn_card
    else:
        ai_section = ''

    # ── Indices ────────────────────────────────────────────────────────────
    IDX_META = [
        ('spx','S&P 500'), ('ixic','NASDAQ'), ('dji','Dow Jones'),
        ('ftse','FTSE 100'), ('n225','Nikkei 225'), ('sse','上证' if is_zh else 'Shanghai'),
        ('csi500','中证500' if is_zh else 'CSI 500'), ('twii','台湾加权' if is_zh else 'Taiwan'), ('kospi','KOSPI'),
    ]
    idx_rows = ''
    for k, lbl in IDX_META:
        d = indices.get(k, {})
        price = d.get('current')
        if price is None:
            continue
        idx_rows += (
            f'<tr>'
            f'<td {CELL} style="padding:5px 8px;font-size:13px;color:#e2e8f0;font-weight:600">{lbl}</td>'
            f'<td {CELL} align="right" style="font-family:monospace">{price:,.2f}</td>'
            f'<td {CELL} align="right">{_email_chg_html(d.get("day_chg"))}</td>'
            f'</tr>'
        )
    indices_section = (
        f'<p {SEC}>{"指数" if is_zh else "Indices"}</p>'
        f'<table {TABLE}>'
        f'<tr><th {HDR}>{"指数" if is_zh else "Index"}</th>'
        f'<th {HDR} align="right">{"价格" if is_zh else "Price"}</th>'
        f'<th {HDR} align="right">{"今日" if is_zh else "Day %"}</th></tr>'
        f'{idx_rows}</table>'
    ) if idx_rows else ''

    # ── Market Metrics ─────────────────────────────────────────────────────
    fg   = sentiment.get('fg', {})
    pcr  = sentiment.get('pcr', {})
    aaii = sentiment.get('aaii', {})
    vix_v = vix_data.get('current')
    met_rows = ''
    if vix_v is not None:
        met_rows += (f'<tr><td {CELL}>VIX</td>'
                     f'<td {CELL} align="right" style="font-family:monospace">{vix_v:.2f}</td>'
                     f'<td {CELL} align="right">{_email_chg_html(vix_data.get("day_chg"))}</td></tr>')
    if pcr.get('current') is not None:
        met_rows += (f'<tr><td {CELL}>{"看跌/看涨比" if is_zh else "Put/Call Ratio"}</td>'
                     f'<td {CELL} align="right" style="font-family:monospace">{pcr["current"]:.2f}</td>'
                     f'<td {CELL}></td></tr>')
    if fg.get('score') is not None:
        fgv = round(fg['score'])
        met_rows += (f'<tr><td {CELL}>{"恐惧/贪婪" if is_zh else "Fear & Greed"}</td>'
                     f'<td {CELL} align="right" style="font-family:monospace">{fgv}</td>'
                     f'<td {CELL} style="color:#94a3b8;font-size:12px">{fg.get("rating","")}</td></tr>')
    if aaii.get('bullish') is not None:
        bull = aaii['bullish']; bear = aaii.get('bearish')
        spread = aaii.get('bull_bear_spread')
        met_rows += (f'<tr><td {CELL}>AAII {"看多" if is_zh else "Bullish"}</td>'
                     f'<td {CELL} align="right" style="color:#34d399;font-family:monospace">{bull:.1f}%</td>'
                     f'<td {CELL} align="right" style="color:#f87171;font-family:monospace">'
                     f'{"空" if is_zh else "Bear"}: {bear:.1f}%</td></tr>' if bear else
                     f'<tr><td {CELL}>AAII</td><td {CELL} align="right" style="color:#34d399">{bull:.1f}%</td>'
                     f'<td {CELL}></td></tr>')
        if spread is not None:
            met_rows += (f'<tr><td {CELL}>{"牛熊差" if is_zh else "Bull-Bear Spread"}</td>'
                         f'<td {CELL} align="right">{_email_chg_html(spread)}</td>'
                         f'<td {CELL}></td></tr>')
    metrics_section = (
        f'<p {SEC}>{"市场情绪与指标" if is_zh else "Market Metrics"}</p>'
        f'<table {TABLE}>'
        f'<tr><th {HDR}>{"指标" if is_zh else "Indicator"}</th>'
        f'<th {HDR} align="right">{"值" if is_zh else "Value"}</th>'
        f'<th {HDR}></th></tr>'
        f'{met_rows}</table>'
    ) if met_rows else ''

    # ── Sectors ────────────────────────────────────────────────────────────
    sorted_sectors = sorted(sectors, key=lambda s: (s.get('day_chg') or -999), reverse=True)
    sec_rows = ''.join(
        f'<tr><td {CELL}>{s.get("label","")}</td>'
        f'<td {CELL} align="right">{_email_chg_html(s.get("day_chg"))}</td></tr>'
        for s in sorted_sectors
    )
    sectors_section = (
        f'<p {SEC}>{"板块表现" if is_zh else "Sector Performance"}</p>'
        f'<table {TABLE}>'
        f'<tr><th {HDR}>{"板块" if is_zh else "Sector"}</th>'
        f'<th {HDR} align="right">{"今日" if is_zh else "Day %"}</th></tr>'
        f'{sec_rows}</table>'
    ) if sec_rows else ''

    # ── Commodity Ratios ───────────────────────────────────────────────────
    gold_rows = ''
    if gold:
        gp = gold.get('gold_price'); sp_v = gold.get('silver_price')
        gs = gold.get('current_gs'); gc = gold.get('current_gc')
        if gp is not None:
            gold_rows += (f'<tr><td {CELL}>{"黄金" if is_zh else "Gold"}</td>'
                          f'<td {CELL} align="right" style="color:#fbbf24;font-family:monospace">${gp:,.0f}</td>'
                          f'<td {CELL}></td></tr>')
        if sp_v is not None:
            gold_rows += (f'<tr><td {CELL}>{"白银" if is_zh else "Silver"}</td>'
                          f'<td {CELL} align="right" style="color:#94a3b8;font-family:monospace">${sp_v:,.2f}</td>'
                          f'<td {CELL}></td></tr>')
        if gs is not None:
            gold_rows += (f'<tr><td {CELL}>{"金银比" if is_zh else "G/S Ratio"}</td>'
                          f'<td {CELL} align="right" style="font-family:monospace">{gs:.1f}</td>'
                          f'<td {CELL} align="right">{_email_chg_html(gold.get("gs_day_chg"))}</td></tr>')
        if gc is not None:
            gold_rows += (f'<tr><td {CELL}>{"金铜比" if is_zh else "G/C Ratio"}</td>'
                          f'<td {CELL} align="right" style="font-family:monospace">{gc:.1f}</td>'
                          f'<td {CELL} align="right">{_email_chg_html(gold.get("gc_day_chg"))}</td></tr>')
    gold_section = (
        f'<p {SEC}>{"商品比率" if is_zh else "Commodity Ratios"}</p>'
        f'<table {TABLE}>'
        f'<tr><th {HDR}>{"品种" if is_zh else "Commodity"}</th>'
        f'<th {HDR} align="right">{"值" if is_zh else "Value"}</th>'
        f'<th {HDR} align="right">{"今日" if is_zh else "Day %"}</th></tr>'
        f'{gold_rows}</table>'
    ) if gold_rows else ''

    # ── Economic Events ────────────────────────────────────────────────────
    upcoming = [e for e in events if (e.get('date') or '') >= today_iso]
    show_ev  = [e for e in upcoming if e.get('impact') == 'High'][:8] or upcoming[:8]
    IMP_COL  = {'High': '#f87171', 'Medium': '#fbbf24', 'Low': '#64748b'}
    ev_rows  = ''.join(
        f'<tr>'
        f'<td {CELL} style="padding:5px 8px;font-size:11px;color:#64748b;white-space:nowrap">{e.get("date","")}</td>'
        f'<td {CELL} style="padding:5px 8px;font-size:11px;color:#94a3b8;white-space:nowrap">{e.get("time","")}</td>'
        f'<td {CELL} style="padding:5px 8px;font-size:12px;color:#cbd5e1">{e.get("event","")}</td>'
        f'<td {CELL} style="padding:5px 8px;font-size:10px;font-weight:700;'
        f'color:{IMP_COL.get(e.get("impact",""),"#64748b")};white-space:nowrap">{e.get("impact","")}</td>'
        f'</tr>'
        for e in show_ev
    )
    events_section = (
        f'<p {SEC}>{"经济事件" if is_zh else "Economic Events"}</p>'
        f'<table {TABLE}>'
        f'<tr><th {HDR}>{"日期" if is_zh else "Date"}</th>'
        f'<th {HDR}>{"时间" if is_zh else "Time"}</th>'
        f'<th {HDR}>{"事件" if is_zh else "Event"}</th>'
        f'<th {HDR}>{"影响" if is_zh else "Impact"}</th></tr>'
        f'{ev_rows}</table>'
    ) if ev_rows else ''

    # ── Top Movers ─────────────────────────────────────────────────────────
    def _mover_rows(movers, is_gain):
        rows = ''
        for m in movers:
            price   = m.get('price')
            chg     = m.get('day_chg')
            col     = '#34d399' if is_gain else '#f87171'
            p_str   = f'${price:,.2f}' if price is not None else '—'
            chg_str = f'{("+" if chg >= 0 else "")}{chg:.2f}%' if chg is not None else '—'
            rows   += (
                f'<tr>'
                f'<td {CELL} style="font-weight:600;color:#e2e8f0">{m.get("ticker","")}</td>'
                f'<td {CELL} style="font-size:12px;color:#64748b">{(m.get("name") or "")[:28]}</td>'
                f'<td {CELL} align="right" style="font-family:monospace;color:#94a3b8">{p_str}</td>'
                f'<td {CELL} align="right" style="color:{col};font-weight:700">{chg_str}</td>'
                f'</tr>'
            )
        return rows

    gr = _mover_rows(gainers, True)
    lr = _mover_rows(losers, False)
    movers_section = (
        f'<p {SEC}>{"今日榜单" if is_zh else "Top Movers"}</p>'
        f'<table {TABLE}>'
        f'<tr><th {HDR} colspan="2">{"涨幅榜" if is_zh else "Top Gainers"}</th>'
        f'<th {HDR} align="right">{"价格" if is_zh else "Price"}</th>'
        f'<th {HDR} align="right">%</th></tr>'
        f'{gr}'
        f'<tr><td colspan="4" style="padding:6px 0"></td></tr>'
        f'<tr><th {HDR} colspan="2">{"跌幅榜" if is_zh else "Top Losers"}</th>'
        f'<th {HDR} align="right">{"价格" if is_zh else "Price"}</th>'
        f'<th {HDR} align="right">%</th></tr>'
        f'{lr}'
        f'</table>'
    ) if (gr or lr) else ''

    # ── Assemble ───────────────────────────────────────────────────────────
    all_sections = [s for s in [
        ai_section, indices_section, metrics_section,
        sectors_section, gold_section, events_section, movers_section,
    ] if s]
    body_rows = f'<tr>{DIV}</tr>'.join(
        f'<tr><td style="padding:0">{s}</td></tr>' for s in all_sections
    )

    # Plain-text fallback
    txt = [f'{"每日市场报告" if is_zh else "Daily Markets Report"} — {today_str}', ""]
    if ai_us:
        txt += [f'=== {"美股市场解读" if is_zh else "US Market Commentary"} ===', "", ai_us, ""]
    if ai_cn:
        txt += [f'=== {"A股市场解读" if is_zh else "CN Market Commentary"} ===', "", ai_cn, ""]
    for k, lbl in IDX_META:
        d = indices.get(k, {})
        if d.get('current') is not None:
            chg   = d.get('day_chg')
            chg_s = f"  {('+' if chg >= 0 else '')}{chg:.2f}%" if chg is not None else ''
            txt.append(f"{lbl}: {d['current']:,.2f}{chg_s}")

    return body_rows, txt


def _wrap_email_html(subject_str: str, header_str: str, today: str,
                     body_rows: str, footer_str: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{subject_str}</title></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:32px 16px">
    <tr><td align="center">
      <table width="1200" cellpadding="0" cellspacing="0"
             style="max-width:1200px;width:100%;background:#1e293b;border-radius:16px;overflow:hidden">
        <tr><td style="background:linear-gradient(135deg,#1d4ed8,#7c3aed);padding:28px 32px">
          <h1 style="margin:0;font-size:22px;font-weight:700">
            <a href="https://stock.li-family.us" style="color:#fff;text-decoration:none">yStocker</a>
          </h1>
          <p style="margin:6px 0 0;color:#bfdbfe;font-size:15px">{header_str}</p>
          <p style="margin:4px 0 0;color:#93c5fd;font-size:13px">{today}</p>
        </td></tr>
        <tr><td style="padding:24px 28px">
          <table width="100%" cellpadding="0" cellspacing="0">{body_rows}</table>
        </td></tr>
        <tr><td style="padding:14px 28px 24px;border-top:1px solid #334155">
          <p style="margin:0;color:#64748b;font-size:12px;line-height:1.6">{footer_str}</p>
          <p style="margin:8px 0 0">__UNSUB__</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _build_daily_email_cache(
    indices: dict, sectors: list, vix_data: dict, gold: dict,
    sentiment: dict, events: list, gainers: list, losers: list,
    summaries_us: dict, summaries_cn: dict,
    today_str: str, today_iso: str, fallback_lang: str = "en",
) -> dict:
    """Build HTML/text email templates for 'en' and 'zh'. Returns {lang: {subject, html_tmpl, text_base}}."""
    cached: dict = {}
    for _l in ("en", "zh"):
        _is_zh = _l == "zh"
        _subj  = f"yStocker {'每日市场报告' if _is_zh else 'Daily Markets Report'} — {today_str}"
        _hdr   = "每日市场报告" if _is_zh else "Daily Markets Report"
        _foot  = ("本报告由 yStocker 自动生成。数据来自 Yahoo Finance、CBOE 及美联储。"
                  if _is_zh else
                  "Auto-generated by yStocker. Data sourced from Yahoo Finance, CBOE, and the Federal Reserve.")
        _ai_us = summaries_us.get(_l) or summaries_us.get(fallback_lang, "")
        _ai_cn = summaries_cn.get(_l) or summaries_cn.get(fallback_lang, "")
        _brows, _txt = _build_email_sections(
            _is_zh, _ai_us, _ai_cn, indices, sectors, vix_data, gold,
            sentiment, events, gainers, losers, today_str, today_iso,
        )
        _txt += ["", _foot]
        cached[_l] = {
            "subject":   _subj,
            "html_tmpl": _wrap_email_html(_subj, _hdr, today_str, _brows, _foot),
            "text_base": "\n".join(_txt),
        }
    return cached


def _ses_send_to_recipients(
    ses_client, recipients: list, cached_templates: dict,
    base_url: str, ses_from: str,
) -> tuple:
    """Send daily report emails. Returns (sent_count, error_addresses)."""
    sent_count = 0
    errors: list = []
    for rec in recipients:
        rec_lang  = rec.get("lang", "en")
        rec_is_zh = rec_lang == "zh"
        token     = rec.get("token", "")
        tmpl      = cached_templates.get(rec_lang) or cached_templates["en"]

        if token:
            unsub_url  = f"{base_url}/unsubscribe?token={token}"
            unsub_html = (f'<a href="{unsub_url}" style="color:#4b5563;font-size:11px;text-decoration:underline">'
                          f'{"退订每日报告" if rec_is_zh else "Unsubscribe from daily reports"}</a>')
            unsub_txt  = f'{"退订" if rec_is_zh else "Unsubscribe"}: {unsub_url}'
        else:
            unsub_html = (f'<span style="color:#475569;font-size:11px">'
                          f'{"此邮件为一次性发送。" if rec_is_zh else "You received this as a one-time send."}</span>')
            unsub_txt  = "此邮件为一次性发送。" if rec_is_zh else "You received this as a one-time send."

        html_body = tmpl["html_tmpl"].replace("__UNSUB__", unsub_html)
        text_body = tmpl["text_base"] + f"\n{unsub_txt}"
        try:
            ses_client.send_email(
                Source=ses_from,
                Destination={"ToAddresses": [rec["email"]]},
                Message={
                    "Subject": {"Data": tmpl["subject"], "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": html_body, "Charset": "UTF-8"},
                        "Text": {"Data": text_body, "Charset": "UTF-8"},
                    },
                },
            )
            sent_count += 1
            log.info("Daily report sent to %s (lang=%s)", rec["email"], rec_lang)
        except Exception as exc:
            log.warning("SES send failed for %s: %s", rec["email"], exc)
            errors.append(rec["email"])
    return sent_count, errors


# ---------------------------------------------------------------------------
# Send Daily Report Email via AWS SES  (/api/send-daily-email)
# ---------------------------------------------------------------------------

@bp.route("/api/send-daily-email", methods=["POST"])
def api_send_daily_email():
    """
    Broadcast the daily report to the requested email address.
    Returns 202 immediately and does the actual SES send in a background
    thread so the Gunicorn worker is never blocked by email I/O.
    """
    SES_FROM = os.environ.get("SES_FROM_EMAIL")
    if not SES_FROM:
        log.warning("API send-daily-email: SES_FROM_EMAIL not configured")
        return jsonify({"error": "SES_FROM_EMAIL not configured"}), 503

    payload        = request.get_json(silent=True) or {}
    email          = (payload.get("email") or "").strip()
    lang           = payload.get("lang", "en")
    log.info("API send-daily-email: email=%s lang=%s", email or "(broadcast)", lang)
    summary_us_raw = (payload.get("summary_us") or "").strip()
    summary_cn_raw = (payload.get("summary_cn") or "").strip()
    indices        = payload.get("indices", {})
    sectors        = payload.get("sectors", [])
    vix_data    = payload.get("vix", {})
    gold        = payload.get("gold_ratios", {})
    sentiment   = payload.get("sentiment", {})
    events      = payload.get("events", [])
    gainers     = payload.get("gainers", [])
    losers      = payload.get("losers", [])

    if not email or "@" not in email:
        return jsonify({"error": "Invalid email address"}), 400

    from datetime import date as _date_cls
    today_str = _date_cls.today().strftime("%B %d, %Y")
    today_iso = _date_cls.today().isoformat()
    base_url  = os.environ.get("APP_BASE_URL", request.host_url).rstrip("/")

    # Snapshot in-memory summaries now (before leaving request context)
    summaries_us: dict = {lang: summary_us_raw} if summary_us_raw else {}
    summaries_cn: dict = {lang: summary_cn_raw} if summary_cn_raw else {}
    for _l in ("en", "zh"):
        if _l not in summaries_us:
            _cached = _DAILY_SUMMARY_CACHE.get(f"{_l}_us", {})
            _s = (_cached.get("data") or {}).get("summary", "")
            if _s:
                summaries_us[_l] = _s
        if _l not in summaries_cn:
            _cached = _DAILY_SUMMARY_CACHE.get(f"{_l}_cn", {})
            _s = (_cached.get("data") or {}).get("summary", "")
            if _s:
                summaries_cn[_l] = _s

    # ── Fire-and-forget: all SES I/O runs in a background thread ─────────────
    def _send_bg():
        try:
            cached = _build_daily_email_cache(
                indices, sectors, vix_data, gold, sentiment, events,
                gainers, losers, summaries_us, summaries_cn, today_str, today_iso, lang,
            )
            recipients = [{"email": email, "lang": lang, "token": ""}]
            import boto3
            ses = boto3.client("ses", region_name="us-east-1")
            sent, errors = _ses_send_to_recipients(ses, recipients, cached, base_url, SES_FROM)
            log.info("send-daily-email bg: sent=%d errors=%d to=%s", sent, errors, email)
        except Exception:
            log.exception("send-daily-email bg: unhandled error for %s", email)

    threading.Thread(target=_send_bg, daemon=True, name=f"email-{email[:12]}").start()
    return jsonify({"ok": True, "queued": True, "email": email}), 202


# ---------------------------------------------------------------------------
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
        # Composite key (date, ticker) — a repeated ticker in the source list
        # would otherwise reject the entire snapshot batch.
        with table.batch_writer(overwrite_by_pkeys=["date", "ticker"]) as batch:
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
    """Fetch live price + day_chg/week_chg/month_chg for all heatmap tickers via yfinance batch download."""
    import yfinance as yf
    from ystocker.heatmap_meta import HEATMAP_META

    tickers_list = list(HEATMAP_META.keys())
    stocks = []
    try:
        # Fetch 35 trading days to support 1M color mode (21 trading days)
        data   = yf.download(
            tickers_list, period="35d", interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
        closes = data["Close"]
        for ticker in tickers_list:
            meta = HEATMAP_META[ticker]
            try:
                closes_col = closes[ticker] if ticker in closes.columns else None
                if closes_col is None:
                    continue
                closes_series = closes_col.dropna()
                vals  = closes_series.tolist()
                last  = float(vals[-1]) if vals else None
                price = round(last, 2) if last is not None else None
                day_chg = round((vals[-1] - vals[-2]) / vals[-2] * 100, 2) if len(vals) >= 2 else None

                # Week change (5 trading days ago)
                week_close  = None
                month_close = None
                try:
                    if len(closes_series) >= 6:
                        week_close  = float(closes_series.iloc[-6])
                    if len(closes_series) >= 22:
                        month_close = float(closes_series.iloc[-22])
                except Exception:
                    pass

                week_chg  = round((last - week_close)  / week_close  * 100, 2) if week_close  and week_close  > 0 and last is not None else None
                month_chg = round((last - month_close) / month_close * 100, 2) if month_close and month_close > 0 and last is not None else None
            except Exception:
                price, day_chg, week_chg, month_chg = None, None, None, None
            stocks.append({
                "ticker":    ticker,
                "name":      meta["name"],
                "sector":    meta["sector"],
                "price":     price,
                "day_chg":   day_chg,
                "week_chg":  week_chg,
                "month_chg": month_chg,
                "mkt_cap":   meta.get("mkt_cap_b"),  # use static approximate value
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
    log.info("API heatmap: date=%s", date_str)

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
    log.info("API heatmap/snapshot: triggered")

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


# ---------------------------------------------------------------------------
# Daily email auto-broadcast scheduler  (post-close on US trading days)
# ---------------------------------------------------------------------------
#
# Trading-day helpers are exposed at module level so the broadcast loop and
# _do_auto_broadcast() can share the same definition.  The Daily Markets
# Report only fires when the US stock market was open that day — weekends
# and US-market holidays are skipped, since fresh data is only available
# on actual trading days.


def _us_market_holidays(year: int) -> set:
    """
    Return a set of date objects for fixed-date US stock market holidays
    in the given year, with weekend observance applied.
    """
    from datetime import date as _date, timedelta as _td
    holidays: set = set()

    # New Year's Day — Jan 1 (with weekend observance)
    d = _date(year, 1, 1)
    if d.weekday() < 5:
        holidays.add(d)
    elif d.weekday() == 6:  # Sun → observed Mon
        holidays.add(d + _td(days=1))

    # MLK Day — 3rd Monday of January
    d = _date(year, 1, 1)
    mondays = 0
    while mondays < 3:
        if d.weekday() == 0:
            mondays += 1
            if mondays == 3:
                break
        d += _td(days=1)
    holidays.add(d)

    # Presidents' Day — 3rd Monday of February
    d = _date(year, 2, 1)
    mondays = 0
    while mondays < 3:
        if d.weekday() == 0:
            mondays += 1
            if mondays == 3:
                break
        d += _td(days=1)
    holidays.add(d)

    # Good Friday — 2 days before Easter Sunday (Anonymous Gregorian algorithm)
    a = year % 19
    b, c = divmod(year, 100)
    d_v, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d_v - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = _date(year, month, day)
    holidays.add(easter - _td(days=2))  # Good Friday

    # Memorial Day — last Monday of May
    d = _date(year, 5, 31)
    while d.weekday() != 0:
        d -= _td(days=1)
    holidays.add(d)

    # Juneteenth — Jun 19 (with weekend observance)
    d = _date(year, 6, 19)
    if d.weekday() == 5:
        holidays.add(d - _td(days=1))
    elif d.weekday() == 6:
        holidays.add(d + _td(days=1))
    else:
        holidays.add(d)

    # Independence Day — Jul 4 (with weekend observance)
    d = _date(year, 7, 4)
    if d.weekday() == 5:
        holidays.add(d - _td(days=1))
    elif d.weekday() == 6:
        holidays.add(d + _td(days=1))
    else:
        holidays.add(d)

    # Labor Day — 1st Monday of September
    d = _date(year, 9, 1)
    while d.weekday() != 0:
        d += _td(days=1)
    holidays.add(d)

    # Thanksgiving — 4th Thursday of November
    d = _date(year, 11, 1)
    thursdays = 0
    while thursdays < 4:
        if d.weekday() == 3:
            thursdays += 1
            if thursdays == 4:
                break
        d += _td(days=1)
    holidays.add(d)

    # Christmas — Dec 25 (with weekend observance)
    d = _date(year, 12, 25)
    if d.weekday() == 5:
        holidays.add(d - _td(days=1))
    elif d.weekday() == 6:
        holidays.add(d + _td(days=1))
    else:
        holidays.add(d)

    return holidays


def _is_us_trading_day(dt_obj) -> bool:
    """
    Return True iff `dt_obj` (a `date`) is a US stock market trading day —
    a weekday that is not a US-market holiday.
    """
    if dt_obj.weekday() >= 5:  # Sat / Sun
        return False
    return dt_obj not in _us_market_holidays(dt_obj.year)


def _do_auto_broadcast() -> None:
    """
    Collect market data from in-memory caches, get/generate AI summaries,
    and send the daily report email to all active subscribers.

    Only runs on US stock market trading days — if the market was closed
    today (weekend or holiday) the broadcast is skipped, since fresh
    market data is only available while the market is open / freshly closed.
    Called by the scheduler shortly after the US market close (16:45 ET).
    """
    import datetime as _dt_mod
    import time as _time_mod

    # ── Trading-day guard ────────────────────────────────────────────────────
    # Conservative ET offset (-5 = EST) works for both EST and EDT since the
    # broadcast fires at 16:45 ET, well before midnight in either zone.
    _et_now = _dt_mod.datetime.utcnow() + _dt_mod.timedelta(hours=-5)
    _today_et = _et_now.date()
    if not _is_us_trading_day(_today_et):
        log.info("Auto-broadcast: %s is not a US trading day (market closed) — skipping daily report",
                 _today_et)
        return

    SES_FROM = os.environ.get("SES_FROM_EMAIL")
    if not SES_FROM:
        log.warning("Auto-broadcast: SES_FROM_EMAIL not configured, skipping")
        return

    GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
    base_url   = os.environ.get("APP_BASE_URL", "https://ystocker.com").rstrip("/")
    today_str  = _dt_mod.date.today().strftime("%B %d, %Y")
    today_iso  = _dt_mod.date.today().isoformat()

    # ── Dedup guard: atomic DynamoDB lock prevents duplicate sends when
    # gunicorn runs multiple workers (each calls create_app() and starts its
    # own scheduler thread).  Only the first process to write the sentinel
    # item proceeds; the rest skip immediately.
    _dedup_tbl = _get_summaries_table()
    if _dedup_tbl:
        try:
            from botocore.exceptions import ClientError as _ClientError
            _dedup_tbl.put_item(
                Item={
                    "date": today_iso,
                    "lang_market": "broadcast_sent",
                    "ts": int(_time_mod.time()),
                    "ttl": int(_time_mod.time()) + 7 * 24 * 3600,
                },
                ConditionExpression="attribute_not_exists(lang_market)",
            )
        except Exception as _exc:
            if hasattr(_exc, "response") and \
                    _exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                log.info("Auto-broadcast: already sent for %s — skipping duplicate", today_iso)
                return
            log.warning("Auto-broadcast: dedup guard error (proceeding anyway): %s", _exc)

    log.info("Auto-broadcast: starting daily report for %s", today_iso)

    # ── Read market data from in-memory caches ───────────────────────────────
    with _MARKETS_CACHE_LOCK:
        markets_entry = (_MARKETS_CACHE.get("data") or {})
    markets_d = markets_entry.get("data") or {}
    indices  = markets_d.get("indices", {})
    sectors  = markets_d.get("sectors", [])
    vix_data = markets_d.get("vix", {})

    with _FG_CACHE_LOCK:
        fg_d = ((_FG_CACHE.get("data") or {}).get("data") or {})

    with _PCR_CACHE_LOCK:
        pcr_d = ((_PCR_CACHE.get("data") or {}).get("data") or {})

    with _AAII_CACHE_LOCK:
        aaii_entry = ((_AAII_CACHE.get("data") or {}).get("data") or {})
    aaii_latest = (aaii_entry.get("latest") or {})

    with _GOLD_RATIOS_CACHE_LOCK:
        gold_d = ((_GOLD_RATIOS_CACHE.get("data") or {}).get("data") or {})

    with _ECON_CACHE_LOCK:
        econ_d = ((_ECON_CACHE.get("data") or {}).get("data") or {})
    events  = (econ_d.get("events") or [])

    with _MOVERS_CACHE_LOCK:
        movers_d = ((_MOVERS_CACHE.get("data") or {}).get("data") or {})
    gainers = (movers_d.get("gainers") or [])
    losers  = (movers_d.get("losers") or [])

    sentiment = {"fg": fg_d, "pcr": pcr_d, "aaii": aaii_latest}

    # ── Get or generate AI summaries ─────────────────────────────────────────
    summaries_us: dict = {}   # {"en": text, "zh": text} — US market commentary
    summaries_cn: dict = {}   # {"en": text, "zh": text} — CN market commentary

    market_data_for_prompt = {
        "indices": indices, "vix": vix_data, "sectors": sectors,
        "fg": fg_d, "pcr": pcr_d, "aaii": aaii_latest,
        "gainers": gainers, "losers": losers,
        "events": [e for e in events if e.get("impact") == "High"][:8],
    }

    tbl = _get_summaries_table()

    def _get_or_generate_summary(lang: str, market: str, prompt_fn) -> str:
        """Load from cache/DynamoDB or generate via Gemini. Returns summary text."""
        cache_key = f"{lang}_{market}"
        with _DAILY_SUMMARY_CACHE_LOCK:
            cached_entry = _DAILY_SUMMARY_CACHE.get(cache_key, {})
        if cached_entry and _time_mod.time() - cached_entry.get("ts", 0) < _DAILY_SUMMARY_CACHE_TTL:
            return cached_entry["data"].get("summary", "")
        if tbl:
            try:
                item = tbl.get_item(Key={"date": today_iso, "lang_market": cache_key}).get("Item")
                if item and item.get("summary"):
                    result = {"summary": item["summary"], "generated_at": item.get("generated_at", "")}
                    with _DAILY_SUMMARY_CACHE_LOCK:
                        _DAILY_SUMMARY_CACHE[cache_key] = {"ts": _time_mod.time(), "data": result}
                    return item["summary"]
            except Exception as exc:
                log.warning("Auto-broadcast: DynamoDB read failed (%s): %s", cache_key, exc)
        if not GEMINI_KEY:
            return ""
        try:
            from google import genai as _genai
            _client = _genai.Client(api_key=GEMINI_KEY)
            resp = _client.models.generate_content(model="gemini-2.5-flash", contents=prompt_fn())
            summary_text = resp.text.strip()
            generated_at = _dt_mod.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            result = {"summary": summary_text, "generated_at": generated_at}
            with _DAILY_SUMMARY_CACHE_LOCK:
                _DAILY_SUMMARY_CACHE[cache_key] = {"ts": _time_mod.time(), "data": result}
            if tbl:
                try:
                    tbl.put_item(Item={
                        "date": today_iso, "lang_market": cache_key,
                        "summary": summary_text, "generated_at": generated_at,
                        "ttl": int(_time_mod.time()) + 90 * 24 * 3600,
                    })
                except Exception as exc:
                    log.warning("Auto-broadcast: DynamoDB write failed (%s): %s", cache_key, exc)
            return summary_text
        except Exception as exc:
            log.warning("Auto-broadcast: Gemini failed (%s_%s): %s", lang, market, exc)
            return ""

    for lang in ("en", "zh"):
        idx = market_data_for_prompt.get("indices", {})

        # ── US market snapshot ────────────────────────────────────────────
        def _us_prompt(lang=lang):
            lines = [f"Date: {today_iso}"]
            for key, label in [("spx","S&P 500"), ("ixic","Nasdaq"), ("dji","Dow Jones")]:
                d = idx.get(key, {})
                if d.get("current"):
                    chg = f"{d['day_chg']:+.2f}%" if d.get("day_chg") is not None else "—"
                    lines.append(f"{label}: {d['current']:,.2f} ({chg}) YTD {d.get('ytd','—')}%")
            vix = market_data_for_prompt.get("vix", {})
            if vix.get("current"):
                lines.append(f"VIX: {vix['current']:.2f}")
            fg = market_data_for_prompt.get("fg", {})
            if fg.get("score"):
                lines.append(f"Fear & Greed: {fg['score']:.0f} ({fg.get('rating','')})")
            pcr = market_data_for_prompt.get("pcr", {})
            if pcr.get("current"):
                lines.append(f"Put/Call Ratio: {pcr['current']:.2f}")
            aaii = market_data_for_prompt.get("aaii", {})
            if aaii.get("bullish"):
                lines.append(f"AAII: Bull {aaii['bullish']:.1f}% Bear {aaii.get('bearish',0):.1f}%")
            secs = market_data_for_prompt.get("sectors", [])
            if secs:
                top = sorted(secs, key=lambda s: s.get("day_chg", 0) or 0)
                lines.append(f"Sectors — best: {top[-1]['label']} {top[-1].get('day_chg',0):+.2f}%, worst: {top[0]['label']} {top[0].get('day_chg',0):+.2f}%")
            snapshot = "\n".join(lines)
            if lang == "zh":
                return (
                    "你是一位简洁的金融分析师。请根据以下数据，用中文撰写一份简短（3-4段）的美国市场每日评论。"
                    "重点分析标普500、纳斯达克、道琼斯、VIX、PCR、AAII情绪及美国板块表现。"
                    "语言要直接、有洞察力、保持中立。不要使用Markdown标题。\n\n"
                    f"市场快照：\n{snapshot}"
                )
            return (
                "You are a concise financial analyst. Write a brief (3-4 paragraph) daily US markets commentary "
                "focusing on S&P 500, Nasdaq, Dow, VIX, Put/Call Ratio, AAII sentiment, and US sectors. "
                "Be direct, insightful, and neutral. Use plain English, no markdown headers.\n\n"
                f"Market snapshot:\n{snapshot}"
            )

        # ── CN market snapshot ────────────────────────────────────────────
        def _cn_prompt(lang=lang):
            lines = [f"Date: {today_iso}"]
            for key, label in [("sse","Shanghai SSE"), ("csi500","CSI 500"), ("twii","Taiwan TWII"), ("kospi","KOSPI")]:
                d = idx.get(key, {})
                if d.get("current"):
                    chg = f"{d['day_chg']:+.2f}%" if d.get("day_chg") is not None else "—"
                    lines.append(f"{label}: {d['current']:,.2f} ({chg})")
            snapshot = "\n".join(lines)
            if lang == "zh":
                return (
                    "你是一位简洁的金融分析师。请根据以下数据，用中文撰写一份简短（2-3段）的中国及亚太市场每日评论。"
                    "重点分析上证指数、中证500、台湾及韩国市场表现。"
                    "语言要直接、有洞察力、保持中立。不要使用Markdown标题。\n\n"
                    f"市场快照：\n{snapshot}"
                )
            return (
                "You are a concise financial analyst. Write a brief (2-3 paragraph) daily CN & Asia-Pacific "
                "markets commentary focusing on Shanghai SSE, CSI 500, Taiwan, and South Korea. "
                "Be direct, insightful, and neutral. Use plain English, no markdown headers.\n\n"
                f"Market snapshot:\n{snapshot}"
            )

        summaries_us[lang] = _get_or_generate_summary(lang, "us", _us_prompt)
        summaries_cn[lang] = _get_or_generate_summary(lang, "cn", _cn_prompt)

    log.info("Auto-broadcast: summaries ready — us_en=%d us_zh=%d cn_en=%d cn_zh=%d",
             len(summaries_us.get("en", "")), len(summaries_us.get("zh", "")),
             len(summaries_cn.get("en", "")), len(summaries_cn.get("zh", "")))

    # ── Get subscribers ───────────────────────────────────────────────────────
    sub_tbl = _get_subscribers_table()
    if not sub_tbl:
        log.warning("Auto-broadcast: subscribers table unavailable, aborting")
        return

    try:
        from boto3.dynamodb.conditions import Attr as _Attr
        recipients = [
            {"email": item["email"], "lang": item.get("lang", "en"),
             "token": item.get("unsubscribe_token", "")}
            for item in sub_tbl.scan(FilterExpression=_Attr("active").eq(True)).get("Items", [])
        ]
    except Exception as exc:
        log.warning("Auto-broadcast: failed to fetch subscribers: %s", exc)
        return

    if not recipients:
        log.info("Auto-broadcast: no active subscribers, done")
        return

    # ── Build and send ────────────────────────────────────────────────────────
    cached_templates = _build_daily_email_cache(
        indices, sectors, vix_data, gold_d, sentiment, events,
        gainers, losers, summaries_us, summaries_cn, today_str, today_iso,
    )

    try:
        import boto3
        ses = boto3.client("ses", region_name="us-east-1")
        sent, errors = _ses_send_to_recipients(ses, recipients, cached_templates, base_url, SES_FROM)
        log.info("Auto-broadcast: sent=%d, failed=%d", sent, len(errors))
        if errors:
            log.warning("Auto-broadcast: failed recipients: %s", errors)
    except Exception as exc:
        log.exception("Auto-broadcast: unhandled error during send: %s", exc)


def _daily_broadcast_loop() -> None:
    """
    Background thread: sleep until the next US market post-close window
    (16:45 ET on the next trading day), then call _do_auto_broadcast().

    The Daily Markets Report is only generated on US stock market trading
    days — weekends and US-market holidays are skipped, since fresh market
    data is only available while the market is open.  Firing at 16:45 ET
    (15 min after close, after the heatmap snapshot at 16:30 ET) ensures
    the in-memory caches reflect a finalised trading day.
    """
    import datetime as _dt_mod

    BROADCAST_HOUR_ET   = 16
    BROADCAST_MINUTE_ET = 45
    # Conservative ET offset (-5 = EST).  Using EST year-round means during
    # EDT (summer) the broadcast fires at 17:45 EDT = 21:45 UTC — still
    # safely after the 16:00 EDT close.  This keeps the math timezone-free.
    ET_OFFSET_HOURS = -5

    def _seconds_until_next_broadcast() -> float:
        now_utc = _dt_mod.datetime.utcnow()
        now_et  = now_utc + _dt_mod.timedelta(hours=ET_OFFSET_HOURS)
        target  = now_et.replace(
            hour=BROADCAST_HOUR_ET, minute=BROADCAST_MINUTE_ET,
            second=0, microsecond=0,
        )
        if now_et >= target:
            target += _dt_mod.timedelta(days=1)
        # Skip non-trading days (weekends + holidays)
        while not _is_us_trading_day(target.date()):
            target += _dt_mod.timedelta(days=1)
        return (target - now_et).total_seconds()

    log.info(
        "Daily broadcast scheduler started — will fire at %02d:%02d ET on US trading days only",
        BROADCAST_HOUR_ET, BROADCAST_MINUTE_ET,
    )

    while True:
        secs = _seconds_until_next_broadcast()
        log.info("Daily broadcast: next fire in %.1f h", secs / 3600)
        time.sleep(secs)

        # Re-check trading day at fire time (in case the wake crossed a
        # boundary or a holiday wasn't accounted for).
        now_et = _dt_mod.datetime.utcnow() + _dt_mod.timedelta(hours=ET_OFFSET_HOURS)
        report_date = now_et.date()

        if not _is_us_trading_day(report_date):
            log.info("Daily broadcast: skipping %s (market closed today)", report_date)
            time.sleep(70)
            continue

        try:
            _do_auto_broadcast()
        except Exception:
            log.exception("Daily broadcast: unhandled error")
        # Sleep 70 s to clear the fire boundary before recalculating,
        # ensuring _seconds_until_next_broadcast returns ~24 h not ~0 s.
        time.sleep(70)


def _start_daily_broadcast_scheduler() -> None:
    t = threading.Thread(target=_daily_broadcast_loop, daemon=True, name="daily-broadcast")
    t.start()


# ---------------------------------------------------------------------------
# Daily summary pre-generator
# Fires at server startup (2-min delay) and at midnight ET each day so the
# /daily page always returns cached summaries instead of calling Gemini live.
# ---------------------------------------------------------------------------

def _do_pregen_daily_summaries() -> None:
    """
    Generate all 4 daily summaries (en_us / zh_us / en_cn / zh_cn) from
    the in-memory market caches and store in DynamoDB + _DAILY_SUMMARY_CACHE.

    If today's summary is already in DynamoDB or memory it is skipped.
    Runs in 1-2 minutes (4 Gemini calls × ~15-30 s each, sequentially).
    """
    import datetime as _dt_mod
    import time as _time_mod

    GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_KEY:
        log.debug("Daily pre-gen: no GEMINI_API_KEY, skipping")
        return

    today_iso = _dt_mod.date.today().isoformat()
    tbl       = _get_summaries_table()
    log.info("Daily pre-gen: generating summaries for %s", today_iso)

    # ── Collect market data from caches (same as _do_auto_broadcast) ─────────
    with _MARKETS_CACHE_LOCK:
        markets_d = ((_MARKETS_CACHE.get("data") or {}).get("data") or {})
    with _FG_CACHE_LOCK:
        fg_d = ((_FG_CACHE.get("data") or {}).get("data") or {})
    with _PCR_CACHE_LOCK:
        pcr_d = ((_PCR_CACHE.get("data") or {}).get("data") or {})
    with _AAII_CACHE_LOCK:
        aaii_d = (((_AAII_CACHE.get("data") or {}).get("data") or {}).get("latest") or {})
    with _MOVERS_CACHE_LOCK:
        movers_d = ((_MOVERS_CACHE.get("data") or {}).get("data") or {})

    idx     = markets_d.get("indices", {})
    vix     = markets_d.get("vix", {})
    sectors = markets_d.get("sectors", [])
    gainers = movers_d.get("gainers", [])
    losers  = movers_d.get("losers",  [])

    def _make_prompt(lang: str, market: str) -> str:
        lines = [f"Date: {today_iso}"]
        if market == "us":
            for key, label in [("spx","S&P 500"),("ixic","Nasdaq"),("dji","Dow Jones")]:
                d = idx.get(key, {})
                if d.get("current"):
                    chg = f"{d['day_chg']:+.2f}%" if d.get("day_chg") is not None else "—"
                    lines.append(f"{label}: {d['current']:,.2f} ({chg}) YTD {d.get('ytd','—')}%")
            if vix.get("current"):
                lines.append(f"VIX: {vix['current']:.2f}")
            if fg_d.get("score"):
                lines.append(f"Fear & Greed: {fg_d['score']:.0f} ({fg_d.get('rating','')})")
            if pcr_d.get("current"):
                lines.append(f"Put/Call Ratio: {pcr_d['current']:.2f}")
            if aaii_d.get("bullish"):
                lines.append(f"AAII: Bull {aaii_d['bullish']:.1f}% Bear {aaii_d.get('bearish',0):.1f}%")
            if sectors:
                top = sorted(sectors, key=lambda s: s.get("day_chg", 0) or 0)
                lines.append(f"Sectors — best: {top[-1]['label']} {top[-1].get('day_chg',0):+.2f}%, "
                             f"worst: {top[0]['label']} {top[0].get('day_chg',0):+.2f}%")
            if gainers:
                lines.append("Top gainers: " + ", ".join(f"{g['ticker']} {g['day_chg']:+.2f}%" for g in gainers[:3]))
            if losers:
                lines.append("Top losers: "  + ", ".join(f"{l['ticker']} {l['day_chg']:+.2f}%" for l in losers[:3]))
            snap = "\n".join(lines)
            if lang == "zh":
                return ("你是一位简洁的金融分析师。请根据以下数据，用中文撰写一份简短（3-4段）的美国市场每日评论。"
                        "重点分析标普500、纳斯达克、道琼斯、VIX、PCR、AAII情绪及板块表现。"
                        "语言要直接、有洞察力、保持中立。不要使用Markdown标题。\n\n"
                        f"市场快照：\n{snap}")
            return ("You are a concise financial analyst. Write a brief (3-4 paragraph) daily US markets commentary "
                    "focusing on S&P 500, Nasdaq, Dow, VIX, Put/Call Ratio, AAII sentiment, and US sectors. "
                    "Be direct, insightful, and neutral. Use plain English, no markdown headers.\n\n"
                    f"Market snapshot:\n{snap}")
        else:  # cn
            for key, label in [("sse","Shanghai SSE"),("csi500","CSI 500"),("twii","Taiwan TWII"),("kospi","KOSPI")]:
                d = idx.get(key, {})
                if d.get("current"):
                    chg = f"{d['day_chg']:+.2f}%" if d.get("day_chg") is not None else "—"
                    lines.append(f"{label}: {d['current']:,.2f} ({chg})")
            snap = "\n".join(lines)
            if lang == "zh":
                return ("你是一位简洁的金融分析师。请根据以下数据，用中文撰写一份简短（2-3段）的中国及亚太市场每日评论。"
                        "重点分析上证指数、中证500、台湾及韩国市场表现。"
                        "语言要直接、有洞察力、保持中立。不要使用Markdown标题。\n\n"
                        f"市场快照：\n{snap}")
            return ("You are a concise financial analyst. Write a brief (2-3 paragraph) daily CN & Asia-Pacific "
                    "markets commentary focusing on Shanghai SSE, CSI 500, Taiwan, and South Korea. "
                    "Be direct, insightful, and neutral. Use plain English, no markdown headers.\n\n"
                    f"Market snapshot:\n{snap}")

    # ── Generate each combination ─────────────────────────────────────────────
    for lang in ("en", "zh"):
        for market in ("us", "cn"):
            cache_key = f"{lang}_{market}"

            # Skip if already in memory cache (fresh)
            with _DAILY_SUMMARY_CACHE_LOCK:
                entry = _DAILY_SUMMARY_CACHE.get(cache_key, {})
            if entry and _time_mod.time() - entry.get("ts", 0) < _DAILY_SUMMARY_CACHE_TTL:
                log.debug("Daily pre-gen: %s already in memory cache, skipping", cache_key)
                continue

            # Skip if already in DynamoDB for today
            if tbl:
                try:
                    item = tbl.get_item(Key={"date": today_iso, "lang_market": cache_key}).get("Item")
                    if item and item.get("summary"):
                        result = {"summary": item["summary"],
                                  "generated_at": item.get("generated_at", ""),
                                  "from_cache": True}
                        with _DAILY_SUMMARY_CACHE_LOCK:
                            _DAILY_SUMMARY_CACHE[cache_key] = {"ts": _time_mod.time(), "data": result}
                        log.info("Daily pre-gen: %s loaded from DynamoDB", cache_key)
                        continue
                except Exception as exc:
                    log.warning("Daily pre-gen: DynamoDB read failed (%s): %s", cache_key, exc)

            # Generate via Gemini
            try:
                from google import genai as _genai
                _client = _genai.Client(api_key=GEMINI_KEY)
                resp    = _client.models.generate_content(
                    model="gemini-2.5-flash", contents=_make_prompt(lang, market))
                summary_text = resp.text.strip()
                generated_at = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                result = {"summary": summary_text, "generated_at": generated_at}
                with _DAILY_SUMMARY_CACHE_LOCK:
                    _DAILY_SUMMARY_CACHE[cache_key] = {"ts": _time_mod.time(), "data": result}
                if tbl:
                    tbl.put_item(Item={
                        "date": today_iso, "lang_market": cache_key,
                        "summary": summary_text, "generated_at": generated_at,
                        "ttl": int(_time_mod.time()) + 90 * 24 * 3600,
                    })
                log.info("Daily pre-gen: generated %s (%d chars)", cache_key, len(summary_text))
            except Exception as exc:
                log.warning("Daily pre-gen: Gemini failed for %s: %s", cache_key, exc)


def _pregen_daily_loop(app=None) -> None:
    """
    Background thread:
      1. On startup — wait 2 minutes (let caches warm), then pre-generate.
      2. Daily at 00:05 ET — pre-generate for the new day so morning visitors
         see instant summaries (no Gemini latency on first visit).
    """
    import datetime as _dt_mod

    PRE_GEN_HOUR_ET   = 0
    PRE_GEN_MINUTE_ET = 5
    ET_OFFSET_HOURS   = -5   # conservative EST, works for EDT too

    def _secs_until_midnight_pregen() -> float:
        now_et  = _dt_mod.datetime.utcnow() + _dt_mod.timedelta(hours=ET_OFFSET_HOURS)
        target  = now_et.replace(hour=PRE_GEN_HOUR_ET, minute=PRE_GEN_MINUTE_ET,
                                 second=0, microsecond=0)
        if now_et >= target:
            target += _dt_mod.timedelta(days=1)
        return (target - now_et).total_seconds()

    log.info("Daily pre-gen scheduler started")

    # Startup warm-up: wait 2 minutes, then generate if today's not cached
    time.sleep(120)
    try:
        _do_pregen_daily_summaries()
    except Exception:
        log.exception("Daily pre-gen: startup warm-up failed")
    try:
        _do_pregen_market_briefs(app)
    except Exception:
        log.exception("Brief pre-gen: startup warm-up failed")

    # Nightly loop
    while True:
        secs = _secs_until_midnight_pregen()
        log.info("Daily pre-gen: next midnight fire in %.1f h", secs / 3600)
        time.sleep(secs)
        try:
            _do_pregen_daily_summaries()
        except Exception:
            log.exception("Daily pre-gen: nightly fire failed")
        try:
            _do_pregen_market_briefs(app)
        except Exception:
            log.exception("Brief pre-gen: nightly fire failed")
        time.sleep(70)


def _start_daily_pregen_scheduler(app=None) -> None:
    t = threading.Thread(target=_pregen_daily_loop, args=(app,),
                         daemon=True, name="daily-pregen")
    t.start()
    log.info("Daily summary pre-gen scheduler started")
