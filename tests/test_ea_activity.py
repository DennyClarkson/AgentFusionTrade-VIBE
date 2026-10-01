import copy
import json
import threading
import time

import pytest

from fusion.ai import AIClient
from fusion.config import DEFAULTS
from fusion.ea_manager import EAManager


def select(store, category, **changes):
    row=next(r for r in store.configs() if r["active"] and r["category"]==category)
    store.save(category,row["id"],row["name"],{**row["data"],**changes},row["version"])


@pytest.mark.parametrize("failure", ["error", "unavailable"])
def test_model_failure_is_not_success_and_persists(configured, tmp_path, failure):
    store,broker,engine=configured
    manager=EAManager(engine,tmp_path)
    manager.client.complete=lambda *a,**kw:{"status":failure,"error":"output exhausted", "tools":[], "model":"reported", "thinking":True}
    launched=manager.start_job("review")
    manager.review_thread.join(3)
    assert not manager.review_thread.is_alive()
    review=manager.workspace()["review"]
    assert not review["busy"] and review["active_job"] is None
    assert review["latest_job"]["status"]=="error" and review["last_error"]=="output exhausted"
    assert launched["conversation_id"]==review["conversation_id"]
    message=store.conversation(launched["conversation_id"])["messages"][-1]
    assert message["status"]==failure and message["model"]=="reported"
    restored=EAManager(engine,tmp_path).workspace()["review"]
    assert restored["latest_job"]["id"]==launched["id"] and restored["last_error"]=="output exhausted"
    assert not broker.sent


def test_running_job_public_progress_and_restart_interruption(configured,tmp_path):
    store,broker,engine=configured
    manager=EAManager(engine,tmp_path)
    entered,released=threading.Event(),threading.Event()
    def complete(*args,**kwargs):
        kwargs["event"]({"state":"model_running","round":1,"thinking":True})
        entered.set()
        assert released.wait(4)
        return {"status":"ok","content":"public summary", "_protocol":[{"reasoning_content":"private reasoning"}]}
    manager.client.complete=complete
    launched=manager.start_job("review")
    try:
        assert entered.wait(2)
        running=manager.workspace()["review"]["active_job"]
        assert running["id"]==launched["id"] and running["conversation_id"]==launched["conversation_id"]
        assert running["events"][-1]["state"]=="model_running" and running["events"][-1]["at"]>0
        # Isolated crash snapshot, not a second worker on the same live store.
        saved=copy.deepcopy(store.get("ea_job:"+launched["id"]))
    finally:
        released.set();manager.review_thread.join(3)
    assert "private reasoning" not in json.dumps(manager.workspace())
    store.put("ea_job:"+launched["id"],saved)
    restored=EAManager(engine,tmp_path).workspace()["review"]
    assert restored["active_job"] is None and restored["latest_job"]["status"]=="interrupted"
    assert not engine.running and not engine.armed and not broker.sent


def test_initial_pending_permission_review_and_intentional_pause_throttle(configured,tmp_path):
    store,broker,engine=configured
    select(store,"workflow",module="ea")
    manager=EAManager(engine,tmp_path)
    engine.running=True
    store.put("ea_last_review",time.time()-120)
    engine.ea_controller.decision={"enabled":False,"expires_at":0}
    calls=[]
    manager.start_job=lambda kind:calls.append(kind)
    manager.maybe_review()
    assert calls==["review"]
    calls.clear()
    store.put("ea_last_review",time.time()-10)
    manager.maybe_review()
    assert not calls  # failure/pending retry is throttled
    assert manager.next_review_at(store.active()[0])>time.time()+40
    engine.ea_controller.decision={"enabled":False,"expires_at":time.time()+600}
    store.put("ea_last_review",time.time()-120)
    manager.maybe_review()
    assert not calls  # an intentional AI pause remains valid


def test_provider_model_limit_cached_and_no_cumulative_token_gate(monkeypatch):
    monkeypatch.setenv("FIXTURE_AI", "fixture-only")
    requests=[];discoveries=[];events=[]
    class Response:
        status_code=200
        def __init__(self,value):self.value=value
        def json(self):return self.value
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):
            discoveries.append(1)
            return Response({"data":[{"id":"deepseek-flash","max_output_tokens":393216}]})
        def post(self,*a,**kw):
            requests.append(copy.deepcopy(kw["json"]))
            return Response({"choices":[{"message":{"content":"ok","reasoning_content":"private"},"finish_reason":"stop"}],"usage":{"total_tokens":500000}})
    monkeypatch.setattr("fusion.ai.httpx.Client",Client)
    cfg={**DEFAULTS["ai"],"key_env":"FIXTURE_AI"}
    client=AIClient()
    for _ in range(2):
        result=client.complete(cfg,"test",[],event=events.append)
        assert result["status"]=="ok" and result["max_output_tokens"]==393216
    assert len(discoveries)==1 and len(requests)==2
    assert all(r["max_tokens"]==393216 for r in requests)
    assert "private" not in json.dumps(events)
    assert client.complete({**cfg,"output_token_policy":"provider_default"},"test",[])["status"]=="ok"
    assert "max_tokens" not in requests[-1]
    client.complete({**cfg,"output_token_policy":"fixed","max_tokens":16384},"test",[])
    assert requests[-1]["max_tokens"]==16384


def test_model_request_activity_does_not_replace_pipeline_node_state(configured):
    engine=configured[2]
    engine.pipeline["nodes"]=[{"id":"market","state":"running"}]
    engine.pipeline_event({"id":"market","state":"model_running","round":1})
    assert engine.pipeline["nodes"][0]["state"]=="running"
    assert engine.pipeline["nodes"][0]["model_activity"]["round"]==1


def test_failed_final_answer_retains_actual_permission_tool_outcome(configured,tmp_path):
    store,broker,engine=configured
    manager=EAManager(engine,tmp_path)
    def complete(*args,**kwargs):
        decision=args[4]("set_entry_permission", {"enabled":False,"reason":"verified pause", "minutes":5,"risk_scale":0})
        trace={"name":"set_entry_permission","result":decision,"duration_ms":1}
        kwargs["event"]({"state":"tool_done",**trace})
        return {"status":"error","error":"final answer truncated","tools":[trace]}
    manager.client.complete=complete
    job=manager.start_job("review");manager.review_thread.join(3)
    assert manager.job(job["id"])["status"]=="error"
    assert engine.ea_controller.decision["reason"]=="verified pause"
    public=manager.workspace()["review"]["latest_job"]
    assert public["events"][0]["result"]["status"]=="decision_saved"
    assert not broker.sent


def test_result_storage_failure_does_not_stick_manager_busy(configured,tmp_path,monkeypatch):
    store,broker,engine=configured
    manager=EAManager(engine,tmp_path)
    original=store.put
    def failing_put(key,value):
        if key.startswith("ea_job:") and value.get("status")!="running": raise OSError("fixture")
        return original(key,value)
    monkeypatch.setattr(store,"put",failing_put)
    manager.review=lambda _: {"status":"ok","content":"done"}
    job=manager.start_job("review");manager.review_thread.join(3)
    assert not manager.busy and manager.active_job_id is None
    assert manager.job(job["id"])["status"]=="error"
    assert "保存失败" in manager.last_error


@pytest.mark.parametrize("http_status",[200,403])
def test_unavailable_model_limit_is_visible_without_post_or_secret(monkeypatch,http_status):
    secret="fixture-model-discovery-secret"
    monkeypatch.setenv("FIXTURE_AI",secret)
    posts=[]
    class Response:
        status_code=http_status
        def json(self):return {"data":[{"id":"deepseek-flash"}],"debug":secret}
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):return Response()
        def post(self,*a,**kw):posts.append(1);raise AssertionError("must not generate")
    monkeypatch.setattr("fusion.ai.httpx.Client",Client)
    result=AIClient().complete({**DEFAULTS["ai"],"key_env":"FIXTURE_AI"},"test",[])
    assert result["status"]=="error" and not posts
    assert "单次上限" in result["error"] and secret not in json.dumps(result)
