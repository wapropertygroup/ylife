"""
Unit tests for the decision ledger — ``ystocker.decisions`` and ``ystocker.settle``.

No app, no network, no DynamoDB: the arithmetic and the row shape are pure, which is
the whole reason the fetching half lives in ``settle`` and not here.

The two tests that matter most are
:meth:`TestReferenceDate.test_a_run_that_finished_after_the_close_cannot_use_it` and
:meth:`TestForwardReturns.test_an_unmatured_horizon_is_absent_not_zero`. The first is
lookahead: measuring from a close that had already printed before the decision
existed credits the system with a move it could not have acted on, and the error is
invisible because the number looks perfectly ordinary. The second is the rule that
makes the ledger quotable — a horizon filled with zero would be counted by every
average taken over the table.
"""

from __future__ import annotations

import unittest
from unittest import mock

from ystocker import decisions

# A real fortnight of US sessions: 2026-08-28 is a Friday, 08-31 the Monday, and
# 09-07 is Labor Day, so the gap after 09-04 is a genuine holiday rather than a
# weekend. Using a real shape means the session arithmetic is not tested against a
# calendar that cannot happen.
SESSIONS = ["2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31",
            "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
            "2026-09-08", "2026-09-09"]
CLOSES = {d: 100.0 + i for i, d in enumerate(SESSIONS)}
# Deliberately NOT a scalar multiple of CLOSES: 50 + 0.5i has the same
# period-on-period ratio as 100 + i, so every benchmark return matched the
# instrument's by construction and the comparison proved nothing.
BENCH = {d: 200.0 - i * 1.5 for i, d in enumerate(SESSIONS)}


def _job(**over):
    job = {
        "id": "abc123", "status": "done", "ticker": "nvda", "date": "2026-08-28",
        "finished_at": "2026-08-28T18:00:00+00:00", "decision": "Buy",
        "elapsed_sec": 890.4, "fallback_models": [], "portfolio_context": True,
        "pm_levels": {"rating": "Buy", "position_size_pct": 5.0,
                      "entry_price": 100.0, "stop_loss": 95.0,
                      "price_target": 115.0, "flags": []},
        "trader_levels": {"action": "Buy", "position_size_pct": 20.0,
                          "entry_price": 100.0, "stop_loss": 95.0,
                          "target_price": 115.0, "reward_risk": 3.0},
        "risk_gate": {"verdict": "clamped", "proposed_size_pct": 20.0,
                      "approved_size_pct": 5.0},
        "gate_compliance": {"status": "compliant", "violated": False},
    }
    job.update(over)
    return job


class TestReferenceDate(unittest.TestCase):
    """Which close a decision could actually have been acted on."""

    def test_a_run_that_finished_before_the_close_uses_that_session(self):
        # 14:00 in New York: the close is still ahead.
        self.assertEqual(
            decisions.reference_date("2026-08-28T18:00:00+00:00", SESSIONS),
            "2026-08-28")

    def test_a_run_that_finished_after_the_close_cannot_use_it(self):
        # 17:00 in New York. Measuring from Friday's close here would credit the
        # system with a move that had already printed before it spoke, and nothing
        # about the resulting number would look wrong.
        self.assertEqual(
            decisions.reference_date("2026-08-28T21:00:00+00:00", SESSIONS),
            "2026-08-31")

    def test_the_boundary_is_the_exchange_close_to_the_minute(self):
        self.assertEqual(
            decisions.reference_date("2026-08-28T19:59:00+00:00", SESSIONS),
            "2026-08-28")                                    # 15:59 ET
        self.assertEqual(
            decisions.reference_date("2026-08-28T20:00:00+00:00", SESSIONS),
            "2026-08-31")                                    # 16:00 ET

    def test_daylight_saving_is_handled_by_the_zone_not_a_fixed_offset(self):
        # January: New York is UTC-5, so 20:00Z is 15:00 ET and still before the
        # close. A hard-coded UTC hour would get exactly this wrong for months.
        winter = ["2026-01-05", "2026-01-06", "2026-01-07"]
        self.assertEqual(
            decisions.reference_date("2026-01-06T20:00:00+00:00", winter),
            "2026-01-06")
        self.assertEqual(
            decisions.reference_date("2026-01-06T21:30:00+00:00", winter),
            "2026-01-07")                                    # 16:30 ET

    def test_a_weekend_finish_rolls_to_the_next_session(self):
        self.assertEqual(
            decisions.reference_date("2026-08-29T12:00:00+00:00", SESSIONS),
            "2026-08-31")

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(
            decisions.reference_date("2026-08-28T18:00:00", SESSIONS),
            "2026-08-28")

    def test_no_session_ahead_yields_none(self):
        # A run that finished after the last session anybody has prices for is not
        # settleable yet, and must not silently reuse the last close.
        self.assertIsNone(
            decisions.reference_date("2027-01-01T12:00:00+00:00", SESSIONS))

    def test_unparseable_or_missing_input_yields_none(self):
        for bad in ("", "not a date", "2026-13-45T00:00:00Z"):
            self.assertIsNone(decisions.reference_date(bad, SESSIONS), bad)
        self.assertIsNone(decisions.reference_date("2026-08-28T18:00:00Z", []))


class TestForwardReturns(unittest.TestCase):
    """Horizons count sessions, and an unmatured one stays absent."""

    def test_one_session_ahead(self):
        # 101 -> 102 from 2026-08-27
        self.assertAlmostEqual(
            decisions.forward_returns(CLOSES, "2026-08-27", (1,))[1],
            102.0 / 101.0 - 1.0, places=6)

    def test_horizons_count_sessions_and_skip_the_holiday(self):
        # Three sessions after Wednesday 2026-09-02 is 2026-09-08: the weekend and
        # Labor Day (09-07) are absent from the series, so a calendar-day horizon
        # would land on a day with no close at all.
        got = decisions.forward_returns(CLOSES, "2026-09-02", (3,))[3]
        self.assertAlmostEqual(got, CLOSES["2026-09-08"] / CLOSES["2026-09-02"] - 1,
                               places=6)
        self.assertNotIn("2026-09-07", CLOSES)

    def test_a_friday_one_day_horizon_is_the_monday_not_the_saturday(self):
        got = decisions.forward_returns(CLOSES, "2026-08-28", (1,))[1]
        self.assertAlmostEqual(got, CLOSES["2026-08-31"] / CLOSES["2026-08-28"] - 1,
                               places=6)

    def test_an_unmatured_horizon_is_absent_not_zero(self):
        # A zero here would be counted by every average taken over the table.
        out = decisions.forward_returns(CLOSES, "2026-09-08", (1, 5, 20, 60))
        self.assertIsNotNone(out[1])
        for horizon in (5, 20, 60):
            self.assertIsNone(out[horizon], horizon)

    def test_a_reference_date_outside_the_series_yields_all_none(self):
        out = decisions.forward_returns(CLOSES, "2026-07-04", (1, 5))
        self.assertEqual(set(out.values()), {None})

    def test_a_zero_base_price_does_not_divide(self):
        closes = dict(CLOSES, **{"2026-08-27": 0.0})
        self.assertIsNone(decisions.forward_returns(closes, "2026-08-27", (1,))[1])


class TestSettleRow(unittest.TestCase):

    def _row(self, **over):
        row = {"date": "2026-08-28", "job_id": "j", "ticker": "NVDA",
               "finished_at": "2026-08-28T18:00:00+00:00"}
        row.update(over)
        return row

    def test_settling_fills_the_matured_horizons_and_stamps_the_time(self):
        out = decisions.settle_row(self._row(), CLOSES, BENCH)
        self.assertEqual(out["ref_date"], "2026-08-28")
        self.assertEqual(out["ref_close"], CLOSES["2026-08-28"])
        self.assertIsNotNone(out["ret_1d"])
        self.assertIsNotNone(out["ret_5d"])
        self.assertIsNone(out.get("ret_60d"))
        self.assertIn("settled_at", out)

    def test_the_benchmark_travels_alongside_rather_than_being_subtracted(self):
        out = decisions.settle_row(self._row(), CLOSES, BENCH)
        self.assertIsNotNone(out["bench_1d"])
        self.assertNotEqual(out["ret_1d"], out["bench_1d"])
        # An excess return is derivable; this module has not picked a definition.
        self.assertNotIn("excess_1d", out)
        self.assertNotIn("alpha_1d", out)

    def test_a_settled_horizon_is_never_rewritten(self):
        # A price series can be restated — a split, a dividend adjustment, a vendor
        # correction — and a ledger that re-derived every pass would rewrite history.
        first = decisions.settle_row(self._row(), CLOSES, BENCH)
        restated = dict(CLOSES, **{"2026-08-31": 999.0})
        second = decisions.settle_row(first, restated, BENCH)
        self.assertEqual(second["ret_1d"], first["ret_1d"])

    def test_settling_is_idempotent_when_nothing_matured(self):
        first = decisions.settle_row(self._row(), CLOSES, BENCH)
        second = decisions.settle_row(first, CLOSES, BENCH)
        self.assertEqual(
            {k: v for k, v in second.items() if k != "settled_at"},
            {k: v for k, v in first.items() if k != "settled_at"})

    def test_a_missing_benchmark_still_records_raw_returns(self):
        out = decisions.settle_row(self._row(), CLOSES, None)
        self.assertIsNotNone(out["ret_1d"])
        self.assertIsNone(out.get("bench_1d"))

    def test_a_benchmark_arriving_on_a_later_pass_fills_in(self):
        first = decisions.settle_row(self._row(), CLOSES, None)
        second = decisions.settle_row(first, CLOSES, BENCH)
        self.assertIsNotNone(second["bench_1d"])
        self.assertEqual(second["ret_1d"], first["ret_1d"])

    def test_an_unsettleable_row_is_returned_untouched(self):
        row = self._row(finished_at="2027-06-01T12:00:00+00:00")
        self.assertEqual(decisions.settle_row(row, CLOSES, BENCH), row)

    def test_is_fully_settled_requires_every_horizon(self):
        row = self._row()
        self.assertFalse(decisions.is_fully_settled(row))
        for horizon in decisions.HORIZONS:
            row[f"ret_{horizon}d"] = 0.01
        self.assertTrue(decisions.is_fully_settled(row))


class TestBuildRow(unittest.TestCase):

    def test_a_finished_job_yields_the_expected_columns(self):
        row = decisions.build_row(_job())
        self.assertEqual(row["date"], "2026-08-28")
        self.assertEqual(row["job_id"], "abc123")
        self.assertEqual(row["ticker"], "NVDA")               # upper-cased
        self.assertEqual(row["pm_size_pct"], 5.0)
        self.assertEqual(row["gate_approved_pct"], 5.0)
        self.assertEqual(row["trader_reward_risk"], 3.0)
        self.assertEqual(row["compliance_status"], "compliant")

    def test_the_row_date_is_when_the_decision_existed(self):
        # Not the trade date the analysis was written for: they differ whenever a
        # run is queued overnight, and a forward return must start from the former.
        row = decisions.build_row(_job(date="2026-08-27",
                                       finished_at="2026-08-28T18:00:00+00:00"))
        self.assertEqual(row["date"], "2026-08-28")
        self.assertEqual(row["trade_date"], "2026-08-27")

    def test_the_user_is_never_recorded(self):
        # Joinable back to ystocker-agent-jobs by job_id; keeping the address out
        # means the ledger is not itself user data.
        row = decisions.build_row(_job(user="someone@example.com"))
        self.assertNotIn("user", row)
        self.assertNotIn("someone@example.com", repr(row))

    def test_the_report_is_never_recorded(self):
        row = decisions.build_row(_job(report="#" * 50_000))
        self.assertNotIn("report", row)
        self.assertLess(len(repr(row)), 2_000)

    def test_no_derived_score_is_recorded(self):
        row = decisions.build_row(_job())
        for banned in ("sharpe", "alpha", "hit_rate", "excess"):
            self.assertNotIn(banned, repr(row).lower(), banned)

    def test_an_unfinished_job_yields_no_row(self):
        for status in ("queued", "running", "error"):
            self.assertIsNone(decisions.build_row(_job(status=status)), status)

    def test_a_job_with_no_id_yields_no_row(self):
        self.assertIsNone(decisions.build_row(_job(id="")))

    def test_a_free_text_run_still_yields_a_row(self):
        # No structured levels at all: the run happened and its outcome is still
        # worth measuring, so the row exists with the numeric columns absent.
        row = decisions.build_row(_job(pm_levels={}, trader_levels={},
                                       risk_gate={}, gate_compliance={}))
        self.assertIsNotNone(row)
        self.assertNotIn("pm_size_pct", row)
        self.assertNotIn("gate_verdict", row)

    def test_provenance_records_which_model_answered(self):
        row = decisions.build_row(_job(fallback_models=["gemini-2.5-flash"],
                                       degraded=True))
        self.assertEqual(row["models"], "gemini-2.5-flash")
        self.assertIs(row["degraded"], True)


class TestSerialisation(unittest.TestCase):
    """DynamoDB and the disk mirror hold the same shape, and one parser reads both."""

    def test_a_row_survives_a_round_trip(self):
        row = decisions.settle_row(
            decisions.build_row(_job()), CLOSES, BENCH)
        back = decisions._row_from_item(decisions._item_from_row(row))
        for key, value in row.items():
            if key == "settled_at":
                continue
            if isinstance(value, float):
                self.assertAlmostEqual(back[key], value, places=6, msg=key)
            else:
                self.assertEqual(back[key], value, key)

    def test_numbers_are_stored_as_strings(self):
        item = decisions._item_from_row(decisions.build_row(_job()))
        self.assertIsInstance(item["pm_size_pct"], str)
        self.assertIsInstance(item["elapsed_sec"], str)

    def test_booleans_stay_boolean(self):
        item = decisions._item_from_row(decisions.build_row(_job()))
        self.assertIsInstance(item["portfolio_context"], bool)

    def test_an_unparseable_cell_does_not_void_the_row(self):
        item = decisions._item_from_row(decisions.build_row(_job()))
        item["pm_size_pct"] = "not a number"
        back = decisions._row_from_item(item)
        self.assertNotIn("pm_size_pct", back)
        self.assertEqual(back["job_id"], "abc123")

    def test_the_key_pair_is_date_and_job_id(self):
        item = decisions._item_from_row(decisions.build_row(_job()))
        self.assertIn("date", item)
        self.assertIn("job_id", item)


class TestRecord(unittest.TestCase):
    """A ledger write must never break a finished run."""

    def test_a_dynamodb_failure_is_swallowed(self):
        table = mock.Mock()
        table.put_item.side_effect = RuntimeError("throttled")
        with mock.patch.object(decisions, "_get_table", return_value=table), \
             mock.patch.object(decisions, "_local_read", return_value={}), \
             mock.patch.object(decisions, "_local_write"):
            row = decisions.record(_job())
        self.assertIsNotNone(row)

    def test_a_malformed_job_is_swallowed(self):
        with mock.patch.object(decisions, "_get_table", return_value=None), \
             mock.patch.object(decisions, "_local_read", return_value={}), \
             mock.patch.object(decisions, "_local_write"):
            self.assertIsNone(decisions.record({"status": "done"}))

    def test_the_local_key_is_the_composite(self):
        self.assertEqual(
            decisions._local_key({"date": "2026-08-28", "job_id": "j"}),
            "2026-08-28#j")

    def test_agents_writes_the_ledger_on_both_completion_paths(self):
        import inspect

        from ystocker import agents

        source = inspect.getsource(agents._record_structured)
        self.assertIn("decisions.record", source)
        for fn in (agents._run, agents._reap):
            self.assertIn("_record_structured", inspect.getsource(fn), fn.__name__)


class TestSettleWorker(unittest.TestCase):
    """The fetching half, with the fetch stubbed out."""

    def test_a_pass_over_an_empty_ledger_does_nothing(self):
        from ystocker import settle

        with mock.patch.object(decisions, "unsettled", return_value=[]):
            self.assertEqual(settle.run_once()["rows"], 0)

    def test_one_fetch_per_ticker_not_per_row(self):
        from ystocker import settle

        rows = [decisions.build_row(_job(id=f"j{i}")) for i in range(4)]
        calls: list[str] = []

        def fake(ticker):
            calls.append(ticker)
            return CLOSES if ticker == "NVDA" else BENCH

        with mock.patch.object(decisions, "unsettled", return_value=rows), \
             mock.patch.object(decisions, "save_settled"), \
             mock.patch.object(settle, "_closes", side_effect=fake), \
             mock.patch.object(settle.time, "sleep"):
            summary = settle.run_once()

        self.assertEqual(summary["rows"], 4)
        self.assertEqual(calls.count("NVDA"), 1)
        self.assertEqual(calls.count(decisions.BENCHMARK), 1)

    def test_a_ticker_with_no_history_is_skipped_not_fatal(self):
        from ystocker import settle

        rows = [decisions.build_row(_job(id="a", ticker="NVDA")),
                decisions.build_row(_job(id="b", ticker="ZZZZ"))]
        saved: list[dict] = []
        with mock.patch.object(decisions, "unsettled", return_value=rows), \
             mock.patch.object(decisions, "save_settled",
                               side_effect=lambda r: saved.append(r)), \
             mock.patch.object(settle, "_closes",
                               side_effect=lambda t: {} if t == "ZZZZ" else CLOSES), \
             mock.patch.object(settle.time, "sleep"):
            settle.run_once()
        self.assertEqual([r["ticker"] for r in saved], ["NVDA"])

    def test_a_row_with_nothing_new_is_not_rewritten(self):
        from ystocker import settle

        settled = decisions.settle_row(decisions.build_row(_job()), CLOSES, BENCH)
        with mock.patch.object(decisions, "unsettled", return_value=[settled]), \
             mock.patch.object(decisions, "save_settled") as save, \
             mock.patch.object(settle, "_closes", return_value=CLOSES), \
             mock.patch.object(settle.time, "sleep"):
            settle.run_once()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
