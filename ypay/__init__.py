"""
ypay package
~~~~~~~~~~~~
Flask application factory for the yPay payment app.
Accept payments via Stripe Checkout for products, services, or donations.
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
        "/ypay/STRIPE_SECRET_KEY":      "STRIPE_SECRET_KEY",
        "/ypay/STRIPE_PUBLISHABLE_KEY": "STRIPE_PUBLISHABLE_KEY",
        "/ypay/STRIPE_WEBHOOK_SECRET":  "STRIPE_WEBHOOK_SECRET",
        # yStocker's key, to verify the subscription handoff tokens it signs
        # for a signed-in reader (ystocker.subscriptions.handoff). Without it
        # every subscribe or billing link is refused, never trusted.
        "/ystocker/YSTOCKER_SECRET_KEY": "YSTOCKER_SECRET_KEY",
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
    # A real key comes from YPAY_SECRET_KEY (SSM or .env). Without one, a random key for
    # this process, never a constant: a constant in this repository lets anyone
    # sign a session cookie. Under gunicorn --preload it is made once in the
    # master, so both workers share it; sessions last until the next restart.
    _key = os.environ.get("YPAY_SECRET_KEY")
    if not _key:
        _key = secrets.token_hex(32)
        logging.getLogger(__name__).warning(
            "YPAY_SECRET_KEY is not set: sessions are signed with a key that lasts until the next restart")
    app.secret_key = _key
    # Brand by hostname, mirroring ystocker/__init__.py. This app answers on
    # pay.li-family.us and on pay.trade-agents.com, and a buyer who started on
    # trade-agents.com must not be handed to a page called yPay at the exact
    # moment they are asked for card details.
    _TA_PAY_HOSTS = {"pay.trade-agents.com", "trade-agents.com",
                     "www.trade-agents.com"}

    @app.context_processor
    def _inject_brand():
        from flask import request as _rq

        host = ""
        try:
            host = (_rq.host or "").split(":")[0].lower()
        except Exception:  # noqa: BLE001 - no request context
            pass
        is_ta = host in _TA_PAY_HOSTS
        return {"brand_name": "TradeAgents" if is_ta else "yPay",
                "brand_is_ta": is_ta}

    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # The TradeAgents pay pages (templates/ta/) draw trade-agents.com's product
    # mark from yStocker's own macro rather than a copy of it. Under an explicit
    # prefix -- {% import "ystocker/wiki/_macros.html" %} -- so a page here names
    # what it borrows, and no ypay template can be shadowed by one of the same
    # name over there (both apps have a base.html and an index.html).
    from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader

    ystocker_templates = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ystocker", "templates")
    app.jinja_env.loader = ChoiceLoader([
        app.jinja_env.loader,
        PrefixLoader({"ystocker": FileSystemLoader(ystocker_templates)}),
    ])

    from ypay.routes import _get_stripe, bp
    app.register_blueprint(bp)

    # TradeAgents Pro: re-read subscriptions near the end of their period, so a
    # renewal, a failed card or a cancellation is recorded even if its webhook
    # never arrives. One thread, in the master under --preload.
    if os.environ.get("YPAY_RECONCILE", "1") != "0":
        from ypay import billing

        billing.start_reconcile_thread(_get_stripe)

    return app
