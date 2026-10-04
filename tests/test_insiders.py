"""ystocker/insiders.py -- insider trades, from SEC Form 4.

Every filing below is real: fetched from EDGAR on the box on 2026-10-04 and
kept verbatim under tests/fixtures/insiders/, where index.json holds each one's
listing row from its issuer's submissions JSON. They were chosen for the traps:

* DKS (Barnes): a plain open-market purchase, no footnotes at all;
* GME: Ryan Cohen's purchases -- one with a weighted-average price footnote,
  one filed as two lines on one day -- Nat Turner's first purchase (a new
  position), and three more buyers in September: five insiders, the same
  cluster OpenInsider lists for GME;
* AAPL (Newstead): a 10b5-1 sale marked by the box (``aff10b5One`` "true") and
  by a footnote; NVDA (Teter): the box as "1", the footnote on the transaction
  code rather than the price, three price bands; NKE (McCartney): "10b5-1
  trading plan" without the word Rule;
* NVDA (Stevens): a sale larger than the holding left after it, where
  shares / holding-after reads 141%;
* TPR (Kulikowsky): an option exercise, two sales to cover and a tax
  withholding in one filing; SEG (Hirsh): an award at $0;
* LEN: Berkshire Hathaway and Warren Buffett filing jointly, buying two share
  classes over three days ($53,882,588 in all, as OpenInsider totals it);
* PYXS, ZDGE: code P with the price only in a footnote, bought in a public
  offering and a private placement; PYXS has four joint filers;
* NKE (Parker) and TPR (Kulikowsky): an original and its 4/A each -- one 4/A
  restates the line, the other only the derivative table;
* AAPL (Khan): a filing with no non-derivative line at all.

No network, no app. The cache tests use a temporary directory and a fake
fetcher that serves these files by URL and counts what it was asked for.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import requests

from ystocker import fetchguard
from ystocker import insiders as ins

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "insiders"
INDEX = json.loads((FIXTURES / "index.json").read_text())

DKS_BUY = "0002100573-26-000006"
GME_COHEN_1002 = "0000921895-26-002719"
GME_COHEN_0929 = "0000921895-26-002670"
GME_TURNER = "0001822293-26-000002"
GME_ALL = ["0001868288-26-000004", "0001840480-26-000002", "0001840485-26-000006",
           "0000921895-26-002531", "0001840485-26-000008", "0000921895-26-002608",
           GME_COHEN_0929, GME_TURNER, GME_COHEN_1002]
AAPL_PLAN_SALE = "0001140361-26-038307"
AAPL_NO_LINES = "0001140361-26-038028"
NVDA_TETER = "0001696841-26-000014"
NVDA_STEVENS = "0001199039-26-000016"
NKE_PLAN_SALE = "0000320187-26-000167"
TPR_EXERCISE = "0001225208-26-007731"
SEG_AWARD = "0001848554-26-000011"
LEN_BERKSHIRE = "0001193125-26-409451"
PYXS_OFFERING = "0000919574-26-006609"
ZDGE_PLACEMENT = "0001193125-26-407951"
NKE_ORIGINAL, NKE_AMENDMENT = "0000320187-26-000055", "0000320187-26-000129"
TPR_ORIGINAL, TPR_AMENDMENT = "0001225208-26-007221", "0001225208-26-007667"
HLT_FRACTION = "0001244673-26-000013"
OXY_TAX = "0001814606-26-000004"


def raw(acc: str) -> bytes:
    return (FIXTURES / f"{acc}.xml").read_bytes()


def filing(acc: str) -> dict:
    """A parsed fixture with the fields a refresh adds from its listing row."""
    parsed = ins.parse_form4(raw(acc))
    row = INDEX[acc]
    parsed.update({"filed": row["filed"], "accepted": row["accepted"], "doc": row["doc"]})
    return parsed


def record(cik: int, accs: list[str], *, ticker: str, name: str, checked: float | None = None,
           covered: str = "2026-07-07") -> dict:
    return {"v": ins.CACHE_VER, "cik": cik, "ticker": ticker, "name": name,
            "filings": {a: filing(a) for a in accs}, "unread": {},
            "checked": checked if checked is not None else time.time(), "covered_since": covered}


def et(y: int, m: int, d: int, hour: int = 12) -> float:
    return dt.datetime(y, m, d, hour, tzinfo=ZoneInfo("America/New_York")).timestamp()


class ParseTests(unittest.TestCase):
    def test_an_open_market_purchase(self):
        f = ins.parse_form4(raw(DKS_BUY))
        self.assertEqual(f["form"], "4")
        self.assertEqual(f["issuer"], {"cik": 1089063, "name": "DICK'S SPORTING GOODS, INC.", "symbol": "DKS"})
        (owner,) = f["owners"]
        self.assertEqual((owner["name"], owner["officer"], owner["director"], owner["title"]),
                         ("Barnes Matthew", True, False, "President - Foot Locker Intl"))
        (tx,) = f["tx"]
        self.assertEqual((tx["code"], tx["date"], tx["shares"], tx["price"], tx["ad"], tx["after"], tx["own"]),
                         ("P", "2026-09-30", 3665.0, 136.3899, "A", 11965.0, "D"))
        self.assertFalse(tx["plan"])
        self.assertIsNone(tx["price_note"])
        self.assertIs(f["aff10b5_1"], False)          # "0", not absent
        self.assertFalse(f["plan"])

    def test_a_price_carries_its_footnote(self):
        (tx,) = ins.parse_form4(raw(GME_COHEN_1002))["tx"]
        self.assertEqual((tx["price"], tx["shares"]), (24.4061, 700000.0))
        self.assertTrue(tx["price_note"].startswith("Represents a weighted average price."))
        self.assertIn("$24.3600 to $24.4400", tx["price_note"])

    def test_a_10b5_1_sale_marked_by_box_and_footnote(self):
        f = ins.parse_form4(raw(AAPL_PLAN_SALE))
        self.assertIs(f["aff10b5_1"], True)           # written "true"
        self.assertTrue(f["plan"])
        (tx,) = f["tx"]
        self.assertEqual((tx["code"], tx["shares"], tx["price"]), ("S", 2399.0, 336.18))
        self.assertTrue(tx["plan"])

    def test_the_plan_footnote_can_hang_on_the_transaction_code(self):
        f = ins.parse_form4(raw(NVDA_TETER))
        self.assertIs(f["aff10b5_1"], True)           # written "1"
        self.assertEqual(len(f["tx"]), 3)
        self.assertTrue(all(t["plan"] for t in f["tx"]))   # F1 sits in <transactionCoding>
        self.assertEqual([t["own"] for t in f["tx"]], ["I"] * 3)
        self.assertEqual(f["tx"][0]["nature"], "By Trust")
        self.assertIn("$221.59 to $222.58", f["tx"][0]["price_note"])

    def test_10b5_1_without_the_word_rule(self):
        (tx,) = ins.parse_form4(raw(NKE_PLAN_SALE))["tx"]
        self.assertTrue(tx["plan"])
        self.assertEqual(tx["after"], 78120.9272)     # fractional holdings are real

    def test_an_exercise_sales_to_cover_and_a_tax_withholding(self):
        f = ins.parse_form4(raw(TPR_EXERCISE))
        self.assertEqual([t["code"] for t in f["tx"]], ["M", "S", "S", "F"])
        self.assertEqual([ins.kind(t["code"]) for t in f["tx"]], ["other", "sell", "sell", "other"])
        self.assertEqual(f["tx"][0]["price"], 40.58)
        # The filer writes only <isOfficer>: an absent flag is a no.
        (owner,) = f["owners"]
        self.assertEqual((owner["officer"], owner["director"], owner["ten_pct"]), (True, False, False))
        self.assertFalse(any(t["plan"] for t in f["tx"]))

    def test_an_award_at_zero_is_a_real_zero(self):
        (tx,) = ins.parse_form4(raw(SEG_AWARD))["tx"]
        self.assertEqual((tx["code"], tx["price"]), ("A", 0.0))
        self.assertEqual(ins.trade_value(tx["shares"], tx["price"]), 0.0)

    def test_joint_filers_are_all_kept(self):
        f = ins.parse_form4(raw(LEN_BERKSHIRE))
        self.assertEqual([(o["name"], o["cik"]) for o in f["owners"]],
                         [("BERKSHIRE HATHAWAY INC", 1067983), ("BUFFETT WARREN E", 315090)])
        self.assertTrue(all(o["ten_pct"] for o in f["owners"]))
        self.assertEqual([t["security"] for t in f["tx"]],
                         ["Class A Common Stock"] * 3 + ["Class B Common Stock"] * 2)
        self.assertEqual(len(ins.parse_form4(raw(PYXS_OFFERING))["owners"]), 4)

    def test_a_footnote_only_price_is_none_not_zero(self):
        for acc in (PYXS_OFFERING, ZDGE_PLACEMENT):
            (tx,) = ins.parse_form4(raw(acc))["tx"]
            self.assertEqual(tx["code"], "P", acc)
            self.assertIsNone(tx["price"], acc)
            self.assertIsNone(ins.trade_value(tx["shares"], tx["price"]), acc)
            self.assertTrue(tx["offering"], acc)
            self.assertTrue(tx["price_note"], acc)

    def test_an_amendment_names_its_original(self):
        f = ins.parse_form4(raw(NKE_AMENDMENT))
        self.assertEqual((f["form"], f["period"], f["original"]), ("4/A", "2026-05-14", "2026-05-15"))

    def test_a_filing_with_no_non_derivative_line(self):
        f = ins.parse_form4(raw(AAPL_NO_LINES))
        self.assertEqual((f["tx"], f["derivatives"]), ([], 2))
        self.assertEqual(ins.filing_trades(f), [])

    def test_fractional_shares(self):
        (tx,) = ins.parse_form4(raw(HLT_FRACTION))["tx"]
        self.assertEqual(tx["shares"], 9.256)

    def test_a_namespace_changes_nothing(self):
        text = raw(DKS_BUY).decode().replace(
            "<ownershipDocument>", '<ownershipDocument xmlns="http://www.sec.gov/edgar/ownership">', 1)
        self.assertEqual(ins.parse_form4(text)["tx"], ins.parse_form4(raw(DKS_BUY))["tx"])

    def test_the_rendered_html_is_refused(self):
        # primaryDocument names the XSL-rendered copy; parsing it as data is
        # how 13F parsing broke, and it must not become an empty filing.
        with self.assertRaises(ValueError):
            ins.parse_form4("<html><body><table><tr><td>FORM 4</td></tr></table></body></html>")
        for junk in ("", "   ", "not xml at all", b"\xef\xbb\xbf<html/>"):
            with self.assertRaises(ValueError):
                ins.parse_form4(junk)

    def test_a_form_3_is_not_a_form_4(self):
        text = raw(DKS_BUY).decode().replace("<documentType>4</documentType>", "<documentType>3</documentType>")
        with self.assertRaises(ValueError):
            ins.parse_form4(text)

    def test_the_box_alone_marks_the_purchases_and_sales(self):
        # The same filings with the footnote that says which line removed: the
        # box is all that is left, and it can only mean a purchase or a sale.
        text = raw(AAPL_PLAN_SALE).decode().replace('<footnoteId id="F1"/>', "")
        text = text.replace("pursuant to a Rule 10b5-1 trading plan", "under the issuer's policy")
        (tx,) = ins.parse_form4(text)["tx"]
        self.assertTrue(tx["plan"])
        tpr = raw(TPR_EXERCISE).decode().replace("<aff10b5One>0</aff10b5One>", "<aff10b5One>1</aff10b5One>")
        self.assertEqual([t["plan"] for t in ins.parse_form4(tpr)["tx"]], [False, True, True, False])

    def test_a_footnote_denying_a_plan_is_not_a_plan(self):
        self.assertTrue(ins.mentions_plan("effected pursuant to a Rule 10b5-1 trading plan adopted May 22, 2026"))
        self.assertTrue(ins.mentions_plan("pursuant to a 10b5-1 trading plan"))
        self.assertFalse(ins.mentions_plan("These sales were not made pursuant to a Rule 10b5-1 trading plan."))
        self.assertFalse(ins.mentions_plan("sold other than under the reporting person's 10b5-1 plan"))
        self.assertFalse(ins.mentions_plan("weighted average price"))
        self.assertFalse(ins.mentions_plan(None))


class ClassifyAndArithmeticTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual([ins.kind(c) for c in ("P", "S", "A", "M", "F", "G", "C", "D", "J", "", None, "p")],
                         ["buy", "sell"] + ["other"] * 9 + ["buy"])

    def test_value_needs_both_numbers(self):
        self.assertEqual(ins.trade_value(3665.0, 136.3899), 3665.0 * 136.3899)
        self.assertIsNone(ins.trade_value(3665.0, None))
        self.assertIsNone(ins.trade_value(None, 10.0))

    def test_stake_for_a_buy_is_against_the_holding_after(self):
        self.assertEqual(ins.stake_pct(3665.0, 11965.0, True), 30.63)   # DKS: 3665 / 11965
        self.assertEqual(ins.stake_pct(10462.0, 10462.0, True), 100.0)  # GME's Turner: a new position
        self.assertIsNone(ins.stake_pct(10.0, 5.0, True))              # a buy bigger than the holding after

    def test_stake_for_a_sale_is_against_the_holding_before(self):
        self.assertEqual(ins.stake_pct(2399.0, 41992.0, False), 5.4)    # AAPL: 2399 / (41992 + 2399)
        self.assertEqual(ins.stake_pct(500.0, 0.0, False), 100.0)       # a full exit
        # NVDA's Stevens: the holding-after formula has no ceiling.
        f = filing(NVDA_STEVENS)
        (sale,) = ins.filing_trades(f)
        self.assertEqual((sale["shares"], sale["after"]), (1366000.0, 970531.0))
        self.assertGreater(sale["shares"] / sale["after"], 1.4)
        self.assertEqual(sale["stake"], 58.46)

    def test_stake_is_unknown_without_its_numbers(self):
        self.assertIsNone(ins.stake_pct(None, 100.0, True))
        self.assertIsNone(ins.stake_pct(100.0, None, False))
        self.assertIsNone(ins.stake_pct(0.0, 100.0, False))


class TradeTests(unittest.TestCase):
    def test_two_lines_one_day_are_one_trade(self):
        (buy,) = ins.filing_trades(filing(GME_COHEN_0929))
        self.assertEqual(buy["shares"], 450000.0)
        self.assertAlmostEqual(buy["value"], 446500 * 23.4753 + 3500 * 23.4499, places=4)
        self.assertAlmostEqual(buy["price"], buy["value"] / 450000.0, places=6)
        self.assertEqual(buy["after"], 40948522.0)    # the higher holding after a run of buys
        self.assertFalse(buy["partial"])

    def test_price_bands_are_one_sale(self):
        f = filing(NVDA_TETER)
        (sale,) = ins.filing_trades(f)
        self.assertEqual(sale["shares"], 12483 + 13478 + 4499)
        self.assertAlmostEqual(sale["value"], sum(t["shares"] * t["price"] for t in f["tx"]), places=4)
        self.assertEqual(sale["after"], 2687660.0)    # the lowest holding after a run of sales
        self.assertTrue(sale["plan"])
        self.assertTrue(sale["price_note"].endswith("(+2 more)"))

    def test_two_classes_stay_apart_and_days_run_together(self):
        trades = ins.filing_trades(filing(LEN_BERKSHIRE))
        by_class = {t["security"]: t for t in trades}
        a, b = by_class["Class A Common Stock"], by_class["Class B Common Stock"]
        self.assertEqual((a["date"], a["date_last"], a["shares"], a["after"]),
                         ("2026-09-28", "2026-09-30", 656302.0, 26034436.0))
        self.assertEqual((b["date"], b["date_last"], b["shares"], b["after"]),
                         ("2026-09-29", "2026-09-30", 4108.0, 553000.0))
        # The total OpenInsider shows for this filing, to the dollar.
        self.assertEqual(round(a["value"] + b["value"]), 53882588)

    def test_an_exercise_filing_splits_by_code(self):
        trades = ins.filing_trades(filing(TPR_EXERCISE))
        self.assertEqual([t["code"] for t in trades], ["M", "S", "F"])
        sale = trades[1]
        self.assertEqual((sale["shares"], sale["after"]), (5810.0, 21136.0))
        self.assertAlmostEqual(sale["price"], (2191 * 115.40 + 3619 * 115.36) / 5810, places=6)
        self.assertEqual(sale["stake"], round(5810 / (21136 + 5810) * 100, 2))

    def test_unpriced_lines_make_a_partial_total(self):
        f = filing(LEN_BERKSHIRE)
        f["tx"][0]["price"] = None            # as if the 09-28 line had been filed unpriced
        a = next(t for t in ins.filing_trades(f) if t["security"] == "Class A Common Stock")
        self.assertTrue(a["partial"])
        self.assertAlmostEqual(a["value"], 12289 * 81.95 + 638813 * 81.59, places=2)

    def test_rows_carry_the_owner_and_role(self):
        rows = ins.issuer_rows(record(920760, [LEN_BERKSHIRE], ticker="LEN", name="Lennar Corporation"))
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual((row["t"], row["k"], row["o"], row["on"], row["ro"]),
                         ("LEN", "buy", "Berkshire Hathaway Inc", 1, "t"))
        self.assertEqual(row["_ow"], [315090, 1067983])
        self.assertEqual(row["pd"], "xslF345X06/ownership.xml")
        self.assertNotIn("_ow", ins.public(row))


class AmendmentTests(unittest.TestCase):
    def test_an_amendment_restating_the_line_replaces_the_original(self):
        filings = {NKE_ORIGINAL: filing(NKE_ORIGINAL), NKE_AMENDMENT: filing(NKE_AMENDMENT)}
        self.assertEqual(ins.superseded(filings), {NKE_ORIGINAL})
        rows = ins.issuer_rows({"cik": 320187, "ticker": "NKE", "name": "NIKE", "filings": filings})
        self.assertEqual([(r["a"], r["am"]) for r in rows], [(NKE_AMENDMENT, True)])   # the gift once, not twice

    def test_an_amendment_of_the_derivative_table_leaves_the_lines_alone(self):
        filings = {TPR_ORIGINAL: filing(TPR_ORIGINAL), TPR_AMENDMENT: filing(TPR_AMENDMENT)}
        self.assertEqual(filings[TPR_AMENDMENT]["tx"], [])
        self.assertEqual(ins.superseded(filings), set())
        rows = ins.issuer_rows({"cik": 1116132, "ticker": "TPR", "name": "Tapestry", "filings": filings})
        self.assertEqual(sorted(r["x"] for r in rows), ["A", "F"])

    def test_an_amendment_for_someone_else_replaces_nothing(self):
        orig, amend = filing(NKE_ORIGINAL), filing(NKE_AMENDMENT)
        amend["owners"] = [dict(amend["owners"][0], cik=999)]
        self.assertEqual(ins.superseded({NKE_ORIGINAL: orig, NKE_AMENDMENT: amend}), set())


class ClusterTests(unittest.TestCase):
    def test_five_insiders_bought_gamestop(self):
        rows = ins.issuer_rows(record(1326380, GME_ALL, ticker="GME", name="GameStop Corp."))
        (cluster,) = ins.cluster_buys(rows)
        self.assertEqual((cluster["t"], cluster["insiders"], cluster["buys"]), ("GME", 5, 9))
        self.assertEqual(cluster["who"][0]["o"], "Cohen Ryan")           # the biggest buyer first
        self.assertEqual((cluster["first"], cluster["last"]), ("2026-09-08", "2026-10-02"))
        self.assertAlmostEqual(cluster["v"], sum(r["v"] for r in rows), places=2)

    def test_joint_filers_are_one_buyer(self):
        rows = ins.issuer_rows(record(920760, [LEN_BERKSHIRE], ticker="LEN", name="Lennar"))
        self.assertEqual(ins.cluster_buys(rows), [])        # Berkshire and Buffett are one buyer
        self.assertEqual(len(ins.cluster_buys(rows, min_insiders=1)[0]["who"]), 1)

    def test_one_buyer_is_not_a_cluster(self):
        rows = ins.issuer_rows(record(1089063, [DKS_BUY], ticker="DKS", name="Dick's"))
        self.assertEqual(ins.cluster_buys(rows), [])

    def test_purchases_in_an_offering_do_not_count(self):
        rows = ins.issuer_rows(record(1782223, [PYXS_OFFERING], ticker="PYXS", name="Pyxis"))
        twin = [dict(r, _ow=[42], o="Somebody Else") for r in rows]    # a second "buyer" at the offer
        self.assertEqual(ins.cluster_buys(rows + twin), [])

    def test_sells_never_cluster(self):
        rows = ins.issuer_rows(record(1045810, [NVDA_TETER, NVDA_STEVENS], ticker="NVDA", name="NVIDIA"))
        self.assertEqual(ins.cluster_buys(rows), [])


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        for cik, accs, t in ((1326380, GME_ALL, "GME"), (1045810, [NVDA_TETER, NVDA_STEVENS], "NVDA"),
                             (320193, [AAPL_PLAN_SALE, AAPL_NO_LINES], "AAPL"),
                             (920760, [LEN_BERKSHIRE], "LEN"), (797468, [OXY_TAX], "OXY")):
            self.rows += ins.issuer_rows(record(cik, accs, ticker=t, name=t))

    def test_the_window_is_by_filing_date(self):
        since = ins.window_start(dt.date(2026, 10, 4), 7).isoformat()
        self.assertEqual(since, "2026-09-28")
        chosen = ins.select(self.rows, since=since, kind_="all", sort="newest")
        self.assertTrue(all(r["f"] >= since for r in chosen))
        self.assertNotIn(NVDA_STEVENS, {r["a"] for r in chosen})       # filed 09-22

    def test_kinds(self):
        since = "2026-09-01"
        buys = ins.select(self.rows, since=since, kind_="buy", sort="value")
        sells = ins.select(self.rows, since=since, kind_="sell", sort="value")
        both = ins.select(self.rows, since=since, kind_="all", sort="value")
        self.assertTrue(buys and all(r["k"] == "buy" for r in buys))
        self.assertTrue(sells and all(r["k"] == "sell" for r in sells))
        self.assertEqual(len(both), len(buys) + len(sells))
        self.assertNotIn("other", {r["k"] for r in both})              # OXY's tax withholding is kept, not fed

    def test_largest_first_and_newest_first(self):
        sells = ins.select(self.rows, since="2026-09-01", kind_="sell", sort="value")
        self.assertEqual(sells[0]["a"], NVDA_STEVENS)                  # ~$300M
        newest = ins.select(self.rows, since="2026-09-01", kind_="all", sort="newest")
        filed = [r["f"] for r in newest]
        self.assertEqual(filed, sorted(filed, reverse=True))
        self.assertEqual(newest[0]["a"], GME_COHEN_1002)               # 10-02, the latest acceptance

    def test_an_unpriced_trade_sorts_last_not_first(self):
        rows = self.rows + ins.issuer_rows(record(1782223, [PYXS_OFFERING], ticker="PYXS", name="Pyxis"))
        buys = ins.select(rows, since="2026-09-01", kind_="buy", sort="value")
        self.assertEqual(buys[-1]["t"], "PYXS")
        self.assertIsNone(buys[-1]["v"])

    def test_the_summary(self):
        window = [r for r in self.rows if r["f"] >= "2026-09-01"]
        s = ins.summarise(window)
        self.assertEqual((s["buy"]["insiders"], s["buy"]["companies"]), (6, 2))   # GME's five, Berkshire
        self.assertEqual(s["sell"]["companies"], 2)
        self.assertGreater(s["sell"]["value"], 3e8)
        self.assertIsNone(ins.summarise([r for r in window if r["t"] == "OXY"])["buy"]["value"])


class ListingTests(unittest.TestCase):
    def test_the_form_4s_in_a_real_submissions_listing(self):
        doc = json.loads((FIXTURES / "submissions_aapl_trimmed.json").read_text())
        listing = ins.form4_listing(doc)
        self.assertEqual(len(listing), doc["filings"]["recent"]["form"].count("4"))
        newest = listing[0]
        self.assertEqual((newest["acc"], newest["form"], newest["filed"], newest["doc"]),
                         (AAPL_PLAN_SALE, "4", "2026-10-01", "xslF345X06/form4.xml"))
        self.assertTrue(all(f["form"] in ("4", "4/A") for f in listing))   # no 144s, 8-Ks, 10-Qs

    def test_garbage_listings(self):
        self.assertEqual(ins.form4_listing(None), [])
        self.assertEqual(ins.form4_listing({"filings": {}}), [])
        self.assertEqual(ins.form4_listing({"filings": {"recent": {"form": ["4"], "accessionNumber": []}}}), [])

    def test_the_raw_document_sits_beside_the_rendered_one(self):
        self.assertEqual(ins.doc_name("xslF345X06/form4.xml"), "form4.xml")
        self.assertEqual(ins.doc_name("xslF345X06/wk-form4_1790196985.xml"), "wk-form4_1790196985.xml")
        self.assertEqual(ins.doc_name("form4.xml"), "form4.xml")
        self.assertIsNone(ins.doc_name("primary_doc.html"))
        # Under the issuer's CIK: the accession prefix is the filing agent's.
        self.assertEqual(ins.archive_url(320193, AAPL_PLAN_SALE, "form4.xml"),
                         "https://www.sec.gov/Archives/edgar/data/320193/000114036126038307/form4.xml")


class UniverseTests(unittest.TestCase):
    BLOB = {"timestamp": 0, "data": {
        "Tech": {"AAPL": {"Name": "Apple Inc.", "Quote Type": "EQUITY"},
                 "GOOGL": {"Name": "Alphabet Inc.", "Quote Type": "EQUITY"},
                 "GOOG": {"Name": "Alphabet Inc.", "Quote Type": "EQUITY"},
                 "NEWCO": {"Name": "Newco"}},                   # no Quote Type yet: still a company
        "US Broad ETFs": {"SPY": {"Name": "SPDR S&P 500", "Quote Type": "ETF"}},
        "Japan (Nikkei)": {"7203.T": {"Name": "Toyota", "Quote Type": "EQUITY"}},
        "Semis": {"AAPL": {"Name": "Apple again"}, "broken": "not a record"},
    }}
    MAP = {"AAPL": (320193, "Apple Inc."), "GOOGL": (1652044, "Alphabet Inc."),
           "GOOG": (1652044, "Alphabet Inc."), "NEWCO": (2000001, "NEWCO HOLDINGS /DE/"),
           "SPY": (884394, "SPDR S&P 500 ETF TRUST")}

    def test_the_followed_companies_one_entry_per_issuer(self):
        uni = ins.universe_from_ticker_cache(self.BLOB, self.MAP.get)
        by_cik = {e["cik"]: e for e in uni["issuers"]}
        self.assertEqual(sorted(by_cik), [320193, 1652044, 2000001])
        self.assertEqual(by_cik[1652044]["tickers"], ["GOOGL", "GOOG"])   # one CIK, two tickers
        self.assertEqual(by_cik[320193]["name"], "Apple Inc.")            # Yahoo's name, first seen
        self.assertEqual(uni["non_equity"], ["SPY"])                      # skipped without a lookup
        self.assertEqual(uni["no_cik"], ["7203.T"])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(ins.universe_from_ticker_cache({}, self.MAP.get)["issuers"], [])
        self.assertEqual(ins.universe_from_ticker_cache(None, self.MAP.get)["issuers"], [])


class NameTests(unittest.TestCase):
    def test_capitals_become_readable_and_mixed_case_is_left_alone(self):
        self.assertEqual(ins.display_name("BUFFETT WARREN E"), "Buffett Warren E")
        self.assertEqual(ins.display_name("BERKSHIRE HATHAWAY INC"), "Berkshire Hathaway Inc")
        self.assertEqual(ins.display_name("GordonMD Long Biased GP LLC"), "GordonMD Long Biased GP LLC")
        self.assertEqual(ins.display_name("SMITH JOHN III"), "Smith John III")
        self.assertEqual(ins.company_name("DANAHER CORP /DE/"), "Danaher Corp")
        self.assertEqual(ins.company_name("LENNAR CORP /NEW/"), "Lennar Corp")

    def test_yahoo_s_cut_short_names_lose_their_share_class_tail(self):
        self.assertEqual(ins.yahoo_name("Booking Holdings Inc. Common St"), "Booking Holdings Inc.")
        for kept in ("Apple Inc.", "Coca-Cola Company (The)", "NVIDIA Corporation", "Common Holdings"):
            self.assertEqual(ins.yahoo_name(kept), kept)
        self.assertEqual(ins.yahoo_name(None), "")


class FakeEdgar:
    """Serves the fixtures by URL, the way edgar_get would, and counts."""

    def __init__(self, cik: int, accs: list[str]):
        self.cik = cik
        self.accs = accs
        self.calls: list[str] = []
        self.fail: dict[str, BaseException] = {}

    def submissions(self) -> bytes:
        rows = sorted((INDEX[a] | {"acc": a} for a in self.accs), key=lambda r: r["filed"], reverse=True)
        recent = {"form": [], "accessionNumber": [], "filingDate": [], "reportDate": [],
                  "primaryDocument": [], "acceptanceDateTime": []}
        for r in rows:
            recent["form"].append(r["form"])
            recent["accessionNumber"].append(r["acc"])
            recent["filingDate"].append(r["filed"])
            recent["reportDate"].append(r["period"])
            recent["primaryDocument"].append(r["doc"])
            recent["acceptanceDateTime"].append(r["accepted"])
        return json.dumps({"cik": str(self.cik), "name": "GameStop Corp.", "tickers": ["GME", "GME-WT"],
                           "filings": {"recent": recent}}).encode()

    def __call__(self, url: str):
        self.calls.append(url)
        for needle, exc in self.fail.items():
            if needle in url:
                raise exc
        if "/submissions/" in url:
            body = self.submissions()
        else:
            folder = url.split("/")[-2]
            acc = f"{folder[:10]}-{folder[10:12]}-{folder[12:]}"
            body = raw(acc)
        return mock.Mock(content=body, json=lambda: json.loads(body))

    def docs(self) -> int:
        return sum(1 for u in self.calls if "/Archives/" in u)


class CacheTests(unittest.TestCase):
    CIK = 1326380

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [mock.patch.object(ins, "CACHE_DIR", self.dir)]
        for p in self.patches:
            p.start()
        ins._digests.clear()
        ins._meta_memo.clear()
        ins._universe_memo.update(mtime=None, value=None, ciks=set(), by_ticker={})
        self.edgar = FakeEdgar(self.CIK, GME_ALL)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        ins._digests.clear()
        ins._meta_memo.clear()
        ins._universe_memo.update(mtime=None, value=None, ciks=set(), by_ticker={})
        self.tmp.cleanup()

    def refresh(self, now: float, **kw):
        return ins.refresh_issuer(self.CIK, ticker="GME", get=self.edgar, pace=lambda: None, now=now, **kw)

    def test_first_fill_is_30_days_then_the_window_is_90(self):
        day = et(2026, 10, 20)
        stats = self.refresh(day)
        # Filed on or after 09-21: five of GME's nine.
        self.assertEqual(stats, {"submissions": 1, "docs": 5, "new": 5, "unread": 0})
        rec = ins._read(self.CIK)
        self.assertEqual(rec["covered_since"], "2026-09-21")
        self.assertEqual(rec["ticker"], "GME")
        self.assertEqual(rec["tickers"], ["GME", "GME-WT"])
        stats = self.refresh(day + 7 * 3600)
        self.assertEqual((stats["docs"], stats["new"]), (4, 4))      # the rest of the 90 days
        self.assertEqual(ins._read(self.CIK)["covered_since"], "2026-07-23")

    def test_a_parsed_accession_is_never_fetched_again(self):
        day = et(2026, 10, 4)
        self.refresh(day)
        first = self.edgar.docs()
        self.assertEqual(first, 9)
        self.refresh(day + 7 * 3600)
        self.refresh(day + 14 * 3600)
        self.assertEqual(self.edgar.docs(), first)                   # two submissions requests, no documents
        self.assertEqual(sum(1 for u in self.edgar.calls if "/submissions/" in u), 3)

    def test_a_failing_document_is_retried_then_given_up(self):
        self.edgar.fail["000092189526002719"] = requests.HTTPError("500 Server Error")
        day = et(2026, 10, 4)
        for i in range(ins.MAX_DOC_TRIES + 1):
            stats = self.refresh(day + i * 7 * 3600)
            self.assertEqual(stats["unread"], 1 if i < ins.MAX_DOC_TRIES else 0, i)
            if i == 0:
                self.assertEqual(stats["docs"], 9)    # the failed request counts as one made
        rec = ins._read(self.CIK)
        self.assertEqual(rec["unread"][GME_COHEN_1002]["n"], ins.MAX_DOC_TRIES)
        self.assertNotIn(GME_COHEN_1002, rec["filings"])
        self.assertEqual(sum(1 for u in self.edgar.calls if "000092189526002719" in u), ins.MAX_DOC_TRIES)
        self.assertEqual(ins.digest(self.CIK)["unread"], 1)           # reported, not vanished

    def test_a_cool_down_keeps_what_was_parsed_and_resumes(self):
        self.edgar.fail["000092189526002608"] = fetchguard.CooldownActive("sec-edgar", 60, "HTTP 429")
        day = et(2026, 10, 4)
        with self.assertRaises(fetchguard.CooldownActive):
            self.refresh(day)
        rec = ins._read(self.CIK)
        self.assertIsNone(rec.get("checked"))                         # not checked: due again at once
        # Newest first: 10-02, 10-01, 09-29, then the 09-21 filing accepted
        # later that evening; the one refused is the other 09-21 filing.
        self.assertEqual(len(rec["filings"]), 4)
        self.assertTrue(ins.is_due(rec, day + 60))
        del self.edgar.fail["000092189526002608"]
        stats = self.refresh(day + 120)
        self.assertEqual(stats["new"], 5)
        self.assertEqual(len(ins._read(self.CIK)["filings"]), 9)

    def test_a_failed_check_waits_like_a_good_one(self):
        self.edgar.fail["/submissions/"] = requests.HTTPError("404 Not Found")
        day = et(2026, 10, 4)
        with self.assertRaises(requests.HTTPError):
            self.refresh(day)
        rec = ins._read(self.CIK)
        self.assertEqual(rec["failed"], day)
        self.assertFalse(ins.is_due(rec, day + 3600))
        self.assertTrue(ins.is_due(rec, day + ins.RECHECK_SECONDS))

    def test_a_submissions_answer_that_is_not_an_object_is_a_failure(self):
        listing = lambda url: mock.Mock(content=b"[]", json=lambda: [])   # noqa: E731
        with self.assertRaises(ValueError):
            ins.refresh_issuer(self.CIK, get=listing, pace=lambda: None, now=et(2026, 10, 4))
        self.assertIn("not an object", ins._read(self.CIK)["why"])

    def test_due_every_six_hours(self):
        self.assertTrue(ins.is_due(None, 0))
        self.assertFalse(ins.is_due({"checked": 1000.0}, 1000.0 + ins.RECHECK_SECONDS - 1))
        self.assertTrue(ins.is_due({"checked": 1000.0}, 1000.0 + ins.RECHECK_SECONDS))

    def test_the_per_check_cap_leaves_the_rest_and_says_so(self):
        with mock.patch.object(ins, "MAX_DOCS_PER_CHECK", 3):
            stats = self.refresh(et(2026, 10, 4))
        self.assertEqual(stats["docs"], 3)
        # Fetched newest first (10-02, 10-01, 09-29); 09-21 was left, so the
        # record is whole only from the day after it.
        self.assertEqual(ins._read(self.CIK)["covered_since"], "2026-09-22")

    def test_two_writers_keep_both_filings(self):
        today = dt.date(2026, 10, 4)
        mine = record(self.CIK, [GME_TURNER], ticker="GME", name="GameStop", checked=1.0)
        ins._save(self.CIK, mine, today)
        theirs = record(self.CIK, [GME_COHEN_1002], ticker="GME", name="GameStop", checked=2.0)
        ins._save(self.CIK, theirs, today)
        self.assertEqual(set(ins._read(self.CIK)["filings"]), {GME_TURNER, GME_COHEN_1002})
        self.assertEqual(ins._read(self.CIK)["checked"], 2.0)

    def test_old_filings_are_pruned_and_stay_pruned(self):
        today = dt.date(2026, 10, 4)
        ins._save(self.CIK, record(self.CIK, [NKE_ORIGINAL, GME_TURNER], ticker="GME", name="x"), today)
        self.assertEqual(set(ins._read(self.CIK)["filings"]), {GME_TURNER})   # 05-15 is past 120 days
        ins._save(self.CIK, record(self.CIK, [GME_COHEN_1002], ticker="GME", name="x"), today)
        self.assertNotIn(NKE_ORIGINAL, ins._read(self.CIK)["filings"])

    def write_universe(self, issuers):
        (self.dir / ins.UNIVERSE_FILE).write_text(json.dumps({"ts": time.time(), "issuers": issuers}))

    def test_the_feed_waits_for_the_first_pass(self):
        now = et(2026, 10, 4)
        self.assertEqual(ins.feed_view(days=30, kind_="buy", sort="value", now=now)["status"], "warming")
        self.write_universe([{"cik": self.CIK, "tickers": ["GME"], "name": "GameStop Corp."},
                             {"cik": 320193, "tickers": ["AAPL"], "name": "Apple Inc."}])
        self.refresh(now)
        view = ins.feed_view(days=30, kind_="buy", sort="value", now=now)
        self.assertEqual(view["status"], "warming")                   # Apple not reached yet
        self.assertEqual((view["coverage"]["checked"], view["coverage"]["pending"]), (1, 1))
        self.assertEqual(len(view["rows"]), 9)                        # what is in is served
        (cluster,) = view["clusters"]
        self.assertEqual(cluster["insiders"], 5)
        self.assertEqual(view["coverage"]["complete_from"], "2026-09-05")

    def test_a_company_a_reader_looked_up_joins_the_feed_for_a_day(self):
        now = et(2026, 10, 4)
        self.write_universe([{"cik": 320193, "tickers": ["AAPL"], "name": "Apple Inc."}])
        (self.dir / "320193.json").write_text(json.dumps(
            record(320193, [AAPL_PLAN_SALE], ticker="AAPL", name="Apple Inc.", checked=now - 60)))
        self.refresh(now - 3600)                                      # GME, outside the universe
        view = ins.feed_view(days=30, kind_="all", sort="newest", now=now)
        self.assertEqual(view["status"], "ok")
        self.assertEqual(view["coverage"]["extra"], 1)
        self.assertIn("GME", view["present"])
        later = ins.feed_view(days=30, kind_="all", sort="newest", now=now + ins.LOOKUP_FRESH_SECONDS)
        self.assertEqual(later["coverage"]["extra"], 0)
        self.assertNotIn("GME", later["present"])

    def test_the_company_view_has_every_kind_newest_first(self):
        now = et(2026, 10, 4)
        (self.dir / "1116132.json").write_text(json.dumps(
            record(1116132, [TPR_EXERCISE, TPR_ORIGINAL, TPR_AMENDMENT], ticker="TPR", name="Tapestry",
                   checked=now - 60)))
        view = ins.company_view(1116132, days=90, now=now)
        # 09-10's exercise filing first; then the 08-19 original, whose tax
        # withholding (08-18) is a day newer than the award it paid for.
        self.assertEqual([r["x"] for r in view["rows"]], ["M", "S", "F", "F", "A"])
        self.assertEqual(view["others"], 4)
        self.assertFalse(view["stale"])
        self.assertIsNone(ins.company_view(999, days=90, now=now))


class LookupTests(unittest.TestCase):
    """The on-demand look-up: one queue and one worker per process, a claim
    file other processes can see, a pause after a failure, a daily cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.spent = []
        self.patches = [
            mock.patch.object(ins, "CACHE_DIR", self.dir),
            mock.patch("ystocker.quota.try_consume_insider_lookup", side_effect=lambda: self.spent.append(1) or True),
        ]
        for p in self.patches:
            p.start()
        self.reset()

    def tearDown(self):
        self.wait()
        for p in reversed(self.patches):
            p.stop()
        self.reset()
        self.tmp.cleanup()

    def reset(self):
        ins._pending.clear()
        ins._worker = None
        ins._current = None
        ins._failed.clear()
        ins._no_cik.clear()

    def wait(self):
        for _ in range(300):
            if ins._worker is None:
                return
            time.sleep(0.01)

    def test_the_worker_drains_the_queue_and_stands_down(self):
        done = []
        with mock.patch.object(ins, "_lookup", side_effect=lambda t: done.append(t)):
            self.assertEqual(ins.kick("AAPL"), "queued")
            self.assertIn(ins.kick("AAPL"), ("already", "queued"))   # "queued" only if it already finished
            self.assertEqual(ins.kick("MSFT"), "queued")
            self.wait()
        self.assertEqual(sorted(set(done)), ["AAPL", "MSFT"])
        self.assertIsNone(ins._worker)
        self.assertEqual(list(self.dir.glob(".lookup-*")), [])      # claims released

    def test_a_full_queue_and_a_spent_allowance_refuse(self):
        ins._pending.extend(["X%d" % i for i in range(ins.MAX_PENDING)])
        ins._worker = object()                                      # busy: nothing starts
        self.assertEqual(ins.kick("AAPL"), "full")
        self.reset()
        with mock.patch("ystocker.quota.try_consume_insider_lookup", return_value=False):
            self.assertEqual(ins.kick("AAPL"), "capped")
        self.assertEqual(list(ins._pending), [])

    def test_another_process_s_claim_is_respected_until_it_goes_stale(self):
        claim = ins._claim_path("AAPL")
        claim.write_text("")
        self.assertTrue(ins.claimed("AAPL"))
        self.assertEqual(ins.kick("AAPL"), "already")
        self.assertEqual(self.spent, [])                            # nothing spent on it
        old = time.time() - ins.CLAIM_TTL_SECONDS - 5
        os.utime(claim, (old, old))
        self.assertFalse(ins.claimed("AAPL"))
        self.assertTrue(ins._claim("AAPL"))                         # a dead run's claim is taken over
        ins._release("AAPL")

    def test_a_failure_pauses_and_no_cik_is_remembered(self):
        with mock.patch.object(ins, "_lookup", side_effect=RuntimeError("boom")), \
                self.assertLogs("ystocker.insiders", level="WARNING") as logs:
            ins.kick("AAPL")
            self.wait()
        self.assertIn("look-up of AAPL failed", logs.output[0])
        self.assertTrue(ins.recently_failed("AAPL"))
        self.assertFalse(ins.recently_failed("AAPL", now=time.time() + ins.FAILURE_PAUSE_SECONDS + 1))
        with mock.patch("ystocker.fundamentals.cik_for", return_value=None):
            ins.kick("7203.T")
            self.wait()
        self.assertTrue(ins.no_cik("7203.T"))
        self.assertFalse(ins.recently_failed("7203.T"))


if __name__ == "__main__":
    unittest.main()
