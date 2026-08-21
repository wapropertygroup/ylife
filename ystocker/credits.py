"""
ystocker.credits
~~~~~~~~~~~~~~~~
Paid run credits for the trading agents.

Every signed-in user gets a small free allowance per day (``quota.limit_default``).
Past that they buy credits, which are spent one per analysis and carry over
between days -- unlike the free allowance, which resets.

Why DynamoDB rather than the JSON counter ``quota`` uses
-------------------------------------------------------
The daily counter is disposable: if it is lost, everyone gets their free runs
again, which costs a little API spend and nothing else. A credit balance is
something a person paid for. It has to survive the instance being replaced, the
same reason the observed valuation series lives in DynamoDB -- and it is written
by two different apps, since the Stripe webhook that grants credits is served by
yPay on another port while the spend happens in yStocker.

Correctness notes
-----------------
Spending uses a conditional atomic update, not read-modify-write. Two gunicorn
workers, or a user with two tabs, can submit simultaneously; ``ADD balance :neg``
guarded by ``balance >= 1`` either succeeds once or raises, so a balance of one
cannot fund two runs. This is why credits are not kept in the flock'd JSON file:
that lock is per box, and correctness here is worth a network round trip.

Granting is idempotent on the Stripe session id. Stripe retries a webhook until
it gets a 2xx, and it can deliver the same event more than once, so a replay must
not top the balance up twice. The session id is claimed with a conditional put
before the balance moves; if the balance update then fails, the claim is released
so Stripe's own retry can complete the grant rather than it being lost.

One table, two kinds of row, distinguished by key prefix:

    u#<email>              balance, updated_at, lifetime_purchased
    s#<stripe_session_id>  email, credits, amount_usd, created_at
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("AGENTS_CREDITS_TABLE", "ystocker-agent-credits")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Where a user is sent to buy more. Configurable so a staging box can point
# somewhere that is not the live payment page.
PAY_URL = os.environ.get("AGENTS_PAY_URL", "https://pay.li-family.us")

_table = None
_table_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------
# id -> (price in USD, credits). The number of credits is ALWAYS taken from here
# and never from the request, so a crafted checkout cannot ask for 10 000 runs at
# the $5 price. yPay looks the pack up by id and Stripe is told the price from
# this same table, so the two cannot disagree.
#
# The ladder gets cheaper per run at every step. $100 is 130 rather than 120
# because at 120 it would cost exactly the same per run as two $50 packs, making
# it a price point with no reason to exist.
PACKS: dict[str, dict[str, Any]] = {
    "runs5":   {"credits": 5,   "price": 5.0,   "label": "5 runs"},
    "runs28":  {"credits": 28,  "price": 25.0,  "label": "28 runs"},
    "runs60":  {"credits": 60,  "price": 50.0,  "label": "60 runs"},
    "runs130": {"credits": 130, "price": 100.0, "label": "130 runs"},
}


def pack(pack_id: str) -> Optional[dict[str, Any]]:
    """A pack by id, or None. Never trust a caller's credit count."""
    return PACKS.get((pack_id or "").strip())


def selling_enabled() -> tuple[bool, str]:
    """Whether it is safe to offer packs for sale, and why not if it is not.

    Runs are granted by yPay's Stripe webhook, and that handler refuses to credit
    an event it could not signature-verify. With no ``STRIPE_WEBHOOK_SECRET`` a
    purchase would therefore charge the card and grant nothing -- so the packs are
    withheld rather than sold. Refusing a sale is recoverable; taking money for
    nothing is not.
    """
    if os.environ.get("AGENTS_SELLING_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return False, "selling disabled by configuration"
    # Two ways to know selling works, because this module is imported by both
    # apps and only one of them holds the Stripe config. yPay has the real
    # variables; yStocker is told the fact at startup (see _load_secrets_from_ssm)
    # so it can show a price without holding a signing key.
    if os.environ.get("AGENTS_SELLING_OK", "").strip() == "1":
        return True, ""
    if not os.environ.get("STRIPE_SECRET_KEY", "").strip():
        return False, "Stripe is not configured"
    if not os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip():
        return False, ("no STRIPE_WEBHOOK_SECRET: a payment could not be "
                       "credited, so packs are not offered")
    return True, ""


def packs_public() -> list[dict[str, Any]]:
    """The packs as the page shows them, cheapest first."""
    out = []
    for pid, p in PACKS.items():
        out.append({
            "id": pid,
            "credits": p["credits"],
            "price": p["price"],
            "label": p["label"],
            # Shown so the ladder is legible rather than arithmetic homework.
            "per_run": round(p["price"] / p["credits"], 3),
        })
    return sorted(out, key=lambda p: p["price"])


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def _get_table():
    """The credits table, or None when DynamoDB is unreachable.

    None is not an error here: the caller treats an unavailable ledger as "no
    credits", which fails closed. Granting free runs because a network call
    failed would be the wrong direction; refusing a paid run is recoverable by
    retrying, and the balance is still on record.
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
            log.info("credits: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - no table, no credits
            log.warning("credits: DynamoDB unavailable (%s); balances are 0", exc)
            return None
    return _table


def _key(email: str) -> str:
    return "u#" + (email or "").strip().lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def balance(email: Optional[str]) -> int:
    """Credits available to this address. 0 when unknown or unreachable."""
    if not (email or "").strip():
        return 0
    table = _get_table()
    if table is None:
        return 0
    try:
        got = table.get_item(Key={"id": _key(email)}, ConsistentRead=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("credits: balance read failed for %s: %s", email, exc)
        return 0
    item = got.get("Item") or {}
    try:
        return max(0, int(item.get("balance", 0)))
    except (TypeError, ValueError):
        return 0


def summary(email: Optional[str]) -> dict[str, Any]:
    """Balance plus what the page needs to offer a top-up."""
    return {
        "balance": balance(email),
        "pay_url": PAY_URL,
        "packs": packs_public(),
    }


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------

def spend(email: Optional[str], n: int = 1) -> bool:
    """Take ``n`` credits, all or nothing. False when there are not enough.

    Conditional so this is safe against itself: the balance check and the
    decrement are one DynamoDB operation, so two concurrent submissions cannot
    both pass a check for the last credit.
    """
    if not (email or "").strip() or n <= 0:
        return False
    table = _get_table()
    if table is None:
        return False
    try:
        table.update_item(
            Key={"id": _key(email)},
            UpdateExpression="ADD balance :neg SET updated_at = :t",
            ConditionExpression="attribute_exists(id) AND balance >= :need",
            ExpressionAttributeValues={":neg": -n, ":need": n, ":t": _now()},
        )
        log.info("credits: spent %d for %s", n, email)
        return True
    except Exception as exc:  # noqa: BLE001
        # ConditionalCheckFailedException is the ordinary "no credits" answer and
        # is not worth a warning; anything else is.
        if type(exc).__name__ == "ConditionalCheckFailedException":
            return False
        log.warning("credits: spend failed for %s: %s", email, exc)
        return False


def refund(email: Optional[str], n: int = 1) -> None:
    """Give credits back, for a paid run that never reached the LLM."""
    if not (email or "").strip() or n <= 0:
        return
    table = _get_table()
    if table is None:
        log.error("credits: cannot refund %d to %s -- ledger unreachable", n, email)
        return
    try:
        table.update_item(
            Key={"id": _key(email)},
            UpdateExpression="ADD balance :n SET updated_at = :t",
            ExpressionAttributeValues={":n": n, ":t": _now()},
        )
        log.info("credits: refunded %d to %s", n, email)
    except Exception as exc:  # noqa: BLE001
        log.error("credits: refund of %d to %s FAILED: %s", n, email, exc)


# ---------------------------------------------------------------------------
# Granting (called by yPay's Stripe webhook)
# ---------------------------------------------------------------------------

class GrantResult:
    """Outcome of a grant, so the webhook can log precisely what happened."""

    def __init__(self, ok: bool, credited: int, reason: str = "") -> None:
        self.ok = ok
        self.credited = credited
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - logging aid
        return f"GrantResult(ok={self.ok}, credited={self.credited}, reason={self.reason!r})"


def grant(email: str, pack_id: str, session_id: str,
          amount_usd: Optional[float] = None) -> GrantResult:
    """Credit a completed purchase. Safe to call more than once per session id.

    ``amount_usd`` is what Stripe says was actually paid. When given it must
    match the pack's price, so a checkout built with a tampered price cannot buy
    a large pack for a small amount. Credits come from :data:`PACKS`, never from
    the caller.
    """
    email = (email or "").strip().lower()
    session_id = (session_id or "").strip()
    p = pack(pack_id)
    if not email or "@" not in email:
        return GrantResult(False, 0, "no usable email on the payment")
    if not session_id:
        return GrantResult(False, 0, "no session id")
    if not p:
        return GrantResult(False, 0, f"unknown pack {pack_id!r}")
    if amount_usd is not None and abs(float(amount_usd) - p["price"]) > 0.01:
        return GrantResult(False, 0,
                           f"paid ${amount_usd:.2f} but {pack_id} costs ${p['price']:.2f}")

    table = _get_table()
    if table is None:
        # Loud: someone has paid and the credits are not applied. Stripe will
        # retry, and the payment is recorded on Stripe's side either way.
        log.error("credits: CANNOT GRANT %s to %s -- ledger unreachable, session %s",
                  pack_id, email, session_id)
        return GrantResult(False, 0, "ledger unreachable")

    credits = int(p["credits"])

    # Claim the session first. Claiming before crediting means a duplicate
    # delivery can at worst do nothing, rather than credit twice -- the safe
    # direction when the mistake costs money.
    try:
        table.put_item(
            Item={
                "id": "s#" + session_id,
                "email": email,
                "pack": pack_id,
                "credits": credits,
                "amount_usd": str(amount_usd if amount_usd is not None else p["price"]),
                "created_at": _now(),
            },
            ConditionExpression="attribute_not_exists(id)",
        )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ConditionalCheckFailedException":
            log.info("credits: session %s already granted; ignoring replay", session_id)
            return GrantResult(True, 0, "already granted")
        log.error("credits: could not claim session %s: %s", session_id, exc)
        return GrantResult(False, 0, "claim failed")

    try:
        table.update_item(
            Key={"id": _key(email)},
            UpdateExpression=("ADD balance :n, lifetime_purchased :n "
                              "SET updated_at = :t, email = :e"),
            ExpressionAttributeValues={":n": credits, ":t": _now(), ":e": email},
        )
    except Exception as exc:  # noqa: BLE001
        # Release the claim so Stripe's retry can apply the grant instead of it
        # being silently swallowed by our own idempotency guard.
        log.error("credits: crediting %s failed (%s); releasing claim on %s",
                  email, exc, session_id)
        try:
            table.delete_item(Key={"id": "s#" + session_id})
        except Exception as cleanup:  # noqa: BLE001
            log.error("credits: could not release claim %s: %s -- %d credits owed "
                      "to %s must be applied by hand", session_id, cleanup,
                      credits, email)
        return GrantResult(False, 0, "credit failed")

    log.info("credits: granted %d to %s (pack %s, session %s)",
             credits, email, pack_id, session_id)
    return GrantResult(True, credits, "granted")
