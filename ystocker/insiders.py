"""
ystocker.insiders
~~~~~~~~~~~~~~~~~
What company insiders bought and sold: the data behind /insiders, from the
Form 4s they file with the SEC.

An officer, a director or a 10% owner must report a trade in the company's
stock within two business days, on Form 4. The filing is an XML
``ownershipDocument`` listing each transaction with a code. P is a purchase and
S a sale. Everything else -- A an award, M an option exercise, F shares withheld
for tax, G a gift, C a conversion -- is compensation or paperwork, not a
decision to put money in or take it out, so the feed is P and S, and the rest
is kept and shown only on a company's own view.

Where the filings are
---------------------
Per issuer, not per day. SEC's daily form index lists a day's Form 4s once
each (3,097 on 2026-10-02), under a single CIK that is often the reporting
owner's rather than the issuer's, so it cannot be filtered by company. An
issuer's submissions JSON can be: ``filings.recent`` lists every filing about
the company, its insiders' Form 4s included, at least a year back. JPMorgan's
runs to 26,300 entries and 4.5 MB because of its structured notes, and still
reaches exactly a year, so a 90-day window never falls off its end. One request
per issuer, 0.02-0.34 s on the box (2026-10-04).

``primaryDocument`` names the XSL-rendered copy (``xslF345X06/form4.xml``),
which is HTML: the same trap that broke 13F parsing (``xslForm13F_X0n`` in
sec13f.py). The raw XML is the same file name at the accession folder's root,
and under the *issuer's* CIK. The accession prefix is the filing agent's:
Apple's Form 4s are filed by 0001140361, and that path 404s.

What it costs
-------------
A refresh is one submissions request, then one per Form 4 not yet cached and
filed within :data:`WINDOW_DAYS` -- :data:`FIRST_FILL_DAYS` on an issuer's first
fill, so a cold box's backfill is bounded and the default 30-day view is whole
after one pass. A parsed accession is never fetched again. Measured on a random
30 of the 215 issuers on 2026-10-04: 23 Form 4s in 7 days, 61 in 30 and 270 in
90, so about 15-23 a day across the universe. The first pass is ~215
submissions plus ~440 Form 4s; the next fills days 31-90 (~1,500); after that a
pass is ~215 submissions plus the handful filed since the last one. At one
pass per :data:`RECHECK_SECONDS` (6 h) that is ~860 submissions requests a day
-- ``INSIDERS_RECHECK_HOURS=24`` makes it ~215 -- and SEC's limit is ten a
second.

Every request goes through ``sec13f.edgar_get``, SEC's one throttle and breaker
in this process, so this module's traffic counts against the same per-client
limit as the Fundamentals builds and the 13F refresh. On top of that it paces
itself :data:`REQUEST_SPACING_SECONDS` apart, and a pass stops at the first
``CooldownActive``.

The request path never fetches
------------------------------
The market-wide feed reads the per-issuer files the sweep writes and answers
202 while the first pass is still reaching some of the universe. A company
asked for by ticker that has never been fetched is queued for one worker per
process and answers 202; the page polls with a bound. Those look-ups are capped
per day (``quota.try_consume_insider_lookup``), because any of ~8,000 SEC
tickers can be asked for and the crawler guard only refuses the bots that say
so.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "cache" / "insiders"
TICKER_CACHE_FILE = Path(__file__).parent.parent / "cache" / "ticker_cache.json"
UNIVERSE_FILE = "_universe.json"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{name}"
SOURCE = "SEC EDGAR, Form 4"


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


#: Bump when a stored record's shape changes; an older file then reads as cold.
CACHE_VER = 1
#: How far back a refresh looks for Form 4s it has not fetched.
WINDOW_DAYS = 90
#: How far back an issuer's very first fill looks. The rest of the window fills
#: on its next check, so a cold box's first pass stays bounded.
FIRST_FILL_DAYS = 30
#: Filings older than this are pruned. Longer than WINDOW_DAYS, so a pruned
#: accession is outside every window and is never fetched again.
KEEP_DAYS = 120
#: An issuer is due for a new check this long after its last one.
RECHECK_SECONDS = _env_float("INSIDERS_RECHECK_HOURS", 6.0, 1.0) * 3600
REQUEST_SPACING_SECONDS = 1.5
#: Form 4s fetched for one issuer in one check; the rest wait for the next.
MAX_DOCS_PER_CHECK = 60
#: A document that failed this many times is given up on (until it is pruned).
MAX_DOC_TRIES = 3
BOOT_DELAY_SECONDS = 60
SWEEP_INTERVAL_SECONDS = 15 * 60
#: Look-ups by ticker waiting for the on-demand worker, per process.
MAX_PENDING = 20
FAILURE_PAUSE_SECONDS = 10 * 60
#: A look-up claim older than this is a dead run, and may be taken again.
CLAIM_TTL_SECONDS = 5 * 60
#: A company outside the followed set stays in the market-wide feed this long
#: after a reader's look-up refreshed it; it is not swept, so it then drops out
#: rather than going stale in place.
LOOKUP_FRESH_SECONDS = 24 * 3600
MAX_FEED_ROWS = 1500
MAX_CLUSTERS = 24
KINDS = ("buy", "sell", "all")
SORTS = ("value", "newest")

_FORMS = ("4", "4/A")
_TRUE = frozenset({"1", "true", "y", "yes"})
_FALSE = frozenset({"0", "false", "n", "no"})


# ── Parsing (pure) ──────────────────────────────────────────────────────────

#: "Rule 10b5-1", "10b5-1 trading plan", "Rule 10b5-1(c)".
_PLAN_RE = re.compile(r"10b5-?1", re.I)
#: A weighted-average price's footnote: "at prices ranging from $211.00 to
#: $211.05". See _price_ruled_out.
_PRICE_RANGE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:to|through|-|–)\s*\$\s?([\d,]+(?:\.\d+)?)", re.I)
#: How far outside its footnote's range a price may sit before it is a typo.
PRICE_RANGE_SLACK = 4.0
#: "not pursuant to a Rule 10b5-1 plan", "other than under ... 10b5-1".
_PLAN_NEG_RE = re.compile(r"\b(?:not|other than|outside(?:\s+of)?)\b[^.;]{0,80}?10b5-?1", re.I)
#: Code P is "open market or private purchase". A purchase in an offering or a
#: private placement is the issuer selling stock at a negotiated price, not an
#: insider choosing the market: ZDGE's director bought 2.2M shares in a private
#: offering on 2026-09-25, and PYXS's 10% owner 5.5M in a public offering on
#: 10-01, both with the price only in a footnote.
_OFFERING_RE = re.compile(
    r"\b(?:private|public|registered direct|rights|initial public|underwritten|secondary|follow-on)"
    r"\s+offering|private placement|subscription agreement|securities purchase agreement", re.I)


def mentions_plan(text: Optional[str]) -> bool:
    """Whether a footnote says a trade was made under a Rule 10b5-1 plan."""
    if not text or not _PLAN_RE.search(text):
        return False
    return not _PLAN_NEG_RE.search(text)


def _root(xml: str | bytes) -> Any:
    """The ``ownershipDocument`` element, namespaces stripped.

    lxml rather than ElementTree: this repo's dev Mac cannot load ``pyexpat``
    (see gics.py), and lxml, a declared dependency, carries its own libxml2.
    No entities, no DTDs, no network: the document came off the internet.
    """
    from lxml import etree

    data = xml.encode("utf-8") if isinstance(xml, str) else bytes(xml)
    data = data.lstrip(b"\xef\xbb\xbf").lstrip()
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=False, recover=False)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"not XML: {exc}") from exc
    if root is None:
        raise ValueError("empty document")
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    if root.tag != "ownershipDocument":
        # The XSL-rendered copy is HTML. Reading it as data is how 13F
        # parsing broke; refusing it here keeps an empty filing out of the cache.
        raise ValueError(f"not an ownershipDocument: <{root.tag}>")
    return root


def _val(parent: Any, path: Optional[str] = None) -> Optional[str]:
    """Text of the element at *path*, read through its ``<value>`` wrapper when
    it has one. A price given only as a footnote has no ``<value>``: that is
    ``None``, never 0."""
    if parent is None:
        return None
    node = parent.find(path) if path else parent
    if node is None:
        return None
    inner = node.find("value")
    target = inner if inner is not None else node
    text = (target.text or "").strip()
    return text or None


def _num(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        value = float(text.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _flag(text: Optional[str]) -> Optional[bool]:
    low = (text or "").strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return None


def _cik(text: Optional[str]) -> Optional[int]:
    digits = (text or "").strip()
    return int(digits) if digits.isdigit() else None


def _ids(node: Any) -> list[str]:
    """The footnote ids referenced anywhere inside *node*, in order, once each."""
    out: list[str] = []
    if node is None:
        return out
    for ref in node.iter("footnoteId"):
        key = (ref.get("id") or "").strip()
        if key and key not in out:
            out.append(key)
    return out


def _flat(node: Any) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def parse_form4(xml: str | bytes) -> dict[str, Any]:
    """One Form 4 (or 4/A) as data. Raises ``ValueError`` on anything else.

    Returns the issuer, every reporting owner, the non-derivative transactions
    and whether the filing marks a Rule 10b5-1 plan. Two things are flagged per
    transaction from the footnotes attached to it: ``plan`` (10b5-1) and
    ``offering`` (a purchase in an offering or private placement). The note on
    the price -- usually "a weighted average of prices ranging from ..." -- is
    kept as ``price_note``.

    The plan box (``aff10b5One``, since the 2023 rule) is one box for the whole
    filing, and the rule asks for the plan's adoption date in a footnote, which
    filers attach to the lines it covers. When the box is ticked and no
    footnote says which line, it is applied to the purchases and sales: the box
    reads "a plan for the purchase or sale", so it cannot mean an award.
    """
    root = _root(xml)
    form = _val(root, "documentType")
    if form not in _FORMS:
        raise ValueError(f"not a Form 4: documentType {form!r}")

    notes: dict[str, str] = {}
    for fn in root.iter("footnote"):
        key = (fn.get("id") or "").strip()
        if key:
            notes[key] = _flat(fn)
    remarks = _flat(root.find("remarks"))

    issuer_el = root.find("issuer")
    owners: list[dict[str, Any]] = []
    for owner in root.findall("reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        owners.append({
            "cik": _cik(_val(owner, "reportingOwnerId/rptOwnerCik")),
            "name": _val(owner, "reportingOwnerId/rptOwnerName") or "",
            # An absent flag is a no: TPR's filer writes only <isOfficer>.
            "director": _flag(_val(rel, "isDirector")) is True,
            "officer": _flag(_val(rel, "isOfficer")) is True,
            "title": _val(rel, "officerTitle"),
            "ten_pct": _flag(_val(rel, "isTenPercentOwner")) is True,
            "other": _flag(_val(rel, "isOther")) is True,
            "other_text": _val(rel, "otherText"),
        })

    txs: list[dict[str, Any]] = []
    for el in root.iter("nonDerivativeTransaction"):
        code = (_val(el, "transactionCoding/transactionCode") or "").upper()
        attached = [notes[i] for i in _ids(el) if i in notes]
        price_el = el.find("transactionAmounts/transactionPricePerShare")
        price_notes = [notes[i] for i in _ids(price_el) if i in notes]
        txs.append({
            "security": _val(el, "securityTitle"),
            "date": _val(el, "transactionDate"),
            "code": code,
            "shares": _num(_val(el, "transactionAmounts/transactionShares")),
            "price": _num(_val(price_el)),
            "ad": (_val(el, "transactionAmounts/transactionAcquiredDisposedCode") or "").upper() or None,
            "after": _num(_val(el, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
            "own": (_val(el, "ownershipNature/directOrIndirectOwnership") or "").upper() or None,
            "nature": _val(el, "ownershipNature/natureOfOwnership"),
            "plan": any(mentions_plan(t) for t in attached),
            "offering": code == "P" and any(_OFFERING_RE.search(t) for t in attached),
            "price_note": " ".join(price_notes) or None,
        })

    box = _flag(_val(root, "aff10b5One"))
    if box is True and not any(t["plan"] for t in txs):
        for t in txs:
            if t["code"] in ("P", "S"):
                t["plan"] = True
    return {
        "form": form,
        "period": _val(root, "periodOfReport"),
        "original": _val(root, "dateOfOriginalSubmission"),
        "issuer": {
            "cik": _cik(_val(issuer_el, "issuerCik")),
            "name": _val(issuer_el, "issuerName"),
            "symbol": _val(issuer_el, "issuerTradingSymbol"),
        },
        "owners": owners,
        "aff10b5_1": box,
        "plan": box is True or any(mentions_plan(t) for t in notes.values()) or mentions_plan(remarks),
        "tx": txs,
        "derivatives": sum(1 for _ in root.iter("derivativeTransaction")),
    }


# ── Classification and arithmetic (pure) ────────────────────────────────────

def kind(code: Optional[str]) -> str:
    """P is a buy and S a sell. Everything else is "other": awards, exercises,
    tax withholding, gifts and conversions are kept but are not the feed."""
    return {"P": "buy", "S": "sell"}.get((code or "").strip().upper(), "other")


def trade_value(shares: Optional[float], price: Optional[float]) -> Optional[float]:
    """Shares times price, only when both are known. No price is no value, not
    zero: a purchase priced in a footnote is not a free one."""
    if shares is None or price is None:
        return None
    return shares * price


def stake_pct(shares: Optional[float], after: Optional[float], acquired: bool) -> Optional[float]:
    """The trade as a share of the holding it moved, 0-100.

    A buy is measured against what the insider owns after it, so 100 is a new
    position. A sale is measured against what they owned before it (after plus
    sold), so 100 is a full exit. Dividing a sale by the holding *after* it, as
    a buy is, has no ceiling: NVIDIA director Mark Stevens sold 1,366,000
    shares on 2026-09-18 and kept 970,531 in that trust, which reads 141%,
    where "sold 58% of the holding" is the fact.

    ``None`` when either number is missing, or when a buy is larger than the
    holding after it (the two then describe different ownership lines).
    """
    if shares is None or after is None or shares <= 0 or after < 0:
        return None
    if acquired:
        if after < shares:
            return None
        base = after
    else:
        base = after + shares
    if base <= 0:
        return None
    return round(shares / base * 100.0, 2)


def _price_ruled_out(tx: dict[str, Any]) -> bool:
    """A line whose price its own footnote rules out, by more than
    PRICE_RANGE_SLACK times either way. Phillips 66's general counsel filed
    3,523 shares at $2,110,482.00 on 2026-07-21 beside a footnote giving "prices
    ranging from $211.00 to $211.05": a decimal point dropped, which read as a
    $7.4 billion sale. Such a price is unknown, as a footnote-only one is."""
    price, note = tx.get("price"), tx.get("price_note")
    if price is None or not note:
        return False
    m = _PRICE_RANGE_RE.search(note)
    if not m:
        return False
    try:
        lo, hi = sorted(float(x.replace(",", "")) for x in m.groups())
    except ValueError:
        return False
    return lo > 0 and not (lo / PRICE_RANGE_SLACK <= price <= hi * PRICE_RANGE_SLACK)


def filing_trades(filing: dict[str, Any]) -> list[dict[str, Any]]:
    """A filing's non-derivative lines as trades.

    One trade per code, security and ownership line in a filing. A sale under
    a plan is routinely reported as one line per price band, and a filing can
    cover several days: NVIDIA's general counsel sold in three bands on
    2026-09-21, and Berkshire's Lennar filing of 09-30 bought on three days.
    One row per line would read as several decisions where there was one.

    The security is part of the key because one filing can buy two classes:
    Berkshire bought Lennar Class A at ~$82 and Class B at $80 on the same
    days, and summed they would be one row at a price neither traded at. So is
    the ownership line (direct, or a named trust): the holding after a sale is
    per line, and mixing two lines' holdings makes the stake meaningless.

    The price is the share-weighted average of the lines that carry one, and
    ``partial`` says when some did not. The holding after is the last one:
    the highest after a run of buys, the lowest after a run of sales, which
    does not depend on the order the lines were written in.
    """
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for tx in filing.get("tx") or []:
        key = (tx.get("code") or "", tx.get("security") or "",
               tx.get("own") or "", tx.get("nature") or "")
        groups.setdefault(key, []).append(tx)
    out: list[dict[str, Any]] = []
    for (code, security, own, nature), lines in groups.items():
        dates = sorted(t["date"] for t in lines if t.get("date"))
        counted = [t for t in lines if t.get("shares") is not None]
        shares = sum(t["shares"] for t in counted) if counted else None
        priced = [t for t in counted if t.get("price") is not None and not _price_ruled_out(t)]
        priced_shares = sum(t["shares"] for t in priced)
        value = sum(t["shares"] * t["price"] for t in priced) if priced else None
        if priced and priced_shares > 0:
            price: Optional[float] = value / priced_shares
        else:
            price = priced[0]["price"] if priced else None
        ad = lines[0].get("ad") or ("A" if code == "P" else "D")
        acquired = ad == "A"
        afters = [t["after"] for t in lines if t.get("after") is not None]
        after = (max(afters) if acquired else min(afters)) if afters else None
        notes: list[str] = []
        for t in lines:
            if t.get("price_note") and t["price_note"] not in notes:
                notes.append(t["price_note"])
        # Several bands' notes say the same thing with different numbers; the
        # first, and how many more there are, is what a tooltip can carry.
        note = notes[0] + (f" (+{len(notes) - 1} more)" if len(notes) > 1 else "") if notes else None
        out.append({
            "code": code, "date": dates[0] if dates else None,
            "date_last": dates[-1] if dates and dates[-1] != dates[0] else None,
            "security": security or None, "own": own or None, "nature": nature or None,
            "shares": shares, "price": price, "value": value,
            "partial": bool(priced) and len(priced) < len(lines),
            "after": after, "acquired": acquired,
            "stake": stake_pct(shares, after, acquired),
            "plan": any(t.get("plan") for t in lines),
            "offering": any(t.get("offering") for t in lines),
            "price_note": note,
        })
    return out


_UPPER_KEEP = {"LLC", "LP", "LLP", "L.P.", "PLC", "NV", "N.V.", "SA", "S.A.", "AG", "SE",
               "GP", "II", "III", "IV", "VI", "USA", "US", "NA", "N.A.", "ETF", "REIT"}
_STATE_SUFFIX = re.compile(r"\s*/[A-Z]{2,4}/?\s*$")
#: Yahoo's shortName is cut at about thirty characters and often ends on the
#: share class, cut too: the box's ticker cache names BKNG "Booking Holdings
#: Inc. Common St".
_SHARE_CLASS_TAIL = re.compile(
    r"\s+(?:Class [A-Z]\s+)?(?:Common\s+St\w*|Common|Ordinary\s+Sh\w*|Ord\s+Sh\w*)\.?$", re.I)


def yahoo_name(name: Optional[str]) -> str:
    """A followed company's name as the page shows it: Yahoo's, without a
    trailing share-class fragment."""
    text = " ".join((name or "").split())
    return _SHARE_CLASS_TAIL.sub("", text).rstrip(" ,") or text


def display_name(name: Optional[str]) -> str:
    """A name as filed, made readable: SEC writes many in capitals
    (``BUFFETT WARREN E``). Mixed-case names are the filer's own and are left
    alone; the surname-first order is SEC's and is kept, so a name on the page
    still matches the one on the filing."""
    text = " ".join((name or "").split())
    if not text or text != text.upper() or not any(c.isalpha() for c in text):
        return text
    words = []
    for word in text.split(" "):
        words.append(word if word in _UPPER_KEEP else word.capitalize())
    return " ".join(words)


def company_name(name: Optional[str]) -> str:
    """SEC's conformed name without its state tag: ``DANAHER CORP /DE/`` ->
    ``Danaher Corp``."""
    return display_name(_STATE_SUFFIX.sub("", (name or "").strip()))


def _roles(owners: list[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """Letters for the roles any reporting owner holds -- d director, o
    officer, t 10% owner, x other -- and the first title given."""
    letters = ""
    title: Optional[str] = None
    for flag, letter in (("director", "d"), ("officer", "o"), ("ten_pct", "t"), ("other", "x")):
        if any(o.get(flag) for o in owners):
            letters += letter
    for owner in owners:
        title = owner.get("title") or (owner.get("other_text") if owner.get("other") else None)
        if title:
            break
    return letters, title


def superseded(filings: dict[str, dict[str, Any]]) -> set[str]:
    """Accessions an amendment in *filings* replaces.

    A 4/A replaces the Form 4 it names -- same reporting owner, same period,
    filed on the 4/A's ``dateOfOriginalSubmission`` -- but only when the 4/A
    restates non-derivative lines itself. TPR's 4/A of 2026-09-03 restates
    only the derivative table, and NIKE's of the same day exists only to attach
    a power of attorney. Dropping an original wholesale for an amendment like
    the first would delete a trade nobody amended.
    """
    out: set[str] = set()
    for acc, amend in filings.items():
        if amend.get("form") != "4/A" or not amend.get("tx") or not amend.get("original"):
            continue
        ciks = {o.get("cik") for o in amend.get("owners") or [] if o.get("cik")}
        for other, orig in filings.items():
            if other == acc or orig.get("form") != "4":
                continue
            if orig.get("filed") != amend["original"] or orig.get("period") != amend.get("period"):
                continue
            if ciks & {o.get("cik") for o in orig.get("owners") or []}:
                out.add(other)
    return out


def issuer_rows(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Every trade in one issuer's record, as feed rows (compact keys).

    ``t`` ticker, ``n`` company, ``c`` CIK, ``a`` accession, ``f`` filed,
    ``d`` trade date (the first, when a filing spans days, and ``d2`` the
    last), ``k`` buy/sell/other, ``x`` code, ``o`` reporting owner,
    ``on`` joint filers beside them, ``ro`` role letters, ``ti`` title, ``sh``
    shares, ``px`` price, ``v`` value, ``pa`` partly priced, ``af`` holding
    after, ``sp`` stake %, ``pl`` 10b5-1, ``of`` offering, ``pn`` price note,
    ``am`` from a 4/A, ``io`` direct or indirect, ``sc`` security, ``pd`` the
    filing's rendered document. Keys starting ``_`` are internal and stripped
    before a response.
    """
    filings = rec.get("filings") or {}
    gone = superseded(filings)
    cik = rec.get("cik")
    ticker = rec.get("ticker") or ""
    name = rec.get("name") or ticker
    rows: list[dict[str, Any]] = []
    for acc, filing in filings.items():
        if acc in gone:
            continue
        # A filer's own submissions list the Form 4s it filed as another
        # company's owner: Berkshire's carry its Lennar purchases, which read
        # as Berkshire's insiders buying BRK-B. Whose trade it is, the filing
        # says; one naming no issuer is kept.
        issuer_cik = (filing.get("issuer") or {}).get("cik")
        if cik and issuer_cik and int(issuer_cik) != int(cik):
            continue
        owners = filing.get("owners") or []
        primary = owners[0] if owners else {}
        letters, title = _roles(owners)
        owner_ciks = sorted({o["cik"] for o in owners if o.get("cik")})
        for trade in filing_trades(filing):
            rows.append({
                "t": ticker or (filing.get("issuer") or {}).get("symbol") or "",
                "n": name, "c": cik, "a": acc,
                "f": filing.get("filed"), "d": trade["date"] or filing.get("period"),
                "d2": trade["date_last"],
                "k": kind(trade["code"]), "x": trade["code"],
                "o": display_name(primary.get("name")), "on": max(0, len(owners) - 1),
                "ro": letters, "ti": title,
                "sh": trade["shares"], "px": trade["price"], "v": trade["value"],
                "pa": trade["partial"], "af": trade["after"], "sp": trade["stake"],
                "pl": trade["plan"], "of": trade["offering"],
                "pn": (trade["price_note"] or "")[:320] or None,
                "am": filing.get("form") == "4/A", "io": trade["own"],
                "sc": trade["security"], "pd": filing.get("doc"),
                "_ow": owner_ciks or [display_name(primary.get("name"))],
                "_ts": filing.get("accepted") or "",
            })
    return rows


def window_start(today: dt.date, days: int) -> dt.date:
    """The first day of "the last *days* days": today counts as one of them."""
    return today - dt.timedelta(days=max(1, int(days)) - 1)


def public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Rows grouped by insider: rows whose filings share a reporting owner are
    one insider, however many entities co-signed. Berkshire Hathaway and
    Warren Buffett file Lennar's purchases jointly, as one buyer, not two."""
    parent: dict[Any, Any] = {}

    def find(x: Any) -> Any:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for row in rows:
        keys = row.get("_ow") or [row.get("o")]
        for key in keys[1:]:
            parent[find(key)] = find(keys[0])
        find(keys[0])
    out: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        keys = row.get("_ow") or [row.get("o")]
        out.setdefault(find(keys[0]), []).append(row)
    return list(out.values())


def cluster_buys(rows: Iterable[dict[str, Any]], *, min_insiders: int = 2) -> list[dict[str, Any]]:
    """Companies where at least *min_insiders* distinct insiders bought.

    Several insiders putting their own money in at once is the signal insider
    data is read for. Only open-market buys count: a purchase in an offering
    or private placement is the company raising money, and on openinsider's
    cluster list a biotech IPO's investors buying at the offer price read as
    a cluster of three. *rows* should already be cut to the window.
    """
    by_company: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("k") == "buy" and not row.get("of"):
            by_company.setdefault(row.get("c") or row.get("t"), []).append(row)
    clusters: list[dict[str, Any]] = []
    for company_rows in by_company.values():
        groups = _groups(company_rows)
        if len(groups) < min_insiders:
            continue
        who = []
        for group in groups:
            first = max(group, key=lambda r: r.get("v") or 0)
            known = [r["v"] for r in group if r.get("v") is not None]
            who.append({"o": first.get("o"), "on": first.get("on"), "ro": first.get("ro"),
                        "ti": first.get("ti"), "v": sum(known) if known else None,
                        "sh": sum(r.get("sh") or 0 for r in group),
                        "d": max(r.get("d") or "" for r in group) or None})
        who.sort(key=lambda w: -(w["v"] or 0))
        values = [w["v"] for w in who if w["v"] is not None]
        dates = [r.get("d") for r in company_rows if r.get("d")]
        head = company_rows[0]
        clusters.append({
            "t": head.get("t"), "n": head.get("n"), "c": head.get("c"),
            "insiders": len(groups), "buys": len(company_rows),
            "v": sum(values) if values else None,
            "sh": sum(r.get("sh") or 0 for r in company_rows),
            "first": min(dates) if dates else None, "last": max(dates) if dates else None,
            "who": who,
        })
    clusters.sort(key=lambda c: (-c["insiders"], -(c["v"] or 0), c["t"] or ""))
    return clusters


def summarise(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Counts and totals for the buys and the sells among *rows*. A side
    whose trades all lack a price has a value of ``None``, not 0."""
    rows = list(rows)
    out: dict[str, Any] = {}
    for side in ("buy", "sell"):
        mine = [r for r in rows if r.get("k") == side]
        values = [r["v"] for r in mine if r.get("v") is not None]
        out[side] = {
            "trades": len(mine),
            "value": sum(values) if values else None,
            "insiders": len(_groups(mine)) if mine else 0,
            "companies": len({r.get("c") or r.get("t") for r in mine}),
        }
    return out


def sort_newest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest filing first; within a filing day by acceptance time, then by
    the last trade date."""
    rows.sort(key=lambda r: (r.get("f") or "", r.get("_ts") or "", r.get("d2") or r.get("d") or ""),
              reverse=True)
    return rows


def select(rows: Iterable[dict[str, Any]], *, since: str, kind_: str, sort: str) -> list[dict[str, Any]]:
    """The feed: rows filed on or after *since*, of *kind_*, in *sort* order.

    The window is by filing date, which is when the trade became public, and
    "all" means buys and sells: the other codes are not the feed. Largest
    first puts an unpriced trade last, not first -- "could not be priced" is
    not "largest" -- and ties stay newest first, since the sort is stable.
    """
    kinds = ("buy", "sell") if kind_ == "all" else (kind_,)
    chosen = sort_newest([r for r in rows if (r.get("f") or "") >= since and r.get("k") in kinds])
    if sort != "newest":
        chosen.sort(key=lambda r: (r.get("v") is None, -(r.get("v") or 0.0)))
    return chosen


def form4_listing(doc: Any) -> list[dict[str, Any]]:
    """The Form 4s and 4/As in a submissions JSON's ``filings.recent``."""
    recent = ((doc or {}).get("filings") or {}).get("recent") if isinstance(doc, dict) else None
    if not isinstance(recent, dict):
        return []
    cols = {k: recent.get(k) or [] for k in ("form", "accessionNumber", "filingDate", "reportDate",
                                             "primaryDocument", "acceptanceDateTime")}
    out: list[dict[str, Any]] = []
    for i, form in enumerate(cols["form"]):
        if form not in _FORMS:
            continue

        def at(key: str) -> Optional[str]:
            seq = cols[key]
            return seq[i] if i < len(seq) and seq[i] else None

        acc, filed = at("accessionNumber"), at("filingDate")
        if acc and filed:
            out.append({"acc": acc, "form": form, "filed": filed, "period": at("reportDate"),
                        "doc": at("primaryDocument") or "", "accepted": at("acceptanceDateTime")})
    return out


def doc_name(primary_document: Optional[str]) -> Optional[str]:
    """The raw XML's file name: ``xslF345X06/form4.xml`` -> ``form4.xml``.
    ``None`` when the primary document is not XML at all."""
    name = (primary_document or "").rsplit("/", 1)[-1].strip()
    return name if name.lower().endswith(".xml") else None


def archive_url(cik: int, accession: str, name: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), folder=accession.replace("-", ""), name=name)


def universe_from_ticker_cache(blob: Any, lookup: Callable[[str], Optional[tuple[int, str]]]
                               ) -> dict[str, Any]:
    """The issuers to sweep: the followed companies, one entry per CIK.

    *blob* is ``cache/ticker_cache.json`` (``data`` -> group -> ticker ->
    record). A fund or an index is skipped on the record's ``"Quote Type"``;
    a record without one is a company until shown otherwise, since skipping a
    real issuer because one Yahoo answer came back thin is the worse error.
    *lookup* maps a ticker to ``(cik, SEC title)``; a ticker it does not know
    (a Tokyo listing, a delisted name) is skipped. Measured 2026-10-04: 308
    tickers, 51 funds, 42 without a CIK (40 Tokyo listings, EA, TCEHY), 215
    issuers.
    """
    from ystocker.data import is_non_equity

    data = blob.get("data") if isinstance(blob, dict) else None
    issuers: dict[int, dict[str, Any]] = {}
    non_equity: list[str] = []
    no_cik: list[str] = []
    seen: set[str] = set()
    for group in (data or {}).values():
        if not isinstance(group, dict):
            continue
        for ticker, record in group.items():
            if not isinstance(record, dict) or ticker in seen:
                continue
            seen.add(ticker)
            if is_non_equity(record.get("Quote Type")):
                non_equity.append(ticker)
                continue
            hit = lookup(ticker)
            if not hit:
                no_cik.append(ticker)
                continue
            cik = int(hit[0])
            entry = issuers.setdefault(cik, {"cik": cik, "tickers": [],
                                             "name": yahoo_name(record.get("Name")) or company_name(hit[1])})
            entry["tickers"].append(ticker)
    return {"issuers": sorted(issuers.values(), key=lambda e: e["tickers"][0]),
            "non_equity": sorted(non_equity), "no_cik": sorted(no_cik)}


def today_et(now: Optional[float] = None) -> dt.date:
    """The market's date, as earnings_calendar keys it."""
    from zoneinfo import ZoneInfo

    stamp = time.time() if now is None else now
    return dt.datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).date()


def is_due(rec: Optional[dict[str, Any]], now: float) -> bool:
    """Whether an issuer's record wants a new check. A failed check waits as
    long as a successful one: a CIK SEC answers with an error answers the same
    way every fifteen minutes."""
    if rec is None:
        return True
    checked = rec.get("checked")
    if checked:
        return now - float(checked) >= RECHECK_SECONDS
    failed = rec.get("failed")
    return not failed or now - float(failed) >= RECHECK_SECONDS


# ── Cache ───────────────────────────────────────────────────────────────────

_write_lock = threading.Lock()
_digest_lock = threading.Lock()
_digests: dict[int, tuple[float, dict[str, Any]]] = {}
_universe_memo: dict[str, Any] = {"mtime": None, "value": None, "ciks": set(), "by_ticker": {}}
_map_memo: dict[str, Any] = {"mtime": None, "value": None}


def _path(cik: int) -> Path:
    return CACHE_DIR / f"{int(cik)}.json"


def _read(cik: int) -> Optional[dict[str, Any]]:
    """The stored record, straight from disk, or ``None``."""
    try:
        rec = json.loads(_path(cik).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == CACHE_VER else None


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save(cik: int, rec: dict[str, Any], today: dt.date) -> None:
    """Write *rec*, merged with whatever is on disk now, then pruned.

    The sweep runs in the master and a reader's look-up in a worker, so two
    processes can refresh one issuer at once. Filings are unioned rather than
    replaced, so neither write drops the other's accessions, and the newer
    check's stamp and coverage win. Pruning comes after the merge, or the
    disk copy would hand back every filing it just aged out.
    """
    with _write_lock:
        disk = _read(cik)
        if disk:
            filings = dict(disk.get("filings") or {})
            filings.update(rec.get("filings") or {})
            rec["filings"] = filings
            unread = dict(disk.get("unread") or {})
            unread.update(rec.get("unread") or {})
            rec["unread"] = {a: u for a, u in unread.items() if a not in filings}
            if float(disk.get("checked") or 0) > float(rec.get("checked") or 0):
                rec["checked"] = disk["checked"]
                rec["covered_since"] = disk.get("covered_since")
        _prune(rec, today)
        _atomic_write(_path(cik), rec)


def _prune(rec: dict[str, Any], today: dt.date) -> None:
    floor = window_start(today, KEEP_DAYS).isoformat()
    rec["filings"] = {a: f for a, f in (rec.get("filings") or {}).items()
                      if (f.get("filed") or "") >= floor}
    rec["unread"] = {a: u for a, u in (rec.get("unread") or {}).items()
                     if (u.get("filed") or "") >= floor}


def digest(cik: int) -> Optional[dict[str, Any]]:
    """An issuer's rows and the facts the page reports about its record, from
    disk, memoised on the file's mtime. Never fetches.

    The rows, not the record, are kept in memory: the record's raw lines and
    owners are what the rows are made from, and holding both for ~215 issuers
    in every worker would double the cost for nothing.
    """
    path = _path(cik)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _digest_lock:
        hit = _digests.get(int(cik))
        if hit and hit[0] == mtime:
            return hit[1]
    rec = _read(cik)
    if rec is None:
        return None
    value = {
        "cik": int(cik),
        "ticker": rec.get("ticker"),
        "name": rec.get("name"),
        "checked": rec.get("checked"),
        "failed": rec.get("failed"),
        "covered_since": rec.get("covered_since"),
        "unread": sum(1 for u in (rec.get("unread") or {}).values() if u.get("n", 0) >= MAX_DOC_TRIES),
        "filings": len(rec.get("filings") or {}),
        "rows": issuer_rows(rec),
    }
    with _digest_lock:
        _digests[int(cik)] = (mtime, value)
    return value


def peek_universe() -> Optional[dict[str, Any]]:
    """The universe the sweep last resolved, or ``None`` before its first pass."""
    path = CACHE_DIR / UNIVERSE_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _universe_memo["mtime"] == mtime:
        return _universe_memo["value"]
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("issuers"), list):
        return None
    _universe_memo.update(mtime=mtime, value=value,
                          ciks={e["cik"] for e in value["issuers"]},
                          by_ticker={t: e["cik"] for e in value["issuers"] for t in e.get("tickers") or []})
    return value


def in_universe(cik: int) -> bool:
    """Whether the sweep keeps this issuer fresh (it is a followed company)."""
    return peek_universe() is not None and int(cik) in _universe_memo["ciks"]


def _map_peek() -> Optional[dict[str, tuple[int, str]]]:
    """SEC's ticker map as fundamentals last saved it, any age; never fetched
    here, since only the background may fetch it."""
    from ystocker import fundamentals

    path = fundamentals.TICKER_MAP_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _map_memo["mtime"] != mtime:
        try:
            parsed = fundamentals._parse_map(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None
        _map_memo.update(mtime=mtime, value=parsed)
    return _map_memo["value"]


def peek_cik(symbol: str) -> tuple[Optional[int], bool]:
    """``(cik, known)`` for a ticker without fetching. ``known`` is ``False``
    when no ticker map is readable at all, which is "cannot tell", not "no"."""
    if peek_universe() is not None and symbol in _universe_memo["by_ticker"]:
        return _universe_memo["by_ticker"][symbol], True
    mapping = _map_peek()
    if mapping is None:
        return None, False
    hit = mapping.get(symbol) or mapping.get(symbol.replace(".", "-"))
    return (hit[0] if hit else None), True


# ── Fetching ────────────────────────────────────────────────────────────────

_pace_lock = threading.Lock()
_last_request = 0.0


def _pace() -> None:
    """At most one request per REQUEST_SPACING_SECONDS from this process.
    The lock is held across the sleep, so the sweep and a look-up queue up
    behind each other rather than both reading the same stamp and firing."""
    global _last_request
    with _pace_lock:
        gap = time.monotonic() - _last_request
        if gap < REQUEST_SPACING_SECONDS:
            time.sleep(REQUEST_SPACING_SECONDS - gap)
        _last_request = time.monotonic()


def _after_fork_in_child() -> None:
    """Fresh locks for a forked gunicorn worker. The sweep runs in the master
    and holds ``_pace_lock`` through its 1.5 s sleep, so a worker forked mid-
    sweep would inherit it held: its first look-up would hang for good and
    every later one queue behind it (``sec13f._after_fork_in_child`` has the
    incident). ``_write_lock`` and ``_digest_lock`` are the master's too."""
    global _pace_lock, _write_lock, _digest_lock
    _pace_lock = threading.Lock()
    _write_lock = threading.Lock()
    _digest_lock = threading.Lock()


os.register_at_fork(after_in_child=_after_fork_in_child)


def _edgar_get(url: str) -> Any:
    from ystocker.sec13f import edgar_get

    return edgar_get(url)


def refresh_issuer(cik: int, *, ticker: Optional[str] = None, name: Optional[str] = None,
                   get: Optional[Callable[[str], Any]] = None,
                   pace: Optional[Callable[[], None]] = None,
                   now: Optional[float] = None) -> dict[str, int]:
    """Check one issuer: its submissions, then each Form 4 not yet cached.

    Returns request and filing counts. Raises ``CooldownActive`` after saving
    whatever it parsed, without marking the issuer checked, so the next pass
    resumes where this one stopped. A submissions request that fails marks the
    issuer failed, which holds it for as long as a successful check would.
    A document that fails is retried on later checks, up to
    :data:`MAX_DOC_TRIES`, and is reported as unread rather than vanishing.
    """
    import requests

    from ystocker import fetchguard

    get = get or _edgar_get
    pace = pace or _pace
    stamp = time.time() if now is None else now
    today = today_et(stamp)
    rec = _read(cik) or {"v": CACHE_VER, "cik": int(cik), "filings": {}, "unread": {}}
    rec["v"], rec["cik"] = CACHE_VER, int(cik)
    filings: dict[str, Any] = rec.setdefault("filings", {})
    unread: dict[str, Any] = rec.setdefault("unread", {})
    if ticker:
        rec["ticker"] = ticker
    if name:
        rec["name"] = name
    first = not rec.get("checked")
    since = window_start(today, FIRST_FILL_DAYS if first else WINDOW_DAYS).isoformat()
    stats = {"submissions": 0, "docs": 0, "new": 0, "unread": 0}

    pace()
    try:
        resp = get(SUBMISSIONS_URL.format(cik=int(cik)))
        stats["submissions"] = 1
        doc = resp.json()
        if not isinstance(doc, dict):
            raise ValueError(f"submissions is a {type(doc).__name__}, not an object")
    except fetchguard.CooldownActive:
        raise
    except (requests.RequestException, ValueError) as exc:
        rec["failed"], rec["why"] = stamp, f"{type(exc).__name__}: {exc}"[:200]
        _save(cik, rec, today)
        raise
    rec.pop("failed", None)
    rec.pop("why", None)
    rec["tickers"] = [t for t in (doc.get("tickers") or []) if isinstance(t, str)]
    if not rec.get("ticker"):
        rec["ticker"] = rec["tickers"][0] if rec["tickers"] else None
    if not rec.get("name"):
        rec["name"] = company_name(doc.get("name"))

    todo = [f for f in form4_listing(doc)
            if f["filed"] >= since and f["acc"] not in filings
            and (unread.get(f["acc"]) or {}).get("n", 0) < MAX_DOC_TRIES]
    todo.sort(key=lambda f: (f["filed"], f.get("accepted") or ""), reverse=True)
    batch, rest = todo[:MAX_DOCS_PER_CHECK], todo[MAX_DOCS_PER_CHECK:]
    interrupted: Optional[BaseException] = None

    def unreadable(item: dict[str, Any], why: str, tries: Optional[int] = None) -> None:
        count = tries if tries is not None else int((unread.get(item["acc"]) or {}).get("n", 0)) + 1
        unread[item["acc"]] = {"n": count, "ts": stamp, "filed": item["filed"], "why": why[:160]}
        stats["unread"] += 1

    for item in batch:
        name_ = doc_name(item["doc"])
        if name_ is None:
            unreadable(item, "not_xml", tries=MAX_DOC_TRIES)
            continue
        pace()
        try:
            raw = get(archive_url(cik, item["acc"], name_))
        except fetchguard.CooldownActive as exc:
            interrupted = exc                       # no request was made
            break
        except requests.RequestException as exc:
            stats["docs"] += 1
            unreadable(item, f"{type(exc).__name__}: {exc}")
            continue
        stats["docs"] += 1
        try:
            parsed = parse_form4(raw.content)
        except ValueError as exc:
            unreadable(item, f"{type(exc).__name__}: {exc}")
            continue
        parsed.update({"filed": item["filed"], "accepted": item.get("accepted"), "doc": item["doc"]})
        filings[item["acc"]] = parsed
        unread.pop(item["acc"], None)
        stats["new"] += 1

    if interrupted is None:
        rec["checked"] = stamp
        # Complete from the window's start, unless the per-check cap left
        # older filings for next time: then only from the day after them.
        if rest:
            newest_left = dt.date.fromisoformat(max(f["filed"] for f in rest))
            rec["covered_since"] = max(since, (newest_left + dt.timedelta(days=1)).isoformat())
        else:
            rec["covered_since"] = since
    _save(cik, rec, today)
    if interrupted is not None:
        raise interrupted
    return stats


def refresh_universe() -> dict[str, Any]:
    """Resolve the followed tickers to issuers and write ``_universe.json``.
    Background only: ``fundamentals.cik_for`` may fetch SEC's ticker map."""
    from ystocker import fundamentals

    blob = json.loads(TICKER_CACHE_FILE.read_text())
    value = universe_from_ticker_cache(blob, fundamentals.cik_for)
    value["ts"] = time.time()
    _atomic_write(CACHE_DIR / UNIVERSE_FILE, value)
    return value


_meta_memo: dict[int, tuple[float, Optional[dict[str, Any]]]] = {}


def _check_meta(cik: int) -> Optional[dict[str, Any]]:
    """An issuer's last check and failure, re-read only when its file changes.
    The sweep asks every fifteen minutes whether each of ~215 issuers is due,
    and parsing every record each time to read two stamps is the waste."""
    path = _path(cik)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _meta_memo.get(int(cik))
    if hit and hit[0] == mtime:
        return hit[1]
    rec = _read(cik)
    meta = {"checked": rec.get("checked"), "failed": rec.get("failed")} if rec else None
    _meta_memo[int(cik)] = (mtime, meta)
    return meta


def sweep_once(*, now: Optional[float] = None) -> dict[str, Any]:
    """One pass: every followed issuer due for a check, oldest check first.

    Stops at the first ``CooldownActive``: SEC asking us to slow down applies
    to the next issuer as much as to this one.
    """
    from ystocker import fetchguard

    started = time.monotonic()
    try:
        uni = refresh_universe()
    except Exception as exc:  # noqa: BLE001 - no universe, no pass; the loop retries
        log.warning("insiders: universe not resolved, pass skipped: %s", exc)
        return {"skipped": 1}
    stamp = time.time() if now is None else now
    entries = []
    for entry in uni["issuers"]:
        meta = _check_meta(entry["cik"])
        if is_due(meta, stamp):
            entries.append((float((meta or {}).get("checked") or 0), entry))
    entries.sort(key=lambda pair: pair[0])
    totals = {"due": len(entries), "checked": 0, "submissions": 0, "docs": 0, "new": 0,
              "unread": 0, "failed": 0, "stopped": 0}
    for _checked, entry in entries:
        try:
            stats = refresh_issuer(entry["cik"], ticker=entry["tickers"][0], name=entry.get("name"))
        except fetchguard.CooldownActive as exc:
            log.warning("insiders: SEC cool-down, pass stopped after %d of %d: %s",
                        totals["checked"], len(entries), exc)
            totals["stopped"] = 1
            break
        except Exception as exc:  # noqa: BLE001 - one issuer must not end the pass
            totals["failed"] += 1
            log.warning("insiders: %s (CIK %s) not checked: %s", entry["tickers"][0], entry["cik"], exc)
            continue
        totals["checked"] += 1
        for key in ("submissions", "docs", "new", "unread"):
            totals[key] += stats.get(key, 0)
    log.info("insiders: pass checked %d of %d due (universe %d issuers): %d submissions + %d Form 4 "
             "requests, %d new filings, %d unread, %d failed, %.0fs",
             totals["checked"], totals["due"], len(uni["issuers"]), totals["submissions"],
             totals["docs"], totals["new"], totals["unread"], totals["failed"],
             time.monotonic() - started)
    return totals


def start_background_thread() -> None:
    """Sweep the due issuers every SWEEP_INTERVAL_SECONDS, a minute after boot
    so a deploy does not open with a burst."""
    def _loop() -> None:
        time.sleep(BOOT_DELAY_SECONDS)
        while True:
            try:
                sweep_once()
            except Exception:  # noqa: BLE001 - the loop must outlive any one pass
                log.exception("insiders: sweep pass failed")
            time.sleep(SWEEP_INTERVAL_SECONDS)

    threading.Thread(target=_loop, name="insiders-sweep", daemon=True).start()
    log.info("insiders: sweep thread started (cache: %s)", CACHE_DIR)


# ── Look-ups by ticker ──────────────────────────────────────────────────────

_pending: deque[str] = deque()
_pending_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_current: Optional[str] = None
_failed: dict[str, float] = {}
_no_cik: dict[str, float] = {}


def _claim_path(symbol: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-." else "_" for c in symbol)[:24]
    return CACHE_DIR / f".lookup-{safe}"


def claimed(symbol: str, now: Optional[float] = None) -> bool:
    """Whether some process is fetching *symbol* now: the claim file is how a
    poll landing on the other gunicorn worker knows not to queue it again."""
    try:
        age = (time.time() if now is None else now) - _claim_path(symbol).stat().st_mtime
    except OSError:
        return False
    return age < CLAIM_TTL_SECONDS


def _claim(symbol: str) -> bool:
    path = _claim_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            if claimed(symbol):
                return False
            try:
                path.unlink()
            except OSError:
                return False
    return False


def _release(symbol: str) -> None:
    try:
        _claim_path(symbol).unlink()
    except OSError:
        pass


def recently_failed(symbol: str, now: Optional[float] = None) -> bool:
    stamp = _failed.get(symbol)
    return stamp is not None and (time.time() if now is None else now) - stamp < FAILURE_PAUSE_SECONDS


def no_cik(symbol: str) -> bool:
    """Whether a look-up found that SEC lists no such ticker."""
    return symbol in _no_cik


def queued(symbol: str) -> bool:
    with _pending_lock:
        return symbol in _pending or symbol == _current


def kick(symbol: str) -> str:
    """Queue a look-up of one company for this process's worker.

    Returns ``"queued"``, ``"already"`` (queued here, or being fetched by
    another process), ``"full"`` (:data:`MAX_PENDING` waiting) or ``"capped"``
    (today's look-ups are spent). The worker clears ``_worker`` under the same
    lock before it exits, so a ticker queued as it finishes is never stranded.
    """
    global _worker
    from ystocker import quota

    with _pending_lock:
        if symbol in _pending or symbol == _current:
            return "already"
        if len(_pending) >= MAX_PENDING:
            return "full"
    if claimed(symbol):
        return "already"
    if not quota.try_consume_insider_lookup():
        return "capped"
    with _pending_lock:
        if symbol in _pending or symbol == _current:
            return "already"
        _pending.append(symbol)
        if _worker is None:
            _worker = threading.Thread(target=_drain, name="insiders-lookup", daemon=True)
            _worker.start()
    return "queued"


def _lookup(symbol: str) -> None:
    from ystocker import fundamentals

    hit = fundamentals.cik_for(symbol)           # may fetch the ticker map, weekly
    if not hit:
        _no_cik[symbol] = time.time()
        return
    cik = int(hit[0])
    uni = peek_universe() or {}
    entry = next((e for e in uni.get("issuers") or [] if e["cik"] == cik), None)
    refresh_issuer(cik, ticker=(entry or {}).get("tickers", [symbol])[0],
                   name=(entry or {}).get("name"))


def _drain() -> None:
    global _worker, _current
    from ystocker import fetchguard

    while True:
        with _pending_lock:
            if not _pending:
                _worker = None
                _current = None
                return
            symbol = _pending.popleft()
            _current = symbol
        if not _claim(symbol):
            continue
        try:
            _lookup(symbol)
            _failed.pop(symbol, None)
        except fetchguard.CooldownActive as exc:
            _failed[symbol] = time.time()
            log.info("insiders: look-up of %s waits out a cool-down: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001 - a look-up must not kill the worker
            _failed[symbol] = time.time()
            log.warning("insiders: look-up of %s failed: %s", symbol, exc)
        finally:
            _release(symbol)


# ── Views (request path) ────────────────────────────────────────────────────

def _extra_ciks(known: set[int]) -> list[int]:
    """Cached issuers outside the universe: companies readers looked up."""
    out: list[int] = []
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return out
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext == ".json" and stem.isdigit() and int(stem) not in known:
            out.append(int(stem))
    return out


def feed_view(*, days: int, kind_: str, sort: str, now: Optional[float] = None,
              limit: int = MAX_FEED_ROWS) -> dict[str, Any]:
    """The market-wide feed, cluster buys and coverage, from the cache only.

    ``status`` is ``"warming"`` while some followed issuer has never been
    checked: the first pass is still reaching it. ``coverage`` says how many of
    the universe are in and from when the record is complete
    (``complete_from``), because right after the first pass a 90-day view holds
    30 days, and an empty stretch must not read as a quiet one.
    """
    stamp = time.time() if now is None else now
    today = today_et(stamp)
    since = window_start(today, days).isoformat()
    uni = peek_universe()
    universe = {e["cik"]: e for e in (uni or {}).get("issuers") or []}
    rows: list[dict[str, Any]] = []
    pending = failed = unread = 0
    checked: list[float] = []
    covered: list[str] = []
    for cik in universe:
        dg = digest(cik)
        if dg is None or (not dg["checked"] and not dg["failed"]):
            pending += 1
        elif not dg["checked"]:
            failed += 1
        else:
            checked.append(float(dg["checked"]))
            if dg["covered_since"]:
                covered.append(dg["covered_since"])
            unread += dg["unread"]
        if dg is not None:
            rows.extend(dg["rows"])
    extras = 0
    for cik in _extra_ciks(set(universe)):
        dg = digest(cik)
        if dg is None or not dg["checked"] or stamp - float(dg["checked"]) > LOOKUP_FRESH_SECONDS:
            continue
        extras += 1
        rows.extend(dg["rows"])

    window = [r for r in rows if (r.get("f") or "") >= since]
    chosen = select(window, since=since, kind_=kind_, sort=sort)
    clusters = cluster_buys(window)[:MAX_CLUSTERS]
    shown = chosen[:limit]
    status = "warming" if uni is None or pending else "ok"
    return {
        "status": status,
        "days": days, "kind": kind_, "sort": sort,
        "since": since, "today": today.isoformat(),
        "rows": [public(r) for r in shown],
        "total": len(chosen),
        "truncated": len(chosen) > limit,
        "clusters": clusters,
        "summary": summarise(window),
        "coverage": {
            "universe": len(universe),
            "checked": len(checked),
            "pending": pending,
            "failed": failed,
            "unread": unread,
            "extra": extras,
            "oldest_check": min(checked) if checked else None,
            "newest_check": max(checked) if checked else None,
            "complete_from": max(covered) if covered else None,
            "universe_as_of": (uni or {}).get("ts"),
        },
        "present": sorted({r["t"] for r in shown if r.get("t")} | {c["t"] for c in clusters if c.get("t")}),
        "source": SOURCE,
    }


def company_view(cik: int, *, days: int, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """One issuer's trades of every kind, newest first, or ``None`` if never
    fetched."""
    dg = digest(cik)
    if dg is None:
        return None
    stamp = time.time() if now is None else now
    since = window_start(today_et(stamp), days).isoformat()
    window = sort_newest([r for r in dg["rows"] if (r.get("f") or "") >= since])
    return {
        "cik": dg["cik"], "ticker": dg["ticker"], "name": dg["name"],
        "checked": dg["checked"], "failed": dg["failed"],
        "covered_since": dg["covered_since"], "unread": dg["unread"],
        "days": days, "since": since,
        "rows": [public(r) for r in window],
        "summary": summarise(window),
        "others": sum(1 for r in window if r.get("k") == "other"),
        "stale": bool(dg["checked"]) and stamp - float(dg["checked"]) >= RECHECK_SECONDS,
        "source": SOURCE,
    }
