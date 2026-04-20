# ACE-Sim: Web3 多智能体经济与治理安全仿真平台

![python](https://img.shields.io/badge/Python-3.10%2B-blue)
![tests](https://img.shields.io/badge/Tests-69%20Passing-brightgreen)
![phase](https://img.shields.io/badge/Phase-1~5-success)
![llm](https://img.shields.io/badge/LLM-API%20Default-orange)

## 1. 项目简介
ACE-Sim 是一个用于复现 Web3 极端风险事件的实验平台，重点覆盖三类机制联动：
1. 经济机制：脱锚、铸造赎回、流动性冲击。
2. 执行机制：Gas 抢跑、滑点失败、拥堵积压。
3. 社会机制：信息传播、语义失真、认知过载。

平台可用于论文实验、机制对比和可追溯复盘。

## 2. 当前完成范围（Phase 1-5）
1. Phase 1：经济物理层（高精度账本、双池、铸造赎回、SQLite流水）。
2. Phase 2：Tick + Event 双轨调度（mempool 排序、Gas 成本、异常隔离）。
3. Phase 3：社会拓扑与信息信道（跨圈层衰减、过载压缩、传播溯源）。
4. Phase 4：LLM 认知层（异构模型路由、休眠唤醒、限流重试、本地记忆）。
5. Phase 5：治理演化与论文看板（NLP-to-DSL、下一 Tick 生效、指标导出）。

## 3. 目录结构
```text
web3v2/
├─ src/ace_sim/
│  ├─ engine/                 # 经济物理引擎
│  ├─ execution/              # 动作注册、护栏、调度器
│  ├─ social/                 # 拓扑、频道、感知过滤
│  ├─ agents/                 # 智能体定义
│  ├─ cognition/              # LLM 路由、记忆流
│  ├─ governance/             # 治理模块、指标、快照
│  └─ runtime/                # Agent 运行时
├─ scripts/
│  ├─ visualization/          # 各阶段可视化脚本
│  ├─ reports/                # 人类可读报告
│  └─ tools/                  # 工具脚本（含 API 预检）
├─ tests/                     # 回归测试
├─ config/                    # 配置样例
├─ docs/                      # 架构与设计文档
├─ logs/                      # 运行日志
├─ artifacts/                 # 实验产出
└─ data/                      # 本地数据（默认不进 Git）
```

## 4. 快速开始
### 4.1 安装
```bash
git clone https://github.com/Entropy-wz/Web3.git
cd Web3
python -m venv .venv
```

Windows:
```powershell
.venv\Scripts\activate
```

Linux/macOS:
```bash
source .venv/bin/activate
```

安装依赖：
```bash
pip install -U pip
pip install -e .
```

如果你需要完整能力（包含本地向量记忆）：
```bash
pip install -e .[full]
```

### 4.2 自检
```bash
pytest -q
```

## 5. API 接入与模型配置
默认读取：`config/llm_config.toml`

建议做法：
1. 密钥走环境变量，不直接写死到版本库文件。
2. 如需中转站，在配置里填写 `base_url`。
3. 若有私有配置，使用 `ACE_LLM_CONFIG_PATH` 指向本地文件。

示例（Windows）：
```powershell
$env:OPENAI_API_KEY="你的key"
$env:ACE_LLM_CONFIG_PATH="D:/exp_all/web3v2/config/llm_config.local.toml"
```

可先做 API 连通性检查：
```bash
python scripts/tools/check_llm_api.py --timeout 12 --output-json artifacts/preflight/llm_api_report.json
```

完整参数总表（建议配合实验记录使用）：
[`docs/PARAMETER_REFERENCE.md`](docs/PARAMETER_REFERENCE.md)

## 6. 主要运行命令
说明：
1. `--scenario` 默认值为 `staircase_formal_run`（可选 `default`）。
2. `--retail` 会自动约束在 `21-27` 区间。
3. `phase5_governance_visualizer.py` 默认会在仿真结束后自动生成论文图（2x2 + 4单图，PNG+PDF）。

### 6.1 Phase 5 治理与看板（默认 API 模式）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 21 --output-dir artifacts/phase5
```

### 6.2 Phase 5 阶梯式死亡螺旋场景（推荐）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --scenario staircase_formal_run --pool-a-init 10000000,10000000 --shock-t1 1000000 --shock-t3 500000 --shock-t6 300000 --retail-ust-cap 5000000 --seed 42 --output-dir artifacts/staircase_formal_api
```

### 6.3 Phase 5 离线规则模式（不走 API）
```bash
python scripts/visualization/phase5_governance_visualizer.py --offline-rules --ticks 80 --retail 21 --output-dir artifacts/phase5_offline
```

### 6.4 常用日志参数
```bash
python scripts/visualization/phase5_governance_visualizer.py \
  --ticks 80 \
  --retail 21 \
  --output-dir artifacts/phase5 \
  --progress-interval 1 \
  --log-file logs/simulation_run.log \
  --log-level INFO
```

可关闭心跳日志：`--no-progress`

### 6.5 关闭自动论文图（可选）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 21 --output-dir artifacts/phase5 --no-paper-charts
```

### 6.6 单独重跑论文图（可选）
```bash
python scripts/visualization/paper_charts_generator.py --metrics artifacts/phase5/metrics.csv --summary artifacts/phase5/summary.json --db artifacts/phase5/phase5_trace.sqlite3 --formats png,pdf --dpi 300
```

### 6.7 社交驱动内存池日食攻击（可选）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --scenario staircase_formal_run --social-eclipse-attack --eclipse-attacker-id whale_1 --eclipse-trigger-tick 1 --eclipse-window-ticks 5 --eclipse-sell-ust 300000 --prompt-profile-path config/prompt_profiles/whale_eclipse_extreme.json --output-dir artifacts/eclipse_attack
```

### 6.8 治理 DoS 占坑攻击（可选）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 80 --retail 24 --scenario staircase_formal_run --governance-dos-attack --dos-whale-luna 4000 --dos-sell-ust 300000 --output-dir artifacts/gov_dos_attack
```

### 6.9 防御 A（语义治理网关，SAGG）（可选）
```bash
python scripts/visualization/phase5_governance_visualizer.py --ticks 40 --retail 24 --scenario staircase_formal_run --governance-dos-attack --mitigation-mode semantic --output-dir artifacts/sagg40_demo --no-paper-charts
```

## 7. 日志与可观测性（Phase 5 增强）
所有日志统一写入终端 + 文件（默认 `logs/simulation_run.log`）。

核心标签：
1. `[BOOT]/[CONFIG]/[CHECK]/[RUN]`：启动与配置过程。
2. `[PROGRESS]`：每 Tick 心跳（Tick、Mempool、UST价格、LLM调用数）。
3. `[SOCIAL]`：流言传播与认知过载统计。
4. `[ACTION]`：散户 SWAP 汇总（总笔数、滑点失败、余额失败、最高 Gas）。
5. `[WHALE-ACTION]`：Whale/Project 的 SWAP 与 SPEAK 逐笔明细。
6. `[GOV-EXEC]`：治理补丁在下一 Tick 生效时的参数变更日志。
7. `[LLM-WARN]`：模型重试、退避、降级告警。

## 8. 数据存储与产物
### 8.1 SQLite（核心可审计数据）
1. `ledger`：经济动作流水与状态快照。
2. `semantic_delivery_log`：语义传播明细。
3. `inbox_overload_log`：过载压缩记录。
4. `thought_log`：智能体思考与审计结果。
5. `governance_proposals` / `governance_votes` / `governance_pending_updates`：治理全流程。

### 8.2 输出目录（以 Phase 5 为例）
1. `artifacts/phase5/metrics.csv`
2. `artifacts/phase5/phase5_dashboard.png`
3. `artifacts/phase5/summary.json`
4. `artifacts/phase5/checkpoints/tick_*.json`
5. `artifacts/phase5/paper_dashboard_2x2.(png|pdf)`
6. `artifacts/phase5/chart1~chart4.(png|pdf)`
7. `artifacts/phase5/shape_report.json`
8. `paper/防御A治理效果.md`（防御A实验结果汇总表）

### 8.3 Git 备份注意事项（小补充）
1. 默认不会把 `artifacts` 下的大体量实验产物提交到 Git。
2. 默认不会把本地虚拟环境目录（如 `.venv/`、`venv/`）提交到 Git。
3. 如果历史上已经跟踪过大文件，先执行：`git rm -r --cached artifacts data .venv venv`，再重新提交。

## 9. 论文指标口径
当前稳定输出：
1. `gini`
2. `panic_word_freq`
3. `peg_deviation`
4. `governance_concentration`
5. `mempool_congestion`
6. `mempool_processed`

## 10. 复现实验建议
1. 固定随机种子，跑多组重复实验。
2. 同时保存 `metrics.csv + summary.json + simulation_run.log`。
3. 以 Tick 为统一时间轴，对齐经济、治理、舆情三个视角做图。

## 11. 引用
```bibtex
@misc{ace_sim_2026,
  title        = {ACE-Sim: A Multi-Agent Web3 Economic and Governance Security Sandbox},
  author       = {Your Name and Collaborators},
  year         = {2026},
  howpublished = {\url{https://github.com/Entropy-wz/Web3}},
  note         = {Paper companion codebase}
}
```
