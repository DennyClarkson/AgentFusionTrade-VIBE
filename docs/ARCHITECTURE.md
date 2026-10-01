# Architecture · native EA execution

Standalone Python application at 127.0.0.1:8787, static Chinese UI, no frontend build or remote assets.

```text
AI workspace:
MT5 closed bars -> causal indicator packet -> market -> background -> judge -> risk
                          bounded tools / upstream consultations / scoped memory
                                                            -> refreshed risk gate
AI workspace -> serialized Python gateway -> durable intent -> demo MT5
EA workspace:
FusionExecutor: closed M1/H1/D1 -> native risk/intent -> native entry/protection
SQLite conversations <-> separate AI manager -> replay -> bounded parameter proposal
                        -> instance-qualified expiring entry permission (not orders)
MT5 SL/TP exit fills -> durable account-scoped queue -> individual AI parameter reviews
pause ack + flat + proof + versions -> publish -> native ack -> save active version
Exactly one workspace opens positions; positive paused/flat handoff required
GDELT/Fed worker -> source cache + account-qualified EA calendar + manual events
MCP stdio -> loopback API; no arm/start/raw order tools
MQL5 companion <-> typed telemetry/calendar/signal/parameter-acknowledgment files
```

## Modules

- config/store: eleven independently versioned categories, immutable history, SQLite cycles/intents/conversations/scoped memory. Credentials are environment names only.
- indicators: causal EMA/Wilder ATR/RSI/ADX, MACD, Bollinger, volume, support/resistance and freshness/warmup/gaps. Epoch and ISO UTC provenance.
- ai/prompts: actual bounded function loop, ancestor-only consultations with no recursive tools, no order or code execution tools. DeepSeek reasoning_content replay remains private; browser sees public evidence summaries and tool audit.
- background: autonomous fixed-source GDELT/Fed refresh, bounded payload/cache, source errors/freshness, manual calendar precedence. News is untrusted data, never executable instructions. Missing news and missing calendar remain separate facts.
- strategy/planning: deterministic entry/trailing, validated Agent plans, ATR/session/event risk scaling, failure and expiry gates. AI can reduce/veto configured budgets.
- engine/broker: account-pinned serialized demo gateway; refresh after inference, recheck after preflight and persistence. OrderNotSubmitted distinguishes confirmed local rejection from uncertain order_send outcomes. No automatic ambiguous retries. Owned SLTP can only tighten, with configured freshness rechecked at send.
- ea_manager: no-order background management, persistent chat/thinking, actual tools, candidate replay and bounded proposals. Persist running/latest job identity and public model/tool progress; restart marks unfinished work interrupted. Model errors remain errors; final state/persistence cleanup is guarded. AI controls entry participation only inside an armed/running EA session. Publication is not application; save an active profile only after native acknowledgment.
- exit_review: SQLite queue keyed by account/server/symbol/Magic scope and exit deal ticket. Recognizes MT5 SL/TP reasons on OUT/OUT_BY trade deals, including partial fills and profitable trailing stops. History snapshots pin identity and preserve raw and normalized timestamps; insertion and scan checkpoint commit together. Default five-second scans with 24-hour overlap find delayed rows, even while AI is busy. One queued event starts one review, without interval/count thresholds; failed work backs off, interrupted work stays pending, completion-storage failures retry persistence without rerunning AI. Restart never dispatches before the user starts management.
- ea_control: canonical typed config, boot/session-qualified leases, expiry bounded by AI decision, stopped-state watcher, positive paused/flat handoff, retained close requests. A new EA boot stops the current session. Native demo cycles never call Python order/protection paths.
- research: shared causal signals, chronological train/validation/cost stress, next-bar fills, conservative intrabar conflict. Close-based trailing becomes effective next bar. No current news injected into history.
- agent_lab: isolated real-model comparisons/scenarios with latency; no order path or live-memory contamination.
- app: loopback host/origin checks and per-process mutation token. AI pipeline/latest decision and EA latest decision isolated.
- static/markdown: fixed-tag Markdown subset with pipe tables, header alignment, inline code/bold, lists and fenced code. Model HTML is escaped; table scrolling is local to the report.
- mcp_server: official MCP Python SDK v1; 14 tools through app API.
- ea/FusionBridge: FUSION2 compatibility; FUSION3 telemetry; typed FUSION_EA1 strategy parameters; account/server-qualified acknowledgment and UTC calendar. No OrderSend, DLL or WebRequest. Fixed-template generation does not execute arbitrary AI code.
- FusionExecutor/FusionKernel: root-authored native risk/orders/protection; local exclusive file handle; demo identity; remaining daily risk, margin, session/calendar gates; durable consumed bar and intent; broker-evidence reconciliation. No arbitrary model code, DLL or WebRequest. Invalid state blocks execution; missing active configuration explicitly reports broker protection only.

## Lifecycle and data

Restart is stopped/disarmed. Config/module switching requires idle/stopped/disarmed, native pause acknowledgment, flat account and no uncertain intents. Navigation does not switch ownership. Stop invalidates entry permissions/proposals. Native EA continues protection while paused or Python is disconnected; removing EA leaves broker SL/TP only. A new EA boot requires a new user-started session.

Agent memory is scoped by symbol, timeframes, node and config hash. EA public conversations survive restarts; compatible private tool protocol preserves thinking history, incompatible configuration invalidates it. Current environment facts belong in handoff, not reusable skill rules.

Automatic EA review conversations are scoped by account/server/symbol/Magic; event reviews always use the captured, validated scope. The first monitoring boundary is established before native session start, so existing old history is not replayed on first use. Later restarts retain the original boundary/checkpoint. Current observed configuration versions are labeled as observation evidence, not claimed as the executed trade's parameter version. Parameter assessment may conclude that evidence is insufficient to change anything.

## Limits

Demo only. Replay is linear XAUUSD/USD with current contract specs as historical approximations, no complete historical event/tick path. Historical AI scenarios are sparse experiments. News freshness is not calendar completeness. No claim of profitable strategy or exact live/replay parity.

Development checks verified FusionExecutor connection, account-qualified config acknowledgment and calendar. Each new installation must repeat its own readiness checks. Native fills, protection lifecycle and cross-regime parity remain unverified. The local lock cannot coordinate another machine trading the same account. See NATIVE-EA.md for setup and HANDOFF.md for validation status. MCP is optional external integration; internal Agent tools do not require an MCP client.

There is no cumulative token quota. AI output_token_policy=model_max discovers the configured provider/model max_output_tokens; fixed uses its explicit numeric limit and provider_default omits the request field. Missing model metadata is visible and calls for a compatible policy. Provider context windows, per-response limits, latency/plan expiry and bounded capabilities are separate constraints.

## Primary sources

PA_Agent and AlphaMaster are behavior-only AGPL references; no code, prompts or assets copied.

- [MT5 Python](https://www.mql5.com/en/docs/python_metatrader5)
- [MT5 deal properties and SL/TP reasons](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties)
- [DeepSeek thinking/tool history](https://api-docs.deepseek.com/guides/thinking_mode/)
- [GDELT DOC](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [Federal Reserve RSS](https://www.federalreserve.gov/feeds/feeds.htm)
- [MT5 calendar](https://www.mql5.com/en/docs/calendar/calendarvaluehistory)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
