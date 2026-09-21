"""
Tests for the external-post inbox (``ystocker.inbox``).

No app, no network, no AWS — the validation and the token check are pure, which
is deliberate: this is the first write endpoint on this box that is not behind a
Google session, so the rules that decide what gets stored are exactly the part
that has to be provable without standing anything up.

What is worth testing here is not "does it keep a string". It is the four ways
an open write door goes wrong:

* **Failing open.** With no token configured the endpoint must refuse, not
  accept. Treating "unset" as "unchecked" is the single mistake that turns a
  missing SSM parameter into an open relay for anyone who finds the path — and
  the access log shows the internet finds paths unprompted.
* **Timing.** The token is a bearer credential with nothing behind it, so the
  comparison is constant-time and stays that way.
* **Unbounded input.** Every field is capped before storage, not at render, or
  one request can fill a table and a page.
* **Silent loss.** A sender's unknown fields are kept, not dropped, and a
  message with nothing in it is refused rather than stored as an empty card —
  in both cases so the sender can tell what happened.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from ystocker import inbox


class TokenTests(unittest.TestCase):

    def setUp(self):
        self._old = inbox.os.environ.get("INBOX_TOKEN")

    def tearDown(self):
        if self._old is None:
            inbox.os.environ.pop("INBOX_TOKEN", None)
        else:
            inbox.os.environ["INBOX_TOKEN"] = self._old

    def test_no_token_configured_refuses_everything(self):
        """The door is shut, not open. This is the whole security posture in one
        assertion: "unset" must never read as "unchecked"."""
        inbox.os.environ.pop("INBOX_TOKEN", None)
        self.assertFalse(inbox.token_configured())
        self.assertFalse(inbox.check_token("anything"))
        self.assertFalse(inbox.check_token(""))
        self.assertFalse(inbox.check_token(None))

    def test_a_matching_token_passes_and_others_do_not(self):
        inbox.os.environ["INBOX_TOKEN"] = "s3cret-value"
        self.assertTrue(inbox.check_token("s3cret-value"))
        self.assertTrue(inbox.check_token("  s3cret-value  "))   # trimmed
        self.assertFalse(inbox.check_token("s3cret-valuf"))
        self.assertFalse(inbox.check_token("s3cret"))            # prefix
        self.assertFalse(inbox.check_token("s3cret-value-more")) # extension
        self.assertFalse(inbox.check_token(None))

    def test_the_comparison_is_constant_time(self):
        """Read from the source rather than timed, because a timing assertion is
        flaky and this only has to stay `compare_digest` rather than `==`."""
        import inspect

        src = inspect.getsource(inbox.check_token)
        self.assertIn("compare_digest", src)
        self.assertNotIn("presented == expected", src)

    def test_both_header_spellings_are_accepted(self):
        """The senders are scripts, not browsers: a shell reaching for a plain
        custom header should not have to learn the Bearer convention."""
        self.assertEqual(
            inbox.token_from_headers({"Authorization": "Bearer abc123"}), "abc123")
        self.assertEqual(
            inbox.token_from_headers({"Authorization": "bearer abc123"}), "abc123")
        self.assertEqual(
            inbox.token_from_headers({"X-Inbox-Token": "abc123"}), "abc123")
        self.assertEqual(inbox.token_from_headers({}), "")
        # A non-Bearer Authorization must not be read as a token.
        self.assertEqual(
            inbox.token_from_headers({"Authorization": "Basic abc123"}), "")


class NormaliseTests(unittest.TestCase):

    NOW = datetime(2026, 9, 21, 14, 30, 5, tzinfo=timezone.utc)

    def norm(self, body):
        return inbox.normalise(body, now=self.NOW)

    def test_a_minimal_message_is_accepted(self):
        out = self.norm({"text": "hello"})
        self.assertEqual(out["text"], "hello")
        self.assertEqual(out["level"], "info")
        self.assertEqual(out["bucket"], "2026-09")
        self.assertTrue(out["sk"].startswith("2026-09-21T14:30:05"))
        self.assertIn("#", out["sk"])

    def test_a_message_with_neither_title_nor_text_is_refused(self):
        """An empty card on the page is indistinguishable from a bug in the
        sender, and only a refusal tells them."""
        with self.assertRaises(inbox.InboxError) as ctx:
            self.norm({"source": "cron"})
        self.assertEqual(ctx.exception.reason, "empty")

    def test_a_non_object_body_is_refused(self):
        for body in ([1, 2], "text", 7, None):
            with self.assertRaises(inbox.InboxError) as ctx:
                self.norm(body)
            self.assertEqual(ctx.exception.reason, "not_an_object")

    def test_common_aliases_for_the_body_field_are_accepted(self):
        for key in ("text", "message", "body"):
            self.assertEqual(self.norm({key: "hi"})["text"], "hi")

    def test_every_field_is_capped_before_storage(self):
        out = self.norm({
            "title": "T" * 5_000,
            "text": "X" * 50_000,
            "source": "s" * 500,
            "ticker": "n" * 100,
            "tags": [f"tag{i}" for i in range(50)],
        })
        self.assertEqual(len(out["title"]), inbox.MAX_TITLE)
        self.assertEqual(len(out["text"]), inbox.MAX_TEXT)
        self.assertEqual(len(out["source"]), inbox.MAX_SOURCE)
        self.assertEqual(len(out["ticker"]), inbox.MAX_TICKER)
        self.assertEqual(len(out["tags"]), inbox.MAX_TAGS)

    def test_an_unknown_level_falls_back_rather_than_refusing(self):
        """A sender inventing a level should not lose the message."""
        self.assertEqual(self.norm({"text": "x", "level": "banana"})["level"], "info")
        self.assertEqual(self.norm({"text": "x", "level": "ERROR"})["level"], "error")

    def test_a_non_http_url_is_refused_not_dropped(self):
        """Dropping it leaves a message whose point was the link, with nothing
        saying so. `javascript:` is the reason this is a refusal at write time
        rather than escaping at render time — escaping leaves it intact."""
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://h/f", "/relative"):
            with self.assertRaises(inbox.InboxError) as ctx:
                self.norm({"text": "x", "url": bad})
            self.assertEqual(ctx.exception.reason, "bad_url")
        self.assertEqual(self.norm({"text": "x", "url": "https://a.test/p"})["url"],
                         "https://a.test/p")

    def test_unknown_fields_are_kept_verbatim(self):
        """A sender adding a field must not have to coordinate with this file,
        and a message stored minus the part that mattered is silent data loss."""
        out = self.norm({"text": "x", "pnl": -12.5, "strategy": {"name": "mr"}})
        self.assertEqual(out["data"]["pnl"], -12.5)
        self.assertEqual(out["data"]["strategy"], {"name": "mr"})

    def test_an_explicit_data_object_merges_over_the_loose_fields(self):
        out = self.norm({"text": "x", "k": 1, "data": {"k": 2, "j": 3}})
        self.assertEqual(out["data"], {"k": 2, "j": 3})

    def test_an_oversized_data_object_is_refused(self):
        with self.assertRaises(inbox.InboxError) as ctx:
            self.norm({"text": "x", "blob": "y" * (inbox.MAX_DATA_BYTES + 100)})
        self.assertEqual(ctx.exception.reason, "data_too_large")

    def test_an_exotic_type_is_coerced_rather_than_refused(self):
        """`default=str` is lenient on purpose. Over HTTP this never fires —
        the body came through `json.loads`, so it is JSON-native by
        construction — and for an internal caller, stringifying an odd value is
        a better outcome than losing the message around it."""
        out = self.norm({"text": "x", "data": {"s": {1, 2}}})
        self.assertIsInstance(out["data"]["s"], (set, str))
        self.assertTrue(json.dumps(out["data"], default=str))

    def test_a_circular_payload_is_refused_with_a_reason(self):
        """The one shape `default=str` cannot rescue. Refused rather than
        allowed to raise out of `put` later, where it would be a 500."""
        loop: dict = {"text": "x"}
        loop["self"] = loop
        with self.assertRaises(inbox.InboxError) as ctx:
            self.norm(loop)
        self.assertEqual(ctx.exception.reason, "unserialisable")

    def test_tags_accept_a_string_as_well_as_a_list(self):
        self.assertEqual(self.norm({"text": "x", "tags": "a, b c"})["tags"],
                         ["a", "b", "c"])

    def test_duplicate_tags_collapse(self):
        self.assertEqual(self.norm({"text": "x", "tags": ["a", "a", "b"]})["tags"],
                         ["a", "b"])

    def test_the_ttl_is_set_and_is_in_the_future(self):
        out = self.norm({"text": "x"})
        self.assertGreater(out["expires_at"], int(self.NOW.timestamp()))
        self.assertEqual(out["expires_at"],
                         int(self.NOW.timestamp()) + inbox.RETENTION_DAYS * 86400)

    def test_ids_do_not_collide(self):
        ids = {self.norm({"text": "x"})["id"] for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_the_sort_key_orders_by_time(self):
        """`recent` reads newest-first off this key, so lexical order has to
        match chronological order."""
        early = inbox.normalise({"text": "a"},
                                now=datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc))
        late = inbox.normalise({"text": "b"},
                               now=datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc))
        self.assertLess(early["sk"], late["sk"])

    def test_a_ticker_is_upper_cased(self):
        self.assertEqual(self.norm({"text": "x", "ticker": "nvda"})["ticker"], "NVDA")


class BucketTests(unittest.TestCase):

    def test_buckets_walk_backwards_by_month(self):
        out = inbox._buckets(datetime(2026, 1, 15, tzinfo=timezone.utc))
        self.assertEqual(out[:3], ["2026-01", "2025-12", "2025-11"])
        self.assertEqual(len(out), inbox.MAX_BUCKETS)

    def test_the_walk_is_bounded(self):
        """Without a bound, a table that has been quiet for years would cost one
        Query per month on every page load."""
        self.assertEqual(len(inbox._buckets()), inbox.MAX_BUCKETS)


class ThawTests(unittest.TestCase):

    def test_stored_json_round_trips(self):
        out = inbox._thaw({"id": "a", "data": json.dumps({"k": 1})})
        self.assertEqual(out["data"], {"k": 1})

    def test_an_unparsable_payload_is_surfaced_not_dropped(self):
        """The failure would be ours, and dropping the message hides it."""
        out = inbox._thaw({"id": "a", "data": "{not json"})
        self.assertEqual(out["data"], {"_unparsed": "{not json"})

    def test_the_ttl_column_is_not_leaked_to_the_page(self):
        self.assertNotIn("expires_at", inbox._thaw({"id": "a", "expires_at": 1}))


class SourcesTests(unittest.TestCase):

    def test_distinct_sources_are_derived_from_the_rows_in_hand(self):
        rows = [{"source": "cron"}, {"source": "bot"}, {"source": "cron"}, {}]
        self.assertEqual(inbox.sources(rows), ["bot", "cron"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
