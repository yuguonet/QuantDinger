0. 先决条件：评测集先行
提智的所有改动都该回答"变好多少"，否则就是换 prompt 玄学。你们缺的不是评测能力（chain/evaluator + T+N 回测基建都在），是覆盖复杂任务的固定评测集：

从历史 qd_traces 里挑 50~100 条真实任务，按复杂度分层：L0 单工具查询 / L1 单段分析 / L2 多阶段（现有 phases 能跑的）/ L3 当前系统跑不好的（跨领域、多标的对比、长链条研究）。
每条标注金标准要点（必须覆盖的结论/数字/来源），每周对当前 prompt + 模型组合全量跑一遍，出通过率曲线。
之后任何"提智"改动（换规划策略、换模型、改 prompt）都对着这条曲线说话。§7.12 的"单块遵守率 2/3"这种手工观测，第一步就该指标化进评测集。
没有这一步，下面每条都没法验收。

1. 规划层：从"一次生成 phases"到"规划可校验、可选优"
现状：planner 一次调用产出 task/phases/tools/step_budget，_normalize_phases 只做规格合法性（id 重排、幻觉工具剔除），不校验规划本身的质量——阶段依赖是否断裂、工具是否够用、预算是否匹配任务规模，全靠模型自觉。

三个具体升级：

Plan Linter（确定性 + LLM 混合）：归一化之后加一道静态检查。确定性部分：每阶段声明的工具能否覆盖该阶段交付物所需数据（阶段 acceptance 条目里出现"龙虎榜"但 tools 里没有 dragon 工具 → 打回）；阶段间是否存在隐式依赖没被 barrier 标记；预算总和与任务规模是否离谱（"全市场回测 + 多空研究"给 6 步）。LLM 部分：一个廉价模型做 plan critic，专挑"这计划会在哪一步断"。不合格的规划带着具体缺陷回炉一次，而不是等执行到一半烧完预算才发现。
Best-of-N 规划选优：关键任务（L2/L3）让 planner 出 2~3 个候选（temperature 非零采样），critic 按"最小工具面、最小步数、依赖最完整"打分选一个。规划是全流程最便宜的调用点，这里花 2 份 token 换执行期少走弯路，杠杆率很高。
阶段粒度自适应：现在 PLAN_MAX_PHASES=5 是拍死的。Linter 可以反过来用：如果一个 phase 的 step_budget 顶到 12 上限还显紧张，说明粒度太粗，打回让 planner 拆分；反之两个 phase 各 2 步且无 barrier，合并掉。让 phase 数随任务复杂度走，而不是固定天花板。
2. 执行层：从"跑完验收"到"边跑边自检 + 结果可信"
关键阶段双跑交叉验证：对结论敏感的阶段（选股结果、回测指标），同一任务书跑两份独立代码，结果 diff 超阈值才触发核查，一致就直接采信。金融数据口径差异（复权、停牌处理）正是单跑容易错且错了没感觉的地方。成本可控：只对 critic 判定"结论敏感"的阶段双跑，且第二次可以小模型跑。
代码块内建断言：在单块契约（§7.12）里加一条——代码中必须包含数据自检（行数非零、日期覆盖任务窗口、关键列 NaN 率、涨跌幅量级 sanity check），断言失败就 final_answer 输出数据缺口而不是硬算。这把验收从"阶段末尾 LLM 判"前移到"数据落地瞬间判"，等于给 _check_phase_acceptance 加了确定性前置层。
失败记忆进 run：GuidedPythonExecutor 已经在改写错误（幻觉调用/沙箱不可用），但改写只作用于当步。加一个轻量机制：本 run 内已出现过的错误类型与修复方式，注入后续步骤的提示（"前两步已经确认 get_daily 不存在，不要再试"）。这直接打掉 §7.12 残留的"反复改名重试"行为。
数据质量契约：工具返回带元数据（更新时间、来源、缺失字段），模型据此判断"这份数据能不能支撑结论"。配合已有的 missing_data 字段做硬要求：结论落盘前必须回答"缺什么数据会推翻这个结论"，空着不许 final_answer。
3. 质检层：finalize 前加一道独立审稿
这是我认为性价比最高的一条，你们的架构上只差一个节点：

finalize 之前插入 critic 调用（同模型不同 prompt 即可，不需要新模型）：核对结论中的数字与工具输出是否逐一对得上（数字溯源：每个关键数字标注来源工具/字段，critic 在工具输出里搜不到这个数字 → 判幻觉）、逻辑跳跃（"资金流出 → 必跌"这类无中间论据的推断）、结论与证据方向对齐。
判定有问题 → 只回炉一次（带具体缺陷清单），仍不过 → 降置信度输出并在回答中标注"以下结论未经数据核对"。
StateGraph 上就是 execute → verify → finalize | execute，路由加一条，route_after_execute 已有类似分叉，改造量小。
数字溯源再往前一步：formatter 层强制结论句带来源脚注，用户可核对，评测集也能自动判幻觉率。

4. 经验层：案例记忆补上技能酿造的粒度空档
技能酿造（§3.16）是"高频链路固化成 SKILL.md"，门槛高、周期长。中间缺一层案例记忆（case-based reasoning）：

每个 run 落一条案例：任务描述 + 规划摘要 + 工具链 + 结果要点 + correct 标记。
plan_node 时向量检索 top-3 相似历史案例注入规划提示（"类似任务当时走的工具链是……，T+N 验证正确"）。
与技能的关系：案例是"一次性的 few-shot"，技能是"固化的最佳实践"。案例库里高频复现的模式，正是酿造的候选信号源——等于给 §3.16 的通道①/②多了一个更早的观察窗口。
失败案例同样入库存档（带 fault 归因），注入时只给"此路不通"警告，不给方案。
RAG 侧对应的升级是执行期检索工具：现在 RAG 只在 chat_node 检索一次，执行中遇到知识缺口（行业口径、指标定义）只能 web_search。加一个 search_knowledge 工具查内部研究知识库/历史分析结论，检索质量比公网可控，也顺便把 RAG 投资用到执行段。

5. 并行与分解：复杂任务的吞吐天花板
phases 依赖图化：现在是线性列表 + barrier/replan 标记。让 planner 顺带声明 depends_on，无依赖的取数阶段并行跑（AsyncIO + 你们现成的线程池基建）。对比类任务（"对比 5 只券商股"）是天然 fan-out：每标的一个并行分支，汇总阶段 barrier 收口。这类任务现在串行跑 5 遍取数，token 和墙钟都是线性浪费。
复合工具合成：执行期发现稳定的代码模式（取 k 线 → 算指标 → 清洗），经 critic 确认后固化为会话级/用户级可复用函数（_SkillFuncTool 机制可以承接）。减少后续任务的步数和代码量，也给酿造提供"预制品"。
长任务异步化：超过墙钟预算的 L3 任务不硬跑，转后台执行 + cron 通知回传（message_queue 基建现成）。"宁可晚点给完整结论，不要 900 秒砍半截"。
6. 路由与成本：让任务难度匹配模型强度
现在 L0 问路价和 L3 全市场回测走同一管线同一个模型。加一层难度路由（intent 分类器顺带判 L0-L3）：

L0/L1 → 小模型、单段执行、低步数（成本降一个量级，为评测集跑真模型腾预算）。
L2/L3 → 强模型、多阶段、best-of-N 规划、critic 全开。
省下来的成本正好覆盖第 1、3 条增加的调用——提智不必然涨总成本，前提是路由先做。
落地顺序建议

阶段	做什么	改动量	收益
先手	评测集 + 数字溯源 critic（第 0、3 条）	小	幻觉率可测可控，后续改动可验收
二波	Plan Linter + 失败记忆进 run + 数据自检断言（第 1、2 条）	中	复杂任务一次成功率明显抬升
三波	案例记忆 + 难度路由（第 4、6 条）	中	提智与降本同时发生
远期	phases 并行化 + 双跑验证 + 工具合成（第 2、5 条）	大	吞吐与可信度上限
共同的底层判断：你们系统现在的短板不是"模型不够聪明"，而是聪明的部分（planner/CodeAgent）之间缺少互相校验的机制——规划没人挑错、结果没人核数、失败只救当步不记后步。把校验环补上，同样的模型能榨出明显更高的任务完成质量，而且这些机制大多站在你们已有的 critic/evaluator/trace 基建上，不算外挂。

要我把其中哪条展开成具体设计（节点契约、数据结构、prompt 草稿）？

展开吧

好，我把先手 + 二波的五件事展开成可直接动工的设计（数据结构、节点契约、prompt 草稿），三波/远期的下轮再拆。顺序上评测集最先行，因为后面四件事的验收都挂在它上面。

一、评测集（Eval Harness）
用例数据结构
每条用例一个 YAML，放 qd_evals/ 目录，离线 runner 批量跑：

yaml
id: EVAL-023
level: L2                      # L0 查询 / L1 单段分析 / L2 多阶段 / L3 长链条
prompt: "分析东方财富3月12日大跌原因并评估当前风险"
gold_requirements:
  - {type: number, key: 跌幅, expect: -7.03, tol_pct: 0.5, source_hint: get_stock_daily.pct_chg}
  - {type: fact,   key: 龙虎榜席位, accept_any: ["机构净卖", "深股通净卖"]}
  - {type: section, key: missing_data, accept: "非空且列出影响结论的数据缺口"}
forbidden:
  - "使用2024-03-12之后的数据推断当日成因"   # 时间泄漏检测
  - "把资金净流出直接断言为'必跌'"
budget: {max_steps: 24, wall_clock_s: 600}
tags: [dragon_list, event_attribution]
id: EVAL-023
level: L2                      # L0 查询 / L1 单段分析 / L2 多阶段 / L3 长链条
prompt: "分析东方财富3月12日大跌原因并评估当前风险"
gold_requirements:
  - {type: number, key: 跌幅, expect: -7.03, tol_pct: 0.5, source_hint: get_stock_daily.pct_chg}
  - {type: fact,   key: 龙虎榜席位, accept_any: ["机构净卖", "深股通净卖"]}
  - {type: section, key: missing_data, accept: "非空且列出影响结论的数据缺口"}
forbidden:
  - "使用2024-03-12之后的数据推断当日成因"   # 时间泄漏检测
  - "把资金净流出直接断言为'必跌'"
budget: {max_steps: 24, wall_clock_s: 600}
tags: [dragon_list, event_attribution]
金标准来源：从 qd_traces 里 correct 标记的 run 反向提炼——当时结论里经过 T+N 验证站住的数字和论点，直接就是 gold_requirements。不用从零标注。

三层判分器

层	判什么	实现	成本
L1 确定性	数字命中（容差）、必备字段、禁用表述、时间泄漏、工具调用是否在 allowed_tools 内、hit_max_steps	解析 run 的 trace JSONL + 最终输出，正则/数值匹配	零
L2 LLM judge	覆盖度、逻辑质量（每维 0–5）、结论-证据对齐	固定 rubric prompt，temperature=0，judge 与被测模型不同源	每条一次调用
L3 统计	幻觉数字率、一次成功率、步数/token 分布、复权正确率	由 L1 结果聚合	零
Runner 与产出
伪并行跑全部用例，落 qd_eval_runs/{date}/results.jsonl，产出周报表：按 level 分层的通过率曲线 + 与上一版的 diff（新增回归标红）。
A/B 模式：run_eval --variant plan-linter-on，直接对比两组结果。之后你说"加了 Plan Linter 之后 L2 一次成功率多少"，一天内有答案，不用猜。
回归门槛可以先手工看，跑稳后挂到发布检查上（L0/L1 通过率不得下降 >2pp）。
二、数字溯源 Critic（verify_node）
Graph 上加一个节点：execute → verify → finalize | execute，route_after_execute 的分叉从两条变三条（现在是验收过 → finalize / 否则回 execute，变成再加 verify 汇入）。先只对含数字结论的 L1+ 任务启用，L0 查询不走，省调用。

生成侧强制：claims 结构
conclusion 的每条关键论断必须带溯源，这是 verify 能落地的前提。FinalAnswerSchema 里给 conclusion 段加结构化 claims（formatter 再渲染成人读的脚注）：

json
"claims": [
  {
    "text": "3月12日收盘下跌7.03%",
    "value": -7.03,
    "provenance": {"tool": "get_stock_daily", "field": "pct_chg", "row": "2024-03-12"},
    "verified_by_exec": true
  }
]
"claims": [
  {
    "text": "3月12日收盘下跌7.03%",
    "value": -7.03,
    "provenance": {"tool": "get_stock_daily", "field": "pct_chg", "row": "2024-03-12"},
    "verified_by_exec": true
  }
]
prompt 侧（FinalizeAnswer 单块契约里加一条）：

【溯源硬要求】conclusion 中每一个数字论断都必须填 claims 并标注来源工具与字段。无法定位来源的数字不得写入结论——宁可写进 missing_data 说"该数字无数据支撑"。

确定性核对（零成本，最先做）
python
def check_grounding(claims, trace) -> GroundingReport:
    unverified = []
    for c in claims:
        outputs = collect_tool_outputs(trace, tool=c.provenance["tool"])
        if not match_value(outputs, c.value, tol=0.005):
            unverified.append(c)
    return GroundingReport(rate=len(unverified)/len(claims), details=unverified)
def check_grounding(claims, trace) -> GroundingReport:
    unverified = []
    for c in claims:
        outputs = collect_tool_outputs(trace, tool=c.provenance["tool"])
        if not match_value(outputs, c.value, tol=0.005):
            unverified.append(c)
    return GroundingReport(rate=len(unverified)/len(claims), details=unverified)
match_value 要处理格式变体，这是实操里最容易误杀的地方：千分位/百分号换算、单位缩放（亿/万）、四舍五入容差（±0.5 个百分点）、复权口径同值不同号。每种变体写进正则白名单，误杀比漏杀更烦人——漏杀交给 LLM 层兜底。

LLM critic 检查 prompt 草稿
输入 = 任务 + claims + 相关工具输出片段 + 逻辑链条。只审不改：

你是研究结论审稿人。针对每条 claim 只回答三件事：

1.落地性：标注的数值能否在引用的工具输出中找到（含单位/百分比换算）？
2.逻辑性：从事实到结论是否有中间论据？有无方向与数据相反、或把相关性当因果？
3.越界性：结论是否超出了证据范围（如从历史资金流推导确定性预测、给出仓位指令）？
输出 JSON：{"pass": [...], "fail": [{"claim_id": ..., "issue": ..., "severity": "fatal|soft"}]}。不要重写结论。
三级判定与路由
PASS（fail 为空或仅 soft）→ finalize。
REPAIR_ONCE（有 fatal）→ 回 execute 一次，回炉 prompt 只带缺陷清单："审稿发现以下问题（见列表）。只修复列出的问题：补数据、改论断或降级为 missing_data。未列出的内容一字不改，防止修一处坏三处。"
DEGRADE（回炉后仍 fatal）→ finalize 但输出降级版：只保留核对通过的 claims，未通过的整段移入"未经数据核对的存疑项"，confidence 降一档，trace 记 verify_failed 供 evaluator 归因。
关键约束是回炉只许一次——verify-execute 死循环是这类节点最常见的坑，写死在路由里而不是靠提示约束。

三、Plan Linter
插在 _normalize_phases 之后、plan 入 state 之前，两层。

确定性规则层（先做这四条）
维护一张静态词典 data_domain → required_tools（"龙虎榜"→dragon_list_hall、"估值"→stock_valuation_indicators……），然后：

R1 工具覆盖：phase 的 acceptance 条目抽数据域关键词 → 查词典得必需工具 → 不在该 phase 或全局 tools 里 → fatal，缺什么直接列给 planner 回炉。这条防的是"阶段交付物要求的数据没有工具能取"这种必然断链的规划。
R2 依赖完整性：planner 输出加 depends_on: [phase_id]（normalize 时缺省 []）。检测到隐式依赖但未声明（B 的 acceptance 引用 A 的产物域）→ 自动补 barrier 标记并 WARN，不阻塞。这条同时是远期 phases 并行化的地基——依赖显式化之后，无依赖分支才能安全 fan-out。
R3 预算-粒度失配：单 phase step_budget 顶到 PHASE_MAX_STEPS 上限且 acceptance > 3 条 → WARN"粒度过粗"，回炉拆分一次；两 phase 各 ≤2 步且无 barrier → 建议合并。
R4 工具面裁剪：planner 声明的 tools 多于实际各 phase 引用并集 → 裁到并集。削减误用面，也降低幻觉调用概率。
fatal 走回炉（一次），WARN 记 trace 供事后统计。

LLM critic 层（plan 选优 + 打分）
best-of-N 场景下同一个 critic 兼做选优。prompt 草稿：

你是研究规划审稿人。给定任务、工具清单（只有这些可用，没有其他）和候选计划。只挑会导致执行失败或结论不可靠的缺陷：

1.哪一步会因数据缺失或工具不支持而断掉？
2.哪一步的验收标准实际无法判定？
3.步数与预算是否和任务规模失配？
输出：{"fatal": [...], "warnings": [...], "score": 0-10}。不要改写计划本身。
选优规则：fatal=0 者按 score 取最高；全有 fatal → 取 fatal 最少者带回炉。评分维度里放一条"最小工具面、最小步数"，防止模型喜欢写宏大计划。

四、失败记忆（run 内闭环）
数据结构
GraphState 加一个字段：

python
"failure_memory": [
  {"step_id": 3, "error_type": "hallucinated_tool",
   "detail": "get_daily 不存在，实际工具 get_stock_daily", "resolved": true},
  {"step_id": 5, "error_type": "empty_result",
   "detail": "龙虎榜查3月返回空，该月无上榜记录", "resolved": true}
]
"failure_memory": [
  {"step_id": 3, "error_type": "hallucinated_tool",
   "detail": "get_daily 不存在，实际工具 get_stock_daily", "resolved": true},
  {"step_id": 5, "error_type": "empty_result",
   "detail": "龙虎榜查3月返回空，该月无上榜记录", "resolved": true}
]
采集点
GuidedPythonExecutor 的异常分类器就是天然采集点——它已经能区分幻觉调用/沙箱不可用/表结构对不上（§7.11 待补的 column-shape 修复也归口到这里，一次分类多处受益）。分类器在改写修复的同一分支里 append 一条，DataRepairAgent 的每次 repair 也 append。顺手把 error_type 词表定死：hallucinated_tool / sandbox_unavailable / wrong_column / wrong_frequency / empty_result / self_check_failed——这也是评测集 L3 统计的口径来源。

注入
execute_node 组 prompt 时，failure_memory 非空才注入，上限 5 条、每条 ≤80 字（token 预算要抠）：

【本次任务已确认的失败事实】

step3: get_daily 不存在，实际工具为 get_stock_daily。不要重试。
step5: 龙虎榜 2024-03 无记录，该事实按"无上榜"处理，不要换工具反复查。
注入语义偏"禁止令 + 已确认事实"，不给修复方案——方案给多了模型会跳步。resloved=true 的条目仍保留（防止换个工具名再犯），但 detail 压缩到一行。

五、数据自检断言
提供预置校验函数，而不是让模型每次自己写 assert
沙箱里预置一个 validate_df（这同时是"复合工具合成"的第一个种子）：

python
def validate_df(df, expected_cols, date_window=None, max_nan_ratio=0.1):
    """检查失败抛 SelfCheckError(code, detail)，执行器捕获后路由。"""
def validate_df(df, expected_cols, date_window=None, max_nan_ratio=0.1):
    """检查失败抛 SelfCheckError(code, detail)，执行器捕获后路由。"""
单块契约（§7.12）追加一条
【数据自检（硬要求）】任何指标计算前，必须先对原始数据执行 validate_df(...)：
覆盖任务时间窗口、行数非零、关键列 NaN 率、价格变动量级 sanity check。
自检失败不得继续计算——抛出的 SelfCheckError 会带缺口信息返回，缺口写入 missing_data。

失败路由（新 error_type: self_check_failed）
软失败（缺日期、有 NaN）→ DataRepairAgent 修一次；修不好 → 记 missing_data，阶段验收可以放行（缺口显式声明比硬凑数字值钱），结论按缺口降置信度。
硬失败（价格量级离谱、频度错配）→ 判口径错误，回 execute 换口径重算，不许绕过。
这样阶段验收 _check_phase_acceptance 前面就多了一道确定性闸门：LLM 判"该做的都做了"之前，数据先得自己过关。

接线总览与验收
text
plan_node → normalize → [Plan Linter] → (fatal? 回炉一次) → execute
execute ←───────────── failure_memory 注入
   ↓（每步 validate_df 闸门 + 分类器采集失败）
verify_node → PASS → finalize
            → REPAIR_ONCE → execute（带缺陷清单）
            → DEGRADE → finalize（降级输出）
plan_node → normalize → [Plan Linter] → (fatal? 回炉一次) → execute
execute ←───────────── failure_memory 注入
   ↓（每步 validate_df 闸门 + 分类器采集失败）
verify_node → PASS → finalize
            → REPAIR_ONCE → execute（带缺陷清单）
            → DEGRADE → finalize（降级输出）
对着评测集的验收目标，二波做完后应看到：


指标	现状口径	目标方向
L2 一次成功率（不走回炉）	基线待测	明显抬升
幻觉数字率（落地性核对失败数/总 claims）	基线待测	趋近 0
hit_max_steps 率	基线待测	下降（规划断链提前拦住）
同一错误重复出现次数	基线待测	每 run ≤1
"现状待测"不是敷衍——评测集没跑起来之前这些基线是真不知道，这也是把它排最前面的原因。

要继续拆三波（案例记忆 + 难度路由），还是先把远期的 phases 并行化展开？

先说总评：这份文档质量在开源项目里属于少见的水平——"代码是怎样文档就怎样"、把高发 bug 单列成章（§7.0/§7.1）、连"零引用但保留"的文件都标注了去向，这种文档纪律本身就是可靠性的一部分。所以下面的意见不是泛泛的"建议加测试加监控"，我尽量只提文档里真实暴露的缺口。

两个前提说明：你贴的内容在 §7.14 中途截断，§8 优化建议和附录 M 我没看到，下面可能与你们已有的条目重复，重复的就当交叉验证。另外我只有文档没有代码，个别判断（尤其一处逻辑疑点）请拿代码核对。

一、安全与信任边界（P0，建议最优先）
1. 当前形态 = LLM 驱动的任意代码执行，且外部内容可触达执行链。
§7.10 自己写了：additional_authorized_imports=["*"]、破坏性操作零拦截、smolagents 的 DANGEROUS_FUNCTIONS 实测不生效。os.system 不在 52 个内置里不要紧——import os 是放行的，之后 os.system 只是普通属性调用。同时沙箱里有 web_search（外部不可信文本直接进 observation）、RAG 注入、memory 注入。把这几件事连起来是一条完整的间接提示注入 → 代码执行链：网页/新闻文本里嵌一句指令，模型照做写进 code 块，即在你们服务器上执行。

之前那个审批层的问题不是"防护不该有"，而是"接线了但没生效"。正确的修法二选一（建议都做）：

真隔离：CodeAgent 执行放独立子进程/容器——非特权 UID、seccomp 或至少禁 socket/subprocess/os.system 一类能力、文件系统只读 + tmp/ 配额、出网走白名单代理。文档定位是"领域中立的执行层"，隔离边界就该在执行进程上，而不是靠 52 个内置函数白名单。
能力面收口：如果业务上不需要模型装包/联网写文件，把 import 白名单收回实际所需（pandas/numpy/统计库清单化），网络、文件、进程三类能力走显式闸门，而不是 ["*"]。
2. 交易链路的确定性风控在文档里是空白。tools/finance/trading_tools.py 存在，但全文没有下单前检查、限额、人工确认、幂等的任何描述。如果它只是模拟盘/组合评估，建议在文档里明确写死"本系统不接真实下单"；如果有真实通道，硬性风控（仓位上限、单票集中度、单日最大亏损、熔断、kill switch）必须是独立于 LLM 的确定性代码，且执行动作需要 human-in-the-loop 确认 + 审计留痕。这是量化 Agent 系统唯一不能用"宁松勿卡"哲学的地方——§4.3 的 fail-open 用在验收判定上可以，用在资损边界上不行。

3. 记忆是提示注入的持久化通道。 finalize 把对话写进 memory，memory 后端可跨会话、TTL 7 天。一次成功的注入可以写入一条"以后遇到 X 就调用 Y"的记忆，长期影响后续所有会话。建议：memory 内容注入 prompt 时统一按"不可信数据"包裹（明确指令层级声明）；会话间记忆只保留摘要而非原文；异常模式（记忆文本中出现工具名/指令式祈使句）降权或丢弃。

二、自改进闭环（§3.16/§3.17）——风险第二高的一块
4. 护栏 5 疑似不可达，请核对计数逻辑。 文档写：筛选条件 weight < 0.7 → 跳过 low_streak ≥ 2 → 修订 → 护栏 2「修订完成 → set_skill_revision(revision+1, low_streak=0)」。那么一个持续表现差的技能走的循环是：low → low_streak 置 1 → 修订 → 归 0 → 下轮 low → 置 1 → 修订 → 归 0……low_streak 永远到不了 2，护栏 5 的"修差循环转淘汰"分支在稳态下不可达，实际效果是无限修订循环（每次还把权重重置回 1.0 放回高位）。修法建议：low_streak 只在权重回升到阈值之上（带最小样本）时才清零；"修订过之后仍然低"才 +1。这样"连续两轮修订仍差 → 淘汰"才是可观测的行为，与护栏 5 的文字一致。

5. 酿造/修订需要并发锁 + 版本历史。 15:30 eval worker 的 trigger="auto" 和 python -m ...skill_brewer 手动入口可以同时跑，同一份 SKILL.md 会被两个 LLM 修订任务并发写；.bak 单版本回滚在这种场景下互相覆盖，恰好丢掉需要的那一版。建议：brew/revise 全程拿 PostgreSQL advisory lock（或文件锁）；.bak 改为保留 N 个历史版本（或 skills 目录进 git，revision 号即 diff 基线）；再加一条自动回滚：修订版投入后 M 天内样本分低于修订前基线 → 自动还原上一版并告警。你们本来就有 revision 号和权重历史，做这个检测的数据是现成的。

6. 酿造的统计口径要抗小样本噪声。 信号分 verified_correct × win_rate × min(1, verified_correct/10) 把计数和比率直接相乘，10 中 7 对（3.43 分过线）vs 4 中 4 对（1.6 分不过线）——后者样本更干净反而进不了酿造，这个倒挂值得重新推一遍。另外 correct 是 T+N 结果判定，会把运气记成技能质量（分析逻辑对但行情反转 = 技能被冤枉降权）。建议：win_rate 用 Beta 后验下界（或 Wilson 下界）替代裸比率参与信号分；correct 拆成"过程分"（验收判定、数据完备性）与"结果分"（T+N）两列分别迭代权重；新酿造技能先走影子期——只在小比例流量生效或先跑一轮验证任务，通过后才进 planner 的一般选择面。现在的机制等于 brew 完立即全量生效，一次坏酿造污染整个意图类。

三、决策正确性（量化场景特有的坑）
7. direct_answer 路径可能编造行情。 意图分类走 chat 就直接 LLM 回答，不触达任何工具。用户问"茅台现在多少钱"这类含实体 + 数据意图的问题如果被分类成 chat，回答来自参数化记忆——数字必错，且 trace 里没有任何取数痕迹，事后无法归因。建议在 chat_node 加一道确定性门：实体 + 数据/行情类意图 → 强制走 task 路径（regex/词表即可，不必上 LLM）；direct_answer 的 prompt 明令禁止输出任何价格、涨跌幅、财务数字，被问到就转 task。

8. 可复现性记录不完整。 qd_traces 根节点有 model / total_tokens，但复盘一个决策至少还需要：代码版本（git SHA）、prompt 版本（plan_system.txt / code_agent.yaml 内容哈希）、温度与 seed 实际值、工具 schema 版本（比如 returns_contract 抽取结果的哈希）。§3.16 的"泛化靠语义匹配"意味着同一技能在不同模型版本下行为会漂，没有这些字段，事后没法回答"这单结论是哪套配置给出的"。字段都很便宜，建议补进 run 级 trace。

四、可靠性与成本（P2）
9. 崩溃恢复的前置债建议现在还。_code_agent / _phase_agents 不可序列化字段躺在 AgentState 里，Checkpointer 因此不能启用——但多阶段任务动辄几十秒到几分钟，进程一挂全丢，cron 场景还会重跑。不必先上数据库 checkpointer，先把这两个字段挪进 NodeContext 或独立 side table（state 里只留 run_id 引用），这一步是纯重构，做完崩溃恢复和断点续跑才有下文。

10. 成本只有软约束，没有硬预算。 有步数预算、墙钟、breaker，但 §7.12 你们自己记录过单任务 30 步/百万级 token 的事故。建议加三条硬线并在超限时优雅收尾（把已完成阶段的产物交出去）：单 run token 预算、单 run 工具调用次数上限、单用户/单日配额。同时把几个手工观测的指标变成计数器 + 告警：单块遵守率（§7.12 残留的 roll 方差）、幻觉拦截次数（GuidedPythonExecutor 的改写分支计数，_err_counts 删了但这个业务计数值得留）、澄清率（clarify 契约命中率高 = 解析器质量问题）、replan 率、breaker 熔断次数。

11. 多进程语义没有交代。 cron_worker 是独立进程，Flask 若多 worker 部署，_SHARED_TOOL_PROVIDER、_InMemoryCache、staging _OBJ、ToolCircuitBreaker 计数都是进程内的。brew_state 放 DB 是对的，但 breaker 状态、缓存、staging 在"Flask 跑一半挂了由另一个 worker 接手/运维重启"的场景下语义是未定义的。建议文档加一节"哪些状态是进程内、哪些是共享的"，进程内的都标注失效场景；生产环境 memory 后端默认 Postgres（local 后端在多 worker 下会话连续性直接断）。

12. cron 正则拦截是脆的。_try_intercept_cron 靠正则匹配调度意图，同义改写就漏、误匹配就直接建任务跳过 agent 流程。建议改为让 planner 以结构化方式输出 create_cron_job 调用（走正常工具链），正则只留作快捷通道；同时加调度滥用防线：cron 表达式校验、最短间隔、单用户任务数上限——否则一句"每分钟给我跑一次全市场回测"就是自打 DDoS。

五、防回归与测试（P2，你们的 bug 类型决定了测试的形态）
§7 里 19 条易错点每条都有"实测"字样，说明回归目前靠人肉跑 run 发现。针对你们两类高发 bug（死接线、契约漂移），测试应该长这样：

契约自动对账：§7.13 已经把 Returns: 段定为真源，那就加一条 CI 测试——对每个声明了 Returns: 的工具跑一次 dry 调用（录制响应），断言真实返回键与文档逐字一致。你们自己总结过"契约漂移比没有更糟"，那就别靠人记得回来改。
死声明/死接线扫描自动化：env.example vs os.getenv 的比对（§7.11 手工做过一次）做成 pre-commit/CI 脚本；顺手加"注册表条目零消费"扫描，能抓住 trace_collector.py 这类零引用残留。
golden tests：_normalize_phases 三态语义、_normalize_plan_tools 并集语义、resilient_parse v5 的伪标签/散落抢救——这些都是纯函数，输入输出固定，是最便宜的回归防线（三态那个 bug 本身就是 golden test 能拦住的类型）。
record/replay E2E：录一次真实 LLM 响应做 fixture，CI 里用 fake model 跑完整 chat→finalize，不依赖付费 API。模型行为类问题（单块遵守率）另设每周评测集跑真模型出趋势，不进 CI 门禁。
六、文档与小项（P3）
建议补"威胁模型/信任边界"一章：用户输入、web_search、RAG、memory、工具返回五路不可信数据的标注与处置策略。文档现在有"可追责"原则，缺"可防御"视角，而 §7 的大半易错点其实都是信任边界问题。
死代码处置口径统一：trace_collector.py（零引用）和 query_cached_tools 读侧（零命中）都还挂着，与 §7.11"删声明"的裁定不一致。按你们自己的原则，要么删，要么在文档里写明保留理由（appendix M B1 如果已写明，把结论同步到 §3.11.2 一句话）。
命名统一：§7.7 的三层 RAG 阈值同名不同义（env 0.5 vs 硬编码 0.7），文档已经解释清楚了，但下一个人还是会踩。改名是十分钟的事（RAG_RERANK_ABS_THRESHOLD），比在文档里加警告段落有效。
附录 M 的 MASK 范围请确认覆盖 JSONL 文件与 log.py 输出，不止 DB。
§3.16 常量散在 store.py / skill_brewer.py 两层（你们计划批 4 迁 chain/constants.py）——建议把 §5.2 的模块常量一并迁入，配置面收敛成"env = 运维可调 / constants.py = 领域常量"两层。
如果要排优先级，我会先做三件事：执行隔离（P0-1）、low_streak 计数修正 + 酿造并发锁（P0-4/5，成本极低）、direct_answer 禁数字门（P0-7，一个词表就能上）。要我把其中哪一块展开成代码级的改造清单？

还能怎样提智和复杂任务处理能力?

好问题。上一轮我提的是"别出错"，这轮提"更聪明"。我按你们现有架构的延伸成本从低到高排，每条都说清楚站在哪个既有组件上加什么——纯外挂的新概念我不提。

0. 先决条件：评测集先行
提智的所有改动都该回答"变好多少"，否则就是换 prompt 玄学。你们缺的不是评测能力（chain/evaluator + T+N 回测基建都在），是覆盖复杂任务的固定评测集：

从历史 qd_traces 里挑 50~100 条真实任务，按复杂度分层：L0 单工具查询 / L1 单段分析 / L2 多阶段（现有 phases 能跑的）/ L3 当前系统跑不好的（跨领域、多标的对比、长链条研究）。
每条标注金标准要点（必须覆盖的结论/数字/来源），每周对当前 prompt + 模型组合全量跑一遍，出通过率曲线。
之后任何"提智"改动（换规划策略、换模型、改 prompt）都对着这条曲线说话。§7.12 的"单块遵守率 2/3"这种手工观测，第一步就该指标化进评测集。
没有这一步，下面每条都没法验收。

1. 规划层：从"一次生成 phases"到"规划可校验、可选优"
现状：planner 一次调用产出 task/phases/tools/step_budget，_normalize_phases 只做规格合法性（id 重排、幻觉工具剔除），不校验规划本身的质量——阶段依赖是否断裂、工具是否够用、预算是否匹配任务规模，全靠模型自觉。

三个具体升级：

Plan Linter（确定性 + LLM 混合）：归一化之后加一道静态检查。确定性部分：每阶段声明的工具能否覆盖该阶段交付物所需数据（阶段 acceptance 条目里出现"龙虎榜"但 tools 里没有 dragon 工具 → 打回）；阶段间是否存在隐式依赖没被 barrier 标记；预算总和与任务规模是否离谱（"全市场回测 + 多空研究"给 6 步）。LLM 部分：一个廉价模型做 plan critic，专挑"这计划会在哪一步断"。不合格的规划带着具体缺陷回炉一次，而不是等执行到一半烧完预算才发现。
Best-of-N 规划选优：关键任务（L2/L3）让 planner 出 2~3 个候选（temperature 非零采样），critic 按"最小工具面、最小步数、依赖最完整"打分选一个。规划是全流程最便宜的调用点，这里花 2 份 token 换执行期少走弯路，杠杆率很高。
阶段粒度自适应：现在 PLAN_MAX_PHASES=5 是拍死的。Linter 可以反过来用：如果一个 phase 的 step_budget 顶到 12 上限还显紧张，说明粒度太粗，打回让 planner 拆分；反之两个 phase 各 2 步且无 barrier，合并掉。让 phase 数随任务复杂度走，而不是固定天花板。
2. 执行层：从"跑完验收"到"边跑边自检 + 结果可信"
关键阶段双跑交叉验证：对结论敏感的阶段（选股结果、回测指标），同一任务书跑两份独立代码，结果 diff 超阈值才触发核查，一致就直接采信。金融数据口径差异（复权、停牌处理）正是单跑容易错且错了没感觉的地方。成本可控：只对 critic 判定"结论敏感"的阶段双跑，且第二次可以小模型跑。
代码块内建断言：在单块契约（§7.12）里加一条——代码中必须包含数据自检（行数非零、日期覆盖任务窗口、关键列 NaN 率、涨跌幅量级 sanity check），断言失败就 final_answer 输出数据缺口而不是硬算。这把验收从"阶段末尾 LLM 判"前移到"数据落地瞬间判"，等于给 _check_phase_acceptance 加了确定性前置层。
失败记忆进 run：GuidedPythonExecutor 已经在改写错误（幻觉调用/沙箱不可用），但改写只作用于当步。加一个轻量机制：本 run 内已出现过的错误类型与修复方式，注入后续步骤的提示（"前两步已经确认 get_daily 不存在，不要再试"）。这直接打掉 §7.12 残留的"反复改名重试"行为。
数据质量契约：工具返回带元数据（更新时间、来源、缺失字段），模型据此判断"这份数据能不能支撑结论"。配合已有的 missing_data 字段做硬要求：结论落盘前必须回答"缺什么数据会推翻这个结论"，空着不许 final_answer。
3. 质检层：finalize 前加一道独立审稿
这是我认为性价比最高的一条，你们的架构上只差一个节点：

finalize 之前插入 critic 调用（同模型不同 prompt 即可，不需要新模型）：核对结论中的数字与工具输出是否逐一对得上（数字溯源：每个关键数字标注来源工具/字段，critic 在工具输出里搜不到这个数字 → 判幻觉）、逻辑跳跃（"资金流出 → 必跌"这类无中间论据的推断）、结论与证据方向对齐。
判定有问题 → 只回炉一次（带具体缺陷清单），仍不过 → 降置信度输出并在回答中标注"以下结论未经数据核对"。
StateGraph 上就是 execute → verify → finalize | execute，路由加一条，route_after_execute 已有类似分叉，改造量小。
数字溯源再往前一步：formatter 层强制结论句带来源脚注，用户可核对，评测集也能自动判幻觉率。

4. 经验层：案例记忆补上技能酿造的粒度空档
技能酿造（§3.16）是"高频链路固化成 SKILL.md"，门槛高、周期长。中间缺一层案例记忆（case-based reasoning）：

每个 run 落一条案例：任务描述 + 规划摘要 + 工具链 + 结果要点 + correct 标记。
plan_node 时向量检索 top-3 相似历史案例注入规划提示（"类似任务当时走的工具链是……，T+N 验证正确"）。
与技能的关系：案例是"一次性的 few-shot"，技能是"固化的最佳实践"。案例库里高频复现的模式，正是酿造的候选信号源——等于给 §3.16 的通道①/②多了一个更早的观察窗口。
失败案例同样入库存档（带 fault 归因），注入时只给"此路不通"警告，不给方案。
RAG 侧对应的升级是执行期检索工具：现在 RAG 只在 chat_node 检索一次，执行中遇到知识缺口（行业口径、指标定义）只能 web_search。加一个 search_knowledge 工具查内部研究知识库/历史分析结论，检索质量比公网可控，也顺便把 RAG 投资用到执行段。

5. 并行与分解：复杂任务的吞吐天花板
phases 依赖图化：现在是线性列表 + barrier/replan 标记。让 planner 顺带声明 depends_on，无依赖的取数阶段并行跑（AsyncIO + 你们现成的线程池基建）。对比类任务（"对比 5 只券商股"）是天然 fan-out：每标的一个并行分支，汇总阶段 barrier 收口。这类任务现在串行跑 5 遍取数，token 和墙钟都是线性浪费。
复合工具合成：执行期发现稳定的代码模式（取 k 线 → 算指标 → 清洗），经 critic 确认后固化为会话级/用户级可复用函数（_SkillFuncTool 机制可以承接）。减少后续任务的步数和代码量，也给酿造提供"预制品"。
长任务异步化：超过墙钟预算的 L3 任务不硬跑，转后台执行 + cron 通知回传（message_queue 基建现成）。"宁可晚点给完整结论，不要 900 秒砍半截"。
6. 路由与成本：让任务难度匹配模型强度
现在 L0 问路价和 L3 全市场回测走同一管线同一个模型。加一层难度路由（intent 分类器顺带判 L0-L3）：

L0/L1 → 小模型、单段执行、低步数（成本降一个量级，为评测集跑真模型腾预算）。
L2/L3 → 强模型、多阶段、best-of-N 规划、critic 全开。
省下来的成本正好覆盖第 1、3 条增加的调用——提智不必然涨总成本，前提是路由先做。
落地顺序建议

阶段	做什么	改动量	收益
先手	评测集 + 数字溯源 critic（第 0、3 条）	小	幻觉率可测可控，后续改动可验收
二波	Plan Linter + 失败记忆进 run + 数据自检断言（第 1、2 条）	中	复杂任务一次成功率明显抬升
三波	案例记忆 + 难度路由（第 4、6 条）	中	提智与降本同时发生
远期	phases 并行化 + 双跑验证 + 工具合成（第 2、5 条）	大	吞吐与可信度上限
共同的底层判断：你们系统现在的短板不是"模型不够聪明"，而是聪明的部分（planner/CodeAgent）之间缺少互相校验的机制——规划没人挑错、结果没人核数、失败只救当步不记后步。把校验环补上，同样的模型能榨出明显更高的任务完成质量，而且这些机制大多站在你们已有的 critic/evaluator/trace 基建上，不算外挂。

要我把其中哪条展开成具体设计（节点契约、数据结构、prompt 草稿）？

展开吧

好，我把先手 + 二波的五件事展开成可直接动工的设计（数据结构、节点契约、prompt 草稿），三波/远期的下轮再拆。顺序上评测集最先行，因为后面四件事的验收都挂在它上面。

一、评测集（Eval Harness）
用例数据结构
每条用例一个 YAML，放 qd_evals/ 目录，离线 runner 批量跑：

yaml
id: EVAL-023
level: L2                      # L0 查询 / L1 单段分析 / L2 多阶段 / L3 长链条
prompt: "分析东方财富3月12日大跌原因并评估当前风险"
gold_requirements:
  - {type: number, key: 跌幅, expect: -7.03, tol_pct: 0.5, source_hint: get_stock_daily.pct_chg}
  - {type: fact,   key: 龙虎榜席位, accept_any: ["机构净卖", "深股通净卖"]}
  - {type: section, key: missing_data, accept: "非空且列出影响结论的数据缺口"}
forbidden:
  - "使用2024-03-12之后的数据推断当日成因"   # 时间泄漏检测
  - "把资金净流出直接断言为'必跌'"
budget: {max_steps: 24, wall_clock_s: 600}
tags: [dragon_list, event_attribution]
id: EVAL-023
level: L2                      # L0 查询 / L1 单段分析 / L2 多阶段 / L3 长链条
prompt: "分析东方财富3月12日大跌原因并评估当前风险"
gold_requirements:
  - {type: number, key: 跌幅, expect: -7.03, tol_pct: 0.5, source_hint: get_stock_daily.pct_chg}
  - {type: fact,   key: 龙虎榜席位, accept_any: ["机构净卖", "深股通净卖"]}
  - {type: section, key: missing_data, accept: "非空且列出影响结论的数据缺口"}
forbidden:
  - "使用2024-03-12之后的数据推断当日成因"   # 时间泄漏检测
  - "把资金净流出直接断言为'必跌'"
budget: {max_steps: 24, wall_clock_s: 600}
tags: [dragon_list, event_attribution]
金标准来源：从 qd_traces 里 correct 标记的 run 反向提炼——当时结论里经过 T+N 验证站住的数字和论点，直接就是 gold_requirements。不用从零标注。

三层判分器

层	判什么	实现	成本
L1 确定性	数字命中（容差）、必备字段、禁用表述、时间泄漏、工具调用是否在 allowed_tools 内、hit_max_steps	解析 run 的 trace JSONL + 最终输出，正则/数值匹配	零
L2 LLM judge	覆盖度、逻辑质量（每维 0–5）、结论-证据对齐	固定 rubric prompt，temperature=0，judge 与被测模型不同源	每条一次调用
L3 统计	幻觉数字率、一次成功率、步数/token 分布、复权正确率	由 L1 结果聚合	零
Runner 与产出
伪并行跑全部用例，落 qd_eval_runs/{date}/results.jsonl，产出周报表：按 level 分层的通过率曲线 + 与上一版的 diff（新增回归标红）。
A/B 模式：run_eval --variant plan-linter-on，直接对比两组结果。之后你说"加了 Plan Linter 之后 L2 一次成功率多少"，一天内有答案，不用猜。
回归门槛可以先手工看，跑稳后挂到发布检查上（L0/L1 通过率不得下降 >2pp）。
二、数字溯源 Critic（verify_node）
Graph 上加一个节点：execute → verify → finalize | execute，route_after_execute 的分叉从两条变三条（现在是验收过 → finalize / 否则回 execute，变成再加 verify 汇入）。先只对含数字结论的 L1+ 任务启用，L0 查询不走，省调用。

生成侧强制：claims 结构
conclusion 的每条关键论断必须带溯源，这是 verify 能落地的前提。FinalAnswerSchema 里给 conclusion 段加结构化 claims（formatter 再渲染成人读的脚注）：

json
"claims": [
  {
    "text": "3月12日收盘下跌7.03%",
    "value": -7.03,
    "provenance": {"tool": "get_stock_daily", "field": "pct_chg", "row": "2024-03-12"},
    "verified_by_exec": true
  }
]
"claims": [
  {
    "text": "3月12日收盘下跌7.03%",
    "value": -7.03,
    "provenance": {"tool": "get_stock_daily", "field": "pct_chg", "row": "2024-03-12"},
    "verified_by_exec": true
  }
]
prompt 侧（FinalizeAnswer 单块契约里加一条）：

【溯源硬要求】conclusion 中每一个数字论断都必须填 claims 并标注来源工具与字段。无法定位来源的数字不得写入结论——宁可写进 missing_data 说"该数字无数据支撑"。

确定性核对（零成本，最先做）
python
def check_grounding(claims, trace) -> GroundingReport:
    unverified = []
    for c in claims:
        outputs = collect_tool_outputs(trace, tool=c.provenance["tool"])
        if not match_value(outputs, c.value, tol=0.005):
            unverified.append(c)
    return GroundingReport(rate=len(unverified)/len(claims), details=unverified)
def check_grounding(claims, trace) -> GroundingReport:
    unverified = []
    for c in claims:
        outputs = collect_tool_outputs(trace, tool=c.provenance["tool"])
        if not match_value(outputs, c.value, tol=0.005):
            unverified.append(c)
    return GroundingReport(rate=len(unverified)/len(claims), details=unverified)
match_value 要处理格式变体，这是实操里最容易误杀的地方：千分位/百分号换算、单位缩放（亿/万）、四舍五入容差（±0.5 个百分点）、复权口径同值不同号。每种变体写进正则白名单，误杀比漏杀更烦人——漏杀交给 LLM 层兜底。

LLM critic 检查 prompt 草稿
输入 = 任务 + claims + 相关工具输出片段 + 逻辑链条。只审不改：

你是研究结论审稿人。针对每条 claim 只回答三件事：

1.落地性：标注的数值能否在引用的工具输出中找到（含单位/百分比换算）？
2.逻辑性：从事实到结论是否有中间论据？有无方向与数据相反、或把相关性当因果？
3.越界性：结论是否超出了证据范围（如从历史资金流推导确定性预测、给出仓位指令）？
输出 JSON：{"pass": [...], "fail": [{"claim_id": ..., "issue": ..., "severity": "fatal|soft"}]}。不要重写结论。
三级判定与路由
PASS（fail 为空或仅 soft）→ finalize。
REPAIR_ONCE（有 fatal）→ 回 execute 一次，回炉 prompt 只带缺陷清单："审稿发现以下问题（见列表）。只修复列出的问题：补数据、改论断或降级为 missing_data。未列出的内容一字不改，防止修一处坏三处。"
DEGRADE（回炉后仍 fatal）→ finalize 但输出降级版：只保留核对通过的 claims，未通过的整段移入"未经数据核对的存疑项"，confidence 降一档，trace 记 verify_failed 供 evaluator 归因。
关键约束是回炉只许一次——verify-execute 死循环是这类节点最常见的坑，写死在路由里而不是靠提示约束。

三、Plan Linter
插在 _normalize_phases 之后、plan 入 state 之前，两层。

确定性规则层（先做这四条）
维护一张静态词典 data_domain → required_tools（"龙虎榜"→dragon_list_hall、"估值"→stock_valuation_indicators……），然后：

R1 工具覆盖：phase 的 acceptance 条目抽数据域关键词 → 查词典得必需工具 → 不在该 phase 或全局 tools 里 → fatal，缺什么直接列给 planner 回炉。这条防的是"阶段交付物要求的数据没有工具能取"这种必然断链的规划。
R2 依赖完整性：planner 输出加 depends_on: [phase_id]（normalize 时缺省 []）。检测到隐式依赖但未声明（B 的 acceptance 引用 A 的产物域）→ 自动补 barrier 标记并 WARN，不阻塞。这条同时是远期 phases 并行化的地基——依赖显式化之后，无依赖分支才能安全 fan-out。
R3 预算-粒度失配：单 phase step_budget 顶到 PHASE_MAX_STEPS 上限且 acceptance > 3 条 → WARN"粒度过粗"，回炉拆分一次；两 phase 各 ≤2 步且无 barrier → 建议合并。
R4 工具面裁剪：planner 声明的 tools 多于实际各 phase 引用并集 → 裁到并集。削减误用面，也降低幻觉调用概率。
fatal 走回炉（一次），WARN 记 trace 供事后统计。

LLM critic 层（plan 选优 + 打分）
best-of-N 场景下同一个 critic 兼做选优。prompt 草稿：

你是研究规划审稿人。给定任务、工具清单（只有这些可用，没有其他）和候选计划。只挑会导致执行失败或结论不可靠的缺陷：

1.哪一步会因数据缺失或工具不支持而断掉？
2.哪一步的验收标准实际无法判定？
3.步数与预算是否和任务规模失配？
输出：{"fatal": [...], "warnings": [...], "score": 0-10}。不要改写计划本身。
选优规则：fatal=0 者按 score 取最高；全有 fatal → 取 fatal 最少者带回炉。评分维度里放一条"最小工具面、最小步数"，防止模型喜欢写宏大计划。

四、失败记忆（run 内闭环）
数据结构
GraphState 加一个字段：

python
"failure_memory": [
  {"step_id": 3, "error_type": "hallucinated_tool",
   "detail": "get_daily 不存在，实际工具 get_stock_daily", "resolved": true},
  {"step_id": 5, "error_type": "empty_result",
   "detail": "龙虎榜查3月返回空，该月无上榜记录", "resolved": true}
]
"failure_memory": [
  {"step_id": 3, "error_type": "hallucinated_tool",
   "detail": "get_daily 不存在，实际工具 get_stock_daily", "resolved": true},
  {"step_id": 5, "error_type": "empty_result",
   "detail": "龙虎榜查3月返回空，该月无上榜记录", "resolved": true}
]
采集点
GuidedPythonExecutor 的异常分类器就是天然采集点——它已经能区分幻觉调用/沙箱不可用/表结构对不上（§7.11 待补的 column-shape 修复也归口到这里，一次分类多处受益）。分类器在改写修复的同一分支里 append 一条，DataRepairAgent 的每次 repair 也 append。顺手把 error_type 词表定死：hallucinated_tool / sandbox_unavailable / wrong_column / wrong_frequency / empty_result / self_check_failed——这也是评测集 L3 统计的口径来源。

注入
execute_node 组 prompt 时，failure_memory 非空才注入，上限 5 条、每条 ≤80 字（token 预算要抠）：

【本次任务已确认的失败事实】

step3: get_daily 不存在，实际工具为 get_stock_daily。不要重试。
step5: 龙虎榜 2024-03 无记录，该事实按"无上榜"处理，不要换工具反复查。
注入语义偏"禁止令 + 已确认事实"，不给修复方案——方案给多了模型会跳步。resloved=true 的条目仍保留（防止换个工具名再犯），但 detail 压缩到一行。

五、数据自检断言
提供预置校验函数，而不是让模型每次自己写 assert
沙箱里预置一个 validate_df（这同时是"复合工具合成"的第一个种子）：

python
def validate_df(df, expected_cols, date_window=None, max_nan_ratio=0.1):
    """检查失败抛 SelfCheckError(code, detail)，执行器捕获后路由。"""
def validate_df(df, expected_cols, date_window=None, max_nan_ratio=0.1):
    """检查失败抛 SelfCheckError(code, detail)，执行器捕获后路由。"""
单块契约（§7.12）追加一条
【数据自检（硬要求）】任何指标计算前，必须先对原始数据执行 validate_df(...)：
覆盖任务时间窗口、行数非零、关键列 NaN 率、价格变动量级 sanity check。
自检失败不得继续计算——抛出的 SelfCheckError 会带缺口信息返回，缺口写入 missing_data。

失败路由（新 error_type: self_check_failed）
软失败（缺日期、有 NaN）→ DataRepairAgent 修一次；修不好 → 记 missing_data，阶段验收可以放行（缺口显式声明比硬凑数字值钱），结论按缺口降置信度。
硬失败（价格量级离谱、频度错配）→ 判口径错误，回 execute 换口径重算，不许绕过。
这样阶段验收 _check_phase_acceptance 前面就多了一道确定性闸门：LLM 判"该做的都做了"之前，数据先得自己过关。

接线总览与验收
text
plan_node → normalize → [Plan Linter] → (fatal? 回炉一次) → execute
execute ←───────────── failure_memory 注入
   ↓（每步 validate_df 闸门 + 分类器采集失败）
verify_node → PASS → finalize
            → REPAIR_ONCE → execute（带缺陷清单）
            → DEGRADE → finalize（降级输出）
plan_node → normalize → [Plan Linter] → (fatal? 回炉一次) → execute
execute ←───────────── failure_memory 注入
   ↓（每步 validate_df 闸门 + 分类器采集失败）
verify_node → PASS → finalize
            → REPAIR_ONCE → execute（带缺陷清单）
            → DEGRADE → finalize（降级输出）
对着评测集的验收目标，二波做完后应看到：


指标	现状口径	目标方向
L2 一次成功率（不走回炉）	基线待测	明显抬升
幻觉数字率（落地性核对失败数/总 claims）	基线待测	趋近 0
hit_max_steps 率	基线待测	下降（规划断链提前拦住）
同一错误重复出现次数	基线待测	每 run ≤1
"现状待测"不是敷衍——评测集没跑起来之前这些基线是真不知道，这也是把它排最前面的原因。


把剩下的三波和远期一次拆完，还是老规矩：数据结构、节点契约、prompt 草稿。

三波（一）：案例记忆（Case-Based Reasoning）
案例数据结构
每次 run 的 finalize 之后，由后置钩子落一条案例到 qd_cases/（JSONL + 向量索引）：

json
{
  "case_id": "CASE-20240318-0042",
  "task_summary": "东方财富3月12日大跌归因+风险评估",
  "task_embedding": ["..."],
  "level": "L2",
  "tags": ["dragon_list", "event_attribution"],
  "plan_digest": [
    {"purpose": "取行情定位跌幅", "tools": ["get_stock_daily"], "steps_used": 3},
    {"purpose": "龙虎榜席位归因", "tools": ["dragon_list_hall"], "steps_used": 5}
  ],
  "outcome": {"label": "pending", "t_plus_n": null, "confidence": 0.85},
  "failure_modes": [],
  "cost": {"steps": 11, "tokens": 82000, "wall_clock_s": 310}
}
{
  "case_id": "CASE-20240318-0042",
  "task_summary": "东方财富3月12日大跌归因+风险评估",
  "task_embedding": ["..."],
  "level": "L2",
  "tags": ["dragon_list", "event_attribution"],
  "plan_digest": [
    {"purpose": "取行情定位跌幅", "tools": ["get_stock_daily"], "steps_used": 3},
    {"purpose": "龙虎榜席位归因", "tools": ["dragon_list_hall"], "steps_used": 5}
  ],
  "outcome": {"label": "pending", "t_plus_n": null, "confidence": 0.85},
  "failure_modes": [],
  "cost": {"steps": 11, "tokens": 82000, "wall_clock_s": 310}
}
一个关键设计点：案例质量标签是延迟的。outcome.label 落盘时是 pending，等 evaluator 的 T+N 回测校验回来才回填 correct / incorrect。所以检索时按"label != incorrect 加权、pending 降权、correct 权重 × 正确间隔系数"处理，不能假定存进去的案例就是对的——不然错误结论会以 few-shot 的形式自我复制，这比没记忆更危险。

检索与注入（plan_node 内）
检索条件：task_embedding 相似度 + tags 交集，取 top-3，相似度低于阈值就一条都不注（宁缺毋滥），近重复案例（相似度 > 0.95）只留最新且已验证的一条。注入 planner prompt：

【历史相似任务——仅作参考，不要照搬结构】

任务"…": 工具链 get_stock_daily → dragon_list_hall，分两阶段，T+3 校验正确。坑：龙虎榜需单独取数，行情工具不含席位。
任务"…": 校验错误。失败原因：把资金流净流出直接推为"必跌"，缺中间论据。
注入语义严格分两类：成功案例给骨架候选，失败案例只给坑（failure_modes 字段就是给这里的）。失败案例绝不附带"当时怎么修的"，防止模型抄修复路径而不理解任务差异。

与技能酿造的关系（补上粒度空档）

案例记忆	技能 SKILL.md
粒度	单次 run	稳定模式
门槛	无，run 完即存	两次验证正确才候选
注入点	plan 时 top-3	task 常驻 prompt
淘汰	近重复去重 + incorrect 屏蔽	酿造权重回灌
案例库反过来给 §3.16 的酿造通道①/②当早期观察窗口：统计 plan_digest 聚类，同一工具链模式出现 ≥3 次且 correct 率高 → 生成酿造候选建议。以前要等技能统计跑起来才发现的高频模式，现在案例库攒两周就能看出来。

三波（二）：难度路由
分级判定放在 intent 分类阶段
router/intent_parser 顺带输出 difficulty: L0-L3，规则特征先算，模型只判规则拿不准的：

确定性特征（不花调用）：预期工具数（涉及几个数据域关键词）、是否含时间序列/回测/多空词汇、是否多标的（≥3 个代码或"对比/组合"）、是否要求报告格式。四个特征各占权重，得分直接映射 L0-L2。
临界带（规则得分在 L1/L2 边界 ±10%）才让廉价模型判一次，prompt 里给判据清单 + 两个 few-shot："判断完成此任务最少需要几步工具编排：纯查询 L0、单段计算 L1、跨数据域多阶段 L2、长链条研究（含回测/多标的/长窗口）L3。"
路由错误必须可回退——升级便宜，猜错不怕：

路由矩阵

模型	管线	verify	plan linter	best-of-N	双跑
L0	小模型	单段	确定性核对即可	关	关	关
L1	小模型	单段	确定性核对 + 可选 critic	确定性层	关	关
L2	强模型	多阶段	全开	全开	关	关
L3	强模型	多阶段	全开	全开	开	开（敏感阶段）
升降级信号（trace 记录，供评测复盘）
升级（L0/L1 → L2）：单段内工具调用数超阈值、validate_df 报跨数据域缺口、模型自报 need_replan。触发即丢弃单段结果从头 plan——单段本来就便宜，沉没成本不心疼。
降级（L2 → L1）：前两阶段的 acceptance 全是查询类、无计算依赖 → 合并快跑。这个方向可以缓做，收益小一半。
成本账：L0/L1 占日常流量的大头（估 60–70%），降级到小模型省的调用费，基本正好覆盖 verify + critic 加的调用——提智不涨总成本的前提就在这条。而且评测集分层之后，路由质量本身有指标：升级率（初始分级偏低的比例）应压在 10% 以下。

远期（三）：phases 并行化
地基是二波 R2 的 depends_on——依赖没显式化之前不要动这个。

执行模型
Graph 保持顺序骨架不动，并行发生在 execute_node 内部：每个 tick 扫描 phases，凡 depends_on 全部完成的 phase 进入就绪集，就绪集通过 asyncio 并发执行（每 phase 一个受控子执行，挂 AsyncIO + 现成线程池基建）。这比把 StateGraph 改成动态 DAG 侵入小得多，route_after_execute 语义不变。

三条硬约束：

1.产物隔离：各分支只写自己的 phase artifacts 字段，不共享可变状态。你们"隔离复用语义(3)"已经立了规矩——计划可以借，数据和步骤绝不共用——正好是并行安全的前提。
2.失败隔离：单分支失败标 phase_failed，兄弟分支照跑；barrier 汇合处再决定补跑该分支还是降级输出。不能一个分支异常掀翻整个 plan。
3.只并行确定性阶段：取数、指标计算并行没问题；解释类阶段串行——两个 LLM 分支并行写归因容易口径漂移（一个说"机构卖出主导"、一个说"情绪主导"），汇合时打架，反而要花 reconcile 成本。
目标场景
compare 类任务是天然 fan-out："对比 5 只券商股"→ 5 个并行取数+指标分支，汇总阶段 barrier 收口。现在串行跑，墙钟和 token 都是线性浪费；并行后墙钟接近单标的耗时。评测集里 tag 为 compare 的用例就是这条的验收对象（指标：墙钟降幅、结果一致性不劣化）。

远期（四）：双跑交叉验证
触发（选择性是成本可控的关键）
critic 在 verify_node 判定"结论敏感"（选股结果、排名、阈值附近指标）；
或 claims 里数字对容差敏感（涨跌幅在 ±0.5pp 边界、估值分位在阈值两侧）。
只双跑指标/选股 phase，解释 phase 不跑。

比对与仲裁
第二跑独立（temperature 非零 + 不同 seed，可换小模型），diff 规则：


diff 情况	处理
数字差 ≤ 容差、claim 集合一致	直接采信，confidence 升一档，trace 记 cross_verified
单点数字差 > 容差	第三方仲裁调用（小模型对着两份工具输出重算一次）或标 uncertain 写入 missing_data
结论方向相反	触发 REPAIR 路径，输出降级为"两个口径下的结论并列 + 分歧原因"
最后一种情况别硬选一个——金融数据口径（复权、停牌）就是会算出两个都"对"的数，把分歧摆出来比赌一个强。

远期（五）：复合工具合成
触发与验证
案例库攒起来后，对 plan_digest 做模式聚类：同一"取数→清洗→指标"片段在 ≥3 个 run 中出现且 label=correct → 生成合成候选。critic 验证一遍（对历史输入重放，输出与历史结果一致）才固化。

固化形态
包成 _SkillFuncTool（你们现成机制），函数内置 validate_df 自检、签名和 docstring 写清数据口径（复权方式、频率）：

python
def get_volatility_profile(code, window, freq="daily") -> pd.DataFrame:
    """取行情并计算波动率画像。前复权，日频。内置数据自检。"""
def get_volatility_profile(code, window, freq="daily") -> pd.DataFrame:
    """取行情并计算波动率画像。前复权，日频。内置数据自检。"""
存用户级（个人常用标的池加工）或全局（通用加工链）。三条风控：

1.合成工具要登记进 linter 的 data_domain → tools 词典，规划期可见；
2.每次合成后自动跑 tag 匹配的评测用例（回归护栏）；
3.与技能的关系理顺：函数是可执行技能，SKILL.md 是叙述技能——高频稳定的合成函数本身可以成为酿造的产物形态之一，酿造权重回灌机制照用。
这条的本质是把"模型每次现写 30 行取数清洗"变成"调一个验证过的函数"，省步数、省 token、降出错面，三个收益同时发生。

远期（六）：长任务异步化
判定在 plan 期：planner 预估总步数 × 单步耗时 > 墙钟预算的 80% → 直接规划成异步任务，不硬跑（现在的 stop_reason=timeout 砍半截是纯浪费）。
挂起与唤醒：execute 超时前落 checkpoint（语义现成），state 标 pending；route_after_execute 加一条 pending 分支走 message_queue 回调 + cron 唤醒续跑。message_queue 和 cron 基建都是现成的，改造集中在唤醒后的状态恢复测试。
交互：前端回答"任务复杂度超出实时预算，已转后台，预计 N 分钟后回传"，完成后 message_queue 推送结果 + 完整报告链接。用户体验上这不是降级——宁可晚点给完整结论，不要 900 秒砍半截。
全景收口

波次	条目	新增节点/字段	评测集验收指标
先手	评测集 + 数字溯源 critic	verify_node、claims	幻觉数字率趋近 0，基线可测
二波	Plan Linter、失败记忆、自检断言	plan 校验层、failure_memory、validate_df	L2 一次成功率、hit_max_steps 率、同错误重复 ≤1
三波	案例记忆、难度路由	qd_cases/、difficulty 字段	升级率 <10%，token 成本不升
远期	并行化、双跑、工具合成、异步化	depends_on fan-out、合成工具注册	compare 墙钟降幅、cross_verified 覆盖率
几个反复出现的设计原则，后面执行时拿它们对齐分歧就行：

1.校验环尽量确定性——LLM critic 只兜确定性查不了的（逻辑、越界），数字核对、数据质量、工具覆盖全走规则，零成本且不引入新的幻觉源；
2.回炉只许一次——verify、linter、repair 全部单次回炉，防死循环烧预算；
3.缺口显式化优于硬凑——missing_data 是一等交付物，评测集里也按必备字段验；
4.记忆必须带延迟标签回填——案例、技能、合成工具，没验证过的降权使用，防止错误自我复制；
5.每条改造都先想清它在评测集上看哪个数——想不出来的，就还没想清楚要不要做。
真到动工顺序，我的建议是：评测集 runner + 确定性数字核对（check_grounding）最先，两者加起来一两天量级，之后每个改动当天就能看到指标变化。要我先把 runner 的目录结构和 check_grounding 的代码骨架写出来吗