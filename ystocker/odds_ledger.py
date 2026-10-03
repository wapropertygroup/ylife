"""
ystocker.odds_ledger
~~~~~~~~~~~~~~~~~~~~
The record of every AI read on /predictions, and what happened next.

Each read is written down the moment it exists -- the probability the model
gave each outcome, the price the market had on it at that same moment, the
time, the model -- and when the market resolves, both are scored against the
answer. That is the whole test of whether a read was worth having: not whether
it sounded convincing, but whether over many questions it priced them better
than the market it was set beside.

Scoring is the Brier score, mean squared error over the outcomes (see
``odds.brier``), computed identically for the AI and for the market price it
was compared with, over exactly the same outcomes. A read and the market are
never scored on different questions, and nothing is scored before it resolves.

An observed series, on the same terms as ``decisions.py``'s ledger of agent
runs: a row records what was believed on a day, from evidence that is not kept,
by a model that will change. Lose it and it is gone, which is why it lives in
DynamoDB and not only in ``cache/``. Unlike the portfolio store, it degrades
rather than failing closed when the table is missing -- the read itself is still
shown, and the disk mirror keeps the row until the table exists -- because a
missing record here costs a data point, not a reader's holdings.

Key schema: ``bucket`` (``YYYY-MM`` of the read) HASH + ``sk``
(``<iso8601>#<event key>``) RANGE, so "the recent reads" is one Query per month
walked back rather than a Scan, which ``PAY_PER_REQUEST`` bills by volume. The
row itself is one JSON string attribute (``payload``): DynamoDB refuses floats,
and a row is only ever read whole.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from ystocker import odds

log = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("PREDICTIONS_LEDGER_TABLE", "ystocker-prediction-reads").strip()
DISK_PATH = Path(__file__).parent.parent / "cache" / "predictions" / "ledger.json"

#: Months of buckets read back. A Fed decision a year out is the longest-dated
#: thing on the page, so anything older has long since settled.
MAX_BUCKETS = 13

#: Disk mirror cap. Far above any plausible volume at the AI read's daily cap.
_DISK_CAP = 5000

#: How long rows() trusts its last answer. Reading the table is a Query per
#: month walked back; the page asks on every load.
_MEMO_SECONDS = 90

#: Settlement cadence. Markets resolve when the data prints or the meeting
#: ends, and nothing about a Brier score is urgent to the minute.
SETTLE_EVERY_SECONDS = 6 * 3600
#: Between two settlement requests.
_SETTLE_SPACING_SECONDS = 0.3

_lock = threading.Lock()
_table = None
_table_unavail_until = 0.0
_memo: tuple[float, list[dict[str, Any]]] = (0.0, [])


# ── Rows ────────────────────────────────────────────────────────────────────

def row_from_read(read: Mapping[str, Any]) -> dict[str, Any]:
    """The ledger's copy of an AI read: what is needed to score it, and to say
    what it was, and nothing a reader typed. The prose stays in the read file."""
    created = str(read.get("created_at") or "")
    key = str(read.get("key") or "")
    return {
        "bucket": created[:7],
        "sk": f"{created}#{key}",
        "key": key,
        "platform": read.get("platform"),
        "event_id": read.get("event_id"),
        "title": read.get("title"),
        "subtitle": read.get("subtitle") or "",
        "url": read.get("url"),
        "kind": read.get("kind"),
        "topic": read.get("topic"),
        "closes": read.get("closes"),
        "created_at": created,
        "model": read.get("model"),
        "market_as_of": read.get("market_as_of"),
        "confidence": read.get("confidence"),
        "outcomes": [{
            "label": o.get("label"),
            "id": o.get("id"),
            "market_p": o.get("market_p"),
            "ai_p": o.get("ai_p"),
        } for o in read.get("outcomes") or []],
        "status": "open",
        "brier_ai": None,
        "brier_market": None,
        "settled_at": None,
    }


# ── Disk mirror ─────────────────────────────────────────────────────────────

def _disk_rows() -> list[dict[str, Any]]:
    try:
        data = json.loads(DISK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("odds_ledger: unreadable disk mirror (%s)", exc)
        return []
    return [r for r in data if isinstance(r, dict) and r.get("sk")] if isinstance(data, list) else []


def _disk_upsert(row: Mapping[str, Any]) -> None:
    with _lock:
        rows = {r["sk"]: r for r in _disk_rows()}
        rows[row["sk"]] = dict(row)
        ordered = sorted(rows.values(), key=lambda r: r["sk"])[-_DISK_CAP:]
        try:
            DISK_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(DISK_PATH.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(ordered, fh, ensure_ascii=False, allow_nan=False)
            os.replace(tmp, DISK_PATH)
        except Exception as exc:  # noqa: BLE001
            log.warning("odds_ledger: could not write disk mirror: %s", exc)


# ── DynamoDB ────────────────────────────────────────────────────────────────

def _get_table():
    """The table, or None (missing, no credentials, or unreachable). A miss is
    remembered for a minute so a page load does not pay for a failed connect."""
    global _table, _table_unavail_until
    if _table is not None:
        return _table
    if time.time() < _table_unavail_until:
        return None
    try:
        import boto3

        tbl = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2")
                             ).Table(TABLE_NAME)
        tbl.load()
        _table = tbl
        log.info("odds_ledger: DynamoDB connected: %s", TABLE_NAME)
    except Exception as exc:  # noqa: BLE001
        log.warning("odds_ledger: DynamoDB unavailable (%s); disk mirror only", exc)
        _table = None
        _table_unavail_until = time.time() + 60
    return _table


def _ddb_put(row: Mapping[str, Any]) -> bool:
    table = _get_table()
    if table is None:
        return False
    body = {k: v for k, v in row.items() if k not in ("bucket", "sk")}
    try:
        table.put_item(Item={"bucket": row["bucket"], "sk": row["sk"],
                             "payload": json.dumps(body, ensure_ascii=False)})
    except Exception as exc:  # noqa: BLE001
        log.warning("odds_ledger: put failed for %s: %s", row.get("sk"), exc)
        return False
    return True


def _buckets(now: Optional[datetime] = None) -> list[str]:
    cursor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(day=1)
    out = []
    for _ in range(MAX_BUCKETS):
        out.append(cursor.strftime("%Y-%m"))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return out


def _ddb_rows() -> list[dict[str, Any]]:
    table = _get_table()
    if table is None:
        return []
    from boto3.dynamodb.conditions import Key

    out: list[dict[str, Any]] = []
    for bucket in _buckets():
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("bucket").eq(bucket)}
        while True:
            try:
                resp = table.query(**kwargs)
            except Exception as exc:  # noqa: BLE001
                log.warning("odds_ledger: query failed for %s: %s", bucket, exc)
                return out
            for item in resp.get("Items", []):
                try:
                    body = json.loads(item.get("payload") or "{}")
                except ValueError:
                    continue
                if isinstance(body, dict):
                    out.append({**body, "bucket": item.get("bucket"), "sk": item.get("sk")})
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    return out


# ── Public ──────────────────────────────────────────────────────────────────

def record(read: Mapping[str, Any]) -> dict[str, Any]:
    """Write one read into the ledger. Never raises: the disk mirror always
    takes it, the table takes it when it exists."""
    global _memo
    row = row_from_read(read)
    if not row["bucket"] or not row["key"]:
        log.warning("odds_ledger: refusing a read with no time or key")
        return row
    _disk_upsert(row)
    _ddb_put(row)
    _memo = (0.0, [])
    return row


def _merge(*sources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Union by ``sk``. A settled copy beats an open one, so a row settled
    while the table was down is not reverted by the table's older copy."""
    rank = {"settled": 2, "void": 2, "open": 1}
    out: dict[str, dict[str, Any]] = {}
    for rows in sources:
        for row in rows:
            sk = row.get("sk")
            if not sk:
                continue
            have = out.get(sk)
            if have is None or rank.get(row.get("status"), 0) > rank.get(have.get("status"), 0):
                out[sk] = dict(row)
    return sorted(out.values(), key=lambda r: r["sk"], reverse=True)


def rows(limit: int = 200, *, fresh: bool = False) -> list[dict[str, Any]]:
    """Recorded reads, newest first: the table and the disk mirror unioned."""
    global _memo
    stamp, cached = _memo
    if fresh or time.time() - stamp > _MEMO_SECONDS:
        cached = _merge(_ddb_rows(), _disk_rows())
        _memo = (time.time(), cached)
    return [dict(r) for r in cached[:max(1, int(limit))]]


def _save(row: Mapping[str, Any]) -> None:
    global _memo
    _disk_upsert(row)
    _ddb_put(row)
    _memo = (0.0, [])


def settle_pass(*, fetch_polymarket: Optional[Callable[[str], Optional[dict]]] = None,
                fetch_kalshi: Optional[Callable[[str], Optional[dict]]] = None,
                now: Optional[datetime] = None) -> dict[str, int]:
    """Look up every open row whose market has closed, and score the ones that
    have resolved. Fetchers are injectable so the logic is testable offline;
    by default they are ``predictions``' own."""
    if fetch_polymarket is None or fetch_kalshi is None:
        from ystocker import predictions

        fetch_polymarket = fetch_polymarket or predictions.fetch_polymarket_market
        fetch_kalshi = fetch_kalshi or predictions.fetch_kalshi_market
    now = now or datetime.now(timezone.utc)
    counts = {"checked": 0, "updated": 0, "settled": 0, "void": 0}
    asked = 0
    for row in rows(limit=_DISK_CAP, fresh=True):
        if row.get("status") != "open":
            continue
        closes = odds.parse_ts(row.get("closes"))
        if closes is not None and closes > now:
            continue
        counts["checked"] += 1
        changed = False
        for o in row.get("outcomes") or []:
            if o.get("result") is not None or not o.get("id"):
                continue
            if asked:
                time.sleep(_SETTLE_SPACING_SECONDS)
            asked += 1
            if row.get("platform") == "polymarket":
                result = odds.polymarket_result(fetch_polymarket(o["id"]) or {})
            else:
                result = odds.kalshi_result(fetch_kalshi(o["id"]) or {})
            if result is not None:
                o["result"] = result
                changed = True
        scored = odds.score_row(row)
        if scored is not None:
            row.update(scored)
            row["settled_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            counts[scored["status"]] += 1
            changed = True
        if changed:
            counts["updated"] += 1
            _save(row)
    if counts["checked"]:
        log.info("odds_ledger: settle pass — %s", counts)
    return counts


def summary(limit: int = 1000) -> dict[str, Any]:
    """``odds.ledger_summary`` over the recorded rows."""
    return odds.ledger_summary(rows(limit=limit))


def start_background_thread() -> None:
    def _loop() -> None:
        time.sleep(15 * 60)     # well after boot; nothing here is urgent
        while True:
            try:
                settle_pass()
            except Exception:   # noqa: BLE001 - a bad pass must not end the loop
                log.exception("odds_ledger: settle pass failed")
            time.sleep(SETTLE_EVERY_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="odds-ledger").start()
    log.info("odds_ledger: settlement thread started (every %dh)", SETTLE_EVERY_SECONDS // 3600)
