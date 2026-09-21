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


class Pinning(RegistryBase):
    """Keeping a name against recency eviction.

    The complaint this answers: the live registry reached its cap (60 of 60, 23
    of them seed), so the reader's own 37 names sat in a pure LRU queue and
    every new lookup silently deleted the least-recently-viewed one. Registration
    is automatic, so there was no way to say "this one is not a browse".

    What pinning must *not* do is raise the ceiling. Every row is six Yahoo reads
    a day whatever it is marked, and the cap is that bill.
    """

    def test_a_pinned_name_outlives_an_unpinned_one(self):
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("KEEPER")
        du.pin("KEEPER")
        du.remember("FILLER1")
        du.remember("FILLER2")        # forces an eviction
        kept = set(du.all_tickers())
        self.assertIn("KEEPER", kept)
        self.assertNotIn("FILLER1", kept)

    def test_pinning_beats_recency_not_just_ties_with_it(self):
        """The whole point: the pinned name is the *oldest* here, so plain LRU
        would drop it first."""
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("OLD_PINNED")
        du.pin("OLD_PINNED")
        du.remember("NEWER1")
        du.touch("NEWER1")
        du.remember("NEWER2")
        self.assertIn("OLD_PINNED", du.all_tickers())

    def test_pinning_does_not_raise_the_cap(self):
        du.MAX_TRACKED = len(du.seed()) + 2
        for i in range(6):
            du.remember(f"X{i}")
            du.pin(f"X{i}")
        self.assertLessEqual(len(du.all_tickers()), du.MAX_TRACKED)

    def test_the_cap_refuses_a_pin_rather_than_evicting_another_pin(self):
        """Losing one saved name to gain another is the behaviour being
        complained about, not a fix for it."""
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("A"); du.pin("A")
        du.remember("B"); du.pin("B")
        du.remember("C")
        result = du.pin("C")
        self.assertFalse(result["pinned"])
        self.assertEqual(result["reason"], "no_room")
        self.assertEqual(result["max_pinned"], 2)
        self.assertEqual(du.pinned(), {"A", "B"})

    def test_a_view_does_not_unpin(self):
        """`remember` preserves an existing source and is called on every view,
        so looking at a kept name must not quietly release it."""
        du.remember("KEEPER")
        du.pin("KEEPER")
        du.touch("KEEPER")
        du.remember("KEEPER")
        self.assertIn("KEEPER", du.pinned())

    def test_unpinning_keeps_it_tracked(self):
        """Release is not delete: `forget` is the one that removes."""
        du.remember("SHOP")
        du.pin("SHOP")
        du.unpin("SHOP")
        self.assertNotIn("SHOP", du.pinned())
        self.assertIn("SHOP", du.all_tickers())

    def test_pinning_an_untracked_name_is_refused_not_created(self):
        """Registration is gated on actually scoring — that is what keeps ETFs
        out of a table headed "all scored names"."""
        result = du.pin("NEVEROPENED")
        self.assertFalse(result["pinned"])
        self.assertEqual(result["reason"], "not_tracked")
        self.assertNotIn("NEVEROPENED", du.tracked())

    def test_pinning_a_seed_name_is_refused_as_redundant(self):
        """It is already unevictable; consuming a slot for it would shrink the
        budget for names that need one."""
        result = du.pin("MSFT")
        self.assertFalse(result["pinned"])
        self.assertEqual(result["reason"], "seed")
        self.assertNotIn("MSFT", du.pinned())

    def test_remember_reports_survival_not_merely_novelty(self):
        """With every slot pinned the new row is evicted on the way through, and
        a True here would claim it was registered."""
        du.MAX_TRACKED = len(du.seed()) + 1
        du.remember("A"); du.pin("A")
        self.assertFalse(du.remember("B"))
        self.assertNotIn("B", du.tracked())

    def test_stats_report_the_pin_budget(self):
        du.MAX_TRACKED = len(du.seed()) + 3
        du.remember("SHOP"); du.pin("SHOP")
        s = du.stats()
        self.assertEqual(s["pinned"], 1)
        self.assertEqual(s["max_pinned"], 3)


class Holdings(RegistryBase):
    """The reader's own positions, protected above pins.

    A company somebody has money in is the one they most need scored, so it
    outranks a name they asked to keep, which outranks one they once opened.
    What it does *not* do is raise the ceiling: the cap is a daily Yahoo bill
    and a holding costs the same six reads as anything else.
    """

    def test_a_holding_outranks_a_pin_and_a_browse(self):
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("OWNED")
        du.remember("KEPT"); du.pin("KEPT")
        du.sync_held(["OWNED"])
        du.remember("BROWSED")          # forces an eviction
        kept = set(du.all_tickers())
        self.assertIn("OWNED", kept)
        self.assertIn("KEPT", kept)
        self.assertNotIn("BROWSED", kept)

    def test_a_holding_survives_when_a_pin_must_go(self):
        du.MAX_TRACKED = len(du.seed()) + 1
        du.remember("OWNED"); du.sync_held(["OWNED"])
        du.remember("KEPT"); du.pin("KEPT")
        self.assertIn("OWNED", du.all_tickers())

    def test_selling_demotes_rather_than_deletes(self):
        """A sold position stops being protected; it does not lose its
        reconstruction, which cost six reads to build."""
        du.remember("SOLD"); du.sync_held(["SOLD"])
        self.assertEqual(du.held(), {"SOLD"})
        du.sync_held([])
        self.assertEqual(du.held(), set())
        self.assertIn("SOLD", du.all_tickers())

    def test_a_holding_that_never_built_is_reported_not_invented(self):
        """Registration stays gated on a rebuild that scored — that is what
        keeps ETFs, which most portfolios are mostly made of, out of a table
        headed "all scored names"."""
        out = du.sync_held(["NEVERBUILT"])
        self.assertEqual(out["not_tracked"], ["NEVERBUILT"])
        self.assertNotIn("NEVERBUILT", du.tracked())
        self.assertEqual(du.held(), set())

    def test_a_portfolio_larger_than_the_budget_reports_no_slot(self):
        """It does not evict to make room. Quietly doubling the daily data bill
        because somebody imported a broker CSV is the failure this module
        exists to prevent — and with every non-seed slot held, a further holding
        will never arrive however long the warm runs, which is a different
        message from "still building"."""
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("A"); du.remember("B")
        du.sync_held(["A", "B"])
        out = du.sync_held(["A", "B", "C", "D"])
        self.assertEqual(out["max_held"], 2)
        self.assertEqual(sorted(out["held"]), ["A", "B"])
        self.assertEqual(out["no_room"], ["C", "D"])
        self.assertEqual(out["not_tracked"], [])
        self.assertLessEqual(len(du.all_tickers()), du.MAX_TRACKED)

    def test_an_absent_holding_with_room_left_is_only_awaiting_a_build(self):
        """The distinction that decides what the reader should do: wait, or
        release a slot."""
        du.MAX_TRACKED = len(du.seed()) + 5
        du.remember("A"); du.sync_held(["A"])
        out = du.sync_held(["A", "NEWONE"])
        self.assertEqual(out["not_tracked"], ["NEWONE"])
        self.assertEqual(out["no_room"], [])

    def test_the_caller_order_decides_who_gets_protection(self):
        """The route passes positions largest-first, so when the cap bites the
        names with the most money behind them are the ones that keep it."""
        du.MAX_TRACKED = len(du.seed()) + 2
        du.remember("BIG"); du.remember("MID")
        out = du.sync_held(["BIG", "MID", "SMALL"])
        self.assertEqual(sorted(out["held"]), ["BIG", "MID"])
        self.assertEqual(out["no_room"], ["SMALL"])

    def test_a_seed_name_in_the_portfolio_is_not_double_counted(self):
        """It is already unevictable; consuming a held slot for it would shrink
        the budget available to holdings that actually need one."""
        out = du.sync_held(["MSFT"])
        self.assertEqual(out["held"], [])
        self.assertNotIn("MSFT", du.held())

    def test_syncing_twice_is_idempotent(self):
        du.remember("OWNED")
        first = du.sync_held(["OWNED"])
        second = du.sync_held(["OWNED"])
        self.assertEqual(first["held"], second["held"])
        self.assertEqual(second["demoted"], [])

    def test_stats_report_the_held_count(self):
        du.remember("OWNED"); du.sync_held(["OWNED"])
        self.assertEqual(du.stats()["held"], 1)

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


class PeerGroupCoverageTests(unittest.TestCase):
    """Every seed name must sit in some ``PEER_GROUPS`` entry.

    These are two hand-maintained lists that grow for different reasons —
    ``PEER_GROUPS`` is curated site-wide (it also drives the analyst sweep,
    relative strength and the agents' peer context), while the DCA seed is the
    framework's own named companies — so nothing stops them drifting apart, and
    when they do the failure is silent and doubly expensive.

    Measured on 2026-09-18, before this was fixed: 30 of the 60 tracked tickers
    were in no peer group, and the correlation with the damage was exact.

      * All 25 rows that dropped the ``peer`` factor were tickers with no group.
        ``peer`` carries 15-20% in nine of the ten templates, so those names
        scored on a renormalised subset of their own model while rendering in the
        same column as names that did not.
      * All 30 showed ``M_盈利`` as a flat 1.00x, because ``analyst.py`` sweeps
        exactly ``PEER_GROUPS`` — so a name outside it can never have revision
        data, and the overlay is inert rather than neutral.

    One missing entry therefore costs a factor *and* an overlay.

    Note what this test does and does not cover. The seed is 23 names; the other
    37 tracked tickers arrived through the registry because a reader opened them,
    and 25 of the 30 gaps were exactly those. So this guards the curated core and
    nothing else — a registry ticker still arrives with no group and no way for a
    test to anticipate it. That case is handled by disclosure instead: the payload
    carries ``no_peer_group`` and the page says so, rather than renormalising in
    silence.
    """

    def test_every_seed_ticker_has_a_peer_group(self):
        from ystocker import PEER_GROUPS

        members = {t for group in PEER_GROUPS.values() for t in group}
        orphans = sorted(du.seed() - members)
        self.assertEqual(
            orphans, [],
            "seed tickers with no peer group (they will score with `peer` dropped "
            f"and no earnings overlay): {orphans}")

    def test_goog_is_excluded_rather_than_orphaned(self):
        """The one deliberate omission, so the assertion above cannot be made to
        pass by quietly dropping a name from the seed instead of grouping it."""
        from ystocker.dca import TICKER_MODELS

        self.assertIn("GOOG", TICKER_MODELS)
        self.assertNotIn("GOOG", du.seed())
