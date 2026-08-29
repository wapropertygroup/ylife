"""
ystocker.portfolio
~~~~~~~~~~~~~~~~~~
Where a signed-in user's positions live.

This is the first thing in yStocker that stores something a user would be upset
to lose. Every other per-email store here is either reconstructible (``quota``
counters reset daily by design) or a ledger written by somebody else (``credits``,
topped up by yPay's Stripe webhook). A portfolio is typed or imported by hand and
exists nowhere else, which changes two decisions:

**Reads fail closed, loudly.** ``share.py`` fails closed because mailing a dead
link is worse than refusing to mail; ``quota.py`` fails open because losing a
counter costs a little API spend. Neither argument applies here. If DynamoDB is
unreachable this raises :class:`StoreUnavailable` and the route answers 503,
because the alternative — returning ``[]`` — renders as *"you have no positions"*
on a page whose entire job is to show them. A user seeing an empty portfolio
concludes their data is gone, and the honest recovery (wait, retry) is
indistinguishable from the dishonest one (re-import everything, now duplicated).

**There is no silent disk fallback.** The cache modules degrade to disk-only when
a table is missing, and ``CLAUDE.md`` notes an absent table is never an access
error because the instance role grants ``table/ystocker-*``. That is right for a
cache and wrong for this: the box is replaceable, so a portfolio written only to
its disk is a portfolio scheduled for deletion. Local development needs *some*
path, so a file store exists behind an explicit ``ASSETS_LOCAL_STORE=1`` opt-in
and is never reached by accident.

Storage shape
-------------
One item per user, holding the whole position list as a JSON string. Positions
are tens of rows, so an item is a few KB against DynamoDB's 400 KB limit, and the
whole-list-at-once shape is what a CSV import wants anyway — replacing a portfolio
becomes one atomic ``put_item`` rather than a diff.

The JSON-string body also sidesteps ``Decimal`` entirely. DynamoDB rejects
``float``, and this repo has three different conventions for coping with that
(``Decimal(str(round(x, n)))``, a stringified number, a serialised blob); the last
is the one with no rounding decisions in it, and quantities like ``150.5``
survive a round trip unexamined. Follows ``routes.py``'s markets-cache and
``agents.py``'s job payload.

The table is not in ``deploy/cloudformation.yaml``, matching the six observed-series
tables and for the same reason — CloudFormation cannot adopt a live table without
an import operation, so adding it would break the next ``--full`` deploy rather
than converge it. Create it by hand::

    aws dynamodb create-table --table-name ystocker-assets --region us-west-2 \\
      --billing-mode PAY_PER_REQUEST \\
      --attribute-definitions AttributeName=id,AttributeType=S \\
      --key-schema AttributeName=id,KeyType=HASH

No TTL: a portfolio does not expire.
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

TABLE_NAME = os.environ.get("ASSETS_TABLE", "ystocker-assets").strip() or "ystocker-assets"
REGION = (os.environ.get("AWS_REGION") or "us-west-2").strip()

#: Local file store, for development only. Off unless explicitly enabled, so a
#: production box that cannot reach DynamoDB fails visibly instead of writing
#: portfolios to an instance disk that will be replaced.
LOCAL_STORE = os.environ.get("ASSETS_LOCAL_STORE", "").strip() in ("1", "true", "yes")
LOCAL_PATH = Path(__file__).parent.parent / "cache" / "assets_local.json"

#: Ceiling per user. Generous against a real portfolio, and the reason it exists is
#: that the item must stay under DynamoDB's 400 KB limit -- a rejected write on
#: save would otherwise present as "your import vanished".
MAX_POSITIONS = 500

_table = None
_table_lock = threading.Lock()
_local_lock = threading.Lock()


class StoreUnavailable(RuntimeError):
    """The position store cannot be reached, so its contents are unknown.

    Deliberately distinct from "the user has no positions". Callers must not
    convert this into an empty list.
    """


# ---------------------------------------------------------------------------
# Position normalisation
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """A stored number as a float, or None. Rejects NaN/Inf and bools.

    ``isinstance(value, bool)`` is checked first because ``float(True) == 1.0``
    would turn a stray boolean into a one-share position. Mirrors
    ``fedwatch._number``.
    """
    import math

    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normalise(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One position, cleaned for storage. None if it is unusable.

    A position needs a symbol and *either* a quantity or a value: quantity alone
    can be priced, value alone is already what look-through needs, and neither
    means there is nothing to weigh.
    """
    from ystocker.portfolio_csv import normalise_symbol

    symbol = normalise_symbol(raw.get("symbol"))
    if not symbol:
        return None

    quantity = _num(raw.get("quantity"))
    value = _num(raw.get("value"))
    if value is None:
        value = _num(raw.get("market_value"))
    if quantity is None and value is None:
        return None
    if (quantity is not None and quantity < 0) or (value is not None and value < 0):
        return None

    out: dict[str, Any] = {"symbol": symbol}
    name = str(raw.get("name") or "").strip()
    if name:
        out["name"] = name[:120]
    if quantity is not None:
        out["quantity"] = round(quantity, 6)
    if value is not None:
        out["value"] = round(value, 2)
    cost = _num(raw.get("cost_basis"))
    if cost is not None and cost >= 0:
        out["cost_basis"] = round(cost, 2)
    account = str(raw.get("account") or "").strip()
    if account:
        out["account"] = account[:60]
    return out


def normalise_all(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean a whole list, dropping unusable rows and merging duplicate symbols.

    Duplicates are merged *here* rather than at import, because a portfolio that
    holds the same symbol on two rows would be double-counted by look-through
    without changing the total — every percentage would be right and the overlap
    table would claim you hold it "2 ways" when you hold it once.
    """
    merged: dict[str, dict[str, Any]] = {}
    for raw in rows:
        pos = normalise(raw)
        if pos is None:
            continue
        key = pos["symbol"]
        cur = merged.get(key)
        if cur is None:
            merged[key] = pos
            continue
        for field in ("quantity", "value", "cost_basis"):
            if field in pos:
                cur[field] = round((cur.get(field) or 0.0) + pos[field], 6)
        if not cur.get("name") and pos.get("name"):
            cur["name"] = pos["name"]
        accounts = [a for a in (cur.get("account"), pos.get("account")) if a]
        if accounts:
            cur["account"] = " + ".join(dict.fromkeys(accounts))[:60]
    return list(merged.values())[:MAX_POSITIONS]


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _key(email: str) -> str:
    """Partition key. ``u#`` prefixed, matching ``credits._key``."""
    return "u#" + (email or "").strip().lower()


def _get_table():
    """The assets table, or None when DynamoDB is unreachable.

    Unlike the cache modules, None is **not** treated as "no data" by callers —
    :func:`load` raises :class:`StoreUnavailable` instead. See the module
    docstring.
    """
    global _table
    if _table is not None:
        return _table
    with _table_lock:
        if _table is not None:
            return _table
        try:
            import boto3

            ddb = boto3.resource("dynamodb", region_name=REGION)
            table = ddb.Table(TABLE_NAME)
            table.load()
            _table = table
            log.info("portfolio: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - no table, no portfolios
            log.warning("portfolio: DynamoDB unavailable (%s)", exc)
            return None
    return _table


def _local_read() -> dict[str, Any]:
    try:
        return json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio: unreadable local store (%s)", exc)
        return {}


def _local_write(data: dict[str, Any]) -> None:
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LOCAL_PATH.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, LOCAL_PATH)


def available() -> tuple[bool, str]:
    """Whether positions can be stored, and which backend would be used."""
    if _get_table() is not None:
        return True, "dynamodb"
    if LOCAL_STORE:
        return True, "local"
    return False, "unavailable"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load(email: str) -> list[dict[str, Any]]:
    """This user's positions.

    Returns ``[]`` only when the user genuinely has none. Raises
    :class:`StoreUnavailable` when that cannot be determined — the caller must
    surface that rather than rendering an empty portfolio.
    """
    email = (email or "").strip().lower()
    if not email:
        return []

    table = _get_table()
    if table is None:
        if not LOCAL_STORE:
            raise StoreUnavailable(
                "The position store is unreachable, so your holdings could not "
                "be loaded. Nothing has been lost — please retry shortly.")
        with _local_lock:
            return list(_local_read().get(_key(email), {}).get("positions", []))

    try:
        resp = table.get_item(Key={"id": _key(email)}, ConsistentRead=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio: read failed for %s: %s", email, exc)
        raise StoreUnavailable(
            "The position store could not be read. Nothing has been lost — "
            "please retry shortly.") from exc

    item = resp.get("Item")
    if not item:
        return []
    return _decode(item)


def _decode(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Positions out of a stored item, tolerating an older or corrupt body.

    A body that will not parse returns ``[]`` rather than raising: the row exists,
    so this is not a store outage, and an unreadable portfolio the user can
    re-import beats a page that 503s forever.
    """
    raw = item.get("positions_json")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        log.warning("portfolio: unparseable positions for %s: %s",
                    item.get("id"), exc)
        return []
    if not isinstance(data, list):
        return []
    return [p for p in (normalise(x) for x in data if isinstance(x, dict))
            if p is not None]


def save(email: str, positions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace this user's positions wholesale. Returns what was stored.

    Whole-list replacement rather than a merge: it is what a CSV import means, and
    it makes the write idempotent, so a retry after a timeout cannot double a
    portfolio.
    """
    email = (email or "").strip().lower()
    if not email:
        raise StoreUnavailable("Not signed in")

    cleaned = normalise_all(positions)
    body = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))

    table = _get_table()
    if table is None:
        if not LOCAL_STORE:
            raise StoreUnavailable(
                "The position store is unreachable, so nothing was saved. "
                "Please retry shortly.")
        with _local_lock:
            data = _local_read()
            data[_key(email)] = {"email": email, "positions": cleaned,
                                 "updated_at": int(time.time())}
            _local_write(data)
        return cleaned

    from decimal import Decimal
    try:
        table.put_item(Item={
            "id": _key(email),
            "email": email,
            "positions_json": body,
            "count": len(cleaned),
            "updated_at": Decimal(str(int(time.time()))),
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio: save failed for %s: %s", email, exc)
        raise StoreUnavailable(
            "Your positions could not be saved. Please retry shortly.") from exc

    log.info("portfolio: saved %d positions for %s", len(cleaned), email)
    return cleaned


def add(email: str, position: dict[str, Any]) -> list[dict[str, Any]]:
    """Add or update one position, keeping the rest. Returns the new list."""
    pos = normalise(position)
    if pos is None:
        raise ValueError("A position needs a symbol and a quantity or value")
    current = [p for p in load(email) if p["symbol"] != pos["symbol"]]
    if len(current) >= MAX_POSITIONS:
        raise ValueError(f"A portfolio is limited to {MAX_POSITIONS} positions")
    return save(email, current + [pos])


def remove(email: str, symbol: str) -> list[dict[str, Any]]:
    """Delete one position by symbol. Returns the new list."""
    from ystocker.portfolio_csv import normalise_symbol

    target = normalise_symbol(symbol)
    return save(email, [p for p in load(email) if p["symbol"] != target])


def clear(email: str) -> None:
    """Delete every position for this user."""
    save(email, [])
