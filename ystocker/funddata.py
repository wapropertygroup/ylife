"""
ystocker.funddata
~~~~~~~~~~~~~~~~~
What any one symbol *is*, and — if it is a wrapper — what is inside it.

Why this is not part of ``etf_holdings.py``
-------------------------------------------
That module is deliberately two ETFs wide: its own docstring says it "exists to
explain that number, not to be a fund browser", and its ``CACHE_VER`` bump
invalidates a payload that ``/multiples`` and the AI brief both read. Look-through
needs the opposite shape — an unbounded set of symbols nobody chose in advance,
arriving from whatever a user imported, cached per symbol so one cold entry does
not invalidate the rest.

Why the request path never fetches
----------------------------------
A twenty-line portfolio of mostly funds needs roughly a hundred Yahoo calls to
resolve cold: two per fund, plus one per distinct child to learn whether that
child is itself a wrapper. At the half-second spacing ``data.fetch_group`` uses
that is about a minute — and ``CLAUDE.md`` records what happens next, because it
has happened here before: gunicorn's ``--timeout 120`` kills the worker and takes
every other request it was serving with it.

So :func:`peek` is the request-path entry point and it never touches the network.
Uncached symbols come back as ``kind="pending"``, which ``lookthrough`` reports
separately from "unresolvable" precisely so a cold cache cannot masquerade as a
claim about the security. A background pass calls :func:`warm` on those symbols
and the next poll sees higher coverage. The steady state is cheap: fund top-tens
are overwhelmingly the same few hundred megacaps, shared across every user.

Two TTLs, one record
--------------------
Composition and price age at completely different rates — holdings are published
monthly at best, a price is stale in a minute — so one TTL is wrong in one
direction or the other. A single record carries ``quote_at`` and ``comp_at`` and
each half refreshes independently.

Negative results are cached too, with a back-off. ``XTSLA`` (a BlackRock cash
sweep inside AOR) 404s at Yahoo and appears in a widely-held fund, so without a
negative entry every recomputation would re-ask for a symbol that has never
existed and never will.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ystocker import fetchguard
from ystocker.portfolio_csv import CASH_SYMBOL

log = logging.getLogger(__name__)

PROVIDER = "yahoo"

CACHE_PATH = Path(__file__).parent.parent / "cache" / "funddata.json"

#: Bump when a record's shape changes; an older cache is then discarded rather
#: than served into code expecting something else. v1 stored holding weights as
#: percentages, which would have inflated every look-through 100x.
CACHE_VER = "v2"

#: Composition: holdings and asset classes are restated monthly at best.
COMP_TTL_SECONDS = 24 * 3600
#: Quote: only used to value a position the user gave no market value for, so it
#: matches the ticker cache rather than chasing intraday prices.
QUOTE_TTL_SECONDS = 8 * 3600
#: A symbol Yahoo does not know is unlikely to appear later, but "unlikely" is not
#: "never" — a newly listed ETF resolves eventually.
NEGATIVE_TTL_SECONDS = 7 * 24 * 3600

#: Cache ceiling. Fund top-tens concentrate hard, so a few thousand symbols covers
#: every portfolio seen; the bound exists so an import of junk symbols cannot grow
#: the file without limit. Eviction is least-recently-read.
MAX_RECORDS = 6_000

#: Ceiling on symbols fetched in one warm pass. Bounded so the background thread
#: yields between passes instead of holding Yahoo open for an hour, and so a
#: single enormous portfolio cannot starve the other warmers behind
#: ``warmup.cold_build``.
WARM_BATCH = 40

#: Spacing between calls, matching data.fetch_group. Yahoo rate-limits a client
#: that opens everything at once, and one 429 costs far more than this sleep.
_FETCH_SPACING_SECONDS = 0.5

KIND_FUND = "fund"
KIND_EQUITY = "equity"
KIND_CASH = "cash"
KIND_OTHER = "other"
KIND_PENDING = "pending"

#: Yahoo quoteType -> our kind. Only KIND_FUND is looked through; everything else
#: is a leaf. INDEX and CURRENCY are leaves rather than errors because a portfolio
#: can legitimately hold a currency or a tracking product.
_QUOTE_TYPES: dict[str, str] = {
    "ETF": KIND_FUND,
    "MUTUALFUND": KIND_FUND,
    "EQUITY": KIND_EQUITY,
    "MONEYMARKET": KIND_CASH,
    "CURRENCY": KIND_CASH,
    "CRYPTOCURRENCY": KIND_OTHER,
    "INDEX": KIND_OTHER,
    "FUTURE": KIND_OTHER,
    "OPTION": KIND_OTHER,
}

#: Yahoo's asset_classes keys -> ours. lookthrough reads ``stock`` to tell a bond
#: sleeve (known not to contain companies) from equity it merely cannot see.
_ASSET_KEYS: dict[str, str] = {
    "stockPosition": "stock",
    "bondPosition": "bond",
    "cashPosition": "cash",
    "preferredPosition": "preferred",
    "convertiblePosition": "convertible",
    "otherPosition": "other",
}

#: Kept per fund. Yahoo returns ten; the cap is here so a vendor change to a full
#: constituent list cannot silently turn a 6 KB cache into a 60 MB one.
MAX_HOLDINGS = 25

#: Yahoo sometimes answers with an internal lookup artifact instead of a name --
#: ``005930.KQ`` (Samsung) comes back as ``"005930.KQ,0P0000B2XZ,1"``, a
#: comma-joined symbol plus Morningstar id. Displaying that verbatim looks like a
#: parsing bug in this app. Returning "" instead lets the *parent fund's* holding
#: name win ("Samsung Electronics Co Ltd"), which is both correct and prettier.
_JUNK_NAME_RE = re.compile(r"0P0000[0-9A-Z]{4}|^[^,]{1,15},[^,]*,\d+$")


def _clean_name(raw: Any, symbol: str) -> str:
    """A display name, or "" when Yahoo returned an artifact rather than a name."""
    name = str(raw or "").strip()
    if not name or name.upper() == symbol.upper():
        return ""
    if _JUNK_NAME_RE.search(name):
        log.debug("funddata: discarding artifact name for %s: %r", symbol, name)
        return ""
    return name[:120]

_lock = threading.Lock()
_mem: dict[str, dict[str, Any]] = {}
_loaded = False
_dirty = False

#: Per-symbol back-off shared with nothing else: a symbol that keeps failing is
#: skipped by every later warm pass rather than retried on each one.
SYMBOL_BACKOFF = fetchguard.FailureBackoff("funddata", base_seconds=300,
                                           max_seconds=6 * 3600)


# ---------------------------------------------------------------------------
# Synthetic records — resolvable without a network call
# ---------------------------------------------------------------------------

def _synthetic(symbol: str) -> Optional[dict[str, Any]]:
    """Records for symbols that are ours, not Yahoo's.

    ``$CASH`` is minted by the CSV importer for a broker line that carries a
    balance and no ticker. Asking Yahoo about it would 404 forever.
    """
    if symbol == CASH_SYMBOL:
        return {"symbol": CASH_SYMBOL, "name": "Cash", "kind": KIND_CASH,
                "holdings": [], "asset_classes": {"stock": 0.0, "cash": 1.0},
                "sectors": {}, "price": 1.0, "currency": "USD",
                "quote_at": None, "comp_at": None, "synthetic": True}
    return None


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def _load() -> None:
    """Populate ``_mem`` from disk once per process. Caller holds ``_lock``."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001 - a cold cache is not an error
        log.warning("funddata: unreadable cache (%s); starting empty", exc)
        return
    if not isinstance(raw, dict) or raw.get("ver") != CACHE_VER:
        log.info("funddata: cache is %s, want %s — starting empty",
                 (raw or {}).get("ver") if isinstance(raw, dict) else "?", CACHE_VER)
        return
    records = raw.get("records")
    if isinstance(records, dict):
        _mem.update({k: v for k, v in records.items() if isinstance(v, dict)})
        log.info("funddata: loaded %d cached symbols", len(_mem))


def _prune_locked() -> None:
    """Evict least-recently-read records down to MAX_RECORDS. Caller holds lock."""
    if len(_mem) <= MAX_RECORDS:
        return
    ordered = sorted(_mem.items(),
                     key=lambda kv: float(kv[1].get("read_at") or 0.0))
    for symbol, _rec in ordered[:len(_mem) - MAX_RECORDS]:
        _mem.pop(symbol, None)
    log.info("funddata: pruned cache to %d records", len(_mem))


def flush() -> None:
    """Write the cache to disk atomically. Cheap no-op when nothing changed."""
    global _dirty
    with _lock:
        if not _dirty:
            return
        _prune_locked()
        payload = {"ver": CACHE_VER, "saved_at": time.time(),
                   "records": dict(_mem)}
        _dirty = False
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:  # noqa: BLE001 - failing to persist is not fatal
        log.warning("funddata: could not write cache: %s", exc)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def _fresh(record: dict[str, Any], *, need_quote: bool = False) -> bool:
    """Whether *record* is usable without refetching.

    Composition is what look-through needs, so a fund is fresh when its
    composition is. A quote matters only for a position the user priced no other
    way, hence ``need_quote``.
    """
    now = time.time()
    if record.get("missing"):
        return now - float(record.get("checked_at") or 0.0) < NEGATIVE_TTL_SECONDS
    if record.get("synthetic"):
        return True
    if record.get("kind") == KIND_FUND:
        if now - float(record.get("comp_at") or 0.0) >= COMP_TTL_SECONDS:
            return False
    elif not record.get("kind"):
        return False
    if need_quote and now - float(record.get("quote_at") or 0.0) >= QUOTE_TTL_SECONDS:
        return False
    return True


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch(symbol: str) -> dict[str, Any]:
    """Ask Yahoo what *symbol* is, and what is in it. Raises on total failure."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    info = ticker.info or {}
    quote_type = str(info.get("quoteType") or "").upper()
    kind = _QUOTE_TYPES.get(quote_type, KIND_EQUITY if quote_type else "")

    if not kind:
        # No quoteType at all is Yahoo's shape for "no such symbol" behind a 200,
        # and is how XTSLA presents. A record with no kind would look pending
        # forever, so this is an explicit miss.
        raise ValueError(f"{symbol}: no quoteType in response")

    from ystocker.data import latest_price

    record: dict[str, Any] = {
        "symbol": symbol,
        "name": _clean_name(info.get("shortName") or info.get("longName"), symbol),
        "kind": kind,
        "quote_type": quote_type,
        "price": latest_price(info),
        "currency": str(info.get("currency") or ""),
        "sector": info.get("sector") or "",
        "holdings": [],
        "asset_classes": {},
        "sectors": {},
        "quote_at": time.time(),
        "comp_at": None,
    }

    if kind != KIND_FUND:
        return record

    # Only funds have composition, and asking for it on an equity raises
    # YFDataException rather than returning empty -- which is why this is gated on
    # quoteType rather than tried speculatively.
    try:
        fd = ticker.funds_data
    except Exception as exc:  # noqa: BLE001 - a fund with no fund data is a leaf
        log.info("funddata: %s has no fund data (%s); treating as a leaf",
                 symbol, type(exc).__name__)
        record["kind"] = KIND_OTHER
        record["comp_at"] = time.time()
        return record

    failures = 0
    try:
        top = fd.top_holdings
        if top is not None and not top.empty:
            for child, row in top.head(MAX_HOLDINGS).iterrows():
                weight = row.get("Holding Percent")
                if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                    continue
                # Kept as a 0..1 fraction, which is how Yahoo returns it and what
                # lookthrough expects. etf_holdings.py stores percent instead --
                # mixing the two scales is a silent 100x.
                record["holdings"].append({
                    "symbol": str(child).upper().strip(),
                    "name": _clean_name(row.get("Name"), str(child)),
                    "weight": round(float(weight), 6),
                })
    except Exception:  # noqa: BLE001
        failures += 1

    try:
        raw = fd.asset_classes or {}
        record["asset_classes"] = {
            ours: round(float(raw[theirs]), 6)
            for theirs, ours in _ASSET_KEYS.items()
            if isinstance(raw.get(theirs), (int, float))
            and not isinstance(raw.get(theirs), bool)
        }
    except Exception:  # noqa: BLE001
        failures += 1

    try:
        raw_sectors = fd.sector_weightings or {}
        record["sectors"] = {
            str(k): round(float(v), 6) for k, v in raw_sectors.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
    except Exception:  # noqa: BLE001
        failures += 1

    # A "fund" with neither holdings nor asset classes cannot be looked through,
    # and must be demoted to a leaf rather than left as a wrapper.
    #
    # This is not hypothetical: Yahoo reports quoteType MUTUALFUND for some
    # foreign equities -- 005930.KQ (Samsung) and 000660.KQ (SK hynix) both arrive
    # as MUTUALFUND with no composition of any kind. Left as funds, lookthrough
    # walks in, finds nothing, and buckets the whole position as `unclassified`,
    # so a correctly identified company is reported as portfolio this tool cannot
    # see. Demoting makes it a named leaf that counts toward coverage.
    #
    # Note the condition is holdings AND asset_classes, not sectors: BND legitimately
    # discloses no holdings and no sectors but does report asset classes, and it
    # must stay a fund so its bond sleeve lands in `non_equity`.
    if not record["holdings"] and not record["asset_classes"]:
        log.info("funddata: %s is typed %s but discloses no composition "
                 "(%d vendor error(s)) — treating as a leaf",
                 symbol, quote_type, failures)
        record["kind"] = KIND_OTHER
    elif failures:
        log.info("funddata: %s composition partially unavailable (%d of 3 blocks)",
                 symbol, failures)

    record["comp_at"] = time.time()
    return record


def get(symbol: str, *, need_quote: bool = False) -> Optional[dict[str, Any]]:
    """A record for *symbol*, fetching if the cache is cold or stale.

    Returns None when the symbol cannot be resolved. **Never call from a request
    handler** — see the module docstring. Use :func:`peek`.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None

    synthetic = _synthetic(symbol)
    if synthetic is not None:
        return synthetic

    with _lock:
        _load()
        cached = _mem.get(symbol)
        if cached is not None:
            cached["read_at"] = time.time()
            if _fresh(cached, need_quote=need_quote):
                return None if cached.get("missing") else cached

    if not SYMBOL_BACKOFF.ready(symbol):
        with _lock:
            cached = _mem.get(symbol)
        return None if (cached is None or cached.get("missing")) else cached

    try:
        fetchguard.guard(PROVIDER)
    except fetchguard.CooldownActive:
        # Serve whatever we have rather than recording a failure against a symbol
        # that was never actually asked about.
        with _lock:
            cached = _mem.get(symbol)
        return None if (cached is None or cached.get("missing")) else cached

    global _dirty
    try:
        record = _fetch(symbol)
    except Exception as exc:  # noqa: BLE001 - a bad symbol is data, not a crash
        text = f"{type(exc).__name__}: {exc}".lower()
        if any(n in text for n in ("429", "too many requests", "rate limit")):
            fetchguard.trip(PROVIDER, fetchguard.FETCH_RATE_LIMIT_COOLDOWN_SECONDS,
                            "funddata rate limit")
            # A rate limit says nothing about the symbol, so it must not be
            # recorded as missing -- that would poison the cache for a week.
            return None
        SYMBOL_BACKOFF.record_failure(symbol)
        log.info("funddata: %s unresolvable (%s)", symbol, exc)
        with _lock:
            _mem[symbol] = {"symbol": symbol, "missing": True,
                            "checked_at": time.time(), "read_at": time.time()}
            _dirty = True
        return None

    SYMBOL_BACKOFF.record_success(symbol)
    record["read_at"] = time.time()
    with _lock:
        _mem[symbol] = record
        _dirty = True
    return record


def peek(symbol: str, *, need_quote: bool = False) -> Optional[dict[str, Any]]:
    """A cached record for *symbol*, or None. Never touches the network.

    Mirrors ``breadth.peek()`` and ``etf_holdings.peek()``. A stale composition is
    returned rather than withheld: fund holdings restate monthly, so yesterday's
    are the right answer and an empty page is not.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    synthetic = _synthetic(symbol)
    if synthetic is not None:
        return synthetic
    with _lock:
        _load()
        record = _mem.get(symbol)
        if record is None:
            return None
        record["read_at"] = time.time()
        if record.get("missing"):
            return None
        return dict(record)


def is_known(symbol: str) -> bool:
    """Whether *symbol* has been looked up before, successfully or not.

    Distinguishes "not fetched" from "fetched and does not exist", which is the
    difference between ``pending`` and ``unresolved`` downstream.
    """
    symbol = (symbol or "").upper().strip()
    if _synthetic(symbol) is not None:
        return True
    with _lock:
        _load()
        return symbol in _mem


# ---------------------------------------------------------------------------
# The resolver handed to lookthrough
# ---------------------------------------------------------------------------

def peek_resolver() -> Callable[[str], Optional[dict[str, Any]]]:
    """A cache-only resolver for ``lookthrough.analyse``.

    Returns ``kind="pending"`` for a symbol nobody has fetched yet, and None for
    one Yahoo has already denied. Collapsing those two would let a cold cache
    render as a statement that the security does not exist — and since the
    residual buckets are what the page reports as its honesty caveat, the two
    have to stay apart.
    """
    def _resolve(symbol: str) -> Optional[dict[str, Any]]:
        record = peek(symbol)
        if record is not None:
            return record
        if is_known(symbol):
            return None          # fetched before and genuinely missing
        return {"symbol": symbol, "name": "", "kind": KIND_PENDING,
                "holdings": [], "asset_classes": {}}
    return _resolve


# ---------------------------------------------------------------------------
# Warming
# ---------------------------------------------------------------------------

def warm(symbols: Iterable[str], *, budget: int = WARM_BATCH,
         need_quote: bool = False) -> tuple[int, int]:
    """Fetch up to *budget* uncached symbols. Returns (fetched, remaining).

    Background use only. Skips anything already fresh, so it is safe to call with
    the same list repeatedly — which is what the poll loop does.
    """
    wanted = []
    for raw in symbols:
        symbol = (raw or "").upper().strip()
        if not symbol or symbol in wanted:
            continue
        if _synthetic(symbol) is not None:
            continue
        with _lock:
            _load()
            cached = _mem.get(symbol)
        if cached is not None and _fresh(cached, need_quote=need_quote):
            continue
        wanted.append(symbol)

    fetched = 0
    for symbol in wanted[:budget]:
        if fetched:
            time.sleep(_FETCH_SPACING_SECONDS)
        if fetchguard.cooldown_remaining(PROVIDER) > 0:
            log.info("funddata: Yahoo cool-down — stopping warm after %d", fetched)
            break
        get(symbol, need_quote=need_quote)
        fetched += 1

    if fetched:
        flush()
    remaining = max(0, len(wanted) - fetched)
    if fetched or remaining:
        log.info("funddata: warmed %d symbols, %d still cold", fetched, remaining)
    return fetched, remaining


def price_of(symbol: str) -> Optional[float]:
    """Cached price for *symbol*, or None. Never fetches.

    Only used to value a position whose CSV gave no market value; a portfolio
    imported with values needs no price at all.
    """
    record = peek(symbol, need_quote=True)
    if not record:
        return None
    price = record.get("price")
    if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
        return float(price)
    return None


def stats() -> dict[str, Any]:
    """Cache counts, for the diagnostics line on /assets."""
    with _lock:
        _load()
        total = len(_mem)
        missing = sum(1 for r in _mem.values() if r.get("missing"))
        funds = sum(1 for r in _mem.values() if r.get("kind") == KIND_FUND)
    return {"cached": total, "funds": funds, "missing": missing,
            "path": str(CACHE_PATH)}
