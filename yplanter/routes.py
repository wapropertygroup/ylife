"""
yplanter.routes
~~~~~~~~~~~~~~~
URL routes for the yPlanter gardening guide.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

from flask import (
    Blueprint, render_template, request, jsonify, Response, session,
)

from yplanter.plants_db import (
    VEGETABLES, HOUSEPLANTS, YARD_SUGGESTIONS, PLANTING_CALENDAR,
    RESOURCES, ALL_PLANTS, CATEGORIES, DIFFICULTIES,
    search_all, get_plant,
)

bp = Blueprint("planter", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DynamoDB helpers (lazy init with backoff, same pattern as yPlanner)
# ---------------------------------------------------------------------------
_HISTORY_TABLE_NAME = "yplanter-history"
_history_table = None
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


def _get_history_table():
    global _history_table
    if _history_table is not None:
        return _history_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_HISTORY_TABLE_NAME)
        table.load()
        _history_table = table
        return _history_table
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _HISTORY_TABLE_NAME, exc)
        return None


def _get_session_id() -> str:
    """Get or create a persistent session ID for history tracking."""
    sid = session.get("session_id")
    if not sid:
        sid = str(uuid.uuid4())[:12]
        session["session_id"] = sid
    return sid


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "")
    diff = request.args.get("difficulty", "")

    plants = search_all(query=q, category=cat, difficulty=diff)
    return render_template(
        "index.html",
        plants=plants,
        vegetables=VEGETABLES,
        houseplants=HOUSEPLANTS,
        yard_suggestions=YARD_SUGGESTIONS,
        calendar=PLANTING_CALENDAR,
        resources=RESOURCES,
        categories=CATEGORIES,
        difficulties=DIFFICULTIES,
        query=q,
        selected_category=cat,
        selected_difficulty=diff,
    )


@bp.route("/plant/<plant_id>")
def plant_detail(plant_id: str):
    plant = get_plant(plant_id)
    if not plant:
        return render_template("404.html"), 404
    return render_template("plant_detail.html", plant=plant)


@bp.route("/calendar")
def calendar():
    return render_template("calendar.html", calendar=PLANTING_CALENDAR)


@bp.route("/yard")
def yard():
    return render_template("yard.html", suggestions=YARD_SUGGESTIONS)


@bp.route("/resources")
def resources_page():
    return render_template("resources.html", resources=RESOURCES)


@bp.route("/history")
def history_page():
    return render_template("history.html")


# ---------------------------------------------------------------------------
# API: Search
# ---------------------------------------------------------------------------

@bp.route("/api/plants")
def api_plants():
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "")
    diff = request.args.get("difficulty", "")
    results = search_all(query=q, category=cat, difficulty=diff)
    return jsonify({"plants": results})


# ---------------------------------------------------------------------------
# API: Gemini AI — ask about a specific plant (SSE streaming)
# ---------------------------------------------------------------------------

@bp.route("/api/plant/<plant_id>/ask", methods=["POST"])
def api_plant_ask(plant_id: str):
    """Stream a Gemini response about a specific plant via SSE."""
    from google import genai

    plant = get_plant(plant_id)
    if not plant:
        return jsonify({"error": "Plant not found"}), 404

    body = request.get_json(force=True, silent=True) or {}
    question = body.get("question", "").strip()
    lang = body.get("lang", "en")
    if not question:
        return jsonify({"error": "No question provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    plant_context = f"""Plant: {plant['name']}
Category: {plant.get('category', 'unknown')}
Difficulty: {plant.get('difficulty', 'unknown')}
PNW Notes: {plant.get('pnw_notes', 'N/A')}
Light: {plant.get('sun') or plant.get('light', 'N/A')}
Water: {plant.get('water', 'N/A')}
Soil: {plant.get('soil', 'N/A')}
Tip: {plant.get('tip', 'N/A')}"""

    lang_instruction = "Answer in Chinese (简体中文)." if lang == "zh" else "Answer in English."

    prompt = f"""You are a knowledgeable Pacific Northwest gardener and plant expert,
specializing in Seattle / USDA Zone 8b gardening. Answer the user's question about this plant.
Be practical, specific to the PNW climate, and concise (2-4 paragraphs max).

{plant_context}

User's question: {question}

{lang_instruction}"""

    client = genai.Client(api_key=api_key)
    session_id = _get_session_id()

    def generate():
        accumulated = []
        try:
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash", contents=prompt
            )
            for chunk in stream:
                text = chunk.text
                if text:
                    accumulated.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            log.exception("Gemini plant ask failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            _save_history(session_id, plant_id, plant["name"], question,
                          "".join(accumulated), lang)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# API: Gemini AI — general gardening question (SSE streaming)
# ---------------------------------------------------------------------------

@bp.route("/api/ask", methods=["POST"])
def api_ask():
    """Stream a Gemini response for a general gardening question via SSE."""
    from google import genai

    body = request.get_json(force=True, silent=True) or {}
    question = body.get("question", "").strip()
    lang = body.get("lang", "en")
    if not question:
        return jsonify({"error": "No question provided"}), 400

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    lang_instruction = "Answer in Chinese (简体中文)." if lang == "zh" else "Answer in English."

    prompt = f"""You are a knowledgeable Pacific Northwest gardener and plant expert,
specializing in Seattle / USDA Zone 8b gardening. You know about vegetables, herbs, fruit,
houseplants, yard design, soil, composting, pests, and seasonal timing for the PNW.

Answer the user's gardening question. Be practical, specific to Seattle's climate
(mild wet winters, dry summers, Zone 8b), and concise (2-4 paragraphs max).
If relevant, mention specific varieties that do well in the PNW.

User's question: {question}

{lang_instruction}"""

    client = genai.Client(api_key=api_key)
    session_id = _get_session_id()

    def generate():
        accumulated = []
        try:
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash", contents=prompt
            )
            for chunk in stream:
                text = chunk.text
                if text:
                    accumulated.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            log.exception("Gemini ask failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            _save_history(session_id, None, None, question,
                          "".join(accumulated), lang)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# API: Chat history (DynamoDB)
# ---------------------------------------------------------------------------

def _save_history(session_id: str, plant_id: str | None, plant_name: str | None,
                  question: str, answer: str, lang: str) -> None:
    """Save a Q&A exchange to DynamoDB."""
    table = _get_history_table()
    if not table or not answer:
        return
    try:
        item = {
            "session_id": session_id,
            "timestamp": int(time.time() * 1000),
            "question": question,
            "answer": answer,
            "lang": lang,
        }
        if plant_id:
            item["plant_id"] = plant_id
            item["plant_name"] = plant_name
        table.put_item(Item=item)
    except Exception as exc:
        log.warning("Failed to save history: %s", exc)


@bp.route("/api/history")
def api_history():
    """List chat history for the current session."""
    table = _get_history_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    try:
        from boto3.dynamodb.conditions import Key
        resp = table.query(
            KeyConditionExpression=Key("session_id").eq(session_id),
            ScanIndexForward=False,
            Limit=50,
        )
        items = []
        for item in resp.get("Items", []):
            items.append({
                "timestamp": int(item["timestamp"]),
                "question": item.get("question", ""),
                "answer": item.get("answer", ""),
                "plant_id": item.get("plant_id"),
                "plant_name": item.get("plant_name"),
                "lang": item.get("lang", "en"),
            })
        return jsonify({"history": items})
    except Exception as exc:
        log.exception("Failed to load history")
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/history/<int:timestamp>", methods=["DELETE"])
def api_delete_history(timestamp: int):
    """Delete a single history item."""
    table = _get_history_table()
    if not table:
        return jsonify({"error": "Database unavailable"}), 503

    session_id = _get_session_id()
    try:
        table.delete_item(Key={"session_id": session_id, "timestamp": timestamp})
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Failed to delete history")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Collection export (kept for compatibility)
# ---------------------------------------------------------------------------

@bp.route("/api/collection/export")
def api_collection_export():
    ids = request.args.get("ids", "")
    if not ids:
        return jsonify({"plants": []})
    plant_ids = [i.strip() for i in ids.split(",") if i.strip()]
    plants = [p for p in ALL_PLANTS if p["id"] in plant_ids]
    return jsonify({"plants": plants})
