# Plan: TradingAgents-Astock Seven-Analyst Integration

## 0. Approved Requirements

* A-share runs use seven analysts: Market, Sentiment, News, Fundamentals, Policy, Hot Money, and Lock-up.
* Minimum role/tool contract:
  * Policy Analyst: `get_news`, `get_global_news`.
  * Hot Money Tracker: `get_stock_data`, `get_news`, `get_insider_transactions`.
  * Lock-up Monitor: `get_insider_transactions`, `get_news`, `get_fundamentals`.
* Dedicated A-share signal tools may be added to those minimum sets to supply Dragon-Tiger, fund-flow, lock-up, reduction, pledge, forecast, and peer evidence.
* All seven reports must feed both Bull/Bear research debate and all three risk debaters. A role that only appears in the UI or final report does not satisfy the requirement.
* Preserve the existing fork's Gemini support/fallback, progress callback, checkpoint/resume, verified snapshots, multi-market vendor fallback, and yStocker's detached-process/job/quota design.
* Non-A-share runs retain the existing four-analyst pipeline. No third-party Streamlit UI is imported.

## 1. Change Manifest

### `/Users/yuanxili/workspace/TradingAgents` — execution engine

| # | File | Action | Summary |
|---|---|---|---|
| 1 | `pyproject.toml` | Modify | Add mootdx in a way that does not downgrade Gemini's httpx; document the deliberate compatibility override and keep a reproducible install path. |
| 2 | `NOTICE` | Add | Attribute the Apache-2.0 TradingAgents-Astock-derived analyst/data work. |
| 3 | `tradingagents/default_config.py` | Modify | Register the A-share signal-data vendor category and optional mootdx/fallback controls without changing non-A-share chains. |
| 4 | `tradingagents/dataflows/a_stock.py` | Modify | Port capability-level source logic: mootdx/Tencent-first quote path, Eastmoney throttling, Sina/Tonghuashun fallbacks, and the A-share signal endpoints needed by the three roles; preserve fail-fast rejection for non-A-share symbols. |
| 5 | `tradingagents/dataflows/interface.py` | Modify | Register signal-data methods and route them through the existing exact vendor-chain mechanism. |
| 6 | `tradingagents/agents/utils/signal_data_tools.py` | Add | Provide typed LangChain tool wrappers for profit forecast, hot stocks, northbound flow, concepts, fund flow, Dragon-Tiger, lock-up, and industry comparison. |
| 7 | `tradingagents/agents/utils/agent_utils.py` | Modify | Re-export the new wrappers for analysts and graph ToolNodes. |
| 8 | `tradingagents/agents/utils/agent_states.py` | Modify | Add `policy_report`, `hot_money_report`, and `lockup_report`. |
| 9 | `tradingagents/agents/analysts/policy_analyst.py` | Add | Implement the policy analyst with the approved baseline tools and A-share policy prompt. |
| 10 | `tradingagents/agents/analysts/hot_money_tracker.py` | Add | Implement the hot-money analyst with approved baseline tools plus dedicated capital-flow/Dragon-Tiger evidence. |
| 11 | `tradingagents/agents/analysts/lockup_watcher.py` | Add | Implement the lock-up analyst with approved baseline tools plus dedicated unlock/reduction evidence. |
| 12 | `tradingagents/agents/__init__.py` | Modify | Export the three analyst factories. |
| 13 | `tradingagents/graph/analyst_execution.py` | Modify | Add canonical node/report specs for `policy`, `hot_money`, and `lockup`; preserve ordered execution and checkpoint signature stability. |
| 14 | `tradingagents/graph/conditional_logic.py` | Modify | Add tool-loop routers for the three new node/clear-node pairs. |
| 15 | `tradingagents/graph/propagation.py` | Modify | Initialize all three report fields to empty strings. |
| 16 | `tradingagents/graph/setup.py` | Modify | Register factories, ToolNodes, clear nodes, and sequential graph edges for the three roles. |
| 17 | `tradingagents/graph/trading_graph.py` | Modify | Register complete tool sets; retain the four-role API default for compatibility while supporting an explicit seven-role selection from yStocker. |
| 18 | `tradingagents/agents/researchers/bull_researcher.py` | Modify | Inject all three reports as named resources. |
| 19 | `tradingagents/agents/researchers/bear_researcher.py` | Modify | Inject all three reports as named resources. |
| 20 | `tradingagents/agents/risk_mgmt/aggressive_debator.py` | Modify | Inject all seven analyst reports into aggressive risk reasoning. |
| 21 | `tradingagents/agents/risk_mgmt/conservative_debator.py` | Modify | Inject all seven analyst reports into conservative risk reasoning. |
| 22 | `tradingagents/agents/risk_mgmt/neutral_debator.py` | Modify | Inject all seven analyst reports into neutral risk reasoning. |
| 23 | `tradingagents/agents/trader/trader.py` | Modify | Apply A-share T+1, board/ST price-limit, lot-size, trading-session, and suspension constraints when the symbol is an A share. |
| 24 | `tradingagents/agents/managers/research_manager.py` | Modify | Tell the manager to reconcile policy, speculative-flow, and supply-shock evidence present in the completed Bull/Bear debate. |
| 25 | `tradingagents/agents/managers/portfolio_manager.py` | Modify | Apply A-share execution constraints to the final decision while retaining current behavior for other markets. |
| 26 | `tradingagents/reporting.py` | Modify | Write and consolidate the three new reports with canonical headings: `Policy Analyst`, `Hot Money Tracker`, and `Lock-up Monitor`. |
| 27 | `tests/test_analyst_execution.py` | Modify | Verify seven-role order/specs, unknown/empty selection behavior, and tracker coverage. |
| 28 | `tests/test_reporting.py` | Modify | Verify all seven reports are saved and consolidated under exact headings. |
| 29 | `tests/test_astock_signal_data.py` | Add | Mock every new source endpoint, point-in-time guard, Eastmoney limiter/fallback, and error path. |
| 30 | `tests/test_astock_analyst_pipeline.py` | Add | Verify factories, state initialization, graph wiring, baseline tool contracts, and complete seven-role ToolNodes. |
| 31 | `tests/test_seven_report_downstream.py` | Add | Use sentinel text to prove Bull, Bear, and all three risk debaters each receive all seven reports. |
| 32 | `tests/test_astock_trading_constraints.py` | Add | Verify A-share constraints are present for Shanghai/Shenzhen/Beijing/ST cases and absent for US symbols. |
| 33 | `tests/test_mootdx_compatibility.py` | Add | Unit-test graceful missing/unavailable mootdx fallback; include an opt-in live smoke marker for import + one TDX quote under modern httpx. |

### `/Users/yuanxili/workspace/ystocker` — product integration

| # | File | Action | Summary |
|---|---|---|---|
| 34 | `ystocker/agents.py` | Modify | Select seven analysts for normalized A-share tickers, retain four elsewhere, stream the three new state fields, enrich self-test output, and expose runtime capability diagnostics. |
| 35 | `ystocker/agent_roles.py` | Modify | Add the three canonical roles, Chinese names/icons/colors/groups, and safe aliases for historical `Watcher`/`Monitor` heading variants. |
| 36 | `ystocker/templates/agents.html` | Modify | Explain the seven-role A-share path and display source/runtime capability status without changing generic four-role behavior. |
| 37 | `ystocker/templates/guide.html` | Modify | Document that A-share runs add policy, hot-money, and lock-up analysis and pass all seven reports into both debate stages. |
| 38 | `ystocker/static/i18n.js` | Modify | Add matching English/Simplified-Chinese UI and guide copy. |
| 39 | `deploy/install-tradingagents.sh` | Modify | Install/converge the fork with the approved mootdx/httpx strategy and fail preflight if the graph cannot construct the seven-role A-share plan. |
| 40 | `deploy/cloudformation.yaml` | Modify | Keep bootstrap installation behavior aligned with the install script and provide the same runtime settings. |
| 41 | `README.md` | Modify | Document the paired-repository seven-role runtime, data sources, and fallback behavior. |
| 42 | `tests/test_agent_roles.py` | Add | Verify exact heading parsing, aliases, ordering, and preservation of model-authored subheadings. |
| 43 | `tests/test_agents_astock_selection.py` | Add | Verify ticker-based 7-vs-4 selection, progress mappings, and diagnostics without running an LLM. |

No files are deleted.

## 2. Sequencing

- [ ] **Step 1: Create isolated feature branches and capture baselines** — Create `codex/tradingagents-astock-seven-analysts` in both repositories; run the existing TradingAgents unit suite and yStocker import/self-test checks before edits. *Depends on: nothing.*
- [ ] **Step 2: Prove or reject mootdx compatibility** — In the TradingAgents venv, install mootdx without allowing its metadata to downgrade modern httpx; verify imports for mootdx, Gemini client, and TradingAgents graph, then run one opt-in TDX canary quote. Record exact versions/results. If runtime compatibility fails, keep mootdx optional/unavailable and use Tencent → Eastmoney → Sina rather than weakening Gemini. *Depends on: Step 1.*
- [ ] **Step 3: Add the A-share signal data layer** — Port only the source adapters needed by the requested roles, including Eastmoney serialized throttling/jitter, point-in-time warnings, timeout/error normalization, and deterministic mocked tests. *Depends on: Step 2.*
- [ ] **Step 4: Add the three analysts and graph wiring** — Add state fields, factories, execution specs, conditional routers, initial state, ToolNodes, and sequential edges. Preserve the existing four-role constructor default; require yStocker to request the seven-role list for A shares. *Depends on: Step 3.*
- [ ] **Step 5: Complete the seven-report decision chain** — Inject all three new reports into Bull, Bear, aggressive, conservative, and neutral prompts; add A-share-aware trader/manager/portfolio constraints. Prove inclusion with unique sentinel values, not string-count heuristics. *Depends on: Step 4.*
- [ ] **Step 6: Extend report generation and engine tests** — Add canonical report files/headings, attribution, graph/report tests, and run the full TradingAgents suite. *Depends on: Steps 4–5.*
- [ ] **Step 7: Integrate yStocker runtime and rendering** — Add ticker-based analyst selection, live progress fields, roles/aliases, diagnostics, self-test coverage, bilingual copy, and parser/selection tests. *Depends on: Step 6.*
- [ ] **Step 8: Harden deployment** — Make installation reproduce the tested dependency layout; add a seven-role graph preflight before service restart and align CloudFormation/bootstrap. *Depends on: Steps 2 and 7.*
- [ ] **Step 9: End-to-end verification** — Run a no-LLM yStocker self-test, a mocked seven-role graph test, and—with explicit use of configured provider credits—a single real A-share smoke run (for example 600519) confirming seven streamed reports, both debate layers, final decision, web split, and PDF. *Depends on: Step 8.*
- [ ] **Step 10: Review diffs and prepare two atomic commits** — One commit in TradingAgents for engine/data work and one in yStocker for product/deploy work; update this checklist with test evidence and suggested messages. Do not push or deploy without separate authorization. *Depends on: Step 9.*

## 3. Impact Analysis

* **Affected consumers**:
  * The CLI/programmatic TradingAgents API can opt into the three new keys; its default remains four analysts for backward compatibility.
  * yStocker A-share jobs explicitly request seven analysts. Existing stored four-role reports remain readable because parsing is additive.
  * Bull/Bear and all risk debaters receive larger prompts, increasing token use and run duration.
  * The deployment venv gains mootdx only if compatibility passes; all HTTP fallbacks remain usable when TDX TCP is unavailable.
  * PDF rendering consumes `ROLES`, so new report sections inherit role badges/colors without separate PDF-specific role duplication.
* **API surface changes**:
  * New valid analyst keys: `policy`, `hot_money`, `lockup`.
  * New state keys: `policy_report`, `hot_money_report`, `lockup_report`.
  * New data tools are public through `agent_utils` and vendor routing.
  * Existing HTTP endpoints and job payload schema remain backward-compatible.
* **Performance**:
  * A-share runs add three LLM/tool loops and additional source calls. The existing single-run semaphore remains unchanged.
  * Eastmoney requests are serialized and jittered; mootdx/Tencent/Sina/Tonghuashun calls are not placed under the Eastmoney limiter.
* **Security/compliance**:
  * Ticker/path safety remains enforced before network or file access.
  * No new credentials are required for these sources.
  * Existing investment disclaimer remains; documentation will identify unofficial/public source fragility.

## 4. Edge Cases

* A-share forms: `600519`, `SH600519`, `600519.SH`, `600519.SS`, Shenzhen/Beijing equivalents, lower-case input, and whitespace.
* US or other non-A-share ticker must use four analysts and must not call A-share endpoints.
* ST, STAR/ChiNext, Beijing 920/43/83/87 ranges, suspended/delisted symbols, trading holidays, and dates before listing.
* Historical analysis must not silently use current fund-flow, valuation, forecast, or lock-up snapshots; unavoidable current snapshots carry explicit warnings.
* mootdx absent, import failure, TDX port blocked, selected server dies after initialization, all canaries fail, or modern-httpx incompatibility.
* Eastmoney 429/block page, malformed JSONP/JSON, empty HTTP 200, stale series, timeout, partial response, and concurrent analyst requests.
* Sina/Tonghuashun markup/encoding changes and missing consensus coverage.
* No Dragon-Tiger appearance, no upcoming unlock, no analyst forecast, no pledge data: analyst should report “no verified evidence” rather than inventing activity.
* One or more analyst reports empty because a model fails to use tools: graph still completes, but downstream prompts and UI clearly identify missing evidence.
* Old reports using `Lock-up Watcher` versus new canonical `Lock-up Monitor`; parser aliases both without treating arbitrary headings as roles.
* Detached child or Gunicorn worker dies after some of seven events: salvage preserves every available role in canonical order.
* DynamoDB payload expansion remains below the compressed 380 KB guard; oversized jobs continue to fail explicitly rather than truncate silently.

## 5. What I'm NOT Changing

* No Streamlit UI, source-fork history store, PDF implementation, or Claude subscription SDK is imported.
* No change to yStocker authentication, quotas, paid credits, job ownership, DynamoDB schema, or public API routes.
* No parallel agent runs; concurrency remains one to protect the production host.
* No automatic push, production deploy, SSM mutation, or paid live LLM run without separate authorization.
* No forced seven-role analysis for US/non-A-share instruments.
* No silent fallback to an unconfigured vendor and no suppression of source errors that affect report confidence.

## 6. Test Plan

| Test | Type | Validates |
|---|---|---|
| Existing TradingAgents suite before/after | Regression | Local fork capabilities remain intact. |
| `test_astock_signal_data.py` | Unit | Direct-source parsing, throttling, fallbacks, point-in-time guards, and failure normalization. |
| `test_mootdx_compatibility.py` | Unit + opt-in integration | Modern-httpx environment degrades safely; optional TDX quote works when enabled. |
| `test_astock_analyst_pipeline.py` | Unit | Seven roles are constructible in order with exact baseline tools and complete ToolNodes. |
| `test_seven_report_downstream.py` | Unit | Bull, Bear, aggressive, conservative, and neutral each receive all seven unique reports. |
| `test_reporting.py` | Unit | Canonical three new files/headings and complete seven-section report. |
| `test_astock_trading_constraints.py` | Unit | A-share-specific execution rules do not leak into US analysis. |
| `test_agent_roles.py` | Unit | Web/PDF parser recognizes canonical/legacy headings while preserving nested Markdown headings. |
| `test_agents_astock_selection.py` | Unit | yStocker selects 7 roles for A shares, 4 elsewhere, and streams all seven state fields. |
| yStocker no-LLM self-test | Integration | Queue, detached child, event file, result extraction, report split, and PDF remain functional. |
| One authorized 600519 smoke run | Live E2E | Real sources + LLM produce seven reports, Bull/Bear, three-way risk debate, and final portfolio decision. |

Required commands will be recorded with results in this file during execution. A live-source test failure is reported separately from deterministic unit failures; it does not justify weakening mocked correctness checks.

## 7. Rollback Notes

* **Safe revert point**: Every step is source-only and split across two feature branches. Before push/deploy, either branch can be discarded without affecting production.
* **Deployment rollback**: The two repositories deploy independently. Record each pre-deploy SHA; revert yStocker and TradingAgents to those SHAs together if the production smoke check fails.
* **mootdx rollback**: The adapter is fail-open to HTTP sources. Removing/disable-setting mootdx must not require reverting the seven-role graph.
* **Stored data**: Existing/new job records are additive JSON. Rolling code back leaves seven-role reports as Markdown; old code can still show unknown sections as report body, but canonical rich rendering requires the new role registry.
* **Points of no return**: None in implementation. Push, deploy, and paid LLM calls are intentionally outside the approved implementation action set.

## 8. Pseudo-code

```python
# yStocker chooses graph shape before construction; TradingAgents stays generic.
BASE_ANALYSTS = ("market", "social", "news", "fundamentals")
ASTOCK_ANALYSTS = BASE_ANALYSTS + ("policy", "hot_money", "lockup")

analysts = ASTOCK_ANALYSTS if is_a_share(ticker) else BASE_ANALYSTS
graph = TradingAgentsGraph(
    selected_analysts=analysts,
    config=config,
    progress_callback=publish_progress,
)
```

```python
# Minimum tools are never lost when richer A-share evidence is added.
POLICY_TOOLS = [get_news, get_global_news]
HOT_MONEY_TOOLS = [
    get_stock_data, get_news, get_insider_transactions,
    get_dragon_tiger_board, get_fund_flow, get_hot_stocks,
]
LOCKUP_TOOLS = [
    get_insider_transactions, get_news, get_fundamentals,
    get_lockup_expiry,
]
```

```python
# Every downstream debater gets a single, explicit seven-report resource block.
def analyst_resources(state):
    return {
        "market": state.get("market_report", ""),
        "sentiment": state.get("sentiment_report", ""),
        "news": state.get("news_report", ""),
        "fundamentals": state.get("fundamentals_report", ""),
        "policy": state.get("policy_report", ""),
        "hot_money": state.get("hot_money_report", ""),
        "lockup": state.get("lockup_report", ""),
    }
```

```python
# Source priority is explicit; Eastmoney limiting applies only to Eastmoney.
def load_astock_ohlcv(code, date):
    for source in (mootdx_if_compatible, tencent, eastmoney_limited, sina):
        try:
            data = source(code, date)
            if valid_non_stale(data):
                return data
        except SourceUnavailable as error:
            record_fallback(source, error)
    raise NoMarketDataError(code)
```
