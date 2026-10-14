# A股 Agent 经验引擎设计 v3 —— 提智 × 经验 → 条件化预测力

> **日期**：2026-10-09（v1）/ 2026-10-10（v2、v2.1）/ 2026-10-11（v2.2 复用 mimoagent）/ **2026-10-11（v3 重建）** / 2026-10-11（v3.1 评审修订）/ **2026-10-11（v3.2 提智与条件化预测力增强，§20）** / 2026-10-12（v3.3 二次评审 5 条就地合入，§19.5）
> **来源**：会话讨论记录整理
> **主题**：如何让 Agent 在 A股中短线分析中建立持续进化的能力，最终指向**条件化预测力**
> **状态**：v3 重建版——结构重排（不再追加式演进）、**数据库重设计**、**回测以外挂接口方式接入**、**观察窗口成为一等公民**
> **适用仓库**：`backend_api_python/app/agent/`（追责系统 v1.1 + 提智方案 T1 之后的存量基线）
> **前版快照**：`del/agent_ext_design_v22_snapshot_20261011/`（v2.1 原版 + v2.2 追加版），本版是重建，不逐条对照

**v3 相对 v2.2 的四点重建**：

1. **观察窗口升为一等公民**：短线 **T+1 / T+3 / T+5**，中线 **T+7 / T+10 / T+15 / T+20**（受控枚举，七档）。结果按窗口分行存、分窗口陆续到期回填；命中只判**主窗口**，其余窗口只记收益不判对错。
2. **回测以外挂 / 接口 / 工具方式出现**：经验引擎内核不依赖任何回测实现，只依赖 `BacktestProvider` 接口；全盘策略与单股分析是两个 provider（可插拔、可禁用、可替换）；agent 侧继续以工具暴露。
3. **数据库重新设计**：结果表按窗口分行、口径规格版本化、模式表不建表，理由见 §5.3。
4. **mimoagent 只当离线验证底座**：经验条目的真伪走纯统计（零 LLM 成本），只有「提智改动值几分」才花 rollout 算力（§9）。

**保留不变的核心姿态**：引擎不预设哪些经验是真的；门槛设在「呈现权重」上，不设在「准入」上；统计契约对所有经验一视同仁地记账，让市场来投票哪些经验活下来。

---

## 0. 核心结论与目标口径

> **Agent 不是预测器，不是传感器，不是校准器——而是「越来越有经验的老股民」。**
> **量化帮它缩小范围，经验帮它做判断，校准帮它知道自己几斤几两，真值帮它不断修正记忆。**

**目标口径（v3 定稿）**：新版本全部改进归**提智 + 经验**两条线，最终指向**预测能力**。这里的「预测力」不是无条件预测个股日频涨跌（可预测成分极低，样本外 R² 仅 1%-5%，见 §1.3），而是**条件化的预测力**——在自己有经验优势的情境里（T+1~T+20 的题材 / 板块 / 个股特定形态），命中率、超额收益、置信度校准度显著高于基线。

* 可测口径：**条件命中率（按主窗口）+ ECE + 相对量化基线的增量**（§10 有效性指纹）；
* **不要用全局 R² 或总命中率当 KPI**——它们不会因这套设计变高，用了会得出「没进步」的错误结论。

**一句话架构**：在线采集经验（QDAgent 主链不动）→ 离线验证与升格（纯统计 + 回测外挂 + mimoagent batch）→ 检索注入影响未来判断（digest）。

> **v3.2 追加 §20**：主干之上再叠「三门地基（PIT 特征 / 残差口径 / 输出契约）→ 四招提智 → 两招变现 → DPO」，
> 指向进一步提智与提高条件化预测力。地基是「让指标可信」，提智/变现是「让指标变高」，**顺序不能反**。

---

## 1. 定位：从「提升预测力」到「经验引擎」

### 1.1 提智的通用策略（小模型 Agent 推理能力）

| 优先级 | 策略 | 成本 | 效果 | 本设计落点 |
|---|---|---|---|---|
| 1 | 结构化 Prompt + Few-Shot | 零 | 立竿见影 | §6 digest / 金例注入 |
| 2 | 工具增强（计算器 / 检索 / 代码） | 工程 | 显著 | 已有（102 工具分级下发） |
| 3 | Self-Reflection 模式 | 近零 | 明显 | 反向证据检查（§10 指纹） |
| 4 | 上下文压缩 + 外挂记忆 | 中 | 长任务稳定性 | compaction + 经验库（§6） |
| 5 | 多次采样 + 验证器 | 算力 | 用算力换质量 | mimoagent `--num-rollouts`（§9） |
| 6 | SFT 微调推理数据 | 数据 + 算力 | 天花板最高 | 轨迹即训练数据（§9.3） |

> 核心原则：用架构约束代替模型能力，用工具精确性代替模型记忆，用结构化 prompt 代替模型的自由发挥。把「智能」从模型参数中解放出来，分散到系统设计的每个环节中。

### 1.2 A股任务重定义与数据金字塔

```
❌ "预测明天大盘涨跌"        → 本质接近抛硬币
❌ "推荐买入哪只股票"        → 输出无法验证
❌ "分析这只股票好不好"       → 主观且模糊

✅ "给出未来 5 日收益率的排序"  → 可用 RankIC 验证
✅ "评估该事件对板块的冲击幅度" → 可回测对比
✅ "识别当前处于什么市场状态"  → 可分类验证
✅ "给出行情信号的置信度"     → 可校准检验
```

**核心原则：让 Agent 做它擅长的「理解和提取」，让量化模型做「预测和排序」。**

```
          政策/监管信号（最强alpha）
        ─────────────────────────
       资金流/北向/融资余额/龙虎榜
      ─────────────────────────────
     财报文本/公告/投资者问答/调研纪要
    ─────────────────────────────────
   行业景气度数据（高频中观数据）
  ─────────────────────────────────────
 基本面因子（ROE/PE/PB/现金流/...）
───────────────────────────────────────────
量价技术因子（动量/波动率/换手率/...）
```

**架构原则：量化 + LLM 混合**——信号融合层加权/机器学习融合，下挂量化因子模型、LLM Agent、另类数据模型。**不要让 LLM 直接输出买卖建议，而是输出结构化特征，交给量化模型。**

### 1.3 两次转变

| | 目标 A · 校准 | 目标 B · 提升预测力 |
|---|---|---|
| 问的是 | 该不该信自己、信到几分 | 更会预测涨跌 |
| 可行性 | ✅ 可行 | ❌ 基本不可行（个股日频收益可预测成分极低） |

第二次转变（用户定位层面）：**如果目标只是校准，回测就够了，Agent 不需要介入。** 校准的本质是「什么条件下历史命中率多少」——回测引擎天然擅长。Agent 真正不可替代的是**非结构化信息的感知与经验积累**。

**最终定位：经验引擎**

```
量化做筛选（掐尖50-100只）
        ↓
经验做判断（这只票在当前环境下值不值得做）
        ↓
校准做修正（我这类判断历史上命中率多少，该下多重注）
        ↓
结果做沉淀（对了为什么对，错了为什么错，存进经验库）
```

**中短线（T+1~T+20）恰好是量化因子最弱、经验最强的地带。**

| 时间框架 | 主导因素 | 谁更强 |
|---------|---------|--------|
| 长线（年级别） | 基本面、估值 | 量化碾压 |
| T+0 | 微观结构、订单流 | 量化碾压 |
| **中短线（T+1~T+20）** | **情绪、节奏、股性、题材生命周期** | **经验 > 量化** |

### 1.4 「老股民」与「分析师」的统一

> 经验给假设，分析给验证。

| 能力 | "老股民" | "分析师" | 系统状态 |
|------|---------|---------|---------|
| 模式触发 | 见过类似的，警觉起来 | — | 🆕 需建 |
| 结构化分析 | — | 拆维度、逐项检查 | ✅ 已有 |
| 信息覆盖 | 凭经验知道该看哪几个面 | 确保不遗漏、不偏食 | ✅ 已有 |
| 事实锚定 | — | 数字可溯源 | ✅ 已有 |
| 自我认知 | 大概几成把握 | 精确到条件命中率 | 🔄 需校准 |
| 直觉修正 | "不对劲，先撤" | 解释哪里不对劲 | 🆕 需建 |

**目标形态示例**：

```
用户："帮我看看XX科技"
  │
  ▼ 【老股民】经验检索
  → "底部放量+题材启动第二天+龙虎榜机构净买"
  → 历史相似案例 15 个，12 赚 3 亏（主窗口 T+3）
  → 3 个亏的都是"板块没跟"的情况
  │
  ▼ 【分析师】结构化验证
  → 板块联动度 ✅ / 量能结构 ✅ / 资金流 ✅ / 公告异动无 ⚠️
  → 发现板块联动度偏弱，触发反向证据检查
  │
  ▼ 【合成判断】
  → 方向：偏多，但板块联动弱是主要风险
  → 置信度：0.55（经验说 0.7，但反向信号调低）
  → 建议：若 T+1 板块启动确认则可做，否则观望
  → 留档：situational 特征 + 判断 + 置信度 + 主窗口 T+3 → 等 T+N 分窗口回填
```

---

## 2. 总架构：三条数据流 + 四层边界

### 2.1 三条数据流

```
【采集】用户提问 → QDAgent（现有主链，不动）→ 结论 + 证据
        → 投影成案例（确定性代码，不用 LLM）→ exp_cases / exp_outcomes（待回填）

【验证】案例分窗口到期回填（T+1 → T+20 陆续到期）→ 战绩账本 exp_claims
        ├─ 经验条目的真伪 = 纯统计（历史 / 回测逐笔 + label_spec 判定式，零 LLM）
        └─ 提智改动值几分 = mimoagent batch A/B（rollout + grader，花算力）

【生效】experience_search 检索 → build_digest 压缩 → 注入下一轮判断
        （呈现权重按战绩：待验证降权 / 有效正常 / 退化中减半 / 已退役不引用）
```

### 2.2 四层边界（谁依赖谁）

| 层 | 职责 | 依赖约束 |
|---|---|---|
| **在线主链**（QDAgent） | 问答、判断、案例采集 | 不依赖回测、不依赖 batch，实时可用性优先 |
| **经验内核**（exp_*） | 数据模型、口径契约、战绩账本、检索摘要 | 只依赖纯函数与 DB；判定口径单一事实源 |
| **回测外挂层**（providers） | 案例工厂 + 预测力测量场 | 只依赖 `BacktestProvider` **接口**；可插拔、可禁用（§8） |
| **离线验证底座**（mimoagent） | 提智改动 A/B、判分冒烟、SFT 轨迹 | 只在离线跑；失败不影响在线（§9） |

> **fail-open 红线**：经验库、回测外挂、离线底座都是**增益层**，绝不阻断规划/收尾主链；但失败必须 warning 可见（本模块高发「声明了没接线」，静默=断链复发）。

---

## 3. 经验分层 L0–L5（决策链映射）

```
L0 市场经验：现在能不能做？该做多重？   → 大盘状态/量能/情绪温度/季节性     [默认主窗口 T+10/15/20]
L1 板块经验：该做哪个方向？           → 轮动规律/联动度/板块生命周期       [默认主窗口 T+7/10]
L2 题材经验：这个题材还能不能跟？      → 题材阶段/扩散规律/龙头-跟风关系   [默认主窗口 T+3/5]
L3 个股经验：这只票具体怎么搞？       → 股性/形态规律/历史相似案例         [默认主窗口 T+1/3]
L4 策略经验：用什么方法切入？         → 低吸 vs 追涨/regime 与方法匹配     [默认主窗口 T+5/10]
L5 交易经验：怎么控节奏？             → 仓位/买卖点/止损止盈/加减仓时机    [默认主窗口 T+1/3]
```

**每层经验都有自己的条件-结果对，检索时按层独立查，最终由 Agent 综合。**

> **开放性原则**：情境特征空间、经验条目种类、判断类型全部开放，不设准入门槛——任何「我觉得……」都可以入库，包括直觉式的、还没想清楚的。受控枚举只用于**标签口径**（什么算命中、什么算到期），不用于**经验来源**（什么值得存）。

**默认主窗口只是默认**：写入时每条经验可自行声明 `primary_window`（只能选七档之一），换窗口必须在写入时声明，禁止事后改（§4.4）。

### 3.1 各层情境示例（situation，写入时投影，检索时硬过滤）

> L0 市场

```python
{
    "layer": "market", "primary_window": "T+10",
    "situation": {
        "index_trend": "震荡/上行/下行",
        "volume_regime": "放量/缩量/平量",
        "sentiment_temp": "冰点/低迷/中性/亢奋/过热",
        "northbound_flow": "大幅流入/小幅流入/平衡/流出",
        "margin_balance_trend": "上升/平稳/下降",
        "calendar_event": "财报季/春节前/季末/无",
        "policy_signal": "宽松/中性/收紧",
        "pattern": "底部企稳/高位放量/破位/横盘"
    },
    "judgment": {"action": "进攻/均衡/防守/空仓", "position_range": [0.3, 0.7], "confidence": 0.75}
}
```

> 示例经验：「两市缩量到 7000 亿以下 + 涨停数 < 40 + 北向流出 → 三天内出大阳线概率 25%，仓位建议 ≤ 3 成」

> L1 板块 / L2 题材

```python
{"layer": "sector", "primary_window": "T+7",
 "situation": {"sector": "半导体", "sector_rotation_day": 3, "sector_linkage": 0.65,
               "sector_volume_ratio": 1.8, "sector_vs_market": "领涨/跟涨/领跌/抗跌",
               "leader_status": "龙头涨停/龙头炸板/龙头调整", "breadth": 0.72, "policy_support": true}}

{"layer": "theme", "primary_window": "T+3",
 "situation": {"theme": "AI应用", "theme_stage": 2,   # 1启动 2发酵 3高潮 4分歧 5退潮
               "days_since_start": 4, "leader_gain": 0.35, "spread_to_followers": true,
               "news_catalyst": "政策", "market_attention": 0.8}}
```

> 示例经验：「板块轮动第 3 天 + 龙头炸板 + 跟风股大面积冲高回落 → 第 4 天补跌概率 70%」；「题材第 4 天以上 + 龙头涨幅 >30% + 全市场都在讨论 → 退潮概率 75%」

> L3 个股 / L4 策略 / L5 交易

```python
{"layer": "stock", "primary_window": "T+1",
 "situation": {"symbol": "600XXX", "sector": "半导体", "theme": "AI应用", "regime": "震荡",
               "stock_pattern": "底部放量突破",
               "stock_personality": {"volatility_rank": 0.8, "sector_follow": 0.9,
                                     "news_sensitivity": 0.6, "limit_up_momentum": 0.7,
                                     "liquidity_score": 0.65, "rebound_habit": "60日线",
                                     "auction_pattern": "高开低走", "turnover_cycle": "3天放量一次"},
               "special_signals": ["龙虎榜机构净买", "底部缩量企稳"]}}

{"layer": "strategy", "primary_window": "T+5",
 "situation": {"regime": "震荡", "theme_stage": 2, "stock_position": "底部启动", "market_sentiment": "中性"},
 "judgment": {"best_approach": "低吸", "entry_timing": "尾盘或次日竞价低开",
              "stop_loss": "-3%", "take_profit": "+6%", "hold_days": 3}}

{"layer": "trading", "primary_window": "T+1",
 "situation": {"portfolio_state": "首次建仓/已有浮盈/已有浮亏", "market_regime": "震荡",
               "consecutive_wins": 3, "drawdown_current": -0.02},
 "judgment": {"position_size": 0.3, "add_or_reduce": "减仓",
              "stop_rule": "跌破5日线无条件走", "max_hold_days": 5}}
```

> 示例经验：「这只票涨停后次日溢价均值 +2.1%，炸板后次日平均 -3.2%」；「震荡市 + 题材中期 + 底部启动形态 → 低吸胜率 62%，追涨胜率 38%」；「连续 3 笔盈利后 → 历史统计上第 4 笔亏损率反而升高到 55%」


---

## 4. 观察窗口与口径契约（v3 新核心）

### 4.1 窗口枚举（受控、单一事实源）

```
短线：T+1 / T+3 / T+5
中线：T+7 / T+10 / T+15 / T+20
```

* 单一事实源：`app/agent/exp/windows.py` → `WINDOWS = ("T+1","T+3","T+5","T+7","T+10","T+15","T+20")`，`SHORT = ("T+1","T+3","T+5")`、`MID = ("T+7","T+10","T+15","T+20")`；全仓只此一处定义，判定、回填、检索、digest 全部引用它。
* 按**交易日**计；到期取 `observed_at` 之后第 N 个交易日的真实行情（沿用现有 T+N 判定的取数口径）。
* 窗口是**受控枚举**——这与「经验开放」不矛盾：**特征开放、口径受控**。

### 4.2 主窗口规则（防「事后挑口径」的可执行形态）

1. 一条经验 / 一次判断**只在一个主窗口上判命中**（写入时声明，七选一）；
2. 其余窗口只记收益（ret / excess / maxdd），**不判对错、不进 hit_rate**；
3. 换主窗口 = 新条目（旧战绩不迁移），并标 `derived_from` 指向原条目；
4. 理由：七个窗口都能判对错，等于事后挑最好看的那个——正是统计契约要防的多重判定。
5. **自动化来源（`source IN {backtest, miner}`）的主窗口必须"先声明、后见结果"**：主窗口由策略 / 规格在**结果可见之前**预声明（pre-commit，随 `BacktestSpec` / miner 任务落库），案例工厂只做**确定性投影**，**不得按各窗口收益反选窗口**。理由：人工写入时"写入即决策"、天然先于结果；但回测 / miner 在成案例时结果已知，若允许此刻选窗口，则第 4 条所防漏洞从后门放回。此约束由案例工厂入口断言（`primary_window 在 trades 落盘前已固定`）+ 溯源字段（`run_ref` 指向声明记录）机检。

### 4.3 到期回填（分窗口陆续到期）

* T+1 次日到期，T+20 三周后；回填 worker 按 `(window, due_date)` 扫描，逐窗口写 `exp_outcomes`；
* **未到期 ≠ miss**：不到期的窗口行不存在；到期但取不到数 → `undecidable` / `data_missing`（不计 hit、不计权重——常量污染 ≠ 误判）；
* **退市 / 长期停牌 ≠ `data_missing`（防幸存者偏差）**：退市是**真实的不利结果**，若归 `data_missing` 直接不计权重，等于只统计"还活着的票"、系统性抹掉最差的尾巴。退市窗口单列 `verdict='delisted'`，按 label_spec 声明的最差路径（或专用口径）计入；长期停牌同理单列，不与"取数失败"混为一谈。
* 写保护：只回填 `verdict IS NULL` 的行；人工反馈（human_reviewed）任何自动流程不得覆盖（沿用 `backfill_by_root` 的 P1 写保护语义）。

### 4.4 label_spec：口径规格书（受控 + 版本化）

| judgment 类型 | outcome 字段 | 默认主窗口 | 命中判定式 | 备注 |
|---|---|---|---|---|
| 方向（偏多/偏空） | ret, excess_ret | 按声明 | 扣成本后的超额收益符号与判断一致 | 主力类型 |
| 仓位区间 | max_drawdown | 持有期 | 不判对错，只统计后续回撤是否超阈值 | |
| 打法（低吸/追涨） | ret_of_approach | T+5 / T+10 | 该打法相对另一打法的超额 | 需成对样本 |
| 止损/止盈规则 | execution_outcome | T+1 / T+3 | 触发规则后的实际滑点 vs 替代路径 | |
| 回避/观望 | counterfactual_ret | 同窗口 | 被回避标的同窗口收益 < 基准（回避正确） | 反向证据 |

* **版本化**：口径落表 `exp_label_specs(id, version, ...)`，案例与条目引用 `label_spec_id + label_spec_ver`；改口径 = **只增版本**，历史战绩不重算、不迁移。这就是「禁止事后挑口径」的可执行形态。
* **三条前置缺陷必须先修**（不修则一切统计虚高）：涨跌停可成交口径分开统计、扣交易成本、给相对基准——与**既有** `chain/schema.py::classify_return` / `DIRECTION_THRESHOLD`（实测定义在 `chain/schema.py:402/405`；`chain/evaluator.py` 仅 import 复用）同口径，**收敛到既有源、禁止再造第三份方向判定**。
* 判定式实现为**纯函数**（`chain/label_spec.py`）：在线 T+N 判定与离线 grader 调同一份实现，禁止两边各写一份。

### 4.5 统计契约（记账规则，对所有经验一视同仁）

| # | 条款 |
|---|---|
| 1 | 任何命中率输出必须带 n、Wilson 95% CI、beta-binomial 收缩后估计值 |
| 2 | 有效样本（聚类折算后）< 30 不输出命中率，只输出「样本不足，仅作先验」 |
| 3 | 模式升格（待验证→有效）需 out-of-time 验证 + FDR 控制（miner / 回测产物适用）；**BH 的检验家族（family）必须显式定义**——默认取「同一次升格批 × 同 layer」为一个 family，q 值只在该 family 内可比、跨 family 不合并 |
| 4 | 样本量按 date×theme 聚类折算（cluster-robust / block bootstrap），禁止名义计数；**聚类作用于"正在聚合的那个统计"**——hit_rate 只用主窗口样本（§4.2），而 `per_window` 收益 / cohort 分桶统计须把「同一案例的不同窗口」视为**非独立**（时间重叠观察），不得按窗口数放大 n_eff |
| 5 | 每类 judgment 写入时声明 label_spec（判定式 + 主窗口），禁止事后挑口径 |
| 6 | digest 统计行强制标注「样本充足 / 不足」 |
| 7 | 记录未出手与失败案例，undecidable 同样入库 |
| 8 | 有效性指纹常开：滚动 out-of-time ECE + 相对量化基线的增量 |
| 9 | **主窗口唯一**：一条经验只在主窗口判命中，其余窗口只记收益（v3 新增） |
| 10 | **口径版本锁定**：label_spec 版本随条目落库，改口径只增版本、不回填历史（v3 新增） |

### 4.6 口径契约补漏（2026-10-11 评审）

> 复核对码时发现 §4.1–§4.5 未覆盖的四类口径缺口；不补则统计在边界情境下系统性偏差。

| # | 缺口 | 补漏 |
|---|---|---|
| 1 | **特征缺失偏倚**：situation 依赖 resolver + 工具输出，工具失败即特征缺失，只在数据齐全的情境学习 | 三选一并显式落库：缺失值当**一类特征值**（`situation[key]="*missing*"`，可被检索命中）/ 降权 / 丢弃并计数。默认取"当一类特征值"，缺失率进统计指纹，禁止静默丢弃 |
| 2 | **冲突消解缺失**：多条经验对同一情境给反向结论时 digest 仅按权重并列 → 模型收到自相矛盾提示 | digest 构建时做**互斥检测**（同 layer + 同 situation 键、方向相反）→ 输出**净方向 + 显式下调置信度**（冲突未消解时代数抵消并标注"经验分歧"），禁止并列呈现 |
| 3 | **情境特征口径未版本化**：label_spec 版本化了，但 `regime/sentiment_temp/theme_stage/cohort` 等**特征枚举**没有 → 改特征定义 = 悄悄改口径，绕过"口径锁定"红线 | 特征枚举与 `exp/windows.py`、cohort 枚举**同批**落 `exp_feature_specs(id, version, enum_json)`；案例 / 条目引用 `feature_spec_ver`，改枚举只增版本（同 label_spec 语义） |
| 4 | **regime 单一事实源缺位**：全链强依赖 regime，却未指定谁算、多久刷新、失败如何降级 | 指定唯一产出方（盘前 cron 的 L0 快照，见 §18.1），随 `feature_spec_ver` 落库；失败 → 该轮 regime 置 `unknown` 并按"降权 + warning"降级，禁止各消费方自行估 regime |

---

## 5. 数据模型（v3 重设计）

### 5.1 实体关系

```
exp_label_specs（口径规格·版本化）
      ▲ 引用 (id, version)
      │
exp_cases（案例：情境 + 判断 + 声明）──1:N──► exp_outcomes（按窗口一行：T+1 … T+20）
      │
      │ N:1（可空）
      ▼
exp_claims（经验条目：条件→判断 + 战绩账本 + 四态状态机）
      │
      └──1:N──► exp_calibration（claim × window 桶：声称置信 vs 实际命中）
```

### 5.2 表 DDL（`migrations/agent_v6_experience.sql`，对齐 agent_v4_trace.sql 的幂等风格；现存迁移最高为 `agent_v4_trace.sql`、无 v5，本设计顺延为 v6）

```sql
-- ① 口径规格：受控、版本化；改口径只增版本，历史不重算
CREATE TABLE IF NOT EXISTS exp_label_specs (
    id             VARCHAR(64) NOT NULL,            -- dir_ret / approach / stop_rule ...
    version        INT         NOT NULL,            -- 口径变更只增版本
    judgment_type  VARCHAR(32) NOT NULL,            -- direction/position/approach/stop_rule/avoid
    default_window VARCHAR(8)  NOT NULL,            -- 默认主窗口（七档之一）
    outcome_fields TEXT[]       NOT NULL,            -- 需要哪些结果字段
    hit_expr       VARCHAR(64) NOT NULL,            -- 命中判定式（纯函数名，chain/label_spec.py）
    cost_model     VARCHAR(16) NOT NULL DEFAULT 'simple',     -- none/simple/realistic
    benchmark      VARCHAR(16) NOT NULL DEFAULT 'index',      -- index/sector/none
    tradable       VARCHAR(16) NOT NULL DEFAULT 'open_next',  -- open_next/vwap/na
    note           TEXT,
    created_at     TIMESTAMP DEFAULT now(),
    PRIMARY KEY (id, version)
);

-- ② 案例：一次观察（情境 + 判断 + 口径声明）
CREATE TABLE IF NOT EXISTS exp_cases (
    id              BIGSERIAL PRIMARY KEY,
    layer           VARCHAR(16) NOT NULL,            -- market/sector/theme/stock/strategy/trading
    entity_id       VARCHAR(32),
    observed_at     DATE NOT NULL,                   -- 情境日期（回测与实盘统一口径）
    situation       JSONB NOT NULL,
    situation_text  TEXT,                            -- 模板化生成（确定性）
    embedding       JSONB,                           -- 向量（JSONB + Python 余弦，见 §5.4）
    judgment        JSONB NOT NULL,
    primary_window  VARCHAR(8) NOT NULL,             -- 主窗口
    label_spec_id   VARCHAR(64) NOT NULL,
    label_spec_ver  INT NOT NULL,
    source          VARCHAR(16) NOT NULL,            -- agent/manual/backtest/miner
    claim_id        BIGINT,                          -- 关联经验条目（可空）
    run_ref         VARCHAR(64),                     -- 来源回测 run_id（外挂层回填）
    created_at      TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS exp_cases_layer_date ON exp_cases (layer, observed_at DESC);
CREATE INDEX IF NOT EXISTS exp_cases_claim     ON exp_cases (claim_id) WHERE claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS exp_cases_sit       ON exp_cases USING gin (situation);

-- ③ 结果：一行 = 一个窗口的到期回填（七窗口陆续到期）
CREATE TABLE IF NOT EXISTS exp_outcomes (
    case_id      BIGINT NOT NULL REFERENCES exp_cases(id) ON DELETE CASCADE,
    window       VARCHAR(8) NOT NULL,
    ret          NUMERIC,
    excess_ret   NUMERIC,
    max_drawdown NUMERIC,
    hit          BOOLEAN,             -- 仅主窗口判 hit，其余窗口恒 NULL
    verdict      VARCHAR(16),         -- hit/miss/undecidable/data_missing/delisted
    due_date     DATE,                -- 该窗口到期日（observed_at + N 交易日），写入时物化，供 (window,due_date) 扫描走索引
    resolved_at  DATE,
    updated_at   TIMESTAMP DEFAULT now(),
    PRIMARY KEY (case_id, window)
);
CREATE INDEX IF NOT EXISTS exp_outcomes_due ON exp_outcomes (window, due_date);

-- ④ 经验条目账本：任何经验可入库，战绩随行
CREATE TABLE IF NOT EXISTS exp_claims (
    id              BIGSERIAL PRIMARY KEY,
    claim_text      TEXT NOT NULL,
    layer           VARCHAR(16),
    condition_tpl   JSONB,
    prediction      JSONB NOT NULL,                  -- 判断 + 主窗口 + 观察窗口集
    label_spec_id   VARCHAR(64) NOT NULL,
    label_spec_ver  INT NOT NULL,
    source          VARCHAR(16) NOT NULL,            -- agent/manual/miner/backtest
    derived_from    BIGINT,                          -- 换主窗口 / 派生条目指向原条目
    -- 战绩账本（主窗口口径；对外只输出 shrunk_rate）
    n_effective     INT DEFAULT 0,
    hit_rate        FLOAT,
    ci_low          FLOAT,
    ci_high         FLOAT,
    shrunk_rate     FLOAT,
    per_window      JSONB,                           -- 各窗口收益统计（不判对错）
    status          VARCHAR(16) DEFAULT '待验证',     -- 待验证/有效/退化中/已退役
    regime_valid    VARCHAR(16)[],
    first_seen_at   DATE,
    last_hit_at     DATE,
    last_miss_at    DATE,
    created_at      TIMESTAMP DEFAULT now(),
    updated_at      TIMESTAMP
);
CREATE INDEX IF NOT EXISTS exp_claims_layer_status ON exp_claims (layer, status, n_effective DESC);
CREATE INDEX IF NOT EXISTS exp_claims_cond         ON exp_claims USING gin (condition_tpl);

-- ⑤ 置信度校准：claim × window 桶
CREATE TABLE IF NOT EXISTS exp_calibration (
    claim_id        BIGINT NOT NULL,
    window          VARCHAR(8) NOT NULL,
    pattern_type    VARCHAR(32),
    direction       VARCHAR(8),
    claimed_conf    FLOAT,
    actual_hit_rate FLOAT,
    sample_count    INT,              -- 必须是聚类折算后的有效样本
    calibrated_conf FLOAT,
    updated_at      TIMESTAMP,
    PRIMARY KEY (claim_id, window)
);
```

### 5.3 与 v2.1 的四处设计差异与理由

| 变化 | v2.1 | v3 | 理由 |
|---|---|---|---|
| 结果存储 | `exp_cases.outcome JSONB` + `verified_at DATE` 单点回填 | **独立表 `exp_outcomes`，按窗口一行** | 七窗口陆续到期，单点回填装不下「部分到期」状态（T+1 已判 hit、T+20 未到期） |
| 口径规格 | 文件规格书 | **落表 + 版本号 + 引用锁版本** | 「禁止事后挑口径」需要可执行约束：改口径只增版本、历史不重算 |
| 战绩分栏 | 单一 hit_rate | hit_rate **只按主窗口**，`per_window` 只存收益统计 | 防七窗口择优（§4.2） |
| 模式表 | `exp_patterns` 独立表 | **不建表**，由 `exp_claims` 聚合查询 / 物化视图产出 | 少一张要同步的表；miner 产物本来就是 claim（待验证赛道） |

### 5.4 存储裁决与既有校准分层

* 向量沿用本仓真实约束（见 `utils/case_memory.py` / `rag/pg_vector_store.py`）：**JSONB 存向量 + Python 余弦**，pgvector 扩展不可依赖、不建 ivfflat 索引；embedding 走 `rag/embeddings.py` 工厂（env `EMBEDDING_*`）；无配置退化 2-gram 词面相似（fail-open）。
* `qd_agent_decisions` 加 `symbol/theme/regime` 三列（同批 DDL；`chain/account_store.save_decision` 增参透传）。反面教材就在本仓：`chain/weight_feed.py` 的 P0-1 教训——decision 只带 domain/intent 时，最细只能诚实做到 domain 粒度，硬反推就是伪精确。
* **校准四层不混**：`utils/calibration.py`（isotonic，桶级）/ `app/services/ai_calibration.py`（market 级买卖阈值）/ `chain/weight_feed.py`（domain×EMA 慢调）/ 本设计 `exp_calibration`（claim×window 级）。共用 `qd_agent_weights` 存储时用独立 layer 隔离；`utils/calibration.py` 的 `_MIN_SAMPLES=10` 收紧到契约口径（n_eff≥30 才出命中率）。


---

## 6. 检索与经验摘要

### 6.1 两阶段混合检索

```
新任务
  │
  ▼ 【Step 1: 特征提取（确定性代码，0ms）】
  提取当前情境 → sector/regime/stock_pattern/theme_stage/...
  │
  ▼ 【Step 2: 结构化硬过滤（SQL，~5ms）】
  WHERE layer IN (...) AND regime=... AND sector IN (...)
    AND observed_at IS NOT NULL AND primary_window IN (...)
  → 候选池 200-500 条
  │
  ▼ 【Step 3: 向量语义排序（限扫 2000，JSONB + Python 余弦）】
  → 取 Top-20（无 embedding 配置退化词面相似，fail-open）
  │
  ▼ 【Step 4: 二次排序（代码，~1ms）】
  score = similarity × 0.4 + recency_decay × 0.2
        + hit_rate_weight × 0.2 + regime_match × 0.2
  → 最终 Top-8~12 条
```
> ⚠️ 二次排序四项权重（0.4 / 0.2 / 0.2 / 0.2）是**初值**，按本仓红线「规则不在代码里」**外置到 `exp/config.yaml`** 并登记「待测量场校准」，禁止写死在检索器里。

**跨层组合检索**（分析一只票时按决策链逐层查，每层按自己的默认主窗口取战绩）：

```
用户："帮我看看XX科技"
  ▼ 【L0 市场】"震荡市+缩量+情绪低迷 → 仓位 ≤5成，低吸为主"          [T+10]
  ▼ 【L1 板块】"板块轮动第3天，联动度0.55 → 仍有空间但不追高"        [T+7]
  ▼ 【L2 题材】"题材第2天，龙头首板，扩散初期 → 可跟"               [T+3]
  ▼ 【L3 个股】"底部放量突破，回调习惯位60日线"                      [T+1]
  ▼ 【L4 策略】"震荡+题材早中期+底部启动 → 低吸胜率62%"             [T+5]
  ▼ 【L5 交易】"首次建仓3成，止损-3%，持有3-5天"                    [T+1]
  ▼ 【合成判断】方向偏多 / 置信度 0.62 / 低吸 3 成 / -3% 止损 / T+5 强制离场
```

### 6.2 Experience Digest（不把原文丢给 Agent，压缩成 ~500 字符统计摘要）

**呈现原则**：

* **逐条经验的战绩引用是主力**，案例共性归纳降级为可选观察（`common_traits` 标注「提示性观察，非统计结论」）；
* 统计数字一律带区间和样本标注，宁可显示「样本不足」，不显示虚假精确的 67%；
* **每条战绩后面标主窗口**（T+3 之类）——不标窗口的命中率没有意义。

**输出示例**：

```
【历史经验摘要】
📌 相关经验（按战绩权重）：
1. 「题材第4天+龙头涨幅>30%+全市场讨论 → 退潮概率高」
   主窗口 T+5，n_eff=87，命中 71%（95%CI 60-80%），status=有效，最近验证 2026-09
2. 「板块联动度<0.4 时跟风股补跌」
   主窗口 T+3，n_eff=24，命中 63%（样本不足 ⚠️），status=待验证
3. 「连续3笔盈利后第4笔谨慎」
   主窗口 T+1，n_eff=12，命中 50%，status=退化中 ⚠️ 近期已不灵

📊 案例统计：命中 8/12（有效样本 n_eff=7）→ 样本不足，仅作先验
⚠️ 当前情境与历史差异：当前板块联动度 0.55，历史同区间案例集中在 2025Q4，regime 可比性存疑
```

> 注入通道：digest 走 mimoagent 的 compaction/surgery 形态注入（不自建上下文手术）；单例注入预算 ≤500 tokens（实测口径）。

---

## 7. 写入与回填

### 7.1 案例怎么产生（投影，不用 LLM）

```
Agent 完成一次分析
  │
  ▼
trace_collector finalize 后置钩子提取案例（确定性代码；同 case_memory.record_case 挂点）
  ├─ situation 特征 ← resolver + 工具输出 + 量化指标
  ├─ situation_text ← 模板化生成（确定性）
  ├─ embedding ← rag/embeddings.py 工厂
  ├─ judgment ← 结论（写入时声明 label_spec_id@ver + primary_window）
  └─ outcome ← 空，按窗口陆续到期回填
  │
  ▼
写入 exp_cases（+ exp_claims 关联：引用/印证/反驳了哪条经验）
```

`situation_text` 必须模板化生成：

```python
def build_situation_text(case):
    return (f"{case.regime}市|{case.sector}板块|个股形态:{case.stock_pattern}|"
            f"题材阶段:{case.theme_stage}期|信号:{','.join(case.special_signals)}|"
            f"板块联动度:{case.sector_linkage:.2f}|主窗口:{case.primary_window}")
```

### 7.2 分窗口到期回填

```
到期扫描（每日盘后 worker）
  → 取 exp_outcomes 中 window 到期且 verdict IS NULL 的行
  → 取真实行情（exec_date 为基准），按 label_spec 纯函数判 hit/miss
  → 写回（undecidable/data_missing 同样写，不计权重）
  → 触发 exp_claims 战绩账本更新（n_eff / Wilson CI / 收缩率 / last_hit_at / last_miss_at）
```

### 7.3 生命周期与呈现权重（门槛在权重，不在准入）

```
任何经验（agent直觉 / 人工录入 / 回测挖掘 / pattern_miner 产物）
    → 入库 status=待验证（可被检索引用，标注"仅为提示"，不参与置信度计算）
        ↓ 有效样本达到阈值（n_eff ≥ 30）+ out-of-time 检验通过
    status=有效（正常权重，digest 中带战绩与置信区间）
        ↓ 近期窗口命中率显著低于历史 / regime 变化检测触发
    status=退化中（降权 + 告警）
        ↓ 持续失效或机制性失效
    status=已退役（保留档案供复盘，不再被检索引用）
```

| status | 检索中 | digest 中措辞 | 对置信度的贡献 |
|---|---|---|---|
| 待验证 | 参与排序但降权 | "新经验，n_eff=3，仅作提示" | 0（不计入） |
| 有效 | 正常 | "n_eff=120，命中 62%（95%CI 53-71%）" | 按收缩后命中率计入 |
| 退化中 | 降权 | "⚠️ 近期失效中" | 减半计入 |
| 已退役 | 不引用 | 不出现 | 0 |

> 一条 n_eff=3 的新经验和一条 n_eff=200 的老经验**同时存在于库中**，区别只在引用时的措辞和分量。这正是「老股民」的真实状态：脑子里既有琢磨了十年的规律，也有最近才有的感觉，两者都有价值，但分量不同。

---

## 8. 回测外挂层（外挂 / 接口 / 工具）

### 8.1 定位：三个面

| 面 | 形态 | 谁用 |
|---|---|---|
| **接口** | `BacktestProvider` 协议（经验内核唯一依赖） | 经验引擎（案例工厂 + 测量场） |
| **外挂** | provider 注册表 + 配置开关，可插拔 / 可禁用 / 可替换 | 部署与运维 |
| **工具** | `tools/finance/backtest_tools.py::run_backtest / get_backtest_history` | Agent（分析个股时就地回测） |

> **内核零依赖原则**：经验引擎（exp_*）只 import 接口与数据结构，绝不 import 任何回测实现；回测实现挂了，经验主链照跑（fail-open + warning）。

### 8.2 接口契约

```python
class BacktestProvider(Protocol):
    name: str                     # "full_market" | "single_stock"
    scope: str                    # "universe"  | "stock"
    def run(self, spec: BacktestSpec) -> BacktestRun: ...
    def export_trades(self, run_id) -> Iterable[TradeRecord]: ...
    def mark_windows(self, trades, windows=W_ALL) -> Iterable[CaseDraft]: ...

@dataclass
class TradeRecord:      # 回测逐笔明细（两个 provider 统一输出）
    symbol: str; entry_at: date; entry_price: float
    exit_at: date; exit_price: float; approach: str     # 低吸/追涨/打板/...
    costs: float; benchmark: str; tags: dict            # 情境特征（由 provider 附带）

@dataclass
class CaseDraft:        # 案例草稿：已投影成 situation + judgment + 各窗口收益
    layer: str; entity_id: str; observed_at: date
    situation: dict; judgment: dict
    label_spec_id: str; label_spec_ver: int; primary_window: str
    outcomes: dict[str, WindowOutcome]                  # {"T+1": ..., "T+3": ...}
```

**要点**：`mark_windows` 是关键一步——回测的出场规则只给一个「策略收益」，但经验库需要**七个窗口各自的 mark-to-market 收益**（进场后第 N 个交易日的市值），所以 provider 必须把逐笔明细重标到窗口上。

### 8.3 两个 provider（代码落点，已核对）

| provider | 包装的实现 | 说明 |
|---|---|---|
| `full_market`（全盘策略） | `app/market_cn/auto/core/backtest.py`（`run_all` / `run_all_intraday`，strategies/ 插件 + `exit_engines`）；根目录薄壳 `run_backtest.py`、`backtest_daily_screener.py` | 全市场、策略级、逐笔明细可落盘（`--out tmp/*.json`） |
| `single_stock`（单股分析） | `app/services/backtest.py::BacktestService`（`run` / `run_multi_timeframe` / `run_strategy_snapshot` / `run_code_strategy`，`persist_run` / `list_runs` / `get_run` 已持久化）；单信号研究 `backtest_rally.py` / `backtest_realtime_monitor.py` | 单票 / 单情境、可成交口径已分开统计 |

* 注册与开关：`app/agent/exp/backtest_providers/` 下注册，配置（env / `exp/config.yaml`）决定启用哪些；缺依赖（如 yfinance 未装）时该 provider 标记不可用，**不阻断**其他 provider 与经验主链；
* ⚠️ **绑定契约而非文件**：`app/market_cn/auto/core/backtest.py` 本身是 auto 内核的 **P3/P6 退役对象（删除目标，见 `tests/present/test_kernel_size.py::RETIRE_MODULES`，目标 0 行）**——`run_all` / `run_all_intraday` 目前在此，但 provider 必须绑定它**产出的 canonical 逐笔契约**（`core/replay/trade_map.py::build_trade` + `strategies/base.py` 折叠契约），而非这个将被删除的编排文件；否则退役动作会连带打断 provider。
* Agent 工具面保持现状（`run_backtest` / `get_backtest_history`），即「工具方式出现」的那一面。

### 8.4 双重用途

**① 案例工厂（正向：回测 → 经验）**——直接解决 §3 冷启动：

```
回测逐笔明细（TradeRecord）
   → mark_windows() 标出 T+1/3/5/7/10/15/20 各窗口收益
   → situation_template() 投影成 situation（确定性）
   → judgment = 策略声明的打法/仓位/止损（写入时锁 label_spec 版本）
   → 落 exp_cases（source='backtest'）+ exp_outcomes（各窗口）
   → 一律 status=待验证；战绩账本照 §4.5 记账
```

**② 测量场（反向：回测 → 预测力测量）**——让「条件化预测力」可测，不等实盘：

1. **历史回放当预测任务**：截断到 T 时刻（只给 T 及之前的数据）→ 跑单股分析链路产出判断（方向 / 置信度 / 打法）→ 用真实走势按 label_spec 判分 → **条件命中率 + ECE + 校准曲线**（§10 有效性指纹的数据来源）。
2. **同窗口对照**：同一段历史、同一口径——全盘策略回测 = **量化基线**；经验引擎加持的单股分析 = **待测对象**；差值 = **经验引擎的净贡献**。
3. **A/B 归因**：经验开/关、金例开/关、digest 开/关 → 每个提智改动拿「条件命中率 +几分」的实测 diff。

### 8.5 三条口径红线（不修则测量场系统性骗人）

| # | 红线 | 理由 |
|---|---|---|
| 1 | 回测口径与 label_spec **同口径**（涨跌停可成交分开、扣成本、给基准） | §4.4 三条前置缺陷不修，测出的「预测力」是虚高 |
| 2 | 回测产的案例只进「待验证」赛道，升格需 out-of-time + FDR | 海量条件组合里回测，纯运气也能挖出「胜率 80%」——多重比较是回测的原罪 |
| 3 | 测量场判分与在线判定调**同一份纯函数** | 口径漂移会让「增量」是假的 |

---

## 9. 验证与升格（mimoagent 作离线底座）

### 9.1 分工：把算力花在刀刃上

| 问题 | 走哪条路 | 花什么 |
|---|---|---|
| 这条经验是真的吗？（战绩账本、升格/退役） | **纯统计**：历史 / 回测逐笔 + label_spec 判定式 | 零 LLM 成本 |
| 经验冷启动案例从哪来？ | **案例工厂**（§8.4①） | 零 LLM 成本 |
| 加了经验 / 金例 / 提智，预测力涨了几分？ | **mimoagent batch A/B**（rollout + grader + `--num-rollouts`） | 算力 |
| 判分链路可信吗？ | `--recalc-input gt` 冒烟 | 一次性 |

### 9.2 落地形态（已核对 mimoagent 源码）

| 件 | 形态 |
|---|---|
| 验证任务集 | `app/agent/exp/datasets/claims_YYYYMM.jsonl`：一行 = `{instance_id, dataset_type: "exp", task, situation, ground_truth, label_spec_id@ver, window}`；**真值只进 grader，不进 agent 上下文** |
| dataset adapter | 新 `app/agent/exp/exp_dataset.py`：继承 `DatasetEnvironment`，`HAS_VERIFIER=True`，`_do_calculate_reward()` 调 label_spec 纯函数；注册用**外部注入** `mimoagent.environments.datasets.DATASET_REGISTRY["exp"] = ExpEnvironment`（注册表是普通 dict，`dataset_type` 显式命中即路由，**不改 mimoagent 包**） |
| 判分壳 | `exp/graders/label_spec_grader.py`（薄壳，只做字段搬运） |
| 判分链路冒烟 | `mimoagent -c xxx.yaml --dataset tasks.jsonl --slice 0:1 --recalc-input gt -o outputs/smoke-gt`：先拿标准答案回放，期望 `Recalculated - Pass` 再跑真题 |
| 结果入账 | `exp/ledger_ingest.py`：`results.jsonl` → exp_claims 战绩账本（与在线回填共用写保护语义） |
| A/B 与回归 | 多份 `-c` 随机分配（一次跑两个 prompt/策略版本）；`--redo-existing` 断点续跑；`--workers` 并行 |

> ⚠️ **不要用 `generic` 数据集当判分载体**：它 `HAS_VERIFIER=False`（无内置 verifier），reward 只能来自 `rubric.rubrics` + judge 或 `--skip-reward`。要确定性判分就注册自己的 `exp` 类型。
> ⚠️ **`--num-rollouts K` 的 K 条轨迹是相关样本**：只用于估方差，不当 K 倍样本；n_eff 折算仍按契约条款 4。
> ⚠️ **基建故障不计 reward=0**：mimoagent 对 `REWARD_TESTBED_CORRUPTED` 整条 mask，与 undecidable 不计权重同款语义，入账侧不得当 miss。

### 9.3 轨迹 → 训练数据（SFT）

mimoagent 轨迹按 training schema 落盘（`traj.json` / `agent_msgs/` / `results.jsonl`），转换脚本现成（`scripts/convert_2_train_json.py`、`collect_fc_trajs.py`、`convert_mini_swe_traj_to_sft.py`）。这是 §1.1 第 6 条「天花板最高」那条路的采集端——金融域只需补字段映射。


---

## 10. 校准闭环与统计指纹

```
         ┌──────────────────────────────┐
         ▼                              │
   经验检索 → 判断 → 校准置信度 → 行动   │
                │                       │
                ▼                       │
        分窗口 T+N 真值回填             │
                │                       │
                ▼                       │
        ┌───────────────┐               │
        │ 经验库更新     │───────────────┘
        │ · 新案例入库   │
        │ · 旧模式修正   │
        │ · 置信度校准   │
        │ · 失败模式提取 │
        └───────────────┘
```

**置信度校准示例**（按主窗口分桶，n_eff≥30 才校准）：

| 经验说 | 该桶历史实际命中（主窗口 T+3） | 校准后输出 |
|--------|------------|-----------|
| "看多，confidence 0.8" | 65% | 修正为 0.65 |
| "看空，confidence 0.6" | 72% | 修正为 0.72 |

### 必须先建的五个统计指纹（防退化 + 防「自信地错」）

| 指标 | 定义 | 异常信号 | 数据来源 |
|------|------|---------|---------|
| 可证伪率 | claims 数 / 有实质结论的答复数 | 下降 = 在变滑头 | 在线计数 |
| undecidable 占比 | 判定为不可判定的比例 | 上升 = 在变滑头 | 回填计数 |
| 出声率 | 给出明确结论的答复占比 | 下降 = 保守化退化 | 在线计数 |
| 反向证据率 | 给方向结论时调用反向工具的比例 | 下降 = cherry-picking | 在线计数 |
| **有效性指纹** | 滚动 out-of-time 命中率 vs 报告置信度的 ECE + 相对量化基线的增量 | ECE 上升或增量归零 = 统计在失真 | **§8.4② 测量场** |

> 前四个指纹复刻 `chain/judge_stats.py` 的**事件消费形态**（只观测不改判定行为），但**必须落盘、禁用模块级进程内计数**：`judge_stats.py` 现为模块级 `Counter`（重启归零，`reset()` 仅供测试用）；四指纹落盘到既有面板通道 `traces/panel.jsonl`（`trace_collector` 已按 run 写 panel、`scripts/weekly_panel.py` 聚合），否则滚动窗口指标在重启后失真。
> 有效性指纹由测量场产出，滚动窗口计算，存 `exp_calibration` 侧。
> **这五个指标同时就是「预测力是否真在涨」的主仪表盘**——不要用全局 R² 或总命中率。

---

## 11. 实施优先级

```
第一批：口径与地基（1 周内，其他一切的前提）
├── 1  exp/windows.py（七窗口单一事实源）+ chain/label_spec.py 纯函数
│      ★ 同批修三条口径缺陷：涨跌停可成交 / 扣成本 / 相对基准（不修则统计虚高）
├── 2  migrations/agent_v6_experience.sql（五张表）+ qd_agent_decisions 加 symbol/theme/regime
├── 3  exp_stats.py（effective_sample_size 聚类折算 / Wilson CI / beta-binomial 收缩）
└── 4  五个统计指纹计数器（chain/judge_stats.py 形态扩展）

第二批：回测外挂层（2-3 周）
├── 5  BacktestProvider 接口 + full_market / single_stock 两个 provider
├── 6  mark_windows()：逐笔明细重标七窗口 mark-to-market 收益
└── 7  backtest → exp_cases 导入器（确定性投影，零 LLM）；agent 工具面补窗口字段

第三批：写入 / 检索 / 生效（2-4 周，与第二批部分并行）
├── 8  case_memory 改造为经验库写入端（situation + claim 关联 + 四态 status）
├── 9  分窗口到期回填 worker + 战绩账本更新（含 P1 写保护）
├── 10 experience_search 两阶段检索
├── 11 build_digest（注入走 mimoagent compaction 通道）
└── 11b 引用账本 exp_claim_citations（§14.2）+ 经验数字溯源（§14.3）+ 消费口分工（§14.4）

第四批：测量与验证（第三批一落地就开跑——早跑早知道值几分）
├── 12 预测力测量场：历史回放 + 同窗口对照（量化基线 vs 经验加持）→ 有效性指纹首跑
├── 13 mimoagent exp dataset adapter + label_spec grader + 判分冒烟（提智改动 A/B 通道）
├── 13b 判定器自检（§14.5）：label_spec 回归用例 + 分歧抽样人工复核触发通道
└── 14 exp_calibration（claim×window）+ 归因标注（先观测后动作，攒一个月定枚举）

第五批：生成与规模化
├── 15 pattern_miner 候选生成器（受统计契约门控，只产"待验证"，验证走 12/13）
├── 16 L2 题材生命周期经验库 / 跨 regime 个股级经验
└── 17 SFT 轨迹导出（金融域字段映射）

────────── 以下为 v3.2 追加（§20），依赖前五批，不占用其工期 ──────────

第六批：三门地基（所有"指标变高"的前提）
├── 18 exp/features/pit.py：PIT 特征层（as-of 契约 + 快照落盘 + 同值 smoke）
├── 19 exp/residual.py：超额口径改因子残差（依赖 14b 灌满 factor 权重）
└── 20 可验证输出契约（direction/main_window/confidence/判别特征/falsifier 全填才落案例）

第七批：提智（让指标变高）
├── 21 判别特征挖掘（hit vs miss contrastive + 决策前主动查）
└── 22 证伪 gate（反事实检查前置；反向证据率指纹同步改口径）

第八批：变现（把预测力变成决策）
├── 25 校准 → 仓位映射 exposure policy（先出声不上仓）
├── 26 EV 排序输出（§1.2 唯一可外部 RankIC 验证的出口，优先做）
├── 23 集成 + 已校准仲裁（非多数投票，仲裁器自身也要计分）
└── 24 自适应算力（utils/budget.py 反向用法；检索/digest/仲裁 token 计入 run 预算）

第九批：高风险高回报（可延后/可不做）
└── 27 战绩 → 偏好优化 DPO（前置：18 + 19 + 20，先做小样本可分离性验证）
```

---

## 12. 风险与红线

| 风险 | 对策 |
|------|------|
| **沉积是放大器不是纠偏器**：采集有偏则固化偏差 | 先建统计指纹监测（§10），再谈沉积 |
| **X 空间不稳定**：问法/路径/结论每次不同 | 投影层做确定性映射（代码做，不用 LLM） |
| **有效样本 ≠ 名义样本**：同题材同天高度相关 | 按 date×theme 聚类折算，分 regime 分桶 |
| **保守化退化**：越校准越不敢说话 | 监控出声率，设下限告警 |
| **归因不能信**：LLM 几乎总能给出合理解释 | 归因落在受控枚举 + 客观锚点；digest 中共性归纳降级为「提示性观察」 |
| **label 口径缺陷**：不可成交 / 未扣成本 / 无基准 | 前置修完再开始沉积（§4.4） |
| **多重比较 / 模式过拟合**：海量条件组合里挖出「胜率 80%」 | FDR 控制 + out-of-time 升格 + miner/回测产物只进「待验证」 |
| **自反馈闭环**：经验影响判断 → 产生新案例 → 反过来强化旧经验 | out-of-time 验证 + 记录未出手样本 + 漏斗版本号 |
| **经验过期（对手盘进化）**：跟庄痕迹被反向利用 | decay + regime 分桶 + 近期样本加权 +「退化中」状态机真执行 |
| **七窗口择优（v3 新增）**：一条经验在 7 个窗口里挑最好看的报 | 主窗口唯一（§4.2）+ 口径版本锁定（§4.4） |
| **回测口径漂移（v3 新增）**：测量场与在线判定各写一份 | 判定式单一纯函数源，smoke 断言同输入同输出 |
| **离线底座拖累在线（v3 新增）**：mimoagent/回测挂了影响实时问答 | 内核零依赖 + fail-open；batch 只在离线跑 |
| **把 rollout 当独立样本（v3 新增）** | `--num-rollouts` 只估方差；n_eff 仍按条款 4 折算 |
| **特征前视（v3.2 新增）**：situation 取到当时不知道的值，条件命中率系统性虚高 | PIT 契约 + 快照落盘 + 在线/离线同值 smoke 断言（§20.1） |
| **因子重复发现（v3.2 新增）**：经验只是重述已知因子，增量长期为正却不可变现 | 基准超额 vs 残差 alpha 双轨记账，跑满一个季度再下结论（§20.2） |
| **证伪 gate 让指纹失真（v3.2 新增）**：反向证据率变恒高常数 | 改用「证伪后仍维持方向结论」的比例（§20.5） |
| **仓位放大校准误差（v3.2 新增）**：分桶误差 × 仓位 = 真金白银 | 先出声不上仓；exposure 与校准分桶同步标定上限（§20.8） |
| **契约不全即丢弃（v3.2 新增）**：造成采集偏 | 契约不全单独计数作表达退化信号，禁止静默丢弃（§20.3） |
| **DPO 学进脏口径（v3.2 新增）** | 18 / 19 / 20 三条地基不落地就不开训（§20.10） |

---

## 13. 与现有代码的接线映射

| 设计组件 | 真实代码位置 | 动作 | 优先级 |
|---|---|---|---|
| 七窗口常量 | 新 `app/agent/exp/windows.py` | 新建（单一事实源） | 1 |
| label_spec 纯函数 | 新 `app/agent/chain/label_spec.py` | 新建（在线 T+N 判定与离线 grader 共用） | 1 |
| 五张表 | 新 `migrations/agent_v6_experience.sql`（对齐 agent_v4_trace.sql 幂等风格） | 新建 | 2 |
| situation 上下文写入 | `chain/account_store.save_decision` + `chain/claims.py` | 改造（decisions 加 symbol/theme/regime 三列） | 2 |
| 统计工具 | 新 `app/agent/exp/exp_stats.py` | 新建（Wilson / 收缩 / 聚类折算） | 3 |
| 统计指纹 | `chain/judge_stats.py` 形态扩展（只观测不改行为） | 扩展 | 4 |
| 回测接口 + providers | 新 `app/agent/exp/backtest_providers/`；包装 `app/market_cn/auto/core/backtest.py` 与 `app/services/backtest.py` | 新建薄壳（内核零依赖） | 5 |
| 案例导入器 | 新 `app/agent/exp/ingest_backtest.py` | 新建（确定性投影） | 7 |
| 案例写入端 | `utils/case_memory.py`（record_case 钩子） | 改造（situation + claim 关联 + 四态 status） | 8 |
| 分窗口回填 | `chain/resolver.py` + `chain/evaluator.py`（复用 backfill_by_root 写保护） | 改造（按 window 扫描到期） | 9 |
| 两阶段检索 | 新 `app/agent/utils/experience_search.py` | 新建 | 10 |
| 摘要压缩 | 新 `app/agent/utils/experience_digest.py` | 新建（注入走 compaction） | 11 |
| 测量场 | 新 `app/agent/exp/metrics/`（历史回放 + 同窗口对照） | 新建 | 12 |
| mimoagent adapter / grader | 新 `app/agent/exp/exp_dataset.py` + `exp/graders/label_spec_grader.py` | 新建薄壳（`DATASET_REGISTRY` 外部注入，不改包） | 13 |
| 置信度校准 | `exp_calibration`（claim×window）；拟合参照 `utils/calibration.py` | 新建（独立 layer 隔离） | 14 |
| 候选生成器 | 新 `app/agent/cron/` 定时任务 | 新建（受统计契约门控） | 15 |
| 引用账本 `exp_claim_citations` | `migrations/agent_v6_experience.sql` + 写入端 `chain/intake.py` 后置钩子 | 新建（决策×知识联结，§14.2） | 11b |
| 经验数字溯源规则 | `utils/grounding.py` | 扩展（命中率/CI/n_eff 必须溯源到 exp_claims 行，§14.3） | 11b |
| 历史信号消费口 | `qd_service._prefetch`（digest 与 weight hint 分工去重） | 改造（同 `chain/weight_hints.py` 唯一入口哲学，§14.4） | 11b |
| 判定器自检 | `chain/label_spec.py` 回归用例 + 人工复核触发通道 | 新建（§14.5） | 13b |
| factor 层喂养 | `chain/evaluator.update_weights`（A1b factors 命名统一后） | 改造（用 per-window 收益，§14.6，顺手闭 D-1） | 14b |
| PIT 特征层 | 新 `app/agent/exp/features/pit.py`；参照 `core/replay/mkt_slots.load_market_slots` 的逐槽 as-of 纪律 | 新建（as-of 契约 + `exp_cases.situation_snapshot`，§20.1） | 18 |
| 残差 alpha 口径 | 新 `app/agent/exp/residual.py`；上游 `chain/store.get_factor_weights`（`chain/store.py:710`，factor 层现恒空） | 新建（双轨记账，§20.2） | 19 |
| 可验证输出契约 | `chain/claims.py` 提取端 + `utils/case_memory.record_case` 前置校验 | 改造（字段不全不落案例但计数，§20.3） | 20 |
| 判别特征挖掘 | 新 `app/agent/exp/discriminative.py`（hit vs miss contrastive） | 新建（受 FDR + out-of-time 门控，§20.4） | 21 |
| 证伪 gate | 新 `app/agent/chain/falsify_check.py`（纯函数 + 工具清单） | 新建（§20.5；同步改反向证据率指纹口径） | 22 |
| 集成与仲裁 | 新 `app/agent/exp/arbiter.py`（吃校准权重，非多数投票） | 新建（自身进 `exp_calibration` 计分，§20.6） | 23 |
| 自适应算力 | `utils/budget.py`（实测零消费方）接入 `_run_inner` | 改造（反向用法：深度 ∝ 不确定度，§20.7） | 24 |
| 仓位映射 | 新 `app/agent/exp/exposure.py`（`exposure = f(校准置信, 赔率, regime)`） | 新建（先出声不上仓，§20.8） | 25 |
| EV 排序输出 | `tools/finance/` 排序出口 + `scripts/qd_bench.py` 加 RankIC 断言 | 新建（对外唯一客观标尺，§20.9） | 26 |
| DPO 偏好优化 | 复用 `exp/exp_dataset.py`（§9.3 轨迹 schema）+ `exp_outcomes` 打标 | 新建（前置 18+19+20，§20.10） | 27 |

> 优先级列 = §11 的**批次-序号**；`1`~`17` 与 §11 一一对应，`11b` / `13b` / `14b` 为同批追加项（原表 `8/11/13/14` 有重复、且与 §11 的 `11b/13b` 不一致，2026-10-11 评审修正）；`18`~`27` 为 v3.2（§20）追加项。

---

## 14. 突破点：决策账本 × 知识账本（v2 闭环与问责的遗产）

> 2026-10-11 · 任务「v3 复核」· OpenClaw agent。
> **v2 是决策账本**（问责：这次错算谁的），**v3 是知识账本**（经验：这类规律灵不灵）。
> 突破点在两个账本的**交叉处**——v3 若只做知识账本，就丢了「经验被谁用了、用了之后判断变好还是变坏」这条因果链，也就谈不上自我迭代。

### 14.1 六条继承与突破

| # | v2 的可取之处 | v3 现状 | 突破点 |
|---|---|---|---|
| ★1 | `decisions → claims → resolutions` 追责链 | 只有 exp_cases/exp_claims，不知道经验被谁用了 | **引用账本 `exp_claim_citations`**：记「经验被引用后那次判断对不对」= 因果贡献度（§14.2） |
| ★2 | 递进归因 `inherited-miss`（M 项） | 经验账本是平面的 | claim 加 `depends_on`：上游经验错导致下游判断错，下游不背锅、不重复喂数（§14.2 规则 2） |
| 3 | grounding gate（数字溯源） | 只说 digest 是确定性压缩 | 结论/digest 里的命中率、CI、n_eff 必须溯源到 `exp_claims` 行，LLM 复述战绩不许改数（§14.3） |
| 4 | `weight_hints` 唯一消费口 | 经验 digest 与权重 hint 两个历史信号并存、无分工 | 案例级 vs 聚合级分工 + 去重，防模型收到自相矛盾的历史提示（§14.4） |
| 5 | `judge_stats`（判定器自己也可能错） | 只有 human_reviewed 写保护 | label_spec 纯函数配回归用例 + 分歧抽样人工复核的触发通道（§14.5） |
| 6 | 因子层未闭环（v2 遗留 D-1） | 未接 | 用 `exp_outcomes` 的 per-window 收益喂 `qd_agent_weights(layer='factor')`，顺手闭 D-1（§14.6） |

> 三条姿态（fail-open / 只观测不改行为 / 宁漏不脏）v3 已继承，不重复建。

### 14.2 `exp_claim_citations`：经验的因果贡献度

```sql
-- 决策账本 × 知识账本的联结表：一次判断引用了哪条经验、结果如何
CREATE TABLE IF NOT EXISTS exp_claim_citations (
    id            BIGSERIAL PRIMARY KEY,
    claim_id      BIGINT NOT NULL REFERENCES exp_claims(id),
    decision_id   BIGINT NOT NULL,        -- 关联 qd_agent_decisions.id（决策账本）
    case_id       BIGINT,                 -- 可空：该次判断对应的 exp_cases
    used_weight   FLOAT,                  -- 引用时给的呈现权重
    cited_status  VARCHAR(16),            -- 引用时该经验的状态（待验证/有效/退化中）
    outcome_delta NUMERIC,                -- 该次判断相对基线的增量（测量场/回填后写入）
    verdict       VARCHAR(16),            -- hit/miss/inherited-miss/undecidable
    created_at    TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS exp_cit_claim ON exp_claim_citations (claim_id);
CREATE INDEX IF NOT EXISTS exp_cit_dec   ON exp_claim_citations (decision_id);
```

**归因规则（四条）**：

1. **升格判据升级**：从「这条经验自己命中率高」升级为「用了它判断更准」——`contribution = mean(outcome_delta | cited)`，与 hit_rate 并列进状态机。hit_rate 60% 的经验若引用时反而拉低判断（常出现在难做行情里）不该升格；hit_rate 52% 但稳定提升判断质量的该加权；
2. **`inherited-miss` 语义**：claim 带 `depends_on`（上游 claim_id / turn 序号）；上游 miss ⇒ 下游记 `inherited-miss`，不重复惩罚、不重复喂数；上游 hit、下游 miss 才归因下游；
3. **样本纪律同款**：引用计数 < MIN_SAMPLES 只记账不改权重（沿用 `weight_feed` 的保守慢调）；
4. **引用批次归一**：某条经验引用次数暴涨（如 prompt 改动导致）时，贡献度按引用批次归一——防「被引用得多 = 贡献大」的假象。

### 14.3 经验数字的溯源（复用 grounding gate）

* 结论 / digest 中的命中率、CI、n_eff **必须能溯源到 `exp_claims` 行**（即出现在本轮注入块的战绩行里），否则改写为「未获取」；
* 落点：`utils/grounding.py` 增加「经验数字」规则；smoke 断言形态同现有数字溯源门（编造命中率必须被拦）；
* 理由：digest 是确定性压缩，但结论是 LLM 复述——**复述环节是战绩数字被篡改的唯一入口**。

### 14.4 历史信号的唯一消费口（防双重计数）

| 信号 | 粒度 | 注入位置 | 禁止 |
|---|---|---|---|
| 经验 digest | **案例级**（哪几条经验、战绩多少、主窗口） | `_prefetch` → digest 块 | 不得在权重 hint 里重复该经验的数字 |
| 权重 hint | **聚合级**（domain/skill/tool 权重） | 预选 history_hint / `_rank_skills` | 不得展开成案例明细 |

> 同一历史事实在两处注入 = 模型收到自相矛盾的历史提示。消费口收敛成一个读取函数，同 `chain/weight_hints.py` 的「唯一入口」哲学。

### 14.5 判定器自检（label_spec 也可能错）

* 纯函数配**回归用例**：同输入同输出 + 口径三缺陷边界用例（涨跌停可成交 / 扣成本 / 相对基准）；
* **分歧抽样人工复核**的触发通道：测量场 ECE 超阈、undecidable 率异常时自动提样送复核（谁触发要写清，不能靠人想起来）；
* 人工反馈 `human_reviewed` 写保护沿用：自动流程不得覆盖。

### 14.6 顺手闭掉 v2 遗留 D-1（因子层未闭环）

`exp_outcomes` 的 per-window 收益是 `get_factor_weights` 恒空的解药：factors 命名统一（A1b）后，用 T+N 各窗口真实收益喂 `qd_agent_weights(layer='factor')`，五维因子权重（bull_bear_research 的 ma/macd/rsi/boll/kdj）从启发式常量变成有战绩的校准值。

---

## 15. 运行形态与依赖边界（mimoagent：双环境 + pin + 外部扩展）

> 2026-10-11 · 任务「v3 复核」· OpenClaw agent。裁定：**不整体内嵌**。

### 15.1 裁定

| 位置 | 形态 | 说明 |
|---|---|---|
| **在线主链** | 现有 `.venv`，mimoagent 以**包**形式、版本 pin 死 | 只做子类化（`QDAgent(MimocodeAgent)`、`ExpEnvironment(DatasetEnvironment)`）+ `DATASET_REGISTRY` 外部注入 + `add_action_interceptor`，**不改包** |
| **离线底座** | **独立环境**（`uv sync` 起的 mimoagent 环境 / 容器） | batch A/B、判分冒烟、SFT 轨迹；与在线物理隔离，靠 JSONL 任务集 / `results.jsonl` 交换 |
| **例外** | 局部 vendor `app/agent/exp/mimo_patch/` | 仅当必须改核心循环且子类挂不进去；配 smoke 断言，防升级时被静默丢弃 |

> 这条边界正是 §2.2「离线底座只在离线跑」的落地形态：环境隔离与代码隔离同向。

### 15.2 为什么不整体内嵌

1. 与本仓红线「网上找轮子比自己造轮子更好」直接冲突；
2. 311 文件 / 2.2MB 变成本仓责任，smoke 回归面扩大一倍；
3. 多人协同会把它改脏（共享区只增量追加的约定也不适用于它）；
4. **v3 的挂载点根本不需要改核心循环**——注册表注入 / 子类 / 拦截器已覆盖全部需求，内嵌买来的自由度用不上；
5. 上游的踩坑成果（detached exec、`REWARD_TESTBED_CORRUPTED` mask、answer leak prevention）自己维护 = 重新踩一遍。

### 15.3 翻到内嵌的触发条件（任一命中，且只 vendor 必要模块）

* 必须改 `BaseAgent` 生命周期、且 `add_action_interceptor` / 子类都挂不进去；
* 上游停更或 0.1.0-alpha 契约大改（盯 `CHANGELOG` 与 `AGENTS.md`）；
* 必须甩掉 `kubernetes==36.0.3` 这类重依赖、且 extras 解决不了。

### 15.4 依赖与部署要点

* mimoagent 精确 pin `openai==3.3.1` / `anthropic==1.0.0` / `kubernetes==36.0.3`，`requires-python==3.12.*`；与在线 venv 的冲突风险主要在 openai 版本——**双环境隔离即解**；
* 开发机 Windows 而 mimoagent 定位 POSIX/Linux：`environment_class: local` 在 Windows 不可靠（smoke 的 `posix_shell` 探针即证据）；**batch 一律跑 docker / k8s**；
* 离线 wheel 缓存放 `vendor/wheels/`（断网可装）；升级 mimoagent 后跑 smoke 248 项 + 判分冒烟（`--recalc-input gt`）双回归。

---

## 16. 因股制宜：异质性、分桶校准与策略自适应

> 2026-10-11 · 用户猜想 + 任务「v3 取舍分析」· OpenClaw agent。
> **猜想**：同一条回测策略在不同股票上可能完全相反；agent 通过经验为不同股票制定不同的回测 / 校准策略——这是提高预测力的强有力表现之一，也是「自我迭代」的具象形态。

### 16.1 现象与机制

* **异质性是常态，不是噪声**：低吸 vs 追涨、止损深度、持有期、甚至主窗口，都会因股性（波动率、板块跟度、题材敏感、流动性、涨停基因）而**反转**。全局统计的「低吸胜率 62%」是平均效应，掩盖了「A 类股 80%、B 类股 35%」的结构。
* **传统做法抹掉了它**：回测一套参数跑所有票 → 得到平均策略；校准一条曲线 → 得到平均置信度。
* **本设计把异质性当信号**：回测参数按股选择、校准曲线按桶区分、异质性自动发现。

### 16.2 三件机制

**① `strategy_profile`：回测参数按股选择（agent 自己制定回测策略）**

* `BacktestSpec` 增加 `strategy_profile`：{approach 优先级、止损、止盈、持有期、主窗口、仓位}；
* 由**经验检索产出**：分析某股前，先查该股 / 该股性历史上「哪类打法、哪个窗口战绩最好」，据此生成 profile，再交给 provider 回测验证；
* 关键转变：**回测从「验证一个全局策略」变成「验证一个针对该股的假设」**——这正是条件化预测力的测量方式。

**② 分桶校准（cohort）：置信度按股性分桶**

* `exp_calibration` 主键扩为 `(claim_id, window, cohort)`；
* cohort = 股性桶，**受控枚举**（如波动率 3 档 × 板块跟度 3 档 = 9 桶，不许自由组合）；
* **分层收缩**：个股样本不足时向上共享（个股 → 股性桶 → 全局），n_eff 约束同款（<30 不出命中率）；
* 输出形态：同一条经验在不同 cohort 给不同校准后置信度（「看多 0.8」→ 高波动桶 0.62 / 低波动桶 0.78）。

**③ 异质性自动发现（pattern_miner 的一类候选任务）**

* 候选任务：同一策略在不同 cohort 上的收益差（interaction effect）；
* 升格门槛照旧：FDR 控制 + out-of-time + 只进「待验证」赛道；
* 产物形态：经验条目「该打法只适用于 X 股性」（layer=strategy，condition_tpl 带 cohort 约束）。

### 16.3 红线（异质性研究的三个坑）

| 坑 | 对策 |
|---|---|
| 分桶 = 多重比较爆炸（股票 × 窗口 × 策略 × 参数） | cohort 受控枚举 + 桶数上限（≤9）+ FDR + out-of-time |
| 个股定制 = 过拟合个股历史（单票样本极少） | 分层收缩：个股 → 股性桶 → 全局；个股经验 n_eff 不足只作提示 |
| 自反馈：agent 选的策略会改变未来数据分布 | `strategy_profile` 落版本号随案例记录；贡献度按引用批次归一（§14.2 规则 4） |

### 16.4 与 L3 `stock_personality` 的关系

§3 的股性画像字段（volatility_rank / sector_follow / news_sensitivity / …）此前只是**检索特征**；本节把它升级为**决策变量**——既选回测参数，也进校准桶。同一字段两用，取值口径单一事实源（与 `exp/windows.py`、cohort 枚举同批落地）。

> **这就是「自我迭代」的具象形态**：agent 不只改进自己的判断（经验/金例/prompt），还**改进自己的验证方式**（为不同股票选不同的回测与校准策略）——v2 缺陷「不能自己总结经验和迭代升级」的解法之一。

---

## 17. 取舍总表：已实现的旧功能在 v3 的处置

> 2026-10-11 · 任务「v3 取舍分析」· OpenClaw agent。
> 背景：`agent差距与改进路线.md`（A~M）的功能**已基本全部实现**（2026-10-11 代码实证）。文档确有滞后——最典型的是 H：旧文档写「未做」，实际 `utils/budget.py` 机制全建，只是**没接线**（`check_budget` / `budget_wrapup_hint` 零消费方）。
> **取舍原则（唯一主轴）**：能否提高**条件化预测力**——① 直接服务预测力 → 进核心；② 服务工程效率 / 回答质量 → 外围保留；③ 被更好机制取代或与新架构冲突 → 降级 / 丢弃。

### 17.1 处置表

| 旧功能（已实现） | v3 处置 | 理由 |
|---|---|---|
| A 规划质量（`_plan_lint` + prompt 规划 5 题） | **保留**（在线主链） | 判断质量的前提，不进经验引擎 |
| B 执行型技能分发（run_skill / 分发卡片 / dispatch_ab） | **保留 + 形态外溢** | 白名单 + 子进程 + 产物落盘 + 对照实验法 = 回测外挂层的现成范式（§8 照抄同款纪律） |
| C qd_bench 五维回答质量 | **保留但降为外围** | 它测「答得好不好」，测量场测「预测准不准」——两个仪表盘不混；确定性打分器可复用给 digest 质检 |
| D 会话持久化 | **维持裁定不做** | 理由（闭环问责污染）仍成立 |
| E case_memory | **升级为经验库写入端** | 骨架全复用（record / retrieve / backfill / P1 写保护 / embedder 已接线），只扩表与状态机 |
| F 参数精调（单变量扫描） | **丢弃，被测量场 A/B 取代** | 扫描是拍脑袋参数的补救；有 A/B 直接出 diff，不必单开流程 |
| G 权重乘子（`_rank_skills`） | **保留**（聚合级信号） | 与经验 digest 分工（§14.4），不重复计权 |
| H 成本 SLO（`utils/budget.py`） | **保留 + 接线**（唯一的欠账） | 机制三线齐备但零消费方；接进 `qd_agent._run_inner` 后经验检索 / digest 一并吃预算 |
| I 技能 TL;DR | **保留**（内容活） | 渐进式加载的一部分；auto-* 四个待写 |
| J 发布工程（smoke 248 + `--with-env`） | **保留** | 回归底线；v3 新组件必须同步补 smoke |
| K 可学习预选（preselect_cooc） | **保留**（工具面，与经验库正交） | 两者都是「历史影响选择」，计权口径要分开 |
| L MiMo-Code 路线 B | **保留存档** | v3 的 mimoagent blackbox 就是它的接入点，条件触发 |
| M 递进链（原未施工） | **并入 v3 §14.2** | `depends_on` + `inherited-miss` 成为归因规则 |
| 追责四表 / grounding / 闸门 | **并入 v3 §14**（决策账本） | 已做 |
| qd_prompt_eval（prompt 回归） | **保留** | 提智改动的回归底线 |
| skill_brewer（技能酿造） | **与 pattern_miner 共用状态机** | 别建两套「候选 → 待验证 → 升格」流水线 |
| preselect_stats / judge_stats（观测计数器） | **保留形态** | v3 统计指纹照抄（只观测不改行为） |

### 17.2 明确丢弃（不做，省下工作量）

* F 的参数单变量扫描流程；
* E 的「金例高分选例」独立机制（被战绩账本 + 呈现权重系统性取代）；
* `exp_patterns` 建表（由 claims 聚合产出）；
* D 的裸 messages 恢复；
* 执行型技能的方法论正文注入（已是分发卡片）；
* 为回测自建执行器 / 判分框架（§9 裁决：全走 mimoagent batch）。

### 17.3 新版本必须新做的三件（旧功能里没有的）

1. **七窗口与口径契约**（§4）——旧系统是单点 T+N，装不下分窗口陆续到期；
2. **回测外挂层 + 案例工厂 + 测量场**（§8）——旧系统的回测只是工具，不是经验来源与预测力测量场；
3. **因股制宜**（§16）——旧系统全局一套参数、一条校准曲线，异质性被当噪声抹掉。

---

## 18. 工作流程（日常怎么跑、改动怎么上线、出事谁兜）

> 2026-10-11 · 任务「v3 取舍分析」· OpenClaw agent。设计不只讲结构，还要讲节奏——本节是运行节奏、改动节奏与故障处置的三张表。

### 18.1 日常运行节奏

**每个交易日**

| 时段 | 谁触发 | 做什么 |
|---|---|---|
| 盘前 | cron | 情境特征预生成（L0/L1/L2 快照：大盘状态 / 情绪温度 / 题材阶段）；经验检索预热（省首问延迟） |
| 盘中 | 用户问答 | 在线主链：检索 → digest → 判断 → **采集案例**（写 `exp_cases`，outcome 待回填）；重活走 `run_skill`；成本吃 `utils/budget` 预算 |
| 盘后 15:30 | 盘后 worker（现有 `start_eval_worker`） | ① 到期窗口回填 `exp_outcomes`（逐窗口）② 战绩账本更新（n_eff / Wilson CI / 收缩 / 状态机）③ 旧链路 `evaluate_pending` + `weight_feed`（保留） |
| 盘后收尾 | cron | 计数器面板落盘（`traces/panel.jsonl`）、五指纹快照 |

**每周**

* `pattern_miner` 候选生成（含 §16 的异质性 interaction 候选）→ 一律只进「待验证」；
* **测量场周跑**：同窗口对照（量化基线 vs 经验加持）→ 有效性指纹（ECE + 增量）；
* `preselect_cooc --db` 周级跑批（工具预选权重）；
* `weekly_panel.py` 周报（预算消耗 / 校验环计数 / 指纹趋势）。

**每月**

* 校准刷新 `exp_calibration`（claim × window × cohort，n_eff ≥ 30 才动）；
* 升格 / 退役批处理（out-of-time + FDR）；
* 归因复盘：`exp_claim_citations` 贡献度 Top / Bottom——**谁在帮忙、谁在帮倒忙**；
* 人工复核抽样（ECE 超阈、undecidable 率异常自动提样，见 §14.5）。

### 18.2 单次判断的数据流

见 §2.1【采集】【生效】与 §7（写入与回填），不重复。

### 18.3 提智改动的开发-验证-上线闭环（改动节奏）

```
想法（经验规则 / 金例 / prompt / 技能 / 判定口径的任何改动）
  → 开发（纯函数优先；判定口径只写在 chain/label_spec.py）
  → 回归：smoke 248 项 + qd_prompt_eval（不得回退；新组件必须补 smoke 断言）
  → 测量：测量场 A/B 或 mimoagent batch A/B —— 拿到「条件命中率 +几分」的实测 diff
  → 上线：只上 diff 显著为正的；上线留快照
  → 观测：一周内盯五指纹（ECE / 出声率 / 反向证据率 / 可证伪率 / undecidable 占比）
  → 异常 → 回滚（smoke 防回退断言帮助定位）
```

**红线**：没有 A/B diff 的改动不上线（宁可不上）；上线一周内指纹异常即回滚。

### 18.4 协同与产物纪律（沿用项目约定）

* 多人共享区（MEMORY.md / docs / tmp / 日志）**只增量追加**、产出署名（任务名 / 署名 / 时间）；**MEMORY.md 禁止追加或覆盖**；
* 删除 = `shutil.move` 到项目根 `del/`，**不硬删**；临时产物 / 日志放 `tmp/`；
* **禁止一切 git 指令**（本地即运行模式，提交由用户手工完成）；
* 每个文件头部注释写清职责与易错点；规则不在代码里（外置到 yaml / 规格书）。

### 18.5 故障与降级（谁发现、谁触发、怎么兜）

| 信号 | 谁发现 | 动作 |
|---|---|---|
| 预算超限 | `utils/budget`（接线后） | 软收尾：交出已完成产物 + 注明未完成部分，不 raise |
| ECE 超阈 / undecidable 率异常 | 测量场周报 | 自动提样人工复核（§14.5）；复核前该批经验不升格 |
| 指纹异常（出声率↓ / 反向证据率↓） | 周面板 | 告警 + **暂停升格**（采集不停，先看清楚） |
| 经验库 / 回测外挂 / mimoagent 挂了 | fail-open + warning | 降级为「无经验模式」继续问答；主链绝不因增益层中断 |
| 口径版本被误改 | smoke 回归用例 | 拒绝写入（版本只增不改），落 warning |

---

## 19. 评审修订记录（v3.1，2026-10-11）

> 任务「v3 复核」：对上一轮提出的改进逐条回代码核实，**只落"确有据"的**。以下为最终处置。

### 19.1 已修正（代码事实 / 文档自相矛盾）

| # | 位置 | 原述 | 修正 |
|---|---|---|---|
| 1 | §4.4 | `chain/evaluator.py classify_return / DIRECTION_THRESHOLD` | 实定义在 **`chain/schema.py:402/405`**（evaluator 仅 import）；改为收敛到既有源 |
| 2 | §4.3 × §5.2 | §4.3 按 `(window, due_date)` 扫描，但 DDL 无 `due_date` | DDL 补 `due_date DATE` + 索引改 `(window, due_date)`（闭合文档内矛盾，兼修性能） |
| 3 | §5.2 标题 | `agent_v6_experience.sql` 未说明编号 | 注明「现存最高 `agent_v4_trace.sql`、无 v5，顺延 v6」 |
| 4 | §13 优先级列 | `8/8`、`11/11/11`、`13/13`、`14/14` 重复，且与 §11 的 `11b/13b` 不一致 | 改为 §11 批次-序号（`11b` / `13b` / `14b`） |
| 5 | §8.3 | provider 直接"包装 `core/backtest.py`" | 加注：该文件是 **P3/P6 退役对象（删除目标）**，provider 应绑定其产出的 canonical 契约（`trade_map.build_trade`）而非文件 |

### 19.2 已补漏（口径契约，见 §4.6 及相关节）

- §4.2 第 5 条：自动化来源（backtest / miner）**主窗口先声明后见结果**（防事后择优）；
- §4.3：退市 / 长期停牌**单列 `delisted`**，不计入 `data_missing`（防幸存者偏差）；
- §4.5 条款 3/4：BH 检验**家族显式定义**；聚类作用于"正在聚合的统计"、案例内多窗口非独立；
- §4.6：特征缺失偏倚 / 冲突消解 / 特征枚举版本化 / regime 单一事实源；
- §6.1：二次排序权重**外置 + 待校准**（不写死）；
- §10：四指纹**落盘 `traces/panel.jsonl`**（禁进程内计数）；
- 附一第 7 条：内核零依赖配 **smoke 机检**。

### 19.3 未采纳（经核实后判定不必要 / 已覆盖）

| 项 | 判定 |
|---|---|
| 「回测外挂包装退役文件」整条推翻 | **部分推翻**：`core/backtest.py` 确为退役目标，但仍是现存编排且产 canonical 契约 ⇒ 改"绑定契约"而非"不接" |
| 「n_eff 漏案例内多窗口」= 硬伤 | **降级**：hit_rate 只用主窗口，不构成硬伤；仅对 `per_window` 分桶统计成立 ⇒ 已并入条款 4 措辞 |
| `utils/budget.py` 零消费方 | **文档已正确**（§17 H 已述），不重复改 |
| §13 改用"阶段-序号" | **未采纳**：沿用 §11 现有 `11b/13b` 编码即可，无需重编 |

### 19.4 v3.2 追加（2026-10-11 同日，见 §20）

在 v3.1 主干之外追加「进一步提智 + 条件化预测力」增强层：三门地基（PIT 特征层 / 残差口径 / 输出契约）
+ 四招提智 + 两招变现 + DPO（编号 18–27）。**不改变 v3.1 的任何裁定**，只在其上追加依赖层。
事实依据（均已 grep 核实）：`chain/store.py:710 get_factor_weights` 存在但 factor 层恒空；
`app/agent` 全仓无 kelly/position_size/exposure/sizing；`utils/budget.py` 零消费方；
agent 侧 `as_of` 仅 `account_store.py:156` 与 `resolver.py:65/92` 两处，无 PIT 特征基建。

### 19.5 二次评审就地合入（2026-10-12，v3.3）

> 任务「v3 合理性检查」：5 条修订意见**直接改进正文**（不设补丁附录），落点如下。

| # | 修订 | 落点 |
|---|---|---|
| 1 | 残差口径的因子模型**必须独立于 agent 侧**（禁用 `qd_agent_weights(layer='factor')` 当基准，防自我抵消）+「行业 + 市值双基准」降级路径，19 不等 14b | §20.2 |
| 2 | 输出契约分两级：最小必填集现在卡、可选增强集后置（防冷启动更冷） | §20.3 |
| 3 | 判别特征挖掘的**离线对比与 18 并行**（不碰在线采集，是最早能看到判别力的信号） | §20.4 / §20.12 |
| 4 | EV 公式改 `p̂×E[res|hit] − (1−p̂)×E[res|miss]`（期望由 per-window 收益分布给出） | §20.9 |
| 5 | 执行排序与 §11 批次对齐；DPO 可分离性验证写死判据（提升 ≥3pp 且 out-of-time 不回落） | §20.12 / §20.10 |

---

## 20. 进一步提智与条件化预测力增强（v3.2 新增）

### 20.0 判断：边际收益已从「多存经验」移到「修地基 + 改用法」

v3.1 主干（采集 → 验证 → 生效）解决的是**数据正确性**。在这条主干上继续加经验条目，边际收益递减：
一条经验的价值 ≈ `条件命中率 × 覆盖频次`，而**条件命中率的上限被三件事卡死**——
特征是否 as-of 正确、超额是否扣掉了已知因子、输出是否可判分。这三件不修，
采集越多只会「越自信地错」，且错得很稳定。

```
   ┌────────── 变现层：把预测力变成决策 ──────────┐
   │  25 校准 → 仓位映射      26 EV 排序（RankIC）│
   └───────────────────▲────────────────────────┘
                       │ 需要「已校准置信度 + 赔率」
   ┌────────── 提智层：让指标变高 ───────────────┐
   │  21 判别特征挖掘        22 证伪 gate        │
   │  23 集成 + 已校准仲裁    24 自适应算力       │
   └───────────────────▲────────────────────────┘
                       │ 需要「特征可信 + 口径可信」
   ┌────────── 地基：不做则上面全是虚高 ─────────┐
   │  18 PIT 特征层（as-of 契约）                │
   │  19 超额口径改因子残差（依赖 14b）           │
   │  20 可验证输出契约                          │
   └───────────────────▲────────────────────────┘
                       │
   v3.1 主干：采集 → 验证 → digest → 生效（本节不动它）
```

> **顺序不能反**：地基 = 让指标**可信**，提智/变现 = 让指标**变高**。
> 跳过地基做提智，等于把前视偏差与因子重复发现一起放大。

---

### 20.1 地基①：PIT 特征层（最高优先；是 §4.6 第 3 条的执行前置）

**实测**：agent 侧**没有** point-in-time 特征基建——全仓 `as_of` 仅出现在
`chain/account_store.py:156 query_due_claims(as_of=)` 与 `chain/resolver.py:65/92`（回填取数两处），
而 `situation` 的每个特征（板块联动度 / 题材阶段 / 情绪温度 / 北向 / 股性）都是**当下现算**。
于是：在线写案例取「今天知道的值」，离线回放若重算就取「那时知道的值」——两者不一致 ⇒
**条件命中率是假的**。

> 这个坑本仓已付过一次学费：`market_cn` 的市场门曾拿「收盘」门控 14:56 入场（单向前视），
> 解法是「逐槽 as-of」（`core/replay/mkt_slots.load_market_slots`）。**同一条纪律搬到 agent 侧。**

做法：

1. 每个特征过一层 **feature provider + as-of 契约**：
   `feat(feat_key, entity, as_of) -> (value, value_as_of, source)`，
   契约要求 `value_as_of <= as_of`，越界**断言失败**，不静默通过；
2. **快照落盘**：写案例时把「当时那一帧特征向量」连同 `as_of` 存进 `exp_cases.situation_snapshot`；
   检索与统计**只读快照，绝不重算**；
3. 离线回放与在线走**同一个 provider、同一份快照**，用 smoke 断言「同输入同输出」锁死；
4. 特征枚举进 `exp_feature_specs` 版本化（§4.6 第 3 条），与 `exp/windows.py` 同批落地。

### 20.2 地基②：超额口径改为「因子残差」

**实测**：`chain/store.py:710 get_factor_weights(skill_name)` 存在，但 factor 层**恒空**——
`bull_bear_research` 的五维因子（ma/macd/rsi/boll/kdj）走的是启发式常量回退分支（§14.6）。

**风险**：KPI 的「增量」现在只相对**基准指数**。基准扣掉了 beta，没扣已知因子 ⇒
「低 RSI + 放量后 T+3 上涨」这类经验很可能只是**重新推导出 RSI 因子**——
那是重复量化，不是提智：增量会长期为正却**不可变现**。

做法：

1. **因子模型必须独立于 agent 侧**（2026-10-12 修订）：因子暴露与因子收益来自**独立的量化因子模型**
   （市场 / 行业 / 风格：beta、市值、动量、波动…，时间序列回归估因子收益）。
   **禁止**用 `qd_agent_weights(layer='factor')` 当残差基准——那是 agent 从自己战绩里学出来的权重，
   用它扣残差 = 把经验的贡献当因子扣掉（自我抵消：经验做得越好、残差越小，最后「证明」经验没价值）。
   §14.6 的 agent 侧 factor 权重只作参考展示，**不进残差计算**；
2. 建 `exp/residual.py`：`residual = ret_window − (基准收益 + Σ w_i^ext · factor_i^ext)`，
   KPI 的「超额」统一换成残差 alpha；
3. **降级路径**：独立因子模型没就绪前，先用「行业 + 市值中性化双基准」做代理残差——
   **19 不等 14b**，也不同步开自研因子工程（避免把残差口径卡死在因子层灌数上）；
4. **双轨并行一个季度**：`excess_vs_benchmark` 与 `residual_alpha` 同时记账。
   残差显著小于基准超额 ⇒ 说明大部分「经验」是因子重述，
   miner 应改挖**交互项 / 非因子信息**（政策、文本、资金流），而不是继续挖价量形态。

### 20.3 地基③：可验证输出契约

把采集从「事后从答复里抽 claim」改成**前置契约**：agent 每次给方向结论，必须填满结构化字段才落 `exp_cases`：

```
{ direction: 多|空|中性,  main_window: T+N,  confidence: 0-1,
  discriminative_features: [受控枚举],  falsifier: "什么情况下我错了" }
```

收益：输出**天然可判分**（label_spec 直接吃）、**天然可对比**（金例/反例自动成对）、
`discriminative_features` 直接喂 §20.4。
**契约分两级（2026-10-12 修订，防冷启动更冷）**：

* **最小必填集**（现在就卡）：`{direction, main_window, confidence}`——缺则不落案例、不进 hit_rate，但**单独计数**；
* **可选增强集**（案例攒够再卡）：`{discriminative_features, falsifier}`——早期只统计覆盖率，不卡落库；
* 「表达退化」计数只对**最小必填集**口径统计，避免把「契约刚上线」误读成「表达退化」。
  任何一档缺失都禁止静默丢弃。

---

### 20.4 提智①：判别特征挖掘（抬条件命中率的正解）

v3 §1.4 已有雏形（"3 个亏的都是板块没跟"）。把它**产品化**：

* 在**同一情境**内取 hit 子案例 vs miss 子案例做 contrastive 统计，
  找出真正区分胜负的**判别特征**——不是「经验说了什么」，而是「什么变量把胜负分开」；
* 产物形态：L1/L2 经验条目，`condition_tpl` 带判别特征约束；
* 决策前**主动查判别特征**（而非只查经验正文）：把「条件」从表面情境推进到真正有区分度的变量，
  **这直接等价于抬条件命中率**；
* 门槛照旧：FDR + out-of-time + 只进「待验证」（§4.5 条款 3）；
* **执行位置（2026-10-12 修订）**：**离线对比与 18 并行**——载体是回测逐笔的 hit/miss contrastive，
  不碰在线采集、不依赖 PIT 特征层，是「经验到底有没有判别力」的最早信号，早出真话比顺序完备更重要；
  在线侧（决策前主动查判别特征）仍等 18 落地。

### 20.5 提智②：证伪优先（把 monitor 升级为 gate）

§10 的「反向证据率」目前只是**观测指纹**。升级为 gate：给方向结论前**必须**跑一次反事实检查——
「若我看错了，最可能是哪个特征不成立？」并调用对应工具验证（复用已有的 回避/观望 label_spec）。

* 不通过 ⇒ 降置信度或转中性，**不允许**直接给方向结论；
* 落点：`chain/label_spec.py` 旁的 `falsify_check`（纯函数 + 工具清单），结果进 digest 提示；
* **副作用必须处理**：一旦成为 gate，反向证据率会变成恒高常数而失去监测力，
  需改用**「证伪后仍维持方向结论」的比例**作为新指纹。

### 20.6 提智③：集成 + 已校准仲裁

高不确定时（digest 显示样本不足 / 经验冲突 / cohort 未校准）采 K 条推理路径，用**已校准的仲裁器**收敛：

* **不是多数投票**——仲裁器吃各路径的 `{方向, 置信, 主窗口, 判别特征}`，
  按 §10 校准后的权重加权，输出仲裁置信度；
* `--num-rollouts` 是**相关样本**：只估方差，不增加 n_eff（沿用 §12 既有红线）；
* 仲裁器本身也要进 `exp_calibration` 计分，否则又是一个拍脑袋组件。

### 20.7 提智④：自适应算力（budget 的反向用法）

§17 H 的 `utils/budget.py` 目前**零消费方**（实测）。接进 `_run_inner` 时反向用：
推理深度（是否展开 K 路、是否跑证伪、是否回测验证）**∝ 不确定度**
（digest 的 n_eff 缺口、经验冲突度、cohort 是否校准）。

* 确定情境走便宜路线（省 token），真不确定处才花钱；
* 口径：检索 / digest / 仲裁的 token **一并计入** run 预算，别只算工具调用。

---

### 20.8 变现①：校准 → 仓位映射（预测力唯一能落地的出口）

**实测**：`app/agent` 全仓**没有任何** kelly / position_size / exposure / sizing 逻辑——
校准做得再准，现在也只能输出「看多 0.65」这种**没法落地**的话。

加一层 `exposure = f(校准后置信度, 赔率, regime)`：

* 赔率来自 §4 的 per-window 收益分布（**不是**拍脑袋的止损位）；
* regime 层做总仓上限（顺/逆周期分档）；
* **先出声不上仓**：观察期内只输出 exposure 建议并记账，不接真实下单——
  与 §14.2「先观测后动作」同一姿态。

### 20.9 变现②：期望值排序（落地 §1.2 唯一可外部验证的任务）

在「掐尖集」（量化已缩圈的候选）内按 `EV = p̂ × E[res|hit] − (1−p̂) × E[res|miss]` 排序输出
（p̂ = 校准后置信度；两个期望由 `exp_outcomes` 的 per-window 收益分布直接给出，2026-10-12 修订），
正好落地 §1.2 那条可用 **RankIC 验证**的目标任务「给出未来 5 日收益率的排序」。

* 这是整套设计**唯一能被外部客观指标直接打分**的出口，建议**优先做**，
  用来对外证明「提智有效」——否则整套系统只有内部指纹，没有外部标尺。

### 20.10 高风险高回报：战绩 → 偏好优化（DPO）

复用 §9.3 已有轨迹 schema，用 `exp_outcomes` 的 hit/miss 直接打偏好标签做 **DPO**（不止 SFT）：
chosen = 命中路径，rejected = miss 路径。

* 天花板最高（§1.1 第 6 条），但要数据量与训练基建；
* **前置条件（缺一不可）**：20.1 PIT 特征（否则把前视偏差学进权重）+ 20.3 输出契约（否则标签不稳）
  + 20.2 残差口径（否则学的是因子重述）；
* **放最后**；先用小样本做可分离性验证，**判据写死（2026-10-12 修订）**：DPO 相对 SFT 基线，
  判别特征命中率提升 ≥3 个百分点且 out-of-time 不回落，才决定铺开；否则维持 SFT。

---

### 20.11 新增红线（并入 §12 风险表）

| 风险（v3.2 新增） | 对策 |
|---|---|
| **特征前视**：situation 用到当时不知道的值 | PIT 契约 + 快照落盘 + 在线/离线同值 smoke 断言（§20.1） |
| **因子重复发现**：经验只是重述已知因子，增量虚高不可变现 | 双轨记账（基准超额 vs 残差 alpha）跑满一个季度再下结论（§20.2） |
| **证伪 gate 让指纹失真**：反向证据率变恒高常数 | 改用「证伪后仍维持方向结论」的比例（§20.5） |
| **仓位放大校准误差**：分桶误差 × 仓位 = 真金白银 | 先出声不上仓；exposure 与校准分桶同步标定上限（§20.8） |
| **契约不全即丢弃 → 采集偏** | 契约不全单独计数，作为表达退化信号，禁止静默丢弃（§20.3） |
| **DPO 学进脏口径** | 三条地基不落地就不开训（§20.10） |

### 20.12 优先级与依赖

```
18 PIT 特征层 ──┬──> 21 判别特征挖掘 ──> 25 仓位映射 ──> 26 EV 排序
                └──> 22 证伪 gate ───┐
19 残差口径（依赖 14b）──────────────┼──> 23 集成 + 仲裁
20 输出契约 ────────────────────────┘        24 自适应算力
                                             27 DPO（最后，依赖 18+19+20）
```

**执行排序（2026-10-12 修订，与 §11 批次一致）**：第六批 `18 / 19 / 20`（**19 有降级路径、不等 14b**；
**21 的离线对比与 18 并行**）→ 第七批 `21 在线 / 22` → 第八批 `25 / 26` 先、`23 / 24` 后 → 第九批 `27`。

> 与 §11 前五批的关系：本节的 18–27 是**第六批起**的追加项，
> 依赖 §11 的 1–17（尤其 12 测量场、14 校准、14b 因子层）先落地；
> 不占用前五批工期，也不改变 v3.1 主干的任何裁定。

---

## 附：开发纪律（沿用本仓红线，v3 补两条）

1. **fail-open**：经验库 / 回测外挂 / 离线底座都是增益层，绝不阻断主链；但失败必须 warning 可见（本模块高发「声明了没接线」，静默=断链复发）；
2. **判定口径单一事实源**：命中判定只在 `chain/label_spec.py` 一处定义，禁止各工具自己算（同 `calibration.py` 的项目红线）；
3. **confidence 抽不到一律 None**，绝不填 0.5（旧表校准曲线退化成常数的根因）；
4. **提取器复用 `trace_collector` 口径**，不搞两套方向判定；
5. **人工反馈（human_reviewed）任何自动流程不得覆盖**（`backfill_by_root` P1 写保护同款语义）；
6. **（v3）主窗口唯一 + 口径版本锁定**：一条经验只在主窗口判命中；label_spec 版本随条目落库，改口径只增版本不回填历史；
7. **（v3）内核零依赖**：`exp_*` 只依赖接口与纯函数，绝不 import 回测实现或 mimoagent 的离线组件；**配 smoke 断言机检**（扫 `exp/` 包 import 图，出现回测实现 / mimoagent 即 FAIL），否则"零依赖"只是口头约定。

## 附二：保留的关键裁决（忘掉会返工）

* **门槛在呈现权重，不在准入**：任何经验可入库并自带战绩账本（n、置信区间、最近灵验/失效时间）；n_eff=3 与 n_eff=200 的经验同库共存，区别只在引用时的措辞与分量。
* **引擎不预设哪些经验是真的**：直觉 / 人工 / 回测挖掘 / miner 同赛道竞争，统计契约一视同仁记账，让市场投票哪些经验活下来。
* **预测不是目标**（准确说是**无条件预测不是目标**）：条件化的预测力——在有经验优势的情境里命中率、超额、校准度高于基线——才是目标；KPI 用条件命中率 + ECE + 相对量化基线的增量。
* **判分链路先冒烟再跑真题**：`--recalc-input gt` 期望 `Recalculated - Pass` 才开始正式测量（对应「label 口径前置修完再沉积」）。
