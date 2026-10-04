"""Tests for ystocker.crawlers -- robots.txt, the sitemap, and the crawler test.

No app, no network. The user-agents are copied from the box's nginx log of
2026-10-04, when Meta's AI crawler rendered trade-agents.com's dashboards and
called every API on them (about 6,000 requests in a few hours), and from the
clients that must keep working: the owner's scripts on /api/posts, link-preview
bots fetching a shared report's card, and phones whose model name contains
"bot".
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ystocker import crawlers  # noqa: E402

META = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36 (compatible; meta-externalagent/1.1 "
        "(+https://developers.facebook.com/docs/sharing/webmasters/crawler))")
GOOGLE = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
PETAL = "Mozilla/5.0 (compatible;PetalBot;+https://webmaster.petalsearch.com/site/petalbot)"
AMAZON = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Amazonbot/0.1)"
TWITTER = "Twitterbot/1.0"
CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/145.0.0.0 Safari/537.36")
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/19.0 Mobile/15E148 Safari/604.1")
CUBOT = ("Mozilla/5.0 (Linux; Android 10; CUBOT X30) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/120.0.0.0 Mobile Safari/537.36")


class CrawlerTests(unittest.TestCase):

    def test_declared_crawlers_are_recognised(self):
        for ua in (META, GOOGLE, PETAL, AMAZON, TWITTER, "GPTBot/1.1", "ClaudeBot/1.0",
                   "facebookexternalhit/1.1", "SomeNewBot/0.3 (+https://example.com)",
                   "AcmeSpider/2.0"):
            self.assertTrue(crawlers.is_crawler(ua), ua)

    def test_people_and_scripts_are_not(self):
        # Cubot is a phone maker: "bot" inside a word is not a crawler.
        for ua in (CHROME, IPHONE, CUBOT, "curl/8.7.1", "python-requests/2.32.3", "", None):
            self.assertFalse(crawlers.is_crawler(ua), ua)

    def test_the_api_and_refresh_routes_are_refused_to_crawlers(self):
        for path in ("/api/fundamentals/NVDA", "/api/history/NVDA", "/api/dca/NVDA",
                     "/api/forecast/NVDA", "/dca/NVDA/refresh", "/refresh", "/fed/refresh"):
            self.assertTrue(crawlers.refused(path, META), path)
            self.assertFalse(crawlers.refused(path, CHROME), path)

    def test_pages_stay_open_to_crawlers(self):
        for path in ("/history/NVDA", "/companies", "/docs/overview", "/home",
                     "/refreshments", "/api-docs"):
            self.assertFalse(crawlers.refused(path, GOOGLE), path)

    def test_share_cards_and_the_write_door_stay_open(self):
        # A pasted report link must unfurl, and the owner's scripts must post.
        self.assertFalse(crawlers.refused("/api/agents/shared/abc/card.png", TWITTER))
        self.assertFalse(crawlers.refused("/api/agents/shared/abc/qr.png", "Slackbot-LinkExpanding 1.0"))
        self.assertFalse(crawlers.refused("/api/posts", "AlertBot/1.0"))
        self.assertFalse(crawlers.refused("/api/inbox", "AlertBot/1.0"))


class RobotsTests(unittest.TestCase):

    def setUp(self):
        self.txt = crawlers.robots_txt("https://trade-agents.com/")
        self.lines = self.txt.splitlines()

    def test_api_disallowed_share_cards_allowed_first(self):
        self.assertIn("Disallow: /api/", self.lines)
        self.assertLess(self.lines.index("Allow: /api/agents/shared/"),
                        self.lines.index("Disallow: /api/"))

    def test_sitemap_names_the_host_asked(self):
        self.assertIn("Sitemap: https://trade-agents.com/sitemap.xml", self.lines)

    def test_every_refresh_route_is_disallowed(self):
        # A refresh route added later must not slip past robots.txt.
        routes = (ROOT / "ystocker" / "routes.py").read_text()
        for rule in re.findall(r'@bp\.route\("([^"]*refresh[^"]*)"', routes):
            explicit = f"Disallow: {rule}" in self.lines
            wildcard = rule.endswith("/refresh") and "Disallow: /*/refresh" in self.lines
            self.assertTrue(explicit or wildcard, rule)
            self.assertTrue(crawlers.guarded(re.sub(r"<[^>]+>", "X", rule)), rule)


class SitemapTests(unittest.TestCase):

    def test_urls_escaped_deduped_and_dated_only_when_valid(self):
        xml = crawlers.sitemap_xml("https://trade-agents.com", [
            ("/home", None), ("/research/a&b", "2026-09-26"), ("/home", None),
            ("/docs/x", "not a date")])
        self.assertEqual(xml.count("<loc>https://trade-agents.com/home</loc>"), 1)
        self.assertIn("<loc>https://trade-agents.com/research/a&amp;b</loc>", xml)
        self.assertIn("<lastmod>2026-09-26</lastmod>", xml)
        self.assertNotIn("not a date", xml)
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-8"?>'))


if __name__ == "__main__":
    unittest.main()
