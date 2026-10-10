# Agent 差距与改进路线

> 2026-10-02 立档 · 基于三面审查（提智/降 token/超大 skills)+ 架构评估
> 最后更新：2026-10-11(J 裁定收口:本地即运行、不做 git 入库;此前 2026-10-10 晚 ABCEGJK 真实端点首轮出数:A 验收②达标;B 出数未达标;C/E 首跑;G/J/K 落地;smoke 基数 248）
> 状态标记：⬜ 未开始 · 🟨 进行中/部分落地 · ✅ 已完成
> 原则：每项都写清「现状 → 差距 → 方案 → 验收」,日后照单施工，不靠记忆。

## 总览

| # | 差距 | 优先级 | 类型 | 依赖 | 状态 |
|---|---|---|---|---|---|
| A | 规划质量无代码强保证 | P1 | 架构 | - | ✅ |
| B | 执行型技能无子 agent 分发通道 | P1 | 架构 | - | 🟨 |
| C | 能力级评测体系缺失 | P0 | 数据 | 跑批样本 | 🟨 |
| D | 会话上下文不跨进程持久化 | P1 | 架构 | - | ⬜ 待观察 |
| E | Few-shot 金例注入 | P2 | 提智 | C | 🟨 |
| F | 参数精调（温度/预算/阈值） | P2 | 调优 | C | ⬜ |
| G | 胜率反哺扩展为排序权重 | P2 | 提智 | C | ✅ |
| H | 成本/时延 SLO(钱和时间的预算） | P2 | 架构 | - | ⬜ |
| I | 超大技能 TL;DR 头部（内容工程） | P2 | 内容 | - | ⬜ |
| J | 发布工程收尾（admission/del/环境） | P1 | 工程 | - | ✅ (入库按本地模式裁定不做) |
| K | 工具预选从启发式到可学习 | P3 | 提智 | C | 🟨 机制已落地 |
| L | 备选路线 B 存档（MiMo-Code 大脑） | P3 | 战略 | - | 🟨 |
| M | 多轮递进会话的闭环结构化（总结机制） | P2 | 架构 | - | ⬜ |

---

## A. 规划质量无代码强保证（收尾完成：三选一已选①②，③按需）

**现状**(2026-10-10 收尾）:
- **2 run 级轻校验**(已选）:`qd_agent._plan_lint` 收尾查 `update_plan` 残账（in_progress/pending → 落 trace 告警，只告警不拦截）;smoke test_33 锁住。
- **1 prompt 回归套件补规划任务**(已选）:`prompt_tasks.yaml` 规划类达 **5 条**(A 验收线）--plan_update / plan_no_residue / plan_no_overhead / **plan_dispatch**(重活必须走 run_skill,兼作 B 验收「派活率」探针）/ **plan_acceptance**(收口必须交代覆盖面与数据缺口）。
- **3 plan_linter 从 del/ 恢复**:按需升级--仅当 1 的评测 diff 显示规划回退严重时才做（纯函数零依赖，归档时已确认可回滚）。

**差距**(余下）:已无（验收②于 2026-10-10 晚真实端点实证完成）;遗留 2 个 token 预算红（plan_update 418k/250k、plan_dispatch 246k/150k）内容质量均过，移交 F/H 处理。
**验收**:套件含 ≥5 个规划任务 ✅(5 条，任务集 10 条/35 断言校验通过）;评测 diff 可见规划质量变化 ✅——
两次改动实证:①R1→R2 题面改动（plan_acceptance「今天」→「最近一个交易日」）1/5→4/5;②R2→R3 改 `qd_system.txt` §重活分发（分步任务必须主对话 update_plan、不整体丢 task 子代理）→ **plan_* 断言 13/19→17/19、未过任务 5→2**、旧 5 任务 31/31 稳定、对 baseline 零回归，plan_no_overhead token 同步 99.5k→37.2k。报告存 `scripts/prompt_eval/round{1,2,3}.json`。

## B. 执行型技能无子 agent 分发通道（收尾：机制齐 + 验收测量器就绪）

**现状**(2026-10-10 收尾）:
- **机制三条已落地**:1 `run_skill(name, args)`(`tools/skill_tools.py`)--执行白名单 `skill_run.EXECUTABLE_SKILLS`(market_screener/strategy_debug)、子进程 600s 超时击杀、产物落 `tmp/skill_output/`、截断预览 + full_path 回传;2 `prompts/qd_system.txt` §重活分发（点名 run_skill、只回结论摘要、明细去文件看）;3 产物约定 → 上下文 O(摘要） 而非 O(全量）。smoke test_33 B2~5 锁住（白名单拦截/真实链路/分层归属/降级激活）。
- **验收测量器就绪**(2026-10-10):`scripts/qd_dispatch_ab.py`--分发 vs 不分发单变量对照（对照臂仅摘 `run_skill`,其余工具面全同）一键出数：成本比（tokens/步数/耗时）+ 质量差（与 C 同一把尺 qd_bench);判定：成本比 ≤ COST_MAX(默认 0.40)且质量降 ≤ 5;`--rejudge` 改阈值复核不必重跑;smoke test_37 锁判定数学与对照臂构造（9 项）。
- **冗余切片已收**(2026-10-10):执行型技能（skill_run 白名单内）命中不再注方法论正文，改注**分发卡片**(何时派/怎么派/入口/结果读法，~280 字符）--正文既费 token 又诱导主对话硬跑（与 §重活分发抢方向盘）;`read_skill` 兜底保留。与渐进式加载的分工：**知识型注方法论（渐进式加载）,执行型注分发卡片（run_skill 派活）**,两层正交不冗余。smoke test_38 锁分叉（9 项）。

**差距**(余下，2026-10-10 晚首跑出数）:成本比 **0.889 > 0.40 不达标**（质量Δ +0.5 ✓）——run_skill 被调用（trace 1849，52.5s 成功）但返回**空结果**（`analyzed: []`，盘后策略无候选）→ 模型读产物后回退主对话硬跑 6 个取数工具 → 只省 11%。方向:①prompt §重活分发补「技能空结果时接受结论或换策略重派，不回主对话硬跑」;②排查 market_screener 非交易日/盘后空筛（联动 eastmoney 断连）。
**验收**:一次全市场筛选任务，主 agent 消耗 ≤ 无分发方案的 40%,结论质量不降 ⬜——
首跑数据（screen_01, step-limit30, `dispatch_ab_20261010_210328.json`）:dispatch 163,980 tok/7步/314.6s/q86.7 vs inline 184,557 tok/7步/231.1s/q86.2 → 成本比 0.8885、质量Δ +0.5;`--rejudge` 可改阈值复核。测量器三坑已修（漏 `load_dotenv` 致"先跑臂必炸"假象、RetryError 报错文本误当答案打分、臂失败 verdict 假通过）。

## C. 能力级评测体系缺失（P0，其余多项依赖它）

**现状**(2026-10-10 部分落地）:标尺已立--`scripts/qd_bench.py` + `scripts/bench_tasks.yaml`(25 条任务，五类各 5:个股分析/选股/复盘/交易确认/通用问答）。
- 打分器（确定性，同输入同输出）:`coverage` 30(baseline.must 机检命中）/ `grounding` 25(复用 utils/grounding:溯源+合规+无绝对化）/ `claims` 20(复用 chain/claims:方向/幅度/价位/置信度）/ `structure` 15(结论先行/风险提示/多维/篇幅）/ `human` 10(标注文件，未标注按比例摊回）;
- CLI:`python -m scripts.qd_bench validate | score --preds x.jsonl | run | trend | diff | agreement | import-traces`;run 存档 `scripts/bench/runs/`,历史曲线可查;`--to-db` 挂 qd_traces 树（fail-open);
- 防线：smoke `test_36_capability_bench` 12 项（任务集体检/确定性/好坏区分度/must 规则/存档对比/一致率退出码）;`validate`/`score` 裸机可跑，不依赖执行核。

**差距**:任务集是 **seed 种子题**,不是 qd_traces 真实样本（`import-traces` 已修通可导出——补 load_dotenv/GROUP BY/psycopg2 `%%`/过滤 bench_ ab_ 会话污染——但 baseline 仍需人工补）;人工标注基线分未铺开（两人各标一轮才能验 agreement ≥80%）;`run` 模式已真实端点首跑（2026-10-10 晚）但伴随修复一个硬 bug（`cmd_run` 对 `_AgentResponse` 做 `len()`，答案被替换成错误串——已修，单题验证 88.6 分）。
**方案**(续）:1 用已修通的 `import-traces` 拉 20~50 条真实任务替换/扩充种子题（人工补 baseline）;2 两人各标 `annotations.json`,`agreement` 验收一致率 ≥80%;3 标尺首用已发生:E 金例注入前后用的就是这把尺（55.6→58.0）。
**验收**:任意改动后一键出分 ✅(score/run,首跑 71.7/25 题:coverage 81.5/grounding 86.8/claims 38.0/structure 62.6,分类均分 个股81.5/选股66.8/复盘48.6/交易81.6/问答81.7,区分度 Δ72.5,存档 `bench_20261010_real_first.json`）;历史曲线可查 ✅(trend/diff);两人一致率 ≥80% ⬜(待实标）。

## D. 会话上下文不跨进程持久化（待观察）

**裁决（2026-10-10,用户判定）**:原方案（messages 全量快照落 PG、重启恢复）**暂不做**,转观察。理由两条：
1. **污染闭环问责**:恢复旧消息会突破证据边界--`_observation_corpus()` 从 `self.messages` 收 grounding 语料 ⇒ 旧轮工具输出里的**过期数字**把"本轮没取到的数"洗成可溯源 → 陈旧结论过溯源门 → `record_decision` 抽出被污染的 claim → T+N 判定归因进 `weight_feed` EMA ⇒ 闭环学到"用旧数据也能对"的伪规律（L0-L2 只读边界的软性突破）;
2. **需求已被受控通道覆盖**:完整问答三处落存储（`qd_agent_messages` role/content 全量、`qd_traces` user_query+answer+工具链、chain intake decisions/claims)--**问责链数据零损失**;跨进程对话连续性已有受控通道（PostgresMemory 记忆近史：`_relevance≥2` 闸门 + 200 字/行截断 + 6 条上限，PG 共享跨 worker 可见）--它只带"聊过什么",不带"数据是什么"。裸恢复 = 把受控记忆换成不受控全量回放。

**现状**:重启真正丢的只剩"同 session 中段的工具中间结果"(如上一轮全量筛选明细）,其受控替代物已在：`run_skill` 的 `full_path` 产物文件（重读即可）/ 重跑工具。
**观察指标**(何时重新评估）:真实端点上因进程重启/多 worker 路由导致的会话断片次数、用户重问率。
**若到做那一天（预案）**:只做**最小受控续承**--恢复"指代指针"(上一轮标的/结论一句话）,走记忆近史同款闸门注入，且代码层硬隔离（恢复块标记后不进 `_observation_corpus`、不参与 claims 提取）。
**关联**:多轮**递进**会话在闭环里的结构化问题见 M 项（总结机制）--两者同源（都关乎"历史怎么进入未来决策"),但 M 不依赖 D。

## E. Few-shot 金例注入（部分落地：链路已接通）

**现状**(2026-10-10 接线完成）:
- **写入侧**:`qd_agent._finish_collector` 后置 `_record_case` → `case_memory.record_case()`(label 恒 pending,真值待 T+N 回填;失败坑/成本/plan_digest 齐落）;
- **注入侧**:`qd_service._prefetch` → `retrieve_cases()` + `format_case_injection()`(`CASE_MEMORY_ENABLED` 总闸;incorrect 硬排除/pending 降权由检索侧保证;成功例给骨架+坑、失败例只给坑）;
- **taxonomy 已定**(六类）:诊个股 / 选股 / 板块复盘 / 大盘研判 / 交易确认 / 止损复核（`case_memory.classify_case_tags` 规则式分类，选例按同型标签 ×1.2 加权，只调序不过滤）;
- 防再断线：`scripts/qd_smoke.py::test_34_case_memory_wiring`(调用方存在断言 + 注入语义 + 端到端 + fail-open)。

**差距**(余下部分,2026-10-10 晚首轮出数）:选例目前纯相似度+标签加权，**不是**「高分金例」--没有质量标尺，低分案例不淘汰（case 全 pending,label 待 T+N 回填）;检索是词面回退（sim=1.0 同题才命中,embedder 未生效）→ 泛化召回存疑;单次 A/B 归因噪声大（+2.4 里 qa_04 贡献 +53.1、6 题结构性免疫）。
**方案**(C 建立评分后继续）:按任务类型各挑 2~3 个高分案例（输入+输出骨架，脱敏数字）;按 evaluator 分数反馈轮换（低分案例淘汰）;接通 embedder 提升召回;复测一轮增强归因。
**验收**:同类任务金例注入后 evaluator 评分均值上升 ✅(首轮:注入关 55.6 → 开 58.0,+2.4,升3平6降1,带 caveat 见差距;存档 `runs/E_A_caseoff.json`/`E_B_caseon.json`、`preds_E_{A,B}.jsonl`）;单例注入成本 ≤ 500 tokens ✅(实测最大 240 tok,tiktoken)。

## F. 参数精调

**现状**:temperature=0.1、max_tokens=16384、step=AGENT_MAX_STEPS×3、技能阈值=2、预取相关度闸门=2--全是拍脑袋初值。
**差距**:无网格对比，不知道这些数是不是局部最优。
**方案**:以 C 的 benchmark 为标尺，对上述 5 个参数做单变量扫描（每次只动一个，跑分对比）;结论写回 `.env.example` 注释。
**验收**:每个参数有数据支撑的推荐值和敏感度说明（"±1 档掉几分")。

## G. 胜率反哺扩展（部分落地）

**现状**(2026-10-10 晚代码落地）:`chain/weight_feed`(追责→权重喂数）+ `weight_hints`(四层快照）已落地;权重已从 tie-break 升级为**排序分乘子**——`qd_service._rank_skills`:`score × (0.5 + 0.5×weight)`（weight∈[0.5,2.0] ⇒ 乘子∈[0.75,1.5]）,样本纪律复用写侧（weight_feed n<MIN_SAMPLES 只记账、evaluator n<10 恒 1.0,读侧不重复校验）;**注入阈值仍消费原始词典分**（权重只调序不过滤,与 hint_text/case 同型标签的 fail-open 哲学一致）。tool_preselect 侧的同理接入已随 K 落地（preselect_cooc 共现权重）。
**差距**:已无（代码+护栏齐;真实权重数据的排序影响待 weight_feed 积累样本后观察）。
**方案**:（完成）乘子化 + smoke 护栏。
**验收**:构造胜率差异明显的技能对，断言高胜率者稳定排前 ✅;冷启动（无数据）行为与现状一致 ✅——smoke `test_39_skill_weight_multiplier` 五断言:冷启动乘子=1.0 位序与纯词典分一致 / 同分对高权重排前 / 权重对调对称翻转（排除名字序假象）/ 低词典分×高权重可反超（乘子语义超出 tie-break）/ 阈值源码防回退（ranked[0][1]）。

## H. 成本/时延 SLO

**现状**:只有步数预算（step_limit);token/费用/时延无硬约束，`GlobalModelStats` 有计数但只用于 wrap-up 提示。
**差距**:一个任务跑飞 = 烧钱无上限;长任务挤占 worker 无降级。
**方案**:
1. run 级 token/费用预算（env:`QD_TOKEN_BUDGET`/`QD_COST_BUDGET`),超限触发与步数同款的"截断收尾"(已验证的独立预算机制可复用）;
2. 时延软 SLO:超时降级为"已有数据出结论 + 注明未完成部分"(比空结果好）。
**验收**:预算超限的 run 返回可读降级文案而非异常;预算事件落 trace(repro 字段已有 token 统计位）。

## I. 超大技能 TL;DR 头部（内容工程）

**现状**:渐进式加载（目录+首段+按需读路标）已上线;但 `auto_stock-analyze`(5.2k 字/6 节）等大技能仍靠截断出首段。
**差距**:截断点是字数不是语义，首段未必是最该看的（何时用/核心流程/硬约束）。
**方案**:给每个 ≥3k 字的 SKILL.md 加 300~500 字 TL;DR 头部（何时用/三步主流程/三条硬约束）,`_skill_block` 优先注入 TL;DR+目录。**内容由技能作者写**(方法论取舍是业务判断，代码不猜）。
**验收**:注入块 ≤1200 字符且覆盖"何时用/主流程/硬约束"三要素。

## J. 发布工程收尾

**现状**(2026-10-10 晚，第 3 步落地）:smoke **248 项断言**（立档 239 → 增 test_34~39）,本机 `--with-env` **245/248 · 0 fail · 3 skip**;`--with-env` 模式:6 个真实环境探针（pg 连库/stock_directory/archive_* 文件存在性/posix_shell/tool_deps import）把缺件 fail→skip,默认模式与 CI 行为逐字节一致。原 5 类环境缺件:PG 已配（转绿）、yfinance 已装（工具面≥95 转绿）、归档缺件转 skip;`del/` 现含 `tools_returns_contract_20261010/`(2026-10-10 归档，见施工日志）。
**差距**:（2026-10-11 裁定收口）1/2 **入库两项不做**——本地即运行模式（代码源起远程服务器的历史遗留，现工作副本=唯一事实源，agent 全程禁 git）;审计件保全改为本地备份需求，另计。4 dashscope 按需（当前 .env 未用）。归档目录缺失（agent_formatters_20261001、tools_returns_contract_20261010 磁盘上不存在）由 `--with-env` skip 兜住。
**方案**:（全部收口:3 已完成;1/2 裁定不做;4 按需）。
**验收**:干净 clone + `pip install -r requirements.txt` + 无 PG 环境 → smoke 全绿（skip 计入）✅（`--with-env` 把 PG/归档/依赖缺件全部转 skip,exit 0,不依赖入库）;带 PG 环境 → 全量通过 ✅(本机 245/248·0 fail·3 skip,3 skip 均为 del/ 归档缺件）。

## K. 工具预选从启发式到可学习

**现状**(2026-10-10 晚,机制落地）:`tool_preselect.py`(25 必注入 + 63 按需，21.2k→8.7k/轮）启发式不变为底;新增 `preselect_stats.record_task_calls` 落盘「任务→实际调用工具」对（env `QD_PRESELECT_PAIRS_FILE`）+ `tools/preselect_cooc.py`（token×工具共现 `build_weights`、mtime 缓存 `load/get_weights`、加权 `rank_candidates`、`heldout_recall` 验收器、周级 CLI `--db` 读现表 qd_agent_traces,未开新表）+ `qd_agent` 两钩子（过闸门后加权目录序/收尾攒对）。**任何缺数据场景（权重缺/样本<50/零覆盖/异常）→ 原序返回,冷启动 bit 级一致**;pytest 365 过（`tests/test_preselect_cooc.py` 7 项）。
**差距**:真实召回 ≥95% 达标性未验证——现仅 44 对（<50 正确退化中）;smoke 曾向配对文件追加演示数据（已隔离为 .bak,首次周级建议 `--db`）。
**方案**:（机制已完成）待数据过 50 对后 `python -m app.agent.tools.preselect_cooc --db` 跑批复核。
**验收**:held-out 任务上预选召回率（实际调用工具 ∈ 预选面）≥95%,token 不回升 ⬜——构造加权面逻辑达标 1.0;真实数据代理评测 8 任务:任务级全中 0→0.375,全量达标待跑批。

## L. 备选路线 B 存档（MiMo-Code 大脑）

**现状**:已评估未采纳（2026-10-01 定稿路线 A=white-box)。MiMo-Code 本体有持久记忆/上下文重建//dream,但金融工具需 MCP 化、行为偏软件工程。
**何时重启**:出现"长程编码类子代理"需求（策略代码生成/回测工程化）,或路线 A 的规划/记忆短板（A/D)修完仍不达标。
**要点存档**：`mimoagent.agents.blackbox.mimocode` 可驱动真 MiMoCode CLI（`mimo run --format json` headless），NDJSON 事件流可转 SSE——集成路径已探明。

---

## M. 多轮递进会话的闭环结构化（总结机制）

**提出**：2026-10-10（D 讨论中用户指出）。递进会话不能按**平面**关系进闭环。

**现状**：闭环按平面处理——每个 run 独立产 root/claims/resolutions，会话内的**递进依赖不可见**。已有的 `compaction_summary_prompt` 是**上下文压缩件**（保数字/结论/未完事项），不是链结构；`qd_agent_messages` 只是 role/content 平面历史；`case_memory.plan_digest` 是 run 级工具链，不上升到会话链。

**差距**（用四轮递进会话走一遍，归因错在哪）：
```
① 昨天出了什么新闻？          → 事实层（L0）
② 根据新闻分析大盘走向        → 判断层：claim_2（大盘方向），前提=①的新闻
③ 根据走向优选几只股票        → 选择层：claim_3（选股），前提=claim_2
④ 分析这几只股票              → 分析层：claim_4（个股结论），前提=claim_3 的名单
```
平面处理的错：②判错时③④连带错——现在会被记成**三个独立 miss**，锅扣到三个技能头上：`weight_feed` 把上游误判的锅分摊给下游选股/分析技能，权重层被系统性污染（与 D 的污染同源，只是入口不同：D 是“旧数据进新决策”，M 是“上游错被记成下游错”）。反过来②对③错才真该罚③——平面模型分不出这两种情况。

**方案**（三件）：
1. **递进链总结机制**：run 收尾把本轮落成结构化节点 {结论、前提（引用了哪些上游结论）、claim_ids}（思路同 case_memory.plan_digest，升维到会话级），会话链存**链式结构**而非平面列表；注入侧以**链摘要**替代裸历史（受控通道：只带结论与前提，不带工具数据）——它同时是 D 预案里“最小受控续承”的天然载体；
2. **claims 依赖标注与沿链传播**：claim 增加 `depends_on`（上游 claim_id/turn 序号）；T+N 判定时上游 miss ⇒ 下游标 `inherited-miss`（不重复惩罚下游技能/不重复喂数），上游 hit、下游 miss 才归因下游；
3. **判定用例**：上述四轮递进链 + 一组平面多轮（无依赖）对照，断言归因正确。

**验收**：递进链归因用例正确率 100%（②错→只罚②链路，③④不背锅）；平面对照组行为与现状完全一致（不误伤）；链摘要注入块 ≤ 1K 字符/轮。

---

## 施工顺序建议

```
第一批(立即): J 发布工程(让 smoke 全绿)→ I 技能 TL;DR(半天内容活)
第二批(攒数据): C 能力评测(P0 根基)→ A/B 架构两件
第三批(有标尺后): E/F/G/K 调优四件 → H 成本 SLO → D 会话持久化
远期: L(按需触发)
```

> 注（2026-10-10):C 已立标尺、A/B 已收尾（A=12组合;B=机制早已齐+验收测量器+分发卡片收敛）、E 链路已接--三件的**实测**(评测 diff / 对照出数 / 金例前后分）都卡在同一件事：真实端点环境。J 的 smoke 基数已更新为 239 项。

## 施工日志（倒序）

### 2026-10-11 · J 裁定收口（本地即运行，不做 git 入库）
- 用户裁定:代码源起远程服务器（历史上带 git），现为**本地运行模式**——本地工作副本即唯一事实源，agent 工作流不含任何 git 指令（与 AGENTS 规则 2 同源）。
- 推论:J 的 1/2 步（admission.json / del 归档入库）**不做**;原"干净 clone 全绿"验收由 `--with-env` skip 机制天然满足（缺件→skip→exit 0,不依赖版本控制）。审计件保全另计为本地备份需求。归档两目录（agent_formatters_20261001 / tools_returns_contract_20261010）磁盘缺失,由 skip 兜住。J 状态 🟨→✅。

### 2026-10-10 晚 · ABCEGJK 真实端点首轮（A 验收②达标;B/C/E 出数;G/J/K 落地）
- **A 验收② ✅**：改 `qd_system.txt` §重活分发（分步任务必须主对话 update_plan、不整体丢 task 子代理、分发不取代计划纪律）→ round3：**plan_* 断言 13/19→17/19、未过任务 5→2**、旧 5 任务 31/31、对 baseline 零回归，plan_no_overhead token 99.5k→37.2k;连同 R1→R2 题面改动（plan_acceptance 1/5→4/5）构成**两次改动 diff 实证**。剩 2 红=token 预算（plan_update 418k/250k、plan_dispatch 246k/150k）移交 F/H。报告 `scripts/prompt_eval/round{1,2,3}.json`。
- **B 首跑出数（未达标+根因）**：screen_01（step-limit30）dispatch 163,980 tok/7步/314.6s/q86.7 vs inline 184,557/7/231.1s/86.2 → **成本比 0.8885✗（≤0.40）· 质量Δ+0.5✓**。run_skill 被调（trace 1849,52.5s 成功）但返回**空结果**（`analyzed:[]`）→ 模型回退主对话硬跑 6 取数工具。测量器修 3 坑：漏 `load_dotenv`（三轮"先跑臂必炸"假象的根因）、RetryError 报错文本误当答案打分、臂失败 verdict/打印假通过。报告 `bench/dispatch_ab/dispatch_ab_20261010_210328.json`。
- **C 真实端点首跑**：**71.7/25 题**（coverage 81.5/grounding 86.8/claims 38.0/structure 62.6;分类:个股81.5/选股66.8/复盘48.6/交易81.6/问答81.7;区分度 Δ72.5）。修 `cmd_run` `len(_AgentResponse)` 硬 bug + `import-traces` 三坑（load_dotenv/DISTINCT→GROUP BY/psycopg2 字面 `%%`）+ 过滤 bench_/ab_ 会话污染;存档 `bench_20261010_real_first.json`。agreement 双人标注与 import baseline 人工补遗留。
- **E 首轮 A/B**：注入关 **55.6** → 开 **58.0（+2.4,升3平6降1**;caveat:qa_04 贡献 +53.1、6 题结构性免疫、case 全 pending）;**单例注入最大 240 tok ≤500 ✓**。词面回退检索（sim=1.0 才命中）待接 embedder。存档 `runs/E_A_caseoff/E_B_caseon`。
- **G 落地**：`qd_service._rank_skills` 乘子 `score×(0.5+0.5×weight)`、阈值看原始分（调序不过滤）、冷启动 bit 级一致;smoke `test_39` 五断言（同分对高权重排前/对称翻转/乘子反超/源码防回退）。
- **J 第 3 步**：`qd_smoke --with-env`（6 环境探针,缺件 fail→skip,默认模式与 CI 逐字节一致）;**断言基数 239→248**。
- **K 落地**：`preselect_stats.record_task_calls` + `tools/preselect_cooc.py`（共现权重/周级 CLI/`--db`）+ `qd_agent` 两钩子;冷启动一致,pytest 365 过;真实 ≥95% 待配对 >50 跑批。
- **本轮修的其他真 bug**：`skill_tools.run_skill` 子进程 GBK 解码崩溃（UTF-8+PYTHONIOENCODING）;`qd_prompt_eval` 本地工具可用性误判（`_tool_available` 回退查工具面,否则 run_skill 永远 SKIP）;`prompt_tasks.yaml` plan_acceptance 周末 TimeResolver 短路脆弱性（题面改「最近一个交易日」）。
- **环境备忘**：活环境 = 根目录 `D:\QuantDinger\.venv`（系统 Python 裸）;`qd_bench latest.json` 现指向单题验证 run,权威首跑 = `bench_20261010_real_first.json`。

### 2026-10-10 · D 裁决 + 新增 M（递进会话闭环结构化）
- D 讨论结论（用户判定）：不做裸 messages 跨进程恢复——① 恢复旧消息会污染闭环问责（过期数字洗白溯源门 → claim 被污染 → weight_feed 学伪规律）；② 完整问答三处落存储，问责链零损失，会话连续性已有受控通道（记忆近史带闸门）。D 保持**待观察**，观察指标=断片次数/用户重问率；若到做那一天只做最小受控续承（指代指针 + 代码层硬隔离）。
- 用户提出递进会话不能按平面关系进闭环（例：新闻→判大盘→选股→分析四轮递进）→ 新增 **M 项**（递进链总结机制 + claims 依赖沿链传播 + 归因判定用例）。M 不依赖 D，但链摘要可兼作 D 预案的受控续承载体。

### 2026-10-10 · 执行型技能分发卡片（B 冗余切片收敛，设计追问触发）
- 用户问「有子 agent 了，渐进式加载是否逻辑冗余」→ 核查结论：**整体不冗余**(知识型方法论 vs 执行型流水线，两层正交）,但执行型技能正文注入是真冗余且有行为风险（诱导主对话硬跑）。
- 改动：`qd_service._prefetch` 分叉--技能名 ∈ `skill_run.EXECUTABLE_SKILLS` → `_dispatch_card()`(~280 字符：何时派/run_skill 签名/入口/结果读法/read_skill 兜底）,不再注正文;白名单按**文件路径**加载（防 `import scripts.*` 被 backend_api_python/scripts 抢解析，test_30 同类坑）。
- 护栏：smoke `test_38_exec_skill_dispatch_card` 9/9 PASS;test_7 技能注入断言从「锁正文」改为「锁分发卡片」(设计变更，护栏跟设计走）。全量 smoke 233/239(余 6 项均环境缺件）。
- 实测待真实端点：plan_dispatch 派活率 + qd_dispatch_ab 成本比，收敛前后对比。

### 2026-10-10 · B 分发通道收尾（P1,机制早已齐，本次补验收测量器）
- 核查结论：B 的三条方案（run_skill 工具/分发纪律/产物约定）**均已落地**(2026-10-02 起）,缺的只是验收测量。
- 新增 `scripts/qd_dispatch_ab.py`:分发 vs 不分发单变量对照（对照臂仅摘 `run_skill`,**不改 prompt**--避免把 prompt 变量混进实验）;成本比 + 质量差一键判定（阈值 env 可调，`--rejudge` 复核不重跑）。
- 防线：smoke `test_37_dispatch_ab_harness` 9/9 PASS(判定数学/对照臂单变量性/报告回读/退出码）;全量 smoke 224/230(余 6 项均环境缺件）。
- 待续：真实端点跑一轮出数（默认题面 screen_01/screen_04,与 plan_dispatch 探针同源）。

### 2026-10-10 · A 规划质量收尾（P1,三选一已选12)
- 用户裁定：A 的门槛是「三选一或组合」,现已选 2(run 级轻校验，已落地）+ 1(prompt 规划任务）,3(plan_linter)按需不预做。
- 1落地：`prompt_tasks.yaml` 规划类 3 → **5 条**(达验收线）:新增 plan_dispatch(重活必须走 run_skill,兼作 B 验收「派活率」探针）、plan_acceptance(收口必须交代覆盖面与数据缺口）。
- 验证：`qd_prompt_eval --validate` 通过（任务集 10 条 / 35 断言）;真实端点实跑与 baseline diff 待环境就绪（验收2实证）。

### 2026-10-10 · C 能力级评测体系立标尺（P0,部分落地）
- 新增 `scripts/qd_bench.py`(能力级跑分器）+ `scripts/bench_tasks.yaml`(25 题，五类各 5)。
- 打分维度：coverage 30 / grounding 25 / claims 20 / structure 15 / human 10(未标注按比例摊回）,全部确定性机检（baseline.must 用检查项 id + kw_any/kw_all/regex 自定义规则）。
- 实测区分度：同一任务强答案 91.6 分 vs 弱答案 36.3 分（含绝对化承诺词被 no_fake_certainty 判负）;`trend`/`diff`/`agreement` 全部跑通（一致率达标退出 0 / 不达标退出 1)。
- 防线：smoke `test_36_capability_bench` 12/12 PASS;全量 smoke 215/221(余 6 项均环境缺件）。
- 待续（不阻塞其他项）:真实样本替换种子题（`import-traces`)、两人实标验证一致率、真实端点实测 `run`。

### 2026-10-10 · E 链路接线（P0,来自方案评估 §1.2)
- 接线两侧：写入 `qd_agent._finish_collector` → `_record_case` → `case_memory.record_case()`;注入 `qd_service._prefetch` → `retrieve_cases()` + `format_case_injection()`(`CASE_MEMORY_ENABLED` 总闸，fail-open)。
- 配套补齐：`trace_collector.plan_digest()`(案例原料）、taxonomy 六类 `classify_case_tags`(选例同型标签 ×1.2,只调序不过滤）。
- 验证：`test_34_case_memory_wiring` 21/21 PASS(防再断线断言 + 分类 + 注入语义 + 端到端 + fail-open);全量 smoke 当日 196/205(后续 203/209),改动链路 0 回归。
- E 的余下部分（高分选例 + 低分淘汰 + 单例 ≤500 tokens 预算）仍依赖 C,留在第三批。

### 2026-10-10 · returns_sampler / returns_contract 半死代码归档（P0,来自方案评估 §1.2)
- 验尸结论：零消费方（接线点 nodes.py/task_agent.py 已随旧编排层删除，v2 无"阶段白名单"概念）;返回结构契约已由 `tools/base.func_to_openai_schema` 并入 schema description(实测 80/114 工具声明 `Returns:`,覆盖 70%);`fn_adapter.py` 明文「2026-09-21 收束后不再采样」= 采样系被裁定掉，非漏接。
- 处置：`shutil.move` → `del/tools_returns_contract_20261010/`(README 含判据/回滚/触发条件）;不接回--接回会逆转落字决策且与省 token 红线冲突。
- 验证：`test_35_returns_contract_archive` 4/4 PASS(防死代码回流 tools/ 扫描目录 + 替代机制存活断言）。

## 附：本文档与既有文档的关系

- `AGENT_DESIGN.md`(根目录）:设计事实（代码是怎样文档就怎样）--差距修复后同步它
- `app/agent/MIGRATION_NOTE.md`:mimoagent 迁移说明与路线定稿（A 路线）
- `app/agent/scripts/qd_smoke.py`:回归防线（2026-10-10 晚起 **248 项**,`--with-env` 模式见 J）;本文档的验收标准最终都要落进它或 qd_prompt_eval;能力级质量分另见 `scripts/qd_bench.py`(路线 C),B 的对照实验见 `scripts/qd_dispatch_ab.py`
