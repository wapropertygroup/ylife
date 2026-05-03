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
        "description": "Support the Li Family apps with a small donation.",
        "price": 5.00,
        "emoji": "☕",
        "category": "donation",
    },
    {
        "id": "lunch",
        "name": "Buy Me Lunch",
        "description": "A bigger thank-you — help cover hosting costs.",
        "price": 15.00,
        "emoji": "\U0001f354",
        "category": "donation",
    },
    {
        "id": "hosting",
        "name": "Monthly Hosting",
        "description": "Cover one month of EC2 + domain costs for all apps.",
        "price": 25.00,
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
    """Payment landing page with available items."""
    stripe_pk = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    return render_template("index.html",
                           items=DEFAULT_ITEMS,
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

    # Find the item
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
            metadata={
                "item_id": item_id,
                "item_name": item["name"],
            },
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

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
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

    return "OK", 200


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
