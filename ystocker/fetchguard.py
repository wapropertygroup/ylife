"""
ystocker.fetchguard
~~~~~~~~~~~~~~~~~~~
Shared resilience primitives for outbound HTTP: per-provider circuit breakers,
retry with exponential back-off and jitter, and a persistent per-item failure
back-off.

Why this exists
---------------
Every vendor this app talks to fails differently, and each module used to handle
that alone -- unevenly. Yahoo had no timeout at all (`yf.Ticker(t).info`), so a
hung socket hung a background thread indefinitely with nothing in the log. FRED
retried three times on a linear 3s/6s delay while its docstring claimed
exponential. SEC EDGAR retried once, on 429 only. None of them stopped asking a
provider that had already answered 429, which is how a soft rate-limit becomes a
hard ban.

Two mechanisms live here, deliberately separate because their scopes differ:

* A **circuit breaker** is per *provider* and short-lived (seconds). After a 429
  or a run of 5xx, stop asking entirely for a cool-down window rather than
  spending the next N calls discovering the same thing. In-process state is the
  right scope -- the window is far shorter than the gap between deploys.

* A **failure back-off** is per *item* (one ticker, one series id) and
  long-lived (hours, doubling). A delisted symbol fails identically forever, and
  retrying it every cycle costs a request and a log line each time. This one is
  persisted to disk, because otherwise every restart forgets and the entire
  dead-symbol set gets retried at once on the next warm.

The two compose: the breaker decides *whether to talk to a provider at all*, the
back-off decides *which items are worth asking about*.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

log = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    import os
    try:
        return max(minimum, float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int, minimum: int = 0) -> int:
    import os
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


#: Default per-request timeout. Never leave this unset -- an absent timeout in
#: `requests` means "wait forever", which in a daemon thread is unrecoverable.
FETCH_TIMEOUT_SECONDS = env_float("FETCH_TIMEOUT_SECONDS", 20.0, 1.0)
FETCH_MAX_RETRIES = env_int("FETCH_MAX_RETRIES", 2, 0)
FETCH_BACKOFF_BASE_SECONDS = env_float("FETCH_BACKOFF_BASE_SECONDS", 0.5, 0.0)
#: 429 means "you are asking too fast" -- back off for a long-ish window.
FETCH_RATE_LIMIT_COOLDOWN_SECONDS = env_float("FETCH_RATE_LIMIT_COOLDOWN_SECONDS", 60.0, 0.0)
#: 5xx / connection errors mean "the vendor is unwell" -- shorter, it may pass.
FETCH_ERROR_COOLDOWN_SECONDS = env_float("FETCH_ERROR_COOLDOWN_SECONDS", 20.0, 0.0)

#: Statuses worth trying again. 404/403 are *answers*, not failures, and are
#: deliberately absent -- retrying them wastes a request and tripping a
#: cool-down on them would stall a whole refresh over one missing document.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# ── Circuit breaker ─────────────────────────────────────────────────────────

_cooldowns: dict[str, tuple[float, str]] = {}   # provider → (until_epoch, reason)
_cooldown_lock = threading.Lock()


class CooldownActive(RuntimeError):
    """Raised instead of calling a provider inside its cool-down window.

    Callers should treat this like any other fetch failure: it is not a bug, it
    is the breaker doing its job. It carries the remaining seconds so a caller
    that wants to log once per cycle rather than once per item can.
    """

    def __init__(self, provider: str, remaining: float, reason: str) -> None:
        super().__init__(
            f"{provider} cool-down active for {remaining:.1f}s ({reason})"
        )
        self.provider = provider
        self.remaining = remaining
        self.reason = reason


def cooldown_remaining(provider: str) -> float:
    """Seconds left in *provider*'s cool-down window, or 0.0 if it is open."""
    with _cooldown_lock:
        until, _ = _cooldowns.get(provider, (0.0, ""))
    return max(0.0, until - time.time())


def trip(provider: str, seconds: float, reason: str) -> None:
    """Open the breaker on *provider* for at least *seconds*.

    Extends an existing window rather than shortening it, so a 5xx arriving
    during a 429 cool-down cannot accidentally let traffic back through early.
    """
    if seconds <= 0:
        return
    until = time.time() + seconds
    with _cooldown_lock:
        previous, _ = _cooldowns.get(provider, (0.0, ""))
        if until > previous:
            _cooldowns[provider] = (until, reason)
            log.warning("fetchguard: %s cool-down %.0fs (%s)", provider, seconds, reason)


def reset(provider: str) -> None:
    """Close the breaker on *provider* immediately. Mainly for tests and ops."""
    with _cooldown_lock:
        _cooldowns.pop(provider, None)


def guard(provider: str) -> None:
    """Raise :class:`CooldownActive` if *provider* is currently cooling down."""
    with _cooldown_lock:
        until, reason = _cooldowns.get(provider, (0.0, ""))
    remaining = until - time.time()
    if remaining > 0:
        raise CooldownActive(provider, remaining, reason)


def snapshot() -> dict[str, dict[str, object]]:
    """Current breaker state, for a health endpoint or a log line."""
    now = time.time()
    with _cooldown_lock:
        items = list(_cooldowns.items())
    return {
        provider: {"remaining_seconds": round(until - now, 1), "reason": reason}
        for provider, (until, reason) in items
        if until > now
    }


def _retry_delay(attempt: int) -> float:
    """Exponential back-off with up to 25% jitter.

    Jitter matters more than it looks: without it, N threads that failed on the
    same vendor outage all wake at the same instant and re-create the spike that
    caused the outage.
    """
    if FETCH_BACKOFF_BASE_SECONDS <= 0:
        return 0.0
    base = FETCH_BACKOFF_BASE_SECONDS * (2 ** attempt)
    return base + random.uniform(0.0, base * 0.25)


def request(
    provider: str,
    url: str,
    *,
    session: Optional[requests.Session] = None,
    method: str = "GET",
    timeout: Optional[float] = None,
    retries: Optional[int] = None,
    retry_statuses: Iterable[int] = RETRYABLE_STATUS,
    raise_for_status: bool = True,
    **kwargs,
) -> requests.Response:
    """Perform one HTTP call to *provider* under retry + breaker protection.

    Refuses to call at all while the breaker is open (raises
    :class:`CooldownActive`). Retries *retry_statuses* and transport errors with
    exponential back-off, then trips the breaker and re-raises.

    `raise_for_status=False` returns the final response even if it is an error,
    which is what a caller that treats 404 as "absent" wants. `retry_statuses`
    is narrowable for the same reason -- SEC's 503 means "no filing here" often
    enough that retrying it, let alone cooling down on it, would be wrong.
    """
    guard(provider)

    retry_statuses = frozenset(retry_statuses)
    attempts = (FETCH_MAX_RETRIES if retries is None else max(0, retries)) + 1
    effective_timeout = FETCH_TIMEOUT_SECONDS if timeout is None else timeout
    caller = session or requests
    last_exc: Optional[BaseException] = None

    for attempt in range(attempts):
        try:
            resp = caller.request(method, url, timeout=effective_timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                delay = _retry_delay(attempt)
                log.warning(
                    "fetchguard: %s %s failed (%s) — retry %d/%d in %.2fs",
                    provider, url, exc, attempt + 1, attempts - 1, delay,
                )
                if delay > 0:
                    time.sleep(delay)
                continue
            trip(provider, FETCH_ERROR_COOLDOWN_SECONDS, type(exc).__name__)
            raise

        if resp.status_code in retry_statuses:
            if attempt < attempts - 1:
                delay = _retry_delay(attempt)
                log.warning(
                    "fetchguard: %s %s HTTP %d — retry %d/%d in %.2fs",
                    provider, url, resp.status_code, attempt + 1, attempts - 1, delay,
                )
                if delay > 0:
                    time.sleep(delay)
                continue
            # Out of retries on a retryable status: this is the vendor telling
            # us to stop, so stop for everyone, not just this call site.
            if resp.status_code == 429:
                trip(provider, FETCH_RATE_LIMIT_COOLDOWN_SECONDS, "HTTP 429")
            else:
                trip(provider, FETCH_ERROR_COOLDOWN_SECONDS, f"HTTP {resp.status_code}")

        if raise_for_status:
            resp.raise_for_status()
        return resp

    # Unreachable: the loop either returns or raises. Kept so a future edit that
    # breaks that invariant fails loudly instead of returning None.
    raise RuntimeError(f"fetchguard: {provider} exhausted retries") from last_exc


# ── Persistent per-item failure back-off ────────────────────────────────────

_CACHE_DIR = Path(__file__).parent.parent / "cache"


class FailureBackoff:
    """Exponential per-item back-off that survives a restart.

    Keyed by an arbitrary string (a ticker, a series id). Each consecutive
    failure doubles the wait from *base_seconds* up to *max_seconds*; one
    success clears the item entirely.

    Persistence is the point. The in-process version of this in `routes.py`
    worked, but forgot everything on restart, so every deploy re-queued the full
    set of permanently-dead symbols for an immediate retry. Writes are
    coalesced -- callers mutate freely and the file is rewritten at most once
    per `flush_interval` seconds, plus whenever :meth:`flush` is called.
    """

    def __init__(
        self,
        name: str,
        *,
        base_seconds: float = 120.0,
        max_seconds: float = 3600.0,
        flush_interval: float = 30.0,
    ) -> None:
        self.name = name
        self.base_seconds = max(1.0, base_seconds)
        self.max_seconds = max(self.base_seconds, max_seconds)
        self.flush_interval = max(0.0, flush_interval)
        self._path = _CACHE_DIR / f"fetch_backoff_{name}.json"
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, float]] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            raw = json.loads(self._path.read_text())
            if not isinstance(raw, dict):
                return
            now = time.time()
            kept: dict[str, dict[str, float]] = {}
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                retry_after = float(entry.get("retry_after") or 0.0)
                # Drop entries whose window has long since passed, so a symbol
                # that failed once a year ago does not live in the file forever.
                if retry_after <= now:
                    continue
                kept[str(key)] = {
                    "count": float(entry.get("count") or 0.0),
                    "retry_after": retry_after,
                }
            self._state = kept
            if kept:
                log.info("fetchguard[%s]: restored %d back-off entries", self.name, len(kept))
        except Exception as exc:
            log.warning("fetchguard[%s]: could not read %s: %s", self.name, self._path, exc)

    def _write_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            tmp.replace(self._path)          # atomic replace
            self._dirty = False
            self._last_flush = time.time()
        except Exception:
            log.exception("fetchguard[%s]: failed to persist back-off state", self.name)

    def _maybe_flush_locked(self) -> None:
        if not self._dirty:
            return
        if time.time() - self._last_flush >= self.flush_interval:
            self._write_locked()

    def flush(self) -> None:
        """Persist pending changes now."""
        with self._lock:
            if self._dirty:
                self._write_locked()

    # -- queries -----------------------------------------------------------

    def ready(self, key: str) -> bool:
        """True if *key* may be attempted now."""
        with self._lock:
            entry = self._state.get(key)
            return not entry or float(entry.get("retry_after", 0.0)) <= time.time()

    def filter_ready(self, keys: Iterable[str], *, log_skipped: bool = True) -> list[str]:
        """Return the subset of *keys* not currently in a back-off window.

        Logs the count and a sample of what it dropped. A filter that silently
        shrinks a work list reads exactly like a filter that covered everything,
        which is how "why is this ticker never updating" becomes unanswerable.
        """
        keys = list(keys)
        now = time.time()
        with self._lock:
            skipped = {
                k for k in keys
                if (e := self._state.get(k)) and float(e.get("retry_after", 0.0)) > now
            }
        ready = [k for k in keys if k not in skipped]
        if skipped and log_skipped:
            log.info(
                "fetchguard[%s]: skipping %d/%d in back-off (e.g. %s)",
                self.name, len(skipped), len(keys), ", ".join(sorted(skipped)[:8]),
            )
        return ready

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Active back-off entries with remaining seconds, for ops/debugging."""
        now = time.time()
        with self._lock:
            items = list(self._state.items())
        return {
            key: {
                "attempts": entry.get("count", 0.0),
                "remaining_seconds": round(float(entry.get("retry_after", 0.0)) - now, 1),
            }
            for key, entry in items
            if float(entry.get("retry_after", 0.0)) > now
        }

    # -- updates -----------------------------------------------------------

    def record_failure(self, key: str) -> float:
        """Register a failure for *key*; returns the new back-off in seconds."""
        with self._lock:
            entry = self._state.get(key) or {}
            count = int(entry.get("count", 0)) + 1
            # Cap the exponent before shifting: 2 ** count with an unbounded
            # count is an arbitrarily large int long before min() sees it.
            delay = min(self.max_seconds, self.base_seconds * (2 ** min(count - 1, 20)))
            self._state[key] = {"count": float(count), "retry_after": time.time() + delay}
            self._dirty = True
            self._maybe_flush_locked()
        return delay

    def record_success(self, key: str) -> None:
        """Clear any back-off for *key*."""
        with self._lock:
            if self._state.pop(key, None) is not None:
                self._dirty = True
                self._maybe_flush_locked()

    def record_batch(self, attempted: Iterable[str], succeeded: Iterable[str]) -> None:
        """Record one batch: everything attempted but not in *succeeded* failed."""
        attempted = list(attempted)
        ok = set(succeeded)
        for key in attempted:
            if key in ok:
                self.record_success(key)
            else:
                self.record_failure(key)
        self.flush()
