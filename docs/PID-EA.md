# FusionPIDTrader：带原生界面的 MT5 EA

这是直接安装到 MT5 的 `.ex5` EA。策略、逐 Tick 判断、本地触线平仓、经纪商保护和界面均由 MQL5 执行。可选的 Python AI 后台负责复盘、对话和有限参数建议；不需要打开原有网页工作台，也不需要编程助手持续参与。

## 安装与开始

1. MT5 → 文件 → 打开数据文件夹，将 `FusionPIDTrader.ex5` 放入 `MQL5/Experts/FusionPIDTrader/`。从源码安装时，把 [FusionPIDTrader.mq5](../integrations/mt5/FusionPIDTrader.mq5) 和 [FusionPIDCore.mqh](../integrations/mt5/FusionPIDCore.mqh) 放在同一目录，在 MetaEditor 打开 `.mq5`，按 F7 编译。
2. 导航器 → EA 交易 → 右键刷新，展开 `FusionPIDTrader`。
3. 切换前先停止旧工作台的管理循环，确认旧 EA 已暂停、账户空仓、没有待确认交易，再从图表移除旧 `FusionExecutor`。两者共享独占执行锁；旧 EA 仍挂着时，新 EA 会拒绝取得执行权。不要通过删除锁文件绕过。
4. 打开 **XAUUSD、M1** 图表，把 `FusionPIDTrader` 拖到图表，允许算法交易。当前版本只接受 **USD 模拟账户**。
5. 面板初始暂停。检查“参数与时段”，然后在“交易与曲线”点击 **启动模拟盘 / START**。这只是允许符合条件的新单，不保证立即开仓。风险预算、最小手数、行情或信号不满足时会等待。

EA 重载、终端重启、周期切换后均需重新手动启动。暂停后，本 EA 仍保护已有仓位；移除 EA 或关闭终端后，本地 PID 不再运行，仅保留经纪商已确认的 SL/TP。

`PanelPreviewOnly=true` 用于在其他图表预览界面：独立目录、不能启动交易、不接管仓位。普通使用保持 `false`。

## 原生面板

| 页面 | 内容 |
|---|---|
| 交易与曲线 | 启动/暂停；暂停并平本 EA 仓位；最近 300 个 Tick；本地上下轨；已确认经纪商止损；当前状态和等待原因 |
| 参数与时段 | 启动盈利金额、上下轨报价距离、Kp/Ki/Kd、UTC 起止小时；暂停并保存 |
| AI 复盘与对话 | 原生文本输入、发送、立即复盘、后台进度和最新结论 |

上下轨在 PID 激活后显示；未激活时显示初始本地止损。空仓时没有虚构保护线。曲线使用多单 Bid、空单 Ask；这是该方向实际退出所用的报价。

参数页保存要求账户空仓且无订单/待确认请求，保存后保持暂停。相同起止小时表示全天，起点大于终点表示跨午夜。周末开关及其他策略参数在 EA 的“输入”页。这里的时段限制新开仓，不强制在收市段末尾平仓。

参数页保存的设置会跨重启保留，优先于相同名称的输入默认值。设置、运行状态与 AI 配置分别存储。MT5 主图高度建议至少 550 像素；若工具箱/测试器占据过多空间，请缩小下面的窗格。

## 交易和保护逻辑

- 大势使用已收盘 M5 EMA20/50 与 ATR 分离程度。明确趋势只做同方向回撤；横盘按最近 3 个完整日线的高低区间选择方向，中部等待。
- M1 布林带与过去 14 根完整 M1 的原始 Stoch 区间提供回撤位置。实时 Tick 触及条件并出现顺向转向才尝试入场；每根 M1 最多一次尝试，平仓后默认冷却 300 秒，不加仓、不摊平。
- 仅使用品种最小手数。初始 1,000 USD 策略资金、单笔 0.5%、日亏损预算 2%。较宽经纪商止损、预计佣金和滑点也必须落入预算；最小手数仍过大就跳过。保证金另行检查。
- 初始本地止损默认 1.5 ATR；下单同时发送更宽的经纪商 SL 和 TP。浮盈按预计净收益计算，默认达到 3 USD 后激活 PID。
- 上下轨围绕 PID 跟踪中心移动，可双向移动。**先检查报价是否穿越上一时刻的线，再更新 PID**，防止急变被新位置吞掉。任意一条线触发都锁定退出意图，然后用当前新鲜报价提交平仓。
- 上下轨距离为固定报价距离与 ATR 距离的较大者，在本次激活时确定。`UpperDistance=1` 是报价移动 1.00，**不是账户盈利 1 USD**。启动阈值和经纪商锁盈目标才是 USD 金额。
- 经纪商 SL 的目标默认对应预计净盈利 3 USD，独立于双向 PID，只允许收紧。需要交易服务器确认，且受最小止损距离和冻结距离约束。刚好浮盈 3 USD 时可能尚无法放到该价格；面板会显示“尚未确认”。跳空、滑点与费用变化也意味着这不是保证到手 3 USD。
- 长时间无报价会重置积分/微分记忆并限制推进时间；速度限制和抗积分饱和防止 PID 无界追赶。使用 `CopyTicks` 消费遗漏报价；历史越线只触发当前时刻的退出请求，不假装在历史价成交。

交易意图在发送前持久化。断线、超时或含糊返回不会自动重复开平仓；需要成交/持仓/订单证据确认。持仓与账户、服务器、品种、Magic 绑定。状态损坏或多个同属仓位会阻止继续开仓，不自动抹去不确定状态。

## 接入 AI

在仓库根目录双击 [Start-PID-AI.cmd](../Start-PID-AI.cmd)，或者：

```powershell
uv sync --python 3.12 --locked
.venv\Scripts\python.exe -m fusion.pid_manager
```

首次运行创建两个独立文件：

- `data/pid-manager.json`：服务地址、模型、密钥环境变量名、思考模式、复盘间隔、许可有效期、是否自动应用参数、上下文长度。
- `data/pid-manager-prompt.txt`：管理 Agent 的提示词。

默认使用项目官方 DeepSeek 配置，密钥只从 `DEEPSEEK` 环境变量读取。`ai.model` 使用服务商实际模型 ID；`ai.thinking` 控制思考模式。没有累计 token 配额。修改 AI 配置后重启这个后台进程；不需要重启交易终端。

默认 `RequireAI=false`，EA 可以独立运行，AI 仅提供建议。若要 AI 管理是否允许新单，把 EA 输入 `RequireAI=true`：手动 START 后，EA 还需等待 AI 的短期许可；许可到期或 AI 否决时暂停新单，已有仓位保护继续。AI 永远不能替用户 START。

自动参数应用需要同时设置 EA `AllowAIParameters=true` 和 JSON `auto_apply_parameters=true`。只允许七个参数：`kp/ki/kd/upper/lower/stop_atr/activation`，并受固定输入基准的 20% 变化幅度、绝对范围、空仓和无待确认交易检查。资金、手数、风控上限不交给 AI。该独立 EA 的参数建议目前**没有接入原网页工作台的候选回测批准链**，因此默认关闭自动应用。

每个确认退出成交都会独立进入持久队列，包含本地退出、经纪商 SL/TP 和部分退出；不会等待凑满交易笔数。定期任务默认 120 秒一次；逐笔复盘可以决定保持参数。后台有 `get_market`、`get_exits` 两个只读工具和按账户/服务器/品种/Magic 隔离的上下文。最新结论在原生 AI 页显示，完整消息和任务保存在本机 SQLite；当前面板不是完整历史聊天浏览器。

文件位置：MT5 公共数据目录下 `Files/AgentTradeFusion/PID/`。原生参数、状态、遥测、成交与问答按作用域分开；后台有系统文件锁，避免重复进程复盘同一任务。准备完成的模型结果持久化后，发布失败重试同一版本，不重复调用模型。测试器与只读预览使用独立子目录，默认后台不会读取它们。

## 验证和边界

MT5 Ctrl+R → 选择本 EA、XAUUSD、M1 → “每个点基于实时点”（Every tick based on real ticks）→ 设置日期、资金和延迟 → 勾选可视化 → 开始。`TesterAutoStart=true` 只在测试器里自动启动，不影响普通图表的启动暂停规则。

原生回放用于观察实际 MQL 的开平仓、保护与面板行为。Python [PID 参考模型](../fusion/pid_envelope.py) 和单元测试用于数学/状态边界，不替代经纪商真实 Tick 回放，也不宣称与完整原生执行逐笔一致。EA 加载时还执行固定参考向量、边界越线、积分饱和自检。

这版尚未接入独立新闻日历，AI 的输入也不包含完整新闻；不能假装已经覆盖非农等事件。需在人工时段设置中避开不想参与的窗口，后续再增加有数据时效证明的事件门。实际部分成交、断线重启和异常回执的完整模拟盘验收仍待补足。编译、界面通过和单日回放都不能说明策略稳定盈利。

实现参考：[OnTick](https://www.mql5.com/en/docs/event_handlers/ontick)、[CopyTicks](https://www.mql5.com/en/docs/series/copyticks)、[OrderSendAsync](https://www.mql5.com/en/docs/trading/ordersendasync)、[交易回执](https://www.mql5.com/en/docs/event_handlers/ontradetransaction)、[OrderCalcProfit](https://www.mql5.com/en/docs/trading/ordercalcprofit)、[测试器指标显示](https://www.mql5.com/en/docs/common/testerhideindicators)。
