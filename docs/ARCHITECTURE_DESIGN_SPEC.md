# ACE-Sim 架构与运行设计说明（Phase 1-5）

## 1. 文档目标
这份文档用于统一项目架构理解、实验运行口径和论文复现流程。

适用范围：
1. 新成员快速上手。
2. 阶段验收对照。
3. 论文实验复盘与图表数据追溯。

参数总表请配合查阅：
[`PARAMETER_REFERENCE.md`](PARAMETER_REFERENCE.md)

## 2. 系统总览
ACE-Sim 采用五层架构：
1. 经济物理层：严格账本、双池 AMM、铸造赎回、守恒审计。
2. 执行调度层：Tick + Event 双轨、Gas 排序、异常隔离。
3. 社会传播层：拓扑传播、频道隔离、语义衰减、认知过载。
4. 认知决策层：异构 LLM 路由、休眠唤醒、记忆检索。
5. 治理演化层：提案投票、NLP-to-DSL、下一 Tick 生效。

## 3. 分层设计细节
### 3.1 经济物理层（ACE Engine）
路径：`src/ace_sim/engine/ace_engine.py`

核心能力：
1. 资产账本：`UST/LUNA/USDC`。
2. 双池结构：
   1. `Pool_A = UST/USDC`
   2. `Pool_B = LUNA/USDC`
3. 内生价格：`Pool_B` 实时价格即铸造赎回计价基准。
4. 铸造赎回：`UST_TO_LUNA`、`LUNA_TO_UST`。
5. 原子执行：任何失败不留下中间状态。
6. SQLite 留痕：每笔成功动作写入 `ledger`。

### 3.2 执行调度层（Orchestrator）
路径：`src/ace_sim/execution/orchestrator/time_orchestrator.py`

核心能力：
1. Fast Loop：处理语义动作（`SPEAK/PROPOSE/VOTE`）。
2. Slow Loop：处理经济动作（`SWAP/UST_TO_LUNA/LUNA_TO_UST`）。
3. Mempool 抢跑规则：`gas_price` 降序，Gas 相同按 FIFO。
4. Gas 成本规则：进入执行后扣费，滑点失败不退。
5. 拥堵仿真：`max_tx_per_tick` 控制每 Tick 最大结算量，剩余交易留在 mempool。

### 3.3 社会传播层（Topology & Channels）
路径：`src/ace_sim/social/`

核心能力：
1. NetworkX 关注图。
2. 公共与私有信道隔离。
3. 跨圈层衰减和延迟投递。
4. Inbox 过载保护（超限压缩并插入系统提示）。
5. 传播溯源（`parent_event_id`）。

### 3.4 认知决策层（Agent Brain）
路径：`src/ace_sim/agents/`, `src/ace_sim/cognition/`, `src/ace_sim/runtime/`

核心能力：
1. 角色异构模型路由。
2. Sleep/Wake 触发，减少无效调用。
3. 并发限流、指数退避、失败降级。
4. 本地 embedding + 向量记忆检索。
5. 输出格式固定：`thought/speak/action`。

### 3.5 治理演化层（Governance）
路径：`src/ace_sim/governance/`

核心能力：
1. 提案与投票闭环。
2. 票权口径仅看 LUNA 快照余额。
3. 防刷规则：
   1. 提案费（默认 1000 LUNA）
   2. 全局并发提案上限
   3. 单地址活跃提案上限
4. 通过后先编译成受限参数补丁，再在下一 Tick 生效。
5. 白名单参数更新，禁止越权修改。
6. 可插拔治理防御中台（Mitigation Layer）：提案写库前可接入语义过滤、优先级识别、抢占式入队策略。

## 4. 日志与可观测架构（Phase 5 增强）
路径：`scripts/visualization/phase5_governance_visualizer.py`

### 4.1 统一日志通道
1. 日志框架统一为 Python `logging`。
2. 默认双写：终端 + 文件。
3. 默认文件：`logs/simulation_run.log`。

### 4.2 日志标签规范
1. `[BOOT]`：系统加载。
2. `[CONFIG]`：配置读取与运行参数。
3. `[CHECK]`：API 预检查。
4. `[RUN]`：仿真启动、结束与产物摘要。
5. `[PROGRESS]`：每 Tick 心跳。
6. `[SOCIAL]`：流言与认知过载统计。
7. `[ACTION]`：散户 SWAP 汇总。
8. `[WHALE-ACTION]`：Whale/Project 逐笔明细。
9. `[GOV-EXEC]`：治理补丁生效日志。
10. `[LLM-WARN]`：重试、退避、降级告警。

### 4.3 Tick 级观测项
`[PROGRESS]` 固定输出：
1. Tick 进度。
2. Mempool 遗留量。
3. UST 价格（Pool_A 口径）。
4. LLM 调用累计数。

`[ACTION]` 固定输出（仅 Retail SWAP）：
1. 总笔数。
2. 滑点失败笔数。
3. 余额失败笔数。
4. 最高 Gas 出价。

`[SOCIAL]` 固定输出：
1. 本 Tick 产生流言数。
2. 本 Tick 触发认知过载人次。

`[GOV-EXEC]` 固定输出：
1. 生效参数名。
2. 新值。
3. 提案 ID。
4. 旧值（可读取时输出）。

### 4.4 自动论文图流水线（补充）
默认行为：`phase5_governance_visualizer.py` 在主仿真结束后自动调用 `paper_charts_generator.py`。  
默认落盘到本次 `output-dir`：  
1. `paper_dashboard_2x2.(png|pdf)`  
2. `chart1~chart4.(png|pdf)`  
3. `shape_report.json`  

可通过 `--no-paper-charts` 关闭自动出图。

## 5. 配置体系与参数入口
### 5.1 LLM 配置
默认路径：`config/llm_config.toml`

可配置项：
1. API Key / 代理地址。
2. 角色到模型路由。
3. 并发、重试、超时。

可通过 `ACE_LLM_CONFIG_PATH` 切换配置文件。

### 5.2 核心参数入口
1. `ACE_Engine`：`minting_allowed`、`swap_fee`、`daily_mint_cap`。
2. `Simulation_Orchestrator`：`ticks_per_day`、`max_tx_per_tick`、`default_max_inbox_size`。
3. `GovernanceModule`：`proposal_fee_luna`、`max_open_proposals`、`max_open_per_agent`、`voting_window_ticks`、`quorum_ratio`。
4. Phase 5 脚本参数：`--scenario`、`--pool-a-init`、`--shock-t1/--shock-t3/--shock-t6`、`--retail-ust-cap`、`--social-eclipse-attack`、`--governance-dos-attack`、`--enable-mitigation-a`、`--mitigation-mode`、`--ticks-per-day`、`--voting-window-ticks`、`--llm-max-concurrent`、`--no-progress`、`--progress-interval`、`--log-file`、`--log-level`。

## 6. 运行与复现实验
说明：
1. `--scenario` 默认值为 `staircase_formal_run`（可选 `default`）。
2. `--retail` 会自动约束在 `21-27` 区间。

### 6.1 环境准备
```bash
git clone https://github.com/Entropy-wz/Web3.git
cd Web3
python -m venv .venv
```

Windows:
```powershell
.venv\Scripts\activate
```

安装依赖：
```bash
pip install -U pip
pip install -e .
```

### 6.2 测试校验
```bash
pytest -q
```

### 6.3 API 预检查
```bash
python scripts/tools/check_llm_api.py --timeout 12 --output-json artifacts/preflight/llm_api_report.json
```

### 6.4 Phase 5 主实验（默认 API）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 21 --output-dir artifacts/phase5
```

### 6.5 Phase 5 阶梯式死亡螺旋场景（推荐）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --scenario staircase_formal_run --pool-a-init 10000000,10000000 --shock-t1 1000000 --shock-t3 500000 --shock-t6 300000 --retail-ust-cap 5000000 --seed 42 --output-dir artifacts/staircase_formal_api
```

### 6.6 Phase 5 离线实验（不调用 API）
```bash
python scripts/visualization/phase5_governance_visualizer.py --offline-rules --ticks 80 --retail 21 --output-dir artifacts/phase5_offline
```

### 6.7 自动论文图参数（补充）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --paper-chart-formats png,pdf --paper-chart-dpi 300 --paper-chart-font-size 14 --paper-chart-congestion-scale log
```

### 6.8 社交驱动日食攻击（补充）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --scenario staircase_formal_run --social-eclipse-attack --eclipse-attacker-id whale_1 --eclipse-trigger-tick 1 --eclipse-window-ticks 5 --eclipse-sell-ust 300000 --prompt-profile-path config/prompt_profiles/whale_eclipse_extreme.json --output-dir artifacts/eclipse_attack
```

### 6.9 治理 DoS 占坑攻击（补充）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --scenario staircase_formal_run --governance-dos-attack --dos-whale-luna 4000 --dos-sell-ust 300000 --output-dir artifacts/gov_dos_attack
```

### 6.10 防御A（SAGG）实验（补充）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 40 --retail 24 --scenario staircase_formal_run --governance-dos-attack --mitigation-mode semantic --output-dir artifacts/sagg40_demo --no-paper-charts
```

## 7. 数据存储与结果落盘
### 7.1 SQLite 数据
1. `ledger`
2. `semantic_delivery_log`
3. `inbox_overload_log`
4. `thought_log`
5. `governance_proposals`
6. `governance_votes`
7. `governance_pending_updates`

### 7.2 产物目录（Phase 5）
1. `artifacts/phase5/metrics.csv`
2. `artifacts/phase5/phase5_dashboard.png`
3. `artifacts/phase5/summary.json`
4. `artifacts/phase5/checkpoints/tick_*.json`
5. `logs/simulation_run.log`
6. `artifacts/phase5/paper_dashboard_2x2.(png|pdf)`
7. `artifacts/phase5/chart1~chart4.(png|pdf)`
8. `artifacts/phase5/shape_report.json`

补充说明：仓库默认 `.gitignore` 会忽略 `artifacts` 大体量产物与本地虚拟环境（`.venv/`、`venv/`），避免备份体积失控。

## 8. 论文指标口径
按 Tick 输出：
1. `gini`
2. `panic_word_freq`
3. `peg_deviation`
4. `governance_concentration`
5. `mempool_congestion`
6. `mempool_processed`

## 9. 验收检查清单
1. `pytest -q` 全部通过。
2. Phase 5 运行后，`summary.json`、`metrics.csv`、`phase5_dashboard.png`、`simulation_run.log` 均生成。
3. 日志中可看到 `[SOCIAL]`、`[ACTION]`、`[WHALE-ACTION]`、`[GOV-EXEC]` 四类增强标签。
4. 自动论文图开启时，`paper_dashboard_2x2`、`chart1~chart4` 与 `shape_report.json` 均生成。

## 10. 当前结论
当前版本已经具备“可运行、可审计、可复盘、可出图”的完整闭环，可直接支撑 Phase 1-5 的连续实验与论文图表产出。
