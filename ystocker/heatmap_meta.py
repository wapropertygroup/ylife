"""
ystocker.heatmap_meta
~~~~~~~~~~~~~~~~~~~~~
Static metadata for the sector heatmap: ~105 top S&P 500 stocks.
ticker -> {name, sector, mkt_cap_b}   (mkt_cap_b is approximate, for tile sizing)
"""
from __future__ import annotations

# Approximate market caps (billions USD) as of early 2025 — used for tile sizing only.
# These are intentionally static; the live day_chg drives colour, not these values.
HEATMAP_META: dict[str, dict] = {
    # ── Technology (15) ─────────────────────────────────────────────────
    "AAPL":  {"name": "Apple",              "sector": "Technology",              "mkt_cap_b": 3300},
    "MSFT":  {"name": "Microsoft",          "sector": "Technology",              "mkt_cap_b": 3000},
    "NVDA":  {"name": "NVIDIA",             "sector": "Technology",              "mkt_cap_b": 2800},
    "AVGO":  {"name": "Broadcom",           "sector": "Technology",              "mkt_cap_b":  900},
    "ORCL":  {"name": "Oracle",             "sector": "Technology",              "mkt_cap_b":  500},
    "CSCO":  {"name": "Cisco",              "sector": "Technology",              "mkt_cap_b":  220},
    "ADBE":  {"name": "Adobe",              "sector": "Technology",              "mkt_cap_b":  200},
    "CRM":   {"name": "Salesforce",         "sector": "Technology",              "mkt_cap_b":  280},
    "AMD":   {"name": "AMD",                "sector": "Technology",              "mkt_cap_b":  250},
    "QCOM":  {"name": "Qualcomm",           "sector": "Technology",              "mkt_cap_b":  170},
    "TXN":   {"name": "Texas Instruments",  "sector": "Technology",              "mkt_cap_b":  170},
    "INTC":  {"name": "Intel",              "sector": "Technology",              "mkt_cap_b":   90},
    "NOW":   {"name": "ServiceNow",         "sector": "Technology",              "mkt_cap_b":  200},
    "INTU":  {"name": "Intuit",             "sector": "Technology",              "mkt_cap_b":  175},
    "IBM":   {"name": "IBM",                "sector": "Technology",              "mkt_cap_b":  220},

    # ── Communication Services (8) ───────────────────────────────────────
    "GOOGL": {"name": "Alphabet",           "sector": "Communication Services",  "mkt_cap_b": 2100},
    "META":  {"name": "Meta",               "sector": "Communication Services",  "mkt_cap_b": 1500},
    "NFLX":  {"name": "Netflix",            "sector": "Communication Services",  "mkt_cap_b":  350},
    "DIS":   {"name": "Disney",             "sector": "Communication Services",  "mkt_cap_b":  195},
    "CMCSA": {"name": "Comcast",            "sector": "Communication Services",  "mkt_cap_b":  145},
    "VZ":    {"name": "Verizon",            "sector": "Communication Services",  "mkt_cap_b":  165},
    "T":     {"name": "AT&T",               "sector": "Communication Services",  "mkt_cap_b":  145},
    "TMUS":  {"name": "T-Mobile",           "sector": "Communication Services",  "mkt_cap_b":  260},

    # ── Consumer Discretionary (10) ──────────────────────────────────────
    "AMZN":  {"name": "Amazon",             "sector": "Consumer Discretionary",  "mkt_cap_b": 2200},
    "TSLA":  {"name": "Tesla",              "sector": "Consumer Discretionary",  "mkt_cap_b":  850},
    "HD":    {"name": "Home Depot",         "sector": "Consumer Discretionary",  "mkt_cap_b":  380},
    "MCD":   {"name": "McDonald's",         "sector": "Consumer Discretionary",  "mkt_cap_b":  215},
    "NKE":   {"name": "Nike",               "sector": "Consumer Discretionary",  "mkt_cap_b":   93},
    "LOW":   {"name": "Lowe's",             "sector": "Consumer Discretionary",  "mkt_cap_b":  140},
    "BKNG":  {"name": "Booking Holdings",   "sector": "Consumer Discretionary",  "mkt_cap_b":  160},
    "TJX":   {"name": "TJX Companies",      "sector": "Consumer Discretionary",  "mkt_cap_b":  130},
    "SBUX":  {"name": "Starbucks",          "sector": "Consumer Discretionary",  "mkt_cap_b":   95},
    "CMG":   {"name": "Chipotle",           "sector": "Consumer Discretionary",  "mkt_cap_b":   85},

    # ── Consumer Staples (9) ─────────────────────────────────────────────
    "WMT":   {"name": "Walmart",            "sector": "Consumer Staples",        "mkt_cap_b":  730},
    "PG":    {"name": "Procter & Gamble",   "sector": "Consumer Staples",        "mkt_cap_b":  380},
    "KO":    {"name": "Coca-Cola",          "sector": "Consumer Staples",        "mkt_cap_b":  270},
    "PEP":   {"name": "PepsiCo",            "sector": "Consumer Staples",        "mkt_cap_b":  215},
    "COST":  {"name": "Costco",             "sector": "Consumer Staples",        "mkt_cap_b":  380},
    "PM":    {"name": "Philip Morris",      "sector": "Consumer Staples",        "mkt_cap_b":  220},
    "MO":    {"name": "Altria",             "sector": "Consumer Staples",        "mkt_cap_b":   95},
    "MDLZ":  {"name": "Mondelez",           "sector": "Consumer Staples",        "mkt_cap_b":   88},
    "CL":    {"name": "Colgate-Palmolive",  "sector": "Consumer Staples",        "mkt_cap_b":   63},

    # ── Financials (12) ──────────────────────────────────────────────────
    "BRK-B": {"name": "Berkshire Hathaway", "sector": "Financials",              "mkt_cap_b":  980},
    "JPM":   {"name": "JPMorgan Chase",     "sector": "Financials",              "mkt_cap_b":  700},
    "V":     {"name": "Visa",               "sector": "Financials",              "mkt_cap_b":  580},
    "MA":    {"name": "Mastercard",         "sector": "Financials",              "mkt_cap_b":  475},
    "BAC":   {"name": "Bank of America",    "sector": "Financials",              "mkt_cap_b":  330},
    "WFC":   {"name": "Wells Fargo",        "sector": "Financials",              "mkt_cap_b":  250},
    "GS":    {"name": "Goldman Sachs",      "sector": "Financials",              "mkt_cap_b":  185},
    "MS":    {"name": "Morgan Stanley",     "sector": "Financials",              "mkt_cap_b":  175},
    "BLK":   {"name": "BlackRock",          "sector": "Financials",              "mkt_cap_b":  150},
    "AXP":   {"name": "American Express",   "sector": "Financials",              "mkt_cap_b":  195},
    "SCHW":  {"name": "Charles Schwab",     "sector": "Financials",              "mkt_cap_b":  130},
    "C":     {"name": "Citigroup",          "sector": "Financials",              "mkt_cap_b":  130},

    # ── Healthcare (12) ──────────────────────────────────────────────────
    "UNH":   {"name": "UnitedHealth",       "sector": "Healthcare",              "mkt_cap_b":  490},
    "LLY":   {"name": "Eli Lilly",          "sector": "Healthcare",              "mkt_cap_b":  760},
    "JNJ":   {"name": "Johnson & Johnson",  "sector": "Healthcare",              "mkt_cap_b":  380},
    "ABBV":  {"name": "AbbVie",             "sector": "Healthcare",              "mkt_cap_b":  330},
    "MRK":   {"name": "Merck",              "sector": "Healthcare",              "mkt_cap_b":  255},
    "TMO":   {"name": "Thermo Fisher",      "sector": "Healthcare",              "mkt_cap_b":  195},
    "ABT":   {"name": "Abbott",             "sector": "Healthcare",              "mkt_cap_b":  205},
    "DHR":   {"name": "Danaher",            "sector": "Healthcare",              "mkt_cap_b":  155},
    "AMGN":  {"name": "Amgen",              "sector": "Healthcare",              "mkt_cap_b":  165},
    "ISRG":  {"name": "Intuitive Surgical", "sector": "Healthcare",              "mkt_cap_b":  185},
    "VRTX":  {"name": "Vertex Pharma",      "sector": "Healthcare",              "mkt_cap_b":  130},
    "PFE":   {"name": "Pfizer",             "sector": "Healthcare",              "mkt_cap_b":  145},

    # ── Industrials (10) ─────────────────────────────────────────────────
    "GE":    {"name": "GE Aerospace",       "sector": "Industrials",             "mkt_cap_b":  205},
    "RTX":   {"name": "RTX Corp",           "sector": "Industrials",             "mkt_cap_b":  175},
    "HON":   {"name": "Honeywell",          "sector": "Industrials",             "mkt_cap_b":  130},
    "CAT":   {"name": "Caterpillar",        "sector": "Industrials",             "mkt_cap_b":  195},
    "UNP":   {"name": "Union Pacific",      "sector": "Industrials",             "mkt_cap_b":  145},
    "LMT":   {"name": "Lockheed Martin",    "sector": "Industrials",             "mkt_cap_b":  120},
    "DE":    {"name": "Deere",              "sector": "Industrials",             "mkt_cap_b":  115},
    "BA":    {"name": "Boeing",             "sector": "Industrials",             "mkt_cap_b":  105},
    "FDX":   {"name": "FedEx",              "sector": "Industrials",             "mkt_cap_b":   63},
    "UPS":   {"name": "UPS",                "sector": "Industrials",             "mkt_cap_b":   85},

    # ── Energy (8) ───────────────────────────────────────────────────────
    "XOM":   {"name": "ExxonMobil",         "sector": "Energy",                  "mkt_cap_b":  510},
    "CVX":   {"name": "Chevron",            "sector": "Energy",                  "mkt_cap_b":  280},
    "COP":   {"name": "ConocoPhillips",     "sector": "Energy",                  "mkt_cap_b":  135},
    "EOG":   {"name": "EOG Resources",      "sector": "Energy",                  "mkt_cap_b":   65},
    "SLB":   {"name": "SLB",               "sector": "Energy",                  "mkt_cap_b":   58},
    "MPC":   {"name": "Marathon Petroleum", "sector": "Energy",                  "mkt_cap_b":   55},
    "OXY":   {"name": "Occidental",         "sector": "Energy",                  "mkt_cap_b":   47},
    "PSX":   {"name": "Phillips 66",        "sector": "Energy",                  "mkt_cap_b":   45},

    # ── Materials (6) ────────────────────────────────────────────────────
    "LIN":   {"name": "Linde",              "sector": "Materials",               "mkt_cap_b":  215},
    "APD":   {"name": "Air Products",       "sector": "Materials",               "mkt_cap_b":   63},
    "SHW":   {"name": "Sherwin-Williams",   "sector": "Materials",               "mkt_cap_b":   85},
    "ECL":   {"name": "Ecolab",             "sector": "Materials",               "mkt_cap_b":   65},
    "FCX":   {"name": "Freeport-McMoRan",   "sector": "Materials",               "mkt_cap_b":   55},
    "NEM":   {"name": "Newmont",            "sector": "Materials",               "mkt_cap_b":   55},

    # ── Real Estate (8) ──────────────────────────────────────────────────
    "AMT":   {"name": "American Tower",     "sector": "Real Estate",             "mkt_cap_b":   90},
    "PLD":   {"name": "Prologis",           "sector": "Real Estate",             "mkt_cap_b":  105},
    "EQIX":  {"name": "Equinix",            "sector": "Real Estate",             "mkt_cap_b":   78},
    "CCI":   {"name": "Crown Castle",       "sector": "Real Estate",             "mkt_cap_b":   45},
    "PSA":   {"name": "Public Storage",     "sector": "Real Estate",             "mkt_cap_b":   55},
    "O":     {"name": "Realty Income",      "sector": "Real Estate",             "mkt_cap_b":   48},
    "SPG":   {"name": "Simon Property",     "sector": "Real Estate",             "mkt_cap_b":   60},
    "DLR":   {"name": "Digital Realty",     "sector": "Real Estate",             "mkt_cap_b":   55},

    # ── Utilities (7) ────────────────────────────────────────────────────
    "NEE":   {"name": "NextEra Energy",     "sector": "Utilities",               "mkt_cap_b":  145},
    "DUK":   {"name": "Duke Energy",        "sector": "Utilities",               "mkt_cap_b":   85},
    "SO":    {"name": "Southern Company",   "sector": "Utilities",               "mkt_cap_b":   90},
    "D":     {"name": "Dominion Energy",    "sector": "Utilities",               "mkt_cap_b":   47},
    "AEP":   {"name": "American Electric",  "sector": "Utilities",               "mkt_cap_b":   52},
    "EXC":   {"name": "Exelon",             "sector": "Utilities",               "mkt_cap_b":   38},
    "SRE":   {"name": "Sempra",             "sector": "Utilities",               "mkt_cap_b":   48},
}

# Sector order for display (largest sectors first)
SECTOR_ORDER = [
    "Technology",
    "Communication Services",
    "Consumer Discretionary",
    "Financials",
    "Healthcare",
    "Industrials",
    "Consumer Staples",
    "Energy",
    "Real Estate",
    "Utilities",
    "Materials",
]
