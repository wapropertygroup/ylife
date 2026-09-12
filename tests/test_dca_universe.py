"""
Tests for the tracked-ticker registry (``ystocker.dca_universe``).

No app, no network, no AWS — DynamoDB is stubbed out by pointing the module at a
temporary mirror file and leaving ``_get_table`` returning ``None``, which is the
same degraded path local dev takes.

What is worth testing here is not "can it store a string". It is the three ways
this grows into a problem:

* **The cap is a daily Yahoo bill, not a storage limit.** Every tracked ticker is
  six reads a day, for ever. A registry that grows without bound is a
  slow-motion version of the bulk sweep ``valuation.py`` records having got this
  box hard-blocked — it would not break on the day it went wrong, it would just
  get heavier every week.
* **Evicting the wrong thing.** The seed is the population the ranked table
  exists for; a burst of lookups pushing MSFT out would quietly turn the
  overview into a list of whatever somebody typed last week. And evicting by
  insertion order rather than recency would drop the name somebody opens daily
  in favour of one they opened once.
* **Admitting what cannot score.** An ETF publishes no statements, so it builds
  to an ``unavailable`` payload. Those must not enter a table headed "all scored
  names" — they would be permanently blank rows that still cost six reads a day
  to re-confirm.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ystocker import dca_universe as du


class RegistryBase(unittest.TestCase):
    """Every test gets its own mirror file and no DynamoDB."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = du.LOCAL_PATH
        self._old_max = du.MAX_TRACKED
        du.LOCAL_PATH = Path(self._tmp.name) / "dca_universe.json"
        # Force the degraded, disk-only path: no credentials, no calls.
        du._table = None
        du._table_unavail_until = float("inf")

    def tearDown(self):
        du.LOCAL_PATH = self._old_path
        du.MAX_TRACKED = self._old_max
        du._table_unavail_until = 0.0
        self._tmp.cleanup()


class Seed(RegistryBase):

    def test_the_seed_is_the_frameworks_named_set(self):
        from ystocker.dca import TICKER_MODELS

        self.assertEqual(du.seed(), set(TICKER_MODELS) - {"GOOG"})

    def test_goog_is_folded_into_googl(self):
        self.assertNotIn("GOOG", du.seed())
        self.assertIn("GOOGL", du.seed())

    def test_the_seed_is_present_with_an_empty_registry(self):
        self.assertEqual(set(du.all_tickers()), du.seed())

    def test_the_seed_cannot_be_forgotten(self):
        self.assertFalse(du.forget("MSFT"))
        self.assertIn("MSFT", du.all_tickers())


class Registration(RegistryBase):

    def test_remembering_adds_to_the_universe(self):
        self.assertTrue(du.remember("SHOP"))
        self.assertIn("SHOP", du.all_tickers())

    def test_remembering_twice_reports_no_new_entry(self):
        du.remember("SHOP")
        self.assertFalse(du.remember("SHOP"), "the second call is a recency bump")
        self.assertEqual(len([t for t in du.all_tickers() if t == "SHOP"]), 1)

    def test_symbols_are_normalised(self):
        du.remember("  shop ")
        self.assertIn("SHOP", du.all_tickers())

    def test_an_empty_symbol_is_refused(self):
        self.assertFalse(du.remember(""))
        self.assertFalse(du.remember("   "))

    def test_it_survives_a_round_trip_through_the_mirror(self):
        du.remember("SHOP")
        self.assertIn("SHOP", du.tracked())
        # Re-read from disk with nothing in memory.
        self.assertIn("SHOP", du._disk_rows())

    def test_forget_removes_a_tracked_name(self):
        du.remember("SHOP")
        self.assertTrue(du.forget("SHOP"))
        self.assertNotIn("SHOP", du.all_tickers())

    def test_forgetting_an_unknown_name_changes_nothing(self):
        self.assertFalse(du.forget("NEVERSEEN"))

    def test_touch_does_not_register_an_unknown_name(self):
        """Registration is gated on actually scoring; touch must not bypass it."""
        du.touch("GDX")
        self.assertNotIn("GDX", du.all_tickers())

    def test_touch_bumps_an_existing_name(self):
        du.remember("SHOP")
        # Rewrite the mirror with a stale timestamp, since tracked() hands back
        # copies and mutating those changes nothing.
        rows = du.tracked()
        rows["SHOP"]["last_seen_at"] = 1.0
        du._write_disk(rows)
        self.assertEqual(du.tracked()["SHOP"]["last_seen_at"], 1.0)

        du.touch("SHOP")
        self.assertGreater(du.tracked()["SHOP"]["last_seen_at"], 1.0)

    def test_touch_preserves_the_original_added_at(self):
        """Recency moves; provenance does not."""
        du.remember("SHOP")
        added = du.tracked()["SHOP"]["added_at"]
        du.touch("SHOP")
        self.assertEqual(du.tracked()["SHOP"]["added_at"], added)


class Capacity(RegistryBase):

    def test_the_universe_never_exceeds_the_cap(self):
        du.MAX_TRACKED = len(du.seed()) + 3
        for i in range(20):
            du.remember(f"X{i}")
        self.assertLessEqual(len(du.all_tickers()), du.MAX_TRACKED)

    def test_eviction_drops_the_least_recently_opened(self):
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("OLD")
        du.remember("MID")
        du.remember("NEW")
        kept = set(du.all_tickers())
        self.assertNotIn("OLD", kept, "the oldest non-seed entry should go first")
        self.assertIn("NEW", kept)

    def test_reopening_a_name_protects_it_from_eviction(self):
        """Recency, not insertion order. A name opened daily must outlive one
        opened once, or the cap evicts exactly the wrong thing."""
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("KEEPER")
        du.remember("FILLER1")
        du.touch("KEEPER")            # opened again, most recent now
        du.remember("FILLER2")        # forces an eviction
        self.assertIn("KEEPER", du.all_tickers())

    def test_the_seed_is_never_evicted(self):
        du.MAX_TRACKED = 1            # absurdly tight
        for i in range(10):
            du.remember(f"X{i}")
        self.assertTrue(du.seed().issubset(set(du.all_tickers())))

    def test_eviction_prunes_the_store_not_just_the_view(self):
        """Hiding the overflow from the page would leave the table growing."""
        du.MAX_TRACKED = len(du.seed()) + 1
        du.remember("FIRST")
        du.remember("SECOND")
        self.assertNotIn("FIRST", du.tracked())

    def test_stats_report_capacity_honestly(self):
        du.MAX_TRACKED = len(du.seed()) + 5
        du.remember("SHOP")
        s = du.stats()
        self.assertEqual(s["max"], du.MAX_TRACKED)
        self.assertEqual(s["seed"], len(du.seed()))
        self.assertEqual(s["effective"], len(du.all_tickers()))
        self.assertEqual(s["room"], 4)


class Degradation(RegistryBase):
    """DynamoDB absent is normal here, not an error."""

    def test_everything_works_with_no_table(self):
        self.assertIsNone(du._get_table())
        du.remember("SHOP")
        self.assertIn("SHOP", du.all_tickers())

    def test_a_corrupt_mirror_falls_back_to_the_seed(self):
        du.LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        du.LOCAL_PATH.write_text("{ not json")
        self.assertEqual(set(du.all_tickers()), du.seed())

    def test_a_mirror_of_the_wrong_shape_is_ignored(self):
        du.LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        du.LOCAL_PATH.write_text(json.dumps({"tickers": ["not", "a", "dict"]}))
        self.assertEqual(set(du.all_tickers()), du.seed())

    def test_the_output_is_sorted_and_deduplicated(self):
        """Membership order must not churn between calls: the page diffs it."""
        du.remember("SHOP")
        du.remember("SHOP")
        out = du.all_tickers()
        self.assertEqual(out, sorted(set(out)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
