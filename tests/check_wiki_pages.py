"""
End-to-end check of the TradeAgents wiki (/docs, /research) and the /agents
landing page, through Flask's test client.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app. Unlike
the other ``check_`` scripts it builds it *hermetically* — no background thread,
no secret, no AWS (see the setup below for why a read-only page still needed
that) — and matplotlib is stubbed before ``ystocker.routes`` is imported, for the
broken Homebrew pyexpat this repo's dev checkout has (see
``check_dca_endpoints.py``).

What it pins, beyond "every page answers 200":

* the landing is shown to exactly one audience — signed out, full page — and
  never to the embedded panel (a 240px frame) or to a signed-in reader, who is
  here to run something;
* an unknown docs or post slug is a 404 that still renders navigation, not a
  bare error page;
* the figures the pages quote are the ones the code enforces (free runs, the
  share lifetime), so a quota change cannot leave the docs quoting the old one.

Run:  venv/bin/python -m tests.check_wiki_pages
"""
from __future__ import annotations

import os
import sys
import types
import unittest


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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-wiki-secret")

# Hermetic, unlike most check_ scripts: these pages need no background thread, no
# secret and no AWS, and create_app() would otherwise start ~20 threads (the
# daily email broadcast and writers to production DynamoDB series among them)
# with whatever credentials this machine holds. A GET is not safe either —
# agents._records(), reached by the showcase on /agents, backfills local job
# files into the production jobs table — so AWS is switched off outright.
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
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
from ystocker import quota, share, wiki                   # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None


def _build_app():
    """create_app() with Thread.start disabled for its duration."""
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


LANDING_MARK = 'id="lpTitle"'


class WikiPages(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def _get(self, path, **kw):
        return self.client.get(path, follow_redirects=False, **kw)

    # ── /docs ─────────────────────────────────────────────────────────────
    def test_docs_root_redirects_to_the_default_page_keeping_the_language(self):
        r = self._get("/docs?lang=zh")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"/docs/{wiki.DEFAULT_DOC}", r.headers["Location"])
        self.assertIn("lang=zh", r.headers["Location"])

    def test_every_docs_page_renders_with_its_sidebar_and_both_languages(self):
        for page in wiki.DOCS:
            with self.subTest(page=page["slug"]):
                r = self._get(f"/docs/{page['slug']}")
                self.assertEqual(r.status_code, 200)
                html = r.get_data(as_text=True)
                self.assertIn('class="w-side-link is-current"', html)
                self.assertIn(page["title"]["en"], html)
                self.assertIn(page["title"]["zh"], html)
                self.assertIn("data-wiki-prose", html)
                self.assertIn("wiki.css", html)

    def test_an_unknown_docs_page_is_a_404_that_keeps_the_navigation(self):
        r = self._get("/docs/no-such-page")
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertIn("w-side-link", html)
        self.assertIn("No such page", html)

    def test_the_pricing_page_quotes_the_enforced_free_allowance(self):
        html = self._get("/docs/pricing").get_data(as_text=True)
        self.assertIn(str(quota.limit_default()), html)

    def test_the_sharing_page_quotes_the_enforced_link_lifetime(self):
        html = self._get("/docs/sharing").get_data(as_text=True)
        self.assertIn(str(share.TTL_DAYS), html)

    def test_the_desk_page_lists_every_seat_the_runner_uses(self):
        from ystocker import agent_roles
        from ystocker.agents import ASTOCK_ANALYSTS, BASE_ANALYSTS
        html = self._get("/docs/desk").get_data(as_text=True)
        for team in wiki.desk(agent_roles.ROLES, BASE_ANALYSTS, ASTOCK_ANALYSTS):
            for role in team["roles"]:
                self.assertIn(role["name"], html)
        self.assertNotIn("Fundamentals Analyst", html)

    # ── /research ─────────────────────────────────────────────────────────
    def test_the_research_index_lists_every_post_newest_first(self):
        html = self._get("/research").get_data(as_text=True)
        positions = [html.find(f"/research/{p['slug']}") for p in wiki.POSTS]
        self.assertTrue(all(pos > 0 for pos in positions), positions)
        self.assertEqual(positions, sorted(positions))

    def test_every_post_renders(self):
        for p in wiki.POSTS:
            with self.subTest(post=p["slug"]):
                r = self._get(f"/research/{p['slug']}")
                self.assertEqual(r.status_code, 200)
                html = r.get_data(as_text=True)
                self.assertIn(p["title"]["en"], html)
                self.assertIn(p["date"], html)
                self.assertIn('id="tldr"', html)

    def test_an_unknown_post_is_a_404_on_the_index(self):
        r = self._get("/research/no-such-post")
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertIn("no-such-post", html)
        self.assertIn(f"/research/{wiki.POSTS[0]['slug']}", html)

    # ── /agents ───────────────────────────────────────────────────────────
    def test_a_signed_out_visitor_gets_the_landing(self):
        html = self._get("/agents").get_data(as_text=True)
        self.assertIn(LANDING_MARK, html)
        self.assertIn("w-paper", html)
        self.assertIn(f"{quota.limit_default()} free runs a day", html)
        # The sample block keeps the ids its script reads.
        self.assertIn('id="agSample"', html)
        self.assertIn('id="samples"', html)

    def test_the_embedded_panel_keeps_the_compact_card(self):
        html = self._get("/agents?embed=1").get_data(as_text=True)
        self.assertNotIn(LANDING_MARK, html)
        self.assertIn('data-i18n="agents.need_signin"', html)

    def test_a_signed_in_reader_gets_the_run_page_not_the_landing(self):
        with self.client.session_transaction() as s:
            s["user_email"] = "reader@example.com"
        try:
            html = self._get("/agents").get_data(as_text=True)
        finally:
            with self.client.session_transaction() as s:
                s.clear()
        self.assertNotIn(LANDING_MARK, html)
        self.assertNotIn(" w-paper", html)
        self.assertIn('data-i18n="agents.title"', html)
        self.assertIn('href="/docs"', html)

    def test_the_footer_links_the_wiki_from_every_page(self):
        html = self._get("/guide").get_data(as_text=True)
        self.assertIn('href="/docs"', html)
        self.assertIn('href="/research"', html)


if __name__ == "__main__":
    unittest.main()
