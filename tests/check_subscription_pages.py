"""
End-to-end check of TradeAgents Pro on yStocker's side: the /subscribe page,
its two doors to yPay, the status API, and the run page's offer -- through
Flask's test client.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic, like ``check_wiki_pages``: no background thread, no AWS, no .env, and
matplotlib stubbed. The subscription row is patched per test
(``subscriptions.status`` / ``load``), and Pro is put on sale by patching
``subscriptions.enabled`` -- this app has no Stripe facts of its own.

What it pins:

* the page quotes the prices, the saving and the trial from
  ``subscriptions.offer()``, and offers a trial only to an account that has not
  had one;
* a subscriber sees their status and the way into billing, not a second
  checkout; a signed-out reader is sent through sign-in and straight on to
  checkout;
* the doors hand yPay a token it can verify, for the right purpose, plan and
  way back, on the pay host of the brand the reader is on -- and never hand a
  subscriber a second checkout;
* the run page offers Pro beside the packs, and says "Pro" on a subscriber's
  run counter.

Run:  venv/bin/python -m tests.check_subscription_pages
"""
from __future__ import annotations

import html
import os
import sys
import time
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit


class _Any:
    def __getattr__(self, _name): return _Any()
    def __call__(self, *_a, **_k): return _Any()
    def __getitem__(self, _k): return _Any()
    def __setitem__(self, _k, _v): return None
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False
    def update(self, *_a, **_k): return None


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

os.environ["YSTOCKER_SECRET_KEY"] = "check-subscription-secret"
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
           "AGENTS_PAY_URL", "SUBSCRIPTIONS", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import quota, subscriptions                 # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

TA = "http://trade-agents.com"
LI = "http://stock.li-family.us"
READER = "reader@example.com"
FUTURE = int(time.time() + 20 * 86400)
LIVE = {"status": "active", "plan": "month", "period_end": FUTURE, "customer": "cus_1",
        "trial_used": True}
TRIAL = {"status": "trialing", "plan": "year", "period_end": FUTURE, "trial_end": FUTURE,
         "customer": "cus_1", "trial_used": True}
LAPSED = {"status": "canceled", "plan": "month", "period_end": 1_000_000_000,
          "customer": "cus_1", "trial_used": True}


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class SubscriptionPages(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.on_sale = mock.patch.object(subscriptions, "enabled", return_value=(True, ""))
        self.on_sale.start()
        self.addCleanup(self.on_sale.stop)

    def _get(self, path, row=None, email=READER, base=TA):
        with mock.patch.object(subscriptions, "status", return_value=row), \
                mock.patch.object(subscriptions, "load", return_value=row):
            with self.client.session_transaction(base_url=base) as s:
                s.clear()
                if email:
                    s["user_email"] = email
            try:
                return self.client.get(path, base_url=base, follow_redirects=False)
            finally:
                with self.client.session_transaction(base_url=base) as s:
                    s.clear()

    def _page(self, path="/subscribe", **kw):
        r = self._get(path, **kw)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    @staticmethod
    def _hrefs(page):
        import re
        return [html.unescape(h) for h in re.findall(r'href="([^"]+)"', page)]

    # ── the page ──────────────────────────────────────────────────────────
    def test_the_offer_is_the_codes(self):
        page = self._page(email=None)
        offer = subscriptions.offer()
        self.assertIn(f"${offer['month']:g}<small>", page)
        self.assertIn(f"${offer['year']:g}<small>", page)
        self.assertIn(f"Save {offer['saving_pct']}%", page)
        self.assertIn(f"{offer['months_free']} months free", page)
        self.assertIn(f"Start {offer['trial_days']}-day free trial", page)
        self.assertIn(f"{offer['runs_per_day']} analyses a day", page)
        self.assertIn(f"against {quota.limit_default()} on the free plan", page)
        self.assertIn("promotion code", page)
        self.assertIn('class="is-current" aria-current="true"', page)

    def test_signed_out_goes_through_sign_in_to_checkout(self):
        hrefs = self._hrefs(self._page(email=None))
        to_login = [h for h in hrefs if h.startswith("/login?")]
        nexts = {parse_qs(urlsplit(h).query)["next"][0] for h in to_login}
        # The plans' two buttons; the masthead's own Sign in leads to the desk.
        self.assertEqual({n for n in nexts if n.startswith("/subscribe")},
                         {"/subscribe/checkout?plan=month", "/subscribe/checkout?plan=year"})

    def test_signed_in_goes_straight_to_checkout(self):
        hrefs = self._hrefs(self._page())
        self.assertIn("/subscribe/checkout?plan=month", hrefs)
        self.assertIn("/subscribe/checkout?plan=year", hrefs)
        self.assertNotIn("/subscribe/manage", hrefs)

    def test_a_used_trial_is_not_offered_again(self):
        page = self._page(row=LAPSED)
        self.assertNotIn("free trial</span>", page)
        self.assertIn("<span data-l=\"en\">Subscribe</span>", page)
        self.assertIn("has ended", page)

    def test_a_subscriber_sees_status_and_billing(self):
        page = self._page(row=LIVE)
        self.assertIn("You are on TradeAgents Pro", page)
        self.assertIn("Monthly plan. Renews on", page)
        self.assertIn(f'data-pro-date="{FUTURE}"', page)
        hrefs = self._hrefs(page)
        self.assertIn("/subscribe/manage", hrefs)
        self.assertNotIn("/subscribe/checkout?plan=month", hrefs)
        self.assertNotIn("/subscribe/checkout?plan=year", hrefs)
        self.assertIn("Your plan", page)
        trial = self._page(row=TRIAL)
        self.assertIn("Free trial until", trial)
        ending = self._page(row=dict(LIVE, cancel_at_period_end=True))
        self.assertIn("will not renew", ending)
        lapsing = self._page(row=dict(LIVE, status="past_due"))
        self.assertIn("did not go through", lapsing)

    def test_notices(self):
        cases = {
            "/subscribe?canceled=1": "Nothing was charged",
            "/subscribe?already=1": "You already have Pro",
            "/subscribe?error=checkout": "Checkout could not be opened",
            "/subscribe?error=expired": "That link has expired",
            "/subscribe?error=no_billing": "no billing account",
        }
        for path, words in cases.items():
            with self.subTest(path=path):
                self.assertIn(words, self._page(path))
        # A notice nobody defined is no notice, not an echo of the query.
        self.assertNotIn("w-callout is-warn", self._page("/subscribe?error=%3Cscript%3E"))
        # Back from Stripe: welcomed when the row is there, polling when not.
        self.assertIn("Welcome to Pro", self._page("/subscribe?welcome=1", row=LIVE))
        waiting = self._page("/subscribe?welcome=1")
        self.assertIn("data-pro-activating", waiting)
        self.assertIn("/api/subscription?fresh=1", waiting)

    def test_not_on_sale(self):
        self.on_sale.stop()
        with mock.patch.object(subscriptions, "enabled", return_value=(False, "no Stripe")):
            page = self._page()
            self.assertIn("Subscriptions are not available", page)
            self.assertNotIn("/subscribe/checkout", page)
            r = self._get("/subscribe/checkout?plan=year")
            self.assertTrue(r.headers["Location"].endswith("/subscribe?error=unavailable"))
        self.on_sale.start()

    def test_a_vip_is_told_there_is_no_need(self):
        page = self._page(email=sorted(quota.vip_emails())[0])
        self.assertIn("no need to subscribe", page)

    def test_the_page_renders_off_trade_agents_too(self):
        page = self._page(base=LI)
        self.assertIn("/subscribe/checkout?plan=year", self._hrefs(page))

    # ── the doors ─────────────────────────────────────────────────────────
    def _token_from(self, location, host, path):
        parts = urlsplit(location)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}", host)
        self.assertEqual(parts.path, path)
        return parse_qs(parts.query)["t"][0]

    def test_checkout_hands_yay_a_token(self):
        r = self._get("/subscribe/checkout?plan=month&lang=zh")
        self.assertEqual(r.status_code, 302)
        token = self._token_from(r.headers["Location"], "https://pay.trade-agents.com", "/subscribe/start")
        payload = subscriptions.read_handoff(token, "checkout")
        self.assertEqual(payload["e"], READER)
        self.assertEqual((payload["plan"], payload["lang"]), ("month", "zh"))
        self.assertEqual(payload["o"], "https://trade-agents.com")
        self.assertIsNone(subscriptions.read_handoff(token, "portal"))
        # On yStocker's host: its pay host, and back to it.
        r = self._get("/subscribe/checkout?plan=bogus", base=LI)
        token = self._token_from(r.headers["Location"], "https://pay.li-family.us", "/subscribe/start")
        payload = subscriptions.read_handoff(token, "checkout")
        self.assertEqual((payload["plan"], payload["o"]), ("year", "https://stock.li-family.us"))

    def test_checkout_signed_out_or_subscribed(self):
        r = self._get("/subscribe/checkout?plan=month", email=None)
        target = urlsplit(r.headers["Location"])
        self.assertEqual(target.path, "/login")
        self.assertEqual(unquote(parse_qs(target.query)["next"][0]), "/subscribe/checkout?plan=month")
        r = self._get("/subscribe/checkout?plan=month", row=LIVE)
        self.assertTrue(r.headers["Location"].endswith("/subscribe?already=1"))

    def test_manage_hands_yay_a_portal_token(self):
        r = self._get("/subscribe/manage", row=LIVE)
        token = self._token_from(r.headers["Location"], "https://pay.trade-agents.com", "/subscribe/portal")
        self.assertEqual(subscriptions.read_handoff(token, "portal")["e"], READER)
        r = self._get("/subscribe/manage", email=None)
        self.assertEqual(urlsplit(r.headers["Location"]).path, "/login")

    def test_the_status_api(self):
        anon = self._get("/api/subscription", email=None).get_json()
        self.assertFalse(anon["signed_in"] or anon["active"])
        self.assertTrue(anon["enabled"])
        live = self._get("/api/subscription?fresh=1", row=LIVE)
        self.assertEqual(live.headers.get("Cache-Control"), "no-store")
        body = live.get_json()
        self.assertTrue(body["active"] and body["signed_in"])
        self.assertEqual((body["plan"], body["runs_per_day"]), ("month", subscriptions.RUNS_PER_DAY))

    # ── the run page ──────────────────────────────────────────────────────
    def test_the_run_page_offers_pro_beside_the_packs(self):
        free = self._page("/agents", base=LI)
        self.assertIn('data-i18n="agents.buy_pro_why"', free)
        self.assertIn('href="/subscribe"', free)
        with mock.patch.object(subscriptions, "is_entitled", return_value=True):
            paid = self._page("/agents", row=LIVE, base=LI)
        self.assertNotIn('data-i18n="agents.buy_pro_why"', paid)
        self.assertIn("<em>Pro</em>", paid)
        self.assertIn(f"/{subscriptions.RUNS_PER_DAY}", paid)


    # ── models by plan (2026-10-04) ───────────────────────────────────────
    @staticmethod
    def _rows(page):
        """Each model row's key, and whether it is drawn locked."""
        import re
        out = {}
        for m in re.finditer(r'<div class="ag-sel-opt" role="option" id="agModelOpt\d+"(.*?)>', page, re.S):
            attrs = m.group(1)
            key = re.search(r'data-value="([^"]*)"', attrs).group(1)
            out[key] = "data-locked" in attrs
        return out

    def _usage(self, **over):
        base = {"day": "2026-10-04", "used": 0, "limit": 3, "remaining": 3, "vip": False,
                "subscribed": False, "global_used": 0, "global_limit": 60,
                "global_remaining": 60, "tz": "America/Los_Angeles",
                "credits": 0, "pay_url": "https://pay.li-family.us"}
        base.update(over)
        return base

    def test_a_free_reader_sees_the_paid_models_locked(self):
        with mock.patch.object(quota, "usage", return_value=self._usage()):
            page = self._page("/agents", base=LI)
        rows = self._rows(page)
        self.assertEqual(rows.pop("deepseek-flash"), False)
        self.assertTrue(rows and all(rows.values()), rows)
        self.assertIn('id="agTierNote" class="ag-tier-note">', page)       # shown
        self.assertIn("DeepSeek V4 Flash</b>", page)

    def test_paid_runs_unlock_every_model(self):
        cases = {
            "pro": self._usage(subscribed=True, limit=10, remaining=10),
            "vip": self._usage(vip=True, limit=15, remaining=15),
            # Free runs gone and a credit banked: the next run is paid.
            "credit": self._usage(used=3, remaining=0, credits=5),
        }
        for name, usage in cases.items():
            with self.subTest(plan=name), mock.patch.object(quota, "usage", return_value=usage):
                page = self._page("/agents", base=LI)
                self.assertFalse(any(self._rows(page).values()))
                self.assertIn('id="agTierNote" class="ag-tier-note" hidden>', page)
        # Free runs left, credits banked: the credit is not what pays next.
        with mock.patch.object(quota, "usage", return_value=self._usage(credits=5)):
            self.assertTrue(self._rows(self._page("/agents", base=LI))["google-pro"])

    def _run(self, model, info):
        refund = mock.MagicMock()
        with mock.patch.object(quota, "try_consume", return_value=(True, None, info)), \
                mock.patch.object(quota, "refund", refund), \
                mock.patch.object(quota, "usage", return_value=self._usage()), \
                mock.patch.object(quota, "is_vip", return_value=False), \
                mock.patch.object(quota, "is_subscribed", return_value=False):
            with self.client.session_transaction(base_url=LI) as s:
                s["user_email"] = READER
            try:
                r = self.client.post("/api/agents/run", base_url=LI,
                                     json={"ticker": "AAPL", "date": "2026-10-02", "model": model})
            finally:
                with self.client.session_transaction(base_url=LI) as s:
                    s.clear()
        return r, refund

    def test_a_free_run_asking_for_a_paid_model_is_refused_and_refunded(self):
        r, refund = self._run("google-pro", {"paid": False})
        self.assertEqual(r.status_code, 403)
        body = r.get_json()
        self.assertEqual(body["reason"], "model_pro")
        self.assertEqual(body["pro_url"], "/subscribe")
        refund.assert_called_once_with(READER, paid=False)


if __name__ == "__main__":
    unittest.main()
