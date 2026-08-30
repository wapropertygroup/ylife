"""Sharing a finished agent report with somebody who did not run it.

The unit of sharing is a *capability*: a row keyed by an unguessable token, and a
public route that will render any job the token names. Two facts about this
codebase force that shape rather than a permission grant:

- **There is no user directory.** yStocker never persists a user row — every
  gate, quota and credit balance keys off ``session["user_email"]`` at use time.
  So there is no way to check that a typed recipient is a real account, and no
  way to tell a typo from a stranger. A grant keyed to an address could only be
  redeemed by someone who signs in with *exactly* that address, and the sender
  would get no signal when they got it wrong.
- **The recipient is, by design, someone with no account.** ``/agents`` is the
  paid surface; the whole point of sharing a report is to show it to a person who
  has not run one. Requiring sign-in to read a link you were sent inverts that.

The cost is real and is not hedged anywhere in this module: **anyone holding the
token can read the report.** It is a secret URL, not an authenticated one, so
forwarding it re-shares the report. That is the same trade
``yplanner-shared-trips`` already makes, and the mitigations are the ones that
bound the blast radius rather than remove it — 128 bits of entropy so it cannot
be guessed, a 30-day expiry so it cannot be sat on, an explicit revoke, and the
sharer's own address masked on the public page so a forwarded link does not also
leak who sent it.

Ordering matters on the write path and is the one thing not to rearrange:
``create()`` must succeed before ``send()`` is called. A mail whose "open the
report" button 404s is worse than no mail, and DynamoDB being unreachable is
exactly when that would happen.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Where share rows live. Single-attribute PK ``token`` (String), no sort key and
#: no GSI: nothing in the product asks "which shares exist for job X" or "who has
#: this user shared with", and adding an index for a question nobody poses costs
#: write capacity on every share. Abuse is bounded by the daily counter in
#: quota.py instead, which is cheaper and already exists.
TABLE_NAME = (os.environ.get("AGENTS_SHARES_TABLE") or "ystocker-agent-shares").strip()
REGION = (os.environ.get("AWS_REGION") or "us-west-2").strip()

#: 16 bytes -> 22 url-safe characters, ~128 bits. Sized against offline guessing
#: rather than against the 8 characters ``yplanner-shared-trips`` uses: that one
#: shares a holiday itinerary, this one shares something the owner paid for.
TOKEN_BYTES = 16

#: Shares stop working after this long. Bounds the window in which a forwarded
#: link keeps paying out, and keeps the table from growing without limit.
TTL_DAYS = 30

#: The sharer's covering note. Capped because it is rendered into an HTML mail
#: sent from our domain to an address we do not control: an uncapped free-text
#: field there is a spam payload with a report attached.
NOTE_MAX = 500

#: Deliberately stricter than RFC 5322, which permits quoting and folding that no
#: human types into a share box. It rejects any whitespace at all, which is what
#: makes a header-injection attempt (``a@b.com\nBcc: ...``) fail validation here
#: rather than at SES.
_EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")

_table = None
_table_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Whether the share feature is offered at all.

    Gated on SES having a From address for the same reason ``report_email`` is:
    the share is a mail, and offering a button that silently cannot send is worse
    than not offering it. ``AGENTS_SHARE=0`` is the kill switch, separate from
    ``AGENTS_EMAIL_REPORT`` so that turning off completion mail — a thing the
    owner asked for — does not also turn off sharing, and vice versa.
    """
    flag = (os.environ.get("AGENTS_SHARE") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return bool((os.environ.get("SES_FROM_EMAIL") or "").strip())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def normalise_email(raw: Any) -> Optional[str]:
    """Lowercase and validate one recipient address, or None.

    Lowercasing matches ``credits._key``, ``quota`` and ``agents.owns``, all of
    which lowercase at every boundary — a share recorded in mixed case would not
    be a bug today but would become one the moment anything joins on it.
    """
    addr = str(raw or "").strip()
    if not addr or len(addr) > 254:
        return None
    addr = addr.lower()
    return addr if _EMAIL_RE.match(addr) else None


def clean_note(raw: Any) -> str:
    """The sharer's note, flattened and capped.

    Control characters are stripped rather than escaped. The note reaches an HTML
    mail (where ``report_email`` escapes it) *and* a web page (where Jinja
    escapes it), so this is not the XSS boundary and must not pretend to be —
    what it removes is the class of character that survives both escapers intact
    and still corrupts a layout, plus anything that could reach a mail header if a
    later caller is careless with where it puts the note.

    Tab is kept through the filter and *then* collapsed with the spaces, rather
    than deleted with the other control characters. Deleting it first welds the
    words either side together — ``"AAPL\\tBUY"`` became ``"AAPLBUY"`` — which is
    worse than the layout problem the filter exists to prevent, and easy to hit
    because pasting a row out of a spreadsheet is a natural thing to do here.
    """
    text = str(raw or "")
    text = "".join(ch for ch in text if ch in "\n\t" or ch >= " ")
    # Collapse runs of blank lines; a note is a sentence or two, not a document.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()[:NOTE_MAX]


def mask_email(addr: Optional[str]) -> str:
    """``alice@example.com`` -> ``alice@…``.

    Used on the public share page, never in the mail. The mail went to an address
    the sharer chose, so naming them there is information the recipient already
    has; the page is readable by anyone the link reaches, and "who sent this" is
    not part of what the sharer agreed to publish.
    """
    addr = (addr or "").strip()
    if "@" not in addr:
        return ""
    local, _, _domain = addr.partition("@")
    return f"{local}@…" if local else ""


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def _get_table():
    """The shares table, or None when DynamoDB is unreachable.

    None fails *closed* at every call site: no row means no share, which means no
    mail. That is the opposite of ``quota``, which fails open because losing a
    counter costs a little API spend — here, failing open would mean mailing a
    stranger a link that cannot work.
    """
    global _table
    if _table is not None:
        return _table
    with _table_lock:
        if _table is not None:
            return _table
        try:
            import boto3

            ddb = boto3.resource("dynamodb", region_name=REGION)
            table = ddb.Table(TABLE_NAME)
            table.load()
            _table = table
            log.info("share: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - no table, no sharing
            log.warning("share: DynamoDB unavailable (%s); sharing disabled", exc)
            return None
    return _table


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def create(job: dict[str, Any], sharer: str, recipient: str,
           note: str = "") -> Optional[dict[str, Any]]:
    """Record a share of ``job`` and return the row, or None if it cannot be.

    Callers must treat None as "do not send anything". The row is the thing that
    makes the link in the mail resolve, so writing it is not an audit side-effect
    to be done afterwards — it is the share.

    ``job`` is passed in rather than looked up so this module never imports
    ``agents``: the authorization decision (may this caller share this job?)
    belongs to the route, which already holds the session, and keeping it there
    means this function cannot be the place someone forgets to check it.
    """
    table = _get_table()
    if table is None:
        return None

    job_id = str(job.get("id") or "").strip()
    owner = str(job.get("user") or "").strip().lower()
    if not job_id or not owner:
        log.warning("share: refusing to share a job with no id or no owner")
        return None

    # A run given the holder's portfolio must not become a public URL. The block
    # only ever reached the decision agents' prompts, but those agents write prose
    # and routinely quote what they were told, so the report can carry the owner's
    # position weights and stated limits — and `/agents/shared/<token>` answers to
    # anybody holding the token, with no sign-in. This is the one refusal that
    # cannot be recovered from afterwards: once the mail is out, the report is out.
    #
    # Refused rather than redacted. Redaction here would mean pattern-matching
    # percentages out of free prose written by a model, which fails quietly in the
    # direction that discloses.
    if job.get("portfolio_context"):
        log.info("share: refusing to share %s — the run included the owner's "
                 "portfolio", job_id)
        return None

    now = _now()
    row = {
        "token":      secrets.token_urlsafe(TOKEN_BYTES),
        "job_id":     job_id,
        "owner":      owner,
        "sharer":     (sharer or "").strip().lower(),
        "recipient":  (recipient or "").strip().lower(),
        "note":       clean_note(note),
        "ticker":     str(job.get("ticker") or "")[:16],
        "lang":       "zh" if str(job.get("lang") or "").lower() == "zh" else "en",
        "created_at": now.isoformat(timespec="seconds"),
        # Stored for DynamoDB's own TTL sweeper *and* re-checked in lookup(). The
        # sweeper is best-effort and can run up to 48h late, so a row past its
        # date is expected to still be present and must not still be readable.
        "expires_at": int((now + timedelta(days=TTL_DAYS)).timestamp()),
    }
    try:
        # A fresh 128-bit token cannot collide in practice; the condition is here
        # so that if it somehow does, the write fails instead of silently
        # repointing an existing share at a different report.
        table.put_item(Item=row,
                       ConditionExpression="attribute_not_exists(#t)",
                       ExpressionAttributeNames={"#t": "token"})
    except Exception as exc:  # noqa: BLE001
        log.warning("share: could not record share of %s by %s: %s",
                    job_id, sharer, exc)
        return None
    log.info("share: %s shared %s (%s) with %s [token=%s…]",
             row["sharer"], job_id, row["ticker"], row["recipient"],
             row["token"][:6])
    return row


def _valid_token(raw: Any) -> Optional[str]:
    """``raw`` if it is exactly the shape ``create()`` mints, else None.

    Validated as-is rather than stripped-then-validated, which is the opposite of
    how this module treats email addresses — and deliberately so. An address is
    prose a human typed into a box, where trimming a stray space is a courtesy; a
    token is an opaque credential arriving in a URL path, where anything not
    byte-for-byte right is either a copy-paste that lost characters (so repairing
    the ends will not save it) or somebody probing. Repairing near misses would
    also make several URLs address one share, for no benefit.

    Both readers go through this, and both call it *before* touching the table:
    ``/agents/shared/<token>`` is public and unauthenticated, so a walk of the URL
    space must not cost a billed DynamoDB read per attempt.
    """
    if not isinstance(raw, str):
        return None
    if not (16 <= len(raw) <= 64):
        return None
    return raw if re.fullmatch(r"[A-Za-z0-9_-]+", raw) else None


def revoke(token: str, requester: str) -> bool:
    """Kill a share. True only if a row was actually removed.

    Conditioned on the requester being the recorded sharer, so revoking is not a
    way to delete other people's shares by guessing tokens — which matters
    because the token *is* the read credential, and anyone who was ever sent the
    link holds one.
    """
    tok = _valid_token(token)
    who = (requester or "").strip().lower()
    if not tok or not who:
        return False
    table = _get_table()
    if table is None:
        return False
    try:
        table.delete_item(
            Key={"token": tok},
            # #s rather than a bare `sharer`: DynamoDB's reserved-word list is
            # long and grows, and a name that collides fails the whole call at
            # runtime with a ValidationException rather than at review time.
            ConditionExpression="#s = :who",
            ExpressionAttributeNames={"#s": "sharer"},
            ExpressionAttributeValues={":who": who},
        )
    except Exception as exc:  # noqa: BLE001 - wrong owner, or already gone
        log.info("share: revoke of %s… by %s did not apply (%s)",
                 tok[:6], who, exc)
        return False
    log.info("share: %s revoked %s…", who, tok[:6])
    return True


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def lookup(token: Any) -> Optional[dict[str, Any]]:
    """The share row for ``token``, or None if absent, malformed or expired."""
    tok = _valid_token(token)
    if tok is None:
        return None
    table = _get_table()
    if table is None:
        return None
    try:
        got = table.get_item(Key={"token": tok})
    except Exception as exc:  # noqa: BLE001
        log.warning("share: lookup failed for %s…: %s", tok[:6], exc)
        return None
    row = got.get("Item") or None
    if not row:
        return None
    try:
        if int(row.get("expires_at", 0)) <= int(_now().timestamp()):
            log.info("share: %s… is expired", tok[:6])
            return None
    except (TypeError, ValueError):
        return None            # unparseable expiry: treat as expired, not as forever
    return row


def share_url(token: str, base: str = "") -> str:
    """The public URL a recipient opens.

    ``base`` defaults to ``report_email.base_url()`` so a shared link points at
    the same host the completion mail does — which is ``trade-agents.com``, not
    ``stock.li-family.us``. This is load-bearing rather than cosmetic:
    ``SESSION_COOKIE_DOMAIN`` is unset, so the two hosts have separate sessions,
    and a link that lands on the wrong one would show a signed-in sharer as
    signed out.
    """
    if not base:
        from ystocker import report_email

        base = report_email.base_url()
    return f"{base.rstrip('/')}/agents/shared/{token}"


#: Fields of a *job* that a share token is allowed to expose. Mirrors
#: ``agents._PUBLIC_FIELDS`` in intent and, like it, omits ``user``: the endpoint
#: is unauthenticated, so anything added here is published to anyone who is ever
#: forwarded the link. An allowlist rather than a denylist so that a new field on
#: the job record is invisible until someone decides otherwise.
_SHAREABLE_JOB_FIELDS = (
    "id", "ticker", "date", "status", "decision", "report", "lang", "language",
    "created_at", "finished_at", "elapsed_sec", "degraded", "fallback_models",
    "recovered",
    # Provenance. The recipient did not choose the configuration and cannot see
    # the sharer's picker, so without these a report on the cheapest tier is
    # indistinguishable from one on the most capable -- and this surface has no
    # sign-in behind which to go and check. ``model_choice`` is omitted for the
    # same reason it is in ``agents._PUBLIC_FIELDS``: it is an internal key.
    "provider", "deep_model", "quick_model", "thinking",
)


def public_payload(row: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """What the public share route may return, and nothing else.

    Note ``chat`` is absent by omission from ``_SHAREABLE_JOB_FIELDS``: the
    follow-up conversation is between the owner and the model, was never part of
    the report, and is owner-only even in the authenticated API
    (``agents.owns``, not ``can_read``).
    """
    out = {k: job.get(k) for k in _SHAREABLE_JOB_FIELDS if k in job}
    out["shared"] = {
        "by":         mask_email(row.get("sharer")),
        "note":       str(row.get("note") or ""),
        "at":         str(row.get("created_at") or ""),
        "expires_at": int(row.get("expires_at") or 0),
    }
    return out
