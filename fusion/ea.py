"""Legacy telemetry companion and fixed native-EA source export. Control in ea_control."""
import csv
import io
import os
import time
from pathlib import Path

from .config import Context, Strategy


def bridge_path(broker):
    with broker.lock:
        broker.connect()
        info = broker.m.terminal_info()
        if info is None:
            raise ValueError("MT5 终端路径不可用")
        return Path(info.commondata_path) / "Files" / "AgentTradeFusion"


def telemetry(broker):
    try:
        root = bridge_path(broker)
        path = root / "telemetry.csv"
        if not path.exists():
            return {"connected": False, "reason": "尚未在 MT5 图表加载 FusionBridge EA"}
        age = time.time()-path.stat().st_mtime
        with path.open(encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f, delimiter=";"))
        if row.get("protocol") not in {"FUSION2","FUSION3"}:
            raise ValueError("未知协议")
        account=broker.status()["account"]
        same_account=int(row["login"])==account.get("login") and row["server"]==account.get("server")
        return {"connected": -5 <= age < 30 and same_account, "age_seconds": round(age, 1), "revision": int(row["revision"]), "symbol": row["symbol"], "bid": float(row["bid"]), "ask": float(row["ask"]), "raw_tick_time": int(row["raw_tick_time"]), "read_only": True,"same_account":same_account,"ema_fast":float(row["ema_fast"]),"ema_slow":float(row["ema_slow"]),"atr":float(row["atr"]),"stop_atr":float(row["stop_atr"]),"reward_risk":float(row["reward_risk"]),"protocol":row["protocol"],"strategy_revision":int(row.get("strategy_revision",0)),"signal":row.get("signal","unavailable"),"reason":row.get("signal_reason","") if age<30 else "EA 遥测已过期"}
    except Exception as exc:
        return {"connected": False, "reason": type(exc).__name__}


def export_parameters(broker, strategy, workflow):
    root = bridge_path(broker)
    root.mkdir(parents=True, exist_ok=True)
    revision = time.time_ns()
    content = io.StringIO(newline="")
    strategy=Strategy.model_validate(strategy).model_dump()
    csv.writer(content, delimiter=";").writerow(["FUSION2", revision, strategy["symbol"], min(300, workflow["poll_seconds"]),strategy["timeframe"],strategy["fast_ema"],strategy["slow_ema"],strategy["atr_period"],strategy["stop_atr"],strategy["reward_risk"]])
    temp = root / "parameters.tmp"
    temp.write_text(content.getvalue(), encoding="utf-8")
    os.replace(temp, root / "parameters.csv")
    return {"ok": True, "revision": revision, "path": str(root / "parameters.csv"), "note": "参数已写入；EA 加载后通过 telemetry revision 确认应用"}


def import_calendar(broker, context):
    path = bridge_path(broker) / "calendar.csv"
    if not path.exists() or not 0 <= time.time()-path.stat().st_mtime <= 600:
        raise ValueError("没有 10 分钟内的 EA 日历导出；请先在图表加载 FusionBridge")
    with path.open(encoding="utf-8-sig", newline="") as f:
        records = list(csv.DictReader(f, delimiter=";"))
    if not telemetry(broker).get("connected"): raise ValueError("EA 遥测未连接到当前账户或已过期")
    # Calendar uses trade-server time, independently from Python quote timestamp anomalies.
    events = [{"title": row["title"], "time_utc": float(row["time_utc"]), "impact": row["impact"], "currency": row["currency"],"source":"MT5 Economic Calendar / FusionBridge"} for row in records]
    return Context.model_validate({**context, "events": events, "updated_at": time.time(),"coverage_note":"MT5 EA USD 经济日历（前1小时至后24小时）；新闻仍需单独提供，日历完整性依赖终端。"}).model_dump()


def export_strategy(broker,strategy):
    s=Strategy.model_validate(strategy).model_dump()
    if s["algorithm"]!="bollinger_pullback" or s["timeframe"]!="M1": raise ValueError("需要 M1 布林顺势策略")
    root=bridge_path(broker);root.mkdir(parents=True,exist_ok=True)
    revision=time.time_ns()//1000  # Microseconds fit signed MQL5 long and JS safe integer.
    account=broker.status()["account"]
    content=io.StringIO(newline="")
    fields=["FUSION_EA1",revision,account.get("login",0),account.get("server",""),s["symbol"],s["fast_ema"],s["slow_ema"],s["atr_period"],s["bollinger_period"],s["bollinger_deviation"],s["min_trend_atr"],s["trend_adx"],s["trend_slope_atr"],s["range_bias"],s["stop_atr"],s["reward_risk"],s["trailing_start_r"],s["trailing_atr"],s["breakeven_r"],s["max_hold_minutes"]]
    csv.writer(content,delimiter=";").writerow(fields)
    temp=root/"strategy.tmp";temp.write_text(content.getvalue(),encoding="utf-8");os.replace(temp,root/"strategy.csv")
    return {"ok":True,"revision":revision,"path":str(root/"strategy.csv"),"requires_protocol":"FUSION3","note":"等待 EA strategy_revision 确认；下单与保护仍由 Python 网关执行"}


def generate_companion(strategy,workflow,output_dir):
    """Generate inspectable MQL5 source from a fixed template and typed values only."""
    s=Strategy.model_validate(strategy).model_dump()
    source=(Path(__file__).resolve().parents[1]/"integrations/mt5/FusionBridge.mq5").read_text(encoding="utf-8")
    replacements={
        'input string MonitorSymbol = "XAUUSD";':f'input string MonitorSymbol = "{s["symbol"]}";',
        'input int PollSeconds = 5;':f'input int PollSeconds = {min(300,workflow["poll_seconds"])};',
        'input ENUM_TIMEFRAMES AnalysisTimeframe = PERIOD_M5;':f'input ENUM_TIMEFRAMES AnalysisTimeframe = PERIOD_{s["timeframe"]};',
        'input int FastEMA = 20;':f'input int FastEMA = {s["fast_ema"]};',
        'input int SlowEMA = 50;':f'input int SlowEMA = {s["slow_ema"]};',
        'input int ATRPeriod = 14;':f'input int ATRPeriod = {s["atr_period"]};',
        'input double StopATR = 1.5;':f'input double StopATR = {s["stop_atr"]};',
        'input double RewardRisk = 1.5;':f'input double RewardRisk = {s["reward_risk"]};',
    }
    for old,new in replacements.items():
        if old not in source: raise ValueError("EA 模板版本不匹配")
        source=source.replace(old,new,1)
    directory=Path(output_dir);directory.mkdir(parents=True,exist_ok=True)
    target=directory/f"FusionBridge_{time.time_ns()}.mq5"
    target.write_text(source,encoding="utf-8")
    return {"ok":True,"path":str(target),"note":"已生成可审阅 MQL5 源码：行情/日历、M1 布林顺势信号与参数确认。执行由 Python 网关负责；生成文件需编译挂载。"}


def generate_executor(strategy, output_dir):
    s = Strategy.model_validate(strategy).model_dump()
    if s["algorithm"] != "bollinger_pullback" or s["timeframe"] != "M1":
        raise ValueError("原生 EA 使用 M1 顺势布林策略")
    source_dir = Path(__file__).resolve().parents[1]/"integrations/mt5"
    target_dir = Path(output_dir)/str(time.time_ns())
    target_dir.mkdir(parents=True, exist_ok=True)
    source = (source_dir/"FusionExecutor.mq5").read_text(encoding="utf-8")
    source = source.replace('input string MonitorSymbol="XAUUSD";', f'input string MonitorSymbol="{s["symbol"]}";')
    (target_dir/"FusionExecutor.mq5").write_text(source, encoding="utf-8")
    (target_dir/"FusionKernel.mqh").write_text((source_dir/"FusionKernel.mqh").read_text(encoding="utf-8"), encoding="utf-8")
    return {"ok": True, "path": str(target_dir/"FusionExecutor.mq5"), "include": str(target_dir/"FusionKernel.mqh"), "note": "固定原生 EA 源码与指标内核已导出；编译挂载后，由参数配置和当前会话许可控制。生成不等于部署或启动。"}
