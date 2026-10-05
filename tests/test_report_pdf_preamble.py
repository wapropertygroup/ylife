"""The PDF drops the report's own title and stamp, in either language.

The PDF's header already states the ticker and when the report was made, so
``report_pdf`` drops the package's ``# Trading Analysis Report`` line and its
``Generated:`` stamp from the body. Since 2026-10-05 TradingAgents writes those
two lines in the report's language, and a Chinese report's ``# 交易分析报告`` /
``生成时间：`` would otherwise print under a header that says the same.

No app, no network.
"""
from __future__ import annotations

import unittest

from ystocker import report_pdf


class RedundantPreambleTests(unittest.TestCase):
    def test_english_title_and_stamp_are_dropped(self):
        body = "# Trading Analysis Report: NVDA\n\nGenerated: 2026-09-21 19:16:50\n\nkept"
        self.assertEqual(report_pdf._strip_redundant_preamble(body), "kept")

    def test_chinese_title_and_stamp_are_dropped(self):
        body = "# 交易分析报告：NVDA\n\n生成时间：2026-09-21 19:16:50\n\n保留"
        self.assertEqual(report_pdf._strip_redundant_preamble(body), "保留")

    def test_other_preamble_lines_stay(self):
        body = "# 交易分析报告：NVDA\n\n> 本公开版略去了交易员。"
        self.assertEqual(report_pdf._strip_redundant_preamble(body), "> 本公开版略去了交易员。")


if __name__ == "__main__":
    unittest.main()
