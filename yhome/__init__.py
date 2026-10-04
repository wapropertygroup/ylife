"""
yhome package
~~~~~~~~~~~~~
Flask application factory for the Li Family home / navigation page.
"""
from __future__ import annotations

import logging
import os
import secrets

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)
    # A real key comes from YHOME_SECRET_KEY (SSM or .env). Without one, a random key for
    # this process, never a constant: a constant in this repository lets anyone
    # sign a session cookie. Under gunicorn --preload it is made once in the
    # master, so both workers share it; sessions last until the next restart.
    _key = os.environ.get("YHOME_SECRET_KEY")
    if not _key:
        _key = secrets.token_hex(32)
        logging.getLogger(__name__).warning(
            "YHOME_SECRET_KEY is not set: sessions are signed with a key that lasts until the next restart")
    app.config["SECRET_KEY"] = _key

    from yhome.routes import bp
    app.register_blueprint(bp)

    return app
