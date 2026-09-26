"""A-share graph selection and progress mapping tests."""

import importlib.util
import pathlib
import unittest


PATH = pathlib.Path(__file__).parents[1] / "ystocker" / "agents.py"
SPEC = importlib.util.spec_from_file_location("agents_under_test", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class AgentSelectionTests(unittest.TestCase):
    def test_a_share_gets_every_astock_analyst(self):
        # Derived rather than a hardcoded count, so a future roster change
        # (like fundamentals -> quality+valuation) cannot make this test stale.
        for ticker in ("600519", "SH600519", "600519.SS", "000001.SZ", "BJ920002"):
            with self.subTest(ticker=ticker):
                self.assertEqual(
                    len(MODULE.analysts_for_ticker(ticker)), len(MODULE.ASTOCK_ANALYSTS)
                )

    def test_non_a_share_keeps_base_analysts(self):
        self.assertEqual(MODULE.analysts_for_ticker("AAPL"), MODULE.BASE_ANALYSTS)

    def test_embedded_runner_streams_specialist_reports(self):
        for field in ("policy_report", "hot_money_report", "lockup_report"):
            self.assertIn(field, MODULE._RUNNER)


class SubmitDateTests(unittest.TestCase):
    """A future date must be refused by submit(), not by the child.

    TradingAgents' graph raises on a future trade date, but only after the child
    has launched — after the quota was taken, and not among the failures
    _refund_preflight gives back — so a reader who picked tomorrow paid for a run
    that could never start. submit() errors are refunded by the route. Found
    2026-09-26 while writing /docs/quick-start, which had to warn about it.
    Every case returns during validation, so nothing is queued or launched —
    and the write and the thread are patched to fail loudly, so a regression
    that let a case through fails here instead of queueing a real run (and
    writing a job record) from inside the test suite.
    """

    def setUp(self):
        from unittest import mock

        def _refuse(*_a, **_k):
            raise AssertionError("submit() got past validation")

        for target in ("_write",):
            patcher = mock.patch.object(MODULE, target, _refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(MODULE.threading, "Thread", _refuse)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_future_date_is_refused_before_anything_is_queued(self):
        from datetime import date, timedelta
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        job_id, err = MODULE.submit("NVDA", tomorrow, "reader@example.com")
        self.assertIsNone(job_id)
        self.assertIn("future", err.lower())

    def test_the_other_date_refusals_still_hold(self):
        self.assertEqual(MODULE.submit("NVDA", "2026/09/25", "r@example.com")[1],
                         "Invalid date (expected YYYY-MM-DD)")
        self.assertEqual(MODULE.submit("NVDA", "2026-02-30", "r@example.com")[1],
                         "Invalid date")


if __name__ == "__main__":
    unittest.main()
