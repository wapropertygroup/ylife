"""
yhome.routes
~~~~~~~~~~~~
URL routes for the Li Family home page.
"""
from flask import Blueprint, render_template

bp = Blueprint("home", __name__, template_folder="templates", static_folder="static")

APPS = [
    {
        "id": "stocker",
        "name": "yStocker",
        "emoji": "\U0001f4c8",
        "tagline": "Stock Market Intelligence",
        "description": "Real-time valuation tracker with sector analysis, peer comparisons, "
                       "daily AI market summaries, Fed balance sheet, 13F holdings, and heatmaps.",
        "url": "https://stock.li-family.us",
        "color": "blue",
        "features": ["Peer-group valuation", "Daily AI summaries", "Sector heatmaps",
                     "Fear & Greed index", "Fed balance sheet"],
    },
    {
        "id": "planner",
        "name": "yPlanner",
        "emoji": "\U0001f5fa\ufe0f",
        "tagline": "AI Trip Planner",
        "description": "Plan trips with AI-powered itineraries, Google Maps integration, "
                       "and smart scheduling. Sign in with Apple or Google to save your plans.",
        "url": "https://planner.li-family.us",
        "color": "violet",
        "features": ["AI itinerary generation", "Google Maps built-in", "Apple Sign-In",
                     "Multi-day planning", "Share with family"],
    },
    {
        "id": "planter",
        "name": "yPlanter",
        "emoji": "\U0001f331",
        "tagline": "Seattle Garden Guide",
        "description": "What to plant, when to plant, and how to grow in the Pacific Northwest. "
                       "50+ plants, monthly calendar, yard ideas, and AI gardening chat.",
        "url": "https://plant.li-family.us",
        "color": "green",
        "features": ["50+ PNW plants", "Planting calendar", "Yard design ideas",
                     "AI garden chat", "YouTube guides"],
    },
]


@bp.route("/")
def index():
    return render_template("index.html", apps=APPS)
