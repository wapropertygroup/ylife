"""
ystocker package
~~~~~~~~~~~~~~~~
Flask application factory + peer-group configuration.
"""
from __future__ import annotations

from typing import Dict, List

from flask import Flask

# ---------------------------------------------------------------------------
# Peer group configuration - edit here OR use the /groups UI in the browser.
# Each key is a group name; each value is a list of Yahoo Finance ticker symbols.
# ---------------------------------------------------------------------------
PEER_GROUPS: Dict[str, List[str]] = {
    "Tech":              ["MSFT", "AAPL", "GOOGL", "META", "NVDA"],
    "Cloud / SaaS":      ["MSFT", "CRM", "NOW", "SNOW", "AMZN"],
    "Semiconductors":    ["NVDA", "AMD", "INTC", "QCOM", "TSM"],
    "EV & Clean Energy": ["TSLA", "RIVN", "NIO", "ENPH", "FSLR"],
    "Financials":        ["JPM", "BAC", "GS", "MS", "BLK", "COF"],
    "Healthcare":        ["UNH", "JNJ", "LLY", "ABBV", "MRK"],
    "Retail":            ["WMT", "AMZN", "COST", "TGT", "HD"],
    "Metals & Mining":   ["FCX", "NEM", "GOLD", "AA", "MP", "GLD", "SLV", "COPX", "GDX", "SIL"],
    "Apparel & Footwear": ["NKE", "LULU", "UAA", "VFC", "PVH"],
    "US Broad ETFs":     ["SPY", "QQQ", "IWM", "DIA", "VTI"],
    "Sector ETFs":       ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE"],
    "International ETFs": ["FLJP", "FLJH", "FLKR", "FLTW", "FLCA", "IXUS", "VXUS", "FLEE", "ASHS"],
}


def create_app() -> Flask:
    """Create and configure the Flask application."""
    import datetime

    app = Flask(__name__)
    app.secret_key = "ystocker-dev-secret"  # needed for flash messages

    # Register the main blueprint (routes live in routes.py)
    from ystocker.routes import bp, _start_background_thread
    app.register_blueprint(bp)

    # Jinja2 filter: unix timestamp → "Feb 21, 2026 15:30"
    @app.template_filter("datetimeformat")
    def datetimeformat(ts):
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%b %d, %Y %H:%M")

    # Configure logging so INFO messages appear in the terminal
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Start background cache-warmer (runs once at startup, then every 24 h).
    # daemon=True ensures the thread never blocks a clean shutdown.
    _start_background_thread()

    return app
