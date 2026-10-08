"""
ystocker.congress
~~~~~~~~~~~~~~~~~
Trades by members of the U.S. House, from the Periodic Transaction Reports
the STOCK Act makes them file, as the House Clerk publishes them.

Why it exists
-------------
/smart-money (asked for 2026-10-07, after openbit.trade/people) reads four
kinds of disclosure side by side: what officers and directors trade (Form 4,
insiders.py), what large funds hold (13F, sec13f.py), and what members of
Congress trade. This module is the third. The fourth on openbit's page,
influencers' posts, is opinion rather than money and is not borrowed.

Source
------
Two keyless files from disclosures-clerk.house.gov:

* ``{year}FD.zip`` -- one XML row per financial-disclosure filing made that
  year: name, district, filing type and date, and a DocID. Measured from the
  box on 2026-10-07: 1,746 filings in 2026, 415 of them PTRs (type ``P``),
  62 KB zipped.
* ``ptr-pdfs/{year}/{DocID}.pdf`` -- the report. An electronic one is 30-70 KB
  and answered in under 0.1 s. A DocID starting 8 or 9 is a paper filing,
  scanned: 51 of the 415. Its text is empty, so it is listed and linked and
  never parsed (``paper``).

What a PTR says, and what it does not (the page repeats this)
-------------------------------------------------------------
* The amount is a range -- $1,001-$15,000 up to over $50,000,000 -- never a
  figure. Sums use range midpoints and say so.
* The owner may be the member, a spouse (SP), a dependent child (DC) or the
  member jointly (JT): ``o`` keeps which.
* A trade must be reported within 30 days of the member learning of it and 45
  days of the trade, so both dates are kept and the lag is shown. A filing in
  October can describe an August trade.
* It does not say who decided the trade, what is held, or anything not
  reported.

Parsing
-------
pdfplumber's text, not its tables: the table reader merged a page's first row
into one cell on the sample. Three facts about the text, all from the 48
reports in ``tests/fixtures/house_ptr``:

* A trade's first line carries its owner, the start of the asset, the type
  (P, S, ``S (partial)``, E), both dates and the start of the amount. The
  asset's name, and an amount's upper bound, wrap onto the lines after it:
  ``JT ARBUCKLE MEM HOSP AUTH P 09/08/2026 09/15/2026 $15,001 -`` then
  ``OKLA SALES TAX 03.00000% $50,000``.
* The column header repeats at every page break, inside a trade as often as
  between two (Gottheimer's RSG purchase is split by one).
* The PDF's font maps the letters of three labels -- Filing Status, Subholding
  Of, Description -- to NULs, so they arrive as ``F  S  :``, ``S  O  :`` and
  ``D  :``. They are matched by their surviving capitals.

The request path never fetches: a background thread in the master keeps the
index and the reports cached, and writes one combined ``feed.json`` that
workers read on its mtime. Not a DynamoDB table: every report can be fetched
again.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from ystocker import fetchguard

log = logging.getLogger(__name__)

PROVIDER = "house-clerk"
INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "congress"
SCHEMA = 1
#: Reports filed within this many days are fetched and kept.
WINDOW_DAYS = 365
INDEX_TTL_SECONDS = 3 * 3600
#: How often the thread looks again, and how long after boot it first does.
CYCLE_SECONDS = fetchguard.env_float("CONGRESS_CYCLE_SECONDS", 3 * 3600.0, 600.0)
FIRST_DELAY_SECONDS = 180.0
REQUEST_SPACING_SECONDS = fetchguard.env_float("CONGRESS_REQUEST_SPACING_SECONDS", 1.5, 0.2)
#: A report that failed to download or parse is tried again after this long.
RETRY_FAILED_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; ystocker/1.0; +https://stock.li-family.us)"

OWNERS = {"SP": "spouse", "DC": "child", "JT": "joint"}
KINDS = {"P": "buy", "S": "sell", "S (partial)": "sell", "E": "exchange"}
#: The asset codes counted towards a company: stock and options on it.
EQUITY_CODES = ("ST", "OP")

#: A few members whose names readers know in Chinese. Everyone else is shown
#: as filed: transliterating a name nobody has rendered before invents it.
MEMBER_ZH: dict[str, str] = {
    "Nancy Pelosi": "南希·佩洛西",
    "Josh Gottheimer": "乔希·戈特海默",
    "Marjorie Taylor Greene": "玛乔丽·泰勒·格林",
    "Ro Khanna": "罗·卡纳",
    "Dan Crenshaw": "丹·克伦肖",
    "Michael McCaul": "迈克尔·麦考尔",
    "Tommy Tuberville": "汤米·塔伯维尔",
}


# ── Pure: the index ──────────────────────────────────────────────────────────

def iso_date(mdy: str) -> Optional[str]:
    """``"9/9/2026"`` or ``"09/09/2026"`` -> ``"2026-09-09"``; None if not a date."""
    try:
        return datetime.strptime(str(mdy).strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def is_paper(doc: str) -> bool:
    """A paper filing's DocID starts with 8 or 9; an electronic one with 2."""
    return str(doc)[:1] in ("8", "9")


def display_name(first: str, last: str, suffix: str = "") -> str:
    name = " ".join(p for p in (str(first or "").strip(), str(last or "").strip()) if p)
    suffix = str(suffix or "").strip()
    return f"{name} {suffix}" if suffix else name


def parse_index(xml: bytes) -> list[dict[str, Any]]:
    """The PTR rows of one year's ``{year}FD.xml``, newest filing first.

    lxml rather than ElementTree: this repo's dev Mac cannot load pyexpat
    (gics.py says why), and lxml carries its own parser.
    """
    from lxml import etree

    root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    rows = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        if f.get("FilingType") != "P" or not f.get("DocID"):
            continue
        filed = iso_date(f.get("FilingDate", ""))
        if not filed:
            continue
        rows.append({
            "doc": f["DocID"], "year": int(f.get("Year") or filed[:4]), "filed": filed,
            "name": display_name(f.get("First", ""), f.get("Last", ""), f.get("Suffix", "")),
            "last": f.get("Last", ""), "district": f.get("StateDst", ""),
            "paper": is_paper(f["DocID"]),
        })
    rows.sort(key=lambda r: (r["filed"], r["doc"]), reverse=True)
    return rows


# ── Pure: one report ─────────────────────────────────────────────────────────

_HEADER_LINES = ("ID Owner Asset Transaction Date Notification Amount Cap.", "Type Date Gains >", "$200?")
_ANCHOR = re.compile(
    r"^(?:(?P<owner>SP|DC|JT)\s+)?(?P<asset>.*?)\s*(?<![^\s])(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<tx>\d{2}/\d{2}/\d{4})\s+(?P<nt>\d{2}/\d{2}/\d{4})\s*(?P<amt>.*)$")
_STATUS = re.compile(r"^F\s+S\s*:\s*(?P<v>.*)$")
_SUBHOLDING = re.compile(r"^S\s+O\s*:\s*(?P<v>.*)$")
_DESCRIPTION = re.compile(r"^D\s*:\s*(?P<v>.*)$")
_END = "* For the complete list of asset type abbreviations"
_TRAILING_AMOUNT = re.compile(r"(?:^|\s)(\$[\d,]+)\s*$")
_TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
_CODE = re.compile(r"\[([A-Z]{2})\]")
_MONEY = re.compile(r"\$([\d,]+)")


def parse_amount(text: str) -> tuple[Optional[int], Optional[int]]:
    """``"$1,001 - $15,000"`` -> (1001, 15000); ``"Over $50,000,000"`` ->
    (50000001, None); an amount still missing its upper bound -> (low, None)."""
    nums = [int(x.replace(",", "")) for x in _MONEY.findall(text or "")]
    if not nums:
        return None, None
    if "over" in (text or "").lower():
        return nums[-1] + 1, None
    if len(nums) >= 2:
        return nums[0], nums[1]
    return nums[0], None


def _amount_open(text: str) -> bool:
    """The amount on a trade's first line stops at its dash: the upper bound
    wrapped onto the next line."""
    t = (text or "").strip()
    return bool(t) and t.endswith("-")


def option_side(description: Optional[str]) -> Optional[str]:
    """``"call"`` or ``"put"`` when an options trade's description says which."""
    m = re.search(r"\b(call|put)s?\b", description or "", re.I)
    return m.group(1).lower() if m else None


def parse_ptr(text: str) -> dict[str, Any]:
    """One report's text as data: the member and every trade, in filed order.

    Raises nothing on an odd report; a trade that cannot be read is skipped,
    and ``unread`` counts the first lines that looked like trades and failed.
    """
    lines = [ln.replace("\x00", " ").strip() for ln in (text or "").splitlines()]
    whole = "\n".join(lines)

    def field(label: str) -> Optional[str]:
        m = re.search(rf"^{label}:\s*(.+)$", whole, re.M)
        return m.group(1).strip() if m else None

    name = field("Name")
    if name:
        name = re.sub(r"^(Hon\.|Honorable)\s+", "", name).strip()
    signed = None
    m = re.search(r"^Digitally Signed:.*?(\d{2}/\d{2}/\d{4})\s*$", whole, re.M)
    if m:
        signed = iso_date(m.group(1))
    m = re.search(r"Filing ID #(\d+)", whole)

    trades: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    mode = None
    started = False
    for line in lines:
        if not started:
            started = line.startswith(_HEADER_LINES[0])
            continue
        if line.startswith(_END):
            break
        if not line or line in _HEADER_LINES or line.startswith(_HEADER_LINES[0]):
            continue
        a = _ANCHOR.match(line)
        if a and a.group("asset").strip():
            cur = {"owner": a.group("owner") or "", "asset_parts": [a.group("asset").strip()],
                   "type": a.group("type"), "tx": iso_date(a.group("tx")), "notified": iso_date(a.group("nt")),
                   "amount": a.group("amt").strip(), "status": None, "sub": None, "desc": None}
            trades.append(cur)
            mode = "asset"
            continue
        if cur is None:
            continue
        for rx, key in ((_STATUS, "status"), (_SUBHOLDING, "sub"), (_DESCRIPTION, "desc")):
            lm = rx.match(line)
            if lm:
                cur[key] = lm.group("v").strip()
                mode = key
                break
        else:
            if mode == "asset":
                if _amount_open(cur["amount"]):
                    tail = _TRAILING_AMOUNT.search(line)
                    if tail:
                        cur["amount"] = f"{cur['amount']} {tail.group(1)}"
                        line = line[:tail.start()].strip()
                if line:
                    cur["asset_parts"].append(line)
            elif mode in ("desc", "sub") and cur.get(mode) is not None:
                cur[mode] = f"{cur[mode]} {line}".strip()
        continue

    out = []
    for t in trades:
        asset = " ".join(t.pop("asset_parts"))
        codes = _CODE.findall(asset)
        code = codes[-1] if codes else None
        before = asset[:asset.rfind(f"[{code}]")] if code else asset
        tickers = _TICKER.findall(before)
        ticker = tickers[-1] if tickers else None
        clean = _CODE.sub("", asset)
        if ticker:
            clean = clean.replace(f"({ticker})", "")
        lo, hi = parse_amount(t["amount"])
        out.append({
            "owner": t["owner"], "asset": re.sub(r"\s+", " ", clean).strip(" -"),
            "ticker": ticker, "code": code, "type": t["type"], "kind": KINDS.get(t["type"], "other"),
            "tx": t["tx"], "notified": t["notified"], "amount": t["amount"], "lo": lo, "hi": hi,
            "status": t["status"], "sub": t["sub"], "desc": t["desc"],
            "option": option_side(t["desc"]) if code == "OP" else None,
        })
    return {"name": name, "district": field("State/District"), "member_status": field("Status"),
            "signed": signed, "filing_id": m.group(1) if m else None, "trades": out}


def normalise_ticker(raw: Optional[str]) -> Optional[str]:
    """A filed ticker as Yahoo spells it: ``BRK.B`` -> ``BRK-B``."""
    if not raw:
        return None
    from ystocker import portfolio_csv

    return portfolio_csv.normalise_symbol(raw) or None


def direction(trade: dict[str, Any]) -> Optional[str]:
    """``"buy"``, ``"sell"`` or None for what a trade says about the company.

    Stock: a purchase is a buy and a sale a sell. Options: buying calls is a
    buy and buying puts a sell; selling either may close a position or write
    one, so it says nothing either way. Anything else -- bonds, funds, an
    exchange -- says nothing about a company.
    """
    code, kind = trade.get("code"), trade.get("kind")
    if code == "ST":
        return kind if kind in ("buy", "sell") else None
    if code == "OP" and kind == "buy":
        side = trade.get("option")
        return "buy" if side == "call" else "sell" if side == "put" else None
    return None


def feed_rows(filings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every trade of every parsed filing as a compact row, newest filing first.

    ``t`` ticker (Yahoo's form, or None), ``n`` asset, ``m`` member, ``md``
    district, ``o`` owner (SP/DC/JT, "" for the member), ``k`` kind, ``dir``
    buy/sell/None (see :func:`direction`), ``c`` asset code, ``op`` call/put,
    ``d`` trade date, ``f`` filed, ``lo``/``hi`` the amount range, ``doc``
    and ``yr`` to link the report.
    """
    rows = []
    for fl in filings:
        for tr in fl.get("trades") or ():
            rows.append({
                "t": normalise_ticker(tr.get("ticker")), "n": tr.get("asset"),
                "m": fl.get("name"), "md": fl.get("district"), "o": tr.get("owner") or "",
                "k": tr.get("kind"), "dir": direction(tr), "c": tr.get("code"), "op": tr.get("option"),
                "d": tr.get("tx"), "f": fl.get("filed"), "lo": tr.get("lo"), "hi": tr.get("hi"),
                "doc": fl.get("doc"), "yr": fl.get("year"),
            })
    rows.sort(key=lambda r: (r["f"] or "", r["d"] or ""), reverse=True)
    return rows


def lag_days(trade_date: Optional[str], filed: Optional[str]) -> Optional[int]:
    try:
        return (date.fromisoformat(filed) - date.fromisoformat(trade_date)).days
    except (TypeError, ValueError):
        return None


def report_url(doc: str, year: int) -> str:
    return PTR_URL.format(year=year, doc=doc)


# ── Cache ────────────────────────────────────────────────────────────────────

_session_lock = threading.Lock()
_session = None
_pace_lock = threading.Lock()
_last_request = 0.0


def _http():
    global _session
    import requests

    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({"User-Agent": USER_AGENT})
        return _session


def _pace() -> None:
    global _last_request
    with _pace_lock:
        wait = REQUEST_SPACING_SECONDS - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()


def _get(url: str):
    _pace()
    return fetchguard.request(PROVIDER, url, session=_http(), timeout=TIMEOUT_SECONDS, retries=1,
                              retry_statuses=(429, 500, 502, 503, 504), raise_for_status=False)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _index_path(year: int) -> Path:
    return CACHE_DIR / f"index_{year}.json"


def _ptr_path(doc: str) -> Path:
    return CACHE_DIR / "ptr" / f"{doc}.json"


def load_index(year: int, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """One year's PTR rows: the cached copy while fresh, else fetched. A failed
    fetch keeps the cached copy, however old."""
    now = time.time() if now is None else now
    cached = _read_json(_index_path(year)) or {}
    if cached.get("v") == SCHEMA and now - float(cached.get("fetched") or 0) < INDEX_TTL_SECONDS:
        return cached.get("rows") or []
    try:
        resp = _get(INDEX_URL.format(year=year))
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            rows = parse_index(z.read(name))
    except fetchguard.CooldownActive:
        raise
    except Exception as exc:
        log.warning("congress: %d index not refreshed: %s", year, exc)
        return cached.get("rows") or []
    _write_json(_index_path(year), {"v": SCHEMA, "fetched": now, "rows": rows})
    return rows


def pdf_text(content: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def fetch_report(row: dict[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
    """Download and parse one report, or record a paper filing; saved either way."""
    now = time.time() if now is None else now
    rec: dict[str, Any] = {"v": SCHEMA, "doc": row["doc"], "year": row["year"], "filed": row["filed"],
                           "name": row["name"], "district": row["district"], "paper": row["paper"],
                           "trades": [], "fetched": now}
    if row["paper"]:
        _write_json(_ptr_path(row["doc"]), rec)
        return rec
    resp = _get(report_url(row["doc"], row["year"]))
    if resp.status_code != 200:
        rec["error"] = f"HTTP {resp.status_code}"
    else:
        try:
            parsed = parse_ptr(pdf_text(resp.content))
            rec["trades"] = parsed["trades"]
            rec["signed"] = parsed["signed"]
            if parsed["name"]:
                rec["filed_name"] = parsed["name"]
        except Exception as exc:   # a malformed PDF costs that report, not the sweep
            rec["error"] = f"{type(exc).__name__}: {exc}"
    _write_json(_ptr_path(row["doc"]), rec)
    return rec


def _due(row: dict[str, Any], now: float) -> bool:
    rec = _read_json(_ptr_path(row["doc"]))
    if not rec or rec.get("v") != SCHEMA:
        return True
    return bool(rec.get("error")) and now - float(rec.get("fetched") or 0) > RETRY_FAILED_SECONDS


def build_feed(rows: list[dict[str, Any]], since: str, *, now: float) -> dict[str, Any]:
    """The combined file workers read: every cached report filed since ``since``."""
    filings, missing, failed, paper = [], 0, 0, 0
    for row in rows:
        if row["filed"] < since:
            continue
        rec = _read_json(_ptr_path(row["doc"]))
        if not rec:
            missing += 1
            continue
        if rec.get("paper"):
            paper += 1
        if rec.get("error"):
            failed += 1
        filings.append({k: rec.get(k) for k in ("doc", "year", "filed", "name", "district", "paper", "trades", "error")})
    members: dict[str, dict[str, Any]] = {}
    for fl in filings:
        m = members.setdefault(fl["name"], {"name": fl["name"], "district": fl["district"], "filings": 0,
                                            "paper": 0, "last_filed": fl["filed"]})
        m["filings"] += 1
        m["paper"] += 1 if fl["paper"] else 0
        m["last_filed"] = max(m["last_filed"], fl["filed"])
    return {
        "v": SCHEMA, "built": now, "since": since,
        "latest_filed": max((f["filed"] for f in filings), default=None),
        "counts": {"filings": len(filings), "paper": paper, "failed": failed, "pending": missing},
        "members": sorted(members.values(), key=lambda m: m["last_filed"], reverse=True),
        "rows": feed_rows(f for f in filings if not f.get("error")),
        "paper": [{"doc": f["doc"], "year": f["year"], "filed": f["filed"], "name": f["name"],
                   "district": f["district"]} for f in filings if f["paper"]],
    }


def refresh_once(*, now: Optional[float] = None, budget: Optional[int] = None) -> dict[str, int]:
    """One pass: the indexes for the window, then every report not yet cached,
    newest first, then the combined feed. Stops at a cool-down, keeping what it
    has. Returns counts."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    years = sorted({today.year, int(since[:4])})
    rows: list[dict[str, Any]] = []
    fetched = 0
    try:
        seen: set[str] = set()
        for y in years:
            # A DocID is the Clerk's, unique across years; a report listed in
            # both files must not be fetched, or counted, twice.
            for r in load_index(y, now=now):
                if r["doc"] not in seen:
                    seen.add(r["doc"])
                    rows.append(r)
        rows.sort(key=lambda r: (r["filed"], r["doc"]), reverse=True)
        for row in rows:
            if row["filed"] < since or not _due(row, now):
                continue
            if budget is not None and fetched >= budget:
                break
            fetch_report(row, now=now)
            fetched += 1
    except fetchguard.CooldownActive as exc:
        log.warning("congress: stopped for the cool-down after %d report(s): %s", fetched, exc)
    if rows:
        _write_json(CACHE_DIR / "feed.json", build_feed(rows, since, now=time.time()))
    return {"index_rows": len(rows), "fetched": fetched}


_feed_lock = threading.Lock()
_feed_mem: Optional[dict[str, Any]] = None
_feed_mtime: Optional[float] = None


def peek() -> Optional[dict[str, Any]]:
    """The combined feed, any age, or None before the first pass. Never fetches;
    re-read when the file's mtime moves (the thread writes it in the master)."""
    global _feed_mem, _feed_mtime
    path = CACHE_DIR / "feed.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _feed_lock:
        if _feed_mem is not None and _feed_mtime == mtime:
            return _feed_mem
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("v") != SCHEMA:
        return None
    with _feed_lock:
        _feed_mem, _feed_mtime = data, mtime
    return data


def start_background_thread() -> None:
    """A pass a few minutes after boot, then every CYCLE_SECONDS. The first pass
    on a new box backfills a year of reports, paced (~535 reports, ~15 min)."""

    def _loop() -> None:
        time.sleep(FIRST_DELAY_SECONDS)
        while True:
            try:
                counts = refresh_once()
                log.info("congress: pass done, %d index rows, %d report(s) fetched",
                         counts["index_rows"], counts["fetched"])
            except Exception:
                log.exception("congress: pass failed")
            time.sleep(CYCLE_SECONDS)

    threading.Thread(target=_loop, daemon=True, name="congress-ptr").start()
    log.info("congress: House PTR thread started (window %dd)", WINDOW_DAYS)
