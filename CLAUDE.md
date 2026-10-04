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
`server_name` and a local `curl` needs `--resolve`. What `/agents` does with a
signed-out visitor on this host is a 302 to `/home` (see the TradeAgents wiki
section below); signed in, the bare domain is the run form.

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
from the request's host (forced to https, as `_share_base()` is), so it follows
whichever host serves it with **no
Stripe-side configuration**. The buyer handoff is by email in the query string,
not a shared session — `SESSION_COOKIE_DOMAIN` is unset in both apps, so the
session does not even cross `stock.` to `pay.`, which is why `credits.topup_url()`
appends `?email=` and `?next=`. Without the address ypay hides the run packs
entirely, so a bare link led to a page with nothing to buy.

**On that host the pay pages are TradeAgents pages** (`ypay/templates/ta/`,
chosen by host in `ypay/routes.py`; pay.li-family.us keeps yPay's, untouched).
Until 2026-09-29 a reader who pressed 充值 got yPay's donation page — "Support the
apps", a coffee, the Li Family apps — and Stripe's page called the pack
"yStocker — 28 runs". Now it is the Prepay page in the site's frame: the
masthead and footer, `wiki.css` served verbatim from ystocker's checkout at
`/ta/wiki.css` (not under `/static/`, which nginx maps to ypay's own folder on
both pay hosts), the mark from ystocker's `wiki/_macros.html` through a
`ystocker/` prefix loader, and run packs only. It is bilingual, and being
another origin it cannot read trade-agents.com's localStorage, so the Prepay and
"Buy more runs" links carry `?lang=` and `?theme=` as they are followed
(`data-carry-prefs`, handled in `_ta_masthead.html`) and every link back carries
`?lang=`. The handler reads the language from `<html lang>`: it first read
`window.I18n`, which does not exist (i18n.js's `const I18n` is script-scoped),
so every 充值 opened in English — `node tests/check_carry_prefs.mjs` runs it
against the real i18n.js. Stripe's page opens in the same language (`locale`), and each refusal
from `/api/checkout` carries a `code` the page words in both.

Three flow faults went with it. Stripe's back button now returns to the pack
page with the address (`/cancel` had nothing to buy on it). The success page
says how many runs are coming and links back to `next`, which is honoured only
over https on trade-agents.com or li-family.us, since it sits beside "payment
received". And the return URLs were http://, so Stripe sent the buyer back over
plain HTTP, address included, for nginx to redirect. `tests/test_ypay_brand.py`
(22, no network, AWS or Stripe).

Until 2026-09-29 the brand mapping reached only `credits.summary()`, which
nothing called. The run page's top-up link came from `quota._credit_info()` and
the 429 upsell, both on the bare `PAY_URL`, so a buyer on trade-agents.com was
handed to pay.li-family.us after all. Both now call `credits.pay_url()`. `next`
had also never been sent, for two reasons at once: `url_for("main.agents")`
names no endpoint (it is `main.agents_page`), and `request.url_root` reads
http:// behind nginx, so the https check ypay applies failed anyway.
`_return_to()` now forces https as `_share_base()` does. `tests/test_credits.py`
pins all three.

**The account menu on trade-agents.com shows the balance and a way to add to
it.** The balance is quoted as an amount, a run at `credits.USD_PER_CREDIT` ($1,
the smallest pack's price). That is a display rate only: nothing is charged at
it, and a run bought for less in a bigger pack still shows as $1 left. Prepay
(充值) is simply the pack page on this brand's pay host, through
`agents_topup_url()`, a Jinja global that reads no ledger. The figure comes from
`GET /api/agents/balance` each time the menu opens, not on every render, since
every page there draws the masthead. It is sign-in gated only (the allowlist
decides who may run, not who may see their own money), and `credits` is null
when the ledger cannot be read. `credits.peek_balance()` exists for that
distinction, and the menu shows "—" rather than $0: a paying reader told they
have nothing has been told their money is gone. `balance()` still answers 0,
because an unreadable ledger must fund no run. stock.li-family.us's own account
menu does not carry either yet.

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

**Every deploy that lands is released** (`deploy/release.sh`, called by both the
ship and `--full` paths after success; `--check` never releases). It tags the
deployed commit `deploy-YYYY-MM-DD-HHMM` (UTC). The tag's message is the release
notes: commits since the previous deploy tag, the TradingAgents commit, and the
health line. It then publishes a GitHub Release for every deploy tag without
one, oldest first, from the tag's own subject and body.
- **Two credentials, deliberately.** The tag needs only `git push`. The release
  needs `gh` on **github.com**, while this laptop's `gh` is logged in to
  another host (`GH_HOST`). Until `gh auth login --hostname github.com`
  the deploy prints a reminder, and the tags carry the history the releases
  will be built from.
- **Which commit.** The deployed SHA is read from the box's own verified
  checkout (an `::deployed::` line the remote body prints and the laptop filters
  out of the log), not from the laptop's idea of main.
- **When nothing is released.** A redeploy of an already-tagged commit is not a
  new release. A tag read that fails tags nothing, rather than risk a duplicate.
- **What can fail.** Tags are made with `tag.gpgSign=false`, because commits
  here are signed by an x509 helper that can wait on a prompt. The step always
  exits 0, so it cannot fail a deploy.

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
# Resolve the box by tag, never by a pinned id — see "EC2 Instance" below for why.
IID=$(aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=ystocker-instance" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)

# One app, by hand
aws ssm send-command --instance-ids "$IID" --region us-west-2 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["cd /opt/ystocker && sudo git fetch origin && sudo git reset --hard origin/main && sudo systemctl restart yplanner"]}'

# Read a command result
aws ssm get-command-invocation --command-id <CMD_ID> --instance-id "$IID" \
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
- `sec13f.py` — SEC EDGAR 13F institutional holdings (48 funds; see the 13F
  notes under Known Pitfalls before adding one)
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
- `futu.py` — Deep-links the `/history` FuTu button into the Futubull app
  (`ftnn://quote/stockDetail/<stockId>/1`), with the futunn.com page as the
  fallback. Futu routes its native quote screen by an opaque internal id, not the
  ticker, so the id is scraped from the quote page once and cached forever in
  `cache/futu_ids.json`.
- `report_email.py` — Mails a finished `/agents` report as HTML. Owns its own
  Markdown→HTML renderer (a server-side port of `static/markdown.js`, emitting
  inline styles), which the daily email now uses too.
- `daily_email.py` — The daily markets email: the 16:45 ET broadcast and the
  Send button on `/daily`. Pure (data in, strings out) and moved out of
  `routes.py`, which keeps only the SES wrappers. It is one 640px column built
  from tables, with the commentaries stacked; it used to be 1,200px wide with
  them in side-by-side cells, which could not reflow, so a phone got two ~180px
  columns. It borrows `report_email`'s palette, mark and escaping renderer, and
  is localised per language: the Chinese edition used to be dated in English,
  with English sector names. Each mail is branded for, and links to, the reader's
  own site. `/api/subscribe` records that as `site` on the subscriber row. Older
  rows fall back to `APP_BASE_URL`, then stock.li-family.us. The fallback this
  replaced was ystocker.com, which this box does not serve, so an unsubscribe
  link built on it could not have worked. Tests: `tests/test_daily_email.py`,
  and `tests/check_daily_email_endpoints.py` through the app.
- `share.py` — Sharing a finished report with somebody who did not run it: mints
  and resolves the capability tokens behind `/agents/shared/<token>`, and owns the
  recipient/note validation and the field allowlist that keeps the owner's address
  out of an unauthenticated response. Holds no mail code — the mail is
  `report_email.send_share()`, so there is one renderer.
- `subscriptions.py` — TradeAgents Pro's state: the offer, who is entitled,
  the `sub#` rows in the credits table, and the signed handoff to yPay. Stripe
  itself is `ypay/billing.py`. See "TradeAgents Pro" below.
- `wiki.py` — the registry behind `/docs` and `/research` (page list, dates,
  bilingual titles) and the `/agents` landing's roster. Pure; see "The
  TradeAgents wiki" below.
- `research_store.py` — the deep-research reports a signed-in reader generated
  on `/history/<ticker>` (`ystocker-research-reports`), owner-scoped by the key
  itself. See "Deep research on /history" below.
- `portfolio.py` / `portfolio_csv.py` / `funddata.py` / `lookthrough.py` /
  `assets.py` — the `/assets` asset tracker and its 穿透 (look-through). See the
  section below; `lookthrough.py` is pure and injectable, which is what makes the
  arithmetic testable without a cache or a network.
- `dca.py` / `dcf.py` / `dcf_store.py` / `dca_history.py` / `dca_universe.py` —
  the `/dca` valuation engine. `dca.py` and `dcf.py` are **pure** for the same
  reason `lookthrough.py` is: a DCF is mostly assumption, so the arithmetic on
  top of those assumptions is the one part that can actually be proven.
- `xbrl.py` / `statements.py` / `fundamentals.py` — the Fundamentals tab on
  `/history/<ticker>`: a company's quarterly figures from its own SEC filings
  (XBRL companyfacts), Yahoo's statement tables where SEC has none. The first two
  are pure. See "The Fundamentals tab" below.
- `directory.py` — every company listed with the SEC, for `/companies`' full
  directory: SEC's `company_tickers_exchange.json`, daily, one card per filer.
  `parse` is pure. See "The Fundamentals tab" below.
- `earnings_calendar.py` — `/earnings`: every US-listed company reporting in a
  week, with the consensus and, once reported, the surprise, from Nasdaq's
  keyless calendar API. `parse_day` and the date rules are pure. See "The
  earnings calendar" below.
- `insiders.py` — `/insiders`: open-market buys and sells by officers,
  directors and 10% owners, from each followed issuer's SEC Form 4s, with
  cluster buys. `parse_form4` and the aggregation are pure. See "Insider
  trades" below.
- `predictions.py` / `odds.py` / `odds_ai.py` / `odds_ledger.py` — `/predictions`:
  live Polymarket and Kalshi odds, the Fed decision three ways (futures vs both
  venues), and the AI read with its scored track record. `odds.py` is pure. See
  "Prediction markets" below.

### The asset tracker and 穿透 (`/assets`)

A signed-in user's own holdings, and what is actually inside them. Loading and
editing the tracker spends no Gemini budget and starts no subprocess, so there is
no allowlist, quota or credit on `/api/assets` — a request is arithmetic over a
warm cache. The separate, user-triggered `/api/assets/analyze` action is the one
exception: it streams a Gemini risk memo after the reader explicitly clicks the
AI Analysis button.

Five modules, split by what can be tested without I/O:

| Module | Job | Pure? |
|---|---|---|
| `lookthrough.py` | The recursive 穿透 engine; resolver injected | **yes** |
| `portfolio_csv.py` | Broker CSV → positions, by header-alias sniffing | **yes** |
| `funddata.py` | Per-symbol quote + fund composition cache | no (Yahoo) |
| `portfolio.py` | Per-user positions in `ystocker-assets` | no (DynamoDB) |
| `assets.py` | Valuation, roll-ups, background warming | no |

**Every figure is a floor, and that is deliberate.** Yahoo discloses a fund's top
ten holdings only — 37.6% of VOO by weight, 46.3% of QQQ, 13.0% of VXUS. The
tempting move is to assume the invisible 62% of VOO resembles the visible 38% and
gross every weight up by `1/0.376`. That is not done, for the reason `brief.py`
states a cold source instead of dropping it: a fabricated number is
indistinguishable from a measured one to the reader, and here it would be
fabricated at the exact moment they are making a concentration decision. So the
page says "at least 6.2% NVDA" and shows `coverage_pct` beside it. The floor also
happens to be the more useful quantity — concentration risk is a "have I got more
than I think" question, which a lower bound answers without inventing anything.

**The residual partition is closed by construction.** `seen + undisclosed_equity +
non_equity + unclassified + unresolved + truncated + pending == portfolio value`,
asserted directly by `tests/test_lookthrough.py`. If that ever stops holding then
every percentage on the page is wrong at once, and wrong *quietly*. Two traps it
encodes:

- `_partition()` computes the residual as `max(0, stock - visible)` and
  `1 - max(visible, stock)`, not `1 - visible`. The naive form counts a bond
  sleeve as "equity we cannot see", which on BND reports the entire fund as hidden
  stock. `stock is None` (no asset-class block at all) is a *third* answer from
  `stock == 0.0` (measured: no equities in here) — the first is `unclassified`, the
  second `non_equity`. Guessing between them invents hidden concentration.
- A child symbol that does not resolve becomes a **named leaf**, never a discard.
  `XTSLA`, a BlackRock cash sweep inside AOR, 404s at Yahoo; dropping it would
  silently shrink the portfolio total, which is the one error that makes every
  percentage wrong simultaneously.

**Recursion is not optional.** A target-date or allocation fund holds *other
funds*: VTTSX's largest holding is VSMPX at 54.15%, itself a fund whose largest
holding is NVDA at 6.40%. One level of look-through on VTTSX reports a 54%
position in something that is not a company. Depth cap is 3 with cycle detection
and a node budget; hitting any of them marks `truncated` rather than quietly
returning a partial answer that reads as complete.

**The request path never fetches.** A twenty-line portfolio of mostly funds needs
~100 Yahoo calls cold — two per fund plus one per distinct child to learn whether
*it* is a wrapper — which at `data.fetch_group`'s 0.5s spacing is a minute, and
`CLAUDE.md` already records what gunicorn's `--timeout 120` does about that. So
`/api/assets` uses `funddata.peek_resolver()`, unresolved symbols come back as
`pending` (distinct from `unresolved`, so a cold cache cannot masquerade as a claim
about the security), and `assets.kick_warm()` fills them on one background thread
while the client polls and watches coverage climb. Steady state is cheap: fund
top-tens are overwhelmingly the same few hundred megacaps, shared across users.

`pending` and `warming` are deliberately different states. A symbol can remain
pending while Yahoo's provider circuit breaker or its per-symbol failure back-off
prevents any request; only an active warm worker may set `warming=true`. A pass
that makes no progress clears its active queue, and the client replaces the
spinner with a paused explanation plus an explicit retry control. The bounded
poll loop must always render this terminal state when its attempt cap is reached —
stopping timers alone leaves the last spinner frame visible forever.

**Two axes need no caveat at all.** `asset_classes` and `sector_weightings` arrive
in the same `funds_data` call and are *already* look-through on Yahoo's side —
VTTSX's sector weights reflect the underlying companies, not "100% funds", and sum
to 100%. So the asset and sector mixes are complete where the name-level view is
partial. Sector keys are emitted in Yahoo's squashed form so the client can reuse
the `mult.comp_*` strings `/multiples` already ships; note `info["sector"]` returns
"Real Estate" while `sector_weightings` returns `realestate`, and
`assets._SECTOR_ALIASES` reconciles them — without it a directly-held REIT and a
fund's property sleeve land in two half-size buckets.

**AI analysis is opt-in and privacy-minimised.** The endpoint reloads the signed-in
user's current positions server-side rather than trusting a client-supplied
portfolio snapshot. `assets.build_ai_prompt()` sends symbols, weights, percentage
P/L, lower-bound exposures, residual coverage and aggregate mixes; it deliberately
omits e-mail, account labels, quantities, imported names and raw CSV text. The
prompt tells Gemini that company exposures are floors, that the asset-class mix is
complete, and that sector weights are percentages of classified equity. Markdown
is streamed as SSE and rendered through the shared escaping renderer.

**Yahoo mis-types some foreign equities as funds.** `005930.KQ` (Samsung) and
`000660.KQ` (SK hynix) both return `quoteType: MUTUALFUND` with no composition of
any kind, and a garbage name (`"005930.KQ,0P0000B2XZ,1"` — a Morningstar id).
`funddata._fetch` demotes a "fund" disclosing neither holdings *nor* asset classes
to a leaf, because left as a wrapper the engine walks in, finds nothing, and
buckets a correctly identified company as `unclassified`. The condition is
holdings AND asset classes, **not** sectors: BND legitimately discloses no
holdings and no sectors but does report asset classes, and must stay a fund.

**CSV import is header-alias sniffing, not a parser per broker.** One code path
serves Fidelity, Schwab, Vanguard, IBKR, Robinhood, E*TRADE, Futu and the
documented `symbol,quantity` template, and an unrecognised export degrades to a
partial mapping the user can see. Import is **two-phase** — preview then commit —
because a mis-mapped column produces a portfolio that looks entirely plausible and
is wrong, and every 穿透 percentage downstream would then be confidently
incorrect. So `ParseResult.mapping` is shown before anything is written. Traps,
all observed in real exports: a UTF-8 BOM (`utf-8-sig`); GB18030 Chinese exports,
where the wrong codec yields mojibake headings that read as "no header found";
`$1,234.56` and `(123.45)`; Schwab's quoted preamble and `Account Total` footer,
which parsed as a position would double the portfolio; Fidelity's `SPAXX**`
footnote markers and trailing disclaimer paragraphs; and cash lines with no symbol
at all, which become `$CASH` rather than being dropped.

`normalise_symbol` rewrites `BRK.B` → `BRK-B`, and the rule is narrowed to an
allowlisted share-class letter on an alphabetic root. "Any single letter after a
dot" is wrong and fails silently: `.L`/`.T`/`.F`/`.V` are London/Tokyo/Frankfurt/
TSX-Venture, so it would convert `HSBA.L` and `7203.T` — the latter being in this
repo's own Nikkei peer group — into unresolvable symbols.

Two things that are *not* hedged. Reads **fail closed and loudly**: `portfolio.load`
raises `StoreUnavailable` and the route answers 503, because returning `[]` renders
as "you have no positions" on the page whose job is to show them, and a user who
concludes their data is gone cannot tell the honest recovery (retry) from the
dishonest one (re-import, now duplicated). And there is **no silent disk
fallback** — the cache modules degrade to disk-only when a table is missing, which
is right for a cache and wrong here, since the box is replaceable. Local dev needs
a path, so a file store sits behind an explicit `ASSETS_LOCAL_STORE=1`.

Share classes (`GOOG`/`GOOGL`) are reported as an `issuer_groups` *hint* rather
than merged, because merging correctly needs a share-class map and this is name
matching, which will occasionally group two similarly-named companies. A note the
reader can check is recoverable; a silently combined row is not.

Server-emitted warnings are **coded** (`{"code": "mixed_valuation", "count": 1}`),
not prose. There is no request language in the warm thread, so a sentence composed
server-side appeared in English on a Chinese page — which is what it did before.

Tests: `tests/test_lookthrough.py` (27, incl. the summation invariant under every
input ordering), `tests/test_portfolio_csv.py` (65, real export shapes), and
`tests/check_assets_endpoints.py` (49 end-to-end through the Flask test client,
`check_` so `unittest discover` skips it — it needs an app and stubs matplotlib).

The table is **not** in `deploy/cloudformation.yaml`, matching the six
observed-series tables and for the same reason — CloudFormation cannot adopt a live
table without an import operation. IAM needs no change (`table/ystocker-*`). No
TTL: a portfolio does not expire.

```bash
aws dynamodb create-table --table-name ystocker-assets --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH
```


### The DCA Valuation Engine (`/dca/<ticker>`)

A per-ticker page that scores a company's valuation on two anchors, folds them
into one 0-100 `V` score, and turns that into a multiplier on a recurring
contribution. `V` is a *cheapness* score:

```
E_REL = Σ wf·Pf          relative: each multiple against its own history
V_REL = 100 − E_REL
V_DCF = 50 + c·(Σ ws·Vs − 50)     absolute: a discounted cash flow, see dcf.py
V     = w_DCF·V_DCF + (1 − w_DCF)·V_REL
M_valuation = 0.5 + V/100
DCA = Base × M_valuation × M_earnings × M_portfolio
```

so V=0 → 0.50x, V=50 → 1.00x, V=100 → 1.50x. Valuation sets the pace of
buying; it never answers whether to buy. It is a tab on `/history/<ticker>`
(below); the page is public, like `/history`.

**It is also a tab on `/history/<ticker>` (2026-10-04)**, and that tab is this
page itself, framed: `/dca/<t>?embed=1` renders through base.html's embedded
mode (no site chrome, wall or launcher), with its own header cut to its actions.
The tab never copies the page, so it cannot drift from it. Including the
template instead was not an option: both pages declare `const TICKER` at top
level and share ids such as `#stockName`. Inside the frame:
- **Height.** The page posts its height (`dca:height`, measured on `#dcaRoot`,
  because `<main>` is flex-1 in a body at least a screen tall) and never scrolls
  itself. /history accepts the message only from that frame and origin.
- **Links.** A peer's `/dca/X` link opens `/history/X?tab=dca` in the top
  window, other links on this site also open in the top window, and Rebuild
  keeps `embed=1` through `dca_refresh`'s redirect.
- **Indexing and language.** The framed copy carries noindex. A language switch
  on /history reloads the frame with `?lang=`.
/history's header has no DCA button: the tab replaced it. Every link to one
company's DCA goes to that tab: the overview's rows and search box, /assets'
DCA tab, the peer panel, and the Research Lab and docs. `/dca/<t>` still
serves the full page, for old links and for the tab's "Full page ↗" button.
Tests: `tests/check_dca_embed.py` (7, hermetic).

Five modules, split by what can be tested without I/O:

| Module | Job | Pure? |
|---|---|---|
| `dca.py` | Weight templates, the direction table, E→V→multipliers, the blend | **yes** |
| `dcf.py` | The absolute branch: WACC, projection, upside→`V_DCF` | **yes** |
| `listing.py` | Reconciling a quote's currency and share basis with its filer's | **yes** |
| `dcf_store.py` | Hand-entered DCF inputs (`ystocker-dca-dcf`) | validator is |
| `dca_history.py` | Reconstructing the distributions, banking the snapshots | mostly |
| `dca_universe.py` | Which tickers the overview ranks | no |

#### The two anchors, and why they are blended rather than pooled

The DCF answers "what are these cash flows worth"; the relative block answers
"what has this market paid for them before". Neither subsumes the other, so the
DCF is **not** added as a sixth percentile — each is reduced to a cheapness
score on its own terms and only then combined. `w_DCF` is ordered by how
*forecastable* the business is, not how good it is: a mega-cap compounder gets
30% because its cash flows are predictable, high-growth software 15% because
most of its value sits in a terminal value nobody can check.

**A missing `V_DCF` renormalises onto `V_REL` and is never filled with 50.**
This is the framework's own most emphatic rule and the easiest one to get wrong,
because 50 is the neutral value and substituting it feels harmless. It is not: a
stock at `V_REL=80` with `w_DCF=0.30` and a filled 50 reports 71, which renders
identically to a measured 71 while quietly dragging every score toward the
middle. `blend_v()` moves the weight instead and reports `w_dcf: 0`, so the page
says the branch is *absent* rather than neutral.

The reverse substitution is refused too, for a different reason: a `V` derived
entirely from a DCF is not on the same scale as one blended at 30%, so it would
sit in a ranked column beside scores it cannot be compared to. A missing
`V_REL` is fatal and sorts last.

**Three of the ten templates never run a DCF at all, and that is the design.**
`DCF_FORMS` names the form each template would need; only FCFF is implemented.
A bank's debt is raw material rather than financing, so enterprise value and
free cash flow are not defined the usual way (§10 says omit the standard DCF); a
REIT needs an AFFO or NAV valuation and the maintenance-versus-development capex
split no Yahoo statement exposes; a utility needs FCFE or a dividend model.
Running FCFF anyway would produce a per-share number that renders on the page
exactly like a valid one, for the companies where it is least meaningful. So
they score on `V_REL` alone and the page says which form was missing.

**Refusals are named, not inferred, and there are eleven of them** (`REFUSALS`).
Two are worth calling out because the tempting implementation is a clamp:

- `WACC ≤ g` is *not a low-confidence DCF, it is not a DCF*. The Gordon
  denominator goes negative or explodes and the per-share figure is meaningless
  rather than merely uncertain. `MIN_WACC_SPREAD` (1.5pp) refuses; just inside
  the singularity the formula returns a huge *finite* number, which is the
  dangerous case because it looks like an answer.
- The **sensitivity test is run, not reasoned about**. §13 asks whether the
  result moves violently for +0.5pp on `g`; that is measurable, so
  `growth_sensitivity()` re-values and compares rather than proxying it with the
  terminal share. The two correlate and are not the same thing.

**Two different guards stop a cyclical being valued at its peak, and only one of
them is the growth rate.** Clamping growth (`GROWTH_CEILING`) stops a peak
*rate* being extrapolated — but at the top of a semiconductor or commodity cycle
the starting *level* `fcf0` is itself the peak, and growing a peak slowly for ten
years values the company as though the peak were the new floor. `DCF_MID_CYCLE`
(semiconductor, cyclical) starts from the window mean instead, per §7 and §11.

**Scenarios come from the company's own dispersion, not a house ±20%.** Bear and
Bull are Base plus and minus the standard deviation of that company's own
year-over-year FCF growth, so a utility gets a narrow band and a foundry a wide
one — measured from the same vintages the relative percentiles are built on.
Confidence `c` is likewise derived only from things visible on the page (history
length, terminal share, band width), so every deduction is checkable. Base-only
caps `c` at 0.75 per §3, and a lone wing is dropped rather than half-used:
filling a missing Bear with the Base case narrows the band and therefore *raises*
implied confidence in exactly the situation where less is known.

**The DCF costs no extra Yahoo call.** `dcf_inputs()` reads only what `build()`
already stored. One trap in it is silent and total: `build()` copies the annual
FCF onto every quarterly TTM vintage (right for a weekly P/FCF), so including
those here would repeat one figure three or four times, making observed growth
exactly zero and the dispersion collapse — with a series that still looks the
right length. Filtering on `kind == "annual"` is the whole fix.

**The price is the reconstruction's last weekly close, not a live quote**, so
`V_DCF` and `V_REL` describe one price rather than two moments. Consequently
§13's staleness gate does **not** fire on the derived path: fair value and price
are struck at the same close, so the upside is internally consistent whatever the
date. Applying it anyway is actively incoherent — an old reconstruction would
drop the DCF for stale prices while `V_REL` went on ranking multiples built from
those *same* stale prices. The gate still applies to a stored valuation, which is
the case it was written for (`MAX_VALUATION_AGE_DAYS`, 120).

Adding `beta` to `_FORWARD_KEYS` was done **without** a `CACHE_VER` bump,
deliberately: a bump invalidates every reconstruction at once, which at the
registry cap is ~360 Yahoo reads in one sweep — the exact burst this module's
budget exists to prevent. An older payload has no beta, `wacc()` assumes 1.0 and
says so in `notes`, and the next daily rebuild fixes it. Degrading visibly for a
day beats a refetch storm on deploy.

**The override is VIP-gated on write and public on read**, which is deliberate
asymmetry: the stored values are already visible in every score the public page
renders, so hiding the inputs while publishing the output would be theatre.
Writing changes the number every visitor sees, so `quota.is_vip` guards it — in
`routes.py`, not in `dcf_store`, because a store that consults the session cannot
be tested without one. Fair values replace the model's *output*; `wacc` /
`terminal_growth` replace its *inputs* and let it run; `w_dcf` is the only
implementation of §4's dynamic down-weighting, which nothing can derive. An
overridden WACC must **replace** the derived rate, not feed into it — setting the
risk-free rate to the target and zeroing the ERP looks equivalent and is not, as
the debt weighting still applies and a requested 12% comes out at 11.9% with the
page reporting a rate the valuation did not use.

`dcf_store` **degrades** where `portfolio` fails closed, and the difference is
which silence misleads: a missing override means the derived DCF runs, which is a
complete answer, and the payload's `source` says which produced it.

`DCA_DCF=0` is the kill switch — the engine then scores exactly as it did before
the branch existed, because `blend_v` puts the whole weight on `V_REL` and the
card is not rendered.

```bash
aws dynamodb create-table --table-name ystocker-dca-dcf --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=ticker,AttributeType=S \
  --key-schema AttributeName=ticker,KeyType=HASH
```

Not in `deploy/cloudformation.yaml`, matching every other table here and for the
same reason. IAM needs no change (`table/ystocker-*`). No TTL: a hand-built
valuation is exactly the thing that must not evaporate — the 120-day staleness
rule refuses to *score* an old row while leaving it visible and editable.

#### An ADR is priced in one currency and files in another (`listing.py`)

Reported from the page on 2026-09-19: **TSM's P/E showing 1.01**. It was not a
data glitch but three mutually inconsistent bases in one reconstruction, all
handed back by the same Yahoo `Ticker` and reconciled by none of them:

| | basis |
|---|---|
| price 434.67 | USD, **per ADR** |
| `Diluted EPS` 431.35 (TTM) | TWD, **per ADR** |
| `Ordinary Shares Number` 25.93e9 | **ordinary** shares |
| `info.sharesOutstanding` 5.19e9 | ADR-equivalent (= ordinary ÷ 5, exactly) |

So `close / eps` was **1.01** against a true 32.46, and `P/FCF` read 11.4 where
the truth is ~73. The direction matters: every error here makes a company look
*cheaper*, on a page whose only output is a cheapness score driving a
contribution multiplier. It also corrupted the DCF branch, which was comparing a
TWD fair value against a USD quote and returning `V_DCF` ≈ 83 — pulling TSM's
headline V up by ~12 points on nothing.

**Only ADRs are affected, and that is the whole population — not a heuristic.**
A foreign *local* listing prices and files in the same currency, so it was always
right: measured, Samsung reconstructs to 40.9 on KRW/KRW and Itochu to 18.0 on
JPY/JPY. The screen against `data.py`'s independently-converted `PE (TTM)` found
exactly 1 of 60 tracked tickers wrong. But the registry is dynamic, so the real
population is "every ADR anybody opens".

Four things hold the fix together:

- **Two corrections, and fixing one is worse than fixing neither.** Currency and
  share basis are independent. Converting the currency alone leaves market cap
  wrong by the depositary ratio, so P/E would come out right and P/FCF, EV/EBITDA
  and P/TBV would not — and a reader has no way to tell which columns were
  repaired.
- **The ADR ratio is derived, never looked up.** `statement_shares /
  info.sharesOutstanding` gives 5.0 for TSM, so there is no table of known ratios
  to maintain. Inside `SHARE_RATIO_TOLERANCE` the two counts are the same number
  differing by treasury stock or basic-vs-diluted, and are left alone.
- **The EPS basis is measured, not assumed.** Yahoo's EPS row is *already*
  per-ADR for TSM — 5.06x the figure `Net Income / Ordinary Shares Number`
  implies — so applying the share ratio there as well would divide the P/E by
  five. Assuming that holds for every issuer is the guess this repo keeps getting
  punished for, so `eps_scale()` checks the converted figure against
  `info.trailingEps`, which Yahoo publishes independently on the quoted basis,
  and accepts a correction only when it is 1.0 or the share ratio. A third answer
  is named, not split.
- **The rate is historical, and that is the same trap as forward-vs-trailing.**
  Spot would have been free — `data.usd_rate` is already cached — and applies a
  2026 rate to a 2021 statement, then ranks today's multiple against the result.
  So `_fetch_fx_series` pulls `{FIN}{PX}=X` weekly, a **seventh** Yahoo read that
  fires only when the currencies actually differ, and each vintage converts at
  the rate in force when it became public. Spot is the labelled fallback; neither
  available is `unavailable: currency_unreconciled`, because the choice is then
  between publishing multiples wrong by an exchange rate and publishing none.

Converting the statements rather than the price is a free choice for the
multiples — every factor is a ratio, so the two are algebraically identical, and
`tests/test_listing.py` pins that. It is *not* free for what the payload stores:
converting the statements leaves `prices` and the DCF's per-share fair value in
the currency the reader sees quoted, where converting the price would put a fair
value in TWD next to a USD ticker.

No `CACHE_VER` bump, matching the `beta` precedent — a bump is ~360 Yahoo reads
in one sweep. Affected payloads self-heal within the 24h TTL; force-refresh the
handful of ADRs instead.

Tests: `tests/test_listing.py` (52, no app/network, anchored on TSM's measured
figures and checked against `trailingEps`, `sharesOutstanding` and `marketCap`),
plus `ListingBasisIntegrationTests` in `tests/test_dca_history.py` (9, `build()`
with the fetch stubbed) — because the arithmetic being right and the arithmetic
being *called* are different questions, and mutating the call out of `build()`
left the pure suite green.

#### The relative branch

**The percentile's basis is the whole ballgame.** A forward P/E and a trailing
P/E are different numbers about the same company, and for anything growing the
forward one is *lower*. Rank today's forward multiple against a distribution of
trailing ones and the answer is not slightly off — it is biased **cheap**, every
time, for every growing company, and that bias flows straight into a larger
contribution while looking entirely normal on the page. So `reconstruct()`
derives today's value as **the last point of its own series**, on the same basis
as every historical point, rather than reading `forwardPE` off Yahoo. A rank
within one consistent series is then correct by construction and there is
nowhere left to make the mistake. This is the same separation `valuation.py`
draws between its bottom-up `forward` series and its `fwd_realized` one.

**There are two histories and they are never spliced.** The *reconstructed* one
(weekly price ÷ statements public that week, ~5y, trailing) is what the score
uses and it works on first page load. The *banked* one
(`ystocker-dca-history`, one row per ticker per day, forward basis) is observed
state on the same terms as `ystocker-valuation-history` — nothing sells back
yesterday's consensus forward multiple, so a row not written is gone. It starts
empty and is worth nothing for months, which is precisely why the reconstruction
exists. The API returns it as a separate `banked` block, reported **even at zero
rows**: a daily series that has silently stopped being written looks exactly
like one that has not started, and this one cannot be backfilled, so the day
that is noticed is the day the gap becomes permanent.

**Point-in-time, or it is not history.** A fiscal year ending 31 Dec is not
knowable on 1 Jan; the 10-K lands 60–90 days later. `ANNUAL_LAG_DAYS` (90) and
`QUARTERLY_LAG_DAYS` (45) hold each vintage back until it was public, and
`percentile_series()` ranks each week against **only the weeks before it**. Both
guards are invisible when wrong and both flatter: step on the period-end and
January is priced on earnings nobody had; rank against the whole sample and a
2022 trough only registers as a trough because 2024 is in the denominator.

**Direction lives on the factor, never in a formula.** The source framework
writes the software template as `0.30 P_EVSales + … + 0.25 (100 − P_FCFYield)` —
the inversion inline, because a high FCF yield is cheap while a high EV/Sales is
dear. Copying that shape invites applying the flip twice (once in the shared
table, once in the template), and a double inversion is silent: still 0-100,
still renders as a percentile, now says the opposite. `DIRECTION` owns it exactly
once and `TEMPLATES` contains no `100 −` anywhere, asserted by a test.

**A dropped factor renormalises, but only so far.** A negative P/E is not "very
cheap", it is *not a measurement*, so the factor is dropped and the survivors
rescaled — the framework's own adaptive rule. Taken literally that rule has no
floor, and one surviving factor rescaled to 100% produces a score that renders
identically to a five-factor one. `MIN_SURVIVING_WEIGHT` (0.5) refuses instead,
and the page lists what was dropped. `MIN_OBSERVATIONS` (60) is the same idea for
the distribution: a rank over eleven points can only return eleven answers and
will happily say 100.0.

**The 1.5x ceiling is on the product.** `M_valuation` alone tops out at 1.50x, so
a cap applied to that term only is invisible until a cheap stock *also* has
estimates being raised (1.08x → 1.62x) — the exact case the ceiling was written
for. `combine()` caps the product and reports `capped` plus the uncapped figure
rather than quietly handing back a number that does not follow from the formula.

**Nothing in the request path fetches.** A cold ticker costs six Yahoo reads
(`info`, `income_stmt`, `balance_sheet`, `cashflow`, `quarterly_income_stmt`,
`history`), which is tens of seconds — and gunicorn's `--timeout 120` would take
the worker's other requests with it. So `/api/dca` answers `202 warming`, kicks
one background rebuild per symbol (deduped), and the client polls with a bounded
loop that **must render its terminal state**: stopping the timer alone leaves the
spinner on screen forever. Cached 24h on disk, one file per ticker.

The daily snapshot sweep costs **no Yahoo call at all** — it reads the
`ticker_cache.json` the rolling refresher already maintains, which is what makes
banking ~230 symbols a day free. It deliberately does *not* pre-build any
reconstruction: that is six reads per symbol across the universe, which is
exactly the sweep `valuation.py` records having got this box hard-blocked.

The two overlays cost nothing extra either. `M_earnings` comes from
`analyst.peek()`, which already sweeps `eps_trend` for the whole universe — a
*vendor-reported* revision rather than one inferred from our own snapshots.
`M_portfolio` comes from `/assets`' 穿透 exposure, so an ETF sleeve counts toward
the name, which is how concentration is actually reached. Both are **neutral when
absent** (1.0x, band `unknown`), never a penalty: docking a contribution because
a feed was down makes the answer depend on vendor uptime. Signed out is `None`
and zero exposure is `0.0` — the same multiplier, deliberately distinguishable,
because the reader needs to know whether the overlay is switched on.

**`peer` is cross-sectional and has no history.** We know what NVDA's peer group
looks like today, not what it looked like in 2023. So the headline V includes the
peer factor and the V-history line does not — the adaptive rule drops it and
renormalises automatically — and the chart says where it ends versus the
headline. Holding today's peer percentile constant back through 2021 would draw
a smoother line the data cannot support.

**The DCF branch has no history either, for the same reason and with the same
consequence.** A DCF struck today says nothing about what a DCF struck in 2023
would have concluded, and back-solving one from the statements public that week
would need a point-in-time WACC and a point-in-time consensus neither of which is
recoverable. So `v_history()` is a **`V_REL` line**, not a `V` line, and on a
blended ticker its last point is deliberately not the headline — exactly as with
peer, and labelled the same way. Two branches now sit between the chart and the
headline rather than one.

Model selection is ticker → industry → sector → `compounder`, and the ticker map
beats the rest for a reason: AMZN and TSLA are both "Consumer Cyclical" on Yahoo,
so letting sector decide would score AMZN on a P/E its business model makes
meaningless. Semiconductors sit under "Technology" and need the cycle adjustment,
which is what `INDUSTRY_MODELS` is for.

**A label can only separate what the label distinguishes**, and `TICKER_MODELS`
now carries a second block beyond the framework's sixteen for exactly the cases
where it cannot. Each was observed misrouted on the live ranked table, and a
template is not cosmetic — it decides which five multiples the score is built
from:

- **Card networks were scoring as banks.** Yahoo files Visa and Mastercard under
  Financial Services, and 40% of the bank template is P/TBV. A payment network is
  asset-light and carries almost no tangible book, so that factor returned noise
  or nothing — and losing it dropped the template under `MIN_SURVIVING_WEIGHT`.
  Both were not merely mis-scored, they rendered as `—`, unscorable. Fixed by
  label (`credit services`), ordered *before* the bank rows so it does not shadow
  them.
- **Yahoo publishes no usable sector or industry for the Korean listings**, the
  same metadata failure that reports them as `MUTUALFUND` with a Morningstar id
  for a name. `005930.KQ` and `000660.KQ` fell through to `compounder` — the one
  template with no cycle adjustment — for two of the largest memory makers there
  are. Named explicitly, because there is no label to fix.
- **No label separates Microsoft from Cloudflare**: Yahoo calls both
  "Software - Infrastructure". `NET`, `CRWD` and `MDB` are named to
  `high_growth_software`; the industry row keeps the mature reading as the
  default for everything else, which is right for ORCL, ADBE, NOW and CRM.
- `SNDK` and `LITE` are named rather than routed, because "Computer Hardware" and
  "Communication Equipment" also cover Dell and Cisco. `WBD` likewise, because
  "Entertainment" also covers Netflix and Disney, both of which score sensibly as
  compounders.

The tests assert both halves: that each named company moved, **and** that the
companies sharing its label did not.

`nav_premium` and `affo_yield` are **not** reconstructed — they need an appraised
NAV and an AFFO reconciliation, neither of which is in a Yahoo statement — so a
REIT scores on FFO and peers and the page names what was dropped. FFO is
`net income + D&A`, NAREIT's first two terms, labelled as the approximation it is.

**Two arithmetic traps in the reconstruction, both found by rendering the numbers
rather than by reading the code.** Yahoo's `quarterly_income_stmt` reports *that
quarter's* EPS, not a trailing-twelve-month one, so feeding it straight into
`price / eps` reports a P/E about **four times too high** — 90x where the truth
is 23x — and because those points land in the same series as the annual ones the
history grows a sawtooth that reads as genuine multiple expansion.
`build_quarterly_ttm()` sums the four-quarter window, and sums **flows only**:
debt, cash, share count and book value are *stocks*, and adding four of them
reports four times the company. Second, once quarterly vintages are interleaved
with annual ones the *adjacent* vintage is often one quarter back, so
`current.eps / previous.eps` measures a quarter's growth and hands it to PEG as
if it were annual — ~3.7% where the truth is ~10%, inflating PEG about 2.7x and
reading as a far more expensive stock. `_year_ago()` searches a 300–430 day
window instead and returns `None` when nothing sits in it, because growth over
the wrong interval is a different quantity, not a rough one. It also excludes
the same-period duplicate: a quarterly TTM ending 31 Dec and that year's annual
are the same period with different publication dates, and comparing them yields
exactly 0% growth. PEG carries 15–25% of the weight in three templates.

**`expensiveness()` leaves `weight` unset when it refuses to score**, and
`_dca_equation` must guard for it. Formatting `None` with `:.2f` raises
`TypeError`, which 500s the request — and this is not an edge case: a bank with
no tangible book value, or an ADR with thin statements, lands there routinely.

**`/dca` (no ticker) is the ranked overview**, in the header nav. It scores the
universe — derived from `dca.TICKER_MODELS` rather than kept as a second list,
so the set the framework names and the set the page ranks cannot drift, with
`GOOG` dropped in favour of `GOOGL` since they are one company and only `GOOGL`
is in `PEER_GROUPS`. It is built **only from reconstructions already on disk**:
a fan-out of six reads per name on a page load is exactly the sweep
`valuation.py` records having got this box hard-blocked. Missing names come back
in `pending` with a count so the page says "12 of 15 scored" — a league table
silently missing its cheapest entry is worse than an honest gap — and a paced
background warm (`WARM_SPACING_SECONDS`, stops on a provider cool-down like
`analyst._fetch`) fills it within a few minutes of the first visit.

`_dca_score()` is the **single scoring path** behind both the row and the detail
page. Two implementations of one formula would agree the day they were written
and drift after, and a reader comparing a row against that ticker's own page is
exactly who would find it; `check_dca_endpoints.py` asserts they match. The two
lookups that are per-request rather than per-ticker are hoisted out of it:
`peer_percentiles` re-parses a ~170 KB file when not handed records, and the
look-through behind `M_portfolio` is a whole-portfolio walk — doing either per
row makes a twenty-row table twenty times the work for the same answer.

**Names at V ≤ 20 are left off the overview unless asked for** (2026-10-04):
off the table, both summary cards and the period total. The "dearest" card
would otherwise list exactly those names, and a total over rows nobody can see
does not add up. The cut is on V as displayed, so a 20.3 that reads "20" goes
too. A V of null is not "≤ 20" and stays. Never silently: `#dearNote` says how
many are hidden, with Show/Hide.

The table sorts cheapest-first and an **unscorable row sorts last, not as V=0**:
"could not be measured" is not "at its most expensive ever", and putting it at
the top of a column headed cheapest would be a plain lie. The page also states
what V is not — a cross-company ranking. Each row is scored on its own model,
and a 70 means "cheap for this company", not "cheaper than the row above".

**Every rebuild draws on one global budget** (`MAX_INFLIGHT_BUILDS`,
`BUILD_MIN_GAP_SECONDS`), shared by the on-demand path and the sweep. The
per-symbol guard in `_dca_kick` stops N readers of one ticker costing N
rebuilds and **bounds nothing when the symbols differ** — which is the case that
actually happens. Measured on the afternoon this shipped: one person following
the DCA link off `/history` pages hit twenty distinct tickers in eight minutes,
~120 Yahoo reads with nothing in between. A refused slot is invisible and cheap:
the API already answered 202 and the client was already polling, so the work
just starts on a later poll — it reports `queued` so the page can say "waiting
for a slot" rather than implying progress. `release_build()` floors at zero,
because a doubled release would silently raise the ceiling for ever, which is
the one limiter failure nobody notices until Yahoo blocks.

**The ranked universe is a registry, not a constant** (`dca_universe.py`,
`ystocker-dca-universe`). Opening `/dca/<TICKER>` for a name that *scores* adds
it, and from then on it is in the table and refreshed by the daily sweep. The
framework's fifteen named companies are an unevictable **seed**, so a burst of
lookups cannot quietly turn the overview into a list of whatever somebody typed
last week.

Three things hold it together:

- **The cap is a daily Yahoo bill, not a storage limit.** Every tracked ticker
  is six reads a day, for ever, so an unbounded registry is a slow-motion
  version of the bulk sweep `valuation.py` records having got this box
  hard-blocked — it would not fail on the day it went wrong, it would just get
  heavier every week. `MAX_TRACKED` (60) bounds it and the **least recently
  opened** non-seed entry is evicted; `touch()` bumps recency on every view, so
  a name somebody opens daily outlives one they opened once. Eviction prunes the
  *store*, not just the view — hiding the overflow from the page would leave the
  table growing.
- **Only what actually scored gets in.** Registration is in `dca_history.get()`,
  after a successful build that produced a series — never on the search itself.
  Yahoo publishes no statements for an ETF, so `GDX` and `IGV` build to an
  `unavailable` payload; admitting those would fill a table headed "all scored
  names" with rows that can never carry a score while still costing six reads a
  day each to re-confirm it. Ten of the twenty names opened on this feature's
  first afternoon were exactly that shape.
- **It degrades, it does not fail closed.** Unlike `portfolio`, a lost row here
  costs one lookup — open the ticker again and it comes back. So DynamoDB and an
  on-disk mirror are both read and unioned (matching
  `valuation._previous_snapshots`), and with neither available the seed still
  ranks.

A seed name cannot be untracked (`/api/dca/track/<ticker>` returns 400): it
would reappear on the next sweep, which reads as a bug rather than a policy.

Note the key schema differs from `ystocker-dca-history` on purpose. This table
is one short row per tracked ticker and bounded by `MAX_TRACKED`, so listing it
is a Scan; the history table gains a row per ticker per day and is therefore
always queried on its `ticker` hash key.

```bash
aws dynamodb create-table --table-name ystocker-dca-universe --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=ticker,AttributeType=S \
  --key-schema AttributeName=ticker,KeyType=HASH
```

**`/dca/<ticker>` carries a peer panel** (`/api/dca/<t>/peers`): the same V, band
and sized contribution for the rest of that company's `PEER_GROUPS` entry, with
its own row sorted into place so the reader can see where it sits.

The group is `dca_history.peer_group()`, extracted so the panel and the `peer`
factor above it cannot name two different peer sets for one company, and the P/E
column is pinned to the basis `peer_percentiles` actually ranked on — one basis
for the whole group or none, since a forward P/E beside a trailing one under a
single heading makes the forward name read cheaper on nothing but a data gap.

**It never fetches and, more to the point, never builds.** The first half is the
overview's rule (six Yahoo reads per name). The second is the one that is easy
to miss: `dca_history.get()` *registers* whatever it successfully builds, so
warming eleven peers because somebody opened one ticker would push eleven names
into a registry capped at 60 and evict whatever was least recently opened. A
side panel must not be able to rewrite what the overview ranks. So an unbuilt
peer is **named, not fetched** — with the cross-sectional P/E it already has from
`ticker_cache.json` and a link that builds it if the reader actually wants that.
`not_built` and `unavailable` stay apart for the reason `pending` and
`unresolved` do in the look-through: only one of the two is worth clicking.

The panel's whole risk is that a column of V scores reads as a ranking, which it
is not — each is a company against its *own* history. Two things push back: the
note says so, and a peer scored on a **different template** is marked (`✻`),
because two V scores built from different five-factor templates are less
comparable than two built from the same one. `same_model` is `None`, not `false`,
when nothing on disk can answer it — flagging every peer as a mismatch on no
evidence is worse than saying nothing.

Its CSS is hand-written rather than Tailwind utilities, and that is not taste:
`css/tailwind.css` is **compiled**, so a class that only ever appears inside a JS
template literal can be missing from the bundle. Measured — `mr-3`,
`hover:underline` and `max-w-[11rem]` are all absent from the shipped file, which
ran every ticker in the unscored list together into one unreadable string.

**`/assets` carries a Dollar-Cost Averaging tab** (`/api/dca/portfolio`), and
its unit of analysis is the **company after full 穿透**, not the held line. A
reader holding VOO does not own "a fund" — they own Apple and Microsoft and 498
others, and only a business has a valuation. So the panel walks
`assets.analyse`'s look-through exposures (already equity-only and value-sorted)
rather than `positions`. Before that it ran off the lines and rendered
"fund — not scored", which left the tab empty for an index investor: the exact
population the engine is most useful to.

That is also what makes `M_portfolio` mean something. Measured on a two-line
fixture: MSFT held directly at 19.8% *plus* VOO takes the real exposure to
25.4% through two routes, tripping the concentration throttle to 0.25× — a
figure invisible on any per-line view.

- **The headline is the exposure-weighted portfolio multiplier**, because it is
  the only number here a reader can act on: one contribution into the existing
  mix scales by it. A look-through row is analysis, not an order — you buy the
  fund, not the Apple inside it — so the per-name amount is explicitly "what one
  base unit into this company would size to".
- **Weighted, not averaged flat.** A 12% position and a 0.3% one do not get
  equal say in how the next payment is sized.
- **Scope and floor are both stated.** The weighted figures report what share of
  penetrated equity they actually cover, and `coverage_pct` rides along because
  Yahoo discloses a fund's top ten only — every exposure is a lower bound.
- **Truncation is reported, never silent.** `DCA_PORTFOLIO_MAX` (30) ranks by
  exposure; a three-ETF portfolio penetrates to over a thousand leaves and each
  is six Yahoo reads. The tail is sub-0.1% slivers no contribution turns on, but
  the response says how many were dropped and what they are worth.
- **The tail's totals come from `seen_value`, not from serialising it.**
  `analyse(top=None)` builds a dict per leaf *including its full routes list*,
  so asking for all of them to add up two numbers was hundreds of kilobytes of
  garbage per request. `seen_value` is the named-equity total whatever `top` is,
  so the exact figures fall out of one subtraction.

The table has its own `min-width` (44rem): borrowing `.as-holdings-table`'s
78rem, which is sized for the twelve-column holdings grid, forced a horizontal
scrollbar onto a table that fits a laptop. The three multiplier terms are one
cell rather than three columns, dimmed at 1.00× rather than hidden — "did not
move" and "could not be measured" are different statements. A row the
concentration term is actually throttling gets a warning rail, since that is the
one signal worth acting on and it is easy to lose in a number.

Tests: `tests/test_dca.py` (72, no app/network — including **both** worked
examples: the relative-only one (E=75.55 → $3,722.50 on a $5,000 base) and §15's
DCF-integrated one (V_DCF=71.0, V_REL=44.0 → V=52.1 → ~$3,905), plus a check that
every band/model/factor key *and every DCF refusal and note* exists in **both** EN
and ZH, since those are composed in JS by string concatenation where
`I18n.apply()` cannot reach them), `tests/test_dcf.py` (56, the upside map's
monotonicity and clamps, the scenario weighting, every refusal, and the
zero-growth perpetuity identity that pins the discounting),
`tests/test_dcf_store.py` (18, the override validator — scenarios out of order,
a WACC that could never score, bands refused rather than clamped),
`tests/test_dca_history.py` (74, the look-ahead guards, the TTM sum, the
year-ago growth window, the build budget, the capex sign trap and the
annual-only DCF series), `tests/test_dca_universe.py` (24, the cap and what it
evicts), `tests/check_dca_endpoints.py` (94 end-to-end, `check_` so
`unittest discover` skips it — it needs an app and stubs matplotlib), and
`node tests/check_dca_peers_panel.mjs` (15, no browser — it extracts
`renderPeers` from the template rather than copying it, since a copy agrees on
the day it is written and drifts after).

Two of those are worth knowing about before changing the engine. The endpoint
check `test_the_equation_evaluates_to_its_own_answer` no longer asserts
`V == 100 − E` — that is `V_REL` now — and instead walks the whole chain, so a
DCF that silently stopped being folded in fails there rather than passing an
identity that had quietly stopped describing the engine. And
`tests/test_theme_classes.py` scans templates for `classList` calls holding more
than one token; it cannot tell code from comment, so an explanatory comment
*quoting* the broken form fails the build.

The table is **not** in `deploy/cloudformation.yaml`, matching every other
observed series here and for the same reason. IAM needs no change
(`table/ystocker-*`). Note the key schema differs from the others: `ticker` HASH
+ `date` RANGE, so reading one symbol is a Query rather than a full-table Scan —
on `PAY_PER_REQUEST` a scan is billed by volume scanned, and a per-ticker page
would otherwise pay for every other ticker's rows on every load. No TTL: the
whole point is that these rows cannot be recreated.

```bash
aws dynamodb create-table --table-name ystocker-dca-history --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=ticker,AttributeType=S \
                          AttributeName=date,AttributeType=S \
  --key-schema AttributeName=ticker,KeyType=HASH \
               AttributeName=date,KeyType=RANGE
```

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

The mail is written in the language the *report* was written in (`job["lang"]`,
frozen at submit), not a UI preference read at send time — chrome, role names,
team dividers, dates and the elapsed clause all follow it, and `_STR` carries EN
+ ZH with a test asserting neither has a key the other lacks. It signs itself
with the brand of the host it links to (`brand_for()`): the page derives
`brand_name` from `request.host`, but there is no request in a background thread,
so `TA_HOSTS` moved to module scope in `__init__.py` for both to share. The
masthead mark is drawn in table cells rather than fetched as an `<img>`, because
Outlook and Apple Mail block remote images by default and the one decorative
element would otherwise be an empty box.

Sending is on by default once `SES_FROM_EMAIL` is set (already synced from SSM);
`AGENTS_EMAIL_REPORT=0` is the kill switch, and `AGENTS_BASE_URL` overrides the
link host, which defaults to `https://trade-agents.com` rather than
`stock.li-family.us` because that is the domain `/agents` exists to serve.
Errors are *not* mailed — only finished reports. Tests:
`tests/test_report_email.py` (76 unit tests, no app, no network, no SES).

### Posts — the external write door (`/api/posts`, `/posts`)

A write-only door for anything that can make an HTTP request — a cron job, a
broker webhook, a script on another machine — and a signed-in feed that renders
what came through it. `text` is rendered by the shared `static/markdown.js`, so
a sender can post Markdown **or** HTML: that renderer escapes text before adding
any tag and rebuilds HTML against an allowlist rather than passing it through,
which is what makes accepting HTML from a token holder safe at all.

Shipped for one day as `/inbox`. `/api/inbox` is still a live alias, because a
sender is a script somebody configured by hand and breaking it to tidy a URL
costs a silent outage to save nothing; the *page* 302s to `/posts` instead, so a
bookmark moves itself rather than leaving two addresses serving one thing. The
module and table keep the `inbox` name — a DynamoDB table cannot be renamed in
place, and the module follows the table it owns. The message shape is loose on purpose: a handful of
optional fields the page knows how to render (`title`, `text`, `source`,
`level`, `ticker`, `tags`, `url`) and **everything else kept verbatim** under
`data`, so a sender never has to ask permission to add a field.

It is the **first write endpoint on this box not behind a Google session**, and
that is the whole reason `inbox.py` is careful out of proportion to its size.
The access log shows what the internet does to a public address unprompted —
`/.env`, `/.git/config`, `/phpinfo.php`, hundreds of probes a day — so an
unauthenticated write endpoint would be found and filled, not in theory.

Four rules, none negotiable:

- **No token configured means the door is shut, not open.** `INBOX_TOKEN` unset
  returns 503 and stores nothing. Treating "unset" as "unchecked" is the single
  mistake that turns a missing SSM parameter into an open relay, and it fails in
  the direction where nothing looks wrong.
- **The token check runs before the body is read.** An unauthenticated caller
  must not be able to make us parse, size or store anything — a malformed body
  from a caller with no credential returns 401, not 400. Comparison is
  `hmac.compare_digest`; `==` leaks the prefix through timing and this is a
  bearer credential with nothing behind it.
- **Every field is bounded before storage, not at render.** An unbounded `text`
  fills a table and a page with one request, and a length check on the way *out*
  has already paid for the storage.
- **Reads are gated even though writes are authenticated.** `/inbox` is
  signed-in only, which decides what a leaked token *is*: a nuisance (somebody
  fills your inbox) rather than a publishing channel onto trade-agents.com in
  your name. Those are different incidents and the difference costs one check.

**A `cid:` image can only be resolved where the message is.** Post
`Content-Type: message/rfc822` instead of JSON and the receiver does the MIME
work: `inbox.parse_email()` takes the subject as the title, the HTML part as the
body, and rewrites every `src="cid:X"` into a `data:` URI from the matching
attachment. This exists because a forwarded digest arrived with
`cid:digest-header` and the banner's bytes were nowhere on this box — a sender
that pre-extracts the HTML has already thrown the picture away, so no amount of
work on the rendering side can recover it.

The budget is set by DynamoDB's 400 KB item ceiling, not by taste: base64
inflates by 4/3, so `MAX_INLINE_IMAGE_BYTES` (96 KB) and
`MAX_INLINE_TOTAL_BYTES` (200 KB) cap the worst case near 280 KB of body, and
`MAX_TEXT` is 300_000 *because of that arithmetic* — at its previous 40_000 a
single inlined image was clipped in half and the picture vanished silently, which
is the exact failure this path removes. An oversized image is **refused, not
resized**: resizing needs an imaging library on the request path and a silently
downscaled picture is a different picture. An unmatched `cid:` is left in the
HTML on purpose, so the page's refusal marker reports it rather than it being
deleted where nobody would learn of it. Every refusal travels back on the post in
`data.inline`.

The raw path gets its own ceiling (`MAX_RAW_BYTES`, 8 MB) because an email with a
banner is megabytes before anything is extracted, and its own way to carry our
fields: `?source=&level=&tags=&ticker=&title=` on the query string, since the
body is the message and cannot hold them. Without that every forwarded mail is
indistinguishable from every other.

`url` is scheme-checked to http(s) **at write time** rather than escaped at
render time, because `javascript:` survives HTML-escaping intact and an `href`
is the one place escaping alone is not enough. Everything else the page renders
goes through `esc()` — every field came from a token holder, not a human.

A store failure is a 503 on both verbs, never a 200 or an empty list. A POST
accepted and dropped leaves the sender with no way to know and no reason to
retry; a GET that answers "no messages" when it means "cannot reach the table"
is the one wrong answer on the page whose job is to show them.

Rate-limited on `quota.py`'s `flock`ed counter (`INBOX_DAILY_LIMIT`, 500/day),
reusing that file's lock so two gunicorn workers cannot both read the same count
and write the same increment.

Key schema is `bucket` (`YYYY-MM`) HASH + `sk` (`<iso8601>#<id>`) RANGE, so "the
most recent fifty" is a Query with `ScanIndexForward=False` rather than a Scan —
on `PAY_PER_REQUEST` a Scan is billed by volume scanned. Monthly buckets rather
than one fixed partition so it cannot grow without bound; `recent()` walks back
a bounded number of buckets so a page opened on the 1st does not show nothing
while the previous month is full. TTL is on at `INBOX_RETENTION_DAYS` (90) —
unlike the observed series in `dca_history`, these rows *can* be re-sent by
whatever produced them, so keeping them for ever buys nothing.

Tests: `tests/test_inbox.py` (28, no app/network/AWS — the fail-closed token,
the caps, the refusals) and `tests/check_inbox_endpoints.py` (17 end-to-end,
`check_` so `unittest discover` skips it).

```bash
aws dynamodb create-table --table-name ystocker-inbox --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=bucket,AttributeType=S \
                          AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=bucket,KeyType=HASH \
               AttributeName=sk,KeyType=RANGE

aws dynamodb wait table-exists --table-name ystocker-inbox --region us-west-2
aws dynamodb update-time-to-live --table-name ystocker-inbox --region us-west-2 \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at"

# The credential. SecureString, read into the app by _load_secrets_from_ssm.
aws ssm put-parameter --name /ystocker/INBOX_TOKEN --region us-west-2 \
  --type SecureString --value "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

Not in `deploy/cloudformation.yaml`, matching every other table here and for the
same reason. IAM needs no change (`table/ystocker-*`, `parameter/ystocker/*`).

### Sharing a report with another user

`share.py` plus six routes let a signed-in user mail or text one of their
finished reports to anybody, and it is the only user-to-user feature in the
monorepo. The unit of sharing is a **capability**: a row keyed by
`secrets.token_urlsafe(16)` in `ystocker-agent-shares`, and `GET
/agents/shared/<token>` will render whatever job that row names, to anyone,
with no sign-in.

That shape is forced, not chosen. yStocker **persists no user record at all** —
every gate, quota and credit balance keys off `session["user_email"]` at use
time, so there is nothing to grant permission *to*, no way to check a typed
recipient is real, and no way to tell a typo from a stranger. And the recipient
is by design somebody with no account: `/agents` is the paid surface, so the
whole point of sharing is to show a report to someone who has not run one.

The cost is not hedged anywhere and should not be: **anyone holding the token can
read the report**, so forwarding re-shares it. The mitigations bound the blast
radius rather than remove it — 128 bits of entropy, a 30-day `expires_at`, an
explicit revoke, and the sharer's own address masked to `alice@…` on the public
page so a forwarded link does not also leak who sent it.

Five things hold it together:

- **The row is written before the mail, always.** The row is what makes the link
  resolve, so a send that got ahead of it would deliver a button that 404s — and
  the case where the ordering matters is exactly when DynamoDB is unreachable.
  `share._get_table()` therefore fails *closed*, the opposite of `quota`.
- **A shared mail must not link to `/agents?job=<id>`.** That route is
  owner-or-VIP and answers 404 to everybody else, so the one button in the mail
  would be dead for the one person it is for. `build(..., link_override=)` exists
  for this; it also fixes the clip notice, which hands off to that same link when
  Gmail truncates at ~102 KB.
- **The banner is reserved out of the size budget** (`_body_rows(..., reserved=)`).
  Chrome added outside the rows still counts against Gmail's limit, and a mail
  pushed over it is clipped *by Gmail*, mid-element — the exact outcome the
  whole-sections rule exists to avoid.
- **Authorization is `owns`, not `can_read`.** A VIP may read anyone's run;
  letting that also mean "may publish anyone's run to an unauthenticated URL"
  would quietly convert a read grant into a disclosure power over other people's
  paid work.
- **The public payload is an allowlist** (`share._SHAREABLE_JOB_FIELDS`),
  mirroring `agents._PUBLIC_FIELDS`. It omits `user`, `log`, `pid` and `chat` —
  the follow-up conversation was never part of the report and is owner-only even
  in the authenticated API.

Two smaller traps. `_share_base()` **forces https** rather than reading
`request.host_url`: nginx forwards `X-Forwarded-Proto` but no app here installs
`ProxyFix`, so Flask sees plain HTTP and would email an `http://` link — which
works, since nginx redirects, but reads like phishing. And the daily cap
(`quota.try_consume_share`, 20/day, `AGENTS_SHARE_DAILY_LIMIT`) is consumed
**before** the send and is not refunded: a share costs almost no compute and
instead spends sending reputation, so the bound has to be on attempts.

`AGENTS_SHARE=0` is the kill switch, separate from `AGENTS_EMAIL_REPORT` so that
turning off completion mail does not also turn off sharing. The table is **not**
in `deploy/cloudformation.yaml`, matching the other five observed-series tables —
create it by hand (IAM already grants `table/ystocker-*`):

```bash
aws dynamodb create-table --table-name ystocker-agent-shares --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=token,AttributeType=S \
  --key-schema AttributeName=token,KeyType=HASH

# create-table returns as soon as the table is CREATING, and update-time-to-live
# refuses a table that is not yet ACTIVE — so the wait is required, not tidiness.
aws dynamodb wait table-exists --table-name ystocker-agent-shares --region us-west-2

aws dynamodb update-time-to-live --table-name ystocker-agent-shares \
  --region us-west-2 \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at"
```

TTL is a convenience, not the guarantee: DynamoDB's sweeper can run up to 48h
late, so `share.lookup()` re-checks `expires_at` on every read and a row past its
date is expected to still be present.

**Three delivery channels mint the identical row.** `channel` on the
`/api/agents/share` call is `"email"`, `"sms"` or `"wechat"`, and only
`"email"` ever asks the server to send anything — same job checks, same
`quota.try_consume_share` counter, same 30-day expiry either way, via the
single shared `share.CHANNELS` tuple both this route and `share.create()`
validate against rather than two literals that could drift. `"sms"` hands the
URL to `static/share.js`, which opens the device's own Messages compose sheet
with a `sms:` link (`smsHref()`) and lets the sharer pick a contact there. This
process never collects a phone number, and that is the scope limit, not a gap:
doing it for real would mean a telephony vendor (Twilio, AWS SNS/Pinpoint),
carrier registration and a per-message cost, none of which this box has, for a
capability the sharer's own phone already provides — the same reason there is
no server-side mail composer either and `mailto:` was never reinvented.
`recipient` is simply `""` for this channel, which is why `share.create()`
takes a `channel` argument instead of inferring one from whether an address was
supplied: an empty string is a valid *value* here, not a validation failure.
iOS's `sms:` handler wants `&body=`; Android's wants `?body=`; `smsHref()`
picks by user-agent because there is no feature to test for which one a
platform's compose intent accepts, and sending the wrong separator is not an
error, just a silently empty compose box — the same "read what the platform
actually does" trap this file already documents for Futu's universal links.

**"wechat" has no URL scheme to hand off to at all**, which is the whole reason
it is a QR code and not a fourth deep link. WeChat's real share integration
(`wx.updateAppMessageShareData`) needs a verified WeChat Official Account, a
bound JS-SDK domain and a per-request signed `wx.config()` call — an external
Tencent registration this box has no part of, not a code change. So
`/api/agents/shared/<token>/qr.png` renders the share URL as a QR code instead,
which WeChat's own built-in scanner ("扫一扫") already reads without any of
that — the same reasoning that makes a QR code the universal fallback on every
Chinese site that shares to WeChat without an Official Account behind it.
`ystocker/qr.py` delegates the actual encoding to the `qrcode` package (Reed-
Solomon and the version/mask tables are not worth hand-rolling — a subtly wrong
implementation produces a code that *looks* right and does not scan) but
rasterises the resulting module grid to a PNG **by hand**, with `zlib` +
`struct` rather than Pillow or matplotlib: `imshow()`'s default interpolation
would blur the crisp module edges a scanner depends on, and a solid-block
rasteriser simply has no antialiasing setting to get wrong. `tests/test_qr.py`
proves the encoder byte-for-byte by decompressing its own PNG output and
diffing it against `qrcode`'s matrix directly, plus a geometric check for the
three finder-pattern squares — no imaging or QR-decoding library needed as a
test dependency for either.

**The link carries its own preview.** `/api/agents/shared/<token>/card.png`
renders a 1200×630 `og:image` — ticker, a decision chip in the same colours as
the completion mail's (`report_email._decision_chip`), brand mark, date — with
matplotlib's Agg backend, the same one `charts.py` already uses for every other
server-rendered PNG. `shared.html` is the only template with Open Graph /
Twitter Card tags at all (`base.html`'s `head_extra` block is empty everywhere
else), because it is the only page meant to be pasted into somebody else's
compose box rather than reached by clicking around signed in. `og:title` /
`og:description` and the picture both come from `share_card.card_text()` /
`share_card.render()`, so the words describing the report and the report
itself cannot disagree. It discloses nothing new: the ticker and the
decision's first line are already public through `share.public_payload`, so
the card is a preview of the JSON, not a second disclosure.

A fixed 96pt ticker size ran a 16-character symbol off the edge of the card
before `share_card._fit_size()` existed — caught by actually rendering a PNG
and looking at it, not by reasoning about the geometry, which is why
`share_card.py`'s own docstring records which environment that check ran in:
this repo's dev checkout intermittently cannot import `matplotlib.pyplot` at
all (the same broken Homebrew `pyexpat` `tests/test_import_graph.py`'s module
docstring already works around) — proving the layout correct needed a second,
throwaway venv with a working one the one time this checkout's own was down.

### Deep research on /history, saved (`research_store.py`), and the TradeAgents card

The ✦ Research tab (深度研究) on `/history/<ticker>` streams a 17-section Gemini
report from a bundle the browser assembles. Until 2026-10-01 that report lived
only in an 8-hour disk cache (`cache/research/`), so reopening the tab the next
day showed nothing and each box rebuild deleted every report ever made. Now a
signed-in reader's report is saved to `ystocker-research-reports`, the tab opens
on their newest one (in the page's language if they have one), and a picker
lists the last twenty with a delete. The same tab carries a TradeAgents card:
this ticker's `/agents` runs, the newest one's rating and Portfolio Manager
turn, every other turn on request, and links to the full report and PDF.

**Only the reader who generated a report can read it, and that is the key
schema, not a check.** `ticker` HASH + `sk` RANGE = `<owner>#<ref>`, with
`ref` = `YYYYMMDDTHHMMSSffffffZ-<id>`. Every read builds the sort key from the
session's address, so another reader's ref simply misses (404 to GET and
DELETE alike). A report written with the position form filled in states the
account value, shares and cost, which is why `agents.owns` keeps a run private
too. An address containing `#` is refused, not escaped: it is the delimiter, and
`tests/test_research_store.py` pins that `a@b.co` cannot see `a@b.com`'s rows
(proven by mutating the `#` out of the `begins_with`).

**Nothing is published to other readers, deliberately.** The bundle is assembled
in the browser, so the server cannot vouch for a number in it; showing one
reader's report to everyone would make the POST a way to put arbitrary text on
trade-agents.com. A signed-out reader still gets a report, unsaved, and the
stream says so.

**A signed-in reader's own rows are their cache, and they never read the disk
cache.** The disk file is keyed by a fingerprint of the portfolio inputs, EPS
and latest quarter only, and its bundle came from whoever POSTed first. That is
tolerable for an 8-hour anonymous cache and wrong as the source of a report
saved permanently into someone's account. If the table cannot be read the route
falls back to the disk cache, which is exactly the old behaviour, and reports
"not saved".

**The stream ends with a `save` event**: `{"ok": true, "report": meta}`
(`existing: true` when it came from the reader's own rows), or `{"ok": false,
"reason": "signed_out"|"store"|"incomplete"|"invalid"}`. A truncated report is
saved, flagged, because it is what the reader was shown; a stream that died
mid-report is not. Reads fail closed: `/api/history/<t>/research/saved` answers
503 when the table is unreachable, never `reports: []`, which would tell a
reader their reports are gone.

**The card is `agents.ticker_runs`, through `/api/history/<t>/agents`**, gated
like every agent read (owner, or a VIP for everyone's with the owner masked). It
reads up to 1000 records, not `_records`' usual 60: that window is the newest 60
across every ticker, and NBIS's only run (08-27) sat behind 182 newer ones, so
the card would have said "no analysis yet" about a ticker that had one. Exact
ticker, every status (a running job is news), an allowlist of fields, and only
the newest finished run carries its PM turn. The rest open on demand through
`/api/agents/job/<id>?events=0`, which skips the progress replay. A rating's
colour is `report_pdf.verdict_tone`, so it matches the PDF.

The card and the saved reports load on the first open of the tab, never with the
page. `?tab=research` opens straight onto it (and is where `/login` returns a
reader who signed in from it); `/agents?ticker=NBIS` pre-fills the run form.
Every /history tab has such a link (`HISTORY_TABS`: charts, fundamentals, dca,
news, videos, research). `switchTab` writes `?tab=` into the address on every
switch, keeping `?lang=` and `?range=`. It uses `replaceState`, so Back leaves
the page instead of walking back through the tabs. The 🔗 Copy link button
carries the tab too. The
Charts tab's deferred panels (forecast, peers, 13F) now register only while that
tab is shown: registered behind another tab, every anchor had no box, so
DeferLoad fetched all three at once, the forecast's model fit included.
`YSTime` moved from agents.html to `static/ystime.js`, shared by both pages.

```bash
aws dynamodb create-table --table-name ystocker-research-reports --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=ticker,AttributeType=S \
                          AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=ticker,KeyType=HASH \
               AttributeName=sk,KeyType=RANGE
```

Not in `deploy/cloudformation.yaml`, matching every other hand-made table, and
IAM already grants `table/ystocker-*`. No TTL. Tests:
`tests/test_research_store.py` (25, an in-memory table that evaluates the real
boto3 key conditions), `tests/test_agents_ticker_runs.py` (10) and
`tests/check_research_endpoints.py` (22, hermetic, through the Flask test client).

### The Fundamentals tab on /history (`xbrl.py`, `statements.py`, `fundamentals.py`)

Modelled on alphascope.trade's company page (2026-10-03): a grid of small
multiples — revenue, gross profit, margins, operating and net income, EPS,
operating and free cash flow, capex, cash and debt, diluted shares, buybacks and
dividends, P/E and P/S — each a bar chart with its latest value, the change on a
year ago and an expand button, switchable between quarterly, trailing twelve
months and annual, over 3/5/10 years or everything. The Charts tab's "Annual
Financials" card is Yahoo's four years; this is the company's own filings back to
the 2009–2011 XBRL phase-in, from EDGAR's keyless companyfacts document (one
request, 3–8 MB, ~0.1 s to parse on the box).

**Every ticker gets the tab.** EDGAR first; where SEC has nothing — Tokyo,
Seoul, Hong Kong, Shanghai listings — Yahoo's statement tables (about four years
and the latest quarters), and the payload's `source` says which. A 20-F filer
(TSM, ASML, SAP) is annual-only from EDGAR and is **never** topped up with Yahoo
quarters: Yahoo states TSMC's EPS per ADR and EDGAR per ordinary share, and one
chart would show a five-fold unit change as growth. An index, ETF, coin or
currency is answered `not_a_company` without a request.

What the filings do not say, and how `xbrl.py` closes each gap — all pinned
against NVIDIA's real figures in `tests/test_xbrl.py`:

- **Nobody files a Q4, and 10-Q cash flows are year-to-date.** One rule covers
  both: two cumulative figures with the same start, a quarter apart, differ by
  that quarter (FY − 9M, H1 − Q1, 9M − H1). Every such figure carries a code
  (`ytd`, `q4`, `fyq`, `avg4`, `calc`) and the tooltip words it.
- **A split re-bases per-share figures from the next filing on.** Facts are
  re-based by the splits *after their filed date* (NVIDIA's Q1 FY25 EPS is $5.98
  as filed, $0.60 restated). The split history rides the one Yahoo price request;
  if it cannot be read, EPS and shares are withheld (`splits_unknown`), not drawn
  across a split as a cliff.
- **Tags change** (`SalesRevenueNet` → ASC 606 in 2018; Oracle's bonds as notes
  payable). Candidates splice only if they agree within 2% on their *oldest*
  shared periods — the newest overlap is where restatements live (Coca-Cola
  2018) — so a component can never be stitched onto a total.
- **Computed gross profit is refused for an insurer** (UnitedHealth's
  "cost of goods" is the pharmacy alone: an 88% margin) **and for a cost line
  under 25% of total costs** (McDonald's: 90%).
- **Fiscal labels come from the filing whose own year it is**, never the earliest
  mention: TSMC's first XBRL 20-F tags 2015 and 2016 fy=2017. Quarters are named
  by where they fall in their fiscal year.
- **Cash excludes marketable securities**, deliberately: NVIDIA moved that line
  between concepts with no overlapping period, and a sum would drop $50B on a
  relabel. **P/E and P/S only for a domestic USD filer** (or a Yahoo listing
  quoted in its reporting currency) — the TSM-P/E-of-1.01 trap again.

The page caps a ratio bar at 4× its median (INTC's 1,149× would flatten ten
years) and says so in the tooltip; a negative TTM EPS is `n/m`, never an old P/E.

**Four more cards (2026-10-04):** revenue growth, return on equity, R&D beside
stock-based compensation, and dividends per share; equity is fetched for ROE.
Growth and ROE are worked out in `xbrl.py`, not on the page. `year_earlier()`
finds the comparison period by date (±25 days), so a missing quarter is a gap
rather than the wrong season, and `growth()` answers only from a positive base.
ROE is trailing income over the *average* of two equity ends and is `None`
wherever either is at or below zero: McDonald's and Starbucks carry negative
equity, and a ratio over it reads as a loss on a profitable company. Measured on
the box: NVDA ~117%, AAPL ~149%, MSFT 34%, JPM 18%. EPS and DPS
(`PER_SHARE_METRICS`) keep four decimals through `_round`, which had rounded
every dividend to a whole dollar. A CSV button exports the view and range on
screen from the payload already loaded.

**The ✦ Research report reads the decade too.** `_buildBundle` asks for
`/api/fundamentals/<t>` alongside its other requests and sends it as
`long_history` (`_longHistory`: ten fiscal years, eight TTM quarters, newest
first, and where today's P/E sits in its own ten-year range), which
`research._long_history_blocks` renders as tables. A cold ticker answers 202
and is left out rather than holding the report. Before, the report saw Yahoo's
three to five years.

**The request path never fetches.** `/api/fundamentals/<t>` reads the disk cache
(`cache/fundamentals/`, 12 h TTL, stale served while it rebuilds); a cold ticker
is 202 and one background build per symbol, under a budget of 2 in flight and
3 s between starts (8 s extra after an 8-request Yahoo-statements build). A
failed build leaves a 10-minute marker read by every worker, so the page's
bounded poll ends on "couldn't load" + Retry (`?retry=1`) rather than spinning.
All EDGAR traffic goes through `sec13f.edgar_get`: SEC's rate limit is per
client, not per module.

Two version stamps, for two kinds of change. `CACHE_VER` is a shape the page
cannot read: a bump drops every cached copy, so each ticker is cold again on its
next view. `CACHE_REV` is a build that carries something an older copy lacks but
can be drawn without, such as a new metric: the old copy is still served and counts
as stale, so it rebuilds in the background on its next view. It went to 2 with
the four cards below. Before it, every ticker cached before that deploy drew
them empty for up to the 12-hour TTL. An `unavailable` answer gains nothing
from a new metric and is left alone.

**`/companies` is the way in** ("Find your next move", after alphascope's
`/dashboard`). Each company is a card with a monogram tile — no third-party logo
CDN, which would hand every reader's IP to a vendor for decoration — and each
card opens that company's Fundamentals tab. The page has two parts:

* **The ~300 companies the ticker cache follows**, with price, day change,
  market cap, P/E and 52-week return, largest first. Gainers / Losers and the
  sector filter apply to these. They read `_get_data()` only, so they cost
  nothing to serve; the quotes refresh a few at a time through the day, and the
  page says so.
* **Every other company listed with the SEC** (`directory.py`), added
  2026-10-03 when asked "why doesn't every company have fundamentals?". Every
  SEC filer already had them (`fundamentals.cik_for` maps any ticker); only the
  directory was narrow. SEC's keyless `company_tickers_exchange.json` lists
  10,434 tickers for 8,008 filers (565 KB, refreshed daily through
  `sec13f.edgar_get`), roughly largest first. A company is one card under its
  first-listed ticker, GOOGL rather than GOOG, with the rest shown as "also",
  where search still finds them. These cards carry no quote: quoting 8,000
  companies would be a bulk Yahoo sweep, the kind `valuation.py` records
  having got this box blocked. The exchange sits where the price would, with a
  divider where the quoted cards end.

`/api/companies/directory` serves one compact JSON (372 KB, 118 KB gzipped by
nginx) built once per load of the file, with an hour's browser cache. A cold
box answers 202 while one fetch runs, and the page shows the followed
companies meanwhile; a failed refresh keeps the last copy. The page draws 240
cards at a time with "Show more", and adds an exchange filter (NYSE, Nasdaq,
CBOE, OTC) and a Followed view. Companies listed only abroad (Tokyo, Hong Kong,
Shanghai) are not in SEC's file. Enter in the search still opens any ticker,
listed or not, and the Fundamentals tab answers them from Yahoo's statements.

**A sort and a watchlist (2026-10-04).** The sort (largest first, best today,
lowest P/E, best 52 weeks, ticker A–Z) orders each part *separately*, the
quoted companies and then SEC's list, so the divider between them is drawn
once; sorting the two together drew it at every crossing. A missing figure
sorts last whichever way the sort runs, and a loss's P/E is not a measurement,
so it is never "lowest". Gainers and Losers are rankings of their own and hide
the sort. The view and sort ride in the address (`?view=watch&sort=pe`).

The star on every card writes to `static/watchlist.js`, one store shared by
three pages. /lookup had a watchlist first, so its key (`ystocker_watchlist`)
and shape (`{ticker, name}`, newest first) were kept and a list built there
carries over. /history's header carries the same star.
- **The cap.** /lookup capped the list at 12 for its chip panel, which with a
  star on 8,000 cards would have dropped picks silently. The cap is now `MAX`
  (200), once, in the module; the panel shows twelve and links to the rest.
- **Storage that refuses** (private mode, a full quota) keeps the list in memory
  for the visit, so a star still toggles.
- **One event.** Every change, from this tab or another (the `storage` event),
  arrives as one `watchlist:change`.
- **Tickers in neither list.** A starred ticker that is in neither list (a
  foreign listing starred on /history) still gets a card in the Watchlist view,
  so it can be opened and unstarred. That waits until SEC's list has answered,
  or every starred SEC company would flash up as a bare card first.
- **Markup.** The star sits beside the card's link in a `.co-cell`, not inside
  it: a button inside an `<a>` is invalid, and its click would follow the link.

Tests: `tests/test_xbrl.py` (57, NVIDIA's filings), `tests/test_statements.py`
(12, Yahoo's tables as served), `node tests/check_fundamentals_js.mjs` (40, the
range/YoY/cap arithmetic), `tests/check_fundamentals_endpoints.py` (23,
hermetic), `tests/test_directory.py` (13, SEC's real rows),
`tests/check_companies_directory.py` (6, hermetic),
`tests/test_research_long_history.py` (5) with
`node tests/check_research_long_history.mjs` (16, `_longHistory` extracted from
the template), `node tests/check_companies_page.mjs` (44, the page's own script
against a fake DOM and the real watchlist.js) and `node tests/check_watchlist.mjs`
(29). Not a DynamoDB table: every figure can be fetched again.

### Choosing the model and thinking depth (`agent_models.py`)

The run form on `/agents` lets a reader pick which models write their report and
how hard the model thinks. `agent_models.py` is a pure table of five choices —
`google-pro`, `google-flash`, `google-lite`, `deepseek-pro`, `deepseek-flash` —
each naming a provider, a `deep_think_llm`, a `quick_think_llm`, the thinking
levels it accepts and its default. Every model id is copied from
`tradingagents/llm_clients/model_catalog.py`, never invented.

**The client sends a table key, never a model id.** TradingAgents does not fail
fast on an unknown model: `base_client.warn_if_unknown_model()` emits a
`RuntimeWarning` reading "Continuing anyway" and the run then dies inside the
vendor SDK on its first call — which is minutes after the credit was spent, with
the reader watching a progress bar. There is no pre-flight validator for the
triple either; `validators.validate_model` checks only (provider, model), returns
`True` for any provider it does not know, and lets the CLI's `"custom"` sentinel
through. So resolution is a dict lookup and a string the client invented cannot
reach the child environment at all, which is a stronger guarantee than validating
one that can. The keys carry **no version** for the same reason
`gemini-3.1-pro-preview` is a preview id: `google-pro` survives the rename, so a
catalog bump does not invalidate every reader's stored preference or make
historical jobs unreadable.

**Accepted thinking levels are per model, and the mismatch is silent.** Gemini
Pro takes `low`/`high`; Flash also takes `minimal`/`medium`. `google_client.py`
remaps exactly one of the four mismatches — `minimal` on Pro becomes `low` — and
forwards `medium` on Pro **verbatim**, so it reaches the API and 400s. Rather
than reproduce that asymmetry in the UI and hope, each choice carries the exact
set it accepts and `resolve()` clamps anything else to that choice's default, so
an out-of-range level is unrepresentable downstream. Providers with no thinking
knob get an empty set and the control is disabled outright: only `google`,
`openai` and `anthropic` are read by `tradingagents.llm_clients.build_llm_kwargs`,
so for DeepSeek the parameter is inert and offering it would be a lie about what
the run does.

**A per-job override must be assigned, not `setdefault`ed.** `_child_env` builds
the child's environment with `setdefault` for the shared knobs, which means
"whatever this process inherited wins" — so on a box whose unit file pins
`TRADINGAGENTS_DEEP_THINK_LLM` a reader's selection would be accepted by the UI,
recorded on the job, shown back on the finished report, and silently ignored by
the process that ran it. This is the same trap the `TRADINGAGENTS_OUTPUT_LANGUAGE`
line was already commented for. `tests/test_agent_models.py` asserts the override
beats a pin.

**Unknown key falls back; unavailable provider is an error.** Those two are
deliberately different. A retired or stale key resolves to the deployment's
default, because model ids churn and a reader whose browser restored a dead
preference must not get a hard failure they cannot clear without knowing to
reload. A provider with **no credential** is refused with a 400 instead, because
TradingAgents raises before the first token and quietly substituting a different
vendor's model would put a name on the report that did not produce it — and
`routes.py` refunds the quota on any `submit()` error, so the rejection is free.
It is unreachable from the page, which disables an unavailable row in both the
pointer and keyboard paths.

The effective triple is **frozen on the job at submit** and `_run` reads it back
rather than re-resolving, for the reason the language is frozen: a queued job
must reproduce the choice the reader made, and re-deriving it would let a table
that shifted under it run one configuration while the report claimed another. A
record written before the picker existed carries no `provider`, which
`job_models()` turns back into the deployment's defaults, so an old queued job
runs exactly as it always did — and because a DynamoDB row is one gzipped opaque
blob, the schema addition needs no table change.

`provider`, `deep_model`, `quick_model` and `thinking` are added to **both**
publishing allowlists (`agents._PUBLIC_FIELDS`, `share._SHAREABLE_JOB_FIELDS`):
a report on the cheapest tier must not read as one on the most capable, and the
share recipient cannot see the sharer's picker. `model_choice` is omitted from
both — it is an internal table key. Publishing a model name to an
unauthenticated visitor is not a new disclosure; `fallback_models` already did.

Selecting a model spends nothing extra: credits are flat (1/run) and the quotas
count runs, not tokens, while the default was already the most expensive setting
(Pro on **both** roles at `thinking=high`). So every choice is at most as costly
as before. `AGENTS_MODEL_CHOICE=0` is the kill switch — with it off the controls
are not rendered, a client that keeps sending a choice is ignored, and the
read-only `Model:` line returns. DeepSeek needs `/ystocker/DEEPSEEK_API_KEY` in
SSM (already set, and IAM's `parameter/ystocker/*` needed no change); without it
`agent_models.provider_available()` reports it unavailable and the rows are
disabled rather than offering a run that would die on a missing key.

Tests: `tests/test_agent_models.py` (43, no app/network/subprocess), including a
cross-check that every offered id appears in TradingAgents' own catalog file.

### The Earnings Analyst, and the Alpha Vantage key behind it

`earnings` joined `BASE_ANALYSTS` in `agents.py`, so every run now draws a sixth
analyst (ninth for A-shares) and every report gains an Earnings section. Nothing
else had to change to render it: `agent_roles.py` already carried the role —
name, 盈利预期修正分析师, icon, colour — so the page and the PDF were waiting for a
turn that was never being generated.

It is ordered between `news` and `quality` to match `agent_roles.ROLES`, which is
the order both renderers lay the turns out in. A roster that disagrees with the
renderer does not error; it reads as agents answering out of turn.

**It is the only analyst with a vendor requirement, and the vendor is opt-in by
credential.** Its evidence tool routes through TradingAgents' `earnings_data`
chain, and `_RUNNER` puts `alpha_vantage` in front of that chain when
`ALPHA_VANTAGE_API_KEY` is set — on by default once the key exists, the same
shape as "sending is on once `SES_FROM_EMAIL` is set" and for the same reason: a
second switch that must be flipped in step with a credential is a switch somebody
forgets. `YSTOCKER_ALPHA_VANTAGE=0` is the kill switch.

Three things about that chain are deliberate:

- **Alpha Vantage goes in *front* of yfinance, not behind it.** The tempting
  reading is "spare tyre for when Yahoo is down", which would put it last. But
  the two answer different questions: Yahoo is the only free source of a real
  7/30/60/90-day revision history, and it publishes **no** announcement dates and
  no release timing — so post-earnings drift cannot be computed from it at all.
  A vendor that supplies what the next one structurally cannot belongs first.
  yfinance sits directly behind it, which is what covers the free tier
  premium-gating the estimate and transcript endpoints, and `a_stock` stays last
  for 沪深京.
- **A keyless box runs exactly as it did.** The vendor raises before any network
  call, the chain falls through to `yfinance,a_stock`, and the analyst still runs
  — reporting the fields it cannot source as stated gaps rather than zeroes.
  That is why this needed no feature flag beyond the kill switch.
- **`DEFAULT_CONFIG.copy()` is shallow**, so `_RUNNER` copies `data_vendors`
  before touching `earnings_data`. Mutating the nested dict in place edits the
  module-level default every later reader sees — invisible in a one-shot child,
  wrong the moment anything in that process builds a second config.

The key is plumbed like every other secret: `/ystocker/ALPHA_VANTAGE_API_KEY` in
`SSM_PARAMS`, and a row in `deploy/sync-ssm.sh`'s `get_ssm_path`. That second row
is not optional and its absence is quiet — `sync-ssm.sh` prints
`SKIP … (no SSM mapping)` and exits 0, so a key added to `.env` alone syncs
nothing while the run reports success. `_child_env` copies `os.environ`, so once
the variable is in the parent there is no further plumbing to the child.

Note the free tier premium-gates `EARNINGS_ESTIMATES` and
`EARNINGS_CALL_TRANSCRIPT`. Those degrade to a stated data gap rather than
failing the run, so the practical purchase is announcement dates, release timing
and the drift figures that depend on them.

### The progress bar on a running report (`/agents`)

While a run is queued or running, the current-run card shows how far it has
got: the turn under way ("Now: 🐂 Bull Researcher, round 2 of 3"), the percentage,
"9 of 24 steps", a clock since the run started, and the six stages
underneath. It is **counted, not timed**. Turns take anything from twenty seconds
to several minutes, so a bar driven by elapsed time would be a guess drawn as a
measurement.

The count works because a run is a fixed sequence and every turn publishes
exactly one progress event. _RUNNER writes when a role's text changes, and a
debater's history grows by one speech a turn. The sequence is:

1. each analyst, in roster order;
2. bull and bear alternating for `2 × debate_rounds` turns;
3. the Research Manager, then the Trader;
4. aggressive, conservative and neutral in turn for `3 × risk_rounds`;
5. the Portfolio Manager.

These are the counts TradingAgents' `conditional_logic` stops at: 24 steps at
the default 3+3, 27 for an A-share run.

`agents.run_plan(job)` rides the job poll as `plan`, so the page never carries
its own copy of the A-share rule `_run` picks the roster by. It maps the package's
`social` onto the `sentiment` its events carry, the one key that differs. The
arithmetic is `static/agent_progress.js`, pure and Node-tested; agents.html only
draws it.

Three rules in it:

- **Everything before a turn that has reported counts as done**, because the
  graph only moves forward. An analyst that publishes nothing (an empty report is
  never written) must not hold the bar at 5/6 while the debate runs.
- **Every count is clamped to its phase**, so a re-publishing role cannot push
  past it.
- **It never shows 100% while running.** Once every turn has reported, it says
  "Writing up the report…" and shimmers across the whole track.

The page counts events by `seq` as a set, not by a high-water mark, so two polls
answering out of order cannot drop a turn or count one twice.

Tests: `node tests/check_agent_progress.mjs` (25, a whole run event by event,
naming the speaker at each step) and `tests/test_agents_progress.py` (8,
including that every planned role is one the runner actually publishes, since a
name the events never carry leaves the bar at zero for the whole run).

### The decision calendar (`/agents`)

Asked for on 2026-10-04: a way to see "the same ticker's decision on each day
from a clear perspective". The history list could not show that. It is sorted
by when runs were made, every ticker mixed, so a reader had to assemble one
ticker's story in their head. A card under the history list, for a signed-in
reader only (not the landing, not the launcher's embedded frame), has three
parts:

- **A month grid**, Monday first. Each day holds that day's runs as chips
  coloured by rating. Under "All tickers" a chip names the ticker; with one
  ticker picked it names the rating.
- **The picked ticker's timeline**: every run in date order, each change of
  rating marked ↑ upgraded, ↓ downgraded or → unchanged.
- **A price chart**: `/api/history/<T>` with each decision placed on its day.
  `period=3mo` (daily bars) when the first run is recent, else `1y`. The axis
  is linear over epoch ms because there is no date adapter (see Known
  Pitfalls).

A chip opens the report in place through `window.agOpenJob`, the page's own
`openJob`, exposed for it. The "Recent runs" card above it starts collapsed
(asked for 2026-10-04). Its title is the toggle and carries the run count, and
the list still loads with the page so the count is there. `?ticker=` from the run form opens on that ticker.

**A run is placed by the trade date it analysed** (`job.date`), not by when it
ran: a Sunday run about Friday's close belongs on Friday.

**The rating keeps all five steps**, from `agents.rating_level`: 2 Buy,
1 Overweight, 0 Hold, -1 Underweight, -2 Sell. `report_pdf.verdict_tone` folds
these into three tones, right for the colour behind a decision box and wrong
here, since Overweight → Buy is exactly the change a ticker's calendar is read
for. The rating word that comes first in the text wins, so "Hold -- upgrade to
Buy above $210" is a Hold. No rating is `None`, never a Hold. A change is
measured only between two rated runs, so a failed run in between cannot fake an
upgrade.

On the chart, strength is in the marker as well as the colour: Buy and Sell are
the largest, and Hold is a diamond, because Buy and Overweight are two greens.
On a phone the day cells use OW / UW (`.s` spans swapped in by CSS under
640px).

`/api/agents/calendar` has the same gate and privacy rule as
`/api/agents/jobs`: signed in, the reader's own runs, a VIP everyone's with the
owner masked. It is never cached. One response carries up to
`agents.CALENDAR_MAX` (1000) runs, so the page draws any month and any ticker
without asking again. It reads up to `_TICKER_SCAN_MAX` records, not the 60 most
recent that every listing reads, or an older run would be missing from its
month. The grid and timeline logic is `static/agent_calendar.js`, pure, as
`agent_progress.js` is.

Tests: `tests/test_agents_calendar.py` (13: the five steps in EN and ZH, the
first-word rule, the privacy rule), `node tests/check_agent_calendar.mjs` (30:
the grid's weekdays, leap years, the timeline's changes across an unrated run)
and `tests/check_agents_calendar.py` (5, hermetic: the gate, the card's
audience, both languages).

### TradeAgents Pro (`/subscribe`, `subscriptions.py`, `ypay/billing.py`)

A monthly or yearly subscription, asked for on 2026-10-04: **$29/month or
$290/year** (two months free, 17%), a **7-day free trial with a card**, and for
that **10 analyses a day** and **every dashboard on trade-agents.com in full**.
Discounts are Stripe promotion codes (`allow_promotion_codes`), made in Stripe's
dashboard and redeemed at checkout, so a discount needs no deploy. Prices,
trial and runs are env-tunable (`SUB_PRICE_MONTH`, `SUB_PRICE_YEAR`,
`SUB_TRIAL_DAYS`, `AGENTS_SUB_DAILY_LIMIT`); `SUBSCRIPTIONS=0` is the kill switch.

**yStocker holds no Stripe secret, and this does not change that.** Every
Stripe call is in yPay (`ypay/billing.py`, three routes in `ypay/routes.py`).
yStocker owns only the *state*: one `sub#<email>` row per subscriber in the
existing credits table (`ystocker-agent-credits`, key `id`), plus a `cus#<id>`
row mapping a Stripe customer back to an address, for an event that names only
the customer. No new table, and IAM already allowed `UpdateItem`.

The signed-in actions cross to yPay as a **15-minute token signed with
`YSTOCKER_SECRET_KEY`** (`subscriptions.handoff`), which yPay now reads from
SSM. Not an address in the query string, which is how a run pack is bought: the
billing portal shows invoices and the card and can cancel, so `?email=` would
let anyone open anyone's. The signer has **no dev fallback** — yStocker's own
session falls back to a key that is in this repository, and a handoff signed
with it would be forgeable — so with no key, Pro is simply not offered.

**Three paths write a row**, so none has to be relied on alone: Stripe's return
to `pay.<brand>/subscribe/done` retrieves the session and records it before the
reader lands (access does not wait for a webhook); the webhook records
`checkout.session.completed` and `customer.subscription.*`; and an hourly thread
in yPay's master re-reads any subscription at or near the end of its period, so
a renewal, a failed card or a cancellation is caught even if its event never
arrives. A row is only replaced by a newer account of it (`event_created`,
conditional write), so a late webhook cannot undo the return's record, and
`trial_used` is only ever set.

**Entitlement is read from the row, never from Stripe on a page view**
(`subscriptions.entitled`): `trialing`/`active` until the period end plus a day
for a renewal not yet reported, `past_due` for a week while Stripe retries the
card, nothing otherwise. The read is cached 60s per process and a row read
within the hour is still trusted if DynamoDB blips, so a paying reader is not
walled by a network fault. Checkout's own read is the opposite —
`subscriptions.load` raises `StoreUnavailable` rather than answering "none",
because "none" would offer a second trial or start a second subscription.

**The reading wall now covers everyone without Pro** on trade-agents.com,
signed in or not (`ta_full_access()` in base.html's `_ta_wall`); a VIP is never
walled, and stock.li-family.us still walls nothing. It is still the **soft**
wall described under the wiki section — the data stays in the page and the APIs,
and stock.li-family.us serves the same dashboards unwalled — so Pro's "all the
data" is a presentation gate, not a lock. With Pro switched off (or not
configured) the wall reverts to signed-out readers only, with the old sign-in
card: a wall with nothing to buy behind it would be a dead end.

**Runs**: `quota.limit_for` gives a subscriber `max(free, 10)`; a VIP keeps 15.
Free runs are spent before credits as before, so a subscriber's packs are used
only after the day's 10. **Subscribers still count against the 60/day global
ceiling** — it is a capacity limit, the docs and the page say so — which means
six subscribers using every run would exhaust it: raise
`AGENTS_GLOBAL_DAILY_LIMIT` as subscribers arrive.

**Found on the way: the run-pack webhook had been failing on every verified
payment.** stripe 15's `construct_event` returns a `StripeObject`, which is not
a dict, and the handler's `event.get("type")` raised `AttributeError` — a 500,
so Stripe retried and no run was ever credited. It now verifies with
`construct_event` and reads the payload as JSON. `tests/test_billing.py` drives
it with a real Stripe signature, so the library's own verification runs.

**And production had no session key at all.** `/ystocker/YSTOCKER_SECRET_KEY`
was listed in `SSM_PARAMS` but had never been created, the box has no `.env`,
and the unit sets no environment — so `app.secret_key` was the fallback
`"ystocker-dev-secret"`, which is in this repository. Verified on the box on
2026-10-04: a session cookie for a made-up address signed with that string got
200 from `/api/agents/balance`, which answers 401 without one. Anyone who had
read the code could sign in as anyone, the VIP owner included, and read every
reader's reports and portfolio. The parameter was created that day
(SecureString, random), which signed every reader out once and nothing else —
only the session is signed with it. yPlanner and yTracker have their keys;
yPlanter, yPay and yBG still fall back to dev keys (yPay keeps nothing in its
session; yBG's admin session is forgeable, but it has no SSM parameters at all
and appears unused).

Stripe setup (live, done 2026-10-04): product `tradeagents_pro` with prices
under lookup keys `tradeagents_pro_month` / `tradeagents_pro_year` (made by
`billing.ensure_price` on first use; a changed amount makes a new price under
the same key, and existing subscribers keep theirs), a billing-portal
configuration tagged `metadata.app=tradeagents` (cancel at period end, switch
monthly/yearly), and the existing webhook endpoint
(`pay.li-family.us/api/webhook`) subscribed to
`customer.subscription.created/updated/deleted` as well as
`checkout.session.completed`.

Tests: `tests/test_subscriptions.py` (25, no app/AWS/Stripe — entitlement, the
conditional store, fail-closed reads, the handoff's refusals),
`tests/test_billing.py` (19 — checkout parameters, price reuse, reconciliation,
the webhook with real signatures, the three yPay routes) and
`tests/check_subscription_pages.py` (14, hermetic — the page, its doors, the
status API, the run page's offer). `check_wiki_pages` covers the wall.

Two traps the tests found before shipping: the portal configuration took the
price cache's lock and then called `ensure_price`, which takes it again —
`threading.Lock` is not reentrant, so the first "Manage billing" would have hung
a ypay worker for good (`_portal_lock` now). And a test that wraps
`sys.modules` in `mock.patch.dict` drops every module imported inside it on
exit, so the next `create_app()` re-imported an *unpatched* `ypay.routes` and a
"hermetic" test reached api.stripe.com with the dummy key; import ypay once at
module level instead.

### The TradeAgents wiki (`/docs`, `/research`) and the `/agents` landing

Modelled on vibetrading.wiki: product docs with a sidebar, an "On this page" list
and prev/next; a Research Lab of long-form, dated posts; and a landing page in
the same editorial style (serif display type, mono kickers, a paper-and-grid
plane). No nginx change was needed — the trade-agents.com vhost proxies every
path but `/` straight through, so `trade-agents.com/docs` just works.

**The landing is the signed-out `/agents`, and only the full-page one.**
`routes.agents_page` passes `landing` only when nobody is signed in and the page
is not `?embed=1` — the floating launcher's frame is 240px tall and keeps its
compact sign-in card, and a signed-in reader is there to run something. The
landing is split into `_agents_landing_top.html` and `_agents_landing_bottom.html`
with the sample-report block left *between* them in `agents.html`, because that
block's script also serves a shared `?job=` link and its ids (`agSample`,
`agSampleCard`) are read there; moving the markup into a partial would have
split one script's DOM across two files.

**`/home` is the same landing for everyone, signed in or not** (`routes.home`,
trailing slash accepted, as on vibetrading.wiki/home/). It exists because the
rule above left the product with no front page its own readers could see: the
owner, always signed in, got the run form at trade-agents.com and could not
check the landing after a deploy. It renders `agents.html`'s landing branch
(`{% if _landing or not signed_in %}`) rather than a copy, and keeps `signed_in`
truthful so every call to action says "Run an analysis" → `/agents` instead of
asking a signed-in reader to sign in. The docs/research bar's Home and "Sample
reports" links point here, because `/` and `/agents` are the run form once
signed in — which has no `#samples` to land on.

**A signed-out visit to trade-agents.com goes to `/home`**, as vibetrading.wiki's
root 302s to `/home/`. nginx is unchanged — it still proxies `/` to `/agents`,
so Flask cannot tell the bare domain from `/agents` — and the redirect lives in
`routes.agents_page`: a 302 with `Cache-Control: no-store`, because the answer
depends on the session. Three cases stay put, all asserted in
`check_wiki_pages`: a signed-in reader (the run form, as before); a `?job=` deep
link, which report emails and shared `/agents?job=` URLs point at and whose
scripts read the id off that page; and `?embed=1`. The query string rides along,
so `?lang=zh` survives. It is scoped to the TradeAgents host
(`_on_trade_agents()`, the same `TA_HOSTS` verdict as `brand_is_ta`), which also
keeps the deploy's health probe — `stock.li-family.us/agents` — at 200.

**On trade-agents.com every page wears the landing's shell** — the masthead
(`_ta_masthead.html`, `.w-top*` in wiki.css), the paper-and-grid plane, one footer
(`_ta_footer.html`) and one column, the masthead's 1180px (`.w-main` as `<main>`'s
default class there) — so moving between the landing, the docs and a chart never
shifts an edge or swaps the bar. That last part was reported from the live site
on 2026-09-27: `/home` sat in a 1180px column under the masthead and `/markets`
ran 1386px wide under the dashboards' own bar, and switching between them jumped
by 103px a side. `base.html`'s `_ta_shell` is simply "the host is
trade-agents.com"; on every other host every page is yStocker's, byte for byte.

A TradeAgents page sets `{% set ta_page = true %}` (the landing, docs, research,
sign-in, contact, the run page, shared reports, the dead-link page). Every other
page is one of the dashboards (`_ta_markets`), and in the shell it adds the Markets
bar (`_ta_markets_bar.html`, `.w-sub*`): their own links, ticker search and
↻ Refresh, as a second row under the masthead in the same column. That is where their navigation went — the masthead has no room
for it. It carries the hooks base.html's scripts need (`[data-navsearch]`,
`#refreshBtn`/`#refreshWrap`/`#refreshTooltipBody`), with `_refresh_map` moved to
base.html's top level so both bars read it. It is sticky under the masthead at
every width. On a phone it used to scroll away, which put a dashboard's
navigation out of reach from halfway down it. Its links scroll sideways at any
width they do not fit. At 880px and below every control in the two rows is 36px tall in
13px type, and the language toggle, theme, search and ↻ are 36px squares. Sized
by its label, the toggle had been 36px in Chinese and 44px in English, so each
switch moved the bar. The
dashboards keep the floating research-desk launcher, which is how a reader runs
the desk from a chart; the product pages drop it, since their masthead leads to
the desk from every page. The footer adds the dashboards' Guide, RSS and FOMC
calendar links on a dashboard. `editorial = true` is the plane and wiki.css
without the shell, for the landing and the wiki, which are editorial everywhere.

The masthead is one bar on every page, as vibetrading.wiki's is: mark, serif
wordmark (plus "Docs"/"Research Lab"/"Markets"/"Research desk" on those
sections), Docs · Research Lab · Samples · Pricing · Markets, language and theme
toggles, and the call to action. A signed-in reader's bar also starts with
**Research desk** (投研台), marked current on the run page (asked for 2026-10-04).
It is not shown signed out, because /agents sends a signed-out visitor on this
host to /home. It reads "Desk" below 1260px, and a signed-in bar drops its
section label and long forms at 1260px rather than 1180. Measured with the extra
link, the full-length bar ran a Research Lab page 49px past the viewport at
1181px, and the long tab ran Docs, Research Lab and Markets 39px wide at 961px,
both in English. It reads the page's `section` — a dashboard's is `markets` —
where `home` makes Samples and Pricing in-page anchors (elsewhere they lead to
`/home#…`), `docs`/`research`/`markets` mark their link current, and `run` and
`login` drop the CTA and the Sign in button, each of which would lead to the page
already open. Signed in it adds an account menu (the balance as an amount and
Prepay — see the pay.trade-agents.com notes at the top — then Portfolio, Posts,
Share a report
through share.js's `[data-share-open]`, Sign out, which reloads), and it reads
sign-in from `current_user`, not the landing's `signed_in`, which only two routes
pass. `showcase_enabled()` is a Jinja global for the same reason — only the
landing's routes pass `showcase`. The wiki's `.w-bar` and the landing's and wiki's
`.w-foot` strips render off trade-agents.com only.

**Both bars are one row of plain links (2026-10-03).** Asked for: "Sectors is
unnecessary, drop it; put Macro's pages at the top level." So the yStocker
header and the Markets bar list Markets, Valuation, DCA, Companies,
Commodities, 13F, Fed, Rates, Housing, P/E, Odds, Daily, Videos — no menus.
The 26 peer groups are still listed on Valuation, and a sector page marks
Valuation as current. Macro's pages take their short labels, with the long
name as the tooltip. Both bars are built from one list in their template.
Thirteen links fit because the header's links went from px-3/gap-1 to
px-2/gap-0.5 and Share became its icon. Its switch point moved from 1280/1400
to 1320/1420, measured: 1301px signed out in English, ~1410 signed in with a
name the chip truncates. With no menu left in the row, the Markets bar
scrolls sideways at any width it does not fit, not just on a phone.

**On a phone the masthead's links get a row of their own.** Below 960px (880px
signed out) the bar has no room for them. Before, a phone then had no way to
Docs, the Research Lab, Samples or Pricing from the header at all. Off the
dashboards the same `<nav>` becomes a second row inside the masthead, pinned
with it and scrolling sideways like the Markets bar. It is a grid, not a wrap,
because a wrapped flex line is only as tall as its items and the bar's own row
shrank to its 36px buttons. `--w-top-h` grows to match (114px, 106px on a
phone), and `PhoneHeaderTests` asserts that it equals the rows drawn plus the
masthead's 1px rule. A dashboard does without the row: the Markets bar holds
that slot, and a third pinned row would be a quarter of a phone's screen. The
footer carries the links on every page, and on a phone they are a grid of 40px
rows rather than a line that wraps wherever a label runs out. The landing's
sections get a `scroll-margin-top` in the row's range, since their top padding
alone had cleared a 61px masthead and not a 106px one. A docs section now
carries its own too: links target the `<section>`, and the existing `h2` rule
had never applied to one.

**A reader of a dashboard on trade-agents.com without TradeAgents Pro meets a
reading wall** (`_ta_wall.html`, base.html's `_ta_wall`) — signed out, or since
2026-10-04 signed in with no trial or subscription (see "TradeAgents Pro"). The
top of the page reads as usual. Below the fold everything in `<main>` sits under
a blur, with a card leading to the free trial: through sign-in for a signed-out
reader, who also gets "Already subscribed? Sign in" back to this page. With Pro
switched off it is the old card that signs the reader in and returns them
(`/login?next=<this page>`). The
footer stays readable. It is **soft by choice**: the data is still in the page
and in the APIs, since it is public market data and the wall asks for an
account rather than protecting anything, so it walls nothing server-side. The
fold is one screen down (`max(560px, 72vh)` from `<main>`'s top) unless a page
marks it with `data-wall-start`. /markets does, on the section after its AI
brief and index cards, and a small script keeps the mark measured as the panels
above it load. The overlay is `position: absolute` in `<main>`, so it grows with
the page, and it sits at z-index 15, under the pages' own pinned bars (/markets'
chips 30, /history's `#sectionNav` 20). It stands down (`hidden`) while less of
`<main>` lies below the fold than its card and padding need: its box then ran
out of `<main>` over the footer and the sticky card stretched the page, as on
/companies searched down to one result (2026-10-04). Its script re-measures
whenever `<main>` resizes, so the wall returns as the page grows. Those stay usable and take the reader to
walled sections. `<main>` becomes `position: relative` only while walled;
measured on every walled dashboard, nothing else in it used `<main>` as its
containing block. A page with no data of its own sets `{% set no_wall = true %}`:
/guide, /lookup, /videos, /assets and /posts (sign-in only anyway), and the
error, warming and unsubscribe pages, whose one job is a message. Never walled:
stock.li-family.us, a subscriber, a trial or a VIP, and every TradeAgents page (the landing,
docs, research and shared reports, the last of which exist to be read without
an account). `check_wiki_pages` asserts each of those.

One column costs width somewhere, and it is paid in two places. The docs'
1260px `.w-wide` is capped to 1180 there (a 630px prose measure). And the
heatmap's own 1800px, and the dashboards' 1475px `app-container`, give way on
trade-agents.com only. The dashboards' Tailwind breakpoints are viewport-based,
so at 1440px `/markets` still sets eight index cards to a row in the narrower
column and truncates a few names — which is what a 1280px laptop already shows
on yStocker. Everything that sticks under the header reads `--w-head-h`, the
masthead's height (`--w-top-h`, which includes the links' row on a phone off the
dashboards) plus the Markets bar's (`--w-sub-h`, 45px on a dashboard and
0 elsewhere). That covers the docs sidebar and contents, the run page's jump bar
and `#agDock`, `/markets`' section bar, `/history`'s `#sectionNav` and
`/guide`'s contents list, each overridden next to the rule it re-measures.
`--w-head-h` is declared on `<body>`, not `:root`, because a custom property's
`var()`s resolve where it is declared.

Three traps. The masthead must carry no `data-nav` attributes (base.html's
breakpoint CSS selects `header [data-nav="desktop"]` and would hide it), and a
test probing for the dashboards' own bar cannot use that string either — the
inline CSS spells it on every page — nor `id="refreshBtn"`, which the Markets bar
carries too; `check_wiki_pages` looks for the drawer's `id="refreshBtnMobile"`.
Likewise the account menu's marker is its markup, because the masthead's script
spells `data-w-acct` on every page. And under 520px a signed-in reader's CTA gives
way to the account button — the two do not fit beside the wordmark at 390px.

**A `scroll-padding-top` on `<html>` covers the pinned header's own controls.**
It is meant to keep a jump's target clear of the sticky bars, but it applies to
anything scrolled into view, the header's buttons included. With /markets'
padding, opening ticker search from 2,200px down a phone scrolled the page
1,100px up to "reveal" an input that was already on screen. Focusing the theme
button scrolled it too, by 440px on a desktop at the 108px yStocker has always
used. So on trade-agents.com /markets zeroes it and puts the clearance on what is
scrolled to: `scroll-margin-top` on everything in `<main>` except its own pinned
chip bar. `tests/test_wiki.py`'s `PhoneHeaderTests` pin this and the two phone
rules above, since all three are CSS that no rendered-HTML check can see.

`/login` now sends a reader who is already signed in to a safe `next`, else to
the run form on trade-agents.com (it used to be `/markets` on every host), and
both it and the page's post-Google script accept `next` only as a path on this
site (`routes._safe_next`): an absolute URL, `//host`, `/\host` or a tab between
two slashes would make sign-in an open redirect.

The landing names `https://trade-agents.com/home` as canonical and `og:url`,
since every signed-out road — and a crawler is always signed out — now ends
there; the preview image's absolute URL is built on `_share_base()`, which forces
https because no app here installs ProxyFix.

**The sample reports publish a portfolio run's first half only.** Since
2026-08-30 `_run()` attaches the holder's `/assets` portfolio to every run by
anybody with positions, and `_is_showcase` used to refuse any such run outright.
That quietly froze the landing's samples at 08-29: on 2026-09-27, 35 of the latest
60 finished runs were excluded, and the rolling 60-run window was about 25 runs
from showing nothing at all. Now they are listed, and `showcase_job()` — the one
door for both the JSON and the PDF route — serves them through
`portfolio_blind_report()`: title and stamp, every analyst turn, both researchers
and the Research Manager, then a note in the report's own language. The Trader,
the three risk debaters and the Portfolio Manager are withheld, because they are
exactly the five prompts TradingAgents' `get_portfolio_block` feeds, all
downstream of the Research Manager. The rating still shows; the listing's fields
are unchanged. `TestPortfolioWiring` reads that wiring out of the TradingAgents
checkout, so a seat that starts receiving the block fails a test here instead of
publishing. On the 35 real runs the block's own vocabulary (BREACH, INDETERMINATE,
持仓上限) occurred only in the withheld turns. The first `SHOWCASE_NEWEST` (4)
samples are always the newest runs and the rest is the hourly draw, so a finished
run reaches the landing within the pool's 60s TTL.

**The page list is a registry, the bodies are files.** `ystocker/wiki.py` holds
every docs page and post — slug, group, dates, and titles/summaries/tags as
`{"en", "zh"}` pairs — and the sidebar, pager, index and browser tab all read it.
Bodies live in `templates/wiki/docs/<slug>.html` and
`templates/wiki/research/<slug>.html`. `tests/test_wiki.py` asserts both
directions: an entry with no file is a 500 on click, and a file with no entry is
an orphan that silently stops being linked.

**Long-form text is paired `data-l` blocks, not i18n.js keys.** A paragraph per
key would be hundreds of entries per article, so both languages are in the markup
(`<div data-l="en">…</div><div data-l="zh">…</div>`) and a two-line rule in
`base.html` hides the one `<html lang>` does not name. That attribute is set by
i18n.js from a blocking `<head>` script, so the right block is the first one
painted, and `I18n.setLang()` flips it with no re-render. The trap is that a
missing Chinese block is not a visible gap on the English page — it is a
paragraph that vanishes from the Chinese one, which no English reader would ever
notice — so `test_wiki` requires equal en/zh counts in every wiki file and in the
landing partials. i18n.js keys remain for the few strings outside these pages
(`nav.docs`, `nav.research`, `wiki.search_ph`, `agents.docs_models`).

**Every enforced number is passed in, never typed.** `routes._wiki_facts()`
reads free runs, the site-wide ceiling, chat and share limits, the share-link
lifetime, debate rounds, the roster, the model table and the pack ladder from the
modules that enforce them, the same reason `/guide` reads its prices from
`credits`. A docs page that said "3 free runs" in prose would go on saying it
after the quota moved. `tests/check_wiki_pages.py` asserts the pricing and sharing
pages quote the live values.

**The roster is the runner's, not `agent_roles`'.** `wiki.desk()` is built from
`agents.BASE_ANALYSTS` / `ASTOCK_ANALYSTS`, mapping the package's `social` to the
report's "Sentiment Analyst". `agent_roles.ROLES` still carries the Fundamentals
Analyst so pre-2026-08-30 reports render its turn; listing it would advertise a
seat nobody has run since quality and valuation replaced it. `wiki.ROLE_NOTES`
describes each seat from the fork's own prompts — the sentiment analyst reads
StockTwits and Reddit as well as headlines, and the quality/valuation analysts
only narrate numbers code computed — rather than from what the names suggest.
The same pass fixed three strings that still said A-share runs had "seven"
analysts; the roster is six, nine for A-shares.

**Research Lab posts are dated to the measurement, and sourced.** Each is built
from a commit message or module docstring that already recorded the finding —
`bae8c9d`/`b54023e`/`8bd8fed` (GICS), `fec1338` (forward history), `27dd9c9`
(TSM), `4ef9a69` (the ledger), the `/assets` section above (look-through) — with
the numbers quoted as measured and the date on the post. Every post ends with
"Check it yourself": the live page that shows the result, and the test that pins
the fix. No post publishes an agent performance number; the decision-ledger post
says why.

Three CSS traps, all in `static/wiki.css` (hand-written, because Tailwind here is
compiled and a class only these pages use can be missing from the bundle):

- **`hidden` loses to any author `display`.** The UA's `[hidden]` rule has the
  specificity of one class and wiki.css loads after it, so `.w-log li
  { display: grid }` kept the A-share rows showing on a US ticker and the sidebar
  filter hid nothing. `.w-run [hidden]` / `.w-docs [hidden]` restore it.
- **Chinese display lines broke mid-word** (投 / 研), because CJK may break
  between any two characters. Headings get `word-break: keep-all` with
  `overflow-wrap: anywhere` as the net, and a `<wbr>` where a long run needs a
  break point.
- **The page plane is on `<body>`** (`{% block body_class %}` in base.html), so
  the grid runs edge to edge under a centred column without a 100vw trick, which
  overflows by the scrollbar's width on Windows. `html.dark body.w-paper` is
  spelled out because Tailwind's `.dark .dark\:bg-slate-950` would outrank it.

Fraunces is loaded **non-blocking** (`media="print"` swapped on load), unlike the
Inter link in base.html: it only changes headings, each of which has a system
serif to fall back on, so a slow font host costs Georgia for a moment rather than
a blank page. `cache_bust` is computed once at app start, so a CSS edit needs a
restart before a browser that has the service worker will see it.

The three template guards (`test_template_ids`, `test_theme_classes`,
`test_deferload_anchors`) now `rglob` rather than `glob`, because a guard that
reads only the top level passes on a subdirectory it never saw.

Tests: `tests/test_wiki.py` (no app — registry, content files both ways, en/zh
pairing, the roster) and `tests/check_wiki_pages.py` (every page through the Flask
test client; the landing shown to exactly one audience; 404s that keep their
navigation; trade-agents.com's shell and its one column on every page there, the
Markets bar on the dashboards, and none of it elsewhere; sign-in's `next` never
leaving the site). `check_` so `unittest
discover` skips it. `test_wiki`'s en/zh pairing guard reads every template, not a
list of them, so a new page in the shell is covered without joining one.

### The AI Markets Brief (`/api/market-brief`)

The card at the top of `/markets` is generated from **every** dashboard —
`/markets`, `/evaluation`, `/commodities`, `/13f`, `/fedwatch`, `/housing`,
`/multiples`, `/fed` — collected server-side by `routes._collect_brief_sources()`
and formatted by `brief.py`. Output is Markdown, rendered through the shared
`static/markdown.js`, because the point of the brief is a table per section.

It is deliberately **not** a mode of `/api/daily-summary`. That endpoint still
feeds the subscriber email (daily_email.py) and a past date on `/daily`, and is
short prose by design, where the brief is a dozen tables. Two consumers, two
shapes, two routes, two DynamoDB key namespaces (`{lang}_brief_v1` vs
`{lang}_{market}`).

**Nothing before the first `## ` is served.** The prompt asks for exactly N
sections and forbids a preamble (不要寒暄 / "no preamble"), and on some days the
model writes one anyway: a title, "尊敬的专业投资者：", a sentence saying the
report is based on the snapshot. On `/daily` that made the report open like a
letter. `brief.strip_preamble` drops it at generation and when a stored row is
read back (`_brief_from_ddb_item`), so a copy saved before the rule is fixed too.
A brief with no `## ` at all is left whole.

**`/daily` shows one report at a time.** It has a tab each for 美国市场 and
中国 / 亚洲, a contents list built from the report's `## ` sections (a chip row
on a phone), and a 760px reading column at 16px. The two reports used to sit
side by side at 13px in two ~470px columns. `enhanceReport()` supplies what
`markdown.js` cannot: a column is right-aligned when 60% of its filled cells
are figures, and only an explicitly signed figure is coloured, so "90%" (a
range position) stays uncoloured. The report is fetched at boot rather than
after the data cards, which it never depended on. The contents links keep their
target in `data-target`, because a language switch has i18n.js rewrite every
same-page link to `/daily?lang=zh#dr-us-3`.

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

### The earnings calendar (`/earnings`)

Added 2026-10-04. The page shows one week, Monday to Friday, of every US-listed
company reporting. Each row has the consensus EPS (Zacks, via Nasdaq), how many
estimates make it up, last year's EPS and the growth that implies. Once a day
has passed, it shows the reported EPS, the surprise and the day's beat rate.
Rows are largest first, with market-cap, Followed and My-holdings filters, and
every row opens that company's Fundamentals tab. `/markets`' "Reporting soon"
card (`/api/upcoming-earnings`, `earnings.py`) is unchanged: it gives the
followed companies' next dates from Yahoo's own fields, and now links here.

**One request per day, from Nasdaq**
(`api.nasdaq.com/api/calendar/earnings?date=`). Measured from the box on
2026-10-04: 307 companies on Oct 29, 188 on Oct 27, 15 on Oct 13, none at a
weekend. The client matters:

- With no User-Agent, a plain `requests` call hangs until it times out.
- With a browser User-Agent it is answered, but in 2–3 s.
- curl_cffi impersonating Chrome, already installed for yfinance, is answered
  in 0.03–0.14 s. `earnings_calendar._http()` uses it, behind the `nasdaq`
  breaker in fetchguard.

The laptop cannot reach the API. Fetch real days on the box.

**A past day is a different shape, not a superset.** The same request returns
the reported EPS and the surprise after the fact, and drops the time of day and
last year's figures. `parse_day` reads whichever fields are present. The page
switches columns per day: estimate, last year and growth before; reported,
estimate and surprise after. Money arrives as text. `($0.05)` is a loss and a
blank is absent, never zero. Growth from a loss is `None` ("n/m"), not +200%.
Share classes come as `GEF.B` and are rewritten to Yahoo's `GEF-B` by
`portfolio_csv`'s narrow rule, so the /history link resolves.

**One list, largest first, with the time of day as a badge.** Grouping by time
buried the biggest names. On 2026-10-29, 249 of 307 rows had no time supplied,
Apple, Amazon and Mastercard among them, and they sat under 42 smaller
pre-market reporters.

**The request path never fetches.** There is one file per day in
`cache/earnings_calendar/`:

- Future days are re-read every 6 h.
- The three days up to today are re-read hourly, since that is when actuals
  land.
- An older day is final once it has been read three days after the fact.

A missing day is queued for one worker per process, paced 1 s apart, and the
API answers 202 with whatever days are already in. The page polls 20 times, 3 s
apart, and ends on a drawn state. A failed day is not queued again for 10
minutes, so a polling page cannot turn one failure into a request every few
seconds. A busy day that comes back empty keeps its copy, because the vendor's
"no rows" looks the same as a glitch. The background thread keeps last week to
three weeks ahead fresh: about 25 requests cold, then a handful an hour. Weeks
are clamped to ±8 around today, so a crawler walking `?week=` back to 1990 is
answered with the nearest allowed week rather than fetched.

**It is in the Companies section, not the nav row.** The header fits 13 links at
its 1320px switch point with 19px to spare. A fourteenth link would push 1440px
laptops onto the compact menu. So `/earnings` marks Companies as current, as a
sector page marks Valuation. It is linked from the mobile drawer, both footers
and the `/markets` card. `held` comes from the reader's `/assets` positions,
best effort: an unreadable store costs the highlight, not the page.

Tests: `tests/test_earnings_calendar.py` (21, Nasdaq's real rows: losses,
blanks, share classes, the dates, the cache) and
`tests/check_earnings_endpoints.py` (7, hermetic). Not a DynamoDB table: every
day can be fetched again.

### Insider trades (`/insiders`)

Added 2026-10-04. The page shows what officers, directors and 10% owners bought
and sold in the open market, from their SEC Form 4s:
- buys (code P) and sells (S) at the followed companies;
- over 7, 30 or 90 days, largest or newest first;
- cluster buys on top: companies where two or more insiders bought in the window.

Each row has the trade date, insider and role, shares, weighted price, value,
stake change and a 10b5-1 badge. It links to the filing on EDGAR and opens the
company's Fundamentals tab. Awards, exercises, tax withholding and gifts are
kept, but only on a company's own view (`/api/insiders/<T>`), which the page's
search opens for any SEC ticker on Enter.

**Per issuer, not per day.** SEC's daily form index lists a Form 4 once (3,097
on 2026-10-02), often under the reporting owner's CIK, so it cannot be filtered
by issuer. An issuer's submissions JSON can: `filings.recent` covers at least a
year (JPMorgan's 26,300 entries and 4.5 MB reach exactly a year), at 0.02–0.34
s a request from the box. `primaryDocument` names the XSL-rendered HTML
(`xslF345X06/form4.xml`), the 13F trap again. The raw `ownershipDocument` is the
same file name at the accession root, under the *issuer's* CIK. The accession
prefix is the filing agent's, and its path 404s.

**What it costs.** Measured 2026-10-04:
- **Universe**: the ticker cache's 308 tickers are 215 issuers. 51 funds are
  skipped on `"Quote Type"`, and 42 have no CIK (40 Tokyo listings, EA, TCEHY).
- **Filing rate**: a random 30 issuers filed 23 Form 4s in 7 days, 61 in 30
  and 270 in 90, so about 15–23 a day across the universe.
- **Per refresh**: one submissions request, plus one per Form 4 filed within 90
  days and not yet cached.
- **Cold start**: an issuer's first fill looks back 30 days, so a cold box's
  first pass is ~215 + ~440 requests (~16 minutes at 1.5 s). Days 31–90
  (~1,500 requests) come on the next pass.
- **Steady state**: a parsed accession is never fetched again, and files keep
  120 days. The sweep starts a minute after boot, checks issuers due after 6 h
  (`INSIDERS_RECHECK_HOURS`), paces 1.5 s through `sec13f.edgar_get`, and stops
  at the first `CooldownActive`. That is about 860 submissions requests a day.

**What the filings do not say.** Each of these is pinned on a real filing in
`tests/test_insiders.py`:

- **Rows**: a filing's lines become one row per code, security and ownership
  line. A plan sale arrives as one line per price band, and a filing can span
  days, but rows never mix share classes. Berkshire's Lennar filing bought
  Class A at ~$82 and Class B at $80 on the same days; its total, $53,882,588,
  matches OpenInsider's to the dollar.
- **Prices**: a price given only in a footnote is unknown, not $0, and has no
  value (ZDGE, PYXS).
- **Code P** is "open market or private purchase". A purchase in an offering or
  a private placement (from the footnotes) is badged and kept out of cluster
  buys.
- **Joint filers** count as one buyer (Berkshire and Buffett; PYXS's four
  GordonMD entities). Insiders are grouped on shared reporting-owner CIKs.
- **Amendments**: a 4/A replaces its original only when it restates the
  non-derivative lines itself. TPR's 4/A of 2026-09-03 restates only the
  derivative table, and dropping the original would delete an award and a tax
  withholding nobody amended.
- **10b5-1** is the `aff10b5One` box (written `true` or `1`) or a footnote
  saying so. A footnote that denies it is not a plan, and the box alone marks
  only the P and S lines.
- **Stake**: the trade as a share of the holding it moved. A buy is measured
  against the holding after it (100% is a new position), a sale against the
  holding before it (100% is a full exit). Measured against the holding after,
  a sale has no ceiling: NVIDIA director Mark Stevens would read 141% where he
  sold 58%.

Parsed with lxml (entities and network off), not ElementTree, because pyexpat
cannot load on the dev Mac (gics.py says why) and lxml is already a dependency.

**The request path never fetches.** There is one file per issuer in
`cache/insiders/`, plus `_universe.json`, which the sweep writes.
`/api/insiders` answers 202, with what is in, until every followed issuer has
been checked once. `coverage` says how many are in and from when the record is
whole (`complete_from`): right after the first pass, a 90-day view holds 30
days, and the page says so rather than letting the gap read as quiet.

`/api/insiders/<T>` answers a cold company 202 and queues just that issuer for
one worker per process:
- a claim file (`.lookup-<T>`) stops the other gunicorn worker queuing it too;
- a failure pauses it for 10 minutes;
- look-ups are capped at 300 a day (`INSIDERS_DAILY_LOOKUPS`, on quota.py's
  flocked counter), since any of ~8,000 SEC tickers can be asked for.

A followed company is the sweep's to refresh; only a stale look-up is queued
again on view. A looked-up company joins the market-wide feed for 24 h after
its last check, marked not followed, so the Followed chip has something to
filter. It is not swept, so it drops out rather than going stale.

It is in the Companies section, as `/earnings` is: Companies is marked current,
and it is linked from the mobile drawer and both footers. `held` comes from
`/assets`, best effort.

Tests: `tests/test_insiders.py` (65, on 26 real Form 4s fetched from the box,
in `tests/fixtures/insiders/`, with each filing's listing row in `index.json`)
and `tests/check_insiders_endpoints.py` (15, hermetic). Not a DynamoDB table:
every filing can be fetched again.

### Prediction markets (`/predictions`)

Built 2026-10-03 from three ideas on alphascope.app/predictions, an AI-forecast
site for Polymarket and Kalshi. First, a forecast without the live price beside
it is half a thought. Second, the gap between the two is where a thesis starts.
Third, write the forecast down before the market resolves so it can be scored.
The page has four parts:

* **The board** — the busiest open markets on the events this site covers (Fed,
  inflation, jobs, growth, stocks, companies/IPOs, commodities, crypto, policy),
  with topic chips, venue and sort filters, liquidity or open interest, and a
  link out to each venue.
* **Biggest moves today**, one row per market.
* **The Fed, three ways** — each upcoming FOMC decision as the ZQ futures,
  Polymarket and Kalshi price it. Also shown as a card on `/fedwatch`.
* **The AI read and its track record** — see below.

In the top-level nav, after P/E.

| Module | Job | Pure? |
|---|---|---|
| `odds.py` | Both venues normalised, topics, card selection, movers, the Fed cross-check, the AI prompt and parser, Brier scoring | **yes** |
| `predictions.py` | Fetch both venues, cache, background thread, `peek()` | no |
| `odds_ai.py` | One grounded Gemini read per market, cached and shared, 202-and-poll | no |
| `odds_ledger.py` | Every read recorded with the market's price, settled when it resolves | no |

**Both APIs are public and keyless**: Polymarket's Gamma API and Kalshi's trade
API v2, each behind its own `fetchguard` breaker (`polymarket`, `kalshi`).
Gamma is read by tag, busiest first (`PM_QUERIES`). Kalshi has no category
filter on its events endpoint, and its Economics category alone has about 900
series, mostly dead. It is read from a curated list of series instead
(`KS_SERIES`): the Fed, CPI, payrolls, GDP, recession and the year-end index
ranges. A refresh is about twenty requests every ten minutes. Gamma repeats
every market's full description inside every listing, so the nine listings
measured 10.4 MB of JSON (about a tenth of that gzipped on the wire). Each is
parsed and dropped before the next is fetched.

The usual rules hold. `peek()` never fetches, and a cold cache answers 202
while one refresh is kicked. A worker re-reads the cache file when its mtime
moves, because under `--preload` the refresh thread lives only in the master.
A venue that fails outright has its previous events carried forward, labelled
with `stale_since`, rather than the board losing half its rows.

Traps in the two vocabularies, each found on live data:

- **An absent change field is unknown, not zero.** Gamma omits
  `oneDayPriceChange` on markets that plainly moved: October's "No change" had
  +49pp on the week and no daily field. Reading it as 0 hides the biggest mover.
- **Open events hold closed and placeholder markets.** "How high will inflation
  get?" keeps its resolved rungs at a price of 1. "Largest Company end of
  October" carries "Company F" through "T", each `active: false` at a 0.5 price
  nobody quotes. Both are dropped. Whether an event is a single question is
  read *before* that filter, or a group left with one live outcome is labelled
  "Yes" instead of "NVIDIA".
- **The price shown is each venue's own display rule**: the midpoint while the
  spread is at most 10¢, otherwise the last trade. Polymarket's `outcomePrices`
  already is that number. Kalshi's is computed by the same rule.
- **A Kalshi contract pays $1**, so its contract count is its notional. That is
  close enough to rank one board on, but not to quote as the same measurement,
  so the card labels Kalshi's depth as open interest.
- **Rolling daily ladders crowd out everything.** "Bitcoin above ___ on
  October 6" are the busiest contracts on either venue. Equities, commodities
  and crypto markets must run at least a week to be listed. Scheduled macro
  releases are exempt, since a CPI market closing tomorrow is the one to see.
  `rank_board` caps any one topic at fifteen.

**The Fed cross-check compares per-meeting decisions, never the grid.**
`/fedwatch`'s grid is *cumulative*: the chance the range is still unchanged by
December. Both venues price the decision *at* each meeting. On 2026-10-03,
December read off the grid would have been 14.6% "no change" beside
Polymarket's 23.5%: a nine-point disagreement between two different questions.
So the futures are restated per meeting from `change_bp`, split between the
two 25bp steps either side as `fedwatch._probability_tree`'s leg already does
(`odds.futures_split`). That leg can only ever name two outcomes; the venues
price the tails too, which is the point of showing them together. Venue odds
are normalised to 100% and the overround is kept (shown on hover). The expected
move counts a "50bp+" outcome as exactly 50bp, so it is a floor. Measured that
day: the venues agreed with the futures on October (2.8pp apart) and not on
December (futures 81.6% hike, Polymarket 72.9%, Kalshi 68.8%).

**The AI read is price-blind on purpose.** The first live read was shown the
prices. It answered "No change 82.5%" to a market at 82.5%, and the prompt's
own format example said 82.5 as well. A read that copies the market has no gap
to show and gives the ledger nothing to measure. So `odds.build_read_prompt`
gives the model the question, the outcomes, the rules and the clock, but no
price, volume or recent move. `tests/test_odds.py` asserts that none of them
leak. The page sets the read beside the price afterwards. The second live read
said 90% against the market's 82.5%, with sources.

How a read is made:

- One `gemini-2.5-flash` call with Google Search grounding returns one JSON
  block carrying an English and a Chinese summary. One call serves both
  languages, and one event cannot get two disagreeing forecasts.
- The parser refuses rather than repairs. A missing outcome, or a full
  one-of-many set summing outside 90–110, is an error the reader can retry.
- **0.5 is half a percent.** Fractions are accepted only for a complete
  one-of-many set summing to about 1. Anywhere else a "fraction guess" would
  record a long shot at 0.5% as 50%.
- POST claims the market with an `O_CREAT|O_EXCL` marker, starts a thread and
  answers 202. The page polls with a bounded loop that ends on a rendered
  state. A marker older than four minutes is a dead run.
- A fresh read (12 h) is shared by everyone and holds nothing about who asked.
  GET is open; POST is gated like other model calls (signed in, plus the
  agents' allowlist when one is set).
- Starting a read spends `quota.try_consume_odds_read`: 5 a day per user, 25 for
  a VIP, 60 for everyone together. The counter lives in the same locked file as
  runs, chats and shares. Asking for a market that already has a fresh read, or
  one being made, spends nothing.
- `PREDICTIONS_AI=0` is the kill switch. `PREDICTIONS_AI_MODEL`,
  `PREDICTIONS_AI_DAILY_LIMIT`, `PREDICTIONS_AI_VIP_DAILY_LIMIT` and
  `PREDICTIONS_AI_GLOBAL_DAILY_LIMIT` tune the rest.

**The ledger is an observed series.** Each read is written down when it exists:
the model's probabilities, the market's from the same snapshot, the time and
the model. A thread every six hours settles rows whose markets have resolved
(Gamma `closed` with prices at 0/1; Kalshi `result`). Both the AI and the market
are scored by Brier over exactly the same outcomes. A void outcome drops from
both scores; a row with nothing left is `void`. With nothing settled, the means
are `None`, never 0, which would read as perfect. No page claims the reads are
good; the ledger is where that gets found out.

Key schema: `bucket` (`YYYY-MM`) HASH + `sk` (`<iso>#<event key>`) RANGE, one
JSON `payload` attribute, read by a Query per month back. It degrades to the
disk mirror (`cache/predictions/ledger.json`) when the table is missing, and the
two are unioned on read. A settled copy beats an open one.

```bash
aws dynamodb create-table --table-name ystocker-prediction-reads --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=bucket,AttributeType=S \
                          AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=bucket,KeyType=HASH \
               AttributeName=sk,KeyType=RANGE
```

The table is not in `deploy/cloudformation.yaml`, like every other hand-made
table, and IAM already grants `table/ystocker-*`. No TTL.

The page's cards, Fed table, AI panel and ledger are drawn by
`static/predictions.js` (`const PM`, script-scoped like `I18n`) with
hand-written `static/predictions.css`, since Tailwind here is compiled and a
class built in JS would be missing from the bundle. Every venue and model
string is escaped before it reaches innerHTML. A collapsed card shows a
ladder's rungs nearest 50% and a bucketed field's likeliest buckets. Showing
the first four instead put "Anthropic IPO Closing Market Cap" up as four
buckets under 1%, with its 94% bucket hidden.

Tests: `tests/test_odds.py` (58, real 2026-10-03 payloads from both venues),
`tests/test_odds_ledger.py` (17: the read's lifecycle, settlement, the quota),
`tests/check_predictions_endpoints.py` (16 through the Flask test client,
hermetic) and `node tests/check_predictions_render.mjs` (54, the real i18n.js
and predictions.js in a vm).

### Crawlers (`crawlers.py`, `/robots.txt`, `/sitemap.xml`)

On 2026-10-04 Meta's AI crawler (`meta-externalagent`) rendered
trade-agents.com's dashboards and called every API on them. That was about
6,000 requests in a few hours, including:
- Fundamentals and DCA rebuilds, each an EDGAR fetch or six Yahoo reads, for a
  client that never read the answer;
- 179 forced `/dca/<T>/refresh` rebuilds;
- DCA registry churn that evicted real readers' names from the 60-slot ranked
  universe.
There was no robots.txt route. Three layers now, with the rules pure in
`crawlers.py`:

- **`/robots.txt` and `/sitemap.xml`, per host** (`_share_base()`, so each
  brand names its own pages). The API, the refresh routes and sign-in are
  disallowed. `/api/agents/shared/` is allowed back, because a shared report's
  preview card is fetched by exactly these bots. The sitemap lists the landing,
  docs and Research Lab posts on trade-agents.com, and the dashboards on both
  hosts.
- **A 403 for a declared crawler on `/api/` and on GET refresh routes**
  (`_refuse_crawlers`, ahead of every other hook). robots.txt asks; this
  refuses the bots that do not ask first. "Declared" means the user agent says
  so: a token list plus a `bot/`/`crawler`/`spider` shape, which matches
  neither "CUBOT X30" (a phone), curl nor python-requests. The share card and
  the write door (`/api/posts`, `/api/inbox`) are exempt; their callers are
  scripts.
- **A daily cap on Fundamentals builds** (`FUNDAMENTALS_DAILY_BUILDS`, 600, on
  quota.py's flocked counter), taken when a build slot is reserved. A crawler
  that ignores both layers still cannot turn the build budget into an all-day
  EDGAR sweep. The cold path answers 503 `daily_cap`, and the page says "ready
  tomorrow" rather than spinning. A ticker already cached is unaffected.

`shared.html` carries `noindex, nofollow`, since a capability link is meant to
be pasted, not found. Tests: `tests/test_crawlers.py` (9) and
`tests/check_crawler_endpoints.py` (6, hermetic, both hosts).

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

**The target range gets its own refresh cycle, and it must not get its own
update.** The two halves of the fedwatch payload have opposite needs: the ZQ
curve reprices all session and wants the 4-hour TTL, while the target range is a
*fact*, changes eight times a year, and is two small FRED CSVs. On 2026-09-17 the
FOMC moved to 3.75–4.00, FRED published the row, and the page showed 3.50–3.75
for hours — the payload happened to have been built ninety minutes earlier. So
`probe_target_range()` reads just DFEDTARL/DFEDTARU every 30 minutes, and every 5
within `_HOT_WINDOW_DAYS` of a decision (`schedule` is in the payload because
`meetings` holds only *upcoming* decisions — the one that caused the change is
dropped from it on exactly the morning it lands, and `fetch_fomc_meetings()` is
not memoised, so reading the calendar per probe would mean scraping
federalreserve.gov 48 times a day).

A changed range then triggers a **full rebuild**; it is never patched into the
cached payload. `lower`/`upper` are not merely displayed — they are the baseline
`_expected_path()` projects every meeting from, and they are written into
`ystocker-fedwatch-history` as `base_lower`/`base_upper`. Patching them alone
would render a new range above probabilities computed from the old one and then
bank that pairing into a series that cannot be recomputed. A failed probe returns
`None`, which is "don't know" and neither triggers nor suppresses a rebuild;
comparison is rounded, since `3.75 != 3.7500000001` off a CSV would otherwise
rebuild the ZQ curve every 30 minutes for ever.

**And the probe is worthless without the disk re-read.** Under `--preload` the
refresh thread runs only in the master, so each worker forks a `_cache_data`
snapshot and — before this — served it for the whole 4-hour TTL without ever
looking at disk again. The master would detect the move within minutes and every
reader would still be told the old range. `get_fedwatch_data()` now compares
`_disk_mtime()` against the mtime its copy came from (one `stat()`, no parse) and
reloads only when the file has actually been rewritten. Ordering is by the
payload's own `_ts`, so a restored backup or a clock step cannot roll a good cache
backwards. Tests: `tests/test_fedwatch_range_probe.py` (27, no app/network).

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
- **Light / dark theme**: All 8 apps toggle. Each stores `<app>_theme` in
  `localStorage`, defaults to **dark**, and flips `class="dark"` on `<html>` from a
  blocking inline script at the very top of `<head>` — above every stylesheet, or
  one dark frame paints before the flip. `prefers-color-scheme` is deliberately
  **not** consulted: it would change the site's appearance for existing readers
  who never asked.

  yStocker was the last one converted and is the interesting case, because its
  dark look was 2,715 *hardcoded* utilities (`bg-slate-900`, `text-slate-100`)
  rather than `dark:` variants. Three things Tailwind's `dark:` variant cannot
  reach had to be handled separately, and each has its own mechanism:

  - **Canvas** — a chart takes no CSS. `CT.c('<dark colour>')` in `base.html`
    maps a dark literal to its light counterpart, so each of the ~585 call sites
    is a mechanical wrap of the value already there. Unmapped colours pass
    through, which is what makes a mistaken wrap harmless. It is *not* a
    Chart.js plugin: v4 resolves `chart.options` through a proxy before
    `beforeUpdate` fires, and a blanket grey-out would flatten the axis
    colour-coding on the dual-axis charts, where a red right-hand axis is how a
    reader knows which line is CPI.
  - **Hand-written `<style>` blocks** — 14 templates style rendered Markdown,
    pipe tables and chips in plain CSS. These use the shared `--t-*` custom
    properties defined in `base.html` (light-first, `.dark` overriding), so a
    page cannot drift a shade from its neighbours.
  - **`toggleTheme()` reloads only if the page has a `<canvas>`.** The class flip
    restyles everything CSS owns instantly, but a Chart.js instance bakes its
    colours in at construction inside an async fetch callback that cannot be
    replayed. Chart-free pages toggle with no navigation.

  Re-running `migrate_theme_classes.py` / `migrate_chart_colors.py` (both
  idempotent) converts a newly added dark-only template. `/tv` is excluded by
  design — it is a standalone kiosk with its own CSS variables, asserted by
  `tests/test_theme_classes.py`.
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

- **Auto-refresh for a tab left open**: `static/autorefresh.js`, mounted only by
  `/housing` so far via `AutoRefresh.watch({meta, stamp})`. The server side of
  these dashboards already refreshes itself — housing re-downloads its eight
  upstreams every 24h from a background thread — but the *browser* never learned,
  so a tab opened on Tuesday still rendered Tuesday's payload on Thursday under a
  header confidently reading "Data as of Tuesday". Nothing was broken and nothing
  said so. Tests: `node tests/check_autorefresh.mjs` (35, no browser).

  Four things hold it together:

  - **It polls a stamp, not the payload.** `/api/housing` is 361 KB raw and ~100
    KB gzipped; polling *that* to answer "is there anything new" costs ~17 MB a
    day per open tab for data that changes once a day. `/api/housing/meta` is 43
    bytes and touches no upstream — two lock reads. It never 500s either: a stamp
    that cannot be read is indistinguishable from "nothing new", and erroring
    would only teach the client to back off from a page that is rendering fine.
  - **It reloads rather than re-rendering in place.** `housing.html` builds ~19
    Chart.js instances inside a single `async` IIFE closed over `data`, so there
    is no `render()` to call twice — but the deciding reason is that a *partial*
    in-place update on a numbers dashboard is worse than none: a KPI tile showing
    today above a chart still showing yesterday is wrong without looking wrong.
    A reload renders everything at one vintage, and the page's own cold-start path
    already reloads when data lands.
  - **A stamp disagreement between workers must not become a reload loop.** Under
    `--preload` the refresh thread lives in the master, so a forked worker serves
    the snapshot it inherited until it is recycled, and `get_cache_ts()` never
    consults disk once it holds one. So two workers can disagree, and "stamp
    differs → reload" ping-pongs forever between the one that has the new payload
    and the one that does not. Three guards: the stamp must be strictly *greater*
    than the rendered one, the stamp it reloaded *for* is recorded in
    `sessionStorage` (which has to survive the very reload it authorises, so a
    variable will not do), and `MIN_GAP_MS` floors two reloads at 5 minutes.
  - **It does not yank the page out from under a reader.** A reload discards
    scroll position, the metro selection, every chart's range button and any open
    AI explanation. An interaction inside the last 60s therefore turns the reload
    into a dismissible offer, and the automatic path is reserved for a tab being
    *returned to* — which is the case the module exists for, and why the
    `visibilitychange` check matters more than the 30-minute timer (hidden tabs
    have their timers throttled to roughly once a minute). Dismissing claims the
    stamp, so the same news does not reappear every interval all day. The pill's
    text is set from JS, where `I18n.apply()` cannot reach it, so it re-reads its
    labels on `i18n:langchange` as well — otherwise an English pill ends up on a
    page switched to Chinese.

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
- **EC2 Instance**: resolve it, do not read it from here. The box is tagged
  `Name=ystocker-instance`, and that tag is the only durable handle:

  ```bash
  aws ec2 describe-instances --region us-west-2 \
    --filters "Name=tag:Name,Values=ystocker-instance" \
              "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text
  ```

  Amazon Linux 2023, `t3.medium`. **Do not pin the id anywhere that matters** —
  `deploy.sh` resolves it from the tag and only falls back to a literal. The box
  has now been rebuilt **twice** (2026-08-31, and again by 2026-09-12: the id
  went `i-0bb73b171210c002e` → `i-061f92cc5b31c7e72`). Each time the elastic IP
  moved with it, so the site never went down and nothing looked wrong; the only
  symptom was every SSM call failing with `InvalidInstanceId: Instances not in a
  valid state for account`, which reads like an SSM agent or permissions fault
  rather than a stale constant. That this has happened twice is the argument: a
  literal written down here is a fact with a shelf life, and the second rebuild
  was diagnosed from scratch because the first one's id had been re-pinned
  instead of removed.
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
- Templates extend `base.html` with Tailwind CSS dark mode (`class="dark"`), and
  are written **dark-first**: the bare utility is the dark value and the light
  counterpart is added alongside it (`bg-white dark:bg-slate-900`). Light mode is
  a three-tier hierarchy — page `slate-50`, card `white`, inset well `slate-100`
  — because mapping both page and card to white left every card as a floating
  border with no plane behind it.

## Known Pitfalls

- **Several agent sessions share this one checkout, so `git commit -a` commits
  other people's work.** There is no auto-committer and no hook — it is simply
  four Claude sessions in `/Users/yuanxili/workspace/ystocker` at once, each
  with files mid-edit in the same working tree. `625d2ef` ("history: give
  indicators a warm-up…") therefore also contains an unrelated `/evaluation`
  P/FCF sort fix and its test, pushed under a message that does not mention
  them: nothing was lost, but the attribution is wrong and reverting that commit
  would now undo a second change. It is not one-directional and not confined to
  this repo — the same sweep put four of another session's in-flight test files
  into `d7a4860` ("add more features") over in `/Users/yuanxili/workspace/
  TradingAgents`, a commit that contains no features.

  So: `git add <the files you actually touched>` for an ordinary commit, and
  read `git status` first — a modified file you do not recognise is somebody
  else's, still being verified. `git commit -a` is the one to never reach for,
  because it stages and commits in a single step with nothing in between to
  look at. `git add -A` is *not* in the same category: resolving a merge
  requires every conflicted path to be staged before `git merge --continue`
  will run, and naming eighteen of them by hand invites missing one. The same
  goes for editing rather than committing — check `git log --oneline -5` before
  starting on a module, because a commit from ten minutes ago means another
  session is probably still in it.

- **Tailwind's `.hidden` loses to a page's own `display`.** It is a plain
  `.hidden { display: none }` in the compiled bundle, and a template's `<style>`
  comes after it, so any hand-written rule of the same specificity that sets a
  display wins and the element never hides. On `/agents` that was `.ag-pill`,
  `.ag-btn` and `.ag-jump`: from 2026-08-17 to 2026-09-27 every report, the
  landing's samples included, carried a "recovered from stream" badge and an
  empty fallback-model pill whether or not either was true, and a running job
  offered its PDF. Nothing errors and the toggling code is correct, which is why
  it lasted six weeks. The fix there is `.ag-pill.hidden, .ag-btn.hidden,
  .ag-jump.hidden { display: none }`: two classes outrank one whatever the order.
  It is the class twin of the `[hidden]` trap wiki.css notes.
- **A Tailwind class pair is not a DOM token.** Light mode turned every hardcoded
  colour utility into a pair (`bg-slate-100 dark:bg-slate-800`), which is fine in
  a `class=` attribute and broken everywhere a template passed the same string to
  the DOM as a *token*: `classList.add`/`remove` are variadic and take one token
  per argument, `classList.toggle`/`contains` take exactly one, and a space in any
  of them throws `InvalidCharacterError`. A selector is worse — `closest('.a
  dark:b')` parses as a *descendant* selector, so it throws or silently matches
  nothing. This bit 42 call sites and one `closest()`, and it is invisible on page
  load: the throw happens on a click, or inside an IIFE whose `catch` swallows it.
  In `fed.html` it killed that page's entire init block, and the only symptom was
  a console line. Use the variadic form, or `toggleClasses(el, pair, on)` from
  `base.html`; for a selector use a `[data-*]` hook, which restyling cannot
  invalidate. `tests/test_theme_classes.py` fails on all three shapes and needs
  no browser.
- **`text-<hue>-400` is invisible in light mode.** The dark theme picks 400-level
  hues so they glow against near-black; `text-emerald-400` on white is 1.87:1, so
  a table of percentage changes reads as a smudge. Light counterparts land on
  **shade-700**, the first rung that clears AA on white for every hue in the set
  (emerald-600 is 3.4:1 and amber-600 only 3.0:1). Same trap in reverse for
  greys: `slate-500` is the muted-text floor at 4.76:1 on white, so both
  `text-slate-500` and `text-slate-600` collapse onto it rather than going paler.
  A fade overlay is the loud version of this — `linear-gradient(…, #0f172a)` over
  a white card renders as a **black block**, which is why `--t-fade-from/to`
  exist.
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
- **`animation: … both` makes every card a permanent stacking context, so a
  dropdown's own `z-index` cannot lift it.** `.fade-up` in `base.html` is
  `animation: fadeUp .35s ease both`, and the `both` fill mode means the
  animation's effect never stops applying — so each card is a stacking context
  for the life of the page, not just for 350 ms. An absolutely-positioned menu
  inside card A therefore resolves its `z-index` *within card A*, and card B
  further down the document paints over it however high that number goes. On
  `/agents` the model and thinking menus opened, rendered, looked correct, and
  silently swallowed every click: Playwright named the intercepting element,
  which is the only reason it was diagnosed rather than filed as a flaky click.
  The fix has to lift the **ancestor card** (`[data-sel-raise].ag-raised`,
  toggled in the listbox's own open/close so a closed control leaves the sticky
  sub-header on top), not the menu. Note the status filter never hit this only
  because it lives in the last card on the page — so "the existing dropdown
  works" is not evidence that a new one will.
- **A 13F fund's CIK is the entity filing its 13F-HR today, and nothing
  checks it at run time.** A wrong CIK does not fail; it shows someone else's
  book under the fund's name. The 2026-10-04 audit found 16 of 51 wrong:
  "Jane Street" was Barber Financial, "Capital Group" was Royal Bank of Canada,
  and "Coatue" was Pershing Square Capital Management. Check EDGAR's entity name
  before adding one. The table has three parts:
  - **`PREDECESSORS`**: firms whose filer changed. Periods up to the cutover
    come from the old entity only.
  - **`COFILERS`**: firms filing through several entities, summed by CUSIP.
    Vanguard is nine since 2026-03-31; Vanguard Group's own last report was a
    combination report that covered them all.
  - **`ALIASES`**: second names, resolved with `resolve_fund_name` and never
    fetched or counted. Each used to double a portfolio in the consensus tables.

  Three traps in the filings themselves:
  - **Units.** `<value>` is whole dollars for filings since 2023-01-03, but T.
    Rowe Price Associates, Baupost and Duquesne still file thousands, so
    `decide_value_unit` decides per filing. The date sets the default; a median
    value per share under $1 means thousands, over $50 means dollars. Storage
    is always thousands. The site showed Berkshire at $299 trillion until then.
  - **Amendments.** A 13F-HR/A is a RESTATEMENT, which replaces the original,
    or NEW HOLDINGS, which adds only what confidential treatment hid. Its cover
    says which. Preferring amendments blindly put Berkshire's 2025-03-31 at
    $1.1B.
  - **Options.** The cover's `tableValueTotal` counts options at underlying
    value, so every quarter carries `reported_value_millions`, the cover's
    figure. The AUM chart plots it. Jane Street jumped from $62B to $397B where
    the old code switched from parsed positions to cover totals.
    `total_value_millions` stays positions only, the base for `pct_portfolio`
    and for cross-fund rankings, where options notional would put Jane Street
    at $1.2T.

  /13f's two tables separate money held from money moved (asked 2026-10-04).
  Consensus shows "Combined holdings", the funds' whole positions, beside "Net
  added this quarter". Net Buys shows "Bought this quarter". Both value columns
  used to sum the funds' whole positions, so a fund adding 1% to a $66B Apple
  stake put $66B under net buys. Money moved is shares changed × quarter-end
  price, from `change_shares` or `change_pct`. A full exit is invisible, since
  the fund no longer holds the stock, and the tooltip says so. Both count each
  fund's top 50 positions, which is what `holdings` carries.

  The infotable is the INFORMATION TABLE row's raw file, never an
  `xslForm13F_X0n/` view. An X01 view slipping past a literal X02 filter was the
  long-running "mismatched tag: line 33, column 2". A slow refresh keeps
  finished funds and carries the rest forward, and `_CACHE_VER` (3) forces a
  refetch when the payload's meaning changes. Tests: `tests/test_sec13f.py`
  (77). Five need pyexpat, which only the box has; run them there.
- **A fund has no company data, and asking Yahoo for it anyway is not free.**
  An ETF's earnings dates, insider trades, statements and EPS trend all come
  back as a 404 ("No fundamentals data found for symbol: SPY"). That was about
  270 requests a day, measured 2026-10-04, from `/api/history`,
  `/api/financials` and the analyst sweep. Each 404 makes yfinance reset the
  cookie and crumb that every thread in the process shares. `data.is_non_equity`
  names the quote types with no company (ETF, MUTUALFUND, INDEX, CURRENCY,
  CRYPTOCURRENCY, MONEYMARKET, FUTURE) and gates all three. The ticker record
  carries `"Quote Type"`, so the sweep knows without asking. An unknown or
  missing type is still asked: skipping a real company because one `info` call
  came back thin is the worse error. The markets warm-up likewise reads an
  index's name, 52-week range and volume from the chart response's
  `history_metadata` rather than `tk.info`. That call failed with 401 "Invalid
  Crumb" in 46 of 291 cycles, and its P/E was null for every index anyway.
  Tests: `tests/check_history_non_equity.py`.
- **A weekly file is downloaded when a newer one can exist, not on every
  restart.** AAII's 1.3 MB sentiment XLS used to be fetched once per process
  after each restart (the master's brief pre-gen, then each worker), because
  nothing read `cache/aaii_cache.json` first. Four back-to-back deploys on
  2026-10-03 made that eight downloads in 33 minutes. Imperva answered the last
  three with its bot challenge, which arrives as HTTP 200 HTML.
  `_aaii_release_due` encodes the release: each row is dated by its Thursday,
  published around 15:00 UTC. So the disk copy is served until the next
  Thursday at 15:30 UTC, a download that brings nothing newer waits 3 h, and
  `_stale` means only "a newer survey was due and could not be had". It used to
  mean "this fetch failed", which put an amber "(cached)" on the current week.
  Tests: `tests/check_aaii_fetch.py`.
- **`routes.py` is monolithic** (5200+ lines in yStocker) — all routes, API endpoints, cache logic, and background tasks in one file.
- **Google Maps API** on yPlanner requires a valid billing-enabled API key; errors show "Oops! Something went wrong" with a purple stripe.
- **SSH deploy** requires a `.pem` key file; the `id_ed25519` key on this machine doesn't have EC2 access. Use SSM `send-command` instead.
- **Never fit ML models in a request process.** Prophet (cmdstanpy) and `pmdarima.auto_arima` each retain hundreds of MB that glibc never returns to the OS, so a worker that served one `/api/forecast` request stayed ~880 MB larger for life. Ten such requests caused nine OOM kills in 48 h, and since the kernel picks its OOM victim globally they took *other* apps down too. `forecast.py` now runs fits in a `subprocess` (`python -m ystocker.forecast <TICKER> <OUT>`) via `run_forecast_isolated()`. Not `multiprocessing`: `fork` would inherit held cache locks from the background threads, and `spawn` re-imports the parent's `__main__` — which under gunicorn is the venv launcher script.
- **Dead FRED series return HTTP 200.** `MBST` and `WASDRAL` still serve well-formed CSV years after they stopped publishing, so stale data flows in silently and corrupts anything derived from it. Prefer the Wednesday-level `WSHO*` ids. The row-count-against-`WALCL` check this file used to prescribe was never implemented and would have needed a hand-maintained expectation per series; `freshness.series_health()` now does it generically instead, inferring each series' cadence from its own observation dates and flagging a trailing gap of more than `cadence * 3 + 7` days. `/api/fed`, `/api/housing` and `/api/multiples` ship the verdict as `meta.series` + `meta.stale_series`. It is deliberately biased toward flagging — a false positive costs one log line, and this failure went unnoticed for years. Tune with `FRESHNESS_CADENCE_TOLERANCE` / `FRESHNESS_CADENCE_GRACE_DAYS`; note a `stale` of `None` means "too few observations to tell", which is not the same as healthy.
- **Never let an outbound call run without a timeout.** `yf.Ticker(t).info` had none, and in a daemon thread that means block forever with nothing in the log — the cache warmer could park indefinitely. Yahoo now gets a `curl_cffi` session carrying one, which **must** be curl_cffi rather than `requests`: yfinance ≥1.x asserts the session type and needs Chrome TLS impersonation, so a `requests.Session` raises `YFDataException` and passing nothing leaves the timeout unset. One module-level session is reused because `YfData` is a singleton that re-binds whatever it is given, so a session per ticker would thrash the cookie/crumb it just negotiated.
- **A deploy takes ystocker down for as long as its slowest in-flight request.** The unit sets `KillMode=process`, so systemd signals only gunicorn's master and then waits while workers finish what they are serving; nginx returns 502 for that whole window, and `--preload` adds a few more seconds re-importing the app before anything binds :8000 again. Measured: 2 seconds for a normal deploy, but **32 seconds** for one that landed while a worker was generating an AI brief, which is a Gemini call capped at 90s. So the outage is bounded by `_BRIEF_GEMINI_TIMEOUT_MS`, not by anything about the deploy. Pre-generating the brief keeps request-path generation rare, but if this matters, drain first or move generation off the request path entirely — do not just retry the deploy and assume the 502 was transient.
- **Not every cache in `routes.py` is keyed `"data"`.** Most are `CACHE["data"] = {"ts", "data"}`, but `_CREDIT_SPREAD_CACHE` is keyed by *period* (`"1y"`, `"2y"`, …) and `_YIELD_CURVE_CACHE` by its schema version (`_YIELD_CURVE_CACHE_VER`). Reading the wrong key returns `None` rather than raising, so the consumer just silently loses a section — `/api/daily-summary` read `_CREDIT_SPREAD_CACHE.get("data")` from the day it was written, which meant its credit-spread line never once appeared in a summary. Check the write site for the key before peeking a cache, and hold its lock.
- **reportlab fails loudly on height and silently on width.** A flowable taller than the frame raises `LayoutError` and kills the whole PDF (a single-cell `Table` cannot split between rows — pass `splitInRow=1`); a flowable *wider* than the frame is simply drawn through the margin, or off the paper. So every fixed-width flowable in `report_pdf.py` is clamped to the measure, and preformatted text is hard-wrapped before it is handed over. Separately, the CJK line breaker deliberately overruns the measure by up to one em rather than start a line with `、` or `。`, which is why the Chinese path lays out to a slightly narrower measure and leaves a gutter for that overhang.
- **An `https://` link opens a vendor's app only for the paths that vendor's association file claims.** Universal links feel automatic, so the natural assumption is that pointing at `futunn.com/en/stock/SMCI-US` will open Futubull on a phone that has it. It will not, and nothing reports the miss — Futu's `/.well-known/apple-app-site-association` claims only `/qq_conn/1101195293/*`, `/weixin_ios/*`, `/app/*` and `/deeplink/*`, so `/en/stock/*` is a plain web page on iOS forever, however the anchor is written. **Read the vendor's `apple-app-site-association` and `assetlinks.json` before assuming, and before hand-rolling a scheme.** The verified route for Futu is the scheme `ftnn://quote/stockDetail/<stockId>/1` (from Futu's own `al:ios:url` tag and its `af_dp=` AppsFlyer parameter; Android package `cn.futu.trader`, iOS App Store id `592031984`; moomoo is `ftmm`). Two traps behind it: `stockId` is Futu's **opaque internal id, not the ticker** — SMCI is `203319`, and HK/A-share ids are 14-digit strings (`00700-HK` is `54047868453564`), so anything that narrows the type breaks exactly the non-US venues `_futu_symbol` exists to support — and the quote page carries **dozens of unrelated `stockId`s** in its "hot stocks" rails, so a positional parse links to the wrong company, which is worse than not linking. `futu.py` round-trips `stockCode` + `marketLabel` back into the requested symbol and refuses a mismatch. Note the Android side is the easy one only because `intent://` carries a declarative `S.browser_fallback_url` (percent-encoded — `intent://` is `;`-delimited, so a raw URL truncates); iOS has no equivalent, so it needs a visibility-timer fallback, and `window.open` after that timer can be popup-blocked because the click gesture has expired.
- **A test that greps rendered HTML for `onerror=` passes on its own escaping.** `&lt;img src=x onerror=alert(1)&gt;` is inert — a string the client displays, not an element it runs — but it contains the needle, so a naive substring assertion reports a vulnerability that is not there, and (worse) an assertion written to accommodate that noise stops catching the real thing. `tests/test_report_email.py` strips `&lt;…&gt;` before checking, so it only ever asserts on *live* markup. Same trap with `href=`: it appears in escaped text too. Note also that `<a>` is deliberately absent from the inline-tag restore list in both `report_email.py` and `static/markdown.js` — honouring it would mean emitting an attribute without vetting it — so a bare inline `<a href>` in model output is shown as text unless the body *opens* with a block-level tag and takes the allowlist path.
- **Yahoo's EPS can sit on a different share basis from its price, and nothing
  in `info` says so.** Tokio Marine (8766.T), 2026-10-04: ¥507.4 against a
  trailing EPS of ¥279.17. That made trailingPE 1.82 and forwardPE 1.03, where
  cap ÷ net income says 26.7. Because /multiples cap-weights `cap / pe`, that
  one name booked ~$88B of forward earnings that do not exist into the Nikkei.
  `data.pe_basis_ok()` checks trailingPE against `marketCap /
  netIncomeToCommon`, which no share count enters, and blanks both P/Es and
  the PEG outside `PE_BASIS_BAND` (0.55–1.8; thirty same-currency listings
  surveyed sat at 0.90–1.23). It skips ADRs, whose net income is in another
  currency, and it fails a positive P/E against negative net income (FUBO and
  PSKY that day). Its first hour in production caught a second split as well:
  Tokyo Electron (8035.T) split 5:1 on 2026-09-29, and Yahoo re-based the
  price and the share count but not either EPS. Its P/E read 9.7, where cap ÷
  net income says 44.
- **A ticker can be handed to a different company, and the quote stays
  plausible.** Yahoo's PARA has been Banzai International, a $2M shell, since
  Paramount moved to PSKY. The "Streaming / Media" default went on tracking it
  (P/E 0.03, cap 0.0), and it topped "Lowest P/E first" on /companies. The
  default now says PSKY. `SYMBOL_RENAMES` in `__init__.py` also rewrites a saved
  `peer_groups.json` at load, because a box holding one (written by the
  /groups UI) replaces the defaults with it, and a code edit alone would never
  reach that box. The current box has no saved file (none survived its
  rebuilds), so there the default was all that mattered.
- **A `check_` script that calls `create_app()` hands its threads this laptop's
  production credentials.** The markets warm-up writes `ystocker-markets-cache`,
  and the observed-series threads write their tables. Strip `AWS_*` (credentials
  and config files pointed at /dev/null), stub SSM, and no-op `Thread.start`
  around `create_app()`, as `check_research_endpoints.py` does.
  `check_inbox_endpoints.py` and `check_assets_endpoints.py` did none of that
  until 2026-10-04.
