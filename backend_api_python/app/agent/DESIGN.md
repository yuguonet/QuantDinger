# QuantDinger Agent 模块设计文档

> 最后更新: 2026-09-10
> 版本: v1.4
> 状态: 生产环境运行中
>
> v1.4（2026-09-10）— 健壮性审计修复：学习闭环接通（update_path_cache 移除 / skill 节点评估字段回填 /
> 评估毒丸出队）、RAG 阈值按分数来源分流、LLM 客户端并发模型修正（共享客户端不再任务级 close）、
> 复盘循环适配 smolagents>=1.27（memory 标记检测）、step_budget 钳制、plan 上下文并发隔离、
> 失败工具检测双通道、trace.finish 幂等、chain_name 接真实意图、feedback 复合词/问句消歧、
> ToolProvider 进程级缓存、实体解析线程隔离、队列背压。详见 git log。

---

## 一、模块概述

### 1.1 定位

Agent 模块是 QuantDinger 系统的智能决策核心，负责：
- 接收用户自然语言输入
- 检索相关知识（RAG）
- 规划执行方案
- 调用工具获取数据
- 生成结构化分析报告
- 记录决策过程（可追责）

### 1.2 核心设计原则

| 原则 | 说明 |
|------|------|
| **可追责** | 每个决策可追溯、可验证、可复盘 |
| **模块化** | 各组件独立，可单独测试和替换 |
| **通用性** | 架构不局限于金融领域，只有细节针对领域优化 |
| **不造轮子** | 优先使用成熟开源方案 |

---

## 二、架构总览

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                    用户层                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                          │
│  │   Web UI     │  │   CLI        │  │   API        │                          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                          │
└─────────┼─────────────────┼─────────────────┼──────────────────────────────────┘
          │                 │                 │
          ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              接入层 (Flask/FastAPI)                              │
│  flask_app.py  ←→  message_queue.py  ←→  agent.py  ←→  graph.py                │
└─────────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           编排层 (StateGraph)                                   │
│                                                                                 │
│  ┌─────────┐    ┌─────────┐    ┌──────────┐    ┌───────────┐                   │
│  │  chat   │───→│  plan   │───→│ execute  │───→│ finalize  │                   │
│  │ (RAG+   │    │ (任务   │    │ (CodeAgent│    │ (存库+   │                   │
│  │  实体)  │    │  规划)  │    │  执行)   │    │  记忆)   │                   │
│  └─────────┘    └─────────┘    └──────────┘    └───────────┘                   │
│      │              │              │                                            │
│      │              └──────────────┘ (复盘循环)                                  │
│      │                                                                        │
│  nodes.py — 节点实现                                                           │
│  agents/task_agent.py — CodeAgent 构建                                          │
└─────────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              能力层                                             │
│                                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │    RAG       │  │    LLM       │  │ToolProvider  │  │   Skills     │        │
│  │  检索增强    │  │  大模型调用  │  │  统一工具表  │  │  技能系统    │        │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘        │
└─────────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              存储层                                             │
│                                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │  PostgreSQL  │  │    Redis     │  │   Qdrant     │  │   本地文件   │        │
│  │  结构化存储  │  │  缓存/会话   │  │  向量存储    │  │  日志/配置   │        │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘        │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 目录结构

```
backend_api_python/app/agent/
├── agent.py              # 统一入口，初始化全局组件
├── graph.py              # StateGraph 编排引擎（轻量 LangGraph 替代）
├── nodes.py              # Graph 节点定义（chat/plan/execute/finalize）
├── flask_app.py          # Flask 路由接入
├── cli.py                # CLI 入口
├── message_queue.py      # 统一消息队列（Flask/Cron 共用）
├── trace_collector.py    # 决策追踪收集器
├── feedback.py           # 负面反馈检测
├── log.py                # 日志配置
├── cache.py              # TTL 缓存工具
│
├── agents/               # Agent 实现
│   ├── base.py           # AgentBase 基类
│   └── task_agent.py     # TaskAgent — 核心任务执行器（双规划器阶段契约/_plan/_build_code_agent）
├── infra/                # 框架内部机制（非插件，标准 import）
│   ├── breaker.py        # ToolCircuitBreaker（工具失败熔断：连续失败2次短路）
│   ├── guided_executor.py # GuidedPythonExecutor（幻觉调用纠正，错误带可用清单）
│   ├── resilient_parse.py # 代码提取加固层 v4（健全性校验/伪标签防线/散落抢救）
│   └── staging.py        # 阶段中转数据暂存区（stage_write/read/list，白名单+2MB上限）
├── capabilities/         # 能力发现层（v2.0 新增；来源层，不占用 planner 可选工具域）
│   ├── scanner.py        # 扫描器（显式包 → 公开函数 → 写操作前缀硬排除）
│   ├── loader.py         # 准入加载（admission.json → 护栏包装 → 注册为来源层 CAPABILITY_DOMAIN）
│   └── admission.json    # 人工过目准入清单（19 项激活 / 2 暂缓留档）
│
├── chain/                # 可追责链（EvalNode 树）
│   ├── schema.py         # EvalNode 数据结构
│   ├── store.py          # 决策树存储（PostgreSQL）
│   └── evaluator.py      # 盘后回溯评估
│
├── llm/                  # LLM 适配层
│   ├── base.py           # LLMBase 抽象基类
│   ├── openai_llm.py     # OpenAI 兼容实现
│   ├── dashscope_llm.py  # 阿里云 DashScope
│   ├── qd_llm.py         # QD 私有 LLM
│   ├── qd_skills.py      # 技能适配器
│   └── factory.py        # LLM 工厂
│
├── rag/                  # RAG 检索增强
│   ├── embeddings.py     # Embedding 抽象层
│   ├── vector_store.py   # 向量存储基类
│   ├── pg_vector_store.py # PostgreSQL 向量存储
│   ├── retriever.py      # 检索器（多路召回 + RRF + Reranker）
│   └── postgres_fts.py   # PostgreSQL 全文搜索
│
├── memory/               # 记忆系统
│   ├── base.py           # MemoryBase 抽象基类
│   ├── local_memory.py   # 本地内存实现
│   ├── postgres_memory.py # PostgreSQL 实现
│   └── redis_memory.py   # Redis 实现
│
├── tools/                # 业务工具集（通过 ToolProvider 统一注册）
│   ├── base.py           # Tool 基类 + ToolProvider 统一注册表
│   ├── format_utils.py   # 格式化工具（必选）
│   ├── web_search_tools.py # 联网搜索（四引擎降级）
│   ├── pagination.py     # 分页工具
│   ├── mcp_bridge.py     # MCP 桥接（serve() 入口不注册为工具）
│   └── finance/          # 金融领域工具（27 个模块）
│       ├── analysis_tools.py    # 技术分析（1613行，最大））
│       ├── data_tools.py        # 数据查询
│       ├── indicator_tools.py   # 指标计算
│       ├── indicator_analysis.py # 指标分析
│       ├── quote_tools.py       # 实时行情
│       ├── news_search_tools.py # 新闻搜索
│       ├── screener_tools.py    # 选股器
│       ├── fund_flow_tools.py   # 资金流
│       ├── capital_tools.py     # 资金汇总
│       ├── sector_analysis_tools.py # 板块分析
│       ├── chip_distribution.py # 筹码分布
│       ├── chart_patterns.py    # 形态识别
│       ├── technical_analysis.py # 技术面
│       ├── intelligence_analysis.py # 情报分析
│       ├── research_tools.py    # 研究分析
│       ├── dragon_tools.py      # 龙虎榜
│       ├── trading_tools.py     # 交易管理
│       ├── backtest_tools.py    # 回测工具
│       ├── backtest_analysis.py # 回测分析
│       ├── batch_review_tools.py # 批量复盘
│       ├── bull_bear_research.py # 多空研究
│       ├── bb_screener_scan.py  # BB筛选扫描
│       ├── index_tools.py       # 指数工具
│       ├── em_utils.py          # 东方财富工具
│       └── screener_config.py   # 选股配置
│
├── skills/               # 技能系统（插件化，零配置）
│   ├── base.py           # SkillAdapter 基类
│   ├── market_screener/  # 市场筛选技能
│   │   ├── SKILL.md      # 技能指令
│   │   ├── run.py        # 技能函数
│   │   ├── common.py     # 公共逻辑
│   │   ├── intraday.py   # 盘中分析
│   │   ├── post_market.py # 盘后分析
│   │   ├── eod.py        # 日终分析
│   │   └── references/   # 参考资料
│   └── stock_evaluation/ # 股票评估技能
│       ├── SKILL.md      # 技能指令
│       ├── run.py        # 技能函数
│       └── stock_report.py # 评估报告
│
├── resolvers/            # 实体解析器（chat_node 组合调用）
│   ├── base.py           # EntityResolver/ResolveResult 协议 + 澄清契约（clarify_question / needs_clarify）
│   ├── composite.py      # CompositeResolver：按序组合子解析器，澄清优先短路 + 上下文累积（2026-09-13）
│   ├── stock.py          # 股票实体（RAG 辅助，线程隔离；多候选歧义 → 反问消歧，不静默取首个）
│   └── time.py           # 时间实体 v3（领域按 显式/实体/语汇 三级登记表倒推；交易日历标定；语义异常与窗口不明 → 反问）
│
├── formatters/           # 结果格式化
│   ├── base.py           # BaseFormatter 基类 + 注册表
│   ├── default.py        # 通用兜底（纯 LLM 自适应）
│   └── finance.py        # 金融领域模板
│
├── prompts/              # 提示词模板
│   ├── plan_system.txt   # Plan 阶段系统提示
│   ├── code_agent.yaml   # CodeAgent 提示词模板
│   └── intent_classifier.txt # 意图分类器提示
│
└── utils/                # 工具函数
    ├── json_parser.py    # JSON 安全解析
    ├── md_format.py      # Markdown 格式化
    ├── tracing.py        # 追踪记录
    ├── prompt_loader.py  # 提示词加载
    ├── prescan.py        # 预扫（技能AST签名/工具签名清单 → 规划提示，v2.0）
    ├── trading_calendar.py # 交易日历（TimeResolver 依赖）
    └── logger.py         # 日志工具
```

---

## 三、核心组件详解

### 3.1 StateGraph 编排引擎 (`graph.py`)

#### 设计理念

自实现的轻量状态机，替代 LangGraph 依赖，保留核心设计模式：
- 节点是 async 函数
- 状态通过 TypedDict 传递
- 条件边支持路由
- 支持 checkpoint 持久化

#### 核心类

```python
class StateGraph:
    """状态图定义"""
    def add_node(name, func)           # 注册节点
    def add_edge(source, target)        # 固定边
    def add_conditional_edges(src, cond, mapping)  # 条件边
    def set_entry_point(name)           # 入口
    def compile(checkpointer)           # 编译

class CompiledGraph:
    """编译后的可执行图"""
    async def ainvoke(state, config)    # 同步执行
    async def astream(state, config)    # 流式执行
```

#### 状态定义 (`AgentState`)

```python
class AgentState(TypedDict):
    # 输入
    user_input: str
    session_id: str
    use_rag: bool

    # chat_node 输出
    entity_code: str        # 实体代码
    entity_name: str        # 实体名称
    entity_type: str        # 实体类型
    context: str            # RAG 上下文
    sources: list           # RAG 来源
    effective_input: str    # 扩写后的指令
    needs_task: bool        # 是否需要任务流程
    direct_answer: str      # 直接回答

    # plan_node 输出
    task: str               # 任务描述
    selected_skill: str     # 选中的技能名
    selected_domain: str    # 选中的工具域
    skill_body: str         # SKILL.md 正文
    skill_tools: list       # 技能工具列表
    step_budget: int        # 步数预算
    planning_interval: int  # 规划间隔
    task_type: str          # 任务子类型

    # execute_node 输出
    result_raw: str         # 执行结果
    hit_max_steps: bool     # 是否步数耗尽
    replan_count: int       # 复盘次数
    _code_agent: Any        # CodeAgent 实例
    _failed_tools: list     # 失败工具

    # finalize_node 输出
    elapsed: float          # 耗时
```

### 3.2 节点实现 (`nodes.py`)

> **v2.0 阶段契约**：chat/plan/execute/finalize 之外的核心机制——execute 为单 phase
> 执行器（`_run_phase_step`），route_after_execute 按 on_fail 分级驱动循环；阶段工具
> 白名单收窄（`_select_phase_skill_tools`）；验收判定（`_check_phase_acceptance`）；
> 步数耗尽取干净输出（`_extract_clean_phase_result`）。设计详见
> `docs/AGENT_ACCOUNTABLE.md` §14。
>
> **v2.1 规划分工**：双 planner 的职责边界由契约字段显式表达——外部 planner 定
> 「范围 / 分段 / 工具白名单 / 每阶段步数（`step_budget`）/ 是否启用内部规划
> （`internal_plan`）」，内部 planner 只在阶段内细化步骤、不得扩范围。简单阶段
> （取数/汇总）由外部 planner 直接关掉内部 planner，省一次 LLM 与上下文。

#### 四节点职责

| 节点 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `chat_node` | RAG检索 + 实体解析 + 意图分类 | user_input | context, entity, needs_task |
| `plan_node` | 任务规划 + 技能/域选择 + 加载 SKILL.md | effective_input, context, history | task, selected_skill, selected_domain, skill_body, skill_tools, step_budget |
| `execute_node` | CodeAgent 执行任务 | task, context | result_raw |
| `finalize_node` | 格式化汇总 + 存库 + 记忆 + 后处理 | result_raw | 最终输出 |

#### chat_node 详细流程

```
用户消息
  │
  ├─→ RAG 检索（向量 + FTS + 关键词）
  │     └─→ 结果 < 3 条？→ web_search 补充实时信息
  │
  ├─→ 实体解析（股票代码/名称/类型）
  │     └─→ RAG 辅助：从 context 提取最近分析的标的
  │
  ├─→ 消息标准化（短指令 → 完整分析指令）
  │
  └─→ 意图分类（LLM 判断）
        ├─→ task → 进入 plan_node
        └─→ chat → 直接回答 → finalize_node
```

#### execute_node 详细流程

```
task + context + selected_domain + skill_tools
  │
  ├─→ 构建 CodeAgent（通过 ToolProvider）
  │     ├─→ ToolProvider 按 domain 过滤工具 → executor.custom_tools
  │     ├─→ 技能工具注入 → executor.custom_tools
  │     ├─→ 4 个必选工具 → smolagents tools=[]（system prompt 可见）
  │     │     ※ 2026-09-14 更正：本项目下 tools= 的工具描述**不会**进入 system prompt
  │     │       （system_prompt 被 prompts/code_agent.yaml 整体覆盖，模板内无 tools
  │     │       渲染块）；实际作用仅"沙箱内可调用 + 以 BaseTool 形态调用"。
  │     │       清单现为 5 个（+ final_answer）。详见 §3.3 工具架构的更正说明。
  │     │     ├─ list_tools() — 列出工具
  │     │     ├─ search_tools() — 搜索工具
  │     │     ├─ format_result() — 格式化
  │     │     └─ web_search() — 联网搜索
  │     └─→ 全量工具 schema → planning YAML {{tool_list}}
  │
  ├─→ CodeAgent.run(task)
  │     └─→ ReAct 循环：思考 → 代码 → 观察
  │           └─→ 工具直接调用，无需 router
  │
  ├─→ 提取失败工具
  │
  └─→ 返回 result_raw
```

### 3.3 TaskAgent (`agents/task_agent.py`)

#### 核心职责

- 构建 smolagents CodeAgent 实例
- 通过 ToolProvider 按 domain 过滤加载工具
- 执行 ReAct 循环

#### 工具架构（3 层可见性）

| 层 | 包含什么 | 用途 |
|---|---|---|
| smolagents tools=[] | 4 个必选工具 | system prompt 自动描述，LLM 天然可见 |
| executor.custom_tools | 领域工具 + 通用工具 + 技能工具 | LLM 代码可调用，但不占 prompt token |
| YAML {{tool_list}} | 全量工具 schema（按 domain 过滤） | planning/replan 选工具 |

必选工具：list_tools、search_tools、format_result、web_search

> **2026-09-14 更正 —— 上表第 1 行"system prompt 自动描述，LLM 天然可见"不成立。**
> `smolagents tools=[]` 里的工具**不会**出现在 system prompt 中：
> smolagents 的默认模板才有 `{% for tool in tools %}{{ tool.to_code_prompt() }}` 渲染块
> （`.venv/.../smolagents/prompts/code_agent.yaml:132-137`），而本项目用
> `prompts/code_agent.yaml` **整体覆盖**了 `system_prompt`
> （`task_agent.py` 里 `agent.prompt_templates.update(custom_templates)`），
> 该模板内**没有**这个渲染块（实测 2026-09-14：用该模板 + 桩 tool 渲染，结果不含工具描述）。
>
> 因此三层的真实可见性通道是：
> - **LLM 可见**：`YAML {{tool_list}}`（provider schema，注入 planning 段）+ `prompts/code_agent.yaml` 正文与示例；
> - **仅沙箱可调用**：`executor.custom_tools`（裸函数）与 `smolagents tools=[]`（BaseTool 形态）。
>   二者差别只在调用形态：`BaseTool.__call__` 会把"单个 dict 且键名匹配 inputs"的入参自动展开成
>   kwargs（smolagents `tools.py:231-246`），避免"模型打包参数传 dict"直接 TypeError 烧步数。
>
> 清单现为 **5 个**（+ `final_answer`；其 `forward` 抛 `FinalAnswerException`）。
> 若确实要让工具描述进入系统提示，必须改 `system_prompt` 模板——属行为变更，需先评审。
>
> **沙箱内"能用哪些工具"的真相（2026-09-14，L14）**：业务工具走 `executor.custom_tools`
> （`task_agent.py` 的 `executor.custom_tools = tool_functions`），**不在** `static_tools` 里
> ——后者由 smolagents `send_tools` 填充，只有 agent tools + `BASE_PYTHON_TOOLS` +
> `additional_functions`（`local_python_executor.py:1763-1765`）。**混淆二者会让"可用工具
> 清单"退化成 Python 内置名列表**，见 §10.1 的 L14。

> **⚠️ 改 `_build_code_agent` 前必读 §3.13。**本函数里的 step callback 链
> （`_truncate_observations` / `_clarify_empty_output` / `_enforce_final_answer`）决定任务
> **能否正常收尾**，是事故高发区——症状是"步数被烧光"，根因却在别处。

#### CodeAgent 构建

```python
def _build_code_agent(self, model, provider, skill_tools, domain, ...):
    # 1. ToolProvider 按 domain 过滤工具函数
    if domain:
        allowed = provider.list_by_domain("common") + provider.list_by_domain(domain)
    else:
        allowed = provider.list_by_domain("common")
    tool_functions = {n: f for n, f in provider.get_functions().items() if n in allowed}

    # 2. 技能工具注入（私有，不和 tools/ 通用）
    for st in skill_tools:
        tool_functions[st.name] = st

    # 3. 创建 executor，注入 custom_tools
    executor = LocalPythonExecutor(...)
    executor.custom_tools = tool_functions

    # 4. 4 个必选工具注册为 smolagents Tool，放入 tools=[]
    smol_tools = [_SearchToolsTool(), _ListToolsTool(), _FormatResultTool(), _WebSearchTool()]

    # 5. 创建 CodeAgent
    agent = SmolCodeAgent(tools=smol_tools, model=model, executor=executor, ...)

    # 6. 注入 YAML 模板，替换 {{tool_list}}
    planning["initial_plan"] = planning["initial_plan"].replace("{{tool_list}}", provider.get_schemas_text(names_filter=allowed))

    return agent
```

#### LLM 工作流

```python
result = search_tools("资金")                    # 发现（必选工具；2026-09-14 更正：不进 system prompt）
result = get_fund_flow(codes="600519")           # 直接调用（在 custom_tools 中）
final_answer(result)                              # 输出（系统自动格式化）
```

### 3.4 RAG 检索增强 (`rag/`)

#### 架构

```
用户查询
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│                 MultiRouteRetriever                         │
│                                                             │
│  ┌─────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐     │
│  │ 向量检索 │  │ FTS检索  │  │聊天历史  │  │ 关键词  │     │
│  │(bge-m3) │  │(Postgres)│  │(PG FTS) │  │ (BM25)  │     │
│  │ w=1.0   │  │ w=0.8   │  │ w=0.4   │  │ w=0.6   │     │
│  └────┬────┘  └────┬────┘  └────┬─────┘  └────┬────┘     │
│       │            │            │              │          │
│       └────────────┼────────────┼──────────────┘          │
│                    ▼                                        │
│            RRF 融合排序                                     │
│                    │                                        │
│                    ▼                                        │
│          BGE-reranker 精排                                 │
│                    │                                        │
└────────────────────┼────────────────────────────────────────┘
                     ▼
               检索结果
```

> 聊天历史检索需开启总开关：`.env` 中 `CHAT_HISTORY_SEARCH_ENABLED=true`
> 且 `MEMORY_BACKEND=postgres`

#### Embedding 模型

| 模型 | 维度 | 特点 |
|------|------|------|
| DashScope text-embedding-v2 | 1536 | 阿里云 API |
| OpenAI text-embedding-3-small | 1536 | OpenAI API |
| **bge-m3-Q8_0** (推荐) | 1024 | 本地 llama.cpp，中文最优 |

#### 向量存储

| 实现 | 依赖 | 适用场景 |
|------|------|----------|
| `QdrantVectorStore` | Qdrant | 大规模向量检索 |
| `PgVectorStore` | PostgreSQL | 中小规模，已有 PG |

#### 检索器

```python
class Retriever:             # 单路检索器
class KeywordRetriever:      # 关键词召回（BM25）
class ChatHistoryRetriever:  # 聊天历史全文检索（PG FTS）
class MultiRouteRetriever:   # 多路召回 + RRF + Reranker
class BGEReranker:           # BGE-reranker 精排
```

#### RRF 融合算法

```python
# Reciprocal Rank Fusion
rrf_score = weight / (rrf_k + rank)
# 其中 rrf_k=60, rank 是该文档在该路线中的排名
```

#### Reranker 精排

```python
class BGEReranker:
    def __init__(model_path, use_api, api_url, api_key)
    def rerank(query, docs, top_k) -> list[dict]
    def _rerank_local(...)   # 本地 sentence-transformers
    def _rerank_api(...)     # 远程 API（jina/cohere/siliconflow）
```

### 3.5 LLM 适配层 (`llm/`)

#### 抽象基类

```python
class LLMBase(ABC):
    async def generate(messages, **kwargs) -> LLMResponse
    async def close()  # 释放底层资源（如 httpx 连接池）

class ChatMessage:
    role: str           # user / assistant / system / tool
    content: str
    name: Optional[str]
    tool_call_id: Optional[str]
    tool_calls: Optional[list]

class LLMResponse:
    content: str
    tool_calls: list
    model: str
    finish_reason: str      # stop / tool_calls / length / error
    tokens_used: int
    prompt_tokens: int
    completion_tokens: int
    metadata: dict
```

#### 实现

| 实现 | 说明 |
|------|------|
| `OpenAILLM` | OpenAI 兼容 API（推荐，支持 llama.cpp） |
| `DashScopeLLM` | 阿里云 DashScope |
| `QDLLM` | QD 私有 LLM |

#### 工厂模式

```python
def create_llm(config) -> LLMBase:
    provider = config["provider"]
    if provider == "openai": return OpenAILLM(...)
    if provider == "dashscope": return DashScopeLLM(...)
    if provider == "qd": return QDLLM(...)
```

### 3.6 工具系统 (`tools/`)

#### ToolProvider 统一注册表

```python
class ToolProvider:
    # 扫描 tools/ 根目录（通用工具）+ 子目录（领域工具）
    scan_directory(tools_dir, domain, package_prefix)
    scan_subdirectories(tools_dir, package_prefix)

    # 两种输出
    get_functions() -> Dict[str, Callable]   # executor 用
    get_schemas() -> List[dict]              # planning 用
    get_schemas_text(names_filter) -> str    # YAML {{tool_list}} 注入

    # LLM 面向接口
    list_tools(domain) -> str    # 列出工具
    search_tools(query, domain) -> str  # 搜索工具

    # 单例
    set_default(provider)
    get_default() -> ToolProvider
```

#### 工具分类（78 个公开函数）

| 分组 | 工具数 | 代表工具 | 职责 |
|------|--------|----------|------|
| 行情数据 | 5 | get_realtime_quote, agent_get_kline, get_stock_info | K线/实时行情/盘口 |
| 指标计算 | 7 | analyze_trend, get_indicator_snapshot, analyze_pattern | 技术指标/形态/筹码 |
| 市场数据 | 7 | get_market_overview, get_northbound_flow, get_sector_rankings | 大盘/板块/北向 |
| 情报搜索 | 4 | search_stock_intel, search_comprehensive_intel | 个股/板块/政策情报 |
| 选股筛选 | 4 | search_stocks, get_screener_presets | 综合选股/策略/筛选 |
| 信号捕捉 | 5 | get_hot_stocks_with_reasons, get_dragon_tiger_detail | 热点/概念/龙虎榜 |
| 研究分析 | 5 | get_consensus_eps, batch_valuation_compare | 盈利预测/估值/新闻 |
| 板块分析 | 7 | get_hot_sectors, get_sector_trend_analysis | 板块趋势/周期/成分股 |
| 交易管理 | 4 | list_strategies, start_strategy | 策略管理/执行 |
| 联网搜索 | 1 | web_search（四引擎降级） | 联网实时信息 |
| 系统工具 | 3 | format_result, list_tools, search_tools | 格式化/工具发现 |
| **总计** | **78** | ToolProvider 自动扫描注册 | |

#### 工具发现与调用

必选工具（4 个）通过 smolagents tools=[] 注入 system prompt：

> 2026-09-14 更正：上一句"注入 system prompt"不成立——`tools=[]` 的工具描述**不会**进入
> system prompt（`system_prompt` 被 `prompts/code_agent.yaml` 整体覆盖，模板内无 tools 渲染块），
> 它们只保证"沙箱内可调用"。清单现为 5 个（+ `final_answer`）。详见 §3.3 工具架构的更正说明。
- `list_tools()` — 列出可用工具
- `search_tools()` — 按关键词搜索
- `format_result()` — 格式化输出
- `web_search()` — 联网搜索

领域工具通过 executor.custom_tools 注入，可调用但不占 prompt token：

```python
# LLM 工作流
result = search_tools("资金")                    # 发现
result = get_fund_flow(codes="600519")           # 直接调用
final_answer(result)                              # 输出
```

#### 联网搜索 (`web_search_tools.py`)

四引擎自动降级：

```python
_ENGINES = [
    ("bocha",    _bocha_search),    # 博查 AI（国内优先）
    ("tavily",   _tavily_search),   # Tavily（AI 优化）
    ("baidu",    _baidu_search),    # 百度（免费无限额）
    ("searxng",  _searxng_search),  # SearXNG（自建兜底）
]
```

### 3.7 记忆系统 (`memory/`)

#### 抽象基类

```python
class MemoryBase(ABC):
    async def add(session_id, role, content)
    async def get_history(session_id, limit) -> list
    async def clear(session_id)
```

#### 实现

| 实现 | 存储 | 持久化 | 全文检索 | 适用场景 |
|------|------|--------|----------|----------|
| `LocalMemory` | 内存 dict | ❌ | ❌ | 开发/测试 |
| `PostgresMemory` | PostgreSQL | ✅ | ✅ PG FTS | 生产环境 |
| `RedisMemory` | Redis | ✅ | ❌ | 高并发场景 |

#### PostgresMemory 全文检索

`PostgresMemory` 内置 PG FTS 全文检索，`agent_messages` 表自动添加 `tsvector` 列 + GIN 索引：

```python
class PostgresMemory(MemoryBase):
    async def search(query, limit, session_id) -> list[dict]:
        """全文搜索历史聊天记录。"""
        # 中文分词：单字 + 双字 gram
        # tsvector + tsquery（OR 逻辑）
        # ts_rank 排序
```

新消息插入时自动填充 `fts_vector`，旧数据首次查询时回填。

与 `ChatHistoryRetriever` 配合，作为 RAG 多路召回的一路。

### 3.8 消息队列 (`message_queue.py`)

Flask 和 Cron 共用同一个队列 + worker 线程池，所有消息走同一条链路：

```
Flask 请求 / Cron 定时
  │
  ▼
submit(message, session_id) → Future
  │
  ▼
_task_queue (Queue, maxsize=256)
  │
  ▼
worker 线程 (4个)
  ├→ asyncio.new_event_loop()
  ├→ agent.chat(message)
  ├→ agent.llm.close()  ← 关闭 httpx 客户端
  ├→ loop.close()
  └→ future.set_result(content)
```

关键设计：
- 每个 worker 创建独立 event loop，避免跨线程共享
- 执行完毕后显式关闭 LLM 客户端（`agent.llm.close()`），防止 httpx 连接池泄漏
- 异常通过 `future.set_exception()` 传递给调用方，同时记录日志

### 3.9 结果格式化 (`formatters/`)

#### 设计模式

采用和 `resolvers/` 相同的注册表模式：
- `BaseFormatter`：抽象基类，定义 `format()` 接口
- `_REGISTRY`：全局注册表，key=**领域名或实体类型**, value=formatter_class
- `@register_formatter()`：装饰器，注册 formatter
- `get_formatter(entity_type, domain)`：查找顺序 **domain（领域级标准输出，多领域复用）→ entity_type（领域内单实体定制）→ default**
- `formatters/__init__.py` 用 pkgutil **自动发现**同目录模块 → 新增领域只需放 `formatters/<domain>.py` 并注册
- `list_formatters()`：注册快照，供启动自检（专门用来发现"注册了但没接线"这类静默断链）

#### 格式化流程

```
finalize_node
  ├→ selected_skill 有值？→ 跳过（SKILL.md 已定义输出规范）
  └→ 没有 skill？
       ├→ entity_type 有对应 formatter？→ 用领域 formatter
       └→ 没有？→ 用 default formatter
```

#### 核心类

```python
class BaseFormatter(ABC):
    @abstractmethod
    async def format(self, raw_result: str, context: dict) -> str:
        """格式化/汇总结果"""
        pass

def get_formatter(entity_type: str) -> BaseFormatter:
    """根据 entity_type 查找 formatter"""

def register_formatter(entity_type: str):
    """装饰器：注册 formatter"""
```

#### 已实现的 Formatter

| Formatter | entity_type | 说明 |
|-----------|-------------|------|
| `DefaultFormatter` | （兜底） | 通用 LLM 自适应 |
| `FinanceFormatter` | `finance` | 金融领域结构化报告 |

#### 扩展新领域

```python
# formatters/crypto.py
from .base import BaseFormatter, register_formatter

@register_formatter("crypto")
class CryptoFormatter(BaseFormatter):
    async def format(self, raw_result: str, context: dict) -> str:
        # 加密货币领域特定格式
        ...
```

### 3.10 可追责链 (`chain/`)

#### EvalNode 树

```python
class EvalNode:
    # 身份
    id: Optional[int]
    parent_id: Optional[int]
    root_id: Optional[int]
    layer: str              # chain / skill / tool
    name: str               # chain_id / skill_name / tool_name
    step_order: int         # 执行顺序

    # 时间/标的
    exec_date: date
    stock_code: str
    stock_name: str

    # 评估结果
    score: float            # 0-100
    direction: str          # bullish / bearish / neutral
    action: str             # buy / sell / hold / skip
    signal: str             # 一句话信号
    confidence: float       # 0.0-1.0
    timeframe: str          # T+1 / T+3 / T+5 / 1W / 1M

    # 内容
    factors: List[FactorItem]
    output_data: Dict
    analysis: str           # 分析文字
    plan: str               # smolagents 规划

    # 调用信息
    input_params: Dict
    tools_called: List[str]
    missing_data: List[str]
    data_source: str

    # 执行信息
    status: str             # ok / error
```

#### 三层追责

| 层级 | 记录内容 | 用途 |
|------|----------|------|
| Chain | agent 整体决策（action/score/direction） | 策略复盘 |
| Skill | 每次 call_skill 的分析报告 | 技能评估 |
| Tool | 每次工具调用的入参出参 | 工具验证 |

#### 盘后回溯 (`evaluator.py`)

```python
def start_eval_worker():
    """启动盘后回溯评估 worker"""
    # 定时任务：T+1 回溯验证
    # 对比预测 vs 实际
    # 更新技能/工具权重
```

### 3.11 技能系统 (`skills/`)

#### 技能定义

```
skills/market_screener/
├── SKILL.md          # 技能指令（Markdown）
├── references/       # 参考资料
└── run.py            # 技能函数（可选）
```

#### 三层注入

| 层级 | 时机 | 内容 |
|------|------|------|
| 第一层 | plan_node | 技能名 + 描述（简历） |
| 第二层 | execute_node | SKILL.md body（完整指令） |
| 第三层 | execute_node | _SkillResourceTool（按需读取资源） |

### 3.12 提示词系统 (`prompts/`)

#### plan_system.txt

Plan 阶段的系统提示词，多占位符注入上下文：

```
用户消息: {user_input}
{entity_info}        ← 实体信息
{task_type_info}     ← 意图类型
{rag_context}        ← RAG 检索结果
{history_context}    ← 历史对话
可用技能: {skills_text}
{completed_phases_text}  ← 复盘上下文
```

输出 JSON：`task` + `selected_skill` + `selected_domain` + `step_budget`

#### code_agent.yaml

CodeAgent 的完整提示词模板：

```yaml
system_prompt: |-
  # 工具调用方式
  # 执行规则
  # 示例

planning:
  initial_plan: |-        # 初始规划模板
  update_plan_pre_messages: |-  # 复盘前消息
  update_plan_post_messages: |- # 复盘后消息

managed_agent:            # 子 agent 模板
final_answer:             # 最终回答模板
```

### 3.13 收尾与退出机制（`final_answer`）—— 事故高发区

> 本节由 2026-09-14 的 CLI 实测事故沉淀（编号 L13）。**凡遇"任务跑不完 / 步数被烧光 / 到点还在重写代码"，
> 先读本节再动手。**
> 这类 bug 反复以不同形态出现，且**症状（步数耗尽）与根因相距很远**——根因通常是模型不知道该往哪交，
> 或误判"上一步什么都没产出"。已连续出现多个变体，故单独立节。

#### 框架事实（已核对 smolagents 源码，勿凭印象改）

| 事实 | 位置 |
|---|---|
| **唯一正常出口 = 模型主动调 `final_answer`** | `agents.py:545` `while not returned_final_answer and step_number <= max_steps`；`returned_final_answer` 仅在 `ActionOutput.is_final_answer` 为真时置位（`agents.py:582-592`） |
| **框架对"忘调 final_answer"零补救** —— 不提醒、不自动收尾 | — |
| 步数耗尽后 `_handle_max_steps_reached` **再额外调一次 LLM** 出终答（多花一次调用） | `agents.py:606-607` / `625-637` / `810-853` |
| `agent.interrupt()` 只是循环头 `raise AgentError` ⇒ **打断 run 且拿不到终答**，不能当收尾开关用 | `agents.py:546-547` / `754-756` |
| **唯一干净的"注入提示"位置 = `ActionStep.observations`** —— 它会被渲染成下一步的 `Observation:` 消息 | `memory.py:126-137`；`agents.py:768-769` 遍历所有 steps |
| **callbacks 里读到的 `is_final_answer` 是准的**：`_finalize_step` → callbacks 发生在 `action_step.is_final_answer = True` **之后** | `agents.py:592` → `601` |

社区量化参考：只靠 prompt 建议"完成后回答" ≈ **30%** 不收敛；把 Finish 做成**显式 Action** ≈ **100%**。
官方无配置开关可解（GitHub issue #1231 至今 Open / 零回复）。

#### 三层防御（A 机制层 / B 契约层 / C 护栏层）

三层都实现在 `agents/task_agent.py::_build_code_agent`，顺序固定：

```python
step_callbacks = (
    [_truncate_observations, _clarify_empty_output, _enforce_final_answer]
    + ([_evt_hook] if _evt_hook is not None else [])
)
```

| 层 | 治什么 | 实现 |
|---|---|---|
| **A 机制层** | 代码只 `print` 无 `return` ⇒ `code_output.output is None` ⇒ observation 出现 `Last output from code snippet: None` ⇒ 模型读成"上一步什么都没产出" ⇒ 从头重写整份代码 ⇒ 探索型死循环（实测：0 次 final_answer，4 步 token 单调膨胀 in 3884→7943 / out 7768→15888） | `_clarify_empty_output`：把该 `None` 补成明确语义（"仅表示本步代码没有 return 值；print 输出已完整列在上方 Execution logs"）。判定用 `rfind` 取最后一个 marker，非 `None` 或非 ActionStep 一律不动 |
| **B 契约层** | 任务书把交付物表述成"直接在答复中以代码块给出"——"答复"不是可执行落点，模型于是只 print 不交 | `prompts/code_agent.yaml`：正文加「交付物铁律」（源码 / 报告 / 结果 / 日志 ＝ `final_answer` 的参数）+ 规则第 9 条 + 代码交付示例；`prompts/plan_system.txt`：代码类任务改为"task 里必须要求执行者用 `final_answer(源码文本 + 运行输出)` 一次性交回" |
| **C 护栏层** | A、B 都没拦住时的兜底：把"跑满 N 步后被强制收尾"压成主动退出 | `_enforce_final_answer`：检测到**原地重写**或**倒数第二步仍未收尾**时，在该步 observations 末尾注入收尾指令 |

**C 的两条硬约束（都踩过坑，勿改回去）：**

1. **注入必须在倒数第二步**：`observations` 要到下一步才渲染成 `Observation:` 消息，
   在最后一步注入等于没人看得到。判据 `agent.max_steps - memory_step.step_number <= 1`。
2. **上一步含错误痕迹时不判定为重复**：`[import 拦截]` / `Traceback` / `Error:` / `Exception:`
   ⇒ 那是**失败重试**，重写是必要的；此时劝"别重写"会直接阻断纠错。
   （真实事故：Step1 因 `import io` 撞沙箱白名单整块中断，Step2 几乎是同一份代码、只换了捕获方式
   ——不设此门控就会被误判成原地重写。）

另外，**判据是"代码重复"而不是"没调 final_answer"**：后者会把正常多步任务
（取数 → 计算 → 交付）的中间步骤全部误判成"该收尾"，属于矫枉过正。
重复判定：最近 `keep_recent`(=2) 步的 `code_action`，去整行注释 + 去全部空白后
`SequenceMatcher(autojunk=False).ratio() >= 0.9`。

#### 排查清单

1. 日志看 `hit_max_steps` —— True ＝ 三层都没生效（退化为强制收尾）。
2. 看每步 `In / Out tokens` 是否**单调递增**：单调膨胀 ＝ 模型在重写整份代码 ＝ A 或 C 失效。
3. 看 observation 末行是否出现 `Last output from code snippet: None` 且**后面没有**补充说明 ＝ A 失效。
4. 看 observations 末尾是否出现 `[系统]` 注入 ＝ C 已介入；若反复出现，说明 A/B 没拦住。

#### 回归测试

`tests/test_wiring.py`：
- `test_empty_output_observation_is_clarified`（A：回调在位 + 真的改写）
- `test_prompt_maps_deliverable_to_final_answer`（B：提示措辞未被改回）
- `test_stalled_rewrite_gets_forced_to_final_answer`（C：重写必注入 / 正常推进不注入 / 倒数第二步保底注入 / 已收尾不介入 / **失败重试不误伤**）

CI 只跑 `compileall`、不跑测试 ⇒ 改完 agent 必须本地跑
`python -m pytest tests/test_wiring.py -v`。

### 3.14 时间解析与事实锚定（`resolvers/time.py`）

> 时间事实错 = 结论全错（取错区间的数据，或模型自己编日期）。本节记录该链路的关键决策，
> 改动前必读。相关事故：L2（domain 从未传入 ⇒ 交易日常识链整体不执行）、
> F2（时间事实可被模型改写并被复盘继承）。

#### 领域三级倒推（chat 先于 plan、拿不到 `selected_domain`）

显式 `domain` > 实体类型（`_ENTITY_DOMAIN`）> 输入语汇（`_WORD_DOMAIN`，复用 `_MARKET_WORDS`）
> `general`。是否走**交易日口径**查 `TRADING_CALENDAR_DOMAINS` 登记表（不是 `== "finance"` 硬编码）。

> **⚠️ `_MARKET_WORDS` 的覆盖率＝口径正确性。**漏一个词就让金融问题掉进 `general` 按自然日算：
> 实测「前天涨幅榜」因"涨幅"未收录 → 算出 2026-09-12（自然日），而交易日口径应为 2026-09-10。
> 2026-09-14 已补 `涨幅 / 跌幅 / 振幅 / 换手 / 成交 / 量比 / 市盈率 / 市值 / 封板 / 炸板 / 打板` 等
> （均为纯行情语汇，不误伤闲聊）。**新增行情语汇时同步此表。**

#### 内联标注是主交付（2026-09-14）

日期**就地钉在原文的时间词上**，而不是挂在消息尾部：

| 原文 | 解析后 |
|---|---|
| 今日涨停概率最大的股票 | `今日(2026-09-14)`涨停概率最大的股票 |
| 昨日涨停的股票 | `昨日(2026-09-11)`涨停的股票（交易日口径：周一说"昨日"＝上周五，不是自然日的 09-13） |
| 前天涨幅榜 | `前天(2026-09-10)`涨幅榜 |

**为什么必须内联**：改之前输出是
`今日涨停概率最大的股票 【时间】今天=2026-09-14；最近已收盘交易日=2026-09-11（以交易日历为准…）`
——信息虽在，但挂在**尾部**，LLM 生成时把它当背景忽略、照旧自己编日期
（实测结论里出现模型臆造的"上周五评分"）。钉在原文的时间词上才绕不过去。

- 可内联的类型见 `_INLINE_KINDS`（能解析出**单一日期**的词）；区间型（最近 / 本周 / 近 N 个交易日）
  不内联——它们不是一个日子，内联成 `最近(2026-09-07~2026-09-11)` 会误导，仍走尾部说明。
- **金融域常驻锚点**：即便原文没提时间也补 `最近已收盘交易日=…`（"金融领域统一加时间解析"的要求）；
  原文已内联"今天"时不再重复输出 `今天=…`。
- 非金融输入不注入时间（实测「帮我写个冒泡排序」→ 返回 `None`）。

> **⚠️ 内联必须贯穿到 execute（2026-09-14 修复）**：`plan_node` 用 `effective_input`
> （`nodes.py:606`），但 `_run_phase_step`（阶段模式）与单段 execute 曾直接取裸
> `state["user_input"]` ⇒ 阶段任务书开头的「用户原始需求」**没有日期**，与下方 goal 里
> planner 写的标定日期不一致，且日志上看起来像"时间解析没生效"（实测即被这样误读）。
> 现统一改为 `state.get("effective_input") or user_input`。
> **凡是"注入原始输入做保底"的段落（防止 planner 丢关键词），都该用 `effective_input`**
> ——它是原文的严格扩写，保底作用不减反增，不会给出无日期的版本。

#### 澄清契约（域无关）

无法准确判断就**反问**而不是猜：非交易日说"今天行情"、"最近/近期"无窗口 → 返回
`clarify_question`，`chat_node` 见非空即反问用户且**不进入执行**。

---

## 四、执行流程

### 4.1 完整请求流程

```
用户: "分析300129"
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ chat_node                                                   │
│                                                             │
│ 1. RAG 检索                                                 │
│    ├→ 向量检索 (bge-m3) → 2 条                              │
│    ├→ FTS 检索 → 1 条                                       │
│    ├→ 关键词检索 → 0 条                                      │
│    └→ RRF 融合 → 3 条                                       │
│                                                             │
│ 2. web_search 补充（RAG < 3 条时触发）                       │
│    └→ 搜索 "泰胜风能 300129 最新消息" → 5 条                 │
│                                                             │
│ 3. 实体解析                                                 │
│    └→ 300129 → 泰胜风能 (stock)                             │
│                                                             │
│ 4. 意图分类                                                 │
│    └→ task（需要工具）                                       │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ plan_node                                                   │
│                                                             │
│ 1. 加载技能列表                                              │
│ 2. LLM 生成执行计划                                          │
│    └→ task: "分析泰胜风能(300129)，获取技术指标..."           │
│    └→ step_budget: 10                                       │
│    └→ planning_interval: 6                                  │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ execute_node                                                │
│                                                             │
│ 1. 构建 CodeAgent                                           │
│    ├→ 扫描加载 tools/*.py 工具函数                          │
│    └→ 加载 YAML 模板                                        │
│                                                             │
│ 2. CodeAgent.run(task)                                      │
│    ├→ Step 1: 规划                                          │
│    │   └→ 选择工具: get_stock_info, get_realtime_quote, ... │
│    ├→ Step 2: 执行                                          │
│    │   └→ tool_name(param="value")                        │
│    ├→ Step 3: 观察                                          │
│    │   └→ 获取返回数据                                       │
│    └→ Step N: final_answer()                                │
│        └→ 输出结构化报告                                     │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ finalize_node                                               │
│                                                             │
│ 1. 结果格式化汇总（selected_skill 有值时跳过）               │
│    ├→ 根据 entity_type 选择 formatter                       │
│    └→ LLM 生成结构化报告                                    │
│ 2. TraceCollector 存库                                      │
│ 3. 追加失败工具信息                                          │
│ 4. 保存 memory                                              │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
输出: **股票名称**: 泰胜风能 (300129)
      **操作建议**: 跳过
      **评    分**: 0
      ...
```

### 4.2 复盘循环

```
execute_node (step_budget 耗尽)
  │
  ├→ hit_max_steps = true
  │
  ▼
route_after_execute → "plan" (复盘)
  │
  ▼
plan_node (replan_count++)
  │
  ├→ 注入前轮结果
  ├→ LLM 重新规划
  │
  ▼
execute_node (继续执行)
  │
  ├→ max(复盘次数) = 2
  │
  ▼
finalize_node
```

> **触发条件（v1.4 修正）**：smolagents >= 1.27 到达 max_steps 时**不抛异常**，
> 而是强制生成 final answer 正常返回，仅在 memory 最后一步的 ActionStep 上标记
> `AgentMaxStepsError`。execute_node 以检测该标记判定步数耗尽（旧版靠捕获异常
> 字符串，在 1.27 下永不触发，复盘循环曾是死代码）。
> step_budget 由 plan 产出，钳制在 [1, 20]。

---

## 五、配置说明

### 5.1 环境变量

```bash
# ═══════════════════════════════════════════════════════════════
#  LLM 配置
# ═══════════════════════════════════════════════════════════════
LLM_PROVIDER=openai                    # openai / dashscope / qd
OPENAI_MODEL=qwen2.5-coder-14b-instruct-q4_k_m
OPENAI_API_KEY=***
OPENAI_BASE_URL=http://localhost:8080/v1
AGENT_LLM_TEMPERATURE=0.1
OPENAI_MAX_TOKENS=16384

# ═══════════════════════════════════════════════════════════════
#  RAG 配置
# ═══════════════════════════════════════════════════════════════
EMBEDDING_PROVIDER=llamacpp             # llamacpp / dashscope / openai
EMBEDDING_MODEL=bge-m3-q8_0
EMBEDDING_BASE_URL=http://localhost:8081/v1
EMBEDDING_API_KEY=***

RAG_TOP_K=5
RAG_SCORE_THRESHOLD=0.3

# Reranker（可选）
RERANKER_PROVIDER=api                   # local / api
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_API_URL=https://api.siliconflow.cn/v1/rerank
RERANKER_API_KEY=***
RERANK_TOP_K=20

# ═══════════════════════════════════════════════════════════════
#  记忆配置
# ═══════════════════════════════════════════════════════════════
MEMORY_BACKEND=local                    # local / postgres / redis
MEMORY_MAX_HISTORY=2000
CHAT_HISTORY_SEARCH_ENABLED=false       # 聊天历史全文检索（需 MEMORY_BACKEND=postgres）

# ═══════════════════════════════════════════════════════════════
#  Agent 配置
# ═══════════════════════════════════════════════════════════════
AGENT_MAX_STEPS=6
AGENT_ENV=development

# ═══════════════════════════════════════════════════════════════
#  联网搜索配置
# ═══════════════════════════════════════════════════════════════
BOCHA_AI_API_KEY=***                    # 博查 AI（推荐）
TAVILY_API_KEY=***                      # Tavily（1000次/月免费）
SEARXNG_BASE_URL=                       # SearXNG（自建）

# ═══════════════════════════════════════════════════════════════
#  数据库配置
# ═══════════════════════════════════════════════════════════════
DATABASE_URL=postgresql://user:pass@localhost:5432/quantdinger
```

### 5.2 llama.cpp 启动配置

```bat
:: 终端 1 - Chat 模型 (端口 8080)
E:\llama.cpp\llama-server.exe ^
  -m E:\models\qwen2.5-coder-14b-instruct-q4_k_m.gguf ^
  --host 0.0.0.0 --port 8080 -t 4 -c 16384 -ngl 99

:: 终端 2 - Embedding 模型 (端口 8081)
E:\llama.cpp\llama-server.exe ^
  -m E:\models\bge-m3-q8_0.gguf ^
  --embedding --host 0.0.0.0 --port 8081 -t 4 -ngl 99
```

---

### 5.3 单进程假设（重要）

agent 子系统以下状态为**进程内单例**，多 gunicorn worker 会直接失效：

| 组件 | 状态 | 多 worker 时的症状 |
|------|------|--------------------|
| feedback 会话→root 映射 | 模块级 dict | 负面反馈找不到对应 trace，惩罚失效 |
| ToolProvider | 类单例 set_default | 各 worker 重复扫描，全局覆盖互踩 |
| trace（utils/tracing.py） | 模块级状态 | 跨进程 trace 断裂，chain 统计失真 |
| message_queue | 进程内 Queue + worker 线程 | 消息分属不同进程队列，SSE 订阅收不到其它进程的结果 |

部署约束：保持 `GUNICORN_WORKERS=1`（gunicorn_config.py 头部注释已同步），
并发吞吐用 `GUNICORN_THREADS`（gthread）扩展。
扩 worker 前置条件：上述四项完成进程安全改造（共享存储或 sticky 路由）。

## 六、数据流

### 6.1 请求数据流

```
用户输入 (JSON)
  │
  ▼
Flask/FastAPI 接口
  │
  ▼
agent.chat(user_input, session_id)
  │
  ▼
StateGraph.ainvoke(initial_state)
  │
  ├→ chat_node: RAG检索 → context
  ├→ plan_node: LLM规划 → task
  ├→ execute_node: CodeAgent → result_raw
  └→ finalize_node: 存库 → response
  │
  ▼
AgentResponse
  ├→ content: 最终回答
  ├→ sources: RAG 来源
  ├→ elapsed_seconds: 耗时
  └→ metadata: 追踪信息
```

### 6.2 状态持久化

```
checkpoint 存储 (PostgreSQL)
  │
  ├→ thread_id = session_id
  ├→ state = AgentState (JSON)
  ├→ node = 当前节点名
  └→ created_at = 时间戳
  │
  ▼
支持状态恢复：
  config = {"resume": true}
  state = await compiled.ainvoke(state, config)
```

> **现状（v1.4 标注）**：checkpointer 未启用——`graph.compile()` 未传入
> checkpointer 实例（需数据库连接池，待接入）。且 AgentState 携带
> `_trace` / `_code_agent` 等不可序列化运行时对象，启用前需先隔离到
> Checkpointer 之外。上图的 resume 能力当前不可用。

---

## 七、扩展指南

### 7.1 添加新工具

```python
# tools/my_tools.py
def my_tool(param1: str, param2: int) -> dict:
    """
    工具描述

    Args:
        param1: 参数1描述
        param2: 参数2描述
    """
    # 实现逻辑
    return {"result": "...", "_fields": ["result"]}
```

工具自动被 ToolProvider 扫描注册。必选工具（list_tools/search_tools/format_result/web_search）放在 `_MUST_HAVE` 中，provider 不扫描。

### 7.2 添加新技能

```bash
# 创建技能目录
mkdir skills/my_skill

# 编写 SKILL.md
cat > skills/my_skill/SKILL.md << 'EOF'
# My Skill

## 执行流程
1. 步骤1
2. 步骤2
EOF

# 可选：编写 run.py
cat > skills/my_skill/run.py << 'EOF'
def my_function(param: str) -> str:
    """技能函数描述"""
    return "result"
EOF
```

### 7.3 添加新 LLM Provider

```python
# llm/my_llm.py
from llm.base import LLMBase, ChatMessage

class MyLLM(LLMBase):
    def __init__(self, model, api_key, **kwargs):
        self.model = model
        self.api_key = api_key

    async def generate(self, messages, **kwargs) -> ChatMessage:
        # 调用 API
        return ChatMessage(role="assistant", content="...")
```

在 `factory.py` 中注册：

```python
def create_llm(config):
    if config["provider"] == "my": return MyLLM(...)
```

### 7.4 添加新 Embedding Provider

```python
# rag/embeddings.py
class MyEmbedding(EmbeddingBase):
    def embed_query(self, text: str) -> list[float]:
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...
```

在 `EmbeddingModel` 工厂中注册：

```python
if provider == "my": return MyEmbedding(...)
```

---

## 八、性能优化

### 8.1 工具发现优化

- **3 层可见性**：必选工具占 prompt token，领域工具不占，schema 仅 planning 可见
  - 2026-09-14 更正：前半句不成立——`tools=[]` 的工具描述不会进入 system prompt（原因见 §3.3 更正说明），
    所以"必选工具占 prompt token / 领域工具不占"这个差异**在当前实现下并不存在**；
    实际对 LLM 可见的只有 planning 段注入的 provider schema 与 `prompts/code_agent.yaml` 正文。
- **零启动开销**：无子进程启动（已移除 MCP），工具直接在 executor 命名空间
- **domain 过滤**：plan 选域后只加载域+通用工具，减少 executor 噪音
- **直接调用**：无需 router，LLM 直接调工具函数

### 8.2 RAG 优化

- **多路召回**：向量 + FTS + 关键词，减少漏召
- **RRF 融合**：无需训练，简单有效
- **Reranker 精排**：显著提升精度
- **web_search 补充**：RAG 不足时自动补充实时信息

### 8.3 LLM 调用优化

- **超时控制**：180s 上限
- **重试机制**：自动重试 1 次
- **温度控制**：0.1 保证稳定性

### 8.4 Token 优化

- **observations 截断**：保留最近 2 步完整，更早步骤截到 200 字符。
  trace 提取（_record_tool_calls_to_trace）在 run 结束后读取 memory，为拿到完整
  observations，v1.4 起依赖 run 中途缓存（execute_node 在截断回调生效前提取的
  步骤数据优先）。此阈值只影响 CodeAgent 上下文窗口，不再决定 trace 完整性。
- **工具描述精简**：只显示前 80 字符
- **上下文限制**：max_length=8000

---

## 九、监控与调试

### 9.1 日志系统

```python
# log.py 配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('agent.log'),
        logging.StreamHandler()
    ]
)
```

### 9.2 追踪系统

> **v1.4 标注**：现行追踪实现是 `utils/tracing.py` 的 `AgentTraceRecorder`
> （事件追加 → finish() 时写 JSONL + 提取结构化字段写 qd_traces）。
> 下述 `trace_collector.py` 是上一代遗留文件，当前主链路未使用。

```python
# trace_collector.py（遗留，仅存档参考）
class TraceCollector:
    session_id: str
    user_query: str
    steps: list

    def on_agent_finish(final_answer, total_steps, ...)
    def flush() -> root_id  # 存库
```

#### trace 记录的 smolagents 内部数据

`_record_tool_calls_to_trace()` 从 smolagents `agent.memory.steps` 提取：

| 数据 | 来源 | 用途 |
|------|------|------|
| `tool_name` / `tool_args` | ActionStep.tool_calls | 工具调用记录 |
| `observations` | ActionStep.observations | 工具返回结果（截断 2000 字符）|
| `model_output` | ActionStep.model_output | LLM 每步推理文本（截断 1000 字符）|
| `code_action` | ActionStep.code_action | LLM 生成的代码块（截断 500 字符）|
| `token_usage` | ActionStep.token_usage | 每步 token 消耗（input/output/total）|
| `plan` | PlanningStep.plan | 每轮规划文本（截断 2000 字符）|

### 9.3 关键日志点

| 日志前缀 | 说明 |
|----------|------|
| `[Chat]` | chat_node 相关 |
| `[Plan]` | plan_node 相关 |
| `[Execute]` | execute_node 相关 |
| `[Finalize]` | finalize_node 相关 |
| `[RAG]` | RAG 检索相关 |
| `[Inject]` | 工具注入相关 |
| `[WebSearch]` | 联网搜索相关 |

---

## 十、已知问题与待优化

### 10.1 已知问题

| 问题 | 状态 | 说明 |
|------|------|------|
| ~~CodeAgent planning prompt 工具注入失败~~ | ✅ 已修复 | 通过 {{tool_list}} 注入 |
| ~~LLM 客户端泄漏 (Event loop is closed)~~ | ✅ v1.3 修复 | LLMBase.close() + 资源清理 |
| ~~agent.py 连接泄漏~~ | ✅ v1.3 修复 | _load_analysis_memory_docs() 加 finally |
| ~~CodeAgent 输出截断（max_tokens 2048）~~ | ✅ v2.0 修复 | CODE_AGENT_MAX_TOKENS=4096 + finish_reason=length 检测 |
| ~~幻觉工具调用循环（create_file 等）~~ | ✅ v2.0 修复 | GuidedPythonExecutor 纠正 + 任务书边界明示 |
| ~~坏工具反复重试不收敛~~ | ✅ v2.0 修复 | ToolCircuitBreaker（连续失败2次短路） |
| ~~RAG 低相关度文档进上下文（RRF 长尾）~~ | ✅ v2.0 修复 | RAG_RRF_MIN_SCORE=0.005 长尾过滤 |
| 阶段重试大上下文 | ⚠️ 已知 | 重试复用 CodeAgent 记忆累积（实测 137k input tokens）；局部记忆待做 |
| 阶段间重数据依赖模型自觉调用暂存区 | ⚠️ 已知 | stage_* 工具已注入+纪律行提示，强制机制待做 |
| ~~模型偶发忘调 final_answer~~ | ✅ 已修（L13） | 三层防御：A 改 observation 的 None 歧义 / B 交付物钉死到 `final_answer` 参数 / C 原地重写或倒数第二步注入收尾指令（**详见 §3.13**）；实测由「4 步跑满 + 强制收尾」降到 1~2 步正常退出 |
| 沙箱白名单缺 io | ⚠️ 已知 | 模型写“捕获 print 输出”的代码时很自然地 `import io`（StringIO）⇒ 撞墙后整块代码中断、白烧一步（2026-09-14 实测）。扩大白名单属安全边界变更（io 能 open 文件），待评估，见 §10.3 |
| ~~拦截提示的"可用工具清单"无效~~ | ✅ 已修（L14） | 旧版只从 `static_tools` 取名（业务工具实际在 `custom_tools`），还与 `dir(builtins)` 混排后截断 `[:30]` ⇒ 清单恒为 ArithmeticError / Ellipsis / False…，真实工具一个不显示。实测误拦真实工具 `technical_analysis`，模型只能按提示放弃工具、改用纯 Python 硬算。改为从 custom_tools + static_tools 取、剔除内置与 BASE_PYTHON_TOOLS（详见 §3.3 更正块） |
| ~~工具注入到包装之前→沙箱内调不到~~ | ✅ 已修（L16） | `tool_functions` 被 `_wrap_stage_guard`/breaker 重新绑定后再注入 executor ⇒ 沙箱持旧（空）表：任务书与 `list_tools()` 都列得出，调用却被误报"幻觉调用"。注入点已移到包装之后，日志加"沙箱实持 N 个"（详见 §3.3 工具架构警示） |
| 能力层准入缺人工审核留痕 | ⚠️ 已知 | admission.json 有 19 项 `admitted: true`，但 L4 修复时是"按扫描报告语义复原"的**批量导入**，无逐项审核留痕；无法区分"审核通过"与"默认准入"。建议加 `reviewed_by/at` 并在 loader 校验 |
| llama.cpp router mode 不支持 embedding | ⚠️ 已知 | 需要两个实例 |
| PgVectorStore 性能瓶颈 | 📋 待优化 | 需引入 pgvector 扩展 |

### 10.2 待优化项

| 优化项 | 优先级 | 说明 |
|--------|--------|------|
| 引入 pgvector 扩展 | 高 | 数据库级 ANN 检索 |
| 统一 VectorStoreBase 接口 | 中 | PgVectorStore 继承基类 |
| ~~添加连接池~~ | ✅ 已有 | PostgresMemory 用 app.utils.db 连接池，_load_analysis_memory_docs 已修复 |
| 中文分词优化 | 低 | jieba 分词 + 停用词过滤 |
| Embedding 分块 | 低 | 长文本自动切分 |

### 10.3 设计稿指引与本轮新增待办（2026-09-14，来源：CodeBuddy agent 会话）

- **P2/P3 设计稿（未实施，待评审）**：`docs/AGENT_EXEC_MODE_DESIGN.md` —— 执行层形态（code 执行 vs 结构化 tool-calls）开关化 + 决策点收敛（外部 planner +1 次显式复盘）+ 工具面治理 + 事实权威层 + A/B 指标口径。评审通过后再进 §附录 A 的版本历史。
- **接线回归网**：`tests/test_wiring.py`（**21 项**，2026-09-14 更新：原 15 项 + L12 两项 + L13 三项 + L14 一项）。CI 只跑 compileall → 改 agent 后本地跑 `python -m pytest tests/test_wiring.py -v`；全量 `pytest tests/` 有与本网无关的既有失败，判断回归要看**增量**而非总数。
- **本轮新增待办**（证据见上表 v2.4「本轮新发现」）：
  | 待办 | 优先级 | 说明 |
  |--------|--------|------|
  | F1 阶段显式 `[]` 工具被当成"未声明" | 高 | 白名单静默退回域基调（实测 phase#2 = 62 工具），与任务书"仅限清单所列"矛盾 |
  | F1b 能力层不可达的留痕盲区 | 中 | v2.3 的 info 只覆盖"无阶段"，"有阶段但未点名能力"同样不可达却无声 |
  | F2 时间事实可被模型改写并被复盘继承 | 高 | 权威口径与模型自由文本同权，最终答案无校验。**2026-09-14 部分缓解**：时间事实改为**内联钉死**在原文时间词上（`今日(2026-09-14)`，见 §3.14），模型无法再当背景忽略；但**最终答案仍无校验**，生成环节仍可能改写 |
  | 死开关 `AGENT_TYPE` / `CODE_EXECUTION_TIMEOUT` | 低 | 仅 `env.example` 声明、代码零读取点（家族第 12/13 处） |
  | 环境漂移：`requirements` 要 `smolagents>=1.27` 实装 1.26.0 | 中 | `nodes.py:934/1385` 的判定逻辑照 1.27 写；A/B 前须对齐 |
  | qd_traces 列漂移 | 中 | `store.py` 写 `plan` 列但 DDL 无；`model/total_tokens/session_id/user_query` 有列无写入 |
  | ~~L12 沙箱 import 边界两处手工维护 + 撞墙后无纠正~~ | ✅ 已修 | 2026-09-14：`SANDBOX_AUTHORIZED_IMPORTS` 单一来源（executor + instructions 同源渲染）、`GuidedPythonExecutor` v3 覆盖 import 类错误并按次数提示、`plan_system.txt` 补沙箱能力边界；`tests/test_wiring.py` 新增 L12 两项 |
  | F3 实体污染无意图门控 | 高 | `nodes.py:432-438`：用户话里无 6 位数字就从 RAG 上下文捞股票代码 → 纯代码任务被强制"包含西安银行(600928)"（2026-09-14 CLI 实测）；方案：(a) 实体解析挪到意图分类后 (b) 代码/通用意图跳过 RAG 辅助注入 |
  | F6 沙箱错误不纳入熔断 | 中 | `ToolCircuitBreaker` 只管工具调用；沙箱 `InterpreterError`（如 import 越界）不经它 → 同一失败模式重复发生（实测 Step2 `sys` / Step4 `argparse` 两次），归 S4 守卫层 |
  | ~~L13 收尾 / 退出通道三层缺失~~ | ✅ 已修 | 2026-09-14：A `_clarify_empty_output` + B 交付物钉到 `final_answer` 参数 + C `_enforce_final_answer`（含「注入必须在倒数第二步」「上一步有错＝失败重试、不判重复」两条硬约束）。**已写入 §3.13 收尾与退出机制（事故高发区）**；回归网 +3 项（共 20 项） |
  | 沙箱白名单缺 io | 中 | 2026-09-14 实测：`import io`（StringIO 捕获输出）撞墙 ⇒ 整块代码中断、白烧一步。与安全边界相关（io 可 open 文件），需单独评估：放行 or 在拦截提示里给出等价替代写法 |
  | ~~L14 拦截提示的"可用工具清单"无效~~ | ✅ 已修 | 2026-09-14：清单改为从 `custom_tools + static_tools` 取、剔除内置与 BASE_PYTHON_TOOLS，`GuidedPythonExecutor` v4；回归网 +1 项（共 21 项）。**架构事实已写进 §3.3**（业务工具在 custom_tools，不在 static_tools） |
  | 工具不在当前阶段沙箱内（domain / phase 白名单） | ⚠️ 已知 | 与 L14 相邻但不同层：L14 是"清单显示不出来"，本项是"真的没注入"——planner 未在 `phase.tools` 点名或 domain 不匹配（F1 类）。修 L14 后可从日志清单直接判断是否属此类 |
  | 诊断记录 | — | `docs/AGENT_EXEC_MODE_DESIGN.md` 附录 D：CLI 复现「写跑马灯」的完整失败链路、代价量化（两次白打整份源码 ≈2.9 万 output tokens）与改动清单 |

---

## 附录 A：版本历史

### v2.1 (2026-09-13) — 规划分工显式化：阶段级预算 + 内部 planner 契约开关

| 类别 | 改动 | 文件 |
|------|------|------|
| 🐛 修复 | **内部 planner 判据失效**：`effective_interval = None if selected_skill else ...` 依赖技能存在；技能层清空后判据恒真 → 内部 planner 全程常开，与"简单指令关闭内部 planner"的设计意图相反。改为读阶段契约 `internal_plan`（缺失时按阶段预算判复杂度兜底） | nodes.py |
| ✨ 契约 | phases[] 增 `step_budget`（钳制 1~12，0=未指定）/ `internal_plan`（bool/None）：外部 planner 显式声明每阶段步数上限与是否开启执行器内部规划 | agents/task_agent.py (_normalize_phases), prompts/plan_system.txt |
| 🐛 修复 | **契约断裂**：plan_system 早已承诺"多阶段按每阶段 3~7 步分别给"，但契约里无该字段，执行侧所有阶段共用全局 step_budget。现 per-phase 预算真正生效 | nodes.py, agents/task_agent.py |
| ✨ 稳定 | 内部 planner 注入范围边界与收敛纪律（initial_plan / update_plan_post_messages）：不得扩范围、不得规划后续阶段、剩余步数少时优先收口 | prompts/code_agent.yaml |
| 📝 文档 | plan_system 示例去技能化（market_screener/stock_evaluation 已不存在 → selected_skill=null）＋域名说明修正（原写"finance、technical"，其中 technical 不存在；**实测真实域为 common / finance / quant**——finance=tools/finance 47 个工具，quant=capabilities 19 个能力） | prompts/plan_system.txt |
| ✨ 审计 | phase_start trace 增 step_budget / internal_plan：规划分工决策可事后对账 | nodes.py |

#### v2.1 附带审计：能力发现层（capabilities）断链清单（2026-09-13）

> 本节为**审计记录**。L1~L8 已于 2026-09-13 全部修复（见下方 v2.2），L9/L10 见 v2.3（L9 = 同一病灶的第三层：点名通道；L10 = 架构拆分遗留 import）、L11 见 v2.4（2026-09-14 由 `tests/test_wiring.py` 查出并修复）；修复前的断链状态与证据原样保留。

| # | 问题 | 证据 | 状态 |
|---|------|------|------|
| L1 | **`quant` 与 `finance` 是两个互斥的"金融域"**：域由目录名推导（`finance` = tools/finance，实测 47 个工具），而能力层硬编码 `domain="quant"`（19 个）。`_build_code_agent` 只加载 `common + 单个域` → planner 看到"可用工具域：finance, quant"却无法判断该选哪个，选任一都丢掉另一半 | nodes.py:160-162、capabilities/loader.py:148、agents/task_agent.py:930-934/1331-1335 | ✅ 已修（v2.2） |
| L2 | **`TimeResolver` 的 domain 从未传入**：调用点 `TimeResolver()`（无参）→ 恒为 `"general"` → `finance = self.domain == "finance"` 恒假 → 交易日常识链整体不执行（`finish` 恒为自然日、非交易日打回澄清永不触发、`今天=…；最近已收盘交易日=…` 标注永不输出）。DESIGN §14.5 所声称的"domain=finance 交易日口径 / 周六打回澄清"从未生效 | agents/task_agent.py:1741、resolvers/time.py:141-159 | ✅ 已修（v2.2） |
| L3 | **`FinanceFormatter` 双层断链**：① `formatters/__init__.py:21` 的 `from . import finance` 被注释掉 → `_REGISTRY` 恒空，`get_formatter()` 恒返回 `DefaultFormatter`；② 更深一层：注册 key 是领域名（`"finance"`）而查询只传 `entity_type`（`"stock"`）→ **即便恢复 import 也命中不了**。领域格式化分发形同虚设（且 `not selected_skill` 恒真 ⇒ 每次 finalize 都多一次 LLM 汇总调用） | formatters/__init__.py:21、formatters/base.py:47-55、nodes.py:1470-1484 | ✅ 已修（v2.2） |
| L4 | `admission.json` 中文不可逆损坏：实测 3779 字节 / 100 个字面 `0x3F` / 0 个非 ASCII 字节（写入时编码丢失，**不是**读取编码问题）→ 该文件定位是"人工过目唯一事实源"，说明文字全部不可读 | capabilities/admission.json | ✅ 已复原 |
| L5 | `_cap_text = prescan_tools(limit=0, …)` 计算后从未被使用，但每次 plan 都白跑一遍全量工具签名扫描 | agents/task_agent.py:950 | ✅ 已删 |
| L6 | `WRITE_PREFIXES` 在 scanner / loader 各存一份，靠注释"保持一致"手工同步 → 必然漂移且无告警 | capabilities/scanner.py:57、capabilities/loader.py:35 | ✅ 已去重 |
| L7 | planner 工具清单被 `prescan_tools(limit=60)` 按**字母序**截断（生产约 78 个工具 ⇒ 约 18 个对 planner 不可见，写不进 `phase.tools`）；而能力视图段**不截断**（19 个全列）→ 诱导 planner 优先选低阶 capabilities 而非语义化 finance 工具 | agents/task_agent.py:939/950、tools/base.py:373-378 | ✅ 已改相关性裁剪（v2.2） |
| L8 | **实体解析与澄清反问在线上从未执行**：`NodeContext` 的契约是 `EntityResolver`（nodes.py 调 `.resolve()`），但 task_agent 注入的是**裸函数** `_combined_resolver` → 调用处 `AttributeError` 被 `except Exception: logger.debug` 吞掉。后果：标的解析、交易日口径、非交易日/歧义澄清**全部静默失效**——与 L2 是同一病灶的两层（即便 L2 的 resolver 逻辑修对了，也永远跑不到） | agents/task_agent.py:1811、nodes.py:440/472 | ✅ 已修（v2.2，`CompositeResolver` + 异常升级 warning） |
| L9 | **能力层唯一注入途径挂在"编排结构"上**：L1 修复后能力刻意不属于任何可选域，其唯一点名通道是 `phases[].tools`；而 phases 是**编排契约**（单段任务本就不拆阶段）⇒ 没有阶段就没有点名通道，**能力层对单段任务永久不可达**，planner 提示里却完整展示 19 个能力。plan_system 自己写着"无 phases 的单段任务用不了能力"（把缺陷写进了规则），且 6 个制导示例里 5 个是不带 phases 的单段 + 另一条规则称"简单任务 1 个阶段即可" → **示例、规则、实现三者互斥** | agents/task_agent.py `_build_code_agent`（whitelist/domain/common 三支均不含能力）、capabilities/loader.py:12-13（自述"唯一注入途径是 stage 级 tools 白名单"）、prompts/plan_system.txt | ✅ 已修（v2.3，与阶段解耦的"附加点名"通道） |
| L10 | **框架级自动落盘从未执行**：`_auto_stage_phase_result` 仍 `from tools.staging import stage_write`，但 staging 在 v2.0 架构拆分中已迁至 `infra/staging.py` → ImportError 被 `except Exception: logger.debug` 静默吞掉。v2.0 承诺的"重数据由框架自动落盘、不依赖模型自觉"（阶段结果防截断的强制机制）**一次都没生效过** | nodes.py `_auto_stage_phase_result`（`except` 处原为 debug） | ✅ 已修（v2.3，改 `infra.staging` + 异常升级 warning） |

| L11 | **暂存区三件套在沙箱内从未存在**（2026-09-14 由 `tests/test_wiring.py` 查出，同族第 11 处）：`_build_code_agent` 的常驻注入写成"provider 里若已注册则补注入"，而 v2.0 把 staging 从 `tools/` 迁到 `infra/` 后它不再被目录扫描注册（`tools/base.py::_SKIP_FILES` 里仍留着迁移前的旧名 `staging_tools`）⇒ 该条件恒假。于是任务书"暂存区工具常驻可用"、`_ListToolsTool` 说明、`_wrap_stage_guard` 自动落盘后的"用 `stage_read(scope, name)` 读取"——全都在让模型调用一个**沙箱里不存在的函数**（Forbidden，且会被误判为"幻觉调用"） | agents/task_agent.py `_build_code_agent`（实测：真实 provider 81 个工具中不含 stage_*） | ✅ 已修（v2.4，直连 `infra.staging` 常驻） |

> 实测数据（本机裸环境，`common` 域因缺 pandas/requests 未计入）：finance=47、capabilities=19、common≈12 ⇒ 工具总数≈78，与既有记录一致。
> `capabilities/__init__.py` 自称"通用机制（不局限金融域）"，但 `scanner.SCAN_TARGETS` 硬编码 `app.market_cn.auto.*` → 壳通用、里硬编码（`SCAN_TARGETS` 待后续按需扩展；`domain="quant"` 已随 L1 修复）。

### v2.2 (2026-09-13) — 能力层审计修复：域模型数据化 + 领域格式化/时间口径接线 + 相关性裁剪

> 起因：用户指出 §14.2 能力发现层"是巨大的 bug、并没有模块化"。核实成立，根因是
> **把"工具来源(provenance)"当成了"领域(domain)"**。附带约束：领域不止 finance，
> 未来会有很多领域 → 一律按"可扩展登记/推导"处理，不做硬编码。

| 类别 | 改动 | 文件 |
|------|------|------|
| 🐛 修复 | **L1 域模型数据化**：能力层不再占用可选域（原 `domain="quant"` → 来源层标记 `CAPABILITY_DOMAIN`）。`ToolProvider` 新增 `_selectable_domains`（在 `scan_subdirectories` 按目录登记）与 `get_domains()`；planner 的"可用工具域"清单与 `selected_domain` 校验都改用它 → **域随目录自动增减，新增领域无需改代码** | capabilities/loader.py, tools/base.py, agents/task_agent.py |
| 🐛 修复 | **L2 时间口径接线**：`TimeResolver` 增 `domain` / `entity_type` 参数。chat 阶段先于 plan、拿不到 `selected_domain`；由 `CompositeResolver` 先跑实体解析，再按 `_ENTITY_DOMAIN` / `_WORD_DOMAIN` 三级倒推领域（见下方两条）；交易日口径判定由 `self.domain == "finance"` 改为查 `TRADING_CALENDAR_DOMAINS` 登记表 | resolvers/time.py, agents/task_agent.py |
| 🐛 修复 | **L3 领域格式化接线（双层）**：① `formatters/__init__.py` 把被注释掉的 `from . import finance` 改为 **pkgutil 自动发现**——新增 `formatters/<domain>.py` 即自动注册；② 新增 `get_formatter(entity_type, domain)`，查找顺序 **domain（领域级标准输出，多领域复用）→ entity_type（领域内单实体定制）→ default**，finalize 侧传 `state["selected_domain"]`；另增 `list_formatters()` 供启动自检 | formatters/*, nodes.py |
| 🐛 修复 | **L7 相关性裁剪**：`prescan_tools(limit, query)` 超上限时按与本次需求的相关度排序再截断（ASCII 词 + 中文 2-gram 打分；命中工具名权重 6 > 命中描述权重 2；零分并列按名称稳定排序；无任何相关信号时退化为字母序以避免随机丢弃），被裁数量如实写入清单末尾。上限改为 env 可配 `PLAN_TOOL_LIST_LIMIT`（默认 60） | utils/prescan.py, agents/task_agent.py |
| 🐛 修复 | **L8 实体解析/澄清接线**：`entity_resolver` 注入的裸函数 `_combined_resolver` 不满足 `EntityResolver` 契约（nodes.py 调 `.resolve()`）→ `AttributeError` 被 `except: logger.debug` 吞掉 ⇒ **实体解析与澄清反问线上从未执行**。改为注入 `CompositeResolver`（新增 `resolvers/composite.py`），并把该处异常由 debug 升级为 warning（接线错误必须可见） | resolvers/composite.py, agents/task_agent.py, nodes.py |
| ✨ 功能 | **通用澄清契约**（用户裁定："无法准确判断就应该反问，拿到准确信息才执行"）：`ResolveResult` 增 `clarify_question` + `needs_clarify`；`chat_node` 改为**域无关**检测（字段优先，兼容 `*_clarify` 标记）——任何领域/解析器都能反问，新增领域无需改 nodes.py | resolvers/base.py, nodes.py, resolvers/time.py |
| ✨ 功能 | **标的歧义反问**：`StockResolver` 原以 `limit=1` 调 `resolve_stock`（DB 至多回 1 条 ⇒ 歧义在结构上不可见，"静默取首个候选"，选错标的最致命）→ 改取多候选，无法用**精确同名收敛**时反问用户确认（列出候选与代码） | resolvers/stock.py |
| ✨ 功能 | **窗口不明反问**：`TimeResolver` 原把"最近/近期"静默当"近 5 个交易日"（自己都标注"可按需调整"）→ 改为反问（交易日常见 5/10/20/60 个交易日；自然日 7/30/90 天），带窗口的说法不触发 | resolvers/time.py |
| ✨ 功能 | **领域三级倒推**（chat 先于 plan、拿不到 `selected_domain`）：显式 `domain` > 实体类型（`_ENTITY_DOMAIN`）> 输入语汇（`_WORD_DOMAIN`，复用 `_MARKET_WORDS`）——纯"最近的行情怎么样"无实体也能落到 finance，否则会问出自然日错口径 | resolvers/time.py |
| 📝 文档 | prompt 域与能力说明改写：删去虚构域 `quant`，示例全部改 `"finance"`；能力段契约改为"只能由阶段 tools 白名单点名注入" | prompts/plan_system.txt |
| ✅ 验证 | 临时脚本全绿后删除：`tmp/_verify_wiring.py`（formatter 自动注册/domain 命中、`get_formatter("stock")` 不误命中、L7 排序/退化、`get_domains()` 排除来源层、周六"今天行情"打回澄清）；`tmp/_verify_clarify.py` **25 项全过**（澄清契约、标的歧义/精确同名收敛/6 位代码、窗口不明/带窗口不触发、三级领域倒推、**注入对象满足 `EntityResolver` 契约**、组合器澄清优先短路与多标的合并） | — |

### v2.3 (2026-09-13) — 点名通道与编排结构解耦：单段任务可达能力层（L9）+ 遗留接线（L10）

> 起因：核验"能力只认 `phases[].tools` 一个点名通道"这条设计时发现——**把"工具可见性"耦合到了"编排结构"**。
> phases 回答"怎么分步、怎么验收"，点名回答"这一轮允许看到哪些函数"，两者正交；而单段任务（不拆阶段）因此
> 在所有分支上都拿不到能力层。修法是把点名通道独立出来，而不是给 LLM 再补一句提示。

| 类别 | 改动 | 文件 |
|------|------|------|
| 🐛 修复 | **L9 单段附加点名通道**：plan 契约新增**顶层 `tools`**，语义 = **附加点名**（在 `selected_domain` 基调〔域+通用〕之上做**并集**，不挤掉域工具）；`phases[].tools` 仍是**独占白名单**、语义不变，两者同时出现时以 phases 为准（避免静默放宽每个阶段的工具面）。新增 `_normalize_plan_tools`（保序去重 / 容错逗号串 / 落空名字告警）；`_build_code_agent` 增 `extra_tools` 参数（`tools` 非空时不生效） | agents/task_agent.py, nodes.py |
| 🐛 修复 | **prompt 三处互斥**：删去"无 phases 的单段任务用不了能力"（缺陷被写成规则），改为按有无 phases 二分点名通道；示例补单段点名用法；"简单任务 1 个阶段即可"与示例（单段）矛盾 → 统一为"单段省略 phases"。另修正能力段头注（原写"仅当本阶段 tools 白名单"，未提单段通道） | prompts/plan_system.txt, agents/task_agent.py |
| 🐛 修复 | **L10 自动落盘接线**：`from tools.staging` → `from infra.staging`（v2.0 架构拆分遗留），异常由 debug 升级为 warning。此前 ImportError 被静默吞掉 ⇒ 阶段结果自动落盘从未执行 | nodes.py |
| 🐛 修复 | **工具契约陈旧实例**：单段路径复用 `state["_code_agent"]` 时未校验工具契约（域+点名单）→ 复盘重规划若改变二者，沙箱里没有新点名的工具而任务书却写着"可直接调用"，模型照做即 Forbidden（会被误判为幻觉调用）。改为契约不一致即重建实例 | nodes.py |
| ✨ 稳定 | 单段任务书补【附加点名工具（括号内为参数名）】：与阶段任务书同规则（2026-09-12 实证：只给名字会诱发参数猜测、连环 TypeError 烧步数），避免两条执行路径行为漂移 | nodes.py |
| ✨ 审计 | 能力可达性可对账：无阶段且未点名能力时记 info（"能力层不可达"）；`plan_result` trace 增 `plan_tools` / `capabilities_named`（点名生效 vs 落空各自留痕） | agents/task_agent.py |
| 📝 文档 | 能力视图构造抽 `_capability_names()` 复用（原两处各写一遍来源层过滤）；`_plan` docstring 补 `phases`/`plan_tools` 返回说明 | agents/task_agent.py |
| ⚠️ 待同步 | `docs/AGENT_ACCOUNTABLE.md` §14.2/§14.4 仍为 v2.2 前的表述（"注册 domain=quant"、"规划器据此把能力函数编排进 phases[]"）——该文件属共享区域（协作约定只增不删），本次未改，标记待同步 | docs/AGENT_ACCOUNTABLE.md |
| ✅ 验证 | 临时脚本全绿后删除：`tmp/_verify_plan_tools.py` **30 项全过**（规格化 7 项含逗号串/幻觉名；**真实构建 CodeAgent 断言注入工具集** 5 项：并集不挤掉域工具、落空不入沙箱、无域+点名可达、`tools` 非空时 extra 不生效、无点名时与旧版一致；能力名过滤 2 项；模板可 format + 契约/规则/示例一致性 6 项；状态字段 1 项；`infra.staging` 导入 2 项；接线审计 7 项） | — |

### v2.4 (2026-09-14) — 接线回归网（P0）+ L11 暂存区常驻 + 真实链路 E2E（P1）

> 起因：v2.3 的验证脚本按惯例"跑完即删"。但 L1~L10 全是同一类问题——**声明了、没接上、静默失效**，
> 靠一次性脚本发现 ⇒ 同类断链反复复发。本轮把那些断言固化进测试套件（P0），并在建设过程中
> 又查出同族第 11 处（L11）；随后用真实链路 E2E 验证 v2.3 的能力点名通道（P1）。

| 类别 | 改动 | 文件 |
|------|------|------|
| ✅ 新增 | **接线契约回归测试**（15 项）：能力层来源层过滤（L1）、`_normalize_plan_tools`/`_normalize_phases` 契约（L9）、**真实构建 CodeAgent 断言沙箱工具面**（附加点名并集 / 白名单独占 / 无域可达 / 无点名回退）、formatter 目录自动发现与 key 语义（L3）、`EntityResolver` 契约＋澄清优先短路＋故障降级（L8）、`TimeResolver` 三级领域倒推（L2）、阶段结果自动落盘读写闭环（L10）、暂存区常驻（L11）、真实 provider 全链不变式。此后 agent 接线改动可一条命令回归：`python -m pytest tests/test_wiring.py -v`（CI 只跑 compileall，不跑测试；本文件用合成 provider + 桩 model + 桩交易日历，不依赖外部服务与 LLM） | tests/test_wiring.py |
| 🐛 修复 | **L11 暂存区三件套在沙箱内从未存在**（见审计表 L11）：常驻注入改为直连 `infra.staging` 本体（框架工具 ≠ 插件工具，不依赖 provider 扫描结果） | agents/task_agent.py |
| 🔁 可复测 | 新增 E2E 驱动 `tmp/_e2e_capability_naming.py`：不经 message_queue（本机 Redis 未起），直接 `agent.run_agent` 走同一个图，可反复手动运行；顺带证明"跳过投递不影响图逻辑" | tmp/ |
| ✅ 验证 | ① `pytest tests/test_wiring.py` **15 项全过**；`pytest tests/` 与改动前同为 29 failed / 44 errors（既有失败，与本改动无关）。② 真实 provider 实测：81 工具 / finance=59 / capability=19 / common=3，`stage_*` **不在 provider 内**（L11 证据）。③ **E2E（真实 LLM，138s）**：`顶层 tools 附加点名 4 个: [list_signals, get_active_signals, strategy_labels, resolve_stock]` → `[Execute] 附加点名工具 4 个` → `加载 65 个工具（通用+finance+附加点名4）` → `附加点名生效 4 个`，任务书出现 `【附加点名工具（可直接调用；括号内为参数名）】`，模型**实际在沙箱里调用了 `list_signals(...)`**（v2.3 前单段任务不可达能力层）；无 `附加点名落空`；复盘重规划走阶段路径，`phase 白名单：加载 3 个工具` + `新建 CodeAgent（工具契约 ('finance', (...))）` 生效；`qd_traces` 写入 root_id=1940 children=14 | — |
| ⚠️ 本轮新发现（未改） | **F1 阶段"显式 0 工具"被当成"未声明工具"**：planner 明确写 `#2汇总清单[0工具]`，`_build_code_agent` 视空列表为"无白名单"⇒ 回退域基调，实测 phase #2 拿到 **62 个工具**，而同一份任务书写着"沙箱内只存在【本阶段可用工具】清单所列函数——不存在其他任何函数"（语义自相矛盾；白名单"独占"设计被静默放宽）。**F1b 审计留痕盲区**：v2.3 的"能力层不可达"info 只覆盖"无阶段"，此处"有阶段但阶段未点名能力"同样不可达却无任何留痕。**F2 事实无权威层**：解析器给出 `今天=2026-09-14（当前交易日）`（正确，周一），执行段模型自造"周日非交易日"，复盘又把该错误事实**继承进第二轮 plan text**（`2026-09-14,当前为周日非交易日`）——harness 对"模型改写时间事实"无校验。**F3 执行形态代价**：单段 7 步耗尽预算（`hit_max_steps=True`），中途因把返回 dict 当 list 切片报 `InterpreterError` 烧步数——印证 ①"在给 smolagents 打补丁"的收益点（见 P2 实验） | agents/task_agent.py, nodes.py |

### v2.0 (2026-09-11 ~ 09-12) — 双规划器阶段契约 + 能力发现层 + 稳定性防线

> 完整设计说明见 `docs/AGENT_ACCOUNTABLE.md` §14；过程日志见 `.workbuddy/memory/2026-09-12.designer.md`。

| 类别 | 改动 | 文件 |
|------|------|------|
| ✨ 架构 | 双规划器阶段契约：外部 planner 产 phases[]（goal/tools/deliverable/acceptance/on_fail），execute 降为单 phase 执行器，route_after_execute 条件边循环；PLAN_MAX_PHASES=5 | agents/task_agent.py, nodes.py, prompts/plan_system.txt |
| ✨ 架构 | 能力发现层：capabilities 包（scanner/loader/admission）+ planner 能力视图；19 项数据函数注册 domain="quant" | capabilities/*, agents/task_agent.py, nodes.py |
| ✨ 稳定 | SSE 事件格式修复（翻译层 + node_start 补发 + 前端预声明）——半流式可见 | flask_app.py, agents/task_agent.py, nodes.py |
| 🐛 修复 | RAG 静默失效：chat_node 局部 import os 作用域污染（UnboundLocalError） | nodes.py |
| ✨ 稳定 | 提取层加固 v4：健全性校验/伪标签防线/散落代码抢救/围栏救援；合法输出零干预 | infra/resilient_parse.py |
| ✨ 稳定 | 幻觉调用纠正：GuidedPythonExecutor（Forbidden 错误 → 可用清单+二选一指引） | infra/guided_executor.py |
| ✨ 稳定 | 工具失败熔断：ToolCircuitBreaker（连续失败2次短路，元工具豁免，实例隔离） | infra/breaker.py |
| ✨ 稳定 | 三道守卫：AGENT_RUN_WALL_TIMEOUT（非主线程墙钟）/ SSE_HARD_TIMEOUT_EXTRA（硬上限+可读超时文案）/ LLM 惰性客户端超时补挂 | nodes.py, flask_app.py, agents/task_agent.py |
| 🐛 修复 | 复用 Agent 重试时 LLM 180s 超时丢失（close 后惰性重建未补挂） | agents/task_agent.py, nodes.py |
| 🐛 修复 | main() 进程入口被注册为工具（mcp.run 挂死进程）→ CLI 入口名注册黑名单 + main→serve | tools/base.py, tools/mcp_bridge.py |
| ✨ 功能 | 暂存区：stage_write/read/list（跨阶段重数据，scope/文件名白名单+2MB 上限） | infra/staging.py, nodes.py |
| ✨ 功能 | 取数批量化：27 个 codes 工具 [支持批量] 标注 + 任务书/规划规则 | utils/prescan.py, nodes.py, prompts/plan_system.txt |
| ✨ 功能 | 预扫：技能 AST 签名解析 + 工具签名清单注入规划提示（替代裸名列表） | utils/prescan.py, agents/task_agent.py |
| ✨ 功能 | 技能阶段清单：SKILL.md 可选 `## stages` 段 → planner 按阶段映射切片；market_screener 样板 | agents/task_agent.py, skills/market_screener/SKILL.md |
| ✨ 功能 | TimeResolver：时间实体解析（domain=finance 交易日口径 / general 自然日；周六"今天行情"打回澄清） | resolvers/time.py, agents/task_agent.py, nodes.py |
| 🐛 修复 | RAG RRF 长尾过滤：RAG_RRF_MIN_SCORE=0.005（低相关文档不再进上下文） | nodes.py |
| 🐛 修复 | _LLMAdapter._desired_timeout 惰性补挂（复用 Agent 重试时 180s 超时丢失） | agents/task_agent.py |
| 🐛 修复 | ToolProvider CLI 入口名黑名单（main/serve 等不注册） | tools/base.py |
| 🐛 修复 | 响应元数据 phase_types 适配契约格式；代码类任务边界规则（禁止规划「写文件→运行文件」） | agents/task_agent.py, prompts/plan_system.txt, nodes.py |
| ✨ 架构 | 框架内部机制（infra/）：breaker/guided_executor/resilient_parse/staging 移出 tools/，标准 import，消除动态加载 | infra/*, agents/task_agent.py, tools/base.py |

### v1.4 (2026-09-10) — 健壮性审计修复

| 类别 | 改动 | 文件 |
|------|------|------|
| P0 | 移除对不存在的 store.update_path_cache 的调用，评估计数与自动权重更新恢复 | chain/evaluator.py |
| P0 | skill 节点回填 direction/score 等评估字段（继承根节点决策），权重聚合数据源接通 | utils/tracing.py |
| P0 | 评估毒丸治理：失败计数写 error 列，>=5 次置 unverifiable 出队 | chain/store.py, chain/evaluator.py |
| P0 | RAG 阈值按分数来源分流：rerank_score 用 0.7，RRF 分数按 top_k 截断 | nodes.py |
| P0 | 共享 LLM 客户端不再任务级 close（跨 loop 竞态根因），连接池随进程存活 | agent.py, message_queue.py |
| P1 | 复盘循环适配 smolagents>=1.27：检测 memory 末步 AgentMaxStepsError 标记 | nodes.py |
| P1 | nodes.py 补 import json（trace 提取 NameError 静默吞） | nodes.py |
| P1 | trace.finish 幂等 + 结构化字段提取改用格式化前原始输出 | utils/tracing.py, nodes.py |
| P1 | 失败工具检测双通道：error 键值 + ToolCall 反查（_failed_tool 无生产者） | nodes.py |
| P1 | chain_name 接真实意图（chat_node → set_intent），不再恒为 "agent" | nodes.py, utils/tracing.py |
| P1 | step_budget 钳制 [1,20] + int 强转 | agents/task_agent.py |
| P1 | plan 上下文挂 NodeContext（并发隔离），_plan 增加 plan_ctx 参数 | nodes.py, agents/task_agent.py |
| P1 | feedback 复合词白名单 + 问句消歧（"垃圾股"/"数据不对？"不再误罚） | feedback.py |
| P1 | 带具体时间的提醒归 cron（intent prompt 规则修正） | prompts/intent_classifier.txt |
| P2 | ToolProvider 进程级缓存（首扫复用，不再每请求重扫） | nodes.py |
| P2 | _LLMAdapter.close() 补充 + execute 收尾调用（同步客户端泄漏） | agents/task_agent.py, nodes.py |
| P2 | ToolCall 提取改用原生 dataclass 字段（function dict 误读） | nodes.py |
| P2 | StockResolver 线程隔离（akshare 同步 HTTP 阻塞事件循环） | resolvers/stock.py |
| P2 | 队列背压：满时 5s 超时快速失败，不再无限挂起 | message_queue.py |
| 文档 | 4.2/6.2/8.4/9.2 与实现对齐（复盘触发条件/checkpointer 现状/截断口径/trace 实现归属） | DESIGN.md |

### v1.3 (2026-07-22)

| 类别 | 改动 | 文件 |
|------|------|------|
| 🐛 修复 | `run_agent()` LLM 客户端泄漏：删 `_client=None`，加 `agent.llm.close()` | agent.py |
| 🐛 修复 | `_load_analysis_memory_docs()` 连接泄漏：加 `finally: conn.close()` | agent.py |
| 🐛 修复 | MQ worker 异常静默吞掉：加 `logger.error` | message_queue.py |
| 🐛 修复 | cache import 路径不一致：`from app.agent.log` → `from log` | cache.py |
| ✨ 新增 | `LLMBase.close()` 基类方法 + `OpenAILLM.close()` 关闭 httpx 客户端 | llm/base.py, llm/openai_llm.py |
| ✨ 新增 | `PostgresMemory.search()` 全文检索（PG FTS） | memory/postgres_memory.py |
| ✨ 新增 | `ChatHistoryRetriever` 聊天历史检索器 | rag/retriever.py |
| ✨ 新增 | RAG 第 3 路召回：聊天历史检索（权重 0.4） | agent.py |
| ✨ 新增 | `CHAT_HISTORY_SEARCH_ENABLED` 环境变量总开关 | agent.py |
| ✨ 新增 | trace 记录 smolagents 推理链（model_output/code_action/token_usage） | nodes.py |
| ✨ 新增 | trace 记录每轮 PlanningStep（不只是最后一轮） | nodes.py |
| 🐛 修复 | PostgresMemory FTS 列检测：PL/pgSQL 内 Identifier 引号导致 information_schema 匹配失败，改用 Python 层参数化查询 | memory/postgres_memory.py |

### v1.2 (2026-07-18)
- 初始版本

---

## 附录 B：依赖清单

```txt
# 核心依赖
smolagents[openai]>=1.27.0    # ReAct agent 框架
openai>=1.0.0                  # OpenAI 兼容 API
flask>=2.3.3                   # Web 框架
psycopg2-binary>=2.9.9        # PostgreSQL 驱动

# RAG 依赖（可选）
# sentence-transformers>=2.0.0 # 本地 embedding/reranker
# qdrant-client>=1.0.0        # 向量存储

# 联网搜索（可选）
requests>=2.28.0               # HTTP 请求
# tavily-python>=0.3.0        # Tavily（1000次/月免费）
# baidusearch>=1.0.0          # 百度搜索

# 工具依赖
akshare>=1.12.0                # A 股数据
pandas>=1.5.0                  # 数据处理
redis>=5.0.0                   # Redis 缓存（可选）
```

## 附录 C：API 接口

### POST /api/agent-v2/chat

普通对话（SSE 流式）

**请求：**
```json
{
    "message": "分析300129",
    "session_id": "optional-session-id"
}
```

**响应（SSE）：**
```
data: {"type": "done", "content": "**股票名称**: ...", "session_id": "..."}
```

### POST /api/agent-v2/task

带工具调用的任务（SSE 流式）

### GET /api/agent-v2/health

健康检查

### GET /api/agent-v2/info

配置信息

### GET /api/agent-v2/tools

工具列表（通过 ToolProvider 动态发现）

### GET /api/agent-v2/skills

技能列表

---

> 文档结束。如有疑问，请联系项目维护者。
