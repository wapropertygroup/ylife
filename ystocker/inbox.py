"""
ystocker.inbox
~~~~~~~~~~~~~~
A write-only door for external systems, and the feed that renders what came
through it.

Anything that can make an HTTP request — a cron job, a broker webhook, a script
on another machine, a TradingAgents run — can POST a JSON message here and have
it appear on ``/inbox``. That is the whole feature. The shape is deliberately
loose: a handful of optional fields this module knows how to render, and
everything else preserved verbatim under ``data`` so a sender never has to ask
permission to add a field.

Why this module is careful out of proportion to its size
--------------------------------------------------------
It is the **first write endpoint on this box that is not behind a Google
session**. Every other POST here is reached by a signed-in human; this one is
reached by whoever holds a token, from anywhere. The access log already shows
what the internet does to a public address unprompted — probes for ``/.env``,
``/.git/config``, ``/phpinfo.php``, hundreds a day — so an unauthenticated
write endpoint would be found and filled, not in theory but within days.

Hence the four rules below, none of which are negotiable:

**No token configured means the door is shut, not open.** ``INBOX_TOKEN`` unset
returns 503 and stores nothing. The alternative — treating "no token" as "no
check" — is the single mistake that turns a missing SSM parameter into an open
relay, and it fails in the direction where nothing looks wrong.

**The comparison is constant-time.** ``hmac.compare_digest``, not ``==``. A
token compared with ``==`` leaks its prefix through timing, and this one is a
bearer credential with no second factor behind it.

**Every field is bounded before it is stored.** Not after, and not at render
time: an unbounded ``text`` is a way to fill a DynamoDB table and a page with
one request, and a length check that happens on the way *out* has already paid
for the storage. The caps are generous enough that a real message never hits
them and small enough that a runaway loop is visibly refused.

**Reads are gated even though writes are authenticated.** The page is
signed-in-only. A token that leaks is then a nuisance — somebody can fill your
inbox — rather than a publishing channel onto a public domain in your name.
Those are very different incidents, and the difference costs one decorator.

Storage
-------
``ystocker-inbox``, hash ``bucket`` (``YYYY-MM``) + range ``sk``
(``<iso8601>#<id>``). Bucketed by month rather than one fixed partition so the
partition cannot grow without bound, and range-keyed by time so "the most
recent fifty" is a Query with ``ScanIndexForward=False`` rather than a Scan —
on ``PAY_PER_REQUEST`` a Scan is billed by volume scanned, which is the trap
``ystocker-dca-history``'s key schema already documents.

Reading across a month boundary walks back a bounded number of buckets, so a
quiet January does not return an empty page while December is full.

TTL is on, at :data:`RETENTION_DAYS`. This is a feed, not a ledger: unlike the
observed series in ``dca_history`` these rows *can* be re-sent by whatever
produced them, so keeping them for ever buys nothing and costs a table that
only grows.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

__all__ = [
    "TABLE_NAME", "LEVELS", "MAX_BODY_BYTES", "RETENTION_DAYS",
    "token_configured", "check_token", "normalise", "InboxError",
    "put", "recent", "sources", "StoreUnavailable",
]

TABLE_NAME = os.environ.get("INBOX_TABLE", "ystocker-inbox").strip()

#: Rendered as a coloured chip, and an unknown value is coerced to ``info``
#: rather than refused — a sender inventing a level should not lose a message.
LEVELS: tuple[str, ...] = ("info", "success", "warn", "error")

#: Whole-request ceiling. Checked before parsing, so a 10 MB body is refused
#: without being read into memory and turned into a dict first.
MAX_BODY_BYTES = int(os.environ.get("INBOX_MAX_BODY", str(64 * 1024)))

#: Per-field caps. Generous for anything a person would send, small enough that
#: a loop is refused rather than absorbed.
MAX_TITLE = 200
MAX_TEXT = 8_000
MAX_SOURCE = 60
MAX_TICKER = 24
MAX_URL = 500
MAX_TAGS = 10
MAX_TAG = 40
#: The verbatim remainder, serialised. Bounded for the same reason as `text`.
MAX_DATA_BYTES = 16 * 1024

#: How long a message lives. See the module docstring on why this is not a
#: ledger.
RETENTION_DAYS = int(os.environ.get("INBOX_RETENTION_DAYS", "90"))

#: How many monthly buckets `recent` will walk back through before giving up.
#: Bounds the read cost of a quiet period; twelve is a year.
MAX_BUCKETS = 12

_table = None
_table_unavail_until = 0.0
_lock = threading.Lock()


class InboxError(ValueError):
    """A message that will not be stored, with a reason fit to return."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class StoreUnavailable(RuntimeError):
    """The table could not be reached.

    Raised rather than swallowed, and the route answers 503. A POST that is
    accepted and dropped is worse than one that is refused: the sender has no
    way to tell, and will not retry.
    """


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _token() -> str:
    return (os.environ.get("INBOX_TOKEN") or "").strip()


def token_configured() -> bool:
    """Whether a token exists at all. False means the endpoint refuses."""
    return bool(_token())


def check_token(presented: Optional[str]) -> bool:
    """Constant-time comparison against the configured token.

    Returns False when nothing is configured, so the caller cannot accidentally
    treat "no token set" as "no check required" — the one mistake that turns a
    missing SSM parameter into an open write endpoint.
    """
    expected = _token()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.strip(), expected)


def token_from_headers(headers: Mapping[str, str]) -> str:
    """Bearer header or ``X-Inbox-Token``, whichever is present.

    Two spellings because the senders are not browsers: a shell script reaching
    for ``curl -H 'X-Inbox-Token: ...'`` should not have to learn the Bearer
    convention, and a library that only speaks OAuth-ish auth should not have to
    avoid it.
    """
    auth = (headers.get("Authorization") or "").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (headers.get("X-Inbox-Token") or "").strip()


# ---------------------------------------------------------------------------
# Validation — pure, so the rules are testable without AWS
# ---------------------------------------------------------------------------

def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text.strip()[:limit]


def normalise(body: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Validate and shape one posted message. Pure; raises :class:`InboxError`.

    The promoted fields are optional individually and required collectively: a
    row with neither ``title`` nor ``text`` renders as an empty card, which is
    indistinguishable on the page from a bug in the sender. Refusing it is the
    only way the sender finds out.

    Unknown keys are kept rather than dropped. A sender adding a field should
    not have to coordinate with this file, and a message stored minus the part
    that mattered is a silent data loss — the page shows the remainder as JSON.
    """
    if not isinstance(body, dict):
        raise InboxError("not_an_object",
                         "the body must be a JSON object, not "
                         f"{type(body).__name__}")

    title = _clip(body.get("title"), MAX_TITLE)
    text = _clip(body.get("text") or body.get("message") or body.get("body"),
                 MAX_TEXT)
    if not title and not text:
        raise InboxError("empty", "supply at least one of `title` or `text`")

    level = _clip(body.get("level"), 20).lower()
    if level not in LEVELS:
        level = "info"

    tags: list[str] = []
    raw_tags = body.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = [t for t in raw_tags.replace(",", " ").split() if t]
    if isinstance(raw_tags, (list, tuple)):
        for tag in raw_tags[:MAX_TAGS]:
            cleaned = _clip(tag, MAX_TAG)
            if cleaned and cleaned not in tags:
                tags.append(cleaned)

    url = _clip(body.get("url"), MAX_URL)
    if url and not url.lower().startswith(("http://", "https://")):
        # Refused rather than dropped: a sender who meant to link something has
        # a broken message either way, and only one of those tells them.
        raise InboxError("bad_url", "`url` must start with http:// or https://")

    known = {"title", "text", "message", "body", "level", "tags", "url",
             "source", "ticker", "data"}
    extra = body.get("data")
    if not isinstance(extra, dict):
        extra = {}
    extra = {**{k: v for k, v in body.items() if k not in known}, **extra}
    if extra:
        try:
            encoded = json.dumps(extra, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            raise InboxError("unserialisable", f"`data` is not JSON: {exc}")
        if len(encoded.encode("utf-8")) > MAX_DATA_BYTES:
            raise InboxError("data_too_large",
                             f"the extra fields exceed {MAX_DATA_BYTES} bytes")

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ident = secrets.token_urlsafe(9)
    iso = stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "bucket": stamp.strftime("%Y-%m"),
        "sk": f"{iso}#{ident}",
        "id": ident,
        "received_at": iso,
        "title": title,
        "text": text,
        "level": level,
        "source": _clip(body.get("source"), MAX_SOURCE),
        "ticker": _clip(body.get("ticker"), MAX_TICKER).upper(),
        "tags": tags,
        "url": url,
        "data": extra,
        "expires_at": int(stamp.timestamp()) + RETENTION_DAYS * 86400,
    }


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def _get_table():
    """The table, or ``None``. Unlike the caches, callers must treat ``None``
    as a failure rather than a degraded mode — see :class:`StoreUnavailable`."""
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
            log.info("inbox: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("inbox: DynamoDB unavailable: %s", exc)
            _table = None
            _table_unavail_until = time.time() + 60
        return _table


def put(record: Mapping[str, Any]) -> dict[str, Any]:
    """Store one normalised message. Raises :class:`StoreUnavailable`."""
    table = _get_table()
    if table is None:
        raise StoreUnavailable(TABLE_NAME)
    item = {k: v for k, v in record.items() if v not in ("", None, [], {})}
    # `data` is stored as a JSON string rather than a map: DynamoDB rejects
    # empty strings inside nested attributes and floats outright, and a sender's
    # arbitrary object will eventually contain both.
    if record.get("data"):
        item["data"] = json.dumps(record["data"], ensure_ascii=False, default=str)
    try:
        table.put_item(Item=item)
    except Exception as exc:  # noqa: BLE001
        log.warning("inbox: put failed for %s: %s", record.get("id"), exc)
        raise StoreUnavailable(str(exc)) from exc
    return dict(record)


def _buckets(now: Optional[datetime] = None) -> list[str]:
    """Month keys, newest first, back as far as :data:`MAX_BUCKETS`."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out, cursor = [], stamp.replace(day=1)
    for _ in range(MAX_BUCKETS):
        out.append(cursor.strftime("%Y-%m"))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return out


def recent(limit: int = 50, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """The newest *limit* messages, newest first.

    Walks monthly buckets backwards only until it has enough, so the usual case
    is one Query. A quiet month costs one more, bounded by
    :data:`MAX_BUCKETS` — without that walk a page opened on the 1st of a month
    would show nothing while the previous month was full.
    """
    table = _get_table()
    if table is None:
        raise StoreUnavailable(TABLE_NAME)
    from boto3.dynamodb.conditions import Key

    limit = max(1, min(int(limit or 50), 200))
    rows: list[dict[str, Any]] = []
    for bucket in _buckets(now):
        if len(rows) >= limit:
            break
        try:
            resp = table.query(
                KeyConditionExpression=Key("bucket").eq(bucket),
                ScanIndexForward=False,
                Limit=limit - len(rows))
        except Exception as exc:  # noqa: BLE001
            log.warning("inbox: query failed for %s: %s", bucket, exc)
            raise StoreUnavailable(str(exc)) from exc
        for item in resp.get("Items", []):
            rows.append(_thaw(item))
    return rows[:limit]


def _thaw(item: Mapping[str, Any]) -> dict[str, Any]:
    """One stored item back into the shape the page renders.

    ``data`` round-trips through JSON; a row whose payload cannot be parsed is
    handed back as a string rather than dropped, because the failure is ours and
    losing the message hides it.
    """
    out = {k: v for k, v in item.items() if k not in ("expires_at",)}
    raw = out.get("data")
    if isinstance(raw, str):
        try:
            out["data"] = json.loads(raw)
        except (TypeError, ValueError):
            out["data"] = {"_unparsed": raw}
    out.setdefault("tags", [])
    return out


def sources(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Distinct ``source`` values present, for the page's filter chips.

    Derived from the rows already fetched rather than queried, because a
    distinct-values query over this table is a Scan and the filter only ever
    needs to cover what is on screen.
    """
    seen: list[str] = []
    for row in rows:
        name = (row.get("source") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return sorted(seen)
