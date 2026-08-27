# Research: TradingAgents-Astock Seven-Analyst Integration

## 1. Context & Scope

* **Requested outcome**: Upgrade the yStocker/TradeAgents product from four analyst reports (market, sentiment, news, fundamentals) to seven by adding policy, hot-money, and lock-up analysts, backed by the free A-share sources used by `simonlin1212/TradingAgents-astock` (mootdx, Eastmoney, Sina, Tonghuashun; plus its supporting Tencent/CLS sources).
* **User-confirmed role contract**:
  * **Policy Analyst / 政策分析师** — regulatory policy, industrial policy, and window guidance; baseline tools: `get_news`, `get_global_news`.
  * **Hot Money Tracker / 游资追踪师** — Dragon-Tiger List, large-order flow, and main-capital activity; baseline tools: `get_stock_data`, `get_news`, `get_insider_transactions`.
  * **Lock-up Monitor / 解禁监控师** — restricted-share unlocks, major-shareholder reductions, and equity pledges; baseline tools: `get_insider_transactions`, `get_news`, `get_fundamentals`.
  * All seven analyst reports are mandatory inputs to both the Bull/Bear research debate and the three-way aggressive/conservative/neutral risk debate. This is an end-to-end decision-chain requirement, not only a report-display requirement.
* **Repositories involved**:
  * `/Users/yuanxili/workspace/ystocker` is the Flask product, job supervisor, report renderer, quota/credit layer, and production deploy owner.
  * `/Users/yuanxili/workspace/TradingAgents` is the locally checked-out `15th-Ave-NE/TradingAgents` fork. Production deliberately installs this fork at `/opt/tradingagents`; it is not vendored into yStocker.
  * Source reference: `https://github.com/simonlin1212/TradingAgents-astock` (Apache-2.0, currently advertising package version 0.5.15).
* **Primary files investigated in detail**:
  * yStocker: `ystocker/agents.py`, `ystocker/agent_roles.py`, `ystocker/routes.py` agent endpoints, `ystocker/templates/agents.html`, `ystocker/templates/_agents_float.html`, `ystocker/static/agents_float.js`, `ystocker/__init__.py`, `deploy/install-tradingagents.sh`, and the TradingAgents sections of `deploy/deploy.sh`.
  * Existing fork: `tradingagents/graph/trading_graph.py`, `tradingagents/graph/setup.py`, `tradingagents/graph/analyst_execution.py`, `tradingagents/graph/conditional_logic.py`, `tradingagents/graph/propagation.py`, `tradingagents/reporting.py`, `tradingagents/default_config.py`, `tradingagents/dataflows/a_stock.py`, `tradingagents/dataflows/interface.py`, `tradingagents/agents/utils/agent_states.py`, `tradingagents/agents/utils/agent_utils.py`, and `pyproject.toml`.
  * Source fork: README/README_en, CLAUDE.md, CHANGES_FROM_UPSTREAM.md, CHANGELOG.md, `pyproject.toml`, and current source views of `tradingagents/dataflows/a_stock.py`, `tradingagents/graph/trading_graph.py`, `tradingagents/graph/setup.py`, and `tradingagents/agents/utils/agent_states.py`.
* **Current as-is flow**:
  1. yStocker validates and queues an agent job, then launches a detached short-lived process using the separate TradingAgents virtual environment.
  2. The child constructs `TradingAgentsGraph` with its default analyst list. The current fork defaults to the original four analysts.
  3. A yStocker-only `progress_callback` receives full graph-state snapshots and appends per-role JSONL progress events.
  4. TradingAgents writes a complete Markdown report tree; yStocker stores the report and splits exact role headings for web/PDF rendering.
  5. Deployment independently updates yStocker and `15th-Ave-NE/TradingAgents`, then installs the latter in `/opt/tradingagents/venv`.

## 2. Intricacies & Findings

* **This is a two-repository feature**: changing only `ystocker/agent_roles.py` would create three empty avatars. The three roles must first exist in the TradingAgents graph, state, tools, prompts, downstream debates, report writer, and tests; then yStocker must recognize and stream them.
* **The existing fork is not stock upstream**: it carries important local work absent or materially different in TradingAgents-Astock, including `progress_callback`, streamed final-state handling, checkpoint signatures, multi-market vendor fallback, deterministic instrument identity, verified-market snapshots, configurable benchmark maps, Gemini model support/fallback, and an indicator-cache race fix. Repointing deployment directly to the third-party A-stock repository would regress these features.
* **The A-share data layer is present but incomplete relative to the requested source set**:
  * Current local fork: Eastmoney + Sina + Tonghuashun, with Eastmoney OHLCV and Sina fallback. It explicitly removed mootdx after observing the package import `httpx` and conflict with Gemini's modern `httpx` requirement.
  * TradingAgents-Astock: a much larger vendor (about 2,252 lines) with mootdx/Tencent priority, Eastmoney throttling, Sina/Tonghuashun fallbacks, Dragon-Tiger board, lock-up expiry, fund flow, hot stocks, concept blocks, northbound flow, profit forecast, and industry comparison.
  * Therefore the role port requires the supporting signal-tool surface, not merely the three analyst files.
* **mootdx/Gemini dependency conflict is real at package-resolution level**: mootdx 0.11.7 declares `httpx>=0.25,<0.26`, while `langchain-google-genai`/`google-genai` requires modern `httpx` (the source project documents `>=0.28.1`). TradingAgents-Astock's documented workaround is to install mootdx and then deliberately override its declared httpx constraint because TDX quote traffic uses TCP. The current local fork previously rejected this workaround after observing that importing mootdx still imports `httpx`. These facts do not prove runtime breakage, but they make a blind dependency merge unsafe. The implementation needs an isolated compatibility test before enabling mootdx in the production Gemini environment, with Eastmoney/Sina fallback retained.
* **All seven reports must flow downstream**: TradingAgents-Astock explicitly calls out an earlier bug where new report fields existed but Bull/Bear and all three risk debaters still consumed only four reports. The port must inject `policy_report`, `hot_money_report`, and `lockup_report` into Bull, Bear, trader, aggressive, conservative, neutral, research manager/quality gate where applicable, and final portfolio reasoning.
* **Baseline versus enriched tool sets**: the three user-confirmed tool lists are the minimum role contract. Dedicated A-share endpoints such as Dragon-Tiger detail, fund flow, lock-up calendar, pledge/reduction records, and industry comparison may be exposed to those roles as additive evidence, but must not replace or bypass the confirmed baseline tools.
* **Graph wiring is cross-cutting**: each analyst needs a state field, factory export, execution-plan entry, conditional tool router, graph node, tool node, initial-state value, report section, and downstream prompt consumption. Missing any one creates either a startup error, an unexecuted role, or a silently omitted report.
* **yStocker uses exact report headings as a protocol**: `agent_roles.split_sections()` intentionally recognizes only exact names to avoid mistaking model-authored subheadings for speakers. New report headings and aliases must match the TradingAgents report writer exactly. Based on source naming, the intended display identities are Policy Analyst, Hot Money Tracker, and Lock-up Monitor/Watcher; the final canonical headings must be taken from the code being ported and covered by tests.
* **Live progress also has an explicit state-key allowlist**: the embedded child runner's `PLAIN` table currently observes only four analyst fields. Without adding three mappings, completed reports would eventually display but users would see no live output from the new analysts.
* **Resource/cost impact**: seven analysts add three tool-calling LLM nodes and more data requests before the existing 3-round Bull/Bear and risk debates. The current one-run semaphore and 90-minute timeout remain important. Eastmoney calls must remain serially throttled with jitter to avoid temporary blocking.
* **Deployment ownership is clear**: production will only receive TradingAgents code after it is committed/pushed to `15th-Ave-NE/TradingAgents`; yStocker's deployment script already tracks that fork. The yStocker repository should not silently switch `TA_REPO` to the source fork.
* **Licensing**: both upstreams are Apache-2.0, but copied/derived files should preserve notices and the TradingAgents fork's NOTICE/attribution should be updated if source code is transplanted.

## 3. The "Invisible" Assumptions

* The phrase "my fork - ystock" means the existing paired yStocker + `15th-Ave-NE/TradingAgents` deployment, not replacing the Flask app with TradingAgents-Astock's Streamlit UI.
* Existing US/non-A-share analysis should continue to work; the seven A-share-specialized roles should be selected for A-share runs, while non-A-share runs should retain the current four-role/multi-market behavior unless explicitly configured otherwise.
* Gemini remains the production provider. A solution that drops Gemini to satisfy mootdx's declared dependency range is not acceptable.
* The free direct sources are allowed to fail over. “mootdx + 东财 + 新浪 + 同花顺” is interpreted as a resilient source chain, not a requirement that every run must successfully call every provider.
* No database schema migration is needed: job payloads store report text/events as flexible JSON and role sections are derived at read time.

## 4. Potential Friction Points

* The requested source fork has diverged substantially from both TauricResearch and the user's fork. A wholesale merge is likely to conflict in graph construction, provider clients, checkpointing, reporting, data routing, and CLI code.
* TradingAgents-Astock is A-share-only in its current vendor validation, while the user's fork intentionally supports US and other instruments through ordered vendor fallback. The A-stock vendor must continue to decline non-A-share symbols without network calls so fallback remains correct.
* The current local `a_stock.py` has no signal-data category or wrappers for Dragon-Tiger, lock-up, fund-flow, hot-stock, concept, northbound, forecast, and industry-comparison tools. These additions affect `interface.py`, wrapper exports, ToolNodes, and tests.
* There is no `tests/` directory in the yStocker repository. New yStocker parser/progress tests need either a small new test module and pytest dependency assumptions, or a standard-library unittest module runnable in the current environment.
* The two repositories are both on `main`. Implementation should use `codex/` feature branches in each repository to avoid mixing a cross-repo port directly into main.
* Source endpoints are unofficial/public web APIs and may change. Tests must mock responses for deterministic coverage; a separate opt-in live smoke test should verify current reachability without making the unit suite depend on the internet.

## 5. Proposed Next Steps for Planning

* Port by capability, not by repository merge:
  1. Add the missing A-share signal APIs and wrappers to `15th-Ave-NE/TradingAgents`, preserving the current vendor router, progress callback, checkpointing, multi-market behavior, and provider clients.
  2. Add the three analyst nodes and seven-report state/downstream/reporting pipeline, selecting all seven for A-share symbols and preserving four analysts for other markets.
  3. Add deterministic unit tests for ticker routing, Eastmoney throttling/fallback, tool registration, graph execution plan, report assembly, and downstream inclusion.
  4. Resolve mootdx as an optional/controlled compatibility layer: install without forcing an old httpx, test import + a TCP quote under the same Gemini environment, and retain HTTP fallbacks if mootdx is unavailable.
  5. Update yStocker's role registry, live-progress state mapping, self-test fixture, environment diagnostics, UI copy, and report/PDF rendering tests for all seven analyst sections.
  6. Update attribution and deployment preflight so production verifies the seven-role graph and data dependencies before restart.
* Do not import the source Streamlit UI, history store, payment model, or Claude-subscription provider; yStocker already owns those product concerns.
