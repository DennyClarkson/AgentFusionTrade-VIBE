import copy
import time

import pytest
from pydantic import ValidationError

from fusion.ai import AIClient, Opinion
from fusion.agent_lab import AgentLab, logic_presets, simulate_plan
from fusion.config import Logic, Risk
from fusion.planning import adaptive_policy, event_policy, form_plan, graph_gate
from fusion.strategy import analyze


def committee(cfg):
    cfg["logic"]=logic_presets()["committee"]
    agents=[{"role":n["id"],"status":"ok","decision":"approve","confidence":.9,"summary":"test"} for n in cfg["logic"]["nodes"]]
    agents[-2]["plan"]={"action":"BUY","stop_atr":1,"reward_risk":2,"max_hold_minutes":60,"valid_for_seconds":120}
    agents[-1]["risk_advice"]={"allow_entries":True,"risk_scale":.5,"stop_atr":2,"reward_risk":1.5,"max_hold_minutes":30}
    return agents


def test_graph_rejects_cycles_missing_nodes_and_wrong_risk_role():
    c=logic_presets()["committee"]
    c["nodes"][0]["depends_on"]=["judge"]
    with pytest.raises(ValidationError): Logic.model_validate(c)
    c=logic_presets()["committee"]
    c["risk_node"]="bull"
    with pytest.raises(ValidationError): Logic.model_validate(c)
    c=logic_presets()["committee"]
    c["nodes"][0]["depends_on"]=["absent"]
    with pytest.raises(ValidationError): Logic.model_validate(c)


def test_committee_can_propose_when_rule_waits_but_risk_recalculates(configured):
    store,broker,engine=configured
    cfg=store.active()[0]; agents=committee(cfg)
    rule=analyze(broker.data,cfg["strategy"])
    rule["action"]="HOLD"
    plan,reasons=form_plan(broker.data,rule,cfg,agents,3)
    assert not reasons and plan["action"]=="BUY" and plan["risk_scale"]==.5
    assert plan["entry"]-plan["sl"]==pytest.approx(plan["atr"]*2)
    assert plan["max_hold_minutes"]==30
    r=engine.guards(broker.data,plan,cfg)
    assert r["risk_money"]<=2.5
    assert not graph_gate(cfg,agents)
    agents[0]["status"]="error"
    assert graph_gate(cfg,agents)
    agents[-1]["risk_advice"]["allow_entries"]=False
    assert form_plan(broker.data,rule,cfg,agents,3)[1]
    agents[-1]["risk_advice"]["risk_scale"]=2
    with pytest.raises(ValidationError): Opinion.model_validate({k:v for k,v in agents[-1].items() if k not in {"role","status"}})


def test_event_risk_phases_and_specific_window(configured):
    cfg=configured[0].active()[0]; c=cfg["context"]; event_time=1800000000
    c["events"]=[{"title":"Employment Situation","time_utc":event_time,"impact":"high","currency":"USD","blackout_before_minutes":30}]
    assert event_policy(event_time-50*60,c)["risk_scale"]==.5
    assert event_policy(event_time-20*60,c)["blackout"]
    assert not event_policy(event_time-20*60,c)["close_existing"]
    assert event_policy(event_time-4*60,c)["close_existing"]
    assert event_policy(event_time+30*60,c)["risk_scale"]==.5
    assert event_policy(event_time+60*60,c)["risk_scale"]==1
    c["events"][0]["currency"]="EUR"
    assert not event_policy(event_time,c)["blackout"]


def test_volatility_reduce_pause_and_schedule_validation():
    risk=Risk().model_dump()
    # 1970-01-01 09:00 UTC is the full-budget European default window.
    assert adaptive_policy(9*3600,risk,1,1)["risk_scale"]==1
    assert adaptive_policy(9*3600,risk,2,1)["risk_scale"]==.5
    assert adaptive_policy(9*3600,risk,3,1)["risk_scale"]==0
    assert adaptive_policy(22*3600,risk,1,1)["risk_scale"]==0
    risk["sessions"][0]["end_utc"]=8
    with pytest.raises(ValidationError): Risk.model_validate(risk)


def test_ai_profile_routing_and_dependencies_are_explicit(configured):
    cfg=configured[0].active()[0]; committee(cfg)
    cfg["logic"]["nodes"][0]["ai_profile"]="research-ai"
    calls=[]
    class Probe(AIClient):
        def opinion(self,role,provider,prompt,facts,output_kind,**runtime):
            calls.append((role,provider["model"],set(facts["upstream"]),output_kind))
            return {"role":role,"status":"ok","decision":"abstain","confidence":0,"summary":"fixture"}
    Probe().run(cfg["ai"],cfg["prompts"],configured[1].data,{},cfg["context"],logic=cfg["logic"],profiles={"research-ai":{**cfg["ai"],"model":"alternate"}})
    assert next(c for c in calls if c[0]=="market")[1]=="alternate"
    assert next(c for c in calls if c[0]=="judge")[2]=={"bull","bear","context"}
    assert next(c for c in calls if c[0]=="risk")[3]=="risk"


def test_lab_failed_dependency_never_produces_approved_scenario(configured):
    store,broker,engine=configured; cfg=store.active()[0]; agents=committee(cfg)
    agents[0]["status"]="error"
    class FixtureAI:
        def run(self,*args): return agents
    engine.ai=FixtureAI()
    r=AgentLab(engine)._analyze(copy.deepcopy(broker.data),cfg,{},engine.generation)
    assert r["risk_reasons"] and not broker.sent
    assert simulate_plan(r,[],cfg,broker.data["spec"])["status"]=="skipped"


def test_minute_replay_cannot_fill_after_short_ai_ttl(configured):
    cfg=configured[0].active()[0]
    result={"as_of":1000,"latency_ms":1000,"risk_reasons":[],"signal":{"action":"BUY","valid_for_seconds":10}}
    assert simulate_plan(result,[{"time":1020}],cfg,{})["status"]=="expired"


def test_order_check_latency_can_expire_plan_before_send(configured,monkeypatch):
    store,broker,engine=configured
    cfg=store.active()[0]; cfg["workflow"]["mode"]="demo"
    engine.arm(123)
    cfg["logic"]["max_plan_age_seconds"]=10
    clock=[101.0]
    monkeypatch.setattr("fusion.engine.time.monotonic",lambda:clock[0])
    signal,_=form_plan(broker.data,analyze(broker.data,cfg["strategy"]),cfg,[],1)
    def delayed_check(request):
        clock[0]=112.0
        return {"retcode":0}
    broker.check_order=delayed_check
    result=engine._execute("ttl-test",signal,{},cfg,[],100.0)
    assert result["status"]=="blocked" and not broker.sent and not store.unresolved()


def test_final_context_frame_change_blocks_order(configured):
    store,broker,engine=configured; cfg=store.active()[0]
    cfg["workflow"]["mode"]="demo"; engine.arm(123)
    signal,_=form_plan(broker.data,analyze(broker.data,cfg["strategy"]),cfg,[],1)
    expected={"M5":signal["bar_time"]-300}
    result=engine._execute("stale-context",signal,{},cfg,[],time.monotonic(),expected)
    assert result["status"]=="blocked" and not broker.sent


def test_storage_delay_cannot_send_an_expired_quote(configured,monkeypatch):
    store,broker,engine=configured; cfg=store.active()[0]
    cfg["workflow"]["mode"]="demo";cfg["risk"]["max_tick_age_seconds"]=2;engine.arm(123)
    signal,_=form_plan(broker.data,analyze(broker.data,cfg["strategy"]),cfg,[],1)
    now=[time.time()]
    monkeypatch.setattr("fusion.engine.time.time",lambda:now[0])
    original=store.intent
    def delayed_intent(*args):
        original(*args)
        now[0]+=3
    store.intent=delayed_intent
    result=engine._execute("slow-store",signal,{},cfg,[],time.monotonic())
    assert result["status"]=="blocked" and not broker.sent and not store.unresolved()
