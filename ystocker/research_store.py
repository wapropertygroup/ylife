"""
ystocker.research_store
~~~~~~~~~~~~~~~~~~~~~~~
The deep-research reports a signed-in reader generated on ``/history/<ticker>``
(the ✦ Research tab), kept so the page can show them again.

Why a table and not the disk cache
----------------------------------
Until 2026-10-01 a report lived only in ``cache/research/``, for eight hours,
keyed by a fingerprint of its inputs. That is a cache and it behaved like one:
reopen the tab the next day and the report was gone, each of the box's two
rebuilds took the directory with it, and nothing could put last week's report
beside this week's. Nor is a report recomputable -- the model, the pages its
search read and the prices it was written against all move -- so by this repo's
own test ("can anyone sell it back to me tomorrow?") it is an observed record.

Who may read a row
------------------
Only the reader who generated it. A report written with the position form filled
in states the account value, shares, cost and the buy plan built on them
(§1, §12–§14) -- the same reason ``agents.owns`` keeps a run private to whoever
paid for it. Ownership is enforced by the key rather than by a check beside it:
every read builds the sort key from the session's address, so no request a
reader can make reaches another reader's row.

The same reasoning rules out the tempting extension, a report shown to every
visitor of the page. The bundle a report is written from is assembled in the
browser and POSTed, so the server cannot vouch for a single number in it, and
publishing one reader's output as the page's research would make that POST a
way to put arbitrary text on trade-agents.com -- the incident ``inbox.py`` gates
its reads to prevent.

A reader who is not signed in still gets a report. It is not saved, because
there is nobody to save it for, and the stream says so.

Failure
-------
Reads raise :class:`StoreUnavailable` and the routes answer 503, never an empty
list: "you have no saved reports" is the one wrong answer on the panel whose job
is to show them, and a reader who believes them lost will regenerate instead of
retrying. A failed save is reported on the stream and costs nothing else -- the
report is already on screen, and generation never depends on this table.

Key schema
----------
``ticker`` (HASH, S) + ``sk`` (RANGE, S) = ``<owner>#<ref>``, where ``ref`` is
``<YYYYMMDDTHHMMSSffffffZ>-<id>``. One Query with ``begins_with`` answers "this
reader's reports on this ticker, newest first", because the stamp sorts as time.
Partitioned on the ticker rather than the reader so a per-ticker read never
touches another ticker's rows. ``ref`` is URL-safe, and with the session's
address it rebuilds the whole key, which is what makes ownership structural.

``#`` is the delimiter, so an address containing one is refused rather than
escaped: no provider issues one, and a delimiter inside a key is exactly how one
reader's prefix would come to match another's.

The body is gzipped. A Chinese report is ~21 KB of UTF-8 and about a third of
that compressed, and a Query is billed on the whole item whatever it projects.

No TTL: these are what the reader asked to keep. Not in
``deploy/cloudformation.yaml``, matching every other hand-made table here (it
cannot adopt a live table without an import operation), and IAM already grants
``table/ystocker-*``::

    aws dynamodb create-table --table-name ystocker-research-reports \\
      --region us-west-2 --billing-mode PAY_PER_REQUEST \\
      --attribute-definitions AttributeName=ticker,AttributeType=S \\
                              AttributeName=sk,AttributeType=S \\
      --key-schema AttributeName=ticker,KeyType=HASH \\
                   AttributeName=sk,KeyType=RANGE
"""
from __future__ import annotations

import gzip
import hashlib
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

__all__ = [
    "TABLE_NAME", "LIST_LIMIT", "MAX_TEXT", "StoreUnavailable",
    "normalise_ticker", "normalise_owner", "valid_ref", "make_ref",
    "build_item", "row_from_item", "meta", "with_text", "cache_hit",
    "pick_latest", "save", "list_for", "get", "delete",
]

TABLE_NAME = (os.environ.get("RESEARCH_TABLE", "ystocker-research-reports").strip()
              or "ystocker-research-reports")

#: How many of one reader's reports on one ticker are read and offered in the
#: picker. Bounds the read; older rows stay in the table.
LIST_LIMIT = 20

#: Characters. The generation is capped at 24,576 output tokens, which is well
#: under this in either language, so the cap only ever meets a runaway.
MAX_TEXT = 200_000

#: DynamoDB refuses an item over 400 KB; this leaves room for the attributes.
_MAX_BODY_BYTES = 380_000

_MAX_SOURCES = 12
_MAX_SOURCE_CHARS = 200

_REF_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{16}$")
# Wider than agents.valid_ticker on purpose: research runs on whatever /history
# opens, which includes indices (^GSPC) and futures (GC=F). The pattern is for
# key hygiene, not market correctness.
_TICKER_RE = re.compile(r"^[A-Z0-9.^=\-]{1,20}$")

_table = None
_table_unavail_until = 0.0
_lock = threading.Lock()


class StoreUnavailable(RuntimeError):
    """The table could not be reached. Never swallowed into an empty answer."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalise_ticker(raw: Any) -> Optional[str]:
    t = str(raw or "").strip().upper()
    return t if _TICKER_RE.match(t) else None


def normalise_owner(raw: Any) -> Optional[str]:
    """The session's address as a key component, or None.

    Refuses rather than escapes a ``#`` -- see the module docstring.
    """
    addr = str(raw or "").strip().lower()
    if not addr or "@" not in addr or "#" in addr or len(addr) > 254:
        return None
    return addr


def valid_ref(raw: Any) -> Optional[str]:
    ref = str(raw or "").strip()
    return ref if _REF_RE.match(ref) else None


def make_ref(now: datetime, rid: str) -> str:
    """``20261001T140311000000Z-<id>``: sorts as time, safe in a URL path.

    Microseconds, not seconds: two saves inside one second would otherwise
    share a stamp and fall back to the random id for their order, and "newest
    first" would be a coin toss -- caught by the endpoint check, whose saves
    land in the same second."""
    return f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{rid}"


def _price_str(value: Any) -> Optional[str]:
    """A price as a string, or None. DynamoDB rejects floats, and a string keeps
    the figure exactly as it was rather than forcing a Decimal rounding."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f"{f:.4f}".rstrip("0").rstrip(".")


def build_item(*, ticker: str, owner: str, text: str, lang: str,
               fingerprint: str = "", template: str = "", model: str = "",
               sources: Iterable[str] = (), degraded: bool = False,
               truncated: bool = False, has_portfolio: bool = False,
               price: Any = None, now: Optional[datetime] = None,
               rid: Optional[str] = None) -> dict[str, Any]:
    """The DynamoDB item for one report. Raises ValueError on input that must
    not be stored, before anything is written."""
    t = normalise_ticker(ticker)
    who = normalise_owner(owner)
    if not t:
        raise ValueError("invalid ticker")
    if not who:
        raise ValueError("invalid owner")
    body = str(text or "")
    if not body.strip():
        raise ValueError("empty report")
    if len(body) > MAX_TEXT:
        raise ValueError(f"report is {len(body)} characters; the cap is {MAX_TEXT}")
    packed = gzip.compress(body.encode("utf-8"), compresslevel=6)
    if len(packed) > _MAX_BODY_BYTES:
        raise ValueError(f"compressed report is {len(packed)} bytes")

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rid = rid or uuid.uuid4().hex[:16]
    ref = make_ref(stamp, rid)
    item: dict[str, Any] = {
        "ticker": t,
        "sk": f"{who}#{ref}",
        "id": rid,
        "owner": who,
        "created_at": stamp.isoformat(timespec="seconds"),
        "lang": "zh" if lang == "zh" else "en",
        "body": packed,
        "chars": len(body),
        "digest": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        "degraded": bool(degraded),
        "truncated": bool(truncated),
        "has_portfolio": bool(has_portfolio),
    }
    for key, value, cap in (("fingerprint", fingerprint, 32),
                            ("template", template, 16),
                            ("model", model, 64)):
        value = str(value or "").strip()[:cap]
        if value:
            item[key] = value
    clean = [str(s).strip()[:_MAX_SOURCE_CHARS] for s in (sources or ())]
    clean = [s for s in clean if s][:_MAX_SOURCES]
    if clean:
        item["sources"] = clean
    p = _price_str(price)
    if p is not None:
        item["price"] = p
    return item


def row_from_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """A stored item back into a plain dict, body decompressed.

    A body that will not decompress is handed back flagged ``unreadable`` rather
    than dropped: the failure is ours, and a row that silently vanishes from the
    picker reads as a report that was never saved.
    """
    sk = str(item.get("sk") or "")
    raw = item.get("body")
    raw = getattr(raw, "value", raw)          # boto3 hands back a Binary
    text, unreadable = "", False
    try:
        text = gzip.decompress(bytes(raw)).decode("utf-8") if raw is not None else ""
    except (EOFError, OSError, TypeError, ValueError, UnicodeDecodeError) as exc:
        log.warning("research_store: unreadable body %s: %s", sk, exc)
        unreadable = True
    try:
        chars = int(item.get("chars") or len(text))
    except (TypeError, ValueError):
        chars = len(text)
    price: Optional[float]
    try:
        price = float(item["price"]) if item.get("price") is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "ticker": str(item.get("ticker") or ""),
        "ref": sk.split("#", 1)[1] if "#" in sk else "",
        "owner": str(item.get("owner") or ""),
        "created_at": str(item.get("created_at") or ""),
        "lang": str(item.get("lang") or "en"),
        "fingerprint": str(item.get("fingerprint") or ""),
        "template": str(item.get("template") or ""),
        "model": str(item.get("model") or ""),
        "sources": [str(s) for s in (item.get("sources") or [])],
        "degraded": bool(item.get("degraded")),
        "truncated": bool(item.get("truncated")),
        "has_portfolio": bool(item.get("has_portfolio")),
        "price": price,
        "chars": chars,
        "digest": str(item.get("digest") or ""),
        "text": text,
        "unreadable": unreadable,
    }


#: What a row says about itself in the picker. An allowlist, as everywhere else
#: a record leaves this box: the owner is the viewer, so sending it back says
#: nothing, and a field added to the row later stays private until named here.
_META_FIELDS = ("ref", "created_at", "lang", "model", "template",
                "degraded", "truncated", "has_portfolio", "price", "chars",
                "unreadable")


def meta(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in _META_FIELDS}


def with_text(row: Mapping[str, Any]) -> dict[str, Any]:
    """One report in full. ``sources`` travels only here, not on each picker
    row: twelve page titles times twenty rows is most of a list's payload."""
    return {**meta(row), "sources": list(row.get("sources") or []),
            "text": row.get("text") or ""}


def _age_seconds(row: Mapping[str, Any], now: datetime) -> Optional[float]:
    try:
        made = datetime.fromisoformat(str(row.get("created_at") or ""))
    except ValueError:
        return None
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    return (now - made).total_seconds()


def cache_hit(rows: Iterable[Mapping[str, Any]], *, lang: str, fingerprint: str,
              template: str, max_age: float,
              now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """The newest saved report that answers this request as the disk cache would.

    Same inputs (fingerprint), same language, same template, inside the TTL --
    and complete: a truncated report is saved so the reader keeps what they were
    shown, but it is not an answer to "generate", for the reason the disk cache
    never stored one. A degraded (search-free) report is, as it was on disk.
    """
    now = now or datetime.now(timezone.utc)
    for row in rows:
        if (row.get("lang") != lang or row.get("fingerprint") != fingerprint
                or row.get("template") != template):
            continue
        if row.get("truncated") or row.get("unreadable") or not row.get("text"):
            continue
        age = _age_seconds(row, now)
        if age is not None and -300 <= age < max_age:
            return dict(row)
    return None


def pick_latest(rows: Iterable[Mapping[str, Any]], lang: str) -> Optional[dict[str, Any]]:
    """The report the tab opens on: the newest in the page's language, else the
    newest at all. A Chinese page with only an English report shows that report,
    labelled, rather than an empty tab that looks like nothing was ever saved."""
    rows = list(rows)
    for row in rows:
        if row.get("lang") == lang:
            return dict(row)
    return dict(rows[0]) if rows else None


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def _get_table():
    """The table, or None after a failed connect (retried after 60 s, so a table
    created by hand is picked up without a restart)."""
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

            tbl = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2")
            ).Table(TABLE_NAME)
            tbl.load()
            _table = tbl
            log.info("research_store: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("research_store: DynamoDB unavailable: %s", exc)
            _table = None
            _table_unavail_until = time.time() + 60
        return _table


def _require_table():
    table = _get_table()
    if table is None:
        raise StoreUnavailable(TABLE_NAME)
    return table


def save(*, ticker: str, owner: str, text: str, lang: str, **fields: Any) -> dict[str, Any]:
    """Store one report and return it as a row. Raises ValueError for input that
    must not be stored, :class:`StoreUnavailable` when the write did not happen."""
    item = build_item(ticker=ticker, owner=owner, text=text, lang=lang, **fields)
    table = _require_table()
    try:
        table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        log.warning("research_store: put failed for %s: %s", item["sk"], exc)
        raise StoreUnavailable(str(exc)) from exc
    log.info("research_store: saved %s %s (%s, %d chars)",
             item["ticker"], item["sk"].split("#", 1)[1], item["lang"], item["chars"])
    return row_from_item(item)


def list_for(ticker: str, owner: str, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """This reader's reports on ``ticker``, newest first. [] only when there are
    none -- an unreachable table raises."""
    t, who = normalise_ticker(ticker), normalise_owner(owner)
    if not t or not who:
        return []
    table = _require_table()
    from boto3.dynamodb.conditions import Key

    limit = max(1, min(int(limit or LIST_LIMIT), 100))
    request: dict[str, Any] = {
        "KeyConditionExpression": Key("ticker").eq(t) & Key("sk").begins_with(who + "#"),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    rows: list[dict[str, Any]] = []
    try:
        while len(rows) < limit:
            resp = table.query(**request)
            rows.extend(row_from_item(i) for i in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            request["ExclusiveStartKey"] = last
            request["Limit"] = limit - len(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("research_store: query failed for %s: %s", t, exc)
        raise StoreUnavailable(str(exc)) from exc
    return rows[:limit]


def _key(ticker: str, owner: str, ref: str) -> Optional[dict[str, str]]:
    t, who, r = normalise_ticker(ticker), normalise_owner(owner), valid_ref(ref)
    if not t or not who or not r:
        return None
    return {"ticker": t, "sk": f"{who}#{r}"}


def get(ticker: str, owner: str, ref: str) -> Optional[dict[str, Any]]:
    """One of this reader's reports, or None. The owner half of the key comes
    from the caller's session, so another reader's ``ref`` simply misses."""
    key = _key(ticker, owner, ref)
    if key is None:
        return None
    table = _require_table()
    try:
        item = table.get_item(Key=key).get("Item")
    except Exception as exc:  # noqa: BLE001
        log.warning("research_store: get failed for %s: %s", key["sk"], exc)
        raise StoreUnavailable(str(exc)) from exc
    return row_from_item(item) if item else None


def delete(ticker: str, owner: str, ref: str) -> bool:
    """Remove one of this reader's reports. False when there was no such row."""
    key = _key(ticker, owner, ref)
    if key is None:
        return False
    table = _require_table()
    try:
        resp = table.delete_item(Key=key, ReturnValues="ALL_OLD")
    except Exception as exc:  # noqa: BLE001
        log.warning("research_store: delete failed for %s: %s", key["sk"], exc)
        raise StoreUnavailable(str(exc)) from exc
    gone = bool(resp.get("Attributes"))
    if gone:
        log.info("research_store: deleted %s %s", key["ticker"], key["sk"].split("#", 1)[1])
    return gone
