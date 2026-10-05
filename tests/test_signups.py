"""The owner hears about each first sign-in exactly once (ystocker.signups).

Asked for 2026-10-05: "an email notification to admin@li-family.us for anyone
who signed up". yStocker has no sign-up form, so a sign-up is an address's first
completed Google sign-in, remembered in ``ystocker-users``. These pin:

* the claim -- the first sign-in mails, every later one only updates the row,
  and an unreachable table mails nobody rather than everybody;
* the seed -- every address the site already holds is written before the
  marker that switches mails on, so nobody who was here first is announced;
* the switches -- the kill switch, a missing sender, the daily cap, the
  recipient override, and one retry of a failed send;
* the mail -- reader text escaped, the subject one line, branded per site.

No app, no network, no AWS, no SES: the table is in memory and the send is a mock.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import (agents, credits, portfolio, quota, report_email,  # noqa: E402
                      research_store, share, signups)


class _CondFailed(Exception):
    """What boto3 raises for a failed ConditionExpression (matched by name)."""


_CondFailed.__name__ = "ConditionalCheckFailedException"


class _NotFound(Exception):
    pass


_NotFound.__name__ = "ResourceNotFoundException"


def _resolve(expr: str, names: dict | None) -> str:
    for placeholder, real in (names or {}).items():
        expr = expr.replace(placeholder, real)
    return expr


class FakeTable:
    """ystocker-users in memory: the conditional put, the three update shapes
    signups.py writes, the marker read and the COUNT scan."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.fail_get: Exception | None = None

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None):
        cond = _resolve(ConditionExpression or "", ExpressionAttributeNames)
        if "attribute_not_exists(email)" in cond and Item["email"] in self.items:
            raise _CondFailed("The conditional request failed")
        self.items[Item["email"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        names, values = ExpressionAttributeNames, ExpressionAttributeValues
        item = self.items.setdefault(Key["email"], {"email": Key["email"]})
        expr, _, add = UpdateExpression.partition(" ADD ")
        assert expr.startswith("SET "), UpdateExpression
        for part in re.split(r",\s*(?![^()]*\))", expr[4:]):
            target, value = (s.strip() for s in part.split(" = ", 1))
            keep = re.fullmatch(r"if_not_exists\((#\w+),\s*(:\w+)\)", value)
            if keep:
                item.setdefault(names[keep.group(1)], values[keep.group(2)])
            else:
                item[names[target]] = values[value]
        if add:
            target, value = add.split()
            item[names[target]] = item.get(names[target], 0) + values[value]

    def get_item(self, Key, ConsistentRead=False):
        if self.fail_get:
            raise self.fail_get
        row = self.items.get(Key["email"])
        return {"Item": dict(row)} if row else {}

    def scan(self, Select=None, FilterExpression=None, ExclusiveStartKey=None):
        assert Select == "COUNT"
        return {"Count": sum(1 for key in self.items if "@" in key)}


FIELDS = {"name": "Alice Example", "site": "trade-agents.com",
          "page": "/login?next=/agents", "lang": "zh"}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.sent: list[tuple] = []
        self.send_ok: list[bool] = []
        self.enterContext(mock.patch.object(signups, "_get_table", lambda: self.table))
        self.enterContext(mock.patch.object(signups, "_seeded", False))
        self.enterContext(mock.patch.object(signups, "RETRY_SECONDS", 0))
        self.enterContext(mock.patch.object(report_email, "_ses_send", self._send))
        self.enterContext(mock.patch.object(quota, "try_consume_signup_notice", lambda: True))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SIGNUP_NOTIFY", "AGENTS_VIP", "AGENTS_ALLOWED"))}
        env["SES_FROM_EMAIL"] = "TradeAgents <noreply@example.com>"
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def _send(self, to_addr, subject, html, text, what=""):
        self.sent.append((to_addr, subject, html, text))
        return self.send_ok.pop(0) if self.send_ok else True

    def mark_seeded(self) -> None:
        self.table.items[signups.SEED_KEY] = {"email": signups.SEED_KEY}


class FirstSignInTests(_Case):

    def test_the_first_sign_in_is_mailed_once_and_later_ones_only_counted(self):
        self.mark_seeded()
        self.assertEqual(signups._record("new@example.com", FIELDS, now="2026-10-05T14:03:00+00:00"),
                         "sent")
        self.assertEqual(len(self.sent), 1)
        to_addr, subject, _html, text = self.sent[0]
        self.assertEqual(to_addr, "admin@li-family.us")
        self.assertIn("new@example.com", subject)
        self.assertIn("/login?next=/agents", text)
        row = self.table.items["new@example.com"]
        self.assertEqual((row["sign_ins"], row["site"], row["lang"], row["source"]),
                         (1, "trade-agents.com", "zh", "sign-in"))
        self.assertIn("notified_at", row)

        # Again, as the other gunicorn worker or a week later: no second mail.
        self.assertEqual(signups._record("new@example.com", FIELDS, now="2026-10-12T09:00:00+00:00"),
                         "returning")
        self.assertEqual(len(self.sent), 1)
        row = self.table.items["new@example.com"]
        self.assertEqual(row["sign_ins"], 2)
        self.assertEqual(row["last_seen"], "2026-10-12T09:00:00+00:00")
        self.assertEqual(row["first_seen"], "2026-10-05T14:03:00+00:00")

    def test_an_unreachable_table_mails_nobody(self):
        # Nobody can tell new from returning without it; "new" would mail on
        # every sign-in for as long as the outage lasted.
        with mock.patch.object(signups, "_get_table", lambda: None):
            self.assertEqual(signups._record("new@example.com", FIELDS), "unavailable")
        self.assertEqual(self.sent, [])

    def test_before_the_seed_a_first_sign_in_is_recorded_but_not_mailed(self):
        self.assertEqual(signups._record("early@example.com", FIELDS), "unseeded")
        self.assertIn("early@example.com", self.table.items)
        self.mark_seeded()
        self.assertEqual(signups._record("early@example.com", FIELDS), "returning")
        self.assertEqual(self.sent, [])

    def test_an_unreadable_marker_means_not_yet(self):
        self.mark_seeded()
        self.table.fail_get = RuntimeError("throttled")
        self.assertEqual(signups._record("new@example.com", FIELDS), "unseeded")
        self.assertEqual(self.sent, [])

    def test_the_kill_switch_and_a_missing_sender_silence_the_mail_not_the_record(self):
        self.mark_seeded()
        with mock.patch.dict(os.environ, {"SIGNUP_NOTIFY": "0"}):
            self.assertEqual(signups._record("a@example.com", FIELDS), "disabled")
        del os.environ["SES_FROM_EMAIL"]
        self.assertEqual(signups._record("b@example.com", FIELDS), "disabled")
        self.assertEqual(self.sent, [])
        self.assertIn("a@example.com", self.table.items)
        self.assertIn("b@example.com", self.table.items)

    def test_past_the_daily_cap_nothing_is_mailed(self):
        self.mark_seeded()
        with mock.patch.object(quota, "try_consume_signup_notice", lambda: False):
            self.assertEqual(signups._record("new@example.com", FIELDS), "capped")
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_tried_once_more(self):
        self.mark_seeded()
        self.send_ok[:] = [False, True]
        self.assertEqual(signups._record("a@example.com", FIELDS), "sent")
        self.assertEqual(len(self.sent), 2)
        self.send_ok[:] = [False, False]
        self.assertEqual(signups._record("b@example.com", FIELDS), "send_failed")
        self.assertEqual(len(self.sent), 4)
        self.assertIn("notify_failed_at", self.table.items["b@example.com"])

    def test_the_recipient_can_be_moved(self):
        self.mark_seeded()
        with mock.patch.dict(os.environ, {"SIGNUP_NOTIFY_EMAIL": "ops@example.com"}):
            signups._record("new@example.com", FIELDS)
        self.assertEqual(self.sent[0][0], "ops@example.com")

    def test_the_count_is_of_addresses_not_the_marker(self):
        self.mark_seeded()
        self.table.items["old@example.com"] = {"email": "old@example.com"}
        signups._record("new@example.com", FIELDS)
        self.assertIn("Accounts on record: 2", self.sent[0][3])

    def test_a_returning_seeded_row_gains_the_name_it_never_had(self):
        self.table.items["old@example.com"] = {"email": "old@example.com", "source": "runs"}
        signups._record("old@example.com", FIELDS)
        self.assertEqual(self.table.items["old@example.com"]["name"], "Alice Example")
        signups._record("old@example.com", dict(FIELDS, name="Renamed"))
        self.assertEqual(self.table.items["old@example.com"]["name"], "Alice Example")


class RecordSignInTests(_Case):

    def test_it_never_raises(self):
        def boom():
            raise RuntimeError("no network")
        with mock.patch.object(signups, "_get_table", boom), \
             self.assertLogs("ystocker.signups", "ERROR"):
            self.assertIsNone(signups.record_sign_in("a@example.com", background=False))

    def test_it_ignores_what_is_not_an_address(self):
        signups.record_sign_in("not an address", background=False)
        signups.record_sign_in("", background=False)
        self.assertEqual(self.table.items, {})

    def test_it_cleans_what_the_reader_sent(self):
        self.mark_seeded()
        signups.record_sign_in(" New@Example.com ", "Alice\r\nBcc: x@y.z",
                               host="Trade-Agents.com:443", page="/home\x00",
                               lang="zh-CN", background=False)
        row = self.table.items["new@example.com"]
        self.assertEqual(row["name"], "AliceBcc: x@y.z")
        self.assertEqual((row["site"], row["page"], row["lang"]),
                         ("trade-agents.com", "/home", "zh"))


def _scanner(tables: dict[str, list[dict]], fail: str = ""):
    def scan(name, **_kw):
        if name == fail:
            raise RuntimeError("ProvisionedThroughputExceededException")
        return [dict(i) for i in tables.get(name, [])]
    return scan


SOURCES = {
    agents.JOBS_TABLE_NAME: [
        {"user": "Runner@Example.com", "created_at": "2026-09-02T10:00:00+00:00"},
        {"user": "runner@example.com", "created_at": "2026-08-30T08:00:00+00:00"},
        {"user": ""},
    ],
    portfolio.TABLE_NAME: [{"email": "holder@example.com"}],
    research_store.TABLE_NAME: [{"sk": "reader@example.com#20261001T120000000000Z-0123456789abcdef"}],
    credits.TABLE_NAME: [{"id": "u#buyer@example.com"}, {"id": "sub#pro@example.com"},
                         {"id": "s#cs_test_1"}, {"id": "cus#cus_1"}],
    share.TABLE_NAME: [{"owner": "sharer@example.com", "sharer": "sharer@example.com"}],
}


class SeedTests(_Case):

    def setUp(self) -> None:
        super().setUp()
        os.environ["AGENTS_VIP_EMAILS"] = "owner@example.com"
        os.environ["AGENTS_ALLOWED_EMAILS"] = "family@example.com"

    def test_every_table_that_holds_addresses_is_read(self):
        found = signups.known_addresses(_scanner(SOURCES))
        self.assertEqual(set(found), {
            "runner@example.com", "holder@example.com", "reader@example.com",
            "buyer@example.com", "pro@example.com", "sharer@example.com",
            "owner@example.com", "family@example.com"})
        self.assertEqual(found["runner@example.com"]["first_seen"], "2026-08-30T08:00:00+00:00")

    def test_rows_are_written_before_the_marker_and_existing_rows_are_kept(self):
        self.table.items["holder@example.com"] = {"email": "holder@example.com", "name": "Kept"}
        wrote = signups.seed(self.table, _scanner(SOURCES), now="2026-10-05T00:00:00+00:00")
        self.assertEqual(wrote, 7)
        self.assertEqual(self.table.items["holder@example.com"], {"email": "holder@example.com",
                                                                   "name": "Kept"})
        self.assertEqual(self.table.items["runner@example.com"]["source"], "runs")
        marker = self.table.items[signups.SEED_KEY]
        self.assertEqual((marker["addresses"], marker["written"]), (8, 7))

        # From now on: the known are not announced, the new are.
        self.assertEqual(signups._record("runner@example.com", FIELDS), "returning")
        self.assertEqual(signups._record("stranger@example.com", FIELDS), "sent")
        self.assertEqual([s[1] for s in self.sent],
                         ["New sign-up on TradeAgents: Alice Example (stranger@example.com)"])

    def test_a_second_seed_changes_nothing(self):
        signups.seed(self.table, _scanner(SOURCES), now="2026-10-05T00:00:00+00:00")
        before = {k: dict(v) for k, v in self.table.items.items()}
        self.assertEqual(signups.seed(self.table, _scanner(SOURCES), now="2026-10-06T00:00:00+00:00"), 0)
        self.assertEqual(self.table.items, before)

    def test_a_source_that_fails_leaves_mails_off(self):
        with self.assertRaises(RuntimeError):
            signups.seed(self.table, _scanner(SOURCES, fail=credits.TABLE_NAME))
        self.assertNotIn(signups.SEED_KEY, self.table.items)
        self.assertFalse(signups._is_seeded(self.table))

    def test_a_missing_source_table_holds_no_addresses_and_anything_else_raises(self):
        class Table:
            def __init__(self, exc): self.exc = exc
            def scan(self, **_kw): raise self.exc

        class Resource:
            def __init__(self, exc): self.exc = exc
            def Table(self, _name): return Table(self.exc)      # noqa: N802 - boto3's name

        with mock.patch("boto3.resource", lambda *a, **k: Resource(_NotFound("gone"))):
            self.assertEqual(signups._scan("ystocker-agent-shares"), [])
        with mock.patch("boto3.resource", lambda *a, **k: Resource(RuntimeError("throttled"))):
            with self.assertRaises(RuntimeError):
                signups._scan("ystocker-agent-shares")


class NoticeTests(unittest.TestCase):

    ROW = {"email": "new@example.com", "name": "Alice Example", "site": "trade-agents.com",
           "page": "/login?next=/agents", "lang": "zh", "first_seen": "2026-10-05T14:03:00+00:00"}

    def test_reader_text_is_escaped(self):
        row = dict(self.ROW, name='<img src=x onerror="alert(1)">', page="/home?<script>")
        _subject, html, _text = signups.build_notice(row, 3)
        live = re.sub(r"&lt;.*?&gt;", "", html)
        self.assertNotIn("<img", live)
        self.assertNotIn("<script", live)
        self.assertIn("&lt;img", html)

    def test_the_subject_is_one_line(self):
        subject, _html, _text = signups.build_notice(dict(self.ROW, name="Eve\r\nBcc: x@y.z"), 3)
        self.assertNotRegex(subject, r"[\r\n]")

    def test_the_brand_follows_the_site(self):
        self.assertTrue(signups.build_notice(self.ROW, 3)[0]
                        .startswith("New sign-up on TradeAgents: Alice Example"))
        self.assertTrue(signups.build_notice(dict(self.ROW, site="stock.li-family.us"), 3)[0]
                        .startswith("New sign-up on yStocker"))

    def test_the_facts_are_all_there(self):
        _subject, html, text = signups.build_notice(self.ROW, 14)
        for fact in ("Name: Alice Example", "Email: new@example.com", "Site: trade-agents.com",
                     "Signed in from: /login?next=/agents", "Language: Chinese (中文)",
                     "When: 2026-10-05 14:03 UTC · 7:03 AM PDT", "Accounts on record: 14"):
            self.assertIn(fact, text)
        self.assertIn('href="mailto:new@example.com"', html)

    def test_without_a_count_or_name_the_mail_still_reads(self):
        row = {"email": "new@example.com", "first_seen": "2026-01-05T14:03:00+00:00"}
        subject, _html, text = signups.build_notice(row, None)
        self.assertEqual(subject, "New sign-up on yStocker: new@example.com")
        self.assertIn("When: 2026-01-05 14:03 UTC · 6:03 AM PST", text)
        self.assertNotIn("Accounts on record", text)


class HelperTests(unittest.TestCase):

    def test_page_of_takes_only_our_own_referer(self):
        host = "trade-agents.com"
        self.assertEqual(signups.page_of("https://trade-agents.com/login?next=%2Fagents", host),
                         "/login?next=/agents")
        self.assertEqual(signups.page_of("https://evil.example/login", host), "")
        self.assertEqual(signups.page_of(None, host), "")
        self.assertEqual(signups.page_of("https://trade-agents.com/a%0d%0ab", host), "/ab")
        self.assertEqual(len(signups.page_of("https://trade-agents.com/" + "x" * 500, host)), 200)

    def test_small_normalisers(self):
        self.assertEqual(signups.site_of("Stock.Li-Family.us:8000"), "stock.li-family.us")
        self.assertEqual([signups.lang_of(v) for v in ("zh-CN", "en", "fr", None)],
                         ["zh", "en", "", ""])
        self.assertEqual(signups.normalise(" A@B.com "), "a@b.com")
        self.assertIsNone(signups.normalise("a b@c.com"))
        self.assertIsNone(signups.normalise(signups.SEED_KEY))

    def test_the_default_recipient_is_the_contact_address(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(signups.recipient(), "admin@li-family.us")


class CapTests(unittest.TestCase):

    def test_the_daily_cap_is_counted_under_the_quota_lock(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.object(quota, "QUOTA_DIR", root), \
             mock.patch.object(quota, "_LOCK_PATH", root / "quota.lock"), \
             mock.patch.dict(os.environ, {"SIGNUP_NOTIFY_DAILY_LIMIT": "2"}):
            self.assertEqual([quota.try_consume_signup_notice() for _ in range(3)],
                             [True, True, False])


if __name__ == "__main__":
    unittest.main()
