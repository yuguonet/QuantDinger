# Agent 提智 · 最优化合并方案(修正版)

> 来源:任务"结合双方分析修正为最优化方案" | 2026-09-23 | 署名:OpenClaw agent
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
  永不触发淘汰**,每次还把权重重置 1.0 放回高位(与你的判断一致)。
- **叠加审计 A2**:`set_brew_state(fail_streak)` 与 `set_skill_revision(low_streak)`
  共写 `brew_state.weight` 一列 → **酿造失败计数会被 `get_skill_revision` 读成 low_streak**
  → 酿造连续失败两次会伪造"转淘汰"(护栏 5 反向误触发)。
- **叠加审计 A3**:`maybe_revise` 里 `_ssr(real_chain, revision=r.get("revision", 0), ...)`
  ——`revise_skill` 返回值无 "revision" 键 → **每轮把 revision 归零**。

**修复(三合一,缺一不可)**:①拆列:low_streak 独立列/独立行(禁一列两用);
②语义按你的设计改:"修订过之后权重仍低于阈值(带最小样本)"才 +1,"权重回升至阈值上
(带最小样本)"才清零;③删 maybe_revise 的二次 `_ssr` 写入(修 revision 归零)。
**这组修复必须在"案例记忆→酿造信号④"之前完成**(我的前置依赖结论不变)。

### 1.2 DataRepairAgent:代码里不存在 ⚠️

你设计的失败路由"软失败 → DataRepairAgent 修一次"——**全仓 grep `repair` 零命中**,
`app/agent` 没有任何修复代理组件。设计文档引用的这个组件属"文档承诺、代码未建"
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

- GuidedPythonExecutor 的 `_rewrite/_reraise` 确实是现成错误分类采集点——失败记忆的
  采集点判断正确;error_type 词表采纳(补一个 `data_repair_failed` 待 repair.py 上线后用)。
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
| 0.3 | **酿造状态机三修**(核对 1.1:拆列 + low_streak 语义 + revision 归零) | chain/store.py + skill_brewer.py | 你 #4 + 审计 A2/A3 |
| 0.4 | **brew/revise 并发锁 + 版本历史**:PG advisory lock;.bak 改保留 N 版;修订版 M 天内样本分低于修订前 → 自动还原+告警 | skill_brewer.py | 你 #5 |
| 0.5 | **执行隔离第一步(低成本)**:危险调用(文件/网络/进程)审计留痕进 trace;`.env`/凭据路径 deny-list;完整隔离(子进程/容器/出网白名单)排远期 5.7 | guided_executor.py | 你 P0-1 + 审计 B3 |
| 0.6 | **记忆注入按不可信数据包裹**:memory 内容注入 prompt 加层级声明;跨会话只存摘要;记忆文本含指令式祈使句/工具名 → 降权丢弃 | nodes.py finalize/chat + memory 层 | 你 P0-3 |
| 0.7 | **并发槽位收编**(审计 B1):`_INTERRUPT_CHECKS/_current_event_cb/_active_code_agent` 进 per-session context,消除多 worker 互杀/事件串流 | message_queue + task_agent | 审计 B1 + 你 #11 |
| 0.8 | **可复现字段**:run 级 trace 补 prompt 哈希(plan_system/code_agent)、温度/seed、工具 schema 哈希、代码版本标识 | utils/tracing.py | 你 #8 |
| 0.9 | **成本硬预算三条线**:单 run token/工具调用/单用户日配额,超限优雅收尾(已完成阶段产物交出去);计数器面板:单块遵守率/幻觉拦截数/澄清率/replan 率/熔断次数 | task_agent + 周报脚本 | 你 #10 + 原则 7 |
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
  resolved=true 保留压缩一行(你,"换个工具名再犯"的洞察采纳);通道走
  `_inject_tool_failures` 现有单一注入口(我的单一通道原则,防多源重放膨胀)。

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
输入 SelfCheckError/失败上下文,输出修复动作或放弃;`_inject_disclaimers` 风格 fail-open。
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
| **0 安全止血** | 0.1 交易闸、0.2 direct_answer 禁数字、0.3 酿造三修、0.4 并发锁、0.5 隔离第一步、0.6 记忆包裹、0.7 并发槽位、0.8 可复现字段、0.9 成本硬线、0.10 审计清扫包 | 小(多为数十行级) | 无,立即可做 |
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

*完。阶段 0 + 先手共 13 个工单项,均在"数十行~数百行"量级,可直接开工;
按约定较大改动(图改造/调度器/隔离)先评审后动。*
