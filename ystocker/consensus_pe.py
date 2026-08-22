"""
ystocker.consensus_pe
~~~~~~~~~~~~~~~~~~~~~
Published *consensus* forward P/E history — the one basis of forward multiple
that ``valuation.py`` could not build itself.

Why this module exists
----------------------
``valuation.py`` computes a cap-weighted forward P/E for SPY and QQQ bottom-up
from constituents, which is genuinely forward-looking but necessarily starts on
the day of the first snapshot: nothing in the app can say what the consensus was
last March. Its docstring concluded that no historical consensus series is
obtainable free. That is right for SPY and QQQ *as this app computes them*, and
too strong for the indices themselves:

* **S&P 500** — FactSet publishes the forward 12-month P/E in free, parseable
  prose in its Earnings Insight posts. Roughly a hundred dated observations from
  2017 on, clustered inside earnings season (these posts are not weekly
  year-round, so the series is dense Jan/Feb, Apr, Jul/Aug and Oct/Nov and empty
  between).
* **Nasdaq-100** — Siblis Research's free tier returns month-end forward P/E for
  NDX. Only a handful of points and only from Dec 2023, but it is the sole free
  forward NDX figure that exists; ~15 other candidates were checked and every
  one was either trailing, paywalled, or login-gated.

Neither is a backfill of the app's own SPY/QQQ numbers and they must never be
spliced onto them. Two separate reasons:

1. *Different construction.* FactSet aggregates its own analyst collection over
   the full index; this app takes Yahoo's per-name forward P/E over whatever
   constituents ``ticker_cache.json`` happens to hold. They land close for the
   S&P 500 (FactSet 20.0 on 2026-08-07 against 19.8 computed here), which makes
   splicing tempting and still wrong — the agreement is a fact about today, not
   a guarantee, and a spliced line hides the seam.
2. *Wildly different for NDX.* Siblis reads 25.2x on 2026-06-30 where this app
   reads 20.3x — a 24% gap, because the constituent coverage and the estimate
   source both differ. Drawn as one line that would invent a cliff that never
   happened, so the NDX series is plotted as its own sparse, dashed marker
   series and labelled as a second opinion rather than as history.

Two traps, both hit during development
--------------------------------------
**FactSet rate-limits with HTTP 429, not 404.** A naive ``except`` that maps any
failure to "no data" turns throttling into apparent absence: the first harvest
run lost 18 posts that way, including whole years, and the series looked merely
sparse rather than broken. 429 is therefore retried with backoff and an
exhausted retry is logged as an error and *keeps the baseline*, never silently
shortens the series.

**FactSet publishes the same sentence for other markets.** The Canadian edition
says "The forward 12-month P/E ratio is 13.4, which is below the 5-year average
(14.9)..." about the S&P/TSX. Matching on the sentence alone pulled five TSX
readings into the S&P 500 series at 11-13x, sitting 4-5 turns under their
neighbours. So a match is accepted only when the nearest index named *before* it
is the S&P 500 — see :func:`parse_factset_fwd_pe`. The slug filter is a second
line of defence, not the primary one, because slugs are not reliably regional.

Storage
-------
Unlike ``forward_history``, this is published data and re-derivable, so it is not
observed state and does not belong in DynamoDB. The bulk lives in a committed
baseline file (``data/consensus_fwd_pe.json``) so a fresh box serves the full
history immediately instead of scraping ~570 posts on first boot; the fetchers
then top it up. Baseline and network results are merged by date, newest winning.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_BASELINE_FILE = Path(__file__).parent / "data" / "consensus_fwd_pe.json"

_FACTSET_AUTHOR = "https://insight.factset.com/author/john-butters"
_FACTSET_POST = "https://insight.factset.com/{slug}"
_SIBLIS_NDX = ("https://siblisresearch.supabase.co/functions/v1/free-data-api"
               "/v1/NDX/pe-forward")

# An index-level forward P/E outside this band is a parse error, not a market
# event. The same guard rail as _PE_MIN/_PE_MAX in valuation.py, deliberately
# duplicated so a bad parse cannot reach the payload even if this module is used
# on its own.
_PE_MIN, _PE_MAX = 5.0, 60.0

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_SESSION = requests.Session()
_SESSION.trust_env = False  # system proxies cause silent timeouts (see fed.py)
_SESSION.headers.update({"User-Agent": _UA, "Accept": "text/html,application/json,*/*"})

# How many posts to look at on an incremental run. The author index lists the
# most recent posts first, so a handful is enough to catch anything published
# since the last refresh while keeping this to one request on a normal day.
_RECENT_LIMIT = 12
_FETCH_BUDGET = 6  # post bodies per refresh; earnings season adds ~1/week


# ---------------------------------------------------------------------------
# FactSet — S&P 500 forward 12-month P/E
# ---------------------------------------------------------------------------

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_HIT = re.compile(r"forward 12-month P/E ratio")
# The value clause. Two published phrasings: the bare "...ratio is 20.0" used in
# the blog posts and the "...ratio for the S&P 500 is 20.0" used in the PDF text.
_VAL = re.compile(r"^(?: for the S&P 500)? is (\d{1,2}\.\d)")

# Index names FactSet writes about. The *last* one before a match is the subject
# of that sentence, which is what separates the S&P 500 reading from the TSX one.
_INDEX_NAMES = re.compile(
    r"(S&P 500|S&P/TSX[A-Za-z ]*|STOXX(?: Europe)?(?: 600)?|TOPIX|Nikkei[ 0-9]*|"
    r"FTSE[ 0-9A-Za-z]*|MSCI[ A-Za-z]*|S&P/ASX[ 0-9]*|DAX|CAC[ 0-9]*|"
    r"S&P 400|S&P 600|Russell[ 0-9]*|Nasdaq[- ]100|Dow 30)")

# Non-US editions of the same recurring post. Belt and braces only.
_FOREIGN_SLUG = re.compile(
    r"(canada|canadian|europe|european|japan|japanese|china|chinese|asia|"
    r"emerging|britain|uk-|india|australia|tsx|stoxx|topix|nikkei)", re.I)

# Subset language immediately before the phrase. The nearest-index test cannot
# catch these because a sector is not an index name: "The S&P 500 is expensive.
# The Energy sector forward 12-month P/E ratio is 13.1" still nearest-names the
# S&P 500, yet 13.1 is Energy's multiple, not the index's.
_SUBSET_BEFORE = re.compile(
    r"(sector|industry|Magnificent|equal[- ]weight|excluding|ex-|"
    r"Energy|Financials|Utilities|Materials|Industrials|Information Technology|"
    r"Staples|Discretionary|Health Care|Real Estate|Communication Services|"
    r"small[- ]cap|mid[- ]cap|large[- ]cap)[^.]{0,60}$", re.I)

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_DATE_IN_SLUG = re.compile(r"-([a-z]+)-(\d{1,2})-(\d{4})$")
_PUBLISHED = re.compile(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_SLUG_LINK = re.compile(r'href="https://insight\.factset\.com/([a-z0-9][a-z0-9\-]{10,})"')


def _plain(body: str) -> str:
    """HTML to single-spaced plain text, entities resolved.

    Entities matter: the sentence contains ``S&amp;P 500`` in source, so matching
    on the raw HTML silently never fires.
    """
    return html.unescape(_WS.sub(" ", _TAGS.sub(" ", body)))


def parse_factset_fwd_pe(body: str) -> Optional[float]:
    """The S&P 500 forward 12-month P/E stated in one FactSet post, or None.

    Accepts a match only when the nearest index named before it is the S&P 500
    *and* no sector/subset qualifier sits immediately in front of it. Neither
    test alone is enough: without the first, the Canadian edition of the same
    post contributes S&P/TSX readings at 11-13x; without the second, the sector
    paragraphs contribute Energy at ~13x. Both pass any sentence-shape test,
    including the 5-year/10-year average clause, because the prose is identical.
    """
    text = _plain(body)
    for m in _HIT.finditer(text):
        got = _VAL.match(text[m.end():])
        if not got:
            continue
        # "...ratio for the S&P 500 is N" names its own subject.
        explicit = text[m.end():m.end() + 22].startswith(" for the S&P 500")
        if not explicit:
            names = _INDEX_NAMES.findall(text[:m.start()])
            if not names or names[-1] != "S&P 500":
                continue
            if _SUBSET_BEFORE.search(text[max(0, m.start() - 90):m.start()]):
                continue
        value = float(got.group(1))
        if not (_PE_MIN <= value <= _PE_MAX):
            log.warning("consensus_pe: FactSet value %.1f outside %.0f-%.0f — ignored",
                        value, _PE_MIN, _PE_MAX)
            continue
        return value
    return None


def _slug_date(slug: str, body: str = "") -> Optional[str]:
    """ISO date for a post, from its slug where possible, else its JSON-LD."""
    m = _DATE_IN_SLUG.search(slug)
    if m and m.group(1) in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    pub = _PUBLISHED.search(body)
    return pub.group(1) if pub else None


def _get(url: str, tries: int = 4, timeout: int = 25) -> Optional[str]:
    """GET with backoff that distinguishes throttling from absence.

    Returns None for both, but only after retrying a 429 — and logs the two
    differently, because "FactSet is rate-limiting us" and "that post does not
    exist" call for opposite responses from the caller.
    """
    for attempt in range(tries):
        try:
            resp = _SESSION.get(url, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("consensus_pe: %s attempt %d failed: %s", url, attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 429:
            wait = 3 * (attempt + 1)
            log.info("consensus_pe: 429 from %s, retrying in %ds", url, wait)
            time.sleep(wait)
            continue
        log.warning("consensus_pe: %s returned HTTP %d", url, resp.status_code)
        return None
    log.warning("consensus_pe: %s still throttled after %d tries — keeping baseline",
                url, tries)
    return None


def _recent_factset_slugs() -> list[str]:
    """Slugs of the most recent Earnings Insight posts, newest first.

    Restricted to slugs that carry their own date. The recurring weekly post is
    always ``sp-500-earnings-season-update-<month>-<d>-<yyyy>``, so a dated slug
    can be checked against what we already hold for free; an undated feature
    article cannot, which means it would be re-downloaded on every single refresh
    forever and would exhaust the fetch budget before reaching the post that
    actually matters. That is exactly what happened on the first run here.
    """
    body = _get(_FACTSET_AUTHOR)
    if not body:
        return []
    out: list[str] = []
    for slug in _SLUG_LINK.findall(body):
        if slug in out or _FOREIGN_SLUG.search(slug):
            continue
        if "earnings" not in slug and "valuation" not in slug:
            continue
        if not _DATE_IN_SLUG.search(slug):
            continue
        out.append(slug)
    return out[:_RECENT_LIMIT]


def fetch_spx_updates(known: set[str]) -> dict[str, float]:
    """Any S&P 500 consensus readings newer than the baseline.

    Only posts whose date is not already held are fetched, so the steady-state
    cost is one request for the index page. ``_FETCH_BUDGET`` caps a cold start
    from turning into a scrape of the whole archive inside a web process — the
    committed baseline is what makes the archive unnecessary.
    """
    found: dict[str, float] = {}
    slugs = _recent_factset_slugs()
    if not slugs:
        return found
    spent = 0
    for slug in slugs:
        stamp = _slug_date(slug)
        if stamp and stamp in known:
            continue
        if spent >= _FETCH_BUDGET:
            log.info("consensus_pe: fetch budget reached, %d slugs left for next run",
                     len(slugs) - slugs.index(slug))
            break
        body = _get(_FACTSET_POST.format(slug=slug))
        spent += 1
        if not body:
            continue
        value = parse_factset_fwd_pe(body)
        if value is None:
            continue
        stamp = stamp or _slug_date(slug, body)
        if not stamp:
            continue
        found[stamp] = value
        log.info("consensus_pe: FactSet %s = %.1fx (%s)", stamp, value, slug)
    return found


# ---------------------------------------------------------------------------
# Siblis — Nasdaq-100 forward P/E, month-end
# ---------------------------------------------------------------------------

def fetch_ndx_series() -> dict[str, float]:
    """Month-end NDX forward P/E from Siblis' free tier.

    The free tier ignores date parameters and returns whatever month-ends it
    feels like exposing, so this is a whole-series replace-and-merge rather than
    an incremental fetch. One cheap request.
    """
    body = _get(_SIBLIS_NDX, tries=2)
    if not body:
        return {}
    try:
        blob = json.loads(body)
    except ValueError as exc:
        log.warning("consensus_pe: Siblis response was not JSON: %s", exc)
        return {}
    rows = blob.get("data")
    if not isinstance(rows, list):
        log.warning("consensus_pe: Siblis payload has no data array")
        return {}
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        # The key is literally "trading_day (EOD)"; tolerate a rename.
        stamp = next((v for k, v in row.items()
                      if isinstance(v, str) and k.startswith("trading_day")), None)
        value = row.get("value")
        if not stamp or not isinstance(value, (int, float)):
            continue
        try:
            datetime.strptime(stamp, "%Y-%m-%d")
        except ValueError:
            continue
        if _PE_MIN <= float(value) <= _PE_MAX:
            out[stamp] = round(float(value), 2)
    log.info("consensus_pe: Siblis returned %d NDX month-end points", len(out))
    return out


# ---------------------------------------------------------------------------
# Baseline + assembly
# ---------------------------------------------------------------------------

def _load_baseline() -> dict[str, dict[str, float]]:
    """The committed history, as ``{"spx": {date: pe}, "ndx": {...}}``."""
    try:
        blob = json.loads(_BASELINE_FILE.read_text())
    except FileNotFoundError:
        log.warning("consensus_pe: baseline %s missing", _BASELINE_FILE)
        return {"spx": {}, "ndx": {}}
    except (OSError, ValueError) as exc:
        log.warning("consensus_pe: baseline unreadable: %s", exc)
        return {"spx": {}, "ndx": {}}
    out: dict[str, dict[str, float]] = {"spx": {}, "ndx": {}}
    for key in ("spx", "ndx"):
        for row in (blob.get(key) or {}).get("rows") or []:
            try:
                stamp, value = row[0], float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if _PE_MIN <= value <= _PE_MAX:
                out[key][stamp] = value
    return out


def _block(points: dict[str, float], label: str, source: str) -> Optional[dict[str, Any]]:
    """Sorted {dates, values} block, or None when there is nothing to plot."""
    if not points:
        return None
    dates = sorted(points)
    return {
        "dates": dates,
        "values": [points[d] for d in dates],
        "label": label,
        "unit": "x",
        "source": source,
    }


def get_consensus_series() -> dict[str, Optional[dict[str, Any]]]:
    """Both published consensus series, baseline merged with anything new.

    Never raises: a page that loses this section is a worse outcome than one
    that shows the committed history alone, so every failure degrades to the
    baseline.
    """
    base = _load_baseline()
    spx, ndx = dict(base["spx"]), dict(base["ndx"])

    try:
        spx.update(fetch_spx_updates(set(spx)))
    except Exception as exc:  # noqa: BLE001 - degrade to baseline
        log.warning("consensus_pe: FactSet update failed: %s", exc)
    try:
        ndx.update(fetch_ndx_series())
    except Exception as exc:  # noqa: BLE001 - degrade to baseline
        log.warning("consensus_pe: Siblis fetch failed: %s", exc)

    today = date.today().isoformat()
    spx = {d: v for d, v in spx.items() if d <= today}
    ndx = {d: v for d, v in ndx.items() if d <= today}

    log.info("consensus_pe: S&P 500 %d points, NDX %d points", len(spx), len(ndx))
    return {
        "spx_consensus_fwd": _block(
            spx, "S&P 500 Forward P/E (consensus)", "FactSet Earnings Insight"),
        "ndx_consensus_fwd": _block(
            ndx, "Nasdaq-100 Forward P/E (consensus)", "Siblis Research (free tier)"),
    }


# ---------------------------------------------------------------------------
# Self-test — the parser is the part that silently corrupts, so it is checked
# against real published prose, including the two decoys that fooled it once.
# ---------------------------------------------------------------------------

_CASES: tuple[tuple[str, Optional[float], str], ...] = (
    ("<p>For CY 2026, analysts are predicting growth of 30.0%. The S&amp;P 500 rose. "
     "The forward 12-month P/E ratio is 20.0, which is above the 5-year average "
     "(19.9) and above the 10-year average (19.0). However, this P/E ratio is below "
     "the forward P/E ratio of 20.4 recorded at the end of the second quarter.</p>",
     20.0, "index sentence, with a quarter-end decoy in the same paragraph"),
    ("<p>The forward 12-month P/E ratio for the S&amp;P 500 is 19.8. This P/E ratio "
     "is above the 5-year average of 18.6.</p>",
     19.8, "PDF phrasing, names its own subject"),
    ("<p>The S&amp;P/TSX Composite is cheap. For CY 2023, analysts are predicting a "
     "decline of -0.6%. The forward 12-month P/E ratio is 13.4, which is below the "
     "5-year average (14.9) and below the 10-year average (15.2).</p>",
     None, "Canadian edition — identical sentence, TSX subject"),
    ("<p>The S&amp;P 500 is expensive. The Energy sector forward 12-month P/E ratio "
     "is 13.1, which is below the 5-year average (15.1) and below the 10-year "
     "average (15.2).</p>",
     None, "sector sentence — nearest-names the S&P 500, caught by the subset guard"),
    ("<p>Nothing about multiples here at all.</p>", None, "no match"),
    ("<p>The S&amp;P 500 forward 12-month P/E ratio is 99.9.</p>",
     None, "outside the sanity band"),
)


def _selftest() -> int:
    bad = 0
    for body, want, why in _CASES:
        got = parse_factset_fwd_pe(body)
        ok = got == want
        if not ok:
            bad += 1
        print(f"  [{'ok ' if ok else 'FAIL'}] {want!r:>6} got {got!r:>6}  {why}")
    base = _load_baseline()
    print(f"  baseline: spx={len(base['spx'])} ndx={len(base['ndx'])}")
    if not base["spx"]:
        print("  [FAIL] baseline carries no S&P 500 history")
        bad += 1
    print("PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
