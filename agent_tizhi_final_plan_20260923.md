# Agent 提智 · 最优化合并方案(修正版)

> 来源:任务"结合双方分析修正为最优化方案" | 2026-09-23 | 署名:OpenClaw agent
> 复核:2026-09-24 | OpenClaw agent(对照 backend_api_python/app/agent 实码逐条核对;修正 6 处,见文末「七、代码复核修正记录」)
> 合并三源:①用户《agent提智.md》(7 条改进 + 展开设计 + 安全/闭环评审 + 三波远期拆解)
> ②本方《agent_improve_detail_20260923.md》(7 条细化 + 3 点补充)
> ③本方《agent_arch_review_20260923.md》(代码审计 P0~P2)
> 原则:冲突处以**代码实证**裁决;每项标注"采纳自谁/裁决理由"。生产代码零改动。

---

## 一、代码核对结果(你标注"请拿代码核对"的项 + 裁决依据)

### 1.1 护栏 5 不可达:结论成立,但机制比你描述的更严重 ⭐

你的判断(low_streak 到不了 2,护栏 5 稳态不可达)**成立**,但实际路径不同且叠加了两个
我此前审计出的 bug,状态机是三重损坏:

- **真实机制**(`skill_brewer.maybe_revise` 实读):low_streak **只在修订尝试失败时** +1
  (`r["status"] in ("llm_error","bad_format","no_delta")` 分支);**修订成功恒清零**
  (`revised` 分支 `low_streak=0`)。⇒ 护栏 5 的实际语义是"**连续两次修订尝试失败**→淘汰",
  而非文档的"**连续两轮修订仍差**→淘汰"——**"修订成功但性能依旧烂"的无限修订循环
  永不触发淘汰**。
  > 【2026-09-24 复核修正·大偏差】原文此处"(每次还把权重重置 1.0 放回高位,与你的判断一致)"
  > **与代码不符——该重置从未实现**。`revise_skill` 收尾只写 `set_brew_state(fail_streak=0)` +
  > `set_skill_revision(low_streak=0)`(都是 layer='brew_state' 行),全 chain/ **无任何**
  > layer='skill' 的 weight=1.0/sample_count=0 写入点;"护栏 2:权重重置(weight=1.0、
  > sample_count=0)"只活在 `skill_brewer.py::revise_skill` 的 docstring 与"护栏 2"注释里
  > ——"声明了但没接线"的又一例。真实稳态:修订成功后权重维持低位(90 日窗交易重算,无人重置),
  > 下轮 `update_weights` 末尾的 `maybe_revise` 再次入列 ⇒ **每个评估周期重复修订**
  > (比"重置 1.0 放回高位、隔几轮再衰减回来"更糟)。"无限修订循环永不触发淘汰"结论不变且更强。
- **叠加审计 A2**:`set_brew_state(fail_streak)` 与 `set_skill_revision(low_streak)`
  共写 `brew_state.weight` 一列 → **酿造失败计数会被 `get_skill_revision` 读成 low_streak**
  → 酿造连续失败两次会伪造"转淘汰"(护栏 5 反向误触发)。
- **叠加审计 A3**:`maybe_revise` 里 `_ssr(real_chain, revision=r.get("revision", 0), ...)`
  ——`revise_skill` 返回值无 "revision" 键 → **每轮把 revision 归零**。

**修复(四合一,缺一不可)**:①拆列:low_streak 独立列/独立行(禁一列两用);另注意
`last_updated` 也是共享列——`get_brew_states` 读它当 last_brew_date,`set_skill_revision`
写 today 会篡改酿造日期,拆行时一并拆;
②语义按你的设计改:"修订过之后权重仍低于阈值(带最小样本)"才 +1,"权重回升至阈值上
(带最小样本)"才清零;③删 maybe_revise 的二次 `_ssr` 写入(修 revision 归零);
④【2026-09-24 复核新增】护栏 2 真落地:修订成功后 skill weight=1.0、sample_count=0
(先重置,②才有"修订后重新观测"的对象;否则修订当轮权重仍低,下一轮立即满足"+1",
护栏 5 两轮就误触发),或删掉 docstring 里的该承诺——二选一,禁再留死声明。
**这组修复必须在"案例记忆→酿造信号④"之前完成**(我的前置依赖结论不变)。

### 1.2 DataRepairAgent:代码里不存在 ⚠️

你设计的失败路由"软失败 → DataRepairAgent 修一次"——**`app/agent` 内 grep `repair`
零命中**,没有任何修复代理组件。【2026-09-24 复核修正】原文"全仓 grep `repair` 零命中"
口径过宽:全仓仅 `app/routes/indicator.py` 有指标 IDE 的 `_repair_code_via_llm`
(另一子系统,与 agent 链路无关),结论不变。设计文档引用的这个组件属"文档承诺、代码未建"
(与审计 D6"§7.19 待补三条未落地"同族)。裁决:失败记忆/validate_df 的软失败路由
**改为两级**——第一步只做"记 missing_data + 降置信度"(零新组件,立刻可上);
第二步再新建轻量 `execution/repair.py`(单次修复代理,复用 `_LLMAdapter`),作为独立
工单项排二波末,不阻塞主链。

### 1.3 trading_tools:是执行链入口,不是查询面 ⚠️(P0-2 成立)

实读确认:`start_strategy` 的 docstring 明写"**策略将按照配置的指标信号自动执行买卖操作**",
启动 `app/services/trading_executor.py::TradingExecutor`,另有 `get_strategy_trades`
返回买卖记录。**无论底层实盘/模拟,agent 工具面暴露了"启动自动交易"且零确认闸、零风控闸**。
裁决:P0-2 全文成立,按你的三件套处理——①文档明写"本系统不接真实下单"或
②若有真实通道,硬风控(仓位/集中度/单日亏损/熔断/kill switch)独立于 LLM 确定性实现;
③无论哪种,`start_strategy/stop_strategy` 加 human-in-the-loop 确认参数 + 审计留痕。
**这条列为阶段 0(安全止血),先于一切提智改动。**

### 1.4 其余核对(确认你的判断)

- 【2026-09-24 复核修正·大偏差】失败记忆的采集点**方向**正确,但"现成错误分类采集点"
  名不副实:`_rewrite` 存在(`infra/guided_executor.py::GuidedCPythonExecutor._rewrite`),
  **`_reraise` 全仓不存在**(源头是 improve_detail §2.3 的幻觉引用);且 `_rewrite` 只改写
  NameError/AttributeError 两类(内部再细分"中文名/已知未点名/未知"),其余错误原样抛出
  ——"已能区分幻觉调用/沙箱不可用/表结构对不上"不成立,**wrong_column/wrong_frequency
  现无信号源**,error_type 词表落地时须同步新增这两个分类点,否则词表又成"声明了但没接线"。
  词表本身采纳(补一个 `data_repair_failed` 待 repair.py 上线后用)。
- `_check_final_answer` 数字溯源确实是死代码(raise 被 except 吞,审计 A1)——
  你的 `check_grounding` 独立设计恰好绕开它,**但那份死代码要顺手修/删**,
  否则运行时拦截与评测判定两个口径。
- cron 正则拦截脆(B7:秒/分钟级延迟会漂到明天)——你的"结构化工具为主、正则为快捷通道"
  + 滥用防线,与我的 B7 修复互补,合并采纳。

---

## 二、设计原则(合并后 7 条,分歧对齐用)

1. **校验环尽量确定性**——LLM critic 只兜确定性查不了的(逻辑/越界),数字核对/数据质量/
   工具覆盖全走规则(你的原则 1,我完全同意);
2. **回炉只许一次**——verify/linter/repair 全部单次,写死在路由里不靠提示约束(你的原则 2);
3. **缺口显式化优于硬凑**——missing_data 是一等交付物(你的原则 3);
4. **记忆必须带延迟标签回填**——案例/技能/合成工具,未验证降权,防错误自我复制(你的原则 4);
5. **每条改造先说清在评测集上看哪个数**——说不出来就不做(你的原则 5);
6. **禁止硬编码/登记表推导/单一事实源**——领域名、路由表、容差、词典全部登记表化
   (项目红线,两版方案共同遵守);
7. **校验环必须自证在工作**(我补充,横切全部):每个新校验环带触发数/拒收数/命中率
   计数器进周报——审计 A1 证明校验环自己也会静默断链,没有流量的校验环 = 删掉的
   审批层同款假防护。

---

## 三、方案主体(按波次的工作分解)

### 阶段 0:安全与闭环止血(小改动,先于一切提智)

| # | 事项 | 改动点 | 来源合并 |
|---|------|--------|----------|
| 0.1 | **交易确认闸**:start/stop_strategy 加 `confirm` 参数 + 审计留痕;文档明写实盘/模拟边界;若接真实通道→硬风控独立模块(kill switch 优先) | trading_tools.py + 文档"威胁模型"新章 | 你 P0-2 + 核对 1.3 |
| 0.2 | **direct_answer 禁数字门**:chat→finalize 路径确定性拦截——实体+数据意图强制走 task(regex 词表);direct_answer 禁出价格/涨跌幅/财务数字,被问即转 task | chat_node + intent 词表 | 你 P0-7 |
| 0.3 | **酿造状态机四修**(核对 1.1:拆列 + low_streak 语义 + revision 归零 + 护栏2 真落地) | chain/store.py + skill_brewer.py | 你 #4 + 审计 A2/A3 + 2026-09-24 复核 |
| 0.4 | **brew/revise 并发锁 + 版本历史**:PG advisory lock;.bak 改保留 N 版;修订版 M 天内样本分低于修订前 → 自动还原+告警 | skill_brewer.py | 你 #5 |
| 0.5 | **执行隔离第一步(低成本)**:危险调用(文件/网络/进程)审计留痕进 trace;`.env`/凭据路径 deny-list;完整隔离(子进程/容器/出网白名单)排远期 5.7 | guided_executor.py | 你 P0-1 + 审计 B3 |
| 0.6 | **记忆注入按不可信数据包裹**:memory 内容注入 prompt 加层级声明;跨会话只存摘要;记忆文本含指令式祈使句/工具名 → 降权丢弃 | nodes.py finalize/chat + memory 层 | 你 P0-3 |
| 0.7 | **并发槽位收编**(审计 B1):`_INTERRUPT_CHECKS/_current_event_cb/_active_code_agent` 进 per-session context,消除多 worker 互杀/事件串流 | message_queue + task_agent | 审计 B1 + 你 #11 |
| 0.8 | **可复现字段**:run 级 trace 补 prompt 哈希(plan_system/code_agent)、温度/seed、工具 schema 哈希、代码版本标识 | utils/tracing.py | 你 #8 |
| 0.9 成本硬预算+计数器面板 | ✅ | `utils/budget.py` 三线预算（token/工具调用/日配额，env 可覆盖）+ 超限软收尾；`AgentTraceRecorder._panel` 计数器 + `scripts/weekly_panel.py` 周报聚合 |
| 0.10 | 审计清扫包:grounding 死代码修/删(A1)、错误 run 落痕(A6)、方向否定语境(B4)、`wikipedia_search` 幻觉名(B5)、AGENT_MAX_STEPS 三默认值统一(B2) | 见审计报告 | 审计 P0/P1 |

### 先手(1~2 周):评测集 + claims + verify_node

**E1. 评测集(一切的验收地基)**

- 用例:你的 YAML schema 采纳(`gold_requirements/forbidden/budget/tags`,含
  **时间泄漏检测**与"资金流出→必跌"禁用表述——比我版本好,采纳);金标准从
  `correct=1` 的历史 run 反向提炼(你的来源设计,免从零标注)。
- 存储裁决:用例放 `backend_api_python/tests/evals/cases/`(我版路径,贴项目测试布局),
  schema 用你的;runner 产出 `qd_eval_runs/{date}/results.jsonl`(你的目录约定)。
- **三层判分器**采纳你的:L1 确定性(数字容差/必备字段/禁用表述/时间泄漏/allowed_tools/
  hit_max_steps)、L2 LLM judge(rubric 固定、温度 0、**judge 与被测模型不同源**——
  你这半句是关键,采纳)、L3 统计聚合。
- A/B variant 模式(`--variant plan-linter-on`)采纳——这是"对着曲线说话"的执行形态。
- 回归门禁:L0/L1 通过率降幅 ≤2pp(你);新增我的"无评测对比表不评审"入 PR 约定。
- 共用 grounding:评测的幻觉判定与 verify_node 同一 `check_grounding` 实现(我的单一
  事实源原则),corpus 取 `executor.state` 全量而非截断 obs(审计 A1b)。

**E2. claims 结构(生成侧强制)**

- 采纳你的 `claims[]`(text/value/provenance{tool,field,row}/verified_by_exec)——
  **优于我的脚注方案**(结构化可机检),我的"formatter 渲染脚注"并入为渲染层。
- prompt 硬要求(你草稿采纳):"无法定位来源的数字不得写入结论——宁可写进 missing_data"。
  改 code_agent.yaml **尾部追加**(不改旧编号,§7.17 教训)。
- `match_value` 格式变体白名单(千分位/百分号/亿·万缩放/±0.5pp 容差/复权同值不同号)——
  你的"误杀比漏杀更烦人"判断采纳;变体登记表化。

**E3. verify_node(三查 + 三级路由)**

- 图:`execute → verify → finalize | execute`(两版一致);**只对 L1+ 含数字结论启用,
  L0 不走**(你,省调用采纳);错误路径跳过审稿(我的跳过条件)。
- 三查:落地性(claims × corpus,确定性先行)、逻辑性(无中间论据/相关当因果)、
  **越界性(超出证据范围/给仓位指令)——你补的第三查比我版本全,采纳**。
- 三级路由 PASS / REPAIR_ONCE / DEGRADE 采纳(fatal|soft 分级比我版本细);
  REPAIR prompt 你的"**只修复列出的问题,未列出的内容一字不改**"是关键防回归约束,采纳;
  DEGRADE 输出"仅保留核对通过的 claims + 未通过移入存疑项 + confidence 降档 +
  trace 记 verify_failed"——比我的"加横幅"完整,采纳(横幅仍保留作渲染)。
- 回炉一次写死在路由(你的约束) + 我的:AgentState 补声明(verify_retry/verify_report +
  顺手补齐历史漂移字段)、方向对齐复用 `_extract_direction` 前先修否定语境(0.10 依赖)、
  verify 计数器自证(原则 7)。

### 二波(2~4 周):Plan Linter + 失败记忆 + 数据自检

**B1. Plan Linter(R1~R4 确定性 + LLM critic)**

- 你的 R1~R4 全采纳(R4 工具面裁剪是好条目,我版本没有);
  R1 实现合并:你的"data_domain → required_tools 词典"为主 + 我的 prescan 倒排索引兜底
  (词典覆盖不到的长尾用 2-gram 相关性索引找候选);**能力层函数进同一索引**
  (审计教训:防"只查 tool_hub 不查 capabilities"断链复发)。
- R2 隐式依赖:自动补 barrier + WARN 不阻塞(你),同时是并行化地基(两版共识);
  **不做机械合并/拆分 phase**,粒度信号(R3)只回炉让 planner 重出(我的约束,防
  acceptance 语义脱绑)。
- LLM plan critic:你的 prompt 草稿三问(哪步会断/验收无法判定/预算失配)+
  输出 `{fatal, warnings, score}` + 选优规则(fatal=0 取最高分,全 fatal 取最少带回炉;
  评分含"最小工具面、最小步数"防宏大计划)——全采纳。
- **Best-of-N 裁决**(冲突点):你直接 N=2~3,我主张先测方差。**合并:N=2 起步
  (顺带防 planner JSON 格式损坏——你们实测的主方差来源),评测集先量"同 temperature
  采 3 个 plan 的质量方差",方差大才升 N=3 并启用 critic 选优**;省下预算给 verify。

**B2. 失败记忆(run 内闭环)**

- 你的 `failure_memory` 字段 + error_type 定死词表(hallucinated_tool/sandbox_unavailable/
  wrong_column/wrong_frequency/empty_result/self_check_failed)采纳——同时是评测 L3 统计
  口径(两版共同目标:同一错误重复 ≤1 次/run)。
- 采集点合并:GuidedPythonExecutor `_rewrite/_reraise` 改写分支 append(你的判断+我的实证);
  DataRepairAgent 按 1.2 裁决改为 repair.py(排二波末)。
- 注入合并:你的预算(≤5 条、每条 ≤80 字)与"禁止令 + 已确认事实、不给方案"语义采纳;
  resolved=true 保留压缩一行(你,"换个工具名再犯"的洞察采纳);**单一注入通道原则不变,
  但注入口要新建**【2026-09-24 复核修正·大偏差】:`_inject_tool_failures` **全仓不存在**
  (improve_detail §2.3 的幻觉引用;nodes.py:366 注释自证旧 `_failed_tool` 标记是
  "无生产者的消费者"(审计 P1-4)——合成 observation 通道从未存在过)。现役只有消费侧
  `_extract_failed_tools`(读工具返回 error 键,供阶段验收 `_tool_data_evidence`),
  它不做每步注入。⇒ 先新建单一注入通道(如 step_callback 合成 observation / 
  `_wrap_stage_guard.before_run` 摘要注入),再谈"防多源重放膨胀"。

**B3. 数据自检(validate_df + SelfCheckError)**

- 你的 `validate_df` 异常式设计(失败抛 SelfCheckError(code, detail),执行器捕获路由)
  **优于我的 `_qd_check_data` 统计式**——采纳为主;统计面并入:捕获时同步写
  `_qd_stats.data_checks`(给 verify/评测/计数器用,零重复采集)。
- 软/硬失败路由采纳(软:记 missing_data+降置信度,阶段验收放行——"缺口显式声明比
  硬凑数字值钱";硬:换口径重算不许绕过);软失败的 repair 环按 1.2 降级为两级。
- 单块契约尾部追加【数据自检(硬要求)】(你的草稿,validate_df 同时是"复合工具合成"
  第一个种子——你的定位采纳)。
- 涨跌停幅等 sanity 参数从工具域元数据取,**不硬编码**(项目红线)。

**B4. 轻量修复代理 repair.py**(新组件,二波末):单次修复调用(复用 `_LLMAdapter`),
输入 SelfCheckError/失败上下文,输出修复动作或放弃;fail-open(异常/超时不阻断主链)
  【2026-09-24 复核修正·大偏差:原文"`_inject_disclaimers` 风格"——`_inject_disclaimers`
  全仓不存在(幻觉引用,源头 improve_detail §2.4);可参照的真实先例是 `_check_final_answer`
  的保守触发与阶段验收的"宁松勿卡"】。
上线后 error_type 补 `data_repair_failed`。

### 三波(4~6 周):案例记忆 + 难度路由 + search_knowledge

**T1. 案例记忆(CBR)**

- 你的案例 schema 采纳(case_id/task_summary/embedding/level/tags/plan_digest
  (purpose+tools+steps_used)/outcome 延迟标签/failure_modes/cost),含:
  **pending→correct/incorrect 回填、检索按"label != incorrect 加权、pending 降权、
  近重复 >0.95 只留最新已验证"**——延迟标签设计是灵魂,全采纳。
- 注入语义严格两类(成功给骨架候选+坑,失败只给坑、绝不附修复路径)——你的
  "防模型抄修复路径而不理解任务差异"洞察采纳;相似度低于阈值一条不注(宁缺毋滥)。
- **存储裁决**(冲突点):你 JSONL+向量索引,我 pgvector 表 → **裁决 pgvector 表**
  (`qd_cases`,DDL 用我的,字段用你的):rag/embeddings.py + pg_vector_store 现成,
  JSONL 还要自建向量检索;若想留离线可审计性,双写一份 JSONL 快照(成本可忽略)。
- 与酿造衔接:plan_digest 聚类 ≥3 次且 correct 率高 → 酿造候选信号④(两版一致);
  **前置依赖 = 阶段 0.3 状态机三修**(裁决 1.1)。
- search_knowledge 执行期工具:两版一致(查历史结论库 + docs 知识,带来源,
  admission.json 留痕,只读);冲突微处:我建议命名 `search_knowledge` 进
  `tools/knowledge_tools.py`(域=knowledge),无分歧。

**T2. 难度路由**

- **判定方式裁决**(冲突点):你"确定性特征先算、模型只判临界带 ±10%"**优于我的
  "intent 分类器 LLM 顺带判"**——采纳你的;我补:路由结果进 intent 分类器输出契约
  一次带回(`{route, task_type, difficulty, sensitive}`),零额外调用;规则得分与
  模型判定都落 trace 供复盘。
- 路由矩阵合并:你的矩阵(模型/管线/verify/linter/best-of-N/双跑)+ 我的登记表化
  (`agents/routing_policy.py`,禁散 if)+ 我的启用闸(**L0/L1 降档需评测集先证
  小模型掉点 <5pp,达标才启用**)。
- 你的升降级信号(升级:单段工具超阈值/跨域缺口/自报 need_replan → 丢弃单段从头
  plan,"单段本来就便宜沉没成本不心疼";降级 L2→L1 可缓做)——采纳;
  成本账与"升级率 <10%"指标采纳(路由质量自此可测)。

### 远期(6 周+):并行化 + 双跑 + 工具合成 + 异步化 + 完整隔离

**F1. phases 并行化**:你的执行模型(graph 骨架不动、并行在 execute_node 内、
ready-set asyncio)与我一致;三条硬约束(产物隔离/失败隔离/**只并行确定性阶段**——
解释类并行会口径漂移打架,你这条洞察采纳)+ 我的节流(`max_parallel_phases`,
同步 client 占 worker 线程);验收=compare 类墙钟降幅 + 结果一致性不劣化。
依赖:R2 depends_on 先行(共识)。

**F2. 双跑交叉验证**:你的触发条件(critic 判敏感 / claims 数字在容差边界)与仲裁表
(≤容差采信+confidence 升档 / 单点超差第三方仲裁或标 uncertain / **方向相反→两口径
并列+分歧原因,不硬选**)采纳;"金融数据口径会算出两个都'对'的数,把分歧摆出来比赌
一个强"——采纳为设计原则;我的 `deliverable_schema`(phase 契约可选 JSON schema)并入
——没有结构化交付物约定,diff 全是格式假阳性(我的补充原样保留)。

**F3. 复合工具合成**:你的触发(案例库聚类 ≥3 且 correct → critic 对历史输入重放验证
→ 固化 _SkillFuncTool,内置 validate_df、docstring 写清数据口径)+ 三条风控(登记进
linter 词典/合成后自动跑 tag 匹配评测/函数=可执行技能与 SKILL.md 关系理顺)——全采纳;
"把模型每次现写 30 行取数清洗变成调一个验证过的函数,省步数省 token 降出错面"——采纳。

**F4. 长任务异步化 — 一处硬修正**:你写"execute 超时前落 checkpoint(语义现成)"——
**不成立**:`AgentState._code_agent/_phase_agents` 不可序列化,checkpoint 启用是被它挡死的
(你自己在评审 #9 也点了这笔债)。裁决:采纳我的**续跑凭据重启**方案——超时/超预算前
落 `{task, completed_phases_text, phase_results, selected_skill/domain, plan_tool_names}`
(全可序列化)入 message_queue 队列,cron_worker 从下一 phase **重新 plan-续跑**
(completed_phases_text 本来就是续跑摘要);恢复=重启执行段,天然免疫序列化问题。
你的 plan 期预判(预估总步数×单步耗时 > 80% 墙钟 → 直接规划为异步)与前端话术
("已转后台,预计 N 分钟后回传")采纳。
配套(你的 #9):把 `_code_agent/_phase_agents` 挪出 state(进 NodeContext/side table,
state 留 run_id 引用)——这是纯重构,做在 F4 前,checkpointer 后续才有下文。

**F5. 完整执行隔离**(阶段 0.5 的终态):独立子进程/容器、非特权 UID、seccomp、
文件系统只读+tmp/ 配额、出网白名单代理;import 白名单从 `["*"]` 收回实际所需清单。
触发条件:对外开放多租户/接真实交易通道之前必须完成(你 P0-1)。

**F6. cron 结构化改造**(你的 #12 + 我的 B7):planner 以结构化输出 create_cron_job
走正常工具链,正则只留快捷通道;调度滥用防线(cron 表达式校验/最短间隔/单用户任务数
上限——"每分钟跑一次全市场回测"= 自打 DDoS,采纳);修 B7 时间语义(秒级延迟漂移到明天)。

### 常设:防回归与文档(与各波并行)

1. **契约自动对账 CI**:Returns: 段 dry 调用录制响应,断言真实返回键与文档逐字一致
   (你的,"契约漂移比没有更糟就别靠人记得回来改");
2. **死声明/死接线扫描**:env.example × os.getenv 比对 + "注册表条目零消费"扫描
   (抓 trace_collector 这类)——你的 pre-commit 方案 + 我的三条 AST 检查
   (提示词调用名×注册表差集、except 吞 return 常量、共享列双语义);
3. **golden tests**:`_normalize_phases` 三态、`_normalize_plan_tools` 并集、
   `resilient_parse` v5 伪标签/散落抢救——纯函数最便宜(你);
4. **record/replay E2E**:录真 LLM 响应做 fixture,fake model 跑 chat→finalize 全链
   (你;模型行为类问题归每周评测集,不进 CI 门禁——你的分层采纳);
5. **文档补章**:威胁模型/信任边界(用户输入/web_search/RAG/memory/工具返回五路不可信
   数据的标注与处置,你的 #6 文档建议)+ 多进程状态语义表(哪些进程内/哪些共享/失效场景,
   你 #11)+ 配置面收敛两层(env=运维可调 / constants.py=领域常量,你的附录建议 +
   RAG 阈值改名 `RAG_RERANK_ABS_THRESHOLD` 十分钟的事);
6. **MASK 范围确认**:覆盖 JSONL 与 log.py 输出,不止 DB(你的附录 M 提醒)。

---

## 四、冲突裁决汇总(一览)

| # | 冲突点 | 你的 | 我的 | 裁决 |
|---|--------|------|------|------|
| 1 | 溯源形态 | claims 结构化 | 脚注 | **claims**(可机检);脚注作渲染层 |
| 2 | verify 路由 | 三级 PASS/REPAIR_ONCE/DEGRADE + fatal/soft | 二级 + 降级横幅 | **你的三级**;横幅并入 DEGRADE 渲染 |
| 3 | 数据自检 | validate_df 抛 SelfCheckError | _qd_check_data 统计 | **你的异常式**;统计并入 _qd_stats |
| 4 | 难度判定 | 确定性特征 + 临界带模型判 | LLM 分类扩展 | **你的**;结果并入 intent 输出契约 |
| 5 | best-of-N | N=2~3 直接上 | 先测方差 | **N=2 起步**,方差大才 N=3+选优 |
| 6 | 案例存储 | JSONL+向量索引 | pgvector 表 | **pgvector 表**+可选 JSONL 快照 |
| 7 | 异步恢复 | 落 checkpoint("语义现成") | 续跑凭据重启 | **你的不可**(1.4/评审#9 自证);用我的重启方案 |
| 8 | 失败修复 | DataRepairAgent 修一次 | 无 | **它不存在**(1.2);两级降级 + 新建 repair.py |
| 9 | R1 工具覆盖 | 静态词典 | 倒排索引 | **词典为主 + 索引兜底**,能力层入索引 |
| 10 | 双跑 diff | 数值容差 | +deliverable_schema | **两者都要**,schema 是 diff 前提 |
| 11 | 落地顺序 | 先手=评测集+critic | 先手+审计小修 | **你的顺序** + 阶段 0 安全止血插队 |

---

## 五、验收指标总表(全部可测,基线待评测集首跑)

| 波次 | 指标 | 目标 |
|------|------|------|
| 阶段 0 | 危险调用审计覆盖率 / 交易闸拦截率 / 酿造护栏 5 触发可复现 | 100% / 演练可拦 / 单测覆盖 |
| 先手 | 幻觉数字率(claims 落地性失败占比) | 趋近 0 |
| 先手 | L0~L3 分层通过率曲线 / 时间泄漏检出 | 基线建立,周更 |
| 二波 | L2 一次成功率(不走回炉) | 明显抬升 |
| 二波 | hit_max_steps 率 / 同一错误重复次数 | 下降 / 每 run ≤1 |
| 二波 | 规划合格率(Linter 首轮) | 基线→70%+ |
| 三波 | 升级率(初始分级偏低) / 单位任务成本 | <10% / 不升反降 |
| 三波 | 案例注入命中率(相似度过阈值的比例) | 观测,校准阈值 |
| 远期 | compare 任务墙钟降幅 / cross_verified 覆盖率 | ~1x(单标的耗时) / 敏感阶段 100% |
| 常设 | 校验环流量(触发/拒收计数) | 全部非零可观测(原则 7) |

---

## 六、最终落地顺序(一张表)

| 阶段 | 内容 | 改动量 | 关键依赖 |
|------|------|--------|----------|
| **0 安全止血** | 0.1 交易闸、0.2 direct_answer 禁数字、0.3 酿造四修、0.4 并发锁、0.5 隔离第一步、0.6 记忆包裹、0.7 并发槽位、0.8 可复现字段、0.9 成本硬线、0.10 审计清扫包 | 小(多为数十行级) | 无,立即可做 |
| **先手** | E1 评测集(三层判分+A/B)、E2 claims、E3 verify_node | 小~中 | 0.10(grounding 修复) |
| **二波** | B1 Linter(R1-R4+critic)、B2 失败记忆、B3 validate_df、B4 repair.py | 中 | 先手(验收挂在评测集) |
| **三波** | T1 案例记忆+酿造信号④、T2 难度路由、search_knowledge | 中 | **0.3**(状态机修好才准接酿造) |
| **远期** | F1 并行、F2 双跑、F3 工具合成、F4 异步化(+state 瘦身重构)、F5 完整隔离、F6 cron 结构化 | 大 | B1-R2(depends_on)、评测集稳 |
| **常设** | CI 五件套 + 文档两章 + 配置收敛 | 增量 | 与各波并行 |

**共同结论**(两版合一):短板不是模型不够聪明,而是聪明的部分之间缺少互相校验
——规划没人挑错、结果没人核数、失败只救当步不记后步;且**校验环自身会静默断链**
(grounding 死代码、护栏 5 不可达都是现行犯)。所以最优方案的本质 =
**补校验环 + 给校验环装自证仪表 + 先建能回答"变好多少"的评测地基**。

---

## 七、代码复核修正记录(2026-09-24 | OpenClaw agent 复核,对照 backend_api_python/app/agent 实码)

| # | 位置 | 原文断言 | 实码 | 判定 |
|---|------|----------|------|------|
| R1 | §1.1 | "每次还把权重重置 1.0 放回高位" | 护栏 2 的 weight=1.0/sample_count=0 **无任何写入点**(revise_skill 收尾只写 brew_state 行);承诺只活在 docstring/注释 | **大偏差**,已改;修复清单增④ |
| R2 | §1.1 修复清单 | "三合一" | 另发现 `last_updated` 双语义(get_brew_states 读作 last_brew_date,set_skill_revision 写 today 覆盖) | 补入①,改"四合一" |
| R3 | §1.2 | "全仓 grep repair 零命中" | app/agent 内零命中 ✓;全仓有 `app/routes/indicator.py::_repair_code_via_llm`(指标 IDE,无关) | 中偏差,已改口径 |
| R4 | §1.4 | "`_rewrite/_reraise` 确实是现成错误分类采集点" | `_rewrite` ✓存在;**`_reraise` 全仓不存在**;且 _rewrite 仅 NameError/AttributeError 两类改写,wrong_column/wrong_frequency 无信号源 | **大偏差**,已改 |
| R5 | §二波 B2 | "通道走 `_inject_tool_failures` 现有单一注入口" | **全仓不存在**(nodes.py:366 自证旧 `_failed_tool` 是"无生产者的消费者",审计 P1-4);现役仅消费侧 `_extract_failed_tools`(读 error 键) | **大偏差**,已改(注入口需新建) |
| R6 | §二波 B4 | "`_inject_disclaimers` 风格 fail-open" | **全仓不存在**(幻觉引用) | **大偏差**,已改 |
| R7 | 阶段0 表 0.3 / 落地顺序表 | "酿造三修" | 随 R1 增为四修 | 已同步 |

> R4/R5/R6 三个幻觉符号名同源 `agent_improve_detail_20260923.md` §2.3/§2.4(该文档同口径问题待其维护者修正,本次未动)。

**复核通过(断言与实码一致,未动)**:
- §1.1:low_streak 只在 llm_error/bad_format/no_delta +1、revised 恒清零;护栏 5 = `REVISE_MAX_STREAK=2`;"revision 归零"(revise_skill 返回 dict 无 revision 键 → maybe_revise 二次 `_ssr` 写 0)——均与 `skill_brewer.py` 一致;
- §1.1 A2 weight 互踩 ✓(get_brew_states 读 weight=fail_streak / get_skill_revision 读 weight=low_streak,同为 layer='brew_state' 行,酿造连续失败 2 次即伪造"转淘汰");
- §1.3 trading_tools ✓(start_strategy docstring 原文一致,起 TradingExecutor(get_trading_executor → app/services/trading_executor.py),get_strategy_trades 存在,无 confirm/风控参数);
- §1.4 grounding 死代码 ✓(_check_final_answer 两处 raise 在 try 内,被 except Exception → logger.debug 吞掉,恒 return True);
- §1.4 B7 ✓(模式 9 分/时/秒三分支均压成 `HH:MM` 进 _parse_at_time,纯时间分支 target≤now 则 +1 天;另模式 10"提醒我"默认 1 分钟后同坑);
- 阶段0.8/0.10:utils/tracing.py ✓;A6 ✓(fail()→finish(status="error")被 `if final_answer and status=="success"` 挡住,零写 qd_traces,docstring 留痕承诺落空);B2 ✓(AGENT_MAX_STEPS 三默认 6(agent.py:47)/20(task_agent.py:1264)/5(nodes.py:901)均在);B4 ✓(_extract_direction 关键词命中、无否定语境);B5 ✓(code_agent.yaml:79 wikipedia_search 仍在);
- F4 ✓(_code_agent/_phase_agents 注释明标非序列化;completed_phases_text 即续跑摘要);E3 "历史漂移字段" ✓(AgentState 未声明 _trace/_start_time/_run_error/_phase_replan_request,使用点 nodes.py:512/1358/1380/1401);
- 常设 2 trace_collector 零引用 ✓(nodes.py:152 注释自证);常设 3 golden tests 三对象 ✓(_normalize_phases:275 / _normalize_plan_tools:372 / resilient_parse v5 伪标签 + `_rescue_loose_code`)。

---

*完。阶段 0 + 先手共 13 个工单项,均在"数十行~数百行"量级,可直接开工;
按约定较大改动(图改造/调度器/隔离)先评审后动。*

---

## 八、评审补丁与底座影响核验（2026-09-24 并入 | OpenClaw agent）

> 来源两稿：`workspace/agent提智方案评审_2026-09-24.md`（补丁 C1~C12）、`workspace/提智方案_四闭环三层追责影响_2026-09-24.md`（底座核验 + 护栏 P1~P3）。
> 性质：评审补丁，**不改变本文方案骨架**；下列最高优先项须在对应波次开工前处置。

### 8.0 最高优先（开工前必须处置）

| 编号 | 事项 | 落点 | 为什么最高优先 |
|---|---|---|---|
| P1 | **给 `correct` 加写保护**：golden test 断言 `qd_traces.correct` 只由 `update_verify_results`/`update_skill_verify`（行情回测）写；verify_node / claims 结论写独立字段 | tests + E2/E3 实现约束 | 一举保护 ①②④闭环 + 三层追责；不修则 E2/E3 污染②的客观依据，多米诺到酿造候选与评测金标准 |
| P2 | **0.3 schema 变更走 additive**：新列/新行，不动 `skill/factor/tool` 层；迁移带快照回滚；验证 `layer IN ('skill','factor','tool')` 行零变化 | 0.3 | 保护②的权重存储不被 brew_state 拆列误伤 |
| P3 | **明确 TODO-1 去留**：补 chain 层权重则并入 0.3 同批（同表同迁移）；不补则白纸黑字记「已知局限 G1」 | 三层追责功能 | 避免追责三层「只记账不加权」缺口被默认继承 |
| C1 | **F4 前置：阶段产物可文本化审计** | F4 前 | 续跑重启隐含「已完成 phase 产物可文本化」，实际只有文本摘要现成、数值变量不现成 |
| C2 | **E2 corpus 口径只认工具返回值槽位** | E2/E1/E3 | 取 `executor.state` 全量会让模型自造变量自我放过幻觉 → 验收失效 |
| C3 | **先建 CI 骨架**（pytest tests/ + compileall，PR 触发） | 常设之前 | 仓库当前无 CI（无 `.github/`），常设多条依赖它 |
| C4 | **名单一致不变量收进 0.10**：`allowed_names` 收敛为沙箱选定面 + `_ListToolsTool.forward` 去 `domain="all"` 回退 | 0.10 | §7.0 三处口径打架（planning ~60 / 沙箱 ~15 / list_tools 58） |

### 8.1 四闭环 + 三层追责：影响核验

**结论**：骨架不受影响、大多为增强；但有 5 个耦合点若不设护栏会连带损坏底座。

**实码锚点（可复查）**：
- ① 错误 run 不落库：`utils/tracing.py:376`（`if final_answer and status=="success"`）、`:381`（`fail()`→`finish(status="error")`）。
- ② correct 写入：`chain/store.py:367`（`update_verify_results`）、`:382-385`（SET correct）；权重计算 `chain/evaluator.py:335`（`update_weights`，只算 skill/factor/tool）。
- ③ 反馈保护（**2026-09-19 已修**）：`chain/store.py:344`（`AND NOT COALESCE(human_reviewed,FALSE)`）、`:533`（`mark_root_wrong` 写 human_reviewed=TRUE）、`:555`（`mark_root_good`）。
- 0.3 互踩：`chain/store.py:790/793`（`set_brew_state`）与 `:1113/1116-1117`（`set_skill_revision`）写**同一 PK 行** `(layer='brew_state', name=chain)`；读侧 `:771-772`、`:1097`。
- ④ brew gate 读 ② correct：`query_brew_candidates`（见 `tmp/AGENT_ORCHESTRATION_REDESIGN.md` §4.4）。
- 三层枚举：`chain/schema.py`（Layer.CHAIN/SKILL/TOOL）。

| 底座 | 方案是否影响 | 判定 | 必须守的不变量 |
|---|---|---|---|
| ① 记录闭环 | 0.8 / 0.10-A6 直接改 | **增强**（且方案依赖它） | 0.8/A6 先行，否则 E1/E3/T1 无地基 |
| ② T+N 回测 | E2/E3 可能回写 correct | ⚠️ **有真实风险（多米诺）** | verify/claims 禁写 correct/calibration |
| ② 存储（同表） | 0.3 改同一张表 | 有限（权重行不同） | 0.3 走 additive + 权重层零变化断言 |
| ③ 用户反馈 | 无涉（09-19 已修） | **安全** | 新写 correct 路径须尊重 human_reviewed |
| ④ 编排闭环 | 0.3 修护栏 5（间接加固） | **增强** | 0.3 是 T1 前置（已列） |
| 三层追责·结构 | 不碰 | **完好** | — |
| 三层追责·功能 | G1 未纳入 | **缺口照旧** | 想补强须拉 TODO-1 进来 |

**关键澄清（防误导）**：`tmp/AGENT_ORCHESTRATION_REDESIGN.md` §10.1 仍把审计 C2「人工反馈被自动验证覆盖」列为待修——那是**历史快照**；实码确认 09-19 已闭环。实现时勿重复修。

### 8.2 评审补丁 C5~C12

| 编号 | 补丁 | 要点 |
|---|---|---|
| C5 | 0.5 隔离第一步措辞降级 | 明标「日志留痕 only，不承诺拦截」，避免制造假防护（真拦截留 F5）。`GuidedCPythonExecutor` 是真 exec，deny-list 绕过面大 |
| C6 | 评测集 gold 分层 | 金标准只从 `correct=1` 池提炼有幸存者偏差（只覆盖带买卖结论任务）；按 task_type 分层，分析/查询类另设来源并声明 |
| C7 | B1 R1 词典维护闭环 | 死声明扫描抓不到「词典条目陈旧」；加 CI 断言「词典工具名 ⊆ provider 注册表」或条目带 last_verified；注明能力层启用条件（`CAPABILITIES_ENABLED` 默认关，62 函数进不了沙箱则验证不了）<br>**2026-09-24 下午 ✅ 已落地**：`test_wiring.py::test_plan_linter_dict_and_dep_tables_are_registered` 覆盖「词典 + 依赖表 ⊆ 真实注册表（含能力层，走 `register_capabilities`）」，随既有 `wiring-contract-tests` CI job 执行；另在 `plan_linter.lint_plan` 内留**运行时孪生**信号：首选工具不在注册表 → 告警 `tool_not_registered` 并拒绝补位（条目陈旧不再静默） |
| C8 | T2 临界带 ±10% 需校准 | 带宽在评测集首跑前是拍脑袋；纳入 E1 校准项，由数据定带子 |
| C9 | 0.9 优雅收尾与收尾护栏打通 | 单段中途耗尽无「阶段产物」；收尾护栏 `_enforce_final_answer` 判据（相似度≥0.9）09-20 实测兜不住，09-21 已移除逃生阀；须与 E3「连续 N 次被拒」判据一并落地 |
| C10 | 死代码 `_check_final_answer` 修/删形态 | 它是 `final_answer_checks` 唯一装配项（task_agent.py:2318）；删=无钩子，修活=装回 09-21 刚删的逃生阀 → 须明确「由 check_grounding 接管」的具体形态 |
| C11 | 死接线清理清单 | `returns_sampler.py`（09-21 已退役写侧/读侧单源，文件保留仅离线体检）、`trace_collector.py`（零引用）、两份 `AGENT_DESIGN.md` 并存（`D:\QuantDinger\AGENT_DESIGN.md` 旧版 vs `docs/AGENT_DESIGN.md` 活跃版） |
| C12 | 评测集首跑基线写成硬门禁 | 未建基线不得进入二波（方案隐含未明写） |

### 8.3 实施顺序增补（把 C*/P* 挂到波次）

- **阶段 0 增补**：C4（名单一致不变量→0.10）、P2（0.3 additive 约束）、C5（0.5 措辞降级）、P3（TODO-1 去留决策）。
- **先手前置**：C3（CI 骨架）+ C1（F4 前置审计，虽挂 F4 但须早排）+ C2（E2 corpus 口径，写进 E2 验收）+ P1（correct 写保护）。
- **二波入口门禁**：C12（评测集首跑基线）+ C6（gold 分层）。
- **三波入口**：C7（词典闭环）+ C8（临界带校准）。
- **远期**：C9（0.9 与收尾护栏打通，排 F4 同批）、C10（check_grounding 接管形态）。
- **常设**：C11（死接线清理并入第 2 项扫描）。

## 九、落地进度（2026-09-24 更新，本文件为活文档；E2/E3/0.9 同日完成）

> 标记：✅ 已落地并验证 · 🟡 部分/已由前向快照落地 · ⬜ 未开始
> 验证口径：`pytest tests/test_wiring.py` = 67 passed；`py_compile` 全绿；评测集 `--selftest` OK。
>
> **2026-09-24 下午 · 代码取证复核（接力 agent，仅增量修正，不改他人结论）**：本表此前滞后于实码——
> ① 0.9 标 ⬜ 与正文 line 116 的 ✅ 自相矛盾，**实码为 ✅**（已就地更正）；
> ② B2/B3/B4 三项已落地却未入表（此前表内无二波段），现补记；
> ③ 验证口径中"38 passed"为旧数，实测 67 passed（本次追加 B1/B4 两段共 16 项后）。
> 复核方式：对 `backend_api_python/app/agent/**` 逐项取证（grep 符号 + 读实现 + 跑测试），
> 不以文档自述为准。二波最大缺口是 **B1 前半段（LLM critic / Best-of-N）** 尚未开工。

### 阶段 0（安全止血）
| 项 | 状态 | 备注 |
|----|------|------|
| 0.1 交易确认闸 | 🟡 | `trading_tools.py::requires_confirmation` 已在位；human-in-the-loop 确认+审计留痕待补 |
| 0.2 direct_answer 禁数字门 | ✅ | `nodes.py` `_DATA_INTENT_RE` + `direct_answer_numbers_detected` 事件 |
| 0.3 酿造四修 | ✅ | 拆列（`set_skill_low_streak`/`reset_skill_weight`）、护栏2真落地、护栏5语义修正、删二次写 |
| 0.4 brew/revise 并发锁 | ✅ | `chain/store.py::acquire_run_lock` + `_brew_locked` |
| 0.5 执行隔离第一步 | 🟡 | 危险调用审计留痕（`_danger_log`/`_guarded_import`）在位；**这是留痕，不是真隔离** |
| 0.6 记忆按不可信数据包裹 | ✅ | `nodes.py::_sanitize_memory_text` |
| 0.7 并发槽位收编 | ✅ | `bind_run_session` / session 绑定 |
| 0.8 可复现字段 | ✅ | `tracing.py::_repro_meta` |
| 0.9 成本硬预算+计数器面板 | ✅ | **2026-09-24 下午更正**（原标 ⬜ 为滞后记载）：`utils/budget.py` 三线预算（run token/工具调用/日配额，全 env 可覆盖）+ `budget_wrapup_hint` 软收尾 + `PANEL_KEYS`/`panel_from_trace`/`render_panel` 校验环计数器；`scripts/weekly_panel.py` 周报聚合；`task_agent._budget_step` 每步现算并注入（不 raise） |
| 0.10 审计清扫包 | ✅ | A1 grounding 复活、A6 tracing 错误留痕、B2 死代码、B4/B5 |

### 先手（1~2 周）
| 项 | 状态 | 备注 |
|----|------|------|
| E1 评测集 | ✅ | `tests/evals/` runner（L1/L2/L3、A/B、基线门禁、`--selftest`）+ 3 种子用例 + `_schema.md` |
| E2 claims 结构 | ✅ | `utils/grounding.py` 变体归一（extract_numbers/numbers_match/value_in_corpus/check_claims）；code_agent.yaml 尾部规则 18 CLAIMS 硬要求 |
| E3 verify_node | ✅ | `nodes.py::make_verify_node` 三查（落地/越界/逻辑）+ 三级路由 PASS/REPAIR_ONCE/DEGRADE；`route_after_verify` 回炉一次写死；verify_feedback 注入下一轮 execute；`_should_verify` 仅含数字启用 |

### 二波（2~4 周）—— 2026-09-24 下午补记（此前表内无本波段）
| 项 | 状态 | 备注 |
|----|------|------|
| B1-A 确定性（R1 工具覆盖 / R2 隐式依赖） | ✅ | `utils/plan_linter.py`：R1 `_DATA_DOMAINS` 词典（域→关键词+候选，首选工具补位）+ 2-gram 倒排索引兜底（低置信只告警）；R2 产消依赖自动补 `barrier`（WARN 不阻塞）。`task_agent._plan` 接线（lint→apply→`trace.record("plan_lint")`），失败 `logger.warning` 发声不静默。CI 门禁：`test_wiring.py` 10 项（含 C7「词典/依赖表 ⊆ provider 注册表」） |
| B1-B LLM plan critic + Best-of-N(N=2) | ⬜ | 未开工。**前置：评测集首跑基线**（C12）——无基线量不出"同 temperature 采 3 个 plan 的质量方差"，选优规则无从校准 |
| B2 失败记忆（run 内闭环） | ✅ | `utils/failure_memory.py`（error_type 定死词表 + 单次注入 + `drain_executor_events`）；采集点 `infra/guided_executor.py::_failure_events`；注入通道 `task_agent._failure_memory_step`（step_callback 单一通道，追加 observation 不改模型代码） |
| B3 数据自检（validate_df + SelfCheckError） | ✅ | `utils/data_check.py`（`validate_df`/`SelfCheckError`/`drain_checks`/`CHECK_CODES`）+ 沙箱注入 `data_check` 别名（README 式，见 guided_executor L365）+ `prompts/code_agent.yaml` 规则 19；软/硬失败两级路由 |
| B4 轻量修复代理 repair.py | ✅ | `execution/repair.py`（只修 `hallucinated_tool/wrong_column/wrong_frequency`、`REPAIR_MAX_ONCE=1`、fail-open、env `AGENT_CODE_REPAIR` 默认关）；**2026-09-24 下午补接线**：`task_agent._repair_step` 进 step_callbacks，且先于 `_failure_memory_step`（先 peek 失败事件、再由 B2 drain 清空） |

> B1-A 与 B2/B3/B4 的关系：B4 复用 B2 的 error_type 词表与 `_failure_events` 采集；三者共用
> step_callback 通道，故顺序不可乱（`_repair_step` → `_failure_memory_step`，测试锁此不变量）。

### 评审前置（8.0 最高优先）
| 项 | 状态 | 备注 |
|----|------|------|
| P1 correct 写保护 | ✅ | `test_qd_traces_correct_only_written_by_store`（静态白名单 `chain/store.py`） |
| P2 0.3 schema additive | ✅ | 0.3 落于独立列/独立行，权重层行零污染 |
| P3 chain 层权重去留 | ⬜ | G1（`update_weights` 不产 chain 权重行）仍待单独拉 TODO-1 |
| C1 F4 前提前置 | ✅（审计） | 已核：仅 `phase_results`/`completed_phases_text` 可序列化，数值变量不可续跑 |
| C2 E2 corpus 口径 | ✅ | `grounding.py` 只认来源槽位（排除自造变量） |
| C3 CI 骨架 | ✅ | `.github/workflows/basic-ci.yml` 新增 `wiring-contract-tests` job |
| C4 名单一致不变量 | 🟡 | `_ListToolsTool` 单源视图已落地；`allowed_names` 收敛核验通过 |

### 下一步（建议顺序）

> 下列 1~5 为 2026-09-24 上午的原始清单，**其中 1/2/3/5 现已完成**（见上方各表），保留原文以便对照。

1. **E2 claims**（先手主体）：code_agent.yaml 尾部追加 claim 契约 + `match_value` 变体白名单登记表化；
   配套 `utils/grounding.py` 扩展变体归一（单源，勿在调用方补丁）。
2. **E3 verify_node**：新增 verify 节点（只对 L1+ 含数字结论启用，三级路由 REPAIR_ONCE 写死一次）。
3. **0.9 成本预算**：三线预算 + 校验环计数器（门触发数/拒收数/命中率）进周报。
4. **P3 / TODO-1**：补 chain 层权重 + planner 消费，与 0.3 同表同批迁移。
5. 二波（Plan Linter / 失败记忆 / validate_df / repair.py）——依赖先手。

#### 2026-09-24 下午 · 接力复核后的剩余清单（按建议顺序）
1. **评测集首跑基线（C12，硬门禁）**：`tests/evals/` 有 runner 与 3 个种子用例，但**无基线文件**——
   而 C12 要求"未建基线不得进入二波"。B2/B3/B4 已在无基线状态下推进（既成事实），
   B1-B（critic / Best-of-N）**不宜再跳过它**：基线是选优规则唯一的客观依据。先补基线，再开工 B1-B。
2. **B1-B（critic + Best-of-N N=2）**：依赖第 1 项。开工前读 §三 二波段的三问 prompt 与选优规则
   （fatal=0 取最高分 / 全 fatal 取最少带回炉；评分含"最小工具面、最小步数"）。
3. **P3 / TODO-1**：`update_weights` 仍不产 chain 层权重行（已知局限 G1），需单独决策补或白纸黑字记局限。
4. **C4 收尾**：`allowed_names` 收敛为"沙箱选定面" + `_ListToolsTool.forward` 去 `domain="all"` 回退（§7.0 三处口径打架）。
5. **文档滞后项（§7 已记录）**：§三 正文对 B1 的描述仍写"你的 R1~R4 全采纳"，
   其中 **R3（粒度信号只回炉、不打散 phase）/ R4（工具面裁剪）尚未实现**——同属 B1-B 范畴。

#### 2026-09-24 傍晚 · E1 取数通道修复（接力 agent，仅增量追加，不改他人结论）

**开工前发现（基线前置阻断项）**：`tests/evals/runner.py --live` 的判分输入是**断链**的——
`judge_l1(c, run["output"], used_tools=...)` **从不传 `route`/`corpus`**：
① `expect_route` 对全部 3 条用例恒判失败（`"" == "task"`）；
② `min_grounding_rate` 因 `and corpus` 恒假而**一次都没被评估**（声明了没接线的判分项）；
③ `used_tools` 靠 stdout 正则 `^\s*toolname\(` 回收，而 CLI 只打印会话头 + 最终 answer，
   工具名只出现在计划正文 `- get_xxx() — ...`（行首是 `- `，正则不命中）⇒ 三条用例必然 0/3。
按现状跑出的"基线"与 agent 质量无关，且会让 C12 门禁（基线→70%）失去意义。

**修复（不在 runner 里猜，改走单一事实源出口）**：
- `utils/tracing.py`：新增评测侧车出口 `AGENT_EVAL_DUMP=<path>`（默认不设=关闭）——
  `AgentTraceRecorder.finish()` 追加一行 JSON，含 `route`（意图路由，与 `route_after_chat`
  的 needs_task 同义）/`intent_verb`/`used_tools`（去重保序）/`corpus`（工具观测语料拼接）。
  route 与工具层数据本就收在采集器内存里（`intent_verb`/`_tool_calls`），此前只落 qd_traces 库、
  JSONL 不含 ⇒ 属"采了但没有出口"，本次只补出口，不改 JSONL/qd_traces 语义。
- `tests/evals/runner.py`：`--live` 传唯一 `--session` + 侧车路径，按 session_id 回收三件套；
  **侧车缺失 ⇒ `error=dump_missing` 并计入 counters（不猜、不 stdout 兜底）**；工具名与注册表
  求交过滤（代码里 `x = foo(` 形态的非工具标识符也会进 `_tool_calls`）；
  同时补上**负向交付判分** `must_not_have`（用例早已声明，判分器此前只读 `must_have`）。
- 验证（离线、零 LLM）：`tmp/qclaw/verify_evalsidecar_0924.py` **20/20 PASS**（route 归一 /
  侧车字段契约 / 未设 env 零写入 / runner 回收 / 四个判分项"真的会失败"）；`--selftest` OK；`compileall` OK。
- 产物：`tmp/qclaw/eval_dump_baseline_20260924.jsonl`（侧车）、`tmp/qclaw/report_baseline_20260924.json`（基线报告）、
  `tmp/qclaw/baseline_run_20260924.log`（运行日志）。

**环境注记**：本机 `.env` 无 `DATABASE_URL` ⇒ `_write_qd_traces` 报错（"跳过写入决策树"），
qd_traces 落库与回测统计在本环境不可用；侧车走文件通道，不受影响。

#### 2026-09-24 夜 · 基线首跑 0/3 拆解与二次修复（接力 agent，仅增量追加）

**首跑基线事实**（`report_baseline_20260924.json`，3 用例，约 46 分钟）：`pass_rate 0.0`；
`route` 3/3、`deliverable` 3/3、`must_not_have` 3/3、`banned_phrases` 3/3、`dump_missing=0` 全过，
三项失败中 **两项是假失败**、一项是真问题：
1. `tools_required` 0/3（missing=4）——**假失败**。侧车 `used_tools` 三条均为 `["python_interpreter"]`：
   ① smolagents CodeAgent 的 `step.tool_calls` **恒为代码执行伪工具**（`python_interpreter`），
   旧采集器把它当真实工具名，且 `if not tool_name and code_action` 被它**短路** ⇒ `code_action`
   提取分支**永不执行**；② 即便进入分支，旧实现用 `re.search` **只取首个**候选。
2. `no_time_leak` 0/3（leaks=4）——**口径问题**。`as_of`（09-20/09-21）vs 实跑运行日（09-24），
   报告里"分析日期 2026-09-24"被整片误判；屏幕用例另含 `2026-09-25`（明日交易日标注）。
3. `grounding` 0/3（0.6316 / 0.5447 / 0.3044 vs 门 0.7/0.6/0.6）——**真问题**，见下"数据层"。

**二次修复（两项，均已离线验证）**：
- `nodes.py`：新增 `_PSEUDO_CODE_TOOLS` / `_PY_BUILTIN_CALLS` / `_tool_names_from_code_action`；
  采集器改为"结构化名是伪工具 ⇒ 剔除后走 `code_action` **全量**提取（`re.finditer`，用**未截断**原文，
  落盘仍截断 500）"，并**与沙箱工具面求交**（`agent.tools`，代码里自定义函数/pandas 方法不入工具层）；
  一步多工具时逐个 `add_tool_call`（观测/推理链只挂首条，避免重复）。工具层节点自此=真实工具调用。
- `runner.py`：`check_time_leak(text, anchor)` 锚点口径改为 **max(as_of, 运行日)**（`_live_time_anchor`），
  并加**前瞻标签例外**（日期紧邻「明日/次日/下个交易日/隔日」等不计泄漏）；每用例原始输出落盘
  `<out_stem>_outputs/<id>.txt` 便于复核。`_schema.md` 同步该口径（增量追加来源标注）。
- 验证：`verify_evalsidecar_0924.py` 扩到 **29/29 PASS**（新增 E1–E9：多工具全收录 / 内置排除 /
  沙箱求交 / 空输入 / 前瞻标签不判泄漏 / 无标签未来日期仍判泄漏 / 锚点三态）；`test_wiring.py` **67 passed**；
  `compileall` OK；`--selftest` OK。

**数据层真实发现（与判分无关，独立立项）**：
- `600176` K线 `count=1`（预期 30）⇒ 多周期涨跌幅/量比/MA 全面退化；300497 主力净流入 `None`、
  筹码/MA 全 `N/A`；屏幕用例 `上涨0家 下跌0家`（涨跌家数缺失）、`综合评分 0/100` ⇒ "无符合条件股票"。
- B3 数据自检**生效**：报告主动写"⚠️ 数据降级警告"并下调评级（说明自检链路通，问题在取数源）。

**产物**：`tmp/qclaw/report_baseline_20260924b.json`（重跑）、`eval_dump_baseline_20260924b.jsonl`、
`baseline_run_20260924b.log`、`report_baseline_20260924b_outputs/*.txt`。
