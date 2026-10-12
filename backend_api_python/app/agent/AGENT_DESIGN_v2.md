# QuantDinger Agent 模块设计文档 v2(mimo 执行核)

> **本文只描述当前态**:设计思路、拓扑、逻辑结构、数据流、精巧处、易错点、缺陷清单。
> **变更历史不在本文** -- 本项目为本地即运行模式、不做 git 入库(裁定见附录 B.2),过程记录查各模块头注。
> 改进项 **A~M 已于 2026-10-11 基本收口并并入本文附录 A**(状态/裁定/验收数据全在,施工过程从略);
> 原 `agent差距与改进路线.md` 同日移入项目根 `del/agent_roadmap_20261011/`(项目约定:删除=移动到根 del/,不硬删)。
>
> 执行核:mimoagent(pip 包),编排层已删除,无自研 StateGraph。
> 旧系统 `app/agent_smolagents/` 保留作对照,**本文不描述它**。
>
> **数字口径**:标「2026-10-02 实测」的为 `tmp/_doc_facts.py` 可复现基线;标「2026-10-10/11」的为收口期实测
> (smoke / qd_bench / dispatch_ab 报告,存档索引见附录 A.3);未标日期的为设计事实(代码即文档)。

---

## 一、设计思路

### 1.1 要解决的问题

Agent 模块是 QuantDinger 的智能决策核心:接收自然语言输入(个股分析、全市场筛选、策略诊断、交易确认、通用问答),
在 **102 个工具**(2026-10-02 口径)的面上收敛出当轮最小可用工具集,跑 ReAct 闭环,输出结论,并把可证伪的断言留档、T+N 用真实行情判定。

三个硬约束决定了后面所有的架构选择:

| 约束 | 后果 |
|---|---|
| 工具面 102 个、全量 schema **32,703 字符**(2026-10-02 口径) | 全量下发不可行 ⇒ 必须分级,且必须每轮现算 |
| 结论里的数字必须可溯源 | 需要 grounding gate(数字溯源拦截) |
| 用户会问"你上次说的对不对" | 需要追责四表 + T+N 真值判定 + 反哺行为 |

### 1.2 设计原则

| 原则 | 落地形态 |
|---|---|
| **最小工具面** | 三级分级 + 预选 + lint;必注入层 **22** 个 / 全量 **102** 个 |
| **fail-open 优先** | 预选空手有域兜底、未激活直调就地激活、能力层补位;宁多不可断 |
| **可追责** | `decisions/claims/resolutions` 三表 + T+N 判定 + 权重反哺 |
| **只提示、不过滤** | 权重/历史表现只做 hint,不做硬过滤(硬过滤与 fail-open 冲突) |
| **澄清优先** | `resolvers/` 挂在取数之前,歧义先反问,不猜默认值 |
| **领域中立** | 域走配置表 `qd_domain_resolvers`,代码里没有 `if domain == 'finance'` |
| **先量化再决定** | 拿不准的先装观测计数器,不写自动调参逻辑 |
| **规则不在代码里** | 决策规则外置(yaml / 参考文档),代码只做客观计算 |

### 1.3 四条关键取舍

1. **删掉自研编排**:旧版 StateGraph + 4 节点 + phase acceptance 全部删除,只保留 mimo 单 ReAct 循环 + 一个
   `update_plan` 清单工具。规划质量改由 prompt 纪律 + 收尾轻校验承载(§3.8)。
   ⚠️ 「单循环」≠「裸循环」:执行核实为 `QDAgent(MimocodeAgent)`,而 mimo 的 `MimocodeAgent`
   (MiMo-Code 目录)本就自带 **`task` 子代理 / `actor` 并行子代理 / `compact` 模型触发压缩 +
   `<context_usage>` 预算感知 / AntiHack 拦截器 / 入参 schema 硬校验 / stray `<tool_call>` 恢复**。
   本项目的「自研」只加在:金融域工具面(102)、工具分级与预选、金融 prompt 层、`update_plan` 补挂、
   追责闭环三件、grounding 门;循环与上述原语**全部沿用上游**(详见 §3.1 继承链)。
2. **分级是架构不是配置**:框架只在 init 算一次工具定义,我们 override `get_model_query_kwargs` 改成**每轮现算**,
   否则 `activate_tools` 激活了也不生效 -- 这是整套分层的前提。
3. **权重只提示不过滤**:硬过滤会让模型在真需要某工具时拿不到它。粒度也只到 domain -- resolutions 不带
   tool/skill 归因,硬按 `user_query` 反推"当时用了哪几个工具"是伪精确。
4. **接入组件前先问"它要几次 LLM 往返"**:本地计算(权重提示 0ms / 实体解析 95ms)与
   一整轮 LLM 生成(结论格式化 11.7s)是两类东西,评审阶段就要区分,不能等用户说"变慢了"再归因。

---

## 二、拓扑

### 2.1 架构图

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                用户层                                     │
│   Web UI(SSE)   │   CLI   │   Cron   │   message_queue                   │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                            接入层                                         │
│  flask_app.py(SSE) ←→ message_queue.py(线程池) ←→ agent.py(run_agent 同步入口) │
│  agent.py 装配期 warmup_tool_face():把冷启动挪出消息路径                 │
│  cron 由 cron/cron_worker.start_cron_worker 在 app/__init__ 启动        │
│  eval worker 由 chain/evaluator.start_eval_worker 启动(agent.py 装配期也触发) │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              会话层:QDAgentService(有状态,按 session_id 复用)         │
│   resolvers/bridge.resolve()(在 _run_sync 内,歧义→短路反问) → _prefetch()(记忆/RAG/技能注入) │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   执行层:QDAgent(继承 MimocodeAgent)                   │
│                                                                          │
│  _run_inner 单循环:                                                      │
│   0 _steps_taken=0 / current_plan=None  ← 预算与计划都是 per-run         │
│   1 _maybe_preselect_tools  ← 外置 plan + 确定性 lint + 域兜底           │
│   2 注入 now / tool_map / tool_availability 到 system_template           │
│   3 super().run()  → ReAct(每轮现算工具面 + 分层 fail-open)            │
│   4 空结果兜底(LimitsExceeded / Empty 分别处理)                        │
│   5 grounding_gate:数字溯源拦截 + 独立预算重试                          │
│   6 _plan_lint() 计划收尾轻校验(只告警)                                │
│   7 _finish_collector → 追责入库 intake                                   │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                            能力层                                         │
│  三级工具分级 │ 执行型技能子进程 │ RAG │ Skills │ Memory │ LLM │ Cron    │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                            存储层                                         │
│  decisions/claims/resolutions(追责) │ qd_agent_weights(权重)             │
│  qd_agent_traces(旧链路,仍在写) │ pgvector(RAG) │ Redis │ JSONL          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 目录结构(实测)

```
backend_api_python/app/agent/
├── agent.py              # 统一入口 Facade(run_agent 同步入口 + 装配期 warmup + 启动期触发 eval worker;cron 由 cron/cron_worker 在 app/__init__ 启动)
├── qd_agent.py           # ★ QDAgent:mimo 子类,全系统核心
├── qd_service.py         # 有状态会话服务(prefetch / 技能渐进式注入 / 会话去重)
├── flask_app.py          # Flask Blueprint(SSE)
├── cli.py                # CLI 入口
├── message_queue.py      # 统一消息队列(Flask/Cron 共用)
├── trace_collector.py    # TraceCollector → qd_agent_traces(root/skill/tool 三层)
├── mimo_boot.py          # mimoagent 依赖引导
├── constants.py          # 全局常量单一事实源(AGENT_MAX_STEPS 等)
├── domain_registry.py    # 跨层分类学注册表
├── log.py
│
├── tools/        [39 py]  工具层:顶层 15 + finance 21 + knowledge 3(2026-10-11 复点)
│   ├── base.py               # ToolProvider / func_to_openai_schema / _is_tool_function
│   ├── availability.py       # 启动期可用性探测 → {{tool_availability}}
│   ├── tool_discovery.py     # search_tools / list_tools / activate_tools
│   ├── tool_preselect.py     # ★ 外置 plan 预选 + 确定性 lint(已声明 _NOT_TOOLS)
│   ├── preselect_stats.py    # ⚠ 观测计数器(_NOT_TOOLS 只登记 2 个新名,见 §8 D-3)
│   ├── preselect_cooc.py     # 预选共现权重(K)build_weights / rank_candidates / heldout_recall
│   ├── skill_tools.py        # list_skills / read_skill / run_skill
│   ├── capability_tools.py   # search_capabilities / call_capability / list_capabilities
│   ├── memory_tools.py       # remember / recall
│   ├── format_utils.py       # 元工具 format_result(_MUST_HAVE,不进注册表;任意结构→LLM 可读串)
│   ├── web_search_tools.py   # 元工具 web_search(_MUST_HAVE;博查/Tavily/baidusearch/SearXNG 四引擎降级)
│   ├── fn_adapter.py         # FnToolAdapter:裸函数工具零改动适配 mimo BaseTool(迁移边界)
│   ├── mcp_bridge.py         # 可选:MCP server,把 tools/*.py 公开函数暴露为 MCP 工具
│   ├── pagination.py         # @paginated + page_tool:大数据量工具返回分页缓存(TTL 隔离)
│   └── finance/(21) knowledge/(3)
│     ※ filesystem_tools.py 已于 2026-10-11 退役:文件读写由 mimo 原生 read/write 承担
│     ※ mimo 原生 lowercase 目录(bash/read/write/edit/grep/glob/task)由 `MimocodeAgent` 基类注册;
│       本项目在其上**补挂** actor/compact(§3.1)+ update_plan(codex 目录,跨目录注册)+ 102 个 FnToolAdapter 工具
│
├── capabilities/ [4 py]  三级能力层(admission.json 准入 69 → 注册 40)
│     ※ admission.json 是准入事实源但**不在工作副本里**(本地文件不入库,见附录 B.2)
├── chain/        [13 py] 追责 + 评估 + 权重闭环 + 技能酿造
│   ├── claims.py             # 域判定 / 入库闸门 / Claim 提取(纯函数,零 LLM)
│   ├── intake.py             # record_decision 唯一入口
│   ├── account_store.py      # decisions/claims/resolutions 持久化
│   ├── resolver.py           # 阶段A 算数判定 + 阶段B LLM judge(默认关)
│   ├── weight_feed.py        # resolutions → qd_agent_weights(EMA 慢调,domain 粒度)
│   ├── weight_hints.py       # 权重**唯一消费口**(TTL 快照)
│   ├── evaluator.py          # 盘后 worker(旧评估 + 追责判定 + weight_feed)
│   ├── reset.py              # S2/S4 复位(dry_run 默认 True)
│   ├── schema.py             # 三层统一评估树数据结构(chain/skill/tool,parent_id 自引用)
│   └── store.py / judge_stats.py / skill_brewer.py
│
├── skills/       [6 目录]  market_screener / strategy_debug(**含 run.py,可执行**,白名单=skill_run.EXECUTABLE_SKILLS)
│                           + auto_stock-analyze / auto_finance-analysis-stock
│                           / auto_finance-query-stock / auto_agent(markdown 型:资本面尽调流水线)
├── rag/[6] memory/[5] llm/[7] utils/[11] resolvers/[6] cron/[4] audit/[2]
├── prompts/      [4 txt]  qd_core_base.txt(基座,~900字,先注入)/ qd_system.txt(金融纪律,后置覆盖)/ skill_brew.txt / skill_revise.txt
├── scripts/      [6 py + 2 yaml]  qd_smoke.py(回归 248 项)/ qd_prompt_eval.py + prompt_tasks.yaml(10 任务/35 断言)/ qd_bench.py + bench_tasks.yaml(25 题)/ qd_dispatch_ab.py(分发 A/B)/ skill_run.py(执行型技能子进程入口)/ weekly_panel.py(计数器面板)+ bench/(跑分、对照报告存档)
├── agents/       -        ✅ 死代码已清(原 2 py、0 引用)
└── traces/, data/, tmp/   辅助目录
```

> **存活判定方法**:`grep -rnE "^\s*(from|import)\s+<pkg>\b"` 排除包内自引用。
> ⚠️ 只查 `from app.agent.<pkg>` 会**漏判** -- 本包统一用**裸名** import(`from chain.store import ...`,见 §7.1)。

### 2.3 核心文件职责速查

| 文件 | 职责 | 关键点 |
|---|---|---|
| `qd_agent.py` | 执行核 | override `get_model_query_kwargs` 每轮现算工具面;`execute_action` 分层 fail-open |
| `tools/base.py` | 工具注册 | `_NOT_TOOLS` 模块级声明,防止管线函数被误注册成工具 |
| `tools/tool_preselect.py` | 预选 + lint | 加法先于减法;有补位则不得作废 |
| `tools/skill_tools.py` | 技能读写 + 执行分发 | `read_skill` 读方法论;`run_skill` 跑流水线(子进程) |
| `scripts/skill_run.py` | 执行型技能子进程入口 | 白名单注册表 + fn 白名单 + 参数校验 + 超时击杀 + 产物落盘 |
| `chain/claims.py` | 追责抽取 | 纯函数;闸门判不出来就拦(宁漏不脏) |
| `chain/weight_hints.py` | 权重消费 | TTL 600s 快照;**务必经此入口读权重** |
| `resolvers/bridge.py` | 澄清 / 实体解析 | 挂在取数**之前**;clarify 非空即短路反问 |
| `qd_service.py` | 会话与预取 | 技能渐进式注入(`_skill_block`)+ 会话级去重(`_skill_seen`)+ 执行型技能分发卡片 |
| `utils/case_memory.py` | 案例记忆(CBR,E) | `record_case` 写入 / `retrieve_cases` 检索 / `backfill_by_root` 延迟回填;incorrect 硬排除、pending 降权 |
| `tools/preselect_cooc.py` | 预选共现权重(K) | 缺数据/样本<50/异常一律退回原序(冷启动与纯启发式 bit 级一致) |
| `scripts/qd_bench.py` | 能力级跑分(C) | coverage/grounding/claims/structure/human 五维确定性打分;validate/score/run/trend/diff/agreement |
| `tools/format_utils.py` | 元工具 | `format_result`(不进注册表,经 `_MUST_HAVE` 暴露);任意结构→LLM 可读串,去 JSON 冗余 |
| `tools/web_search_tools.py` | 元工具 | `web_search`(不进注册表);博查/Tavily/baidusearch/SearXNG 四引擎自动降级 |
| `tools/fn_adapter.py` | 工具适配 | FnToolAdapter:裸函数工具零改动适配 mimo BaseTool(契约沿用现行约定) |
| `tools/mcp_bridge.py` | 工具桥接 | 可选 MCP server,把 tools/*.py 公开函数暴露为 MCP 工具(独立进程) |
| `tools/pagination.py` | 返回分页 | `@paginated` + `page_tool`:大数据量工具返回按页缓存,跨 session TTL 隔离 |
| `prompts/qd_core_base.txt` | 系统提示基座 | ~900 字通用基座(原生工具清单/系统标签/压缩纪律),`qd_system.txt` 后置覆盖 |

---

## 三、逻辑结构

### 3.1 执行核:mimoagent

**事实(读安装包源码得出)**:

| 位置 | 行为 |
|---|---|
| `agents/default.py:102` | `self._tool_definitions = registry.get_function_definitions()` -- **init 算一次** |
| `agents/default.py:197` | `get_model_query_kwargs()` 每轮返回**同一份** + `tool_choice="auto"` |
| `agents/base.py:255` | `model.query(messages, **kwargs)` ⇒ tools 进请求体,**每轮重发** |
| `agents/default.py:189` | `get_tool_context()` 返回 `{"env","model","agent":self}` ⇒ `update_plan` 能把计划写回 agent |

- **框架不做任何工具筛选/预选/检索**,选谁完全交给模型。
- 包内无 tool search / 按需激活 / 延迟加载(我们自己在 `tools/tool_discovery.py` 补的)。
- **我们能做分层的关键**:override `get_model_query_kwargs` 改成每轮现算。
  (以上 4 条读自 `agents/default.py` / `agents/base.py`——即 `MimocodeAgent` 的**祖父类**;实际继承链见下。)

**继承链(实测,安装版 `mimoagent 0.1.0`,与上游 `mimo-oss` 分支同构)**:`BaseAgent → DefaultAgent → MimocodeAgent → QDAgent`。

- `BaseAgent`(`agents/base.py`):循环骨架;`run() → (status, message)`;状态 `Idle / LimitsExceeded / ModelQueryError / InfraError`;`step_limit` 在 `query()` 里卡;**空回复也抛 `LimitsExceeded("Empty assistant response")`**;`self.messages` 跨 `run()` 复用(多轮);`subagents` 列表;`msg_path` 逐 agent 转写。
- `DefaultAgent`(`agents/default.py`):**并行工具调用**(`tool_parallel_workers=8`,ThreadPoolExecutor,`executor.map` 保序);**observation 中截断**(`max_observation_length=40000` 字符,`utils/truncate.py::truncate_middle`,带刺眼 banner + 切点标记);`wrap_up_hint`(上下文预算催收,`wrap_up_hint_fraction=0.9`,默认关);`ActionInterceptor` 预派发钩子(`add_action_interceptor`,命中即短路该工具)+ `_execute_tool` 结果装饰缝,两条缝分工;`ToolOutput(output/success/metadata/media)` 支撑多模态观察。
- `MimocodeAgent`(`agents/mimocode/`,**本项目实际基类**):MiMo-Code lowercase 目录(`bash/read/write/edit/grep/glob/task` + 可选 `actor`/`compact`);`task`=**子代理**(explore/general 预设,共享 env/model、独立 messages、`step_limit=500`、登记进父 agent 的 `subagents`);`actor`=**并行子代理**(受 `actor_max_concurrency` 信号量约束);`compact`=**模型自触发压缩** + `<context_usage>` 页脚(`compaction_context_window` 上游默认 **1,000,000**);`AntiHackGuard` 以 interceptor 形态挂载(上游默认关);`_parse_tool_call` 做**入参 schema 硬校验**;`step()` 检测 **stray `<tool_call>`** 转 `FormatError` 续跑;`tool_state` = 逐 agent 可重放状态(cwd + 计划台账);`_execute_tool` 给每个真实结果挂 `cwd`。
- 框架其余:`agents/factory.py`(19 个 `agent.type`,含 13 个 blackbox CLI 适配器)、`models/`(**chat / anthropic / responses 三原生协议** + `TokenStats` / `GlobalModelStats` 全局计数限)、`environments/`(local/docker/kubernetes/cube/modal)、`environments/datasets/*` + `rubric_judge.py` + `reward_mode`(**dataset / grader 层**)、`run/extra/batch.py` + `ray_batch.py`(批量 rollout;轨迹按训练 schema 落盘)。

**框架给了、本项目用没用/改没改口径的原语**(对齐 §1.3「原版为主、增强为辅」):

| 框架原语 | 本项目处置 | 依据 |
|---|---|---|
| `task` 子代理 / `actor` 并行子代理 | `task` 进必注入(`task≈2.1k`);`actor` 默认降级为按需 | `qd_agent.py:230-232, 270-272` |
| `compact` + `<context_usage>` | 沿用;并把窗口从上游默认 **1M** 改回**真实窗口** | `qd_agent.py:312-321` |
| `wrap_up_hint` | **关**(避免与 `<context_usage>` 双重催收) | `qd_agent.py:319` |
| `AntiHackGuard` | **显式开启**(`AntiHackConfig(enabled=True)`,上游默认关) | `qd_agent.py:234` |
| `compaction_summary_prompt` | 换成金融域压缩纪律 `_QD_COMPACT_NOTES` | `qd_agent.py:236` |
| `max_observation_length` | 继承 Mimocode 的 `0`(目录自限);项目另建 `tools/pagination.py` 分页 | `MimocodeAgentConfig:53` |
| `execute_action` | 项目**覆写**(探针 + 分层 fail-open),与 interceptor 两缝并用 | `qd_agent.py:900-923, 440-442` |
| dataset / grader / rollout(`batch.py`、`rubric_judge`) | **未用**;评测走自建 `scripts/qd_bench.py` 三尺 | §3.13 |

⚠️ 因此旧口径「只保留 mimo 单 ReAct 循环 + 一个 `update_plan`」**低估了框架**:子代理 / 压缩 / 预算感知 / 反作弊 / schema 校验都是**上游原语**,本项目是「继承 + 域增强」而非"从零搭循环"。不写清会被后人当成自研件重复造。

### 3.2 工具面:三级分级 + 分层下发 + 按需降级

| 级 | 定义 | 实测 |
|---|---|---|
| **一级 必注入** | `tools/` 顶层 + mimo 原生/元工具 | **22**(其中 19 个来自注册表 + `web_search`/`format_result`/`update_plan`;19 里含 mimo 原生 `task`/`compact`,`actor` 默认按需) |
| **二级 域内** | `tools/finance` + `tools/knowledge` | 域内优先于能力层 |
| **三级 能力层** | `capabilities/` 准入函数 | 准入 69 → 注册 **40**(29 项让位给域内) |

- 注册表总量 **102**(工具 62 + 能力层 40);必注入 **22** + 按需 **83**(含 3 个非注册表来源)。
- **schema 体积**:全量 **32,703 字符** ⇒ 必注入 **7,912 字符**(**-76%**)。每轮请求都带这一整块,prompt cache 省钱但**不省窗口**。
- **"域内优先"的三处落实**:1 `build_catalog_grouped` 目录分两段、能力层垫底并标注"仅当域内无覆盖时才选";2 lint **R0 能力层让位**;3 `apply_lint(capability_names=)` 排序垫底。

**按需降级旋钮**:`QD_ON_DEMAND_TOOLS`(默认 `actor,run_skill`)可把必注入工具降级为按需。
降级后仍拿得到,三条兜底:

1. **分层 fail-open**(`qd_agent.py::execute_action`):已注册但未激活的工具被直调时**就地激活放行**,不会 `Unknown tool`;
2. **工具预选目录由 `_on_demand_names` 构建** ⇒ 相关任务首轮就能被点名激活,不产生"search→list→activate"三轮发现;
3. **`qd_system.txt` 的"重活分发"小节直接点名 `run_skill`** ⇒ 模型知道名字。

> ⚠️ 因此 `qd_system.txt` 里**不能**写"未激活直接调用会失败"--那是旧文案,与 fail-open 相反,会让模型绕回三轮发现。

### 3.3 工具预选:外置 plan + 确定性 lint

**为什么不是"让 LLM 裸选工具"**:裸选会让天气问题被塞 8 个股票工具、token +136%、净收益≈0。真根因是**预选输入被 prefetch 污染**(用了含历史个股记忆的增强文本)。

```
1 域特征闸门 has_domain_hint()   -- 纯字符串,成本≈0,不命中才跳预选
2 分级计算 cap/dom               -- ★ 必须在闸门之前算,否则兜底拿不到候选
3 build_catalog_grouped()        -- 域内段 + 能力层段
4 LLM 产出 plan {goal, deliverables, tools, step_budget, phases}(带 history_hint)
5 parse_plan() → 6 lint_selection()(不调 LLM)
     R0 能力层让位(域内已覆盖则裁)
     R1 域词典覆盖补位(加法,高置信)
     R4 零相关裁剪(减法)  R4b 整体作废
     ⚠ 有补位则不得作废
7 apply_lint()  -- 先加法后减法,清空则保留原面
8 _finish_preselect()  -- 空手时用 fallback_domain_tools 兜底
```

**实测收益(重任务)**:`stock_screen` 153,960 tok / 6 步 → 43,318 / 2 步(**-72%**);
`trade_confirm` 100,150 / 6 步 → 34,976 / 2 步(**-65%**)。

**域兜底(不许落回裸工具面)**:`fallback_domain_tools(query, available, limit=4)`(纯词典、零 LLM)补核心子集 --
「帮我看看大盘」→ market 补 4 个;「今天北京天气」「写个跑马灯」→ **0 个**(命中不了不硬塞)。

### 3.4 技能系统

| 环节 | 机制 |
|---|---|
| **选技能** | 词典分 + 范围信号(有无标的)+ 意图信号;权重仅 tie-break;`_FINANCE_GATE` 防"跑马灯"被注入选股技能 |
| **注入** | 渐进式:`_skill_block` = 正文首段(上限 2200 字符)+ `[技能目录]` + `[按需读取]` 路标。**执行型技能不注正文**:命中 `EXECUTABLE_SKILLS` 时改注**分发卡片** `_dispatch_card()`(≤600 字符门禁,现 407:何时派/`run_skill` 签名/入口/结果读法/**空结果纪律**/`read_skill` 兜底)|
| **去重** | `_skill_seen`(per session):二次命中只给一行指针,正文不再重复注入 |
| **读** | `read_skill(name)` 全文 / `read_skill(name, heading=)` 小节 / `read_skill(name, resource=)` 资源 |
| **执行** | `run_skill(name, arguments)` → 子进程 `scripts/skill_run.py`(仅执行型技能) |

**执行型技能分发**(`market_screener` / `strategy_debug`):

```
主 agent
  └─ run_skill(name, arguments)  ──► 子进程 scripts/skill_run.py
        1 技能名白名单(EXECUTABLE_SKILLS)  2 fn 白名单
        3 inspect.signature 未知参数拦截(不执行就退出)
        4 执行 → 产物落 app/agent/tmp/skill_output/*.json
        5 回传 {ok, result(预览≤20k字符), full_path, elapsed_s}
        6 空结果(result.analyzed 为空)⇒ 按 §重活分发「空结果纪律」收尾交付,
          不得回退主对话逐个取数重跑(2026-10-12 接线;空分支需自带口径,见上注)
```

三重收益:**超时可击杀**(600s)、**崩溃隔离**(不污染主进程)、**主上下文只拿摘要**(O(摘要) 而非 O(全量))。

> ⚠️ **技能名两个拼写,必须归一化(2026-10-12 实测修复)**:
> - **目录名**(下划线)= 执行注册表键:`skills/market_screener/` ⇔ `skill_run.EXECUTABLE_SKILLS["market_screener"]`;
>   `strategy_debug` 同理。
> - **显示名**(连字符)= SKILL.md frontmatter `name: market-screener`,由 `QDSkillAdapter.display_name`
>   产出,是 `list_skills()` / `read_skill` 路标 / `[相关技能：xxx]` 注入给模型的那个名字。
> - `read_skill` **能**吃显示名(`QDSkillAdapter.get` 走 `_display_to_name` 映射);`run_skill`
>   **原来不做归一化**就透传给 `skill_run.py`,而那边是精确匹配 ⇒
>   `run_skill("market-screener")` 恒定返回 `未知技能。可执行: ['market_screener', 'strategy_debug']`。
> - 修复:`run_skill` 在起子进程前用 `_get_skills().get(name).name` 归一化(复用既有注册表,
>   不另写字符串替换)。实测修复前 `ok=false` / 修复后 `ok=true`。
> - 仍存在的隐患:**同一份上下文里两种拼写并存**(分发卡片用显示名、系统提示"重活分发"小节写
>   `market_screener / strategy_debug`),名字不一致本身就在诱导模型犹豫。

> ⚠️ **子进程「跑完退不出」陷阱 —— 600s 超时的真身不是流水线慢(2026-10-12 实测修复)**:
> - 现象:盘前 `run_skill(market_screener)` 恒定 `Error: skill_run 超时（>600s）已中止`，
>   一度被读作「盘前全市场扫描比盘后慢 12 倍」。
> - 实测(`tmp/_screener_hang.py` 线程栈采样):`run()` 本体只要 **51~58s**，随后**进程 10 分钟不退出**；
>   `tmp/_screener_timing.py` 逐项计时也印证取数不慢:涨停池 8 天 29s / 热点 0s(盘前源空) /
>   条件搜索 2 条 2s / 市场态 2.2s / 30 次 K 线 4.1s ≈ 37s。
> - 真因:取大盘日线走 mootdx，`Quotes.factory(heartbeat=True)` 会起
>   `tdxpy.heartbeat.HeartBeatThread` —— **非 daemon**，10s 周期 `stop_event.wait()` 无限循环，
>   只有 `disconnect()` 才停。主线程结束后 `threading._shutdown()` join 它 ⇒ 永不返回 ⇒
>   `subprocess.run(timeout=600)` 必然超时。连带的 `mkline-timeout_*` 等 futures 池线程靠
>   `threading._register_atexit` 唤醒，而该回调排在 join **之后**，连唤醒信号都收不到。
> - 两道防线:① `app/utils/mootdx_client.py::_daemonize_heartbeat()` 把心跳线程设为 daemon
>   (**根治**——CLI 主进程同样受益，否则它退出时也会被挂住)；
>   ② `scripts/skill_run.py` 的 `__main__` 显式 `os._exit(_rc)`(一次性子进程不等任何常驻线程，止血)。
> - 守卫:`tests/test_skill_run_exits.py`(心跳线程 daemon 化 + `__main__` 必须含 `os._exit`，共 2 项)。
> - 修复后实测:子进程 **43.6s 自行退出**(rc=0，`elapsed_s` 42.98)，600s 超时不再触发。

**两层分工裁定(2026-10-10)**:知识型技能注方法论正文(渐进式加载),执行型技能只注分发卡片(`run_skill` 派活)。
执行型正文注进上下文是真冗余且有行为风险(诱导主对话硬跑,与「重活分发」抢方向盘);`read_skill` 兑底保留。

> 📌 **与 mimo 原生子代理的关系(别混)**:`run_skill` 是**独立子进程**跑本项目自己的流水线
> (`scripts/skill_run.py`),拿不到主 agent 的 env/model,只能跑白名单技能。
> mimo 的 `task`/`actor` 子代理是**同进程子循环**(共享 env/model、独立 messages,用于探索/并行分析),
> 读不到本项目技能注册表。两者**正交,不要互相替代**(§3.1 继承链)。

⚠️ 白名单目前仍是**硬编码在 `skill_run.py` 里**的字典 -- 与"规则不在代码里"相悖,
待执行型技能变多后下沉为各技能目录内的声明文件(详见 §8 D-11)。

### 3.5 追责闭环(`chain/`)

**四张表**(`migrations/agent_v4_trace.sql`):

```
qd_agent_decisions    一次对话一行(闸门不通过 ⇒ 一行都不写)
qd_agent_claims       可证伪断言(direction / magnitude / level)
qd_agent_resolutions  T+N 判定结果(verdict / score / attribution / deviation)
qd_domain_resolvers   域→策略配置(enabled / horizon / judge 采样率)
```

**三条裁定**:1 不追责的不入库(`intake_gate` 前置,宁漏不脏);2 慢调、容忍误判(方向对即 hit 0.75,
幅度差只记 deviation;但 `undecidable`/`data_missing` **不计权重**--常量污染 ≠ 误判);3 多域通用,v1 只开 finance。

**闭环**:

```
resolutions ──weight_feed.feed()──→ qd_agent_weights(layer='domain')
                                      │ EMA 慢调:α=clamp(n/50, 0.1, 0.5)
                                      ↓
                            chain/weight_hints.py(唯一读取口,TTL 600s)
                                      ├→ 工具预选 history_hint(只提示不过滤)
                                      └→ qd_service._rank_skills **排序分乘子**(G)
                                           score × (0.5 + 0.5×weight),weight∈[0.5,2.0] ⇒ 乘子∈[0.75,1.5]

**表分层(四层快照 skill / tool / chain / domain)**:两条产线各写各层、互不覆盖--`weight_feed`
只写 domain(resolutions 挂 claim→decision,不带 tool/skill 归因,硬反推是伪精确);skill/factor/tool/chain
由旧 `evaluator.update_weights`(traces 侧)负责。样本纪律在写侧(`weight_feed` n<MIN_SAMPLES 只记账、
evaluator n<10 恒 1.0),读侧不重复校验。
```

**判定两阶段**:阶段 A 代码算数(取真实 K 线);阶段 B LLM judge(默认关,`QD_CLAIM_JUDGE` 开)。

### 3.6 澄清优先(`resolvers/`)

`resolvers/bridge.py` 挂在 `_run_sync` 取数**之前**:`clarify_question` 非空 ⇒ **直接反问、不进执行**
(`QD_RESOLVER=0` 关整套;`QD_RESOLVER_CLARIFY=0` 退化为"只解析不阻断")。
解析出的标的/领域注入上下文,并供技能选择使用。

⚠️ 副作用需接受:非交易日问「今天买什么股票好」会被反问。`QD_RESOLVER_CLARIFY=0` 可退让。

### 3.7 grounding gate

结论中的数字必须能在本轮工具输出(`_observation_corpus()`)里溯源;无法溯源的数字改写为"未获取"并说明缺失口径。
重试给**独立步数预算**(否则主轮耗尽后重试瞬死)。仍不过 → `status="grounding_failed"`。

### 3.8 计划质量轻校验(`_plan_lint`)

`update_plan` 的清单若收尾时仍留 `in_progress`/`pending`,通常是跑偏/被截断的信号。
`_plan_lint()` 在 run 收尾落 `plan_lint` trace 事件 + warning,**只告警不拦截**。
`current_plan` 是 **per-run** 的(与步数同生命周期,run 开头归 `None`);它由 `UpdatePlanTool` 经
`context["agent"]` 写回(`update_plan.py:74`)。

### 3.9 观测计数器(只观测,不改行为)

| 模块 | 观测 | 阈值(写死在 docstring) |
|---|---|---|
| `tools/preselect_stats.py` | 空域占比 | >2% 补域词典 → >5% 放宽 GATE → >10% 查 prompt/模型 |
| `chain/judge_stats.py` | judge vs 规则锚点分歧率 | <10% 且样本>100 ⇒ 永远别开;>20% ⇒ 建人工校准集 |

设计约束:只观测,不自动改判定行为;原因标签走 `_empty_route()` **稳定归类**(gate/no_pick/lint_void/call_error/unknown),
不许存 `reason` 原文(改文案会断统计);空域日志 keyword **`preselect-empty-face`**。

### 3.10 其余组件

| 组件 | 说明 |
|---|---|
| RAG (`rag/`) | BGE reranker + pgvector + PostgresFTS + 历史检索 |
| Memory (`memory/`) | 跨会话记忆(`remember`/`recall` 工具化) |
| LLM (`llm/`) | `LLMFactory` + `QDSkillAdapter`(技能加载器) |
| Capabilities (`capabilities/`) | 三级能力层,`admission.json` 准入,注册期 29 项让位给域内 |
| Cron (`cron/`) | ⚠️ 被**旧系统**反向依赖(§8 D-2) |

### 3.11 案例记忆(`utils/case_memory.py`,E)

run 级一次性 few-shot(CBR),填技能酿造的粒度空档。**增益层**:一切 DB/向量操作 fail-open,
绝不阻断主链(但失败要 warning 可见--本模块高发"声明了没接线",静默=断链复发)。

| 环节 | 机制 |
|---|---|
| **写入** | `qd_agent._finish_collector` 后置 `_record_case` → `record_case()`;label 恒 `pending`,真值待 T+N 回填;失败坑/成本/plan_digest 齐落 |
| **检索/注入** | `qd_service._prefetch` → `retrieve_cases()` + `format_case_injection()`;`CASE_MEMORY_ENABLED` 总闸(默认开) |
| **取舍** | `incorrect` **硬排除**(错误结论不许以 few-shot 自我复制);`pending` 降权 0.5×;近重复(相似度>0.95)只留最新且已验证一条;成功例给骨架+坑,失败例只给坑 |
| **分类** | `classify_case_tags` 六类:诊个股/选股/板块复盘/大盘研判/交易确认/止损复核;同型标签 ×1.2 只调序不过滤 |
| **回填** | `backfill_by_root`:T+N 定论后 pending→correct/incorrect;只写 pending 行(人工反馈 human_reviewed 不得被覆盖) |
| **存储** | pgvector 表 `qd_agent_cases`(JSONB 存向量 + Python 余弦,不依赖 pgvector 扩展);无 embedding 配置退化 2-gram 词面相似 |
| **预算** | 单例注入 ≤500 tokens(实测最大 240,tiktoken) |

### 3.12 工具预选共现权重(`tools/preselect_cooc.py`,K)

预选底座仍是启发式(token 倒排 + 域词典),此层给它加**可学习的排序权重**:

1. `preselect_stats.record_task_calls` 落盘「任务 → 实际调用工具」对(env `QD_PRESELECT_PAIRS_FILE`);
2. `preselect_cooc.py`:token×工具共现 `build_weights`、mtime 缓存 `load/get_weights`、加权 `rank_candidates`、
   验收器 `heldout_recall`、周级 CLI(`--db` 读现表 `qd_agent_traces`,**未开新表**);
3. `qd_agent` 两钩子:过域闸门后加权目录序、收尾攪对。

**铁律**:权重缺/样本<50/零覆盖/异常 → 原序返回,**冷启动与纯启发式 bit 级一致**。
验收口径:held-out 任务预选召回率(实际调用工具 ∈ 预选面)≥95% 且 token 不回升。

### 3.13 评测三尺(谁管什么)

| 尺 | 管什么 | 产物 |
|---|---|---|
| `scripts/qd_smoke.py` | 回归(不调模型) | 248 项断言;`--with-env` 把环境缺件 fail→skip |
| `scripts/qd_prompt_eval.py` + `prompt_tasks.yaml` | prompt 行为(真实端点) | 10 任务/35 断言,含规划 5 题;round 报告存 `scripts/prompt_eval/` |
| `scripts/qd_bench.py` + `bench_tasks.yaml` | 能力质量(C) | 25 题五类各 5;五维确定性打分;run 存档 `scripts/bench/runs/` |
| `scripts/qd_dispatch_ab.py` | 成本对照(B) | 分发 vs 不分发单变量 A/B;成本比+质量差一键判定,`--rejudge` 不重跑复核 |

`qd_bench` 五维:coverage 30(baseline.must 机检命中)/ grounding 25(复用 `utils/grounding`)/
claims 20(复用 `chain/claims`)/ structure 15 / human 10(未标注按比例摊回);
CLI:`validate | score | run | trend | diff | agreement | import-traces`;`--to-db` 挂 `qd_traces` 树(fail-open)。

---

## 四、执行流程

### 4.1 单轮请求流程

```
用户输入
  │
  ▼
QDAgentService.chat()  (外部经 agent.run_agent 同步入口进入)
  ├── 设 agent.last_user_query = 原始问题(★ 预选只认它,防 prefetch 污染)
  ├── 0 resolvers/bridge.resolve() 在 _run_sync 内调用 → (info, clarify)
  │       └ clarify 非空且 clarify_enabled() ⇒ 短路反问,直接返回(不进执行)
  ├── _prefetch(resolve_info) → (text, report):记忆/RAG/技能渐进式注入,report 落 trace
  ▼
QDAgent._run_inner()
  ├── _steps_taken = 0 / current_plan = None     (per-run,不跨请求累积)
  ├── 1 _maybe_preselect_tools:闸门 → 目录 → LLM plan(带 history_hint)→ lint → activate
  ├── 2 注入 now / tool_map / tool_availability
  ├── 3 super().run()  ReAct(每轮现算工具面;未激活直调 → fail-open 就地激活)
  ├── 4 空结果兜底:LimitsExceeded→可读文案;Empty→轻推一次(独立预算)
  ├── 5 grounding_gate:数字溯源拦截,最多重试 N 次(独立预算)
  ├── 6 _plan_lint():计划残账告警
  └── 7 _finish_collector → _intake_accountability(追责入库)
```

> ⚠️ **顺序有讲究,别随便挪**:resolver 在最前(歧义要在花钱调工具之前拦截);
> weight_hints 在预选时读(要影响工具选择,就得在选之前)。

### 4.2 步数与预算语义

`step_limit` 是**单次 run 预算**,会话复用同一 agent ⇒ `_steps_taken` 必须每轮归零,否则轮次多了新消息一进来就
`LimitsExceeded` 且 `str(e)==""`(静默空结果)。**每个"附加轮"(空回复轻推 / grounding 重试)都给独立预算**。

### 4.3 盘后 worker

`chain/evaluator.start_eval_worker()`(由 app/__init__ 启动;agent.py 装配期也触发一次)→ 每日 15:30 等 `post_market_done` → `auto_evaluate()`:
1 旧 `evaluate_pending()`(写 `qd_agent_traces`);2 `resolve_due_claims(limit=200)`(写 resolutions);
3 `weight_feed.feed()`(写权重)。三者**独立 try,失败互不影响**;健康度看 `worker_health.last_weight_feed`。

---

## 五、数据流

### 5.1 请求数据流

```
用户问题
  → resolver(实体/领域解析;clarify 非空 ⇒ 短路反问)
  → prefetch(记忆/RAG/技能) → 预选(plan+lint+history_hint) → 激活工具
  → ReAct 多轮(每轮: 必注入层 + 已激活 schema + 历史)
  → grounding 拦截 → 文字结论
  → TraceCollector.flush() → qd_agent_traces(root/skill/tool 三层)
  → intake.record_decision() → decisions + claims(闸门通过才写)
```

### 5.2 追责 → 权重 → 行为

```
decisions/claims ──T+N到期──→ resolver.resolve_due_claims()
                                  ├ 取真实 K 线(exec_date 为基准★)
                                  ├ 阶段A 算偏差 → verdict/score/attribution
                                  └ 写 qd_agent_resolutions
                                        │
                                  weight_feed.feed()(EMA 慢调,domain 粒度)
                                        ↓
                                  qd_agent_weights
                                        │
                                  chain/weight_hints.py(唯一读取口,TTL 600s)
                                        ├→ 工具预选 history_hint(只提示不过滤)
                                        └→ qd_service._skill_score(tie-break)
```

⚠️ 非遗漏的设计选择:**只提示不过滤**(硬过滤与 fail-open 冲突);**粒度只到 domain**
(resolutions 不带 tool/skill 归因);**因子层未闭环**(`get_factor_weights` 仍返回 `{}`,见 §8 D-1)。

### 5.3 执行型技能分发流

```
用户:"全市场扫一遍"
  → 主 agent 读 system「重活分发」纪律(或预选激活)
  → run_skill(name="market_screener")
       └ 子进程 skill_run.py → 白名单校验 → 执行 → 产物落盘
  → 回传预览 + full_path
  → 主 agent 引用预览写结论,明细留在文件里(不搬进上下文)
```

---

## 六、精巧处

1. **每轮现算工具面**:override `get_model_query_kwargs` -- 框架只在 init 算一次,不 override 则激活无效。
2. **预选输入只认 `last_user_query`**:剥掉 prefetch 注入块,否则 goal 被历史个股带偏(曾致工具全错)。
3. **lint 加法先于减法 + 有补位不作废**:减法性规则曾把资金流工具全清空。
4. **域兜底 `fallback_domain_tools`**:宁可补 4 个也不能落回裸工具面(旧系统 P0 断言同款)。
5. **分层 fail-open**:既省每轮 token,又不制造"工具看得见调不动"的死胡同。
6. **未激活直调就地激活**:把"分层"的成本转嫁给真正用到的那一次调用。
7. **技能渐进式注入 + 会话去重**:同一技能连续命中时从 2419 → 101 字符(换技能的那轮不省)。
8. **resource 路标只在 `list_resources()` 非空时才给**:5 个技能里 4 个无资源文件,无条件给会诱导必然失败的调用。
9. **指针不写"见上方历史"**:上下文压缩会改写历史,而 `_skill_seen` 对此不知情 ⇒ 指针必须给
   `read_skill(name=...)` 自取全文的路径。
10. **执行型技能走子进程白名单**:超时可击杀 + 崩溃隔离 + 主上下文 O(摘要)。
11. **追责闸门前置**:判不出来就不入库(宁漏不脏),避免用噪声训练权重。
12. **观测只计数**:不让观察演变成自动调参。
13. **执行型技能只注分发卡片**:正文既费 token 又诱导主对话硬跑;卡片只给"何时派/怎么派/结果读法",明细留在产物文件。
14. **权重乘子冷启动 bit 级一致**:`score × (0.5+0.5×weight)` 但阈值仍看原始词典分--调序不过滤,无数据时行为与改动前逐位相同。
15. **技能空结果也是结果**:盘后空筛是合法结论,不得回退主对话硬跑重现一遍(B 首跑教训,见 §8 D-18)。

---

## 七、易错点与设计陷阱

### 7.1 导入与路径(本包统一**裸名**,最容易踩)

| 坑 | 说明 |
|---|---|
| **`app/agent` 必须排在 `app/` 之前** | 否则 `import utils` 命中 `app/utils`(23 个裸名都会错位)。`ensure_path_order()` 幂等保序 |
| **`app/agent/skills/market_screener/common.py` 的路径层级** | 曾按"..×3"算成 3 层,实为 4 层 ⇒ 把 `app/` 插到 sys.path[0] ⇒ 9 个 finance 工具静默挂掉,而**能力层补位把缺口掩盖了**;只有直接 `python cli.py` 才会踩 |
| **`cron_worker` 不是顶层名** | 必须 `from cron.cron_worker import start_cron_worker`。写成裸名会 ImportError 被吞 ⇒ 定时任务从未启动 |
| **判定"是否真执行"查标志位** | 别靠日志(logger propagate 不可靠),查 `_worker_started` 这类状态标志 |
| **`python -m` 跑 smoke 会误报** | `-m` 会先导入 `app.agent.scripts` 包,留下长名键 ⇒ test_29 判"无 `app.agent.*` 长名键"失败。必须 `python app/agent/scripts/qd_smoke.py` |

### 7.2 工具面

| 坑 | 说明 |
|---|---|
| **管线函数被注册成工具** | 判据是"公开 + 有 docstring + 定义在本模块" ⇒ 纯工具模块对,管线模块会被误伤。用模块级 `_NOT_TOOLS` 退出注册(目前只有 `tool_preselect.py` 声明了,`preselect_stats.py` **漏了**,见 §8 D-3) |
| **模块导入失败 = 静默降级** | 只 warning 没人看 ⇒ 落 `_TOOL_MODULE_IMPORT_FAILURES`,由 smoke `test_30` 断言为空 |
| **聚合摘除 key 要抹顶层前缀** | `_SUPERSEDED_BY_MERGE` 的 key 用「子目录.模块名:函数名」,否则长名/短名周期命中不一致(曾致 20 个废弃工具混在面里而自检反通过) |

### 7.3 数据库与统计

| 坑 | 说明 |
|---|---|
| **游标行是 dict(RealDictRow)** | 必须 `row['k']`;`row[0]` KeyError、`for a,b in rows` 解包出列名(全静默)。裸 psycopg2 那几处是 tuple 行,**别一起改** |
| **`SELECT COALESCE(MAX(id),0)` 必须起别名** | 否则列名是 `coalesce`,按 `["c"]` 取 KeyError ⇒ 清理逻辑被静默跳过 |
| **`%m-%d` 丢年份** | 日期一律 `%Y-%m-%d` |
| **统计推断三坑** | 1 AUC 写成补角;2 平移置换只能平移"标签序列",平移值序列零分布退化;3 **跨基数比较**(过滤后 n 变小/NaN 静默丢样本 ⇒ "过滤提升"多为假象)。详见项目 MEMORY |

### 7.4 行为一致性(prompt 与代码会漂移)

| 坑 | 说明 |
|---|---|
| **prompt 文案 vs 代码行为** | 曾出现 `qd_system.txt` 写"未激活直接调用会失败",而代码是 fail-open -- 模型被误导绕远路。**改分层/改 fail-open 必须同步 system prompt** |
| **compaction 会改写历史** | 任何"见上方历史"式的引用都会失效。指针必须给可自取的 `read_skill(name=...)` |
| **会话去重键要同口径** | `_skill_seen` 的键必须 `str(session_id)`,与 `_get_agent` 一致,否则将来加清理逻辑会漏删 |
| **格式化类组件 = 一整轮 LLM** | `formatters/` 已因延迟摘除(稳态 11.7s)。恢复前必须满足:有超时 / 轻量模型 + max_tokens 上限 / 可选触发 |

### 7.5 验证方法

- **负向验证必须确认缺陷真注入**:曾因 heredoc 转义出错导致缺陷没注入却跑出全绿,差点误判"护栏失效"。
  改为落盘脚本(`inject` / `restore`)+ 断言还原后与原文字节相等。
- **env 类改动优先用真旋钮做负向验证**:如 `QD_ON_DEMAND_TOOLS=actor` 复跑,精确 FAIL 对应判据即可,无需改文件。
- **护栏判据要可证伪**:新增判据后跑一次"注入缺陷 → 必须 FAIL → 还原 → 必须 PASS"。

---

## 八、缺陷与待改进(2026-10-11 收口后)

> 改进路线 A~M 的收口结论(状态/裁定/验收数据)见**附录 A**;关键裁定见**附录 B**。
> 此处只列**当前仍是缺陷/缺口**的项,按影响排序;✅ 已收口的项只留一行存目。

| # | 问题 | 影响 | 现状(路线) |
|---|---|---|---|
| **D-1** | 因子层未闭环:`get_factor_weights` 恒返回空 | 权重闭环只到 domain,因子层无反馈 | ⬜ 消费端已接线(A1a)但 factors 输出名("多头:xxx")与维度名不匹配 ⇒ 恒走回退常量;待 A1b 统一命名 |
| **D-2** | `cron/` 被**旧系统**反向依赖(`agent_smolagents/cron/cron_tools.py` import `app.agent.cron`) | 删旧系统会直接断掉定时任务;是迁移的前置阻塞 | ⬜ 未做 |
| **D-3** | `tools/preselect_stats.py` `_NOT_TOOLS` 只登记 2 个新名 | `empty_rate`/`record_empty_face`/`record_selected`/`report`/`reset` 仍在工具面里,每轮白下发 schema | ⬜ 未做(2026-10-11 复核仍真) |
| **D-4** | 会话上下文不跨进程持久化 | 重启/发版即失忆 | 🔒 **裁定不做、转观察**(理由见附录 B.1;观察指标=断片次数/用户重问率) |
| **D-5** | 能力评测的样本与人工基线未铺开 | 标尺可信度不足 ⇒ 调优归因噪声大 | 🟨 标尺已立(C,首跑 71.7);缺真实样本替换种子题 + 双人标注(agreement ≥80% 未验) |
| **D-6** | `agents/` 2 py 死代码 | 0 引用 | ✅ 已清 |
| **D-7** | `tracing.py` 读 3 个**已删** prompt ⇒ `prompt_sha` 3/5 恒 missing | 静默降级,复盘时对不上版本 | ⬜ 未做(仍读 plan_system.txt / code_agent.yaml / intent_classifier.txt) |
| **D-8** | 观测计数器是**进程内**的,重启归零 | 拿不到长期趋势 | ⬜ 未做(K 的 pairs 落盘只覆盖工具配对) |
| **D-9** | `admission.json` 的 `doc` 字段全空 | `func_overlap` 的"文档共同词"分支永不命中 | ⬜ 待定(admission.json 是本地文件不入工作副本,本仓无法复核) |
| **D-10** | 发布工程:归档/准入件不在版本控制 | 干净 clone 缺件 | ✅ 收口(**裁定不做入库**,`--with-env` skip 兼容;见附录 B.2) |
| **D-11** | `run_skill` 白名单硬编码在 `skill_run.py` | 违反"规则不在代码里";加执行型技能要改代码 | ⬜ 未做(技能变多后下沉为各技能目录声明文件) |
| **D-12** | `_skill_seen` 无 LRU 淘汰 | 长跑内存增长点 | ⬜ 未做 |
| **D-13** | `app/agent/utils` 与 `app/utils` 同名 | 长期雷(依赖 sys.path 顺序) | ⬜ 未做(建议改名 `qdutils`,涉 9 处) |
| **D-14** | `weight_feed` EMA 参数(α、MIN_SAMPLES=5)未经真实样本校准 | 慢调强度是拍的 | ⬜ 待攒样本(F 项范畴) |
| **D-15** | judge 未校准;LLM 网关可用性未端到端验证 | 阶段 B 不敢开 | ⬜ 未验证 |
| **D-16** | 工具预选底座仍是启发式(token 倒排 + 域词典) | 同义问法/新黑话("打板""埋伏")召回差 | 🟨 K 共现权重已落地;真实召回 ≥95% 待配对 >50 跑批 |
| **D-17** | 超大技能靠字数截断出首段 | 截断点不是语义 ⇒ 首段未必是最该看的 | ⬜ I 未做(建议 ≥3k 字技能写 300~500 字 TL;DR 头部) |
| **D-18** | 分发降本未达标:成本比 **0.889 > 0.40** | 重活分发只省 11%(B 验收线未过) | 🟨 **空结果纪律已接线(2026-10-12)**:① `qd_system.txt` §重活分发新增空结果三步纪律(直接交付/只允许再派一次 run_skill/禁止主对话重跑);② `_dispatch_card` 补「空结果=结论已成立」;③ `market_screener.run()` 空分支**不再丢弃 bundle**,改回 `pool_size/hard_blocked/hard_reasons/missing/mood_regime/themes`(实测 pool=10、hard_blocked=10、原因=涨停封板/ST/低价<2元)——原先只回 `analyzed:[]` 模型无从下笔,只能回退硬跑。**待重跑** `qd_dispatch_ab` 验成本比 |
| **D-19** | 多轮**递进**会话按平面进闭环 | 上游误判的锅被 `weight_feed` 分摊给下游技能 ⇒ 权重层系统性污染 | ⬜ M 方案已定未施工(三件:链总结节点 / claims `depends_on` 沿链传播 / 归因判定用例) |
| **D-20** | 金例不"金":选例纯相似度+标签加权,低分不淘汰;检索词面回退 | few-shot 增益不稳(首轮 +2.4 里单题贡献 +53.1) | 🟨 E 链路已通;待 C 出分后做高分选例+低分淘汰+接 embedder |
| **D-21** | 关键参数全是拍脑袋初值（temperature/max_tokens/step/阈值） | 调优无依据 | ⬜ F 未做（成本 SLO **已实现**，见下注） |

> **2026-10-11 代码实证补正（文档滞后修正，二次校准）**：原 D-21「成本 SLO 未做」不准确，但也不是「已达成」——真相是**机制全建、没接线**：
> `utils/budget.py`（提智方案阶段 0.9）三条预算线（`AGENT_RUN_TOKEN_BUDGET` 200k / `AGENT_RUN_TOOL_CALL_BUDGET` 60 / `AGENT_DAY_TOKEN_QUOTA` 2M）+ `check_budget()` / `budget_wrapup_hint()`（超限软收尾提示）+ 校验环计数器面板（`scripts/weekly_panel.py` 读 `traces/panel.jsonl`）。
> 但 `check_budget` / `budget_wrapup_hint` / `add_day_usage` / `tokens_from_agent` **全仓零消费方**（仅 `tracing.py` 做 panel bump、weekly_panel 读面板）——预算超限今天**不会**触发收尾。典型的「声明了没接线」，接进 `qd_agent._run_inner` 才算 ✅。
>
> **2026-10-12 已接线 ✅（P0 修复，起因见下）**：`QDAgent._account_budget()` 挂在 `after_step`
> （埋点之前），按**请求**累计 `_run_tokens` / `_run_steps_total` / `_run_tool_calls`，超限抛
> `LimitsExceeded(hint)` → `base.run` 捕获返回 `("LimitsExceeded", 提示)` → `_run_inner` 因文案
> 非空原样交给用户（**软收尾、不硬崩**）。记账源 = 框架原生 `_last_request_tokens`。
> **重试轮只归零 `_steps_taken`、成本不归零**（否则空回复轻推 / 溯源重写可无限续杯）。
> ⚠️ `tokens_from_agent` / `tool_calls_from_agent` 仍为零消费方——它们 import 已退役的
> `smolagents.memory`，是死代码，新记账不走它们。
>
> **起因（实测）**：CLI「明天买什么股」一轮 **15 次 LLM 调用、≈40 万 token、零产出**。
> 模型陷入「元工具发现环 + 占位探测」（`read_skill/search_tools/list_tools/...` 11 次，
> `bash echo probe` 4 次，数据工具仅 5 次）。
> 而**唯一刹车只有 step_limit=21**（`AGENT_MAX_STEPS=7 × QD_STEP_SCALE=3`），对 token 毫无约束力。
>
> **2026-10-12 订正：真近因已定位并修复**（原判「`run_skill` 被 `on_demand_tools` 降级、
> 首轮不在工具面」**不准确** —— 实测它的 schema 一直在下发的 37 个工具里，模型看得见全参）。
> 真正让模型认定「工具不存在」的是 `ToolProvider.search()` 的**整串子串匹配**：
> 模型用自然语言短语检索（`run_skill 执行技能流水线`），`整串 in "run_skill"` 恒 False ⇒ 零命中；
> 而同一次 run 里 `list_tools(domain='all')` 却又列出了 `run_skill` —— 自相矛盾把模型搞懵，
> 于是转 `bash echo` 占位。已修（`tools/base.py::search`）：**整串匹配优先，仅当零命中时**
> 降级为「空格/中英标点分词 OR」兜底（纯中文长串按 3 字滑窗切词），既有精确检索行为不变。
> **同一提问复测：15 次 / ≈400k token → 5 次 / ≈94k token，模型第 4 步直接调用
> `run_skill(market_screener)`，元工具/占位循环完全消失**（token 降 ~76%）。
> 顺带实证的其他滞后项：I 的 TL;DR 只有 market_screener（3 处）/strategy_debug（1 处）写了，auto_* 四个未写；E 的 embedder **已接线**（`case_memory._get_embedder()` 走 `EMBEDDING_*`），并非「纯词面回退」。

---

## 九、配置说明(`QD_*`)

| 变量 | 默认 | 作用 |
|---|---|---|
| `QD_ACCOUNTABILITY` | 1 | 追责总开关(=0 关) |
| `QD_CLAIM_JUDGE` | - | 阶段B LLM judge(默认关) |
| `QD_TOOL_PRESELECT` | 1 | 工具预选总开关 |
| `QD_PRESELECT_GATE` / `QD_PRESELECT_HINTS` / `QD_PRESELECT_MAX` | 1 / "" / 8 | 域特征闸门与预选上限 |
| `QD_TOOL_TIER_MODE` | tiered | 分级口径 |
| `QD_EXTRA_CORE_TOOLS` | "" | 追加必注入工具 |
| `QD_ON_DEMAND_TOOLS` | `actor,run_skill` | **反向旋钮**:把必注入工具降级为按需 |
| `QD_MAX_ACTIVE_TOOLS` / `QD_AUTO_ACTIVATE_MAX` | 12 / 8 | 同时激活上限 / 自动激活上限 |
| `QD_CAPABILITIES` | 1 | 三级能力层总开关 |
| `QD_TOOL_PROBE_LIVE` / `QD_TOOL_PROBE_TTL` | "" / 600 | 启动期可用性探测 |
| `QD_CONTEXT_WINDOW` / `QD_COMPACT_WINDOW` | 65536 / - | 上下文窗口(统一由 `_context_window()` 读;`QD_COMPACT_WINDOW` 为旧名兼容) |
| `QD_COMPACT_NOTES` | - | 压缩摘要提示词(内置 4 条压缩纪律) |
| `QD_COMPACT_RATIO` | 0.55 | 压缩触发阈值(占窗口比例;显式配 `QD_COMPACT_THRESHOLD` 优先) |
| `QD_COMPACT_THRESHOLD` | - | 压缩触发绝对字符阈值 |
| `QD_STEP_LIMIT` / `QD_STEP_SCALE` / `QD_EVAL_STEP_LIMIT` / `QD_EVAL_TIMEOUT` | - / 3 / 8 / 240 | 步数与超时 |
| `QD_WEIGHT_TTL` / `QD_FEED_MIN_SAMPLES` | 600 / 5 | 权重快照缓存 / 慢调最小样本 |
| `QD_RESOLVER` / `QD_RESOLVER_CLARIFY` | 1 / 1 | 实体解析总开关 / 只反问 vs 只解析 |
| `QD_PROVIDER_MAP` / `QD_CODE_VERSION` | - / unknown | 供应商映射 / 版本标识 |
| `QD_PRESELECT_PAIRS_FILE` / `QD_PRESELECT_COOC_FILE` / `QD_PRESELECT_MIN_PAIRS` | - / - / 50 | K:任务→实际调用工具配对落盘 / 共现权重文件 / 加权生效最小样本 |
| `QD_BENCH_DIR` | - | 评测与对照报告存档目录(qd_bench / dispatch_ab) |
| `QD_DISPATCH_COST_MAX` / `QD_DISPATCH_QUALITY_TOL` | 0.40 / 5 | B 对照判定阈值(成本比上限 / 质量降容忍);`--rejudge` 可改不重跑 |
| `QD_AB_ARMS` | `dispatch,inline` | 分发 A/B 对照臂顺序(`scripts/qd_dispatch_ab.py` 专用旋钮) |
| `CASE_MEMORY_ENABLED` | 1 | 案例记忆总闸（⚠️ 非 `QD_` 前缀；关=存/取/回填全链路停） |
| `AGENT_RUN_TOKEN_BUDGET` / `AGENT_RUN_TOOL_CALL_BUDGET` | 200000 / 60 | 单 run 成本硬预算（`utils/budget.py`）**2026-10-12 已接线**：`QDAgent._account_budget()` 每步记账，超限抛 `LimitsExceeded` 软收尾 |
| `AGENT_DAY_TOKEN_QUOTA` | 2000000 | 单用户日 token 配额 ⚠️ **仍未接线**（`add_day_usage` 尚无调用方） |
| `QD_MAX_OBSERVATION_CHARS` | 30000 | observation 字符闸门（2026-10-12）。上游 `MimocodeAgentConfig` 把它设为 **0（关闭）**，理由是 MiMo 自带工具自我限流；但**本项目 102 个金融工具走 `FnToolAdapter`、输出无上限**，`tools/base.py` 全程无 truncate ⇒ 继承 0 = 放弃唯一闸门。设 `0` 退回上游行为 |
| `QD_RUN_STEP_CAP` / `QD_RUN_STEP_GRACE` | 0 / 3 | 单请求**累计步数**上限（含空回复轻推轮 + 溯源重写轮）。`0` = 自动取 `step_limit + grace` |

> 共 **36** 个运行时 `QD_*` 配置(下表全列;`QD_COMPACT_*` 已展开为 4 项;grep 全仓含 `tmp/` 调试件的 `QD_NATIVE_FS_TOOLS` 共 37,非运行时配置不计入)+ `CASE_MEMORY_ENABLED` 等非 QD_ 开关。
> ⚠️ 上下文窗口**统一由 `QDAgent._context_window()` 读**:`QD_CONTEXT_WINDOW` > `QD_COMPACT_WINDOW`(旧名兼容)> `OPENAI_MAX_TOKENS` > 32768,不再多个 env 名打架。
> ⚠️ 下列**非 env** 开关在 `QDAgentConfig`(继承自 mimo),不在上表:`AntiHackGuard` **默认开**(`antihack=AntiHackConfig(enabled=True)`,`qd_agent.py:234`);`wrap_up_hint` **保持关**(与 `<context_usage>` 二选一,`qd_agent.py:319`);`compaction_context_window` / `compaction_threshold_tokens` 由 `_context_window()` / `_compaction_threshold()` 派生(§3.1)。

---

## 十、回归基线

| 项 | 实测 |
|---|---|
| `compileall app/ scripts/` | ✅ 通过 |
| `qd_smoke.py` | ✅ **248 项**(2026-10-10;test_29 单实例 / 30 导入健康 / 31 启动裸名 / 32 技能注入 / 33 技能分发+计划校验 / 34 案例记忆接线 / 35 半死代码归档 / 36 能力评测 / 37 分发对照 / 38 分发卡片 / 39 权重乘子);本机 `--with-env` **245/248 · 0 fail · 3 skip**(3 skip 均为 del/ 归档缺件) |
| `qd_prompt_eval --validate` | ✅ 10 条任务 / 35 项断言(含规划 5 题);R3 后 plan_* 断言 **17/19**、未过任务 5→2、旧题 31/31 零回归 |
| `qd_bench` | 25 题五类各 5;真实端点首跑 **71.7**(2026-10-10,`bench_20261010_real_first.json`;区分度 Δ72.5) |
| `qd_dispatch_ab` | 首跑成本比 **0.889**(未达 0.40,见 D-18)、质量Δ +0.5(2026-10-10,`dispatch_ab_20261010_210328.json`) |
| 工具面 | 注册表 **102**(工具 62 + 能力层 40);必注入 **22** / 按需 **83** |
| schema 体积 | 全量 **32,703 字符** → 必注入 **7,912 字符**(-76%) |
| 技能 | **6** 个目录,其中可执行(含 `run.py`)**2** 个 |
| `formatters/` | 不在生产包内(已归档 `del/agent_formatters_20261001/`,smoke 有防复活断言) |

⚠️ smoke 输出里的 Traceback 多来自 `chain/store.py save_tree` 在无 `DATABASE_URL` 时的 fail-open ERROR
(smoke 不加载 `.env`)-- 已知、非故障,别误判。

**跑法**：`cd backend_api_python && python app/agent/scripts/qd_smoke.py`
（**不要用 `-m`**，原因见 §7.1）

---

## 附录 A：改进路线 A~M 收口记录（2026-10-11 并入）

> 来源：`agent差距与改进路线.md`（2026-10-02 立档 · 2026-10-11 收口），原件已移入项目根 `del/agent_roadmap_20261011/`。
> 并入口径：只保留**当前态需要的结论、裁定与验收数据**；逐日施工过程不进本文（本地即运行、不做 git 入库）。
> 状态：✅ 完成 · 🟨 部分落地/待验 · ⬜ 未做 · 🔒 裁定不做。
> 来源标注：2026-10-11 · 任务「路线收口并入设计文档」· OpenClaw agent。

### A.1 状态总表

| # | 项 | 优先级/类型 | 状态 | 一句话收口 |
|---|---|---|---|---|
| A | 规划质量无代码强保证 | P1 架构 | ✅ | 三选一选定 ①+②：`_plan_lint` 轻校验 + prompt 规划 5 题；③ plan_linter 按需不预做 |
| B | 执行型技能子 agent 分发通道 | P1 架构 | 🟨 | 机制三条 + 测量器 + 分发卡片全齐；成本比 0.889 未达标（根因见 D-18） |
| C | 能力级评测体系 | P0 数据 | 🟨 | 标尺已立并首跑 71.7；真实样本与双人标注未铺开 |
| D | 会话上下文跨进程持久化 | P1 架构 | 🔒 | 裁定不做、转观察（附录 B.1） |
| E | Few-shot 金例注入 | P2 提智 | 🟨 | 链路通，首轮 +2.4；选例非金例；**embedder 已接线**（`EMBEDDING_*` 配置即生效） |
| F | 参数精调 | P2 调优 | ⬜ | 5 参数无扫描 |
| G | 胜率反哺 → 排序权重 | P2 提智 | ✅ | 乘子化 + smoke 五断言，冷启动 bit 级一致 |
| H | 成本/时延 SLO | P2 架构 | 🟨 | `utils/budget.py` 机制全建；**2026-10-12 token/工具/累计步数三线已接线**（`QDAgent._account_budget`），日配额线仍未接线——见下注 |
| I | 超大技能 TL;DR 头部 | P2 内容 | 🟨 | market_screener 已有 TL;DR 形态、strategy_debug 部分；auto_* 四个未写 |
| J | 发布工程收尾 | P1 工程 | ✅ | `--with-env` 落地；入库两项裁定不做（附录 B.2） |
| K | 工具预选可学习 | P3 提智 | 🟨 | 机制落地、冷启动一致；真实召回 ≥95% 待跑批 |
| L | 备选路线 B（MiMo-Code 大脑） | P3 战略 | 🟨 | 存档未采纳，重启条件见附录 B.3 |
| M | 多轮递进会话闭环结构化 | P2 架构 | ⬜ | 方案已定（三件）未施工 |

### A.2 各项收口要点

**A 规划质量** ✅
- 落地：① `qd_agent._plan_lint` run 收尾查 `update_plan` 残账（`in_progress`/`pending` → 落 trace 告警，**只告警不拦截**，smoke test_33 锁）；② `prompt_tasks.yaml` 规划类 **5 条**：plan_update / plan_no_residue / plan_no_overhead / plan_dispatch（兼作 B 的“派活率”探针）/ plan_acceptance（收口必须交代覆盖面与数据缺口）。
- 验收②实证（两次改动 diff）：R1→R2 题面「今天」→「最近一个交易日」⇒ plan_acceptance 1/5→4/5；R2→R3 改 `qd_system.txt` §重活分发（分步任务必须主对话 `update_plan`、不整体丢 task 子代理）⇒ plan_* 断言 **13/19→17/19**、未过任务 5→2、旧 5 题 31/31 稳定、对 baseline 零回归，plan_no_overhead token 99.5k→37.2k。
- 遗留：2 个 token 预算红（plan_update 418k/250k、plan_dispatch 246k/150k）内容质量均过 → 移交 F/H（D-21）。

**B 执行型技能分发** 🟨
- 机制：① `run_skill(name, args)`（白名单 `skill_run.EXECUTABLE_SKILLS`、子进程 600s 超时击杀、产物落 `tmp/skill_output/`、截断预览 ≤20k + `full_path`）；② `qd_system.txt` §重活分发；③ 产物约定 → 上下文 O(摘要)。
- 冗余收敛：执行型技能命中不再注方法论正文，改注**分发卡片**（~280 字符）；`read_skill` 兑底保留（smoke test_38 锁）。
- 测量器：`scripts/qd_dispatch_ab.py`（对照臂仅摘 `run_skill`、**不改 prompt**；成本比 + 质量差一键判定；`--rejudge` 改阈值复核不重跑）；smoke test_37 锁判定数学与对照臂构造。
- 首跑（screen_01，step-limit30，2026-10-10）：dispatch 163,980 tok / 7 步 / 314.6s / q86.7  vs  inline 184,557 / 7 / 231.1s / 86.2 ⇒ **成本比 0.8885 ✗（验收 ≤0.40）· 质量Δ +0.5 ✓**。
- 根因：`run_skill` 被调用（trace 1849，52.5s 成功）但返回**空结果**（`analyzed: []`，盘后无候选）→ 模型回退主对话硬跑 6 个取数工具 → 只省 11%。
- 测量器修过的三坑（别再踩）：漏 `load_dotenv`（“先跑臂必炸”假象）、RetryError 报错文本误当答案打分、臂失败 verdict 假通过。

**C 能力级评测** 🟨
- 标尺：`scripts/qd_bench.py` + `bench_tasks.yaml`（25 题，五类各 5：个股分析/选股/复盘/交易确认/通用问答）；打分 **coverage 30 / grounding 25 / claims 20 / structure 15 / human 10**，全确定性机检（同输入同输出）。
- 首跑（2026-10-10 真实端点）：**71.7/25 题**（coverage 81.5 / grounding 86.8 / claims 38.0 / structure 62.6；分类均分：个股 81.5 / 选股 66.8 / 复盘 48.6 / 交易 81.6 / 问答 81.7；强弱答案区分度 Δ72.5）。
- 修过三个真 bug：`cmd_run` 对 `_AgentResponse` 做 `len()`（答案被换成错误串）、`import-traces` 漏 `load_dotenv` + `DISTINCT`→`GROUP BY` + psycopg2 字面 `%%`、bench_/ab_ 会话污染过滤。
- 遗留：seed 种子题需用 `import-traces` 换成 20~50 条真实任务（baseline 人工补）；两人各标一轮验 agreement ≥80%。

**D 会话上下文持久化** 🔒 → 裁定与理由见附录 B.1。

**E Few-shot 金例** 🟨
- 链路：写入 `_finish_collector` → `_record_case` → `case_memory.record_case()`；注入 `_prefetch` → `retrieve_cases()` + `format_case_injection()`（`CASE_MEMORY_ENABLED` 总闸，fail-open）。taxonomy 六类，同型标签 ×1.2 只调序不过滤。机制细节见 §3.11。
- 首轮 A/B（2026-10-10）：注入关 55.6 → 开 **58.0（+2.4，升 3 平 6 降 1）**；单例注入最大 **240 tok ≤ 500 ✓**。
- caveat：qa_04 单题贡献 +53.1、6 题结构性免疫、case 全 pending；检索是词面回退（sim=1.0 同题才命中，embedder 未生效）。

**F 参数精调** ⬜：temperature=0.1、max_tokens=16384、step=`AGENT_MAX_STEPS`×3、技能阈值=2、预取相关度闸门=2 全是拍脑袋初值。方案：以 C 为标尺做 5 参数单变量扫描（每次只动一个），结论写回 `.env.example` 注释（含“±1 档掉几分”）。

**G 胜率反哺** ✅
- `qd_service._rank_skills` 排序分乘子 `score × (0.5 + 0.5×weight)`（weight∈[0.5,2.0] ⇒ 乘子∈[0.75,1.5]）；**阈值仍看原始词典分**（只调序不过滤，与 hint_text / case 同型标签同款 fail-open 哲学）。
- smoke `test_39` 五断言：冷启动乘子=1.0 位序与纯词典分一致 / 同分对高权重排前 / 权重对调对称翻转（排除名字序假象）/ 低词典分×高权重可反超（超出 tie-break 语义）/ 阈值源码防回退。
- 遗留：真实权重数据的排序影响待 `weight_feed` 攒样本后观察。

**H 成本/时延 SLO** ⬜：现只有步数预算（`step_limit`）。方案已定未施工：① run 级 `QD_TOKEN_BUDGET`/`QD_COST_BUDGET`，超限复用已验证的“截断收尾”机制；② 时延软 SLO，超时降级为“已有数据出结论 + 注明未完成部分”（比空结果好）；预算事件落 trace。验收：预算超限返回可读降级文案而非异常。

**I 超大技能 TL;DR** ⬜：渐进式加载已上线，但 `auto_stock-analyze`（5.2k 字/6 节）等仍靠字数截断出首段。方案：每个 ≥3k 字 SKILL.md 加 300~500 字 TL;DR 头部（何时用 / 三步主流程 / 三条硬约束），`_skill_block` 优先注入 TL;DR+目录；**内容由技能作者写**（方法论取舍是业务判断，代码不猜）。验收：注入块 ≤1200 字符且覆盖三要素。

**J 发布工程** ✅ → 裁定见附录 B.2。落地件：smoke 断言基数 239→**248**；`--with-env` 模式（6 个真实环境探针：pg 连库 / stock_directory / archive_* 文件存在性 / posix_shell / tool_deps import，缺件 fail→skip；默认模式与 CI 逐字节一致）；归档目录磁盘缺失由 skip 兜住。

**K 工具预选可学习** 🟨 → 机制见 §3.12。实测现状：配对仅 44 对（<50，**正确退化中**）；代理评测 8 任务“任务级全中”0→0.375；构造加权面逻辑达标 1.0。⚠️ smoke 曾向配对文件追加演示数据（已隔离 `.bak`），首次周级跑批建议 `--db`。

**L 备选路线 B 存档** 🟨 → 见附录 B.3。

**M 多轮递进会话闭环结构化** ⬜
- 问题：递进会话（新闻 → 判大盘 → 选股 → 分析）按平面进闭环，② 判错时 ③④ 连带错却被记成三个独立 miss，`weight_feed` 把上游的锅分摊给下游技能 ⇒ 权重层系统性污染（与 D 同源不同入口：D 是“旧数据进新决策”，M 是“上游错记成下游错”）。
- 方案三件：① **递进链总结**——run 收尾落结构化节点 {结论、前提（引用了哪些上游结论）、claim_ids}，会话链存链式结构而非平面列表；注入侧以**链摘要**替代裸历史（只带结论与前提、不带工具数据），兼作 D 预案的受控续承载体；② **claims 依赖标注与沿链传播**——claim 增 `depends_on`，T+N 上游 miss ⇒ 下游标 `inherited-miss`（不重复惩罚下游技能/不重复喂数），上游 hit、下游 miss 才归因下游；③ **判定用例**——四轮递进链 + 一组平面多轮（无依赖）对照。
- 验收：递进链归因用例正确率 100%（② 错只罚 ② 链路，③④ 不背锅）；平面对照组行为与现状完全一致（不误伤）；链摘要注入块 ≤1K 字符/轮。

### A.3 验收数据与归档索引

| 产物 | 位置（`app/agent/` 下） |
|---|---|
| prompt 回归三轮报告 | `scripts/prompt_eval/round{1,2,3}.json`（运行时产出，不在工作副本） |
| 能力评测首跑 | `bench_20261010_real_first.json`；⚠️ `latest.json` 现指向单题验证 run，权威首跑以本件为准 |
| 分发 A/B 首跑 | `bench/dispatch_ab/dispatch_ab_20261010_210328.json` |
| 金例 A/B | `runs/E_A_caseoff.json` / `E_B_caseon.json`、`preds_E_{A,B}.jsonl`（工作副本内现存 `scripts/bench/preds_E_*.jsonl`） |
| 半死代码归档 | 项目根 `del/tools_returns_contract_20261010/`（README 含判据/回滚/触发条件；smoke test_35 防回流） |
| formatters 归档 | 项目根 `del/agent_formatters_20261001/`（smoke 有防复活断言） |
| 本路线原件 | 项目根 `del/agent_roadmap_20261011/agent差距与改进路线.md` |

> 环境备忘（收口期）：活环境 = 项目根 `.venv`（系统 Python 裸）；smoke 跑法见 §10；`qd_bench` 的 `import-traces` 已修通可导真实样本。

### A.4 剩余工作施工顺序（收口后重排）

1. **还验收债**：~~B 的 prompt 空结果纪律~~（**2026-10-12 已接线**：`qd_system.txt` §重活分发三步纪律 + `_dispatch_card` 空结果提示 + `market_screener.run()` 空分支自带口径，见 D-18）→ **剩最后一步：重跑 `qd_dispatch_ab` 出数**验成本比是否降到 0.40 以下；
2. **数据侧**：C 真实样本替换种子题 + 双人标注验 agreement（D-5）→ 同时喂 E 的高分选例/低分淘汰/接 embedder（D-20）与 K 的 ≥50 对后 `--db` 跑批复核；
3. **结构侧**：M 递进链三件（D-19）→ 之后再评估 D 是否需要最小受控续承；
4. **预算与调优**：H 成本/时延 SLO + F 五参数扫描（D-21；A 的 2 个 token 预算红一并结）；
5. **内容活**：I 技能 TL;DR 头部（半天量级，技能作者写）。

远期/按需：L（触发条件见 B.3）、D-11 白名单下沉、D-13 `utils` 改名、D-2/D-7 旧系统与 prompt 遗留。

---

## 附录 B：关键裁定记录（忘掉会返工）

### B.1 D 裁定（2026-10-10，用户判定）：会话上下文不做裸跨进程恢复

原方案（messages 全量快照落 PG、重启恢复）**暂不做**，转观察。理由两条：

1. **污染闭环问责**：恢复旧消息会突破证据边界——`_observation_corpus()` 从 `self.messages` 收 grounding 语料 ⇒ 旧轮工具输出里的**过期数字**把“本轮没取到的数”洗成可溯源 → 陈旧结论过溯源门 → `record_decision` 抽出被污染的 claim → T+N 判定归因进 `weight_feed` EMA ⇒ 闭环学到“用旧数据也能对”的伪规律；
2. **需求已被受控通道覆盖**：完整问答三处落存储（`qd_agent_messages` role/content 全量、`qd_traces` user_query+answer+工具链、chain intake decisions/claims）——**问责链数据零损失**；跨进程对话连续性已有受控通道（PostgresMemory 记忆近史：`_relevance≥2` 闸门 + 200 字/行截断 + 6 条上限，PG 共享跨 worker 可见）——只带“聊过什么”，不带“数据是什么”。裸恢复 = 把受控记忆换成不受控全量回放。

- 现状：重启真正丢的只剩“同 session 中段的工具中间结果”（如上一轮全量筛选明细），受控替代物已在（`run_skill` 的 `full_path` 产物文件、重跑工具）。
- **观察指标**（何时重新评估）：真实端点上因进程重启 / 多 worker 路由导致的会话断片次数、用户重问率。
- **若到做那一天（预案）**：只做最小受控续承——恢复“指代指针”（上一轮标的/结论一句话），走记忆近史同款闸门注入，且代码层硬隔离（恢复块标记后不进 `_observation_corpus`、不参与 claims 提取）。
- 关联：多轮**递进**会话的结构化问题见 M（D-19），两者同源但 M 不依赖 D。

### B.2 J 裁定（2026-10-11，用户裁定）：本地即运行，不做 git 入库

代码源起远程服务器（历史上带 git），现为**本地运行模式**：本地工作副本即唯一事实源，agent 工作流**不含任何 git 指令**（与 AGENTS 规则同源）。

- 推论：J 的入库两项（admission.json / del 归档）**不做**；原“干净 clone + 无 PG → smoke 全绿”验收由 `--with-env` skip 机制天然满足（缺件→skip→exit 0，不依赖版本控制）。
- `admission.json` 因此是**本地文件**，不在工作副本里（缺它时能力层按空清单处理，注册 0 个、不抛错）；审计件保全另计为本地备份需求。
- 归档目录（`agent_formatters_20261001` / `tools_returns_contract_20261010`）磁盘缺失时由 `--with-env` skip 兜住。

### B.3 L 存档：MiMo-Code 大脑（备选路线 B）

已评估**未采纳**(2026-10-01 定稿路线 A = white-box):MiMo-Code 本体有持久记忆 / 上下文重建 / dream,但金融工具需 MCP 化、行为偏软件工程。

> 澄清(2026-10-11,避免混淆两条 MiMo-Code 通道):mimoagent 同时提供
> ① **native** `MimocodeAgent`(`agents/mimocode/`,lowercase 目录 + task/actor/compact/antihack)——
> **已是本届执行核基类**(§3.1);② **blackbox** `agents/blackbox/mimocode.py`(在容器里驱动真
> MiMo-Code CLI,`mimo run --format json` headless)——**这才是路线 B 的集成路径**,本节指后者。

- **重启条件**：出现“长程编码类子代理”需求（策略代码生成 / 回测工程化），或路线 A 的规划/记忆短板（A/D）修完仍不达标。
- **集成路径已探明**：`mimoagent.agents.blackbox.mimocode` 可驱动真 MiMo-Code CLI（`mimo run --format json` headless），NDJSON 事件流可转 SSE。

### B.4 分发/加载两层分工（2026-10-10 裁定，勿再合并）

知识型技能 → 注方法论正文（渐进式加载）；执行型技能 → 注**分发卡片**（`run_skill` 派活）。两层正交不冗余；执行型注正文 = 真冗余 + 行为风险（诱导主对话硬跑，与「重活分发」抢方向盘）。
