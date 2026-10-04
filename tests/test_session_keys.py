"""Every app signs its session with a real key or a random one, never a constant.

Found 2026-10-04: /ystocker/YSTOCKER_SECRET_KEY had never been created in SSM,
so production signed sessions with the "ystocker-dev-secret" this repository
spelled out, and a cookie forged with it was accepted. yPlanter, yPay, yBG and
yImage had the same fallback with no parameter behind it, and yHome hardcoded
its key outright, so yBG's admin login could be forged by anyone who read the
code. Each app now reads its key from the environment and, without one, draws
a random key for the process (logging a warning). Under gunicorn --preload that
key is made once in the master and shared by the workers.

No network, no AWS: the factories run are the two that need neither (yHome, and
yBG with SSM and .env stubbed); the rest are checked by source.
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APPS = ("ystocker", "yplanner", "yplanter", "yhome", "ytracker", "ypay", "yimage", "ybg")


class NoConstantSessionKey(unittest.TestCase):

    def test_no_app_falls_back_to_a_constant(self):
        for app in APPS:
            src = (ROOT / app / "__init__.py").read_text()
            env = f"{app.upper()}_SECRET_KEY"
            with self.subTest(app=app):
                # Read with no default, so a missing variable is None, not a constant.
                self.assertRegex(src, rf'environ\.get\("{env}"\)\n')
                self.assertIn("token_hex(32)", src)
                self.assertNotRegex(src, r'"[a-z]+-dev-(secret|key)"')
                self.assertIn(f'"{env} is not set', src)    # and it says so in the log


def _without(*names):
    env = {k: v for k, v in os.environ.items() if k not in names}
    return mock.patch.dict(os.environ, env, clear=True)


class FactoriesDrawAFreshKey(unittest.TestCase):

    def test_yhome(self):
        from yhome import create_app
        with _without("YHOME_SECRET_KEY"), self.assertLogs("yhome", "WARNING"):
            a, b = create_app(), create_app()
        self.assertNotEqual(a.config["SECRET_KEY"], b.config["SECRET_KEY"])
        self.assertEqual(len(a.config["SECRET_KEY"]), 64)
        with mock.patch.dict(os.environ, {"YHOME_SECRET_KEY": "k" * 48}):
            self.assertEqual(create_app().config["SECRET_KEY"], "k" * 48)

    def test_ybg_admin_sessions_cannot_be_signed_with_a_known_key(self):
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *a, **k: False
        aws = {"AWS_SHARED_CREDENTIALS_FILE": os.devnull, "AWS_CONFIG_FILE": os.devnull,
               "AWS_EC2_METADATA_DISABLED": "true"}
        with mock.patch.dict(sys.modules, {"dotenv": dotenv}), \
             _without("YBG_SECRET_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                      "AWS_SESSION_TOKEN", "AWS_PROFILE"), \
             mock.patch.dict(os.environ, aws):
            import ybg
            with self.assertLogs("ybg", "WARNING"):
                a, b = ybg.create_app(), ybg.create_app()
            self.assertNotEqual(a.secret_key, b.secret_key)
            self.assertNotEqual(a.secret_key, "ybg-dev-secret")
            with mock.patch.dict(os.environ, {"YBG_SECRET_KEY": "s" * 48}):
                self.assertEqual(ybg.create_app().secret_key, "s" * 48)


if __name__ == "__main__":
    unittest.main()
