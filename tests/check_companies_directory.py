"""
End-to-end check of /companies' full directory through Flask's test client:
``/api/companies/directory`` and the page that reads it.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic, as the other endpoint checks are -- no background thread, no secret,
no AWS, no network. SEC's list is a temporary file holding rows cut from the
real one; the fetch is a stub that counts calls.

What it pins:

* a cached list is served as the same compact bytes to everyone, with an hour's
  browser cache, and without touching SEC;
* a cold box answers 202 and kicks exactly one fetch, so the page can show the
  followed companies while it waits;
* a list two days old (the master's daily refresh has stopped) is still served
  and triggers a refetch, while a merely day-old one does not -- the master's
  hourly check is due to refresh it;
* the page carries the Followed view, the exchange filter and the show-more
  control, and every string it composes exists in both languages.

Run:  venv/bin/python -m tests.check_companies_directory
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-companies-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import directory                            # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_directory import HEAD, _filler, _raw            # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "ystocker" / "static" / "i18n.js"


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class CompaniesDirectory(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "sec_company_tickers_exchange.json"
        self.kick = mock.Mock(return_value=True)
        self.patches = [mock.patch.object(directory, "CACHE_FILE", self.file),
                        mock.patch.object(directory, "kick", self.kick)]
        for p in self.patches:
            p.start()
        directory._mem, directory._mem_mtime = None, 0.0

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        directory._mem, directory._mem_mtime = None, 0.0
        self.tmp.cleanup()

    def _seed(self, age=0.0):
        self.file.write_text(json.dumps(_raw(HEAD + _filler(1200))))
        if age:
            t = time.time() - age
            os.utime(self.file, (t, t))

    def test_a_cached_list_is_served_compact_and_cacheable(self):
        self._seed()
        resp = self.client.get("/api/companies/directory")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("public, max-age=3600", resp.headers.get("Cache-Control", ""))
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["count"], 1210)
        self.assertEqual(body["companies"][2], ["GOOGL", "Alphabet Inc.", "Nasdaq", "GOOG"])
        self.kick.assert_not_called()
        # Same bytes again: built once per load, not per request.
        self.assertEqual(self.client.get("/api/companies/directory").get_data(), resp.get_data())

    def test_a_cold_box_answers_202_and_kicks_once(self):
        resp = self.client.get("/api/companies/directory")
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.get_json()["warming"])
        self.kick.assert_called_once()

    def test_a_day_old_list_is_left_to_the_master(self):
        self._seed(age=directory.TTL_SECONDS + 600)
        self.assertEqual(self.client.get("/api/companies/directory").status_code, 200)
        self.kick.assert_not_called()

    def test_a_list_the_master_abandoned_is_refetched_behind(self):
        self._seed(age=2 * directory.TTL_SECONDS + 600)
        self.assertEqual(self.client.get("/api/companies/directory").status_code, 200)
        self.kick.assert_called_once()

    def test_the_page_carries_the_full_directory_controls(self):
        html = self.client.get("/companies").get_data(as_text=True)
        self.assertIn('data-co-view="followed"', html)
        self.assertIn('id="coExch"', html)
        self.assertIn('id="coMore"', html)
        self.assertIn("/api/companies/directory", html)
        self.assertIn('data-i18n="companies.all_exchanges"', html)

    def test_every_composed_string_exists_in_both_languages(self):
        tpl = (ROOT / "ystocker/templates/companies.html").read_text()
        keys = set(re.findall(r"tr\('(companies\.[a-z0-9_]+)'", tpl))
        keys |= set(re.findall(r'data-i18n="(companies\.[a-z0-9_]+)"', tpl))
        self.assertGreater(len(keys), 15)
        i18n = I18N.read_text()
        for key in sorted(keys):
            self.assertRegex(i18n, r"'%s':\s*\{\s*en: '[^']+',\s*zh: '[^']+'" % re.escape(key), key)


if __name__ == "__main__":
    unittest.main()
