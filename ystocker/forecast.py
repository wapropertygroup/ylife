"""
ystocker.forecast
~~~~~~~~~~~~~~~~~
Multi-model price forecasting using:
  - Prophet  (Facebook/Meta time-series)
  - ARIMA    (statsmodels AutoARIMA via pmdarima)
  - Linear   (simple linear-regression baseline)

All models train on ~2 years of weekly closing prices and produce
a 90-day (≈13-week) forward forecast with 80% confidence intervals.

Returns a plain dict ready for jsonify().
"""
from __future__ import annotations

import logging
import warnings
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

FORECAST_WEEKS = 26     # ~6 months forward
TRAIN_PERIOD   = "3y"   # how much history to train on
INTERVAL       = "1wk"  # weekly bars


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(s: pd.Series) -> pd.Series:
    """Forward-fill then drop remaining NaNs."""
    return s.ffill().dropna()


def _to_records(dates: list[str], vals: list, lo: list, hi: list) -> list[dict]:
    return [
        {"date": d, "value": round(float(v), 2),
         "lo": round(float(l), 2), "hi": round(float(h), 2)}
        for d, v, l, h in zip(dates, vals, lo, hi)
    ]


# ---------------------------------------------------------------------------
# Model 1 — Prophet
# ---------------------------------------------------------------------------

def _prophet_forecast(prices: pd.Series, horizon: int) -> tuple[list, list, list]:
    """Returns (dates, yhat, yhat_lower, yhat_upper) as lists of str / float."""
    from prophet import Prophet  # lazy import — heavyweight

    df = pd.DataFrame({"ds": prices.index.tz_localize(None), "y": prices.values})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = Prophet(
            daily_seasonality=False,
            weekly_seasonality=False,
            yearly_seasonality=True,
            changepoint_prior_scale=0.05,
            interval_width=0.80,
        )
        m.fit(df)

    future  = m.make_future_dataframe(periods=horizon, freq="W")
    forecast = m.predict(future).tail(horizon)

    dates = [str(d.date()) for d in forecast["ds"]]
    yhat  = forecast["yhat"].clip(lower=0).tolist()
    lo    = forecast["yhat_lower"].clip(lower=0).tolist()
    hi    = forecast["yhat_upper"].clip(lower=0).tolist()
    return dates, yhat, lo, hi


# ---------------------------------------------------------------------------
# Model 2 — ARIMA (via pmdarima auto_arima)
# ---------------------------------------------------------------------------

def _arima_forecast(prices: pd.Series, horizon: int) -> tuple[list, list, list]:
    import pmdarima as pm  # lazy import

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = pm.auto_arima(
            prices.values,
            seasonal=False,
            stepwise=True,
            suppress_warnings=True,
            error_action="ignore",
            max_p=5, max_q=5, max_d=2,
        )
        fc, conf = model.predict(n_periods=horizon, return_conf_int=True, alpha=0.20)

    last_date = prices.index[-1]
    future_dates = [
        str((last_date + timedelta(weeks=i + 1)).date())
        for i in range(horizon)
    ]
    lo = np.clip(conf[:, 0], 0, None).tolist()
    hi = conf[:, 1].tolist()
    return future_dates, np.clip(fc, 0, None).tolist(), lo, hi


# ---------------------------------------------------------------------------
# Model 3 — Linear regression (simple baseline)
# ---------------------------------------------------------------------------

def _linear_forecast(prices: pd.Series, horizon: int) -> tuple[list, list, list]:
    y = prices.values.astype(float)
    x = np.arange(len(y))

    # Least-squares fit
    coeffs = np.polyfit(x, y, 1)
    slope, intercept = coeffs

    # Residual std for confidence interval
    y_hat_train = np.polyval(coeffs, x)
    residuals   = y - y_hat_train
    std         = float(np.std(residuals))
    z           = 1.28  # 80% CI

    last_date = prices.index[-1]
    future_x  = np.arange(len(y), len(y) + horizon)
    fc = np.polyval(coeffs, future_x).clip(0)

    future_dates = [
        str((last_date + timedelta(weeks=i + 1)).date())
        for i in range(horizon)
    ]
    lo = (fc - z * std).clip(0).tolist()
    hi = (fc + z * std).tolist()
    return future_dates, fc.tolist(), lo, hi


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_forecast(ticker: str) -> dict:
    """
    Fetch 2y of weekly data for *ticker* and run all three models.

    Returns a dict:
    {
      "ticker": str,
      "train": [{"date": str, "value": float}, ...],   # historical weekly
      "prophet":  {"forecast": [...], "error": str|None},
      "arima":    {"forecast": [...], "error": str|None},
      "linear":   {"forecast": [...], "error": str|None},
    }
    Each forecast item: {"date": str, "value": float, "lo": float, "hi": float}
    """
    log.info("Forecast: fetching %s history (%s %s)", ticker, TRAIN_PERIOD, INTERVAL)
    try:
        hist = yf.Ticker(ticker).history(period=TRAIN_PERIOD, interval=INTERVAL)
    except Exception as exc:
        return {"error": f"Could not fetch data for {ticker}: {exc}"}

    if hist.empty:
        return {"error": f"No price history for '{ticker}'."}

    prices = _clean(hist["Close"])
    if len(prices) < 10:
        return {"error": "Not enough data to forecast."}

    # Historical series for the chart
    train = [
        {"date": str(d.date()), "value": round(float(v), 2)}
        for d, v in zip(prices.index, prices.values)
    ]

    result: dict = {"ticker": ticker, "train": train}

    # ── Prophet ─────────────────────────────────────────────────────────
    try:
        dates, yhat, lo, hi = _prophet_forecast(prices, FORECAST_WEEKS)
        result["prophet"] = {"forecast": _to_records(dates, yhat, lo, hi), "error": None}
    except ImportError:
        result["prophet"] = {"forecast": [], "error": "prophet not installed"}
    except Exception as exc:
        log.warning("Prophet forecast failed for %s: %s", ticker, exc)
        result["prophet"] = {"forecast": [], "error": str(exc)}

    # ── ARIMA ────────────────────────────────────────────────────────────
    try:
        dates, fc, lo, hi = _arima_forecast(prices, FORECAST_WEEKS)
        result["arima"] = {"forecast": _to_records(dates, fc, lo, hi), "error": None}
    except ImportError:
        result["arima"] = {"forecast": [], "error": "pmdarima not installed"}
    except Exception as exc:
        log.warning("ARIMA forecast failed for %s: %s", ticker, exc)
        result["arima"] = {"forecast": [], "error": str(exc)}

    # ── Linear ────────────────────────────────────────────────────────────
    try:
        dates, fc, lo, hi = _linear_forecast(prices, FORECAST_WEEKS)
        result["linear"] = {"forecast": _to_records(dates, fc, lo, hi), "error": None}
    except Exception as exc:
        log.warning("Linear forecast failed for %s: %s", ticker, exc)
        result["linear"] = {"forecast": [], "error": str(exc)}

    return result
