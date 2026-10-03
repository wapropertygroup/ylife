"""ystocker/odds.py -- the arithmetic behind /predictions, pinned to real payloads.

Every fixture below is cut from a live response captured on 2026-10-03: Gamma's
``/events`` for Polymarket, ``/trade-api/v2/events`` for Kalshi, and
``/api/fedwatch`` for the futures. They are trimmed to the fields the module
reads, never invented, so a rule that passes here passes on what the venues
actually send.

The failures these exist to catch are the quiet ones:

* comparing /fedwatch's *cumulative* grid with the venues' *per-meeting*
  markets, which on 2026-10-03 would have shown a 9-point December disagreement
  between two different questions (14.6% "no change by December" against
  Polymarket's 23.5% "no change at December");
* listing a resolved ladder rung as a live 100% (Gamma keeps "Above 3%" in the
  open inflation event at a price of 1);
* reading an absent ``oneDayPriceChange`` as zero, which hides exactly the
  market that moved most (October's "No change": +49pp on the week, no daily
  field at all);
* scoring an AI read and the market over different outcomes, or calling an
  unsettled ledger a perfect score.

Pure: no network, no app, no clock.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from ystocker import odds

NOW = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)


def _pm_market(label, price, *, bid=None, ask=None, d1=None, w1=None, order=0,
               mid="1", closed=False, question=None, outcomes=("Yes", "No")):
    m = {
        "id": mid, "question": question or label, "groupItemTitle": label,
        "groupItemThreshold": str(order), "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps([str(price), str(round(1 - price, 4))]),
        "bestBid": bid, "bestAsk": ask, "lastTradePrice": price, "closed": closed,
        "description": f'This market will resolve to "Yes" if {label}.',
    }
    if d1 is not None:
        m["oneDayPriceChange"] = d1
    if w1 is not None:
        m["oneWeekPriceChange"] = w1
    return m


#: "Fed Decision in October?" as Gamma listed it.
PM_FED_OCT = {
    "slug": "fed-decision-in-october-20260617190323537",
    "title": "Fed Decision in October?",
    "endDate": "2026-10-29T03:59:00Z", "closed": False, "negRisk": True,
    "volume": 24391674.91, "volume24hr": 181358.06, "liquidity": 3159786.60,
    "tags": [{"slug": s} for s in ("fed", "fomc", "economy", "fed-rates", "Global-Rates",
                                   "economic-policy")],
    "markets": [
        _pm_market("50+ bps decrease", 0.0025, bid=0.002, ask=0.003, order=0, mid="2589810"),
        _pm_market("25 bps decrease", 0.0045, bid=0.004, ask=0.005, order=1, mid="2589811"),
        _pm_market("No change", 0.825, bid=0.82, ask=0.83, w1=0.49, order=2, mid="2589812"),
        _pm_market("25 bps increase", 0.175, bid=0.17, ask=0.18, w1=-0.47, order=3, mid="2589813"),
        _pm_market("50+ bps increase", 0.0045, bid=0.004, ask=0.005, w1=-0.004, order=4,
                   mid="2589814"),
    ],
}

PM_FED_DEC = {
    "slug": "fed-decision-in-december-20260729232808632",
    "title": "Fed Decision in December?",
    "endDate": "2026-12-10T04:59:00Z", "closed": False, "negRisk": True,
    "volume": 2588325.73, "volume24hr": 35723.03, "liquidity": 816769.48,
    "tags": [{"slug": "fed-rates"}, {"slug": "economy"}],
    "markets": [
        _pm_market("50+ bps decrease", 0.0035, bid=0.003, ask=0.004, w1=-0.001, order=0, mid="3215005"),
        _pm_market("25 bps decrease", 0.0175, bid=0.017, ask=0.018, d1=-0.001, w1=0.0015, order=1,
                   mid="3215006"),
        _pm_market("No change", 0.235, bid=0.23, ask=0.24, d1=0.01, w1=-0.04, order=2, mid="3215007"),
        _pm_market("25 bps increase", 0.735, bid=0.73, ask=0.74, d1=-0.01, w1=0.05, order=3,
                   mid="3215008"),
        _pm_market("50+ bps increase", 0.0175, bid=0.017, ask=0.018, d1=0.0005, w1=-0.0105, order=4,
                   mid="3215009"),
    ],
}

#: An open ladder holding two rungs that have already resolved.
PM_INFLATION = {
    "slug": "how-high-will-inflation-get-in-2026",
    "title": "How high will inflation get in 2026?",
    "endDate": "2027-02-01T04:59:00Z", "negRisk": False,
    "volume": 1480291.43, "volume24hr": 11017.52, "liquidity": 53516.83,
    "tags": [{"slug": "inflation"}, {"slug": "macro-indicators"}, {"slug": "economy"}],
    "markets": [
        _pm_market("Above 4.5%", 0.13, bid=0.12, ask=0.14, d1=-0.035, w1=-0.035, order=3, mid="2241741"),
        _pm_market("Above 3%", 1.0, bid=0.999, ask=1.0, order=0, mid="680949", closed=True),
        _pm_market("Above 4%", 1.0, bid=0.999, ask=1.0, order=2, mid="680950", closed=True),
        _pm_market("Above 5%", 0.08, bid=0.07, ask=0.09, w1=-0.005, order=4, mid="680951"),
    ],
}

PM_RECESSION = {
    "slug": "us-recession-by-end-of-2026", "title": "US recession by end of 2026?",
    "endDate": "2026-12-31T00:00:00Z", "volume": 2243342.20, "volume24hr": 2549.93,
    "liquidity": 88715.25, "openInterest": 621951.96,
    "tags": [{"slug": "business"}, {"slug": "economy"}, {"slug": "macro-graph"}],
    "markets": [_pm_market("", 0.075, bid=0.07, ask=0.08, w1=-0.015, mid="609655",
                           question="US recession by end of 2026?")],
}


def _ks_market(ticker, label, bid, ask, last, prev, *, stype="custom", floor=None, cap=None,
               vol="1000.00", v24="0.00", oi="500.00", close="2026-10-28T17:59:00Z",
               status="active"):
    m = {"ticker": ticker, "yes_sub_title": label, "strike_type": stype,
         "yes_bid_dollars": bid, "yes_ask_dollars": ask, "last_price_dollars": last,
         "previous_price_dollars": prev, "volume_fp": vol, "volume_24h_fp": v24,
         "open_interest_fp": oi, "status": status, "close_time": close,
         "rules_primary": f"If {label}, then the market resolves to Yes."}
    if floor is not None:
        m["floor_strike"] = floor
    if cap is not None:
        m["cap_strike"] = cap
    return m


KS_FED_OCT = {
    "event_ticker": "KXFEDDECISION-26OCT", "series_ticker": "KXFEDDECISION",
    "title": "Fed decision in Oct 2026?", "sub_title": "On Oct 28, 2026",
    "mutually_exclusive": True, "strike_date": "2026-10-28T18:00:00Z",
    "markets": [
        _ks_market("KXFEDDECISION-26OCT-C26", "Cut >25bps", "0.0000", "0.0100", "0.0100", "0.0100",
                   vol="225754.87", v24="747.62"),
        _ks_market("KXFEDDECISION-26OCT-C25", "Cut 25bps", "0.0000", "0.0100", "0.0100", "0.0100",
                   vol="880283.29", v24="6321.26"),
        _ks_market("KXFEDDECISION-26OCT-H0", "Fed maintains rate", "0.8200", "0.8300", "0.8200",
                   "0.8200", vol="3244960.08", v24="135184.86", oi="1186223.92"),
        _ks_market("KXFEDDECISION-26OCT-H25", "Hike 25bps", "0.1800", "0.1900", "0.1800", "0.1900",
                   vol="2412282.23", v24="19052.46"),
        _ks_market("KXFEDDECISION-26OCT-H26", "Hike >25bps", "0.0000", "0.0100", "0.0100", "0.0100",
                   vol="869393.74", v24="3543.15"),
    ],
}

KS_FED_DEC = {
    "event_ticker": "KXFEDDECISION-26DEC", "series_ticker": "KXFEDDECISION",
    "title": "Fed decision in Dec 2026?", "sub_title": "On Dec 9, 2026",
    "mutually_exclusive": True, "strike_date": "2026-12-09T19:00:00Z",
    "markets": [
        _ks_market("KXFEDDECISION-26DEC-C26", "Cut >25bps", "0.0000", "0.0100", "0.0100", "0.0100",
                   close="2026-12-09T18:59:00Z"),
        _ks_market("KXFEDDECISION-26DEC-C25", "Cut 25bps", "0.0100", "0.0200", "0.0100", "0.0200",
                   v24="9922.04", close="2026-12-09T18:59:00Z"),
        _ks_market("KXFEDDECISION-26DEC-H0", "Fed maintains rate", "0.2700", "0.2800", "0.2800",
                   "0.2400", v24="2872.87", close="2026-12-09T18:59:00Z"),
        _ks_market("KXFEDDECISION-26DEC-H25", "Hike 25bps", "0.7000", "0.7100", "0.7000", "0.7200",
                   v24="456.04", close="2026-12-09T18:59:00Z"),
        _ks_market("KXFEDDECISION-26DEC-H26", "Hike >25bps", "0.0200", "0.0300", "0.0300", "0.0300",
                   v24="199.80", close="2026-12-09T18:59:00Z"),
    ],
}

#: September CPI, all fourteen rungs.
_CPI = [(-0.4, "0.9900", "1.0000", "0.9900", "0.9900"), (-0.3, "0.9900", "1.0000", "0.9900", "0.9900"),
        (-0.2, "0.9900", "1.0000", "0.9900", "0.9900"), (-0.1, "0.9900", "1.0000", "0.9900", "0.9900"),
        (0.0, "0.9900", "1.0000", "0.9900", "0.9900"), (0.1, "0.9900", "1.0000", "0.9900", "0.9900"),
        (0.2, "0.9700", "0.9800", "0.9800", "0.9900"), (0.3, "0.9800", "0.9900", "0.9800", "0.9700"),
        (0.4, "0.9000", "0.9100", "0.9100", "0.9200"), (0.5, "0.5900", "0.6000", "0.6000", "0.5900"),
        (0.6, "0.1700", "0.1800", "0.1700", "0.1900"), (0.7, "0.0300", "0.0400", "0.0400", "0.0300"),
        (0.8, "0.0100", "0.0300", "0.0300", "0.0200"), (0.9, "0.0000", "0.0100", "0.0100", "0.0100")]
KS_CPI = {
    "event_ticker": "KXCPI-26SEP", "series_ticker": "KXCPI", "title": "CPI in September",
    "sub_title": "In Sep 2026", "mutually_exclusive": False,
    # Listed out of order on purpose: the rungs must come back sorted by strike.
    "markets": [_ks_market(f"KXCPI-26SEP-T{s}", f"Above {s:.1f}%", b, a, last, prev,
                           stype="greater", floor=s, vol="20000.00", v24="500.00",
                           close="2026-10-14T12:25:00Z")
                for s, b, a, last, prev in reversed(_CPI)],
}

#: Year-end S&P buckets: ten of the twenty-seven, the busy middle.
_SPX = [(7000, "0.0300", "0.0400"), (7200, "0.0500", "0.0600"), (7400, "0.0900", "0.1000"),
        (7600, "0.1300", "0.1400"), (7800, "0.2000", "0.2100"), (8000, "0.1600", "0.1700"),
        (8200, "0.0700", "0.0800"), (8400, "0.0400", "0.0500"), (8600, "0.0100", "0.0200")]
KS_SPX = {
    "event_ticker": "KXINXY-26DEC31H1600", "series_ticker": "KXINXY",
    "title": "S&P close price end of 2026?", "sub_title": "On Dec 31, 2026 at 4pm EST",
    "mutually_exclusive": True,
    "markets": [_ks_market("KXINXY-26DEC31H1600-T4000", "3,999.99 or below", "0.0000", "0.0100",
                           "0.0100", "0.0100", stype="less", cap=4000,
                           close="2026-12-31T21:00:00Z")]
               + [_ks_market(f"KXINXY-26DEC31H1600-B{lo + 100}", f"{lo:,} to {lo + 199.99:,.2f}",
                             b, a, a, a, stype="between", floor=lo, cap=lo + 199.99,
                             vol="500000.00", v24="2000.00", close="2026-12-31T21:00:00Z")
                  for lo, b, a in _SPX],
}

#: /api/fedwatch on 2026-10-02: the meetings' expected moves.
FEDWATCH = {
    "as_of": "2026-10-02",
    "meetings": [
        {"date": "2026-10-28", "label": "Oct 2026", "change_bp": 5,
         "outcomes": [{"steps": 0, "prob": 80}, {"steps": 1, "prob": 20}]},
        {"date": "2026-12-09", "label": "Dec 2026", "change_bp": 20.4,
         # Cumulative, as the page's grid shows it -- the trap this test is for.
         "outcomes": [{"steps": 0, "prob": 14.6}, {"steps": 1, "prob": 69},
                      {"steps": 2, "prob": 16.3}]},
        {"date": "2027-01-27", "label": "Jan 2027", "change_bp": 9.6, "outcomes": []},
    ],
}


def _pm(event, topic="growth"):
    return odds.normalize_polymarket(event, fallback_topic=topic)


def _ks(event, topic="rates"):
    return odds.normalize_kalshi(event, topic=topic)


class PriceRuleTests(unittest.TestCase):
    def test_midpoint_inside_ten_cents(self):
        self.assertEqual(odds.display_price(0.82, 0.83, 0.82), 0.825)

    def test_last_trade_when_the_spread_is_wide(self):
        self.assertEqual(odds.display_price(0.40, 0.60, 0.47), 0.47)

    def test_one_sided_or_crossed_book_uses_the_last_trade(self):
        self.assertEqual(odds.display_price(None, 0.30, 0.25), 0.25)
        self.assertEqual(odds.display_price(0.60, 0.50, 0.55), 0.55)

    def test_kalshi_one_cent_offer_reads_as_half_a_cent(self):
        # yes_bid 0 / yes_ask 0.01 is a market nobody will pay a cent for.
        self.assertEqual(odds.display_price(0.0, 0.01, 0.01), 0.005)


class PolymarketTests(unittest.TestCase):
    def test_fed_october_normalises(self):
        ev = _pm(PM_FED_OCT)
        self.assertEqual(ev["key"], "pm:fed-decision-in-october-20260617190323537")
        self.assertEqual(ev["platform"], "polymarket")
        self.assertEqual(ev["topic"], "rates")
        self.assertEqual(ev["kind"], "exclusive")
        self.assertEqual(ev["closes"], "2026-10-29T03:59:00Z")
        self.assertEqual(ev["url"], "https://polymarket.com/event/fed-decision-in-october-20260617190323537")
        no_change = next(o for o in ev["outcomes"] if o["label"] == "No change")
        self.assertEqual(no_change["p"], 0.825)
        self.assertEqual(no_change["id"], "2589812")

    def test_absent_daily_change_is_unknown_not_zero(self):
        no_change = next(o for o in _pm(PM_FED_OCT)["outcomes"] if o["label"] == "No change")
        self.assertIsNone(no_change["d1"])
        self.assertEqual(no_change["w1"], 0.49)

    def test_resolved_rungs_inside_an_open_event_are_dropped(self):
        ev = _pm(PM_INFLATION)
        self.assertEqual([o["label"] for o in ev["outcomes"]], ["Above 4.5%", "Above 5%"])
        self.assertTrue(all(o["p"] < 1 for o in ev["outcomes"]))
        self.assertEqual(ev["topic"], "inflation")
        self.assertEqual(ev["kind"], "ladder")

    def test_single_market_is_binary_and_labelled_by_its_outcome(self):
        ev = _pm(PM_RECESSION)
        self.assertEqual(ev["kind"], "binary")
        self.assertEqual(ev["outcomes"][0]["label"], "Yes")
        self.assertEqual(ev["outcomes"][0]["p"], 0.075)
        self.assertEqual(ev["topic"], "growth")          # macro-graph tag

    def test_closed_event_is_not_listed(self):
        self.assertIsNone(_pm({**PM_RECESSION, "closed": True}))

    def test_placeholder_slots_are_not_outcomes(self):
        """"Largest Company end of October 2026?" carries "Company F" to "T":
        active false, no orders, a 0.5 price nobody quotes."""
        nvda = _pm_market("NVIDIA", 0.951, bid=0.942, ask=0.96, order=0, mid="n")
        nvda.update(active=True, acceptingOrders=True)
        slot = _pm_market("Company F", 0.5, bid=0, ask=1, order=1, mid="f")
        slot.update(active=False, acceptingOrders=False)
        event = {"slug": "largest-company-end-of-october-2026",
                 "title": "Largest Company end of October 2026?", "negRisk": True,
                 "endDate": "2026-11-01T03:59:00Z", "volume": 85832.1, "volume24hr": 19326.4,
                 "tags": [{"slug": "big-tech"}], "markets": [nvda, slot]}
        ev = _pm(event)
        self.assertEqual([o["label"] for o in ev["outcomes"]], ["NVIDIA"])
        self.assertEqual(ev["kind"], "binary")

    def test_fallback_topic_applies_only_when_no_tag_names_one(self):
        untagged = {**PM_RECESSION, "tags": [{"slug": "politics"}]}
        self.assertEqual(_pm(untagged, topic="policy")["topic"], "policy")

    def test_companies_beat_equities_and_rates_beat_policy(self):
        self.assertEqual(odds.classify_tags(["big-tech", "ipos"]), "companies")
        self.assertEqual(odds.classify_tags(["economic-policy", "fed-rates", "tariffs"]), "rates")
        self.assertEqual(odds.classify_tags(["Global-Rates"]), "rates")   # case-insensitive
        self.assertIsNone(odds.classify_tags(["politics"]))


class KalshiTests(unittest.TestCase):
    def test_fed_october_normalises(self):
        ev = _ks(KS_FED_OCT)
        self.assertEqual(ev["key"], "ks:KXFEDDECISION-26OCT")
        self.assertEqual(ev["kind"], "exclusive")
        self.assertEqual(ev["url"], "https://kalshi.com/markets/kxfeddecision/kxfeddecision-26oct")
        hold = next(o for o in ev["outcomes"] if o["id"].endswith("-H0"))
        self.assertEqual(hold["p"], 0.825)
        self.assertEqual(hold["d1"], 0.0)
        hike = next(o for o in ev["outcomes"] if o["id"].endswith("-H25"))
        self.assertEqual(hike["d1"], -0.01)
        self.assertEqual(ev["closes"], "2026-10-28T17:59:00Z")

    def test_volumes_are_summed_across_markets(self):
        ev = _ks(KS_FED_OCT)
        self.assertAlmostEqual(ev["volume_24h"], 747.62 + 6321.26 + 135184.86 + 19052.46 + 3543.15,
                               places=2)
        self.assertIsNone(ev["liquidity"])

    def test_markets_that_are_not_trading_are_dropped(self):
        ev = dict(KS_FED_OCT)
        ev["markets"] = [dict(m) for m in KS_FED_OCT["markets"]]
        ev["markets"][0]["status"] = "settled"
        ev["markets"][1]["status"] = "initialized"
        self.assertEqual(len(_ks(ev)["outcomes"]), 3)

    def test_ladder_rungs_sort_by_strike(self):
        ev = _ks(KS_CPI, "inflation")
        self.assertEqual(ev["kind"], "ladder")
        strikes = [o["label"] for o in odds.select_outcomes(ev, limit=14)]
        self.assertEqual(strikes[0], "Above -0.4%")
        self.assertEqual(strikes[-1], "Above 0.9%")

    def test_a_less_than_bucket_sorts_below_its_cap(self):
        ev = _ks(KS_SPX, "equities")
        self.assertEqual(ev["kind"], "exclusive")
        ordered = sorted(ev["outcomes"], key=lambda o: o["order"])
        self.assertEqual(ordered[0]["label"], "3,999.99 or below")


class SelectionTests(unittest.TestCase):
    def test_a_ladder_shows_the_rungs_around_fifty_percent(self):
        shown = odds.select_outcomes(_ks(KS_CPI, "inflation"), limit=6)
        labels = [o["label"] for o in shown]
        self.assertIn("Above 0.5%", labels)              # 59.5%, the crossing
        self.assertIn("Above 0.6%", labels)
        self.assertNotIn("Above -0.4%", labels)          # 99.5% says nothing
        self.assertEqual(len(shown), 6)
        self.assertEqual(labels, sorted(labels, key=lambda x: float(x.split()[1].rstrip("%"))))

    def test_numeric_buckets_keep_the_likeliest_in_strike_order(self):
        shown = odds.select_outcomes(_ks(KS_SPX, "equities"), limit=4)
        # The four likeliest (20.5%, 16.5%, 13.5%, 9.5%), read low to high.
        self.assertEqual([o["label"] for o in shown],
                         ["7,400 to 7,599.99", "7,600 to 7,799.99",
                          "7,800 to 7,999.99", "8,000 to 8,199.99"])

    def test_named_outcomes_stay_in_probability_order(self):
        ev = {"kind": "exclusive", "numeric": False, "outcomes": [
            {"label": "Alphabet", "p": 0.0045, "order": 0},
            {"label": "NVIDIA", "p": 0.91, "order": 5},
            {"label": "Microsoft", "p": 0.05, "order": 2}]}
        self.assertEqual([o["label"] for o in odds.select_outcomes(ev)],
                         ["NVIDIA", "Microsoft", "Alphabet"])

    def test_trim_keeps_the_full_count_even_when_trimmed_twice(self):
        once = odds.trim(_ks(KS_SPX, "equities"), limit=4)
        twice = odds.trim(once, limit=4)
        self.assertEqual(once["n_outcomes"], 10)
        self.assertEqual(twice["n_outcomes"], 10)
        self.assertEqual(len(twice["outcomes"]), 4)


class BoardTests(unittest.TestCase):
    def test_up_or_down_and_past_markets_are_not_listed(self):
        ev = _pm(PM_RECESSION)
        self.assertTrue(odds.on_board(ev, NOW))
        self.assertFalse(odds.on_board({**ev, "title": "Bitcoin Up or Down on October 4?"}, NOW))
        self.assertFalse(odds.on_board({**ev, "closes": "2026-10-01T00:00:00Z"}, NOW))

    def test_short_dated_price_ladders_wait_but_macro_releases_do_not(self):
        tomorrow = "2026-10-04T21:00:00Z"
        in_six_days = "2026-10-09T21:00:00Z"            # "Bitcoin above ___ on October 9?"
        in_a_month = "2026-11-01T04:00:00Z"             # "...hit in October?"
        crypto = {**_pm(PM_RECESSION), "topic": "crypto"}
        cpi = {**_ks(KS_CPI, "inflation"), "closes": tomorrow}
        self.assertFalse(odds.on_board({**crypto, "closes": tomorrow}, NOW))
        self.assertFalse(odds.on_board({**crypto, "closes": in_six_days}, NOW))
        self.assertTrue(odds.on_board({**crypto, "closes": in_a_month}, NOW))
        self.assertTrue(odds.on_board(cpi, NOW))

    def test_thin_markets_are_not_listed(self):
        thin = {**_pm(PM_RECESSION), "volume": 4000.0, "volume_24h": 50.0}
        self.assertFalse(odds.on_board(thin, NOW))

    def test_one_topic_cannot_fill_the_board(self):
        events = [{"key": f"c{i}", "topic": "crypto", "volume_24h": 1e6 - i} for i in range(30)]
        events.append({"key": "fed", "topic": "rates", "volume_24h": 10.0})
        ranked = odds.rank_board(events, per_topic=5)
        self.assertEqual(sum(1 for e in ranked if e["topic"] == "crypto"), 5)
        self.assertEqual(ranked[-1]["key"], "fed")

    def test_movers_skip_unknown_changes_and_list_an_event_once(self):
        rows = odds.movers([_pm(PM_FED_DEC), _pm(PM_INFLATION), _ks(KS_FED_DEC)])
        keys = [r["key"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        dec = next(r for r in rows if r["key"] == "ks:KXFEDDECISION-26DEC")
        self.assertEqual(dec["label"], "Fed maintains rate")      # 0.24 → 0.28
        self.assertAlmostEqual(dec["d1"], 0.04)
        infl = next(r for r in rows if r["key"].startswith("pm:how-high"))
        self.assertEqual(infl["d1"], -0.035)
        # Polymarket December's biggest daily move is 1pp: under the floor.
        self.assertNotIn("pm:fed-decision-in-december-20260729232808632", keys)


class FedCrosscheckTests(unittest.TestCase):
    def setUp(self):
        self.events = [odds.trim(_pm(PM_FED_OCT)), odds.trim(_pm(PM_FED_DEC)),
                       odds.trim(_ks(KS_FED_OCT)), odds.trim(_ks(KS_FED_DEC))]

    def test_fed_step_reads_both_vocabularies(self):
        cases = {"50+ bps decrease": -2, "25 bps decrease": -1, "No change": 0,
                 "25 bps increase": 1, "50+ bps increase": 2, "Cut >25bps": -2,
                 "Cut 25bps": -1, "Fed maintains rate": 0, "Hike 25bps": 1, "Hike >25bps": 2,
                 "Hike 0bps": 0}
        for label, want in cases.items():
            self.assertEqual(odds.fed_step(label), want, label)
        self.assertEqual(odds.fed_step("anything", "KXFEDDECISION-26OCT-H26"), 2)
        self.assertIsNone(odds.fed_step("Powell resigns"))

    def test_futures_split_mirrors_the_fedwatch_leg(self):
        self.assertEqual(odds.futures_split(5)[0], 0.8)
        self.assertEqual(odds.futures_split(5)[1], 0.2)
        dec = odds.futures_split(20.4)
        self.assertAlmostEqual(dec[0], 0.184, places=4)
        self.assertAlmostEqual(dec[1], 0.816, places=4)
        cut = odds.futures_split(-10)
        self.assertAlmostEqual(cut[-1], 0.4, places=4)
        self.assertAlmostEqual(cut[0], 0.6, places=4)
        self.assertAlmostEqual(odds.futures_split(70)[2], 1.0, places=4)   # 50bp+ folds

    def test_venues_are_normalised_and_report_their_overround(self):
        buckets, total = odds.fed_buckets(self.events[3])                 # Kalshi December
        self.assertAlmostEqual(total, 0.005 + 0.015 + 0.275 + 0.705 + 0.025, places=4)
        self.assertAlmostEqual(sum(buckets.values()), 1.0, places=6)
        self.assertAlmostEqual(buckets[0], 0.275 / total, places=4)

    def test_october_lines_up_three_ways(self):
        rows = odds.fed_crosscheck(FEDWATCH, self.events)
        oct_ = rows[0]
        self.assertEqual(oct_["date"], "2026-10-28")
        self.assertEqual([s["source"] for s in oct_["sources"]], ["futures", "polymarket", "kalshi"])
        holds = {s["source"]: s["hold"] for s in oct_["sources"]}
        self.assertAlmostEqual(holds["futures"], 0.80, places=3)
        self.assertAlmostEqual(holds["polymarket"], 0.825 / 1.0115, places=3)
        self.assertTrue(oct_["agree"])
        self.assertLess(oct_["spread_pp"], 5)

    def test_december_compares_like_with_like(self):
        """The cumulative grid says 14.6% unchanged by December; the futures'
        own December leg is 18.4% hold. The cross-check must use the latter."""
        dec = odds.fed_crosscheck(FEDWATCH, self.events)[1]
        fut = next(s for s in dec["sources"] if s["source"] == "futures")
        self.assertAlmostEqual(fut["hold"], 0.184, places=3)
        self.assertNotAlmostEqual(fut["hold"], 0.146, places=2)
        self.assertEqual(fut["expected_bp"], 20.4)
        pm = next(s for s in dec["sources"] if s["source"] == "polymarket")
        self.assertAlmostEqual(pm["expected_bp"], 18.6, delta=0.3)
        self.assertGreater(dec["spread_pp"], 4)        # the venues price a hold the futures do not

    def test_a_meeting_with_one_source_is_left_out(self):
        rows = odds.fed_crosscheck(FEDWATCH, self.events)
        self.assertEqual([r["date"] for r in rows], ["2026-10-28", "2026-12-09"])

    def test_without_the_futures_the_venues_still_compare(self):
        rows = odds.fed_crosscheck(None, self.events)
        self.assertEqual(len(rows), 2)
        self.assertEqual([s["source"] for s in rows[0]["sources"]], ["polymarket", "kalshi"])

    def test_a_distribution_with_a_hole_is_refused(self):
        broken = dict(self.events[0])
        broken["outcomes"] = [dict(o) for o in broken["outcomes"]]
        broken["outcomes"][0]["label"] = "Powell resigns"
        self.assertIsNone(odds.fed_buckets(broken))


class ReadPromptTests(unittest.TestCase):
    def test_prompt_carries_the_outcomes_the_rules_and_the_shape(self):
        ev = odds.trim(_pm(PM_FED_OCT))
        prompt = odds.build_read_prompt(ev, today="2026-10-03")
        for label in ("No change", "25 bps increase", "50+ bps decrease"):
            self.assertIn(f'"{label}"', prompt)
        self.assertIn("must sum to 100", prompt)
        self.assertIn("2026-10-29 03:59 UTC (26 days from today)", prompt)
        self.assertIn("half of one percent is written 0.5", prompt)
        self.assertIn("Polymarket", prompt)
        self.assertIn("This market will resolve", prompt)
        self.assertIn('"zh"', prompt)

    def test_the_market_is_not_shown_its_own_answer(self):
        """The first live read, shown the prices, answered 82.5% to a market at
        82.5%. No price, volume, depth or recent move may reach the model."""
        ev = odds.trim(_pm(PM_FED_OCT))
        prompt = odds.build_read_prompt(ev, today="2026-10-03")
        for leak in ("82.5", "82%", "17.5", "18%", "liquidity", "Traded", "bid", "ask ",
                     "1-day", "1-week", "+49", "$24", "volume"):
            self.assertNotIn(leak, prompt, leak)
        self.assertIn("deliberately not given", prompt)
        ks = odds.build_read_prompt(odds.trim(_ks(KS_FED_DEC)), today="2026-10-03")
        for leak in ("27.5", "28%", "70.5", "open interest", "contracts"):
            self.assertNotIn(leak, ks, leak)

    def test_a_partial_field_is_told_it_is_partial(self):
        ev = odds.trim(_ks(KS_SPX, "equities"), limit=4)
        prompt = odds.build_read_prompt(ev, today="2026-10-03")
        self.assertIn("a selection from 10 listed outcomes", prompt)
        self.assertIn("at most 100", prompt)

    def test_a_ladder_is_told_to_stay_monotone(self):
        prompt = odds.build_read_prompt(odds.trim(_ks(KS_CPI, "inflation")), today="2026-10-03")
        self.assertIn("harder threshold", prompt)


class ReadParseTests(unittest.TestCase):
    def setUp(self):
        self.ev = odds.trim(_pm(PM_FED_OCT))

    def _answer(self, probs, **extra):
        body = {"probabilities": probs, "confidence": "medium", "evidence_date": "2026-10-02",
                "en": {"summary": "Hold is likely.", "drivers": ["Inflation sticky"],
                       "watch": ["CPI on 10-14"]},
                "zh": {"summary": "大概率维持不变。", "drivers": ["通胀粘性"], "watch": ["10月14日CPI"]}}
        body.update(extra)
        return "Here you go:\n```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"

    def _probs(self, hold=78, hike=19):
        return {"50+ bps decrease": 0.5, "25 bps decrease": 1, "No change": hold,
                "25 bps increase": hike, "50+ bps increase": 1.5}

    def test_a_good_answer_parses_in_both_languages(self):
        got = odds.parse_read(self._answer(self._probs()), self.ev)
        self.assertAlmostEqual(got["ai"]["No change"], 0.78, places=3)
        self.assertEqual(got["confidence"], "medium")
        self.assertEqual(got["zh"]["summary"], "大概率维持不变。")
        self.assertEqual(got["en"]["watch"], ["CPI on 10-14"])

    def test_labels_match_without_regard_to_case_or_spacing(self):
        probs = {k.upper().replace(" ", "  "): v for k, v in self._probs().items()}
        got = odds.parse_read(self._answer(probs), self.ev)
        self.assertIn("No change", got["ai"])

    def test_a_missing_outcome_is_refused_not_guessed(self):
        probs = self._probs()
        del probs["50+ bps increase"]
        with self.assertRaises(odds.ReadParseError) as ctx:
            odds.parse_read(self._answer(probs), self.ev)
        self.assertEqual(ctx.exception.reason, "missing")

    def test_rounding_drift_is_rescaled_but_a_bad_sum_is_refused(self):
        got = odds.parse_read(self._answer(self._probs(hold=80, hike=20)), self.ev)  # sums to 103
        self.assertAlmostEqual(sum(got["ai"].values()), 1.0, places=3)
        with self.assertRaises(odds.ReadParseError) as ctx:
            odds.parse_read(self._answer(self._probs(hold=80, hike=60)), self.ev)
        self.assertEqual(ctx.exception.reason, "sum")

    def test_a_complete_one_of_many_set_in_fractions_is_read_as_fractions(self):
        probs = {k: v / 100 for k, v in self._probs(hold=78, hike=19).items()}
        got = odds.parse_read(self._answer(probs), self.ev)
        self.assertAlmostEqual(got["ai"]["No change"], 0.78, places=3)

    def test_half_a_percent_on_a_long_shot_is_half_a_percent(self):
        """The one place a fraction guess would do harm: a binary long shot
        written as 0.5 must be recorded as 0.5%, not 50%."""
        ev = odds.trim(_pm(PM_RECESSION))
        got = odds.parse_read(self._answer({"Yes": 0.5}), ev)
        self.assertAlmostEqual(got["ai"]["Yes"], 0.005, places=4)

    def test_an_unfenced_object_with_a_trailing_comma_still_parses(self):
        text = ('{"probabilities": {"50+ bps decrease": 0.5, "25 bps decrease": 1, '
                '"No change": 78, "25 bps increase": 19, "50+ bps increase": 1.5,}, '
                '"confidence": "HIGH"}')
        got = odds.parse_read(text, self.ev)
        self.assertEqual(got["confidence"], "high")
        self.assertEqual(got["zh"]["summary"], "")

    def test_no_json_at_all(self):
        with self.assertRaises(odds.ReadParseError) as ctx:
            odds.parse_read("I cannot help with that.", self.ev)
        self.assertEqual(ctx.exception.reason, "no_json")

    def test_a_ladder_need_not_sum_to_anything(self):
        ev = odds.trim(_ks(KS_CPI, "inflation"))
        probs = {o["label"]: 50 for o in odds.read_outcomes(ev)}
        got = odds.parse_read(self._answer(probs), ev)
        self.assertTrue(all(v == 0.5 for v in got["ai"].values()))

    def test_a_partial_exclusive_field_may_leave_room_for_the_rest(self):
        ev = odds.trim(_ks(KS_SPX, "equities"), limit=4)
        probs = {o["label"]: 15 for o in odds.read_outcomes(ev)}           # 60 of 100
        got = odds.parse_read(self._answer(probs), ev)
        self.assertAlmostEqual(sum(got["ai"].values()), 0.60, places=3)

    def test_gaps_are_in_points(self):
        self.assertEqual(odds.gaps({"a": 0.70}, {"a": 0.825}), {"a": -12.5})


class SettlementTests(unittest.TestCase):
    def test_brier_is_mean_squared_error_over_shared_outcomes(self):
        self.assertAlmostEqual(odds.brier({"hold": 0.8, "hike": 0.2}, {"hold": 1, "hike": 0}), 0.04)
        self.assertIsNone(odds.brier({"hold": 0.8}, {"hike": 1}))

    def test_polymarket_resolution(self):
        resolved = {"closed": True, "outcomePrices": json.dumps(["1", "0"])}
        self.assertEqual(odds.polymarket_result(resolved), 1)
        self.assertEqual(odds.polymarket_result({**resolved, "outcomePrices": '["0","1"]'}), 0)
        self.assertEqual(odds.polymarket_result({**resolved, "outcomePrices": '["0.5","0.5"]'}),
                         "void")
        self.assertIsNone(odds.polymarket_result({"closed": False, "outcomePrices": '["1","0"]'}))

    def test_kalshi_resolution(self):
        self.assertEqual(odds.kalshi_result({"result": "yes", "status": "finalized"}), 1)
        self.assertEqual(odds.kalshi_result({"result": "no", "status": "settled"}), 0)
        self.assertEqual(odds.kalshi_result({"result": "scalar", "status": "finalized"}), "void")
        self.assertIsNone(odds.kalshi_result({"result": "", "status": "active"}))

    def _row(self, results, ai=(0.7, 0.3), mk=(0.825, 0.175)):
        labels = ("No change", "25 bps increase")
        return {"outcomes": [{"label": lab, "ai_p": a, "market_p": m, "result": r}
                             for lab, a, m, r in zip(labels, ai, mk, results)]}

    def test_a_row_scores_only_once_every_outcome_has_resolved(self):
        self.assertIsNone(odds.score_row(self._row((1, None))))
        scored = odds.score_row(self._row((1, 0)))
        self.assertEqual(scored["status"], "settled")
        self.assertAlmostEqual(scored["brier_ai"], 0.09)
        self.assertAlmostEqual(scored["brier_market"], 0.030625)

    def test_void_outcomes_drop_from_both_scores_alike(self):
        scored = odds.score_row(self._row((1, "void")))
        self.assertAlmostEqual(scored["brier_ai"], 0.09)            # (0.7-1)^2 alone
        self.assertAlmostEqual(scored["brier_market"], 0.030625)
        self.assertEqual(odds.score_row(self._row(("void", "void")))["status"], "void")

    def test_an_unsettled_ledger_reports_no_score_rather_than_zero(self):
        summary = odds.ledger_summary([{"status": "open"}, {"status": "open"}])
        self.assertEqual(summary["recorded"], 2)
        self.assertEqual(summary["open"], 2)
        self.assertIsNone(summary["brier_ai"])
        self.assertIsNone(summary["brier_market"])

    def test_summary_compares_the_ai_with_the_market_over_the_same_rows(self):
        rows = [{"status": "settled", "brier_ai": 0.09, "brier_market": 0.03},
                {"status": "settled", "brier_ai": 0.01, "brier_market": 0.05},
                {"status": "void"}, {"status": "open"}]
        s = odds.ledger_summary(rows)
        self.assertEqual((s["settled"], s["void"], s["open"]), (2, 1, 1))
        self.assertAlmostEqual(s["brier_ai"], 0.05)
        self.assertAlmostEqual(s["brier_market"], 0.04)
        self.assertEqual((s["ai_better"], s["market_better"]), (1, 1))


class TimestampTests(unittest.TestCase):
    def test_kalshi_short_microseconds_parse(self):
        self.assertEqual(odds.parse_ts("2026-09-28T20:18:26.89Z"),
                         datetime(2026, 9, 28, 20, 18, 26, 890000, tzinfo=timezone.utc))

    def test_garbage_is_none(self):
        self.assertIsNone(odds.parse_ts("soon"))
        self.assertIsNone(odds.parse_ts(None))


if __name__ == "__main__":
    unittest.main()
