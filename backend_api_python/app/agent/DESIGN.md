# QuantDinger Agent 模块设计文档

> 最后更新: 2026-07-22
> 版本: v1.3
> 状态: 生产环境运行中

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
│   └── task_agent.py     # TaskAgent — 核心任务执行器
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
├── tools/                # 工具集（78 个公开函数）
│   ├── base.py           # Tool 基类 + ToolProvider 统一注册表
│   ├── format_utils.py   # 格式化工具（必选）
│   ├── web_search_tools.py # 联网搜索（四引擎降级）
│   ├── pagination.py     # 分页工具
│   ├── mcp_bridge.py     # MCP 桥接
│   └── finance/          # 金融领域工具（27 个模块）
│       ├── analysis_tools.py    # 技术分析（1613行，最大）
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
result = search_tools("资金")                    # 发现（必选工具，system prompt 可见）
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
- `_REGISTRY`：全局注册表，key=entity_type, value=formatter_class
- `@register_formatter()`：装饰器，注册 formatter
- `get_formatter()`：根据 entity_type 查找 formatter，找不到返回 default

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

- **observations 截断**：保留最近 2 步完整
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

```python
# trace_collector.py
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

---

## 附录 A：版本历史

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
