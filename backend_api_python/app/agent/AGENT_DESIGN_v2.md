# QuantDinger Agent 模块设计文档 v2（mimo 执行核）

> **本文只描述当前态**：设计思路、拓扑、逻辑结构、数据流、精巧处、易错点、缺陷清单。
> **变更历史不在本文** —— 看 git 提交记录；待施工的改进项看
> `backend_api_python/app/agent/agent差距与改进路线.md`（A~L 12 项，含验收标准）。
>
> 执行核：mimoagent（pip 包），编排层已删除，无自研 StateGraph。
> 旧系统 `app/agent_smolagents/` 保留作对照，**本文不描述它**。
> 本文所有数字均为 2026-10-02 实测（`tmp/_doc_facts.py` 可复现），非估算。

---

## 一、设计思路

### 1.1 要解决的问题

Agent 模块是 QuantDinger 的智能决策核心：接收自然语言输入（个股分析、全市场筛选、策略诊断、交易确认、通用问答），
在 **102 个工具** 的面上收敛出当轮最小可用工具集，跑 ReAct 闭环，输出结论，并把可证伪的断言留档、T+N 用真实行情判定。

三个硬约束决定了后面所有的架构选择：

| 约束 | 后果 |
|---|---|
| 工具面 102 个、全量 schema **32,703 字符** | 全量下发不可行 ⇒ 必须分级，且必须每轮现算 |
| 结论里的数字必须可溯源 | 需要 grounding gate（数字溯源拦截） |
| 用户会问"你上次说的对不对" | 需要追责四表 + T+N 真值判定 + 反哺行为 |

### 1.2 设计原则

| 原则 | 落地形态 |
|---|---|
| **最小工具面** | 三级分级 + 预选 + lint；必注入层 **22** 个 / 全量 **102** 个 |
| **fail-open 优先** | 预选空手有域兜底、未激活直调就地激活、能力层补位；宁多不可断 |
| **可追责** | `decisions/claims/resolutions` 三表 + T+N 判定 + 权重反哺 |
| **只提示、不过滤** | 权重/历史表现只做 hint，不做硬过滤（硬过滤与 fail-open 冲突） |
| **澄清优先** | `resolvers/` 挂在取数之前，歧义先反问，不猜默认值 |
| **领域中立** | 域走配置表 `qd_domain_resolvers`，代码里没有 `if domain == 'finance'` |
| **先量化再决定** | 拿不准的先装观测计数器，不写自动调参逻辑 |
| **规则不在代码里** | 决策规则外置（yaml / 参考文档），代码只做客观计算 |

### 1.3 四条关键取舍

1. **删掉自研编排**：旧版 StateGraph + 4 节点 + phase acceptance 全部删除，只保留 mimo 单 ReAct 循环 + 一个
   `update_plan` 清单工具。规划质量改由 prompt 纪律 + 收尾轻校验承载（§3.8）。
2. **分级是架构不是配置**：框架只在 init 算一次工具定义，我们 override `get_model_query_kwargs` 改成**每轮现算**，
   否则 `activate_tools` 激活了也不生效 —— 这是整套分层的前提。
3. **权重只提示不过滤**：硬过滤会让模型在真需要某工具时拿不到它。粒度也只到 domain —— resolutions 不带
   tool/skill 归因，硬按 `user_query` 反推"当时用了哪几个工具"是伪精确。
4. **接入组件前先问"它要几次 LLM 往返"**：本地计算（权重提示 0ms / 实体解析 95ms）与
   一整轮 LLM 生成（结论格式化 11.7s）是两类东西，评审阶段就要区分，不能等用户说"变慢了"再归因。

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
│  flask_app.py(SSE) ←→ message_queue.py(线程池) ←→ agent.py(Facade)      │
│  agent.py 装配期 warmup_tool_face()：把冷启动挪出消息路径                 │
│  start_cron_worker()：定时任务（★ 导入必须是 cron.cron_worker）           │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              会话层：QDAgentService（有状态，按 session_id 复用）         │
│   _resolve_query()（歧义→短路反问） → _prefetch()（记忆/RAG/技能注入）    │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   执行层：QDAgent（继承 MimocodeAgent）                   │
│                                                                          │
│  _run_inner 单循环：                                                      │
│   ⓪ _steps_taken=0 / current_plan=None  ← 预算与计划都是 per-run         │
│   ① _maybe_preselect_tools  ← 外置 plan + 确定性 lint + 域兜底           │
│   ② 注入 now / tool_map / tool_availability 到 system_template           │
│   ③ super().run()  → ReAct（每轮现算工具面 + 分层 fail-open）            │
│   ④ 空结果兜底（LimitsExceeded / Empty 分别处理）                        │
│   ⑤ grounding_gate：数字溯源拦截 + 独立预算重试                          │
│   ⑥ _plan_lint() 计划收尾轻校验（只告警）                                │
│   ⑦ _finish_collector → 追责入库 intake                                   │
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

### 2.2 目录结构（实测）

```
backend_api_python/app/agent/
├── agent.py              # 统一入口 Facade（装配 + warmup + cron 启动）
├── qd_agent.py           # ★ QDAgent：mimo 子类，全系统核心
├── qd_service.py         # 有状态会话服务（prefetch / 技能渐进式注入 / 会话去重）
├── flask_app.py          # Flask Blueprint（SSE）
├── cli.py                # CLI 入口
├── message_queue.py      # 统一消息队列（Flask/Cron 共用）
├── trace_collector.py    # TraceCollector → qd_agent_traces（root/skill/tool 三层）
├── mimo_boot.py          # mimoagent 依赖引导
├── constants.py          # 全局常量单一事实源（AGENT_MAX_STEPS 等）
├── domain_registry.py    # 跨层分类学注册表
├── log.py
│
├── tools/        [41 py]  工具层：顶层 17 + finance 21 + knowledge 3
│   ├── base.py               # ToolProvider / func_to_openai_schema / _is_tool_function
│   ├── availability.py       # 启动期可用性探测 → {{tool_availability}}
│   ├── tool_discovery.py     # search_tools / list_tools / activate_tools
│   ├── tool_preselect.py     # ★ 外置 plan 预选 + 确定性 lint（已声明 _NOT_TOOLS）
│   ├── preselect_stats.py    # ⚠ 观测计数器（**未声明 _NOT_TOOLS**，见 §8 D-3）
│   ├── skill_tools.py        # list_skills / read_skill / run_skill
│   ├── capability_tools.py   # search_capabilities / call_capability / list_capabilities
│   ├── memory_tools.py       # remember / recall
│   ├── filesystem_tools.py   # read_text / write_text / list_dir / run_python_file
│   └── finance/(21) knowledge/(3)
│
├── capabilities/ [4 py]  三级能力层（admission.json 准入 69 → 注册 40）
├── chain/        [13 py] 追责 + 评估 + 权重闭环 + 技能酿造
│   ├── claims.py             # 域判定 / 入库闸门 / Claim 提取（纯函数，零 LLM）
│   ├── intake.py             # record_decision 唯一入口
│   ├── account_store.py      # decisions/claims/resolutions 持久化
│   ├── resolver.py           # 阶段A 算数判定 + 阶段B LLM judge（默认关）
│   ├── weight_feed.py        # resolutions → qd_agent_weights（EMA 慢调，domain 粒度）
│   ├── weight_hints.py       # 权重**唯一消费口**（TTL 快照）
│   ├── evaluator.py          # 盘后 worker（旧评估 + 追责判定 + weight_feed）
│   ├── reset.py              # S2/S4 复位（dry_run 默认 True）
│   └── store.py / judge_stats.py / skill_brewer.py
│
├── skills/       [5 目录]  market_screener / strategy_debug（**含 run.py，可执行**）
│                           + auto_stock-analyze / auto_finance-analysis-stock
│                           / auto_finance-query-stock（markdown 型，酿造产物）
├── rag/[6] memory/[5] llm/[7] utils/[11] resolvers/[6] cron/[4] audit/[2]
├── prompts/      [3 txt]  qd_system.txt / skill_brew.txt / skill_revise.txt
├── scripts/      [5]      qd_smoke.py（回归 188 项）/ qd_prompt_eval.py
│                          prompt_tasks.yaml（8 任务）/ skill_run.py（执行型技能子进程入口）
│                          weekly_panel.py
├── agents/       [2 py]   ❌ 死代码（0 引用）
└── traces/, data/, tmp/   辅助目录
```

> **存活判定方法**：`grep -rnE "^\s*(from|import)\s+<pkg>\b"` 排除包内自引用。
> ⚠️ 只查 `from app.agent.<pkg>` 会**漏判** —— 本包统一用**裸名** import（`from chain.store import …`，见 §7.1）。

### 2.3 核心文件职责速查

| 文件 | 职责 | 关键点 |
|---|---|---|
| `qd_agent.py` | 执行核 | override `get_model_query_kwargs` 每轮现算工具面；`execute_action` 分层 fail-open |
| `tools/base.py` | 工具注册 | `_NOT_TOOLS` 模块级声明，防止管线函数被误注册成工具 |
| `tools/tool_preselect.py` | 预选 + lint | 加法先于减法；有补位则不得作废 |
| `tools/skill_tools.py` | 技能读写 + 执行分发 | `read_skill` 读方法论；`run_skill` 跑流水线（子进程） |
| `scripts/skill_run.py` | 执行型技能子进程入口 | 白名单注册表 + fn 白名单 + 参数校验 + 超时击杀 + 产物落盘 |
| `chain/claims.py` | 追责抽取 | 纯函数；闸门判不出来就拦（宁漏不脏） |
| `chain/weight_hints.py` | 权重消费 | TTL 600s 快照；**务必经此入口读权重** |
| `resolvers/bridge.py` | 澄清 / 实体解析 | 挂在取数**之前**；clarify 非空即短路反问 |
| `qd_service.py` | 会话与预取 | 技能渐进式注入（`_skill_block`）+ 会话级去重（`_skill_seen`） |

---

## 三、逻辑结构

### 3.1 执行核：mimoagent

**事实（读安装包源码得出）**：

| 位置 | 行为 |
|---|---|
| `agents/default.py:102` | `self._tool_definitions = registry.get_function_definitions()` —— **init 算一次** |
| `agents/default.py:197` | `get_model_query_kwargs()` 每轮返回**同一份** + `tool_choice="auto"` |
| `agents/base.py:255` | `model.query(messages, **kwargs)` ⇒ tools 进请求体，**每轮重发** |
| `agents/default.py:189` | `get_tool_context()` 返回 `{"env","model","agent":self}` ⇒ `update_plan` 能把计划写回 agent |

- **框架不做任何工具筛选/预选/检索**，选谁完全交给模型。
- 包内无 tool search / 按需激活 / 延迟加载（我们自己在 `tools/tool_discovery.py` 补的）。
- **我们能做分层的关键**：override `get_model_query_kwargs` 改成每轮现算。

### 3.2 工具面：三级分级 + 分层下发 + 按需降级

| 级 | 定义 | 实测 |
|---|---|---|
| **一级 必注入** | `tools/` 顶层 + mimo 原生/元工具 | **22**（其中 19 个来自注册表 + `web_search`/`format_result`/`update_plan`） |
| **二级 域内** | `tools/finance` + `tools/knowledge` | 域内优先于能力层 |
| **三级 能力层** | `capabilities/` 准入函数 | 准入 69 → 注册 **40**（29 项让位给域内） |

- 注册表总量 **102**（工具 62 + 能力层 40）；必注入 **22** + 按需 **83**（含 3 个非注册表来源）。
- **schema 体积**：全量 **32,703 字符** ⇒ 必注入 **7,912 字符**（**−76%**）。每轮请求都带这一整块，prompt cache 省钱但**不省窗口**。
- **"域内优先"的三处落实**：① `build_catalog_grouped` 目录分两段、能力层垫底并标注"仅当域内无覆盖时才选"；② lint **R0 能力层让位**；③ `apply_lint(capability_names=)` 排序垫底。

**按需降级旋钮**：`QD_ON_DEMAND_TOOLS`（默认 `actor,run_skill`）可把必注入工具降级为按需。
降级后仍拿得到，三条兜底：

1. **分层 fail-open**（`qd_agent.py::execute_action`）：已注册但未激活的工具被直调时**就地激活放行**，不会 `Unknown tool`；
2. **工具预选目录由 `_on_demand_names` 构建** ⇒ 相关任务首轮就能被点名激活，不产生"search→list→activate"三轮发现；
3. **`qd_system.txt` 的"重活分发"小节直接点名 `run_skill`** ⇒ 模型知道名字。

> ⚠️ 因此 `qd_system.txt` 里**不能**写"未激活直接调用会失败"——那是旧文案，与 fail-open 相反，会让模型绕回三轮发现。

### 3.3 工具预选：外置 plan + 确定性 lint

**为什么不是"让 LLM 裸选工具"**：裸选会让天气问题被塞 8 个股票工具、token +136%、净收益≈0。真根因是**预选输入被 prefetch 污染**（用了含历史个股记忆的增强文本）。

```
① 域特征闸门 has_domain_hint()   —— 纯字符串，成本≈0，不命中才跳预选
② 分级计算 cap/dom               —— ★ 必须在闸门之前算，否则兜底拿不到候选
③ build_catalog_grouped()        —— 域内段 + 能力层段
④ LLM 产出 plan {goal, deliverables, tools, step_budget, phases}（带 history_hint）
⑤ parse_plan() → ⑥ lint_selection()（不调 LLM）
     R0 能力层让位（域内已覆盖则裁）
     R1 域词典覆盖补位（加法，高置信）
     R4 零相关裁剪（减法）  R4b 整体作废
     ⚠ 有补位则不得作废
⑦ apply_lint()  —— 先加法后减法，清空则保留原面
⑧ _finish_preselect()  —— 空手时用 fallback_domain_tools 兜底
```

**实测收益（重任务）**：`stock_screen` 153,960 tok / 6 步 → 43,318 / 2 步（**−72%**）；
`trade_confirm` 100,150 / 6 步 → 34,976 / 2 步（**−65%**）。

**域兜底（不许落回裸工具面）**：`fallback_domain_tools(query, available, limit=4)`（纯词典、零 LLM）补核心子集 ——
「帮我看看大盘」→ market 补 4 个；「今天北京天气」「写个跑马灯」→ **0 个**（命中不了不硬塞）。

### 3.4 技能系统

| 环节 | 机制 |
|---|---|
| **选技能** | 词典分 + 范围信号（有无标的）+ 意图信号；权重仅 tie-break；`_FINANCE_GATE` 防"跑马灯"被注入选股技能 |
| **注入** | 渐进式：`_skill_block` = 正文首段（上限 2200 字符）+ `[技能目录]` + `[按需读取]` 路标 |
| **去重** | `_skill_seen`（per session）：二次命中只给一行指针，正文不再重复注入 |
| **读** | `read_skill(name)` 全文 / `read_skill(name, heading=)` 小节 / `read_skill(name, resource=)` 资源 |
| **执行** | `run_skill(name, arguments)` → 子进程 `scripts/skill_run.py`（仅执行型技能） |

**执行型技能分发**（`market_screener` / `strategy_debug`）：

```
主 agent
  └─ run_skill(name, arguments)  ──► 子进程 scripts/skill_run.py
        ① 技能名白名单（EXECUTABLE_SKILLS）  ② fn 白名单
        ③ inspect.signature 未知参数拦截（不执行就退出）
        ④ 执行 → 产物落 app/agent/tmp/skill_output/*.json
        ⑤ 回传 {ok, result(预览≤20k字符), full_path, elapsed_s}
```

三重收益：**超时可击杀**（600s）、**崩溃隔离**（不污染主进程）、**主上下文只拿摘要**（O(摘要) 而非 O(全量)）。

⚠️ 白名单目前仍是**硬编码在 `skill_run.py` 里**的字典 —— 与"规则不在代码里"相悖，
待执行型技能变多后下沉为各技能目录内的声明文件（详见 §8 D-11）。

### 3.5 追责闭环（`chain/`）

**四张表**（`migrations/agent_v4_trace.sql`）：

```
qd_agent_decisions    一次对话一行（闸门不通过 ⇒ 一行都不写）
qd_agent_claims       可证伪断言（direction / magnitude / level）
qd_agent_resolutions  T+N 判定结果（verdict / score / attribution / deviation）
qd_domain_resolvers   域→策略配置（enabled / horizon / judge 采样率）
```

**三条裁定**：① 不追责的不入库（`intake_gate` 前置，宁漏不脏）；② 慢调、容忍误判（方向对即 hit 0.75，
幅度差只记 deviation；但 `undecidable`/`data_missing` **不计权重**——常量污染 ≠ 误判）；③ 多域通用，v1 只开 finance。

**闭环**：

```
resolutions ──weight_feed.feed()──→ qd_agent_weights(layer='domain')
                                      │ EMA 慢调：α=clamp(n/50, 0.1, 0.5)
                                      ↓
                            chain/weight_hints.py（唯一读取口，TTL 600s）
                                      ├→ 工具预选 history_hint（只提示不过滤）
                                      └→ qd_service._skill_score 做 tie-break
```

**判定两阶段**：阶段 A 代码算数（取真实 K 线）；阶段 B LLM judge（默认关，`QD_CLAIM_JUDGE` 开）。

### 3.6 澄清优先（`resolvers/`）

`resolvers/bridge.py` 挂在 `_run_sync` 取数**之前**：`clarify_question` 非空 ⇒ **直接反问、不进执行**
（`QD_RESOLVER=0` 关整套；`QD_RESOLVER_CLARIFY=0` 退化为"只解析不阻断"）。
解析出的标的/领域注入上下文，并供技能选择使用。

⚠️ 副作用需接受：非交易日问「今天买什么股票好」会被反问。`QD_RESOLVER_CLARIFY=0` 可退让。

### 3.7 grounding gate

结论中的数字必须能在本轮工具输出（`_observation_corpus()`）里溯源；无法溯源的数字改写为"未获取"并说明缺失口径。
重试给**独立步数预算**（否则主轮耗尽后重试瞬死）。仍不过 → `status="grounding_failed"`。

### 3.8 计划质量轻校验（`_plan_lint`）

`update_plan` 的清单若收尾时仍留 `in_progress`/`pending`，通常是跑偏/被截断的信号。
`_plan_lint()` 在 run 收尾落 `plan_lint` trace 事件 + warning，**只告警不拦截**。
`current_plan` 是 **per-run** 的（与步数同生命周期，run 开头归 `None`）；它由 `UpdatePlanTool` 经
`context["agent"]` 写回（`update_plan.py:74`）。

### 3.9 观测计数器（只观测，不改行为）

| 模块 | 观测 | 阈值（写死在 docstring） |
|---|---|---|
| `tools/preselect_stats.py` | 空域占比 | >2% 补域词典 → >5% 放宽 GATE → >10% 查 prompt/模型 |
| `chain/judge_stats.py` | judge vs 规则锚点分歧率 | <10% 且样本>100 ⇒ 永远别开；>20% ⇒ 建人工校准集 |

设计约束：只观测，不自动改判定行为；原因标签走 `_empty_route()` **稳定归类**（gate/no_pick/lint_void/call_error/unknown），
不许存 `reason` 原文（改文案会断统计）；空域日志 keyword **`preselect-empty-face`**。

### 3.10 其余组件

| 组件 | 说明 |
|---|---|
| RAG (`rag/`) | BGE reranker + pgvector + PostgresFTS + 历史检索 |
| Memory (`memory/`) | 跨会话记忆（`remember`/`recall` 工具化） |
| LLM (`llm/`) | `LLMFactory` + `QDSkillAdapter`（技能加载器） |
| Capabilities (`capabilities/`) | 三级能力层，`admission.json` 准入，注册期 29 项让位给域内 |
| Cron (`cron/`) | ⚠️ 被**旧系统**反向依赖（§8 D-2） |

---

## 四、执行流程

### 4.1 单轮请求流程

```
用户输入
  │
  ▼
QDAgentService.chat()
  ├── 设 agent.last_user_query = 原始问题（★ 预选只认它，防 prefetch 污染）
  ├── ⓪ _resolve_query() → (info, clarify)
  │       └ clarify 非空 ⇒ 短路反问，直接返回
  ├── _prefetch() → (text, report)：记忆/RAG/技能渐进式注入，report 落 trace
  ▼
QDAgent._run_inner()
  ├── _steps_taken = 0 / current_plan = None     （per-run，不跨请求累积）
  ├── ① _maybe_preselect_tools：闸门 → 目录 → LLM plan（带 history_hint）→ lint → activate
  ├── ② 注入 now / tool_map / tool_availability
  ├── ③ super().run()  ReAct（每轮现算工具面；未激活直调 → fail-open 就地激活）
  ├── ④ 空结果兜底：LimitsExceeded→可读文案；Empty→轻推一次（独立预算）
  ├── ⑤ grounding_gate：数字溯源拦截，最多重试 N 次（独立预算）
  ├── ⑥ _plan_lint()：计划残账告警
  └── ⑦ _finish_collector → _intake_accountability（追责入库）
```

> ⚠️ **顺序有讲究，别随便挪**：resolver 在最前（歧义要在花钱调工具之前拦截）；
> weight_hints 在预选时读（要影响工具选择，就得在选之前）。

### 4.2 步数与预算语义

`step_limit` 是**单次 run 预算**，会话复用同一 agent ⇒ `_steps_taken` 必须每轮归零，否则轮次多了新消息一进来就
`LimitsExceeded` 且 `str(e)==""`（静默空结果）。**每个"附加轮"（空回复轻推 / grounding 重试）都给独立预算**。

### 4.3 盘后 worker

`agent.py` `start_eval_worker` → 每日 15:30 等 `post_market_done` → `auto_evaluate()`：
① 旧 `evaluate_pending()`（写 `qd_agent_traces`）；② `resolve_due_claims(limit=200)`（写 resolutions）；
③ `weight_feed.feed()`（写权重）。三者**独立 try，失败互不影响**；健康度看 `worker_health.last_weight_feed`。

---

## 五、数据流

### 5.1 请求数据流

```
用户问题
  → resolver（实体/领域解析；clarify 非空 ⇒ 短路反问）
  → prefetch(记忆/RAG/技能) → 预选(plan+lint+history_hint) → 激活工具
  → ReAct 多轮（每轮: 必注入层 + 已激活 schema + 历史）
  → grounding 拦截 → 文字结论
  → TraceCollector.flush() → qd_agent_traces（root/skill/tool 三层）
  → intake.record_decision() → decisions + claims（闸门通过才写）
```

### 5.2 追责 → 权重 → 行为

```
decisions/claims ──T+N到期──→ resolver.resolve_due_claims()
                                  ├ 取真实 K 线（exec_date 为基准★）
                                  ├ 阶段A 算偏差 → verdict/score/attribution
                                  └ 写 qd_agent_resolutions
                                        │
                                  weight_feed.feed()（EMA 慢调，domain 粒度）
                                        ↓
                                  qd_agent_weights
                                        │
                                  chain/weight_hints.py（唯一读取口，TTL 600s）
                                        ├→ 工具预选 history_hint（只提示不过滤）
                                        └→ qd_service._skill_score（tie-break）
```

⚠️ 非遗漏的设计选择：**只提示不过滤**（硬过滤与 fail-open 冲突）；**粒度只到 domain**
（resolutions 不带 tool/skill 归因）；**因子层未闭环**（`get_factor_weights` 仍返回 `{}`，见 §8 D-1）。

### 5.3 执行型技能分发流

```
用户："全市场扫一遍"
  → 主 agent 读 system「重活分发」纪律（或预选激活）
  → run_skill(name="market_screener")
       └ 子进程 skill_run.py → 白名单校验 → 执行 → 产物落盘
  → 回传预览 + full_path
  → 主 agent 引用预览写结论，明细留在文件里（不搬进上下文）
```

---

## 六、精巧处

1. **每轮现算工具面**：override `get_model_query_kwargs` —— 框架只在 init 算一次，不 override 则激活无效。
2. **预选输入只认 `last_user_query`**：剥掉 prefetch 注入块，否则 goal 被历史个股带偏（曾致工具全错）。
3. **lint 加法先于减法 + 有补位不作废**：减法性规则曾把资金流工具全清空。
4. **域兜底 `fallback_domain_tools`**：宁可补 4 个也不能落回裸工具面（旧系统 P0 断言同款）。
5. **分层 fail-open**：既省每轮 token，又不制造"工具看得见调不动"的死胡同。
6. **未激活直调就地激活**：把"分层"的成本转嫁给真正用到的那一次调用。
7. **技能渐进式注入 + 会话去重**：同一技能连续命中时从 2419 → 101 字符（换技能的那轮不省）。
8. **resource 路标只在 `list_resources()` 非空时才给**：5 个技能里 4 个无资源文件，无条件给会诱导必然失败的调用。
9. **指针不写"见上方历史"**：上下文压缩会改写历史，而 `_skill_seen` 对此不知情 ⇒ 指针必须给
   `read_skill(name=...)` 自取全文的路径。
10. **执行型技能走子进程白名单**：超时可击杀 + 崩溃隔离 + 主上下文 O(摘要)。
11. **追责闸门前置**：判不出来就不入库（宁漏不脏），避免用噪声训练权重。
12. **观测只计数**：不让观察演变成自动调参。

---

## 七、易错点与设计陷阱

### 7.1 导入与路径（本包统一**裸名**，最容易踩）

| 坑 | 说明 |
|---|---|
| **`app/agent` 必须排在 `app/` 之前** | 否则 `import utils` 命中 `app/utils`（23 个裸名都会错位）。`ensure_path_order()` 幂等保序 |
| **`app/agent/skills/market_screener/common.py` 的路径层级** | 曾按"..×3"算成 3 层，实为 4 层 ⇒ 把 `app/` 插到 sys.path[0] ⇒ 9 个 finance 工具静默挂掉，而**能力层补位把缺口掩盖了**；只有直接 `python cli.py` 才会踩 |
| **`cron_worker` 不是顶层名** | 必须 `from cron.cron_worker import start_cron_worker`。写成裸名会 ImportError 被吞 ⇒ 定时任务从未启动 |
| **判定"是否真执行"查标志位** | 别靠日志（logger propagate 不可靠），查 `_worker_started` 这类状态标志 |
| **`python -m` 跑 smoke 会误报** | `-m` 会先导入 `app.agent.scripts` 包，留下长名键 ⇒ test_29 判"无 `app.agent.*` 长名键"失败。必须 `python app/agent/scripts/qd_smoke.py` |

### 7.2 工具面

| 坑 | 说明 |
|---|---|
| **管线函数被注册成工具** | 判据是"公开 + 有 docstring + 定义在本模块" ⇒ 纯工具模块对，管线模块会被误伤。用模块级 `_NOT_TOOLS` 退出注册（目前只有 `tool_preselect.py` 声明了，`preselect_stats.py` **漏了**，见 §8 D-3） |
| **模块导入失败 = 静默降级** | 只 warning 没人看 ⇒ 落 `_TOOL_MODULE_IMPORT_FAILURES`，由 smoke `test_30` 断言为空 |
| **聚合摘除 key 要抹顶层前缀** | `_SUPERSEDED_BY_MERGE` 的 key 用「子目录.模块名:函数名」，否则长名/短名周期命中不一致（曾致 20 个废弃工具混在面里而自检反通过） |

### 7.3 数据库与统计

| 坑 | 说明 |
|---|---|
| **游标行是 dict（RealDictRow）** | 必须 `row['k']`；`row[0]` KeyError、`for a,b in rows` 解包出列名（全静默）。裸 psycopg2 那几处是 tuple 行，**别一起改** |
| **`SELECT COALESCE(MAX(id),0)` 必须起别名** | 否则列名是 `coalesce`，按 `["c"]` 取 KeyError ⇒ 清理逻辑被静默跳过 |
| **`%m-%d` 丢年份** | 日期一律 `%Y-%m-%d` |
| **统计推断三坑** | ① AUC 写成补角；② 平移置换只能平移"标签序列"，平移值序列零分布退化；③ **跨基数比较**（过滤后 n 变小/NaN 静默丢样本 ⇒ "过滤提升"多为假象）。详见项目 MEMORY |

### 7.4 行为一致性（prompt 与代码会漂移）

| 坑 | 说明 |
|---|---|
| **prompt 文案 vs 代码行为** | 曾出现 `qd_system.txt` 写"未激活直接调用会失败"，而代码是 fail-open —— 模型被误导绕远路。**改分层/改 fail-open 必须同步 system prompt** |
| **compaction 会改写历史** | 任何"见上方历史"式的引用都会失效。指针必须给可自取的 `read_skill(name=...)` |
| **会话去重键要同口径** | `_skill_seen` 的键必须 `str(session_id)`，与 `_get_agent` 一致，否则将来加清理逻辑会漏删 |
| **格式化类组件 = 一整轮 LLM** | `formatters/` 已因延迟摘除（稳态 11.7s）。恢复前必须满足：有超时 / 轻量模型 + max_tokens 上限 / 可选触发 |

### 7.5 验证方法

- **负向验证必须确认缺陷真注入**：曾因 heredoc 转义出错导致缺陷没注入却跑出全绿，差点误判"护栏失效"。
  改为落盘脚本（`inject` / `restore`）+ 断言还原后与原文字节相等。
- **env 类改动优先用真旋钮做负向验证**：如 `QD_ON_DEMAND_TOOLS=actor` 复跑，精确 FAIL 对应判据即可，无需改文件。
- **护栏判据要可证伪**：新增判据后跑一次"注入缺陷 → 必须 FAIL → 还原 → 必须 PASS"。

---

## 八、缺陷与待改进

> 完整路线（A~L，含方案与验收）见 `backend_api_python/app/agent/agent差距与改进路线.md`。
> 此处只列**当前确认为缺陷/缺口**的项，按影响排序。

| # | 问题 | 影响 | 现状 |
|---|---|---|---|
| **D-1** | 因子层未闭环：`get_factor_weights` 恒返回 `{}` | 权重闭环只到 domain，因子层无反馈 | ⬜ 未做 |
| **D-2** | `cron/` 被**旧系统**反向依赖（`agent_smolagents/cron/cron_tools.py` import `app.agent.cron`） | 删旧系统会直接断掉定时任务；是迁移的前置阻塞 | ⬜ 未做 |
| **D-3** | `tools/preselect_stats.py` **未声明 `_NOT_TOOLS`** | 5 个观测/管线函数（`empty_rate`/`record_empty_face`/`record_selected`/`report`/`reset`）挂在**必注入层**，每轮白下发 schema，且模型可能真去调 | ⬜ 未做（实测确认） |
| **D-4** | 会话上下文不跨进程持久化 | 重启/发版即失忆；多 worker 部署同 session 路由不到同进程也会断 | ⬜ 未做 |
| **D-5** | 能力级评测体系缺失 | 只有回归级 smoke，没有"选股分析质量 82 分"这类标尺 ⇒ 所有调优无依据 | ⬜ P0 |
| **D-6** | `agents/` 2 py 死代码 | 0 引用 | ⬜ 未清 |
| **D-7** | `tracing.py` 读 3 个**已删** prompt ⇒ `prompt_sha` 中 3/5 恒 missing | 静默降级，复盘时对不上版本 | ⬜ 未做 |
| **D-8** | 观测计数器是**进程内**的，重启归零 | 拿不到长期趋势 | ⬜ 未做 |
| **D-9** | `admission.json` 的 `doc` 字段 69/69 全空 | `func_overlap` 的"文档共同词"分支永不命中 ⇒ 漏"名字不像功能一样"的等价对 | ⬜ 待定 |
| **D-10** | 发布工程：`admission.json` / `del/` 归档未入版本控制；smoke 无 `--skip-env` 模式 | 干净 clone 过不了自家 smoke | ⬜ 未做 |
| **D-11** | `run_skill` 白名单硬编码在 `skill_run.py` | 违反"规则不在代码里"；加执行型技能要改代码 | ⬜ 未做（技能变多后再下沉为各技能目录声明文件） |
| **D-12** | `_skill_seen` 无 LRU 淘汰 | 长跑内存增长点 | ⬜ 未做 |
| **D-13** | `app/agent/utils` 与 `app/utils` 同名 | 长期雷（依赖 sys.path 顺序） | ⬜ 未做（建议改名 `qdutils`，涉 9 处） |
| **D-14** | `weight_feed` EMA 参数（α、MIN_SAMPLES=5）未经真实样本校准 | 慢调强度是拍的 | ⬜ 待攒样本 |
| **D-15** | judge 未校准；LLM 网关可用性未端到端验证 | 阶段 B 不敢开 | ⬜ 未验证 |
| **D-16** | 工具预选是纯启发式（token 倒排 + 域词典） | 同义问法/新黑话（"打板""埋伏"）召回差 | ⬜ 未做 |
| **D-17** | 超大技能靠字数截断出首段 | 截断点不是语义 ⇒ 首段未必是最该看的 | ⬜ 未做（建议各技能写 TL;DR 头部） |

---

## 九、配置说明（`QD_*`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `QD_ACCOUNTABILITY` | 1 | 追责总开关（=0 关） |
| `QD_CLAIM_JUDGE` | — | 阶段B LLM judge（默认关） |
| `QD_TOOL_PRESELECT` | 1 | 工具预选总开关 |
| `QD_PRESELECT_GATE` / `QD_PRESELECT_HINTS` / `QD_PRESELECT_MAX` | 1 / "" / 8 | 域特征闸门与预选上限 |
| `QD_TOOL_TIER_MODE` | tiered | 分级口径 |
| `QD_EXTRA_CORE_TOOLS` | "" | 追加必注入工具 |
| `QD_ON_DEMAND_TOOLS` | `actor,run_skill` | **反向旋钮**：把必注入工具降级为按需 |
| `QD_MAX_ACTIVE_TOOLS` / `QD_AUTO_ACTIVATE_MAX` | 12 / 8 | 同时激活上限 / 自动激活上限 |
| `QD_CAPABILITIES` | 1 | 三级能力层总开关 |
| `QD_TOOL_PROBE_LIVE` / `QD_TOOL_PROBE_TTL` | "" / 600 | 启动期可用性探测 |
| `QD_CONTEXT_WINDOW` / `QD_COMPACT_*` | 65536 / — | 上下文窗口与压缩参数（统一由 `_context_window()` 读） |
| `QD_STEP_LIMIT` / `QD_STEP_SCALE` / `QD_EVAL_STEP_LIMIT` / `QD_EVAL_TIMEOUT` | — / 3 / 8 / 240 | 步数与超时 |
| `QD_WEIGHT_TTL` / `QD_FEED_MIN_SAMPLES` | 600 / 5 | 权重快照缓存 / 慢调最小样本 |
| `QD_RESOLVER` / `QD_RESOLVER_CLARIFY` | 1 / 1 | 实体解析总开关 / 只反问 vs 只解析 |
| `QD_PROVIDER_MAP` / `QD_CODE_VERSION` | — / unknown | 供应商映射 / 版本标识 |

> 共 29 个 `QD_*`（grep 唯一名）。⚠️ 上下文窗口**统一由 `QDAgent._context_window()` 读**，不再两个 env 名打架。

---

## 十、回归基线

| 项 | 实测 |
|---|---|
| `compileall app/ scripts/` | ✅ 通过 |
| `qd_smoke.py` | ✅ **188 / 188**（含 test_29 单实例 / test_30 导入健康 / test_31 启动裸名 / test_32 技能注入 / test_33 技能分发+计划校验） |
| 工具面 | 注册表 **102**（工具 62 + 能力层 40）；必注入 **22** / 按需 **83** |
| schema 体积 | 全量 **32,703 字符** → 必注入 **7,912 字符**（−76%） |
| 技能 | **5** 个，其中可执行（含 `run.py`）**2** 个 |
| `formatters/` | 不在生产包内（已归档 `del/agent_formatters_20261001/`，smoke 有防复活断言） |
| `qd_prompt_eval --validate` | 8 条任务 / 27 项断言通过 |

⚠️ smoke 输出里的 Traceback 多来自 `chain/store.py save_tree` 在无 `DATABASE_URL` 时的 fail-open ERROR
（smoke 不加载 `.env`）—— 已知、非故障，别误判。

**跑法**：`cd backend_api_python && python app/agent/scripts/qd_smoke.py`
（**不要用 `-m`**，原因见 §7.1）
