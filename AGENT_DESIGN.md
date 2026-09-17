# QuantDinger Agent 模块设计文档

> 版本: v7.0
> 原则: **代码是怎样，设计文档就是怎样**。不一致时以代码为准。
> 不含：修改记录、老版本设计思想、历史记忆点
> 优化建议统一放末尾 §8

---

## 一、模块概述

### 1.1 定位

Agent 模块是 QuantDinger 系统的智能决策核心，负责：

- 接收用户自然语言输入（股票分析、数据查询、研究任务等）
- 按意图路由：简单查询直接回答；复杂任务进入规划-执行-收尾闭环
- 规划执行方案（外部 Planner 产出 task / phases / tools / step_budget）
- 调用工具获取数据（18 个金融领域工具模块 + 通用工具）
- 在沙箱中执行代码（smolagents CodeAgent，GuidedPythonExecutor 隔离）
- 生成结构化 AgentResponse，由 finalize 渲染为最终答复
- 记录决策过程（AgentTraceRecorder → qd_traces + JSONL）

### 1.2 核心设计原则

| 原则 | 说明 |
|---|---|
| **可追责** | 每个决策可追溯、可验证、可复盘；Trace 独立落库 qd_traces |
| **模块化** | 各组件独立，可单独测试和替换；新增领域只需放 `formatters/<domain>.py` + `@register_formatter` |
| **领域中立** | 架构不局限于金融领域；工具集域来自 `tools/<子目录>` 自动发现；能力层（`capabilities/`）是来源层，不占用 planner 可选工具域 |
| **澄清优先** | 解析器遇到歧义必须反问用户，不猜默认值（猜错标的/时间窗会让整份分析作废） |
| **最小依赖** | 仅依赖 smolagents（执行）+ 标准库（状态机）；无 LangGraph 等第三方图框架 |

---

## 二、架构总览

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  用户层                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                      │
│  │   Web UI     │  │   CLI        │  │   Cron       │                      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                      │
└─────────┼─────────────────┼─────────────────┼──────────────────────────────┘
          │                 │                 │
          ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          接入层                                             │
│  flask_app.py (SSE)  ←→  message_queue.py (线程池)  ←→  agent.py (Facade)  │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          编排层 (StateGraph)                                │
│                                                                             │
│  ┌─────────┐    ┌─────────┐    ┌──────────┐    ┌───────────┐              │
│  │  chat   │───→│  plan   │───→│ execute  │───→│ finalize  │              │
│  └────┬────┘    └────┬────┘    └─────┬────┘    └───────────┘              │
│       │              │               │                                     │
│       │              └───────────────┘ (复盘/replan 循环)                    │
│       │                                                                   │
│  nodes.py — 节点工厂 + AgentState + NodeContext                             │
│  agents/task_agent.py — TaskAgent (_plan / _build_code_agent / _chat_plan_graph) │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          执行层                                             │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ smolagents   │  │   Guided     │  │  resilient   │  │   staging    │   │
│  │  CodeAgent   │  │  Executor    │  │   _parse     │  │   变量续承   │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          能力层                                             │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ToolProvider  │  │    LLM       │  │    RAG       │  │   Skills     │   │
│  │ 统一工具表   │  │  大模型调用  │  │  检索增强    │  │  技能系统    │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          存储层                                             │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  PostgreSQL  │  │    Redis     │  │  pgvector    │  │   本地文件   │   │
│  │  qd_traces   │  │  缓存/会话   │  │  向量存储    │  │  JSONL/日志  │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 目录结构（代码实际文件，已验证存在）

```
backend_api_python/app/agent/
├── __init__.py
├── agent.py              # 统一入口，初始化全局组件（LLM/Memory/Retriever/SkillAdapter）
├── graph.py              # 自研 StateGraph 编排引擎（235行，无第三方依赖）
├── nodes.py              # Graph 节点定义（2085行：AgentState + NodeContext + 4工厂 + 3路由 + 辅助函数）
├── flask_app.py          # Flask Blueprint 路由（SSE 流式，_translate_agent_event 事件翻译）
├── cli.py                # CLI 入口（交互模式 / 单次对话 / --info / --list-tools）
├── message_queue.py      # 统一消息队列（Flask/Cron 共用，线程池 worker）
├── trace_collector.py    # TraceCollector 决策追踪器（构建 EvalNode 树，flush 写 SQL）
├── feedback.py           # 负面反馈检测（严重/温和两级关键词匹配）
├── check_exit.py         # 进程退出验证脚本
├── log.py                # 日志配置（桥接 app.agent.log，RotatingFileHandler）
├── cache.py              # TTL 缓存工具（线程安全，_InMemoryCache）
├── DESIGN.md             # 旧设计文档指针
│
├── agents/               # Agent 实现
│   ├── __init__.py
│   ├── base.py           # AgentBase 基类 + AgentResponse 数据类
│   └── task_agent.py     # TaskAgent 核心实现（~2420行）
│                         #   _plan()            — 外部 Planner，产出 phases/tools/step_budget
│                         #   _build_code_agent() — 构建 smolagents CodeAgent（工具注入/沙箱/事件钩子）
│                         #   _chat_plan_graph()  — 主流程：StateGraph 4节点 astream 流式执行
│                         #   _try_intercept_cron()— 定时任务正则拦截
│                         #   _LLMAdapter         — LLMBase → smolagents Model 适配
│                         #   _SkillResourceTool / _SkillFuncTool — 技能工具包装
│                         #   _normalize_phases() — 阶段契约规格化
│                         #   _normalize_plan_tools() — 顶层附加点名规格化
│
├── infra/                # 执行基础设施（非插件，标准 import）
│   ├── breaker.py        # ToolCircuitBreaker（连续失败≥2次 → 熔断短路）
│   ├── guided_executor.py # GuidedPythonExecutor（幻觉调用纠正 + dunder放行 + import边界纠正）
│   ├── resilient_parse.py # 代码提取加固层v5（伪标签防线 + ast健全性校验 + 散落抢救）
│   └── staging.py        # 跨阶段会话级变量存储（进程内 _OBJ dict，scope 隔离）
│
├── capabilities/         # 能力发现层（来源层，不占用 planner 可选工具域）
│   ├── __init__.py       # register_capabilities / load_admitted 导出
│   ├── loader.py         # 准入加载（admission.json → 护栏包装 → 注册为 CAPABILITY_DOMAIN）
│   └── scanner.py        # 能力扫描器（显式包 → 公开函数 → 写操作前缀硬排除）
│
├── chain/                # 可追责链（EvalNode 树）
│   ├── __init__.py
│   ├── schema.py         # EvalNode 数据结构（Layer/Status/JSONB 反序列化）
│   ├── store.py          # EvalNode 持久化（PostgreSQL，save_tree/load_tree/query_roots/get_skill_weights）
│   └── evaluator.py      # 盘后回溯评估 + 权重迭代 + 归因
│
├── cron/                 # 定时任务
│   ├── __init__.py
│   ├── cron_tools.py     # 定时任务工具（create_cron_job，注册为 ToolProvider 一员）
│   └── cron_worker.py    # 调度 worker（独立进程）
│
├── formatters/           # 领域回答格式化（注册表 + 自动发现）
│   ├── __init__.py       # @register_formatter 装饰器 + 自动加载
│   ├── base.py           # BaseFormatter 抽象 + _REGISTRY 注册表
│   ├── default.py        # 默认 formatter（纯 LLM 自适应）
│   └── finance.py        # 金融领域 formatter
│
├── llm/                  # LLM 适配层
│   ├── __init__.py       # create_llm / QDSkillAdapter 导出
│   ├── base.py           # LLMBase 抽象基类（ChatMessage / LLMResponse / LLMConfig）
│   ├── openai_llm.py     # OpenAI 兼容实现
│   ├── dashscope_llm.py  # 阿里云 DashScope
│   ├── qd_llm.py         # QD 私有 LLM
│   ├── qd_skills.py      # 技能适配器（Skill 调用增强）
│   └── factory.py        # LLM 工厂（按 env 选择 provider，注册表模式）
│
├── memory/               # 记忆系统（多后端）
│   ├── __init__.py
│   ├── base.py           # MemoryBase 抽象基类（MemoryMessage dataclass）
│   ├── local_memory.py   # 本地内存实现（dict，进程级，滑动窗口裁剪）
│   ├── postgres_memory.py # PostgreSQL 实现（滑动窗口 + TTL + run_in_executor 适配 async）
│   └── redis_memory.py   # Redis 实现（List 存储 + TTL）
│
├── rag/                  # RAG 检索增强
│   ├── __init__.py
│   ├── embeddings.py     # Embedding 适配（OpenAI / DashScope 双 provider）
│   ├── vector_store.py   # 向量存储基类
│   ├── pg_vector_store.py # pgvector 存储
│   ├── postgres_fts.py   # PostgreSQL 全文搜索 fallback
│   └── retriever.py      # 主检索器（多路召回 + RRF 融合 + Reranker 精排 + 低分过滤）
│
├── resolvers/            # 实体解析器
│   ├── __init__.py
│   ├── base.py           # EntityResolver 协议 + ResolveResult（含澄清契约 clarify_question）
│   ├── composite.py      # CompositeResolver（按序组合，澄清优先短路 + 上下文累积）
│   ├── stock.py          # 股票实体解析（代码/名称/行业，多候选→反问消歧）
│   └── time.py           # 时间解析（领域三级倒推 + 交易日历标定 + 澄清契约）
│
├── skills/               # 技能系统
│   ├── __init__.py
│   └── base.py           # Skill 基类 + SkillAdapter 协议
│
├── tools/                # 业务工具集（通过 ToolProvider 统一注册）
│   ├── __init__.py
│   ├── base.py           # Tool 基类 + ToolProvider 统一注册表（scan_directory / get_domains / get_functions / get_schemas_text）
│   ├── format_utils.py   # 格式化工具（format_result）
│   ├── web_search_tools.py # 联网搜索（_sanitize_result 注入消毒，web_search 函数）
│   ├── pagination.py     # 分页工具
│   ├── mcp_bridge.py     # MCP 协议桥接
│   └── finance/          # 金融领域工具（18 个模块）
│       ├── __init__.py
│       ├── _analysis_utils.py    # 分析工具共用辅助
│       ├── analysis_tools.py     # 形态分析、技术指标、组合评估
│       ├── backtest_tools.py     # 回测工具
│       ├── bull_bear_research.py # 多空研究
│       ├── chip_distribution.py  # 筹码分布
│       ├── data_tools.py         # 行情/财务/龙虎榜等取数
│       ├── dragon_tools.py       # 龙虎榜
│       ├── em_utils.py           # 东方财富工具
│       ├── fund_flow_tools.py    # 资金流
│       ├── indicator_tools.py    # 技术指标
│       ├── news_search_tools.py  # 新闻检索
│       ├── screener_config.py    # 选股器配置
│       ├── screener_tools.py     # 选股工具
│       ├── sector_analysis_tools.py # 板块分析
│       ├── signal_tools.py       # 信号工具
│       ├── technical_analysis.py # 技术分析
│       └── trading_tools.py      # 交易工具
│
├── prompts/              # 提示词模板（仅 3 个文件）
│   ├── plan_system.txt   # 外部 Planner 的任务书生成规则
│   ├── code_agent.yaml   # smolagents CodeAgent 模板整体覆写（initial_plan + update_plan）
│   └── intent_classifier.txt # 意图分类器
│
└── utils/                # 通用工具
    ├── __init__.py
    ├── json_parser.py    # JSON 安全解析（多重定界符 + 尾随逗号修复）
    ├── logger.py         # 日志工具
    ├── md_format.py      # Markdown 格式化
    ├── prescan.py        # 预扫（技能 AST 签名 / 工具签名清单 → 规划提示注入）
    ├── prompt_loader.py  # Prompt 文件加载器（缓存 + 编码兜底）
    └── tracing.py        # AgentTraceRecorder（JSONL + qd_traces 落库）
```

### 2.3 文件说明（核心文件职责速查）

| 文件 | 行数 | 核心职责 |
|---|---|---|
| `graph.py` | 235 | 自研 StateGraph / CompiledGraph / END；节点 + 边 + 条件边；ainvoke + astream |
| `nodes.py` | 2085 | AgentState TypedDict；NodeContext 运行时上下文；4 节点工厂；3 路由函数；验收/批次/守卫辅助 |
| `agents/task_agent.py` | ~2420 | TaskAgent 类：_plan / _build_code_agent / _chat_plan_graph / _LLMAdapter / 阶段契约归一化 |
| `agents/base.py` | ~160 | AgentBase 基类 + AgentResponse dataclass |
| `tools/base.py` | ~600 | ToolProvider 统一注册表（扫描 / 域过滤 / schema 生成） |
| `infra/guided_executor.py` | ~380 | GuidedPythonExecutor（幻觉调用纠正 + dunder 放行 + import 边界） |
| `infra/staging.py` | ~170 | 跨阶段会话级变量存储（_OBJ dict，scope 隔离） |
| `infra/breaker.py` | ~120 | ToolCircuitBreaker（连续失败熔断） |
| `infra/resilient_parse.py` | ~130 | 代码提取加固层 v5（伪标签 + ast 校验 + 散落抢救） |
| `utils/tracing.py` | ~600 | AgentTraceRecorder（JSONL + qd_traces upsert） |
| `rag/retriever.py` | ~500 | 主检索器（多路召回 + RRF + Reranker + 低分过滤） |
| `resolvers/composite.py` | ~130 | CompositeResolver（按序组合，澄清短路） |
| `flask_app.py` | ~300 | Flask Blueprint（SSE 流式，事件翻译） |

---

## 三、核心组件详解

### 3.1 StateGraph 编排引擎（`graph.py`，235 行）

**设计理念**：移植 LangGraph 核心设计模式，不引入 langgraph 依赖，自己实现轻量状态机。

**核心类**：

| 类 | 职责 |
|---|---|
| `StateGraph` | 图定义：注册节点、添加边/条件边、设置入口、编译 |
| `CompiledGraph` | 编译后可执行图：ainvoke（同步阻塞）+ astream（流式 yield） |
| `END = "__end__"` | 终止标记 |

**节点协议**：
```python
async def node(state: dict) -> dict | None
# 输入：当前完整状态
# 输出：partial state（只返回需要更新的字段，自动合并到主状态）
# 返回 None 表示不更新状态
```

**边协议**：
- `add_edge(source, target)` — 固定边：source 执行完后一定走 target
- `add_conditional_edges(source, condition, mapping)` — 条件边：`condition(state) -> str`，返回 mapping 中的 key

**Checkpointer**：接口存在（`compile(checkpointer=None)`），**当前未启用**（task_agent.py 注释"暂不启用，需要数据库连接池"）。

**易错点**：
- 节点名 `"__end__"` 是保留字，不可注册
- 条件函数返回值必须在 mapping 中，否则抛 ValueError
- 节点无出边时自动结束（warning 级日志）

### 3.2 节点实现（`nodes.py`，2085 行）

#### 3.2.1 AgentState（TypedDict，total=False）

```python
class AgentState(TypedDict, total=False):
    # ── 输入 ──
    user_input: str
    session_id: str
    use_rag: bool

    # ── chat_node 输出 ──
    entity_code: str          # 实体代码
    entity_name: str          # 实体名称
    entity_type: str          # 实体类型（stock/commodity/crypto/...）
    context: str              # RAG 上下文（仅 chat_node 检索一次）
    sources: list             # RAG 来源
    effective_input: str      # 扩写后的完整指令（含实体注入+时间标注）
    needs_task: bool          # True=进 plan→execute, False=直接回答
    task_type: str            # 子类型: analysis/screen/compare/query/code/explain/general
    direct_answer: str        # 直接回答内容（needs_task=False 时有值）

    # ── plan_node 输出 ──
    task: str                 # 完整任务描述
    selected_skill: str       # 选中的技能名（None=无技能）
    selected_domain: str      # 选中的领域名（空=仅通用工具）
    skill_body: str           # SKILL.md 正文
    skill_tools: list         # 技能工具列表
    step_budget: int          # CodeAgent 本轮步数预算
    planning_interval: int    # 内部规划步距
    plan_tools: list          # 顶层附加点名工具（有 phases 时恒为 []）
    phases: list              # 阶段契约（[]=单段执行旧路径）

    # ── phase 契约 ──
    phase_index: int          # 当前执行第几个阶段（0 起）
    phase_retry: int          # 当前阶段已重试次数
    phase_replan_count: int   # 因阶段失败触发的重设计次数（上限 2）
    phase_results: list       # 阶段结果记录 [{id,name,status,note,elapsed,preview}]
    completed_phases_text: str # 阶段摘要累积
    phase_last_note: str      # 最近一次验收失败说明
    _phase_agents: Any        # 各阶段 CodeAgent 实例（dict，非序列化）
    _phase_abort: bool        # 中断标记

    # ── execute_node 输出 ──
    result_raw: str           # CodeAgent 执行结果
    hit_max_steps: bool       # max_steps 耗尽标记
    replan_count: int         # 已复盘次数
    _code_agent: Any          # CodeAgent 实例（非序列化）
    _failed_tools: list       # 失败工具列表
    _agent_plan: str          # smolagents 最终规划

    # ── finalize_node 输出 ──
    elapsed: float

    # ── 错误处理 ──
    error: str
    failed_node: str
```

**设计要点**：`_code_agent` 和 `_phase_agents` 字段**不可序列化**（smolagents CodeAgent 实例）。启用 Checkpointer 前必须先把这些字段移出 state。

#### 3.2.2 NodeContext（运行时上下文）

```python
class NodeContext:
    llm: LLMBase              # LLM 实例
    memory                    # 记忆后端
    retriever                 # RAG 检索器
    skill_adapter             # 技能适配器
    system_prompt: str        # 系统提示
    memory_window_size: int   # 记忆窗口
    max_tool_rounds: int      # 最大工具轮数
    entity_resolver           # EntityResolver 实例（必须有 .resolve() 方法）
    tool_provider             # ToolProvider（惰性初始化）
    model                     # _LLMAdapter（惰性初始化）
    event_cb                  # SSE 过程事件回调（None=零开销）
    agent                     # TaskAgent 实例
    collectors: dict          # TraceCollector
```

**关键**：NodeContext 通过闭包传给节点函数，不存 checkpoint。

**ToolProvider 初始化**（`init_tools()`）：
- 扫描 `tools/` 根目录（通用工具）+ 子目录（领域工具）
- 进程级缓存（`_SHARED_TOOL_PROVIDER` 全局变量）：tools/ 目录运行期不变，扫描一次全程复用
- 能力层默认屏蔽（`CAPABILITIES_ENABLED != "1"` 时跳过注册）
- 设置全局默认 `ToolProvider.set_default(provider)` 只在首扫时执行一次

#### 3.2.3 四节点职责

| 节点 | 工厂函数 | 职责 |
|---|---|---|
| **chat** | `make_chat_node(ctx)` | RAG 检索 → 实体解析 → 意图分类 → 简单问题直接回答 |
| **plan** | `make_plan_node(ctx)` | ToolProvider 初始化 → _plan() → 渐进式加载技能 → 返回 phases/task |
| **execute** | `make_execute_node(ctx)` | _run_phase_step 批次化 → _build_code_agent → agent.run → 验收 |
| **finalize** | `make_finalize_node(ctx)` | 格式化 → 保存 memory → trace.finish() → 写 qd_traces |

#### 3.2.4 chat_node 详细流程

1. **RAG 检索**（仅此处执行一次，结果贯穿后续链路）
   - 代码意图门控（`_CODE_INTENT_RE`）：命中代码/通用意图时不注入历史标的
   - 分数阈值按来源分流：Reranker 用绝对阈值 0.7；RRF 用相对排名截断（`RAG_RRF_MIN_SCORE`，默认 0.005）
2. **实体解析**（CompositeResolver：先标的后时间）
   - RAG 辅助：用户消息无明确代码时，从 context 提取最近分析的标的（代码意图时跳过）
   - 澄清契约：解析器返回 `clarify_question` 非空 → 直接反问用户，**不进入执行**
3. **意图分类**（LLM 调用，从 `prompts/intent_classifier.txt` 加载）
   - `chat` → 直接回答（needs_task=False）
   - `task` → 进 plan（needs_task=True），提取 task_type（analysis/screen/compare/query/code/explain/general）
4. **Cron 拦截**（task_type == "cron" 时，调用 `TaskAgent._try_intercept_cron`）
5. **直接回答**（不需要工具时，组装 system_prompt + context + memory + user_input → LLM）

#### 3.2.5 plan_node 详细流程

1. ToolProvider 延迟初始化（首次进入任务流程时才扫描）
2. 复盘时注入前轮结果（`replan_context`）
3. 加载历史对话（最近 10 条）
4. 组装 plan 输入（effective_input + entity_info + task_type + rag_context + history + replan_context）
5. 调用 `ctx.agent._plan()` → 返回 task/phases/tools/step_budget
6. 渐进式加载技能（SKILL.md body + 技能工具）
7. phase 契约透传（游标归零，_phase_agents 重置）

#### 3.2.6 execute_node 详细流程

1. **单段旧路径**（phases 为空）：直接 `_build_code_agent` → `agent.run` → 结果
2. **多阶段路径**（phases 非空）：调用 `_run_phase_step` 批次化执行
3. 路由决策由 `route_after_execute` 收口

#### 3.2.7 finalize_node 详细流程

1. 格式化结果（调用 formatters/）
2. 保存到 memory
3. `trace.finish()` 写入 qd_traces
4. 清理 staging scope

#### 3.2.8 路由函数

```python
route_after_chat(state):
    needs_task → "plan"
    不需要  → "finalize"

route_after_plan(state):
    task 非空且 step_budget > 0 → "execute"
    否则 → "finalize"

route_after_execute(state):
    # phase 模式：
    _phase_abort → "finalize"
    _phase_replan_request → "plan"
    phase_index >= len(phases) → "finalize"（全部完成）
    否则 → "execute"（跑下一阶段）
    # 无 phases 旧路径：
    hit_max_steps 且 replan_count < MAX_REPLAN(2) → "plan"（复盘）
    否则 → "finalize"
```

### 3.3 TaskAgent（`agents/task_agent.py`，~2420 行）

#### 3.3.1 核心职责

TaskAgent 继承 AgentBase，是整个 Agent 模块的核心实现类：

| 方法 | 行号 | 职责 |
|---|---|---|
| `_plan()` | ~997 | 外部 Planner：LLM 选择技能、划分阶段、产出 phases/tools/step_budget |
| `_build_code_agent()` | ~1490 | 构建 smolagents CodeAgent：工具注入/沙箱配置/事件钩子/记忆截断 |
| `_chat_plan_graph()` | ~2225 | 主流程：构建 StateGraph 4 节点，astream 流式执行 |
| `_try_intercept_cron()` | ~1309 | 定时任务正则拦截（直接创建 cron job，跳过 agent 流程） |
| `chat()` | 入口 | 负面反馈检测 + 委托给 `_chat_plan_graph` |

#### 3.3.2 _plan() — 外部 Planner

**输入**：user_input（含实体/时间/上下文注入）

**输出**：
```python
{
    "task": str,                    # 任务描述
    "selected_skill": str | None,   # 选中的技能名
    "selected_domain": str,         # 选中的领域名
    "step_budget": int,             # 步数预算 [1,20]
    "planning_interval": int,       # 内部规划步距 [2,6]
    "phases": list,                 # 阶段契约（[]=单段执行）
    "plan_tools": list,             # 顶层附加点名（有 phases 时恒为 []）
}
```

**注入到 plan 提示的内容**：
- 技能列表（按权重降序，含预扫函数签名）
- 可用域列表（`tool_provider.get_domains()`）
- 工具签名清单（`prescan_tools`，上限 `PLAN_TOOL_LIST_LIMIT`=60，按相关度裁剪）
- 能力视图（`_capability_names`，来源层标记的工具）
- 编排缓存（历史成功链路，`query_cached_tools`）
- 工具权重提示（低权重工具 <0.7 警告）
- 已完成阶段摘要（复盘时）

**阶段契约归一化**（`_normalize_phases`）：
- id 重排为 1..n，总数上限 `PLAN_MAX_PHASES`=5
- tools 只保留 provider 中真实存在的名字（幻觉名丢弃并记入 tools_dropped）
- on_fail ∈ {retry, replan, abort}（默认 retry）
- max_retries 钳制 [0,3]（默认 `PLAN_PHASE_MAX_RETRIES`=1）
- step_budget 钳制 [0, `PLAN_PHASE_MAX_STEPS`=12]
- internal_plan ∈ {True, False, None}（None=按特征自动判定）
- barrier / replan 边界标记

**顶层附加点名归一化**（`_normalize_plan_tools`）：
- 语义：在 selected_domain 基调之上做**并集**（不是白名单）
- 与 phases 同时出现时以 phases 为准（不生效）

#### 3.3.3 _build_code_agent() — CodeAgent 构建

**工具注入顺序**（关键，顺序即语义）：

1. **provider 函数**：从 ToolProvider 按白名单/domain 过滤
   - phase 白名单模式（tools 非空）→ 只注入白名单内的工具
   - domain 模式 → 域工具 + 通用工具
   - 无域 → 仅通用工具
   - 附加点名（extra_tools）→ 并入基调
2. **技能工具**：`_SkillResourceTool` + `_SkillFuncTool`（私有，不和 tools/ 通用）
3. **_wrap_stage_guard**：工具结果存入 `executor.state`（`_r_<工具名>`），原样返回数据本身
4. **ToolCircuitBreaker.wrap**：连续失败≥2次 → 熔断短路
5. **executor.install_tools**：注入沙箱（必须在 tool_functions **最终确定之后**）

**⚠️ 易错点**：`_wrap_stage_guard` 和 breaker 包装会**重新绑定** tool_functions 名字。若在绑定前把旧 dict 交给 executor，沙箱里一个都调不到，调用即被误报成"幻觉调用"。

**smolagents Tool 包装**（5 个必选工具）：
- `FinalAnswerTool` — 抛 FinalAnswerException 结束任务
- `SearchToolsTool` — 按关键词搜索可用工具
- `ListToolsTool` — 列出可用工具（白名单模式只展示本阶段可见工具）
- `FormatResultTool` — 格式化工具返回值
- `WebSearchTool` — 联网搜索

**smolagents tools= 的真实作用**：
- ✅ 沙箱可调用（send_tools 注入 executor）
- ❌ 描述不会进 system prompt（本项目用 `prompts/code_agent.yaml` 整体覆盖了默认模板）
- LLM 认识工具名的通道：prompts/code_agent.yaml 正文与示例 + planning 段注入的 provider schema

**沙箱配置**：
- `additional_authorized_imports=["*"]`（import 全放行）
- `additional_functions`：`final_answer`（抛异常）+ `_SANDBOX_EXTRA_BUILTINS`（repr/format/hash 等纯内置）
- `max_print_outputs_length`：默认 6000（`CODE_MAX_PRINT_CHARS` 可调）
- `timeout_seconds`：默认 120（`CODE_EXEC_TIMEOUT` 可调）

**记忆截断**（`_truncate_observations` step callback）：
- 保留最近 2 步完整
- 更早步骤：observations 截到 400 字符，code_action/model_output 截到 200

**会话级变量投影**：
- 新建 executor 时，从 `stage_scope_vars(run_scope)` 取出已促升变量
- `send_variables(prev)` 装回 state，模型下一阶段直接用同名变量

#### 3.3.4 _LLMAdapter（smolagents Model 适配）

- 把 LLMBase 包装为 smolagents Model 接口
- 直接使用同步 OpenAI client（`_get_sync_client` 惰性创建）
- 非标 role 转换：`tool-call` → `assistant`，`tool-response` → `user`
- `_normalize_code_blocks`：markdown 代码块 → `<code>...</code>` 格式
- `close()` 关闭同步客户端释放 httpx 连接池

#### 3.3.5 _chat_plan_graph() — 主流程

```python
async def _chat_plan_graph(self, user_input, session_id, use_rag, event_cb):
    # 1. 创建 NodeContext（含 CompositeResolver = [StockResolver, TimeResolver 工厂]）
    # 2. 构建 StateGraph：
    #    add_node("chat", make_chat_node(ctx))
    #    add_node("plan", make_plan_node(ctx))
    #    add_node("execute", make_execute_node(ctx))
    #    add_node("finalize", make_finalize_node(ctx))
    # 3. 条件边：
    #    chat → plan | finalize
    #    plan → execute | finalize
    #    execute → execute | plan | finalize
    #    finalize → END
    # 4. 编译（checkpointer=None）
    # 5. astream 流式执行 + event_cb 播报节点生命周期
```

**实体解析器组装**：
```python
CompositeResolver([StockResolver(), lambda ctx: TimeResolver(entity_type=ctx["entity_types"][0] if ctx["entity_types"] else "")])
```
顺序即语义：先标的后时间。时间解析依赖前序识别出的实体类型倒推领域。

### 3.4 工具系统（`tools/`）

#### 3.4.1 ToolProvider 统一注册表（`tools/base.py`）

**核心 API**：

| 方法 | 职责 |
|---|---|
| `scan_directory(dir, domain)` | 扫描目录，注册所有公开函数 |
| `scan_subdirectories(dir)` | 扫描子目录，每个子目录名作为 domain |
| `get_domains()` | 返回可选域列表（来自 `tools/<子目录>` 名） |
| `get_functions()` | 返回 {name: function} 全量字典 |
| `get(name)` | 取单个工具函数 |
| `get_tool_names()` | 全量工具名列表 |
| `list_by_domain(domain)` | 按域列出工具名 |
| `get_schemas_text(names_filter)` | 生成 OpenAI Function Calling schema 文本 |
| `set_default(provider)` | 设置全局默认 provider |
| `get_default()` | 获取全局默认 provider |

**域发现规则**：工具集域 = `tools/<子目录>` 名（自动发现）。`common` 是特殊域，始终可加载。

**来源层 vs 工具域**：
- 工具集域 = `tools/<子目录>` 名，planner 用 `selected_domain` 选择
- 来源层（`capabilities/` 准入函数）— **不是域**，不占用 `selected_domain`，只通过阶段 tools 白名单点名注入

#### 3.4.2 工具返回值格式化

`ToolResult.to_str()` 按数据形态选择最省 token 的格式：
- 标量 → 直接转字符串
- 扁平 dict → "key: value" 每行一条
- list[dict]（表形数据）→ TSV
- 嵌套结构 → 带缩进的 JSON

#### 3.4.3 工具注入到沙箱的完整链路

```
provider.scan_directory()
    ↓
provider.get_functions()  →  按白名单/domain 过滤  →  tool_functions dict
    ↓
skill_tools 合入（_SkillResourceTool + _SkillFuncTool）
    ↓
_wrap_stage_guard（工具结果存 executor.state，原样返回数据）
    ↓
ToolCircuitBreaker.wrap（连续失败熔断）
    ↓
executor.install_tools（注入沙箱 custom_tools + static_tools + state）
```

### 3.5 技能系统（`skills/`）

**Skill 基类**（`skills/base.py`）：封装 LLM 调用 + 提示词模板 + 输出解析。

**SkillAdapter 协议**（注入到 TaskAgent）：
- `list_skills()` — 列出所有 skill 名+描述
- `get(name)` — 取 skill 配置
- `load_body(name)` — 加载 SKILL.md 正文

**渐进式加载**：plan 选中技能后才加载 SKILL.md body + 工具（非全量注入）。

**技能工具包装**：
- `_SkillResourceTool` — 读取技能资源文件
- `_SkillFuncTool` — 技能 run.py 中的函数

### 3.6 RAG 检索增强（`rag/`）

#### 3.6.1 检索器（`rag/retriever.py`）

**多路召回**：
- pgvector 向量检索
- PostgreSQL 全文搜索（FTS）fallback

**RRF 融合算法**：
```python
# Reciprocal Rank Fusion
# score = weight / (rrf_k + rank)
# 其中 rrf_k=60, rank 是该文档在该路线中的排名
```

**Reranker 精排**（可选）：启用时输出 rerank_score ∈ [0,1]。

**低分过滤**（chat_node 中执行）：
- Reranker 启用时：绝对阈值 0.7
- RRF 模式：相对排名截断（`RAG_RRF_MIN_SCORE`，默认 0.005，约 rank>40 的分数）

**易错点**：旧实现统一用 0.7 阈值，RRF 尺度下过滤掉全部文档，RAG 静默失效。

### 3.7 LLM 适配层（`llm/`）

**抽象基类**（`llm/base.py`）：
- `ChatMessage(role, content, name, tool_call_id, tool_calls)`
- `LLMResponse(content, tool_calls, model, finish_reason, tokens_used)`
- `LLMBase`：抽象类，定义 `generate()` / `chat()` 接口

**实现**：
- `OpenAILLM` — OpenAI 兼容（AsyncOpenAI）
- `DashScopeLLM` — 阿里云 DashScope
- `QDLLM` — QD 私有 LLM

**工厂**（`llm/factory.py`）：`create_llm(config)` 按 env 选择 provider，注册表模式。

### 3.8 记忆系统（`memory/`）

**抽象基类**（`memory/base.py`）：
- `MemoryMessage(role, content)`
- `MemoryBase`：`add(session_id, role, content)` / `get_history(session_id, limit)`

**实现**：
- `LocalMemory` — dict 存储，进程级，滑动窗口裁剪
- `PostgresMemory` — PostgreSQL，run_in_executor 适配 async，TTL 7 天
- `RedisMemory` — Redis List，TTL 24 小时

### 3.9 实体解析（`resolvers/`）

#### 3.9.1 ResolveResult（`resolvers/base.py`）

```python
@dataclass
class ResolveResult:
    entities: list         # [{code, name, type}, ...]
    entity_code: str       # 逗号分隔的代码
    entity_name: str       # 逗号分隔的名称
    entity_type: str       # 实体类型
    effective_input: str   # 处理后的用户输入（含实体信息+时间标注）
    clarify_question: str  # 非空 = 必须先反问用户，不得继续执行
```

**澄清契约**（域无关）：解析器遇到歧义必须填 `clarify_question`，不猜默认值。chat_node 见到非空 clarify_question 会直接反问用户且**不进入执行**。

#### 3.9.2 CompositeResolver（`resolvers/composite.py`）

按序执行子解析器并合并结果：
- **澄清优先**：任一子解析器 needs_clarify → 立即返回，不再继续
- **上下文累积**：前序结果以 ctx 交给后续子解析器（`{"entities": [], "entity_types": []}`）
- children 可以是 EntityResolver 实例或 `ctx -> EntityResolver` 工厂

#### 3.9.3 时间解析（`resolvers/time.py`）

**领域三级倒推**（chat 先于 plan、拿不到 selected_domain）：
1. 显式领域标注
2. 已识别实体类型倒推（`_ENTITY_DOMAIN` 登记表）
3. 语汇匹配

**交易日历标定**：`is_trading_day`、`window_start/end`。

#### 3.9.4 股票解析（`resolvers/stock.py`）

- 代码/名称/行业识别
- 多候选歧义 → 反问消歧（不静默取首个）
- RAG 辅助：从 context 提取最近分析的标的

### 3.10 结果格式化（`formatters/`）

**注册表模式**（`formatters/base.py`）：
```python
@register_formatter("finance")
class FinanceFormatter(BaseFormatter):
    def format(self, result, entity_type="", **kwargs):
        ...
```

**查找顺序**：domain → entity_type → default。

**易错点**：注册方用领域名（`"finance"`），调用方传 entity_type（`"stock"`）时可能不命中。查找顺序已修复为先 domain 后 entity_type。

**自动发现**：`formatters/__init__.py` 自动加载同目录下所有模块。

### 3.11 可追责链（`chain/`）

#### 3.11.1 EvalNode 树（`chain/schema.py`）

```
chain (根)
├── skill: market_screener
│   ├── tool: search_stocks
│   └── tool: get_realtime_quote
├── skill: stock_analysis
│   └── tool: get_kline
└── tool: get_market_overview    ← 无 skill 归属，直接挂 chain
```

**层级**：chain → skill → tool

#### 3.11.2 持久化（`chain/store.py`）

- `save_tree(root)` — 递归写入 qd_traces（INSERT/UPDATE）
- `load_tree(root_id)` — 读取整棵树，重建父子关系
- `query_roots(...)` — 查询根节点列表（分页/过滤）
- `get_skill_weights()` — 获取技能历史权重
- `get_tool_weights()` — 获取工具权重
- `query_cached_tools()` — 查询历史成功链路（编排缓存）

#### 3.11.3 盘后评估（`chain/evaluator.py`）

- T+N 反馈：根据后续行情验证分析结论
- 权重迭代：更新 skill/tool 权重
- 归因：区分 agent_fault vs tool_data_fault

### 3.12 基础设施层（`infra/`）

#### 3.12.1 GuidedPythonExecutor（`infra/guided_executor.py`）

**职责**：smolagents LocalPythonExecutor 的子类，增加：

1. **幻觉调用纠正**：`Forbidden function evaluation` 错误改写为 `[幻觉调用拦截]` + 可用工具清单 + 修复指引
2. **import 边界纠正**：`Import of xxx is not allowed` 错误改写为可用 import 清单
3. **dunder 属性放行**：monkeypatch `evaluate_attribute`，放行 `__name__`/`__class__` 等常见调试写法
4. **工具名延迟解析**：从 `custom_tools + static_tools` 动态收集真实可用名单（不从构造期取）

**易错点**：v4 版本曾从 `static_tools` 取工具名，但业务工具走 `custom_tools` 注入，导致清单恒为空。

#### 3.12.2 ToolCircuitBreaker（`infra/breaker.py`）

- 同一工具**连续**失败 ≥2 次 → 熔断打开
- 打开后调用立即短路，返回明确指令（禁止再调 + 降级建议）
- 成功调用清零计数
- 元工具不熔断：`search_tools`, `list_tools`, `format_result`, `web_search`, `final_answer`

#### 3.12.3 resilient_parse（`infra/resilient_parse.py`）

**v5 原则**：原生提取成功后做 **ast 健全性校验**；解析失败 → 进入救援链（散文跳过 + 最长可解析区段）。

**伪标签防线**：模型散文中引用的字面 `<code>` 标签对会被误匹配，v5 通过 ast 校验拦截。

#### 3.12.4 staging（`infra/staging.py`）

**跨阶段会话级变量存储**：
- 进程内 `_OBJ`（scope → {name: 原对象}），不走序列化
- 写入：`stage_put_obj(scope, name, obj)`
- 读出：`stage_scope_vars(scope)` → 返回全部变量 dict
- 清理：`stage_clear(scope)`

**两级变量统一**（2026-09-15）：
- 2 级（阶段内）= smolagents 原生 `executor.state`
- 1 级（跨阶段）= 同样是 state 变量，被 `_promote_model_vars` 促升到 staging 存储

### 3.13 能力发现层（`capabilities/`）

**来源层 vs 工具域**：
- 能力层是"数据能力层"——底层取数函数
- **不是** planner 用 `selected_domain` 能选的工具集域
- 打 `CAPABILITY_DOMAIN` 标记，从可选域清单天然排除
- 唯一注入途径：阶段 tools 白名单点名

**三层闸门**：
1. 写操作前缀硬复核（名称命中写操作前缀的条目拒绝注册）
2. 超时护栏（线程池提交 + 限时等待）
3. 体积护栏（结果 >max_chars 时写入 tmp/capability_output/，返回预览+路径）

**默认屏蔽**：`CAPABILITIES_ENABLED != "1"` 时跳过注册。

### 3.14 提示词系统（`prompts/`）

| 文件 | 用途 |
|---|---|
| `plan_system.txt` | 外部 Planner 的任务书生成规则（含阶段契约模板） |
| `code_agent.yaml` | smolagents CodeAgent 模板**整体覆写**（initial_plan + update_plan_pre/post_messages） |
| `intent_classifier.txt` | 意图分类器（chat_node 路由判定） |

**关键**：`code_agent.yaml` 整体覆盖了 smolagents 默认模板，因此 smolagents tools= 的描述不会进 system prompt。

### 3.15 消息队列与定时任务

**消息队列**（`message_queue.py`）：
- Flask 和 Cron 共用同一个队列 + worker 线程池
- `init_workers(n)` 启动 worker（幂等）
- `submit(message, session_id)` 提交任务 → queue → worker → agent.chat

**定时任务**（`cron/`）：
- `cron_tools.py`：`create_cron_job` 注册为 ToolProvider 一员
- `cron_worker.py`：调度 worker（独立进程）
- `_try_intercept_cron()`：正则匹配调度意图，命中则直接创建 cron job 跳过 agent 流程

---

## 四、执行流程

### 4.1 完整请求流程

```
用户输入
  │
  ▼
chat_node
  ├── RAG 检索（一次，结果贯穿后续）
  ├── 实体解析（CompositeResolver：先标的后时间）
  ├── 意图分类（LLM）
  │   ├── chat → 直接回答 → finalize
  │   ├── cron → _try_intercept_cron → finalize
  │   └── task → plan
  │
  ▼
plan_node
  ├── ToolProvider 初始化（首次）
  ├── _plan() → task / phases / tools / step_budget
  ├── 渐进式加载技能（SKILL.md + 工具）
  │
  ▼
execute_node
  ├── 无 phases → 单段执行
  │   └── _build_code_agent → agent.run → 结果
  │
  ├── 有 phases → 批次化执行（_run_phase_step）
  │   ├── 批次边界计算（连续非边界 phase 合并；barrier 独占；replan 回 planner）
  │   ├── 组装批次任务书
  │   ├── _build_code_agent（白名单/domain + 技能工具 + 变量投影）
  │   ├── agent.run
  │   ├── 验收（_check_phase_acceptance：LLM 逐条核对）
  │   │   ├── passed → advance
  │   │   ├── tool_data_fault → 软通过（保留已产出工作）
  │   │   ├── agent_fault + on_fail=replan + 额度够 → replan
  │   │   ├── agent_fault + on_fail=retry + 额度够 → retry
  │   │   └── 额度耗尽 → advance（容忍失败，推进管道）
  │   └── 变量续承（_auto_stage_phase_result → staging）
  │
  ▼
finalize_node
  ├── 格式化（formatters/）
  ├── 保存 memory
  ├── trace.finish() → qd_traces
  └── 清理 staging scope
```

### 4.2 批次化执行

**批次边界规则**：
- 普通 phase：与后续非边界 phase 合并为一批，一次 CodeAgent 跑完
- barrier phase：目标已知但依赖上游运行结果 → 独占一批
- replan phase：目标本身未知 → 暂停并回 planner 重规划

**批次内预算**：各 phase step_budget 之和，封顶 `PLAN_BATCH_MAX_STEPS`=30。

**内部规划**（planning_interval）：
- 由外部 planner 显式声明（internal_plan=True/False）
- 未声明时按特征自动判定：工具面宽（≥7）/ 多阶段（≥2）/ 预算非小（≥6）→ 开启
- 开启时 `effective_interval = max(2, min(sum_budget // 2, 6))`

### 4.3 验收与重试机制

**验收判定**（`_check_phase_acceptance`）：
- 无 acceptance 条目 → 引擎默认验收（正常收尾 + 结果非空 → pass）
- 有标准 → LLM 逐条核对，输出 JSON `{passed, score, reason, note}`
- 判定通道不可用 → fail-open 放行（宁松勿卡）

**归因**：
- `pass` — 验收通过
- `agent_fault` — 缺失项属于 agent 可控（分析错误/交付物不完整）→ 应重试
- `tool_data_fault` — 缺失项因工具不可用/外部接口未返回 → 不应重试，软通过

**推进决策**（`_decide_phase_transition`）：
```
passed 或 tool_data_fault → advance（软通过保留已产出工作）
agent_fault + on_fail=retry + retry < max_retries → retry
agent_fault + on_fail=replan + replan_count < MAX_REPLAN → replan
额度耗尽 → advance（容忍单阶段失败，让后续阶段继续产出）
```

### 4.4 变量续承机制

**两级统一**（executor.state 里的 Python 变量）：

| 级别 | 范围 | 实现 |
|---|---|---|
| 2 级 | 阶段内跨 step | smolagents 原生 `executor.state` |
| 1 级 | 跨阶段/整个 run | `_promote_model_vars` 促升 → staging → 投影回新 executor.state |

**工具结果自动登记**：`_wrap_stage_guard` 把工具结果存入 `_r_<工具名>`，模型可按确定性名字取回。

**跨阶段续承**：新建 executor 时，`stage_scope_vars(run_scope)` 取出已促升变量，`send_variables` 装回 state。

---

## 五、配置说明

### 5.1 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | "" | LLM 提供商 |
| `OPENAI_MODEL` | qwen-plus | 模型名 |
| `OPENAI_API_KEY` | "" | API Key |
| `OPENAI_BASE_URL` | None | API Base URL |
| `AGENT_LLM_TEMPERATURE` | 0.1 | 温度 |
| `AGENT_LLM_SEED` | None | 可复现采样 seed |
| `OPENAI_MAX_TOKENS` | 16384 | 最大输出 token |
| `AGENT_MAX_STEPS` | 6 | 默认步数预算 |
| `AGENT_MEMORY_MAX_HISTORY` | 2000 | 记忆最大历史 |
| `MEMORY_BACKEND` | local | 记忆后端（local/postgres/redis） |
| `DATABASE_URL` | "" | PostgreSQL 连接串 |
| `CAPABILITIES_ENABLED` | "0" | 能力层开关 |
| `AGENT_RUN_WALL_TIMEOUT` | 900 | agent.run 墙钟上限（秒） |
| `CODE_EXEC_TIMEOUT` | 120 | 单步代码执行超时（秒） |
| `CODE_MAX_PRINT_CHARS` | 6000 | 沙箱打印上限 |
| `PHASE_ACCEPT_TIMEOUT` | 60 | 验收判定超时（秒） |
| `RAG_RRF_MIN_SCORE` | 0.005 | RRF 长尾过滤阈值 |
| `PLAN_TOOL_LIST_LIMIT` | 60 | 工具清单注入截断 |
| `LLM_MAX_RETRIES` | (llm默认) | LLM 重试次数 |

### 5.2 模块级常量

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `PLAN_MAX_PHASES` | 5 | task_agent.py | 阶段数上限 |
| `PLAN_PHASE_MAX_RETRIES` | 1 | task_agent.py | 阶段内重试上限 |
| `PLAN_PHASE_MAX_STEPS` | 12 | task_agent.py | 单阶段步数上限 |
| `PLAN_BATCH_MAX_STEPS` | 30 | nodes.py | 批次级步数上限 |
| `MAX_REPLAN` | 2 | nodes.py | 隐式复盘上限 |
| `AGENT_RUN_WALL_TIMEOUT` | 900 | nodes.py | agent.run 墙钟上限 |

---

## 六、数据流

### 6.1 请求数据流

```
HTTP/CLI/Cron
  │
  ▼
agent.py (Facade: 初始化 LLM/Memory/Retriever/SkillAdapter)
  │
  ▼
TaskAgent.chat()
  │
  ▼
_chat_plan_graph()
  │
  ├── NodeContext 创建（含 CompositeResolver）
  ├── StateGraph 构建 + 编译
  ├── astream 流式执行
  │
  ▼
chat_node → plan_node → execute_node → finalize_node
  │            │             │              │
  │            │             │              └─→ qd_traces (PostgreSQL)
  │            │             │              └─→ memory (Local/Postgres/Redis)
  │            │             │
  │            │             └─→ smolagents CodeAgent
  │            │                 ├── executor.state (变量)
  │            │                 ├── staging.py (跨阶段变量)
  │            │                 └─→ ToolProvider (工具调用)
  │            │
  │            └─→ _plan() → LLM (规划)
  │
  └─→ RAG (检索) + CompositeResolver (实体解析) + LLM (意图分类)
```

### 6.2 Trace 数据流

```
AgentTraceRecorder
  │
  ├── JSONL 文件（每节点/每 step/每 tool_call 一行）
  │
  └── qd_traces (PostgreSQL)
      ├── trace_id / mode / created_at
      ├── chain (JSONB) — EvalNode 树
      ├── skills (JSONB) — 技能使用记录
      ├── tool_calls (JSONB) — 工具调用记录
      └── verdict — 评估结论
```

---

## 七、易错点与设计陷阱

### 7.1 高发 Bug 模式："声明了但没接线"的静默断链

本项目最高发的 bug 类型。判据依赖已被清空的字段、参数从未传入、模块从未 import、注册 key 与查询 key 语义不一致、变量算了不用。共同点是**不报错、不告警、无 trace**。

**已知实例**：
- NodeContext.entity_resolver 注入裸函数而非 EntityResolver 实例 → AttributeError 被 except: logger.debug 吞掉 → 实体解析从未执行
- capabilities 注册为虚构域 "quant" 与真实域 "finance" 并列 → 选任一都丢另一半工具
- formatter 注册表 key 用领域名，调用传 entity_type → finance formatter 永不命中

**防御**：新增/改动"注册表/契约"时，务必确认消费端真会命中；凡 except 里只写 debug 的接线处都该怀疑。

### 7.2 工具注入时序

`_wrap_stage_guard` 和 breaker 包装会**重新绑定** tool_functions 名字。必须在 tool_functions **最终确定之后**才调用 `executor.install_tools()`。否则 executor 持有旧表，沙箱里一个都调不到。

### 7.3 smolagents tools= 不进 system prompt

本项目用 `prompts/code_agent.yaml` 整体覆盖了 smolagents 默认模板，模板里没有 `{% for tool in tools %}` 渲染块。因此放进 smol_tools 的工具描述不会进入 LLM 提示。LLM 认识工具名的通道是 yaml 正文与示例 + planning 段注入的 provider schema。

### 7.4 final_answer 必须抛异常

`final_answer` 必须抛 `FinalAnswerException`，不能 return。smolagents 据此判定 `is_final_answer=True`。

### 7.5 沙箱 break/continue 不能放进 try/except

沙箱用异常实现循环控制（BreakException 继承 Exception），break/continue 放在 try/except Exception 里面会被捕获，导致循环无法终止。

### 7.6 裸调用 tool() 放在代码最后一行

返回值会整份进观测（_truncate_observations 只截断 keep_recent 之前的旧步骤，当前步不截）。应赋给变量或 print 提炼后的关键字段。

### 7.7 RAG 分数阈值按来源分流

Reranker 启用时输出 rerank_score ∈ [0,1]，用绝对阈值 0.7。RRF 模式输出融合分（上限≈0.016），绝对阈值不可用，改为只保留排名前 N 条。

### 7.8 capabilities 层不是可选域

capabilities 是"数据能力层"，不占用 `selected_domain`。planner 无法通过 selected_domain 选择能力层工具，只能通过阶段 tools 白名单点名。

### 7.9 _tool_result_var 确定性名字

工具结果变量名 = `_r_<工具名>`，模型可直接引用。同名重复调用会覆盖上一个结果。

### 7.10 沙箱 import 全放行

`additional_authorized_imports=["*"]`，所有 import 放行。破坏性操作不再靠沙箱提前堵死，改由 GuidedPythonExecutor 在执行前扫描。dunder 属性访问也已放行。

---

## 八、优化建议（原方案更优部分）

### 8.1 事实权威层 state["facts"]

**当前代码**：复盘输入取 `completed_phases_text` 里的日期断言，模型可改写时间事实且被复盘继承。

**优化方案**：
- `state["facts"]`（结构化、只读）：`{ref_date, weekday, is_trading_day, window_start, window_end, basis, source}`
- 任务书固定区块 `【时间口径·权威（不得改写）】`，渲染自 `facts`
- 收尾时若最终答案含与 `facts` 冲突的断言 → 记 `fact_conflict` 到 trace（只记账，不硬阻断）

### 8.2 阶段显式 0 工具语义

**当前代码**：`_normalize_phases` 把"缺失"与"显式 `[]`"都归一为 `tools: []`；`_build_code_agent` 视空为"无白名单"→ 回退域基调。

**优化方案**：
- 增 `tools_declared: bool` 字段
- 显式 `[]` → 只注入 stage 工具 + 必要元工具
- 缺失 → 维持当前回退到域基调

### 8.3 qd_traces 列修复

**当前代码**：`plan` 列 DDL 缺失；`model / total_tokens / session_id / user_query` 有 DDL 无写入。

**优化方案**：
```sql
ALTER TABLE qd_traces ADD COLUMN IF NOT EXISTS plan text;
```
接线位置：`utils/tracing.py` 的 `record()` 与 `finish()` 方法。

### 8.4 replan_reason 字段接线

**当前代码**：`replan_reason` 字段在代码里很少填，trace 里看不到真实的复盘原因。

**优化方案**：在 `_plan` 的复盘分支显式填 `replan_reason`，加枚举：能力撞墙 / 任务太难 / 工具失败 / 用户中止 / 其他。

### 8.5 决策点收敛

**当前代码**：3 个决策点（外部 Planner + 内部 planning_interval + 隐式复盘）。

**优化方案**：收敛为 1+1 — 外部 Planner（必经）+ 显式复盘（唯一补救）。`planning_interval` 走 auto 由 stage 决定是否启用。