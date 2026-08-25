"""Role-heading protocol tests without importing the Flask application."""

import importlib.util
import pathlib
import unittest


PATH = pathlib.Path(__file__).parents[1] / "ystocker" / "agent_roles.py"
SPEC = importlib.util.spec_from_file_location("agent_roles_under_test", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class AgentRoleTests(unittest.TestCase):
    def test_three_astock_roles_are_canonical(self):
        by_key = {role["key"]: role for role in MODULE.ROLES}
        self.assertEqual(by_key["policy"]["name"], "Policy Analyst")
        self.assertEqual(by_key["hot_money"]["name"], "Hot Money Tracker")
        self.assertEqual(by_key["lockup"]["name"], "Lock-up Monitor")

    def test_aliases_and_nested_headings(self):
        report = """## I. Analyst Team Reports
### Policy Analyst
Policy body
## Internal model heading
Still policy
### Hot Money Tracker
Flow body
### Lock-up Watcher
Unlock body
"""
        sections = [s for s in MODULE.split_sections(report) if s["role"]]
        self.assertEqual([s["role"]["key"] for s in sections], ["policy", "hot_money", "lockup"])
        self.assertIn("## Internal model heading", sections[0]["body"])


if __name__ == "__main__":
    unittest.main()
