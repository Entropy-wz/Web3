# ACE-Sim Agent 设计说明（agent_design）

## 1. 文档定位
本文件专门描述多智能体系统中最关键的两部分：
1. LLM 的 Prompt 设计。
2. Agent 的 JSON 输出协议设计。

目标：让研究者和工程同学拿到文档即可理解并复用本项目的 Agent 决策接口。

---

## 2. 设计原则

### 2.1 为什么这部分是核心
在多智能体仿真中，LLM 直接影响动作和市场轨迹。若 Prompt 与输出协议不严谨，会出现：
1. 输出不可执行。
2. 内部思考泄露，污染社交传播实验。
3. 动作参数不规范，导致回放不可复现。
4. LLM 数学误差放大，交易失败率失真。

### 2.2 解决思路
我们采用“三层控制”：
1. Prompt 层：输入上下文标准化。
2. 协议层：输出结构强约束。
3. 护栏层：执行前硬校验。

---

## 3. Agent 决策链路
每个 Agent 在一个 tick 的执行顺序：
1. 读取公共状态、inbox、记忆检索结果。
2. 拼装 Prompt 并调用 LLM。
3. 产出 `thought/speak/action` JSON。
4. 通过秘书审计（结构、权限、参数、余额、gas）。
5. 合法动作提交到执行层。

对应代码：
1. Prompt/结构化输出：`src/ace_sim/cognition/llm_brain.py`
2. Agent 执行主流程：`src/ace_sim/agents/base_agent.py`
3. 审计拦截：`src/ace_sim/execution/guardrails/secretary_auditor.py`
4. 动作 schema：`src/ace_sim/execution/action_registry/actions.py`

---

## 4. Prompt 设计

### 4.1 输入组成
Prompt 由固定段落组成：
1. Role：角色（whale/retail/project）。
2. Hidden goals：角色私有目标。
3. Risk threshold：风险阈值。
4. Current state：环境压缩状态。
5. Inbox：当 tick 可见消息。
6. Relevant memory：检索出的历史记忆。
7. Allowed actions：允许动作清单。
8. 输出要求：严格 JSON，禁止额外字段。

### 4.2 Prompt 模板
```text
You are a Web3 market participant in a multi-agent crisis simulation.
Role: {role}
Hidden goals: {hidden_goals_json}
Risk threshold: {risk_threshold}
Current state: {public_state_json}
Inbox: {inbox_json}
Relevant memory: {memory_json}
Allowed actions: {allowed_actions_json}
Return strict JSON only:
{"thought":"...","speak":{...}|null,"action":{...}|null}.
No markdown, no extra keys.
```

设计目的：
1. 把模型任务聚焦为“策略选择”，而不是开放写作。
2. 保证不同模型后端下输出行为尽量一致。

---

## 5. JSON 顶层协议

### 5.1 强制结构
```json
{
  "thought": "string",
  "speak": { "...": "..." } | null,
  "action": { "...": "..." } | null
}
```

### 5.2 语义定义
1. `thought`：内部独白，仅存档，不广播。
2. `speak`：社交发言，进入语义通道。
3. `action`：执行动作，进入调度器。

### 5.3 约束
1. `thought` 必填，且必须是非空字符串。
2. `speak` 只能是对象或 null。
3. `action` 只能是对象或 null。
4. 顶层不允许额外字段。

---

## 6. speak 协议设计

### 6.1 基础格式
```json
{
  "target": "forum",
  "message": "UST可能继续承压",
  "mode": "new"
}
```

### 6.2 可选字段
1. `channel`：显式频道（如 `PUBLIC_CHANNEL` / `PRIVATE_CHANNEL`）。
2. `receiver`：私信接收者。
3. `parent_event_id`：传播溯源 ID。

### 6.3 强规则
1. `mode` 仅允许 `new/relay/reply`。
2. `mode` 为 `relay/reply` 时，`parent_event_id` 必填。
3. 私有频道必须提供 `receiver`。

---

## 7. action 协议设计

### 7.1 通用结构
```json
{
  "action_type": "...",
  "params": { ... },
  "gas_price": "仅经济动作需要"
}
```

### 7.2 经济动作
1. `SWAP`
```json
{
  "action_type": "SWAP",
  "params": {
    "pool_name": "Pool_A",
    "token_in": "UST",
    "amount": "100",
    "slippage_tolerance": "0.05"
  },
  "gas_price": "8"
}
```

2. `UST_TO_LUNA`
```json
{
  "action_type": "UST_TO_LUNA",
  "params": {
    "amount_ust": "50"
  },
  "gas_price": "6"
}
```

3. `LUNA_TO_UST`
```json
{
  "action_type": "LUNA_TO_UST",
  "params": {
    "amount_luna": "20"
  },
  "gas_price": "6"
}
```

### 7.3 治理动作
1. `VOTE`
```json
{
  "action_type": "VOTE",
  "params": {
    "proposal_id": "proposal_xxx",
    "decision": "approve"
  }
}
```

2. `PROPOSE`
```json
{
  "action_type": "PROPOSE",
  "params": {
    "proposal_text": "disable minting and set swap fee to 0.01"
  }
}
```

---

## 8. 关键设计点：滑点输入改造
为了避免 LLM 在 AMM 数学上频繁出错：
1. LLM 只输出 `slippage_tolerance`。
2. 后端调用估算接口自动计算执行下限。
3. 交易对象中固化 `min_amount_out` 后再进入结算。

结果：
1. 大幅降低“模型算错导致失败”。
2. 保留策略表达能力（风险偏好）。

---

## 9. 审计与护栏
所有模型输出必须经过 `SecretaryAuditor`：
1. 顶层结构审计。
2. schema 审计。
3. 角色权限审计。
4. 余额和 gas 预检。
5. 经济动作必须携带 `gas_price`。

执行层规则：
1. 进入执行阶段后，gas 真实扣除。
2. 即使滑点失败也不退 gas。

---

## 10. thought 字段隔离（论文关键）
`thought` 是内部认知，不对外传播：
1. 只写入 `thought_log`。
2. 不进入 `event_bus`。
3. 不被其他 Agent 接收。

这保证可以独立研究：
1. “内心判断”与“外部行为”的偏差。
2. 信息不对称下的表达策略。

---

## 11. 鲁棒性与降级
当模型输出非法或 API 异常：
1. 路由层先重试（退避 + 抖动）。
2. 超过重试后降级规则脑。
3. 降级输出仍遵守相同 JSON 协议。
4. 单个 Agent 失败不阻断整轮仿真。

---

## 12. 复现实验建议
建议在论文附录固定以下版本信息：
1. Prompt 模板版本。
2. JSON 协议版本。
3. 动作 schema 版本。
4. 审计规则版本。
5. 角色模型路由配置。
6. 随机种子与关键参数。

---

## 13. 总结
本项目 Agent 设计的核心思想是：
“让 LLM 负责策略意图，让系统负责格式约束、风险控制和执行正确性。”

这套设计保证了多智能体实验的可执行性、可审计性和可复现性。
