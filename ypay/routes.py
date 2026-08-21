"""
ypay.routes
~~~~~~~~~~~
URL routes for the yPay payment app.
Integrates with Stripe Checkout for secure payment processing.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for, session,
)

bp = Blueprint("pay", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DynamoDB helpers (for storing payment items + history)
# ---------------------------------------------------------------------------
_ITEMS_TABLE_NAME = "ypay-items"
_PAYMENTS_TABLE_NAME = "ypay-payments"
_items_table = None
_payments_table = None
_dynamo_unavail_until = 0.0
_DYNAMO_BACKOFF = 300


def _get_dynamodb():
    global _dynamo_unavail_until
    if time.time() < _dynamo_unavail_until:
        return None
    try:
        import boto3
        from botocore.config import Config
        return boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        )
    except Exception as exc:
        log.warning("DynamoDB unavailable: %s", exc)
        _dynamo_unavail_until = time.time() + _DYNAMO_BACKOFF
        return None


def _get_items_table():
    global _items_table
    if _items_table is not None:
        return _items_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_ITEMS_TABLE_NAME)
        table.load()
        _items_table = table
        return _items_table
    except Exception:
        return None


def _get_payments_table():
    global _payments_table
    if _payments_table is not None:
        return _payments_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_PAYMENTS_TABLE_NAME)
        table.load()
        _payments_table = table
        return _payments_table
    except Exception:
        return None


def _decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Default payment items (used when DynamoDB is unavailable)
# ---------------------------------------------------------------------------
DEFAULT_ITEMS = [
    {
        "id": "coffee",
        "name": "Buy Me a Coffee",
        "description": "A small token of appreciation — thank you! ☕",
        "price": 5.00,
        "emoji": "☕",
        "category": "donation",
    },
    {
        "id": "supporter",
        "name": "Supporter",
        "description": "A generous contribution to keep the apps growing.",
        "price": 25.00,
        "emoji": "⭐",
        "category": "donation",
    },
    {
        "id": "champion",
        "name": "Champion",
        "description": "Top-tier support for all Li Family apps.",
        "price": 50.00,
        "emoji": "🏆",
        "category": "donation",
    },
    {
        "id": "hosting",
        "name": "Monthly Hosting",
        "description": "Covers one full month of EC2 + domain costs for all apps.",
        "price": 100.00,
        "emoji": "\U0001f5a5",
        "category": "hosting",
    },
    {
        "id": "custom",
        "name": "Custom Amount",
        "description": "Choose your own amount to contribute.",
        "price": 0,
        "emoji": "\U0001f49d",
        "category": "custom",
    },
]


def _get_stripe():
    """Return configured stripe module, or None if not available."""
    secret = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret:
        return None
    try:
        import stripe
        stripe.api_key = secret
        return stripe
    except ImportError:
        log.warning("stripe package not installed")
        return None


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    """Payment landing page with available items.

    ``?email=`` and ``?next=`` arrive when yStocker sends a signed-in user here
    to buy analysis runs: the address says which balance to credit, and next is
    where to return afterwards. The address is not proof of anything on its own,
    and it does not need to be -- it only decides who benefits from a payment
    somebody actually makes.
    """
    stripe_pk = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    email = (request.args.get("email") or "").strip()[:200]
    nxt = (request.args.get("next") or "").strip()[:300]
    return render_template("index.html",
                           items=DEFAULT_ITEMS,
                           agent_packs=_agent_packs(),
                           buyer_email=email if "@" in email else "",
                           return_to=nxt if nxt.startswith("https://") else "",
                           stripe_pk=stripe_pk,
                           stripe_configured=bool(stripe_pk))


@bp.route("/success")
def success():
    """Payment success page."""
    session_id = request.args.get("session_id", "")
    return render_template("success.html", session_id=session_id)


@bp.route("/cancel")
def cancel():
    """Payment cancelled page."""
    return render_template("cancel.html")


# ---------------------------------------------------------------------------
# yStocker agent run packs
# ---------------------------------------------------------------------------
# The pack table lives in ystocker.credits so that the app that *sells* runs and
# the app that *spends* them cannot drift apart on how many a pack contains. Both
# apps run from the same checkout on the same box, so this is a plain import.
def _agent_packs():
    """The run packs, or [] if yStocker is not importable from here."""
    try:
        from ystocker import credits

        ok, why = credits.selling_enabled()
        if not ok:
            log.warning("ypay: not offering run packs — %s", why)
            return []
        return credits.packs_public()
    except Exception as exc:  # noqa: BLE001 - yPay still works without them
        log.warning("ypay: agent packs unavailable: %s", exc)
        return []


def _agent_pack_item(pack_id: str):
    """A pack as a checkout line item, or None when the id is unknown.

    The price comes from the pack table, never from the request, so a caller
    cannot buy the 130-run pack for $5.
    """
    try:
        from ystocker import credits

        p = credits.pack(pack_id)
    except Exception:  # noqa: BLE001
        return None
    if not p:
        return None
    return {
        "id": pack_id,
        "name": f"yStocker — {p['label']}",
        "description": (f"{p['credits']} Trading Agents analysis runs. "
                        "Credits never expire and carry over between days."),
        "price": float(p["price"]),
        "emoji": "\U0001f4c8",
        "category": "agent_runs",
        "credits": int(p["credits"]),
    }


# ---------------------------------------------------------------------------
# API: Create Stripe Checkout session
# ---------------------------------------------------------------------------

@bp.route("/api/checkout", methods=["POST"])
def api_checkout():
    """Create a Stripe Checkout session. Body: {"item_id": "...", "amount": 5.00}"""
    stripe = _get_stripe()
    if not stripe:
        return jsonify({"error": "Stripe is not configured. Add STRIPE_SECRET_KEY to .env"}), 503

    body = request.get_json(force=True, silent=True) or {}
    item_id = body.get("item_id", "")
    custom_amount = body.get("amount")
    buyer_email = (body.get("email") or "").strip().lower()[:200]
    return_to = (body.get("next") or "").strip()[:300]

    # An agent run pack, or one of the donation items.
    item = _agent_pack_item(item_id)
    if item:
        # Re-checked here and not only on the page: the page could have been
        # loaded while selling was possible and submitted after it stopped being.
        try:
            from ystocker import credits

            sellable, why = credits.selling_enabled()
        except Exception:  # noqa: BLE001
            sellable, why = False, "credits module unavailable"
        if not sellable:
            log.error("ypay: refusing to sell %s — %s", item_id, why)
            return jsonify({"error": "Run packs are temporarily unavailable. "
                                     "No charge has been made."}), 503
    if item and not (buyer_email and "@" in buyer_email):
        # Refused rather than sold: a run pack with nowhere to deliver the credits
        # is a payment we would have to refund by hand.
        return jsonify({"error": "Sign in on stock.li-family.us first so the "
                                 "runs can be added to your account."}), 400
    if not item:
        item = next((i for i in DEFAULT_ITEMS if i["id"] == item_id), None)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    # Determine price
    if item_id == "custom":
        try:
            amount = float(custom_amount or 0)
            if amount < 1:
                return jsonify({"error": "Minimum amount is $1.00"}), 400
            if amount > 9999:
                return jsonify({"error": "Maximum amount is $9,999"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid amount"}), 400
    else:
        amount = item["price"]

    # Determine base URL for success/cancel redirects
    base_url = request.host_url.rstrip("/")

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": item["name"],
                        "description": item["description"],
                    },
                    "unit_amount": int(amount * 100),  # Stripe uses cents
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{base_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/cancel",
            # Metadata is the only thing the webhook gets to work with, and it is
            # echoed back by Stripe rather than re-sent by the browser, so it is
            # the right place for the address to credit. The credit *count* is
            # deliberately absent: the webhook looks it up from the pack id, so a
            # tampered session cannot ask for more runs than it paid for.
            metadata={
                "item_id": item_id,
                "item_name": item["name"],
                "agent_credits": "1" if item.get("category") == "agent_runs" else "",
                "buyer_email": buyer_email,
                "return_to": return_to,
            },
            # Prefill and pin the address so the receipt goes to the same account
            # the runs land in.
            customer_email=buyer_email or None,
        )

        log.info("Stripe checkout created: %s ($%.2f) → %s", item["name"], amount, checkout_session.id)

        # Record the payment attempt
        _record_payment(checkout_session.id, item, amount, "pending")

        return jsonify({"checkout_url": checkout_session.url, "session_id": checkout_session.id})

    except Exception as exc:
        log.exception("Stripe checkout failed")
        return jsonify({"error": f"Payment failed: {exc}"}), 500


# ---------------------------------------------------------------------------
# API: Stripe webhook (payment confirmation)
# ---------------------------------------------------------------------------

@bp.route("/api/webhook", methods=["POST"])
def api_webhook():
    """Handle Stripe webhook events (payment completed, etc.)."""
    stripe = _get_stripe()
    if not stripe:
        return "Stripe not configured", 503

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # Whether this event's authenticity was actually established. Without the
    # signing secret the body is just an unauthenticated POST, and anyone who can
    # reach this URL could forge "payment completed". That was survivable while
    # this only incremented a donation counter; it is not now that the same event
    # grants paid analysis runs, so an unverified event may be recorded but never
    # credited.
    verified = False
    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            verified = True
        else:
            log.error("ypay: STRIPE_WEBHOOK_SECRET is not set — event accepted "
                      "UNVERIFIED. Run credits will NOT be granted.")
            event = json.loads(payload)
    except ValueError:
        return "Invalid payload", 400
    except Exception as exc:
        log.warning("Webhook signature verification failed: %s", exc)
        return "Invalid signature", 400

    # Handle checkout.session.completed
    if event.get("type") == "checkout.session.completed":
        session_data = event["data"]["object"]
        session_id = session_data.get("id", "")
        amount = session_data.get("amount_total", 0) / 100
        email = session_data.get("customer_details", {}).get("email", "")
        name = session_data.get("customer_details", {}).get("name", "")
        metadata = session_data.get("metadata", {})

        log.info("Payment completed: $%.2f from %s (%s) — %s",
                 amount, name, email, metadata.get("item_name", ""))

        _record_payment(session_id, {
            "id": metadata.get("item_id", ""),
            "name": metadata.get("item_name", ""),
        }, amount, "completed", email=email, customer_name=name)

        if metadata.get("agent_credits"):
            _grant_agent_runs(metadata, session_id, amount, email, verified)

    return "OK", 200


def _grant_agent_runs(metadata: dict, session_id: str, amount: float,
                      stripe_email: str, verified: bool) -> None:
    """Add purchased runs to a yStocker balance.

    Prefers the address the buyer was signed in as over the one Stripe collected:
    someone may pay with a different card email than the account they run
    analyses under, and the runs have to land where they will be spent.

    Never raises. A webhook that returns non-2xx is retried by Stripe, and the
    grant is idempotent on the session id, so a transient failure here is
    recoverable -- but an exception escaping into the handler would turn every
    delivery into a retry storm.
    """
    if not verified:
        log.error("ypay: refusing to grant runs for %s — event was not "
                  "signature-verified", session_id)
        return
    target = (metadata.get("buyer_email") or "").strip().lower() or stripe_email
    try:
        from ystocker import credits

        result = credits.grant(target, metadata.get("item_id", ""),
                               session_id, amount_usd=amount)
        if result.ok:
            log.info("ypay: granted %d runs to %s (session %s, %s)",
                     result.credited, target, session_id, result.reason)
        else:
            log.error("ypay: FAILED to grant runs to %s for session %s: %s",
                      target, session_id, result.reason)
    except Exception as exc:  # noqa: BLE001
        log.exception("ypay: granting runs for session %s raised: %s",
                      session_id, exc)


# ---------------------------------------------------------------------------
# API: Payment history
# ---------------------------------------------------------------------------

@bp.route("/api/payments")
def api_payments():
    """List recent payments (admin only in the future)."""
    table = _get_payments_table()
    if not table:
        return jsonify({"payments": []})

    try:
        resp = table.scan(Limit=50)
        payments = _decimal_to_float(resp.get("Items", []))
        payments.sort(key=lambda p: p.get("created_at", 0), reverse=True)
        return jsonify({"payments": payments})
    except Exception as exc:
        log.warning("Failed to list payments: %s", exc)
        return jsonify({"payments": [], "error": str(exc)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record_payment(session_id: str, item: dict, amount: float, status: str,
                    email: str = "", customer_name: str = "") -> None:
    """Record a payment to DynamoDB."""
    table = _get_payments_table()
    if not table:
        return
    try:
        table.put_item(Item={
            "session_id": session_id,
            "item_id": item.get("id", ""),
            "item_name": item.get("name", ""),
            "amount": Decimal(str(round(amount, 2))),
            "currency": "USD",
            "status": status,
            "email": email,
            "customer_name": customer_name,
            "created_at": int(time.time() * 1000),
        })
    except Exception as exc:
        log.warning("Failed to record payment: %s", exc)
