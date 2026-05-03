"""
ypay package
~~~~~~~~~~~~
Flask application factory for the yPay payment app.
Accept payments via Stripe Checkout for products, services, or donations.
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
        "/ypay/STRIPE_SECRET_KEY":      "STRIPE_SECRET_KEY",
        "/ypay/STRIPE_PUBLISHABLE_KEY": "STRIPE_PUBLISHABLE_KEY",
        "/ypay/STRIPE_WEBHOOK_SECRET":  "STRIPE_WEBHOOK_SECRET",
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
    app.secret_key = os.environ.get("YPAY_SECRET_KEY", "ypay-dev-secret")
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    from ypay.routes import bp
    app.register_blueprint(bp)

    return app
