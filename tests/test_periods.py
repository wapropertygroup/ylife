"""Tests for ystocker.periods — where a return period starts.

The three sector sections on /markets now share this rule, after each had its
own: the rotation grid started a window at its first close *inside* the period
(dropping that session's move — for YTD, 2 January's), and the ranking's "YTD"
column was a one-year return. So besides the rule itself, this pins that both
routes call it — the arithmetic being right and it being called are different
questions.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

import pandas as pd

from ystocker import periods

ROUTES = Path(__file__).resolve().parent.parent / "ystocker" / "routes.py"


def _series(start: str, end: str, value: float = 100.0) -> pd.Series:
    days = pd.bdate_range(start, end)
    return pd.Series([value] * len(days), index=days, dtype=float)


class RuleTests(unittest.TestCase):

    def test_ytd_includes_the_first_session_of_the_year(self):
        s = _series("2025-06-02", "2026-06-30").drop(pd.Timestamp("2026-01-01"))  # a holiday
        s.loc["2026-01-02":] = 110.0             # the whole year's move is on 2 Jan
        self.assertAlmostEqual(periods.period_return(s, "YTD"), 10.0)
        # The old rule — first close inside the window — reads this as flat.
        inside = s[s.index >= "2026-01-01"]
        self.assertEqual(inside.iloc[-1] / inside.iloc[0] - 1, 0.0)

    def test_a_year_that_ends_on_a_weekend(self):
        # 2022 ended on a Saturday and 2 Jan 2023 was a market holiday, so the
        # base is Friday 30 December. An on-or-after rule lands on 3 January
        # and silently drops the first session of the year — the grid's old bug.
        s = _series("2022-06-01", "2023-06-30").drop(pd.Timestamp("2023-01-02"))
        s.loc["2023-01-03":] = 104.0
        self.assertEqual(periods.base_date(s.index, s.index[-1], "YTD"), pd.Timestamp("2022-12-30"))
        self.assertAlmostEqual(periods.period_return(s, "YTD"), 4.0)

    def test_the_base_is_the_close_before_the_window(self):
        days = pd.bdate_range("2025-06-02", "2026-06-30")   # ends Tue 30 Jun
        end = days[-1]
        self.assertEqual(periods.base_date(days, end, "1D"), pd.Timestamp("2026-06-29"))
        self.assertEqual(periods.base_date(days, end, "1W"), pd.Timestamp("2026-06-23"))
        self.assertEqual(periods.base_date(days, end, "YTD"), pd.Timestamp("2025-12-31"))
        self.assertEqual(periods.base_date(days, end, "1Y"), pd.Timestamp("2025-06-30"))

    def test_an_anniversary_on_a_weekend_falls_back_to_the_friday(self):
        days = pd.bdate_range("2026-01-01", "2026-06-30")
        # 30 Jun minus a month is Sat 30 May; the base is Fri 29 May.
        self.assertEqual(periods.base_date(days, days[-1], "1M"), pd.Timestamp("2026-05-29"))

    def test_month_arithmetic_clamps_rather_than_rolls(self):
        days = pd.bdate_range("2025-12-01", "2026-03-31")
        # 31 Mar minus a month is 28 Feb (a Saturday in 2026 -> Fri 27 Feb),
        # not "3 March", which is where naive day arithmetic would land.
        self.assertEqual(periods.base_date(days, days[-1], "1M"), pd.Timestamp("2026-02-27"))

    def test_monday_1d_is_against_friday(self):
        days = pd.bdate_range("2026-06-01", "2026-06-29")   # ends Mon 29 Jun
        self.assertEqual(periods.base_date(days, days[-1], "1D"), pd.Timestamp("2026-06-26"))

    def test_a_series_too_short_for_the_period_reports_nothing(self):
        # Measuring from its first close would put half a year under "1Y".
        s = _series("2026-01-05", "2026-06-30")
        s.iloc[-1] = 130.0
        self.assertIsNone(periods.period_return(s, "1Y"))
        self.assertIsNotNone(periods.period_return(s, "3M"))

    def test_degenerate_series(self):
        self.assertIsNone(periods.period_return(pd.Series(dtype=float), "1M"))
        self.assertIsNone(periods.period_return(_series("2026-06-30", "2026-06-30"), "1D"))
        with_nans = _series("2026-01-02", "2026-06-30")
        with_nans.iloc[-3:] = float("nan")
        self.assertIsNotNone(periods.period_return(with_nans, "1M"), "trailing NaN is dropped, not fatal")
        with self.assertRaises(ValueError):
            periods.anniversary(pd.Timestamp("2026-06-30"), "2Q")

    def test_a_timezone_aware_index_is_read_as_its_dates(self):
        s = _series("2025-06-02", "2026-06-30")
        s.loc["2026-01-02":] = 105.0
        s.index = s.index.tz_localize("America/New_York")
        self.assertAlmostEqual(periods.period_return(s, "YTD"), 5.0)


class RoutesUseTheRuleTests(unittest.TestCase):
    """Source-level, because importing routes.py needs the whole app."""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(ROUTES.read_text())
        cls.funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def _calls(self, func: str) -> set[str]:
        out = set()
        for node in ast.walk(self.funcs[func]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name):
                out.add(f"{node.func.value.id}.{node.func.attr}")
        return out

    def test_the_rotation_grid_uses_it(self):
        self.assertIn("periods.period_return", self._calls("api_sector_rotation_grid"))

    def test_the_ranking_uses_it(self):
        self.assertIn("periods.period_return", self._calls("api_sector_ranking"))

    def test_the_ranking_no_longer_reads_the_first_close(self):
        # closes.iloc[0] of a one-year download is how "YTD" became one year.
        src = ast.get_source_segment(ROUTES.read_text(), self.funcs["api_sector_ranking"])
        self.assertNotIn(".iloc[0]", src)


if __name__ == "__main__":
    unittest.main()
