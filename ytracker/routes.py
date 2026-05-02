"""
ytracker.routes
~~~~~~~~~~~~~~~
URL routes for the yTracker price tracker.
Supports Amazon, Walmart, and Uber Eats.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, jsonify, Response, session,
)

from ytracker.scraper import (
    fetch_product, detect_store, extract_item_id,
    STORE_NAMES, STORE_COLORS,
)

bp = Blueprint("tracker", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DynamoDB helpers (lazy init with backoff, same pattern as yPlanter)
# ---------------------------------------------------------------------------
_ITEMS_TABLE_NAME = "ytracker-items"
_PRICES_TABLE_NAME = "ytracker-prices"
_items_table = None
_prices_table = None
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
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _ITEMS_TABLE_NAME, exc)
        return None


def _get_prices_table():
    global _prices_table
    if _prices_table is not None:
        return _prices_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_PRICES_TABLE_NAME)
        table.load()
        _prices_table = table
        return _prices_table
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _PRICES_TABLE_NAME, exc)
        return None


def _get_session_id() -> str:
    """Get or create a persistent session ID for tracking items."""
    sid = session.get("session_id")
    if not sid:
        sid = str(uuid.uuid4())[:12]
        session["session_id"] = sid
    return sid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decimal_to_float(obj):
    """Convert DynamoDB Decimals to floats for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


def _now_ts() -> int:
    """Current Unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def _format_ts(ts_ms: int) -> str:
    """Format millisecond timestamp to human-readable string."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    return render_template("index.html", store_names=STORE_NAMES, store_colors=STORE_COLORS)


@bp.route("/item/<store>/<item_id>")
def item_detail(store: str, item_id: str):
    return render_template("item_detail.html", store=store, item_id=item_id,
                           store_names=STORE_NAMES, store_colors=STORE_COLORS)


# ---------------------------------------------------------------------------
# API: List tracked items
# ---------------------------------------------------------------------------

@bp.route("/api/items")
def api_items():
    """List all tracked items for the current session."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable", "items": []}), 503

    session_id = _get_session_id()
    try:
        from boto3.dynamodb.conditions import Key
        resp = table.query(
            KeyConditionExpression=Key("user_id").eq(session_id),
        )
        items = _decimal_to_float(resp.get("Items", []))
        # Sort by added_at descending
        items.sort(key=lambda x: x.get("added_at", 0), reverse=True)

        # Fetch last 30 prices for sparklines
        prices_table = _get_prices_table()
        for item in items:
            item["sparkline"] = []
            if prices_table:
                try:
                    key = f'{item["store"]}#{item["item_id"]}'
                    pr = prices_table.query(
                        KeyConditionExpression=Key("store_item_id").eq(key),
                        ScanIndexForward=True,
                        Limit=60,
                    )
                    item["sparkline"] = _decimal_to_float([
                        {"t": int(p["timestamp"]), "p": float(p["price"])}
                        for p in pr.get("Items", []) if p.get("price")
                    ])
                except Exception:
                    pass

        return jsonify({"items": items})
    except Exception as exc:
        log.exception("Failed to list items")
        return jsonify({"error": str(exc), "items": []}), 500


# ---------------------------------------------------------------------------
# API: Add item
# ---------------------------------------------------------------------------

@bp.route("/api/item/add", methods=["POST"])
def api_add_item():
    """Add an item to track. Body: {"url": "https://..."} """
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    store = detect_store(url)
    if not store:
        supported = ", ".join(STORE_NAMES.values())
        return jsonify({"error": f"Unsupported store. Supported stores: {supported}"}), 400

    item_id = extract_item_id(url, store)
    if not item_id:
        return jsonify({"error": f"Could not extract item ID from URL"}), 400

    # Check if already tracked
    table = _get_items_table()
    session_id = _get_session_id()
    if table:
        try:
            existing = table.get_item(Key={"user_id": session_id, "item_key": f"{store}#{item_id}"})
            if existing.get("Item"):
                return jsonify({"error": "Item is already being tracked", "item": _decimal_to_float(existing["Item"])}), 409
        except Exception:
            pass

    # Fetch product info
    product = fetch_product(url, store)
    if not product:
        return jsonify({"error": f"Could not fetch product info from {STORE_NAMES.get(store, store)}. The page may be blocked or the URL may be incorrect."}), 422

    now = _now_ts()

    # Save to items table
    item = {
        "user_id": session_id,
        "item_key": f"{store}#{item_id}",
        "store": store,
        "item_id": item_id,
        "title": product["title"],
        "image_url": product.get("image_url", ""),
        "item_url": product.get("item_url", url),
        "current_price": Decimal(str(product["price"])) if product.get("price") else None,
        "previous_price": None,
        "record_low_price": Decimal(str(product["price"])) if product.get("price") else None,
        "record_low_date": now if product.get("price") else None,
        "record_high_price": Decimal(str(product["price"])) if product.get("price") else None,
        "is_record_low": True if product.get("price") else False,
        "currency": product.get("currency", "USD"),
        "added_at": now,
        "last_checked": now,
        "notify_email": "",
        "notify_enabled": False,
    }

    # Remove None values (DynamoDB doesn't accept None)
    item = {k: v for k, v in item.items() if v is not None}

    if table:
        try:
            table.put_item(Item=item)
        except Exception as exc:
            log.exception("Failed to save item")
            return jsonify({"error": f"Failed to save: {exc}"}), 500

    # Record first price point
    if product.get("price"):
        _record_price(store, item_id, product["price"], now)

    return jsonify({"item": _decimal_to_float(item)}), 201


# ---------------------------------------------------------------------------
# API: Delete item
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>", methods=["DELETE"])
def api_delete_item(store: str, item_id: str):
    """Remove a tracked item."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    try:
        table.delete_item(Key={"user_id": session_id, "item_key": f"{store}#{item_id}"})
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Failed to delete item")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Price history
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/prices")
def api_prices(store: str, item_id: str):
    """Get price history for an item."""
    table = _get_prices_table()
    if not table:
        return jsonify({"error": "Database unavailable", "prices": []}), 503

    days = int(request.args.get("days", "90"))
    since = _now_ts() - (days * 86400 * 1000)

    try:
        from boto3.dynamodb.conditions import Key
        key = f"{store}#{item_id}"
        resp = table.query(
            KeyConditionExpression=Key("store_item_id").eq(key) & Key("timestamp").gte(since),
            ScanIndexForward=True,
        )
        prices = _decimal_to_float([
            {"timestamp": int(p["timestamp"]), "price": float(p["price"]),
             "formatted_date": _format_ts(int(p["timestamp"]))}
            for p in resp.get("Items", []) if p.get("price")
        ])
        return jsonify({"prices": prices})
    except Exception as exc:
        log.exception("Failed to fetch prices")
        return jsonify({"error": str(exc), "prices": []}), 500


# ---------------------------------------------------------------------------
# API: Force price check
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/check", methods=["POST"])
def api_check_price(store: str, item_id: str):
    """Force an immediate price check for one item."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    try:
        resp = table.get_item(Key={"user_id": session_id, "item_key": f"{store}#{item_id}"})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Item not found"}), 404

        result = _check_single_item(item)
        if not result:
            return jsonify({"error": "Could not reach store — try again in a moment"}), 422

        out = _decimal_to_float(result)
        if not result.get("_price_found"):
            out["_warning"] = "Product info updated but price could not be extracted. The store may be blocking automated access."
        return jsonify({"item": out})
    except Exception as exc:
        log.exception("Price check failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Live fetch (scrape without requiring DynamoDB)
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/fetch-live", methods=["POST"])
def api_fetch_live(store: str, item_id: str):
    """
    Scrape current product info live from the store.
    Always updates DynamoDB with whatever we get (title, image, price).
    Returns the scraped product data + the updated item from DynamoDB.
    """
    store = store.strip().lower()
    item_id = item_id.strip()
    if store not in STORE_NAMES:
        return jsonify({"error": f"Unknown store: {store}"}), 400

    log.info("Live fetch: %s/%s", store, item_id)
    try:
        from ytracker.scraper import _build_url
        url = _build_url(store, item_id)
        if not url:
            return jsonify({"error": "Cannot build URL for this store/item"}), 400

        product = fetch_product(url, store)
        if not product:
            return jsonify({"error": f"Could not fetch from {STORE_NAMES.get(store, store)}. The page may be blocked or unavailable."}), 422

        # Always update the tracked DynamoDB item with whatever we scraped
        updated_item = None
        table = _get_items_table()
        if table:
            session_id = _get_session_id()
            try:
                resp = table.get_item(Key={"user_id": session_id, "item_key": f"{store}#{item_id}"})
                existing = resp.get("Item")
                if existing:
                    updated_item = _check_single_item(existing)
            except Exception:
                pass

        result = {"product": product}
        if updated_item:
            result["item"] = _decimal_to_float(updated_item)
        return jsonify(result)
    except Exception as exc:
        log.exception("Live fetch failed for %s/%s", store, item_id)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Update title
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/title", methods=["POST"])
def api_update_title(store: str, item_id: str):
    """Update the item title. Body: {"title": "..."}"""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    body = request.get_json(force=True, silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    session_id = _get_session_id()
    try:
        table.update_item(
            Key={"user_id": session_id, "item_key": f"{store}#{item_id}"},
            UpdateExpression="SET title = :t",
            ExpressionAttributeValues={":t": title[:300]},
        )
        return jsonify({"ok": True, "title": title[:300]})
    except Exception as exc:
        log.exception("Failed to update title")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Alternate URLs (compare same product across stores)
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/alt-urls", methods=["GET"])
def api_get_alt_urls(store: str, item_id: str):
    """Get alternate URLs for an item."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable", "alt_urls": []}), 503

    session_id = _get_session_id()
    try:
        resp = table.get_item(Key={"user_id": session_id, "item_key": f"{store}#{item_id}"})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Item not found", "alt_urls": []}), 404
        alt_urls = json.loads(item.get("alt_urls", "[]"))
        return jsonify({"alt_urls": _decimal_to_float(alt_urls)})
    except Exception as exc:
        return jsonify({"error": str(exc), "alt_urls": []}), 500


@bp.route("/api/item/<store>/<item_id>/alt-urls", methods=["POST"])
def api_add_alt_url(store: str, item_id: str):
    """Add an alternate URL for the same product. Body: {"url": "https://..."}"""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    alt_store = detect_store(url)
    if not alt_store:
        supported = ", ".join(STORE_NAMES.values())
        return jsonify({"error": f"Unsupported store. Supported: {supported}"}), 400

    alt_item_id = extract_item_id(url, alt_store)
    if not alt_item_id:
        return jsonify({"error": "Could not extract item ID from URL"}), 400

    session_id = _get_session_id()
    item_key = f"{store}#{item_id}"

    try:
        resp = table.get_item(Key={"user_id": session_id, "item_key": item_key})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Item not found"}), 404

        alt_urls = json.loads(item.get("alt_urls", "[]"))

        # Check for duplicate
        for au in alt_urls:
            if au.get("store") == alt_store and au.get("item_id") == alt_item_id:
                return jsonify({"error": f"Already tracking this {STORE_NAMES.get(alt_store, alt_store)} URL"}), 409

        # Try to fetch current price
        product = fetch_product(url, alt_store)
        new_entry = {
            "store": alt_store,
            "item_id": alt_item_id,
            "url": product.get("item_url", url) if product else url,
            "title": product.get("title", "") if product else "",
            "price": product["price"] if product and product.get("price") else None,
            "last_checked": _now_ts(),
        }
        alt_urls.append(new_entry)

        # Save to DynamoDB
        table.update_item(
            Key={"user_id": session_id, "item_key": item_key},
            UpdateExpression="SET alt_urls = :au",
            ExpressionAttributeValues={":au": json.dumps(alt_urls, default=str)},
        )

        # Record price if available
        if new_entry["price"]:
            _record_price(alt_store, alt_item_id, new_entry["price"])

        return jsonify({"alt_urls": alt_urls, "added": new_entry}), 201
    except Exception as exc:
        log.exception("Failed to add alt URL")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/item/<store>/<item_id>/alt-urls/<int:idx>", methods=["DELETE"])
def api_delete_alt_url(store: str, item_id: str, idx: int):
    """Remove an alternate URL by index."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    item_key = f"{store}#{item_id}"

    try:
        resp = table.get_item(Key={"user_id": session_id, "item_key": item_key})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Item not found"}), 404

        alt_urls = json.loads(item.get("alt_urls", "[]"))
        if idx < 0 or idx >= len(alt_urls):
            return jsonify({"error": "Invalid index"}), 400

        removed = alt_urls.pop(idx)
        table.update_item(
            Key={"user_id": session_id, "item_key": item_key},
            UpdateExpression="SET alt_urls = :au",
            ExpressionAttributeValues={":au": json.dumps(alt_urls, default=str)},
        )
        return jsonify({"ok": True, "removed": removed, "alt_urls": alt_urls})
    except Exception as exc:
        log.exception("Failed to delete alt URL")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/item/<store>/<item_id>/alt-urls/check", methods=["POST"])
def api_check_alt_urls(store: str, item_id: str):
    """Check prices for all alternate URLs."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    item_key = f"{store}#{item_id}"

    try:
        resp = table.get_item(Key={"user_id": session_id, "item_key": item_key})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Item not found"}), 404

        alt_urls = json.loads(item.get("alt_urls", "[]"))
        updated = []
        for au in alt_urls:
            product = fetch_product(au.get("url", ""), au.get("store", ""))
            now = _now_ts()
            au["last_checked"] = now
            if product:
                if product.get("title"):
                    au["title"] = product["title"][:300]
                if product.get("price"):
                    old_alt = au.get("price") or 0
                    new_alt = product["price"]
                    price_changed = abs(new_alt - (old_alt or 0)) >= 0.005
                    au["price"] = new_alt
                    if price_changed or not old_alt:
                        _record_price(au["store"], au["item_id"], new_alt, now)
            updated.append(au)
            time.sleep(2)  # Rate limit

        table.update_item(
            Key={"user_id": session_id, "item_key": item_key},
            UpdateExpression="SET alt_urls = :au",
            ExpressionAttributeValues={":au": json.dumps(updated, default=str)},
        )
        return jsonify({"alt_urls": updated})
    except Exception as exc:
        log.exception("Failed to check alt URLs")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Check all prices
# ---------------------------------------------------------------------------

@bp.route("/api/check-all", methods=["POST"])
def api_check_all():
    """Trigger price check for all tracked items."""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    try:
        from boto3.dynamodb.conditions import Key
        resp = table.query(KeyConditionExpression=Key("user_id").eq(session_id))
        items = resp.get("Items", [])

        checked = 0
        errors = 0
        for item in items:
            result = _check_single_item(item)
            if result:
                checked += 1
            else:
                errors += 1
            time.sleep(3)  # Rate limit between checks

        return jsonify({"checked": checked, "errors": errors, "total": len(items)})
    except Exception as exc:
        log.exception("Check-all failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Set notification preferences
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/notify", methods=["POST"])
def api_set_notify(store: str, item_id: str):
    """Set notification email for an item. Body: {"email": "...", "enabled": true}"""
    table = _get_items_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    body = request.get_json(force=True, silent=True) or {}
    email = body.get("email", "").strip()
    enabled = body.get("enabled", True)

    session_id = _get_session_id()
    try:
        table.update_item(
            Key={"user_id": session_id, "item_key": f"{store}#{item_id}"},
            UpdateExpression="SET notify_email = :e, notify_enabled = :n",
            ExpressionAttributeValues={":e": email, ":n": enabled},
        )
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Failed to update notification settings")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: AI price analysis (SSE streaming via Gemini)
# ---------------------------------------------------------------------------

@bp.route("/api/item/<store>/<item_id>/ai-analysis", methods=["POST"])
def api_ai_analysis(store: str, item_id: str):
    """Stream a Gemini AI analysis of price trends via SSE."""
    from google import genai

    body = request.get_json(force=True, silent=True) or {}
    lang = body.get("lang", "en")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    # Fetch item + price history
    table = _get_items_table()
    prices_table = _get_prices_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    try:
        resp = table.get_item(Key={"user_id": session_id, "item_key": f"{store}#{item_id}"})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Item not found"}), 404
    except Exception:
        return jsonify({"error": "Failed to load item"}), 500

    # Get price history
    price_data = []
    if prices_table:
        try:
            from boto3.dynamodb.conditions import Key as DKey
            key = f"{store}#{item_id}"
            pr = prices_table.query(
                KeyConditionExpression=DKey("store_item_id").eq(key),
                ScanIndexForward=True,
            )
            price_data = [
                f"{_format_ts(int(p['timestamp']))}: ${float(p['price']):.2f}"
                for p in pr.get("Items", []) if p.get("price")
            ]
        except Exception:
            pass

    store_name = STORE_NAMES.get(store, store)
    lang_instruction = "Answer in Chinese (简体中文)." if lang == "zh" else "Answer in English."

    prompt = f"""You are a savvy online shopping advisor and price analysis expert.
Analyze the price history of this product and provide buying advice.

Product: {item.get('title', 'Unknown')}
Store: {store_name}
Current Price: ${float(item.get('current_price', 0)):.2f}
Record Low: ${float(item.get('record_low_price', 0)):.2f}
Record High: ${float(item.get('record_high_price', 0)):.2f}

Price History (last 90 days):
{chr(10).join(price_data[-30:]) if price_data else 'Limited data available'}

Provide:
1. Price trend analysis (rising, falling, stable, volatile)
2. Whether the current price is a good deal
3. Best time to buy recommendation
4. Any patterns (e.g., regular sales cycles)

Be concise (2-3 paragraphs). {lang_instruction}"""

    client = genai.Client(api_key=api_key)

    def generate():
        try:
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash", contents=prompt
            )
            for chunk in stream:
                text = chunk.text
                if text:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            log.exception("Gemini analysis failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Price recording + checking logic
# ---------------------------------------------------------------------------

def _record_price(store: str, item_id: str, price: float, ts: int = None) -> None:
    """Record a price point in the prices table."""
    table = _get_prices_table()
    if not table or not price:
        return
    try:
        table.put_item(Item={
            "store_item_id": f"{store}#{item_id}",
            "timestamp": ts or _now_ts(),
            "price": Decimal(str(round(price, 2))),
            "store": store,
        })
    except Exception as exc:
        log.warning("Failed to record price for %s#%s: %s", store, item_id, exc)


def _check_single_item(item: dict) -> dict | None:
    """Check price for one item. Returns updated item dict or None on total fetch failure."""
    store = item.get("store", "")
    item_id = item.get("item_id", "")
    item_url = item.get("item_url", "")

    product = fetch_product(item_url or item_id, store)
    if not product:
        return None

    now = _now_ts()
    table = _get_items_table()

    # Always update title / image / last_checked, even if price is missing
    update_expr = "SET last_checked = :lc"
    expr_vals: dict = {":lc": now}

    if product.get("title"):
        update_expr += ", title = :t"
        expr_vals[":t"] = product["title"][:300]
        item["title"] = product["title"][:300]
    if product.get("image_url"):
        update_expr += ", image_url = :img"
        expr_vals[":img"] = product["image_url"]
        item["image_url"] = product["image_url"]

    item["last_checked"] = now

    # If we got a price, update price fields
    new_price = product.get("price")
    if new_price:
        old_price = float(item.get("current_price", 0) or 0)
        record_low = float(item.get("record_low_price", new_price) or new_price)
        record_high = float(item.get("record_high_price", new_price) or new_price)

        price_changed = abs(new_price - old_price) >= 0.005  # changed by at least 1 cent
        is_record_low = new_price <= record_low
        is_drop = old_price > 0 and new_price < old_price

        update_expr += ", current_price = :cp, previous_price = :pp, is_record_low = :rl"
        expr_vals[":cp"] = Decimal(str(round(new_price, 2)))
        expr_vals[":pp"] = Decimal(str(round(old_price, 2))) if old_price else Decimal("0")
        expr_vals[":rl"] = is_record_low

        if is_record_low:
            update_expr += ", record_low_price = :rlp, record_low_date = :rld"
            expr_vals[":rlp"] = Decimal(str(round(new_price, 2)))
            expr_vals[":rld"] = now
            item["record_low_price"] = Decimal(str(round(new_price, 2)))
            item["record_low_date"] = now

        if new_price > record_high:
            update_expr += ", record_high_price = :rhp"
            expr_vals[":rhp"] = Decimal(str(round(new_price, 2)))
            item["record_high_price"] = Decimal(str(round(new_price, 2)))

        item["current_price"] = Decimal(str(round(new_price, 2)))
        item["previous_price"] = Decimal(str(round(old_price, 2))) if old_price else Decimal("0")
        item["is_record_low"] = is_record_low

        # Only add a new price record when the price actually changed
        if price_changed or old_price == 0:
            _record_price(store, item_id, new_price, now)

        # Send notification if price dropped
        if is_drop and item.get("notify_enabled") and item.get("notify_email"):
            _send_price_drop_alert(item, old_price, new_price, is_record_low)

    # Write to DynamoDB
    if table:
        try:
            table.update_item(
                Key={"user_id": item["user_id"], "item_key": item["item_key"]},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_vals,
            )
        except Exception as exc:
            log.warning("Failed to update item %s: %s", item.get("item_key"), exc)

    item["_price_found"] = bool(new_price)

    # Also check alternate URLs if any
    alt_urls_raw = item.get("alt_urls", "[]")
    try:
        alt_urls = json.loads(alt_urls_raw) if isinstance(alt_urls_raw, str) else alt_urls_raw
    except Exception:
        alt_urls = []

    if alt_urls:
        alt_updated = False
        for au in alt_urls:
            try:
                au_product = fetch_product(au.get("url", ""), au.get("store", ""))
                au_now = _now_ts()
                au["last_checked"] = au_now
                if au_product:
                    if au_product.get("title"):
                        au["title"] = au_product["title"][:300]
                    if au_product.get("price"):
                        old_alt = au.get("price") or 0
                        new_alt = au_product["price"]
                        price_changed = abs(new_alt - (old_alt or 0)) >= 0.005
                        au["price"] = new_alt
                        if price_changed or not old_alt:
                            _record_price(au["store"], au["item_id"], new_alt, au_now)
                alt_updated = True
                time.sleep(2)
            except Exception as exc:
                log.warning("Alt URL check failed for %s/%s: %s", au.get("store"), au.get("item_id"), exc)

        if alt_updated and table:
            try:
                table.update_item(
                    Key={"user_id": item["user_id"], "item_key": item["item_key"]},
                    UpdateExpression="SET alt_urls = :au",
                    ExpressionAttributeValues={":au": json.dumps(alt_urls, default=str)},
                )
            except Exception:
                pass

    return item


# ---------------------------------------------------------------------------
# Email notifications via AWS SES
# ---------------------------------------------------------------------------

def _send_price_drop_alert(item: dict, old_price: float, new_price: float, is_record_low: bool) -> None:
    """Send HTML email via AWS SES when price drops."""
    ses_from = os.environ.get("SES_FROM_EMAIL")
    if not ses_from:
        log.warning("SES_FROM_EMAIL not configured, skipping price alert")
        return

    email = item.get("notify_email", "")
    if not email:
        return

    try:
        import boto3
        from botocore.config import Config
        ses = boto3.client(
            "ses",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
        )
    except Exception as exc:
        log.warning("Cannot create SES client: %s", exc)
        return

    store_name = STORE_NAMES.get(item.get("store", ""), "Store")
    drop_pct = ((old_price - new_price) / old_price * 100) if old_price > 0 else 0
    title = item.get("title", "Product")[:100]
    item_url = item.get("item_url", "#")
    image_url = item.get("image_url", "")

    record_badge = ""
    if is_record_low:
        record_badge = """
        <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px;margin:16px 0;text-align:center">
          <span style="font-size:24px">&#x1f3c6;</span>
          <strong style="color:#b45309;font-size:16px"> RECORD LOW PRICE!</strong>
        </div>"""

    subject = f"{'🏆 Record Low! ' if is_record_low else ''}Price Drop: {title[:60]}"

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<div style="max-width:560px;margin:24px auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0">
  <div style="background:linear-gradient(135deg,#f59e0b,#fb923c);padding:20px;text-align:center">
    <h1 style="margin:0;color:#fff;font-size:20px">&#x1f3f7; yTracker Price Alert</h1>
  </div>
  <div style="padding:24px">
    {record_badge}
    <div style="display:flex;gap:16px;margin-bottom:20px">
      {'<img src="' + image_url + '" alt="" style="width:80px;height:80px;object-fit:contain;border-radius:8px;border:1px solid #e2e8f0">' if image_url else ''}
      <div>
        <h2 style="margin:0 0 4px;font-size:15px;color:#1e293b">{title}</h2>
        <p style="margin:0;font-size:12px;color:#64748b">{store_name}</p>
      </div>
    </div>
    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:12px;color:#64748b;text-transform:uppercase">Was</div>
          <div style="font-size:20px;font-weight:600;color:#94a3b8;text-decoration:line-through">${old_price:.2f}</div>
        </div>
        <div style="font-size:24px;color:#16a34a">&#x2192;</div>
        <div>
          <div style="font-size:12px;color:#64748b;text-transform:uppercase">Now</div>
          <div style="font-size:24px;font-weight:700;color:#16a34a">${new_price:.2f}</div>
        </div>
        <div style="background:#dcfce7;border-radius:20px;padding:4px 12px">
          <span style="font-size:14px;font-weight:600;color:#16a34a">-{drop_pct:.1f}%</span>
        </div>
      </div>
    </div>
    <a href="{item_url}" style="display:block;text-align:center;background:#f59e0b;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">
      View on {store_name} &#x2192;
    </a>
  </div>
  <div style="background:#f8fafc;padding:12px 24px;text-align:center;border-top:1px solid #e2e8f0">
    <p style="margin:0;font-size:11px;color:#94a3b8">Sent by yTracker &middot; <a href="https://tracker.li-family.us" style="color:#64748b">tracker.li-family.us</a></p>
  </div>
</div>
</body></html>"""

    text_body = f"""yTracker Price Alert
{'🏆 RECORD LOW! ' if is_record_low else ''}{title}
{store_name}

Price dropped from ${old_price:.2f} to ${new_price:.2f} (-{drop_pct:.1f}%)

View: {item_url}"""

    try:
        ses.send_email(
            Source=ses_from,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        log.info("Price alert sent to %s for %s (%s)", email, item.get("title", ""), store_name)
    except Exception as exc:
        log.warning("SES send failed for %s: %s", email, exc)


# ---------------------------------------------------------------------------
# Background price checker (daemon thread)
# ---------------------------------------------------------------------------

_PRICE_CHECK_INTERVAL = 1 * 3600  # 1 hour
_checker_started = False


def _price_checker_loop() -> None:
    """Background daemon: checks all tracked items every hour."""
    log.info("Price checker thread started (interval: %dm)", _PRICE_CHECK_INTERVAL // 60)
    time.sleep(60)  # Wait 1 minute after startup before first check

    while True:
        try:
            table = _get_items_table()
            if table:
                try:
                    resp = table.scan()
                    items = resp.get("Items", [])
                    log.info("Price checker: checking %d items", len(items))

                    for item in items:
                        store = item.get("store", "?")
                        item_id = item.get("item_id", "?")
                        store_name = STORE_NAMES.get(store, store)
                        try:
                            log.info("Checking [%s] %s ...", store_name, item.get("title", item_id)[:50])
                            result = _check_single_item(item)
                            if result and result.get("_price_found"):
                                log.info("  [%s] %s -> $%.2f", store_name, item_id,
                                         float(result.get("current_price", 0)))
                            elif result:
                                log.info("  [%s] %s -> price not available", store_name, item_id)
                            else:
                                log.warning("  [%s] %s -> fetch failed", store_name, item_id)
                        except Exception as exc:
                            log.warning("  [%s] %s -> error: %s", store_name, item_id, exc)
                        time.sleep(3)  # Rate limit between items

                except Exception as exc:
                    log.exception("Price checker scan failed: %s", exc)
        except Exception as exc:
            log.exception("Price checker loop error: %s", exc)

        time.sleep(_PRICE_CHECK_INTERVAL)


def _start_price_checker() -> None:
    """Start the background price checker thread (once only)."""
    global _checker_started
    if _checker_started:
        return
    _checker_started = True
    t = threading.Thread(target=_price_checker_loop, daemon=True, name="price-checker")
    t.start()
    log.info("Price checker daemon started (interval: %dh)", _PRICE_CHECK_INTERVAL // 3600)
