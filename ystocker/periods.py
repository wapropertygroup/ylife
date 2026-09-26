"""
ystocker.periods
~~~~~~~~~~~~~~~~
What "1W", "YTD" and "1Y" mean, once, for the sector returns on /markets.

Why this is a module
--------------------
Three sector sections sit together on /markets — the rotation grid, the
rotation ranking and the GICS panel — and a reader compares them row against
row. Each had its own idea of where a period starts:

* the rotation grid began each window at the first close *inside* it, which
  drops that session's move — for YTD, all of 2 January's;
* the ranking's "YTD" column was the first close of a one-year download, i.e. a
  **one-year return under a YTD heading** (S&P-wide, 18.7% shown where 14.5% was
  meant);
* the GICS panel used the rule below.

Fixing each in place would leave three definitions that agree today and drift
the next time one of them is touched, so there is one, and all three call it.

The rule
--------
A period's base is the **last close on or before its anniversary** — the close
that precedes the window, not the first one inside it. YTD's anniversary is 31
December of the previous year. The others are calendar offsets back from the
series' own latest close rather than from today, so on a Saturday "1W" is
Friday against the Friday before. Month arithmetic is pandas', which clamps 31
March minus a month to 28 February instead of rolling it into March.

A series that does not reach back to the anniversary returns None for that
period. The alternative — measuring from its first close — publishes a shorter
period's return under a longer period's heading, which is the ranking bug again.
"""
from __future__ import annotations

from typing import Any, Optional

PERIODS: tuple[str, ...] = ("1D", "1W", "1M", "3M", "6M", "YTD", "1Y")


def anniversary(end: Any, period: str) -> Any:
    """The calendar date a period starts from, counted back from ``end``."""
    import pandas as pd

    if period == "1W":
        return end - pd.Timedelta(days=7)
    if period == "1M":
        return end - pd.DateOffset(months=1)
    if period == "3M":
        return end - pd.DateOffset(months=3)
    if period == "6M":
        return end - pd.DateOffset(months=6)
    if period == "YTD":
        return pd.Timestamp(end.year - 1, 12, 31)
    if period == "1Y":
        return end - pd.DateOffset(years=1)
    raise ValueError(f"no anniversary for period {period!r}")


def base_date(days: Any, end: Any, period: str) -> Optional[Any]:
    """The session ``period`` starts from, among ``days`` (a sorted DatetimeIndex).

    "1D" is the previous session rather than a calendar day back, so a Monday's
    day change is against Friday. None when ``days`` does not reach back far
    enough.
    """
    if period == "1D":
        prior = days[days < end]
        return prior[-1] if len(prior) else None
    pos = days.searchsorted(anniversary(end, period), side="right") - 1
    return days[pos] if pos >= 0 else None


def period_return(closes: Any, period: str) -> Optional[float]:
    """Percent return of one close series over ``period``, ending at its last close."""
    import pandas as pd

    s = closes.dropna()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s) < 2:
        return None
    end = s.index[-1]
    start = base_date(s.index, end, period)
    if start is None or start == end:
        return None
    base = float(s.loc[start])
    if base <= 0:
        return None
    return (float(s.iloc[-1]) / base - 1) * 100
