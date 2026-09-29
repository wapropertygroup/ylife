"""The run-credit ledger's reading side, and the links that sell more.

Needs no app, no network and no DynamoDB: the table is stubbed, and the few
functions that read the request get a minimal Flask app whose only route is the
agents page, which is all ``credits`` asks ``url_for`` for.

Three failures this exists to catch, none of which raises:

* **An unreachable ledger shown as an empty one.** The account menu quotes the
  balance, and a paying reader told "$0" because DynamoDB was down has been told
  their money is gone. ``peek_balance`` says None for "could not read";
  ``balance`` still says 0, because an unreadable ledger must fund no run.
* **A buyer sent to the other brand's pay page.** The run page's top-up link
  was built from ``PAY_URL`` alone, so a reader on trade-agents.com was handed
  to pay.li-family.us at the moment an unfamiliar domain costs most.
* **A link that never comes back.** ``next`` was built from ``url_for`` of an
  endpoint that does not exist, and from ``request.url_root``, which reads
  http:// behind nginx and so failed the https check ypay applies. Either one
  alone dropped it from every link.
"""
from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from flask import Blueprint, Flask

from ystocker import credits, quota


def _app() -> Flask:
    app = Flask(__name__)
    bp = Blueprint("main", __name__)
    bp.add_url_rule("/agents", "agents_page", lambda: "")
    app.register_blueprint(bp)
    return app


class _Table:
    def __init__(self, item=None, fail=False):
        self.item, self.fail = item, fail

    def get_item(self, **_kw):
        if self.fail:
            raise RuntimeError("throttled")
        return {"Item": self.item} if self.item is not None else {}


class BalanceTests(unittest.TestCase):
    def test_an_unreachable_ledger_is_unknown_not_empty(self):
        with mock.patch.object(credits, "_get_table", return_value=None):
            self.assertIsNone(credits.peek_balance("reader@example.com"))
            # Spending still fails closed.
            self.assertEqual(credits.balance("reader@example.com"), 0)

    def test_a_failed_read_is_unknown(self):
        with mock.patch.object(credits, "_get_table", return_value=_Table(fail=True)):
            self.assertIsNone(credits.peek_balance("reader@example.com"))
            self.assertEqual(credits.balance("reader@example.com"), 0)

    def test_a_reader_with_no_row_has_nothing(self):
        # Answered, and empty: that is 0, not unknown.
        with mock.patch.object(credits, "_get_table", return_value=_Table()):
            self.assertEqual(credits.peek_balance("reader@example.com"), 0)

    def test_the_stored_balance(self):
        with mock.patch.object(credits, "_get_table", return_value=_Table({"balance": Decimal(12)})):
            self.assertEqual(credits.peek_balance("reader@example.com"), 12)
            self.assertEqual(credits.balance("reader@example.com"), 12)

    def test_no_address_is_unknown_and_funds_nothing(self):
        self.assertIsNone(credits.peek_balance(""))
        self.assertEqual(credits.balance(None), 0)

    def test_a_run_is_quoted_at_a_dollar(self):
        # As asked: the menu shows what is left as an amount, a run at $1.
        self.assertEqual(credits.USD_PER_CREDIT, 1)


class TopupUrlTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENTS_PAY_URL", None)

    def _url(self, host, email="reader@example.com"):
        with self.app.test_request_context("/docs", base_url=f"http://{host}"):
            return credits.topup_url(email)

    def test_trade_agents_buys_on_its_own_pay_host_and_comes_back(self):
        url = urlsplit(self._url("trade-agents.com"))
        self.assertEqual(f"{url.scheme}://{url.netloc}", "https://pay.trade-agents.com")
        query = parse_qs(url.query)
        self.assertEqual(query["email"], ["reader@example.com"])
        # https although Flask saw http: nginx terminates TLS in front of it.
        self.assertEqual(query["next"], ["https://trade-agents.com/agents"])

    def test_li_family_buys_on_its_own(self):
        url = urlsplit(self._url("stock.li-family.us"))
        self.assertEqual(f"{url.scheme}://{url.netloc}", "https://pay.li-family.us")
        self.assertEqual(parse_qs(url.query)["next"], ["https://stock.li-family.us/agents"])

    def test_a_local_host_sends_no_next(self):
        # ypay takes only an https `next`, and nothing local has TLS in front.
        self.assertNotIn("next", parse_qs(urlsplit(self._url("127.0.0.1:5071")).query))

    def test_the_staging_override_still_wins(self):
        os.environ["AGENTS_PAY_URL"] = "https://pay.staging.example"
        with mock.patch.object(credits, "PAY_URL", "https://pay.staging.example"):
            self.assertTrue(self._url("trade-agents.com").startswith("https://pay.staging.example?"))

    def test_the_summary_uses_the_same_link(self):
        with self.app.test_request_context("/docs", base_url="http://trade-agents.com"), \
             mock.patch.object(credits, "balance", return_value=3):
            got = credits.summary("reader@example.com")
            self.assertEqual(got["pay_url"], credits.topup_url("reader@example.com"))
        self.assertEqual(got["balance"], 3)


class RunPageLinkTests(unittest.TestCase):
    """The run page's top-up prompt is built from quota's payload."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENTS_PAY_URL", None)

    def test_the_buy_link_follows_the_brand(self):
        app = _app()
        for host, pay in (("trade-agents.com", "https://pay.trade-agents.com"),
                          ("stock.li-family.us", "https://pay.li-family.us")):
            with self.subTest(host=host), app.test_request_context("/", base_url=f"http://{host}"), \
                 mock.patch.object(credits, "balance", return_value=3):
                self.assertEqual(quota._credit_info("reader@example.com"),
                                 {"credits": 3, "pay_url": pay})


if __name__ == "__main__":
    unittest.main()
