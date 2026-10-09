"""Tests for ystocker.smart_money -- /smart-money and /history's 聪明钱 tab.

No app, no network: small hand-built inputs in the three shapes the route
hands over (sec13f's holdings, insiders.feed_view's rows, congress' feed rows).
What is pinned:

* the named managers are funds /13f actually tracks, and a fund whose fetch
  failed is listed with its error rather than dropped;
* a 13F reads opened/added as buying and trimmed as selling, among the 50
  largest positions only, and never claims an exit;
* a 10b5-1 plan trade, or a purchase in an offering, is listed and does not
  set the insiders' side;
* a House sale of options, a bond or a fund sets no side; only stock and
  options trades with a ticker count;
* "same way" needs two sources on one side and none on the other, and says
  how many days apart their latest dates are;
* the window: Form 4s and PTRs filed before it are left out;
* each stock's ``who`` entries carry the id their person has in ``people``,
  list up to MAX_WHO people, and read "mixed" for someone who traded both
  ways -- the page's network draws its links from them;
* one stock's dossier lists the named managers first, then the other funds.
"""
from __future__ import annotations

import unittest
from datetime import date

from ystocker import sec13f, smart_money as sm

TODAY = date(2026, 10, 7)
SINCE = "2026-07-09"   # TODAY - 90 days


def _fund(holdings, period="2026-06-30", filed="2026-08-14", **extra):
    return {"error": None, "quarters": [{"period": period, "filing_date": filed, "holdings": holdings,
                                         "total_holdings": len(holdings), "total_value_millions": 1000.0}],
            **extra}


def _h(t, ch, w, pct=None, name=None):
    return {"ticker": t, "name": name or t, "change": ch, "change_pct": pct, "pct_portfolio": w, "rank": 1}


HOLDINGS = {
    "Berkshire Hathaway": _fund([_h("AAPL", "unchanged", 22.0), _h("GOOGL", "increased", 12.6, 45.2),
                                 _h("BAC", "reduced", 9.2, -5.9), _h("NVDA", "new", 1.0)]),
    "Pershing Square": _fund([_h("NVDA", "increased", 10.0, 20.0), _h("HLT", "reduced", 8.0, -10.0)],
                             period="2026-03-31", filed="2026-05-15"),
    "Baupost Group": {"error": "Could not fetch any holdings", "cik": "0001061768"},
    "Vanguard Group": _fund([_h("NVDA", "reduced", 7.0, -1.0)]),   # tracked, not a named manager
}

INSIDERS = [
    # NVDA: a director buys; a plan sale does not set the side.
    {"t": "NVDA", "n": "NVIDIA", "o": "Doe Jane", "ti": "Director", "k": "buy", "d": "2026-09-20", "f": "2026-09-22", "v": 500000.0},
    {"t": "NVDA", "n": "NVIDIA", "o": "Huang Jensen", "ti": "CEO", "k": "sell", "d": "2026-09-21", "f": "2026-09-23", "v": 9e6, "pl": True},
    # BAC: an officer sells, off plan.
    {"t": "BAC", "n": "Bank of America", "o": "Roe Rick", "ti": "CFO", "k": "sell", "d": "2026-09-01", "f": "2026-09-03", "v": 2e6},
    # HLT: a purchase in an offering sets no side.
    {"t": "HLT", "n": "Hilton", "o": "Poe Pat", "ti": "Director", "k": "buy", "d": "2026-09-05", "f": "2026-09-08", "v": 1e5, "of": True},
    # Filed before the window.
    {"t": "AAPL", "n": "Apple", "o": "Old Ollie", "k": "sell", "d": "2026-06-01", "f": "2026-06-03", "v": 1e6},
    # An award is not a trade.
    {"t": "AAPL", "n": "Apple", "o": "Cook Tim", "k": "other", "d": "2026-09-01", "f": "2026-09-03"},
]

HOUSE = [
    {"t": "NVDA", "n": "NVIDIA Corporation", "m": "Nancy Pelosi", "md": "CA11", "o": "SP", "k": "buy", "dir": "buy",
     "c": "OP", "op": "call", "d": "2026-07-24", "f": "2026-08-21", "lo": 1000001, "hi": 5000000, "doc": "20035143", "yr": 2026},
    {"t": "BAC", "n": "Bank of America", "m": "Kevin Hern", "md": "OK01", "o": "JT", "k": "sell", "dir": "sell",
     "c": "ST", "d": "2026-09-04", "f": "2026-09-15", "lo": 15001, "hi": 50000, "doc": "20035491", "yr": 2026},
    {"t": "MSFT", "n": "Microsoft", "m": "Josh Gottheimer", "md": "NJ05", "o": "JT", "k": "sell", "dir": None,
     "c": "OP", "op": "call", "d": "2026-08-14", "f": "2026-09-14", "lo": 250001, "hi": 500000, "doc": "20035455", "yr": 2026},
    {"t": None, "n": "REOF XXX, LLC", "m": "Nancy Pelosi", "md": "CA11", "o": "SP", "k": "buy", "dir": None,
     "c": "AB", "d": "2026-09-08", "f": "2026-10-02", "lo": 500001, "hi": 1000000, "doc": "20035553", "yr": 2026},
    {"t": "AAPL", "n": "Apple", "m": "Old Member", "md": "XX01", "o": "", "k": "buy", "dir": "buy",
     "c": "ST", "d": "2026-05-01", "f": "2026-06-01", "lo": 1001, "hi": 15000, "doc": "1", "yr": 2026},
]


class PeopleTests(unittest.TestCase):

    def test_every_named_manager_is_a_tracked_fund(self):
        self.assertTrue(set(sm.PEOPLE) <= set(sec13f.FUNDS), set(sm.PEOPLE) - set(sec13f.FUNDS))
        for fund, (person, zh, role) in sm.PEOPLE.items():
            self.assertTrue(person and zh and person != zh, fund)
            self.assertIn(role, ("runs", "founded"), fund)

    def test_none_of_the_index_giants_quants_or_pods_is_a_person(self):
        for fund in ("Vanguard Group", "BlackRock", "State Street", "Renaissance Technologies", "Two Sigma Investments",
                     "Jane Street", "Susquehanna", "Citadel Advisors", "Millennium Management", "Point72 Asset Management"):
            self.assertNotIn(fund, sm.PEOPLE)

    def test_a_managers_quarter(self):
        funds = {p["org"]: p for p in sm.fund_people(HOLDINGS)}
        b = funds["Berkshire Hathaway"]
        self.assertEqual((b["name"], b["name_zh"], b["as_of"], b["filed"], b["lag"]),
                         ("Warren Buffett", "沃伦·巴菲特", "2026-06-30", "2026-08-14", 45))
        self.assertEqual((b["buys"], b["sells"], b["holds"]), (2, 1, 1))
        self.assertEqual(b["opened"], ["NVDA"])
        self.assertEqual(b["top"], ["AAPL", "GOOGL", "BAC"])
        # Moves first, opened before added, then what was held.
        self.assertEqual([a["t"] for a in b["actions"]], ["NVDA", "GOOGL", "BAC", "AAPL"])

    def test_a_failed_fund_is_listed_with_its_error(self):
        funds = {p["org"]: p for p in sm.fund_people(HOLDINGS)}
        self.assertEqual(funds["Baupost Group"]["error"], "Could not fetch any holdings")
        self.assertEqual(funds["Baupost Group"]["actions"], [])
        # A named fund missing from the cache altogether is a gap, not absent.
        self.assertEqual(funds["Icahn Capital"]["error"], "no 13F data")

    def test_insiders(self):
        people = {p["name"]: p for p in sm.insider_people(INSIDERS, SINCE)}
        self.assertNotIn("Old Ollie", people)        # filed before the window
        self.assertNotIn("Cook Tim", people)         # an award
        self.assertEqual(people["Huang Jensen"]["sells"], 1)
        self.assertTrue(people["Huang Jensen"]["actions"][0]["plan"])
        self.assertEqual(list(people)[0], "Huang Jensen")   # largest first

    def test_house_members(self):
        people = {p["name"]: p for p in sm.house_people(HOUSE, SINCE, {"Nancy Pelosi": "南希·佩洛西"})}
        pelosi = people["Nancy Pelosi"]
        self.assertEqual((pelosi["name_zh"], pelosi["trades"], pelosi["buys"], pelosi["sells"], pelosi["n_tickers"]),
                         ("南希·佩洛西", 2, 1, 0, 1))
        self.assertEqual(pelosi["mid"], round((1000001 + 5000000) / 2 + (500001 + 1000000) / 2))
        self.assertEqual(people["Josh Gottheimer"]["buys"] + people["Josh Gottheimer"]["sells"], 0)
        self.assertNotIn("Old Member", people)


class TickerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.funds = sm.fund_people(HOLDINGS)
        cls.rows = {r["t"]: r for r in sm.tickers_view(cls.funds, INSIDERS, HOUSE, SINCE)}

    def test_three_sources_buying_agree(self):
        nvda = self.rows["NVDA"]
        self.assertEqual({k: v["side"] for k, v in nvda["src"].items()}, {"funds": "buy", "insiders": "buy", "house": "buy"})
        self.assertEqual(nvda["agree"]["side"], "buy")
        self.assertEqual(sorted(nvda["agree"]["sources"]), ["funds", "house", "insiders"])
        # The 13F's quarter end (06-30) to the director's buy (09-20), not to
        # the plan sale the next day, which sets no side.
        self.assertEqual(nvda["agree"]["span_days"], 82)
        self.assertEqual(nvda["src"]["insiders"]["seen"], "2026-09-21")
        self.assertEqual(nvda["latest"], "2026-09-21")
        self.assertFalse(nvda["split"])

    def test_a_plan_sale_is_listed_but_sets_no_side(self):
        ins = self.rows["NVDA"]["src"]["insiders"]
        self.assertEqual((ins["buy"], ins["sell"], ins["plan"]), (1, 0, 1))
        self.assertIn("Huang Jensen", [w["name"] for w in ins["who"]])

    def test_two_sources_selling_agree(self):
        bac = self.rows["BAC"]
        self.assertEqual(bac["agree"]["side"], "sell")
        self.assertEqual(sorted(bac["agree"]["sources"]), ["funds", "house", "insiders"])

    def test_an_offering_purchase_sets_no_side(self):
        hlt = self.rows["HLT"]
        self.assertIsNone(hlt["src"]["insiders"]["side"])
        self.assertIsNone(hlt["agree"])      # the 13F trim alone is one source

    def test_a_held_position_is_not_a_side(self):
        aapl = self.rows["AAPL"]
        self.assertEqual(aapl["src"]["funds"]["hold"], 1)
        self.assertIsNone(aapl["src"]["funds"]["side"])
        self.assertNotIn("house", aapl["src"])       # filed before the window
        self.assertNotIn("insiders", aapl["src"])

    def test_an_options_sale_and_a_bond_set_nothing(self):
        self.assertNotIn("MSFT", self.rows)
        self.assertNotIn(None, self.rows)

    def test_opposite_sides_are_a_split_not_an_agreement(self):
        holdings = {"Berkshire Hathaway": _fund([_h("XYZ", "new", 1.0)])}
        house = [dict(HOUSE[1], t="XYZ")]   # a House sale
        insiders = [dict(INSIDERS[2], t="XYZ")]
        row = sm.tickers_view(sm.fund_people(holdings), insiders, house, SINCE)[0]
        self.assertIsNone(row["agree"])
        self.assertTrue(row["split"])

    def test_agreements_sort_first(self):
        order = [r["t"] for r in sm.tickers_view(self.funds, INSIDERS, HOUSE, SINCE)]
        self.assertEqual(set(order[:2]), {"NVDA", "BAC"})


class WhoTests(unittest.TestCase):
    """Each stock's ``who`` lists are what the page's network draws its links
    from, joined to ``people`` by id -- so an entry without the id, or a list
    cut short, is a link missing from the picture with nothing to say so."""

    def test_every_entry_carries_the_id_its_person_has(self):
        out = sm.build(HOLDINGS, {"rows": INSIDERS, "coverage": {}}, {"rows": HOUSE, "counts": {}},
                       today=TODAY, names_zh={"Nancy Pelosi": "南希·佩洛西"})
        ids = {p["id"] for p in out["people"]}
        seen = 0
        for row in out["tickers"]:
            for src, x in row["src"].items():
                for w in x["who"]:
                    self.assertIn(w["id"], ids, (row["t"], src, w["name"]))
                    seen += 1
        self.assertGreater(seen, 6)
        nvda = next(r for r in out["tickers"] if r["t"] == "NVDA")
        self.assertEqual([w["id"] for w in nvda["src"]["house"]["who"]], ["house-nancy-pelosi"])
        self.assertIn("ins-huang-jensen-nvda", [w["id"] for w in nvda["src"]["insiders"]["who"]])
        self.assertEqual({w["id"] for w in nvda["src"]["funds"]["who"]},
                         {"fund-berkshire-hathaway", "fund-pershing-square"})

    def test_a_dossier_without_peoples_ids_still_reads_the_sides(self):
        # dossier() hands tickers_view managers built without people's ids.
        d = sm.dossier("NVDA", HOLDINGS, INSIDERS, HOUSE, today=TODAY)
        self.assertEqual(d["sides"]["funds"], "buy")

    def test_more_than_eight_people_are_listed(self):
        house = [dict(HOUSE[0], m=f"Member {i:02d}", md=f"XX{i:02d}") for i in range(sm.MAX_WHO + 5)]
        row = next(r for r in sm.tickers_view([], [], house, SINCE) if r["t"] == "NVDA")
        who = row["src"]["house"]["who"]
        self.assertEqual(len(who), sm.MAX_WHO)
        self.assertEqual(row["src"]["house"]["buy"], sm.MAX_WHO + 5)   # counts are not cut
        self.assertEqual(row["actors"], sm.MAX_WHO)
        self.assertGreater(sm.MAX_WHO, 8)

    def test_a_member_who_bought_and_sold_is_one_mixed_entry(self):
        house = [HOUSE[0], dict(HOUSE[0], k="sell", dir="sell", d="2026-07-30", doc="20035999")]
        row = next(r for r in sm.tickers_view([], [], house, SINCE) if r["t"] == "NVDA")
        self.assertEqual([(w["name"], w["dir"]) for w in row["src"]["house"]["who"]], [("Nancy Pelosi", "mixed")])
        self.assertEqual((row["src"]["house"]["buy"], row["src"]["house"]["sell"]), (1, 1))

    def test_an_insiders_entry_says_what_set_no_side(self):
        rows = {r["t"]: r for r in sm.tickers_view([], INSIDERS, [], SINCE)}
        nvda = {w["name"]: w for w in rows["NVDA"]["src"]["insiders"]["who"]}
        self.assertEqual((nvda["Doe Jane"]["dir"], nvda["Doe Jane"]["plan"]), ("buy", False))
        self.assertTrue(nvda["Huang Jensen"]["plan"])
        self.assertTrue(rows["HLT"]["src"]["insiders"]["who"][0]["offering"])

    def test_the_index_behind_the_lists_is_not_in_the_payload(self):
        for row in sm.tickers_view(sm.fund_people(HOLDINGS), INSIDERS, HOUSE, SINCE):
            for x in row["src"].values():
                self.assertNotIn("_by", x)


class BuildAndDossierTests(unittest.TestCase):

    def test_build(self):
        out = sm.build(HOLDINGS, {"rows": INSIDERS, "coverage": {"checked": 200, "universe": 215, "newest_check": 1.0}},
                       {"rows": HOUSE, "counts": {"filings": 520, "paper": 50, "pending": 0}, "latest_filed": "2026-10-05"},
                       today=TODAY, names_zh={"Nancy Pelosi": "南希·佩洛西"})
        self.assertEqual((out["since"], out["window_days"]), (SINCE, 90))
        s = out["sources"]
        self.assertEqual(s["funds"]["named"], len(sm.PEOPLE))
        self.assertEqual(s["funds"]["with_data"], 2)
        self.assertEqual(s["funds"]["as_of"], "2026-06-30")
        self.assertEqual(s["insiders"]["trades"], 4)
        self.assertEqual((s["house"]["filings"], s["house"]["latest_filed"]), (520, "2026-10-05"))
        self.assertEqual({p["kind"] for p in out["people"]}, {"fund", "house", "insider"})
        nvda = next(r for r in out["tickers"] if r["t"] == "NVDA")
        self.assertEqual(nvda["src"]["house"]["who"][0]["name_zh"], "南希·佩洛西")
        fund_who = nvda["src"]["funds"]["who"]
        self.assertEqual({w["name"] for w in fund_who}, {"Warren Buffett", "Bill Ackman"})
        self.assertTrue(all(w["name_zh"] for w in fund_who))

    def test_build_with_nothing_yet(self):
        out = sm.build({}, None, None, today=TODAY)
        self.assertEqual(out["tickers"], [])
        self.assertTrue(all(p.get("error") for p in out["people"]))
        self.assertIsNone(out["sources"]["house"]["filings"])

    def test_dossier(self):
        d = sm.dossier("NVDA", HOLDINGS, INSIDERS, HOUSE, today=TODAY, names_zh={"Nancy Pelosi": "南希·佩洛西"})
        self.assertEqual([f["fund"] for f in d["funds"]], ["Pershing Square", "Berkshire Hathaway", "Vanguard Group"])
        self.assertEqual(d["funds"][0]["person"], "Bill Ackman")
        self.assertIsNone(d["funds"][2]["person"])
        self.assertEqual([r["o"] for r in d["insiders"]], ["Doe Jane", "Huang Jensen"])
        self.assertEqual(d["insiders"][0]["lag"], 2)
        self.assertEqual([r["m"] for r in d["house"]], ["Nancy Pelosi"])
        self.assertEqual(d["house"][0]["m_zh"], "南希·佩洛西")
        self.assertEqual(d["house"][0]["lag"], 28)
        self.assertEqual(d["agree"]["side"], "buy")
        self.assertEqual(d["sides"], {"funds": "buy", "insiders": "buy", "house": "buy"})

    def test_a_stock_nobody_touched(self):
        d = sm.dossier("ZZZZ", HOLDINGS, INSIDERS, HOUSE, today=TODAY)
        self.assertEqual((d["funds"], d["insiders"], d["house"], d["agree"], d["split"]), ([], [], [], None, False))


if __name__ == "__main__":
    unittest.main()
