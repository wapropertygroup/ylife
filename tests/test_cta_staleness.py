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
import json
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


#: Verbatim from the live article (2026-07-28), as ``_visible_text`` leaves it.
#: Every number here has been checked against the hand-entered built-in payload,
#: which is the only ground truth available for this source.
ARTICLE_FLOWS = (
    "01 Why are CTAs selling no matter what happens? • CTAs — trend-following "
    "quant funds — are net sellers globally next week whether the market rises, "
    "falls, or stays flat . • Flat scenario: ~$7.48 bn global net selling "
    "( ~$5.96 bn in US equities). Up scenario: still ~$275 mn global net selling "
    "( ~$2.03 bn in US stocks). Down scenario: ~$31.46 bn global ( ~$15.1 bn US). "
    "• This means → even a rally won't trigger fresh buying. "
    "02 How large is the $184.3 billion worst-case selling wave? • Goldman's "
    "one-month downside scenario: global CTA net selling surges to ~$184.3 billion , "
    "with US equities accounting for ~$71.1 billion . • For contrast, the "
    "one-month upside scenario flips to global net buying of ~$34.39 billion "
    "(US ~$8.31 bn ). 03 Where are the key support levels for the S&P 500? • "
    "Goldman flags three support thresholds: short-term 7,455 , medium-term 7,204 , "
    "long-term 6,765 .")


class FlowParsing(unittest.TestCase):
    """The scenario tables. Every assertion is a value from the real article."""

    def setUp(self):
        self.out = cta.parse_article("<p>%s</p>" % ARTICLE_FLOWS)

    def test_the_1000x_unit_trap(self):
        """"$275 mn" among "bn" neighbours. Reading it as billions is 1000x off
        and lands *inside* MAX_FLOW_BN, so no other gate would catch it."""
        self.assertEqual(self.out["flows_1w_global_bn"]["up"], -0.275)

    def test_sign_comes_from_the_verb_not_the_position(self):
        """"net buying" is positive, "net selling" negative, in the same table."""
        self.assertEqual(self.out["flows_1m_global_bn"]["up"], 34.39)
        self.assertEqual(self.out["flows_1m_global_bn"]["down"], -184.3)

    def test_a_scenario_with_no_verb_is_refused_not_guessed(self):
        """The 1-week down scenario states no direction of its own.

        The sentence after it ends "...won't trigger fresh buying", and an
        unbounded window read that as a purchase — recording +31.46bn where the
        report means a sale of 31.46bn. An inverted flow is worse than a missing
        one, so the span stops at the bullet and the value is dropped.
        """
        self.assertNotIn("down", self.out["flows_1w_global_bn"])
        self.assertNotIn("down", self.out["flows_1w_us_bn"])

    def test_us_figures_are_split_out(self):
        self.assertEqual(self.out["flows_1w_us_bn"]["flat"], -5.96)
        self.assertEqual(self.out["flows_1m_us_bn"]["down"], -71.1)

    def test_matches_the_hand_entered_values(self):
        built_in = cta._PUBLIC_DATA["latest"]
        for key, value in self.out["flows_1w_global_bn"].items():
            self.assertEqual(value, built_in["flows_1w_global_bn"][key], key)
        self.assertEqual(self.out["flows_1m_global_bn"]["down"],
                         built_in["flows_1m_global_bn"]["down"])

    def test_the_real_parse_validates(self):
        """Regression: a gate must not reject the only real article there is.

        A "US cannot exceed global" check looked like arithmetic and rejected
        this: 1-week up is $275mn global against $2.03bn US, because global net
        is a sum of regional nets that offset. The check is gone.
        """
        ok, why = cta._validate(self.out, 7739.0)
        self.assertTrue(ok, why)

    def test_absurd_flow_in_any_bucket_is_rejected(self):
        for bucket in ("flows_1w_global_bn", "flows_1w_us_bn",
                       "flows_1m_global_bn", "flows_1m_us_bn"):
            with self.subTest(bucket=bucket):
                bad = dict(self.out, **{bucket: {"down": -9999.0}})
                ok, why = cta._validate(bad, 7739.0)
                self.assertFalse(ok)
                self.assertIn("exceeds", why)

    def test_flows_are_optional_and_never_block_the_triggers(self):
        """A phrasing change must cost that number, not the whole report."""
        out = cta.parse_article(
            "<p>Goldman flags short-term 7,455 , medium-term 7,204 , "
            "long-term 6,765 and says nothing about flows.</p>")
        self.assertEqual(out["spx_triggers"]["short"], 7455.0)
        self.assertEqual(out["flows_1m_global_bn"], {})
        self.assertTrue(cta._validate(out, 7739.0)[0])

    def test_money_units(self):
        self.assertEqual(cta._money_bn("$7.48 bn"), [7.48])
        self.assertEqual(cta._money_bn("$275 mn"), [0.275])
        self.assertEqual(cta._money_bn("$184.3 billion"), [184.3])
        self.assertEqual(cta._money_bn("$1,200 million"), [1.2])
        # A bare number with no unit is not money and must not be read as one.
        self.assertEqual(cta._money_bn("7455 and $3 bn"), [3.0])

    def test_direction_refuses_when_silent(self):
        self.assertEqual(cta._direction("~$31.46 bn global ( ~$15.1 bn US)."), 0)
        self.assertEqual(cta._direction("global net selling of $5bn"), -1)
        self.assertEqual(cta._direction("flips to global net buying of $5bn"), 1)
        # Both present: the earlier verb governs.
        self.assertEqual(cta._direction("net selling now, net buying later"), -1)
        self.assertEqual(cta._direction("net buying now, net selling later"), 1)


class DistanceToTrigger(unittest.TestCase):
    """The more actionable of the two views: how close the selling is, not how
    much of it there could be."""

    T = {"short": 7455.0, "medium": 7204.0, "long": 6765.0}

    def test_signed_distance_and_next_level(self):
        out = cta.distance_to_triggers(7739.0, self.T)
        self.assertEqual(out["breached"], [])
        self.assertEqual(out["next_trigger"], "short")
        self.assertAlmostEqual(out["next_trigger_distance_pct"], 3.81, places=2)
        self.assertAlmostEqual(
            [r for r in out["levels"] if r["key"] == "long"][0]["distance_pct"],
            14.40, places=2)

    def test_next_trigger_is_the_highest_still_below(self):
        out = cta.distance_to_triggers(7300.0, self.T)
        self.assertEqual(out["breached"], ["short"])
        self.assertEqual(out["next_trigger"], "medium")

    def test_all_breached_is_not_the_same_as_no_data(self):
        out = cta.distance_to_triggers(6000.0, self.T)
        self.assertEqual(out["breached"], ["short", "medium", "long"])
        self.assertNotIn("next_trigger", out)
        self.assertEqual(len(out["levels"]), 3)

    def test_no_price_yields_no_levels_rather_than_zeroes(self):
        for bad in (None, 0, -5, float("nan"), "abc", {}, []):
            with self.subTest(value=bad):
                out = cta.distance_to_triggers(bad, self.T)
                self.assertEqual(out["levels"], [])

    def test_a_numeric_string_price_is_accepted(self):
        """``_number`` coerces throughout this module — SSM JSON may carry a
        number as a string — so this must not be special-cased here."""
        self.assertEqual(len(cta.distance_to_triggers("7739", self.T)["levels"]), 3)

    def test_missing_triggers_are_skipped_not_zeroed(self):
        out = cta.distance_to_triggers(7739.0, {"short": 7455.0, "medium": None})
        self.assertEqual([r["key"] for r in out["levels"]], ["short"])

    def test_defaults_to_the_current_snapshot(self):
        out = cta.distance_to_triggers(7739.0)
        self.assertEqual(len(out["levels"]), 3)


class Tracker(unittest.TestCase):
    """The durable daily series. No AWS, no network.

    Distance-to-trigger is only meaningful as a series, and it cannot be
    backfilled: each row depends on which report was in force that day, and
    nothing publishes the history of Goldman's triggers. So losing rows is
    permanent, which is why they go somewhere that outlives the instance.
    """

    def setUp(self):
        import tempfile
        from unittest import mock
        self.tmp = tempfile.mkdtemp()
        self.path = pathlib.Path(self.tmp) / "cta_history.json"
        self._p = mock.patch.object(cta, "_HIST_PATH", str(self.path))
        self._p.start()
        self.addCleanup(self._p.stop)
        # No DynamoDB in tests: the disk tier must work entirely on its own.
        self._t = mock.patch.object(cta, "_get_hist_table", return_value=None)
        self._t.start()
        self.addCleanup(self._t.stop)

    def test_records_a_row_with_distances(self):
        row = cta.record_observation(7711.76, today=TODAY)
        self.assertEqual(row["date"], "2026-08-28")
        self.assertEqual(row["spx"], 7711.76)
        self.assertEqual(row["t_short"], 7455.0)
        self.assertAlmostEqual(row["d_short"], 3.44, places=2)
        self.assertEqual(row["report_date"], "2026-07-28")

    def test_it_survives_a_reread(self):
        cta.record_observation(7711.76, today=TODAY)
        rows = cta.history()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["d_short"], 3.44, places=2)

    def test_same_day_updates_rather_than_duplicating(self):
        """The poller runs hourly, so a day must converge on one row."""
        cta.record_observation(7711.76, today=TODAY)
        cta.record_observation(7600.00, today=TODAY)
        rows = cta.history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spx"], 7600.0)

    def test_days_accumulate_in_order(self):
        for offset, spx in ((2, 7600.0), (0, 7711.0), (1, 7650.0)):
            cta.record_observation(
                spx, today=date.fromordinal(TODAY.toordinal() - offset))
        rows = cta.history()
        self.assertEqual([r["date"] for r in rows],
                         ["2026-08-26", "2026-08-27", "2026-08-28"])

    def test_limit_keeps_the_most_recent(self):
        for offset in range(5):
            cta.record_observation(
                7700.0, today=date.fromordinal(TODAY.toordinal() - offset))
        self.assertEqual([r["date"] for r in cta.history(limit=2)],
                         ["2026-08-27", "2026-08-28"])

    def test_no_price_records_nothing_rather_than_a_zero_row(self):
        self.assertIsNone(cta.record_observation(None, today=TODAY))
        self.assertIsNone(cta.record_observation(0, today=TODAY))
        self.assertEqual(cta.history(), [])

    def test_a_corrupt_file_does_not_stop_recording(self):
        self.path.write_text("{not json", encoding="utf-8")
        row = cta.record_observation(7711.76, today=TODAY)
        self.assertIsNotNone(row)
        self.assertEqual(len(cta.history()), 1)

    def test_rows_are_json_safe(self):
        cta.record_observation(7711.76, today=TODAY)
        json.dumps(cta.history(), allow_nan=False)


class TrackerDurability(unittest.TestCase):
    """The DynamoDB tier, faked. This is the instance-replacement path."""

    class FakeTable:
        def __init__(self):
            self.items = {}

        def put_item(self, Item):                      # noqa: N803 - boto3 kwarg
            self.items[Item["date"]] = dict(Item)

        def get_item(self, Key):                       # noqa: N803 - boto3 kwarg
            item = self.items.get(Key["date"])
            return {"Item": item} if item else {}

        def scan(self, **kwargs):
            return {"Items": list(self.items.values())}

    def setUp(self):
        import tempfile
        from unittest import mock
        self.table = self.FakeTable()
        self.tmp = tempfile.mkdtemp()
        self.path = pathlib.Path(self.tmp) / "cta_history.json"
        self.fetch = pathlib.Path(self.tmp) / "cta_fetched.json"
        for target, value in (("_HIST_PATH", str(self.path)),
                              ("_FETCH_CACHE", str(self.fetch))):
            p = mock.patch.object(cta, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cta, "_get_hist_table", return_value=self.table)
        p.start()
        self.addCleanup(p.stop)

    def test_values_go_as_strings_because_dynamodb_rejects_float(self):
        cta.record_observation(7711.76, today=TODAY)
        item = self.table.items["2026-08-28"]
        self.assertIsInstance(item["spx"], str)
        self.assertIsInstance(item["d_short"], str)
        # And they come back as numbers, not strings.
        self.assertEqual(cta.history()[0]["spx"], 7711.76)

    def test_the_series_survives_losing_the_disk(self):
        """A replaced instance keeps the history. This is the whole point.

        The valuation chart once reset to a single point exactly this way, so the
        test deletes the file rather than trusting that it would work.
        """
        cta.record_observation(7711.76, today=TODAY)
        cta.record_observation(7650.0,
                               today=date.fromordinal(TODAY.toordinal() - 1))
        self.path.unlink()
        rows = cta.history()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["date"] for r in rows], ["2026-08-27", "2026-08-28"])

    def test_a_fetched_report_survives_losing_the_disk(self):
        snapshot = {"latest": {"report_date": "2026-08-20",
                               "spx_triggers": {"short": 7500.0, "medium": 7300.0,
                                                "long": 6900.0}}}
        cta._write_fetched(snapshot)
        self.assertTrue(self.fetch.exists())
        self.fetch.unlink()                       # instance replaced
        got = cta._read_fetched()
        self.assertIsNotNone(got, "report must come back from DynamoDB")
        self.assertEqual(got["latest"]["report_date"], "2026-08-20")
        # And it is still preferred over the older built-in snapshot.
        self.assertEqual(cta.get_cta_positioning()["source_mode"], "fetched")

    def test_the_report_sentinel_is_not_read_as_an_observation(self):
        """One table holds both kinds; a scan must not turn the report row into a
        data point with no date."""
        cta._write_fetched({"latest": {"report_date": "2026-08-20"}})
        cta.record_observation(7711.76, today=TODAY)
        self.assertIn(cta._REPORT_KEY, self.table.items)
        self.assertEqual([r["date"] for r in cta.history()], ["2026-08-28"])

    def test_disk_and_ddb_are_unioned_not_preferred(self):
        """Disk can hold a row written while DynamoDB was unreachable."""
        from unittest import mock
        cta.record_observation(7711.76, today=TODAY)
        with mock.patch.object(cta, "_get_hist_table", return_value=None):
            cta.record_observation(7650.0,
                                   today=date.fromordinal(TODAY.toordinal() + 1))
        self.assertNotIn("2026-08-29", self.table.items)   # never reached DDB
        self.assertEqual([r["date"] for r in cta.history()],
                         ["2026-08-28", "2026-08-29"])

    def test_a_broken_ddb_does_not_break_recording(self):
        from unittest import mock
        boom = mock.MagicMock()
        boom.put_item.side_effect = RuntimeError("throttled")
        boom.scan.side_effect = RuntimeError("throttled")
        boom.get_item.side_effect = RuntimeError("throttled")
        with mock.patch.object(cta, "_get_hist_table", return_value=boom):
            row = cta.record_observation(7711.76, today=TODAY)
            self.assertIsNotNone(row)
            self.assertEqual(len(cta.history()), 1)      # disk still answers

    def test_paginated_scan_reads_every_page(self):
        """Without following LastEvaluatedKey the series silently truncates at
        1 MB, which would look like history simply stopping."""
        from unittest import mock
        pages = [
            {"Items": [{"date": "2026-08-01", "spx": "7000"}],
             "LastEvaluatedKey": {"date": "2026-08-01"}},
            {"Items": [{"date": "2026-08-02", "spx": "7100"}]},
        ]
        table = mock.MagicMock()
        table.scan.side_effect = pages
        with mock.patch.object(cta, "_get_hist_table", return_value=table):
            rows = cta._hist_load_ddb()
        self.assertEqual([r["date"] for r in rows], ["2026-08-01", "2026-08-02"])


class MultiSource(unittest.TestCase):
    """Discovery across every source, which is the fix for a dry single feed.

    This was one feed whose window holds ~50 items over ~5.5 hours, against a
    report published weekly. When that site stopped carrying CTA write-ups the
    card just aged — 47 days before anyone looked — because one dry source and
    one quiet week are the same observation. Seven publishers cannot all go
    quiet at once.
    """

    FEED = ("<rss><item><title>Goldman sees CTAs poised to buy $34b next week</title>"
            "<link>https://example.invalid/a</link>"
            "<pubDate>Mon, 08 Sep 2026 07:00:00 GMT</pubDate></item>"
            "<item><title>Unrelated story</title>"
            "<link>https://example.invalid/b</link></item></rss>")
    EMPTY = ("<rss><item><title>Nothing here</title>"
             "<link>https://x.invalid/1</link></item></rss>")

    def setUp(self):
        import tempfile
        from unittest import mock
        self.mock = mock
        self._status = cta._STATUS_CACHE
        self._tmp = tempfile.mkdtemp()
        cta._STATUS_CACHE = str(pathlib.Path(self._tmp) / "status.json")

    def tearDown(self):
        cta._STATUS_CACHE = self._status

    def test_the_legacy_name_still_points_at_a_real_source(self):
        self.assertEqual(cta.REPORT_RSS_URL, cta.REPORT_SOURCES[0])
        self.assertGreater(len(cta.REPORT_SOURCES), 1)

    def test_every_source_is_polled_not_just_the_first(self):
        """A CTA report in the fourth feed is worth exactly as much as one in
        the first, and the old code would never have seen it."""
        calls = []

        def get(url, attempts=3):
            calls.append(url)
            return self.FEED if url == cta.REPORT_SOURCES[3] else self.EMPTY

        with self.mock.patch.object(cta, "_http_get", get):
            items, ok, _seen = cta._collect_candidates()
        self.assertEqual(len(calls), len(cta.REPORT_SOURCES))
        self.assertEqual(ok, len(cta.REPORT_SOURCES))
        self.assertEqual([u for _t, u, _p in items], ["https://example.invalid/a"])

    def test_one_dead_source_does_not_cost_the_others(self):
        """The entire reason for having more than one."""
        def get(url, attempts=3):
            if url == cta.REPORT_SOURCES[0]:
                raise OSError("connection refused")
            return self.FEED if url == cta.REPORT_SOURCES[2] else self.EMPTY

        with self.mock.patch.object(cta, "_http_get", get):
            items, ok, _seen = cta._collect_candidates()
        self.assertEqual(ok, len(cta.REPORT_SOURCES) - 1)
        self.assertEqual(len(items), 1)

    def test_a_syndicated_story_is_one_candidate_not_seven(self):
        """The same wire story appears in several feeds; fetching it once per
        feed would spend seven requests to reach the same conclusion."""
        with self.mock.patch.object(cta, "_http_get",
                                    lambda u, attempts=3: self.FEED):
            items, _ok, _seen = cta._collect_candidates()
        self.assertEqual(len(items), 1)

    def test_google_news_redirects_are_skipped_before_they_are_fetched(self):
        """Their targets resolve only under JavaScript — fetching one returns a
        582 KB shell with 11 bytes of text, which would burn a request and log a
        misleading "no trigger levels found"."""
        feed = ('<rss><item><title>Goldman CTAs to net sell</title>'
                '<link>https://news.google.com/rss/articles/CBMiABCD?oc=5</link>'
                '</item></rss>')
        with self.mock.patch.object(cta, "_http_get", lambda u, attempts=3: feed):
            items, _ok, _seen = cta._collect_candidates()
        self.assertEqual(items, [])

    def test_all_sources_down_is_distinct_from_none_publishing(self):
        """Seven publishers going quiet together is a network fault at this end,
        and calls for a different response than a genuinely quiet week."""
        def dead(url, attempts=3):
            raise OSError("refused")
        with self.mock.patch.object(cta, "_http_get", dead):
            self.assertIsNone(cta.fetch_latest_report(spx_ref=7000.0))
        self.assertIn("unreachable", cta.last_fetch_diagnosis())

        with self.mock.patch.object(cta, "_http_get",
                                    lambda u, attempts=3: self.EMPTY):
            self.assertIsNone(cta.fetch_latest_report(spx_ref=7000.0))
        reason = cta.last_fetch_diagnosis()
        self.assertIn("no CTA article", reason)
        self.assertNotIn("unreachable", reason)

    def test_the_diagnosis_survives_a_fork(self):
        """Under --preload the poller runs in the master and the API in a forked
        worker, so a module global never reaches the reader. It shipped that way
        once: /api/cta-positioning said "not yet run" while the master's log
        carried the real reason."""
        def dead(url, attempts=3):
            raise OSError("refused")
        with self.mock.patch.object(cta, "_http_get", dead):
            cta.fetch_latest_report(spx_ref=7000.0)
        cta._LAST_FETCH_DIAGNOSIS = "not yet run"     # a fresh worker
        self.assertNotEqual(cta.last_fetch_diagnosis(), "not yet run")


class FetchDiagnosis(unittest.TestCase):
    """Every empty fetch pass must say *why* it was empty.

    An empty pass is the normal outcome here — Goldman publishes weekly and the
    poller runs hourly, so ~167 of every 168 passes correctly find nothing. That
    made the one failure that matters invisible: "the feed is healthy and has no
    CTA article in it" and "our parser broke" both logged at DEBUG, and the card
    aged 47 days while the log repeated "no new report picked up" — a sentence
    equally true of all three situations and useful in none of them.
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock

    def test_an_unreachable_feed_is_named_as_such(self):
        def boom(url, attempts=3):
            raise OSError("connection refused")
        with self.mock.patch.object(cta, "_http_get", boom):
            self.assertIsNone(cta.fetch_latest_report(spx_ref=7000.0))
        self.assertIn("unreachable", cta.last_fetch_diagnosis())

    def test_a_healthy_feed_with_no_cta_article_is_distinguishable(self):
        """The case actually observed: 50 items, 34 KB, zero CTA articles. This
        must not read the same as a broken fetcher."""
        feed = "<rss>" + "".join(
            f"<item><title>Unrelated market story {i}</title>"
            f"<link>https://example.invalid/{i}</link></item>" for i in range(50)
        ) + "</rss>"
        with self.mock.patch.object(cta, "_http_get", lambda u, attempts=3: feed):
            self.assertIsNone(cta.fetch_latest_report(spx_ref=7000.0))
        reason = cta.last_fetch_diagnosis()
        self.assertIn("no CTA article", reason)
        self.assertIn("50", reason)          # says how big the window was
        self.assertNotIn("unreachable", reason)

    def test_the_placeholder_never_survives_a_pass(self):
        """'not yet run' means the poller has not started. If it can still be
        read after a pass, some branch returns without recording a reason."""
        cta._LAST_FETCH_DIAGNOSIS = "not yet run"
        with self.mock.patch.object(
                cta, "_http_get", lambda u, attempts=3: "<rss></rss>"):
            cta.fetch_latest_report(spx_ref=7000.0)
        self.assertNotEqual(cta.last_fetch_diagnosis(), "not yet run")

    def test_the_diagnosis_is_a_short_phrase_fit_for_a_log_line(self):
        with self.mock.patch.object(
                cta, "_http_get", lambda u, attempts=3: "<rss></rss>"):
            cta.fetch_latest_report(spx_ref=7000.0)
        reason = cta.last_fetch_diagnosis()
        self.assertLess(len(reason), 120)
        self.assertNotIn("\n", reason)


class PositioningRegions(unittest.TestCase):
    """A reading counts if it carries *either* region's figure.

    Requiring the global one dropped the 2026-09-17 note — US length near a
    one-year low, two weeks and a Fed hike after the global figure sat near the
    top of its range — and left the card headlining the older, higher number as
    the latest word on positioning.
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock

    def _payload(self, points):
        import os
        with self.mock.patch.object(cta, "_read_fetched", return_value=None), \
             self.mock.patch.dict(os.environ, {
                 "GOLDMAN_CTA_DATA_JSON": json.dumps({"positioning": points})}):
            return cta.get_cta_positioning()

    def test_a_us_only_reading_is_kept(self):
        points = cta._positioning_points(
            [{"date": "2026-09-17", "us_equity_bn": 37.3}])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["us_equity_bn"], 37.3)
        self.assertNotIn("global_equity_bn", points[0])

    def test_a_reading_with_no_region_is_still_dropped(self):
        """A percentile alone is not a position."""
        self.assertEqual(cta._positioning_points(
            [{"date": "2026-09-17", "percentile": 12.0}]), [])

    def test_the_headline_is_the_newest_global_reading_not_the_newest_point(self):
        """The card's headline is global; the last point may now have no
        global figure to show."""
        out = self._payload([
            {"date": "2026-09-02", "global_equity_bn": 146.5},
            {"date": "2026-09-17", "us_equity_bn": 37.3},
        ])
        self.assertEqual(out["latest_positioning"]["date"], "2026-09-02")
        self.assertEqual(out["latest_us_positioning"]["date"], "2026-09-17")

    def test_no_us_reading_is_none_rather_than_a_global_one(self):
        out = self._payload([{"date": "2026-09-02", "global_equity_bn": 146.5}])
        self.assertIsNone(out["latest_us_positioning"])

    def test_the_built_in_series_carries_both_september_readings(self):
        with self.mock.patch.object(cta, "_read_fetched", return_value=None):
            out = cta.get_cta_positioning()
        by_date = {p["date"]: p for p in out["positioning"]}
        self.assertEqual(by_date["2026-09-02"]["global_equity_bn"], 146.5)
        self.assertEqual(by_date["2026-09-17"]["us_equity_bn"], 37.3)
        # Positioning moved on and the triggers did not: no public source has
        # printed new levels since the 2026-07-28 report, so its date — and the
        # stale badge that follows from it — must not ride along.
        self.assertEqual(out["latest"]["report_date"], "2026-07-28")


if __name__ == "__main__":
    unittest.main(verbosity=2)
