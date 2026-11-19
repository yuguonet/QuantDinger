# T+N 回评改进方案

> 文档来源：Copilot SDK in VS Code 会话分析  
> 任务：评估现有 T+N 回评是否形成有效学习闭环，并提出改进方向  
> 署名：Copilot  
> 时间：2026-11-18  
> 状态：方案草案，尚未修改生产代码

## 1. 结论

当前 T+N 回评更像“审计账本”，还不是完整的“学习回路”：

- 可以记录预测是否命中；
- 可以统计方向、幅度和点位偏差；
- 可以慢调部分权重；
- 但难以证明下一次决策因此获得真实改善。

本方案的明确目的：

> **增强 T+N 回评有效性，形成“经验获取 → 有效性评估 → 经验应用 → 结果回流 → 迭代验证”的学习回路，提高指定视界和适用情境下的预测成功率。**

预测成功率是主目标，必须按 horizon、情境和有效样本口径衡量；概率校准用于衡量置信度是否可信。若建议进一步进入交易或执行流程，净收益、超额收益和回撤作为应用层结果与风险护栏，不能用收益指标替代预测质量，也不能只凭方向命中率宣称系统有效。

### 1.1 实施范围与边界

- 生产代码修改范围仅限 `backend_api_python/app/agent/`。
- **不得修改** `backend_api_python/app/agent/skills/` 和 `backend_api_python/app/agent/tools/` 中的文件；现有 skill、tool 内容和行为保持不变。
- **不得修改** `backend_api_python/app/agent/` 以外的文件，包括迁移、数据库目录、其他 backend 模块和前端。
- 可以在 `app/agent/` 内调用或导入范围外已有接口；调用不代表可以修改其实现。若现有接口不足，应在 `app/agent/` 内通过适配、编排或新增本地模块完成。
- 经验不应通过改写或复制进各个 skill 来维护。应从现有 skill 的只读内容、skill 执行结果、agent 会话、工具结果及成熟的 T+N 回评中提取候选经验，统一记录在 `app/agent/` 所属的经验层，并由多个 skill/分析链路共享检索和使用。
- 从来源提取出的经验是候选知识，不等同于真值；必须保留来源、适用条件和验证状态，不得将未经回评的模型总结直接标为有效经验。

以上是本方案后续设计和实施的硬边界；若必须修改这些范围以外的文件才能完成，先调整方案并取得用户确认。

## 2. 当前链路

当前实际上有两条相关但不能混作一份标签的回评路径：

```text
最终回答
  ├→ 新 claim 路径：
  │    record_decision → decisions / claims
  │      → T+N resolver → qd_agent_resolutions
  │      ├→ weight_feed → qd_agent_weights → weight_hints
  │      └→ calib_feed（pairs × resolutions）→ calibration → p_final
  └→ 旧 trace 路径：
       qd_agent_traces root → evaluator.evaluate_pending
         → root/skill correct、pnl_pct
         ├→ qd_skill_selection_events 标签
         └→ case_memory.backfill_by_root
```

两条路径的对象和标签语义不同：claim 是按 horizon 的具体预测声明；旧 trace root 的 `correct` 是旧链的决策级标签。`skill_events` 当前接收旧 trace 路径标签，案例库也由 root 回填。新经验回路必须明确唯一 outcome 真源及其 join 键，不能把 root、claim、skill event 的标签混为一类或重复计票。建议以新 claim/resolution 作为有明确预测对象和 horizon 的主预测 outcome；旧 trace outcome 仅作独立辅助指标，只有显式映射和可验证时才参与经验效果估计。

已落地的能力包括：

1. 按交易日计算成熟期；
2. 方向、幅度、点位等规则判定；
3. 一 claim 一票去重；
4. `undecidable` 不参与权重；
5. domain 权重慢调；
6. 条件校准和独立簇折算；
7. 案例标签延迟回填。

这些能力解决了大量口径和接线问题，但仍未形成足够强的行为闭环。

## 3. 当前作用有限的原因

### 3.1 聚合到 skill 仍不足以评价共享经验的效果

`weight_feed` 将 resolutions 聚合到 domain 层。

A 股分析几乎全部属于 `finance`，最终只能得到类似：

```text
finance 最近命中率偏高
finance 最近命中率偏低
```

domain 汇总无法区分：

- 哪个 skill 有效；
- 哪组工具或工具链组合真正带来增益；
- 哪种市场状态适用；
- 哪类 horizon 更稳定；
- 问题来自数据、推理、工具还是执行。

而仅把粒度改成 skill 仍不足以解决核心问题：现有 skill、会话和回评中可能包含许多可复用经验，有些有效，有些无效，甚至在特定情境中起反作用。skill 的总体效果可能把这些相反效果平均掉，无法指导“哪条经验该在何种条件下被检索和采用”。

这里的改进不是改造或重写现有 skill，也不是把维护责任下放到各个 skill。目标是只读获取现有 skill/会话中的经验，形成一个集中、版本化、可供多个 skill 共用的经验目录，并把每条经验的展示记录与消费请求的成熟结果关联。

仓库已有 `skill_events`，可跟踪技能候选、选中、注入、执行和成熟标签；但标签由旧 `qd_agent_traces` root/skill 回评接入，不能直接视为新 `qd_agent_resolutions` 中某个 claim/horizon 的标签。案例记忆 `qd_agent_cases` 会记录案例并通过 root 回填原案例标签；当前 prefetch 只留“命中多少条案例、总字符数”等聚合信息，没有把每个 `case_id` 与消费请求的 decision/outcome 关联。因此不能据此判断某一条被复用的经验是否帮助了新请求。新经验回路必须显式定义 claim outcome 到消费请求/曝光事件的映射，保留 legacy 标签作为独立口径，不能无损假设两者等价。

domain 层适合作总览；skill/version 适合作来源或应用链路维度；**统一经验项及其适用条件**才是需要记录、评估和迭代的对象。三者不能互相替代。

### 3.2 经验注入没有可评价的逐项留痕

LLM 分析时可使用的经验来源至少包括：

- 现有 skill 中的方法论、规则、禁忌和输出示例（只读获取，不修改 skill 文件）；
- 现有 skill 执行过程产生的经验；
- agent 会话中的分析过程、用户反馈和复盘结果；
- 历史案例及其判别特征、失败坑、工具链骨架；
- RAG 参考资料、文档片段或历史记录；
- C3P 挖掘出的判别特征、证伪条件；
- 权重提示、预选提示和其他运行策略。

这些来源进入统一经验层后，skill 只是经验的来源或使用者，不是经验存放和维护的边界。多个 skill 可以检索相同经验，但是否适用、效果如何，应按经验自身的适用条件和结果统计判断。

如果只记 `skill_id` 或“case 注入了 3 条”，就无法回答：

- 实际注入的是哪条经验、哪个版本；
- 它是否进入送给模型的最终消息，位于什么位置、占用多少 token；
- 同一条经验是否被重复注入或与其他经验冲突；
- 哪些结果对应单条经验，哪些只对应整个经验组合；
- 经验是没有被检索、未被展示、展示后未被引用，还是引用后结论变差。

“被注入”只表示模型看得到，不表示模型采纳；模型提到某条经验也不代表它导致结果变好。两者都不能直接当成因果证据。

### 3.3 反馈没有直接改变行为

domain/tool/chain 权重主要通过 `weight_hints` 注入预选提示。

当前策略是：

```text
只提示，不过滤；
只提示，不过强制重排。
```

模型可以忽略历史表现提示，因此 T+N 结果对下一次行为没有确定性影响。

技能排序又已经移除旧权重链，单独使用 `skill_events`。T+N 的 domain 回评与 skill 反馈并不是一条统一、可归因的学习链。

### 3.4 C3P 的 p_final 不参与当前回答

C3P 在最终回答收尾后执行。

因此 `p_final` 当前只用于：

- 记录 pair；
- 后续拟合校准；
- 观察概率偏移。

它不会纠正当前已经输出的判断，也没有进入正式的交易决策接口。

在 `p_final` 达到可用样本前，通常退回 `p_base`，这是正确的 fail-open 行为，但也说明 C3P 目前以数据积累为主，不是即时决策优化。

### 3.5 方向正确不等于策略有效

当前规则判定的主要对象是涨跌方向。

例如预测看多，实际上涨 0.31%，可能被记为 hit；但它可能：

- 低于手续费和滑点；
- 跑输基准指数；
- 达到止损后才回升；
- 具有不可接受的最大回撤；
- 无法以模型假设的价格执行。

因此方向命中率不能作为交易系统收益/风险表现的主指标；它仍可作为预测层中明确方向预测的质量指标，必须与交易执行结果分层报告。

### 3.6 缺少对照组

当前体系难以回答：

- 不使用某 skill 是否效果更差；
- 使用旧 prompt 是否更好；
- 工具链增加的数据是否有增量价值；
- 收益来自模型判断还是市场普涨；
- 额外成本是否换来足够收益；
- 回评权重是否真的优于关闭权重。

没有对照组，只能形成描述性统计，无法建立因果增益。

### 3.7 评估对象没有冻结执行契约

现有记录偏重预测结论，缺少完整、冻结的决策快照。

至少需要保存：

- 预测产生时的数据截止时间；
- 使用的模型、prompt、skill 和能力版本；
- 预期概率、方向、价格区间和 horizon；
- 工具链和工具返回摘要；
- 入场、止损、目标价的执行口径；
- 样本层、condition key 和市场状态；
- 是否允许执行或仅为研究结论。

缺少这些信息时，T+N 结果无法稳定归因，也无法回放。

## 4. 改进目标

改造后的 T+N 回评应具备四个能力：

1. **可回放**：相同决策快照及其实际使用的经验包可以离线重放；
2. **可归因**：结果能追溯到预测、经验项、来源版本、适用条件、工具链和 horizon；
3. **可共享、可应用**：不同 skill 能从统一经验层检索同一条经过验证且适用的经验；
4. **可迭代**：经验权重、胜率、样本量和不确定性可更新，新的结果能回流到同一经验项。

**增量验证不是第五种系统能力，而是启用回路的验收门槛：**只有受控对照证明预测质量改善，且交易应用风险满足预注册护栏，才允许从 shadow 升级为 active。

## 5. 评估对象改造

### 5.1 冻结 Decision Snapshot

每次决策生成后保存一次不可变快照：

```text
decision_id
snapshot_version
created_at
as_of
data_as_of
model_name
prompt_sha
skill_id
skill_version
experience_bundle_id
experience_bundle_sha
experience_injection_ids
experience_snapshot_at
tool_chain
query_type
subject
subject_kind
horizon
prediction_target
label_rule_version
forecast_capture_status
probability_source
probability_target
probability_horizon
sample_layer
condition_key
predicted_direction
predicted_probability
entry_rule
entry_price
stop_rule
stop_price
target_rule
target_price
is_actionable
```

生成后禁止覆盖；需要修正时应新增版本记录。

`experience_bundle_sha` 应覆盖最终发给模型的经验内容、版本和顺序；敏感或冗长内容不必重复存储，可保存不可变来源引用与内容 hash，但必须保证能复现当时实际注入的文本。

经验项按来源抽取后写入统一经验目录；在请求快照中记录实际命中的 `experience_id`、版本、适用性、权重快照和最终 bundle。不能只存来源 skill 名称，否则无法识别跨 skill 共用的同一经验。

### 5.2 定义真实 Outcome

T+N 到期后记录：

```text
exit_date
entry_fill
exit_fill
gross_return
commission
slippage
net_return
benchmark_return
excess_return
max_drawdown
max_favorable_excursion
max_adverse_excursion
stop_hit
target_hit
execution_status
data_quality
```

**预测实体唯一键冻结为** `(decision_id, claim_id, subject, prediction_target, horizon)`，命名为 `prediction_id`。`label_rule_version` 不属于预测身份，因为它描述的是如何评分，不是模型当时预测了什么。

回评结果另设 `outcome_id`，以 `(prediction_id, outcome_policy_version)` 幂等唯一；`outcome_policy_version` 固定包含 `label_rule_version`、resolver/数据口径版本。改变评分规则时保留新旧 outcome 版本，并显式指定当前 canonical 版本，不能覆盖或重复累计同一预测。记录 `resolution_id`、成熟时间和数据截止时间。

`entry_fill`、`exit_fill`、滑点和手续费必须使用统一、可配置的口径；执行字段是应用层 outcome，不应成为预测标签的前置条件。

#### 5.2.1 多视界（T+1 / T+3 / T+5）记录口径

T+N 回评是事后对已给出建议的评估：真值源为到期后的真实行情，按预测成立时点不可更改。但“真值源可靠”不等于“每条样本都可判定”，也不等于“所有视界都进判定”。多视界必须按以下口径分开记录，避免重复计票或静默丢失：

1. **claim 层分档**：同一回答若给出多个视界的方向（如“T+1 看多 / T+3 中性 / T+5 看空”），每个视界各立一条 claim，携带各自的 `horizon`、到期日与 resolution；中性/未明示的档位不产出 claim。因此 T+1 / T+3 / T+5 在 claim 层是分条存储的；
2. **主窗口唯一进 verdict**：一条 claim 的 `prediction_verdict` / `verdict_score` 及权重只由其自身 `horizon`（主窗口）决定。额外观测视界（通过配置 `QD_LABEL_WINDOWS`，默认 `T+1,T+3`）只挂在 `actual.windows` / `deviation.windows` 供快速诊断（这两个字段已列入 §10 的 outcome schema，作为诊断子结构），**不进 verdict、不进权重**——否则同一预测会被记多票；
3. **T+5 成为权威结果的条件**：T+5 若要作为可评分的主结果，必须是某条 claim 自身的 `horizon`；仅把它加入额外观测配置只够诊断，不能进 verdict。方案在定义 outcome 时必须为每个主视界显式确认其 claim 来源，不得把附加窗口当作独立预测；
4. **不可判定态单列**：截断（数据不足、窗口被截短）、不可判定（落在 flat 区间，方向不可分）、数据缺失（如板块指数不支持取数）不得当作真值或 miss，记入 coverage/exclusion 并注明原因，与 §5.3 的处理一致。

统一经验层在做多视界归因时，必须沿用上述口径：一条经验在一个主视界上贡献一票，附加视界只作诊断证据，不得把同一预测的不同视界拆成多条独立样本进入成功率或 Brier 统计。

### 5.3 分离三类评估结果

不要只保留单一 `correct`。

建议分成三类，并分别判定纳入条件：

```text
prediction_verdict   # 方向、幅度、点位是否命中
execution_outcome    # 假设执行后的净收益和风险
learning_eligibility # 是否具备进入校准或反馈的数据质量
```

**有效、已成熟、目标和 horizon 明确的预测，即使不可执行，仍可纳入预测成功率/校准评估；**“不可执行”只意味着不纳入实际/假设执行收益统计。没有明确预测、预测解析失败、数据缺失或标签未成熟应分别记为 coverage/exclusion 状态，不能把它们静默删除，也不能统统当成 miss。

在写入快照和计算指标前，必须运行预测契约校验（在 `app/agent/` 内实现并版本化）：

1. `prediction_target`、`subject`、`horizon`、`as_of` 与标签规则必须齐全且一致；
2. 概率存在时，概率值必须有限且在 `[0,1]`；`probability_target == prediction_target`、`probability_horizon == horizon`，并校验 `probability_source` 对应的实际模型口径；
3. 任一概率契约不匹配（例如 T+1 `p_base` 配 T+3 claim）时，**该预测仍可参与符合条件的方向指标**，但 Brier 记 N/A，且 `coverage_gap` 必须记录 `probability_target_mismatch`（含来源/视界）；不得换用别的概率或静默剔除；
4. 概率缺失时，Brier 记 N/A、`coverage_gap=probability_missing`；自然语言 confidence 不得自动填成概率；
5. `probability_source` 只取两个枚举值：`calibrated`（标定概率，如 predict_next_day 的 `p_up`，唯一允许计算 Brier 的来源）与 `heuristic_confidence`（结构化把握度，如 stock-analyzer 各档 confidence，只参与 coverage 与分层观测、不进 Brier）；其余取值视为未知来源（记 `unknown_p_source`、概率作废），不得参与任何概率指标。

### 5.4 建立统一经验目录并评价每条经验

#### 5.4.1 经验项身份与类型

在 `app/agent/` 范围内建立统一经验目录（逻辑名示例 `qd_agent_experiences`；具体存储可复用现有接口或在允许范围内实现），为每条可注入经验分配稳定的 `experience_id`。目录保存候选经验和状态，不修改其原始来源；来源引用用于追溯，标准化经验文本用于跨 skill 共享。

经验项至少包含：

```text
experience_id
experience_version
experience_type
normalized_content
content_sha
source_kind
source_ref
source_version
source_span_or_event
applicability_conditions
source_available_at
registered_at
created_at
evidence_status          # 证据成熟度：candidate / validated / retired
consumption_mode         # 行为档位：observe / shadow / active（与证据成熟度正交）
```

来源类型包括：

| 来源类型 | 可追溯身份示例 | 版本/冻结内容 |
|---|---|---|
| 历史案例 | `case_id` | 原案例 outcome 标签、证据版本、案例文本 hash |
| 现有 skill 内容 | `skill_id + section_id` | skill version、段落/内容 hash（只读，不修改源文件） |
| skill/agent 会话 | `trace_root_id + message/span` | 会话/trace 版本、经验片段 hash |
| RAG 资料 | `document_id + chunk_id` | 文档版本、chunk hash、来源时间 |
| 判别特征/证伪规则 | `feature_key` / `falsifier_key` | 规则版本、可计算定义、适用条件 |
| 反馈策略/权重提示 | `policy_id + rule_id` | policy version、参数 hash |

来源经验不能稳定定位和版本化时，可以作为临时上下文供分析，但不得进入“单条经验效果”统计，其 `evidence_status` 不得升为 `validated`，`consumption_mode` 也不得放开到 `shadow`/`active`。

候选提取与发布必须分开：只读来源片段并保留原始引用 → 规范化成一个可检验的“条件—判断/做法—适用边界—证伪条件”经验项 → 去重并检查冲突/敏感信息 → `evidence_status=candidate` → 满足**入库质检**后标为 `validated` → 证据被推翻或失效时转 `retired`。提取 LLM 的总结本身不构成验证证据；不确定、冲突或无法脱离原上下文的内容应保持候选状态。

此处 `validated` 是**经验项自身的入库质检档**，判据为"来源可追溯 + 质检门通过 + 抽检通过"，**不要求 §9 的受控实验或 δ 门槛**——那是 `consumption_mode` 从 `observe` 升 `shadow`/`active` 才需要的（§8.2、§9）。把质检与效果验证分开，是为了不让"候选能否入库"也绕回"必须先做实验"的循环。

入库质检的两项关键判据必须机械化，不接受 LLM 自证：

- **引用校验机械化**：`source_ref` + `source_span_or_event`（§5.4.1 字段）必须能定位回该来源类型表中的身份与版本，且 `content_sha` 与该 span 覆盖的原文内容对得上。span 定位失败、hash 不符，或仅有模型自述"引用了某来源"的，一律维持 `evidence_status=candidate` 并降级，不得靠 LLM 自证引用晋升 `validated`。
- **证伪条件可观察**：四段字段（§8.4① 的 `condition`/`judgement`/`boundary`/`falsifier`）非空的同时，`falsifier` 必须含可观察事件——落成与 `condition` 同构的 `(key, op, value)` 谓词（key 同样受 §8.4① 白名单约束）或绑定可枚举的可观测事件字段（价格 / 日期 / 形态须有确定计算口径）。"若判断错则证伪"这类空话式证伪不算通过，降级为 `candidate` 并记原因；否则该经验永远无法被 §8.4④ 的失败归因触发改写。

`evidence_status` 只描述经验自身的证据成熟度，与 §8.2 的 `consumption_mode`（行为档位）是两个正交维度，不共用取值、不在同一字段流转。二者的对应规则见 §8.2 末的状态对应表；“升级为 active”在本文档中一律指 `consumption_mode` 的变化，不以它替代证据成熟度判定。

经验的 `source_available_at` 和 `registered_at` 必须早于被评估决策的 `created_at`。经验包只可包含该决策时点已经可用的经验版本及成熟结果，禁止把之后的 T+N 标签、后来修订的 skill 文本或未来 RAG 内容回灌到历史预测中。

若历史时点没有可重建的经验快照，就不能用今天抽取出的经验回算并声称它改善了过去的预测；该经验只能从登记/可用时点之后进入前瞻性评估。会话来源在进入全局共享经验层前必须去除凭据、个人信息和无关上下文，只保存最小化、可复用的经验片段与必要来源引用。

#### 5.4.2 经验有效性、权重和胜率

经验项的“有效性”是根据**使用该经验的后续请求**累积评估，而不是由来源案例自身的正确标签直接决定。目录/统计视图需按经验项记录：

```text
eligible_exposures
actual_injections
matured_outcomes
prediction_eligible_outcomes
execution_eligible_outcomes
excluded_outcomes + exclusion_reasons
success_count / failure_count / partial_count
prediction_success_rate
brier_score / calibration_error
applicability_conditions
confidence_interval
effective_weight
weight_policy_version
last_matured_at
```

`effective_weight` 表示该经验在检索/排序中的**证据强度**，不是预测概率、不是交易仓位，也不是“LLM 必须服从”的指令。它应由成熟、可归因的消费请求结果计算，采用样本门槛、收缩/不确定性惩罚和范围限制；样本不足、来源不明或适用条件不匹配时保持中性/候选状态。其计算公式、有效样本门槛和更新频率必须在试验前预注册并版本化，不得看到结果后临时调整口径。

LLM 接收的是可读经验及其适用条件、样本量、胜率/预测质量、可信度和限制；由 LLM 结合当前证据判断是否采用。量化模块独立计算基线概率、回测统计和风险指标，不把文字经验的权重直接混入量化概率。文本经验和量化判断可以相互校验，但必须分别留痕、分别评估。

`claim.confidence` 或模型文字中的“较有把握”不自动等同于校准概率。只有 `predicted_probability` 与明确的 `prediction_target`、`subject`、`horizon` 和标签定义一一对应且记录来源时，才计算 Brier/calibration；例如现有 `p_base` 是 `predict_next_day` 的 T+1 上涨概率，不能拿 T+3 claim 的结果给它打分。缺少合格概率时仍可评估有效方向预测，但 Brier 记为不可用并报告覆盖率，不造概率。

#### 5.4.3 逐项记录曝光漏斗

建议新增 `qd_agent_experience_injection_events`（名称可在实施时与现有 schema 统一）。每行表示“一个请求中的一个统一经验项”，关联 request、decision 和 outcome：

```text
event_id
request_id
decision_id
trace_root_id
experience_id
source_kind
source_ref
source_version
content_sha
eligible
retrieved
exposed
injected
excluded_reason
retrieval_score
selection_reason
position
chars
tokens
bundle_sha
experiment_arm
assignment_probability
model_referenced
reference_evidence
assigned_at
experience_snapshot_at
created_at
```

漏斗状态要分开报告：

```text
eligible_not_retrieved
retrieved_not_selected
selected_not_injected
injected_not_in_context
in_context_not_observed_as_referenced
referenced_or_paraphrased
outcome_pending
outcome_matured
excluded_from_learning
```

其中 `model_referenced` 与 `reference_evidence` 仅作为弱观测信号；无法直接观测模型内部是否真正使用了某条经验。记录最终上下文中的 item ID、顺序、字符/token 数和 bundle hash，才能识别位置偏差、长度效应和内容漂移。

一个请求可以注入多条经验并产生多个 claims/outcomes，一条经验也可在多个请求中复用。须通过单独的 link 关系（例如 `experience_event_id × outcome_id`）表达多对多关联，并带上 `experiment_arm`、`assignment_probability`、`eligible_for_attribution` 和排除原因；禁止把一个 decision 的全部结果无条件复制成每条经验各自的一票。

#### 5.4.4 来源结果与复用效果分开统计

`case_memory.backfill_by_root` 将本次结果回填到本次生成的案例；这回答的是“该案例对应的原决策后来怎样”，不是“该案例被用于另一请求时是否改善了结果”。

要评价案例作为提示经验的效果，必须把**消费它的请求**与**被注入的 case_id**关联，再连接消费请求自己的 T+N outcome。禁止把案例自己的标签直接当作它对后续请求的效果标签。

同理：

- 案例 `correct` 只说明原案例的原始结论在原口径下判定正确；
- `pending` 不是失败样本；
- `incorrect` 也不必然表示案例里的所有事实都错；
- 案例中某个工具链出现过，不证明它对之后结果有贡献；
- 一次请求同时注入多条经验时，单个 outcome 不能分摊给每条经验。

学习前需保留 label 来源、判定版本、horizon、情境和数据质量。数据缺失、版本不明、未成熟或不可归因样本不得伪装成负例；不可执行但预测有效、数据完整且已成熟的样本仍可进入预测质量统计，只排除在执行收益统计之外。

#### 5.4.5 先测“经验组合”，再测单条经验

分两层做试验，避免一开始就做不可靠的逐条归因：

1. **组合级试验**：比较无经验注入、当前经验包、等 token 的中性占位/格式对照，判断经验注入整体是否有增益，以及收益是否只是来自更多上下文或更长提示。
2. **单项/小组试验**：只在合格候选之间，对非安全关键经验做受控随机曝光或消融，估计单项及少数预注册交互项；同一批实验保持模型、提示主体、工具、数据时点和 token 预算一致。

控制约束：

- 不随机移除安全、合规、数据边界、强制验证等必要规则；
- 优先离线回放或 shadow；如线上试验，需用户可见行为和交易动作安全隔离；
- 随机分配只在同一适用条件、同一任务类型且都合格的经验中进行，并记录分配概率；
- 对随机化前的条件、horizon、symbol/date cluster、skill/model 版本做分层或区组；
- 同一 bundle 中多条经验不可各自重复领取完整 outcome；先评 bundle，再逐步消融；
- 固定 token 预算和注入位置，或将位置/长度作为实验因素，否则无法区分“内容效果”和“位置/篇幅效应”；
- 对经验之间的冲突和交互做单独标记，不能把组合效果硬拆成单项权重；
- 报告暴露、采纳代理信号、成熟和排除数量，不把“未提及”视为负面效果。

在不能随机化的历史数据上，可以做倾向分层、匹配或敏感性分析，但结论必须标为观察性相关，不得据此自动删除/降权经验。

#### 5.4.6 每条经验的效果报告

按以下维度报告经验效果：

```text
experience_id / source_kind / version
曝光数 / 合格数 / 未曝光数
成熟可评价数 / 排除数及原因
被引用或呼应的比例（弱信号）
净超额收益差 / 风险差 / Brier score 差
置信区间 / 日期簇数 / 股票簇数
平均 token 增量 / 延迟增量
适用条件与预注册交互项
```

只有具备受控对照、足够独立簇和稳定样本外效果时，才能称为“经验有效”或“有害”；其他情况使用“证据不足”或“仅观察到相关”。

## 6. 指标体系

### 6.1 主指标

指标分为预测质量与交易应用质量两层，不能混成一个总分。

**预测层主指标：**对明确方向预测，使用按 horizon/情境分层的方向成功率；对具有匹配概率的预测，使用 Brier score。两类分别报告，不重复计权；每个实验需预先指定主要端点。方向预测有有效样本但没有概率时，仍可评价方向成功率，不能补造概率。

```text
direction_success_rate OR Brier score  # 按预注册的预测目标选择一个主要端点
```

“预测成功”必须绑定预测对象、方向/概率、成熟窗口和标签规则；只报总体命中率不够。对拒绝预测、缺数据和未成熟样本分别计数，不得从分母中静默消失。使用置信区间和独立日期/标的簇评估不确定性。

报告分母至少拆成：预期可追责的请求数、成功抽取的明确预测数、已成熟可判预测数、有效概率数。预测未抽取或表达含糊不能悄悄从 coverage 中消失；它们不一定是方向 miss，但必须留在预测覆盖/可评估率指标里。有效且已成熟但不可执行的预测仍进入预测质量统计。

`coverage / abstention rate` 是主指标的 guardrail，不与成功率或 Brier 合并；`calibration error` 是概率质量的诊断性次指标，不与 Brier 重复作为主端点。

若预测被用于交易或执行，则单独报告应用层结果，作为重要护栏而不是预测成功率的替代：

```text
net excess return
max drawdown
cost / turnover
Sortino 或 Calmar
```

预测层的主指标仅在相应数据条件成立时计算：`direction_success_rate` 要求明确方向、对象和成熟视界；Brier score 要求概率与标签目标严格匹配。未通过概率契约的数据只计入概率 coverage gap，不得用其他视界或模型概率补位。

**当前数据现状对主端点的约束**：现有决策记录中，`claims.predicted` 为方向/幅度/点位结构，不含与标签目标匹配的校准概率键（`p` / `prob` / `p_up` 等覆盖率为 0），`claims.confidence` 非空率约 2.2%。因此在概率字段按本方案补齐并积累之前，Brier score 与 `δ_brier` 相关端点**无数据可算**，预测主端点只能用 `direction_success_rate`，并把概率 coverage gap 记为 100% 而非 0；不得用置信度冒充概率，也不得据此宣布概率质量达标。这是数据供给缺口，不是指标定义问题——补齐概率字段后本节契约自动适用，无需改口径。

**交易应用层主指标：**只有实际或仿真执行口径明确时，才评价净超额收益和回撤；不得用方向成功率替代交易绩效。

### 6.2 次指标

```text
点位命中率
止损命中率
概率校准误差（Brier 的分箱诊断）
交易成本与换手
决策一致性 / 经验呼应率（采纳信号；实测未证实与终局相关，仅诊断、不驱动升级；§5.4.6、§9.4 第 5 条）
```

`决策一致性` 与 `经验呼应率` 是采纳信号（弱信号），不等于预测质量本身：第 1 步配对实测显示 T+1 与 T+3/T+5 的 outcome 相关性不显著（§9.4 第 5 条），故这两个信号**仅作诊断与候选生成，不得作为低档位（`observe` → `shadow`）升级的辅助证据**，也不得替代 §6.1 主端点。其口径沿用 §5.4.6 的"被引用或呼应比例"；将来若要用作升级证据，须先单独验证与终局的相关性并预注册。

### 6.3 诊断指标

```text
幅度误差
时间偏差
数据缺失率
undecidable 率
截断率
退市/停牌率
工具失败率
模型格式失败率
```

经验提取与供给漏斗（诊断指标，逐级计数，分母为各级入口数，逐级留存率单列）：

```text
抽出 → 结构化 → 可算 → 去重 → candidate → validated → 检索命中 → 组合增益
```

每级记录入口数、通过数、拒绝数与拒绝原因。`白名单覆盖率`（`condition` 可计算候选占全部候选比）单列，与 §8.4① 的越界 key 拒绝数、`needs_data` 滞留数对齐；末级"组合增益"是 §5.4.5/§9 的实验量，只在此标注为漏斗终点，不是诊断性的比率。

指标层级：预测层主指标用于判断预测质量；交易应用层主指标用于判断策略可执行性；诊断指标解释误差来源。二者分开验收，不将收益、命中率和校准误差加权成一个未经验证的综合分。

## 7. 归因粒度

最低学习粒度建议为：

```text
experience_id × experience_version × condition_key × horizon
```

例如：

```text
exp-00421/v2 × down|high × T+3
```

skill/version、工具链和 action type 是来源/应用链路的分组维度，不是经验身份的替代品。每个经验单元统计：

- 名义样本数；
- 独立簇数；
- 净超额收益；
- 风险调整收益；
- 胜率；
- 最大回撤；
- Brier score；
- 置信区间；
- 版本开始时间和最后更新时间。

domain 层只作为仪表盘汇总。

## 8. 反馈机制改造

### 8.1 不以自然语言提示作为唯一反馈

历史结果不能只写成：

```text
finance 历史表现较弱，请谨慎。
```

应进入 `app/agent/` 内统一经验目录的检索与应用控制逻辑。反馈主要调整“什么经验在什么条件下被检索、排序、注入和降级”，而不是修改 skill 本身。

允许反馈影响统一经验层：

1. 经验项是否满足检索/注入资格；
2. 适用情境、horizon、任务类型是否匹配；
3. 经验项的检索优先级及曝光频率；
4. 提高经验的证据成熟度（`candidate → validated`），以及是否将其消费档位从 observe 放开到 shadow/active；
5. 经验提示是否要求 LLM 重新核对当前数据、明确冲突或降低结论确定度；
6. 是否应 abstain 或触发额外验证。

交易门禁、仓位、止损等执行策略属于另外的决策层；只有通过独立验证并在允许范围内时才可调用其现有接口，不通过修改 `skills/` 或 `tools/` 内文件实现。

禁止把 `effective_weight` 直接变成模型概率、胜率预测或买卖指令。它只表示该经验作为上下文证据的历史支持强度；LLM 仍需结合本轮数据作判断，量化模块仍独立给出数值结果。

### 8.2 建立三态消费方式

`consumption_mode` 描述反馈机制对行为的作用强度，与 §5.4.1 的 `evidence_status`（经验自身的证据成熟度）是两个正交维度：

```text
observe  # 只记录，不影响行为
shadow   # 计算新决策，但不对外使用
active   # 正式影响行为
```

新反馈默认从 `observe` 开始，禁止直接进入 `active`。两个维度的对应规则：

| `evidence_status`（证据成熟度） | 允许的 `consumption_mode`（行为档位） | 说明 |
|---|---|---|
| `candidate` | 仅 `observe` | 证据未确认，只记录曝光与结果，不参与决策 |
| `validated` | `observe` / `shadow` / `active` | 入库质检通过（§5.4.1，非效果验证）；默认仅 `observe`，升 `shadow`/`active` 须另按版本化判定策略达标逐级自动生效（见 §9.3/§9.4），人保留否决与撤回权 |
| `retired` | 强制 `observe` | 证据被推翻或失效，退出决策影响 |

`consumption_mode` 可按版本/请求比例/domain 灰度（§8.3），灰度只改变作用强度，不改变经验的 `evidence_status`。同一条经验的成熟度升级与档位放开是两个独立决定，不能相互替代。

### 8.3 灰度和回滚

反馈行为必须支持：

- 按版本灰度；
- 按请求比例灰度；
- 按 domain 或 skill 灰度；
- 一键关闭；
- 恢复上一组确定性参数。

### 8.4 经验如何随回评增长：五个引擎

前面各章定义了"评估什么"（§5）、"反馈以什么档位作用"（§8.2）和"如何验证有效"（§9），但尚未定义一条经验在拿到失败结果后**如何变得更好**——若失败只导致降权，系统只会淘汰经验，不会改进经验，这正是"回评没有提智"的直接原因。本节把"经验→更聪明"的过程显式化为五个引擎。

边界声明：以下引擎中，①—④借鉴了 `docs/经验库升级设计_20261104.md` 与 `经验库升级设计_20261117.md` 的成熟机制，但**只借契约思想，不搬其实体**——不引入 DSL/AST 表达式解释器、独立特征注册表服务、节点树、谱系族预算、建表与迁移；一切在 `app/agent/` 内以本方案的 `experience_id` 目录字段、本地求值与版本化配置实现，遵守 §1.1 硬边界。文中"已登记特征 key"仅指版本化配置中的一个轻量白名单（定义见 ①），不是独立注册表服务。⑤ 为本方案独有增量。

**① 结构化编码：让经验可机读、可匹配**

§5.4.1 已把候选经验规范化为"条件—判断/做法—适用边界—证伪条件"四段式，但 `applicability_conditions` / `normalized_content` 仍是自由文本。增强方向：把四段拆成稳定字段——`condition`（可计算条件）、`judgement`（判断/做法）、`boundary`（适用边界）、`falsifier`（证伪条件），并让 `condition` 只引用"已登记、可计算的特征 key"；未登记或不可计算的条件不得进入匹配（这与来源类型表"判别特征/证伪规则"的 `feature_key` 对齐）。

这里的"登记"落为一个**版本化轻量白名单**：一份带版本号的配置清单，列出允许在 `condition` 中引用、且能由上下文直接取值的 key；初始集合为情境帧 `context_frame` 的六个条件字段（`regime` / `theme_heat` / `vol_bucket` / `index_ret` / `limit_up_cnt` / `turnover`，`as_of` 为时点不参与条件）加上 claim 已有字段（`subject` / `horizon` / `prediction_target` / `direction`）。白名单的扩展须经评审并记录版本，不得由提取 LLM 自行新增 key（这与 §9.3 版本化判定策略同一套版本纪律）。

`condition` 的求值是**固定谓词函数**，不是表达式解释器：每个条件形如 `(key, op, value)`，`op` 只允许 `eq` / `in` / `gt` / `lt` 四种，输出 `applicable` / `not_applicable` / `unknown` 三值（取值缺失即 `unknown`，不猜、不当 `false`）。组合只用显式记录的 `and` / `or`，不解析任意表达式字符串、不执行代码。借鉴自两份设计的"特征门 + 三值条件求值"思想，但落点是白名单 + 谓词函数，不搬 `feature_registry` 服务或 AST 解释器实体。可观测产物：每条经验四段字段非空率、`condition` 可计算比例、白名单版本与越界 key 拒绝数。

**①附·来源优先级与提取预算倾斜**：候选来源按信息价值显式排序——**失败改写（§8.4④ 产物） > 用户纠正 > 成功复盘 > 自由文本**。每 run 的候选提取上限（预算）按此优先级倾斜，高价值来源优先占用额度，避免均等配额让高噪声的自由文本挤占预算。此优先级只约束提取配额，不改变 §5.4.1 的入库质检与 `evidence_status` 流转：失败改写产物仍须回到 §5.4.1 全流程重评，不得因来源优先级高而跳过质检。预算数值与各级配比写入版本化配置（§9.3 同一套版本纪律），不在代码中硬编码。可观测产物：各来源类型的候选数、各阶段成功率（§6.3 漏斗）、预算使用率。

**①附·不可计算候选不留滞留区**：`condition` 引用越界 key 或运行时不可计算的候选（`needs_data`），不得长期滞留为 `candidate` 堆积成"永不生效的僵尸条目"。处置二选一——当场拒绝（记入 coverage 与拒绝原因），或标记为待白名单扩展并设时限（如 30 天，数值走版本化配置），到期仍不可计算则转 `evidence_status=retired`。此状态是 `candidate` 的一个待办标记，不新增第五种 `evidence_status`，也不与 §5.3 的 `undecidable`/`data_missing`（那是 resolution 层判定态）混用。可观测产物：`needs_data` 候选数、待扩展时长分布、到期退役数。

**② 情境匹配：把"适用条件"从字段变成求值**

这是 1118 原方案的实质缺口——`applicability_conditions` 只是被记录的字段，没有"当前状态→该经验是否适用"的判定逻辑。定义本地 `applicability` 求值结果：`applicable` / `not_applicable` / `unknown`（缺数据、无匹配、存储故障分开，与 §5.3 不可判定态一致）；检索先按 `condition` 求值过滤，再按 §5.4.2 的 `effective_weight` 与证据强度排序，顺序、版本与 PIT 可用时间一并留痕（§5.4.1）。`unknown` 不得当作 `applicable`，也不得把旧快照复用到今天（§10.3）。借鉴自两份设计的 `retrieval_policy` 与三值条件求值（`unknown ≠ false`）。可观测产物：每次检索的 applicable/unknown 计数、求值失败原因。

**③ 冲突消解与组合合成：多条经验如何在一次决策里共存**

§5.4.5 只要求"标记冲突与交互"，没有给出消解规则。定义：适用条件重叠且结论相反的经验，按共享样本/特征来源归为同组联合评估（§5.4.5），不把高度相关的经验当独立投票；无法单独归因时标 `unattributable`（沿用 outcome link 的 `eligible_for_attribution` / `exclusion_reason`），不摊分权重；组合级结论先于单项结论（§5.4.5）。冲突未解决时，注入内容显式呈现分歧与不确定性，由 LLM 结合当前证据判断，不得静默按"最新版本"覆盖，也不得让文字权重直接改写量化概率（§5.4.2）。借鉴自两份设计的共线归组、联合评估、冲突未决时提高不确定性或 abstain。可观测产物：冲突组数量、每组联合评估结论、abstain 次数。

**④ 反事实学习：失败经验如何被改写而不是只被降权**

这是"提智"的核心引擎。一条经验在独立回评中失败（`evidence_status=retired` 或效果检验不通过）时，不直接淘汰，而是先按失败归因分类再对症改写：

| 失败类型 | 判定特征（沿用 §6/§9 口径） | 改写动作 |
|---|---|---|
| 无区分力 | 样本内效果就在中性区间、条件没有分开样本 | 叠加：补一个能把样本分开的前提条件，收窄适用范围 |
| 时间外不重现 | 样本内有效应、独立时间外不延续 | 精简：删掉样本内偶然成立、时间外不稳的条件项 |
| 归因不清 | 多条经验重叠命中、无法单独评估 | 双轨：并行保留两个近似变体，各自独立累积曝光与 outcome |

改写产出的是**新版本经验**（新 `experience_id` / `experience_version`），必须回到 §5.4.1 的"候选→独立回评→validated"全流程重评，不复活旧结论、不覆盖旧证据。防自欺约束：改写候选由探索数据形成后、在确认性评估开始前登记（预注册）——改写本身由失败结果触发，不可能先于结果存在，故约束的是"确认评估前登记"而非"看结果前登记"；确认性评估只能用未参与本轮探索的新数据；改写代数与候选数上限写入版本化配置，不得在代码中硬编码。借鉴自两份设计的改造通道（叠加/精简/双轨并行）与"整批预注册、不得复用看过的数据、按谱系封顶"约束，但**不建谱系树/族预算实体**，改写谱系只用新版本指向旧版本的引用字段表达。可观测产物：改写候选数、各失败类型的改写成功率、改写版相对原版的增量。

**⑤ 元学习：把"哪类经验在哪类情境有效"本身登记为经验（本方案独有）**

前四个引擎优化的是单条经验；但系统是否"越用越聪明"，取决于它能否把跨经验的规律沉淀下来。定义一类特殊经验 `meta_experience`：它不描述具体行情判断，而描述**经验选择本身的规律**（例如"当前波动状态下，带明确证伪条件的经验比泛化经验的有效率更高"）。其身份、版本、`evidence_status`/`consumption_mode` 与 §5.4.1/§8.2 完全复用，仅 `experience_type` 区分；它的 outcome 不是行情收益，而是"按该元规则选出的经验包"相对"无差别经验包"在组合级实验（§5.4.5）中的预测质量增量，同样受 §9 对照与 §9.3 δ 门槛约束，默认 `observe`。`meta_experience` 的准入、披露与灰度与普通经验一致，不得绕过对照实验直接生效。

对照臂口径：初期不新增第四臂，直接用 §9.1 现有两项实验的数据做**描述性差值**——在实验一/实验二的臂内，把经验包按 `meta_experience` 的元规则分组与"无差别选包"子组比较，报告预测质量差值与不确定性。该差值只作诊断与候选生成，**不构成因果有效结论，也不得单独驱动档位升级**；`meta_experience` 若要 `shadow`/`active`，仍须走 §9 的独立预注册对照（为其专门设臂）并通过 §9.3 门槛，§13 相应验收项同步约束。这样既不让元学习绕过门槛，也避免实验组合在起步阶段爆炸。

两份设计均未提供这一层，故其实现与判定口径为本方案新增，需单独预注册，不与具体 skill 或工具绑定。

五个引擎的关系：① 使经验可计算，② 决定用哪条，③ 决定多条如何共存，④ 使失败经验得到改写，⑤ 使选择规律本身可积累。前四者提升"单条与组合经验的效果"，⑤ 提升"选择经验的能力"——只有 ⑤ 闭环，系统才会随时间改变自己的检索与融合策略，而非仅增减条目。

### 8.5 经验细化的纪律（规则先定，实施留待后续）

本节只固定将来把一条经验拆成更细子经验时必须遵守的纪律；本方案当前**不实施任何细化动作**，也不新增来源或改权重。细化指按 §8.4① 白名单内可计算条件把父经验切成更窄的适用情境，属于经验粒度的演化，不改变 §5.2.1 的主视界一票口径。

1. **一票不因细化稀释**（根本约束）：同一决策、同一父经验的所有细化子经验在成功率、Brier、权重中**合并计一票**，不得因拆分而增加样本数；口径沿用 §5.2.1 主视界一票与 §8.4③ 共线归组。违反此条等于用拆分人为制造独立样本、绕过 §9 的功效约束（该约束已由独立交易日簇实测钉死）。
2. **证据不继承**：子经验的 outcome 只由自身曝光与回评独立累积，不得继承父经验的命中记录充当自身证据；确认性评估只用未参与该次细分探索的数据（§8.4④ 防自欺约束）。细分依据必须是 §8.4① 白名单内的可计算谓词，不得由提取 LLM 自行发明条件。
3. **谱系可追溯但不建树**：子经验记录 `parent_experience_id` / `split_rule` / 版本，沿用 §8.4④"新版本指向旧版本"的引用字段表达，不建谱系树或族预算实体（§8.4 边界声明）。
4. **粒度下限与僵尸防治**：子经验独立簇低于 §9.4 的 `n_min`、或其 `condition` 不可计算时，不得挂为长期 `candidate`——应对照 §8.4① 的 `needs_data` 处置当场拒绝、限时等白名单扩展或合并回父级，到期仍不成立则转 `retired`。
5. **档位与闸门不绕过**：子经验一律从 `evidence_status=candidate` 重走 §5.4.1 全流程、默认 `observe`；升 `shadow`/`active` 仍按 §9.3/§9.4 达标自动生效，不因"是父的细化"而直接获得 `validated` 或放开档位。细化代数与分支数上限写入版本化配置，防无限细分。

可观测产物：细分候选数、各分支独立簇数、合并计一票的口径留痕，并入 §6.3 漏斗的去重级。

## 9. 对照实验

### 9.1 分开验证“回评迭代效果”和“经验注入效果”

不要把所有变化塞入一个 A/B/C/D 总实验，否则难以区分提升来自回评更新、经验注入还是提示长度。至少拆成两项预注册对照：

**实验一：T+N 回评迭代是否改善预测**

```text
A：启用 T+N 更新后的统一经验策略
B：同一初始经验快照冻结、不吸收新 T+N 结果
C：统一经验检索关闭的当前 agent（仅作系统基线）
```

A 与 B 的模型、提示框架、检索实现、经验初始内容和 token 预算一致，唯一差别是是否让成熟 T+N 结果更新经验有效性/排序。该实验用于验证“回评 + 迭代”本身是否提高预测成功率。

**实验二：共享经验作为上下文是否有增益**

```text
A：启用统一经验检索与注入
B：关闭经验注入
C：等 token 的中性格式对照（用于排除单纯上下文长度效应）
```

该实验固定经验快照，不做在线更新，用于单独测“给 LLM 看这些经验”是否有帮助。通过后再做单项/小组消融，定位具体有效、无效或有害的经验。

所有臂必须使用：

- 离线成对回放时使用相同 decision/股票集合；前瞻随机试验时使用相同纳入规则，并在区组内平衡 symbol clusters（不要求同一标的同时进入不同 arm）；
- 相同数据截止时间；
- 相同 T+N 窗口；
- 相同交易成本；
- 相同执行规则；
- 相同抽样日期和预测 coverage 口径；
- 影子/离线试验，不触发真实下单或不可逆外部副作用。

实验一验证持续更新效果，采用前瞻的 **`symbol_cluster × evaluation_window`** 分配：同一标的在同一评估窗口内的所有 decision、claim 和相邻 horizon 必须固定在同一 arm，避免同标的相关样本跨臂污染。区组至少按市场状态/日期窗口分层，并在区组内平衡标的簇。

实验二验证固定经验包的即时注入效果，优先使用离线成对回放：同一决策输入分别送入各 arm，固定数据、工具结果和模型版本，随机化运行顺序；使用配对差异并按 symbol cluster/date 计算不确定性。若采用前瞻随机曝光，则同样按 symbol cluster × evaluation_window 分配，不能让同一标的相邻 horizon 跨 arm。

效果区间使用 symbol cluster 与 date 的双向 cluster bootstrap（或预注册的等价聚类方法），不能把同标的相邻 horizon 当独立样本。

入组时冻结 arm、经验目录快照、模型/prompt 版本和分配概率。实验一中每个 arm 的经验状态隔离：某 arm 的成熟 outcome 只能更新该 arm 下一批的 policy snapshot，不能写回另一 arm 正在使用的快照。历史回放必须按时间顺序做 point-in-time/prequential 评估：每个测试日只允许使用该时点前已成熟的 outcome 更新经验，不能先用全量历史 T+N 标签拟合后再回测同一历史区间。

### 9.2 比较指标

```text
净超额收益
单位时间净收益
最大回撤
Brier score
方向命中率
token 和工具成本
响应耗时
```

### 9.3 验收标准

实验前必须预注册主要端点、实际意义阈值、样本量/独立簇门槛和统计检验：

1. 实验一须预先选定一个预测主端点，并以按 `symbol_cluster × date` 聚类的单侧 95% CI 检验：若主端点为方向成功率，则 A−B 下界需大于 `δ_pred`；若主端点为 Brier，则 A−B 上界需小于 `-δ_brier`。另一端点作次要指标并进行多重比较控制；同时报告与 C 的系统基线差异；
2. 实验二经验注入组优于无经验组及等 token 对照，才说明经验内容有独立价值；
3. 概率校准、coverage 和 abstention rate 同时报告，不能靠少预测来虚增成功率；
4. 若接入交易执行，净超额收益差的单侧 95% CI 下界须高于 `-δ_return`，最大回撤增量的单侧 95% CI 上界须低于 `δ_drawdown`；
5. 结果覆盖多个交易日、行情状态、horizon 和标的簇，不依赖单条经验、单只股票或单个市场事件；
6. 独立簇数达到预注册门槛，关闭经验回路后可恢复基线行为。

判定门槛不采用“由人拍定单一收益阈值后才解锁”的方式——那会形成“没有效应量数据就不敢拍阈值 → 回路停在 `observe` → 更没有数据”的死锁。改为**版本化判定策略**：把主端点、实际意义阈值 `δ_pred`/`δ_brier`/`δ_return`/`δ_drawdown`、最小可检测效应 `mde`、功效、最小独立簇数 `n_min`、look 里程碑与上限 `n_cap`/`max_looks` 集中写入一份带版本的实验配置（与 §5.4.2 `effective_weight` 口径的版本化要求一致），由策略负责人在实验前一次定档，并经独立簇功效审计校验；文档不擅自填具体数值。

发布规则：**门槛达标即按消费档位逐级自动生效**（`observe` → `shadow` → `active`），人只保留否决与随时撤回权，不需要在每轮实验后再次人工批准一个收益数字。`δ_*` 取自预置的策略风险容忍区间，不得从本次实验结果反推；预测层升级依赖 `δ_pred`/`δ_brier`，交易层依赖 `δ_return`/`δ_drawdown`。任一层所需参数缺失或配置版本不兼容时，该层维持当前档位并记录原因，而不是进入无限等待（退出机制见 §9.4）。多主端点须预注册优先级并控制多重比较。

**功效审计的前置数据门槛**：§9.4 的 `n_min`/`n_cap` 必须以**独立交易日簇**为单位估算，不能用名义 resolution 条数。按现有记录的实测口径：约 26 个已回评交易日、每日标的池约 6.8，折算后独立簇约 78、MDE 约 15.9pp（按天算簇则 MDE 约 27.5pp）；分辨 5pp 需约 262 个交易日。因此当前样本不足以判定任何 5~10pp 量级的经验增益，任何经验项在此期间一律维持 `observe`，不得据现有数据升 `shadow`/`active`——这与 §9.4 的"未达门槛不升级"一致，是功效不足的如实反映，不是死锁复发。此外概率端点当前无数据（§6.1），`δ_brier` 相关判定在数据补齐前标为不可判定并计入 coverage，不参与档位升级。

### 9.4 门槛定档与自适应停止

功效不足是 T+N 回评落地的主要障碍：独立簇数近似等于可用交易日数，T+3/T+5 成熟慢，固定样本显著性检验容易被噪声拖成年级周期。判定策略按以下方式定档与停止，使任何经验在有限期内都能出结论，不长期卡在 `observe`/`shadow`：

1. **按预注册 `mde` 定样本量**：`n_min ∝ σ²/mde²`，`σ` 用历史同簇方差的保守估计，`mde` 用策略预注册的最小有意义效应而非本次实测效应——用实测效应反推样本量会系统性高估收益（winner's curse）；
2. **等价检验判无效**：对“无实质增益”采用 TOST/等价检验，达标即判 `no_gain` 并维持或降档，而不是因“证不出有效”永久 pending；
3. **双向封顶**：设独立簇上限 `n_cap` 与最大 look 次数 `max_looks`；到上限仍无定论即判 `inconclusive`（证据不足），按当前档位保留并记入 coverage，不再等待；
4. **前置 look**：预注册里程碑（例如 5/10/20/40/80 独立簇），在早期点即可按停止规则判有效/无效；多次 look 须预注册 α 消耗控制（如 alpha spending 或 Holm 步降），不得看到结果后追加 look；
5. **代理信号不得驱动升级（实测否证后降级为纯诊断）**：现有数据中，同一 (标的, 决策交易日) 上 T+1 与 T+3 的 outcome 可配对 36 组（展开 793 对），二值列联表给出 `phi≈0.062`、`OR=1.35`、`95%CI [0.96,1.90]` 含 1，相关性不显著；T+1 与 T+5 仅 2 组（`phi≈0.024`），按 decision_id 配对则为 0。因此 **T+1 命中不能预测 T+3/T+5 命中，代理-终局相关性不成立**：T+1 的预测质量、决策一致性/经验呼应率等早期可得信号只作诊断与候选生成，**不得作为 `observe` → `shadow` 升级的代理证据**。升级的前置证据只能是该经验自身的独立簇回评；若将来确需代理信号，须先单独验证其与终局的相关性并预注册，未验证前一律不用。T+3/T+5 真实收益与净超额收益仍仅用于高档位（`shadow` → `active`）确认。证据出处：第 1 步只读配对探针 `tmp/probe_tn_pairing_20261119.py` / `.json`。

以上仅为判定口径的通用约定，不绑定具体 skill、工具或领域；实现留在 `app/agent/` 内，符合 §1.1 边界。

## 10. 数据落库方案

建议新增统一结果表 `qd_agent_evaluation_outcomes`，一条记录对应一个可评价 decision/horizon。

由于实施范围不包括 `migrations/` 等目录，不新增或修改范围外的迁移文件。表结构在 `app/agent/` 内按本仓已有的 `_DDL + _ensure_schema()` 惰性初始化模式实现，参考 `utils/case_memory.py` 和 `rag/pg_vector_store.py`：仅做 additive、幂等的 `CREATE TABLE/INDEX IF NOT EXISTS`，通过现有 DB 接口执行并显式 commit/rollback；初始化失败必须产生可观测告警/健康状态，不能返回“写入成功”的假结果。DDL/初始化代码必须留在 `app/agent/`，不得要求手工改 `migrations/`。若数据库权限或既有部署策略不允许运行时建表，先停在设计/影子阶段并请用户确认范围，不得越界补迁移。

逻辑上需要四类持久化结构（物理表名可在实施时按现有约定调整）：

```text
qd_agent_experiences          # 统一经验目录与版本/状态/来源
qd_agent_experience_injection_events # 请求级逐项经验曝光及实验分配
qd_agent_evaluation_outcomes         # 预测身份及不可变的回评版本
qd_agent_experience_outcome_links    # 曝光事件 ↔ outcome 的多对多归因连接
```

核心字段示例（字段按 §10.3 的"共享核心信封 / 领域 adapter 扩展"两层理解；本清单是全集，其中标注 `[finance]` 的字段仅存在于 finance adapter 扩展、不进共享核心表；实现时以 §10.3 为准）：

```text
decision_id
claim_id
prediction_id
resolution_id
outcome_id
outcome_policy_version
snapshot_version
experiment_arm
subject
subject_kind
horizon
as_of
data_as_of
outcome_available_at
resolved_at
target_event
prediction_target
label_rule_version
forecast_capture_status
probability_source
probability_target
probability_horizon
coverage_gap
condition_key
sample_layer
model_name
prompt_sha
skill_id
skill_version
tool_chain_sha
experience_bundle_id
experience_bundle_sha
experience_injection_ids
experience_assignment
predicted_probability
predicted_direction
entry_fill            # [finance] adapter 扩展，不进共享核心表（§10.3）
exit_fill             # [finance]
net_return            # [finance]
benchmark_return      # [finance]
excess_return         # [finance]
max_drawdown          # [finance]
verdict
verdict_score
brier_score
actual.windows             # 多视界诊断子结构（§5.2.1），只诊断、不进 verdict/权重
deviation.windows          # 多视界诊断子结构（§5.2.1），只诊断、不进 verdict/权重
execution_status
data_quality
```

预测身份唯一键为 `prediction_id = (decision_id, claim_id, subject, prediction_target, horizon)`；结果记录以 `(prediction_id, outcome_policy_version)` 幂等唯一。`label_rule_version` 是 `outcome_policy_version` 的组成部分而不是 prediction key；同一预测按新规则重评时新增 outcome 版本，旧结果保留但只允许一个版本作为当前 canonical outcome 参与同一份统计。`experience_event_id × outcome_id` 的多对多 link 单独保存，不在 outcome 表里重复膨胀样本数。

pair、claim、case memory 可以继续存在，但新经验学习必须明确其 canonical outcome 来自哪条链。建议预测经验主标签读取 `qd_agent_resolutions` 中按 claim/horizon 成熟的结果；旧 `qd_agent_traces.correct`、`qd_skill_selection_events.label` 和案例 `outcome.label` 保持各自语义，通过明确映射关联，不能当作重复独立样本。

### 10.1 实体关系

```text
decision 1 ── N claim
claim 1 ── N resolution_attempt
claim 1 ── 1 prediction_id
prediction_id 1 ── N outcome_version
outcome_version N ── 1 canonical resolution/data snapshot
decision 1 ── N experience_injection_event
experience 1 ── N experience_injection_event
experience_injection_event 1 ── N experience_outcome_link
outcome_version 1 ── N experience_outcome_link
```

所有经验统计必须沿 `experience_injection_event → experience_outcome_link → canonical outcome_version` 连接；原案例的 outcome 不代表其后续复用效果。分析每个 outcome 版本时只取指定 canonical policy 版本；其他 label/resolver 版本留作审计和重算，不并行累加。link 表至少含 `event_id`、`outcome_id`、`eligible_for_attribution`、`exclusion_reason`、`experiment_arm` 和随机分配概率。

### 10.2 术语边界

| 术语 | 本方案中的严格含义 | 不等同于 |
|---|---|---|
| `claim` | 从一次回答中抽取、带 subject/target/horizon 的待验证预测声明 | 回评结果 |
| `resolution` | resolver 对某 claim 的一次判定尝试及其原始数据、`verdict`、规则/模型版本；重试可有多条 | 独立样本或最终 outcome |
| `verdict` | resolver 原始分类，如 hit/partial/miss/undecidable/data_missing | 经验有效性权重、预测概率 |
| `outcome` | 对唯一 `prediction_id` 按一个 `outcome_policy_version` 规范化后的可评分结果；预测分数与执行结果分栏 | 每一次 resolution 或通用 label |
| `label` | 某个消费方/评估策略基于 outcome 生成的专用分类；必须带 label policy/version | raw verdict；不同表的同名 label |
| `experience` | 有来源、版本、条件和状态的可复用经验项 | skill 本身或原案例的正确标签 |
| `experience_injection_event` | 某请求实际候选、曝光/注入某经验的记录 | 证明 LLM 内部采纳或证明因果有效 |
| `domain` / `domain_schema_version` | outcome 所属任务领域及其契约版本，决定用哪个 adapter 解析结果 | 现有 `weight_feed` 的 domain 权重层；两者同名但语义不同 |
| `DomainAdapter` | 提供该领域 snapshot 校验、outcome 解析/成熟、评估与检索策略的显式注册插件（§10.3） | 共享表里的金融专属字段；通用层里的领域判断分支 |

`resolution.verdict` 是判定事实；`outcome` 是在指定预测目标、成熟视界和 outcome policy 下的评分记录；面向技能反馈或经验聚合的 `label` 是从 outcome 派生的消费口径。任何聚合都必须记录从 verdict 到 outcome/label 的确定性映射版本。

### 10.3 领域适配边界与通用信封

上节核心字段表里，并非所有字段都跨域通用：`prediction_target`、`horizon`、`verdict`、`verdict_score`、`brier_score`、`predicted_probability` 及各类 id/version/时间字段对所有预测回评一致；而 `entry_fill`、`exit_fill`、`net_return`、`benchmark_return`、`excess_return`、`max_drawdown` 是金融交易专属。若把这些字段平铺进共享结果表并让通用回评链路直接读取，链路会隐式假设“任何 outcome 都有成交价与基准收益”，换到其他任务类型便无法复用。因此采用两层契约：

**共享核心信封（全域一致，语义不依赖任何具体领域）**

- 身份与版本：`decision_id`、`claim_id`、`prediction_id`、`resolution_id`、`outcome_id`、`outcome_policy_version`、`snapshot_version`、`domain`、`domain_schema_version`；
- 时间与分配：`subject`、`subject_kind`、`horizon`、`as_of`、`data_as_of`、`outcome_available_at`、`resolved_at`、`experiment_arm`；
- 预测侧：`target_event`、`prediction_target`、`label_rule_version`、`predicted_probability`、`predicted_direction`、`probability_source/target/horizon`、`coverage_gap`；
- 评分侧：`verdict`、`verdict_score`、`brier_score`、`data_quality`、`condition_key`、`sample_layer`；
- 来源/实验/模型版本：`skill_id`、`skill_version`、`tool_chain_sha`、`model_name`、`prompt_sha`、`experience_bundle_id/sha`、`experience_injection_ids`、`experience_assignment`。

**领域 adapter（显式注册，领域专属语义不进共享表）**

每条 outcome 记录带 `domain` 与 `domain_schema_version`，由注册表中的 adapter 提供下列能力；共享层只按注册表调用，不写 `if domain == ...` 分支：

| 能力 | 职责 | 金融域（当前唯一实现） |
|---|---|---|
| `task_classifier` | 判定 decision 属于哪个领域、是否适合提取经验 | 股票方向/选股/板块任务分类 |
| `validate_snapshot` | 特征、单位、时间语义与数据质量校验 | PIT 时点、行情可见时间、缺帧 |
| `resolve_outcome` / `maturity_status` | 结果如何观测、何时成熟、何时不可判 | 主窗口取 claim 自身 horizon，`QD_LABEL_WINDOWS` 附加视界只诊断；`truncated`/`undecidable`/`data_missing` 记 coverage（§5.2.1、§5.3） |
| `evaluation_policy` | 指标、基准、成本、时间切分与统计门槛 | 预测质量为主、净收益为护栏；δ 与 look 门槛走版本化配置（§6、§9.3） |
| `retrieval_policy` | scope 兼容、证据排序、冲突展示 | 按适用条件、horizon 与版本兼容性排序 |
| `retention_policy` | 数据保留、脱敏与归档 | 快照/曝光/outcome 的保留期与脱敏 |

金融专属字段（`entry_fill`/`exit_fill`/`net_return`/`benchmark_return`/`excess_return`/`max_drawdown`）只放在 finance adapter 的 outcome 扩展里，共享引擎不直接读这些列，也不要求其他领域实现它们。

**硬约束**

1. adapter 缺失或 `domain_schema_version` 不兼容时，明确返回 `unsupported_domain` / `schema_incompatible`，记为 coverage/exclusion（§5.3），不用 finance 默认 resolver 兜底，也不当成负面 outcome 或经验失败。
2. 不强行把不同领域的 outcome 压成统一“胜率”；outcome 可为分数、分类、集合一致性、时长或成本。确需跨领域上层比较时，必须先定义带版本的业务效用函数与归一化口径。
3. 本方案首期只落地 finance adapter；adapter 接口先定义、只实现金融一个领域，不为“未来可能跨域”提前写死通用逻辑，避免单一领域上的过度通用化。
4. adapter 的注册、解析与调用全部位于 `app/agent/` 内，不改 `migrations/`、`skills/`、`tools/`，符合 §1.1 硬边界。

## 11. 分阶段实施

### P0：口径与观测

1. 在 `app/agent/` 内定义 `Decision Snapshot`、prediction identity key 和版本化预测契约校验器；
2. 明确预测成功、概率评分、horizon 和样本分母；若进入交易应用，再定义收益/基准/成本护栏；
3. 在 `app/agent/` 内建立统一经验目录，保存经验身份、规范化内容、适用条件、来源/版本和候选状态；此阶段只报告目录与曝光/连接数量，不声称已有有效性、胜率或可用权重；
4. 只读接入现有 skill、会话、工具结果和成熟回评作为经验来源，不改 `skills/`、`tools/`；
5. 建立逐经验曝光事件，把 `case_id` / skill 段落 / 会话 span / RAG chunk / feature rule 与消费请求关联；
6. 留存最终注入 bundle 的 ID、顺序、token 预算和版本 hash；
7. 明确新 claim/resolution 为哪类预测的 canonical outcome；`wrong data`、`not matured`、不可归因样本不进入有效性统计并分别报告；不可执行但有效且成熟的预测保留在预测质量样本中，只排除在执行收益样本外；
8. 报告“预期可追责请求 → 明确预测抽取 → T+N 成熟 → 有效评分”的 coverage 漏斗，防止只挑容易预测的样本提高成功率。

### P1：建立基线与对照

1. 分别固定“回评更新效果”和“经验注入效果”的对照定义；
2. 预留离线回放接口；具备可复现决策快照时用回放，否则一律走前瞻评估；
3. 产出按日期、市场状态、经验项和 horizon 的预测结果；
4. 不允许仅比较总体平均值；
5. 先完成“无经验 / 当前统一经验包 / 等 token 中性对照”的组合级实验；
6. 建立经验曝光漏斗和 outcome 连接率基线，先确认数据完整再估效果；
7. 历史 replay 只使用决策时点可用的经验/标签；不能重建时只做登记后的前瞻评估。

### P2：确定性反馈影子

1. 只有消费请求的 canonical outcome 成熟、目标匹配且归因合格后，才计算统一经验项的胜率、预测质量、有效性权重和不确定性，并生成效果视图；
2. 先进入 shadow 模式；
3. 由 `app/agent/` 中心经验检索器按当前请求条件为任意 skill/分析链路供给候选经验；
4. 记录新经验策略与当前策略的命中、排序和注入差异，但不改 skill/tool 源文件；
5. 对非安全关键经验运行预注册的小组/单项消融或受控随机曝光；
6. 同时记录 token、位置、bundle 交互和分配概率；不把 LLM 自述采纳当作因果证据；
7. 经验效果不确定或样本不足时保持候选/中性，不自动删除或强化。

### P3：小流量灰度

1. 建议从离线和 shadow 验证开始，再决定是否小流量启用；
2. 只允许统一经验目录的检索、排序和曝光策略生效，不修改 skill/tool 文件；
3. 监控预测成功率、校准、覆盖率、应用层收益/回撤、成本、超时和数据缺失；
4. 设置自动回滚条件；
5. 只启用经受控评估验证的经验策略；单条经验在灰度期间继续留存对照组；
6. 监控经验之间的负交互、跨情境迁移失败和提示长度/位置带来的副作用。

### P4：扩大反馈面

只有在影子和灰度通过后，才允许反馈影响：

1. 交易结论门禁；
2. 置信度上限；
3. 仓位建议；
4. 止损或观望策略；
5. 低收益工具链淘汰。

## 12. 建议优先实现

第一优先级不是进一步增加规则判定，而是建立如下闭环：

```text
只读获取现有 skill / 会话 / 工具结果 / 成熟 T+N 经验
  → app/agent 统一经验目录（来源、版本、条件、有效性、胜率、权重、不确定性）
  → 按当前请求检索适用经验并记录逐项曝光与 bundle
  → LLM 结合本轮事实判断；量化模块独立计算预测与统计
  → 冻结 decision snapshot
  → T+N 回评预测结果，并单独评估应用层收益/风险
  → outcome 回连本次实际使用的经验项，更新其效果与适用范围
  → shadow / 消融 / 对照验证迭代
  → 通过验收后更新统一经验层策略
```

## 13. 验收清单

- [ ] Decision Snapshot 有版本、数据截止时间和完整输入口径；
- [ ] 修改范围严格限制在 `app/agent/`；`skills/`、`tools/` 及 `app/agent/` 外文件均未修改；
- [ ] 可以只读获取现有 skill 和会话经验，且不要求在各 skill 内手工维护重复经验；
- [ ] 经验目录在多个 skill/分析链路间共享同一 `experience_id` 和效果统计；
- [ ] 经验权重只影响经验检索/排序/曝光，不直接伪装成量化概率或买卖指令；
- [ ] 概率与 target/horizon 不匹配时，Brier 为 N/A 且明确计入 `coverage_gap`；不得换用其他视界概率；
- [ ] Outcome 同时保存预测结果和执行收益；
- [ ] 有统一基准收益和交易成本；
- [ ] 可以按 skill/version/condition/horizon 查询结果；
- [ ] 能追溯每个消费请求实际注入的经验 ID、来源版本、内容 hash 和顺序；
- [ ] 案例原始 T+N 标签与“案例被注入后的消费请求 outcome”分开存储、分开计数；
- [ ] 曝光、未曝光、未选中、未执行、未成熟和不可归因状态分开统计；
- [ ] 单条经验结论基于受控对照或随机消融；观察性关联明确标注，不能自动当因果；
- [ ] 经验 bundle 和单项经验分别报告，不把组合结果重复分摊给每条经验；
- [ ] token、位置、版本、情境和经验交互作为效果分析的控制因素；
- [ ] 报告同时显示名义样本数和独立簇数；
- [ ] `undecidable` 和数据缺失不被伪装为中性结果；
- [ ] 关闭反馈后行为可恢复；
- [ ] `evidence_status`（candidate/validated/retired）与 `consumption_mode`（observe/shadow/active）分字段记录，状态对应表完整；
- [ ] 失败经验先按失败类型归因再叠加/精简/双轨改写，改写产新版本并回到候选→回评流程，确认只用未参与探索的数据；
- [ ] 来源按失败改写 > 用户纠正 > 成功复盘 > 自由文本显式排序，每 run 提取预算按此倾斜且高价值来源不跳过入库质检；
- [ ] 入库质检机械化：span 定位失败或 hash 不符即降级、`validated` 不由 LLM 自证引用产生；`falsifier` 含可观察事件谓词，空话式证伪降级；
- [ ] 不可计算候选（`needs_data`）当场拒绝或限时等白名单扩展、到期转 `retired`，不留永久滞留的僵尸候选；
- [ ] §6.3 提取漏斗逐级计数（抽出→结构化→可算→去重→candidate→validated→检索命中→组合增益），白名单覆盖率单列；
- [ ] 经验细化纪律已固化且当前未实施细化：子经验合并计一票、证据不继承、谱系只作引用、粒度下限与僵尸防治、档位闸门不绕过；
- [ ] `meta_experience` 与普通经验分型记录，其效果按组合级对照衡量、默认 observe，不绕过 §9 门槛；
- [ ] 版本化判定策略已一次定档：主端点、`δ_pred`/`δ_brier`/`δ_return`/`δ_drawdown`、`mde`、功效、`n_min`、look 里程碑与 `n_cap`/`max_looks` 均写入带版本配置并经独立簇功效审计校验，未擅自填数值；
- [ ] 门槛达标按消费档位逐级自动生效，人工否决/撤回路径可用且可观测；预测层与交易层分别依赖 `δ_pred`/`δ_brier` 与 `δ_return`/`δ_drawdown`；
- [ ] `n_min ∝ 1/mde²` 用预注册 `mde`、无效走等价检验、`n_cap`/`max_looks` 双封顶，任何经验在有限期内出有效/无效/证据不足三者之一；
- [ ] 功效审计按独立交易日簇（非名义 resolution 条数）估算；独立簇未达 `n_min` 或概率字段未补齐时一律维持 `observe`，Brier/`δ_brier` 标不可判定并计入 coverage，不用置信度冒充概率；
- [ ] 至少分别完成“回评更新效果”与“经验注入效果”两项预注册对照；
- [ ] 只有通过离线对照和灰度后才允许全量启用。

## 14. 参考代码位置

以下位置是本方案对应的主要现有实现：

- `app/agent/chain/evaluator.py`：旧 trace 回评、T+N 收益和 worker；
- `app/agent/chain/resolver.py`：claim 判定；
- `app/agent/chain/weight_feed.py`：resolution 到 domain 权重；
- `app/agent/chain/weight_hints.py`：权重读取和提示消费；
- `app/agent/chain/calib_feed.py`：pair 与 resolution 的校准喂数；
- `app/agent/chain/c3p.py`：证据提取、p_base/p_final 配对；
- `app/agent/chain/aggregator.py`：条件概率聚合和 pair 落盘；
- `app/agent/qd_aftercare.py`：请求收尾、C3P 和案例留痕；
- `app/agent/qd_preselect.py`：历史权重提示接入；
- `app/agent/utils/case_memory.py`：案例检索、注入格式和原案例标签延迟回填；
- `app/agent/qd_service.py`：RAG 与案例 prefetch，以及 skill 选择/注入事件接线；
- `app/agent/chain/skill_events.py`：skill/version 级候选、曝光、执行和成熟标签漏斗。

## 15. 最终原则

本方案的成功标准是：在不改动现有 skill/tool 内容的前提下，T+N 回评能否持续提高可验证预测的成功率，并使统一经验层中的有效经验被不同 skill 适当地检索和使用。

交易应用场景还需同时验证风险和收益；不得以交易收益替代预测准确性，也不得以命中率单独证明可执行策略有效。最终必须满足：

1. 它能否改变行为；
2. 新经验是否有足够、可追溯的消费结果支持；
3. 预测质量是否优于关闭经验回路的对照；
4. 改变后的行为是否可控、可回滚且没有突破实施边界；
5. 改善能否跨日期、跨标的、跨 horizon 和市场状态复现。
