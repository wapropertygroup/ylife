"""
Tests for the FuTu *app* link on /history/<ticker> — ``ystocker.futu``.

No app, no network, no writes outside a temp dir: the URL builders are pure
string work, and the page parser and classifier are fed pages Futu actually
served (``tests/fixtures/futu``) as well as fixtures shaped like the real
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
* **A WAF block is the box's, never the symbol's.** Futu refuses with a 302 to
  /403 or an HTTP 200 challenge page, and neither is an error status. Both must
  pause Futu for every worker and charge nothing to the symbol -- and must not
  be reported as a redesign, which is the false alarm the journal used to raise.
* **The pause fails open, the pace never waits, and no worker erases another's
  ids.** A corrupt marker that paused Futu would never be cleared, because
  nothing would fetch; a request thread that waited for the fetch slot would
  hold a worker for nothing; and a save that wrote one worker's dict over the
  file is how 38 of 192 resolved ids were lost.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ystocker import futu

ROOT = Path(__file__).resolve().parent.parent

# Real pages, captured on the production box on 2026-10-04:
#
# * quote_TSM-US.html -- the live quote page, trimmed from ~1.39 MB to its head
#   and window.__INITIAL_STATE__ (rails included). Parses to 202020.
# * waf403.html -- where `requests` landed after following the WAF's 302 to
#   https://www.futunn.com/403: an HTTP 200 page reading 访问频繁，请稍后重试.
# * wafchallenge_KLAC-US.html -- the HTTP 200 challenge served at the quote URL
#   itself. It embeds the requesting address as the client_ip claim of a
#   base64url JWT, which a plain search-and-replace does not see; that claim was
#   rewritten to 203.0.113.7 (TEST-NET-3). The token's signature was left as
#   served and no longer matches its payload, which nothing here checks.
FIXTURES = ROOT / "tests" / "fixtures" / "futu"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


TSM_PAGE = _fixture("quote_TSM-US.html")
WAF403_PAGE = _fixture("waf403.html")
CHALLENGE_PAGE = _fixture("wafchallenge_KLAC-US.html")


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


def _resp(status: int = 200, text: str = "", *, location: str | None = None,
          url: str = "https://www.futunn.com/en/stock/SMCI-US"):
    """A stand-in for a ``requests.Response`` with every attribute futu reads.

    A bare Mock invents whatever attribute it is asked for, so a test that left
    ``headers`` or ``url`` unset would be classifying a Mock object rather than
    the response it means to describe.
    """
    return mock.Mock(status_code=status, text=text,
                     headers={"Location": location} if location else {},
                     is_redirect=bool(location) and 300 <= status < 400,
                     url=url)


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

    def test_a_page_without_stock_info_is_not_called_a_redesign_here(self):
        """Only the classifier can tell a redesign from a WAF page -- both lack
        stock_info -- so the parser stays quiet and lets it decide."""
        with self.assertNoLogs("ystocker.futu", "WARNING"):
            self.assertIsNone(futu._parse_stock_id(WAF403_PAGE, "XOM-US"))


class RealPages(unittest.TestCase):
    """The parser against pages Futu actually served."""

    def test_the_live_quote_page_parses(self):
        self.assertEqual(futu._parse_stock_id(TSM_PAGE, "TSM-US"), "202020")

    def test_the_live_quote_page_is_refused_for_another_symbol(self):
        """ASML sits in this very page's rails with a stockId of its own -- the
        id a positional parse would hand back for ASML-US."""
        self.assertIn('"stockCode":"ASML"', TSM_PAGE)
        with self.assertLogs("ystocker.futu", "WARNING") as logs:
            self.assertIsNone(futu._parse_stock_id(TSM_PAGE, "ASML-US"))
        self.assertIn("identifies itself as TSM-US", "\n".join(logs.output))

    def test_neither_block_page_has_a_stock_info(self):
        """Why the old code blamed the parser: both block pages lack exactly what
        a redesign would lack."""
        for page in (WAF403_PAGE, CHALLENGE_PAGE):
            self.assertNotIn("stock_info", page)
            self.assertNotIn("__INITIAL_STATE__", page)

    def test_the_challenge_fixture_carries_no_real_address(self):
        """The challenge embeds the caller's IP in a base64url JWT's client_ip
        claim, invisible to a plain search-and-replace. Proven by decoding it,
        without writing the real address down here."""
        token = re.search(r"eyJ[\w-]+\.(eyJ[\w-]+)\.[\w-]+", CHALLENGE_PAGE).group(1)
        claims = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        self.assertEqual(claims["client_ip"], "203.0.113.7")


class Classification(unittest.TestCase):
    """found / blocked / miss, decided in the documented order."""

    def _verdict(self, resp, symbol="TSM-US"):
        return futu._classify_response(resp, symbol)[0]

    def test_the_quote_page_is_found(self):
        verdict, stock_id, _ = futu._classify_response(_resp(200, TSM_PAGE), "TSM-US")
        self.assertEqual((verdict, stock_id), (futu._FOUND, "202020"))

    def test_a_redirect_to_403_is_a_block_on_any_host(self):
        """The challenge script itself sends a client that comes back too fast
        to www.moomoo.com/403, so the host is no part of the test."""
        for location in ("https://www.futunn.com/403", "/403",
                         "https://www.futunn.com/403/",
                         "https://www.futunn.com/403?from=quote",
                         "https://www.moomoo.com/403"):
            with self.subTest(location=location):
                self.assertEqual(self._verdict(_resp(302, location=location)),
                                 futu._BLOCKED)

    def test_the_challenge_page_is_a_block(self):
        self.assertEqual(self._verdict(_resp(200, CHALLENGE_PAGE), "KLAC-US"),
                         futu._BLOCKED)

    def test_a_redirect_followed_anyway_is_recognised_where_it_landed(self):
        resp = _resp(200, WAF403_PAGE, url="https://www.futunn.com/403")
        self.assertEqual(self._verdict(resp, "XOM-US"), futu._BLOCKED)

    def test_a_block_is_no_longer_reported_as_a_redesign(self):
        """The false alarm: "no stock_info in XOM-US page — parser may need
        updating", logged for a page that had not changed."""
        for resp in (_resp(302, location="https://www.futunn.com/403"),
                     _resp(200, CHALLENGE_PAGE),
                     _resp(200, WAF403_PAGE, url="https://www.futunn.com/403")):
            with self.subTest(status=resp.status_code, url=resp.url), \
                    self.assertNoLogs("ystocker.futu", "WARNING"):
                self.assertEqual(self._verdict(resp, "XOM-US"), futu._BLOCKED)

    def test_other_redirects_are_misses_that_name_their_target(self):
        """Not followed any more, so a redirect Futu starts using for real pages
        must at least be visible in the journal."""
        with self.assertLogs("ystocker.futu", "WARNING") as logs:
            verdict = self._verdict(
                _resp(301, location="https://www.futunn.com/en/stock/BRK-B-US"), "BRK.B-US")
        self.assertEqual(verdict, futu._MISS)
        self.assertIn("/en/stock/BRK-B-US", "\n".join(logs.output))

    def test_a_malformed_location_is_a_miss_not_a_crash(self):
        with self.assertLogs("ystocker.futu", "WARNING"):
            self.assertEqual(self._verdict(_resp(302, location="http://[::1")),
                             futu._MISS)

    def test_a_404_is_a_miss(self):
        self.assertEqual(self._verdict(_resp(404)), futu._MISS)

    def test_a_429_is_a_block(self):
        """The one status that means "this caller" by definition."""
        self.assertEqual(self._verdict(_resp(429)), futu._BLOCKED)

    def test_a_quote_page_that_mentions_the_waf_is_still_a_quote(self):
        """(b) before (d): the markers are only consulted on pages that are not
        quotes, which is what makes two plain strings safe to key on."""
        page = TSM_PAGE.replace("</body>",
                                "<script>var k='wafToken=';var s='WAF_EXPIRED'</script></body>")
        verdict, stock_id, _ = futu._classify_response(_resp(200, page), "TSM-US")
        self.assertEqual((verdict, stock_id), (futu._FOUND, "202020"))

    def test_a_redesigned_quote_page_is_a_parser_fault_not_a_block(self):
        """(c) before (d), and the one case that earns the parser warning."""
        page = ('<script>window.__INITIAL_STATE__={"quote":{"code":"TSM"}}</script>'
                "<script>var s='WAF_EXPIRED'</script>")
        with self.assertLogs("ystocker.futu", "WARNING") as logs:
            self.assertEqual(self._verdict(_resp(200, page)), futu._MISS)
        self.assertIn("parser may need updating", "\n".join(logs.output))

    def test_challenge_and_slider_are_not_markers(self):
        """Both words occur in ordinary pages; keying on them would pause Futu
        for every worker over a page that merely mentions a slider."""
        page = "<html><title>Slider challenge</title><body>challenge slider</body></html>"
        with self.assertLogs("ystocker.futu", "WARNING"):
            self.assertEqual(self._verdict(_resp(200, page)), futu._MISS)

    def test_an_unknown_page_is_a_miss_that_says_what_it_was(self):
        with self.assertLogs("ystocker.futu", "WARNING") as logs:
            self.assertEqual(self._verdict(_resp(200, "<title>Maintenance</title>back soon")),
                             futu._MISS)
        self.assertIn("'Maintenance'", "\n".join(logs.output))


class _Isolated(unittest.TestCase):
    """Redirect every persistent thing ``futu`` owns into a temp dir.

    The id cache and its lock, the pace stamp, the fetch lock and the WAF marker
    are all on disk by design -- they are how the workers agree -- so a test that
    does not relocate them writes into the repo's ``cache/`` and leaves the
    developer's next /history/SMCI paced, paused or without its id. The back-off
    also has to be a *fresh* instance per test, not just a fresh file: it keeps
    state in memory, so one test's recorded failure otherwise suppresses the next
    test's fetch and the failure surfaces somewhere unrelated. fetchguard's
    breaker is process-global for the same reason, so it is emptied for each test
    and restored afterwards.
    """

    _FILES = {"_IDS_PATH": "futu_ids.json", "_IDS_LOCK_PATH": "futu_ids.lock",
              "_PACE_PATH": "futu_last_fetch", "_FETCH_LOCK_PATH": "futu_fetch.lock",
              "_WAF_PATH": "futu_waf_until"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        patches = [mock.patch.object(futu, name, self.tmp / filename)
                   for name, filename in self._FILES.items()]
        patches += [mock.patch.object(futu, "_CACHE_DIR", self.tmp),
                    mock.patch.object(futu, "_ids", None),
                    mock.patch.object(futu, "_ids_sig", None),
                    mock.patch.object(futu, "_last_claim", 0.0),
                    mock.patch.object(futu, "_waf_bad_sig", None),
                    mock.patch.object(futu.fetchguard, "_CACHE_DIR", self.tmp),
                    mock.patch.dict(futu.fetchguard._cooldowns, clear=True),
                    # No test reaches futunn.com. One that expected a refusal and
                    # did not get one used to fetch the real page instead -- and
                    # pass or fail on what Futu happened to answer.
                    mock.patch.object(futu.fetchguard, "request",
                                      side_effect=AssertionError("a test reached the network"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        fresh = futu.fetchguard.FailureBackoff("futu_ids_test", flush_interval=0.0)
        p = mock.patch.object(futu, "_backoff", fresh)
        p.start()
        self.addCleanup(p.stop)


class IsolationGuard(_Isolated):

    def test_every_path_the_module_owns_is_relocated(self):
        """A path added to futu but not to _Isolated writes into the repo's
        cache/. It happened while this suite was being extended: the old
        isolation left a pace stamp there, and the next test's fetch was refused
        by a test that had already finished."""
        stray = {name: str(value) for name, value in vars(futu).items()
                 if isinstance(value, Path)
                 and value != self.tmp and self.tmp not in value.parents}
        self.assertEqual(stray, {})


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
        with mock.patch.object(futu.fetchguard, "request",
                               return_value=_resp(200, SMCI_PAGE)) as req:
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
            self.assertEqual(req.call_count, 1, "second call must come from cache")

    def test_a_404_is_absence_not_an_error(self):
        """Futu not listing a symbol is normal; it must not raise into a render."""
        with mock.patch.object(futu.fetchguard, "request", return_value=_resp(404)):
            self.assertIsNone(futu.resolve_stock_id("NOPE-US"))
            self.assertFalse(futu._backoff.ready("NOPE-US"),
                             "a miss must be remembered, or every page view refetches")

    def test_an_open_breaker_is_not_an_exception(self):
        """Raised inside the fetch when the breaker opens after the pre-check.
        That is Futu's state, not the symbol's, so nothing is charged to it."""
        err = futu.fetchguard.CooldownActive("futu", 30.0, "HTTP 429")
        with mock.patch.object(futu.fetchguard, "request", side_effect=err):
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))
        self.assertTrue(futu._backoff.ready("SMCI-US"))

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


class WafPause(_Isolated):
    """A block pauses Futu for every worker, and charges nothing to the symbol."""

    T = 2_000_000_000.0                 # an injected clock, for the window arithmetic

    def _resolve(self, resp, symbol="XOM-US"):
        with mock.patch.object(futu.fetchguard, "request", return_value=resp) as req:
            return futu.resolve_stock_id(symbol), req

    def _assert_paused_but_not_charged(self, symbol):
        self.assertGreater(futu.fetchguard.cooldown_remaining("futu"), 1700,
                           "this worker's breaker")
        until = float(futu._WAF_PATH.read_text(encoding="utf-8"))
        self.assertAlmostEqual(until - time.time(), 1800, delta=10,
                               msg="every worker's marker: 30 minutes, first time")
        self.assertTrue(futu._backoff.ready(symbol), "a block is not the symbol's fault")

    def test_a_302_to_403_pauses_futu_not_the_symbol(self):
        result, _ = self._resolve(_resp(302, location="https://www.futunn.com/403"))
        self.assertIsNone(result)
        self._assert_paused_but_not_charged("XOM-US")

    def test_the_challenge_page_pauses_futu_not_the_symbol(self):
        result, _ = self._resolve(_resp(200, CHALLENGE_PAGE), "KLAC-US")
        self.assertIsNone(result)
        self._assert_paused_but_not_charged("KLAC-US")

    def test_a_429_pauses_futu_too(self):
        self.assertIsNone(self._resolve(_resp(429))[0])
        self._assert_paused_but_not_charged("XOM-US")

    def test_redirects_are_not_followed_and_nothing_is_retried(self):
        _, req = self._resolve(_resp(200, SMCI_PAGE), "SMCI-US")
        kwargs = req.call_args.kwargs
        self.assertIs(kwargs.get("allow_redirects"), False,
                      "followed, the block arrives as an HTTP 200 page with no stock_info")
        self.assertEqual(kwargs.get("retries"), 0,
                         "a retry is a second fetch seconds after the first")

    def test_a_marker_from_another_worker_stops_this_one_before_any_request(self):
        futu._record_waf_block("recorded by another worker")
        futu.fetchguard.reset("futu")   # the breaker is per process; this one never saw it
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=AssertionError("fetched during a WAF pause")):
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))
        self.assertFalse(futu._PACE_PATH.exists(),
                         "a fetch that is not going to happen must not spend the slot")
        self.assertGreater(futu.fetchguard.cooldown_remaining("futu"), 0,
                           "mirrored into this worker's breaker, so the next check "
                           "reads no file")

    def test_a_success_clears_the_marker(self):
        """An expired marker stays on disk -- it is what makes the next block
        double -- until a quote page comes back."""
        futu._record_waf_block("earlier", now=time.time() - 3600)
        futu.fetchguard.reset("futu")
        self.assertTrue(futu._WAF_PATH.exists())
        self.assertEqual(self._resolve(_resp(200, SMCI_PAGE), "SMCI-US")[0], "203319")
        self.assertFalse(futu._WAF_PATH.exists())

    def test_consecutive_blocks_double_up_to_the_cap(self):
        t, windows = self.T, []
        for _ in range(7):
            windows.append(futu._record_waf_block("blocked again", now=t))
            until, _ = futu._read_waf_marker()
            self.assertAlmostEqual(until - t, windows[-1], places=2)
            t = until + 1.0             # the pause ran out; the first fetch after it was blocked
        self.assertEqual(windows, [1800, 3600, 7200, 14400, 21600, 21600, 21600])

    def test_a_success_starts_the_next_pause_from_the_beginning(self):
        futu._record_waf_block("one", now=self.T)
        self.assertEqual(futu._record_waf_block("two", now=self.T + 1801), 3600)
        futu._clear_waf_block()
        self.assertEqual(futu._record_waf_block("three", now=self.T + 9000), 1800)

    def test_a_block_inside_a_pause_does_not_lengthen_it(self):
        """A request already on the wire when the block landed is the same
        event, not a second one."""
        futu._record_waf_block("first", now=self.T)
        self.assertEqual(futu._record_waf_block("in flight", now=self.T + 5), 1800)
        self.assertAlmostEqual(futu._read_waf_marker()[0], self.T + 1800, places=2)

    def test_a_corrupt_marker_does_not_pause_futu(self):
        """Fail open: a marker that paused Futu could never be cleared, because
        nothing would fetch to clear it."""
        with self.assertLogs("ystocker.futu", "WARNING"):
            for raw in ("", "garbage", "nan", "inf", "-inf", "1e300", "12 34",
                        f"{time.time() + 10 * 86_400:.3f}"):   # no block gets 10 days
                with self.subTest(raw=raw):
                    futu._WAF_PATH.write_text(raw, encoding="utf-8")
                    self.assertEqual(futu._waf_remaining(), 0.0)
        with mock.patch.object(futu.fetchguard, "request",
                               return_value=_resp(200, SMCI_PAGE)) as req:
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
        req.assert_called_once()

    def test_a_failed_marker_write_leaves_the_old_marker_whole(self):
        futu._record_waf_block("first", now=self.T)
        before = futu._WAF_PATH.read_text(encoding="utf-8")
        with mock.patch.object(futu.os, "replace", side_effect=OSError("disk full")), \
                self.assertLogs("ystocker.futu", "WARNING"):
            futu._record_waf_block("second", now=self.T + 1801)
        self.assertEqual(futu._WAF_PATH.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])
        self.assertGreater(futu.fetchguard.cooldown_remaining("futu"), 0,
                           "this worker still pauses")

    def test_a_clock_stepped_back_cannot_stretch_a_pause(self):
        futu._record_waf_block("x", now=self.T)
        self.assertLessEqual(futu._waf_remaining(now=self.T - 86_400), 1800.0)

    def test_an_open_breaker_is_checked_before_the_slot(self):
        futu.fetchguard.trip("futu", 60.0, "HTTP 429")
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=AssertionError("asked while cooling down")):
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))
        self.assertTrue(futu._backoff.ready("SMCI-US"))
        self.assertFalse(futu._PACE_PATH.exists())


class GlobalPacing(_Isolated):
    """/api/futu is public and maps any string to <IT>-US, so new resolutions
    need a ceiling -- otherwise one cheap request in is a 1.3 MB fetch out, and
    enumerating invented tickers makes this box the abusive party. It is one
    fetch per gap for the whole box, and a refusal never waits."""

    def _stamp(self, when: float) -> None:
        """As if another worker had claimed the slot at *when*."""
        futu._PACE_PATH.write_text(f"{when:.3f}\n", encoding="utf-8")

    def test_invented_symbols_get_one_fetch_per_gap(self):
        with mock.patch.object(futu.fetchguard, "request", return_value=_resp(404)) as req:
            for i in range(10):
                futu.resolve_stock_id(f"FAKE{i}-US")
        self.assertEqual(req.call_count, 1,
                         "the old ceiling allowed ten a minute per worker")

    def test_the_gap_is_shared_with_other_workers(self):
        self._stamp(time.time())
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=AssertionError("fetched inside another worker's gap")):
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))

    def test_the_slot_reopens_after_the_gap(self):
        self._stamp(time.time() - futu._MIN_GAP_SECONDS - 1)
        with mock.patch.object(futu.fetchguard, "request", return_value=_resp(200, SMCI_PAGE)):
            self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
        self.assertAlmostEqual(float(futu._PACE_PATH.read_text(encoding="utf-8")),
                               time.time(), delta=5)

    def test_a_refused_slot_never_waits(self):
        self._stamp(time.time())
        with mock.patch.object(futu.time, "sleep",
                               side_effect=AssertionError("slept on a request thread")), \
                mock.patch.object(futu.fetchguard, "request",
                                  side_effect=AssertionError("fetched inside the gap")):
            started = time.monotonic()
            self.assertIsNone(futu.resolve_stock_id("SMCI-US"))
            self.assertLess(time.monotonic() - started, 1.0)

    def test_a_stamp_a_hair_ahead_is_a_fresh_fetch_not_a_clock_step(self):
        """Stamps are written to the millisecond, so one written a microsecond
        ago can read as slightly in the future. Treating that as a clock that
        moved let a second fetch through right behind the first."""
        now = time.time()
        self._stamp(now + 0.0004)
        self.assertFalse(futu._resolve_slot_available(now=now))
        self._stamp(now + futu._MIN_GAP_SECONDS + 1)    # beyond a gap: the clock moved
        self.assertTrue(futu._resolve_slot_available(now=now))

    def test_a_busy_lock_is_a_refusal_not_a_wait(self):
        """Another worker inside the critical section is claiming the slot itself."""
        futu._FETCH_LOCK_PATH.touch()
        with open(futu._FETCH_LOCK_PATH, "a+") as other_worker:
            fcntl.flock(other_worker.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            self.assertFalse(futu._resolve_slot_available())
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(futu._resolve_slot_available(), "released, the slot is free again")

    def test_a_corrupt_or_future_stamp_does_not_wedge_resolution(self):
        """A stamp in the future is a clock that moved; honouring it could refuse
        for as long as the step."""
        for raw in ("", "garbage", "nan", f"{time.time() + 3600:.3f}"):
            with self.subTest(raw=raw):
                futu._PACE_PATH.write_text(raw, encoding="utf-8")
                futu._last_claim = 0.0
                self.assertTrue(futu._resolve_slot_available())

    def test_an_unwritable_stamp_still_paces_this_worker(self):
        with mock.patch.object(futu, "_write_atomic", side_effect=OSError("read-only")), \
                self.assertLogs("ystocker.futu", "WARNING"):
            self.assertTrue(futu._resolve_slot_available())
            self.assertFalse(futu._resolve_slot_available(),
                             "this worker's own floor still holds")

    def test_the_per_symbol_backoff_does_not_cover_this(self):
        """Every invented symbol is a fresh back-off key and therefore always
        ready — which is exactly why a separate global ceiling is needed."""
        self.assertTrue(all(futu._backoff.ready(f"NEW{i}-US") for i in range(5)))

    def test_cache_hits_never_consume_the_slot(self):
        """Ordinary reading of known symbols must not be rationed."""
        futu.remember_stock_id("SMCI-US", "203319")
        with mock.patch.object(futu.fetchguard, "request",
                               side_effect=AssertionError("no fetch expected")):
            for _ in range(50):
                self.assertEqual(futu.resolve_stock_id("SMCI-US"), "203319")
        self.assertFalse(futu._PACE_PATH.exists())
        self.assertTrue(futu._resolve_slot_available(),
                        "50 cache hits must leave the slot untouched")


class SharedIdCache(_Isolated):
    """Several workers, one file: an id learned by any must be served by all.
    (The ids below other than SMCI's are synthetic.)"""

    def _other_worker_writes(self, ids: dict[str, str]) -> None:
        """Write the file the way another worker does: whole, through os.replace."""
        staged = self.tmp / "other-worker.json"
        staged.write_text(json.dumps(ids, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(staged, futu._IDS_PATH)

    def _on_disk(self) -> dict[str, str]:
        return json.loads(futu._IDS_PATH.read_text(encoding="utf-8"))

    def test_a_save_keeps_an_id_another_worker_wrote_after_this_one_loaded(self):
        """The lost-id bug. Each worker wrote its own dict over the file, so an
        id another worker saved in between was erased: 38 of 192 on the box,
        SHOP-US, JD-US, TSLA-US and EWY-US among them."""
        futu.remember_stock_id("SMCI-US", "203319")
        real_load = futu._load_locked

        def load_then_lose_the_race():
            ids = real_load()
            self._other_worker_writes({"SMCI-US": "203319", "JD-US": "900101"})
            return ids

        with mock.patch.object(futu, "_load_locked", side_effect=load_then_lose_the_race):
            futu.remember_stock_id("TSLA-US", "900102")
        self.assertEqual(self._on_disk(),
                         {"SMCI-US": "203319", "JD-US": "900101", "TSLA-US": "900102"})
        self.assertEqual(futu.cached_stock_id("JD-US"), "900101",
                         "and this worker now serves it as well")

    def test_a_worker_sees_an_id_another_worker_resolved(self):
        """MSFT-US was resolved three times because each worker only ever read
        the file once."""
        futu.remember_stock_id("SMCI-US", "203319")
        self.assertIsNone(futu.cached_stock_id("SHOP-US"))
        self._other_worker_writes({"SMCI-US": "203319", "SHOP-US": "900103"})
        self.assertEqual(futu.cached_stock_id("SHOP-US"), "900103")

    def test_an_unchanged_file_is_not_reread(self):
        """The request path pays one stat(), not a parse."""
        futu.remember_stock_id("SMCI-US", "203319")
        with mock.patch.object(futu, "_read_ids_file",
                               side_effect=AssertionError("re-parsed an unchanged file")):
            for _ in range(5):
                self.assertEqual(futu.cached_stock_id("SMCI-US"), "203319")

    def test_an_id_this_worker_could_not_save_survives_a_reload(self):
        futu.remember_stock_id("SMCI-US", "203319")
        with mock.patch.object(futu, "_write_atomic", side_effect=OSError("disk full")), \
                self.assertLogs("ystocker.futu", "WARNING"):
            futu.remember_stock_id("EWY-US", "900104")
        self.assertEqual(futu.cached_stock_id("EWY-US"), "900104")
        self._other_worker_writes({"SMCI-US": "203319", "MSFT-US": "900105"})
        self.assertEqual(futu.cached_stock_id("MSFT-US"), "900105")
        self.assertEqual(futu.cached_stock_id("EWY-US"), "900104",
                         "a reload must add to what this worker holds, not replace it")
        futu.remember_stock_id("TSLA-US", "900106")
        self.assertEqual(set(self._on_disk()), {"SMCI-US", "MSFT-US", "EWY-US", "TSLA-US"},
                         "the next save carries the unsaved id to disk")

    def test_a_file_that_cannot_be_read_is_not_overwritten(self):
        """Writing this worker's dict over a file it could not read is the
        clobber itself."""
        futu.remember_stock_id("SMCI-US", "203319")
        before = futu._IDS_PATH.read_text(encoding="utf-8")
        with mock.patch.object(futu, "_read_ids_file", side_effect=PermissionError("denied")), \
                self.assertLogs("ystocker.futu", "WARNING"):
            futu.remember_stock_id("JD-US", "900101")
        self.assertEqual(futu._IDS_PATH.read_text(encoding="utf-8"), before)
        self.assertEqual(futu.cached_stock_id("JD-US"), "900101", "kept in memory instead")

    def test_a_corrupt_file_reads_as_empty_and_is_repaired_on_save(self):
        futu._IDS_PATH.write_text("{not json", encoding="utf-8")
        with self.assertLogs("ystocker.futu", "WARNING"):
            self.assertIsNone(futu.cached_stock_id("SMCI-US"))
            futu.remember_stock_id("SMCI-US", "203319")
        self.assertEqual(self._on_disk(), {"SMCI-US": "203319"})

    def test_saves_leave_no_temp_files(self):
        for i in range(3):
            futu.remember_stock_id(f"T{i}-US", f"90020{i}")
        self.assertEqual(len(self._on_disk()), 3)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])


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
