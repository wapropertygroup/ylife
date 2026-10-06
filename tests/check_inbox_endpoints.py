"""
End-to-end check of /api/posts and /posts, through Flask's test client.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app (which
starts background threads). No network and no AWS — the table is swapped for an
in-memory stand-in, because what is being checked here is the *route*: the order
of its guards, what it returns, and who may read it. Since 2026-10-06 that
includes whose feed a post lands in, so the inbox module's own key logic runs
against the stand-in rather than being stubbed out.

matplotlib is stubbed before ``ystocker.routes`` is imported, for the broken
Homebrew pyexpat this repo's dev checkout has (see ``check_dca_endpoints.py``).

Run:  venv/bin/python -m tests.check_inbox_endpoints
"""
from __future__ import annotations

import json
import os
import sys
import types
import unittest


class _Any:
    def __getattr__(self, _name): return _Any()
    def __call__(self, *_a, **_k): return _Any()
    def __getitem__(self, _k): return _Any()
    def __setitem__(self, _k, _v): return None
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False
    def update(self, *_a, **_k): return None


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-inbox-secret")
os.environ["INBOX_TOKEN"] = "test-token-value"
# No AWS and no SSM: create_app() starts the background threads -- the markets
# warm-up, the observed-series writers -- and this laptop's credentials reach
# production DynamoDB, so a check must not hand them over. Threads are held only
# while the app is built (_build_app), so one a test starts itself still runs.
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

from ystocker import create_app, inbox, quota                       # noqa: E402

TOKEN = "test-token-value"
SITE_OWNER = "owner@example.com"
os.environ["INBOX_OWNER"] = SITE_OWNER
def _build_app():
    import threading
    import ystocker
    ystocker._load_secrets_from_ssm = lambda *a, **k: None
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return create_app()
    finally:
        threading.Thread.start = real_start



class _MemTable:
    """Stands in for DynamoDB, keyed (bucket, sk). Also lets a test make the
    table fail on demand, which is the branch that matters most: a POST accepted
    and dropped is worse than one refused, and a GET that answers "no messages"
    when it means "cannot reach the table" is the one wrong answer on this page."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise RuntimeError("stubbed outage")

    def put_item(self, Item):
        self._check()
        self.items[(Item["bucket"], Item["sk"])] = dict(Item)

    def get_item(self, Key, ConsistentRead=False):
        self._check()
        item = self.items.get((Key["bucket"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self._check()
        self.items.pop((Key["bucket"], Key["sk"]), None)
        return {}

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None):
        self._check()
        name, value = KeyConditionExpression.get_expression()["values"]
        hits = sorted((i for i in self.items.values() if i.get(name.name) == value),
                      key=lambda i: i["sk"], reverse=not ScanIndexForward)
        return {"Items": [dict(i) for i in hits[:Limit]]}

    @property
    def rows(self) -> list[dict]:
        """The stored posts, oldest first, without the token rows."""
        posts = [i for (b, _), i in self.items.items()
                 if b not in (inbox.TOKEN_PARTITION, inbox.OWNER_PARTITION)]
        return [inbox._thaw(i) | {"owner": i.get("owner")}
                for i in sorted(posts, key=lambda i: i["sk"])]


class InboxEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        import tempfile

        self.store = _MemTable()
        self._table = inbox._table
        inbox._table = self.store
        # The daily ceilings live in quota.py's counter file, which is this
        # checkout's cache otherwise.
        self._quota_dir, self._lock = quota.QUOTA_DIR, quota._LOCK_PATH
        self._tmp = tempfile.TemporaryDirectory()
        quota.QUOTA_DIR = quota.Path(self._tmp.name)
        quota._LOCK_PATH = quota.QUOTA_DIR / "quota.lock"
        os.environ["INBOX_TOKEN"] = TOKEN
        # The test client keeps its cookie jar across methods, so a test that
        # signs in leaves every later one signed in — which would make the two
        # signed-out assertions below pass for the wrong reason, in the
        # direction that hides a missing gate. Cleared per test.
        with self.client.session_transaction() as sess:
            sess.clear()

    def tearDown(self):
        inbox._table = self._table
        quota.QUOTA_DIR, quota._LOCK_PATH = self._quota_dir, self._lock
        self._tmp.cleanup()
        os.environ["INBOX_TOKEN"] = TOKEN
        os.environ.pop("INBOX_USER_DAILY_LIMIT", None)

    def sign_in(self, email):
        with self.client.session_transaction() as sess:
            sess["user_email"] = email

    def post(self, body, token=TOKEN, **kw):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/api/posts",
                                data=json.dumps(body) if not isinstance(body, (bytes, str))
                                else body,
                                headers=headers, **kw)

    # ── the guards, in order ──────────────────────────────────────────────
    def test_a_good_message_is_stored(self):
        r = self.post({"title": "Hi", "source": "cron"})
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["id"])
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.rows[0]["title"], "Hi")

    def test_no_token_is_rejected(self):
        r = self.post({"title": "Hi"}, token=None)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["reason"], "unauthorized")
        self.assertEqual(self.store.rows, [])

    def test_a_wrong_token_is_rejected(self):
        r = self.post({"title": "Hi"}, token="not-the-token")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store.rows, [])

    def test_an_unconfigured_token_shuts_the_door(self):
        """The whole security posture in one assertion. "No token set" must
        store nothing, never fall through to accepting: an unset INBOX_TOKEN
        matches no token, so even its old value is refused."""
        os.environ.pop("INBOX_TOKEN", None)
        for presented in ("anything", TOKEN):
            r = self.post({"title": "Hi"}, token=presented)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.get_json()["reason"], "unauthorized")
        self.assertEqual(self.store.rows, [])

    def test_the_token_is_checked_before_the_body_is_parsed(self):
        """An unauthenticated caller must not be able to make us do work. A
        body that would fail validation still returns 401, not 400 — the wrong
        order here is how an open endpoint becomes a parser to attack."""
        r = self.post(b"{ not json at all", token=None)
        self.assertEqual(r.status_code, 401)

    def test_an_oversized_body_is_refused(self):
        big = json.dumps({"title": "x", "text": "y" * (inbox.MAX_BODY_BYTES + 500)})
        r = self.post(big.encode())
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.get_json()["reason"], "too_large")
        self.assertEqual(self.store.rows, [])

    def test_bad_json_is_named_not_generic(self):
        """The caller is a script, so the reason has to be machine-readable."""
        r = self.post(b"{ nope")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "bad_json")

    def test_an_empty_message_is_refused_with_its_reason(self):
        r = self.post({"source": "cron"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "empty")

    def test_a_dangerous_url_is_refused(self):
        r = self.post({"title": "x", "url": "javascript:alert(1)"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "bad_url")

    def test_a_store_failure_is_a_503_not_a_200(self):
        """A POST accepted and dropped leaves the sender with no way to know and
        no reason to retry."""
        self.store.fail = True
        r = self.post({"title": "Hi"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "store_unavailable")

    def test_the_old_names_still_work(self):
        """`/api/inbox` shipped first and a sender may already be pointed at it.
        Breaking a hand-configured script to tidy a URL costs a silent outage to
        save nothing, so the alias is asserted rather than assumed. The *page*
        redirects instead — a bookmark should move itself rather than leave two
        addresses serving one thing."""
        r = self.post({"title": "via the old name"}, )
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/inbox",
                             data=json.dumps({"title": "alias"}),
                             headers={"Content-Type": "application/json",
                                      "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 201)
        moved = self.client.get("/inbox")
        self.assertEqual(moved.status_code, 302)
        self.assertTrue(moved.headers["Location"].endswith("/posts"))

    # ── raw email: the receiver does the MIME work ────────────────────────
    #
    # A `cid:` reference points at a part of *this message*, so it can only be
    # resolved where the message is. A sender that pre-extracts the HTML has
    # already thrown the picture away — which is what happened to a real digest,
    # whose banner arrived as `cid:digest-header` with the bytes nowhere on the
    # box. Posting the raw message moves that work here.
    def _digest(self):
        import base64
        from email.message import EmailMessage

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk"
            "YPhfz0BhYGBgYGAAAEeoAxWZk1WhAAAAAElFTkSuQmCC")
        msg = EmailMessage()
        msg["Subject"] = "NYT + WSJ 新闻摘要"
        msg.set_content("fallback")
        msg.add_alternative(
            '<html><body><img src="cid:digest-header" alt="banner">'
            '<h2>头条</h2></body></html>', subtype="html")
        part = msg.get_payload()[1]
        part.make_related()
        part.add_related(png, maintype="image", subtype="png", cid="<digest-header>")
        return msg.as_bytes()

    def test_a_raw_email_resolves_its_inline_images(self):
        r = self.client.post("/api/posts", data=self._digest(),
                             headers={"Content-Type": "message/rfc822",
                                      "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 201)
        stored = self.store.rows[-1]
        self.assertEqual(stored["title"], "NYT + WSJ 新闻摘要")
        self.assertEqual(stored["source"], "email")
        self.assertIn("data:image/png;base64,", stored["text"])
        self.assertNotIn("cid:digest-header", stored["text"])
        self.assertEqual(stored["data"]["inline"]["rewritten"], 1)

    def test_the_query_string_supplies_what_the_raw_body_cannot(self):
        """Without this every forwarded message is indistinguishable from every
        other: one source, no level, no tags."""
        r = self.client.post("/api/posts?source=muse&level=warn&tags=digest,news",
                             data=self._digest(),
                             headers={"Content-Type": "message/rfc822",
                                      "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 201)
        stored = self.store.rows[-1]
        self.assertEqual(stored["source"], "muse")
        self.assertEqual(stored["level"], "warn")
        self.assertEqual(stored["tags"], ["digest", "news"])

    def test_a_raw_post_is_still_token_gated(self):
        r = self.client.post("/api/posts", data=self._digest(),
                             headers={"Content-Type": "message/rfc822"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store.rows, [])

    def test_a_raw_email_gets_its_own_size_ceiling(self):
        """An email carrying a banner is megabytes before anything is extracted,
        so the JSON ceiling would refuse every real message."""
        self.assertGreater(inbox.MAX_RAW_BYTES, inbox.MAX_BODY_BYTES)
        r = self.client.post("/api/posts", data=b"x" * (inbox.MAX_RAW_BYTES + 10),
                             headers={"Content-Type": "message/rfc822",
                                      "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.get_json()["max_bytes"], inbox.MAX_RAW_BYTES)

    def test_malformed_mail_never_500s_and_never_vanishes(self):
        """Python's email parser is deliberately lenient: bytes with no headers
        become a body rather than an error. So garbage is *stored* rather than
        refused, and that is the better outcome — a forwarder that starts sending
        rubbish is visible on the page, where a 400 it does not log would not be.

        The invariant is therefore not "it is refused" but "it is never a 500 and
        never silently dropped": either it lands, or it comes back with a named
        reason."""
        r = self.client.post("/api/posts", data=b"\xff\xfe not a message at all",
                             headers={"Content-Type": "message/rfc822",
                                      "Authorization": f"Bearer {TOKEN}"})
        self.assertLess(r.status_code, 500)
        if r.status_code == 201:
            self.assertTrue(self.store.rows, "accepted but not stored")
        else:
            self.assertIn(r.get_json()["reason"],
                          ("empty", "no_body", "bad_email"))

    def test_get_is_not_a_way_to_write(self):
        self.assertEqual(self.client.put("/api/posts").status_code, 405)
        self.assertEqual(self.client.delete("/api/posts").status_code, 405)

    # ── reads are gated even though writes are authenticated ──────────────
    def test_reading_signed_out_is_refused(self):
        """This is what decides whether a leaked token is a nuisance or a
        publishing channel onto a public domain."""
        r = self.client.get("/api/posts")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["reason"], "signed_out")

    def test_reading_signed_in_returns_the_readers_feed(self):
        self.post({"title": "One", "source": "cron"})
        self.post({"title": "Two", "source": "bot"})
        self.sign_in(SITE_OWNER)
        body = self.client.get("/api/posts").get_json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["messages"][0]["title"], "Two")   # newest first
        self.assertEqual(body["sources"], ["bot", "cron"])
        self.assertTrue(body["site_token"])

    # ── per person: a post is its token holder's, and only theirs to read ──
    def test_the_site_token_posts_into_the_site_owners_feed_alone(self):
        """Until 2026-10-06 every signed-in reader saw every post, the owner's
        forwarded mail included."""
        self.post({"title": "the owner's mail"})
        self.assertEqual(self.store.rows[0]["owner"], SITE_OWNER)
        self.sign_in("someone@example.com")
        body = self.client.get("/api/posts").get_json()
        self.assertEqual(body["count"], 0)
        self.assertFalse(body["site_token"])

    def test_a_personal_token_posts_into_its_holders_feed(self):
        self.sign_in("alice@example.com")
        made = self.client.post("/api/posts/token")
        self.assertEqual(made.status_code, 201)
        token = made.get_json()["token"]
        self.assertEqual(self.post({"title": "for alice"}, token=token).status_code, 201)
        self.assertEqual(self.store.rows[-1]["owner"], "alice@example.com")
        self.assertEqual([m["title"] for m in self.client.get("/api/posts").get_json()["messages"]],
                         ["for alice"])
        self.sign_in("bob@example.com")
        self.assertEqual(self.client.get("/api/posts").get_json()["count"], 0)
        self.sign_in(SITE_OWNER)
        self.assertEqual(self.client.get("/api/posts").get_json()["count"], 0)

    def test_the_token_is_shown_once_and_never_again(self):
        self.sign_in("alice@example.com")
        token = self.client.post("/api/posts/token").get_json()["token"]
        info = self.client.get("/api/posts/token").get_json()
        self.assertNotIn("token", info)
        self.assertNotIn(token, json.dumps(info))
        self.assertEqual(info["info"]["hint"], token[-4:])

    def test_a_replaced_or_revoked_token_stops_posting(self):
        self.sign_in("alice@example.com")
        first = self.client.post("/api/posts/token").get_json()["token"]
        second = self.client.post("/api/posts/token").get_json()["token"]
        self.assertEqual(self.post({"title": "x"}, token=first).status_code, 401)
        self.assertEqual(self.post({"title": "x"}, token=second).status_code, 201)
        revoked = self.client.delete("/api/posts/token")
        self.assertTrue(revoked.get_json()["revoked"])
        self.assertEqual(self.post({"title": "x"}, token=second).status_code, 401)
        self.assertIsNone(self.client.get("/api/posts/token").get_json()["info"])

    def test_the_token_endpoints_are_signed_in_only(self):
        for method in ("get", "post", "delete"):
            r = getattr(self.client, method)("/api/posts/token")
            self.assertEqual(r.status_code, 401, method)
            self.assertEqual(r.get_json()["reason"], "signed_out")
        self.assertEqual(self.store.items, {})

    def test_a_token_lookup_outage_is_a_503_not_a_401(self):
        """401 would tell a script its token was revoked."""
        self.store.fail = True
        r = self.post({"title": "x"}, token="a-personal-token")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "store_unavailable")

    def test_one_persons_ceiling_is_not_everybodys(self):
        os.environ["INBOX_USER_DAILY_LIMIT"] = "1"
        self.sign_in("alice@example.com")
        alice = self.client.post("/api/posts/token").get_json()["token"]
        self.assertEqual(self.post({"title": "1"}, token=alice).status_code, 201)
        r = self.post({"title": "2"}, token=alice)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["reason"], "rate_limited")
        self.assertEqual(self.post({"title": "site"}).status_code, 201)

    def test_a_read_failure_is_a_503_not_an_empty_list(self):
        """"No messages" is the one wrong answer on the page whose job is to
        show them — a reader cannot tell it from "nothing was sent"."""
        self.sign_in("someone@example.com")
        self.store.fail = True
        r = self.client.get("/api/posts")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "store_unavailable")

    # ── the page ──────────────────────────────────────────────────────────
    def test_the_page_renders_signed_out_without_the_feed_script(self):
        html = self.client.get("/posts").data.decode()
        self.assertEqual(self.client.get("/posts").status_code, 200)
        self.assertIn("inbox.signin_title", html)
        self.assertNotIn("loadInbox()", html)

    def test_the_page_renders_the_feed_when_signed_in(self):
        self.sign_in("someone@example.com")
        html = self.client.get("/posts").data.decode()
        self.assertIn("loadInbox()", html)
        self.assertIn('id="ibList"', html)
        # The reader's own token, managed on the page.
        self.assertIn('id="ibToken"', html)
        self.assertIn("loadToken()", html)
        self.assertIn("<YOUR_TOKEN>", html)
        self.assertNotIn("<INBOX_TOKEN>", html)

    def test_the_page_escapes_everything_it_renders(self):
        """Every field here came from a token holder, not a signed-in human, so
        the renderer must escape — asserted on the source rather than on output,
        since the list is composed client-side. Signed in, because the script
        block only renders for a reader who can see the feed."""
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        html = self.client.get("/posts").data.decode()
        self.assertIn("function esc(", html)
        # The two places escaping alone would not be enough.
        self.assertIn("encodeURIComponent(m.ticker)", html)
        self.assertIn('rel="noopener noreferrer"', html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
