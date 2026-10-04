"""
yimage package
~~~~~~~~~~~~~~
Flask application factory for the yImage image/PDF tools app.
Browser-based image and PDF processing — fully offline, server-side.
"""
from __future__ import annotations

import logging
import os
import secrets

from flask import Flask


def create_app() -> Flask:
    """Create and configure the Flask application."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__)
    # A real key comes from YIMAGE_SECRET_KEY (SSM or .env). Without one, a random key for
    # this process, never a constant: a constant in this repository lets anyone
    # sign a session cookie. Under gunicorn --preload it is made once in the
    # master, so both workers share it; sessions last until the next restart.
    _key = os.environ.get("YIMAGE_SECRET_KEY")
    if not _key:
        _key = secrets.token_hex(32)
        logging.getLogger(__name__).warning(
            "YIMAGE_SECRET_KEY is not set: sessions are signed with a key that lasts until the next restart")
    app.secret_key = _key
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
