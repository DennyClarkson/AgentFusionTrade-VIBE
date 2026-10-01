"""Chronological, cost-aware XAUUSD research. Root-authored; no execution capability."""
import bisect
import gzip
import hashlib
import itertools
import json
import math
import threading
import time
import statistics
from datetime import datetime, timezone
from pathlib import Path

from .store import Conflict
from .strategy import TF_SECONDS, feature_series, rule_action, valid_bars, session_open, trend_direction, trailing_levels
from .planning import adaptive_policy


def save_research_report(artifact_dir, result):
    """Preserve each run, including failed and superseded experiments."""
    root=Path(artifact_dir); root.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).resolve().parent
    result["source_sha256"]={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in ("research.py","strategy.py","planning.py","config.py")}
    result["run_id"]=f"{time.time_ns()}-{result['dataset']['sha256'][:10]}"
    directory=root/"research-runs"/result["run_id"]
    directory.mkdir(parents=True,exist_ok=False)
    content=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    (directory/"report.json").write_text(content,encoding="utf-8")
    (root/"gold-research.json").write_text(content,encoding="utf-8")
    return result


def replay(bars, context_frames, strategy, risk, workflow, spec, start=0, end=None, cost_multiplier=1.0):
    """Simulate next-open market entries and broker protective exits from bid OHLC."""
    end = len(bars) if end is None else min(end, len(bars))
    warmup = max(strategy["slow_ema"]*3, strategy["breakout_bars"]+2, strategy["atr_period"]+2)
    valid_bars(bars, warmup+2)
    features = feature_series(bars, strategy)
    contexts = {tf: (feature_series(rows, strategy), [r["time"]+TF_SECONDS[tf] for r in rows]) for tf, rows in context_frames.items() if tf in strategy["context_timeframes"]}
    capital, equity = risk["capital"], risk["capital"]
    position, trades, curve = None, [], []
    peak, drawdown, day, day_equity = capital, 0.0, None, capital
    signals, small_lot_skips, margin_skips = 0, 0, 0
    point, contract, step = spec["point"], spec["trade_contract_size"], spec["volume_step"]
    slip = risk["max_slippage_points"]*point*cost_multiplier
    commission = risk["commission_per_lot"]*cost_multiplier
    margin_per_lot = spec.get("margin_per_lot", 0)
    def within_session(timestamp):
        return session_open(timestamp, workflow)

    for i in range(max(warmup, start), end):
        bar = bars[i]
        current_day = int(bar["time"]//86400)
        if current_day != day:
            day, day_equity = current_day, equity
        spread = max(spec.get("fallback_spread_points", 0), bar.get("spread", 0))*point*cost_multiplier
        entry_spread=max(spec.get("fallback_spread_points",0),bars[i-1].get("spread",0))*point*cost_multiplier
        signal_age = bar["time"]-bars[i-1]["time"]-TF_SECONDS[strategy["timeframe"]]
        if position is None and equity > 0 and within_session(bar["time"]) and -5 <= signal_age <= TF_SECONDS[strategy["timeframe"]]*1.5:
            directions, context_ok = [], True
            cutoff = bars[i-1]["time"]+TF_SECONDS[strategy["timeframe"]]
            for tf in strategy["context_timeframes"]:
                if tf not in contexts:
                    context_ok = False; break
                htf_features, completion_times = contexts[tf]
                idx = bisect.bisect_right(completion_times, cutoff)-1
                if idx < strategy["slow_ema"]*3-1 or bar["time"]-completion_times[idx] > TF_SECONDS[tf]*1.5:
                    context_ok = False; break
                htf = htf_features[idx]
                directions.append(trend_direction(htf,strategy,strict=False) if strategy.get("algorithm")=="bollinger_pullback" else "UP" if htf["fast_ema"]>htf["slow_ema"] else "DOWN" if htf["fast_ema"]<htf["slow_ema"] else "FLAT")
            signal = rule_action(features[i-1], directions, strategy) if context_ok else "HOLD"
            if signal != "HOLD":
                signals += 1
                sign = 1 if signal == "BUY" else -1
                nominal = bar["open"] + (entry_spread if sign == 1 else 0)
                entry = nominal + sign*slip
                baseline=statistics.median(x["atr"] for x in features[max(1,i-100):i-1])
                policy=adaptive_policy(bar["time"],risk,features[i-1]["atr"],baseline)
                distance = features[i-1]["atr"]*strategy["stop_atr"]*policy["stop_scale"]
                tick = spec["trade_tick_size"]
                sl = round(round((nominal-sign*distance)/tick)*tick, spec["digits"])
                tp = round(round((nominal+sign*distance*strategy["reward_risk"]*policy["target_scale"])/tick)*tick, spec["digits"])
                per_lot = (abs(entry-sl)+slip)*contract+commission
                remaining_daily = max(0, capital*risk["daily_loss_pct"]/100+min(0,equity-day_equity))
                budget = min(max(0,min(equity,capital)*risk["risk_per_trade_pct"]/100*policy["risk_scale"]),remaining_daily)
                volume = round(math.floor(min(budget/per_lot if per_lot>0 else 0,spec["volume_max"])/step)*step,8)
                stop_distance = max(spec.get("trade_stops_level",0),spec.get("trade_freeze_level",0))*point
                geometry = sl < entry < tp if sign==1 else tp < entry < sl
                if entry_spread/point > risk["max_spread_points"] or not geometry or distance-entry_spread < stop_distance:
                    continue
                if volume < spec["volume_min"]:
                    small_lot_skips += 1
                elif margin_per_lot*volume*entry/spec.get("reference_price",entry) > min(equity,capital)*risk["max_margin_pct"]/100:
                    margin_skips += 1
                else:
                    position = {"entry_time":bar["time"],"action":signal,"sign":sign,"entry":entry,"sl":sl,"initial_sl":sl,"tp":tp,"volume":volume,"risk_money":per_lot*volume}
        if position:
            p = position
            ask_offset = spread if p["sign"] == -1 else 0
            high, low, opening = bar["high"]+ask_offset,bar["low"]+ask_offset,bar["open"]+ask_offset
            stop_hit = low<=p["sl"] if p["sign"]==1 else high>=p["sl"]
            target_hit = high>=p["tp"] if p["sign"]==1 else low<=p["tp"]
            expired = bar["time"]-p["entry_time"] >= strategy["max_hold_minutes"]*60
            price, reason = None, None
            if not within_session(bar["time"]) or expired:
                price, reason = opening,"session" if not within_session(bar["time"]) else "time"
            elif stop_hit:
                price = min(opening,p["sl"]) if p["sign"]==1 else max(opening,p["sl"])
                reason = "stop" if not target_hit else "both_hit_stop_first"
            elif target_hit:
                price, reason = p["tp"],"target"
            elif i == end-1:
                price, reason = bar["close"]+ask_offset,"end_of_segment"
            if price is not None:
                price -= p["sign"]*slip
                pnl = (price-p["entry"])*p["sign"]*contract*p["volume"]-commission*p["volume"]
                equity += pnl
                exit_time = bar["time"] if reason in ("session","time") else bar["time"]+TF_SECONDS[strategy["timeframe"]]
                trades.append({**p,"exit":price,"exit_time":exit_time,"pnl":round(pnl,6),"reason":reason,"r_multiple":pnl/p["risk_money"]})
                position = None
            elif strategy.get("algorithm")=="bollinger_pullback":
                # Only trail after this bar's exits. Its new level starts NEXT bar:
                # no optimistic ordering of unknown intrabar high/low paths.
                price = bar["close"]+ask_offset
                p["sl"],p["tp"] = trailing_levels(p,price,features[i]["atr"],strategy,spec,commission/contract+slip)
        mark = equity
        if position:
            exit_price = bar["close"]+(spread if position["sign"]==-1 else 0)
            mark += (exit_price-position["entry"])*position["sign"]*contract*position["volume"]-commission*position["volume"]
        peak = max(peak, mark)
        drawdown = max(drawdown, (peak-mark)/peak*100 if peak>0 else 0)
        curve.append({"time":bar["time"]+TF_SECONDS[strategy["timeframe"]],"equity":round(mark,4)})
    gains = sum(max(0,t["pnl"]) for t in trades)
    losses = -sum(min(0,t["pnl"]) for t in trades)
    streak = max_streak = 0
    for t in trades:
        streak = streak+1 if t["pnl"]<0 else 0
        max_streak = max(max_streak,streak)
    metrics = {"initial_capital":capital,"final_equity":round(equity,2),"net_profit":round(equity-capital,2),"return_pct":round((equity/capital-1)*100,3),"max_drawdown_pct":round(drawdown,3),
               "profit_factor":round(gains/losses,3) if losses>0 else None,"gross_profit":round(gains,3),"gross_loss":round(losses,3),"trades":len(trades),"win_rate":round(sum(t["pnl"]>0 for t in trades)/len(trades)*100,2) if trades else 0,
               "max_consecutive_losses":max_streak,"expectancy":round(sum(t["pnl"] for t in trades)/len(trades),3) if trades else 0,
               "signals":signals,"min_lot_skips":small_lot_skips,"margin_skips":margin_skips}
    return {"metrics":metrics,"equity":curve,"trades":trades}


def research_dataset(snapshot, cfg, progress=lambda _: None):
    strategy, risk, workflow = cfg["strategy"], cfg["risk"], cfg["workflow"]
    bars, spec = snapshot["frames"][strategy["timeframe"]],snapshot["spec"]
    count = len(bars)
    if count < 5000:
        raise ValueError("策略研究至少需要 5000 根真实历史 K 线")
    train_end, val_end = int(count*.5),int(count*.75)
    context = {k:v for k,v in snapshot["frames"].items() if k != strategy["timeframe"]}
    rankings = []
    candidates = list(itertools.product(["donchian","pullback"],[12,24],[.8,1.2,1.6],[1.5,2.0]))
    for n,(algorithm,lookback,stop,reward) in enumerate(candidates):
        params = {"algorithm":algorithm,"breakout_bars":lookback,"stop_atr":stop,"reward_risk":reward}
        if algorithm == "pullback" and lookback != 12:
            continue  # lookback is irrelevant for the pullback rule; no duplicate trial.
        trial = {**strategy,**params}
        progress(f"训练与验证：{n+1}/{len(candidates)} · {algorithm} ATR {stop} RR {reward}")
        train = replay(bars,context,trial,risk,workflow,spec,0,train_end)["metrics"]
        validation = replay(bars,context,trial,risk,workflow,spec,train_end,val_end)["metrics"]
        # Candidate selection cannot access the final holdout.
        enough = train["trades"] >= 30 and validation["trades"] >= 15
        score = validation["return_pct"]/(1+validation["max_drawdown_pct"]) + .25*train["return_pct"]/(1+train["max_drawdown_pct"]) if enough else -10000+validation["trades"]
        rankings.append({"params":params,"training":train,"validation":validation,"score":round(score,5)})
    rankings.sort(key=lambda x:x["score"],reverse=True)
    best = rankings[0]
    selected = {**strategy,**best["params"]}
    progress("对冻结候选执行留出测试和双倍交易成本压力测试")
    holdout = replay(bars,context,selected,risk,workflow,spec,val_end,count)
    stress = replay(bars,context,selected,risk,workflow,spec,val_end,count,2.0)
    reasons = []
    hm, vm, tm = holdout["metrics"],best["validation"],best["training"]
    if tm["trades"]<30 or vm["trades"]<15 or hm["trades"]<20: reasons.append("训练／验证／留出交易样本不足（最低 30 / 15 / 20）")
    if tm["net_profit"] <= 0 or vm["net_profit"] <= 0 or hm["net_profit"] <= 0: reasons.append("三个时间区间未全部保持正净收益")
    if hm["gross_profit"]<=0 or (hm["profit_factor"] is not None and hm["profit_factor"]<1.15): reasons.append("留出区间利润因子未达到 1.15")
    if max(tm["max_drawdown_pct"],vm["max_drawdown_pct"],hm["max_drawdown_pct"]) > 8: reasons.append("有区间最大回撤超过 8%")
    if stress["metrics"]["net_profit"] <= 0: reasons.append("双倍成本压力测试未保持正收益")
    neighbors = [x for x in rankings if x["params"]["algorithm"]==best["params"]["algorithm"]]
    positive = sum(x["validation"]["net_profit"]>0 for x in neighbors)/len(neighbors)
    if positive < .6: reasons.append("相邻参数验证收益缺乏一致性（正收益比例低于 60%）")
    raw = json.dumps(snapshot["frames"],separators=(",",":"),allow_nan=False).encode()
    def period(a,b):return {"start":bars[a]["time"],"end":bars[b-1]["time"],"bars":b-a}
    return {"created_at":time.time(),"dataset":{"symbol":snapshot["symbol"],"timeframe":strategy["timeframe"],"bars":count,"start":bars[0]["time"],"end":bars[-1]["time"],"sha256":hashlib.sha256(raw).hexdigest(),"source":"MT5 closed bid OHLC; recorded historical spreads"},
            "split":{"train":period(0,train_end),"validation":period(train_end,val_end),"holdout":period(val_end,count)},
            "candidate":{"params":selected,"training":tm,"validation":vm,"holdout":hm,"stress":stress["metrics"],"approved":not reasons,"reasons":reasons,"neighbor_positive_ratio":positive},
            "ranking":rankings,"equity":holdout["equity"][::max(1,len(holdout["equity"])//1200)],"trades":holdout["trades"],
            "assumptions":["严格按时间 50% 训练 / 25% 验证 / 25% 留出；候选选择未使用留出收益，仅排名第一候选接受留出与压力测试。修正模拟器后的重复研究属探索，不能再称全新未见样本。", "黄金线性合约、USD 资金、单持仓、按经纪商手数向下取整；达不到最小手数就跳过。", "入场用下一根开盘；H1 等背景周期只使用已闭合时点。SL/TP 均从请求价固定，不随模拟滑点移动。", "入场点差采用前一根历史点差（至少当前点差底线），退出采用当根记录点差作为估计；计入双向滑点与往返佣金，双倍成本将三项一并加倍。", "同 K 线 SL/TP 同时触及按止损；穿越止损跳空按不利开盘价。只在配置时段开仓，到时段外或最长持仓后退出。", "复用时段／波动率风险缩放；回撤为 K 线收盘权益回撤，不是盘中最大回撤。", "不含 AI 历史回放、新闻避让和真实订单簿，历史合约规格／杠杆按当前规格近似；OHLC 内路径未知，结果不是盈利承诺。", "参数稳定性是同一策略族的验证区间正收益比例；历史样本相关，单次留出不等于长期有效。"]}


class ResearchService:
    def __init__(self, engine, artifact_dir):
        self.engine = engine
        self.artifact_dir = Path(artifact_dir)
        self.job = {"status":"idle","progress":"尚未进行完整策略研究"}
        previous = engine.store.get("research_result")
        if previous: self.job = {"status":"completed","progress":"已载入上次研究结果","result":previous}

    def start(self):
        with self.engine.control:
            self.engine.mutation_allowed()
            if not self.engine.lock.acquire(blocking=False): raise Conflict("其他周期正在运行")
            self.engine.busy = True
            self.job = {"status":"running","progress":"读取 MT5 黄金历史数据"}
            threading.Thread(target=self._run,daemon=True,name="fusion-research").start()
            return self.job

    def _run(self):
        try:
            cfg, versions = self.engine.configs()
            # A research scope is explicit and preserved in its manifest, not silently saved as live config.
            cfg["strategy"] = {**cfg["strategy"],"symbol":"XAUUSD","timeframe":"M5","context_timeframes":["H1"],"bars":60000}
            status = self.engine.broker.status()
            if status["account"].get("currency") != "USD": raise ValueError("本轮研究需要 USD 账户合约规格")
            snap = self.engine.broker.snapshot(cfg["strategy"])
            first = snap["frames"]["M5"][0]["time"]
            snap["frames"]["H1"] = [b for b in snap["frames"]["H1"] if b["time"] >= first-400*3600]
            spec = snap["spec"]
            spec["fallback_spread_points"] = (snap["tick"]["ask"]-snap["tick"]["bid"])/spec["point"]
            spec["reference_price"] = snap["tick"]["ask"]
            spec["margin_per_lot"] = self.engine.broker.margin("XAUUSD","BUY",1.0,spec["reference_price"])
            self.artifact_dir.mkdir(parents=True,exist_ok=True)
            with gzip.open(self.artifact_dir/"gold-history.json.gz","wt",encoding="utf-8") as f: json.dump(snap,f,ensure_ascii=False,allow_nan=False)
            result = research_dataset(snap,cfg,lambda message:self.job.update(progress=message))
            result["config_versions"] = versions
            result["research_config"] = cfg
            save_research_report(self.artifact_dir,result)
            self.engine.store.put("research_result",result)
            self.engine.store.audit("research.completed",{"dataset":result["dataset"],"approved":result["candidate"]["approved"],"holdout":result["candidate"]["holdout"]})
            self.job = {"status":"completed","progress":"研究完成；查看留出测试与准入结论","result":result}
        except Exception as exc:
            self.job = {"status":"error","progress":"研究未完成","error":str(exc)}
            self.engine.store.audit("research.error",{"error":str(exc)})
        finally:
            self.engine.busy = False
            self.engine.lock.release()
