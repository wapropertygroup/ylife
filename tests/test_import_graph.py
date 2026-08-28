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
    "YF_TIMEOUT_SECONDS",
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
        for path in ("/markets", "/api/markets", "/api/market-brief", "/daily"):
            self.assertIn(path, rules, f"route missing: {path}")

    def test_cached_modules_expose_peek(self):
        """brief.py calls peek() on each of these; a rename would break it."""
        import importlib
        for name in ("fed", "fedwatch", "housing", "valuation", "breadth"):
            with self.subTest(module=name):
                mod = importlib.import_module(f"ystocker.{name}")
                self.assertTrue(callable(getattr(mod, "peek", None)),
                                f"ystocker.{name}.peek() is missing")

    def test_quota_limits_are_sane(self):
        from ystocker import quota
        self.assertLessEqual(quota.limit_default(), quota.limit_vip())
        self.assertLessEqual(quota.limit_vip(), quota.limit_global())


if __name__ == "__main__":
    unittest.main(verbosity=2)
