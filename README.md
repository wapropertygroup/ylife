# Li Family Apps

A Flask monorepo hosting eight web apps at **li-family.us**, plus two bonus
domains that front pieces of the flagship app under a different brand. The
flagship, **yStocker**, is a stock-research dashboard that has grown a second
identity: `/agents` runs a multi-agent AI equity-research framework
([TradingAgents](https://github.com/15th-Ave-NE/TradingAgents), our fork of
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents))
against any ticker, including mainland China A-shares.

> The bare apex `li-family.us` is **not** this box — it resolves to GitHub
> Pages. Every app below lives on a subdomain.

| App | Dir | Dev port | Prod port | URL | Storage |
|---|---|---|---|---|---|
| **yStocker** | `ystocker/` | 5000 | 8000 | [stock.li-family.us](https://stock.li-family.us) | JSON cache files |
| **yPlanner** | `yplanner/` | 5001 | 8001 | planner.li-family.us | DynamoDB |
| **yPlanter** | `yplanter/` | 5002 | 8002 | planter.li-family.us | DynamoDB |
| **yHome** | `yhome/` | 5003 | 8003 | home.li-family.us | None |
| **yTracker** | `ytracker/` | 5004 | 8004 | tracker.li-family.us | DynamoDB |
| **yPay** | `ypay/` | 5005 | 8005 | pay.li-family.us | None (Stripe API) |
| **yImage** | `yimage/` | 5006 | 8006 | image.li-family.us | None |
| **yBG** | `ybg/` | 5007 | 8007 | ybackground.li-family.us | None (Checkr API) |

yStocker — stock research & portfolio analysis, and the multi-agent research
runner. yPlanner — AI-powered trip planner. yPlanter — Pacific Northwest
gardening guide. yHome — landing page / app directory. yTracker — multi-store
price tracker with alerts. yPay — Stripe checkout, currently selling
`/agents` run credits. yImage — a browser-based image/PDF toolkit (compress,
convert, crop, passport photos, EXIF, merge/split). yBG — tenant background
checks via Checkr.

### Two more domains

| Vhost | Behaviour |
|---|---|
| `tv.li-family.us` | 302 → `stock.li-family.us/tv`. Exists purely because 15 characters is typeable on a TV remote's on-screen keyboard and 21 is not. |
| `trade-agents.com`, `www.` | **Proxies** (not redirects) to ystocker:8000 — `/` maps to `/agents`, everything else passes through too, so the domain stays in the address bar. Its apex can point straight at the box (`35.155.14.61`) because, unlike `li-family.us`, it's a domain of its own. |
| `pay.trade-agents.com` | Fronts the **same** yPay app on 8005, for brand continuity: a buyer who started on trade-agents.com shouldn't see an unfamiliar domain while entering card details. Zero Stripe-side config — ypay builds its success/cancel URLs from `request.host_url`, so it just follows whichever host served it. |

Neither `tv.` nor the `trade-agents.com` vhosts are provisioned by
`cloudformation.yaml` — they were added by hand on the box (nginx + certbot),
which is why the two open items below are also manual:

- **`www.trade-agents.com` has no TLS certificate.** It can pass an HTTP-01
  challenge now, so re-running the `certbot` call fixes it; nginx already
  accepts the name, it just has nothing to present over HTTPS.
- **`trade-agents.com` isn't yet in `GOOGLE_CLIENT_ID`'s authorized JavaScript
  origins**, so the `/agents` sign-in button fails silently on that domain
  until it's added in the Google Cloud console.

---

## Table of contents

- [Project structure](#project-structure)
- [Quick start](#quick-start)
- [yStocker feature tour](#ystocker-feature-tour)
- [TradingAgents: the `/agents` product](#tradingagents-the-agents-product)
- [The other seven apps](#the-other-seven-apps)
- [Pages](#pages)
- [API endpoints](#api-endpoints)
- [Caching & persistence](#caching--persistence)
- [Background threads](#background-threads-ystocker)
- [Frontend](#frontend)
- [Auth](#auth)
- [Secrets](#secrets)
- [Production infrastructure](#production-infrastructure)
- [Deploying](#deploying)
- [Testing](#testing)
- [Dependencies](#dependencies)
- [License](#license)

---

## Project structure

```
ystocker/                       <- git repo root (remote is 15th-Ave-NE/ylife —
|                                   a name from before the monorepo grew to 8 apps)
+-- run/                        <- one dev entry point per app
|   +-- run_stocker.py   run_home.py     run_planner.py  run_planter.py
|   +-- run_tracker.py   run_pay.py      run_image.py    run_bg.py
|
+-- ystocker/                   <- stock research + AI agents (the flagship app)
|   +-- __init__.py             <- Flask factory, SSM secret loading, PEER_GROUPS config
|   +-- routes.py               <- every route, JSON API, and background-job glue (5,200+ lines)
|   +-- data.py / fed.py / sec13f.py        <- Yahoo Finance / FRED / SEC EDGAR fetchers
|   +-- forecast.py             <- Prophet / ARIMA / linear forecasting, run out-of-process
|   +-- charts.py / report_charts.py / report_pdf.py  <- server-side chart + PDF rendering
|   +-- fetchguard.py           <- circuit breakers + backoff shared by every outbound vendor call
|   +-- freshness.py            <- cache age vs. market-hours staleness vs. upstream-death detection
|   +-- brief.py                <- the AI Markets Brief (/api/market-brief)
|   +-- agents.py, agent_models.py, agent_roles.py, analyst.py, research.py
|   |                           <- the /agents job runner and its model/thinking-level picker
|   +-- credits.py, quota.py    <- per-run credits, daily/global quotas, brand→pay-host map
|   +-- report_email.py, share.py           <- email a finished report / share it via a link
|   +-- portfolio.py, portfolio_csv.py, portfolio_import.py, funddata.py, lookthrough.py, assets.py
|   |                           <- the /assets tracker and its recursive 穿透 look-through
|   +-- futu.py                 <- deep-links the FuTu button on /history into the Futubull app
|   +-- breadth.py, cta.py, etf_holdings.py, etf_returns.py, fedwatch.py, housing.py,
|   |   sectors.py, valuation.py, consensus_pe.py, exposure.py, feeds.py, warmup.py
|   |                           <- one module per dashboard (market breadth, the CTA trigger
|   |                              tracker, ETF pages, Fed-funds-futures odds, housing, sector
|   |                              rotation, valuation history, exposure, news feeds, cache warm-up)
|   +-- heatmap_meta.py         <- static S&P 500 metadata for heatmap tile sizing
|   +-- templates/              <- 30+ Jinja2 templates, all extending base.html
|   +-- static/                 <- css, i18n.js, markdown.js, sw.js, manifest.json, ...
|
+-- yhome/  yplanner/  yplanter/  ytracker/  ypay/  yimage/  ybg/   <- the other seven apps
|                                (each: __init__.py, routes.py, templates/, static/)
|
+-- cache/                      <- on-disk JSON cache, auto-created (yStocker's primary store)
+-- deploy/                     <- deploy.sh, install-tradingagents.sh, sync-ssm.sh, cloudformation.yaml
+-- tests/                      <- test_*.py (unittest discover) + check_*.py (live-network diagnostics)
+-- requirements_<app>.txt      <- one dependency file per app, plus requirements_build.txt (Tailwind)
+-- CLAUDE.md                   <- the deep internals doc: architecture rationale, gotchas, the "why"
+-- LICENSE                     <- MIT
```

---

## Quick start

### 1. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

### 2. Install dependencies for the app you want to run

```bash
pip install -r requirements_stocker.txt   # or _planner / _planter / _home / _tracker / _pay / _image / _bg
```

### 3. Configure secrets (optional)

Most features work with no secrets at all. Create a `.env` file in the repo
root for the ones you want:

```
GEMINI_API_KEY=your_key_here
```

Without it, yStocker runs normally — only the AI panels (chart/Fed
explanations, the Markets Brief, `/agents`) are disabled. See
[Secrets](#secrets) below for the full per-app key list.

### 4. Run a dev server

| App | Command | URL |
|---|---|---|
| yStocker | `python run/run_stocker.py` | http://127.0.0.1:5000 |
| yPlanner | `python run/run_planner.py` | http://127.0.0.1:5001 |
| yPlanter | `python run/run_planter.py` | http://127.0.0.1:5002 |
| yHome | `python run/run_home.py` | http://127.0.0.1:5003 |
| yTracker | `python run/run_tracker.py` | http://127.0.0.1:5004 |
| yPay | `python run/run_pay.py` | http://127.0.0.1:5005 |
| yImage | `python run/run_image.py` | http://127.0.0.1:5006 |
| yBG | `python run/run_bg.py` | http://127.0.0.1:5007 |

---

## yStocker feature tour

### Peer group valuation dashboard
Forward PE, TTM PE, PEG, analyst targets, upside %, EPS growth, and market cap
for every ticker in each peer group, plus a valuation scatter (Forward PE vs.
upside) and a colour-coded heatmap. Peer groups are editable at `/groups` and
persist to `cache/peer_groups.json`.

### Single-ticker analysis (`/history/<ticker>`)
Historical PE/PEG/price charts, an options wall (aggregated call/put open
interest across expirations), institutional holders, AI chart explanation
(streamed, EN/中文), recent news with importance scoring, and — for phones
with the Futu/moomoo app installed — a deep link straight into it.

### Price forecasting (`/api/forecast/<ticker>`)
Prophet, AutoARIMA, and linear regression, 6 months out with 80% confidence
intervals. The fit itself runs in a `subprocess`, never in a gunicorn worker —
Prophet and `pmdarima` each leak hundreds of MB that the process never gives
back, so fitting in-request turned into nine OOM kills in 48 hours across
*every* app on the box before this was isolated.

### Market heatmap, broad markets, Fed, and 13F
`/heatmap` (S&P 500 by sector, sized by market cap), `/markets` (indices,
commodities, crypto, Fear & Greed, put/call ratio), `/fed` (weekly FRED H.4.1
balance-sheet series), and `/13f` (22 tracked funds' SEC EDGAR holdings) — each
with an AI explainer over the data actually on the page.

### AI Markets Brief
The card atop `/markets` is generated from *all eight* dashboards at once —
one Gemini call, one Markdown response, a table per section. A cold upstream
is stated (`DATA UNAVAILABLE`) rather than silently dropped, because an
omitted section reads to the model as "nothing to say" and invites it to
invent a number instead.

### Asset tracker & 穿透 look-through (`/assets`)
Import a broker CSV (Fidelity, Schwab, Vanguard, IBKR, Robinhood, E\*TRADE,
Futu, or a plain `symbol,quantity` template) and see not just your fund
positions but what's *inside* them, recursively — VTTSX is a fund whose
largest holding is another fund. Every figure is reported as a **floor**
(Yahoo discloses only a fund's top ten holdings) with a coverage percentage
alongside it, because a guessed number is indistinguishable from a measured
one to the reader, at exactly the moment they're making a concentration call.

### Sharing and emailing reports
A finished `/agents` report can be mailed to the account that ran it, or
shared as a link to someone with no account at all — see the dedicated
section below.

### Internationalisation & theme
Every app ships English + Simplified Chinese (`static/i18n.js`) and a
light/dark toggle that defaults to dark and flips before first paint.

---

## TradingAgents: the `/agents` product

`/agents` streams a full multi-agent equity-research report for any ticker —
Bull/Bear researcher debate, an aggressive/conservative/neutral risk debate,
and a Portfolio Manager's final call — by launching the separate
[`15th-Ave-NE/TradingAgents`](https://github.com/15th-Ave-NE/TradingAgents)
framework (our fork of `TauricResearch/TradingAgents`) as a short-lived
subprocess. It is a second product bolted onto a stock dashboard, and most of
what's interesting about yStocker's backend exists to support it safely.

### Four analysts, or seven for A-shares
Non-A-share tickers get the framework's established four analysts
(Fundamentals, Sentiment, News, Technical). Mainland China A-share codes get
seven — the four above plus **Market, Policy, Hot Money, and Lock-up** — and
all seven reports are explicit inputs to both debates, not just background
colour. A-share data is fetched keyless: mootdx/通达信 TCP when it's reachable,
falling through to Tencent/Eastmoney/Sina/同花顺 over HTTP when it isn't, with
Eastmoney calls serialized and rate-limited. Changing how yStocker *renders*
a role doesn't add an analyst to the decision graph — that lives entirely in
the paired repo, installed by `deploy/install-tradingagents.sh`.

### Choosing the model (`agent_models.py`)
The run form offers five choices — `google-pro`, `google-flash`,
`google-lite`, `deepseek-pro`, `deepseek-flash` — each naming a provider, a
deep-think and quick-think model, and the thinking levels it accepts. The
client sends this **table key**, never a raw model id: TradingAgents doesn't
validate a model id before spending the run's credit, so a typo'd id would
die inside the vendor SDK minutes in, with the reader watching a progress bar.
An unknown key quietly falls back to the deployment default (catalogs churn);
a provider with no credential is refused up front with a 400 and the quota is
refunded, because silently substituting a different vendor's model would put
a name on the report that didn't write it. The choice is frozen on the job at
submit time, so a queued run reproduces exactly what the reader picked even
if the catalog moves under it later. DeepSeek needs
`/ystocker/DEEPSEEK_API_KEY` in SSM; `AGENTS_MODEL_CHOICE=0` turns the picker
off entirely.

### Credits and quotas
Every run costs **1 credit**, flat, regardless of model — the default was
already the most expensive setting (Pro, both roles, high thinking), so no
choice costs more than the old fixed behaviour. A global daily cap
(`AGENTS_GLOBAL_DAILY_LIMIT`, 60/day) is enforced **per box**, not per
hostname, so `trade-agents.com` traffic adds no new billing exposure but does
share the ceiling. Credits are sold through yPay's Stripe checkout; there's no
shared session between `stock.` and `pay.` (`SESSION_COOKIE_DOMAIN` is unset
in both apps), so the handoff is a plain `?email=` query parameter, and
`credits.py` maps brand → pay host so a `trade-agents.com` buyer is sent to
`pay.trade-agents.com` rather than `pay.li-family.us`.

### Emailing a finished report
A deep run takes tens of minutes, so `report_email.py` emails it on
completion — closing the tab loses nothing. Completion can be detected from
two different code paths racing each other (a normal finish, and an orphan
being reaped on a poll), so the send is claimed with an `O_CREAT|O_EXCL`
sentinel file rather than a database flag, closing the read-then-write race.
Gmail silently clips long HTML mail around 102 KB; the cutoff lands on a
section boundary with a link to the rest, rather than mid-table.
`AGENTS_EMAIL_REPORT=0` disables sending.

### Sharing a report with someone who has no account
yStocker keeps no user table at all — every gate keys off the session email
at the moment it's used — so there's no notion of "share with this person."
Instead, `share.py` mints a capability token (`secrets.token_urlsafe(16)`,
30-day expiry, revocable) and `GET /agents/shared/<token>` renders that one
report to anyone holding the link, no sign-in required. That is the whole
point — the recipient is, by design, someone who never ran a report — and the
trade-off is not hidden: anyone with the link can read it, so forwarding
re-shares it. `AGENTS_SHARE=0` disables sharing; sends are capped at
`AGENTS_SHARE_DAILY_LIMIT` (20/day).

### Surviving a deploy
Every deploy does a full `systemctl restart` on all eight services, and the
unit sets `KillMode=process`, so the restart signals only gunicorn's master —
the detached TradingAgents child survives and finishes its run. Before this
was tightened, a deploy landing mid-analysis killed the run and threw away
its API spend. Restarts (not `kill -HUP`) are load-bearing for a second
reason: under `--preload`, HUP only re-reads gunicorn's *config file*, not the
already-imported app, so new code silently never runs.

### Wanting more detail
`CLAUDE.md` in this repo has the rest — byte-budget arithmetic for the email
clip, the residual-partition invariant behind `/assets`, the exact race
conditions each guard closes, and the reasoning behind every one of the
`AGENTS_*` environment flags.

---

## The other seven apps

- **yPlanner** and **yTracker** are the only two apps with real accounts —
  Google Sign-In + Apple Sign-In into a DynamoDB users table. yPlanner also
  drives the Google Maps JavaScript API, which needs a billing-enabled key or
  it fails with a generic "Oops!" and a purple stripe.
- **yPlanter** and **yHome** are public, static-ish reference apps (a
  gardening guide; the app directory landing page) with no persistent store.
- **yPay** is a thin Stripe wrapper: it currently exists to sell `/agents` run
  credits, has no database of its own, and builds every redirect URL from
  the request it's handling rather than a hardcoded host — which is what lets
  `pay.trade-agents.com` work with no Stripe-side configuration.
- **yImage** is a client-triggered image/PDF toolbox (compression, format
  conversion, cropping, passport photos, EXIF, merge/split) with no storage —
  files are processed and returned, not kept.
- **yBG** wraps the Checkr API for tenant background checks.

---

## Pages

| URL | Description |
|---|---|
| `/` | Home — sector cards, valuation scatter, PEG map, cross-sector heatmap |
| `/sector/<name>` | One peer group: PE, upside, PEG charts + data table |
| `/history/<ticker>` | PE/PEG history, options wall, holders, news, AI explainer, FuTu deep link |
| `/lookup` | Search any ticker, or discover tickers by sector/industry |
| `/groups` | Add, remove, and manage peer groups (persisted to disk) |
| `/fed` | Federal Reserve balance sheet charts + AI trend explanation |
| `/13f` | Institutional 13F holdings from 22 tracked funds |
| `/heatmap` | S&P 500 market heatmap by sector |
| `/markets` | Broad market overview + the AI Markets Brief |
| `/assets` | Your asset tracker and its 穿透 look-through |
| `/agents` | Submit and read multi-agent equity-research reports |
| `/agents/shared/<token>` | A shared report, no sign-in required |
| `/guide` | Help documentation and feature overview |
| `/videos` | Curated YouTube finance channels |
| `/refresh` | Clears the cache and triggers a background re-fetch (cooldown-gated) |

---

## API endpoints

### AI agents (`/agents`)

| Endpoint | Method | Description |
|---|---|---|
| `/api/agents/run` | POST | Submit a run (spends 1 credit, refunded on rejection) |
| `/api/agents/job/<job_id>` | GET | Poll job status / read the finished report |
| `/api/agents/job/<job_id>/pdf` | GET | Render the report as a PDF |
| `/api/agents/job/<job_id>/chat` | POST | Ask a follow-up question about a finished report |
| `/api/agents/jobs` | GET | List your own jobs |
| `/api/agents/search` | GET | Search your jobs |
| `/api/agents/share` | POST | Mint a share link for a finished job |
| `/api/agents/share/<token>/revoke` | POST | Revoke a share link |
| `/api/agents/shared/<token>` | GET | Public, unauthenticated read of a shared report |
| `/api/agents/showcase` | GET | Curated example reports (public) |

### Asset tracker (`/assets`)

| Endpoint | Method | Description |
|---|---|---|
| `/api/assets` | GET | Positions, valuation, and 穿透 roll-ups (never fetches; cache-only) |
| `/api/assets/positions` | POST | Replace all positions |
| `/api/assets/position` | POST | Add/update one position |
| `/api/assets/position/<symbol>` | DELETE/POST | Remove or adjust one position |
| `/api/assets/import` | POST | Two-phase CSV import: preview the column mapping |
| `/api/assets/template.csv` | GET | Download the plain `symbol,quantity` template |
| `/api/assets/analyze` | POST | Stream an AI risk memo over your current positions (SSE) |
| `/api/assets/policy` | GET/PUT/POST | Read/write per-user analysis preferences |

### AI brief & daily summary

| Endpoint | Method | Description |
|---|---|---|
| `/api/market-brief` | POST | The eight-dashboard AI Markets Brief |
| `/api/daily-summary` | POST | The `/daily` digest / subscriber email content |
| `/api/daily-summary/<date>/<lang>` | GET | A previously generated daily summary |

### Stock & market data

| Endpoint | Method | Description |
|---|---|---|
| `/api/cache-age` | GET | Cache metadata and age |
| `/api/ticker/<ticker>` | GET | Single ticker metrics |
| `/api/history/<ticker>` | GET | Historical PE/PEG/price data |
| `/api/history/<ticker>/explain` | POST | AI chart explanation (SSE stream) |
| `/api/financials/<ticker>` | GET | Income statement, balance sheet |
| `/api/discover` | GET | Sector/industry ticker discovery |
| `/api/forecast/<ticker>` | GET | 6-month forecast (Prophet/ARIMA/linear) |
| `/api/markets` | GET | Broad market indices |
| `/api/fear-greed` | GET | CNN Fear & Greed index |
| `/api/put-call-ratio` | GET | Options sentiment |
| `/api/gold-ratios` | GET | Precious metals ratios |
| `/api/credit-spread` | GET | Corporate credit spreads |

### News, Fed, and 13F

| Endpoint | Method | Description |
|---|---|---|
| `/api/news/<ticker>` | GET | Recent news articles |
| `/api/news/translate` | POST | AI news translation |
| `/api/videos/<ticker>` | GET | Financial videos for a ticker |
| `/api/fed` | GET | H.4.1 balance sheet data |
| `/api/fed/explain` | POST | AI Fed data explanation (SSE stream) |
| `/api/13f/<fund_slug>` | GET | Holdings for one fund |
| `/api/13f/ticker/<ticker>` | GET | Which funds own this stock |

---

## Caching & persistence

Two-tier cache: an in-memory dict plus on-disk JSON in `cache/`, guarded by
`threading.Lock`, written atomically (temp file + `os.replace()`).

| Cache | TTL | File |
|---|---|---|
| Stock metrics | 8 hours | `cache/ticker_cache.json` |
| Fed balance sheet | 24 hours | `cache/fed_cache.json` |
| 13F holdings | 24 hours | `cache/sec13f_cache.json` |
| News | 5 minutes | in-memory only |

### Observed series (DynamoDB, not cache)

A handful of series accumulate one row per day and **cannot be recomputed**
from anything upstream, so a cache-file-only copy is lost the moment the EC2
instance is replaced. These live in DynamoDB in addition to their on-disk
mirror: `ystocker-valuation-history`, `ystocker-fear-greed`,
`ystocker-pcr-history`, `ystocker-cta-history`, `ystocker-fedwatch-history` —
plus `ystocker-assets` (portfolios) and `ystocker-agent-shares` (share
tokens), which are records rather than time series but share the same "must
outlive the box" reasoning. None of the seven are in
`deploy/cloudformation.yaml` — CloudFormation can't adopt a table that
already exists, so they're created by hand once (see `CLAUDE.md` for the
exact `aws dynamodb create-table` calls).

---

## Background threads (yStocker)

Started once in `create_app()`, all daemon threads:

| Task | Frequency |
|---|---|
| Stock cache warming | Every 8 hours |
| 13F holdings refresh | Every 24 hours |
| Heatmap daily snapshot | Weekdays 16:30 ET |
| Daily email broadcast | Daily 00:00 UTC |

Under `--preload` in production, these threads live **only in the master
process** — forked workers inherit a snapshot and refill on demand, which is
also why the eight services must be `systemctl restart`ed and never `kill
-HUP`ed (see [Surviving a deploy](#surviving-a-deploy) above).

---

## Frontend

- **Tailwind CSS** via CDN for every page, plus a small compiled bundle
  (`css/tailwind.css`, rebuilt by `build_css.sh`) for the few contexts that
  can't reach the CDN, like the offline page and pull-to-refresh's
  dynamically-injected classes.
- **Alpine.js** for yPlanner's interactivity; **Chart.js 4** for every
  yStocker chart; the **Google Maps JavaScript API** for yPlanner.
- **i18n**: every app ships `static/i18n.js` with English + Simplified
  Chinese, toggled from the navbar.
- **Light/dark theme**: defaults to dark, stored per-app in `localStorage`,
  flipped on `<html>` by a blocking inline script before any stylesheet
  loads. Canvas charts get their own colour-mapping layer (`CT.c(...)` in
  `base.html`) since Chart.js takes no CSS.
- **Deferred panel loading** (`deferload.js`) and **pull-to-refresh**
  (`pulltorefresh.js`, installed-PWA-friendly, suppresses the native gesture
  it would otherwise double up with) ship on every yStocker page except a
  short exclusion list (`/agents`, `/login`, `/contact`, `/tv`).
- **Auto-refresh for a tab left open** (`autorefresh.js`) polls a 43-byte
  timestamp — not the payload — and offers a reload when a background
  refresh has produced newer data than what's on screen; currently wired up
  on `/housing`.

---

## Auth

- **yPlanner / yTracker**: Google Sign-In + Apple Sign-In → Flask session →
  DynamoDB users table.
- **yStocker / yPlanter / yHome**: public, no auth.
- yStocker itself keeps **no persisted user record** even where it gates
  behaviour (credits, quotas, `/assets` ownership, `/agents` job ownership) —
  every check is `session["user_email"]` at the moment it's used. That's a
  deliberate trade-off, not an oversight: see
  [Sharing a report](#sharing-a-report-with-someone-who-has-no-account) above
  for what it costs.

---

## Secrets

1. `_load_secrets_from_ssm()` in each app's `__init__.py` tries AWS SSM
   Parameter Store first (namespaced per app, e.g. `/ypay/STRIPE_SECRET_KEY`,
   `/ybg/CHECKR_API_KEY`, `/ystocker/DEEPSEEK_API_KEY`).
2. Falls back to `python-dotenv` loading `.env` from the project root.

| App | Key secrets |
|---|---|
| yStocker | `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` (optional, for `/agents` model choice), `GOOGLE_CLIENT_ID`, `YOUTUBE_API_KEY`, `SES_FROM_EMAIL` |
| yPlanner | `GOOGLE_MAPS_API_KEY`, `GOOGLE_CLIENT_ID` |
| yTracker | `GOOGLE_CLIENT_ID` |
| yPay | `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` |
| yBG | `CHECKR_API_KEY` |

Run `bash deploy/sync-ssm.sh` (or `--dry-run`) to push everything from a
local `.env` up to SSM.

---

## Production infrastructure

- **Region**: us-west-2. **Instance**: `i-059a024daff6bd015`, Amazon Linux
  2023, `t3.medium`.
- **Process model**: nginx → 8 Gunicorn systemd services (ports 8000–8007, 2
  workers each, `--preload`, recycled every ~200 requests).
- **Memory budget**: 4 GB + 2 GB swap. yStocker runs ~1 GB
  (`MemoryMax=1800M`); the other seven ~100 MB each (`MemoryMax=400M`).
- **TLS**: Let's Encrypt via certbot, one certificate per domain (see the two
  known gaps under [Two more domains](#two-more-domains) above).

---

## Deploying

### Day to day

```bash
bash deploy/deploy.sh              # ship code: both repos, restart all 8, health check
bash deploy/deploy.sh --ystocker   # same, but skip the TradingAgents checkout
bash deploy/deploy.sh --check      # report what's deployed; change nothing
bash deploy/deploy.sh --full       # also converge the box itself; needs -i key.pem
```

The default path runs over **SSM**, so it needs no SSH key: `fetch` + `reset
--hard` on both the yStocker and TradingAgents checkouts, restart all eight
services, health-check — about a minute. It refuses to proceed if your local
`main` is ahead of or diverged from GitHub (`reset --hard` takes what GitHub
has), and it re-reads `main` from the remote after resetting to confirm the
checkout actually landed, rather than trusting a chained `&&` that could
report success while shipping nothing.

`--full` additionally converges the machine itself over SSH — CloudFormation,
pip, systemd units, nginx, certbot, swap, the CJK font — and needs a `.pem`.
Use it for a new box or after editing a unit file, not for ordinary code
changes.

<details><summary>One app, by hand, via SSM (no .pem needed)</summary>

```bash
aws ssm send-command --instance-ids i-059a024daff6bd015 --region us-west-2 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["cd /opt/ystocker && sudo git fetch origin && sudo git reset --hard origin/main && sudo systemctl restart yplanner"]}'

aws ssm get-command-invocation --command-id <CMD_ID> --instance-id i-059a024daff6bd015 \
  --region us-west-2 --query "[Status, StandardOutputContent]" --output text
```

</details>

### Provisioning a new box

`deploy/cloudformation.yaml` provisions the whole stack from scratch: a VPC
with a public subnet, an EC2 instance with a persistent Elastic IP, nginx +
certbot, one systemd unit per app, and an IAM role with SSM access. `KeyName`
is optional — leave it unset and manage the box entirely over SSM.

```bash
aws cloudformation deploy \
  --template-file deploy/cloudformation.yaml \
  --stack-name ystocker \
  --parameter-overrides \
      AllowedSSHCidr=$(curl -s https://checkip.amazonaws.com)/32 \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-west-2
```

Every app domain is its own parameter (`DomainYStocker`, `DomainYPlanner`, …,
`DomainYBg`), and `GitRepo` already defaults to this repo, so a fresh stack
clones and installs itself on first boot with no upload step. Outputs include
all eight app URLs plus ready-to-run `SSHCommand` and `LogsCommand` strings:

```bash
aws cloudformation describe-stacks --stack-name ystocker \
  --query "Stacks[0].Outputs" --output table
```

> **Cost note:** an Elastic IP not attached to a running instance is billed;
> deleting the stack releases it automatically.

### Local production server

```bash
pip install gunicorn
gunicorn "ystocker:create_app()" --bind 0.0.0.0:8000
```

---

## Testing

```bash
python -m unittest discover -s tests          # everything named test_*.py
python -m pytest tests/                        # equivalent, if pytest is installed
```

Files named `check_*.py` are excluded from `unittest discover` on purpose —
they need a live cache, a running Flask app, or a real Gemini/network call
(`tests/check_assets_endpoints.py`, `tests/check_brief_live.py`), so they're
diagnostics you run by hand, not part of CI. The `test_*.py` suite needs
neither a network nor a database: `tests/test_lookthrough.py` (the `/assets`
look-through math, including the summation invariant), `tests/
test_portfolio_csv.py` (65 real broker-export shapes), `tests/
test_agent_models.py` (43, cross-checked against TradingAgents' own model
catalog), `tests/test_report_email.py` (76), `tests/test_brief_formatters.py`
(60), and `tests/test_theme_classes.py` among them.

---

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web framework |
| `yfinance` | Stock data (prices, PE, PEG, analyst targets) from Yahoo Finance |
| `pandas` | Tabular data manipulation |
| `matplotlib` / `seaborn` | Server-side chart rendering |
| `requests` | HTTP client for FRED and SEC EDGAR |
| `google-genai` | Google Gemini API for AI explanations and `/agents` |
| `python-dotenv` | Load secrets from `.env` |
| `boto3` | AWS SSM Parameter Store and DynamoDB |
| `prophet` | Facebook/Meta time-series forecasting |
| `pmdarima` | AutoARIMA model selection |
| `statsmodels` | Statistical modelling |
| `numpy` | Numerical computing |
| `gunicorn` | Production WSGI server |

### TradingAgents runtime

The paired [`15th-Ave-NE/TradingAgents`](https://github.com/15th-Ave-NE/TradingAgents)
checkout is a separate Python environment, installed and updated by
`deploy/install-tradingagents.sh` — not a pip dependency of yStocker itself,
since it runs as a subprocess rather than an import. It keeps a modern
`httpx` (needed for Gemini) and installs `mootdx` without dependency
resolution, because `mootdx`'s published metadata pins an incompatible old
`httpx` range.

---

## License

[MIT](LICENSE)
