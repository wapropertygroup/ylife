"""CTA snapshot staleness — no network.

``cta.py`` has no upstream API: every number is hand-entered from a public
write-up of Goldman's weekly CTA Corner. So the failure mode is not a bad fetch,
it is a human forgetting — and the card rendered a month-old positioning reading
in the same neutral grey as yesterday's, which is what made it invisible. These
tests pin the age arithmetic and, in particular, the boundaries, because
off-by-one here is the difference between "stale" and "looks current".
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import unittest
from datetime import date

PATH = pathlib.Path(__file__).parents[1] / "ystocker" / "cta.py"
SPEC = importlib.util.spec_from_file_location("cta_under_test", PATH)
cta = importlib.util.module_from_spec(SPEC)
sys.modules["cta_under_test"] = cta
assert SPEC.loader
SPEC.loader.exec_module(cta)

TODAY = date(2026, 8, 28)


class Thresholds(unittest.TestCase):
    def test_weekly_cadence_assumption(self):
        """Goldman publishes weekly; the bands are multiples of that."""
        self.assertEqual(cta.FRESH_DAYS, 10)   # one cycle + slack
        self.assertEqual(cta.STALE_DAYS, 21)   # three cycles

    def test_boundaries_are_exact(self):
        """`fresh` up to and including FRESH_DAYS; `stale` strictly past STALE_DAYS."""
        cases = [
            (0,  "fresh"),
            (10, "fresh"),   # last fresh day
            (11, "aging"),   # first aging day
            (21, "aging"),   # last aging day
            (22, "stale"),   # first stale day
            (31, "stale"),
        ]
        for age, want in cases:
            with self.subTest(age=age):
                day = date.fromordinal(TODAY.toordinal() - age).isoformat()
                self.assertEqual(cta._staleness(day, TODAY)["level"], want)
                self.assertEqual(cta._staleness(day, TODAY)["report_age_days"], age)


class UnknownIsNotFresh(unittest.TestCase):
    """An unreadable or impossible date must not be treated as current."""

    def test_unparseable(self):
        for bad in ("not-a-date", "", None, 20260728, {}, []):
            with self.subTest(value=bad):
                out = cta._staleness(bad, TODAY)
                self.assertEqual(out["level"], "unknown")
                self.assertIsNone(out["report_age_days"])

    def test_future_date_is_a_data_entry_error_not_freshness(self):
        out = cta._staleness("2029-01-01", TODAY)
        self.assertEqual(out["level"], "unknown")
        self.assertLess(out["report_age_days"], 0)


class PayloadContract(unittest.TestCase):
    def test_freshness_rides_with_the_payload(self):
        """Consumers must not each reimplement the thresholds and drift."""
        out = cta.get_cta_positioning()
        f = out["freshness"]
        for key in ("report_age_days", "level", "fresh_days", "stale_days"):
            self.assertIn(key, f)
        self.assertEqual(f["fresh_days"], cta.FRESH_DAYS)
        self.assertEqual(f["stale_days"], cta.STALE_DAYS)

    def test_level_matches_the_built_in_report_date(self):
        out = cta.get_cta_positioning()
        expected = cta._staleness(out["latest"]["report_date"])
        self.assertEqual(out["freshness"]["level"], expected["level"])

    def test_built_in_snapshot_is_currently_stale(self):
        """Documents the state that prompted this: the shipped data is old.

        Not a failure — it records that the built-in payload is a fallback, and
        that the honest thing is to say so on the card rather than to fetch
        something and call it Goldman.
        """
        out = cta.get_cta_positioning()
        self.assertEqual(out["source_mode"], "built_in")
        self.assertGreater(out["freshness"]["report_age_days"], cta.STALE_DAYS)

    def test_status_line_never_raises(self):
        self.assertIn("cta:", cta.staleness_line())

    def test_status_line_survives_a_broken_payload(self):
        from unittest import mock
        with mock.patch.object(cta, "get_cta_positioning",
                               side_effect=RuntimeError("boom")):
            self.assertIn("unavailable", cta.staleness_line())


class SsmOverrideStillWorks(unittest.TestCase):
    """The one-command update path must keep working, and refresh the age."""

    def test_override_updates_report_date_and_freshness(self):
        import json
        import os
        from unittest import mock
        recent = date.fromordinal(date.today().toordinal() - 2).isoformat()
        payload = json.dumps({"latest": {"report_date": recent,
                                         "spx_triggers": {"short": 7500.0,
                                                          "medium": 7200.0,
                                                          "long": 6800.0}}})
        with mock.patch.dict(os.environ, {"GOLDMAN_CTA_DATA_JSON": payload}):
            out = cta.get_cta_positioning()
        self.assertEqual(out["source_mode"], "ssm")
        self.assertEqual(out["latest"]["report_date"], recent)
        self.assertEqual(out["freshness"]["level"], "fresh")
        self.assertEqual(out["freshness"]["report_age_days"], 2)

    def test_malformed_override_falls_back_without_claiming_freshness(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"GOLDMAN_CTA_DATA_JSON": "{not json"}):
            out = cta.get_cta_positioning()
        self.assertEqual(out["source_mode"], "built_in")
        # Still stale, because falling back must not reset the clock.
        self.assertEqual(out["freshness"]["level"], "stale")


REAL_ARTICLE = (
    "<p>Goldman flags three support thresholds: short-term 7,455 , "
    "medium-term 7,204 , long-term 6,765 . How large is the $184.3 billion "
    "worst-case selling wave</p>")


class Parsing(unittest.TestCase):
    """Verbatim phrasing from the real source article."""

    def test_extracts_the_three_levels_and_the_flow(self):
        out = cta.parse_article(REAL_ARTICLE)
        self.assertEqual(out["spx_triggers"],
                         {"short": 7455.0, "medium": 7204.0, "long": 6765.0})
        self.assertEqual(out["flows_1m_global_bn"]["down"], -184.3)

    def test_no_levels_returns_none_rather_than_a_guess(self):
        self.assertIsNone(cta.parse_article("<p>Goldman said things about CTAs.</p>"))
        self.assertIsNone(cta.parse_article(""))

    def test_html_and_entities_are_stripped(self):
        html = ("<div><script>var x='short-term 1'</script>"
                "short-term 7,455 &amp; medium-term 7,204 , long-term 6,765</div>")
        out = cta.parse_article(html)
        self.assertEqual(out["spx_triggers"]["short"], 7455.0)


class ValidationFailsClosed(unittest.TestCase):
    """The gate, not the parser, is what makes this safe to run unattended."""

    GOOD = {"spx_triggers": {"short": 7455.0, "medium": 7204.0, "long": 6765.0}}

    def test_good_parse_passes(self):
        for ref in (7700.0, None):
            ok, why = cta._validate(self.GOOD, ref)
            self.assertTrue(ok, why)

    def test_the_failure_i_used_as_a_reason_not_to_build_this(self):
        """"short-term 7.46k" yielding 7 instead of 7455 must be rejected."""
        ok, why = cta._validate(
            {"spx_triggers": {"short": 7.0, "medium": 5.0, "long": 3.0}}, 7700.0)
        self.assertFalse(ok)
        self.assertIn("from S&P", why)

    def test_scrambled_labels_rejected_by_goldmans_own_ordering(self):
        ok, why = cta._validate(
            {"spx_triggers": {"short": 6765.0, "medium": 7204.0, "long": 7455.0}}, 7700.0)
        self.assertFalse(ok)
        self.assertIn("not ordered", why)

    def test_a_year_picked_up_instead_of_a_level(self):
        ok, _ = cta._validate(
            {"spx_triggers": {"short": 2026.0, "medium": 2025.0, "long": 2024.0}}, 7700.0)
        self.assertFalse(ok)

    def test_incomplete_parse_rejected(self):
        ok, why = cta._validate({"spx_triggers": {"short": 7455.0, "medium": 7204.0}}, 7700.0)
        self.assertFalse(ok)
        self.assertIn("exactly", why)

    def test_absurd_flow_rejected(self):
        ok, why = cta._validate(
            dict(self.GOOD, flows_1m_global_bn={"down": -9999.0}), 7700.0)
        self.assertFalse(ok)
        self.assertIn("exceeds", why)

    def test_without_an_spx_reference_a_range_check_still_applies(self):
        """The weaker fallback must still reject the small-number mis-parse."""
        ok, _ = cta._validate(
            {"spx_triggers": {"short": 7.0, "medium": 5.0, "long": 3.0}}, None)
        self.assertFalse(ok)


class Precedence(unittest.TestCase):
    """ssm > fetched > built_in, so a human can always overrule the fetcher."""

    FETCHED = {"latest": {"report_date": "2026-08-27",
                          "spx_triggers": {"short": 7500.0, "medium": 7300.0,
                                           "long": 6900.0}},
               "fetched_from": "https://example.invalid/a"}

    def test_fetched_beats_built_in_when_newer(self):
        from unittest import mock
        with mock.patch.object(cta, "_read_fetched", return_value=self.FETCHED):
            out = cta.get_cta_positioning()
        self.assertEqual(out["source_mode"], "fetched")
        self.assertEqual(out["latest"]["report_date"], "2026-08-27")

    def test_stale_fetched_file_cannot_pull_the_card_backwards(self):
        from unittest import mock
        old = {"latest": {"report_date": "2026-01-01"}}
        with mock.patch.object(cta, "_read_fetched", return_value=old):
            out = cta.get_cta_positioning()
        self.assertEqual(out["source_mode"], "built_in")

    def test_ssm_override_still_wins_over_a_fetched_report(self):
        import json
        import os
        from unittest import mock
        manual = json.dumps({"latest": {"report_date": "2026-08-28",
                                        "spx_triggers": {"short": 1.0, "medium": 2.0,
                                                         "long": 3.0}}})
        with mock.patch.object(cta, "_read_fetched", return_value=self.FETCHED), \
             mock.patch.dict(os.environ, {"GOLDMAN_CTA_DATA_JSON": manual}):
            out = cta.get_cta_positioning()
        self.assertEqual(out["source_mode"], "ssm")
        self.assertEqual(out["latest"]["report_date"], "2026-08-28")


class ReportDateComesFromTheFeed(unittest.TestCase):
    """``report_date`` must be the article's date, never "today".

    The first version of the fetcher stamped ``date.today()``, which broke both
    things the date is used for. These tests reproduce each failure rather than
    just asserting the fixed behaviour.
    """

    FEED = ('<rss><channel><item>'
            '<title>Goldman Sachs: CTAs to Net Sell Across the Board</title>'
            '<link>https://example.invalid/cta-1</link>'
            '<pubDate>Mon, 10 Aug 2026 13:00:00 GMT</pubDate>'
            '</item></channel></rss>')

    def _fetch(self, feed=None, tmp=None, current=None):
        from unittest import mock
        feed = feed if feed is not None else self.FEED
        article = REAL_ARTICLE

        def fake_get(url):
            return feed if url == cta.REPORT_RSS_URL else article

        stack = [
            mock.patch.object(cta, "_http_get", fake_get),
            mock.patch.object(cta, "_FETCH_CACHE", tmp or "/dev/null"),
            mock.patch.object(cta, "_write_fetched", lambda p: None),
        ]
        if current is not None:
            stack.append(mock.patch.object(cta, "_read_fetched", return_value=current))
        with mock.patch.object(cta, "_http_get", fake_get), \
             mock.patch.object(cta, "_FETCH_CACHE", tmp or "/dev/null"), \
             mock.patch.object(cta, "_write_fetched", lambda p: None):
            if current is not None:
                with mock.patch.object(cta, "_read_fetched", return_value=current):
                    return cta.fetch_latest_report(spx_ref=7700.0)
            return cta.fetch_latest_report(spx_ref=7700.0)
    def test_pubdate_is_used_not_today(self):
        got = self._fetch()
        self.assertIsNotNone(got)
        self.assertEqual(got["latest"]["report_date"], "2026-08-10")
        self.assertNotEqual(got["latest"]["report_date"], date.today().isoformat())

    def test_an_old_article_is_dated_honestly_not_marked_fresh(self):
        """A report published four weeks ago must read as stale, not as new.

        Stamping today would have shown "fresh, 0 days" for numbers four weeks
        out of date — the exact misrepresentation the staleness work existed to
        remove. The date chosen is newer than the built-in snapshot (so the
        newness guard lets it through) but still past STALE_DAYS.
        """
        feed = self.FEED.replace("Mon, 10 Aug 2026 13:00:00 GMT",
                                 "Sat, 01 Aug 2026 13:00:00 GMT")
        got = self._fetch(feed=feed)
        self.assertEqual(got["latest"]["report_date"], "2026-08-01")
        self.assertEqual(cta._staleness("2026-08-01", TODAY)["level"], "stale")
        # And what the old code would have produced instead reads as fresh:
        self.assertEqual(cta._staleness(TODAY.isoformat(), TODAY)["level"], "fresh")

    def test_an_article_older_than_what_is_shown_is_refused(self):
        """Going backwards is worse than showing nothing new.

        The built-in snapshot is dated 2026-07-28, so a June article loses.
        """
        feed = self.FEED.replace("Mon, 10 Aug 2026 13:00:00 GMT",
                                 "Mon, 01 Jun 2026 13:00:00 GMT")
        self.assertIsNone(self._fetch(feed=feed))

    def test_the_same_article_twice_does_not_re_store(self):
        """Same-day idempotence: one article read twice stores once."""
        first = self._fetch()
        second = self._fetch(current=first)      # same article, snapshot in place
        self.assertIsNone(second, "an unchanged article must not re-store")

    def test_the_date_does_not_depend_on_when_it_is_read(self):
        """The property that closes the ratchet, tested where it actually lives.

        With ``report_date = date.today()``, re-reading one unchanged article on
        a later day yields a *newer* date than the stored one, so it stores again
        and the card announces a publication that never happened — ratcheting
        forward on every poll and reading "fresh" forever. Note the same-day test
        above cannot catch that: within one day ``today <= today`` holds and the
        guard appears to work.

        A pubDate is a property of the article, so the fix is that this is a pure
        function of the feed and two different reading days give one answer.
        """
        raw = "Mon, 10 Aug 2026 13:00:00 GMT"
        days = [date(2026, 8, 11), date(2026, 8, 20), date(2026, 9, 30)]
        got = {cta._pubdate_to_iso(raw, d) for d in days}
        self.assertEqual(got, {"2026-08-10"})
        # Whereas "today" would have produced a different answer on each of them.
        self.assertEqual(len({d.isoformat() for d in days}), 3)

    def test_a_genuinely_newer_article_still_wins(self):
        """The guard must not be so tight that a real new report is refused."""
        first = self._fetch()
        newer = self.FEED.replace("Mon, 10 Aug 2026 13:00:00 GMT",
                                  "Mon, 17 Aug 2026 13:00:00 GMT")
        second = self._fetch(feed=newer, current=first)
        self.assertIsNotNone(second)
        self.assertEqual(second["latest"]["report_date"], "2026-08-17")

    def test_missing_pubdate_falls_back_to_today_loudly(self):
        feed = re.sub(r"<pubDate>.*?</pubDate>", "", self.FEED)
        with self.assertLogs(cta.log, level="WARNING") as logs:
            got = self._fetch(feed=feed)
        self.assertEqual(got["latest"]["report_date"], date.today().isoformat())
        self.assertIn("no usable pubDate", "\n".join(logs.output))


class PubDateParsing(unittest.TestCase):
    REF = date(2026, 8, 28)

    def test_rfc822_forms(self):
        for raw, want in (
            ("Mon, 10 Aug 2026 13:00:00 GMT", "2026-08-10"),
            ("Mon, 10 Aug 2026 13:00:00 +0000", "2026-08-10"),
            ("10 Aug 2026 13:00:00 GMT", "2026-08-10"),
            ("Mon, 10 Aug 2026 23:59:59 -0700", "2026-08-10"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(cta._pubdate_to_iso(raw, self.REF), want)

    def test_unusable_values(self):
        for bad in (None, "", "   ", "not a date", 12345, [], "Mon, 32 Aug 2026"):
            with self.subTest(value=bad):
                self.assertIsNone(cta._pubdate_to_iso(bad, self.REF))

    def test_future_pubdate_refused_so_it_falls_back_rather_than_showing_unknown(self):
        """A future date is a clock error; passing it through would render the
        card 'unknown' and hide an otherwise-good parse behind a bad timestamp."""
        self.assertIsNone(cta._pubdate_to_iso("Mon, 10 Aug 2029 13:00:00 GMT", self.REF))
        # Today itself is fine — same-day publication is the normal case.
        self.assertEqual(cta._pubdate_to_iso("Fri, 28 Aug 2026 01:00:00 GMT", self.REF),
                         "2026-08-28")


if __name__ == "__main__":
    unittest.main(verbosity=2)
