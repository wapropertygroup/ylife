"""
End-to-end check of the DCA tab on /history, through Flask's test client:
``/dca/<ticker>?embed=1`` (the page framed inside the tab), the full page it
must leave untouched, the rebuild's way back, and the tab on /history.

Asked for, 2026-10-04: "https://trade-agents.com/dca/ADBE can be a tab in
https://trade-agents.com/history/ADBE right?" The tab frames the /dca page
itself rather than copying it, so these pin the seams that framing creates:
* the framed copy drops the site's chrome and the page's own header, keeps the
  element its script writes the name into, reports its height, sends links
  that leave it to the top window, and is not indexed;
* the full page renders exactly as before -- no frame logic, no noindex;
* a rebuild started in the tab comes back to the framed page, not to the full
  page inside the frame;
* /history carries the tab, the panel, the frame's loader and its ``?tab=dca``
  link;
* every tab has a deep link: switching writes ``?tab=`` and each opens from it.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app.
Built hermetically, as ``check_fundamentals_endpoints`` is -- no background
thread, no secret, no AWS, no network. The rebuild is a stub.

Run:  venv/bin/python -m tests.check_dca_embed
"""
from __future__ import annotations

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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-dca-embed-secret")
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
           "AWS_PROFILE", "AGENTS_ALLOWED_EMAILS"):
    os.environ.pop(_k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AGENTS_EMAIL_REPORT"] = "0"
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *a, **k: False
_dotenv.find_dotenv = lambda *a, **k: ""
_dotenv.dotenv_values = lambda *a, **k: {}
sys.modules["dotenv"] = _dotenv

import threading                                          # noqa: E402

import ystocker                                           # noqa: E402
from ystocker import dca_history, routes                  # noqa: E402

ystocker._load_secrets_from_ssm = lambda *a, **k: None

# The class on <body>, not the string: base.html's <head> carries the
# `body.agents-embedded > main` rule on every page.
BODY_EMBEDDED = r'<body class="[^"]*\bagents-embedded\b'


def _build_app():
    real_start = threading.Thread.start
    threading.Thread.start = lambda self: None
    try:
        return ystocker.create_app()
    finally:
        threading.Thread.start = real_start


class DcaEmbed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.kicks = []
        self._kick, routes._dca_kick = routes._dca_kick, lambda s: (self.kicks.append(s), True)[1]
        self._peek, dca_history.peek = dca_history.peek, lambda s: None

    def tearDown(self):
        routes._dca_kick = self._kick
        dca_history.peek = self._peek

    def _page(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return resp.get_data(as_text=True)

    def test_the_framed_page_drops_the_chrome_and_keeps_its_hooks(self):
        html = self._page("/dca/ADBE?embed=1")
        self.assertRegex(html, BODY_EMBEDDED)                  # base.html's embedded mode
        self.assertNotIn('data-i18n="footer.text"', html)      # no site footer
        self.assertNotIn('data-i18n="dca.back"', html)         # no back button / crumbs
        self.assertIn('<span id="stockName" hidden></span>', html)
        self.assertIn('id="dcaRoot"', html)
        self.assertIn("dca:height", html)
        self.assertIn('<meta name="robots" content="noindex">', html)
        self.assertIn('data-i18n="dca.open_full"', html)
        self.assertRegex(html, r'href="/dca/ADBE"\s+target="_top"')
        self.assertIn("/dca/ADBE/refresh?embed=1", html)

    def test_the_full_page_is_unchanged(self):
        html = self._page("/dca/ADBE")
        self.assertNotRegex(html, BODY_EMBEDDED)
        self.assertIn('data-i18n="dca.back"', html)
        self.assertIn('data-i18n="dca.to_history"', html)
        self.assertNotIn("dca:height", html)
        self.assertNotIn('id="dcaRoot"', html)
        self.assertNotIn('content="noindex"', html)
        self.assertNotIn("refresh?embed=1", html)

    def test_only_embed_1_frames_the_page(self):
        self.assertNotIn("dca:height", self._page("/dca/ADBE?embed=0"))
        self.assertNotIn("dca:height", self._page("/dca/ADBE?embed=yes"))

    def test_a_rebuild_from_the_tab_comes_back_framed(self):
        resp = self.client.get("/dca/ADBE/refresh?embed=1")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/dca/ADBE?embed=1"), resp.headers["Location"])
        self.assertEqual(self.kicks, ["ADBE"])

    def test_a_rebuild_from_the_full_page_comes_back_full(self):
        resp = self.client.get("/dca/ADBE/refresh")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/dca/ADBE"), resp.headers["Location"])

    def test_history_carries_the_tab(self):
        html = self._page("/history/ADBE")
        self.assertIn('id="tabDcaBtn"', html)
        self.assertIn('id="panelDca"', html)
        self.assertIn("function _initDcaPanel()", html)
        self.assertIn("?embed=1&lang=", html)
        self.assertIn("e.source !== frame.contentWindow", html)   # only this frame's messages
        self.assertRegex(html, r"const HISTORY_TABS = \[[^\]]*'dca'")
        self.assertIn("HISTORY_TABS.includes(_tab)", html)      # ?tab=dca opens it
        self.assertIn('id="tabDcaBtn" onclick="switchTab(\'dca\')"', html)

    def test_every_tab_has_a_deep_link(self):
        # Asked for: "each tab can have a deep link". Switching writes ?tab=,
        # and every tab button names a tab the deep-link block accepts.
        import re
        html = self._page("/history/ADBE")
        tabs = re.search(r"const HISTORY_TABS = \[([^\]]*)\]", html).group(1)
        listed = set(re.findall(r"'(\w+)'", tabs))
        buttons = set(re.findall(r'onclick="switchTab\(\'(\w+)\'\)"', html))
        self.assertEqual(buttons, listed)
        self.assertEqual(listed, {"charts", "fundamentals", "dca", "news", "videos", "research"})
        self.assertIn("_writeTabToAddress(tab)", html)
        self.assertIn("history.replaceState", html)


if __name__ == "__main__":
    unittest.main()
