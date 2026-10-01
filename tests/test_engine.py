import copy
import threading
import time

import pytest

from fusion.broker import MT5Broker, BrokerError
from fusion.store import Conflict
from fusion.strategy import analyze


def update(store, category, **kwargs):
    row = next(x for x in store.configs() if x["category"] == category and x["active"])
    store.save(category, row["id"], row["name"], {**row["data"], **kwargs}, row["version"])


def test_shadow_never_sends_and_same_bar_cannot_execute_twice(configured):
    store, broker, engine = configured
    result = engine.cycle()
    assert result["status"] == "paper_filled"
    assert result["risk"]["risk_money"] <= 5
    assert not broker.sent
    engine.close_paper()
    result = engine.cycle()
    assert result["status"] == "blocked"
    assert "重复" in result["risk"]["reasons"][0]


def test_minimum_lot_is_rejected_not_rounded_up(configured):
    store, broker, engine = configured
    broker.data["spec"]["volume_min"] = 1
    result = engine.cycle()
    assert result["status"] == "blocked"
    assert any("最小手数" in x for x in result["risk"]["reasons"])
    assert not broker.sent and not store.get("paper_positions")


@pytest.mark.parametrize("age", [400, -10800])
def test_stale_or_future_ticks_block(configured, age):
    _, broker, engine = configured
    broker.data["tick"]["time"] = time.time()-age
    result = engine.cycle()
    assert not result["risk"]["allowed"]
    assert not broker.sent


def test_daily_loss_circuit_breaker(configured):
    store, _, engine = configured
    store.put("paper_trades", [{"pnl": -20, "exit_time": time.time()}])
    result = engine.cycle()
    assert any("当日亏损" in x for x in result["risk"]["reasons"])


def test_demo_requires_arm_and_persisted_intent_before_send(configured):
    store, broker, engine = configured
    update(store, "workflow", mode="demo")
    assert engine.cycle()["status"] == "blocked"
    engine.arm(123)
    broker.send_hook = lambda: pytest.assume(store.unresolved()) if hasattr(pytest, "assume") else (_ for _ in ()).throw(AssertionError()) if not store.unresolved() else None
    result = engine.cycle()
    assert result["status"] == "filled"
    assert len(broker.sent) == 1
    assert not store.unresolved()


def test_uncertain_send_locks_future_orders(configured):
    store, broker, engine = configured
    update(store, "workflow", mode="demo")
    engine.arm(123)
    def uncertain(): raise TimeoutError("transport")
    broker.send_hook = uncertain
    assert engine.cycle()["status"] == "unknown"
    assert store.unresolved()
    assert not engine.armed
    with pytest.raises(Conflict): engine.arm(123)


def test_real_account_never_arms_and_low_level_guard_rejects(configured):
    _, broker, engine = configured
    broker.demo = False
    with pytest.raises(ValueError): engine.arm(123)
    adapter = MT5Broker(module=object())
    adapter.status = broker.status
    adapter.connect = lambda: None
    adapter.account_pin = (123, "Demo")
    with pytest.raises(BrokerError): adapter.send_order({})


def test_account_switch_rejects_demo_cycle(configured):
    store, broker, engine = configured
    update(store, "workflow", mode="demo")
    engine.arm(123)
    broker.login = 456
    result = engine.cycle()
    assert not result["risk"]["allowed"] and not broker.sent


def test_stop_during_ai_discards_inflight_decision(configured):
    store, broker, engine = configured
    update(store, "workflow", ai_gate="advisory")
    entered, released = threading.Event(), threading.Event()
    class PausingAI:
        def run(self, *args):
            entered.set()
            assert released.wait(5)
            return []
    engine.ai = PausingAI()
    results = []
    thread = threading.Thread(target=lambda: results.append(engine.cycle()))
    thread.start()
    assert entered.wait(5)
    with pytest.raises(Conflict): engine.cycle()
    engine.stop()
    released.set()
    thread.join(5)
    assert not thread.is_alive()
    assert not results[0]["risk"]["allowed"]
    assert not store.get("paper_positions") and not broker.sent


def test_required_ai_failure_is_closed(configured):
    store, _, engine = configured
    update(store, "workflow", ai_gate="required")
    class MissingAI:
        def run(self, *args): return []
    engine.ai = MissingAI()
    result = engine.cycle()
    assert any("AI 必需" in x for x in result["risk"]["reasons"])


def test_new_bar_after_ai_invalidates_decision(configured):
    store, broker, engine = configured
    update(store, "workflow", ai_gate="advisory")
    class AdvancingAI:
        def run(self, *args):
            broker.data["frames"]["M5"][-1]["time"] += 300
            return []
    engine.ai = AdvancingAI()
    result = engine.cycle()
    assert any("新闭合" in x for x in result["risk"]["reasons"])
    assert not store.get("paper_positions")
