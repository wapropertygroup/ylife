"""
ystocker.data
~~~~~~~~~~~~~
Fetches financial metrics from Yahoo Finance for a single ticker.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import yfinance as yf

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when Yahoo Finance data cannot be retrieved."""


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
    Raises FetchError if the network request fails entirely.
    """
    try:
        info = yf.Ticker(ticker).info
    except Exception as exc:
        raise FetchError(f"Could not fetch data for {ticker}: {exc}") from exc

    current_price = (info.get("currentPrice")
                     or info.get("regularMarketPrice")
                     or info.get("navPrice")
                     or info.get("previousClose"))

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
    }


def fetch_group(tickers: List[str]) -> Tuple[Dict[str, dict], List[str]]:
    """
    Fetch data for every ticker in *tickers*.

    Returns (results, errors) where:
      results - {ticker: data_dict} for every ticker that succeeded
      errors  - list of error message strings for tickers that failed
    """
    results: Dict[str, dict] = {}
    errors: List[str] = []
    for t in tickers:
        try:
            results[t] = fetch_ticker_data(t)
        except FetchError as exc:
            errors.append(str(exc))
    return results, errors
