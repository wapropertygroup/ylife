"""
ystocker.symbols
~~~~~~~~~~~~~~~~
Which symbol a reader means, before a run is spent on it: the suggestions under
the 股票代码 box on /agents, and the check that refuses a ticker with no prices.

Asked for 2026-10-05: "股票代码 should have auto complete and stop analyze if the
ticker is not found". The first two runs on the free tier died in 7 and 9
seconds on "NIFTY50" (an index) and "TCS" (Tata Consultancy is TCS.NS). Both
were refunded (``agents.is_no_price_data``), but the reader still waited for a
run that could never work, and only learned what to type afterwards.

Both answers come from Yahoo, because Yahoo is where the run's prices come from:

* **Suggestions are Yahoo's own search** (``/v1/finance/search``). "TCS" finds
  TCS.NS on India's NSE first, "tata consult" finds it by name, and "600519"
  finds 600519.SS. Only what the run form accepts is offered: equities and ETFs
  whose symbol passes ``agents.valid_ticker``, so an index (^NSEI) or a
  digit-first listing (0700.HK) is never suggested just to be refused. The
  followed companies and SEC's list are the fallback when Yahoo cannot be
  asked. They hold no foreign listing, which is why they are not first.
* **The check asks for a month of daily prices** (``/v8/finance/chart``). A 404,
  or an answer with no rows, is ``missing``, and the route refuses the run
  before the quota is touched. Anything else (a timeout, a 5xx, a 429, the
  breaker open) is ``unknown``, and the run goes ahead. A check that cannot be
  made must not stop a run that might work, and a ticker with truly no prices
  is still refunded by ``agents.is_no_price_data``. A-share codes are not
  checked: TradingAgents reads them through its own a_stock vendor, about which
  a Yahoo 404 says nothing.

Measured from the box on 2026-10-05, both endpoints answer in 0.03-0.08 s and
need no cookie or crumb. They are called directly through curl_cffi as Chrome,
not through yfinance, whose 30-second timeout is fixed and would hold a submit
that long. Each call gets 4 seconds and no retry, under its own breaker
(``yahoo-lookup``), so a slow lookup cannot cool down the data warmers' Yahoo
traffic.

Answers are cached per process: a search for 6 hours, "found" for a day, and
"missing" for 30 minutes, since a listing can begin trading.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import quote

log = logging.getLogger(__name__)

PROVIDER = "yahoo-lookup"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TIMEOUT_SECONDS = 4
MAX_RESULTS = 8

SEARCH_TTL_SECONDS = 6 * 3600
FOUND_TTL_SECONDS = 24 * 3600
MISSING_TTL_SECONDS = 30 * 60
_CACHE_MAX = 2000

#: What a run can analyse. A fund has prices but no company, and an index,
#: future or currency cannot even be typed into the form.
SUGGEST_TYPES = frozenset({"EQUITY", "ETF"})

FOUND, MISSING, UNKNOWN = "found", "missing", "unknown"

#: Letters, digits and the punctuation of company names, in ASCII: Yahoo's
#: search gave no answer for 贵州茅台 from the box (2026-10-05), so a query
#: outside this is answered with nothing rather than sent.
_QUERY_RE = re.compile(r"^[A-Za-z0-9 .&'\-]{1,40}$")

_lock = threading.Lock()
_searches: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_verdicts: dict[str, tuple[float, str]] = {}
_session = None


# ── Pure ────────────────────────────────────────────────────────────────────

def parse_search(payload: Any, accept: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Yahoo's search answer as suggestions the run form will take.

    Each is ``{"ticker", "name", "exchange", "type"}``, in Yahoo's order (its
    relevance), at most :data:`MAX_RESULTS`, one per symbol. ``accept`` is the
    form's own validator, so nothing is offered that the form would refuse.
    """
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in quotes if isinstance(quotes, list) else []:
        if not isinstance(q, dict):
            continue
        symbol = str(q.get("symbol") or "").strip().upper()
        kind = str(q.get("quoteType") or "").upper()
        if not symbol or symbol in seen or kind not in SUGGEST_TYPES or not accept(symbol):
            continue
        seen.add(symbol)
        out.append({
            "ticker": symbol,
            "name": " ".join(str(q.get("longname") or q.get("shortname") or "").split()),
            "exchange": str(q.get("exchDisp") or q.get("exchange") or "").strip(),
            "type": kind,
        })
        if len(out) >= MAX_RESULTS:
            break
    return out


def parse_chart(status: int, payload: Any) -> str:
    """:data:`FOUND` if a chart answer carries price rows, :data:`MISSING` if
    Yahoo says there are none, :data:`UNKNOWN` for anything else."""
    if status == 404:
        return MISSING
    if status != 200 or not isinstance(payload, dict):
        return UNKNOWN
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return UNKNOWN
    if chart.get("error"):
        return MISSING
    results = chart.get("result")
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict):
        return UNKNOWN
    return FOUND if first.get("timestamp") else MISSING


def local_matches(query: str, rows: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """Suggestions from a local list of ``(ticker, name, exchange)``: tickers
    starting with the query first, then names containing it."""
    q = query.strip().upper()
    if not q:
        return []
    prefix = [r for r in rows if r[0].startswith(q)]
    named = [r for r in rows if not r[0].startswith(q) and q in r[1].upper()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ticker, name, exchange in prefix + named:
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append({"ticker": ticker, "name": name, "exchange": exchange, "type": ""})
        if len(out) >= MAX_RESULTS:
            break
    return out


# ── Yahoo ───────────────────────────────────────────────────────────────────

def _http():
    """One session per process: curl_cffi as Chrome where it imports, which is
    how the endpoints were measured, else plain requests."""
    global _session
    if _session is None:
        try:
            from curl_cffi import requests as curl_requests

            _session = curl_requests.Session(impersonate="chrome")
        except Exception:  # noqa: BLE001 - any import trouble means plain requests
            import requests

            _session = requests.Session()
    return _session


def _get(url: str, params: dict[str, Any]) -> tuple[int, Any]:
    """One bounded call. Raises on a transport failure or an open breaker."""
    from ystocker import fetchguard

    resp = fetchguard.request(PROVIDER, url, session=_http(), params=params,
                              timeout=TIMEOUT_SECONDS, retries=0,
                              raise_for_status=False)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def _remember(store: dict, key: str, value: Any) -> None:
    with _lock:
        if len(store) >= _CACHE_MAX:
            for old in sorted(store, key=lambda k: store[k][0])[:_CACHE_MAX // 10]:
                store.pop(old, None)
        store[key] = (time.time(), value)


def _recall(store: dict, key: str, ttl: float) -> Optional[Any]:
    with _lock:
        hit = store.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _accept(symbol: str) -> bool:
    # Through the package attribute rather than ``from ystocker.agents import``:
    # tests/test_report_email.py puts a stub at sys.modules["ystocker.agents"].
    from ystocker import agents

    return agents.valid_ticker(symbol)


def _local_rows(followed: Optional[Callable[[], list[tuple[str, str, str]]]]
                ) -> list[tuple[str, str, str]]:
    """The followed companies (from the caller, which holds the ticker cache)
    and SEC's list, as ``(ticker, name, exchange)``."""
    rows: list[tuple[str, str, str]] = []
    if followed is not None:
        try:
            rows.extend(followed())
        except Exception as exc:  # noqa: BLE001 - a fallback must not fail
            log.debug("symbols: followed companies unavailable: %s", exc)
    try:
        from ystocker import directory

        got = directory.peek()
        for r in (got or {}).get("rows") or []:
            rows.append((r["t"], r["n"], r["x"]))
    except Exception as exc:  # noqa: BLE001
        log.debug("symbols: SEC directory unavailable: %s", exc)
    return [r for r in rows if _accept(r[0])]


def search(query: Any, followed: Optional[Callable[[], list[tuple[str, str, str]]]] = None
           ) -> tuple[list[dict[str, Any]], str]:
    """Suggestions for what has been typed, and where they came from
    (``yahoo``, ``local``, or ``none`` for a query not worth sending).
    ``followed`` supplies the followed companies for the local fallback."""
    q = " ".join(str(query or "").split())
    if not q or not _QUERY_RE.match(q):
        return [], "none"
    key = q.lower()
    cached = _recall(_searches, key, SEARCH_TTL_SECONDS)
    if cached is not None:
        return cached, "yahoo"
    try:
        status, body = _get(SEARCH_URL, {"q": q, "quotesCount": 10,
                                         "newsCount": 0, "listsCount": 0})
        if status == 200 and isinstance(body, dict):
            found = parse_search(body, _accept)
            _remember(_searches, key, found)
            return found, "yahoo"
        log.info("symbols: search for %r answered HTTP %s", q, status)
    except Exception as exc:  # noqa: BLE001 - fall back to what is on disk
        log.info("symbols: search for %r failed (%s)", q, exc)
    return local_matches(q, _local_rows(followed)), "local"


def check(ticker: Any) -> str:
    """:data:`FOUND`, :data:`MISSING` or :data:`UNKNOWN` for a symbol the form
    accepted. Only :data:`MISSING` stops a run."""
    from ystocker import agents

    symbol = str(ticker or "").strip().upper()
    if not _accept(symbol) or agents.is_a_share(symbol):
        return UNKNOWN
    cached = _recall(_verdicts, symbol, FOUND_TTL_SECONDS)
    if cached == FOUND:
        return FOUND
    cached = _recall(_verdicts, symbol, MISSING_TTL_SECONDS)
    if cached == MISSING:
        return MISSING
    try:
        status, body = _get(CHART_URL.format(symbol=quote(symbol, safe="")),
                            {"range": "1mo", "interval": "1d"})
    except Exception as exc:  # noqa: BLE001 - cannot tell, so do not stop the run
        log.info("symbols: price check for %s failed (%s); letting it run", symbol, exc)
        return UNKNOWN
    verdict = parse_chart(status, body)
    if verdict != UNKNOWN:
        _remember(_verdicts, symbol, verdict)
    log.info("symbols: %s is %s (HTTP %s)", symbol, verdict, status)
    return verdict
