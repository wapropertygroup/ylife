"""
ystocker.sec13f
~~~~~~~~~~~~~~~
Fetches 13F institutional holdings from SEC EDGAR for a fixed list of top funds.

Data flow per fund:
  1. GET data.sec.gov/submissions/CIK{cik}.json  → find latest 13F-HR filing
  2. GET Archives/edgar/data/{cik}/{accession}-index.json  → find infotable filename
  3. GET Archives/edgar/data/{cik}/{accession}/{infotable}  → parse XML holdings
  4. Repeat step 1-3 for previous quarter to compute change classification

All results are cached on disk (24h TTL) and in memory.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fund registry  {display_name: zero-padded CIK}
# ---------------------------------------------------------------------------
FUNDS: Dict[str, str] = {
    # Mega funds — household names
    "Berkshire Hathaway":       "0001067983",
    "Vanguard Group":           "0000102909",
    "BlackRock":                "0002012383",  # BlackRock, Inc. (BLK) — active 13F filer, period 2025-09
    "State Street":             "0000093751",
    "Fidelity (FMR)":           "0000315066",

    # Macro / multi-strategy
    "Bridgewater Associates":   "0001350694",
    "Citadel Advisors":         "0001423053",  # Citadel Advisors LLC
    "Millennium Management":    "0001273087",  # Millennium Management LLC
    "Point72 Asset Management": "0001603466",
    "DE Shaw":                  "0001009207",

    # Tiger cubs & growth equity
    "Tiger Global":             "0001167483",
    "Coatue Management":        "0001336528",
    "Viking Global":            "0001103804",
    "Lone Pine Capital":        "0001061165",
    "Maverick Capital":         "0000934639",  # Maverick Capital Ltd (active, filed Feb 2026)

    # Value / activist
    "Third Point":              "0001040273",
    "Pershing Square":          "0002026053",  # Pershing Square Holdco, L.P.
    "Baupost Group":            "0001061768",
    "Elliott Management":       "0001791786",  # Elliott Investment Management L.P.
    "Starboard Value":          "0001517137",  # Starboard Value LP

    # Growth / tech focus
    "Soros Fund Management":    "0001029160",
    "Duquesne Family Office":   "0001536411",
    "ARK Investment":           "0001697748",  # ARK Investment Management LLC
    "Whale Rock Capital":       "0001387322",  # Whale Rock Capital Management LLC

    # Quant / systematic
    "Renaissance Technologies": "0001037389",
    "Two Sigma Investments":    "0001179392",
    "AQR Capital":              "0001167557",
}

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
    "166764100": "C",
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

    # ── Telecom ──────────────────────────────────────────────────────────────
    "92343V104": "VZ",
    "00206R102": "T",
    "88339J105": "TMUS",
    "87264F100": "TMUS",    # T-Mobile alternate
    "872590104": "TTD",     # The Trade Desk (NOT TMUS)

    # ── Utilities / Real Estate ──────────────────────────────────────────────
    "637640103": "NEE",
    "65339F101": "NEE",     # alternate
    "15135B101": "CEG",     # Constellation Energy
    "21037T109": "CEG",     # alternate
    "263534109": "ECL",
    "49446R109": "PSA",     # Public Storage correct CUSIP
    "78467J100": "SPG",

    # ── Materials / Mining ───────────────────────────────────────────────────
    "345370860": "FCX",
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
}

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_CACHE_FILE = Path(__file__).parent.parent / "cache" / "sec13f_cache.json"
_CACHE_TTL  = 24 * 60 * 60  # 24 h — 13F data changes quarterly

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


def _get(url: str, **kwargs) -> requests.Response:
    """Rate-limited GET. Raises on non-2xx (caller must handle)."""
    global _LAST_REQ_TIME
    gap = time.time() - _LAST_REQ_TIME
    if gap < _RATE_LIMIT_INTERVAL:
        time.sleep(_RATE_LIMIT_INTERVAL - gap)
    _LAST_REQ_TIME = time.time()
    resp = _SESSION.get(url, timeout=20, **kwargs)
    if resp.status_code == 429:
        log.warning("SEC rate limit hit, sleeping 2s")
        time.sleep(2)
        resp = _SESSION.get(url, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


def _get_maybe(url: str, **kwargs) -> Optional[requests.Response]:
    """
    Rate-limited GET that returns None on 404/403 instead of raising.
    All other errors still raise.
    """
    global _LAST_REQ_TIME
    gap = time.time() - _LAST_REQ_TIME
    if gap < _RATE_LIMIT_INTERVAL:
        time.sleep(_RATE_LIMIT_INTERVAL - gap)
    _LAST_REQ_TIME = time.time()
    resp = _SESSION.get(url, timeout=20, **kwargs)
    if resp.status_code == 429:
        log.warning("SEC rate limit hit, sleeping 2s")
        time.sleep(2)
        resp = _SESSION.get(url, timeout=20, **kwargs)
    if resp.status_code in (404, 403, 503):
        return None
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# SEC EDGAR parsing helpers
# ---------------------------------------------------------------------------

def _get_filings_list(cik: str) -> list:
    """Return list of recent filings dicts from SEC submissions endpoint.

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
            {"form": forms[i], "accession": accessions[i],
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
    if len(periods_in_recent) < 2:
        extra_files = data.get("filings", {}).get("files", [])
        for extra in extra_files[:2]:          # fetch at most 2 extra pages
            extra_name = extra.get("name", "")
            if not extra_name:
                continue
            extra_url = f"https://data.sec.gov/submissions/{extra_name}"
            try:
                extra_data = _get(extra_url).json()
                extra_filings = _extract(extra_data)
                filings.extend(extra_filings)
                # Stop once we have ≥2 distinct 13F periods
                periods_so_far = {
                    f["period"] for f in filings if f["form"] in ("13F-HR", "13F-HR/A")
                }
                if len(periods_so_far) >= 2:
                    break
            except Exception as exc:
                log.debug("Could not fetch extra filings page %s: %s", extra_name, exc)

    return filings


def _find_infotable_url(cik: str, accession: str, primary_doc: str = "") -> Optional[str]:
    """
    Return the URL of the infotable XML for a given 13F-HR filing.

    Strategy (most reliable first):
    1. Try the -index.json endpoint (available for filings ~2019+)
    2. Parse the -index.htm HTML for document links (universal fallback)
    3. Try common infotable filename patterns directly (last resort)

    Uses _get_maybe() for index fetches so 404 silently falls through
    to the next strategy instead of raising.
    """
    import re
    cik_int    = str(int(cik))
    acc_nodash = accession.replace("-", "")
    # SEC index filenames use the dashed accession number, e.g. 0000950123-25-002701-index.htm
    # The directory uses no dashes, e.g. 000095012325002701/
    # Re-insert dashes: 18-digit → XXXXXXXXXX-YY-ZZZZZZ
    acc_dashed = f"{acc_nodash[:10]}-{acc_nodash[10:12]}-{acc_nodash[12:]}"
    # data.sec.gov serves index/metadata; actual filing documents live on www.sec.gov
    index_base = f"https://data.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
    doc_base   = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
    primary_lower = primary_doc.lower()

    # ── Strategy 1: JSON index (newer filings ~2019+) ───────────────────────
    r = _get_maybe(f"{index_base}-index.json")
    if r is not None:
        try:
            idx = r.json()
            for doc in idx.get("documents", []):
                desc  = (doc.get("documentDescription") or "").lower()
                dname = (doc.get("name") or "")
                fname = dname.lower()
                dtype = (doc.get("type") or "").upper()
                if "xslform13f" in fname:
                    continue  # XSLT-rendered HTML, skip
                if (dtype == "INFORMATION TABLE"
                        or "information table" in desc
                        or "infotable" in fname
                        or "info_table" in fname):
                    return f"{doc_base}/{dname}"
            # fallback within JSON: first raw XML that isn't primary_doc.xml (cover page)
            for doc in idx.get("documents", []):
                dname = doc.get("name") or ""
                fname = dname.lower()
                if (fname.endswith(".xml")
                        and "xslform13f" not in fname
                        and fname.split("/")[-1] != "primary_doc.xml"):
                    return f"{doc_base}/{dname}"
        except Exception as exc:
            log.debug("JSON index parse failed for %s/%s: %s", cik_int, acc_nodash, exc)

    # ── Strategy 2: HTML index — try both dashed filename variants ───────────
    for htm_url in [
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_dashed}-index.htm",
        f"https://data.sec.gov/Archives/edgar/data/{cik_int}/{acc_dashed}-index.htm",
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}-index.htm",
        f"https://data.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}-index.htm",
    ]:
        r2 = _get_maybe(htm_url)
        if r2 is None:
            continue
        try:
            # Match absolute paths (/Archives/edgar/data/...) for any file type
            xml_links = re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.xml)"',
                r2.text, re.IGNORECASE
            )
            # Also match relative hrefs (e.g. xslForm13F_X02/39042.xml or plain filename.xml)
            if not xml_links:
                rel_links = re.findall(r'href="([^"]+\.xml)"', r2.text, re.IGNORECASE)
                xml_links = [
                    f"/Archives/edgar/data/{cik_int}/{acc_nodash}/{f}"
                    if not f.startswith("/") else f
                    for f in rel_links
                ]
            log.info("13F HTML index %s → %d xml links: %s", htm_url, len(xml_links), xml_links[:6])
            # xslForm13F_X02/ paths are XSLT-rendered HTML, not raw XML — skip them.
            # primary_doc.xml at root level is the cover/header XML (edgarSubmission),
            # not the infotable. The actual data file has a unique name (50240.xml,
            # form13fInfoTable.xml, 20260217_FMRLLC.xml, etc.)
            raw_links = [
                p for p in xml_links
                if "xslForm13F_X02/" not in p
                and p.split("/")[-1].lower() != "primary_doc.xml"
            ]
            # Prefer raw files with 'infotable', 'info_table', or similar in name
            for path in raw_links:
                fname = path.split("/")[-1].lower()
                if "infotable" in fname or "info_table" in fname:
                    return f"https://www.sec.gov" + path
            # Take first qualifying raw XML
            for path in raw_links:
                return f"https://www.sec.gov" + path
            break  # parsed OK (even if no XML found) — stop trying variants
        except Exception as exc:
            log.debug("HTML index parse failed for %s/%s: %s", cik_int, acc_nodash, exc)

    # ── Strategy 3: Try common filename patterns directly ───────────────────
    primary_stem = primary_doc.rsplit(".", 1)[0] if "." in primary_doc else primary_doc
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
        if not fname or fname.startswith("_"):
            continue
        r3 = _get_maybe(f"{doc_base}/{fname}")
        if r3 is not None and r3.text.strip():
            log.debug("Found infotable via direct guess: %s/%s", acc_nodash, fname)
            return f"{doc_base}/{fname}"

    log.warning("Could not find infotable for CIK %s accession %s", cik_int, acc_nodash)
    return None


def _parse_infotable(xml_text: str) -> List[dict]:
    """Parse SEC 13F infotable XML and return list of holding dicts.

    Holdings with the same CUSIP (e.g. split across multiple sub-advisors or
    share classes filed separately) are aggregated into a single row so that
    change detection and portfolio-weight calculations are accurate.
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

    raw_holdings = []
    for entry in root.iter(f"{ns_prefix}infoTable"):
        def _t(tag: str) -> Optional[str]:
            el = entry.find(f"{ns_prefix}{tag}")
            return el.text.strip() if el is not None and el.text else None

        # Skip options positions
        put_call = _t("putCall")
        if put_call:
            continue

        try:
            value_k = int(_t("value") or "0")
            shares_el = entry.find(f"{ns_prefix}shrsOrPrnAmt")
            shares = int(shares_el.find(f"{ns_prefix}sshPrnamt").text) if shares_el is not None else 0
        except (ValueError, AttributeError):
            continue

        cusip = (_t("cusip") or "").strip()
        name  = (_t("nameOfIssuer") or "").strip()
        ticker = CUSIP_TO_TICKER.get(cusip)

        raw_holdings.append({
            "cusip":          cusip,
            "name":           name,
            "ticker":         ticker,
            "shares":         shares,
            "value_thousands": value_k,
            "value_millions": round(value_k / 1000, 1),
        })

    log.info("13F _parse_infotable: found %d raw rows", len(raw_holdings))

    # Aggregate duplicate CUSIPs (same security filed across multiple rows,
    # e.g. different sub-advisors or custodians within the same fund)
    seen: dict = {}
    merged: List[dict] = []
    for h in raw_holdings:
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

    log.info("13F _parse_infotable: %d holdings after dedup", len(merged))
    return merged


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
            else:
                h["change_pct"] = round(pct, 1)
                if delta > 0:
                    h["change"] = "increased"
                elif delta < 0:
                    h["change"] = "reduced"
                else:
                    h["change"] = "unchanged"
        else:
            h["change_pct"] = None
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
# Core fetch function
# ---------------------------------------------------------------------------

def fetch_fund_holdings(name: str, cik: str) -> dict:
    """
    Fetch 13F holdings for one fund from SEC EDGAR — up to 4 quarters.

    Returns a dict with:
      filing_date, period_of_report, holdings (top-50), total_holdings,
      total_value_millions  — all from the *latest* quarter (for the main table)
      quarters              — list of {period, filing_date, holdings,
                              total_value_millions} for up to 4 recent quarters
    """
    log.info("Fetching 13F for %s (CIK %s)", name, cik)
    try:
        filings = _get_filings_list(cik)
        all_13f = [f for f in filings if f["form"] in ("13F-HR", "13F-HR/A")]
        if not all_13f:
            return {"error": "No 13F-HR filings found", "cik": cik}

        log.info("13F filings for %s: %s", name,
                 [(f["accession"], f.get("primary_doc")) for f in all_13f[:6]])

        # Group by period so we can pick the best filing per quarter.
        periods_seen: list = []
        by_period: dict = {}
        for f in all_13f:
            p = f.get("period", "")
            if p not in by_period:
                by_period[p] = []
                periods_seen.append(p)
            by_period[p].append(f)

        def _best_for_period(candidates):
            non_cover = [c for c in candidates
                         if c.get("primary_doc", "").lower() != "primary_doc.xml"]
            return (non_cover or candidates)[0]

        # Build ordered list: latest period first
        thirteenf_filings = [_best_for_period(by_period[p]) for p in periods_seen]

        log.info("13F selected for %s: accession=%s primary_doc=%s period=%s",
                 name, thirteenf_filings[0]["accession"],
                 thirteenf_filings[0].get("primary_doc"),
                 thirteenf_filings[0].get("period"))
        latest = thirteenf_filings[0]

        # Select up to 4 consecutive quarters, each 60-200 days apart from
        # the previous one.  This gives us history for the chart.
        from datetime import date
        selected_filings = [latest]
        for candidate in thirteenf_filings[1:]:
            if len(selected_filings) >= 4:
                break
            cand_period = candidate.get("period", "")
            prev_period = selected_filings[-1].get("period", "")
            if not cand_period or not prev_period:
                break
            try:
                pp = date.fromisoformat(prev_period)
                cp = date.fromisoformat(cand_period)
                delta_days = (pp - cp).days
                if 60 <= delta_days <= 200:
                    selected_filings.append(candidate)
                elif delta_days > 200:
                    break
            except ValueError:
                break

        log.info("13F fetching %d quarters for %s: %s",
                 len(selected_filings), name,
                 [f["period"] for f in selected_filings])

        # Fetch holdings for each selected quarter
        fetched_quarters: list = []   # newest first
        for i, filing in enumerate(selected_filings):
            try:
                url = _find_infotable_url(cik, filing["accession"], filing.get("primary_doc", ""))
                if not url:
                    log.warning("13F no infotable for %s period=%s", name, filing["period"])
                    continue
                xml_text = _get(url).text
                holdings = _parse_infotable(xml_text)
                fetched_quarters.append({
                    "period":       filing["period"],
                    "filing_date":  filing["filing_date"],
                    "holdings":     holdings,
                })
                log.info("13F fetched %d holdings for %s period=%s",
                         len(holdings), name, filing["period"])
            except Exception as exc:
                log.warning("Could not fetch holdings for %s period=%s: %s",
                            name, filing.get("period"), exc)

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

        # Post-process each quarter: merge tickers, sort, rank, compute pct
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
                "period":               q["period"],
                "filing_date":          q["filing_date"],
                "holdings":             top50,
                "total_holdings":       len(hl),
                "total_value_millions": total_m,
            })

        latest_q = processed_quarters[0]
        return {
            "cik":                  cik,
            "filing_date":          latest_q["filing_date"],
            "period_of_report":     latest_q["period"],
            "holdings":             latest_q["holdings"],
            "total_holdings":       latest_q["total_holdings"],
            "total_value_millions": latest_q["total_value_millions"],
            "quarters":             processed_quarters,
            "error":                None,
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
        payload = {"timestamp": ts, "data": data}
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


def refresh_cache() -> None:
    """Fetch all funds and write cache. Runs in a background thread."""
    global _sec13f_data, _sec13f_ts, _sec13f_warming
    with _sec13f_lock:
        _sec13f_warming = True
    try:
        result = {}
        for name, cik in FUNDS.items():
            result[name] = fetch_fund_holdings(name, cik)
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
    def _loop():
        global _sec13f_warming
        if not _load_cache():
            log.info("13F: no fresh disk cache — fetching now")
            refresh_cache()
        while True:
            with _sec13f_lock:
                last = _sec13f_ts
            sleep_for = _CACHE_TTL - (time.time() - last) if last else _CACHE_TTL
            sleep_for = max(sleep_for, 0)
            log.info("Next 13F refresh in %.1fh", sleep_for / 3600)
            time.sleep(sleep_for)
            refresh_cache()

    t = threading.Thread(target=_loop, daemon=True, name="sec13f-warmer")
    t.start()
    log.info("13F cache warmer started (TTL 24h, file: %s)", _CACHE_FILE)
