# 提智改造 · 二波收尾 + 三波落地记录（2026-09-24，署名：OpenClaw agent）

> 来源任务：完成 `agent_tizhi_final_plan_20260923.md` 的后续波次（第二波/第三波）。
> 约束：**只改 `app/agent/` 目录内**；禁止 git 指令；本文件为增量记录，勿覆盖他人内容。
> 本文对应根目录活文档 §九「剩余清单」的第 2/3/4/5 项 + 三波 T1/T2 全部。

## 一、本次落地（全部在 app/agent/ 内）

### 二波 B1-B（此前最大缺口）✅
- `utils/plan_critic.py`（新）：三问 critic prompt（断点/验收不可判定/预算失配，输出
  {fatal, warnings, score}）+ Best-of-N 选优纯函数（fatal=0 取 score 最高；全 fatal 取
  最少带回炉；破平=最小工具面/最小步数/最少粒度信号）+ format_defects 回炉反馈。
- `agents/task_agent.py::_plan`：N=AGENT_PLAN_BEST_OF_N（默认 2，钳 [1,3]）并发采样 →
  critic 逐候选 → select_best → **回炉一次**（critic fatal 或 R3 粒度信号触发，
  缺陷清单带「其余一字不改」约束）。trace 新事件：plan_critic / plan_selected /
  plan_replan（候选 tag 全程可审计）。L0/L1 单候选不跑 critic（矩阵成本闸）。
- R3/R4 补进 `utils/plan_linter.py`：
  R3 粒度信号（预算顶格+验收>3 → 拆分建议；相邻阶段各≤2 步无边界 → 合并建议），
  只出信号回炉、不机械拆合；`granularity_hints()` 为单一事实源（预选期用原始承诺
  先算，与 lint_plan 内部同函数）。
  R4 工具面裁剪：只裁「零相关」条目（正文/倒排索引均未命中、非清单候选/产消/能力/
  技能保护名单），保留面 ≥ R4_MIN_FACE(3) 才裁；裁剪记录进 trace（如实报数）。

### 三波 T1（案例记忆 + search_knowledge + 酿造信号④）✅
- `utils/case_memory.py`（新）：qd_cases 表（additive 自建，JSONB 向量 + 词面兜底
  相似度）；record_case（finalize 后置钩子，pending 开局）/ retrieve_cases（top-3，
  incorrect 硬排除、pending 降权 0.5、近重复>0.95 只留最新已验证、低于阈值一条不注）/
  **backfill_by_root 延迟标签回填**（挂在 chain/store.update_verify_results 之后，
  只读 correct、只改 pending 行——P1 写保护不破）/ query_brew_case_signals（信号④）。
- 注入：`_plan` 里 top-3 案例按「成功=骨架候选+坑 / 失败=只给坑不给修复路径」渲染。
- `tools/knowledge/knowledge_tools.py`（新，域=knowledge 由子目录名登记表推导）：
  `search_knowledge(query, count, source)` 查历史结论库（qd_analysis_memory PG FTS，
  复用 rag/postgres_fts 分词语义）+ 案例库，带来源、只读、fail-open 缺口显式化。
- 酿造信号④接进 `chain/skill_brewer.brew_skills` 通道 3（聚类≥3 次 + 已定论≥2 +
  correct 率≥0.7 → 酿造候选，冷却门照常）。

### 三波 T2（难度路由）✅
- `agents/routing_policy.py`（新，登记表化禁散 if）：ROUTING_MATRIX（L0~L3 ×
  模型/管线/verify/linter/best-of-N/双跑）+ 确定性特征打分（series/research/multi/
  report 四族 + 域广度 + 多标的，L3 直升/L0 直降短路）+ 临界带 ±10%（C8 待评测校准）。
- 零额外调用（裁决 #4）：临界带把难度判定拼进**意图分类同一调用**，
  `parse_level` 从响应回收 Lx（prompts/intent_classifier.txt 增量追加契约）。
- 传导：chat → state.difficulty → plan（N 的取值）→ trace（difficulty_route 事件）。
- 升级信号：单段工具调用 ≥ AGENT_UPGRADE_TOOL_CALLS(8) / self_check_failed 数据缺口
  → 丢弃单段从头 plan（一次性，_upgrade_done）；route_after_execute 增 `_upgrade_pending`
  分支。模型自报 need_replan 的单段通道未建（参数留位，不假装生效）。
- **启用闸**：L0/L1 降档（小模型）默认关（AGENT_ROUTING_DOWNGRADE=0），需评测集先证
  掉点 <5pp；模型档位切换列暂未接线（每 run 单 LLM 实例），矩阵留档。

### 评审项收尾
- **P3 / TODO-1（chain 层权重）✅**：evaluator.update_weights 增⑦段产 layer='chain'
  权重行（n<10 不动权重、0.5~2.0 夹紧、unknown 链排除）；store.get_chain_weights()；
  planner 消费 = `_plan`【链路权重提示】（与工具权重同阈值事实源）。G1 缺口关闭。
- **C4 收尾 ✅**：planning YAML 工具视图收敛为**沙箱选定面**（tool_functions 键集），
  不再三条件各自推导——§7.0 三处口径（planning/沙箱/list_tools）自此同源。
  `_ListToolsTool` 的 domain 回退此前已删（加固②），本次无涉。
- **0.1 复核**：trading_tools 的 confirm 硬闸 + [Trading][AUDIT] 留痕**已完整存在**
  （stop 刻意不加闸——kill 动作安全优先），根文档标 🟡 为滞后记载。

## 二、验证（2026-09-24，本机）
- `python3 -m compileall app/agent` OK；`py_compile` 全部新改文件 OK。
- `pytest tests/test_wiring.py`：**33 passed, 2 failed**——2 条失败均为能力层
  （`load_admitted()`=0 → admission.json 不在本检出），与本次改动零交集（能力层
  注册链路未触碰），属检出既有缺口，见下。
- 新模块离线冒烟全过：select_best 三种情形（含 parse 失败恒排最后——冒烟当场抓到
  一个 tie-break bug 并修复）/ R3 双判据 / R4 裁剪+小面板保护 / 难度打分 L0~L3 /
  case_memory 无 DB fail-open / search_knowledge Returns 契约 + 1024 上限。

## 三、本检出与文档的漂移（只记录不动手，超出 app/agent 约束范围）
1. `capabilities/admission.json` **不在仓库**（load_admitted=0）→ 能力层在本检出
   整层不可用，wiring 2 条测试恒红。该文件带人工审核留痕（reviewed_by/at），须由
   用户从本机提交，不应由 agent 伪造。
2. `tests/evals/`（E1 runner + 种子用例）**不在仓库**——与文档"已落地"记载不符；
   同属未提交本地产物（提交由用户手工完成，agent 禁 git）。
3. 文档称 `test_wiring.py` 67 passed 且含 plan_linter/P1 锁定测试；本检出只有
   35 个测试函数（33+2）。文档描述的测试文件版本也未提交。
4. 既有红灯不修（超约束）：intent_classifier.txt 尾部「规则」块重复一遍（原文如此）。

## 四、遗留（下一步建议顺序）
1. **评测集基线**随代码回仓（tests/evals + 基线 jsonl）后，校准 C8 临界带宽与
   CASE_SIM_MIN（0.35 为拍脑袋初值）；基线首跑的 grounding 0/3 是**数据层**真问题
   （600176 K线 count=1 等），独立立项，不在 agent 层。
2. 模型档位切换（small/strong）接 llm 工厂（前置：评测集证小模型掉点 <5pp）。
3. 模型自报 need_replan 的单段通道（T2 升级信号第三路）。
4. 远期 F1~F6（并行/双跑/工具合成/异步化/完整隔离/cron 结构化）——属"较大变动
   先评审后动"，本轮**刻意未动**。

## 五、正确性 + 架构复查（2026-09-24 傍晚，同任务续，OpenClaw agent）

**抓到并已修的缺陷（3 个）**：
1. 🔴 **升级信号死循环**：`_upgrade_pending=True` 后若下一轮 execute 走早退错误分支
   （不带该字段），state 残留 True → route_after_execute 反复回 plan，每轮烧一次规划
   LLM，直到 MAX_ITERATIONS=50 兜底。修：plan_node 消费即清（return 带
   `_upgrade_pending: False`）+ execute 两个早退分支显式覆盖 False + AgentState 补声明
   （difficulty/_upgrade_pending/_upgrade_done）。防回归仿真已进冒烟。
2. 🟠 **采样全灭**：`asyncio.gather(return_exceptions=False)`——N 个采样 1 个网关 5xx
   全灭。修：return_exceptions=True，幸存候选照常选优，全部失败才抛首异常（保持
   "plan 完全不可用时如实报错"语义）；critic gather 同款兜底。
3. 🟠 **回炉候选无条件采纳**：可能比原件更烂却顶替。修：回炉产物与原候选再走同一
   select_best 纯函数，**严格更优才顶替，并列不换**（破平含 R3 粒度信号数）。
   另修 case_memory 去重"迭代中改列表"隐患（改原位替换）。

**逐项核对通过**：plan_response 多事件无消费端解析（tracing 只存事件，plan 列走
set_plan，不受 N 影响）；信号④产出键（chain_name/sample_root_id）与 brew 循环消费
键逐字对齐；evaluator⑦ 的 ON CONFLICT 形态与 tool 层同款、update_verify_results
早退不破坏 P1 写保护；R4 补位与裁剪互不冲突（R1 候选恒在保护名单）；lint 主体
（规格化→lint→apply）只作用于选中候选，与 Best-of-N 解析层不交叠；_use_critic/
_names/_diff_src 均先初始化后引用（无 UnboundLocalError 面）；新工具 search_knowledge
过 W17 Returns 契约与 1024 描述上限断言。

**架构评估**：职责分层干净（routing_policy=判定政策单源 / plan_linter=确定性 /
plan_critic=LLM 审查+选优 / case_memory=记忆层，全部懒 import 无环）；所有新增校验环
带 trace 计数（plan_critic/plan_replan/plan_selected/difficulty_route/case_injected/
difficulty_upgrade）满足原则 7 自证；成本闸三层（L0/L1 单候选、critic 按矩阵启用、
回炉一次写死）。

**残余风险（已知、不阻塞）**：critic 评分主观性（评测集校准后可调）；CASE_SIM_MIN=0.35
与临界带 ±10% 为拍脑袋初值（C8 家族）；升级信号的"模型自报 need_replan"单段通道未建；
多 worker 部署下 qd_cases/权重表为共享态而 case_memory._schema_ok 为进程内标记（幂等
DDL，无正确性影响）。
