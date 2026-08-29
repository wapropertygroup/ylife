"""Focused tests for the private portfolio AI prompt."""
from __future__ import annotations

import unittest

from ystocker.assets import build_ai_prompt


class AssetsAiPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "total_value": 100_000,
            "position_count": 2,
            "coverage_pct": 72.5,
            "pending_count": 1,
            "positions": [
                {"symbol": "VOO", "name": "Private imported name",
                 "account": "Family Trust 1234", "quantity": 100,
                 "value": 60_000, "kind": "fund", "gain_pct": 12.4},
                {"symbol": "AAPL", "account": "Secret IRA", "quantity": 50,
                 "value": 40_000, "kind": "equity", "gain_pct": -3.0},
            ],
            "exposures": [{"symbol": "AAPL", "pct": 44.2,
                           "direct_pct": 40, "indirect_pct": 4.2,
                           "route_count": 2}],
            "overlaps": [{"symbol": "AAPL", "pct": 44.2,
                          "direct_pct": 40, "route_count": 2}],
            "asset_mix": [{"key": "stock", "pct": 99.9}],
            "sector_mix": [{"key": "technology", "pct": 42.1}],
            "residual": {"undisclosed_equity": {"value": 27_500, "pct": 27.5}},
        }

    def test_prompt_keeps_decision_facts_but_omits_private_fields(self) -> None:
        prompt = build_ai_prompt(self.snapshot, "en")
        self.assertIn("VOO; weight 60.00%", prompt)
        self.assertIn("AAPL: at least 44.20%", prompt)
        self.assertIn("Named-company look-through coverage: 72.50%", prompt)
        self.assertNotIn("Family Trust", prompt)
        self.assertNotIn("Secret IRA", prompt)
        self.assertNotIn("Private imported name", prompt)
        self.assertNotIn("quantity", prompt.lower())

    def test_chinese_language_is_explicit(self) -> None:
        prompt = build_ai_prompt(self.snapshot, "zh")
        self.assertIn("Write the entire answer in Simplified Chinese", prompt)
        self.assertIn("至少", prompt)


if __name__ == "__main__":
    unittest.main()
