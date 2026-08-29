"""Import-graph smoke test.

Nothing else in the default suite imports ``ystocker.routes``. The scripts that
do are named ``check_*`` so ``unittest discover`` skips them — right, since they
need live caches and a Gemini key, but it left the import graph unguarded by
anything that runs by default.

That gap shipped a 502: a refactor of ``data.py`` removed ``TICKER_BACKOFF``,
which ``routes.py`` imports at module scope, and the 128-test suite passed
because no test ever imported it. gunicorn found out instead, and with
``--preload`` an ImportError in ``create_app`` means the master never binds
:8000 at all.

So this asserts only what is cheap and would have caught it: every module
imports, and the names one module takes from another still exist.

matplotlib is stubbed because ``charts.py`` imports pyplot at module scope and
this machine's Homebrew python has a broken pyexpat — the stubs are about being
able to run the test at all, not about what it checks.
"""

from __future__ import annotations

import sys
import types
import unittest


def _stub(name: str, **attrs) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)


def _install_plot_stubs() -> None:
    noop = lambda *a, **k: None      # noqa: E731
    _stub("matplotlib", use=noop, rcParams={}, __version__="0")
    _stub("matplotlib.pyplot", subplots=noop, close=noop, figure=noop,
          savefig=noop, rcParams={}, tight_layout=noop)
    _stub("matplotlib.ticker", FuncFormatter=object, MaxNLocator=object)
    _stub("matplotlib.dates", DateFormatter=object)
    _stub("matplotlib.patches", Patch=object, Rectangle=object)
    _stub("matplotlib.colors", LinearSegmentedColormap=object, to_hex=noop,
          Normalize=object)
    _stub("matplotlib.cm", get_cmap=noop)
    _stub("matplotlib.font_manager", FontProperties=object, findSystemFonts=noop,
          fontManager=types.SimpleNamespace(addfont=noop, ttflist=[]))
    _stub("seaborn", set_theme=noop, set_style=noop, despine=noop, heatmap=noop,
          color_palette=noop, barplot=noop, scatterplot=noop)


_install_plot_stubs()

# Import the real package *now*, at module load, before any other test module
# gets the chance to put a stub in its place. tests/test_report_email.py
# deliberately installs a fake ``ystocker`` package with
# ``sys.modules.setdefault`` so it can run without an app, and whichever module
# unittest loads first wins. Discovery happens to load this file first
# (alphabetically), so the suite passes -- but naming both modules explicitly
# (``python -m unittest tests.test_import_graph tests.test_report_email``) loads
# every named module before running any of them, the stub wins, and every import
# here fails with a baffling "cannot import name PEER_GROUPS from ystocker
# (unknown location)". One line here makes the order stop mattering.
import ystocker  # noqa: E402,F401  - load order is the point

# Names routes.py takes from data.py at module scope. Listed explicitly so a
# deletion fails here with a readable message rather than at gunicorn start.
DATA_EXPORTS = (
    "TICKER_BACKOFF",
    "fetch_group",
    "dividend_yield_pct",
    "ps_ratio",
    "reset_yf_for_process",
    "FetchError",
    "latest_price",
    "PROVIDER",
)

MODULES = (
    "ystocker.data",
    "ystocker.fetchguard",
    "ystocker.freshness",
    "ystocker.brief",
    "ystocker.fed",
    "ystocker.fedwatch",
    "ystocker.housing",
    "ystocker.valuation",
    "ystocker.breadth",
    "ystocker.sec13f",
    "ystocker.quota",
    "ystocker.share",
    "ystocker.etf_holdings",
    "ystocker.analyst",
    "ystocker.sectors",
    "ystocker.cta",
    "ystocker.futu",
    "ystocker.routes",
)


class ImportGraphTests(unittest.TestCase):
    def test_every_module_imports(self):
        import importlib
        for name in MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_data_exports_routes_depends_on(self):
        import ystocker.data as data
        missing = [n for n in DATA_EXPORTS if not hasattr(data, n)]
        self.assertEqual(missing, [], f"ystocker.data lost: {missing}")

    def test_routes_blueprint_is_registerable(self):
        from flask import Flask

        from ystocker.routes import bp
        app = Flask(__name__)
        app.register_blueprint(bp)
        rules = {str(r) for r in app.url_map.iter_rules()}
        # A few routes that must exist; a blueprint that registers but exposes
        # nothing would otherwise pass.
        for path in ("/markets", "/api/markets", "/api/market-brief", "/daily",
                     "/api/evaluation-extras", "/api/cta-positioning",
                     # Sharing: the public read side is what matters here. An
                     # emailed capability URL outlives any deploy, so a route
                     # rename would break links already sitting in inboxes --
                     # unlike an in-app path, where nothing holds a stale copy.
                     "/agents/shared/<token>", "/api/agents/shared/<token>",
                     "/api/agents/shared/<token>/pdf", "/api/agents/share"):
            self.assertIn(path, rules, f"route missing: {path}")

    def test_share_and_report_email_agree_on_expiry(self):
        """The mail promises a window; share.lookup enforces one.

        report_email.build() prints share.TTL_DAYS into the footer, so these
        cannot drift while that import stands -- this asserts the import is still
        how it gets there, since replacing it with a local constant would be an
        easy "cleanup" that makes the mail lie.
        """
        from ystocker import report_email, share

        self.assertIsInstance(share.TTL_DAYS, int)
        self.assertGreater(share.TTL_DAYS, 0)
        job = {"id": "x", "ticker": "NVDA", "date": "2026-08-29", "status": "done",
               "lang": "en", "report": "# NVDA\n\n## Market Analyst\n\nUp."}
        built = report_email.build(job, shared_by="a@b.com", note="",
                                   link_override="https://example.com/agents/shared/t")
        self.assertIsNotNone(built)
        self.assertIn(str(share.TTL_DAYS), built[1])

    def test_cached_modules_expose_peek(self):
        """brief.py calls peek() on each of these; a rename would break it."""
        import importlib
        for name in ("fed", "fedwatch", "housing", "valuation", "breadth",
                     "etf_holdings"):
            with self.subTest(module=name):
                mod = importlib.import_module(f"ystocker.{name}")
                self.assertTrue(callable(getattr(mod, "peek", None)),
                                f"ystocker.{name}.peek() is missing")

    def test_yfinance_internals_the_fork_fix_depends_on(self):
        """reset_yf_for_process() reaches into yfinance private internals.

        That is deliberate — there is no public way to rebuild the singleton
        after a fork — but it means a yfinance upgrade can silently break the one
        thing standing between --preload and a worker crash-loop. These are the
        exact attributes it touches; if an upgrade moves any of them this fails
        here rather than as SIGSEGV in production.
        """
        from yfinance.data import SingletonMeta, YfData

        self.assertIsInstance(getattr(SingletonMeta, "_instances", None), dict)
        lock = getattr(SingletonMeta, "_lock", None)
        self.assertTrue(hasattr(lock, "acquire") and hasattr(lock, "release"),
                        "SingletonMeta._lock is no longer a lock")
        self.assertIs(type(YfData), SingletonMeta,
                      "YfData is no longer built by SingletonMeta")

        inst = YfData()
        self.assertIn(YfData, SingletonMeta._instances,
                      "instantiating YfData no longer registers in _instances")
        cookie_lock = getattr(inst, "_cookie_lock", None)
        self.assertTrue(hasattr(cookie_lock, "acquire"),
                        "YfData._cookie_lock is gone — the inherited-lock hang "
                        "this guards against would no longer be detectable")
        self.assertIsNotNone(getattr(inst, "_session", None),
                             "YfData._session is gone")

    def test_reset_yf_rebuilds_the_singleton(self):
        """The reset must actually replace the instance, not just no-op."""
        from yfinance.data import SingletonMeta, YfData

        import ystocker.data as data
        YfData()
        before = SingletonMeta._instances.get(YfData)
        data._yf_fork_pid = None          # pretend we are a fresh process
        self.assertTrue(data.reset_yf_for_process())
        after = SingletonMeta._instances.get(YfData)
        self.assertIsNotNone(after)
        self.assertIsNot(before, after, "singleton was not rebuilt")
        # Idempotent within a pid.
        self.assertFalse(data.reset_yf_for_process())

    def test_yfinance_enforces_a_request_timeout(self):
        """The 30s bound is yfinance's, and data.py's comments rest on it."""
        import inspect

        from yfinance import data as yd
        for fn in ("get", "post", "_make_request", "_get_cookie_and_crumb"):
            with self.subTest(fn=fn):
                sig = inspect.signature(getattr(yd.YfData, fn))
                self.assertIn("timeout", sig.parameters,
                              f"YfData.{fn} lost its timeout parameter")

    def test_quota_limits_are_sane(self):
        from ystocker import quota
        self.assertLessEqual(quota.limit_default(), quota.limit_vip())
        self.assertLessEqual(quota.limit_vip(), quota.limit_global())


if __name__ == "__main__":
    unittest.main(verbosity=2)
