"""
yplanner.routes
~~~~~~~~~~~~~~~
URL routes for the trip planner.
"""
from __future__ import annotations

import json
import logging
import os
import time
import string
import random

from flask import Blueprint, render_template, request, jsonify, session

bp = Blueprint("planner", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
_USERS_TABLE_NAME = "yplanner-users"
_users_table = None

# ---------------------------------------------------------------------------
# DynamoDB helpers (lazy init, same pattern as ystocker)
# ---------------------------------------------------------------------------
_TRIPS_TABLE_NAME = "yplanner-trips"
_SHARED_TABLE_NAME = "yplanner-shared-trips"
_trips_table = None
_shared_table = None
_dynamo_unavail_until = 0.0
_DYNAMO_BACKOFF = 300  # 5 min backoff on connection failure


def _get_dynamodb():
    """Get boto3 DynamoDB resource (lazy init with backoff)."""
    global _dynamo_unavail_until
    if time.time() < _dynamo_unavail_until:
        return None
    try:
        import boto3
        return boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    except Exception as exc:
        log.warning("DynamoDB unavailable: %s", exc)
        _dynamo_unavail_until = time.time() + _DYNAMO_BACKOFF
        return None


def _get_trips_table():
    """Get yplanner-trips table (lazy init)."""
    global _trips_table
    if _trips_table is not None:
        return _trips_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_TRIPS_TABLE_NAME)
        table.load()
        _trips_table = table
        return _trips_table
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _TRIPS_TABLE_NAME, exc)
        return None


def _get_shared_table():
    """Get yplanner-shared-trips table (lazy init)."""
    global _shared_table
    if _shared_table is not None:
        return _shared_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_SHARED_TABLE_NAME)
        table.load()
        _shared_table = table
        return _shared_table
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _SHARED_TABLE_NAME, exc)
        return None


def _gen_id(length=8):
    """Generate a short random ID."""
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=length))


def _get_users_table():
    """Get yplanner-users table (lazy init)."""
    global _users_table
    if _users_table is not None:
        return _users_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_USERS_TABLE_NAME)
        table.load()
        _users_table = table
        return _users_table
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _USERS_TABLE_NAME, exc)
        return None


def _upsert_user(email: str, name: str, picture: str, provider: str) -> None:
    """Create or update user in DynamoDB."""
    table = _get_users_table()
    if not table:
        return
    try:
        table.update_item(
            Key={"email": email},
            UpdateExpression="SET #n = :name, picture = :pic, provider = :prov, last_login = :now",
            ConditionExpression="attribute_exists(email)",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={
                ":name": name,
                ":pic": picture,
                ":prov": provider,
                ":now": int(time.time()),
            },
        )
    except Exception:
        # User doesn't exist yet — create
        try:
            table.put_item(Item={
                "email": email,
                "name": name,
                "picture": picture,
                "provider": provider,
                "created_at": int(time.time()),
                "last_login": int(time.time()),
            })
        except Exception as exc:
            log.warning("Failed to create user %s: %s", email, exc)


def _get_session_user():
    """Get current authenticated user from session, or None."""
    email = session.get("user_email")
    if not email:
        return None
    return {
        "email": email,
        "name": session.get("user_name", ""),
        "picture": session.get("user_picture", ""),
        "provider": session.get("user_provider", ""),
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@bp.route("/api/auth/google", methods=["POST"])
def auth_google():
    """Verify Google ID token and create session."""
    data = request.get_json()
    credential = data.get("credential", "") if data else ""
    if not credential:
        return jsonify({"error": "No credential provided"}), 400

    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not google_client_id:
        return jsonify({"error": "Google sign-in not configured"}), 503

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        idinfo = id_token.verify_oauth2_token(
            credential, google_requests.Request(), google_client_id
        )

        email = idinfo.get("email", "")
        name = idinfo.get("name", email.split("@")[0])
        picture = idinfo.get("picture", "")

        if not email:
            return jsonify({"error": "No email in token"}), 400

        # Store in session
        session["user_email"] = email
        session["user_name"] = name
        session["user_picture"] = picture
        session["user_provider"] = "google"

        # Upsert in DynamoDB
        _upsert_user(email, name, picture, "google")

        return jsonify({
            "ok": True,
            "user": {"email": email, "name": name, "picture": picture, "provider": "google"},
        })
    except ValueError as exc:
        log.warning("Google token verification failed: %s", exc)
        return jsonify({"error": "Invalid Google token"}), 401
    except Exception as exc:
        log.exception("Google auth error")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/auth/apple", methods=["POST"])
def auth_apple():
    """Verify Apple ID token and create session."""
    data = request.get_json()
    id_token_str = data.get("id_token", "") if data else ""
    if not id_token_str:
        return jsonify({"error": "No id_token provided"}), 400

    try:
        import jwt
        import requests as http_requests

        # Fetch Apple's public keys
        apple_keys_resp = http_requests.get("https://appleid.apple.com/auth/keys", timeout=10)
        apple_keys = apple_keys_resp.json()

        # Decode header to find the right key
        header = jwt.get_unverified_header(id_token_str)
        kid = header.get("kid")

        # Find matching key
        key_data = None
        for key in apple_keys.get("keys", []):
            if key["kid"] == kid:
                key_data = key
                break

        if not key_data:
            return jsonify({"error": "Apple key not found"}), 401

        # Build public key and verify
        from jwt.algorithms import RSAAlgorithm
        public_key = RSAAlgorithm.from_jwk(key_data)

        apple_service_id = os.environ.get("APPLE_SERVICE_ID", "")
        claims = jwt.decode(
            id_token_str,
            public_key,
            algorithms=["RS256"],
            audience=apple_service_id if apple_service_id else None,
            issuer="https://appleid.apple.com",
            options={"verify_aud": bool(apple_service_id)},
        )

        email = claims.get("email", "")
        # Apple only sends user info on first sign-in
        user_data = data.get("user", {})
        name = ""
        if user_data:
            first = user_data.get("name", {}).get("firstName", "")
            last = user_data.get("name", {}).get("lastName", "")
            name = f"{first} {last}".strip()

        if not email:
            return jsonify({"error": "No email in token"}), 400

        # If no name from Apple, try to get from existing DB record
        if not name:
            table = _get_users_table()
            if table:
                try:
                    resp = table.get_item(Key={"email": email})
                    item = resp.get("Item")
                    if item:
                        name = item.get("name", email.split("@")[0])
                except Exception:
                    pass
            if not name:
                name = email.split("@")[0]

        session["user_email"] = email
        session["user_name"] = name
        session["user_picture"] = ""
        session["user_provider"] = "apple"

        _upsert_user(email, name, "", "apple")

        return jsonify({
            "ok": True,
            "user": {"email": email, "name": name, "picture": "", "provider": "apple"},
        })
    except Exception as exc:
        log.exception("Apple auth error")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/auth/me")
def auth_me():
    """Return current session user."""
    user = _get_session_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"user": user})


@bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Clear session."""
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    """Main trip planner page."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    apple_service_id = os.environ.get("APPLE_SERVICE_ID", "")
    return render_template("planner.html",
                           google_maps_api_key=api_key,
                           google_client_id=google_client_id,
                           apple_service_id=apple_service_id)


@bp.route("/trip/<trip_id>")
def shared_trip(trip_id: str):
    """View a shared trip."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    apple_service_id = os.environ.get("APPLE_SERVICE_ID", "")
    return render_template("planner.html",
                           google_maps_api_key=api_key,
                           google_client_id=google_client_id,
                           apple_service_id=apple_service_id,
                           shared_trip_id=trip_id)


# ---------------------------------------------------------------------------
# API: Save/Load trips (per-user)
# ---------------------------------------------------------------------------

@bp.route("/api/trip/save", methods=["POST"])
def api_save_trip():
    """Save a trip for a user."""
    table = _get_trips_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    username = data.get("username", "").strip()
    # Fall back to session user if no username in body
    if not username:
        user = _get_session_user()
        if user:
            username = user["email"]
    trip_name = data.get("name", "").strip()
    stops = data.get("stops", [])
    travel_mode = data.get("travelMode", "DRIVING")
    departure_time = data.get("departureTime", "")

    if not username:
        return jsonify({"error": "Username required"}), 400
    if not trip_name:
        return jsonify({"error": "Trip name required"}), 400
    if not stops:
        return jsonify({"error": "At least one stop required"}), 400

    trip_id = data.get("trip_id") or f"{int(time.time() * 1000)}-{_gen_id(4)}"

    try:
        table.put_item(Item={
            "username": username,
            "trip_id": trip_id,
            "name": trip_name,
            "stops": json.dumps(stops),
            "travelMode": travel_mode,
            "departureTime": departure_time,
            "updated_at": int(time.time()),
        })
        return jsonify({"ok": True, "trip_id": trip_id})
    except Exception as exc:
        log.exception("Failed to save trip")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/trips/<username>")
def api_list_trips(username: str):
    """List all trips for a user."""
    table = _get_trips_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        from boto3.dynamodb.conditions import Key
        resp = table.query(KeyConditionExpression=Key("username").eq(username))
        trips = []
        for item in resp.get("Items", []):
            trips.append({
                "trip_id": item["trip_id"],
                "name": item.get("name", "Untitled"),
                "travelMode": item.get("travelMode", "DRIVING"),
                "stops_count": len(json.loads(item.get("stops", "[]"))),
                "updated_at": int(item.get("updated_at", 0)),
            })
        # Sort by most recent
        trips.sort(key=lambda t: t["updated_at"], reverse=True)
        return jsonify({"trips": trips})
    except Exception as exc:
        log.exception("Failed to list trips")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/trip/<username>/<trip_id>")
def api_get_trip(username: str, trip_id: str):
    """Get a specific trip."""
    table = _get_trips_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        resp = table.get_item(Key={"username": username, "trip_id": trip_id})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Trip not found"}), 404
        return jsonify({
            "trip_id": item["trip_id"],
            "name": item.get("name", "Untitled"),
            "stops": json.loads(item.get("stops", "[]")),
            "travelMode": item.get("travelMode", "DRIVING"),
            "departureTime": item.get("departureTime", ""),
            "updated_at": int(item.get("updated_at", 0)),
        })
    except Exception as exc:
        log.exception("Failed to get trip")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/trip/<username>/<trip_id>", methods=["DELETE"])
def api_delete_trip(username: str, trip_id: str):
    """Delete a saved trip."""
    table = _get_trips_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        table.delete_item(Key={"username": username, "trip_id": trip_id})
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Failed to delete trip")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Share trips
# ---------------------------------------------------------------------------

@bp.route("/api/trip/share", methods=["POST"])
def api_share_trip():
    """Create a shareable trip link."""
    table = _get_shared_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    stops = data.get("stops", [])
    if not stops:
        return jsonify({"error": "At least one stop required"}), 400

    trip_id = _gen_id(8)
    ttl = int(time.time()) + (30 * 24 * 60 * 60)  # 30 days

    try:
        table.put_item(Item={
            "trip_id": trip_id,
            "stops": json.dumps(stops),
            "travelMode": data.get("travelMode", "DRIVING"),
            "departureTime": data.get("departureTime", ""),
            "created_by": data.get("username", "anonymous"),
            "created_at": int(time.time()),
            "ttl": ttl,
        })
        return jsonify({"ok": True, "trip_id": trip_id})
    except Exception as exc:
        log.exception("Failed to share trip")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/shared/<trip_id>")
def api_get_shared_trip(trip_id: str):
    """Get a shared trip by ID."""
    table = _get_shared_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        resp = table.get_item(Key={"trip_id": trip_id})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Shared trip not found"}), 404
        return jsonify({
            "trip_id": item["trip_id"],
            "stops": json.loads(item.get("stops", "[]")),
            "travelMode": item.get("travelMode", "DRIVING"),
            "departureTime": item.get("departureTime", ""),
            "created_by": item.get("created_by", "anonymous"),
            "created_at": int(item.get("created_at", 0)),
        })
    except Exception as exc:
        log.exception("Failed to get shared trip")
        return jsonify({"error": str(exc)}), 500
