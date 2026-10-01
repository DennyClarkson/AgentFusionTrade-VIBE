"""Root-authored orchestration: proposal -> deterministic guards -> durable intent."""
import math
import threading
import time
import copy
from datetime import datetime, timezone

from .ai import AIClient
from .broker import BrokerError, OrderNotSubmitted
from .store import Conflict, uid
from .strategy import TF_SECONDS, analyze, session_open, trailing_levels
from .planning import environment, form_plan, event_policy, graph_gate


class Engine:
    def __init__(self, store, broker, ai=None):
        self.store, self.broker, self.ai = store, broker, ai or AIClient(store)
        self.lock = threading.Lock()
        self.control = threading.RLock()
        self.wake = threading.Event()
        self.running = False
        self.busy = False
        self.armed = False
        self.generation = 0
        self.last_error = None
        self.market = None
        self.thread = None
        self.background = None
        self.ea_manager = None
        from .ea_control import EAController
        self.ea_controller = EAController(self)
        self.pipeline_lock = threading.RLock()
        self.pipeline = {"run_id":None,"status":"idle","stage":"idle","nodes":[],"packet":None}
        previous=self.store.get("last_pipeline")
        if previous and previous.get("module","ai")=="ai" and not self.store.get("last_ai_pipeline"):
            self.store.put("last_ai_pipeline",{**previous,"module":"ai"})
        self.store.audit("engine.boot", {"running": False, "armed": False, "unresolved": len(store.unresolved())})

    def configs(self):
        configs, versions = self.store.active()
        self.broker.configure(configs["broker"])
        if self.background: configs["context"] = self.background.current(configs["context"])
        if configs["workflow"].get("module")=="ea":
            configs["strategy"] = copy.deepcopy(configs["ea"]["strategy"])
            configs["workflow"].update(ai_gate="off",poll_seconds=min(5,configs["workflow"]["poll_seconds"]))
            # EA direction/entry are deterministic; its manager is a separate no-order worker.
            from .config import Logic
            configs["logic"] = Logic().model_dump()
        return configs, versions

    def pipeline_event(self, value):
        with self.pipeline_lock:
            node = next((n for n in self.pipeline["nodes"] if n["id"]==value["id"]),None)
            if not node: return
            if value["state"]=="tool_running":
                node["active_tool"] = {k:v for k,v in value.items() if k not in {"id","state"}}
            elif value["state"]=="tool_done":
                node.setdefault("tools",[]).append({k:v for k,v in value.items() if k not in {"id","state"}})
                node.pop("active_tool",None)
            elif value["state"] in {"model_config", "model_running", "model_done"}:
                node["model_activity"] = {k:v for k,v in value.items() if k!="id"}
            else:
                node.update({k:v for k,v in value.items() if k!="id"})
            self.pipeline["stage"] = value["id"]

    def pipeline_status(self, module=None):
        with self.pipeline_lock:
            if module and self.pipeline.get("module","ai") != module:
                return self.store.get("last_"+module+"_pipeline",{"run_id":None,"module":module,"status":"idle","stage":"idle","nodes":[],"packet":None})
            current=copy.deepcopy(self.pipeline)
        if not current["run_id"] and module:
            return self.store.get("last_"+module+"_pipeline",current)
        return current

    def arm(self, login):
        with self.control:
            if self.busy:
                raise Conflict("研判进行中，不能切换解锁状态")
            cfg, _ = self.configs()
            self.ea_controller.require_paused()
            s = self.broker.status()
            a, t = s["account"], s["terminal"]
            if not s["connected"] or not a.get("demo") or a.get("login") != login:
                raise ValueError("请输入当前已连接模拟账户的正确账号")
            if not a["trade_allowed"] or not t["trade_allowed"] or (cfg["workflow"]["module"] != "ea" and t["tradeapi_disabled"]):
                raise ValueError("MT5 未允许自动交易")
            if self.store.unresolved():
                raise Conflict("有未核对订单意图，请先人工核对终端及审计记录")
            self.broker.account_pin = (a["login"], a["server"])
            self.armed = True
            self.store.audit("engine.armed", {"login": login, "server": a["server"]})

    def disarm(self):
        with self.control:
            self.ea_controller.pause("解除交易解锁")
            self.generation += 1
            self.armed = False
            self.broker.account_pin = None
            self.store.audit("engine.disarmed", {})

    def start(self):
        with self.control:
            if self.running:
                return
            if self.busy or (self.thread and self.thread.is_alive()):
                raise Conflict("上一周期正在退出，请稍后启动")
            cfg, _ = self.configs()
            if cfg["workflow"]["mode"] == "demo" and not self.armed:
                raise ValueError("模拟实单模式需要先解锁当前模拟账户")
            if cfg["workflow"]["mode"] == "demo" and cfg["workflow"]["module"] == "ea":
                self.ea_controller.start(cfg)
            else:
                self.ea_controller.require_paused()
            self.running = True
            self.wake.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True, name="fusion-scheduler")
            self.thread.start()
            self.store.audit("engine.started", {"mode": cfg["workflow"]["mode"]})

    def stop(self):
        with self.control:
            self.running = False
            self.wake.set()
            self.disarm()
            if self.ea_manager: self.ea_manager.stop()
            self.store.audit("engine.stopped", {"note": "停止新单；原生 EA 继续保护已有持仓，经纪商 SL/TP 保留"})

    def _loop(self):
        while self.running:
            try:
                if self.ea_manager: self.ea_manager.maybe_review()
                self.cycle(scheduled=True)
            except Exception as exc:
                self.last_error = str(exc)
                self.store.audit("engine.loop_error", {"error": str(exc)})
            cfg, _ = self.configs()
            self.wake.wait(cfg["workflow"]["poll_seconds"])

    def mutation_allowed(self):
        if self.running or self.busy or self.armed:
            raise Conflict("请先停止并等待当前周期结束，再修改配置")
        cfg, _ = self.configs()
        self.ea_controller.require_paused()
        if self.store.unresolved():
            raise Conflict("有未核对订单意图，请先核对再修改配置")
        if self.store.get("paper_positions", []):
            raise Conflict("影子持仓仍在管理中，请先平掉影子持仓")
        connection = self.broker.status()
        ownership = self.store.get("managed_demo_accounts", {})
        a = connection["account"]
        account_key = f"{a.get('login')}|{a.get('server')}"
        if any(value for key,value in ownership.items() if key != account_key):
            raise Conflict("其他模拟账户仍有已记录敞口，需切回对应账户核对平仓")
        if not connection["connected"] and any(ownership.values()):
            raise Conflict("无法核对已知模拟盘敞口，重连并确认平仓后才能修改配置")
        if connection["connected"] and any(p["magic"] in {cfg["risk"]["magic"], cfg["ea"]["execution_magic"]} for p in self.broker.positions()):
            raise Conflict("框架仍有模拟盘持仓，平仓后再修改配置")

    def status(self):
        cfg, _ = self.configs()
        connection = self.broker.status()
        positions, error = [], None
        if connection["connected"]:
            try:
                positions = self.broker.positions()
                if self.market is None or time.time()-self.market["captured_at"] >= 5:
                    self.market = self.broker.snapshot(cfg["strategy"])
            except Exception as exc:
                error = str(exc)
        if self.armed and (connection["account"].get("login"), connection["account"].get("server")) != self.broker.account_pin:
            self.disarm()
        cycles = self.store.cycles(1)
        paper_trades = self.store.get("paper_trades", [])
        day = int(time.time()//86400)
        return {"engine": {"running": self.running, "armed": self.armed, "busy": self.busy, "mode": cfg["workflow"]["mode"], "module":cfg["workflow"].get("module","ai"), "last_error": error or self.last_error, "unresolved_orders": len(self.store.unresolved())},
                "connection": connection, "market": self.market, "positions": positions, "paper_positions": self.store.get("paper_positions", []),
                "metrics": {"today_pnl": sum(t["pnl"] for t in paper_trades if int(t["exit_time"]//86400)==day), "cycle_count": self.store.count_cycles(), "pnl_source": "shadow", "strategy_capital": cfg["risk"]["capital"]}, "latest": cycles[0] if cycles else None,
                "latest_ai":self.store.latest_cycle("ai"),"latest_ea":self.store.latest_cycle("ea")}

    def _day_pnl(self, account, positions, cfg, mode):
        now = time.time()
        midnight = int(now//86400)*86400
        if mode == "shadow":
            trades = self.store.get("paper_trades", [])
            realized = sum(t["pnl"] for t in trades if t["exit_time"] >= midnight)
            return realized + sum(x.get("profit", 0) for x in self.store.get("paper_positions", []))
        deals = self.broker.deals_since(midnight)
        realized = sum(sum(float(d.get(k, 0)) for k in ("profit", "commission", "swap", "fee")) for d in deals if d.get("type") in (0, 1))
        floating = sum(p["profit"] + p.get("swap", 0) for p in positions)
        key = f"day_equity:{account['login']}:{account['server']}:{midnight}"
        baseline = self.store.get(key)
        if baseline is None:
            baseline = account["equity"]
            self.store.put(key, baseline)
        return min(realized + floating, account["equity"] - baseline)

    def guards(self, snapshot, signal, cfg, connection=None):
        r, w, s = cfg["risk"], cfg["workflow"], cfg["strategy"]
        reasons, volume, risk_money, margin = [], 0.0, 0.0, 0.0
        now, tick, spec = time.time(), snapshot["tick"], snapshot["spec"]
        connection = connection or self.broker.status()
        account = connection["account"]
        if not connection["connected"]:
            reasons.append("MT5 未连接")
        if account.get("currency") != "USD":
            reasons.append("当前预设资金单位为 USD，账户币种不匹配")
        if w["mode"] == "demo":
            if not account.get("demo") or not self.armed or self.broker.account_pin != (account.get("login"), account.get("server")):
                reasons.append("模拟盘未解锁或账户发生变化")
            t = connection["terminal"]
            if not account.get("trade_allowed") or not t.get("trade_allowed") or t.get("tradeapi_disabled"):
                reasons.append("自动交易权限不可用")
            if self.store.unresolved():
                reasons.append("存在未核对订单，禁止新单")
        age = now - tick["time"]
        if age < -5 or age > r["max_tick_age_seconds"]:
            reasons.append("报价时间无效／过期，请核对经纪商 UTC 偏移")
        if not all(math.isfinite(tick[k]) and tick[k] > 0 for k in ("bid", "ask")) or tick["ask"] < tick["bid"]:
            reasons.append("报价异常")
        if spec["point"] <= 0 or spec["volume_step"] <= 0 or spec["trade_tick_size"] <= 0:
            reasons.append("合约规格异常")
        spread = (tick["ask"]-tick["bid"])/spec["point"] if spec["point"] > 0 else math.inf
        if spread > r["max_spread_points"]:
            reasons.append("点差超过配置上限")
        for tf, bars in snapshot["frames"].items():
            bar_age = now - bars[-1]["time"] - TF_SECONDS[tf]
            if bar_age < -5 or bar_age > TF_SECONDS[tf] * 1.5:
                reasons.append(f"{tf} 最近闭合 K 线时间异常或过期")
        if not session_open(now, w):
            reasons.append("不在配置交易时段")
        context = cfg["context"]
        if w["require_context"] and (not context["updated_at"] or not 0 <= now-context["updated_at"] <= context["max_age_hours"]*3600):
            reasons.append("背景数据缺失或过期")
        if event_policy(now,context)["blackout"]:
            reasons.append("处于高影响经济事件避让窗口")
        positions = self.broker.positions()
        orders = self.broker.orders()
        paper = self.store.get("paper_positions", []) if w["mode"] == "shadow" else []
        if len(positions) + len(orders) + len(paper) >= r["max_positions"]:
            reasons.append("账户总持仓／挂单数量达到上限")
        if any(p["symbol"] == s["symbol"] for p in positions + orders + paper):
            reasons.append("当前品种已有敞口，禁止加仓摊平或与手工单混合")
        pnl = self._day_pnl(account, positions, cfg, w["mode"])
        if pnl <= -r["capital"] * r["daily_loss_pct"] / 100:
            reasons.append("触及当日亏损上限")
        if signal["action"] == "HOLD":
            reasons.append("策略尚无入场信号")
        elif not reasons:
            entry, sl, tp = signal["entry"], signal["sl"], signal["tp"]
            side = signal["action"]
            if not all(math.isfinite(x) and x > 0 for x in (entry, sl, tp)) or not (sl < entry < tp if side == "BUY" else tp < entry < sl):
                reasons.append("止损止盈价格方向无效")
            minimum = max(spec["trade_stops_level"], spec["trade_freeze_level"]) * spec["point"]
            reference = tick["bid"] if side == "BUY" else tick["ask"]
            if (reference-sl < minimum or tp-reference < minimum) if side == "BUY" else (sl-reference < minimum or reference-tp < minimum):
                reasons.append("保护价格小于经纪商最小距离")
            if not reasons:
                # Budget includes adverse entry slippage; never round UP to the broker minimum.
                worst_entry = entry + (1 if side == "BUY" else -1) * r["max_slippage_points"] * spec["point"]
                worst_exit = sl - (1 if side == "BUY" else -1) * r["max_slippage_points"] * spec["point"]
                loss = self.broker.loss_per_lot(s["symbol"], side, worst_entry, worst_exit) + r["commission_per_lot"]
                capital = min(r["capital"], account["equity"])
                if w["mode"] == "shadow":
                    capital = min(capital, r["capital"] + sum(t["pnl"] for t in self.store.get("paper_trades", [])))
                scale = min(1,max(0,signal.get("risk_scale",1)))
                budget = max(0, capital * r["risk_per_trade_pct"] / 100 * scale)
                budget = min(budget, max(0, r["capital"] * r["daily_loss_pct"] / 100 + min(0, pnl)))
                volume = round(math.floor(min(budget/loss, spec["volume_max"])/spec["volume_step"])*spec["volume_step"], 8)
                risk_money = volume * loss
                if volume < spec["volume_min"] or risk_money > budget + 1e-8:
                    reasons.append("经纪商最小手数超过风险预算，跳过交易")
                else:
                    margin = self.broker.margin(s["symbol"], side, volume, worst_entry)
                    if margin > min(account["free_margin"], capital*r["max_margin_pct"]/100):
                        reasons.append("保证金超过策略预算或账户可用保证金")
        return {"allowed": not reasons, "reasons": reasons, "volume": volume, "risk_money": risk_money, "margin": margin, "spread_points": spread if math.isfinite(spread) else None, "today_pnl": pnl}

    def _paper_manage(self, cfg, snapshot, close_all=False):
        positions, remaining = self.store.get("paper_positions", []), []
        trades = self.store.get("paper_trades", [])
        now = time.time()
        tick = snapshot["tick"]
        if not -5 <= now-tick["time"] <= cfg["risk"]["max_tick_age_seconds"]:
            if close_all:
                raise ValueError("影子平仓需要有效的当前报价")
            return
        for p in positions:
            sign = 1 if p["action"] == "BUY" else -1
            price = tick["bid"] if sign == 1 else tick["ask"]
            profit = (price-p["entry"])*sign*p["contract_size"]*p["volume"] - p.get("commission", 0)
            hit = price <= p["sl"] or price >= p["tp"] if sign == 1 else price >= p["sl"] or price <= p["tp"]
            expired = now-p["time"] >= p["max_hold_minutes"]*60
            session_end = not session_open(now, cfg["workflow"])
            event_exit=event_policy(now,cfg["context"])["close_existing"]
            if hit or expired or close_all or session_end or event_exit:
                trades.append({**p, "exit": price, "exit_time": now, "pnl": profit, "reason": "manual" if close_all else "stop/target" if hit else "event" if event_exit else "session" if session_end else "time"})
            else:
                if cfg["strategy"].get("algorithm")=="bollinger_pullback":
                    atr=analyze(snapshot,cfg["strategy"])["atr"]
                    p["sl"],p["tp"]=trailing_levels(p,price,atr,p.get("protection_strategy",cfg["strategy"]),snapshot["spec"],cfg["risk"]["commission_per_lot"]/p["contract_size"])
                remaining.append({**p, "profit": profit})
        self.store.put_many({"paper_positions": remaining, "paper_trades": trades})

    def close_paper(self):
        if not self.lock.acquire(blocking=False):
            raise Conflict("周期进行中")
        try:
            cfg, _ = self.configs()
            self._paper_manage(cfg, self.broker.snapshot(cfg["strategy"]), close_all=True)
            self.store.audit("paper.closed", {})
        finally:
            self.lock.release()

    def close_demo(self, ticket):
        if not self.lock.acquire(blocking=False):
            raise Conflict("周期进行中")
        try:
            self.busy = True
            cfg, _ = self.configs()
            if any(p["ticket"] == ticket and p["magic"] == cfg["ea"]["execution_magic"] for p in self.broker.positions()):
                return self.ea_controller.close(ticket)
            return self._close_demo(ticket, cfg)
        finally:
            self.busy = False
            self.lock.release()

    def _close_demo(self, ticket, cfg):
        with self.control:
            return self._close_demo_locked(ticket, cfg)

    def _close_demo_locked(self, ticket, cfg):
        # A known preflight failure is not an ambiguous submission.
        request = self.broker.prepare_close(ticket, cfg["risk"])
        pin = self.broker.account_pin
        if any(x["data"].get("request", {}).get("position") == ticket and x["data"].get("account") == list(pin) for x in self.store.unresolved()):
            raise Conflict("该持仓已有未确认平仓操作，请先核对")
        key = f"close:{pin[0]}:{pin[1]}:{ticket}:{uid()}"
        intent = {"ticket": ticket, "action": "close", "request": request, "account": pin}
        self.store.intent(key, intent)
        try:
            with self.control:
                result = self.broker.send_order(request)
            ok = result["retcode"] == 10009
            uncertain = result["retcode"] in (10008, 10010, 10012)
            remaining = self.broker.positions()
            if ok and any(p["ticket"] == ticket for p in remaining):
                ok, uncertain = False, True
            ownership = self.store.get("managed_demo_accounts", {})
            ownership[f"{pin[0]}|{pin[1]}"] = [p for p in remaining if p["magic"] == cfg["risk"]["magic"]]
            self.store.resolve(key, "done" if ok else "unknown" if uncertain else "rejected", {**intent, "result": result}, {"managed_demo_accounts": ownership})
            self.store.audit("order.close", {"ticket": ticket, "result": result})
            return result
        except OrderNotSubmitted as exc:
            self.store.resolve(key,"rejected",{**intent,"reason":str(exc),"submitted":False})
            return {"status":"rejected","reason":str(exc),"submitted":False}
        except Exception:
            self.store.resolve(key, "unknown", {**intent, "note": "核对终端后处理，不能自动重试"})
            raise

    def _manage_trailing(self, snapshot, cfg):
        if self.store.unresolved(): return
        with self.control:
            if not self.armed: return
            pin=self.broker.account_pin
            plans=self.store.get("position_plans",{})
            atr=analyze(snapshot,cfg["strategy"])["atr"]
            for p in self.broker.positions():
                plan=plans.get(f"{pin[0]}|{pin[1]}|{p['ticket']}",{})
                if p["magic"]!=cfg["risk"]["magic"] or plan.get("module")!="ea": continue
                side="BUY" if p["type"]==0 else "SELL"
                price=snapshot["tick"]["bid" if side=="BUY" else "ask"]
                position={"action":side,"entry":p["price_open"],"sl":p["sl"],"initial_sl":plan["initial_sl"],"tp":p["tp"]}
                cost=cfg["risk"]["commission_per_lot"]/snapshot["spec"]["trade_contract_size"]+cfg["risk"]["max_slippage_points"]*snapshot["spec"]["point"]
                sl,tp=trailing_levels(position,price,atr,plan["strategy"],snapshot["spec"],cost)
                if sl==p["sl"]: continue
                try: request=self.broker.prepare_protection(p["ticket"],sl,tp,cfg["risk"]["magic"],cfg["risk"]["max_tick_age_seconds"])
                except BrokerError as exc:
                    self.store.audit("protection.deferred",{"ticket":p["ticket"],"reason":str(exc)})
                    continue
                checked=self.broker.check_order(request)
                if checked["retcode"]!=0: continue
                key=f"protection:{pin[0]}:{pin[1]}:{p['ticket']}:{uid()}"
                intent={"action":"protection","account":pin,"request":request,"created_at":time.time()}
                self.store.intent(key,intent)
                try:
                    result=self.broker.send_order(request)
                    status="done" if result["retcode"]==10009 else "unknown" if result["retcode"] in (10008,10010,10012) else "rejected"
                    self.store.resolve(key,status,{**intent,"result":result})
                    self.store.audit("protection.updated",{"ticket":p["ticket"],"sl":sl,"tp":tp,"status":status})
                    if status=="unknown": self.disarm();return
                except OrderNotSubmitted as exc:
                    self.store.resolve(key,"rejected",{**intent,"reason":str(exc),"submitted":False})
                    self.store.audit("protection.deferred",{"ticket":p["ticket"],"reason":str(exc)})
                except Exception:
                    self.store.resolve(key,"unknown",intent)
                    self.disarm();return

    def cycle(self, scheduled=False, expected_mode=None):
        if not self.lock.acquire(blocking=False):
            raise Conflict("已有研判周期运行中")
        with self.control:
            self.busy = True
            generation = self.generation
        cycle = {"id": uid(), "created_at": time.time(), "status": "analyzing", "agents": [], "execution": None}
        started = time.monotonic()
        try:
            cycle["module"]=self.store.active()[0]["workflow"].get("module","ai")
            if self.ea_manager: self.ea_manager.apply_pending(generation)
            cfg, versions = self.configs()
            cycle["module"] = cfg["workflow"].get("module","ai")
            if expected_mode and cfg["workflow"]["mode"] != expected_mode:
                raise ValueError("请求模式与活动配置不同；未执行交易")
            cycle["config_versions"] = versions
            if cycle["module"] == "ea" and cfg["workflow"]["mode"] == "demo":
                # Native module never calls Python entry, close or SLTP paths.
                with self.control:
                    native = self.ea_controller.tick(cfg)
                cycle.update(status="native_monitor", executor="MT5 EA", signal={"action": native.get("signal", "HOLD"), "reason": native.get("reason", "")},
                             execution=native, latency_ms=round((time.monotonic()-started)*1000))
                previous = self.store.latest_cycle("ea")
                if not previous or previous.get("signal") != cycle["signal"] or time.time()-previous["created_at"] >= 60:
                    self.store.cycle(cycle)
                self.last_error = None
                return cycle
            previous_pipeline=self.pipeline_status(cycle["module"])
            labels={"market":"行情整理","context":"背景分析","judge":"研判决策","risk":"风险审查","critic":"信号审议"}
            with self.pipeline_lock:
                nodes=[] if cfg["workflow"].get("module")=="ea" else [{"id":n["id"],"label":labels.get(n["id"],n["id"]),"depends_on":n["depends_on"],"state":"pending","thinking":n.get("thinking"),"tools":[]} for n in cfg["logic"]["nodes"]]
                self.pipeline={"run_id":cycle["id"],"module":cycle["module"],"status":"running","stage":"data","nodes":[{"id":"data","label":"MT5 · 指标","state":"running","depends_on":[]},*nodes,{"id":"gateway","label":"执行网关","state":"pending","depends_on":[nodes[-1]["id"]] if nodes else ["data"]}],"packet":None}
            snapshot = self.broker.snapshot(cfg["strategy"])
            from .indicators import packet
            snapshot["analysis_packet"] = packet(snapshot,cfg["indicators"])
            with self.pipeline_lock: self.pipeline["packet"]=snapshot["analysis_packet"]
            self.pipeline_event({"id":"data","state":"done","finished_at":time.time()})
            snapshot["risk_environment"] = environment(snapshot,cfg["strategy"],cfg["risk"],cfg["context"])
            self.market = snapshot
            self._paper_manage(cfg, snapshot)
            if cfg["workflow"]["mode"] == "demo" and self.armed:
                for p in self.broker.positions():
                    pin=self.broker.account_pin
                    plans=self.store.get("position_plans",{})
                    hold=plans.get(f"{pin[0]}|{pin[1]}|{p['ticket']}",{}).get("max_hold_minutes",cfg["strategy"]["max_hold_minutes"])
                    if p["magic"] == cfg["risk"]["magic"] and (time.time()-p["time"] >= hold*60 or not session_open(time.time(), cfg["workflow"]) or event_policy(time.time(),cfg["context"])["close_existing"]):
                        self._close_demo(p["ticket"], cfg)
                if cfg["workflow"].get("module")=="ea": self._manage_trailing(snapshot,cfg)
            signal = analyze(snapshot, cfg["strategy"])
            cycle["signal"] = signal
            cycle["risk"] = self.guards(snapshot, signal, cfg)
            cycle["market_summary"] = {"symbol": snapshot["symbol"], "tick": snapshot["tick"], "source": snapshot.get("source", "MT5")}
            connection = self.broker.status()
            account = connection["account"]
            key = f"signal:{account.get('login')}:{account.get('server')}:{cfg['workflow']['mode']}:{cfg['strategy']['symbol']}:{cfg['strategy']['timeframe']}:{signal['bar_time']}"
            if scheduled and self.store.get("last_scheduled_bar") == key:
                with self.pipeline_lock:
                    self.pipeline=previous_pipeline
                return {**cycle, "status": "unchanged"}
            if scheduled:
                self.store.put("last_scheduled_bar", key)
            if cfg["workflow"]["ai_gate"] != "off" or cfg["logic"]["topology"]!="signal_review":
                ai_rows=[r for r in self.store.configs() if r["category"]=="ai"]
                profiles={r["id"]:r["data"] for r in ai_rows}
                cycle["provider_versions"]={r["id"]:r["version"] for r in ai_rows}
                runtime={"event":self.pipeline_event,"memory_scope":"live"} if isinstance(self.ai,AIClient) else {}
                cycle["agents"] = self.ai.run(cfg["ai"], cfg["prompts"], snapshot, signal, cfg["context"], lambda: generation != self.generation,cfg["logic"],profiles,**runtime)
            cycle["latency_ms"] = round((time.monotonic()-started)*1000)
            cycle["topology"] = cfg["logic"]["topology"]
            cycle["module"] = cfg["workflow"].get("module","ai")
            self.pipeline_event({"id":"gateway","state":"running","started_at":time.time()})
            # Renew all inputs after the model latency. Do not execute an obsolete bar.
            refreshed = self.broker.snapshot(cfg["strategy"])
            if self.background: cfg["context"] = self.background.current()
            refreshed_rule = analyze(refreshed, cfg["strategy"])
            current,plan_reasons = form_plan(refreshed,refreshed_rule,cfg,cycle["agents"],time.monotonic()-started)
            cycle["risk"] = self.guards(refreshed, current, cfg)
            cycle["risk"]["reasons"].extend(plan_reasons)
            self.market = refreshed
            if any(snapshot["frames"][tf][-1]["time"] != refreshed["frames"][tf][-1]["time"] for tf in snapshot["frames"]):
                cycle["risk"]["reasons"].append("分析期间出现新闭合 K 线，等待下一周期重新研判")
            if cfg["logic"]["topology"]=="signal_review" and current["action"] != signal["action"]:
                cycle["risk"]["reasons"].append("分析期间信号变化")
            cycle["risk"]["reasons"].extend(graph_gate(cfg,cycle["agents"]))
            if generation != self.generation:
                cycle["risk"]["reasons"].append("操作已停止或解锁状态变更")
            cycle["risk"]["allowed"] = not cycle["risk"]["reasons"]
            cycle["signal"] = current
            cycle["status"] = "blocked"
            if cycle["risk"]["allowed"]:
                with self.control:
                    if generation != self.generation:
                        cycle["risk"].update(allowed=False, reasons=["已停止"])
                    elif self.store.get(key):
                        cycle["risk"].update(allowed=False, reasons=["该闭合 K 线信号已执行，禁止重复下单"])
                    elif cfg["workflow"]["mode"] == "shadow":
                        commission = cfg["risk"]["commission_per_lot"] * cycle["risk"]["volume"]
                        p = {"ticket": cycle["id"][:12], "symbol": cfg["strategy"]["symbol"], **current, "initial_sl":current["sl"],"protection_strategy":cfg["strategy"],"module":cfg["workflow"].get("module","ai"),"volume": cycle["risk"]["volume"], "time": time.time(), "contract_size": refreshed["spec"]["trade_contract_size"], "commission": commission, "profit": -commission}
                        self.store.put_many({"paper_positions": self.store.get("paper_positions", [])+[p], key: cycle["id"]})
                        cycle.update(status="paper_filled", execution={"mode": "shadow", "position": p, "note": "本地影子成交；止损只在程序运行并收到报价时评估"})
                    else:
                        cycle["execution"] = self._execute(key, current, cycle["risk"], cfg,cycle["agents"],started,{tf:rows[-1]["time"] for tf,rows in snapshot["frames"].items()})
                        cycle["status"] = cycle["execution"]["status"]
            self.store.cycle(cycle)
            self.pipeline_event({"id":"gateway","state":"done","finished_at":time.time(),"result":{"summary":current["reason"],"risk":cycle["risk"],"execution":cycle["execution"]}})
            with self.pipeline_lock: self.pipeline["status"]=cycle["status"]
            self.store.put("last_"+cycle["module"]+"_pipeline",self.pipeline_status())
            self.last_error = None
            return cycle
        except Exception as exc:
            cycle.update(status="error", error=str(exc))
            self.last_error = str(exc)
            self.store.cycle(cycle)
            self.store.audit("cycle.error", {"id": cycle["id"], "error": str(exc)})
            with self.pipeline_lock: self.pipeline.update(status="error",error=str(exc))
            if self.pipeline.get("run_id")==cycle["id"]:
                self.store.put("last_"+cycle.get("module","ai")+"_pipeline",self.pipeline_status())
            return cycle
        finally:
            self.busy = False
            self.lock.release()

    def _execute(self, key, signal, risk, cfg, agents=None, started=None, expected_frames=None):
        if cfg["workflow"].get("module") == "ea":
            raise Conflict("EA 实盘执行仅允许原生 MT5 EA，Python 不得代下单")
        self.ea_controller.require_paused()
        # A last independent read immediately before check/send limits stale execution.
        if self.background: cfg={**cfg,"context":self.background.current()}
        latest = self.broker.snapshot(cfg["strategy"])
        new_signal,plan_reasons = form_plan(latest,analyze(latest, cfg["strategy"]),cfg,agents or [],time.monotonic()-started if started else 0)
        latest_risk = self.guards(latest, new_signal, cfg)
        stale_context=expected_frames and any(latest["frames"][tf][-1]["time"]!=stamp for tf,stamp in expected_frames.items())
        if stale_context or plan_reasons or not latest_risk["allowed"] or new_signal["bar_time"] != signal["bar_time"] or new_signal["action"] != signal["action"]:
            return {"status": "blocked", "reasons": latest_risk["reasons"]+plan_reasons+["最终执行前校验未通过"]}
        r = self.broker.make_request(cfg["strategy"]["symbol"], new_signal["action"], latest_risk["volume"], new_signal["entry"], new_signal["sl"], new_signal["tp"], cfg["risk"], "fusion " + uid()[:12])
        # Price normalization must not increase budget beyond the calculated bound.
        worst = r["price"] + (1 if new_signal["action"] == "BUY" else -1)*cfg["risk"]["max_slippage_points"]*latest["spec"]["point"]
        worst_exit=r["sl"]-(1 if new_signal["action"]=="BUY" else -1)*cfg["risk"]["max_slippage_points"]*latest["spec"]["point"]
        if (self.broker.loss_per_lot(r["symbol"], new_signal["action"], worst, worst_exit)+cfg["risk"]["commission_per_lot"])*r["volume"] > latest_risk["risk_money"] + 0.01:
            return {"status": "blocked", "reasons": ["价格规格化增加了风险，需要重新计算"]}
        checked = self.broker.check_order(r)
        if checked["retcode"] != 0:
            return {"status": "rejected", "check": checked}
        if (started is not None and time.monotonic()-started>=new_signal["valid_for_seconds"]) or time.time()-latest["tick"]["time"]>cfg["risk"]["max_tick_age_seconds"]:
            return {"status":"blocked","reasons":["订单预检后计划／报价已过期，未提交"]}
        pinned_account = self.broker.account_pin
        self.store.intent(key, {"request": r, "account": pinned_account, "created_at": time.time()})
        self.store.put(key, True)
        try:
            final_context=self.background.current() if self.background else cfg["context"]
            context_expired=cfg["workflow"]["require_context"] and (not final_context["updated_at"] or not 0<=time.time()-final_context["updated_at"]<=final_context["max_age_hours"]*3600)
            policy_changed=event_policy(time.time(),final_context)["risk_scale"]<new_signal.get("risk_environment",{}).get("event_context",{}).get("risk_scale",1)
            if (started is not None and time.monotonic()-started>=new_signal["valid_for_seconds"]) or time.time()-latest["tick"]["time"]>cfg["risk"]["max_tick_age_seconds"] or event_policy(time.time(),final_context)["blackout"] or context_expired or policy_changed:
                self.store.resolve(key,"rejected",{"request":r,"account":pinned_account,"reason":"持久化后计划／报价过期或进入事件避让，未发送"})
                return {"status":"blocked","reasons":["计划／报价过期或事件避让，未发送订单"]}
            try:
                self.ea_controller.require_paused()
            except Conflict as exc:
                raise OrderNotSubmitted(str(exc)) from exc
            result = self.broker.send_order(r)
            status = "filled" if result["retcode"] == 10009 else "unknown" if result["retcode"] in (10008, 10010, 10012) else "rejected"
            self.store.audit("order.sent", {"request": r, "result": result, "status": status})
            positions = self.broker.positions()
            ownership = self.store.get("managed_demo_accounts", {})
            pin = self.broker.account_pin
            ownership[f"{pin[0]}|{pin[1]}"] = [p for p in positions if p["magic"] == cfg["risk"]["magic"]]
            plans=self.store.get("position_plans",{})
            for p in ownership[f"{pin[0]}|{pin[1]}"]:
                plans[f"{pin[0]}|{pin[1]}|{p['ticket']}"]={"max_hold_minutes":new_signal["max_hold_minutes"],"topology":cfg["logic"]["topology"],"module":cfg["workflow"].get("module","ai"),"initial_sl":r["sl"],"strategy":cfg["strategy"]}
            if status == "filled" and not any(p["magic"] == cfg["risk"]["magic"] and p["symbol"] == r["symbol"] for p in positions):
                status = "unknown"
            self.store.resolve(key, "done" if status == "filled" else status, {"request": r, "result": result, "account": pin}, {"managed_demo_accounts": ownership,"position_plans":plans})
            return {"status": status, "mode": "demo", "request": r, "result": result}
        except OrderNotSubmitted as exc:
            self.store.resolve(key,"rejected",{"request":r,"account":pinned_account,"reason":str(exc),"submitted":False})
            return {"status":"rejected","mode":"demo","reason":str(exc),"submitted":False}
        except Exception as exc:
            self.store.resolve(key, "unknown", {"request": r, "account": pinned_account, "error": str(exc)})
            self.disarm()
            return {"status": "unknown", "mode": "demo", "error": "成交状态不确定，已锁定新单，请核对终端"}

    def reconcile(self):
        """Resolve only positive broker evidence; absence of evidence never causes resubmit."""
        if not self.lock.acquire(blocking=False):
            raise Conflict("周期进行中，稍后核对")
        try:
            with self.control:
                cfg, _ = self.configs()
                status = self.broker.status()
                if not status["connected"]:
                    raise ValueError("需要连接原始模拟账户才能核对")
                account = status["account"]
                pin = [account["login"],account["server"]]
                positions = self.broker.positions()
                results = []
                ownership = self.store.get("managed_demo_accounts", {})
                ownership[f"{pin[0]}|{pin[1]}"] = [p for p in positions if p["magic"] == cfg["risk"]["magic"]]
                for row in self.store.unresolved():
                    data = row["data"]
                    if list(data.get("account") or []) != pin:
                        results.append({"id":row["id"],"resolved":False,"reason":"需要原始账户／服务器"})
                        continue
                    request = data.get("request",{})
                    deals = self.broker.deals_since(data.get("created_at",row["updated_at"])-3600)
                    matching = [d for d in deals if d.get("magic")==request.get("magic") and d.get("symbol")==request.get("symbol")]
                    if data.get("action")=="protection":
                        p=next((p for p in positions if p["ticket"]==request["position"] and p["magic"]==request["magic"]),None)
                        evidence=[d for d in matching if d.get("position_id")==request["position"] and d.get("entry") in (1,3)]
                        found=bool(evidence) if not p else abs(p["sl"]-request["sl"])<1e-7 and abs(p["tp"]-request["tp"])<1e-7
                    elif request.get("position"):
                        evidence = [d for d in matching if d.get("position_id")==request["position"] and d.get("entry") in (1,3)]
                        found = bool(evidence) and not any(p["ticket"]==request["position"] for p in positions)
                    else:
                        evidence = [d for d in matching if d.get("comment")==request.get("comment") or d.get("ticket")==data.get("result",{}).get("deal")]
                        found = bool(evidence) or any(p["magic"]==request.get("magic") and p["symbol"]==request.get("symbol") and p.get("comment")==request.get("comment") for p in positions)
                    if found:
                        self.store.resolve(row["id"],"done",{**data,"reconciled_at":time.time(),"evidence_deals":[d["ticket"] for d in evidence]}, {"managed_demo_accounts":ownership})
                    results.append({"id":row["id"],"resolved":found,"reason":"已匹配成交／持仓证据" if found else "证据不足，保留锁定；不会自动重试"})
                self.store.put("managed_demo_accounts",ownership)
                self.store.audit("orders.reconciled",results)
                return {"results":results,"unresolved":len(self.store.unresolved())}
        finally:
            self.lock.release()

    def backtest(self):
        cfg, versions = self.configs()
        snap = self.broker.snapshot(cfg["strategy"])
        status = self.broker.status()
        if status["account"].get("currency") != "USD" or not cfg["strategy"]["symbol"].startswith("XAUUSD"):
            raise ValueError("当前线性回测仅支持 XAUUSD / USD 账户预设")
        from .research import replay
        snap["spec"]["fallback_spread_points"]=(snap["tick"]["ask"]-snap["tick"]["bid"])/snap["spec"]["point"]
        snap["spec"]["reference_price"]=snap["tick"]["ask"]
        snap["spec"]["margin_per_lot"]=self.broker.margin(snap["symbol"],"BUY",1,snap["tick"]["ask"])
        result = replay(snap["frames"][cfg["strategy"]["timeframe"]],snap["frames"],cfg["strategy"],cfg["risk"],cfg["workflow"],snap["spec"])
        result.update(symbol=cfg["strategy"]["symbol"],timeframe=cfg["strategy"]["timeframe"],created_at=time.time(),assumptions=["确定性基线，使用相同因果信号及动态时段/波动风控；不代表活动 AI 链路历史业绩。", "下一根开盘成交；使用入场前一根点差与当前底线、双向滑点与往返佣金。", "历史事件数据未提供，不做新闻过滤；回撤为 K 线收盘权益回撤。"])
        result["config_versions"] = versions
        self.store.put("last_backtest", result)
        self.store.audit("backtest.completed", result["metrics"])
        return result
