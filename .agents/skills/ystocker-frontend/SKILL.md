---
name: ystocker-frontend
description: "Front-end reference for the yStocker monorepo templates and static JS (/Users/yuanxili/workspace/ystocker) — the light/dark theme system, Chart.js colour mapping, EN/ZH i18n, and the four hand-rolled JS modules (deferload, autorefresh, pulltorefresh, sw). Use this BEFORE editing any template under */templates/, any file in ystocker/static/, any Chart.js config, any `classList` / `closest` call, any `data-i18n` key, or before adding a Tailwind class. Every trap documented here is a SILENT failure — the page still renders, so nothing tells you it broke. Read it in particular before: passing a Tailwind class string to the DOM, picking a colour for light mode, adding a `type: 'time'` axis, adding a dropdown inside a card, wiring DeferLoad.when(), or composing a user-visible string in JavaScript. For deploy/ops use the `ystocker` skill; for the test suite that guards all of this, `ystocker-testing`."
---

# yStocker front end — theme, i18n, and the hand-rolled JS

Eight Flask apps, Jinja2 + **compiled** Tailwind + vanilla JS. No build step
for the JS, no framework (except Alpine on yPlanner). Chart.js 4 for every
chart in yStocker.

The single most important property of this layer: **almost every mistake here
is invisible.** The page renders, the chart draws, the handler is bound — and
the colour is wrong, or the click does nothing, or the panel never fills. Each
section below is a failure that already shipped.

## Tailwind is compiled, not CDN

`shared/input.css` → `shared/tailwind.css` → copied into each app's
`static/css/tailwind.css`.

```bash
bash build_css.sh        # needs npx (tailwindcss@3) or pytailwindcss
```

Commit `shared/tailwind.css` **and** the seven `*/static/css/tailwind.css`
copies together with the template change that needed them.

Consequence: **a class a script invents at runtime is simply absent from the
bundle.** Tailwind scans `content:` globs in `tailwind.config.js` (templates +
static JS for 7 apps; `yhome` is excluded on purpose — it is hand-written CSS).
A class assembled by string concatenation in JS is not seen by the scanner and
does not exist in the output. This is why `pulltorefresh.js` injects its own
CSS rather than using utilities.

## Theme: dark-first, three tiers of light

Templates are written **dark-first** — the bare utility is the dark value, the
light counterpart is added beside it: `bg-white dark:bg-slate-900`.

Light mode is a three-tier plane hierarchy, because mapping both page and card
to white left every card as a floating border with nothing behind it:

| tier | light | dark |
|---|---|---|
| page | `slate-50` | `slate-950` |
| card | `white` | `slate-900` |
| inset well | `slate-100` | `slate-950/deep` |

The class is flipped by a **blocking inline script at the very top of
`<head>`**, above every stylesheet — one line lower and a dark frame paints
before the flip. It reads `localStorage['ystocker_theme']`, defaults to
**dark**, and deliberately ignores `prefers-color-scheme` (existing readers
never asked for the site to change appearance).

`toggleTheme()` reloads **only if the page contains a `<canvas>`** — CSS
restyles instantly, but a Chart.js instance bakes its colours in at
construction inside an async fetch callback that cannot be replayed.

### The three things `dark:` cannot reach

| what | mechanism |
|---|---|
| `<canvas>` (takes no CSS) | `CT.c('<dark colour>')` in `base.html` |
| hand-written `<style>` blocks (19 templates; 15 converted so far) | the shared `--t-*` custom properties |
| a class passed to the DOM as a *token* | `toggleClasses(el, pair, on)` |

## `CT.c()` — chart colours

A canvas is not styled by CSS, so ~490 colour literals in the dashboards'
Chart.js configs would stay dark on a white card. `CT.c()` maps a **dark**
value to its light counterpart, so each call site is a mechanical wrap of the
value already there:

```js
ticks: { color: '#64748b' }   →   ticks: { color: CT.c('#64748b') }
```

- **Unmapped colours pass through unchanged** — which is what makes a mistaken
  wrap of a data colour harmless rather than a silent recolouring.
- Lookup normalises case and whitespace, so `rgba(51,65,85,0.3)` and
  `rgba(51, 65, 85, .3)` both hit. It does **not** rewrite `.55` into `0.55` —
  those are separate keys and both spellings must be present or they silently
  disagree.
- `CT.dark` is the boolean; `Chart.defaults.color` / `borderColor` are set from
  `CT.c()` so legend and tooltip text follow too.
- Deliberately **not** a Chart.js plugin. v4 resolves `chart.options` through a
  proxy before `beforeUpdate` fires, and a blanket grey-out would flatten the
  axis colour-coding on the dual-axis charts, where a red right-hand axis is
  how a reader knows which line is CPI.

`migrate_chart_colors.py` (idempotent) wraps a newly added dark-only template.
It mirrors `base.html`'s MAP **by hand**, so the two can drift —
`tests/test_chart_theme_map.py` exists because that drift degrades to *no
theming*, which nothing reports.

### Light-mode contrast rules

- **`text-<hue>-400` is invisible on white.** `text-emerald-400` is 1.87:1.
  Light counterparts land on **shade-700** — the first rung that clears AA on
  white for every hue in the set (emerald-600 is 3.4:1, amber-600 only 3.0:1).
- **Greys collapse, they do not go paler.** `slate-500` is the muted-text floor
  at 4.76:1, so both `text-slate-500` and `text-slate-600` map onto it.
- In chart space the rule is 400 → 600, with alpha raised by ~0.25 (floored at
  whatever the hue needs for 3:1). Yellow is the exception: yellow-600 is
  2.94:1, so `#facc15` → `#a16207` (700).
- **A fade overlay is the loud version of this.** `linear-gradient(…, #0f172a)`
  over a white card renders as a black block — use `--t-fade-from/to`.

## `toggleClasses()` — a class pair is not a DOM token

Light mode turned every hardcoded colour utility into a **pair**
(`bg-slate-100 dark:bg-slate-800`). That is fine in a `class=` attribute and
broken everywhere the same string reaches the DOM as a token:

- `classList.add` / `remove` are **variadic** — one token per argument.
- `classList.toggle` / `contains` take **exactly one** token; a space throws
  `InvalidCharacterError`.
- A **selector** is worse: `closest('.a dark:b')` parses as a *descendant*
  selector, so it throws or silently matches nothing.

All three throw only when the handler runs — on a click, or inside an IIFE
whose `catch` swallows it. In `fed.html` this killed the page's entire init
block and the only symptom was one console line.

```js
window.toggleClasses(el, 'bg-slate-100 dark:bg-slate-800', on);   // use this
el.classList.add(...'a b'.split(' '));                            // or variadic
```

For a selector, use a `[data-*]` hook — restyling cannot invalidate it.
`tests/test_theme_classes.py` fails on all three shapes and needs no browser.
It **cannot tell code from comment**, so an explanatory comment quoting the
broken form fails the build.

## Chart.js — there is no date adapter

`base.html` loads `chart.umd.min.js` alone. `type: 'time'` throws inside
Chart.js and leaves an **empty canvas**. Every chart on the site uses a
category axis, which is fine for evenly-spaced series.

For a genuinely irregular series use `type: 'linear'` over **epoch
milliseconds** with a tick `callback` — as the two history charts in
`fedwatch.html` do. Reaching for a category axis instead is worse than wrong:
it spaces points evenly, so on the target-range history the 1982–90 flurry and
the 2009–15 flat line would occupy equal width.

Adding the adapter to `base.html` costs every page a script for the benefit of
one.

## `.fade-up` makes every card a permanent stacking context

`animation: fadeUp .35s ease both` — the `both` fill mode means the effect
never stops applying, so each card is a stacking context for the life of the
page, not 350 ms. An absolutely-positioned menu inside card A resolves its
`z-index` *within card A*, and card B further down paints over it however high
the number goes.

The fix lifts the **ancestor card** (`[data-sel-raise].ag-raised`, toggled in
the listbox's own open/close so a closed control leaves the sticky sub-header
on top) — not the menu. On `/agents` the model and thinking menus opened,
rendered, looked correct, and silently swallowed every click.

Note the status filter never hit this only because it lives in the last card on
the page — "the existing dropdown works" is not evidence that a new one will.

## i18n — EN + ZH, and the parts `apply()` cannot reach

`static/i18n.js` per app (yStocker's is 3,600 lines). Markup is declarative:

```html
<span data-i18n="nav.dca">DCA</span>
<input data-i18n-placeholder="lookup.ph">
<button data-i18n-title="nav.refresh_body">
```

`I18n.apply(root)` walks `[data-i18n*]` and must be called after **any dynamic
DOM insertion**. `I18n.toggle()` switches language and dispatches
`i18n:langchange` on `document`.

Three places `apply()` structurally cannot reach, each with its own escape
hatch:

- **Chart labels and axis titles** live in a Chart.js config, not the DOM →
  `I18n.label('chart.peg', 'PEG')` / `I18n.axis('chart.axis_fwd_pe', 'Forward PE')`,
  refreshed by the page's own `retranslateCharts()` on `i18n:langchange`.
- **Strings composed in JS by concatenation** → `I18n.t(key)`. Every band,
  model, factor and refusal name on `/dca` is this shape, which is why
  `tests/test_dca.py` asserts each key exists in **both** languages.
- **The server-rendered `<title>`** → `<meta name="i18n-title">` in the page's
  `title_key` block, plus `data-brand` (a server-side value the client cannot
  otherwise know).

Anything set from JS — a pill's text, a status line — must re-read its labels
on `i18n:langchange` as well, or an English string ends up on a Chinese page.

**A translation may never be the empty string.** Every lookup is spelled
`I18n.t(key) || fallback`, and `''` is falsy in JavaScript — so an empty `zh`
renders as the **English fallback**. Caught live: a page printed
`可用权重 40%，最低要求 50% minimum`. The fix is structural (one key carrying
both placeholders), not a better empty value.
`tests/test_i18n_completeness.py` guards this and that both languages exist.

## The four hand-rolled JS modules

| module | loaded by | purpose |
|---|---|---|
| `deferload.js` | blocking, every yStocker page | run a panel's fetch when it nears the viewport |
| `autorefresh.js` | `/housing` only | reload a tab left open for days |
| `pulltorefresh.js` | end of `base.html`, most pages | the pull gesture in the installed PWA |
| `sw.js` | service worker | offline fallback |

### `DeferLoad.when(anchor, loader)`

Loaded **blocking** because pages call it during their initial parse.

**A hidden anchor defers nothing, and says so only in the console.**
`IntersectionObserver` can never fire for an element with no box, so deferload
detects that and runs the loader *immediately* — the panel still fills, which
is exactly why this ships. The page looks lazy while fetching everything on
load.

Nearly every card in `history.html` and `fed.html` is `style="display:none"`
until its own loader reveals it, so **the obvious id is usually the wrong
anchor**. Use the card's visible loading placeholder (`#forecastLoading`,
`#peLoading`) or the nearest element in flow from first paint. A visible anchor
inside a hidden card is equally dead.

Registration order matters: deferload waits for layout before observing, but a
`when()` called after an `await` is measured against the layout at that
instant. Register once the page has reached its real height.

Only worth applying where a page fires *several* independent requests on load.
`/tv` must never use it — a kiosk nobody scrolls, whose `opacity:0` slides all
intersect anyway.

Guards: `tests/test_deferload_anchors.py` (call sites, no browser) +
`node tests/check_deferload.mjs` (the module).

### `AutoRefresh.watch({meta, stamp})`

The server refreshes itself; the *browser* never learned. A tab opened Tuesday
still rendered Tuesday's payload on Thursday under a header reading "Data as of
Tuesday". Nothing was broken and nothing said so.

Four load-bearing decisions:

- **Polls a 43-byte stamp endpoint, not the payload.** `/api/housing` is ~100 KB
  gzipped; polling it costs ~17 MB/day per open tab for data that changes once
  a day. `/api/housing/meta` touches no upstream and **never 500s** — a stamp
  that cannot be read is indistinguishable from "nothing new".
- **Reloads rather than re-rendering.** A partial in-place update on a numbers
  dashboard is worse than none: a KPI tile showing today above a chart showing
  yesterday is wrong without looking wrong.
- **Three guards against a reload loop**, because under `--preload` two workers
  can disagree about the stamp: strictly-greater comparison, the reloaded-for
  stamp recorded in `sessionStorage` (it has to survive the very reload it
  authorises), and `MIN_GAP_MS` (5 min).
- **An interaction in the last 60s turns the reload into a dismissible offer.**
  The automatic path is reserved for a tab being *returned to* — which is why
  the `visibilitychange` check matters more than the 30-minute timer (hidden
  tabs throttle to ~1/min).

Guard: `node tests/check_autorefresh.mjs` (35 assertions, no browser).

### `pulltorefresh.js`

`location.reload()` — deliberately **not** what the header's `↻ Refresh` button
does. That button hits a per-endpoint route that purges the server cache and
re-fetches from Yahoo/FRED/SEC, and is cooldown-gated to 10 minutes because it
costs real upstream calls. Wrong thing to wire to a gesture an overscroll can
trigger by accident. The strings differ for the same reason (`ptr.*` promises
less than `nav.refresh_body`).

**It stacks with the browser's native gesture unless suppressed.** Chrome on
Android and iOS standalone already have it, so a hand-rolled one fires twice.
The module injects `html { overscroll-behavior-y: contain }` **from
JavaScript**, so a script that fails to load leaves the native gesture intact
rather than removing it with nothing in its place.

Offline it **declines** rather than reloads — a reload would hand the
navigation to `sw.js`, whose `networkFirst` falls through to `offline.html`,
swapping a stale-but-usable page for one that is not. `navigator.onLine` is
trusted only in the `=== false` direction, and the verdict is re-read at
release, not carried from the start of the pull.

Excluded: `/agents`, `/login`, `/contact` (a reload destroys typed input), the
embedded `/agents` iframe, and `/tv` (which does not extend `base.html`, so it
is excluded for free).

The non-passive `touchmove` this needs costs the browser its fast scroll path,
so it is bound **per gesture** on a touch starting at `scrollTop === 0`, not
for the page's lifetime.

Guard: `node tests/check_pulltorefresh.mjs` (56 assertions).

### `sw.js` + PWA

`cacheFirst` for static assets, `networkFirst` for navigations falling through
to `/static/offline.html`. `manifest.json` sets `"display": "standalone"` —
which is why pull-to-refresh matters: there is no address bar or reload button.

A manifest whose icon `src` 404s throws nothing; the browser silently declines
to offer installation and iOS uses a screenshot of the page instead.
`tests/test_pwa_assets.py` asserts every referenced path resolves on disk.

## Markup traps

- **Nested `<button>`** breaks DOM structure — browsers auto-close the outer
  one, so sibling sections escape their parent container. Use `<div>`/`<span>`
  for clickable elements inside a button.
- **A duplicate element id is invalid HTML that does not fail.**
  `getElementById` returns the first match, so two renderers silently fight over
  one element and whichever runs last wins. The symptom is a correct-looking
  value that is not the one the code beside it computed. Caught live when a new
  balance-sheet card reused `statCurrentRatio`. Guard:
  `tests/test_template_ids.py`.
- **`type="number"` measures `step` from `min`, not zero.** `min="0.01"
  step="0.1"` permits only 0.01, 0.11, 0.21 … — every whole number is a
  `stepMismatch`. The browser runs constraint validation *before* dispatching
  `submit`, so the listener never runs: no request, no console line, just a
  native bubble. The `/assets` Limits form shipped exactly this and could not
  save a whole number while the server-side check went on proving it stored
  `8.0` perfectly. Guard: `tests/test_number_input_steps.py`.

## Before you commit a front-end change

```bash
source venv/bin/activate
python -m unittest discover -s tests          # includes every static guard above
for f in tests/check_*.mjs; do node "$f"; done
bash build_css.sh                             # only if you added a Tailwind class
```

See `ystocker-testing` for what the expected failures are (there are 4, and 3
of them are one broken Homebrew dependency, not your change).
