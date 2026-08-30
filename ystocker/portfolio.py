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


def _policy_key(email: str) -> str:
    """Partition key for the limits, on its own row.

    Deliberately **not** another attribute on the ``u#`` item. :func:`save` is a
    whole-item ``put_item`` — that is what makes a CSV import atomic — so a policy
    stored alongside the positions would be erased by the very next import, and
    silently: the write would succeed, the page would still render, and the limits
    would simply be gone. A separate row costs one extra read and cannot be
    clobbered by a position write at all.
    """
    return "p#" + (email or "").strip().lower()


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


# ---------------------------------------------------------------------------
# The policy — stated limits, which are not derivable from the holdings
# ---------------------------------------------------------------------------

#: Holding-type tags a user may attach to a symbol. An allowlist rather than free
#: text because these are read by ``exposure.PortfolioPolicy`` and shown in the UI;
#: an arbitrary string would render as an unlabelled chip and mean nothing to a
#: second reader.
HOLDING_TYPES = ("core", "tactical")

#: Ceiling on tagged symbols, so the policy row cannot grow unbounded. Well past
#: :data:`MAX_POSITIONS` is pointless — a tag on a symbol you do not hold is inert.
MAX_HOLDING_TYPES = MAX_POSITIONS

#: What a user who has never opened the form has. Every limit unset, because an
#: absent limit must not read as a satisfied one — ``exposure.assess`` emits no
#: check at all for ``None`` rather than defaulting to some house number the user
#: never agreed to.
DEFAULT_POLICY: dict[str, Any] = {
    "max_single_name_pct": None,
    "max_issuer_pct": None,
    "cash": 0.0,
    "holding_types": {},
}


def _pct_or_none(value: Any) -> Optional[float]:
    """A percentage in (0, 100], or None for "not stated".

    Zero is rejected rather than stored: a 0% limit means "hold none of anything",
    which is never what somebody typing into a form meant, and it would mark every
    position a breach. An out-of-range value is dropped to None for the same reason
    the whole module fails closed — a limit nobody can satisfy is worse than no
    limit, because the page would then be permanently red.
    """
    out = _num(value)
    if out is None or out <= 0 or out > 100:
        return None
    return round(out, 4)


def normalise_policy(raw: dict[str, Any]) -> dict[str, Any]:
    """One policy, cleaned for storage. Unknown keys are dropped.

    A whitelist on the same terms as :func:`normalise`, and it runs on read as
    well as write, so a field injected straight into DynamoDB cannot survive a
    round trip.
    """
    from ystocker.portfolio_csv import normalise_symbol

    if not isinstance(raw, dict):
        return normalise_policy({})

    out: dict[str, Any] = {
        "max_single_name_pct": _pct_or_none(raw.get("max_single_name_pct")),
        "max_issuer_pct": _pct_or_none(raw.get("max_issuer_pct")),
    }

    cash = _num(raw.get("cash"))
    out["cash"] = round(cash, 2) if cash is not None and cash >= 0 else 0.0

    types: dict[str, str] = {}
    for key, value in (raw.get("holding_types") or {}).items():
        if len(types) >= MAX_HOLDING_TYPES:
            break
        symbol = normalise_symbol(key)
        tag = str(value or "").strip().lower()
        if symbol and tag in HOLDING_TYPES:
            types[symbol] = tag
    out["holding_types"] = types
    return out


def load_policy(email: str) -> dict[str, Any]:
    """This user's stated limits, or :data:`DEFAULT_POLICY` if they set none.

    Fails closed like :func:`load`, and for a sharper reason: a policy that reads
    as absent does not just look empty, it turns every limit check off. A page
    quietly reporting no breaches because the store was unreachable is worse than
    one saying it could not check.
    """
    email = (email or "").strip().lower()
    if not email:
        return normalise_policy({})

    table = _get_table()
    if table is None:
        if not LOCAL_STORE:
            raise StoreUnavailable(
                "Your position limits could not be loaded, so nothing was "
                "checked against them. Please retry shortly.")
        with _local_lock:
            stored = _local_read().get(_policy_key(email), {}).get("policy")
        return normalise_policy(stored or {})

    try:
        resp = table.get_item(Key={"id": _policy_key(email)}, ConsistentRead=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio: policy read failed for %s: %s", email, exc)
        raise StoreUnavailable(
            "Your position limits could not be read. Please retry shortly."
        ) from exc

    item = resp.get("Item")
    if not item:
        return normalise_policy({})
    raw = item.get("policy_json")
    if not raw:
        return normalise_policy({})
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        log.warning("portfolio: unparseable policy for %s: %s", item.get("id"), exc)
        return normalise_policy({})
    return normalise_policy(data if isinstance(data, dict) else {})


def save_policy(email: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Replace this user's limits wholesale. Returns what was stored."""
    email = (email or "").strip().lower()
    if not email:
        raise StoreUnavailable("Not signed in")

    cleaned = normalise_policy(policy)
    body = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))

    table = _get_table()
    if table is None:
        if not LOCAL_STORE:
            raise StoreUnavailable(
                "Your limits could not be saved. Please retry shortly.")
        with _local_lock:
            data = _local_read()
            data[_policy_key(email)] = {"email": email, "policy": cleaned,
                                        "updated_at": int(time.time())}
            _local_write(data)
        return cleaned

    from decimal import Decimal
    try:
        table.put_item(Item={
            "id": _policy_key(email),
            "email": email,
            "policy_json": body,
            "updated_at": Decimal(str(int(time.time()))),
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio: policy save failed for %s: %s", email, exc)
        raise StoreUnavailable(
            "Your limits could not be saved. Please retry shortly.") from exc

    log.info("portfolio: saved policy for %s", email)
    return cleaned
