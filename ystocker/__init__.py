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
    "Software":          ["MSFT", "ORCL", "CRM", "ADBE", "PLTR", "INTU", "NOW", "CRWD", "PANW",
                          "ADSK", "SNPS", "CDNS", "ROP", "WDAY", "TEAM", "ANSS", "HUBS", "DDOG",
                          "SNOW", "MDB", "ZS", "NET", "OKTA", "VEEV", "PTC", "TYL", "FTNT", "FFIV",
                          "BSY", "DOCU", "TWLO", "ZM", "U", "GTLB", "S", "DT", "PATH", "AI",
                          "APP", "AYX", "DBX", "BILL", "PCOR", "ESTC", "FROG", "MNDY", "TENB",
                          "NTNX", "FRSH", "AVPT", "IGV"],
    "Cloud / SaaS":      ["MSFT", "CRM", "NOW", "AMZN", "ORCL", "SNOW", "DDOG", "MDB", "NET", "ZS",
                          "PANW", "CRWD", "OKTA", "WDAY", "TEAM", "INTU", "SHOP", "IGV"],
    "Semiconductors":    ["NVDA", "AMD", "INTC", "QCOM", "TSM", "AVGO", "ASML", "MU", "TXN", "AMAT",
                          "LRCX", "KLAC", "MRVL", "ARM", "ADI", "MCHP", "ON", "NXPI", "SMH", "SOXX"],
    "Financials":        ["JPM", "BAC", "GS", "MS", "BLK", "COF", "BRK-B", "AXP", "WFC", "C",
                          "SCHW", "USB", "PNC", "TFC", "MA", "V", "PYPL", "FI", "FIS", "KRE", "XLF"],
    "Healthcare":        ["UNH", "JNJ", "LLY", "ABBV", "MRK", "ISRG", "PFE", "TMO", "ABT", "DHR",
                          "AMGN", "BMY", "GILD", "VRTX", "REGN", "CVS", "CI", "HUM", "ELV", "XLV"],
    "Biotech":           ["LLY", "AMGN", "VRTX", "REGN", "GILD", "BIIB", "MRNA", "BNTX", "ILMN",
                          "ALNY", "BMRN", "INCY", "EXEL", "IBB", "XBI"],
    "Retail":            ["WMT", "AMZN", "COST", "TGT", "HD", "LOW", "TJX", "DG", "DLTR", "BJ",
                          "ULTA", "FIVE", "ROST", "BURL", "KR", "XRT"],
    "E-commerce":        ["AMZN", "SHOP", "MELI", "EBAY", "ETSY", "CHWY", "W", "PDD", "JD", "BABA"],
    "Streaming / Media": ["NFLX", "DIS", "WBD", "PARA", "CMCSA", "SPOT", "ROKU", "FUBO", "TME", "BIDU"],
    "Real Estate":       ["AMT", "PLD", "EQIX", "SPG", "O", "DLR", "PSA", "WELL", "EXR", "AVB",
                          "EQR", "VICI", "VTR", "MAA", "ESS", "INVH", "SUI", "VNQ", "XLRE"],
    "Metals & Mining":   ["FCX", "NEM", "AA", "MP", "RIO", "BHP", "VALE", "GOLD", "SCCO", "TECK",
                          "AEM", "WPM", "FNV", "GDX", "GDXJ", "SIL", "COPX"],
    "Energy / Oil & Gas":["XOM", "CVX", "COP", "EOG", "SLB", "PSX", "MPC", "VLO", "OXY", "PXD",
                          "HAL", "BKR", "DVN", "FANG", "HES", "WMB", "KMI", "ENB", "XLE"],
    "Industrials":       ["BA", "RTX", "HON", "CAT", "DE", "GE", "LMT", "NOC", "GD", "MMM",
                          "EMR", "ETN", "ITW", "PH", "UNP", "CSX", "NSC", "FDX", "UPS", "XLI"],
    "Apparel & Footwear":["NKE", "LULU", "UAA", "VFC", "DECK", "ONON", "BIRK", "SKX", "CROX",
                          "RL", "TPR", "CPRI", "PVH", "GPS", "ANF"],
    "Consumer Staples":  ["PG", "KO", "PEP", "WMT", "COST", "PM", "MO", "MDLZ", "CL", "KMB",
                          "GIS", "K", "HSY", "STZ", "EL", "CHD", "CLX", "MNST", "XLP"],
    "Auto / EV":         ["TSLA", "GM", "F", "RIVN", "LCID", "STLA", "TM", "HMC", "NIO", "XPEV",
                          "LI", "BYDDY", "FFIE"],
    "Airlines & Travel": ["DAL", "UAL", "AAL", "LUV", "ALK", "JBLU", "RYAAY", "BKNG", "EXPE", "ABNB",
                          "MAR", "HLT", "H", "RCL", "CCL", "NCLH"],
    "Communication":     ["GOOGL", "META", "NFLX", "DIS", "CMCSA", "VZ", "T", "TMUS", "CHTR", "EA",
                          "TTWO", "ATVI", "RBLX", "XLC"],
    "Utilities":         ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED", "PEG",
                          "WEC", "ES", "AWK", "ETR", "CMS", "FE", "XLU"],
    "Crypto / Blockchain":["COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF", "CIFR", "WULF",
                          "GBTC", "IBIT", "ETHA", "BITX", "BITO"],
    "AI / Robotics":     ["NVDA", "MSFT", "GOOGL", "META", "AMZN", "PLTR", "AI", "SOUN", "SMCI",
                          "ANET", "ARM", "DELL", "TSM", "AVGO", "BOTZ", "ROBO", "IRBT"],
    "US Broad ETFs":     ["SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VXUS", "BND", "AGG", "TLT",
                          "SHY", "IEF", "HYG", "LQD", "RSP"],
    "Sector ETFs":       ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
                          "XLC", "COPX", "GDX", "SIL", "SLX", "SMH", "IGV", "IBB", "XBI", "ARKK",
                          "ITA", "KRE", "KBE", "XHB", "XRT"],
    "International ETFs":["FLJP", "FLJH", "FLKR", "FLTW", "FLCA", "IXUS", "VXUS", "FLEE", "ASHS",
                          "FLBR", "FLCH", "FLGR", "FLMX", "FLAX", "FLSW", "EWJ", "EWZ", "EWU",
                          "EWG", "FXI", "MCHI", "EEM", "VWO", "INDA", "EWY"],
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
        "/ystocker/GEMINI_API_KEY":  "GEMINI_API_KEY",
        "/ystocker/YOUTUBE_API_KEY": "YOUTUBE_API_KEY",
        "/ystocker/SES_FROM_EMAIL":  "SES_FROM_EMAIL",
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
    app.secret_key = "ystocker-dev-secret"  # needed for flash messages

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
