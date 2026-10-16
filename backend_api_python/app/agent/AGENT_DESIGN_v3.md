# QuantDinger Agent 设计文档 v3 —— 执行系统 × 条件化预测引擎（合并重写版）

> **📌 2026-10-13·P6 目录改名（读本文前先看这三条）**
> 1. **本包就是本文描述的执行系统**，现名 `app/agent/`（`app.agent.*`）。
>    它先后叫过：`app/agent` → `app/agent_legacy`（给新版让位）→ **P6 收回 `app/agent`**。
> 2. 文中**历史路径一律按现名读**：早年的 `app/agent/...` 指的就是本包；
>    期间的 `app/agent_legacy/...` 也已随 P6 改回 `app/agent/...`。
> 3. 那套新版骨架（v6）**已于 2026-10-13 P16 整体退役**：目录 `app/agentcore/`
>    归档到 `del/agentcore_retired_20261013/`，`/api/agent2/*` 路由同步摘除
>    （`app/routes/__init__.py` 原注册处刻意不做 try/except 兜底，摘目录必摘路由）。
>    前端对该前缀本就零调用。⚠️ 它走之前留下的 **LLM 出口已于 P15 先迁回本包**
>    （`app/agent/llm.py`）、**统计功效三件套**（`estimate_mde` /
>    `estimate_independent_clusters` / `required_days_for_mde`，本文 §10.2 的
>    唯一可执行实现）迁至 `app/utils/stat_power.py`。其余为该骨架自有的
>    工具/运行时实现，随目录归档，本文不再描述。

> **本文取代谁**
> - 取代 `backend_api_python/app/agent/AGENT_DESIGN_v2.md`（执行系统现状）
> - 取代 `agent扩展设计-v5.md`（条件化预测引擎）
> - 吸收 `agent扩展设计-v4.md`（测量学）的统计地基与实测数字，做成**自包含**
> - 取代后 v2 / v5 移入 `del/agent_design_pre_v3_20261013/` 归档；v3 / v4 保留作历史沿革（v4 是测量学原始论证，v3 只引结论）
>
> **本文的两编分界（读之前先看这一条）**
> - **上编 · 执行系统** = **现状**（代码即文档，描述今天跑着的东西）
> - **下编 · 条件化预测引擎** = **目标态**（含已落地与未落地的部分，§21 逐项标注进度）
> 两编的**接口**只有一处：上编产出「一次推理的留痕」，下编把它读成「一条可校准的经验」。
>
> **数字口径**
> - 标「实测」的为可复现基线（脚本 / smoke / bench / 报告存档）
> - 标「未实测」的一律不许当承诺（典型：§11.3 的 `ρ_pair`）
> - 未标日期的为设计事实（代码即文档）
>
> **引用纪律**：*行号只作线索，函数名才是锚*。本文给出 `文件:行号` 会因后续改动漂移，施工时以函数名 / 常量名为准。
>
> **本地即运行、不做 git 入库**（裁定见附录 B.2）；过程记录查各模块头注与 `.workbuddy/memory/`。

---

## 第 0 章 本文主张（记住这五句，可以不要本文其他部分）

1. **LLM 做理解和提取，量化模型做预测和排序** —— 这条从 v2 立到现在没有变过，v3 只是把它做实。
2. **不许 LLM 报概率** —— LLM 只产出结构化的**条件证据向量**；概率由「冻结基线 + 经验校准项」的定量聚合层给（红线 R1）。
3. **换 estimand，不换数据源** —— 别问「命中率高不高」，问「经验让概率动了多少、方向对不对」（Δp 配对符号检验）。
4. **对照臂不要花钱买，顺手拿** —— `p_base` 是冻结模型，对每一个被分析的标的本来就能算，Δp 零额外成本。
5. **先让闭环说真话** —— 标签脏（判定取价不落地、退市不单列）的时候，后面全是在脏标签上做精密统计。

---

# 上编 · 执行系统（现状）

## 1 设计思路

### 1.1 要解决的问题

Agent 模块是 QuantDinger 的智能决策核心：接收自然语言输入（个股分析、全市场筛选、策略诊断、交易确认、通用问答），在 **92 个工具**的面上收敛出当轮最小可用工具集，跑 ReAct 闭环，输出结论，并把可证伪的断言留档、T+N 用真实行情判定。

三个硬约束决定了后面所有的架构选择：

> 📌 **本文所有「实测」数字的时点与口径（2026-10-13 统一订正）**
> - 工具面构成：自发现工具 **53**（common 11 / finance 41 / knowledge 1）+ 能力层 **39** = **92**。
> - 必注入口径：`CORE_TOOL_DOMAINS = ("common","meta")` ⇒ 注册表内 **11** 个，
>   加上元工具 `web_search`/`format_result`（**2**，走 `_MUST_HAVE` 独立通道、不进注册表）、
>   `update_plan`（**1**）与 mimo 原生 lowercase 目录（bash/read/write/edit/grep/glob/task，**7**）。
>   合计口径约 **21**。**旧文档写「19 个来自注册表」是 P10 清理误注册统计函数之前的数**。
> - ⚠️ 早期版本的「102 个 / 全量 32,703 字符 / 必注入 7,912」与 §22.4 的「58 个」
>   分属不同时点且口径不一，现全部作废，**以本节为准**。
> - 工具变少而 schema 反而变大（32,703 → 49,507）：因为 P12 恢复了 finance 的
>   自包含参数（如 kdj 的 m1/m2），单工具 schema 变胖。结论方向**不变且更强**
>   —— 全量下发更不可行，分级与预选的必要性只增不减。

| 约束 | 后果 |
|---|---|
| 工具面 **92** 个（工具 53 + 能力层 39）、全量 schema **49,507 字符**（实测） | 全量下发不可行 ⇒ 必须分级，且必须每轮现算 |
| 结论里的数字必须可溯源 | 需要 grounding gate（数字溯源拦截） |
| 用户会问"你上次说的对不对" | 需要追责四表 + T+N 真值判定 + 反哺行为 |

### 1.2 设计原则

| 原则 | 落地形态 |
|---|---|
| **最小工具面** | 三级分级 + 预选 + lint；必注入层 **21** 个 / 全量 **92** 个（口径见 §1.1） |
| **fail-open 优先** | 预选空手有域兜底、未激活直调就地激活、能力层补位；宁多不可断 |
| **可追责** | `decisions/claims/resolutions` 三表 + T+N 判定 + 权重反哺 |
| **只提示、不过滤** | 权重/历史表现只做 hint，不做硬过滤（硬过滤与 fail-open 冲突） |
| **澄清优先** | `resolvers/` 挂在取数之前，歧义先反问，不猜默认值 |
| **领域中立** | 域走配置表 `qd_domain_resolvers`，代码里没有 `if domain == 'finance'` |
| **先量化再决定** | 拿不准的先装观测计数器，不写自动调参逻辑 |
| **规则不在代码里** | 决策规则外置（yaml / 参考文档），代码只做客观计算 |
| **标准兼容** | Tool 完全兼容 **OpenAI Function Calling** 标准（JSON Schema 入参）；Skill 完全兼容 **Anthropic SKILL** 标准（`<目录>/SKILL.md` + frontmatter：**仅 `name`/`description` 置顶**，`version`/`tags`/`tools` 收进 `metadata`）。兼容性契约真源见 `skills/__init__.py`（2026-10-14 与 S3 对齐，旧文写 `name/version/description/tags/tools` 平铺为历史口径） |

### 1.3 四条关键取舍

1. **删掉自研编排**：旧版 StateGraph + 4 节点 + phase acceptance 全部删除，只保留 mimo 单 ReAct 循环 + 一个 `update_plan` 清单工具。
   ⚠️ 「单循环」≠「裸循环」：执行核实为 `QDAgent(MimocodeAgent)`，而 mimo 的 `MimocodeAgent` 本就自带 **`task` 子代理 / `actor` 并行子代理 / `compact` 模型触发压缩 + `<context_usage>` 预算感知 / AntiHack 拦截器 / 入参 schema 硬校验 / stray `<tool_call>` 恢复**。本项目的「自研」只加在：金融域工具面（**92**，口径见 §1.1；⚠️ 此处早期写 102，是已作废旧数）、工具分级与预选、金融 prompt 层、`update_plan` 补挂、追责闭环三件、grounding 门；循环与上述原语全部沿用上游。
2. **分级是架构不是配置**：框架只在 init 算一次工具定义，我们 override `get_model_query_kwargs` 改成**每轮现算**，否则 `activate_tools` 激活了也不生效 —— 这是整套分层的前提。
3. **权重只提示不过滤**：硬过滤会让模型在真需要某工具时拿不到它。粒度也只到 domain —— resolutions 不带 tool/skill 归因，硬按 `user_query` 反推"当时用了哪几个工具"是伪精确。
4. **接入组件前先问"它要几次 LLM 往返"**：本地计算（权重提示 0ms / 实体解析 95ms）与一整轮 LLM 生成（结论格式化 11.7s）是两类东西，评审阶段就要区分，不能等用户说"变慢了"再归因。

---

## 2 拓扑

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
│  eval worker 由 chain/evaluator.start_eval_worker 启动(装配期也触发)     │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              会话层:QDAgentService(有状态,按 session_id 复用)         │
│   resolvers/bridge.resolve()(在 _run_sync 内,歧义→短路反问) → _prefetch() │
└────────────────────────────┬─────────────────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   执行层:QDAgent(继承 MimocodeAgent)                   │
│  _run_inner 单循环:                                                      │
│   0 _steps_taken=0 / current_plan=None  ← 预算与计划都是 per-run         │
│   1 _maybe_preselect_tools  ← 外置 plan + 确定性 lint + 域兜底           │
│   2 注入 now / tool_map / tool_availability 到 system_template           │
│   3 super().run()  → ReAct(每轮现算工具面 + 分层 fail-open)            │
│   4 空结果兜底(LimitsExceeded / Empty 分别处理)                        │
│   5 grounding_gate:数字溯源拦截 + 独立预算重试                          │
│   6 _plan_lint() 计划收尾轻校验(只告警)                                │
│   7 _finish_collector → 追责入库 intake → 下编的经验留痕                │
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
│  qd_agent_cases(案例记忆 + EV-1 载荷) │ pgvector(RAG) │ Redis │ JSONL     │
│  qd_agent_traces(旧链路,仍在写)                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 目录结构（实测）

```
backend_api_python/app/agent/
├── agent.py              # 统一入口 Facade(run_agent 同步入口 + 装配期 warmup + 触发 eval worker)
├── qd_agent.py           # ★ QDAgent:mimo 子类,全系统核心
├── qd_service.py         # 有状态会话服务(prefetch / 技能渐进式注入 / 会话去重)
├── flask_app.py          # Flask Blueprint(SSE)
├── cli.py                # CLI 入口
├── message_queue.py      # 统一消息队列(Flask/Cron 共用)
├── trace_collector.py    # TraceCollector → qd_agent_traces(root/skill/tool 三层)
├── mimo_boot.py          # mimoagent 依赖引导
├── constants.py          # 全局常量单一事实源(AGENT_MAX_STEPS 等)
├── domain_registry.py    # 跨层分类学注册表（2026-10-14 精简：只留有消费方的 4 字段/4 API；
│                         #   数据域词典**单源**在 preselect/tool_preselect.py::DATA_DOMAINS）
├── log.py
│
├── tools/        [38 py]  工具层:顶层 14(含 __init__) + finance 21 + knowledge 3
│   ├── tool_discovery.py     # search_tools / list_tools / activate_tools(两级装载)
│   ├── skill_tools.py        # list_skills / read_skill / run_skill
│   ├── capability_tools.py   # search_capabilities / call_capability / list_capabilities
│   ├── memory_tools.py       # remember / recall
│   ├── format_utils.py       # 元工具 format_result(_MUST_HAVE,不进注册表)
│   ├── web_search_tools.py   # 元工具 web_search(博查/Tavily/baidusearch/SearXNG 四引擎降级)
│   ├── _base.py              # Tool ABC + ToolResult + re-export(下划线=非工具)
│   ├── _provider.py          # ToolProvider 注册表(扫描/注册/查询/元数据)
│   ├── _schema_gen.py        # OpenAI schema + 「什么是工具函数」判据
│   ├── _serialize.py         # 结果序列化(TSV/紧凑 JSON/空值剥离)
│   ├── _fn_adapter.py        # FnToolAdapter:裸函数工具零改动适配 mimo BaseTool
│   ├── _availability.py      # 启动期工具可用性探测(进系统提示工具地图)
│   └── finance/(21) knowledge/(3)
│     ※ **扫描规则(2026-10-13 P10)**:文件名以 `_` 开头 = 非工具模块,
│       扫描器天然跳过。取代已删除的 `_SKIP_FILES` 名单(曾有两份且实测漂移:
│       mcp 侧漏登记 availability ⇒ 自检函数被暴露成 MCP 工具)。
│       finance/ 下同样适用:_em_utils / _screener_config / _analysis_utils。
│     ※ **非工具模块已于 P10 改 `_` 前缀**(base→_base、availability→_availability);
│       本清单早期版本把新旧两个名字并列(base.py 与 _base.py)且 tool_discovery
│       重复列两次,是文档笔误,现已按实际目录订正为 14 个(含 __init__.py);
│       2026-10-14 归档 _pagination.py 后为 **13 个**。
│     ※ 预选管线(tool_preselect / preselect_stats / preselect_cooc)已于 P9 迁出
│       本目录到 app/agent/preselect/ —— 它们不是工具,不该住在自发现目录里。
│     ※ mcp_bridge.py 于 P10 删除(从未接线:requirements 未声明 mcp、零启动入口)
│     ※ filesystem_tools.py 已退役;mimo 原生 lowercase 目录由基类注册
│     ※ **_pagination.py(444 行)于 2026-10-14 归档 del/deadcode_20261014/**
│        —— AST 复核(三形式 import + 全仓扫描 + 同名异物判定)确认全仓零引用。
│        目录树顶层随之 14 → 13。
│
├── capabilities/ [4 py]  三级能力层(admission.json 准入 **68** → 注册 39)
│     ※ **admission.json 就在工作副本里**(2026-10-13 订正:早期注「不在工作副本」
│       与事实相反)。实测 **71 条目 / 68 准入 / 3 排除 / 29 项让位域内 ⇒ 注册 39**。
│       (2026-10-13 晚 P14 又订正:此前写「69 准入 / 1 项导入失败」——那项
│       `app.market_cn.auto.store:get_signal_by_code` 是**幽灵条目**(函数从未存在),
│       已改 `admitted:false` + `recheck_note`,故排除数 2→3、准入 69→68。
│       **零行为变更**:注册数始终 39,它从来没注册上。)
│
├── chain/        [23 py] ★ 追责 + 评估 + 权重闭环 + 条件化预测引擎(下编)
│   ├── claims.py             # 域判定 / 入库闸门 / Claim 提取(纯函数,零 LLM)
│   ├── intake.py             # record_decision 唯一入口
│   ├── account_store.py      # decisions/claims/resolutions 持久化
│   ├── resolver.py           # 阶段A 算数判定(含 delisted 单列)+ 阶段B LLM judge(默认关)
│   ├── weight_feed.py        # resolutions → qd_agent_weights(EMA 慢调,domain 粒度)
│   ├── weight_hints.py       # 权重**唯一消费口**(TTL 快照)
│   ├── evaluator.py          # 盘后 worker(旧评估 + 追责判定 + weight_feed + 日采集)
│   ├── label_spec.py         # ★ 持有期/bar_freq 口径单一真源(SCHEMA_VER=ls2)
│   ├── context_frame.py      # ★ C 类情境档案每日一帧(as_of/regime/vol_bucket/theme_heat)
│   ├── sampler.py            # ★ 分层配额选股 pick_daily_universe(活跃60/中热25/低热+随机15)
│   ├── collector.py          # ★ 日采集编排 run_daily_collection
│   ├── evidence.py           # ★ EV-1 证据向量构建 + 条件键 + R1 拦截
│   ├── aggregator.py         # ★ 定量聚合器 aggregate() + record_pair()(无 LLM)
│   ├── calibration.py        # ★ 条件校准与层级部分池化 fit_with_pooling / coef_usable（纯算法）
│   ├── calib_feed.py         # ★ P3 接线(2026-10-14)：pairs × resolutions → cells → β₀ → coefs
│   │     消费方 1：chain/c3p.py::_c3p_pipeline（aggregate 的 coefs 实参）
│   └── reset.py / schema.py / store.py / judge_stats.py / skill_brewer.py
│
├── skills/       [6 目录]  market-screener / strategy-debug(**含 run.py,可执行**)
│                           + auto-stock-analyze / auto-finance-analysis-stock
│                           / auto-finance-query-stock / auto-agent
├── rag/          [6 py, 1230 行]  检索:BGE reranker + pgvector(PgVectorStore) + PG FTS + 聊天历史/关键词
│     消费方 3:agent.py(_build_retriever 四路线 / _build_reranker)、tools/knowledge/knowledge_tools.py:62、utils/case_memory.py:200
│     ⚠️ 消费方**全用相对 import**(`.rag.X`) —— 只扫绝对形式会漏成"零引用"
│       (2026-10-13 差点据此把它误判死代码归档,见 §7.1 三形式判据)
├── memory/       [5 py,  672 行]  跨会话记忆 remember/recall;base + local/postgres/redis 三后端
│     消费方 4:agent.py:15、scripts/qd_bench.py、scripts/qd_dispatch_ab.py、scripts/qd_prompt_eval.py
│     ※ **redis_memory.py 曾被误判为死代码(2026-10-14 纠正)**:复核探针的**相对 import
│       解析在 `__init__.py` 上少剥一层** ⇒ 漏掉 `memory/__init__.py:8` 的
│       `from .redis_memory import RedisMemory` ⇒ 判成"零引用"并归档,结果
│       `import app.agent.memory` 直接 ModuleNotFoundError。**已回滚**。
│       事实:`RedisMemory` 在 `__init__.py` 的 `__all__` 里 —— 是包的**公开导出**,
│       不算死代码(工厂虽从不创建它,但导出契约存在)。要删须先改 `__init__` 并确认
│       仓外无人 import,属"降能力"操作,不在死代码清理范围。
├── utils/        [11 py, 2800 行] 共用件:budget / calibration / case_memory / grounding / json_parser /
│                                 logger / mask / md_format / prescan / tracing
│     消费方 28(**全包最大枢纽**):chain×4、qd_*×4、scripts×3、tools/finance×11、
│                                 tools/knowledge、trace_collector、tests×3
├── resolvers/    [6 py,  968 行] 实体/时间/复合解析 + 澄清优先(bridge 挂取数前,clarify 非空短路反问)
│     消费方 2:qd_service.py(296/451/454/467)、scripts/qd_smoke.py
├── cron/         [4 py,  969 行] 定时任务:cron_tools(工具面) + cron_worker(执行) + guards
│     消费方 4 **全在包外**:app/__init__.py:423(启动 start_cron_worker)、routes/cron.py、
│                            routes/health.py、tests/test_v6_legacy_boundary.py
│     ⚠️ 即 D-2「被**旧系统**反向依赖」:摘旧路由会直接断掉定时任务启动
├── audit/        [2 py,  106 行] TraceAdapter 执行留痕(root/skill/tool 三层)
│     消费方 1:qd_agent.py:53 顶层导入,按 `trace_enabled` 实例化
└── preselect/    [4 py, 1629 行] 预选管线(P9 从 tools/ 迁出):tool_preselect / preselect_stats /
      │                           preselect_cooc(共现权重 K) / __init__
      └─ 消费方 8:qd_preselect.py、qd_tool_face.py、qd_aftercare.py、scripts/qd_smoke.py、
                   tests/{_p9_preselect_baseline,test_preselect_cooc,test_v14_preselect_capability_catalog,
                   test_v9_preselect_pipeline}.py
├── **qd_agent.py 的 P6 拆分产物**(执行核按职责拆开,不是散落的杂物):
│   ├── qd_tool_face.py       # 工具面 get_model_query_kwargs(每轮现算)/ 启动期预加载
│   ├── qd_preselect.py       # _maybe_preselect_tools(预选接线)
│   ├── qd_aftercare.py       # _finish_collector / _intake_accountability / _record_case
│   ├── qd_budgeting.py       # _account_budget 成本记账
│   ├── qd_grounding.py       # grounding gate 数字溯源
│   ├── qd_guards.py          # 观测型守卫
│   ├── qd_hooks.py           # 生命周期钩子
│   ├── qd_config.py          # 配置与常量(CORE_TOOL_DOMAINS / _context_window)
│   └── skills_adapter.py     # 技能加载适配
│   ※ ⚠️ §2.3/§4.1 早期版本把这些方法一律记在 `qd_agent.py` 名下 —— 那是**拆分前**
│     的写法。以函数名为锚、以本清单为落点;`qd_agent.py` 现只保留 `execute_action`
│     (分层 fail-open)与 run 主干。
│   ※ **LLM 出口的位置变迁**：P2 删掉旧自建 `llm/` 包（原 7 个文件）→ 出口唯一化到
│     `app.agentcore.llm` → **2026-10-13 P15 迁回本包**，现为 `app/agent/llm.py`
│     （301 行，详见 §3.10 订正）。
│     ⚠️ 注意区分两个不同东西：被删的是**旧自建 `llm/` 子包**（`LLMFactory` /
│     `QDSkillAdapter` 那套）；现在的是**单文件 `llm.py`**（mimo models 驱动的
│     `complete`/`acomplete`/`resolve_provider`/`LLMResponse`）。同名不同物。
├── prompts/      提示词外置（2026-10-14 全量收口；loader=prompts/__init__.py）
│                 qd_core_base.txt / qd_system.txt / skill_brew.txt
│                 skill_revise.md / tool_preselect.yaml / wrapup_summary.md
│                 evidence_extract.md / verdict_judge.md / skill_engineer.yaml / func_overlap.yaml
├── scripts/      [6 py + 2 yaml]  qd_smoke.py(回归 257 项,2026-10-13 实测;旧记 256)/ qd_prompt_eval.py + yaml(10 任务/35 断言)/
│                                  qd_bench.py + yaml(25 题)/ qd_dispatch_ab.py / skill_run.py / weekly_panel.py
└── traces/, data/, tmp/   辅助目录
```

> **存活判定方法**：`grep -rnE "^\s*(from|import)\s+app\.agent\.<pkg>\b"` 排除包内自引用。
> ⚠️ **必须同时覆盖三种 import 形式**（2026-10-13 P17-b 踩坑实录）：
> ① `app.agent.x` 点号连写；② `from app.agent.x import y`；③ **相对 import**
> `from .rag.retriever import X`（要按文件所在包解析成绝对模块名）。
> 只扫 ①② 会把包内相对引用漏成「零引用」——`app/agent/rag/`（6 个文件引用）与
> `app/agent/audit/`（1 个）曾因此被误判为死代码、差点归档进 `del/`。
> 旧版此处写「本包统一用**裸名** import（`from chain.store import ...`）」——
> 那是 **P5 之前**的写法，现活代码 0 处，判据已随 P5 改为 `app.agent.*`。

### 2.3 核心文件职责速查

| 文件 | 职责 | 关键点 |
|---|---|---|
| `qd_agent.py` | 执行核（**P6 已拆**） | `execute_action` 分层 fail-open 留在本文件；`get_model_query_kwargs` 拆到 `qd_tool_face.py`、`_maybe_preselect_tools` 拆到 `qd_preselect.py`、收尾三件拆到 `qd_aftercare.py`（落点见 §2.2） |
| `qd_tool_face.py` | 工具面 | ★ override `get_model_query_kwargs` **每轮现算**工具面 —— 整套分层的前提；工具面启动期预加载 |
| `qd_aftercare.py` | 收尾三件 | `_finish_collector` → `_intake_accountability`（追责入库）→ `_record_case`（经验留痕） |
| `tools/_provider.py` | 工具注册与扫描 | 扫描判据：文件名以 `_` 开头即跳过（**P10 起**）。⚠️ 早期版本的 `_NOT_TOOLS` / `_SKIP_FILES` / `_ENTRY_NAME_DENY` 三套名单**均已删除**，不要再按旧文档把它们加回来 |
| `preselect/tool_preselect.py` | 预选 + lint | 加法先于减法；有补位则不得作废（P9 已从 `tools/` 迁出） |
| `tools/skill_tools.py` | 技能读写 + 执行分发 | `read_skill` 读方法论；`run_skill` 跑流水线（子进程） |
| `scripts/skill_run.py` | 执行型技能子进程入口 | 白名单注册表 + fn 白名单 + 参数校验 + 超时击杀 + 产物落盘 |
| `chain/claims.py` | 追责抽取 | 纯函数；闸门判不出来就拦（宁漏不脏） |
| `chain/label_spec.py` | 口径真源 | 持有期单位恒为**交易日**；`hold_days` 严格 / `resolve_hold_days` 宽松（全项目唯一兜底处） |
| `chain/weight_hints.py` | 权重消费 | TTL 600s 快照；**务必经此入口读权重** |
| `resolvers/bridge.py` | 澄清 / 实体解析 | 挂在取数**之前**；clarify 非空即短路反问 |
| `qd_service.py` | 会话与预取 | 技能渐进式注入 + 会话级去重 + 执行型技能分发卡片 |
| `utils/case_memory.py` | 案例记忆（CBR） | `record_case` 写入 / `retrieve_cases` 检索 / `backfill_by_root` 延迟回填；`incorrect` 硬排除、pending 降权 |
| `preselect/preselect_cooc.py` | 预选共现权重（K） | 缺数据/样本<50/异常一律退回原序（冷启动与纯启发式 bit 级一致）（P9 已从 `tools/` 迁出） |
| `scripts/qd_bench.py` | 能力级跑分（C） | coverage/grounding/claims/structure/human 五维确定性打分 |

---

## 3 逻辑结构

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

**继承链（实测，安装版 `mimoagent 0.1.0`）**：`BaseAgent → DefaultAgent → MimocodeAgent → QDAgent`。

- `BaseAgent`：循环骨架；`run() → (status, message)`；状态 `Idle / LimitsExceeded / ModelQueryError / InfraError`；**空回复也抛 `LimitsExceeded("Empty assistant response")`**；`self.messages` 跨 `run()` 复用（多轮）。
- `DefaultAgent`：**并行工具调用**（`tool_parallel_workers=8`）；**observation 中截断**（`max_observation_length`，`truncate_middle`）；`wrap_up_hint`（上下文预算催收）；`ActionInterceptor` 预派发钩子。
- `MimocodeAgent`（**本项目实际基类**）：MiMo-Code lowercase 目录（`bash/read/write/edit/grep/glob/task` + 可选 `actor`/`compact`）；`task` = 子代理（`step_limit=500`，登记进父 agent 的 `subagents`）；`actor` = 并行子代理；`compact` = **模型自触发压缩** + `<context_usage>` 页脚（上游默认窗口 **1,000,000**）；`AntiHackGuard` 以 interceptor 形态挂载（上游默认关）；`_parse_tool_call` 做**入参 schema 硬校验**；`step()` 检测 **stray `<tool_call>`** 转 `FormatError` 续跑。
- 框架其余：`agents/factory.py`、`models/`（chat / anthropic / responses 三原生协议 + `TokenStats`）、`environments/`、dataset / grader / rollout（`batch.py` / `ray_batch.py`）。

**框架给了、本项目用没用/改没改口径的原语**：

| 框架原语 | 本项目处置 | 依据 |
|---|---|---|
| `task` 子代理 / `actor` 并行子代理 | `task` 进必注入；`actor` 默认降级为按需 | `qd_agent.py` |
| `compact` + `<context_usage>` | 沿用；并把窗口从上游默认 **1M** 改回**真实窗口** | `qd_agent.py` |
| `wrap_up_hint` | **关**（避免与 `<context_usage>` 双重催收） | `qd_agent.py` |
| `AntiHackGuard` | **显式开启**（上游默认关） | `qd_agent.py` |
| `compaction_summary_prompt` | 换成金融域压缩纪律 `_QD_COMPACT_NOTES` | `qd_agent.py` |
| `max_observation_length` | 继承 Mimocode 的 `0`；项目另建 `tools/pagination.py` + `QD_MAX_OBSERVATION_CHARS` | `MimocodeAgentConfig` |
| `execute_action` | 项目**覆写**（探针 + 分层 fail-open） | `qd_agent.py` |
| dataset / grader / rollout | **未用**；评测走自建 `scripts/qd_bench.py` 三尺 | §3.13 |

⚠️ 因此旧口径「只保留 mimo 单 ReAct 循环 + 一个 `update_plan`」**低估了框架**：子代理 / 压缩 / 预算感知 / 反作弊 / schema 校验都是**上游原语**，本项目是「继承 + 域增强」而非"从零搭循环"。不写清会被后人当成自研件重复造。

### 3.2 工具面：三级分级 + 分层下发 + 按需降级

| 级 | 定义 | 实测 |
|---|---|---|
| **一级 必注入** | `tools/` 顶层 + mimo 原生/元工具 | **21**（注册表 11 + 元工具 `web_search`/`format_result` 2 + `update_plan` 1 + mimo 原生 7，口径见 §1.1） |
| **二级 域内** | `tools/finance` + `tools/knowledge` | 域内优先于能力层（finance 41 / knowledge 1） |
| **三级 能力层** | `capabilities/` 准入函数 | 准入 **68** → 注册 **39**（29 项让位给域内） |

- 注册表总量 **92**（工具 53 + 能力层 39）；必注入 **21** + 按需 **71**（**全口径**，含元工具与 mimo 原生）。
  ⚠️ **两种口径别混**：只数注册表内的子域/能力层，按需是 **81**（=92−11）；
  加上元工具 2 + `update_plan` 1 + mimo 原生 7 这 10 个「注册表外但必注入」的，按需才是 71。
- **schema 体积**：全量 **49,507 字符** ⇒ 必注入（注册表 common+meta 11 个）**4,188 字符**。每轮请求都带这一整块，prompt cache 省钱但**不省窗口**。
- **"域内优先"的三处落实**：① `build_catalog_grouped` 目录分两段、能力层垫底；② lint **R0 能力层让位**；③ `apply_lint(capability_names=)` 排序垫底。

> ✅ **① 已于 2026-10-13（P14）修回**。此前它是**代码缺陷**（非设计问题）：
> `qd_preselect.py` 调 `build_catalog_grouped(defs, dom_names, [])` **第三参传空**，
> ⇒ 能力层不进预选目录，只在末尾拼一行"另有 N 个"的计数指针。
> 这是 2026-10-11「目录瘦身」时改的，未同步本文。
>
> **为什么必须修回（不只是"优先级退化"，是功能性断裂）**：`PRESELECT_SYSTEM`
> 明写「某条交付信息在候选目录里找不到对应工具，就在 deliverables 里删掉它」。
> 能力层是**域内无覆盖时的唯一兜底**，指针行只报数量、不列名字 ⇒ 模型既无法按名
> 调用，也会把只能由能力层支撑的交付要素整条删掉。**不可见即不可用。**
>
> **代价实测**：预选目录 4,144 → 6,946 字符（**+2,802，+67.6%**），仅发生在预选那一轮；
> 相对全量 schema 49,507 字符仍是量级优势（§22.4）。取舍结论：**省下的 token 换不来
> 一条被误删的交付要素**，本项目取"能力可达"而非"省 token"。
> ②③ 在缺陷期间仍生效，故线上表现为"能力层几乎选不中"而非"完全不可用"。

**按需降级旋钮**：`QD_ON_DEMAND_TOOLS`（默认 `actor,run_skill`）可把必注入工具降级为按需。降级后仍拿得到，三条兜底：

1. **分层 fail-open**（`qd_agent.py::execute_action`）：已注册但未激活的工具被直调时**就地激活放行**，不会 `Unknown tool`；
2. **工具预选目录由 `_on_demand_names` 构建** ⇒ 相关任务首轮就能被点名激活，不产生"search→list→activate"三轮发现；
3. **`qd_system.txt` 的"重活分发"小节直接点名 `run_skill`** ⇒ 模型知道名字。

> ⚠️ 因此 `qd_system.txt` 里**不能**写"未激活直接调用会失败" —— 那是旧文案，与 fail-open 相反，会让模型绕回三轮发现。

### 3.3 工具预选：外置 plan + 确定性 lint

**为什么不是"让 LLM 裸选工具"**：裸选会让天气问题被塞 8 个股票工具、token +136%、净收益≈0。真根因是**预选输入被 prefetch 污染**（用了含历史个股记忆的增强文本）。

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

**实测收益（重任务）**：`stock_screen` 153,960 tok / 6 步 → 43,318 / 2 步（**-72%**）；
`trade_confirm` 100,150 / 6 步 → 34,976 / 2 步（**-65%**）。

> ✅ **「已注入执行型技能时能否跳过预选」A/B 结论（2026-10-14，问题=现在买什么股）**：**不能跳**。
> buy2（正常预选）4 步 / 64,159 tok / 200.9s / 4 次调用 ← 最优；
> buy3（跳过**域**预选、保留能力层）7 步 / 133,726 tok / 313.2s / 9 次；
> buy4（整段跳过预选）10 步 / 238,960 tok / 286.5s / 15 次（3×`search_tools` + `list_skills` + `read_skill` 的工具发现抖动）。
> 预选这次 LLM 调用**必要**：它预先激活正确的域工具；跳过省下的 token 会以「工具发现抖动」加倍还回去。
> 真正的收益来自 §3.4 的技能注入 bug 修复，与「跳预选」无关；实验分支代码已回退（净保留 `_canonical_skill_name` 修复与 `get_fund_flow_daily` 主备翻转）。

**域兜底（不许落回裸工具面）**：`fallback_domain_tools(query, available, limit=4)`（纯词典、零 LLM）补核心子集 —— 「帮我看看大盘」→ market 补 4 个；「今天北京天气」「写个跑马灯」→ **0 个**（命中不了不硬塞）。

### 3.4 技能系统

> 📌 **兼容性契约（对外承诺，变更需评审）**：**Tool 完全兼容 OpenAI Function Calling 标准**（JSON Schema 参数描述）；**Skill 完全兼容 Anthropic SKILL 标准**（`<目录>/SKILL.md` + frontmatter：仅 `name`/`description` 置顶，`version`/`tags`/`tools` 收进 `metadata`）。真源：`app/agent/skills/__init__.py` 头注；酿造提示词 `prompts/skill_brew.txt` + 修订模板 `chain/skill_brewer._REVISE_TEMPLATE`（须与 `skills_adapter._validate_frontmatter` 同口径）。

| 环节 | 机制 |
|---|---|
| **选技能** | 词典分 + 范围信号（有无标的）+ 意图信号；权重仅 tie-break；`_FINANCE_GATE` 防"跑马灯"被注入选股技能 |
| **注入** | 渐进式：`_skill_block` = 正文首段（上限 2200 字符）+ `[技能目录]` + `[按需读取]` 路标。**执行型技能不注正文**：命中 `EXECUTABLE_SKILLS` 时改注**分发卡片** `_dispatch_card()`（≤600 字符门禁） |
| **去重** | `_skill_seen`（per session）：二次命中只给一行指针，正文不再重复注入 |
| **读** | `read_skill(name)` 全文 / `read_skill(name, heading=)` 小节 / `read_skill(name, resource=)` 资源 |
| **执行** | `run_skill(name, arguments)` → 子进程 `scripts/skill_run.py`（仅执行型技能） |

**执行型技能分发**（`market-screener` / `strategy-debug`）：

```
主 agent
  └─ run_skill(name, arguments)  ──► 子进程 scripts/skill_run.py
        1 技能名白名单(EXECUTABLE_SKILLS)  2 fn 白名单
        3 inspect.signature 未知参数拦截(不执行就退出)
        4 执行 → 产物落 <backend_api_python>/tmp/skill_output/*.json
                  （2026-10-14 迁址：原 app/agent/tmp/skill_output/，随 agent/tmp 移除）
        5 回传 {ok, result(预览≤20k字符), full_path, elapsed_s}
        6 空结果 ⇒ 按「空结果纪律」收尾交付,不得回退主对话逐个取数重跑
```

三重收益：**超时可击杀**（600s）、**崩溃隔离**（不污染主进程）、**主上下文只拿摘要**（O(摘要) 而非 O(全量)）。

> ⚠️ **技能名两个拼写，必须归一化**：
> - **目录名**（下划线）= 执行注册表键：`skills/market-screener/` ⇔ `skill_run.EXECUTABLE_SKILLS["market-screener"]`。
> - **显示名**（连字符）= SKILL.md frontmatter `name: market-screener`，是 `list_skills()` / 注入给模型的那个名字。
> - `read_skill` 能吃显示名；`run_skill` 必须归一化后再传 `skill_run.py`（那边是精确匹配）。
> - 仍存在的隐患：**同一份上下文里两种拼写并存**，名字不一致本身就在诱导模型犹豫。
> - ✅ **2026-10-14 已修（注入路径）**：`qd_service._prefetch` 曾用**显示名**去查 `_executable_skills()`（键是**目录名**）→ 恒 `None` → 落回正文注入分支，把整份 SKILL.md 正文灌进上下文，**分发卡片自 2026-10-10 起对所有执行型技能实际失效**。修复：新增 `_canonical_skill_name(skills, name)`（复用 adapter 的 display→name 映射 `skills.get(name).name`，取不到原样返回，fail-open），先归一化再查白名单。实测（现在买什么股）：注入 2529→407 字符，步数 9→4、元工具调用 6→0、token 290,065→64,159（**-78%**）、耗时 328.9s→200.9s。回归守卫 `tests/test_v6_d11_whitelist_knobs.py`（新增 4 项 `_canonical_skill_name` 断言）。
> - ✅ **2026-10-14 解耦修复（技能卡片 ↔ 工具激活）**：执行型技能注入分发卡片点名的 `run_skill` 是**按需工具**（`QD_ON_DEMAND_TOOLS` 默认含它），而 §3.3 的预选看的是**剥掉注入后的用户原始问题**（`qd_preselect._preselect_query`）⇒ 看不到技能、不会点 `run_skill` ⇒ 模型实测要白花一轮 `search_tools` 才发现（trace step1）。兜底 ②（"预选目录由 `_on_demand_names` 构建"）在此场景**不成立**：目录里有名，但模型不知道有技能。修法：`_prefetch` 把卡片点名的工具写进 `report["skill_force_tools"]`（卡片分支与二次指针分支都写），service 挂到 `agent.prefetch_force_tools`，agent 在**预选之后、`tool_map` 渲染之前** `activate_tools`（放 `_activated` 尾部，避开 FIFO 先淘汰）。回归守卫 `tests/test_v6_d11_whitelist_knobs.py` §④（4 项）。
> - ✅ **2026-10-14 实时覆盖层 + 非空结果纪律（`deep_analyze` bar/rt 对账）**：`run()`/`deep_analyze` 的 tech 只吃 `fetch_kline`（本地日线），盘后 bar 滞后时可执行性字段与真值打架（实测 600408：bar 3.69/+4.53%/非涨停 vs 实时 4.06/+10.03%/封板）。主 agent 闻到矛盾后自设计划「技术面+资金流+实时报价验证」逐只重拉 ⇒ **11 步 / 210k token / budget_wrapped**（对照 buy2 基线 4 步 / 64k）。修法两件：① `_helpers.fetch_rt_map` + `apply_rt_overlay` —— 历史指标（MA/RSI/20 日位）留 bar，可执行性字段（收盘/涨跌停）以 rt 为准，显式标 `stale_bar` / `rt` / `rt_risks`；② `qd_system.txt` §重活分发 + `_dispatch_card` 补**非空结果纪律**（证据齐 ⇒ 直接交付；禁止逐只重拉 quote/flow/tech；只允许 Top1–2 `inspect_stock` 或补明确 missing，合计 ≤2 轮）。守卫 `tests/test_v6_market_screener_rt.py`（4 项）+ `qd_smoke` test_38 ②c（2 项）。

> ⚠️ **子进程「跑完退不出」陷阱 —— 600s 超时的真身不是流水线慢**：
> - 现象：盘前 `run_skill(market-screener)` 恒定 `Error: skill_run 超时（>600s）已中止`，一度被读作「盘前全市场扫描比盘后慢 12 倍」。
> - 实测：`run()` 本体只要 **51~58s**，随后**进程 10 分钟不退出**。真因：取大盘日线走 mootdx，`Quotes.factory(heartbeat=True)` 会起 `tdxpy.heartbeat.HeartBeatThread` —— **非 daemon**，主线程结束后 `threading._shutdown()` join 它 ⇒ 永不返回。
> - 两道防线：① `app/utils/mootdx_client.py::_daemonize_heartbeat()` 把心跳线程设为 daemon（**根治**）；② `scripts/skill_run.py` 的 `__main__` 显式 `os._exit(_rc)`（止血）。
> - 守卫：`tests/test_skill_run_exits.py`。修复后子进程 **43.6s 自行退出**。

**两层分工裁定（2026-10-10）**：知识型技能注方法论正文（渐进式加载），执行型技能只注分发卡片。执行型正文注进上下文是真冗余且有行为风险（诱导主对话硬跑，与「重活分发」抢方向盘）。

> 📌 **与 mimo 原生子代理的关系（别混）**：`run_skill` 是**独立子进程**跑本项目自己的流水线；mimo 的 `task`/`actor` 是**同进程子循环**（共享 env/model）。两者**正交，不要互相替代**。

> **白名单为什么留在代码里（2026-10-14 D-11 结论，别再当"硬编码"去搬）**：
> `EXECUTABLE_SKILLS` 的取值是**模块路径 + 允许函数名 + 默认函数名**（`skills.market-screener.run` /
> `("run",)`），属**代码契约**而非"阈值/口径" ⇒ 塞进 env 只会更易错、且失去重构保护。
> `qd_service._executable_skills()` 把它当**单一真源**读取（2026-10-14 起改为包内相对导入，
> 不再按文件路径加载），另见 D-11 关于元工具名单 `QD_META_TOOLS` 的下沉。

### 3.5 追责闭环（`chain/`）

**四张表**（`migrations/agent_v4_trace.sql`）：

```
qd_agent_decisions    一次对话一行(闸门不通过 ⇒ 一行都不写)
qd_agent_claims       可证伪断言(direction / magnitude / level)
qd_agent_resolutions  T+N 判定结果(verdict / score / attribution / deviation)
qd_domain_resolvers   域→策略配置(enabled / horizon / judge 采样率)
```

**三条裁定**：① 不追责的不入库（`intake_gate` 前置，宁漏不脏）；② 慢调、容忍误判（方向对即 hit 0.75，幅度差只记 deviation；但 `undecidable`/`data_missing` **不计权重** —— 常量污染 ≠ 误判）；③ 多域通用，v1 只开 finance。

**闭环**：

```
resolutions ──weight_feed.feed()──→ qd_agent_weights(layer='domain')
                                      │ EMA 慢调:α=clamp(n/50, 0.1, 0.5)
                                      ↓
                            chain/weight_hints.py(唯一读取口,TTL 600s)
                                      ├→ 工具预选 history_hint(只提示不过滤)
                                      └→ qd_service._rank_skills 排序分乘子(G)
                                           score × (0.5 + 0.5×weight),weight∈[0.5,2.0]
```

**表分层（四层快照 skill / tool / chain / domain）**：两条产线各写各层、互不覆盖——
`weight_feed` 只写 domain（resolutions 挂 claim→decision，不带 tool/skill 归因）；
skill/factor/tool/chain 由旧 `evaluator.update_weights`（traces 侧）负责。
样本纪律在写侧（n<MIN_SAMPLES 只记账），读侧不重复校验。

**判定两阶段**：阶段 A 代码算数（取真实 K 线）；阶段 B LLM judge（默认关，`QD_CLAIM_JUDGE` 开）。

### 3.6 澄清优先（`resolvers/`）

`resolvers/bridge.py` 挂在 `_run_sync` 取数**之前**：`clarify_question` 非空 ⇒ **直接反问、不进执行**
（`QD_RESOLVER=0` 关整套；`QD_RESOLVER_CLARIFY=0` 退化为"只解析不阻断"）。解析出的标的/领域注入上下文，并供技能选择使用。

⚠️ 副作用需接受：非交易日问「今天买什么股票好」会被反问。

### 3.7 grounding gate

结论中的数字必须能在本轮工具输出（`_observation_corpus()`）里溯源；无法溯源的数字改写为"未获取"并说明缺失口径。
重试给**独立步数预算**（否则主轮耗尽后重试瞬死）。仍不过 → `status="grounding_failed"`。

### 3.8 计划质量轻校验（`_plan_lint`）

`update_plan` 的清单若收尾时仍留 `in_progress`/`pending`，通常是跑偏/被截断的信号。
`_plan_lint()` 在 run 收尾落 `plan_lint` trace 事件 + warning，**只告警不拦截**。
`current_plan` 是 **per-run** 的（与步数同生命周期，run 开头归 `None`）。

### 3.9 观测计数器（只观测，不改行为）

| 模块 | 观测 | 阈值（写死在 docstring） |
|---|---|---|
| `preselect/preselect_stats.py` | 空域占比 | >2% 补域词典 → >5% 放宽 GATE → >10% 查 prompt/模型（P9 已从 `tools/` 迁出） |
| `chain/judge_stats.py` | judge vs 规则锚点分歧率 | <10% 且样本>100 ⇒ 永远别开；>20% ⇒ 建人工校准集 |

设计约束：只观测，不自动改判定行为；原因标签走 `_empty_route()` **稳定归类**（gate/no_pick/lint_void/call_error/unknown），不许存 `reason` 原文（改文案会断统计）；空域日志 keyword **`preselect-empty-face`**。

⚠️ 计数器是**进程内**的，重启归零（D-8）。

### 3.10 其余组件

| 组件 | 说明 |
|---|---|
| RAG (`rag/`) | BGE reranker + pgvector + PostgresFTS + 历史检索 |
| Memory (`memory/`) | 跨会话记忆（`remember`/`recall` 工具化） |
| LLM | **单文件 `app/agent/llm.py`（301 行，本包内）** —— **agent 链路的出口唯一**，对外给 `complete` / `acomplete` / `resolve_provider` / `LLMResponse` / `clear_model_cache`，底层是 `mimoagent.models.get_model`。<br>⚠️ **位置 2026-10-13 变过两次**：旧自建 `llm/` 子包（P2 删，7 个文件含 `LLMFactory` + `QDSkillAdapter`）→ `app.agentcore.llm`（P2~P15）→ **P15 迁回本包**。<br>⚠️ `QDSkillAdapter` **仍在**，但住在 `skills_adapter.py`（不在 `llm.py`）；`LLMFactory` 已随旧包消失，不要照旧文档找它。<br>★ **与 `app/services/llm.py` 的关系（2026-10-13 用户裁定：两套并存，各自用自己的，不合并）**：本文件管 agent 链路（`chain/`×3、`skills/base`、`capabilities/func_overlap`、`cli`、`market_cn/auto/chat_why`）；`app/services/llm.py`（723 行，`LLMService`，requests 直连）管旧分析/指标/策略链路（`routes/indicator`×2、`routes/settings`、`routes/strategy`、`services/experiment/runner`、`services/fast_analysis`×2、`services/polymarket_*`×2）。两者**不互调**：services 那套独有 JSON mode / 模型名归一化 / Gemini 原生协议 / fallback 链；本套独有 mimo tenacity 重试 / reasoning_signature 保链 / Model 缓存。**要复用对方能力须做能力平移，不要直接换出口**（换出口即回退自建轮子，属降智） |
| Capabilities (`capabilities/`) | 三级能力层，`admission.json` 准入 |
| Cron (`cron/`) | ⚠️ 被**旧系统**反向依赖（D-2） |

### 3.11 案例记忆（`utils/case_memory.py`）

run 级一次性 few-shot（CBR），填技能酿造的粒度空档。**增益层**：一切 DB/向量操作 fail-open，绝不阻断主链（但失败要 warning 可见 —— 本模块高发"声明了没接线"，静默=断链复发）。

| 环节 | 机制 |
|---|---|
| **写入** | `qd_agent._finish_collector` 后置 `_record_case` → `record_case()`；label 恒 `pending`，真值待 T+N 回填；失败坑/成本/plan_digest 齐落；**payload 已扩展为 EV-1**（下编 §13） |
| **检索/注入** | `qd_service._prefetch` → `retrieve_cases()` + `format_case_injection()`；`CASE_MEMORY_ENABLED` 总闸（默认开） |
| **取舍** | `incorrect` **硬排除**（错误结论不许以 few-shot 自我复制）；`pending` 降权 0.5×；近重复（相似度>0.95）只留最新且已验证一条；成功例给骨架+坑，失败例只给坑 |
| **分类** | `classify_case_tags` 六类：诊个股/选股/板块复盘/大盘研判/交易确认/止损复核；同型标签 ×1.2 只调序不过滤 |
| **回填** | `backfill_by_root`：T+N 定论后 pending→correct/incorrect；只写 pending 行（人工反馈 `human_reviewed` 不得被覆盖） |
| **存储** | pgvector 表 `qd_agent_cases`（JSONB 存向量 + Python 余弦，不依赖 pgvector 扩展）；无 embedding 配置退化 2-gram 词面相似 |
| **预算** | 单例注入 ≤500 tokens（实测最大 240，tiktoken） |

> ⚠️ **金例不会被压缩吞掉（但有条件）**：`_prefetch` 在 `_run_sync` 内只调用一次，注入结果拼进**首轮 user 消息**；mimo 的 `compaction/surgery.py::create_original_user_issue_anchor_message` 把首轮 user 消息原样包进 `<original_user_issue>` 作 anchor，压缩后保留。
> **风险仍在两处**：① 多轮 session 复用 agent 时，第二轮的注入**不是** anchor ⇒ 压缩即丢；② anchor 永久占用上下文，注入块越大越挤。
> 纵深防御：`_QD_COMPACT_NOTES` 已追加「must preserve the `[参考案例]`/`[参考资料]` injection blocks verbatim」；守卫见 `tests/test_v5_compaction_guard.py`。

### 3.12 工具预选共现权重（`preselect/preselect_cooc.py`）

预选底座仍是启发式（token 倒排 + 域词典），此层给它加**可学习的排序权重**：

1. `preselect_stats.record_task_calls` 落盘「任务 → 实际调用工具」对（env `QD_PRESELECT_PAIRS_FILE`）；
2. `preselect_cooc.py`：token×工具共现 `build_weights`、mtime 缓存 `load/get_weights`、加权 `rank_candidates`、验收器 `heldout_recall`、周级 CLI（`--db` 读现表 `qd_agent_traces`，**未开新表**）；
3. `qd_agent` 两钩子：过域闸门后加权目录序、收尾校对。

**铁律**：权重缺/样本<50/零覆盖/异常 → 原序返回，**冷启动与纯启发式 bit 级一致**。
验收口径：held-out 任务预选召回率 ≥95% 且 token 不回升。

### 3.13 评测三尺（谁管什么）

| 尺 | 管什么 | 产物 |
|---|---|---|
| `scripts/qd_smoke.py` | 回归（不调模型） | **257** 项断言（40 个 test 函数，2026-10-13 晚实测 255/257）；`--with-env` 把环境缺件 fail→skip。⚠️ 旧记「248 项」「256 项」是更早时点的数，现以 **257** 为准（口径演变见 §9） |
| `scripts/qd_prompt_eval.py` + `prompt_tasks.yaml` | prompt 行为（真实端点） | 10 任务/35 断言，含规划 5 题 |
| `scripts/qd_bench.py` + `bench_tasks.yaml` | 能力质量（C） | 25 题五类各 5；五维确定性打分 |
| `scripts/qd_dispatch_ab.py` | 成本对照（B） | 分发 vs 不分发单变量 A/B；`--rejudge` 不重跑复核 |

`qd_bench` 五维：coverage 30 / grounding 25（复用 `utils/grounding`）/ claims 20（复用 `chain/claims`）/ structure 15 / human 10。

---

## 4 执行流程

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
  └── 7 _finish_collector → _intake_accountability(追责入库)→ _record_case(经验留痕)
```

> ⚠️ **顺序有讲究，别随便挪**：resolver 在最前（歧义要在花钱调工具之前拦截）；weight_hints 在预选时读（要影响工具选择，就得在选之前）。

### 4.2 步数与预算语义

`step_limit` 是**单次 run 预算**，会话复用同一 agent ⇒ `_steps_taken` 必须每轮归零，否则轮次多了新消息一进来就 `LimitsExceeded` 且 `str(e)==""`（静默空结果）。**每个"附加轮"（空回复轻推 / grounding 重试）都给独立预算**。

成本预算：`QDAgent._account_budget()` 挂在 `after_step`，按**请求**累计 `_run_tokens` / `_run_steps_total` / `_run_tool_calls`，超限抛 `LimitsExceeded(hint)` ⇒ 软收尾、不硬崩。**重试轮只归零 `_steps_taken`、成本不归零**（否则空回复轻推 / 溯源重写可无限续杯）。

> 实测起因：CLI「明天买什么股」一轮 **15 次 LLM 调用、≈40 万 token、零产出** —— 模型陷入「元工具发现环 + 占位探测」。
> 真近因：`ToolProvider.search()` 的**整串子串匹配**，模型用自然语言短语检索（`run_skill 执行技能流水线`）恒 False ⇒ 零命中；而 `list_tools` 却又列出了它 —— 自相矛盾把模型搞懵。
> 已修（`tools/_provider.py::search`，P10 改名后不在 `base.py`）：**整串匹配优先，仅当零命中时**降级为「空格/中英标点分词 OR」兜底。同提问复测：**15 次 / ≈400k token → 5 次 / ≈94k token**。

### 4.3 盘后 worker

`chain/evaluator.start_eval_worker()` → 每日 15:30 等 `post_market_done` → `auto_evaluate()`：

1. 旧 `evaluate_pending()`（写 `qd_agent_traces`）；
2. `resolve_due_claims(limit=200)`（写 resolutions）；
3. `weight_feed.feed()`（写权重）；
4. `collector.run_daily_collection()`（写 context_frame + daily_universe）。

四者**独立 try，失败互不影响**；健康度看 `worker_health.last_weight_feed`。
⚠️ 四段各自扫表、各自取数 ⇒ 下编 §22 的 O-1 建议合并为单条日终结算流水线。

---

## 5 数据流

### 5.1 请求数据流

```
用户问题
  → resolver(实体/领域解析;clarify 非空 ⇒ 短路反问)
  → prefetch(记忆/RAG/技能/金例) → 预选(plan+lint+history_hint) → 激活工具
  → ReAct 多轮(每轮: 必注入层 + 已激活 schema + 历史)
  → grounding 拦截 → 文字结论
  → TraceCollector.flush() → qd_agent_traces(root/skill/tool 三层)
  → intake.record_decision() → decisions + claims(闸门通过才写)
  → _record_case() → qd_agent_cases(含 EV-1 载荷)   ← 下编的经验入口
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

⚠️ 非遗漏的设计选择：**只提示不过滤**（硬过滤与 fail-open 冲突）；**粒度只到 domain**（resolutions 不带 tool/skill 归因）；**因子层未闭环**（`get_factor_weights` 仍返回 `{}`，见 D-1；⚠️ 2026-10-14 复核：措辞不准 —— 不是 getter 恒空，是**样本层 0 行**，bull_bear 的**键名层已于 2026-10-14 对齐**（外层 factors 名 = ma/macd/rsi/boll/kdj，与权重键同名），technical_analysis 键也自洽，详见 D-1 行）。

### 5.3 执行型技能分发流

```
用户:"全市场扫一遍"
  → 主 agent 读 system「重活分发」纪律(或预选激活)
  → run_skill(name="market-screener")
       └ 子进程 skill_run.py → 白名单校验 → 执行 → 产物落盘
  → 回传预览 + full_path
  → 主 agent 引用预览写结论,明细留在文件里(不搬进上下文)
```

### 5.4 T+N 回评流向（全景；2026-10-15 补驱动器与判定口径）

```
【驱动器】app/agent/scripts/exp_grind.py（小量刷经验；不写任何合成数据，只是生产链路的驱动器）
  daily_universe 分层轮采(hot/warm/cold,同日确定) → run_agent 真实分析(每票独立 session)
    → claims/pairs 落库                                  ← S-5「daily_universe 只写不读」缺环至此闭合
                    ↓
【到期判定】盘后 worker(15:30) 或 exp_grind 跑完立即驱一次
  evaluator.auto_evaluate() → 到期 pending claims(due_date ≤ 今日)
    → resolver.resolve_due_claims()
        ├ 取真实 K 线(exec_date 为基准★;as_of 必须是 ISO 字符串)
        ├ 阶段A 算偏差 → verdict/score/attribution
        └ 写 qd_agent_resolutions(写败计 errors,不装成 resolved★)
              ↓                        ↘
   weight_feed.feed()(EMA 慢调)   calib_feed.build_samples()(pairs × resolutions JOIN)
     → qd_agent_weights(行为,§5.2)   → cells → fit_with_pooling → β₀ → p_final(§21.4)
```

判定口径表（每一行都是静默事故换来的）：

| 环节 | 口径 | 不这么写会出事 |
|---|---|---|
| `resolutions.actual` | `as_of` 等日期字段一律 ISO 字符串 | date 对象（含 windows 嵌套）过不了 `json.dumps`，被 except 吞 ⇒ **82/86 条静默失败、报表却报 resolved=86**（B1，2026-10-15 实证修复） |
| `resolve_due_claims` 统计 | `resolution_id=None` 计 errors + warn | 计成 resolved ⇒ 报表 errors=0 一切完好、实际零落库（B2） |
| 一 claim 一票 | resolutions 按 claim 取最新一条 | 同 claim 多条判定会重复投票，权重被单票放大 |
| horizon 过滤 | 只判 T+1/T+3 到期的 claim | 提前判定 = 拿未来数据评过去（as-of 破坏） |
| 校准只用 `p_base` | pairs 进 samples 只取 p_base | p_final 已含 β₀，二次代入 = 自我强化循环 |
| `n_samples=0` / `Δp≡0` | **冷启动设计行为**，不是故障 | 簇 < `QD_CALIB_MIN_CLUSTERS`(30) 只出先验，`coefs_for()` 返回 None；**不要**为看非零 Δp 去编系数。观测入口 `calib_feed.build_report()` |
| `due=0` | 正常 | T+1 claim 要下一个交易日才到期；exp_grind 快照行的 resolutions/n_samples 计数是观测面 |

### 5.5 闭环与问责总图（三环合一；专题细节在各指针章）

```
                 ┌──────────────── 追责环（每次请求都转）────────────────┐
用户问题 → ReAct → intake.record_decision → decisions ─┬─→ claims（闸门通过才写）─┐
                    （C3P 五段,§12）                   │                        │ T+N 到期
                        └─→ record_pair → pairs_*.jsonl┘                        ↓
                                                    resolver.resolve_due_claims()
                                                                    │
                                                    qd_agent_resolutions（verdict/score/attribution）
                                                          ↙              ↘
                                    weight_feed.feed() → qd_agent_weights   calib_feed → cells → β₀
                                              ↓                                  ↓
                                    weight_hints.py（唯一读取口,TTL 600s）   c3p.aggregate(coefs=β₀)
                                        ↙            ↘                            ↓
                          预选 history_hint    _skill_score(tie-break)        p_final → 下一次配对
```

| 环 | 转的是什么 | 产物 | 专题位置 |
|---|---|---|---|
| 追责环 | 一次请求的留痕 → 可判定的 claim | decisions / claims / resolutions | §3.5、§5.2 |
| 校准环 | 历史 pair 的概率位移校准 | β₀ → p_final | §17、§21.4 |
| 问责环 | verdict 反作用于工具/技能选择 | weights → 行为 | §5.2、§3.5 |

本节与 §3.5 / §5.2 / §21.4 / §F.3 的分工：那四处是**各环专题**（含代码级接线），本节是**语义级全景**——三环共用同一个 `qd_agent_resolutions` 岔口，改任一环的落库口径都必须回这里对账。

### 5.6 skill/tools 工具流（一次请求内的工具面生命周期）

```
① 分层（§3.2）  必注入层(21,每轮必带) / 二级·域内(预选集) / 三级·能力层(39,按需激活)
② 预选（§3.3）  plan_lint(加法先于减法★) → history_hint(只提示不过滤) → 激活 schema
③ ReAct 循环    每轮 = 必注入 + 已激活 schema + 历史；未激活直调就地激活
④ grounding     无依据数字拦截（§3.7）→ 文字结论 → TraceCollector.flush(root/skill/tool 三层)

能力层（三级）供给链：
  capabilities/scanner 全仓扫公开函数（SCAN_TARGETS 显式列 auto 的 hub/store/registry）
    → admission.json 人工准入（70 项；auto 只放行只读函数）
    → loader 导入 + 写操作前缀二次硬拒 + 60s 超时 + >8000 字符落 tmp/capability_output/
    → 注册为 CAPABILITY_DOMAIN（planner 不可选域，只走按需激活）

技能 vs 工具的分界：
  工具 = 一次确定性调用（转调函数，结果回注上下文）
  技能 = 多步编排（子进程 + 白名单 + 产物落盘，§5.3）
  先例：skills/strategy-debug 把 auto/tools 的 why/debug/doctor 包成 4 个工具
        （转调 CLI、零第二份规则；chat_why 是其退役前的轻量聊天壳）
```

| 坑 | 说明 |
|---|---|
| 每轮现算工具面 | 框架只在 init 算一次；不 override `get_model_query_kwargs` 则激活无效 |
| 预选输入只认 `last_user_query` | 剥掉 prefetch 注入块，否则 goal 被历史个股带偏 |
| `run_` 前缀硬拒 | admission 的 `WRITE_PREFIXES` 把 `run_*` 一网打尽 ⇒ **只读语义的桥函数取名也要避开**（§5.7 的 `agent_backtest_strategy` 即因此命名） |
| fail-open 边界 | 工具挂不杀请求但必须 trace 留痕；"看得见调不动"比挂更糟 |

### 5.7 agent × market_cn/auto 回测桥（设计态，2026-10-15 立项；未实现）

用户裁定三原则：① **独立桥**——不侵入/破坏 agent 与 auto 两套架构；② **直传策略**——agent 自己想出的策略直接给 auto 回测；③ **不污染**——不影响其它回测链路中的数据。

⚠️ **2026-10-15 复核（数据流缺口）**：立项稿把 ①② 写成 `proposal.apply` 写活 yaml、③ 写成把 `.py/.yaml` 丢进 `strategies/`、执行写成同进程 `run_all`。这三条**同时违反原则 ①③**：

| 立项稿做法 | 实际串扰 |
|---|---|
| `proposal.apply` / 改活 `strategies/<key>.yaml` | `_yaml_doc` 按 mtime 热读；生产 scan/monitor/**别人的** `run_all` 下一轮就吃到 agent 改过的门 |
| 新策略落 `strategies/` | `autodiscover` 拾取；重复 key 直接抛；`strategy_keys()` 并进 store 查询/清理范围 |
| 同进程调 `run_all` | 共享 `_REGISTRY` 单例、`strategies/*._SPEC` 懒缓存、`cross_section._POOL` 单槽、`param_scan` 式 `default_params` 实例覆写；与盘中 scan/其它回测互踩 |
| 能力层 60s / `run_*` 前缀硬拒 | 全盘 5k 票远超 60s；函数名踩 `WRITE_PREFIXES` 会被 loader 当写操作拒 |
| 接到现有 `tools/finance/backtest_tools.py` | 那是 **指标/脚本策略链**（`BacktestService` → `qd_backtest_runs`），与 auto 折叠回测是两套口径 |

修正后的数据流（原则③的根 = **子进程 + 作业目录 overlay，永不 apply 到活策略树**）：

```
agent（LLM 想出策略假设："断板后缩量回踩 5 日线，胜率/盈亏比如何？"）
 ├─ 个股/小集合（默认）→ 工具 agent_backtest_strategy（三级能力层；名字避 run_ 前缀★）
 └─ 全盘            → 技能 skills/auto-backtest（§5.3 子进程+产物落盘；能力层 60s 扛不住）
        └─ app/agent/chain/backtest_bridge.py（独立桥：载体解析 / overlay 物化 / 预算 / 归一化 / 落盘）
            ├─ ① 载体 → 作业目录 data/backtest_bridge/jobs/<job_id>/
            │     yaml overlay +（③才有）.py；validate 用 proposal.py（只校验，禁止 --apply）
            ├─ ② 预算钳制：QD_BT_MAX_DAYS / QD_BT_MAX_CODES / QD_BT_TIMEOUT / QD_BT_MAX_CONCURRENT
            └─► 子进程 python -m app.market_cn.auto.backtest_cli
                  env: QD_STRATEGY_YAML_OVERLAY_DIR=<job>  （子进程才读 overlay）
                  公开入口仍是 run_all / backtest_stock → replay → fold_range → stdout JSON
            ←─ ③ 归一化 summary（n/winrate/pl_ratio/ret_buckets + meta{git_sha,effective_params,overlay_digest}）
            ←─ ④ 桥自有 JSONL：data/backtest_bridge/runs/<date>_<job>.jsonl
        └─ 回 agent：解读结论；（可选）§5.4 记一条 claim——回测假设可被 T+N 问责
```

与现网两条回测链的关系（必须画开，否则工具预选会把「回测」送到错链）：

```
用户说「回测」
  ├─ 指标/脚本策略（strategy_id 整数）→ 已有 run_backtest → BacktestService → qd_backtest_runs
  └─ auto 折叠策略（strategy_key 字符串 / proposal / 新 .py+.yaml）
        → 本桥 → backtest_cli → 内存 trades；零写 qd_backtest_runs / qd_dragon_signals
```

策略载体三档（用户拍板三档全上；物化目标 = **作业目录**，不是活 `strategies/`）：

| 档 | 载体 | 校验/红线 | 回滚 |
|---|---|---|---|
| ① proposal | 对现有策略 yaml 的 JSON（`param`/`gate_expr`/`gate_enabled`/`gate_add`/`gate_del`） | `proposal.validate` 全套红线（路径白名单改成「overlay 里的 `<key>.yaml`」；选择集须附 stability+pool_check；不删 qualify；as-of 禁正偏移）。**禁止 `apply --apply`** | 删作业目录即回滚；活 yaml 从未改过 |
| ② gate_add | ① 的子集：只加门，expr 限 `gate_stdlib` + 该策略已注册私有函数 | 同① | 同① |
| ③ 新策略 | `.py`（`@register` + 折叠契约）+ `.yaml` 成对放进**作业目录** | 子进程 `importlib` 按路径加载，key 必须 `agentbt_<job>_*` 前缀（防撞生产 key）；折叠契约三方法非基类；yaml 过 as-of 静态检查。**禁止写入 `app/market_cn/auto/strategies/`** | 删作业目录；生产 `autodiscover` 看不见 |

auto 侧唯一允许的薄钩子（原则①的「不破坏」边界——加法，默认关闭则行为与今日逐字节相同）：

- `_paths.STRATEGY_DIR` / `evaluate.load_strategy` / `strategies._yaml_doc`：若 env `QD_STRATEGY_YAML_OVERLAY_DIR` 下存在 `<key>.yaml`，**子进程**优先读它。未设 env ⇒ 现路径。生产进程不设此 env。
- 不改 `run_all` / `backtest_stock` / replay / fold / scan / store 的判定与写域。
- `proposal.py` 已有 `--strategy-dir`，validate 直指作业目录，零改也能用。

隔离清单（原则③；「数据」= 库表 + 进程内单例 + 磁盘策略树 + 生产缓存，四层都要列）：

| 不碰 | 为什么 |
|---|---|
| 活 `strategies/*.yaml` `*.py` `config.json` `.history/` | 物化只进作业目录；禁止 apply |
| `_REGISTRY` / `_*_SPEC` / `cross_section._POOL` / 实例 `default_params` | 子进程私有；父进程（Flask/scan/其它回测）零共享 |
| `qd_dragon_signals` / `qd_watchlist` | 生产 scan/rebuild/monitor 写域 |
| `qd_backtest_runs` / `qd_backtest_trades` | 指标回测链；混口径即污染 |
| `window_cache` 写路径 | `run_all` 日线主路走 `fetch_klines_batch`（不进缓存）；禁止桥调 `load_windows`/`scan`/`rebuild`。`load_market_gain` 会读滑动缓存——桥不传这条路径 |
| `daily_universe` | agent 采集链，桥不需要 |
| `run_scan` / `rebuild` / `monitor` / `store` 写函数 | 生产写者 |
| 共享表零写 | 桥只写 `data/backtest_bridge/**` |

JSONL 一行字段：`ts / session_id / job_id / strategy_key / carrier / overlay_digest / scope(codes|all) / days / stats{n,winrate,pl_ratio} / meta{git_sha,effective_params} / pid / conclusion_ref`。

实现落点见同目录 `backtest_bridge_plan.md`（TDD 先行）。代码落点：`app/agent/chain/backtest_bridge.py` ＋ `app/agent/tools/finance/auto_bt_tools.py`（**不**塞进现有 `backtest_tools.py`）＋ `skills/auto-backtest/` ＋ overlay 三行钩子 ＋ 守卫（桥不 import auto 写者、不碰四张表、作业目录不得位于 `strategies/`）。

> **施工回填（2026-10-15，双泳道已建成；署名：Trae agent / backtest_bridge_plan·Task 9 批次F）**：
> 上面的设计与缺口分析已全部落地（9 任务，批次A-E；施工中的计划修订 v2-v6 见 `backtest_bridge_plan.md` 文末；改进 A~N 逐条锚点见 `backtest_bridge_plan.md` §0）。设计者只需知道以下**与计划初稿不同的终态结构与不变量**：
> 1. **overlay 三钩子**（批次A）：`strategies/__init__.py` 的 `_resolve_yaml_path`（`_yaml_doc` 的路径解析统一走它）与 `_import_overlay_modules`（`autodiscover` 尾部，模块名 `qd_overlay_<stem>` 防撞 sys.modules；AST 静态预检 key 必须 `agentbt_` 前缀——不 exec 就能拒；exec 失败 `sys.modules.pop` 清残留，防"命中即永不重试"）；`evaluate.py::_overlay_yaml_path`（core 不反向 import strategies，自带薄实现——刻意的双份路径逻辑，非漂移）。
> 2. **桥核心 v4 修正**：existing 载体 key 单一事实源 = `carrier["key"]`（`strategy_key` 参数仅作回退，`carrier.get("key") or strategy_key`）；proposal 形状校验前移 Phase1（畸形入参不落任何盘）；`_assert_guards` 上移 **mkdir 前**（写域白名单先于任何写盘）；作业目录包含判断以 `os.sep` 锚定（`strategies_backup` 类兄弟目录不误拒）；`backtest_cli._build_report` 补 `meta` 纯字段透传（`report["meta"]` 回读 git_sha/effective_params 契约闭环）；`_manifest_is_stale` 判死超期 running（崩溃残留不锁桥）；posix `start_new_session` 杀树不误伤桥自己；scope=codes 空 codes 直接拒绝（堵 MAX_CODES 静默绕过）。
> 3. **载体规整单源**（批次C，v5 根修）：`backtest_bridge.parse_carrier` 是工具面/技能面共用的唯一入口——分发、days 归一、形状校验只此一份，技能 `run.py` 不再有降级副本（半③档不再静默路由成 existing）。失败形态统一 finance 域约定**裸 `{"error": ...}`**（`_fn_adapter._to_output` 单键判据才置 success=False；双键 `{"ok","error"}` 会把失败误标成功）。
> 4. **源码守卫单源**（批次D）：`tests/_ast_guard.py::_ast_violations(source, *, import_prefixes, call_names, package)`——import 前缀边界匹配 + 调用名（Attribute/Name）断言 + 相对 import 按 package 解析（解析不了 = fail-closed）；三静态 import 形态全闭环。svc CLI 守卫 3 个全部 AST 级（docstring 解释权恢复，改进 N 兑现）。
> 5. **svc 泳道与口径诚实**（批次E）：manifest 以 `lane` 字段区分双泳道、`_spawn_and_wait` 共用；`backtest_svc_cli.py` 只调 `run_strategy_snapshot`，persist_run/ensure_storage_schema 仅存于 docstring（零落库是铁律不是开关）；market 枚举单源 = `app/data_sources/factory.py::SUPPORTED_MARKETS`（7 市场公开 frozenset，工厂加市场自动跟进，桥不硬编码）；`cost_model` 口径标注——显式 `0` = 零费率毛收益、`None` = 引擎默认，summary 记 `source`，不造隐式费率默认值；svc 结论按「探索性参考」进 answer（问责边界在工具 docstring，`lane=svc` JSONL 可过滤）；user_id 全链透传（工具→桥→snapshot spec→CLI→Resolver，CLI spec 驱动故不加 argv）。
> 6. **不变量**：`services/backtest.py` **零改动**（桥只读消费，回填时已核实零引用桥/钩子符号）；测试终态 `tests/test_backtest_bridge.py` 23 例 / `tests/test_auto_bt_tools.py` 24 例 / `tests/test_auto_overlay_hooks.py` 8 例，各自独立全绿（venv 解释器、backend 根 cwd，不与现网基线混跑判成败）。
> 7. **执行域钉死**（改进 F，终态约束）：一期 `exec_engine="daily"`——双泳道回测引擎执行域固定 daily（桥签名默认值即 `"daily"`，`backtest_bridge.py` 作业入口注明"由调用方保证"），intraday/auto 的 1m 精修路径取数缓存行为未验证，二期验证只读后再放开；manifest/summary 记 `exec_engine` 供审计。
> 8. **claim 闭环归属**（改进 G，终态裁定）：**桥不记 claim**——回测结论不直接写 claims 表，agent 把结论写进 answer，由主请求 finalize 流程经 `chain/intake.record_decision`（闸门→提取→落库）自动接住；JSONL 只留 `conclusion_ref`（=session_id）作关联。svc 泳道结论按「探索性参考」表述（`lane=svc` 可在 JSONL 过滤），不与 auto 泳道结论同权重进 T+N 判定闭环。⚠ 澄清：上文立项稿数据流图中「（可选）§5.4 记一条 claim」字样已被改进 G 终态裁定取代——桥不记 claim，主流程接住，该行仅存历史价值。
> 9. **E/A/I 落点补充**：`apply --strategy-dir <作业overlay> --apply` 是唯一物化通道（禁的是活树 apply；`.history` 备份随 `--strategy-dir` 落在作业目录内）；摘要只搬 `stats`，**桥绝不重算指标**（口径单源 `core.backtest._summary`）；子进程固定 `cwd=backend_api_python` 根（CLI 的 `.env` 读取与 `python -m` 模块解析都依赖它）。
> 10. **状态澄清**：§5.7 回测桥自 2026-10-15 起为**已建成终态**（本章标题及 §8 配置表中「设计态/未接线」表述仅存历史价值）；§8 表中回测桥 env 默认值与代码不符——以 `backtest_bridge.py` 常量区为准：`QD_BT_MAX_DAYS=365` / `QD_BT_TIMEOUT=900` / `QD_BT_MAX_CONCURRENT=1` / `QD_BT_JOB_TTL_DAYS=7`（`QD_BT_MAX_CODES=50` 不变），表值 120/50/120/1 系立项稿预填，勿按表配置。

---

## 6 精巧处

1. **每轮现算工具面**：override `get_model_query_kwargs` —— 框架只在 init 算一次，不 override 则激活无效。
2. **预选输入只认 `last_user_query`**：剥掉 prefetch 注入块，否则 goal 被历史个股带偏。
3. **lint 加法先于减法 + 有补位不作废**：减法性规则曾把资金流工具全清空。
4. **域兜底 `fallback_domain_tools`**：宁可补 4 个也不能落回裸工具面。
5. **分层 fail-open**：既省每轮 token，又不制造"工具看得见调不动"的死胡同。
6. **未激活直调就地激活**：把"分层"的成本转嫁给真正用到的那一次调用。
7. **技能渐进式注入 + 会话去重**：同一技能连续命中时从 2419 → 101 字符。
8. **resource 路标只在 `list_resources()` 非空时才给**：无条件给会诱导必然失败的调用。
9. **指针不写"见上方历史"**：上下文压缩会改写历史 ⇒ 指针必须给 `read_skill(name=...)` 自取全文的路径。
10. **执行型技能走子进程白名单**：超时可击杀 + 崩溃隔离 + 主上下文 O(摘要)。
11. **追责闸门前置**：判不出来就不入库（宁漏不脏），避免用噪声训练权重。
12. **观测只计数**：不让观察演变成自动调参。
13. **执行型技能只注分发卡片**：正文既费 token 又诱导主对话硬跑。
14. **权重乘子冷启动 bit 级一致**：`score × (0.5+0.5×weight)` 但阈值仍看原始词典分 —— 调序不过滤。
15. **技能空结果也是结果**：盘后空筛是合法结论，不得回退主对话硬跑重现一遍。
16. **判定口径单一真源**：`chain/label_spec.py` 一张 `HOLDS` 表，持有期单位恒为**交易日**，旧名不留别名（附录 C）。
17. **Δp 的对照臂是免费的**：`p_base` 由冻结模型顺手算出，零额外采集（下编 §11）。
18. **不可复算的证据只作分组变量**：防止把主观判断合法化地塞进点估计（下编 §18.2）。

---

## 7 易错点与设计陷阱

### 7.1 导入与路径（**P5 起统一 `app.agent.*`**；下表前两条是坑史，后三条仍有效）

> ✅ **P5 导入现代化（2026-10-13）已根治本节前三条的病根**：`app/agent/` 不再进
> `sys.path`，全包统一**单一模块名 `app.agent.*`**（`app/__init__.py` 头注即记录），
> M-1「同一份源码两套 module 对象」病根消失。实测**活代码裸名 import 0 处**
> （仅 `tmp/*.bak` 备份件残留），`ensure_path_order()` **全仓已不存在**。
> ⇒ 下表标注 ✅ 的三条**不要再照着改代码**，保留只为理解历史；标 ⚠️ 的三条仍然有效。
> ⚠️ **守卫缺口（2026-10-13 发现）**：P5 的红线守卫 `tests/test_v6_import_modernize.py`
> （断言零裸名 / 零 sys.path / 零断裂引用）已随 **P16 归档**到
> `del/agentcore_retired_20261013/tests/`，**现无守卫** ⇒ 有人重新引入裸名 import
> 或 `sys.path` 注入不会 FAIL。建议从 `del/` 取回该守卫（它守的是 P5 契约，与
> agentcore 是否退役无关），或补一条等价断言。

| 坑 | 说明 |
|---|---|
| ✅ **（P5 已根治）`app/agent` 必须排在 `app/` 之前** | 旧世界：裸名 `import utils` 会命中 `app/utils`（23 个裸名全错位），故有 `ensure_path_order()` 保序。**现况**：裸名 import 与 `ensure_path_order()` 均已随 P5 删除，**无需再管 sys.path 顺序** |
| ✅ **（P5 已根治）`chain.X` 与 `app.agent.chain.X` 双模块实例** | 旧世界：`app/agent` 被单独加进 `sys.path` ⇒ 两套路径各有一份模块、模块级常量互相独立，测试须 patch 两条路径。**现况**：单一 `app.agent.*`，只 patch 一条 |
| ⚠️ **`skills/market-screener/common.py` 的路径层级** | 曾按"..×3"算成 3 层，实为 4 层 ⇒ 把 `app/` 插到 sys.path[0] ⇒ 9 个 finance 工具静默挂掉，而**能力层补位把缺口掩盖了**。改包内路径时仍按实数层级核 |
| ⚠️ **`cron_worker` 不是顶层名** | 必须写全 `from app.agent.cron.cron_worker import start_cron_worker`（`app/__init__.py` 即此写法）。裸名 `cron_worker` 根本不存在 ⇒ ImportError 被吞 ⇒ 定时任务从未启动 |
| ⚠️ **判定"是否真执行"查标志位** | 别靠日志（logger propagate 不可靠），查 `_worker_started` 这类状态标志 |
| ⚠️ **`python -m` 跑 smoke 会误报** | `-m` 会先导入 `app.agent.scripts` 包，留下长名键 ⇒ test_29 判"无 `app.agent.*` 长名键"失败。必须 `python app/agent/scripts/qd_smoke.py` |
| ⚠️ **扫 import 要覆盖三种形式** | `A.B.C` 连写、`from A.B import C`，**以及相对 import**（`from .rag.retriever import X` 须按所在包解析成绝对名）。只扫前两种会把包内相对引用漏成"零引用"（2026-10-13 差点据此误删 `rag/`/`audit/`） |

### 7.2 工具面

| 坑 | 说明 |
|---|---|
| **管线函数被注册成工具** | ✅ **已由 P10 根治（2026-10-13）**：非工具模块统一改 `_` 前缀（`_base` / `_provider` / `_em_utils` …），扫描器对 `startswith("_")` 天然跳过 ⇒ 目录名即契约。**不再需要任何名单**。⚠️ **旧文档让你"补 `_NOT_TOOLS`"（见 D-3 / O-11 / 附录 F D-6）—— 那是 P10 之前的方案，别照着做，加了等于回退**。新模块不是工具就直接改下划线前缀 |
| **模块导入失败 = 静默降级** | 只 warning 没人看 ⇒ 落 `_TOOL_MODULE_IMPORT_FAILURES`，由 smoke test_30 断言为空 |
| **聚合摘除 key 要抹顶层前缀** | `_SUPERSEDED_BY_MERGE` 的 key 用「子目录.模块名:函数名」，否则长名/短名周期命中不一致 |

### 7.3 数据库与统计

| 坑 | 说明 |
|---|---|
| **游标行是 dict（RealDictRow）** | 必须 `row['k']`；`row[0]` KeyError、`for a,b in rows` 解包出列名（全静默）。裸 psycopg2 那几处是 tuple 行，**别一起改** |
| **`SELECT COALESCE(MAX(id),0)` 必须起别名** | 否则列名是 `coalesce` ⇒ 清理逻辑被静默跳过 |
| **`%m-%d` 丢年份** | 日期一律 `%Y-%m-%d` |
| **统计推断三坑** | ① AUC 写成补角；② 平移置换只能平移"标签序列"，平移值序列零分布退化；③ **跨基数比较**（过滤后 n 变小/NaN 静默丢样本 ⇒ "过滤提升"多为假象） |
| **★ 名义案例数 ≠ 独立簇数** | 同一只股连续几天的案例高度相关，UI 上「案例数 30」会让使用者高估置信度。**所有对外计数必须双报**（下编 §10.4） |

### 7.4 行为一致性（prompt 与代码会漂移）

| 坑 | 说明 |
|---|---|
| **prompt 文案 vs 代码行为** | 曾出现 `qd_system.txt` 写"未激活直接调用会失败"，而代码是 fail-open —— 模型被误导绕远路。**改分层/改 fail-open 必须同步 system prompt** |
| **compaction 会改写历史** | 任何"见上方历史"式的引用都会失效。指针必须给可自取的 `read_skill(name=...)` |
| **会话去重键要同口径** | `_skill_seen` 的键必须 `str(session_id)`，与 `_get_agent` 一致 |
| **格式化类组件 = 一整轮 LLM** | `formatters/` 已因延迟摘除（稳态 11.7s）。恢复前必须满足：有超时 / 轻量模型 + max_tokens 上限 / 可选触发 |

### 7.5 验证方法

- **负向验证必须确认缺陷真注入**：曾因 heredoc 转义出错导致缺陷没注入却跑出全绿，差点误判"护栏失效"。改为落盘脚本（`inject`/`restore`）+ 断言还原后与原文字节相等。
- **env 类改动优先用真旋钮做负向验证**：如 `QD_ON_DEMAND_TOOLS=actor` 复跑。
- **护栏判据要可证伪**：新增判据后跑一次「注入缺陷 → 必须 FAIL → 还原 → 必须 PASS」。口径收口批的变异检验实测 **8/8 全被抓到**。
- ~~⚠️ `llm_inputs.log` 的 `TYPE=` 标签是启发式，不等于实际用途~~（2026-10-15 **已根除**）：该调试转储（`mimoagent/models/openai_chat.py::_debug_dump_messages`，受 QD_LLM_DEBUG 控制）是历史调试产物，随 mimoagent 重装为纯净上游版（只打 `patches/mimoagent_windows.patch`）已从包内移除，`llm_inputs.log` 不再产生。保留两条教训：① **第三方包内的行为升级即被覆盖，别去改它**；② 启发式二分标签曾把所有不带 tools 的轻量调用错标成 `PRESELECT预选`（含 C3P 证据抽取 / resolver），排查看 system prompt 别信标签。

---

## 8 配置说明（`QD_*`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `QD_ACCOUNTABILITY` | 1 | 追责总开关 |
| `QD_CLAIM_JUDGE` | - | 阶段B LLM judge（默认关） |
| `QD_TOOL_PRESELECT` | 1 | 工具预选总开关 |
| `QD_PRESELECT_GATE` / `_HINTS` / `_MAX` | 1 / "" / 8 | 域特征闸门与预选上限 |
| `QD_TOOL_TIER_MODE` | tiered | 分级口径 |
| `QD_EXTRA_CORE_TOOLS` | "" | 追加必注入工具 |
| `QD_ON_DEMAND_TOOLS` | `actor,run_skill` | **反向旋钮**：把必注入降级为按需 |
| `QD_MAX_ACTIVE_TOOLS` / `QD_AUTO_ACTIVATE_MAX` | 12 / 8 | 激活上限 |
| `QD_CAPABILITIES` | 1 | 三级能力层总开关 |
| `QD_TOOL_PROBE_LIVE` / `_TTL` | "" / 600 | 启动期可用性探测 |
| `QD_CONTEXT_WINDOW` / `QD_COMPACT_WINDOW` | 65536 / - | 上下文窗口（统一由 `_context_window()` 读） |
| `QD_COMPACT_NOTES` / `QD_COMPACT_RATIO` / `QD_COMPACT_THRESHOLD` | - / 0.55 / - | 压缩纪律与触发阈值 |
| `QD_STEP_LIMIT` / `QD_STEP_SCALE` / `QD_EVAL_STEP_LIMIT` / `QD_EVAL_TIMEOUT` | - / 3 / 8 / 240 | 步数与超时 |
| `QD_WEIGHT_TTL` / `QD_FEED_MIN_SAMPLES` | 600 / 5 | 权重快照 / 慢调最小样本 |
| `QD_RESOLVER` / `QD_RESOLVER_CLARIFY` | 1 / 1 | 实体解析总开关 / 只反问 vs 只解析 |
| `QD_PROVIDER_MAP` / `QD_CODE_VERSION` | - / unknown | 供应商映射 / 版本标识 |
| `QD_PRESELECT_PAIRS_FILE` / `_COOC_FILE` / `_MIN_PAIRS` | - / - / 50 | K：配对落盘 / 共现权重 / 加权生效最小样本 |
| `QD_BENCH_DIR` | - | 评测与对照报告存档目录 |
| `QD_DISPATCH_COST_MAX` / `QD_DISPATCH_QUALITY_TOL` | 0.40 / 5 | B 对照判定阈值 |
| `QD_AB_ARMS` | `dispatch,inline` | 分发 A/B 对照臂顺序 |
| `CASE_MEMORY_ENABLED` | 1 | 案例记忆总闸（⚠️ 非 `QD_` 前缀） |
| `AGENT_RUN_TOKEN_BUDGET` / `AGENT_RUN_TOOL_CALL_BUDGET` | 200000 / 60 | 单 run 成本硬预算（已接线：`_account_budget`） |
| `AGENT_DAY_TOKEN_QUOTA` | 2000000 | 单用户日 token 配额 ⚠️ **仍未接线**（`add_day_usage` 无调用方） |
| `QD_MAX_OBSERVATION_CHARS` | 30000 | observation 字符闸门（上游设为 0=关闭，本项目必须开） |
| `QD_RUN_STEP_CAP` / `QD_RUN_STEP_GRACE` | 0 / 3 | 单请求累计步数上限 |
| **下编新增** | | |
| `QD_BAR_FREQ` | `1d` | 证据的 K 线频率（`label_spec.bar_freq()`）；分钟线暂不开库 |
| `QD_EV_MAX_CELLS` | 12 | 条件空间上限（格） |
| `QD_EV_KEY_DIMS` | `regime,vol_bucket` | 条件键维度（可选 `theme_heat`，加上就 27 格超预算） |
| `QD_AGG_LOGIT_MAX` | 0.4 | Δlogit 上限（约 ±10pp） |
| `QD_CF_VOL_LOW` / `_VOL_HIGH` | 0.12 / 0.22 | 情境帧波动分档（年化） |
| `QD_CF_HEAT_LOW` / `_HEAT_HIGH` | 0.5 / 1.5 | 情境帧题材热度分档（头部板块均涨幅 pct） |
| `QD_CF_INDEX_CODE` / `_KLINE_DAYS` / `_CLOSE_HM` | 000001 / 30 / 15:05 | 情境帧基准指数 / 回看天数 / 收盘时刻 |
| **C3P 接线（P2 新增）** | | |
| `QD_C3P_ENABLED` / `QD_C3P_MAX_CLAIMS` / `QD_C3P_BARS` | 1 / 2 / 120 | C3P 总开关 / 一次最多处理几条 claim / 取数根数 |
| `QD_C3P_LLM_MODEL` | `""`（=用全局模型） | ⚠️ **建议单独指定轻量快模型**：证据抽取是轻量 JSON 活，不需推理模型。未设时吃全局模型（`mimo-v2.6-pro` 类慢模型）⇒ 又慢又贵，直接加剧超时 |
| `QD_C3P_LLM_TIMEOUT` | **20** 秒 | ⚠️ **20s 对推理型模型 + 慢网关偏紧**（2026-10-13 实测当日 `APITimeoutError` 129 次、`APIConnectionError` 8 次；网关 `api.g2claw.com` 间歇性不稳）。超时本身不阻断主链（`extract_evidence` fail-open 返回 None 降级），但 mimo 的 tenacity 硬编码 5 次退避（4→60s）最坏要等 5×timeout 才放弃，用户体感是"卡住"。建议按网关实测中位延迟放大到 45~60s，或换轻量模型 |
| **§5.7 回测桥（设计态，未接线）** | | |
| `QD_BT_MAX_DAYS` / `_MAX_CODES` / `_TIMEOUT` / `_MAX_CONCURRENT` | 120 / 50 / 120 / 1 | 个股工具预算；全盘技能另用更宽超时。并发默认 1，避免与 scan 抢核 |
| `QD_STRATEGY_YAML_OVERLAY_DIR` | 空 | **只许子进程设置**。该目录下存在 `<key>.yaml` 则 `load_strategy`/`_yaml_doc` 优先读它。生产进程不设 |

> ⚠️ 上下文窗口**统一由 `QDAgent._context_window()` 读**：`QD_CONTEXT_WINDOW` > `QD_COMPACT_WINDOW`（旧名兼容）> `OPENAI_MAX_TOKENS` > 32768。
> ⚠️ 下列**非 env** 开关在 `QDAgentConfig`（继承自 mimo）：`AntiHackGuard` **默认开**；`wrap_up_hint` **保持关**。

---

## 9 回归基线

| 项 | 实测 |
|---|---|
| `compileall app/ scripts/` | ✅ 通过 |
| `qd_smoke.py` | ✅ **257 项**（40 个 test 函数）；本机 `--with-env` **255 passed / 0 failed / 2 skipped**（2 skip 均为 `del/` 归档缺件）。⚠️ 口径演变：P12 时点写「256 项」、附录 F C-14 写「256 项 / 254 pass」，**2026-10-13 晚实测 255/257**，以本行为准 |
| `qd_prompt_eval --validate` | ✅ 10 条任务 / 35 项断言；plan_* 断言 **17/19** |
| `qd_bench` | 25 题；真实端点首跑 **71.7**（coverage 81.5 / grounding 86.8 / claims 38.0 / structure 62.6） |
| `qd_dispatch_ab` | 首跑成本比 **0.889**（未达 0.40，见 D-18）、质量Δ +0.5 |
| v5 单测（pytest） | **162 passed / 1 skipped**（**14 个** `test_v5_*.py`；2026-10-14 P3 接线新增 `test_v5_calib_feed.py` 21 条 + `test_v5_c3p.py` 接线守卫 3 条 ⇒ 138 → 162） |
| 全量 `tests/` | **716 passed / 4 skipped**（**2026-10-14 O-11 收口后实测**）。⚠️ 口径演变：P12 后曾为 **761 passed / 4 skipped**；P16 归档 6 个 `test_v6_*` 到 `del/agentcore_retired_20261013/tests/` 后降为 652；2026-10-14 P3 接线 +24 ⇒ 676；同日 O-11 三项收口再 +41（新增 `test_v6_skill_seen_lru.py` 15 / `test_v6_d11_whitelist_knobs.py` 13 / `test_v6_cron_boundary.py` 13），同时因 `_pagination` 归档删除 1 个参数化用例 ⇒ **716**。更早期版本写「529 passed」是 P12 之前的数 |
| 工具面 | 注册表 **92**（工具 53 + 能力层 39）；必注入 **21** / 按需 **71** |
| schema 体积 | 全量 **49,507 字符** → 必注入（common+meta 11 个）**4,188 字符** |
| 技能 | **6** 个目录，其中可执行 **2** 个 |
| `formatters/` | 不在生产包内（已归档 `del/agent_formatters_20261001/`） |

⚠️ smoke 输出里的 Traceback 多来自 `chain/store.py save_tree` 在无 `DATABASE_URL` 时的 fail-open ERROR（smoke 不加载 `.env`）—— 已知、非故障，别误判。

**跑法**：`cd backend_api_python && python app/agent/scripts/qd_smoke.py`（**不要用 `-m`**，原因见 §7.1）。

---

# 下编 · 条件化预测引擎（目标态）

> 本编来自 `agent扩展设计-v5.md` 全文 + `agent扩展设计-v4.md` 的统计地基（§7 功效预算、§28 实测、§3.2 特征三分、§8.4 支撑度门槛、§17.3 分层、§19.3 两腿分离、§23.3 `evidence_class`）。
> **吸收 v4 的目的**：v3 自包含，读本文不需要再翻 v4。v4 保留作原始论证，但**统计结论以本编为准**（本编已含 v4 之后的新实测）。

## 10 统计地基（先读这一章，否则后面全是空谈）

### 10.1 瓶颈是独立簇数，不是案例数

A 股日频、T+3 窗口、150 只/日 ⇒ 名义 **36,450 条/年**。但这些样本彼此**高度相关**，折算后只有 **361 个独立簇/年**。

**实测（v4 §28，可复现）**：

| 量 | 实测值 | 含义 |
|---|---|---|
| 平均成对相关 ρ̄ | **0.2191** | 主导成分是**市场共同因子** |
| PC1 解释方差 | **24.1%**（谱参与率 14.3/500） | 相关性不是噪声，是有结构的 |
| 同题材内 ρ̄ | **0.2416** | 比全体只高 **+0.0226** ⇒ 题材分层是**次要的** |
| 日设计效应 `DEFF_day` | `1 + 149×0.2191 = ` **33.6** | 150 只/日 ≈ **4.5 个独立观察/日** |
| T+N 窗口重叠相关 | `(N−1)/N`（T+3 = **0.667**） | 相邻窗口天然重叠，是时序相关的真实来源 |

### 10.2 独立簇与 MDE 表

样本公式：`n = 7.8489 · p̄(1−p̄) / Δ²`；反解 MDE：`Δ = √(7.8489 × 0.25 / n)`（p̄=0.5）。

| 窗口 | 独立簇/年 | MDE（可检测最小改进） |
|---|---|---|
| T+1 | 1,083 | **4.3pp** |
| **T+3（主判据）** | **361** | **7.4pp** |
| T+5 | 217 | 9.5pp |
| T+20 | 54 | 19.0pp |

单标的纯时序（一只股连续分析一年）：T+3 = **81 簇 / MDE 15.6pp**。
⇒ **跨标的池化是最大的功效杠杆**：MDE 从 15.6pp 降到 7.4pp。

### 10.3 条件空间必须按簇预算反推

```
max_cells = floor(独立簇数 / 每格最小簇数) = floor(361 / 30) = 12 格（T+3，年 1）
```

默认 `regime × vol_bucket` = 3×3 = **9 格** ≤ 12 ✅；加 `theme_heat` ⇒ **27 格** ❌ 超预算。

### 10.4 计数必须双报（硬要求）

**同一只股连续几天的案例高度相关**（窗口重叠 + 基本面/资金面延续）。看起来「这只股攒了 30 条案例」，实际只等于 **1–2 个独立事实**。这是整个经验库里**最容易骗人的地方**。

⇒ 所有对外展示的计数必须同时给出 **名义案例数** 与 **折算后独立簇数**；注入决策（§18.3）以独立簇数为门槛，**不以案例数为门槛**。

### 10.5 特征三分（PIT 纪律的依据）

| 类 | 内容 | 能否回溯重建 |
|---|---|---|
| **A 类** | OHLCV 派生（量比、均线乖离、连板数……） | ✅ 可重建 |
| **B 类** | 历史已落库的横截面（板块统计、成分股排名） | ⚠️ 可重建但带**成分股前视污染**，须先修 |
| **C 类** | 实时 API 现算（当时盘口情绪、题材热度、市场态） | ❌ **不可重建，只能前向积累** |

⇒ C 类「不急，但必须现在开始」：晚一天就永久少一天。这就是 `chain/context_frame.py`（每日一帧）存在的理由。

### 10.6 支撑度门槛（v4 §8.4）

| 用途 | 门槛 |
|---|---|
| 进**呈现位**（给用户看） | `n_eff ≥ 30` **且** 独立簇 ≥ 10 |
| 进**升格位**（当作可信经验影响行为） | 独立簇 ≥ 30 **且** out-of-time 验证 **且** BH-FDR 通过 |

### 10.7 `evidence_class` 三值（分开报，不合并）

| 值 | 含义 | 能否报 agent 判断质量 |
|---|---|---|
| `live` | 无污染的前向证据 | ✅ **唯一可报**，headline 只取它 |
| `rule` | 固定规则回测产出（无 LLM） | 可以报，但与 live 分开 |
| `replay` | agent 重放历史（**有污染**） | ❌ 只作探索，**不得当作「经验有效」的证据** |

---

## 11 第一性原理：换 estimand，而不是等样本

### 11.1 老 estimand 为什么贵

要回答「有经验的一组比没经验的一组命中率高多少」= **两独立样本的比例检验**：

- 每臂 `n = 7.8489·p̄(1−p̄)/Δ²`，Δ=5pp 时每臂 783、双臂 1566；
- 再除以 `DEFF` ⇒ 落到 361 簇 ⇒ **MDE 只能到 7.4pp**。

贵的根源有三处：

| 贵的根源 | 老现状 | 解法 |
|---|---|---|
| ① 两组**独立**样本，方差含个体差 | 必须两组，样本翻倍 | **配对**：同一个 (标的, 日期, 情境) 上跑两次预测，用差的方差 |
| ② 结果是**二值**（hit/miss） | 信息量最粗 | **连续量** `Δp`，可用 t 检验 / 符号检验 / 回归 |
| ③ 对照臂要**额外采集**（20–30 只/日，成本 +13~20%） | 没对照臂就算不出 lift | **天然对照臂**：`p_base` 来自冻结模型，零额外成本 |

### 11.2 新 estimand：Δp 的符号正确性

```
p_base  = 冻结基线模型的概率（不用经验）        # app/watchlist/predict.predict_next_day
p_final = p_base ⊕ 经验校准项                  # 定量聚合层，见 §15
Δp      = p_final − p_base                     # 经验带来的概率位移（连续、可正可负）
```

结果标签沿用既有口径，不新建：`chain/schema.py::classify_return`（`±0.3%` 死区，`DIRECTION_THRESHOLD=0.003`）⇒ `y ∈ {+1, 0, −1}`。

**主判据（预注册，写死）**：

```
H0: sign(Δp) 与 sign(y) 的一致性 = 随机（0.5，剔除 y=0）
H1: 一致性 > 0.5，且按聚类折算后显著
```

即：**「经验把我推的方向，是不是更可能是对的」**。

### 11.3 配对带来的功效增益（⚠️ `ρ_pair` 未实测 —— G19）

`Var(Δp) = 2σ²(1−ρ_pair)` ⇒ 样本需求降为独立设计的 `(1−ρ_pair)` 倍。

| ρ_pair | 功效倍数 | T+3 的 MDE（基线 7.4pp） |
|---|---|---|
| 0.3 | 1.43× | 6.2pp |
| 0.5 | 2.0× | 5.2pp |
| 0.7 | 3.33× | 4.1pp |
| 0.8 | 5.0× | 3.3pp |

> ⚠️ **`ρ_pair` 目前没有实测值**（`p_base` 与 `p_final` 尚未成对落库）。上表是**条件表，不是承诺**。
> **G19**：P2 真正接线后第 4 周第一件事就是实测 `corr(p_base, p_final)`；实测前不许对外报「把可检测下限降到了 X pp」。**若 `ρ_pair < 0.2`，上表作废，退回绝对口径。**

### 11.4 这违反「两腿只对比、不加权平均」吗 —— 不违反

红线是：**两条腿的汇合点只能是对比表，不能是加权平均**。

- `Δp_evidence` **本身就是对比量**（经验相对基线的位移），不是「两个模型各占一半权重」；
- 两腿的产出始终**分开落库、分开报数**：`p_base`（量化腿）、`Δp`（经验腿）、`p_final`（合成）三条都存；
- 任何 headline 数字只取 `Δp` 的检验结果，`p_final` 只用于实盘输出；
- `Δp=0` 时系统**退化为纯基线**（fail-open），经验挂了不会带崩预测。

⇒ **相加是合成，不是融合**：融合会把两条腿的错误混在一起再也分不开，合成随时可以拆开归因。

---

## 12 C3P：条件化预测流水线（五段）

```
① 情境键抽取  →  ② 证据生成  →  ③ 定量聚合  →  ④ 配对产出  →  ⑤ 记账校准
   (确定性)       (LLM)          (无 LLM)        (天然对照)      (条件校准)
   0 token        ~400 token     0 token        0 额外成本      盘后 T+N
```

| 段 | 做什么 | 是否用 LLM | 成本 | 产出 |
|---|---|---|---|---|
| ① 情境键 | `regime/vol_bucket/theme_heat` → `condition_key` | 否 | 0 | 冻结字符串键 |
| ② 证据生成 | LLM 读数据 → 输出**证据向量**（§13） | **是** | 一次推理 | `EvidenceVector` |
| ③ 定量聚合 | `p_base` + β₀ + Σβⱼ·eⱼ ⇒ `p_final` | 否 | 0 | `p_base`、`p_final` |
| ④ 配对产出 | 同一次推理同时落 `p_base`/`p_final`/`Δp` | 否 | **0 额外** | 一条 pair 记录 |
| ⑤ 记账校准 | T+N 后回写结果，更新该条件格的校准参数 | 否 | 0 | 校准表更新 |

**段 ④ 是整个成本杠杆**：老设计要「每日 20–30 只同池随机对照、成本 +13~20%」；新设计里对照臂是 `p_base` —— 冻结模型对**每一个被分析的标的**都能顺手算出，不需要额外跑一只票、不需要额外一个 token。**G7 因此免费关闭**。

### 12.1 LLM 角色的重定义（这是最硬的一条）

| | 老设定 | v3 |
|---|---|---|
| LLM 输出 | 方向 + 置信度（**一个概率**） | **证据向量 + 证伪条件**（**没有概率**） |
| 概率从哪来 | LLM 自报 | 定量聚合层（可校准、可回测、可替换） |
| 置信度可信吗 | 不可信（LLM 自评校准差） | 不需要 LLM 自评；置信度 = 聚合层输出 + 条件格的 `n_eff` |

**红线 R1：LLM 输出的概率字段一律丢弃并记违规。** 若模型在证据向量外自行给出「上涨概率 X%」，该字段不入聚合器，只入 `violations` 计数 —— 它是提示词纪律的体温计。

> 实现落点：`chain/evidence.py::_strip_llm_probability`（生成侧）+ `chain/aggregator.py::_scan_r1`（聚合侧再拦一道）。两侧 `_P_KEYS` 覆盖 `p_up/p_down/p_up_pct/probability/prob/confidence_llm/llm_confidence/p_llm/win_prob/胜率/概率`。

---

## 13 EV-1 证据向量契约（冻结字段，版本化）

```jsonc
{
  "ev_ver": "ev1",
  "condition_key": "neutral|low",           // ① 段产出,冻结键集合;取不到标 unknown(不猜)
  "subject": {"code": "002031", "kind": "stock"},
  "claims": [                                // 可验证声明(沿用 chain/claims.py 闸门口径)
    {"direction": "bullish", "window": "T+3", "magnitude_pct": null}
  ],
  "discriminators": [                        // 命中的判别特征键(≤5 个)
    {"key": "vol_ratio_gt_2_and_ma20_up", "value": 1, "computable": true}
  ],
  "falsifiers": [                            // 证伪条件,≥1 条
    {"key": "vol_ratio", "op": "<", "threshold": 1.2,
     "meaning": "若次日量能掉到 1.2 倍以下,我的放量启动判断不成立"}
  ],
  "evidence_strength": 0.9,                  // 随内容计算(0.2 起 +claims .2 +disc .2 +fals .3 +frame .1)
  "violations": [],                          // R1 拦截记录
  "provenance": {
    "producer_ver": "ev1-producer",
    "prompt_sha": "...",                     // 与 utils.tracing._repro_meta 同一口径
    "evidence_class": "live",                // ★ 见 §10.7 三值
    "bar_freq": "1d",                        // label_spec.bar_freq()
    "as_of": "2026-10-12"
  }
}
```

**字段纪律：**

1. `computable` 是分流开关（关键设计，见 §18.2）：
   - `computable: true` ⇒ 可被客观复算 ⇒ 允许进聚合器的 βⱼ 点估计；
   - `computable: false` ⇒ 主观判断 ⇒ **只能作分组变量进 `condition_key` 的扩展维**，永远不能直接当 βⱼ 的自变量。**理由：不可复算的量无法 PIT 化，进点估计就是合法化的过拟合。**
2. `evidence_strength` 只用于**事后分析**（看 LLM 自评与 Δp 正确性的关系），**不进 `p_final` 的判定口径**。
3. `falsifiers` 缺失 ⇒ 该次预测降级：不进正式判定队列，只记账（§19.1）。
4. `ev_ver` 变更 = 生产者版本变更 = 校准参数必须重新积累。

> ⚠️ **代码现状偏差**：`chain/evidence.py::_evidence_class()` 当前返回 `weak/medium/strong`（按有无 falsifiers/discriminators 判），与 §10.7 的 `live/rule/replay` **不是同一组语义**。前者是「这次证据的强度」，后者是「这条证据的污染等级」。两者都需要，但**不能互相顶替** —— 见附录 F 偏差表 B-3。

---

## 14 条件空间：定义与格子封顶

### 14.1 条件 = 情境维 × 证据维

```
condition_key = f(情境维) × g(证据维)
  情境维（冻结，年 1 只开下列单维，不做笛卡尔）:
    layer   ∈ {L1板块, L2题材, L3个股}          # 与 §17.1 分层对齐
    regime  ∈ {weak, neutral, strong}
    vol_bucket ∈ {low, mid, high}
    theme_heat ∈ {cold, warm, hot}             # 默认不开（开了就 27 格超预算）
    window  ∈ {T+3, T+5}
    bar_freq ∈ {1d}                            # 分钟线暂不开库
  证据维（动态，来自 §18 挖掘，年 1 上限 4 个键）
```

**实现**：`chain/evidence.py` —— `_DIM_CARD = {"regime":3,"vol_bucket":3,"theme_heat":3}`；`_DEFAULT_DIMS = ("regime","vol_bucket")`；`cell_count()` 算当前格数；`max_cells()` 读 `QD_EV_MAX_CELLS`（默认 12）；`assert_cell_budget()` 超预算**如实返回 False 并 warning**，不静默、不崩。

### 14.2 格子数必须按独立簇预算反推（硬约束）

见 §10.3：**年 1 上限 12 格，且必须全部预注册**。新增一格 = 从现有格子里挤掉一格或等下一年。

### 14.3 键的冻结与版本化

- 情境维键集合冻结在代码里（`chain/evidence.py`），改动即 `COND_VER += 1`；
- `COND_VER` 变更 ⇒ 所有既有条件格的 `n_eff` 归零重算（不许跨版本合并）；
- 制度断点（如涨跌停规则变更）两侧的条件格**禁止合并**；
- **G23**：必须有一张 `exp_cond_registry`（键 / 提出日期 / 依据 / 预期方向 / 样本预算 / 状态），**未登记格一律不进判定**。

### 14.4 聚类口径裁定：以 `date` 为主

v4 §28 实测（§10.1）：同题材 ρ̄=0.2416 vs 全体 0.2191 ⇒ **题材内增益仅 +0.0226**。

⇒ **裁定：聚类单位以 `date` 为主**，`date×theme` 只在有明确证据支持时才用；条件键默认**不含 theme**（现状代码已如此，与本节一致）。这条同时修正了 v5 §7.2 原文「按 `date×theme` 聚类折算」的口径。

---

## 15 定量聚合器（无 LLM）

### 15.1 形式

```
logit(p_final) = logit(p_base)                     # 冻结基线
               + β₀(condition_key)                 # 条件截距
               + Σⱼ βⱼ(condition_key) · eⱼ          # 可复算证据项
```

- 无经验时（新格子 / 冷启动）：`β₀ = 0, βⱼ = 0` ⇒ `p_final = p_base`，**fail-open 天然成立**。
- `β` 的估计：**层级部分池化**（§17），不是每格独立估计。
- 杠杆上限：`|Δlogit| ≤ logit_delta_max()`（默认 0.4，约 ±10pp），防止单条经验把概率推飞；触发记 `clipped=True` 并留痕。

**实现**：`chain/aggregator.py::aggregate(p_base, evidence, condition_key, coefs)` 返回
`{p_base, p_final, delta_logit, clipped, used_coefs, fail_open, reason, condition_key, violations, agg_ver}`。

三条硬设计（写在代码注释里，别改）：

1. **`logit(0)` / `logit(1)` 会炸** ⇒ 夹到 `(1e-6, 1−1e-6)`；
2. **裁剪必须被记录**（`clipped=True`）—— 静默裁剪会让校准报告误判"模型很保守"；
3. **系数非数字 ⇒ 报错走 fail-open，不是静默当 0** —— 「β 真的拟合成 0」与「β 配置损坏」表现完全一样，静默当 0 事后无从区分。

### 15.2 为什么基线必须是冻结模型

`p_base` 来自 `app/watchlist/predict.py::predict_next_day` —— 手写 logistic，系数由离线脚本拟合后冻结（样本 3200、AUC 0.6126、`SCORE_VERSION=3`）。

三个理由：
1. **确定性**：同一输入永远同一输出 ⇒ `p_base` 可以事后补算，`Δp` 可复现；
2. **零成本**：不消耗 token，天然对照臂成立；
3. **不与经验互喂**：它读不到 `qd_agent_weights` ⇒ 两腿独立性天然成立。

> **红线 R2：`p_base` 路径禁止接入任何来自 `qd_agent_weights` / `exp_*` 的信号。**
> 若有人把 `qd_agent_weights` 接进 `pred_weights.json`，基线就不再独立，`Δp` 会退化成自证。
> 守卫：静态扫描断言 `app/watchlist/` 不得 import `app.agent.chain`。

### 15.3 `p_final` 不做什么

**`p_final` 只用于单标的的条件判断，不做全市场排序** —— 排序仍是量化腿的活（RankIC 只作评估不作生产）。
Δp 的正确性是整套设计里**唯一能被外部客观指标直接打分的出口**。

---

## 16 配对双跑：Δp 怎么产出、怎么落库

### 16.1 一次推理，三条记录

| 记录 | 来源 | 是否进判定 |
|---|---|---|
| `p_base` | 冻结模型，对同一 subject 顺手算 | ✅（对照臂） |
| `p_final` | 聚合器输出 | ✅（实验臂） |
| `Δp` | 二者之差 | ✅（主判据） |

**实现**：`chain/aggregator.py::record_pair(...)` / `pair_path(as_of)` / `load_pairs(as_of)` —— 当前落 **JSONL**（`exp_pair` 表尚未建，见 §22 O-4）。

### 16.2 检验方式（预注册，写死）

1. **主检验**：符号一致性配对检验。剔除 `y=0`（0.3% 死区）后，`sign(Δp) == sign(y)` 的比例 vs 0.5，**按 `date` 聚类折算**（§14.4）。
2. **副检验**：`y`（或残差收益）对 `Δp` 的回归斜率 > 0（连续量，功效高于符号检验）。
3. **辅助展示**：分组命中率、`lift` —— **只展示，不作判定**。
4. **多重比较**：同批 × 同 layer 内做 BH-FDR。

### 16.3 这条路上有哪些坑（诚实列出）

| 坑 | 为什么 | 处置 |
|---|---|---|
| **Δp≈0 常态化** | 若经验挖出的证据只是基线的重述 | G20：先测「证据项与 base 特征的相关性」，高度共线 ⇒ 该证据项作废 |
| **Δp 被杠杆上限截断** | `LOGIT_DELTA_MAX` 压住大位移 | 记录 `clipped` 比例，>20% 说明先验太强 |
| **标签仍不可靠** | `_get_actual_return` 走**实时行情源**，历史日期会取到当下价格 | G21（P0-3）：判定改用本地 K 线库 |
| **长度效应** | 注入证据变长本身可能改变行为 | 保留 C 臂（等长无信息安慰剂） |
| **y=0 被剔除** | 0.3% 死区内的样本没有方向 | 剔除比例本身要报；>50% 说明死区或窗口选错了 |

---

## 17 条件校准与层级部分池化：把 361 簇借到千级

### 17.1 为什么必须借力

361 个独立簇 ÷ ≤12 个条件格 ⇒ 每格约 30 簇，**刚好踩在门槛上，没有任何余量**。
单格独立估计必然过拟合。合法出路只有两条：减少格子数（§14.2 已做）、**跨格借力**（本节）。

### 17.2 三层结构

```
L1 板块层   （簇最少,最稳,30–60 簇/年）
  └─ L2 题材层  （50–100 簇/年）
       └─ L3 个股层  （上千量级,但噪声最大）
```

L0（市场层，10–30 簇/年，需 3 年+）在年 1 **不作为估计层**，只作为**全局先验的锚**（给 β 的先验均值，不参与显著性判定）。

> ⚠️ **L0/L1/L2 是分层变量，不是待验证假设** —— 别把它们当「要检验的条件」去加格子。

### 17.3 收缩规则（经验贝叶斯，参数可算不许拍）

```
β̂_cell = w · β̂_cell_ML + (1 − w) · β̂_parent
w      = n_cell / (n_cell + τ)
τ      = σ²_within / σ²_between        # 由同层各格的实测方差反推,不是常数
```

- 逐级收缩：L3 → L2 → L1 → 全局先验（L0 锚）；
- `τ` 每个季度重估一次，**不许硬编码**；
- 每格同时存 `n_eff`（聚类折算后）与**独立簇数**（独立簇 < 30 的格子**只出先验值，禁止出独立估计，禁止进判定**）。

**实现**：`chain/calibration.py` —— `fit_cell()` / `fit_with_pooling()` / `_parent_key()` / `is_fresh()` / `coef_usable()` / `calibration_report()`；`min_clusters()`、`tau_days()` 走 env。

**喂数层**：`chain/calib_feed.py`（2026-10-14 P3 接线）—— `build_samples()`（pairs × resolutions JOIN，含 G24 一 claim 一票 + horizon 口径过滤）/ `build_cells()`（独立簇**折算**，走 `app.utils.stat_power.estimate_independent_clusters`）/ `coefs_for()`（进程内缓存 → 落盘 τ 内复用 → 重拟合）/ `build_report()`。`ρ` / `horizon` / 缓存秒数 / 回看天数一律走 env。

> ⚠️ **算法层与喂数层为何分家**：`calibration.py` 保持**纯函数**（零 IO、好测），
> `calib_feed.py` 承担读 JSONL + 查 PG + 缓存/落盘。与 `weight_feed`（resolutions → weights）
> 同一范式 —— 别把 IO 塞回 `calibration.py`，否则 P3 的单测就要连库。

### 17.4 借力的诚实边界

> **借力增加的是稳定性，不是新信息。**
> 收缩后的 β̂ 方差更小、样本外更稳，但它的**偏差**来自「各格同质」这个假设。
> 若某格的真实效应与父格相反，收缩会把它抹掉。
> ⇒ **每个格的原始 ML 估计必须一并落库**，季度复盘时看「被收缩抹掉的方向性差异」是否集中在某些格 —— 那是要升级为独立假设的信号，不是噪声。

---

## 18 判别特征：挖掘 · 分流 · 注入

### 18.1 挖掘

同一条件格内 hit vs miss 做 contrastive，产出**判别特征键**。

- 特征键必须**可命名、可复算、可证伪**（三有原则），否则不入键表；
- 每个键必须给出**客观计算式**，与 `computable` 标记严格对应；
- 年 1 证据维上限 4 个键（由 §14.2 的 12 格预算决定）。

### 18.2 两类证据的分流（关键设计）

| 证据类型 | 例子 | 能否进 βⱼ 点估计 | 依据 |
|---|---|---|---|
| `computable: true` | 量比>2、连板数、距 MA20 乖离、换手率分位 | ✅ 可以 | 可客观复算 ⇒ 可 PIT 化 ⇒ 可回测 |
| `computable: false` | 「该股是这波题材的龙头」「资金明显在抢筹」 | ❌ 只能作分组变量 | 不可复算 ⇒ 无法 PIT ⇒ 进点估计 = 合法化过拟合 |

> 这条把 §10.5「特征三分」的 PIT 纪律**落实到聚合器的输入侧**：
> A 类进点估计；B 类（须先修成分股前视）修好后进；**C 类永远只作分组变量**。

### 18.3 注入：证据键怎么回到 LLM

判别特征有两个去处，**必须都做**：

1. **进聚合器**（`computable: true`）：纯数值，不经过 LLM；
2. **进提示词**（全部键）：以「本次请重点检查这些特征」的形式注入，让 LLM 在证据生成阶段**知道要看什么**。

> 一次挖掘两处收益：**「现在的注入项」+「未来的 SFT 语料」**。37,500 条/年喂 SFT 足足有余，喂频率统计则差两个数量级。

**注入门槛以独立簇数为据，不以案例数为据**（§10.4）。

---

## 19 三个回收组件

> 这三项在 v3 的老版本里被静默丢弃，v3 合并时捡回并**重新挂点**（不是原样搬回）。

### 19.1 证伪 gate（挂 `falsifiers` 字段）

**为什么它比"把对的说得更有把握"更划算**：§10.2 实测把可检测下限压到 7.4pp，意味着「提升置信度」的空间极窄。此时更划算的路径是**减少错误方向的发声** —— gate 正是这个机制。

**规则（写死）**：

1. 每次预测**必须**产出 ≥1 条 `falsifiers`；
2. 缺失 ⇒ 该次预测**降级**：不进正式判定队列，只记账，且 `p_final = p_base`（不给经验加成）；
3. T+N 后回看：若该次被证伪条件命中且结论错误 ⇒ 计入 `falsified_wrong`，是**最有价值的训练样本**；
4. **指纹防失真**：统计口径用「**证伪后仍维持方向结论的比例**」，不用「反向证据率」—— 后者会因为 gate 的存在而恒高，变成没有分辨力的常数。

**归因复用** `qd_agent_resolutions.attribution` 六值（`data_missing｜logic_error｜black_swan｜timing｜caliber｜noise`），**不新建分类**。

### 19.2 自适应算力（挂样本缺口）

推理深度 ∝ **不确定度**：

```
U = w1·格子样本缺口(max(0, 1 − n_eff/30))
  + w2·证据冲突度(computable 证据与 LLM 判定方向不一致的比例)
  + w3·多路不一致度(K 路 rollout 的方向分歧)
```

| U | 动作 |
|---|---|
| 低 | 单路（K=1）、不跑证伪展开、不回测验证 |
| 中 | K=2、跑证伪 gate |
| 高 | K=3 + 证伪 + 转中性选项（宁可不发声） |

> 老设计要每个落库案例都跑 K=3（成本 3×）。U 路由让**同样的预算花在真正不确定的地方**。
> ⚠️ 「K 不增加独立样本数」依然成立 —— K 路只测**稳定性**，不增加统计功效。

**成本记账**：检索 / digest / 仲裁 / K 路 rollout 的 token **一并计入 run 预算**；日配额 `add_day_usage`（G9）必须接线，否则自适应路由会绕过日成本保护。

### 19.3 多路仲裁与方向冲突裁决

老设计只讲「置信度的读取顺序」，**没讲方向冲突怎么裁**。补这一层：

1. K 路方向**一致** ⇒ 正常产出，分歧度记 0；
2. K 路方向**不一致** ⇒ 按证据的**客观性**加权，不是按票数：
   - `computable: true` 的证据项占权重 ≥ 0.7（可复算的说了算）；
   - 若不一致全部发生在 `computable: false` 的主观项上 ⇒ **转中性**（不发声），记 `abstain_by_conflict`；
3. **仲裁器自身也要进计分**：每次仲裁的后续正确性落校准表，否则它就是一个不受监督的拍脑袋组件。

---

## 20 输出契约与红线

**最小必填（缺一项 ⇒ 不进判定队列）：**

| 字段 | 来源 | 缺失时 |
|---|---|---|
| `ev_ver` + `condition_key` | ① 段 | 不进判定，只记账 |
| `claims[{direction, window}]` | LLM | 不进判定（不可验证 = 不可追责） |
| `falsifiers[≥1]` | LLM | 降级：`p_final = p_base` |
| `p_base` / `p_final` / `Δp` | 聚合器 | 不可能缺失（基线必算） |
| `provenance{prompt_sha, producer_ver, evidence_class}` | 运行时 | `prompt_sha` 缺失 ⇒ 记 G10 违规，该批不进判定 |

**可选增强（不进判定，只做分析）**：`evidence_strength`、K 路分歧度、`clamped`、耗时/token。

**红线（不放松）：**

| 编号 | 内容 | 落点 |
|---|---|---|
| **R1** | LLM 自报概率一律丢弃并记违规 | `evidence._strip_llm_probability` + `aggregator._scan_r1` |
| **R2** | `p_base` 路径禁止接入 `qd_agent_weights` / `exp_*` 信号 | 静态扫描断言 |
| **R3** | 不同 `evidence_class` 的数字**禁止合并**；headline 只取 `live`；`replay` 不得当「经验有效」的证据 | 报数层 |
| **R4** | `range` 类型已由 `claims.py` 砍掉，不加码 | `chain/claims.py` |
| **R5** | 判定维度**冻结不细化**：direction / magnitude / level | `chain/schema.py` |

---

## 21 落地路径与当前进度

> 原则：**不复辟空表**。在标签还不可靠时建表，只会得到一张漂亮的空表。

### 21.1 批次总表

```
【P0｜阻断级】—— 不做完,后面全是脏标签上的精密统计
├── P0-1  delisted 单列(G18)                    → chain/resolver.py
├── P0-2  _VERDICT_SCORE 补 delisted: 0.0(G18)  → chain/weight_feed.py
├── P0-3  判定取价改本地 K 线(G21)              → chain/evaluator.py
├── P0-4  验证 weight_feed 真的喂数了           → chain/weight_feed.py feed()
└── 并行  D1  C 类情境档案每日一帧(不记即丢)     → chain/context_frame.py

【P1｜证据向量接线】—— 复用既有写入/注入链路,不新建管道
├── P1-1  落 chain/label_spec.py(口径单一真源)
├── P1-2  record_case 的 payload 扩展为 EV-1     → utils/case_memory.py
├── P1-3  注入块升级为「金例 + 判别特征检查清单」 → utils/case_memory.py
└── P1-4  修 prompt_sha missing(G10)             → qd_agent.py

【P2｜聚合器与配对双跑】—— 核心,也是 G7 的关闭点
├── P2-1  新建 chain/aggregator.py(含 LOGIT_DELTA_MAX 截断)
├── P2-2  p_base 复用 app/watchlist/predict.py(只调用不改)
├── P2-3  一次推理落三条(p_base/p_final/Δp)
└── P2-4  红线 R2 守卫(静态扫描)

【P3｜条件校准与层级池化】
├── P3-1  建 exp_cond_calib(格参数 + n_eff + 独立簇数 + 父格 + τ)
├── P3-2  收缩实现(§17.3),τ 季度重估(不许硬编码)
├── P3-3  独立簇 < 30 的格只出先验值、禁进判定(硬断言)
└── P3-4  exp_cond_registry 台账(G23)

【D｜数据采集】（v5 §19.2 分层配额拍板)
├── D1  context_frame 每日一帧
├── D2  sampler 分层配额选股(活跃 60% / 中热 25% / 低热+随机 15%)
├── D3  双窗口标签(T+1 快速出结论 + T+3 主判据)
└── D4  盘后自动采集接线
```

### 21.2 进度（2026-10-13 实点）

| 项 | 状态 | 证据 |
|---|---|---|
| P0-1 `delisted` 单列 | ✅ 已落地 | `chain/resolver.py` `verdict='delisted'` |
| P0-2 `_VERDICT_SCORE` 补 `delisted: 0.0` | ✅ 已落地 | `chain/weight_feed.py` `_COUNTED_VERDICTS` 含 `delisted`；`_VERDICT_SCORE` `delisted: 0.0` |
| P0-3 判定取价改本地 K 线 | ✅ **已落地**（2026-10-13） | `evaluator._get_actual_return` 已改为按交易日历算真实跨度取足够长窗口 + 走 `CNStockDataSource().get_kline(...,"1D",...)`；源码注释即实测案例（`from_date=2026-08-29/T+3` 原取到 `2026-09-22`） |
| P0-4 验证 `weight_feed` 真喂数 | 🟨 待验 | **接线已确认存在**（`evaluator.py:972` `from .weight_feed import feed as _feed_weights`，2026-10-13 复核）；仍缺跑后断言：`sample_count` 全 0 的行数下降 |
| D1 `context_frame.py` | ✅ 已建 + 已接线 | 392 行；`data/context_frames/context_frames_2026-10.jsonl` 已落帧 |
| D2 `sampler.py` | ✅ 已建 + 已接线 | 292 行；`data/daily_universe/daily_universe_2026-10.jsonl` 已落 |
| D3 双窗口标签 | ✅ 已落地 | 同条 resolution 挂 T+1/T+3；`tests/test_v5_dual_window.py` |
| D4 采集接线 | ✅ 已接线 | `collector.run_daily_collection()` 由 `evaluator` 调用（**v5 唯一真正接线的组件**） |
| P1-1 `label_spec.py` | ✅ 已落地（ls2） | 口径已统一为单一 `HOLDS` 表，见附录 C |
| P1-2 `record_case` 扩展 EV-1 | ✅ **已接通（含写侧）** | `qd_agent._record_case()` 现在传 `evidence=`（取自 `_c3p_pipeline`）；守卫 `qd_smoke` test_40 ③ 断言实参**不是常量** `None` |
| P1-3 注入块升级 | ✅ 已落地 | `tests/test_v5_case_injection.py` |
| P1-4 `prompt_sha` | ✅ 已落地 | `tests/test_v5_prompt_sha.py`；`utils/tracing._repro_meta` 同口径 |
| P2-1 `aggregator.py` | ✅ **已接线** | `chain/c3p.py` 调 `aggregate()`；不再只被 `tests/` import |
| P2-2 `p_base` 复用 | ✅ **已接线** | `c3p.p_base_of()` → `kline_feed` → `predict_next_day`；`_MIN_BARS` 与 `predict.MIN_BARS` 有守卫测试绑死 |
| P2-3 `record_pair` | ✅ **已接线** | 按 **claim_id** 落三条（p_base / p_final / Δp），写 JSONL |
| P2-4 R2 守卫 | ✅ **已落地** | `qd_smoke` test_40 ④ 扫 `app/watchlist/**` 不得反向 import chain；变异检验 M7（真注入违规）证明有 bite |
| P3 `calibration.py` | ✅ **已接线（2026-10-14）** | 经 `chain/calib_feed.py` 接入：`c3p._c3p_pipeline` 的 `aggregate(..., coefs=calib_feed.coefs_for(ck))`；守卫 `test_v5_c3p.py::test_aggregate_coefs_is_not_constant_none`（AST 级，变异检验已证有 bite）。**四个孤岛全通** |

> ✅ **2026-10-14 P3 接线已完成**：四个孤岛（`evidence` / `aggregator` / P1-2 写侧 /
> `calibration`）**全部接通**。新增接线层 `chain/calib_feed.py`（喂数），
> `chain/calibration.py` 保持纯算法层不动。
> 接线点：`qd_agent._finish_collector` → `_intake_accountability` → **`_c3p_pipeline`**
> → `calib_feed.coefs_for(ck)` → `aggregate()` → `_record_case`。
>
> ⚠️ **Δp 仍恒为 0 是设计内的预期行为**（冷启动）：样本不足 ⇒ `coefs_for()` 返回
> `None`（独立簇 < `min_clusters` 只出先验，P3-3 禁进判定）⇒ `p_final == p_base`。
> 本阶段在**攒数据**，不是看效果 —— 别为了"看到非零 Δp"去凭空编系数。
> ⇒ 判别「接线有没有真生效」不看 Δp，看 `build_report()` 的格子与簇数，
> 以及 smoke/单测的接线守卫（详见 §21.4）。

### 21.3 分层配额（v5 §19.2 拍板，已落代码）

| 层 | 占比 | 来源 | 作用 |
|---|---|---|---|
| 活跃 | 60% | 龙虎榜 / 热榜 / 涨跌停炸板 / 策略扫描 | 主体经验来源 |
| 中热 | 25% | 成交额/换手分层抽样 | 撑开情境维的**中段变异** |
| 低热 + 随机 | 15% | 全市场随机 | 保证情境维有足够的**低尾变异**，兼作天然对照 |

三条硬要求：① 配额按 `date` 强制，不许「今天活跃股多就多采」；② 每层**独立记账**（`sample_layer` 字段）；③ 低热层不得因「分析价值低」被跳过 —— **它的价值不在单条案例，在撑开变异**。

> **为什么必须分层**（第二条理由，同样硬）：若采样集中在少数活跃股，`theme_heat`/`vol_bucket` 等情境维在样本里近乎**常数** ⇒ **条件化失效**（β₀/βⱼ 估不出来）。
> 而 §10.2 的表说明：覆盖 5,000 只 ⇒ 共享参数 361 簇 / 7.4pp；覆盖 500 只 ⇒ 个股级只有 24 簇 / 28.4pp。
> ⇒ **数万条案例的价值，取决于它们覆盖了多少只不同的股票，而不是总条数。**

### 21.4 P3 接线细则（2026-10-14 新增）

```
pairs_*.jsonl（p_base / condition_key / claim_id / extra.horizon）
   ×  qd_agent_resolutions（每 claim 只取最新一条，G24）
      →  samples[{ck, as_of, p_base, y}]        # y ∈ {hit 1.0, partial 0.5, miss/delisted 0.0}
        →  cells{ck: {n_clusters, win_rate, mean_p_base}}
          →  calibration.fit_with_pooling() → β₀（含来源层标注）
            →  coefs_for(ck) → aggregator.aggregate(coefs=...) → p_final
```

| 环节 | 口径 | 为什么不这么写会出事 |
|---|---|---|
| **JOIN 键** | `claim_id`（不是 run / 不是股票） | Δp 的真值 y 挂在 claim 上，按 run 配对会对不上 |
| **y 的来源** | resolution 的 `verdict`（`verdict_score` 缺失时取 `schema.VERDICT_SCORE`） | `undecidable` / `data_missing` 是**常量污染**，不是半票 ⇒ 不进样本 |
| **一 claim 一票** | 同一 claim 多条留痕只认 `res_id` 最大 | resolution 是纯 INSERT（留痕）⇒ 不去重会把**重试次数**当样本数，n 虚涨 ⇒ 门槛虚过 |
| **horizon 过滤** | 默认只收 `T+1`（`QD_CALIB_HORIZON`） | `p_base` 来自 `predict_next_day`（**次日**概率）；拿 T+3 判定校它 = A 尺子量 B |
| **只用 p_base** | β₀ 的 estimand 是「实际胜率 − p_base 均值」 | 用 `p_final` 会让系数去追自己上一轮的修正 ⇒ **自我强化** |
| **n_clusters** | `stat_power.estimate_independent_clusters(天数, 每日本人)` 折算 | 150 只/日 ≈ 4.5 个独立观察；用名义样本数当 n 会**严重高估功效** |
| **τ 与落盘** | 系数落 `data/calib/coefs_cal1.json`，τ 内跨进程复用 | 若每次冷启动都重拟合，`fitted_at` 恒为 now ⇒ τ（季度重估）形同虚设 |

**观测入口**（接线生效与否，不看 Δp 看这些）：

```python
from app.agent.chain import calib_feed
calib_feed.build_report()      # 每格：n_obs / n_days / n_clusters / win_rate / β₀ / source_level
```
`_c3p_pipeline` 的返回值带 `calib{used, source, n_clusters, pooled_n_clusters}`，
pair 的 `extra` 带 `calib_source` / `calib_n` / `calib_pooled_n` ⇒ **事后能一眼看出
这条 Δp 是本格自己的还是借来的**（把借来的当成自己的 = 静默放大）。

> ⚠️ **诚实的边界**：`win_rate` / `mean_p_base` 是**样本等权**均值，而门槛用的是
> **折算簇数** —— 两者口径不同：当"每日本人很多、天数很少"时，点估计方差仍被低估。
> `min_clusters` 门槛 + 层级池化已在保护，但**严格按日等权**要等 O-4 建 `exp_pair`
> 表之后再做（现在先记着，别当它不存在）。
>
> ⚠️ **P3-1 的 `exp_cond_calib` 表仍未建**（附录 F D-2）：当前用 JSON 落盘替代，
> 够跑通链路但不做 JOIN。建表属 O-4 批次，不阻塞 P3 已接线的事实。

---

## 22 优化分析（不考虑兼容性）

> 本章回答「还能不能更优」。每项给：现状 → 目标态、收益、代价/风险、**是否推荐与顺序**。
> ⚠️ 「不考虑兼容性」= 允许改表结构、允许删代码、允许改命名；**不等于允许放松统计纪律**（红线 R1–R5、支撑度门槛、格子封顶一律不动）。

### 22.1 优化项总表

| # | 优化 | 收益 | 代价 / 风险 | 推荐 |
|---|---|---|---|---|
| **O-1** | 盘后四条独立链路 → 单条**日终结算**流水线 | 行情取数 O(4N)→O(N)；同一 as_of 快照保证一致；整批可重放 | 需重排 `evaluator.auto_evaluate`；失败域变大（要分段 try 保留） | **① 先做** |
| **O-2** | `qd_agent_traces` 旧评估链路**退役** | 消掉双账本；消掉 D-7（prompt_sha 3/5 missing 的源头之一）；省盘后时间 | `qd_smoke` 有若干断言要同步摘 | **② 做** |
| **O-3** | 判定取价再加一道：claim 创建即冻结 `exec_date` + **基准价**，T+N 只查本地日线（G21 已修窗口长度，本项是「基准价冻结」的加固版） | 标签可复现、可批量、可回补；**口径变更不再恐惧重算** | 要补一张价格快照表或复用现有日线表 | **③ 做（加固，非救火）** |
| **O-4** | Δp 记账从 JSONL 提到 `exp_pair` 表，并建 `exp_cond_calib` / `exp_cond_registry` | P3 的核心操作是「按 condition_key 聚合」—— JSONL 做不了 JOIN | 一次 migration | **④ 做** |
| **O-5** | 经验存储**合一**：一次推理一行 case（内嵌 EV-1 + pair + condition_key，frame 按 as_of 单独存） | 从「同一次推理散在 4 个文件」到一次写入一次检索 | 要改 `qd_agent_cases` 的 payload schema（本来就要扩展） | **⑤ 做** |
| **O-6** | 权重闭环粒度从 `domain` 升到 `condition_key`（新增 `layer='cond'`） | 闭环对象与条件化引擎对齐；domain 层保留作粗粒度 | 每格 30 簇 ⇒ **只作展示，不作行为调整** | **⑥ 做（限展示）** |
| **O-7** | 观测计数器进程内 → 落盘 JSONL（D-8） | 拿到长期趋势 | 便宜 | **⑦ 做** |
| **O-8** | `app/agent/utils` → `qdutils`（D-13） | 可读性：同名目录在 grep / IDE 检索里易串 | 涉 9 处改名 | ⑧ **降级为可选**（⚠️ 2026-10-13 复核：原动机「sys.path 顺序雷」已被 P5 根治，本项不再是"排雷"，只余可读性收益，不必抢在 O-1 前后做） |
| **O-9** | 日配额 `add_day_usage` 接线 + 自适应算力 U 路由（G9 + §19.2） | 自适应路由不会绕过日成本保护 | 要定 U 的三个权重（不许拍，先观测一季） | ⑨ 做 |
| **O-10** | `admission.json` 落进工作副本 | 干净环境不再「注册 0 个、不抛错」地静默降级 | 与「本地不入库」裁定不冲突（落工作副本即可） | ✅ **已落地（2026-10-13 复核）**：文件实测在 `app/agent/capabilities/admission.json`、未被 .gitignore 忽略（P13 已订正「不在工作副本」的旧说法）。**本项关闭**；残留问题转为 D-9（文件在，但 71 条 `doc` 全空） |
| **O-11** | 小项打包：`run_skill` 白名单下沉（D-11）/ `_skill_seen` LRU（D-12）/ 旧系统反向依赖（D-2） | 各自都是几十分钟 | 低 | ✅ **三项全部收口（2026-10-14）**：D-11 元工具名单下沉 `QD_META_TOOLS` + 白名单改包内相对导入；D-12 LRU 容量上限 `QD_SKILL_SEEN_MAX`；D-2 cron 对外能力出口契约入代码。新增 3 个守卫文件（`test_v6_skill_seen_lru.py` / `test_v6_d11_whitelist_knobs.py` / `test_v6_cron_boundary.py`），变异检验 15/15 全有 bite。**本项关闭** |
| ~~`preselect_stats._NOT_TOOLS`（D-3）~~ | ✅ **已由 P10 根治**（非工具模块改 `_` 前缀，扫描器天然跳过；名单方案整体删除） | — | — | **不做（已解决）** |
| **O-12** | ~~工具面简化：撤掉「LLM plan + lint + 共现权重」三层~~ | ~~省掉预选那一整轮 LLM~~ | ❌ **方向错了**：实测预选 `stock_screen` **-72%** token / `trade_confirm` **-65%**；且净收益随工具数**单调递增**（见 §22.4 成本模型） | **不做（明确保留）** |
| **O-13** | 冻结基线 `p_base` 换成更强的量化模型 | 短期绝对准确率更高 | ❌ 会引入「两腿互喂」风险，且 Δp 的分母变得不可复现 | **不做**（与红线 R2 同源） |
| **O-14** | 预选目录**分级展开**（先给域摘要，只展开命中域）+ 按 query 哈希缓存预选结果 | 工具面上百后预选那一轮自身也不便宜（N=500 时目录 36.7k 字符）；分级后回到 ~1k 量级 | 域摘要要维护；命中域判错会漏（已有 `has_domain_hint` + lint 补位 + `list_tools` 兜底三道） | **⑫ 做**（工具面破 120 个时启动） |

### 22.2 三条最值得先做的（展开）

**O-1 日终结算合一**：现状 `evaluator.auto_evaluate()` 里四段独立 try、各自扫表取数。目标态：

```
daily_settlement(as_of)
  ① build_frame(as_of)              ← 一次取大盘/波动/热点,后面三段共用同一快照
  ② pick_daily_universe(as_of, n)   ← 分层配额
  ③ resolve_due_claims(as_of)       ← 判定用 ① 的价格口径
  ④ weight_feed.feed()
  ⑤ 落 Δp 配对记账
  每段独立 try + 一段失败不阻断其余(保留现状的容错语义)
```

**O-2 traces 退役**：`qd_agent_traces` 现有 874 条 root 已 unverifiable、`qd_agent_weights.sample_count` 全 0 —— 这链路**跑了但没产出可用信息**。保留 traces 作**观测流水**（tool/skill 调用记录），摘掉它的评估写路径与 `update_weights`。

**O-5 经验存储合一**：现在同一次推理的产物散在 `qd_agent_cases`（EV-1 载荷）+ `data/context_frames/*.jsonl` + `data/evidence/*.jsonl` + `data/pairs/*.jsonl` 四处。目标态：

```
qd_agent_cases 一行 = { 原有 CBR 字段
                      + ev: {...EV-1...}
                      + pair: {p_base, p_final, delta_logit, clipped, ...}
                      + condition_key
                      + as_of }        # frame 按 as_of 单独存,不每条重复
```

### 22.3 明确不做 / 需先测量的

- **O-12（撤预选）——不做，且不需要再测量。** 初版把这条写成「需先做 A/B」是**判断错误**：A/B 早就跑过了（`stock_screen` 153,960→43,318 tok，**-72%**；`trade_confirm` 100,150→34,976 tok，**-65%**），再测一遍是重复劳动。真正的判据是 §22.4 的成本模型：**预选的净收益随工具数单调递增**，撤掉它只会让工具面越大越亏。共现权重（K）另有一层价值：它是「可学习」的长期资产，撤了就没了。
- **O-13（换更强基线）**：绝对准确率与 Δp 的可估性是两回事。更强的基线通常更不透明、更难冻结，会破坏 §15.2 的三条理由。
- **扩条件格**：§10.3 的 12 格是**上限不是目标**，实际年 1 先开 ≤3 格。扩格会让每格掉到 30 独立簇以下、全部落进「只出先验值」，等于用实证换精度。

### 22.4 O-12 成本模型（2026-10-13 实测，为什么净收益单调递增）

> 测量脚本：`tmp/measure_preselect_cost.py`（2026-10-14 随 agent/tmp 移除迁到项目级 tmp/）。当前工具面 **53 个**（finance 41 / common 11 / knowledge 1）
> —— ⚠️ 早期版本写「58 个 / common 16」，是 P10 清理误注册统计函数之前的数（2026-10-13 订正）。

**单位成本（实测）**

| 项 | 字符数 | 说明 |
|---|---|---|
| 单工具 JSON schema | **544** | 每步都要随请求下发 |
| 预选目录单行（`name\|desc\|params`，`max_desc=60`） | **73.4** | 只在预选那一轮出现一次 |
| 比值 | **7.4×** | 目录是 schema 的压缩摘要 |
| 预选固定开销（system + prompt 骨架，不含目录） | **504** | **常数，不随工具数变** |

**决定性的一点：schema 是「每步」都发，预选是「一次」。** 撤掉预选不等于省一轮 LLM，而是把「全量 schema × 步数」这笔账加回来。

| 工具数 N | 无预选：N×544×6 步 | 有预选：目录一次 + (必注入 11 + 激活 ≤8)×544×6 步 | 节省 |
|---|---|---|---|
| 53（当前） | 172,992 | 66,410 | **62%** |
| 100 | 326,400 | 69,860 | **79%** |
| 200 | 652,800 | 77,200 | **88%** |
| 300 | 979,200 | 84,540 | **91%** |
| 500 | 1,632,000 | 99,220 | **94%** |

> 本表已按 2026-10-13 实测参数重算（必注入由 16 降为 **11**，P10 清理误注册统计函数所致）。
> **结论不变且更强**：节省率随工具数单调递增，53 → 500 从 62% 升到 94%。

> 模型低估了实测值（实测 -72% vs 模型 62%），差额来自步数从 6→2：工具面窄，模型更少「摸索式」试错。但**趋势是对的且方向不可逆**：固定成本是常数，被省掉的成本线性增长 ⇒ **工具越多，预选越划算**。用户「tools 目录内会有多个域、上百个 tools」的判断，恰恰是**加强**预选的理由，不是撤掉它的理由。

**功能侧：撤掉也不致命，所以更不能拿「没影响」当撤的理由。**
`list_tools` / `search_tools` / `activate_tools`（`tools/tool_discovery.py`，`common` 域 ⇒ 必注入层，永远可达）+ 分层 fail-open（未激活直调就地激活）+ `fallback_domain_tools` 兜底 —— 三条退路都在，所以「撤掉预选功能上跑得通」成立。但**跑得通 ≠ 划算**：省一轮 5k 字符的调用，换回每步 31k→（未来 272k）字符的常驻支出。

**正确的方向是 O-14：给目录分级，而不是把预选删掉。**
N=500 时目录本身 36.7k 字符、占预选输入 99% —— 这确实是个负担，但解法是「先发域摘要（约 10 行），只展开 `has_domain_hint` 命中的 1–2 个域」，把目录压回 ~1k 量级。`build_catalog_grouped(defs, domain_names, ...)` 的域过滤参数**现成就支持**这一改动，只需把「先算命中域 → 再按域建目录」两步串起来。触发条件：工具面破 120 个。

---

## 23 诚实边界：这套系统证不了什么

1. **证不了绝对预测力**：Δp > 0 只说明「经验让概率朝对的方向动了」，不说明 `p_final` 本身有多准（`p_base` 50% → 51% 也是 Δp > 0）。**绝对水平必须另报**（`p_final` 的 Brier / 校准曲线，按季）。
2. **证不了因果**：条件格是**观察到**的，不是随机分配的。「在 `theme_heat=hot` 格里 Δp 显著为正」≠「因为热度高所以经验有用」。随机化呈现证因果明确不做。
3. **证不了跨 regime 稳定性**：12 格已经吃满 361 簇，**不要再按 regime 切第二刀**。
4. **层级池化借来的是先验，不是数据**：`n_eff` 变大、区间变窄 —— 但**窄来自假设（可交换性），不来自观察**。报数时必须同时报「自有独立簇数」和「借用后有效样本数」，不许只报后者。
5. **符号检验在 Δp≈0 时退化为抛硬币**（G20）。这是**最可能失败的方式**，且失败形式是「跑了一年，什么也没测出来」，不是「测出来无效」。
6. **12 格之外的条件空间一律不报数**。第 13 个格子的任何结论都是过拟合。
7. **`computable:false` 的证据永远只能做分组变量**。这条一旦被打破，聚合器就从「定量层」退化成「LLM 意见的另一个马甲」，红线 R1 形同虚设。
8. **Δp 证不了分钟线**（不同 estimand，该用 RankIC / 方向准确率）；日频跑通前**不开分钟线库**，开了也不混。

---

# 附录

## 附录 A：改进路线 A~M 收口记录

> 来源：`agent差距与改进路线.md`（2026-10-02 立档 · 2026-10-11 收口），原件已移入 `del/agent_roadmap_20261011/`。
> 状态：✅ 完成 · 🟨 部分落地/待验 · ⬜ 未做 · 🔒 裁定不做。

| # | 项 | 状态 | 一句话收口 |
|---|---|---|---|
| A | 规划质量无代码强保证 | ✅ | `_plan_lint` 轻校验 + prompt 规划 5 题 |
| B | 执行型技能子 agent 分发通道 | 🟨 | 机制全齐；成本比 0.889 未达标（D-18） |
| C | 能力级评测体系 | 🟨 | 标尺已立并首跑 71.7；真实样本与双人标注未铺开 |
| D | 会话上下文跨进程持久化 | 🔒 | 裁定不做、转观察（附录 B.1） |
| E | Few-shot 金例注入 | 🟨 | 链路通，首轮 +2.4；embedder 已接线 |
| F | 参数精调 | ⬜ | 5 参数无扫描 |
| G | 胜率反哺 → 排序权重 | ✅ | 乘子化 + smoke 五断言，冷启动 bit 级一致 |
| H | 成本/时延 SLO | 🟨 | token/工具/累计步数三线已接线；**日配额仍未接线** |
| I | 超大技能 TL;DR 头部 | 🟨 | market-screener 有、strategy-debug 部分、auto-* 四个未写 |
| J | 发布工程收尾 | ✅ | `--with-env` 落地；入库两项裁定不做 |
| K | 工具预选可学习 | 🟨 | 机制落地、冷启动一致；真实召回 ≥95% 待跑批 |
| L | 备选路线 B（MiMo-Code 大脑） | 🟨 | 存档未采纳，重启条件见附录 B.3 |
| M | 多轮递进会话闭环结构化 | ⬜ | 方案已定（三件）未施工 |

**C 能力评测**：`qd_bench` 25 题五类各 5；打分 coverage 30 / grounding 25 / claims 20 / structure 15 / human 10，全确定性机检。首跑 **71.7**（分类均分：个股 81.5 / 选股 66.8 / 复盘 48.6 / 交易 81.6 / 问答 81.7；强弱答案区分度 Δ72.5）。
遗留：seed 种子题需用 `import-traces` 换成 20~50 条真实任务；两人各标一轮验 agreement ≥80%。

**B 分发 A/B**：首跑 dispatch 163,980 tok / 7 步 / 314.6s / q86.7 vs inline 184,557 / 7 / 231.1s / 86.2 ⇒ **成本比 0.8885 ✗（验收 ≤0.40）· 质量Δ +0.5 ✓**。根因：`run_skill` 返回空结果（盘后无候选）→ 模型回退主对话硬跑。空结果纪律已接线，**待重跑验成本比**。

**验收数据索引**（`app/agent/` 下）：`scripts/prompt_eval/round{1,2,3}.json`、`bench_20261010_real_first.json`、`bench/dispatch_ab/dispatch_ab_20261010_210328.json`、`scripts/bench/preds_E_*.jsonl`。
项目根 `del/`：`tools_returns_contract_20261010/`、`agent_formatters_20261001/`、`agent_roadmap_20261011/`。

---

## 附录 B：关键裁定记录（忘掉会返工）

### B.1 D 裁定（2026-10-10）：会话上下文不做裸跨进程恢复

原方案（messages 全量快照落 PG、重启恢复）**暂不做**，转观察。理由两条：

1. **污染闭环问责**：恢复旧消息会突破证据边界 —— `_observation_corpus()` 从 `self.messages` 收 grounding 语料 ⇒ 旧轮工具输出里的**过期数字**把"本轮没取到的数"洗成可溯源 → 陈旧结论过溯源门 → `record_decision` 抽出被污染的 claim → T+N 判定归因进 `weight_feed` EMA ⇒ **闭环学到"用旧数据也能对"的伪规律**；
2. **需求已被受控通道覆盖**：完整问答三处落存储（`qd_agent_messages`、`qd_traces`、chain intake）—— **问责链数据零损失**；跨进程连续性已有受控通道（PostgresMemory 记忆近史：`_relevance≥2` 闸门 + 200 字/行截断 + 6 条上限）。

- **观察指标**：真实端点上因进程重启 / 多 worker 路由导致的会话断片次数、用户重问率。
- **若到做那一天（预案）**：只做最小受控续承 —— 恢复"指代指针"，走记忆近史同款闸门注入，且代码层硬隔离（恢复块不进 `_observation_corpus`、不参与 claims 提取）。

### B.2 J 裁定（2026-10-11）：本地即运行，不做 git 入库

代码源起远程服务器，现为**本地运行模式**：本地工作副本即唯一事实源，agent 工作流**不含任何 git 指令**。

- 推论：入库两项（admission.json / del 归档）**不做**；"干净 clone + 无 PG → smoke 全绿"由 `--with-env` skip 机制天然满足。
- ⚠️ 已知副作用：`del/tools_returns_contract_20261010` 与 `del/agent_formatters_20261001` **当前磁盘上都不存在** ⇒ 文档中「已归档、可回滚」的承诺**当前失效**（活路径 `.py` 源文件干净，防回流意图仍满足）。

### B.3 L 存档：MiMo-Code 大脑（备选路线 B）

已评估**未采纳**（路线 A = white-box）：MiMo-Code 本体有持久记忆 / 上下文重建 / dream，但金融工具需 MCP 化、行为偏软件工程。

> 澄清两条 MiMo-Code 通道：① **native** `MimocodeAgent` —— **已是本届执行核基类**（§3.1）；② **blackbox** `agents/blackbox/mimocode.py`（容器里驱动真 MiMo-Code CLI）—— **这才是路线 B 的集成路径**。

- **重启条件**：出现"长程编码类子代理"需求（策略代码生成 / 回测工程化），或路线 A 的规划/记忆短板（A/D）修完仍不达标。

### B.4 分发/加载两层分工（2026-10-10）

知识型技能 → 注方法论正文（渐进式加载）；执行型技能 → 注**分发卡片**（`run_skill` 派活）。两层正交不冗余；执行型注正文 = 真冗余 + 行为风险。

### B.5 两腿分离（v4 §19.3，v3 沿用）

**量化臂与经验臂对比而非加权平均** —— 反向案例是最有价值的资产，平均掉等于扔掉。参数不共享、不互相调参、两账本不互写。
`p_final = p_base + Δlogit` 是**在 Δlogit 层相加的合成**，不是融合（§11.4）。

---

## 附录 C：口径统一记（`label_spec` ls2）

> 2026-10-13 完成。记录一次**原判断被推翻**的经过，防止后人改回去。

**原判断（错）**：「`evaluator._TIMEFRAME_DAYS`（1M=22，自然月口径）与新增 `HOLD_LABELS`（1M=20，交易日口径）语义不同，**故意并存**，强行统一会静默改掉旧链路判据。」

**推翻它的三条证据**：

1. **消费端按交易日解释**：`_maturity_days` 的 docstring 原文「hold_days 按交易日计（_TIMEFRAME_DAYS）」，且 `_get_actual_return` 用 `base_idx + hold_days` 偏移的是 **K 线根数**，不是日历日；
2. **22×12 = 264 > `1Y`=252**，自身不自洽；
3. **影响面实测为零**：`qd_agent_traces` 根节点 **1256 条 timeframe 全空**（恒走默认 3）⇒ 22/66/252 从未被消费（死值）；`qd_agent_claims` 128 条中 `1M` 仅 3 条，且 `status=pending`、`due_date` 未到期、`resolution=0` ⇒ **无需重算**。

**统一结果**：`chain/label_spec.py` 一张 `HOLDS` 表，单位恒为**交易日**：

```python
HOLDS = {"T+1":1, "T+3":3, "T+5":5, "1W":5, "2W":10,
         "1M":21, "3M":63, "1Y":252, "n/a":0}
```

- 反查表 `HOLD_LABELS` 用**先到先得**（不是后覆盖）：`T+5` 与 `1W` 同为 5 天，用 `{v:k for k,v in HOLDS.items()}` 会被 `1W` 覆盖 ⇒ `hold_label(5)` 静默变 `"1W"`。
- `hold_days()` 严格（未知返回 `None`）；`resolve_hold_days()` 宽松（**全项目唯一的兜底处**）。
- 旧名 `CLAIM_HOLDS` / `TRACE_HOLDS` / `CLAIM_LABELS` / `TRACE_HOLD_DEFAULT` 及对应函数**全部删除、不留别名**（留别名 = 给分裂留复活入口）。
- 防复活守卫 3 条（`tests/test_v5_label_spec.py`）：真源唯一性、长周期自洽（`1M×12==1Y`、`3M×4==1Y`、`2W==2×1W`）、`chain/*.py` 不得**定义**旧名（允许注释提及溯源）。
- 变异检验 **8/8 全被抓到**；回归 v5 单测 120 passed / 1 skipped、全量 511 passed / 4 skipped、`compileall` rc=0 —— 零回归。
- `bar_freq()` 读 `QD_BAR_FREQ`（默认 `1d`），为分钟线预留但**暂不开库**（§10.5 / 边界第 8 条）。

---

## 附录 D：缺陷与缺口总表

### D.1 执行系统缺陷（D-1~D-21）

| # | 问题 | 影响 | 现状 |
|---|---|---|---|
| **D-1** | 因子层未闭环：`get_factor_weights` 恒返回空 | 权重闭环只到 domain | 🟨 **2026-10-14 复核（审计 A-05）拆成两种截然不同的情况，别再一刀切**：① **样本层**：`qd_agent_weights WHERE layer='factor'` **实测 0 行**（`tmp/probe_factor_weights_20261014.py`）⇒ 两个消费方当前都走回退常量，这层随样本自愈，**getter 实现是对的**（不是"恒空的实现问题"）。② **键名层**：`technical_analysis` **键自洽**（产出的 factors 名 = 趋势/指标/量价/形态/筹码/流通盘，与自己的权重键同名）⇒ 攒够样本自动命中；`bull_bear_research` **2026-10-14 已对齐**（外层 factors 名改为 ma/macd/rsi/boll/kdj，与 `_BB_DEFAULT_WEIGHTS` 键**同名**，多空论据仍保留在 output_data.bull_case/bear_case）⇒ **键名层已闭环**，只剩样本层 0 行随样本自愈。已落：两侧代码注释改准确 + 守卫 `tests/test_v9_factor_weight_keys_20261014.py`（TA 键双向对称断言 / BB 五维键双向对称断言 / store 逐处查 layer='factor'，变异 4/4 有 bite）；BB 旧 `xfail(strict=True)` 台账已于对齐当日转为**真实断言**（XPASS 风险消除） |
| **D-2** | `cron/` 被**旧系统**反向依赖 | 删旧系统会断掉定时任务 | ✅ **已收口（2026-10-14，O-11）**：复核结论是**方向本身不是缺陷，是契约没被记录**。外部 → `app.agent.cron` 正是"方案代码只在 app/agent"的预期形态（agent 是能力提供方）；风险只在**反向**（cron → `app.routes`/`app.services`）会与消费方成环。已做三件事：① `app/agent/cron/__init__.py` 写明对外能力出口契约（三个消费方 `app/__init__.py`(启动) / `app/routes/cron.py` / `app/routes/health.py` + 迁移指引 + 已知历史包袱：routes 借用 `_execute_prompt_job`/`_execute_function_job` 两个私有名）；② 守卫 `tests/test_v6_cron_boundary.py`（导出名 8 个 / cron 不得反向 import Web·服务层 / 全 agent 不得 import `app.routes` / 契约注释不得删），**相对 import 按包解析**已覆盖（变异 5/5 有 bite）；③ 例外已登记：`tools/finance/*` 依赖 `app.services.*`（筹码唯一内核在服务层）是"不造轮子"的既定产物，故"禁 services"**只作用于 cron 子包** |
| **D-3** | ~~`preselect_stats.py` `_NOT_TOOLS` 只登记 2 个新名~~ | ✅ **已由 P10 根治（2026-10-13）**：非工具模块统一改 `_` 前缀，扫描器对 `startswith("_")` 天然跳过 ⇒ 名单方案（含 `_NOT_TOOLS`/`_SKIP_FILES`/`_ENTRY_NAME_DENY` 三套）整体删除，管线函数根本不会被扫进注册表。**不要再按本条去"补名单"** | ✅ 已解决 |
| **D-4** | 会话上下文不跨进程持久化 | 重启即失忆 | 🔒 **裁定不做**（附录 B.1） |
| **D-5** | 能力评测样本与人工基线未铺开 | 标尺可信度不足 | 🟨 标尺已立；缺真实样本 + 双人标注 |
| **D-6** | `agents/` 死代码 | 0 引用 | ✅ 已清 |
| **D-7** | `tracing.py` 读 3 个已删 prompt ⇒ `prompt_sha` 恒 missing | 复盘时对不上版本 | ✅ **已修（2026-10-13，与 G10 同源）**：`_repro_meta` 由硬编码 5 个文件名改为**扫描 `app/agent/prompts/` 目录**（`_PROMPT_SUFFIXES` + `QD_PROMPT_SHA_FILES` 收窄），结构上不可能再恒 missing。⚠️ 本行旧写「⬜ 未做（O-2 顺手解决）」与附录 D.2 的 G10「✅ 已修」**自相矛盾**，2026-10-13 复核后统一为已修 |
| **D-8** | 观测计数器进程内，重启归零 | 拿不到长期趋势 | ⬜ 未做（→ O-7） |
| **D-9** | `admission.json` 的 `doc` 字段全空 | `func_overlap` 分支永不命中 | ⬜ **doc 仍全空（实测 71/71 条为空，2026-10-13 复核）**；⚠️ 旧注「本地文件不在工作副本」**已过期** —— 该文件就在 `app/agent/capabilities/admission.json`（24 KB，未被 .gitignore 忽略，P13 已订正过一次），缺的是**内容**不是文件 |
| **D-10** | 发布工程：归档/准入件不在版本控制 | 干净 clone 缺件 | ✅ 收口（裁定不入库） |
| **D-11** | `run_skill` 白名单硬编码 | 违反"规则不在代码里" | 🟨 **2026-10-13 复核：原指控的"硬编码名单"已不存在** —— 执行型技能由各 skill 模块自行导出 `EXECUTABLE_SKILLS`（`qd_service._executable_skills()` 动态读取），按需工具开关也已是 env（`QD_ON_DEMAND_TOOLS`）。**残余项已于 2026-10-14 收口（O-11）**：① `skill_brewer` 的元工具名单由硬编码集合下沉为 `QD_META_TOOLS`（默认沿用原 6 个；空 env 回落默认而非空集）—— 之所以只能配置不能"从 provider 推导"，是因为 `list_tools`/`search_tools`/`final_answer`/`python_interpreter` 是框架内置、**不进 ToolProvider 注册表**，`get_meta_names()` 盖不全；② `_executable_skills()` 由 `spec_from_file_location` 按文件路径加载改为**包内相对导入**（按路径加载会造出第二个模块实例，与 P5 根治的"双模块实例"同类）。守卫 `tests/test_v6_d11_whitelist_knobs.py`（变异 4/4 有 bite）。⚠️ `EXECUTABLE_SKILLS` 本体仍留在 `skill_run.py`：它的取值是**模块路径 + 函数名**（代码契约），塞进 env 只会更易错，不属"阈值/口径"红线范围 |
| **D-12** | `_skill_seen` 无 LRU 淘汰 | 长跑内存增长点 | ✅ **已做（2026-10-14，O-11）**：`qd_service._skill_seen` 改为带容量上限的 LRU（`OrderedDict` + `move_to_end`，容量 `QD_SKILL_SEEN_MAX` 默认 512，脏值/非正值夹紧 ≥1）；写入与淘汰**唯一入口** `_skill_seen_for(session_id)`（键仍严格 `str(session_id)`，与 `_get_agent` 同口径）。淘汰只影响"正文 vs 一行指针"⇒ 被淘汰会话下次重注一次正文，正确性不变（fail-open 方向天然安全）。守卫 `tests/test_v6_skill_seen_lru.py`（含 AST 守卫禁止绕过助手直接 `setdefault`，变异 6/6 有 bite） |
| **D-13** | `app/agent/utils` 与 `app/utils` 同名 | ⚠️ **前提已被 P5 根治**（2026-10-13 复核）：P5 起 `app/agent/` 不进 `sys.path`、全包统一 `app.agent.*`，活代码裸名 `import utils` **0 处** ⇒ 「sys.path 顺序雷」**已不存在**。残余风险只是**人读混淆**（同名目录，grep / IDE 检索易串） | 🟨 顺序雷已消；改名属可读性改善（→ O-8 降级） |
| **D-14** | `weight_feed` EMA 参数未经真实样本校准 | 慢调强度是拍的 | ⬜ 待攒样本 |
| **D-15** | judge 未校准；LLM 网关可用性未端到端验证 | 阶段 B 不敢开 | 🟨 **网关可用性已实测（2026-10-13 晚）**：`logs/app.log` 单日 `APITimeoutError` **129 次** / `APIConnectionError` **8 次**（端点 `api.g2claw.com`；详见 §8 的 `QD_C3P_*` 超时条目与 §7.5 TYPE 标签陷阱）⇒ 状态不是"未验证"，而是**已验证为不达标**；judge 校准仍 ⬜ |
| **D-16** | 工具预选底座仍是启发式 | 同义问法召回差 | 🟨 K 共现权重已落地；召回 ≥95% 待跑批 |
| **D-17** | 超大技能靠字数截断出首段 | 首段未必最该看 | ⬜ 未做（TL;DR） |
| **D-18** | 分发降本未达标：成本比 0.889 > 0.40 | 重活分发只省 11% | 🟨 空结果纪律已接线，**待重跑** |
| **D-19** | 多轮**递进**会话按平面进闭环 | 权重层系统性污染 | ⬜ M 方案已定未施工 |
| **D-20** | 金例不"金"：低分不淘汰、词面回退 | few-shot 增益不稳 | 🟨 embedder 已接线；待高分选例 + 低分淘汰 |
| **D-21** | 关键参数全是拍脑袋初值 | 调优无依据 | 🟨 token/工具/累计步数三线已接线；日配额未接线 |

### D.2 条件化引擎缺口（G0~G23，v5 视角下的活跃项）

| 编号 | 缺口 | 性质 | 处置 |
|---|---|---|---|
| **G3** | C 类情境档案未开始记录 | 工程 | ✅ **已落地**（`context_frame.py` + 已接线） |
| **G5** | 成分股前视未量化 | 只读分析 | ⬜ 第 0 批 |
| **G7** | 无对照臂（原需 20–30 只/日） | 设计 | ✅ **天然关闭**（`p_base` 顺手算，零成本） |
| **G9** | 日配额 `add_day_usage` 未接线 | 工程 | ⬜（→ O-9） |
| **G10** | `prompt_sha` 恒 missing | 工程 | ✅ **已修**（`_repro_meta` 同口径） |
| **G15** | `evidence_class` 未入数据模型 | 数据模型 | 🟨 已入 EV-1 provenance；**但取值语义与 §10.7 不一致**，见附录 F B-3 |
| **G17** | 可成交口径四处不一致 | 工程 | ⬜ 接入前收敛成一份 |
| **G18** | `delisted` 未继承 ⇒ 幸存者偏差 | 工程 | ✅ **已落地**（P0-1 + P0-2） |
| **G19** | 🔴 **`ρ_pair` 未实测** | 只读分析（**可测**） | P2 接线后第 4 周估 `corr(p_base, p_final)`；**<0.2 则 §11.3 增益表作废** |
| **G20** | **Δp≈0 常态化 / 证据与基线共线** | 设计（可预注册检测） | 三项监控：每格报 `median(|Δp|)` 与 `P(|Δp|<0.01)`；每周算 `corr(eⱼ, p_base)`；**`P(|Δp|<0.01)>0.7` 的格冻结，降级为分组变量** |
| **G21** | **判定取价走实时行情源、历史日期取到当下价格** | 工程（小） | ✅ **已落地（2026-10-13）** —— `_get_actual_return` 已改按交易日历取足够长窗口 + 本地日线。剩余项见 O-3（基准价冻结，属加固不属修复） |
| **G22** | `p_base` 冻结模型漂移无监控 | 工程（监控，便宜） | 按季出 Brier / 校准曲线；**基线恶化超阈值触发重拟合评审**（*不许*自动重拟合） |
| **G23** | 条件格预注册台账尚未存在 | 工程（流程，小） | 建 `exp_cond_registry`；**未登记格一律不进判定** |

---

## 附录 E：文档沿革与归档索引

| 版本 | 文件 | 角色 | 状态 |
|---|---|---|---|
| v2 | `backend_api_python/app/agent/AGENT_DESIGN_v2.md` | 执行系统现状（877 行） | **已被本文取代** → 归档 `del/agent_design_pre_v3_20261013/` |
| v3（旧） | `agent扩展设计-v3.md` | 经验机制设计（1416 行） | 保留作历史；其 §20.5/§20.6/§20.7 三项已捡回（§19） |
| v4 | `agent扩展设计-v4.md` | **测量学**（2043 行） | 保留作原始论证；统计结论已吸收进本文 §10，**以本文为准** |
| v5 | `agent扩展设计-v5.md` | 预测力学（955 行） | **已被本文取代** → 归档 `del/agent_design_pre_v3_20261013/` |
| — | `agent扩展设计-v5-实施计划.md` | 施工日志（只增量追加） | **继续保留**，本文不并它 |
| **v3（本）** | `backend_api_python/app/agent/AGENT_DESIGN_v3.md` | **单一权威设计文档** | 现行 |

**修订记录**

- 2026-10-13 **合并重写（第一版）**：v2（执行系统）+ v5（条件化预测引擎）合并为单一权威文档；吸收 v4 统计地基（§10）做成自包含；新增 §22 优化分析（不考虑兼容性，13 项）；附录 C 记口径统一；附录 F 记代码偏差。
  - 一并落定的三处口径修正：① 聚类口径改以 `date` 为主（§14.4，依据 v4 §28 同题材 ρ̄ 仅 +0.0226）；② T+3 窗口重叠相关统一为 **0.667**（`=(N−1)/N`；v5 §1 C3 原文的 0.638 为笔误）；③ 条件格「12 」是**上限不是目标**，年 1 先开 ≤3 格。
- 引用纪律：**行号只作线索，函数名才是锚**；未实测参数一律标注「未实测」（典型：§11.3 的 `ρ_pair`）。
- 本文不含代码改动：施工按 §21 批次 + §22 优化顺序另行立项；**P2 的接线是当前最高优先级**（§21.2）。
- 同日补 **附录 F（代码 × 设计偏差核对表）**：全量核（v2 架构 + v5 设计），五类标注。
- 2026-10-13 晚 **设计文档回写（P13，代码 × 设计机检后订正）**
  - 机检方式：把本文拆成可判定条款逐条对代码验真，探针 `tmp/probe_design_drift_20261013.py`
    （可复跑，结果落 `tmp/design_drift_result_20261013.json`）。
    **可判定 90 项 / 通过 76 / 偏离 14 / 偏离率 15.6%**；归因：**12 条是文档滞后，仅 2 条是代码缺陷**。
  - ① **§2.2 目录结构**：删 `llm/` 目录（原 7 个文件，P2 已整目录删除，出口唯一化到 `app.agent.llm`）；
    补 P6 拆分产物 9 个模块（`qd_tool_face` / `qd_preselect` / `qd_aftercare` / `qd_budgeting` /
    `qd_grounding` / `qd_guards` / `qd_hooks` / `qd_config` / `skills_adapter`）；
    修 tools 清单（删 P10 改名前的旧名 `base.py` / `availability.py` 与重复列出的
    `tool_discovery.py`）；chain 19→22；能力层 40→39（含 1 项导入失败）；
    订正 `admission.json`「不在工作副本里」这一与事实相反的描述。
  - ② **数字统一到实测并在 §1.1 固定口径与时点**：工具面 102→**92**、必注入 22→**21**、
    全量 schema 32,703→**49,507**、全量 tests 529→**761 passed**、§22.4 工具面 58→**53**
    （成本表按新参数重算，节省率结论不变）。⚠️ 早期版本四个数字分属不同时点且口径不一，现全部作废。
  - ②b **能力层准入 69→68**：2026-10-13 晚复核发现 `app.market_cn.auto.store:get_signal_by_code`
    是**幽灵条目**——函数从未存在（store.py 19 个公开函数无此名），已按 admission 既有范式
    （参照 `signal_row`）改为 `admitted:false` 并留 `recheck_note`。**零行为变更**：能力层
    注册数始终为 39（它从来没注册上），只是不再每轮报"1 项未注册"。缺口已如实记入
    `recheck_note`：同模块 `list_signals` 无 `code` 参数，"按代码查单票信号"当前无实现。
  - ③ **`_NOT_TOOLS` 标注为「已由 P10 根治」** —— 涉及 §2.3 / §7.2 / §22.1 O-11 /
    附录 D D-3 / 附录 F D-6 共 **5 处**，并写明「别照旧文档补名单，加了等于回退」。
    **这是本次最高优先级的一条**：否则后人会按缺陷表去"修复"一个已被根治的问题。
  - ④ **§3.2「域内优先」① 标注为代码未落实**（`qd_preselect.py` 调
    `build_catalog_grouped(defs, dom_names, [])` 第三参传空 ⇒ 能力层不进目录）。
    **保留设计断言、不因代码暂时没做到就降低要求**，修回动作归 P11。
  - ④b **上一条已于同日 P14 修回**：`qd_preselect.py` 第三参改回 `cap_names`，末尾
    计数指针行删除（能力层已列进目录第二段，指针既冗余且与 `PRESELECT_SYSTEM`
    「目录找不到就删交付要素」冲突）。§3.2 已改标 ✅ 并记入代价实测（+2,802 字符）。
  - ⑤ 修 **§3.13 与 §9 的内部矛盾**（smoke 项数 248 vs 256，统一为 **256**）。
  - 📌 **本次订正立下的纪律**：本文中的「实测」数字**必须标时点与口径**；
    改代码批次（P2/P6/P9/P10/P12）后**必须回写本文全文的相关引用**，而不只是补自己改的那一段。
- 2026-10-13 深夜 **二次审核订正（superpowers + 架构气味视角，机检 99 项全 OK / 偏离率 0.0%）**
  本次专查「文档自身可优化处」，主攻**过时事实**与**内部不一致**，不改任何规范性断言。
  - 🔴 **C-1 §7.1 整节描述的是 P5 之前的世界**（本次最高价值发现）：该节自称「导入与路径，
    本包统一**裸名**，最容易踩」，但实测**活代码裸名 import 0 处**（仅 `tmp/*.bak` 残留）、
    `ensure_path_order()` **全仓 0 命中**、`app/__init__.py` 头注明载 P5 已统一为 `app.agent.*`
    （M-1 双模块实例病根已消失）。**读者照它去调 sys.path 顺序、或去"同时 patch 两条路径"，
    等于按已根治的旧坑施工**。处置：节标题改为「P5 起统一 `app.agent.*`」，7 条逐条标
    ✅ 已根治（保留历史教训）/ ⚠️ 仍有效，并补第 7 条「扫 import 要覆盖三种形式」。
  - 🔴 **C-2 连带 D-13 / O-8 前提失效**：「`app/agent/utils` 与 `app/utils` 同名 = sys.path
    顺序雷」随 P5 已消（裸名 `import utils` 0 处）⇒ D-13 改记「顺序雷已消，残余只是人读混淆」，
    **O-8 改名从"排雷"降级为可选**（不必抢在 O-1 前后做）。
  - 🔴 **C-3 守卫缺口**：P5 的红线守卫 `tests/test_v6_import_modernize.py`（零裸名/零 sys.path/
    零断裂引用）已随 **P16 归档**进 `del/agentcore_retired_20261013/tests/` ⇒ **现无守卫**，
    重新引入裸名 import 不会 FAIL。已在 §7.1 记明，建议取回该守卫（它守的是 P5 契约，
    与 agentcore 退役无关）。
  - **C-4 数字与口径统一**：§9 全量 tests 761 → 记 **652 passed / 4 skipped**（P16 后实测，
    归档 6 个 `test_v6_*` 所致，业务测试一条未少，761 留作 P12 时点沿革）；
    smoke 项数 **256 → 257**（§2.2 / §3.13 / §9 / 附录 F C-14 四处统一，实测 255/257）；
    §2.2 能力层 **准入 69 → 68**（幽灵条目改 `admitted:false` 后排除 2→3，注册数恒 39）。
  - **C-5 文内自相矛盾**：§1.3「金融域工具面（**102**）」与 §1.1「102 已作废」冲突 ⇒ 改 92 并留痕。
  - **M 排版**：§1.1 三行约束表被 📌 引文块**从中间截断**（渲染成两个断表）⇒ 引文块移到表前；
    §3.5「表分层」整段中文被误包进 ASCII 图代码块 ⇒ 移出代码块。
  - **机检探针自身两处假检测**（`tmp/probe_design_drift_20261013.py` 已修）：
    ① D20 把注释/文档里的**沿革叙述**当残留引用，恒报假偏离 ⇒ 改只认**活 import** 行；
    ② F4 把结论**硬编码成实参**（恒输出"§3.13 写248 / §9 写256"），根本没读文档 ⇒
    改为真解析两处数字比对。修后 **可判定 99 项 / OK 99 / DRIFT 0 / 偏离率 0.0%**。
  - ✅ **P18-b §2.2 目录树补「消费方」（用户裁定"修补吧"，2026-10-13 21:35）**：
    `rag/ memory/ utils/ resolvers/ cron/ audit/ preselect/` 七目录原被压成一行、
    只有一句功能说明，**没写谁在消费** —— `rag/`（1230 行）因此一度被误判死代码。
    现逐行展开为「目录 + 规模 + 职责 + **真实消费方**（文件:行号）」，并把易错点标进树里：
    · `rag/` 消费方全用**相对 import**（`.rag.X`），只扫绝对形式会漏成零引用；
    · `utils/` 是全包最大枢纽（**28 个消费方**：chain×4 / qd_*×4 / scripts×3 /
      tools/finance×11 / tools/knowledge / trace_collector / tests×3）；
    · `cron/` 消费方**全在包外**（`app/__init__.py:423` 启动 + `routes/cron.py` +
      `routes/health.py`）⇒ 这正是 D-2「被旧系统反向依赖」的实证；
    · `audit/` 仅 `qd_agent.py:53` 一处消费，`preselect/` 8 处。
    数据来源：`tmp/probe_agent_consumers_20261013.py`（三形式 import 解析，
    扫描范围**全仓**——初版只扫 `app/agent/` 会把 cron 的包外消费方漏成"仅自引用"）。
    机检复跑 **99 项全 OK / 偏离率 0.0%**，不变。
- 2026-10-14 **P3 条件校准接线（附录 F A-6 关闭，孤岛清零）**
  - 🔴 **动了什么**：`chain/calibration.py` 此前是**唯一还断着的孤岛**（`fit_with_pooling()`
    只被自身 `calibration_report()` 调用）⇒ 生产链路 `aggregate(coefs=None)` ⇒ **Δp 恒为 0**。
    新建 **喂数层 `chain/calib_feed.py`**（与 `weight_feed` 同范式：IO 与纯算法分家），
    `c3p._c3p_pipeline` 的 `coefs` 实参改由 `calib_feed.coefs_for(ck)` 供给。
  - **新增模块 `chain/calib_feed.py`**（`build_samples` / `build_cells` / `coefs_for` /
    `build_report`）；`chain/calibration.py` **一行未改**（保持纯函数、零 IO）。
  - **顺带收敛的重复**：verdict 计分此前只有 `weight_feed._VERDICT_SCORE` 一份，
    P3 是第二个消费方 ⇒ **上移到 `chain/schema.py`**（`VERDICT_SCORE` / `COUNTED_VERDICTS`），
    `weight_feed` 保留原私有别名做引用（不是抄一份）。守卫：
    `test_v5_calib_feed.py::test_verdict_score_has_single_source` 断言二者是**同一个对象**
    （2026-10-13 踩过「改了计分漏了 SQL 白名单」的分叉坑，见附录 F C-2）。
  - **新增查询** `account_store.load_latest_resolutions()`（PG 侧 `DISTINCT ON (claim_id)`，
    一 claim 一票 G24）；`weight_feed` **未改**（它有守卫在守，改动收益 < 风险，
    已在函数 docstring 记明「同一条纪律两个实现，将来应收敛到本函数」）。
  - **口径 decisions**（详见新增 §21.4）：JOIN 键 = `claim_id`；只收与 `p_base`
    同口径的 horizon（默认 `T+1`，`QD_CALIB_HORIZON`）；`n_clusters` 走
    `stat_power.estimate_independent_clusters` 折算（**不用名义样本数**）；
    样本不足 ⇒ `coefs_for()` 返回 `None`（P3-3 禁进判定），**不编系数**。
  - **τ 为什么必须落盘**：若每次冷启动都重拟合，`fitted_at` 恒为 now ⇒ τ（季度重估）
    形同虚设 ⇒ 系数落 `data/calib/coefs_cal1.json`，τ 内跨进程复用（有单测守）。
  - **守卫与验证**：AST 级接线守卫 `test_v5_c3p.py::test_aggregate_coefs_is_not_constant_none`
    （全文匹配会被注释里的 `coefs=None` 骗过去 ⇒ 用 AST 只认真实调用点）；
    **变异检验 4/4 全被抓到**（`tmp/mutate_p3_guard_20261014.py`：
    coefs 回退常量 None / 取第一条留痕 / 用名义样本冒充簇 / 落盘失效）。
  - **回归**：全量 `tests/` **652 → 676 passed / 4 skipped**；v5 单测
    **138 → 162 passed / 1 skipped**（14 个文件）；`qd_smoke` **255/257 / 0 failed** 不变。
  - ⚠️ **别误判**：接线后 **Δp 仍然恒为 0**，因为样本还没攒够（当前 2 条 pair、0 条判定
    ⇒ 全部只出先验）。这是**设计行为**，不是回退。看接线生效与否请用
    `calib_feed.build_report()`（每格的 n_obs / n_clusters / β₀ / source_level）与接线守卫。

- **2026-10-14 晚 · O-11 小项打包（D-11 残留 / D-12 LRU / D-2）+ 死代码复核归档**
  - **D-11 残留**：`chain/skill_brewer` 的元工具名单由硬编码集合下沉为
    `QD_META_TOOLS`（默认沿用原 6 个；空 env 回落默认**而非空集**）。**为什么只能配置**：
    `list_tools`/`search_tools`/`final_answer`/`python_interpreter` 是框架内置、
    不进 `ToolProvider` 注册表 ⇒ `get_meta_names()` 盖不全，不能假装"零配置推导"。
    另：`qd_service._executable_skills()` 由 `spec_from_file_location` **按文件路径加载**
    改为**包内相对导入** —— 按路径加载会造出第二个模块实例（与 P5 根治的"双模块实例"
    同类）；P5 后 `app/agent` 不进 sys.path，"防顶层 scripts/ 抢解析"的前提已不成立。
    ⚠ 源码守卫用 **AST 找调用**，不能用 `in src`：docstring 里写了
    "spec_from_file_location" 解释为什么废弃它 ⇒ 全文匹配恒红（P3 同款陷阱的反面）。
  - **D-12**：`_skill_seen` 改为带容量上限的 LRU（`OrderedDict` + `move_to_end`，
    `QD_SKILL_SEEN_MAX` 默认 512，脏值/非正值夹紧 ≥1）。写入与淘汰**唯一入口**
    `_skill_seen_for()`，AST 守卫禁止绕过助手直接 `setdefault`。
    淘汰只影响"正文 vs 一行指针" ⇒ 被淘汰会话下次重注一次正文，正确性不变。
  - **D-2**：判定为「**对外能力出口契约未记录**」而非缺陷 —— 外部 → `app.agent.cron`
    正是"方案代码只在 app/agent"的预期形态；风险只在反向（cron → `app.routes`/
    `app.services`）会成环。已把契约写进 `cron/__init__.py` docstring（三个消费方 +
    迁移指引 + 已知历史包袱：routes 借用两个私有名），并用
    `test_v6_cron_boundary.py` 钉住导出名与方向红线（**相对 import 也解析**）。
    例外登记：`tools/finance/*` 依赖 `app.services.*`（筹码唯一内核在服务层，
    "不造轮子"的既定产物）⇒ "禁 services" 只作用于 cron 子包，不下到全 agent。
  - **死代码**：`tools/_pagination.py`(444) 经 `tmp/probe_deadcode_refs_20261014.py`
    复核（三形式 import + 全仓扫描 + **同名异物判定**）确认零引用 ⇒ 归档
    `del/deadcode_20261014/`（含 README：回滚方式与基线回改点）。
    **函数级只出清单不动活代码**；其中 `budget.add_day_usage` 等是**设计 O-9 的
    待接线点**、不是死代码（外部审查报告只做引用扫描、没对照设计文档 ⇒ 报告的问题）。
  - ⚠️ **`memory/redis_memory.py` 同批被误判并归档、当日回滚**：探针的相对 import
    解析在 `__init__.py` 上**少剥一层** ⇒ 漏掉 `memory/__init__.py:8` 的
    `from .redis_memory import RedisMemory` ⇒ 判成"零引用"。归档后
    `import app.agent.memory` 报 ModuleNotFoundError —— **由导入冒烟抓到，
    全量 pytest 没抓到**（无单测 import 该包）。事实是 `RedisMemory` 在
    `memory/__init__.py` 的 `__all__` 里 = **包的公开导出**，不属于死代码。
    **教训已固化**：判"零引用"前必须做导入冒烟，静态扫描的口径 bug 会以
    "假零引用"的形式吃掉活代码（这是本仓第三次同类事故，前两次是 P17-b / P18-b）。
  - **变异检验**：D-12 6/6、D-11 4/4、D-2 5/5，全部有 bite
    （`tmp/mutate_d12_lru_20261014.py` / `mutate_d11_knobs_20261014.py` /
    `mutate_d2_cron_20261014.py`）。
  - **回归**：全量 `tests/` **676 → 716 passed / 4 skipped**；设计机检
    **100/100 OK / 偏离率 0.0%**（新增 A21/A22 守"这两个文件应已归档"）。
- 2026-10-15 **§5.7 回测桥数据流复核**：立项稿的 `proposal.apply` / 活 `strategies/` 落盘 / 同进程 `run_all` 违反「不污染其它回测链路」。改为子进程 `backtest_cli` + 作业目录 overlay；全盘走技能（能力层 60s 不够）；工具不塞进现有 `backtest_tools.py`。施工清单：`backtest_bridge_plan.md`。
- 2026-10-15 **§5.7 回测桥施工回填（终态）**：双泳道 9 任务建成（批次A-E；施工中计划修订 v2-v6 见 `backtest_bridge_plan.md` 文末，与初稿差异的终态细节见 §5.7 末「施工回填」块）。要点：overlay 三钩子（`_resolve_yaml_path`/`_import_overlay_modules`/`_overlay_yaml_path`，exec 失败清 sys.modules、fail-closed AST 预检 `agentbt_` 前缀）；桥核心 v4 修正（existing 读 `carrier["key"]`、proposal 形状校验前移、`_assert_guards` 上移 mkdir 前、`os.sep` 锚定、`_build_report` 补 meta 透传、`_manifest_is_stale` 判死、posix start_new_session、空 codes 拒 scope=codes）；`parse_carrier` 载体规整单源（工具/技能两面收敛）+ 失败形态统一裸 `{"error"}`（finance 域 `_fn_adapter` 单键判据）；AST 守卫单源 `tests/_ast_guard.py::_ast_violations`（import_prefixes/call_names/package，相对导入解析 + fail-closed，svc CLI 守卫 3 个全 AST 级）；svc 泳道（manifest `lane` 字段/`_spawn_and_wait` 共用/`backtest_svc_cli.py` 零落库/工具 `agent_backtest_svc`）+ market 枚举单源 `data_sources/factory.py::SUPPORTED_MARKETS`（7 市场，不硬编码）+ `cost_model` 口径标注（显式 0=零费率毛收益 vs None=引擎默认）+ svc 结论「探索性参考」+ user_id 全链透传。不变量：`services/backtest.py` 零改动；测试 `test_backtest_bridge.py` 23 例 / `test_auto_bt_tools.py` 24 例 / `test_auto_overlay_hooks.py` 8 例各自独立全绿。署名：Trae agent / backtest_bridge_plan·Task 9（批次F）。

---

## 附录 F：代码 × 设计偏差核对表（2026-10-13 全量核）

> 核对方式：逐条拿设计断言去代码里找**消费方 / 常量 / 取值**，不是看文档有没有写。
> 分类：**① 已落地** · **② 部分落地** · **③ 未落地** · **④ 代码超前于设计** · **⑤ 设计与代码冲突**。
> 引用纪律：行号只作线索，函数名才是锚。

### F.1 五类汇总

> **核对时点**：初核 2026-10-13（合并 v3 时）；**复核 2026-10-13 晚（P2 接线完成后）**。
> 复核把 A 组的 5 条「未接线」变成了 4 条「已接线」，下表已更新为复核后状态。

| 类 | 条数 | 代表项 |
|---|---|---|
| ① 已落地 | **19** | `delisted` 单列、`label_spec` ls2、情境帧、分层配额、双窗口、`prompt_sha`、判定取价修复、**C3P ②③ 段接线**、**R2 守卫**、**EV-1 写侧** |
| ② 部分落地 | **3** | D-18、D-20、G15（`evidence_class` 语义） |
| ③ 未落地 | **5** | **P3 校准未接线**、`exp_*` 三表、G23 台账、`ρ_pair` 实测、日配额 |
| ④ 代码超前于设计 | **2** | 判定取价修复（设计还写着"未落地"）、`sample_layer` 分层记账 |
| ⑤ 设计与代码冲突 | **3** | `evidence_class` 语义、`ev_ver` 取值、条件键默认维度与聚类口径 |

### F.2 逐条明细

#### 🔴 A 组：C3P 流水线的接线缺口（**P2 接线已解决 4 条**）

| # | 设计断言 | 代码事实 | 类 |
|---|---|---|---|
| **A-1** | §12 段②：LLM 推理产出 `EvidenceVector` | ✅ **已接线**（2026-10-13 晚）：`chain/c3p.py::extract_evidence` 调 `evidence.build_evidence()` + `append_evidence()`。取数方式 = **收尾一次轻量 LLM**（已拍板方案，见 §21.2 注） | ① |
| **A-2** | §15 段③：聚合器产出 `p_final` | ✅ **已接线**：`c3p.run_for_decision()` 调 `aggregator.aggregate()` | ① |
| **A-3** | §16 段④：一次推理落三条，`Δp` 进判定 | ✅ **已接线**：`c3p` 按 **claim_id** 调 `aggregator.record_pair()`，落 `p_base`/`p_final`/`delta_p`；`exp_pair` 表未建（落 JSONL，符合"先落 JSONL"决策） | ① |
| **A-4** | §13：EV-1 随案例入库（P1-2） | ✅ **写侧已通**：`qd_agent._record_case()` 传 `evidence=`（值来自 `_c3p_pipeline`）。守卫 `qd_smoke` test_40 ③ 断言实参**不是常量** `None`（变异检验 M6 证明该判据有 bite） | ① |
| **A-5** | §15.2：`p_base` 复用 `predict_next_day` | ✅ **已接线**：`c3p.p_base_of()` → `chain/kline_feed.py` → `predict_next_day`；`_MIN_BARS` 与 `predict.MIN_BARS` 有守卫测试绑死（防两边漂移） | ① |
| **A-6** | §17.3：层级池化产出 β | ✅ **已接线（2026-10-14 P3）**：`chain/calib_feed.py` 把 pairs × resolutions 喂成 cells，`fit_with_pooling` 由此进生产链路（`c3p._c3p_pipeline` 的 `coefs` 实参）。⚠️ **冷启动期仍返回 None**（簇数不足只出先验）—— 这是设计行为，不是回退；判别看 `build_report()` 与接线守卫 | ③ |
| **A-7** | §21.2：采集链 | ✅ 采集链：`evaluator` → `collector.run_daily_collection()` → `context_frame.build_frame()` + `sampler.pick_daily_universe()`；`data/context_frames/context_frames_2026-10.jsonl`（1 帧）、`data/daily_universe/daily_universe_2026-10.jsonl` 均已落盘 | ① |

> **P2 接线的接线点与形态**（2026-10-13 晚）
> ```
> qd_agent._finish_collector
>   → _intake_accountability   # 产出 decision_id / claim_ids / claims
>   → ★ _c3p_pipeline          # 新增：chain/c3p.py 编排，claim 级
>   → _record_case             # 拿到 evidence，落 outcome.evidence
> ```
> 新增模块：`chain/c3p.py`（编排）、`chain/kline_feed.py`（K 线统一取数，收敛了第 3 处重复）。
> 新增 env：`QD_C3P_ENABLED`(1) / `QD_C3P_MAX_CLAIMS`(2) / `QD_C3P_LLM_MODEL`("") /
> `QD_C3P_LLM_TIMEOUT`(20s) / `QD_C3P_BARS`(120)。
> 变异检验 **7/7 全被抓到**；回归：v5 全量 138 passed/1 skipped、全量 tests 529 passed/4 skipped、
> smoke 254/256（0 failed / 2 skipped）—— 零回归。

#### 🟡 B 组：语义冲突（设计要写清楚，否则后人按错的那个改）

| # | 设计断言 | 代码事实 | 类 | 处置 |
|---|---|---|---|---|
| **B-1** | §10.7 `evidence_class ∈ {live, rule, replay}`（**污染等级**，决定能不能对外报数） | `evidence.py::_evidence_class()` 返回 **`weak/medium/strong`**（按有无 `falsifiers`/`discriminators` 判，=**证据强度**）；全仓 `live/rule/replay` **零命中** | ⑤ | 两者**都需要**但不能互相顶替：建议 `provenance.evidence_class` 用 `live/rule/replay`，新增 `evidence_strength` 承载 `weak/medium/strong`（`evidence_strength` 当前已是 0.2~1.0 的连续值，可并存） |
| **B-2** | §13 `ev_ver: "EV-1"` | `evidence.py:EV_VER = "ev1"`、`PRODUCER_VER = "ev1-producer"`；`aggregator.AGG_VER = "agg1"`；`sampler._SCHEMA_VER = "smp1"`；`context_frame` 帧内 `"schema_ver": "cf1"` | ⑤（命名） | 值域统一即可，**不要**改成带连字符（已是 5 个模块的一致风格）；§13 的示例按实际值更正 |
| **B-3** | §14.4 聚类以 `date` 为主（v3 新裁定） | `evidence._DEFAULT_DIMS = ("regime","vol_bucket")`，**不含 theme** ⇒ 与裁定一致 ✅；但 v5 §7.2 原文「按 `date×theme` 折算」与代码口径不一致 | ⑤（文档内部） | §14.4 已统一为 `date`；v5 原文作废 |
| **B-4** | §14.2 `max_cells=12` | `evidence.max_cells()` 默认 12、`cell_count()` = 3×3 = **9**、`assert_cell_budget()` 超预算 warning 不崩 ✅ | ① | — |

#### 🟢 C 组：已落地项（核对通过，列此防回退）

| # | 设计断言 | 代码证据 |
|---|---|---|
| **C-1** | P0-1 `delisted` 单列 | `chain/resolver.py:386` `verdict="delisted"`、`verdict_score=0.0`、`attribution="delisted"` |
| **C-2** | P0-2 `_VERDICT_SCORE` 补 `delisted` | `chain/weight_feed.py:55` `_COUNTED_VERDICTS` 含 `delisted`；`:59` `_VERDICT_SCORE["delisted"]=0.0`；`:68` `_BUCKET` 含 `delisted`。⚠️ 源码注释记了一次教训：只改 `_VERDICT_SCORE` 而漏 SQL 的 `IN` 列表 ⇒ 进了分但进不了账 |
| **C-3** | P0-3 判定取价本地化（G21） | `chain/evaluator.py` `_get_actual_return`：按 `trading_calendar.trading_days_count` 算真实跨度取 K 线（`QD_EVAL_KLINE_MAX` 上限 500），走 `CNStockDataSource().get_kline(...,"1D",...)`。源码注释含实测反例 |
| **C-4** | P1-1 口径单一真源 | `chain/label_spec.py` `HOLDS`（1M=21/3M=63/1Y=252）；`evaluator` 委派 `_TIMEFRAME_DAYS = _ls.HOLDS`；旧名零定义（守卫 `tests/test_v5_label_spec.py`）。见附录 C |
| **C-5** | D1 情境帧 | `chain/context_frame.py`（392 行）；实测帧 `{"schema_ver":"cf1","as_of":"2026-10-12","market_closed":true,"regime":"down","theme_heat":"cold","vol_bucket":"low","index_ret":-0.43,"limit_up_cnt":46,"turnover":7.15e11,"index_code":"000001","missing":{}}` |
| **C-6** | D2 分层配额 + 分层记账 | `chain/sampler.py`：`sample_layer` 进 items；`LAYERS`/`_quota()` 按层强制。产物 `data/daily_universe/daily_universe_2026-10.jsonl` |
| **C-7** | D3 双窗口 | 同条 resolution 挂 T+1/T+3；`_get_actual_return(extra_holds=[1])` 一次取数出多窗口，`windows` 键为**天数字符串** |
| **C-8** | P1-3 注入块升级 | `case_memory.py:446/472-476` 读 `outcome.evidence` 的 `condition_key`/`discriminators`/`falsifiers` |
| **C-9** | P1-4 `prompt_sha` | `utils/tracing.py:285-298` 注释记录原「3/5 恒 missing」根因并修；`evidence._prompt_sha()` 复用同一口径 |
| **C-10** | 红线 R1 双道拦截 | `evidence._strip_llm_probability`（生成侧）+ `aggregator._scan_r1`（聚合侧），`_P_KEYS` 覆盖 11 个中英键 |
| **C-11** | 红线 R2（当前未被违反） | `app/watchlist/` **未** import `app.agent.chain`；`predict.py` 只吃 `pred_weights.json`，该文件无经验回写方 |
| **C-12** | §15.1 聚合器三条硬设计 | `aggregator.logit()` 夹紧 `(1e-6, 1-1e-6)`；`clipped=True` 留痕；`_coef()` 非数字**抛错走 fail-open**（不是静默当 0） |
| **C-13** | 金例压缩存活（§3.11） | `tests/test_v5_compaction_guard.py` 存在；`_QD_COMPACT_NOTES` 已加保护指令 |
| **C-14** | 回归基线 | `qd_smoke.py` **40 个 test 函数**（P2 后 +1：`test_40_c3p_wiring_and_r2_guard`）；对外口径 **257 项**（255 pass / 2 skip，**2026-10-13 晚实测**；P2 时点曾记 256 项 / 254 pass，现以 §9 为准）。v5 单测 **13 个文件**（`test_v5_aggregator / c3p / calibration / case_injection / collector / compaction_guard / context_frame / dual_window / evidence / label_spec / p0_closure / prompt_sha / sampler`） |
| **C-15** | P2 接线 | `chain/c3p.py`（编排）+ `chain/kline_feed.py`（K 线统一取数）；`qd_agent._c3p_pipeline` 挂在 `_finish_collector` 中、夹在 intake 与 record_case **之间**（顺序由 smoke test_40 ② 守着） |

#### ⚪ D 组：仍未落地（清单，不再展开论证）

> ⚠️ **编号空间提示**：本组的 `D-1~D-9` 是**附录 F 自己的序号**，与附录 D 的
> `D-1~D-21`（执行系统缺陷）**不是同一套编号**。引用时请写明「附录 F D-6」/
> 「附录 D D-3」，否则极易张冠李戴（这两个编号下恰好都躺着 `_NOT_TOOLS` 这一项）。

| # | 项 | 证据 |
|---|---|---|
| **D-1** | P2-4 R2 静态守卫 | `qd_smoke.py` 全文**无** `watchlist` 扫描断言（红线当前未被违反，但无守卫 ⇒ 将来有人接上不会 FAIL） |
| **D-2** | `exp_pair` / `exp_cond_calib` / `exp_cond_registry` 三表 | `migrations/` 全量 grep **零命中**（符合「先落 JSONL、不建表」决策）。🟨 **2026-10-14 部分缓解**：P3 接线已不依赖这三表 —— `calib_feed` 用「pairs JSONL × resolutions（PG 侧 `DISTINCT ON`）」在 Python 侧 JOIN，系数落 `data/calib/coefs_cal1.json`。**按格 JOIN 的能力仍缺**（O-4 批次，不阻塞 P3 已接线） |
| **D-3** | G23 条件格预注册台账 | 无 |
| **D-4** | G19 `ρ_pair` 实测 | 一条 Δp 都没有 ⇒ 无从估 |
| **D-5** | G9 日配额 `add_day_usage` | 零调用方（**注意：是设计 O-9 的待接线点，不是死代码** —— 外部审查报告据此列进"死代码清单"属误判，勿删） |
| **D-6** | ~~D-3 `preselect_stats._NOT_TOOLS` 只登记 2 个~~（原证据：`_NOT_TOOLS = {"record_task_calls","pairs_path"}`，而公开函数有 7 个 ⇒ 5 个管线函数被误注册） | ✅ **已由 P10 根治**：预选管线已于 P9 迁出 `tools/` 到 `app/agent/preselect/`，且 P10 起非工具模块走 `_` 前缀、名单机制删除 ⇒ 双重不再成立。工具面实测 common 由 16 降为 **11**（消失的 5 个即这些误注册函数） | ✅ 已解决 |
| **D-7** | D-11 `run_skill` 白名单硬编码 | `scripts/skill_run.py::EXECUTABLE_SKILLS` 仍是字典 —— **2026-10-14 裁定保留**：它的取值是模块路径+函数名（代码契约，非阈值/口径），搬进 env 更易错。`qd_service._executable_skills()` 把它当**单一真源**读取并缓存（2026-10-14 起改为**包内相对导入**，不再是按文件路径加载 ⇒ 消除第二个模块实例） |
| **D-8** | D-7 `prompt_sha` 的**旧链路**残留 | `tracing.py` 已修；但 D-7 原文指向的「读已删 prompt」问题随 O-2（traces 退役）一并消失 |
| **D-9** | D-8 观测计数器进程内 | `preselect_stats` / `judge_stats` 均为模块级字典 |

#### 🔵 E 组：代码超前于设计（设计文档已滞后，本文已补）

| # | 事实 | 本文处置 |
|---|---|---|
| **E-1** | P0-3 判定取价 **已修**（2026-10-13），而 v5 §14 P0-3 / §16 G21 仍写「未做」 | §21.2 + 附录 D G21 已改 ✅；O-3 降级为「加固」 |
| **E-2** | `sample_layer` 分层记账已落代码，且 `context_frame` 已实测出 `regime=down / theme_heat=cold / vol_bucket=low` 三值齐备 | §21.3 已写明；§14.1 维度表按实测值对齐 |
| **E-3** | `predict_next_day` 在 `market-screener` 里**已经在用**（设计只说「P2 要复用它」） | §21.2 A-5 标注为「旁路有、主链无」，并把 O-5 的接线路径指向它 |
| **E-4** | `weight_feed` 的 SQL `IN` 列表坑已被注释固化（只改 `_VERDICT_SCORE` 不够） | 附录 F C-2 记录，防回退 |

### F.3 一张图看懂当前接线状态

```
✅ 已通:   evaluator ──► collector ──► context_frame ──► data/context_frames/*.jsonl
                              └──────► sampler      ──► data/daily_universe/*.jsonl
           evaluator ──► resolver(含 delisted) ──► weight_feed ──► qd_agent_weights
           qd_agent  ──► intake ──► decisions/claims ──► (T+N) ──► resolutions
           qd_agent  ──► ★ _c3p_pipeline ──► evidence ──► build_evidence ──► data/evidence/*.jsonl
                                        ├──► kline_feed ──► predict_next_day ──► p_base
                                        ├──► calib_feed ──► calibration.fit_with_pooling ──► β₀  ★P3(2026-10-14)
                                        ├──► aggregate(p_base, ev, ck, coefs=β₀) ──► p_final
                                        ├──► record_pair() ──► data/pairs/*.jsonl
                                        └──► evidence ──► case_memory(outcome.evidence)

✅ 孤岛清零(2026-10-14 复核): 四个孤岛(evidence / aggregator / P1-2 写侧 / calibration)
   全部接通 —— 最后一个 `calibration` 由新增的 `chain/calib_feed.py` 接上。

⇒ 结论(复核后): 采集在跑、标签在判、权重在喂、系数在算,
   「经验 → 概率位移」主链 **端到端已通**。
   ⚠️ 但 Δp **仍然恒为 0** —— 不是没接线,而是**样本还没攒够**:
      独立簇 < min_clusters(默认 30) ⇒ 只出先验 ⇒ coefs=None ⇒ p_final == p_base。
      判别接线是否生效: 看 `calib_feed.build_report()` 的格子/簇数,不是看 Δp。
```

**复核说明（2026-10-13 晚）**：初核时 A 组 5 条「未接线」中有 4 条在 P2 接线后转为「已接线」；
`chain/kline_feed.py` 是本次为收敛「K 线取数」第 3 处重复而新建的统一接口
（另两处：`evaluator._get_actual_return` 内联、`skills/market-screener/common.py::fetch_kline`）。
`evaluator` 的迁移**未在本批做**（它刚在 P0-3 修过、有守卫在守，动它风险大于收益），记为遗留项。
