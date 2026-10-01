# AgentFusionTrade-VIBE · development handoff

Updated: 2026-10-01. This is the public VIBE development snapshot. The application/package/protocol names remain AgentTradeFusion / fusion / FUSION_EXEC1. A future manually developed version is separate work, not silently substituted here.

## Read first

- Follow [AGENTS.md](../AGENTS.md) and the [stable project skill](../.agents/skills/fusion-development/SKILL.md).
- Python owns the workbench and AI managers. FusionExecutor owns the original EA workspace; the new independent FusionPIDTrader owns its own native Tick execution and chart interface. Never attach both as active execution owners. AI cannot arm a stopped session.
- All critical implementation remains root-owned. Subagents are limited to presentation or independent read-only review, explicitly GPT-6.1-Sol, without further delegation.
- Keep this file current after meaningful changes. Machine-specific accounts, paths, conversations and detailed operation logs belong in ignored local records, not this public handoff.

## Current delivery: native FusionPIDTrader 1.00

The user's latest direction is an installable MT5 EA with its interface inside MT5, minimum-lot trend pullbacks, Tick-level PID exits and optional AI review. Root authored all critical native/Python implementation and tests. Two GPT-6.1-Sol children performed independent read-only native and manager reviews; their findings were integrated.

- [Native setup and controls](PID-EA.md); source `integrations/mt5/FusionPIDTrader.mq5` and `FusionPIDCore.mqh`. The compiled EA was installed in a separate terminal Experts directory and packaged locally with source and instructions. Binaries/generated evidence remain ignored by Git; fresh clones compile with MetaEditor.
- Native chart pages: trade controls and 300-Tick curve; PID settings and UTC trading hours; AI question/review controls and latest conclusion. Local PID boundaries and actual broker SL also appear on the price chart. No browser is needed for execution or these controls.
- Closed M5 trend/M1 Bollinger-Stoch context, three closed D1 range location, Tick reversal entry, minimum lot only, risk including wider emergency stop/costs, account-wide exposure guard, daily loss/margin/session checks, and cooldown. USD demo only; ordinary chart startup is paused. Preview/tester state is isolated from live manager discovery.
- Default estimated net +3 USD activates PID. Check the previous boundaries before moving the controller; either crossing latches exit. Anti-windup, filtered derivative, gap reset and speed cap are native. A separate broker SL aims at +3 USD only when valid and acknowledged; this is not a guaranteed realized profit. Durable intent/reconciliation prevents ambiguous send retries. Existing protection continues when new entries pause.
- Optional `python -m fusion.pid_manager` / `Start-PID-AI.cmd`: separately persisted model/thinking/connection and prompt configuration, scoped history, `get_market`/`get_exits`, each confirmed exit fill queued separately, periodic/manual review. No cumulative token quota. Persist the validated model result before post-call reads/publication; retry its frozen revision rather than call the model again. A singleton lock prevents duplicate workers. Boot/session generations invalidate old controls.
- AI is advisory by default. `RequireAI` optionally gates user-armed entries. Parameter auto-application is disabled by default, bounded and flat-only, and currently does **not** use the original workbench's candidate-backtest approval chain. Independent news/calendar coverage is also not implemented; the prompt and documentation disclose this.

### Current validation and runtime state

- Full Python suite: **137 passed**, one upstream Starlette/httpx warning, 70.78 seconds. Includes 18 PID math tests and 12 manager tests for per-fill deduplication, stale/cancelled generations, scoped context, singleton ownership, persistence/publication retry and post-model telemetry failure. Node: **16 passed** (8 hours, 5 Markdown, 3 optional offline PID-reference tests). Whitespace check passed.
- Final MetaEditor compile: **0 errors, 0 warnings**, 1,037 ms. Workspace and installed `.ex5` SHA256 matched. Native controller reference vectors/crossing/anti-windup self-test passed in the actual MT5 tester.
- First uninterrupted native real-Tick replay: XAUUSD M1, 2026-09-29 through 2026-09-30, 100 ms execution delay, USD 3,000 test deposit, USD 1,000 strategy budget. **688,868 ticks / 1,377 bars; final balance USD 2,981.82 (-18.18)**. Minimum-lot entry, local stop exit, PID activation, broker SL modification/acknowledgment and broker stop fill were observed. This preceded final display/isolation/logging fixes and is functional evidence, not stable-alpha acceptance.
- Final binary was exercised again with the native panel: all three tabs, pause, flat settings save and manual AI-review queue creation were verified. Settings and request files were confirmed in an isolated tester directory. One full-period UI run ended at USD 3,004.20 **after manual entry pause**, so it is not an uninterrupted strategy performance result. The final interaction run was stopped explicitly while flat and paused. Actual manual close of an open position, partial fills and reconnect/uncertainty remain unverified scenarios.
- An isolated real official DeepSeek `deepseek-flash` probe with thinking enabled completed one persisted job and both real read-only tools. Usage: 1,817 prompt + 199 completion = 2,016 tokens. The packet was synthetic, paused and explicitly labelled; the only published permission was disabled in a temporary test directory. No live trading-control file was published by this probe.
- Existing user-started FusionExecutor/workbench session was preserved. New FusionPIDTrader was **not attached to a live chart and did not take over execution**. No existing backend restart or old EA replacement was performed. To switch, follow PID-EA.md: stop old management, require native pause/flat/resolved orders, remove old EA, then attach and manually start the new one. Default PID AI config/prompt were prepared locally; its long-running service was not started.
- `experiments/pid-tick-envelope.html` and Python/JS models are optional mathematical references only. They are not the plugin deliverable or a substitute for native acceptance.

## Original workbench implemented

- Sequential MT5 indicators -> market -> background -> judge -> risk, with per-node model/thinking/tool settings, upstream consultation and scoped memory.
- Eleven independently versioned configuration categories; credentials are environment-variable names only.
- Separate persistent EA conversation and manager, real tool calls, candidate replay, bounded parameter proposals and entry permissions.
- Each confirmed native EA SL/TP exit fill queues a separate AI parameter review, bypassing scheduled/minimum-count gates. Account/server/symbol/Magic scope plus deal ticket prevents duplicate discovery; pending work survives interruption. Reviews use scoped automatic conversations and retain validation/flat/acknowledgment requirements for parameter changes.
- Safe Markdown tables in conversations and AI activity reports: headers, alignment, inline code, long identifiers and local horizontal scrolling. Raw model HTML is escaped.
- Native FusionExecutor/FusionKernel: closed M1/H1/D1 trend/Bollinger strategy, risk and event gates, durable intents, SL/TP and tightening-only trailing, unknown-result reconciliation. Optional FusionBridge supplies telemetry/calendar.
- Exactly one execution owner. Native pause acknowledgment, flat exposure and resolved orders are required before parameter changes or ownership transfer. Restart stays stopped/disarmed.
- Public AI activity: persisted job identity, model/tool events, errors, latency and usage. No cumulative token quota. Per-response output policy supports model capability discovery, provider default and explicit fixed limits.
- Manual trading-hours UI: Beijing/UTC display, whole-hour start/end, cross-midnight/all-day, UTC weekends, current gate, saved version and separate draft. Save pauses management, waits for native pause, checks versions, saves, exports and waits for matching acknowledgment. It never auto-starts. End-of-window exits and independent zero-risk sessions are visible.
- Historical replay, cost stress, chronological splits and sparse historical AI experiments; 14 optional MCP tools through the local API.

## Previous workbench validation (before the native PID addition)

- Final Python full suite: **107 passed**, one upstream Starlette/httpx deprecation warning, 59.63 seconds. This includes all 18 exit-review tests after the final account-conversation race correction.
- Frontend JS: **13 tests passed** (8 trading-hours, 5 Markdown), including malformed tables, escaped/code pipes, fenced code and HTML injection. Both frontend scripts passed syntax checks.
- Exit-review tests use isolated broker history/model responses: per-fill deduplication, busy queue, delayed history, account/symbol/Magic filtering, raw/UTC timestamps, failed scans, backoff/recovery, stop cancellation, completion-storage retry without rerunning AI, worker-start failure, bounded prompts and account-scoped conversations. No live SL/TP trade was created for this change.
- Native compiler previously completed with zero errors/warnings; native connection, account-qualified parameter acknowledgment and calendar were observed locally. Compiled `.ex5` files are not shipped: rebuild in MetaEditor.
- Actual provider thinking/tool history, sequential consultation, persistent chat and public activity were exercised in earlier isolated probes. They are opt-in, may consume API usage, and are not required for offline regression.
- Browser checks covered both workspaces, actual AI activity and the new time editor. Public repository omits screenshots containing local account details.
- The user's existing six-column/two-row candidate report was verified as a real table in the running browser, with long-ID wrapping and narrow-screen horizontal scrolling. The static UI is loaded; the running Python process still uses the previous backend. The exit-review feature requires a workbench restart and a user-started EA session; no EA source update or recompilation is required.
- Root authored all critical changes and tests; a GPT-6.1-Sol child supplied table CSS and another performed read-only correctness review. Review findings on storage recovery, prompt size and account isolation were fixed and regression-tested. Existing live trading configuration and the user-started session were preserved.

These checks do not certify profitability, actual native fills or full protection lifecycle behavior. No strategy has passed stability acceptance. The M1 preset's initial validation and cost-stress results were negative; see [RESEARCH.md](RESEARCH.md).

## Run and verify

Use Windows, Python 3.12 and an MT5 demo terminal. From this checkout:

```powershell
uv sync --python 3.12 --locked
.venv\Scripts\python.exe run.py
.venv\Scripts\python.exe -m pytest -q
node --test tests/test_trading_hours.cjs tests/test_markdown.cjs
node --check fusion/static/markdown.js
node --check fusion/static/app.js
```

Configure the provider key in an environment variable (`DEEPSEEK` by default), never in tracked files. See [NATIVE-EA.md](NATIVE-EA.md) for source compilation, attachment, parameter acknowledgment and manual start; [INTEGRATIONS.md](INTEGRATIONS.md) for optional MCP. Fresh clones have no broker setup, conversations or research data.

For the independent native PID EA use [PID-EA.md](PID-EA.md), not the workbench launch sequence. Its optional manager starts with `.venv\Scripts\python.exe -m fusion.pid_manager`. Include `tests/test_pid_visual.cjs` in the Node command to run all 16 tests. The original workbench MCP does not automatically control the new independent EA.

The complete pre-publication handoff was retained locally as ignored `docs/HANDOFF.local.md`. It is not distributed and is not a prerequisite to run or develop this repository. Avoid reintroducing its private account/runtime details into commits. Data, logs, SQLite files, keys and generated reports are ignored.

## Remaining work

1. Complete bounded demo acceptance of native entry, fill/partial-fill/uncertain-result recovery, trailing and reconnect/restart protection; do not infer completion from compile or configuration ACK.
2. Compare native signals/protection against causal Python replay across market regimes and verify contract, cost, quote/time and calendar assumptions per terminal.
3. Gather sufficient independent out-of-sample data and cost sensitivity before calling any strategy stable. Current experiments remain research, not a performance claim.
4. Improve configuration ergonomics and continue separating operational evidence from archived local history.
5. Decide an explicit reuse license before describing the repository as licensed open source.

## Public repository

Published to [DennyClarkson/AgentFusionTrade-VIBE](https://github.com/DennyClarkson/AgentFusionTrade-VIBE), public, default branch `main`. Initial source snapshot: `588b310`. Public visibility and matching local/remote source revision were verified on 2026-10-01.

The initial snapshot included 62 files: complete Python/native/UI source, tests, lockfile, design sketch and development rules. Runtime databases, personal paths/account logs, secrets, binaries and generated evidence are excluded. Independent read-only publication and onboarding reviews completed; local Markdown links and MCP JSON/TOML examples validated. No reuse license was selected on the author's behalf. Publication did not restart the local app, change trading configuration or place trades.
