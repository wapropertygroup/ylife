"""
yplanter.routes
~~~~~~~~~~~~~~~
URL routes for the yPlanter gardening guide.
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, render_template, request, jsonify

from yplanter.plants_db import (
    VEGETABLES, HOUSEPLANTS, YARD_SUGGESTIONS, PLANTING_CALENDAR,
    RESOURCES, ALL_PLANTS, CATEGORIES, DIFFICULTIES,
    search_all, get_plant,
)

bp = Blueprint("planter", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)


@bp.route("/")
def index():
    """Main page — browse all plants, calendar, and yard suggestions."""
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
    """Individual plant detail page."""
    plant = get_plant(plant_id)
    if not plant:
        return render_template("404.html"), 404
    return render_template("plant_detail.html", plant=plant)


@bp.route("/calendar")
def calendar():
    """Full planting calendar page."""
    return render_template("calendar.html", calendar=PLANTING_CALENDAR)


@bp.route("/yard")
def yard():
    """Yard suggestions page."""
    return render_template("yard.html", suggestions=YARD_SUGGESTIONS)


@bp.route("/resources")
def resources():
    """Resources page."""
    return render_template("resources.html", resources=RESOURCES)


@bp.route("/api/plants")
def api_plants():
    """Search plants API."""
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "")
    diff = request.args.get("difficulty", "")
    results = search_all(query=q, category=cat, difficulty=diff)
    return jsonify({"plants": results})


@bp.route("/api/collection/export")
def api_collection_export():
    """Return plant details for a list of IDs (for collection feature)."""
    ids = request.args.get("ids", "")
    if not ids:
        return jsonify({"plants": []})
    plant_ids = [i.strip() for i in ids.split(",") if i.strip()]
    plants = [p for p in ALL_PLANTS if p["id"] in plant_ids]
    return jsonify({"plants": plants})
