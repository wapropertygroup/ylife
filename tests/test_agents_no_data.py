"""A run that stops for want of any price history is refunded and coded.

Two runs on 2026-10-05 -- "NIFTY50" and "TCS", one reader a minute apart --
ended in 7 and 9 seconds with ``NoMarketDataError: No market data for 'TCS': no
price rows``: Yahoo prices neither symbol (an index; and Tata Consultancy is
TCS.NS). Each spent a free run on nothing. ``agents.is_no_price_data`` is the
rule ``_run`` refunds on and codes ``no_market_data`` for the page's hint.

No app, no network, no subprocess.
"""
from __future__ import annotations

import unittest

from ystocker import agents

TCS = "NoMarketDataError: No market data for 'TCS': no price rows"


def _job(**over):
    job = {"status": "error", "error": TCS, "elapsed_sec": 9.0}
    job.update(over)
    return job


class NoPriceDataTests(unittest.TestCase):
    def test_the_two_runs_of_2026_10_05(self):
        self.assertTrue(agents.is_no_price_data(_job()))
        self.assertTrue(agents.is_no_price_data(_job(
            error="NoMarketDataError: No market data for 'NIFTY50': no price rows", elapsed_sec=7.0)))

    def test_only_a_quick_failure_is_refunded(self):
        """Past the bound a run may have spent real calls before failing."""
        bound = agents.NO_DATA_REFUND_MAX_SECONDS
        self.assertTrue(agents.is_no_price_data(_job(elapsed_sec=bound)))
        self.assertFalse(agents.is_no_price_data(_job(elapsed_sec=bound + 1)))

    def test_other_missing_data_is_not_this(self):
        # A fundamentals gap is a stated hole in a report, not a refusal.
        for error in ("NoMarketDataError: No market data for 'TSM': not a US SEC filer",
                      "VendorError: Yahoo is unreachable",
                      "RuntimeError: something else"):
            with self.subTest(error=error):
                self.assertFalse(agents.is_no_price_data(_job(error=error)))

    def test_only_a_failed_run(self):
        self.assertFalse(agents.is_no_price_data(_job(status="done")))
        self.assertFalse(agents.is_no_price_data(_job(status="running")))
        self.assertFalse(agents.is_no_price_data({}))


if __name__ == "__main__":
    unittest.main()
