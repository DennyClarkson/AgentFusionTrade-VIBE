"""Bounded real-model pipeline comparison and minute-resolution scenario replay."""
import bisect
import copy
import math
import time

from .config import Logic
from .planning import environment, form_plan, graph_gate
from .strategy import analyze, TF_SECONDS, session_open
from .store import Conflict


def logic_presets():
    committee={"topology":"market_committee","nodes":[{"id":"market","prompt_key":"market"},{"id":"context","prompt_key":"context"},{"id":"bull","prompt_key":"bull","depends_on":["market","context"]},{"id":"bear","prompt_key":"bear","depends_on":["market","context"]},{"id":"judge","prompt_key":"judge","depends_on":["bull","bear","context"]},{"id":"risk","prompt_key":"risk_ai","depends_on":["judge","market","context"]}],"decision_node":"judge","risk_node":"risk"}
    advisor={"topology":"ea_parameter_advisor","nodes":[{"id":"market","prompt_key":"market"},{"id":"tuner","prompt_key":"tuner","depends_on":["market"]}],"decision_node":"tuner"}
    return {"signal-review":Logic().model_dump(),"committee":Logic.model_validate(committee).model_dump(),"ea-advisor":Logic.model_validate(advisor).model_dump()}


class AgentLab:
    def __init__(self,engine): self.engine=engine

    def _begin(self):
        with self.engine.control:
            self.engine.mutation_allowed()
            if not self.engine.lock.acquire(blocking=False): raise Conflict("已有任务运行中")
            self.engine.busy=True
            return self.engine.generation

    def _end(self):
        self.engine.busy=False
        self.engine.lock.release()

    def _analyze(self,snap,cfg,profiles,generation):
        signal=analyze(snap,cfg["strategy"])
        snap["risk_environment"]=environment(snap,cfg["strategy"],cfg["risk"],cfg["context"])
        started=time.monotonic()
        from .ai import AIClient
        runtime={"memory_scope":"experiment"} if isinstance(self.engine.ai,AIClient) else {}
        agents=self.engine.ai.run(cfg["ai"],cfg["prompts"],snap,signal,cfg["context"],lambda:generation!=self.engine.generation,cfg["logic"],profiles,**runtime)
        elapsed=time.monotonic()-started
        plan,reasons=form_plan(snap,signal,cfg,agents,elapsed)
        reasons.extend(graph_gate(cfg,agents))
        if generation!=self.engine.generation: reasons.append("已停止实验")
        return {"topology":cfg["logic"]["topology"],"as_of":snap["captured_at"],"latency_ms":round(elapsed*1000),"signal":plan,"risk_reasons":reasons,"agents":agents}

    def compare(self):
        generation=self._begin()
        try:
            cfg,versions=self.engine.configs()
            profiles={r["id"]:r["data"] for r in self.engine.store.configs() if r["category"]=="ai"}
            snapshot=self.engine.broker.snapshot(cfg["strategy"])
            results=[]
            for name,logic in logic_presets().items():
                if generation!=self.engine.generation: break
                trial={**cfg,"logic":logic}
                result=self._analyze(copy.deepcopy(snapshot),trial,profiles,generation)
                result["profile"]=name
                # Validate EA suggestions as a NEW profile proposal, never apply them.
                if logic["topology"]=="ea_parameter_advisor":
                    from .config import Strategy
                    patch=(result["agents"][-1].get("strategy_patch") or {}) if result["agents"] else {}
                    allowed={"fast_ema","slow_ema","breakout_bars","stop_atr","reward_risk"}
                    try:
                        if not patch or set(patch)-allowed: raise ValueError("未提供受支持参数")
                        result["validated_parameter_proposal"]=Strategy.model_validate({**cfg["strategy"],**patch}).model_dump()
                    except ValueError: result["risk_reasons"].append("EA 参数建议未通过策略配置验证")
                results.append(result)
            report={"kind":"compare","created_at":time.time(),"config_versions":versions,"summary":"三条链路对同一个真实行情快照调用模型；全部仅观察，未下单或自动修改参数。", "results":results}
            self.engine.store.put("agent_lab_compare",report)
            self.engine.store.audit("agents.compared",{"count":len(results),"latencies":[r["latency_ms"] for r in results]})
            return report
        finally:self._end()

    def replay(self):
        generation=self._begin()
        try:
            cfg,versions=self.engine.configs()
            cfg=copy.deepcopy(cfg)
            cfg["logic"]=logic_presets()["committee"] | {"historical_samples":cfg["logic"]["historical_samples"]}
            primary=self.engine.broker.snapshot(cfg["strategy"])
            minutes=self.engine.broker.snapshot({**cfg["strategy"],"timeframe":"M1","context_timeframes":[],"bars":min(10000,cfg["strategy"]["bars"]*5+200)})["frames"]["M1"]
            bars=primary["frames"][cfg["strategy"]["timeframe"]]
            count=cfg["logic"]["historical_samples"]
            warmup=max(cfg["strategy"]["slow_ema"]*3,150)
            if len(bars)<warmup+count*3: raise ValueError("历史数据不足")
            indices=[int(warmup+(len(bars)-warmup-35)*n/(count-1)) for n in range(count)]
            profiles={r["id"]:r["data"] for r in self.engine.store.configs() if r["category"]=="ai"}
            results=[]
            # Never pass today's news into a historical scenario.
            cfg["context"]={**cfg["context"],"events":[],"news":[],"updated_at":0}
            for index in indices:
                if generation!=self.engine.generation: break
                cutoff=bars[index]["time"]+TF_SECONDS[cfg["strategy"]["timeframe"]]
                frames={tf:[b for b in rows if b["time"]+TF_SECONDS[tf]<=cutoff] for tf,rows in primary["frames"].items()}
                snap={**primary,"captured_at":cutoff,"frames":frames,"tick":{"time":cutoff,"bid":bars[index]["close"],"ask":bars[index]["close"]+primary["spec"]["point"]*max(bars[index].get("spread",0),1)}}
                result=self._analyze(snap,cfg,profiles,generation)
                result["simulation"]=simulate_plan(result,minutes,cfg,primary["spec"])
                results.append(result)
            report={"kind":"replay","created_at":time.time(),"config_versions":versions,"summary":"真实 AI 调用的稀疏历史场景回放，非连续组合回测、非盈利验证。缺少历史新闻明确告知模型；计入真实推理耗时后，取下一根 M1 开盘作为成交代理，若超过 M5 信号有效期则弃单。模型训练知识可能包含事后信息。", "results":results}
            self.engine.store.put("agent_lab_replay",report)
            self.engine.store.audit("agents.replayed",{"samples":len(results),"orders_sent":0})
            return report
        finally:self._end()


def simulate_plan(result,minutes,cfg,spec):
    plan=result["signal"]
    if result["risk_reasons"] or plan["action"]=="HOLD": return {"status":"skipped","reason":"AI 等待／风控未批准","pnl":0}
    times=[b["time"] for b in minutes]
    ready=result["as_of"]+result["latency_ms"]/1000
    index=bisect.bisect_left(times,ready)
    ttl=min(TF_SECONDS[cfg["strategy"]["timeframe"]],cfg["logic"]["max_plan_age_seconds"],plan["valid_for_seconds"])
    if index>=len(minutes) or times[index]-result["as_of"]>=ttl:
        return {"status":"expired","reason":"推理耗时／分钟数据粒度导致计划过期","pnl":0}
    r=cfg["risk"];sign=1 if plan["action"]=="BUY" else -1
    b=minutes[index]
    if not session_open(b["time"],cfg["workflow"]):return {"status":"skipped","reason":"交易时段外","pnl":0}
    # Spread known at decision time; the future minute's spread is not an entry filter.
    spread=(minutes[index-1] if index else b).get("spread",0)*spec["point"];slip=r["max_slippage_points"]*spec["point"]
    if spread/spec["point"]>r["max_spread_points"]:return {"status":"skipped","reason":"历史点差超过上限","pnl":0}
    nominal=b["open"]+(spread if sign==1 else 0);entry=nominal+sign*slip
    stop_distance=abs(plan["entry"]-plan["sl"]);target_distance=abs(plan["entry"]-plan["tp"])
    sl=nominal-sign*stop_distance;tp=nominal+sign*target_distance
    per_lot=(stop_distance+2*slip)*spec["trade_contract_size"]+r["commission_per_lot"]
    budget=r["capital"]*r["risk_per_trade_pct"]/100*plan["risk_scale"]
    volume=math.floor(budget/per_lot/spec["volume_step"])*spec["volume_step"]
    if volume<spec["volume_min"]:return {"status":"skipped","reason":"最小手数超过场景风险预算","pnl":0}
    volume=min(volume,spec["volume_max"])
    price=entry;reason="end_of_data";exit_time=b["time"]
    for row in minutes[index:]:
        offset=row.get("spread",0)*spec["point"] if sign==-1 else 0
        high,low,opening=row["high"]+offset,row["low"]+offset,row["open"]+offset
        stop=low<=sl if sign==1 else high>=sl;target=high>=tp if sign==1 else low<=tp
        exit_time=row["time"]
        if not session_open(row["time"],cfg["workflow"]) or row["time"]-b["time"]>=plan["max_hold_minutes"]*60: price=opening;reason="session/time";break
        if stop:price=min(opening,sl) if sign==1 else max(opening,sl);reason="stop";break
        if target:price=tp;reason="target";break
        price=row["close"]+offset
    price-=sign*slip
    pnl=(price-entry)*sign*spec["trade_contract_size"]*volume-r["commission_per_lot"]*volume
    return {"status":"simulated","reason":reason,"pnl":round(pnl,3),"volume":volume,"entry":entry,"exit":price,"entry_time":b["time"],"exit_time":exit_time,"note":"每个场景独立用起始资金；不汇总为连续资金曲线。"}
