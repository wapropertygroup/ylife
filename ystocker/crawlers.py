"""
ystocker.crawlers
~~~~~~~~~~~~~~~~~
What this site tells crawlers, and what it refuses them: ``robots.txt``, the
sitemap, and the user-agent test behind the guard that keeps them off the API.

Why this exists
---------------
On the night of 2026-10-03 Meta's AI crawler (``meta-externalagent/1.1``)
rendered trade-agents.com's ``/history/<ticker>?tab=fundamentals`` pages for
271 tickers, reached through the links on /companies, and its headless Chrome
called every API those pages call: about 6,000 requests from one /16 in a few
hours, 266 of them starting a Fundamentals build -- an EDGAR request and a
Yahoo one each -- for a client that never stayed to read the answer. Yahoo
rationing is the failure this codebase is most careful about (``valuation.py``
records the box being hard-blocked), and a crawler multiplies it by the size of
the directory, which is now every company SEC lists.

The site had no ``robots.txt`` at all: it was a 404. Two layers now, because
they fail differently:

* **robots.txt** asks every crawler to stay off ``/api/`` and off the refresh
  routes, which purge a cache and re-fetch upstream on a GET. Well-behaved
  crawlers -- Googlebot's renderer included, and Meta's per its own docs --
  honour it for the requests a page's JavaScript makes, so the pages stay
  indexable while their data calls stop.
* **The guard** refuses a declared crawler on ``/api/`` and on the refresh
  routes with a 403 regardless, for the ones that do not ask first. It keys on what a crawler says it is, and
  that is the right scope: a crawler that lies about its user-agent is a
  scraper, and a quota is the tool for that (``quota.try_consume_fundamentals_build``).

Two exceptions are load-bearing. ``/api/agents/shared/`` serves the share
card's ``og:image`` and QR code, which link-preview bots (Slack, X, Discord,
Facebook) must fetch for a pasted report link to unfurl at all; X's honours
robots.txt for card images, so it is allowed there too. And ``/api/posts`` /
``/api/inbox`` is the token-authenticated write door for scripts and webhooks,
whose user-agents are whatever their author wrote -- "AlertBot/1.0" included.

The user-agent test matches declared crawlers only: named tokens, and the
``<name>bot/<version>`` shape almost every crawler uses. It does not match
generic HTTP clients (curl, python-requests), which is how the owner's own
scripts call this API, and it does not treat a bare "bot" inside a word as a
crawler -- Cubot is a phone maker, and "CUBOT X30" appears in real Android UAs.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional
from xml.sax.saxutils import escape

#: Crawlers that name themselves. Lower-case substrings of the user-agent.
CRAWLER_TOKENS: tuple[str, ...] = (
    # search
    "googlebot", "google-inspectiontool", "googleother", "adsbot-google",
    "mediapartners-google", "storebot-google", "apis-google", "bingbot",
    "bingpreview", "msnbot", "adidxbot", "slurp", "duckduckbot", "baiduspider",
    "yandex", "sogou", "exabot", "seznambot", "naver",
    # AI and answer engines
    "meta-externalagent", "meta-externalfetcher", "gptbot", "chatgpt-user",
    "oai-searchbot", "claudebot", "claude-web", "anthropic-ai", "perplexitybot",
    "perplexity-user", "ccbot", "cohere-ai", "bytespider", "amazonbot",
    "applebot", "petalbot", "youbot", "diffbot", "timpibot", "imagesiftbot",
    # link previews and social
    "facebookexternalhit", "facebookcatalog", "twitterbot", "linkedinbot",
    "slackbot", "discordbot", "telegrambot", "whatsapp", "pinterest",
    "redditbot", "embedly", "skypeuripreview",
    # SEO and archives
    "ahrefsbot", "semrushbot", "mj12bot", "dotbot", "rogerbot", "blexbot",
    "dataforseobot", "serpstatbot", "seekportbot", "ia_archiver",
    "archive.org_bot",
)

#: "<Name>bot/2.1", "<Name>Bot;", "<Name>-crawler", "<Name>Spider/1.0".
_SHAPE = re.compile(r"(?:bot[/;]|crawler|spider)", re.IGNORECASE)

#: API paths a declared crawler may still call (see the module docstring).
API_ALLOW_PREFIXES: tuple[str, ...] = (
    "/api/agents/shared/",
    "/api/posts",
    "/api/inbox",
)

#: GET routes that purge a cache and re-fetch upstream. Listed explicitly as
#: well as by pattern, because only some crawlers understand wildcards.
REFRESH_PATHS: tuple[str, ...] = (
    "/refresh", "/fed/refresh", "/fedwatch/refresh", "/housing/refresh",
    "/multiples/refresh", "/13f/refresh", "/predictions/refresh",
)


def is_crawler(user_agent: Optional[str]) -> bool:
    """Whether *user_agent* declares itself a crawler."""
    ua = (user_agent or "").lower()
    if not ua:
        return False
    return any(token in ua for token in CRAWLER_TOKENS) or bool(_SHAPE.search(ua))


#: A GET that purges and rebuilds: ``/refresh`` and ``/<anything>/refresh``.
_REFRESH = re.compile(r"(^|/)refresh/?$")


def guarded(path: str) -> bool:
    """Whether *path* is one a declared crawler is refused on.

    Two families. The API, which every dashboard's JavaScript calls and which
    fetches upstream on a cold cache -- and, for ``/api/dca``, does more than
    spend: each scored view registers the ticker in the 60-name ranked
    universe, so a crawler opening 198 ``/dca/<T>`` pages was evicting real
    readers' names from it. And the refresh routes, which are ordinary links on
    the page: ``/dca/<T>/refresh`` was fetched 179 times by one crawler on
    2026-10-04, each a forced six-read Yahoo rebuild.
    """
    if path.startswith("/api/"):
        return not any(path.startswith(prefix) for prefix in API_ALLOW_PREFIXES)
    return bool(_REFRESH.search(path))


def refused(path: str, user_agent: Optional[str]) -> bool:
    """Whether the guard should refuse this request."""
    return guarded(path) and is_crawler(user_agent)


def robots_txt(base_url: str) -> str:
    """The robots.txt for one host. *base_url* is e.g. ``https://trade-agents.com``.

    ``Allow`` before ``Disallow`` for the share-card path: Google takes the most
    specific (longest) match whatever the order, and older parsers take the
    first match, so this order is right for both.
    """
    lines = ["User-agent: *", "Allow: /api/agents/shared/", "Disallow: /api/"]
    lines += [f"Disallow: {p}" for p in REFRESH_PATHS]
    lines += ["Disallow: /*/refresh", "Disallow: /login", "Disallow: /logout", "Allow: /", "",
              f"Sitemap: {base_url.rstrip('/')}/sitemap.xml", ""]
    return "\n".join(lines)


def sitemap_xml(base_url: str, pages: Iterable[tuple[str, Optional[str]]]) -> str:
    """A sitemap of (path, lastmod-or-None) pairs on *base_url*."""
    base = base_url.rstrip("/")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    seen: set[str] = set()
    for path, lastmod in pages:
        if path in seen:
            continue
        seen.add(path)
        out.append("  <url>")
        out.append(f"    <loc>{escape(base + path)}</loc>")
        if lastmod and _iso_date(lastmod):
            out.append(f"    <lastmod>{lastmod}</lastmod>")
        out.append("  </url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"


def _iso_date(raw: str) -> bool:
    try:
        date.fromisoformat(raw)
        return True
    except (TypeError, ValueError):
        return False
