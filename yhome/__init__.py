"""
yhome package
~~~~~~~~~~~~~
Flask application factory for the Li Family home / navigation page.
"""
from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "yhome-dev-key"

    from yhome.routes import bp
    app.register_blueprint(bp)

    return app
