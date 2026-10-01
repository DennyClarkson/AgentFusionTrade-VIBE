"""Loopback application server. Mutation token + origin/host checks, no permissive CORS."""
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError, Field
from typing import Literal
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .broker import MT5Broker
from .config import MODELS
from .engine import Engine
from .store import Store, Conflict
from . import ea
from .research import ResearchService
from .agent_lab import AgentLab, logic_presets
from .background import BackgroundService
from .ea_manager import EAManager
from .prompts import v2_prompts, staged_logic

ROOT = Path(__file__).resolve().parent.parent


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    data: dict
    expected_version: int


class ArmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_login: int


class ModuleInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    module: Literal["ai","ea"]


class ConversationInput(BaseModel):
    title: str = Field(default="EA 策略讨论", min_length=1, max_length=100)


class ChatInput(BaseModel):
    message: str = Field(min_length=1,max_length=8000)


def create_app(store=None, broker=None, ai=None):
    store = store or Store(Path(os.environ.get("FUSION_DATA_DIR", ROOT / "data")) / "fusion.sqlite3")
    existing_logic={r["id"] for r in store.configs() if r["category"]=="logic"}
    names={"signal-review":"策略提案审议对照","committee":"多空委员会 + 独立风控AI","ea-advisor":"EA 参数顾问"}
    for profile,data in logic_presets().items():
        if profile not in existing_logic:
            store.save("logic",profile,names[profile],data,0)
    engine = Engine(store, broker or MT5Broker(), ai)
    background = BackgroundService(store,engine.broker)
    engine.background = background
    manager = EAManager(engine,ROOT/"artifacts")
    engine.ea_manager = manager
    # Explicit new profiles preserve all previous prompts/graphs in their histories.
    if not store.get("v2_profiles_seeded"):
        for category,profile,name,data in [("prompts","staged-v2","分工与证据 v2",v2_prompts()),("logic","staged-v2","行情 → 背景 → 研判 → 风控",staged_logic())]:
            if not any(r["category"]==category and r["id"]==profile for r in store.configs()): store.save(category,profile,name,data,0)
        if not store.unresolved() and not store.get("paper_positions",[]) and not any(store.get("managed_demo_accounts",{}).values()):
            store.activate("prompts","staged-v2"); store.activate("logic","staged-v2")
            row=next(r for r in store.configs() if r["category"]=="ai" and r["active"])
            data={**row["data"],"max_tokens":max(6000,row["data"]["max_tokens"]),"timeout_seconds":max(60,row["data"]["timeout_seconds"])}
            if data!=row["data"]: store.save("ai",row["id"],row["name"],data,row["version"])
        store.put("v2_profiles_seeded",True)
    research = ResearchService(engine, ROOT / "artifacts")
    agent_lab = AgentLab(engine)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if isinstance(engine.broker,MT5Broker):
            engine.ea_controller.pause("Python 应用重启；等待用户重新启动")
            engine.ea_controller.start_watchdog()
            background.start()
        yield
        engine.stop()
        engine.ea_controller.shutdown()
        manager.stop()
        background.stop()
        engine.broker.shutdown()

    app = FastAPI(title="AgentTradeFusion", version="0.2.0", lifespan=lifespan)
    app.state.engine = engine
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:8787", "http://localhost:8787", "http://testserver"}:
            return JSONResponse({"detail": "跨来源访问被拒绝"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("x-fusion-token", ""), token):
                return JSONResponse({"detail": "缺少有效操作令牌，请刷新工作台"}, status_code=403)
            if int(request.headers.get("content-length", "0")) > 2_000_000:
                return JSONResponse({"detail": "请求内容过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        # Do not echo submitted values, which may accidentally contain secrets.
        if isinstance(exc, ValidationError):
            detail = "; ".join(".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors(include_input=False))
        else:
            detail = str(exc)
        return JSONResponse({"detail": detail}, status_code=422)

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"token": token, "configs": store.configs(), "schemas": {k: v.model_json_schema() for k, v in MODELS.items()}}

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.get("/api/pipeline")
    def pipeline():
        return engine.pipeline_status("ai")

    @app.post("/api/workspace/select")
    def select_module(value: ModuleInput):
        with engine.control:
            engine.mutation_allowed()
            row=next(r for r in store.configs() if r["category"]=="workflow" and r["active"])
            saved=store.save("workflow",row["id"],row["name"],{**row["data"],"module":value.module},row["version"])
            engine.market=None
            return saved

    @app.get("/api/background")
    def get_background():
        return background.current()

    @app.post("/api/background/refresh")
    def refresh_background():
        return background.refresh(force=True)

    @app.get("/api/ea/workspace")
    def ea_workspace():
        return manager.workspace()

    @app.post("/api/ea/conversations")
    def new_conversation(value: ConversationInput):
        return store.new_conversation(value.title)

    @app.get("/api/ea/conversations/{conversation_id}")
    def conversation(conversation_id: str):
        return store.conversation(conversation_id)

    @app.post("/api/ea/conversations/{conversation_id}/messages")
    def chat(conversation_id: str,value: ChatInput):
        store.conversation(conversation_id)
        return manager.start_job("chat",conversation_id,value.message)

    @app.get("/api/ea/jobs/{job_id}")
    def ea_job(job_id: str):
        return manager.job(job_id)

    @app.post("/api/ea/review")
    def review_ea():
        return manager.start_job("review")

    @app.post("/api/ea/backtest")
    def ea_backtest():
        return manager.start_job("backtest")

    @app.post("/api/ea/parameters/export")
    def export_ea_strategy():
        with engine.control:
            engine.mutation_allowed()
            cfg,_=store.active()
            engine.broker.configure(cfg["broker"])
            return engine.ea_controller.export(cfg)

    @app.get("/api/configs")
    def configs():
        return store.configs()

    @app.put("/api/configs/{category}/{profile_id}")
    def save(category: str, profile_id: str, value: ProfileInput):
        with engine.control:
            engine.mutation_allowed()
            saved = store.save(category, profile_id, value.name, value.data, value.expected_version)
            engine.market = None
            return saved

    @app.post("/api/configs/{category}/{profile_id}/activate")
    def activate(category: str, profile_id: str):
        with engine.control:
            engine.mutation_allowed()
            saved = store.activate(category, profile_id)
            engine.market = None
            return saved

    @app.get("/api/configs/{category}/{profile_id}/history")
    def history(category: str, profile_id: str):
        return store.history(category, profile_id)

    @app.get("/api/cycles")
    def cycles(limit: int = 30):
        return store.cycles(limit)

    @app.get("/api/audit")
    def audit(limit: int = 50):
        return store.audits(limit)

    @app.get("/api/paper/trades")
    def paper_trades():
        return store.get("paper_trades", [])

    @app.post("/api/cycle")
    def cycle():
        return engine.cycle()

    @app.post("/api/shadow-cycle")
    def shadow_cycle():
        return engine.cycle(expected_mode="shadow")

    @app.get("/api/market")
    def market():
        return engine.broker.snapshot(engine.configs()[0]["strategy"])

    @app.post("/api/engine/start")
    def start():
        engine.start()
        return engine.status()

    @app.post("/api/engine/stop")
    def stop():
        engine.stop()
        return engine.status()

    @app.post("/api/engine/arm")
    def arm(value: ArmInput):
        engine.arm(value.account_login)
        return engine.status()

    @app.post("/api/engine/disarm")
    def disarm():
        engine.disarm()
        return engine.status()

    @app.post("/api/positions/{ticket}/close")
    def close(ticket: int):
        return engine.close_demo(ticket)

    @app.post("/api/orders/reconcile")
    def reconcile():
        return engine.reconcile()

    @app.post("/api/paper/close")
    def close_paper():
        engine.close_paper()
        return {"ok": True}

    @app.post("/api/ai/test")
    def test_ai():
        return engine.ai.test(engine.configs()[0]["ai"])

    @app.post("/api/backtest")
    def backtest():
        return engine.backtest()

    @app.get("/api/backtest")
    def last_backtest():
        return store.get("last_backtest")

    @app.get("/api/research")
    def research_status():
        return research.job

    @app.post("/api/research/start")
    def research_start():
        return research.start()

    @app.post("/api/agents/compare")
    def compare_agents():
        return agent_lab.compare()

    @app.post("/api/agents/replay")
    def replay_agents():
        return agent_lab.replay()

    @app.get("/api/agents/results")
    def agent_results():
        return {"compare":store.get("agent_lab_compare"),"replay":store.get("agent_lab_replay")}

    @app.get("/api/integrations")
    def integrations():
        return {"mcp": {"transport": "stdio", "entry": "python -m fusion.mcp_server", "status": "available", "requires": "本地工作台已启动"}, "ea": {"source": "integrations/mt5/FusionExecutor.mq5", "status": "native_executor", "telemetry": engine.ea_controller.status(), "purpose": "MT5 EA 独立交易及保护；AI 管理参数、复盘和开仓许可", "companion": ea.telemetry(engine.broker)}, "context": background.current()}

    @app.post("/api/ea/export")
    def export_ea():
        cfg, _ = engine.configs()
        result = ea.export_parameters(engine.broker, cfg["strategy"], cfg["workflow"])
        store.audit("ea.parameters_exported", result)
        return result

    @app.post("/api/ea/generate")
    def generate_ea():
        cfg,_=engine.configs()
        result=ea.generate_executor(cfg["ea"]["strategy"],ROOT/"artifacts/generated-ea") if cfg["workflow"]["module"]=="ea" else ea.generate_companion(cfg["strategy"],cfg["workflow"],ROOT/"artifacts/generated-ea")
        store.audit("ea.source_generated",result)
        return result

    @app.post("/api/ea/import-context")
    def import_context():
        with engine.control:
            engine.mutation_allowed()
            cfg, _ = engine.configs()
            data = ea.import_calendar(engine.broker, cfg["context"])
            row = next(x for x in store.configs() if x["category"] == "context" and x["active"])
            return store.save("context", row["id"], row["name"], data, row["version"])

    @app.get("/")
    def index():
        return FileResponse(ROOT / "fusion" / "static" / "index.html")

    static = ROOT / "fusion" / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
