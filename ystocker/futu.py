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

What Futu does to a box that asks too often
-------------------------------------------
Futu runs a WAF in front of the quote pages, and on 2026-10-04 it was
rate-limiting this box: about 150 failed resolutions against 10 successes in
two hours, driven by crawlers running /history's ``futuWarm()`` once per page
they rendered. A request 7 s after a success was refused; requests 85 s apart
passed. It refuses in two shapes, and neither is an error status:

* **an HTTP 302 to** ``https://www.futunn.com/403``. ``requests`` used to follow
  it to an HTTP 200 page reading 访问频繁，请稍后重试 ("too frequent"), so the
  status check never fired, the parser found no ``stock_info``, and the log
  blamed the parser ("may need updating") for a page that had not changed;
* **an HTTP 200 JavaScript challenge** at the quote URL itself (~10.9 KB, title
  "Document") that sets a ``wafToken=`` cookie and knows a ``WAF_EXPIRED`` state.

Both are about this box, not the symbol that was asked for, so
:func:`_classify_response` tells them apart from a miss before anything is
charged to a symbol, and a block pauses Futu as a whole. Recording it as one
symbol's failure paused nothing -- the next symbol was asked at once, which is
how a soft limit becomes a hard one.

Everything that has to hold across gunicorn's workers lives on disk, not in
memory: there are two of them, each replaced every ~200 requests by a fork of
the master, so in-process state both disagrees between workers and resets
without warning. That covers the WAF pause, the fetch pacing and the id cache,
whose workers used to write their own copies over one another (see the cache
section below).
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
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX; the box and every laptop are
    fcntl = None  # type: ignore[assignment]

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

_CACHE_DIR = Path(__file__).parent.parent / "cache"

# ── Shared state across workers ─────────────────────────────────────────────
#
# Every piece of it is a small file replaced whole (temp file + os.replace), so
# a reader always sees a complete file and never needs a lock. A
# read-modify-write does need one, and it is an flock on a *dedicated* lock file
# for the reason quota._Guard gives: os.replace installs a new inode, which
# would detach a lock held on the data file itself.


class _FileLock:
    """An exclusive ``flock`` on *path* for the length of a ``with`` block.

    ``__enter__`` answers whether the lock is held. Blocking (the default) that
    is always True; non-blocking it is False while another process holds it,
    which is how a caller on a request thread declines instead of waiting.
    Without flock (non-POSIX, or a filesystem that refuses it) it answers True
    and logs: the in-process locks still serialise this worker, and only the
    cross-worker guarantee is gone -- the same trade quota._Guard makes.
    """

    def __init__(self, path: Path, *, blocking: bool = True) -> None:
        self._path = path
        self._blocking = blocking
        self._fh = None

    def __enter__(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+")
        if fcntl is None:
            log.warning("futu: no flock on this platform; %s guards this worker "
                        "only", self._path.name)
            return True
        flags = fcntl.LOCK_EX | (0 if self._blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(self._fh.fileno(), flags)
        except BlockingIOError:
            return False
        except OSError as exc:
            log.warning("futu: flock unavailable on %s (%s); it guards this "
                        "worker only", self._path.name, exc)
        return True

    def __exit__(self, *exc) -> bool:
        if self._fh is not None:
            self._fh.close()            # closing the descriptor releases the flock
            self._fh = None
        return False


def _write_atomic(path: Path, text: str, *, mtime: float | None = None) -> None:
    """Replace *path* with *text* whole, or leave it untouched and raise OSError.

    *mtime*, when given, is stamped on the new file before it is moved into
    place, so no reader ever sees the file carrying any other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_epoch(path: Path) -> float | None:
    """The epoch float in *path*, or None when it is absent or not a finite number."""
    try:
        value = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ── Pacing ──────────────────────────────────────────────────────────────────
#
# /api/futu/<ticker> is public and unauthenticated, and _futu_symbol() maps any
# bare string to "<IT>-US" -- so without a ceiling, enumerating invented tickers
# turns this box into an outbound amplifier: one cheap request in, one ~1.3 MB
# fetch of futunn.com out, repeated. The per-symbol FailureBackoff does not cover
# it, because every invented symbol is a *new* key and so always ready.
#
# The ceiling used to be 10 new resolutions per 60 s *per process*, which bounded
# the average and nothing else. There are two workers, each replaced every ~200
# requests by a fork that starts a fresh window, and nothing stopped a window's
# fetches arriving in the same second -- which is the shape Futu refuses: on
# 2026-10-04 a request 7 s after a success was blocked, while requests 85 s apart
# passed. Lowering the count to 2 would have kept both faults (four a minute
# box-wide, and still back to back). So the ceiling is now a *gap*, and it is
# box-wide: one fetch per _MIN_GAP_SECONDS, claimed through a timestamp file
# under an flock, so every worker -- and every worker that replaces one -- sees
# the same last fetch. That also bounds the amplifier at two fetches a minute,
# where the old ceiling allowed twenty. 30 s sits between the two measurements
# rather than being proven safe; the WAF pause below is the backstop if it is
# not.
#
# A request that cannot claim the slot never waits for it: /api/futu runs on a
# request thread, and the button already works as a web link. It just does not
# resolve this time, and a later reader's page asks again.

_MIN_GAP_SECONDS = fetchguard.env_float("FUTU_MIN_GAP_SECONDS", 30.0, 1.0)
_PACE_PATH = _CACHE_DIR / "futu_last_fetch"
_FETCH_LOCK_PATH = _CACHE_DIR / "futu_fetch.lock"

_pace_lock = threading.Lock()
# This worker's own last claim. Redundant while the shared stamp can be written;
# it is the floor that still holds when it cannot.
_last_claim = 0.0


def _resolve_slot_available(now: float | None = None) -> bool:
    """Claim the box-wide fetch slot, or report that it is taken. Never waits."""
    global _last_claim
    now = time.time() if now is None else now
    # Another thread already in here is claiming the same slot, so coming second
    # is the same answer as being refused -- and costs no wait.
    if not _pace_lock.acquire(blocking=False):
        return False
    try:
        if abs(now - _last_claim) < _MIN_GAP_SECONDS:
            return False
        try:
            with _FileLock(_FETCH_LOCK_PATH, blocking=False) as held:
                if not held:            # another worker is inside, claiming it
                    return False
                last = _read_epoch(_PACE_PATH)
                # Within one gap of the last fetch, on *either* side. A stamp a
                # hair ahead of now is one just written and rounded up to the
                # millisecond -- reading that as a clock step let a second
                # fetch through. Only a stamp more than a gap ahead is a clock
                # that moved, and honouring it could refuse for as long as the
                # step, so that one is overwritten.
                if last is not None and abs(now - last) < _MIN_GAP_SECONDS:
                    return False
                _write_atomic(_PACE_PATH, f"{now:.3f}\n")
        except OSError as exc:
            log.warning("futu: shared fetch pacing unavailable (%s) — pacing "
                        "this worker alone", exc)
        _last_claim = now
        return True
    finally:
        _pace_lock.release()


# ── Futu's WAF ──────────────────────────────────────────────────────────────
#
# A block is about this box, so it is never charged to the symbol's back-off;
# it pauses Futu as a whole. fetchguard's breaker does that for one process
# only, and the processes are several and short-lived, so the pause is also
# written to cache/futu_waf_until, which every worker reads before fetching. 30
# minutes the first time, doubling for each block that follows without a
# success in between, up to 6 h; the next quote page that comes back clears it.
#
# The file holds one epoch float: when the pause ends. Its mtime is stamped with
# the moment the pause began, so the window's length is until - mtime and the
# next block can double it without a second file. A marker that does not parse,
# or describes a window this module never writes, is ignored rather than
# honoured -- failing closed on a corrupt file would pause Futu until somebody
# deleted it by hand, because nothing would ever fetch to clear it. Each pause
# is also bounded by its own window from *now*, so a clock stepped back cannot
# stretch thirty minutes into a day.

_WAF_PATH = _CACHE_DIR / "futu_waf_until"
_WAF_FIRST_SECONDS = fetchguard.env_float("FUTU_WAF_PAUSE_SECONDS", 1800.0, 60.0)
_WAF_MAX_SECONDS = max(_WAF_FIRST_SECONDS,
                       fetchguard.env_float("FUTU_WAF_MAX_PAUSE_SECONDS", 21_600.0, 60.0))
# Room for filesystems whose timestamps are coarser than this module's clock.
_WAF_WINDOW_SLACK = 60.0

# The classifier's markers. Words such as "challenge" or "slider" are
# deliberately not among them: both occur in ordinary quote pages.
_STATE_MARK = "__INITIAL_STATE__"
_WAF_MARKERS = ("wafToken=", "WAF_EXPIRED")
_BLOCK_PATH = "/403"
_TITLE_RE = re.compile(r"<title[^>]*>([^<]{0,120})</title>", re.IGNORECASE)

_FOUND, _BLOCKED, _MISS = "found", "blocked", "miss"

_waf_bad_sig: tuple[int, int] | None = None     # the unusable marker last warned about


def _read_waf_marker() -> tuple[float, float] | None:
    """``(until, window)`` from the shared marker, or None if there is none to honour.

    An expired marker is still returned: it is what tells the next block to
    double. Read through one descriptor, so the mtime belongs to the content.
    """
    global _waf_bad_sig
    try:
        with open(_WAF_PATH, encoding="utf-8") as fh:
            st = os.fstat(fh.fileno())
            raw = fh.read(64)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("futu: unreadable WAF marker (%s) — Futu is not paused", exc)
        return None
    try:
        until = float(raw.strip())
    except ValueError:
        until = math.nan
    window = until - st.st_mtime
    if math.isfinite(until) and 0.0 < window <= _WAF_MAX_SECONDS + _WAF_WINDOW_SLACK:
        return until, window
    sig = (st.st_ino, st.st_mtime_ns)
    if sig != _waf_bad_sig:             # once per bad file, not once per request
        _waf_bad_sig = sig
        log.warning("futu: ignoring a WAF marker this module could not have "
                    "written (%r) — Futu is not paused", raw[:40])
    return None


def _waf_remaining(now: float | None = None) -> float:
    """Seconds left in the box-wide WAF pause, or 0.0 when there is none."""
    marker = _read_waf_marker()
    if marker is None:
        return 0.0
    until, window = marker
    now = time.time() if now is None else now
    return max(0.0, min(until - now, window))


def _record_waf_block(reason: str, *, now: float | None = None) -> float:
    """Pause every worker's Futu fetches after a block; returns the window applied."""
    now = time.time() if now is None else now
    window = remaining = _WAF_FIRST_SECONDS
    try:
        with _FileLock(_FETCH_LOCK_PATH):
            prev = _read_waf_marker()
            if prev is not None and prev[0] > now:
                # Still inside a pause, so this request was already on the wire
                # when another worker recorded the block: the same event, not a
                # new one, and the window does not grow.
                window = prev[1]
                remaining = min(prev[0] - now, window)
            else:
                if prev is not None:
                    # Blocked again on the first fetch after a pause ran out,
                    # with no success in between.
                    window = min(_WAF_MAX_SECONDS, round(prev[1]) * 2.0)
                remaining = window
                _write_atomic(_WAF_PATH, f"{now + window:.3f}\n", mtime=now)
    except OSError as exc:
        log.warning("futu: could not write the WAF marker (%s) — pausing this "
                    "worker only", exc)
    fetchguard.trip(_PROVIDER, remaining, f"WAF block: {reason}")
    log.warning("futu: blocked by Futu's WAF (%s) — Futu paused for %.0f min",
                reason, remaining / 60.0)
    return window


def _clear_waf_block() -> None:
    """A quote page came back, so the box is welcome again: the next block starts over."""
    try:
        _WAF_PATH.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("futu: could not clear the WAF marker (%s)", exc)
        return
    log.info("futu: a quote page came back — WAF pause cleared")


def _futu_paused() -> bool:
    """True while Futu as a whole must not be asked, by this worker or any other."""
    if fetchguard.cooldown_remaining(_PROVIDER) > 0:
        return True
    remaining = _waf_remaining()
    if remaining <= 0:
        return False
    # Recorded by another worker, or by this one before a restart. Mirrored into
    # this worker's breaker, so the next check is a dict lookup, not a file read.
    fetchguard.trip(_PROVIDER, remaining, "WAF pause (shared marker)")
    return True


# ── Persistent id cache ─────────────────────────────────────────────────────
#
# Not a TTL cache: a Futu stockId is permanent, so there is nothing to expire and
# an entry is worth keeping across a box replacement. Same shape as
# peer_groups.json -- one flat dict, atomic replace on write.
#
# It is shared by every worker, and that is the part that used to go wrong. Each
# worker read the file once and then wrote *its own* dict over it whenever it
# learned an id, erasing whatever another worker had added in the meantime: on
# 2026-10-04, 38 of the 192 symbols resolved since 2026-09-01 (SHOP-US, JD-US,
# TSLA-US, EWY-US...) were missing from the file, and MSFT-US had been resolved
# three times -- each loss a fetch spent again, against a vendor that now
# rations them. A save therefore re-reads the file under an flock and merges
# into it, and a read reloads the file whenever its stat signature moves (the
# predictions.peek / directory.peek pattern), so an id learned by one worker is
# served by all of them.

_IDS_PATH = _CACHE_DIR / "futu_ids.json"
_IDS_LOCK_PATH = _CACHE_DIR / "futu_ids.lock"
_lock = threading.Lock()
_ids: dict[str, str] | None = None
_ids_sig: tuple[int, int, int] | None = None    # the file _ids was last merged from


def _file_sig(path: Path) -> tuple[int, int, int] | None:
    """Inode, mtime and size. os.replace always installs a new inode, so a
    rewrite inside one mtime tick still changes the signature."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _read_ids_file() -> dict[str, str]:
    """The ids on disk; ``{}`` when there is no file yet.

    Raises ValueError when the file is not a JSON object of ids, and OSError
    when it exists but cannot be read -- two different things to a caller about
    to write, since only the first is safe to replace.
    """
    try:
        raw = json.loads(_IDS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, found {type(raw).__name__}")
    return {str(k).upper(): str(v) for k, v in raw.items() if _ID_RE.match(str(v))}


def _load_locked() -> dict[str, str]:
    """This process's ids, refreshed from disk when the file has moved. Caller holds ``_lock``.

    A reload *adds* to what this process holds rather than replacing it, so an
    id whose save failed is still served -- and written on the next save.
    """
    global _ids, _ids_sig
    sig = _file_sig(_IDS_PATH)
    if _ids is not None and sig == _ids_sig:
        return _ids
    try:
        disk = _read_ids_file()
    except (OSError, ValueError) as exc:
        log.warning("futu: unreadable id cache (%s) — keeping what this worker "
                    "has", exc)
        disk = {}
    _ids = {**(_ids or {}), **disk}
    _ids_sig = sig                      # a broken file is not re-parsed per request
    return _ids


def _save_locked(key: str, stock_id: str) -> None:
    """Merge into the file under the cross-worker lock, *key* winning. Caller holds ``_lock``.

    The file's ids beat this process's for any other key: the file is the
    already-merged copy every worker writes through.
    """
    global _ids, _ids_sig
    try:
        with _FileLock(_IDS_LOCK_PATH):
            try:
                disk = _read_ids_file()
            except ValueError as exc:
                # Writes are atomic, so this is not a torn write of ours: an
                # empty file after a crash, or a hand edit. Nothing in it can be
                # read, so replacing it cannot lose anything.
                log.warning("futu: id cache is not valid (%s) — rewriting it "
                            "from memory", exc)
                disk = {}
            merged = {**(_ids or {}), **disk, key: stock_id}
            _write_atomic(_IDS_PATH, json.dumps(merged, indent=1, sort_keys=True))
            _ids, _ids_sig = merged, _file_sig(_IDS_PATH)
    except OSError as exc:
        # Includes failing to *read* the file: writing this worker's dict over a
        # file it could not read is exactly the clobber the merge exists to stop.
        log.warning("futu: could not persist id cache (%s) — %s is kept in memory",
                    exc, key)


def cached_stock_id(futu_symbol: str | None) -> str | None:
    """Return a known stockId for *futu_symbol*, or None. Never fetches.

    This is the request-path entry point: one ``stat()`` per call, re-reading
    the file only when another worker has rewritten it, and it cannot block on
    a vendor.
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
        ids[key] = stock_id             # served by this worker even if the save fails
        _save_locked(key, stock_id)


# A symbol Futu does not list fails identically forever, so it must not be
# re-fetched on every page view. Persisted for the reason FailureBackoff exists:
# otherwise a restart re-queues every dead symbol at once. Only misses land here;
# a WAF block is about the box, and pauses Futu instead (see above).
_backoff = fetchguard.FailureBackoff("futu_ids", base_seconds=900.0, max_seconds=86_400.0)


def resolve_stock_id(futu_symbol: str | None, *, session=None) -> str | None:
    """Return the stockId for *futu_symbol*, fetching the quote page if needed.

    Off the request path only -- this makes an outbound call. Returns None
    (rather than raising) for every failure, so the caller falls back to the web
    link instead of surfacing a vendor problem as a page error. A refusal to
    fetch at all -- a WAF pause, an open breaker, the box's fetch slot taken --
    returns None at once: this runs on /api/futu's request thread, and waiting
    would hold a worker to save the reader nothing.
    """
    if not futu_symbol:
        return None
    key = futu_symbol.upper()

    hit = cached_stock_id(key)
    if hit:
        return hit
    if not _backoff.ready(key):
        return None
    # Futu as a whole before the slot: a fetch that is not going to happen must
    # not spend the box's one fetch per gap.
    if _futu_paused():
        return None
    if not _resolve_slot_available():
        log.info("futu: %s not resolved now — the box's one fetch per %.0fs is "
                 "taken", key, _MIN_GAP_SECONDS)
        return None

    url = web_url(key)
    try:
        resp = fetchguard.request(
            _PROVIDER, url,
            session=session,
            timeout=_TIMEOUT_SECONDS,
            # A retry is a second fetch inside the gap, seconds after the first:
            # the shape the WAF refuses. The back-off and the next reader retry.
            retries=0,
            raise_for_status=False,
            # A block arrives as a redirect to /403, and following it turned the
            # block into an HTTP 200 page with no stock_info. Redirects are
            # classified, never followed.
            allow_redirects=False,
            headers={"User-Agent": _UA, "Accept-Language": "en"},
        )
    except fetchguard.CooldownActive as exc:
        # The breaker opened between the check above and here. That is Futu's
        # state rather than this symbol's, so nothing is charged to the symbol.
        log.info("futu: %s not resolved (%s)", key, exc)
        return None
    except (requests.RequestException, RuntimeError) as exc:
        # Timeout, DNS, TLS, retries exhausted -- all mean "no id this time", and
        # all are ordinary enough not to deserve a traceback.
        log.info("futu: %s not resolved (%s)", key, exc)
        _backoff.record_failure(key)
        return None

    verdict, stock_id, why = _classify_response(resp, key)
    if verdict == _BLOCKED:
        _record_waf_block(f"{key}: {why}")
        return None
    if verdict != _FOUND or not stock_id:
        _backoff.record_failure(key)
        return None

    _clear_waf_block()
    _backoff.record_success(key)
    remember_stock_id(key, stock_id)
    log.info("futu: resolved %s -> %s", key, stock_id)
    return stock_id


def _classify_response(resp, symbol: str) -> tuple[str, str | None, str]:
    """Sort one quote-page response into found, blocked or miss.

    Returns ``(verdict, stock_id, why)``, deciding in this order:

    a. a redirect to ``/403`` is a block, on any host -- the challenge script
       itself sends a client that comes back too fast to ``www.moomoo.com/403``.
       Any other redirect is a miss, logged with its target since it is not
       followed;
    b. a page that parses, round-trip rule included, is found, whatever else it
       contains;
    c. a page carrying ``__INITIAL_STATE__`` that does not parse is a quote page
       whose shape moved: a miss, and the one case that earns "parser may need
       updating";
    d. ``wafToken=`` or ``WAF_EXPIRED`` is the challenge page: a block;
    e. anything else is some other page: a miss, logged with its size and title,
       so the next new shape can be recognised from the journal alone.

    Taking (b) and (c) first is what makes (d) safe to key on two strings: a real
    quote page that happened to contain one never gets that far. HTTP 429 is a
    block as well, being the one status that means "this caller" by definition,
    and a 200 that has already landed on /403 -- a redirect followed after all --
    is recognised by its URL.
    """
    status = resp.status_code
    if 300 <= status < 400:
        location = str(resp.headers.get("Location") or "")
        if _is_block_url(location):
            return _BLOCKED, None, f"HTTP {status} to {location}"
        log.warning("futu: %s redirected (HTTP %d) to %s — not followed",
                    symbol, status, location or "nowhere")
        return _MISS, None, f"HTTP {status}"
    if status == 429:
        return _BLOCKED, None, "HTTP 429"
    if status != 200:
        # 404 is the common one and is not an error: Futu simply does not list
        # this symbol, which the caller renders as "no app link".
        log.info("futu: %s returned HTTP %d", symbol, status)
        return _MISS, None, f"HTTP {status}"
    if _is_block_url(resp.url):
        return _BLOCKED, None, f"landed on {resp.url}"

    html = resp.text or ""
    stock_id = _parse_stock_id(html, symbol)
    if stock_id:
        return _FOUND, stock_id, "quote page"
    if _STATE_MARK in html:
        if not _STOCK_INFO_ANCHOR.search(html):
            log.warning("futu: no stock_info in %s page — parser may need updating",
                        symbol)
        # Otherwise _parse_stock_id has already said what was wrong with it.
        return _MISS, None, "quote page without a usable stock_info"
    if any(marker in html for marker in _WAF_MARKERS):
        return _BLOCKED, None, f"WAF challenge page, {len(html)} bytes"
    title = _TITLE_RE.search(html)
    log.warning("futu: %s answered with neither a quote page nor a known block "
                "page (%d bytes, title %r)", symbol, len(html),
                title.group(1).strip() if title else None)
    return _MISS, None, "not a quote page"


def _is_block_url(url) -> bool:
    """True when *url*'s path is Futu's ``/403`` page, on whichever host."""
    try:
        return urlsplit(str(url or "")).path.rstrip("/") == _BLOCK_PATH
    except ValueError:                  # e.g. an unterminated IPv6 literal
        return False


def _parse_stock_id(html: str, expected_symbol: str) -> str | None:
    """Pull stockId out of a quote page, verifying it is the right company.

    ``stockCode`` + ``marketLabel`` from ``stock_info`` rebuild the requested
    symbol exactly (``SMCI`` + ``US`` -> ``SMCI-US``), so the match is *checked*
    rather than trusted. A mismatch means the page shape moved and the id is not
    trustworthy, which is reported as "no id" -- the caller then renders the web
    link, so a Futu redesign costs the app handoff and never mislinks.

    A page with no ``stock_info`` at all returns None *quietly*: a WAF page has
    none either, and only :func:`_classify_response` can tell the two apart.
    Logging "parser may need updating" here is what made every block look like
    a redesign.
    """
    block = _STOCK_INFO_ANCHOR.search(html or "")
    if not block:
        return None
    info = html[block.end():block.end() + _STOCK_INFO_WINDOW]

    code = _CODE_RE.search(info)
    stock_id = _ID_FIELD_RE.search(info)
    market = _MARKET_RE.search(info)
    if not (code and stock_id and market):
        log.warning("futu: stock_info for %s missing code/id/market — parser may "
                    "need updating", expected_symbol)
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
