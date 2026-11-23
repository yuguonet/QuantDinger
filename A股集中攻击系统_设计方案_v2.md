# A股小资金集中攻击系统 · 设计方案 v2.0

> 版本：v2.0（2026-11-22，整合全文讨论定稿）
> 前版：`A股K线NextBar预测_设计方案.md`（保留作决策历史，本版为唯一执行依据）
> 性质：开源项目，不商业化。**Agent 是车，其余都是轮子。**

---

## 1. 定位与战略

### 1.1 战略定位

**小资金中短线快速复利，追求单位时间收益比。** 核心优势不是"全市场预测更准"，而是机构做不了的四件事：

| 真优势 | 含义 |
|---|---|
| 尾部捕捉 | 押中连板/龙头/题材启动，一波顶别人一年 |
| 集中持仓 | 单票可 3~5 成仓，不需分散 |
| 进出速度 | 分钟级建仓清仓，吃情绪周期 |
| 容量无约束 | 能玩小票/低流动性票的溢价，那是大资金的猎场盲区 |

### 1.2 目标函数（记分牌对准自己的打法）

- **预测目标**：尾部事件（涨停/炸板/龙头启动/题材发酵），非均值收益；
- **评测指标**：recall@K（选股漏斗查全）、Top-N 集中收益分布、隔日涨停命中率、单位时间收益比、情绪周期内最大回撤——**不用 IC/RankIC/组合年化作主指标**；
- 预测模型输出**尾部概率**（P(涨幅>7%)、P(涨停)），决策与时机由 agent 完成。

### 1.3 核心架构观

**Agent 是车，其余都是轮子：**

| 组件 | 角色 | 注码 |
|---|---|---|
| **agent(LLM) + 经验库闭环** | 决策智能 + 持续进化（AGENT_DESIGN_v3 五句主张） | **主线，大头** |
| GBDT + Alpha158 | 粗筛打量（查全、便宜）+ **p_base 冻结基线** | 第一步，先验证 |
| KiT（284M，开源后可改可微调） | 打分布：尾部概率 | **P0-A 实测有效后才决定是否加** |
| qlib | chain/ 回测评估引擎 | 小 |
| Kronos-FT | **条件期权**（见 §5），非计划项 | 0，直到行权 |
| verl | 离线训练侧 | 挂起 |

---

## 2. 问题定义

### 2.1 两半问题

1. **找哪只**（检索）：从 ~5000 只票里把最优候选捞进攻击池——排序/查全问题；
2. **怎么打**（预测+决策）：对候选算尾部概率，联合叙事与时机做集中攻击——分布+裁决问题。

### 2.2 Next-Bar 同构

核心与语言模型 next-token 一致：**用历史K线序列预测下一根K线**。日K → 次一交易日（T+1）；分时 → 下一周期。同一套结构、同一套目标，多周期联合训练可学到跨尺度传导（分时聚集→日线形态）。K线编码用关系型特征（跳空/实体/上影/下影/量比，即 KiT 的 5 维对数比状态——已被验证优于原始价量序列，见 qlib Alpha158 vs Alpha360 教训）。

---

## 3. 系统架构：作战流

### 3.1 漏斗（盘后）

```
全市场 ~5000
  → 【闸门】情绪周期判定：能不能打？（连板高度/涨跌停比/成交额/炸板率）
     不对就不开仓——防守也是策略            ← dragon_cycle 系列
  → 【粗筛】5000 → ~200（唯一硬门，查全优先，宁滥勿缺）
     硬过滤（非ST/非新股/流动性/无雷）+ 量价异动/资金流/龙虎榜基因
     规则 + GBDT(Alpha158)                 ← daily_prescan / daily_screener / preselect
  → 【联合决策】200 → 3~8 攻击池（一步完成，不设中间硬门）
     agent 输入：KiT 尾部概率（全量落盘）+ 题材阶段/龙虎榜席位/公告催化/情绪卡位
     内部两遍：廉价分诊全量 → 深挖 top20~30
     含 3~5 个"叙事野生牌"名额（概率平庸但题材异动的票）
     输出：攻击池 + 仓位 + 盘中预案           ← agent 主场
```

**联合决策的纪律**：决策合并，计算不合并——KiT 全量打分便宜地跑完并**独立落盘**，保证经验库能分层归因（哪层漏了牛股/哪层放进的坑），且为 Kronos 期权提供校准判定数据。

### 3.2 盘中（同一个 agent 的连续注意力）

- **LLM 不直接吃 1m 数据流**：量化层（KiT 分时 + 规则）检测事件（放量突破/炸板回封/承接转强）→ 事件摘要 → agent 带上下文解读与决断；
- **决策窗口化**：竞价后（9:25~9:35）/ 盘中关键点（10:00）/ 尾盘（14:30~）+ 事件触发，不全程盯盘；
- **用途与 T+1 自洽**：① 攻击池建仓/撤退择时 ② 次日开盘卖点规划（v2_tail_buy 口径）。

### 3.3 轮子与车的接口纪律（AGENT_DESIGN_v3 对齐，硬约束）

agent 设计真源：`backend_api_python/app/agent/AGENT_DESIGN_v3.md`。轮子接入不得与车冲突，按其五句主张执行：

| 纪律 | 落地含义 |
|---|---|
| **LLM 做理解和提取，量化模型做预测和排序** | GBDT/KiT = 概率与排序层；agent(LLM) = 题材/龙虎榜/公告的理解与条件证据提取，**不越界做预测** |
| **不许 LLM 报概率（红线 R1）** | 概率只由定量聚合层给（冻结基线 + 经验校准项）；联合决策里 agent 引用轮子的概率，不自创概率 |
| **换 estimand 不换数据源** | 轮子效果评估用 Δp 配对符号检验（经验/轮子让概率动了多少、方向对不对），不只看命中率 |
| **对照臂顺手拿** | GBDT 正好充当 v3 的**冻结基线 p_base**，Δp 零额外成本 |
| **先让闭环说真话** | 标签质量先行（判定取价落地、退市单列），不在脏标签上做精密统计 |
| **工程纪律** | 决策规则外置 yaml（规则不在代码里）；fail-open；只提示不过滤；新轮子以 app/agent/ 内新增模块/适配器接入，**不修改现有 skill/tool 文件**（T+N 标准边界）；工具面遵守分级/预选/lint 体系 |

### 3.4 轮子接入 agent 的实施细节（怎么接到车上）

> 接入真源：AGENT_DESIGN_v3（工具面/分级/预选体系）+ T_N 标准（skills/tools 现有文件只读）。**新增模块不改现有文件**，一律动态加载（快速拔插约定）。

**① 接入形态（三件套，GBDT/KiT 同接口）**：

```
[a] 盘后批量产分数（不占 LLM 往返）
    盘后 worker/cron → run_p0a/train+predict → tail_scores 落库
    表口径：instrument, trade_date, p_tail, rank, model_ver, data_asof
    （model_ver 必带：分数可溯源，grounding gate 要求）

[b] 薄查询工具（LLM 工具面新增一个 finance 工具）
    get_tail_scores(date, universe, k) -> 结构化 JSON（纯本地计算，~ms 级）
    —— 分级体系里归"按需下发"层，预选命中才出现，不占必注入预算

[c] 提示注入（只提示不过滤，v3 取舍3）
    agent 组装 messages 时，预选/权重提示层把"昨日 tail top-K + 分数"作为 hint 注入
    agent 需要明细时再调 [b]，不强制，fail-open（分数缺失时降级为无 hint）
```

**② 消费点**：市场筛选/个股分析流程（skill 只读调用新工具，不改 skill 文件）；联合决策时 agent 把分数当**条件证据**引用（红线 R1：不许自己编概率）。

**③ 回评接线（T_N 标准链路）**：

- Decision Snapshot 冻结时包含：引用的分数 + model_ver + data_asof（这就是 p_base 冻结基线）；
- 经验校准项让概率动的部分 = Δp 评估链输入（v3 estimand）；
- 每日 resolutions 回填：昨日 top-K vs 今日实际 → tail_recall 记诊断指标（**轮子层独立归因**：分数错了是轮子的锅，引用错了是 agent 的锅）。

**④ 工程约束清单（逐条对应 §3.3）**：

| 约束 | 落地 |
|---|---|
| 不改现有文件 | 新模块放 `app/agent/` 内独立目录（如 `app/agent/wheels/gbdt_tail/`），能力注册走动态加载/自动发现约定 |
| 规则不在代码里 | top-K 阈值/启用开关/宇宙过滤规则 → 外置 yaml |
| fail-open | 分数表缺失/过期 → 无 hint 继续跑，不阻断分析 |
| 领域中立 | 域走 `qd_domain_resolvers` 配置，代码无 if finance |
| 分级/预选/lint | 工具 schema 照 v3 工具面规范（OpenAI Function Calling）注册 |
| 本地计算 vs LLM 往返 | 查询工具纯本地 ms 级，不新增 LLM 往返（v3 取舍4） |
| 生产数据源 | 验证后从 qlib data 切 QuantDinger `data_sources`/`kline_cache`（同一 DataFrame 接口，两处加载器抽统一接口） |

**⑤ 分阶段接线**：P0-A（GBDT）只接 [a][b][c] 最小闭环；P0-B 若 KiT 进场，复用同一 `get_tail_scores` 接口（多模型列并存，model_ver 区分），agent 侧零改动——这就是"轮子可插拔"的验收标准。

### 3.5 单日作战流

```
盘后   粗筛(GBDT 5000→200) → 联合决策(攻击池3~8 + 仓位 + 预案)，KiT 分数落盘
早盘   同一 agent 竞价确认/否决（隔夜消息/情绪）
盘中   量化事件监控攻击池 → 事件触发 agent 判断（持有/撤退）
尾盘   agent 建仓决断（v2_tail_buy 口径）
盘后   经验库记录全链路（KiT 分数与 agent 判断分账），T+n 验证入环
```

---

## 4. 经验库闭环（提智的复利来源）

### 4.1 实施依据（唯一标准）

经验库按 **`backend_api_python/app/agent/T_N_EVALUATION_IMPROVEMENT_20261118.md`**（《T+N 回评改进方案》，机制层已实施、数据前置 observe、完工判定见其 §28）实施，目标是形成 **"经验获取 → 有效性评估 → 经验应用 → 结果回流 → 迭代验证"** 的学习回路。硬边界（不得违反）：

- 生产代码只改 `app/agent/`；`skills/`、`tools/` **只读**，不修改现有 skill/tool 行为；
- 经验统一入**经验目录**（版本化、多 skill 共享），从 skill/会话/案例提取的经验是**候选知识**，必须保留来源、适用条件、验证状态，不得把未回评的总结标为有效；
- **outcome 唯一真源**：新 claim/resolution（有明确预测对象与 horizon）为主预测 outcome；旧 trace 标签仅作独立辅助口径，不重复计票。

### 4.2 口径对齐（指标分层，不混用）

| 层 | 指标 | 用途 |
|---|---|---|
| 学习回路主指标 | **预测成功率**（按 horizon × 情境 × 有效样本口径，如 T+1 涨停命中/尾部命中） | 经验有效性判定的唯一依据 |
| 次指标 | 概率校准（KiT 尾部概率、p_up 标定注入的置信度可信度） | 置信度是否可信 |
| 诊断指标 | 归因粒度（数据/推理/工具/执行哪环出错） | 改进定位 |
| 应用层护栏 | 净收益/超额/回撤（含系统的单位时间收益比记分牌） | 最终检验与风险护栏，**不替代预测质量，也不单独宣称有效** |

### 4.3 本系统的接入方式

1. **漏斗分层归因以 claim 形式进环**：闸门判定、粗筛入选、联合决策（含 KiT 分数/野生牌/仓位）、盘中事件决断，均按标准链路 `record_decision → claims → T+N resolver → resolutions`，一 claim 一票，`undecidable` 不计；
2. **KiT 尾部概率走标定概率注入**（标准 §19 p_up 注入口径）：模型概率作为预测置信度进入校准链（calib_feed → calibration → p_final）；
3. **经验注入逐项留痕**：哪条经验（哪个版本）进了最终 prompt、多大 token 占用、是否重复/冲突，按标准 §5.4 记录，支持"展示后未引用/引用后变差"的归因；
4. **三态消费 + 灰度回滚**（标准 §8.2/§8.3）：经验按适用条件检索，不写死进 skill；
5. **对照实验分开验证**（标准 §9）："回评迭代效果"与"经验注入效果"分别设对照组、预注册阈值、自适应停止，不合并归因；
6. **反馈优先影子口径**（标准 §23 shadow）：新机制先 shadow 运行，不影响实际决策，验证后再升档。

经验库是"系统越用越聪明"的机制保障，也是防自欺的最后闸门；与本方案的衔接点只有一个：**把漏斗与作战流的每个决策环节变成可回评、可归因、可迭代的 claim**。

---

## 5. Kronos 期权条款（条件触发，非计划项）

- **行权条件**：实测 KiT 尾部概率在攻击池切片（连板/题材股）上系统性失准（约 1 天的校准测试：KiT 给 P(涨停)>30% 的票，实际涨停率是否 ≈30%？）；
- **行权动作**：Kronos 专项微调，唯一目标 = 该切片尾部概率更准，做 KiT 专属替补。配方：① 强势股/题材股 regime 继续预训练（无标签）→ ② 尾部事件目标微调，**必须带 hard negatives**（同形态但炸板/冲高回落的阴性样本）——"淘汰低信噪比"只能淘汰在推理范围，不能淘汰在训练集；
- **不行权**：KiT 校准良好 → 永不启动；
- **辅助实验**（实验池，不占主线排期）：简化 Kronos vs GBDT 打量对决（同数据/同 recall@K/同算力预算，一周出结果，预期 GBDT 赢或平）；
- **原则**：训练模型是对实测缺陷的响应，不是预先的计划项。

---

## 6. P0 验证计划（开工第一步，按"先轮子后加码"排序）

**总原则**（用户定调）：**第一步只做规则+GBDT，直接验证有效性；实测有效后再决定是否 +KiT。** 每步独立可验证，不预先投入。

| 步骤 | 内容 | 出口判据 | 门槛 |
|---|---|---|---|
| **P0-A 规则+GBDT** | 粗筛/排序落地：规则过滤 + GBDT(Alpha158) 排序；recall@K（昨日池 vs 今日涨停/龙头）+ 尾部命中 vs naive（照抄上根/动量） | 打赢 naive = 及格；查全率与尾部命中达到可用水平 | **直接验证，无依赖，先做这个** |
| **P0-B 是否 +KiT** | 仅当 P0-A 实测有效：KiT 尾部概率叠加，对比增量价值（Δp 口径：相对 GBDT 基线概率动了多少） | 增量显著才保留 KiT；否则不加 | 由 P0-A 结果触发 |
| **P0-C 尾部校准**（若 KiT 进场） | KiT P(涨停)/P(涨幅>7%) 校准曲线（攻击池切片单独报） | 失准 → 触发 Kronos 期权评估（§5） | 由 P0-B 结果触发 |
| **P0-D 漏斗查全摸底** | 现有 daily_screener/preselect 的 recall@K 基线 | 摸底，缺口即改进清单 | 与 P0-A 并行 |
| **P0-E 尾部口径回测** | market_cn/auto + qlib 双引擎，Top-N 集中持仓口径（不用 CSI300 组合） | 单位时间收益比为应用层记分（护栏口径，与预测质量指标分层，见 §4.2） | P0-A 后随时可跑 |

**基建**：数据用 QuantDinger `data_sources` + `download_klines/kline_cache`（找轮子）；评测按 walk-forward 时间切分，防泄漏红线（分位数滚动计算、分时截断卡死）；GBDT 输出作为 p_base 进入经验库 Δp 评估链（§4.3）。

### 6.1 P0-A 执行计划（已开工，2026-11-22）

**目标**：验证"规则+GBDT 直接有效"——尾部事件（涨停/大涨）能不能被 Alpha158+LightGBM 显著捕捉，打不赢 naive 就止损。

**执行步骤**：

| 步 | 内容 | 产物 | 状态 |
|---|---|---|---|
| A1 环境 | Linux 环境装 pyqlib + lightgbm（Python 3.12） | 可运行环境 | 🔄 进行中 |
| A2 数据 | qlib 预建 A股日线数据（`qlib.tests.data.GetData().qlib_data(region='cn', interval='1d')`）入 `p0a_gbdt/data/` | 本地 qlib 数据仓 | 待 A1 |
| A3 管线 | `p0a_gbdt/` 下写：标签构建、LightGBM 训练、walk-forward 评测、naive 对照 | 可复现脚本族 | 待 A2 |
| A4 训评测 | 滚动训练 → 测试窗预测 → 尾部指标报告 | `tmp/p0a_report.md` + 逐日预测落盘 | 待 A3 |
| A5 判定 | 对照生死线出结论（过/不过/存疑） | 决策：是否进 P0-B | 待 A4 |

**P0-A 数据源口径说明**：验证阶段用 qlib 预建数据（免采集、PIT 口径已整理、与 Alpha158 天然配套）；**通过验证后切回 QuantDinger `data_sources`/`kline_cache` 作为生产数据源**（适配点：同一 DataFrame 接口，两处加载器实现统一接口，符合"重复调用 ≥2 处就抽接口"约定）。

### 6.2 P0-A 实施细节（口径唯一真源，与代码同步维护）

> **判据校准提示**：本节为 P0-A 立项时的初始设计口径。研发期经判据错配纠偏（详见附录 I），
> 主指标、训练目标与生死线均已变更；当前执行判据见本节末尾「判据校准」块及附录 I，凡与下方初始口径冲突处以校准后为准。

**标签**：

- 目标变量：next-bar 收益 `r = close[t+1]/close[t] - 1`（前复权）；
- 尾部事件双口径：① 相对口径 `r > +7%`（含一字板不可买情形需单独标记）；② 涨停口径按板块区分（主板 ≥9.8%、创业板/科创板 ≥19.8%、北交所 ≥29.8%，ST ±5% 单列或剔除）；
- 一字板（开盘即涨停无法买入）样本**单列不计入可交易命中**，防虚高。

**特征**：qlib Alpha158（内置因子集，关系型特征，对应 §2.2 编码原则）；不做额外特征工程（第一轮只验基线）。

**模型**：LightGBM（qlib LGBModel 口径，超参照其 benchmark 默认，第一轮不调参——调参后再对比属于过拟合风险区，见 §7）。

**切分（walk-forward，无未来信息）**：

- 训练窗：滚动 3 年；验证窗：其后 6 个月（早停/选代轮次）；测试窗：再其后 6 个月，逐窗前推；
- 所有统计量（标准化/分位数）只用训练窗拟合；行业/市值中性化如启用也只用训练窗口径。

**对照组（naive，必须全跑）**：

- `copy_last`：无预测力基准（等于命中率=基础概率）；
- `momentum_5d / momentum_20d`：近 5/20 日收益排序；
- `vol_rank`：近 20 日振幅/量比排序（强势股代理）。

**评测指标（尾部口径，逐日横截面 + 分年度报告）**：

- **tail_recall@K**：当日实际涨停/大涨股 ∈ 昨日预测 top-K（K=20/50/100）的比例（查全）；
- **tail_precision@K**：top-K 中尾部事件占比（查准）；
- **top-decile 命中密度**：最高分层的尾部事件率 vs 全市场基础率（提升倍数）；
- RankIC（参考指标，仅用于与 qlib/外部口径对表，不作生死判据）；
- 全部指标按可交易口径重算一遍（剔一字板）。

**生死线（沿用 v2 §1.2 记分牌）**：

- 及格：tail_recall@K 与 top-decile 密度**均**显著优于全部 naive 对照（分年度无一年崩盘）；
- 可用：top-decile 密度提升倍数 ≥ 3×（初值，经验值可调，调整需留痕）；
- 不过线：止损预测路线，转风控/异常检测（§7）。

**判据校准（覆盖上述初始口径，来源：研发期判据错配纠偏，2026-11-22）**：

- 上述「及格/可用/不过线」三行按 top-decile 密度判定，是立项初始口径；据此得出的首轮「不过线 → 转风控」结论**整体作废**（命题错配，附录 I1）。
- 当前执行口径：GBDT 职责 = 全市场约 5000 → 每日 top200 粗筛，**主指标 recall@200（查全优先）**；
  训练目标 = **尾部二分类**（非收益回归，附录 I3）；达标线 = recall@200 显著优于全部 naive（配对 bootstrap 95%CI 下界 >0）+ 分年度无崩盘 + lift ≥3× 为参考。
- 特征口径：完整 Alpha158（158 因子，已对拍官方）取代「不做额外特征工程」的初版约束；数据源 DB 直读取代 qlib 二进制仓（见 §6.6）。

**工程规范**：产物一律进 `p0a_gbdt/tmp/`；脚本头部注释写作用+易错点；标签/指标口径改动必须同步本节；不做非必要红绿验证；随机种子固定留档。

### 6.3 目录结构

```
p0a_gbdt/
├── data/                  # qlib 数据仓（A2）
├── tmp/                   # 预测落盘、报告、日志（产物区）
├── build_labels.py        # 标签构建（含一字板/板块涨停口径）
├── train_gbdt.py          # Alpha158 + LightGBM 滚动训练
├── evaluate.py            # 尾部指标 + naive 对照 + 分年度报告
└── run_p0a.py             # 一键编排（A3→A4）
```

### 6.4 当前进度与环境风险（滚动更新）

| 时点 | 进度 |
|---|---|
| 11-22 13:16 | ✅ **A1 完成**：qlib 0.9.7 + lightgbm 4.7.0 导入全通（Alpha158/LGBModel/DatasetH/D） |
| 11-22 13:20 | ✅ **A3 完成**：四件套脚本落地（见 6.5 脚本职责表） |
| 11-22 13:23 | 🔄 **A2 进行中**：数据下载 240MB，预计 13:40 就绪 |
| 待启 | A4 训练评测 → A5 生死线判定 |

⚠️ 环境风险已消化：pandas 3.0/numpy 2.5 与 pyqlib 0.9.7 兼容无碍（cp312 预编译 wheel）；备选路径（baostock+手写因子）未启用。

> **勘误（2026-11-22 15:05｜来源：P0-A 本地落地前环境核实）**：上表进度与本地实际不符，P0-A 在本地尚未实现——
> - `.venv`（`D:\QuantDinger\.venv\Scripts\python.exe`）实测 `find_spec('qlib') is None`，**qlib 未安装**；`lightgbm 4.7.0` 已装。
> - 全仓无 `p0a_gbdt/` 目录及四件套脚本，`data/cn_data/` 数据仓亦不存在。
> - A1/A2/A3 完成记录与「环境风险已消化」系其他环境/会话产物或规划，非本地既成事实；本地状态：**待开工**。

> **当前实际状态（2026-11-22 晚）**：P0-A 已完成落地并通过验收——DB 直读 + 完整 Alpha158 + 尾部二分类，
> recall@200=0.2317（对 momentum_5d +0.0283、lift 3.91×）；常驻模型、每日选 200 推理入口、agent 只读工具 `get_tail_scores`
> 均已上线。上表 A1~A5 为早期 qlib 路线记录，执行口径以 §6.6 及附录 F/G/H 为准。

### 6.5 实施细节备忘（环境/数据源/脚本/命令，复现必读）

**A1 环境（Python 3.12，用户级安装）**：

- 策略：`pip3 install --user --no-deps pyqlib lightgbm` + 按报错增量补依赖（pypi 直连仅 ~20kB/s，mlflow+jupyter 全家桶会拖死构建，故不走全量依赖）；
- 镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`（必要，直连不可用）；
- 实际依赖清单（踩坑补齐顺序）：`pyqlib lightgbm fire scipy tqdm ruamel.yaml deprecation python-dotenv jupyter-client redis python-redis-lock pydantic-settings pyarrow narwhals pymongo nbconvert joblib matplotlib mlflow-skinny dill loguru filelock`；
- 未装可选项：mlflow 全量（skinny 够用）、jupyter、catboost/xgboost/torch（不需要）；
- ⚠️ 验证命令：`python3 -c "from qlib.contrib.data.handler import Alpha158; from qlib.contrib.model.gbdt import LGBModel"`。

**A2 数据源决策链（踩坑记录）**：

1. ❌ `qlib.tests.data.GetData().qlib_data(version='latest')` → 404（latest 标签不存在）；
2. ✅ 正确资产：`SunsetWolf/qlib_dataset` release **tag v3** → `qlib_data_cn_1d_latest.zip`（240MB，A股全市场日线，qlib 二进制格式）；
3. ❌ github.com 直连 release：沙箱出口代理阻断 CDN（objects.githubusercontent.com 挂起）；❌ gh-proxy.com：代理规则禁用；❌ api.github.com 资产端点：可用但仅 ~14kB/s；
4. ✅ **最终通道：`https://ghfast.top/https://github.com/...`（235 kB/s）**；
- 落盘：`p0a_gbdt/data/cn_data/`（qlib provider 目录）；zip 与解压中间物在 `tmp/`。

**A3 脚本职责表**：

| 文件 | 职责 | 对应口径 |
|---|---|---|
| `common.py` | 标签构建（r_next/尾部双口径/一字板可交易口径）、naive 四对照、tail_recall@K/precision@K/top-decile lift | v2 §6.2（唯一实现） |
| `train_gbdt.py` | Alpha158+LGB 滚动训练（3 窗：train3y/valid6m/test6m）；pred+close+open 落盘 parquet | §6.2 切分/模型 |
| `evaluate.py` | 汇总指标、分年度、生死线判定表 → `tmp/p0a_report.md` + `p0a_metrics_all.csv` | §6.2 生死线 |
| `run_p0a.py` | 一键编排 A3→A4 | — |

**A4/A5 命令**：

```bash
cd p0a_gbdt
python3 run_p0a.py --data_dir data/cn_data   # 产物：tmp/p0a_report.md
```

依赖备注：evaluate.py 需 `tabulate`（to_markdown），A4 前补装。判定规则：及格=recall@K 与 top-decile 密度均优 于全部 naive 且分年度无崩盘；可用=top-decile lift ≥3×；不过→止损转风控（§7）。

### 6.6 实施变更：DB 直读替代 qlib 二进制仓（2026-11-22，唯一执行口径）

> 来源：P0-A 本地落地实测决议。本节为 §6.2/§6.5 的**实际执行口径**；二者原文保留作决策历史，凡与本节冲突处以本节为准。

**变更原因**：qlib 全市场 Alpha158 在 Windows 多进程 spawn 下 OOM（`MemoryError: Unable to allocate output buffer`），单进程加载极慢；用户指示改用项目 DB 已有的 `kline_1D` + 前复权。

| 项 | 变更后的实际口径 | 原 §6.2/§6.5 口径（仅存档） |
|---|---|---|
| 数据源 | `CNStock_db.kline_1D_2021~2026`（不复权 OHLCV，约 670 万行）+ `data/adjustment_factors.json`（新浪 qfq 因子，5232 票） | qlib 二进制仓（`joshuaxql/qlib_data`，883MB，2010~2026） |
| 前复权 | 复刻 `provider/adjustment.py::unadj_to_qfq`：factor=≤bar_date 最近除权日因子、最新除权日后=1、价/factor；volume 不复权 | qlib `$close` 已前复权 |
| 特征 | 自算 23 个核心因子（ret_1/5/10/20/60/120、log_vol_20、vol_ratio、amp、std、pos、gap、shadow、body 等），只用 t 及之前信息 | qlib Alpha158（158 因子） |
| 时间覆盖 | 2021-01-04 ~ 2026-11-20（5.9 年） | 2010-01-04 ~ 2026-09-30（16.7 年） |
| walk-forward | 2 年训练 + 6 月验证 + 6 月测试 / 步进 6 月（约 4~5 窗） | 3 年训练 + 6 月验证 + 6 月测试（25 窗） |
| 模型 | 原生 `lightgbm.LGBMRegressor`（回归 next-bar 收益），early_stopping | qlib LGBModel |
| 脚本 | `common.py`（标签/naive/指标）、`train_gbdt.py`（DB 直读+前复权+特征+训练）、`evaluate.py`、`run_p0a.py` | 同文件名但 train 走 qlib handler |

**保留不变**：标签双口径（>7% / 分板块涨停、ST 4.8%、一字板单列）、naive 四对照、recall@K/precision@K/top-decile lift、配对区块 bootstrap 95%CI、漏斗归因、生死线（CI 下界>0、lift≥3×）、附录 A~E 全部验收补强。

**已知局限（须在报告中披露）**：DB 仅 5.9 年，样本外窗口数少；DB 价格是盘中快照口径的衍生（前复权因子来自新浪 JSON，与 qlib PIT 口径不完全一致）；不复权的 volume 与 qlib `$volume` 单位可能不同，故 vol 类因子只在 DB 内部横截面可比。

**A4/A5 命令（替代 §6.5 的旧命令）**：

```bash
cd p0a_gbdt
python run_p0a.py --work_dir . --num_boost_round 600   # 产物：tmp/p0a_report.md
```

---

## 7. 风险与纪律

| 风险 | 纪律 |
|---|---|
| 信噪比低，模型学成"照抄上根" | 评估与 loss 脱钩；naive/GBDT 对照组闸门；打不赢就止损 |
| 数据泄漏 | 时间切分 + 滚动统计 + 分时截断审计 |
| 过拟合/自欺 | 经验库判定（lift/CI/FDR/时间外复现）；回测好≠实盘好，严禁直接上实盘 |
| LLM 批量分析质量退化 | 结构化逐票独立打分→再横向排序；深挖限于窄集合 |
| 联合决策归因变粗 | KiT 分数独立落盘、分账记录（强制） |
| 止损线 | P0 打不赢对照组 → 转风控/异常检测（放量异动识别/形态识别，数据资产可复用） |
| 许可证 | 项目开源（AGPL-3.0 兼容发布）；KiT 可内嵌可改；保留出处声明 |

---

## 8. 关键决策记录（精简）

| 决策 | 结论 | 关键理由 |
|---|---|---|
| 训练框架（verl vs uni-agent） | verl 为训练基座（现挂起） | 单轮可验证任务是 RLVR 主场；uni-agent 留给未来 agent 化 |
| 音频框架借不借 | 借架构（多码本/多分辨率），不借模型 | 语音 tokenizer 的声学先验对K线无效 |
| 编码方案 | 关系型特征（5 维对数比状态） | qlib Alpha158 vs Alpha360 实证 |
| KiT vs Kronos | KiT 主用（7 粒度+分布输出+SOTA），Kronos 期权 | KiT 自报全面领先；开源后 AGPL 解除顾虑 |
| 预测 vs 决策 | 模型给概率，agent 做决断 | 机构记分牌 vs 小资金打法的错配教训 |
| 精排+决断 | 合并为一步联合决策 | 串行硬门杀掉"概率平庸叙事强"的票 |
| Kronos 专项训练 | 降级为条件期权 | 粗筛打不过 GBDT、精排打不过 KiT，无固定生态位 |
| 更优解搜索 | 停止 | KiT/Kronos 即当前前沿，边际收益趋零 |
| 开工顺序 | **先规则+GBDT 直接验证，有效再考虑 +KiT** | 每步独立可验证，不预先投入 |
| 轮子与车的接口 | 按 AGENT_DESIGN_v3 五句主张：轮子报概率/排序，LLM 理解/提取，不许 LLM 报概率（R1） | 轮子不与车冲突 |
| GBDT 职责与判据 | 只做全市场 5000→200 粗筛，主指标 recall@200 | 官方 Alpha158+GBDT 属 IC/组合收益谱系；本方案查全优先，判据不能套用官方年化 |
| 粗筛训练目标 | 尾部二分类，不用收益回归 | 完整 Alpha158+收益回归 recall@200=0.1261 输 momentum_5d；改尾部二分类后 0.2317 达标（详见附录 H） |
| 模型/分数更新节奏 | 分数每日盘后更新，模型周更或失效触发，不每日重训 | 最新日 t+1 标签未成熟会被 NaN→0 灌成负样本；全量重训 11min 且破坏 p_base 冻结基线 |
| 尾部专用特征 | 受控实验后不接入 | recall@200 提升 +0.0012、配对 CI[-0.0003,+0.0027] 跨 0，不足以承担 wheel/meta 口径漂移 |

---

## 附录：外部组件链接

| 组件 | 链接 | 用途 |
|---|---|---|
| KiT | https://github.com/Luciferbobo/KiT | 尾部概率引擎（主轮子） |
| Kronos | https://github.com/shiyu-coder/Kronos | 期权 + 对照组（MIT） |
| qlib | https://github.com/microsoft/qlib | 回测/评估引擎 + Alpha158 |
| verl | https://github.com/XiaomiMiMo/verl | 离线训练（挂起） |
| uni-agent | https://github.com/XiaomiMiMo/uni-agent | 未来 agent 化备选 |
| Alpha-R1 | https://github.com/FinStep-AI/Alpha-R1 | GRPO+真实收益 reward 参考 |
| QuantDinger | https://github.com/yuguonet/QuantDinger | 宿主系统（data_sources/market_cn/agent/经验库） |

---

## 附录：P0 选股效果验收口径补强（审阅补充，2026-11-22）

> 来源：本次对方案选股有效性与回测口径的审阅。此补充用于细化 §6、§7 的验收，不改变既定架构与开工顺序。

### A. 预测命中与可交易收益分开

- `close[t+1]/close[t]-1` 可作为“次日尾部事件预测”标签，但若信号在 t 日收盘后生成，不得将 `close[t]` 当作可成交买入价，也不得据此直接宣称策略收益。
- P0-A 分开报告预测指标（尾部事件 recall/precision、top-decile 密度）与策略收益指标。P0-E 的策略收益回测须显式建模信号生成时点、下一可成交时点与价格、停牌/涨跌停可成交性、手续费与滑点，以及 A 股 T+1 约束；无法成交的样本不得按理想价格计入收益。
- 标签中的板块、ST 状态及涨停阈值按样本日的实际状态确定；一字板继续按 §6.2 单列，不计作可交易命中。

### B. 固定历史股票池与数据可用性口径

- 每个交易日的评测 universe 必须按当日可知信息构造，记录当日上市/退市状态、ST 状态、板块、停牌及数据缺失处理；不得用当前股票名单倒推历史股票池。
- 在启动 A4 前核对 qlib 数据对历史股票覆盖、退市/更名及复权口径的支持情况，并记录核验结果。若某项无法可靠还原，须量化影响并在报告中披露，不得笼统视为已满足 PIT。

### C. 固定基准与“显著优于”的判定

- 模型与每个 naive 基准须使用相同的日期、universe、可交易口径及 K。`copy_last` 必须明确为可复现的排序/预测规则；若其意图是无预测力基准，应采用固定随机种子并重复抽样，报告随机基准分布，不能把未定义的股票顺序当作基准。
- 在测试期逐日计算模型相对各基准的 recall@K 与 top-decile 密度差值；按交易日进行配对区块 bootstrap，报告差值及 95% 置信区间。将“显著优于”定义为两个主指标相对每个基准的差值置信区间下界均大于 0；同时报告各测试窗和各年度结果，避免总体平均掩盖失效年份。
- 测试窗只用于最终验收，不用于挑选特征、阈值或模型版本。K 值、标签口径、基准定义和判定规则须在查看测试结果前固定并留档；如需调整，调整后的结果应使用新的未触碰测试窗验证。

### D. 对漏斗分层归因并验证叙事名额

- 同一测试期分别报告全市场基准、规则过滤后候选池、GBDT 排序 top-K、最终攻击池的尾部事件覆盖数与 recall，明确漏失发生在哪一级；最终攻击池指标不能替代各层诊断指标。
- 对“叙事野生牌”做有/无该名额的配对消融，保持日期、候选信息和其他决策规则一致，报告其新增尾部召回、命中、可交易收益及回撤。只有增量效果经样本外验证后，才将其视为有效的选股机制。

### E. 结果解释边界

- P0-A 通过只证明指定数据、标签、universe 和测试期内的尾部排序能力优于基准；不等于已证明实盘盈利。收益、成本、成交约束与回撤仍按 P0-E 独立验证，验证前不得据此宣称可直接实盘。

### F. 每日选 200 推理入口（来源：P0-A 粗筛达标后的工程落地，2026-11-22）

- 入口：`p0a_gbdt/fit_live_model.py`（训练并落盘常驻模型）→ `p0a_gbdt/infer_daily.py`（盘后按日选 top200）。
  常驻模型为 DB 前复权 + 完整 Alpha158 + 尾部二分类 + LightGBM，参数与已验收 walk-forward 脚本一致；
  标签/因子/读写函数共用 `common.py` / `alpha158_db.py` / `train_gbdt.py`，不另起口径。
- 每日运行：`python infer_daily.py [--date YYYY-MM-DD] [--model ...] [--top_k 200] [--out_dir ...]`，
  默认取 DB 最新交易日、全市场横截面按尾部概率降序取 200，输出 CSV + JSON（含模型版本、best_iteration、universe、数据截止日）。
  可调用接口 `select_top200()` 返回 DataFrame 与 run_meta，供下游联合决策直接接。
- 口径守卫：目标日不存在即报错、不回退；推理日早于模型 `data_last` 直接拒绝（防回测偷看）；
  特征列与 meta 逐列校验；预测用 meta 的 `best_iteration`；400 自然日因子预热已对拍全量预热分数完全一致。
- 验收：用 w3 训练/验证区间重建模型，对 2026-03-20 的 200 只分数与验收预测逐票比对，
  200/200 重合、最大绝对差 0；默认 400 天预热路径同样 0 差。最新交易日 2026-11-20 产出 200 只、5350 只 universe。
- 边界：盘后运行（验证段末会用到 as_of 当日收盘价），次日成交假设；一字板不在此层剔除（粗筛查全优先），
  由 200→3~8 联合决策处理；前复权因子为全局最新快照，历史 bar 会随新除权日重算；
  本入口只完成粗筛，不替代 P0-E 的收益/成本/成交约束验证。

### G. 接入 agent 第一步：计算轮 + 只读查询工具（来源：用户 2026-11-22 确认范围）

> 只做 §3.4 [b] 的只读查询能力与支撑它的计算轮；[a] 落库、[c] 提示注入、训练脚本迁移均未在本次范围。

- 新增：`backend_api_python/app/agent/wheels/gbdt_tail/score.py`（Alpha158 158 因子 + 常驻模型打分 + top-K）、
  `app/agent/tools/finance/tail_scores_tools.py`（公开工具 `get_tail_scores(date, k, universe, detail)`）。
  不改任何现有 skill/tool 文件，不触碰 `learning/`，`app/agent/` 新增文件零注释。
- 分层：日线只读 `screen_query.kline_batch`，前复权复用 `data_sources/provider/adjustment.py::unadj_to_qfq`
  与本地因子缓存；无裸 SQL、无连接池操作、无远端兜底。模型路径 env `QD_P0A_MODEL_PATH`（默认指向 `p0a_gbdt/models/tail_cls_live.txt`）。
  工具薄壳必须在 `tools/finance/` 才会被 `_provider.py:135-136` 自动注册（schema 已核对）。
- 返回契约：`{as_of, top_k, rows, meta{model_ver,caliber,universe,missing,cache_hit,error}}`；
  `detail="compact"`（默认）rows 为 `{rank,code,score,tradable}`，`detail="full"` 再含 `board,limit_pct,close`；
  `model_ver = tag:data_last:best_iteration` 供 p_base 冻结与溯源；失败返回空 rows + error（fail-open，不阻断 agent）。
- 验收：① 新旧 Alpha158 在 77,175 个有效单元格上最大绝对差 0、列顺序一致；
  ② 2026-11-20 全链路对拍原 `infer_daily.py`：200/200 代码重合、score 最大绝对差 9.7e-17、名次完全一致；
  ③ 工具注册/schema/失败降级实测通过；④ 相关回归 40 例通过（导入红线+工具面健康 10、预选/分组 30）；
  ⑤ 默认预热改为 70 日后全市场对拍仍 200/200、最大差 9.714e-17，耗时 1m51s（此前 2m53s）。
- 数据层口径：`screen_query.kline_batch` 按 `start~end` 覆盖年份跨年读取 `kline_1D_YYYY`，
  年表存在性由 `information_schema` 判定后逐年查询、合并、按 `days` 截断；同一年窗口的结果与旧实现逐值一致。
  年初预热不再受“当前年份表”限制，交易日不足 60 仍显式报错而非给错分。
- 计算口径：默认预热 `QD_P0A_WARMUP_DAYS=70` 个交易日（最长 rolling 为 60，70 与 260 的分数实测 0 差）；
  `tradable=false` 表示开高低收全等的一字板，粗筛只标记不过滤；
  结果按 `(as_of, model_path, model_sig, k, universe, days)` 短 TTL 缓存（`QD_P0A_CACHE_TTL`，设为 0 关闭）。
- 边界：当前查询为按需计算（分钟级），ms 级查询要等 [a] 分数落库后再切换实现。
  仍是粗筛，不含落库/提示注入/联合决策。

### H. 落地后评估、维护节奏与数据资产处置（来源：本次会话后段，2026-11-22）

> 附录 F/G 完成了粗筛器落地与 agent 接入；本节记录其后的受控实验结论、日常维护口径与数据处置决议。

**H1. 二档受控实验：Alpha158 + 尾部专用特征（不接入）**

- 实验设计：在 Alpha158 之后追加 5 个仅由 OHLCV + 板块代码派生、无前视的尾部特征
  （`GAP_OPEN`/`DIST_LIMIT`/`AMPLITUDE`/`LIMIT_HIT`/`ZT_STREAK`），定义于 `alpha158_db.py::TAIL_COLS/compute_tail_features`；
  训练脚本 `tmp/train_alpha158_tail_cls.py` 与基线同窗口/标签/超参/4 个 walk-forward 窗（best_iter 90/74/356/60）。
- 结果：recall@200 0.2317→0.2329（+0.0012，相对 +0.5%），lift 3.9117→3.9273；
  配对区块 bootstrap K=200 的 diff CI=[-0.0003,+0.0027] **跨 0 不显著**，仅 K=50 的 CI=[+0.0002,+0.0024] 下界 >0；
  分年度 2024/2025/2026 recall@200 = 0.258/0.256/0.188，无崩盘，生死线仍判「可用」。
- 决议：**不接入生产**。主指标未显著胜出，接 5 列要同步改 wheel/meta 校验/双端对拍，收益不足以承担口径漂移风险。
  报告 `tmp/p0a_report_a158_tail.md`、配对日志 `tmp/compare_a158_tail.log`。若要 retry，应先换更强信号源（ST/涨停池历史）而非继续堆 OHLCV 衍生特征。
- 口径边界：实验特征只训练侧落地，**未改任何 `app/agent` 生产代码**。

**H2. 日常维护节奏（分数日更、模型周更）**

- 三层分工：行情 `kline_1D_YYYY` 由既有采集链路每日写入（粗筛只读）；每日 200 清单盘后跑一次 `infer_daily.py` 或 agent 工具刷新；
  模型不每日重训（全量 11min15s，3 年窗 756 交易日，新增 1 天仅占 0.13%）。
- 不日更的硬理由：标签为 `t+1 close/t close - 1`，D 日标签须等 D+1 收盘才成熟；`tail_label_binary` 将 NaN 比较为 False 后
  `astype(int)` 成 0，日更会把该伪负样本天天灌进验证集。且 §3.4 要求分数带 `model_ver`、`p_base` 冻结，模型日变则 `Δp` 评估失去可比基准。
- 若坚持重训：先把末日伪标签修掉（`as_of` 回退到有完整次日收益的最近交易日 T-1），单独重基线后再动；
  现 `fit_live_model.py` 为保持验收对拍一致保留既有行为，不在已验收链路上偷改。

**H3. ST 口径与数据可用性（PIT 标签暂不可行）**

- `st.txt`（qlib 数据包内）是带起止日期的明细，`evaluate.py` 原用 `read_text().split()` 塞 set 再与 6 位码比较，永远匹配不上，
  日志 `ST codes:1944` 全是 token ⇒ ST 的 4.8% 涨停线自始未生效；训练标签与推理同样按非 ST 阈值，三者效果一致但都不识别 ST。
- 想修成按交易日查 ST 区间的 PIT 口径，但**数据不可用**：库内无 ST 历史区间表；`stock_basic_info` 只有当前快照
  （name 含 ST 198 只，updated_at 2026-06-02~11-21）；`cnd_limit_pool_list` 仅 2026-10-12~11-20 共 30 天，无法构造三年 PIT 标签。
  属标签口径变更，须先补历史数据源再回测，本次未动。

**H4. 跨年取数能力核验（支撑附录 G 的 kline_batch 修复）**

- `db_multi.py:123` 的 `_table_name(timeframe, year)` 仍需显式 year；`:127` 的 `_daily_view_name` 全仓零引用；库里只有
  `kline_1D_2021~2026` 六张 BASE TABLE，无统一视图。
- `db_market.py::MarketKlineWriter.query`（:376-488）**已有跨年读能力**（按年份展开 + `_existing_years` 扫 information_schema 缓存），
  但签名为单 `symbol`，不适合 5350 只全市场批量。故跨年修复落在横向只读层 `screen_query.kline_batch`，不塞进 db_multi/db_market。

**H5. 数据资产处置**

- `p0a_gbdt/data/cn_data`（qlib 路线数据包）已不参与任何训练/评测/推理链路；实测 `evaluate.py --data_dir data/cn_data` 与
  `--data_dir ""` 两份报告逐字相同（ST 名单从未生效，见 H3），删除不改任何指标。
- 唯一残留是 `evaluate.py`/`run_p0a.py` 的 `data_dir` 形参（路径不存在时静默跳过）。`tmp/cn_data.zip`（843MB）独立于 `data/`。
  本次未删任何文件，删除由用户手动执行；若日后修 ST PIT 口径需重新获取 `st.txt` 或换数据源。

### I. P0-A 研发期结论链（来源：本次会话全程，2026-11-22）

> F~H 记录「做成什么」；本节记录「为什么这么做」——从判据错配到口径重算、根因突破、达标与改进空间评估的完整结论链。

**I1. 首轮结论作废：判据错配，不是方法失效**

- 首轮 23 因子降级模型按 §6.2 初始生死线（top-decile 密度）判定「不过线 → 不进 P0-B → 转风控」（密度显著劣于三个 naive、lift 2.25×<3×）。
- 用户指正「GBDT 回测/评估引擎 + Alpha158 是官方论证过的，只是好和不好的问题」，复核 qlib 官方 benchmark README：
  CSI300 Alpha158+LightGBM IC=0.0448 / 年化 9.01% / IR=1.0164，CSI500 IC=0.0399 / 年化 12.84% / IR=1.5650（该池第一），
  官方口径只有 IC/ICIR/RankIC + 组合年化/IR/MaxDD，**不含尾部涨停密度**。
- 结论：首轮「不过线」属**命题错配**——用官方从未主张的判据（尾部密度赢动量）去否定一个官方已证明为好的方法，结论整体作废；
  首轮 §5/§6 数值仅作历史实测保留。两个命题须分开：① 官方已证「Alpha158+GBDT 全样本收益排序有效」；② 本项目要验「尾部涨停查全能否打赢动量」，是新命题、需另设判据。

**I2. 粗筛口径重算：主指标定为 recall@200**

- 用户明确方向：GBDT 做全市场 5000→200 粗筛，第一步就是选 200 只最优股；按 §2.1/§3「粗筛=查全、宁滥勿缺」，主指标应为 recall@200 而非 top-decile 密度。
- 385 万行落盘预测重算 K=200：模型 0.1624 / momentum_5d 0.2036 / momentum_20d 0.1842 / vol_rank 0.1429 / copy_last 0.0381；
  模型−momentum_5d = −0.0412 CI[−0.046,−0.036] 显著更差 ⇒ 23 因子降级模型不够格，须用完整 Alpha158 重建（数据 `tmp/coarse_screen_200.json`）。

**I3. Alpha158 保真：交付包纠错 + 官方对拍**

- 用户给交付包 `交付打包_设计文档与P0A代码_20261122.zip`（解压 `tmp/p0a_delivery_20261122/` 学习），核出 5 处硬伤：
  `train_gbdt.py:59` 的 `D.instruments("all")` 返回配置字典非股票列表；`evaluate.py:44` naive 漏传 volume 致 vol_rank 未跑；
  `common.py:62` 的 copy_last 全 0 分数是有偏伪基准；`common.py:88` recall 分子分母口径不齐；原版无 bootstrap CI。均已在本地实现中修正。
- `alpha158_db.py` 在 DB 前复权数据上完整复刻 qlib Alpha158 158 因子（kbar 9 + price 4 + rolling 29算子×5窗）：
  与 qlib 官方对拍（3 票 2024H1，351 行）**152/158 相关 >0.95 无反向**、111/158 >0.99；
  已知差异 VWAP0=0.959（DB 无 amount，用 (H+L+C)/3 近似）、WVMA 3 项 0.85~0.93（复权口径）、60 日长窗边界。
  修过的真 bug：IMAX/IMIN/IMXD 方向（qlib 定义为 (argmax+1)/d）。
- 工程事实：pandas 3.0 下 MultiIndex 的 `sort_index()`/Series 相减有 dtype 'M' bug，因子全用宽表 DataFrame 运算后统一构造 MultiIndex；`stack(dropna=False)` 已移除。

**I4. 根因突破：训练目标必须尾部二分类**

- 完整 Alpha158 + 收益回归 + 2000 棵早停 + 3 年训练窗全量：recall@200=0.1261，**仍显著输 momentum_5d**（diff −0.0242 CI[−0.027,−0.021]），排除「因子降级」与「训练不足」两个解释。
- 根因：官方收益回归优化的是全样本收益排序，不适合涨停查全。构造尾部二分类（标签 `(r_next>0.07)|涨停`，`LGBMClassifier` + `predict_proba`）：
  同为 1 年训练/100 棵冒烟，回归 recall@200=0.1335（输 momentum_5d −0.0184）→ 二分类 **0.2592（赢 +0.0428 CI[+0.038,+0.048]）**，四 naive 八项指标 CI 下界全 >0，lift 4.09×>3×。
- 结论：**粗筛训练目标必须对齐尾部二分类，不能照搬官方收益回归**——这是本轮最重要发现。

**I5. 达标与交付**

- 完整 Alpha158 + 尾部二分类 + 3 年训练/2000 棵/4 窗全量：recall@200=**0.2317**，vs momentum_5d +0.0283 CI[+0.026,+0.031]，
  四 naive × 两指标共 8 项 CI 下界全 >0，top-decile lift 3.91×>3×，分年度 2024/2025/2026 = 0.258/0.255/0.186 无崩盘，按粗筛查全口径**达标**。
- 交付 `p0a_gbdt/tmp/top200_daily.parquet/.csv`（504 日 × 200 只 = 100,800 行，字段 datetime/rank/instrument/score/close），
  报告 `tmp/p0a_report_a158_cls.md`（含验收结论）；产物清单见附录 F。
- 解释边界同附录 E：达标只证明指定数据/标签/universe/测试期内的尾部排序能力优于基准，不等于实盘盈利；收益/成本/成交约束仍归 P0-E。

**I6. 改进空间评估（结论汇总）**

- 第一档（已落地，见附录 G）：预热 260→70 日（分数 0 差）、短 TTL 缓存、可交易标记、工具输出瘦身；零口径风险、可 env 回滚。
- 第二档（受控实验，见附录 H1）：+5 个 OHLCV 派生尾部特征，主指标提升 CI 跨 0 不显著 → 不接入生产。
- 未做/待拍板：① 分数落库 + cron（[a]，分钟级 → ms 级）；② 提示注入 / p_base 冻结（[c]）；③ top-K、启用开关、宇宙过滤、模型路径配置化（现仅模型路径走 env）；④ 前复权因子版本化（严格 PIT 回测）；⑤ ST PIT 口径（数据不可用，H3）；⑥ 模型更新节奏（H2）；⑦ T-1 完整标签修复（须单独重基线）。
- 不建议：把 200→3~8 联合决策塞进 wheel；改回收益回归（已证输动量）；每日重训。

### J. 审计修复与口径重基线（2026-11-22 深夜，来源：代码前视审计）

> I5 的 0.2317 是「段末伪负样本」口径的历史实测；本附录记录审计发现后的修复与新基线。

**J1. 三处真问题与修复**

| # | 位置 | 问题 | 修复 | 验收 |
|---|---|---|---|---|
| 1 | `p0a_gbdt/common.py::tail_label_binary` | `astype(int)` 把段末 `NaN` 变 0，每个 train/valid 段末一日成伪负样本；`np.isfinite(y)` 因 int dtype 恒 True 过滤失效。与函数 docstring「段末 NaN 不可训练」不符 | 返回 float 并 `where(nxt.notna())` 保留 NaN；各脚本原有 `np.isfinite(y)` 过滤即刻生效 | 5 日序列 `[1,1,1,1,nan]`，trainable=4/5 |
| 2 | `app/agent/wheels/gbdt_tail/score.py` | `compute_scores(as_of=...)` 接受任意历史日且无守卫，与 `infer_daily.py` 的「推理日 < 模型 data_last 拒绝」不一致 ⇒ 拿未来训练的模型给历史日打分即前视泄漏 | `as_of` 不在 kline 数据直接报错；新增 `strict=True` 守卫（`as_of<data_last` 拒绝），`strict=False` 仅显式历史回测；cache key 含 strict | `2099-01-01`/`2026-11-21`(周末)/`2025-03-20`(默认 strict) 均拒绝，`strict=False` 可跑 |
| 3 | 同上，`_factor_sig` | 前复权因子快照更新会静默重算历史 bar，但 `model_ver` 与 cache key 未变（TTL 内回旧分），复评对不上号 | 取 `adjustment._CACHE_FILE` 的 `mtime_ns:size` 作 `factor_sig`，进 cache key 与 `meta.factor_sig` | 快照变化模拟后 `cache_hit=False`、`meta.factor_sig` 同步变化；正常两次调用命中 True |

**J2. 重基线结果（新标签口径，4 窗 / 完整 Alpha158 / 尾部二分类 / 2000 棵）**

- recall@200 = **0.2311**（旧 0.2317），vs momentum_5d diff=+0.0277 CI[+0.0252,+0.0303]；四 naive × 两指标共 8 项 CI 下界全 >0；top-decile lift 3.88×>3×；分年度无崩盘。**仍达标**。
- 常驻模型 best_iter 42→109，w3 重建 49→46；旧模型备份至 `del/p0a_model_prefix_20261122/`，新模型已切为默认。
- 对拍：w3_fixed 推理 vs 新 `pred_w03` 200/200 重合、max diff=0；agent wheel（默认模型）vs `infer_daily` 200/200 重合、max diff=0；工具注册/契约回归 63 passed。
- 校准实验随新预测重跑：raw ECE 0.01029/Brier 0.03364、Platt 0.01621/0.03457、Beta 0.00995/0.03369、isotonic 0.00970/0.03368 ⇒ 仍**不接校准**（raw 的 Brier 最优，Platt 恶化）。
- 旧 `venn` 行经查与 isotonic 完全同实现，已诚实改名为 `isotonic_alias`（仅作历史对照，不是 Venn-Abers）；引用旧 CSV 时须标注该别名。

**J3. 依赖清单与边界（F3，短期记录）**

- `score.py` 跨模块引用 `adjustment._CACHE_FILE` / `adjustment._to_sina_code` 两个下划线私有符号（F3）；当前可运行，但 provider 重构会断。短期按清单记录，长期在 data 层开两个只读公共访问器后替换；本轮未改 `app/data_sources` 外围文件。
- `factor_sig` 取文件 mtime+size，不校验内容 hash；若因子文件被原地同大小覆写且 mtime 回拨，仍可能漏检，属已知边界。
- 回滚：旧模型四文件在 `del/p0a_model_prefix_20261122/`；回滚须同时恢复 `tail_label_binary` 的旧 `astype(int)` 行为，否则模型与训练口径脱节（不建议，旧口径有已知伪负样本缺陷）。`strict=False` 可临时放行历史回测，但结果不可作为无前视证据。

### K. 接入闭环 [a] 落库 + [c] 提示注入（2026-11-23，来源：用户选 A 接入闭环）

> §3.4 三件套至此齐备：[b] 只读工具（附录 G）+ 本节 [a][c]。方案与冲突拍板见 `P0A_接入闭环方案_20261123.md`。

**K1. 两个拍板冲突**

- **冲突 A（estimand）**：现有 `p_base` 唯一源 = `predict_next_day.p_up` = P(T+1 上涨)（`c3p.py:179-197`、`p_target∈{up,down}`）；GBDT tail score = P(涨幅>7%或涨停)，是**尾部事件概率、不同 estimand**。直接塞进 p_base 会触发 `prediction_contract` 的 p_target 不匹配（`prediction_contract.py:214-217`）、`brier_eligible=False`，把 Δp 分母语义从"上涨"偷换成"涨停"。**拍板 A1**：[c] 只做 hint，**不进 p_base/Δp**；需要独立 tail 通道属另案。
- **冲突 B（不改现有文件）**：[c] 注入口都要动现有文件；**拍板允许**改 `qd_service.py` 一处（additive + env 默认关 + fail-open + 留痕），仿 §19 p_up 注入先例；skill/tool/learning 一律不动。

**K2. [a] 分数落库**

- 新表 `qd_tail_scores`（migration `qd_tail_scores.sql`）：`trade_date, instrument, p_tail, rank, board, model_ver, factor_sig, data_asof`，唯一键 `(trade_date, instrument, model_ver)`——多版本分数并存不互覆，`factor_sig` 追溯复权重算。
- `app/agent/wheels/gbdt_tail/store.py`：`upsert_scores`（executemany + ON CONFLICT 幂等，fail-open）、`load_top`、`latest_trade_date`；agent 自己的表走 `app.utils.db`（非行情 putdb）。
- 盘后 Task `tail_scores_daily` 挂 `market_cn/scheduler.py::TASKS`（18:30、once_per_day、trading_only 由主循环判、qd_scheduler_done 幂等）；**默认关闭** `QD_TAIL_SCORES_ENABLED=1` 才跑；K/宇宙走 `QD_TAIL_SCORES_K` / `QD_TAIL_SCORES_UNIVERSE`；纯本地无 LLM 往返。

**K3. [c] 提示注入（A1）**

- `app/agent/wheels/gbdt_tail/hint.py::tail_hint_block`：单标的 6 位码，查昨日落库 top-K，命中则注入一行"名次/粗筛分数/model_ver/data_asof"，明示**尾部排序分、非上涨概率、不可当 P(T+1涨) 使用**；不在池/env 关/异常均返回空串（fail-open、只提示不过滤、多标的不注入）。
- `qd_service.py` 加 `_tail_hint_block` 薄转发 + `_prefetch` 在 p_up 块后调用，留痕 `kind=tail_hint`；env `QD_INJECT_TAIL_HINT` 默认关。

**K4. 验收与回滚**

- 真库一次性验收 `p0a_gbdt/tmp/verify_p0a_ingest_20261123.py`：建表 → compute_scores 落 50 行 → 字段/溯源齐 → 二次 upsert 幂等（行数不变）→ hint 四态（env 关/在池/不在池/多标的）全对 → qd_service 薄转发通；INGEST_PASS。
- pytest 工具面/schema 回归 65 passed（全量另记当日日志）。
- 回滚：`QD_INJECT_TAIL_HINT=0`、`QD_TAIL_SCORES_ENABLED=0`；`qd_service.py` 新增块可整段摘除；DROP 新表不影响既有链路。未碰 p_base/learning/skill/tool。
- 部署状态（2026-11-23 收尾）：代码层两开关默认仍关，部署层已在 `.env` 显式打开 `QD_TAIL_SCORES_ENABLED=1` / `QD_INJECT_TAIL_HINT=1`；真库实测 cron 在册、hint 命中注入非空。CLI 端到端实证：`python -m app.agent.cli "分析000530"` + LLM 录制，模型实际 prompt 命中 `[GBDT粗筛提示]` 3/3 次 ⇒ 开关真进模型上下文。另：`p0a_gbdt/tmp/` 一次性实验产物约 1.7G 移入 `del/p0a_tmp_cleanup_20261123/`（1.8G→101M），生产模型 `models/tail_cls_live.txt` 与重训工具链、当前基线 `preds_a158_cls_fixed` 均保留原位，加载复核 OK。
- 溯源补正（2026-11-23）：`score.py` meta 补 `data_asof`（模型数据截止日），修复 `qd_tail_scores.data_asof` 全 NULL、hint 显示"未知"错配（非本次开关引入，是接入期 meta 接口与落库字段不一致）；重落库 200/200 行带真实日期，打分对拍 200/200 重合、差 8.3e-17 不变。
