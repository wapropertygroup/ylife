"""
End-to-end check of /api/posts and /posts, through Flask's test client.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app (which
starts background threads). No network and no AWS — the store is swapped for an
in-memory stand-in, because what is being checked here is the *route*: the order
of its guards, what it returns, and who may read it.

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

from ystocker import create_app, inbox                              # noqa: E402

TOKEN = "test-token-value"


class _MemStore:
    """Stands in for DynamoDB. Also lets a test make the store fail on demand,
    which is the branch that matters most: a POST accepted and dropped is worse
    than one refused, and a GET that answers "no messages" when it means "cannot
    reach the table" is the one wrong answer on this page."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.fail = False

    def put(self, record):
        if self.fail:
            raise inbox.StoreUnavailable("stubbed")
        self.rows.append(dict(record))
        return dict(record)

    def recent(self, limit=50, **_kw):
        if self.fail:
            raise inbox.StoreUnavailable("stubbed")
        return list(reversed(self.rows))[:limit]


class InboxEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.store = _MemStore()
        self._put, self._recent = inbox.put, inbox.recent
        inbox.put, inbox.recent = self.store.put, self.store.recent
        os.environ["INBOX_TOKEN"] = TOKEN
        # The test client keeps its cookie jar across methods, so a test that
        # signs in leaves every later one signed in — which would make the two
        # signed-out assertions below pass for the wrong reason, in the
        # direction that hides a missing gate. Cleared per test.
        with self.client.session_transaction() as sess:
            sess.clear()

    def tearDown(self):
        inbox.put, inbox.recent = self._put, self._recent
        os.environ["INBOX_TOKEN"] = TOKEN

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
        return 503 and store nothing, never fall through to accepting."""
        os.environ.pop("INBOX_TOKEN", None)
        r = self.post({"title": "Hi"}, token="anything")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "not_configured")
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

    def test_reading_signed_in_returns_the_feed(self):
        self.post({"title": "One", "source": "cron"})
        self.post({"title": "Two", "source": "bot"})
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        body = self.client.get("/api/posts").get_json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["messages"][0]["title"], "Two")   # newest first
        self.assertEqual(body["sources"], ["bot", "cron"])

    def test_a_read_failure_is_a_503_not_an_empty_list(self):
        """"No messages" is the one wrong answer on the page whose job is to
        show them — a reader cannot tell it from "nothing was sent"."""
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
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
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        html = self.client.get("/posts").data.decode()
        self.assertIn("loadInbox()", html)
        self.assertIn('id="ibList"', html)

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
