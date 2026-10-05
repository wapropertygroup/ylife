"""
End-to-end check of the sign-up mail's hook in ``/api/auth/google``, through
Flask's test client.

ystocker.signups is tested on its own in ``tests/test_signups.py``; this pins
the seam between it and the route:
* a verified sign-in hands signups the address, the name, the site it was made
  on, the page it was made from (a same-site Referer, decoded) and the language
  the sign-in page posted;
* a token that fails verification never reaches signups;
* the sign-in page posts that language;
* ``create_app`` starts the seed thread.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Built hermetically, as ``check_dca_embed`` is -- no background thread, no
secret, no AWS, no network. Google's verification and signups are stubs.

Run:  venv/bin/python -m tests.check_signup_route
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock


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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-signup-route-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS", "SES_FROM_EMAIL"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
os.environ["GOOGLE_CLIENT_ID"] = "check-client-id.apps.googleusercontent.com"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import signups                              # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

IDINFO = {"email": "new@example.com", "name": "Alice Example",
          "picture": "https://example.com/a.png"}


def _build_app():
    started = []
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: started.append(self.name)
    try:
        return ystocker.create_app(), started
    finally:
        threading.Thread.start = real_start


class SignupRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.threads = _build_app()
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        self.calls = []
        self.enterContext(mock.patch.object(
            signups, "record_sign_in",
            lambda *a, **k: self.calls.append((a, k))))

    def _sign_in(self, verify, **headers):
        with mock.patch("google.oauth2.id_token.verify_oauth2_token", verify):
            return self.client.post(
                "/api/auth/google", base_url="https://trade-agents.com",
                json={"credential": "token", "lang": "zh-CN"}, headers=headers)

    def test_a_verified_sign_in_reaches_signups_with_where_it_came_from(self):
        resp = self._sign_in(lambda *a, **k: dict(IDINFO),
                             Referer="https://trade-agents.com/login?next=%2Fagents")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(self.calls, [(("new@example.com", "Alice Example"), {
            "host": "trade-agents.com", "page": "/login?next=/agents", "lang": "zh-CN"})])
        with self.client.session_transaction(base_url="https://trade-agents.com") as sess:
            self.assertEqual(sess["user_email"], "new@example.com")

    def test_another_sites_referer_is_not_recorded(self):
        self._sign_in(lambda *a, **k: dict(IDINFO), Referer="https://evil.example/x")
        self.assertEqual(self.calls[0][1]["page"], "")

    def test_a_token_that_fails_verification_never_reaches_signups(self):
        def bad(*_a, **_k):
            raise ValueError("Token expired")
        resp = self._sign_in(bad)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.calls, [])

    def test_the_sign_in_page_posts_its_language(self):
        html = self.client.get("/login", base_url="https://trade-agents.com").get_data(as_text=True)
        self.assertIn("lang: document.documentElement.lang || ''", html)

    def test_create_app_starts_the_seed(self):
        self.assertIn("signups-seed", self.threads)


if __name__ == "__main__":
    unittest.main()
