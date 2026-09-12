"""
Tests for the daily broadcast's freshness gate (``routes._summary_is_post_close``).

Named ``check_`` so ``unittest discover`` skips it: importing ``ystocker.routes``
builds enough of the app to need matplotlib stubbed, which the sibling
``check_dca_endpoints`` already does and this reuses.

Why this exists
---------------
The daily email was a trading session behind, every day, and nothing about it
looked wrong: the subject carried today's date, the numbers were real, and the
commentary was coherent. It was simply about yesterday.

The mechanism is worth stating because it is entirely made of correct-looking
parts. The pre-generator fires at 00:05 ET so that morning visitors to ``/daily``
get an instant summary instead of waiting on Gemini. At that hour the in-memory
market caches still hold the **previous** session's close, which is right --
there is no newer data. It stores the result under ``date.today()``, which is
also right by its own lights. Then the 16:45 ET broadcast asks for "today's
summary", finds that row, and mails it.

Every step is defensible and the composition is wrong. Observed 2026-09-11: all
four summaries stamped ``05:05 UTC`` (01:05 ET), hours before the session the
email claimed to describe.

So the rule is not "is this recent" but "was this written after the session it
claims to describe had closed" -- a summary is *about* a session, and age is the
wrong question to ask about it.

Run:  venv/bin/python -m tests.check_daily_broadcast
"""
from __future__ import annotations

import unittest

# Stubs matplotlib and sets the env this import needs. Must precede the routes
# import, which is why it is not grouped with the stdlib imports above.
import tests.check_dca_endpoints  # noqa: F401

from ystocker.routes import (  # noqa: E402
    _MIN_SUMMARY_UTC_HOUR,
    _summary_is_post_close,
)


class FreshnessGate(unittest.TestCase):

    def test_the_overnight_pregen_that_caused_this_is_refused(self):
        """The exact stamp observed on the day this was reported."""
        self.assertFalse(_summary_is_post_close("2026-09-11 05:05 UTC", "2026-09-11"))

    def test_a_summary_written_between_close_and_broadcast_is_accepted(self):
        """The one legitimate reuse window: something generated it after the
        close but before 16:45 ET, so a second Gemini call would be waste."""
        self.assertTrue(_summary_is_post_close("2026-09-11 21:44 UTC", "2026-09-11"))

    def test_the_boundary_is_inclusive(self):
        self.assertTrue(_summary_is_post_close("2026-09-11 21:00 UTC", "2026-09-11"))
        self.assertFalse(_summary_is_post_close("2026-09-11 20:59 UTC", "2026-09-11"))

    def test_the_cutoff_is_the_later_close_not_the_current_one(self):
        """21:00 UTC is 16:00 EST. In EDT the close is an hour earlier, so a
        20:30 UTC summary is genuinely post-close and still refused.

        That is deliberate: the strict direction costs one Gemini call, the
        permissive direction mails a pre-close summary for half the year. The
        check never has to know which side of daylight saving it is on.
        """
        self.assertEqual(_MIN_SUMMARY_UTC_HOUR, 21)
        self.assertFalse(_summary_is_post_close("2026-06-11 20:30 UTC", "2026-06-11"))

    def test_another_days_summary_is_refused_however_late_it_was_written(self):
        """Yesterday's post-close copy is post-*a*-close, just not this one."""
        self.assertFalse(_summary_is_post_close("2026-09-10 22:00 UTC", "2026-09-11"))
        self.assertFalse(_summary_is_post_close("2026-09-12 22:00 UTC", "2026-09-11"))

    def test_an_unreadable_stamp_never_reads_as_current(self):
        """"We cannot tell when this was written" must not pass as "it is
        current" -- the same reasoning ``dcf._age_days`` applies to a stored
        valuation date."""
        for bad in (None, "", "   ", "garbage", "2026-09-11", "2026-09-11 21:00",
                    "11/09/2026 21:00 UTC", 20260911, 1.5, True, [], {}):
            self.assertFalse(_summary_is_post_close(bad, "2026-09-11"), msg=repr(bad))

    def test_every_hour_of_the_reporting_day_is_decided_by_the_cutoff(self):
        for hour in range(24):
            stamp = f"2026-09-11 {hour:02d}:30 UTC"
            self.assertEqual(_summary_is_post_close(stamp, "2026-09-11"),
                             hour >= _MIN_SUMMARY_UTC_HOUR, msg=stamp)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
