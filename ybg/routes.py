"""
ybg.routes
~~~~~~~~~~
URL routes for the yBG tenant background check app.

Flow:
  1. Landlord creates an application link from the dashboard
  2. Tenant opens the link, fills out the application form
  3. App runs background check via Checkr API
  4. Landlord reviews results on the dashboard

Checkr API docs: https://docs.checkr.com/
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import requests as http_requests
from flask import (
    Blueprint, render_template, request, jsonify, redirect, session, url_for,
)

bp = Blueprint("bg", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Checkr API client
# ---------------------------------------------------------------------------

CHECKR_API_BASE = "https://api.checkr.com/v1"
CHECKR_STAGING_BASE = "https://api.checkr-staging.com/v1"


def _checkr_base() -> str:
    """Return Checkr API base URL (staging or prod based on key prefix)."""
    key = os.environ.get("CHECKR_API_KEY", "")
    if key.startswith("test_") or not key:
        return CHECKR_STAGING_BASE
    return CHECKR_API_BASE


def _checkr_request(method: str, path: str, **kwargs) -> dict | None:
    """Make an authenticated Checkr API request."""
    api_key = os.environ.get("CHECKR_API_KEY", "")
    if not api_key:
        log.warning("CHECKR_API_KEY not configured")
        return None

    url = f"{_checkr_base()}{path}"
    try:
        resp = http_requests.request(
            method, url,
            auth=(api_key, ""),
            timeout=30,
            **kwargs,
        )
        log.info("Checkr %s %s → %d", method, path, resp.status_code)
        if resp.status_code in (200, 201):
            return resp.json()
        log.warning("Checkr error: %d — %s", resp.status_code, resp.text[:300])
        return {"error": resp.text, "status_code": resp.status_code}
    except Exception as exc:
        log.exception("Checkr request failed: %s %s", method, path)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

_APPS_TABLE = "ybg-applications"
_apps_table = None
_dynamo_unavail_until = 0.0


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
        _dynamo_unavail_until = time.time() + 300
        return None


def _get_apps_table():
    global _apps_table
    if _apps_table is not None:
        return _apps_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_APPS_TABLE)
        table.load()
        _apps_table = table
        return _apps_table
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


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Auth helpers (simple admin login)
# ---------------------------------------------------------------------------

def _is_admin() -> bool:
    """Check if current session is an authenticated admin."""
    return bool(session.get("admin_email"))


def _require_admin():
    """Return redirect response if not admin, else None."""
    if not _is_admin():
        return redirect("/login")
    return None


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    """Landlord dashboard — list all applications."""
    if not _is_admin():
        return redirect("/login")

    checkr_configured = bool(os.environ.get("CHECKR_API_KEY"))
    return render_template("index.html", checkr_configured=checkr_configured)


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Admin login page."""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password", "")

        # Simple auth: check against env var or allow any email in dev
        admin_email = os.environ.get("YBG_ADMIN_EMAIL", "")
        admin_pass = os.environ.get("YBG_ADMIN_PASSWORD", "admin")

        if admin_email and email != admin_email:
            return render_template("login.html", error="Access denied")
        if password != admin_pass:
            return render_template("login.html", error="Invalid password")

        session["admin_email"] = email
        return redirect("/")

    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.pop("admin_email", None)
    return redirect("/login")


@bp.route("/apply/<token>")
def apply_form(token: str):
    """Tenant application form — accessed via unique link."""
    return render_template("apply.html", token=token)


@bp.route("/status/<app_id>")
def status_page(app_id: str):
    """Application status page for the tenant."""
    return render_template("status.html", app_id=app_id)


@bp.route("/review/<app_id>")
def review_page(app_id: str):
    """Detailed review page for the landlord."""
    guard = _require_admin()
    if guard:
        return guard
    return render_template("review.html", app_id=app_id)


# ---------------------------------------------------------------------------
# API: Application management
# ---------------------------------------------------------------------------

@bp.route("/api/applications")
def api_list_applications():
    """List all applications (admin only)."""
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    table = _get_apps_table()
    if not table:
        return jsonify({"applications": []})

    try:
        resp = table.scan()
        apps = _decimal_to_float(resp.get("Items", []))
        apps.sort(key=lambda a: a.get("created_at", 0), reverse=True)
        return jsonify({"applications": apps})
    except Exception as exc:
        return jsonify({"applications": [], "error": str(exc)})


@bp.route("/api/application/<app_id>")
def api_get_application(app_id: str):
    """Get a single application."""
    table = _get_apps_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        resp = table.get_item(Key={"app_id": app_id})
        item = resp.get("Item")
        if not item:
            return jsonify({"error": "Application not found"}), 404
        return jsonify({"application": _decimal_to_float(item)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/invite", methods=["POST"])
def api_create_invite():
    """Create a new application invite link (admin only).

    Body: {"property_address": "...", "rent": 2500, "notes": "..."}
    Returns: {"token": "...", "apply_url": "..."}
    """
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    address = body.get("property_address", "").strip()
    rent = body.get("rent", 0)
    notes = body.get("notes", "")

    if not address:
        return jsonify({"error": "Property address is required"}), 400

    token = secrets.token_urlsafe(16)
    app_id = str(uuid.uuid4())[:8]

    table = _get_apps_table()
    if table:
        try:
            table.put_item(Item={
                "app_id": app_id,
                "token": token,
                "property_address": address,
                "rent": Decimal(str(rent)) if rent else Decimal("0"),
                "notes": notes,
                "status": "invited",
                "created_at": _now_ms(),
                "created_by": session.get("admin_email", ""),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    base = request.host_url.rstrip("/")
    return jsonify({
        "app_id": app_id,
        "token": token,
        "apply_url": f"{base}/apply/{token}",
    }), 201


@bp.route("/api/apply/<token>", methods=["POST"])
def api_submit_application(token: str):
    """Submit a tenant application.

    Body: {
      "first_name", "last_name", "email", "phone", "dob",
      "ssn_last4", "current_address", "employer", "annual_income",
      "move_in_date", "num_occupants", "pets", "references": [...],
      "consent": true
    }
    """
    body = request.get_json(force=True, silent=True) or {}

    # Validate required fields
    required = ["first_name", "last_name", "email", "phone", "dob", "consent"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    if not body.get("consent"):
        return jsonify({"error": "You must consent to the background check"}), 400

    # Find the application by token
    table = _get_apps_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        # Scan for the token (small table, fine for family use)
        from boto3.dynamodb.conditions import Attr
        resp = table.scan(FilterExpression=Attr("token").eq(token))
        items = resp.get("Items", [])
        if not items:
            return jsonify({"error": "Invalid application link"}), 404

        app = items[0]
        app_id = app["app_id"]

        if app.get("status") not in ("invited", "started"):
            return jsonify({"error": "This application has already been submitted"}), 409

        # Update the application with tenant info
        now = _now_ms()
        update_expr = """SET #s = :s, first_name = :fn, last_name = :ln,
                         email = :em, phone = :ph, dob = :dob,
                         ssn_last4 = :ssn, current_address = :addr,
                         employer = :emp, annual_income = :inc,
                         move_in_date = :mid, num_occupants = :occ,
                         pets = :pets, references_json = :refs,
                         submitted_at = :sub, consent = :con"""
        expr_vals = {
            ":s": "submitted",
            ":fn": body["first_name"][:100],
            ":ln": body["last_name"][:100],
            ":em": body["email"][:200],
            ":ph": body["phone"][:20],
            ":dob": body["dob"],
            ":ssn": body.get("ssn_last4", "")[:4],
            ":addr": body.get("current_address", "")[:500],
            ":emp": body.get("employer", "")[:200],
            ":inc": Decimal(str(body.get("annual_income", 0) or 0)),
            ":mid": body.get("move_in_date", ""),
            ":occ": int(body.get("num_occupants", 1) or 1),
            ":pets": body.get("pets", "None"),
            ":refs": json.dumps(body.get("references", [])[:3]),
            ":sub": now,
            ":con": True,
        }

        table.update_item(
            Key={"app_id": app_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=expr_vals,
        )

        # Trigger background check if Checkr is configured
        bg_result = _run_background_check(app_id, body)

        # Send notification email to landlord
        _notify_landlord(app_id, body)

        return jsonify({
            "app_id": app_id,
            "status": "submitted",
            "background_check": bg_result,
            "message": "Application submitted successfully. The landlord will review it shortly.",
        })

    except Exception as exc:
        log.exception("Failed to submit application")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Background check (Checkr)
# ---------------------------------------------------------------------------

@bp.route("/api/application/<app_id>/check", methods=["POST"])
def api_run_check(app_id: str):
    """Manually trigger a background check for an application (admin only)."""
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    table = _get_apps_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        resp = table.get_item(Key={"app_id": app_id})
        app = resp.get("Item")
        if not app:
            return jsonify({"error": "Application not found"}), 404

        result = _run_background_check(app_id, app)
        return jsonify({"result": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/application/<app_id>/check-status")
def api_check_status(app_id: str):
    """Get the latest background check status from Checkr."""
    table = _get_apps_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        resp = table.get_item(Key={"app_id": app_id})
        app = resp.get("Item")
        if not app:
            return jsonify({"error": "Application not found"}), 404

        report_id = app.get("checkr_report_id")
        if not report_id:
            return jsonify({"status": "no_check", "message": "No background check initiated"})

        # Fetch latest status from Checkr
        result = _checkr_request("GET", f"/reports/{report_id}")
        if result and "error" not in result:
            status = result.get("status", "unknown")
            # Update DynamoDB with latest status
            table.update_item(
                Key={"app_id": app_id},
                UpdateExpression="SET checkr_status = :s, checkr_result = :r",
                ExpressionAttributeValues={
                    ":s": status,
                    ":r": json.dumps(result, default=str)[:5000],
                },
            )
            return jsonify({
                "status": status,
                "report": _sanitize_report(result),
            })

        return jsonify({"status": app.get("checkr_status", "unknown")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/application/<app_id>/decision", methods=["POST"])
def api_set_decision(app_id: str):
    """Set landlord decision on an application. Body: {"decision": "approved|denied", "notes": "..."}"""
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    decision = body.get("decision", "")
    if decision not in ("approved", "denied", "pending"):
        return jsonify({"error": "Decision must be 'approved', 'denied', or 'pending'"}), 400

    table = _get_apps_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        table.update_item(
            Key={"app_id": app_id},
            UpdateExpression="SET #s = :s, decision = :d, decision_notes = :n, decided_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": decision,
                ":d": decision,
                ":n": body.get("notes", "")[:500],
                ":t": _now_ms(),
            },
        )
        return jsonify({"ok": True, "decision": decision})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/application/<app_id>", methods=["DELETE"])
def api_delete_application(app_id: str):
    """Delete an application (admin only)."""
    if not _is_admin():
        return jsonify({"error": "Unauthorized"}), 401

    table = _get_apps_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    try:
        table.delete_item(Key={"app_id": app_id})
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Checkr integration helpers
# ---------------------------------------------------------------------------

def _run_background_check(app_id: str, applicant: dict) -> dict:
    """Create a Checkr candidate + invitation or report."""
    api_key = os.environ.get("CHECKR_API_KEY", "")
    if not api_key:
        return {"status": "skipped", "reason": "CHECKR_API_KEY not configured"}

    # Step 1: Create candidate
    candidate = _checkr_request("POST", "/candidates", json={
        "first_name": applicant.get("first_name", ""),
        "last_name": applicant.get("last_name", ""),
        "email": applicant.get("email", ""),
        "phone": applicant.get("phone", ""),
        "dob": applicant.get("dob", ""),
        "ssn": applicant.get("ssn_last4", ""),  # Checkr accepts last 4
    })

    if not candidate or "error" in candidate:
        return {"status": "error", "detail": str(candidate)}

    candidate_id = candidate.get("id", "")

    # Step 2: Create an invitation (tenant consents via Checkr's hosted flow)
    # Using "tasker_standard" package — includes criminal, sex offender, global watchlist
    invitation = _checkr_request("POST", "/invitations", json={
        "candidate_id": candidate_id,
        "package": "tasker_standard",
    })

    # Update the application record
    table = _get_apps_table()
    if table:
        try:
            update = "SET checkr_candidate_id = :cid, checkr_status = :s"
            vals: dict = {
                ":cid": candidate_id,
                ":s": "pending",
            }
            if invitation and invitation.get("id"):
                update += ", checkr_invitation_id = :iid, checkr_invitation_url = :iurl"
                vals[":iid"] = invitation["id"]
                vals[":iurl"] = invitation.get("invitation_url", "")

            if invitation and invitation.get("report_id"):
                update += ", checkr_report_id = :rid"
                vals[":rid"] = invitation["report_id"]

            table.update_item(
                Key={"app_id": app_id},
                UpdateExpression=update,
                ExpressionAttributeValues=vals,
            )
        except Exception as exc:
            log.warning("Failed to update app %s with Checkr data: %s", app_id, exc)

    return {
        "status": "initiated",
        "candidate_id": candidate_id,
        "invitation_url": invitation.get("invitation_url", "") if invitation else "",
    }


def _sanitize_report(report: dict) -> dict:
    """Remove sensitive fields from a Checkr report before sending to frontend."""
    safe_fields = [
        "id", "status", "result", "created_at", "completed_at",
        "package", "turnaround_time",
    ]
    result = {k: report.get(k) for k in safe_fields if k in report}

    # Include summary of checks
    for check_type in ["criminal_searches", "sex_offender_searches",
                       "global_watchlist_searches", "national_criminal_searches"]:
        checks = report.get(check_type, [])
        if checks:
            result[check_type] = [{
                "status": c.get("status"),
                "result": c.get("result"),
                "state": c.get("state"),
                "county": c.get("county"),
                "records_count": len(c.get("records", [])),
            } for c in checks]

    return result


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _notify_landlord(app_id: str, applicant: dict) -> None:
    """Send email to landlord when a new application is submitted."""
    ses_from = os.environ.get("SES_FROM_EMAIL")
    admin_email = os.environ.get("YBG_ADMIN_EMAIL")
    if not ses_from or not admin_email:
        return

    try:
        import boto3
        ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        name = f"{applicant.get('first_name', '')} {applicant.get('last_name', '')}".strip()

        ses.send_email(
            Source=ses_from,
            Destination={"ToAddresses": [admin_email]},
            Message={
                "Subject": {"Data": f"New tenant application: {name}", "Charset": "UTF-8"},
                "Body": {
                    "Text": {
                        "Data": f"New application from {name}\n"
                                f"Email: {applicant.get('email', '')}\n"
                                f"Phone: {applicant.get('phone', '')}\n\n"
                                f"Review: {request.host_url}review/{app_id}",
                        "Charset": "UTF-8",
                    },
                },
            },
        )
        log.info("Notification sent to %s for app %s", admin_email, app_id)
    except Exception as exc:
        log.warning("Failed to send notification: %s", exc)
