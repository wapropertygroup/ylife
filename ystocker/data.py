"""
ystocker.data
~~~~~~~~~~~~~
Fetches financial metrics from Yahoo Finance for a single ticker.
"""
from __future__ import annotations

import logging
import os
import threading

import yfinance as yf

from ystocker import fetchguard

log = logging.getLogger(__name__)

#: Breaker/back-off identity for every Yahoo call made through this module.
PROVIDER = "yahoo"

#: Request timeout applied to yfinance's own session, in seconds.
#:
#: Note yfinance >=1.x does *not* need this to avoid hanging: every request path
#: in ``yfinance.data`` already defaults to ``timeout=30`` (verified on the
#: deployed 1.6.0 — ``get``, ``post``, ``_make_request``,
#: ``_get_cookie_and_crumb`` and ``_get_cookie_basic`` all carry it), and
#: curl_cffi's own session default is 30s too. The "yfinance sets no timeout, so
#: a daemon thread blocks forever" problem was real under the old
#: requests-based 0.2.x and is not real now.
#:
#: So this only tightens 30s to something shorter, which is worth having for
#: background fetches but is emphatically not worth handing yfinance a session of
#: our own to achieve. Doing that cost two outages: see reset_yf_for_process.
#: It is applied to the session yfinance itself creates, so there is still
#: exactly one session per process.
YF_TIMEOUT_SECONDS = fetchguard.env_float("YF_TIMEOUT_SECONDS", 15.0, 1.0)


class FetchError(Exception):
    """Raised when Yahoo Finance data cannot be retrieved."""


_yf_fork_pid: int | None = None
_yf_fork_guard = threading.Lock()


def reset_yf_for_process() -> bool:
    """Give this process its own ``YfData`` singleton. Idempotent, cheap.

    Must be called in a forked worker before it makes any yfinance call. Returns
    True if a reset actually happened.

    Disabling our own curl_cffi session was not enough, because yfinance builds
    one itself: ``YfData.__init__`` runs
    ``self._set_session(session or requests.Session(impersonate="chrome"))``,
    and in yfinance >=1.x that ``requests`` *is* curl_cffi. So under --preload the
    master's background threads instantiate the singleton, and every forked worker
    inherits it holding

      * a libcurl handle owned by the parent — not fork-safe, which is the
        SIGSEGV, and
      * two ``threading.Lock`` objects, ``YfData._cookie_lock`` and
        ``SingletonMeta._lock``. A lock inherited in the *held* state can never be
        released, because the thread that held it does not exist in the child. The
        worker blocks in ``_get_cookie_and_crumb`` until gunicorn's --timeout 120
        aborts it, which is the hang: ``handle_abort`` -> SystemExit -> "Worker
        exiting", over and over, with requests queueing behind it.

    Clearing ``_instances`` makes the next ``YfData()`` build a fresh instance
    with a fresh cookie lock and a session belonging to this process. The
    metaclass lock is replaced outright rather than cleared, since there is no way
    to release a lock this process never acquired.

    It then builds that instance eagerly and stamps ``YF_TIMEOUT_SECONDS`` on its
    session. Eagerly, because doing it here is the only moment there is exactly
    one session and nothing is using it yet; ``YfData.__init__`` performs no
    network I/O, it only constructs the session, so this costs nothing.

    This is also why we no longer hand yfinance a session of our own. Passing one
    per thread would have each of the master's background threads rebind the
    *shared* singleton to its own session, so a thread could issue a request on
    another thread's libcurl handle. One session per process, created by yfinance,
    configured here, is both simpler and what upstream's "one session, one cookie,
    shared by all threads" design assumes.

    Cost is one Yahoo cookie/crumb negotiation per worker lifetime. Workers
    recycle every ~200 requests, so that is negligible against a crash loop.
    """
    global _yf_fork_pid
    pid = os.getpid()
    if _yf_fork_pid == pid:
        return False
    with _yf_fork_guard:
        if _yf_fork_pid == pid:          # another thread won the race
            return False
        try:
            from yfinance.data import SingletonMeta, YfData

            # Order matters: take the new lock first, so nothing can block on the
            # inherited one while _instances is being emptied.
            SingletonMeta._lock = threading.Lock()
            SingletonMeta._instances.clear()

            # Build this process's instance now, and tighten its timeout.
            # yfinance's own default is 30s; anything we set is a floor on how
            # long a background fetch can stall.
            inst = YfData()
            try:
                inst._session.timeout = YF_TIMEOUT_SECONDS
            except Exception as exc:  # noqa: BLE001 - private attr, upstream may move it
                log.debug("yfinance: could not set session timeout (%s); "
                          "leaving upstream's 30s", exc)
            _yf_fork_pid = pid
            log.info("yfinance state reset for pid %d (session timeout %.0fs)",
                     pid, YF_TIMEOUT_SECONDS)
            return True
        except Exception as exc:  # noqa: BLE001 - never block a request over this
            log.warning("yfinance reset failed for pid %d (%s) — continuing", pid, exc)
            _yf_fork_pid = pid
            return False


# ── Field helpers ───────────────────────────────────────────────────────────
# Yahoo silently changes the units and availability of `info` fields. Each
# helper below normalises one such field and is shared by every call site, so a
# future change is fixed in one place instead of three.

def latest_price(info: dict) -> float | None:
    """
    Latest market price, falling back through Yahoo's variants.

    ETFs report no `currentPrice`, so `navPrice` / `previousClose` are needed.
    """
    return (info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("navPrice")
            or info.get("previousClose"))


def dividend_yield_pct(info: dict, price: float | None = None) -> float | None:
    """
    Annual dividend yield as a percentage — 2.44 means 2.44%.

    Yahoo's `dividendYield` used to be a decimal (0.0244) and is now already a
    percentage (2.44), so multiplying by 100 inflated every yield 100x (MSFT
    reported 73% instead of 0.73%). A magnitude heuristic cannot separate the
    two scales, because a low-yield value like 0.73 is plausible under either
    reading. So derive the yield from `dividendRate` (dollars per share), which
    is unit-unambiguous and immune to another scale flip, and fall back to
    `dividendYield` as-is only when the rate is missing — ETFs report no
    `dividendRate`, but their `dividendYield` is a percentage too.

    Note this is NOT interchangeable with the ETF-only `yield` field, which is
    still a decimal and does need the * 100.
    """
    if price is None:
        price = latest_price(info)
    rate = info.get("dividendRate")
    if rate and price:
        return round(rate / price * 100, 2)
    dy = info.get("dividendYield")
    return round(dy, 2) if dy else None


def ps_ratio(info: dict) -> float | None:
    """
    Price-to-sales ratio (trailing twelve months).

    Yahoo stopped populating `priceToSalesTrailingTwelveMonths` — it is null for
    every ticker as of 2026-08 — which silently blanked P/S everywhere it was
    displayed. Fall back to marketCap / totalRevenue, which is the same ratio.
    Both are null for ETFs, so ETFs correctly stay None.
    """
    ps = info.get("priceToSalesTrailingTwelveMonths")
    if ps:
        return round(ps, 2)
    market_cap = info.get("marketCap")
    revenue    = info.get("totalRevenue")
    if market_cap and revenue and revenue > 0:
        return round(market_cap / revenue, 2)
    return None


def fetch_ticker_data(ticker: str) -> dict:
    """
    Return a flat dict of key valuation metrics for *ticker*.

    Keys returned
    -------------
    Ticker          str   - uppercase symbol
    Name            str   - company short name
    Current Price   float - latest market price (USD)
    Target Price    float - analyst consensus 12-month target (USD)
    Upside (%)      float - (target - current) / current * 100
    PE (TTM)        float - trailing twelve-month price/earnings
    PE (Forward)    float - forward (next-12-month) price/earnings
    PEG             float - PE-to-growth ratio (trailing)
    Market Cap ($B) float - market capitalisation in billions USD

    Any value that Yahoo Finance does not provide is returned as None.
    Raises FetchError if the network request fails entirely, including when the
    Yahoo circuit breaker is open -- callers already handle FetchError, and a
    cool-down is just another reason the data is not available right now.
    """
    try:
        fetchguard.guard(PROVIDER)
    except fetchguard.CooldownActive as exc:
        raise FetchError(str(exc)) from exc

    try:
        # No session argument: yfinance builds its own curl_cffi session, and
        # reset_yf_for_process() has already given this process a private one
        # carrying YF_TIMEOUT_SECONDS.
        info = yf.Ticker(ticker).info
    except Exception as exc:
        # yfinance flattens HTTP status into generic exceptions, so the only
        # signal that this was a rate-limit rather than a bad symbol is the
        # message text. Worth checking: one 429 seen early saves the rest of the
        # batch from walking into the same wall.
        if _looks_rate_limited(exc):
            fetchguard.trip(PROVIDER, fetchguard.FETCH_RATE_LIMIT_COOLDOWN_SECONDS,
                            "yfinance rate limit")
        raise FetchError(f"Could not fetch data for {ticker}: {exc}") from exc

    current_price = latest_price(info)

    # Day change %: use Yahoo's pre-computed value first, fall back to manual calc
    day_change_pct = info.get("regularMarketChangePercent")
    if day_change_pct is None:
        prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
        if current_price and prev_close and prev_close > 0:
            day_change_pct = round((current_price - prev_close) / prev_close * 100, 2)
    else:
        day_change_pct = round(day_change_pct, 2)

    target_price  = info.get("targetMeanPrice")
    pe_ttm        = info.get("trailingPE")
    pe_fwd        = info.get("forwardPE")
    market_cap    = info.get("marketCap")

    # Growth rates (decimal → percentage)
    earnings_growth_ttm = info.get("earningsGrowth")           # TTM YoY, e.g. 0.25 = 25%
    earnings_growth_q   = info.get("earningsQuarterlyGrowth")  # most recent quarter YoY

    # PEG: prefer yfinance's own value; fall back to PE(TTM) / (earningsGrowth * 100)
    peg = info.get("pegRatio")
    if peg is None and pe_ttm is not None:
        growth = earnings_growth_ttm if earnings_growth_ttm is not None else earnings_growth_q
        if growth and growth > 0:
            peg = round(pe_ttm / (growth * 100), 2)
            log.debug("%s: PEG calculated from PE(%.1f) / growth(%.1f%%) = %.2f",
                      ticker, pe_ttm, growth * 100, peg)
        else:
            log.debug("%s: PEG unavailable - no earnings growth data", ticker)

    upside = None
    if current_price and target_price:
        upside = (target_price - current_price) / current_price * 100

    return {
        "Ticker":              ticker,
        "Name":                info.get("shortName", ticker),
        "Current Price":       current_price,
        "Target Price":        target_price,
        "Upside (%)":          upside,
        "PE (TTM)":            pe_ttm,
        "PE (Forward)":        pe_fwd,
        "PEG":                 peg,
        "Market Cap ($B)":     round(market_cap / 1e9, 1) if market_cap else None,
        "EPS Growth TTM (%)":  round(earnings_growth_ttm * 100, 1) if earnings_growth_ttm is not None else None,
        "EPS Growth Q (%)":    round(earnings_growth_q   * 100, 1) if earnings_growth_q   is not None else None,
        "Day Change (%)":      day_change_pct,
        "EV/EBITDA":           round(info.get("enterpriseToEbitda"), 1) if info.get("enterpriseToEbitda") is not None else None,
        "EV ($B)":             round(info.get("enterpriseValue") / 1e9, 1) if info.get("enterpriseValue") else None,
        "EBITDA ($B)":         round(info.get("ebitda") / 1e9, 1) if info.get("ebitda") else None,
        "P/S Ratio":          ps_ratio(info),
        "P/B Ratio":          round(info.get("priceToBook"), 2) if info.get("priceToBook") else None,
        "FCF ($B)":           round(info.get("freeCashflow") / 1e9, 1) if info.get("freeCashflow") else None,
        "Short Float (%)":    round(info.get("shortPercentOfFloat") * 100, 1) if info.get("shortPercentOfFloat") else None,
        "Dividend Yield (%)": dividend_yield_pct(info, current_price),
        "Revenue Growth (%)": round(info.get("revenueGrowth") * 100, 1) if info.get("revenueGrowth") else None,
        "52W Return (%)":  round(info.get("52WeekChange") * 100, 1) if info.get("52WeekChange") else None,
        "YTD Return (%)":  round(info.get("ytdReturn") * 100, 1) if info.get("ytdReturn") else None,
    }


def _looks_rate_limited(exc: BaseException) -> bool:
    """Best-effort detection of a Yahoo rate-limit hiding inside a generic error."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        needle in text
        for needle in ("429", "too many requests", "rate limit", "rate-limited")
    )


def fetch_group(tickers: list[str]) -> tuple[dict[str, dict], list[str]]:
    """
    Fetch data for every ticker in *tickers*.

    Returns (results, errors) where:
      results - {ticker: data_dict} for every ticker that succeeded
      errors  - list of error message strings for tickers that failed

    Also maintains :data:`TICKER_BACKOFF`. That bookkeeping lives here rather
    than in the callers because only this loop knows which tickers it actually
    *attempted*: a ticker skipped because the breaker opened part-way through was
    never asked about, and recording a failure against it would blame a symbol
    for a provider outage and double its back-off for nothing.

    Two provider-level behaviours also belong here, because both only make sense
    across a batch:

    * If the breaker is already open, return immediately instead of walking the
      whole list. Each iteration would otherwise fail instantly *and still sleep
      0.5s*, turning a cool-down into minutes of doing nothing slowly.
    * If every ticker in a batch of three or more fails, treat that as the
      provider being unwell rather than coincidence, and trip the breaker.
      Individual symbols fail all the time -- delistings, renames, thin ETFs --
      so a *unanimous* failure is the only reliable signal available here.
    """
    import time
    results: dict[str, dict] = {}
    errors: list[str] = []

    remaining = fetchguard.cooldown_remaining(PROVIDER)
    if remaining > 0:
        log.info("Yahoo cool-down active (%.0fs) — skipping batch of %d", remaining, len(tickers))
        return results, [f"Yahoo cool-down active for {remaining:.0f}s"]

    attempted: list[str] = []
    for i, t in enumerate(tickers):
        if i > 0:
            time.sleep(0.5)  # Add delay to avoid rate limiting
        attempted.append(t)
        try:
            results[t] = fetch_ticker_data(t)
        except FetchError as exc:
            errors.append(str(exc))
            # A breaker tripped mid-batch (by this call or another thread) means
            # the rest of the list is wasted effort.
            if fetchguard.cooldown_remaining(PROVIDER) > 0:
                log.warning("Yahoo cool-down opened mid-batch — abandoning %d remaining",
                            len(tickers) - i - 1)
                break

    if len(attempted) >= 3 and not results:
        fetchguard.trip(PROVIDER, fetchguard.FETCH_ERROR_COOLDOWN_SECONDS,
                        f"all {len(attempted)} tickers in batch failed")

    TICKER_BACKOFF.record_batch(attempted, results.keys())
    return results, errors
