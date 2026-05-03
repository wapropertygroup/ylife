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
_TRANSLATIONS_TABLE_NAME = "yplanter-translations"
_history_table = None
_translations_table = None
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


def _get_translations_table():
    global _translations_table
    if _translations_table is not None:
        return _translations_table
    ddb = _get_dynamodb()
    if not ddb:
        return None
    try:
        table = ddb.Table(_TRANSLATIONS_TABLE_NAME)
        table.load()
        _translations_table = table
        return _translations_table
    except Exception as exc:
        log.warning("Cannot load table %s: %s", _TRANSLATIONS_TABLE_NAME, exc)
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
    log.info("API plants: q=%s cat=%s diff=%s", q or "(all)", cat or "(all)", diff or "(all)")
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
    log.info("API plant/ask: plant=%s lang=%s q=%s", plant_id, lang, question[:80] if question else "(empty)")
    if not question: = f"""Plant: {plant['name']}
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
# API: YouTube video search
# ---------------------------------------------------------------------------

@bp.route("/api/youtube")
def api_youtube():
    """Search YouTube for gardening videos."""
    query = request.args.get("q", "").strip()
    max_results = min(int(request.args.get("max", "4")), 8)
    if not query:
        return jsonify({"videos": []})

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return jsonify({"videos": []})

    try:
        import requests as http_req
        resp = http_req.get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "key": api_key,
            "relevanceLanguage": "en",
            "safeSearch": "strict",
        }, timeout=8)
        if resp.status_code != 200:
            return jsonify({"videos": []})

        videos = []
        for item in resp.json().get("items", []):
            videos.append({
                "id": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "thumbnail": item["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
            })
        return jsonify({"videos": videos})
    except Exception as exc:
        log.warning("YouTube search failed: %s", exc)
        return jsonify({"videos": []})


# ---------------------------------------------------------------------------
# Translation helper — splits large batches for reliable Gemini output
# ---------------------------------------------------------------------------


def _translate_texts_batch(client, texts: dict) -> dict:
    """Translate texts to Chinese via Gemini, splitting large batches."""
    MAX_BATCH = 50
    items = list(texts.items())
    result = {}

    for start in range(0, len(items), MAX_BATCH):
        batch = items[start:start + MAX_BATCH]
        numbered = {i: (k, v) for i, (k, v) in enumerate(batch)}
        lines = "\n".join(f"[{i}] {v}" for i, (_, v) in numbered.items())

        prompt = f"""Translate the following English gardening/plant content to Chinese (简体中文).
Keep plant variety names, brand names, and scientific names in English.
Keep numbers, dates, and units as-is.
Return ONLY a JSON object mapping the same numbered keys to Chinese translations.
Do not add any explanation.

{lines}

Return format: {{"0": "中文翻译", "1": "中文翻译", ...}}"""

        resp = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        batch_map = json.loads(raw)

        for i_str, zh_text in batch_map.items():
            idx = int(i_str)
            if idx in numbered:
                result[numbered[idx][0]] = zh_text

    return result


# ---------------------------------------------------------------------------
# API: Gemini-powered content translation (cached in DynamoDB)
# ---------------------------------------------------------------------------

@bp.route("/api/translate", methods=["POST"])
def api_translate():
    """Translate a batch of content strings to Chinese via Gemini.

    Request:  { "page_key": "plant:tomato", "texts": {"field1": "English text", ...} }
    Response: { "translations": {"field1": "中文翻译", ...} }

    Results cached in DynamoDB so each page_key only translates once.
    """
    from google import genai

    body = request.get_json(force=True, silent=True) or {}
    page_key = body.get("page_key", "").strip()
    texts = body.get("texts", {})
    if not page_key or not texts:
        return jsonify({"error": "page_key and texts required"}), 400

    table = _get_translations_table()
    if table:
        try:
            resp = table.get_item(Key={"page_key": page_key})
            item = resp.get("Item")
            if item and item.get("translations"):
                cached = json.loads(item["translations"])
                if set(texts.keys()).issubset(set(cached.keys())):
                    return jsonify({"translations": cached})
        except Exception as exc:
            log.warning("Translation cache read failed: %s", exc)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    try:
        client = genai.Client(api_key=api_key)
        translations = _translate_texts_batch(client, texts)

        if table and translations:
            try:
                table.put_item(Item={
                    "page_key": page_key,
                    "translations": json.dumps(translations, ensure_ascii=False),
                    "updated_at": int(time.time()),
                })
            except Exception as exc:
                log.warning("Translation cache write failed: %s", exc)

        return jsonify({"translations": translations})
    except Exception as exc:
        log.exception("Translation failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Pre-warm: generate all translations ahead of time
# ---------------------------------------------------------------------------


def _build_all_page_texts():
    """Build translatable texts for every page in the app."""
    pages = {}

    # ── Index page ──
    idx = {
        'tab_veg': '\U0001f96c Vegetables', 'tab_fruit': '\U0001f353 Fruit',
        'tab_herbs': '\U0001f33f Herbs', 'tab_house': '\U0001fab4 Houseplants',
        'tab_perennial': '\U0001f338 Perennials', 'tab_annual': '\U0001f33b Annuals',
        'tab_shrub': '\U0001f33f Shrubs', 'tab_tree': '\U0001f333 Trees',
        'tab_cal': '\U0001f4c5 Calendar', 'tab_yard': '\U0001f3e1 Yard Ideas',
    }
    for plant in ALL_PLANTS:
        pid = plant['id']
        idx[f'name_{pid}'] = plant['name']
        idx[f'diff_{pid}'] = plant['difficulty'].capitalize()
        if plant.get('pnw_notes'):
            idx[f'notes_{pid}'] = plant['pnw_notes']
        if plant.get('harvest'):
            idx[f'harvest_{pid}'] = f"Harvest: {plant['harvest']}"
        elif plant.get('bloom'):
            idx[f'harvest_{pid}'] = f"Blooms: {plant['bloom']}"
        if plant.get('frost_hardy'):
            idx[f'frost_{pid}'] = (
                '\u2744\ufe0f Perennial in PNW' if plant['category'] == 'herb'
                else '\u2744\ufe0f Frost Hardy'
            )
        if 'toxic_pets' in plant and not plant.get('toxic_pets'):
            idx[f'pet_{pid}'] = '\U0001f43e Pet Safe'
    for month in PLANTING_CALENDAR[:3]:
        idx[f'month_{month["month"]}'] = month['month']
        for i, task in enumerate(month['tasks'][:4], 1):
            idx[f'cal_{month["month"]}_{i}'] = task
    for s in YARD_SUGGESTIONS[:3]:
        idx[f'yard_title_{s["id"]}'] = s['title']
        idx[f'yard_desc_{s["id"]}'] = s['description']
        for i, benefit in enumerate(s['benefits'][:2], 1):
            idx[f'yard_ben_{s["id"]}_{i}'] = benefit
    pages['index'] = idx

    # ── Calendar page ──
    cal = {}
    for month in PLANTING_CALENDAR:
        for i, task in enumerate(month['tasks'], 1):
            cal[f'{month["month"]}_{i}'] = task
    pages['calendar'] = cal

    # ── Yard page ──
    yard = {}
    for s in YARD_SUGGESTIONS:
        yard[f'title_{s["id"]}'] = s['title']
        yard[f'desc_{s["id"]}'] = s['description']
        yard[f'lbl_plants_{s["id"]}'] = 'Suggested Plants'
        for i, p in enumerate(s['plants'], 1):
            yard[f'plant_{s["id"]}_{i}_name'] = p['name']
            yard[f'plant_{s["id"]}_{i}_notes'] = p['notes']
        for i, benefit in enumerate(s['benefits'], 1):
            yard[f'ben_{s["id"]}_{i}'] = f'\u2713 {benefit}'
        if s.get('tip'):
            yard[f'tip_{s["id"]}'] = f'\U0001f4a1 Tip: {s["tip"]}'
    pages['yard'] = yard

    # ── Plant detail pages ──
    for plant in ALL_PLANTS:
        pid = plant['id']
        t = {
            'name': plant['name'],
            'difficulty': plant['difficulty'].capitalize(),
            'category': plant['category'].capitalize(),
        }
        if plant.get('pnw_notes'):
            t['pnw_notes'] = plant['pnw_notes']
            t['lbl_pnw'] = '\U0001f332 PNW Growing Notes'
        sun_or_light = plant.get('sun') or plant.get('light')
        if sun_or_light:
            t['light'] = sun_or_light
            t['lbl_light'] = '\u2600\ufe0f Light'
        if plant.get('water'):
            t['water'] = plant['water']
            t['lbl_water'] = '\U0001f4a7 Water'
        if plant.get('soil'):
            t['soil'] = plant['soil']
            t['lbl_soil'] = '\U0001faa8 Soil'
        if plant.get('humidity'):
            t['humidity'] = plant['humidity']
            t['lbl_humidity'] = '\U0001f4a8 Humidity'
        if plant.get('temp'):
            t['temp'] = plant['temp']
            t['lbl_temp'] = '\U0001f321\ufe0f Temperature'
        if plant.get('spacing_in'):
            t['spacing'] = f'{plant["spacing_in"]}" apart'
            t['lbl_spacing'] = '\U0001f4cf Spacing'
        if plant.get('tip'):
            t['tip'] = plant['tip']
            t['lbl_tip'] = '\U0001f4a1 Pro Tip'
        if plant.get('fertilizer'):
            t['fertilizer'] = plant['fertilizer']
            t['lbl_fertilizer'] = '\U0001f9ea Fertilizer'
        if plant.get('frost_hardy'):
            t['frost_hardy'] = '\u2744\ufe0f Frost Hardy'
        if 'toxic_pets' in plant and not plant.get('toxic_pets'):
            t['pet_safe'] = '\U0001f43e Pet Safe'
        if any(plant.get(f) for f in ('start_indoors', 'direct_sow', 'transplant', 'harvest', 'bloom')):
            t['lbl_timeline'] = '\U0001f4c5 Planting Timeline (Seattle)'
        if plant.get('start_indoors'):
            t['lbl_indoors'] = 'Start Indoors'
        if plant.get('direct_sow'):
            t['lbl_sow'] = 'Direct Sow'
        if plant.get('transplant'):
            t['lbl_transplant'] = 'Transplant Outdoors'
        if plant.get('harvest'):
            t['lbl_harvest'] = 'Harvest'
        if plant.get('bloom'):
            t['lbl_bloom'] = '\U0001f338 Bloom Season'
        if plant.get('companions'):
            t['lbl_companion'] = '\U0001f91d Companion Planting'
            t['lbl_good'] = '\u2705 Good Companions'
        if plant.get('avoid_near') and plant['avoid_near']:
            t['lbl_avoid'] = '\u274c Keep Away From'
        if plant.get('varieties'):
            t['lbl_varieties'] = 'Best varieties:'
        t['lbl_videos'] = 'Growing Guides'
        t['lbl_ask'] = f'Ask AI about {plant["name"]}'
        pages[f'plant:{pid}'] = t

    return pages


@bp.route("/api/prewarm", methods=["POST"])
def api_prewarm():
    """Pre-warm all translation caches via SSE progress stream."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    body = request.get_json(force=True, silent=True) or {}
    force = body.get("force", False)

    pages = _build_all_page_texts()
    client = genai.Client(api_key=api_key)
    table = _get_translations_table()

    def generate():
        total = len(pages)
        done = 0
        n_cached = 0
        n_errors = 0

        for page_key, texts in pages.items():
            if not force and table:
                try:
                    resp = table.get_item(Key={"page_key": page_key})
                    item = resp.get("Item")
                    if item and item.get("translations"):
                        cached = json.loads(item["translations"])
                        if set(texts.keys()).issubset(set(cached.keys())):
                            n_cached += 1
                            done += 1
                            yield f"data: {json.dumps({'page': page_key, 'status': 'cached', 'progress': f'{done}/{total}'})}\n\n"
                            continue
                except Exception:
                    pass

            try:
                translations = _translate_texts_batch(client, texts)
                if table and translations:
                    try:
                        table.put_item(Item={
                            "page_key": page_key,
                            "translations": json.dumps(translations, ensure_ascii=False),
                            "updated_at": int(time.time()),
                        })
                    except Exception as exc:
                        log.warning("Cache write failed for %s: %s", page_key, exc)
                done += 1
                yield f"data: {json.dumps({'page': page_key, 'status': 'translated', 'count': len(translations), 'progress': f'{done}/{total}'})}\n\n"
            except Exception as exc:
                n_errors += 1
                done += 1
                log.exception("Prewarm translation failed for %s", page_key)
                yield f"data: {json.dumps({'page': page_key, 'status': 'error', 'error': str(exc), 'progress': f'{done}/{total}'})}\n\n"

        summary = {
            'done': True, 'total': total,
            'translated': done - n_cached - n_errors,
            'cached': n_cached, 'errors': n_errors,
        }
        yield f"data: {json.dumps(summary)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
