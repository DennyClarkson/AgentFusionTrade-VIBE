# MT5 MCP 与 EA

## 三种连接

启动 `Start-Fusion.cmd` 并保持 MT5 登录模拟账户。工作台内部 Agent 工具调用、上下文和 EA 参数桥接由 Python 应用提供，不依赖外部编程助手持续运行。

- **FusionExecutor ↔ Python AI 管理器**：EA 工作室的原生交易与持仓保护；AI 发送参数和限时开仓许可。挂载、编译、参数确认和启动见 [原生 EA 说明](NATIVE-EA.md)。
- **可选 FusionBridge ↔ Python 工作台**：行情遥测、经济日历、策略信号和旧参数回执；FusionBridge 本身不交易。
- **外部助手 ↔ MCP ↔ Python 工作台**：供支持 MCP 的客户端查询行情、决策、回测和 EA 状态。MCP 配置不是 EA 更新步骤，正常使用工作台无需 MCP 客户端。

AI 交易工作区使用 Python 执行网关；EA 工作室使用 FusionExecutor。连接 FusionBridge 不能证明原生交易执行器已经就绪。

## 配置 MCP 客户端

先启动工作台，再让客户端通过标准输入输出启动 `fusion.mcp_server`。它不是网页服务器。服务端使用官方 Python MCP SDK 1.x，具体依赖由 `uv.lock` 固定。

仓库提供两份示例：

- [codex.example.toml](../integrations/mcp/codex.example.toml)：合并进客户端支持的 MCP TOML 配置，保留已有内容。
- [client.example.json](../integrations/mcp/client.example.json)：供使用 `mcpServers` JSON 格式的客户端参考，不可当作 TOML 使用。

把示例中 **所有** `C:/path/to/AgentFusionTrade-VIBE` 替换为实际克隆目录。示例不会自动修改全局配置，也不包含 API 密钥。

| 字段 | 配置 |
|---|---|
| 名称 | `agent-trade-fusion` |
| 命令 | `C:/path/to/AgentFusionTrade-VIBE/.venv/Scripts/python.exe` |
| 参数 | `-m`、`fusion.mcp_server`，作为两个参数 |
| 工作目录 | 项目克隆目录 |
| `PYTHONPATH` | 项目克隆目录 |
| `PYTHONIOENCODING` | `utf-8` |

连接后调用 `mt5_status`；返回当前工作台状态才表示接入成功。AI 供应商密钥仍由工作台进程的环境变量提供，不需复制到 MCP 配置。

## MCP 工具

| 工具 | 行为 |
|---|---|
| `mt5_status` | 连接、账户类型、敞口、引擎状态 |
| `mt5_market_snapshot` | 品种报价与多周期已收盘行情 |
| `fusion_configuration` | 方案与版本，不返回 API 密钥 |
| `fusion_decisions` | 最近决策证据 |
| `fusion_research_backtest` | 确定性规则回放，不下单 |
| `fusion_shadow_cycle` | 仅活动模式为 shadow 时分析/影子记账，可消耗 AI 用量 |
| `fusion_stop` | 停止新周期并解除交易解锁 |
| `mt5_ea_bridge_status` | EA 遥测连接状态 |
| `mt5_reconcile_orders` | 按既有成交/持仓证据核对未知执行意图，不重发 |
| `fusion_agent_pipeline` | 节点状态、指标包与工具证据 |
| `fusion_background` | 新闻缓存、日历与来源健康状态 |
| `fusion_ea_workspace` | EA 策略、复盘、建议与原生回执 |
| `fusion_ea_backtest` | 异步 M1 历史回放，不下单 |
| `fusion_ea_job` | 指定任务结果 |

没有解锁、启动交易循环、任意原始订单、任意文件写入或任意代码执行工具。MCP 的操作通过现有本地 API 和风控边界。

## 可选 FusionBridge

源码位于 `integrations/mt5/FusionBridge.mq5`，需要用 MetaEditor 编译。该 EA 不包含 OrderSend、DLL 或 WebRequest；负责导出报价、账户标识、闭合指标、USD 经济日历和参数确认。

协议位于 MT5 公共文件夹 `Files/AgentTradeFusion/`。旧参数兼容 FUSION2；v2 遥测使用 FUSION3。策略参数文件 `strategy.csv` 使用账户/服务器限定的 FUSION_EA1。Python 原子发布文件，EA 读取应用后返回版本；不能将写入成功当作应用成功。

如果使用伴随 EA，将它挂在不同于 FusionExecutor 的图表。更新源码后先编译，再重新挂载以加载新程序。连接是否新鲜、账户是否匹配及日历是否可用，以工作台实时状态为准。日历成功刷新不代表历史或未来事件覆盖完整。

固定模板源码生成 API 会将经过配置验证的 `.mq5` 保存到本机 `artifacts/generated-ea/`。它不执行任意模型代码，不自动挂图或交易。原生交易使用独立的 FusionExecutor。

## 时间来源

Python 原始 MT5 时间校正默认 **0**。只有测量确认当前终端的 rates/ticks/成交时间相对 UTC 存在异常时才调整；普通经纪商时区不是自动设置修正值的依据。

MQL5 经济日历使用交易服务器时间，EA 以 `TimeTradeServer` 与 `TimeGMT` 的差值单独转换。此转换与 Python 原始时间异常修正相互独立。交易允许时段统一保存在 UTC，UI 可显示北京时间。
