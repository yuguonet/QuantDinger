# QuantDinger Agent 模块设计文档

> 最后更新: 2026-07-18
> 版本: v1.1
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
│  flask_app.py  ←→  agent.py  ←→  graph.py                                      │
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
├── checkpointer.py       # 状态持久化（PostgreSQL）
├── trace_collector.py    # 决策追踪收集器
├── feedback.py           # 负面反馈检测
├── json_extractor.py     # JSON 提取工具
├── log.py                # 日志配置
├── cache.py              # 缓存工具
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
├── tools/                # 工具集（72 个）
│   ├── base.py           # Tool 基类 + ToolProvider 统一注册表
│   ├── data_tools.py     # 数据查询
│   ├── analysis_tools.py # 技术分析
│   ├── indicator_tools.py # 指标计算
│   ├── news_search_tools.py # 新闻搜索
│   ├── web_search_tools.py # 联网搜索
│   ├── screener_tools.py # 选股器
│   ├── format_utils.py   # 格式化工具（必选）
│   └── ...               # 更多领域工具
│
├── skills/               # 技能系统
│   └── market_screener/  # 市场筛选技能
│
├── prompts/              # 提示词模板
│   ├── plan_system.txt   # Plan 阶段系统提示
│   └── code_agent.yaml   # CodeAgent 提示词模板
│
└── utils/                # 工具函数
    ├── json_parser.py    # JSON 安全解析
    ├── md_format.py      # Markdown 格式化
    ├── tracing.py        # 追踪记录
    └── prompt_loader.py  # 提示词加载
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
| `finalize_node` | 存库 + 记忆 + 后处理 | result_raw | 最终输出 |

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
┌─────────────────────────────────────────────────┐
│           MultiRouteRetriever                   │
│                                                 │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐        │
│  │ 向量检索 │  │ FTS检索  │  │ 关键词  │        │
│  │(bge-m3) │  │(Postgres)│  │ (BM25)  │        │
│  └────┬────┘  └────┬────┘  └────┬────┘        │
│       │            │            │              │
│       └────────────┼────────────┘              │
│                    ▼                           │
│            RRF 融合排序                         │
│                    │                           │
│                    ▼                           │
│          BGE-reranker 精排                     │
│                    │                           │
└────────────────────┼───────────────────────────┘
                     ▼
               检索结果
```

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
class Retriever:           # 单路检索器
class KeywordRetriever:    # 关键词召回（BM25）
class MultiRouteRetriever: # 多路召回 + RRF + Reranker
class BGEReranker:         # BGE-reranker 精排
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

#### 工具分类

| 类别 | 工具数 | 说明 |
|------|--------|------|
| 数据查询 | 5 | 股票信息、行情、财务 |
| 技术分析 | 18 | 指标、趋势、形态 |
| 指标计算 | 3 | RSI、MACD、KDJ 等 |
| 新闻情报 | 7 | 新闻搜索、舆情分析 |
| 联网搜索 | 12 | Bocha/Tavily/百度/SearXNG |
| 选股筛选 | 6 | 条件选股、板块筛选 |
| 其他工具 | 50+ | 资金流、龙虎榜、回测等 |
| **总计** | **72** | ToolProvider 自动扫描注册 |

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

| 实现 | 存储 | 持久化 | 适用场景 |
|------|------|--------|----------|
| `LocalMemory` | 内存 dict | ❌ | 开发/测试 |
| `PostgresMemory` | PostgreSQL | ✅ | 生产环境 |
| `RedisMemory` | Redis | ✅ | 高并发场景 |

### 3.8 可追责链 (`chain/`)

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

### 3.9 技能系统 (`skills/`)

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

### 3.10 提示词系统 (`prompts/`)

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
│ 1. 领域特化：提取股票数据                                    │
│ 2. EvalNode 存库（root_id=1120）                            │
│ 3. TraceCollector 存库                                      │
│ 4. 追加失败工具信息                                          │
│ 5. 保存 memory                                              │
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
| llama.cpp router mode 不支持 embedding | ⚠️ 已知 | 需要两个实例 |
| PgVectorStore 性能瓶颈 | 📋 待优化 | 需引入 pgvector 扩展 |

### 10.2 待优化项

| 优化项 | 优先级 | 说明 |
|--------|--------|------|
| 引入 pgvector 扩展 | 高 | 数据库级 ANN 检索 |
| 统一 VectorStoreBase 接口 | 中 | PgVectorStore 继承基类 |
| 添加连接池 | 中 | psycopg2 pool / qdrant 单例 |
| 中文分词优化 | 低 | jieba 分词 + 停用词过滤 |
| Embedding 分块 | 低 | 长文本自动切分 |

---

## 附录 A：依赖清单

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

## 附录 B：API 接口

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
