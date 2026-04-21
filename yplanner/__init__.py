"""
yplanner package
~~~~~~~~~~~~~~~~
Flask application factory for the yPlanner trip planning app.
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
        "/yplanner/GOOGLE_CLIENT_ID":    "GOOGLE_CLIENT_ID",
        "/yplanner/GOOGLE_MAPS_API_KEY": "GOOGLE_MAPS_API_KEY",
        "/yplanner/APPLE_SERVICE_ID":    "APPLE_SERVICE_ID",
        "/yplanner/YPLANNER_SECRET_KEY": "YPLANNER_SECRET_KEY",
    }

    # Skip params already set in env
    needed = {k: v for k, v in SSM_PARAMS.items() if not os.environ.get(v)}
    if not needed:
        return

    try:
        ssm = boto3.client(
            "ssm",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 1}),
        )
        # Batch fetch all params in one API call
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
    app.secret_key = os.environ.get("YPLANNER_SECRET_KEY", "yplanner-dev-secret")
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    from yplanner.routes import bp
    app.register_blueprint(bp)

    # Eagerly init DynamoDB tables so the first request isn't slow
    with app.app_context():
        from yplanner.routes import _get_trips_table, _get_shared_table, _get_users_table
        _get_trips_table()
        _get_shared_table()
        _get_users_table()

    return app
