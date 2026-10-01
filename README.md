# AgentFusionTrade-VIBE

基于 **Python + MetaTrader 5** 的 AI 交易工作台。此仓库保存 VIBE 开发版，作为后续手动开发版本的独立基线。应用内部名称仍为 AgentTradeFusion，Python 包为 `fusion`，现有 EA 名称和文件协议保持不变。

两个工作区：

- **AI交易**：MT5 行情与指标 → 行情 Agent → 背景 Agent → 研判 Agent → 风控 Agent → Python 执行网关。
- **EA工作室**：原生 FusionExecutor 负责交易和持仓保护；AI 负责对话、复盘、候选参数回测、调参及有时限的开仓许可。

目前支持影子模拟与 MT5 **模拟账户**，启动时停止、未解锁。策略仍处于研究阶段，尚未通过稳定盈利验收；原生交易的完整成交与保护生命周期仍需验证。

## 首次运行

需要 Windows、已登录模拟账户的 MT5 终端，以及 Git 和 `uv`。项目使用 Python 3.12；`uv` 可在同步时准备对应解释器。Node.js 仅用于前端逻辑测试，运行工作台不需要。

```powershell
git clone https://github.com/DennyClarkson/AgentFusionTrade-VIBE.git
cd AgentFusionTrade-VIBE
uv sync --python 3.12 --locked
.venv\Scripts\python.exe run.py
```

以后可双击 `Start-Fusion.cmd`，浏览器打开 <http://127.0.0.1:8787>。如果已有同端口工作台，启动器会打开已有服务；刷新网页不等于重启 Python 进程。更新 Python 源码后的重启方法见 [原生 EA 使用说明](docs/NATIVE-EA.md)。

AI 密钥在 Windows 用户环境变量中配置，默认变量名为 `DEEPSEEK`，设置后重新打开启动终端。不要将密钥写入源码或提交文件。应用不自动加载 `.env`。在“配置方案 → AI 连接”设置服务地址、模型 ID、环境变量名和思考协议；官方 DeepSeek 预设模型 ID 为 `deepseek-flash`，以实际账户可用模型为准。

源码不携带本地数据库、聊天记录、经纪商配置或 EA 编译产物。首次运行创建本机配置；首次使用 EA 需在 MetaEditor 编译随仓库提供的 `.mq5` 和 `.mqh`，详见 [挂载与启动步骤](docs/NATIVE-EA.md)。

## 功能

### AI交易

程序先从已收盘 K 线计算 EMA、ATR、RSI、ADX/DMI、MACD、布林带、量能、支撑阻力及波动率状态，再交给 Agent。各节点可独立选择 AI、提示词、依赖、思考模式和工具权限。

Agent 可查询更多行情、读取背景和历史观点，通过 `ask_agent` 追问已完成的上游；工具边界不允许递归获得下单或任意代码能力。观点记忆按品种、周期、节点及配置指纹隔离。执行前重新核对行情、事件和计划时效。

### EA工作室

默认 XAUUSD M1 顺势布林回撤：明显多头只买、明显空头只卖，震荡时按配置使用 H1/D1 方向；回撤触及前值外轨并收回确认。EA 管理初始 SL/TP、保本与跟踪保护；AI 在独立线程复盘。

管理 AI 有独立模型方案、思考设置、上下文和真实工具记录。参数候选需通过时间分段回测、成本压力、基线比较、变化幅度、配置版本及空仓检查。参数发布和 EA 确认是两个不同状态。

每笔 EA 止损/止盈出场成交默认独立触发复盘与参数评估，包括追踪止损和部分成交。忙时持久排队、失败退避重试，不受定时或最低笔数门槛限制；证据不足时保留原参数。活动面板显示原因和队列进度，对话报告支持可横向滚动的 Markdown 表格。详见 [逐笔复盘说明](docs/NATIVE-EA.md#每次止损止盈后的复盘)。

顶部“**允许交易时间**”支持北京时间/UTC、整点起止、跨午夜、全天和周末开关。保存会暂停管理并等待 EA 确认，之后手动解锁、启动。到结束时间会触发框架持仓退出；风险预算为零的时段也会显示，不会被“全天”覆盖。

“**AI 管理活动**”显示当前任务、模型与思考选项、工具过程、错误、耗时和用量。没有累计 token 配额；单次输出可使用模型声明上限、服务默认或自定义值，仍受供应商上下文与响应限制。

### 配置、新闻与风险

- AI、提示词、策略、风险、工作流、背景、MT5、Agent 链路、指标、新闻、EA 共 11 类独立命名方案，带版本、历史和激活状态。
- 免费新闻使用 GDELT DOC API 与美联储 RSS；经济日历来自账户匹配的 MT5 EA，也支持人工事件。数据时效和采集失败单独显示。
- 初始策略资金 1,000 USD，单笔风险预算 0.5%，日亏损上限 2%，最多一笔、不摊平。手数按最终止损距离向下取整；最小手数超过预算时跳过。
- 时段、波动率、重大事件和风控 AI 可收紧预算；确定性风控独立于模型置信度。两个工作区互斥执行，重启不自动恢复交易。
- [可选 MT5 MCP](docs/INTEGRATIONS.md) 供外部助手查询和研究。应用内 Agent 工具、EA 管理和正常交易不依赖外部编程助手持续参与。

## 回测与当前边界

历史回放包含下一根开盘成交、点差、双向滑点、佣金、最小手数及保证金约束；高周期仅使用当时已收盘数据。同根止盈/止损冲突保守按止损优先，收盘形成的跟踪保护下一根生效。

首轮 M1 的 15,000 根历史实验未通过：校准 -5.03%、验证 -1.01%、双倍成本验证 -1.80%。这些是历史研究结果，不是新克隆仓库自动生成的结果。完整数据和本机运行记录不随仓库发布；见 [研究记录与假设](docs/RESEARCH.md)。

历史事件与逐笔路径不完整；原生 EA 按报价/定时器更新保护，不能声称与回测逐笔一致。编译、单元测试、连接和参数回执均不等同于成交或盈利验收。

## 开发

```powershell
.venv\Scripts\python.exe -m pytest -q
node --test tests/test_trading_hours.cjs tests/test_markdown.cjs
node --check fusion/static/markdown.js
node --check fusion/static/app.js
```

测试使用隔离存储与模拟经纪商；`scripts/demo_smoke.py` 是会操作模拟账户的显式实机测试，不属于上述测试集。`scripts/verify_native_manager.py` 是可消耗 API 用量的显式供应商探测；`scripts/research_cached.py` 需要自行准备的本地历史缓存。

| 路径 | 内容 |
|---|---|
| `fusion/` | Python 服务、AI 链路、风险、执行与回测 |
| `fusion/static/` | 无构建步骤的中文 Web UI |
| `integrations/mt5/` | 原生交易 EA、算法内核、可选日历伴随 EA |
| `integrations/mcp/` | 外部客户端配置示例，需要替换安装路径 |
| `tests/` | 隔离回归测试 |
| `docs/` | 架构、接口、EA/MCP、研究和开发交接 |
| `.agents/skills/` | 项目稳定开发规则 |

[开发交接](docs/HANDOFF.md) · [架构](docs/ARCHITECTURE.md) · [v2 接口](docs/V2-CONTRACT.md) · [开发约定](AGENTS.md) · [稳定 skill](.agents/skills/fusion-development/SKILL.md)

`data/fusion.sqlite3` 保存本机配置、审计、执行意图、聊天和记忆；`artifacts/` 保存研究与验证产物，均被 Git 忽略。备份应使用 SQLite backup API，或在停止服务后保存完整数据目录及 WAL。不要通过删除未知订单或 EA 状态记录恢复交易。

## 许可

本次发布尚未指定开源许可证；具体授权由作者后续决定。参考项目仅用于行为研究，没有打包其源码、提示词或素材。第三方依赖遵循各自许可证。
