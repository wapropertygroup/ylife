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

The Fundamentals tab's compare box (``/api/companies/suggest``) reads the same
two sources the other way round, local first (2026-10-06, "TSMC ticker should
have auto complete"). Yahoo's search does not know TSMC is TSM: from the box it
answered with a São Paulo receipt (TSMC34.SA), a Buenos Aires one, Tesmec
(TSMCF, another company), three crypto tokens and a Korean ETF. SEC's list does
know, by initials: Taiwan Semiconductor Manufacturing Co Ltd. So
:func:`local_matches` reads a name's initials as well as its words, and Yahoo
(:func:`quotes`) only fills what the local lists leave, which is where a renamed
brand ("google" is Alphabet) or a listing SEC has never seen (005930.KS) turns
up.
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

#: What has fundamentals: a company. A fund is ``not_a_company`` on the tab.
COMPANY_TYPES = frozenset({"EQUITY"})

#: The fields :func:`parse_search` reads, all a cached answer keeps.
_QUOTE_FIELDS = ("symbol", "quoteType", "longname", "shortname", "exchDisp", "exchange")

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

def parse_search(payload: Any, accept: Callable[[str], bool],
                 types: frozenset[str] = SUGGEST_TYPES) -> list[dict[str, Any]]:
    """Yahoo's search answer as suggestions the run form will take.

    Each is ``{"ticker", "name", "exchange", "type"}``, in Yahoo's order (its
    relevance), at most :data:`MAX_RESULTS`, one per symbol. ``accept`` is the
    form's own validator, so nothing is offered that the form would refuse, and
    ``types`` the quote types it can use.
    """
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in quotes if isinstance(quotes, list) else []:
        if not isinstance(q, dict):
            continue
        symbol = str(q.get("symbol") or "").strip().upper()
        kind = str(q.get("quoteType") or "").upper()
        if not symbol or symbol in seen or kind not in types or not accept(symbol):
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


def _fold(text: str) -> str:
    """Upper case with punctuation as spaces, so "Coca-Cola" meets SEC's
    "COCA COLA CO" and "AT&T" meets "AT T INC"."""
    return " ".join(re.sub(r"[^0-9A-Z]+", " ", text.upper()).split())


def _initials(folded: str) -> str:
    """Each word's first character, from a :func:`_fold`-ed name: TSMCL for
    "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"."""
    return "".join(word[0] for word in folded.split())


def local_matches(query: str, rows: list[tuple]) -> list[dict[str, Any]]:
    """Suggestions from a local list of ``(ticker, name, exchange)``, in three
    tiers: tickers starting with the query; then, for one word of three letters
    or more, names whose initials start with it ("TSMC" is Taiwan Semiconductor
    Manufacturing Co, ticker TSM, whose name and ticker hold no "TSMC"); then
    names containing it, punctuation aside. Within a tier, the rows' order.

    A row may carry a fourth element, other names to match but not show: the
    compare box shows the ticker cache's name, which is Yahoo's short name and
    can be cut at 31 characters ("Taiwan Semiconductor Manufactur", initials
    TSM), so SEC's full name rides along for matching."""
    q = query.strip().upper()
    if not q:
        return []
    folded = _fold(q)
    abbreviation = folded if len(folded) >= 3 and folded.isalpha() else ""
    names = [[_fold(n) for n in (r[1], *(r[3] if len(r) > 3 else ()))] for r in rows] if folded else []
    prefix = [r for r in rows if r[0].startswith(q)]
    initials = [r for r, ns in zip(rows, names)
                if abbreviation and any(_initials(n).startswith(abbreviation) for n in ns)]
    named = [r for r, ns in zip(rows, names) if any(folded in n for n in ns)]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in prefix + initials + named:
        ticker, name, exchange = row[0], row[1], row[2]
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append({"ticker": ticker, "name": name, "exchange": exchange, "type": ""})
        if len(out) >= MAX_RESULTS:
            break
    return out


def merge(first: list[dict[str, Any]], then: list[dict[str, Any]],
          limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """*first*, then what *then* adds to it, one per ticker, at most *limit*."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in first + then:
        if item["ticker"] in seen:
            continue
        seen.add(item["ticker"])
        out.append(item)
        if len(out) >= limit:
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


def quotes(query: Any, budget: Optional[Callable[[], bool]] = None
           ) -> Optional[list[dict[str, Any]]]:
    """Yahoo's search answer for *query*, every quote unfiltered (callers filter
    with :func:`parse_search`), cached per query. ``None`` when Yahoo could not
    be asked: a query not worth sending, a transport failure or a non-200, or
    *budget* declining to spend a call. A cached answer spends nothing."""
    q = " ".join(str(query or "").split())
    if not q or not _QUERY_RE.match(q):
        return None
    key = q.lower()
    cached = _recall(_searches, key, SEARCH_TTL_SECONDS)
    if cached is not None:
        return cached
    if budget is not None and not budget():
        log.info("symbols: today's searches are spent; %r is answered locally", q)
        return None
    try:
        status, body = _get(SEARCH_URL, {"q": q, "quotesCount": 10,
                                         "newsCount": 0, "listsCount": 0})
    except Exception as exc:  # noqa: BLE001 - the caller falls back to what is on disk
        log.info("symbols: search for %r failed (%s)", q, exc)
        return None
    if status != 200 or not isinstance(body, dict):
        log.info("symbols: search for %r answered HTTP %s", q, status)
        return None
    raw = body.get("quotes")
    found = [{k: x[k] for k in _QUOTE_FIELDS if k in x}
             for x in (raw if isinstance(raw, list) else []) if isinstance(x, dict)]
    _remember(_searches, key, found)
    return found


def search(query: Any, followed: Optional[Callable[[], list[tuple[str, str, str]]]] = None
           ) -> tuple[list[dict[str, Any]], str]:
    """Suggestions for what has been typed, and where they came from
    (``yahoo``, ``local``, or ``none`` for a query not worth sending).
    ``followed`` supplies the followed companies for the local fallback."""
    q = " ".join(str(query or "").split())
    if not q or not _QUERY_RE.match(q):
        return [], "none"
    found = quotes(q)
    if found is not None:
        return parse_search({"quotes": found}, _accept), "yahoo"
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
