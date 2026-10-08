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


class HeaderNormaliserTests(unittest.TestCase):
    """TradingAgents 0.6.0 heads a report with bullets (analysis date, rating,
    stamp, a memory-log note). agents.normalise_report_header, applied wherever
    a run's report is recorded, leaves the title and a bare stamp, which is what
    the page, the mails, the PDF and the public copy read."""

    def test_english_header_becomes_a_title_and_a_stamp(self):
        from ystocker import agents

        report = ("# Trading Analysis Report: NVDA\n\n- Analysis date: 2026-10-06\n"
                  "- Rating: Buy\n- Generated: 2026-10-07 01:02:03\n"
                  "- Memory log: 2 past decision(s) could not be settled this run and stay pending.\n\n"
                  "## I. Analyst Team Reports\n\n### Market Analyst\n- Rating: kept\n")
        out = agents.normalise_report_header(report)
        self.assertTrue(out.startswith("# Trading Analysis Report: NVDA\n\nGenerated: 2026-10-07 01:02:03\n"))
        self.assertNotIn("Analysis date", out)
        self.assertNotIn("Memory log", out)
        self.assertIn("- Rating: kept", out)            # a turn's own bullets stay
        self.assertEqual(report_pdf._strip_redundant_preamble(out.split("## I.")[0]), "")

    def test_chinese_header_keeps_its_stamp_unspaced(self):
        from ystocker import agents

        report = "# 交易分析报告：NVDA\n\n- 分析日期：2026-10-06\n- 评级：买入\n- 生成时间：2026-10-07 01:02:03\n\n## I. x\n"
        out = agents.normalise_report_header(report)
        self.assertIn("\n生成时间：2026-10-07 01:02:03\n", out)
        self.assertNotIn("评级", out)

    def test_an_older_report_is_unchanged(self):
        from ystocker import agents

        report = "# Trading Analysis Report: NVDA\n\nGenerated: 2026-09-21 19:16:50\n\n## I. x\n"
        self.assertEqual(agents.normalise_report_header(report), report)


if __name__ == "__main__":
    unittest.main()
