---
name: ystocker-data
description: "The yStocker data layer (/Users/yuanxili/workspace/ystocker): when state belongs in a cache file versus a DynamoDB observed series, the two-tier cache mechanics and `_CACHE_VER` economics, the `peek()` / `is_cache_fresh()` / `get_*()` triad, the `fetchguard` circuit breakers and `freshness` staleness classifier, and the full DynamoDB table inventory with the hand-creation recipe. Use this before adding a cache, adding a DynamoDB table, bumping a `_CACHE_VER`, making an outbound HTTP call to Yahoo / FRED / SEC EDGAR / OpenFIGI, adding a background refresh thread, or writing an API endpoint that might fetch on the request path. Read it in particular before any change that could turn one page load into N vendor reads — this box has been hard-blocked by Yahoo before — and before assuming a missing table raises rather than silently degrading."
---

# yStocker — caches, observed series, and outbound calls

Three rules govern everything in this layer:

1. **Recomputable → cache. Not recomputable → DynamoDB.**
2. **The request path never fetches.**
3. **Every outbound call has a timeout and a circuit breaker.**

Each has been violated once and each violation cost something specific.

## Rule 1 — cache vs observed series

**Cache** is anything you could rebuild by asking upstream again. Two tiers:
in-memory dict + on-disk JSON under `cache/`, all access guarded by a
`threading.Lock`, disk writes atomic (temp file + `os.replace()`).

| cache | TTL | file |
|---|---|---|
| stock metrics | 8 h | `cache/ticker_cache.json` |
| Fed balance sheet | 24 h | `cache/fed_cache.json` |
| 13F holdings | 24 h | `cache/sec13f_cache.json` |
| peer groups | persistent | `cache/peer_groups.json` |

**An observed series is not a cache**, and keeping one in a cache file loses it
whenever the EC2 box is replaced — which is exactly how the SPY/QQQ forward-P/E
chart once reset to a single point. The box has been rebuilt **twice**.

The test is not "is it valuable", it is **"can anyone sell it back to me
tomorrow?"** Nothing sells back yesterday's consensus forward multiple,
yesterday's ZQ curve, or yesterday's CNN Fear & Greed reading.

The clearest case is the CTA tracker: each row records how far the S&P sat from
Goldman's trigger levels *that day*, and the triggers change weekly with no
published history — so a lost row is gone permanently even though the index
history is freely available.

**`/fedwatch` is the instructive contrast**, because it draws two history charts
and only one is observed:

- *Target range history* — what the Fed actually did. Fully recomputable from
  FRED on every call, so it is **cache** and deliberately not stored. It costs
  no extra fetch: `_latest_fred_value()` was already downloading the whole
  DFEDTARL/DFEDTARU CSV and discarding all but the last row.
- *Expectations over time* — what the market **thought** it would do. Nothing
  upstream sells back yesterday's curve. Observed.

## The DynamoDB inventory (verified against the code, 2026-09-20)

Region `us-west-2`, all `PAY_PER_REQUEST`.

**yStocker — observed series / user data**

`ystocker-valuation-history` · `ystocker-fear-greed` · `ystocker-pcr-history` ·
`ystocker-cta-history` · `ystocker-fedwatch-history` ·
`ystocker-heatmap-snapshots` · `ystocker-aaii-sentiment` ·
`ystocker-economic-events` · `ystocker-dca-history` · `ystocker-dca-universe` ·
`ystocker-dca-dcf` · `ystocker-assets` · `ystocker-agent-jobs` ·
`ystocker-agent-shares` · `ystocker-agent-credits` ·
`ystocker-agent-decisions` · `ystocker-subscribers` ·
`ystocker-daily-summaries` · `ystocker-news-translations` ·
`ystocker-markets-cache`

**Sibling apps** — `yplanner-users`, `yplanner-trips`, `yplanner-shared-trips`,
`yplanter-history`, `yplanter-translations`, `ytracker-items`,
`ytracker-prices`, `ypay-items`, `ypay-payments`, `ybg-applications`.

### Only ONE table is in CloudFormation

`deploy/cloudformation.yaml` declares exactly one `AWS::DynamoDB::Table`:
**`ystocker-agent-jobs`**. Every other table above is created by hand, and that
is deliberate — CloudFormation cannot adopt a live table without an import
operation, so adding an existing one breaks the next `--full` deploy rather
than converging it.

### IAM, and the gap in it

The instance role grants four wildcards:

```
table/ystocker-*   table/yplanner-*   table/yplanter-*   table/ytracker-*
```

So a new `ystocker-*` table needs **no IAM change** — and a *missing* table is
never an access error, just a silent fall back to disk-only.

⚠️ **`ypay-*` and `ybg-*` are not in that list**, but `ypay/routes.py` and
`ybg/routes.py` both open tables (`ypay-items`, `ypay-payments`,
`ybg-applications`). Those calls hit the same lazy-init-and-log-a-warning
pattern as everywhere else, so an `AccessDeniedException` degrades silently
rather than failing. If either app's persistence appears to do nothing in
production, check the IAM wildcards first.

### Creating one

```bash
aws dynamodb create-table --table-name ystocker-<name> --region us-west-2 \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=date,AttributeType=S \
  --key-schema AttributeName=date,KeyType=HASH
```

Pick the key schema by **how it will be read**, because `PAY_PER_REQUEST` bills
a Scan by volume scanned:

- `date` HASH — the default for a daily series small enough to Scan.
- `ticker` HASH + `date` RANGE — `ystocker-dca-history`, which gains a row per
  ticker per day; a per-ticker page would otherwise pay for every other
  ticker's rows on every load.
- `ticker` HASH alone — `ystocker-dca-universe`, one short bounded row per
  tracked ticker, so listing it is a Scan on purpose.

If the table needs a TTL, `create-table` returns as soon as it is `CREATING`
and `update-time-to-live` refuses a table that is not yet `ACTIVE` — so
`aws dynamodb wait table-exists` between them is required, not tidiness. And
TTL is a *convenience*, never the guarantee: DynamoDB's sweeper can run up to
48 h late, so re-check the expiry on read (`share.lookup()` does).

Most tables here have **no TTL on purpose** — a portfolio does not expire, and
a hand-built valuation is exactly the thing that must not evaporate.

## Fail closed or degrade? Decide deliberately

Both patterns are in the codebase and the choice is about **which silence
misleads**:

- `portfolio.load` **fails closed** — raises `StoreUnavailable`, route answers
  503. Returning `[]` renders as "you have no positions" on the page whose job
  is to show them, and a user who concludes their data is gone cannot tell the
  honest recovery (retry) from the dishonest one (re-import, now duplicated).
- `dcf_store` **degrades** — a missing override just means the derived DCF
  runs, which is a complete answer, and the payload's `source` says which
  produced it.
- `share._get_table()` fails **closed**, the opposite of `quota`, because the
  row is what makes a shared link resolve and a mail that got ahead of it would
  deliver a button that 404s.

There is deliberately **no silent disk fallback for user data**. Cache modules
degrade to disk-only when a table is missing — right for a cache, wrong for a
portfolio, since the box is replaceable. Local dev gets a file store behind an
explicit `ASSETS_LOCAL_STORE=1`.

## The read triad: `get_*()` / `peek()` / `is_cache_fresh()`

Eleven modules expose `peek()` (`analyst`, `breadth`, `dca_history`,
`etf_holdings`, `fed`, `fedwatch`, `funddata`, `housing`, `sectors`,
`valuation`, plus `funddata.peek_resolver()`).

| call | semantics |
|---|---|
| `get_*()` | may fetch. **Never call from a request handler.** |
| `peek()` | returns whatever is cached, **ignoring TTL but still honouring `_CACHE_VER`**. Never fetches. |
| `is_cache_fresh()` | TTL-only boolean. |

**Gating a reader on `is_cache_fresh()` is usually the bug.** The AI Markets
Brief did, and dropped whole sections whenever a nightly refresh ran late — on
monthly series where a day changes nothing. Stale beats absent, but it must be
**labelled**: `brief.py` carries the stale names into the snapshot so the model
dates them. A cold source is *stated*, never dropped, because omitting a
section reads to the model as "nothing to say about housing" and invites it to
answer from training data.

### Trap: not every cache dict is keyed `"data"`

Most are `CACHE["data"] = {"ts": …, "data": …}`. But `_CREDIT_SPREAD_CACHE` is
keyed by *period* (`"1y"`, `"2y"`, …) and `_YIELD_CURVE_CACHE` by its schema
version constant. Reading the wrong key returns `None` rather than raising, so
the consumer silently loses a section — `/api/daily-summary` read
`_CREDIT_SPREAD_CACHE.get("data")` from the day it was written, so its
credit-spread line never once appeared. **Check the write site for the key
before peeking, and hold its lock.**

## `_CACHE_VER` — a bump is a refetch storm, price it first

Stamp the payload with `_ver` and refuse a mismatch on read. Current values
live in the module (`fedwatch` v3, `housing` v4, `funddata` v2, `analyst` v2,
`etf_holdings` v2, `dca_history` v1, `routes._YIELD_CURVE_CACHE_VER` v4).

**Bump when the payload's shape changes, or today's stored copy is served all
day and the change looks like it did nothing.** `brief.py`'s `_BRIEF_KEY_VER`
is the same idea in a DynamoDB key.

But a bump invalidates **every** entry at once. On `dca_history` at the
registry cap that is ~360 Yahoo reads in one sweep — the exact burst the
module's budget exists to prevent. So two changes deliberately shipped
**without** a bump:

- adding `beta` to `_FORWARD_KEYS` — an older payload has no beta, `wacc()`
  assumes 1.0 and *says so in `notes`*, and the next daily rebuild fixes it.
- the ADR currency/share-basis reconciliation — affected payloads self-heal
  within the 24 h TTL; force-refresh the handful of ADRs instead.

Degrading **visibly** for a day beats a refetch storm on deploy. That is only
available to you if the degraded state announces itself.

## Rule 2 — the request path never fetches

A cold DCA ticker is six Yahoo reads (`info`, `income_stmt`, `balance_sheet`,
`cashflow`, `quarterly_income_stmt`, `history`) — tens of seconds. A twenty-line
`/assets` portfolio of funds is ~100 reads, a minute at `fetch_group`'s 0.5 s
spacing. gunicorn's `--timeout 120` then takes the worker's **other** requests
down with it; `api_markets()` on a cold cache measured 120 s and got the worker
SIGKILLed.

The shape, used identically by `/api/dca`, `/api/assets` and `/markets`:

1. Answer **`202 warming`** (or return `pending` rows) from whatever is cached.
2. Kick **one** background rebuild per symbol, deduped, drawing on a **global**
   budget (`MAX_INFLIGHT_BUILDS`, `BUILD_MIN_GAP_SECONDS`, `WARM_SPACING_SECONDS`).
3. Client polls with a **bounded** loop that **must render its terminal state**
   — stopping the timer alone leaves the spinner on screen forever.

Two details that are easy to get wrong:

- **A per-symbol guard bounds nothing when the symbols differ**, which is the
  case that actually happens: one reader following DCA links hit twenty
  distinct tickers in eight minutes, ~120 Yahoo reads. The global budget is the
  real limiter; a refused slot reports `queued` so the page can say "waiting
  for a slot" rather than implying progress.
- **`release_build()` must floor at zero.** A doubled release silently raises
  the ceiling for ever — the one limiter failure nobody notices until Yahoo
  blocks.

Distinguish the states honestly. `pending` (not yet fetched) ≠ `warming` (a
worker is actively on it) ≠ `unresolved` (asked, and the vendor has nothing).
Only an active worker may set `warming=true`; a pass that makes no progress
clears its queue so the client can show a paused explanation and a retry
control instead of a spinner.

Bulk sweeps are the same hazard at scale. The daily DCA snapshot costs **zero**
Yahoo calls — it reads the `ticker_cache.json` the rolling refresher already
maintains — and deliberately pre-builds no reconstructions, because that is six
reads per symbol across the universe, which is the sweep `valuation.py` records
having got this box **hard-blocked**.

## Rule 3 — `fetchguard`, and never a call without a timeout

`ystocker/fetchguard.py` is the shared outbound-HTTP resilience layer. Every
vendor call (Yahoo, FRED, SEC EDGAR, OpenFIGI) goes through it, and **each
vendor has its own breaker** so one vendor's 429 cannot stall the others.

```python
from ystocker import fetchguard

fetchguard.guard("fred")                       # raises CooldownActive if open
resp = fetchguard.request("fred", url, timeout=20, raise_for_status=False)
fetchguard.trip("sec", 900, "HTTP 429")        # stop for everyone, not just me
fetchguard.snapshot()                          # breaker state, for /health
```

- `request()` retries transport errors and `retry_statuses` with exponential
  back-off, then **trips the breaker and re-raises**. A 429 out of retries is
  the vendor telling us to stop — so it stops for every call site.
- `raise_for_status=False` is for a caller that treats 404 as "absent".
- `retry_statuses` is narrowable for the same reason: SEC's 503 means "no
  filing here" often enough that retrying it, let alone cooling down, is wrong.
- `FailureBackoff(name, base_seconds=…, max_seconds=…)` is the **persisted**
  per-item version (keyed by ticker or series id, doubling per consecutive
  failure, cleared by one success). Persistence is the point: the in-process
  version forgot everything on restart, so every deploy re-queued the full set
  of permanently-dead symbols for an immediate retry.

**Never let an outbound call run without a timeout.** `yf.Ticker(t).info` had
none, and in a daemon thread that means block forever with nothing in the log.
Yahoo gets a **`curl_cffi`** session carrying one — which must be `curl_cffi`,
not `requests`: yfinance ≥1.x asserts the session type and needs Chrome TLS
impersonation, so a `requests.Session` raises `YFDataException` and passing
nothing leaves the timeout unset. One module-level session is reused because
`YfData` is a singleton that re-binds whatever it is given, so a session per
ticker would thrash the cookie/crumb it just negotiated.

## `freshness.py` — three different questions

Do not conflate them; the module exists because they were conflated:

| function | answers |
|---|---|
| `describe_age(ts, ttl_seconds=)` | how old is this **cache entry** |
| `classify_quote(ts)` | `realtime` / `session_close` / `stale` — relative to **market hours** |
| `series_health(dates, label=)` | has this **upstream series stopped publishing** |

`series_health` infers a series' cadence from its own observation dates and
flags a trailing gap longer than `cadence * 3 + 7` days. It exists because
**dead FRED series return HTTP 200**: `MBST` and `WASDRAL` still serve
well-formed CSV years after they stopped publishing, so stale data flowed in
silently for years. Prefer the Wednesday-level `WSHO*` ids.

It is deliberately biased toward flagging — a false positive costs one log
line. `/api/fed`, `/api/housing` and `/api/multiples` ship the verdict as
`meta.series` + `meta.stale_series`. Tune with `FRESHNESS_CADENCE_TOLERANCE` /
`FRESHNESS_CADENCE_GRACE_DAYS`. **A `stale` of `None` means "too few
observations to tell"**, which is not the same as healthy.

## Background threads under `--preload`

`create_app()` runs once **in the gunicorn master**, so refresh threads live
only there. Forked workers inherit a cache *snapshot* and never look at disk
again on their own.

That is why a background probe is worthless without a **disk re-read** in the
reader. `get_fedwatch_data()` compares `_disk_mtime()` against the mtime its
copy came from (one `stat()`, no parse) and reloads only when the file actually
changed — otherwise the master detects an FOMC move within minutes and every
worker goes on serving the old range for the whole 4-hour TTL. Order by the
payload's own `_ts`, so a restored backup or a clock step cannot roll a good
cache backwards.

Two related lessons from `fedwatch`:

- **A cheap sub-probe can have its own cadence.** The ZQ curve wants a 4-hour
  TTL; the target range is a fact that changes eight times a year and is two
  small CSVs, so `probe_target_range()` runs every 30 min (every 5 near a
  decision). A changed range triggers a **full rebuild** and is never patched
  into the cached payload — `lower`/`upper` are the baseline every projected
  meeting is computed from *and* are banked into a series that cannot be
  recomputed.
- **Compare rounded.** `3.75 != 3.7500000001` off a CSV would rebuild the whole
  curve every 30 minutes for ever. And a failed probe returns `None` — "don't
  know" — which neither triggers nor suppresses a rebuild.

Same-date writes should **overwrite, not accumulate**, and should be keyed by
the data's own `as_of` rather than `date.today()`: a Saturday ZQ close *is*
Friday's, so keying by today writes phantom weekend rows holding Friday's
numbers again.

## See also

- `ystocker` — deploy, restart, the box.
- `ystocker-testing` — the pure modules exist so this arithmetic is provable.
- `CLAUDE.md` — per-feature rationale (DCA engine, look-through, brief).
