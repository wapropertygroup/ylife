"""
AAII's sentiment file is downloaded only when a newer survey can exist.

Through Flask's test client, with ``requests.get`` counted: a process with
nothing in memory serves the copy on disk rather than downloading (three
processes doing that on every deploy is what put the box behind Imperva's bot
wall on 2026-10-03), downloads once the next Thursday's release is due, and
calls the copy stale only when a newer survey was due and could not be had.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Hermetic -- no thread, no secret, no AWS, no network.

Run:  venv/bin/python -m tests.check_aaii_fetch
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-aaii-secret")
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
from ystocker import routes                               # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

HELD = {"latest": {"date": "2026-10-01", "bullish": 34.6, "neutral": 18.9, "bearish": 46.5,
                   "bull_bear_spread": -11.8},
        "history": [{"date": "2026-10-01", "bullish": 34.6, "neutral": 18.9, "bearish": 46.5,
                     "bull_bear_spread": -11.8}]}


def _ts(*args):
    return dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp()


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class ReleaseRule(unittest.TestCase):
    def test_the_next_thursday_afternoon(self):
        latest = {"date": "2026-10-01"}                  # a Thursday
        self.assertFalse(routes._aaii_release_due(latest, _ts(2026, 10, 4, 12)))
        self.assertFalse(routes._aaii_release_due(latest, _ts(2026, 10, 8, 15, 0)))
        self.assertTrue(routes._aaii_release_due(latest, _ts(2026, 10, 8, 15, 31)))
        self.assertTrue(routes._aaii_release_due(None, _ts(2026, 10, 4)))
        self.assertTrue(routes._aaii_release_due({"date": "garbage"}, _ts(2026, 10, 4)))


class AaiiEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "aaii_cache.json"
        self.get = mock.Mock(side_effect=AssertionError("must not download"))
        self.patches = [mock.patch.object(routes, "_AAII_FILE", self.file),
                        mock.patch("requests.get", self.get),
                        mock.patch.object(routes, "_aaii_load_from_dynamo", return_value=None)]
        for p in self.patches:
            p.start()
        routes._AAII_CACHE.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        routes._AAII_CACHE.clear()
        self.tmp.cleanup()

    def _hold(self, mtime):
        self.file.write_text(json.dumps(HELD))
        os.utime(self.file, (mtime, mtime))

    def test_a_fresh_process_serves_the_disk_copy_without_downloading(self):
        self._hold(_ts(2026, 10, 1, 15, 40))
        with mock.patch("time.time", return_value=_ts(2026, 10, 4, 12)):
            body = self.client.get("/api/aaii-sentiment").get_json()
        self.assertEqual(body["latest"]["date"], "2026-10-01")
        self.assertNotIn("_stale", body)
        self.get.assert_not_called()

    def test_a_due_release_is_fetched_and_a_failure_is_called_stale(self):
        self._hold(_ts(2026, 10, 1, 15, 40))
        self.get.side_effect = RuntimeError("Imperva says no")
        with mock.patch("time.time", return_value=_ts(2026, 10, 8, 16)):
            body = self.client.get("/api/aaii-sentiment").get_json()
        self.get.assert_called_once()
        self.assertTrue(body["_stale"])
        self.assertEqual(body["latest"]["date"], "2026-10-01")

    def test_a_failure_before_anything_is_due_is_not_stale(self):
        self.get.side_effect = RuntimeError("Imperva says no")
        routes._AAII_CACHE["data"] = {"ts": 0, "data": dict(HELD, _stale=True)}
        with mock.patch("time.time", return_value=_ts(2026, 10, 4, 12)):
            body = self.client.get("/api/aaii-sentiment").get_json()
        self.assertNotIn("_stale", body)
        self.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
