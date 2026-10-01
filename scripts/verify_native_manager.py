"""Opt-in paid provider probe; isolated store, stopped engine, broker sends forbidden.

Run explicitly from the project root after reviewing scope. Reads current local
configuration/market/background, asks the EA manager to use tools and save a
pause decision. It never arms, starts, installs an EA or publishes a permission.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fusion.broker import MT5Broker
from fusion.engine import Engine
from fusion.ea_manager import EAManager
from fusion.store import Store


class ReadOnlyBroker(MT5Broker):
    def send_order(self, request):
        raise AssertionError("Native-manager probe must never send broker orders")


def main():
    bootstrap=json.load(urllib.request.urlopen("http://127.0.0.1:8787/api/bootstrap"))
    background=json.load(urllib.request.urlopen("http://127.0.0.1:8787/api/background"))
    directory=Path("artifacts/native-manager-probe")/str(time.time_ns())
    store=Store(directory/"isolated.sqlite3")
    for profile in bootstrap["configs"]:
        existing=next((r for r in store.configs() if r["category"]==profile["category"] and r["id"]==profile["id"]),None)
        store.save(profile["category"],profile["id"],profile["name"],profile["data"],existing["version"] if existing else 0)
        if profile["active"]:store.activate(profile["category"],profile["id"])
    broker=ReadOnlyBroker()
    engine=Engine(store,broker)
    engine.background=SimpleNamespace(current=lambda *args:background)
    manager=EAManager(engine,directory)
    engine.ea_manager=manager
    conversation=store.new_conversation("原生 EA 管理工具隔离验证")
    job=manager.start_job("chat",conversation["id"],"这是停止状态下的功能验证：请依次调用 get_market_data、get_background，然后调用 set_entry_permission，enabled=false、minutes=5、risk_scale=0，reason写明功能验证保持暂停。不要回测、调参、开启或解锁交易。最后说明实际工具结果，不把保存决定说成 EA 已收到。")
    manager.review_thread.join(timeout=330)
    if manager.review_thread.is_alive():raise RuntimeError("Probe exceeded bounded wait")
    result=manager.job(job["id"])
    data=result.get("result",{})
    tools=[t.get("name") for t in data.get("tools",[])]
    summary={"job_status":result["status"],"model_status":data.get("status"),"model":data.get("model"),"thinking":data.get("thinking"),"latency_ms":data.get("latency_ms"),"tools":tools,
             "output_token_policy":data.get("output_token_policy"),"max_output_tokens":data.get("max_output_tokens"),"usage":data.get("usage"),"progress_states":sorted({e["state"] for e in result.get("events",[])}),
             "entry_decision":engine.ea_controller.decision,"engine_running":engine.running,"armed":engine.armed,"native_command_published":engine.ea_controller.last_command is not None,
             "scope":"Real provider + read-only MT5 market + isolated stopped controller. Not native execution acceptance."}
    (directory/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False))
    assert data.get("status")=="ok",result.get("error","Model probe failed")
    assert {"get_market_data","get_background","set_entry_permission"}<=set(tools)
    assert not engine.running and not engine.armed and engine.ea_controller.last_command is None
    broker.shutdown()


if __name__=="__main__":main()
