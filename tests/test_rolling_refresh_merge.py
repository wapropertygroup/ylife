"""The rolling cache refresher must admit tickers newly added to a peer group.

The refresher picks its batch from ``PEER_GROUPS`` but used to write results back
with ``if ticker in group_data`` — patching only keys the cache already held. A
ticker added to a group was therefore fetched every cycle and discarded every
cycle: the Yahoo call was paid and nothing was stored, and the only path that
could ever admit it was the full 8-hourly rebuild.

That is invisible from outside. The log says "Rolling refresher active" and
reports no errors, the fetch succeeds, and the cache count simply never moves.
Observed on 2026-09-18 after adding 30 names to ``PEER_GROUPS``: the cache sat at
exactly 271 across several cycles while the fetch was running each time.

This models the merge step alone rather than importing ``routes`` (matplotlib,
an app context and live threads), which is also why the test can state the
invariant plainly: *after a batch, every fetched ticker is stored in every group
that lists it.*
"""
from __future__ import annotations

import unittest


def _merge_fixed(cache, peer_groups, raw):
    """The merge as it now is: keyed off group membership."""
    for group, members in peer_groups.items():
        group_data = cache.get(group)
        if group_data is None:
            continue
        for ticker, data in raw.items():
            if ticker in members:
                group_data[ticker] = data
    return cache


def _merge_old(cache, peer_groups, raw):
    """The merge as it was, kept so the test can show the difference is real."""
    for group_data in cache.values():
        for ticker, data in raw.items():
            if ticker in group_data:
                group_data[ticker] = data
    return cache


class RollingMergeTests(unittest.TestCase):
    def setUp(self):
        self.groups = {
            "Utilities": ["NEE", "DUK", "PPL"],          # PPL just added
            "Tech": ["MSFT", "AAPL"],
            "AI / Robotics": ["MSFT", "NVDA"],           # MSFT is in two groups
        }
        # What the cache holds before the batch: PPL absent, as it would be.
        self.cache = {
            "Utilities": {"NEE": {"v": 1}, "DUK": {"v": 1}},
            "Tech": {"MSFT": {"v": 1}, "AAPL": {"v": 1}},
            "AI / Robotics": {"MSFT": {"v": 1}, "NVDA": {"v": 1}},
        }

    def test_a_newly_grouped_ticker_is_stored(self):
        _merge_fixed(self.cache, self.groups, {"PPL": {"v": 2}})
        self.assertEqual(self.cache["Utilities"].get("PPL"), {"v": 2})

    def test_the_old_merge_dropped_it(self):
        """Guards the fix itself. Without this the test above would still pass
        against a refresher that had quietly reverted, because nothing else here
        distinguishes 'stored' from 'was already there'."""
        _merge_old(self.cache, self.groups, {"PPL": {"v": 2}})
        self.assertNotIn("PPL", self.cache["Utilities"],
                         "old merge unexpectedly stored it — the bug this "
                         "test documents may no longer be real")

    def test_an_existing_ticker_is_still_refreshed(self):
        _merge_fixed(self.cache, self.groups, {"NEE": {"v": 9}})
        self.assertEqual(self.cache["Utilities"]["NEE"], {"v": 9})

    def test_multi_group_membership_updates_every_group(self):
        """MSFT is in Tech and AI / Robotics. One fetch must freshen both, or the
        two groups drift and the same ticker shows two different prices on two
        pages."""
        _merge_fixed(self.cache, self.groups, {"MSFT": {"v": 7}})
        self.assertEqual(self.cache["Tech"]["MSFT"], {"v": 7})
        self.assertEqual(self.cache["AI / Robotics"]["MSFT"], {"v": 7})

    def test_a_ticker_in_no_group_is_not_stored(self):
        """The batch comes from PEER_GROUPS, so this should not arise — but the
        merge must not become a way for arbitrary symbols to enter the cache."""
        _merge_fixed(self.cache, self.groups, {"ZZZZ": {"v": 3}})
        self.assertFalse(any("ZZZZ" in gd for gd in self.cache.values()))

    def test_a_group_absent_from_the_cache_is_skipped(self):
        """A group added to PEER_GROUPS since the cache was built has no dict
        yet. Skipping is correct; creating one here would publish a group the
        rest of the load path has not seen."""
        self.groups["Brand New"] = ["PPL"]
        _merge_fixed(self.cache, self.groups, {"PPL": {"v": 2}})
        self.assertNotIn("Brand New", self.cache)
        self.assertEqual(self.cache["Utilities"].get("PPL"), {"v": 2})

    def test_the_shipped_merge_matches_this_model(self):
        """The model above is only worth testing if it is what routes.py does.
        Compares source text rather than importing the module, which needs
        matplotlib and an app."""
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "ystocker" / "routes.py"
        body = src.read_text()
        self.assertIn("for group, members in PEER_GROUPS.items():", body)
        self.assertIn("if ticker in members:", body)
        self.assertNotIn("if ticker in group_data:", body,
                         "routes.py still contains the patch-only merge")


if __name__ == "__main__":
    unittest.main()
