import copy
import time
from types import SimpleNamespace

import pytest

from fusion.config import DEFAULTS
from fusion.ea_control import EAController, configuration, read_packet, write_packet
from fusion.ea_manager import EAManager
from fusion.store import Conflict


def select(store, category, **changes):
    row=next(r for r in store.configs() if r["active"] and r["category"]==category)
    return store.save(category,row["id"],row["name"],{**row["data"],**changes},row["version"])


@pytest.fixture
def native(configured,tmp_path,monkeypatch):
    store,broker,engine=configured
    controller=engine.ea_controller
    broker.m=SimpleNamespace()
    monkeypatch.setattr(controller,"root",lambda:tmp_path)
    select(store,"workflow",module="ea",mode="demo")
    state={"connected":True,"instance_id":"boot-A","symbol":"XAUUSD","entries_enabled":False,"pending":False,"positions":0,"command_revision":0,"strategy_revision":0,"signal":"HOLD","reason":"paused"}
    monkeypatch.setattr(controller,"status",lambda:copy.deepcopy(state))
    return store,broker,engine,controller,state,tmp_path


def test_packet_rejects_partial_duplicate_and_unsafe_fields(tmp_path):
    path=tmp_path/"packet.csv"
    write_packet(tmp_path,path.name,{"x":1,"y":"ok"})
    assert read_packet(path)=={"x":"1","y":"ok"}
    path.write_text("x;1\nx;2\nEND;2\n")
    with pytest.raises(ValueError):read_packet(path)
    path.write_text("x;1\n")
    with pytest.raises(ValueError):read_packet(path)
    with pytest.raises(ValueError):write_packet(tmp_path,path.name,{"x":"a;b"})


def test_envelope_demo_only_distinct_magic_and_all_sessions(configured):
    store,broker,_=configured;cfg=store.active()[0]
    result=configuration(cfg,broker.status()["account"])
    assert len([k for k in result if k.startswith("hour_")])==72
    assert result["revision"]<2**53
    cfg["ea"]["execution_magic"]=cfg["risk"]["magic"]
    with pytest.raises(ValueError,match="Magic"):configuration(cfg,broker.status()["account"])
    cfg=store.active()[0]
    with pytest.raises(ValueError,match="模拟"):configuration(cfg,{**broker.status()["account"],"demo":False})


def test_start_canonicalizes_enriched_background_and_does_not_enable(native):
    store,broker,engine,controller,state,path=native
    engine.background=SimpleNamespace(current=lambda base:{**base,"providers":[],"calendar_fresh":True})
    cfg,_=engine.configs()
    controller.start(cfg)
    packet=read_packet(path/"execution.csv")
    assert int(packet["revision"])==configuration(store.active()[0],broker.status()["account"])["revision"]
    assert read_packet(path/"control.csv")["enabled"]=="0"
    assert not controller.decision["enabled"]
    assert controller.decision["expires_at"] == 0  # pending is not an AI decision with a future TTL


def ready(native):
    store,broker,engine,controller,state,path=native
    controller.start(store.active()[0]);state["command_revision"]=controller.last_command["revision"]
    state["strategy_revision"]=store.get("ea_native_export")["revision"]
    engine.running=engine.armed=True;broker.account_pin=(broker.login,"Demo")
    return native


def test_ai_cannot_enable_stopped_session_or_old_generation(native):
    store,broker,engine,controller,state,path=native
    with pytest.raises(Conflict):controller.decide(True,"evidence",10,.5,engine.generation)
    ready(native)
    with pytest.raises(Conflict):controller.decide(True,"stale",10,.5,engine.generation-1)
    with pytest.raises(ValueError):controller.decide(True,"risk escalation",10,1.1,engine.generation)
    controller.decide(True,"fresh market and background",10,.5,engine.generation)
    command=read_packet(path/"control.csv")
    assert command["enabled"]=="1" and float(command["risk_scale"])==.5 and command["instance_id"]=="boot-A"
    assert not broker.sent


def test_parameter_ack_mismatch_never_enables(native):
    store,broker,engine,controller,state,path=ready(native)
    state["strategy_revision"]+=1
    controller.decide(True,"enable requested",5,1,engine.generation)
    assert read_packet(path/"control.csv")["enabled"]=="0"
    assert not broker.sent


def test_lease_never_outlives_ai_decision_and_restart_does_not_resume(native):
    store,broker,engine,controller,state,path=ready(native)
    controller.decision={"enabled":True,"reason":"bounded","expires_at":time.time()+5,"risk_scale":1}
    controller.tick(store.active()[0])
    assert int(read_packet(path/"control.csv")["valid_until"])<=controller.decision["expires_at"]
    new=EAController(engine)
    assert not new.decision["enabled"]


def test_expired_native_lease_is_not_permission_for_python_takeover(native):
    store,broker,engine,controller,state,path=ready(native)
    controller.decide(True,"test",5,1,engine.generation)
    state["connected"]=False
    store.put("ea_native_command",{**controller.last_command,"valid_until":time.time()-1000})
    with pytest.raises(Conflict,match="恢复连接"):controller.require_paused()
    state.update(connected=True,entries_enabled=False,command_revision=controller.last_command["revision"])
    controller.require_paused()
    assert not store.get("ea_native_release_required")


def test_pause_ack_and_flat_required_before_handoff(native):
    store,broker,engine,controller,state,path=ready(native)
    controller.decide(True,"test",5,1,engine.generation)
    controller.pause()
    with pytest.raises(Conflict,match="回执"):controller.require_paused()
    state["command_revision"]=controller.last_command["revision"]
    state["positions"]=1
    with pytest.raises(Conflict,match="保护"):controller.require_paused()
    state.update(positions=0,pending=True)
    with pytest.raises(Conflict,match="未确认"):controller.require_paused()


def test_manual_close_is_not_overwritten_by_supervisor_tick(native):
    store,broker,engine,controller,state,path=ready(native)
    broker.open_positions=[{"ticket":42,"magic":store.active()[0]["ea"]["execution_magic"]}]
    controller.close(42);command=read_packet(path/"control.csv")
    controller.tick(store.active()[0])
    assert read_packet(path/"control.csv")==command and command["close_ticket"]=="42"
    assert not broker.sent


def test_native_cycle_never_calls_python_entry_exit_or_trailing(native,monkeypatch):
    store,broker,engine,controller,state,path=ready(native)
    def forbidden(*a,**kw):raise AssertionError("Python execution forbidden in native module")
    for name in ("_execute","_close_demo","_manage_trailing","_paper_manage"):
        monkeypatch.setattr(engine,name,forbidden)
    broker.snapshot=forbidden;broker.send_order=forbidden
    result=engine.cycle()
    assert result["status"]=="native_monitor" and result["executor"]=="MT5 EA"


def test_native_parameter_publication_is_not_application(native):
    store,broker,engine,controller,state,path=ready(native)
    manager=EAManager(engine,path);engine.ea_manager=manager
    cfg,versions=store.active()
    store.put("ea_proposal",{"id":"validated","status":"queued","patch":{"bollinger_period":22},"created_at":time.time(),"versions":versions,"generation":engine.generation})
    manager.apply_pending(engine.generation)
    assert store.get("ea_proposal")["status"]=="pausing"
    state["command_revision"]=controller.last_command["revision"]
    manager.apply_pending(engine.generation)
    proposal=store.get("ea_proposal")
    assert proposal["status"]=="published" and store.active()[0]["ea"]["strategy"]["bollinger_period"]==20
    manager.apply_pending(engine.generation)
    assert store.get("ea_proposal")["status"]=="published"
    state["strategy_revision"]=proposal["bridge_export"]["revision"]
    manager.apply_pending(engine.generation)
    assert store.get("ea_proposal")["status"]=="applied"
    assert store.active()[0]["ea"]["strategy"]["bollinger_period"]==22


def test_stop_invalidates_ai_permission_and_requests_native_pause(native):
    store,broker,engine,controller,state,path=ready(native)
    controller.decide(True,"test",5,1,engine.generation)
    generation=engine.generation
    engine.stop()
    assert engine.generation>generation and not engine.armed and not engine.running
    assert read_packet(path/"control.csv")["enabled"]=="0"
    assert not controller.decision["enabled"]


def test_new_ea_boot_stops_session_instead_of_renewing_old_ai_permission(native):
    store,broker,engine,controller,state,path=ready(native)
    controller.decide(True,"old permission",5,1,engine.generation)
    state["instance_id"]="boot-B"
    controller.tick(store.active()[0])
    assert not engine.running and not engine.armed and not controller.decision["enabled"]
    command=read_packet(path/"control.csv")
    assert command["instance_id"]=="boot-B" and command["enabled"]=="0"


def test_idle_watchdog_binds_new_boot_paused(native):
    store,broker,engine,controller,state,path=native
    controller.start_watchdog()
    try:
        deadline=time.monotonic()+3
        while not (path/"control.csv").exists() and time.monotonic()<deadline: time.sleep(.01)
        controller.shutdown()  # wait until atomic publication and audit bookkeeping finish
        command=read_packet(path/"control.csv")
        assert command["enabled"]=="0" and command["instance_id"]=="boot-A"
    finally:
        controller.shutdown()
