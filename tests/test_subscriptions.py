"""TradeAgents Pro's state: who is entitled, what is stored, and the handoff.

No app, no network, no AWS, no Stripe: the credits table is an in-memory fake
that evaluates the one condition ``save`` relies on, and Stripe subscriptions are
the plain dicts webhooks deliver -- in both of the shapes Stripe sends.

What this exists to catch, none of which raises:

* **A lapsed subscriber still reading, or a paying one walled.** ``entitled``
  decides from the stored row alone, with a day of slack for a renewal not yet
  reported and a week of grace while Stripe retries a card.
* **A late webhook undoing a newer account.** ``save`` only replaces a row with
  one at least as recent, and a trial once taken stays taken.
* **A second free trial, or a second subscription, from a failed read.**
  ``load`` raises rather than answering "none", and the trial check fails closed.
* **A forged billing link.** The handoff is signed with YSTOCKER_SECRET_KEY and
  nothing else; with no key it is refused, never signed with a known one.
* **The wrong number of runs.** A subscriber gets ten a day, never fewer than
  the free allowance; a VIP keeps the VIP limit.
"""
from __future__ import annotations

import os
import time
import unittest
from decimal import Decimal
from unittest import mock

from ystocker import credits, quota, subscriptions as subs

NOW = 1_800_000_000.0
DAY = 24 * 3600


class _CondFailed(Exception):
    """What boto3 raises for a failed ConditionExpression (by name)."""


_CondFailed.__name__ = "ConditionalCheckFailedException"


class FakeTable:
    """The credits table, as far as subscriptions reaches into it."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.fail = False

    def get_item(self, Key, ConsistentRead=False):
        if self.fail:
            raise RuntimeError("ProvisionedThroughputExceeded")
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues):
        current = self.items.get(Key["id"], {})
        incoming = ExpressionAttributeValues[":c"]
        if "event_created" in current and current["event_created"] > incoming:
            raise _CondFailed("The conditional request failed")
        item = dict(current, id=Key["id"])
        for part in UpdateExpression[len("SET "):].split(", "):
            name, value = (s.strip() for s in part.split(" = "))
            name = ExpressionAttributeNames.get(name, name)
            val = ExpressionAttributeValues[value]
            item[name] = Decimal(val) if isinstance(val, int) and not isinstance(val, bool) else val
        self.items[Key["id"]] = item

    def scan(self, **kw):
        rows = [dict(v) for k, v in self.items.items() if k.startswith("sub#")]
        return {"Items": rows}


class _TableCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.enterContext(mock.patch.object(subs, "_table", lambda: self.table))
        subs._cache.clear()
        self.addCleanup(subs._cache.clear)


def _sub(**over):
    """A Stripe subscription as the API's current version sends it: the period
    on the item, not the subscription."""
    sub = {
        "id": "sub_123", "object": "subscription", "status": "trialing",
        "customer": "cus_abc", "cancel_at_period_end": False,
        "trial_end": int(NOW + 7 * DAY),
        "metadata": {"email": "Reader@Example.com", "plan": "year"},
        "items": {"data": [{"current_period_end": int(NOW + 7 * DAY),
                            "price": {"id": "price_y", "recurring": {"interval": "year"}}}]},
    }
    sub.update(over)
    return sub


class EntitlementTests(unittest.TestCase):
    def test_active_and_trialing_hold_to_the_period_end_plus_a_day(self):
        for status in ("active", "trialing"):
            row = {"status": status, "period_end": NOW}
            self.assertTrue(subs.entitled(row, now=NOW - 1), status)
            self.assertTrue(subs.entitled(row, now=NOW + DAY - 1), status)
            self.assertFalse(subs.entitled(row, now=NOW + DAY + 1), status)

    def test_a_failing_card_holds_for_a_week(self):
        row = {"status": "past_due", "period_end": NOW}
        self.assertTrue(subs.entitled(row, now=NOW + 6 * DAY))
        self.assertFalse(subs.entitled(row, now=NOW + 8 * DAY))

    def test_everything_else_is_nothing(self):
        for status in ("canceled", "unpaid", "incomplete", "incomplete_expired", "paused", ""):
            self.assertFalse(subs.entitled({"status": status, "period_end": NOW + 99 * DAY}, now=NOW))
        self.assertFalse(subs.entitled(None, now=NOW))
        self.assertFalse(subs.entitled({}, now=NOW))


class RowTests(unittest.TestCase):
    def test_the_current_api_shape(self):
        row = subs.row_from_subscription(_sub(), "Reader@Example.com", event_created=NOW)
        self.assertEqual(row["email"], "reader@example.com")
        self.assertEqual(row["plan"], "year")
        self.assertEqual(row["period_end"], int(NOW + 7 * DAY))
        self.assertTrue(row["trial_used"])
        self.assertEqual(row["customer"], "cus_abc")
        self.assertEqual(row["event_created"], int(NOW))

    def test_the_older_shape_webhooks_arrive_in(self):
        # The account's default API version keeps the period on the subscription.
        old = _sub(current_period_end=int(NOW + 30 * DAY), trial_end=None, status="active")
        old["items"]["data"][0].pop("current_period_end")
        old["items"]["data"][0]["price"]["recurring"]["interval"] = "month"
        row = subs.row_from_subscription(old, "a@b.co")
        self.assertEqual(row["period_end"], int(NOW + 30 * DAY))
        self.assertEqual(row["plan"], "month")
        self.assertFalse(row["trial_used"])

    def test_an_expanded_customer_and_a_plan_from_metadata(self):
        sub = _sub(customer={"id": "cus_x", "object": "customer"})
        sub["items"]["data"][0]["price"] = {}
        row = subs.row_from_subscription(sub, "a@b.co")
        self.assertEqual(row["customer"], "cus_x")
        self.assertEqual(row["plan"], "year")


class OfferTests(unittest.TestCase):
    def test_the_default_offer(self):
        offer = subs.offer()
        self.assertEqual((offer["month"], offer["year"]), (29.0, 290.0))
        self.assertEqual(offer["saving_pct"], 17)
        self.assertEqual(offer["months_free"], 2)
        self.assertEqual(offer["year_per_month"], 24.17)
        self.assertEqual((offer["trial_days"], offer["runs_per_day"]), (7, 10))

    def test_months_free_only_when_whole(self):
        with mock.patch.dict(subs.PLANS, {"month": dict(subs.PLANS["month"], price=30.0),
                                          "year": dict(subs.PLANS["year"], price=300.0)}):
            self.assertEqual(subs.months_free(), 2)
        with mock.patch.dict(subs.PLANS, {"month": dict(subs.PLANS["month"], price=29.0),
                                          "year": dict(subs.PLANS["year"], price=299.0)}):
            self.assertEqual(subs.months_free(), 0)

    def test_a_plan_is_looked_up_never_trusted(self):
        self.assertIsNotNone(subs.plan("YEAR "))
        self.assertIsNone(subs.plan("lifetime"))
        self.assertIsNone(subs.plan(""))


class StoreTests(_TableCase):
    def test_save_then_status(self):
        self.assertTrue(subs.save(subs.row_from_subscription(_sub(), "reader@example.com", NOW)))
        row = subs.status("READER@example.com")
        self.assertEqual(row["status"], "trialing")
        self.assertIsInstance(row["period_end"], int)          # not a Decimal
        self.assertEqual(self.table.items["cus#cus_abc"]["email"], "reader@example.com")
        self.assertEqual(subs.email_for_customer("cus_abc"), "reader@example.com")

    def test_a_late_event_does_not_undo_a_newer_one(self):
        subs.save(subs.row_from_subscription(_sub(status="active"), "r@x.co", NOW + 100))
        self.assertFalse(subs.save(subs.row_from_subscription(_sub(status="trialing"), "r@x.co", NOW)))
        self.assertEqual(subs.status("r@x.co", fresh=True)["status"], "active")
        # The same moment is not "older": a retried delivery is written again.
        self.assertTrue(subs.save(subs.row_from_subscription(_sub(status="canceled"), "r@x.co", NOW + 100)))

    def test_a_trial_once_taken_stays_taken(self):
        subs.save(subs.row_from_subscription(_sub(), "r@x.co", NOW))
        later = _sub(status="active", trial_end=None)
        subs.save(subs.row_from_subscription(later, "r@x.co", NOW + 10))
        self.assertTrue(subs.status("r@x.co", fresh=True)["trial_used"])
        self.assertFalse(subs.trial_available("r@x.co"))

    def test_status_is_cached_and_survives_a_blip(self):
        subs.save(subs.row_from_subscription(_sub(status="active"), "r@x.co", NOW))
        self.assertEqual(subs.status("r@x.co")["status"], "active")
        self.table.fail = True
        # Within the cache: no read at all. Past it, the last row is still served.
        self.assertEqual(subs.status("r@x.co")["status"], "active")
        with mock.patch.object(subs.time, "time", return_value=time.time() + subs.CACHE_SECONDS + 5):
            self.assertEqual(subs.status("r@x.co")["status"], "active")
        with mock.patch.object(subs.time, "time", return_value=time.time() + subs.STALE_OK_SECONDS + 5):
            self.assertIsNone(subs.status("r@x.co"))

    def test_load_raises_rather_than_answering_none(self):
        self.table.fail = True
        with self.assertRaises(subs.StoreUnavailable):
            subs.load("r@x.co")
        with mock.patch.object(subs, "_table", lambda: None), self.assertRaises(subs.StoreUnavailable):
            subs.load("r@x.co")

    def test_the_trial_check_fails_closed(self):
        self.assertTrue(subs.trial_available("new@x.co"))
        self.table.fail = True
        self.assertFalse(subs.trial_available("new@x.co"))
        self.assertEqual(subs.customer_for("new@x.co"), "")

    def test_all_rows_lists_subscriptions_only(self):
        subs.save(subs.row_from_subscription(_sub(), "a@x.co", NOW))
        self.table.put_item({"id": "u#a@x.co", "credits": 3})
        rows = subs.all_rows()
        self.assertEqual([r["email"] for r in rows], ["a@x.co"])

    def test_summary_for_a_page(self):
        subs.save(subs.row_from_subscription(_sub(), "r@x.co", NOW))
        with mock.patch.object(subs.time, "time", return_value=NOW):
            s = subs.summary("r@x.co", fresh=True)
        self.assertTrue(s["active"] and s["trialing"])
        self.assertFalse(s["trial_available"])
        self.assertTrue(s["has_customer"])
        self.assertEqual(s["month"], 29.0)
        anon = subs.summary(None)
        self.assertFalse(anon["active"])
        self.assertTrue(anon["trial_available"])


class AccessTests(_TableCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"YSTOCKER_SECRET_KEY": "k", "AGENTS_SELLING_OK": "1"}))
        os.environ.pop("SUBSCRIPTIONS", None)
        os.environ.pop("AGENTS_SELLING_DISABLED", None)

    def test_enabled(self):
        self.assertEqual(subs.enabled(), (True, ""))
        with mock.patch.dict(os.environ, {"SUBSCRIPTIONS": "0"}):
            self.assertFalse(subs.enabled()[0])
        with mock.patch.dict(os.environ, {"YSTOCKER_SECRET_KEY": ""}):
            self.assertFalse(subs.enabled()[0])
        with mock.patch.object(credits, "selling_enabled", return_value=(False, "no Stripe")):
            self.assertEqual(subs.enabled(), (False, "no Stripe"))

    def test_the_wall_check(self):
        self.assertFalse(subs.full_access_for_page(None))
        self.assertFalse(subs.full_access_for_page("free@x.co"))
        vip = sorted(quota.vip_emails())[0]
        self.assertTrue(subs.full_access_for_page(vip))
        subs.save(subs.row_from_subscription(_sub(status="active", trial_end=None,
                                                  current_period_end=int(time.time() + 9 * DAY)),
                                             "paid@x.co", NOW))
        self.assertTrue(subs.full_access_for_page("paid@x.co"))
        # Pro switched off: a signed-in reader reads everything, as before Pro.
        with mock.patch.dict(os.environ, {"SUBSCRIPTIONS": "0"}):
            self.assertTrue(subs.full_access_for_page("free@x.co"))
            self.assertFalse(subs.full_access_for_page(None))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"YSTOCKER_SECRET_KEY": "test-secret"}))

    def test_round_trip(self):
        token = subs.handoff("Reader@Example.com", "checkout", "https://trade-agents.com",
                             plan="year", lang="zh")
        payload = subs.read_handoff(token, "checkout")
        self.assertEqual(payload["e"], "reader@example.com")
        self.assertEqual((payload["plan"], payload["lang"]), ("year", "zh"))
        self.assertEqual(payload["o"], "https://trade-agents.com")

    def test_refusals(self):
        token = subs.handoff("r@x.co", "checkout", "https://trade-agents.com")
        self.assertIsNone(subs.read_handoff(token, "portal"))           # another purpose
        self.assertIsNone(subs.read_handoff(token[:-2] + "xx", "checkout"))
        self.assertIsNone(subs.read_handoff("", "checkout"))
        with mock.patch.dict(os.environ, {"YSTOCKER_SECRET_KEY": "another-secret"}):
            self.assertIsNone(subs.read_handoff(token, "checkout"))
        with mock.patch("itsdangerous.timed.time.time", return_value=time.time() + subs.HANDOFF_MAX_AGE + 60):
            self.assertIsNone(subs.read_handoff(token, "checkout"))

    def test_no_secret_no_handoff(self):
        # The repository's dev fallback must never sign or verify one.
        token = subs.handoff("r@x.co", "portal")
        with mock.patch.dict(os.environ, {"YSTOCKER_SECRET_KEY": ""}):
            with self.assertRaises(subs.HandoffUnavailable):
                subs.handoff("r@x.co", "portal")
            self.assertIsNone(subs.read_handoff(token, "portal"))
        from itsdangerous import URLSafeTimedSerializer
        forged = URLSafeTimedSerializer("ystocker-dev-secret", salt=subs._SALT).dumps(
            {"e": "victim@x.co", "p": "portal", "o": "https://trade-agents.com"})
        self.assertIsNone(subs.read_handoff(forged, "portal"))

    def test_the_way_back_is_one_of_ours(self):
        token = subs.handoff("r@x.co", "checkout", "https://evil.example")
        self.assertEqual(subs.read_handoff(token, "checkout")["o"], subs.DEFAULT_ORIGIN)
        self.assertEqual(subs.origin_ok("https://stock.li-family.us/"), "https://stock.li-family.us")


class QuotaTests(unittest.TestCase):
    def test_a_subscriber_gets_ten_a_day(self):
        with mock.patch.object(subs, "is_entitled", return_value=True):
            self.assertEqual(quota.limit_for("paid@x.co"), 10)
            with mock.patch.dict(os.environ, {"AGENTS_DAILY_LIMIT": "12"}):
                self.assertEqual(quota.limit_for("paid@x.co"), 12)   # never fewer than free
            self.assertEqual(quota.limit_for(sorted(quota.vip_emails())[0]), quota.limit_vip())
        with mock.patch.object(subs, "is_entitled", return_value=False):
            self.assertEqual(quota.limit_for("free@x.co"), quota.limit_default())

    def test_an_unreadable_row_is_the_free_allowance(self):
        with mock.patch.object(subs, "is_entitled", side_effect=RuntimeError("boom")):
            self.assertFalse(quota.is_subscribed("paid@x.co"))
            self.assertEqual(quota.limit_for("paid@x.co"), quota.limit_default())
        self.assertFalse(quota.is_subscribed(None))


if __name__ == "__main__":
    unittest.main()
