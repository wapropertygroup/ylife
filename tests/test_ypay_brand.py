"""pay.trade-agents.com's pages, and the checkout they start.

Needs no network, no AWS and no Stripe: the ypay app is built with dummy Stripe
keys (so it never asks SSM for the real ones) and no .env, the payments table is
switched off, and stripe.checkout.Session is a stand-in that records what would
have been sent.

What this exists to catch, none of which raises:

* **A TradeAgents buyer shown yPay.** pay.trade-agents.com served yPay's page --
  "Support the apps", a coffee, the Li Family apps -- to a reader who had come
  from trade-agents.com's account menu to prepay for runs, and Stripe's page
  called the pack "yStocker — 28 runs".
* **A dead end behind Stripe's back button.** The cancel URL was /cancel, whose
  "Back" led to a pack page with no address -- which shows no packs.
* **A way back that goes anywhere.** `next` becomes a link on the page that says
  "payment received", so only our own sites, over https, are honoured.
* **A language the page cannot show.** Every English block has its Chinese
  partner, and every code /api/checkout sends has a line in both.
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
TA_TEMPLATES = ROOT / "ypay" / "templates" / "ta"
TA = "https://pay.trade-agents.com"
LI = "https://pay.li-family.us"
READER = "reader@example.com"
DESK = "https://trade-agents.com/agents"

_ENV = {"STRIPE_SECRET_KEY": "sk_test_dummy",
        "STRIPE_PUBLISHABLE_KEY": "pk_test_dummy",
        "STRIPE_WEBHOOK_SECRET": "whsec_dummy"}
# Anything in the shell that would change what is on sale or where it goes.
_DROP = ("AGENTS_SELLING_DISABLED", "AGENTS_SELLING_OK", "AGENTS_PAY_URL")

_EN = re.compile(r"""data-l\s*=\s*["']en["']""")
_ZH = re.compile(r"""data-l\s*=\s*["']zh["']""")
_COMMENT = re.compile(r"\{#.*?#\}", re.S)


class _Stripe:
    """stripe, as far as api_checkout reaches into it."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail
        self.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        if self.fail:
            raise RuntimeError("Invalid API Key provided")
        self.calls.append(kw)
        return types.SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_x",
                                     id="cs_test_x")


class _PayCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, _ENV))
        for key in _DROP:
            os.environ.pop(key, None)
        self.enterContext(mock.patch.dict(
            sys.modules, {"dotenv": types.SimpleNamespace(load_dotenv=lambda *a, **k: None)}))
        from ypay import create_app, routes

        self.routes = routes
        self.enterContext(mock.patch.object(routes, "_get_payments_table", return_value=None))
        self.stripe = _Stripe()
        self.enterContext(mock.patch.object(routes, "_get_stripe", side_effect=lambda: self.stripe))
        self.app = create_app()
        self.client = self.app.test_client()

    def get(self, path: str, base: str = TA) -> str:
        r = self.client.get(path, base_url=base)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def checkout(self, base: str = TA, **body):
        body.setdefault("item_id", "runs28")
        body.setdefault("email", READER)
        body.setdefault("next", DESK)
        return self.client.post("/api/checkout", json=body, base_url=base)


class PageTests(_PayCase):
    def test_the_pay_page_wears_the_sites_frame(self):
        page = self.get(f"/?email={READER}&next={DESK}")
        self.assertIn('class="w-paper w-pay"', page)
        self.assertIn('class="w-top"', page)
        self.assertIn('class="w-footer"', page)
        self.assertIn("/ta/wiki.css?v=", page)
        # The product mark, drawn from yStocker's own macro.
        self.assertIn('fill="#34d399"', page)
        self.assertIn("TradeAgents", page)
        self.assertIn(READER, page)
        for pack in ("runs5", "runs28", "runs60", "runs130"):
            self.assertIn(f'value="{pack}"', page)

    def test_nothing_on_the_trade_agents_host_speaks_as_ypay(self):
        for path in (f"/?email={READER}", "/", "/success?session_id=cs_x&pack=runs5", "/cancel"):
            with self.subTest(path=path):
                page = self.get(path)
                for phrase in ("yPay", "Support the apps", "Li Family", "li-family.us",
                               "Coffee", "yStocker"):
                    self.assertNotIn(phrase, page)

    def test_pay_li_family_keeps_ypays_own_page(self):
        page = self.get(f"/?email={READER}", base=LI)
        self.assertIn("Support the apps", page)
        self.assertNotIn("w-paper", page)
        self.assertIn("yPay", self.get("/success?session_id=cs_x", base=LI))
        self.assertIn("Back to yPay", self.get("/cancel", base=LI))

    def test_without_an_address_it_offers_sign_in_and_the_prices(self):
        page = self.get("/")
        self.assertNotIn('name="pack"', page)
        self.assertNotIn("data-pay-form", page)
        self.assertIn('href="https://trade-agents.com/login?next=/agents"', page)
        self.assertIn("$25", page)

    def test_an_address_without_an_at_sign_is_no_address(self):
        self.assertNotIn('name="pack"', self.get("/?email=reader"))

    def test_with_selling_off_nothing_is_offered(self):
        os.environ["AGENTS_SELLING_DISABLED"] = "1"
        page = self.get(f"/?email={READER}")
        self.assertNotIn('name="pack"', page)
        self.assertIn("Run packs are paused.", page)
        self.assertIn("次数包暂停销售。", page)

    def test_back_from_stripe_says_nothing_was_charged(self):
        self.assertIn("nothing was charged", self.get(f"/?email={READER}&canceled=1"))
        self.assertNotIn("nothing was charged", self.get(f"/?email={READER}"))

    def test_each_pack_says_what_it_saves_against_the_smallest(self):
        packs = self.routes._ta_packs([
            {"id": "a", "credits": 5, "price": 5.0, "per_run": 1.0},
            {"id": "b", "credits": 130, "price": 100.0, "per_run": 0.769},
        ])
        self.assertEqual([p["save_pct"] for p in packs], [0, 23])
        self.assertEqual(self.routes._ta_packs([]), [])

    def test_the_success_page_counts_the_pack_from_the_table(self):
        page = self.get(f"/success?session_id=cs_live_abc&pack=runs28&next={DESK}")
        self.assertIn("28 runs are being added", page)
        self.assertIn("cs_live_abc", page)
        self.assertIn(f'href="{DESK}"', page)
        # An id the table does not know is "your runs", not a number.
        self.assertIn("your runs are being added", self.get("/success?pack=runs999"))


class ReturnTests(_PayCase):
    def test_only_our_own_sites_over_https(self):
        ok = (DESK, "https://www.trade-agents.com/agents",
              "https://stock.li-family.us/agents")
        refused = ("", "http://trade-agents.com/agents", "https://evil.example/",
                   "https://trade-agents.com.evil.example/", "https://eviltrade-agents.com/",
                   "https://trade-agents.com@evil.example/", "https://evil.example\\@trade-agents.com/",
                   "javascript:alert(1)", "//trade-agents.com/agents", "https://trade-agents.com/\tagents")
        for url in ok:
            self.assertEqual(self.routes._safe_return(url), url)
        for url in refused:
            with self.subTest(url=url):
                self.assertEqual(self.routes._safe_return(url), "")

    def test_a_planted_next_is_not_linked(self):
        for page in (self.get(f"/?email={READER}&next=https://evil.example/"),
                     self.get("/success?session_id=cs_x&next=https://evil.example/"),
                     self.get("/cancel?next=https://evil.example/")):
            self.assertNotIn("evil.example", page)
            self.assertIn(f'href="{DESK}"', page)


class CheckoutTests(_PayCase):
    def test_a_trade_agents_pack_comes_back_to_the_packs_or_to_the_desk(self):
        r = self.checkout(lang="zh")
        self.assertEqual(r.status_code, 200, r.get_json())
        sent = self.stripe.calls[0]
        cancel = urlsplit(sent["cancel_url"])
        self.assertEqual((cancel.scheme, cancel.netloc, cancel.path), ("https", "pay.trade-agents.com", "/"))
        self.assertEqual(parse_qs(cancel.query),
                         {"email": [READER], "canceled": ["1"], "next": [DESK]})
        self.assertTrue(sent["success_url"].startswith(
            "https://pay.trade-agents.com/success?session_id={CHECKOUT_SESSION_ID}&"), sent["success_url"])
        done = parse_qs(urlsplit(sent["success_url"]).query)
        self.assertEqual((done["pack"], done["next"]), (["runs28"], [DESK]))
        # Stripe's page in the reader's language, and the pack named for this brand.
        self.assertEqual(sent["locale"], "zh")
        self.assertEqual(sent["line_items"][0]["price_data"]["product_data"]["name"],
                         "TradeAgents — 28 次分析")
        self.assertEqual(sent["metadata"]["buyer_email"], READER)

    def test_english_and_an_unsaid_language(self):
        self.checkout(lang="en")
        self.assertEqual(self.stripe.calls[0]["locale"], "en")
        self.assertEqual(self.stripe.calls[0]["line_items"][0]["price_data"]["product_data"]["name"],
                         "TradeAgents — 28 runs")
        self.checkout(lang="fr")
        self.assertNotIn("locale", self.stripe.calls[1])

    def test_https_although_flask_sees_http_behind_nginx(self):
        self.checkout(base="http://pay.trade-agents.com")
        self.assertTrue(self.stripe.calls[0]["success_url"].startswith("https://pay.trade-agents.com/"))
        self.assertTrue(self.stripe.calls[0]["cancel_url"].startswith("https://pay.trade-agents.com/"))

    def test_a_local_host_keeps_http(self):
        self.checkout(base="http://localhost:5005")
        self.assertEqual(self.stripe.calls[0]["cancel_url"], "http://localhost:5005/cancel")

    def test_pay_li_family_checkout_is_as_it_was(self):
        self.assertEqual(self.checkout(base=LI).status_code, 200)
        sent = self.stripe.calls[0]
        self.assertEqual(sent["cancel_url"], "https://pay.li-family.us/cancel")
        self.assertEqual(sent["success_url"],
                         "https://pay.li-family.us/success?session_id={CHECKOUT_SESSION_ID}")
        self.assertEqual(sent["line_items"][0]["price_data"]["product_data"]["name"],
                         "yStocker — 28 runs")
        self.assertNotIn("locale", sent)
        # Donations still sell there.
        self.assertEqual(self.checkout(base=LI, item_id="coffee", email="").status_code, 200)

    def test_a_planted_next_is_not_carried(self):
        self.checkout(next="https://evil.example/")
        sent = self.stripe.calls[0]
        self.assertNotIn("evil", sent["success_url"] + sent["cancel_url"])
        self.assertEqual(sent["metadata"]["return_to"], "")

    def test_every_refusal_carries_a_code(self):
        cases = [
            (dict(email=""), 400, "sign_in"),
            (dict(item_id="runs999"), 404, "not_found"),
            # The pay page there sells no donations, so it takes none.
            (dict(item_id="coffee"), 404, "not_found"),
        ]
        for body, status, code in cases:
            with self.subTest(body=body):
                r = self.checkout(**body)
                self.assertEqual((r.status_code, r.get_json()["code"]), (status, code))
        os.environ["AGENTS_SELLING_DISABLED"] = "1"
        r = self.checkout()
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "unavailable"))
        del os.environ["AGENTS_SELLING_DISABLED"]
        self.stripe.fail = True
        r = self.checkout()
        self.assertEqual((r.status_code, r.get_json()["code"]), (500, "failed"))
        with mock.patch.object(self.routes, "_get_stripe", return_value=None):
            r = self.checkout()
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "unavailable"))
        self.assertEqual(self.stripe.calls, [])


class LanguageTests(_PayCase):
    def test_every_english_block_has_its_chinese_partner(self):
        files = sorted(TA_TEMPLATES.glob("*.html"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                self.assertEqual(len(_EN.findall(text)), len(_ZH.findall(text)))
        for path in (f"/?email={READER}", "/", "/success?pack=runs5", "/cancel"):
            page = self.get(path)
            with self.subTest(path=path):
                self.assertGreater(len(_EN.findall(page)), 10)
                self.assertEqual(len(_EN.findall(page)), len(_ZH.findall(page)))

    def test_every_code_the_checkout_sends_has_a_line_in_both_languages(self):
        # "amount" is the donation card's custom figure, which the TradeAgents
        # host refuses before it is read.
        sent = set(re.findall(r'"code":\s*"(\w+)"', (ROOT / "ypay" / "routes.py").read_text()))
        self.assertTrue({"sign_in", "unavailable", "not_found", "failed"} <= sent, sent)
        page = (TA_TEMPLATES / "index.html").read_text(encoding="utf-8")
        shown = set(re.findall(r"^\s+(\w+):\s+\['", page, re.M))
        self.assertEqual(sent - {"amount"} - shown, set())
        self.assertIn("network", shown)


class AssetTests(_PayCase):
    def test_the_sites_stylesheet_is_served_as_it_is(self):
        with self.client.get("/ta/wiki.css", base_url=TA) as r:
            self.assertEqual((r.status_code, r.mimetype), (200, "text/css"))
            self.assertEqual(r.get_data(), (ROOT / "ystocker" / "static" / "wiki.css").read_bytes())
        with self.client.get("/ta/favicon.svg", base_url=TA) as r:
            self.assertEqual((r.status_code, r.mimetype), (200, "image/svg+xml"))

    def test_nothing_else_of_ystockers_is_served(self):
        for name in ("i18n.js", "base.html", "..%2F__init__.py", "manifest.json"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(f"/ta/{name}", base_url=TA).status_code, 404)


if __name__ == "__main__":
    unittest.main()
