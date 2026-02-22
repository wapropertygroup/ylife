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
    "037833100": "AAPL",
    "02079K305": "GOOGL",
    "02079K107": "GOOGL",
    "594918104": "MSFT",
    "023135106": "AMZN",
    "67066G104": "NVDA",
    "30303M102": "META",
    "88160R101": "TSLA",
    "46090E103": "JPM",
    "60505104":  "BAC",
    "172967424": "BRK-B",
    "166764100": "C",
    "949746101": "WFC",
    "38141G104": "GS",
    "617446448": "MS",
    "26441C204": "KO",
    "713448108": "PEP",
    "732834105": "PG",
    "459200101": "IBM",
    "097023105": "BA",
    "742718109": "RTX",
    "110122108": "BRK-A",
    "437076102": "HD",
    "931142103": "WMT",
    "438516106": "HON",
    "254687106": "DIS",
    "912093108": "UNH",
    "460690100": "JNJ",
    "58933Y105": "MRK",
    "002824100": "ABT",
    "002921109": "ABBV",
    "339750101": "LLY",
    "698435105": "PFE",
    "478160104": "JCI",
    "92343V104": "VZ",
    "00206R102": "T",
    "742556105": "PRU",
    "855244109": "SQ",
    "064058100": "BAX",
    "651639106": "NFLX",
    "64110D104": "NET",
    "023608102": "AMGN",
    "655044105": "NKE",
    "717081103": "PFG",
    "891482102": "TD",
    "25470F104": "DKNG",
    "52736R102": "LVS",
    "88339J105": "TMUS",
    "025816109": "AXP",
    "369550108": "GE",
    "149123101": "CAT",
    "172967304": "BRK-B",
    "78467J100": "SPG",
    "46625H100": "JPM",   # alternate
    "91324P102": "UPS",
    "268648102": "EL",
    "404280406": "GS",    # alternate
    "61945C103": "MS",    # alternate
    "78462F103": "S&P",
    "31428X106": "FDX",
    "631103108": "NOC",
    "526057104": "LMT",
    "38259P508": "GOOGL", # class C
    "57060D108": "MA",
    "92826C839": "V",
    "44920010":  "IAC",
    "49456B101": "KHC",
    "456788108": "INTU",
    "097693109": "ADBE",
    "40171V100": "GOOG",
    "76657R106": "RIVN",
    "650135108": "NIO",
    "811156100": "SCHW",
    "15135B101": "CEG",
    "637640103": "NEE",
    "03218560":  "AIG",
    "458140100": "INTC",
    "009728109": "AMD",
    "72352L106": "PINS",
    "80105N105": "SNAP",
    "883556102": "TWTR",  # historical
    "78410G104": "SE",
    "74164M108": "BIDU",
    "01609W102": "BABA",
    "87936U109": "TME",
    "98421M106": "VIPS",
    "67020Y100": "NVS",
    "145220105": "CVX",
    "30231G102": "XOM",
    "202795101": "COP",
    "26875P101": "EOG",
    "263534109": "ECL",
    "36467W109": "GDX",
    "742514509": "PSX",
    "872540109": "TSN",
    "883948100": "TGT",
    "902494103": "TJX",
    "460148109": "JD",
    "548661107": "LOW",
    "742718":    "RTX",
    "025816109": "AXP",
    "84265V105": "SBUX",
    "584977":    "MMM",
    "009158106": "ADM",
    "06738G103": "BIIB",
    "74159L101": "REGN",
    "900111204": "VRTX",
    "60871R209": "MRNA",
    "345370860": "FCX",
    "643659105": "NEM",
    "670346105": "OXY",
    "867914":    "SLB",
    "693475105": "PSA",
    "895126505": "WBA",
    "500754106": "KR",
    "78814P168": "MELI",
    "18915M107": "CLOV",
    "67085R104": "OKTA",
    "09857L108": "SNOW",
    "156700106": "CRM",
    "67066G104": "NVDA",
    "20030N101": "COIN",
    "57667L107": "MSTR",
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
    """Return list of recent filings dicts from SEC submissions endpoint."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _get(url).json()
    recent = data.get("filings", {}).get("recent", {})
    forms       = recent.get("form", [])
    accessions  = recent.get("accessionNumber", [])
    dates       = recent.get("filingDate", [])
    periods     = recent.get("reportDate", [])
    prim_docs   = recent.get("primaryDocument", [""] * len(forms))
    return [
        {"form": forms[i], "accession": accessions[i],
         "filing_date": dates[i], "period": periods[i],
         "primary_doc": prim_docs[i] if i < len(prim_docs) else ""}
        for i in range(len(forms))
    ]


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
    """Parse SEC 13F infotable XML and return list of holding dicts."""
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

    holdings = []
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

        holdings.append({
            "cusip":          cusip,
            "name":           name,
            "ticker":         ticker,
            "shares":         shares,
            "value_thousands": value_k,
            "value_millions": round(value_k / 1000, 1),
        })
    log.info("13F _parse_infotable: found %d holdings", len(holdings))
    return holdings


def _annotate_changes(curr: List[dict], prev: List[dict]) -> List[dict]:
    """Add 'change' and 'change_pct' fields to each holding by comparing with previous quarter."""
    prev_map = {h["cusip"]: h["shares"] for h in prev if h["cusip"]}
    for h in curr:
        cusip = h.get("cusip", "")
        if not cusip or cusip not in prev_map:
            h["change"] = "new"
            h["change_pct"] = None
        else:
            prev_shares = prev_map[cusip]
            curr_shares = h["shares"]
            delta = curr_shares - prev_shares
            if prev_shares:
                pct = delta / prev_shares * 100
                # If the percentage is implausibly large (e.g. due to share-count
                # unit changes between filings or sub-advisor restructuring), treat
                # the position as effectively new rather than showing a misleading number.
                if abs(pct) > 10000:
                    h["change"] = "new"
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


# ---------------------------------------------------------------------------
# Core fetch function
# ---------------------------------------------------------------------------

def fetch_fund_holdings(name: str, cik: str) -> dict:
    """
    Fetch latest 13F holdings for one fund from SEC EDGAR.
    Returns a dict with filing metadata, top-50 holdings, and error field.
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
        # Within a period, prefer the filing whose primary_doc is NOT the bare
        # "primary_doc.xml" cover stub (agent-filed wrappers use that name).
        from itertools import groupby
        periods_seen: list = []
        by_period: dict = {}
        for f in all_13f:
            p = f.get("period", "")
            if p not in by_period:
                by_period[p] = []
                periods_seen.append(p)
            by_period[p].append(f)

        def _best_for_period(candidates):
            # Prefer a filing whose primary_doc is NOT bare "primary_doc.xml"
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
        prev   = thirteenf_filings[1] if len(thirteenf_filings) > 1 else None

        # Fetch latest holdings
        info_url = _find_infotable_url(cik, latest["accession"], latest.get("primary_doc", ""))
        if not info_url:
            return {"error": "Could not locate infotable XML", "cik": cik}
        log.info("13F infotable URL for %s: %s", name, info_url)
        xml_resp = _get(info_url)
        xml_text = xml_resp.text
        log.info("13F infotable content-type=%s first100=%s", xml_resp.headers.get('content-type'), xml_text[:100].replace('\n',' '))
        curr_holdings = _parse_infotable(xml_text)

        # Fetch previous holdings for change detection
        if prev:
            try:
                prev_url = _find_infotable_url(cik, prev["accession"], prev.get("primary_doc", ""))
                if prev_url:
                    prev_xml = _get(prev_url).text
                    prev_holdings = _parse_infotable(prev_xml)
                    curr_holdings = _annotate_changes(curr_holdings, prev_holdings)
                else:
                    for h in curr_holdings:
                        h["change"] = "unknown"
            except Exception as exc:
                log.warning("Could not fetch prev quarter for %s: %s", name, exc)
                for h in curr_holdings:
                    h["change"] = "unknown"
        else:
            for h in curr_holdings:
                h["change"] = "new"

        # Sort by value descending, compute portfolio %
        curr_holdings.sort(key=lambda h: h["value_thousands"], reverse=True)
        total_value_k = sum(h["value_thousands"] for h in curr_holdings)
        total_millions = round(total_value_k / 1000, 1)

        top50 = curr_holdings[:50]
        for i, h in enumerate(top50, 1):
            h["rank"] = i
            h["pct_portfolio"] = (
                round(h["value_thousands"] / total_value_k * 100, 2)
                if total_value_k > 0 else 0.0
            )

        return {
            "cik":                cik,
            "filing_date":        latest["filing_date"],
            "period_of_report":   latest["period"],
            "holdings":           top50,
            "total_holdings":     len(curr_holdings),
            "total_value_millions": total_millions,
            "error":              None,
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
