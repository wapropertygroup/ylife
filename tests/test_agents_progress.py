"""The plan the /agents progress bar counts a run's events against.

``agents.run_plan`` names the turns a run takes; the page counts the child's
progress events (one per finished turn) against it. Three ways that goes wrong
without an error anywhere, each pinned here:

* **A role the events never use.** The plan says "social" while the runner
  publishes "sentiment", and the bar sits at 0/6 analysts for the whole run.
  So every role in the plan must be one the runner's own mapping publishes.
* **A roster that differs from the one the run uses.** The page cannot know
  A-share runs seat three more analysts unless the plan says so, and a US run
  counted against nine would stop at 67%.
* **Rounds that are not a number.** The deployment's rounds are env strings,
  and the page's arithmetic needs integers.

No app, no network: agents.py is loaded from its file, as
test_agents_astock_selection does.
"""
import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).parents[1]
PATH = ROOT / "ystocker" / "agents.py"
SPEC = importlib.util.spec_from_file_location("agents_progress_under_test", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

ROLES_SRC = (ROOT / "ystocker" / "agent_roles.py").read_text(encoding="utf-8")
ROLE_KEYS = set(re.findall(r'\{"key":\s*"([a-z_]+)"', ROLES_SRC))

#: The runner's state-key -> event-role table: ("market_report", "market"), ...
PUBLISHED = set(re.findall(r'\("[a-z_]+",\s*"([a-z_]+)"\)', MODULE._RUNNER))


class RunPlanTests(unittest.TestCase):
    def test_the_regexes_found_the_real_tables(self):
        self.assertIn("sentiment", PUBLISHED)
        self.assertIn("portfolio", PUBLISHED)
        self.assertIn("bull", ROLE_KEYS)

    def test_a_us_run_seats_the_base_roster_in_order(self):
        plan = MODULE.run_plan({"ticker": "NVDA"})
        self.assertEqual(len(plan["analysts"]), len(MODULE.BASE_ANALYSTS))
        self.assertEqual(plan["analysts"][0], "market")

    def test_an_a_share_run_seats_the_specialists(self):
        plan = MODULE.run_plan({"ticker": "600519.SS"})
        self.assertEqual(len(plan["analysts"]), len(MODULE.ASTOCK_ANALYSTS))
        self.assertEqual(plan["analysts"][-3:], ["policy", "hot_money", "lockup"])

    def test_the_sentiment_analyst_is_counted_by_the_name_its_events_carry(self):
        plan = MODULE.run_plan({"ticker": "NVDA"})
        self.assertIn("sentiment", plan["analysts"])
        self.assertNotIn("social", plan["analysts"])

    def test_every_planned_analyst_is_one_the_runner_publishes(self):
        for ticker in ("NVDA", "600519.SS"):
            with self.subTest(ticker=ticker):
                missing = set(MODULE.run_plan({"ticker": ticker})["analysts"]) - PUBLISHED
                self.assertEqual(missing, set())

    def test_the_debate_seats_the_page_counts_are_published_too(self):
        # agent_progress.js counts these by name.
        for role in ("bull", "bear", "research_mgr", "trader",
                     "aggressive", "conservative", "neutral", "portfolio"):
            with self.subTest(role=role):
                self.assertIn(role, PUBLISHED)
                self.assertIn(role, ROLE_KEYS)

    def test_rounds_are_positive_integers(self):
        plan = MODULE.run_plan({"ticker": "NVDA"})
        for key in ("debate_rounds", "risk_rounds"):
            with self.subTest(key=key):
                self.assertIsInstance(plan[key], int)
                self.assertGreaterEqual(plan[key], 1)
        self.assertEqual(MODULE._rounds("junk"), 1)
        self.assertEqual(MODULE._rounds("0"), 1)
        self.assertEqual(MODULE._rounds("4"), 4)

    def test_a_missing_job_still_gets_a_plan(self):
        self.assertEqual(MODULE.run_plan(None)["analysts"][0], "market")


if __name__ == "__main__":
    unittest.main()
