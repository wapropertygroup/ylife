"""Report-sharing tests — no Flask app, no network, no SES, no DynamoDB.

Covers ystocker/share.py (the capability rows), the share path through
ystocker/report_email.py (``build(shared_by=…)`` and ``send_share``), and
``quota.try_consume_share``. Four things here are worth guarding and the rest is
detail:

1. **The token is the credential.** So it must be minted with real entropy, must
   stop working at its expiry even though DynamoDB's own TTL sweeper runs late,
   and must be checked for shape *before* a read is billed. Revoking is
   conditioned on the sharer, since everyone who was ever forwarded the link
   holds a valid token.

2. **The public payload is unauthenticated output.** ``/api/agents/shared/<token>``
   answers anyone. The owner's address, the follow-up chat, the runner's pid and
   its stderr all live on the job record next to the report, so the allowlist is
   asserted by *absence of sentinels from the serialised payload* rather than by
   reading the field list back.

3. **The recipient did not ask for this mail.** It goes from our SES identity to
   an address a signed-in user typed, so the sharer's name has to arrive above the
   report, the footer must not claim "you ran this analysis", the CTA must point
   at the capability URL rather than the owner-only ``/agents?job=`` route, and the
   one free-text field a human wrote must reach the client escaped.

4. **Nothing may reach AWS.** ``share._get_table`` is replaced with an in-memory
   fake, ``quota.QUOTA_DIR``/``_LOCK_PATH`` are pointed at a temp dir, and both
   are asserted to have stayed that way. No app is created, no socket is opened
   and no SES client is built: boto3 is stubbed at the module boundary for the
   one class that exercises a send.

The modules under test are loaded by path, so patching their globals cannot leak
into another test module's copy. Their package, however, is the real one -- see the
note below on why the stub in test_report_email.py is not reused.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


# The real ``ystocker`` package, so the lazy cross-module imports resolve.
#
# Deliberately *not* the stub package test_report_email.py installs. That trick
# keeps Flask out of the process, but a bare ModuleType has no ``PEER_GROUPS``,
# so anything that later imports ``ystocker.routes`` in the same process dies on
# its module-scope ``from ystocker import PEER_GROUPS`` -- which is exactly what
# happens to tests/test_import_graph.py when it is loaded after such a module.
# The package's ``__init__`` has no import-time side effects (SSM is read inside
# ``create_app``), so importing it costs one Flask import and no app, no network
# and no AWS. The stub is kept only for the case where Flask is absent, so a
# missing dependency loses this file's coverage rather than the whole run.
try:
    import ystocker as _pkg
except Exception:                       # noqa: BLE001 - no Flask, degrade
    _pkg = types.ModuleType("ystocker")
    _pkg.__path__ = [str(ROOT / "ystocker")]
    sys.modules.setdefault("ystocker", _pkg)
    _pkg = sys.modules["ystocker"]

# agent_roles is a dependency rather than a subject: report_email splits the
# report with it. All four are loaded by path so that patching module globals
# here cannot leak into another test module's copy.
agent_roles = _load("ystocker.agent_roles", "ystocker/agent_roles.py")
mail = _load("ystocker.report_email", "ystocker/report_email.py")
share = _load("ystocker.share", "ystocker/share.py")
quota = _load("ystocker.quota", "ystocker/quota.py")

# report_email reaches for `from ystocker.share import TTL_DAYS` when building a
# shared mail, share.share_url() reaches back for report_email.base_url(), and
# report_email's send-once machinery wants ystocker.agents. Only the first two
# are exercised here; the agents stub is insurance against a real import.
# setdefault throughout: whatever is already registered stays, so nothing another
# test module put here is replaced.
_agents_stub = types.ModuleType("ystocker.agents")
_agents_stub.JOB_DIR = pathlib.Path(tempfile.gettempdir()) / "ystocker-test-jobs"
_agents_stub.EMAIL_MARKER_SUFFIX = ".emailed"
sys.modules.setdefault("ystocker.agents", _agents_stub)
for _name, _mod in (("agent_roles", agent_roles), ("report_email", mail),
                    ("share", share), ("quota", quota)):
    sys.modules.setdefault(f"ystocker.{_name}", _mod)
    if not hasattr(_pkg, _name):
        setattr(_pkg, _name, sys.modules[f"ystocker.{_name}"])


# Anything that must never appear as *live* markup in output built from text a
# user typed. Escaped text is inert by construction -- "&lt;img src=x
# onerror=..&gt;" is a string a mail client displays, not an element it runs --
# so escaped sequences are removed before the check. Without that this flags its
# own successes, which is the failure mode that makes a security test worthless.
# Same list and same technique as test_report_email.py.
FORBIDDEN = ("<script", "<iframe", "<style", "<svg", "<object", "<embed",
             "<img", "<input", "<form", "onerror=", "onclick=", "onload=",
             "onmouseover=", "javascript:", "data:text")

_ESCAPED = re.compile(r"&lt;.*?&gt;", re.S)


def assert_inert(case: unittest.TestCase, html: str, label: str = "") -> None:
    live = _ESCAPED.sub("", html).lower()
    for needle in FORBIDDEN:
        case.assertNotIn(needle, live, f"{label}: {needle!r} survived as live markup")


class _Elements(HTMLParser):
    """Every element and attribute a client would actually *parse*.

    With ``convert_charrefs=True`` an escaped payload arrives as text: the
    tokeniser has already decided what is a tag, so ``&lt;img …&gt;`` reaches
    ``handle_data`` and is never reported as an element. That is precisely the
    property the substring scan cannot see.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: Counter = Counter()
        self.attrs: set[tuple[str, str]] = set()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.tags[tag.lower()] += 1
        for name, _value in attrs:
            self.attrs.add((tag.lower(), (name or "").lower()))

    handle_startendtag = handle_starttag


def _parse(html: str) -> _Elements:
    p = _Elements()
    p.feed(html)
    p.close()
    return p


def assert_no_new_markup(case: unittest.TestCase, hostile: str, baseline: str,
                         label: str = "") -> None:
    """The hostile note must add no element and no attribute to the document.

    Stronger than the substring scan and immune to its central trap. The mail is
    compared against the same mail built with a harmless note, so if the payload
    were honoured rather than escaped the parser would report an element the
    baseline does not have -- and if it were escaped, it reports nothing at all.
    """
    got, base = _parse(hostile), _parse(baseline)
    added = {t for t, n in got.tags.items() if n > base.tags[t]}
    case.assertEqual(added, set(), f"{label}: the note introduced {added}")
    new_attrs = got.attrs - base.attrs
    case.assertEqual(new_attrs, set(), f"{label}: the note introduced {new_attrs}")
    handlers = {a for _t, a in got.attrs if a.startswith("on")}
    case.assertEqual(handlers, set(), f"{label}: event handlers present: {handlers}")


# ── Fixtures ────────────────────────────────────────────────────────────────

def _para(n: int) -> str:
    return ("Analysis paragraph with **bold** text and a figure of 123.45. " * 40
            + "\n\n") * n


def build_report(per_section: int = 1) -> str:
    """A report shaped like write_report_tree's, at a controllable size."""
    out = "# NVDA Analysis\n\nGenerated 2026-08-29 by TradingAgents\n\n"
    out += "## I. Analyst Team Reports\n\n"
    for name in ("Market Analyst", "Sentiment Analyst", "News Analyst",
                 "Fundamentals Analyst"):
        out += f"### {name}\n\n{_para(per_section)}"
    out += "## II. Research Team Decision\n\n"
    for name in ("Bull Researcher", "Bear Researcher", "Research Manager"):
        out += f"### {name}\n\n{_para(per_section)}"
    out += "## V. Portfolio Manager Decision\n\n### Portfolio Manager\n\n"
    out += "**BUY** — high conviction.\n\nEntry 178, stop 165.\n"
    return out


OWNER = "owner@example.com"
SHARER = "alice@example.com"
FRIEND = "bob@example.net"


def make_job(**over) -> dict:
    job = {
        "id": "abc123def4567890",
        "ticker": "NVDA",
        "date": "2026-08-29",
        "user": OWNER,
        "lang": "en",
        "language": "English",
        "status": "done",
        "decision": "BUY",
        "report": build_report(),
        "elapsed_sec": 1834.2,
    }
    job.update(over)
    return job


class ConditionFailed(Exception):
    """Stands in for botocore's ConditionalCheckFailedException.

    share.py catches bare ``Exception`` around both conditional writes, so the
    class matters less than the fact that something is raised -- but a distinct
    type keeps a test that *wanted* a failure from passing on an AttributeError.
    """


def _ddb(value):
    """Mimic the resource layer handing numbers back as ``Decimal``.

    Worth the four lines: ``lookup`` and ``public_payload`` both do ``int()`` on
    ``expires_at``, and a fake that returns plain ints would let a comparison
    against a string or a float slip through.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb(v) for v in value]
    return value


def _resolve(cond: str, names: dict | None) -> str:
    """Substitute ``#alias`` placeholders back into a condition expression.

    share.py writes both of its conditions through ExpressionAttributeNames --
    ``attribute_not_exists(#t)`` and ``#s = :who`` -- because DynamoDB's
    reserved-word list is long and a collision fails the whole call at runtime.
    A fake that only understood the bare-name form would silently accept every
    condition and report that anyone may revoke anyone's share.
    """
    for placeholder, real in (names or {}).items():
        cond = cond.replace(placeholder, real)
    return cond


class FakeTable:
    """An in-memory stand-in for the shares table.

    Honours the two conditions share.py actually writes --
    ``attribute_not_exists(#t)`` on put and ``#s = :who`` on delete -- and counts
    calls, so a test can assert that a malformed token never reached the wire.
    """

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.puts = 0
        self.gets = 0
        self.deletes = 0
        self.raise_on_put: Exception | None = None
        self.raise_on_get: Exception | None = None

    # -- DynamoDB surface ---------------------------------------------------
    def put_item(self, **kw):
        self.puts += 1
        if self.raise_on_put:
            raise self.raise_on_put
        item = dict(kw["Item"])
        cond = _resolve(kw.get("ConditionExpression") or "",
                        kw.get("ExpressionAttributeNames"))
        if "attribute_not_exists(token)" in cond and item["token"] in self.items:
            raise ConditionFailed("attribute_not_exists(token)")
        self.items[item["token"]] = item
        return {}

    def get_item(self, Key):            # noqa: N803 - boto3's spelling
        self.gets += 1
        if self.raise_on_get:
            raise self.raise_on_get
        row = self.items.get(Key["token"])
        return {"Item": _ddb(dict(row))} if row else {}

    def delete_item(self, **kw):
        self.deletes += 1
        token = kw["Key"]["token"]
        cond = _resolve(kw.get("ConditionExpression") or "",
                        kw.get("ExpressionAttributeNames"))
        values = kw.get("ExpressionAttributeValues") or {}
        row = self.items.get(token)
        if row is None:
            # A conditional delete of a missing item fails the condition.
            raise ConditionFailed("no such item")
        m = re.fullmatch(r"\s*(\w+)\s*=\s*(:\w+)\s*", cond)
        if cond and not m:
            raise AssertionError(f"unrecognised ConditionExpression: {cond!r}")
        if m:
            field, placeholder = m.group(1), m.group(2)
            if str(row.get(field) or "") != str(values.get(placeholder) or ""):
                raise ConditionFailed(cond)
        del self.items[token]
        return {}

    def load(self):                      # pragma: no cover - never reached
        raise AssertionError("the fake table must not be 'connected'")


class TableCase(unittest.TestCase):
    """Base for anything that touches the table.

    ``_get_table`` is replaced rather than ``_table`` seeded, because half of
    these tests need it to answer None -- and ``_table = None`` is exactly the
    state that makes the real one reach for boto3. ``share._table`` is asserted
    to still be None afterwards, which is the check that no test quietly opened a
    connection.
    """

    def setUp(self):
        self.table = FakeTable()
        self._real_get_table = share._get_table
        share._get_table = lambda: self.table

    def tearDown(self):
        share._get_table = self._real_get_table
        self.assertIsNone(share._table, "share._table was populated for real")

    def no_table(self) -> None:
        share._get_table = lambda: None

    def forbid_table(self) -> None:
        def _boom():
            raise AssertionError("_get_table() must not be reached")
        share._get_table = _boom


# ── Recipient validation ────────────────────────────────────────────────────

class NormaliseEmailTests(unittest.TestCase):
    """The one place a typed address is vetted.

    Stricter than RFC 5322 on purpose. The interesting rejections are not the
    malformed ones -- they are the *well-formed* ones that mean something else
    downstream: a bare newline reaching an SES header, or a comma turning one
    recipient into two.
    """

    def test_ordinary_addresses_are_accepted(self):
        for addr in ("alice@example.com", "a.b+c@x.co.uk", "a_b-c@sub.domain.io",
                     "x@y-z.com", "user+tag@x.museum", "q@a.io"):
            self.assertEqual(share.normalise_email(addr), addr, addr)

    def test_addresses_are_lowercased(self):
        self.assertEqual(share.normalise_email("ALICE@Example.COM"),
                         "alice@example.com")
        self.assertEqual(share.normalise_email("MiXeD.Case+Tag@Sub.Domain.IO"),
                         "mixed.case+tag@sub.domain.io")

    def test_surrounding_whitespace_is_stripped(self):
        for raw in ("  bob@x.test", "bob@x.test\t", "\n bob@x.test \n"):
            self.assertEqual(share.normalise_email(raw), "bob@x.test", repr(raw))

    def test_empty_and_missing_are_rejected(self):
        for bad in ("", "   ", "\n", None, 0, False, [], {}):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_a_thing_that_is_not_an_address_is_rejected(self):
        for bad in ("nobody", "example.com", "@x.test", "a@", "a@@b.com"):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_internal_whitespace_is_rejected(self):
        for bad in ("a b@x.test", "a@x .test", "a@x.te st", "a\tb@x.test"):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_header_injection_is_rejected_here_not_at_ses(self):
        # The whole reason the pattern forbids \s rather than just leading and
        # trailing space: an embedded CR or LF in a To: value is a second header.
        for bad in ("a@b.com\nBcc: x@y.com",
                    "a@b.com\rBcc: x@y.com",
                    "a@b.com\r\nBcc: evil@x.com",
                    "a@b.com\nSubject: free money",
                    "a@b.com\n\n<html>body</html>"):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_multiple_recipients_smuggled_as_one_are_rejected(self):
        for bad in ("a@b.com, c@d.com", "a@b.com,c@d.com", "a@b.com;c@d.com",
                    "a@b.com; c@d.com"):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_display_name_forms_are_rejected(self):
        for bad in ("<a@b.com>", "Alice <a@b.com>", 'a"b@c.com',
                    '"Alice"@b.com', "a@b.com>"):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_a_missing_tld_is_rejected(self):
        for bad in ("a@b", "a@localhost", "a@b.", "a@b.c", "a@.com",
                    "a@b..com", "a@-b.com", "a@b-.com"):
            self.assertIsNone(share.normalise_email(bad), repr(bad))

    def test_length_is_bounded_at_254(self):
        long_local = "a" * 246 + "@ex.test"           # exactly 254
        self.assertEqual(len(long_local), 254)
        self.assertEqual(share.normalise_email(long_local), long_local)
        self.assertIsNone(share.normalise_email("a" * 247 + "@ex.test"))
        self.assertIsNone(share.normalise_email("a" * 300 + "@ex.test"))

    def test_the_length_check_survives_padding(self):
        # The cap is measured after strip(), so whitespace cannot be used to
        # smuggle a longer address past it, nor to fail a legal one.
        addr = "a" * 246 + "@ex.test"
        self.assertEqual(share.normalise_email(f"   {addr}   "), addr)


# ── The covering note ───────────────────────────────────────────────────────

class CleanNoteTests(unittest.TestCase):
    """The one free-text field a human writes and a stranger receives.

    Not the XSS boundary -- report_email escapes it into the mail and Jinja
    escapes it into the page -- so what is asserted here is that it stays a
    sentence or two, that nothing survives which corrupts a layout after both
    escapers have run, and that it does not quietly mangle real prose.
    """

    def test_a_normal_note_is_returned_unchanged(self):
        for note in ("Take a look at the entry levels.",
                     "Thoughts? I'm not sure about the 178 entry.",
                     "P/E looks rich — but the guide was +40% & the mix improved!",
                     "Re: NVDA (Q3) — see §2, ~$178, 50%/50% split; call me?"):
            self.assertEqual(share.clean_note(note), note, note)

    def test_cjk_and_other_non_ascii_survive_intact(self):
        for note in ("中文备注：请看第三段的估值区间。",
                     "看多，但是止损要放在 165 附近。",
                     "日本語のメモ、よろしく。",
                     "Résumé of the thesis — naïve, but ±2σ."):
            self.assertEqual(share.clean_note(note), note, note)

    def test_it_is_capped_at_note_max(self):
        self.assertEqual(len(share.clean_note("x" * 5000)), share.NOTE_MAX)
        self.assertEqual(len(share.clean_note("字" * 5000)), share.NOTE_MAX)
        exact = "y" * share.NOTE_MAX
        self.assertEqual(share.clean_note(exact), exact)

    def test_control_characters_are_stripped(self):
        # Everything below space except \n, which is meaningful in a note and is
        # turned into <br> by the mail. A NUL or a BEL survives both escapers
        # intact and still corrupts whatever renders it.
        dirty = "a\x00b\x07c\x1bd\x08e\x0bf\x0cg\x7f"
        cleaned = share.clean_note(dirty)
        for ch in cleaned:
            self.assertTrue(ch == "\n" or ch >= " ",
                            f"control char {ch!r} survived")
        for ch in ("\x00", "\x07", "\x1b", "\x08", "\x0b", "\x0c"):
            self.assertNotIn(ch, cleaned)

    def test_carriage_returns_do_not_survive_as_line_endings(self):
        # \r reaching a mail header is header injection, and the note is the one
        # field a later caller might be careless about placing.
        self.assertNotIn("\r", share.clean_note("one\r\ntwo\rthree"))

    def test_blank_line_runs_collapse(self):
        self.assertEqual(share.clean_note("one\n\n\n\n\ntwo"), "one\n\ntwo")
        self.assertEqual(share.clean_note("one\n\n\ntwo"), "one\n\ntwo")
        # Two is a paragraph break and is left alone; one is a line break.
        self.assertEqual(share.clean_note("one\n\ntwo"), "one\n\ntwo")
        self.assertEqual(share.clean_note("one\ntwo"), "one\ntwo")

    def test_space_runs_collapse(self):
        self.assertEqual(share.clean_note("far      apart"), "far apart")
        self.assertEqual(share.clean_note("a  b   c    d"), "a b c d")

    def test_leading_and_trailing_whitespace_goes(self):
        self.assertEqual(share.clean_note("\n\n  hello  \n\n"), "hello")

    def test_non_string_input_does_not_raise(self):
        self.assertEqual(share.clean_note(None), "")
        self.assertEqual(share.clean_note(""), "")
        self.assertEqual(share.clean_note(123), "123")
        self.assertEqual(share.clean_note(12.5), "12.5")

    def test_markup_is_left_alone_for_the_escapers_to_handle(self):
        # Deliberately *not* sanitised here: two downstream escapers own that,
        # and a third partial one would be the thing that disagrees with them.
        note = '<img src=x onerror=alert(1)>'
        self.assertEqual(share.clean_note(note), note)


# ── Masking the sharer on the public page ───────────────────────────────────

class MaskEmailTests(unittest.TestCase):
    """Who sent this is not part of what the sharer agreed to publish.

    The page is readable by anyone the link reaches, including whoever it was
    forwarded to, so the domain in particular must not survive -- for a corporate
    address that is the employer.
    """

    def test_the_local_part_is_kept_and_the_domain_is_not(self):
        self.assertEqual(share.mask_email("alice@example.com"), "alice@…")
        self.assertEqual(share.mask_email("bob.smith+tag@acme-corp.co.uk"),
                         "bob.smith+tag@…")

    def test_no_domain_ever_leaks(self):
        for addr, domain in (("alice@example.com", "example.com"),
                             ("x@bigbank.internal", "bigbank.internal"),
                             ("a@b.io", "b.io")):
            masked = share.mask_email(addr)
            self.assertNotIn(domain, masked, addr)
            self.assertNotIn("@" + domain, masked, addr)

    def test_garbage_and_emptiness_mask_to_nothing(self):
        for bad in ("", "   ", None, "nobody", "not an address", "@example.com",
                    "  @example.com  "):
            self.assertEqual(share.mask_email(bad), "", repr(bad))

    def test_case_is_preserved_rather_than_invented(self):
        # create() already lowercases what it stores, so this only has to avoid
        # doing anything surprising to a row written by hand.
        self.assertEqual(share.mask_email("Alice@Example.com"), "Alice@…")


# ── Reading a share ─────────────────────────────────────────────────────────

class LookupTests(TableCase):
    """Shape first, then the read, then the expiry.

    The order is the point: ``/agents/shared/<token>`` takes arbitrary path text
    from anyone, and every uncached ``get_item`` is billed, so a crawler walking
    the URL space must not be able to turn that into a DynamoDB request per hit.
    """

    def _row(self, token: str, **over) -> dict:
        row = {"token": token, "job_id": "abc123def4567890", "owner": OWNER,
               "sharer": SHARER, "recipient": FRIEND, "note": "",
               "ticker": "NVDA", "lang": "en",
               "created_at": "2026-08-29T12:00:00+00:00",
               "expires_at": int((datetime.now(timezone.utc)
                                  + timedelta(days=30)).timestamp())}
        row.update(over)
        self.table.items[token] = row
        return row

    def test_a_live_row_comes_back(self):
        self._row("A" * 22)
        got = share.lookup("A" * 22)
        self.assertIsNotNone(got)
        self.assertEqual(got["job_id"], "abc123def4567890")
        self.assertEqual(self.table.gets, 1)

    def test_a_short_or_long_token_never_reaches_the_table(self):
        for bad in ("", "x", "abc", "a" * 15, "a" * 65, "a" * 300):
            self.assertIsNone(share.lookup(bad), repr(bad))
        self.assertEqual(self.table.gets, 0, "a malformed token was billed a read")

    def test_a_bad_alphabet_never_reaches_the_table(self):
        for bad in ("a" * 10 + "!" * 12, "../" + "a" * 19,
                    "tok with spaces here!!", "a" * 21 + "/", "a" * 21 + "%",
                    "a" * 21 + "=", "abc" + "\x00" * 19, "ü" * 22,
                    "a" * 20 + "'--", "{}" + "a" * 20, "a" * 21 + "\n" + "b" * 21,
                    # These two are the reason _valid_token exists. lookup() used
                    # to strip() before validating, which *repaired* them into a
                    # valid token and spent a billed read on each.
                    "a" * 21 + " ", "a" * 21 + "\n"):
            self.assertIsNone(share.lookup(bad), repr(bad))
        self.assertEqual(self.table.gets, 0, "a malformed token was billed a read")

    def test_a_padded_token_is_rejected_rather_than_repaired(self):
        # The opposite rule to normalise_email, deliberately: an address is prose
        # a human typed, a token is an opaque credential in a URL path. Repairing
        # the ends would also make several URLs address one share.
        self._row("A" * 22)
        for padded in (" " + "A" * 22, "A" * 22 + "\n", "\t " + "A" * 22 + " \n",
                       "A" * 22 + " "):
            self.assertIsNone(share.lookup(padded), repr(padded))
        self.assertEqual(self.table.gets, 0)
        self.assertIsNotNone(share.lookup("A" * 22), "the unpadded token broke")

    def test_none_and_non_strings_never_reach_the_table(self):
        for bad in (None, 0, False, [], {}, 12345):
            self.assertIsNone(share.lookup(bad), repr(bad))
        self.assertEqual(self.table.gets, 0)

    def test_the_shape_gate_runs_before_the_table_is_even_asked_for(self):
        self.forbid_table()
        self.assertIsNone(share.lookup("!!!"))

    def test_an_unknown_but_well_formed_token_is_one_read_and_none(self):
        self.assertIsNone(share.lookup("Z" * 22))
        self.assertEqual(self.table.gets, 1)

    def test_an_expired_row_is_not_readable_even_though_it_is_present(self):
        # DynamoDB's TTL sweeper is best-effort and can run up to 48h late, so a
        # row past its date is expected to still be there.
        past = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp())
        self._row("B" * 22, expires_at=past)
        self.assertIsNone(share.lookup("B" * 22))
        self.assertIn("B" * 22, self.table.items, "the row should still exist")

    def test_expiry_is_checked_against_now_not_the_created_date(self):
        long_gone = int((datetime.now(timezone.utc) - timedelta(days=400)).timestamp())
        self._row("C" * 22, expires_at=long_gone)
        self.assertIsNone(share.lookup("C" * 22))

    def test_a_row_expiring_in_a_moment_is_still_live(self):
        soon = int((datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp())
        self._row("D" * 22, expires_at=soon)
        self.assertIsNotNone(share.lookup("D" * 22))

    def test_a_missing_or_unparseable_expiry_is_treated_as_expired(self):
        # Fail closed. "No expiry" must not mean "forever".
        self._row("E" * 22)
        del self.table.items["E" * 22]["expires_at"]
        self.assertIsNone(share.lookup("E" * 22))
        for junk in ("banana", "", None, [], {}):
            self._row("F" * 22, expires_at=junk)
            self.assertIsNone(share.lookup("F" * 22), repr(junk))

    def test_a_decimal_expiry_is_understood(self):
        # The resource layer hands numbers back as Decimal; a comparison that
        # only worked on int would expire every live share.
        self._row("G" * 22)
        self.assertIsInstance(
            share._get_table().get_item(Key={"token": "G" * 22})["Item"]["expires_at"],
            Decimal)
        self.assertIsNotNone(share.lookup("G" * 22))

    def test_no_table_means_no_share(self):
        self.no_table()
        self.assertIsNone(share.lookup("A" * 22))

    def test_a_failed_read_is_none_rather_than_an_exception(self):
        self._row("H" * 22)
        self.table.raise_on_get = RuntimeError("throttled")
        self.assertIsNone(share.lookup("H" * 22))


# ── Minting a share ─────────────────────────────────────────────────────────

class CreateTests(TableCase):
    """create() is the share; the mail is only its delivery.

    So None has to mean "send nothing", and it has to mean that *before* anything
    is written -- which is why the storage-unavailable case is asserted on the
    write count and not just on the return value.
    """

    def test_no_table_returns_none_and_writes_nothing(self):
        self.no_table()
        self.assertIsNone(share.create(make_job(), SHARER, FRIEND, "hi"))
        self.assertEqual(self.table.puts, 0)
        self.assertEqual(self.table.items, {})

    def test_a_write_failure_returns_none(self):
        self.table.raise_on_put = ConditionFailed("collision")
        self.assertIsNone(share.create(make_job(), SHARER, FRIEND))
        self.assertEqual(self.table.items, {})

    def test_a_job_with_no_id_is_refused(self):
        for bad in ("", None, "   "):
            self.assertIsNone(share.create(make_job(id=bad), SHARER, FRIEND),
                              repr(bad))
        self.assertEqual(self.table.puts, 0)

    def test_a_job_with_no_owner_is_refused(self):
        for bad in ("", None, "   "):
            self.assertIsNone(share.create(make_job(user=bad), SHARER, FRIEND),
                              repr(bad))
        self.assertEqual(self.table.puts, 0)

    def test_the_row_is_written_under_its_token(self):
        row = share.create(make_job(), SHARER, FRIEND, "look at this")
        self.assertIsNotNone(row)
        self.assertEqual(list(self.table.items), [row["token"]])
        self.assertEqual(self.table.items[row["token"]]["job_id"],
                         "abc123def4567890")

    def test_the_token_has_the_expected_length_and_alphabet(self):
        seen = set()
        for _ in range(25):
            row = share.create(make_job(), SHARER, FRIEND)
            token = row["token"]
            self.assertEqual(len(token), 22, token)      # 16 bytes url-safe
            self.assertRegex(token, r"^[A-Za-z0-9_-]{22}$", token)
            seen.add(token)
        self.assertEqual(len(seen), 25, "tokens repeated")

    def test_a_minted_token_is_one_lookup_accepts(self):
        # The two ends of the same rule. A token 15 characters long, or one
        # containing '=', would be written and then refused on read.
        row = share.create(make_job(), SHARER, FRIEND)
        self.assertTrue(16 <= len(row["token"]) <= 64)
        self.assertIsNotNone(share.lookup(row["token"]))

    def test_addresses_are_lowercased_on_the_way_in(self):
        row = share.create(make_job(user="Owner@Example.COM"),
                           "Alice@Example.com", "BOB@example.NET")
        self.assertEqual(row["owner"], "owner@example.com")
        self.assertEqual(row["sharer"], "alice@example.com")
        self.assertEqual(row["recipient"], "bob@example.net")

    def test_the_expiry_is_ttl_days_ahead(self):
        before = datetime.now(timezone.utc)
        row = share.create(make_job(), SHARER, FRIEND)
        want = (before + timedelta(days=share.TTL_DAYS)).timestamp()
        self.assertIsInstance(row["expires_at"], int)
        self.assertAlmostEqual(row["expires_at"], want, delta=120)
        self.assertEqual(share.TTL_DAYS, 30)

    def test_the_note_is_cleaned_on_the_way_in(self):
        row = share.create(make_job(), SHARER, FRIEND,
                           "  a\x00b   c\n\n\n\nd  " + "z" * 5000)
        self.assertNotIn("\x00", row["note"])
        self.assertEqual(len(row["note"]), share.NOTE_MAX)
        self.assertTrue(row["note"].startswith("ab c\n\nd"))

    def test_a_missing_note_is_an_empty_string_not_none(self):
        row = share.create(make_job(), SHARER, FRIEND)
        self.assertEqual(row["note"], "")

    def test_the_ticker_is_bounded_and_the_language_normalised(self):
        row = share.create(make_job(ticker="A" * 40, lang="ZH"), SHARER, FRIEND)
        self.assertEqual(len(row["ticker"]), 16)
        self.assertEqual(row["lang"], "zh")
        for code in ("en", "EN", "fr", "", None, "zh-TW"):
            row = share.create(make_job(lang=code), SHARER, FRIEND)
            self.assertEqual(row["lang"], "en", repr(code))

    def test_the_row_carries_no_report_body(self):
        # The report is read from the job record at serve time. Copying it here
        # would double the storage and let the two drift.
        row = share.create(make_job(), SHARER, FRIEND)
        self.assertNotIn("report", row)
        self.assertNotIn("user", row)

    def test_channel_defaults_to_email(self):
        # Every caller before the SMS channel existed omitted this argument, so
        # the default has to reproduce the only behaviour that ever shipped.
        row = share.create(make_job(), SHARER, FRIEND)
        self.assertEqual(row["channel"], "email")

    def test_an_sms_share_is_recorded_with_no_recipient(self):
        # The whole point of "sms" is that this process never learns who the
        # link goes to -- the client never even collects an address for it, so
        # create() must accept an empty recipient for this channel rather than
        # refusing it the way a blank email address would be refused upstream.
        row = share.create(make_job(), SHARER, "", channel="sms")
        self.assertIsNotNone(row)
        self.assertEqual(row["channel"], "sms")
        self.assertEqual(row["recipient"], "")

    def test_an_unrecognised_channel_falls_back_to_email(self):
        # A client from the future sending a channel this version does not
        # know about should degrade to the original behaviour, not be refused
        # or silently mis-recorded as something unrecognisable.
        for bad in ("carrier-pigeon", "EMAIL", "Sms", "", None, 123, ["sms"]):
            row = share.create(make_job(), SHARER, FRIEND, channel=bad)
            self.assertEqual(row["channel"], "email", repr(bad))

    def test_channel_does_not_affect_the_rest_of_the_row(self):
        email_row = share.create(make_job(), SHARER, FRIEND, "hi", "email")
        sms_row = share.create(make_job(), SHARER, "", "hi", "sms")
        for key in ("job_id", "owner", "sharer", "note", "ticker", "lang"):
            self.assertEqual(email_row[key], sms_row[key], key)

    def test_a_token_collision_fails_the_write_rather_than_repointing_a_share(self):
        first = share.create(make_job(), SHARER, FRIEND)
        fixed = first["token"]
        # The module's reference is swapped, not the stdlib module mutated: a
        # collision cannot be provoked any other way, and monkeypatching
        # secrets.token_urlsafe itself would leak into every other test in the run
        # if anything raised in between.
        real = share.secrets
        share.secrets = types.SimpleNamespace(token_urlsafe=lambda n: fixed)
        try:
            second = share.create(make_job(id="other-job"), SHARER, FRIEND)
        finally:
            share.secrets = real
        self.assertIsNone(second)
        self.assertEqual(self.table.items[fixed]["job_id"], "abc123def4567890")


# ── Revoking ────────────────────────────────────────────────────────────────

class RevokeTests(TableCase):
    """Only the person who minted the link may kill it.

    Everyone who was ever forwarded the mail holds a valid token, so an
    unconditioned delete would make "revoke" a way to destroy other people's
    shares by replaying a link you were sent.
    """

    def _make(self, sharer: str = SHARER) -> str:
        return share.create(make_job(), sharer, FRIEND)["token"]

    def test_the_sharer_can_revoke(self):
        token = self._make()
        self.assertTrue(share.revoke(token, SHARER))
        self.assertEqual(self.table.items, {})
        self.assertIsNone(share.lookup(token))

    def test_case_does_not_matter_for_the_sharer(self):
        token = self._make()
        self.assertTrue(share.revoke(token, "ALICE@Example.COM"))

    def test_somebody_else_cannot_revoke(self):
        token = self._make()
        for other in ("mallory@example.org", FRIEND, OWNER, "alice@example.co"):
            self.assertFalse(share.revoke(token, other), other)
        self.assertIn(token, self.table.items)
        self.assertIsNotNone(share.lookup(token))

    def test_a_missing_token_fails(self):
        for bad in ("", None, "   ", "Z" * 22, "nope"):
            self.assertFalse(share.revoke(bad, SHARER), repr(bad))

    def test_a_missing_requester_fails(self):
        token = self._make()
        for bad in ("", None, "   "):
            self.assertFalse(share.revoke(token, bad), repr(bad))
        self.assertIn(token, self.table.items)

    def test_an_empty_token_or_requester_is_not_even_a_delete(self):
        share.revoke("", SHARER)
        share.revoke("Z" * 22, "")
        self.assertEqual(self.table.deletes, 0)

    def test_a_malformed_token_never_reaches_the_table(self):
        # Same gate as lookup(), and for the same reason: this route is behind
        # sign-in but the token space is not, and a billed write per attempt is
        # worse than a billed read.
        self.forbid_table()
        for bad in ("", "   ", "nope", "a" * 21 + " ", "a" * 21 + "!",
                    "a" * 65, None, 12345):
            self.assertFalse(share.revoke(bad, SHARER), repr(bad))

    def test_a_padded_token_does_not_revoke(self):
        token = self._make()
        self.assertFalse(share.revoke(f" {token}", SHARER))
        self.assertIn(token, self.table.items)
        self.assertEqual(self.table.deletes, 0)

    def test_no_table_means_no_revoke(self):
        token = self._make()
        self.no_table()
        self.assertFalse(share.revoke(token, SHARER))

    def test_revoking_twice_reports_failure_the_second_time(self):
        token = self._make()
        self.assertTrue(share.revoke(token, SHARER))
        self.assertFalse(share.revoke(token, SHARER))


# ── What the public route may return ────────────────────────────────────────

class PublicPayloadTests(unittest.TestCase):
    """The most consequential function in the module.

    ``/api/agents/shared/<token>`` is unauthenticated, so this is a publishing
    decision, not a formatting one. The job record it draws from carries the
    owner's address, the follow-up conversation, the runner's pid and its stderr,
    and every one of those sits one key away from the report.
    """

    def _row(self, **over) -> dict:
        row = {"token": "T" * 22, "job_id": "abc123def4567890", "owner": OWNER,
               "sharer": SHARER, "recipient": FRIEND, "note": "have a look",
               "ticker": "NVDA", "lang": "en",
               "created_at": "2026-08-29T12:00:00+00:00",
               "expires_at": 1800000000}
        row.update(over)
        return row

    def _loaded_job(self, **over) -> dict:
        """A job record with every owner-only field populated with a sentinel."""
        job = make_job(
            user=OWNER,
            chat=[{"q": "SENTINEL_CHAT_QUESTION", "a": "SENTINEL_CHAT_ANSWER"}],
            log="SENTINEL_STDERR_TRACEBACK",
            pid=424242,
            error="SENTINEL_ERROR_TEXT",
            returncode=137,
            cmd=["python", "SENTINEL_ARGV"],
            selftest=False,
            api_key="SENTINEL_API_KEY",
        )
        job.update(over)
        return job

    def test_the_owners_address_is_nowhere_in_the_payload(self):
        payload = share.public_payload(self._row(), self._loaded_job())
        self.assertNotIn("user", payload)
        blob = json.dumps(payload, default=str)
        self.assertNotIn(OWNER, blob, "the owner's address was published")
        self.assertNotIn("owner@", blob)

    def test_the_owners_address_is_hidden_even_when_they_are_the_sharer(self):
        # The common case: you share your own run. The masked form may keep the
        # local part, but the address itself must not appear.
        payload = share.public_payload(self._row(sharer=OWNER),
                                       self._loaded_job())
        blob = json.dumps(payload, default=str)
        self.assertNotIn(OWNER, blob)
        self.assertEqual(payload["shared"]["by"], "owner@…")

    def test_the_recipient_is_not_published_either(self):
        # The row knows who it was mailed to; a forwarded link must not.
        payload = share.public_payload(self._row(), self._loaded_job())
        self.assertNotIn(FRIEND, json.dumps(payload, default=str))

    def test_owner_only_fields_are_absent(self):
        payload = share.public_payload(self._row(), self._loaded_job())
        for field in ("chat", "log", "pid", "error", "returncode", "cmd",
                      "user", "api_key", "selftest", "token", "recipient",
                      "owner"):
            self.assertNotIn(field, payload, f"{field} leaked into the payload")

    def test_no_owner_only_value_survives_serialisation(self):
        # Asserted on the serialised form rather than the key list, so a field
        # nested inside a published one cannot slip through.
        payload = share.public_payload(self._row(), self._loaded_job())
        blob = json.dumps(payload, default=str)
        for sentinel in ("SENTINEL_CHAT_QUESTION", "SENTINEL_CHAT_ANSWER",
                         "SENTINEL_STDERR_TRACEBACK", "SENTINEL_ERROR_TEXT",
                         "SENTINEL_ARGV", "SENTINEL_API_KEY", "424242", "137"):
            self.assertNotIn(sentinel, blob, f"{sentinel} was published")

    def test_a_new_job_field_is_invisible_until_allowlisted(self):
        job = self._loaded_job(newly_added_field="SENTINEL_FUTURE_FIELD")
        payload = share.public_payload(self._row(), job)
        self.assertNotIn("newly_added_field", payload)
        self.assertNotIn("SENTINEL_FUTURE_FIELD",
                         json.dumps(payload, default=str))

    def test_the_report_and_its_context_are_published(self):
        payload = share.public_payload(self._row(), self._loaded_job())
        for field in ("id", "ticker", "date", "status", "decision", "report",
                      "lang", "elapsed_sec"):
            self.assertIn(field, payload, f"{field} should be shareable")
        self.assertIn("Portfolio Manager", payload["report"])

    def test_absent_job_fields_are_omitted_rather_than_nulled(self):
        payload = share.public_payload(self._row(), {"id": "x", "report": "r"})
        self.assertEqual(set(payload) - {"shared"}, {"id", "report"})

    def test_the_sharer_is_masked_not_named(self):
        payload = share.public_payload(self._row(), self._loaded_job())
        self.assertEqual(payload["shared"]["by"], "alice@…")
        self.assertNotIn("example.com", payload["shared"]["by"])
        self.assertNotIn(SHARER, json.dumps(payload, default=str))

    def test_the_share_provenance_travels_with_the_payload(self):
        payload = share.public_payload(self._row(), self._loaded_job())
        self.assertEqual(payload["shared"]["note"], "have a look")
        self.assertEqual(payload["shared"]["at"], "2026-08-29T12:00:00+00:00")
        self.assertEqual(payload["shared"]["expires_at"], 1800000000)

    def test_the_delivery_channel_is_not_published(self):
        # channel is bookkeeping for this process's own logs, not something a
        # reader landing on the page needs -- "how did this link reach me" is
        # not part of what the sharer agreed to publish, the same reasoning
        # mask_email already applies to who sent it.
        payload = share.public_payload(self._row(channel="sms"), self._loaded_job())
        self.assertNotIn("channel", payload)
        self.assertNotIn("channel", payload["shared"])

    def test_a_decimal_expiry_is_serialisable(self):
        # Straight off a DynamoDB read, expires_at is a Decimal, which
        # json.dumps refuses. int() in public_payload is what stops a 500.
        payload = share.public_payload(self._row(expires_at=Decimal(1800000000)),
                                       self._loaded_job())
        self.assertIsInstance(payload["shared"]["expires_at"], int)
        json.dumps(payload)                        # must not raise

    def test_a_garbled_row_does_not_produce_nulls(self):
        payload = share.public_payload({}, self._loaded_job())
        self.assertEqual(payload["shared"]["by"], "")
        self.assertEqual(payload["shared"]["note"], "")
        self.assertEqual(payload["shared"]["expires_at"], 0)


# ── The public URL ──────────────────────────────────────────────────────────

class ShareUrlTests(unittest.TestCase):
    def test_the_url_is_the_capability_route(self):
        self.assertEqual(share.share_url("tok", base="https://trade-agents.com"),
                         "https://trade-agents.com/agents/shared/tok")

    def test_a_trailing_slash_on_the_base_does_not_double(self):
        self.assertEqual(share.share_url("tok", base="https://x.test/"),
                         "https://x.test/agents/shared/tok")

    def test_it_falls_back_to_the_mail_base(self):
        old = dict(os.environ)
        try:
            os.environ["AGENTS_BASE_URL"] = "https://trade-agents.com"
            self.assertEqual(share.share_url("tok"),
                             "https://trade-agents.com/agents/shared/tok")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_the_default_base_is_the_trade_agents_brand(self):
        # Not stock.li-family.us: the hosts have separate sessions, and the mail
        # and the link have to agree on which one the reader lands on.
        old = dict(os.environ)
        try:
            os.environ.pop("AGENTS_BASE_URL", None)
            os.environ.pop("APP_BASE_URL", None)
            self.assertTrue(share.share_url("tok").startswith(
                "https://trade-agents.com/agents/shared/"))
        finally:
            os.environ.clear()
            os.environ.update(old)


# ── Configuration ───────────────────────────────────────────────────────────

class EnabledTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_it_needs_a_from_address(self):
        os.environ.pop("AGENTS_SHARE", None)
        os.environ.pop("SES_FROM_EMAIL", None)
        self.assertFalse(share.enabled())
        os.environ["SES_FROM_EMAIL"] = "from@x.test"
        self.assertTrue(share.enabled())

    def test_the_kill_switch_is_its_own(self):
        # Separate from AGENTS_EMAIL_REPORT: turning off completion mail must not
        # also turn off sharing, and vice versa.
        os.environ["SES_FROM_EMAIL"] = "from@x.test"
        os.environ["AGENTS_EMAIL_REPORT"] = "0"
        self.assertTrue(share.enabled())
        for off in ("0", "false", "no", "off", "OFF", " Off "):
            os.environ["AGENTS_SHARE"] = off
            self.assertFalse(share.enabled(), off)
        for on in ("1", "true", "yes", ""):
            os.environ["AGENTS_SHARE"] = on
            self.assertTrue(share.enabled(), on)


# ── The shared mail ─────────────────────────────────────────────────────────

class ShareMailTests(unittest.TestCase):
    """build(shared_by=…) turns the completion mail into a share.

    One builder rather than two, so what is worth asserting is that the *shared*
    shape actually differs everywhere it has to: the subject, the banner, the
    call to action, the link behind it and the footer's explanation of why this
    arrived. Each of those has a failure mode that is silent -- a dead button, or
    a stranger being told they ran an analysis they have never heard of.
    """

    def test_the_subject_names_the_sharer_and_withholds_the_verdict(self):
        subject, _, _ = mail.build(make_job(), shared_by=SHARER)
        self.assertIn(SHARER, subject)
        self.assertIn("NVDA", subject)
        self.assertIn("August 29, 2026", subject)
        self.assertIn("shared", subject)
        # "BUY" in the subject of mail nobody asked for reads as a tip being
        # pushed at them; who sent it is the part that makes it openable.
        self.assertNotIn("BUY", subject)
        self.assertNotIn("analysis ready", subject)

    def test_the_unshared_subject_is_untouched(self):
        subject, html, _ = mail.build(make_job())
        self.assertIn("NVDA analysis ready", subject)
        self.assertIn("BUY", subject)
        self.assertNotIn("shared", subject)
        self.assertNotIn("shared this report with you", html)

    def test_the_banner_names_the_sharer_above_the_report(self):
        _, html, _ = mail.build(make_job(), shared_by=SHARER)
        line = f"{SHARER} shared this report with you"
        self.assertIn(line, html)
        # Above the report and below the masthead: the explanation has to arrive
        # before the content, not in the footer.
        self.assertLess(html.index(line), html.index("Market Analyst"))
        self.assertLess(html.index("NVDA — AI research report"), html.index(line))

    def test_the_note_is_shown_with_its_label(self):
        _, html, text = mail.build(make_job(), shared_by=SHARER,
                                   note="Take a look at the entry levels.")
        self.assertIn("Take a look at the entry levels.", html)
        self.assertIn("Their note", html)
        self.assertIn("Take a look at the entry levels.", text)

    def test_a_multiline_note_keeps_its_lines(self):
        _, html, _ = mail.build(make_job(), shared_by=SHARER,
                                note="First thought.\nSecond thought.")
        self.assertIn("First thought.<br>Second thought.", html)

    def test_no_note_means_no_note_block(self):
        _, html, _ = mail.build(make_job(), shared_by=SHARER, note="")
        self.assertIn("shared this report with you", html)
        self.assertNotIn("Their note", html)
        _, html2, _ = mail.build(make_job(), shared_by=SHARER, note="   \n  ")
        self.assertNotIn("Their note", html2)

    def test_the_footer_explains_the_share_and_not_a_subscription(self):
        _, html, text = mail.build(make_job(), shared_by=SHARER)
        self.assertIn(f"You are receiving this because {SHARER} entered your "
                      "address on", html)
        self.assertIn("You are not subscribed to anything", html)
        # The unshared footer claims "you ran this analysis", which is false for
        # a recipient and is the sentence a reader checks before trusting mail.
        self.assertNotIn("you ran this analysis", html)
        self.assertNotIn("you ran this analysis", text)

    def test_the_footer_promises_the_expiry_that_is_actually_enforced(self):
        _, html, _ = mail.build(make_job(), shared_by=SHARER)
        self.assertIn(f"stops working in {share.TTL_DAYS} days", html)

    def test_no_unsubstituted_placeholder_survives_a_share(self):
        for lang in ("en", "zh"):
            _, html, text = mail.build(make_job(lang=lang), shared_by=SHARER,
                                       note="hi")
            for blob in (html, text):
                self.assertNotRegex(blob, r"\{\w+\}", lang)

    def test_the_call_to_action_is_the_shared_one(self):
        _, html, text = mail.build(make_job(), shared_by=SHARER)
        self.assertIn("Open the shared report", html)
        self.assertNotIn("Open the full report", html)
        self.assertIn("Open the shared report", text)

    def test_the_link_override_is_what_the_buttons_point_at(self):
        url = "https://trade-agents.com/agents/shared/" + "t" * 22
        _, html, text = mail.build(make_job(), shared_by=SHARER,
                                   link_override=url)
        self.assertIn(f'href="{url}"', html)
        self.assertIn(url, text)
        # /agents?job=<id> is owner-or-VIP and 404s for everyone else, so the
        # recipient of a share would find the one button in the mail dead.
        self.assertNotIn("/agents?job=", html)
        self.assertNotIn("/agents?job=", text)
        self.assertNotIn(make_job()["id"], html)
        self.assertNotIn(make_job()["id"], text)

    def test_the_override_also_replaces_the_masthead_and_clip_links(self):
        url = "https://trade-agents.com/agents/shared/" + "t" * 22
        _, html, _ = mail.build(make_job(report=build_report(8)),
                                shared_by=SHARER, link_override=url)
        self.assertIn("border:1px dashed", html)          # it did clip
        self.assertNotIn("/agents?job=", html)
        self.assertEqual(html.count("/agents?job="), 0)
        self.assertGreaterEqual(html.count(url), 3)       # masthead, CTA, clip

    def test_without_an_override_a_share_still_links_somewhere(self):
        # Belt and braces: build() must not produce a mail with no link at all
        # if a caller forgets the override.
        _, html, _ = mail.build(make_job(), shared_by=SHARER,
                                link_base="https://trade-agents.com")
        self.assertIn("https://trade-agents.com/agents?job=abc123def4567890", html)

    def test_the_share_is_branded_by_the_link_host(self):
        _, html, _ = mail.build(make_job(), shared_by=SHARER,
                                link_base="https://trade-agents.com")
        self.assertIn("TradeAgents", html)
        self.assertNotIn("yStocker", html)
        _, html2, _ = mail.build(make_job(), shared_by=SHARER,
                                 link_base="https://stock.li-family.us")
        self.assertIn("yStocker", html2)

    def test_a_chinese_report_is_shared_in_chinese(self):
        subject, html, text = mail.build(make_job(lang="zh"), shared_by=SHARER,
                                         note="请看第三段。")
        self.assertIn("与您分享了", subject)
        self.assertIn("2026年8月29日", subject)
        self.assertIn(f"{SHARER} 与您分享了这份报告", html)
        self.assertIn("对方留言", html)
        self.assertIn("请看第三段。", html)
        self.assertIn("打开分享的报告", html)
        self.assertIn(f"您收到此邮件是因为 {SHARER} 在", html)
        self.assertIn(f"此链接将在 {share.TTL_DAYS} 天后失效", html)
        self.assertNotIn("上运行了本次分析", html)
        self.assertIn("与您分享了这份报告", text)

    def test_a_share_of_a_reportless_run_is_none(self):
        for empty in ("", None, "   \n "):
            self.assertIsNone(mail.build(make_job(report=empty),
                                         shared_by=SHARER), repr(empty))

    def test_the_report_itself_is_unchanged_by_sharing(self):
        _, plain, _ = mail.build(make_job())
        _, shared, _ = mail.build(make_job(), shared_by=SHARER)
        for marker in ("Market Analyst", "Portfolio Manager",
                       "Entry 178, stop 165", "Analyst Team Reports"):
            self.assertIn(marker, plain, marker)
            self.assertIn(marker, shared, marker)

    def test_a_long_sharer_address_is_bounded(self):
        _, html, _ = mail.build(make_job(), shared_by="a" * 400 + "@x.test")
        self.assertNotIn("a" * 200, html)


class ShareBudgetTests(unittest.TestCase):
    """The banner is chrome outside the report rows, so it must be charged.

    Gmail clips at ~102 KB silently, mid-element. Everything ``_body_rows``
    does to stop at a section boundary is wasted if a banner and a 500-character
    note push the finished mail past the limit anyway.
    """

    LINK = "https://trade-agents.com/agents/shared/" + "t" * 22

    def test_a_shared_mail_stays_under_the_clip_limit(self):
        note = "x" * share.NOTE_MAX
        for size in (1, 3, 8, 20):
            _, html, _ = mail.build(make_job(report=build_report(size)),
                                    shared_by="a" * 90 + "@x.test", note=note,
                                    link_override=self.LINK)
            self.assertLess(len(html), 102_000, f"per_section={size}")

    def test_reserved_budget_shrinks_the_body(self):
        job = make_job(report=build_report(8))
        free = mail._body_rows(job, "en", self.LINK, reserved=0)
        charged = mail._body_rows(job, "en", self.LINK, reserved=30_000)
        self.assertLess(len(free), mail._HTML_BUDGET)
        self.assertLess(len(charged), len(free))
        self.assertLess(len(charged), mail._HTML_BUDGET - 30_000)

    def test_the_decision_survives_a_reserved_budget(self):
        # The reserve-the-decision rule has to hold against the *reduced*
        # budget too, or a share drops the answer and keeps the analysts.
        job = make_job(report=build_report(8))
        for reserved in (0, 5_000, 40_000, 77_000):
            rows = mail._body_rows(job, "en", self.LINK, reserved=reserved)
            self.assertIn("Portfolio Manager", rows, f"reserved={reserved}")
            self.assertIn("Entry 178, stop 165", rows, f"reserved={reserved}")

    def test_an_absurd_reserve_still_sends_something(self):
        job = make_job(report=build_report(8))
        rows = mail._body_rows(job, "en", self.LINK, reserved=10 ** 7)
        self.assertTrue(rows)
        self.assertIn("Portfolio Manager", rows)

    def test_a_negative_reserve_is_not_extra_budget(self):
        job = make_job(report=build_report(8))
        free = mail._body_rows(job, "en", self.LINK, reserved=0)
        self.assertEqual(len(mail._body_rows(job, "en", self.LINK, reserved=-50_000)),
                         len(free))

    def test_the_banner_is_charged_at_its_real_size(self):
        # Asserted on the argument rather than on the output, deliberately. A
        # banner is ~1 KB and a section is ~19 KB, so charging it changes nothing
        # about which sections fit for any realistic report -- an output-shaped
        # test here would pass just as happily with reserved=0 and would catch
        # nothing. What must hold is that the number is wired through.
        seen: list[int] = []
        real = mail._body_rows

        def spy(job, lang, link, reserved=0):
            seen.append(reserved)
            return real(job, lang, link, reserved=reserved)

        mail._body_rows = spy
        try:
            note = "z" * share.NOTE_MAX
            mail.build(make_job(), shared_by=SHARER, note=note,
                       link_override=self.LINK)
            mail.build(make_job())
        finally:
            mail._body_rows = real
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], len(mail._share_banner(SHARER, note, "en")))
        self.assertGreater(seen[0], 0)
        self.assertEqual(seen[1], 0, "an unshared mail reserved something")


class ShareEscapingTests(unittest.TestCase):
    """The note and the sharer are user input, unlike the report.

    The report comes from our own model; the note comes from one user and is
    delivered to another. ``_share_banner`` escapes it rather than running it
    through ``render()``, which would honour Markdown and a subset of inline
    HTML.

    Two complementary checks, because each covers the other's blind spot.
    ``assert_inert`` strips escaped sequences and scans for live needles, which
    is the technique the rest of this suite uses -- but it cannot see a payload
    with no angle brackets at all, such as a bare ``javascript:`` URL, and
    reports it as live when it is plain text. ``assert_no_new_markup`` parses the
    document instead and compares it with the same mail built from a harmless
    note, which is immune to that by construction.
    """

    BENIGN = "a perfectly ordinary covering note"

    ANGLED = (
        '<img src=x onerror=alert(1)>',
        '<a href="javascript:alert(1)">x</a>',
        '<script>alert(document.cookie)</script>',
        '<style>body{display:none}</style>',
        '<iframe src="data:text/html,<b>x</b>"></iframe>',
        '<svg/onload=alert(1)>',
        '"><img src=x onerror=alert(1)>',
        '</td></tr><tr><td onclick="evil()">injected',
        '</div></div><form action="https://evil.test"><input name=p>',
        '<b onmouseover="evil()">hover</b>',
        '<a href="data:text/html;base64,PHN2Zz4=">x</a>',
        '<object data="x"></object><embed src="y">',
    )

    # No markup to escape, so the substring scan cannot tell these from a live
    # attribute; the structural check can.
    UNANGLED = (
        '[click](javascript:alert(1))',
        '[click](data:text/html;base64,PHN2Zz4=)',
        'javascript:alert(1)',
        'style=display:none',
    )

    HOSTILE = ANGLED + UNANGLED

    def _mail(self, note: str) -> str:
        return mail.build(make_job(), shared_by=SHARER, note=note)[1]

    def test_a_hostile_note_adds_no_element_and_no_attribute(self):
        baseline = self._mail(self.BENIGN)
        for bad in self.HOSTILE:
            assert_no_new_markup(self, self._mail(bad), baseline, bad)

    def test_the_structural_check_can_actually_fail(self):
        # A negative control for the helper itself. A parse-based assertion that
        # silently saw no elements would pass every case above, which is the same
        # class of worthlessness as a substring scan matching its own escaping.
        with self.assertRaises(AssertionError):
            assert_no_new_markup(self, '<div><img src=x onerror=alert(1)></div>',
                                 "<div>note</div>")
        with self.assertRaises(AssertionError):
            assert_no_new_markup(self, '<div><b onclick="e()">x</b></div>',
                                 "<div><b>x</b></div>")
        # And the escaped form of the same payload must pass.
        assert_no_new_markup(self, "<div>&lt;img src=x onerror=alert(1)&gt;</div>",
                             "<div>note</div>")

    def test_a_hostile_note_reaches_the_client_escaped(self):
        for bad in self.ANGLED:
            html = self._mail(bad)
            assert_inert(self, html, bad)
            self.assertIn("&lt;", html, bad)      # escaped, not silently dropped

    def test_a_hostile_note_is_shown_rather_than_dropped(self):
        # Escaped, not deleted: the recipient should see what was sent to them.
        html = self._mail('<img src=x onerror=alert(1)>')
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_note_markdown_is_not_interpreted(self):
        # render() is deliberately not used on the note, so a recipient's client
        # is never asked to interpret markup one user typed for another.
        _, html, _ = mail.build(make_job(), shared_by=SHARER,
                                note="**bold** and `code` and | a | table |")
        self.assertIn("**bold**", html)
        self.assertIn("`code`", html)
        self.assertNotIn("<strong>bold</strong>", html)

    def test_a_markdown_link_in_a_note_never_becomes_an_href(self):
        # Asserted directly as well as structurally: because the note is not
        # rendered, "javascript:" stays literal text, which contains the needle
        # and is nonetheless inert -- so assert_inert would report a false
        # positive here. The claim worth making is that no attribute was built
        # out of it, and "no href anywhere" would be wrong since the mail does
        # carry real links.
        for bad in self.UNANGLED:
            _, html, _ = mail.build(make_job(), shared_by=SHARER, note=bad)
            self.assertIn(bad, html, bad)                  # shown as text
            self.assertNotRegex(html, r'href="[^"]*(javascript|data):', bad)
            self.assertNotIn('<a href="javascript', html, bad)
            self.assertNotIn('style="display:none"', html, bad)

    def test_quotes_and_ampersands_in_a_note_cannot_break_an_attribute(self):
        note = '" style="display:none" x="  &  <  >'
        _, html, _ = mail.build(make_job(), shared_by=SHARER, note=note)
        assert_inert(self, html)
        assert_no_new_markup(self, html, self._mail(self.BENIGN), note)
        self.assertIn("&quot;", html)
        self.assertIn("&amp;", html)
        self.assertNotIn('style="display:none"', html)

    def test_a_hostile_sharer_address_is_escaped_everywhere_it_appears(self):
        for nasty in ('alice@example.com <script>alert(1)</script>',
                      'alice@example.com <b onclick="evil()">x</b>'):
            subject, html, text = mail.build(make_job(), shared_by=nasty)
            assert_inert(self, html, nasty)
            assert_no_new_markup(self, html, self._mail(self.BENIGN), nasty)
            self.assertNotIn("<script", subject)
            self.assertNotIn("<script", text)

    def test_a_sharer_address_cannot_break_out_of_an_attribute(self):
        # No angle brackets, so nothing is escaped *away* and the substring scan
        # would flag the inert text -- the same false positive as UNANGLED. What
        # matters is that the quote was escaped, so the run of text stays inside
        # the value it was placed in and never becomes an attribute of its own.
        nasty = 'alice@example.com" onmouseover="evil()'
        _, html, _ = mail.build(make_job(), shared_by=nasty)
        assert_no_new_markup(self, html, self._mail(self.BENIGN), nasty)
        self.assertIn("&quot; onmouseover=&quot;evil()", html)
        self.assertNotIn('" onmouseover="evil()', html)

    def test_a_note_cannot_inject_a_newline_into_the_subject(self):
        # The subject does not carry the note, but _plain collapses whitespace
        # for exactly this class of mistake; assert the invariant.
        subject, _, _ = mail.build(make_job(),
                                   shared_by="a@x.test\nBcc: evil@x.test",
                                   note="x\ny")
        self.assertNotIn("\n", subject)
        self.assertNotIn("\r", subject)

    def test_the_whole_shared_mail_is_inert_with_a_hostile_report_too(self):
        nasty = ('# T\n\n<script>alert(1)</script>\n\n'
                 '<img src=x onerror=alert(1)>\n\n### Portfolio Manager\n\nBUY\n')
        _, html, _ = mail.build(make_job(report=nasty, ticker='<b>X</b>'),
                                shared_by=SHARER,
                                note='<img src=x onerror=alert(1)>')
        assert_inert(self, html)


class ShareStringTests(unittest.TestCase):
    """Localisation parity for the five strings sharing added.

    ``_t()`` falls back to English for a missing key, which is right at runtime
    and exactly why this needs a test: a share mailed in Chinese with one English
    sentence in it would ship silently.
    """

    KEYS = ("share_subject", "share_by", "share_note", "share_open", "share_why")

    def test_every_share_string_exists_in_every_language(self):
        for code, table in mail._STR.items():
            for key in self.KEYS:
                self.assertIn(key, table, f"{code} is missing {key}")
                self.assertTrue(str(table[key]).strip(), f"{code}.{key} is empty")

    def test_the_share_key_sets_agree(self):
        en = {k for k in mail._STR["en"] if k.startswith("share_")}
        self.assertEqual(en, set(self.KEYS))
        for code, table in mail._STR.items():
            self.assertEqual({k for k in table if k.startswith("share_")}, en,
                             f"{code} share keys differ from en")

    def test_the_share_strings_are_actually_translated(self):
        same = [k for k in self.KEYS
                if mail._STR["zh"][k] == mail._STR["en"][k]]
        self.assertEqual(same, [], f"untranslated zh keys: {same}")

    def test_share_placeholders_match_across_languages(self):
        holes = re.compile(r"\{(\w+)\}")
        for key in self.KEYS:
            for code, table in mail._STR.items():
                self.assertEqual(sorted(holes.findall(mail._STR["en"][key])),
                                 sorted(holes.findall(table[key])),
                                 f"{code}.{key} placeholders differ from en")

    def test_share_why_carries_the_expiry_placeholder(self):
        # The number comes from share.TTL_DAYS; a language that dropped {days}
        # would promise a window nothing enforces.
        for code, table in mail._STR.items():
            self.assertIn("{days}", table["share_why"], code)
            self.assertIn("{sharer}", table["share_why"], code)
            self.assertIn("{brand}", table["share_why"], code)

    def test_share_why_replaces_the_unshared_why(self):
        for code, table in mail._STR.items():
            self.assertNotEqual(table["share_why"], table["why"], code)

    def test_an_unknown_language_shares_in_english(self):
        for code in ("fr", "", None, "EN", "zh-TW"):
            _, html, _ = mail.build(make_job(lang=code), shared_by=SHARER)
            self.assertIn("shared this report with you", html, repr(code))


class SendShareTests(unittest.TestCase):
    """send_share() with SES stubbed at the boto3 boundary.

    The property worth pinning is the one the docstring argues for: a share is
    deliberately *not* routed through notify()/_claim(), because sharing the same
    report with a second colleague -- or resending after a typo -- is legitimate,
    and a send-once marker keyed on the job would swallow it.
    """

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["SES_FROM_EMAIL"] = "from@x.test"
        self.sent: list[dict] = []
        self.fail_next = False
        outer = self

        class _Client:
            def send_email(self, **kw):
                if outer.fail_next:
                    raise RuntimeError("SES refused")
                outer.sent.append(kw)

        self._boto = types.ModuleType("boto3")
        self._boto.client = lambda *a, **k: _Client()
        self._saved_boto = sys.modules.get("boto3")
        sys.modules["boto3"] = self._boto

    def tearDown(self):
        if self._saved_boto is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = self._saved_boto
        os.environ.clear()
        os.environ.update(self._env)

    URL = "https://trade-agents.com/agents/shared/" + "t" * 22

    def test_it_goes_to_the_recipient_not_the_owner(self):
        self.assertTrue(mail.send_share(make_job(), FRIEND, SHARER, "hi", self.URL))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["Destination"]["ToAddresses"], [FRIEND])
        self.assertEqual(msg["Source"], "from@x.test")
        self.assertNotIn(OWNER, msg["Message"]["Body"]["Html"]["Data"])

    def test_the_message_is_the_shared_shape(self):
        mail.send_share(make_job(), FRIEND, SHARER, "have a look", self.URL)
        msg = self.sent[0]
        self.assertIn(SHARER, msg["Message"]["Subject"]["Data"])
        html = msg["Message"]["Body"]["Html"]["Data"]
        self.assertIn("have a look", html)
        self.assertIn(self.URL, html)
        self.assertIn("Text", msg["Message"]["Body"])

    def test_the_same_report_may_be_shared_more_than_once(self):
        job = make_job()
        for addr in (FRIEND, "carol@example.org", FRIEND):
            self.assertTrue(mail.send_share(job, addr, SHARER, "", self.URL))
        self.assertEqual(len(self.sent), 3)

    def test_a_reportless_run_is_not_shared(self):
        self.assertFalse(mail.send_share(make_job(report=""), FRIEND, SHARER,
                                         "", self.URL))
        self.assertEqual(self.sent, [])

    def test_a_refused_send_returns_false_rather_than_raising(self):
        self.fail_next = True
        self.assertFalse(mail.send_share(make_job(), FRIEND, SHARER, "", self.URL))
        self.assertEqual(self.sent, [])

    def test_no_from_address_means_no_send(self):
        os.environ.pop("SES_FROM_EMAIL", None)
        self.assertFalse(mail.send_share(make_job(), FRIEND, SHARER, "", self.URL))
        self.assertEqual(self.sent, [])

    def test_no_recipient_means_no_send(self):
        for bad in ("", None):
            self.assertFalse(mail.send_share(make_job(), bad, SHARER, "",
                                             self.URL), repr(bad))
        self.assertEqual(self.sent, [])


# ── The daily share counter ─────────────────────────────────────────────────

def _snapshot(d: pathlib.Path) -> set:
    if not d.exists():
        return set()
    return {(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in d.iterdir()}


class ShareQuotaTests(unittest.TestCase):
    """try_consume_share(), with the counter redirected to a temp directory.

    ``_LOCK_PATH`` is computed at import time from ``QUOTA_DIR``, so pointing the
    directory alone would leave every test flock-ing the real ``cache/agents``;
    both are patched, and ``test_nothing_is_written_outside_the_temp_dir`` is
    what proves it rather than assuming it.

    What the counter has to get right is independence: a share costs sending
    reputation, not model calls, so spending one must not reduce the allowance
    for producing a report.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._real_dir = quota.QUOTA_DIR
        self._real_lock = quota._LOCK_PATH
        self._real_before = _snapshot(self._real_dir)
        quota.QUOTA_DIR = self.dir
        quota._LOCK_PATH = self.dir / "quota.lock"
        self._env = dict(os.environ)
        # credits is only reachable from try_consume(); stubbed so the run
        # counter can be exercised without a DynamoDB ledger.
        self._saved_credits = sys.modules.get("ystocker.credits")
        sys.modules["ystocker.credits"] = types.SimpleNamespace(
            PAY_URL="https://pay.test", balance=lambda e: 0,
            spend=lambda e, n=1: False, refund=lambda e, n=1: None)

    def tearDown(self):
        quota.QUOTA_DIR = self._real_dir
        quota._LOCK_PATH = self._real_lock
        if self._saved_credits is None:
            sys.modules.pop("ystocker.credits", None)
        else:
            sys.modules["ystocker.credits"] = self._saved_credits
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()
        self.assertEqual(_snapshot(self._real_dir), self._real_before,
                         "a test wrote into the real cache/agents")

    def _counter(self) -> dict:
        return json.loads((self.dir / f"quota-{quota.today()}.json")
                          .read_text(encoding="utf-8"))

    # -- the redirection itself --------------------------------------------
    def test_nothing_is_written_outside_the_temp_dir(self):
        ok, _ = quota.try_consume_share("a@x.test")
        self.assertTrue(ok)
        self.assertTrue(quota._path(quota.today()).is_file())
        self.assertEqual(quota._path(quota.today()).parent, self.dir)
        self.assertEqual(quota._LOCK_PATH.parent, self.dir)
        self.assertTrue((self.dir / "quota.lock").exists(),
                        "the flock landed somewhere else")
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertIn(f"quota-{quota.today()}.json", names)

    # -- the limit ---------------------------------------------------------
    def test_the_default_limit_is_twenty(self):
        os.environ.pop("AGENTS_SHARE_DAILY_LIMIT", None)
        self.assertEqual(quota.limit_share(), 20)

    def test_the_limit_is_configurable(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "4"
        self.assertEqual(quota.limit_share(), 4)

    def test_a_nonsense_limit_falls_back_to_the_default(self):
        for bad in ("abc", "-1", "1.5", "  "):
            os.environ["AGENTS_SHARE_DAILY_LIMIT"] = bad
            self.assertEqual(quota.limit_share(), 20, bad)

    def test_the_share_limit_is_its_own_number(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "4"
        os.environ["AGENTS_DAILY_LIMIT"] = "9"
        os.environ["AGENTS_CHAT_DAILY_LIMIT"] = "11"
        self.assertEqual(quota.limit_share(), 4)
        self.assertEqual(quota.limit_default(), 9)
        self.assertEqual(quota.limit_chat(), 11)

    # -- counting ----------------------------------------------------------
    def test_it_increments(self):
        for n in (1, 2, 3):
            ok, info = quota.try_consume_share("a@x.test")
            self.assertTrue(ok)
            self.assertEqual(info["used"], n)
            self.assertEqual(info["limit"], quota.limit_share())
            self.assertEqual(info["remaining"], quota.limit_share() - n)
            self.assertEqual(info["tz"], quota.QUOTA_TZ)
        self.assertEqual(self._counter()["share"], {"a@x.test": 3})

    def test_it_stops_at_the_limit(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "3"
        for _ in range(3):
            self.assertTrue(quota.try_consume_share("a@x.test")[0])
        for _ in range(4):
            ok, info = quota.try_consume_share("a@x.test")
            self.assertFalse(ok)
            self.assertEqual(info["used"], 3)
            self.assertEqual(info["remaining"], 0)
        # A refused share is not counted, so the number cannot run away.
        self.assertEqual(self._counter()["share"], {"a@x.test": 3})

    def test_counters_are_per_user(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "2"
        self.assertTrue(quota.try_consume_share("a@x.test")[0])
        self.assertTrue(quota.try_consume_share("a@x.test")[0])
        self.assertFalse(quota.try_consume_share("a@x.test")[0])
        self.assertTrue(quota.try_consume_share("b@x.test")[0])

    def test_the_address_is_case_folded_to_one_counter(self):
        quota.try_consume_share("A@X.test")
        ok, info = quota.try_consume_share("  a@x.TEST  ")
        self.assertTrue(ok)
        self.assertEqual(info["used"], 2)
        self.assertEqual(self._counter()["share"], {"a@x.test": 2})

    def test_an_anonymous_caller_gets_nothing(self):
        for bad in ("", None, "   "):
            ok, info = quota.try_consume_share(bad)
            self.assertFalse(ok, repr(bad))
            self.assertEqual(info["used"], 0)
            self.assertEqual(info["remaining"], 0)
        self.assertFalse(list(self.dir.glob("quota-*.json")),
                         "an anonymous attempt wrote a counter")

    # -- independence ------------------------------------------------------
    def test_spending_shares_does_not_touch_the_run_or_chat_counters(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "5"
        for _ in range(5):
            quota.try_consume_share("a@x.test")
        data = self._counter()
        self.assertEqual(data["share"], {"a@x.test": 5})
        self.assertEqual(data["users"], {}, "shares ate the run allowance")
        self.assertEqual(data.get("chat", {}), {})
        self.assertEqual(data.get("total", 0), 0,
                         "shares counted against the global run ceiling")

    def test_a_user_at_their_share_limit_can_still_run_an_analysis(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "2"
        self.assertTrue(quota.try_consume_share("a@x.test")[0])
        self.assertTrue(quota.try_consume_share("a@x.test")[0])
        self.assertFalse(quota.try_consume_share("a@x.test")[0])
        ok, reason, info = quota.try_consume("a@x.test")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(info["used"], 1)
        self.assertEqual(info["remaining"], quota.limit_default() - 1)

    def test_a_user_out_of_runs_can_still_share(self):
        os.environ["AGENTS_DAILY_LIMIT"] = "1"
        self.assertTrue(quota.try_consume("a@x.test")[0])
        self.assertFalse(quota.try_consume("a@x.test")[0])
        ok, info = quota.try_consume_share("a@x.test")
        self.assertTrue(ok)
        self.assertEqual(info["used"], 1)

    def test_shares_and_chats_are_separate_ledgers(self):
        quota.try_consume_share("a@x.test")
        quota.try_consume_chat("a@x.test")
        quota.try_consume_chat("a@x.test")
        data = self._counter()
        self.assertEqual(data["share"], {"a@x.test": 1})
        self.assertEqual(data["chat"], {"a@x.test": 2})

    def test_usage_does_not_report_shares_as_runs(self):
        os.environ["AGENTS_SHARE_DAILY_LIMIT"] = "5"
        for _ in range(4):
            quota.try_consume_share("a@x.test")
        u = quota.usage("a@x.test")
        self.assertEqual(u["used"], 0)
        self.assertEqual(u["remaining"], quota.limit_default())
        self.assertEqual(u["global_used"], 0)

    def test_a_corrupt_counter_file_does_not_lose_the_share_gate(self):
        (self.dir / f"quota-{quota.today()}.json").write_text("{not json",
                                                             encoding="utf-8")
        ok, info = quota.try_consume_share("a@x.test")
        self.assertTrue(ok)
        self.assertEqual(info["used"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
