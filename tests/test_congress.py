"""Tests for ystocker.congress -- House members' trades from their PTRs.

No app, no network, no pdfplumber: the reports are the text the box extracted
from 14 real PTRs on 2026-10-07 (tests/fixtures/house_ptr/reports.json, with
their rows of the Clerk's 2026FD.xml). What is pinned:

* the index keeps PTRs only, with ISO dates, and knows a paper filing;
* every trade line of every report is read -- the count of trades equals the
  count of lines carrying a trade's two dates;
* the traps, each on the report that has it: an amount whose upper bound
  wraps (alone on its line, or after more of the asset's name), an asset split
  by a page break's repeated header, the NUL-filled labels, options, owners,
  subholdings and wrapped descriptions;
* what a trade says about a company (stock both ways, bought calls and puts,
  nothing for bonds or a sale of options);
* a refresh pass against a faked Clerk: paper filings are never downloaded, a
  failed report is retried only after a day, the budget and a cool-down stop
  it, and workers read the combined feed on its mtime.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ystocker import congress, fetchguard

FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "house_ptr" / "reports.json").read_text())
REPORTS: dict[str, str] = FIX["reports"]
ROWS: list[dict] = FIX["rows"]


def _xml(rows: list[dict]) -> bytes:
    out = ['<?xml version="1.0" encoding="utf-8"?>\n<FinancialDisclosure>']
    for r in rows:
        out.append("<Member>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + "</Member>")
    out.append("</FinancialDisclosure>")
    return "".join(out).encode()


def _trades(doc: str) -> list[dict]:
    return congress.parse_ptr(REPORTS[doc])["trades"]


class IndexTests(unittest.TestCase):

    def test_ptrs_only_newest_first_with_iso_dates(self):
        other = dict(ROWS[0], FilingType="C", DocID="10070000")
        rows = congress.parse_index(_xml(ROWS + [other]))
        self.assertEqual(len(rows), len(ROWS))
        self.assertNotIn("10070000", [r["doc"] for r in rows])
        self.assertEqual([r["filed"] for r in rows], sorted((r["filed"] for r in rows), reverse=True))
        pelosi = next(r for r in rows if r["doc"] == "20035553")
        self.assertEqual((pelosi["name"], pelosi["district"], pelosi["filed"], pelosi["year"]),
                         ("Nancy Pelosi", "CA11", "2026-10-02", 2026))
        self.assertFalse(pelosi["paper"])

    def test_a_paper_filing_is_known_by_its_docid(self):
        rows = {r["doc"]: r for r in congress.parse_index(_xml(ROWS))}
        self.assertTrue(rows["9116361"]["paper"])
        self.assertTrue(congress.is_paper("8221234"))
        self.assertFalse(congress.is_paper("20035553"))

    def test_dates(self):
        self.assertEqual(congress.iso_date("9/9/2026"), "2026-09-09")
        self.assertEqual(congress.iso_date("10/02/2026"), "2026-10-02")
        self.assertIsNone(congress.iso_date("not a date"))


class EveryReportTests(unittest.TestCase):

    def test_every_trade_line_is_read(self):
        import re
        both = re.compile(r"\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}")
        for doc, text in REPORTS.items():
            lines = [ln for ln in text.replace("\x00", " ").splitlines() if both.search(ln)]
            parsed = congress.parse_ptr(text)
            self.assertEqual(len(parsed["trades"]), len(lines), doc)

    def test_every_trade_is_whole(self):
        for doc, text in REPORTS.items():
            for t in congress.parse_ptr(text)["trades"]:
                self.assertTrue(t["tx"] and t["notified"], (doc, t))
                self.assertIsNotNone(t["lo"], (doc, t))
                self.assertIsNotNone(t["hi"], (doc, t["amount"]))
                self.assertIsNotNone(t["code"], (doc, t["asset"]))
                self.assertEqual(t["status"], "New", (doc, t))
                if t["code"] in congress.EQUITY_CODES:
                    self.assertTrue(t["ticker"], (doc, t["asset"]))
                self.assertNotIn("[", t["asset"], (doc, t["asset"]))

    def test_the_member_and_signature(self):
        p = congress.parse_ptr(REPORTS["20035553"])
        self.assertEqual((p["name"], p["district"], p["member_status"], p["signed"], p["filing_id"]),
                         ("Nancy Pelosi", "CA11", "Member", "2026-10-02", "20035553"))

    def test_a_paper_filing_has_nothing_to_read(self):
        p = congress.parse_ptr(REPORTS["9116361"])
        self.assertEqual(p["trades"], [])
        self.assertIsNone(p["name"])


class TrapTests(unittest.TestCase):

    def test_an_upper_bound_alone_on_the_next_line(self):
        (t,) = _trades("20035553")
        self.assertEqual((t["owner"], t["asset"], t["code"], t["ticker"]), ("SP", "REOF XXX, LLC", "AB", None))
        self.assertEqual((t["lo"], t["hi"]), (500001, 1000000))
        # The description wraps too, and is kept whole.
        self.assertTrue(t["desc"].endswith("225 Bush Street in San Francisco, CA."), t["desc"])

    def test_an_upper_bound_after_more_of_the_name(self):
        bond = next(t for t in _trades("20035491") if t["code"] == "GS")
        self.assertEqual((bond["lo"], bond["hi"]), (15001, 50000))
        self.assertEqual(bond["asset"], "ARBUCKLE MEM HOSP AUTH OKLA SALES TAX 03.00000% 01/01/2027 REV BDS SER. 2018")
        self.assertEqual(bond["sub"], "Hern Family Revocable Trust")
        self.assertEqual(bond["owner"], "JT")

    def test_a_page_break_inside_a_trade(self):
        rsg = next(t for t in _trades("20035455") if t["ticker"] == "RSG")
        self.assertEqual(rsg["asset"], "Republic Services, Inc. Common Stock")
        self.assertEqual(rsg["sub"], "Morgan Stanley - Select UMA Account # 1")

    def test_options_name_their_side(self):
        msft = [t for t in _trades("20035455") if t["ticker"] == "MSFT"]
        self.assertEqual(len(msft), 4)
        self.assertTrue(all(t["code"] == "OP" and t["option"] == "call" for t in msft))
        self.assertEqual([congress.direction(t) for t in msft], ["buy", "buy", None, None])
        be = [t for t in _trades("20035143") if t["ticker"] == "BE"]
        self.assertEqual([(t["code"], congress.direction(t)) for t in be],
                         [("ST", "buy"), ("OP", "buy"), ("ST", "buy"), ("OP", "buy")])

    def test_owners_and_partial_sales(self):
        hern = _trades("20035491")
        self.assertEqual({t["owner"] for t in hern}, {"", "DC", "JT"})
        aapl = [t for t in hern if t["ticker"] == "AAPL"]
        self.assertTrue(aapl and all(t["type"] == "S (partial)" and t["kind"] == "sell" for t in aapl))
        self.assertEqual({t["sub"] for t in aapl}, {"Kelby Austin Hern Trust", "Kaden Everett Hern Trust"})

    def test_a_trade_by_the_member_has_no_owner_code(self):
        t = _trades("20035580")[0]
        self.assertEqual(t["owner"], "")
        self.assertEqual(t["desc"], "Automatic reinvestment of dividends earned.")


class AmountAndDirectionTests(unittest.TestCase):

    def test_amounts(self):
        self.assertEqual(congress.parse_amount("$1,001 - $15,000"), (1001, 15000))
        self.assertEqual(congress.parse_amount("$15,001 -"), (15001, None))
        self.assertEqual(congress.parse_amount("Over $50,000,000"), (50000001, None))
        self.assertEqual(congress.parse_amount("Spouse/DC Over $1,000,000"), (1000001, None))
        self.assertEqual(congress.parse_amount(""), (None, None))

    def test_direction(self):
        d = congress.direction
        self.assertEqual(d({"code": "ST", "kind": "buy"}), "buy")
        self.assertEqual(d({"code": "ST", "kind": "sell"}), "sell")
        self.assertIsNone(d({"code": "ST", "kind": "exchange"}))
        self.assertEqual(d({"code": "OP", "kind": "buy", "option": "put"}), "sell")
        self.assertIsNone(d({"code": "OP", "kind": "buy", "option": None}))
        self.assertIsNone(d({"code": "OP", "kind": "sell", "option": "call"}))
        self.assertIsNone(d({"code": "GS", "kind": "buy"}))

    def test_tickers_in_yahoos_form(self):
        self.assertEqual(congress.normalise_ticker("BRK.B"), "BRK-B")
        self.assertIsNone(congress.normalise_ticker(None))

    def test_lag(self):
        self.assertEqual(congress.lag_days("2026-08-14", "2026-09-14"), 31)
        self.assertIsNone(congress.lag_days(None, "2026-09-14"))


class FeedRowTests(unittest.TestCase):

    def test_rows_are_compact_and_newest_filing_first(self):
        filings = []
        for r in congress.parse_index(_xml(ROWS)):
            parsed = congress.parse_ptr(REPORTS[r["doc"]])
            filings.append(dict(r, trades=parsed["trades"]))
        rows = congress.feed_rows(filings)
        self.assertEqual(len(rows), sum(len(f["trades"]) for f in filings))
        self.assertEqual([r["f"] for r in rows], sorted((r["f"] for r in rows), reverse=True))
        msft = next(r for r in rows if r["t"] == "MSFT" and r["c"] == "OP" and r["dir"] == "buy")
        self.assertEqual((msft["m"], msft["md"], msft["o"], msft["c"], msft["op"]), ("Josh Gottheimer", "NJ05", "JT", "OP", "call"))
        self.assertEqual(congress.report_url(msft["doc"], msft["yr"]),
                         "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035455.pdf")


class _FakeClerk:
    """The two URLs the module asks for, from the fixtures."""

    def __init__(self, missing=(), last_year=()):
        self.calls: list[str] = []
        self.missing = set(missing)
        self.last_year = list(last_year)

    def get(self, url):
        self.calls.append(url)
        if url.endswith("FD.zip"):
            year = url.rsplit("/", 1)[-1][:4]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr(f"{year}FD.xml", _xml(ROWS if year == "2026" else self.last_year))
            return mock.Mock(status_code=200, content=buf.getvalue())
        doc = url.rsplit("/", 1)[-1][:-4]
        if doc in self.missing:
            return mock.Mock(status_code=404, content=b"")
        return mock.Mock(status_code=200, content=doc.encode())


class RefreshTests(unittest.TestCase):
    NOW = time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for p in (mock.patch.object(congress, "CACHE_DIR", self.dir),
                  mock.patch.object(congress, "REQUEST_SPACING_SECONDS", 0),
                  mock.patch.object(congress, "pdf_text", side_effect=lambda content: REPORTS[content.decode()]),
                  mock.patch.object(congress, "_feed_mem", None), mock.patch.object(congress, "_feed_mtime", None)):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, clerk, **kw):
        with mock.patch.object(congress, "_get", side_effect=clerk.get):
            return congress.refresh_once(now=self.NOW, **kw)

    def test_a_pass_caches_every_report_and_writes_the_feed(self):
        clerk = _FakeClerk()
        counts = self._run(clerk)
        electronic = [r for r in ROWS if not r["DocID"].startswith("9")]
        self.assertEqual(counts["fetched"], len(ROWS))
        # One index per year in the window, and no download for a paper filing.
        self.assertEqual(sum(u.endswith("FD.zip") for u in clerk.calls), 2)
        self.assertEqual(sum(u.endswith(".pdf") for u in clerk.calls), len(electronic))
        self.assertFalse(any("9116361" in u for u in clerk.calls))
        feed = congress.peek()
        self.assertEqual(feed["counts"], {"filings": len(ROWS), "paper": 1, "failed": 0, "pending": 0})
        self.assertEqual(feed["latest_filed"], "2026-10-05")
        self.assertEqual([p["doc"] for p in feed["paper"]], ["9116361"])
        self.assertIn("Nancy Pelosi", [m["name"] for m in feed["members"]])
        self.assertTrue(any(r["t"] == "BE" for r in feed["rows"]))

    def test_a_second_pass_fetches_nothing_new(self):
        self._run(_FakeClerk())
        clerk = _FakeClerk()
        self.assertEqual(self._run(clerk)["fetched"], 0)
        self.assertFalse(any(u.endswith(".pdf") for u in clerk.calls))

    def test_a_failed_report_waits_a_day(self):
        self._run(_FakeClerk(missing={"20035553"}))
        feed = congress.peek()
        self.assertEqual(feed["counts"]["failed"], 1)
        clerk = _FakeClerk()
        self._run(clerk)
        self.assertFalse(any("20035553" in u for u in clerk.calls), "retried within the day")
        with mock.patch.object(congress, "_get", side_effect=clerk.get):
            congress.refresh_once(now=self.NOW + congress.RETRY_FAILED_SECONDS + 60)
        self.assertTrue(any("20035553" in u for u in clerk.calls))

    def test_the_budget_stops_the_pass(self):
        self.assertEqual(self._run(_FakeClerk(), budget=3)["fetched"], 3)
        self.assertEqual(congress.peek()["counts"]["pending"], len(ROWS) - 3)

    def test_a_cool_down_stops_the_pass_and_keeps_what_it_has(self):
        clerk = _FakeClerk()
        n = {"pdf": 0}

        def get(url):
            if url.endswith(".pdf"):
                n["pdf"] += 1
                if n["pdf"] > 2:
                    raise fetchguard.CooldownActive(congress.PROVIDER, 60, "HTTP 429")
            return clerk.get(url)

        with mock.patch.object(congress, "_get", side_effect=get), \
             self.assertLogs("ystocker.congress", "WARNING"):
            counts = congress.refresh_once(now=self.NOW)
        self.assertEqual(n["pdf"], 3)                       # the third download met the cool-down
        self.assertLess(counts["fetched"], len(ROWS))
        self.assertEqual(len(list((self.dir / "ptr").glob("*.json"))), counts["fetched"])
        self.assertEqual(congress.peek()["counts"]["pending"], len(ROWS) - counts["fetched"])

    def test_a_report_in_both_years_files_is_counted_once(self):
        clerk = _FakeClerk(last_year=ROWS[:3])
        self._run(clerk)
        self.assertEqual(congress.peek()["counts"]["filings"], len(ROWS))
        self.assertEqual(sum(u.endswith(".pdf") for u in clerk.calls),
                         len([r for r in ROWS if not r["DocID"].startswith("9")]))

    def test_peek_re_reads_a_rewritten_feed(self):
        self._run(_FakeClerk(), budget=2)
        first = congress.peek()["counts"]["filings"]
        self._run(_FakeClerk())
        later = time.time() + 5
        os.utime(self.dir / "feed.json", (later, later))
        self.assertGreater(congress.peek()["counts"]["filings"], first)

    def test_no_feed_is_none(self):
        self.assertIsNone(congress.peek())


if __name__ == "__main__":
    unittest.main()
