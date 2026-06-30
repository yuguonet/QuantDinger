# Agent 可追责架构设计

> 最后更新: 2026-06-30（v4.5 — 意图识别细化 + Tool/Skill 回测引擎 + 高胜率股票记录）
> 状态: 实施中（LangGraph 版本）
> 仓库: https://github.com/yuguonet/QuantDinger

## 一、设计原则

### 1. LangGraph 是编排层，Agent 是执行者
- **LangGraph StateGraph** 负责节点编排、状态管理、持久化
- **smolagents CodeAgent** 保持完整推理-行动-观察循环（ReAct）
- **Planner 主要选工具，兼做结论检测**：正常情况只选工具；当 Agent 上一步已输出结论（`_has_conclusion()`）时，Planner 直接结束，不再调 LLM#2
- **Agent 只执行，返回原始数据**：`final_answer()` 就是最终输出，不做二次汇总
- **路由做兜底判断**：`route_after_agent` 在步数超限或 Agent 输出含结论时强制结束
- **兼容性**：Tool 完全兼容 OpenAI Function Calling 标准（JSON Schema）；Skill 兼容 Anthropic SKILL 标准

### 1.1 设计终极目标
- **最大实现可复测**：同样的输入条件能重放 → 才能验证决策对不对 → 才能迭代权重
- **最终目的**：减小系统 bug 或规则不完善带来的亏钱
- 三个闭环 + qd_traces 缓存各自堵一个漏洞，复杂但必要

### 2. EvalNode 树是审计日志，不是执行引擎
- Agent 执行过程中，每一步自动构建 EvalNode 树
- 树记录"发生了什么"，不决定"应该发生什么"
- 执行完 → 树存库 → 盘后回溯验证 → 权重迭代

### 3. 三层追责不变
- **Chain 层**：agent 的整体决策（最终 action/score/direction）
- **Skill 层**：每次 call_skill 的分析报告（标准化 SkillReport）
- **Tool 层**：每次工具调用的入参出参

### 4. 可回测 > 可解释 > 可复现
- 优先保证每个决策能和实际盈亏对比
- 其次保证能追溯"为什么做了这个决定"
- 可复现靠数据快照 + LangGraph Checkpointer，不靠砍掉 agent 自适应

## 二、架构总览（LangGraph + 3-LLM）

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  LangGraph StateGraph（graph.py）                            │
│                                                              │
│  ┌──────────┐        ┌──────────┐                            │
│  │ prepare  │───────→│ planner  │──skip──→ finalize → END    │
│  │ (LLM#1)  │        │ (LLM#2)  │                            │
│  └──────────┘        └────┬─────┘                            │
│                           │ run                               │
│                           ▼                                   │
│                     ┌──────────┐                              │
│                     │  agent   │──continue──→ (back to planner)│
│                     │ (LLM#3)  │                              │
│                     └────┬─────┘                              │
│                          │ finish                             │
│                          ▼                                    │
│                       finalize → END                          │
│                                                              │
│  状态管理: LangGraph Checkpointer (PostgreSQL)               │
│  追踪: TraceCollector（模块级 dict 存储，不进 state）        │
│  TTL: TraceCollector 1h / Checkpointer 7d                    │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
  EvalNode 树 → 存库 → 盘后回溯 → 权重迭代
```

### 与旧架构对比

| 维度 | 旧架构 | LangGraph 版本 |
|------|--------|---------------|
| 编排方式 | 手动 for-loop + `_execute_plan()` | `StateGraph` 节点图 |
| LLM 数量 | 4 个（Intent/Planner/Agent/Judge） | 3 个（Intent/Planner/Agent） |
| Judge (LLM#4) | 每步总结+纠错+控循环 | **已移除**，Agent 的 final_answer 即最终输出 |
| 状态管理 | 手动 checkpoint.py（dict 传递） | LangGraph Checkpointer（PostgreSQL 自动持久化） |
| 对话历史 | 手动拼接 | `messages: Annotated[List, add]` 自动追加 |
| 循环控制 | Judge 的 `continue_loop` | agent → planner 循环（should_continue + loop_step） |
| 流式输出 | 自行实现 | `app.stream()` 原生支持 |
| 闲聊快速通道 | 无 | planner 无工具时 LLM 直接回复 → skip → finalize |

## 三、LangGraph 图结构

### 3.1 节点定义

```python
# graph.py
graph = StateGraph(AgentState)

graph.add_node("prepare", prepare_node)     # 意图分析
graph.add_node("planner", planner_node)     # 工具选择
graph.add_node("agent", agent_node)         # ReAct 执行
graph.add_node("finalize", finalize_node)   # 后处理

graph.set_entry_point("prepare")
graph.add_edge("prepare", "planner")         # 固定边
graph.add_conditional_edges("planner", route_after_planner, {
    "skip": "finalize",    # 无工具（闲聊等）→ LLM 直接回复
    "run": "agent",        # 有工具 → ReAct 执行
})
graph.add_conditional_edges("agent", route_after_agent, {
    "continue": "planner",  # 还需要更多工具 → 回到 planner
    "finish": "finalize",  # 执行完毕 → finalize → END
})
graph.add_edge("finalize", END)
```

### 3.2 执行流程

```
用户消息
    │
    ▼
阶段 1: prepare_node ──────────────────────────────────
│  1. 负面反馈检测 — 惩罚上一轮 chain（checkpointer 读 last_verb/noun）
│  2. 意图分析 ← LLM #1
│     → domain / verb / noun / strategy / confidence
│  3. stock_code 提取（仅金融领域，3 级：context → 正则 → 中文名解析）
│  4. qd_traces 缓存查询（五层质量门，见 §7.4）
│     → 命中: cached_tools 存入 state
│     → 未命中: cached_tools = None
│  5. TraceCollector 创建（仅 traced 策略，即使缓存命中也创建）
│     → 存入模块级 _collectors dict（不可 msgpack 序列化）
│  6. 输出 state 更新 → 固定边到 planner
└──────────────────────────────────────────────────────
    │
    ▼
阶段 2: planner_node ──────────────────────────────────
│  1. 检查 state.cached_tools（prepare_node 已查询）
│     - 有缓存 + 无 step_records → 跳过 LLM#2，直接返回 current_tools
│     - cached_tools 置 None（用完即清，防止循环误读）
│  2. 缓存未命中 → Planner (LLM #2) 选工具
│     输入: 用户消息 + step_records（前序步骤结论）
│     输出: StepResult (tools/skill/tool_strategy/reasoning)
│  3. 结论检测（在调 LLM#2 之前）:
│     - step_records 非空 且 _has_conclusion(last_content) → should_continue=False → skip → finalize
│  4. 空工具检查:
│     - 无工具 + 无 skill → LLM 直接回复 → should_continue=False → skip → finalize
│  5. 有工具:
│     - should_continue=True（标记需要继续）
│     - loop_step += 1
│  6. route_after_planner 决定去向（skip / run）
└──────────────────────────────────────────────────────
    │ run
    ▼
阶段 3: agent_node ────────────────────────────────────
│  1. 构建步骤上下文（step_context）
│     - 前序步骤结论（最近 3 步）
│     - 标的信息（stock_code / stock_name）
│     - Skill 指令（如有）
│  2. 工具过滤（如有 skill，加载 skill 的 tools 列表）
│  3. 构建 smolagents CodeAgent（LLM #3）
│     - per-step 重建，上下文最小化
│     - TracedTool 包装（如有 collector）
│  4. agent.run(step_context)
│     - ReAct 循环: Observe → Think → Act → Observe
│     - final_answer() → 原始数据 + 一句话结论
│  5. 提取 tool_calls_log 和 charts
│  6. 构建 StepRecord → 追加到 state.step_records
│  7. route_after_agent 结束判断（兜底）:
│     - loop_step >= max_loop → finish（步数硬限制）
│     - _has_conclusion(last_content) → finish（Agent 输出含结论）
│     - 否则 → continue → 回 planner 继续选工具
└──────────────────────────────────────────────────────
    │ finish
    ▼
阶段 4: finalize_node（只在最后执行一次）────────────────
│  1. 从 step_records 拼接内容
│  2. JSON 提取 → 结构化 final_output
│  3. 学习闭环:
│     - _save_traces() → TraceCollector 存库 或 兜底写入 EvalNode
│  4. 更新 last_verb / last_noun（供下轮负面反馈检测）
│  5. TTL 清理过期 TraceCollector
│  6. 写入 messages（对话历史自动持久化）
└──────────────────────────────────────────────────────
    │
    ▼
  END → AgentResult 返回调用方
```

### 3.3 状态定义（AgentState）

```python
class AgentState(TypedDict, total=False):
    # ── 对话历史（Checkpointer 自动持久化）────────
    messages: Annotated[List[Dict[str, str]], add]  # 自动追加合并

    # ── 用户输入 ──────────────────────────────────
    query: str
    stock_code: str
    stock_name: str
    domain: str
    intent: Dict[str, Any]

    # ── 执行状态 ──────────────────────────────────
    step_records: List[StepRecord]  # 每轮覆盖（非追加），避免跨轮执行状态污染
    current_tools: List[str]
    current_skill: Optional[str]
    cached_tools: Optional[List[str]]   # qd_traces 缓存命中时注入，跳过 LLM#2

    # ── 控制流 ────────────────────────────────────
    loop_step: int
    max_loop_steps: int
    should_continue: bool
    all_phases_completed: bool

    # ── 输出 ──────────────────────────────────────
    final_output: Dict[str, Any]
    total_steps: int
    total_tokens: int
    tool_calls_log: List[Dict[str, Any]]  # 每轮覆盖，agent_node 内部手动累积
    charts: List[str]  # 每轮覆盖

    # ── 元数据 ────────────────────────────────────
    session_id: str
    user_id: str
    strategy: str                   # direct / traced
    collector: Any                  # TraceCollector（运行时，不序列化）
    intent_verb: str
    intent_noun: str
    intent_dimension: str           # technical / fundamental / capital / chip / news / sector / all
    intent_depth: str               # brief / normal / deep
    domain_instructions: str

    # ── 跨轮元数据（Checkpointer 自动持久化）─────
    last_verb: str
    last_noun: str
```

**关键设计点**：
- `messages` 使用 `Annotated[List, add]` reducer，对话历史自动追加
- `step_records` / `tool_calls_log` / `charts` 使用普通 List，每轮由 `create_initial_state()` 初始化为空，覆盖 checkpointer 旧值，避免跨轮执行状态污染。同轮内 agent→planner 多步循环由 agent_node 手动累积（`prev_records + [record]`）
- `TraceCollector` 不能放入 state（不可 msgpack 序列化），通过 `_collectors` 模块级 dict 存取
- `last_verb` / `last_noun` 跨轮持久化，供负面反馈检测使用
- 已删除: `enriched`（LangGraph messages 替代）、`intent_context`（死代码）、`current_rules`（tool_strategy 替代）、`step_content`/`step_success`（step_records 替代）

### 3.4 Checkpointer（PostgreSQL 持久化）

```python
def _build_checkpointer():
    database_url = os.getenv("DATABASE_URL")
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(conninfo=database_url, min_size=2, max_size=10)
    saver = PostgresSaver(pool)
    saver.setup()  # 自动建表
    return saver
```

- 优先使用 `langgraph-checkpoint-postgres`（官方包，v3.x）
- v3.x 的 `from_conn_string()` 是 `@contextmanager`，不直接返回实例；改用 `ConnectionPool` 直接构造，连接池自动管理连接生命周期
- 降级到 `MemorySaver`（跨重启不持久化）并输出警告日志
- 使用独立的 `psycopg_pool.ConnectionPool`（psycopg v3），与业务层的 `psycopg2.ThreadedConnectionPool`（psycopg2 v2）互不影响
- `thread_id = session_id`，每个会话独立
- `get_previous_state(session_id)` 可读取上一轮 state
- `get_session_messages(session_id)` 读取消息历史
- `list_checkpointer_sessions(limit)` 列出所有会话
- `delete_checkpointer_session(session_id)` 删除会话
- TTL: `cleanup_old_checkpoints(days=7)` 启动时清理 7 天前数据
- 消息历史统一: 所有消息读写走 Checkpointer，session_store 仅保留元数据（stock_code 等）

## 四、核心组件

### 4.1 Intent Analyzer（LLM #1）

**职责**: 分析用户意图，提取 domain/verb/noun/strategy/context_summary。

**流程**:
1. 空消息快速返回（0 LLM 调用）
2. LLM 分析: 单次调用同时完成意图分类 + 上下文压缩
   - 输入: 用户消息 + context_summary（上轮摘要）
   - 输出: domain / verb / noun / confidence / context_summary
   - **不提取 stock_code**（由 prepare_node 第 3 步处理）
3. strategy 计算: finance/trading → `traced`，其他 → `direct`

**注意**: stock_code 提取由 prepare_node 第 3 步负责（正则 + text_utils），不在意图分析阶段处理，避免 LLM 误匹配。

**输出**: `IntentResult` 结构体 → 序列化到 `state.intent`

### 4.2 Planner（LLM #2）

**职责**: 选工具为主，兼做结论检测。正常情况只选工具；当 Agent 上一步已输出结论时，跳过 LLM#2 直接结束。

**结束判断逻辑**（优先级从高到低）:
1. `_has_conclusion(step_records[-1])` → 跳过 LLM#2，直接结束（避免重复调用）
2. LLM#2 返回无工具无 skill → 闲聊快速通道，LLM 直接回复
3. 有工具 → 标记 `should_continue=True`，交给 agent 执行

**注入内容**:

| 内容 | 来源 | 用途 |
|------|------|------|
| persona | `semantics/persona.md` | 告诉 Planner 它是谁 |
| step_records | `state.step_records` | 前序步骤结论 |
| skills | `get_skills_summary_xml()` | 全量 skill 列表+描述 |
| tools | `get_tools_summary_xml()` | 全量 tool 列表+描述 |
| rules | `semantics/planner.md` | 输出格式和选工具规则 |
| dimension | `state.intent_dimension` | 分析方向，指导工具类型选择 |
| depth | `state.intent_depth` | 分析深度，指导工具数量选择 |

**输出格式**:
```json
{
  "tools": ["tool_name_1", "tool_name_2"],
  "skill": "skill_name 或 null",
  "description": "步骤描述",
  "tool_strategy": "为什么选这些工具、执行顺序"
}
```

### 4.2.1 意图识别细化（v4.5 新增）

在 verb/noun 基础上，新增两个独立维度指导 Planner 选工具：

**dimension — 分析方向**（仅 finance/trading 域有效）

| dimension | 含义 | 典型关键词 | 对应工具类型 |
|-----------|------|-----------|-------------|
| technical | 技术面分析 | K线、均线、MACD、RSI、趋势、形态 | analyze_trend, get_indicator_snapshot, analyze_pattern |
| fundamental | 基本面分析 | 市盈率、PE、PB、ROE、业绩、估值 | get_stock_info, get_consensus_eps, batch_valuation_compare |
| capital | 资金面分析 | 资金流向、主力、北向、融资、大单 | get_fund_flow, get_northbound_flow, get_concept_fund_flow |
| chip | 筹码分析 | 筹码、持仓、成本、套牢、获利盘 | get_chip_distribution |
| news | 情报分析 | 新闻、公告、研报、舆情、政策 | search_stock_intel, search_comprehensive_intel |
| sector | 板块分析 | 板块、行业、概念、热点、轮动 | get_hot_sectors, get_sector_trend_analysis |
| all | 全面分析 | 全面分析、综合分析 | 多维度工具组合 |

**depth — 分析深度**

| depth | 含义 | 典型表述 | 工具数量 |
|-------|------|---------|----------|
| brief | 快速查看 | “看一眼/快速查/简单看” | 1 个 |
| normal | 常规分析 | “分析/看看/怎么样”（默认） | 2-3 个 |
| deep | 深度分析 | “深度分析/详细分析/全面分析” | 4-6 个，可分步 |

**实现流程**:
```
用户: “茅台深度分析技术面”
  → intent: analyze+stock, dimension=technical, depth=deep
  → Planner 收到 dimension + depth 指导
  → 第一步: 选 analyze_trend + get_indicator_snapshot + get_volume_analysis
  → 第二步: 根据结果选 analyze_pattern + get_chip_distribution
```

### 4.3 Agent（LLM #3，smolagents CodeAgent）

**职责**: ReAct 推理循环，执行工具，返回原始数据。

**设计要点**:
- per-step 重建，只加载当前步骤需要的工具，上下文最小化
- TracedTool 包装（traced 策略时），对 agent 透明
- `final_answer()` 返回原始数据 + 一句话结论，不做结构化 JSON 输出
- Skill 通过 `read_skill` 工具加载 SKILL.md 指令执行

**工具集**:
1. 80+ 普通工具（get_kline, analyze_trend, ...）
2. `read_skill` 工具（加载 SKILL.md 指令）
3. `get_skill_catalog` 工具（查看可用技能）
4. `final_answer` 工具（结束并返回原始数据）

### 4.4 Skill Registry

**职责**: 扫描 `agent/skills/` 目录，自动发现并注册技能。

**插件化**: 新增技能只需往 `skills/` 目录扔文件夹和 SKILL.md 文件，零配置。

### 4.5 Tool Registry

**职责**: 扫描 `agent/tools/` 目录，自动发现并注册工具。

**发现规则**:
- `tools/` 目录下 `.py` 文件
- 跳过 `_` 开头的文件
- 有 docstring 的公开函数 → 自动注册

**插件化**: 新增工具只需往 `tools/` 目录扔 `.py` 文件，零配置。

### 4.6 TraceCollector — 执行追踪器

**职责**: Agent 执行过程中自动收集信息，构建 EvalNode 树。

**存储方式**: 模块级 `_collectors` dict + `_collectors_ts` 时间戳 dict，key 为 `session_id`。
- `TraceCollector` 实例不可被 msgpack 序列化，不能放入 LangGraph state
- `prepare_node` 创建并存入 → `agent_node` 读取 → `finalize_node` 弹出并存库
- TTL: `_cleanup_stale_collectors()` 在 finalize_node 末尾调用，清理超过 1 小时未消费的 collector

**提取策略**: JSON 优先，正则降级。

**层级**:
- Tool 层: 每次 tool_call → EvalNode 子树（由 TracedTool 自动触发 `on_tool_call()`）
- Skill 层: `on_skill_call()` 方法已定义，skill 层数据由 `_post_process` 兜底写入
- Chain 层: 最终 answer → 根节点

### 4.7 Evaluator — 评估器

**职责**: 盘后回溯验证，驱动权重更新。

**组件**: `chain/evaluator.py` — 离线评估，T+N 验证，盘后定时运行

**数据来源**: `qd_traces` 表 + `qd_skill_weights` + `qd_factor_weights`

## 五、语义文件与 Skill 结构

```
semantics/                      # 语义文件
├── persona.md          # 人设 → Planner + Agent
├── intent.md           # 意图分类规则 → IntentAnalyzer
├── planner.md          # 规划规则 → Planner LLM
├── agent_rules.md      # 执行规则 → Agent instructions
└── skills/             # Skill 定义（不在 semantics 下，与 semantics 平级）
```

**职责分离**:

| 文件 | 注入到 | 职责 |
|------|--------|------|
| `planner.md` | Planner LLM | 选什么 Skill、为什么选、输出执行计划 |
| `agent_rules.md` | Agent instructions | 执行纪律 + 输出要求 |
| `persona.md` | Planner + Agent | 人设 |
| `intent.md` | IntentAnalyzer | 意图分类规则 |

**Skill 目录**: `agent/skills/<name>/SKILL.md`，每个 skill 独立目录。

## 六、数据库设计

### 6.1 qd_traces — 执行追踪表

```sql
CREATE TABLE qd_traces (
    id SERIAL PRIMARY KEY,
    root_id INTEGER REFERENCES qd_traces(id),
    parent_id INTEGER REFERENCES qd_traces(id),
    layer VARCHAR(16),          -- 'chain' / 'skill' / 'tool'
    name VARCHAR(128),
    step_order INTEGER,
    exec_date DATE,
    stock_code VARCHAR(16),
    stock_name VARCHAR(32),
    score FLOAT,
    direction VARCHAR(16),
    action VARCHAR(16),
    signal TEXT,
    confidence FLOAT,
    timeframe VARCHAR(8),
    factors JSONB,              -- [{name, value, score, weight, status}]
    output_summary JSONB,
    analysis TEXT,
    input_params JSONB,
    tools_called TEXT[],        -- PostgreSQL 数组
    missing_data TEXT[],
    data_source VARCHAR(64),
    status VARCHAR(16),         -- 'ok' / 'missing' / 'failed' / 'skipped' / 'veto'
    error TEXT,
    elapsed_ms FLOAT,
    -- 回溯验证（盘后写入）
    exit_date DATE,
    exit_reason VARCHAR(32),    -- 'take_profit' / 'stop_loss' / 'max_hold' / 'signal_change'
    pnl_pct FLOAT,
    hold_days INTEGER,
    correct BOOLEAN,
    calibration FLOAT DEFAULT 1.0,
    -- 人工介入
    human_reviewed BOOLEAN DEFAULT FALSE,
    human_verdict VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 6.2 qd_factor_weights — 因子权重表

```sql
CREATE TABLE qd_factor_weights (
    id SERIAL PRIMARY KEY,
    skill_name VARCHAR(128),
    factor_name VARCHAR(128),
    weight FLOAT DEFAULT 1.0,
    win_rate FLOAT DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,
    last_updated TIMESTAMP DEFAULT NOW(),
    UNIQUE(skill_name, factor_name)
);
```

### 6.3 qd_skill_weights — Skill 权重表

```sql
CREATE TABLE qd_skill_weights (
    id SERIAL PRIMARY KEY,
    skill_name VARCHAR(128) UNIQUE,
    weight FLOAT DEFAULT 1.0,
    win_rate FLOAT DEFAULT 0,
    avg_pnl_pct FLOAT DEFAULT 0,
    avg_hold_days FLOAT DEFAULT 1,
    return_per_day FLOAT DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    last_updated TIMESTAMP DEFAULT NOW()
);
```

## 七、三个闭环 + qd_traces 缓存

三个闭环 + 缓存各自堵一个漏洞。终极目标：减小系统 bug 或规则不完善带来的亏钱。

### 7.1 记录闭环（数据源）
```
agent_node → TracedTool.forward() → collector.on_tool_call() → tool 节点
  → finalize_node → collector.on_agent_finish() → store.save_tree(root) → qd_traces
```
**作用**: 记录每一步执行过程，是其他三个闭环的数据源。
**实现**: TracedTool 包装所有工具，对 agent 透明。非 traced 策略有兜底写入（finalize_node 中 `EvalNode` 直接构造）。
**去掉后果**: 没有数据，回测和溯源都废了。

### 7.2 T+N 回测闭环（概率验证 + 渐进调权）
```
app 启动 → start_eval_worker() → 等到盘后 15:30
  → auto_evaluate()
    → evaluate_pending() → 取实际行情 → 写回 correct/pnl_pct
    → update_skill_weights()      # 哪个 Skill 权重在下降
    → update_factor_weights()     # 哪个因子权重在下降
```
**作用**: 用实际行情客观验证预测方向，多次验证渐进调权（单次对错无意义，看统计）。
**核心指标**: 单位时间收益率 = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days
**追踪能力**: 权重下降 → 识别哪个 Skill/因子在退化。
**去掉后果**: 无法区分预测对不对，权重失去客观依据。

### 7.3 用户反馈闭环（加速检测 tool 故障）
```
用户说"不对/垃圾/反了" → prepare_node._check_negative_feedback()
  → detect_feedback_severity(severe/mild)
  → 有 stock_code: query_latest_root(stock_code) → mark_root_wrong() / delete_tree()
  → 无 stock_code: query_latest_root_by_chain(chain_name) → mark_root_wrong() / delete_tree()
```
**作用**: 检测执行层故障（tool 更新、数据源挂了、API 变了）。T+N 回测也能通过统计发现，但需要 N 轮，期间持续亏钱。用户反馈是加速器。
**和 T+N 的关系**: 不在同一层——用户反馈检测**执行层**（工具坏了），T+N 检测**预测层**（方向对不对）。
**惩罚方式**:
- 轻度（mild）: `mark_root_wrong()` 设置 `correct=false` + `calibration=1.10`
- 重度（severe）: 惩罚次数 >= 3 时 `delete_tree()` 删除整棵 trace 树
**两种匹配模式**:
- 有 stock_code → 按股票代码匹配最近一条 trace
- 无 stock_code → 按 `domain+verb+noun` chain_name 匹配（如用户说"分析大盘"后反馈"不对"）
**前提条件**: `finalize_node` 在每轮结束时写 `last_verb`/`last_noun` 到 state（Checkpointer 自动持久化），下一轮 `prepare_node` 读取。
**去掉后果**: tool 坏了要等 N 轮 T+N 才能淘汰，期间持续亏钱。

**v4.4 修复**: 原实现中无 stock_code 时不会执行惩罚操作。新增 `query_latest_root_by_chain()` 和 `get_penalty_count_by_chain()` 函数，支持通过 chain_name 匹配。

### 7.4 qd_traces 编排缓存（省去 LLM #2 重复工作量）

~~原"编排路径学习闭环"已删除。tool_chains.json 读写链路断裂（Planner 从来不读），改用 qd_traces 聚合。~~

```
prepare_node
  → query_cached_tools(domain, verb, noun, stock_code)
    → GROUP BY tools_called
    → 质量门五层过滤（见下）
    → 命中: cached_tools 存入 state
  → planner_node
    → cached_tools 非空 → 跳过 LLM#2，直接注入 current_tools
    → agent_node 执行
    → 第2轮 agent→planner → cached_tools 已清 → 正常走 LLM#2
```

**作用**: 首次走一遍后，后续直接使用编排路径，省去 LLM #2（Planner）的重复思考。

**五层质量门**（全部通过才返回缓存）：

| 层级 | 条件 | 实现位置 | 作用 |
|------|------|----------|------|
| 1. 意图置信度 | confidence >= 0.7 | nodes.py prepare_node | 意图识别不确定时不走缓存 |
| 2. 聚合胜率 | win_rate >= 0.7, sample_count >= 3 | store.py SQL HAVING | 工具序列整体表现好 |
| 3. 步数限制 | tools_called 长度 <= 6 | store.py SQL WHERE | 链路不能太长（低效） |
| 4. 子节点无失败 | NOT EXISTS child.status='failed' | store.py SQL NOT EXISTS | 工具执行无错误 |
| 5. 工具权重 | 无低权重工具（win_rate < 0.4） | nodes.py prepare_node | 工具历史上表现差的不进缓存 |

**v4.4 修复**: 质量门第2层原仅在文档中描述，SQL 未实现。已添加 `HAVING COUNT(*) >= 3 AND AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) >= 0.7`。

**工具级权重过滤**（独立于缓存，作用于 LLM#2 候选集）：

```
agent.py → get_smolagent()
  → query_low_weight_tools()  ← 聚合 qd_traces child+root
  → 移除低权重工具 → LLM#2 看不到它们
```

**数据来源**: 纯聚合 qd_traces，不加新表，不加字段。1 万条数据 GROUP BY 无压力，数据有过期清理。

**前提条件**: domain + verb + noun 非空，意图置信度 >= 0.7。

**去掉后果**: 每次都从零规划，重复消耗 LLM #2 token。

## 八、工具分类

### 8.0 工具注册规则

ToolRegistry 自动扫描 `agent/tools/*.py`，注册所有公开函数（不以 `_` 开头）。

**命名规范**:
- 公开工具：`def tool_name(...)` — 会被注册，Planner 可见
- 内部函数：`def _helper(...)` — 不注册，仅供模块内部调用

**重复问题预防**:
- 如果函数仅供内部调用（如 `bull_bear_research.py` 中的分析函数），必须加 `_` 前缀
- 如果多个文件需要复用同一函数，从源文件 import，不要重新定义
- ToolRegistry 按文件名字母序扫描，同名函数后加载的覆盖先加载的

**v4.4 修复**: `bull_bear_research.py` 中 5 个内部函数 + `backtest_analysis.py` 中 2 个内部函数 + 2 个 intel 函数已改为 `_` 前缀，公开工具从 87 个降至 78 个。

### 8.1 工具层（78 工具，按职责分组）

| 分组 | Tools | 职责 |
|------|-------|------|
| 行情数据 | search_stock_by_name, get_realtime_quote, agent_get_kline, get_stock_info, get_order_book | 取K线/实时行情/盘口 |
| 指标计算 | analyze_trend, calculate_ma, get_volume_analysis, analyze_pattern, get_chip_distribution, get_indicator_snapshot, run_indicator_signal | 技术指标/形态/筹码 |
| 市场数据 | get_market_overview, get_market_indices, get_sector_rankings, get_hot_sectors, get_sector_fund_flow, get_concept_fund_flow, get_northbound_flow | 大盘/板块/北向 |
| 情报搜索 | search_stock_intel, search_sector_intel, search_policy_intel, search_comprehensive_intel | 个股/板块/政策/综合情报 |
| 选股筛选 | search_stocks, list_user_selection_strategies, build_keyword_from_filters, get_screener_presets | 综合选股/策略/筛选 |
| 信号捕捉 | get_hot_stocks_with_reasons, get_stock_concept_blocks, get_lockup_expiry, get_industry_ranking, get_dragon_tiger_detail | 热点/概念/解禁/龙虎榜 |
| 研究分析 | get_consensus_eps, get_capital_summary, batch_valuation_compare, get_eastmoney_stock_news, get_global_finance_news | 盈利预测/估值/新闻 |
| 板块分析 | get_hot_sectors, get_sector_trend_analysis, get_sector_history_data, get_sector_prediction, get_sector_cycle, get_stock_sector_info, get_sector_stocks | 板块趋势/周期/成分股 |
| 交易管理 | list_strategies, get_strategy_detail, start_strategy, stop_strategy | 策略管理/执行 |
| 系统工具 | get_page, get_cache_summary, get_text_page | 分页/缓存 |

### 8.2 Skill 层（通过 read_skill 加载）

Skill 从 `agent/skills/*/SKILL.md` 动态加载，包含：
- 名称、描述、标签
- 优先级、默认权重
- 工具列表
- 执行指令（SKILL.md body）

### 8.3 工具评分（evaluation）

**原则**: token 优化在工具层完成，不在 Agent 层。

**判断标准**: 数据量大（多指标、多行）→ 用评分压缩；数据量小（单个数字）→ 直接返回原始数据，不出评分。

**统一结构**:
```json
{
  "evaluation": {
    "score": 72,
    "scores": {"dim1": 80, "dim2": 65},
    "highlights": ["RSI 28 超卖"],
    "warnings": ["MACD 死叉"]
  }
}
```

**出评分的工具**（数据量大、多指标综合）：
- `analysis_tools.py` analyze_trend — MA/MACD/RSI/BOLL/KDJ 多维度加权
- `intelligence_analysis.py` — 新闻/政策文字量大，评分压缩有效
- `market_screener` — 候选池数据量大

**不出评分的工具**（数据量小、Agent 直接看更靠谱）：
- `data_tools.py` get_stock_info — PE/PB/ROE 就几个数字
- `news_search_tools.py` — 文字性内容，Agent 自行分析

## 九、配置项

### 9.1 环境变量

```bash
# Agent 模型配置
AGENT_LLM_MODEL=                 # 主 Agent 模型
AGENT_COMPRESS_MODEL=            # 上下文压缩模型
AGENT_EMBED_MODEL=               # Embedding 模型

# LangGraph 配置
MAX_LOOP_STEPS=10              # 最大循环步数（state 中记录）

# 意图分析
INTENT_ANALYSIS_ENABLED=true     # 是否启用意图分析

# 代码执行
CODE_EXECUTION_TIMEOUT=120       # 代码执行超时（秒）
```

### 9.2 语义文件

- `semantics/persona.md` — 人设定义
- `semantics/intent.md` — 意图分类规则
- `semantics/planner.md` — Planner 规则
- `semantics/agent_rules.md` — Agent 执行规则
- `skills/*/SKILL.md` — Skill 定义

## 十、已知限制

1. ~~**无循环回边**~~: ✅ 已实现（agent → planner 条件循环）。
2. **Planner 输出解析**: 依赖 LLM 输出 JSON 格式，有容错处理但不保证 100% 成功。
3. **单步执行**: 当前实现为顺序执行，不支持并行步骤。
4. **per-step agent 重建**: 每步重建 agent 有性能开销，但保证上下文最小化。
5. **TraceCollector 内存泄漏风险**: 模块级 `_collectors` dict，如果 finalize_node 未正常执行（异常中断），collector 不会被清理。已加 TTL（1小时）缓解。
6. **Checkpointer TTL**: 启动时清理 7 天前数据，无实时清理。
7. ~~**工具重复注册**~~: ✅ 已修复（v4.4）。`bull_bear_research.py` 和 `backtest_analysis.py` 中的内部函数与 `analysis_tools.py`/`news_search_tools.py`/`trading_tools.py`/`backtest_tools.py` 同名，导致 ToolRegistry 后加载的覆盖前加载的，Planner 看到错误描述。已将内部函数改为 `_` 前缀。

## 十一、混合输出模式

### 设计

后端始终使用 `app.stream()` 内部流式执行，前端始终通过 SSE 接收事件。无流式/非流式开关。

**后端**（`graph.py`）：
- `chat_hybrid()` — 核心生成器，逐节点 yield SSE 事件
- `chat()` — 同步包装器，内部调用 `chat_hybrid()` 收集最终结果

**路由**（`agent_blueprint.py`）：
- 单一端点 `POST /api/agent/chat` — 始终返回 `text/event-stream`

**前端**（`api/agent.js`）：
- `createAgentStream()` — 统一 SSE 连接，事件类型对齐后端

### SSE 事件类型

| 事件 | 说明 | 关键字段 |
|------|------|----------|
| `node_start` | 节点开始执行 | `node` |
| `node_done` | 节点执行完成 | `node` |
| `progress` | 工具规划进度 | `message` |
| `step_content` | 步骤输出内容 | `content` |
| `tool_start` | 工具调用开始 | `tool` |
| `tool_done` | 工具调用完成 | `tool`, `success` |
| `done` | 执行完成 | `success`, `content`, `total_steps`, `charts` |
| `error` | 执行异常 | `message` |

## 十二、Cron Worker

**职责**: 定时任务后台执行器（自调度模式）。

**启动**: `app/__init__.py` → `start_cron_worker()`

**模式**:
- `prompt` 模式 — 定时执行 Agent 聊天（调用 `graph.chat()`）
- `function` 模式 — 定时执行 Python 函数

**调度**: 从 `qd_cron_jobs` 表加载 enabled 任务 → 计算下次执行时间 → 设 `threading.Timer`。任务创建/更新/删除时 → 重算 → 重建 Timer。

**SSE 推送**: 前端连接 `/api/cron/events` 接收任务执行事件（`job_start` / `job_success` / `job_error`）。

## 十三、后续迭代

1. ~~**循环回边**~~: ✅ 已实现（agent → planner 条件循环，should_continue + loop_step）
2. **并行步骤执行**: 支持无依赖关系的步骤并行执行。
3. **动态 skill 加载**: 根据步骤需要动态加载/卸载 skill。
4. ~~**Checkpointer TTL**~~: ✅ 已实现（启动时清理 7 天前数据）
5. **回测引擎**: 完整的回测系统设计。
