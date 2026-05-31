"""
ystocker package
~~~~~~~~~~~~~~~~
Flask application factory + peer-group configuration.
"""
from __future__ import annotations

from flask import Flask

# ---------------------------------------------------------------------------
# Peer group configuration - edit here OR use the /groups UI in the browser.
# Each key is a group name; each value is a list of Yahoo Finance ticker symbols.
# ---------------------------------------------------------------------------
PEER_GROUPS: dict[str, list[str]] = {
    "Tech":              ["MSFT", "AAPL", "GOOGL", "META", "NVDA", "AMZN", "TSLA", "NFLX", "ADBE", "CRM", "PLTR", "ORCL"],
    "Software":          ["MSFT", "ORCL", "CRM", "ADBE", "PLTR", "INTU", "NOW", "CRWD"],
    "Cloud / SaaS":      ["MSFT", "CRM", "NOW", "AMZN", "ORCL", "SNOW",
                          "DDOG", "CRWD", "MDB", "NET", "ZS", "IGV"],
    "Semiconductors":    ["NVDA", "AMD", "TSM", "AVGO", "ASML", "INTC", "QCOM", "MU"],
    "Financials":        ["JPM", "BAC", "GS", "MS", "BLK", "BRK-B", "V", "MA", "WFC", "AXP"],
    "Healthcare":        ["UNH", "JNJ", "LLY", "ABBV", "MRK", "ISRG", "PFE", "TMO", "ABT", "DHR"],
    "Biotech":           ["LLY", "AMGN", "VRTX", "REGN", "GILD", "BIIB", "MRNA", "BNTX", "ILMN", "IBB"],
    "Retail":            ["WMT", "AMZN", "COST", "TGT", "HD", "LOW", "TJX", "DG", "DLTR", "BJ",
                          "ULTA", "FIVE", "ROST", "BURL", "KR", "XRT"],
    "E-commerce":        ["AMZN", "SHOP", "MELI", "EBAY", "ETSY", "CHWY", "W", "PDD", "JD", "BABA"],
    "Streaming / Media": ["NFLX", "DIS", "WBD", "PARA", "CMCSA", "SPOT", "ROKU", "FUBO", "TME", "BIDU"],
    "Real Estate":       ["AMT", "PLD", "EQIX", "SPG", "O", "DLR", "PSA", "WELL"],
    "Metals & Mining":   ["FCX", "NEM", "AA", "MP", "RIO", "BHP", "VALE", "GOLD", "SCCO", "TECK",
                          "AEM", "WPM", "FNV", "GDX", "GDXJ", "SIL", "COPX"],
    "Energy / Oil & Gas":["XOM", "CVX", "COP", "EOG", "SLB", "PSX", "MPC", "OXY"],
    "Industrials":       ["BA", "RTX", "HON", "CAT", "DE", "GE", "LMT", "MMM", "UNP", "UPS"],
    "Apparel & Footwear":["NKE", "LULU", "DECK", "ONON", "BIRK", "SKX", "CROX", "RL", "TPR", "UAA"],
    "Consumer Staples":  ["PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "MDLZ"],
    "Airlines & Travel": ["DAL", "UAL", "AAL", "LUV", "BKNG", "EXPE", "ABNB", "MAR", "HLT", "RCL"],
    "Communication":     ["GOOGL", "META", "NFLX", "DIS", "CMCSA", "VZ", "T", "TMUS", "CHTR", "EA",
                          "TTWO", "ATVI", "RBLX", "XLC"],
    "Utilities":         ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED", "PEG",
                          "WEC", "ES", "AWK", "ETR", "CMS", "FE", "XLU"],
    "AI / Robotics":     ["NVDA", "MSFT", "GOOGL", "META", "AMZN", "PLTR", "AI", "SMCI",
                          "ANET", "ARM", "DELL", "TSM", "AVGO", "IRBT"],
    "US Broad ETFs":     ["SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VXUS", "BND", "AGG",
                          "SHY", "IEF", "HYG", "LQD", "RSP"],
    "Sector ETFs":       ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU"],
    "International ETFs":["EWJ", "FXI", "MCHI", "EEM", "VWO", "IXUS", "VXUS", "EWZ", "INDA", "EWY"],
    "Bonds / Fixed Income":["TLT", "IEF", "SHY", "BND", "AGG", "LQD", "HYG", "JNK", "TIP", "STIP",
                            "SHV", "SGOV", "MUB"],
    "Commodities ETFs":  ["GLD", "SLV", "USO", "UNG", "DBC", "PDBC", "URA", "WEAT", "CORN", "SOYB",
                          "PPLT", "PALL", "CPER"],
    "China Tech":        ["BABA", "JD", "PDD", "BIDU", "TME", "TCEHY", "NTES", "BILI", "NIO", "XPEV",
                          "LI", "FXI", "MCHI", "KWEB", "ASHR"],
}

# ---------------------------------------------------------------------------
# YouTube curated channels for the Videos feed.
# Each tuple: (handle, channel_id, display_name)
# Chinese-language channels first, then English channels.
# ---------------------------------------------------------------------------
YT_CHANNELS: list[tuple[str, str, str]] = [
    # ── Chinese-language channels ──────────────────────────────────────────
    ("andyleegogo",       "UCwyRBuGpaLYnFuohCYyjBeQ", "Andy lee"),
    ("RhinoFinance",      "UCFQsi7WaF5X41tcuOryDk8w", "视野环球财经"),
    ("MeiTouNews",        "UCGpj3DO_5_TUDCNUgS9mjiQ", "美投侃新闻"),
    ("NaNaShuoMeiGu",     "UCFhJ8ZFg9W4kLwFTBBNIjOw", "NaNa说美股"),
    ("gendanqun",         "UCf48rlZVxa_CPsrG6LW5big", "美股短线交易"),
    ("MeiTouJun",         "UCBUH38E0ngqvmTqdchWunwQ", "美投讲美股"),
    ("LA_Banker",         "UCW1cHQAzfL3pwKlKNwRjelQ", "精英财经 LABanker"),
    ("ShepherdCapital",   "UCkvZ2usiWOy1sfYmNfY9Pdw", "Shepherd Capital Markets"),
    ("yutinghaofinance",  "UC0lbAQVpenvfA2QqzsRtL_g", "游庭皓的財經皓角"),
    ("windkiss-cn5tu",    "UCpJPv66uSo3Tj1iT_UmfW6Q", "财经-沉默的螺旋上"),
    # ── English-language channels ──────────────────────────────────────────
    ("zinvestglobal1190", "UCXt4LLGqNUiRiJpBnZuqYuA", "ZInvest Global"),
    ("Fundstrat_Direct",  "UCiWP9CSmjdaV5vJgRfDqsKA", "Fundstrat"),
    ("TheTravelingTrader","UCe6eTFJOPJgE5GDmJTroSmA", "The Traveling Trader"),
    ("CNBCtelevision",    "UCvJJ_dzjViJCoLf5uKUTwoA", "CNBC Television"),
]


def _load_secrets_from_ssm() -> None:
    """Fetch secrets from AWS SSM Parameter Store and inject into os.environ.

    Only runs when boto3 is available and the parameters exist.
    Falls back silently so local dev (plain env vars) is unaffected.

    Parameters fetched:
      /ystocker/GEMINI_API_KEY  → os.environ["GEMINI_API_KEY"]
    """
    import logging
    import os

    log = logging.getLogger(__name__)

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return  # boto3 not installed — skip

    SSM_PARAMS = {
        "/ystocker/GEMINI_API_KEY":     "GEMINI_API_KEY",
        "/ystocker/YOUTUBE_API_KEY":    "YOUTUBE_API_KEY",
        "/ystocker/SES_FROM_EMAIL":     "SES_FROM_EMAIL",
        "/ystocker/GOOGLE_CLIENT_ID":   "GOOGLE_CLIENT_ID",
        "/ystocker/YSTOCKER_SECRET_KEY": "YSTOCKER_SECRET_KEY",
    }

    try:
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        for param_name, env_key in SSM_PARAMS.items():
            if os.environ.get(env_key):
                continue  # already set locally — don't overwrite
            try:
                resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
                os.environ[env_key] = resp["Parameter"]["Value"]
                log.info("SSM: loaded %s → %s", param_name, env_key)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code != "ParameterNotFound":
                    log.warning("SSM: could not fetch %s: %s", param_name, e)
    except NoCredentialsError:
        pass  # not on AWS — skip silently
    except Exception as exc:
        log.warning("SSM: unexpected error: %s", exc)


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
    import os as _os
    app.secret_key = _os.environ.get("YSTOCKER_SECRET_KEY", "ystocker-dev-secret")  # needed for flash + session
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # Register the main blueprint (routes live in routes.py)
    from ystocker.routes import bp, _start_background_thread, _start_heatmap_scheduler, _start_daily_broadcast_scheduler
    app.register_blueprint(bp)

    # Jinja2 filter: unix timestamp → "Feb 21, 2026 15:30"
    @app.template_filter("datetimeformat")
    def datetimeformat(ts):
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%b %d, %Y %H:%M")

    # Cache-busting token for static assets. Uses the file mtime of i18n.js
    # so browsers re-download translations whenever they change.
    import os
    _i18n_path = os.path.join(app.static_folder, "i18n.js")
    try:
        _cache_bust = str(int(os.path.getmtime(_i18n_path)))
    except OSError:
        _cache_bust = str(int(datetime.datetime.now().timestamp()))

    @app.context_processor
    def _inject_cache_bust():
        return {"cache_bust": _cache_bust}

    @app.context_processor
    def _inject_auth_context():
        """Make google_client_id + current_user available in every template."""
        from flask import session
        email = session.get("user_email")
        if email:
            current_user = {
                "email":   email,
                "name":    session.get("user_name", email.split("@")[0]),
                "picture": session.get("user_picture", ""),
            }
        else:
            current_user = {"email": "", "name": "", "picture": ""}
        return {
            "google_client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "current_user":     current_user,
        }

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

    # Start heatmap daily auto-snapshot scheduler (weekdays at 16:30 ET)
    _start_heatmap_scheduler()

    # Start daily email broadcast scheduler (UTC 00:00 every day)
    _start_daily_broadcast_scheduler()

    return app
