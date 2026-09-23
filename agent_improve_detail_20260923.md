# Agent 提智改进 · 细化设计(对应 7 条意见)

> 来源:任务"agent 改进意见细化" | 2026-09-23 | 署名:OpenClaw agent
> 基线:AGENT_DESIGN.md v8.0 + app/agent 全量代码审计(见 agent_arch_review_20260923.md)
> 约定:定位用函数名;新增常量走 env/登记表不硬编码;改 code_agent.yaml 规则编号前必查
> 外部交叉引用(现存「规则 9」「规则 13」被 task_agent.py 注释引用,新规则一律**追加在尾部**)。
> 本文是设计细化,未动任何生产代码。

---

## 0. 评测集先行 —— 细化

### 0.1 一个前置依赖必须先修(否则评测也被骗)

**审计 A1**:`task_agent._check_final_answer` 的数字溯源 `raise` 写在 try 内,被
`except Exception → logger.debug → return True` 吞掉——**现有 grounding 是死代码,恒放行**。
如果评测集复用它判幻觉率,会得出"幻觉率 0"的假象。所以:

- **第一步**:把 grounding 抽成独立模块 `execution/grounding.py`
  (`check_numbers_grounded(conclusion, corpus) -> {grounded: bool, details: [...]}`),
  运行时拦截(改后接回 `_check_final_answer`)与评测判定器**共用这一个实现**;
- corpus 取数同步修正(审计 A1b):`_truncate_observations` 已把历史 obs 截到 400 字符,
  溯源语料必须从 `executor.state`(`_r_*` 变量 + 非下划线模型变量)取全量,
  不能用 memory 里的截断残影。

### 0.2 抽样与分层

- **来源**:qd_traces 根节点(`utils/tracing._write_qd_traces` 写入),取近 90 天
  `status='success'` 的 run;`correct` 用 `chain/evaluator` 现成 T+N 通道回填。
- **分层先自动后人工**(两级规则 + 人工圈 L3):
  - L0:trace 内工具调用数 == 1(复用 `_record_tool_calls_to_trace` 的提取)
  - L1:单段(`phases` 为空,run 内 `<code>` 块 == 1——顺手就把 §7.12"单块遵守率"指标化了)
  - L2:有 phases 且 phase_replan_count == 0
  - L3:人工圈定(跨领域 / 多标的对比 / 长链研究 / 当前跑不好的),目标 15~20 条
- 50~100 条配比建议:L0 15 / L1 25 / L2 35 / L3 20。

### 0.3 金标准格式(每 case 一个 YAML)

放 `backend_api_python/tests/evals/cases/ev_xxx.yaml`:

```yaml
id: ev_001
source_trace_id: 1234          # 可回溯原始 run
level: L2
input: "对比中信证券和东方财富近20日主力资金流"
time_window: "近20个交易日"
gold:
  must_tools: [get_stock_fund_flow]        # 必经工具(防跳过取数瞎编结论)
  must_include:
    - {type: number, value: -12.3, tol_pct: 5, desc: "中信20日主力净流入(亿)"}
    - {type: regex, pattern: "龙虎榜|上榜", desc: "覆盖龙虎榜维度"}
  must_not: [{type: text, pattern: "建议重仓|稳赚"}]   # 合规红线
  missing_data_ok: true          # 允许(且期望)如实报数据缺口
checks:
  hallucination: grounding        # 复用 0.1 的 grounding 模块
  single_block: true              # <code> 块数 == 1
  clarify_used: false             # 本 case 无歧义,不应反问
judge:
  semantic: true                  # LLM judge(温度0,廉价模型)判语义覆盖
  rubric: "必须给出两家公司各自的净流入数值与对比结论"
```

- 数字判定容差分两档:行情类 `tol_pct: 5`(数据源刷新差异),计数类必须精确。
- **确定性判定优先**(数字/工具/正则/块数),LLM judge 只判语义覆盖——防止评测自己变成玄学。

### 0.4 runner 与指标

- `tests/evals/run_evals.py`:逐 case 构造 TaskAgent(独立 session_id 前缀 `eval_`),
  跑完读 response + grounding 判定 + trace 摘要,结果落
  `tests/evals/results/eval_<date>_<git-free-tag>.jsonl`(不依赖 git,用日期+prompt 版本号)。
- **指标面板**(每周全量,L0/L1 可抽 30 条省预算):
  | 指标 | 口径 | 对应改进条 |
  |---|---|---|
  | 分层通过率 | must_include+must_tools+judge 全过 | 全部 |
  | 幻觉率 | grounding 判定:结论数字在 corpus 命中率 <30% 的 case 占比 | 第 3 条 |
  | 单块遵守率 | `<code>` 块 == 1 的 run 占比 | §7.12 指标化 |
  | 规划合格率 | Plan Linter 首轮通过率 | 第 1 条 |
  | 一次成功率 | 无 replan/无 verify 回炉即通过 | 第 1、3 条 |
  | 平均步数/token/墙钟 | trace 现成 | 第 6 条成本面 |
- **曲线与门禁**:同一 case 集跑分差异用配对比较(逐 case 对比,不是只比总分);
  任何 prompt/模型/策略改动,PR 描述必须附"评测对比表"——没有这张表不评审(你定的原则:
  "对着曲线说话")。历史 `tests/test_reproducible_run.py` 的 token 复现测试可并入同 runner。

---

## 1. 规划层:Plan Linter + Best-of-N + 粒度自适应 —— 细化

### 1.1 Plan Linter(确定性 + LLM 混合)

**插桩点**:`task_agent._normalize_phases` / `_normalize_plan_tools` 之后、`_plan()` 返回前。
新模块 `app/agent/planning/plan_linter.py`(头部注释按项目约定写清"检查器自身失效的表现")。

**确定性三条规则**(全部登记表驱动,遵守"禁止硬编码领域名"红线):

1. **工具覆盖检查**。实现不搞关键词 if——复用预扫基建:`utils/prescan.py` 已提取工具签名,
   扩展建**倒排索引**(token → tool names;中文 2-gram 打分复用"清单裁剪用相关性"现成写法,
   tool 侧 token 取 docstring+Returns 段)。每 phase 的 acceptance 条目文本查索引,
   命中数据性名词(登记表 `planning/data_nouns_lexicon.py`:龙虎榜/资金流/筹码/财务…)但
   `tools` 与技能工具面零候选 → 缺陷 `tool_gap`。
   注意:命中"能力层"函数也算覆盖(capabilities 注册表要进同一索引),
   避免复刻"只查 tool_hub 不查 capabilities"的历史断链(§3.13 同款教训)。
2. **隐式依赖检查**。phase 契约加可选 `depends_on`(见 5.1);Linter 启发式:
   汇总型 phase(动词登记表:对比/合并/汇总/排名)缺 `depends_on` 覆盖前序分支 → `dep_gap`;
   后续 phase 的 acceptance 文本引用前序交付物名(`completed_phases_text` 同款命名约定)但
   中间隔着无 barrier 的 >2 个 phase → `barrier_gap` 警告。
3. **预算-规模匹配**。规模信号词典(登记表:`全市场/回测/多标的≥3/分钟级/600日窗口` → 预估步数下界)
   vs `sum(step_budget)`;离谱(如"全市场回测+多空研究"给 6 步)→ `budget_gap`。

**LLM critic**:`prompts/plan_critic.txt` 新文件;`_LLMAdapter` 现成,模型
`PLAN_CRITIC_MODEL` env(默认廉价档)。输出契约:

```json
{"passed": false, "break_point": "phase2 取数依赖 phase1 的股票池但没标 barrier",
 "defects": [{"phase_id": 2, "issue": "...", "severity": "high", "suggestion": "..."}]}
```

**回炉机制**:`_plan()` 加一次重试——缺陷清单注入 planner prompt(走现有
`replan_context` 同款拼接通道,**截断 ≤300 字符**);仍不合格 → 降级接受,
defects 落 trace(`plan_defects` 进 `qd_traces.ext_data`),执行期照跑但 verify 层加严。
**只回炉一次**(成本封顶,防 planner 空转)。

### 1.2 Best-of-N 规划选优

- `_plan(n_candidates=1)`:N 次独立调用(temperature 非零,走 `_LLMAdapter.generate(**kwargs)`
  透传)。**仅 L2/L3**(难度路由表驱动),N≤3。
- 评分**确定性为主**(免额外 LLM 调用):
  `score = w1*(工具面覆盖) + w2*(-step_budget总和) + w3*(依赖完整度) + w4*(规模匹配)`
  权重放 `planning/routing_policy.py` 登记表;同分才叫 critic LLM 裁决(便宜)。
- 全部候选与得分落 trace(`plan_candidates`),评测集后续可反向分析"选优是否选对"。

### 1.3 阶段粒度自适应

- 信号:Linter 在选中 plan 上做——某 phase `step_budget` 顶到 `MAX_STEPS_PER_PHASE`(12)
  且 acceptance 条目 ≥3 → `phase_too_coarse`,回炉提示拆分;相邻两 phase 各 ≤3 步、
  无 barrier、无跨引用 → `phase_too_fine`,提示合并。
- **只让 planner 重出 JSON,不做机械后处理**(合并/拆分会破坏 acceptance 语义绑定)。
- `PLAN_MAX_PHASES=5` 改软目标:plan prompt 写"阶段数随复杂度,通常 2~4,上限 8",
  Linter 校验 3~8 区间;env 改名 `PLAN_MAX_PHASES_CAP=8` 保留强制上限(兼容旧配置读法)。

---

## 2. 执行层:边跑边自检 + 结果可信 —— 细化

### 2.1 关键阶段双跑交叉验证

- **触发**:phase 契约加可选 `verify: "dual_run"`(planner/critic 标注"结论敏感":选股结果/
  回测指标/数值交付物);缺省 `none`,登记表推导。
- **实现**:`_run_phase_step` 对 dual_run 阶段并发跑两个 `_phase_run_call`(asyncio.gather,
  已是 async):B 跑用小模型(`DUAL_RUN_MODEL` env)+ 任务书加"独立实现,勿参考他人代码";
  变量隔离靠现有 staging scope(`run_scope+"_b"`);trace 记 `dual_run_diff`。
- **diff 判定**:数值抽取复用 `execution/grounding.py` 的数字抽取;逐 key 相对误差 >
  `VERIFY_TOLERANCE`(登记表,行情类 1%/计数类 0)→ 进 verify 节点核查;
  一致 → 直接采信 A 跑结果。
- **成本护栏**:仅敏感阶段 + 小模型;评测集记账双跑触发率(目标 <30% 阶段)。

### 2.2 代码块内建断言(数据自检前置层)

- **注入方式**:沙箱 helper `_qd_check_data(df, window=None, key_cols=[], bounds={})`
  走 `GuidedPythonExecutor.send_tools` 的 `additional_functions` 通道(已有 `_qd_peak` 先例,
  **不要进 ToolProvider**——它是 sandbox-only helper,登记表 `capabilities/admission.json`
  原则同样适用:helper 也该留痕一行)。
- 检查项:行数非零 / 日期覆盖任务窗口 / 关键列 NaN 率 / 涨跌幅量级 sanity(涨跌停幅 10%/20cm
  从工具域元数据取,**不硬编码**——这是项目红线)。
- helper 把结果写 `executor.state['_qd_stats']['data_checks']`(结构化),
  **确定性前置**:`_check_phase_acceptance` 收口前查——存在 failed check 而 final_answer
  无 `missing_data` 说明 → 判 fail(带"data 缺口未声明"缺陷),等于验收从"阶段末尾 LLM 判"
  前移到"数据落地瞬间判"。
- prompt 契约:code_agent.yaml **尾部追加**一条规则(不改旧编号,§7.17 教训):
  "取数后必须调 `_qd_check_data` 自检;失败则 final_answer 如实输出数据缺口,禁止硬算"。
- 缺失清单硬要求:`AgentResponse.missing_data` 已有字段;verify 节点(第 3 条)校验
  "结论含数值交付物而 missing_data 为空且 data_checks 有 failed → 幻觉风险"。

### 2.3 失败记忆进 run

- 现状已有半条通道:`_extract_failed_tools` + `_inject_tool_failures`(每步合成 observation
  注"xx 工具失败,不要重试")。缺的是**错误类型与修复方式**。扩展:
- `executor.state['_qd_stats']['error_memory']` 累积
  `{kind, snippet, resolved_as}`;信号源就是 `GuidedPythonExecutor._rewrite` / `_reraise` 的
  改写日志(unknown_tool → 已改名调用、attr_error、name_error、type_error、import、timeout;
  kind 走登记表不散 if)。
- 注入:`_wrap_stage_guard.before_run` 把摘要(≤200 字符)追加进当步可见的 memory 通道
  (与 `_inject_tool_failures` 同款合成 observation,单一注入通道避免多源重复):
  **只陈述事实**("get_daily 不存在,前两步已改用 get_stock_daily 成功"),不给方案猜测
  ——遵守"同名让位/不猜默认值"精神。
- 验收:评测集 L1 的"重复同错率"(同一 kind 三次以上未解决的 run 占比)前后对比,
  直接量化 §7.12 残留"反复改名重试"的消减。

### 2.4 数据质量契约

- 工具返回加 `_meta`: {source, updated_at, missing_fields}`——`tools/base.py::format_result`
  透传,薄封装从 DataSource 元数据取;缺失字段由工具层如实声明(契约装饰器化后自动抽取,
  过渡期手工加在关键取数工具)。
- `_inject_disclaimers` 旁平行加**时效提示**通道:updated_at 超 `DATA_STALE_THRESHOLD`
  (env,默认 1 个交易日)→ 注入"数据截至 X,可能已过时"。
- `missing_data` 硬要求接 2.2/3:finalize 前置检查一次(确定性,不加 LLM 调用)。

---

## 3. 质检层:verify 节点(独立审稿)— 细化

### 3.1 图改造(你判断对:改造量小)

- `graph.py`:新节点 `verify`,边 `execute → verify → finalize`;
  `route_after_execute` 的"阶段完成/单段完成"分支 → `"verify"`;新增 `route_after_verify`:
  `passed → finalize`;`failed & verify_retry==0 → execute`(带缺陷清单);
  `failed & retry==1 → finalize`(降置信度)。
- **状态增量**(`AgentState`):`verify_retry: int`、`verify_report: dict`(含
  `passed/defects/confidence`)。注意审计 C8:AgentState TypedDict 与实际 state 已漂移,
  这次改动顺手把 `_phase_replan_request/_trace/_start_time/_run_error/final_output`
  一并补声明(total=False 下无行为变化,纯契约修复)。
- verify 节点**跳过条件**(省钱):`hit_max_steps`/`_phase_abort`/无 final_output 时直接放行
  ——错误路径不做审稿(现 `_check_phase_acceptance` 已是 fail-open 于 IO,风格一致)。

### 3.2 critic 三查(同模型不同 prompt,`VERIFY_CRITIC_MODEL` 可选降级)

1. **数字溯源**(核心):结论中每个关键数字 × corpus(`executor.state` 全量,见 0.1)
   ——复用 `execution/grounding.py`,critic 只输出 `{quote, found: bool}` 逐数字清单;
2. **逻辑跳跃**:few-shot 列"资金流出→必跌"类无中间论据的断言;输出
   `{type: logic_jump, location, missing_link}`;
3. **方向对齐**:结论方向词(bullish/bearish)× 证据倾向。
   ⚠️ 依赖审计 B4:`utils/tracing._extract_direction` 不识别否定语境("不建议买入"→bullish),
   verify 复用前先修(否定前缀登记表),否则审稿人自己判错方向。

输出契约:
```json
{"passed": false, "confidence": 0.4,
 "defects": [{"type": "number_hallucination", "location": "第2段", "quote": "净流入12.3亿",
              "suggestion": "工具输出未见该数字,应删除或标注来源"}]}
```

### 3.3 回炉与降级

- 回炉注入:缺陷清单截断 ≤300 字符,走 replan_context 同款通道进 execute 的任务书
  (**不新增第二注入通道**——审计教训:多源注入=重放膨胀);verify_retry=1 封顶。
- 降级输出:`finalize` 把 `verify_report` 塞进 `fmt_context`;formatter 层:
  confidence < 阈值 → 回答头部加固定横幅"⚠️ 以下结论未经完整数据核对";
  finance formatter 已有 disclaimer 通道,接同一出口。
- **数字溯源脚注**(formatter 强制):结论句带 `[来源:工具名/字段]`;
  verify 抽检脚注指向的工具输出真能找到该数字——**评测集"幻觉率" = 脚注命中率**,
  运行时拦截、审稿、评测三方共用 grounding,单一事实源。

### 3.4 校验环自证机制(我的补充,见 §C)

verify/Linter/grounding 都新增"在工作吗"指标落 trace:grounding 拒收次数、verify defects
命中率、Linter 触发率,进周报面板。**防再犯 guided_executor 破坏性审批层"看起来有防护、
实际零作用"的老毛病**——审计 A1 就是校验环静默断链的现行犯。

---

## 4. 经验层:案例记忆 + 执行期知识检索 —— 细化

### 4.1 存储(独立表,复用 pgvector 基建)

```sql
CREATE TABLE qd_cases (
  id            BIGSERIAL PRIMARY KEY,
  run_id        BIGINT,                  -- 对应 qd_traces root id
  task          TEXT NOT NULL,
  task_type     TEXT, level TEXT,
  plan_digest   TEXT,                    -- phases/task 摘要(≤200字)
  tool_chain    TEXT[],                  -- 实际调用序列(_record_tool_calls_to_trace 现成数据)
  result_digest TEXT,                    -- 结果要点(≤80字)
  correct       SMALLINT,                -- T+N 回填(evaluator 现成)
  fault         TEXT,                    -- agent_fault/tool_data_fault/NULL
  emb           VECTOR(1024),            -- rag/embeddings.py 现成双 provider
  created_at    TIMESTAMP DEFAULT now()
);
CREATE INDEX ON qd_cases USING ivfflat (emb vector_cosine_ops);
```

- **写入**:finalize 收尾异步插(与 `_record_tool_calls_to_trace` 同段,
  worker 线程池执行,失败只 warning)。
- **correct 延迟**:T+N 前 `correct=NULL`;注入呈现"未验证"中性标注,不冒充经验。

### 4.2 注入(plan_node)

- 检索:top-3 cosine(`rag/pg_vector_store` 复用)+ 时间衰减(90 天窗,与 evaluator 对齐)。
- 注入格式(总预算 ≤300 字符,相关性 2-gram 裁剪——沿用既有裁剪原则):
  `[案例] 类似任务:…→ 工具链:…→ 结果:…(T+N 正确)` /
  `[反例] 此路不通:…(fault=agent_fault,注意:…)——**失败案例只给警告不给方案**。
- 防泄漏:案例 digest 由 finalize 生成(已有"记忆写入先出总结区"约定,同一出口)。

### 4.3 与技能酿造的衔接(补粒度空档)

- `chain/skill_brewer.maybe_brew` 加**通道④(观察信号,不直接酿)**:
  同 tool_chain 签名(n-gram)重复 ≥3 次且 correct=1 的案例簇 → "酿造候选"信号
  进 `get_brew_states` 决策;仍走既有酿造成熟门槛(channel 3 双条件)。
- 连带修复(否则案例层地基是歪的——审计 A2/A3):
  **A2**:brew_state 表 weight 列被 fail_streak/low_streak 一列两用互相覆盖 → 拆列/拆行;
  **A3**:`maybe_revise` 的 `r.get("revision", 0)` 归零 bug、`revise_skill` 的 since_id 算了不用
  (增量修订实际喂全量)。这两处不修,案例→酿造的信号会进一台互相投毒的状态机。

### 4.4 search_knowledge 执行期工具

- 新模块 `tools/knowledge_tools.py`(ToolProvider 自动发现,域 = `knowledge`);
  查:①历史分析结论库(qd_traces 历史 final_answer 向量化)②docs/研究知识(RAG corpus)。
- 返回带来源(可溯源,verify 层可核);进 `capabilities/admission.json` 准入评审留痕
  (名称 `search_knowledge`,只读)。
- 意义:RAG 从 chat_node 单点检索扩到执行段可控检索——执行中遇到行业口径/指标定义缺口
  不再只能搜公网。**注意工具面膨胀**:plan 提示注入按相关性裁剪现成,新增即入册。

---

## 5. 并行与分解 —— 细化

### 5.1 phases 依赖图化

- 契约:`_normalize_phases` 归一 `depends_on: [phase_id]`(缺省=线性前驱,**完全兼容现状**);
  同组 fan-out 用 `parallel_group: "compare"` 标记。
- 调度:`_run_phase_step` 现在是"批次化单阶段推进";改**就绪集合调度**:
  ready = 依赖全 completed;同一 ready 集 `asyncio.gather(_phase_run_call(...))`。
  ⚠️ 并发约束:`_LLMAdapter.generate` 是同步 OpenAI client(线程池适配)——并行分支占
  worker 线程,`_process_pool` 默认 4,需按 `max_parallel_phases`(env,默认 2)节流;
  staging scope 已 per-run 隔离;`qd_traces` 写入已有线程锁(审计确认安全)。
- barrier 收口天然兼容:`completed_phases_text` 是逐 phase 追加的,部分分支失败时汇总阶段
  自动看到缺失——但 plan prompt 要求汇总阶段 acceptance 明确"缺分支时如实声明"。
- 天然 fan-out:"对比 5 只券商股" = 5 个无依赖取数 phase + 1 个 depends_on 全部的汇总 phase,
  token/墙钟从 5x 降到 ~1x(汇总前)。

### 5.2 复合工具合成(远期,建议先做观察层)

- 识别跨 run 的稳定模式需要统计:先在案例表加 `tool_chain` n-gram 统计(与 4.3 复用,零新代码);
- 固化:重复 ≥3 次的序列 → 生成合成函数草稿 → critic 确认 → `_SkillFuncTool` 机制注入
  会话级/用户级复用;
- ⚠️ 契约风险:合成函数要带 Returns 契约与文件头注释(项目约定),且注册进 ToolProvider 前
  过 admission 留痕;否则又是"包装层价值"争议(§3.13 的可删性论证)。放远期是对的。

### 5.3 长任务异步化(不序列化现场,按 phase 边界重启)

- 入口:显式指令("后台跑")或墙钟检测(`AGENT_WALL_CLOCK_MS` 前 60s,`_phase_abort` 改
  `to_background` 标记)。
- **关键设计**:AgentState 含 `_code_agent/_phase_agents` 不可序列化(checkpointer 启用前置
  约束)——所以异步化**不是**把现场塞队列,而是把 `{task, completed_phases_text, phase_results,
  selected_skill, selected_domain, plan_tool_names}`(全部可序列化)作为**续跑凭据**入
  `message_queue` 队列,cron_worker 从下一个 phase 重新 plan-续跑
  (`completed_phases_text` 本来就是为续跑设计的摘要)。恢复 = 重启执行段,天然免疫序列化问题。
- 完成通知:现成 `flush_chat_message` 推送;trace 用同一 root 的续篇(`run_id` 关联)。

---

## 6. 难度路由 —— 细化

- **意图分类器扩维**:`prompts/intent_classifier.txt` 输出扩为
  `{route: chat|task, task_type, complexity: L0|L1|L2|L3, sensitive: bool}`
  (一次调用顺带判,零新增 LLM 调用)。⚠️ 现 `_ALL_TYPES` 是子串扫描,扩维后改
  `json_parser` 结构化解析(现成),保留子串回退(向后兼容)。
- **路由登记表**(`agents/routing_policy.py`,不散 if——项目红线):

| level | 模型档 | 执行形态 | best_of_n | verify | dual_run | step_budget |
|---|---|---|---|---|---|---|
| L0 | flash | 单段 | 1 | 仅 grounding | 无 | ≤2 |
| L1 | flash | 单段 | 1 | grounding+方向 | 无 | ≤5 |
| L2 | 标准 | phases 1~4 | 2 | 全 | 敏感阶段 | ≤12/段 |
| L3 | 强 | best-of-N=3 | 3 | 全+回炉 | 全敏感 | 按 Linter |

- 模型档映射:`MODEL_FLASH/MODEL_STANDARD/MODEL_STRONG` env,`llm/factory` 注册表现成;
  `_LLMAdapter` 按 run 选档(带 `tool_choice="required"` 兜底的 retry 机制照旧)。
- **阈值不拍脑袋**:L0/L1 降档前先用评测集量"小模型掉点幅度",达标(通过率差 <5pp)才启用
  ——这条依赖第 0 条,顺序不能倒。
- 成本记账:trace 记 `level+model+token`,周报出"单位任务成本"曲线——
  "提智不涨总成本"的验收凭据。

---

## 7. 落地顺序(细化版,含依赖)

| 阶段 | 内容 | 改动点 | 依赖 | 验收 |
|---|---|---|---|---|
| **先手** | ①修审计 A1+A1b 抽 `execution/grounding.py`;②评测集抽样/标注/runner/面板;③verify 节点(数字溯源 critic)+脚注 | grounding 模块、tests/evals/、graph+nodes 加 verify | 无 | 幻觉率可测、曲线首跑 |
| **先手(顺手)** | 修审计 A2/A3(brew 列冲突/revision 归零)、B4(否定语境)、A6(错误 run 无痕) | store/skill_brewer/tracing | 无 | 各带单测 |
| **二波** | Plan Linter(确定性先行→critic 后补)、失败记忆、`_qd_check_data` 断言 | planning/、guided_executor、code_agent.yaml(尾部追加) | 评测集 | 规划合格率、重复同错率 |
| **三波** | 案例记忆(qd_cases+注入+酿造信号④)、难度路由表 | SQL、plan_node、intent_classifier、routing_policy | 评测集(路由阈值) | L0/L1 成本降一个量级 |
| **远期** | DAG 并行、双跑验证、工具合成、异步化 | _run_phase_step 调度器、message_queue 续跑 | 二波稳定 | fan-out 任务墙钟 ~1x |

---

## C. 我的三点补充(与你的方案有出入的地方)

1. **校验环必须自证在工作**。你判断"短板是校验环缺失"我完全同意,但审计 A1 的教训更狠:
   **校验环本身也会静默断链**(数字溯源写了、日志在告警、实际恒放行)。所以每个新校验环
   (Linter/critic/grounding/verify)都必须带"在工作吗"指标(触发数/拒收数/命中率)进周报,
   没有流量的校验环 = 删掉的 guided_executor 审批层同款假防护。这是对全部 7 条的横切要求。
2. **双跑验证的 diff 判定别用 regex 抽数字**。两次独立代码的输出结构可能不同(一个输出表格
   一个输出列表),数值 diff 前需要交付物 schema 约定——建议 phase 契约加可选
   `deliverable_schema`(JSON schema 描述关键数值字段),双跑按 schema 提取后 diff。
   否则 diff 全是"格式不同"的假阳性。这比你说的"结果 diff 超阈值"多一步,但没有它双跑不可用。
3. **Best-of-N 的性价比要小心评估**。规划调用确实便宜,但 N=3 的 critic 打分若用 LLM 裁决
   同分场景,规划段成本 3~4 倍;而你自己的 token 实测(MEMORY:planner JSON 偶发损坏靠
   replan 自愈)表明 planner 的方差主要在**格式损坏**而非质量——先用评测集测
   "同 temperature 采 3 个 plan 的质量方差",方差小就只做 N=2 防损坏、不做选优,
   把省下的 critic 预算给 verify 节点(你说的"性价比最高"那条,我也这么排)。

---

*完。逐条可开工;较大改动(图改造、调度器、SQL)按约定先评审后动。*
