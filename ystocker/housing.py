"""
ystocker.housing
~~~~~~~~~~~~~~~~
US housing-market data from Zillow Research and Redfin Data Center.

Both publishers put the data behind their Tableau Public dashboards
(public.tableau.com/app/profile/zillow.economic.research and .../redfin) into
plain public files, so this module reads the files directly rather than
scraping the vizzes.

Sources
-------
Zillow Research public CSVs (files.zillowstatic.com), metro-level, wide format
— one row per region, one column per month:
  ZHVI  home value index, smoothed + seasonally adjusted   (2000-01 → )
  ZORI  observed rent index, smoothed                      (2015-01 → )
  INVT  for-sale inventory                                 (2018-03 → )
  DOZ   median days to pending                             (2018-03 → )
  CUTS  share of listings with a price cut                  (2018-03 → )
  MSP   median sale price, smoothed + seasonally adjusted   (2018-08 → )
Row 0 of each file is the "country" aggregate; the rest are MSAs ordered by
SizeRank, which is how the top-metro table is picked.

Redfin Data Center (redfin-public-data.s3.us-west-2.amazonaws.com), national
monthly tracker, long format (2012-01 → ). Contributes the metrics Zillow does
not publish: months of supply, days on market, sold-above-list share, price
drops, pending sales, homes sold.

Two traps in these feeds
------------------------
1. Redfin ships TWO rows per month per property type — one seasonally adjusted
   and one not — and the values differ materially (2026-05: median sale price
   $440,411 SA vs $449,846 NSA; inventory 1.397M vs 1.460M). Filtering only on
   PROPERTY_TYPE duplicates every month and silently interleaves two different
   series, so `_parse_redfin` pins IS_SEASONALLY_ADJUSTED as well.

2. Redfin and Zillow price *levels* are not interchangeable. For 2026-05 Redfin
   reports a $440,411 median sale price against Zillow's $370,072 — a 19% gap,
   because the two cover different transaction universes (Redfin is MLS-centric
   and metro-weighted; Zillow's universe is broader). The same applies to
   Zillow's price-cut share (24.4%) versus Redfin's price-drops share (20.0%),
   which are different definitions rather than the same number measured twice.
   So the two are never plotted on one axis as if they agreed; each publisher
   owns the charts it is authoritative for, and housing.html labels the source
   on every card.

Deliberately NOT fetched
------------------------
Redfin's metro tracker is 111 MB gzipped and its weekly file is 1.6 GB. On a
4 GB box where ystocker already carries MemoryMax=1800M (see CLAUDE.md), either
one would OOM the worker and, because the kernel picks its OOM victim globally,
take the other seven apps down with it. Metro coverage therefore comes from
Zillow's 0.3–4.4 MB files, and everything here is parsed with the streaming
stdlib csv reader — never pandas, which would hold the whole frame plus its
index in memory for a payload we throw away after extracting a few columns.

Cache TTL: 24 hours. Both publishers update monthly, so this is about not
re-downloading ~10 MB per request, not about freshness.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).parent.parent / "cache" / "housing_cache.json"
_CACHE_TTL = 24 * 60 * 60  # 24 hours

# How many metros (by Zillow SizeRank) to carry in the comparison table.
_TOP_METROS = 25
# Months of per-metro history kept for the metro chart. The national series
# keep their full history; 25 metros x full history would bloat the cache for
# no benefit, since the metro view is a "recent trend" comparison.
_METRO_MONTHS = 120

_ZILLOW_BASE = "https://files.zillowstatic.com/research/public_csvs/"
_REDFIN_NATIONAL = (
    "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
    "redfin_market_tracker/us_national_market_tracker.tsv000.gz"
)
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Zillow metric key -> (path, label, unit). `unit` drives formatting in the
# template; "pct_dec" means the file stores 0.24 for 24%.
ZILLOW_SERIES: dict[str, dict[str, str]] = {
    "zhvi": {
        "path": "zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
        "label": "Typical Home Value (ZHVI)", "unit": "usd",
    },
    "zori": {
        "path": "zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
        "label": "Typical Asking Rent (ZORI)", "unit": "usd",
    },
    "inventory": {
        "path": "invt_fs/Metro_invt_fs_uc_sfrcondo_sm_month.csv",
        "label": "For-Sale Inventory", "unit": "count",
    },
    "days_to_pending": {
        "path": "med_doz_pending/Metro_med_doz_pending_uc_sfrcondo_sm_month.csv",
        "label": "Median Days to Pending", "unit": "days",
    },
    "price_cuts": {
        "path": "perc_listings_price_cut/Metro_perc_listings_price_cut_uc_sfrcondo_sm_month.csv",
        "label": "Share of Listings with a Price Cut", "unit": "pct_dec",
    },
    "median_sale_price": {
        "path": "median_sale_price/Metro_median_sale_price_uc_sfrcondo_sm_sa_month.csv",
        "label": "Median Sale Price", "unit": "usd",
    },
}

# Redfin columns kept, and whether the file stores a decimal share.
REDFIN_METRICS: dict[str, dict[str, Any]] = {
    "median_sale_price":  {"label": "Median Sale Price",       "unit": "usd"},
    "median_ppsf":        {"label": "Median $/sqft",           "unit": "usd"},
    "homes_sold":         {"label": "Homes Sold",              "unit": "count"},
    "pending_sales":      {"label": "Pending Sales",           "unit": "count"},
    "new_listings":       {"label": "New Listings",            "unit": "count"},
    "inventory":          {"label": "Inventory",               "unit": "count"},
    "months_of_supply":   {"label": "Months of Supply",        "unit": "months"},
    "median_dom":         {"label": "Median Days on Market",   "unit": "days"},
    "sold_above_list":    {"label": "Sold Above List",         "unit": "pct_dec"},
    "price_drops":        {"label": "Share with a Price Drop", "unit": "pct_dec"},
    "off_market_2wk":     {"label": "Off Market in 2 Weeks",   "unit": "pct_dec"},
}

# Redfin TSV column name for each key above.
_REDFIN_COLS = {
    "median_sale_price": "MEDIAN_SALE_PRICE",
    "median_ppsf":       "MEDIAN_PPSF",
    "homes_sold":        "HOMES_SOLD",
    "pending_sales":     "PENDING_SALES",
    "new_listings":      "NEW_LISTINGS",
    "inventory":         "INVENTORY",
    "months_of_supply":  "MONTHS_OF_SUPPLY",
    "median_dom":        "MEDIAN_DOM",
    "sold_above_list":   "SOLD_ABOVE_LIST",
    "price_drops":       "PRICE_DROPS",
    "off_market_2wk":    "OFF_MARKET_IN_TWO_WEEKS",
}

# ---------------------------------------------------------------------------
# FRED series — the rental, construction, mortgage and credit context that
# neither Zillow nor Redfin publishes.
#
# `keep` caps how many trailing observations reach the payload. Series whose
# whole point is the long view (Case-Shiller back to 1987, delinquencies across
# 2008) keep everything; the weekly mortgage series would otherwise ship ~2,900
# points to draw a line a few hundred pixels wide.
#
# Every id here was checked for a *current* last observation, not just a 200.
# Dead FRED series keep serving well-formed CSV for years — see the MBST /
# WASDRAL note in CLAUDE.md — so `_fetch_fred_series` re-checks staleness at
# runtime and drops anything that has gone quiet.
# ---------------------------------------------------------------------------
FRED_SERIES: dict[str, dict[str, Any]] = {
    # ── prices ──
    "case_shiller":  {"id": "CSUSHPINSA",    "label": "Case-Shiller US National Home Price Index",
                      "unit": "index", "freq": "M", "keep": None, "max_age_days": 200},
    # ── rents (official inflation measures) ──
    # Charted only as the derived YoY comparison against Zillow, so the raw
    # index never reaches the client — see `ship`.
    "rent_cpi":      {"id": "CUSR0000SEHA",  "label": "CPI: Rent of Primary Residence",
                      "unit": "index", "freq": "M", "keep": None, "max_age_days": 120,
                      "ship": False},
    # ── rental / ownership balance ──
    "rental_vacancy":  {"id": "RRVRUSQ156N", "label": "Rental Vacancy Rate",
                        "unit": "pct", "freq": "Q", "keep": None, "max_age_days": 300},
    "owner_vacancy":   {"id": "RHVRUSQ156N", "label": "Homeowner Vacancy Rate",
                        "unit": "pct", "freq": "Q", "keep": None, "max_age_days": 300},
    "homeownership":   {"id": "RHORUSQ156N", "label": "Homeownership Rate",
                        "unit": "pct", "freq": "Q", "keep": None, "max_age_days": 300},
    # ── construction pipeline (thousands of units, SAAR) ──
    "permits":       {"id": "PERMIT",        "label": "Building Permits",
                      "unit": "thousands", "freq": "M", "keep": None, "max_age_days": 150},
    "starts":        {"id": "HOUST",         "label": "Housing Starts",
                      "unit": "thousands", "freq": "M", "keep": None, "max_age_days": 150},
    "completions":   {"id": "COMPUTSA",      "label": "Housing Completions",
                      "unit": "thousands", "freq": "M", "keep": None, "max_age_days": 150},
    "new_home_sales":  {"id": "HSN1F",       "label": "New Single-Family Homes Sold",
                        "unit": "thousands", "freq": "M", "keep": None, "max_age_days": 150},
    "months_supply_new": {"id": "MSACSR",    "label": "Months' Supply of New Homes",
                          "unit": "months", "freq": "M", "keep": None, "max_age_days": 150},
    # ── mortgage rates ──
    "mortgage_30":   {"id": "MORTGAGE30US",  "label": "30-Year Fixed Mortgage",
                      "unit": "pct", "freq": "W", "keep": 1200, "max_age_days": 30},
    "mortgage_15":   {"id": "MORTGAGE15US",  "label": "15-Year Fixed Mortgage",
                      "unit": "pct", "freq": "W", "keep": 1200, "max_age_days": 30},
    "treasury_10":   {"id": "DGS10",         "label": "10-Year Treasury Yield",
                      "unit": "pct", "freq": "D", "keep": 6000, "max_age_days": 30,
                      "ship": False},
    # ── credit stress ──
    "delinquency":   {"id": "DRSFRMACBS",    "label": "Single-Family Mortgage Delinquency Rate",
                      "unit": "pct", "freq": "Q", "keep": None, "max_age_days": 400},
    # ── existing-home sales: KPI only, see _CHARTABLE_MIN ──
    "existing_sales": {"id": "EXHOSLUSM495S", "label": "Existing Home Sales",
                       "unit": "units", "freq": "M", "keep": None, "max_age_days": 150},
}

# NAR licenses its existing-home-sales data and its history was pulled from
# FRED: EXHOSLUSM495S and its siblings now serve ~13 observations starting
# 2025-07 instead of decades. The values are current and fine as a headline
# number, but a 13-point line next to an 800-point one reads as a broken
# chart, so anything this short is marked KPI-only rather than plotted.
_CHARTABLE_MIN = 36

# Plain client UA. A spoofed browser UA is silently blackholed by FRED's Akamai
# bot detection — see the FRED_USER_AGENT comment block in fed.py.
from ystocker.fed import FRED_USER_AGENT

_SESSION = requests.Session()
_SESSION.trust_env = False  # system proxies cause silent timeouts
_SESSION.headers.update({
    "User-Agent": FRED_USER_AGENT,
    "Accept": "text/csv,text/tab-separated-values,*/*",
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _num(raw: str) -> Optional[float]:
    """Parse a CSV cell into a float, treating blanks and sentinels as None."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw in (".", "NA", "N/A", "ND", "null"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _fetch_zillow_metric(key: str, meta: dict[str, str]) -> Optional[dict[str, Any]]:
    """Fetch one wide-format Zillow CSV.

    Returns {"dates": [...], "national": [...], "metros": {name: {...}}}.
    RegionName contains commas ("New York, NY"), so this must go through
    csv.reader rather than a naive split.

    Streamed line-by-line rather than via ``resp.text``: buffering the whole
    body would hold the bytes, the decoded str, and a StringIO copy of it at
    once — three copies of a 4.4 MB file per metric, with several metrics in
    flight. csv.reader consumes any iterator of lines, so nothing needs to be
    materialised. Zillow quotes region names but never embeds a newline in
    one, so line-at-a-time parsing is safe here.
    """
    url = _ZILLOW_BASE + meta["path"]
    try:
        resp = _SESSION.get(url, timeout=90, stream=True)
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        lines = resp.iter_lines(decode_unicode=True)
        reader = csv.reader(lines)
        header = next(reader)
    except Exception as exc:
        log.warning("Housing: Zillow %s fetch failed: %s", key, exc)
        return None

    try:
        i_size = header.index("SizeRank")
        i_name = header.index("RegionName")
        i_type = header.index("RegionType")
    except ValueError:
        log.warning("Housing: Zillow %s missing expected columns: %s", key, header[:6])
        resp.close()
        return None

    # Date columns are month-end stamps ("2026-06-30") and always trail the
    # metadata columns.
    date_idx = [i for i, c in enumerate(header) if len(c) == 10 and c[:2] == "20" and c[4] == "-"]
    if not date_idx:
        log.warning("Housing: Zillow %s has no date columns", key)
        resp.close()
        return None
    dates = [header[i] for i in date_idx]

    national: Optional[list[Optional[float]]] = None
    metros: list[tuple[int, str, list[Optional[float]]]] = []

    try:
        for row in reader:
            if len(row) <= date_idx[-1]:
                continue
            rtype = row[i_type].strip().lower()
            if rtype == "country":
                national = [_num(row[i]) for i in date_idx]
                continue
            size = _num(row[i_size])
            name = row[i_name].strip()
            if size is None or not name:
                continue
            # Only the newest window is ever charted per metro, so slice on the
            # way in instead of keeping 26 years of history for 900 metros.
            metros.append((int(size), name, [_num(row[i]) for i in date_idx[-_METRO_MONTHS:]]))
    except Exception as exc:
        log.warning("Housing: Zillow %s parse aborted: %s", key, exc)
    finally:
        resp.close()

    if national is None:
        log.warning("Housing: Zillow %s has no country row", key)

    metros.sort(key=lambda t: t[0])
    kept = {
        name: {"size_rank": size, "values": values}
        for size, name, values in metros[:_TOP_METROS]
    }
    metros.clear()

    log.info("Housing: Zillow %s — %d months (%s … %s), %d metros kept",
             key, len(dates), dates[0], dates[-1], len(kept))
    return {
        "dates": dates,
        "metro_dates": dates[-_METRO_MONTHS:],
        "national": national,
        "metros": kept,
    }


def _fetch_redfin_national() -> Optional[dict[str, Any]]:
    """Fetch and parse Redfin's national monthly tracker.

    Pins PROPERTY_TYPE to "All Residential" AND IS_SEASONALLY_ADJUSTED to
    "true". Filtering on property type alone yields two rows per month whose
    values differ by several percent — see the module docstring.

    Decompressed incrementally off the socket. ``gzip.decompress(resp.content)``
    followed by ``.decode().splitlines()`` would hold the compressed bytes, the
    full decompressed bytes, the decoded string, and a list of every line
    simultaneously — the single largest allocation in this module.
    """
    try:
        resp = _SESSION.get(_REDFIN_NATIONAL, timeout=120, stream=True)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Housing: Redfin national fetch failed: %s", exc)
        return None

    by_month: dict[str, dict[str, Optional[float]]] = {}
    header: list[str] = []
    ix: dict[str, int] = {}

    try:
        with gzip.GzipFile(fileobj=resp.raw) as gz:
            stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
            for lineno, line in enumerate(stream):
                f = [c.strip().strip('"') for c in line.rstrip("\n").split("\t")]
                if lineno == 0:
                    header = f
                    ix = {c: i for i, c in enumerate(header)}
                    required = ["PERIOD_END", "PROPERTY_TYPE", "IS_SEASONALLY_ADJUSTED"]
                    missing = [c for c in required if c not in ix]
                    if missing:
                        log.warning("Housing: Redfin schema changed — missing %s", missing)
                        return None
                    continue
                if len(f) < len(header):
                    continue
                if f[ix["PROPERTY_TYPE"]] != "All Residential":
                    continue
                if f[ix["IS_SEASONALLY_ADJUSTED"]].lower() != "true":
                    continue
                month = f[ix["PERIOD_END"]]
                if not month:
                    continue
                by_month[month] = {
                    key: _num(f[ix[col]]) if col in ix else None
                    for key, col in _REDFIN_COLS.items()
                }
    except Exception as exc:
        log.warning("Housing: Redfin parse failed: %s", exc)
        return None
    finally:
        resp.close()

    if not by_month:
        log.warning("Housing: Redfin produced no All Residential / SA rows")
        return None

    dates = sorted(by_month)
    series = {
        key: [by_month[m].get(key) for m in dates]
        for key in _REDFIN_COLS
    }
    log.info("Housing: Redfin national — %d months (%s … %s)",
             len(dates), dates[0], dates[-1])
    return {"dates": dates, "series": series}


def _fetch_mortgage_rate() -> Optional[dict[str, Any]]:
    """Monthly average 30-year fixed mortgage rate from FRED (MORTGAGE30US).

    The series is weekly; averaging to month-end aligns it with Zillow's
    monthly ZHVI for the affordability calculation.
    """
    try:
        resp = _SESSION.get(_FRED_CSV.format(series="MORTGAGE30US"), timeout=45)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Housing: MORTGAGE30US fetch failed: %s", exc)
        return None

    buckets: dict[str, list[float]] = {}
    for line in resp.text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d = parts[0].strip()
        v = _num(parts[1])
        if len(d) != 10 or v is None:
            continue
        buckets.setdefault(d[:7], []).append(v)

    if not buckets:
        return None
    months = sorted(buckets)
    return {
        "months": months,
        "values": [round(sum(buckets[m]) / len(buckets[m]), 3) for m in months],
    }


def _fetch_fred_series(key: str, meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Fetch one FRED series as {"dates": [...], "values": [...]}.

    Drops the series if its newest observation is older than ``max_age_days``.
    That check is the point: a retired FRED id keeps returning HTTP 200 with
    well-formed CSV long after it stops publishing (MBST and WASDRAL still do
    years later, per CLAUDE.md), so a status code proves nothing. Better to
    show one fewer chart than a chart of silently frozen data.
    """
    series_id = meta["id"]
    try:
        resp = _SESSION.get(_FRED_CSV.format(series=series_id), timeout=45)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        log.warning("Housing: FRED %s (%s) fetch failed: %s", key, series_id, exc)
        return None

    dates: list[str] = []
    values: list[float] = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d = parts[0].strip()
        v = _num(parts[1])
        if len(d) != 10 or v is None:
            continue
        dates.append(d)
        values.append(v)

    if not dates:
        log.warning("Housing: FRED %s (%s) returned no usable observations", key, series_id)
        return None

    try:
        age = (date.today() - date.fromisoformat(dates[-1])).days
    except ValueError:
        age = 0
    max_age = meta.get("max_age_days")
    if max_age and age > max_age:
        log.warning(
            "Housing: FRED %s (%s) looks retired — newest observation %s is %d days "
            "old (limit %d). Dropping rather than charting frozen data.",
            key, series_id, dates[-1], age, max_age,
        )
        return None

    keep = meta.get("keep")
    if keep and len(dates) > keep:
        dates, values = dates[-keep:], values[-keep:]

    return {
        "dates": dates,
        "values": values,
        "label": meta["label"],
        "unit": meta["unit"],
        "freq": meta["freq"],
        "source": "FRED",
        "series_id": series_id,
        "chartable": len(dates) >= _CHARTABLE_MIN,
    }


def _fetch_all_fred() -> dict[str, Any]:
    """Fetch every FRED series concurrently, skipping any that fail."""
    import concurrent.futures as cf

    out: dict[str, Any] = {}
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_fetch_fred_series, k, m): k for k, m in FRED_SERIES.items()}
        for fut in cf.as_completed(list(futs)):
            key = futs[fut]
            try:
                data = fut.result()
            except Exception as exc:
                log.warning("Housing: FRED %s raised: %s", key, exc)
                continue
            if data:
                out[key] = data

    dropped = [k for k in FRED_SERIES if k not in out]
    kpi_only = [k for k, v in out.items() if not v["chartable"]]
    log.info("Housing: FRED — %d/%d series (%s)", len(out), len(FRED_SERIES),
             ", ".join(f"{k} KPI-only" for k in kpi_only) or "all chartable")
    if dropped:
        log.warning("Housing: FRED series unavailable: %s", ", ".join(dropped))
    return out


# ---------------------------------------------------------------------------
# Derived series
# ---------------------------------------------------------------------------

def _monthly_last(series: dict[str, Any]) -> dict[str, float]:
    """Collapse any frequency to one value per YYYY-MM (the month's last obs)."""
    out: dict[str, float] = {}
    for d, v in zip(series["dates"], series["values"]):
        out[d[:7]] = v
    return out


def _build_mortgage_spread(fred: dict[str, Any]) -> Optional[dict[str, Any]]:
    """30-year mortgage rate minus the 10-year Treasury yield.

    Mortgages are priced off the 10-year, not off the funds rate, so this
    spread separates "rates are high because the Fed is tight" from "rates are
    high because mortgage credit itself is expensive". It normally sits near
    170bp and blew past 300bp in 2023, which is why /fedwatch easing does not
    mechanically translate into cheaper mortgages.
    """
    m30, t10 = fred.get("mortgage_30"), fred.get("treasury_10")
    if not m30 or not t10:
        return None

    # The mortgage series is weekly (Thursdays) and the Treasury series daily,
    # so align on the month rather than trying to match exact dates — a
    # calendar-day join would silently drop most weeks to null.
    t10_by_month = _monthly_last(t10)
    m30_by_month = _monthly_last(m30)

    dates, spread, mort, tsy = [], [], [], []
    for month in sorted(m30_by_month):
        t = t10_by_month.get(month)
        if t is None:
            continue
        m = m30_by_month[month]
        dates.append(month)
        mort.append(m)
        tsy.append(t)
        spread.append(round(m - t, 3))

    if not dates:
        return None
    log.info("Housing: mortgage spread — %d months, latest %.2fpp (%.2f%% vs %.2f%%)",
             len(dates), spread[-1], mort[-1], tsy[-1])
    return {"dates": dates, "spread": spread, "mortgage": mort, "treasury": tsy}


def _build_price_to_rent(fred: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Case-Shiller home prices divided by CPI rent, indexed to 100 at the start.

    Both inputs are already indices on arbitrary bases, so the ratio is only
    meaningful as its own index. Rebasing to 100 makes the level readable: the
    2006 peak and the trough after it are what give the current reading scale.
    """
    cs, rent = fred.get("case_shiller"), fred.get("rent_cpi")
    if not cs or not rent:
        return None

    cs_m, rent_m = _monthly_last(cs), _monthly_last(rent)
    months = sorted(set(cs_m) & set(rent_m))
    if len(months) < _CHARTABLE_MIN:
        return None

    raw = [cs_m[m] / rent_m[m] for m in months]
    base = raw[0]
    if not base:
        return None
    ratio = [round(v / base * 100, 2) for v in raw]

    peak_i = max(range(len(ratio)), key=lambda i: ratio[i])
    log.info("Housing: price-to-rent — %d months, latest %.1f, peak %.1f (%s)",
             len(months), ratio[-1], ratio[peak_i], months[peak_i])
    return {
        "dates": months, "values": ratio,
        "peak": ratio[peak_i], "peak_date": months[peak_i],
    }


def _build_rent_growth(
    zillow: dict[str, Any], fred: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Market asking-rent growth (Zillow ZORI) vs official CPI rent inflation.

    These measure different things and that is the point. ZORI tracks what new
    tenants are asked to pay, while CPI rent tracks what all tenants actually
    pay including those mid-lease, so CPI turns roughly a year after the market
    does. The gap between the two lines is a lead indicator for shelter
    inflation rather than a discrepancy to reconcile.
    """
    zori = zillow.get("zori")
    rent_cpi = fred.get("rent_cpi")
    if not zori or not zori.get("national") or not rent_cpi:
        return None

    def yoy_by_month(dates: list[str], values: list[Optional[float]]) -> dict[str, float]:
        by_month = {d[:7]: v for d, v in zip(dates, values) if v is not None}
        out: dict[str, float] = {}
        for month, v in by_month.items():
            y, m = int(month[:4]), int(month[5:7])
            prior = by_month.get(f"{y - 1:04d}-{m:02d}")
            if prior:
                out[month] = round((v / prior - 1) * 100, 2)
        return out

    z = yoy_by_month(zori["dates"], zori["national"])
    c = yoy_by_month(rent_cpi["dates"], rent_cpi["values"])
    months = sorted(set(z) & set(c))
    if len(months) < 12:
        return None

    return {
        "dates": months,
        "market": [z[m] for m in months],
        "cpi": [c[m] for m in months],
    }


# ---------------------------------------------------------------------------
# Derived: affordability
# ---------------------------------------------------------------------------

def _monthly_payment(loan: float, annual_rate_pct: float, years: int = 30) -> Optional[float]:
    """Standard amortising principal+interest payment."""
    if loan <= 0 or annual_rate_pct is None:
        return None
    r = annual_rate_pct / 100.0 / 12.0
    n = years * 12
    if r <= 0:
        return loan / n
    factor = (1 + r) ** n
    return loan * r * factor / (factor - 1)


def _build_affordability(
    zhvi: Optional[dict[str, Any]],
    mortgage: Optional[dict[str, Any]],
    down_pct: float = 0.20,
) -> Optional[dict[str, Any]]:
    """Monthly principal+interest on the typical US home, month by month.

    This is the series that makes the 2021-22 rate shock legible: the home
    value and the mortgage rate each tell half the story, and neither alone
    shows that the payment on the same house roughly doubled. Taxes,
    insurance, and HOA are excluded, so it is a floor on the true cost.
    """
    if not zhvi or not mortgage or not zhvi.get("national"):
        return None

    rate_by_month = dict(zip(mortgage["months"], mortgage["values"]))
    dates, payments, values, rates = [], [], [], []

    for d, home_value in zip(zhvi["dates"], zhvi["national"]):
        rate = rate_by_month.get(d[:7])
        if home_value is None or rate is None:
            continue
        pmt = _monthly_payment(home_value * (1 - down_pct), rate)
        if pmt is None:
            continue
        dates.append(d)
        payments.append(round(pmt, 0))
        values.append(round(home_value, 0))
        rates.append(rate)

    if not dates:
        return None
    log.info("Housing: affordability — %d months, latest payment $%.0f/mo",
             len(dates), payments[-1])
    return {
        "dates": dates,
        "payment": payments,
        "home_value": values,
        "rate": rates,
        "down_pct": down_pct * 100,
    }


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

def _pct_change(series: list[Optional[float]], periods: int) -> Optional[float]:
    """Percent change over *periods* observations, ignoring trailing gaps."""
    vals = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(vals) < 2:
        return None
    last_i, last_v = vals[-1]
    target = last_i - periods
    prior = [v for i, v in vals if i <= target]
    if not prior or not prior[-1]:
        return None
    return round((last_v / prior[-1] - 1) * 100, 1)


def _point_change(series: list[Optional[float]], periods: int,
                  scale: float = 1.0) -> Optional[float]:
    """Absolute change over *periods* observations, scaled."""
    vals = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(vals) < 2:
        return None
    last_i, last_v = vals[-1]
    target = last_i - periods
    prior = [v for i, v in vals if i <= target]
    if not prior:
        return None
    return round((last_v - prior[-1]) * scale, 1)


def _yoy(series: list[Optional[float]], unit: str) -> tuple[Optional[float], str]:
    """Year-over-year change, in the unit that is actually meaningful.

    A rate or a share moves in percentage *points*, not percent: the price-cut
    share going 25.6% -> 24.4% is -1.2pp, and calling that "-4.8%" (the
    relative change) invites exactly the misreading that made the /fed OAS
    spreads wrong by 100x. Levels (dollars, counts, days) keep percent change.
    """
    if unit == "pct_dec":            # stored as 0.244 for 24.4%
        return _point_change(series, 12, scale=100.0), "pp"
    if unit == "pct":                # stored as 6.69 for 6.69%
        return _point_change(series, 12), "pp"
    return _pct_change(series, 12), "pct"


def _latest(series: Optional[list[Optional[float]]]) -> Optional[float]:
    if not series:
        return None
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _build_payload() -> dict[str, Any]:
    """Fetch every source and assemble the full housing payload."""
    import concurrent.futures as cf

    zillow: dict[str, Any] = {}
    redfin: Optional[dict[str, Any]] = None
    mortgage: Optional[dict[str, Any]] = None
    fred: dict[str, Any] = {}

    # ~10 MB across 8 requests. Two workers, not more: each in-flight parse
    # holds a decompressing stream, so concurrency multiplies peak memory on a
    # box that has already been OOM-killed once (see CLAUDE.md).
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        z_futs = {
            pool.submit(_fetch_zillow_metric, k, m): k
            for k, m in ZILLOW_SERIES.items()
        }
        r_fut = pool.submit(_fetch_redfin_national)
        m_fut = pool.submit(_fetch_mortgage_rate)
        f_fut = pool.submit(_fetch_all_fred)

        for fut in cf.as_completed(list(z_futs)):
            key = z_futs[fut]
            try:
                data = fut.result()
            except Exception as exc:
                log.warning("Housing: Zillow %s raised: %s", key, exc)
                data = None
            if data:
                zillow[key] = data
        try:
            redfin = r_fut.result()
        except Exception as exc:
            log.warning("Housing: Redfin raised: %s", exc)
        try:
            mortgage = m_fut.result()
        except Exception as exc:
            log.warning("Housing: mortgage rate raised: %s", exc)
        try:
            fred = f_fut.result() or {}
        except Exception as exc:
            log.warning("Housing: FRED batch raised: %s", exc)

    if not zillow and not redfin:
        log.error("Housing: every source failed")
        return {"_ts": time.time(), "error": "housing data unavailable"}

    affordability = _build_affordability(zillow.get("zhvi"), mortgage)
    mortgage_spread = _build_mortgage_spread(fred)
    price_to_rent = _build_price_to_rent(fred)
    rent_growth = _build_rent_growth(zillow, fred)

    # ── National headline numbers ────────────────────────────────────────
    headline: dict[str, Any] = {}
    for key in ("zhvi", "zori", "days_to_pending", "price_cuts"):
        block = zillow.get(key)
        if not block or not block.get("national"):
            continue
        natl = block["national"]
        unit = ZILLOW_SERIES[key]["unit"]
        yoy, yoy_unit = _yoy(natl, unit)
        headline[key] = {
            "value": _latest(natl),
            "yoy": yoy,
            "yoy_unit": yoy_unit,
            "unit": unit,
            "as_of": block["dates"][-1],
            "source": "Zillow",
        }
    if redfin:
        for key in ("months_of_supply", "median_dom", "inventory"):
            s = redfin["series"].get(key)
            if not s:
                continue
            unit = REDFIN_METRICS[key]["unit"]
            yoy, yoy_unit = _yoy(s, unit)
            headline[f"redfin_{key}"] = {
                "value": _latest(s),
                "yoy": yoy,
                "yoy_unit": yoy_unit,
                "unit": unit,
                "as_of": redfin["dates"][-1],
                "source": "Redfin",
            }
    if mortgage:
        m_yoy, m_yoy_unit = _yoy(mortgage["values"], "pct")
        headline["mortgage_rate"] = {
            "value": mortgage["values"][-1],
            "yoy": m_yoy,
            "yoy_unit": m_yoy_unit,
            "unit": "pct",
            "as_of": mortgage["months"][-1],
            "source": "FRED",
        }

    # FRED headline tiles. Quarterly series get a 4-observation lookback so the
    # comparison is still year-over-year rather than quarter-over-quarter.
    for key in ("rental_vacancy", "homeownership", "months_supply_new",
                "starts", "permits", "new_home_sales", "existing_sales",
                "delinquency", "case_shiller"):
        block = fred.get(key)
        if not block:
            continue
        periods = 4 if block["freq"] == "Q" else 12
        unit = block["unit"]
        if unit == "pct":
            yoy, yoy_unit = _point_change(block["values"], periods), "pp"
        else:
            yoy, yoy_unit = _pct_change(block["values"], periods), "pct"
        headline[f"fred_{key}"] = {
            "value": block["values"][-1],
            "yoy": yoy,
            "yoy_unit": yoy_unit,
            "unit": unit,
            "as_of": block["dates"][-1],
            "source": "FRED",
            "label": block["label"],
        }

    if mortgage_spread:
        headline["mortgage_spread"] = {
            "value": mortgage_spread["spread"][-1],
            "yoy": round(mortgage_spread["spread"][-1] - mortgage_spread["spread"][-13], 2)
                   if len(mortgage_spread["spread"]) > 13 else None,
            "yoy_unit": "pp",
            "unit": "pp",
            "as_of": mortgage_spread["dates"][-1],
            "source": "FRED",
        }

    # ── Metro comparison table ──────────────────────────────────────────
    metro_rows: list[dict[str, Any]] = []
    zhvi = zillow.get("zhvi")
    if zhvi:
        for name, block in zhvi["metros"].items():
            row: dict[str, Any] = {
                "metro": name,
                "size_rank": block["size_rank"],
                "zhvi": _latest(block["values"]),
                "zhvi_yoy": _pct_change(block["values"], 12),
            }
            for key in ("zori", "days_to_pending", "price_cuts", "inventory"):
                blk = zillow.get(key)
                m = blk["metros"].get(name) if blk else None
                row[key] = _latest(m["values"]) if m else None
                if key == "zori":
                    row["zori_yoy"] = _pct_change(m["values"], 12) if m else None
            metro_rows.append(row)
        metro_rows.sort(key=lambda r: r["size_rank"])

    # ── National time series, kept per publisher ─────────────────────────
    national_series = {
        key: {
            "dates": block["dates"],
            "values": block["national"],
            "label": ZILLOW_SERIES[key]["label"],
            "unit": ZILLOW_SERIES[key]["unit"],
            "source": "Zillow",
        }
        for key, block in zillow.items()
        if block.get("national")
    }

    redfin_series = {}
    if redfin:
        redfin_series = {
            key: {
                "dates": redfin["dates"],
                "values": vals,
                "label": REDFIN_METRICS[key]["label"],
                "unit": REDFIN_METRICS[key]["unit"],
                "source": "Redfin",
            }
            for key, vals in redfin["series"].items()
            if any(v is not None for v in vals)
        }

    as_of = max(
        [b["dates"][-1] for b in zillow.values() if b.get("dates")]
        + ([redfin["dates"][-1]] if redfin else [])
        or [date.today().isoformat()]
    )

    payload = {
        "_ts": time.time(),
        "as_of": as_of,
        "headline": headline,
        "zillow": national_series,
        "redfin": redfin_series,
        # Series marked ship=False exist only to derive something else
        # (treasury_10 feeds the spread, rent_cpi feeds rent growth). Sending
        # 6,000 daily Treasury points the page never plots would triple the
        # response for nothing.
        "fred": {
            k: v for k, v in fred.items()
            if FRED_SERIES.get(k, {}).get("ship", True)
        },
        "affordability": affordability,
        "mortgage_spread": mortgage_spread,
        "price_to_rent": price_to_rent,
        "rent_growth": rent_growth,
        "metros": metro_rows,
        "metro_dates": zhvi["metro_dates"] if zhvi else [],
        "metro_zhvi": {
            name: blk["values"] for name, blk in zhvi["metros"].items()
        } if zhvi else {},
    }
    log.info("Housing: payload built — %d Zillow, %d Redfin, %d FRED series, %d metros",
             len(national_series), len(redfin_series), len(fred), len(metro_rows))
    return payload


# ---------------------------------------------------------------------------
# Cache — mirrors fed.py: memory -> disk -> network, lock never held over IO
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_data: Optional[dict[str, Any]] = None
_cache_ts: Optional[float] = None

_warming = False
_warming_lock = threading.Lock()

_fetch_in_progress = threading.Event()


def _load_disk_cache() -> Optional[dict[str, Any]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        payload = json.loads(_CACHE_FILE.read_text())
        if time.time() - payload.get("_ts", 0) >= _CACHE_TTL:
            return None
        if not payload.get("zillow") and not payload.get("redfin"):
            return None
        return payload
    except Exception as exc:
        log.warning("Housing: failed to read disk cache: %s", exc)
    return None


def _save_disk_cache(data: dict[str, Any]) -> None:
    """Atomically write cache to disk using temp file + rename."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_CACHE_FILE.parent, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(_CACHE_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:
        log.warning("Housing: failed to write disk cache: %s", exc)


def get_housing_data(force: bool = False) -> dict[str, Any]:
    """Return cached housing data. memory -> disk -> network.

    The cache lock is never held across the network fetch: this download is
    ~10 MB and takes seconds, and blocking a gunicorn worker on it would stall
    unrelated requests.
    """
    global _cache_data, _cache_ts

    with _cache_lock:
        now = time.time()
        if not force and _cache_data and _cache_ts and (now - _cache_ts) < _CACHE_TTL:
            return _cache_data

    if not force:
        disk = _load_disk_cache()
        if disk:
            with _cache_lock:
                _cache_data = disk
                _cache_ts = disk.get("_ts", time.time())
            return disk

    if not force and _fetch_in_progress.is_set():
        log.info("Housing: fetch in progress — returning stale/warming payload")
        with _cache_lock:
            if _cache_data:
                return _cache_data
        return {"_warming": True, "_ts": None, "zillow": {}, "redfin": {}}

    _fetch_in_progress.set()
    try:
        log.info("Housing: downloading Zillow + Redfin data")
        fresh = _build_payload()
        if fresh.get("zillow") or fresh.get("redfin"):
            with _cache_lock:
                _cache_data = fresh
                _cache_ts = fresh["_ts"]
            _save_disk_cache(fresh)
        else:
            with _cache_lock:
                if _cache_data:
                    log.warning("Housing: build failed — keeping previous cache")
                    return _cache_data
        return fresh
    finally:
        _fetch_in_progress.clear()


def get_cache_ts() -> Optional[float]:
    with _cache_lock:
        if _cache_ts:
            return _cache_ts
    disk = _load_disk_cache()
    return disk.get("_ts") if disk else None


def is_cache_fresh() -> bool:
    """True when we hold a non-stale payload with at least one publisher's data."""
    with _cache_lock:
        data = _cache_data if _cache_data else _load_disk_cache()
    if not data:
        return False
    ts = data.get("_ts")
    if not ts or (time.time() - ts) >= _CACHE_TTL:
        return False
    return bool(data.get("zillow") or data.get("redfin"))


def is_warming() -> bool:
    with _warming_lock:
        return _warming


def refresh_cache() -> None:
    """Force a refresh, ignoring the TTL."""
    global _warming
    with _warming_lock:
        _warming = True
    try:
        get_housing_data(force=True)
    finally:
        with _warming_lock:
            _warming = False


def start_background_thread() -> None:
    """Warm the cache on startup, then refresh every TTL.

    Under gunicorn --preload this runs only in the master (see CLAUDE.md), so
    the ~10 MB download happens once per deploy rather than once per worker.
    """

    def _loop() -> None:
        try:
            disk = _load_disk_cache()
            if disk:
                global _cache_data, _cache_ts
                with _cache_lock:
                    _cache_data = disk
                    _cache_ts = disk.get("_ts", time.time())
                log.info("Housing background: memory cache warmed from disk")
            else:
                log.info("Housing background: no disk cache — downloading now")
                refresh_cache()
        except Exception as exc:
            log.warning("Housing background: startup warm failed: %s", exc)

        while True:
            time.sleep(_CACHE_TTL)
            try:
                log.info("Housing background: TTL elapsed — refreshing")
                refresh_cache()
            except Exception as exc:
                log.warning("Housing background: refresh failed: %s", exc)

    t = threading.Thread(target=_loop, name="housing-background-refresh", daemon=True)
    t.start()
    log.info("Housing: background refresh thread started")
