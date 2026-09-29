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

* /agents shows the landing to exactly one audience — signed out, full page —
  and never to the embedded panel (a 240px frame) or to a signed-in reader, who
  is here to run something; /home shows it to everyone, and does not ask a
  signed-in reader to sign in;
* on trade-agents.com a signed-out visit to the bare domain (which nginx
  proxies to /agents) is a 302 to /home, while a signed-in reader, a ?job= deep
  link and the embedded panel stay put, and no other host is redirected;
* the landing wears trade-agents.com's own masthead there and only there, and
  names /home as its canonical address;
* so does every other TradeAgents page there -- docs, research, sign-in,
  contact, the run page, shared reports -- with the paper plane and one footer,
  the product's; the same pages elsewhere, and the dashboards everywhere, keep
  yStocker's bar and footer;
* sign-in sends a signed-in reader where the link was going, and never off the
  site, whatever `next` says;
* an unknown docs or post slug is a 404 that still renders navigation, not a
  bare error page;
* the figures the pages quote are the ones the code enforces (free runs, the
  share lifetime), so a quota change cannot leave the docs quoting the old one.

Run:  venv/bin/python -m tests.check_wiki_pages
"""
from __future__ import annotations

import html
import os
import re
import sys
import types
import unittest
from unittest import mock


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
# The run page's own heading, which the landing replaces -- carried by both its
# variants, yStocker's and trade-agents.com's. Not the run button: that sits
# behind the allowlist, which this hermetic app's reader is not on.
RUN_PAGE_MARK = 'id="agPageTitle"'
MASTHEAD_MARK = "data-w-top"
# An element only base.html's own bar renders: its drawer's Refresh link. Not
# `data-nav="desktop"`, which the page's inline breakpoint CSS spells out on
# every page, bar or no bar -- nor `id="refreshBtn"` any more, which the
# Markets bar on trade-agents.com carries too, for the cooldown script.
DASHBOARD_BAR_MARK = 'id="refreshBtnMobile"'
MARKETS_BAR_MARK = '<nav class="w-sub" aria-label="Markets" data-w-sub>'
FLOAT_MARK = 'id="agentsFloatingRoot"'
TA_FOOTER_MARK = '<footer class="w-footer">'
# The dashboards' footer. Its first line, which only that footer carries.
YSTOCKER_FOOTER_MARK = 'data-i18n="footer.text"'
# The menu's markup. Not `data-w-acct` alone, which the masthead's script
# spells on every page, signed in or not.
ACCOUNT_MARK = '<div class="w-acct" data-w-acct>'
RUN_CTA = '<a class="w-btn w-btn-primary w-btn-sm" href="/agents">'
SIGNIN_NEXT = "/agents"
LOCAL = "http://localhost"
TA = "http://trade-agents.com"      # plain http: nginx terminates TLS in front

# Every TradeAgents page there is, signed out: the ones base.html's `_ta_shell`
# should dress on trade-agents.com and leave as yStocker's everywhere else. A
# share token that is not one renders the dead-link page, which is the page a
# stale share mail opens.
SHELL_PAGES = ("/home", "/docs/overview", "/research", f"/research/{wiki.POSTS[0]['slug']}",
               "/login", "/contact", "/agents/shared/not-a-token")


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
        import re
        from ystocker import agent_roles
        from ystocker.agents import ASTOCK_ANALYSTS, BASE_ANALYSTS
        html = self._get("/docs/desk").get_data(as_text=True)
        # The roster cards only: the prose is allowed to say the Fundamentals
        # Analyst existed (older reports still carry its turn), but it must not
        # be listed as a seat.
        cards = " ".join(re.findall(r'<div class="w-role-name">(.*?)</div>', html, re.S))
        for team in wiki.desk(agent_roles.ROLES, BASE_ANALYSTS, ASTOCK_ANALYSTS):
            for role in team["roles"]:
                self.assertIn(role["name"], cards)
        self.assertNotIn("Fundamentals Analyst", cards)

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

    # ── /home ─────────────────────────────────────────────────────────────
    def _as_reader(self, path, base_url=LOCAL):
        """GET `path` signed in. The session is set on the host being asked:
        a cookie minted for localhost is not sent to trade-agents.com."""
        with self.client.session_transaction(base_url=base_url) as s:
            s["user_email"] = "reader@example.com"
        try:
            return self._get(path, base_url=base_url).get_data(as_text=True)
        finally:
            with self.client.session_transaction(base_url=base_url) as s:
                s.clear()

    def test_home_is_the_landing_for_a_signed_out_visitor(self):
        r = self._get("/home")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(LANDING_MARK, html)
        self.assertIn('id="samples"', html)
        self.assertIn('id="pricing"', html)
        self.assertIn(f"login?next={SIGNIN_NEXT}", html)

    def test_home_takes_vibetradings_trailing_slash_too(self):
        r = self._get("/home/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(LANDING_MARK, r.get_data(as_text=True))

    def test_home_is_the_landing_for_a_signed_in_reader_too(self):
        # The whole point of the page: /agents gives this reader the run form.
        html = self._as_reader("/home")
        self.assertIn(LANDING_MARK, html)
        self.assertIn('id="agSample"', html)
        self.assertNotIn(RUN_PAGE_MARK, html)

    def test_home_does_not_ask_a_signed_in_reader_to_sign_in(self):
        html = self._as_reader("/home")
        self.assertNotIn(f"login?next={SIGNIN_NEXT}", html)
        self.assertIn('<a class="w-btn w-btn-primary" href="/agents">', html)
        self.assertIn("Run an analysis", html)

    def test_home_does_not_float_a_launcher_for_the_page_it_is(self):
        self.assertNotIn(FLOAT_MARK, self._get("/home").get_data(as_text=True))
        self.assertIn(FLOAT_MARK, self._get("/guide").get_data(as_text=True))

    def test_the_wiki_bar_goes_home_to_home(self):
        # Not `/` or /agents, which a signed-in reader gets as the run form --
        # with no #samples for the bar's "Sample reports" link to land on.
        html = self._get("/docs/overview").get_data(as_text=True)
        self.assertIn('<a class="w-bar-brand" href="/home">', html)
        self.assertIn('href="/home#samples"', html)

    # ── the masthead ──────────────────────────────────────────────────────
    def test_trade_agents_landing_wears_its_own_masthead(self):
        # /home, and the one /agents view on this host that still renders the
        # landing in place: a ?job= deep link.
        for path in ("/home", "/agents?job=0123456789abcdef"):
            with self.subTest(path=path):
                html = self._get(path, base_url=TA).get_data(as_text=True)
                self.assertIn(LANDING_MARK, html)
                self.assertIn(MASTHEAD_MARK, html)
                self.assertNotIn(DASHBOARD_BAR_MARK, html)
                self.assertIn('class="w-top-btn lang-toggle-btn"', html)
                self.assertIn('onclick="toggleTheme()"', html)

    def test_the_masthead_follows_sign_in(self):
        out = self._get("/home", base_url=TA).get_data(as_text=True)
        self.assertIn(f'<a class="w-btn w-btn-sm" href="/login?next={SIGNIN_NEXT}">', out)
        inside = self._as_reader("/home", base_url=TA)
        self.assertIn(MASTHEAD_MARK, inside)
        self.assertIn('<a class="w-btn w-btn-primary w-btn-sm" href="/agents">', inside)

    def test_yStocker_keeps_its_own_bar_on_the_landing(self):
        # Off trade-agents.com the landing is one section of yStocker.
        for path in ("/agents", "/home"):
            with self.subTest(path=path):
                html = self._get(path).get_data(as_text=True)
                self.assertIn(LANDING_MARK, html)
                self.assertIn(DASHBOARD_BAR_MARK, html)
                self.assertNotIn(MASTHEAD_MARK, html)

    def test_the_run_page_wears_the_masthead_on_trade_agents(self):
        # A TradeAgents page like the rest: the dashboards' bar is yStocker's.
        html = self._as_reader("/agents", base_url=TA)
        self.assertIn(RUN_PAGE_MARK, html)
        self.assertIn(MASTHEAD_MARK, html)
        self.assertNotIn(DASHBOARD_BAR_MARK, html)
        # Headed in the shell's type, and set in the masthead's column.
        self.assertIn('<h1 class="w-page-title" id="agPageTitle">', html)
        self.assertIn('<main class="flex-1 w-main ', html)

    def test_the_run_page_keeps_the_dashboard_bar_elsewhere(self):
        html = self._as_reader("/agents")
        self.assertIn(RUN_PAGE_MARK, html)
        self.assertIn('data-i18n="agents.title"', html)
        self.assertIn(DASHBOARD_BAR_MARK, html)
        self.assertNotIn(MASTHEAD_MARK, html)
        self.assertIn('<main class="flex-1 app-container mx-auto ', html)

    def test_the_landing_names_home_as_its_address(self):
        # A signed-out visit to `/` or /agents lands on /home, and a crawler is
        # always signed out, so /home is the address to index.
        for path in ("/home", "/agents?job=0123456789abcdef"):
            with self.subTest(path=path):
                html = self._get(path, base_url=TA).get_data(as_text=True)
                self.assertIn('<link rel="canonical" href="https://trade-agents.com/home">', html)
                self.assertIn('<meta property="og:url" content="https://trade-agents.com/home">', html)
                # https even though the request reached Flask as plain http.
                self.assertIn('<meta property="og:image" content="https://trade-agents.com/static/', html)
        self.assertNotIn('rel="canonical"', self._get("/home").get_data(as_text=True))

    # ── trade-agents.com's front door ─────────────────────────────────────
    # nginx proxies the bare domain to /agents, so these requests are what
    # https://trade-agents.com/ turns into by the time Flask sees it.
    def test_the_bare_domain_sends_a_signed_out_visitor_home(self):
        r = self._get("/agents", base_url=TA)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/home")
        # The answer depends on the session, so nothing may keep it.
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_the_redirect_keeps_the_language(self):
        r = self._get("/agents?lang=zh", base_url=TA)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/home?lang=zh")

    def test_a_signed_in_reader_keeps_the_run_page_at_the_bare_domain(self):
        with self.client.session_transaction(base_url=TA) as s:
            s["user_email"] = "reader@example.com"
        try:
            r = self._get("/agents", base_url=TA)
        finally:
            with self.client.session_transaction(base_url=TA) as s:
                s.clear()
        self.assertEqual(r.status_code, 200)
        self.assertIn(RUN_PAGE_MARK, r.get_data(as_text=True))

    def test_a_deep_link_and_the_embedded_panel_are_not_redirected(self):
        # Report emails and shared /agents?job= links point here and the page's
        # scripts read the id off it; the launcher's frame keeps its compact card.
        deep = self._get("/agents?job=0123456789abcdef", base_url=TA)
        self.assertEqual(deep.status_code, 200)
        self.assertIn('id="agSample"', deep.get_data(as_text=True))
        embed = self._get("/agents?embed=1", base_url=TA)
        self.assertEqual(embed.status_code, 200)
        self.assertIn('data-i18n="agents.need_signin"', embed.get_data(as_text=True))

    def test_other_hosts_keep_the_landing_at_agents(self):
        # Including the deploy's health probe, which asks stock.li-family.us.
        for base in (LOCAL, "http://stock.li-family.us"):
            with self.subTest(host=base):
                r = self._get("/agents", base_url=base)
                self.assertEqual(r.status_code, 200)
                self.assertIn(LANDING_MARK, r.get_data(as_text=True))

    # ── trade-agents.com's shell ──────────────────────────────────────────
    # base.html dresses every TradeAgents page there in the landing's
    # masthead, paper and footer (`_ta_shell`); elsewhere they are yStocker's.
    def _shell(self, html, where, dashboard=False):
        self.assertIn(MASTHEAD_MARK, html, where)
        self.assertNotIn(DASHBOARD_BAR_MARK, html, where)
        self.assertIn(" w-paper", html, where)
        self.assertIn("wiki.css", html, where)
        # One footer, and it is the product's: the landing and the docs each
        # used to close with a strip of their own above yStocker's footer.
        self.assertEqual(html.count("<footer"), 1, where)
        self.assertIn(TA_FOOTER_MARK, html, where)
        self.assertNotIn(YSTOCKER_FOOTER_MARK, html, where)
        self.assertNotIn('class="w-foot"', html, where)
        if dashboard:
            # Their own navigation as a second row, and the research-desk
            # launcher, which is how a reader runs the desk from a chart.
            self.assertIn(MARKETS_BAR_MARK, html, where)
            self.assertIn(FLOAT_MARK, html, where)
        else:
            # The masthead leads to the desk from every page, as /home's did.
            self.assertNotIn(MARKETS_BAR_MARK, html, where)
            self.assertNotIn(FLOAT_MARK, html, where)

    def test_every_trade_agents_page_wears_the_shell(self):
        for path in SHELL_PAGES:
            with self.subTest(path=path):
                self._shell(self._get(path, base_url=TA).get_data(as_text=True), path)
        self._shell(self._as_reader("/agents", base_url=TA), "/agents signed in")

    def test_the_same_pages_are_yStockers_elsewhere(self):
        for path in SHELL_PAGES:
            with self.subTest(path=path):
                html = self._get(path).get_data(as_text=True)
                self.assertIn(DASHBOARD_BAR_MARK, html)
                self.assertNotIn(MASTHEAD_MARK, html)
                self.assertNotIn(TA_FOOTER_MARK, html)
                self.assertIn(YSTOCKER_FOOTER_MARK, html)

    def test_the_dashboards_share_the_shell_on_trade_agents(self):
        # Same masthead, plane, footer and column as the landing, so switching
        # to them moves no edge; their own navigation is the Markets bar.
        for path in ("/guide", "/videos"):
            with self.subTest(path=path):
                html = self._get(path, base_url=TA).get_data(as_text=True)
                self._shell(html, path, dashboard=True)
                self.assertIn('<main class="flex-1 w-main ', html)
                self.assertIn(" w-markets", html)
                # Search and ↻ Refresh keep the hooks base.html's scripts find.
                self.assertIn("data-navsearch-toggle", html)
                self.assertIn('id="refreshBtn"', html)
                self.assertIn('id="refreshTooltipBody"', html)
                # What the dashboards' footer carried.
                for href in ('href="/guide"', 'href="/rss.xml"', "webcal://trade-agents.com/calendar.ics"):
                    self.assertIn(href, html)

    def test_the_dashboards_keep_their_bar_elsewhere(self):
        html = self._get("/guide").get_data(as_text=True)
        self.assertIn(DASHBOARD_BAR_MARK, html)
        self.assertNotIn(MASTHEAD_MARK, html)
        self.assertNotIn(MARKETS_BAR_MARK, html)
        self.assertIn(YSTOCKER_FOOTER_MARK, html)
        self.assertIn('<a href="/markets" class="flex items-center gap-2 font-bold text-lg shrink-0">', html)
        self.assertIn('<main class="flex-1 app-container mx-auto ', html)

    def test_every_page_on_trade_agents_shares_one_column(self):
        # The masthead's; a shared report keeps its narrower reading measure,
        # centred in it, as a Research Lab post does.
        for path in (*SHELL_PAGES, "/guide", "/videos"):
            with self.subTest(path=path):
                html = self._get(path, base_url=TA).get_data(as_text=True)
                self.assertIn('<main class="flex-1 w-main ', html)
        self.assertIn('<main class="flex-1 w-main ', self._as_reader("/agents", base_url=TA))

    def test_the_markets_bar_marks_the_page(self):
        html = self._get("/videos", base_url=TA).get_data(as_text=True)
        self.assertIn('<a class="w-sub-link is-current" href="/videos"', html)
        self.assertNotIn('<a class="w-sub-link is-current" href="/markets"', html)
        # And the masthead marks the section every dashboard is in.
        self.assertIn('<a href="/markets" class="is-current" aria-current="true">', html)
        self.assertIn('TradeAgents<small><span data-l="en">Markets</span>', html)

    def test_the_masthead_leads_back_to_the_landing_from_elsewhere(self):
        # Sample reports and Pricing are sections of the landing: anchors there,
        # links to it everywhere else.
        home = self._get("/home", base_url=TA).get_data(as_text=True)
        self.assertIn('href="#samples"', home)
        self.assertIn('href="#pricing"', home)
        docs = self._get("/docs/overview", base_url=TA).get_data(as_text=True)
        self.assertIn('href="/home#samples"', docs)
        self.assertIn('href="/home#pricing"', docs)
        self.assertNotIn('href="#pricing"', docs)

    def test_the_masthead_marks_the_section(self):
        docs = self._get("/docs/overview", base_url=TA).get_data(as_text=True)
        self.assertIn('<a href="/docs" class="is-current" aria-current="true">', docs)
        self.assertNotIn('<a href="/research" class="is-current"', docs)
        post = self._get(f"/research/{wiki.POSTS[0]['slug']}", base_url=TA).get_data(as_text=True)
        self.assertIn('<a href="/research" class="is-current" aria-current="true">', post)
        # The wordmark carries the section, as the product bar did.
        self.assertIn('TradeAgents<small><span data-l="en">Research Lab</span>', post)

    def test_the_wiki_drops_its_own_bar_on_trade_agents(self):
        # The masthead replaces it; off trade-agents.com it is still the
        # product's only bar, under yStocker's.
        self.assertNotIn('class="w-bar"', self._get("/docs/overview", base_url=TA).get_data(as_text=True))
        self.assertIn('class="w-bar"', self._get("/docs/overview").get_data(as_text=True))

    def test_signed_in_the_masthead_has_an_account_menu(self):
        docs = self._as_reader("/docs/overview", base_url=TA)
        self.assertIn(ACCOUNT_MARK, docs)
        self.assertIn("data-w-signout", docs)
        self.assertIn("reader@example.com", docs)
        self.assertIn(RUN_CTA, docs)
        # Share opens the same dialog it does from the dashboards' bar.
        self.assertIn('class="w-acct-item" data-share-open=""', docs)
        # And the per-reader pages the dashboards' account menu lists.
        self.assertIn('<a class="w-acct-item" href="/assets">', docs)
        self.assertIn('<a class="w-acct-item" href="/posts">', docs)
        # The run page drops the call to action that would lead to itself.
        run = self._as_reader("/agents", base_url=TA)
        self.assertIn(ACCOUNT_MARK, run)
        self.assertNotIn(RUN_CTA, run)
        self.assertNotIn(ACCOUNT_MARK, self._get("/docs/overview", base_url=TA).get_data(as_text=True))

    def test_the_account_menu_shows_the_balance_and_a_way_to_add_to_it(self):
        # The line starts as "…" and is filled from /api/agents/balance when the
        # menu opens. Prepay is the existing pack page on this brand's pay host,
        # with the reader's address (ypay shows no pack without one) and the way
        # back -- https, although Flask sees http behind nginx.
        docs = self._as_reader("/docs/overview", base_url=TA)
        self.assertIn('<b data-w-balance>…</b>', docs)
        link = re.search(r'<a class="w-acct-item" href="([^"]+)" data-w-topup>', docs)
        self.assertTrue(link, "no Prepay link in the account menu")
        href = html.unescape(link.group(1))
        self.assertTrue(href.startswith("https://pay.trade-agents.com?"), href)
        self.assertIn("email=reader%40example.com", href)
        self.assertIn("next=https%3A%2F%2Ftrade-agents.com%2Fagents", href)

    def test_the_balance_endpoint(self):
        from ystocker import credits

        self.assertEqual(self._get("/api/agents/balance", base_url=TA).status_code, 401)
        with self.client.session_transaction(base_url=TA) as s:
            s["user_email"] = "reader@example.com"
        try:
            with mock.patch.object(credits, "peek_balance", return_value=7):
                r = self._get("/api/agents/balance", base_url=TA)
            self.assertEqual(r.get_json(), {"credits": 7, "usd": 7})
            self.assertEqual(r.headers.get("Cache-Control"), "no-store")
            # An unreadable ledger is unknown, not an empty balance.
            with mock.patch.object(credits, "peek_balance", return_value=None):
                r = self._get("/api/agents/balance", base_url=TA)
            self.assertEqual(r.get_json(), {"credits": None, "usd": None})
        finally:
            with self.client.session_transaction(base_url=TA) as s:
                s.clear()

    def test_the_sign_in_page_is_the_products_on_trade_agents(self):
        html = self._get("/login", base_url=TA).get_data(as_text=True)
        self.assertIn('class="w-split"', html)
        self.assertIn(f"{quota.limit_default()} free runs a day", html)
        # No Sign in button on the page that is the sign-in.
        self.assertNotIn(f'href="/login?next={SIGNIN_NEXT}"', html)
        self.assertNotIn('data-i18n="login.subtitle"', html)
        self.assertIn('data-i18n="login.subtitle"', self._get("/login").get_data(as_text=True))

    def test_sign_in_sends_a_signed_in_reader_where_the_link_was_going(self):
        def login(query, base):
            with self.client.session_transaction(base_url=base) as s:
                s["user_email"] = "reader@example.com"
            try:
                r = self._get("/login" + query, base_url=base)
            finally:
                with self.client.session_transaction(base_url=base) as s:
                    s.clear()
            self.assertEqual(r.status_code, 302)
            return r.headers["Location"]

        self.assertEqual(login("?next=/docs/overview", TA), "/docs/overview")
        # With nowhere named, the run form there -- not the dashboards.
        self.assertEqual(login("", TA), "/agents")
        self.assertEqual(login("", LOCAL), "/markets")
        # Never off the site: each of these is a URL a browser resolves
        # elsewhere, and `next` is whatever the link's author put in it.
        for bad in ("https://evil.example/", "//evil.example/", "/\\evil.example",
                    "/%09/evil.example", "javascript:alert(1)"):
            with self.subTest(next=bad):
                self.assertEqual(login("?next=" + bad, TA), "/agents")

    def test_the_dead_share_link_page_quotes_the_enforced_lifetime(self):
        r = self._get("/agents/shared/not-a-token", base_url=TA)
        self.assertEqual(r.status_code, 404)
        self.assertIn(f"after {share.TTL_DAYS} days", r.get_data(as_text=True))

    def test_a_shared_report_is_rendered_and_dressed(self):
        from unittest import mock
        from ystocker import routes

        row = {"sharer": "alice@example.com", "note": "Worth a read.",
               "created_at": "2026-09-27T10:00:00+00:00", "expires_at": 4102444800}
        job = {"id": "0123456789abcdef", "ticker": "NVDA", "status": "done",
               "lang": "en", "date": "2026-09-26", "decision": "Buy", "report": "# NVDA"}
        with mock.patch.object(routes, "_shared_or_404", return_value=(None, row, job)):
            ta = self._get("/agents/shared/sometoken", base_url=TA).get_data(as_text=True)
            local = self._get("/agents/shared/sometoken").get_data(as_text=True)
        self._shell(ta, "/agents/shared/<token>")
        self.assertIn('<div class="w-callout w-share-note">Worth a read.</div>', ta)
        self.assertIn(DASHBOARD_BAR_MARK, local)
        # Both load the Markdown renderer the turns go through. The page never
        # did, so every shared report arrived as Markdown source.
        for html in (ta, local):
            self.assertIn("/static/markdown.js", html)


if __name__ == "__main__":
    unittest.main()
