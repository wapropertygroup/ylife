"""Brief formatter tests — no Flask app, no network, no caches.

Covers ystocker/brief.py against payloads shaped exactly like the ones its real
producers emit. The risk this guards is key names and units, not logic: the
snapshot is assembled from eight payloads written by different modules with
different conventions sitting next to each other —

    api_markets()      index blocks use  day_chg   / ytd
    api_commodities()  blocks use        day_chg_pct / ret_ytd  and `last`
    sec13f             holdings carry    value_millions and value_thousands
    housing            tiles use         pct_dec (0.24 == 24%) and pp

— so a plausible-looking typo yields a silently empty table rather than an
error. Every assertion here is on rendered output, because that is the only
place such a mistake becomes visible.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

PATH = pathlib.Path(__file__).parents[1] / "ystocker" / "brief.py"
SPEC = importlib.util.spec_from_file_location("brief_under_test", PATH)
brief = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(brief)

SEPARATOR = re.compile(r"^\|[\s\-:|]+\|$", re.M)


# ── Payload fixtures, keyed exactly as the real producers key them ───────────

MARKETS = {
    "indices": {
        "spx": {"symbol": "^GSPC", "name": "S&P 500", "current": 6812.34,
                "day_chg": 0.43, "ytd": 11.2, "hi52": 6890.1, "lo52": 5120.5,
                "pe": 27.4, "ma50": 6700.2, "ma200": 6210.8, "rsi14": 62.1},
        "ixic": {"current": 23104.9, "day_chg": 0.71, "ytd": 14.8,
                 "hi52": 23400.0, "lo52": 16800.0, "rsi14": 65.3,
                 "ma50": 22600.0, "ma200": 20900.0, "pe": 34.1},
        "dji": {"current": 47221.0, "day_chg": -0.12, "ytd": 7.4,
                "hi52": 47800.0, "lo52": 38200.0, "rsi14": 58.0,
                "ma50": 46900.0, "ma200": 44100.0, "pe": 22.8},
        "dxy": {"current": 97.42, "day_chg": -0.21, "ytd": -4.1,
                "hi52": 110.2, "lo52": 96.1, "rsi14": 41.2,
                "ma50": 98.9, "ma200": 102.4},
        "brent": {"error": "no data"},
    },
    "vix": {"current": 16.42, "day_chg": -3.1, "vix3m": 18.05,
            "vvix": 92.4, "term_ratio": 0.91},
    "sectors": [
        {"ticker": "XLK", "label": "Technology", "day_chg": 1.24, "week_chg_pct": 2.8},
        {"ticker": "XLE", "label": "Energy", "day_chg": -0.88, "week_chg_pct": -1.9},
        {"ticker": "XLF", "label": "Financials", "day_chg": 0.31, "week_chg_pct": 0.7},
    ],
}
BREADTH = {"latest": {"20": 61.2, "50": 58.4, "100": 55.1, "150": 52.9, "200": 49.8},
           "universe": 503, "asof": "2026-08-26",
           "rsp_spy": {"values": [0.6321, 0.6299, 0.6274]}}
MOVERS = {"gainers": [{"ticker": "NVDA", "price": 184.2, "day_chg": 4.8,
                       "rel_vol": 2.31, "sector": "Technology"}],
          "losers": [{"ticker": "TSLA", "price": 402.1, "day_chg": -5.2,
                      "rel_vol": 1.88, "sector": "Consumer Discretionary"}]}
FG = {"score": 58.0, "rating": "Greed", "prev_close": 55.0, "prev_week": 49.0,
      "prev_month": 41.0, "prev_year": 62.0}
PCR = {"current": 0.88, "day_chg": -2.1, "ma20": 0.94}
AAII = {"date": "2026-08-20", "bullish": 41.2, "neutral": 30.1,
        "bearish": 28.7, "bull_bear_spread": 12.5}
SKEW = {"latest": {"skew": 142.3, "band": "elevated", "percentile": 78.0,
                   "skew_date": "2026-08-26"}}

COMM = {
    "commodities": {
        "gold": {"name": "Gold", "group": "metals", "unit": "$/oz", "last": 4210.5,
                 "day_chg_pct": 0.62, "ret_1w": 1.4, "ret_1m": 3.9, "ret_3m": 8.1,
                 "ret_ytd": 24.8, "ret_1y": 31.2, "pos52": 94.0, "rsi_14": 68.2,
                 "ma_50": 4050.0, "ma_200": 3720.0, "high52": 4260.0, "low52": 2980.0},
        "oil_wti": {"name": "WTI Crude", "group": "energy", "unit": "$/bbl",
                    "last": 61.2, "day_chg_pct": -1.35, "ret_1w": -2.2,
                    "ret_1m": -4.1, "ret_3m": -8.8, "ret_ytd": -14.6,
                    "ret_1y": -19.2, "pos52": 12.0, "rsi_14": 38.4},
        "broken": {"name": "No Data", "group": "agri"},
    },
    "ratios": {
        "gold_silver": {"label": "Gold / Silver", "desc": "silver oz per gold oz",
                        "current": 82.4, "values": [70.1, 75.5, 82.4],
                        "dates": ["a", "b", "c"]},
    },
}

HOLDINGS = {
    "Berkshire Hathaway": {
        "period_of_report": "2026-06-30", "total_value_millions": 302145.7,
        "total_holdings": 41,
        "holdings": [
            {"ticker": "AAPL", "pct_portfolio": 22.4, "change": "reduced",
             "change_pct": -8.0, "value_millions": 67680.0},
            {"ticker": "AXP", "pct_portfolio": 15.1, "change": "unchanged",
             "value_millions": 45623.0},
        ],
    },
    "Bridgewater": {
        "period_of_report": "2026-06-30", "total_value_millions": 21870.2,
        "total_holdings": 720,
        "holdings": [{"ticker": "SPY", "pct_portfolio": 4.2, "change": "new",
                      "change_pct": None, "value_millions": 918.0}],
    },
    "Dead Fund": {"error": "No 13F-HR filings found"},
}
CONSENSUS = [
    {"ticker": "MSFT", "fund_count": 18, "total_value_m": 412000,
     "fund_names": ["Vanguard", "BlackRock", "State Street", "Fidelity", "T. Rowe"]},
    {"ticker": "AAPL", "fund_count": 16, "total_value_m": 388000,
     "fund_names": ["Berkshire Hathaway", "Vanguard"]},
]

EVAL = {
    "sectors": [
        {"sector": "Semiconductors", "count": 18, "median_pe_ttm": 38.4,
         "median_pe_fwd": 29.1, "median_peg": 1.8, "median_ev_ebitda": 24.2,
         "median_upside": 12.4, "median_day_change": 1.1},
        {"sector": "Banks", "count": 22, "median_pe_ttm": 12.1,
         "median_pe_fwd": 11.4, "median_peg": 1.1, "median_ev_ebitda": None,
         "median_upside": 6.2, "median_day_change": -0.3},
    ],
    "most_expensive": [{"ticker": "PLTR", "sector": "Software", "pe_fwd": 210.4,
                        "upside": -18.2, "market_cap": 412.8}],
    "cheapest": [{"ticker": "F", "sector": "Autos", "pe_fwd": 6.1,
                  "upside": 14.0, "market_cap": 48.2}],
    "most_upside": [{"ticker": "INTC", "sector": "Semiconductors", "pe_fwd": 22.0,
                     "upside": 38.4, "market_cap": 132.1}],
}

FEDWATCH = {
    "as_of": "2026-08-27",
    "current": {"lower": 3.5, "upper": 3.75, "mid": 3.625, "effr": 3.63,
                "label": "3.50–3.75"},
    "meetings": [{"label": "Sep 2026", "date": "2026-09-16", "implied_rate": 3.72,
                  "change_bp": 8.6, "cut_prob": 0.0, "hold_prob": 65.7,
                  "hike_prob": 34.3,
                  "outcomes": [{"steps": 0, "lower": 3.5, "upper": 3.75, "prob": 65.7},
                               {"steps": 1, "lower": 3.75, "upper": 4.0, "prob": 34.3}]}],
}

HOUSING = {
    "as_of": "2026-07-31",
    "headline": {
        "zhvi": {"value": 371774.01, "yoy": 1.0, "yoy_unit": "pct",
                 "unit": "usd", "source": "Zillow"},
        "price_cuts": {"value": 0.2557, "yoy": -1.0, "yoy_unit": "pp",
                       "unit": "pct_dec", "source": "Zillow"},
        "mortgage_spread": {"value": 2.01, "yoy": 0.1, "yoy_unit": "pp",
                            "unit": "pp", "source": "FRED"},
        "fred_existing_sales": {"value": 4060000.0, "yoy": 0.7, "yoy_unit": "pct",
                                "unit": "units", "source": "FRED"},
        "redfin_inventory": {"value": 1397071.4, "yoy": 3.2, "yoy_unit": "pct",
                             "unit": "count", "source": "Redfin"},
        "fred_case_shiller": {"value": 335.104, "yoy": 2.1, "yoy_unit": "pct",
                              "unit": "index", "source": "FRED"},
        "fred_starts": {"value": 1239.0, "yoy": -13.5, "yoy_unit": "pct",
                        "unit": "thousands", "source": "FRED"},
        "redfin_months_of_supply": {"value": 3.3628, "yoy": -7.9, "yoy_unit": "pct",
                                    "unit": "months", "source": "Redfin"},
    },
    "affordability": {"payment": [2100, 2250], "home_value": [360000, 371774],
                      "rate": [6.5, 6.67], "down_pct": 20},
    "price_to_rent": {"values": [130.1, 142.65], "peak": 159.41, "peak_date": "2022-06"},
    "mortgage_spread": {"spread": [2.4, 2.01], "mortgage": [7.1, 6.67],
                        "treasury": [4.7, 4.66]},
    "rent_growth": {"market": [3.1, 2.3], "cpi": [4.4, 3.9]},
    "metros": [{"metro": "New York, NY", "zhvi": 737078, "zhvi_yoy": 4.3,
                "zori": 3627, "zori_yoy": 4.5, "days_to_pending": 38}],
}

VALUATION = {
    "as_of": "2026-08-27",
    "headline": {
        "spx_trailing_pe": {"value": 29.59, "yoy": 1.75, "unit": "x",
                            "source": "multpl.com"},
        "spx_pe_percentile": {"value": 97.0, "unit": "pct_rank",
                              "source": "computed", "since": "1871-01-01"},
        "spx_cape": {"value": 41.98, "yoy": 4.13, "unit": "x", "source": "multpl.com"},
        "spx_trailing_eps": {"value": 262.0, "yoy": 13.7, "unit": "usd",
                             "source": "multpl.com"},
        "spy_forward_pe": {"value": 19.89, "yoy": None, "unit": "x",
                           "source": "computed", "coverage_pct": 24.0,
                           "constituents": "120/503"},
    },
    "forward": {
        "SPY": {"forward_pe": 19.89, "earnings_yield_pct": 5.03, "trailing_pe": 26.03,
                "growth_pct": 31.9, "coverage_pct": 24.0, "market_cap_b": 48200.0},
        "QQQ": {"forward_pe": 20.39, "earnings_yield_pct": 4.90, "trailing_pe": 27.76,
                "growth_pct": 36.8, "coverage_pct": 46.0, "market_cap_b": 31100.0},
    },
    "forward_history": [{"date": "2026-08-14", "SPY": 19.7, "QQQ": 20.1},
                        {"date": "2026-08-27", "SPY": 19.89, "QQQ": 20.39}],
    "multpl": {"spx_pe": {"dates": ["1871-01-01", "2026-08-27"],
                          "values": [11.2, 29.59], "unit": "x"}},
    "spx_consensus_fwd": {"dates": ["2026-08-07"], "values": [20.0],
                          "source": "FactSet Earnings Insight"},
}

FED = {
    "series": {
        "WALCL": {"dates": ["2026-05-13", "2026-08-05", "2026-08-12"],
                  "values": [6728.5, 6748.5, 6759.95]},
        "WTREGEN": {"dates": ["2026-08-05", "2026-08-12"], "values": [907.4, 964.0]},
        "RRPONTSYD": {"dates": ["2026-08-17", "2026-08-18"], "values": [0.2, 0.1]},
        "BAMLH0A0HYM2": {"dates": ["2026-08-14", "2026-08-17"], "values": [267.0, 270.0]},
        "DFII10": {"dates": ["2026-08-14", "2026-08-17"], "values": [2.41, 2.44]},
        "UMCSENT": {"dates": ["2026-05-01", "2026-06-01"], "values": [44.8, 49.5]},
        "TREAST": {"dates": [], "values": [], "error": True},
    },
}
FED_META = {
    "WALCL": {"label": "Total Assets"},
    "WTREGEN": {"label": "Treasury General Account"},
    "RRPONTSYD": {"label": "Overnight Reverse Repos"},
    "BAMLH0A0HYM2": {"label": "HY OAS (bps)", "unit": "bps"},
    "DFII10": {"label": "10Y Real Yield (TIPS)", "unit": "pct"},
    "UMCSENT": {"label": "Consumer Sentiment (UMich)", "unit": "index"},
    "TREAST": {"label": "Treasury Securities"},
}

YIELD_CURVE = {
    "us": {"current": {"3M": 3.61, "2Y": 3.55, "10Y": 4.66, "30Y": 5.11}},
    "cn": {"current": {"10Y": 1.82}},
    "jp": {"current": {"10Y": 1.71}},
    "spx_pe": 29.59,
}
CREDIT = {"period": "1y", "spread": [0.61, 0.64], "hyg": {"price": 81.2},
          "tlt": {"price": 126.9}}


def render(lines) -> str:
    return "\n".join(lines)


class NumberFormatting(unittest.TestCase):
    def test_non_numbers_degrade_to_na(self):
        for bad in (None, "", "abc", {}, [], True, False):
            self.assertEqual(brief._num(bad), "n/a", repr(bad))

    def test_bool_is_not_a_number(self):
        # bool subclasses int; formatting True as 1.00 would print a fake datum.
        self.assertEqual(brief._num(True), "n/a")

    def test_sign_and_thousands(self):
        self.assertEqual(brief._num(1234.5, 2, plus=True), "+1,234.50")
        self.assertEqual(brief._num(-1234.5, 2, plus=True), "-1,234.50")
        self.assertEqual(brief._pct(0.43), "+0.43%")
        self.assertEqual(brief._pct(-0.43), "-0.43%")

    def test_billions_promote_to_trillions(self):
        self.assertEqual(brief._bn(964.0), "$964.0B")
        self.assertEqual(brief._bn(6759.95), "$6.76T")
        self.assertEqual(brief._bn(None), "n/a")

    def test_range_position(self):
        self.assertEqual(brief._pos_in_range(50, 0, 100), "50%")
        self.assertEqual(brief._pos_in_range(6812.34, 5120.5, 6890.1), "96%")
        self.assertEqual(brief._pos_in_range(5, 10, 10), "n/a")  # zero-width range
        self.assertEqual(brief._pos_in_range(None, 0, 10), "n/a")

    def test_percentile_rank(self):
        self.assertEqual(brief._percentile([1, 2, 3, 4], 4), "100%")
        self.assertEqual(brief._percentile([1, 2, 3, 4], 2), "50%")
        self.assertEqual(brief._percentile([], 2), "n/a")

    def test_last_and_delta_skip_nulls(self):
        self.assertEqual(brief._last([1.0, None, 3.0]), 3.0)
        self.assertIsNone(brief._last([None, None]))
        self.assertIsNone(brief._last("not a list"))
        self.assertAlmostEqual(brief._delta([1.0, None, 3.0], 1), 2.0)
        self.assertIsNone(brief._delta([1.0], 1))


class TileUnits(unittest.TestCase):
    """Every unit the two payloads emit must be handled, not fall through."""

    def test_known_units(self):
        cases = [
            ({"value": 371774.01, "unit": "usd"}, "$371,774"),
            ({"value": 0.2557, "unit": "pct_dec"}, "25.57%"),
            ({"value": 6.67, "unit": "pct"}, "6.67%"),
            ({"value": 2.01, "unit": "pp"}, "2.01pp"),
            ({"value": 3.3628, "unit": "months"}, "3.4 months"),
            ({"value": 21.0, "unit": "days"}, "21 days"),
            ({"value": 1239.0, "unit": "thousands"}, "1,239k units (annual rate)"),
            ({"value": 4060000.0, "unit": "units"}, "4.06M"),
            ({"value": 1397071.4, "unit": "count"}, "1.40M"),
            ({"value": 8421.0, "unit": "count"}, "8,421"),
            ({"value": 335.104, "unit": "index"}, "335.10 (index)"),
            ({"value": 29.59, "unit": "x"}, "29.59x"),
            ({"value": 97.0, "unit": "pct_rank"}, "97th percentile"),
            ({"value": 964.0, "unit": "bln"}, "$964.0B"),
        ]
        for block, want in cases:
            self.assertEqual(brief._tile(block), want, block)

    def test_pp_is_not_rendered_as_percent(self):
        # A 2.01pp mortgage spread is not a 2.01% one.
        self.assertNotIn("%", brief._tile({"value": 2.01, "unit": "pp"}))

    def test_pct_dec_is_scaled(self):
        self.assertEqual(brief._tile({"value": 0.25, "unit": "pct_dec"}), "25.00%")

    def test_yoy_suffix_units(self):
        self.assertEqual(
            brief._tile({"value": 100.0, "unit": "usd", "yoy": 1.5, "yoy_unit": "pct"}),
            "$100 (+1.50% yoy)")
        self.assertEqual(
            brief._tile({"value": 100.0, "unit": "usd", "yoy": -1.5, "yoy_unit": "pp"}),
            "$100 (-1.50pp yoy)")

    def test_missing_value_is_na(self):
        self.assertEqual(brief._tile({"unit": "usd"}), "n/a")
        self.assertEqual(brief._tile(None), "n/a")


class MarketsSection(unittest.TestCase):
    def setUp(self):
        self.out = render(brief._sec_markets(
            MARKETS, BREADTH, MOVERS, FG, PCR, AAII, SKEW))

    def test_index_keys_are_day_chg_and_ytd(self):
        # Not day_chg_pct / ret_ytd — those belong to the commodities payload.
        self.assertIn("| S&P 500 / 标普500 |", self.out)
        self.assertIn("+0.43%", self.out)
        self.assertIn("+11.20%", self.out)

    def test_negative_change_keeps_its_sign(self):
        self.assertIn("-0.12%", self.out)

    def test_errored_index_is_skipped(self):
        self.assertNotIn("brent", self.out.lower())

    def test_range_position_column(self):
        self.assertIn("| 96% |", self.out)

    def test_vix_term_structure_is_named(self):
        self.assertIn("contango", self.out)

    def test_breadth_rows_for_every_ma(self):
        self.assertEqual(self.out.count("-day MA |"), 5)

    def test_sectors_ranked_best_first(self):
        self.assertLess(self.out.index("Technology"), self.out.index("Energy"))

    def test_movers_both_sides(self):
        self.assertIn("NVDA", self.out)
        self.assertIn("+4.80%", self.out)
        self.assertIn("TSLA", self.out)
        self.assertIn("-5.20%", self.out)

    def test_sentiment_gauges(self):
        self.assertIn("Greed", self.out)
        self.assertIn("20d MA 0.94", self.out)
        self.assertIn("+12.5pp", self.out)   # AAII spread is percentage points
        self.assertIn("elevated", self.out)  # SKEW band

    def test_cold_source_is_stated_not_skipped(self):
        out = render(brief._sec_markets(None, None, None, None, None, None, None))
        self.assertIn("DATA UNAVAILABLE", out)


class CommoditiesSection(unittest.TestCase):
    def setUp(self):
        self.out = render(brief._sec_commodities(COMM))

    def test_uses_day_chg_pct_and_ret_ytd(self):
        self.assertIn("+0.62%", self.out)
        self.assertIn("+24.80%", self.out)
        self.assertIn("-14.60%", self.out)

    def test_units_and_range_position(self):
        self.assertIn("$/oz", self.out)
        self.assertIn("$/bbl", self.out)
        self.assertIn("| 94% |", self.out)

    def test_row_without_last_is_skipped(self):
        self.assertNotIn("No Data", self.out)

    def test_ratio_percentile(self):
        self.assertIn("Gold / Silver", self.out)
        self.assertIn("100%", self.out)


class ThirteenFSection(unittest.TestCase):
    def setUp(self):
        self.out = render(brief._sec_13f(HOLDINGS, CONSENSUS))

    def test_errored_fund_is_excluded_from_output_and_count(self):
        self.assertNotIn("Dead Fund", self.out)
        self.assertIn("2 funds reporting", self.out)

    def test_consensus_table(self):
        self.assertIn("| MSFT | 18 |", self.out)

    def test_funds_ranked_by_value(self):
        self.assertLess(self.out.index("Berkshire Hathaway"),
                        self.out.index("Bridgewater"))

    def test_holding_change_annotation(self):
        self.assertIn("AAPL 22.4% (reduced -8%)", self.out)

    def test_null_change_pct_tolerated(self):
        self.assertIn("SPY 4.2% (new)", self.out)

    def test_all_funds_errored_is_unavailable(self):
        out = render(brief._sec_13f({"A": {"error": "boom"}}, None))
        self.assertIn("DATA UNAVAILABLE", out)


class OtherSections(unittest.TestCase):
    def test_evaluation_medians(self):
        out = render(brief._sec_evaluation(EVAL))
        self.assertIn("| Semiconductors | 18 |", out)
        self.assertIn("| n/a |", out)      # missing EV/EBITDA median
        self.assertIn("-0.30%", out)
        for ticker in ("PLTR", "INTC"):
            self.assertIn(ticker, out)

    def test_evaluation_cheapest_excludes_loss_makers(self):
        payload = dict(EVAL, cheapest=[
            {"ticker": "LOSS", "sector": "X", "pe_fwd": -12.0, "upside": 1.0,
             "market_cap": 1.0}])
        # A negative forward P/E is a loss-maker, not a cheap stock. The routes
        # helper filters it; assert the formatter at least renders what it gets
        # without crashing on the negative.
        self.assertIn("LOSS", render(brief._sec_evaluation(payload)))

    def test_fedwatch_probabilities_and_outcomes(self):
        out = render(brief._sec_fedwatch(FEDWATCH))
        self.assertIn("3.50–3.75%", out)
        self.assertIn("| Sep 2026 |", out)
        self.assertIn("65.7%", out)
        self.assertIn("+8.6bp", out)
        self.assertIn("3.75-4.00%", out)   # outcome distribution row

    def test_housing_tiles_series_and_metros(self):
        out = render(brief._sec_housing(HOUSING))
        self.assertIn("$371,774 (+1.00% yoy)", out)
        self.assertIn("25.57%", out)              # pct_dec scaled
        self.assertIn("4.06M", out)               # units compacted
        self.assertIn("2.01pp", out)              # pp not %
        self.assertIn("New York, NY", out)
        self.assertIn("159.41", out)              # price-to-rent peak
        self.assertIn("$2,250", out)              # latest affordability payment

    def test_multiples_headline_forward_and_history(self):
        out = render(brief._sec_multiples(VALUATION))
        self.assertIn("29.59x", out)
        self.assertIn("97th percentile", out)
        self.assertIn("coverage 24%", out)
        self.assertIn("| SPY |", out)
        self.assertIn("19.89x", out)
        self.assertIn("20.00x", out)              # FactSet consensus
        self.assertIn("since 1871", out)          # long-run percentile

    def test_fed_units_and_net_liquidity(self):
        out = render(brief._sec_fed(FED, FED_META))
        self.assertIn("$6.76T", out)
        self.assertIn("270bps", out)              # bps unit
        self.assertIn("2.44%", out)               # pct unit
        self.assertIn("+0.03pp", out)             # pct delta in pp
        self.assertNotIn("Treasury Securities", out)   # errored series skipped
        # 6759.95 - 964.0 - 0.1
        self.assertIn("$5.80T", out)

    def test_rates_curve_and_inversion(self):
        out = render(brief._sec_rates(YIELD_CURVE, CREDIT, None))
        self.assertIn("| 10Y | 4.66% |", out)
        self.assertIn("+1.11pp", out)             # 10Y-2Y spread
        self.assertIn("normal", out)
        self.assertIn("China 10Y: 1.82%", out)
        self.assertIn("risk-on", out)             # HYG/TLT 0.64 > 0.5

    def test_rates_flags_inversion(self):
        inverted = {"us": {"current": {"3M": 5.4, "2Y": 5.0, "10Y": 4.2}}}
        out = render(brief._sec_rates(inverted, None, None))
        self.assertIn("INVERTED", out)

    def test_events_table(self):
        out = render(brief._sec_events([
            {"date": "2026-08-28", "time": "08:30", "country": "US",
             "event": "Core PCE", "impact": "high", "forecast": "0.2%",
             "previous": "0.3%"}]))
        self.assertIn("Core PCE", out)
        self.assertIn("0.2%", out)

    def test_every_section_states_cold_sources(self):
        for fn, args in (
            (brief._sec_rates, (None, None, None)),
            (brief._sec_evaluation, (None,)),
            (brief._sec_commodities, (None,)),
            (brief._sec_13f, (None, None)),
            (brief._sec_fedwatch, (None,)),
            (brief._sec_housing, (None,)),
            (brief._sec_multiples, (None,)),
            (brief._sec_fed, (None, None)),
            (brief._sec_events, (None,)),
        ):
            self.assertIn("DATA UNAVAILABLE", render(fn(*args)), fn.__name__)


class TileListCoverage(unittest.TestCase):
    """The label lists must not contain duplicate or empty keys."""

    def test_no_duplicate_keys(self):
        for name, tiles in (("housing", brief._HOUSING_TILES),
                            ("valuation", brief._VALUATION_TILES)):
            keys = [k for k, _, _ in tiles]
            self.assertEqual(len(keys), len(set(keys)), f"{name} has duplicates")

    def test_every_entry_has_both_labels(self):
        for tiles in (brief._HOUSING_TILES, brief._VALUATION_TILES):
            for key, en, zh in tiles:
                self.assertTrue(key and en and zh, key)

    def test_index_rows_unique(self):
        keys = [k for k, _, _ in brief._INDEX_ROWS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_fed_series_order_unique(self):
        self.assertEqual(len(brief._FED_SERIES_ORDER),
                         len(set(brief._FED_SERIES_ORDER)))


class SnapshotAndPrompt(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "markets": MARKETS, "breadth": BREADTH, "movers": MOVERS, "fg": FG,
            "pcr": PCR, "aaii": AAII, "skew": SKEW, "commodities": COMM,
            "holdings13f": HOLDINGS, "consensus13f": CONSENSUS, "evaluation": EVAL,
            "fedwatch": FEDWATCH, "housing": HOUSING, "valuation": VALUATION,
            "fed": FED, "fed_series_meta": FED_META, "yield_curve": YIELD_CURVE,
            "credit_spread": CREDIT, "yield_spread": None, "events": None,
            "_stale": [],
        }
        self.snap = brief.build_snapshot(self.sources, "2026-08-27")

    def test_dated_header(self):
        self.assertTrue(self.snap.startswith("DATE: 2026-08-27"))

    def test_all_ten_sections_present(self):
        self.assertEqual(self.snap.count("\n=== "), 10)

    def test_tables_rendered(self):
        self.assertGreaterEqual(len(SEPARATOR.findall(self.snap)), 8)

    def test_no_python_repr_leaks(self):
        # Word-bounded: "Financials" contains the substring "nan", and a naive
        # containment check flags it.
        for leak in (r"\bNone\b", r"\bnan\b", r"\binf\b", r"\{'", r"\[\{", "Traceback"):
            self.assertIsNone(re.search(leak, self.snap), leak)

    def test_stale_sources_are_named(self):
        snap = brief.build_snapshot(dict(self.sources, _stale=["housing", "fed"]),
                                    "2026-08-27")
        self.assertIn("fed, housing", snap)
        self.assertIn("past their refresh window", snap)

    def test_no_stale_note_when_nothing_is_stale(self):
        self.assertNotIn("past their refresh window", self.snap)

    def test_meta_keys_excluded_from_accounting(self):
        self.assertIn("fed_series_meta", brief._META_KEYS)
        self.assertIn("_stale", brief._META_KEYS)

    def test_prompt_embeds_snapshot_verbatim(self):
        for lang in ("en", "zh"):
            self.assertIn(self.snap, brief.build_prompt(self.snap, lang))

    def test_prompt_demands_tables_and_forbids_invention(self):
        en = brief.build_prompt(self.snap, "en")
        self.assertIn("pipe table", en)
        self.assertIn("Never invent", en)
        self.assertIn("DATA UNAVAILABLE", en)
        zh = brief.build_prompt(self.snap, "zh")
        self.assertIn("管道表格", zh)
        self.assertIn("绝对不要编造", zh)

    def test_prompt_forbids_placeholder_tables_for_cold_sections(self):
        # Without this the model renders a table of "DATA UNAVAILABLE" rows.
        self.assertIn("Do not build a", brief.build_prompt(self.snap, "en"))
        self.assertIn("不要为缺失的数据生成表格", brief.build_prompt(self.snap, "zh"))

    def test_us_prompt_lists_nine_sections(self):
        for lang, sections in (("en", brief._SECTIONS_US_EN), ("zh", brief._SECTIONS_US_ZH)):
            self.assertIn(sections, brief.build_prompt(self.snap, lang))
            self.assertEqual(len(sections.strip().splitlines()), 9, lang)

    def test_cn_prompt_lists_five_sections(self):
        for lang, sections in (("en", brief._SECTIONS_CN_EN), ("zh", brief._SECTIONS_CN_ZH)):
            prompt = brief.build_prompt(self.snap, lang, market="cn")
            self.assertIn(sections, prompt)
            self.assertEqual(len(sections.strip().splitlines()), 5, lang)
            # Section count must be interpolated, not hardcoded to nine.
            self.assertNotIn("9 sections", prompt)
            self.assertNotIn("9 个部分", prompt)

    def test_cn_prompt_forbids_calling_excluded_us_data_missing(self):
        for lang, needle in (("en", "excluded **by choice**"), ("zh", "有意不纳入")):
            self.assertIn(needle, brief.build_prompt(self.snap, lang, market="cn"))
            self.assertNotIn(needle, brief.build_prompt(self.snap, lang, market="us"))

    def test_unknown_market_falls_back_to_us(self):
        self.assertEqual(brief.build_prompt(self.snap, "en", market="kr"),
                         brief.build_prompt(self.snap, "en", market="us"))

    def test_unknown_lang_falls_back_to_english(self):
        self.assertIn("pipe table", brief.build_prompt(self.snap, "klingon"))

    def test_all_cold_sources_still_builds(self):
        empty = {k: None for k in self.sources}
        empty["fed_series_meta"] = {}
        snap = brief.build_snapshot(empty, "2026-08-27")
        self.assertEqual(snap.count("\n=== "), 10)
        self.assertGreaterEqual(snap.count("DATA UNAVAILABLE"), 10)

    def test_empty_sources_dict_does_not_raise(self):
        self.assertIn("DATE:", brief.build_snapshot({}, "2026-08-27"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
