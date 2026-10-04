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
from urllib.parse import urlencode, urlsplit

from flask import (
    Blueprint, abort, render_template, request, jsonify, redirect, url_for,
    send_from_directory, session,
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

    On pay.trade-agents.com this is the Prepay (充值) page in that site's frame,
    selling run packs only; see "The TradeAgents brand" below.
    """
    stripe_pk = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    email = (request.args.get("email") or "").strip()[:200]
    buyer_email = email if "@" in email else ""
    nxt = _safe_return(request.args.get("next"))
    if _is_ta_host():
        return render_template("ta/index.html",
                               packs=_ta_packs(_agent_packs()),
                               buyer_email=buyer_email,
                               return_to=nxt or TA_RETURN,
                               canceled=request.args.get("canceled") == "1")
    return render_template("index.html",
                           items=DEFAULT_ITEMS,
                           agent_packs=_agent_packs(),
                           buyer_email=buyer_email,
                           return_to=nxt,
                           stripe_pk=stripe_pk,
                           stripe_configured=bool(stripe_pk))


@bp.route("/success")
def success():
    """Payment success page."""
    session_id = request.args.get("session_id", "")
    if _is_ta_host():
        return render_template("ta/success.html",
                               session_id=session_id[:200],
                               pack=_pack(request.args.get("pack")),
                               return_to=_safe_return(request.args.get("next")) or TA_RETURN)
    return render_template("success.html", session_id=session_id)


@bp.route("/cancel")
def cancel():
    """Payment cancelled page.

    A TradeAgents checkout no longer cancels to here -- it goes back to the pack
    page (see api_checkout) -- so on that host this only answers a session
    opened before that change, and says so in the site's own frame.
    """
    if _is_ta_host():
        return render_template("ta/cancel.html",
                               return_to=_safe_return(request.args.get("next")) or TA_RETURN)
    return render_template("cancel.html")


# ---------------------------------------------------------------------------
# The TradeAgents brand
# ---------------------------------------------------------------------------
# pay.trade-agents.com is this same app, but a buyer there came from that site's
# account menu (Prepay, 充值) or from its run page, and is still on TradeAgents
# while being asked for money. So on that host the pages are drawn in the site's
# frame -- its masthead, paper plane, type and footer, in English or Chinese --
# and sell run packs only: the donation card and its "Li Family apps" copy mean
# nothing there. pay.li-family.us keeps yPay's own pages, unchanged.

#: Where a TradeAgents buyer goes back to when `next` is absent or refused: the
#: run form, which is where both links that lead here were clicked from.
TA_SITE = "https://trade-agents.com"
TA_RETURN = TA_SITE + "/agents"

# `next` arrives in the query string and is shown as a link on the page that
# says "payment received" -- the one place a planted address would be believed
# most -- so only this monorepo's own sites are honoured, over https. A backslash
# is refused outright because Python and browsers disagree about it:
# urlsplit("https://evil.example\\@trade-agents.com") names trade-agents.com as
# the host, and a browser goes to evil.example.
_RETURN_DOMAINS = ("trade-agents.com", "li-family.us")


def _safe_return(url) -> str:
    """``url`` if it is an https address on one of our domains, else ""."""
    url = (url or "").strip()[:300]
    if not url or "\\" in url or any(ord(c) < 0x21 or ord(c) == 0x7f for c in url):
        return ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    if parts.scheme != "https" or "@" in parts.netloc:
        return ""
    if not any(host == d or host.endswith("." + d) for d in _RETURN_DOMAINS):
        return ""
    return url


def _ta_packs(packs: list) -> list:
    """The packs as the TradeAgents page lists them: each with what it saves
    per run against the dearest one (the smallest pack), so the ladder reads
    at a glance. Whole percent, and 0 for the pack everything is measured by."""
    if not packs:
        return []
    base = max(p["per_run"] for p in packs)
    out = []
    for p in packs:
        save = round((1 - p["per_run"] / base) * 100) if base else 0
        out.append({**p, "save_pct": max(0, save)})
    return out


def _pack(pack_id):
    """A run pack by id from yStocker's table, or None -- for the success page,
    which says how many runs are on the way. The count comes from the table,
    never from the query string that names the pack."""
    try:
        from ystocker import credits

        return credits.pack(pack_id or "")
    except Exception:  # noqa: BLE001 - the page falls back to "your runs"
        return None


# What the TradeAgents pages borrow from yStocker's static folder: the site's
# stylesheet and its mark. Served from that checkout rather than copied, so the
# pay page cannot drift from the site it belongs to -- both apps run from one
# checkout on one box, the reason the run packs are a plain import of
# ystocker.credits. By name, so this host serves exactly these two files, and not
# under /static/, which nginx maps to this app's own folder on both pay hosts.
_YSTOCKER_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ystocker", "static")
_TA_ASSETS = {"wiki.css": "text/css", "favicon.svg": "image/svg+xml"}


def _asset_version() -> str:
    """A stamp for the pages' own stylesheets, taken once at import: the deploy
    restarts this app whenever either file changes, and nginx serves /static/
    with a 7-day expiry, so a URL that does not change is a stale page."""
    stamps = []
    for path in (os.path.join(_YSTOCKER_STATIC, "wiki.css"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static", "css", "ta.css")):
        try:
            stamps.append(int(os.path.getmtime(path)))
        except OSError:
            pass
    return str(max(stamps, default=0))


TA_ASSET_VER = _asset_version()


@bp.app_context_processor
def _inject_ta_asset_ver():
    return {"ta_asset_ver": TA_ASSET_VER, "ta_site": TA_SITE}


@bp.route("/ta/<name>")
def ta_asset(name: str):
    """trade-agents.com's stylesheet or mark, read from yStocker's checkout."""
    if name not in _TA_ASSETS:
        abort(404)
    return send_from_directory(_YSTOCKER_STATIC, name,
                               mimetype=_TA_ASSETS[name], max_age=7 * 86400)


# ---------------------------------------------------------------------------
# yStocker agent run packs
# ---------------------------------------------------------------------------
# The pack table lives in ystocker.credits so that the app that *sells* runs and
# the app that *spends* them cannot drift apart on how many a pack contains. Both
# apps run from the same checkout on the same box, so this is a plain import.
def _is_ta_host() -> bool:
    """True when this request arrived on the TradeAgents side of the house."""
    try:
        host = (request.host or "").split(":")[0].lower()
    except Exception:  # noqa: BLE001
        return False
    return host in {"pay.trade-agents.com", "trade-agents.com",
                    "www.trade-agents.com"}


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


def _agent_pack_item(pack_id: str, locale: str | None = None):
    """A pack as a checkout line item, or None when the id is unknown.

    The price comes from the pack table, never from the request, so a caller
    cannot buy the 130-run pack for $5.

    The name and description are what Stripe's page and its receipt show, so
    they carry the brand of the host the buyer is on -- "yStocker — 28 runs" in
    front of a TradeAgents buyer was the one line on Stripe's page naming a
    site they had never visited -- and, from that brand's page, its language.
    """
    try:
        from ystocker import credits

        p = credits.pack(pack_id)
    except Exception:  # noqa: BLE001
        return None
    if not p:
        return None
    n = int(p["credits"])
    if not _is_ta_host():
        name = f"yStocker — {p['label']}"
        description = (f"{n} Trading Agents analysis runs. "
                       "Credits never expire and carry over between days.")
    elif locale == "zh":
        name = f"TradeAgents — {n} 次分析"
        description = f"TradeAgents {n} 次分析。购买的次数永不过期，可跨日累积使用。"
    else:
        name = f"TradeAgents — {n} runs"
        description = (f"{n} analysis runs on TradeAgents. "
                       "Runs never expire and carry over between days.")
    return {
        "id": pack_id,
        "name": name,
        "description": description,
        "price": float(p["price"]),
        "emoji": "\U0001f4c8",
        "category": "agent_runs",
        "credits": n,
    }


# ---------------------------------------------------------------------------
# API: Create Stripe Checkout session
# ---------------------------------------------------------------------------

@bp.route("/api/checkout", methods=["POST"])
def api_checkout():
    """Create a Stripe Checkout session. Body: {"item_id": "...", "amount": 5.00}

    Every refusal carries a ``code`` beside its English ``error``: the
    TradeAgents page is in English or Chinese and says it in the reader's
    language from the code, where yPay's page shows ``error`` as it always has.
    """
    stripe = _get_stripe()
    if not stripe:
        return jsonify({"error": "Stripe is not configured. Add STRIPE_SECRET_KEY to .env",
                        "code": "unavailable"}), 503

    body = request.get_json(force=True, silent=True) or {}
    item_id = body.get("item_id", "")
    custom_amount = body.get("amount")
    buyer_email = (body.get("email") or "").strip().lower()[:200]
    return_to = _safe_return(body.get("next"))
    # The page's language, sent only by the TradeAgents page: Stripe's own page
    # then opens in it too, rather than in whatever the browser's locale says.
    locale = {"en": "en", "zh": "zh"}.get((body.get("lang") or "").strip().lower())

    # An agent run pack, or one of the donation items.
    item = _agent_pack_item(item_id, locale)
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
                                     "No charge has been made.",
                            "code": "unavailable"}), 503
    if item and not (buyer_email and "@" in buyer_email):
        # Refused rather than sold: a run pack with nowhere to deliver the credits
        # is a payment we would have to refund by hand.
        # Names the site the buyer actually came from. Hardcoding
        # stock.li-family.us sent a TradeAgents buyer to a domain they had never
        # seen, at the one moment they are being asked to trust the page.
        _origin = "trade-agents.com" if _is_ta_host() else "stock.li-family.us"
        return jsonify({"error": f"Sign in on {_origin} first so the "
                                 "runs can be added to your account.",
                        "code": "sign_in"}), 400
    if not item and not _is_ta_host():
        item = next((i for i in DEFAULT_ITEMS if i["id"] == item_id), None)
    if not item:
        # Donations are yPay's, and the TradeAgents page sells none: an item id
        # that is not a pack there is not something that page offered.
        return jsonify({"error": "Item not found", "code": "not_found"}), 404

    # Determine price
    if item_id == "custom":
        try:
            amount = float(custom_amount or 0)
            if amount < 1:
                return jsonify({"error": "Minimum amount is $1.00", "code": "amount"}), 400
            if amount > 9999:
                return jsonify({"error": "Maximum amount is $9,999", "code": "amount"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid amount", "code": "amount"}), 400
    else:
        amount = item["price"]

    # Determine base URL for success/cancel redirects. https whatever host_url
    # says: nginx terminates TLS and no app here installs ProxyFix, so it reads
    # http:// in production, and Stripe then sent the buyer back over plain
    # HTTP -- the address in the cancel URL included -- for nginx to redirect.
    # ystocker's _share_base() forces it for the same reason. A local host, with
    # no TLS in front, keeps what it has.
    base_url = request.host_url.rstrip("/")
    if request.host.split(":")[0] not in ("localhost", "127.0.0.1"):
        base_url = "https://" + request.host
    success_url = f"{base_url}/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base_url}/cancel"
    if _is_ta_host() and item.get("category") == "agent_runs":
        # Back on Stripe's page means back to the packs, with the address the
        # pack page needs to show any: /cancel had nothing to buy on it, and
        # "Try again" from there led to a pack page with no address, which
        # shows none. The success page is told the pack, so it can say how many
        # runs are coming, and the way back to the desk.
        again = {"email": buyer_email, "canceled": "1"}
        done = {"pack": item_id}
        if return_to:
            again["next"] = done["next"] = return_to
        cancel_url = f"{base_url}/?{urlencode(again)}"
        success_url += "&" + urlencode(done)

    extra = {"locale": locale} if locale else {}
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
            success_url=success_url,
            cancel_url=cancel_url,
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
            **extra,
        )

        log.info("Stripe checkout created: %s ($%.2f) → %s", item["name"], amount, checkout_session.id)

        # Record the payment attempt
        _record_payment(checkout_session.id, item, amount, "pending")

        return jsonify({"checkout_url": checkout_session.url, "session_id": checkout_session.id})

    except Exception as exc:
        log.exception("Stripe checkout failed")
        return jsonify({"error": f"Payment failed: {exc}", "code": "failed"}), 500


# ---------------------------------------------------------------------------
# TradeAgents Pro: the subscription checkout, its return, and the billing portal
# ---------------------------------------------------------------------------
# Every request arrives as a token yStocker signed for a signed-in reader
# (ystocker.subscriptions.handoff): this host shares no session with yStocker,
# and the portal manages a subscription, so a bare address must not open it.

def _pay_base() -> str:
    base = request.host_url.rstrip("/")
    if request.host.split(":")[0] not in ("localhost", "127.0.0.1"):
        base = "https://" + request.host
    return base


@bp.route("/subscribe/start")
def subscribe_start():
    """Open Stripe Checkout for a plan, with the trial if this account has not had one."""
    from ystocker import subscriptions

    payload = subscriptions.read_handoff(request.args.get("t", ""), "checkout")
    if not payload:
        return redirect(subscriptions.DEFAULT_ORIGIN + "/subscribe?error=expired")
    origin, email = payload["o"], payload["e"]
    plan_id = payload.get("plan") or "month"
    if subscriptions.plan(plan_id) is None:
        return redirect(origin + "/subscribe?error=plan")
    # One strict read decides all three: already subscribed, trial taken, and
    # the existing Stripe customer. Unreadable is a refusal, not "none" -- that
    # would offer a second trial, or a second subscription on top of the first.
    try:
        row = subscriptions.load(email) or {}
    except subscriptions.StoreUnavailable as exc:
        log.error("ypay: subscription row unreadable for %s: %s", email, exc)
        return redirect(origin + "/subscribe?error=unavailable")
    if subscriptions.entitled(row):
        return redirect(origin + "/subscribe?already=1")
    stripe = _get_stripe()
    if not stripe:
        return redirect(origin + "/subscribe?error=unavailable")
    trial = subscriptions.TRIAL_DAYS if not row.get("trial_used") else 0
    try:
        from ypay import billing

        price_id = billing.ensure_price(stripe, plan_id)
        params = billing.checkout_params(
            plan_id=plan_id, email=email, price_id=price_id,
            customer=str(row.get("customer") or ""), trial_days=trial,
            origin=origin, pay_base=_pay_base(), lang=payload.get("lang") or "")
        session_obj = stripe.checkout.Session.create(**params)
    except Exception:  # noqa: BLE001
        log.exception("ypay: subscription checkout failed for %s", email)
        return redirect(origin + "/subscribe?error=checkout")
    log.info("ypay: subscription checkout %s for %s (%s, trial %d days)",
             session_obj.id, email, plan_id, trial)
    return redirect(session_obj.url, code=303)


@bp.route("/subscribe/done")
def subscribe_done():
    """Stripe's return: record the subscription now, then send the reader home."""
    from ystocker import subscriptions

    session_id = request.args.get("session_id", "")
    origin = subscriptions.DEFAULT_ORIGIN
    stripe = _get_stripe()
    if not stripe or not session_id.startswith("cs_"):
        return redirect(origin + "/subscribe?error=unavailable")
    try:
        from ypay import billing

        session_obj = billing._as_dict(stripe.checkout.Session.retrieve(session_id))
        meta = session_obj.get("metadata") or {}
        origin = subscriptions.origin_ok(meta.get("origin", ""))
        lang = meta.get("lang") if meta.get("lang") in ("en", "zh") else ""
        row = billing.record_session(stripe, session_id)
    except Exception:  # noqa: BLE001 - the webhook and the reconciliation will catch up
        log.exception("ypay: could not record subscription session %s", session_id)
        return redirect(origin + "/subscribe?pending=1")
    tail = f"&lang={lang}" if lang else ""
    return redirect(origin + ("/subscribe?welcome=1" if row else "/subscribe?pending=1") + tail)


@bp.route("/subscribe/portal")
def subscribe_portal():
    """Stripe's billing portal: card, invoices, switch plan, cancel."""
    from ystocker import subscriptions

    payload = subscriptions.read_handoff(request.args.get("t", ""), "portal")
    if not payload:
        return redirect(subscriptions.DEFAULT_ORIGIN + "/subscribe?error=expired")
    origin, email = payload["o"], payload["e"]
    customer = subscriptions.customer_for(email)
    stripe = _get_stripe()
    if not customer:
        return redirect(origin + "/subscribe?error=no_billing")
    if not stripe:
        return redirect(origin + "/subscribe?error=unavailable")
    try:
        from ypay import billing

        lang = payload.get("lang") if payload.get("lang") in ("en", "zh") else ""
        extra = {"locale": lang} if lang else {}
        portal = stripe.billing_portal.Session.create(
            customer=customer, return_url=origin + "/subscribe" + (f"?lang={lang}" if lang else ""),
            configuration=billing.ensure_portal_config(stripe), **extra)
    except Exception:  # noqa: BLE001
        log.exception("ypay: billing portal failed for %s", email)
        return redirect(origin + "/subscribe?error=portal")
    return redirect(portal.url, code=303)


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
            # construct_event verifies the signature; the event is then read
            # from the payload as a plain dict. stripe 15's objects are not
            # dicts, and every `.get` below raised AttributeError on one: a
            # verified run-pack payment would have 500'd and never been
            # credited (found 2026-10-04, before any customer hit it).
            stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            event = json.loads(payload)
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

    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}

    # TradeAgents Pro: a subscription checkout, and the subscription's own
    # lifecycle. Recorded only from a verified event; see ypay.billing.
    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        if verified:
            try:
                from ypay import billing

                billing.record_session(stripe, obj.get("id", ""))
            except Exception:  # noqa: BLE001 - Stripe retries a non-2xx
                log.exception("ypay: could not record subscription checkout %s", obj.get("id"))
                return "retry", 500
        return "OK", 200
    if etype.startswith("customer.subscription."):
        if verified:
            try:
                from ypay import billing

                billing.record_event_subscription(obj, int(event.get("created") or 0))
            except Exception:  # noqa: BLE001
                log.exception("ypay: could not record %s for %s", etype, obj.get("id"))
                return "retry", 500
        return "OK", 200

    # Handle checkout.session.completed
    if etype == "checkout.session.completed":
        session_data = obj
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
