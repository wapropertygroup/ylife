"""
ystocker.dca_universe
~~~~~~~~~~~~~~~~~~~~~
Which tickers the ``/dca`` overview ranks — a registry that grows as people use
it, rather than a list in the source.

Opening ``/dca/<TICKER>`` for a name that scores adds it here, and from then on
it appears in the ranked table and is refreshed by the daily sweep like
everything else. That is the whole feature: the framework named fifteen
companies, and the list a reader actually cares about is the one they built by
looking things up.

Why this is a store and not a constant
--------------------------------------
It is **accumulated state**, on the same terms as the observed series in
``dca_history`` — but weaker, and the difference matters for how it degrades.
A lost snapshot row is gone for ever because nothing upstream sells back
yesterday's forward multiple. A lost registry row costs one lookup: open the
ticker again and it comes back. So this does *not* fail closed the way
``portfolio`` does; with DynamoDB unreachable it falls back to the on-disk
mirror and then to the seed, and the page still works.

Both are read and unioned rather than one being preferred, matching
``valuation._previous_snapshots``: the disk copy can hold a row written while
DynamoDB was briefly unreachable, and DynamoDB holds everything that predates
the current instance.

The cap is the load-bearing part
--------------------------------
Every tracked ticker is **six Yahoo reads a day, for ever**. A registry that
grows without limit is therefore a slow-motion version of the bulk sweep
``valuation.py`` records having got this box hard-blocked — it would not fail on
the day it broke anything, it would just get heavier every week until it did.

So :data:`MAX_TRACKED` bounds the whole thing and the least-recently-opened
entry is evicted to make room. At the cap the daily pass is 60 x 6 = 360 reads
spread over 60 x ``WARM_SPACING_SECONDS`` = half an hour, which is a fifth of
what ``analyst.py`` already sweeps and paced an order of magnitude gentler.

The seed can never be evicted. Those are the companies the source framework
assigns a model to by name, so they are the population the ranked table is
*for*; letting a burst of lookups push MSFT out would quietly turn the overview
into a list of whatever somebody typed last week.

Only what actually scored gets in
---------------------------------
Registration happens after a **successful** rebuild that produced a usable
series, never on the search itself. Yahoo publishes no statements for an ETF, so
``GDX`` and ``IGV`` build to an ``unavailable`` payload — and adding those would
put rows in a table headed "all scored names" that can never carry a score,
while still costing six reads a day each to re-confirm it. Observed on the first
afternoon: ten of the twenty names opened were exactly this shape.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

__all__ = [
    "TABLE_NAME", "MAX_TRACKED", "PINNED", "seed", "tracked", "all_tickers",
    "remember", "forget", "touch", "stats", "pin", "unpin", "pinned",
    "HELD", "held", "sync_held",
]

TABLE_NAME = os.environ.get("DCA_UNIVERSE_TABLE", "ystocker-dca-universe").strip()

#: Ceiling on the whole ranked universe, seed included. See the module docstring
#: for the arithmetic; the short version is that this is a daily Yahoo bill, not
#: a storage question, so it is set by what the sweep can afford rather than by
#: what DynamoDB can hold.
MAX_TRACKED = int(os.environ.get("DCA_MAX_TRACKED", "60"))

#: ``source`` marking a row the reader asked to keep.
#:
#: Registration is automatic — opening a ticker that scores puts it in the table
#: — and eviction was therefore pure recency over everything the seed did not
#: protect. That is right for a name somebody glanced at once and wrong for one
#: they went and found: with the registry saturated (it reached 60/60, of which
#: 23 are seed) every new lookup silently deleted the least-recently-viewed of
#: the reader's own 37, which reads exactly as the page losing their list.
#:
#: Pinning does not raise the ceiling, because the ceiling is the daily Yahoo
#: bill and a pin costs the same six reads a day as any other row. It only
#: changes the *order*: unpinned names go first, and when only pinned ones are
#: left :func:`pin` refuses rather than quietly dropping one to admit another.
PINNED = "pinned"

#: ``source`` marking a row that is one of the signed-in reader's own holdings.
#:
#: Ranked above :data:`PINNED` in :func:`_evict` and below the framework seed,
#: which is the order the names deserve: a company somebody actually owns is the
#: one they most need scored, ahead of one they merely asked to keep, ahead of
#: one they once opened. Kept as a *source* on the row rather than resolved at
#: eviction time because `_evict` runs on the background sweep where there is no
#: request and therefore no session to ask who is signed in.
#:
#: Synced rather than set once. A position that is sold stops being a holding,
#: so :func:`sync_held` demotes it back to ``opened`` — otherwise the protected
#: set would only ever grow and would end up pinning a portfolio from months ago
#: against the cap.
HELD = "held"

#: Disk mirror. Small, rewritten whole, atomic — it is a set of short strings.
LOCAL_PATH = Path(__file__).parent.parent / "cache" / "dca_universe.json"

_lock = threading.Lock()
_table = None
_table_unavail_until = 0.0


def seed() -> set[str]:
    """The names the framework assigns a model to. Never evicted.

    ``GOOG`` is dropped in favour of ``GOOGL``: one company, two share classes,
    and only ``GOOGL`` is in ``PEER_GROUPS`` so ``GOOG`` could never get a peer
    percentile anyway.
    """
    from ystocker.dca import TICKER_MODELS

    return set(TICKER_MODELS) - {"GOOG"}


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def _get_table():
    """The table, or ``None``. Degrades rather than raising.

    Local dev has no credentials and the seed still works, so absence is not an
    error. The five-minute back-off stops every page load paying a connection
    timeout when the table genuinely is not there.
    """
    global _table, _table_unavail_until
    if _table is not None:
        return _table
    if time.time() < _table_unavail_until:
        return None
    with _lock:
        if _table is not None:
            return _table
        if time.time() < _table_unavail_until:
            return None
        try:
            import boto3

            ddb = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(TABLE_NAME)
            tbl.load()
            _table = tbl
            log.info("dca_universe: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("dca_universe: DynamoDB unavailable: %s", exc)
            _table = None
            _table_unavail_until = time.time() + 300
        return _table


def _ddb_rows() -> dict[str, dict[str, Any]]:
    """Every tracked row from DynamoDB, keyed by ticker.

    A Scan, and that is fine *because of the cap*: this table holds one short row
    per tracked ticker and is bounded at :data:`MAX_TRACKED`, unlike
    ``ystocker-dca-history`` which gains a row per ticker per day and is
    therefore queried, never scanned.
    """
    table = _get_table()
    if table is None:
        return {}
    try:
        out: dict[str, dict[str, Any]] = {}
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                symbol = str(item.get("ticker") or "").upper()
                if not symbol:
                    continue
                out[symbol] = {
                    "ticker": symbol,
                    "added_at": _num(item.get("added_at")),
                    "last_seen_at": _num(item.get("last_seen_at")),
                    "source": str(item.get("source") or "opened"),
                }
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("dca_universe: scan failed: %s", exc)
        return {}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Disk mirror
# ---------------------------------------------------------------------------

def _disk_rows() -> dict[str, dict[str, Any]]:
    try:
        if not LOCAL_PATH.exists():
            return {}
        blob = json.loads(LOCAL_PATH.read_text())
        rows = blob.get("tickers") if isinstance(blob, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {str(k).upper(): v for k, v in rows.items() if isinstance(v, dict)}
    except Exception as exc:  # noqa: BLE001
        log.debug("dca_universe: unreadable mirror: %s", exc)
        return {}


def _write_disk(rows: Mapping[str, Mapping[str, Any]]) -> None:
    try:
        LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(LOCAL_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"tickers": dict(rows), "saved_at": time.time()}, handle)
            os.replace(tmp, LOCAL_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 - the mirror is a convenience
        log.warning("dca_universe: could not persist mirror: %s", exc)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def tracked() -> dict[str, dict[str, Any]]:
    """Every registered ticker, DynamoDB and disk unioned, newest write winning."""
    merged = dict(_disk_rows())
    for symbol, row in _ddb_rows().items():
        current = merged.get(symbol) or {}
        if _num(row.get("last_seen_at")) >= _num(current.get("last_seen_at")):
            merged[symbol] = row
    return merged


def all_tickers() -> list[str]:
    """The effective universe: seed plus registry, capped, sorted.

    The cap drops the least-recently-opened *non-seed* entries first. Sorting the
    result alphabetically rather than by recency is deliberate — the page sorts
    by V anyway, and a universe whose membership order changed on every view
    would make the ``pending`` list churn for no reason.
    """
    keep = seed()
    extra = [(row.get("last_seen_at") or 0.0, symbol)
             for symbol, row in tracked().items() if symbol not in keep]
    extra.sort(reverse=True)                       # most recently opened first
    room = max(0, MAX_TRACKED - len(keep))
    keep.update(symbol for _seen, symbol in extra[:room])
    return sorted(keep)


def remember(ticker: str, *, source: str = "opened") -> bool:
    """Register *ticker*, or bump its recency if already there. Returns changed.

    Called only after a rebuild that actually produced a score -- see the module
    docstring on why an ETF that can never score must not enter a table headed
    "all scored names".

    Writing to DynamoDB and the mirror both, so a registry entry survives either
    one being unavailable. Never raises: failing to remember a ticker costs one
    lookup next time, and taking the page down over it would be a far worse
    trade.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return False

    now = time.time()
    rows = tracked()
    existing = rows.get(symbol)
    row = {
        "ticker": symbol,
        "added_at": (existing or {}).get("added_at") or now,
        "last_seen_at": now,
        "source": (existing or {}).get("source") or source,
    }
    rows[symbol] = row

    # Evict here as well as in all_tickers(): that one only hides the overflow
    # from the page, this one stops the table itself growing without bound.
    _evict(rows)

    table = _get_table()
    if table is not None:
        try:
            table.put_item(Item={
                "ticker": symbol,
                "added_at": str(round(row["added_at"], 3)),
                "last_seen_at": str(round(row["last_seen_at"], 3)),
                "source": row["source"],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("dca_universe: could not register %s: %s", symbol, exc)
    _write_disk(rows)
    if existing is None:
        log.info("dca_universe: now tracking %s (%d total)", symbol, len(rows))
    # Whether it is actually *in* the table, not merely whether it was new.
    # With every non-seed slot pinned, `_evict` drops this row again on the way
    # through — which is the pinned list winning, as intended, but a `True` here
    # would tell the caller a name was registered when it was not.
    return existing is None and symbol in rows


def _evict(rows: dict[str, dict[str, Any]]) -> None:
    """Drop the least-recently-opened rows until inside the cap, in tiers.

    The sort key is ``(held, pinned, last_seen_at)`` descending, so a holding is
    considered only after every pin is gone, and a pin only after every plain
    browse. The cap itself is unchanged by any of it: pinning and holding
    reorder the queue, they do not lengthen it, because every row in the table
    is six Yahoo reads a day whatever it is marked.
    """
    protected = seed()
    room = max(0, MAX_TRACKED - len(protected))
    extra = [(1 if row.get("source") == HELD else 0,
              1 if row.get("source") == PINNED else 0,
              row.get("last_seen_at") or 0.0, symbol)
             for symbol, row in rows.items() if symbol not in protected]
    if len(extra) <= room:
        return
    extra.sort(reverse=True)
    for _held, _pin, _seen, symbol in extra[room:]:
        rows.pop(symbol, None)
        _delete(symbol)
        log.info("dca_universe: evicted %s (cap %d)", symbol, MAX_TRACKED)


def _delete(ticker: str) -> None:
    table = _get_table()
    if table is None:
        return
    try:
        table.delete_item(Key={"ticker": ticker})
    except Exception as exc:  # noqa: BLE001
        log.warning("dca_universe: could not delete %s: %s", ticker, exc)


def forget(ticker: str) -> bool:
    """Remove *ticker* from the registry. Seed names cannot be removed.

    Returns whether anything changed. A reader who added a symbol by opening it
    needs a way to take it out again, or the list only ever accretes and the cap
    ends up evicting something they wanted rather than the mistake.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol or symbol in seed():
        return False
    rows = tracked()
    if symbol not in rows:
        return False
    rows.pop(symbol, None)
    _delete(symbol)
    _write_disk(rows)
    log.info("dca_universe: stopped tracking %s", symbol)
    return True


def pinned() -> set[str]:
    """Tracked names the reader asked to keep. Seed names are not among them —
    they are already unevictable and marking them would only overstate the
    pin budget."""
    return {symbol for symbol, row in tracked().items()
            if row.get("source") == PINNED and symbol not in seed()}


def held() -> set[str]:
    """Tracked names that are one of the reader's own holdings."""
    return {symbol for symbol, row in tracked().items()
            if row.get("source") == HELD and symbol not in seed()}


def sync_held(symbols: Iterable[str]) -> dict[str, Any]:
    """Reconcile the held set against the reader's current positions.

    *symbols* is the whole portfolio, in the order the caller wants them
    admitted — largest position first is the sane one, because the cap can bite.
    Returns what changed and, more usefully, what did not fit.

    Three deliberate refusals to do the obvious thing:

    * **Only names already in the registry are marked.** Registration stays
      gated on a rebuild that actually scored, which is what keeps an ETF — the
      thing most portfolios are mostly made of — out of a table headed "all
      scored names". A holding that has never been built comes back in
      ``not_tracked`` so the caller can queue it for the warm instead.
    * **It does not evict to make room.** A holding that does not fit is
      reported, not forced in over something else. The cap is a daily Yahoo
      bill, and quietly doubling it because somebody imported a broker CSV is
      the failure this whole module exists to prevent.
    * **Demotion is to ``opened``, never to deleted.** Selling a position is not
      a reason to lose its reconstruction; it just stops being protected.

    ``not_tracked`` and ``no_room`` are separated because they need different
    actions and are easy to confuse. An untracked holding in a registry with
    space is simply waiting for its first build, and the warm will get to it. An
    untracked holding in a registry whose whole non-seed budget is already
    protected will *never* arrive however long anyone waits — nothing is
    evictable, so there is no slot for it — and the only fix is to release
    something or raise the cap, which is a decision about daily spend.
    """
    wanted = [s.strip().upper() for s in symbols if s and s.strip()]
    wanted = [s for s in dict.fromkeys(wanted) if s not in seed()]
    rows = tracked()
    room = max(0, MAX_TRACKED - len(seed()))

    marked, not_tracked, no_room = [], [], []
    for symbol in wanted:
        if symbol in rows:
            if rows[symbol].get("source") != HELD:
                _set_source(symbol, HELD)
                # Keep the local snapshot in step. `_set_source` re-reads the
                # store, so without this the `evictable` count below still sees
                # rows this pass has already protected and reports a holding as
                # "awaiting a build" when in fact there is no slot for it.
                rows[symbol] = {**rows[symbol], "source": HELD}
            marked.append(symbol)
            continue
        # Absent. Whether that is temporary depends on whether anything in the
        # table could ever be evicted to admit it.
        evictable = sum(1 for s, r in rows.items()
                        if s not in seed() and r.get("source") != HELD)
        if len(marked) >= room and not evictable:
            no_room.append(symbol)
        else:
            not_tracked.append(symbol)

    still = set(marked)
    demoted = [s for s in held() if s not in still]
    for symbol in demoted:
        _set_source(symbol, "opened")

    if marked or demoted:
        log.info("dca_universe: held sync — %d marked, %d demoted, "
                 "%d awaiting a build, %d with no slot",
                 len(marked), len(demoted), len(not_tracked), len(no_room))
    return {"held": sorted(still), "marked": len(marked),
            "demoted": sorted(demoted), "not_tracked": not_tracked,
            "no_room": no_room, "max_held": room}


def pin(ticker: str) -> dict[str, Any]:
    """Protect *ticker* from recency eviction. Reports what it did, and why not.

    Never silently succeeds-as-noop and never silently evicts to make room. The
    three refusals are all cases where doing the obvious thing would be worse
    than saying no:

    ``seed``
        Already unevictable. Reporting it as pinned would consume a slot in the
        reader's pin budget for a guarantee they already had.
    ``not_tracked``
        Registration happens on a rebuild that actually scored — see
        :func:`remember`. Creating a row here would put a name in a table headed
        "all scored names" without knowing that it can score, which is precisely
        what keeps ETFs out of it.
    ``no_room``
        Every non-seed slot is already pinned. Pinning this one would mean
        evicting another pinned name, i.e. losing one saved list entry to gain
        another, which is the behaviour being complained about rather than a fix
        for it. The cap is a daily Yahoo bill and is not negotiable here.

        Checked *before* ``not_tracked``, which looks like the wrong order and
        is not: with the budget full, :func:`remember` evicts a newly opened
        name on the way through, so the symbol the reader is looking at is
        genuinely absent from the table — and telling them "not tracked" sends
        them to rebuild it, which will not help. The full budget is the cause
        and the only thing they can act on.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return {"pinned": False, "reason": "not_tracked", "ticker": symbol}
    if symbol in seed():
        return {"pinned": False, "reason": "seed", "ticker": symbol}

    rows = tracked()
    row = rows.get(symbol)
    if row is not None and row.get("source") == PINNED:
        return {"pinned": True, "reason": "already", "ticker": symbol}

    room = max(0, MAX_TRACKED - len(seed()))
    have = pinned()
    if len(have) >= room:
        return {"pinned": False, "reason": "no_room", "ticker": symbol,
                "pinned_count": len(have), "max_pinned": room}
    if row is None:
        return {"pinned": False, "reason": "not_tracked", "ticker": symbol}

    _set_source(symbol, PINNED)
    log.info("dca_universe: pinned %s (%d pinned of %d slots)",
             symbol, len(pinned()), room)
    return {"pinned": True, "ticker": symbol}


def unpin(ticker: str) -> dict[str, Any]:
    """Return *ticker* to ordinary recency eviction. It stays tracked.

    Distinct from :func:`forget`, which removes it outright. Unpinning says
    "stop protecting this", not "lose it now".
    """
    symbol = (ticker or "").strip().upper()
    rows = tracked()
    if not symbol or symbol not in rows:
        return {"pinned": False, "reason": "not_tracked", "ticker": symbol}
    if rows[symbol].get("source") != PINNED:
        return {"pinned": False, "reason": "already", "ticker": symbol}
    _set_source(symbol, "opened")
    log.info("dca_universe: unpinned %s", symbol)
    return {"pinned": False, "ticker": symbol}


def _set_source(symbol: str, source: str) -> None:
    """Rewrite one row's ``source``, in the table and the mirror.

    Separate from :func:`remember` because that one deliberately *preserves* an
    existing ``source`` -- it is called on every view, and letting a view reset
    the marker would unpin a name simply for being looked at.
    """
    rows = tracked()
    row = rows.get(symbol)
    if row is None:
        return
    row["source"] = source
    # Recency is bumped too: pinning is an interaction, and leaving the row
    # stale would make a just-pinned name the first candidate the moment it is
    # unpinned again.
    row["last_seen_at"] = time.time()
    rows[symbol] = row

    table = _get_table()
    if table is not None:
        try:
            table.put_item(Item={
                "ticker": symbol,
                "added_at": str(round(_num(row.get("added_at")), 3)),
                "last_seen_at": str(round(_num(row.get("last_seen_at")), 3)),
                "source": source,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("dca_universe: could not re-source %s: %s", symbol, exc)
    _write_disk(rows)


def touch(ticker: str) -> None:
    """Bump recency for an already-registered ticker, cheaply.

    Recency is what eviction sorts on, so a name somebody keeps opening must not
    be dropped for one they opened once. Unregistered tickers are ignored --
    registration is :func:`remember`'s job and is gated on actually scoring.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol or symbol in seed():
        return
    if symbol in tracked():
        remember(symbol)


def stats() -> dict[str, Any]:
    """Registry size and capacity, for the page to explain an eviction."""
    rows = tracked()
    protected = seed()
    return {
        "tracked": len(rows),
        "seed": len(protected),
        "effective": len(all_tickers()),
        "max": MAX_TRACKED,
        "room": max(0, MAX_TRACKED - len(protected) -
                    len([s for s in rows if s not in protected])),
        # Both numbers, because "room: 0" alone does not tell a reader whether
        # the next name they open will cost them one they wanted. With 37 of 37
        # slots pinned nothing is evictable and the *new* name is the one that
        # will not stick; with 0 pinned, every one of theirs is a candidate.
        "pinned": len(pinned()),
        "max_pinned": max(0, MAX_TRACKED - len(protected)),
        # Holdings are protected above pins, so this is the share of the
        # non-seed budget that is spoken for before anything can be evicted.
        "held": len(held()),
    }
