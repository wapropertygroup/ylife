"""
ystocker.data
~~~~~~~~~~~~~
Fetches financial metrics from Yahoo Finance for a single ticker.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import yfinance as yf

from ystocker import fetchguard

log = logging.getLogger(__name__)

#: Breaker/back-off identity for every Yahoo call made through this module.
PROVIDER = "yahoo"

#: Yahoo requests time out after 30 seconds, and that is not configurable.
#:
#: yfinance enforces it itself: ``_make_request`` builds
#: ``request_args = {'url':…, 'params':…, 'timeout': timeout}`` and passes it on
#: every call, with ``timeout`` defaulting to 30 in ``get``, ``post``,
#: ``_make_request``, ``_get_cookie_and_crumb`` and both cookie/crumb
#: strategies. Verified in 1.6.0 and 1.7.0.
#:
#: There used to be a ``YF_TIMEOUT_SECONDS`` here, first used to build our own
#: curl_cffi session and later to stamp ``_session.timeout`` on yfinance's. Both
#: were pointless: an explicit per-request ``timeout=30`` overrides a session
#: default, so the value never took effect either way. `YfConfig.network` exposes
#: only ``proxy`` and ``retries``, so there is no supported knob to lower it, and
#: a setting that silently does nothing is worse than no setting at all.
#:
#: 30s bounded is fine for the purpose the old constant claimed — a background
#: fetch cannot hang forever — and gunicorn's --timeout 120 bounds the request
#: path independently.

#: Per-ticker exponential back-off, shared by every consumer of `fetch_group` --
#: the 8-hour full warm and the 5-minute rolling refresher both consult it, so a
#: symbol that keeps failing in one is skipped by the other. Persisted, so a
#: delisted symbol stays skipped across a deploy instead of the whole dead set
#: being retried at once on the next restart.
TICKER_BACKOFF = fetchguard.FailureBackoff("tickers", base_seconds=120, max_seconds=3600)


class FetchError(Exception):
    """Raised when Yahoo Finance data cannot be retrieved."""


_yf_fork_pid: int | None = None
_yf_fork_guard = threading.Lock()


def reset_yf_for_process() -> bool:
    """Give this process its own ``YfData`` singleton. Idempotent, cheap.

    Must be called in a forked worker before it makes any yfinance call. Returns
    True if a reset actually happened.

    Disabling our own curl_cffi session was not enough, because yfinance builds
    one itself. In 1.6.0 ``YfData.__init__`` ran
    ``self._set_session(session or requests.Session(impersonate="chrome"))`` where
    that ``requests`` *is* curl_cffi; in 1.7.0 it is ``session or new_session()``,
    which returns a curl_cffi session whenever curl_cffi imports — and it is still
    a required dependency, so it does. Either way the handle is libcurl's. So under
    --preload the master's background threads instantiate the singleton, and every
    forked worker inherits it holding

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

    It then builds that instance eagerly rather than leaving it to the first
    caller, so the session belongs to this pid from the outset. ``YfData.__init__``
    performs no network I/O — it only constructs the session — so this costs
    nothing. No timeout is set on it; yfinance passes an explicit per-request one
    that would override anything we put on the session (see the note above).

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

            # Build this process's instance now rather than leaving it to the
            # first caller, so the session belongs to this pid from the outset.
            # No timeout is set on it: see the note on _make_request below.
            YfData()
            _yf_fork_pid = pid
            log.info("yfinance state reset for pid %d", pid)
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


def day_change_pct(info: dict, price: float | None = None) -> float | None:
    """
    Today's price change as a percentage, e.g. 1.23 means +1.23%.

    Prefers Yahoo's own ``regularMarketChangePercent``; funds and some foreign
    listings omit it, so this falls back to deriving it from *price* (or
    `latest_price(info)` when not given) against whichever previous-close field
    Yahoo populated. Shared by the main dashboard's ticker cache and
    ``funddata``'s per-symbol quotes so "today's change" means the same thing
    in both places rather than drifting into two slightly different formulas.
    """
    pct = info.get("regularMarketChangePercent")
    if pct is not None:
        return round(pct, 2)
    if price is None:
        price = latest_price(info)
    prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
    if price and prev_close and prev_close > 0:
        return round((price - prev_close) / prev_close * 100, 2)
    return None


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


# ---------------------------------------------------------------------------
# Currency — the "$B" and price fields must actually be dollars
# ---------------------------------------------------------------------------
# PEER_GROUPS gained non-USD tickers when the Nikkei group was added (see
# ystocker/__init__.py), and Yahoo reports `marketCap`, `enterpriseValue`,
# `ebitda`, `freeCashflow` and every price in the listing's own currency. Left
# raw, Toyota rendered as "36899.2 $B" against Microsoft's "3800.0 $B" — a
# thousandfold error in a column headed "$B", which sorts to the top of every
# table and silently poisons anything that cap-weights.
#
# Failure blanks the field rather than passing the local-currency number
# through: a missing cap shows as "—", which is recoverable, while a wrong one
# is not distinguishable from a real one by anybody reading the page.
#
# Only non-USD tickers touch the network. A USD listing short-circuits to 1.0,
# so the ~230 existing tickers cost exactly what they did before.
_FX_TTL_SECONDS = 6 * 60 * 60
_fx_lock = threading.Lock()
_fx_cache: dict[str, tuple[float | None, float]] = {}


def usd_rate(currency: str | None) -> float | None:
    """Multiplier taking *currency* to USD, or None if it cannot be determined.

    ``usd_rate("JPY")`` is about 0.0065, so ``jpy_value * usd_rate("JPY")`` is
    dollars. USD (and a missing currency, which Yahoo only omits for US
    listings) returns 1.0 without a request.

    A negative result is cached as well as a positive one, so a delisted or
    unsupported pair is not retried on every ticker in the batch.
    """
    if not currency:
        return 1.0
    code = currency.strip().upper()
    if code in ("USD", ""):
        return 1.0

    now = time.time()
    with _fx_lock:
        hit = _fx_cache.get(code)
        if hit and (now - hit[1]) < _FX_TTL_SECONDS:
            return hit[0]

    rate: float | None = None
    try:
        # "{CUR}USD=X" quotes CUR->USD directly, which is the multiplier wanted.
        # The inverse pair ("JPY=X" is USD/JPY) would need a division and reads
        # backwards at the call site.
        info = yf.Ticker(f"{code}USD=X").info
        got = info.get("regularMarketPrice") or info.get("previousClose")
        if isinstance(got, (int, float)) and got > 0:
            rate = float(got)
            log.info("FX: %s -> USD = %.6g", code, rate)
        else:
            log.warning("FX: %sUSD=X returned no price — $ fields will be blank", code)
    except Exception as exc:  # noqa: BLE001 - a blank field beats a wrong one
        log.warning("FX: could not price %s -> USD: %s", code, exc)

    with _fx_lock:
        _fx_cache[code] = (rate, now)
    return rate


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
        # No session argument: yfinance builds its own, and
        # reset_yf_for_process() has already made sure that one belongs to this
        # process rather than being inherited from the gunicorn master.
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
    day_chg_pct = day_change_pct(info, current_price)

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

    # Everything below headed "$" or "$B" must be dollars. `fx` is 1.0 for the
    # US listings that make up almost all of PEER_GROUPS, so this is a no-op for
    # them; for a JPY line it is ~0.0065, and None if the pair could not be
    # priced, which blanks those fields instead of shipping a 150x error.
    #
    # Ratios are deliberately left alone: PE, PEG, EV/EBITDA, P/S, P/B, every
    # growth and return percentage and `upside` above all divide one local
    # figure by another, so the currency cancels and converting would be a
    # second, opposite bug.
    fx = usd_rate(info.get("currency"))

    def _usd(value: Any) -> float | None:
        """Price-like field in USD. A no-op on the USD path, deliberately.

        Not rounded when fx == 1.0: these were full-precision floats before this
        function existed and a sub-cent price would round to 0.00, which reads as
        free and makes `price / multiple` a division by zero downstream.
        """
        if value is None or fx is None:
            return None
        if fx == 1.0:
            return value
        try:
            return round(float(value) * fx, 4)
        except (TypeError, ValueError):
            return None

    def _usd_b(value: Any) -> float | None:
        if value is None or fx is None:
            return None
        try:
            return round(float(value) * fx / 1e9, 1)
        except (TypeError, ValueError):
            return None

    return {
        "Ticker":              ticker,
        "Name":                info.get("shortName", ticker),
        "Current Price":       _usd(current_price),
        "Target Price":        _usd(target_price),
        "Upside (%)":          upside,
        "PE (TTM)":            pe_ttm,
        "PE (Forward)":        pe_fwd,
        "PEG":                 peg,
        "Market Cap ($B)":     _usd_b(market_cap),
        "EPS Growth TTM (%)":  round(earnings_growth_ttm * 100, 1) if earnings_growth_ttm is not None else None,
        "EPS Growth Q (%)":    round(earnings_growth_q   * 100, 1) if earnings_growth_q   is not None else None,
        "Day Change (%)":      day_chg_pct,
        "EV/EBITDA":           round(info.get("enterpriseToEbitda"), 1) if info.get("enterpriseToEbitda") is not None else None,
        "EV ($B)":             _usd_b(info.get("enterpriseValue")),
        "EBITDA ($B)":         _usd_b(info.get("ebitda")),
        "P/S Ratio":          ps_ratio(info),
        "P/B Ratio":          round(info.get("priceToBook"), 2) if info.get("priceToBook") else None,
        "FCF ($B)":           _usd_b(info.get("freeCashflow")),
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
