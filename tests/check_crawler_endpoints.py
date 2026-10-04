"""
End-to-end check of what crawlers are told and refused, through Flask's test
client: /robots.txt, /sitemap.xml and the guard on the API and refresh routes.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app,
hermetically -- no background thread, no secret, no AWS, no network -- as
``check_fundamentals_endpoints`` does.

What it pins (2026-10-04: Meta's AI crawler called ~6,000 API URLs on
trade-agents.com in a few hours, through the /companies directory's links):

* robots.txt names the host that was asked in its Sitemap line, and keeps
  crawlers off /api/ while leaving share cards open;
* the sitemap lists the wiki on trade-agents.com, dated, and only dashboards on
  stock.li-family.us, so a crawler is not offered two copies of the docs;
* a declared crawler is refused on the API and on a refresh route, before the
  view runs -- so no build is started -- while a browser and a link-preview bot
  fetching a share card are not;
* a shared report, a capability URL, asks not to be indexed.

Run:  venv/bin/python -m tests.check_crawler_endpoints
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path


class _Any:
    def __getattr__(self, _name): return _Any()
    def __call__(self, *_a, **_k): return _Any()
    def __getitem__(self, _k): return _Any()
    def __setitem__(self, _k, _v): return None
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False
    def update(self, *_a, **_k): return None


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-crawlers-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import fundamentals                         # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

ROOT = Path(__file__).resolve().parent.parent
META = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36 (compatible; meta-externalagent/1.1 "
        "(+https://developers.facebook.com/docs/sharing/webmasters/crawler))")
CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/145.0.0.0 Safari/537.36")


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class CrawlerEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.kicks = []
        self._kick = fundamentals.kick
        fundamentals.kick = lambda s: (self.kicks.append(s), False)[1]

    def tearDown(self):
        fundamentals.kick = self._kick

    def test_robots_names_the_host_asked(self):
        resp = self.client.get("/robots.txt", headers={"Host": "trade-agents.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.mimetype.startswith("text/plain"))
        body = resp.get_data(as_text=True)
        self.assertIn("Sitemap: https://trade-agents.com/sitemap.xml", body)
        self.assertIn("Disallow: /api/", body)
        self.assertIn("Allow: /api/agents/shared/", body)
        other = self.client.get("/robots.txt", headers={"Host": "stock.li-family.us"})
        self.assertIn("Sitemap: https://stock.li-family.us/sitemap.xml", other.get_data(as_text=True))

    def test_sitemap_lists_the_wiki_on_trade_agents_only(self):
        ta = self.client.get("/sitemap.xml", headers={"Host": "trade-agents.com"}).get_data(as_text=True)
        self.assertIn("<loc>https://trade-agents.com/docs/overview</loc>", ta)
        self.assertIn("<loc>https://trade-agents.com/home</loc>", ta)
        self.assertIn("<lastmod>", ta)                       # research posts are dated
        stock = self.client.get("/sitemap.xml", headers={"Host": "stock.li-family.us"}).get_data(as_text=True)
        self.assertIn("<loc>https://stock.li-family.us/companies</loc>", stock)
        self.assertNotIn("/docs/", stock)

    def test_a_crawler_is_refused_before_any_build_starts(self):
        resp = self.client.get("/api/fundamentals/NVDA", headers={"User-Agent": META})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
        self.assertEqual(self.kicks, [])
        self.assertEqual(self.client.get("/dca/NVDA/refresh", headers={"User-Agent": META}).status_code, 403)

    def test_a_browser_is_served(self):
        resp = self.client.get("/api/fundamentals/NVDA", headers={"User-Agent": CHROME})
        self.assertNotEqual(resp.status_code, 403)

    def test_crawlers_still_read_pages_and_share_cards(self):
        page = self.client.get("/companies", headers={"User-Agent": META})
        self.assertEqual(page.status_code, 200)
        card = self.client.get("/api/agents/shared/not-a-token/card.png",
                               headers={"User-Agent": "Twitterbot/1.0"})
        self.assertNotEqual(card.status_code, 403)

    def test_a_shared_report_asks_not_to_be_indexed(self):
        html = (ROOT / "ystocker" / "templates" / "shared.html").read_text()
        self.assertIn('<meta name="robots" content="noindex, nofollow">', html)


if __name__ == "__main__":
    unittest.main()
