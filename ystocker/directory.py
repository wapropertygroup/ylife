"""
ystocker.directory
~~~~~~~~~~~~~~~~~~
Every company listed with the SEC: the list behind /companies' "all companies".

/companies began as the ~300 companies the ticker cache follows, because those
are the ones with quotes. But the Fundamentals tab it leads to already works for
any SEC filer (``fundamentals.cik_for`` maps the ticker), so the directory was
the narrow part, not the data. SEC publishes the whole list as one keyless file,
``company_tickers_exchange.json``: measured 2026-10-03 at 10,434 tickers for
8,008 filers, 565 KB, listed roughly largest first, each with its exchange
(Nasdaq 4,376, NYSE 3,299, OTC 2,545, CBOE 44, none 170).

One card per company, not per ticker. Rows are grouped by CIK in the order SEC
lists them, so a company's face is its first-listed ticker -- GOOGL (row 3)
rather than GOOG (row 7,471), BRK-B rather than BRK-A -- and its other tickers
ride along as ``also``, where search still finds them.

No quotes, deliberately. Quoting 8,000 companies would be a bulk Yahoo sweep,
the kind ``valuation.py`` records getting this box blocked; the followed
companies keep theirs, and every other card opens a Fundamentals tab built from
that company's own filings.

Fetched through ``sec13f.edgar_get``, so SEC's per-client rate limit is shared
with the 13F refresher and the Fundamentals builds. Daily, from a background
thread; the request path only ever reads what is cached, and a cold cache
answers 202 while one fetch is kicked. A refresh that fails keeps the last copy,
however old: a day-old list missing one IPO beats an empty page.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CACHE_FILE = Path(__file__).parent.parent / "cache" / "sec_company_tickers_exchange.json"
TTL_SECONDS = 24 * 3600

#: An answer this short is a truncated or error document, not SEC's list.
MIN_ROWS = 1000

#: The exchanges SEC names. Anything else (``None``, a new label) is shown as
#: no exchange rather than invented into one of these.
EXCHANGES: tuple[str, ...] = ("NYSE", "Nasdaq", "CBOE", "OTC")

_lock = threading.Lock()
_mem: Optional[dict[str, Any]] = None      # {"as_of", "rows", "json"}
_mem_mtime = 0.0
_refreshing = threading.Event()


# ── Pure ────────────────────────────────────────────────────────────────────

def parse(raw: Any) -> list[dict[str, Any]]:
    """SEC's ``{"fields": [...], "data": [[...], ...]}`` as one row per company.

    Each row is ``{"t": face ticker, "n": name, "x": exchange or "", "also":
    [other tickers], "cik": int}``, in SEC's order. Columns are found by name,
    so a reordered file still parses; a row with no CIK or no ticker is skipped.
    """
    if not isinstance(raw, dict):
        return []
    fields = raw.get("fields")
    data = raw.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        return []
    col = {str(name): i for i, name in enumerate(fields)}
    if not {"cik", "ticker"} <= set(col):
        return []

    def cell(row: list, name: str) -> Any:
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    by_cik: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for row in data:
        if not isinstance(row, list):
            continue
        try:
            cik = int(cell(row, "cik"))
        except (TypeError, ValueError):
            continue
        ticker = str(cell(row, "ticker") or "").strip().upper()
        if not ticker:
            continue
        have = by_cik.get(cik)
        if have is not None:
            if ticker != have["t"] and ticker not in have["also"]:
                have["also"].append(ticker)
            continue
        exchange = str(cell(row, "exchange") or "").strip()
        by_cik[cik] = {
            "t": ticker,
            "n": str(cell(row, "name") or "").strip() or ticker,
            "x": exchange if exchange in EXCHANGES else "",
            "also": [],
            "cik": cik,
        }
        order.append(cik)
    return [by_cik[c] for c in order]


def wire(rows: Iterable[dict[str, Any]]) -> list[list[Any]]:
    """The compact form the page downloads: ``[ticker, name, exchange, also]``,
    ``also`` a space-joined string. ~380 KB for 8,000 companies, about a third
    of that gzipped -- once a day per reader at most, cached by the browser."""
    return [[r["t"], r["n"], r["x"], " ".join(r["also"])] for r in rows]


# ── Cache ───────────────────────────────────────────────────────────────────

def _disk_mtime() -> float:
    try:
        return CACHE_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _load(raw: Any, as_of: float) -> Optional[dict[str, Any]]:
    rows = parse(raw)
    if len(rows) < MIN_ROWS // 2:
        return None
    body = json.dumps({"status": "ok", "as_of": as_of, "count": len(rows),
                       "companies": wire(rows)},
                      ensure_ascii=False, separators=(",", ":"))
    return {"as_of": as_of, "rows": rows, "json": body}


def _read_disk() -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("directory: unreadable cache (%s)", exc)
        return None
    return _load(raw, _disk_mtime())


def peek() -> Optional[dict[str, Any]]:
    """The cached directory, any age, or None. Never fetches. A worker re-reads
    the file when its mtime moves past the copy it holds (the daily refresh
    runs in gunicorn's master)."""
    global _mem, _mem_mtime
    mtime = _disk_mtime()
    with _lock:
        if _mem is not None and (not mtime or mtime <= _mem_mtime):
            return _mem
    loaded = _read_disk()
    with _lock:
        if loaded is not None:
            _mem, _mem_mtime = loaded, mtime
        return _mem


def is_fresh() -> bool:
    got = peek()
    return bool(got) and time.time() - float(got["as_of"]) < TTL_SECONDS


def refresh() -> bool:
    """Fetch SEC's list now and store it. Never raises; True on success."""
    global _mem, _mem_mtime
    if _refreshing.is_set():
        return False
    _refreshing.set()
    try:
        from ystocker import sec13f

        try:
            resp = sec13f.edgar_get(URL)
            raw = resp.json()
        except Exception as exc:  # noqa: BLE001 - keep whatever copy exists
            log.warning("directory: SEC list unavailable (%s); keeping the last copy", exc)
            return False
        rows = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(rows, list) or len(rows) < MIN_ROWS:
            log.warning("directory: SEC list came back short (%s rows); keeping the last copy",
                        len(rows) if isinstance(rows, list) else "no")
            return False
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(CACHE_FILE.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, ensure_ascii=False)
            os.replace(tmp, CACHE_FILE)
        except OSError as exc:
            log.warning("directory: could not write the cache: %s", exc)
        loaded = _load(raw, time.time())
        if loaded is None:
            return False
        with _lock:
            _mem, _mem_mtime = loaded, _disk_mtime() or time.time()
        log.info("directory: %d companies (%d tickers) from SEC", len(loaded["rows"]), len(rows))
        return True
    finally:
        _refreshing.clear()


def kick() -> bool:
    """Start a background refresh unless one is running. True if started."""
    if _refreshing.is_set():
        return False
    threading.Thread(target=refresh, daemon=True, name="directory-refresh").start()
    return True


def start_background_thread() -> None:
    def _loop() -> None:
        time.sleep(90)          # after the boot-time warmers
        while True:
            if not is_fresh():
                refresh()
            time.sleep(3600)    # check hourly; fetch when a day old

    threading.Thread(target=_loop, daemon=True, name="directory").start()
    log.info("directory: background thread started (daily)")
