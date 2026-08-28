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


if __name__ == "__main__":
    unittest.main(verbosity=2)
