import copy

import pytest

from fusion.strategy import analyze, backtest


def test_closed_breakout_signal_and_invalid_ohlc(configured):
    store, broker, _ = configured
    cfg = store.active()[0]
    result = analyze(broker.data, cfg["strategy"])
    assert result["action"] == "BUY"
    assert result["sl"] < result["entry"] < result["tp"]
    broker.data["frames"]["M5"][-1]["high"] = 1
    with pytest.raises(ValueError): analyze(broker.data, cfg["strategy"])


def test_hold_has_no_executable_prices(configured):
    store, broker, _ = configured
    for b in broker.data["frames"]["M5"]:
        b.update(open=2000, high=2001, low=1999, close=2000)
    signal = analyze(broker.data, store.active()[0]["strategy"])
    assert signal["action"] == "HOLD"
    assert signal["entry"] is signal["sl"] is signal["tp"] is None


def test_backtest_never_trades_on_last_bar_signal_without_next_bar(configured):
    store, broker, _ = configured
    cfg = store.active()[0]
    bars = broker.data["frames"]["M5"]
    # Prior highs prevent entry until last bar; no future bar exists to fill it.
    for b in bars[:-1]: b["high"] += 10
    bars[-1]["close"] += 20
    bars[-1]["high"] = bars[-1]["close"]+1
    result = backtest(bars, cfg["strategy"], cfg["risk"], broker.data["spec"])
    assert result["metrics"]["trades"] == 0
    assert result["metrics"]["final_equity"] == 1000
