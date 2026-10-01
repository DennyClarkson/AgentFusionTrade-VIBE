> Historical v0.1 contract retained for legacy APIs. The current two-workspace behavior is specified by [V2-CONTRACT.md](V2-CONTRACT.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

# Historical v0.1 implementation contract

Ownership override: **all critical modules and tests are authored by the root coordinator**. Children may only implement static presentation or perform read-only review/research, and only with explicitly requested gpt-6.1-sol. Source schemas in `fusion/config.py` and live `/openapi.json` are authoritative; the lists below summarize interfaces rather than freeze schema evolution.

First version is local Windows FastAPI + static ES module UI, loopback port 8787. Source is independently authored; PA_Agent and AlphaMaster are behavioral references only. Use JSON-serializable dictionaries across module boundaries, UTC Unix seconds, ascending bars, no forming bar. No keys returned or logged. Python 3.12, pydantic 2. Backend root `fusion/`, static `fusion/static/`.

## Configuration data (active object per category)

- ai: `base_url`, `model`, `key_env`, `enabled`, `timeout_seconds`, `temperature`, `max_tokens`, `thinking` (bool).
- prompts: `market`, `context`, `critic`, `bull`, `bear`, `judge`, `tuner`, `risk_ai` (strings).
- strategy: `symbol`, `timeframe` (M1/M5/M15/M30/H1/H4), `context_timeframes` (list), `bars`, `fast_ema`, `slow_ema`, `breakout_bars`, `atr_period`, `stop_atr`, `reward_risk`, `max_hold_minutes`.
- risk: `capital`, `risk_per_trade_pct`, `daily_loss_pct`, `max_positions`, `max_spread_points`, `max_tick_age_seconds`, `max_slippage_points`, `max_margin_pct`, `magic`.
- workflow: `mode` (shadow/demo), `poll_seconds`, `ai_gate` (off/advisory/required), `min_confidence`, `require_context`, `session_start_utc`, `session_end_utc`, `block_weekends`.
- context: `events` (list of `{title,time_utc,impact,currency}`), `news` (list of `{title,summary,source,time_utc}`), `updated_at` (UTC epoch), `max_age_hours`, `blackout_before_minutes`, `blackout_after_minutes`. Context initially explicitly unavailable, manually importable; no invented news or calendar connection.

- broker: terminal path and exceptional measured raw timestamp correction (default 0).
- logic: topology, validated acyclic nodes (id/prompt/provider/dependencies), decision/risk nodes, bounded parallelism and plan TTL. See `planning.py` for shared failure/expiry guards.
- Additional risk fields: commission, full UTC session schedule and volatility thresholds. Additional context fields: specific event windows/source, caution phases/risk/protection factors, pre-event close and coverage note.

## Broker / strategy implementation (root ownership)

`fusion/broker.py`: `MT5Broker` serializes all MT5 calls. `status()->dict` returns `{connected,error,account:{login,server,currency,balance,equity,free_margin,trade_mode,demo,trade_allowed},terminal:{trade_allowed,tradeapi_disabled},symbols:[str]}`. No password/name. Init lazily. `snapshot(strategy:dict)->dict` returns `{symbol,tick:{bid,ask,time},spec:{point,digits,trade_tick_size,volume_min,volume_max,volume_step,trade_stops_level,trade_freeze_level,trade_contract_size,filling_mode},frames:{TF:[{time,open,high,low,close,volume}]},captured_at}`. Resolve exact configured symbol, error if missing. `positions()->list[dict]` all account positions with ticket/symbol/type/volume/price_open/sl/tp/profit/magic/time. `orders()->list[dict]` pending orders. `deals_since(epoch)->list[dict]` incl profit/commission/swap/fee/magic/symbol/time. None/error must raise, never silently return empty.

`loss_per_lot(symbol,side,entry,stop)->float` use `order_calc_profit` absolute; `margin(symbol,side,volume,entry)->float` use order_calc_margin. `check_order(request)->dict`, `send_order(request)->dict` serializable namedtuple results. `make_request(symbol,side,volume,entry,sl,tp,risk,comment)->dict` handles supported filling modes, tick normalization, market action. `close_position(ticket,risk)->dict` only for given risk.magic + current demo account; refresh tick, check then send. Demo check repeated inside every send, no real-account execution. `shutdown()`.

`fusion/strategy.py`: `analyze(snapshot,strategy)->dict` returns `{action:BUY/SELL/HOLD,reason,entry,sl,tp,atr,bar_time,indicators:{...},context:{...}}`. Deterministic EMA trend / Donchian breakout / ATR SL; multi-timeframe alignment. Null entry/sl/tp when HOLD. Closed bars only, data validation and sufficient warmup. `backtest(bars,strategy,risk,spec,costs=None)->dict` deterministic closed-bar signal, next-bar entry, pessimistic both-hit handling, spread+commission assumptions, equity curve and metrics; label research approximation. Reuse signal logic; no future data. Tests owned by root: `tests/test_strategy.py`, `tests/test_broker.py` (fakes, no live sends).

## Root ownership

`config.py`, `store.py`, `ai.py`, `engine.py`, `app.py`, `tests/test_engine.py`, `tests/test_api.py`, docs, scripts. Store active configs in SQLite version history, optimistic concurrency, audit/cycle/order records. Engine creates one locked cycle, preserves config versions, refreshes guards after AI, demo arm only in memory pinned to account/server, persistent order intent before send + no automatic retries when uncertain. Server restart always stopped/disarmed. Shadow uses same gates and paper ledger. No uncontrolled unattended launch.

## UI / API (frontend agent ownership: static/ only)

GET `/api/bootstrap` -> `{token,configs:[{category,id,name,version,data,active,updated_at}],schemas:{category:JSONSchema}}`. All mutations need `X-Fusion-Token` from bootstrap.
GET `/api/status` -> `{engine:{running,armed,mode,busy,last_error},connection:broker.status,market:snapshot or null,positions:[],paper_positions:[],metrics:{today_pnl,cycle_count},latest:cycle or null}`.
GET `/api/cycles?limit=30` -> list of `{id,created_at,status,signal,risk:{allowed,reasons,volume,risk_money},agents:[],execution,config_versions}`. Agent record `{role,status,summary,confidence,decision,model,latency_ms}`. Status updates every 5 seconds without overlapping calls.
POST `/api/cycle` -> cycle. POST `/api/engine/start`, `/stop` -> status. POST `/api/engine/arm` `{account_login:integer}` -> status. `/disarm`. POST `/api/positions/{ticket}/close` -> result (owned demo positions only). Stop stops new cycles and disarms, keeps broker SL/TP.
GET `/api/configs` -> config list. PUT `/api/configs/{category}/{id}` `{name,data,expected_version}` (0=new) -> saved config; active saved profiles remain active, updates while running rejected. POST `/api/configs/{category}/{id}/activate` -> config. GET `/api/configs/{category}/{id}/history` -> versions. POST `/api/ai/test` -> `{ok,model,summary,latency_ms,error?}`.
POST `/api/backtest` -> `{metrics,equity,trades,assumptions,...}` uses latest MT5 bars and current config. GET `/api/audit?limit=50` -> events.
API errors `{detail:string}`. No fabricated data. UI supports overview (chart + decisions + agent chain + risk), configuration (logical category tabs, form fields + advanced JSON, named profile create/save/activate/history), journal/backtest (trade table + equity/cycle inspection), data/context status + event/news JSON editing via config. Real functionality, explicit pending/errors/disabled state. Defaults no live send; demo arm is distinct button with account ID field.

## Extended research and integration API

Public `/api/backtest` now calls `research.replay`, sharing live causal rule features and dynamic session/volatility policies. The legacy `strategy.backtest` remains internal; it is not the public simulator. `/api/research/start` launches full chronological research, `/api/research` reads progress/result. Saved reports include config, source/data fingerprints, assumptions and failed acceptance reasons.

POST `/api/agents/compare` compares three graphs on one frozen quote snapshot; POST `/api/agents/replay` performs sparse, real-model historical scenarios with latency/TTL handling. GET `/api/agents/results` reads stored reports. These paths have no order sends. POST `/api/orders/reconcile` only resolves positive broker evidence.

GET `/api/integrations` returns MCP/EA/context status. POST `/api/ea/export` writes typed FUSION2 params; `/api/ea/generate` writes validated fixed-template MQL5 source; `/api/ea/import-context` creates a context revision from fresh account-matching EA calendar. No arbitrary code execution or chart attachment API.
