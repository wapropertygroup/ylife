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


if __name__ == "__main__":
    unittest.main()
