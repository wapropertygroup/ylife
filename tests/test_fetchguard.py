"""Fetchguard tests — no network, no Flask app.

Covers ystocker/fetchguard.py: the per-provider circuit breaker and the
persistent per-item back-off.

The retry/HTTP paths are exercised against a throwaway loopback HTTP server
rather than a mocked `requests`, because the behaviour that matters here is the
interaction between status codes, retry counts and the breaker -- and a mock of
`requests` would let a wrong assumption about that interaction pass silently.
Everything binds to port 0 and lives for the duration of one test.
"""

from __future__ import annotations

import http.server
import threading
import time
import unittest
from pathlib import Path

import requests

from ystocker import fetchguard as fg


class _Handler(http.server.BaseHTTPRequestHandler):
    """Replies with the status queued on the server, counting every hit."""

    def _reply(self):
        self.server.hits += 1
        status = self.server.statuses.pop(0) if self.server.statuses else self.server.final
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *args):
        pass


class _Server:
    def __enter__(self):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.hits = 0
        self.srv.statuses = []
        self.srv.final = 200
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/"
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        for p in ("p", "q", "stub"):
            fg.reset(p)

    def test_open_breaker_has_no_remaining_time(self):
        self.assertEqual(fg.cooldown_remaining("p"), 0.0)
        fg.guard("p")          # must not raise

    def test_trip_blocks_and_reports_remaining(self):
        fg.trip("p", 30, "HTTP 429")
        self.assertGreater(fg.cooldown_remaining("p"), 25)
        with self.assertRaises(fg.CooldownActive) as ctx:
            fg.guard("p")
        self.assertEqual(ctx.exception.provider, "p")
        self.assertEqual(ctx.exception.reason, "HTTP 429")

    def test_trip_never_shortens_an_existing_window(self):
        """A later, smaller trip must not let traffic back through early."""
        fg.trip("p", 60, "HTTP 429")
        fg.trip("p", 1, "HTTP 500")
        self.assertGreater(fg.cooldown_remaining("p"), 55)

    def test_zero_duration_trip_is_a_no_op(self):
        fg.trip("p", 0, "nothing")
        self.assertEqual(fg.cooldown_remaining("p"), 0.0)

    def test_snapshot_only_lists_live_cooldowns(self):
        fg.trip("p", 30, "HTTP 429")
        self.assertIn("p", fg.snapshot())
        fg.reset("p")
        self.assertNotIn("p", fg.snapshot())

    def test_request_makes_no_call_while_cooling_down(self):
        """The point of the breaker: not spending a request to rediscover a 429."""
        with _Server() as s:
            fg.trip("q", 30, "test")
            with self.assertRaises(fg.CooldownActive):
                fg.request("q", s.url)
            self.assertEqual(s.srv.hits, 0)


class RetryTests(unittest.TestCase):
    def setUp(self):
        fg.reset("stub")

    def test_retries_then_trips_on_429(self):
        with _Server() as s:
            s.srv.final = 429
            with self.assertRaises(requests.HTTPError):
                fg.request("stub", s.url, retries=2)
            self.assertEqual(s.srv.hits, 3)                    # 1 try + 2 retries
            # 429 earns the long cool-down, not the short one.
            self.assertGreater(fg.cooldown_remaining("stub"),
                               fg.FETCH_ERROR_COOLDOWN_SECONDS)

    def test_recovers_without_tripping_when_a_retry_succeeds(self):
        with _Server() as s:
            s.srv.statuses = [503]
            s.srv.final = 200
            resp = fg.request("stub", s.url, retries=2)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(s.srv.hits, 2)
            self.assertEqual(fg.cooldown_remaining("stub"), 0.0)

    def test_404_is_an_answer_not_a_failure(self):
        """404 must not be retried and must not trip the breaker."""
        with _Server() as s:
            s.srv.final = 404
            resp = fg.request("stub", s.url, retries=2, raise_for_status=False)
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(s.srv.hits, 1)
            self.assertEqual(fg.cooldown_remaining("stub"), 0.0)

    def test_narrowed_retry_statuses_pass_503_straight_through(self):
        """sec13f._get_maybe depends on this: 503 means "no filing", not "retry"."""
        with _Server() as s:
            s.srv.final = 503
            resp = fg.request("stub", s.url, retries=2, raise_for_status=False,
                              retry_statuses={429, 500, 502, 504})
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(s.srv.hits, 1)
            self.assertEqual(fg.cooldown_remaining("stub"), 0.0)

    def test_raise_for_status_false_returns_the_error_response(self):
        with _Server() as s:
            s.srv.final = 500
            resp = fg.request("stub", s.url, retries=0, raise_for_status=False)
            self.assertEqual(resp.status_code, 500)

    def test_backoff_grows(self):
        a, b, c = fg._retry_delay(0), fg._retry_delay(1), fg._retry_delay(4)
        self.assertLess(a, c)
        self.assertLessEqual(a, b * 2)      # jitter is bounded, not unbounded


class FailureBackoffTests(unittest.TestCase):
    NAME = "unittest_selftest"

    def _path(self):
        return Path(__file__).parents[1] / "cache" / f"fetch_backoff_{self.NAME}.json"

    def setUp(self):
        self._path().unlink(missing_ok=True)
        self.b = fg.FailureBackoff(self.NAME, base_seconds=100, max_seconds=800,
                                   flush_interval=0)

    def tearDown(self):
        self._path().unlink(missing_ok=True)

    def test_unknown_key_is_ready(self):
        self.assertTrue(self.b.ready("AAPL"))

    def test_failures_double_then_cap(self):
        self.assertEqual(self.b.record_failure("X"), 100)
        self.assertEqual(self.b.record_failure("X"), 200)
        self.assertEqual(self.b.record_failure("X"), 400)
        self.assertEqual(self.b.record_failure("X"), 800)
        self.assertEqual(self.b.record_failure("X"), 800)      # capped

    def test_huge_attempt_count_does_not_explode(self):
        """The exponent is clamped before shifting, not after."""
        self.b._state["X"] = {"count": 5000.0, "retry_after": 0.0}
        self.assertEqual(self.b.record_failure("X"), 800)

    def test_failure_blocks_and_success_clears(self):
        self.b.record_failure("DEAD")
        self.assertFalse(self.b.ready("DEAD"))
        self.b.record_success("DEAD")
        self.assertTrue(self.b.ready("DEAD"))

    def test_filter_ready_drops_only_backed_off_keys(self):
        self.b.record_failure("DEAD")
        self.assertEqual(
            self.b.filter_ready(["AAPL", "DEAD", "MSFT"], log_skipped=False),
            ["AAPL", "MSFT"],
        )

    def test_filter_ready_preserves_order(self):
        self.assertEqual(
            self.b.filter_ready(["C", "A", "B"], log_skipped=False), ["C", "A", "B"]
        )

    def test_state_survives_a_restart(self):
        """The whole reason this is on disk: a deploy must not forget."""
        self.b.record_failure("ZOMBIE")
        self.b.flush()
        reloaded = fg.FailureBackoff(self.NAME, base_seconds=100, max_seconds=800)
        self.assertFalse(reloaded.ready("ZOMBIE"))

    def test_expired_entries_are_pruned_on_load(self):
        """Otherwise the file grows forever with symbols that failed once."""
        self.b._state["OLD"] = {"count": 1.0, "retry_after": time.time() - 10}
        self.b._dirty = True
        self.b.flush()
        reloaded = fg.FailureBackoff(self.NAME)
        self.assertEqual(reloaded.snapshot(), {})

    def test_record_batch_splits_success_and_failure(self):
        self.b.record_batch(["A", "B", "C"], ["A", "C"])
        self.assertTrue(self.b.ready("A"))
        self.assertFalse(self.b.ready("B"))
        self.assertTrue(self.b.ready("C"))

    def test_snapshot_reports_attempts_and_remaining(self):
        self.b.record_failure("X")
        snap = self.b.snapshot()
        self.assertEqual(snap["X"]["attempts"], 1.0)
        self.assertGreater(snap["X"]["remaining_seconds"], 90)

    def test_unreadable_state_file_does_not_raise(self):
        self._path().write_text("this is not json")
        self.assertEqual(fg.FailureBackoff(self.NAME).snapshot(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
