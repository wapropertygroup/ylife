"""Smoke-test every parameterless GET endpoint for a 500.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app and
stubs matplotlib, which is the same reason ``check_dca_endpoints.py`` is named
that way.

Why this exists
---------------
On 2026-09-19 a rewritten ``/api/upcoming-earnings`` reached production and
returned 500 on every request. The cause was one line — ``datetime.datetime.now``
in a module that imports ``datetime`` inside functions rather than at module
scope — and nothing caught it. The logic had 19 unit tests, all passing, because
they covered the pure module underneath and nothing exercised the route.

That is the gap this closes, and the class matters more than the instance: a
``NameError``, a bad import, a typo'd helper name, or a template that no longer
renders are all invisible to a unit test of the function beneath them and all
produce a 500 the moment somebody loads the page.

What it does *not* assert is content. An endpoint answering 202 because a cache
is cold, or 404 for a symbol that does not exist here, is behaving correctly —
this is only looking for the responses that mean the code is broken rather than
the data being absent.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Any(dict):
    """Stands in for anything matplotlib is asked for, including subscripting
    (`plt.rcParams["figure.dpi"] = 110` runs at import)."""

    def __call__(self, *a, **k): return _Any()
    def __getattr__(self, _n): return _Any()
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-api-smoke")
# Never let a check edit real holdings.
os.environ["ASSETS_LOCAL_STORE"] = "1"

from ystocker import create_app                                   # noqa: E402

#: Endpoints that mutate, cost money, or take tens of minutes. Refresh routes
#: purge a cache and re-fetch from Yahoo; the agents routes spend credits.
SKIP_PREFIXES = ("/refresh", "/api/agents", "/agents")
SKIP_SUFFIXES = ("/refresh",)


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def _targets(self) -> list[str]:
        out = []
        for rule in self.app.url_map.iter_rules():
            if "GET" not in (rule.methods or set()):
                continue
            if rule.arguments:                       # needs a path parameter
                continue
            path = str(rule.rule)
            if path.startswith(SKIP_PREFIXES) or path.endswith(SKIP_SUFFIXES):
                continue
            if not path.startswith("/api/"):
                continue
            out.append(path)
        return sorted(out)

    def test_no_api_endpoint_raises(self):
        """A 500 means the code is broken. Every other status is a data state."""
        broken: dict[str, str] = {}
        checked = 0
        for path in self._targets():
            checked += 1
            try:
                response = self.client.get(path)
            except Exception as exc:                 # noqa: BLE001 - reporting
                broken[path] = f"raised {type(exc).__name__}: {exc}"
                continue
            # 500 only. 502 and 503 are honest states — an upstream that is
            # down or a cache still warming — and in this sandbox Yahoo is
            # blocked outright, so a 502 from /api/skew is the endpoint
            # reporting correctly rather than failing. Treating them as
            # failures makes the check unrunnable here, which is the same as
            # not having it.
            if response.status_code == 500:
                # The body carries the traceback under TESTING, which is what
                # makes the failure actionable rather than just a status code.
                detail = response.get_data(as_text=True).strip().splitlines()
                broken[path] = f"{response.status_code}: {detail[-1] if detail else ''}"

        self.assertGreater(checked, 10, "the route scan found almost nothing")
        self.assertEqual(broken, {}, f"endpoints returning 5xx: {broken}")

    def test_the_scan_reaches_the_endpoint_this_was_written_for(self):
        """Guards the filter. A SKIP rule that quietly grew to cover everything
        would make the test above pass for ever."""
        self.assertIn("/api/upcoming-earnings", self._targets())


if __name__ == "__main__":
    unittest.main(verbosity=2)
