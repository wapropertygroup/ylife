"""
ystocker.feeds
~~~~~~~~~~~~~~
Syndication endpoints: an iCalendar feed of FOMC meeting dates and an RSS feed
of the daily market commentary.

Both are built from data the app already has — ``fedwatch.fetch_fomc_meetings``
for the calendar and the ``ystocker-daily-summaries`` DynamoDB table for the
commentary — so neither adds an upstream dependency.

Why hand-rolled instead of a library
------------------------------------
Both formats are a few dozen lines of text assembly, and the repo has no feed
dependency to reuse. The risk is not volume, it is that both specs have sharp
edges that silently produce a feed which *parses* but is wrong:

* **RFC 5545 (iCalendar)** requires CRLF line endings, folding of lines longer
  than 75 octets, and — the classic bug — an **exclusive** DTEND. A one-day
  all-day event on the 16th is ``DTSTART;VALUE=DATE:20260916`` with
  ``DTEND;VALUE=DATE:20260917``; writing the 16th for both makes the event
  vanish or render as zero-length depending on the client. TEXT values must
  also escape backslash, semicolon, comma and newline, or a description
  containing a comma truncates the field.

* **RSS 2.0** wants RFC 822 dates (not ISO 8601), a stable ``guid``, and
  XML-escaped content. An unescaped ``&`` in a summary makes the whole feed
  unparseable in most readers.

Each of those is asserted in the round-trip checks rather than trusted.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

SITE = "https://stock.li-family.us"
PRODID = "-//li-family.us//yStocker//EN"

# How many days of commentary an RSS reader gets. The DynamoDB rows carry a
# TTL, so older entries expire upstream regardless.
RSS_LIMIT = 20


# ---------------------------------------------------------------------------
# Shared text helpers
# ---------------------------------------------------------------------------

def _xml_escape(text: str) -> str:
    """Escape a string for XML character data / attribute values.

    Hand-rolled rather than via xml.sax.saxutils to keep this import-light, and
    because & must be replaced first or the other replacements double-escape.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _ics_escape(text: str) -> str:
    """Escape a value for an iCalendar TEXT property (RFC 5545 §3.3.11).

    Backslash first, then the delimiters. An unescaped comma or semicolon ends
    the property value early, so a description would silently truncate.
    """
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Fold a content line to 75 octets, continuations prefixed with a space.

    RFC 5545 §3.1 counts *octets*, not characters, so folding has to happen on
    the UTF-8 encoding — splitting by character would overflow the limit for any
    Chinese description, and splitting mid-codepoint would corrupt it.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]

    out: list[str] = []
    chunk = bytearray()
    limit = 75
    for ch in line:
        enc = ch.encode("utf-8")
        if len(chunk) + len(enc) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = bytearray()
            limit = 74  # continuation lines carry a leading space
        chunk += enc
    if chunk:
        out.append(chunk.decode("utf-8"))
    return [out[0]] + [" " + part for part in out[1:]]


def _rfc822(dt: datetime) -> str:
    """RFC 822 date, which is what RSS requires (not ISO 8601)."""
    from email.utils import format_datetime
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


# ---------------------------------------------------------------------------
# iCalendar — FOMC meeting dates
# ---------------------------------------------------------------------------

def _fomc_probability_note(meeting: date) -> str:
    """One line of rate-probability context for a meeting, if we have it.

    Read from the FedWatch cache only — never triggers a rebuild. A calendar
    subscription hitting this endpoint must not be able to kick off a futures
    fetch, and a stale or missing cache just means a plainer description.
    """
    try:
        from ystocker.fedwatch import get_cache_ts, is_cache_fresh, _cache_data
        if not is_cache_fresh() or not _cache_data:
            return ""
        for m in _cache_data.get("meetings", []):
            if m.get("date") != meeting.isoformat():
                continue
            parts = [
                f"{o['lower']:.2f}-{o['upper']:.2f}%: {o['prob']:.0f}%"
                for o in (m.get("outcomes") or []) if o.get("prob", 0) >= 1
            ]
            if not parts:
                return ""
            return ("Market-implied odds as of "
                    f"{date.fromtimestamp(get_cache_ts() or 0).isoformat()}: "
                    + " | ".join(parts))
    except Exception as exc:
        log.debug("feeds: no probability note for %s: %s", meeting, exc)
    return ""


def build_fomc_ics(include_past: bool = True) -> str:
    """Build an iCalendar feed of scheduled FOMC decision dates.

    Events are all-day on the decision day — the day the target range is
    announced — rather than spanning the two-day meeting, because that is the
    date people actually want in their calendar.
    """
    from ystocker.fedwatch import fetch_fomc_meetings

    meetings: Iterable[date] = fetch_fomc_meetings()
    today = date.today()
    if not include_past:
        meetings = [m for m in meetings if m >= today]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        # X-WR-* are non-standard but every major client reads them for the
        # subscription's display name.
        "X-WR-CALNAME:FOMC Meetings (yStocker)",
        "X-WR-CALDESC:Federal Open Market Committee decision dates",
        "X-WR-TIMEZONE:UTC",
    ]

    for m in sorted(meetings):
        note = _fomc_probability_note(m)
        desc = "FOMC announces its federal funds target range on this date."
        if note:
            desc += "\n\n" + note
        desc += f"\n\nRate probabilities: {SITE}/fedwatch"

        lines += [
            "BEGIN:VEVENT",
            # Stable UID keyed on the date: re-subscribing or refreshing
            # updates the existing event instead of duplicating it.
            f"UID:fomc-{m.isoformat()}@stock.li-family.us",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{m.strftime('%Y%m%d')}",
            # DTEND is EXCLUSIVE for DATE values, so a single all-day event
            # ends on the following day. Using the same date makes the event
            # zero-length and it disappears in several clients.
            f"DTEND;VALUE=DATE:{(m + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape('FOMC Rate Decision')}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            f"URL:{SITE}/fedwatch",
            "CATEGORIES:Economics,Monetary Policy",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    # RFC 5545 §3.1: lines are CRLF-delimited, and the body ends with one.
    return "\r\n".join(folded) + "\r\n"


# ---------------------------------------------------------------------------
# RSS — daily market commentary
# ---------------------------------------------------------------------------

def _recent_summaries(lang: str, limit: int) -> list[dict[str, Any]]:
    """Most recent daily commentary rows, newest first.

    Scans rather than queries because the table is keyed by date and there is
    no index on lang_market; the table is small (hundreds of rows, each with a
    TTL) so this stays cheap. Returns [] when DynamoDB is unavailable so the
    feed degrades to empty rather than erroring.
    """
    try:
        from ystocker.routes import _get_summaries_table
        tbl = _get_summaries_table()
        if not tbl:
            return []
        from boto3.dynamodb.conditions import Attr

        wanted = {f"{lang}_us", f"{lang}_cn"}
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("lang_market").is_in(list(wanted)),
        }
        # One or two pages is plenty for a 20-item feed.
        for _ in range(3):
            resp = tbl.scan(**kwargs)
            items.extend(resp.get("Items", []))
            key = resp.get("LastEvaluatedKey")
            if not key or len(items) > limit * 4:
                break
            kwargs["ExclusiveStartKey"] = key

        items.sort(key=lambda i: (str(i.get("date", "")), str(i.get("lang_market", ""))),
                   reverse=True)
        return items[:limit]
    except Exception as exc:
        log.warning("feeds: could not load summaries: %s", exc)
        return []


_MARKET_TITLE = {
    "us": {"en": "US Markets", "zh": "美国市场"},
    "cn": {"en": "China & Asia-Pacific Markets", "zh": "中国及亚太市场"},
}


def build_rss(lang: str = "en", limit: int = RSS_LIMIT) -> str:
    """Build an RSS 2.0 feed of the daily market commentary."""
    lang = "zh" if lang == "zh" else "en"
    items = _recent_summaries(lang, limit)

    title = "yStocker Daily Market Commentary" if lang == "en" else "yStocker 每日市场评论"
    desc = ("AI-written daily commentary on US and Asia-Pacific markets"
            if lang == "en" else "由 AI 撰写的美国与亚太市场每日评论")
    self_url = f"{SITE}/rss.xml?lang={lang}"

    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{_xml_escape(title)}</title>",
        f"<link>{SITE}/daily</link>",
        f"<description>{_xml_escape(desc)}</description>",
        f"<language>{'zh-cn' if lang == 'zh' else 'en-us'}</language>",
        f"<lastBuildDate>{_rfc822(datetime.now(timezone.utc))}</lastBuildDate>",
        f"<generator>yStocker</generator>",
        # A self link is required for a strictly valid feed.
        f'<atom:link href="{_xml_escape(self_url)}" rel="self" type="application/rss+xml"/>',
    ]

    for it in items:
        day = str(it.get("date", ""))
        lm = str(it.get("lang_market", ""))
        market = lm.split("_")[-1] if "_" in lm else "us"
        label = _MARKET_TITLE.get(market, _MARKET_TITLE["us"])[lang]
        body = str(it.get("summary", "")).strip()
        if not body:
            continue

        try:
            pub = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        except ValueError:
            pub = datetime.now(timezone.utc)

        out += [
            "<item>",
            f"<title>{_xml_escape(f'{label} — {day}')}</title>",
            f"<link>{SITE}/daily?date={_xml_escape(day)}&amp;lang={lang}</link>",
            # guid is not a resolvable URL, so it must say so or readers treat
            # it as a link and may 404 on it.
            f'<guid isPermaLink="false">{_xml_escape(f"ystocker-{day}-{lm}")}</guid>',
            f"<pubDate>{_rfc822(pub)}</pubDate>",
            f"<category>{_xml_escape(label)}</category>",
            f"<description>{_xml_escape(body)}</description>",
            "</item>",
        ]

    out += ["</channel>", "</rss>"]
    return "\n".join(out)
