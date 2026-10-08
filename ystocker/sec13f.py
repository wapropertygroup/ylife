"""
ystocker.sec13f
~~~~~~~~~~~~~~~
Fetches 13F institutional holdings from SEC EDGAR for a fixed list of top funds.

Data flow per fund:
  1. GET data.sec.gov/submissions/CIK{cik}.json for every filer the fund maps
     to -- FUNDS, plus COFILERS and PREDECESSORS -- and tag each filing with
     the CIK that made it
  2. Route each period to the filer(s) that own it (_filers_for_period), and
     within each filer's period resolve its amendments by the type on their
     cover pages: a restatement replaces the report, NEW HOLDINGS add to it
  3. GET Archives/edgar/data/{cik}/{accession}-index.htm  → the raw XML named
     in its INFORMATION TABLE row (_pick_infotable_from_index)
  4. GET that XML, decide whether its <value> column is dollars or thousands
     (decide_value_unit), normalise to thousands, sum co-filers by CUSIP; total
     the options rows separately, for reported_value_millions
  5. Quarters past the newest five: the cover pages' tableValueTotal only
  6. Compare each quarter with the one before it for the change classification

All results are cached on disk (24h TTL) and in memory.
"""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import requests

from ystocker import fetchguard

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fund registry  {display_name: zero-padded CIK of the entity filing the 13F-HR}
#
# Nothing checks a CIK at run time, and a wrong one does not fail: it shows
# somebody else's portfolio under this name. Every entry was checked against
# EDGAR's entity name and latest 13F-HR on 2026-10-04, and 18 of the 51 then
# listed had to change. "Jane Street" was Barber Financial Group (last 13F-HR
# 2023-03-31), "Capital Group" was Royal Bank of Canada, "Coatue" was Pershing
# Square Capital Management, "Geode" was BlackRock's retired CIK, "Altimeter"
# was Marcato Capital (last 13F-HR 2019-12-31), "Pabrai" was Mohnish Pabrai's
# personal CIK (last 13F-HR a 2011 text filing), and ten more -- Balyasny,
# Brevan Howard, Marshall Wace, D1, Light Street (Fastly, Inc.), Hillhouse,
# Tudor, Susquehanna, Hudson Bay, and T. Rowe Price (the parent group, which
# files no 13F) -- named people or companies that never file one, which /13f
# showed as "No 13F-HR filings found". Greenlight and Vanguard were the right
# firms, but their 13F-HR now comes from other entities: see PREDECESSORS and
# COFILERS. When adding a fund, check the name EDGAR gives for the CIK.
# ---------------------------------------------------------------------------
FUNDS: Dict[str, str] = {
    # ─── Tier 1: Mega funds (household names, biggest AUM) ──────────────────
    "Berkshire Hathaway":       "0001067983",
    "Vanguard Group":           "0002100119",  # Vanguard Capital Management; + COFILERS, PREDECESSORS
    "BlackRock":                "0002012383",  # BlackRock, Inc. (BLK)
    "State Street":             "0000093751",
    "Fidelity (FMR)":           "0000315066",
    "T. Rowe Price":            "0000080255",  # T. Rowe Price Associates, Inc.
    # Capital Research Global Investors only. Capital Group files 13Fs through
    # three entities; Capital World Investors (0001422849) and Capital
    # International Investors (0001562230) are not included, so this is not
    # the whole of Capital Group.
    "Capital Group":            "0001422848",
    "Wellington Management":    "0000902219",
    "Northern Trust":           "0000073124",
    "Geode Capital":            "0001214717",  # Geode Capital Management, LLC

    # ─── Tier 2: Macro / multi-strategy hedge funds ─────────────────────────
    "Bridgewater Associates":   "0001350694",
    "Citadel Advisors":         "0001423053",
    "Millennium Management":    "0001273087",
    "Point72 Asset Management": "0001603466",
    "DE Shaw":                  "0001009207",
    "Balyasny Asset Mgmt":      "0001218710",  # Balyasny Asset Management L.P.
    "Brevan Howard":            "0001512857",  # Brevan Howard Capital Management LP
    "Marshall Wace":            "0001318757",  # Marshall Wace, LLP

    # ─── Tier 3: Tiger cubs & growth equity ──────────────────────────────────
    "Tiger Global":             "0001167483",
    "Coatue Management":        "0001135730",  # Coatue Management LLC
    "Viking Global":            "0001103804",
    "Lone Pine Capital":        "0001061165",
    "Maverick Capital":         "0000934639",
    "D1 Capital":               "0001747057",  # D1 Capital Partners L.P.
    "Light Street Capital":     "0001569049",  # Light Street Capital Management, LLC
    "Tiger Cub Hill House":     "0001762304",  # HHLR Advisors, Ltd. (Hillhouse)

    # ─── Tier 4: Value / activist investors ──────────────────────────────────
    "Third Point":              "0001040273",
    "Pershing Square":          "0002026053",  # Pershing Square Inc.; + PREDECESSORS
    "Baupost Group":            "0001061768",
    "Elliott Management":       "0001791786",  # Elliott Investment Management L.P.
    "Starboard Value":          "0001517137",
    "Icahn Capital":            "0000921669",  # filed by Carl C. Icahn himself
    "Trian Partners":           "0001345471",

    # ─── Tier 5: Famous individual investor / family office vehicles ────────
    "Soros Fund Management":    "0001029160",
    "Duquesne Family Office":   "0001536411",  # Stanley Druckenmiller
    "Appaloosa (Tepper)":       "0001656456",  # Appaloosa LP - David Tepper
    "Greenlight Capital (Einhorn)": "0001489933",  # DME Capital Management, LP; + PREDECESSORS
    "Pabrai Investment Funds":  "0001549575",  # Dalal Street, LLC; Mohnish Pabrai signs its 13F

    # ─── Tier 6: Growth / tech focus ────────────────────────────────────────
    "ARK Investment":           "0001697748",
    "Whale Rock Capital":       "0001387322",
    "Altimeter Capital":        "0001541617",  # Altimeter Capital Management, LP
    "Tudor Investment":         "0000923093",  # Tudor Investment Corp (Paul Tudor Jones)

    # ─── Tier 7: Quant / systematic ─────────────────────────────────────────
    "Renaissance Technologies": "0001037389",
    "Two Sigma Investments":    "0001179392",
    "AQR Capital":              "0001167557",
    "Susquehanna":              "0001446194",  # Susquehanna International Group, LLP
    "Jane Street":              "0001595888",  # Jane Street Group, LLC
    "Hudson Bay Capital":       "0001393825",  # Hudson Bay Capital Management LP
}

#: Funds whose 13F-HR moved to a new filer: {name: ((cik, last period it
#: filed for), ...)}. A period on or before a predecessor's cutover is taken
#: from that predecessor only -- even where the current filer also filed for
#: it -- and later periods from the current filer only. The two are different
#: books, not two copies of one: Pershing Square Inc. filed a one-position
#: 13F-HR for every quarter from 2025-06-30 to 2026-03-31 while Pershing Square
#: Capital Management filed the real portfolio, so taking either for the other's
#: quarters would mark the whole book "new" or "sold" with nothing traded.
PREDECESSORS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    # Vanguard Group Inc: last 13F-HR 2025-12-31, then 13F-NT notices naming
    # the managers in COFILERS as the ones that report for it.
    "Vanguard Group":               (("0000102909", "2025-12-31"),),
    # Pershing Square Capital Management: last 13F-HR 2026-03-31; its 13F-NT
    # for 2026-06-30 names Pershing Square Inc. (0002026053) as the reporter.
    "Pershing Square":              (("0001336528", "2026-03-31"),),
    # Greenlight Capital Inc: last 13F-HR 2023-12-31. DME Capital Management
    # filed a 13F-NT for that quarter and files the 13F-HR now (latest
    # 2026-06-30); the last Greenlight cover and the current DME one have the
    # same signer.
    "Greenlight Capital (Einhorn)": (("0001079114", "2023-12-31"),),
}

#: Funds that report through several filers at once, summed into one fund:
#: {name: extra CIKs filing beside FUNDS[name]}.
#:
#: Vanguard Group Inc's last 13F-HR (2025-12-31, $6,897.7B) was a combination
#: report that included seven smaller Vanguard managers. From 2026-03-31 it
#: files a 13F-NT instead, and every manager it names files its own 13F-HR:
#: the two index advisers that replaced it, and those same seven. Summing the
#: two advisers alone broke the series at the cutover -- 2026-03-31 read
#: $5,957.0B, down 13.6% on the quarter while BlackRock fell 3.3% and State
#: Street 2.8%. With all nine it is $6,728.4B (-2.5%), and 2026-06-30 is
#: $7,799.9B (+15.9%, against +17.6% and +16.4%). Vanguard Marketing Corp, also
#: on the 13F-NT, files one row worth under $0.1B and was never part of the
#: combination report, so it is left out. Totals below: 2026-06-30 covers.
COFILERS: Dict[str, Tuple[str, ...]] = {
    "Vanguard Group": (
        "0002100121",  # Vanguard Portfolio Management LLC   3,141 rows  $2,219.1B
        "0000933478",  # Vanguard Fiduciary Trust Co         4,076 rows    $454.4B
        "0001811242",  # Vanguard Global Advisers, LLC        3,638 rows    $214.6B
        "0001680208",  # Vanguard Asset Management, Ltd       2,447 rows    $148.0B
        "0001550100",  # Vanguard Investments Australia, Ltd. 2,370 rows     $40.2B
        "0000947529",  # Vanguard Advisers Inc                  715 rows     $29.4B
        "0001767306",  # Vanguard Personalized Indexing Mgmt  2,242 rows     $12.2B
        "0001984256",  # Vanguard National Trust Co             787 rows      $1.8B
    ),
}

#: Second names for a fund already in FUNDS. They used to be FUNDS entries of
#: their own, so each portfolio was fetched twice per refresh and counted twice
#: in /13f's consensus and net-buy tables -- one Berkshire portfolio read as
#: "held by 2 funds". Now they are only names: never fetched or counted, but a
#: slug for one still resolves to the canonical fund (resolve_fund_name).
ALIASES: Dict[str, str] = {
    "Buffett Family Office":       "Berkshire Hathaway",
    "Carl Icahn":                  "Icahn Capital",
    "Druckenmiller Family Office": "Duquesne Family Office",
}


def fund_slug(name: str) -> str:
    """The URL slug /13f and /api/13f/<slug> use for a display name."""
    return name.lower().replace(" ", "-")


def resolve_fund_name(slug: str) -> Optional[str]:
    """Return the FUNDS key for a slug or display name, following ALIASES.

    Case-insensitive; None when nothing matches. Pure, so routes can call it
    without touching the cache.
    """
    key = (slug or "").strip().lower()
    if not key:
        return None
    for name in FUNDS:
        if key in (fund_slug(name), name.lower()):
            return name
    for alias, canonical in ALIASES.items():
        if key in (fund_slug(alias), alias.lower()):
            return canonical
    return None

# ---------------------------------------------------------------------------
# Static CUSIP → ticker mapping for the most common large-cap holdings
# This avoids any on-the-fly resolution network call.
# ---------------------------------------------------------------------------
CUSIP_TO_TICKER: Dict[str, str] = {
    # ── Mega-cap tech ────────────────────────────────────────────────────────
    "037833100": "AAPL",
    "594918104": "MSFT",
    "023135106": "AMZN",
    "67066G104": "NVDA",
    "30303M102": "META",
    "02079K305": "GOOGL",   # Class A
    "02079K107": "GOOGL",   # Class A alt
    "38259P508": "GOOGL",   # Class C
    "40171V100": "GOOG",    # Class C alt
    "88160R101": "TSLA",
    "64110D104": "NET",
    "09857L108": "BKNG",    # Booking Holdings old CUSIP
    "833445109": "BKNG",    # Booking Holdings new CUSIP (NOT Snowflake)
    "20030N101": "COIN",
    "57667L107": "MSTR",
    "651639106": "NFLX",    # old CUSIP
    "64110L106": "NFLX",    # current CUSIP
    "156700106": "CRM",     # old CUSIP
    "79466L302": "CRM",     # current CUSIP (Salesforce Inc)
    "097693109": "ADBE",
    "00724F101": "ADBE",    # alternate
    "456788108": "INTU",
    "461202103": "INTU",    # alternate
    "11135F101": "AVGO",    # Broadcom
    # "67066G104" already defined above ───────────────────────────────────────────────────────────
    "46090E103": "QQQ",     # Invesco QQQ Trust (NOT JPM — JPM uses 46625H100)
    "46625H100": "JPM",
    "060505104": "BAC",     # correct 9-digit
    "60505104":  "BAC",     # some filers omit leading zero
    "166764100": "CVX",     # Chevron Corp (NOT Citigroup — Citi is 172967100)
    "949746101": "WFC",
    "38141G104": "GS",
    "404280406": "GS",      # alternate
    "617446448": "MS",
    "61945C103": "MS",      # alternate
    "025816109": "AXP",
    "811156100": "SCHW",
    "808513105": "SCHW",    # alternate
    "742556105": "PRU",
    "717081103": "PFG",
    "57636Q104": "MA",
    "57060D108": "MKTX",    # MarketAxess (NOT MA alternate)
    "92826C839": "V",
    "615369105": "MCO",     # Moody's
    "14040H105": "COF",     # Capital One
    "693475105": "PNC",     # PNC Financial (NOT PSA)
    "48251W104": "KKR",     # KKR & Co

    # ── Berkshire ────────────────────────────────────────────────────────────
    "172967424": "BRK-B",
    "172967304": "BRK-B",   # alternate
    "084670702": "BRK-B",   # current CUSIP
    "084670108": "BRK-B",   # alternate
    "110122108": "BMY",     # Bristol-Myers Squibb (NOT BRK-A)

    # ── Healthcare / Pharma ──────────────────────────────────────────────────
    "912093108": "UNH",
    "460690100": "JNJ",
    "58933Y105": "MRK",
    "002824100": "ABT",
    "002921109": "ABBV",
    "00287Y109": "ABBV",    # current CUSIP
    "339750101": "LLY",
    "532457108": "LLY",     # alternate
    "698435105": "PFE",
    "G0593M107": "AZN",     # AstraZeneca PLC (UK ADR)
    "023608102": "AEE",     # Ameren Corp (NOT AMGN)
    "031162100": "AMGN",    # Amgen correct CUSIP
    "06738G103": "BIIB",
    "74159L101": "REGN",
    "900111204": "VRTX",
    "60871R209": "MRNA",
    "375558103": "GILD",
    "101137107": "BSX",     # Boston Scientific
    "02043Q107": "ALNY",    # Alnylam
    "04016X101": "ARGX",    # argenx

    # ── Consumer ────────────────────────────────────────────────────────────
    "26441C204": "KO",
    "191216100": "KO",      # alternate
    "713448108": "PEP",
    "732834105": "PG",
    "931142103": "WMT",
    "437076102": "HD",
    "548661107": "LOW",
    "883948100": "TGT",
    "902494103": "TJX",
    "500754106": "KR",
    "501044101": "KR",      # alternate
    "84265V105": "SCCO",    # Southern Copper Corp (NOT SBUX — Starbucks is 855244108)
    "855244108": "SBUX",    # Starbucks Corp
    "580135101": "MCD",
    "655044105": "NKE",
    "49456B101": "KHC",
    "872540109": "TJX",     # TJX Companies (NOT TSN)
    "22160K105": "COST",
    "254687106": "DIS",
    "874054109": "TTWO",    # Take-Two

    # ── Industrials / Defense ────────────────────────────────────────────────
    "097023105": "BA",
    "742718109": "RTX",
    "742718":    "RTX",     # truncated
    "75513E101": "RTX",     # RTX Corporation new CUSIP
    "438516106": "HON",
    "478160104": "JCI",
    "369550108": "GE",      # old GE (pre-split)
    "369604301": "GEV",     # GE Vernova (post-split)
    "369604103": "GE",      # GE Aerospace (post-split)
    "36828A101": "GEV",     # GE Vernova alternate
    "526057104": "LEN",     # Lennar Corp Class A (NOT LMT)
    "526057302": "LEN",     # Lennar Corp Class B
    "539830109": "LMT",     # Lockheed Martin
    "631103108": "NOC",
    "149123101": "CAT",
    "91324P102": "UNH",     # UnitedHealth Group (NOT UPS)
    "31428X106": "FDX",
    "655844108": "NSC",     # Norfolk Southern
    "244199105": "DE",      # Deere & Co
    "34959J108": "FTV",     # Fortive Corp
    "363576109": "AJG",     # Arthur J Gallagher
    "049468101": "TEAM",    # Atlassian
    "125523100": "CI",      # The Cigna Group
    "235851102": "DHR",     # Danaher

    # ── Energy ──────────────────────────────────────────────────────────────
    "145220105": "CVX",
    "30231G102": "XOM",
    "202795101": "COP",
    "26875P101": "EOG",
    "742514509": "PSX",
    "718546104": "PSX",     # alternate
    "718172109": "PM",      # Philip Morris
    "670346105": "NUE",     # Nucor Corp (NOT OXY)
    "674599105": "OXY",     # Occidental Petroleum (correct CUSIP)
    "42809H107": "HES",     # Hess Corp
    "867914":    "SLB",
    "69331C108": "PCG",     # PG&E
    "867224107": "SU",      # Suncor Energy
    "453038408": "IMO",     # Imperial Oil
    "92840M102": "VST",     # Vistra Corp

    # ── Semiconductors ───────────────────────────────────────────────────────
    "458140100": "INTC",
    "009728109": "AMD",
    "007903107": "AMD",     # current CUSIP
    "595112103": "MU",
    "512807306": "LRCX",
    "038222105": "AMAT",
    "747525103": "QCOM",
    "573874104": "MRVL",    # Marvell
    "N6596X109": "NXPI",    # NXP Semiconductors
    "N07059210": "ASML",    # ASML
    "482480100": "KLAC",    # KLA Corp
    "55024U109": "LITE",    # Lumentum Holdings (optical/laser components)
    "98954M101": "ZG",      # Zillow Group Class C
    "98954M200": "Z",       # Zillow Group Class A
    "98980G102": "ZS",      # Zscaler
    "47215P106": "JD",      # JD.com

    # ── Telecom ──────────────────────────────────────────────────────────────
    "92343V104": "VZ",
    "00206R102": "T",
    "88339J105": "TMUS",
    "87264F100": "TTD",     # The Trade Desk Inc (correct CUSIP)
    "872590104": "TMUS",    # T-Mobile US (NOT TTD — The Trade Desk is 87264F100 / 872590104 conflict: use 87264F100 for TTD)

    # ── Utilities / Real Estate ──────────────────────────────────────────────
    "637640103": "NEE",
    "65339F101": "NEE",     # alternate
    "15135B101": "CEG",     # Constellation Energy
    "21037T109": "CEG",     # alternate
    "263534109": "ECL",
    "49446R109": "PSA",     # Public Storage correct CUSIP
    "78467J100": "SPG",

    # ── Materials / Mining ───────────────────────────────────────────────────
    "345370860": "F",       # Ford Motor Co (NOT FCX — Freeport is 35671D857)
    "643659105": "NEM",
    "36467W109": "GDX",

    # ── Other / Misc ────────────────────────────────────────────────────────
    "459200101": "IBM",
    "68389X105": "ORCL",
    "17275R102": "CSCO",
    "267475101": "EMR",
    "882508104": "TXN",     # Texas Instruments
    "H1467J104": "CB",      # Chubb
    "032095101": "APH",     # Amphenol
    "03831W108": "APP",     # AppLovin
    "040413205": "ANET",    # Arista Networks
    "69608A108": "PLTR",    # Palantir
    "82509L107": "SHOP",    # Shopify
    "780253109": "SHEL",    # Shell
    "780259305": "SHEL",    # Shell alternate
    "958102105": "WDC",     # Western Digital
    "80004C200": "SNDK",    # SanDisk (spun off from WDC)
    "G25508105": "CRH",     # CRH plc
    "92343E102": "VRSN",    # VeriSign
    "11271J107": "BN",      # Brookfield Corp
    "23918K108": "DVA",     # DaVita
    "81141R100": "SE",      # Sea Limited
    "37045V100": "GM",      # General Motors
    "90353T100": "UBER",    # Uber
    "829933100": "SIRI",    # Sirius XM
    "146869102": "CVNA",    # Carvana
    "771049103": "RBLX",    # Roblox
    "44267T102": "HHH",     # Howard Hughes
    "844741108": "LUV",     # Southwest Airlines
    "21036P108": "STZ",     # Constellation Brands
    "G54950103": "LIN",     # Linde plc
    "81762P102": "NOW",     # ServiceNow
    "G1151C101": "ACN",     # Accenture
    "743315103": "PGR",     # Progressive Corp
    "L8681T102": "SPOT",    # Spotify
    "571748102": "MMC",     # Marsh & McLennan
    "G8994E103": "TT",      # Trane Technologies
    "75886F107": "REGN",    # Regeneron (new CUSIP)
    # "872590104" → TTD (already in Telecom section above)
    "G3643J108": "FLUT",    # Flutter Entertainment
    "770700102": "HOOD",    # Robinhood
    "46438F101": "IBIT",    # iShares Bitcoin Trust ETF
    "76131D103": "QSR",     # Restaurant Brands Intl
    "04626A103": "ALAB",    # Astera Labs
    "171779309": "CIEN",    # Ciena
    "75734B100": "RDDT",    # Reddit
    "G0403H108": "AON",     # Aon plc
    "25809K105": "DASH",    # DoorDash
    "833445109": "BKNG",    # Booking Holdings new CUSIP (NOT Snowflake)
    "G6683N103": "NU",      # Nu Holdings
    "169656105": "CMG",     # Chipotle
    "824348106": "SHW",     # Sherwin-Williams
    "50212V100": "LPLA",    # LPL Financial
    "25754A201": "DPZ",     # Domino's Pizza
    "98980L101": "ZM",      # Zoom
    "009066101": "ABNB",    # Airbnb
    "03769M106": "APO",     # Apollo Global
    "49177J102": "KVUE",    # Kenvue
    "778296103": "ROST",    # Ross Stores
    "922475108": "VEEV",    # Veeva Systems
    "91307C102": "UTHR",    # United Therapeutics
    "83406F102": "SOFI",    # SoFi Technologies
    "15101Q207": "CLS",     # Celestica
    "02005N100": "ALLY",    # Ally Financial
    "422806208": "HEI",     # HEICO Corp
    "73278L105": "POOL",    # Pool Corp
    "546347105": "LPX",     # Louisiana-Pacific
    "16119P108": "CHTR",    # Charter Communications
    "512816109": "LAMR",    # Lamar Advertising
    "G0176J109": "ALLE",    # Allegion
    "62944T105": "NVR",     # NVR Inc
    "47233W109": "JEF",     # Jefferies Financial
    "25243Q205": "DEO",     # Diageo
    "G9001E102": "LILA",    # Liberty Latin America Class A
    "G9001E128": "LILAK",   # Liberty Latin America Class C
    "047726302": "BATRK",   # Atlanta Braves Holdings
    "44920010":  "IAC",
    "78410G104": "SBAC",    # SBA Communications (NOT SE)
    "74164M108": "BIDU",
    "01609W102": "BABA",
    "87936U109": "TME",
    "98421M106": "VIPS",
    "67020Y100": "NVS",
    "72352L106": "PINS",
    "80105N105": "SNAP",
    "883556102": "TMO",     # Thermo Fisher Scientific (NOT TWTR)
    "268648102": "EL",
    "78462F103": "SPY",     # S&P 500 ETF
    "78467X109": "DIA",     # DJIA ETF
    "891482102": "TD",
    "25470F104": "DKNG",
    "52736R102": "LVS",
    "064058100": "BAX",
    "855244109": "SQ",
    "009158106": "ADM",
    "895126505": "WBA",
    "78814P168": "MELI",
    "18915M107": "NET",     # Cloudflare (NOT CLOV)
    "67085R104": "OKTA",
    "584977":    "MMM",
    "03218560":  "AIG",
    "650135108": "NIO",
    "76657R106": "RIVN",
    "874039100": "TSM",
    "46120E602": "ISRG",
    # ── Ark / Innovation / Biotech / Growth ──────────────────────────────────
    "77543R102": "ROKU",
    "19260Q107": "COIN",    # Coinbase alternate CUSIP
    "H17182108": "CRSP",    # CRISPR Therapeutics
    "880770102": "TER",     # Teradyne
    "88023B103": "TEM",     # Tempus AI
    "07373V105": "BEAM",    # Beam Therapeutics
    "03945R102": "ACHR",    # Archer Aviation
    "50077B207": "KTOS",    # Kratos Defense
    "90184D100": "TWST",    # Twist Bioscience
    "852234103": "XYZ",     # Block Inc
    "88025U109": "TXG",     # 10x Genomics
    "452327109": "ILMN",    # Illumina
    "040919102": "ARKB",    # ARK Bitcoin ETF
    "632307104": "NTRA",    # Natera
    "92337F107": "VCYT",    # Veracyte
    "26142V105": "DKNG",    # DraftKings new CUSIP
    "773121108": "RKLB",    # Rocket Lab
    "75629V104": "RXRX",    # Recursion Pharma
    "056752108": "BIDU",    # Baidu new CUSIP
    "21873S108": "CRWV",    # CoreWeave
    "45826J105": "NTLA",    # Intellia Therapeutics
    "05605H100": "BWXT",    # BWX Technologies
    "69553P100": "PD",      # PagerDuty
    "502431109": "LHX",     # L3Harris
    "896239100": "TRMB",    # Trimble
    "81663L200": "WGS",     # GeneDx Holdings
    "40131M109": "GH",      # Guardant Health
    "888787108": "TOST",    # Toast
    "69404D108": "PACB",    # Pacific Biosciences
    "172573107": "CRCL",    # Circle Internet
    # ── More growth / tech ───────────────────────────────────────────────────
    "M6191J100": "FROG",    # JFrog
    "87305R109": "TTMI",    # TTM Technologies
    "816850101": "SMTC",    # Semtech
    "G3323L100": "FN",      # Fabrinet
    "60937P106": "MDB",     # MongoDB
    "82982T106": "SITM",    # SiTime
    "219350105": "GLW",     # Corning
    "19247G107": "COHR",    # Coherent Corp
    "58733R102": "MELI",    # MercadoLibre (new CUSIP)
    "453204109": "PI",      # Impinj
    "26603R106": "DUOL",    # Duolingo
    "55405Y100": "MTSI",    # MACOM Technology
    "093712107": "BE",      # Bloom Energy
    "49845K101": "KVYO",    # Klaviyo
    "443573100": "HUBS",    # HubSpot
    "42824C109": "HPE",     # Hewlett Packard Enterprise
    "679295105": "OKTA",    # Okta new CUSIP
    "530909308": "LLYVK",   # Liberty Live Holdings Class C
    "530909100": "LLYVA",   # Liberty Live Holdings Class A
    "531229755": "FWONA",   # Liberty Media (Formula One)
    "650111107": "NYT",     # New York Times

    # ── Missing from user data ───────────────────────────────────────────────
    # Industrials / Transport
    "907818108": "UNP",     # Union Pacific Corp
    "172967100": "C",       # Citigroup Inc
    "36164L108": "GDS",     # GDS Holdings Ltd
    "31488V107": "FERG",    # Ferguson Enterprises Inc
    "26969P108": "EXP",     # Eagle Materials Inc
    "372460105": "GPC",     # Genuine Parts Co
    "256677105": "DG",      # Dollar General Corp
    "337738108": "FI",      # Fiserv Inc (new ticker)
    "31620M106": "FIS",     # Fidelity National Information Services
    "95082P105": "WCC",     # WESCO International Inc
    "03064D108": "COLD",    # Americold Realty Trust

    # Healthcare
    "60855R100": "MOH",     # Molina Healthcare Inc
    "036752103": "ELV",     # Elevance Health (formerly Anthem)
    "281020107": "EIX",     # Edison International
    "445658107": "JBHT",    # J.B. Hunt Transport
    "172908105": "CTAS",    # Cintas Corp
    "620076307": "MSI",     # Motorola Solutions
    "086516101": "BBY",     # Best Buy
    "192446102": "CTSH",    # Cognizant Technology
    "194162103": "CL",      # Colgate-Palmolive
    "231021106": "CMI",     # Cummins Inc
    "67103H107": "ORLY",    # O'Reilly Automotive
    "30212P303": "EXPE",    # Expedia Group
    "199908104": "FIX",     # Comfort Systems USA Inc (NOT FWRD)

    # Finance / ETFs
    "464287200": "IVV",     # iShares Core S&P 500 ETF
    "464288513": "IJH",     # iShares Core S&P Mid-Cap ETF
    "912932100": "UNIT",    # Uniti Group

    # Mining / Commodities
    "89679M104": "TFPM",    # Triple Flag Precious Metals

    # International / ADRs
    "G96629103": "WTW",     # Willis Towers Watson
    "G61188127": "LBTYK",   # Liberty Global Class C
    "G61188101": "LBTYA",   # Liberty Global Class A
    "G4412G101": "HLF",     # Herbalife Ltd
    "40054J109": "AEROMEX", # Grupo Aeromexico (Mexican airline)
    "G7997W102": "SDRL",    # Seadrill Ltd
    "G8060N102": "ST",      # Sensata Technologies
    "36164V800": "GLIBA",   # GCI Liberty Inc
    "40415F101": "HDB",     # HDFC Bank ADR
    "302635206": "FSK",     # FS KKR Capital Corp
    "43300A203": "HLT",     # Hilton Worldwide Holdings
    "812215200": "SEG",     # Seaport Entertainment Group
    "42806J700": "HTZ",     # Hertz Global Holdings

    # Healthcare / Biotech
    "88033G407": "THC",     # Tenet Healthcare Corp
    "184496107": "CLH",     # Clean Harbors Inc
    "29362U104": "ENTG",    # Entegris Inc
    "144285103": "CRS",     # Carpenter Technology Corp
    "893641100": "TDG",     # TransDigm Group
    "974155103": "WING",    # Wingstop Inc
    "58507V107": "MEDS",    # Medline Industries (private — no ticker)
    "87422Q109": "TLN",     # Talen Energy Corp
    "929160109": "VMC",     # Vulcan Materials Co
    "00827B106": "AFRM",    # Affirm Holdings
    "68390D106": "OR",      # Osisko Gold Royalties
    "29444U700": "EQIX",    # Equinix Inc
    "22822V101": "CCI",     # Crown Castle Inc
    "29786A106": "ETSY",    # Etsy Inc
    "090043100": "BILL",    # Bill Holdings Inc
    "94419LAR2": "W",       # Wayfair Inc (note: unusual CUSIP format)
    "594972AJ0": "MSTR",    # Strategy Inc (MicroStrategy bonds)

    # ── More from user data (round 2) ───────────────────────────────────────
    # Industrials / Transport
    "576323109": "MTZ",     # MasTec Inc
    "77311W101": "RKT",     # Rocket Companies Inc
    "126408103": "CSX",     # CSX Corp
    "538034109": "LYV",     # Live Nation Entertainment
    "879433829": "TDS",     # Telephone & Data Systems
    "147528103": "CASY",    # Casey's General Stores
    "22160N109": "CSGP",    # CoStar Group Inc (NOT Costco — Costco is 22160K105)
    "00187Y100": "APG",     # API Group Corp

    # Tech / growth
    "G8068L108": "SN",      # SharkNinja Inc
    "88023U101": "SNBR",    # Somnigroup International (Sleep Number)
    "09073M104": "TECH",    # Bio-Techne Corp
    "36168Q104": "GFL",     # GFL Environmental Inc
    "61174X109": "MNST",    # Monster Beverage Corp
    "133131102": "CPT",     # Camden Property Trust
    "253393102": "DKS",     # Dick's Sporting Goods
    "844895102": "SWX",     # Southwest Gas Holdings
    "04010E109": "AGX",     # Argan Inc
    "20464U100": "COMP",    # Compass Inc
    "704551100": "BTU",     # Peabody Energy Corp
    "171757206": "CDTX",    # Cidara Therapeutics
    "565394103": "CART",    # Maplebear (Instacart)
    "N62509109": "NAMS",    # NewAmsterdam Pharma
    "92243G108": "PCVX",    # Vaxcyte Inc
    "23804L103": "DDOG",    # Datadog Inc
    "00534A102": "IVVD",    # Invivyd Inc
    "589889104": "MMSI",    # Merit Medical Systems
    "155923105": "CTRI",    # Centuri Holdings
    "152309100": "CNTA",    # Centessa Pharmaceuticals
    "22266T109": "CPNG",    # Coupang Inc
    "78781J109": "SAIL",    # SailPoint Inc
    "Y95308105": "WVE",     # Wave Life Sciences

    # Finance / Real Estate
    "092667104": "STRC",    # Strata Critical Medical / Sarcos Technology
    "42806J148": "HTZ",     # Hertz Global Holdings (warrant)

    # Misc
    "82835W108": "SPRY",    # ARS Pharmaceuticals
    "62548M209": "CTAV",    # Claritev Corporation
    "343928107": "FLYX",    # flyExclusive Inc
    "051774107": "AUR",     # Aurora Innovation Inc
    "051774115": "AUR",     # Aurora Innovation (warrant)
    "343928115": "FLYX",    # flyExclusive (warrant)
    "071734107": "BHC",     # Bausch Health Companies
    "M98068105": "WIX",     # Wix.com Ltd

    # ── Round 3 additions ────────────────────────────────────────────────────
    "87507T101": "TBN",     # Tamboran Resources Corp
    "G16910120": "BLSH",    # Bullish (crypto exchange)
    "008073108": "AVAV",    # AeroVironment Inc
    "947002101": "WFNT",    # Wealthfront Corp (private — no exchange ticker)
    "16935C109": "CHYM",    # Chime Financial Inc (private)
    "35671D857": "FCX",     # Freeport-McMoRan Inc (correct CUSIP)
    "87264F100": "TTD",     # The Trade Desk Inc (correct CUSIP, NOT T-Mobile)
    "G4124C109": "GRAB",    # Grab Holdings Ltd
    "19240Q201": "COGT",    # Cogent Biosciences Inc
    "518415104": "LSCC",    # Lattice Semiconductor Corp
    "68404L201": "OPCH",    # Option Care Health Inc
    "74623V103": "PCYO",    # PureCycle Technologies Inc
    "86384P109": "SH",      # StubHub Holdings Inc (private)
    "64119N608": "NTSK",    # Netskope Inc (private)
    "349381103": "FIG",     # Figure Technology Solutions (private)
    "G5279N105": "KLAR",    # Klarna Group plc (private)
    "G32089107": "ETOR",    # eToro Group Ltd
    "732908108": "PONY",    # Pony AI Inc
    "00138L108": "RERE",    # ATRenew Inc
    "G9572D103": "BULL",    # WeBull Corp
    "433313103": "HINGE",   # Hinge Health Inc (private)
    "98138H101": "WDAY",    # Workday Inc
    "74275K108": "PCOR",    # Procore Technologies Inc
    "N14506104": "ESTC",    # Elastic N.V.
    "G0896C103": "TBBB",    # BBB Foods Inc
    "74967X103": "RH",      # RH (Restoration Hardware)
    "929740108": "WAB",     # Wabtec Corp
    "66267T109": "NUAN",    # placeholder — Nordstrom?
    "256163106": "DOCU",    # DocuSign Inc
    "219948106": "CPAY",    # Corpay Inc
    "12503M108": "CBOE",    # Cboe Global Markets
    "58506Q109": "MEDP",    # Medpace Holdings
    "01973R101": "ALSN",    # Allison Transmission Holdings
    "26210C104": "DBX",     # Dropbox Inc
    "683712103": "OPEN",    # Opendoor Technologies
    "92532F100": "VRTX",    # Vertex Pharmaceuticals (alternate CUSIP)
    "85208M102": "SFM",     # Sprouts Farmers Market
    "859241101": "STRL",    # Sterling Infrastructure
    "76954A103": "RIVN",    # Rivian Automotive
    "64125C109": "NBIX",    # Neurocrine Biosciences
    "45337C102": "INCY",    # Incyte Corp
    "30161Q104": "EXEL",    # Exelixis Inc
    "496902404": "KGC",     # Kinross Gold Corp
    "351858105": "FNV",     # Franco-Nevada Corp
    "74624M102": "PSTG",    # Pure Storage
    "02376R102": "AAL",     # American Airlines
    "910047109": "UAL",     # United Airlines Holdings
    "247361702": "DAL",     # Delta Air Lines
    "682189105": "ON",      # ON Semiconductor
    "984245100": "YPF",     # YPF Sociedad Anonima
    "861012102": "STM",     # STMicroelectronics
    "91332U101": "U",       # Unity Software
    "234264109": "DAKT",    # Daktronics
    "74366E102": "PTGX",    # Protagonist Therapeutics
    "13645T100": "CAVA",    # CAVA Group
    "464286400": "IEMG",    # iShares Core MSCI Emerging Markets ETF
    "464287234": "IJR",     # iShares Core S&P Small-Cap ETF
    "81369Y605": "XLV",     # Select Sector SPDR Health Care ETF
    "46137V357": "IDEV",    # iShares Core MSCI International Developed Markets ETF
    "013872106": "AA",      # Alcoa Corp
    "185899101": "CLF",     # Cleveland-Cliffs
    "980745103": "WWD",     # Woodward Inc
    "881624209": "TEVA",    # Teva Pharmaceutical Industries
}

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_CACHE_FILE = Path(__file__).parent.parent / "cache" / "sec13f_cache.json"
_CACHE_TTL  = 24 * 60 * 60  # 24 h — 13F data changes quarterly
#: Written into the saved payload and required on load, so a deploy that
#: changes what the payload means refetches instead of serving the old one.
#: 2 (2026-10): values normalised to thousands per filing -- a version-1 file
#: holds every post-2023 figure 1000x too high -- 18 funds re-pointed at the
#: entities that actually file their 13F-HR, and the aliases dropped.
#: 3: amendments resolved by type (a version-2 file can hold a NEW HOLDINGS
#: amendment's few rows as a whole quarter), and reported_value_millions added.
#: 4 (2026-10-07): a version-3 file can hold 18 funds as "Could not fetch any
#: holdings", saved while SEC answered the short index URL 403 (see
#: _find_infotable_url). Without the bump the next fix would be a day away.
_CACHE_VER  = 4

_sec13f_lock: threading.Lock = threading.Lock()
_sec13f_data: Optional[Dict] = None
_sec13f_ts:   Optional[float] = None
_sec13f_warming: bool = False

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "yStocker/1.0 ystocker-app@example.com",
    "Accept-Encoding": "gzip, deflate",
})
_LAST_REQ_TIME: float = 0.0
_RATE_LIMIT_INTERVAL = 0.15   # seconds between requests
_rate_lock = threading.Lock()

#: Breaker identity for SEC EDGAR.
PROVIDER = "sec-edgar"
_SEC_TIMEOUT_SECONDS = fetchguard.env_float("SEC_TIMEOUT_SECONDS", 20.0, 1.0)

#: `_get_maybe` treats 503 as "not here" rather than "try again", so it must not
#: be retried or allowed to trip the breaker -- EDGAR serves it routinely for
#: funds with no filing at the requested path, and stalling the whole 13F
#: refresh on one of those would be wrong.
_MAYBE_RETRY_STATUS = frozenset({429, 500, 502, 504})


def _throttle() -> None:
    """Serialise outbound SEC requests to at most one per _RATE_LIMIT_INTERVAL.

    The lock is held across the sleep deliberately. Without it the
    read-modify-write on `_LAST_REQ_TIME` races: under the ThreadPoolExecutor in
    `refresh_cache()` several threads could read the same timestamp, each
    conclude no wait was needed, and fire together -- precisely the burst SEC's
    request-rate limit punishes.
    """
    global _LAST_REQ_TIME
    with _rate_lock:
        gap = time.time() - _LAST_REQ_TIME
        if gap < _RATE_LIMIT_INTERVAL:
            time.sleep(_RATE_LIMIT_INTERVAL - gap)
        _LAST_REQ_TIME = time.time()


def _get(url: str, **kwargs) -> requests.Response:
    """Rate-limited GET with retry + breaker. Raises on non-2xx (caller handles)."""
    _throttle()
    return fetchguard.request(
        PROVIDER, url, session=_SESSION, timeout=_SEC_TIMEOUT_SECONDS, **kwargs
    )


def edgar_get(url: str, **kwargs) -> requests.Response:
    """:func:`_get` for callers outside this module (``fundamentals.py``).

    SEC's rate limit is per client, not per module, so every EDGAR request in
    the process goes through the one throttle and the one breaker here rather
    than each caller pacing itself and the sum exceeding the limit.
    """
    return _get(url, **kwargs)


def _get_maybe(url: str, **kwargs) -> Optional[requests.Response]:
    """
    Rate-limited GET that returns None on 404/403/503 instead of raising.
    All other errors still raise.
    """
    _throttle()
    resp = fetchguard.request(
        PROVIDER,
        url,
        session=_SESSION,
        timeout=_SEC_TIMEOUT_SECONDS,
        retry_statuses=_MAYBE_RETRY_STATUS,
        raise_for_status=False,
        **kwargs,
    )
    if resp.status_code in (404, 403, 503):
        return None
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# CUSIP auto-resolver via OpenFIGI (free, no API key required)
# ---------------------------------------------------------------------------

#: OpenFIGI gets its own breaker. It is a different vendor with a much tighter
#: anonymous rate limit, and a CUSIP lookup being throttled must not stop the
#: 13F filings themselves from downloading.
FIGI_PROVIDER = "openfigi"
_FIGI_TIMEOUT_SECONDS = fetchguard.env_float("OPENFIGI_TIMEOUT_SECONDS", 10.0, 1.0)

_CUSIP_CACHE_FILE = Path(__file__).parent.parent / "cache" / "cusip_cache.json"
_cusip_cache: Optional[dict] = None
_cusip_cache_lock = threading.Lock()
# Track CUSIPs we've already tried but couldn't resolve, so we don't re-query
_cusip_unresolved: set = set()


def _load_cusip_cache() -> dict:
    """Load the persistent CUSIP→ticker cache from disk (one-time per process)."""
    global _cusip_cache
    if _cusip_cache is not None:
        return _cusip_cache
    try:
        if _CUSIP_CACHE_FILE.exists():
            _cusip_cache = json.loads(_CUSIP_CACHE_FILE.read_text())
        else:
            _cusip_cache = {}
    except Exception:
        _cusip_cache = {}
    return _cusip_cache


def _save_cusip_cache() -> None:
    """Persist the CUSIP cache to disk atomically."""
    if _cusip_cache is None:
        return
    try:
        _CUSIP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CUSIP_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cusip_cache, sort_keys=True, indent=0))
        tmp.replace(_CUSIP_CACHE_FILE)
    except Exception as exc:
        log.debug("CUSIP cache save failed: %s", exc)


def _resolve_cusip_to_ticker(cusip: str) -> Optional[str]:
    """Resolve an unknown CUSIP to a ticker via OpenFIGI.

    OpenFIGI is Bloomberg's free open data service — no API key required for
    light usage (≤25 requests / 6s window).  We cache successes (and known
    failures, in-process only) so each CUSIP is queried at most once.
    """
    if not cusip or len(cusip) != 9:
        return None

    cache = _load_cusip_cache()
    with _cusip_cache_lock:
        if cusip in cache:
            return cache[cusip] or None
        if cusip in _cusip_unresolved:
            return None  # already failed this run, don't retry

    # Query OpenFIGI
    try:
        resp = fetchguard.request(
            FIGI_PROVIDER,
            "https://api.openfigi.com/v3/mapping",
            session=_SESSION,
            method="POST",
            json=[{"idType": "ID_CUSIP", "idValue": cusip}],
            headers={"Content-Type": "application/json"},
            timeout=_FIGI_TIMEOUT_SECONDS,
            raise_for_status=False,
        )
        if resp.status_code == 429:
            log.warning("OpenFIGI rate-limited; will retry CUSIP %s later", cusip)
            return None  # don't cache — try again next run
        if resp.status_code != 200:
            log.debug("OpenFIGI HTTP %d for %s", resp.status_code, cusip)
            with _cusip_cache_lock:
                _cusip_unresolved.add(cusip)
            return None

        data = resp.json()
        if not data or not isinstance(data, list):
            with _cusip_cache_lock:
                _cusip_unresolved.add(cusip)
            return None

        first = data[0]
        if "data" not in first or not first["data"]:
            # No match — cache the negative so we don't re-query
            with _cusip_cache_lock:
                cache[cusip] = ""
                _cusip_unresolved.add(cusip)
            _save_cusip_cache()
            return None

        # Pick the US-listed common stock ticker if available
        ticker = None
        for entry in first["data"]:
            t = entry.get("ticker", "")
            sec_type = entry.get("securityType2", "") or entry.get("securityType", "")
            ex_code = entry.get("exchCode", "")
            if t and ex_code in ("US", "UN", "UQ", "UF", "UA", "UR", "UV", "UD", "UW", "UP"):
                # US exchange — prefer Common Stock / ADR / ETP
                if sec_type in ("Common Stock", "Depositary Receipt", "ADR", "ETP", ""):
                    ticker = t.upper()
                    break

        # Fallback: just use the first entry's ticker
        if not ticker and first["data"][0].get("ticker"):
            ticker = first["data"][0]["ticker"].upper()

        with _cusip_cache_lock:
            cache[cusip] = ticker or ""
        _save_cusip_cache()

        if ticker:
            log.info("OpenFIGI resolved CUSIP %s → %s", cusip, ticker)
        else:
            with _cusip_cache_lock:
                _cusip_unresolved.add(cusip)
        return ticker

    except Exception as exc:
        log.debug("OpenFIGI resolve failed for %s: %s", cusip, exc)
        with _cusip_cache_lock:
            _cusip_unresolved.add(cusip)
        return None


# ---------------------------------------------------------------------------
# SEC EDGAR parsing helpers
# ---------------------------------------------------------------------------

def _get_filings_list(cik: str) -> list:
    """Return list of recent filings dicts from SEC submissions endpoint.

    Each dict carries the ``cik`` it was listed under: a fund can span several
    filers (COFILERS, PREDECESSORS), and every document URL must be built from
    the filer that made that filing, not from the fund's primary CIK.

    The SEC API paginates older filings into separate JSON files listed under
    filings.files.  For funds like Vanguard/BlackRock that file thousands of
    forms, the 'recent' window may only contain the single latest 13F.  We
    fetch the first extra page as well so we always have at least two
    quarterly 13F-HR filings available for change detection.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _get(url).json()
    recent = data.get("filings", {}).get("recent", {})

    def _extract(block: dict) -> list:
        forms       = block.get("form", [])
        accessions  = block.get("accessionNumber", [])
        dates       = block.get("filingDate", [])
        periods     = block.get("reportDate", [])
        prim_docs   = block.get("primaryDocument", [""] * len(forms))
        return [
            {"cik": cik, "form": forms[i], "accession": accessions[i],
             "filing_date": dates[i], "period": periods[i],
             "primary_doc": prim_docs[i] if i < len(prim_docs) else ""}
            for i in range(len(forms))
        ]

    filings = _extract(recent)

    # Check whether the recent window already contains at least two distinct
    # 13F-HR periods.  If not, fetch the first pagination file so we have
    # enough history for quarter-over-quarter change detection.
    periods_in_recent = {
        f["period"] for f in filings if f["form"] in ("13F-HR", "13F-HR/A")
    }
    if len(periods_in_recent) < 16:
        extra_files = data.get("filings", {}).get("files", [])
        for extra in extra_files[:12]:         # fetch up to 12 extra pages for ≥4 years history
            extra_name = extra.get("name", "")
            if not extra_name:
                continue
            extra_url = f"https://data.sec.gov/submissions/{extra_name}"
            try:
                extra_data = _get(extra_url).json()
                extra_filings = _extract(extra_data)
                filings.extend(extra_filings)
                # Stop once we have ≥16 distinct 13F periods (~4 years)
                periods_so_far = {
                    f["period"] for f in filings if f["form"] in ("13F-HR", "13F-HR/A")
                }
                if len(periods_so_far) >= 16:
                    break
            except Exception as exc:
                log.debug("Could not fetch extra filings page %s: %s", extra_name, exc)

    return filings


# ---------------------------------------------------------------------------
# Value units
# ---------------------------------------------------------------------------

#: The SEC's 2022 Form 13F amendments moved <value> (and the cover page's
#: tableValueTotal) from thousands of dollars to whole dollars for filings made
#: on or after this date. This module read every filing as thousands until
#: 2026-10, so every post-2023 figure was 1000x too high: Berkshire's
#: 2026-06-30 AAPL line -- 65,950,296,923 for 227,917,808 shares, i.e. $289.36
#: a share, exactly that day's close -- was cached as $65.95 trillion.
DOLLAR_VALUES_FROM = "2023-01-03"

#: The date alone is not enough, because not every filer complied: T. Rowe
#: Price Associates, Baupost and Duquesne all still filed thousands for
#: 2026-06-30 (AAPL at 0.2894 a share, AMZN at 0.2383, Natera at 0.2715). So
#: the filing's own numbers get a vote. A median value per share under $1 can
#: only be thousands; one whose thousands reading would put the median share
#: over $50,000 can only be dollars. In between, the filing date decides.
_THOUSANDS_IF_MEDIAN_BELOW = 1.0
_DOLLARS_IF_THOUSANDS_MEDIAN_ABOVE = 50_000.0


def _default_value_unit(filing_date: str) -> str:
    """The unit Form 13F prescribed for a filing made on *filing_date*."""
    return "dollars" if (filing_date or "") >= DOLLAR_VALUES_FROM else "thousands"


def _median_value_per_share(rows: List[dict]) -> Optional[float]:
    """Median value / shares over share (SH) rows, or None when there are none.

    Principal-amount rows are skipped -- their count is face value, not shares --
    and so are zero values and zero counts, which say nothing about the unit.
    """
    ratios = [
        r["value"] / r["shares"]
        for r in rows
        if (r.get("share_type") or "SH").upper() == "SH"
        and (r.get("shares") or 0) > 0
        and (r.get("value") or 0) > 0
    ]
    return statistics.median(ratios) if ratios else None


def decide_value_unit(rows: List[dict], filing_date: str) -> str:
    """Return "dollars" or "thousands" for one filing's <value> column. Pure.

    *rows* are raw information-table rows: ``value`` as filed, ``shares`` and
    ``share_type``. The filing date sets the default; an unambiguous median
    value per share overrides it either way.
    """
    default = _default_value_unit(filing_date)
    median = _median_value_per_share(rows)
    if median is None:
        return default
    if median < _THOUSANDS_IF_MEDIAN_BELOW:
        return "thousands"
    if median * 1000 > _DOLLARS_IF_THOUSANDS_MEDIAN_ABOVE:
        return "dollars"
    return default


def _to_thousands(value: float, unit: str) -> float:
    """Express a <value> or tableValueTotal figure in thousands of dollars."""
    return value / 1000 if unit == "dollars" else value


def _cover_field(xml_text: str, tag: str) -> Optional[str]:
    """One scalar element of a 13F cover page, as written, or None."""
    m = re.search(rf"<(?:[\w.-]+:)?{tag}\b[^>]*>\s*([^<]*?)\s*</(?:[\w.-]+:)?{tag}\s*>",
                  xml_text or "", re.I)
    return m.group(1) if m else None


def _parse_cover(xml_text: str) -> dict:
    """A 13F cover page's summary fields, exactly as filed. Pure.

    Read by pattern rather than by XML parser: these are five scalars, and
    filers write them both with and without a namespace prefix. tableValueTotal
    is in the filing's own unit (see decide_value_unit) and includes options
    rows at their underlying value; amendmentType is RESTATEMENT or NEW
    HOLDINGS on a 13F-HR/A and absent on an original.
    """
    def number(tag: str) -> Optional[float]:
        raw = _cover_field(xml_text, tag)
        try:
            return float(raw.replace(",", "")) if raw else None
        except ValueError:
            return None

    amendment_type = _cover_field(xml_text, "amendmentType")
    entries = number("tableEntryTotal")
    return {
        "table_value_total": number("tableValueTotal"),
        "entries":           int(entries) if entries is not None else None,
        "amendment_type":    " ".join(amendment_type.split()).upper() if amendment_type else None,
        "is_amendment":      (_cover_field(xml_text, "isAmendment") or "").lower() == "true",
        "report_type":       _cover_field(xml_text, "reportType"),
    }


def _read_cover(cik: str, accession: str, primary_doc: str = "") -> Optional[dict]:
    """One request: a filing's cover page, parsed (_parse_cover), or None.

    *cik* must be the filer of this accession.
    """
    cik_int    = str(int(cik))
    acc_nodash = accession.replace("-", "")
    if primary_doc.lower().endswith(".txt"):
        return None  # pre-2013 text filing: no XML cover page exists
    # The submissions JSON names the rendered view (xslForm13F_X02/primary_doc.xml);
    # the raw cover is the same file name directly in the accession folder.
    cover_name = primary_doc.split("/")[-1] if primary_doc.lower().endswith(".xml") else "primary_doc.xml"
    cover_url  = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{cover_name}"
    try:
        resp = _get_maybe(cover_url)
        if not resp:
            return None
        if _is_html(resp.text, resp.headers.get("content-type", "")):
            log.debug("13F cover %s is HTML, not XML", cover_url)
            return None
        return _parse_cover(resp.text)
    except Exception as exc:
        log.debug("Could not read cover page for %s/%s: %s", cik_int, acc_nodash, exc)
        return None


def _cover_millions(cover: Optional[dict], filing_date: str, unit: Optional[str] = None) -> Optional[float]:
    """A cover's tableValueTotal in millions USD: in *unit* if known, else the date's default."""
    if not cover or cover.get("table_value_total") is None:
        return None
    return _to_thousands(cover["table_value_total"], unit or _default_value_unit(filing_date)) / 1000


def _get_aum_from_cover(cik: str, accession: str, filing_date: str = "",
                        unit: Optional[str] = None, primary_doc: str = "") -> Optional[float]:
    """
    Extract total portfolio value (AUM) from a 13F-HR cover page.
    Returns value in millions USD, or None if unavailable.

    The cover page XML (primary_doc.xml) carries <tableValueTotal>, in the same
    unit as the filing's information table. *unit* is that filing's decided
    unit when the caller has one (see fetch_fund_holdings); otherwise the
    default for *filing_date* applies. Much faster than the full infotable for
    historical quarters. *cik* must be the filer of this accession.
    """
    total = _cover_millions(_read_cover(cik, accession, primary_doc), filing_date, unit)
    return round(total, 1) if total is not None else None


class NotAnInfoTable(ValueError):
    """A URL that should have served information-table XML served something else."""


def _is_html(text: str, content_type: str = "") -> bool:
    """True for an HTML page -- a filing's XSL-rendered view, or an error page."""
    if "text/html" in (content_type or "").lower():
        return True
    head = (text or "")[:512].lstrip("\ufeff \t\r\n").lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _check_not_html(text: str, content_type: str, url: str) -> None:
    """Raise NotAnInfoTable rather than hand an HTML page to the XML parser.

    Parsing one fails with ``mismatched tag: line 33, column 2`` -- the rendered
    view's ``</head>`` -- which is all the logs said, every refresh, for each
    2018-2022 filing of the CIKs then listed as "Jane Street" and "Altimeter".
    """
    if _is_html(text, content_type):
        raise NotAnInfoTable(
            f"{url} served HTML ({content_type or 'no content-type'}), not "
            f"information-table XML -- an XSL-rendered view or an error page"
        )


_INDEX_TABLE_RE = re.compile(r'<table[^>]*class="tableFile"[^>]*>(.*?)</table>', re.S | re.I)
_INDEX_ROW_RE   = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_INDEX_CELL_RE  = re.compile(r"<t([dh])[^>]*>(.*?)</t[dh]>", re.S | re.I)
_HREF_RE        = re.compile(r'href="([^"]+)"', re.I)
_TAG_RE         = re.compile(r"<[^>]+>")
#: A document directly inside an accession folder. Every XSL-rendered view sits
#: one level down (xslForm13F_X01/, xslForm13F_X02/), so this excludes them all
#: without having to know which stylesheet versions exist.
_DIRECT_DOC_RE  = re.compile(r"^/Archives/edgar/data/\d+/\d{18}/[^/]+$", re.I)
_XSL_DIR_RE     = re.compile(r"/xsl[^/]*/", re.I)


def _pick_infotable_from_index(index_html: str) -> Optional[str]:
    """The raw information-table XML named by a filing's -index.htm, or None. Pure.

    The index lists each XML document of a 13F twice: as the XSL-rendered view
    under ``xslForm13F_X0n/`` (no size shown) and as the raw file. Take the row
    whose Type is INFORMATION TABLE, and of its links the one directly in the
    accession folder. Going by the Type column rather than the file name is
    what makes this indifferent to what a filer calls the file -- seen:
    ``Form13F.xml``, ``57127.xml``, ``q2-2026-13f-hr.xml``, ``file.XML``.
    """
    for table in _INDEX_TABLE_RE.findall(index_html or ""):
        type_col = 3  # Seq | Description | Document | Type | Size
        for row in _INDEX_ROW_RE.findall(table):
            cells = _INDEX_CELL_RE.findall(row)
            texts = [re.sub(r"\s+", " ", _TAG_RE.sub("", body)).strip() for _, body in cells]
            if cells and all(kind.lower() == "h" for kind, _ in cells):
                lowered = [t.lower() for t in texts]
                if "type" in lowered:
                    type_col = lowered.index("type")
                continue
            if len(texts) <= type_col or texts[type_col].upper() != "INFORMATION TABLE":
                continue
            for href in _HREF_RE.findall(row):
                path = href.split("?", 1)[0]
                if _DIRECT_DOC_RE.match(path) and path.lower().endswith(".xml"):
                    return "https://www.sec.gov" + path
    return None


def _fallback_infotable_link(index_html: str, cik_int: str, acc_nodash: str) -> Optional[str]:
    """The older link heuristic, for an index page without the usual table. Pure.

    Any .xml link that is neither an XSL-rendered view nor the cover page,
    preferring one with "infotable" in its name. Its view filter used to be the
    literal ``"xslForm13F_X02/"``, which let ``xslForm13F_X01/`` through -- the
    stylesheet on the 2018-2022 filings seen -- so for those it picked the
    rendered HTML.
    """
    xml_links = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', index_html or "", re.IGNORECASE)
    if not xml_links:
        rel_links = re.findall(r'href="([^"]+\.xml)"', index_html or "", re.IGNORECASE)
        xml_links = [
            f if f.startswith("/") else f"/Archives/edgar/data/{cik_int}/{acc_nodash}/{f}"
            for f in rel_links
        ]
    # primary_doc.xml at root level is the cover/header XML (edgarSubmission),
    # not the infotable.
    raw_links = [
        p for p in xml_links
        if not _XSL_DIR_RE.search(p) and p.split("/")[-1].lower() != "primary_doc.xml"
    ]
    for path in raw_links:
        fname = path.split("/")[-1].lower()
        if "infotable" in fname or "info_table" in fname:
            return "https://www.sec.gov" + path
    return "https://www.sec.gov" + raw_links[0] if raw_links else None


def _find_infotable_url(cik: str, accession: str, primary_doc: str = "") -> Optional[str]:
    """
    Return the URL of the raw infotable XML for a given 13F-HR filing, or None.

    *cik* must be the CIK of the entity that made this filing -- a fund with
    predecessors or co-filers spans several -- since its documents live there.

    1. A primary document ending in .txt is a pre-2013 text filing, which has no
       XML information table at all. The filename guesses below used to run for
       those anyway: ten requests a filing, every refresh, for five filings
       under the CIK then listed as Pabrai.
    2. The -index.htm page: the INFORMATION TABLE row, then the link heuristic.
    3. Only when no index page could be read at all: common filenames.

    A data.sec.gov ``…-index.json`` used to be tried first. It answered 404 for
    every filing tested in 2026-10, so all it did was cost a request.

    The index page is asked for inside the accession's folder first. The short
    form, ``…/data/<cik>/<accession>-index.htm`` with no folder, was the first
    try until 2026-10-07, when SEC was found answering it 403 while the folder
    path answered 200. Nothing failed loudly: every filing fell through to the
    filename guesses, which find ``infotable.xml`` and miss a file SEC names by
    number. Berkshire's is ``56757.xml``, so 18 of the 48 funds were saved as
    "Could not fetch any holdings" while the other 30 looked fine.
    """
    cik_int    = str(int(cik))
    acc_nodash = accession.replace("-", "")
    # SEC index filenames use the dashed accession number, e.g. 0000950123-25-002701-index.htm
    # The directory uses no dashes, e.g. 000095012325002701/
    acc_dashed = f"{acc_nodash[:10]}-{acc_nodash[10:12]}-{acc_nodash[12:]}"
    doc_base   = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"

    if primary_doc.lower().endswith(".txt"):
        log.info("13F %s/%s is a pre-XML text filing (%s): no information table to fetch",
                 cik_int, acc_nodash, primary_doc)
        return None

    for htm_url in [
        f"{doc_base}/{acc_dashed}-index.htm",
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_dashed}-index.htm",
        f"https://data.sec.gov/Archives/edgar/data/{cik_int}/{acc_dashed}-index.htm",
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}-index.htm",
    ]:
        r2 = _get_maybe(htm_url)
        if r2 is None:
            continue
        url, how = _pick_infotable_from_index(r2.text), "INFORMATION TABLE row"
        if not url:
            url, how = _fallback_infotable_link(r2.text, cik_int, acc_nodash), "link heuristic"
        if url:
            log.info("13F infotable %s/%s → %s (%s)", cik_int, acc_nodash, url.rsplit("/", 1)[-1], how)
            return url
        # The index lists every document; guessing names it does not list
        # cannot find anything.
        log.warning("Could not find infotable for CIK %s accession %s: %s lists none",
                    cik_int, acc_nodash, htm_url)
        return None

    # ── No index page could be read: try common filename patterns directly ──
    primary_stem = primary_doc.split("/")[-1].rsplit(".", 1)[0] if primary_doc else ""
    candidates = [
        "infotable.xml",
        "information_table.xml",
        "13finfotable.xml",
        "form13fInfoTable.xml",
        "informationtable.xml",
        "InfoTable.xml",
        "13F_InfoTable.xml",
    ]
    if primary_stem:
        candidates = [
            f"{primary_stem}_infotable.xml",
            f"{primary_stem}_info_table.xml",
            f"{primary_stem}infotable.xml",
        ] + candidates
    for fname in candidates:
        r3 = _get_maybe(f"{doc_base}/{fname}")
        if r3 is not None and r3.text.strip() and not _is_html(r3.text, r3.headers.get("content-type", "")):
            log.debug("Found infotable via direct guess: %s/%s", acc_nodash, fname)
            return f"{doc_base}/{fname}"

    log.warning("Could not find infotable for CIK %s accession %s", cik_int, acc_nodash)
    return None


def _parse_infotable_rows(xml_text: str) -> List[dict]:
    """Raw information-table rows, ``value`` exactly as filed.

    Its unit is not known here: see decide_value_unit. ``share_type`` is the
    row's sshPrnamtType (SH for shares, PRN for a principal amount).

    Options rows are kept, with ``put_call`` set: they are not holdings, and the
    holdings table leaves them out, but they are part of what the filer
    reported and of the cover page's tableValueTotal (see _ParsedTable). Their
    tickers are not resolved -- that would spend OpenFIGI lookups on rows
    nothing displays.
    """
    root = ET.fromstring(xml_text)
    ns_prefix = ""
    # Detect namespace from root tag
    if root.tag.startswith("{"):
        ns_uri = root.tag.split("}")[0].lstrip("{")
        ns_prefix = f"{{{ns_uri}}}"

    # Log root tag and first child to diagnose namespace/structure issues
    first_child = next(iter(root), None)
    log.info("13F XML root=%s ns=%r first_child=%s",
             root.tag, ns_prefix, first_child.tag if first_child is not None else None)

    rows: List[dict] = []
    for entry in root.iter(f"{ns_prefix}infoTable"):
        def _t(tag: str, parent=entry) -> Optional[str]:
            el = parent.find(f"{ns_prefix}{tag}")
            return el.text.strip() if el is not None and el.text else None

        put_call = _t("putCall")

        try:
            value = int(_t("value") or "0")
            shares_el = entry.find(f"{ns_prefix}shrsOrPrnAmt")
            shares = int(shares_el.find(f"{ns_prefix}sshPrnamt").text) if shares_el is not None else 0
            share_type = (_t("sshPrnamtType", shares_el) or "SH") if shares_el is not None else "SH"
        except (ValueError, AttributeError):
            continue

        cusip = (_t("cusip") or "").strip()
        name  = (_t("nameOfIssuer") or "").strip()
        ticker = None
        if not put_call:
            ticker = CUSIP_TO_TICKER.get(cusip)
            # Auto-resolve unknown CUSIPs via OpenFIGI (cached to disk)
            if not ticker and cusip:
                ticker = _resolve_cusip_to_ticker(cusip)

        rows.append({
            "cusip":      cusip,
            "name":       name,
            "ticker":     ticker,
            "shares":     shares,
            "share_type": share_type.upper(),
            "value":      value,
            "put_call":   put_call,
        })

    log.info("13F _parse_infotable: found %d raw rows (%d options)",
             len(rows), sum(1 for r in rows if r["put_call"]))
    return rows


def _normalise_values(rows: List[dict], unit: str) -> List[dict]:
    """Holding dicts with value_thousands / value_millions, whatever *unit* was filed."""
    out: List[dict] = []
    for r in rows:
        value_k = _to_thousands(r["value"], unit)
        out.append({
            "cusip":           r["cusip"],
            "name":            r["name"],
            "ticker":          r["ticker"],
            "shares":          r["shares"],
            "value_thousands": value_k,
            "value_millions":  round(value_k / 1000, 1),
        })
    return out


def _aggregate_by_cusip(holdings: List[dict]) -> List[dict]:
    """Sum holdings that share a CUSIP into one row; rows without one stay as they are.

    One security is often filed on several rows (per sub-adviser or "other
    manager": Berkshire files AAPL on twelve), and co-filers (COFILERS) each
    file their own -- both are summed here, so change detection and portfolio
    weights see one position. The inputs are not mutated.
    """
    seen: dict = {}
    merged: List[dict] = []
    for h in holdings:
        cusip = h["cusip"]
        if cusip and cusip in seen:
            existing = seen[cusip]
            existing["shares"] += h["shares"]
            existing["value_thousands"] += h["value_thousands"]
            existing["value_millions"] = round(existing["value_thousands"] / 1000, 1)
        else:
            entry = dict(h)
            if cusip:
                seen[cusip] = entry
            merged.append(entry)
    return merged


class _ParsedTable(NamedTuple):
    """One information table, every value in thousands of dollars."""
    holdings: List[dict]       # options rows excluded, aggregated by CUSIP
    unit: str                  # the unit the filing reported <value> in
    #: The put/call rows, at their underlying's value. Holdings leave them out
    #: and the cover's tableValueTotal does not -- for Jane Street Group's
    #: 2025-06-30 filing that is $62.0B of positions against $505.6B reported,
    #: $443.7B of it options -- so a series that mixed the two jumped wherever
    #: it changed from parsed quarters to cover-only ones.
    options_thousands: float

    @property
    def reported_thousands(self) -> float:
        """Positions plus options: what the filer reported, equal to the cover total."""
        return sum(h["value_thousands"] for h in self.holdings) + self.options_thousands


def _parse_infotable_with_unit(xml_text: str, filing_date: str = "",
                               label: str = "") -> _ParsedTable:
    """Parse an information table: holdings and options in thousands, and the unit filed."""
    rows = _parse_infotable_rows(xml_text)
    positions = [r for r in rows if not r["put_call"]]
    # The unit is decided on the positions. An options row's value and share
    # count are both its underlying's, but a filer counting contracts instead
    # of shares would move the median a hundredfold.
    evidence = positions or rows
    unit = decide_value_unit(evidence, filing_date)
    if unit != _default_value_unit(filing_date):
        log.info("13F %s: <value> is in %s although filed %s (median value/share %.4f)",
                 label or "infotable", unit, filing_date or "?", _median_value_per_share(evidence) or 0.0)
    merged = _aggregate_by_cusip(_normalise_values(positions, unit))
    options_thousands = sum(_to_thousands(r["value"], unit) for r in rows if r["put_call"])
    log.info("13F _parse_infotable: %d holdings after dedup", len(merged))
    return _ParsedTable(merged, unit, options_thousands)


def _parse_infotable(xml_text: str, filing_date: str = "") -> List[dict]:
    """Parse SEC 13F infotable XML and return list of holding dicts.

    Values come back in thousands whichever unit the filing used (see
    decide_value_unit), options rows are left out, and holdings with the same
    CUSIP are aggregated into a single row so change detection and portfolio
    weights are accurate.
    """
    return _parse_infotable_with_unit(xml_text, filing_date).holdings


def _annotate_changes(curr: List[dict], prev: List[dict]) -> List[dict]:
    """Add 'change' and 'change_pct' fields to each holding by comparing with previous quarter.

    Compares first by CUSIP, then falls back to resolved ticker symbol.
    Guards against implausible swings caused by share-count unit mismatches.
    """
    # Build previous-quarter lookup by CUSIP and by ticker
    prev_shares_by_cusip: dict = {}
    prev_shares_by_ticker: dict = {}
    for h in prev:
        cusip = h.get("cusip", "")
        if cusip:
            prev_shares_by_cusip[cusip] = prev_shares_by_cusip.get(cusip, 0) + h["shares"]
        ticker = h.get("ticker")
        if ticker:
            prev_shares_by_ticker[ticker] = prev_shares_by_ticker.get(ticker, 0) + h["shares"]

    for h in curr:
        cusip  = h.get("cusip", "")
        ticker = h.get("ticker")
        curr_shares = h["shares"]

        # Prefer CUSIP match; fall back to ticker match
        if cusip and cusip in prev_shares_by_cusip:
            prev_shares = prev_shares_by_cusip[cusip]
        elif ticker and ticker in prev_shares_by_ticker:
            prev_shares = prev_shares_by_ticker[ticker]
        else:
            h["change"] = "new"
            h["change_pct"] = None
            h["change_shares"] = None
            continue

        delta = curr_shares - prev_shares
        if prev_shares:
            pct = delta / prev_shares * 100
            # Guard against implausibly large swings caused by share-count
            # unit mismatches between filings (e.g. one quarter in lots of
            # 100, next quarter in actual shares) or sub-advisor restructuring.
            # A genuine quarter-over-quarter move above 500% is essentially
            # impossible for a large institutional position.
            if abs(pct) > 500:
                h["change"] = "unknown"
                h["change_pct"] = None
                h["change_shares"] = None
            else:
                h["change_pct"] = round(pct, 1)
                h["change_shares"] = delta
                if delta > 0:
                    h["change"] = "increased"
                elif delta < 0:
                    h["change"] = "reduced"
                else:
                    h["change"] = "unchanged"
        else:
            h["change_pct"] = None
            h["change_shares"] = delta
            if delta > 0:
                h["change"] = "increased"
            elif delta < 0:
                h["change"] = "reduced"
            else:
                h["change"] = "unchanged"
    return curr


def _merge_by_ticker(holdings: List[dict]) -> List[dict]:
    """Merge holdings that share the same resolved ticker symbol.

    Some companies file multiple CUSIP rows for the same ticker (e.g. GOOGL
    Class A vs Class C, or BRK-A vs BRK-B each mapped to the same symbol).
    Rows without a ticker are left as-is.
    """
    seen_ticker: dict = {}
    merged: List[dict] = []
    for h in holdings:
        ticker = h.get("ticker")
        if ticker and ticker in seen_ticker:
            existing = seen_ticker[ticker]
            existing["shares"] += h["shares"]
            existing["value_thousands"] += h["value_thousands"]
            existing["value_millions"] = round(existing["value_thousands"] / 1000, 1)
            # For change: if either row has a definitive signal, keep the most
            # informative one (prefer increased/reduced over unknown/unchanged)
            priority = {"increased": 4, "reduced": 3, "new": 2, "unchanged": 1, "unknown": 0}
            if priority.get(h.get("change", "unknown"), 0) > priority.get(existing.get("change", "unknown"), 0):
                existing["change"] = h["change"]
                existing["change_pct"] = h.get("change_pct")
        else:
            entry = dict(h)
            if ticker:
                seen_ticker[ticker] = entry
            merged.append(entry)
    return merged


# ---------------------------------------------------------------------------
# Which filer stands for which quarter
# ---------------------------------------------------------------------------

#: Full holdings for the newest quarters; older ones are cover-page totals only,
#: which avoids downloading years of multi-MB infotables for an AUM chart.
FULL_HOLDINGS_QUARTERS = 5
MAX_QUARTERS = 12
MAX_LOOKBACK_DAYS = 365 * 4

#: Form 13F's two kinds of amendment, as a 13F-HR/A's cover page names them.
RESTATEMENT = "RESTATEMENT"
NEW_HOLDINGS = "NEW HOLDINGS"


def _filers_for_period(period: str, current: Tuple[str, ...],
                       predecessors: Tuple[Tuple[str, str], ...] = ()) -> Tuple[str, ...]:
    """Return the CIK(s) whose 13F-HR stands for *period*. Pure.

    The earliest predecessor whose cutover is on or after *period* owns it,
    alone; a later period belongs to the current filers (one, or several
    co-filers to be summed). ISO dates compare correctly as strings.
    """
    for cik, last_period in sorted(predecessors, key=lambda p: p[1]):
        if period <= last_period:
            return (cik,)
    return tuple(current)


def _filed_order(filing: dict) -> Tuple[str, str]:
    """Sort key for one filer's filings: filing date, then accession number."""
    return (filing.get("filing_date") or "", filing.get("accession") or "")


def _group_period_filings(cik: str, filings: List[dict]) -> dict:
    """One filer's filings for one period: its 13F-HR and its 13F-HR/As. Pure.

    ``original`` is the latest 13F-HR (normally the only one); ``amendments``
    are oldest first. Which of them count, and how, depends on each
    amendment's cover page -- see _resolve_amendments.
    """
    originals = sorted((f for f in filings if f.get("form") == "13F-HR"), key=_filed_order)
    return {
        "cik":        cik,
        "original":   originals[-1] if originals else None,
        "amendments": sorted((f for f in filings if f.get("form") == "13F-HR/A"), key=_filed_order),
    }


def _resolve_amendments(group: dict,
                        amendment_types: Dict[str, Optional[str]]) -> Tuple[Optional[dict], List[dict]]:
    """(the complete report, the NEW HOLDINGS amendments to add to it). Pure.

    *amendment_types* maps each amendment's accession to its cover page's
    amendmentType. This used to prefer any 13F-HR/A to the original, on the
    theory that an amendment is the more complete filing. A NEW HOLDINGS
    amendment is the opposite -- only the positions confidential treatment kept
    out of the original -- so Berkshire's 2025-03-31 read $1.1B (four rows)
    instead of $259.8B, and Vanguard's 2026-03-31 read $74.3B (its two
    advisers' NEW HOLDINGS amendments, 80 and 54 rows) instead of $5,957B.

    * A RESTATEMENT replaces the report; of several, the latest.
    * NEW HOLDINGS rows are added to the report, by CUSIP. One filed before the
      restatement in use is taken to be inside it, a restatement being a
      complete report; one filed after it is added. Vanguard Capital
      Management filed both on 2026-05-15, restatement first: it has the
      original's 3,982 rows, and the NEW HOLDINGS amendment 80 more.
    * An amendment whose type cannot be read is ignored: taken for either kind
      it could double the quarter or reduce it to a few rows.

    (None, []) when there is neither an original nor a restatement -- NEW
    HOLDINGS alone are not a portfolio.
    """
    amendments = group.get("amendments") or []
    restatements = [a for a in amendments if amendment_types.get(a["accession"]) == RESTATEMENT]
    base = max(restatements, key=_filed_order) if restatements else group.get("original")
    if base is None:
        return None, []
    adds = [a for a in amendments
            if amendment_types.get(a["accession"]) == NEW_HOLDINGS and _filed_order(a) > _filed_order(base)]
    return base, adds


def _plan_quarters(filings: List[dict], current: Tuple[str, ...],
                   predecessors: Tuple[Tuple[str, str], ...] = ()) -> List[Tuple[str, List[dict]]]:
    """[(period, [each owning CIK's filing group])], newest period first. Pure.

    Filings from a CIK that does not own their period (_filers_for_period) are
    dropped here, so a period is never answered by the wrong entity.

    Periods sort CHRONOLOGICALLY, not by filing date: amended filings for old
    periods can be filed years later, and filing-date order would put a
    2023-Q3 amendment after a 2024-Q1 original.
    """
    by_period: Dict[str, Dict[str, List[dict]]] = {}
    for f in filings:
        period = f.get("period") or ""
        if f.get("form") not in ("13F-HR", "13F-HR/A") or not period:
            continue
        if f.get("cik") not in _filers_for_period(period, current, predecessors):
            continue
        by_period.setdefault(period, {}).setdefault(f["cik"], []).append(f)

    plan: List[Tuple[str, List[dict]]] = []
    for period in sorted(by_period, reverse=True):
        owners = _filers_for_period(period, current, predecessors)
        filed = by_period[period]
        plan.append((period, [_group_period_filings(c, filed[c]) for c in owners if c in filed]))
    return plan


def _select_quarters(plan: List[Tuple[str, List[dict]]], max_quarters: int = MAX_QUARTERS,
                     lookback_days: int = MAX_LOOKBACK_DAYS) -> List[Tuple[str, List[dict]]]:
    """The newest quarters to show: up to *max_quarters* within *lookback_days*. Pure.

    GAP-TOLERANT: if a fund skipped a quarter (confidential treatment, a late
    amendment) the surrounding quarters are still shown -- if Berkshire's
    2023-Q3/Q4 are missing, its earlier quarters still appear.
    """
    from datetime import date
    if not plan:
        return []
    try:
        latest = date.fromisoformat(plan[0][0])
    except ValueError:
        latest = None
    selected = [plan[0]]
    for period, entry in plan[1:]:
        if len(selected) >= max_quarters:
            break
        try:
            cp = date.fromisoformat(period)
        except ValueError:
            continue
        if latest and (latest - cp).days > lookback_days:
            break
        selected.append((period, entry))
    return selected


def _fetch_filing_holdings(filing: dict) -> Optional[_ParsedTable]:
    """One filing's information table, parsed; None when it has none.

    Every URL is built from ``filing["cik"]``, the entity that made the filing.
    Raises NotAnInfoTable for an HTML response instead of a parse error.
    """
    url = _find_infotable_url(filing["cik"], filing["accession"], filing.get("primary_doc", ""))
    if not url:
        return None
    resp = _get(url)
    _check_not_html(resp.text, resp.headers.get("content-type", ""), url)
    return _parse_infotable_with_unit(
        resp.text, filing.get("filing_date", ""),
        label=f"{int(filing['cik'])}/{filing['accession']}",
    )


def _fetch_logged(name: str, period: str, filing: dict) -> Optional[_ParsedTable]:
    """_fetch_filing_holdings, with a failure logged and returned as None."""
    try:
        table = _fetch_filing_holdings(filing)
    except Exception as exc:
        log.warning("Could not fetch holdings for %s period=%s: %s (CIK %s, %s)",
                    name, period, exc, filing["cik"], filing["accession"])
        return None
    if table is None:
        log.warning("13F no infotable for %s period=%s (CIK %s, %s)",
                    name, period, filing["cik"], filing["accession"])
    return table


def _amendment_covers(group: dict) -> Dict[str, Optional[dict]]:
    """Each 13F-HR/A's cover page, one request apiece: its type, and its total."""
    return {a["accession"]: _read_cover(group["cik"], a["accession"], a.get("primary_doc", ""))
            for a in group.get("amendments") or []}


def _inherited_unit(filing: dict, unit_by_cik: Dict[str, str]) -> Optional[str]:
    """The unit this filer was seen filing in, for a post-2023 filing not parsed itself."""
    if (filing.get("filing_date") or "") >= DOLLAR_VALUES_FROM:
        return unit_by_cik.get(filing["cik"])
    return None


# ---------------------------------------------------------------------------
# Core fetch function
# ---------------------------------------------------------------------------

def fetch_fund_holdings(name: str, cik: Optional[str] = None) -> dict:
    """
    Fetch 13F holdings for one fund from SEC EDGAR — up to 12 quarters.

    *cik* defaults to FUNDS[name]; COFILERS[name] are summed beside it and
    PREDECESSORS[name] answer the periods before their cutover. Within one
    filer's period, a restatement replaces the original and NEW HOLDINGS
    amendments are added to it (_resolve_amendments).

    Returns a dict with:
      filing_date, period_of_report, holdings (top-50), total_holdings,
      total_value_millions, reported_value_millions — the *latest* quarter
      quarters — newest first, each with period, filing_date, filers (CIKs),
                 accessions (the filings used) and:
        reported_value_millions  what the filer reported: positions plus
                                 options at underlying value, the cover total.
                                 On every quarter, so it is the series to chart.
        total_value_millions     on a parsed quarter, positions only -- the base
                                 of pct_portfolio; on an aum_only quarter the
                                 cover total, as no infotable was read
        holdings, total_holdings, value_units ({cik: unit filed}) on parsed
        quarters; aum_only on the rest
      ciks — every CIK consulted
    """
    cik = cik or FUNDS[name]
    current = (cik,) + tuple(COFILERS.get(name, ()))
    predecessors = tuple(PREDECESSORS.get(name, ()))
    all_ciks = list(dict.fromkeys(current + tuple(c for c, _ in predecessors)))
    log.info("Fetching 13F for %s (CIK %s)", name, ", ".join(all_ciks))
    try:
        filings: list = []
        for filer in all_ciks:
            try:
                filings.extend(_get_filings_list(filer))
            except Exception as exc:
                # A co-filer's filings are part of every current quarter: without
                # them the remainder would read as the whole portfolio. A
                # predecessor only supplies history, so carry on without it.
                if filer in current:
                    raise
                log.warning("13F: no filings list for %s predecessor CIK %s: %s", name, filer, exc)

        all_13f = [f for f in filings if f["form"] in ("13F-HR", "13F-HR/A")]
        if not all_13f:
            return {"error": "No 13F-HR filings found", "cik": cik}

        log.info("13F filings for %s: %s", name,
                 [(f["cik"], f["form"], f["accession"]) for f in all_13f[:6]])

        plan = _plan_quarters(filings, current, predecessors)
        if not plan:
            return {"error": "No 13F-HR filings found", "cik": cik}
        selected_filings = _select_quarters(plan)

        latest_period, latest_entry = selected_filings[0]
        log.info("13F selected for %s: period=%s filings=%s", name, latest_period,
                 [(g["cik"], (g["original"] or {}).get("accession"), [a["accession"] for a in g["amendments"]])
                  for g in latest_entry])
        log.info("13F fetching %d quarters for %s: %s",
                 len(selected_filings), name, [p for p, _ in selected_filings])

        # Quarters 0-4 (5 most recent): full infotable for holdings + change detection.
        # Quarters 5+: cover page only for AUM history (faster, avoids huge XML downloads).
        fetched_quarters: list = []   # newest first
        aum_history_only: list = []
        # The unit each filer used, from its oldest parsed post-2023 filing. The
        # cover-only quarters are older still, and a filer that still filed
        # thousands for 2026-06-30 (T. Rowe, Baupost, Duquesne) did so then too;
        # the filing-date default would make their history 1000x too small.
        unit_by_cik: Dict[str, str] = {}
        for i, (period, entry) in enumerate(selected_filings):
            owners = _filers_for_period(period, current, predecessors)
            if len(entry) < len(owners):
                # Not a failure -- that co-filer filed no 13F-HR for the period --
                # but the quarter is then one entity's book, and "filers" says so.
                log.warning("13F %s period=%s: no 13F-HR from co-filer CIK %s; showing CIK %s alone",
                            name, period,
                            ", ".join(c for c in owners if c not in {g["cik"] for g in entry}),
                            ", ".join(g["cik"] for g in entry))

            # Each filer's complete report for the period, and what to add to it.
            resolved: List[Tuple[str, dict, List[dict], Dict[str, Optional[dict]]]] = []
            no_report: List[str] = []
            for group in entry:
                covers = _amendment_covers(group)
                types = {acc: (cover or {}).get("amendment_type") for acc, cover in covers.items()}
                for a in group["amendments"]:
                    if types.get(a["accession"]) not in (RESTATEMENT, NEW_HOLDINGS):
                        log.warning("13F %s period=%s: amendment %s (CIK %s) has no readable "
                                    "amendmentType; ignored", name, period, a["accession"], group["cik"])
                base, adds = _resolve_amendments(group, types)
                if base is None:
                    log.warning("13F %s period=%s: CIK %s filed NEW HOLDINGS amendments but no report",
                                name, period, group["cik"])
                    no_report.append(group["cik"])
                    continue
                resolved.append((group["cik"], base, adds, covers))
            if no_report:
                if resolved:
                    log.warning("13F %s period=%s skipped: CIK %s has no report, and the other "
                                "co-filers alone would read as the whole portfolio",
                                name, period, ", ".join(no_report))
                continue

            if i < FULL_HOLDINGS_QUARTERS:
                parts: List[List[dict]] = []
                used: List[dict] = []
                units: Dict[str, str] = {}
                failed: List[str] = []
                reported_k = 0.0
                for filer, base, adds, _covers in resolved:
                    table = _fetch_logged(name, period, base)
                    if table is None:
                        failed.append(filer)
                        continue
                    parts.append(table.holdings)
                    used.append(base)
                    reported_k += table.reported_thousands
                    units[filer] = table.unit
                    if (base.get("filing_date") or "") >= DOLLAR_VALUES_FROM:
                        unit_by_cik[filer] = table.unit
                    for add in adds:
                        extra = _fetch_logged(name, period, add)
                        if extra is None:
                            continue   # the report as originally filed, without the late rows
                        parts.append(extra.holdings)
                        used.append(add)
                        reported_k += extra.reported_thousands
                if failed:
                    if parts:
                        log.warning("13F %s period=%s skipped: CIK %s failed, and the other "
                                    "co-filers alone would read as the whole portfolio",
                                    name, period, ", ".join(failed))
                    continue
                merged = _aggregate_by_cusip([h for part in parts for h in part])
                fetched_quarters.append({
                    "period":             period,
                    "filing_date":        max(f["filing_date"] for f in used),
                    "holdings":           merged,
                    "filers":             [filer for filer, *_ in resolved],
                    "accessions":         [f["accession"] for f in used],
                    "value_units":        units,
                    "reported_thousands": reported_k,
                })
                log.info("13F fetched %d holdings for %s period=%s", len(merged), name, period)
            else:
                # AUM-only: cover totals, by the same rule -- the report's total
                # (the original's, or the restatement's) plus each NEW HOLDINGS
                # amendment's -- summed over co-filers. A report whose cover
                # cannot be read drops the point rather than charting part of
                # the portfolio as a fall in assets.
                total: Optional[float] = 0.0
                used = []
                for filer, base, adds, covers in resolved:
                    cover = covers.get(base["accession"]) or _read_cover(filer, base["accession"],
                                                                         base.get("primary_doc", ""))
                    value = _cover_millions(cover, base.get("filing_date", ""),
                                            _inherited_unit(base, unit_by_cik))
                    if value is None:
                        total = None
                        break
                    total += value
                    used.append(base)
                    for add in adds:
                        extra = _cover_millions(covers.get(add["accession"]), add.get("filing_date", ""),
                                                _inherited_unit(add, unit_by_cik))
                        if extra is None:
                            log.warning("13F %s period=%s: no cover total for NEW HOLDINGS %s; left out",
                                        name, period, add["accession"])
                            continue
                        total += extra
                        used.append(add)
                if total is not None:
                    aum_history_only.append({
                        "period":               period,
                        "filing_date":          max(f["filing_date"] for f in used),
                        "total_value_millions": round(total, 1),
                        "filers":               [filer for filer, *_ in resolved],
                        "accessions":           [f["accession"] for f in used],
                    })
                    log.info("13F AUM-only %s period=%s aum=$%sM", name, period, round(total, 1))

        if not fetched_quarters:
            return {"error": "Could not fetch any holdings", "cik": cik}

        # Annotate changes: each quarter vs the one immediately after it
        for i in range(len(fetched_quarters) - 1):
            fetched_quarters[i]["holdings"] = _annotate_changes(
                fetched_quarters[i]["holdings"],
                fetched_quarters[i + 1]["holdings"],
            )
        # Oldest quarter has no prior — mark unknown
        for h in fetched_quarters[-1]["holdings"]:
            h.setdefault("change", "unknown")
            h.setdefault("change_pct", None)
            h.setdefault("change_shares", None)

        # Post-process each full quarter: merge tickers, sort, rank, compute pct
        processed_quarters = []
        for q in fetched_quarters:
            hl = _merge_by_ticker(q["holdings"])
            hl.sort(key=lambda h: h["value_thousands"], reverse=True)
            total_k = sum(h["value_thousands"] for h in hl)
            total_m = round(total_k / 1000, 1)
            top50 = hl[:50]
            for j, h in enumerate(top50, 1):
                h["rank"] = j
                h["pct_portfolio"] = (
                    round(h["value_thousands"] / total_k * 100, 2)
                    if total_k > 0 else 0.0
                )
            processed_quarters.append({
                "period":                  q["period"],
                "filing_date":             q["filing_date"],
                "holdings":                top50,
                "total_holdings":          len(hl),
                "total_value_millions":    total_m,
                "reported_value_millions": round(q["reported_thousands"] / 1000, 1),
                "filers":                  q["filers"],
                "accessions":              q["accessions"],
                "value_units":             q["value_units"],
            })

        # Append AUM-only history quarters (no holdings data, just totals for the chart)
        for aq in aum_history_only:
            processed_quarters.append({
                "period":                  aq["period"],
                "filing_date":             aq["filing_date"],
                "holdings":                [],
                "total_holdings":          0,
                "total_value_millions":    aq["total_value_millions"],
                "reported_value_millions": aq["total_value_millions"],
                "filers":                  aq["filers"],
                "accessions":              aq["accessions"],
                "aum_only":                True,  # flag: no holdings detail available
            })

        latest_q = processed_quarters[0]
        return {
            "cik":                     cik,
            "ciks":                    all_ciks,
            "filing_date":             latest_q["filing_date"],
            "period_of_report":        latest_q["period"],
            "holdings":                latest_q["holdings"],
            "total_holdings":          latest_q["total_holdings"],
            "total_value_millions":    latest_q["total_value_millions"],
            "reported_value_millions": latest_q["reported_value_millions"],
            "quarters":                processed_quarters,
            "error":                   None,
        }

    except Exception as exc:
        log.exception("Failed to fetch 13F for %s: %s", name, exc)
        return {"error": str(exc), "cik": cik}


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _save_cache(data: dict, ts: float) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _CACHE_VER, "timestamp": ts, "data": data}
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        tmp.replace(_CACHE_FILE)
        log.info("13F cache saved to %s", _CACHE_FILE)
    except Exception:
        log.exception("Failed to save 13F cache")


def _load_cache() -> bool:
    global _sec13f_data, _sec13f_ts
    if not _CACHE_FILE.exists():
        return False
    try:
        payload = json.loads(_CACHE_FILE.read_text())
        if payload.get("version") != _CACHE_VER:
            log.info("13F disk cache is version %r, this code writes %r -- refetching",
                     payload.get("version"), _CACHE_VER)
            return False
        ts  = float(payload["timestamp"])
        age = time.time() - ts
        if age > _CACHE_TTL:
            log.info("13F disk cache stale (%.1fh)", age / 3600)
            return False
        with _sec13f_lock:
            _sec13f_data = payload["data"]
            _sec13f_ts   = ts
        log.info("Loaded 13F cache (%.1fh old)", age / 3600)
        return True
    except Exception:
        log.exception("Failed to load 13F cache")
        return False


#: How long one refresh may run before the funds still fetching keep their
#: previous entry. 900 s, not the 300 s it used to be: the refresh at 14:21 on
#: 2026-10-03 needed 308 s for the old registry, in which ten funds answered
#: "No 13F-HR filings found" after one request, and the corrected one puts
#: Jane Street Group, Susquehanna, Geode and Marshall Wace in their place --
#: 6,500 to 15,200 rows a quarter each. The first refresh after a deploy has
#: nothing to carry forward (_CACHE_VER), so a fund that misses the budget then
#: shows an error until the next refresh, a day later.
_REFRESH_TIMEOUT_SECONDS = fetchguard.env_float("SEC13F_REFRESH_TIMEOUT_SECONDS", 900.0, 1.0)


def refresh_cache() -> None:
    """Fetch all funds in parallel and write cache. Runs in a background thread.

    Bounded by _REFRESH_TIMEOUT_SECONDS. This used ``as_completed(timeout=300)``,
    whose TimeoutError escaped the loop and threw away every fund that *had*
    finished: at 14:21:24 on 2026-10-03, 5 of 51 fetches were still running,
    the other 46 results were discarded with the cache left as it was, and the
    refresh started over at once. Leaving the ``with ThreadPoolExecutor`` block
    would also have waited for the stragglers regardless. Now finished results
    are kept, fetches not yet started are cancelled, and a fund still running
    keeps its previous entry, marked ``carried_forward``.
    """
    import concurrent.futures as _cf

    global _sec13f_data, _sec13f_ts, _sec13f_warming
    with _sec13f_lock:
        _sec13f_warming = True
        previous = dict(_sec13f_data or {})
    try:
        result: dict = {}
        pool = _cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="sec13f")
        try:
            futures = {pool.submit(fetch_fund_holdings, name, cik): name
                       for name, cik in FUNDS.items()}
            done, not_done = _cf.wait(futures, timeout=_REFRESH_TIMEOUT_SECONDS)
        finally:
            # Cancels what has not started. A fetch already running cannot be
            # interrupted: it finishes in the background and its result is unused.
            pool.shutdown(wait=False, cancel_futures=True)

        refetch_failed: List[str] = []
        for fut in done:
            name = futures[fut]
            try:
                result[name] = fut.result()
            except Exception as exc:
                log.warning("13F: parallel fetch failed for %s: %s", name, exc)
                result[name] = {"error": str(exc), "quarters": []}
            # A failed refetch keeps yesterday's book rather than replacing it
            # with an error for a day: the filings did not change, the fetch
            # did. The error rides along so the page can still say so.
            prev = previous.get(name)
            if (result[name].get("error") and isinstance(prev, dict)
                    and not prev.get("error") and prev.get("quarters")):
                result[name] = dict(prev, carried_forward=True,
                                    refresh_error=result[name]["error"])
                refetch_failed.append(name)
        if refetch_failed:
            log.warning("13F refresh: %d funds failed to refetch and kept their "
                        "previous entry: %s", len(refetch_failed),
                        ", ".join(sorted(refetch_failed)))

        carried: List[str] = []
        lost: List[str] = []
        for fut in not_done:
            name = futures[fut]
            prev = previous.get(name)
            if isinstance(prev, dict):
                result[name] = dict(prev, carried_forward=True)
                carried.append(name)
            else:
                result[name] = {"error": f"Timed out after {_REFRESH_TIMEOUT_SECONDS:.0f}s",
                                "cik": FUNDS.get(name), "quarters": []}
                lost.append(name)
        if not_done:
            log.warning("13F refresh: %d of %d funds unfinished after %.0fs; "
                        "carried forward: %s; no previous entry: %s",
                        len(not_done), len(futures), _REFRESH_TIMEOUT_SECONDS,
                        ", ".join(sorted(carried)) or "none", ", ".join(sorted(lost)) or "none")

        ts = time.time()
        with _sec13f_lock:
            _sec13f_data = result
            _sec13f_ts   = ts
            _sec13f_warming = False
        _save_cache(result, ts)
    except Exception:
        log.exception("Unhandled error in 13F refresh_cache")
        with _sec13f_lock:
            _sec13f_warming = False


def get_all_holdings() -> Dict[str, dict]:
    """Return cached holdings for all funds, loading/fetching as needed."""
    with _sec13f_lock:
        data = _sec13f_data
    if data is not None:
        return data
    if _load_cache():
        with _sec13f_lock:
            return _sec13f_data or {}
    # No fresh cache — return empty; background thread will fill it
    return {}


def is_cache_fresh() -> bool:
    with _sec13f_lock:
        if _sec13f_ts is None:
            return False
        return (time.time() - _sec13f_ts) < _CACHE_TTL


def get_cache_ts() -> Optional[float]:
    with _sec13f_lock:
        return _sec13f_ts


def is_warming() -> bool:
    with _sec13f_lock:
        return _sec13f_warming


def start_background_thread() -> None:
    """Start background thread: load or refresh on startup, then every 24h."""
    from ystocker import warmup

    def _loop():
        global _sec13f_warming
        if not _load_cache():
            log.info("13F: no fresh disk cache — fetching now")
            with warmup.cold_build('sec13f'):
                refresh_cache()
        while True:
            with _sec13f_lock:
                last = _sec13f_ts
            sleep_for = _CACHE_TTL - (time.time() - last) if last else _CACHE_TTL
            sleep_for = max(sleep_for, 0)
            log.info("Next 13F refresh in %.1fh", sleep_for / 3600)
            time.sleep(sleep_for)
            with warmup.cold_build('sec13f'):
                refresh_cache()

    t = threading.Thread(target=_loop, daemon=True, name="sec13f-warmer")
    t.start()
    log.info("13F cache warmer started (TTL 24h, file: %s)", _CACHE_FILE)
