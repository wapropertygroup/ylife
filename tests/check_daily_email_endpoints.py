"""End-to-end: /daily's "Send" and "Subscribe", through the real app.

tests/test_daily_email.py proves the mail; this proves routes.py calls it the
way the mail expects -- the argument order of _build_daily_email_cache, the
site read off the request, the site recorded on a subscription. The builder
being right and the builder being *called* are different questions.

Hermetic like check_wiki_pages.py: no background thread, no AWS, no .env. SES
and the subscribers table are fakes; the send's background thread runs inline.

Run:  venv/bin/python -m tests.check_daily_email_endpoints
"""
from __future__ import annotations

import os
import sys
import threading
import types
import unittest
from unittest import mock


class _Any:
    def __getattr__(self, _name): return _Any()
    def __call__(self, *_a, **_k): return _Any()
    def __getitem__(self, _k): return _Any()
    def __setitem__(self, _k, _v): return None
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
           "APP_BASE_URL"):
    os.environ.pop(_k, None)
os.environ.update(AWS_SHARED_CREDENTIALS_FILE=os.devnull, AWS_CONFIG_FILE=os.devnull,
                  AWS_EC2_METADATA_DISABLED="true", AGENTS_EMAIL_REPORT="0",
                  YSTOCKER_SECRET_KEY="check-daily-email", SES_FROM_EMAIL="daily@example.com")
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import ystocker                                           # noqa: E402
from ystocker import routes                               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class _SES:
    def __init__(self):
        self.sent = []

    def send_email(self, **kw):
        self.sent.append(kw)


class _Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["email"])
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[Item["email"]] = Item


PAYLOAD = {
    "lang": "zh",
    "indices": {"spx": {"current": 7709.51, "day_chg": 0.5}},
    "sectors": [{"label": "Tech", "day_chg": 1.08}],
    "vix": {"current": 15.81, "day_chg": -1.43},
    "gold_ratios": {"gold_price": 4201.9},
    "sentiment": {"fg": {"score": 35, "rating": "Fear"}},
    "events": [], "gainers": [], "losers": [],
}


class DailyEmailEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.client = cls.app.test_client()

    def _send(self, base_url, **extra):
        ses = _SES()
        fake_boto3 = types.SimpleNamespace(client=lambda *a, **k: ses)
        # The endpoint answers 202 and sends from a thread; run that inline.
        inline = lambda self: self.run()                      # noqa: E731
        with mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
                mock.patch.object(threading.Thread, "start", inline), \
                mock.patch.dict(routes._DAILY_SUMMARY_CACHE, {
                    "zh_us": {"data": {"summary": "美股**小幅**回调。"}},
                    "zh_cn": {"data": {"summary": "亚太分化。"}}}):
            r = self.client.post("/api/send-daily-email", base_url=base_url,
                                 json=dict(PAYLOAD, email="reader@example.com", **extra))
        self.assertEqual(r.status_code, 202, r.get_data(as_text=True))
        self.assertEqual(len(ses.sent), 1)
        return ses.sent[0]

    def test_send_from_trade_agents_mails_a_tradeagents_report(self):
        mail = self._send("http://trade-agents.com")
        html = mail["Message"]["Body"]["Html"]["Data"]
        self.assertTrue(mail["Message"]["Subject"]["Data"].startswith("TradeAgents 每日市场报告 — "))
        self.assertIn('href="https://trade-agents.com/daily?lang=zh"', html)
        self.assertIn("美股市场解读", html)
        self.assertIn("<strong", html)                 # the commentary was rendered
        self.assertIn("科技", html)                     # and the sectors localised
        self.assertIn("一次性发送", html)               # a one-off, so no unsubscribe link
        self.assertNotIn("__UNSUB__", html)
        self.assertNotIn('width="1200"', html)

    def test_send_from_stock_li_family_stays_ystocker(self):
        mail = self._send("http://stock.li-family.us")
        self.assertTrue(mail["Message"]["Subject"]["Data"].startswith("yStocker "))
        self.assertIn("https://stock.li-family.us/daily?lang=zh",
                      mail["Message"]["Body"]["Html"]["Data"])

    def test_a_subscription_records_its_site(self):
        table = _Table()
        with mock.patch.object(routes, "_get_subscribers_table", return_value=table):
            r = self.client.post("/api/subscribe", base_url="http://trade-agents.com",
                                 json={"email": "a@example.com", "lang": "zh"})
            self.assertEqual(r.status_code, 200)
            r = self.client.post("/api/subscribe", base_url="http://stock.li-family.us",
                                 json={"email": "b@example.com", "lang": "en"})
            self.assertEqual(r.status_code, 200)
            r = self.client.post("/api/subscribe", base_url="http://localhost",
                                 json={"email": "c@example.com", "lang": "en"})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(table.items["a@example.com"]["site"], "https://trade-agents.com")
        self.assertEqual(table.items["b@example.com"]["site"], "https://stock.li-family.us")
        # A local host records none, and the broadcast falls back.
        self.assertNotIn("site", table.items["c@example.com"])

    def test_the_broadcast_sends_each_subscriber_their_sites_mail(self):
        ses = _SES()
        mails = routes._build_daily_email_cache(
            PAYLOAD["indices"], PAYLOAD["sectors"], PAYLOAD["vix"], PAYLOAD["gold_ratios"],
            PAYLOAD["sentiment"], [], [], [], {"en": "US"}, {"en": "CN"}, "2026-09-29",
            sites={"https://trade-agents.com", "https://stock.li-family.us"})
        sent, errors = routes._ses_send_to_recipients(ses, [
            {"email": "a@example.com", "lang": "zh", "token": "tA", "site": "https://trade-agents.com"},
            {"email": "b@example.com", "lang": "en", "token": "tB", "site": "https://stock.li-family.us"},
        ], mails, "daily@example.com")
        self.assertEqual((sent, errors), (2, []))
        a, b = (m["Message"]["Body"]["Html"]["Data"] for m in ses.sent)
        self.assertIn("https://trade-agents.com/unsubscribe?token=tA", a)
        self.assertIn("https://stock.li-family.us/unsubscribe?token=tB", b)


if __name__ == "__main__":
    unittest.main()
