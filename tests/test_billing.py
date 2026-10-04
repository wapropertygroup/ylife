"""yPay's half of TradeAgents Pro: the checkout, recording, reconciling, and
the webhook that delivers it -- the last one with a real Stripe signature.

No network, no AWS, no Stripe account: stripe is a stand-in that records what
would have been sent, the subscription store is patched at
``ystocker.subscriptions``, and the webhook is driven through yPay's own test
client with a payload signed exactly as Stripe signs one, so
``stripe.Webhook.construct_event`` runs for real.

What this exists to catch, none of which raises a visible error:

* **A verified run-pack payment never credited.** stripe 15's event objects
  are not dicts, and the handler's ``.get`` raised on every one; it now reads
  the verified payload as JSON. The pack test below fails on the old code.
* **A trial handed out twice, or a card not taken.** The trial is in the
  session only when the account has none, and the card is always collected.
* **A discount that needs a deploy.** Promotion codes are allowed at checkout.
* **A forged billing link.** /subscribe/start and /subscribe/portal refuse any
  token yStocker did not sign for that purpose.
* **A cancellation missed.** The hourly reconciliation re-reads any
  subscription at or past the end of its period.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import stripe as real_stripe

# Imported here, once, and never under a patch.dict of sys.modules: leaving
# that context drops every module imported inside it, so the next create_app()
# re-imported ypay.routes -- a copy with the real _get_stripe -- and a route test
# went out to api.stripe.com with the dummy key.
from ypay import billing as pay_billing
from ypay import create_app as pay_create_app
from ypay import routes as pay_routes
from ystocker import subscriptions as subs

SECRET = "test-ystocker-secret"
WHSEC = "whsec_test_signing"
_ENV = {"STRIPE_SECRET_KEY": "sk_test_dummy", "STRIPE_PUBLISHABLE_KEY": "pk_test_dummy",
        "STRIPE_WEBHOOK_SECRET": WHSEC, "YSTOCKER_SECRET_KEY": SECRET,
        "YPAY_RECONCILE": "0"}


class _Obj(dict):
    """A Stripe object: attribute access, and to_dict()."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self):
        return dict(self)


class FakeStripe:
    def __init__(self):
        self.created: list[dict] = []
        self.prices: list[dict] = []
        self.price_creates: list[dict] = []
        self.product_exists = True
        self.sessions: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.portal_calls: list[dict] = []
        self.portal_configs: list[dict] = []
        outer = self

        class _Missing(Exception):
            code = "resource_missing"

        self._Missing = _Missing
        self.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(
            create=self._session_create, retrieve=self._session_retrieve))
        self.Price = types.SimpleNamespace(list=self._price_list, create=self._price_create)
        self.Product = types.SimpleNamespace(retrieve=self._product_retrieve,
                                             create=lambda **kw: outer.__setattr__("product_exists", True))
        self.Subscription = types.SimpleNamespace(retrieve=lambda sid, **kw: _Obj(outer.subscriptions[sid]))
        self.billing_portal = types.SimpleNamespace(
            Session=types.SimpleNamespace(create=self._portal_create),
            Configuration=types.SimpleNamespace(list=self._config_list, create=self._config_create))

    def _session_create(self, **kw):
        self.created.append(kw)
        return _Obj(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")

    def _session_retrieve(self, sid, expand=None):
        return _Obj(self.sessions[sid])

    def _price_list(self, lookup_keys, active, limit):
        hits = [_Obj(p) for p in self.prices if p["lookup_key"] in lookup_keys]
        return types.SimpleNamespace(data=hits[:limit])

    def _price_create(self, **kw):
        self.price_creates.append(kw)
        price = dict(id=f"price_{len(self.price_creates)}", unit_amount=kw["unit_amount"],
                     currency=kw["currency"], recurring=kw["recurring"], lookup_key=kw["lookup_key"])
        self.prices = [p for p in self.prices if p["lookup_key"] != kw["lookup_key"]] + [price]
        return _Obj(price)

    def _product_retrieve(self, pid):
        if not self.product_exists:
            raise self._Missing("No such product: 'tradeagents_pro'")
        return _Obj(id=pid)

    def _portal_create(self, **kw):
        self.portal_calls.append(kw)
        return _Obj(url="https://billing.stripe.com/p/session/test_1")

    def _config_list(self, active, limit):
        return types.SimpleNamespace(data=[_Obj(c) for c in self.portal_configs])

    def _config_create(self, **kw):
        conf = dict(id="bpc_1", metadata=kw.get("metadata", {}))
        self.portal_configs.append(conf)
        return _Obj(conf)


def _sub(status="trialing", email="reader@example.com", **over):
    end = int(time.time() + 7 * 86400)
    sub = {"id": "sub_1", "status": status, "customer": "cus_1", "trial_end": end,
           "cancel_at_period_end": False, "metadata": {"email": email, "plan": "month"},
           "items": {"data": [{"current_period_end": end,
                               "price": {"recurring": {"interval": "month"}}}]}}
    sub.update(over)
    return sub


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.billing = pay_billing
        pay_billing._price_cache.clear()
        pay_billing._portal_config = None
        self.addCleanup(pay_billing._price_cache.clear)
        self.stripe = FakeStripe()

    def _params(self, **kw):
        args = dict(plan_id="year", email="r@x.co", price_id="price_y", customer="",
                    trial_days=7, origin="https://trade-agents.com",
                    pay_base="https://pay.trade-agents.com", lang="zh")
        args.update(kw)
        return self.billing.checkout_params(**args)

    def test_checkout_params(self):
        p = self._params()
        self.assertEqual(p["mode"], "subscription")
        self.assertTrue(p["allow_promotion_codes"])
        self.assertEqual(p["payment_method_collection"], "always")
        self.assertEqual(p["subscription_data"]["trial_period_days"], 7)
        self.assertEqual(p["subscription_data"]["metadata"]["email"], "r@x.co")
        self.assertEqual(p["customer_email"], "r@x.co")
        self.assertNotIn("customer", p)
        self.assertEqual(p["locale"], "zh")
        self.assertEqual(p["cancel_url"], "https://trade-agents.com/subscribe?canceled=1&lang=zh")
        self.assertTrue(p["success_url"].startswith("https://pay.trade-agents.com/subscribe/done?session_id="))
        self.assertIn("{CHECKOUT_SESSION_ID}", p["success_url"])

    def test_no_second_trial_and_one_customer(self):
        p = self._params(trial_days=0, customer="cus_9", lang="")
        self.assertNotIn("trial_period_days", p["subscription_data"])
        self.assertEqual(p["customer"], "cus_9")
        self.assertNotIn("customer_email", p)
        self.assertNotIn("locale", p)

    def test_a_price_is_reused_until_the_amount_changes(self):
        first = self.billing.ensure_price(self.stripe, "month")
        self.assertEqual(self.stripe.price_creates[0]["unit_amount"], 2900)
        self.assertEqual(self.stripe.price_creates[0]["recurring"], {"interval": "month"})
        self.assertTrue(self.stripe.price_creates[0]["transfer_lookup_key"])
        self.billing._price_cache.clear()
        self.assertEqual(self.billing.ensure_price(self.stripe, "month"), first)
        self.assertEqual(len(self.stripe.price_creates), 1)
        self.billing._price_cache.clear()
        with mock.patch.dict(subs.PLANS, {"month": dict(subs.PLANS["month"], price=35.0)}):
            self.assertNotEqual(self.billing.ensure_price(self.stripe, "month"), first)
        self.assertEqual(self.stripe.price_creates[-1]["unit_amount"], 3500)

    def test_the_product_is_made_if_missing(self):
        self.stripe.product_exists = False
        self.billing.ensure_price(self.stripe, "year")
        self.assertTrue(self.stripe.product_exists)

    def test_the_portal_configuration_is_made_once(self):
        first = self.billing.ensure_portal_config(self.stripe)
        self.billing._portal_config = None
        self.assertEqual(self.billing.ensure_portal_config(self.stripe), first)
        self.assertEqual(len(self.stripe.portal_configs), 1)

    def test_record_session(self):
        self.stripe.subscriptions["sub_1"] = _sub()
        self.stripe.sessions["cs_1"] = {"id": "cs_1", "mode": "subscription", "status": "complete",
                                        "metadata": {"email": "Reader@Example.com"},
                                        "subscription": "sub_1"}
        self.stripe.sessions["cs_pack"] = {"id": "cs_pack", "mode": "payment", "status": "complete"}
        self.stripe.sessions["cs_open"] = {"id": "cs_open", "mode": "subscription", "status": "open"}
        with mock.patch.object(subs, "save", return_value=True) as save:
            row = self.billing.record_session(self.stripe, "cs_1")
            self.assertEqual(row["email"], "reader@example.com")
            self.assertEqual(row["status"], "trialing")
            save.assert_called_once()
            self.assertIsNone(self.billing.record_session(self.stripe, "cs_pack"))
            self.assertIsNone(self.billing.record_session(self.stripe, "cs_open"))
            self.assertEqual(save.call_count, 1)

    def test_an_event_names_its_subscriber(self):
        with mock.patch.object(subs, "save", return_value=True) as save:
            self.assertEqual(self.billing.record_event_subscription(_sub(), 123)["event_created"], 123)
            # No address on it: the customer map, else not one of ours.
            bare = _sub(metadata={})
            with mock.patch.object(subs, "email_for_customer", return_value="mapped@x.co"):
                self.assertEqual(self.billing.record_event_subscription(bare, 124)["email"], "mapped@x.co")
            with mock.patch.object(subs, "email_for_customer", return_value=""):
                self.assertIsNone(self.billing.record_event_subscription(bare, 125))
            self.assertEqual(save.call_count, 2)

    def test_what_reconciliation_rereads(self):
        now = 1_800_000_000
        due = self.billing.due_for_reconcile
        self.assertFalse(due({"subscription": "", "status": "active", "period_end": 0}, now))
        self.assertFalse(due({"subscription": "s", "status": "canceled", "period_end": 0}, now))
        self.assertTrue(due({"subscription": "s", "status": "past_due", "period_end": now + 9e6}, now))
        self.assertTrue(due({"subscription": "s", "status": "active", "period_end": now + 3600}, now))
        self.assertFalse(due({"subscription": "s", "status": "active", "period_end": now + 86400}, now))

    def test_reconcile_records_a_cancellation_it_was_never_told_of(self):
        now = time.time()
        rows = [{"email": "a@x.co", "subscription": "sub_1", "status": "active", "period_end": now - 60},
                {"email": "b@x.co", "subscription": "sub_2", "status": "active", "period_end": now + 9e6}]
        self.stripe.subscriptions["sub_1"] = _sub(status="canceled", email="a@x.co")
        with mock.patch.object(subs, "all_rows", return_value=rows), \
                mock.patch.object(subs, "save", return_value=True) as save:
            self.assertEqual(self.billing.reconcile(self.stripe, now=now), 1)
        saved = save.call_args[0][0]
        self.assertEqual((saved["email"], saved["status"]), ("a@x.co", "canceled"))


def _signed(payload: dict, secret: str = WHSEC) -> tuple[bytes, str]:
    """A body and Stripe-Signature header exactly as Stripe sends them. Every
    real event says it is one; construct_event raises KeyError without it."""
    body = json.dumps({"object": "event", **payload}).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


class _AppCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, _ENV))
        for key in ("AGENTS_SELLING_DISABLED", "AGENTS_SELLING_OK", "AGENTS_PAY_URL", "SUBSCRIPTIONS"):
            os.environ.pop(key, None)
        # No .env: the real one holds a live Stripe key.
        self.enterContext(mock.patch("dotenv.load_dotenv", lambda *a, **k: False))
        self.routes, self.billing = pay_routes, pay_billing
        pay_billing._price_cache.clear()
        pay_billing._portal_config = None
        self.enterContext(mock.patch.object(pay_routes, "_get_payments_table", return_value=None))
        self.stripe = FakeStripe()
        # The webhook verifies with the real library; everything else is the fake.
        self.stripe.Webhook = real_stripe.Webhook
        self.enterContext(mock.patch.object(pay_routes, "_get_stripe", side_effect=lambda: self.stripe))
        self.app = pay_create_app()
        self.client = self.app.test_client()


class WebhookTests(_AppCase):
    def _post(self, payload, secret=WHSEC):
        body, sig = _signed(payload, secret)
        return self.client.post("/api/webhook", data=body, headers={"Stripe-Signature": sig},
                                content_type="application/json", base_url="https://pay.li-family.us")

    def test_a_verified_pack_payment_is_credited(self):
        # The fault this replaces: stripe 15's event is not a dict, and the
        # handler's `.get` raised before any run was granted.
        session = {"id": "cs_pack_1", "object": "checkout.session", "mode": "payment",
                   "amount_total": 2500, "customer_details": {"email": "buyer@x.co", "name": "B"},
                   "metadata": {"agent_credits": "28", "pack_id": "runs28", "email": "buyer@x.co",
                                "item_id": "runs28", "item_name": "28 runs"}}
        with mock.patch.object(self.routes, "_grant_agent_runs") as grant:
            r = self._post({"id": "evt_1", "type": "checkout.session.completed",
                            "created": int(time.time()), "data": {"object": session}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        grant.assert_called_once()
        self.assertTrue(grant.call_args[0][4])          # verified

    def test_a_bad_signature_is_refused(self):
        with mock.patch.object(self.routes, "_grant_agent_runs") as grant:
            r = self._post({"type": "checkout.session.completed", "data": {"object": {}}},
                           secret="whsec_wrong")
        self.assertEqual(r.status_code, 400)
        grant.assert_not_called()

    def test_subscription_events_are_recorded(self):
        with mock.patch.object(self.billing, "record_event_subscription") as rec:
            r = self._post({"id": "evt_2", "type": "customer.subscription.updated", "created": 777,
                            "data": {"object": _sub(status="active")}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(rec.call_args[0][0]["status"], "active")
        self.assertEqual(rec.call_args[0][1], 777)

    def test_a_subscription_checkout_is_recorded_not_credited(self):
        with mock.patch.object(self.billing, "record_session") as rec, \
                mock.patch.object(self.routes, "_grant_agent_runs") as grant:
            r = self._post({"id": "evt_3", "type": "checkout.session.completed", "created": 1,
                            "data": {"object": {"id": "cs_sub_1", "mode": "subscription"}}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(rec.call_args[0][1], "cs_sub_1")
        grant.assert_not_called()

    def test_a_failed_record_asks_stripe_to_retry(self):
        with mock.patch.object(self.billing, "record_event_subscription", side_effect=RuntimeError("ddb")):
            r = self._post({"type": "customer.subscription.deleted", "created": 1,
                            "data": {"object": _sub(status="canceled")}})
        self.assertEqual(r.status_code, 500)


class SubscribeRouteTests(_AppCase):
    def _token(self, purpose, **extra):
        return subs.handoff("reader@example.com", purpose, "https://trade-agents.com", **extra)

    def _get(self, path):
        return self.client.get(path, base_url="https://pay.trade-agents.com")

    def test_start_opens_checkout_with_the_trial(self):
        with mock.patch.object(subs, "load", return_value=None):
            r = self._get("/subscribe/start?t=" + self._token("checkout", plan="year", lang="zh"))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["Location"], "https://checkout.stripe.com/c/pay/cs_test_1")
        sent = self.stripe.created[0]
        self.assertEqual(sent["subscription_data"]["trial_period_days"], subs.TRIAL_DAYS)
        self.assertEqual(sent["customer_email"], "reader@example.com")
        self.assertEqual(sent["metadata"]["origin"], "https://trade-agents.com")
        self.assertTrue(sent["success_url"].startswith("https://pay.trade-agents.com/subscribe/done"))
        self.assertEqual(self.stripe.price_creates[0]["unit_amount"], 29000)

    def test_start_without_a_second_trial(self):
        row = {"status": "canceled", "trial_used": True, "customer": "cus_7", "period_end": 0}
        with mock.patch.object(subs, "load", return_value=row):
            self._get("/subscribe/start?t=" + self._token("checkout", plan="month"))
        sent = self.stripe.created[0]
        self.assertNotIn("trial_period_days", sent["subscription_data"])
        self.assertEqual(sent["customer"], "cus_7")

    def test_start_refusals(self):
        live = {"status": "active", "period_end": time.time() + 9e5}
        cases = {
            "/subscribe/start?t=forged": "/subscribe?error=expired",
            "/subscribe/start?t=" + self._token("portal"): "/subscribe?error=expired",
            "/subscribe/start?t=" + self._token("checkout", plan="lifetime"): "/subscribe?error=plan",
        }
        for path, where in cases.items():
            with self.subTest(path=path[:40]):
                r = self._get(path)
                self.assertEqual(r.status_code, 302)
                self.assertTrue(r.headers["Location"].endswith(where), r.headers["Location"])
        with mock.patch.object(subs, "load", return_value=live):
            r = self._get("/subscribe/start?t=" + self._token("checkout", plan="year"))
        self.assertTrue(r.headers["Location"].endswith("/subscribe?already=1"))
        with mock.patch.object(subs, "load", side_effect=subs.StoreUnavailable("down")):
            r = self._get("/subscribe/start?t=" + self._token("checkout", plan="year"))
        self.assertTrue(r.headers["Location"].endswith("/subscribe?error=unavailable"))
        self.assertEqual(self.stripe.created, [])

    def test_done_records_and_returns(self):
        self.stripe.sessions["cs_ok"] = {"id": "cs_ok", "mode": "subscription", "status": "complete",
                                         "metadata": {"origin": "https://stock.li-family.us",
                                                      "email": "reader@example.com", "lang": "zh"},
                                         "subscription": "sub_1"}
        self.stripe.subscriptions["sub_1"] = _sub()
        with mock.patch.object(subs, "save", return_value=True) as save:
            r = self._get("/subscribe/done?session_id=cs_ok")
        save.assert_called_once()
        self.assertEqual(r.headers["Location"], "https://stock.li-family.us/subscribe?welcome=1&lang=zh")
        r = self._get("/subscribe/done?session_id=not-a-session")
        self.assertIn("error=unavailable", r.headers["Location"])

    def test_portal(self):
        with mock.patch.object(subs, "customer_for", return_value="cus_1"):
            r = self._get("/subscribe/portal?t=" + self._token("portal", lang="zh"))
        self.assertEqual(r.status_code, 303)
        call = self.stripe.portal_calls[0]
        self.assertEqual(call["customer"], "cus_1")
        self.assertEqual(call["return_url"], "https://trade-agents.com/subscribe?lang=zh")
        self.assertEqual(call["locale"], "zh")
        with mock.patch.object(subs, "customer_for", return_value=""):
            r = self._get("/subscribe/portal?t=" + self._token("portal"))
        self.assertTrue(r.headers["Location"].endswith("/subscribe?error=no_billing"))
        r = self._get("/subscribe/portal?t=" + self._token("checkout"))
        self.assertTrue(r.headers["Location"].endswith("/subscribe?error=expired"))


if __name__ == "__main__":
    unittest.main()
