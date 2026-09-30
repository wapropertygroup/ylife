"""The daily markets email (ystocker/daily_email.py): layout, language, brand,
and the per-reader send.

No app, no network, no SES: the builder is pure, and the send takes its SES
client as an argument, so a fake records what would have gone out.

What this exists to catch, none of which raises:

* **A layout a phone cannot read.** The mail was 1,200px wide with the two
  commentaries in side-by-side cells, which cannot reflow: on a phone each was a
  column about 180px wide. It is one 640px column now, and stays that way.
* **Model text as markup.** The commentary went in unescaped, and its Markdown
  arrived as literal asterisks.
* **The wrong language.** The Chinese edition was dated "September 30, 2026"
  and named its sectors, ratings and event impact in English.
* **The wrong site.** Every mail said yStocker, and the broadcast's links fell
  back to ystocker.com, a domain this box does not serve -- so an unsubscribe
  link could lead nowhere.
"""
from __future__ import annotations

import os
import re
import unittest
from unittest import mock

from ystocker import daily_email as de

TA, YS = de.TA_SITE, de.YS_SITE
TODAY = "2026-09-29"

DATA = dict(
    indices={"spx": {"current": 7709.51, "day_chg": 0.5}, "dji": {"current": 51378.75, "day_chg": -0.27},
             "sse": {"current": 3842.19, "day_chg": 0.0}, "kospi": {"current": None}},
    sectors=[{"label": "Tech", "day_chg": 1.08, "week_chg_pct": 0.7},
             {"label": "Telecom", "day_chg": -0.35, "week_chg_pct": -1.2}],
    vix={"current": 15.81, "day_chg": -1.43},
    gold={"gold_price": 4201.9, "silver_price": 60.92, "current_gs": 68.97, "gs_day_chg": 0.08},
    sentiment={"fg": {"score": 35.5, "rating": "Fear"}, "pcr": {"current": 0.38, "ma20": 0.556},
               "aaii": {"bullish": 32.7, "bearish": 48.1, "bull_bear_spread": -15.4}},
    events=[{"date": "2026-09-30", "time": "8:30 AM", "event": "CPI", "zh": "消费者物价指数",
             "country": "Japan", "impact": "High"},
            {"date": "2026-09-01", "event": "Old", "impact": "High"}],
    gainers=[{"ticker": "ROG", "name": "Rogers Corporation", "price": 162.42, "day_chg": 17.56}],
    losers=[{"ticker": "FICO", "name": "Fair Isaac", "price": 1200.0, "day_chg": -26.52}],
    today_iso=TODAY,
)
US = "美国股市小幅回调。\n\n**情绪**偏谨慎，<script>alert(1)</script> 需要关注。"
CN = "亚太市场分化。"


def _mail(lang="zh", site=TA, **kw):
    args = dict(DATA, summary_us=US, summary_cn=CN)
    args.update(kw)
    return de.build(lang, site, **args)


def _live(html: str) -> str:
    """Only live markup: escaped text (&lt;...&gt;) is what a reader sees, not
    what a client runs -- see the trap CLAUDE.md records for test_report_email."""
    return re.sub(r"&lt;.*?&gt;", "", html)


class LayoutTests(unittest.TestCase):
    def test_one_640px_column(self):
        html = _mail()["html_tmpl"]
        self.assertIn('width="640"', html)
        self.assertIn("max-width:640px", html)
        self.assertNotIn('width="1200"', html)
        # The two commentaries are stacked rows, never two cells of one row.
        self.assertNotIn('width="49%"', html)
        self.assertLess(html.index("美股市场解读"), html.index("中国 / 亚洲市场解读"))

    def test_inline_styles_only_and_dark_on_purpose(self):
        html = _mail()["html_tmpl"]
        self.assertNotIn("<style", html)          # Gmail drops it
        self.assertIn('<meta name="color-scheme" content="dark">', html)

    def test_under_gmails_clip(self):
        # Gmail clips at ~102 KB; a mail with every section filled stays far below.
        big = dict(DATA, gainers=DATA["gainers"] * 10, losers=DATA["losers"] * 10)
        self.assertLess(len(_mail(**big)["html_tmpl"].encode()), 90_000)

    def test_the_web_version_is_one_click_away(self):
        html = _mail()["html_tmpl"]
        self.assertIn('href="https://trade-agents.com/daily?lang=zh"', html)
        self.assertIn("在网页上阅读完整报告", html)

    def test_empty_data_still_makes_a_mail(self):
        mail = de.build("en", TA, today_iso=TODAY)
        self.assertIn("__UNSUB__", mail["html_tmpl"])
        self.assertNotIn("Indices", mail["html_tmpl"])     # an empty section is left out
        self.assertTrue(mail["subject"].startswith("TradeAgents Daily Markets Report"))


class CommentaryTests(unittest.TestCase):
    def test_model_text_is_escaped(self):
        live = _live(_mail()["html_tmpl"])
        self.assertNotIn("<script", live)
        self.assertIn("&lt;script&gt;", _mail()["html_tmpl"])

    def test_markdown_is_rendered_not_printed(self):
        html = _mail()["html_tmpl"]
        self.assertIn("<strong", html)
        self.assertNotIn("**情绪**", html)
        # Two paragraphs, two <p>: the blank line is kept.
        us = html[html.index("美股市场解读"):html.index("中国 / 亚洲市场解读")]
        self.assertEqual(us.count("<p "), 2)

    def test_the_inbox_preview_is_the_commentary(self):
        preheader = _mail()["html_tmpl"].split("<table")[0]
        self.assertIn("美国股市小幅回调", preheader)
        # Plain text: an inbox prints Markdown's markers as they are.
        self.assertIn("情绪偏谨慎", preheader)


class LanguageTests(unittest.TestCase):
    def test_the_chinese_edition_is_chinese(self):
        mail = _mail("zh")
        html = mail["html_tmpl"]
        self.assertIn("2026年9月29日", mail["subject"])
        self.assertNotIn("September", html)
        self.assertIn("2026年9月29日 星期二", html)
        for zh in ("科技", "电信", "恐惧", "高", "日本", "消费者物价指数", "标普500", "每日市场报告"):
            self.assertIn(zh, html)
        for en in (">Tech<", ">Fear<", ">High<", "Japan"):
            self.assertNotIn(en, html)

    def test_the_english_edition_is_english(self):
        mail = _mail("en")
        self.assertEqual(mail["subject"], "TradeAgents Daily Markets Report — September 29, 2026")
        self.assertIn("Tuesday, September 29, 2026", mail["html_tmpl"])
        self.assertIn(">Tech<", mail["html_tmpl"])
        self.assertIn(">CPI<", _live(mail["html_tmpl"]).replace("· ", ">"))

    def test_both_languages_have_every_string(self):
        self.assertEqual(set(de._T["en"]), set(de._T["zh"]))

    def test_past_events_are_left_out(self):
        self.assertNotIn("Old", _mail("en")["html_tmpl"])


class FigureTests(unittest.TestCase):
    def test_signed_changes_take_the_up_and_down_ink(self):
        self.assertIn(f"color:{de._UP}\">+0.50%", de._chg(0.5))
        self.assertIn(f"color:{de._DOWN}\">-0.27%", de._chg(-0.27))
        self.assertIn(f"color:{de._DIM}\">0.00%", de._chg(0))     # unchanged is neither
        self.assertIn("—", de._chg(None))

    def test_an_index_without_a_price_is_skipped(self):
        self.assertNotIn("韩国综合", _mail("zh")["html_tmpl"])


class SiteTests(unittest.TestCase):
    def test_the_host_names_the_site(self):
        self.assertEqual(de.site_for_host("trade-agents.com"), TA)
        self.assertEqual(de.site_for_host("www.trade-agents.com:443"), TA)
        self.assertEqual(de.site_for_host("stock.li-family.us"), "https://stock.li-family.us")
        for local in ("localhost:5000", "127.0.0.1", "", None):
            self.assertEqual(de.site_for_host(local), "")

    def test_no_recorded_site_falls_back_to_one_this_box_serves(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_BASE_URL", None)
            self.assertEqual(de.default_site(), YS)
            self.assertNotIn("ystocker.com", de.default_site().replace("li-family", ""))
        with mock.patch.dict(os.environ, {"APP_BASE_URL": "https://staging.li-family.us/"}):
            self.assertEqual(de.default_site(), "https://staging.li-family.us")

    def test_the_brand_follows_the_site(self):
        self.assertTrue(_mail("en", TA)["subject"].startswith("TradeAgents "))
        ys = _mail("en", YS)
        self.assertTrue(ys["subject"].startswith("yStocker "))
        self.assertIn(f'href="{YS}/daily?lang=en"', ys["html_tmpl"])
        self.assertNotIn("trade-agents.com", ys["html_tmpl"])

    def test_every_site_gets_both_languages(self):
        mails = de.build_all([TA, YS, TA + "/"], {"en": "US en"}, {"en": "CN en"}, **DATA)
        self.assertEqual(set(mails), {(TA, "en"), (TA, "zh"), (YS, "en"), (YS, "zh")})
        # A language with no commentary of its own borrows the fallback's.
        self.assertIn("US en", mails[(TA, "zh")]["html_tmpl"])


class _SES:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send_email(self, **kw):
        to = kw["Destination"]["ToAddresses"][0]
        if to in self.fail_for:
            raise RuntimeError("throttled")
        self.sent.append(kw)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.mails = de.build_all([TA, YS], {"en": US, "zh": US}, {"en": CN, "zh": CN}, **DATA)

    def test_each_reader_gets_their_sites_mail_and_unsubscribe_link(self):
        ses = _SES()
        sent, errors = de.send_all(ses, [
            {"email": "a@example.com", "lang": "zh", "token": "tokA", "site": TA},
            {"email": "b@example.com", "lang": "en", "token": "tokB", "site": YS},
        ], self.mails, "from@example.com")
        self.assertEqual((sent, errors), (2, []))
        a, b = ses.sent
        a_html = a["Message"]["Body"]["Html"]["Data"]
        b_html = b["Message"]["Body"]["Html"]["Data"]
        self.assertTrue(a["Message"]["Subject"]["Data"].startswith("TradeAgents 每日市场报告"))
        self.assertIn(f"{TA}/unsubscribe?token=tokA", a_html)
        self.assertIn("退订每日报告", a_html)
        self.assertTrue(b["Message"]["Subject"]["Data"].startswith("yStocker Daily Markets Report"))
        self.assertIn(f"{YS}/unsubscribe?token=tokB", b_html)
        for html in (a_html, b_html):
            self.assertNotIn("__UNSUB__", html)
        self.assertIn(f"{YS}/unsubscribe?token=tokB", b["Message"]["Body"]["Text"]["Data"])

    def test_a_one_off_send_says_so(self):
        ses = _SES()
        de.send_all(ses, [{"email": "a@example.com", "lang": "en", "token": "", "site": TA}],
                    self.mails, "from@example.com")
        html = ses.sent[0]["Message"]["Body"]["Html"]["Data"]
        self.assertIn("one-time send", html)
        self.assertNotIn("unsubscribe?token", html)

    def test_one_bad_address_does_not_stop_the_rest(self):
        ses = _SES(fail_for={"bad@example.com"})
        sent, errors = de.send_all(ses, [
            {"email": "bad@example.com", "lang": "en", "site": TA},
            {"email": "ok@example.com", "lang": "en", "site": TA},
        ], self.mails, "from@example.com")
        self.assertEqual((sent, errors), (1, ["bad@example.com"]))

    def test_a_reader_with_no_site_gets_the_default(self):
        ses = _SES()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_BASE_URL", None)
            de.send_all(ses, [{"email": "a@example.com", "lang": "en", "token": "t"}],
                        self.mails, "from@example.com")
        self.assertIn(f"{YS}/unsubscribe?token=t", ses.sent[0]["Message"]["Body"]["Html"]["Data"])


if __name__ == "__main__":
    unittest.main()
