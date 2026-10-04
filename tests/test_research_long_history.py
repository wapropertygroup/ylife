"""Tests for the long-history block of the deep-research data pack (research.py).

No app, no network, no model. The ✦ Research report on /history used to see
Yahoo's three to five years and eight quarters only; the page now sends the
Fundamentals tab's decade (``_longHistory`` in history.html) as
``bundle["long_history"]``, and these pin how the prompt renders it -- and that
a ticker not built yet adds nothing, rather than an empty table that reads as
"no history".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import research  # noqa: E402

LONG = {
    "source": "sec", "currency": "USD",
    "latest": {"filed": "2026-08-26", "form": "10-Q", "end": "2026-07-26"},
    "annual": [
        {"fy": "FY2026", "end": "2026-01-25", "revenue": 215.94, "revenue_yoy": 65.5,
         "gross_margin": 71.1, "operating_margin": 60.4, "net_margin": 55.6, "eps": 4.9,
         "fcf": 97.0, "roe": 101.5, "shares": 24.51},
        {"fy": "FY2025", "end": "2025-01-26", "revenue": 130.5, "revenue_yoy": 114.2,
         "gross_margin": 75.0, "operating_margin": 62.4, "net_margin": 55.8, "eps": 2.94,
         "fcf": 60.85, "roe": 119.2, "shares": 24.8},
    ],
    "ttm": [{"end": "2026-07-26", "period": "Q2 FY2027", "revenue": 302.97, "revenue_yoy": 83.4,
             "net_income": 192.88, "fcf": 127.0, "gross_margin": 74.8, "roe": 117.2}],
    "pe_10y": {"now": 29.6, "median": 51.8, "low": 21.0, "high": 153.0, "quarters": 40,
               "percentile": 15},
}


def _L(lang):
    return (lambda en, cn: cn if lang == "zh" else en)


class LongHistoryTests(unittest.TestCase):

    def test_nothing_sent_adds_nothing(self):
        self.assertEqual(research._long_history_blocks({}, _L("en")), [])
        self.assertEqual(research._long_history_blocks({"annual": [], "ttm": []}, _L("en")), [])

    def test_the_decade_renders_as_tables_with_its_source(self):
        text = "\n".join(research._long_history_blocks(LONG, _L("en")))
        self.assertIn("Ten-year history from the company's filings (SEC filings; USD billions", text)
        self.assertIn("FY2026 | 215.94 | 65.50", text)
        self.assertIn("Q2 FY2027", text)
        self.assertIn("- Now: 29.6x", text)
        self.assertIn("- Percentile of the range: 15%", text)
        self.assertIn("- Filed: 2026-08-26", text)

    def test_chinese_labels(self):
        text = "\n".join(research._long_history_blocks(LONG, _L("zh")))
        self.assertIn("公司财报中的十年历史（SEC 财报", text)
        self.assertIn("在区间中的百分位", text)

    def test_a_yahoo_sourced_decade_says_so(self):
        text = "\n".join(research._long_history_blocks(dict(LONG, source="yahoo", currency="JPY"), _L("en")))
        self.assertIn("(Yahoo Finance statements; JPY billions", text)

    def test_it_reaches_the_data_pack(self):
        pack = research._render_data_pack({"identity": {"ticker": "NVDA"}, "long_history": LONG}, "en")
        self.assertIn("### Ten-year history from the company's filings", pack)
        self.assertNotIn("### Ten-year history", research._render_data_pack({"identity": {}}, "en"))


if __name__ == "__main__":
    unittest.main()
