"""
Tests for the hand-entered DCF override store (``ystocker.dcf_store``).

Validation only — no AWS, no Flask, no session. The store's DynamoDB half
degrades to a disk mirror by design and is exercised through the endpoint
checks; what is worth proving here is the validator, because it is the only
thing standing between a mistyped number and a public valuation score.

The rules that matter are the ones nothing downstream would catch:

* ``bear`` and ``bull`` typed into each other's boxes produce three fair values
  that ``combine_scenarios`` will happily weight 25/50/25. The score comes out
  merely wrong rather than obviously so.
* A wing with no base case has nothing to shrink toward; §3 maps ``V_Base``.
* A WACC at or below terminal growth can never score, so rejecting it while the
  person who typed it is still looking at the form beats storing a row that
  silently refuses for ever afterwards.
* A value outside its band is refused rather than clamped. A clamped fair value
  is a number the reader did not enter, sitting on a page that says they did.
"""
from __future__ import annotations

import unittest

from ystocker import dcf, dcf_store


class Validation(unittest.TestCase):

    def test_a_plain_three_scenario_row(self):
        row = dcf_store.validate("msft", {"bear": 300, "base": 400, "bull": 500,
                                          "valuation_date": "2026-09-01"})
        self.assertEqual(row["ticker"], "MSFT")
        self.assertEqual(row["mode"], dcf_store.MODE_VALUES)
        self.assertEqual(row["base"], 400.0)
        self.assertEqual(row["valuation_date"], "2026-09-01")

    def test_an_assumptions_only_row_is_a_different_mode(self):
        row = dcf_store.validate("MSFT", {"wacc": 0.09, "terminal_growth": 0.02})
        self.assertEqual(row["mode"], dcf_store.MODE_ASSUMPTIONS)
        self.assertNotIn("base", row)

    def test_fair_values_win_when_both_are_given(self):
        """The more specific claim. Recorded on the row rather than inferred at
        read time, so a reader can see which one is in force."""
        row = dcf_store.validate("MSFT", {"base": 400, "wacc": 0.09})
        self.assertEqual(row["mode"], dcf_store.MODE_VALUES)

    def test_scenarios_out_of_order_are_refused(self):
        for payload in ({"bear": 500, "base": 400, "bull": 300},
                        {"bear": 500, "base": 400},
                        {"base": 400, "bull": 300}):
            with self.assertRaises(dcf_store.ValidationError, msg=repr(payload)):
                dcf_store.validate("MSFT", payload)

    def test_a_wing_without_a_base_case_is_refused(self):
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("MSFT", {"bull": 500})

    def test_an_empty_submission_is_refused(self):
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("MSFT", {})
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("MSFT", {"note": "just a note"})

    def test_a_value_outside_its_band_is_refused_not_clamped(self):
        for field, bad in (("confidence", 0.2), ("confidence", 1.5),
                           ("w_dcf", 0.9), ("wacc", 0.9),
                           ("terminal_growth", 0.5), ("base", -1)):
            with self.assertRaises(dcf_store.ValidationError, msg=field):
                dcf_store.validate("MSFT", {field: bad, "base": 100}
                                   if field != "base" else {field: bad})

    def test_the_weight_ceiling_matches_the_engines(self):
        """Two ceilings that could drift apart is one ceiling too many."""
        from ystocker import dca

        self.assertEqual(dcf_store.FIELDS["w_dcf"][1], dca.MAX_DCF_WEIGHT)

    def test_the_confidence_band_matches_the_engines(self):
        self.assertEqual(dcf_store.FIELDS["confidence"],
                         (dcf.CONFIDENCE_FLOOR, dcf.CONFIDENCE_CEILING))

    def test_a_wacc_below_terminal_growth_is_refused_at_entry(self):
        """§13 calls this an invalid model. Catching it here means the person
        who typed it finds out now rather than through a row that silently
        refuses to score from then on."""
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("MSFT", {"wacc": 0.03, "terminal_growth": 0.025})

    def test_a_wacc_comfortably_above_terminal_growth_is_accepted(self):
        row = dcf_store.validate("MSFT", {"wacc": 0.09, "terminal_growth": 0.025})
        self.assertEqual(row["wacc"], 0.09)

    def test_a_missing_date_defaults_to_today_rather_than_to_nothing(self):
        """An override with no date can never be aged out, so it would outlive
        the staleness rule §13 exists to enforce."""
        row = dcf_store.validate("MSFT", {"base": 400})
        self.assertTrue(row["valuation_date"])
        self.assertEqual(len(row["valuation_date"]), 10)

    def test_an_unparseable_date_falls_back_rather_than_being_stored(self):
        row = dcf_store.validate("MSFT", {"base": 400, "valuation_date": "soon"})
        self.assertNotEqual(row["valuation_date"], "soon")

    def test_a_blank_ticker_is_refused(self):
        for bad in ("", "   ", None, "X" * 40):
            with self.assertRaises(dcf_store.ValidationError, msg=repr(bad)):
                dcf_store.validate(bad, {"base": 400})

    def test_a_long_note_is_truncated_rather_than_refused(self):
        """Prose is not an input to the arithmetic, so a cap is about row size
        and does not justify throwing away the numbers alongside it."""
        row = dcf_store.validate("MSFT", {"base": 400, "note": "x" * 5000})
        self.assertEqual(len(row["note"]), dcf_store.MAX_NOTE)

    def test_non_numeric_input_is_ignored_not_coerced(self):
        row = dcf_store.validate("MSFT", {"base": 400, "bull": "", "bear": None})
        self.assertNotIn("bull", row)
        self.assertNotIn("bear", row)

    def test_booleans_are_not_numbers(self):
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("MSFT", {"base": True})


class StoredRowFeedsTheEngine(unittest.TestCase):
    """A validated row must be directly usable by the scorer."""

    def test_a_validated_row_scores(self):
        row = dcf_store.validate("MSFT", {"bear": 360, "base": 500, "bull": 560,
                                          "confidence": 0.8,
                                          "valuation_date": "2026-09-01"})
        out = dcf.score(price=400.0, bear=row.get("bear"), base=row["base"],
                        bull=row.get("bull"), confidence=row.get("confidence"),
                        valuation_date=row["valuation_date"],
                        as_of="2026-09-12", source="override")
        self.assertFalse(out["refused"])
        self.assertAlmostEqual(out["V"], 71.0, places=4)
        self.assertEqual(out["source"], "override")


class FactorOverrides(unittest.TestCase):
    """Hand-entered relative-factor percentiles.

    These exist for a failure the DCF override cannot reach. A factor goes
    unmeasurable when its *series* could not be reconstructed at all — negative
    earnings leave no P/E history — and when enough of them go, the surviving
    weight drops under MIN_SURVIVING_WEIGHT and the whole ticker refuses to
    score. Reported on a semiconductor template left with only P/FCF and
    EV/EBITDA: 30% against a 50% floor, so five factors produced nothing.

    The unit is a **percentile**, not a multiple, and that is forced rather than
    chosen: with no reconstructed history there is nothing to rank a hand-typed
    multiple against.
    """

    def test_a_factor_map_is_enough_on_its_own(self):
        row = dcf_store.validate("NVDA", {"factors": {"pe": 62, "peg": 45.5}})
        self.assertEqual(row["factors"], {"pe": 62.0, "peg": 45.5})

    def test_an_empty_submission_is_still_refused(self):
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("NVDA", {})

    def test_a_percentile_outside_0_100_is_refused_not_clamped(self):
        """Outside the range is a mistake, not a strong opinion — and clamping
        would store a number the reader did not type."""
        for bad in (140, -3, 100.5):
            with self.assertRaises(dcf_store.ValidationError, msg=str(bad)):
                dcf_store.validate("NVDA", {"factors": {"pe": bad}})

    def test_an_unknown_factor_is_refused_rather_than_ignored(self):
        """A typo that stores silently looks exactly like an override that
        took effect, which is the worst of both."""
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("NVDA", {"factors": {"p_e": 50}})

    def test_every_known_factor_is_accepted(self):
        from ystocker.dca import DIRECTION

        row = dcf_store.validate("NVDA", {"factors": {k: 50 for k in DIRECTION}})
        self.assertEqual(len(row["factors"]), len(DIRECTION))

    def test_a_json_string_is_accepted(self):
        """A form post may send it encoded rather than as a nested object."""
        row = dcf_store.validate("NVDA", {"factors": '{"pe": 50}'})
        self.assertEqual(row["factors"], {"pe": 50.0})

    def test_malformed_json_is_refused_with_a_readable_message(self):
        with self.assertRaises(dcf_store.ValidationError):
            dcf_store.validate("NVDA", {"factors": "{pe: 50"})

    def test_a_blank_value_clears_rather_than_scoring_zero(self):
        """Zero is a real percentile meaning "cheapest it has ever been", so a
        blanked field must not arrive as one."""
        row = dcf_store.validate("NVDA", {"factors": {"pe": "", "peg": 40}})
        self.assertEqual(row["factors"], {"peg": 40.0})


class FactorOverridesUnblockScoring(unittest.TestCase):
    """The reported failure, end to end through the pure engine."""

    MEASURED = {"pfcf": 95.5, "ev_ebitda": 35.6}

    def test_the_reported_case_refuses_before_any_override(self):
        from ystocker import dca

        out = dca.evaluate(ticker="NVDA", model="semiconductor",
                           percentiles=self.MEASURED)
        self.assertIsNone(out["V"])
        self.assertAlmostEqual(out["surviving_weight"], 0.30, places=4)

    def test_filling_the_gaps_restores_a_score(self):
        from ystocker import dca

        filled = dict(self.MEASURED, pe=62.0, peg=45.0, cycle_adjusted=55.0)
        out = dca.evaluate(ticker="NVDA", model="semiconductor",
                           percentiles=filled,
                           overridden=["pe", "peg", "cycle_adjusted"])
        self.assertIsNotNone(out["V"])
        self.assertAlmostEqual(out["surviving_weight"], 1.0, places=4)

    def test_overridden_rows_are_marked_and_measured_ones_are_not(self):
        from ystocker import dca

        filled = dict(self.MEASURED, pe=62.0)
        out = dca.evaluate(ticker="NVDA", model="semiconductor",
                           percentiles=filled, overridden=["pe"])
        marked = {f["factor"] for f in out["factors"] if f["overridden"]}
        plain = {f["factor"] for f in out["factors"] if not f["overridden"]}
        self.assertEqual(marked, {"pe"})
        self.assertEqual(plain, {"pfcf", "ev_ebitda"})
        self.assertEqual(out["overridden"], ["pe"])

    def test_marking_changes_no_arithmetic(self):
        """The tag is presentation. If it moved the score, an override would be
        worth a different amount depending on whether we admitted to it."""
        from ystocker import dca

        filled = dict(self.MEASURED, pe=62.0, peg=45.0, cycle_adjusted=55.0)
        tagged = dca.evaluate(ticker="NVDA", model="semiconductor",
                              percentiles=filled, overridden=list(filled))
        plain = dca.evaluate(ticker="NVDA", model="semiconductor",
                             percentiles=filled)
        self.assertEqual(tagged["V"], plain["V"])
        self.assertEqual(tagged["E"], plain["E"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
