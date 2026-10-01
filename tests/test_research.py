import copy
import time

import pytest

from fusion.research import replay
from fusion.strategy import feature_series, analyze, rule_action


def test_feature_prefix_does_not_change_when_future_prices_change(configured):
    store, broker, _ = configured
    s = store.active()[0]["strategy"]
    original = broker.data["frames"]["M5"]
    changed = copy.deepcopy(original)
    for b in changed[180:]:
        for k in ("open","high","low","close"): b[k] *= 10
    assert feature_series(original,s)[:180] == feature_series(changed,s)[:180]
    assert analyze(broker.data,s)["action"] == rule_action(feature_series(original,s)[-1],[],s)


def fixture_bars(configured):
    store,broker,_ = configured
    cfg = store.active()[0]
    cfg["risk"]["sessions"]=[{"name":"fixture","start_utc":0,"end_utc":24,"risk_scale":1,"stop_scale":1,"target_scale":1}]
    cfg["risk"].update(volatility_reduce_ratio=10,volatility_pause_ratio=20)
    bars = copy.deepcopy(broker.data["frames"]["M5"])
    cfg["strategy"].update(slow_ema=10, fast_ema=3, breakout_bars=3, atr_period=3, min_trend_atr=0, stop_atr=1,reward_risk=1.5)
    for i,b in enumerate(bars):
        close = 2000+i*.1
        b.update(open=close-.01,high=close+1,low=close-1,close=close)
    # First trade signal only at penultimate bar, and its next bar triggers both protections.
    bars[-2].update(open=2021,close=2024,high=2024.2,low=2020)
    bars[-1].update(open=2024,close=2024,high=2035,low=2010)
    return cfg,broker.data["spec"],bars


def test_replay_both_protections_hit_is_stop_and_includes_costs(configured):
    cfg,spec,bars = fixture_bars(configured)
    result = replay(bars,{},cfg["strategy"],cfg["risk"],cfg["workflow"],spec)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == bars[-1]["time"]
    assert trade["reason"] == "both_hit_stop_first"
    assert trade["pnl"] < 0
    assert trade["pnl"] == pytest.approx((trade["exit"]-trade["entry"])*100*trade["volume"]-cfg["risk"]["commission_per_lot"]*trade["volume"],abs=1e-6)


def test_replay_missing_weekend_bar_does_not_execute_old_signal(configured):
    cfg,spec,bars = fixture_bars(configured)
    bars[-1]["time"] += 3*86400
    result = replay(bars,{},cfg["strategy"],cfg["risk"],cfg["workflow"],spec)
    assert result["metrics"]["trades"] == 0


def test_future_higher_timeframe_bars_do_not_authorize_entry(configured):
    cfg,spec,bars = fixture_bars(configured)
    cfg["strategy"]["context_timeframes"] = ["H1"]
    future = copy.deepcopy(bars)
    for i,b in enumerate(future): b["time"] = bars[-1]["time"]+86400+i*3600
    result = replay(bars,{"H1":future},cfg["strategy"],cfg["risk"],cfg["workflow"],spec)
    assert result["metrics"]["trades"] == 0


def test_stop_gap_fills_at_adverse_open_not_stop(configured):
    cfg,spec,bars = fixture_bars(configured)
    # Add another bar, with first trade bar neither hitting protection.
    bars[-1].update(high=2024.1,low=2023.9)
    bars.append({**bars[-1],"time":bars[-1]["time"]+300,"open":2010,"high":2011,"low":2008,"close":2009})
    result = replay(bars,{},cfg["strategy"],cfg["risk"],cfg["workflow"],spec)
    trade = result["trades"][0]
    assert trade["exit"] < trade["sl"]
