"""
Tests for the FuTu *app* link on /history/<ticker> — ``ystocker.futu``.

No app, no network, no writes outside a temp dir: the URL builders are pure
string work and the page parser is fed fixtures shaped like the real
``window.__INITIAL_STATE__``.

Companion to test_futu_links.py, which covers the ticker -> ``SYMBOL-MARKET``
mapping. This file covers the second half of the problem, which is subtler: Futu
will not open its native quote screen from the web URL, so the button has to
carry a scheme link, and every constant in it was read off the live site because
a wrong one is a button that does nothing and reports nothing.

The properties worth protecting, in rough order of how quietly they break:

* **A missing id must degrade to the web link, never to a dead one.** Everything
  here is an upgrade on top of a working anchor. ``link_context`` with no cached
  id must still hand the template a web URL and no scheme link, because that is
  the pre-existing behaviour and also the correct no-app-installed fallback.
* **The id must never be trusted positionally.** A quote page carries dozens of
  other stockIds in its rails; linking one of those sends the reader to the wrong
  company, which is worse than not linking at all. Hence the stockCode +
  marketLabel round-trip check, and hence a test that a page for the wrong symbol
  is refused.
* **stockId is a string.** US ids are 6 digits, HK and A-share ids are 14
  (00700-HK is 54047868453564), so anything that narrows to a smaller integer
  type, or reformats, breaks exactly the non-US venues the symbol mapping went to
  such trouble to support.
* **The Android fallback URL must be percent-encoded.** ``intent://`` is
  ``;``-delimited, so an unencoded ``browser_fallback_url`` truncates at the
  query string and Chrome falls back to a different page than intended.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ystocker import futu

ROOT = Path(__file__).resolve().parent.parent

# Shaped exactly like the live page: marketLabel precedes stockCode/stockId
# inside stock_info, and unrelated stockIds appear both before and after it.
def _page(code: str, market: str, stock_id: str) -> str:
    return (
        '<html><body><div data-rail=\'{"stockId":"200001"}\'></div>'
        '<script>window.__INITIAL_STATE__={"stock_info":{"name":"Some Company",'
        '"enName":"Some Company","marketType":2,"marketLabel":"' + market + '",'
        '"isPlate":false,"stockCode":"' + code + '","stockId":"' + stock_id + '",'
        '"marketCode":11,"instrumentType":3},'
        '"hotRail":[{"stockId":"205513"},{"stockId":"208805"}]}</script>'
    )


SMCI_PAGE = _page("SMCI", "US", "203319")


class DeepLinkShape(unittest.TestCase):
    """The scheme URL, verified against Futu's own af_dp on the SMCI page."""

    def test_matches_futus_own_deep_link_verbatim(self):
        """The live SMCI page carries
        ``af_dp=ftnn://quote/stockDetail/203319/1`` in its AppsFlyer link. If this
        assertion is ever "fixed" to something tidier, the button stops working
        and nothing else notices."""
        self.assertEqual(futu.deep_link("203319"),
                         "ftnn://quote/stockDetail/203319/1")

    def test_fourteen_digit_ids_survive(self):
        """00700-HK is 54047868453564. Anything int-narrowing breaks HK and CN."""
        self.assertEqual(futu.deep_link("54047868453564"),
                         "ftnn://quote/stockDetail/54047868453564/1")

    def test_no_id_means_no_link_rather_than_a_broken_scheme(self):
        for bad in (None, "", "   ", "abc", "203319; rm -rf /", "203-319",
                    "../../evil", "203319/1/extra"):
            self.assertIsNone(futu.deep_link(bad),
                              f"{bad!r} must not be interpolated into a URL")

    def test_scheme_and_ids_are_the_verified_ones(self):
        """Provenance, asserted so a rename has to be deliberate: these come from
        Futu's own App Links tags on /deeplink/ (al:ios:url, al:android:package,
        al:ios:app_store_id)."""
        self.assertEqual(futu.SCHEME, "ftnn")
        self.assertEqual(futu.ANDROID_PACKAGE, "cn.futu.trader")
        self.assertEqual(futu.IOS_APP_STORE_ID, "592031984")


class AndroidIntent(unittest.TestCase):
    """intent:// is the easy platform only if the fallback is encoded."""

    def setUp(self):
        self.web = "https://www.futunn.com/en/stock/SMCI-US"
        self.url = futu.android_intent_url("203319", self.web)

    def test_carries_scheme_package_and_path(self):
        self.assertTrue(self.url.startswith("intent://quote/stockDetail/203319/1#Intent;"))
        self.assertIn("scheme=ftnn;", self.url)
        self.assertIn("package=cn.futu.trader;", self.url)
        self.assertTrue(self.url.endswith(";end"))

    def test_fallback_url_is_percent_encoded(self):
        """An unencoded fallback truncates at the first ';' or '&' and Chrome
        opens something other than the quote page."""
        self.assertIn("S.browser_fallback_url=https%3A%2F%2Fwww.futunn.com%2Fen%2Fstock%2FSMCI-US",
                      self.url)
        after = self.url.split("S.browser_fallback_url=", 1)[1]
        self.assertEqual(after, "https%3A%2F%2Fwww.futunn.com%2Fen%2Fstock%2FSMCI-US;end",
                         "raw '/' or ':' in the fallback would end the intent early")

    def test_no_id_means_no_intent(self):
        self.assertIsNone(futu.android_intent_url(None, self.web))
        self.assertIsNone(futu.android_intent_url("nope", self.web))


class PageParsing(unittest.TestCase):
    """The id must be the requested company's, or absent."""

    def test_reads_the_id_from_stock_info(self):
        self.assertEqual(futu._parse_stock_id(SMCI_PAGE, "SMCI-US"), "203319")

    def test_ignores_the_rails(self):
        """200001 appears before stock_info and 205513 after; neither is SMCI."""
        self.assertNotIn(futu._parse_stock_id(SMCI_PAGE, "SMCI-US"),
                         {"200001", "205513", "208805"})

    def test_a_page_for_another_symbol_is_refused(self):
        """The mislink guard. Futu 302s and serves odd pages often enough that
        'whatever came back' cannot be trusted to be what was asked for."""
        self.assertIsNone(futu._parse_stock_id(SMCI_PAGE, "AAPL-US"))

    def test_case_folding_does_not_defeat_the_check(self):
        self.assertEqual(futu._parse_stock_id(_page("smci", "us", "203319"), "SMCI-US"),
                         "203319")

    def test_non_us_venues_round_trip(self):
        cases = [("00700", "HK", "54047868453564"),
                 ("600519", "SH", "49649823542279"),
                 ("000001", "SZ", "33333243184257"),
                 ("BRK.B", "US", "203520")]
        for code, market, sid in cases:
            with self.subTest(code=code):
                self.assertEqual(
                    futu._parse_stock_id(_page(code, market, sid), f"{code}-{market}"),
                    sid)

    def test_missing_or_moved_structure_yields_none(self):
        for html in ("", "<html></html>",
                     '{"stock_info":{"name":"x","marketLabel":"US"}}',      # no id
                     '{"stock_info":{"stockCode":"SMCI","stockId":"203319"}}',  # no market
                     '{"other":{"stockCode":"SMCI","stockId":"1","marketLabel":"US"}}'):
            with self.subTest(html=html[:40]):
                self.assertIsNone(futu._parse_stock_id(html, "SMCI-US"))

    def test_a_distant_id_is_not_scavenged(self):
        """stock_info is read through a bounded window; a stockId thousands of
        characters later belongs to something else."""
        html = ('{"stock_info":{"marketLabel":"US","stockCode":"SMCI"'
                + ',"filler":"' + "x" * 900 + '","stockId":"203319"}}')
        self.assertIsNone(futu._parse_stock_id(html, "SMCI-US"))


class _Isolated(unittest.TestCase):
    """Redirect every persistent thing ``futu`` owns into a temp dir.

    Both the id cache and the failure back-off are on disk by design, so a test
    that does not relocate them writes into the repo's ``cache/`` and leaves the
    developer's next /history/SMCI in a back-off window. The back-off also has to
    be a *fresh* instance per test, not just a fresh file: it keeps state in
    memory, so one test's recorded failure otherwise suppresses the next test's
    fetch and the failure surfaces somewhere unrelated.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        for p in (mock.patch.object(futu, "_IDS_PATH", tmp / "futu_ids.json"),
                  mock.patch.object(futu, "_ids", None),
                  mock.patch.object(futu.fetchguard, "_CACHE_DIR", tmp)):
            p.start()
            self.addCleanup(p.stop)

        fresh = futu.fetchguard.FailureBackoff("futu_ids_test", flush_interval=0.0)
        p = mock.patch.object(futu, "_backoff", fresh)
        p.start()
        self.addCleanup(p.stop)


class CacheIsolation(_Isolated):
    """Resolution caches forever; the request path must only ever peek."""

    def test_cached_stock_id_never_makes_a_request(self):
        """/history renders through this. A vendor fetch in front of a page
        render is the thing the AI-brief note in CLAUDE.md exists to forbid."""
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=AssertionError("fetched on the request path")):
            self.assertIsNone(futu.cached_stock_id("SMCI-US"))
            futu.remember_stock_id("SMCI-US", "203319")
            self.assertEqual(futu.cached_stock_id("SMCI-US"), "203319")

    def test_ids_survive_a_restart(self):
        futu.remember_stock_id("SMCI-US", "203319")
        futu._ids = None                      # simulate a fresh process
        self.assertEqual(futu.cached_stock_id("SMCI-US"), "203319")

    def test_lookup_is_case_insensitive_like_the_route(self):
        futu.remember_stock_id("smci-us", "203319")
        self.assertEqual(futu.cached_stock_id("SMCI-US"), "203319")

    def test_a_junk_id_is_never_stored(self):
        futu.remember_stock_id("SMCI-US", "not-an-id")
        self.assertIsNone(futu.cached_stock_id("SMCI-US"))

    def test_resolve_caches_and_then_stops_fetching(self):
        resp = mock.Mock(status_code=200, text=SMCI_PAGE)
        with mock.patch.object(futu.fetchguard, "request", return_value=resp) as req:
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
            self.assertEqual(req.call_count, 1, "second call must come from cache")

    def test_a_404_is_absence_not_an_error(self):
        """Futu not listing a symbol is normal; it must not raise into a render."""
        resp = mock.Mock(status_code=404, text="")
        with mock.patch.object(futu.fetchguard, "request", return_value=resp):
            self.assertIsNone(futu.resolve_stock_id("NOPE-US"))
            self.assertFalse(futu._backoff.ready("NOPE-US"),
                             "a miss must be remembered, or every page view refetches")

    def test_an_open_breaker_is_not_an_exception(self):
        err = futu.fetchguard.CooldownActive("futu", 30.0, "HTTP 429")
        with mock.patch.object(futu.fetchguard, "request", side_effect=err):
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))

    def test_a_transport_failure_is_not_an_exception(self):
        import requests
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=requests.Timeout("slow")):
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))

    def test_backoff_suppresses_a_known_bad_symbol(self):
        with mock.patch.object(futu._backoff, "ready", return_value=False):
            with mock.patch.object(futu.fetchguard, "request",
                                   side_effect=AssertionError("should not retry yet")):
                self.assertIsNone(futu.resolve_stock_id("NOPE-US"))

    def test_a_cached_id_is_returned_even_while_backed_off(self):
        """Back-off gates *fetching*, not reading. A symbol that failed once must
        not lose an id it already has."""
        futu.remember_stock_id("SMCI-US", "203319")
        with mock.patch.object(futu._backoff, "ready", return_value=False):
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")


class AmplificationGate(_Isolated):
    """/api/futu is public and maps any string to <IT>-US, so new resolutions
    need a ceiling — otherwise one cheap request in is a 1.3 MB fetch out, and
    enumerating invented tickers makes this box the abusive party."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(futu, "_rate_window_start", 0.0),
                  mock.patch.object(futu, "_rate_count", 0),
                  mock.patch.object(futu, "_RESOLVE_MAX_PER_WINDOW", 3),
                  mock.patch.object(futu, "_RESOLVE_WINDOW_SECONDS", 60.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_new_resolutions_stop_at_the_ceiling(self):
        resp = mock.Mock(status_code=404, text="")
        with mock.patch.object(futu.fetchguard, "request", return_value=resp) as req:
            for i in range(10):
                futu.resolve_stock_id(f"FAKE{i}-US")
            self.assertEqual(req.call_count, 3,
                             "an unbounded endpoint would have made 10 vendor fetches")

    def test_the_per_symbol_backoff_does_not_cover_this(self):
        """Every invented symbol is a fresh back-off key and therefore always
        ready — which is exactly why a separate global ceiling is needed."""
        self.assertTrue(all(futu._backoff.ready(f"NEW{i}-US") for i in range(5)))

    def test_the_window_reopens(self):
        resp = mock.Mock(status_code=404, text="")
        with mock.patch.object(futu.fetchguard, "request", return_value=resp) as req:
            for i in range(5):
                futu.resolve_stock_id(f"FAKE{i}-US")
            self.assertEqual(req.call_count, 3)
            futu._rate_window_start = 0.0     # as if the window had elapsed
            futu._rate_count = 0
            futu.resolve_stock_id("FAKE99-US")
            self.assertEqual(req.call_count, 4)

    def test_cache_hits_never_consume_a_slot(self):
        """Ordinary reading of known symbols must not be rationed."""
        futu.remember_stock_id("SMCI-US", "203319")
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=AssertionError("no fetch expected")):
            for _ in range(50):
                self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
        self.assertTrue(futu._resolve_slot_available(),
                        "50 cache hits must leave the window untouched")


class LinkContext(_Isolated):
    """What the template is handed. The fallback guarantee lives here."""

    def test_no_symbol_means_every_key_is_none(self):
        """The keys must still exist: Jinja treats a missing name as falsy, so a
        dropped key silently removes the link for every ticker."""
        ctx = futu.link_context(None)
        self.assertEqual(set(ctx), {"futu_symbol", "futu_web", "futu_deeplink", "futu_intent"})
        self.assertTrue(all(v is None for v in ctx.values()))

    def test_an_unresolved_symbol_still_gets_the_web_link(self):
        """The whole degradation story: no id, but the button still works."""
        ctx = futu.link_context("SMCI-US")
        self.assertEqual(ctx["futu_web"], "https://www.futunn.com/en/stock/SMCI-US")
        self.assertIsNone(ctx["futu_deeplink"])
        self.assertIsNone(ctx["futu_intent"])

    def test_a_resolved_symbol_gets_both_app_links(self):
        futu.remember_stock_id("SMCI-US", "203319")
        ctx = futu.link_context("SMCI-US")
        self.assertEqual(ctx["futu_deeplink"], "ftnn://quote/stockDetail/203319/1")
        self.assertIn("package=cn.futu.trader", ctx["futu_intent"])
        self.assertEqual(ctx["futu_web"], "https://www.futunn.com/en/stock/SMCI-US")

    def test_web_url_matches_the_url_the_template_hardcodes(self):
        """history.html builds the href itself; a divergence would make the
        intent fallback point somewhere the anchor does not."""
        html = (ROOT / "ystocker" / "templates" / "history.html").read_text(encoding="utf-8")
        m = re.search(r'href="(https://www\.futunn\.com/[^"]*)"', html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).replace("{{ futu_symbol }}", "SMCI-US"),
                         futu.web_url("SMCI-US"))


class TemplateWiring(unittest.TestCase):
    """The handler is only reachable if the markup calls it."""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "ystocker" / "templates" / "history.html").read_text(encoding="utf-8")
        cls.routes = (ROOT / "ystocker" / "routes.py").read_text(encoding="utf-8")

    def test_anchor_invokes_the_handler(self):
        self.assertIn('onclick="return futuApp(event)"', self.html)
        self.assertIn("function futuApp(ev)", self.html)

    def test_handler_defaults_to_following_the_href(self):
        """Every non-mobile / no-data branch must return true, or desktop breaks."""
        body = self.html.split("function futuApp(ev)", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!isAndroid && !isIOS) return true", body)
        self.assertIn("if (!intent) return true", body)
        self.assertIn("if (!deeplink) return true", body)

    def test_ios_fallback_survives_a_blocked_popup(self):
        """The timeout outlives the click gesture, so window.open can be blocked.
        Without the location.href fallback the button would do nothing at all on
        an iPhone that does not have Futubull installed — the exact silent
        failure this whole module exists to avoid."""
        body = self.html.split("function futuApp(ev)", 1)[1].split("\n}", 1)[0]
        self.assertIn("const opened = window.open(a.href, '_blank', 'noopener')", body)
        self.assertIn("if (!opened) window.location.href = a.href", body)

    def test_data_attributes_are_guarded_on_their_values(self):
        """An empty data-futu-deeplink="" is truthy-absent in dataset terms but
        would still be read as a string; only emit them when real."""
        self.assertIn("{% if futu_deeplink %}data-futu-deeplink=", self.html)
        self.assertIn("{% if futu_intent %}data-futu-intent=", self.html)

    def test_route_feeds_the_context(self):
        self.assertRegex(self.routes, r"\*\*futu\.link_context\(futu_symbol\)")

    def test_warm_endpoint_exists_and_is_not_on_the_render_path(self):
        self.assertIn('@bp.route("/api/futu/<ticker>")', self.routes)
        history = self.routes.split('@bp.route("/history/<ticker>")', 1)[1].split("@bp.route", 1)[0]
        self.assertNotIn("resolve_stock_id", history,
                         "resolving during the render puts a 1.3 MB vendor fetch "
                         "in front of the page")


if __name__ == "__main__":
    unittest.main()
