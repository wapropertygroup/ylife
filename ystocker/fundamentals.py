"""
ystocker.fundamentals
~~~~~~~~~~~~~~~~~~~~~
The I/O around :mod:`ystocker.xbrl`: which SEC filer a ticker is, that filer's
companyfacts document, weekly prices for the valuation ratios, and a per-ticker
disk cache the request path only ever reads.

Cost of one build
-----------------
* **One EDGAR request.** Companyfacts is the company's whole XBRL history in a
  single document: 3-5 MB for most large caps, 8 MB for JPMorgan, and about a
  tenth of a second to parse (measured on the box, 2026-10-03). It goes through
  ``sec13f.edgar_get``, so it shares the 13F refresher's throttle and breaker --
  SEC's rate limit is per client, not per module.
* **One Yahoo request.** Weekly closes since 2008, with the split history in
  the same response (``actions=True``), which is what re-bases per-share
  figures. Not dividend-adjusted: a P/E is a price over earnings, and an
  adjusted series re-prices history after every dividend.
* **The ticker map**, ``company_tickers.json`` (800 KB), at most once a week.

So a build is cheap, and fundamentals change four times a year; the cache is
good for :data:`TTL_SECONDS` and a stale copy is still served while a rebuild
runs.

The request path never fetches
------------------------------
``/api/fundamentals/<t>`` reads :func:`peek` and nothing else. A cold ticker
answers 202 and :func:`kick` starts one background build, deduplicated per
symbol and drawn from one global budget -- the per-symbol guard bounds nothing
when a reader clicks through twenty different tickers, which is what readers
do (``dca_history`` measured it). The page polls with a bounded loop.

Failure is remembered, briefly
------------------------------
A failed build leaves a marker beside the cache for :data:`FAIL_TTL_SECONDS`,
read by every worker, so the poll ends on "could not load" instead of
spinning until its attempt cap and the next visitor does not immediately
re-send the request that just failed. A ticker that is not an SEC filer at all
is not a failure: that is an answer, cached for a week like any other.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests

from ystocker import fetchguard, sec13f, statements, xbrl

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "cache" / "fundamentals"
TICKER_MAP_FILE = Path(__file__).parent.parent / "cache" / "sec_company_tickers.json"

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

#: Bump when the payload's shape changes. A bump costs one EDGAR and one Yahoo
#: request per ticker on its next view, not a sweep: nothing pre-builds.
CACHE_VER = "v1"

TTL_SECONDS = fetchguard.env_float("FUNDAMENTALS_TTL_HOURS", 12.0, 1.0) * 3600
#: A build that got the filings but not the prices is re-tried sooner.
PARTIAL_TTL_SECONDS = 3600.0
#: "Not an SEC filer" and "files no XBRL" are answers, and do not change daily.
UNAVAILABLE_TTL_SECONDS = 7 * 86400.0
FAIL_TTL_SECONDS = 600.0
TICKER_MAP_TTL_SECONDS = 7 * 86400.0

#: Weekly prices from here. Facts start with the 2009-2011 XBRL phase-in, and a
#: split before the oldest filing re-bases nothing.
PRICE_START = "2008-01-01"

#: Builds running at once, and the floor between starting two. One build is
#: one EDGAR and one Yahoo request, so a single build is gentle; the floor is
#: what bounds a script walking ticker after ticker, since any symbol can be
#: requested -- at most twenty Yahoo requests a minute from this page however
#: it is driven.
MAX_INFLIGHT_BUILDS = 2
BUILD_MIN_GAP_SECONDS = 3.0

_SYMBOL_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.\-^=]{0,14}$")


class BuildError(RuntimeError):
    """A build that failed for a reason worth retrying later."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def normalise(ticker: str) -> Optional[str]:
    """An upper-cased Yahoo-style symbol, or ``None`` for anything else."""
    symbol = (ticker or "").strip().upper()
    return symbol if _SYMBOL_RE.match(symbol) else None


def _safe_name(symbol: str) -> str:
    """A file name for *symbol*. ``^`` and ``=`` become ``_`` rather than being
    dropped, so ``^GSPC`` cannot share a cache file with a ticker ``GSPC``."""
    return "".join(c if c.isalnum() or c in "-." else "_" for c in symbol.upper())[:24] or "_"


def _path(symbol: str) -> Path:
    return CACHE_DIR / f"{_safe_name(symbol)}.json"


def _fail_path(symbol: str) -> Path:
    return CACHE_DIR / f"{_safe_name(symbol)}.fail.json"


_disk_lock = threading.Lock()


def _write_json(path: Path, payload: Any) -> None:
    """Atomic temp-file + replace, matching every other cache writer here."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _disk_lock:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(payload, handle, separators=(",", ":"))
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail a page
        log.warning("fundamentals: could not write %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Ticker -> CIK
# ---------------------------------------------------------------------------

_map_lock = threading.Lock()
_map: Optional[dict[str, tuple[int, str]]] = None
_map_loaded_at = 0.0


def _parse_map(raw: Any) -> dict[str, tuple[int, str]]:
    rows = raw.values() if isinstance(raw, dict) else (raw or [])
    out: dict[str, tuple[int, str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("ticker") or "").strip().upper()
        cik = item.get("cik_str")
        if symbol and isinstance(cik, int) and symbol not in out:
            out[symbol] = (cik, str(item.get("title") or ""))
    return out


def ticker_map() -> dict[str, tuple[int, str]]:
    """SEC's ticker -> (CIK, name) map: memory, then disk, then EDGAR.

    Background use only -- it may fetch. A refresh that fails keeps whatever
    copy exists, however old: a renamed ticker is a rare miss, and an empty
    map would turn every company on the site into "not an SEC filer".
    """
    global _map, _map_loaded_at
    with _map_lock:
        if _map is not None and time.time() - _map_loaded_at < TICKER_MAP_TTL_SECONDS:
            return _map
    disk: Optional[dict[str, tuple[int, str]]] = None
    try:
        age = time.time() - TICKER_MAP_FILE.stat().st_mtime
        disk = _parse_map(json.loads(TICKER_MAP_FILE.read_text()))
    except (OSError, ValueError):
        age = math.inf
    fresh = disk if disk and age < TICKER_MAP_TTL_SECONDS else None
    if fresh is None:
        try:
            resp = sec13f.edgar_get(TICKER_MAP_URL)
            raw = resp.json()
            parsed = _parse_map(raw)
            if len(parsed) > 1000:          # a truncated answer is not a map
                _write_json(TICKER_MAP_FILE, raw)
                fresh = parsed
        except (fetchguard.CooldownActive, requests.RequestException, ValueError) as exc:
            log.warning("fundamentals: ticker map refresh failed: %s", exc)
    chosen = fresh or disk
    if chosen is None:
        raise BuildError("sec_unavailable", "no ticker map")
    with _map_lock:
        _map, _map_loaded_at = chosen, time.time()
    return chosen


def cik_for(symbol: str) -> Optional[tuple[int, str]]:
    """(CIK, filer name) for a symbol, or ``None`` if SEC lists no such ticker.

    Yahoo writes a share class with a hyphen (BRK-B) and so does SEC; a few
    feeds use a dot, so that spelling is tried too.
    """
    mapping = ticker_map()
    return mapping.get(symbol) or mapping.get(symbol.replace(".", "-"))


# ---------------------------------------------------------------------------
# Prices and splits
# ---------------------------------------------------------------------------

def _prices(symbol: str) -> tuple[Optional[list[tuple[date, float]]],
                                  Optional[list[tuple[date, float]]], Optional[str]]:
    """(splits, weekly closes, problem). Splits are ``None`` when unknown.

    ``None`` and ``[]`` are different answers and :func:`xbrl.build` treats
    them differently: no split history read means the per-share lines are
    withheld, where an empty one means there were no splits.
    """
    import yfinance as yf

    from ystocker import data as ydata

    try:
        fetchguard.guard(ydata.PROVIDER)
    except fetchguard.CooldownActive:
        return None, None, "cooldown"
    try:
        hist = yf.Ticker(symbol).history(start=PRICE_START, interval="1wk",
                                         auto_adjust=False, actions=True)
    except Exception as exc:  # noqa: BLE001 - yfinance raises a zoo of types
        if ydata._looks_rate_limited(exc):
            fetchguard.trip(ydata.PROVIDER, fetchguard.FETCH_RATE_LIMIT_COOLDOWN_SECONDS,
                            "yfinance rate limit (fundamentals)")
        log.info("fundamentals: price history failed for %s: %s", symbol, exc)
        return None, None, "error"
    if hist is None or hist.empty or "Close" not in hist:
        return None, None, "empty"
    closes: list[tuple[date, float]] = []
    for stamp, close in hist["Close"].items():
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            closes.append((stamp.date(), value))
    splits: list[tuple[date, float]] = []
    if "Stock Splits" in hist:
        for stamp, ratio in hist["Stock Splits"].items():
            try:
                value = float(ratio)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                splits.append((stamp.date(), value))
    return splits, closes, None


# ---------------------------------------------------------------------------
# Build, cache, read
# ---------------------------------------------------------------------------

def _stamped(payload: dict[str, Any], ttl: float) -> dict[str, Any]:
    payload["_ver"] = CACHE_VER
    payload["_ts"] = time.time()
    payload["_ttl"] = ttl
    return payload


#: Symbol shapes that are never a company: an index, a currency pair, a
#: future, a coin. Answered without asking anybody.
_NOT_COMPANY_RE = re.compile(r"^\^|=X$|=F$|-USD$|-USDT$")

#: Extra seconds added to the build budget after a Yahoo-statements build: it
#: is eight Yahoo requests where an EDGAR build is one.
YAHOO_BUILD_PENALTY_SECONDS = 8.0


def build(symbol: str) -> dict[str, Any]:
    """Fetch and assemble one ticker's payload. Raises :class:`BuildError`.

    EDGAR first. Only when SEC has nothing to chart -- not a registrant, no
    XBRL, no statements this module can read -- do Yahoo's statement tables
    stand in, and the payload's ``source`` says which it is.
    """
    if _NOT_COMPANY_RE.search(symbol):
        return _stamped({"ticker": symbol, "unavailable": "not_a_company"}, UNAVAILABLE_TTL_SECONDS)
    payload = _build_sec(symbol)
    if not payload.get("unavailable"):
        return payload
    fallback = _build_yahoo(symbol)
    if fallback.get("unavailable"):
        # Keep SEC's reason unless Yahoo's says more (not a company at all).
        if fallback["unavailable"] == "not_a_company":
            payload["unavailable"] = "not_a_company"
        return payload
    return fallback


def _build_sec(symbol: str) -> dict[str, Any]:
    entry = cik_for(symbol)
    if entry is None:
        return _stamped({"ticker": symbol, "unavailable": "not_sec_filer"}, UNAVAILABLE_TTL_SECONDS)
    cik, title = entry
    try:
        resp = sec13f.edgar_get(COMPANYFACTS_URL.format(cik=cik))
    except fetchguard.CooldownActive as exc:
        raise BuildError("sec_cooldown", str(exc)) from exc
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            # EDGAR has the filer and no XBRL for it: a fund, a trust, a shell.
            return _stamped({"ticker": symbol, "cik": cik, "entity": title,
                             "unavailable": "no_xbrl"}, UNAVAILABLE_TTL_SECONDS)
        raise BuildError("sec_unavailable", str(exc)) from exc
    except requests.RequestException as exc:
        raise BuildError("sec_unavailable", str(exc)) from exc
    try:
        doc = resp.json()
    except ValueError as exc:
        raise BuildError("sec_unreadable", str(exc)) from exc
    entity = str(doc.get("entityName") or title)
    if xbrl.choose_basis(doc) == (None, None):
        # Nothing to chart, so no price request either.
        return _stamped({"ticker": symbol, "cik": cik, "entity": entity,
                         "unavailable": "no_statements"}, UNAVAILABLE_TTL_SECONDS)

    splits, prices, price_problem = _prices(symbol)
    payload = xbrl.build(doc, splits=splits, prices=prices)
    del doc
    payload.update({"ticker": symbol, "cik": cik, "entity": entity, "source": "sec"})
    return _stamped(payload, _ttl(payload, price_problem))


def _ttl(payload: dict[str, Any], price_problem: Optional[str]) -> float:
    if payload.get("unavailable"):
        return UNAVAILABLE_TTL_SECONDS
    if price_problem:
        payload["prices"] = price_problem
        return PARTIAL_TTL_SECONDS
    return TTL_SECONDS


def _frame_dict(frame: Any) -> dict[str, dict[str, float]]:
    """A yfinance statement DataFrame as ``{row: {iso date: value}}``."""
    out: dict[str, dict[str, float]] = {}
    if frame is None or getattr(frame, "empty", True):
        return out
    for row in frame.index:
        cells: dict[str, float] = {}
        for col in frame.columns:
            try:
                value = float(frame.at[row, col])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                cells[col.date().isoformat()] = value
        if cells:
            out[str(row)] = cells
    return out


def _yahoo_frames(symbol: str) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, Any]]:
    """Yahoo's six statement tables and the quote metadata. Raises BuildError."""
    import yfinance as yf

    from ystocker import data as ydata

    try:
        fetchguard.guard(ydata.PROVIDER)
    except fetchguard.CooldownActive as exc:
        raise BuildError("yahoo_cooldown", str(exc)) from exc
    tk = yf.Ticker(symbol)
    try:
        info = tk.info or {}
        # No quote type is no such symbol: six statement requests would only
        # confirm it.
        kind = str(info.get("quoteType") or "").upper()
        if not kind or kind in statements.NOT_COMPANIES:
            return {}, info
        _penalise(YAHOO_BUILD_PENALTY_SECONDS)
        frames = {
            "income_q": _frame_dict(tk.quarterly_income_stmt),
            "income_a": _frame_dict(tk.income_stmt),
            "cash_q": _frame_dict(tk.quarterly_cashflow),
            "cash_a": _frame_dict(tk.cashflow),
            "balance_q": _frame_dict(tk.quarterly_balance_sheet),
            "balance_a": _frame_dict(tk.balance_sheet),
        }
    except Exception as exc:  # noqa: BLE001 - yfinance raises a zoo of types
        if ydata._looks_rate_limited(exc):
            fetchguard.trip(ydata.PROVIDER, fetchguard.FETCH_RATE_LIMIT_COOLDOWN_SECONDS,
                            "yfinance rate limit (fundamentals statements)")
            raise BuildError("yahoo_cooldown", str(exc)) from exc
        raise BuildError("yahoo_unavailable", str(exc)) from exc
    return frames, info


def _build_yahoo(symbol: str) -> dict[str, Any]:
    frames, info = _yahoo_frames(symbol)
    if not any(frames.values()):
        kind = str(info.get("quoteType") or "").upper()
        reason = "not_a_company" if kind in statements.NOT_COMPANIES else "no_statements"
        return _stamped({"ticker": symbol, "unavailable": reason}, UNAVAILABLE_TTL_SECONDS)
    _splits, prices, price_problem = _prices(symbol)
    payload = statements.build(frames, info, prices=prices)
    payload.update({"ticker": symbol, "source": "yahoo",
                    "entity": str(info.get("longName") or info.get("shortName") or symbol)})
    return _stamped(payload, _ttl(payload, price_problem))


_memo_lock = threading.Lock()
_memo: dict[str, tuple[float, dict[str, Any]]] = {}
_MEMO_MAX = 64


def peek(symbol: str) -> Optional[dict[str, Any]]:
    """The cached payload, stale or not, or ``None``. Never fetches.

    Re-read when the file's mtime moves, so a build finished by the other
    worker is seen on the next request rather than at this worker's recycle.
    """
    path = _path(symbol)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _memo_lock:
        hit = _memo.get(symbol)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.debug("fundamentals: unreadable cache for %s: %s", symbol, exc)
        return None
    if not isinstance(payload, dict) or payload.get("_ver") != CACHE_VER:
        return None
    with _memo_lock:
        if len(_memo) >= _MEMO_MAX:
            _memo.pop(next(iter(_memo)))
        _memo[symbol] = (mtime, payload)
    return payload


def is_stale(payload: dict[str, Any], *, now: Optional[float] = None) -> bool:
    stamp, ttl = payload.get("_ts"), payload.get("_ttl")
    if not isinstance(stamp, (int, float)) or not isinstance(ttl, (int, float)):
        return True
    return ((now if now is not None else time.time()) - stamp) > ttl


def recent_failure(symbol: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The last build failure, if it happened within :data:`FAIL_TTL_SECONDS`."""
    try:
        marker = json.loads(_fail_path(symbol).read_text())
    except (OSError, ValueError):
        return None
    stamp = marker.get("ts") if isinstance(marker, dict) else None
    if not isinstance(stamp, (int, float)):
        return None
    if ((now if now is not None else time.time()) - stamp) > FAIL_TTL_SECONDS:
        return None
    return marker


def refresh(symbol: str) -> dict[str, Any]:
    """Build, persist, clear any failure marker. Background use only."""
    try:
        payload = build(symbol)
    except BuildError as exc:
        _write_json(_fail_path(symbol), {"ts": time.time(), "reason": exc.reason})
        raise
    _write_json(_path(symbol), payload)
    try:
        _fail_path(symbol).unlink()
    except OSError:
        pass
    return payload


# ---------------------------------------------------------------------------
# Background builds
# ---------------------------------------------------------------------------

_budget_lock = threading.Lock()
_inflight = 0
_last_start = 0.0
_building: dict[str, threading.Thread] = {}


def _try_reserve() -> bool:
    global _inflight, _last_start
    now = time.monotonic()
    with _budget_lock:
        if _inflight >= MAX_INFLIGHT_BUILDS or now - _last_start < BUILD_MIN_GAP_SECONDS:
            return False
        _inflight += 1
        _last_start = now
        return True


def _penalise(seconds: float) -> None:
    """Push the earliest start of the next build back by *seconds*."""
    global _last_start
    with _budget_lock:
        _last_start = max(_last_start, time.monotonic()) + seconds


def _release() -> None:
    """Floors at zero: a doubled release would raise the ceiling for ever."""
    global _inflight
    with _budget_lock:
        _inflight = max(0, _inflight - 1)


def building(symbol: str) -> bool:
    with _budget_lock:
        return symbol in _building


def kick(symbol: str) -> bool:
    """Start a background build of *symbol* if the budget allows.

    Returns whether one started. Refusal is cheap: the caller answers 202
    either way and the page is already polling, so the build begins on a
    later poll.
    """
    with _budget_lock:
        if symbol in _building:
            return False
    if not _try_reserve():
        return False

    def _run() -> None:
        try:
            refresh(symbol)
        except BuildError as exc:
            log.info("fundamentals: build failed for %s: %s", symbol, exc)
        except Exception:  # noqa: BLE001 - a thread must not die silently
            log.exception("fundamentals: build crashed for %s", symbol)
            _write_json(_fail_path(symbol), {"ts": time.time(), "reason": "internal"})
        finally:
            _release()
            with _budget_lock:
                _building.pop(symbol, None)

    with _budget_lock:
        if symbol in _building:
            already = True
        else:
            already = False
            thread = threading.Thread(target=_run, name=f"fundamentals-{symbol}", daemon=True)
            _building[symbol] = thread
    if already:
        _release()
        return False
    thread.start()
    return True
