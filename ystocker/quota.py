"""
ystocker.quota
~~~~~~~~~~~~~~
Daily run quotas for the trading agents.

/agents is open to any signed-in user, and a single deep run is roughly thirty
Gemini 3 Pro calls at ``thinking_level=high``. The quota is therefore not a
fairness nicety, it is the thing standing between a public Google Sign-In button
and an unbounded bill:

* per user, per day  -- ``AGENTS_DAILY_LIMIT`` (default 3)
* per VIP, per day   -- ``AGENTS_VIP_DAILY_LIMIT`` (default 50)
* everyone, per day  -- ``AGENTS_GLOBAL_DAILY_LIMIT`` (default 60)

Past the free daily allowance a user may spend a purchased credit -- see
``ystocker.credits``. Credits are checked only after the free allowance is used
up, so nobody is ever charged while a free run is still available.

The global ceiling exists because the per-user limit alone bounds nothing: the
site is public, so the number of distinct Google accounts is unbounded and
3-per-user multiplies by however many people show up. Paid runs are counted
against it too: it is a capacity limit, not a billing one, and the box cannot
serve more analyses in a day just because they were paid for.

Correctness notes
-----------------
Counting happens under an ``flock`` on a dedicated lock file, because two
gunicorn workers serve this app and a read-modify-write of a JSON counter across
processes is otherwise a lost-update race -- two simultaneous submissions would
both read ``9``, both write ``10``, and eleven runs would happen. The data file
itself is replaced atomically, so a process killed mid-write cannot leave a
truncated counter (which would silently hand out free quota).

Quota is consumed at submit time, not on completion, so a user cannot start
fifty runs while the first is still queued. A run that fails *before the child
launches* is refunded, since nothing was spent; a run that fails afterwards is
not, because by then an unknown number of LLM calls have been paid for.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

QUOTA_DIR = Path(__file__).parent.parent / "cache" / "agents"
_LOCK_PATH = QUOTA_DIR / "quota.lock"

# Local midnight, not UTC: "10 runs per day" resetting at 4pm would be baffling.
# The users and the box are US Pacific.
QUOTA_TZ = os.environ.get("AGENTS_QUOTA_TZ", "America/Los_Angeles")

_SPLIT_RE = re.compile(r"[,;\s]+")
_local_lock = threading.Lock()


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, ignoring nonsense.

    A typo'd limit must not silently become 0 (nobody can run) or negative.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("quota: %s=%r is not an integer; using %d", name, raw, default)
        return default
    if val < 0:
        log.warning("quota: %s=%d is negative; using %d", name, val, default)
        return default
    return val


def limit_default() -> int:
    return _int_env("AGENTS_DAILY_LIMIT", 3)


def limit_vip() -> int:
    return _int_env("AGENTS_VIP_DAILY_LIMIT", 50)


def limit_global() -> int:
    return _int_env("AGENTS_GLOBAL_DAILY_LIMIT", 60)


def vip_emails() -> set[str]:
    """VIP addresses. Configurable, with the owner as the built-in default so a
    missing SSM parameter cannot lock the owner down to the public limit."""
    raw = os.environ.get("AGENTS_VIP_EMAILS", "").strip()
    if not raw:
        raw = "liyuanxi23@gmail.com"
    return {e.strip().lower() for e in _SPLIT_RE.split(raw) if "@" in e}


def is_vip(email: Optional[str]) -> bool:
    return bool(email) and email.strip().lower() in vip_emails()


def limit_for(email: Optional[str]) -> int:
    return limit_vip() if is_vip(email) else limit_default()


def limit_chat() -> int:
    """Follow-up questions per user per day.

    Far higher than the run limit because a question is one Flash call, not the
    ~22 Pro calls a run costs -- but not unlimited, since the page is open to
    any signed-in user and every call is still billed.
    """
    return _int_env("AGENTS_CHAT_DAILY_LIMIT", 60)


def try_consume_chat(email: Optional[str]) -> tuple[bool, dict[str, Any]]:
    """Reserve one follow-up question. Counted separately from runs.

    Kept in the same daily file and under the same lock as the run counter, so
    the two cannot interleave a lost update, but under its own key: spending a
    question must never eat into the analysis allowance.
    """
    key = (email or "").strip().lower()
    if not key:
        return False, {"used": 0, "limit": limit_chat(), "remaining": 0}
    day, lim = today(), limit_chat()
    with _Guard():
        data = _read(day)
        chat = data.setdefault("chat", {})
        used = int(chat.get(key, 0))
        if used >= lim:
            return False, {"used": used, "limit": lim, "remaining": 0, "tz": QUOTA_TZ}
        chat[key] = used + 1
        data["day"] = day
        _write(day, data)
    return True, {"used": used + 1, "limit": lim,
                  "remaining": max(0, lim - used - 1), "tz": QUOTA_TZ}


def today() -> str:
    """Current quota day as YYYY-MM-DD in the configured timezone."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(QUOTA_TZ)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 - missing tzdata: fall back to local time
        return datetime.now().strftime("%Y-%m-%d")


def _path(day: str) -> Path:
    return QUOTA_DIR / f"quota-{day}.json"


def _read(day: str) -> dict[str, Any]:
    try:
        data = json.loads(_path(day).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - corrupt counter, start the day over
        log.warning("quota: unreadable counter for %s (%s); resetting", day, exc)
    return {"day": day, "users": {}, "total": 0}


def _write(day: str, data: dict[str, Any]) -> None:
    QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(QUOTA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _path(day))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Guard:
    """Cross-process mutual exclusion around the counter.

    ``flock`` is advisory and per-open-file, so a dedicated lock file is used
    rather than the data file -- the data file is replaced by ``os.replace``,
    which would detach any lock held on the old inode.
    """

    def __init__(self) -> None:
        self._fh = None

    def __enter__(self):
        _local_lock.acquire()
        try:
            QUOTA_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = open(_LOCK_PATH, "a+")
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError) as exc:
                # No flock (non-POSIX, or a filesystem without it). The
                # in-process lock still serialises this worker; log it because
                # the cross-worker guarantee is genuinely gone.
                log.warning("quota: flock unavailable (%s); counter is "
                            "per-process only", exc)
        except BaseException:
            _local_lock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._fh:
                try:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                self._fh.close()
        finally:
            self._fh = None
            _local_lock.release()
        return False


def usage(email: Optional[str]) -> dict[str, Any]:
    """Current usage for a user, without consuming anything."""
    day = today()
    key = (email or "").strip().lower()
    with _Guard():
        data = _read(day)
    used = int(data["users"].get(key, 0))
    lim = limit_for(email)
    g_used, g_lim = int(data.get("total", 0)), limit_global()
    out = {
        "day": day,
        "used": used,
        "limit": lim,
        "remaining": max(0, lim - used),
        "vip": is_vip(email),
        "global_used": g_used,
        "global_limit": g_lim,
        "global_remaining": max(0, g_lim - g_used),
        "tz": QUOTA_TZ,
    }
    out.update(_credit_info(key))
    return out


def try_consume(email: Optional[str]) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Reserve one run. Returns (ok, reason, usage-after).

    ``reason`` is ``"user"``, ``"global"`` or ``"auth"`` so the caller can explain
    *which* ceiling was hit -- being told "limit reached" when it was actually the
    site-wide cap, and your own quota is untouched, is needlessly confusing.

    Order of spending, which is the part worth getting right: the free daily
    allowance first, a purchased credit only once that is exhausted. Reversed, a
    user would be charged for a run they were entitled to for nothing.

    The global cap is checked *before* credits, and a paid run counts against it.
    That cap is about what the box and the upstream model quota can actually
    deliver in a day, not about money, so selling a credit cannot raise it.
    """
    key = (email or "").strip().lower()
    if not key:
        return False, "auth", usage(email)

    day = today()
    lim, g_lim = limit_for(email), limit_global()
    paid = False
    with _Guard():
        data = _read(day)
        used = int(data["users"].get(key, 0))
        total = int(data.get("total", 0))
        if total >= g_lim:
            reason = "global"
        elif used >= lim:
            # Free allowance gone. A credit is spent outside the flock on
            # purpose: it is a conditional DynamoDB update, already atomic, and
            # holding a file lock across a network call would serialise every
            # submission on the box behind it.
            reason = "user"
        else:
            reason = None
        if reason is None:
            data["users"][key] = used + 1
            data["total"] = total + 1
            data["day"] = day
            _write(day, data)
            used, total = used + 1, total + 1
        _prune_locked(day)

    if reason == "user":
        from ystocker import credits

        if credits.spend(key, 1):
            paid = True
            reason = None
            with _Guard():
                data = _read(day)
                # The per-user counter is deliberately not advanced for a paid
                # run: it counts the free allowance, and bumping it would make
                # "3 of 3 used" read as "4 of 3". The global counter *is* bumped,
                # because that one is about total load.
                data["total"] = int(data.get("total", 0)) + 1
                data["day"] = day
                _write(day, data)
                total = int(data["total"])

    info = {
        "day": day, "used": used, "limit": lim,
        "remaining": max(0, lim - used), "vip": is_vip(email),
        "global_used": total, "global_limit": g_lim,
        "global_remaining": max(0, g_lim - total), "tz": QUOTA_TZ,
        "paid": paid,
    }
    info.update(_credit_info(key))
    return reason is None, reason, info


def _credit_info(email: str) -> dict[str, Any]:
    """Credit balance and where to buy more, for the response payload.

    Never raises: the quota answer must not depend on the ledger being reachable,
    and a missing balance shows as 0 with the buy link still offered.
    """
    try:
        from ystocker import credits

        return {"credits": credits.balance(email), "pay_url": credits.PAY_URL}
    except Exception as exc:  # noqa: BLE001
        log.warning("quota: credit balance unavailable: %s", exc)
        return {"credits": 0, "pay_url": ""}


def refund(email: Optional[str], day: Optional[str] = None,
           paid: bool = False) -> None:
    """Give a reservation back, for a run that never reached the LLM.

    A paid run took a credit and left the daily counter alone, so it has to be
    refunded the same way round. Decrementing the daily counter for a paid run
    would hand out a free run that was never used and quietly keep the money.
    """
    key = (email or "").strip().lower()
    if not key:
        return
    if paid:
        from ystocker import credits

        credits.refund(key, 1)
        # The global counter was bumped for a paid run, so unwind that too.
        _refund_global(day or today())
        return
    d = day or today()
    with _Guard():
        data = _read(d)
        used = int(data["users"].get(key, 0))
        if used <= 0:
            return
        data["users"][key] = used - 1
        data["total"] = max(0, int(data.get("total", 0)) - 1)
        _write(d, data)
    log.info("quota: refunded one run to %s for %s", key, d)


def _refund_global(day: str) -> None:
    with _Guard():
        data = _read(day)
        data["total"] = max(0, int(data.get("total", 0)) - 1)
        _write(day, data)


_KEEP_DAYS = 10


def _prune_locked(day: str) -> None:
    """Drop counter files well past their day. Caller holds the guard."""
    try:
        files = sorted(QUOTA_DIR.glob("quota-*.json"))
        for p in files[:-_KEEP_DAYS] if len(files) > _KEEP_DAYS else []:
            if p.name != _path(day).name:
                p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - housekeeping only
        log.debug("quota: prune skipped: %s", exc)
