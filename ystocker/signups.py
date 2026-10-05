"""
ystocker.signups
~~~~~~~~~~~~~~~~
Mails the site's owner when somebody signs in for the first time.

There is no sign-up form here. An account is a Google address that has completed
``/api/auth/google``, and every gate (quotas, credits, a reader's runs and
portfolio) keys off that address at the moment it is used. So a sign-up is a
first sign-in, and noticing one needs the one thing yStocker never kept: a
durable record of which addresses have been seen. ``ystocker-users`` is that
record, one row per address. Nothing gates on it; it only remembers.

Four rules hold it together:

* **The claim is a conditional write.** The first sign-in puts the row with
  ``attribute_not_exists(email)`` and mails; a later one, from either gunicorn
  worker, fails the condition and only moves ``last_seen``. A read and then a
  write would let two near-simultaneous sign-ins both mail -- the same reason
  ``report_email`` claims a finished run with ``O_EXCL`` rather than a field.
* **Unreachable means silent.** Without the table nobody can tell a new address
  from a returning one, and guessing "new" would mail on every sign-in for as
  long as an outage lasted. A first sign-in made then leaves no row, so it is
  announced on that address's next sign-in instead.
* **The record is seeded before anything is announced.** On the day this
  shipped, thirteen addresses had runs, portfolios or research reports on file
  and no row here, so each would have been announced as new the next time it
  signed in. :func:`seed` writes a row for every address those tables hold, plus
  the VIP list and the agents' allowlist, and only then writes the
  :data:`SEED_KEY` row that switches announcements on. It runs once by itself,
  from ``create_app``; a sign-in before it finishes is recorded without a mail.
  An address that signed in before but left nothing stored can still be
  announced once, when it next signs in.
* **Bounded.** ``SIGNUP_NOTIFY_DAILY_LIMIT`` (50) mails a day, on quota.py's
  flocked counter: a Google account costs nothing to make.

``SIGNUP_NOTIFY=0`` is the kill switch, and ``SIGNUP_NOTIFY_EMAIL`` sends the
mails somewhere other than ``CONTACT_EMAIL`` (admin@li-family.us). They go out
through ``report_email._ses_send``, so region and From address match every other
mail this process sends. Recording happens either way: the switch silences the
mail, not the memory, so turning it back on does not announce a backlog.

A row holds the address, the Google profile name, when and where it first signed
in (site, page, language), when it last did and how many times. No IP address
and no picture: an admin note needs neither, and a row that holds less is a row
that leaks less.

The table is not in ``deploy/cloudformation.yaml``, matching every hand-made
table here; IAM already grants ``table/ystocker-*``::

    aws dynamodb create-table --table-name ystocker-users --region us-west-2 \\
      --billing-mode PAY_PER_REQUEST \\
      --attribute-definitions AttributeName=email,AttributeType=S \\
      --key-schema AttributeName=email,KeyType=HASH
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urlsplit

log = logging.getLogger(__name__)

TABLE_NAME = (os.environ.get("USERS_TABLE", "ystocker-users").strip()
              or "ystocker-users")
REGION = os.environ.get("AWS_REGION", "us-west-2")

#: The row that switches announcements on, written when :func:`seed` finishes.
#: Not an address (no "@"), so no sign-in can claim it.
SEED_KEY = "_meta:seed"

#: How long after start-up the seed runs: past the burst of work create_app
#: starts, and short enough that few sign-ins land before it.
SEED_DELAY_SECONDS = 20

#: A failed SES send is tried once more after this long. Only one more: the row
#: is already claimed, so this is the last chance this sign-up has to be told.
RETRY_SECONDS = 5

_OFF = ("0", "false", "no", "off")
_LANGS = {"en": "English", "zh": "Chinese (中文)"}
_TABLE_RETRY_SECONDS = 600

_table = None
_table_retry_at = 0.0
_table_lock = threading.Lock()
_seeded = False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Whether a first sign-in is mailed. On once SES has a From address."""
    flag = (os.environ.get("SIGNUP_NOTIFY") or "").strip().lower()
    if flag in _OFF:
        return False
    return bool((os.environ.get("SES_FROM_EMAIL") or "").strip())


def recipient() -> str:
    """Who is told: ``SIGNUP_NOTIFY_EMAIL``, else the site's contact address."""
    from ystocker import CONTACT_EMAIL

    return (os.environ.get("SIGNUP_NOTIFY_EMAIL") or "").strip() or CONTACT_EMAIL


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalise(raw: Any) -> Optional[str]:
    """An address as a key, or None. Lower-cased, as every other table keys it."""
    addr = str(raw or "").strip().lower()
    if "@" not in addr or len(addr) > 254 or any(c.isspace() for c in addr):
        return None
    return addr


def _printable(text: Any, limit: int) -> str:
    """``text`` without control characters, at most ``limit`` long. The name and
    the page come from the reader, and both end up in a mail."""
    return "".join(c for c in str(text or "") if c.isprintable()).strip()[:limit]


def site_of(host: Any) -> str:
    """``request.host`` without its port, lower-cased."""
    return _printable(host, 100).lower().split(":")[0]


def page_of(referrer: Optional[str], host: str) -> str:
    """The page a sign-in was made from: path and query of a same-site Referer,
    decoded for reading. ``/login?next=/agents`` says where the reader was going.
    A Referer from anywhere else is not ours to record, so it is ``""``."""
    if not referrer:
        return ""
    try:
        parts = urlsplit(referrer)
    except ValueError:
        return ""
    if parts.netloc.lower() != (host or "").lower():
        return ""
    page = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return _printable(unquote(page), 200)


def lang_of(raw: Any) -> str:
    """``<html lang>`` as the sign-in page sent it, reduced to en or zh."""
    value = str(raw or "").strip().lower()
    return "zh" if value.startswith("zh") else "en" if value.startswith("en") else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _when(stamp: Any) -> str:
    """``2026-10-05 14:03 UTC · 7:03 AM PDT``: the owner reads Pacific time."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return str(stamp or "")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:  # noqa: BLE001 - no tzdata: UTC alone is still right
        return utc
    return f"{utc} · {int(local.strftime('%I'))}:{local:%M %p %Z}"


# ---------------------------------------------------------------------------
# The mail
# ---------------------------------------------------------------------------

def build_notice(row: dict[str, Any], total: Optional[int]) -> tuple[str, str, str]:
    """Subject, HTML and plain text announcing ``row``, a just-claimed address.

    Laid out in report_email's palette and with its mark, so it reads as the
    same sender as the run reports. Every value that came from the reader (the
    name above all, which Google lets anyone set) is escaped in the HTML and
    flattened to one line for the subject, where a CR or LF would be header
    injection.
    """
    from ystocker import quota
    from ystocker import report_email as rm

    addr = str(row.get("email") or "")
    name = _printable(row.get("name"), 120)
    site = str(row.get("site") or "")
    brand = rm.brand_for(site) if site else "yStocker"
    who = f"{name} ({addr})" if name else addr
    subject = rm._plain(f"New sign-up on {brand}: {who}", 200)

    facts: list[tuple[str, str, str]] = []      # label, text, html
    if name:
        facts.append(("Name", name, rm._esc(name)))
    href = "mailto:" + quote(addr, safe="@.+-_")
    facts.append(("Email", addr,
                  f'<a href="{rm._esc(href)}" style="color:{rm._ACCENT}">{rm._esc(addr)}</a>'))
    if site:
        facts.append(("Site", site, rm._esc(site)))
    page = str(row.get("page") or "")
    if page:
        facts.append(("Signed in from", page,
                      f'<span style="font-family:{rm._MONO};font-size:12px">'
                      f"{rm._esc(page)}</span>"))
    lang = _LANGS.get(str(row.get("lang") or ""), "")
    if lang:
        facts.append(("Language", lang, rm._esc(lang)))
    when = _when(row.get("first_seen"))
    facts.append(("When", when, rm._esc(when)))
    if total is not None:
        facts.append(("Accounts on record", str(total), rm._esc(str(total))))

    why = (f"One mail per new address, at most {quota.limit_signup_notices()} a day. "
           "SIGNUP_NOTIFY=0 stops them; SIGNUP_NOTIFY_EMAIL sends them elsewhere.")

    cells = "".join(
        f'<tr><td style="padding:7px 14px 7px 0;color:{rm._DIM};font-size:12px;'
        f'white-space:nowrap;vertical-align:top">{rm._esc(label)}</td>'
        f'<td style="padding:7px 0;color:{rm._TEXT};font-size:14px;'
        f'word-break:break-word">{value}</td></tr>'
        for label, _text, value in facts)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{rm._esc(subject)}</title></head>
<body style="margin:0;padding:0;background:{rm._BG};font-family:{rm._SANS}">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:{rm._BG};padding:28px 12px">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="max-width:560px;width:100%;background:{rm._CARD};border-radius:14px;overflow:hidden">
        <tr><td style="background:linear-gradient(135deg,#1d4ed8,#7c3aed);padding:20px 26px">
          <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
            <td width="44" valign="top" style="width:44px;padding:0 14px 0 0">{rm._logo()}</td>
            <td valign="middle">
              <div style="color:#ffffff;font-size:20px;font-weight:700;line-height:1.2">New sign-up</div>
              <div style="color:#bfdbfe;font-size:14px;line-height:1.4;margin:2px 0 0">{rm._esc(brand)}</div>
            </td></tr></table>
        </td></tr>
        <tr><td style="padding:18px 26px 8px">
          <table cellpadding="0" cellspacing="0" border="0" width="100%">{cells}</table>
        </td></tr>
        <tr><td style="padding:12px 26px 20px;border-top:1px solid {rm._LINE}">
          <p style="margin:0;color:#64748b;font-size:11px;line-height:1.6">{rm._esc(why)}</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    text = "\n".join([f"New sign-up on {brand}", "",
                      *(f"{label}: {plain}" for label, plain, _html in facts),
                      "", why])
    return subject, html, text


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def _get_table():
    """The users table, or None. A miss is remembered for ten minutes, so a box
    without the table does not describe it to DynamoDB on every sign-in."""
    global _table, _table_retry_at
    if _table is not None:
        return _table
    with _table_lock:
        if _table is not None:
            return _table
        if time.time() < _table_retry_at:
            return None
        try:
            import boto3

            table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
            table.load()
            _table = table
            log.info("signups: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - no table, no record, no mail
            _table_retry_at = time.time() + _TABLE_RETRY_SECONDS
            log.warning("signups: %s unavailable (%s); sign-ins are not recorded",
                        TABLE_NAME, exc)
    return _table


def _conditional_failed(exc: Exception) -> bool:
    return type(exc).__name__ == "ConditionalCheckFailedException"


def _put_new(table, item: dict[str, Any]) -> bool:
    """Write ``item`` only if its address has no row. True if this call wrote it."""
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(#e)",
                       ExpressionAttributeNames={"#e": "email"})
    except Exception as exc:  # noqa: BLE001 - anything else is the caller's to log
        if _conditional_failed(exc):
            return False
        raise
    return True


def _touch(table, addr: str, stamp: str, name: str) -> None:
    """A returning sign-in: when, how many, and the name if the row has none (a
    seeded row is written from tables that never held one)."""
    names = {"#l": "last_seen", "#n": "sign_ins"}
    values: dict[str, Any] = {":t": stamp, ":one": 1}
    expr = "SET #l = :t"
    if name:
        names["#nm"] = "name"
        values[":nm"] = name
        expr += ", #nm = if_not_exists(#nm, :nm)"
    try:
        table.update_item(Key={"email": addr}, UpdateExpression=expr + " ADD #n :one",
                          ExpressionAttributeNames=names,
                          ExpressionAttributeValues=values)
    except Exception as exc:  # noqa: BLE001 - bookkeeping only
        log.warning("signups: could not update %s: %s", addr, exc)


def _mark(table, addr: str, field: str, stamp: str) -> None:
    try:
        table.update_item(Key={"email": addr}, UpdateExpression="SET #f = :t",
                          ExpressionAttributeNames={"#f": field},
                          ExpressionAttributeValues={":t": stamp})
    except Exception as exc:  # noqa: BLE001 - bookkeeping only
        log.warning("signups: could not mark %s %s: %s", addr, field, exc)


def _is_seeded(table) -> bool:
    """Whether :data:`SEED_KEY` exists. Once true it stays true for the process;
    a read that fails is "not yet", which records without mailing."""
    global _seeded
    if _seeded:
        return True
    try:
        got = table.get_item(Key={"email": SEED_KEY}, ConsistentRead=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("signups: could not read the seed marker: %s", exc)
        return False
    _seeded = bool(got.get("Item"))
    return _seeded


def _count(table) -> Optional[int]:
    """Addresses on record, for the mail. A Scan, because the table is one short
    row per reader and this runs once per new one."""
    try:
        from boto3.dynamodb.conditions import Attr

        kwargs: dict[str, Any] = {"Select": "COUNT",
                                  "FilterExpression": Attr("email").contains("@")}
        total = 0
        while True:
            page = table.scan(**kwargs)
            total += int(page.get("Count") or 0)
            if not page.get("LastEvaluatedKey"):
                return total
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001 - the mail goes without the figure
        log.warning("signups: could not count accounts: %s", exc)
        return None


# ---------------------------------------------------------------------------
# A sign-in
# ---------------------------------------------------------------------------

def _record(addr: str, fields: dict[str, str], now: Optional[str] = None) -> str:
    """Claim ``addr`` and announce it if the claim is new. Returns what happened,
    for the log and the tests."""
    table = _get_table()
    if table is None:
        return "unavailable"
    stamp = now or _now()
    item: dict[str, Any] = {"email": addr, "first_seen": stamp, "last_seen": stamp,
                            "sign_ins": 1, "source": "sign-in"}
    item.update({k: v for k, v in fields.items() if v})
    if not _put_new(table, item):
        _touch(table, addr, stamp, fields.get("name", ""))
        return "returning"
    if not enabled():
        return "disabled"
    if not _is_seeded(table):
        log.info("signups: %s is new, but the record is not seeded yet; not announced", addr)
        return "unseeded"

    from ystocker import quota
    from ystocker import report_email

    if not quota.try_consume_signup_notice():
        log.warning("signups: %d sign-up mails sent today; %s not announced",
                    quota.limit_signup_notices(), addr)
        return "capped"
    subject, html, text = build_notice(item, _count(table))
    to_addr = recipient()
    ok = report_email._ses_send(to_addr, subject, html, text, what=f"sign-up of {addr}")
    if not ok:
        time.sleep(RETRY_SECONDS)
        ok = report_email._ses_send(to_addr, subject, html, text, what=f"sign-up of {addr}")
    _mark(table, addr, "notified_at" if ok else "notify_failed_at", _now())
    return "sent" if ok else "send_failed"


def record_sign_in(email: Any, name: Any = "", host: Any = "", page: str = "",
                   lang: Any = "", background: bool = True) -> None:
    """Note a completed sign-in, and mail the owner if it is the address's first.

    Called by ``/api/auth/google`` once the session is set. ``background`` moves
    the DynamoDB and SES calls onto their own thread, so a sign-in never waits
    on either. Never raises: a sign-in matters far more than a note about it.
    """
    addr = normalise(email)
    if not addr:
        return
    fields = {"name": _printable(name, 120), "site": site_of(host),
              "page": _printable(page, 200), "lang": lang_of(lang)}

    def _go() -> None:
        try:
            outcome = _record(addr, fields)
            log.info("signups: sign-in by %s: %s", addr, outcome)
        except Exception:  # noqa: BLE001
            log.exception("signups: could not record a sign-in by %s", addr)

    if background:
        try:
            threading.Thread(target=_go, daemon=True, name="signup").start()
            return
        except RuntimeError as exc:  # cannot start a thread: do it here instead
            log.warning("signups: no thread for %s (%s); recording inline", addr, exc)
    _go()


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------

def _scan(name: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Every item of table ``name`` under the projection in ``kwargs``. A table
    that does not exist holds no addresses, so it is ``[]``; any other failure
    raises, and the seed is tried again later rather than finished short."""
    import boto3

    table = boto3.resource("dynamodb", region_name=REGION).Table(name)
    out: list[dict[str, Any]] = []
    try:
        while True:
            page = table.scan(**kwargs)
            out.extend(page.get("Items") or [])
            if not page.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ResourceNotFoundException":
            log.info("signups: %s does not exist; no addresses from it", name)
            return []
        raise


def known_addresses(scan: Callable[..., list[dict[str, Any]]] = _scan
                    ) -> dict[str, dict[str, Any]]:
    """Every address the site already holds something for, with where it was
    found and, from the run records, when it first appears.

    The tables are named through their own modules, so a renamed table cannot
    leave the seed reading an empty one: runs (``user``), portfolios
    (``email``), research reports (the owner before ``#`` in ``sk``), credit and
    subscription rows (``u#`` and ``sub#`` ids), and shares (owner and sharer).
    The VIP list and the allowlist are added because their people are the
    owner's own and may hold nothing at all.
    """
    from ystocker import agents, credits, portfolio, quota, research_store, share

    found: dict[str, dict[str, Any]] = {}

    def add(raw: Any, source: str, first: Any = None) -> None:
        addr = normalise(raw)
        if not addr or "#" in addr:
            return
        entry = found.setdefault(addr, {"source": source})
        stamp = str(first or "")
        if stamp and (not entry.get("first_seen") or stamp < entry["first_seen"]):
            entry["first_seen"] = stamp

    for item in scan(agents.JOBS_TABLE_NAME, ProjectionExpression="#u, created_at",
                     ExpressionAttributeNames={"#u": "user"}):
        add(item.get("user"), "runs", item.get("created_at"))
    for item in scan(portfolio.TABLE_NAME, ProjectionExpression="email"):
        add(item.get("email"), "portfolio")
    for item in scan(research_store.TABLE_NAME, ProjectionExpression="sk"):
        add(str(item.get("sk") or "").split("#", 1)[0], "research")
    for item in scan(credits.TABLE_NAME, ProjectionExpression="id"):
        key = str(item.get("id") or "")
        for prefix in ("u#", "sub#"):
            if key.startswith(prefix):
                add(key[len(prefix):], "credits")
    for item in scan(share.TABLE_NAME, ProjectionExpression="#o, sharer",
                     ExpressionAttributeNames={"#o": "owner"}):
        add(item.get("owner"), "shares")
        add(item.get("sharer"), "shares")
    for addr in sorted(quota.vip_emails()):
        add(addr, "vip")
    for addr in sorted(agents.allowed_emails()):
        add(addr, "allowlist")
    return found


def seed(table, scan: Callable[..., list[dict[str, Any]]] = _scan,
         now: Optional[str] = None) -> int:
    """Write a row for every address :func:`known_addresses` finds, then the
    marker that switches announcements on. Rows already present are left alone.
    Returns how many rows this call wrote."""
    global _seeded
    found = known_addresses(scan)
    stamp = now or _now()
    wrote = 0
    for addr, info in sorted(found.items()):
        item = {"email": addr, "source": info["source"], "seeded_at": stamp}
        if info.get("first_seen"):
            item["first_seen"] = info["first_seen"]
        if _put_new(table, item):
            wrote += 1
    _put_new(table, {"email": SEED_KEY, "seeded_at": stamp,
                     "addresses": len(found), "written": wrote})
    _seeded = True
    log.info("signups: seeded %d of %d known addresses; first sign-ins are announced from now on",
             wrote, len(found))
    return wrote


def start_background_thread() -> None:
    """Seed the record once, if it has not been. Three tries, five minutes apart;
    until one succeeds, first sign-ins are recorded without a mail."""
    def _run() -> None:
        time.sleep(SEED_DELAY_SECONDS)
        for attempt in range(3):
            try:
                table = _get_table()
                if table is None or _is_seeded(table):
                    return
                seed(table)
                return
            except Exception:  # noqa: BLE001 - try again later
                log.exception("signups: seed attempt %d failed", attempt + 1)
                time.sleep(300)

    threading.Thread(target=_run, daemon=True, name="signups-seed").start()
