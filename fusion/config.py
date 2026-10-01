"""Typed, independently versioned configuration; secrets are environment references."""
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AI(Settings):
    enabled: bool = True
    base_url: str = "https://api.deepseek.com"
    model: str = Field(default="deepseek-flash", min_length=1, max_length=160)
    key_env: str = Field(default="DEEPSEEK", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_seconds: int = Field(default=40, ge=5, le=120)
    temperature: float = Field(default=0.1, ge=0, le=2)
    output_token_policy: Literal["model_max", "fixed", "provider_default"] = "model_max"
    max_tokens: int = Field(default=32768, ge=128, le=1048576)
    thinking: bool = False
    reasoning_effort: Literal["low", "high", "max"] = "high"
    thinking_format: Literal["auto", "deepseek", "reasoning_effort"] = "auto"

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        u = urlparse(value)
        if not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("API 地址不能包含凭据、查询参数或片段")
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("远程 API 必须使用 HTTPS")
        return value.rstrip("/")


class Prompts(Settings):
    ea_manager: str = Field(default="你是黄金 M1 顺势回踩 EA 的策略管理人。先读取当前策略、行情、成交复盘及对话记忆；需要数据时使用工具。清晰趋势禁止逆向，震荡方向由闭合 H1/D1 决定。触及布林外轨后收回轨内才考虑入场。亏损由初始止损控制，盈利后只收紧保护。你不能下单或提高资金风险，不能执行任意代码。参数调整必须先调用回测验证工具，再提交有限参数建议；工具拒绝时如实解释。用中文直接回答用户，区分已执行、待验证和未获通过的修改，不虚构绩效。", max_length=24000)
    market: str = Field(default="你是市场分析员。仅依据输入的已收盘多周期行情与指标，解释趋势、波动、突破的质量；不要虚构价格或新闻。", max_length=16000)
    context: str = Field(default="你是背景数据分析员。只分析提供的新闻与经济事件，区分已知和缺失数据；识别与黄金相关的事件风险。缺少信息必须明确说明。", max_length=16000)
    critic: str = Field(default="你是研判 Agent。审查策略信号、行情及背景结论，寻找反证。决定 approve、veto 或 abstain，并简述原因。不能改变风险限额或创建交易指令。", max_length=16000)
    bull: str = Field(default="你是多头研判员。基于已收盘多周期行情提出看涨交易假设，同时给出使假设失效的证据。无优势则弃权，不要为了辩论虚构事实。", max_length=16000)
    bear: str = Field(default="你是空头与反证研判员。提出看跌假设并检验多头风险，明确方向、震荡噪音和信息缺口。无优势则弃权。", max_length=16000)
    judge: str = Field(default="你是交易计划 Agent。综合独立行情、背景、多空研判，选择 BUY、SELL 或 HOLD。必须有方向性证据与清晰的失效条件；信号冲突或不确定时 HOLD。可独立于传统策略提案做出判断。", max_length=16000)
    tuner: str = Field(default="你是 EA 参数顾问。根据行情状态提出 EMA/ATR 策略的有限参数建议，并说明假设。建议不自动生效，不能修改资金预算或下单。", max_length=16000)
    risk_ai: str = Field(default="你是独立风控 AI。评估亚洲/欧洲/纽约时段、ATR 与近期波动变化、点差、经济事件和新闻缺失。对非农、CPI、利率决议等事件解释时间与不确定性。只可缩小风险、调整保护距离或拒绝开仓；不能提升硬风险预算。根据证据输出 risk_advice；背景未知时明确降低参与意愿，不虚构日历。", max_length=16000)


Timeframe = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1"]


class Strategy(Settings):
    algorithm: Literal["donchian", "pullback", "bollinger_pullback"] = "donchian"
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.#-]+$")
    timeframe: Timeframe = "M5"
    context_timeframes: list[Timeframe] = Field(default_factory=lambda: ["M15", "H1"], max_length=4)
    bars: int = Field(default=500, ge=100, le=10000)
    fast_ema: int = Field(default=20, ge=2, le=200)
    slow_ema: int = Field(default=50, ge=3, le=400)
    breakout_bars: int = Field(default=20, ge=3, le=200)
    atr_period: int = Field(default=14, ge=3, le=100)
    stop_atr: float = Field(default=1.5, ge=0.5, le=10)
    reward_risk: float = Field(default=1.5, ge=1, le=10)
    max_hold_minutes: int = Field(default=120, ge=5, le=1440)
    min_trend_atr: float = Field(default=0.2, ge=0, le=5)
    bollinger_period: int = Field(default=20, ge=10, le=60)
    bollinger_deviation: float = Field(default=2, ge=1, le=3.5)
    trend_adx: float = Field(default=20, ge=10, le=40)
    trend_slope_atr: float = Field(default=.05, ge=0, le=.5)
    range_bias: Literal["H1", "D1", "consensus"] = "H1"
    trailing_start_r: float = Field(default=1, ge=.5, le=3)
    trailing_atr: float = Field(default=1, ge=.3, le=3)
    breakeven_r: float = Field(default=.8, ge=.3, le=3)

    @model_validator(mode="after")
    def consistency(self):
        if self.fast_ema >= self.slow_ema:
            raise ValueError("快 EMA 周期必须小于慢 EMA")
        if self.bars < max(self.slow_ema * 3, self.breakout_bars + 2, self.atr_period + 2):
            raise ValueError("K 线数量不足以预热指标（至少慢 EMA 周期的三倍）")
        return self


class SessionRisk(Settings):
    name: str = Field(max_length=50)
    start_utc: int = Field(ge=0,le=23)
    end_utc: int = Field(ge=1,le=24)
    risk_scale: float = Field(ge=0,le=1)
    stop_scale: float = Field(ge=.5,le=3)
    target_scale: float = Field(ge=.5,le=3)


class Risk(Settings):
    capital: float = Field(default=1000, gt=0, le=1e8)
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=5)
    daily_loss_pct: float = Field(default=2, gt=0, le=20)
    max_positions: int = Field(default=1, ge=1, le=10)
    max_spread_points: float = Field(default=60, gt=0, le=100000)
    max_tick_age_seconds: int = Field(default=30, ge=2, le=300)
    max_slippage_points: int = Field(default=20, ge=0, le=1000)
    max_margin_pct: float = Field(default=30, gt=0, le=80)
    magic: int = Field(default=26010001, ge=1, le=2147483647)
    commission_per_lot: float = Field(default=7, ge=0, le=1000)
    volatility_reduce_ratio: float = Field(default=1.8,ge=1,le=10)
    volatility_pause_ratio: float = Field(default=3,ge=1.1,le=20)
    sessions: list[SessionRisk] = Field(default_factory=lambda:[SessionRisk(name="Asia",start_utc=0,end_utc=7,risk_scale=.6,stop_scale=1.1,target_scale=.9),SessionRisk(name="Europe",start_utc=7,end_utc=13,risk_scale=1,stop_scale=1,target_scale=1),SessionRisk(name="New York",start_utc=13,end_utc=21,risk_scale=.7,stop_scale=1.2,target_scale=1),SessionRisk(name="Rollover",start_utc=21,end_utc=24,risk_scale=0,stop_scale=1,target_scale=1)], min_length=1,max_length=12)

    @model_validator(mode="after")
    def schedule(self):
        coverage=[0]*24
        for s in self.sessions:
            if s.end_utc<=s.start_utc: raise ValueError("时段跨午夜请拆成两段")
            for h in range(s.start_utc,s.end_utc): coverage[h]+=1
        if any(n!=1 for n in coverage): raise ValueError("风险时段必须无重叠地覆盖 24 小时")
        if self.volatility_pause_ratio<=self.volatility_reduce_ratio: raise ValueError("暂停波动阈值必须大于降风险阈值")
        return self


class Workflow(Settings):
    module: Literal["ai", "ea"] = "ai"
    mode: Literal["shadow", "demo"] = "shadow"
    poll_seconds: int = Field(default=15, ge=5, le=3600)
    ai_gate: Literal["off", "advisory", "required"] = "advisory"
    min_confidence: float = Field(default=0.65, ge=0, le=1)
    require_context: bool = False
    session_start_utc: int = Field(default=6, ge=0, le=23)
    session_end_utc: int = Field(default=21, ge=0, le=23)
    block_weekends: bool = True


class Event(Settings):
    title: str = Field(min_length=1, max_length=300)
    time_utc: float = Field(gt=0)
    impact: Literal["low", "medium", "high"] = "high"
    currency: str = Field(default="USD", max_length=8)
    source: str = Field(default="manual", max_length=500)
    blackout_before_minutes: int | None = Field(default=None, ge=0, le=240)
    blackout_after_minutes: int | None = Field(default=None, ge=0, le=240)


class News(Settings):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=3000)
    source: str = Field(default="manual", max_length=500)
    time_utc: float = Field(gt=0)


class Context(Settings):
    events: list[Event] = Field(default_factory=list, max_length=500)
    news: list[News] = Field(default_factory=list, max_length=100)
    updated_at: float = Field(default=0, ge=0)
    max_age_hours: float = Field(default=24, ge=0.5, le=168)
    blackout_before_minutes: int = Field(default=15, ge=0, le=240)
    blackout_after_minutes: int = Field(default=15, ge=0, le=240)
    caution_before_minutes: int = Field(default=60, ge=0, le=1440)
    caution_after_minutes: int = Field(default=45, ge=0, le=1440)
    event_risk_scale: float = Field(default=.5, ge=0, le=1)
    event_stop_scale: float = Field(default=1.2, ge=.5, le=3)
    event_target_scale: float = Field(default=.8, ge=.5, le=3)
    close_before_event_minutes: int = Field(default=5, ge=0, le=240)
    coverage_note: str = Field(default="手动背景输入；未连接完整新闻／经济日历。缺失不等于无事件。", max_length=2000)


class BrokerSettings(Settings):
    terminal_path: str = Field(default="", max_length=500)
    server_utc_offset_hours: float = Field(default=0, ge=-14, le=14)


class AgentNode(Settings):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,30}$")
    prompt_key: Literal["market", "context", "critic", "bull", "bear", "judge", "tuner", "risk_ai"]
    ai_profile: str = Field(default="active", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    thinking: bool | None = None
    tools_enabled: bool = True


class Logic(Settings):
    topology: Literal["signal_review", "market_committee", "ea_parameter_advisor"] = "signal_review"
    nodes: list[AgentNode] = Field(default_factory=lambda: [AgentNode(id="market",prompt_key="market"),AgentNode(id="context",prompt_key="context"),AgentNode(id="critic",prompt_key="critic",depends_on=["market","context"])], min_length=1, max_length=8)
    decision_node: str = "critic"
    risk_node: str | None = None
    max_parallel: int = Field(default=2,ge=1,le=3)
    max_plan_age_seconds: int = Field(default=180,ge=10,le=600)
    allow_countertrend: bool = False
    historical_samples: int = Field(default=6,ge=2,le=12)
    memory_turns: int = Field(default=3, ge=0, le=12)
    tool_rounds: int = Field(default=4, ge=0, le=8)

    @model_validator(mode="after")
    def graph(self):
        ids = [n.id for n in self.nodes]
        if len(ids)!=len(set(ids)) or self.decision_node not in ids:
            raise ValueError("Agent ID 必须唯一，且决策节点必须存在")
        if self.risk_node and self.risk_node not in ids: raise ValueError("风控节点不存在")
        if self.risk_node and next(n for n in self.nodes if n.id==self.risk_node).prompt_key!="risk_ai":
            raise ValueError("风控节点必须使用 risk_ai 提示词")
        resolved=set()
        for _ in self.nodes:
            ready=[n for n in self.nodes if n.id not in resolved and set(n.depends_on)<=resolved]
            if not ready: break
            resolved.update(n.id for n in ready)
        if len(resolved)!=len(ids):
            raise ValueError("Agent 链路存在环或缺失的依赖节点")
        if self.topology=="market_committee" and next(n for n in self.nodes if n.id==self.decision_node).prompt_key!="judge":
            raise ValueError("市场委员会需要以 judge 提示词节点输出交易计划")
        return self


class Indicators(Settings):
    ema_periods: list[int] = Field(default_factory=lambda: [20, 50, 200], min_length=1, max_length=6)
    rsi_period: int = Field(default=14, ge=3, le=50)
    atr_period: int = Field(default=14, ge=3, le=50)
    adx_period: int = Field(default=14, ge=3, le=50)
    bollinger_period: int = Field(default=20, ge=10, le=60)
    bollinger_deviation: float = Field(default=2, ge=1, le=3.5)
    macd_fast: int = Field(default=12, ge=2, le=50)
    macd_slow: int = Field(default=26, ge=3, le=100)
    macd_signal: int = Field(default=9, ge=2, le=30)
    candles_in_prompt: int = Field(default=24, ge=5, le=100)

    @model_validator(mode="after")
    def periods(self):
        if any(p < 2 or p > 400 for p in self.ema_periods) or self.macd_fast >= self.macd_slow:
            raise ValueError("指标周期无效")
        return self


class NewsSettings(Settings):
    enabled: bool = True
    provider: Literal["gdelt_fed", "fed"] = "gdelt_fed"
    query: str = Field(default='"gold price"', min_length=3, max_length=500)
    refresh_minutes: int = Field(default=15, ge=5, le=180)
    timeout_seconds: int = Field(default=20, ge=5, le=60)
    max_records: int = Field(default=30, ge=5, le=100)
    lookback_hours: int = Field(default=48, ge=1, le=168)
    auto_calendar: bool = True


class EASettings(Settings):
    execution_magic: int = Field(default=26010002, ge=1, le=2147483647)
    entry_lease_seconds: int = Field(default=30, ge=10, le=120)
    ai_controls_entries: bool = True
    decision_ttl_minutes: int = Field(default=60, ge=5, le=120)
    require_calendar: bool = True
    strategy: Strategy = Field(default_factory=lambda: Strategy(algorithm="bollinger_pullback", timeframe="M1", context_timeframes=["H1", "D1"], bars=600, max_hold_minutes=45, stop_atr=1.5, reward_risk=2))
    ai_profile: str = Field(default="active", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    thinking: bool = True
    prompt_key: Literal["ea_manager"] = "ea_manager"
    review_enabled: bool = True
    review_on_protection_exit: bool = True
    exit_review_scan_seconds: int = Field(default=5, ge=1, le=60)
    exit_review_overlap_hours: int = Field(default=24, ge=1, le=720)
    exit_review_retry_seconds: int = Field(default=60, ge=10, le=3600)
    review_interval_minutes: int = Field(default=30, ge=5, le=1440)
    auto_apply: bool = True
    min_review_trades: int = Field(default=5, ge=1, le=100)
    memory_turns: int = Field(default=12, ge=2, le=40)
    backtest_bars: int = Field(default=15000, ge=2000, le=60000)
    max_parameter_change_pct: float = Field(default=20, ge=1, le=30)

    @model_validator(mode="after")
    def strategy_kind(self):
        if self.strategy.algorithm != "bollinger_pullback" or self.strategy.timeframe != "M1" or not {"H1", "D1"} <= set(self.strategy.context_timeframes):
            raise ValueError("EA 模块使用 M1 布林顺势回踩，并需要 H1/D1 背景")
        return self


MODELS = {"ai": AI, "prompts": Prompts, "strategy": Strategy, "risk": Risk, "workflow": Workflow, "context": Context, "broker": BrokerSettings, "logic": Logic, "indicators": Indicators, "news": NewsSettings, "ea": EASettings}
DEFAULTS = {category: model().model_dump() for category, model in MODELS.items()}


def validate(category, data):
    if category not in MODELS:
        raise ValueError("未知配置分类")
    return MODELS[category].model_validate(data).model_dump()
