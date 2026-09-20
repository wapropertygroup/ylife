---
name: ystocker-testing
description: "How to run and write tests in the yStocker monorepo (/Users/yuanxili/workspace/ystocker). Use this before running the test suite, before concluding a test failure is your fault, and before writing a new test. The suite is two-tier by naming convention — `tests/test_*.py` runs under `unittest discover` with no network, `tests/check_*.py` and `tests/check_*.mjs` are hand-run diagnostics that need a live cache, a Flask app, a Gemini key or node — and it has a KNOWN-FAILING baseline of 4 tests on this machine that are environmental, not regressions. Read this if `python3 -m unittest` reports 37 errors (you forgot the venv), if a test fails on `pyexpat` / `openpyxl` / `pdfplumber`, or when adding a guard test for a silent front-end failure."
---

# yStocker — the test suite

~1,630 assertions across 54 `test_*.py` files, 10 `check_*.py` diagnostics and
8 `check_*.mjs` node scripts. No pytest config, no `conftest.py`, no CI — the
suite is run by hand.

## Run it

```bash
source venv/bin/activate                  # REQUIRED — see below
python -m unittest discover -s tests      # the default suite
python -m pytest tests/                   # equivalent if pytest is installed

for f in tests/check_*.mjs; do node "$f"; done   # the JS guards
```

**The venv is not optional.** Bare `python3` on this machine has neither flask
nor requests, so `python3 -m unittest discover -s tests` reports **37 import
errors and only 536 tests collected** — which reads like a broken suite and is
just a missing interpreter. Inside the venv it collects **1,633**.

## The known-failing baseline (verified 2026-09-20)

```
Ran 1633 tests — FAILED (failures=3, errors=1, skipped=14)
```

All four are **environmental, not regressions.** Do not chase them, and do not
let them mask a real failure — diff against this list:

| test | cause |
|---|---|
| `test_futu_links` (import error) | broken Homebrew `pyexpat` |
| `test_portfolio_import.XlsxImportTests.test_multisheet_benefit_statement…` | broken Homebrew `pyexpat` |
| `test_portfolio_import.PdfImportTests.test_pdf_table_uses_canonical_mapping…` | `pdfplumber` not installed |
| `test_portfolio_import.PdfImportTests.test_image_or_empty_pdf_has_an_actionable_error` | `pdfplumber` not installed |

### The `pyexpat` thing, because it will bite you again

This checkout's Homebrew Python 3.12 has a broken `pyexpat`:

```
ImportError: dlopen(.../pyexpat.cpython-312-darwin.so):
  Symbol not found: _XML_SetAllocTrackerActivationThreshold
```

It is a **shared root cause with two very different-looking symptoms**, because
anything that parses XML reaches it transitively:

- `matplotlib` → `font_manager` → `plistlib` → `xml.parsers.expat`. So any test
  that imports `charts.py`, `report_charts.py` or `share_card.py` dies at
  *import* time with a stack trace that mentions fonts.
- `openpyxl` → `xml.etree` (an `.xlsx` is a zip of XML). It imports fine and
  fails on first use with `No module named expat; use SimpleXMLTreeBuilder
  instead` — which reads like an openpyxl bug.

This is why `tests/test_import_graph.py` and
`tests/check_assets_endpoints.py` **stub matplotlib** rather than import it:
the stubs are about being able to run the test at all, not about what it
checks. Copy that pattern for any new test that touches a plotting module:

```python
def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items(): setattr(mod, k, v)
    sys.modules.setdefault(name, mod)

_stub("matplotlib", use=lambda *a, **k: None, rcParams={}, __version__="0")
_stub("matplotlib.pyplot", subplots=..., close=..., savefig=..., ...)
```

`share_card.py`'s docstring records that proving its layout needed a second,
throwaway venv with a working pyexpat — that is the documented workaround when
you genuinely must render a PNG.

## The two tiers, and why the naming carries the rule

`unittest discover` only collects `test*.py`. That is the whole mechanism:

- **`tests/test_*.py`** — runs by default. **No network, no database, no Flask
  app, no browser.** These are pure-function tests plus static analysers that
  parse the templates.
- **`tests/check_*.py`** — deliberately *not* collected. Needs a live cache, a
  Flask test client with matplotlib stubbed, or a real Gemini key.
  `check_brief_live.py` does one real generation and prints it.
- **`tests/check_*.mjs`** — node, no browser, no npm install (they only import
  `node:fs` / `node:path` and read the source files as text). All 8 pass.

A `check_*.py` is not second-class: `check_dca_endpoints.py` is 68 end-to-end
assertions and `check_assets_endpoints.py` is 49. They are excluded because
they need an app, not because they matter less. **Run the relevant `check_`
script by hand when you touch its feature** — `unittest discover` will not.

## What the pure modules are for

`lookthrough.py`, `portfolio_csv.py`, `dca.py`, `dcf.py`, `listing.py` are pure
and injectable *specifically so* the arithmetic can be proven without a cache
or a network. When adding logic, put the arithmetic in a pure module and the
I/O in its caller — that is the repo's testing strategy, not a style
preference.

Two examples of what that buys:

- `tests/test_lookthrough.py` asserts a **summation invariant** under every
  input ordering: `seen + undisclosed_equity + non_equity + unclassified +
  unresolved + truncated + pending == portfolio value`. If it ever stops
  holding, every percentage on `/assets` is wrong at once, and wrong quietly.
- `tests/test_dca.py` walks **both** of the source framework's worked examples
  end to end, so a change to the blend fails against a published number rather
  than against a snapshot of itself.

## The static guards — what each one protects

These need no browser and no app. They exist because the failure they catch is
invisible; each was written after the failure shipped.

| test | catches |
|---|---|
| `test_import_graph.py` | a module that no longer imports, or a cross-module name that vanished. Shipped a 502: `routes.py` imported `TICKER_BACKOFF` after `data.py` dropped it, and 128 tests passed because nothing imported `routes`. Under `--preload` that means the master never binds :8000. |
| `test_theme_classes.py` | a Tailwind class **pair** passed to `classList` / `closest` as a token (throws or silently matches nothing). Cannot tell code from comment — a comment quoting the broken form fails the build. `/tv` is excluded by design. |
| `test_chart_theme_map.py` | drift between `base.html`'s `CT` colour MAP and `migrate_chart_colors.py`'s hand-mirrored copy — which degrades to *no theming*, so nothing reports it. |
| `test_deferload_anchors.py` | a `DeferLoad.when()` anchor that is typo'd (loader never runs, spinner forever) or `display:none` (loads eagerly, so the page only *looks* lazy). |
| `test_template_ids.py` | the same element id rendered twice in one template — `getElementById` returns the first, so two renderers fight and the later one loses silently. |
| `test_i18n_completeness.py` | a translation that is `''` (falsy, so it renders as the **English** fallback), or a key missing a language. |
| `test_number_input_steps.py` | `type="number"` whose `step` base is `min`, making every whole number a `stepMismatch` that blocks `submit` before any listener runs. |
| `test_pwa_assets.py` | a manifest/service-worker path that 404s — the browser just declines to offer installation, with nothing in any log. |
| `test_deploy_heredocs.py` | shell expansion in `deploy.sh`'s nested nginx heredocs. |
| `check_deferload.mjs`, `check_pulltorefresh.mjs`, `check_autorefresh.mjs`, `check_sw_routing.mjs` | the behaviour of the four hand-rolled JS modules, by reading and evaluating their source. |

## Writing a new test — house rules

1. **Name it `test_*.py` only if it needs nothing external.** If it needs an
   app, a cache or a key, name it `check_*.py` and say so in the docstring.
2. **Lead with the failure it exists to catch**, concretely and with the date
   if it shipped. Every good test in this repo does; it is what makes the suite
   readable as a list of real bugs rather than a list of assertions.
3. **Prefer parsing the source over mocking a browser.** The guards above are
   all `re` + `pathlib` over templates. No browser is a feature — they run in
   milliseconds and in any environment.
4. **Stub matplotlib** if the import graph reaches it (see above).
5. **Assert on live markup, not on text.** A test that greps rendered HTML for
   `onerror=` **passes on its own escaping**: `&lt;img src=x onerror=alert(1)&gt;`
   is inert but contains the needle. `tests/test_report_email.py` strips
   `&lt;…&gt;` before checking, so it only ever asserts on real elements. Same
   trap with `href=`.
6. **If two code paths implement one formula, assert they agree.**
   `check_dca_endpoints.py` pins the ranked-table row against the detail page's
   score, because two implementations agree the day they are written and drift
   after.

## Counts worth knowing (they are quoted in `CLAUDE.md` and drift)

`test_dca.py` 72 · `test_dcf.py` 56 · `test_dca_history.py` 74 ·
`test_dcf_store.py` 18 · `test_dca_universe.py` 24 · `test_listing.py` 52 ·
`test_lookthrough.py` 27 · `test_portfolio_csv.py` 65 ·
`test_report_email.py` 76 · `test_brief_formatters.py` 60 ·
`test_agent_models.py` 43 · `test_fedwatch_range_probe.py` 27 ·
`check_dca_endpoints.py` 68 · `check_assets_endpoints.py` 49.

Treat these as approximate — they are maintained by hand in `CLAUDE.md` and the
suite grows faster than the prose.

## See also

- `ystocker-frontend` — the failures the static guards were written for.
- `ystocker` — deploy and ops; note a failing import here means gunicorn's
  master never binds its port under `--preload`.
