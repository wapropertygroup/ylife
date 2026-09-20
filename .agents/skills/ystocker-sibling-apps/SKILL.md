---
name: ystocker-sibling-apps
description: "Reference for the seven non-yStocker Flask apps in the li-family.us monorepo (/Users/yuanxili/workspace/ystocker): yPlanner (trips + Redfin houses), yPlanter (PNW gardening), yTracker (retail price tracking), yHome (the landing page), yPay (Stripe checkout), yImage (54 image/PDF tools), yBG (Checkr tenant background checks). Use this when working in yplanner/, yplanter/, ytracker/, yhome/, ypay/, yimage/ or ybg/ — they share one app skeleton but almost nothing else, and CLAUDE.md is ~99% about yStocker, so several of its per-app claims are out of date. Read it before assuming an app has no storage, no auth, no background thread, or its own SSM namespace; and before editing a shared secret path, since three of these apps read /ystocker/* parameters. For yStocker itself use `ystocker`; for the shared front-end layer, `ystocker-frontend`."
---

# The seven sibling apps

One Flask monorepo, eight apps, one nginx, one EC2 box, one `deploy.sh`. They
share a skeleton and a CSS bundle and **nothing else** — different storage,
different auth, different dependency files, wildly different size.

yStocker is ~47k lines and has its own skills. These seven total ~11k and have
essentially no prose documentation anywhere. This is it.

## The table (authoritative — from `deploy/deploy.sh`'s `APPS` array)

| App | Dir | Dev | Prod | Domain | Storage | Auth |
|---|---|---|---|---|---|---|
| yPlanner | `yplanner/` | 5001 | 8001 | planner.li-family.us | DynamoDB ×3 | Google + Apple |
| yPlanter | `yplanter/` | 5002 | 8002 | planter.li-family.us | DynamoDB ×2 (+ static module) | none |
| yHome | `yhome/` | 5003 | 8003 | home.li-family.us | none | none |
| yTracker | `ytracker/` | 5004 | 8004 | tracker.li-family.us | DynamoDB ×2 | Google |
| yPay | `ypay/` | 5005 | 8005 | pay.li-family.us | **DynamoDB ×2** + Stripe | none |
| yImage | `yimage/` | 5006 | 8006 | image.li-family.us | none | none |
| yBG | `ybg/` | 5007 | 8007 | ybackground.li-family.us | **DynamoDB ×1** + Checkr | password → session |

⚠️ **Three corrections to `CLAUDE.md`'s table**, all verified in the code:

- **yPay is not "None (Stripe API)"** — `ypay/routes.py` opens `ypay-items` and
  `ypay-payments`.
- **yBG is not "None (Checkr API)"** — `ybg/routes.py` opens `ybg-applications`.
- **Background threads are not yStocker-only** — `ytracker` starts a
  `price-checker` daemon.

And a live bug: `yhome/routes.py` links yPlanter as `plant.li-family.us`, but
nginx only serves `planter.li-family.us`.

## The shared skeleton

```
{app}/__init__.py      create_app() + _load_secrets_from_ssm()
{app}/routes.py        one Blueprint: pages, /api/*, background tasks
{app}/templates/       Jinja2 extending the app's own base.html
{app}/static/          i18n.js (EN+ZH), css/tailwind.css, favicon
run/run_{app}.py       dev entry point (adds project root to sys.path)
requirements_{app}.txt one dependency file per app
```

```bash
source venv/bin/activate
python run/run_planner.py        # http://127.0.0.1:5001
```

Each `base.html` is **per app**, not shared. The Tailwind bundle *is* shared —
built once by `build_css.sh` and copied into all seven `static/css/`. yHome is
excluded from `tailwind.config.js`'s `content` globs on purpose: it is
hand-written CSS.

Every app toggles light/dark the same way (`<app>_theme` in `localStorage`,
dark default, blocking inline script at the top of `<head>`). See
`ystocker-frontend` — the theme and i18n rules are identical here.

### Secrets: `_load_secrets_from_ssm()`, and the shared namespace trap

Identical in all six apps that have it: build an `SSM_PARAMS` dict, skip
anything already in `os.environ`, one batched `get_parameters(WithDecryption)`
with a 3 s connect/read timeout and `max_attempts=1`, swallow
`NoCredentialsError` / `ClientError`, fall back to `python-dotenv` reading the
root `.env`.

| app | parameters it reads |
|---|---|
| yPlanner | `/yplanner/GOOGLE_CLIENT_ID`, `/yplanner/GOOGLE_MAPS_API_KEY`, `/yplanner/APPLE_SERVICE_ID`, `/yplanner/YPLANNER_SECRET_KEY` |
| yPlanter | **`/ystocker/GEMINI_API_KEY`**, **`/ystocker/YOUTUBE_API_KEY`** |
| yTracker | **`/ystocker/GEMINI_API_KEY`**, **`/ystocker/SES_FROM_EMAIL`** |
| yPay | `/ypay/STRIPE_SECRET_KEY`, `/ypay/STRIPE_PUBLISHABLE_KEY`, `/ypay/STRIPE_WEBHOOK_SECRET` |
| yBG | `/ybg/CHECKR_API_KEY`, `/ybg/YBG_ADMIN_EMAIL`, `/ybg/GEMINI_API_KEY`, **`/ystocker/SES_FROM_EMAIL`** |
| yImage, yHome | **none** — no `_load_secrets_from_ssm()` at all |

⚠️ **`/ystocker/GEMINI_API_KEY` and `/ystocker/SES_FROM_EMAIL` are read by three
apps each.** Rotating or revoking one silently breaks yPlanter's plant Q&A,
yTracker's AI analysis and yBG's notification mail — none of which mention
yStocker anywhere. Adding a key to `.env` is also not enough: it needs a row in
`deploy/sync-ssm.sh`'s `get_ssm_path`, and without one the script prints
`SKIP … (no SSM mapping)` and **exits 0**.

## Per app

### yPlanner — trip planner + house search (700 lines)

Two unrelated features behind one app. Trips are saved per user and shareable;
`/houses` searches Redfin (`redfin>=0.1`) with lookup and "similar" endpoints.

The only app with **Apple Sign-In** as well as Google (`PyJWT` + `cryptography`
for the Apple identity token). Tables: `yplanner-users`, `yplanner-trips`,
`yplanner-shared-trips`.

Uses **Alpine.js** — the only app that does. Google Maps needs a
billing-enabled key; a bad one renders "Oops! Something went wrong" over a
purple stripe rather than erroring.

### yPlanter — Seattle/PNW gardening guide (731 + 1,434 lines)

**Its data is a static Python module, not a database.** `plants_db.py` is 1,434
lines of curated vegetables, herbs and yard suggestions for USDA Zone 8b. To
add a plant, edit that module — DynamoDB is only for the *derived* bits:
`yplanter-history` (AI question history) and `yplanter-translations`.

Gemini answers `/api/plant/<id>/ask` and `/api/ask`; `/api/youtube` pulls
videos. Public, no auth. `/api/prewarm` exists to warm the caches.

### yTracker — retail price tracker (1,210 + 1,167 lines)

Tracks prices across Amazon, Walmart, Uber Eats, Home Depot and more.
`scraper.py` is a per-store table of URL patterns, title selectors and image
selectors — **add a store there, not in `routes.py`**.

**It has a background thread**, and it is the only sibling app that does:
`_price_checker_loop`, a daemon started once (`_checker_started` guard),
sleeping 60 s after startup then scanning `ytracker-items` **every hour**. Two
consequences that follow from the rest of this monorepo:

- Under gunicorn `--preload` the thread lives only in the **master**, so worker
  restarts do not multiply it — but nor do workers see its results without
  re-reading the store.
- It does a **full table Scan** per cycle, billed by volume on
  `PAY_PER_REQUEST`.

Tables `ytracker-items` + `ytracker-prices`. Google Sign-In. Gemini powers
`/api/item/<store>/<id>/ai-analysis`; alt-URLs let one item be tracked across
several listings.

### yHome — the landing page (132 + 18 lines)

The smallest app by far. A static `APPS` list rendered as cards, plus `/tv`
→ a redirect. No storage, no secrets, no Tailwind (hand-written CSS), no
`i18n.js`. nginx maps **both** `home.li-family.us` and the `li-family.us`
server name to it — though the apex actually resolves to GitHub Pages, not this
box.

This is the app CLAUDE.md cites for the HUP trap: a route added to
`yhome/routes.py` returned 404 after `kill -HUP` and 200 after
`systemctl restart`.

### yPay — Stripe Checkout (496 lines)

Six routes. `/api/checkout` creates a Session, `/api/webhook` receives events,
`/api/payments` lists them. Tables `ypay-items`, `ypay-payments`.

**Success and cancel URLs are built from `request.host_url`**, which is exactly
why the app needs no Stripe-side change to serve a second brand:
`pay.trade-agents.com` fronts the same :8005 and the buyer never leaves the
domain they started on. yStocker's `credits.py` hands off by **email in the
query string**, not a shared session — `SESSION_COOKIE_DOMAIN` is unset in both
apps, so the session does not cross `stock.` to `pay.`, and without `?email=`
yPay hides the run packs entirely.

Note `STRIPE_WEBHOOK_SECRET` is per-endpoint: a new webhook URL means a new
secret in SSM.

### yImage — 54 image and PDF tools (2,137 + 2,733 lines)

The biggest sibling and structurally the simplest: **one page route + one
`POST /api/*` route per tool** — 54 page routes, 59 API routes, 56 templates —
with all the real work in `processing.py`. No storage, no secrets, no auth, no DynamoDB, no SSM. Files
are processed in memory and returned.

Its own docstring says "14 tools" and is badly out of date — count the routes,
not the comment.

Stack: `Pillow`, `PyMuPDF`, `pikepdf`, `numpy`, `qrcode[pil]`. That is a heavy
import set for a `MemoryMax=400M` unit; when adding a tool, prefer the libraries
already listed over a new dependency.

### yBG — tenant background checks (627 lines)

Four-step flow: landlord creates an application link → tenant opens
`/apply/<token>` and fills the form → the app runs a Checkr check →
landlord reviews at `/review/<app_id>`.

Auth is a **single admin password** compared against `YBG_ADMIN_EMAIL`, stored
as `session["admin_email"]` — not Google Sign-In. Table: `ybg-applications`.

`_checkr_base()` picks the **staging** API (`api.checkr-staging.com`)
automatically from the key's prefix, so a staging key cannot accidentally hit
production. With no `CHECKR_API_KEY` at all it logs a warning and returns
`None` — the flow degrades rather than erroring.

## Cross-cutting things worth knowing

- **IAM does not cover `ypay-*` or `ybg-*`.** The instance role grants
  `table/ystocker-*`, `yplanner-*`, `yplanter-*`, `ytracker-*` only. Both apps
  use the lazy-init-and-log-a-warning pattern, so an `AccessDeniedException`
  degrades **silently**. If yPay or yBG persistence appears to do nothing in
  production, check that first. See `ystocker-data`.
- **Only `ystocker-agent-jobs` is in `deploy/cloudformation.yaml`.** Every table
  these apps use was created by hand and must be recreated by hand on a new box.
- **All eight get a full `systemctl restart`, never `kill -HUP`** — under
  `--preload` HUP re-forks workers from the master's stale module state, so new
  Python never runs while templates *do* refresh, which is what makes it easy to
  miss. `deploy.sh` restarts all eight together; there is no per-app deploy
  short of an SSM one-liner.
- **Memory budget is asymmetric**: yStocker `MemoryMax=1800M`, the other seven
  `400M` each, on a 4 GB box with 2 GB swap. The kernel picks its OOM victim
  globally, so one app's leak has taken others down.
- **`deploy.sh --check`** reports what is deployed without changing anything;
  its health check only curls yStocker, yPlanner and yHome, so a broken
  yImage/yBG/yPay deploy still reports success.

## See also

- `ystocker` — deploy, restart, the box, SSM.
- `ystocker-data` — table inventory, IAM, create-table recipe.
- `ystocker-frontend` — theme + i18n, which apply identically to all eight.
