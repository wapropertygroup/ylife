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
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return

    SSM_PARAMS = {
        "/yplanner/GOOGLE_CLIENT_ID":  "GOOGLE_CLIENT_ID",
        "/yplanner/GOOGLE_MAPS_API_KEY": "GOOGLE_MAPS_API_KEY",
        "/yplanner/APPLE_SERVICE_ID":  "APPLE_SERVICE_ID",
        "/yplanner/YPLANNER_SECRET_KEY": "YPLANNER_SECRET_KEY",
    }

    try:
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        for param_name, env_key in SSM_PARAMS.items():
            if os.environ.get(env_key):
                continue
            try:
                resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
                os.environ[env_key] = resp["Parameter"]["Value"]
            except ClientError:
                pass
    except NoCredentialsError:
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

    return app
