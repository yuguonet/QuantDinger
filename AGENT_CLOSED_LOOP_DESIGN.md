# Agent 闭环架构设计（新版）

> 基于新 agent（plan + smolagents CodeAgent）的闭环系统设计
> 日期: 2026-07-06
> 状态: 设计中

---

## 一、设计目标

**终极目标**: 减小系统 bug 或规则不完善带来的亏钱。

四个闭环各自堵一个漏洞：

| 闭环 | 堵的漏洞 | 去掉后果 |
|------|---------|---------|
| 记录闭环 | 没有执行过程数据 | 回测和溯源都废了 |
| T+N 回测闭环 | 无法区分预测对不对 | 权重失去客观依据 |
| 用户反馈闭环 | tool 坏了要等 N 轮才发现 | 期间持续亏钱 |
| 编排缓存 | 每次从零规划 | 重复消耗 LLM token |

---

## 二、新 Agent 现状

### 2.1 当前架构

```
用户输入
    │
    ▼
TaskAgent._plan(LLM#1 选工具)
    │
    ├── 无工具 → AgentBase.chat()（普通对话）
    │
    └── 有工具 → smolagents CodeAgent（筛选后的工具）
                    │
                    ├── ReAct 循环: Observe → Think → Act
                    └── final_answer() → 响应
```

### 2.2 已有可复用组件

| 组件 | 位置 | 能力 | 复用方式 |
|------|------|------|---------|
| AgentTraceRecorder | `utils/tracing.py` | 记录 run/llm/memory/tool 全过程 → JSONL | **增强**: 加 tool_call 结构化记录 |
| qd_analysis_memory | `services/analysis_memory.py` | 存分析决策 + 验证字段 + 反馈字段 | **直接复用**: 作为回测/反馈的数据源 |
| ReflectionService | `services/reflection.py` | 盘后验证历史决策 + 触发校准 | **直接复用**: 回测闭环的核心 |
| AICalibrationService | `services/ai_calibration.py` | 校准 buy/sell 阈值 | **直接复用**: 权重迭代 |
| TaskAgent | `agents/task_agent.py` | plan + CodeAgent | **增强**: 加闭环逻辑 |
| ToolRegistry | `tools/registry.py` | 自动扫描注册 | 不动 |
| QDSkillAdapter | `llm/qd_skills.py` | 技能发现 | 不动 |

### 2.3 不引入的东西

- ❌ LangGraph（当前 plan + CodeAgent 够用，不过度设计）
- ❌ qd_traces 新表（复用 qd_analysis_memory + 增强 JSONL）
- ❌ EvalNode 树（用现有 trace events 替代）
- ❌ 拆分 prepare/planner/agent/finalize 四节点（在 TaskAgent 内部实现）

---

## 三、四个闭环实现

### 3.1 记录闭环

**目标**: 记录每一步工具调用的入参出参，供回测和溯源使用。

**数据流**:
```
TaskAgent.chat()
  → _plan() 记录 plan_result
  → CodeAgent 执行
    → 每个 tool_call 记录到 trace
  → 结果写入 qd_analysis_memory
```

**实现**: 增强 `AgentTraceRecorder` + TaskAgent 中补 tool_call 记录。

**agent-del 可复用代码**:
- `chain/traced_tool.py` 的 TracedTool 包装逻辑（适配到 smolagents 的 _SmolTool）
- `chain/eval_node.py` 的 EvalNode 数据结构（简化为 dict，不建树）

**改动文件**:
- `agents/task_agent.py` — chat() 中 CodeAgent 执行后提取 tool_call 记录
- `utils/tracing.py` — AgentTraceRecorder 新增 `record_tool_call()` 便捷方法

### 3.2 T+N 回测闭环

**目标**: 盘后用实际行情验证预测方向，多次验证渐进调权。

**数据流**:
```
app 启动 → start_eval_worker() → 等到盘后 15:30
  → ReflectionService.run_verification_cycle()
    → validate_unvalidated_older_than() → 取实际行情 → 写回 was_correct/actual_return_pct
    → AICalibrationService.calibrate_market() → 校准阈值
```

**已有代码**: `services/reflection.py` + `services/ai_calibration.py` 已完整实现。

**需要补的**:
1. TaskAgent 分析结果写入 qd_analysis_memory（打通数据源）
2. ReflectionService 增加 skill/工具级权重统计（当前只做整体校准）

**改动文件**:
- `agents/task_agent.py` — chat() 末尾写入 qd_analysis_memory
- `services/reflection.py` — 增加工具级胜率统计
- `app/__init__.py` — 确认 start_eval_worker() 已调用

### 3.3 用户反馈闭环

**目标**: 用户说"不对"时，立即惩罚上一轮分析，加速检测 tool 故障。

**数据流**:
```
用户说"不对/垃圾/反了"
  → TaskAgent._detect_negative_feedback()
  → _handle_negative_feedback()
    → 查 qd_analysis_memory 最近记录
    → mark_wrong() / record_feedback()
```

**agent-del 可复用代码**:
- `_check_negative_feedback()` 的关键词匹配逻辑
- `detect_feedback_severity()` 的 severe/mild 分级
- `mark_root_wrong()` / `delete_tree()` 的惩罚逻辑（适配到 qd_analysis_memory）

**改动文件**:
- `agents/task_agent.py` — chat() 入口加反馈检测
- `services/analysis_memory.py` — 补 `get_recent_by_session()` / `mark_wrong()` 方法

### 3.4 编排缓存

**目标**: 首次走一遍后，后续相同意图直接使用工具链，跳过 LLM 选工具。

**数据流**:
```
TaskAgent._plan()
  → _query_cached_tools(user_input)
    → 从 qd_analysis_memory 查询相似意图的历史成功分析
    → 五层质量门过滤
    → 命中: 直接返回 tools，跳过 LLM
  → 未命中: 走 LLM 选工具
```

**agent-del 可复用代码**:
- `query_cached_tools()` 的 SQL 聚合逻辑
- 五层质量门的过滤条件
- `query_low_weight_tools()` 的工具权重过滤

**改动文件**:
- `agents/task_agent.py` — _plan() 开头加缓存查询
- `services/analysis_memory.py` — 补 `find_similar_successful()` / `get_tool_chain_stats()` 方法

---

## 四、需要从 agent-del 复用的代码清单

| 源文件 (agent-del) | 目标文件 (新 agent) | 复用内容 | 适配要点 |
|-------------------|-------------------|---------|---------|
| `chain/traced_tool.py` | `agents/task_agent.py` | TracedTool 包装逻辑 | 适配到 _SmolTool，不需要独立类 |
| `chain/eval_node.py` | 不需要独立文件 | EvalNode 数据结构 | 简化为 dict，用 trace events 替代树 |
| `chain/store.py` | `services/analysis_memory.py` | SQL 查询函数 | 复用 qd_analysis_memory 表，不建 qd_traces |
| `chain/evaluator.py` | `services/reflection.py` | 回测验证逻辑 | 已有 ReflectionService，补充工具级统计 |
| `chain/nodes.py` 中的反馈检测 | `agents/task_agent.py` | `_check_negative_feedback()` | 适配到 TaskAgent.chat() 入口 |
| `chain/nodes.py` 中的缓存查询 | `agents/task_agent.py` | `query_cached_tools()` | 数据源改为 qd_analysis_memory |
| `chain/graph.py` | 不需要 | LangGraph 编排 | 不引入，逻辑在 TaskAgent 内部实现 |

---

## 五、数据表设计

### 5.1 复用: qd_analysis_memory（已有）

```sql
-- 已有字段，直接复用
id, market, symbol, decision, confidence, summary,
was_correct, actual_return_pct, user_feedback, feedback_at,
raw_result  -- JSONB，存 tools_used / trace_id / session_id
```

**新增需求**:
- `raw_result` 中约定存储格式: `{"tools_used": [...], "trace_id": "...", "session_id": "..."}`
- 新增索引: `CREATE INDEX idx_am_raw_result_tools ON qd_analysis_memory USING GIN ((raw_result->'tools_used'));`

### 5.2 新增: qd_tool_weights（工具级权重）

```sql
CREATE TABLE qd_tool_weights (
    tool_name       VARCHAR(100) PRIMARY KEY,
    weight          FLOAT NOT NULL DEFAULT 1.0,
    win_rate        FLOAT,
    sample_count    INT NOT NULL DEFAULT 0,
    avg_return_pct  FLOAT,
    last_updated    TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**用途**:
- 回测闭环: 盘后统计每个工具的胜率，更新权重
- 编排缓存: 低权重工具（win_rate < 0.4）不进缓存
- 工具筛选: plan 时移除低权重工具

---

## 六、核心指标

**单位时间收益率**（不是胜率）:
```
return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days
```

这个指标同时考虑了:
- 胜率（win_rate）
- 盈亏比（avg_win / avg_loss）
- 效率（hold_days）

一个工具胜率 80% 但平均持有 30 天，不如胜率 60% 但平均持有 3 天。

---

## 七、实施顺序

```
Phase 1: 记录闭环（数据基础）
  ├── 增强 AgentTraceRecorder，加 record_tool_call()
  ├── TaskAgent.chat() 中记录 tool_call 到 trace
  ├── TaskAgent.chat() 末尾写入 qd_analysis_memory
  └── qd_analysis_memory 加索引

Phase 2: 用户反馈闭环（最快见效）
  ├── TaskAgent.chat() 入口加 _detect_negative_feedback()
  ├── services/analysis_memory.py 补 get_recent_by_session() / mark_wrong()
  └── 前端加反馈按钮（可选）

Phase 3: T+N 回测闭环（核心价值）
  ├── 创建 qd_tool_weights 表
  ├── services/reflection.py 增加工具级统计
  ├── 确认 start_eval_worker() 已调用
  └── 盘后自动更新 qd_tool_weights

Phase 4: 编排缓存（性能优化）
  ├── TaskAgent._plan() 加缓存查询
  ├── services/analysis_memory.py 补 find_similar_successful()
  ├── 五层质量门过滤
  └── 工具权重过滤（低权重工具不进 plan 候选）
```

---

## 八、关键设计决策

### Q: 为什么不引入 LangGraph?

当前 plan + smolagents CodeAgent 已经实现了"选工具 → 执行 → 循环"的完整链路。LangGraph 的价值在于：
- 显式节点图（可视化）
- 状态持久化（Checkpointer）
- 流式输出（原生支持）

但引入的代价：
- 多一层抽象，调试复杂度增加
- smolagents CodeAgent 已有 ReAct 循环，再套 LangGraph 是重复
- 当前 SSE 流式已通过 flask_app.py 实现

**结论**: 闭环逻辑在 TaskAgent 内部实现，不加编排层。如果未来需要多 Agent 协作或复杂审批流，再引入 LangGraph。

### Q: 为什么不建 qd_traces 表?

AGENT_ACCOUNTABLE.md 设计的 qd_traces 是一棵树（root_id/parent_id），适合记录 Chain→Skill→Tool 的层级关系。但新 agent 的架构是扁平的（plan → CodeAgent），没有 Chain/Skill 层级。

**替代方案**:
- 工具调用记录 → AgentTraceRecorder JSONL（已有）
- 分析决策记录 → qd_analysis_memory（已有）
- 工具级统计 → qd_tool_weights（新建，轻量）

这样不增加表复杂度，复用已有基础设施。

### Q: agent-del 的代码怎么复用?

agent-del 的核心代码（TracedTool、store、evaluator、反馈检测）逻辑是对的，但需要适配：

| agent-del 代码 | 适配点 |
|---------------|--------|
| TracedTool 类 | 不需要独立类，在 _SmolTool.forward() 中加 trace 记录即可 |
| EvalNode 树 | 不需要树结构，用 flat dict + trace events 替代 |
| store.py SQL | 表名从 qd_traces 改为 qd_analysis_memory，字段映射调整 |
| evaluator.py | 复用 ReflectionService，补工具级统计 |
| 反馈检测 | 关键词列表直接复用，匹配逻辑从 qd_traces 改为 qd_analysis_memory |
| 缓存查询 | SQL 从 qd_traces 聚合改为 qd_analysis_memory 聚合 |

---

## 九、风险和降级

| 风险 | 降级方案 |
|------|---------|
| qd_analysis_memory 数据量大，聚合慢 | 加索引 + 限制查询范围（最近 30 天） |
| 工具权重误判（样本太少） | sample_count < 10 时权重保持 1.0（不惩罚） |
| 负面反馈误判（用户说"不对"但其实对了） | 只标记，不删除；人工复核 |
| 缓存命中率低 | 先观察，如果 < 30% 则关闭缓存功能 |
