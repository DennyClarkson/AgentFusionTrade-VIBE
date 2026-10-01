"""Agent proposals and adaptive risk are data, converted to bounded executable plans."""
import statistics
from datetime import datetime,timezone

from .strategy import feature_series


def graph_gate(cfg, agents):
    if cfg["workflow"]["ai_gate"]!="required" and cfg["logic"]["topology"]!="market_committee": return []
    ids={n["id"] for n in cfg["logic"]["nodes"]}
    judge=next((a for a in agents if a["role"]==cfg["logic"]["decision_node"]),{})
    veto=cfg["logic"]["topology"]=="signal_review" and any(a["decision"]=="veto" for a in agents)
    if len(agents)!=len(ids) or {a["role"] for a in agents}!=ids or any(a["status"]!="ok" for a in agents) or judge.get("decision")!="approve" or judge.get("confidence",0)<cfg["workflow"]["min_confidence"] or veto:
        return ["AI 必需门禁未通过（缺失、否决或置信度不足）"]
    return []


def adaptive_policy(timestamp,risk,atr,baseline):
    hour=datetime.fromtimestamp(timestamp,timezone.utc).hour
    session=next(s for s in risk["sessions"] if s["start_utc"]<=hour<s["end_utc"])
    ratio=atr/baseline if baseline else 1
    scale=0 if ratio>=risk["volatility_pause_ratio"] else .5 if ratio>=risk["volatility_reduce_ratio"] else 1
    return {"session":session["name"],"utc_hour":hour,"atr":atr,"atr_baseline":baseline,"volatility_ratio":ratio,
            "risk_scale":session["risk_scale"]*scale,"stop_scale":session["stop_scale"],"target_scale":session["target_scale"],
            "note":"时段为可配置 UTC 窗口；夏令时需按经纪商／市场调整。事件信息由独立背景输入提供。"}


def event_policy(timestamp, context):
    """Only explicit timestamped events can affect deterministic event guards."""
    matches=[]
    scale,stop,target=1,1,1
    blackout,close_existing=False,False
    for event in context["events"]:
        if event["impact"]!="high" or event["currency"].upper() not in ("USD","ALL","XAU"): continue
        delta=(timestamp-event["time_utc"])/60
        before=event.get("blackout_before_minutes")
        after=event.get("blackout_after_minutes")
        before=context["blackout_before_minutes"] if before is None else before
        after=context["blackout_after_minutes"] if after is None else after
        blocked=-before<=delta<=after
        if -max(context["caution_before_minutes"],before)<=delta<=max(context["caution_after_minutes"],after):
            matches.append({"title":event["title"],"time_utc":event["time_utc"],"minutes_since":round(delta,1),"phase":"blackout" if blocked else "caution"})
            scale=min(scale,0 if blocked else context["event_risk_scale"])
            stop=max(stop,context["event_stop_scale"])
            target=min(target,context["event_target_scale"])
        blackout=blackout or blocked
        lead=context["close_before_event_minutes"]
        close_existing=close_existing or (lead>0 and -lead<=delta<=after)
    return {"events":matches,"blackout":blackout,"close_existing":close_existing,"risk_scale":scale,"stop_scale":stop,"target_scale":target,
            "context_fresh":bool(context["updated_at"] and 0<=timestamp-context["updated_at"]<=context["max_age_hours"]*3600),"coverage_note":context["coverage_note"]}


def environment(snapshot, strategy, risk, context=None):
    features=feature_series(snapshot["frames"][strategy["timeframe"]],strategy)
    baseline=statistics.median(x["atr"] for x in features[-100:-1] if x["atr"]>0)
    result=adaptive_policy(snapshot["captured_at"],risk,features[-1]["atr"],baseline)
    if context:
        events=event_policy(snapshot["captured_at"],context)
        result.update(event_context=events,risk_scale=result["risk_scale"]*events["risk_scale"],stop_scale=result["stop_scale"]*events["stop_scale"],target_scale=result["target_scale"]*events["target_scale"])
    return result


def form_plan(snapshot, rule_signal, cfg, agents, age_seconds):
    logic=cfg["logic"]
    env=environment(snapshot,cfg["strategy"],cfg["risk"],cfg["context"])
    signal=dict(rule_signal)
    reasons=[]
    scale=env["risk_scale"]
    stop_atr,rr=cfg["strategy"]["stop_atr"],cfg["strategy"]["reward_risk"]
    max_hold=cfg["strategy"]["max_hold_minutes"]
    decision=next((a for a in agents if a["role"]==logic["decision_node"]),None)
    if logic["topology"]=="market_committee":
        plan=decision.get("plan") if decision else None
        if not decision or decision["status"]!="ok" or decision["decision"]!="approve" or decision["confidence"]<cfg["workflow"]["min_confidence"] or not plan:
            signal.update(action="HOLD",entry=None,sl=None,tp=None,reason=decision["summary"] if decision and decision.get("status")=="ok" else "交易计划 Agent 未给出有效提案")
            reasons.append("AI 委员会交易计划未通过")
        else:
            signal.update(action=plan["action"],reason=decision["summary"])
            stop_atr,rr=plan["stop_atr"],plan["reward_risk"]
            max_hold=min(max_hold,plan["max_hold_minutes"])
            if age_seconds>=min(plan["valid_for_seconds"],logic["max_plan_age_seconds"]): reasons.append("AI 计划已超过有效期")
            if not logic["allow_countertrend"]:
                opposing="DOWN" if plan["action"]=="BUY" else "UP"
                if plan["action"]!="HOLD" and any(v["direction"]==opposing for v in signal.get("context",{}).values()): reasons.append("AI 提案逆背景趋势，配置未允许逆势")
    elif logic["topology"]=="ea_parameter_advisor":
        signal.update(action="HOLD",entry=None,sl=None,tp=None,reason="EA 参数顾问链路只提出参数建议，不直接交易")
        reasons.append("参数建议需保存为新策略版本后才能生效")
    if logic.get("risk_node"):
        risk_opinion=next((a for a in agents if a["role"]==logic["risk_node"]),None)
        advice=risk_opinion.get("risk_advice") if risk_opinion else None
        if not risk_opinion or risk_opinion["status"]!="ok" or not advice or not advice["allow_entries"] or risk_opinion["decision"]!="approve" or risk_opinion["confidence"]<cfg["workflow"]["min_confidence"]:
            reasons.append("独立风控 AI 未批准参与")
        else:
            scale*=advice["risk_scale"]
            stop_atr,rr=advice["stop_atr"],advice["reward_risk"]
            max_hold=min(max_hold,advice["max_hold_minutes"])
    if age_seconds>=logic["max_plan_age_seconds"]: reasons.append("整条 Agent 链路超过计划有效期")
    if scale<=0: reasons.append("时段／波动政策暂停新开仓")
    ttl=min(logic["max_plan_age_seconds"],decision["plan"]["valid_for_seconds"] if decision and decision.get("plan") else logic["max_plan_age_seconds"])
    signal.update(risk_scale=scale,max_hold_minutes=max_hold,risk_environment=env,stop_atr=stop_atr,reward_risk=rr,valid_for_seconds=ttl)
    if signal["action"]!="HOLD":
        sign=1 if signal["action"]=="BUY" else -1
        entry=snapshot["tick"]["ask" if sign==1 else "bid"]
        distance=signal["atr"]*stop_atr*env["stop_scale"]
        signal.update(entry=entry,sl=entry-sign*distance,tp=entry+sign*distance*rr*env["target_scale"])
    else:
        signal.update(entry=None,sl=None,tp=None)
    return signal,reasons
