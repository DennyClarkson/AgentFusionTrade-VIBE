"""Causal market feature packet. Missing/warming indicators stay explicit, never invented."""
import math
import statistics
from collections import deque
from datetime import datetime, timezone

from .config import Indicators
from .strategy import TF_SECONDS, valid_bars


def series(bars, settings=None):
    c = Indicators.model_validate(settings or {}).model_dump()
    valid_bars(bars, 2)
    periods = sorted(set(c["ema_periods"] + [c["macd_fast"], c["macd_slow"]]))
    emas = {p: bars[0]["close"] for p in periods}
    atr = gain = loss = tr_adx = plus = minus = adx = signal = 0.0
    bb, tr_history, volume_history, result = deque(), deque(), deque(), []
    for i, b in enumerate(bars):
        prev = bars[max(0, i-1)]
        for p in periods:
            emas[p] += 2/(p+1)*(b["close"]-emas[p])
        change = b["close"]-prev["close"]
        tr = max(b["high"]-b["low"], abs(b["high"]-prev["close"]), abs(b["low"]-prev["close"]))
        # Expanding mean seeds Wilder recurrences; after N use alpha=1/N.
        an, rn, dn = min(i+1,c["atr_period"]), min(max(i,1),c["rsi_period"]), min(i+1,c["adx_period"])
        atr += (tr-atr)/an
        if i:
            gain += (max(change,0)-gain)/rn
            loss += (max(-change,0)-loss)/rn
        up, down = b["high"]-prev["high"], prev["low"]-b["low"]
        tr_adx += (tr-tr_adx)/dn
        plus += ((up if up>down and up>0 else 0)-plus)/dn
        minus += ((down if down>up and down>0 else 0)-minus)/dn
        di_plus, di_minus = (100*plus/tr_adx,100*minus/tr_adx) if tr_adx else (0,0)
        dx = 100*abs(di_plus-di_minus)/(di_plus+di_minus) if di_plus+di_minus else 0
        adx += (dx-adx)/dn
        macd = emas[c["macd_fast"]]-emas[c["macd_slow"]]
        signal += 2/(c["macd_signal"]+1)*(macd-signal)
        bb.append(b["close"])
        if len(bb)>c["bollinger_period"]: bb.popleft()
        mid = sum(bb)/len(bb)
        sd = statistics.pstdev(bb) if len(bb)>1 else 0
        lower,upper = mid-c["bollinger_deviation"]*sd,mid+c["bollinger_deviation"]*sd
        prior_atr = statistics.median(tr_history) if tr_history else atr
        tr_history.append(atr)
        if len(tr_history)>100: tr_history.popleft()
        mean_vol = sum(volume_history)/len(volume_history) if volume_history else 0
        volume = b.get("volume",0)
        volume_history.append(volume)
        if len(volume_history)>20: volume_history.popleft()
        row = {"time":b["time"], "close":b["close"], **{f"ema_{p}":emas[p] for p in c["ema_periods"]},
               "atr":atr, "atr_percent":atr/b["close"]*100, "atr_baseline":prior_atr,
               "volatility_ratio":atr/prior_atr if prior_atr else 1,
               "rsi":(100-100/(1+gain/loss) if loss else 100 if gain else 50) if i>=c["rsi_period"] else None,
               "adx":adx if i>=2*c["adx_period"] else None, "di_plus":di_plus,"di_minus":di_minus,
               "macd":macd,"macd_signal":signal,"macd_histogram":macd-signal,
               "bb_middle":mid,"bb_lower":lower,"bb_upper":upper,"bb_width_percent":(upper-lower)/mid*100,
               "bb_percent_b":(b["close"]-lower)/(upper-lower) if upper>lower else .5,
               "return_1_pct":change/prev["close"]*100,"relative_tick_volume":volume/mean_vol if mean_vol else None}
        result.append(row)
    return result


def packet(snapshot, settings=None):
    c = Indicators.model_validate(settings or {}).model_dump()
    frames = {}
    for tf,bars in snapshot["frames"].items():
        rows = series(bars,c)
        last = rows[-1]
        support = min(b["low"] for b in bars[-20:])
        resistance = max(b["high"] for b in bars[-20:])
        gap_count = sum(b["time"]-a["time"]>TF_SECONDS[tf]*1.5 for a,b in zip(bars[-100:-1],bars[-99:]))
        frames[tf] = {"indicators":last,"previous_indicators":rows[-2],"support_20":support,"resistance_20":resistance,
                      "candles":bars[-c["candles_in_prompt"]:],"indicator_history":rows[-5:],
                      "quality":{"bars":len(bars),"last_closed_at":bars[-1]["time"]+TF_SECONDS[tf],
                                 "age_seconds":round(snapshot["captured_at"]-bars[-1]["time"]-TF_SECONDS[tf],1),
                                 "warmup_sufficient":len(bars)>=max(c["ema_periods"])*3,"gaps_in_last_100":gap_count}}
    return {"symbol":snapshot["symbol"],"as_of_utc":snapshot["captured_at"],"as_of_iso_utc":datetime.fromtimestamp(snapshot["captured_at"],timezone.utc).isoformat(),"quote":snapshot["tick"],
            "spread_points":(snapshot["tick"]["ask"]-snapshot["tick"]["bid"])/snapshot["spec"]["point"],
            "contract":snapshot["spec"],"frames":frames,"indicator_config":c,
            "provenance":{"source":snapshot.get("source","MT5"),"closed_only":True,"time_offset_hours":snapshot.get("time_offset_hours",0),
                          "volume_kind":"tick_volume; not exchange volume","bb_std":"population","atr_rsi_adx":"Wilder recurrence with expanding seed"}}
