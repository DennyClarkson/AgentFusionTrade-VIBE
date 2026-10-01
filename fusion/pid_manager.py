"""Optional, independently running AI manager for FusionPIDTrader.

No broker SDK, order capability or terminal restart. The EA remains the sole
execution owner. Files are account/server/symbol/magic scoped by native telemetry.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fusion.ai import AIClient, function
from fusion.config import AI


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool
    reason: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, float] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def allowed_parameters(cls, values):
        if set(values)-{"kp", "ki", "kd", "upper", "lower", "stop_atr", "activation"}:
            raise ValueError("unknown parameter")
        return values


class ManagerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ai: AI = Field(default_factory=lambda: AI(thinking=True, output_token_policy="provider_default"))
    review_interval_seconds: int = Field(default=120, ge=30, le=1800)
    permission_seconds: int = Field(default=240, ge=30, le=300)
    auto_apply_parameters: bool = False
    prompt_file: str = "pid-manager-prompt.txt"
    history_pairs: int = Field(default=10, ge=1, le=40)


DEFAULT_PROMPT = """你是 MT5 原生 FusionPIDTrader 的后台管理 Agent。EA 自主逐 Tick 执行，
你只能评估未来开仓许可、复盘每一笔确认的退出成交，以及建议有限参数。你不能下单、启动 EA、
提高资金风险或更改已有持仓保护。使用可用工具核实行情与历史，保留对话上下文。
策略是顺势回撤（BOLL 或原始 Stoch 极值 + Tick 转向），震荡时按最近几天的位置选择方向。
浮盈达到阈值后启动可双向移动的 PID 通道，急变穿越旧线触发市价平仓。经纪商保护 SL 独立，
目标盈利金额不等于已实现或保证收益。数据不含完整新闻日历，不可虚构新闻或实测性能。
每个退出成交都要复盘，但不能因为单笔盈亏而强制改参。parameters 仅允许 kp/ki/kd/upper/
lower/stop_atr/activation；没有充分证据则保持空对象。自动改参上限由 EA 再独立检查。
最终只输出 JSON：enabled(bool), reason(中文结论及参数理由), parameters(object)。
enabled 只代表短期未来入场许可；停止、超时和风险闸门始终由 EA 决定。"""


def packet_read(path: Path) -> dict[str, str]:
    rows = [line.split(";") for line in path.read_text(encoding="utf-8-sig").splitlines()]
    if not rows or any(len(row) != 2 for row in rows):
        raise ValueError("incomplete native packet")
    if rows[-1] != ["END", str(len(rows)-1)]:
        raise ValueError("incomplete native packet")
    data = dict(rows[:-1])
    if len(data) != len(rows)-1:
        raise ValueError("duplicate native key")
    return data


def packet_write(path: Path, values: dict) -> None:
    # MQL FILE_CSV uses delimiter reads, not RFC-4180 quoted multiline fields.
    clean = {str(k): str(v).replace(";", "，").replace("\r", " ").replace("\n", " ") for k, v in values.items()}
    text = "".join(f"{k};{v}\n" for k, v in clean.items()) + f"END;{len(clean)}\n"
    temp = path.with_suffix(path.suffix+".python.tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def fresh(data, now=None):
    now = time.time() if now is None else now
    try:
        required = {"boot", "session", "login", "server", "symbol", "magic", "position", "armed", "pending", "time", "kp", "ki", "kd", "upper", "lower", "stop_atr", "activation", "allow_ai_parameters"}
        if not required <= data.keys() or any(not isinstance(v, str) or len(v)>4096 for v in data.values()):
            return False
        for key in ("boot", "session", "server", "symbol"):
            if not data[key]:
                return False
        for key in ("login", "magic"):
            if int(data[key])<=0:
                return False
        if any(data[key] not in {"0", "1"} for key in ("armed", "allow_ai_parameters")) or int(data["pending"]) not in range(4) or int(data["position"])<0:
            return False
        if not all(math.isfinite(float(data[k])) for k in ("kp", "ki", "kd", "upper", "lower", "stop_atr", "activation")):
            return False
        return data["protocol"] == "FUSION_PID1" and -2 <= now-int(data["time"]) <= 10
    except (KeyError, ValueError, TypeError):
        return False


def safe_facts(data):
    names = {"time", "symbol", "armed", "pending", "note", "bid", "ask", "atr", "trend_fast", "trend_slow", "boll_upper", "boll_lower", "active", "centre", "upper", "lower", "kp", "ki", "kd", "stop_atr", "activation", "require_ai", "allow_ai_parameters"}
    return {k: v for k, v in data.items() if k in names}


class PIDManager:
    def __init__(self, directory: Path, config: ManagerConfig, prompt=DEFAULT_PROMPT, client=None):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = (directory/"manager.lock").open("a+b")
        self.lock.seek(0, 2)
        if self.lock.tell() == 0:
            self.lock.write(b"0"); self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("PID AI manager is already running") from None
        self.config = config
        self.prompt = prompt
        self.client = client or AIClient()
        self.db = sqlite3.connect(directory/"pid-manager.sqlite3")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, scope TEXT, kind TEXT,
              payload TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
              retry REAL DEFAULT 0, result TEXT);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, scope TEXT, role TEXT, content TEXT);
            CREATE TABLE IF NOT EXISTS schedule (scope TEXT PRIMARY KEY, next_at REAL);
            UPDATE jobs SET state='pending' WHERE state='running';
        """)
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock.close()

    def discover(self, scope, data):
        for kind, pattern in (("exit", "deal_*.csv"), ("chat", "ask_*.csv")):
            for file in sorted(self.directory.glob(scope+pattern)):
                try:
                    payload = packet_read(file)
                    if kind == "chat" and payload.get("boot") != data["boot"]:
                        continue
                    self.db.execute("INSERT OR IGNORE INTO jobs(id,scope,kind,payload) VALUES(?,?,?,?)", (file.name, scope, kind, json.dumps(payload, ensure_ascii=False)))
                except (OSError, ValueError):
                    continue
        scheduled = self.db.execute("SELECT next_at FROM schedule WHERE scope=?", (scope,)).fetchone()
        if data.get("armed") == "1" and (not scheduled or scheduled[0] <= time.time()):
            self.db.execute("INSERT OR IGNORE INTO jobs(id,scope,kind,payload) VALUES(?,?,?,?)", (scope+"timer_"+str(int(time.time())), scope, "review", json.dumps({"boot": data["boot"], "session": data["session"]})))
            self.db.execute("INSERT OR REPLACE INTO schedule VALUES(?,?)", (scope, time.time()+self.config.review_interval_seconds))
        self.db.commit()

    def report(self, scope, data, text):
        try:
            packet_write(self.directory/(scope+"report.csv"), {"boot": data["boot"], "time": int(time.time()), "text": text})
        except OSError:
            pass  # Display availability cannot change queue completion or repeat a model call.

    def control(self, data, initial, decision):
        if not fresh(data) or any(data[k] != initial[k] for k in ("boot", "session", "login", "server", "symbol", "magic")):
            return None
        now = int(time.time())
        until = min(now+self.config.permission_seconds, int(initial["time"])+self.config.permission_seconds)
        if until <= now:
            return None
        values = {k: data[k] for k in ("boot", "session", "login", "server", "symbol", "magic")}
        parameters = decision.parameters
        allowed = {"kp", "ki", "kd", "upper", "lower", "stop_atr", "activation"}
        if set(parameters)-allowed:
            raise ValueError("unknown parameter")
        apply = bool(parameters) and self.config.auto_apply_parameters and data.get("allow_ai_parameters") == "1" and data.get("position") == "0" and data.get("pending") == "0"
        values.update(revision=time.time_ns()//1000, issued=now, until=until,
                      enabled=int(decision.enabled and data.get("armed") == "1"), reason=decision.reason,
                      apply=int(apply))
        values.update({key: parameters.get(key, data[key]) for key in allowed})
        return values

    def finish_prepared(self, scope, telemetry, job, outcome):
        data = packet_read(telemetry)
        if "control" not in outcome:
            outcome["control"] = self.control(data, outcome["initial"], Decision.model_validate(outcome["decision"]))
            # The validated model reply is already durable. Freeze publication
            # identity/revision/expiry before the first publication attempt.
            with self.db:
                self.db.execute("UPDATE jobs SET result=? WHERE id=? AND state='prepared'", (json.dumps(outcome, ensure_ascii=False), job))
        control = outcome.get("control")
        published = bool(control) and fresh(data) and int(control["until"])>time.time() and all(data[k] == str(control[k]) for k in ("boot", "session", "login", "server", "symbol", "magic"))
        if published:
            packet_write(self.directory/(scope+"control.csv"), control)
        outcome["published"] = published
        with self.db:
            self.db.execute("UPDATE jobs SET state='done',result=? WHERE id=?", (json.dumps(outcome, ensure_ascii=False), job))
        self.report(scope, data, outcome["decision"]["reason"]+(" | 许可已发布，参数需 EA 确认" if published else " | 状态已变，本次未发布"))

    def run_one(self, telemetry: Path):
        data = packet_read(telemetry)
        if not fresh(data):
            return False
        scope = telemetry.name.removesuffix("telemetry.csv")
        prepared = self.db.execute("SELECT id,result FROM jobs WHERE scope=? AND state='prepared' ORDER BY rowid LIMIT 1", (scope,)).fetchone()
        if prepared:
            self.finish_prepared(scope, telemetry, prepared[0], json.loads(prepared[1]))
            return True
        self.discover(scope, data)
        row = self.db.execute("SELECT id,kind,payload,attempts FROM jobs WHERE scope=? AND state='pending' AND retry<=? ORDER BY CASE kind WHEN 'exit' THEN 0 WHEN 'chat' THEN 1 ELSE 2 END,rowid LIMIT 1", (scope, time.time())).fetchone()
        if not row:
            return False
        job, kind, payload, attempts = row
        event = json.loads(payload)
        if kind != "exit" and (event.get("boot") != data["boot"] or event.get("session") != data["session"]):
            self.db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,)); self.db.commit()
            return True
        self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,)); self.db.commit()
        self.report(scope, data, f"AI 正在{ '逐笔成交复盘' if kind == 'exit' else '分析' } · {self.config.ai.model} · 思考模式 {'开' if self.config.ai.thinking else '关'}")
        try:
            past = self.db.execute("SELECT role,content FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, self.config.history_pairs*2)).fetchall()[::-1]
            messages = [{"role": role, "content": content, **({"reasoning_content": ""} if role == "assistant" else {})} for role, content in past]
            request = json.dumps({"task": kind, "event_or_question": json.loads(payload), "market": safe_facts(data)}, ensure_ascii=False)
            messages.append({"role": "user", "content": request})
            def dispatch(name, args):
                if args:
                    raise ValueError("tool has no arguments")
                if name == "get_market":
                    current = packet_read(telemetry)
                    return {"fresh": fresh(current), "data": safe_facts(current)}
                if name == "get_exits":
                    recent = self.db.execute("SELECT payload FROM jobs WHERE scope=? AND kind='exit' ORDER BY rowid DESC LIMIT 20", (scope,)).fetchall()
                    return [json.loads(row[0]) for row in recent]
                raise ValueError("unknown tool")
            result = self.client.complete(self.config.ai.model_dump(), self.prompt,
                messages, tools=[function("get_market", "读取最新原生 EA 市场与参数"), function("get_exits", "读取最近已确认退出成交；不可视为完整回测")],
                dispatch=dispatch, json_output=True, max_seconds=120)
            if result["status"] != "ok":
                raise ValueError(result.get("error", "AI unavailable"))
            decision = Decision.model_validate_json(result["content"])
            public = {k: v for k, v in result.items() if not k.startswith("_")}
            public["decision"] = decision.model_dump()
            public["initial"] = data
            # No telemetry/file reads between validation and durable model outcome.
            # Later read/share/publication failures retry without another model call.
            with self.db:
                self.db.execute("UPDATE jobs SET state='prepared',result=? WHERE id=?", (json.dumps(public, ensure_ascii=False), job))
                self.db.executemany("INSERT INTO messages(scope,role,content) VALUES(?,?,?)", [(scope, "user", request), (scope, "assistant", result["content"])])
        except Exception as exc:
            detail = str(exc)[:250] if isinstance(exc, ValueError) else type(exc).__name__
            with self.db:
                self.db.execute("UPDATE jobs SET state='pending',attempts=attempts+1,retry=?,result=? WHERE id=? AND state='running'", (time.time()+min(300, 15*2**min(attempts, 5)), json.dumps({"error": detail}), job))
            self.report(scope, data, "AI 失败，稍后重试："+detail)
            # No successful permission renewal; the last bounded lease naturally expires.
            return True
        self.finish_prepared(scope, telemetry, job, public)
        return True

    def tick(self):
        for path in sorted(self.directory.glob("*telemetry.csv")):
            try:
                if self.run_one(path):
                    return True
            except (OSError, ValueError):
                continue
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("data/pid-manager.json"))
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("APPDATA", "."))/"MetaQuotes/Terminal/Common/Files/AgentTradeFusion/PID")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.config.exists():
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(ManagerConfig().model_dump_json(indent=2), encoding="utf-8")
    config = ManagerConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    prompt_file = args.config.parent/config.prompt_file
    if not prompt_file.exists():
        prompt_file.write_text(DEFAULT_PROMPT, encoding="utf-8")
    manager = PIDManager(args.directory, config, prompt_file.read_text(encoding="utf-8"))
    print("FusionPID AI manager running; native EA remains the sole order executor.")
    try:
        while True:
            manager.tick()
            if args.once:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()


if __name__ == "__main__":
    main()
