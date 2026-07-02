# Agent Template 项目指南

> 一个面向生产原型的 Python AI Agent 项目模板，内置多模型切换、Function Calling 工具调用、RAG 多路召回、对话记忆、LangGraph 编排示例，以及完整执行过程 Trace 复盘能力。

---

## 能力概览

- **多模型抽象**：统一封装 DashScope 和 OpenAI 兼容接口，支持 DeepSeek、GLM、Moonshot、Ollama 等。
- **Agent 分层**：提供纯对话 `ChatAgent` 和支持 ReAct 工具循环的 `TaskAgent`。
- **工具系统**：基于 OpenAI Function Calling schema，支持工具注册、自动执行、结果回传。
- **RAG 增强**：支持 Qdrant 向量检索、关键词召回、多路召回、RRF 融合排序。
- **记忆系统**：支持本地内存和 Redis 滑动窗口记忆。
- **LangGraph 示例**：展示如何把检索、规划、工具调用、回答生成编排成显式图流程。
- **Trace 复盘**：记录一次请求中的 RAG、Memory、LLM、Tool 调用全过程，便于排查和评估。

---

## 目录结构

```text
agent_template/
├── agents/                         # Agent 核心层
│   ├── base.py                      # AgentBase + AgentResponse
│   ├── chat_agent.py                # 对话型 Agent
│   └── task_agent.py                # ReAct 工具调用 Agent
│
├── config/                         # 配置管理
│   ├── loader.py                    # YAML + ENV 配置加载
│   ├── settings.py                  # dataclass 配置结构
│   ├── settings.yaml                # 基础配置
│   └── settings.development.yaml    # 开发环境覆盖配置
│
├── examples/                       # 示例代码
│   ├── chat_example.py              # 基础对话
│   ├── tool_agent_example.py        # 工具调用
│   ├── model_switch_example.py      # 模型热切换
│   ├── rag_example.py               # RAG 多路召回问答
│   ├── multi_agent_example.py       # 多 Agent 协作
│   └── langgraph_orchestration_example.py # LangGraph 编排
│
├── llm/                            # LLM 多模型抽象层
│   ├── base.py                      # ChatMessage / LLMResponse / LLMBase
│   ├── dashscope_llm.py             # DashScope 实现
│   ├── openai_llm.py                # OpenAI 兼容实现
│   └── factory.py                   # LLMFactory / create_llm
│
├── memory/                         # 对话记忆
│   ├── base.py                      # MemoryBase
│   ├── local_memory.py              # 本地内存记忆
│   └── redis_memory.py              # Redis 记忆
│
├── rag/                            # RAG 检索增强
│   ├── embeddings.py                # DashScope/OpenAI Embedding
│   ├── vector_store.py              # Qdrant 向量存储
│   └── retriever.py                 # 向量召回、关键词召回、多路召回
│
├── skills/                         # 可复用 LLM 技能
│   ├── base.py
│   ├── json_extractor.py
│   ├── document_parser.py
│   └── report_generator.py
│
├── tools/                          # Function Calling 工具系统
│   ├── base.py                      # Tool / ToolResult
│   ├── registry.py                  # ToolRegistry
│   └── builtin/
│       ├── code_executor.py         # Python 代码执行工具
│       ├── file_reader.py           # 文件读取工具
│       └── web_search.py            # 搜索工具示例桩
│
├── utils/                          # 通用工具
│   ├── json_parser.py               # JSON 容错解析
│   ├── logger.py                    # 标准日志
│   ├── prompt_loader.py             # Prompt 模板加载
│   └── tracing.py                   # Agent 执行 Trace
│
├── prompts/                        # Prompt 模板目录
├── main.py                         # FastAPI 服务入口
├── requirements.txt                # Python 依赖
└── .env.example                    # 环境变量模板
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

复制环境变量模板：

```bash
cp .env.example .env
```

至少配置：

```bash
AGENT_ENV=development
AGENT_LLM_API_KEY=your-api-key
```

默认配置使用 DashScope：

```yaml
llm:
  provider: "dashscope"
  model: "qwen-plus"
```

如果使用 DeepSeek、GLM、Moonshot、Ollama 等 OpenAI 兼容接口，将 `provider` 改为 `openai` 并配置 `base_url`。

### 3. 启动 FastAPI 服务

```bash
uvicorn main:app --reload --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

对话请求：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "session_id": "test"}'
```

响应示例：

```json
{
  "reply": "你好！有什么可以帮你？",
  "session_id": "test",
  "trace_id": "..."
}
```

`trace_id` 可用于定位 `traces/agent_runs.jsonl` 中的完整执行记录。

---

## 配置系统

配置加载入口是 `config.loader.get_settings()`，优先级从低到高：

1. `config/settings.py` 中的 dataclass 默认值
2. `config/settings.yaml`
3. `config/settings.{env}.yaml`
4. `AGENT_` 前缀环境变量

环境变量映射规则：

```text
AGENT_LLM_API_KEY       -> settings.llm.api_key
AGENT_RAG_QDRANT_HOST   -> settings.rag.qdrant_host
AGENT_MEMORY_REDIS_URL  -> settings.memory.redis_url
AGENT_ENV               -> 当前环境名称
```

配置加载器会把环境变量中的 `int`、`float`、`bool`、`list` 字符串转换为 dataclass 字段对应类型。

---

## LLM 多模型抽象

核心对象：

- `ChatMessage`：统一消息结构
- `LLMResponse`：统一模型响应结构，包含内容、tool_calls、token、finish_reason
- `LLMBase`：所有模型 Provider 的抽象基类
- `LLMFactory` / `create_llm`：模型工厂

支持的 Provider：

| Provider | 模型示例 | 配置方式 |
| --- | --- | --- |
| DashScope | qwen-plus, qwen-turbo, qwen-max | `provider: "dashscope"` |
| OpenAI | gpt-4o, gpt-4.1 | `provider: "openai"` |
| DeepSeek | deepseek-chat | `provider: "openai"` + `base_url` |
| GLM | glm-4 | `provider: "openai"` + `base_url` |
| Moonshot | moonshot-v1-8k | `provider: "openai"` + `base_url` |
| Ollama | qwen2:7b, llama3 | `provider: "openai"` + 本地 `base_url` |

使用示例：

```python
from llm import ChatMessage, LLMFactory

llm = LLMFactory.create(
    provider="openai",
    model="deepseek-chat",
    api_key="sk-xxx",
    base_url="https://api.deepseek.com/v1",
)

response = await llm.generate([
    ChatMessage(role="user", content="你好")
])
print(response.content)
```

运行时切换模型：

```python
agent.llm = LLMFactory.create(
    provider="openai",
    model="deepseek-chat",
    api_key="sk-xxx",
    base_url="https://api.deepseek.com/v1",
)
```

---

## Agent 层

### ChatAgent

适合客服、问答、咨询、知识库助手等纯对话场景。

流程：

```text
system prompt -> RAG 可选 -> memory history -> user input -> LLM -> save memory -> response
```

示例：

```python
from agents import ChatAgent
from memory import LocalMemory

agent = ChatAgent(
    llm=llm,
    memory=LocalMemory(max_history=20),
    retriever=retriever,
    system_prompt="你是一个中文 AI 助手。",
)

response = await agent.chat("介绍一下这个项目", session_id="user_1")
print(response.content)
print(response.metadata["trace_id"])
```

### TaskAgent

适合需要工具调用的任务，例如搜索、代码执行、文件读取、业务 API 调用。

流程：

```text
user input
  -> LLM with tool schemas
  -> tool_calls?
      -> execute tools
      -> append tool results
      -> LLM again
  -> final answer
```

示例：

```python
from agents import TaskAgent
from tools import ToolRegistry
from tools.builtin import CodeExecutorTool, FileReaderTool

registry = ToolRegistry()
registry.register(CodeExecutorTool())
registry.register(FileReaderTool())

agent = TaskAgent(
    llm=llm,
    memory=memory,
    tool_registry=registry,
    system_prompt="你是一个可以使用工具的助手。",
    max_tool_rounds=5,
)

response = await agent.chat("帮我计算第 20 个斐波那契数", session_id="task_1")
print(response.content)
print(response.tool_calls)
```

---

## Tools 工具系统

工具需要继承 `Tool`，并声明 Function Calling schema。

```python
from tools import Tool, ToolRegistry, ToolResult


class WeatherTool(Tool):
    name = "get_weather"
    description = "查询城市天气"
    parameters = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称"},
        },
        "required": ["city"],
    }

    async def execute(self, city: str) -> ToolResult:
        return ToolResult(
            success=True,
            output={"city": city, "weather": "sunny"},
        )


registry = ToolRegistry()
registry.register(WeatherTool())
```

`ToolRegistry` 提供：

- `register(tool)`：注册工具实例或工具类
- `get_function_schemas()`：生成模型可识别的工具 schema
- `call(name, **kwargs)`：按名称执行工具
- `call_from_llm_response(tool_call)`：从模型返回的 tool_call 执行工具

内置工具：

- `WebSearchTool`：搜索工具示例桩，实际项目需接 SerpAPI、Bing Search 等真实搜索 API
- `FileReaderTool`：读取本地文本文件
- `CodeExecutorTool`：执行 Python 代码片段

注意：`CodeExecutorTool` 当前是模板级示例实现，生产环境应替换为 Docker、微服务沙箱或其他隔离执行环境。

---

## Skills 技能系统

Skill 是“代码直接调用的 LLM 能力单元”，区别于由模型自动触发的 Tool。

内置 Skill：

- `JsonExtractorSkill`：从文本中提取 JSON，支持 Markdown 代码块、前后缀文本、尾随逗号
- `DocumentParserSkill`：从非结构化文本提取结构化字段
- `ReportGeneratorSkill`：基于模板生成报告

使用示例：

```python
from skills import JsonExtractorSkill

skill = JsonExtractorSkill(llm)
data = await skill.run('用户说: {"name": "张三", "age": 25}')
print(data)
```

自定义 Skill：

```python
from skills import Skill


class SummarySkill(Skill):
    name = "summary"
    description = "文本摘要"
    system_prompt = "你是一个摘要专家。"

    async def run(self, text: str) -> str:
        response = await self.call_llm(f"请总结以下内容：\n{text}")
        return response.content
```

---

## RAG 检索增强

当前 RAG 层包括：

- `EmbeddingModel`：按 provider 创建 `DashScopeEmbedding` 或 `OpenAIEmbedding`
- `QdrantVectorStore`：Qdrant 向量存储
- `Retriever`：标准向量召回封装
- `KeywordRetriever`：轻量关键词召回，不依赖外部服务
- `RetrieverRoute`：描述一条检索路线
- `MultiRouteRetriever`：多路召回 + RRF 融合排序

### 基础向量检索

```python
from rag import EmbeddingModel, QdrantVectorStore, Retriever

embedding = EmbeddingModel(
    provider="dashscope",
    model="text-embedding-v3",
    api_key="sk-xxx",
)

vector_store = QdrantVectorStore(
    collection_name="docs",
    embedding=embedding,
    host="localhost",
    port=6333,
    dimension=1024,
)

await vector_store.add_texts(
    texts=["FastAPI 是一个现代化的 Python Web 框架。"],
    metadatas=[{"source": "fastapi_docs"}],
)

retriever = Retriever(vector_store=vector_store, top_k=5)
docs = await retriever.retrieve("FastAPI 是什么？")
context = Retriever.format_context(docs)
```

### 多路召回

多路召回适合处理：

- 向量检索漏掉专有名词、编号、短关键词
- 同时查询多个知识源或多个 collection
- 把向量召回、关键词召回、业务过滤召回融合排序

```python
from rag import KeywordRetriever, MultiRouteRetriever, RetrieverRoute

vector_retriever = Retriever(vector_store=vector_store, top_k=5)
keyword_retriever = KeywordRetriever(
    documents=[
        {
            "content": "FastAPI 基于 Starlette 和 Pydantic。",
            "metadata": {"source": "fastapi_docs"},
        }
    ],
    top_k=5,
)

retriever = MultiRouteRetriever(
    routes=[
        RetrieverRoute("vector", vector_retriever, weight=1.0),
        RetrieverRoute("keyword", keyword_retriever, weight=0.6),
    ],
    top_k=5,
)

docs = await retriever.retrieve("FastAPI Pydantic")
```

`MultiRouteRetriever` 会并发执行每条路线，然后用 Reciprocal Rank Fusion 合并排序。融合后的文档 metadata 中会包含 `retrieval_routes` 和 `raw_scores`，Trace 中也会记录这些信息。

### 运行 RAG 示例

```bash
python -m examples.rag_example
```

该示例需要本地或远程 Qdrant 可用，并配置 Embedding API Key。

---

## Memory 对话记忆

### LocalMemory

适合开发调试和单进程小应用，进程重启后记忆丢失。

```python
from memory import LocalMemory

memory = LocalMemory(max_history=20)
```

### RedisMemory

适合多进程、多实例和需要 TTL 的生产场景。

```python
from memory import RedisMemory

memory = RedisMemory(
    redis_url="redis://localhost:6379/0",
    max_history=20,
    ttl_seconds=86400,
    redis_prefix="agent:memory:",
)
```

---

## LangGraph 编排示例

示例文件：

```text
examples/langgraph_orchestration_example.py
```

图流程：

```text
START
  -> retrieve
  -> plan
  -> 条件判断 need_tool
      -> tool
      -> answer
  -> END
```

节点说明：

- `retrieve`：使用 RAG 检索上下文
- `plan`：让 LLM 输出 JSON，判断是否需要工具
- `tool`：通过 `ToolRegistry` 执行工具
- `answer`：结合检索上下文和工具结果生成最终回答

运行：

```bash
python -m examples.langgraph_orchestration_example
```

这个示例使用 `StateGraph` 显式描述节点和边，适合扩展为审批流、任务拆解流、多 Agent 协作流、带人工确认的流程等。

---

## 执行过程复盘 Trace

模板默认开启轻量 Trace，写入：

```text
traces/agent_runs.jsonl
```

每次 `ChatAgent`、`TaskAgent`、LangGraph 示例运行都会生成 `trace_id`，并记录：

- `run_start`：请求入口、session、用户输入
- `rag_retrieve`：召回文档、分数、metadata、多路召回路线信息
- `memory_load` / `memory_save`：会话记忆读取和写入
- `tools_available`：当前可用工具 schema
- `llm_request`：模型、messages、tools schema
- `llm_response`：模型输出、tool_calls、finish_reason、token 用量
- `tool_request` / `tool_response`：工具名、参数、结果、错误、耗时
- `tool_round_limit_reached`：工具轮次达到上限
- `run_error`：异常类型和错误信息
- `run_end`：最终响应、总耗时

FastAPI `/chat` 会返回 `trace_id`：

```json
{
  "reply": "...",
  "session_id": "test",
  "trace_id": "..."
}
```

常用环境变量：

```bash
# 开关，默认 true
AGENT_TRACE_ENABLED=true

# Trace 文件路径
AGENT_TRACE_FILE=traces/agent_runs.jsonl

# 单个字段最大记录长度，默认 12000
AGENT_TRACE_MAX_CHARS=12000
```

查看最近一条 Trace：

```bash
tail -n 1 traces/agent_runs.jsonl
```

注意：Trace 会记录完整 prompt、工具参数和工具输出。生产环境包含敏感数据时，建议在业务层脱敏，或关闭完整 Trace。

---

## FastAPI 接口

### `GET /health`

返回服务状态：

```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

### `POST /chat`

请求：

```json
{
  "message": "你好",
  "session_id": "test"
}
```

响应：

```json
{
  "reply": "...",
  "session_id": "test",
  "trace_id": "..."
}
```

### `POST /switch_model`

运行时切换当前全局 Agent 使用的模型。

参数：

- `provider`
- `model`
- `api_key`
- `base_url`

---

## 示例命令

```bash
# 基础对话
python -m examples.chat_example

# 工具调用
python -m examples.tool_agent_example

# 模型切换
python -m examples.model_switch_example

# RAG 多路召回
python -m examples.rag_example

# 多 Agent 协作
python -m examples.multi_agent_example

# LangGraph 编排
python -m examples.langgraph_orchestration_example
```

---

## 扩展指南

### 添加新的 LLM Provider

1. 在 `llm/` 下新增实现文件，继承 `LLMBase`
2. 实现 `generate()` 和 `stream()`
3. 通过 `register_provider(name, cls)` 注册

### 添加新的 Tool

1. 继承 `Tool`
2. 定义 `name`、`description`、`parameters`
3. 实现 `execute()`
4. 注册到 `ToolRegistry`

### 添加新的 Skill

1. 继承 `Skill`
2. 实现 `run()`
3. 通过 `self.call_llm(...)` 复用模型调用逻辑

### 添加新的 Memory 后端

1. 继承 `MemoryBase`
2. 实现 `add()`、`get_history()`、`clear()`
3. 在业务初始化时注入到 Agent

### 添加新的 RAG 路线

1. 实现一个带 `retrieve(query, top_k, filter)` 方法的检索器
2. 用 `RetrieverRoute(name, retriever, weight)` 包装
3. 加入 `MultiRouteRetriever(routes=[...])`

---

## 生产化注意事项

- `WebSearchTool` 当前是示例桩，需要替换为真实搜索 API。
- `CodeExecutorTool` 当前直接执行 Python 代码，生产环境必须使用隔离沙箱。
- Trace 会写入完整 prompt 和工具输出，涉及隐私或密钥时需要脱敏。
- LocalMemory 不适合多实例部署，生产建议使用 RedisMemory。
- RAG 生产环境需要为文档分块、去重、增量更新、召回评估和 rerank 继续补齐。
- `/switch_model` 会接收明文 `api_key`，生产环境建议改为受控配置或密钥管理。
