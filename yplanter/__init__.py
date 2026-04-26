"""
yplanter package
~~~~~~~~~~~~~~~~
Flask application factory for the yPlanter gardening guide app.
Seattle / Pacific Northwest focused plant & yard recommendations.
"""
from __future__ import annotations

import os

from flask import Flask


def create_app() -> Flask:
    """Create and configure the Flask application."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__)
    app.secret_key = os.environ.get("YPLANTER_SECRET_KEY", "yplanter-dev-secret")

    from yplanter.routes import bp
    app.register_blueprint(bp)

    return app
