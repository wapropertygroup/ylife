"""
ystocker.futu
~~~~~~~~~~~~~
Deep-links the /history FuTu button into the Futubull native app, keeping the
existing web quote page as the fallback.

Why this is not just a longer href
----------------------------------
The obvious approach -- leave the anchor pointing at futunn.com and let the phone
decide -- cannot work, and fails *silently*. Futu does serve
``/.well-known/apple-app-site-association``, but the only paths it claims are
``/qq_conn/1101195293/*``, ``/weixin_ios/*``, ``/app/*`` and ``/deeplink/*``.
``/en/stock/*`` is **not** among them, so the quote URL the button already used
can never become a universal link however it is written: iOS opens it in Safari
because Futu never asked for that path.

Every constant below was read off the live site rather than guessed, because a
wrong scheme is a button that does nothing and says nothing:

* The scheme is ``ftnn``, taken from the App Links tags Futu serves on its own
  ``/deeplink/`` bridge -- ``<meta property="al:ios:url" content="ftnn://">``,
  ``al:android:package`` = ``cn.futu.trader``, ``al:ios:app_store_id`` =
  ``592031984``. ``ftmm`` is the same thing for moomoo, per the mobile bundle's
  ``function(e){return e?"ftmm":"ftnn"}``; this module links Futubull because
  that is what ``futunn.com`` is.
* The quote path is ``quote/stockDetail/{stockId}/1``, a literal template in the
  mobile quote bundle, which also appears fully resolved in the quote page's own
  AppsFlyer link: ``af_dp=ftnn://quote/stockDetail/203319/1`` on SMCI-US.

``stockId`` is the trap: it is Futu's own opaque id, **not** the ticker. SMCI is
203319, AAPL 205189, and HK / A-share ids are 14 digits (00700-HK is
54047868453564), so they are strings and would overflow anything narrower.
``quote/stockDetail/{stockId}/1`` is the only quote template in the bundle -- no
symbol-based path exists -- so the id has to be looked up and remembered.

Resolution therefore reads the quote page once per symbol and keeps the answer
forever, since an id never changes. Two consequences shape the API:

* **The request path peeks and never fetches.** /history is a page render, and a
  1.3 MB vendor fetch does not belong in front of one -- the same rule the AI
  brief follows. :func:`cached_stock_id` is what the route calls;
  :func:`resolve_stock_id` is what the warm endpoint calls.
* **Every failure degrades to the previous behaviour.** No id means the template
  renders exactly the plain web link that shipped before this module existed,
  which is also the correct no-app-installed fallback. So a dead vendor, an open
  breaker, a changed page shape or an unmapped venue all cost the app handoff and
  nothing else.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from . import fetchguard

log = logging.getLogger(__name__)

# ── Verified app identifiers (see module docstring for provenance) ───────────

SCHEME = "ftnn"                     # Futubull; moomoo is "ftmm"
ANDROID_PACKAGE = "cn.futu.trader"
IOS_APP_STORE_ID = "592031984"

_QUOTE_PATH = "quote/stockDetail/{stock_id}/1"
_WEB_BASE = "https://www.futunn.com/en/stock/"

# Futu's own ids are decimal digits at every venue checked (US 6, HK/SH/SZ 14).
# Enforced rather than assumed: the id is interpolated into a URL, so anything
# else is either a parse that went wrong or something that must not be trusted.
_ID_RE = re.compile(r"\A[0-9]{1,20}\Z")

# ``window.__INITIAL_STATE__={"stock_info":{...,"marketLabel":"US",...,
# "stockCode":"SMCI","stockId":"203319",...}`` — the requested company. Matching
# is scoped to that object rather than to the page, because the page also carries
# dozens of *other* stockIds in its "hot stocks" rails and a positional match
# could link to the wrong company, which is the one failure worse than no link.
# 600 chars is ~4x the observed distance from the key to stockId at every venue
# checked, and bounding it keeps a later rail entry from being read as stock_info.
# Deliberately *not* matched as a balanced ``{...}`` object: stock_info's closing
# brace is thousands of characters away, past nested objects, so requiring it made
# the pattern fail on every real page while still looking correct.
_STOCK_INFO_ANCHOR = re.compile(r'"stock_info"\s*:\s*\{')
_STOCK_INFO_WINDOW = 600
_CODE_RE = re.compile(r'"stockCode"\s*:\s*"([^"]{1,20})"')
_ID_FIELD_RE = re.compile(r'"stockId"\s*:\s*"?([0-9]{1,20})"?')
_MARKET_RE = re.compile(r'"marketLabel"\s*:\s*"([A-Za-z]{2,4})"')

_PROVIDER = "futu"
_TIMEOUT_SECONDS = fetchguard.env_float("FUTU_TIMEOUT_SECONDS", 12.0, 1.0)

_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# ── Amplification gate ──────────────────────────────────────────────────────
#
# /api/futu/<ticker> is public and unauthenticated, and _futu_symbol() maps any
# bare string to "<IT>-US" -- so without a ceiling, enumerating invented tickers
# turns this box into an outbound amplifier: one cheap request in, one ~1.3 MB
# fetch of futunn.com out, repeated. The per-symbol FailureBackoff does not cover
# it, because every invented symbol is a *new* key and so always ready.
#
# The breaker in fetchguard is the wrong tool too: it reacts after Futu starts
# answering 429, by which point we are the ones who look abusive. This is a
# ceiling on *new* resolutions instead. Cache hits are unaffected, so ordinary
# reading never touches it. In-process scope for the same reason the breaker uses
# it -- the window is far shorter than the gap between deploys.
_RESOLVE_WINDOW_SECONDS = fetchguard.env_float("FUTU_RESOLVE_WINDOW_SECONDS", 60.0, 1.0)
_RESOLVE_MAX_PER_WINDOW = fetchguard.env_int("FUTU_RESOLVE_MAX_PER_WINDOW", 10, 1)

_rate_lock = threading.Lock()
_rate_window_start = 0.0
_rate_count = 0


def _resolve_slot_available() -> bool:
    """Claim one new-resolution slot, or report the window is full."""
    global _rate_window_start, _rate_count
    now = time.monotonic()
    with _rate_lock:
        if now - _rate_window_start >= _RESOLVE_WINDOW_SECONDS:
            _rate_window_start, _rate_count = now, 0
        if _rate_count >= _RESOLVE_MAX_PER_WINDOW:
            return False
        _rate_count += 1
        return True

# ── Persistent id cache ─────────────────────────────────────────────────────
#
# Not a TTL cache: a Futu stockId is permanent, so there is nothing to expire and
# an entry is worth keeping across a box replacement. Same shape as
# peer_groups.json -- one flat dict, atomic replace on write.

_IDS_PATH = Path(__file__).parent.parent / "cache" / "futu_ids.json"
_lock = threading.Lock()
_ids: dict[str, str] | None = None


def _load_locked() -> dict[str, str]:
    """Read the cache from disk once. Caller holds ``_lock``."""
    global _ids
    if _ids is not None:
        return _ids
    loaded: dict[str, str] = {}
    try:
        if _IDS_PATH.exists():
            raw = json.loads(_IDS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                loaded = {
                    str(k).upper(): str(v)
                    for k, v in raw.items()
                    if _ID_RE.match(str(v))
                }
    except (OSError, ValueError) as exc:
        log.warning("futu: unreadable id cache, starting empty: %s", exc)
    _ids = loaded
    return _ids


def _save_locked() -> None:
    """Atomically persist the cache. Caller holds ``_lock``."""
    if _ids is None:
        return
    try:
        _IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_IDS_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_ids, fh, indent=1, sort_keys=True)
        os.replace(tmp, _IDS_PATH)
    except OSError as exc:
        log.warning("futu: could not persist id cache: %s", exc)


def cached_stock_id(futu_symbol: str | None) -> str | None:
    """Return a known stockId for *futu_symbol*, or None. Never fetches.

    This is the request-path entry point: it touches disk at most once per
    process and cannot block on a vendor.
    """
    if not futu_symbol:
        return None
    with _lock:
        return _load_locked().get(futu_symbol.upper())


def remember_stock_id(futu_symbol: str, stock_id: str) -> None:
    """Record a resolved id, persisting only when it is new or changed."""
    if not futu_symbol or not _ID_RE.match(stock_id or ""):
        return
    key = futu_symbol.upper()
    with _lock:
        ids = _load_locked()
        if ids.get(key) == stock_id:
            return
        ids[key] = stock_id
        _save_locked()


# A symbol Futu does not list fails identically forever, so it must not be
# re-fetched on every page view. Persisted for the reason FailureBackoff exists:
# otherwise a restart re-queues every dead symbol at once.
_backoff = fetchguard.FailureBackoff("futu_ids", base_seconds=900.0, max_seconds=86_400.0)


def resolve_stock_id(futu_symbol: str | None, *, session=None) -> str | None:
    """Return the stockId for *futu_symbol*, fetching the quote page if needed.

    Off the request path only -- this makes an outbound call. Returns None
    (rather than raising) for every failure, so the caller falls back to the web
    link instead of surfacing a vendor problem as a page error.
    """
    if not futu_symbol:
        return None
    key = futu_symbol.upper()

    hit = cached_stock_id(key)
    if hit:
        return hit
    if not _backoff.ready(key):
        return None
    if not _resolve_slot_available():
        log.warning("futu: resolution ceiling reached (%d per %.0fs) — %s serves "
                    "the web link", _RESOLVE_MAX_PER_WINDOW,
                    _RESOLVE_WINDOW_SECONDS, key)
        return None

    url = web_url(key)
    try:
        resp = fetchguard.request(
            _PROVIDER, url,
            session=session,
            timeout=_TIMEOUT_SECONDS,
            raise_for_status=False,
            headers={"User-Agent": _UA, "Accept-Language": "en"},
        )
    except (fetchguard.CooldownActive, requests.RequestException, RuntimeError) as exc:
        # Breaker open, timeout, DNS, TLS, retries exhausted -- all mean "no id
        # this time", and all are ordinary enough not to deserve a traceback.
        log.info("futu: %s not resolved (%s)", key, exc)
        _backoff.record_failure(key)
        return None

    if resp.status_code != 200:
        # 404 is the common one and is not an error: Futu simply does not list
        # this symbol, which the caller renders as "no app link".
        log.info("futu: %s returned HTTP %d", key, resp.status_code)
        _backoff.record_failure(key)
        return None

    stock_id = _parse_stock_id(resp.text, key)
    if not stock_id:
        _backoff.record_failure(key)
        return None

    _backoff.record_success(key)
    remember_stock_id(key, stock_id)
    log.info("futu: resolved %s -> %s", key, stock_id)
    return stock_id


def _parse_stock_id(html: str, expected_symbol: str) -> str | None:
    """Pull stockId out of a quote page, verifying it is the right company.

    ``stockCode`` + ``marketLabel`` from ``stock_info`` rebuild the requested
    symbol exactly (``SMCI`` + ``US`` -> ``SMCI-US``), so the match is *checked*
    rather than trusted. A mismatch means the page shape moved and the id is not
    trustworthy, which is reported as "no id" -- the caller then renders the web
    link, so a Futu redesign costs the app handoff and never mislinks.
    """
    block = _STOCK_INFO_ANCHOR.search(html or "")
    if not block:
        log.warning("futu: no stock_info in %s page — parser may need updating",
                    expected_symbol)
        return None
    info = html[block.end():block.end() + _STOCK_INFO_WINDOW]

    code = _CODE_RE.search(info)
    stock_id = _ID_FIELD_RE.search(info)
    market = _MARKET_RE.search(info)
    if not (code and stock_id and market):
        log.warning("futu: stock_info for %s missing code/id/market", expected_symbol)
        return None

    found = f"{code.group(1)}-{market.group(1)}".upper()
    if found != expected_symbol.upper():
        log.warning("futu: %s page identifies itself as %s — refusing the id",
                    expected_symbol, found)
        return None
    return stock_id.group(1)


# ── URL builders ────────────────────────────────────────────────────────────

def web_url(futu_symbol: str) -> str:
    """The public quote page: the fallback, and what desktop keeps using."""
    return f"{_WEB_BASE}{quote(futu_symbol, safe='.-')}"


def deep_link(stock_id: str | None) -> str | None:
    """``ftnn://quote/stockDetail/<id>/1`` — the native quote screen."""
    if not stock_id or not _ID_RE.match(str(stock_id)):
        return None
    return f"{SCHEME}://" + _QUOTE_PATH.format(stock_id=stock_id)


def android_intent_url(stock_id: str | None, fallback_url: str) -> str | None:
    """The same link as an Android intent: URL, with the web page as fallback.

    Android is the easy platform precisely because the fallback is declarative --
    ``S.browser_fallback_url`` makes Chrome open the web page when the app is
    absent, so there is no timer to tune and no window in which the reader sees
    nothing. ``package=`` pins it to Futubull rather than offering a chooser.
    """
    if not stock_id or not _ID_RE.match(str(stock_id)):
        return None
    path = _QUOTE_PATH.format(stock_id=stock_id)
    return (
        f"intent://{path}#Intent;scheme={SCHEME};package={ANDROID_PACKAGE};"
        f"S.browser_fallback_url={quote(fallback_url, safe='')};end"
    )


def ios_store_url() -> str:
    """Where an iPhone without the app is sent, if the caller wants the store."""
    return f"https://apps.apple.com/app/id{IOS_APP_STORE_ID}"


def link_context(futu_symbol: str | None) -> dict[str, str | None]:
    """Everything the template needs, built from cache alone (no fetching).

    Returned as a dict so the route stays a one-liner and a future addition
    (moomoo, an analyst-page link) does not change the route's signature.
    """
    if not futu_symbol:
        return {"futu_symbol": None, "futu_web": None,
                "futu_deeplink": None, "futu_intent": None}
    web = web_url(futu_symbol)
    stock_id = cached_stock_id(futu_symbol)
    return {
        "futu_symbol": futu_symbol,
        "futu_web": web,
        "futu_deeplink": deep_link(stock_id),
        "futu_intent": android_intent_url(stock_id, web),
    }
