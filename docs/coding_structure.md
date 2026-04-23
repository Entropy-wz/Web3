# ACE-Sim 全量代码设计报告（coding_structure）

## 1. 文档目的与读者
本文件是项目的正式工程设计报告，面向以下对象：
1. 论文评审与合作研究者：快速理解系统设计思想与实验可复现性。
2. 新加入开发者：不读完整源码也能掌握结构、边界和扩展方式。
3. 实验执行人员：按统一口径配置参数、运行仿真、复盘结果。

与 `README.md` 的分工：
1. README：快速安装、快速运行、常用命令。
2. coding_structure：架构设计、模块职责、数据结构、流程细节、约束规则、测试映射。

---

## 2. 系统目标与设计原则

### 2.1 研究目标
ACE-Sim 用于复现 Web3 风险事件中的五类关键机制耦合：
1. 经济机制：稳定币脱锚、铸造赎回反馈。
2. 执行机制：Gas 抢跑、滑点失败、拥堵积压。
3. 信息机制：跨圈层传播、语义失真、谣言扩散。
4. 认知机制：异构模型推理、有限理性、注意力稀缺。
5. 治理机制：投票权迁移、参数演化、制度反馈。

### 2.2 工程原则
1. 先物理后认知：经济账本与执行规则必须先严格成立。
2. 原子与可回放：每笔动作可审计，失败不能留下半状态。
3. 仿真时钟统一：不依赖真实时间，全部由 tick 驱动。
4. 可插拔可降级：在线模型失败时系统仍可继续运行。
5. 数据闭环：日志、指标、快照、数据库可相互核对。

---

## 3. 总体架构分层

ACE-Sim 采用五层结构，从下到上分别是：
1. 经济物理层（Phase 1）
2. 执行调度层（Phase 2）
3. 社会拓扑层（Phase 3）
4. 认知决策层（Phase 4）
5. 治理演化层（Phase 5）

代码目录：`src/ace_sim/`
1. `engine/`：账本、池子、铸造赎回、不变量。
2. `execution/`：动作协议、护栏、Tick 调度。
3. `social/`：网络拓扑、频道、语义过滤。
4. `agents/`：角色与行为基类。
5. `cognition/`：模型路由、记忆检索、决策拼装。
6. `runtime/`：批量驱动 Agent 的每 tick 决策。
7. `governance/`：提案、投票、编译、指标、快照。
8. `config/`：LLM 配置解析。

---

## 4. 经济物理层设计（engine）

核心文件：`src/ace_sim/engine/ace_engine.py`

### 4.1 状态对象
1. `Account`（Pydantic）
   1. 字段：`address`, `UST`, `LUNA`, `USDC`
   2. 余额必须非负
2. `AMM_Pool`
   1. 常数乘积池 `x*y=k`
   2. 支持输入侧费率
3. `ACE_Engine`
   1. 管理账户、双池、费库、计数器、总供应
   2. 管理 SQLite 账本和状态快照

### 4.2 经济结构（三角闭环）
1. `Pool_A = UST/USDC`：稳定币兑换池。
2. `Pool_B = LUNA/USDC`：LUNA 现货定价池。
3. `UST <-> LUNA`：铸造赎回通道。
4. 内生预言机：铸造赎回价格来自 `Pool_B` 即时边际价格。

### 4.3 原子执行模型
所有写操作统一走 `_atomic_action`：
1. 克隆当前状态。
2. 在克隆状态上执行动作。
3. 执行全局不变量检查。
4. 通过后一次性提交并写流水。
5. 失败时放弃修改，仅记录失败流水。

### 4.4 守恒规则
1. 交换动作：资产在“账户+池+费库”范围内守恒。
2. 铸造赎回：通过累计铸造/销毁计数器闭环审计。
3. USDC：不通过铸造凭空增减，仅在系统内部迁移。

### 4.5 极端数值处理
1. 全链路使用 `Decimal`。
2. 设高精度上下文，避免 float 崩溃。
3. 双重校验：Decimal 阈值 + `math.isclose`。
4. 支持超低价格与超大供应量级实验。

### 4.6 引擎配置参数
1. `minting_allowed`
2. `swap_fee`
3. `daily_mint_cap`

运行中可通过治理更新（受白名单约束）。

---

## 5. 执行调度层设计（execution）

### 5.1 动作注册表（action_registry/actions.py）
定义全部合法动作 Schema：
1. 经济动作：`SWAP`, `UST_TO_LUNA`, `LUNA_TO_UST`
2. 语义动作：`SPEAK`, `VOTE`, `PROPOSE`

关键约束示例：
1. `SWAP.slippage_tolerance ∈ [0,1)`
2. `gas_price >= 0`
3. `SPEAK.mode in {new, relay, reply}`
4. `relay/reply` 必须携带 `parent_event_id`

### 5.2 护栏审计器（guardrails/secretary_auditor.py）
`SecretaryAuditor` 在执行前强制检查：
1. 参数结构合法。
2. 余额合法（本金 + gas）。
3. SWAP 自动包装 min amount（LLM 只需给容忍度）。
4. 角色权限合法。
5. Agent 输出必须是 `thought/speak/action` 三段结构。

### 5.3 调度器（orchestrator/time_orchestrator.py）

#### 5.3.1 核心状态
1. `current_tick`
2. `mempool`
3. `event_bus`
4. `protocol_fee_vault`
5. `tick_history`

#### 5.3.2 双轨循环
1. Fast Loop：语义动作即时处理（消息、提案、投票）。
2. Slow Loop：经济动作按 tick 批处理。

#### 5.3.3 `step_tick()` 顺序
1. tick +1
2. 应用“到期治理补丁”
3. 同步引擎仿真时钟
4. 先处理语义队列
5. 取出 mempool 批次
6. 按 `gas 降序 + FIFO` 排序
7. 处理上限 `max_tx_per_tick`，其余留队
8. 逐笔：预检 -> 扣 gas -> 执行 -> 回执
9. 到期提案结算
10. 产出 Tick 报告、指标、快照

#### 5.3.4 异常隔离
1. 单笔失败只影响该笔。
2. 滑点失败不影响后续单。
3. 系统不变量异常触发 `halted` 熔断。

#### 5.3.5 评估流量模式（补充）
1. `stress`：保持高压混合噪声（默认）。
2. `eval`：优先生成可执行散户交易，用于衡量防御上限。
3. 防御B支持 `warm-start`，可在首窗口提前生效并产生日志/账本证据。

---

## 6. 社会拓扑与信息信道设计（social）

### 6.1 拓扑图（network_graph.py）
1. 有向图表示“谁能听到谁”。
2. 节点属性：`agent_id`, `role`, `community_id`。
3. 支持分层混合拓扑与无标度拓扑。

### 6.2 信道管理（channel_manager.py）
1. `SYSTEM_NEWS`：全体广播。
2. `FORUM/PUBLIC_CHANNEL`：按拓扑传播。
3. `PRIVATE_CHANNEL`：点对点。

支持延迟投递：消息带 `deliver_tick`，到期入 inbox。

### 6.3 感知过滤器（perception_filter.py）
跨圈层时可触发语义衰减：
1. 数字信息泛化（精确信息丢失）。
2. 注入 `[RUMOR]/[PANIC]` 前缀（可调概率）。
3. 增加传播延迟。
4. 可接入外部小模型；不可用时自动回退规则版。

### 6.4 认知过载机制
`read_inbox(max_inbox_size)`：
1. 先按频道权重与新鲜度排序。
2. 超限时压缩并插入系统提示。
3. 被压缩消息写入 overload 日志。

---

## 7. Agent 与认知层设计（agents + cognition + runtime）

### 7.1 Agent 三层结构
1. Perception：读取和过滤信息。
2. Cognition：拼接上下文并生成决策。
3. Action：提交语义或经济动作。

### 7.2 输出协议
每次决策强制输出：
1. `thought`
2. `speak`
3. `action`

其中：
1. `thought` 仅入库，不广播。
2. `speak/action` 必须经过审计器。

### 7.3 Sleep/Wake 降本机制
若满足低风险条件，Agent 直接睡眠不调用 LLM：
1. inbox 为空
2. 价格波动低
3. 风险信号未触发
4. 未达到强制唤醒间隔

### 7.4 模型路由（llm_router.py）
1. 异构角色路由（whale/retail/project 可不同模型）。
2. 并发上限（Semaphore）。
3. 限速（令牌桶）。
4. 重试（指数退避 + 抖动）。
5. 失败降级（规则脑）。
6. 告警日志：`[LLM-WARN]`。

### 7.5 记忆流（memory_stream.py）
1. 文本记忆写入 SQLite。
2. embedding 使用本地模型。
3. 向量检索 + 规则重排。
4. 支持 FAISS，不可用时回退 numpy 索引。

### 7.6 运行时协调器（runtime/agent_runtime.py）
每 tick 批量执行：
1. 让所有 runtime agents 先决策并提交动作。
2. 调用 orchestrator 结算。
3. 统计 LLM 调用数、睡眠比率。

---

## 8. 治理演化层设计（governance）

### 8.1 治理闭环
1. `PROPOSE`：发起提案并收提案费。
2. `VOTE`：基于快照权重投票。
3. `settle_due`：窗口到期后判定通过或拒绝。
4. `compile_proposal`：提案文本转参数补丁。
5. `apply_due_updates`：下一 tick 生效。

### 8.2 票权与门槛
1. 票权仅来自 LUNA 快照余额。
2. 通过条件：
   1. 参与率 >= quorum
   2. 赞成权重 > 反对权重

### 8.3 防刷规则
1. 提案费（默认 1000 LUNA）。
2. 全局 open 提案上限。
3. 单地址 open 提案上限。

### 8.4 NLP-to-DSL 安全边界
`compiler_agent.py` 只允许白名单参数修改：
1. `engine.minting_allowed`
2. `engine.swap_fee`
3. `engine.daily_mint_cap`
4. `orchestrator.ticks_per_day`
5. `orchestrator.max_inbox_size`

禁止任意代码执行，类型和范围必须通过校验。

---

## 9. 数据存储设计（SQLite + 文件）

### 9.1 核心表
1. `ledger`：经济动作流水与快照。
2. `thought_log`：Agent 思考与审计结果。
3. `semantic_delivery_log`：消息传播链路。
4. `inbox_overload_log`：认知过载压缩日志。
5. `memory_stream`：长期记忆文本与向量。
6. `governance_proposals`：提案主表。
7. `governance_votes`：投票明细。
8. `governance_pending_updates`：待生效补丁。

### 9.2 产物文件
1. `metrics.csv`：论文指标按 tick 导出。
2. `tick_*.json`：全量状态快照。
3. `summary.json`：本次实验总结。
4. `phase5_dashboard.png`：可视化图。
5. `run_window_metrics.csv`：窗口双口径指标快照。
6. `logs/simulation_run.log`：统一运行日志。

---

## 10. 可观测性与日志设计

### 10.1 日志统一策略
1. 统一使用 Python logging。
2. 终端与文件双写。
3. 默认每 tick 心跳。

### 10.2 关键标签
1. `[BOOT]`：启动过程。
2. `[CONFIG]`：配置加载。
3. `[CHECK]`：API 预检查。
4. `[RUN]`：运行状态。
5. `[PROGRESS]`：tick 心跳。
6. `[SOCIAL]`：谣言与过载统计。
7. `[ACTION]`：散户 SWAP 汇总。
8. `[WHALE-ACTION]`：巨鲸/项目方逐笔动作。
9. `[GOV-EXEC]`：治理参数生效节点。
10. `[LLM-WARN]`：重试/降级告警。

---

## 11. 指标口径

`logger_metrics.py` 每 tick 输出：
1. `gini`
2. `tx_count / tx_success / tx_failed`
3. `panic_word_freq`
4. `peg_deviation`
5. `governance_concentration`
6. `mempool_congestion`
7. `mempool_processed`

窗口级补充（`summary.json` / `run_window_metrics.csv`）：
1. `retail_tx_success_rate_window`
2. `retail_tx_success_rate_executable_window`
3. `attacker_capped_in_window`
4. `attacker_min_effective_gas_in_window`

定义原则：
1. 指标口径必须固定，便于横向对比。
2. 同一实验中不得中途更换计算公式。

---

## 12. 参数管理与配置体系

完整参数列表见：`docs/PARAMETER_REFERENCE.md`

参数来源优先级：
1. 代码显式传参。
2. 环境变量（如 `ACE_LLM_CONFIG_PATH`, `OPENAI_API_KEY`）。
3. `config/llm_config.local.toml`
4. `config/llm_config.toml`

---

## 13. 测试与验收映射

测试目录：`tests/`

覆盖要点：
1. 引擎：守恒、极值、压力、内生价格。
2. 调度：Gas 排序、滑点隔离、时钟驱动限额。
3. 社会：过载机制、传播衰减、溯源链。
4. 认知：路由、重试降级、休眠唤醒、记忆检索。
5. 治理：LUNA 票权、防刷、下一 tick 生效、白名单安全。

验收基准：
1. 全量测试通过。
2. 关键日志标签完整输出。
3. 产物目录齐全可复盘。

---

## 14. 扩展规范（给后续开发）

### 14.1 新增经济动作
必须同步修改：
1. 动作 schema。
2. 审计器预检。
3. orchestrator 分发逻辑。
4. 指标统计与测试用例。

### 14.2 新增治理参数
必须同步修改：
1. 编译器白名单。
2. 应用逻辑。
3. 范围校验。
4. 回归测试。

### 14.3 新增角色
必须同步修改：
1. 角色画像。
2. 权限矩阵。
3. 拓扑注册逻辑。
4. 实验脚本默认构造。

---

## 15. 当前边界与后续建议

当前边界：
1. AMM 为主，未覆盖订单簿微结构。
2. 未把 token 成本纳入统一报告。
3. 治理编译器为保守白名单策略。

后续建议：
1. 增加多随机种子批量实验编排。
2. 增加置信区间统计脚本。
3. 增加对抗式治理攻击场景模板。

---

## 16. 结论
这份设计报告对应的是一个“可运行、可审计、可复现、可扩展”的多智能体 Web3 风险仿真平台。

它已经具备：
1. 严格账本与极端值稳定性。
2. 抢跑与拥堵机制复现能力。
3. 社会传播与认知过载建模能力。
4. 治理演化与参数热更新能力。

可直接支撑 Phase 1-5 的连续实验和论文图表输出。
