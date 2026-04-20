"""
yplanner package
~~~~~~~~~~~~~~~~
Flask application factory for the yPlanner trip planning app.
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
    app.secret_key = os.environ.get("YPLANNER_SECRET_KEY", "yplanner-dev-secret")
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    from yplanner.routes import bp
    app.register_blueprint(bp)

    return app
