"""
ystocker.earnings
~~~~~~~~~~~~~~~~~
When each tracked company next reports.

Pure — no network, no clock unless one is passed in — for the reason
:mod:`ystocker.fcf` and :mod:`ystocker.health` are: the selection rule below is
the only part that can be wrong, and it is only cheaply provable if proving it
needs no I/O.

Why the field name is not trusted
---------------------------------
Yahoo's ``info`` carries several earnings timestamps and their names do not
reliably say which way they point. Measured on MSFT on 2026-09-19::

    earningsTimestamp           2026-07-29     the last report
    earningsTimestampStart      2026-10-28     the next one
    earningsCallTimestampStart  2026-07-29     the last call

Reading ``earningsTimestampStart`` because it sounds like a start is the tempting
implementation, and across a few hundred tickers it puts past dates into a list
headed "upcoming" — which is not a visible failure, just a calendar that is
quietly wrong for whichever names Yahoo happens to populate differently. So every
candidate is collected and the **earliest one still in the future** wins, which
is a property of the data rather than of the key.

This costs no fetch. Every field is already in the ``info`` dict
``data.fetch_ticker_data`` pulls for each of the ~308 tickers in ``PEER_GROUPS``
every eight hours, and was being discarded.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping, Optional

__all__ = ["CANDIDATE_FIELDS", "next_earnings", "upcoming"]

#: Every field that has been observed to hold an earnings timestamp. Order is
#: irrelevant — the date decides, not the key.
CANDIDATE_FIELDS: tuple[str, ...] = (
    "earningsTimestampStart",
    "earningsTimestampEnd",
    "earningsTimestamp",
    "earningsCallTimestampStart",
    "earningsDate",
    "earningsTimestamps",
)


def _dates(value: Any) -> list[dt.date]:
    """Every date a field's value can yield. Yahoo sends scalars and lists."""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[dt.date] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, (int, float)):
            # Guard the epoch. A 0 or a negative resolves to 1970 and is then
            # discarded as past, which also covers a stray `True` — an explicit
            # bool check was here and was dead, proven by mutating it out and
            # watching the suite stay green. A very large value is the case that
            # actually needs handling: a millisecond timestamp read as seconds
            # lands in the year 58000 and sorts to the top of a calendar for ever.
            try:
                seconds = float(item)
                if seconds > 1e11:          # milliseconds, not seconds
                    seconds /= 1000.0
                if seconds <= 0:
                    continue
                out.append(dt.datetime.fromtimestamp(seconds, dt.timezone.utc).date())
            except (ValueError, OSError, OverflowError):
                continue
        elif isinstance(item, dt.datetime):
            out.append(item.date())
        elif isinstance(item, dt.date):
            out.append(item)
        else:
            try:
                out.append(dt.date.fromisoformat(str(item)[:10]))
            except ValueError:
                continue
    return out


def next_earnings(info: Mapping[str, Any], *,
                  today: Optional[dt.date] = None) -> Optional[str]:
    """The next reporting date as ``YYYY-MM-DD``, or ``None``.

    "Next" means the earliest candidate that is not in the past. Today counts as
    future — a company reporting this morning has not stopped being today's news.

    ``None`` when nothing qualifies, which is the honest answer for a company
    between cycles or an ETF. A caller must not fall back to the most recent past
    date: a calendar showing last quarter's date as upcoming is worse than a
    calendar with a gap.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    candidates: list[dt.date] = []
    for field in CANDIDATE_FIELDS:
        candidates.extend(_dates(info.get(field)))
    future = sorted(d for d in candidates if d >= today)
    return future[0].isoformat() if future else None


def upcoming(records: Iterable[tuple[str, Mapping[str, Any]]], *,
             within_days: int = 21,
             today: Optional[dt.date] = None,
             limit: int = 40) -> list[dict[str, Any]]:
    """Tracked companies reporting in the next *within_days*, soonest first.

    *records* is ``(ticker, cached_record)`` pairs — the cached record, not a
    live ``info`` dict, so this runs on the request path without a fetch.

    Sorted by date and then by market cap descending, so a day's biggest reporter
    leads rather than whichever ticker sorted first alphabetically.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    horizon = today + dt.timedelta(days=within_days)
    out: list[dict[str, Any]] = []
    for ticker, record in records:
        raw = record.get("Earnings Date")
        if not raw:
            continue
        try:
            when = dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if not (today <= when <= horizon):
            continue
        out.append({
            "ticker": ticker,
            "name": record.get("Name") or ticker,
            "date": when.isoformat(),
            "days_away": (when - today).days,
            "market_cap": record.get("Market Cap ($B)"),
            # Carried so the page can show what the market expects to be told,
            # beside when it will be told — the two are only useful together.
            "pe_fwd": record.get("PE (Forward)"),
            "eps_growth_q": record.get("EPS Growth Q (%)"),
        })
    out.sort(key=lambda r: (r["date"], -(r["market_cap"] or 0)))
    return out[:limit]
