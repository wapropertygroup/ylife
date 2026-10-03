"""
ystocker.predictions
~~~~~~~~~~~~~~~~~~~~
Live Polymarket and Kalshi odds on the events that move markets: the feed behind
/predictions, and the prediction-market side of the futures cross-check on
/fedwatch.

Both venues publish market data on public, keyless APIs -- Polymarket's Gamma
API and Kalshi's trade API v2 -- so this is a fetcher and a cache like
``sectors.py``, with one difference in cadence. Odds reprice all day, so the
TTL is ten minutes rather than a day, and each refresh is about twenty small
requests: nine Gamma tag listings and one Kalshi request per tracked series.

The selection is deliberately narrow. Gamma's tags already sort its catalogue
into economy, finance, stocks, earnings, IPOs, commodities, crypto and tariffs,
so those listings are read busiest-first. Kalshi offers no category filter at
all on its events endpoint -- its Economics category alone has ~900 series,
most of them dead or a few hundred dollars deep -- so it is read from a curated
list of series instead (:data:`KS_SERIES`), the ones with real open interest on
questions a markets reader asks: the Fed, CPI, payrolls, GDP, recession and the
year-end index ranges.

Three rules, all inherited from the rest of this package:

* **The request path never fetches.** :func:`peek` returns whatever is cached;
  a cold cache is kicked into a background refresh and the API answers 202.
* **A worker re-reads the disk when the file changes.** Under ``--preload`` the
  refresh thread lives only in gunicorn's master, so a forked worker would
  otherwise serve the odds it inherited for its whole life. ``peek()`` costs
  one ``stat()`` when nothing changed -- ``fedwatch.get_fedwatch_data`` is the
  precedent.
* **Stale beats absent, but is labelled.** If one venue fails outright, its
  events from the previous payload are carried forward and the payload says
  since when (``sources[venue]["stale_since"]``), rather than the board quietly
  losing half its rows on the day Kalshi has an outage.

Each venue has its own ``fetchguard`` breaker, so a Gamma 429 cannot stall the
Kalshi half and neither can stall Yahoo.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ystocker import odds

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

PM_PROVIDER = "polymarket"
KS_PROVIDER = "kalshi"

CACHE_PATH = Path(__file__).parent.parent / "cache" / "predictions_cache.json"
_LOCK_PATH = CACHE_PATH.with_suffix(".refresh")
TTL_SECONDS = 10 * 60

#: Bump when the payload's shape changes. Cheap here -- a refresh is twenty
#: small requests, not a Yahoo sweep -- so there is no reason to hesitate.
CACHE_VER = "v1"

#: A refresh claimed by another process longer ago than this is presumed dead.
_REFRESH_STALE_SECONDS = 180

#: Gamma tag listings: (tag, fallback topic, how many events). The fallback
#: applies only when none of an event's own tags names a topic; an event listed
#: under several tags keeps the first listing's, so the more specific queries
#: come first. The counts are a size budget as much as a coverage one: Gamma
#: repeats every market's full description inside every listing, so these nine
#: requests measured 10.4 MB of JSON at 60/30/30 (about a tenth of that on the
#: wire, gzipped). Each listing is parsed and dropped before the next is asked
#: for, so the peak is one listing, never the lot.
PM_QUERIES: tuple[tuple[str, str, int], ...] = (
    ("fed-rates", "rates", 20),
    ("economy", "growth", 50),
    ("finance", "companies", 25),
    ("stocks", "equities", 25),
    ("earnings", "equities", 20),
    ("ipos", "companies", 15),
    ("commodities", "commodities", 15),
    ("crypto", "crypto", 20),
    ("tariffs", "policy", 12),
)

#: Kalshi series: (series ticker, topic, nearest N open events). KXFEDDECISION
#: takes six because the cross-check reaches four meetings ahead and a meeting
#: can sit between two listings while one is rolling off.
KS_SERIES: tuple[tuple[str, str, int], ...] = (
    ("KXFEDDECISION", "rates", 6),
    ("KXRATECUTCOUNT", "rates", 1),
    ("KXRATEHIKE", "rates", 1),
    ("KXCPI", "inflation", 2),
    ("KXCPIYOY", "inflation", 2),
    ("KXCPICORE", "inflation", 2),
    ("KXPAYROLLS", "jobs", 2),
    ("KXU3", "jobs", 2),
    ("KXGDP", "growth", 2),
    ("KXRECSSNBER", "growth", 2),
    ("KXINXY", "equities", 1),
    ("KXNASDAQ100Y", "equities", 1),
    ("KXWTI", "commodities", 4),
)

#: Between two requests to the same venue. Polite rather than necessary: both
#: venues allow far more, and a refresh is twenty calls every ten minutes.
_SPACING_SECONDS = 0.15

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "yStocker/1.0 (+https://stock.li-family.us)",
}

_lock = threading.Lock()
_mem: Optional[dict[str, Any]] = None
_mem_mtime: float = 0.0
_refreshing = threading.Event()


# ── Fetching ────────────────────────────────────────────────────────────────

def _get_json(provider: str, url: str, params: dict[str, Any]) -> Any:
    from ystocker import fetchguard

    resp = fetchguard.request(provider, url, params=params, headers=_HEADERS, timeout=20)
    return resp.json()


def _pm_events(tag: str, limit: int) -> list[dict[str, Any]]:
    data = _get_json(PM_PROVIDER, f"{GAMMA_BASE}/events", {
        "tag_slug": tag, "closed": "false", "order": "volume24hr",
        "ascending": "false", "limit": limit,
    })
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _ks_events(series: str, limit: int) -> list[dict[str, Any]]:
    data = _get_json(KS_PROVIDER, f"{KALSHI_BASE}/events", {
        "series_ticker": series, "status": "open",
        "with_nested_markets": "true", "limit": limit,
    })
    events = data.get("events") if isinstance(data, dict) else None
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def fetch_polymarket_market(market_id: str) -> Optional[dict[str, Any]]:
    """One Gamma market by id, for settling a ledger row. None on any failure."""
    try:
        data = _get_json(PM_PROVIDER, f"{GAMMA_BASE}/markets/{market_id}", {})
    except Exception as exc:  # noqa: BLE001 - settlement retries on its next pass
        log.info("predictions: Polymarket market %s unavailable: %s", market_id, exc)
        return None
    return data if isinstance(data, dict) else None


def fetch_kalshi_market(ticker: str) -> Optional[dict[str, Any]]:
    """One Kalshi market by ticker, for settling a ledger row."""
    try:
        data = _get_json(KS_PROVIDER, f"{KALSHI_BASE}/markets/{ticker}", {})
    except Exception as exc:  # noqa: BLE001
        log.info("predictions: Kalshi market %s unavailable: %s", ticker, exc)
        return None
    market = data.get("market") if isinstance(data, dict) else None
    return market if isinstance(market, dict) else None


def _collect(venue: str, jobs, fetch, normalise) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one venue's requests, normalising as it goes.

    Stops at the first breaker refusal: once a venue has said "slow down",
    asking it eleven more times this cycle is how a soft limit becomes a ban.
    """
    from ystocker import fetchguard

    events: list[dict[str, Any]] = []
    ok = failed = 0
    error: Optional[str] = None
    for i, job in enumerate(jobs):
        if i:
            time.sleep(_SPACING_SECONDS)
        try:
            raw = fetch(job)
        except fetchguard.CooldownActive as exc:
            error = str(exc)
            log.warning("predictions: %s paused — %s", venue, exc)
            break
        except Exception as exc:  # noqa: BLE001 - one listing failing is not the venue failing
            failed += 1
            error = f"{type(exc).__name__}: {exc}"
            log.warning("predictions: %s request %s failed: %s", venue, job[0], exc)
            continue
        ok += 1
        for item in raw:
            ev = normalise(item, job)
            if ev is not None:
                events.append(ev)
    return events, {"ok": ok > 0, "requests": ok, "failed": failed, "error": error}


def _build(previous: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Fetch both venues and assemble a payload. Raises if both come back empty."""
    now = datetime.now(timezone.utc)
    pm, pm_status = _collect(
        "Polymarket", PM_QUERIES,
        lambda q: _pm_events(q[0], q[2]),
        lambda item, q: odds.normalize_polymarket(item, fallback_topic=q[1]))
    ks, ks_status = _collect(
        "Kalshi", KS_SERIES,
        lambda s: _ks_events(s[0], s[2]),
        lambda item, s: odds.normalize_kalshi(item, topic=s[1]))

    status = {"polymarket": pm_status, "kalshi": ks_status}
    for venue, rows in (("polymarket", pm), ("kalshi", ks)):
        if rows or not previous:
            continue
        kept_board = [e for e in previous.get("events") or [] if e.get("platform") == venue]
        kept_fed = [e for e in previous.get("fed_events") or [] if e.get("platform") == venue]
        if kept_board or kept_fed:
            since = (previous.get("sources") or {}).get(venue, {}).get("stale_since") \
                or previous.get("fetched_at")
            status[venue]["stale_since"] = since
            rows.extend(kept_board + [e for e in kept_fed if e not in kept_board])
            log.warning("predictions: %s returned nothing; carrying %d events forward",
                        venue, len(kept_board) + len(kept_fed))

    unique: dict[str, dict[str, Any]] = {}
    for ev in pm + ks:
        unique.setdefault(ev["key"], ev)
    if not unique:
        raise RuntimeError("no prediction-market data from either venue")

    board = odds.rank_board(odds.trim(ev) for ev in unique.values() if odds.on_board(ev, now))
    fed_events = [odds.trim(ev) for ev in unique.values() if odds.is_fed_decision(ev)]
    return {
        "ver": CACHE_VER,
        "fetched_at": time.time(),
        "events": board,
        "fed_events": fed_events,
        "sources": status,
        "counts": {
            "polymarket": sum(1 for e in board if e.get("platform") == "polymarket"),
            "kalshi": sum(1 for e in board if e.get("platform") == "kalshi"),
        },
    }


# ── Cache ───────────────────────────────────────────────────────────────────

def _disk_mtime() -> float:
    try:
        return CACHE_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _read_disk() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("predictions: unreadable cache (%s)", exc)
        return None
    if not isinstance(data, dict) or data.get("ver") != CACHE_VER:
        return None
    return data


def _write_disk(payload: dict[str, Any]) -> float:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, allow_nan=False, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning("predictions: could not write cache: %s", exc)
        return 0.0
    return _disk_mtime()


def peek() -> Optional[dict[str, Any]]:
    """The cached payload, ignoring TTL but honouring :data:`CACHE_VER`.

    Never fetches. Re-reads the disk copy only when its mtime has moved past
    the one this process last loaded, and never lets an older payload replace a
    newer one (ordered by the payload's own ``fetched_at``).
    """
    global _mem, _mem_mtime
    mtime = _disk_mtime()
    with _lock:
        if _mem is not None and (not mtime or mtime <= _mem_mtime):
            return _mem
    disk = _read_disk()
    with _lock:
        if disk is not None and (
                _mem is None or float(disk.get("fetched_at") or 0) >= float(_mem.get("fetched_at") or 0)):
            _mem, _mem_mtime = disk, mtime
        elif _mem is not None:
            _mem_mtime = max(_mem_mtime, mtime)
        return _mem


def age_seconds(payload: Optional[dict[str, Any]] = None) -> Optional[float]:
    payload = payload if payload is not None else peek()
    if not payload:
        return None
    return max(0.0, time.time() - float(payload.get("fetched_at") or 0))


def is_fresh() -> bool:
    age = age_seconds()
    return age is not None and age < TTL_SECONDS


def is_refreshing() -> bool:
    """Whether a refresh is running in this process or, recently, in another."""
    if _refreshing.is_set():
        return True
    try:
        return time.time() - _LOCK_PATH.stat().st_mtime < _REFRESH_STALE_SECONDS
    except OSError:
        return False


def _claim() -> bool:
    """Cross-process claim on a refresh: O_EXCL, so two workers kicking a cold
    cache in the same second cost one round of requests, not two."""
    try:
        if time.time() - _LOCK_PATH.stat().st_mtime >= _REFRESH_STALE_SECONDS:
            _LOCK_PATH.unlink()
    except OSError:
        pass
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as exc:
        log.warning("predictions: refresh claim failed (%s); refreshing unclaimed", exc)
        return True
    os.close(fd)
    return True


def _release() -> None:
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


def refresh() -> Optional[dict[str, Any]]:
    """Fetch both venues now and store the result. Never raises; returns the
    new payload, or None if this process did not (or could not) refresh."""
    global _mem, _mem_mtime
    if _refreshing.is_set() or not _claim():
        return None
    _refreshing.set()
    try:
        previous = peek()
        try:
            payload = _build(previous)
        except Exception as exc:  # noqa: BLE001
            log.warning("predictions: refresh failed (%s); keeping the previous payload", exc)
            return None
        mtime = _write_disk(payload)
        with _lock:
            _mem, _mem_mtime = payload, mtime or time.time()
        log.info("predictions: refreshed — %d on the board (%d Polymarket, %d Kalshi), "
                 "%d Fed decisions", len(payload["events"]), payload["counts"]["polymarket"],
                 payload["counts"]["kalshi"], len(payload["fed_events"]))
        return payload
    finally:
        _refreshing.clear()
        _release()


def kick() -> bool:
    """Start a background refresh unless one is already running. True if one
    was started by this call."""
    if is_refreshing():
        return False
    threading.Thread(target=refresh, daemon=True, name="predictions-refresh").start()
    return True


def find(key: str) -> Optional[dict[str, Any]]:
    """One event from the cached payload, by key, from the board or the Fed
    listings. Never fetches."""
    payload = peek()
    if not payload or not key:
        return None
    for ev in (payload.get("events") or []) + (payload.get("fed_events") or []):
        if ev.get("key") == key:
            return ev
    return None


def start_background_thread() -> None:
    def _loop() -> None:
        time.sleep(30)          # after the boot-time warmers have had their turn
        while True:
            try:
                refresh()
            except Exception:   # pragma: no cover - refresh() already never raises
                log.exception("predictions: refresh loop failed")
            time.sleep(TTL_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="predictions").start()
    log.info("predictions: background thread started (every %d min)", TTL_SECONDS // 60)
