"""
yimage package
~~~~~~~~~~~~~~
Flask application factory for the yImage image/PDF tools app.
Browser-based image and PDF processing — fully offline, server-side.
"""
from __future__ import annotations

import logging
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
    app.secret_key = os.environ.get("YIMAGE_SECRET_KEY", "yimage-dev-secret")
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    from yimage.routes import bp
    app.register_blueprint(bp)

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    return app
