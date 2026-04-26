"""
yplanter package
~~~~~~~~~~~~~~~~
Flask application factory for the yPlanter gardening guide app.
Seattle / Pacific Northwest focused plant & yard recommendations.
"""
from __future__ import annotations

import os

from flask import Flask


def _load_secrets_from_ssm() -> None:
    """Fetch secrets from AWS SSM Parameter Store and inject into os.environ."""
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return

    SSM_PARAMS = {
        "/ystocker/GEMINI_API_KEY": "GEMINI_API_KEY",
        "/ystocker/YOUTUBE_API_KEY": "YOUTUBE_API_KEY",
    }

    needed = {k: v for k, v in SSM_PARAMS.items() if not os.environ.get(v)}
    if not needed:
        return

    try:
        ssm = boto3.client(
            "ssm",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 1}),
        )
        resp = ssm.get_parameters(Names=list(needed.keys()), WithDecryption=True)
        for param in resp.get("Parameters", []):
            env_key = needed.get(param["Name"])
            if env_key and param.get("Value"):
                os.environ[env_key] = param["Value"]
    except (NoCredentialsError, ClientError):
        pass
    except Exception:
        pass


def create_app() -> Flask:
    """Create and configure the Flask application."""
    _load_secrets_from_ssm()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__)
    app.secret_key = os.environ.get("YPLANTER_SECRET_KEY", "yplanter-dev-secret")
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    from yplanter.routes import bp
    app.register_blueprint(bp)

    with app.app_context():
        from yplanter.routes import _get_history_table
        _get_history_table()

    return app
