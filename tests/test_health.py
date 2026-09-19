"""Tests for :mod:`ystocker.health`.

The arithmetic is simple; the refusals are the point. Each one is a case where a
number would render in the same column, in the same font, as a meaningful one —
and two of them are sector-shaped rather than arithmetic, which is the kind that
survives a review of the formula.

Pure: no app, no network, no cache.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import health  # noqa: E402

# Roughly Apple's shape, read off the live info dict.
APPLE = {
    "totalDebt": 84_343_996_416,
    "totalCash": 62_399_000_576,
    "ebitda": 167_959_003_136,
    "totalRevenue": 466_822_987_776,
    "freeCashflow": 107_721_875_456,
    "operatingCashflow": 146_723_995_648,
    "netIncomeToCommon": 128_929_996_800,
    "currentRatio": 1.003,
    "quickRatio": 0.812,
    "debtToEquity": 78.445,
    "payoutRatio": 0.1204,
    "returnOnAssets": 0.27082002,
    "operatingMargins": 0.32623002,
}


class LeverageTests(unittest.TestCase):
    def test_net_debt_is_debt_minus_cash(self):
        out = health.assess(APPLE, sector="Technology")
        self.assertAlmostEqual(out["net_debt"], 84_343_996_416 - 62_399_000_576, places=0)

    def test_net_debt_ebitda(self):
        out = health.assess(APPLE, sector="Technology")
        self.assertAlmostEqual(out["net_debt_ebitda"], 0.13, places=2)
        self.assertEqual(out["leverage_band"], "low")

    def test_net_cash_is_reported_as_a_positive_figure(self):
        """A reader scanning a debt column reads -60B as a typo."""
        rich = {**APPLE, "totalCash": 200_000_000_000}
        out = health.assess(rich, sector="Technology")
        self.assertLess(out["net_debt"], 0)
        self.assertGreater(out["net_cash"], 0)
        self.assertEqual(out["leverage_band"], "net_cash")

    def test_a_leveraged_company_lands_in_a_higher_band(self):
        levered = {**APPLE, "totalDebt": 700_000_000_000, "totalCash": 10_000_000_000}
        out = health.assess(levered, sector="Industrials")
        self.assertGreater(out["net_debt_ebitda"], 4)
        self.assertEqual(out["leverage_band"], "high")

    def test_bands_are_ordered_and_cover_the_line(self):
        seen = [health.leverage_band(r) for r in (-1, 0.5, 2.0, 3.5, 9.0)]
        self.assertEqual(seen, ["net_cash", "low", "moderate", "elevated", "high"])

    def test_no_band_without_a_ratio(self):
        """`None` in, `None` out — never a default. A defaulted band would put a
        company with no data in the same bucket as a measured one."""
        self.assertIsNone(health.leverage_band(None))
        self.assertIsNone(health.conversion_band(None))


class RefusalTests(unittest.TestCase):
    def test_a_bank_gets_no_leverage_ratio(self):
        """Their debt is raw material, not financing — the same reason dcf.py
        declines a standard DCF for them. Any threshold right for an industrial
        flags every bank as distressed."""
        out = health.assess({**APPLE, "totalDebt": 2e12}, sector="Financial Services")
        self.assertNotIn("net_debt_ebitda", out)
        self.assertEqual(out["leverage_reason"], "financial_sector")
        # The raw figure survives — it is a fact, it just is not a ratio.
        self.assertIn("net_debt", out)

    def test_the_sector_match_is_case_and_space_insensitive(self):
        for sector in ("financial services", "  Financial Services  ", "FINANCIALS"):
            with self.subTest(sector=sector):
                out = health.assess(APPLE, sector=sector)
                self.assertEqual(out.get("leverage_reason"), "financial_sector")

    def test_a_non_financial_sector_is_not_caught(self):
        out = health.assess(APPLE, sector="Technology")
        self.assertNotIn("leverage_reason", out)

    def test_negative_ebitda_refuses(self):
        """A company with negative EBITDA is not "zero times levered"."""
        out = health.assess({**APPLE, "ebitda": -5e9}, sector="Technology")
        self.assertNotIn("net_debt_ebitda", out)
        self.assertEqual(out["leverage_reason"], "negative_ebitda")

    def test_missing_ebitda_refuses(self):
        info = {k: v for k, v in APPLE.items() if k != "ebitda"}
        out = health.assess(info, sector="Technology")
        self.assertEqual(out["leverage_reason"], "no_data")

    def test_conversion_refuses_on_a_loss(self):
        """Dividing by a loss gives a number with the wrong sign that renders in
        the same column as a healthy one."""
        out = health.assess({**APPLE, "netIncomeToCommon": -1e9}, sector="Technology")
        self.assertNotIn("cash_conversion", out)
        self.assertEqual(out["conversion_reason"], "negative_income")

    def test_reason_and_value_are_mutually_exclusive(self):
        for info, sector in ((APPLE, "Technology"),
                             (APPLE, "Financial Services"),
                             ({**APPLE, "ebitda": -1}, "Technology")):
            out = health.assess(info, sector=sector)
            with self.subTest(sector=sector):
                self.assertNotEqual("net_debt_ebitda" in out,
                                    "leverage_reason" in out)

    def test_every_reason_is_declared(self):
        seen = set()
        for info, sector in (
            (APPLE, "Financial Services"),
            ({**APPLE, "ebitda": -1}, "Technology"),
            ({k: v for k, v in APPLE.items() if k != "ebitda"}, "Technology"),
            ({**APPLE, "netIncomeToCommon": -1}, "Technology"),
        ):
            out = health.assess(info, sector=sector)
            seen.update({out.get("leverage_reason"), out.get("conversion_reason")})
        seen.discard(None)
        self.assertEqual(seen, set(health.REFUSALS))


class CashConversionTests(unittest.TestCase):
    def test_conversion_is_fcf_over_income(self):
        out = health.assess(APPLE, sector="Technology")
        self.assertAlmostEqual(out["cash_conversion"], 0.836, places=3)
        # 0.836 — free cash flow is 84% of reported profit, which sits in the
        # healthy band. Asserted as the value *and* the band so a shifted
        # boundary is caught rather than silently reclassifying every company.
        self.assertEqual(out["conversion_band"], "healthy")

    def test_a_company_converting_fully_is_healthy(self):
        out = health.assess({**APPLE, "freeCashflow": 130_000_000_000}, sector="Technology")
        self.assertGreater(out["cash_conversion"], 1.0)
        self.assertIn(out["conversion_band"], ("healthy", "strong"))

    def test_poor_conversion_is_named(self):
        """The case the whole figure exists for: profit that is not arriving as
        money, hidden behind a normal-looking P/E."""
        out = health.assess({**APPLE, "freeCashflow": 20_000_000_000}, sector="Technology")
        self.assertLess(out["cash_conversion"], 0.5)
        self.assertEqual(out["conversion_band"], "poor")

    def test_capex_intensity_is_the_share_of_operating_cash_consumed(self):
        out = health.assess(APPLE, sector="Technology")
        expected = (1 - 107_721_875_456 / 146_723_995_648) * 100
        self.assertAlmostEqual(out["capex_intensity"], expected, places=1)

    def test_fcf_margin(self):
        out = health.assess(APPLE, sector="Technology")
        self.assertAlmostEqual(out["fcf_margin"],
                               107_721_875_456 / 466_822_987_776 * 100, places=2)


class RobustnessTests(unittest.TestCase):
    def test_an_empty_info_yields_an_empty_block_not_an_error(self):
        """This runs over every cached ticker, including ADRs and foreign
        listings with thin coverage."""
        self.assertEqual(health.assess({}), {})

    def test_one_missing_field_does_not_cost_the_block(self):
        info = {k: v for k, v in APPLE.items() if k != "totalCash"}
        out = health.assess(info, sector="Technology")
        self.assertNotIn("net_debt", out)
        self.assertIn("cash_conversion", out)     # unrelated figure survives
        self.assertIn("current_ratio", out)

    def test_strings_and_nans_are_ignored(self):
        """Yahoo returns strings and NaNs in places, and a NaN propagates
        silently through every arithmetic it touches."""
        out = health.assess({**APPLE, "currentRatio": "n/a",
                             "quickRatio": float("nan")}, sector="Technology")
        self.assertNotIn("current_ratio", out)
        self.assertNotIn("quick_ratio", out)
        self.assertIn("net_debt_ebitda", out)

    def test_no_sector_still_produces_leverage(self):
        """Sector is optional; absent it, only the financial carve-out is lost."""
        out = health.assess(APPLE)
        self.assertIn("net_debt_ebitda", out)

    def test_zero_denominators_are_skipped(self):
        out = health.assess({**APPLE, "totalRevenue": 0, "operatingCashflow": 0},
                            sector="Technology")
        self.assertNotIn("fcf_margin", out)
        self.assertNotIn("capex_intensity", out)


if __name__ == "__main__":
    unittest.main()


class TranslationParityTests(unittest.TestCase):
    """Every refusal and band must have copy in both languages.

    They reach the page as `health.why_<reason>` / `health.band_<name>` with the
    raw identifier as the fallback, so a missing key does not fail — it renders
    `financial_sector` under a figure somebody is about to act on.
    """

    def _i18n(self) -> str:
        return (Path(__file__).resolve().parent.parent
                / "ystocker" / "static" / "i18n.js").read_text()

    def test_every_refusal_has_a_string(self):
        js = self._i18n()
        missing = [r for r in health.REFUSALS if f"'health.why_{r}'" not in js]
        self.assertEqual(missing, [], f"no i18n key for: {missing}")

    def test_every_band_has_a_string(self):
        js = self._i18n()
        bands = ({b for _, b in health.LEVERAGE_BANDS} | {"high"}
                 | {b for _, b in health.CONVERSION_BANDS} | {"strong"})
        missing = [b for b in sorted(bands) if f"'health.band_{b}'" not in js]
        self.assertEqual(missing, [], f"no i18n key for: {missing}")

    def test_both_languages_are_present(self):
        import re
        js = self._i18n()
        keys = [f"health.why_{r}" for r in health.REFUSALS]
        keys += ["history.group_health", "history.net_debt", "history.leverage"]
        for key in keys:
            m = re.search(r"'" + re.escape(key) + r"':\s*\{(.*?)\}", js, re.S)
            self.assertIsNotNone(m, f"{key} not found")
            self.assertIn("en:", m.group(1), f"{key} has no en")
            self.assertIn("zh:", m.group(1), f"{key} has no zh")
