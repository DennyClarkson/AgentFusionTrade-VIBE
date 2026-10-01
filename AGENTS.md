# AgentTradeFusion development rules

Read `docs/HANDOFF.md` for current status and `.agents/skills/fusion-development/SKILL.md` for stable working rules before editing. Update handoff at milestones, interruptions and completion; keep volatile results out of the skill.

The user specifically requires the root coordinator to author all critical logic and code: strategy, backtesting, risk, execution, AI orchestration, configuration/storage, MCP and EA integration. Subagents may only handle noncritical presentation or read-only research/review. Every new subagent must explicitly use **gpt-6.1-sol**; do not silently substitute another model. No child delegation. Root integrates and verifies all changes.

Keep the application Python-based. Reference projects in sibling directories are read-only AGPL behavioral references, not code/prompt sources. Preserve LogicMap.jpg. Never read, print, store or transmit API keys except passing the configured environment value directly to its configured provider. Documentation should contain variable names, never key values.
