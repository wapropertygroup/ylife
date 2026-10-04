"""
ybg package
~~~~~~~~~~~
Flask application factory for the yBG tenant background check app.
Landlords send applicants a link → tenants fill out info → background
check runs via Checkr API → landlord reviews results.
"""
from __future__ import annotations

import logging
import os
import secrets

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
        "/ybg/CHECKR_API_KEY":    "CHECKR_API_KEY",
        "/ybg/YBG_ADMIN_EMAIL":   "YBG_ADMIN_EMAIL",
        "/ybg/GEMINI_API_KEY":    "GEMINI_API_KEY",
        "/ystocker/SES_FROM_EMAIL": "SES_FROM_EMAIL",
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
    # A real key comes from YBG_SECRET_KEY (SSM or .env). Without one, a random key for
    # this process, never a constant: a constant in this repository lets anyone
    # sign a session cookie. Under gunicorn --preload it is made once in the
    # master, so both workers share it; sessions last until the next restart.
    _key = os.environ.get("YBG_SECRET_KEY")
    if not _key:
        _key = secrets.token_hex(32)
        logging.getLogger(__name__).warning(
            "YBG_SECRET_KEY is not set: sessions are signed with a key that lasts until the next restart")
    app.secret_key = _key
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from ybg.routes import bp
    app.register_blueprint(bp)

    return app
