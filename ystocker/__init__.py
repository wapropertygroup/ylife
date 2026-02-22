"""
ystocker package
~~~~~~~~~~~~~~~~
Flask application factory + peer-group configuration.
"""
from __future__ import annotations

from typing import Dict, List

from flask import Flask

# ---------------------------------------------------------------------------
# Peer group configuration - edit here OR use the /groups UI in the browser.
# Each key is a group name; each value is a list of Yahoo Finance ticker symbols.
# ---------------------------------------------------------------------------
PEER_GROUPS: Dict[str, List[str]] = {
    "Tech":              ["MSFT", "AAPL", "GOOGL", "META", "NVDA"],
    "Cloud / SaaS":      ["MSFT", "CRM", "NOW", "AMZN", "ORCL"],
    "Semiconductors":    ["NVDA", "AMD", "INTC", "QCOM", "TSM", "AVGO", "ASML"],
    "Financials":        ["JPM", "BAC", "GS", "MS", "BLK", "COF", "BRK-B", "AXP"],
    "Healthcare":        ["UNH", "JNJ", "LLY", "ABBV", "MRK", "ISRG"],
    "Retail":            ["WMT", "AMZN", "COST", "TGT", "HD"],
    "Real Estate":       ["AMT", "PLD", "EQIX", "SPG", "O", "HLT"],
    "Metals & Mining":   ["FCX", "NEM", "AA", "MP", "COPX", "GDX", "SIL", "SLX"],
    "Apparel & Footwear": ["NKE", "LULU", "UAA", "VFC"],
    "US Broad ETFs":     ["SPY", "QQQ", "IWM", "DIA", "VTI"],
    "Sector ETFs":       ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE"],
    "International ETFs": ["FLJP", "FLJH", "FLKR", "FLTW", "FLCA", "IXUS", "VXUS", "FLEE", "ASHS", "FLBR", "FLCH", "FLGR", "FLMX", "FLAX", "FLSW"],
}


def _load_secrets_from_ssm() -> None:
    """Fetch secrets from AWS SSM Parameter Store and inject into os.environ.

    Only runs when boto3 is available and the parameters exist.
    Falls back silently so local dev (plain env vars) is unaffected.

    Parameters fetched:
      /ystocker/GEMINI_API_KEY  → os.environ["GEMINI_API_KEY"]
    """
    import os
    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return  # boto3 not installed — skip

    SSM_PARAMS = {
        "/ystocker/GEMINI_API_KEY": "GEMINI_API_KEY",
    }

    try:
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        for param_name, env_key in SSM_PARAMS.items():
            if os.environ.get(env_key):
                continue  # already set locally — don't overwrite
            try:
                resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
                os.environ[env_key] = resp["Parameter"]["Value"]
                import logging
                logging.getLogger(__name__).info(
                    "SSM: loaded %s → %s", param_name, env_key
                )
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code != "ParameterNotFound":
                    import logging
                    logging.getLogger(__name__).warning(
                        "SSM: could not fetch %s: %s", param_name, e
                    )
    except NoCredentialsError:
        pass  # not on AWS — skip silently
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("SSM: unexpected error: %s", exc)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    import datetime

    # Pull secrets from AWS SSM Parameter Store (no-op outside AWS or if
    # the env vars are already set locally).
    _load_secrets_from_ssm()

    # Load .env from the project root so secrets like GEMINI_API_KEY are
    # available even when the server is started outside an interactive shell.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__)
    app.secret_key = "ystocker-dev-secret"  # needed for flash messages

    # Register the main blueprint (routes live in routes.py)
    from ystocker.routes import bp, _start_background_thread
    app.register_blueprint(bp)

    # Jinja2 filter: unix timestamp → "Feb 21, 2026 15:30"
    @app.template_filter("datetimeformat")
    def datetimeformat(ts):
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%b %d, %Y %H:%M")

    # Configure logging so INFO messages appear in the terminal
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Start background cache-warmer (runs once at startup, then every 24 h).
    # daemon=True ensures the thread never blocks a clean shutdown.
    _start_background_thread()

    # Start 13F institutional holdings cache warmer (24h TTL)
    from ystocker.sec13f import start_background_thread as _start_sec13f_thread
    _start_sec13f_thread()

    return app
