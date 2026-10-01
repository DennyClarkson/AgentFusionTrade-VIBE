"""Bounded OpenAI-compatible tool loop and persistent, staged Agent collaboration."""
import json
import os
import time
import hashlib
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class TradePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["BUY", "SELL", "HOLD"]
    stop_atr: float = Field(ge=.5, le=5)
    reward_risk: float = Field(ge=1, le=5)
    max_hold_minutes: int = Field(ge=5, le=240)
    valid_for_seconds: int = Field(ge=10, le=300)


class RiskAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    allow_entries: bool
    risk_scale: float = Field(ge=0, le=1)
    stop_atr: float = Field(ge=.5, le=5)
    reward_risk: float = Field(ge=1, le=5)
    max_hold_minutes: int = Field(ge=5, le=240)


class Opinion(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    decision: Literal["approve", "veto", "abstain"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=6000)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=15)
    invalidation: list[str] = Field(default_factory=list, max_length=15)
    market_report: dict | None = None
    plan: TradePlan | None = None
    strategy_patch: dict[str, float | int] | None = None
    risk_advice: RiskAdvice | None = None


def redact_tree(value, secret):
    if not secret: return value
    if isinstance(value, str): return value.replace(secret, "[REDACTED]")
    if isinstance(value, list): return [redact_tree(v, secret) for v in value]
    if isinstance(value, dict): return {redact_tree(k, secret):redact_tree(v, secret) for k,v in value.items()}
    return value


def function(name, description, properties=None, required=None):
    return {"type":"function", "function":{"name":name, "description":description,
            "parameters":{"type":"object", "properties":properties or {}, "required":required or [], "additionalProperties":False}}}


def compact(value, limit=50000):
    text = json.dumps(value,ensure_ascii=False,allow_nan=False)
    if len(text) <= limit: return text
    return json.dumps({"truncated":True,"excerpt":text[:limit]},ensure_ascii=False)


class AIClient:
    def __init__(self, store=None):
        self.store = store
        self.output_limits = {}

    def output_limit(self, config, client, key, timeout):
        policy = config.get("output_token_policy", "fixed")
        if policy == "provider_default": return None
        if policy == "fixed": return config["max_tokens"]
        cache_key = (config["base_url"], config["model"], config["key_env"])
        cached = self.output_limits.get(cache_key)
        if cached and time.monotonic()-cached[0] < 900: return cached[1]
        response = client.get(config["base_url"]+"/models", headers={"Authorization":f"Bearer {key}"}, timeout=min(15, timeout))
        if response.status_code != 200: raise ValueError("无法读取模型输出上限；请检查服务，或在 AI 配置改为服务默认/自定义单次上限")
        model = next((m for m in response.json().get("data",[]) if m.get("id") == config["model"]), {})
        limit = model.get("max_output_tokens")
        if type(limit) is not int or not 128 <= limit <= 1048576:
            raise ValueError("服务未声明模型 max_output_tokens；可在 AI 配置选择服务默认或自定义单次上限")
        self.output_limits[cache_key] = (time.monotonic(), limit)
        return limit

    def complete(self, config, prompt, messages, tools=None, dispatch=None, rounds=4, cancelled=lambda:False,
                 event=lambda _:None, json_output=False, max_seconds=300):
        """Persistable protocol transcript keeps reasoning_content for thinking + tools.

        Tools are application-owned functions; no eval, filesystem or shell is exposed.
        Raw provider reasoning stays internal and is not in the public result.
        """
        key = os.environ.get(config["key_env"], "")
        record = {"status":"error", "content":"", "tools":[], "model":config["model"], "thinking":config.get("thinking",False)}
        if not config["enabled"] or not key:
            return {**record,"status":"unavailable","error":"AI 未启用或环境变量未设置","latency_ms":0}
        started = time.monotonic()
        transcript = [{"role":"system","content":prompt}, *messages]
        usage = {}
        try:
            with httpx.Client(timeout=config["timeout_seconds"],follow_redirects=False) as client:
                event({"state":"model_config","output_token_policy":config.get("output_token_policy", "fixed")})
                output_limit = self.output_limit(config, client, key, min(config["timeout_seconds"],max_seconds))
                record["output_token_policy"] = config.get("output_token_policy", "fixed")
                record["max_output_tokens"] = output_limit
                for turn in range(rounds+1):
                    if cancelled(): raise ValueError("操作已取消")
                    remaining = max_seconds-(time.monotonic()-started)
                    if remaining <= 0: raise TimeoutError("Agent exceeded time budget")
                    payload = {"model":config["model"],"messages":transcript,"stream":False}
                    if output_limit is not None: payload["max_tokens"] = output_limit
                    host = httpx.URL(config["base_url"]).host
                    thinking_format=config.get("thinking_format","auto")
                    if thinking_format=="deepseek" or (thinking_format=="auto" and host in {"deepseek.com","api.deepseek.com"}):
                        payload["thinking"] = {"type":"enabled" if config.get("thinking") else "disabled"}
                        if config.get("thinking"): payload["reasoning_effort"] = config.get("reasoning_effort","high")
                    elif config.get("thinking"):
                        payload["reasoning_effort"] = config.get("reasoning_effort","high")
                    if not config.get("thinking"): payload["temperature"] = config["temperature"]
                    if json_output: payload["response_format"] = {"type":"json_object"}
                    if tools: payload["tools"] = tools
                    request_start = time.monotonic()
                    event(redact_tree({"state":"model_running","round":turn+1,"model":config["model"],"thinking":config.get("thinking",False)},key))
                    response = client.post(config["base_url"]+"/chat/completions",headers={"Authorization":f"Bearer {key}"},json=payload,timeout=min(config["timeout_seconds"],remaining))
                    event({"state":"model_done","round":turn+1,"duration_ms":round((time.monotonic()-request_start)*1000),"http_status":response.status_code})
                    if response.status_code != 200:
                        record["error"] = f"AI 服务返回 HTTP {response.status_code}（响应原文不记录）"
                        break
                    raw = response.json()
                    choice = raw["choices"][0]
                    message = choice["message"]
                    record["model"] = str(raw.get("model",config["model"]))[:160]
                    for k,v in (raw.get("usage") or {}).items():
                        if k in {"prompt_tokens","completion_tokens","total_tokens","prompt_cache_hit_tokens","prompt_cache_miss_tokens"} and type(v) is int and v>=0: usage[k] = usage.get(k,0)+v
                    if choice.get("finish_reason") == "length": raise ValueError("本次响应达到单次输出或上下文上限，未完成评估；请核对 AI 输出设置。这不是累计 token 配额")
                    assistant = {"role":"assistant","content":message.get("content") or ""}
                    # Official DeepSeek requires this on ALL historical assistant turns when tools are supplied.
                    if "reasoning_content" in message: assistant["reasoning_content"] = message["reasoning_content"] or ""
                    calls = message.get("tool_calls") or []
                    if calls: assistant["tool_calls"] = calls
                    transcript.append(assistant)
                    if not calls:
                        if cancelled(): raise ValueError("操作已取消")
                        record.update(status="ok",content=assistant["content"])
                        break
                    if turn == rounds: raise ValueError("工具轮次已达上限，未生成最终结论")
                    if len(calls)>6: raise ValueError("单轮工具调用超出预算")
                    known = {t["function"]["name"] for t in tools or []}
                    for call in calls:
                        if cancelled(): raise ValueError("操作已取消")
                        tool_start = time.monotonic()
                        name = call["function"]["name"]
                        args = {}
                        try:
                            raw_args = call["function"]["arguments"]
                            if len(raw_args)>12000: raise ValueError("工具参数过长")
                            args = json.loads(raw_args)
                            if not isinstance(args,dict) or name not in known or not dispatch: raise ValueError("未知工具或参数无效")
                            args = redact_tree(args,key)
                            event({"state":"tool_running","name":name,"args":args})
                            result = dispatch(name,args)
                        except Exception as exc:
                            result = {"error":type(exc).__name__,"detail":str(exc)[:500] if isinstance(exc,ValueError) else "工具调用失败"}
                        trace = {"name":name,"args":args,"result":result,"duration_ms":round((time.monotonic()-tool_start)*1000)}
                        trace = redact_tree(trace,key)
                        record["tools"].append(trace)
                        event({"state":"tool_done",**trace})
                        transcript.append({"role":"tool","tool_call_id":call["id"],"content":compact(trace["result"],24000)})
                else:
                    record["error"] = "工具预算耗尽"
        except (httpx.TimeoutException,TimeoutError):
            record["error"] = "AI 调用超时；本轮不执行交易"
        except Exception as exc:
            record["error"] = str(exc)[:300] if isinstance(exc,ValueError) else f"AI 协议错误：{type(exc).__name__}"
        record.update(latency_ms=round((time.monotonic()-started)*1000),usage=usage)
        # Private data, caller must strip before HTTP/log output.
        record["_protocol"] = transcript[1:] if record["status"]=="ok" else []
        return redact_tree(record,key)

    def opinion(self, role, config, prompt, facts, output_kind="opinion", **runtime):
        schema = Opinion.model_json_schema()
        instruction = "\n最终输出单个 JSON 对象，必须有 decision(approve/veto/abstain)、confidence(0..1)、summary（最多180字，详细依据放 evidence）。可有 evidence/uncertainties/invalidation 字符串数组及 market_report。不要输出 Markdown 或其他字段。JSON schema: " + compact(schema,18000)
        if output_kind=="plan": instruction += "\n必须有 plan；只有 decision=approve 才可 BUY/SELL，否则 plan.action=HOLD。不输出手数或价格。"
        elif output_kind=="risk": instruction += "\n必须有 risk_advice。只有同意参与才 allow_entries=true 且 decision=approve，risk_scale 不得超过1。"
        elif output_kind=="parameters": instruction += "\n仅建议 strategy_patch，不创建交易计划。"
        else: instruction += "\n本节点仅整理/审查证据，plan和risk_advice为null。"
        result = self.complete(config,prompt+instruction,[{"role":"user","content":compact(facts,80000)}],json_output=True,**runtime)
        record = {"role":role,"status":result["status"],"decision":"abstain","confidence":0,"summary":result.get("error",""),
                  **{k:result.get(k) for k in ("latency_ms","usage","model","thinking","tools")}}
        if result["status"]=="ok":
            try:
                opinion = Opinion.model_validate_json(result["content"])
                if output_kind=="plan" and opinion.plan is None: raise ValueError("缺少 plan")
                if output_kind=="risk" and opinion.risk_advice is None: raise ValueError("缺少 risk_advice")
                record.update(opinion.model_dump())
            except (ValidationError,ValueError) as exc:
                detail = "; ".join(".".join(map(str,e["loc"]))+":"+e["type"] for e in exc.errors(include_input=False)) if isinstance(exc,ValidationError) else str(exc)
                record.update(status="error",summary="AI 输出不合规："+detail)
        return redact_tree(record,os.environ.get(config["key_env"],""))

    def test(self, config):
        result = self.opinion("connection",config,"连接测试：返回 approve、confidence=1、summary=连接成功。",{"test":True})
        return {"ok":result["status"]=="ok",**result}

    def run(self, config, prompts, snapshot, signal, context, cancelled=lambda:False, logic=None, profiles=None, event=lambda _:None, memory_scope="live"):
        if not logic:
            from .prompts import staged_logic
            logic = staged_logic()
        return self.run_graph(config,prompts,snapshot,signal,context,cancelled,logic,profiles or {},event,memory_scope)

    def run_graph(self, config, prompts, snapshot, signal, context, cancelled, logic, profiles, event=lambda _:None, memory_scope="live"):
        from .indicators import packet
        completed = {}
        market = snapshot.get("analysis_packet") or packet(snapshot)
        context_brief={**context,"news":context.get("news",[])[:20],"events":sorted(context.get("events",[]),key=lambda e:abs(e["time_utc"]-snapshot["captured_at"]))[:30],"available_news":len(context.get("news",[])),"available_events":len(context.get("events",[]))}
        facts = {"market":market,"rule_proposal":signal,"context":context_brief,"risk_environment":snapshot.get("risk_environment",{})}
        nodes = {n["id"]:n for n in logic["nodes"]}
        graph_started=time.monotonic()
        tool_specs = [
            function("get_market_data","查看冻结快照的完整指标与指定周期闭合K线；不读取未来或移动快照",{"timeframe":{"type":"string","enum":list(snapshot["frames"])},"bars":{"type":"integer","minimum":5,"maximum":200}},["timeframe"]),
            function("get_background","查看本轮新闻/日历，可按关键词检索；保持冻结快照",{"query":{"type":"string","maxLength":100},"offset":{"type":"integer","minimum":0,"maximum":500}}),
            function("get_memory","读取此Agent之前的观点；仅历史记忆，不能当作当前证据"),
            function("ask_agent","向已完成的上游Agent提出具体证据问题，并取得其补充回答",{"agent_id":{"type":"string"},"question":{"type":"string","maxLength":2000}},["agent_id","question"]),
        ]
        while len(completed)<len(nodes) and not cancelled():
            ready = [n for n in logic["nodes"] if n["id"] not in completed and all(d in completed for d in n["depends_on"])]
            if not ready: raise ValueError("Agent 图无法继续")
            # Each node receives a completed upstream packet; sequential by design for inspectability.
            node = ready[0]
            role = node["id"]
            provider = config if node["ai_profile"]=="active" else profiles.get(node["ai_profile"])
            if provider is None: raise ValueError("Agent AI 方案不存在："+node["ai_profile"])
            provider = {**provider,"thinking":node.get("thinking") if node.get("thinking") is not None else provider.get("thinking",False)}
            identity=hashlib.sha256(compact({"provider":provider,"prompt":prompts[node["prompt_key"]],"graph":logic}).encode()).hexdigest()[:16]
            scope=f"{memory_scope}:{snapshot['symbol']}:{','.join(snapshot['frames'])}:{role}:{identity}"
            memory = self.store.memories(scope,logic.get("memory_turns",3),before=snapshot["captured_at"]) if self.store and memory_scope=="live" else []
            payload = {**facts,"upstream":{d:{k:v for k,v in completed[d].items() if k not in {"tools","usage"}} for d in node["depends_on"]},"memory":memory}
            started = time.time()
            event({"id":role,"state":"running","started_at":started,"thinking":provider["thinking"]})
            kind = "risk" if node["prompt_key"]=="risk_ai" else "plan" if role==logic["decision_node"] and logic["topology"]=="market_committee" else "parameters" if node["prompt_key"]=="tuner" else "opinion"
            ancestors = set(node["depends_on"])
            for _ in nodes:
                ancestors.update(d for a in list(ancestors) for d in nodes[a]["depends_on"])
            questions = [0]
            def dispatch(name,args):
                if name=="get_market_data":
                    if set(args)-{"timeframe","bars"}: raise ValueError("无效行情参数")
                    tf,n = args.get("timeframe"),args.get("bars",60)
                    if tf not in snapshot["frames"] or type(n) is not int or not 5<=n<=200: raise ValueError("行情范围无效")
                    return {**market["frames"][tf],"candles":snapshot["frames"][tf][-n:],"as_of_utc":snapshot["captured_at"]}
                if name=="get_background":
                    query,offset=args.get("query",""),args.get("offset",0)
                    if set(args)-{"query","offset"} or not isinstance(query,str) or len(query)>100 or type(offset) is not int or not 0<=offset<=500: raise ValueError("背景检索参数无效")
                    news=[n for n in context.get("news",[]) if query.lower() in (n["title"]+n.get("summary","")).lower()]
                    events=[e for e in context.get("events",[]) if query.lower() in e["title"].lower()]
                    return {**context,"news":news[offset:offset+15],"events":events[offset:offset+30],"available_news":len(news),"available_events":len(events)}
                if name=="get_memory":
                    if args: raise ValueError("该工具无需参数")
                    return {"historical_only":True,"records":memory}
                if name=="ask_agent":
                    target,question = args.get("agent_id"),args.get("question")
                    if set(args)!={"agent_id","question"} or target not in ancestors or target not in completed: raise ValueError("只能追问本节点已完成的上游Agent")
                    if not isinstance(question,str) or not 1<=len(question)<=2000 or questions[0]>=2: raise ValueError("追问预算或问题长度无效")
                    questions[0] += 1
                    other = nodes[target]
                    pc = config if other["ai_profile"]=="active" else profiles[other["ai_profile"]]
                    pc={**pc,"thinking":other.get("thinking") if other.get("thinking") is not None else pc.get("thinking",False)}
                    # Consultation cannot recursively ask Agents or mutate original decision.
                    answer = self.opinion(target,pc,prompts[other["prompt_key"]]+"\n本次仅回答下游的证据追问，不改变已提交的交易计划。",{**facts,"previous_answer":{k:v for k,v in completed[target].items() if k not in {"tools","usage"}},"question_from":role,"question":question},cancelled=cancelled,max_seconds=min(90,max(.1,logic["max_plan_age_seconds"]-(time.monotonic()-graph_started))))
                    return {"agent_id":target,"question":question,"answer":answer,"consultation_only":True}
                raise ValueError("未知工具")
            if any(completed[d]["status"]!="ok" for d in node["depends_on"]):
                result={"role":role,"status":"skipped","decision":"abstain","confidence":0,"summary":"上游节点失败，未进入决策","tools":[]}
            else:
                result = self.opinion(role,provider,prompts[node["prompt_key"]],payload,kind,
                                      tools=tool_specs if node.get("tools_enabled",True) else None,dispatch=dispatch,
                                      rounds=logic.get("tool_rounds",4),cancelled=cancelled,event=lambda v:event({"id":role,**v}),max_seconds=max(.1,logic["max_plan_age_seconds"]-(time.monotonic()-graph_started)))
            result.update(prompt_key=node["prompt_key"],ai_profile=node["ai_profile"],depends_on=node["depends_on"],started_at=started,finished_at=time.time(),memory_records=len(memory))
            completed[role] = result
            if self.store and memory_scope=="live" and result["status"]=="ok":
                self.store.remember(scope,{k:result.get(k) for k in ("role","summary","evidence","uncertainties","plan","risk_advice","model")}|{"as_of_utc":snapshot["captured_at"],"symbol":snapshot["symbol"]})
            event({"id":role,"state":"done" if result["status"]=="ok" else "skipped" if result["status"]=="skipped" else "error","finished_at":time.time(),"result":result})
        return [completed[n["id"]] for n in logic["nodes"] if n["id"] in completed]
