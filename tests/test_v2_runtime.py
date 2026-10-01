import copy
import json
import time
import threading
from types import SimpleNamespace

import pytest

from fusion.ai import AIClient, function
from fusion.config import DEFAULTS
from fusion.indicators import series, packet
from fusion.strategy import rule_action, trailing_levels
from fusion.ea_manager import bounded_patch, EAManager
from fusion.store import Store
from fusion.broker import MT5Broker, BrokerError, OrderNotSubmitted
from fusion.planning import form_plan
from fusion.strategy import analyze, TF_SECONDS


def test_thinking_tool_loop_preserves_reasoning_and_real_tool_result(monkeypatch):
    requests=[]
    replies=[{"role":"assistant","content":"","reasoning_content":"opaque provider state","tool_calls":[{"id":"call_1","type":"function","function":{"name":"quote","arguments":"{}"}}]},
             {"role":"assistant","content":"工具确认 42","reasoning_content":"opaque final state"}]
    class Response:
        status_code=200
        def json(self): return {"model":"reported-model","choices":[{"message":replies.pop(0),"finish_reason":"stop"}],"usage":{"prompt_tokens":10}}
    class Client:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,*args,**kwargs): requests.append(copy.deepcopy(kwargs["json"]));return Response()
    monkeypatch.setattr("fusion.ai.httpx.Client",Client)
    monkeypatch.setenv("FIXTURE_AI","local-test-value")
    cfg={**DEFAULTS["ai"],"thinking":True,"key_env":"FIXTURE_AI","output_token_policy":"fixed"}
    calls=[]
    result=AIClient().complete(cfg,"test",[{"role":"user","content":"quote"}],[function("quote","test")],lambda n,a:calls.append(n) or {"price":42})
    assert result["status"]=="ok" and calls==["quote"]
    assert requests[1]["messages"][-2]["reasoning_content"]=="opaque provider state"
    assert json.loads(requests[1]["messages"][-1]["content"])=={"price":42}
    assert result["model"]=="reported-model" and result["tools"][0]["name"]=="quote"
    assert "reasoning_content" not in {k:v for k,v in result.items() if not k.startswith("_")}


def test_complete_unknown_function_never_runs_dispatch(monkeypatch):
    class Response:
        status_code=200
        def json(self):return {"choices":[{"message":{"content":"","tool_calls":[{"id":"x","function":{"name":"shell","arguments":"{}"}}]}}]}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr("fusion.ai.httpx.Client",Client);monkeypatch.setenv("FIXTURE_AI","test")
    calls=[]
    r=AIClient().complete({**DEFAULTS["ai"],"key_env":"FIXTURE_AI","output_token_policy":"fixed"},"test",[],[function("safe","safe")],lambda *a:calls.append(a),rounds=1)
    assert not calls and r["status"]=="error"


def test_indicators_are_causal_and_packet_has_provenance(configured):
    bars=configured[1].data["frames"]["M5"]
    rows=series(bars)
    assert series(bars[:180])[-1]==rows[179]
    p=packet(configured[1].data)
    assert p["provenance"]["closed_only"] and p["frames"]["M5"]["indicators"]["bb_upper"]>=p["frames"]["M5"]["indicators"]["bb_lower"]
    assert {"rsi","adx","di_plus","macd","relative_tick_volume","atr_baseline"}<=p["frames"]["M5"]["indicators"].keys()


def test_bollinger_never_fades_clear_trend_and_range_uses_selected_bias():
    s=DEFAULTS["ea"]["strategy"]
    f={"fast_ema":202,"slow_ema":200,"atr":2,"adx":30,"ema_slope_atr":.1,"low":197,"high":205,"open":202,"close":201,"bb_lower":198,"bb_upper":204,"previous_bb_lower":198,"previous_bb_upper":204}
    assert rule_action(f,["DOWN","DOWN"],s)=="HOLD"  # upper-band sell cannot fade uptrend
    f.update(open=198,close=199)
    assert rule_action(f,["DOWN","DOWN"],s)=="BUY"  # clear M1 remains directional
    f.update(fast_ema=200,slow_ema=202,ema_slope_atr=-.1)
    assert rule_action(f,["UP","UP"],s)=="HOLD"
    f.update(fast_ema=200,slow_ema=200,adx=10,ema_slope_atr=0)
    assert rule_action(f,["UP","DOWN"],s)=="BUY"
    assert rule_action(f,["DOWN","UP"],s)=="HOLD"
    assert rule_action(f,["UP","DOWN"],{**s,"range_bias":"consensus"})=="HOLD"


def test_trailing_locks_profit_and_cannot_loosen(configured):
    spec=configured[1].data["spec"];s=DEFAULTS["ea"]["strategy"]
    p={"action":"BUY","entry":2000,"initial_sl":1998,"sl":1998,"tp":2004}
    sl,tp=trailing_levels(p,2003.5,1,s,spec,.1)
    assert sl>2000 and tp>2004
    p.update(sl=sl,tp=tp)
    assert trailing_levels(p,2001,1,s,spec,.1)==(sl,tp)
    short={"action":"SELL","entry":2000,"initial_sl":2002,"sl":2002,"tp":1996}
    sl,tp=trailing_levels(short,1996.5,1,s,spec,.1)
    assert sl<2000 and tp<1996


def test_conversation_memory_survives_store_reopen(tmp_path):
    path=tmp_path/"state.db";s=Store(path)
    chat=s.new_conversation("布林参数")
    s.message(chat["id"],"user","记住仅顺势")
    s.message(chat["id"],"assistant","已记录",{"tools":[{"name":"get_strategy"}]})
    s.remember("live:market",{"summary":"previous regime"})
    again=Store(path)
    assert len(again.conversation(chat["id"])["messages"])==2
    assert again.memories("live:market")[0]["summary"]=="previous regime"


def test_bounded_parameter_lane_cannot_modify_risk_or_skip_validation(configured,tmp_path):
    settings=DEFAULTS["ea"]
    for patch in ({"risk_per_trade_pct":5},{"bollinger_period":50},{"stop_atr":float("nan")}):
        with pytest.raises(ValueError):bounded_patch(settings,patch)
    assert bounded_patch(settings,{"bollinger_period":22})["bollinger_period"]==22
    manager=EAManager(configured[2],tmp_path)
    with pytest.raises(ValueError):manager.propose({"bollinger_period":22},"invented","change")


def test_secret_redaction_also_covers_progress_and_mutating_dispatch(monkeypatch):
    secret="fixture-secret-dont-persist"
    count=[0]
    class Response:
        status_code=200
        def json(self):
            count[0]+=1
            message={"content":"ok"} if count[0]>1 else {"content":"","tool_calls":[{"id":"a","function":{"name":"save","arguments":json.dumps({"reason":secret})}}]}
            return {"choices":[{"message":message}]}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr("fusion.ai.httpx.Client",Client);monkeypatch.setenv("FIXTURE_AI",secret)
    events=[];mutations=[]
    AIClient().complete({**DEFAULTS["ai"],"key_env":"FIXTURE_AI","output_token_policy":"fixed"},"test",[],[function("save","test")],lambda n,a:mutations.append(a) or a,event=events.append)
    assert mutations and any(e["state"]=="model_running" for e in events)
    assert secret not in json.dumps(events+mutations)


def test_protection_rechecks_configured_quote_age_after_storage(configured,monkeypatch):
    _,fake,_=configured
    clock=[100.0];sent=[]
    module=SimpleNamespace(TRADE_ACTION_SLTP=6,POSITION_TYPE_BUY=0,
        symbol_info_tick=lambda _:SimpleNamespace(bid=2003,ask=2003.1,time=100),
        symbol_info=lambda _:SimpleNamespace(trade_tick_size=.01,point=.01,digits=2,trade_stops_level=0,trade_freeze_level=0),
        order_send=lambda r:sent.append(r))
    broker=MT5Broker(module);broker.connect=lambda:None;broker.status=fake.status;broker.account_pin=(123,"Demo")
    broker.positions=lambda:[{"ticket":9,"magic":1,"symbol":"XAUUSD","type":0,"sl":1998,"tp":2005}]
    monkeypatch.setattr("fusion.broker.time.time",lambda:clock[0])
    request=broker.prepare_protection(9,2001,2005,1,2)
    clock[0]=104
    with pytest.raises(OrderNotSubmitted):broker.send_order(request)
    assert not sent


def test_event_arriving_during_persistence_blocks_submission(configured):
    store,broker,engine=configured
    cfg=store.active()[0];cfg["workflow"]["mode"]="demo";engine.arm(123)
    context=copy.deepcopy(cfg["context"])
    engine.background=SimpleNamespace(current=lambda *a:copy.deepcopy(context))
    signal,_=form_plan(broker.data,analyze(broker.data,cfg["strategy"]),cfg,[],0)
    original=store.intent
    def persist_then_event(*args):
        original(*args)
        context["events"]=[{"title":"fresh event","currency":"USD","impact":"high","time_utc":time.time()}]
    store.intent=persist_then_event
    result=engine._execute("new-event",signal,{},cfg,[],time.monotonic())
    assert result["status"]=="blocked" and not broker.sent and not store.unresolved()


def test_stop_invalidates_inflight_manager_generation(configured,tmp_path):
    manager=EAManager(configured[2],tmp_path)
    configured[2].ea_manager=manager
    generation=configured[2].generation
    configured[2].stop()
    assert manager.cancel.is_set()
    with pytest.raises(ValueError,match="停止"):
        manager.propose({"bollinger_period":22},"does-not-matter","change",generation)


def test_stale_queued_proposal_never_applies(configured,tmp_path):
    store,broker,engine=configured;manager=EAManager(engine,tmp_path)
    row=next(r for r in store.configs() if r["category"]=="workflow" and r["active"])
    store.save("workflow",row["id"],row["name"],{**row["data"],"module":"ea"},row["version"])
    cfg,versions=store.active()
    store.put("ea_proposal",{"id":"old","status":"queued","patch":{"bollinger_period":22},"created_at":time.time()-7200,"versions":versions,"generation":engine.generation})
    engine.running=True
    manager.apply_pending(engine.generation)
    engine.running=False
    assert store.get("ea_proposal")["status"]=="expired"
    assert store.active()[1]["ea"]==versions["ea"]


def test_slow_manager_does_not_hold_trading_cycle(configured,tmp_path):
    store,broker,engine=configured
    cfg=store.active()[0]
    row=next(r for r in store.configs() if r["category"]=="workflow" and r["active"])
    store.save("workflow",row["id"],row["name"],{**row["data"],"module":"ea"},row["version"])
    rows=broker.data["frames"]["M5"]
    broker.data["frames"]={tf:[{**b,"time":int(time.time()//seconds)*seconds-(len(rows)-i)*seconds} for i,b in enumerate(rows)] for tf,seconds in TF_SECONDS.items() if tf in {"M1","H1","D1"}}
    manager=EAManager(engine,tmp_path);engine.ea_manager=manager
    entered,released=threading.Event(),threading.Event()
    def slow_review(_):entered.set();assert released.wait(5);return {"ok":True}
    manager.review=slow_review
    manager.start_job("review");assert entered.wait(1)
    try:
        began=time.monotonic();result=engine.cycle()
        assert time.monotonic()-began<2 and result["status"]!="error" and manager.busy
    finally:
        released.set();manager.review_thread.join(2)


def test_ai_pipeline_and_decision_survive_ea_cycles_and_restart(configured):
    store,broker,engine=configured
    original=engine.cycle()
    assert original["module"]=="ai"
    ai_pipeline=engine.pipeline_status("ai")
    row=next(r for r in store.configs() if r["category"]=="workflow" and r["active"])
    store.save("workflow",row["id"],row["name"],{**row["data"],"module":"ea"},row["version"])
    rows=broker.data["frames"]["M5"]
    broker.data["frames"]={tf:[{**b,"time":int(time.time()//seconds)*seconds-(len(rows)-i)*seconds} for i,b in enumerate(rows)] for tf,seconds in TF_SECONDS.items() if tf in {"M1","H1","D1"}}
    current=engine.cycle()
    assert current["module"]=="ea"
    assert engine.pipeline_status("ai")==ai_pipeline
    assert engine.status()["latest_ai"]["id"]==original["id"]
    assert engine.status()["latest_ea"]["id"]==current["id"]
    from fusion.engine import Engine
    assert Engine(store,broker).pipeline_status("ai")==ai_pipeline


def test_ask_agent_reinvokes_only_completed_ancestor(configured):
    store,broker,engine=configured
    class Probe(AIClient):
        def __init__(self): super().__init__();self.calls=[]
        def opinion(self,role,config,prompt,payload,kind="opinion",**kwargs):
            self.calls.append((role,payload))
            if role=="context":
                answer=kwargs["dispatch"]("ask_agent",{"agent_id":"market","question":"确认 ATR"})
                assert answer["consultation_only"] and answer["answer"]["summary"]=="ATR evidence"
                with pytest.raises(ValueError):kwargs["dispatch"]("ask_agent",{"agent_id":"context","question":"self"})
                with pytest.raises(ValueError):kwargs["dispatch"]("ask_agent",{"agent_id":"risk","question":"future"})
            return {"role":role,"status":"ok","decision":"abstain","summary":"ATR evidence","confidence":0}
    from fusion.prompts import staged_logic, v2_prompts
    logic=staged_logic();logic["nodes"]=logic["nodes"][:2];logic["topology"]="signal_review";logic["decision_node"]="context"
    probe=Probe()
    result=probe.run(DEFAULTS["ai"],v2_prompts(),broker.data,{},DEFAULTS["context"],logic=logic,memory_scope="experiment")
    assert [role for role,_ in probe.calls]==["market","context","market"]
    assert probe.calls[-1][1]["question_from"]=="context"
    assert [r["role"] for r in result]==["market","context"]


@pytest.mark.parametrize("known",[True,False])
def test_protection_distinguishes_no_send_from_ambiguous_send(configured,monkeypatch,known):
    store,broker,engine=configured;cfg=store.active()[0];engine.arm(123)
    broker.open_positions=[{"ticket":42,"magic":cfg["risk"]["magic"],"symbol":"XAUUSD","type":0,"price_open":2000,"sl":1998,"tp":2050}]
    store.put("position_plans",{"123|Demo|42":{"module":"ea","initial_sl":1998,"strategy":cfg["strategy"]}})
    monkeypatch.setattr("fusion.engine.trailing_levels",lambda *a:(2001,2050))
    broker.prepare_protection=lambda *a:{"action":6,"position":42,"sl":2001,"tp":2050,"magic":cfg["risk"]["magic"]}
    def fail(_):raise OrderNotSubmitted("stale before send") if known else BrokerError("no response after send")
    broker.send_order=fail
    engine._manage_trailing(broker.data,cfg)
    assert len(store.unresolved())==(0 if known else 1)
    assert engine.armed==known


def test_ea_failure_before_snapshot_does_not_replace_ai_decision(configured):
    store,broker,engine=configured
    old=engine.cycle()
    row=next(r for r in store.configs() if r["category"]=="workflow" and r["active"])
    store.save("workflow",row["id"],row["name"],{**row["data"],"module":"ea"},row["version"])
    def failure(_):raise BrokerError("connection lost while applying proposal")
    engine.ea_manager=SimpleNamespace(apply_pending=failure)
    current=engine.cycle()
    assert current["status"]=="error" and current["module"]=="ea"
    assert store.latest_cycle("ai")["id"]==old["id"]


def test_waiting_for_next_bar_preserves_completed_pipeline(configured):
    engine=configured[2]
    engine.cycle(scheduled=True)
    old=engine.pipeline_status("ai")
    assert engine.cycle(scheduled=True)["status"]=="unchanged"
    assert engine.pipeline_status("ai")==old
