"""
End-to-end check of /api/inbox and /inbox, through Flask's test client.

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
        return self.client.post("/api/inbox",
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

    def test_get_is_not_a_way_to_write(self):
        self.assertEqual(self.client.put("/api/inbox").status_code, 405)
        self.assertEqual(self.client.delete("/api/inbox").status_code, 405)

    # ── reads are gated even though writes are authenticated ──────────────
    def test_reading_signed_out_is_refused(self):
        """This is what decides whether a leaked token is a nuisance or a
        publishing channel onto a public domain."""
        r = self.client.get("/api/inbox")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["reason"], "signed_out")

    def test_reading_signed_in_returns_the_feed(self):
        self.post({"title": "One", "source": "cron"})
        self.post({"title": "Two", "source": "bot"})
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        body = self.client.get("/api/inbox").get_json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["messages"][0]["title"], "Two")   # newest first
        self.assertEqual(body["sources"], ["bot", "cron"])

    def test_a_read_failure_is_a_503_not_an_empty_list(self):
        """"No messages" is the one wrong answer on the page whose job is to
        show them — a reader cannot tell it from "nothing was sent"."""
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        self.store.fail = True
        r = self.client.get("/api/inbox")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "store_unavailable")

    # ── the page ──────────────────────────────────────────────────────────
    def test_the_page_renders_signed_out_without_the_feed_script(self):
        html = self.client.get("/inbox").data.decode()
        self.assertEqual(self.client.get("/inbox").status_code, 200)
        self.assertIn("inbox.signin_title", html)
        self.assertNotIn("loadInbox()", html)

    def test_the_page_renders_the_feed_when_signed_in(self):
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        html = self.client.get("/inbox").data.decode()
        self.assertIn("loadInbox()", html)
        self.assertIn('id="ibList"', html)

    def test_the_page_escapes_everything_it_renders(self):
        """Every field here came from a token holder, not a signed-in human, so
        the renderer must escape — asserted on the source rather than on output,
        since the list is composed client-side. Signed in, because the script
        block only renders for a reader who can see the feed."""
        with self.client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
        html = self.client.get("/inbox").data.decode()
        self.assertIn("function esc(", html)
        # The two places escaping alone would not be enough.
        self.assertIn("encodeURIComponent(m.ticker)", html)
        self.assertIn('rel="noopener noreferrer"', html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
