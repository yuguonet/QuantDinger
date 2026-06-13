# Nanobot 迁移设计方案 v2

> 日期: 2026-06-13
> 状态: 代码完成，待集成测试
> 原则: 完全剥离 smolagents，零残留 import

## 一、架构总览

```
【迁移前】
Flask Route → _AgentExecutor.chat() → smolagents CodeAgent.run() (同步)
                                       ├── OpenAIModel (同步 HTTP)
                                       ├── Tool.forward() (同步)
                                       └── final_answer() → JSON

【迁移后】
Flask Route → NanobotAgent.chat() → asyncio.run_coroutine_threadsafe()
                                          ↓
                                    Nanobot AgentLoop.process_direct() (async)
                                       ├── AsyncOpenAI (async HTTP)
                                       ├── Tool.execute() (async, run_in_executor)
                                       └── 自然语言输出 → DecisionCard 格式化
```

## 二、文件清单（5 个活跃文件，0 个 smolagents import）

| 文件 | 行数 | 职责 | 状态 |
|------|------|------|------|
| `app/nanobot_agent.py` | ~490 | 核心桥接层：单例、事件循环、同步API、追责Hook、DecisionCard | ✅ |
| `app/nanobot_config_gen.py` | ~220 | .env → ~/.nanobot/config.json 自动生成 | ✅ |
| `app/agent/nanobot_tools.py` | ~470 | 93个工具适配器 + call_skill + LLM provider 缓存 | ✅ |
| `app/agent/nanobot_skills.py` | ~190 | domain_registry → 3个 SKILL.md 目录 | ✅ |
| `app/routes/agent_blueprint.py` | ~440 | Flask 路由（已切换到 nanobot_agent） | ✅ |

## 三、核心组件设计

### 3.1 桥接层 (`nanobot_agent.py`)

```
NanobotAgent (单例)
├── __init__()
│   ├── ensure_nanobot_config()        # 生成配置
│   ├── asyncio.new_event_loop()       # 持久事件循环
│   ├── threading.Thread(run_forever)  # 守护线程
│   ├── Nanobot.from_config()          # 初始化 Nanobot
│   ├── register_quantdinger_tools()   # 注入 93 个工具
│   ├── register_call_skill_tool()     # 注入 call_skill
│   └── ensure_nanobot_skills()        # 生成 SKILL.md
│
├── chat(message, session_id, context) → NanobotResult
│   ├── _enrich_message()              # 拼接上下文
│   ├── _detect_domain()               # 关键词领域检测
│   ├── TraceCollectorHook.setup()     # 追责初始化
│   ├── _current_hook.set(hook)        # contextvars 传递给工具层
│   ├── run_coroutine_threadsafe()     # 异步执行
│   ├── _maybe_format_decision_card()  # JSON → 标准卡片
│   └── hook.on_agent_finish()         # EvalNode 存库
│
├── chat_stream() → Generator[SSE事件]
│   └── 同上，但流式输出
│
└── close()                            # 关闭事件循环
```

**关键设计决策**:

| 决策 | 选择 | 理由 |
|------|------|------|
| 事件循环 | 持久 `new_event_loop` + `run_forever` | 避免每次调用创建/销毁开销 |
| 线程模型 | 单守护线程 | Flask 多线程通过 `run_coroutine_threadsafe` 安全提交 |
| 单例模式 | `__new__` + `_initialized` | 进程内唯一 AgentLoop |
| session 映射 | `f"qd:{session_id}"` | 隔离 QuantDinger 会话与 Nanobot 内部会话 |

### 3.2 工具适配器 (`nanobot_tools.py`)

```
register_quantdinger_tools(nanobot_registry)
├── qd_registry.discover()              # 触发 93 个 @tool 注册
├── for name, spec in qd_registry._tools:
│   ├── _build_json_schema(spec)        # 从函数签名推断 JSON Schema
│   └── QuantDingerToolAdapter(...)     # 包装为 Nanobot Tool
│       ├── name / description / parameters  # 元数据
│       ├── read_only                   # 数据工具可并发
│       └── execute(**kwargs)           # async, run_in_executor
│           ├── self._fn(**kwargs)      # 调用原始同步函数
│           ├── hook.on_tool_call()     # 通知追责体系
│           └── 截断过长结果
│
└── register_call_skill_tool()
    └── CallSkillToolAdapter
        ├── _tool_fn_cache              # 工具函数缓存
        ├── _cached_provider            # LLM provider 缓存
        └── _call_skill_sync()
            ├── skill_registry.get()    # 获取 BaseSkill
            ├── call_llm(prompt)        # 复用缓存的 provider
            ├── call_tool_fn(name, **kw) # 直接调用原始函数
            ├── sk.run()                # 执行 Skill
            └── chain_store.save_tree() # EvalNode 持久化
```

**smolagents 剥离要点**:

| 旧调用 | 新调用 | 说明 |
|--------|--------|------|
| `smolagents.Tool.forward()` | `nanobot.Tool.execute()` | async 接口 |
| `build_all_tools()` → smolagents Tool 列表 | `qd_registry._tools[name].fn()` | 直接调用原始函数 |
| `smolagents.OpenAIModel()` | `nanobot provider.chat_with_retry()` | async HTTP |
| `smolagents.BaseTool.register(TracedTool)` | `QuantDingerToolAdapter` | 不需要类型伪装 |

### 3.3 配置生成 (`nanobot_config_gen.py`)

```
.env 文件
├── AGENT_LLM_PROVIDER → provider 名
├── AGENT_LLM_MODEL → model 名
├── *_API_KEY → providers.*.apiKey
├── *_API_BASE → providers.*.apiBase
├── AGENT_MAX_STEPS → maxToolIterations
└── OLLAMA_* → providers.ollama

          ↓ generate_nanobot_config()

~/.nanobot/config.json
{
  "agents": { "defaults": { model, provider, workspace, ... } },
  "providers": { openrouter/openai/deepseek/...: { apiKey, apiBase } },
  "tools": { restrictToWorkspace: false }
}
```

**自动检测优先级**: AGENT_LLM_PROVIDER > 有 API_KEY 的第一个 provider > OLLAMA > fallback openrouter

### 3.4 Skill 目录 (`nanobot_skills.py`)

```
~/.nanobot/workspace/skills/
├── finance/SKILL.md    # 金融分析指令（call_skill 流程、输出格式、追责说明）
├── trading/SKILL.md    # 交易执行指令（安全确认、展示规范）
└── coding/SKILL.md     # 代码开发指令（读→改→验工作流）
```

Nanobot 的 `SkillsLoader` 自动发现并注入到 system prompt。

### 3.5 追责体系（TraceCollector 集成）

```
请求进入
  ↓
TraceCollectorHook 创建
  ├── setup_collector(domain, stock_code)  # 仅 finance 域
  └── _current_hook.set(hook)              # contextvars 注入
  ↓
工具执行 (QuantDingerToolAdapter.execute)
  ├── self._fn(**kwargs)                   # 调用原始工具
  └── _current_hook.get() → hook.on_tool_call()  # 自动记录
  ↓
Agent 完成
  └── hook.on_agent_finish()               # 构建 EvalNode 树 + 存库
  ↓
盘后定时任务 (不变)
  ├── evaluate_pending()                   # 按 timeframe 取行情验证
  ├── update_skill_weights()               # 单位时间收益率迭代
  └── update_factor_weights()              # 因子权重 + 清理过期
```

## 四、数据流完整路径

### 4.1 同步聊天

```
POST /api/agent/chat
  → agent_blueprint.agent_chat()
    → get_nanobot_agent()                    # 单例，首次初始化
    → agent.chat(message, session_id, context)
      → _enrich_message(message, context)    # 拼接 stock_code/行情
      → _detect_domain(message, context)     # 关键词 → finance/coding/trading/chat
      → TraceCollectorHook()                 # 追责初始化
      → run_coroutine_threadsafe(            # 提交到事件循环
          loop.process_direct(enriched, session_key)
        )
        ↓ (事件循环线程)
        Nanobot AgentLoop
          → ContextBuilder.build_messages()  # system prompt + history + skills
          → AgentRunner.run()
            → provider.chat_with_retry()     # AsyncOpenAI
            → response.tool_calls
            → ToolRegistry.execute()
              → QuantDingerToolAdapter.execute()
                → run_in_executor(fn(**kwargs))  # 线程池执行同步工具
                → hook.on_tool_call()            # 追责记录
            → (循环直到 final response)
          → SessionManager.save()
        ↓ (返回)
      → _maybe_format_decision_card()        # JSON → 标准卡片
      → hook.on_agent_finish()               # EvalNode 存库
      → NanobotResult(success, content, ...)
    → jsonify(result)
```

### 4.2 流式聊天

```
POST /api/agent/chat/stream
  → agent_blueprint.agent_chat_stream()
    → threading.Thread(_run)                 # 后台线程
      → agent.chat_stream()                  # Generator
        → run_coroutine_threadsafe(
            loop.process_direct(..., on_stream=callback)
          )
          ↓ (事件循环线程)
          on_stream(delta) → event_queue.put({type: "generating", message: delta})
          完成 → event_queue.put({type: "done", content: ...})
        ↓ (Flask 线程)
      → SSE: while event_queue: yield f"data: {json.dumps(ev)}\n\n"
```

## 五、与旧架构的兼容性

| 组件 | 旧依赖 | 新实现 | 兼容性 |
|------|--------|--------|--------|
| 工具执行 | `smolagents.Tool.forward()` | `nanobot.Tool.execute()` + `run_in_executor` | ✅ 同步函数不变 |
| LLM 调用 | `smolagents.OpenAIModel` (同步) | `nanobot provider.chat_with_retry()` (async) | ✅ 结果等价 |
| 工具注册 | `@tool` → `ToolSpec` → `smolagents.Tool` | `@tool` → `ToolSpec` → `QuantDingerToolAdapter` | ✅ @tool 不变 |
| Skill 调用 | `call_skill` → `BaseSkill.run()` | 同（`CallSkillToolAdapter` 包装） | ✅ BaseSkill 不变 |
| 追责体系 | `TracedTool` + `TraceCollector` | `QuantDingerToolAdapter.execute()` + `contextvars` | ✅ 数据结构不变 |
| 盘后回溯 | `chain/evaluator.py` | 不变 | ✅ 零改动 |
| 会话管理 | `SessionStore` (自建) | `Nanobot.SessionManager` (内置) | ⚠️ 冷启动，历史不迁移 |
| 输出格式 | `final_answer()` 强制 JSON | 自然语言 → `_maybe_format_decision_card()` | ⚠️ 依赖 LLM 输出 JSON |

## 六、已知风险与缓解

| 风险 | 严重度 | 缓解 |
|------|--------|------|
| LLM 不输出 JSON → DecisionCard 失效 | 中 | `_maybe_format_decision_card` 降级为原始文本返回 |
| Nanobot session_key 格式不兼容 | 低 | 统一 `f"qd:{session_id}"` 前缀 |
| `run_in_executor` 线程池耗尽 | 低 | 默认 ThreadPoolExecutor，可配置 `max_workers` |
| `call_llm` 在 executor 线程创建新 event loop | 低 | provider 已缓存，loop 创建开销 ~0.1ms |
| Nanobot 内置工具（shell/web/fs）被注册 | 低 | Nanobot ToolLoader 按 config 控制，QuantDinger 工具覆盖同名 |

## 七、旧文件处置

| 文件 | 处置 | 理由 |
|------|------|------|
| `agent/agent.py` | 🔒 保留只读 | format_decision_card 等函数参考 |
| `agent/model.py` | 🔒 保留只读 | OpenAIModel 适配器，不再调用 |
| `agent/tool_adapter.py` | 🔒 保留只读 | smolagents Tool 构建，不再调用 |
| `agent/traced_tool.py` | 🔒 保留只读 | TracedTool 包装，不再调用 |
| `agent/skills/call_skill_tool.py` | 🔒 保留只读 | 旧 CallSkillTool，被 nanobot_tools 替代 |
| `agent/tools/registry.py` | ✅ 保留活跃 | `@tool` 注册 + `discover()`，被 nanobot_tools 调用 |
| `agent/skills/registry.py` | ✅ 保留活跃 | `@skill` 注册 + `discover()`，被 call_skill 调用 |
| `agent/skills/base.py` | ✅ 保留活跃 | `BaseSkill.run()`，被 call_skill 调用 |
| `agent/chain/*` | ✅ 保留活跃 | EvalNode + store + evaluator，追责体系核心 |
| `agent/trace_collector.py` | ✅ 保留活跃 | TraceCollector，被 hook 调用 |
| `agent/session_store.py` | ✅ 保留活跃 | 会话历史（Flask 路由仍用） |

## 八、实施检查清单

- [x] `nanobot_config_gen.py` — .env → config.json
- [x] `nanobot_tools.py` — 93 个工具适配器
- [x] `nanobot_tools.py` — call_skill 适配器（无 smolagents 依赖）
- [x] `nanobot_tools.py` — LLM provider 缓存
- [x] `nanobot_tools.py` — 工具执行 → hook.on_tool_call() 通知
- [x] `nanobot_skills.py` — 3 个 SKILL.md 目录
- [x] `nanobot_agent.py` — 单例 + 事件循环 + 线程
- [x] `nanobot_agent.py` — chat() 同步 API
- [x] `nanobot_agent.py` — chat_stream() 流式 API
- [x] `nanobot_agent.py` — TraceCollectorHook + contextvars
- [x] `nanobot_agent.py` — DecisionCard 格式化（无 smolagents 依赖）
- [x] `nanobot_agent.py` — close() 清理
- [x] `agent_blueprint.py` — Flask 路由切换
- [x] `agent_blueprint.py` — 移除 visualize/save 依赖 smolagents 的路由
- [x] 全部 5 文件语法检查通过
- [x] 活跃路径 0 个 smolagents import
- [ ] 端到端集成测试（待部署环境）
