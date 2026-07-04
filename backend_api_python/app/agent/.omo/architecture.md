# 多轮 ReAct 循环架构设计

## 1. 背景与动机

### 1.1 当前问题

现有 `TaskAgent` = `plan(一次性工具筛选)` → `CodeAgent(max_steps=10)` → `返回结果`。

当任务复杂（如：趋势分析 + 新闻搜索 + 板块分析 + 综合研判），10 步很容易耗尽。超步后 smolagents 直接截断，返回不完整结果，没有重试或接力机制。

### 1.2 核心解决思路

**多轮 plan→ReAct 循环**：ReAct 超步后不截断结束，而是返回给 plan 做下一轮调度。每轮 ReAct 共享一个扁平的内存缓存，通过 `read_cache` 工具按需读取前轮结果。

### 1.3 关键设计原则

- **框架不替 agent 做决策**——不自动拼凑上下文，不自动摘要，让 agent 自己决定读什么数据
- **扁平缓存**——没有轮次、层级概念，只有 key-value
- **就近释放**——cache 生命周期 = 单次用户请求，用完即释

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────┐
│ TaskAgent.chat()                                        │
│                                                         │
│  cache = {}   ← 用户消息级缓存                           │
│                                                         │
│  ┌─ while not done ─────────────────────────────────┐   │
│  │                                                   │   │
│  │  Plan:  根据意图/上轮结果选工具 + 分配步数         │   │
│  │                                                   │   │
│  │  ReAct: 执行工具 → 自动写 cache                   │   │
│  │         通过 read_cache 读前轮结果                  │   │
│  │         通过上下文中的 cache index 知道有什么数据   │   │
│  │                                                   │   │
│  │  超步? ──→ 记录本轮摘要 → 继续循环                │   │
│  │  完成? ──→ 返回最终结果                            │   │
│  │                                                   │   │
│  └───────────────────────────────────────────────────┘   │
│                                                         │
│  return response  → cache 释放                          │
└─────────────────────────────────────────────────────────┘
```

### 2.1 关键变更：提取 ReAct 核心

smolagents CodeAgent 的 ReAct 循环无法满足需求，原因：

| 问题 | smolagents 行为 | 需要的 |
|---|---|---|
| 超步后返回 | 裸 string，可能是中间推理碎屑 | 结构化 cache + 可见的已完成/未完成 |
| 状态暴露 | memory 是内部对象，外部难访问 | Plan 需要知道当前执行状态 |
| Python 沙箱 | 强依赖 `additional_authorized_imports` | 不需要沙箱，直接调工具 |
| 重试策略 | 内部自动重试，消耗步数 | 让 plan 决定重试/换工具/跳过 |

**决策**：将 ReAct 核心循环从 smolagents 中提取出来， embedding 到项目内，完全控制循环粒度。

---

## 3. ReAct 核心模块

### 3.1 位置

```
tools/
  react/
    __init__.py          # 导出 ReactEngine
    engine.py            # ReAct 循环引擎
    tool_adapter.py      # 工具包装（将 Tool → ReAct 可用格式）
    cache.py             # 内存缓存
    summary.py           # 步骤摘要（可选）
```

### 3.2 接口

```python
class ReactEngine:
    """ReAct 循环引擎，替代 smolagents CodeAgent"""

    def __init__(
        self,
        llm: LLMBase,                    # 模型实例
        tools: list[Tool],               # 本轮可用工具
        cache: RoundCache,               # 全局缓存引用
        system_prompt: str,              # 系统提示词
        max_steps: int = 10,             # 本轮最大步数
    )

    async def run(
        self,
        task: str,                       # 本轮任务描述
        cache_index: dict[str, str],     # 前轮已有的缓存索引 {key: 简述}
    ) -> ReactResult:
        ...


@dataclass
class ReactResult:
    success: bool                        # True=正常完成 / False=超步中断
    output: str                          # LLM 最终输出（如果有）
    steps_taken: int                     # 实际执行步数
    error: str | None                    # 错误信息
    summary: str | None                  # 本轮执行摘要（超步时用）
    cache_keys_written: list[str]        # 本轮新写入的 cache key 列表
```

### 3.3 ReAct 循环伪代码

```python
async def run(self, task, cache_index) -> ReactResult:
    messages = self._build_messages(task, cache_index)

    for step in range(self.max_steps):
        response = await self.llm.generate(messages, tools=self.tool_schemas)

        if response.finish_reason == "stop":
            # LLM 给出了最终回答
            return ReactResult(success=True, output=response.content, steps_taken=step+1)

        if not response.tool_calls:
            continue  # LLM 没决定调工具，继续

        for tool_call in response.tool_calls:
            result = await self._execute_tool(tool_call)
            messages.append(self._tool_result_message(tool_call, result))
            # 工具结果已通过 registry 自动写入 cache

    # max_steps 耗尽
    summary = await self._summarize(messages)  # 可选：LLM 生成摘要
    return ReactResult(
        success=False,
        steps_taken=self.max_steps,
        summary=summary,
        cache_keys_written=list(self.cache.keys()),  # ← 本轮写的 key
    )
```

---

## 4. Cache 系统

### 4.1 数据结构

```python
# cache.py

class RoundCache:
    """扁平缓存，生命周期=单次 TaskAgent.chat()"""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._round_keys: set[str] = set()       # 本轮新增的 key

    def put(self, key: str, value: Any):
        self._data[key] = value
        self._round_keys.add(key)

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def index(self) -> dict[str, str]:
        """返回 {key: type_hint} 供注入到 ReAct 上下文"""
        return {
            k: self._infer_type(v)
            for k, v in self._data.items()
        }

    def flush_round_keys(self) -> list[str]:
        """取出本轮写入了哪些 key（给 ReactResult）"""
        keys = list(self._round_keys)
        self._round_keys.clear()
        return keys

    def clear(self):
        self._data.clear()
        self._round_keys.clear()
```

### 4.2 自动写入：registry 层

```python
# registry.py — ToolRegistry.call()

def __init__(self):
    ...
    self._cache: RoundCache | None = None

def bind_cache(self, cache: RoundCache):
    self._cache = cache

async def call(self, name, **kwargs):
    result = await tool.safe_execute(**kwargs)
    if self._cache and result.success:
        key = self._make_cache_key(name, kwargs)
        self._cache.put(key, result.output)
    return result

def _make_cache_key(self, name, kwargs) -> str:
    """生成扁平 cache key：{tool_name}_{主要参数值}"""
    # 取 codes / stock_code / symbol 等标识性参数
    primary = kwargs.get("codes") or kwargs.get("stock_code") or kwargs.get("symbol") or "default"
    return f"{name}_{primary}"
```

### 4.3 read_cache 工具

```python
# tools/builtin/cache_tools.py

def read_cache(key: str) -> Any:
    """读取之前工具执行的结果。
    
    Args:
        key: 缓存键名，格式 '{tool_name}_{codes}'，如 'analyze_trend_000001'
    
    返回之前工具的输出结果，或错误信息。
    可用缓存键可通过 CACHE_INDEX 查看。
    """
    cache = _get_current_cache()
    result = cache.get(key)
    if result is None:
        return {"error": f"缓存中不存在 key '{key}'", "available_keys": cache.keys()}
    return result


def list_cache() -> dict[str, str]:
    """列出当前可用的所有缓存条目，返回 {key: value_type} 字典。"""
    cache = _get_current_cache()
    return cache.index()
```

### 4.4 Cache Index 注入

每轮 ReAct 启动时，将当前 cache index 注入到 task prompt 中：

```
【缓存中的已有数据】
- analyze_trend_000001: dict (趋势评分+指标数据)
- kdj_000001: dict (KDJ金叉/死叉+数值)

使用 read_cache(key) 读取上述数据，无需重复调用工具。
```

---

## 5. TaskAgent 改造

### 5.1 新流程

```python
async def chat(self, user_input, session_id, use_rag=True):
    cache = RoundCache()
    self.tool_registry.bind_cache(cache)

    # 初始 plan
    selected_tools, expanded_query = await self._plan(user_input, self.llm, trace)
    if not selected_tools:
        return await super().chat(user_input, ...)

    round_num = 0
    max_rounds = 3          # 全局最大循环轮次
    max_total_steps = 30    # 全局最大总步数
    total_steps = 0
    all_summaries = []

    while round_num < max_rounds and total_steps < max_total_steps:
        round_num += 1

        # 动态分配本轮步数
        step_budget = min(10, max_total_steps - total_steps)

        # 构造 task prompt（含 cache index）
        task = self._build_task_prompt(
            expanded_query,
            round_num,
            cache.index(),
            all_summaries,
        )

        # 运行 ReAct
        engine = ReactEngine(
            llm=self.llm,
            tools=_build_react_tools(selected_tools, cache),
            cache=cache,
            max_steps=step_budget,
        )
        result = await engine.run(task, cache.index())

        total_steps += result.steps_taken
        trace.record("react_round", {
            "round": round_num,
            "steps": result.steps_taken,
            "success": result.success,
            "cache_keys": result.cache_keys_written,
        })

        if result.success:
            # 正常完成
            return AgentResponse(content=result.output, ...)

        # 超步 → plan 重新调度
        if round_num >= max_rounds or total_steps >= max_total_steps:
            break

        # 重新 plan（带上本轮摘要）
        selected_tools, expanded_query = await self._plan(
            f"{user_input}\n[前轮摘要: {result.summary}]",
            self.llm, trace,
        )
        all_summaries.append(result.summary)

    # 所有轮次耗尽，返回最后结果或错误提示
    return AgentResponse(
        content=f"任务未完全完成（已达全局步数上限）\n{result.output or ''}",
        ...
    )
```

### 5.2 Plan 多轮适配

当前 `_plan()` 的 prompt 只是静态工具列表。多轮时需要加上：

```
【上一轮执行情况】
已完成: analyze_trend(000001) ✓, kdj_calc(000001) ✓
失败: news_search(000001) → 服务超时
未尝试: sector_flow(新能源)
【当前可用缓存】
- analyze_trend_000001
- kdj_000001
```

---

## 6. 与 smolagents 解耦

### 6.1 替换清单

| 当前依赖 | 替代 | 原因 |
|---|---|---|
| `SmolCodeAgent` | `ReactEngine` | 控制超步行为、暴露中间状态 |
| `_SmolTool` | `ToolAdapter` | 简化包装层，去掉 smolagents 的 inputs/output_type 约束 |
| `_LLMAdapter` | 直接调 `LLMBase.generate()` | 去掉 smolagents 的 ChatMessage 转换层 |
| `write_memory_to_messages` | `RoundCache + summary` | 扁平缓存替代分层 memory |

### 6.2 逐步替换策略

**Phase 1（当前）**：保留 smolagents，在外部套多轮循环
- 修改 `TaskAgent.chat()` 加 while 循环
- 在 CodeAgent 外套 `RoundCache`
- 通过 ToolRegistry 注入自动缓存
- 按 `agent.write_memory_to_messages()` 提取步骤上下文传给下一轮

**Phase 2**：提取 ReAct 核心
- 新建 `tools/react/engine.py`
- 实现 `ReactEngine`，只依赖 `LLMBase.generate()` + `Tool`
- 逐步替换 smolagents CodeAgent 的调用点
- 验证全部现有工具函数兼容

**Phase 3**：清理
- 移除 `_SmolTool`、`_LLMAdapter` 等适配层
- 从依赖中移除或可选化 `smolagents`

---

## 7. 边界条件与错误处理

### 7.1 工具错误 vs ReAct 超步

两种中断需要区分处理：

```
工具执行失败:
  → registry 返回 ToolResult(success=False)
  → ReactEngine 继续循环（LLM 可以看到错误信息）
  → LLM 自行决定重试/换工具/跳过

ReAct 超步:
  → ReactEngine 达到 max_steps
  → 触发 plan 重新调度
  → plan 看到哪些工具失败了，可以换工具或跳过
```

### 7.2 全局终止保障

```python
MAX_ROUNDS = 3          # 最多 3 轮循环
MAX_TOTAL_STEPS = 30    # 最多 30 步
MAX_TOKENS_PER_ROUND = 80000  # 每轮上下文上限（防 prompt 爆炸）
```

任意一个达到 → 立即停止，返回当前已完成的 cache 内容 + 超限提示。

### 7.3 幂等性与缓存误用

`read_cache` 按 key 精确读取，不存在跨轮污染。如果前轮写了 `trend_000001`，后轮读到的就是同样的数据。如需刷新数据，调原始工具本身即可（工具调用的结果会覆盖 cache）。

---

## 8. 实现步骤（建议顺序）

### Step 1: RoundCache + 自动写缓存
- 实现 `RoundCache` 类
- registry.py `call()` 加缓存写入
- 注册 `read_cache` / `list_cache` 工具
- 验证：单轮 CodeAgent 内能读写 cache

### Step 2: TaskAgent 多轮循环（不拆 smolagents）
- `TaskAgent.chat()` 加 while 循环
- 超步后解析 `agent.memory` 获取摘要
- 构造下一轮 task prompt + cache index
- 验证：2 轮循环能接力完成

### Step 3: 提取 ReactEngine
- 新建 `tools/react/engine.py`
- 实现纯 ReAct 循环替代 CodeAgent
- 移除外层 smolagents 依赖
- 验证：全部现有工具在新引擎下行为一致

### Step 4: 清理与收尾
- 移除 `_SmolTool`, `_LLMAdapter`
- 从 `requirements.txt` 去掉 smolagents（如不再需要）

---

## 9. 附录：关键文件变更清单

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `tools/registry.py` | 修改 | `call()` 加自动缓存写入、`_make_cache_key` |
| `tools/react/cache.py` | 新建 | `RoundCache` 类 |
| `tools/react/engine.py` | 新建 | `ReactEngine` ReAct 循环 |
| `tools/react/tool_adapter.py` | 新建 | 工具格式适配 |
| `tools/builtin/__init__.py` | 修改 | 注册 `read_cache`, `list_cache` |
| `agents/task_agent.py` | 重构 | 多轮循环，替换 CodeAgent → ReactEngine |
| `prompts/plan_system.txt` | 修改 | 加入上轮摘要 + cache index 上下文 |
| `requirements.txt` | 修改 | 可移除 smolagents |
