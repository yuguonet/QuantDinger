# T+N 回评改进方案

> 文档来源：Copilot SDK in VS Code 会话分析  
> 任务：评估现有 T+N 回评是否形成有效学习闭环，并提出改进方向  
> 署名：Copilot  
> 时间：2026-11-18  
> 状态：机制层已实施、数据前置按设计维持 observe；完工判定见 §28。

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
2. 概率存在时，概率值必须有限且在 `[0,1]`；`probability_horizon == horizon`，且 `probability_target` 与 `prediction_target` 口径一致——方向类预测的生产目标是细分 `up`/`down`（契约 v2 接受 `prediction_target=direction` + `probability_target=up|down`），其余目标仍按字面一致校验；并校验 `probability_source` 对应的实际模型口径；
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

> **落地记录（2026-11-19，增量标注来源）**：新增 `app/agent/chain/ingestion_quality.py` +
> `ingestion_quality.json`（纯函数、零生产调用方、observe 档、回滚即删）。`verify_citation()`
> 机械化引用校验（ref 可定位 + span 非空 + `content_sha` 与 `source_text` 重算 sha256[:32]
> 一致，复用 `experience_capture` 口径；仅 LLM 自述引用不算）；`verify_falsifier()` 四段非空
> + falsifier 为白名单 `(key,op,value)` 谓词（复用 `structural_check`）或可观测事件字段绑定
> （边界匹配，空话式短语优先拦截降级）；`quality_gate()` 双判据过 **且** 调用方传入
> `spot_check=True` 才建议 `validated`，否则维持 `candidate` 并记原因，抽检缺失不假装通过。
> 配置/白名单损坏 fail-closed。一次性探针 `tmp/probe_ingestion_quality_20261119.py` 32/32 PASS。

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

> **落地记录（2026-11-19，增量标注来源）**：新增 `app/agent/chain/effective_weight.py` +
> `effective_weight_config.json`（纯函数、零生产调用方、observe 档、回滚即删）。公式
> `beta_shrink_wilson_lower_v1`：有效样本 `n_eff=min(独立交易日簇, 成熟可归因数)`
> （§9.3 聚类口径），Beta 收缩 `(success+z0*prior)/(scored+z0)`，不确定性惩罚取收缩后
> Wilson 单侧下界，再 clamp 到 `[w_min,w_max]`。全部阈值走版本化配置、默认 `null`=
> 未预注册；schema/公式不符、关键阈值缺失、来源不明、条件不匹配、独立簇不足、冷却未到
> 一律 `weight=None`（undecidable 或 neutral），**不伪造中性评估值**（验收 §13-
> 「undecidable 不伪装为中性结果」）。`neutral_placeholder()` 仅供排序缺省位、须标注非
> 评估值。权重只表示证据强度，不改 LLM 判断、不进量化概率。一次性探针
> `tmp/probe_effective_weight_20261119.py` 45/45 PASS。

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

> **落地记录（2026-11-19，增量标注来源）**：新增 `app/agent/chain/extraction_funnel.py` +
> `source_budget.py` + `source_budget.json`（纯函数、零生产调用方、observe 档、回滚即删）。
> ① `build_funnel()` 实现八级逐级计数，每级入口/通过/拒绝/拒绝原因分布、留存率分母为本级
> 入口；可算级复用 `structural_check`/`needs_data_disposition`，越界 key 与结构非法分开计；
> 白名单覆盖率单列（可算/抽出）；末级组合增益留存率置 `None` 并标 `experimental_endpoint_
> not_diagnostic`，不作诊断比率；`validated` 只认传入 evidence_status、不由 LLM 自证。
> ② `allocate()` 按失败改写>用户纠正>成功复盘>自由文本排序分配每 run 预算，高价值超预算
> 仍标 `must_quality_check`（不跳质检）、低价值超预算标 overflow 非丢弃；budget 未预注册
> (null) 不倾斜；同级稳定、未知来源排最末。两模块配置/枚举走版本化配置、fail-closed。
> 一次性探针 `tmp/probe_funnel_budget_20261119.py` 46/46 PASS。

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

> **实施落地（2026-11-19）**：新增 `chain/rollout_policy.py` + `rollout_policy_config.json`，纯函数、无生产调用方。`by_version()` 仅允许版本放行、`by_traffic()` 用 `sha256(request_id+salt)` 确定性分桶（同一请求恒同结果、比例走配置）、`by_domain()` 命中 domain/skill 名单才放行、`kill_switch()` 一键关闭强制 observe、`restore_previous()` 恢复调用方持有的确定性参数快照（本模块不持久化）。`effective_mode()` 叠加全部规则取最保守（任一不放行即 observe），且 `evidence_status_unchanged=True`（灰度只改作用强度、不改成熟度）。名单/比例/开关全走版本化配置；`enabled=false`/`kill_switch=true` 为默认不放行，配置缺失/异常/未预注册一律 observe（fail-closed）。一次性探针 `tmp/probe_rollout_policy_20261119.py` 34/34 PASS。消费层（档位真正改变行为）仍属 P2/P3，本模块不接生产调用方。

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

> **实施落地（2026-11-19）**：新增 `chain/conflict_resolution.py` + `conflict_resolution_config.json`，纯函数、无生产调用方。
> `conditions_overlap()` 结构判条件重叠（空条件=通用潜在重叠，同 key 值域相交，gt/lt 区间）。
> `group_conflicts()` 把条件重叠且结论相反（opposing_pairs 走配置）归冲突组、共享 `source_lineage` 归共线组，共线/冲突组内不当独立投票。
> `attribute_group()` 对齐 outcome link 字段语义：`eligible_for_attribution=False` 或共享 outcome link 或共线→ `attribution=unattributable` + `exclusion_reason`，`weight_split=False`（不摊分），`group_conclusion_first=True`（组合级先于单项）。
> `resolve_presentation()` 冲突未决→显式分歧 + `uncertainty=elevated` + abstain，`overwrite_by_version=False`、`probability_rewritten=False`（不静默按版本覆盖、不改量化概率）。可观测产物齐：冲突组数/每组联合结论/abstain 次数/unattributable 数。枚举与排除原因走配置，schema 不符 fail-closed。
> 一次性探针 `tmp/probe_conflict_resolution_20261119.py` 33/33 PASS。至此五引擎均有独立模块实现（①condition_eval ②applicability ③conflict_resolution ④counterfactual_learning ⑤meta_learning），全部零生产调用方、默认 observe、不绕过 §9。

**④ 反事实学习：失败经验如何被改写而不是只被降权**

这是"提智"的核心引擎。一条经验在独立回评中失败（`evidence_status=retired` 或效果检验不通过）时，不直接淘汰，而是先按失败归因分类再对症改写：

| 失败类型 | 判定特征（沿用 §6/§9 口径） | 改写动作 |
|---|---|---|
| 无区分力 | 样本内效果就在中性区间、条件没有分开样本 | 叠加：补一个能把样本分开的前提条件，收窄适用范围 |
| 时间外不重现 | 样本内有效应、独立时间外不延续 | 精简：删掉样本内偶然成立、时间外不稳的条件项 |
| 归因不清 | 多条经验重叠命中、无法单独评估 | 双轨：并行保留两个近似变体，各自独立累积曝光与 outcome |

改写产出的是**新版本经验**（新 `experience_id` / `experience_version`），必须回到 §5.4.1 的"候选→独立回评→validated"全流程重评，不复活旧结论、不覆盖旧证据。防自欺约束：改写候选由探索数据形成后、在确认性评估开始前登记（预注册）——改写本身由失败结果触发，不可能先于结果存在，故约束的是"确认评估前登记"而非"看结果前登记"；确认性评估只能用未参与本轮探索的新数据；改写代数与候选数上限写入版本化配置，不得在代码中硬编码。借鉴自两份设计的改造通道（叠加/精简/双轨并行）与"整批预注册、不得复用看过的数据、按谱系封顶"约束，但**不建谱系树/族预算实体**，改写谱系只用新版本指向旧版本的引用字段表达。可观测产物：改写候选数、各失败类型的改写成功率、改写版相对原版的增量。

> **落地记录（2026-11-19，增量标注来源）**：新增 `app/agent/chain/counterfactual_learning.py` +
> `counterfactual_config.json`（纯函数、零生产调用方、observe 档、回滚即删）。`classify_failure()`
> 按样本内效应/时间外效应/重叠命中数三类归因（无区分力/时间外不重现/归因不清），证据不足
> 或阈值未预注册→undecidable 不硬猜；`rewrite()` 对症叠加(补白名单前提)/精简(删不稳条件项)/
> 双轨(两独立变体)，产新版本草稿含 `parent_experience_id`/`split_rule` 引用旧版、主版本号递增、
> 回到 candidate/observe，condition 须过白名单校验，代数/候选数封顶走配置；`confirmation_eligible()`
> 防自欺——确认数据与探索数据零交集 + 候选在确认开始前登记才允许确认性评估。不复活旧结论、
> 不覆盖旧证据、不建谱系树。一次性探针 `tmp/probe_counterfactual_20261119.py` 41/41 PASS。

**⑤ 元学习：把"哪类经验在哪类情境有效"本身登记为经验（本方案独有）**

前四个引擎优化的是单条经验；但系统是否"越用越聪明"，取决于它能否把跨经验的规律沉淀下来。定义一类特殊经验 `meta_experience`：它不描述具体行情判断，而描述**经验选择本身的规律**（例如"当前波动状态下，带明确证伪条件的经验比泛化经验的有效率更高"）。其身份、版本、`evidence_status`/`consumption_mode` 与 §5.4.1/§8.2 完全复用，仅 `experience_type` 区分；它的 outcome 不是行情收益，而是"按该元规则选出的经验包"相对"无差别经验包"在组合级实验（§5.4.5）中的预测质量增量，同样受 §9 对照与 §9.3 δ 门槛约束，默认 `observe`。`meta_experience` 的准入、披露与灰度与普通经验一致，不得绕过对照实验直接生效。

对照臂口径：初期不新增第四臂，直接用 §9.1 现有两项实验的数据做**描述性差值**——在实验一/实验二的臂内，把经验包按 `meta_experience` 的元规则分组与"无差别选包"子组比较，报告预测质量差值与不确定性。该差值只作诊断与候选生成，**不构成因果有效结论，也不得单独驱动档位升级**；`meta_experience` 若要 `shadow`/`active`，仍须走 §9 的独立预注册对照（为其专门设臂）并通过 §9.3 门槛，§13 相应验收项同步约束。这样既不让元学习绕过门槛，也避免实验组合在起步阶段爆炸。

两份设计均未提供这一层，故其实现与判定口径为本方案新增，需单独预注册，不与具体 skill 或工具绑定。

> **实施落地（2026-11-19）**：新增 `chain/meta_learning.py` + `meta_learning_config.json`，纯函数、无生产调用方。
> `build_meta_candidate()` 把元规则（selection_condition 白名单谓词 + 情境 + 描述）包装成 meta_experience 草稿——仅 `experience_type='meta_experience'` 分型，身份/版本/evidence_status/consumption_mode 完全复用普通经验口径，默认 candidate/observe，selection_condition 须过 `condition_eval.structural_check`，不绑定任何 skill/工具。
> `descriptive_delta()` 算"按元规则选包 vs 无差别选包"的描述性差值，强制标注 `semantics=diagnostic_only/causal=False/drives_promotion=False`；描述性门槛（min_group_size/min_clusters）未预注册即不可用。
> `promotion_eligible()` 落实"描述性差值不构成因果结论、不得单独升档"：传入 ok 的描述性差值会被显式拒绝；升 shadow/active 必须有 meta 专属预注册对照（`promotion_experiment` 指向 experiment_config 已登记项且实验名一致）并通过 `decide_mode` §9.3 门槛，否则维持 observe。全部分型值/阈值走版本化配置，schema 不符/配置损坏 fail-closed。
> 一次性探针 `tmp/probe_meta_learning_20261119.py` 39/39 PASS。五引擎（①白名单谓词 ②情境匹配 ③冲突归组 ④反事实改写 ⑤元学习）机制层至此齐备，均零生产调用方、默认 observe，不绕过 §9。

五个引擎的关系：① 使经验可计算，② 决定用哪条，③ 决定多条如何共存，④ 使失败经验得到改写，⑤ 使选择规律本身可积累。前四者提升"单条与组合经验的效果"，⑤ 提升"选择经验的能力"——只有 ⑤ 闭环，系统才会随时间改变自己的检索与融合策略，而非仅增减条目。

**①②①附机制层落地（2026-11-19，用户「继续实施」）**：五个引擎中①/②/①附先行，
纯新增、无生产调用方、observe 档、回滚即删文件，均在 `app/agent/chain/`：

- `condition_whitelist.json` + `condition_eval.py`：① 版本化 key 白名单（情境帧六字段
  + claim 四字段）+ `(key,op,value)` 三元组谓词，op 仅 eq/in/gt/lt，三值
  applicable/not_applicable/unknown（缺值/越界/非法一律 unknown，不当 false）；组合只用
  显式 and/or 树，不解析表达式字符串、不执行代码。`structural_check` 只查结构可计算性
  不依赖取值。可观测：computable_ratio、越界拒绝数、白名单版本。
- `applicability.py`：② `evaluate()` 三值求值（collector 无帧→unknown、异常→storage_error
  单列，unknown 与存储故障分开）；`filter_and_rank()` 先按 condition 过滤（unknown 不进
  applicable）再按证据强度排序。①附 `needs_data_disposition()`：越界 key→pending_whitelist
  （时限 QD_NEEDS_DATA_PENDING_DAYS 默认30天）到期 retire、结构非法→reject、缺值 unknown
  不属 needs_data（避免僵尸 candidate，不新增第五种 evidence_status）。
- 关键边界：key/op 全走版本化配置不硬编码；时间基准由调用方显式传入（无时间词不猜）；
  不碰 DB/LLM、不做实际检索、不接生产调用方。
- 验证：一次性探针 `tmp/probe_condition_applicability_20261119.py` 40/40 PASS；修复两处
  真 bug——`in` 的列表 target 被单值类型检查误拒、`applicability` 未定义 UNKNOWN 常量。

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

**P0-4 来源口径的实现调整（2026-11-19）**：本节第 4 条原文列的是
“skill、会话、工具结果、成熟回评”四类来源；实际实现改为接 `_prefetch`
**本轮真正注入 LLM 的块**（`skill / memory / rag / case` 四类）。这是一次改进，
不是范围缩水，依据三条：

1. 2026-11-17 用户裁定原话：「经验库要接入的位置应该在 LLM 输入的前端和
   LLM 输出端，也就是捕获全文，分析里面用了哪些经验」。`_prefetch` 的 report
   与 `parts` 严格一一对应，掌握的是**实际注入了什么**，而非“可能用到什么”；
2. 工具预选层在 `_prefetch` **之前**且刻意剥掉注入块（模块头实测：带加工文本
   预选会把“问天气”带偏成点名 7 个股票工具、token +136%）⇒ 接预选层拿不到
   曝光事实，只能拿到检索输入；
3. 扫 `SKILL.md` 只能猜“可能用到什么”，还会碰 11-17「经验库不读 SKILL.md」
   的红线。

因此“工具结果 / 成熟回评”不进前端曝光的来源白名单：它们属于**产出侧**与
**回评侧**的事实，分别走 `qd_agent_traces`（工具留痕）和 `resolver → outcome`
的连接链，不是“本轮上下文里注入了哪条经验”。强行塞进前端白名单会制造
“看起来曝光了、其实没进上下文”的假事件。实现落在
`chain/experience_capture.py::capture_from_report`，来源白名单
`skill→skill_content / memory→session_span / rag→rag_chunk / case→historical_case`；
`resolve / prob_contract` 是运行时解析与契约注入，同样排除。所有项一律
`candidate / observe`，不经 T+N 回评不升 validated（§5.4 候选≠有效）。

### P1：建立基线与对照

1. 分别固定“回评更新效果”和“经验注入效果”的对照定义；
2. 预留离线回放接口；具备可复现决策快照时用回放，否则一律走前瞻评估；
3. 产出按日期、市场状态、经验项和 horizon 的预测结果；
4. 不允许仅比较总体平均值；
5. 先完成“无经验 / 当前统一经验包 / 等 token 中性对照”的组合级实验；
6. 建立经验曝光漏斗和 outcome 连接率基线，先确认数据完整再估效果；
7. 历史 replay 只使用决策时点可用的经验/标签；不能重建时只做登记后的前瞻评估。

**P1 机制层落地（2026-11-19）**：P0 代码侧已齐、K 线断供已修复（outcome 持续产出），
但数据侧三前置未满足——曝光↔结果连接率 0（时间差，会随新决策成熟自愈）、概率
coverage 0（Brier 无数据）、独立交易日簇不足（§9.3 实测 26 日、MDE≈15.9pp，
不足以判 5~10pp）。故先落地不依赖数据成熟的机制层，实验层等 links>0 且概率
供给起来再跑。三个新增模块均在 `app/agent/chain/`，纯新增、无生产调用方、
observe 档，回滚即删文件：

- `experiment_config.py` + `experiment_config.json`：§9.1 两套对照的判定策略。
  配置 schema 化、所有阈值默认 `null`（未预注册）；gate 在未定档 / 端点不符 /
  证据缺失 / schema 不符时一律 `undecidable` 维持当前档位（fail-closed），
  达标一次只升一档 observe→shadow→active。δ_* / mde / n_min 等由策略负责人
  实验前一次定档写入配置，代码内不硬编码数值（§9.3）。
- `baseline_report.py`：§11 P1-3/4/6 只读报表，按 horizon/condition_key/
  skill_id/as_of 分层出方向成功率与独立交易日簇，曝光漏斗按 `exposure_stage`
  分组，连接率基线如实报 `data_complete=false`。不写库、不新增表。
- `replay.py`：§11 P1-2/7 离线成对回放接口预留 + PIT 过滤。`pit_available` 要求
  经验的 `source_available_at` / `registered_at` 严格早于决策 `created_at`，
  缺时间即不可用；`replay_pair` 只生成冻结输入契约、不执行，不接生产调用方。

验证：一次性探针 `tmp/probe_p1_mechanism_20261119.py` 35/35 PASS。实验层（第 5 条
组合级对照）仍未启动，需等数据前置满足。

**自动升档执行链落地（2026-11-19，承接上条）**：`experiment_config.decide_mode`
原是零调用方的纯判定；本轮补上"判定→落库→留痕"执行链，落实 §9.3"门槛达标逐级
自动生效、人只保留否决/撤回权"与 §8.3 灰度回滚。用户裁定"完成设计文档、自动启用
而非人工盯着；巡检待全链路走完再评估"。新增 `app/agent/chain/consumption_policy.py`
（纯新增、无生产调用方、observe 档、回滚即删文件）：

- `apply_decision()` 从库读当前档位→人工否决拦截→调 `decide_mode`→达标才写回
  `qd_agent_experiences.consumption_mode`（一次只升一档）→全过程留痕。任何 DB 异常
  fail-soft 返回 undecidable，配置未预注册/版本不符/证据不全一律 fail-closed 维持原档。
- `human_override()` 三动作 `veto`/`revoke_veto`/`demote`，留痕可观测；否决优先于自动升档。
- 自带 additive 留痕表 `qd_agent_consumption_decisions`（幂等 sha、不新增迁移文件），
  `decision_ledger()` 只读查询；不硬编码阈值，全部来自版本化 `experiment_config`。
- 当前配置阈值全 null（未预注册）⇒ 今天跑只会 `undecidable/not_preregistered` 维持
  observe，真升档要等策略负责人预注册 + 组合级对照达标 + 数据三前置满足。
- 行取值兼容 `RealDictRow`（`app.utils.db` 的连接 cursor 工厂是 RealDictCursor，
  不支持整数下标）——`_load_modes`/`_active_vetoes`/`decision_ledger` 均已显式兼容。

验证：一次性探针 `tmp/probe_consumption_policy_20261119.py` 25/25 PASS（fail-closed
六分支、单级升档、否决拦截/撤销、降档、留痕幂等、禁用开关、guard 字段零命中），
临时数据已清理。消费层（档位真正改变行为）仍属 P2/P3 灰度，本轮未碰。

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

> **验收状态说明（2026-11-20 实测）**：下列 32 项保持未勾选，表示「尚未逐项走完正式验收流程」，
> 不代表机制未落地。按当前证据分三类：①**机制层已实现且有落库证据**（snapshot 表含
> `snapshot_version/contract_version/snapshot_sha`；outcomes 含 `predicted_probability`/
> `brier_score`/`experiment_arm`/`experience_bundle_sha`/`condition_key`/`skill_version`；
> stats 含 `eligible_exposures`/`success_count`/`prediction_success_rate`/`confidence_interval`；
> 经验目录 173 条）——对应"字段/结构/只读获取/分字段记录/漏斗/纪律固化"类条目；
> ②**数据前置未达、按 §9.3/§9.4 维持 observe**（概率字段 3/967、Brier 0、独立交易日簇 43
> 低于 n_min=60、两类预注册对照未完成、未全量启用）——对应"受控对照结论/升档/全量启用"类条目，
> 是如实反映而非死锁；③**尚未逐项复核**——正式验收签字前仍需按证据逐条确认，故不预先勾选。
> 完工判定见 §28。

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

## 16. 复核后口径补齐（来源：WorkBuddy 会话 2026-11-19 22:0x，增量追加）

本节记录 §5.1 / §5.4.2 / §10.3 的当前落地形态，作为复核后代码与设计对齐的基线：

- **§5.1 决策快照**：每次决策在 claim 落库后冻结决策头与逐 claim 快照，算 canonical
  SHA-256；快照本体落 `qd_agent_decision_snapshots`（decision/outcome 两种 scope），
  回评时优先使用已冻结快照，未冻结的历史决策才在回评时构造。快照自带 hash 校验，
  不接受事后改写；prompt/工具链/经验包版本取决策当场曝光事件，缺失留空不猜。
- **§5.4.2 统计字段**：`qd_agent_experience_stats` 落 eligible_exposures、
  actual_injections、matured_outcomes、prediction_eligible_outcomes、
  execution_eligible_outcomes、excluded_outcomes + reasons、success/failure/partial、
  prediction_success_rate、brier_score、calibration_error、applicability_conditions、
  confidence_interval、effective_weight、weight_policy_version、last_matured_at；
  由曝光/link/outcome 真值重算，权重与 CI 在策略未预注册时保持 NULL。
- **§10.3 领域适配**：`domain_adapter` 注册表是共享层唯一的领域能力来源，首期仅
  finance；adapter 缺失或 schema 不兼容时记 coverage/exclusion，不用 finance 默认
  resolver 兜底，也不当负面 outcome/经验失败。金融专属字段仍只进 outcome.adapter_data。
- **§5.2/§5.3 口径**：`outcome_policy_version` 含 resolver/数据口径版本；
  `truncated` 进 coverage 且不得定论；direction 类概率目标按契约 v2 接受
  probability_target=up|down（见 §5.3 第 2 条），不按字面等值误判 mismatch。

实验层（预注册对照、受控结论、全量启用）仍按数据前置与 §9.3 维持 observe，未因本节变更启动。

## 17. 历史 T+n 数据链补齐（来源：WorkBuddy 会话 2026-11-19 22:4x，增量追加）

用户拍板范围 **A+B**：只回灌已有真值 + 补判到期 claim；不做 LLM 概率/经验抽取（C/D 暂缓），
不改技能层，不伪造历史曝光。证据：行情库 `kline_1D_2026` 111.9 万行、`kline_1m_2026`
1.84 亿行、`kline_index_5m` 6.36 万行均覆盖至 11-19；业务库已有 1613 条 resolution
（1451 条 hit/miss/partial，actual_as_of 10-09~11-17），但 outcome 只有 400 条，
缺口是**入账**而非行情复现。

- **A 回灌**：1213 条未入账 resolution 中 1056 条有真值 verdict，逐条复用
  `sync_outcome_from_resolution()` 入账。净新增 853 条 outcome（400→1253）；
  285 条因同一 prediction 已有 outcome 被按唯一键
  `(prediction_id, outcome_policy_version)` 合并（ON CONFLICT 只更新 canonical/resolved_at），
  属重复判定的预期去重，不是数据丢失。
- **B 补判**：`query_due_claims(500)` 到期 82 条，`resolve_claim(exec_date=数据日,
  use_judge=False)` 全部落库，80 resolved / 2 undecidable / 0 error，82 条均同步 outcome。
- **基准日**：34 条决策的 UTC 日期 ≠ 北京数据日，其中 33 条有关联 claim，逐条清单见
  `docs/历史T_n回灌跨日决策清单_20261119.md`。回灌/补判一律按 `current_data_day()`
  从北京保存时间换算数据日；`sync_outcome_from_resolution()` 新增 additive 可选参数
  `as_of_override`（默认 None，不改既有行为），并把写 outcome 的 `as_of` 从重复计算的
  UTC date 改为统一使用 `dec_date`。已存在的 20 条跨日 outcome 按同一口径修正，
  修正后跨日 outcome 基准日错误 0。跨日决策的 pending claim 因 `due_date` 仍按旧基准
  生成，本轮**不补判**（宁漏勿错）。
- **现状边界**：`coverage_funnel()` = outcomes 1253 / events 8 / links 0 / experiences 10 /
  claims_with_p 4（probability_coverage 0.0066）/ brier 0。概率与经验抽取属 C/D，
  未授权不实施；曝光全在 11-19 生成、T+1 未到期，links=0 是时间未到，不补造。
- **回滚**：回灌 outcome_id 清单 `tmp/backfill_history_outcome_ids_20261119.txt`、
  补判 resolution_id 清单 `tmp/backfill_history_resolution_ids_20261119.txt`、
  as_of 回滚清单 `tmp/fix_cross_day_asof_rollback_20261119.txt`。
  生产代码改动仅一处 additive 参数与一处赋值统一，删参数并改回原赋值即还原。
  需重启后端让常驻 worker 加载新代码。
- **人工抽查后的修正（同日）**：逐条对照源表确认搬运字段无误（verdict/score/actual/
  as_of 一致，288 对多 resolution verdict 零冲突且版本隔离，B 补判 82 条零 magnitude、
  2 条 undecidable 有据）。但发现 A 回灌筛 SQL 只判 verdict、未排 claim_type，把
  **307 条 magnitude** 历史 verdict 入账（既有 400 条基线中 magnitude=0），违反
  §12 的 magnitude 全链停用裁定。已按用户拍板清理：删 307 条 magnitude outcome +
  307 条 outcome scope 快照（links 恒 0），outcomes 1253→946，magnitude 残留 0；
  回滚清单 `tmp/clean_magnitude_outcomes_rollback_20261119.json`。核对剩余 186 条
  未入账非 magnitude 真值全部是同 claim 已有 outcome 的重复判定（真正漏账 0）。
  `coverage_funnel` = outcomes 946 / events 8 / links 0 / experiences 10 /
  claims_with_p 4 / brier 0。

## 18. C/D 数据链补齐决策（来源：WorkBuddy 会话 2026-11-19 23:0x，增量追加）

**C（概率/Brier）——不抽概率，如实记缺口**。用户拍板。查证结论：历史 602 条
  direction claim 的 `source_quote` 含「把握XX%」仅 4 条（P0 已抽为
  heuristic_confidence）、含 `P(T+N涨)=` 为 0；666 条决策答案原文含 `P(T+N涨)=`
  也仅 2 条（decision 859/860，均为 11-19 新记录），且归属不成立——859 同段写
  「T+1中性(证据不足)」且有两个不同标的的 P() 值（全文不唯一，规则正确拒绝关联），
  860 的 P() 来自「上轮 get_watchlist、trade_date 2026-11-18」属上一交易日旧预测，
  给 11-19 claim 打分是视界错配。`qd_agent_decision_calibration` 表 0 行，行情库
  无概率留存表。**结论**：历史原文不存在可供 Brier 的合规标定概率，LLM 只能抽原文
  已写下的东西，凭语义估概率＝编造，撞 §5.4.2「自然语言 confidence 不得自动填概率」
  与「宁漏勿错」。代码无需改：`sync_outcome_from_resolution` 已在 verdict 为真值但
  p 缺失时写 `no_probability`（漏斗 939 条），heuristic 不进 Brier，`claims_calibrated=0`
  即合格概率覆盖缺口 100%。此缺口是数据供给问题（§550），不据此宣布概率质量达标。

**D（历史案例经验候选）——已授权，范围收窄**。用户拍板：只从 136 条有
  correct/incorrect 标签或有 failure_modes 的 `qd_agent_cases` 抽取（82 有标签 +
  54 仅失败模式）。cases 未存完整回答原文，只有任务摘要 + 工具链骨架(plan_digest) +
  成败标签 + 失败模式 ⇒ 只抽「哪类任务用哪组工具链→成功/失败」的**流程经验**，
  不抽预测经验。全部 candidate/observe 落经验目录供未来注入候选，**不回填历史曝光
  事件/links**。脚本 `tmp/extract_case_experience_20261119.py` 走 `complete_direct`
  （自己控 attempts/gap，绕开 mimo 硬编码 5 次重试），防幻觉三查：工具名必须来自原文
  plan_digest、任务类型从摘要归纳、失败模式引用原文；max_tokens=1400（600 会截断
  JSON），解析失败重试一次；断点续跑状态 `tmp/case_experience_state_20261119.json`。
  **实测**：136 条候选 → 写入 127 条（9 条无实质/幻觉被拒，0 失败，约 13 分钟）。
  核验 `tmp/verify_case_experience_20261119.py`：127 条全 candidate/observe/finance、
  来源版本 `qd_agent_cases@20261119`、来源引用无缺失；632 个工具条目逐条对回原文
  plan_digest **零幻觉**；funnel experiences 10→137，events 仍 8、links 仍 0（曝光
  未回填）。回滚：按 `experience_id LIKE 'caseflow-%'` 删除 127 行即可，既有 10 条
  经验与全部业务表未动；脚本/状态文件均在 tmp/。

## §19 未来决策的标定概率注入（23:3x 落地，2026-11-19）

**缘起**：用户提出「走消息注入/上下文/前端注入能否让 Brier 有数」。查证结论——
三条注入路径本质是同一件事：`qd_service._prefetch` 把块拼到 task 前缀（mimoagent
系统提示词在第三方包、项目无可维护 system prompt；前端消息也走同一 `_prefetch`）。
而 `qd_service.py` **早已有** `[预测概率契约]` 注入块，要求「工具返回过 T+1 上涨概率
就原样写 P(T+1涨)=0.62；没给不写、禁止自拟」。历史 answer 几乎无 P() 的根因不是没注入，
而是 **stock-analyzer 把 p_up 改写成中文白话（"次日上涨参考"），工具从不输出 P() 字面**，
模型无文本可抄。

**用户拍板**：① Brier 评估主体 = 系统 `predict_next_day` 标定分（非 mimo 自估置信，
后者撞「confidence 不得冒充概率」红线）；② 先做未来注入，历史 946 条不回填
（回填需按决策日截断重算 p_up，否则前视偏差污染回测，另案）。

**隔离原则（用户复核）**：skill 可随时换/升级/删除，经验须恒定有效 ⇒ 注入概率源
固定 `app/watchlist/predict.py` 冻结基线（C3P `p_base_of` 同款），**不碰 skills/**、
不引用任何 skill 名/产物字段/中文信号串/任务常量。基线独立于所有 skill，skill 更换
不影响标定口径。

**改动（仅 `qd_service.py`，additive + env 关）**：
- 新增 `_pup_inject_block(entity_code)`：env `QD_INJECT_P_UP` 默认关（0/空=旁路）；
  仅单标的（6 位代码）时调 `c3p.p_base_of` 取当日冻结基线 p_up；取不到/异常
  fail-open 返回空串、不兜底 0.5；只补 T+1 唯一标定档。注入文本标注口径
  `calibrated`、基准=决策当日，要求看多时原样写 P(T+1涨)=三位小数（四位会被
  `_PUP_RE` 的 `0?\.\d{1,3}` 截断）。
- `_prefetch` 在概率契约块后调用该函数，注入块记入 `blocks`（kind=p_up_ref）留痕。
- 多标的不注入（entity_code 单值；避免抽取器 P() 全文唯一约束冲突）。

**实测（纯函数 + 真 LLM，零写库）**：
- 注入块四态：env 关/非法码=空串旁路；env 开产出带 P(T+1涨)=0.620 + calibrated 口径。
- 真 LLM（`complete_direct`/deepseek-flash）：模型看到注入块后在中文白话回答末尾
  **原样写 `P(T+1涨)=0.620`**——注入压过「禁止写 p_up 内部字段名」技能规范，且两者不冲突。
- 端到端：注入 → answer → 规则/LLM 抽取器均认 `p_source=calibrated`/`p_target=up`/
  `p_horizon=T+1` → 契约 `validate_contract` 判 `brier_eligible=True`、`coverage_gap=[]`。
- `p_base_of` 耗时：冷启 1.74s（日历/DB 初始化），热路径 0.02s；生产常驻进程已预热。

**边界与回滚**：默认 env 关 = 与改动前完全一致；`QD_INJECT_P_UP=1` 才启用。
零数据结构改动、零 skill 改动。回滚 = env 归零即还原。要使历史也有 Brier，
需另案按决策日截断重算 p_up 回填 claim（前视偏差风险，本轮未做）。
需重启后端让常驻 worker 加载新代码。

## §20 未实施/未接线盘点 + §9.4 功效审计落地（23:4x，2026-11-19）

**缘起**：用户问「现在可以完善文档中未实施/未接线的部分了吗」。先按证据链盘点，
不盲接。实测数据前置与复核时**无变化**：events 8 / links 0 / outcomes 946 /
claims_calibrated 0 / brier 0 / 独立簇不足。8 条曝光全在 11-19 06:58 UTC
（北京 14:58）生成，links=0 是 T+1 未到期的**时间差**，非断链；历史概率仍 0
（§19 注入只对未来生效）。

**三类盘点结论**：
1. **已落地，无需再做**：机制层全部 + 上一轮三项口径偏离（决策快照/经验统计
   字段落库且 `recompute_experience_stats` 已接 outcome 写链 evaluation_store.py:983
   / DomainAdapter）+ 11 健壮性缺陷。
2. **"零生产调用方"消费层——不该现在接**：五引擎/`consumption_policy`/`rollout`/
   `effective_weight` 等约 10 模块仍零调用方。这是用户拍板的设计取舍；数据三前置
   未变，现在接等于让机制基于空数据判定，违背 §9.3「独立簇未达 n_min 或概率未
   补齐一律 observe」。不是"没做完"，是"时候没到"。
3. **真正未实施且可补：仅 §9.4 功效审计**。其余未落地项（§6.1 净超额/回撤/
   Sortino、§13-1152 基准收益与交易成本、§5.4.6 效果报告）属"进入交易应用才需要"
   或数据源为 0，现在补只能报"不可判定"，空转。

**§9.4 落地（用户拍板"只补功效审计"）**：`experiment_config.decide_mode` 原有
n_min/n_cap/max_looks/CI 粗判但缺三样——TOST 等价检验、alpha spending/Holm
多次 look 校正、n_min ∝ σ²/mde² 公式校验。新增 `chain/power_analysis.py` +
`power_analysis.json`，纯函数、零生产调用方、默认 observe、不碰 skill：
- `tost_equivalent()`：CI 完全落在 ±margin 内判 equivalent(no_gain)，避免"证不出
  有效"永久 pending；三态 equivalent/not_equivalent/undecidable。
- `obrien_fleming_alpha()` + `holm_stepdown()`：两种预注册多次 look 族错误率控制；
  OBF α 随 look 单调不减、末 look≈α，Holm 步降首次不拒即全停。
- `required_n()`：n ∝ σ²/mde²（mde 减半 → n≈4×），design one/two_sample 切换。
- `audit_from_config()`：配置驱动统一入口，全 null=未预注册→undecidable；
  schema 不符/缺 look/缺 p 值/畸形参数一律 fail-closed。
- 全部阈值来自版本化配置，**代码零硬编码业务值**；正态分布用标准库 math.erf
  二分求逆，不引 scipy。

**验证**：一次性探针 `tmp/probe_power_analysis_20261119.py` **40/40 PASS**
（TOST 三态+7 畸形、OBF 单调/末look/5 畸形、Holm 步降/4 畸形、n 公式+比例+
畸形、配置默认未预注册、临时预注册全链路、schema 不符），零写库。

**links 只读评估（用户"评估能否安全加速"）**：结论**不需要也不能加速**——
`sync_outcome`→`link_outcomes`（evaluation_store.py:971）全自动，EvalWorker 盘后
`auto_evaluate→resolve_due_claims→sync_outcome` 链路完整，明天 T+1 到期即自然出
link；人工催跑只会拿未成熟真值，违"宁漏勿错"。3 条 claims=0 曝光永不产 link
（预期）。**既有缺口**：8 条曝光 `experience_ids` 全为 `[]` ⇒ 明天即使出 link，
experience_id 也为空、归因不到具体经验（曝光时未记注入经验 ID）——属另一案，
本轮未改（不伪造历史留痕）。

**回滚**：删 `power_analysis.py` + `.json` 即还原，零数据结构改动、零 skill 改动。
需重启后端让常驻 worker 加载新代码。

**2026-11-20 更正（来源：本会话「修正两个额外发现」）**：上文 §18「events 仍 8、
links 仍 0（曝光未回填）」与 §20「8 条曝光 `experience_ids` 全为 `[]`、
归因不到具体经验」已过期。当日已按确定性判据手工回填 4 条曝光（decision
857/858/859/860，共 10 个 `cap-*` ID），另 4 条（848~851）因接线前无经验可证
按「宁漏勿错」保持空。现况：events 8 = 4 非空 + 4 空；links 仍 0（T+1 未到期）。
回填判据、备份与复核见 `.workbuddy/memory/2026-11-20.md` 与
`tmp/verify_experience_ids_repair_20261120.txt`。另一个额外发现
`qd_agent_consumption_decisions` 8 行 11-19 12:35 探针残留（evidence 含伪造
`n_clusters=100`）已于同日按四重命中条件精确删除，备份
`tmp/clean_consumption_probe_rows_20261120_before.json`，该表现 0 行；
该删除只清假证据，不产生任何升档结论。零生产代码改动、零 git。

## §22 可实施/可接线部分的落地（2026-11-20，增量追加）

**已落地：§5.4.6 逐经验效果报告（纯只读）**。新增
`chain/experience_effect_report.py` + 只读路由
`GET /api/agent-analysis/effect-report`（`routes/agent_analysis.py`，
`login_required`）。报告维度按 §5.4.6 全量给出：曝光/合格/成熟可评价/排除原因、
弱引用比例、净超额与风险差、Brier 差、置信区间、日期/标的簇、token 与延迟差、
适用条件、归因丢位数。**没有对照臂就不报收益/风险差；没有标定概率就不报
Brier 差；数据不足统一标「证据不足」，无一处称「有效/有害」。**

**接线范围（AST 复扫 `tmp/probe_tn_wiring_20261120_after.txt`）**：本模块把
`baseline_report`（曝光漏斗）、`experiment_config`（预注册状态）、
`power_analysis`（§9.4 功效审计）、`rollout_policy`（灰度档位状态）、
`consumption_policy`（开关状态；只读查询不触发 DDL）、`replay`（PIT 可用性）
六个原零调用方模块接到真实只读路径上。`independent_effect` 不因此产生：报告
只观察，不升档、不写库。

**PIT 实测新发现**：用 `replay.pit_available` 对 11-20 手工回填的 10 条经验 ID
复核，全部 `available_at_or_after_decision` —— 这些 ID 是事后登记后回填，
**PIT 无效，不得计入合格曝光**。报告据此把 `qualified_exposures` 计为 0，
另设 `answer_available_exposures` 保留原始事实；`pit_violations` /
`pit_reasons` 显式披露。回填本身仍保留用于曝光可追溯，不参与任何效果估计。

**未接线且保持未接线（按设计，不是遗漏）**：
- `conflict_resolution`：需要结构化的经验 condition/conclusion，现存
  `normalized_content` 只有 excerpt，喂入即编造；
- `counterfactual_learning` / `meta_learning`：依赖已验证经验与受控对照，
  当前 validated=0 / links=0；
- `extraction_funnel` / `source_budget` / `ingestion_quality`：属于摄取时
  闸门，需在抽取/入库调用点接线，事后回放只能得到 `spot_check_missing`，
  无信息量；
- P1 实验启动、P2 影子注入、P3 灰度、P4 行为门禁与交易指标（净超额收益/
  回撤/Sortino、基准收益、交易成本）：前置仍是 links=0、标定概率 0、
  预注册阈值全 null，按 §9.3 一律 observe，不硬接。

**验证**：一次性探针 `tmp/probe_experience_effect_report_20261120.py`
全绿（断言维度齐全、无对照/无 Brier 时数值为 null 且带原因、弱引用标记
`causal=False`、调用前后五张表行数不变、路由已注册）。全量 pytest 本轮被
沙箱 bulk-delete 守卫在 pytest 临时目录清理阶段中断（非测试失败），未拿到
完整汇总，已在当日 memory 记录，未重试同类命令。

**回滚**：删 `chain/experience_effect_report.py`、撤销
`routes/agent_analysis.py` 的 import 与路由块即还原；零表结构改动、零 skill
改动。前面两处修复的回滚分别是
`tmp/repair_experience_ids_20261120.py --rollback` 与
`tmp/clean_consumption_probe_rows_20261120.py --rollback`。

## §23 · P2 影子反馈最小落地：shadow 经验注入（2026-11-20，用户拍板 shadow 口径）

**背景**：§20 盘点确认 P2（影子注入）未实施、`links=0` 且短期内不会有带
`experience_id` 的有效 link（历史 outcome 的决策早于曝光功能上线，交集为 0；
经验库 `active=0`，从未发生真实经验注入）。用户拍板按 **shadow 口径**实施
最小注入：经验保持 `candidate/observe` 态注入决策上下文，只收集
「曝光→outcome」配对数据，不宣称有效、不参与消费、不进升档判定。

**实现**（生产代码 3 处 + 新增 2 文件）：

1. 新增 `chain/shadow_injection.py`：纯查询 + 确定性选择。配置
   `shadow_injection_config.json`（版本化 `shadow-injection-v1`）白名单 11 条
   `cap-*` 候选经验；`domain=finance` 闸门；`max_inject=2 / max_chars=600`；
   选择顺序 = condition_key 命中实体优先 + **按日期轮转**（`toordinal % len`
   ，同日确定、跨日摊开曝光面）；任何异常/配置损坏返回空（fail-closed）。
   开关：配置 `enabled` ∧ 环境变量 `QD_SHADOW_INJECT`（默认开，`=0` 关）。
2. `qd_service._prefetch`：金融域决策拼装期调用选择器，注入
   `[经验影子注入]` 块（块头明示 observe 态、未验证、禁止照抄），
   `blocks` 记 `kind=experience_shadow`，report 新增 `shadow_experiences`。
3. 曝光写入（`_run_sync` 内）：shadow 项并入 `exposure_items`
   （`kind=experience_shadow` + `experience_id/content_sha/source_ref/position`）、
   `experience_ids`、`bundle_sha`。
4. `evaluation_store`：新增纯函数 `shadow_experience_ids(items)`；
   `sync_outcome_from_resolution` 反查曝光事件时同时取 `exposure_items`；
   归因对象取 `experience_ids` 中首个属于 shadow 集合的 ID（无 shadow 项才
   回退首条捕获项），`experiment_arm` 随归因对象是否为 shadow 项判定。

**纪律**：shadow 经验的 link 归因臂显式标 `shadow`，效果报告与未来消费
判定可按臂区分；经验仍 `candidate/observe`，不改升档规则；白名单外的
137-11 条经验不注入。注入发生在 prompt 组装前 ⇒ PIT 天然有效
（`registered_at < 决策时刻`）。

**验证**：`tmp/probe_shadow_inject_20261120.py` 18 项全 PASS（配置加载/
开关 env 双向/白名单过滤/域过滤/轮转确定性/块格式/`shadow_experience_ids`
三形态/两处接线源码断言）；`py_compile` 通过；qd_service 导入冒烟通过；
prefetch 相关 6 个既有测试文件 60 例全过（`tmp/pytest_shadow_subset_20261120.txt`）。

**回滚**：`QD_SHADOW_INJECT=0` 或配置 `enabled=false` 即停（不删代码）；
彻底还原 = 删 `shadow_injection.py` + 配置、撤 `qd_service.py` 三处
（shadow 块段 / report 键 / 曝光合并段）与 `evaluation_store.py` 三处
（SELECT 扩列 / shadow 项优先归因 / `_arm`）即可。零表结构改动、零 skill 改动。

**预期链路**：下一笔金融域决策（后端重启加载新代码后）⇒ 曝光事件带
shadow `experience_ids` ⇒ T+1 成熟 ⇒ 首批 `experiment_arm=shadow` 的有效
PIT link ⇒ 统计与对照开始积累。

## §24 · 预注册阈值 + 升档驱动接线（2026-11-20，用户拍板「阈值初稿+接线」）

**背景**：§23 后链路缺口是——`apply_decision` 判定/写档执行链零生产调用方
（升档不会自动发生）、`experiment_config.json` 两实验全字段 null（未预注册，
decide_mode 恒 `not_preregistered`）。升档卡在「预注册」而非数据；且配置纪律
明文 δ_* 不得从实验结果反推 ⇒ 预注册必须落在数据之前。

**预注册初稿（策略负责人 2026-11-20 拍板口径，改数字 = 重新预注册）**：
端点两实验均取 `direction_success_rate`——brier 需标定概率（当前 0 条）、
net_excess_return 需成本/基准计算（未实施），唯一现在可算。
- `experience_injection`：δ_pred=0.08（经验注入要 ≥8pp 方向成功率提升才值得
  改消费行为）、mde=0.08、power=0.8、n_min=60、n_cap=600、max_looks=4、
  milestones=[60,150,300,600]。
- `feedback_iteration`：δ_pred=0.05（反馈链覆盖全部决策、效应预期更小）、
  n_min=80、n_cap=800、max_looks=4、milestones=[80,200,400,800]。

**升档驱动**：新增 `chain/promotion_runner.py`（证据计算 + 周期执行）：
- 注入臂 = 该经验被 links 关联的成熟方向 outcome 按 decision 聚簇；对照臂 =
  同一时间窗内、未注入该经验的 decision 的成熟方向 outcome（以当时落库的
  `exposure_items` 中 shadow 项是否含该 ID 判定，PIT 安全；按日轮转在同窗内
  天然产生对照）；臂均值差 ±1.96×Wald SE 为 95% CI；n_clusters=min(两臂簇数)。
- **look 纪律**：looks = milestones 中 ≤n_clusters 的个数（alpha-spending 风格），
  不是每日运行次数——盘后天天跑不吃 max_looks 配额。
- 簇数 < n_min 不调 apply_decision、不留痕（避免台账日刷）；判定/写档/否决
  拦截全在 `apply_decision` 既有链内（fail-closed）。
- `feedback_iteration` 对照臂构成未定稿，本模块不计算不驱动（宁缺毋错）。
- 接线：`evaluator.auto_evaluate` 在 weight_feed 后、报告前插
  `run_promotion_check()`（独立 try + `_worker_health["last_promotion"]`）。

**验证**：`tmp/probe_promotion_driver_20261120.py` 15 项全 PASS——两实验
registration registered=True、decide_mode 五分支行为（below_n_min 维持 /
threshold_met 单级升 shadow / looks 超 4 capped / 端点不匹配 undecidable）、
looks 里程碑映射、evidence 形状、runner 实跑（当前 links=0 ⇒ checked=0，
零台账写入）；v41 EvalWorker 自愈 27 例全过。

**回滚**：`experiment_config.json` 阈值改回 null 即回到未预注册态（驱动整步
空转）；删 `promotion_runner.py` + 撤 `evaluator.py` 一处插入即撤驱动。
零表结构改动、零 skill 改动。

**预期**：今天盘后首批 shadow link 落地后，驱动开始每日累计证据；注入臂
簇数达 60 起进入判定，达标自动升 shadow→active（单级、可否决）。

## §25 数据链推进：p_up 注入启用 + shadow 域判定兜底（2026-11-20 上午）

**背景**：09:19 后端重启后首批实时决策（861/862）已产生带 experience_ids 的
曝光事件，但全部来自**输出端捕获**（kind=skill_content/rag_chunk/session_span），
`experience_shadow` 条目为零——shadow 注入未触发。

**根因**（实测 861/862）：`qd_service._prefetch` 的领域判定只取
`resolve_info.domain`（resolver 由实体类型倒推）。861（大盘资金流查询，
resolver 未跑出实体）与 862（「上海新阳」resolver 解析失败，resolve_status=none）
⇒ `_dom=""` ⇒ 被 `select_for_decision` 的 domain=finance 闸门拦掉。而决策行
`qd_agent_decisions.domain=finance` 来自 `claims.classify_domain(query, answer)`
——注入侧与入库侧领域判定**不同源**。

**修复**（`qd_service._prefetch`，1 处）：`_dom` 为空时用
`classify_domain(message)`（与决策行同源分类器、只喂 query）兜底。861 类查询
（含行情/资金流词）兜为 finance，shadow 与 p_up 契约块恢复触发；862 类（纯
股票名、无领域词、resolver 又 miss）query 侧仍判 general——**根因在 resolver
实体解析缺失，属另案**，此处不扩改动面。

**p_up 注入启用**：`.env` 新增 `QD_INJECT_P_UP=1`（备份
`tmp/env_backup_20261120_pre_pup.env`）。此前全库 `claims_calibrated=0`
（仅 4 条 heuristic_confidence），Brier 链路空转；启用后单标的金融决策注入
冻结基线 `P(T+1涨)=0.620` 式标定概率（实测 `p_base_of("002594")=0.6425`
当日可用），其 T+1 成熟起产生首批 Brier 样本。

**全链健康探针**（`tmp/probe_chain_health_20261120.py`，9 项 8 PASS）：
冻结基线 PASS / p_up 开关 PASS / shadow 开关+选择 PASS / 预注册 2/2 PASS /
升档驱动空转安全 PASS / shadow 标记函数 PASS / 决策快照与漏斗 PASS；
唯一 GAP 即 862 类域判定边界（已定性）。

**边界**：当日实测曝光事件 11 条、experiences 142；861/862 的 experience_ids
均为输出端捕获（同秒登记，PIT 边界待 T+1 复核），首批 `experience_shadow`
注入事件待重启后下一笔金融决策产生。

**回滚**：`QD_INJECT_P_UP=0` 或删行；`_prefetch` 兜底块删除即还原（此前
60 例 prefetch 测试全过，无行为回归）。零表结构改动、零 skill 改动。

**需再次重启后端**：09:19 启动的进程不含本节修复与 env 开关。

## §26 名称解析收缩兜底 + 捕获侧闭环盘点（2026-11-20 09:45）

**resolver 根因修复**（`resolvers/stock.py`，862 类缺口的真正源头）：
名称解析链是「片段清洗 → 整词查库」，实测「上海新阳76.8的本」洗成
「上海新阳76.8」整词 0 命中、「比亚迪该如何进攻和防守」全 CJK 整词 0 命中
⇒ 静默返回 None ⇒ 实体/领域全空 ⇒ shadow 与 p_up 契约块都被拦。
新增**片段级前缀收缩兜底**：取片段开头连续汉字串逐级右缩（`_FALLBACK_MAX_QUERIES=8`
上限），只收「候选名 == 收缩词」的**精确命中**；多候选继续缩不判歧义
——兜底路径绝不触发反问（避免 2026-10-16 类误拦整轮执行）。
实测 8 例全过：上海新阳→300236、比亚迪→002594、贵州茅台/002594/双标的/
无实体/代码类闸门/「平安」歧义反问全部行为正确；resolver 专项 3 例 +
prefetch 相关 60 例全过。

**捕获侧防膨胀盘点**（结论：已有结构化防线，预算闸不接）：
① 捕获 ID 是内容寻址 `cap-{kind}-{sha[:16]}` + `ON CONFLICT DO NOTHING`
——同内容天然幂等，膨胀率 = 新内容率（skill 卡片稳定复用同一 ID）；
② `source_budget.json` 已存在且其 taxonomy（failure_rewrite / user_correction /
success_review / free_text）属**答案抽取/反事实闭环**（`counterfactual_learning`
是唯一生产者，尚未运行）——与 cap-* 捕获 taxonomy（skill_content / rag_chunk /
session_span）不同源，混接即破坏「priority 是文档固定契约」；
③ `ingestion_quality.quality_gate` 需 citation/falsifier 结构（升档路径专用，
cap-* 无这些字段，硬接=全拒或编造）；`extraction_funnel` 需条件白名单配置
（抽取路径专用）。三者按 §9.3 保持 observe，接线随对应管线上线。
真正的长期防线是**候选退役路径**（无 link 的 cap-* 定期 retired），归维护层设计。

**边界**：本轮 `stock.py` 沿用该文件既有注释风格（新增 shrink 逻辑带说明注释），
与「app/agent 不写注释」纪律的适用范围问题留待用户裁定是否回改。

**回滚**：删 `_shrink_hit`/`_CJK_LEAD_RE`/`_FALLBACK_MAX_QUERIES` 与
名称循环中的兜底分支即还原（名称解析回到整词行为）。零表结构、零 skill、无 git。

## §27 有效经验链两处语义修复 + 设计文档对齐（2026-11-20 13:41-14:00）

**背景**：用户目标从「增加经验条数」澄清为「增加**有效**经验」（拿到真实
link、可进升档判定）。实测 153 条经验全 `candidate/observe`、links=0，加条数
无意义；链路卡在两处语义断点，均属必然失效而非偶发数据问题。

**断点① 归因错位**：`link` 表唯一键 `(event_id, outcome_id)`（一个 outcome 只能
归因一条经验，故不改 DDL）；原 `sync_outcome_from_resolution` 取
`experience_ids[0]`，而 `qd_service` 把 shadow 项追加在捕获项之后 ⇒ 首条恒为
cap-memory/cap-case ⇒ 11 条白名单经验永远拿不到 link、`experiment_arm` 恒空
（今日 6/6 曝光事件实测）。改为 shadow 项优先、无 shadow 才回退首条。

**断点② 对照臂恒空**：原定义为「同窗内不在任何曝光事件中的 decision」，但
注入臂本身就来自曝光事件，实测窗口 11-19~11-20 内决策全带曝光 ⇒ ctl=0 ⇒
`n_clusters=min(n_inj,n_ctl)=0` ⇒ `_ci_diff` 因 `n_b<2` 恒 None ⇒ 升档永远
skip。改为「同窗内未注入该经验的 decision」，以落库 `exposure_items` 的 shadow
项判定（PIT 安全），保留削减时期偏差的窗口设计。

**预注册影响评估（对抗 p-hacking 质疑）**：
- `experiment_config.json` 的数字全部未动——δ_pred 0.08/0.05、n_min 60/80、
  n_cap 600/800、max_looks 4、milestones 均保持 11-20 预注册初稿，不存在
  「看到结果后反推阈值」。
- 对照臂**定义**属实验口径而非阈值，且是在发现 ctl=0 后修改，故如实留痕、
  不伪装成初始设计；其性质是纠正与实验意图不符的必然失效口径，非挑样本。
- 关键清白证据：实测 `qd_agent_consumption_decisions` = 0 行、
  `qd_agent_experience_outcome_links` = 0 行 ⇒ 旧口径运行期间**没有产生任何
  升档判定**，没有任何结论需要撤回或重算，不存在「换口径洗结论」。
- 数据未到期的部分不伪造：`probe_link_attribution_baseline_20261120.py` 实测
  24 个带曝光决策全 pending、0 outcomes，946 个历史 outcome 全无曝光 ⇒
  今天没有可回填的成熟样本，未写回填脚本。

**验证**：两处 `py_compile` 通过；归因纯函数 4/4、对照臂互斥变异 5/5；全库成熟
对照臂 SQL 返回 514 簇（语义可用，当前窗口 0 是数据未到）；`run_promotion_check()`
空转安全（checked=0/skipped=0）；相关回归 49 passed。两处备份
`tmp/evaluation_store_before_attribution_fix_20261120.py`、
`tmp/promotion_runner_before_control_fix_20261120.py`，回滚=备份覆盖。

**文档对齐**：本节同步把 §23 第 4 点、§23 回滚说明、§24 对照臂定义改为当前
设计（此前描述的是旧口径）；§24「今天盘后首批 link」是当时预期，实测未实现，
首批 shadow 决策到期日 11-25~11-27，届时两处修复才产生实际数据。改动前文档
备份 `tmp/T_N_EVALUATION_IMPROVEMENT_before_doc_align_20261120.md`。

**边界**：吞吐杠杆（批量 CLI 回放把决策从约 16 笔/天提到 100+）未动——会写
真实决策且 `exec_date` 口径敏感，属独立拍板项。零 DDL、零 skill 改动、无 git。

## §28 完工判定（2026-11-20 16:33-16:45，用户问「设计可以完工吗」）

**结论：机制层完工；数据前置层按设计维持 observe（未达是如实反映，非缺陷）；设计文档本身在本次定稿后定稿。**
若「完工」指"方案全部目标已达成、经验已升到 active"——**否**，且按 §9.3/§9.4 现在不应达成。

**三层判定（均有实测证据）**：

1. **设计/机制层：完工。** §5 快照/结果/目录/归因、§8 反馈三态与五引擎、§9 对照实验设计、
   §10 落库、§9.4 功效审计——18 个机制模块文件齐全（`condition_eval`/
   `applicability`/`conflict_resolution`/`counterfactual_learning`/`meta_learning`/
   `consumption_policy`/`rollout_policy`/`effective_weight`/`power_analysis`/
   `source_budget`/`ingestion_quality`/`extraction_funnel`/`experiment_config`/
   `shadow_injection`/`promotion_runner`/`experience_effect_report`/`replay`/
   `baseline_report`）。落库结构齐：snapshot 表含版本/契约/哈希字段；outcomes 含
   概率/Brier/arm/条件键/skill 版本/bundle 哈希；stats 含曝光/成功/成功率/置信区间；
   经验目录 173 条。归因链与对照臂两处语义修复已落盘并经编译+变异+回归验证。

2. **数据前置层：未达，按设计维持 observe。** 实测（2026-11-20 16:00 采集后）：
   outcomes 967 条中有校准概率仅 3、Brier 0；独立交易日簇 43 < n_min=60；
   arm 标记 outcome 0；两类预注册对照（回评更新效果 / 经验注入效果）未完成；
   未全量启用。这些正是 §13-1174「独立簇未达 n_min 或概率未补齐一律维持 observe」
   与 §9.4「如实反映不是死锁复发」所要求的状态。links 5 / stats 3 是 16:00
   首次贯通，均为无 shadow 的旧曝光事件归因，不构成升档证据。

3. **文档层：本次定稿。** 状态行从「方案草案，尚未修改生产代码」改为当前真实状态；
   §13 验收清单前补实测状态说明（32 项保留未勾选=未逐项走正式验收，非机制缺失）；
   本节给出完工判定。此前 §7/§13 与 §20-§27 的实施结果自相矛盾，至此消除。

**仍按设计未接线（非遗漏）**：8 个零生产调用方模块（冲突归组/反事实改写/元学习/
入库质检/提取漏斗/来源预算/有效权重/灰度策略部分）——§20 用户拍板「数据三前置未到，
现在接等于让机制基于空数据判定」，等数据达标再接；rollout_policy 已被效果报告引用 1 处、
consumption_policy 2 处。

**未做的独立拍板项**：吞吐杠杆（批量 CLI 回放提 16→100+ 笔/天，`exec_date` 口径敏感）、
席位名称补采、§6.1 净超额/回撤/Sortino（进入交易应用层才需要，当前数据源为 0）。
这些属于方案边界之外的新工作，不构成本设计未完工。

**回滚**：本次文档定稿改状态行 1 处 + §13 说明段 + 本节；备份
`tmp/T_N_EVALUATION_IMPROVEMENT_before_finalize_20261120.md`，回滚=备份覆盖。零生产代码改动。
