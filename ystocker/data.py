"""
ystocker.data
~~~~~~~~~~~~~
Fetches financial metrics from Yahoo Finance for a single ticker.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any

import yfinance as yf

from ystocker import fetchguard
from ystocker import health
from ystocker import earnings as earnings_mod

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

#: Yahoo quote types with no company behind them: no statements, estimates,
#: earnings dates or insider filings. Asking anyway is a request Yahoo answers
#: with a 404 ("No fundamentals data found for symbol: SPY") -- about 270 a day
#: from /history, /api/financials and the analyst sweep, measured 2026-10-04 --
#: and each one makes yfinance reset the cookie and crumb every thread in the
#: process shares, which is what the warm-up's "Invalid Crumb" 401s follow.
NON_EQUITY_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND", "MONEYMARKET", "INDEX",
                                    "CURRENCY", "CRYPTOCURRENCY", "FUTURE"})


def is_non_equity(quote_type: object) -> bool:
    """True only for a type known to have no company data. An unknown or
    missing type is False: skipping a real company's statements because one
    ``info`` call came back thin would be the worse mistake."""
    return str(quote_type or "").upper() in NON_EQUITY_QUOTE_TYPES


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

    None for a listing quoted in one currency and reporting in another, where
    the fallback would divide dollars by TWD: :func:`statement_metrics` converts
    both sides instead.
    """
    if not _same_currency(info):
        return None
    ps = info.get("priceToSalesTrailingTwelveMonths")
    if ps:
        return round(ps, 2)
    market_cap = info.get("marketCap")
    revenue    = info.get("totalRevenue")
    if market_cap and revenue and revenue > 0:
        return round(market_cap / revenue, 2)
    return None


def _same_currency(info: dict) -> bool:
    """Whether the quote and the statements are in one currency.

    A missing ``financialCurrency`` is taken as the listing's own, which is what
    Yahoo omits it for.
    """
    quote = (info.get("currency") or "").strip().upper()
    fin = (info.get("financialCurrency") or "").strip().upper()
    return not fin or not quote or fin == quote


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Margins and EV multiples, on one currency basis
# ---------------------------------------------------------------------------
# Yahoo's `info` mixes two currencies and says so only in two fields. Prices,
# `marketCap` and its own `enterpriseValue` are in the listing's `currency`;
# everything off the statements (`totalRevenue`, `ebitda`, `freeCashflow`,
# `totalDebt`, `totalCash`) is in `financialCurrency`. For a US listing of a US
# filer they agree and nothing here matters. For an ADR they do not, and
# Yahoo's own ratios are simply the two divided. Measured on the box on
# 2026-10-06: ASML (USD/EUR) had an `enterpriseValue` of 39.7 trillion against a
# $704B cap, so `enterpriseToRevenue` read 1,123 and `enterpriseToEbitda`
# 2,942; TSM's (USD/TWD) EV was 17.7 trillion, neither a dollar nor a TWD
# figure; BABA's added CNY debt to a USD cap. TSM's EBITDA and FCF were TWD
# shown as dollars, 30x too large, and P/S and P/FCF built on them were 30x off
# the other way. The same trap as TSM's P/E of 1.01 (listing.py), one field over.
#
# So for a listing whose currencies differ, EV is rebuilt from parts this file
# can convert: the cap at the quote's rate, plus debt less cash at the
# statements' rate. Where the currencies agree Yahoo's EV is kept, since it also
# counts minority interest and preferred stock, and every figure stays what it
# was. Gross margin, operating margin and FCF margin are ratios within one
# currency and need no conversion at all.


def statement_rates(info: dict) -> tuple[float | None, float | None]:
    """:func:`usd_rate` for the listing's currency and for the statements'.

    The second lookup is skipped when the two are one currency, which is every
    listing but an ADR or a foreign line like it.
    """
    fx = usd_rate(info.get("currency"))
    return fx, (fx if _same_currency(info) else usd_rate(info.get("financialCurrency")))


#: Industries whose debt is raw material rather than financing (a bank's
#: deposits, an insurer's float, a broker's client money), so enterprise value
#: does not measure what an EV multiple assumes. The same three that dca.py sends
#: to its bank template, which omits the standard DCF for the same reason. JPM
#: read EV/EBIT 7.7 without this, a plausible number that means nothing.
EV_UNDEFINED_INDUSTRIES: tuple[str, ...] = ("bank", "insurance", "capital markets")


def statement_metrics(info: dict, fx_quote: float | None,
                      fx_fin: float | None) -> dict[str, float | None]:
    """Margins and EV multiples from Yahoo's ``info``, on one currency basis. Pure.

    ``fx_quote`` and ``fx_fin`` are :func:`usd_rate` for the listing's currency
    and for the statements'. Returns USD-billion figures (``ev_b``,
    ``ebitda_b``, ``fcf_b``, ``revenue_b``), multiples (``ev_sales``,
    ``ev_ebitda``, ``ev_ebit``, ``ps``) and margins in percent
    (``gross_margin``, ``operating_margin``, ``fcf_margin``). A figure that
    cannot be put on one basis is None, never a mixed-currency number.

    Two rules beyond the currency:

    - **EBIT is operating income** (``operatingMargins`` x revenue), the usual
      proxy where no EBIT line is published, and a multiple of a loss is not a
      multiple: EV/EBIT and EV/EBITDA are None when the earnings are at or
      below zero, rather than a negative number that sorts as the cheapest.
    - **A gross margin of exactly 0.0 is not reported.** Yahoo writes 0.0 for a
      bank, which has no cost of goods (JPM, 2026-10-06), and no company sells
      at precisely cost.
    - **No EV multiple for a bank or an insurer** (:data:`EV_UNDEFINED_INDUSTRIES`).
      The EV figure itself is still given.
    """
    same = _same_currency(info)
    cap, ev_yahoo = _num(info.get("marketCap")), _num(info.get("enterpriseValue"))
    revenue, ebitda = _num(info.get("totalRevenue")), _num(info.get("ebitda"))
    fcf = _num(info.get("freeCashflow"))
    debt, cash = _num(info.get("totalDebt")), _num(info.get("totalCash"))
    gross, operating = _num(info.get("grossMargins")), _num(info.get("operatingMargins"))

    def conv(value: float | None, rate: float | None) -> float | None:
        return None if value is None or rate is None else value * rate

    # One basis for the ratios: the listing's own currency when the statements
    # share it, so a failed FX lookup costs only the $B figures; dollars when
    # they do not, which takes both rates.
    q_rate, f_rate = (1.0, 1.0) if same else (fx_quote, fx_fin)
    cap_c, rev_c, ebitda_c = conv(cap, q_rate), conv(revenue, f_rate), conv(ebitda, f_rate)
    if same:
        ev_c = ev_yahoo
    elif cap_c is not None and debt is not None and cash is not None and f_rate is not None:
        ev_c = cap_c + (debt - cash) * f_rate
    else:
        ev_c = None
    # A negative EV (more cash than the whole company is valued at) is shown as
    # a figure, but a multiple of it reads as the cheapest stock on the page.
    industry = str(info.get("industry") or "").lower()
    ev_defined = not any(word in industry for word in EV_UNDEFINED_INDUSTRIES)
    ev_pos = ev_c if ev_defined and ev_c is not None and ev_c > 0 else None
    ebit_c = rev_c * operating if rev_c is not None and operating is not None else None

    def ratio(top: float | None, bottom: float | None, digits: int) -> float | None:
        if top is None or bottom is None or bottom <= 0:
            return None
        return round(top / bottom, digits)

    def billions(value: float | None) -> float | None:
        return None if value is None else round(value / 1e9, 1)

    ps = _num(info.get("priceToSalesTrailingTwelveMonths")) if same else None
    return {
        # The basis is local currency or dollars; the quote's rate takes the
        # first to dollars and leaves the second as it is.
        "ev_b": billions(conv(ev_c, fx_quote if same else 1.0)),
        "ebitda_b": billions(conv(ebitda, fx_fin)),
        "fcf_b": billions(conv(fcf, fx_fin)),
        "revenue_b": billions(conv(revenue, fx_fin)),
        "ev_sales": ratio(ev_pos, rev_c, 2),
        "ev_ebitda": ratio(ev_pos, ebitda_c, 1),
        "ev_ebit": ratio(ev_pos, ebit_c, 1),
        "ps": round(ps, 2) if ps else ratio(cap_c, rev_c, 2),
        "gross_margin": round(gross * 100, 1) if gross else None,
        "operating_margin": round(operating * 100, 1) if operating is not None else None,
        "fcf_margin": (round(fcf / revenue * 100, 1)
                       if fcf is not None and revenue is not None and revenue > 0 else None),
    }


# Yahoo's per-share earnings can sit on a different share basis from its price,
# and nothing in `info` says so. Measured 2026-10-04: Tokio Marine (8766.T) at
# ¥507.4 against a trailing EPS of ¥279.17 and a forward EPS of ¥493.02, so
# trailingPE 1.82 and forwardPE 1.03 -- while marketCap / netIncomeToCommon,
# which no share count enters, says 26.7. One name like that is enough to move an
# index: /multiples cap-weights `cap / pe`, so Tokio Marine alone booked ~$88B of
# forward earnings into the Nikkei that do not exist. The guard's first hour in
# production caught Tokyo Electron (8035.T) too: split 5:1 on 2026-09-29, price
# and share count re-based, both EPS not -- 9.7x where cap / net income says 44.
#
# So trailingPE is checked against marketCap / netIncomeToCommon, and outside
# this band both P/Es and the PEG built from them are dropped. Across thirty
# same-currency listings surveyed that day the ratio sat between 0.90 (TSLA) and
# 1.23 (6857.T: Yahoo's TTM EPS and net income cover slightly different
# windows); a split applied to one figure and not the other is a factor of two at
# the least, which lands outside it.
PE_BASIS_BAND = (0.55, 1.8)


def pe_basis_ratio(info: dict) -> float | None:
    """``trailingPE`` over ``marketCap / netIncomeToCommon``: about 1.0 when
    Yahoo's per-share and whole-company figures agree.

    None when it cannot be measured, which is not a failure: no P/E, no cap or
    no net income, or a listing quoted in one currency and reporting in another
    (an ADR: TSM's net income is in TWD and its cap in USD, so the ratio would
    measure the exchange rate). A positive P/E against net income at or below
    zero returns 0.0 -- the two figures disagree on whether there are earnings
    at all, which is as far outside the band as anything can be.
    """
    pe = info.get("trailingPE")
    cap = info.get("marketCap")
    ni = info.get("netIncomeToCommon")
    cur = (info.get("currency") or "").strip().upper()
    fin = (info.get("financialCurrency") or "").strip().upper()
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (pe, cap, ni)):
        return None
    if pe <= 0 or cap <= 0 or not cur or cur != fin:
        return None
    if ni <= 0:
        return 0.0
    return pe / (cap / ni)


def pe_basis_ok(info: dict) -> bool:
    """False only when :func:`pe_basis_ratio` measured a disagreement."""
    ratio = pe_basis_ratio(info)
    return ratio is None or PE_BASIS_BAND[0] <= ratio <= PE_BASIS_BAND[1]


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

    # Both P/Es and the PEG divide by an EPS that may be on another share basis
    # from the price; see PE_BASIS_BAND. Blank beats a multiple off by a split.
    basis = pe_basis_ratio(info)
    if not pe_basis_ok(info):
        log.warning("%s: trailing P/E %.2f disagrees with cap / net income (ratio %.3f) -- "
                    "dropping P/E (TTM), P/E (Forward) and PEG", ticker, pe_ttm, basis)
        pe_ttm = pe_fwd = peg = None

    upside = None
    if current_price and target_price:
        upside = (target_price - current_price) / current_price * 100

    # Everything below headed "$" or "$B" must be dollars. `fx` is 1.0 for the
    # US listings that make up almost all of PEER_GROUPS, so this is a no-op for
    # them; for a JPY line it is ~0.0065, and None if the pair could not be
    # priced, which blanks those fields instead of shipping a 150x error.
    #
    # Ratios are deliberately left alone: PE, PEG, P/B, every growth and return
    # percentage and `upside` above all divide one local figure by another, so
    # the currency cancels and converting would be a second, opposite bug. The
    # EV multiples and P/S are the exception: their two halves come from
    # different currencies on an ADR, so statement_metrics converts each half.
    # The statements' own rate is a second lookup only for an ADR (TSM files in
    # TWD). See statement_metrics.
    fx, fx_fin = statement_rates(info)
    stmt = statement_metrics(info, fx, fx_fin)

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
        # Every EV figure and every statement figure on one currency basis, so
        # an ADR's EBITDA is not its TWD number headed "$B". See statement_metrics.
        "EV/EBITDA":           stmt["ev_ebitda"],
        "EV ($B)":             stmt["ev_b"],
        "EBITDA ($B)":         stmt["ebitda_b"],
        "P/S Ratio":          stmt["ps"],
        "P/B Ratio":          round(info.get("priceToBook"), 2) if info.get("priceToBook") else None,
        "FCF ($B)":           stmt["fcf_b"],
        "EV/Sales":           stmt["ev_sales"],
        "EV/EBIT":            stmt["ev_ebit"],
        "Gross Margin (%)":   stmt["gross_margin"],
        "Operating Margin (%)": stmt["operating_margin"],
        "FCF Margin (%)":     stmt["fcf_margin"],
        "Short Float (%)":    round(info.get("shortPercentOfFloat") * 100, 1) if info.get("shortPercentOfFloat") else None,
        "Dividend Yield (%)": dividend_yield_pct(info, current_price),
        "Revenue Growth (%)": round(info.get("revenueGrowth") * 100, 1) if info.get("revenueGrowth") else None,
        "52W Return (%)":  round(info.get("52WeekChange") * 100, 1) if info.get("52WeekChange") else None,
        "YTD Return (%)":  round(info.get("ytdReturn") * 100, 1) if info.get("ytdReturn") else None,
        # Balance-sheet strength and cash conversion. Costs nothing: every field
        # behind it is already in this `info` dict and was being discarded, and
        # the site could say a stock was cheap without ever saying whether the
        # company was fragile. See ystocker.health.
        "Health":          health.assess(info, sector=info.get("sector")),
        # The next reporting date, chosen by date rather than by field name —
        # Yahoo's `earningsTimestamp` is the *last* report on some tickers and
        # the next on others. See ystocker.earnings.
        "Earnings Date":   earnings_mod.next_earnings(info),
        # So the analyst sweep can skip a fund without asking Yahoo first.
        "Quote Type":      info.get("quoteType"),
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
