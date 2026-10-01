---
name: fusion-development
description: Develop or review AgentTradeFusion, the local Python MT5 agent workbench, including its risk gateway, configuration, backtests, MCP and EA bridge. Use only in this project; not for unrelated financial advice or live-account operations.
---

Read `docs/HANDOFF.md` in the AgentTradeFusion root first. It owns current progress, tests, environment observations, pending work and authorizations. Read `docs/ARCHITECTURE.md` and relevant source for the subsystem being changed. Update handoff after meaningful milestones and before yielding; record both verified outcomes and unverified assumptions.

## Ownership

The user requires the root coordinator to personally author all critical logic and code: strategy, historical simulation/research, risk, broker execution, AI orchestration, configuration/persistence, MCP and EA. Delegate only presentation work or independent read-only research/review. Subagents must explicitly use **gpt-6.1-sol**, normally medium reasoning and high for difficult correctness review. Do not substitute if unavailable. Use clean task context, explicit file ownership and acceptance criteria; children must not delegate. Root reviews evidence, integrates and tests. Tool-requested model and actual server metadata are different facts; report them accurately.

## Invariants

- Python is the application core and AI-workspace executor; the EA workspace executes inside the root-authored native MQL5 EA. Its AI manager only reviews, proposes validated parameters and grants bounded entry permission inside a user-armed session. Exactly one workspace may open trades. After native entry permission has been published, require fresh native pause acknowledgment, flat exposure and resolved orders before Python ownership; lease expiry alone is insufficient. Never equate a file write with acknowledgment.
- Store AI connection, prompts, strategy, risk, workflow, context, broker and Agent DAG logic as separately validated/versioned profiles. Key configuration stores environment names only. Redact provider errors/metadata; never copy keys to logs or client responses.
- Signals use ascending, closed bars; entry cannot see future bars. Reuse indicator/signal definitions in replay and live analysis. Data freshness and provenance must remain visible.
- Default raw MT5 time correction is zero. Apply nonzero correction only after measuring a terminal-specific anomaly; preserve raw timestamps, document evidence, and verify deal-time behavior separately. Ordinary broker timezone alone is insufficient evidence.
- Risk rules are deterministic and independent of LLM confidence. Floor volume to step and reject minimum-lot budget violations. Include costs, available margin, existing account exposure, session/news rules and daily loss. Estimated stop risk is not guaranteed realized loss.
- A risk Agent may reduce budget, veto participation or propose protection distances; always resize from final distances. Share DAG failure gates and plan expiry checks across live analysis and historical Agent experiments. Recheck plan and quote freshness after preflight and persistence, immediately before send.
- Calendar availability is explicit: absent events are not proof of no event. MQL5 calendar trade-server time conversion is independent of Python raw quote timestamp correction. Historical AI experiments must not inherit today's news or pass future candles.
- Execution is demo-only, stopped and disarmed on restart; session arming pins account and server. Recheck account and market immediately before submission. Preserve broker SL/TP. Do not auto-resume or enable real accounts.
- Persist execution intent before a send; preserve uncertainty after timeouts/partial fills. Never retry an ambiguous trade automatically. Reconcile against broker evidence. Preserve per-account ownership and transactional paper PnL/idempotency.
- Distinguish confirmed local rejection before entering order_send from ambiguous submission. A rejected preflight must not create an unresolved journal lock. Protection updates may only tighten owned stops and must recheck the configured quote-age limit at final submission.
- AI-trading and EA-manager state/memory are separate. Native EA protection continues while entries are paused or the manager is disconnected; startup never reuses entry permission from a previous EA boot. Native execution enforces demo/account pin, exclusive executor ownership, durable no-repeat intents, independent risk/session/event limits and tightening-only protection. Keep LLM work outside this loop; parameter publication/application requires cancellation generation, configuration versions, bounded validation proof, acknowledged entry pause and flat account. Never present exported parameters as acknowledged before fresh matching EA telemetry.
- Tool calls use explicit bounded schemas and fixed capabilities. Upstream consultations can only target completed ancestors and cannot recursively acquire order/code tools. Preserve provider-required private reasoning state internally, exposing evidence summaries rather than raw reasoning protocol.
- Do not impose a cumulative token quota. Keep provider per-response/context limits and stale-decision deadlines distinct from token accounting; model capability discovery must be explicit and configurable. Surface real task failures and confirmed tool side effects, including when a final model answer fails after a tool completed.
- Changing config during a run, in-flight analysis or managed exposure must not invalidate ownership or risk state. Cancellation invalidates precomputed proposals.
- Research separates training/validation/holdout chronologically, uses explicit transaction costs, reports sample sufficiency and parameter sensitivity, and does not call a strategy stable/profitable on an in-sample score alone.

## Validation / handoff

Use `uv sync --python 3.12`, `.venv/Scripts/python.exe -m pytest -q`, and the documented read-only probe for normal validation. Run authorized demo smoke separately, bounded by the agreed risk envelope, close owned test exposure, and leave execution stopped/disarmed. UI assertions and compiler success do not establish profitable strategy performance. Record exact checks, results, outstanding limitations and how to launch in handoff.

Keep the skill stable: current account IDs, prices, results, model availability and completed tasks belong in handoff or research artifacts. Broaden a rule only for a demonstrated reusable invariant.
