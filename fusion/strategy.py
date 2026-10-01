"""Pure closed-bar CTA rules shared by analysis and historical replay."""
import math
from collections import deque
from datetime import datetime, timezone

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1":86400}


def session_open(timestamp, workflow):
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    a, b = workflow["session_start_utc"], workflow["session_end_utc"]
    hours = a == b or (a <= dt.hour < b if a < b else dt.hour >= a or dt.hour < b)
    return hours and not (workflow["block_weekends"] and dt.weekday() >= 5)


def ema(values, period):
    value = values[0]
    alpha = 2 / (period + 1)
    for x in values[1:]:
        value += alpha * (x - value)
    return value


def feature_series(bars, strategy):
    """One-pass causal indicators; index i includes bar i and no future data."""
    valid_bars(bars, 2)
    fast, slow = bars[0]["close"], bars[0]["close"]
    af, ass = 2/(strategy["fast_ema"]+1), 2/(strategy["slow_ema"]+1)
    highq, lowq, trq = deque(), deque(), deque()
    trsum, result = 0.0, []
    for i, b in enumerate(bars):
        previous_fast = fast
        fast += af*(b["close"]-fast)
        slow += ass*(b["close"]-slow)
        if i:
            prev = bars[i-1]
            tr = max(b["high"]-b["low"], abs(b["high"]-prev["close"]), abs(b["low"]-prev["close"]))
            trq.append(tr); trsum += tr
            if len(trq) > strategy["atr_period"]: trsum -= trq.popleft()
            while highq and bars[highq[-1]]["high"] <= prev["high"]: highq.pop()
            while lowq and bars[lowq[-1]]["low"] >= prev["low"]: lowq.pop()
            highq.append(i-1); lowq.append(i-1)
        while highq and highq[0] < i-strategy["breakout_bars"]: highq.popleft()
        while lowq and lowq[0] < i-strategy["breakout_bars"]: lowq.popleft()
        result.append({"fast_ema": fast, "slow_ema": slow, "atr": trsum/len(trq) if trq else 0,
                       "channel_high": bars[highq[0]]["high"] if highq else b["high"], "channel_low": bars[lowq[0]]["low"] if lowq else b["low"],
                       "close": b["close"], "previous_high": bars[i-1]["high"] if i else b["high"], "previous_low": bars[i-1]["low"] if i else b["low"], "previous_fast": previous_fast})
    if strategy.get("algorithm")=="bollinger_pullback":
        from .indicators import series
        extra = series(bars,{"ema_periods":[strategy["fast_ema"],strategy["slow_ema"]],"atr_period":strategy["atr_period"],"bollinger_period":strategy["bollinger_period"],"bollinger_deviation":strategy["bollinger_deviation"]})
        for i,(f,e,b) in enumerate(zip(result,extra,bars)):
            f.update(e)
            f["fast_ema"],f["slow_ema"] = e[f"ema_{strategy['fast_ema']}"],e[f"ema_{strategy['slow_ema']}"]
            prior = extra[max(0,i-5)]
            f.update(ema_slope_atr=(f["fast_ema"]-prior[f"ema_{strategy['fast_ema']}"])/max(f["atr"],1e-12)/max(1,min(i,5)),
                     previous_bb_lower=extra[max(0,i-1)]["bb_lower"],previous_bb_upper=extra[max(0,i-1)]["bb_upper"],open=b["open"],high=b["high"],low=b["low"])
    return result


def trend_direction(f,strategy,strict=True):
    separation = (f["fast_ema"]-f["slow_ema"])/max(f["atr"],1e-12)
    if abs(separation)<strategy["min_trend_atr"]: return "FLAT"
    side = "UP" if separation>0 else "DOWN"
    aligned_slope = f.get("ema_slope_atr",0)*(1 if side=="UP" else -1)
    if strict and aligned_slope<strategy["trend_slope_atr"] and (f.get("adx") or 0)<strategy["trend_adx"]: return "FLAT"
    return side


def rule_action(features, directions, strategy):
    f = features
    if strategy.get("algorithm")=="bollinger_pullback":
        direction = trend_direction(f,strategy)
        if direction=="FLAT":
            context = dict(zip(strategy["context_timeframes"],directions))
            bias = strategy["range_bias"]
            if bias=="consensus":
                direction=context.get("H1","FLAT") if context.get("H1")==context.get("D1") else "FLAT"
            else: direction=context.get(bias,"FLAT")
        # Closed-bar touch + recovery inside band; never reverse an explicit M1 trend.
        if direction=="UP" and f["low"]<=f["previous_bb_lower"] and f["close"]>f["bb_lower"] and f["close"]>f["open"]: return "BUY"
        if direction=="DOWN" and f["high"]>=f["previous_bb_upper"] and f["close"]<f["bb_upper"] and f["close"]<f["open"]: return "SELL"
        return "HOLD"
    if f["atr"] <= 0 or abs(f["fast_ema"]-f["slow_ema"])/f["atr"] < strategy.get("min_trend_atr", .2):
        return "HOLD"
    if strategy.get("algorithm", "donchian") == "pullback":
        long_trigger = f["previous_low"] <= f["previous_fast"] and f["close"] > f["previous_high"] and f["close"] > f["fast_ema"]
        short_trigger = f["previous_high"] >= f["previous_fast"] and f["close"] < f["previous_low"] and f["close"] < f["fast_ema"]
    else:
        long_trigger = f["close"] > f["channel_high"]
        short_trigger = f["close"] < f["channel_low"]
    if long_trigger and f["fast_ema"] > f["slow_ema"] and all(d == "UP" for d in directions): return "BUY"
    if short_trigger and f["fast_ema"] < f["slow_ema"] and all(d == "DOWN" for d in directions): return "SELL"
    return "HOLD"


def valid_bars(bars, minimum):
    if len(bars) < minimum:
        raise ValueError(f"闭合 K 线不足：需要 {minimum} 根，实际 {len(bars)}")
    last_time = -1
    for b in bars:
        prices = [b[k] for k in ("open", "high", "low", "close")]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in prices):
            raise ValueError("OHLC 包含非正数或非有限值")
        if not (b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]):
            raise ValueError("OHLC 价格关系错误")
        if not math.isfinite(b["time"]) or b["time"] <= last_time:
            raise ValueError("K 线必须时间递增且无重复")
        last_time = b["time"]


def analyze(snapshot, strategy):
    frames = snapshot["frames"]
    bars = frames[strategy["timeframe"]]
    minimum = max(strategy["slow_ema"] * 3, strategy["breakout_bars"] + 2, strategy["atr_period"] + 2)
    valid_bars(bars, minimum)
    features = feature_series(bars, strategy)[-1]
    atr = features["atr"]
    context = {}
    for tf in strategy["context_timeframes"]:
        other = frames.get(tf, [])
        valid_bars(other, strategy["slow_ema"] * 3)
        values = [b["close"] for b in other]
        f, s = ema(values, strategy["fast_ema"]), ema(values, strategy["slow_ema"])
        direction="UP" if f > s else "DOWN" if f < s else "FLAT"
        if strategy.get("algorithm")=="bollinger_pullback": direction=trend_direction(feature_series(other,strategy)[-1],strategy,strict=False)
        context[tf] = {"direction": direction, "fast_ema": f, "slow_ema": s, "bar_time": other[-1]["time"]}
    action = rule_action(features, [v["direction"] for v in context.values()], strategy)
    reason = "等待闭合 K 线触发策略并与多周期趋势一致" if action == "HOLD" else ("闭合突破" if strategy.get("algorithm", "donchian") == "donchian" else "趋势回踩后确认") + "，EMA 与背景周期同向"
    if strategy.get("algorithm")=="bollinger_pullback":
        direction=trend_direction(features,strategy)
        reason=f"M1 {'趋势 '+direction if direction!='FLAT' else '震荡，采用 '+strategy['range_bias']+' 背景'}；"+("布林外轨触及后收回确认" if action!='HOLD' else "等待顺势布林回踩确认")
    entry = snapshot["tick"]["ask" if action == "BUY" else "bid"] if action != "HOLD" else None
    sign = 1 if action == "BUY" else -1
    distance = atr * strategy["stop_atr"]
    return {"action": action, "reason": reason, "entry": entry, "sl": entry - sign * distance if entry else None,
            "tp": entry + sign * distance * strategy["reward_risk"] if entry else None, "atr": atr, "bar_time": bars[-1]["time"],
            "indicators": features, "context": context}


def trailing_levels(position, price, atr, strategy, spec, cost_buffer=0):
    """Monotonic protection, based on the initial risk. Current quote/closed ATR only."""
    sign = 1 if position["action"]=="BUY" else -1
    initial = abs(position["entry"]-position.get("initial_sl",position["sl"]))
    sl,tp = position["sl"],position["tp"]
    gain = (price-position["entry"])*sign
    if initial<=0 or gain<=0: return sl,tp
    candidate = sl
    if gain>=strategy["breakeven_r"]*initial:
        candidate = max(sl,position["entry"]+cost_buffer) if sign==1 else min(sl,position["entry"]-cost_buffer)
    if gain>=strategy["trailing_start_r"]*initial:
        trailing = price-sign*atr*strategy["trailing_atr"]
        candidate = max(candidate,trailing) if sign==1 else min(candidate,trailing)
    step = spec["trade_tick_size"]
    # Round away from current market, then require an actual tightening.
    candidate = round((math.floor(candidate/step) if sign==1 else math.ceil(candidate/step))*step,spec["digits"])
    minimum = max(spec.get("trade_stops_level",0),spec.get("trade_freeze_level",0))*spec["point"]+step
    if (price-candidate)*sign<minimum or (candidate-sl)*sign<step*.9: return sl,tp
    if gain>=strategy["trailing_start_r"]*initial and (tp-price)*sign<atr:
        tp=round((price+sign*max(atr*strategy["reward_risk"],minimum))/step)*step
    return candidate,round(tp,spec["digits"])


def backtest(bars, strategy, risk, spec, costs=None, context_frames=None):
    """OHLC research approximation. Never claims an AI or tick-level backtest."""
    costs = costs or {}
    spread = float(costs.get("spread_points", 0)) * spec["point"]
    slippage = float(costs.get("slippage_points", 0)) * spec["point"]
    commission = float(costs.get("commission_per_lot", 0))
    contract = spec["trade_contract_size"]
    minimum = max(strategy["slow_ema"] * 3, strategy["breakout_bars"] + 2, strategy["atr_period"] + 2)
    valid_bars(bars, minimum + 2)
    equity, curve, trades, position = risk["capital"], [], [], None
    peak, max_drawdown = equity, 0
    skipped, day_start, day_equity = 0, None, equity
    for i in range(minimum, len(bars)):
        b = bars[i]
        day = int(b["time"] // 86400)
        if day != day_start:
            day_start, day_equity = day, equity
        # Signal at previous close. The next bar's open is the earliest possible fill.
        if position is None and equity > 0 and equity > day_equity - risk["capital"] * risk["daily_loss_pct"] / 100:
            cutoff = bars[i-1]["time"] + TF_SECONDS[strategy["timeframe"]]
            frames = {strategy["timeframe"]: bars[:i]}
            for tf in strategy["context_timeframes"]:
                frames[tf] = [x for x in (context_frames or {}).get(tf, []) if x["time"] + TF_SECONDS[tf] <= cutoff]
            try:
                signal = analyze({"frames": frames, "tick": {"bid": b["open"], "ask": b["open"] + spread}}, strategy)
            except ValueError:
                skipped += 1
                signal = {"action": "HOLD"}
            if signal["action"] != "HOLD":
                sign = 1 if signal["action"] == "BUY" else -1
                entry = signal["entry"] + sign * slippage
                distance = signal["atr"] * strategy["stop_atr"]
                risk_money = min(equity, risk["capital"]) * risk["risk_per_trade_pct"] / 100
                risk_money = min(risk_money, max(0, risk["capital"]*risk["daily_loss_pct"]/100+min(0,equity-day_equity)))
                per_lot = (distance + slippage) * contract + commission
                volume = math.floor((risk_money / per_lot + 1e-10) / spec["volume_step"]) * spec["volume_step"]
                volume = min(volume, spec["volume_max"])
                if volume >= spec["volume_min"]:
                    position = {"action": signal["action"], "sign": sign, "entry": entry, "sl": signal["sl"], "tp": signal["tp"], "volume": volume, "time": b["time"]}
        if position:
            p = position
            # MT5 candles typically represent bid. Shorts exit at ask.
            high, low = b["high"] + (spread if p["sign"] == -1 else 0), b["low"] + (spread if p["sign"] == -1 else 0)
            stop = low <= p["sl"] if p["sign"] == 1 else high >= p["sl"]
            take = high >= p["tp"] if p["sign"] == 1 else low <= p["tp"]
            expired = b["time"] - p["time"] >= strategy["max_hold_minutes"] * 60
            exit_price, reason = None, None
            if stop:
                opening = b["open"] + (spread if p["sign"] == -1 else 0)
                exit_price = min(p["sl"], opening) if p["sign"] == 1 else max(p["sl"], opening)
                reason = "stop"
            elif take:
                exit_price, reason = p["tp"], "target"
            elif expired or i == len(bars)-1:
                exit_price, reason = b["close"] + (spread if p["sign"] == -1 else 0), "time/end"
            if exit_price is not None:
                exit_price -= p["sign"] * slippage
                pnl = (exit_price - p["entry"]) * p["sign"] * contract * p["volume"] - commission * p["volume"]
                equity += pnl
                trades.append({**p, "exit": exit_price, "exit_time": b["time"], "pnl": pnl, "reason": reason})
                position = None
        mark = equity
        if position:
            mark_price = b["close"] + (spread if position["sign"] == -1 else 0)
            mark += (mark_price-position["entry"])*position["sign"]*contract*position["volume"]
        peak = max(peak, mark)
        max_drawdown = max(max_drawdown, (peak-mark)/peak*100 if peak else 0)
        curve.append({"time": b["time"], "equity": round(mark, 2)})
    return {"metrics": {"initial_capital": risk["capital"], "final_equity": round(equity, 2), "net_profit": round(equity-risk["capital"], 2), "return_pct": (equity/risk["capital"]-1)*100,
                        "max_drawdown_pct": max_drawdown, "trades": len(trades), "win_rate": sum(t["pnl"]>0 for t in trades)/len(trades)*100 if trades else 0}, "equity": curve, "trades": trades,
            "assumptions": ["研究近似：只回放确定性 CTA；不含 AI、新闻、时段过滤或真实成交队列。", "使用闭合信号，下一根开盘成交；同根止损止盈都触发时按止损处理。", "合约盈亏按线性 USD 报价计算，仅适用于当前 XAUUSD/USD 预设。", f"点差 {costs.get('spread_points',0)} 点，单边滑点 {costs.get('slippage_points',0)} 点，往返佣金 {commission}/手；不含隔夜费。", f"背景周期预热不足跳过 {skipped} 个时点；样本 {len(bars)} 根，不能据此认定策略盈利。"]}
