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
which `li-family.us` cannot. The DNS move has been done: `trade-agents.com`,
`www.trade-agents.com` and `pay.trade-agents.com` all resolve to the box, the
apex certificate issued (expires 2026-11-21), and `https://trade-agents.com/`
serves `/agents` — verified from the box, since the vhost only answers to its own
`server_name` and a local `curl` needs `--resolve`.

Two things remain:

- **`www` has no certificate.** `certbot` runs with `--allow-subset-of-names`, so
  when `www` was still a CNAME to Squarespace it dropped that name and issued for
  the apex alone; the cert's only SAN is `DNS:trade-agents.com`. Now that `www`
  points at the box, nginx accepts it (`server_name trade-agents.com
  www.trade-agents.com`) but has no cert to present, so HTTPS to `www` fails at
  the TLS handshake rather than serving anything. It can pass an HTTP-01
  challenge now, so re-running the cert call would fix it — that flag is why the
  gap is silent instead of a deploy failure.
- **Google OAuth origin** — `/agents` is sign-in gated, so `https://trade-agents.com`
  must be added to the authorized JavaScript origins of `GOOGLE_CLIENT_ID` or the
  sign-in button fails silently and the page is decorative. Not verifiable from
  the box; it lives in the Google Cloud console.

`pay.trade-agents.com` is an eleventh vhost fronting **ypay on 8005** — the same
app as `pay.li-family.us`. It exists for brand continuity at the one moment it
matters: a buyer who started on trade-agents.com should not be shown an
unfamiliar domain while being asked for card details. It has its own A record and
its own certificate (expires 2026-11-21) and serves 200.

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
- `report_email.py` — Mails a finished `/agents` report as HTML. Owns its own
  Markdown→HTML renderer (a server-side port of `static/markdown.js`, emitting
  inline styles) rather than reusing `_build_email_sections`, which splits on
  blank lines into `<p>` and would deliver a report's pipe tables as literal
  pipes.

### Emailing a finished agent report

A deep run takes tens of minutes and `/agents` only learns it finished by
polling (`pollJob`, 5s backing off to 30s), so closing the tab means nothing
tells you. `report_email.notify()` closes that gap. Browser notifications were
the other option and are not implemented: they would need a push subscription,
VAPID keys, an installed PWA on iOS, and a server-side completion hook that
outlives the worker supervising the run — email needs none of it, and the run
already knows the address because it was charged to it.

Four things hold it together:

- **Completion is detected in two places, so the claim must be atomic.**
  `agents._run()` reaches the end of a normal run, and `agents._reap()` settles
  an orphan — and `_reap` runs on *every* read in *every* worker, so two polls
  can settle the same job milliseconds apart. The guard is an `O_CREAT|O_EXCL`
  sentinel (`<job>.emailed`, `EMAIL_MARKER_SUFFIX`), not a field on the record:
  read-then-write lets both through. A failed send releases the claim so a later
  reap retries; a crash after sending does not.
- **Gmail clips at ~102 KB, silently.** `_HTML_BUDGET` stops the body at a
  *section* boundary and says so with a link, because cutting mid-section leaves
  a dangling table and reads like a short analysis rather than a truncated one.
  The Portfolio Manager's turn is **reserved out of the budget before anything
  else is placed** — it is the last section a report emits, so an in-order walk
  drops the decision and keeps seven analysts, inverting the value of the mail.
- **Only `status == "done"` is mailed**, and `_reap` only acts on `queued`/
  `running`. That is what stops a deploy from retro-mailing every historical
  report: jobs already `done` never reach the hook.
- **Styling is inline on every element.** Gmail drops a `<style>` block, so a
  stylesheet renders the whole report as unformatted text in the one client most
  readers use.

Sending is on by default once `SES_FROM_EMAIL` is set (already synced from SSM);
`AGENTS_EMAIL_REPORT=0` is the kill switch, and `AGENTS_BASE_URL` overrides the
link host, which defaults to `https://trade-agents.com` rather than
`stock.li-family.us` because that is the domain `/agents` exists to serve.
Errors are *not* mailed — only finished reports. Tests:
`tests/test_report_email.py` (52 unit tests, no app, no network, no SES).

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
same pattern `ystocker-fear-greed`, `ystocker-pcr-history`,
`ystocker-cta-history` and `ystocker-fedwatch-history` already use. Keeping such
a series only in a cache file loses it whenever the EC2 instance is replaced,
which is exactly how the SPY/QQQ chart reset to a single point.

The CTA tracker is the clearest case of "cannot be recomputed": each row records
how far the S&P sat from Goldman's trigger levels *that day*, and the triggers
change weekly with no published history, so a lost row is lost permanently even
though the index history is freely available. That table also holds the last
fetched report under the sentinel key `_latest_report` — one table rather than
two, with readers skipping any key that is not an ISO date — so a replaced box
does not forget every report the fetcher ever picked up.

`ystocker-fedwatch-history` is the same story one level subtler, and the contrast
inside `/fedwatch` is the useful part. That page draws two history charts and only
one of them is an observed series:

- **Target range history** — what the Fed actually did. Recomputable from FRED in
  full on every call, so it is *cache* and is deliberately not stored. It costs no
  extra fetch either: `_latest_fred_value()` was already downloading the whole
  DFEDTARL/DFEDTARU CSV and keeping the last row, so `_rate_change_points()` just
  parses what was being discarded, compressing ~13,000 daily observations to ~90
  change points (a policy rate is a step function; Chart.js draws the flat
  segments given `stepped`). Snapshotting this daily would start an empty chart
  and take years to rebuild what one GET already returns.
- **Expectations over time** — what the market *thought* it would do. Nothing
  upstream sells back yesterday's ZQ curve, so this is the row that is lost
  permanently if not written down.

Two details in `fedwatch.record_snapshot()` that are load-bearing. Rows are keyed
by the curve's own `as_of`, not `date.today()`: the ZQ close on a Saturday *is*
Friday's, so keying by today would write phantom Sat/Sun rows holding Friday's
numbers again. And same-date writes overwrite rather than accumulate, so the ~6
refreshes a 4-hour TTL produces converge on the latest curve — which is why this
series needs **no scheduler of its own**, unlike the 16:30 ET heatmap snapshot.
The stored `implied_rate` is absolute for a related reason: the cut/hold/hike
probabilities are relative to whatever the target range was that day, so a raw
`cut_prob` plotted across a real rate change silently changes meaning mid-line —
`base_lower`/`base_upper` travel with the row so a reader can re-base.

Reads on the request path go through `history_cached()`, not `history()`.
`/api/fedwatch/history` is public and unauthenticated and every uncached call is a
full table scan, which on `PAY_PER_REQUEST` is billed by volume scanned.

None of these five tables are in `deploy/cloudformation.yaml`, deliberately: they
already exist, and CloudFormation cannot adopt a live table without an import
operation, so adding them would break the next `--full` deploy rather than
converge it. IAM needs no change for a new one — the instance role grants
`table/ystocker-*` (`cloudformation.yaml`), which also means a missing table is
never an access error, just a silent fall back to disk-only. Create by hand,
matching the others (`date` string hash key, `PAY_PER_REQUEST`):

```bash
aws dynamodb create-table --table-name ystocker-cta-history --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=date,AttributeType=S \
  --key-schema AttributeName=date,KeyType=HASH
```

```bash
aws dynamodb create-table --table-name ystocker-cta-history --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=date,AttributeType=S \
  --key-schema AttributeName=date,KeyType=HASH
```

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
- **Deferred panel loading**: `static/deferload.js` exposes `DeferLoad.when(anchor, loader)`,
  which runs a panel's fetch when that panel nears the viewport. Loaded blocking
  from `base.html` for every yStocker page, because pages call it during their
  initial parse. Only worth applying where a page fires *several* independent
  requests on load — most dashboards are one request that renders everything, and
  `/tv` must never use it (a kiosk nobody scrolls, whose `opacity:0` slides all
  intersect anyway).
- **Pull-to-refresh**: `static/pulltorefresh.js`, loaded at the end of `base.html`
  for every yStocker page except `/agents`, `/login` and `/contact` (a reload
  there destroys typed input) and the embedded `/agents` iframe. `/tv` does not
  extend `base.html`, so the kiosk is excluded for free — it reloads on its own
  timer. It matters most in the installed PWA: `manifest.json` sets `"display":
  "standalone"`, so there is no address bar or reload button and the gesture is
  the *only* way to refresh. Tests: `node tests/check_pulltorefresh.mjs`.

  The gesture is `location.reload()` and deliberately **not** what the header's
  `↻ Refresh` button does. That button navigates to a per-endpoint refresh route
  that purges the server cache and re-fetches from Yahoo, FRED and SEC EDGAR, and
  is cooldown-gated to 10 minutes precisely because it costs real upstream calls
  — so it is the wrong thing to wire to a gesture an overscroll can trigger by
  accident. The strings differ for the same reason (`ptr.*` promises less than
  `nav.refresh_body`).

  Offline it declines rather than reloads, and says "No connection" from the
  start of the pull. A reload would hand the navigation to `sw.js`, whose
  `networkFirst` falls through to `offline.html` — so the gesture would swap a
  stale page the reader could still use for one they cannot. `navigator.onLine`
  is only trusted in the `=== false` direction; `true` merely means "attached to
  a network" and is not worth acting on. The verdict is re-read at the moment of
  release, not carried over from the start of the pull.

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
- **A `DeferLoad` anchor that is hidden defers nothing, and says so only in the console.**
  `IntersectionObserver` can never fire for an element with no box, so
  `deferload.js` detects that case and runs the loader *immediately* — the panel
  still fills, which is exactly why this is easy to ship. The page looks lazy
  while fetching everything on load. Nearly every card in `history.html` and
  `fed.html` is `style="display:none"` until its own loader reveals it, so the
  obvious id is usually the wrong anchor: use the card's visible loading
  placeholder (`#forecastLoading`, `#peLoading`), or the nearest element that is
  in flow from first paint. Same trap one level up — a visible anchor inside a
  hidden card is equally dead. `tests/test_deferload_anchors.py` checks every
  call site in the templates for all three shapes and needs no browser.
  Registration order matters too: `deferload.js` waits for layout before
  observing, but a `when()` called after an `await` is measured against the
  layout at that instant, so register once the page has reached its real height.
- **A custom pull-to-refresh stacks with the browser's own unless you suppress it.**
  Chrome on Android (and iOS standalone) already has the gesture, so a hand-rolled
  one fires *twice* — the page reloads out from under its own animation.
  `pulltorefresh.js` injects `html { overscroll-behavior-y: contain }` to take the
  native gesture off the table, and injects it *from JavaScript* so that a script
  that fails to load leaves the native gesture intact rather than removing it with
  nothing in its place. Its CSS is likewise self-contained: Tailwind here is
  **compiled** (`css/tailwind.css`, rebuild with `build_css.sh`), so a class a
  script invents is simply absent from the bundle. Note the non-passive
  `touchmove` this needs costs the browser its fast scroll path, which is why it
  is bound per-gesture on a touch that starts at `scrollTop === 0` rather than for
  the page's lifetime.
- **There is no Chart.js date adapter, so `type: 'time'` renders nothing.**
  `base.html` loads `chart.umd.min.js` alone; a time axis without an adapter
  throws inside Chart.js and leaves an empty canvas. No chart on the site uses
  one — every existing chart is a category axis, which is fine because they plot
  evenly-spaced series. For a genuinely irregular series use `type: 'linear'` over
  epoch milliseconds with a tick `callback`, as the two history charts in
  `fedwatch.html` do. Reaching for a category axis instead is worse than merely
  wrong: it spaces points evenly, so on the target-range history the 1982–90
  flurry of moves and the 2009–15 flat line would occupy equal width and the chart
  would misstate the history it exists to show. Adding the adapter to `base.html`
  would cost every page a script for the benefit of one.
- **Nested `<button>` elements** break DOM structure in templates — browsers auto-close the outer button, causing sibling sections to escape their parent container. Always use `<div>` or `<span>` for clickable elements inside buttons.
- **`routes.py` is monolithic** (5200+ lines in yStocker) — all routes, API endpoints, cache logic, and background tasks in one file.
- **Google Maps API** on yPlanner requires a valid billing-enabled API key; errors show "Oops! Something went wrong" with a purple stripe.
- **SSH deploy** requires a `.pem` key file; the `id_ed25519` key on this machine doesn't have EC2 access. Use SSM `send-command` instead.
- **Never fit ML models in a request process.** Prophet (cmdstanpy) and `pmdarima.auto_arima` each retain hundreds of MB that glibc never returns to the OS, so a worker that served one `/api/forecast` request stayed ~880 MB larger for life. Ten such requests caused nine OOM kills in 48 h, and since the kernel picks its OOM victim globally they took *other* apps down too. `forecast.py` now runs fits in a `subprocess` (`python -m ystocker.forecast <TICKER> <OUT>`) via `run_forecast_isolated()`. Not `multiprocessing`: `fork` would inherit held cache locks from the background threads, and `spawn` re-imports the parent's `__main__` — which under gunicorn is the venv launcher script.
- **Dead FRED series return HTTP 200.** `MBST` and `WASDRAL` still serve well-formed CSV years after they stopped publishing, so stale data flows in silently and corrupts anything derived from it. Prefer the Wednesday-level `WSHO*` ids. The row-count-against-`WALCL` check this file used to prescribe was never implemented and would have needed a hand-maintained expectation per series; `freshness.series_health()` now does it generically instead, inferring each series' cadence from its own observation dates and flagging a trailing gap of more than `cadence * 3 + 7` days. `/api/fed`, `/api/housing` and `/api/multiples` ship the verdict as `meta.series` + `meta.stale_series`. It is deliberately biased toward flagging — a false positive costs one log line, and this failure went unnoticed for years. Tune with `FRESHNESS_CADENCE_TOLERANCE` / `FRESHNESS_CADENCE_GRACE_DAYS`; note a `stale` of `None` means "too few observations to tell", which is not the same as healthy.
- **Never let an outbound call run without a timeout.** `yf.Ticker(t).info` had none, and in a daemon thread that means block forever with nothing in the log — the cache warmer could park indefinitely. Yahoo now gets a `curl_cffi` session carrying one, which **must** be curl_cffi rather than `requests`: yfinance ≥1.x asserts the session type and needs Chrome TLS impersonation, so a `requests.Session` raises `YFDataException` and passing nothing leaves the timeout unset. One module-level session is reused because `YfData` is a singleton that re-binds whatever it is given, so a session per ticker would thrash the cookie/crumb it just negotiated.
- **A deploy takes ystocker down for as long as its slowest in-flight request.** The unit sets `KillMode=process`, so systemd signals only gunicorn's master and then waits while workers finish what they are serving; nginx returns 502 for that whole window, and `--preload` adds a few more seconds re-importing the app before anything binds :8000 again. Measured: 2 seconds for a normal deploy, but **32 seconds** for one that landed while a worker was generating an AI brief, which is a Gemini call capped at 90s. So the outage is bounded by `_BRIEF_GEMINI_TIMEOUT_MS`, not by anything about the deploy. Pre-generating the brief keeps request-path generation rare, but if this matters, drain first or move generation off the request path entirely — do not just retry the deploy and assume the 502 was transient.
- **Not every cache in `routes.py` is keyed `"data"`.** Most are `CACHE["data"] = {"ts", "data"}`, but `_CREDIT_SPREAD_CACHE` is keyed by *period* (`"1y"`, `"2y"`, …) and `_YIELD_CURVE_CACHE` by its schema version (`_YIELD_CURVE_CACHE_VER`). Reading the wrong key returns `None` rather than raising, so the consumer just silently loses a section — `/api/daily-summary` read `_CREDIT_SPREAD_CACHE.get("data")` from the day it was written, which meant its credit-spread line never once appeared in a summary. Check the write site for the key before peeking a cache, and hold its lock.
- **reportlab fails loudly on height and silently on width.** A flowable taller than the frame raises `LayoutError` and kills the whole PDF (a single-cell `Table` cannot split between rows — pass `splitInRow=1`); a flowable *wider* than the frame is simply drawn through the margin, or off the paper. So every fixed-width flowable in `report_pdf.py` is clamped to the measure, and preformatted text is hard-wrapped before it is handed over. Separately, the CJK line breaker deliberately overruns the measure by up to one em rather than start a line with `、` or `。`, which is why the Chinese path lays out to a slightly narrower measure and leaves a gutter for that overhang.
- **A test that greps rendered HTML for `onerror=` passes on its own escaping.** `&lt;img src=x onerror=alert(1)&gt;` is inert — a string the client displays, not an element it runs — but it contains the needle, so a naive substring assertion reports a vulnerability that is not there, and (worse) an assertion written to accommodate that noise stops catching the real thing. `tests/test_report_email.py` strips `&lt;…&gt;` before checking, so it only ever asserts on *live* markup. Same trap with `href=`: it appears in escaped text too. Note also that `<a>` is deliberately absent from the inline-tag restore list in both `report_email.py` and `static/markdown.js` — honouring it would mean emitting an attribute without vetting it — so a bare inline `<a href>` in model output is shown as text unless the body *opens* with a block-level tag and takes the allowlist path.
