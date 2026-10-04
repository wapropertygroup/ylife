"""
ypay.billing
~~~~~~~~~~~~
Stripe's half of TradeAgents Pro: the subscription checkout, the billing portal,
recording what Stripe says, and keeping it in step.

The state lives in yStocker's ``subscriptions`` module (one ``sub#`` row per
subscriber in the credits table); this module is the only place it is written
from Stripe, and the only place a Stripe subscription call is made -- yStocker
holds no Stripe secret. Requests reach it as a token yStocker signed for a
signed-in reader (``subscriptions.handoff``), never as a bare address.

Three paths write a row, so none of them has to be relied on alone:

* **the checkout return** (``/subscribe/done``) retrieves the finished session
  and records it before the reader is sent back, so access does not wait for a
  webhook;
* **the webhook** records ``checkout.session.completed`` and the
  ``customer.subscription.*`` events as Stripe sends them;
* **the hourly reconciliation** re-reads any subscription at or near the end of
  its period, so a renewal, a failed card or a cancellation is caught even if
  its event never arrives.

Stripe objects are converted with ``to_dict()`` before anything reads them:
stripe 15's objects are not dicts, and ``.get`` on one raises -- the fault that
had silently broken the run-pack webhook.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from ystocker import subscriptions

log = logging.getLogger(__name__)

_price_cache: dict[str, tuple[float, str]] = {}
_PRICE_CACHE_SECONDS = 3600
_portal_config: Optional[str] = None
_lock = threading.Lock()
# Its own lock, not _lock: creating the configuration calls ensure_price, which
# takes _lock, and threading.Lock is not reentrant -- sharing one deadlocked the
# first portal session (caught by tests/test_billing.py before it shipped).
_portal_lock = threading.Lock()


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(obj)


def _missing(stripe: Any, exc: BaseException) -> bool:
    """Whether a Stripe error means "no such object"."""
    code = getattr(exc, "code", "") or ""
    return code == "resource_missing" or "No such" in str(exc)


# ── The product and its prices ──────────────────────────────────────────────

def ensure_product(stripe: Any) -> str:
    try:
        stripe.Product.retrieve(subscriptions.PRODUCT_ID)
    except Exception as exc:  # noqa: BLE001
        if not _missing(stripe, exc):
            raise
        stripe.Product.create(
            id=subscriptions.PRODUCT_ID, name=subscriptions.PRODUCT_NAME,
            description="10 analyses a day and every dashboard in full on trade-agents.com.")
        log.info("billing: created Stripe product %s", subscriptions.PRODUCT_ID)
    return subscriptions.PRODUCT_ID


def ensure_price(stripe: Any, plan_id: str) -> str:
    """The Stripe Price for a plan at today's configured amount.

    Found by ``lookup_key``. A price that no longer matches the configured amount
    or interval is replaced by a new one under the same key
    (``transfer_lookup_key``); subscribers on the old one keep it.
    """
    plan = subscriptions.plan(plan_id)
    if plan is None:
        raise ValueError(f"unknown plan {plan_id!r}")
    amount = int(round(plan["price"] * 100))
    cache_key = f"{plan_id}:{amount}"
    with _lock:
        hit = _price_cache.get(cache_key)
        if hit and time.time() - hit[0] < _PRICE_CACHE_SECONDS:
            return hit[1]
    found = stripe.Price.list(lookup_keys=[plan["lookup_key"]], active=True, limit=1).data
    price_id = ""
    if found:
        price = _as_dict(found[0])
        recurring = price.get("recurring") or {}
        if (price.get("unit_amount") == amount and price.get("currency") == "usd"
                and recurring.get("interval") == plan["interval"]):
            price_id = price["id"]
    if not price_id:
        ensure_product(stripe)
        created = stripe.Price.create(
            product=subscriptions.PRODUCT_ID, currency="usd", unit_amount=amount,
            recurring={"interval": plan["interval"]}, lookup_key=plan["lookup_key"],
            transfer_lookup_key=True, nickname=f"{subscriptions.PRODUCT_NAME} ({plan_id})")
        price_id = _as_dict(created)["id"]
        log.info("billing: created price %s for %s at $%.2f", price_id, plan_id, plan["price"])
    with _lock:
        _price_cache[cache_key] = (time.time(), price_id)
    return price_id


def ensure_portal_config(stripe: Any) -> str:
    """The billing-portal configuration sessions are opened with.

    The account had none (2026-10-04), and a portal session cannot be created
    without one. Cancelling takes effect at the end of the period paid for;
    switching between monthly and yearly is allowed, prorated.
    """
    global _portal_config
    if _portal_config:
        return _portal_config
    with _portal_lock:
        if _portal_config:
            return _portal_config
        for conf in stripe.billing_portal.Configuration.list(active=True, limit=20).data:
            conf = _as_dict(conf)
            if (conf.get("metadata") or {}).get("app") == "tradeagents":
                _portal_config = conf["id"]
                return _portal_config
        prices = [ensure_price(stripe, p) for p in ("month", "year")]
        created = stripe.billing_portal.Configuration.create(
            business_profile={"headline": "TradeAgents Pro"},
            features={
                "customer_update": {"enabled": True, "allowed_updates": ["email", "address"]},
                "invoice_history": {"enabled": True},
                "payment_method_update": {"enabled": True},
                "subscription_cancel": {
                    "enabled": True, "mode": "at_period_end",
                    "cancellation_reason": {"enabled": True, "options": [
                        "too_expensive", "unused", "missing_features", "other"]},
                },
                "subscription_update": {
                    "enabled": True, "default_allowed_updates": ["price"],
                    "proration_behavior": "create_prorations",
                    "products": [{"product": subscriptions.PRODUCT_ID, "prices": prices}],
                },
            },
            metadata={"app": "tradeagents"},
        )
        _portal_config = _as_dict(created)["id"]
        log.info("billing: created portal configuration %s", _portal_config)
        return _portal_config


# ── Checkout ────────────────────────────────────────────────────────────────

def checkout_params(*, plan_id: str, email: str, price_id: str, customer: str,
                    trial_days: int, origin: str, pay_base: str, lang: str = "") -> dict[str, Any]:
    """The Checkout Session for a subscription. Pure, so the rules are testable:

    * the trial only when this account has never had one, and a card always
      collected (Stripe charges when the trial ends unless it is cancelled);
    * promotion codes allowed, so a discount made in Stripe's dashboard can be
      redeemed without a deploy;
    * the address on the subscription's own metadata, which every later
      ``customer.subscription.*`` event carries back;
    * the existing Stripe customer when there is one, so one person stays one
      customer across a cancel and a resubscribe.
    """
    lang = lang if lang in ("en", "zh") else ""
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "allow_promotion_codes": True,
        "payment_method_collection": "always",
        "client_reference_id": email,
        "metadata": {"kind": "subscription", "email": email, "plan": plan_id,
                     "origin": origin, "lang": lang},
        "subscription_data": {"metadata": {"email": email, "plan": plan_id}},
        "success_url": f"{pay_base}/subscribe/done?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{origin}/subscribe?canceled=1" + (f"&lang={lang}" if lang else ""),
    }
    if trial_days > 0:
        params["subscription_data"]["trial_period_days"] = trial_days
    if customer:
        params["customer"] = customer
    else:
        params["customer_email"] = email
    if lang:
        params["locale"] = lang
    return params


def record_session(stripe: Any, session_id: str) -> Optional[dict[str, Any]]:
    """Record the subscription a finished Checkout Session created.

    Returns the saved row, or None when the session is not a completed
    subscription checkout of ours.
    """
    session = _as_dict(stripe.checkout.Session.retrieve(session_id, expand=["subscription"]))
    if session.get("mode") != "subscription" or session.get("status") != "complete":
        return None
    meta = session.get("metadata") or {}
    email = str(meta.get("email") or session.get("client_reference_id") or "").strip().lower()
    sub = session.get("subscription")
    if isinstance(sub, str):
        sub = _as_dict(stripe.Subscription.retrieve(sub))
    if "@" not in email or not isinstance(sub, dict):
        log.error("billing: session %s has no address or subscription", session_id)
        return None
    row = subscriptions.row_from_subscription(sub, email)
    subscriptions.save(row)
    return row


def record_event_subscription(sub: dict[str, Any], event_created: int) -> Optional[dict[str, Any]]:
    """Record a ``customer.subscription.*`` event's object, if it is one of ours."""
    email = str((sub.get("metadata") or {}).get("email") or "").strip().lower()
    if "@" not in email:
        email = subscriptions.email_for_customer(str(sub.get("customer") or ""))
    if "@" not in email:
        log.info("billing: subscription %s is not one of ours", sub.get("id"))
        return None
    row = subscriptions.row_from_subscription(sub, email, event_created=event_created)
    subscriptions.save(row)
    return row


# ── Reconciliation ──────────────────────────────────────────────────────────

RECONCILE_EVERY_SECONDS = 3600
#: Re-read a subscription this close to the end of its period, or past it.
RECONCILE_WINDOW_SECONDS = 2 * 3600


def due_for_reconcile(row: dict[str, Any], now: float) -> bool:
    """Whether a stored row should be checked against Stripe now. Pure."""
    if not row.get("subscription"):
        return False
    status = row.get("status") or ""
    if status in ("canceled", "incomplete_expired"):
        return False
    if status in ("past_due", "incomplete", "unpaid"):
        return True
    return float(row.get("period_end") or 0) < now + RECONCILE_WINDOW_SECONDS


def reconcile(stripe: Any, now: Optional[float] = None) -> int:
    """Re-read every subscription due a check; returns how many were updated."""
    stamp = time.time() if now is None else now
    updated = 0
    for row in subscriptions.all_rows():
        if not due_for_reconcile(row, stamp):
            continue
        try:
            sub = _as_dict(stripe.Subscription.retrieve(row["subscription"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("billing: could not re-read %s: %s", row.get("subscription"), exc)
            continue
        email = str(row.get("email") or "")
        if subscriptions.save(subscriptions.row_from_subscription(sub, email, event_created=stamp)):
            updated += 1
    return updated


def start_reconcile_thread(get_stripe) -> None:
    """Hourly, from yPay's master process. ``get_stripe`` returns the configured
    module or None, read each pass so a key added later is picked up."""
    def _loop() -> None:
        time.sleep(120)
        while True:
            try:
                stripe = get_stripe()
                if stripe is not None:
                    n = reconcile(stripe)
                    if n:
                        log.info("billing: reconciled %d subscription(s)", n)
            except Exception:  # noqa: BLE001 - the loop must outlive any one pass
                log.exception("billing: reconcile pass failed")
            time.sleep(RECONCILE_EVERY_SECONDS)

    threading.Thread(target=_loop, name="billing-reconcile", daemon=True).start()
