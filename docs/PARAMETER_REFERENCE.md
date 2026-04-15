# ACE-Sim 参数清单（Parameter Reference）

本文档集中记录当前项目可调参数，按“参数名 / 可选值 / 作用”给出。

## 1. 经济引擎参数（ACE_Engine）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `db_path` | 任意本地 SQLite 路径（默认 `ace_engine.sqlite3`） | 指定账本数据库文件位置。 |
| `pool_a_reserves` | 二元组 `(UST, USDC)`，均需 `>0`（默认 `("1000000","1000000")`） | 初始化 `Pool_A(UST/USDC)` 流动性。 |
| `pool_b_reserves` | 二元组 `(LUNA, USDC)`，均需 `>0`（默认 `("1000000","1000000")`） | 初始化 `Pool_B(LUNA/USDC)` 流动性。 |
| `engine_config.minting_allowed` | `True/False`（默认 `True`） | 控制 `UST<->LUNA` 铸造赎回通道是否开放。 |
| `engine_config.swap_fee` | `Decimal`，范围 `[0,1)`（默认 `0.0`） | AMM 输入侧手续费。 |
| `engine_config.daily_mint_cap` | `Decimal>=0` 或 `None`（默认 `1000000`） | 每个仿真日 `UST_TO_LUNA` 累计上限；`None` 表示不限制。 |
| `set_simulation_clock.current_tick` | `int>=0` | 写入引擎仿真时间。 |
| `set_simulation_clock.ticks_per_day` | `int>0` | 规定多少 tick 算一天，用于日限额窗口。 |

## 2. 执行调度参数（Simulation_Orchestrator）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `ticks_per_day` | `int>0`（默认 `100`） | 仿真时间日历粒度。 |
| `max_tx_per_tick` | `int>0`（默认 `50`） | 每个 tick 最多处理的经济交易数。 |
| `default_max_inbox_size` | `int>0`（默认 `5`） | 每个 agent 每 tick 默认可读消息上限。 |
| `secretary` | `SecretaryAuditor` 或 `None` | 动作预检、余额与权限护栏。 |
| `topology` | `SocialNetworkGraph` 或 `None` | 社交拓扑对象。 |
| `perception_filter` | `PerceptionFilter` 或 `None` | 语义衰减与延迟过滤器。 |
| `channel_manager` | `ChannelManager` 或 `None` | 频道路由管理器。 |
| `governance` | `GovernanceModule` 或 `None` | 治理流程模块。 |
| `metrics_logger` | `LoggerMetrics` 或 `None` | 每 tick 指标记录。 |
| `state_checkpoint` | `StateCheckpoint` 或 `None` | 每 tick 全量快照导出。 |
| `set_ticks_per_day(...)` | `int>0` | 运行中修改 `ticks_per_day`。 |
| `set_default_max_inbox_size(...)` | `int>0` | 运行中修改默认 inbox 上限。 |

## 3. 交易/语义动作参数（Action Schema）

### 3.1 经济动作

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `SWAP.pool_name` | `Pool_A` 或 `Pool_B` | 指定交易池。 |
| `SWAP.token_in` | `UST/LUNA/USDC`（且必须属于对应池） | 输入资产类型。 |
| `SWAP.amount` | `Decimal>0` | 交易输入数量。 |
| `SWAP.slippage_tolerance` | `Decimal`，范围 `[0,1)` | 滑点容忍度（由系统包装成 `min_amount_out`）。 |
| `UST_TO_LUNA.amount_ust` | `Decimal>0` | 销毁 UST 数量。 |
| `LUNA_TO_UST.amount_luna` | `Decimal>0` | 销毁 LUNA 数量。 |
| `gas_price`（经济动作） | `Decimal>=0` | 抢跑排序权重与真实 gas 扣费。 |

### 3.2 语义/治理动作

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `SPEAK.target` | 任意非空字符串 | 目标频道语义入口（如 `forum/public/private`）。 |
| `SPEAK.message` | 任意非空字符串 | 发言文本。 |
| `SPEAK.channel` | 可选；非空字符串 | 显式频道（优先于 `target` 映射）。 |
| `SPEAK.receiver` | 私信场景必填 | 私有信道接收者。 |
| `SPEAK.mode` | `new/relay/reply`（默认 `new`） | 发言类型。 |
| `SPEAK.parent_event_id` | 可选；`relay/reply` 时必填 | 传播溯源链父事件。 |
| `VOTE.proposal_id` | 非空字符串 | 目标提案 ID。 |
| `VOTE.decision` | `approve/reject/abstain` | 投票选项。 |
| `PROPOSE.proposal_text` | 非空字符串 | 提案文本。 |

## 4. 治理模块参数（GovernanceModule）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `proposal_fee_luna` | `Decimal>0`（默认 `1000`） | 发起提案手续费（LUNA）。 |
| `max_open_proposals` | `int>0`（默认 `3`） | 全局同时 open 提案上限。 |
| `max_open_per_agent` | `int>0`（默认 `1`） | 单地址同时 open 提案上限。 |
| `voting_window_ticks` | `int>0`（默认 `20`） | 投票窗口长度。 |
| `quorum_ratio` | `Decimal`，范围 `[0,1]`（默认 `0.3`） | 法定参与率门槛。 |

### 4.1 治理白名单参数（NLP-to-DSL 可改）

| 参数名 | 可选参数 | 作用 |
|---|---|---|
| `engine.minting_allowed` | `True/False` | 开关铸造赎回通道。 |
| `engine.swap_fee` | `Decimal`，推荐 `[0,1)` | 修改交易费率。 |
| `engine.daily_mint_cap` | `Decimal>=0` 或 `None` | 修改每日铸造上限。 |
| `orchestrator.ticks_per_day` | `int>0` | 修改仿真日历步长。 |
| `orchestrator.max_inbox_size` | `int>0` | 修改默认认知带宽上限。 |

## 5. 社会传播参数

### 5.1 感知过滤器（PerceptionFilter）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `cross_community_delay_ticks` | `int>=0`（默认 `2`） | 跨圈层传播固定延迟。 |
| `prefix_probability` | `float`，范围 `[0,1]`（默认 `0.3`） | 规则衰减前缀注入概率。 |
| `seed` | 任意整数（默认 `42`） | 规则衰减随机性复现。 |
| `model_adapter` | 可选模型适配器 | 可插拔语义改写模型，失败自动回退规则版。 |

### 5.2 拓扑构建（SocialNetworkGraph）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `build_layered_mixed_topology.seed` | `int`（默认 `42`） | 分层混合拓扑随机种子。 |
| `build_scale_free_topology.seed` | `int`（默认 `42`） | 无标度拓扑随机种子。 |
| `build_scale_free_topology.m` | `int>=1`（默认 `2`） | 无标度图每个新节点连接边数。 |
| `reachable_listeners.max_distance` | `int` 或 `None` | 限制传播最远跳数。 |

## 6. 智能体认知参数

### 6.1 角色画像（AgentProfile / AttentionPolicy）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `llm_backend` | 如 `openai/local/rule/custom` | 指定模型后端。 |
| `llm_model` | 任意模型名 | 指定模型版本。 |
| `risk_threshold` | `Decimal` | 风险触发阈值。 |
| `hidden_goals` | 文本列表 | 角色隐含目标。 |
| `attention_policy.price_change_threshold` | `Decimal`（角色默认不同） | 价格波动唤醒阈值。 |
| `attention_policy.risk_wake_threshold` | `Decimal` | 风险唤醒阈值。 |
| `attention_policy.force_wake_interval` | `int` | 最长强制唤醒间隔。 |
| `attention_policy.memory_top_k` | `int` | Prompt 注入记忆条数上限。 |

### 6.2 LLM Router / Brain

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `max_concurrent` | `int>0`（默认来自配置 `5`） | 最大并发请求数。 |
| `bucket_capacity` | `int>0`（默认 `10`） | 令牌桶容量。 |
| `bucket_refill_rate_per_sec` | `float>0`（默认 `5.0`） | 令牌桶补充速率。 |
| `max_retries` | `int>=0`（默认 `3`） | 最大重试次数。 |
| `base_backoff_seconds` | `float`（默认 `0.25`） | 指数退避基准时长。 |
| `jitter_seconds` | `float`（默认 `0.15`） | 退避随机抖动上限。 |
| `default_timeout`（LLMBrain） | `float`（默认配置 `20.0`） | 单次模型调用默认超时。 |

### 6.3 记忆流（MemoryStream）

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `db_path` | SQLite 路径 | 记忆库持久化位置。 |
| `embedding_provider` | 可选自定义向量器 | 替换默认本地 embedding。 |
| `vector_dim` | `int`（默认 `384`） | 向量维度。 |
| `add_memory.price_shock` | `float`（默认 `0`） | 重要性评分中的价格冲击因子。 |
| `add_memory.risk_relevance` | `float`（默认 `0`） | 重要性评分中的风险相关因子。 |
| `add_memory.importance` | `float` 或 `None` | 手动指定重要性分数。 |
| `query.top_k` | `int>0`（默认 `5`） | 返回记忆条数。 |
| `query.current_tick` | `int` 或 `None` | 时间衰减参照 tick。 |

## 7. LLM 配置文件参数（`config/llm_config.toml`）

### 7.1 `[router]`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `max_concurrent` | `int>0`（默认 `5`） | 最大并发请求数。 |
| `bucket_capacity` | `int>0`（默认 `10`） | 限流桶容量。 |
| `bucket_refill_rate_per_sec` | `float>0`（默认 `5.0`） | 限流桶补充速度。 |
| `max_retries` | `int>=0`（默认 `3`） | 最大重试次数。 |
| `base_backoff_seconds` | `float`（默认 `0.25`） | 退避基础时间。 |
| `jitter_seconds` | `float`（默认 `0.15`） | 抖动时间。 |
| `default_timeout` | `float`（默认 `20.0`） | 默认调用超时。 |

### 7.2 `[providers.openai]`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `api_key` | 字符串或空（默认空） | 直接填写密钥。 |
| `api_key_env` | 环境变量名（默认 `OPENAI_API_KEY`） | 从环境变量取密钥。 |
| `base_url` | URL 或空 | API 中转/代理入口。 |
| `organization` | 字符串或空 | OpenAI 组织字段。 |
| `project` | 字符串或空 | OpenAI 项目字段。 |

### 7.3 `[roles.*]`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `roles.whale.backend/model` | 默认 `openai/gpt-4o` | 巨鲸角色路由。 |
| `roles.retail.backend/model` | 默认 `openai/gpt-4o-mini` | 散户角色路由。 |
| `roles.project.backend/model` | 默认 `openai/gpt-4o-mini` | 项目方角色路由。 |

## 8. 环境变量参数

| 参数名 | 可选参数 | 作用 |
|---|---|---|
| `OPENAI_API_KEY` | `sk-...` | 默认 API 密钥来源。 |
| `ACE_LLM_CONFIG_PATH` | 本地 TOML 路径 | 覆盖默认配置文件位置。 |

## 9. 脚本启动参数（CLI）

### 9.1 `scripts/visualization/phase5_governance_visualizer.py`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `--ticks` | `int`（默认 `80`） | 仿真 tick 数。 |
| `--retail` | `int`（默认 `30`） | 散户数量。 |
| `--seed` | `int`（默认 `42`） | 随机种子。 |
| `--output-dir` | 路径（默认 `artifacts/phase5`） | 产物输出目录。 |
| `--offline-rules` | 开关（默认关闭） | 启用离线规则模式（不走 API）。 |
| `--llm-agent-count` | `int`（默认 `12`） | 由运行时控制的 agent 数量。 |
| `--preflight-timeout` | `float`（默认 `12.0`） | API 预检查单角色超时。 |
| `--max-inbox-size` | `int`（默认 `5`） | 每 tick 每 agent 的 inbox 上限。 |
| `--no-progress` | 开关（默认关闭） | 关闭每 tick 心跳日志。 |
| `--progress-interval` | `int>0`（默认 `1`） | 心跳日志间隔 tick。 |
| `--log-file` | 路径（默认 `logs/simulation_run.log`） | 日志文件位置。 |
| `--log-level` | `DEBUG/INFO/WARNING/ERROR`（默认 `INFO`） | 日志级别。 |

### 9.2 `scripts/tools/check_llm_api.py`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `--config` | TOML 路径或空 | 指定 LLM 配置文件。 |
| `--timeout` | `float`（默认 `12.0`） | 每个模型检查超时。 |
| `--output-json` | 路径或空 | 输出 JSON 检查报告。 |
| `--stop-on-first-fail` | 开关（默认关闭） | 首次失败即停止。 |

### 9.3 `scripts/visualization/phase2_orchestrator_visualizer.py`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `--ticks` | `int`（默认 `80`） | 仿真轮数。 |
| `--num-retail` | `int`（默认 `20`） | 散户数量。 |
| `--seed` | `int`（默认 `17`） | 随机种子。 |
| `--ticks-per-day` | `int`（默认 `100`） | 仿真日历步长。 |
| `--output-dir` | 路径（默认 `artifacts/phase2`） | 产物目录。 |

### 9.4 `scripts/visualization/phase3_topology_visualizer.py`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `--ticks` | `int`（默认 `50`） | 仿真轮数。 |
| `--communities` | `int`（默认 `3`） | 社区数。 |
| `--retail-per-community` | `int`（默认 `8`） | 每社区散户数。 |
| `--seed` | `int`（默认 `23`） | 随机种子。 |
| `--output-dir` | 路径（默认 `artifacts/phase3`） | 产物目录。 |

### 9.5 `scripts/visualization/ace_conservation_visualizer.py`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `--agents` | `int`（默认 `100`） | 虚拟账户数。 |
| `--rounds` | `int`（默认 `1000`） | 随机操作轮数。 |
| `--seed` | `int`（默认 `42`） | 随机种子。 |
| `--sample-interval` | `int`（默认 `10`） | 每隔多少轮采样一次。 |
| `--daily-mint-cap` | `Decimal` 或 `none`（默认 `1000000`） | 每日铸造上限（或不设限）。 |
| `--output-dir` | 路径（默认 `artifacts/conservation`） | 产物目录。 |

### 9.6 `scripts/reports/ace_human_report.py`

| 参数名 | 可选参数（默认） | 作用 |
|---|---|---|
| `--db` | SQLite 路径（默认 `data/sqlite/ace_demo.sqlite3`） | 生成人类可读流水报告。 |

## 10. 建议优先调参顺序

| 参数名 | 可选参数（建议） | 作用 |
|---|---|---|
| `max_tx_per_tick` | 先 `50`，再尝试 `20/100` | 控制拥堵强度。 |
| `swap_fee` | `0.0 -> 0.01 -> 0.03` | 测试交易摩擦和抛压反馈。 |
| `default_max_inbox_size` | `5 -> 3 -> 1` | 加强认知过载与信息丢失。 |
| `proposal_fee_luna` | `1000` 基线，向上测试 | 抑制治理垃圾提案。 |
| `prefix_probability` | `0.3 -> 0.5` | 放大跨圈层恐慌化。 |
| `llm_agent_count` | `12 -> 24` | 增加 LLM 决策参与密度。 |

---

如需锁定“论文复现实验参数包”，建议把本文件中的关键参数复制到单独实验配置表（CSV/TOML）并随图表一起归档。
