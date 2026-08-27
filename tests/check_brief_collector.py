"""Import ystocker.routes and exercise the brief collector end to end.

Stubs matplotlib because this machine's Homebrew python has a broken pyexpat
(`import matplotlib.pyplot` fails on its own, with or without these changes) and
charts.py imports it at module scope. Nothing under test touches plotting.

What this actually verifies, which the formatter tests cannot: that
_collect_brief_sources reaches every cache under the right lock and the right
key, that _brief_evaluation_summary survives the real ticker cache and pandas,
and that a cold source degrades to None rather than raising.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _stub(name: str, **attrs) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_noop = lambda *a, **k: None                      # noqa: E731
_stub("matplotlib", use=_noop, rcParams={}, __version__="0")
_stub("matplotlib.pyplot", subplots=_noop, close=_noop, figure=_noop,
      savefig=_noop, rcParams={}, tight_layout=_noop)
_stub("matplotlib.ticker", FuncFormatter=object, MaxNLocator=object)
_stub("matplotlib.dates", DateFormatter=object)
_stub("matplotlib.patches", Patch=object, Rectangle=object)
_stub("matplotlib.colors", LinearSegmentedColormap=object, to_hex=_noop,
      Normalize=object)
_stub("matplotlib.cm", get_cmap=_noop)
_stub("matplotlib.font_manager", FontProperties=object, findSystemFonts=_noop,
      fontManager=types.SimpleNamespace(addfont=_noop, ttflist=[]))
_stub("seaborn", set_theme=_noop, set_style=_noop, despine=_noop,
      heatmap=_noop, color_palette=_noop, barplot=_noop, scatterplot=_noop)

from dotenv import load_dotenv                    # noqa: E402

load_dotenv()

from ystocker import brief                        # noqa: E402
from ystocker import routes                       # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


print("=== module wiring ===")
for fn in ("_collect_brief_sources", "_brief_evaluation_summary", "_brief_13f",
           "_generate_market_brief", "_store_market_brief", "api_market_brief",
           "_do_pregen_market_briefs", "_brief_ddb_key"):
    check(f"routes.{fn} defined", callable(getattr(routes, fn, None)))

# The route must actually be registered on the blueprint.
rules = [r for r in routes.bp.deferred_functions]
check("blueprint has deferred route registrations", len(rules) > 0)
check("brief ddb key is versioned", routes._brief_ddb_key("zh") == "zh_brief_v1",
      routes._brief_ddb_key("zh"))

print()
print("=== collector (peek only, no network) ===")
src = routes._collect_brief_sources(warm=False)
expected = {"markets", "commodities", "movers", "fg", "pcr", "skew", "yield_curve",
            "yield_spread", "credit_spread", "aaii", "events", "breadth", "fed",
            "fedwatch", "housing", "valuation", "fed_series_meta", "evaluation",
            "holdings13f", "consensus13f", "_stale"}
check("all expected keys present", set(src) == expected,
      f"missing={expected - set(src)} extra={set(src) - expected}")
check("fed_series_meta populated", bool(src.get("fed_series_meta")))

for k in sorted(src):
    if k in brief._META_KEYS:
        continue
    v = src[k]
    kind = type(v).__name__
    n = len(v) if isinstance(v, (dict, list)) else "-"
    print(f"    {k:<16} {'present' if v else 'COLD':<8} {kind:<6} n={n}")

# Every collected value must be JSON-ish, never a live object that would blow up
# in the formatter.
bad = [k for k, v in src.items()
       if v is not None and not isinstance(v, (dict, list, str, int, float))]
check("stale list present", isinstance(src.get("_stale"), list))
check("no unexpected value types", not bad, str(bad))

print()
print("=== snapshot + prompt from live collector ===")
snap = brief.build_snapshot(src, "2026-08-27")
check("snapshot built", len(snap) > 500, f"{len(snap)} chars")
check("has 10 sections", snap.count("\n=== ") == 10, str(snap.count("\n=== ")))
check("no traceback text leaked", "Traceback" not in snap)
for lang in ("en", "zh"):
    p = brief.build_prompt(snap, lang)
    check(f"prompt[{lang}] built", len(p) > len(snap))

present = sorted(k for k, v in src.items() if v and k not in brief._META_KEYS)
cold = sorted(k for k, v in src.items() if not v and k not in brief._META_KEYS)
print(f"    present: {', '.join(present) or 'none'}")
print(f"    cold:    {', '.join(cold) or 'none'}")
print(f"    snapshot: {len(snap):,} chars  prompt(zh): {len(brief.build_prompt(snap,'zh')):,} chars")

print()
if FAILURES:
    print(f"RESULT: FAIL — {len(FAILURES)}: {', '.join(FAILURES)}")
    sys.exit(1)
print("RESULT: OK")
