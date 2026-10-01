"""Native EA supervisor. Publishes configuration/entry leases, never broker orders."""
import csv
import hashlib
import io
import json
import os
import threading
import time
from pathlib import Path

from . import ea
from .config import validate
from .store import Conflict, uid


def write_packet(root, name, values):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    rows = list(values.items())
    if any(any(c in str(v) for c in '\r\n;"') for pair in rows for v in pair):
        raise ValueError("EA 协议字段包含非法分隔符")
    csv.writer(output, delimiter=";").writerows(rows + [("END", len(rows))])
    temp = root / (name + "." + uid() + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as f:
            f.write(output.getvalue())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, root / name)
    finally:
        temp.unlink(missing_ok=True)


def read_packet(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    if not rows or any(len(r) != 2 for r in rows) or rows[-1] != ["END", str(len(rows)-1)]:
        raise ValueError("不完整的 EA 数据包")
    result = dict(rows[:-1])
    if len(result) != len(rows)-1:
        raise ValueError("重复的 EA 协议字段")
    return result


def configuration(cfg, account):
    cfg = {k: validate(k, v) for k, v in cfg.items()}
    s, r, w, c, e = (cfg[k] for k in ("ea", "risk", "workflow", "context", "ea"))
    s = s["strategy"]
    if e["execution_magic"] == r["magic"]:
        raise ValueError("原生 EA 与 AI 交易必须使用不同 Magic ID")
    if not account.get("demo") or account.get("currency") != "USD":
        raise ValueError("原生 EA 当前仅允许 USD 模拟账户")
    values = {"protocol": "FUSION_EXEC_CONFIG1", "login": account["login"], "server": account["server"], "symbol": s["symbol"], "magic": e["execution_magic"]}
    for key in ("fast_ema", "slow_ema", "atr_period", "bollinger_period", "bollinger_deviation", "min_trend_atr", "trend_adx", "trend_slope_atr", "range_bias", "stop_atr", "reward_risk", "trailing_start_r", "trailing_atr", "breakeven_r", "max_hold_minutes"):
        values[key] = s[key]
    values["history_bars"] = max(600, s["slow_ema"]*3)
    for key in ("capital", "risk_per_trade_pct", "daily_loss_pct", "max_positions", "max_spread_points", "max_tick_age_seconds", "max_slippage_points", "max_margin_pct", "commission_per_lot", "volatility_reduce_ratio", "volatility_pause_ratio"):
        values[key] = r[key]
    for hour in range(24):
        session = next(x for x in r["sessions"] if x["start_utc"] <= hour < x["end_utc"])
        for key in ("risk_scale", "stop_scale", "target_scale"):
            values[f"hour_{hour}_{key}"] = session[key]
    for key in ("session_start_utc", "session_end_utc", "block_weekends"):
        values[key] = int(w[key])
    values["require_calendar"] = int(e["require_calendar"] or w["require_context"])
    for key in ("blackout_before_minutes", "blackout_after_minutes", "caution_before_minutes", "caution_after_minutes", "event_risk_scale", "event_stop_scale", "event_target_scale", "close_before_event_minutes"):
        values[key] = c[key]
    events = [x for x in c["events"] if x["impact"] == "high" and x["currency"].upper() in {"USD", "XAU", "ALL"}]
    values["event_count"] = len(events)
    for i, event in enumerate(events):
        values[f"event_{i}_time"] = int(event["time_utc"])
        for key in ("blackout_before_minutes", "blackout_after_minutes"):
            values[f"event_{i}_{key}"] = event[key] if event[key] is not None else c[key]
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    values["revision"] = int(digest[:13], 16)  # exact in JS and MQL long
    return values


class EAController:
    def __init__(self, engine):
        self.engine, self.store, self.broker = engine, engine.store, engine.broker
        self.session = uid()
        self.decision = {"enabled": False, "reason": "等待 AI 开仓许可", "expires_at": 0, "risk_scale": 0}
        self.last_command = None
        self.armed_instance = None
        self.halt = threading.Event()
        self.thread = None

    def start_watchdog(self):
        """Bind newly attached EA boots to a paused session even while UI is idle."""
        if self.thread and self.thread.is_alive():
            return
        self.halt.clear()
        def watch():
            while not self.halt.is_set():
                try:
                    with self.engine.control:
                        if not self.engine.running and not self.engine.armed:
                            state = self.status()
                            command = self.store.get("ea_native_command", {})
                            if state.get("connected") and (command.get("instance_id") != state["instance_id"] or command.get("session") != self.session or command.get("enabled")):
                                self.pause("已连接原生 EA，等待用户启动")
                except Exception as exc:
                    self.store.audit("ea.watchdog_error", {"error": type(exc).__name__})
                self.halt.wait(1)
        self.thread = threading.Thread(target=watch, daemon=True, name="fusion-native-supervisor")
        self.thread.start()

    def shutdown(self):
        self.halt.set()
        if self.thread:
            self.thread.join(timeout=2)

    def root(self):
        return ea.bridge_path(self.broker)

    def status(self):
        try:
            path = self.root() / "executor.csv"
            if not path.exists():
                return {"connected": False, "reason": "尚未挂载 FusionExecutor 交易 EA", "entries_enabled": False}
            row = read_packet(path)
            if row.get("protocol") != "FUSION_EXEC1":
                raise ValueError("未知原生 EA 协议")
            account = self.broker.status()["account"]
            age = time.time()-float(row["heartbeat_utc"])
            same = int(row["login"]) == account.get("login") and row["server"] == account.get("server")
            result = {"protocol": row["protocol"], "connected": same and -3 <= age <= 5 and -3 <= time.time()-path.stat().st_mtime <= 5,
                      "age_seconds": round(age, 1), "same_account": same, "read_only": False,
                      "instance_id": row["instance_id"], "symbol": row["symbol"], "version": row["version"],
                      "reason": row["reason"], "signal": row["signal"], "state": row["state"], "protection": row["protection"],
                      "entries_enabled": row["entries_enabled"] == "1", "pending": row["pending"] == "1",
                      "strategy_revision": int(row["config_revision"]), "command_revision": int(row["command_revision"]),
                      "lease_until": float(row["lease_until"]), "positions": int(row["positions"]), "magic": int(row["magic"]),
                      "calendar_fresh": row["calendar_fresh"] == "1", "bar_time": int(row["bar_time"]),
                      "last_retcode": int(row["last_retcode"])}
            if not result["connected"]:
                result["reason"] = "EA 遥测过期或账户不匹配"
                result["entries_enabled"] = False
            return result
        except Exception as exc:
            return {"connected": False, "reason": type(exc).__name__, "entries_enabled": False}

    def _send(self, enabled, cfg, state, close_ticket=0):
        account = self.broker.status()["account"]
        now = time.time()
        values = {"protocol": "FUSION_EXEC_CONTROL1", "revision": time.time_ns()//1000, "login": account.get("login", 0), "server": account.get("server", ""),
                  "symbol": cfg["ea"]["strategy"]["symbol"], "instance_id": state.get("instance_id", "0"), "session": self.session,
                  "config_revision": self.store.get("ea_native_export", {}).get("revision", 0), "enabled": int(enabled),
                  "issued_at": int(now), "valid_until": int(now+cfg["ea"]["entry_lease_seconds"]),
                  "risk_scale": self.decision["risk_scale"] if enabled else 0, "close_ticket": close_ticket}
        if enabled and cfg["ea"]["ai_controls_entries"]:
            values["valid_until"] = min(values["valid_until"], int(self.decision["expires_at"]))
        # Persist claimed ownership before any enable can reach MT5.
        if enabled:
            self.store.put("ea_native_release_required", True)
        write_packet(self.root(), "control.csv", values)
        self.last_command = values
        self.store.put("ea_native_command", values)
        return values

    def pause(self, reason="用户停止"):
        self.decision = {"enabled": False, "reason": reason, "expires_at": 0, "risk_scale": 0}
        cfg, _ = self.store.active()
        if not hasattr(self.broker, "m"):  # test doubles without a file bridge
            return
        try:
            return self._send(False, cfg, self.status())
        except Exception as exc:
            self.store.audit("ea.pause_publish_failed", {"error": type(exc).__name__, "note": "等待原生回执；不能切换执行权"})

    def require_paused(self):
        state = self.status()
        command = self.store.get("ea_native_command", {})
        if state.get("pending"):
            raise Conflict("原生 EA 有未确认交易，需按成交证据核对")
        required = self.store.get("ea_native_release_required", False)
        if state.get("connected"):
            if state["entries_enabled"] or (command and state["command_revision"] != command["revision"]):
                raise Conflict("等待原生 EA 暂停回执，不能切换执行权或参数")
            if state["positions"]:
                raise Conflict("原生 EA 正在保护持仓，不能切换执行权或参数")
            if required:
                self.store.put("ea_native_release_required", False)
        elif required:
            raise Conflict("原生 EA 曾获开仓许可；需恢复连接并确认暂停，不能仅凭超时接管")
        return state

    def export(self, cfg=None):
        with self.engine.control:
            self.require_paused()
            if self.broker.positions() or self.broker.orders() or self.store.unresolved():
                raise Conflict("参数发布需要空仓、无挂单及已核对的执行状态")
            cfg = cfg or self.store.active()[0]
            values = configuration(cfg, self.broker.status()["account"])
            write_packet(self.root(), "execution.csv", values)
            result = {"ok": True, "revision": values["revision"], "status": "published", "requires_protocol": "FUSION_EXEC1", "note": "等待 FusionExecutor 参数回执"}
            self.store.put("ea_native_export", result)
            return result

    def start(self, cfg):
        state = self.require_paused()
        if not state.get("connected") or state.get("symbol") != cfg["ea"]["strategy"]["symbol"]:
            raise ValueError("请先挂载并连接 FusionExecutor 交易 EA（FusionBridge 不执行交易）")
        self.export()  # canonical persisted envelope, not enriched live background fields
        self.armed_instance = state["instance_id"]
        self.decision = {"enabled": not cfg["ea"]["ai_controls_entries"], "reason": "等待 AI 开仓许可" if cfg["ea"]["ai_controls_entries"] else "用户启动，AI 开仓管理已关闭", "expires_at": 0 if cfg["ea"]["ai_controls_entries"] else time.time()+cfg["ea"]["decision_ttl_minutes"]*60, "risk_scale": 1}
        self._send(False, cfg, state)

    def decide(self, enabled, reason, minutes, risk_scale, generation):
        if type(enabled) is not bool or not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
            raise ValueError("开仓许可参数无效")
        cfg, _ = self.store.active()
        if type(minutes) is not int or not 1 <= minutes <= cfg["ea"]["decision_ttl_minutes"] or type(risk_scale) not in (int, float) or not 0 <= risk_scale <= 1:
            raise ValueError("许可有效期或风险系数超出边界")
        with self.engine.control:
            if generation != self.engine.generation:
                raise Conflict("已停止，本轮许可失效")
            if enabled and (not self.engine.running or not self.engine.armed or cfg["workflow"]["module"] != "ea" or cfg["workflow"]["mode"] != "demo"):
                raise Conflict("AI 只能在用户已经启动并解锁的 EA 模拟盘会话内允许开仓")
            if enabled and not cfg["ea"]["ai_controls_entries"]:
                raise Conflict("配置未允许 AI 管理开仓许可")
            self.decision = {"enabled": enabled, "reason": reason, "expires_at": time.time()+minutes*60, "risk_scale": risk_scale}
            self.store.audit("ea.entry_decision", self.decision)
            before = self.last_command
            self.tick(cfg)
            published = self.last_command is not before
            return {**self.decision, "status": "published" if published else "decision_saved", "note": "这是开仓管理决策，不是订单；未启动时只保存决定，已启动时以 EA 回执和本地风控为准"}

    def tick(self, cfg):
        state = self.status()
        active = self.engine.running and self.engine.armed and cfg["workflow"]["module"] == "ea" and cfg["workflow"]["mode"] == "demo"
        account = self.broker.status()["account"]
        if not active or self.broker.account_pin != (account.get("login"), account.get("server")) or not account.get("demo"):
            return state
        if state.get("connected") and state.get("instance_id") != self.armed_instance:
            self.engine.stop()
            self.store.audit("ea.restarted", {"note": "原生 EA 重启，当前会话已停止，需用户重新启动"})
            return state
        if self.last_command and self.last_command.get("close_ticket") and state.get("command_revision") != self.last_command["revision"] and time.time() <= self.last_command["valid_until"]:
            return state  # do not overwrite a close request before native acknowledgment
        published = self.store.get("ea_native_export", {})
        desired = configuration(self.store.active()[0], account)["revision"]
        enabled = (self.decision["enabled"] and (not cfg["ea"]["ai_controls_entries"] or time.time() < self.decision["expires_at"])
                   and state.get("connected") and not state.get("pending") and state.get("strategy_revision") == published.get("revision") == desired
                   and not self.store.unresolved())
        self._send(bool(enabled), cfg, state)
        return state

    def close(self, ticket):
        with self.engine.control:
            cfg, _ = self.store.active()
            state = self.status()
            if not state.get("connected") or state.get("pending"):
                raise Conflict("需要已连接且没有未确认交易的 FusionExecutor")
            position = next((p for p in self.broker.positions() if p["ticket"] == ticket and p["magic"] == cfg["ea"]["execution_magic"]), None)
            if not position:
                raise ValueError("不是当前原生 EA 的持仓")
            self.decision = {"enabled": False, "reason": "用户请求平仓", "expires_at": 0, "risk_scale": 0}
            command = self._send(False, cfg, state, ticket)
            self.store.audit("ea.close_requested", {"ticket": ticket, "revision": command["revision"]})
            return {"status": "queued", "executor": "MT5 EA", "command_revision": command["revision"]}
