"""
ystocker.gics
~~~~~~~~~~~~~
S&P 500 performance by GICS sector *and industry group*, built from the
constituents rather than read off sector ETFs.

Why this exists
---------------
/markets already shows sector performance twice, and both are ETF prices: the
Select Sector SPDRs for day/week, and the same eleven against SPY for the
rotation grid. That answers "which sector" and stops there. Rotation most weeks
happens a level down — semiconductors against software inside Information
Technology, banks against insurers inside Financials, pharma against managed
care inside Health Care — and an eleven-ETF view cannot show it, because each
ETF averages the two halves together. GICS has 25 industry groups under the 11
sectors, and no ETF tracks most of them on the S&P 500's own definition (the
SPDR industry funds — XBI, KRE, XRT — are equal-weighted and draw on the wider
S&P Total Market), so they are computed here.

It also sidesteps something the ETFs get wrong by design: the Select Sector
SPDRs *cap* single names, and three companies are over half of S&P 500 IT, so
XLK is not the S&P 500 IT sector. What is computed here is — uncapped — so the
two can legitimately disagree, and the page says why rather than leaving the
reader to decide which is broken.

Yahoo does list S&P's official GICS indices (``^SP500-45``, ``^SP500-4530``, …)
and they would have been the obvious source. Measured: 35 of the 36 symbols
resolve, but only 8 carry any history — the rest return today's quote alone,
so they cannot give a one-week return, never mind a year. Those 8 are used as
the oracle in ``tests/test_gics.py`` instead.

Cost
----
Prices cost **no network call**: they ride breadth.py's daily download, which
already fetches every S&P 500 constituent's history in one batched call, and
:func:`performance` does arithmetic on that frame.

The classification and the weights are **two small files a day** — Wikipedia's
constituent list and SPY's holdings — fetched by :func:`refresh_snapshot` at the
start of that same background build, through fetchguard, and saved to
``cache/gics_sp500.json``. Nobody has to run anything: a reconstitution or a
day's weight drift reaches the page, and breadth's universe, within a day. A
failed or refused refresh keeps the snapshot in force, and the refusals are
what make it safe unattended (see :func:`build_snapshot`) — the two sources are
independent, so a broken scrape would have to fail both identically to pass.

``ystocker/data/gics_sp500.json`` is the committed baseline: what a fresh box
starts from until its first refresh, and what the tests check.
:func:`write_snapshot` moves it; that is optional upkeep, not a chore the page
depends on.

Method
------
Each member's snapshot weight ``w_i`` (from SPY's holdings file, which
replicates the index) is turned into a constant share count against the
adjusted close on the snapshot date, ``q_i = w_i / P_i(S)``. A group's value on
any day is then ``Σ q_i·P_i(d)``: its return is the ratio of two such sums and
its index weight is its share of the total. That is buy-and-hold with constant
shares, which is what a cap-weighted index is between rebalances.

``P_i(S)`` is deliberately read from the *same* download rather than taken from
the holdings file's own price. Yahoo re-bases an adjusted series after every
split, dividend and spin-off, so only a ratio of two points from one series is
invariant to those; pairing a raw price with an adjusted one would misweight
every name that has split since the snapshot, by exactly the split ratio.
Spin-offs are handled by that same re-basing — checked on DD→Q, FDX→FDXF and
HON→HONA, none of which shows a cliff on the distribution date.

**Every horizon counts only companies already in the index at its start**
(Wikipedia's "Date added"). This is the part that matters. Backcasting today's
membership is the usual shortcut, and it is biased *upward*, because names are
added to the S&P 500 after they have risen: measured against the official
index over the year to 2026-09-25, the shortcut read +18.37% for the S&P 500
against an official +17.24%, and date-gating reads +17.31%. For Semiconductors
it closes 58.0 → 56.6 against 55.8. A company that joins mid-window is left out
of that window rather than credited with gains it made outside the index.

What remains is honest drift, not error in the arithmetic:

* Share counts are held at the snapshot's. Buybacks and float changes move
  them a few percent a year at most; regenerate a few times a year.
* Removed members are absent from history (survivorship, as in breadth.py).
* A small group can be off by more than a point, because one disputed
  classification is a large share of it — Telecommunication Services is four
  names, and EchoStar alone accounts for its gap to the official index. So the
  constituent count travels with every row and the page shows it.
* Returns are **total** returns (breadth downloads adjusted closes), matching
  the rotation grid's ETFs; S&P's headline indices are price-only, so a
  high-yield sector reads above them by its dividend over the period.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ystocker import periods

log = logging.getLogger(__name__)

SNAPSHOT_FILE = Path(__file__).parent / "data" / "gics_sp500.json"
SNAPSHOT_SCHEMA = 1

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HOLDINGS_URL = ("https://www.ssga.com/us/en/intermediary/etfs/library-content/"
                "products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx")

# Where each period starts is ystocker/periods.py's, shared with the rotation
# grid and ranking beside this panel so the three agree by construction.
PERIODS: tuple[str, ...] = periods.PERIODS

# The share of SPY's holdings weight the constituent list must account for
# before a snapshot is accepted. The two sources are independent, so a table
# that lost rows cannot pass by also losing their weights.
MIN_WEIGHT_COVERAGE = 0.99

# A day is only used as an end point when this share of snapshot weight has a
# close on it. Yahoo occasionally omits the newest print for a handful of names,
# and a sector return that quietly lost NVDA on its last day is a different
# number that reads as the same one.
MIN_END_COVERAGE = 0.95
# Weaker floor for a *base* date: a stray row carrying three tickers must not be
# chosen as the start of a window, but a normal session with a couple of gaps is
# fine — the missing names simply sit that horizon out.
MIN_BASE_COVERAGE = 0.50

# The session's closing bar is final by ~16:15 ET; a build before this reads
# the current price into a bar that looks like a close.
_SESSION_FINAL_ET = (16, 30)


# ---------------------------------------------------------------------------
# The GICS hierarchy (structure effective 17 March 2023)
# ---------------------------------------------------------------------------

SECTORS: dict[str, str] = {
    "10": "Energy",
    "15": "Materials",
    "20": "Industrials",
    "25": "Consumer Discretionary",
    "30": "Consumer Staples",
    "35": "Health Care",
    "40": "Financials",
    "45": "Information Technology",
    "50": "Communication Services",
    "55": "Utilities",
    "60": "Real Estate",
}

# The sector is the code's first two digits, so it is not repeated here.
INDUSTRY_GROUPS: dict[str, str] = {
    "1010": "Energy",
    "1510": "Materials",
    "2010": "Capital Goods",
    "2020": "Commercial & Professional Services",
    "2030": "Transportation",
    "2510": "Automobiles & Components",
    "2520": "Consumer Durables & Apparel",
    "2530": "Consumer Services",
    "2550": "Consumer Discretionary Distribution & Retail",
    "3010": "Consumer Staples Distribution & Retail",
    "3020": "Food, Beverage & Tobacco",
    "3030": "Household & Personal Products",
    "3510": "Health Care Equipment & Services",
    "3520": "Pharmaceuticals, Biotechnology & Life Sciences",
    "4010": "Banks",
    "4020": "Financial Services",
    "4030": "Insurance",
    "4510": "Software & Services",
    "4520": "Technology Hardware & Equipment",
    "4530": "Semiconductors & Semiconductor Equipment",
    "5010": "Telecommunication Services",
    "5020": "Media & Entertainment",
    "5510": "Utilities",
    "6010": "Equity Real Estate Investment Trusts (REITs)",
    "6020": "Real Estate Management & Development",
}

# All 163 sub-industries, not just the ~126 the index happens to hold today, so
# a reconstitution that brings in a new sub-industry does not fail the rebuild.
SUB_INDUSTRY_GROUP: dict[str, str] = {
    # 1010 Energy
    "Oil & Gas Drilling": "1010",
    "Oil & Gas Equipment & Services": "1010",
    "Integrated Oil & Gas": "1010",
    "Oil & Gas Exploration & Production": "1010",
    "Oil & Gas Refining & Marketing": "1010",
    "Oil & Gas Storage & Transportation": "1010",
    "Coal & Consumable Fuels": "1010",
    # 1510 Materials
    "Commodity Chemicals": "1510",
    "Diversified Chemicals": "1510",
    "Fertilizers & Agricultural Chemicals": "1510",
    "Industrial Gases": "1510",
    "Specialty Chemicals": "1510",
    "Construction Materials": "1510",
    "Metal, Glass & Plastic Containers": "1510",
    "Paper & Plastic Packaging Products & Materials": "1510",
    "Aluminum": "1510",
    "Diversified Metals & Mining": "1510",
    "Copper": "1510",
    "Gold": "1510",
    "Precious Metals & Minerals": "1510",
    "Silver": "1510",
    "Steel": "1510",
    "Forest Products": "1510",
    "Paper Products": "1510",
    # 2010 Capital Goods
    "Aerospace & Defense": "2010",
    "Building Products": "2010",
    "Construction & Engineering": "2010",
    "Electrical Components & Equipment": "2010",
    "Heavy Electrical Equipment": "2010",
    "Industrial Conglomerates": "2010",
    "Construction Machinery & Heavy Transportation Equipment": "2010",
    "Agricultural & Farm Machinery": "2010",
    "Industrial Machinery & Supplies & Components": "2010",
    "Trading Companies & Distributors": "2010",
    # 2020 Commercial & Professional Services
    "Commercial Printing": "2020",
    "Environmental & Facilities Services": "2020",
    "Office Services & Supplies": "2020",
    "Diversified Support Services": "2020",
    "Security & Alarm Services": "2020",
    "Human Resource & Employment Services": "2020",
    "Research & Consulting Services": "2020",
    "Data Processing & Outsourced Services": "2020",
    # 2030 Transportation
    "Air Freight & Logistics": "2030",
    "Passenger Airlines": "2030",
    "Marine Transportation": "2030",
    "Rail Transportation": "2030",
    "Cargo Ground Transportation": "2030",
    "Passenger Ground Transportation": "2030",
    "Airport Services": "2030",
    "Highways & Railtracks": "2030",
    "Marine Ports & Services": "2030",
    # 2510 Automobiles & Components
    "Automotive Parts & Equipment": "2510",
    "Tires & Rubber": "2510",
    "Automobile Manufacturers": "2510",
    "Motorcycle Manufacturers": "2510",
    # 2520 Consumer Durables & Apparel
    "Consumer Electronics": "2520",
    "Home Furnishings": "2520",
    "Homebuilding": "2520",
    "Household Appliances": "2520",
    "Housewares & Specialties": "2520",
    "Leisure Products": "2520",
    "Apparel, Accessories & Luxury Goods": "2520",
    "Footwear": "2520",
    "Textiles": "2520",
    # 2530 Consumer Services
    "Casinos & Gaming": "2530",
    "Hotels, Resorts & Cruise Lines": "2530",
    "Leisure Facilities": "2530",
    "Restaurants": "2530",
    "Education Services": "2530",
    "Specialized Consumer Services": "2530",
    # 2550 Consumer Discretionary Distribution & Retail
    "Distributors": "2550",
    "Broadline Retail": "2550",
    "Apparel Retail": "2550",
    "Computer & Electronics Retail": "2550",
    "Home Improvement Retail": "2550",
    "Other Specialty Retail": "2550",
    "Automotive Retail": "2550",
    "Homefurnishing Retail": "2550",
    # 3010 Consumer Staples Distribution & Retail
    "Drug Retail": "3010",
    "Food Distributors": "3010",
    "Food Retail": "3010",
    "Consumer Staples Merchandise Retail": "3010",
    # 3020 Food, Beverage & Tobacco
    "Brewers": "3020",
    "Distillers & Vintners": "3020",
    "Soft Drinks & Non-alcoholic Beverages": "3020",
    "Agricultural Products & Services": "3020",
    "Packaged Foods & Meats": "3020",
    "Tobacco": "3020",
    # 3030 Household & Personal Products
    "Household Products": "3030",
    "Personal Care Products": "3030",
    # 3510 Health Care Equipment & Services
    "Health Care Equipment": "3510",
    "Health Care Supplies": "3510",
    "Health Care Distributors": "3510",
    "Health Care Services": "3510",
    "Health Care Facilities": "3510",
    "Managed Health Care": "3510",
    "Health Care Technology": "3510",
    # 3520 Pharmaceuticals, Biotechnology & Life Sciences
    "Biotechnology": "3520",
    "Pharmaceuticals": "3520",
    "Life Sciences Tools & Services": "3520",
    # 4010 Banks
    "Diversified Banks": "4010",
    "Regional Banks": "4010",
    # 4020 Financial Services
    "Diversified Financial Services": "4020",
    "Multi-Sector Holdings": "4020",
    "Specialized Finance": "4020",
    "Commercial & Residential Mortgage Finance": "4020",
    "Transaction & Payment Processing Services": "4020",
    "Consumer Finance": "4020",
    "Asset Management & Custody Banks": "4020",
    "Investment Banking & Brokerage": "4020",
    "Diversified Capital Markets": "4020",
    "Financial Exchanges & Data": "4020",
    "Mortgage REITs": "4020",
    # 4030 Insurance
    "Insurance Brokers": "4030",
    "Life & Health Insurance": "4030",
    "Multi-line Insurance": "4030",
    "Property & Casualty Insurance": "4030",
    "Reinsurance": "4030",
    # 4510 Software & Services
    "IT Consulting & Other Services": "4510",
    "Internet Services & Infrastructure": "4510",
    "Application Software": "4510",
    "Systems Software": "4510",
    # 4520 Technology Hardware & Equipment
    "Communications Equipment": "4520",
    "Technology Hardware, Storage & Peripherals": "4520",
    "Electronic Equipment & Instruments": "4520",
    "Electronic Components": "4520",
    "Electronic Manufacturing Services": "4520",
    "Technology Distributors": "4520",
    # 4530 Semiconductors & Semiconductor Equipment
    "Semiconductor Materials & Equipment": "4530",
    "Semiconductors": "4530",
    # 5010 Telecommunication Services
    "Alternative Carriers": "5010",
    "Integrated Telecommunication Services": "5010",
    "Wireless Telecommunication Services": "5010",
    # 5020 Media & Entertainment
    "Advertising": "5020",
    "Broadcasting": "5020",
    "Cable & Satellite": "5020",
    "Publishing": "5020",
    "Movies & Entertainment": "5020",
    "Interactive Home Entertainment": "5020",
    "Interactive Media & Services": "5020",
    # 5510 Utilities
    "Electric Utilities": "5510",
    "Gas Utilities": "5510",
    "Multi-Utilities": "5510",
    "Water Utilities": "5510",
    "Independent Power Producers & Energy Traders": "5510",
    "Renewable Electricity": "5510",
    # 6010 Equity Real Estate Investment Trusts (REITs)
    "Diversified REITs": "6010",
    "Industrial REITs": "6010",
    "Hotel & Resort REITs": "6010",
    "Office REITs": "6010",
    "Health Care REITs": "6010",
    "Multi-Family Residential REITs": "6010",
    "Single-Family Residential REITs": "6010",
    "Retail REITs": "6010",
    "Other Specialized REITs": "6010",
    "Self-Storage REITs": "6010",
    "Telecom Tower REITs": "6010",
    "Timber REITs": "6010",
    "Data Center REITs": "6010",
    # 6020 Real Estate Management & Development
    "Diversified Real Estate Activities": "6020",
    "Real Estate Operating Companies": "6020",
    "Real Estate Development": "6020",
    "Real Estate Services": "6020",
}


def _norm(label: str) -> str:
    """Case, punctuation and '&'-versus-'and' insensitive key for a GICS name.

    Wikipedia is edited by hand, and "Multi-line" / "Multi-Line" or "&" / "and"
    drift between revisions. An exact-match lookup would turn that into a failed
    rebuild over a capital letter.
    """
    s = label.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_SUB_BY_NORM = {_norm(k): v for k, v in SUB_INDUSTRY_GROUP.items()}
_SECTOR_BY_NORM = {_norm(v): k for k, v in SECTORS.items()}


def group_of(sub_industry: str) -> Optional[str]:
    """Industry-group code for a sub-industry name, or None if unrecognised."""
    return _SUB_BY_NORM.get(_norm(sub_industry or ""))


def sector_of_name(sector: str) -> Optional[str]:
    """Sector code for a sector name ("Information Technology" -> "45")."""
    return _SECTOR_BY_NORM.get(_norm(sector or ""))


def yahoo_symbol(symbol: str) -> str:
    """Index-list ticker to Yahoo's form: BRK.B -> BRK-B."""
    return symbol.strip().upper().replace(".", "-")


# ---------------------------------------------------------------------------
# Snapshot: build (developer command), load (runtime)
# ---------------------------------------------------------------------------

def build_snapshot(members: list[dict[str, str]], weights: dict[str, float],
                   *, weights_asof: str, members_asof: str) -> dict[str, Any]:
    """Join the classification to the weights. Pure; raises on anything unknown.

    ``members`` rows carry ``symbol``, ``sector``, ``sub_industry`` and
    ``added``; ``weights`` is keyed by Yahoo symbol. Refuses rather than guesses
    in all three failure modes, because each one would otherwise produce a
    plausible-looking table with a row quietly wrong: an unrecognised
    sub-industry (a renamed GICS level), a sector that disagrees with the one
    the sub-industry implies (a mis-edit on the source page, or a mapping
    error here), and a member with no weight.
    """
    out: dict[str, dict[str, Any]] = {}
    unknown, mismatched, unweighted = [], [], []
    for row in members:
        sym = yahoo_symbol(row["symbol"])
        grp = group_of(row["sub_industry"])
        if grp is None:
            unknown.append(f"{sym}: {row['sub_industry']!r}")
            continue
        if sector_of_name(row["sector"]) != grp[:2]:
            mismatched.append(f"{sym}: {row['sector']!r} vs {row['sub_industry']!r}")
            continue
        w = weights.get(sym)
        if w is None or not math.isfinite(w) or w <= 0:
            unweighted.append(sym)
            continue
        added = str(row.get("added") or "").strip()
        out[sym] = {
            "sector": SECTORS[grp[:2]],
            "sub": row["sub_industry"].strip(),
            "added": added if re.fullmatch(r"\d{4}-\d{2}-\d{2}", added) else None,
            "w": round(float(w), 6),
        }
    problems = []
    if unknown:
        problems.append("unrecognised sub-industry: " + "; ".join(unknown))
    if mismatched:
        problems.append("sector disagrees with sub-industry: " + "; ".join(mismatched))
    if unweighted:
        problems.append("no SPY weight: " + ", ".join(unweighted))
    if problems:
        raise ValueError(" | ".join(problems))
    if len(out) < 450:
        # The page has ~503. A scrape that found 60 rows is a changed page
        # layout, not a smaller index, and committing it would shrink every
        # sector to whichever names happened to parse.
        raise ValueError(f"only {len(out)} members parsed — refusing a partial snapshot")
    # And against the *other* source. A table that lost thirty rows still
    # clears 450, but SPY holds the whole index, so the members must account
    # for nearly all of its weight — this is the check that makes an unattended
    # refresh safe, because the two sources would have to fail identically.
    total = sum(v for v in weights.values() if math.isfinite(v) and v > 0)
    covered = sum(m["w"] for m in out.values())
    if total > 0 and covered / total < MIN_WEIGHT_COVERAGE:
        raise ValueError(f"members cover only {covered / total:.1%} of SPY's weight — "
                         "refusing a partial snapshot")
    return {
        "schema": SNAPSHOT_SCHEMA,
        "weights_asof": weights_asof,
        "members_asof": members_asof,
        "sources": {"members": WIKI_URL, "weights": HOLDINGS_URL},
        "members": dict(sorted(out.items())),
    }


def _parse_wikipedia(html: str) -> list[dict[str, str]]:
    import io

    import pandas as pd

    table = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    need = {"Symbol", "GICS Sector", "GICS Sub-Industry", "Date added"}
    if not need.issubset(table.columns):
        raise ValueError(f"constituents table changed shape: {list(table.columns)}")
    return [{"symbol": str(r["Symbol"]), "sector": str(r["GICS Sector"]),
             "sub_industry": str(r["GICS Sub-Industry"]), "added": str(r["Date added"])}
            for _, r in table.iterrows()]


_XLSX_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _xlsx_column(ref: str) -> int:
    """'B7' -> 1. Letters only; the row number is read from the <row> itself."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n - 1


def _xlsx_rows(xlsx: bytes) -> list[list[Any]]:
    """The first worksheet of an .xlsx as rows of cell values, without openpyxl.

    ``pandas.read_excel`` goes through openpyxl, which reads every part with the
    standard library's ElementTree — and ElementTree's parser is ``pyexpat``.
    On this repo's dev Mac that extension cannot be loaded at all: Homebrew's
    Python 3.12.14 was built against a newer libexpat than the
    ``/usr/lib/libexpat.1.dylib`` it finds at runtime (``Symbol not found:
    _XML_SetAllocTrackerActivationThreshold``), the same breakage CLAUDE.md
    records under matplotlib. So the snapshot command fetched both sources and
    then died reading the one spreadsheet.

    An .xlsx is a zip of XML parts, and lxml — already required for the
    Wikipedia table through ``read_html``, and carrying its own libxml2 — reads
    them without expat. Row positions follow each ``<row r=…>``, so a blank row
    in the preamble stays a blank row, as it does under ``read_excel(header=None)``.
    """
    import io
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in etree.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_XLSX_MAIN}si"):
                # Rich text splits one string into runs; the phonetic guide
                # (<rPh>) is annotation, not text, and is left out.
                shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_MAIN}t")
                                      if t.getparent().tag != f"{_XLSX_MAIN}rPh"))
        # The first sheet by the workbook's own order, via its relationship —
        # not "sheet1.xml" by name, which is only a writer's habit.
        path = "xl/worksheets/sheet1.xml"
        try:
            first = etree.fromstring(z.read("xl/workbook.xml")).find(f".//{_XLSX_MAIN}sheet")
            rid = first.get(f"{_XLSX_REL}id")
            for rel in etree.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                if rel.get("Id") == rid:
                    target = rel.get("Target", "")
                    path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        except (KeyError, AttributeError, etree.XMLSyntaxError):
            pass
        sheet = etree.fromstring(z.read(path))

    rows: list[list[Any]] = []
    for row in sheet.iter(f"{_XLSX_MAIN}row"):
        r = int(row.get("r") or len(rows) + 1) - 1
        while len(rows) < r:
            rows.append([])
        cells: dict[int, Any] = {}
        col = -1
        for c in row.iter(f"{_XLSX_MAIN}c"):
            col = _xlsx_column(c.get("r")) if c.get("r") else col + 1
            kind, v = c.get("t"), c.findtext(f"{_XLSX_MAIN}v")
            if kind == "s":
                cells[col] = shared[int(v)] if v is not None else None
            elif kind == "inlineStr":
                cells[col] = "".join(t.text or "" for t in c.iter(f"{_XLSX_MAIN}t"))
            elif kind in ("str", "e"):
                cells[col] = v
            elif kind == "b":
                cells[col] = v == "1"
            else:
                cells[col] = float(v) if v not in (None, "") else None
        rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return rows


def _parse_holdings(xlsx: bytes) -> tuple[str, dict[str, float]]:
    """SPY's daily holdings file -> (as-of ISO date, {yahoo symbol: weight %})."""
    rows = _xlsx_rows(xlsx)
    cell = lambda row, i: row[i] if i < len(row) else None  # noqa: E731
    asof = None
    for row in rows[:6]:
        m = re.search(r"As of (\d{1,2}-[A-Za-z]{3}-\d{4})", str(cell(row, 1) or ""))
        if m:
            asof = datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
    hdr = next((i for i, row in enumerate(rows)
                if cell(row, 0) == "Name" and cell(row, 1) == "Ticker"), None)
    if asof is None or hdr is None or "Weight" not in rows[hdr]:
        raise ValueError("SPY holdings file changed shape (no as-of date or header row)")
    ti, wi = rows[hdr].index("Ticker"), rows[hdr].index("Weight")
    weights: dict[str, float] = {}
    for row in rows[hdr + 1:]:
        tick, w = cell(row, ti), cell(row, wi)
        try:
            weights[yahoo_symbol(str(tick))] = float(w)
        except (TypeError, ValueError):
            continue
    return asof, weights


class SnapshotFetchError(RuntimeError):
    """A source for the snapshot could not be reached."""


# The snapshot the app reads is the box's own refresh when it has one, and the
# committed file otherwise — a fresh box, a refresh that has never succeeded, or
# a cached copy that fails to load. The committed file is only a baseline now.
CACHE_SNAPSHOT_FILE = Path(__file__).parent.parent / "cache" / "gics_sp500.json"
# Once a day is what the sources change at: SPY's holdings file is published
# each session, and the constituent list moves a handful of names a quarter.
# Twenty hours rather than twenty-four so a build that lands a little early on
# its daily timer does not skip a day.
REFRESH_AFTER_SECONDS = 20 * 3600
# Kill switch, as elsewhere in this app: 0 pins the app to whatever snapshot it
# already has (the cached refresh, else the committed file).
REFRESH_ENABLED = os.environ.get("GICS_SNAPSHOT_REFRESH", "1") != "0"

_UA = "Mozilla/5.0 (ystocker gics snapshot)"


def _fetch(provider: str, url: str) -> bytes:
    """One source file, through fetchguard like every other vendor call."""
    import requests

    from ystocker import fetchguard

    try:
        return fetchguard.request(provider, url, timeout=60, headers={"User-Agent": _UA}).content
    except (fetchguard.CooldownActive, requests.RequestException) as exc:
        raise SnapshotFetchError(
            f"could not fetch {url} ({exc}). If this machine cannot reach it, "
            "run `bash deploy/gics-snapshot.sh` to regenerate the snapshot on the box."
        ) from exc


def fetch_snapshot() -> dict[str, Any]:
    """Build a snapshot from the live sources. Raises; writes nothing."""
    members = _parse_wikipedia(_fetch("wikipedia", WIKI_URL).decode("utf-8"))
    weights_asof, weights = _parse_holdings(_fetch("ssga", HOLDINGS_URL))
    return build_snapshot(members, weights, weights_asof=weights_asof,
                          members_asof=date.today().isoformat())


def _log_membership(before: Optional[dict[str, Any]], after: dict[str, Any]) -> None:
    if before:
        old, new = set(before["members"]), set(after["members"])
        log.info("GICS: joined %s; left %s", sorted(new - old) or "none", sorted(old - new) or "none")


def write_snapshot(out: Path = SNAPSHOT_FILE) -> Path:
    """Fetch the sources and rewrite the *committed* baseline.

    The box refreshes its own copy daily (:func:`refresh_snapshot`), so this is
    no longer something that has to be run: it only moves the baseline a fresh
    box starts from, and the one the tests check. Worth doing a few times a year
    so that baseline does not drift far from the index:

        ./venv/bin/python -m ystocker.gics --write-snapshot

    With the repo's venv, not whatever ``python`` is first on the path:
    ``-m`` imports the ``ystocker`` package before this module, and the
    package's ``__init__`` is the Flask app factory, so an interpreter without
    Flask fails on ``from flask import Flask`` before a line of this runs.

    It needs en.wikipedia.org and www.ssga.com. Where those are blocked — a
    sandboxed session, a restrictive proxy — ``bash deploy/gics-snapshot.sh``
    runs this same command on the box and brings the file back.
    """
    snap = fetch_snapshot()
    before = load_snapshot(out) if out.exists() else None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps_snapshot(snap))
    log.info("GICS: snapshot written to %s (%d members, weights as of %s)",
             out, len(snap["members"]), snap["weights_asof"])
    _log_membership(before, snap)
    return out


def dumps_snapshot(snap: dict[str, Any]) -> str:
    """One member per line, so a regeneration diffs as the names that changed."""
    head = {k: v for k, v in snap.items() if k != "members"}
    lines = [json.dumps(head, indent=2)[:-2] + ',\n  "members": {']
    items = list(snap["members"].items())
    for i, (sym, rec) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"    {json.dumps(sym)}: {json.dumps(rec, ensure_ascii=False)}{comma}")
    lines.append("  }\n}\n")
    return "\n".join(lines)


_snapshot_cache: Optional[dict[str, Any]] = None
_snapshot_lock = threading.Lock()


def _read_snapshot(path: Path, *, quiet_missing: bool = False) -> Optional[dict[str, Any]]:
    try:
        snap = json.loads(path.read_text())
    except FileNotFoundError:
        if not quiet_missing:
            log.warning("GICS: no snapshot at %s", path)
        return None
    except (OSError, ValueError) as exc:
        log.warning("GICS: snapshot unreadable at %s: %s", path, exc)
        return None
    # The same floor build_snapshot applies, so a cached file that was cut
    # short on disk cannot be preferred over the committed one.
    if snap.get("schema") != SNAPSHOT_SCHEMA or len(snap.get("members") or {}) < 450:
        log.warning("GICS: snapshot at %s is not usable (schema %r, %d members) — ignoring",
                    path, snap.get("schema"), len(snap.get("members") or {}))
        return None
    return snap


def load_snapshot(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The snapshot in force, or exactly the file at ``path`` when one is given.

    In force means the box's own daily refresh when a usable one exists,
    falling back to the committed baseline. None only when neither loads.
    """
    global _snapshot_cache
    if path is not None:
        return _read_snapshot(path)
    with _snapshot_lock:
        if _snapshot_cache is not None:
            return _snapshot_cache
    snap = _read_snapshot(CACHE_SNAPSHOT_FILE, quiet_missing=True) or _read_snapshot(SNAPSHOT_FILE)
    if snap is not None:
        with _snapshot_lock:
            _snapshot_cache = snap
    return snap


def refresh_snapshot(*, max_age: float = REFRESH_AFTER_SECONDS) -> Optional[dict[str, Any]]:
    """Rebuild the snapshot from the live sources if the box's copy is old.

    Called from breadth's daily build, before its download, so the prices it
    fetches already cover today's members. Returns the snapshot in force
    afterwards and **never raises**: every way it can fail keeps the last good
    snapshot, because a day-old membership list is harmless and a missing one
    empties every sector. What guards against a *wrong* refresh is
    build_snapshot's refusals — an unknown sub-industry, a sector that
    disagrees with it, a member SPY does not hold, fewer than 450 names, or
    members covering under 99% of SPY's weight.

    Two small files a day, both through fetchguard's breaker.
    """
    global _snapshot_cache
    before = load_snapshot()
    if not REFRESH_ENABLED:
        return before
    try:
        age = time.time() - CACHE_SNAPSHOT_FILE.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age < max_age:
        # The file, not `before`: the copy in memory can be older than the one
        # on disk when another process wrote it (a refresh run by hand, say),
        # and returning memory would keep this process on it for good.
        current = _read_snapshot(CACHE_SNAPSHOT_FILE, quiet_missing=True)
        if current:
            with _snapshot_lock:
                _snapshot_cache = current
            return current
    try:
        snap = fetch_snapshot()
    except (SnapshotFetchError, ValueError) as exc:
        log.warning("GICS: snapshot refresh failed, keeping the one from %s: %s",
                    (before or {}).get("members_asof"), exc)
        return before
    try:
        CACHE_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_SNAPSHOT_FILE.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(dumps_snapshot(snap))
        os.replace(tmp, CACHE_SNAPSHOT_FILE)
    except OSError as exc:
        # Still use it this once; the next build will try the write again.
        log.warning("GICS: could not save the refreshed snapshot: %s", exc)
    with _snapshot_lock:
        _snapshot_cache = snap
    log.info("GICS: snapshot refreshed (%d members, weights as of %s)",
             len(snap["members"]), snap["weights_asof"])
    _log_membership(before, snap)
    return snap


def tickers(snap: dict[str, Any]) -> list[str]:
    """Every symbol the computation needs a price series for."""
    return list(snap.get("members") or {})


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------

def _session_is_final(last_bar: date, now: Optional[float]) -> bool:
    """Whether a bar dated ``last_bar`` is a closing price at time ``now``.

    breadth.py rebuilds on a 24h timer that starts at process boot, so a build
    can land mid-session and receive today's bar at the current price. Every
    other column would then silently mix a close with a quote. Anything dated
    before today in New York is final; today's is final only after the close.
    """
    if now is None:
        return True
    from zoneinfo import ZoneInfo

    ny = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
    if last_bar < ny.date():
        return True
    return last_bar == ny.date() and (ny.hour, ny.minute) >= _SESSION_FINAL_ET


def _r(x: float, digits: int = 2) -> Optional[float]:
    return round(float(x), digits) if x is not None and math.isfinite(x) else None


def performance(closes: Any, snap: dict[str, Any], *, now: Optional[float] = None,
                until: Any = None) -> dict[str, Any]:
    """Cap-weighted GICS sector and industry-group returns and weights.

    ``closes`` is a DataFrame of adjusted daily closes, one column per Yahoo
    symbol, as breadth.py downloads it. ``now`` (epoch seconds) is used only to
    decide whether the newest bar is a finished session; pass None to trust it.
    ``until`` ends on the last usable session on or before that date instead of
    the newest — see :func:`trail`. Returns a JSON-ready dict; see the module
    docstring for the method.
    """
    import pandas as pd

    members: dict[str, dict[str, Any]] = snap["members"]
    cols = [t for t in members if t in closes.columns]
    px = closes[cols].sort_index()
    px = px[~px.index.duplicated(keep="last")]
    px.index = pd.DatetimeIndex(px.index).tz_localize(None).normalize()

    w_snap = pd.Series({t: float(members[t]["w"]) for t in cols})
    total_w = float(sum(float(m["w"]) for m in members.values()))

    # Constant share counts from the snapshot, against the same adjusted series
    # the returns are read from. Nothing on or before the snapshot date (a name
    # that has since listed, or a gap) leaves the name out rather than pricing
    # it off a different day.
    snap_day = pd.Timestamp(snap["weights_asof"])
    anchor = px.loc[:snap_day].ffill().iloc[-1] if (px.index <= snap_day).any() else pd.Series(dtype=float)
    anchor = anchor.reindex(cols)
    q = (w_snap / anchor).replace([math.inf, -math.inf], math.nan).dropna()
    q = q[q > 0]

    # Coverage by snapshot weight, per day, to pick the end and base dates.
    have = px[q.index].notna()
    cover = (have * w_snap[q.index]).sum(axis=1) / total_w

    days = cover.index[cover >= MIN_BASE_COVERAGE]
    partial_dropped = False
    if len(days) and not _session_is_final(days[-1].date(), now):
        days = days[:-1]
        partial_dropped = True
    # A past end is a shorter list of sessions, never a shorter frame: slicing
    # `closes` would move the anchor above to the new last row, re-weighting
    # every name as though the snapshot had been taken that day.
    if until is not None:
        days = days[days <= pd.Timestamp(until)]
    ends = [d for d in days if cover[d] >= MIN_END_COVERAGE]
    if not ends:
        raise ValueError("no session with enough constituent coverage to end on")
    end = ends[-1]
    days = days[days <= end]

    # Among usable sessions only, so a window never starts on a stray thin row.
    bases: dict[str, Any] = {p: periods.base_date(days, end, p) for p in PERIODS}

    group = pd.Series({t: group_of(members[t]["sub"]) for t in q.index})
    sector = group.str[:2]
    added = pd.to_datetime(pd.Series({t: members[t].get("added") for t in q.index}), errors="coerce")

    p_end = px.loc[end, q.index]
    live = q.index[p_end.notna()]
    v_now = q[live] * p_end[live]
    tot_now = float(v_now.sum())
    g_now = v_now.groupby(group[live]).sum()
    s_now = v_now.groupby(sector[live]).sum()
    g_n = group[live].value_counts()
    s_n = sector[live].value_counts()

    # Per horizon: participants, their values at both ends, per group and sector.
    per: dict[str, dict[str, Any]] = {}
    excluded_new: dict[str, int] = {}
    for p, d0 in bases.items():
        if d0 is None:
            continue
        p0 = px.loc[d0, q.index]
        ok = p0.notna() & p_end.notna()
        was_member = added.isna() | (added <= d0)
        excluded_new[p] = int((ok & ~was_member).sum())
        ok = ok & was_member
        idx = q.index[ok]
        v0, v1 = q[idx] * p0[idx], q[idx] * p_end[idx]
        t0, t1 = float(v0.sum()), float(v1.sum())
        if t0 <= 0:
            continue
        per[p] = {
            "tot": (t0, t1),
            "g": (v0.groupby(group[idx]).sum(), v1.groupby(group[idx]).sum()),
            "s": (v0.groupby(sector[idx]).sum(), v1.groupby(sector[idx]).sum()),
        }

    def _series(level: str, code: str) -> dict[str, dict[str, Optional[float]]]:
        ret, rel, wchg = {}, {}, {}
        for p, blk in per.items():
            t0, t1 = blk["tot"]
            a0, a1 = blk[level]
            if code not in a0.index or a0[code] <= 0:
                ret[p] = rel[p] = wchg[p] = None
                continue
            r = a1[code] / a0[code] - 1
            ret[p] = _r(r * 100)
            rel[p] = _r((r - (t1 / t0 - 1)) * 100)
            wchg[p] = _r((a1[code] / t1 - a0[code] / t0) * 100, 3)
        return {"returns": ret, "rel": rel, "weight_chg": wchg}

    sectors_out = []
    for sc in sorted(s_now.index, key=lambda c: -s_now[c]):
        groups_out = []
        codes = [g for g in g_now.index if g[:2] == sc]
        for gc in sorted(codes, key=lambda c: -g_now[c]):
            names = v_now[group[live] == gc].sort_values(ascending=False)
            groups_out.append({
                "code": gc, "name": INDUSTRY_GROUPS[gc],
                "weight": _r(g_now[gc] / tot_now * 100),
                "n": int(g_n.get(gc, 0)),
                "top": [{"t": t, "w": _r(v / tot_now * 100)} for t, v in names.head(3).items()],
                **_series("g", gc),
            })
        sectors_out.append({
            "code": sc, "name": SECTORS[sc],
            "weight": _r(s_now[sc] / tot_now * 100),
            "n": int(s_n.get(sc, 0)),
            **_series("s", sc),
            "groups": groups_out,
        })

    index_ret = {p: _r((blk["tot"][1] / blk["tot"][0] - 1) * 100) for p, blk in per.items()}
    priced_w = float(w_snap[live].sum())
    return {
        "asof": end.date().isoformat(),
        "partial_dropped": partial_dropped,
        "weights_asof": snap["weights_asof"],
        "members_asof": snap.get("members_asof"),
        "periods": [p for p in PERIODS if p in per],
        "base_dates": {p: bases[p].date().isoformat() for p in per},
        "index": {"returns": index_ret},
        "sectors": sectors_out,
        "coverage": {
            "members": len(members),
            "priced": int(len(live)),
            "weight_pct": _r(priced_w / total_w * 100, 1),
            "missing": sorted(set(members) - set(live)),
            "excluded_new": excluded_new,
        },
    }


# The rotation map's trails: the windows its axes pair, and how far back a
# trail reaches — the latest close and the weekly close before it, so each
# group draws one line, from where it stood a week earlier into its bubble.
# Five weekly closes, the tail a weekly relative-rotation chart draws, were a
# tangle here: that chart plots smoothed ratios, these are raw window returns,
# which zigzag from week to week. On the 2026-09-25 close at 1M/3M the median
# group's five-week path ran 29 points, about half the width of the plot.
TRAIL_PERIODS: tuple[str, ...] = ("1W", "1M", "3M", "6M", "1Y")
TRAIL_POINTS = 2
TRAIL_STEP_DAYS = 7


def trail(closes: Any, snap: dict[str, Any], latest: dict[str, Any], *,
          points: int = TRAIL_POINTS, step_days: int = TRAIL_STEP_DAYS) -> dict[str, Any]:
    """Each industry group's relative returns at the last few weekly closes.

    ``latest`` is :func:`performance`'s result for the newest close and is the
    first point; each earlier one is :func:`performance` again with ``until`` a
    week further back. Same constituents, same share counts, same date-gating,
    so a trail moves only because prices did — and its head is exactly the
    table's figure. History that runs out ends the trail early rather than
    padding it, and so does a step with no usable close inside it: the map
    labels the point before the bubble "a week earlier", so a close from a
    week further back is left out rather than drawn there.
    """
    import pandas as pd

    end = pd.Timestamp(latest["asof"])
    results = [latest]
    for k in range(1, points):
        step = end - pd.Timedelta(days=step_days * k)
        try:
            res = performance(closes, snap, until=step)
        except ValueError:
            break
        # The last usable session on or before the step, but not a whole step
        # before it: past a gap that long, the close found belongs to an older
        # step, or is the one already in hand.
        if pd.Timestamp(res["asof"]) <= step - pd.Timedelta(days=step_days):
            break
        results.append(res)

    rel: dict[str, dict[str, list[Optional[float]]]] = {}
    for i, res in enumerate(results):
        for sec in res["sectors"]:
            for g in sec["groups"]:
                row = rel.setdefault(g["code"], {p: [None] * len(results) for p in TRAIL_PERIODS})
                for p in TRAIL_PERIODS:
                    row[p][i] = g["rel"].get(p)
    return {"dates": [r["asof"] for r in results], "step_days": step_days, "rel": rel}


if __name__ == "__main__":  # pragma: no cover - developer entry point
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--write-snapshot", action="store_true",
                    help="fetch Wikipedia + SPY holdings and rewrite the committed snapshot")
    ap.add_argument("--out", type=Path, default=SNAPSHOT_FILE)
    args = ap.parse_args()
    if args.write_snapshot:
        try:
            write_snapshot(args.out)
        except SnapshotFetchError as exc:
            raise SystemExit(f"GICS: {exc}") from None
    else:
        ap.print_help()
