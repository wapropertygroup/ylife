---
name: ystocker
description: "Operational reference for the yStocker Flask monorepo (8 apps for li-family.us — yStocker, yPlanner, yPlanter, yHome, yTracker, yPay, yImage, yBG — rooted at /Users/yuanxili/workspace/ystocker). Use this whenever the user wants to run one of these apps locally, deploy or restart the production box, debug a stale cache / missing DynamoDB table / observed series, work on dark-and-light theme or Tailwind classes, touch routes.py, Chart.js, or a background refresh thread, or mentions deploy.sh, SSM, gunicorn, systemctl, li-family.us, or trade-agents.com. Always check this skill before running a production restart or deploy, and before writing DOM code that adds or removes a Tailwind `dark:` class pair — both have documented silent-failure traps below. For the TradingAgents multi-agent engine behind /agents, use the sibling `tradingagents` skill instead."
---

# yStocker — operations & gotchas

Flask monorepo hosting 8 apps for the Li family at **li-family.us**. Full
architectural detail lives in `AGENTS.md` at the repo root (already loaded as
project instructions whenever a session is rooted here) — this skill is the
condensed, task-oriented slice: the commands you actually run, and the traps
that have already bitten this codebase once.

## The 8 apps

| App | Dir | Dev port | Prod port | URL | Storage |
|-----|-----|----------|-----------|-----|---------|
| yStocker | `ystocker/` | 5000 | 8000 | stock.li-family.us | JSON cache files |
| yPlanner | `yplanner/` | 5001 | 8001 | planner.li-family.us | DynamoDB |
| yPlanter | `yplanter/` | 5002 | 8002 | planter.li-family.us | DynamoDB |
| yHome | `yhome/` | 5003 | 8003 | home.li-family.us | None |
| yTracker | `ytracker/` | 5004 | 8004 | tracker.li-family.us | DynamoDB |
| yPay | `ypay/` | 5005 | 8005 | pay.li-family.us | None (Stripe) |
| yImage | `yimage/` | 5006 | 8006 | image.li-family.us | None |
| yBG | `ybg/` | 5007 | 8007 | ybackground.li-family.us | None (Checkr) |

`tv.li-family.us` and `trade-agents.com` (+ `www.` and `pay.` subdomains) are
extra nginx vhosts with no app of their own behind most of them — `tv.`
redirects to `/tv`, `trade-agents.com` proxies to `/agents` on :8000 (and
`pay.trade-agents.com` fronts ypay on :8005 for brand continuity at checkout).

yStocker is the odd one out: it stores in flat JSON cache files, not
DynamoDB, and it is the only app with background daemon threads and a
TradingAgents child process — which is why it gets the largest memory budget
and its own restart caveats below.

## Run locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements_stocker.txt   # or requirements_{planner,planter,tracker,home}.txt
python run/run_stocker.py                 # http://127.0.0.1:5000
```

## Deploy

```bash
bash deploy/deploy.sh              # code-only: both repos, restart all 8, health check, ~1 min
bash deploy/deploy.sh --ystocker   # same, but skip the TradingAgents checkout
bash deploy/deploy.sh --check      # report what is deployed, change nothing
bash deploy/deploy.sh --full       # also converges the box over SSH; needs -i key.pem
```

The default path runs over **SSM**, so it needs no SSH key — this machine's
`id_ed25519` does not have EC2 access; only `--full` needs a `.pem`.

Two safety checks worth knowing about, because they're easy to misread as the
deploy doing nothing:

- **Before** touching anything, it warns about uncommitted or unpushed work.
  It asks the *remote* for `main`'s SHA via `ls-remote` (not a local
  `origin/main` ref), because `reset --hard` takes whatever GitHub has —
  a commit still sitting on the laptop would silently not ship.
- **After** `fetch` + `reset --hard`, it re-reads `main` from the remote via
  `ls-remote` again and aborts *before restarting* if the checkout doesn't
  match. It prints `already at latest` or `updated to <sha>` plus the new
  commits — so "nothing happened" and "nothing needed to happen" both have a
  distinct message, and a failed fetch can't quietly report success.

## The #1 rule: restart, never HUP

`kill -HUP` looks like a graceful reload and is not one. Under gunicorn
`--preload`, the HUP handler re-reads only the **config file** — the WSGI app
was imported once by the master at `ExecStart`, so HUP re-forks workers from
that same stale module state. New Python code never runs. Jinja templates
*do* refresh (a forked worker starts with an empty template cache), which is
exactly what makes this easy to miss: the page looks like it updated.

**Always `systemctl restart <service>`.** This is safe to do even mid-analysis
— the unit sets `KillMode=process`, so systemd only signals gunicorn's master
and a detached TradingAgents child survives. The one cost: the restart takes
the app down for as long as its slowest in-flight request (nginx 502s for
that window). Measured 2 seconds normally, but 32 seconds when a worker was
mid-Gemini-call generating a Markets Brief — bounded by that call's own
timeout, not by anything about the deploy. If this matters, drain first
rather than assuming a 502 was transient.

## Single-app deploy without a `.pem` (SSM)

```bash
# Resolve the box by tag. Never pin the id: it has changed twice, and a stale
# one fails with "InvalidInstanceId", which reads like a permissions fault.
IID=$(aws ec2 describe-instances --region us-west-2 \
  --filters "Name=tag:Name,Values=ystocker-instance" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)

aws ssm send-command --instance-ids "$IID" --region us-west-2 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["cd /opt/ystocker && sudo git fetch origin && sudo git reset --hard origin/main && sudo systemctl restart yplanner"]}'

aws ssm get-command-invocation --command-id <CMD_ID> --instance-id "$IID" \
  --region us-west-2 --query "[Status, StandardOutputContent]" --output text
```

## Caching model

Two tiers, and the distinction matters:

- **Recomputable → cache.** In-memory dict + on-disk JSON in `cache/`,
  `threading.Lock`-guarded, atomic temp-file + `os.replace()` writes. Ticker
  metrics (8h), Fed balance sheet (24h), 13F holdings (24h), peer groups
  (persistent).
- **Not recomputable → DynamoDB "observed series."** `ystocker-valuation-history`,
  `ystocker-fear-greed`, `ystocker-pcr-history`, `ystocker-cta-history`,
  `ystocker-fedwatch-history` (plus `ystocker-assets`, `ystocker-agent-shares`
  for user data). These accumulate one row per day and cannot be rebuilt from
  anything else — the CTA tracker is the clearest case: Goldman's trigger
  levels change weekly with no published history, so a lost row is gone
  forever even though the underlying index history is freely available.

None of these tables are in `deploy/cloudformation.yaml`, **on purpose** —
CloudFormation can't adopt a live table without an import operation, so
adding them would break the next `--full` deploy rather than converge it. IAM
already grants `table/ystocker-*`, so a missing table is never an access
error, just a silent fall-back to disk-only (cache) or an error (observed
series, which fail closed).

**Trap:** not every cache dict is keyed `"data"`. Most are
`CACHE["data"] = {"ts", "data"}`, but `_CREDIT_SPREAD_CACHE` is keyed by
*period* and `_YIELD_CURVE_CACHE` by a schema-version constant. Reading the
wrong key returns `None` silently rather than raising — check the write site
before peeking a cache.

## Known pitfalls — quick index

Each of these has bitten this codebase for real. One-liners here; the full
explanation and fix for each is in `references/known-pitfalls.md` — read that
before touching any of these areas:

1. A Tailwind class pair (`"bg-x dark:y"`) is not a DOM token — `classList`/`closest` on it throws or silently matches nothing.
2. `text-<hue>-400` is invisible in light mode; light counterparts must be shade-700.
3. `kill -HUP` does not reload code under `--preload` (see above).
4. A `DeferLoad` anchor that's `display:none` defers nothing — it fetches immediately instead.
5. A hand-rolled pull-to-refresh stacks with the browser's native one unless the native gesture is suppressed.
6. There's no Chart.js date adapter loaded — `type: 'time'` renders an empty canvas; use `type: 'linear'` over epoch ms.
7. Nested `<button>` elements break DOM structure — browsers auto-close the outer one.
8. `.fade-up`'s `animation: … both` makes every card a permanent stacking context, breaking a dropdown's `z-index`.
9. `routes.py` is monolithic (5200+ lines) — expect to grep, not skim.
10. Never fit Prophet/ARIMA in the request process — `forecast.py` shells out via `subprocess`, deliberately not `multiprocessing`.
11. Dead FRED series (`MBST`, `WASDRAL`) return HTTP 200 with stale data — `freshness.series_health()` catches this generically.
12. Every outbound call needs an explicit timeout; yfinance specifically needs a `curl_cffi` session, not `requests`.
13. reportlab (`report_pdf.py`) fails loudly on a flowable too *tall*, silently on one too *wide*.
14. An `https://` link only opens a vendor's app for paths that vendor's `apple-app-site-association` actually claims — verify before hand-rolling a deep link.
15. A test that greps rendered HTML for `onerror=`/`href=` can pass on its own escaping — strip `&lt;…&gt;` first or it's asserting on inert text.

## Cross-repo planning docs

Feature work spanning both this repo and TradingAgents gets written up as a
pair of design docs: `claude/research-<slug>.md` + `claude/todo-<slug>.md`.
Note the directory really is spelled **`claude/`** on disk whichever agent is
reading — a plain directory at the repo root, distinct from the dot-directory
that holds this skill. The pair can end up in either repo's `claude/` dir
depending on which was the session
root when it was written, so if you're looking for the design rationale
behind a TradingAgents-side feature, check both repos, not just this one.

## Production facts

- Instance: resolve by the tag `Name=ystocker-instance` in `us-west-2`; the id
  has changed twice and must not be pinned. Amazon Linux 2023, `t3.medium`.
- nginx → 8 gunicorn systemd services on ports 8000–8007, 2 workers each,
  `--preload`, recycled every ~200 requests.
- 4 GB RAM + 2 GB swap total. yStocker gets `MemoryMax=1800M`; the other
  seven get `MemoryMax=400M` each.
- Let's Encrypt via certbot.
- No SSH key on this dev machine has EC2 access — use SSM `send-command`
  unless the user supplies a `.pem`.

## See also

Four sibling skills split this repo by the kind of work rather than by app.
Prefer the specific one — each carries traps this file only indexes:

- `ystocker-frontend` — templates and `static/`: the light/dark theme, `CT.c()`
  chart colours, the Tailwind-pair-is-not-a-DOM-token trap, EN/ZH i18n, and the
  four hand-rolled JS modules. Read before touching any template.
- `ystocker-testing` — how to run the suite (the venv is mandatory), the
  known-failing baseline of 4 environmental tests, and what each static guard
  protects.
- `ystocker-data` — cache vs DynamoDB observed series, `peek()` /
  `is_cache_fresh()`, `_CACHE_VER` economics, `fetchguard`, `freshness`, the
  full table inventory and the never-fetch-on-the-request-path rule.
- `ystocker-sibling-apps` — the seven non-yStocker apps, which share this
  skeleton and nothing else.
- `tradingagents` skill — the multi-agent engine behind `/agents`, a separate
  git repo not vendored into this one.
- `CLAUDE.md` at the repo root — the full architectural detail this skill was
  condensed from (asset-tracker look-through, the AI Markets Brief, report
  emailing/sharing, model selection, theming conversion, etc.). Read that one
  rather than `AGENTS.md` beside it whichever agent you are: `AGENTS.md` is a
  **stale** copy that still pins a long-dead EC2 instance id, still recommends
  `kill -HUP`, and still describes `routes.py` as 5,200 lines (it is 13,345).
