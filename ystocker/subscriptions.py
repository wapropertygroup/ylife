"""
ystocker.subscriptions
~~~~~~~~~~~~~~~~~~~~~~
TradeAgents Pro: a monthly or yearly subscription, with a 7-day free trial.

What it buys (asked for 2026-10-04): 10 analyses a day instead of the free
allowance, and every dashboard in full. On trade-agents.com the reading wall now
covers anyone without an active trial or subscription, signed in or not.

Who does what
-------------
Stripe is called from yPay only. yStocker deliberately holds no Stripe secret
(see ``_load_secrets_from_ssm``: "only the fact is imported, never the
secrets"), so this module owns the *state*: one row per subscriber in the
credits table, which both apps already reach. yPay writes it -- on Stripe's
return from checkout, from the webhook, and from an hourly reconciliation --
and yStocker reads it wherever entitlement matters.

The signed-in actions, starting a checkout and opening the billing portal,
cross from yStocker's session to yPay as a short-lived token signed with
yStocker's own secret key (:func:`handoff`), which yPay reads from SSM. The pay
hosts share no session with yStocker, and an address in a query string -- how
a run pack is bought -- would let anyone open anyone's billing portal.

Rows, in the credits table under its ``id`` key::

    sub#<email>    status, plan, customer, subscription, period_end,
                   trial_end, cancel_at_period_end, trial_used,
                   event_created, updated_at
    cus#<cus_id>   email -- for an event that names only the customer

Entitlement is decided from the row alone (:func:`entitled`), never by asking
Stripe on a page view:

* ``trialing`` / ``active`` -- until ``period_end``, plus a day of slack for a
  renewal Stripe has not reported yet;
* ``past_due`` -- Stripe is retrying the card, so access holds for a week past
  ``period_end`` rather than vanishing on the first failed charge;
* anything else (``canceled``, ``unpaid``, ``incomplete``, ``paused``...) -- none.

A row is only ever replaced by a newer account of the same subscription:
``event_created`` orders them, so a webhook Stripe delivers late cannot undo
what the checkout return or the reconciliation already recorded.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        log.warning("subscriptions: %s=%r is not a number; using %s", name, raw, default)
        return default
    return value if value > 0 else default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        log.warning("subscriptions: %s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value >= 0 else default


# ── The offer ───────────────────────────────────────────────────────────────

#: The Stripe Product every plan's Price belongs to. A fixed id, so a checkout
#: never mints a product of its own.
PRODUCT_ID = "tradeagents_pro"
PRODUCT_NAME = "TradeAgents Pro"

#: Plan -> price and billing interval. The yearly plan is ten months' price, two
#: months free. Prices live here and in the environment only; Stripe is told
#: them through a Price found by ``lookup_key``, so changing one here (or with
#: SUB_PRICE_MONTH / SUB_PRICE_YEAR) makes yPay create a new Price under the same
#: key on the next checkout. Existing subscribers stay on the price they bought.
PLANS: dict[str, dict[str, Any]] = {
    "month": {"interval": "month", "price": _float_env("SUB_PRICE_MONTH", 29.0),
              "lookup_key": "tradeagents_pro_month"},
    "year":  {"interval": "year",  "price": _float_env("SUB_PRICE_YEAR", 290.0),
              "lookup_key": "tradeagents_pro_year"},
}

#: Days of free trial. Stripe collects a card at checkout and charges at the end.
TRIAL_DAYS = _int_env("SUB_TRIAL_DAYS", 7)

#: Analyses a day for a subscriber or trial (the free allowance is quota's).
RUNS_PER_DAY = _int_env("AGENTS_SUB_DAILY_LIMIT", 10)

ACTIVE_STATUSES = frozenset({"trialing", "active"})
RENEWAL_SLACK_SECONDS = 24 * 3600
PAST_DUE_GRACE_SECONDS = 7 * 24 * 3600

#: Where a handoff may send the reader back to: this app's own hosts.
ORIGINS = frozenset({
    "https://trade-agents.com", "https://www.trade-agents.com",
    "https://stock.li-family.us",
    "http://localhost:5000", "http://127.0.0.1:5000",
})
DEFAULT_ORIGIN = "https://trade-agents.com"


def plan(plan_id: str) -> Optional[dict[str, Any]]:
    """A plan by id, or None. A caller's price is never trusted."""
    return PLANS.get((plan_id or "").strip().lower())


def yearly_saving_pct() -> int:
    """How much cheaper a year is than twelve months, in whole percent."""
    month, year = PLANS["month"]["price"], PLANS["year"]["price"]
    return max(0, round((1 - year / (12 * month)) * 100)) if month else 0


def months_free() -> int:
    """Whole months a year saves (2 at $29/$290), or 0 when the prices do not
    come out to a whole number -- "1.7 months free" is not a thing to print."""
    month, year = PLANS["month"]["price"], PLANS["year"]["price"]
    if not month:
        return 0
    free = 12 - year / month
    return int(round(free)) if free >= 1 and abs(free - round(free)) < 0.01 else 0


def offer() -> dict[str, Any]:
    """The plans as the pages show them."""
    month, year = PLANS["month"]["price"], PLANS["year"]["price"]
    return {
        "month": month,
        "year": year,
        "year_per_month": round(year / 12, 2),
        "saving_pct": yearly_saving_pct(),
        "months_free": months_free(),
        "trial_days": TRIAL_DAYS,
        "runs_per_day": RUNS_PER_DAY,
    }


def enabled() -> tuple[bool, str]:
    """Whether Pro is offered, and why not if it is not.

    ``SUBSCRIPTIONS=0`` is the kill switch. The handoff needs
    YSTOCKER_SECRET_KEY, and a checkout needs Stripe configured on the pay side
    -- the same fact ``credits.selling_enabled`` reads, since yStocker is told
    it rather than holding the keys.
    """
    if os.environ.get("SUBSCRIPTIONS", "").strip().lower() in ("0", "false", "no", "off"):
        return False, "disabled by configuration"
    if not os.environ.get("YSTOCKER_SECRET_KEY", "").strip():
        return False, "YSTOCKER_SECRET_KEY is not set"
    from ystocker import credits

    return credits.selling_enabled()


# ── Entitlement (pure) ──────────────────────────────────────────────────────

def entitled(row: Optional[dict[str, Any]], now: Optional[float] = None) -> bool:
    """Whether a stored subscription grants Pro right now. See the module docstring."""
    if not row:
        return False
    stamp = time.time() if now is None else now
    status = str(row.get("status") or "")
    end = float(row.get("period_end") or 0)
    if status in ACTIVE_STATUSES:
        return stamp < end + RENEWAL_SLACK_SECONDS
    if status == "past_due":
        return stamp < end + PAST_DUE_GRACE_SECONDS
    return False


def _sub_field(sub: dict[str, Any], name: str) -> Any:
    """A field of a Stripe subscription, wherever this API version keeps it.

    Newer API versions moved ``current_period_end`` from the subscription onto
    its items: the library (stripe 15) requests the new shape, while a webhook
    event arrives in the account's default version, which is older. Both are read.
    """
    value = sub.get(name)
    if value is None:
        items = ((sub.get("items") or {}).get("data")) or []
        if items and isinstance(items[0], dict):
            value = items[0].get(name)
    return value


def _id(value: Any) -> str:
    """A Stripe reference that may arrive as an id or as the expanded object."""
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def row_from_subscription(sub: dict[str, Any], email: str,
                          event_created: Optional[float] = None) -> dict[str, Any]:
    """What to store for a Stripe subscription object (as a plain dict)."""
    items = ((sub.get("items") or {}).get("data")) or []
    price = (items[0].get("price") or {}) if items and isinstance(items[0], dict) else {}
    interval = ((price.get("recurring") or {}).get("interval")) or ""
    meta = sub.get("metadata") or {}
    plan_id = {"month": "month", "year": "year"}.get(interval) or str(meta.get("plan") or "")
    trial_end = sub.get("trial_end") or 0
    return {
        "email": (email or "").strip().lower(),
        "status": str(sub.get("status") or ""),
        "plan": plan_id,
        "customer": _id(sub.get("customer")),
        "subscription": str(sub.get("id") or ""),
        "period_end": int(_sub_field(sub, "current_period_end") or 0),
        "trial_end": int(trial_end or 0),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "trial_used": bool(trial_end),
        "event_created": int(event_created if event_created is not None else time.time()),
    }


# ── Storage ─────────────────────────────────────────────────────────────────

CACHE_SECONDS = 60
#: How long a row read before DynamoDB became unreachable is still trusted:
#: a paying reader should not hit the wall because of a network blip.
STALE_OK_SECONDS = 3600

_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


class StoreUnavailable(RuntimeError):
    """The subscription table could not be read."""


def _table():
    from ystocker import credits

    return credits._get_table()


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    """A DynamoDB item with its Decimals turned back into numbers."""
    out: dict[str, Any] = {}
    for key, value in item.items():
        try:
            from decimal import Decimal

            if isinstance(value, Decimal):
                value = int(value) if value == value.to_integral_value() else float(value)
        except Exception:  # noqa: BLE001
            pass
        out[key] = value
    return out


def load(email: Optional[str]) -> Optional[dict[str, Any]]:
    """The stored row, read consistently, or None if there is none. Raises
    :class:`StoreUnavailable` instead of guessing: checkout decides from this
    whether to offer the trial and whether the reader is already subscribed, and
    an unreadable row read as "none" would give a second trial or a second
    subscription."""
    key = _norm(email)
    if not key:
        return None
    table = _table()
    if table is None:
        raise StoreUnavailable("no credits table")
    try:
        got = table.get_item(Key={"id": "sub#" + key}, ConsistentRead=True)
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable(str(exc)) from exc
    item = got.get("Item")
    row = _clean(item) if item else None
    with _cache_lock:
        _cache[key] = (time.time(), row)
    return row


def status(email: Optional[str], fresh: bool = False) -> Optional[dict[str, Any]]:
    """The stored subscription for an address, or None if there is none.

    Cached per process for :data:`CACHE_SECONDS`: every dashboard page asks, to
    decide the wall. ``fresh`` re-reads, for the page a reader lands on straight
    from checkout. When the table cannot be read, a row read within
    :data:`STALE_OK_SECONDS` is still served.
    """
    key = _norm(email)
    if not key:
        return None
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and not fresh and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    table = _table()
    if table is None:
        return hit[1] if hit and now - hit[0] < STALE_OK_SECONDS else None
    try:
        got = table.get_item(Key={"id": "sub#" + key}, ConsistentRead=fresh)
    except Exception as exc:  # noqa: BLE001
        log.warning("subscriptions: could not read %s: %s", key, exc)
        return hit[1] if hit and now - hit[0] < STALE_OK_SECONDS else None
    item = got.get("Item")
    row = _clean(item) if item else None
    with _cache_lock:
        _cache[key] = (now, row)
    return row


def is_entitled(email: Optional[str], fresh: bool = False) -> bool:
    return entitled(status(email, fresh=fresh))


def has_full_access(email: Optional[str]) -> bool:
    """Every dashboard in full: a VIP, or an active trial or subscription."""
    if not _norm(email):
        return False
    from ystocker import quota

    return quota.is_vip(email) or is_entitled(email)


def full_access_for_page(email: Optional[str]) -> bool:
    """For trade-agents.com's reading wall: whether this reader sees every
    dashboard in full.

    Signed out, never. Signed in with Pro switched off, always -- that is how
    the wall worked before Pro existed, and a wall with nothing to buy behind it
    would be a dead end. Otherwise a VIP, a trial or a subscription. Never
    raises: a page must render whatever the table is doing.
    """
    if not _norm(email):
        return False
    try:
        if not enabled()[0]:
            return True
        return has_full_access(email)
    except Exception as exc:  # noqa: BLE001
        log.warning("subscriptions: access check failed for %s: %s", email, exc)
        return False


def trial_available(email: Optional[str]) -> bool:
    """One trial per account. Fails closed: an unreadable row means no trial
    offered, never a second one."""
    if TRIAL_DAYS <= 0:
        return False
    if not _norm(email):
        return True
    try:
        return not (load(email) or {}).get("trial_used")
    except StoreUnavailable:
        return False


def customer_for(email: Optional[str]) -> str:
    """The reader's Stripe customer id, or "" (also when unreadable)."""
    try:
        return str((load(email) or {}).get("customer") or "")
    except StoreUnavailable:
        return ""


def email_for_customer(customer_id: str) -> str:
    """The address a Stripe customer belongs to, from the ``cus#`` row."""
    table = _table()
    if table is None or not customer_id:
        return ""
    try:
        got = table.get_item(Key={"id": "cus#" + customer_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("subscriptions: could not map customer %s: %s", customer_id, exc)
        return ""
    return str((got.get("Item") or {}).get("email") or "")


def save(row: dict[str, Any]) -> bool:
    """Store a subscription row, unless a newer account of it is already stored.

    ``trial_used`` is only ever set, never cleared: a trial taken stays taken.
    Returns whether the row was written.
    """
    key = _norm(row.get("email"))
    table = _table()
    if not key or table is None:
        log.error("subscriptions: cannot save %s (table %s)", key or "?",
                  "missing" if table is None else "ok")
        return False
    names = {"#s": "status"}
    values: dict[str, Any] = {
        ":s": row.get("status") or "",
        ":p": row.get("plan") or "",
        ":cu": row.get("customer") or "",
        ":sub": row.get("subscription") or "",
        ":pe": int(row.get("period_end") or 0),
        ":te": int(row.get("trial_end") or 0),
        ":cap": bool(row.get("cancel_at_period_end")),
        ":c": int(row.get("event_created") or 0),
        ":u": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    sets = ["#s = :s", "plan = :p", "customer = :cu", "subscription = :sub",
            "period_end = :pe", "trial_end = :te", "cancel_at_period_end = :cap",
            "event_created = :c", "updated_at = :u"]
    if row.get("trial_used"):
        sets.append("trial_used = :t")
        values[":t"] = True
    try:
        table.update_item(
            Key={"id": "sub#" + key},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_not_exists(event_created) OR event_created <= :c",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:  # noqa: BLE001
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            log.info("subscriptions: kept the newer row for %s", key)
            return False
        log.error("subscriptions: could not save %s: %s", key, exc)
        return False
    if row.get("customer"):
        try:
            table.put_item(Item={"id": "cus#" + row["customer"], "email": key})
        except Exception as exc:  # noqa: BLE001 - the map is a convenience
            log.warning("subscriptions: could not map %s: %s", row["customer"], exc)
    with _cache_lock:
        _cache.pop(key, None)
    log.info("subscriptions: %s is %s (%s, until %s)", key, row.get("status"),
             row.get("plan"), row.get("period_end"))
    return True


def all_rows() -> list[dict[str, Any]]:
    """Every stored subscription, for yPay's reconciliation. A Scan, because the
    table is small and this runs hourly."""
    table = _table()
    if table is None:
        return []
    out: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    try:
        from boto3.dynamodb.conditions import Attr

        kwargs["FilterExpression"] = Attr("id").begins_with("sub#")
        while True:
            page = table.scan(**kwargs)
            out.extend(_clean(i) for i in page.get("Items") or [])
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        log.warning("subscriptions: scan failed: %s", exc)
    for row in out:
        row.setdefault("email", str(row.get("id", ""))[4:])
    return out


# ── What a page shows ───────────────────────────────────────────────────────

def summary(email: Optional[str], fresh: bool = False) -> dict[str, Any]:
    """The reader's subscription for a page: status, dates, and what to offer."""
    row = status(email, fresh=fresh) if _norm(email) else None
    active = entitled(row)
    out = {
        "active": active,
        "status": (row or {}).get("status") or "",
        "plan": (row or {}).get("plan") or "",
        "period_end": int((row or {}).get("period_end") or 0),
        "trial_end": int((row or {}).get("trial_end") or 0),
        "cancel_at_period_end": bool((row or {}).get("cancel_at_period_end")),
        "trialing": active and (row or {}).get("status") == "trialing",
        "trial_available": bool(TRIAL_DAYS) and not (row or {}).get("trial_used"),
        "has_customer": bool((row or {}).get("customer")),
    }
    out.update(offer())
    return out


# ── The handoff to yPay ─────────────────────────────────────────────────────

HANDOFF_MAX_AGE = 15 * 60
_SALT = "tradeagents-subscription-handoff-v1"


class HandoffUnavailable(RuntimeError):
    """No signing secret is configured, so no handoff can be made or trusted."""


def _serializer():
    """Signs with YSTOCKER_SECRET_KEY and nothing else. yStocker's session falls
    back to a random per-process key when that is unset, which is safe for a
    session; a handoff must not fall back at all. ypay verifies it in another
    process, so only the shared key can work, and a constant would be in this
    repository: a token forged with it opens anyone's billing portal."""
    from itsdangerous import URLSafeTimedSerializer

    secret = os.environ.get("YSTOCKER_SECRET_KEY", "").strip()
    if not secret:
        raise HandoffUnavailable("YSTOCKER_SECRET_KEY is not set")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def origin_ok(origin: str) -> str:
    """The origin if it is one of this app's own, else the default."""
    origin = (origin or "").strip().rstrip("/")
    return origin if origin in ORIGINS else DEFAULT_ORIGIN


def handoff(email: str, purpose: str, origin: str = "", **extra: Any) -> str:
    """A token yPay accepts as "this signed-in reader asked for this"."""
    payload = {"e": _norm(email), "p": purpose, "o": origin_ok(origin)}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return _serializer().dumps(payload)


def read_handoff(token: str, purpose: str, max_age: int = HANDOFF_MAX_AGE) -> Optional[dict[str, Any]]:
    """The token's payload, or None if it is forged, stale or for another purpose."""
    from itsdangerous import BadSignature

    try:
        payload = _serializer().loads(token or "", max_age=max_age)
    except (BadSignature, HandoffUnavailable):
        return None
    if not isinstance(payload, dict) or payload.get("p") != purpose or "@" not in str(payload.get("e") or ""):
        return None
    payload["o"] = origin_ok(payload.get("o", ""))
    return payload
