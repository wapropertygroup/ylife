# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask monorepo hosting 8 web apps for the Li family at **li-family.us**:

| App | Dir | Dev Port | Prod Port | URL | Storage |
|-----|-----|----------|-----------|-----|---------|
| **yStocker** | `ystocker/` | 5000 | 8000 | stock.li-family.us | JSON cache files |
| **yPlanner** | `yplanner/` | 5001 | 8001 | planner.li-family.us | DynamoDB |
| **yPlanter** | `yplanter/` | 5002 | 8002 | planter.li-family.us | DynamoDB |
| **yHome** | `yhome/` | 5003 | 8003 | home.li-family.us | None |
| **yTracker** | `ytracker/` | 5004 | 8004 | tracker.li-family.us | DynamoDB |
| **yPay** | `ypay/` | 5005 | 8005 | pay.li-family.us | None (Stripe API) |
| **yImage** | `yimage/` | 5006 | 8006 | image.li-family.us | None |
| **yBG** | `ybg/` | 5007 | 8007 | ybackground.li-family.us | None (Checkr API) |

`tv.li-family.us` is a ninth nginx vhost with no app behind it: a 302 to
`stock.li-family.us/tv`, the TV dashboard. It exists because 15 characters is
typeable on a television remote's on-screen keyboard and 21 is not. Note the apex
`li-family.us` is **not** this box — it resolves to GitHub Pages
(`papersboys.github.io`), so only subdomains can be pointed here. DNS is at
Squarespace, not Route53.

`trade-agents.com` is a tenth vhost, also with no app of its own, and unlike
`tv.` it **proxies** rather than redirects: `/` maps to `/agents` on ystocker:8000
and every other path passes through, so the domain stays in the address bar. That
is the whole point of owning the name, and it does mean the rest of yStocker is
reachable under it too — accepted deliberately, since filtering paths in nginx
would be a second routing table to keep in step with `routes.py`.

Being a separate registrable domain, its **apex can point here** (`35.155.14.61`),
which `li-family.us` cannot. Two things live outside this repo and will not work
until they are done by hand:

- **DNS at Squarespace** — the apex A records must move off Squarespace's website
  IPs (`198.185.159.144/145`, `198.49.23.144/145`) to the box. Doing so replaces
  whatever Squarespace serves on that domain. Until then the vhost is inert and
  `certbot` cannot pass its HTTP-01 challenge, so the cert call is written to fail
  non-fatally and `--allow-subset-of-names` is set, because `www` is a CNAME to
  Squarespace and demanding both names would fail the apex too.
- **Google OAuth origin** — `/agents` is sign-in gated, so `https://trade-agents.com`
  must be added to the authorized JavaScript origins of `GOOGLE_CLIENT_ID` or the
  sign-in button fails silently and the page is decorative.

`pay.trade-agents.com` is an eleventh vhost fronting **ypay on 8005** — the same
app as `pay.li-family.us`. It exists for brand continuity at the one moment it
matters: a buyer who started on trade-agents.com should not be shown an
unfamiliar domain while being asked for card details. It needs its own A record;
until then the vhost is inert and its certbot call fails non-fatally.

Nothing about the payment differs. ypay builds its Stripe success and cancel URLs
from `request.host_url`, so it follows whichever host serves it with **no
Stripe-side configuration**. The buyer handoff is by email in the query string,
not a shared session — `SESSION_COOKIE_DOMAIN` is unset in both apps, so the
session does not even cross `stock.` to `pay.`, which is why `credits.summary()`
appends `?email=` and `?next=`. Without the address ypay hides the run packs
entirely, so a bare link led to a page with nothing to buy.

`AGENTS_PAY_URL` still overrides everything for staging, but it is read once at
import and so is process-global — which is exactly why the per-brand mapping is a
dict in `credits.py` rather than a second env var.

The agents quotas in `quota.py` are per box, not per domain, so a second hostname
adds no new billing exposure — but the 60/day global ceiling is shared with
whatever traffic the new name attracts.

## Commands

### Run locally
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements_stocker.txt   # or requirements_{planner,planter,tracker,home}.txt
python run/run_stocker.py                 # starts on http://127.0.0.1:5000
```

### Deploy
```bash
bash deploy/deploy.sh              # ship code: both repos, restart all 8, health check
bash deploy/deploy.sh --ystocker   # skip TradingAgents
bash deploy/deploy.sh --check      # report what is deployed, change nothing
bash deploy/deploy.sh --full       # also converge the box; needs -i key.pem
```

The default path is code-only and runs over SSM, so it needs no SSH key — `fetch`
+ `reset --hard` both checkouts, restart, health-check, about a minute. `--full`
additionally converges the machine over SSH (CloudFormation, pip, systemd units,
nginx, certbot, swap, CJK font) and needs a `.pem`; use it for a new box or after
editing a unit file, not to ship code.

`/opt/tradingagents` tracks **15th-Ave-NE/TradingAgents** (our fork), not
TauricResearch; `deploy.sh` and `install-tradingagents.sh` repoint that one checkout
if they still find the old remote. They do **not** rewrite ystocker's remote — a
mismatch there is reported, since silently rewriting a working remote can break it
(an SSH deploy-key URL turned into HTTPS the box has no credentials for).

After resetting, the deploy re-reads `main` straight from the remote with
`ls-remote` and aborts before restarting if the checkout does not match it. The
previous `git fetch origin && git reset --hard origin/main` chained the two on
`&&`, so a failed fetch skipped the reset, printed the *old* commit and still
exited 0 — a deploy that reported success while shipping nothing. It also prints
`already at latest` or `updated to <sha>` plus the new commits, so "nothing
happened" and "nothing needed to happen" are distinguishable.

Before deploying, `deploy.sh` warns about uncommitted or unpushed work, since
`reset --hard` takes what GitHub has and would otherwise appear to ship a commit
still sitting on the laptop. That check asks the remote for its `main` SHA with
`ls-remote` on the URL rather than reading a local `<remote>/main` ref, so it does
not care what the clone happens to name its remotes.

That naming used to matter and was a trap: the local TradingAgents clone called
TauricResearch `origin` and our fork `upstream`, so `main` tracked *upstream of
record* and a bare `git push` from `main` aimed at TauricResearch. They are now
swapped to the usual convention — `origin` is the fork, `upstream` is what it was
forked from — so a bare push goes somewhere harmless. `deploy.sh` was already
URL-matching rather than name-matching, which is why the swap needed no change
there.

All 8 apps get a full `systemctl restart`, **not** `kill -HUP`. HUP looks like a
graceful reload but under `--preload` it ships stale code: gunicorn's HUP handler
re-reads the *config file* only, and the WSGI app was imported once by the master
at `ExecStart`, so HUP re-forks workers from that same module state. Verified on the
box — a route added to `yhome/routes.py` returned 404 after HUP and 200 after
restart. Templates *do* refresh (a forked worker starts with an empty Jinja cache),
which is what made this easy to miss for so long. ystocker must not be HUPed for a
second reason: its background threads live in the master, and HUP cannot stop the
threads a previous import started.

Restarting is safe mid-analysis: the unit sets `KillMode=process`, so systemd
signals only gunicorn's master and the detached TradingAgents child survives.
Before that, every deploy killed in-flight runs and threw away the API spend.

TradingAgents used to be installed as a pinned TauricResearch commit plus
`deploy/tradingagents.patch`, and later as a `git am` series in
`deploy/tradingagents/`. Both are deleted — the pin had drifted so far behind that
a freshly provisioned box would have come up missing the A-share vendor, the Gemini
fallback, the progress callback and the indicator race fix, while reporting a
successful deploy.

<details><summary>Lower-level alternative</summary>

```bash
# One app, by hand
aws ssm send-command --instance-ids i-059a024daff6bd015 --region us-west-2 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["cd /opt/ystocker && sudo git fetch origin && sudo git reset --hard origin/main && sudo systemctl restart yplanner"]}'

# Read a command result
aws ssm get-command-invocation --command-id <CMD_ID> --instance-id i-059a024daff6bd015 \
  --region us-west-2 --query "[Status, StandardOutputContent]" --output text
```
</details>

### Sync secrets
```bash
bash deploy/sync-ssm.sh          # reads .env, writes to SSM Parameter Store
bash deploy/sync-ssm.sh --dry-run
```

## Architecture

### App structure
Each app follows the same pattern:
- `{app}/__init__.py` — Flask factory (`create_app()`) + SSM secret loading
- `{app}/routes.py` — Blueprint with all routes and API endpoints
- `{app}/templates/` — Jinja2 templates extending `base.html`
- `{app}/static/` — CSS, `i18n.js` (EN + ZH translations), favicon
- `run/run_{app}.py` — Dev entry point (adds project root to `sys.path`)

### yStocker-specific modules
- `data.py` — Yahoo Finance fetching (`fetch_ticker_data`, `FetchError`)
- `fed.py` — Federal Reserve H.4.1 from FRED (no API key needed)
- `sec13f.py` — SEC EDGAR 13F institutional holdings (22 funds tracked)
- `forecast.py` — Prophet / ARIMA / Linear price forecasting
- `charts.py` — Matplotlib/Seaborn → base64 PNG (server-side, no disk I/O)
- `heatmap_meta.py` — Static S&P 500 metadata for market heatmap tile sizing
- `fetchguard.py` — Shared outbound-HTTP resilience: per-provider circuit
  breakers (`guard`/`trip`/`request`) and `FailureBackoff`, a persisted per-item
  exponential back-off. Every vendor call (Yahoo, FRED, SEC EDGAR, OpenFIGI)
  goes through it; each has its own breaker so one vendor's 429 cannot stall the
  others.
- `freshness.py` — Separates *cache age* (`describe_age`) from *market-hours
  staleness* (`classify_quote`: realtime / session_close / stale) from *upstream
  death* (`series_health`, which infers a series' publication cadence from its
  own dates and flags one that has stopped)
- `brief.py` — The AI Markets Brief: formats all eight dashboards into one
  prompt and asks for a sectioned, table-per-section daily brief

### The AI Markets Brief (`/api/market-brief`)

The card at the top of `/markets` is generated from **every** dashboard —
`/markets`, `/evaluation`, `/commodities`, `/13f`, `/fedwatch`, `/housing`,
`/multiples`, `/fed` — collected server-side by `routes._collect_brief_sources()`
and formatted by `brief.py`. Output is Markdown, rendered through the shared
`static/markdown.js`, because the point of the brief is a table per section.

It is deliberately **not** a mode of `/api/daily-summary`. That endpoint still
feeds `/daily` and the subscriber email, and `_build_email_sections` splits its
text on blank lines into `<p>` tags — so a brief containing pipe tables would
arrive as literal pipes in somebody's inbox. Two consumers, two shapes, two
routes, two DynamoDB key namespaces (`{lang}_brief_v1` vs `{lang}_{market}`).

Three rules hold the thing together, and breaking any of them degrades quietly:

- **A cold source is stated, never dropped.** Each section emits an explicit
  `DATA UNAVAILABLE` line, and the prompt turns that into one italic sentence.
  Omitting the section instead reads to the model as "nothing to say about
  housing" and invites it to answer from training data — in a dated, numeric
  brief an invented number is much worse than an admitted hole.
- **The request path peeks caches and never rebuilds.** `_collect_brief_sources()`
  only fetches when passed `warm=True`, which only the overnight pre-generator
  does (and which needs the `app`, since these are Flask views). `api_markets()`
  on a cold cache measured 120s and got the worker SIGKILLed — see the note on
  `_tv_markets_cached`. The Gemini call is likewise capped at 90s, under
  gunicorn's `--timeout 120`, because a killed worker takes every other request
  it was serving with it.
- **Stale beats absent, but must be labelled.** The four cached modules expose
  `peek()` (mirroring `breadth.peek()`) which ignores TTL but still honours
  `_CACHE_VER`. Gating on `is_cache_fresh()` dropped whole sections whenever a
  nightly refresh ran late, on monthly series where a day changes nothing.
  `_stale` carries the names into the snapshot so the model dates them.

The version suffix in the DynamoDB key is load-bearing: bump `_BRIEF_KEY_VER`
whenever the brief's shape changes, or today's already-stored copy is served all
day and the change looks like it did nothing.

Tests: `tests/test_brief_formatters.py` (60 unit tests, no app/network — this is
the one that catches the real risk, which is key names and units, since the eight
payloads use `day_chg` vs `day_chg_pct`, `ytd` vs `ret_ytd`, `pct` vs `pct_dec`).
The `tests/check_brief_*.py` scripts are diagnostics that need live caches, a
Flask app, or a Gemini key, and are named `check_` so `unittest discover` skips
them; `check_brief_live.py` does one real generation and prints it.

### Caching (yStocker)
Two-tier: in-memory dict + on-disk JSON in `cache/`. All cache access guarded by `threading.Lock`. Disk writes use atomic temp file + `os.replace()`.

| Cache | TTL | File |
|-------|-----|------|
| Stock metrics | 8 hours | `cache/ticker_cache.json` |
| Fed balance sheet | 24 hours | `cache/fed_cache.json` |
| 13F holdings | 24 hours | `cache/sec13f_cache.json` |
| Peer groups | persistent | `cache/peer_groups.json` |

**Observed series are not cache.** The forward-P/E snapshots accumulate one row
per day and cannot be recomputed from anything, so they live in DynamoDB
(`ystocker-valuation-history`) as well as in `cache/valuation_cache.json` — the
same pattern `ystocker-fear-greed` and `ystocker-pcr-history` already use. Keeping
such a series only in a cache file loses it whenever the EC2 instance is replaced,
which is exactly how the SPY/QQQ chart reset to a single point.

### Background threads (yStocker)
Started in `create_app()`, all daemon threads:
- Stock cache warming (every 8h)
- 13F holdings refresh (every 24h)
- Heatmap daily snapshot (weekdays 16:30 ET)
- Daily email broadcast (UTC 00:00)

### Frontend
- **Tailwind CSS** via CDN (`<script src="https://cdn.tailwindcss.com">`)
- **Alpine.js** for yPlanner interactivity
- **Chart.js 4** for yStocker charts
- **Google Maps API** for yPlanner
- **i18n**: Each app has `static/i18n.js` with EN + ZH translations, toggled via `I18n.toggle()`

### Auth
- yPlanner/yTracker: Google Sign-In + Apple Sign-In → Flask session → DynamoDB users table
- yStocker/yPlanter/yHome: Public, no auth

### Secrets flow
1. `_load_secrets_from_ssm()` in each app's `__init__.py` tries AWS SSM first
2. Falls back to `python-dotenv` loading `.env` from project root
3. Key secrets: `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, `GOOGLE_CLIENT_ID`, `YOUTUBE_API_KEY`, `SES_FROM_EMAIL`

## Production

### Infrastructure
- **Region**: us-west-2
- **EC2 Instance**: `i-059a024daff6bd015` (Amazon Linux 2023, `t3.medium`)
- **App directory**: `/opt/ystocker`
- **Process model**: nginx → 8 Gunicorn systemd services (ports 8000-8007, 2 workers each, `--preload`, recycled every ~200 requests)
- **Memory budget**: 4 GB total + 2 GB swap. ystocker runs ~1 GB (`MemoryMax=1800M`); the other seven ~100 MB each (`MemoryMax=400M`). With `--preload`, `create_app()` runs once in the master, so the background refresh threads live **only in the master** — forked workers inherit a cache snapshot and refill on demand.
- **SSL**: Let's Encrypt via certbot

### Deployment flow
`deploy/deploy.sh` SSHs to EC2 and: git pull → pip install → restart systemd services → nginx reload → certbot SSL → health check curl. Alternative: use SSM `send-command` (see commands above).

## Code Conventions

- Python 3.12+ with `from __future__ import annotations`
- Modern type hints: `dict[str, list[str]]` not `Dict[str, List[str]]`
- All modules have docstrings and structured logging (`logging.getLogger(__name__)`)
- Private helpers prefixed with `_`
- No bare `except:` — always catch specific exceptions
- Templates extend `base.html` with Tailwind CSS dark mode (`class="dark"`)

## Known Pitfalls

- **`kill -HUP` does not reload code under `--preload`.** Gunicorn's HUP handler re-reads the config file, not the WSGI app, which the master imported once at `ExecStart`. Workers re-fork from the old module state, so new Python never runs — while templates *do* refresh, because a fresh worker has an empty Jinja cache. The deploy one-liner in this file used HUP for a long time and was therefore shipping stale code. Use `systemctl restart`.
- **Nested `<button>` elements** break DOM structure in templates — browsers auto-close the outer button, causing sibling sections to escape their parent container. Always use `<div>` or `<span>` for clickable elements inside buttons.
- **`routes.py` is monolithic** (5200+ lines in yStocker) — all routes, API endpoints, cache logic, and background tasks in one file.
- **Google Maps API** on yPlanner requires a valid billing-enabled API key; errors show "Oops! Something went wrong" with a purple stripe.
- **SSH deploy** requires a `.pem` key file; the `id_ed25519` key on this machine doesn't have EC2 access. Use SSM `send-command` instead.
- **Never fit ML models in a request process.** Prophet (cmdstanpy) and `pmdarima.auto_arima` each retain hundreds of MB that glibc never returns to the OS, so a worker that served one `/api/forecast` request stayed ~880 MB larger for life. Ten such requests caused nine OOM kills in 48 h, and since the kernel picks its OOM victim globally they took *other* apps down too. `forecast.py` now runs fits in a `subprocess` (`python -m ystocker.forecast <TICKER> <OUT>`) via `run_forecast_isolated()`. Not `multiprocessing`: `fork` would inherit held cache locks from the background threads, and `spawn` re-imports the parent's `__main__` — which under gunicorn is the venv launcher script.
- **Dead FRED series return HTTP 200.** `MBST` and `WASDRAL` still serve well-formed CSV years after they stopped publishing, so stale data flows in silently and corrupts anything derived from it. Prefer the Wednesday-level `WSHO*` ids. The row-count-against-`WALCL` check this file used to prescribe was never implemented and would have needed a hand-maintained expectation per series; `freshness.series_health()` now does it generically instead, inferring each series' cadence from its own observation dates and flagging a trailing gap of more than `cadence * 3 + 7` days. `/api/fed`, `/api/housing` and `/api/multiples` ship the verdict as `meta.series` + `meta.stale_series`. It is deliberately biased toward flagging — a false positive costs one log line, and this failure went unnoticed for years. Tune with `FRESHNESS_CADENCE_TOLERANCE` / `FRESHNESS_CADENCE_GRACE_DAYS`; note a `stale` of `None` means "too few observations to tell", which is not the same as healthy.
- **Never let an outbound call run without a timeout.** `yf.Ticker(t).info` had none, and in a daemon thread that means block forever with nothing in the log — the cache warmer could park indefinitely. Yahoo now gets a `curl_cffi` session carrying one, which **must** be curl_cffi rather than `requests`: yfinance ≥1.x asserts the session type and needs Chrome TLS impersonation, so a `requests.Session` raises `YFDataException` and passing nothing leaves the timeout unset. One module-level session is reused because `YfData` is a singleton that re-binds whatever it is given, so a session per ticker would thrash the cookie/crumb it just negotiated.
- **Not every cache in `routes.py` is keyed `"data"`.** Most are `CACHE["data"] = {"ts", "data"}`, but `_CREDIT_SPREAD_CACHE` is keyed by *period* (`"1y"`, `"2y"`, …) and `_YIELD_CURVE_CACHE` by its schema version (`_YIELD_CURVE_CACHE_VER`). Reading the wrong key returns `None` rather than raising, so the consumer just silently loses a section — `/api/daily-summary` read `_CREDIT_SPREAD_CACHE.get("data")` from the day it was written, which meant its credit-spread line never once appeared in a summary. Check the write site for the key before peeking a cache, and hold its lock.
- **reportlab fails loudly on height and silently on width.** A flowable taller than the frame raises `LayoutError` and kills the whole PDF (a single-cell `Table` cannot split between rows — pass `splitInRow=1`); a flowable *wider* than the frame is simply drawn through the margin, or off the paper. So every fixed-width flowable in `report_pdf.py` is clamped to the measure, and preformatted text is hard-wrapped before it is handed over. Separately, the CJK line breaker deliberately overruns the measure by up to one em rather than start a line with `、` or `。`, which is why the Chinese path lays out to a slightly narrower measure and leaves a gutter for that overhang.
