# A股 Agent 经验库体系设计 —— 从预测到经验引擎

> **日期**：2026-10-09（初版）/ 2026-10-10（v2 修订）/ 2026-10-10（v2.1 落地定制）
> **来源**：会话讨论记录整理
> **主题**：如何让 Agent 在 A 股中短线分析中建立持续进化的能力
> **状态**：v2.1 落地定制版 —— 已对照 QuantDinger `backend_api_python` 实际代码库调整：表名/模块名/钩子全部落到真实位置，组件映射见 §15
> **适用仓库**：`backend_api_python/app/agent/`（追责系统 v1.1 + 提智方案 T1 之后的存量代码基线）
>
> **v2 修订要点**：引擎不预设哪些经验是真的。任何经验（直觉/人工/挖掘）都可入库并自带战绩账本；门槛设在「呈现权重」上，不设在「准入」上。统计契约对所有经验一视同仁地记账，让市场来投票哪些经验活下来。

---

## 0. 核心结论（一句话）

> **Agent 不是预测器，不是传感器，不是校准器——而是"越来越有经验的老股民"。**
> **量化帮它缩小范围，经验帮它做判断，校准帮它知道自己几斤几两，真值帮它不断修正记忆。**

---

## 1. 问题起点：小模型 Agent 的推理能力提升

### 1.1 通用策略

| 优先级 | 策略 | 成本 | 效果 |
|--------|------|------|------|
| 1 | 结构化 Prompt + Few-Shot | 零 | 立竿见影 |
| 2 | 工具增强（计算器/检索/代码） | 工程 | 显著 |
| 3 | Self-Reflection 模式 | 近零 | 明显 |
| 4 | 上下文压缩 + 外挂记忆 | 中 | 长任务稳定性 |
| 5 | 多次采样 + 验证器 | 算力 | 用算力换质量 |
| 6 | SFT 微调推理数据 | 数据+算力 | 天花板最高 |

### 1.2 核心原则

> 用架构约束代替模型能力，用工具精确性代替模型记忆，用结构化 prompt 代替模型的自由发挥。
> 把"智能"从模型参数中解放出来，分散到系统设计的每个环节中。

---

## 2. A 股分析的准确率困境与定位转变

### 2.1 任务重定义

```
❌ "预测明天大盘涨跌"        → 本质接近抛硬币
❌ "推荐买入哪只股票"        → 输出无法验证
❌ "分析这只股票好不好"       → 主观且模糊

✅ "给出未来5日收益率的排序"  → 可用 RankIC 验证
✅ "评估该事件对板块的冲击幅度" → 可回测对比
✅ "识别当前处于什么市场状态"  → 可分类验证
✅ "给出行情信号的置信度"     → 可校准检验
```

**核心原则：让 Agent 做它擅长的"理解和提取"，让量化模型做"预测和排序"。**

### 2.2 A股数据金字塔

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

### 2.3 架构原则：量化 + LLM 混合

```
         ┌──────────────┐
         │  信号融合层   │ ← 加权/机器学习融合
         └──────┬───────┘
                │
   ┌────────────┼────────────┐
   │            │            │
量化因子模型  LLM Agent   另类数据模型
```

**不要让 LLM 直接输出买卖建议，而是输出结构化特征，交给量化模型。**

---

## 3. 方向转变：从"提升预测力"到"校准该信几分"

### 3.1 第一次转变（系统设计层面）

| | 目标 A · 校准 | 目标 B · 提升预测力 |
|---|---|---|
| 问的是 | 该不该信自己、信到几分 | 更会预测涨跌 |
| 可行性 | ✅ 可行 | ❌ 基本不可行 |

B 不可行的理由：个股日频收益的可预测成分极低，业界天花板样本外 R² 仅 1%-5%。

### 3.2 第二次转变（用户定位层面）

> **用户观点：如果目标是校准，回测就够了，Agent 不需要介入。**

**这个质疑是准确的。** 校准的本质是"什么条件下历史命中率多少"——回测引擎天然擅长。

Agent 真正不可替代的是**非结构化信息的感知与经验积累**。

### 3.3 最终定位：经验引擎

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

---

## 4. "老股民"与"分析师"的统一

两者不冲突，而是互为增强：

> 经验给假设，分析给验证。

| 能力 | "老股民" | "分析师" | 系统状态 |
|------|---------|---------|---------|
| 模式触发 | 见过类似的，警觉起来 | — | 🆕 需建 |
| 结构化分析 | — | 拆维度、逐项检查 | ✅ 已有 |
| 信息覆盖 | 凭经验知道该看哪几个面 | 确保不遗漏、不偏食 | ✅ 已有 |
| 事实锚定 | — | 数字可溯源 | ✅ 已有 |
| 自我认知 | 大概几成把握 | 精确到条件命中率 | 🔄 需校准 |
| 直觉修正 | "不对劲，先撤" | 解释哪里不对劲 | 🆕 需建 |

### 目标形态示例

```
用户："帮我看看XX科技"
  │
  ▼ 【老股民】经验检索
  → "底部放量+题材启动第二天+龙虎榜机构净买"
  → 历史相似案例 15 个，12 赚 3 亏
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
  → 留档：situational 特征 + 判断 + 置信度 → 等 T+N 回填
```

---

## 5. 经验库设计

### 5.1 核心原则

> **别存原始问答，存"投影后的案例"。**
> 写入时做投影（确定性代码做），检索时先过滤再排序，输出时压缩成摘要。

```
原始问答（噪声大，不可直接检索）
      ↓ 投影（确定性代码做，不用LLM）
结构化案例（可过滤、可检索、可统计）
```

### 5.2 多层经验体系（决策链映射）

```
┌─────────────────────────────────────────────────────┐
│ L0 市场经验：现在能不能做？该做多重？                   │
│     → 大盘状态 / 量能 / 情绪温度 / 季节性             │
├─────────────────────────────────────────────────────┤
│ L1 板块经验：该做哪个方向？                           │
│     → 轮动规律 / 联动度 / 板块生命周期               │
├─────────────────────────────────────────────────────┤
│ L2 题材经验：这个题材还能不能跟？                      │
│     → 题材阶段 / 扩散规律 / 龙头-跟风关系            │
├─────────────────────────────────────────────────────┤
│ L3 个股经验：这只票具体怎么搞？                       │
│     → 股性 / 形态规律 / 历史相似案例                 │
├─────────────────────────────────────────────────────┤
│ L4 策略经验：用什么方法切入？                         │
│     → 低吸 vs 追涨 / 什么 regime 用什么方法          │
├─────────────────────────────────────────────────────┤
│ L5 交易经验：怎么控节奏？                             │
│     → 仓位 / 买卖点 / 止损止盈 / 加减仓时机          │
└─────────────────────────────────────────────────────┘
```

**每层经验都有自己的条件-结果对，检索时按层独立查，最终由 Agent 综合。**

> **开放性原则**：情境特征空间、经验条目种类、判断类型全部开放，不设准入门槛——任何「我觉得……」都可以入库，包括直觉式的、还没想清楚的。受控枚举只用于**标签口径**（什么算命中），不用于**经验来源**（什么值得存）。

### 5.3 各层数据结构

#### 统一案例表

```sql
CREATE TABLE exp_cases (
    id              BIGSERIAL PRIMARY KEY,
    layer           VARCHAR(20) NOT NULL,    -- market/sector/theme/stock/strategy/trading
    entity_id       VARCHAR(50),             -- 板块代码/题材名/个股代码/策略名

    -- 情境层（用于过滤）
    situation       JSONB NOT NULL,          -- 各层不同的条件特征

    -- 语义层（用于相似度）
    situation_text  TEXT,                    -- 模板化生成（确定性）
    embedding       VECTOR(768),             -- BGE 向量

    -- 判断层
    judgment        JSONB NOT NULL,

    -- 结果层（T+N 回填）
    outcome         JSONB,
    verified_at     DATE,

    created_at      TIMESTAMP DEFAULT now(),
    source          VARCHAR(20)              -- agent/auto/manual
);

CREATE INDEX ON exp_cases (layer, created_at DESC);
CREATE INDEX ON exp_cases (layer, entity_id) WHERE entity_id IS NOT NULL;
-- 向量存储沿用本仓真实约束（见 case_memory.py / rag/pg_vector_store.py）：
-- pgvector 扩展不可依赖，embedding 存 JSONB + Python 余弦，限扫限批量；
-- 无 embedding 配置时退化为 2-gram 词面相似（fail-open，通道不断）。
-- 不建 ivfflat 索引（冷启动数据量下召回极差，且扩展不可用）。
```

> **v2.1 存储裁决**：向量方案照抄 `rag/pg_vector_store.py` 同款形态（JSONB 存向量 + Python 余弦），embedding 走 `rag/embeddings.py` 工厂（env EMBEDDING_*）。表结构落 `migrations/agent_v5_experience.sql`，风格对齐 `migrations/agent_v4_trace.sql`（CREATE TABLE IF NOT EXISTS，可重复执行）。

#### L0 市场经验

```python
{
    "layer": "market",
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
    "judgment": {
        "action": "进攻/均衡/防守/空仓",
        "position_range": [0.3, 0.7],
        "confidence": 0.75
    }
}
```

> 示例经验："两市缩量到 7000 亿以下 + 涨停数 < 40 + 北向流出 → 三天内出大阳线概率 25%，仓位建议 ≤ 3 成"

#### L1 板块经验

```python
{
    "layer": "sector",
    "situation": {
        "sector": "半导体",
        "sector_rotation_day": 3,
        "sector_linkage": 0.65,
        "sector_volume_ratio": 1.8,
        "sector_vs_market": "领涨/跟涨/领跌/抗跌",
        "leader_status": "龙头涨停/龙头炸板/龙头调整",
        "breadth": 0.72,
        "policy_support": true
    }
}
```

> 示例经验："板块轮动第 3 天 + 龙头炸板 + 跟风股大面积冲高回落 → 第 4 天补跌概率 70%"

#### L2 题材经验

```python
{
    "layer": "theme",
    "situation": {
        "theme": "AI应用",
        "theme_stage": 2,           # 1启动 2发酵 3高潮 4分歧 5退潮
        "days_since_start": 4,
        "leader_gain": 0.35,
        "spread_to_followers": true,
        "news_catalyst": "政策",
        "market_attention": 0.8
    }
}
```

> 示例经验："题材第 4 天以上 + 龙头涨幅 >30% + 全市场都在讨论 → 退潮概率 75%"

#### L3 个股经验

```python
{
    "layer": "stock",
    "situation": {
        "symbol": "600XXX",
        "sector": "半导体",
        "theme": "AI应用",
        "regime": "震荡",
        "stock_pattern": "底部放量突破",
        "stock_personality": {
            "volatility_rank": 0.8,
            "sector_follow": 0.9,
            "news_sensitivity": 0.6,
            "limit_up_momentum": 0.7,
            "liquidity_score": 0.65,
            "rebound_habit": "60日线",
            "auction_pattern": "高开低走",
            "turnover_cycle": "3天放量一次"
        },
        "special_signals": ["龙虎榜机构净买", "底部缩量企稳"]
    }
}
```

> 示例经验："这只票涨停后次日溢价均值 +2.1%，炸板后次日平均 -3.2%"

#### L4 策略经验

```python
{
    "layer": "strategy",
    "situation": {
        "regime": "震荡",
        "theme_stage": 2,
        "stock_position": "底部启动",
        "market_sentiment": "中性"
    },
    "judgment": {
        "best_approach": "低吸",
        "entry_timing": "尾盘或次日竞价低开",
        "stop_loss": "-3%",
        "take_profit": "+6%",
        "hold_days": 3
    }
}
```

> 示例经验："震荡市 + 题材中期 + 底部启动形态 → 低吸胜率 62%，追涨胜率 38%"

#### L5 交易经验

```python
{
    "layer": "trading",
    "situation": {
        "portfolio_state": "首次建仓/已有浮盈/已有浮亏",
        "market_regime": "震荡",
        "consecutive_wins": 3,
        "drawdown_current": -0.02
    },
    "judgment": {
        "position_size": 0.3,
        "add_or_reduce": "减仓",
        "stop_rule": "跌破5日线无条件走",
        "max_hold_days": 5
    }
}
```

> 示例经验："连续 3 笔盈利后 → 历史统计上第 4 笔亏损率反而升高到 55%"

### 5.4 经验模式表（从案例提炼）

```sql
CREATE TABLE exp_patterns (
    id                  BIGSERIAL PRIMARY KEY,
    layer               VARCHAR(20),
    conditions          JSONB,           -- 条件组合
    sample_count        INT,
    hit_rate            FLOAT,
    avg_return          FLOAT,
    avg_excess          FLOAT,
    max_drawdown        FLOAT,
    win_loss_ratio      FLOAT,
    winner_traits       TEXT[],          -- 赚钱案例共同特征
    loser_traits        TEXT[],          -- 亏钱案例共同特征
    key_differentiator  TEXT,            -- 区分胜负的关键变量
    regime_valid        VARCHAR(20)[],
    decay_factor        FLOAT DEFAULT 1.0,
    updated_at          TIMESTAMP
);
```

### 5.5 置信度校准表

```sql
CREATE TABLE exp_calibration (
    claim_id        BIGINT,              -- v2：校准对象下沉到 claim 级
    pattern_type    VARCHAR(50),
    direction       VARCHAR(10),
    claimed_conf    FLOAT,
    actual_hit_rate FLOAT,
    sample_count    INT,                 -- 必须是有效样本数（聚类折算后）
    calibrated_conf FLOAT,
    updated_at      TIMESTAMP
);
```

> **v2 修正**：校准对象是 `exp_claims` 中 status=有效 的条目（claim 级），而非宽泛的 pattern_type 桶。每个校准桶同样受最小样本约束（n_eff < 30 不校准），否则校准本身在放大噪声。

#### 5.6 经验条目账本（exp_claims）—— 任何经验都可入库，战绩随行

> **核心姿态：引擎不预设哪些经验是真的。** 直觉经验、人工录入、回测挖掘、pattern_miner 产物走同一条赛道，公平竞争，凭战绩说话。

```sql
CREATE TABLE exp_claims (
    id            BIGSERIAL PRIMARY KEY,
    claim_text    TEXT NOT NULL,          -- 经验陈述："缩量7000亿以下+涨停数<40 → 三天内大阳线概率低"
    layer         VARCHAR(20),
    condition_tpl JSONB,                  -- 条件模板（自由扩展，不设受控枚举）
    prediction    JSONB,                  -- 预言了什么 + 观察窗口 + 命中判定式
    label_spec_id VARCHAR(50),            -- 引用标签口径规格书中的判定式（写入时声明，禁止事后挑口径）
    source        VARCHAR(20),            -- agent直觉 / manual / miner / backtest
    n_effective   INT DEFAULT 0,          -- 按 date×theme 聚类折算后的有效样本
    hit_rate      FLOAT,                  -- 原始命中率（仅供内部计算）
    ci_low        FLOAT,                  -- Wilson 95% CI 下界
    ci_high       FLOAT,                  -- Wilson 95% CI 上界
    shrunk_rate   FLOAT,                  -- beta-binomial 向基线收缩后估计（对外输出用这个）
    last_hit_at   DATE,
    last_miss_at  DATE,
    status        VARCHAR(20) DEFAULT '待验证',  -- 待验证 / 有效 / 退化中 / 已退役
    regime_valid  VARCHAR(20)[],
    created_at    TIMESTAMP DEFAULT now(),
    updated_at    TIMESTAMP
);

CREATE INDEX ON exp_claims (layer, status, n_effective DESC);
CREATE INDEX ON exp_claims USING gin (condition_tpl);
```

#### 5.7 生命周期与呈现权重（门槛在权重，不在准入）

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
| 有效 | 正常 | "n_eff=120，命中 62%（95%CI 53–71%）" | 按收缩后命中率计入 |
| 退化中 | 降权 | "⚠️ 近期失效中" | 减半计入 |
| 已退役 | 不引用 | 不出现 | 0 |

> 一条 n_eff=3 的新经验和一条 n_eff=200 的老经验**同时存在于库中**，区别只在引用时的措辞和分量。这正是"老股民"的真实状态：脑子里既有琢磨了十年的规律，也有最近才有的感觉，两者都有价值，但分量不同。

#### 5.8 统计契约（记账规则，对所有经验一视同仁）

| # | 条款 |
|---|---|
| 1 | 任何命中率输出必须带 n、Wilson 95% CI、beta-binomial 收缩后估计值 |
| 2 | 有效样本（聚类折算后）< 30 不输出命中率，只输出"样本不足，仅作先验" |
| 3 | 模式升格（待验证→有效）需 out-of-time 验证 + FDR 控制（miner 产物适用） |
| 4 | 样本量按 date×theme 聚类折算（cluster-robust / block bootstrap），禁止名义计数 |
| 5 | 每类 judgment 写入时声明 label_spec（命中判定式 + 观察窗口 + undecidable 标记），禁止事后挑口径 |
| 6 | digest 统计行强制标注"样本充足/不足" |
| 7 | 记录未出手与失败案例，undecidable 同样入库 |
| 8 | 有效性指纹常开：滚动 out-of-time ECE + 相对量化基线的增量 |

**标签口径规格书（label_spec，与统计契约同批落地）**：

| judgment 类型 | outcome 字段 | 观察窗口 | 命中判定式 |
|---|---|---|---|
| 方向（偏多/偏空） | ret, excess_ret | T+5 / T+10 / T+20（写入时声明） | 超额收益符号与判断一致 |
| 仓位区间 | max_drawdown | 持有期 | 不判对错，只统计后续回撤是否超阈值 |
| 打法（低吸/追涨） | ret_of_approach | 声明窗口 | 该打法相对另一打法的超额 |
| 止损/止盈规则 | execution_outcome | 持有期 | 触发规则后的实际滑点 vs 替代路径 |
| 回避/观望 | counterfactual_ret | 同窗口 | 被回避标的同窗口收益 < 基准（回避正确） |

---

## 6. 检索流程：两阶段混合检索

```
新任务
  │
  ▼ 【Step 1: 特征提取（确定性代码，0ms）】
  提取当前情境 → sector/regime/stock_pattern/theme_stage/...
  │
  ▼ 【Step 2: 结构化硬过滤（SQL，~5ms）】
  WHERE layer IN (...) AND regime=... AND sector IN (...)
    AND outcome_verified_at IS NOT NULL
  → 候选池 200-500 条
  │
  ▼ 【Step 3: 向量语义排序（本仓向量库，限扫 2000）】
  situation_embedding ~ query_embedding（JSONB + Python 余弦，同 case_memory.retrieve_cases）
  → 取 Top-20（无 embedding 配置时退化为词面相似，fail-open）
  │
  ▼ 【Step 4: 二次排序（代码，~1ms）】
  score = similarity × 0.4
        + recency_decay × 0.2
        + hit_rate_weight × 0.2
        + regime_match × 0.2
  → 最终 Top-8~12 条
```

### 跨层组合检索（Agent 分析一只票时按决策链逐层查）

```
用户："帮我看看XX科技"
  │
  ▼ 【L0 市场】"震荡市+缩量+情绪低迷 → 仓位 ≤5成，低吸为主"
  ▼ 【L1 板块】"板块轮动第3天，联动度0.55 → 仍有空间但不追高"
  ▼ 【L2 题材】"题材第2天，龙头首板，扩散初期 → 可跟"
  ▼ 【L3 个股】"底部放量突破，回调习惯位60日线"
  ▼ 【L4 策略】"震荡+题材早中期+底部启动 → 低吸胜率62%"
  ▼ 【L5 交易】"首次建仓3成，止损-3%，持有3-5天"
  │
  ▼ 【合成判断】
  → 方向：偏多  置信度：0.62
  → 策略：低吸  仓位：3成
  → 风控：-3%止损，T+5强制离场
  → 依据：L0保守+L1中性+L2积极+L3有利+L4胜率62%
```

---

## 7. 经验摘要压缩（Experience Digest）

**检索到 N 条案例后，不把原文丢给 Agent，而是压缩成统计摘要（~500字符）：**

```python
def build_experience_digest(query_situation, cases, claims):
    # 主力：带战绩账本的逐条经验引用（不做归纳式共性总结）
    # 辅助：案例统计摘要（带区间与样本标注）
    winners = [c for c in cases if c.outcome_hit]
    n_eff = effective_sample_size(cases)        # date×theme 聚类折算，非名义计数
    rate, lo, hi = wilson_ci(len(winners), n_eff)
    rate = shrink_to_base(rate, n_eff)          # beta-binomial 向基线收缩

    stat_line = (
        f"命中 {len(winners)}/{len(cases)}（有效样本 n_eff={n_eff}）→ {rate:.0%}（95%CI {lo:.0%}–{hi:.0%}）"
        if n_eff >= 30
        else f"样本不足（n_eff={n_eff}），统计仅作先验提示"
    )

    return f"""
【历史经验摘要】
📌 相关经验（按战绩权重）：
{format_claims(claims)}
📊 案例统计：{stat_line}，平均超额 {mean(c.outcome_excess):+.1f}%
⚠️ 当前情境与历史差异：{diff_from_history(query_situation, cases)}
"""
```

**输出示例**：

```
【历史经验摘要】
📌 相关经验（按战绩权重）：
1. 「题材第4天+龙头涨幅>30%+全市场讨论 → 退潮概率高」
   n_eff=87，命中 71%（95%CI 60–80%），status=有效，最近验证 2026-09
2. 「板块联动度<0.4 时跟风股补跌」
   n_eff=24，命中 63%（样本不足 ⚠️），status=待验证
3. 「连续3笔盈利后第4笔谨慎」
   n_eff=12，命中 50%，status=退化中 ⚠️ 近期已不灵

📊 案例统计：命中 8/12（有效样本 n_eff=7）→ 样本不足，仅作先验
⚠️ 当前情境与历史差异：当前板块联动度 0.55，历史同区间案例集中在 2025Q4，regime 可比性存疑
```

**呈现原则**：

- **逐条经验的战绩引用是主力**，案例共性归纳降级为可选观察（`common_traits` 输出标注"提示性观察，非统计结论"）——引用有账本的旧判断，比对 8 个赢家做过拟合式归纳可靠得多；
- 统计数字一律带区间和样本标注，宁可显示"样本不足"，不显示虚假精确的 67%；
- 500 字符 = 若干条带战绩的经验 + 统计摘要 + 差异提示。

---

## 8. 写入流程：案例怎么产生

```
Agent 完成一次分析
  │
  ▼
trace_collector finalize 后置钩子提取案例（代码做，不用LLM；
同 case_memory.record_case 挂点，改造为经验库写入端）
  ├─ situation 特征 ← resolver + 工具输出 + 量化指标
  ├─ situation_text ← 模板化生成（确定性）
  ├─ embedding ← rag/embeddings.py 工厂
  ├─ judgment ← Agent 的结论（写入时必须声明 label_spec：命中判定式+观察窗口）
  └─ outcome ← 暂空，T+N 回填
  │
  ▼
写入 exp_cases + exp_claims 关联（该判断引用/印证/反驳了哪条经验）
  │
  ▼
（T+N 后）chain/resolver + evaluator.evaluate_pending 回填 outcome
（未出手/undecidable 同样回填；沿用 backfill_by_root 的 P1 写保护语义——
只在 label 仍为 pending 时回填，人工反馈 human_reviewed 不得覆盖）
  │
  ▼
定期任务：更新 exp_claims 战绩账本 → exp_calibration 校准
         （pattern_miner 提炼 → 仅进 exp_claims"待验证"赛道）
```

**situation_text 必须模板化生成**：

```python
def build_situation_text(case):
    return (
        f"{case.regime}市|{case.sector}板块|"
        f"个股形态:{case.stock_pattern}|"
        f"题材阶段:{case.theme_stage}期|"
        f"信号:{','.join(case.special_signals)}|"
        f"板块联动度:{case.sector_linkage:.2f}"
    )
```

---

## 9. 校准闭环：从经验中提高命中率

```
         ┌──────────────────────────────┐
         │                              │
         ▼                              │
   经验检索 → 判断 → 校准置信度 → 行动   │
                │                       │
                ▼                       │
           T+N 真值回填                 │
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

**置信度校准示例**：

| 经验说 | 历史实际命中 | 校准后输出 |
|--------|------------|-----------|
| "看多，confidence 0.8" | 该模式命中率 65% | 修正为 0.65 |
| "看空，confidence 0.6" | 该模式命中率 72% | 修正为 0.72 |

### 必须先建的五个统计指纹（防退化 + 防"自信地错"）

| 指标 | 定义 | 异常信号 |
|------|------|---------|
| 可证伪率 | claims 数 / 有实质结论的答复数 | 下降 = 在变滑头 |
| undecidable 占比 | 判定为不可判定的比例 | 上升 = 在变滑头 |
| 出声率 | 给出明确结论的答复占比 | 下降 = 保守化退化 |
| 反向证据率 | 给出方向结论时调用反向工具的比例 | 下降 = cherry-picking |
| **有效性指纹** | 滚动 out-of-time 命中率 vs 报告置信度的 ECE + 相对量化基线的增量 | ECE 上升或增量归零 = 统计在失真（前四个全绿也可能在自信地错） |

> **v2.1 落点**：前四个指纹扩展 `chain/judge_stats.py` 的计数器形态（同款"先证明有必要再谈校准"的观测姿态，只观测不改判定行为）；有效性指纹由 `exp_calibration` 产出，滚动窗口计算，同存 `qd_agent_weights` 新 layer 或 exp 侧新表。

---

## 10. 各层经验的沉淀速度与冷启动

| 层级 | 积累速度 | 冷启动方案 | 需要样本 |
|------|---------|-----------|---------|
| L0 市场 | 慢 | 手工录入历史大事件 | 50-100 条 |
| L1 板块 | 中 | 回测引擎批量生成 | 200-500 条/板块 |
| L2 题材 | 中 | 历史题材复盘 | 100-300 条/题材 |
| L3 个股 | 快 | Agent 运行自动积累 | 1000+ 开始有效 |
| L4 策略 | 慢 | 回测引擎 AB 对比 | 200-500 条 |
| L5 交易 | 中 | 手工 + 交易记录 | 300+ 条 |

> **注**："需要样本"一列均指**有效样本**（date×theme 聚类折算后）。名义样本需按 3–10 倍预估——同题材同天的 20 条案例折算后可能只有 1–3 个独立观察。

**L4 策略经验天然适合回测引擎生成**——同一情境下"低吸 vs 追涨"的胜率对比，回测跑一遍就有。这是回测引擎和经验库的最佳衔接点。

---

## 11. 与现有系统的接线

### 保留不动（真实组件）

| 组件 | 作用 |
|------|------|
| `tools/tool_preselect.py` + `tool_discovery.py`（102 工具分级下发） | 信息覆盖 |
| `utils/grounding.py`（grounding gate） | 数字可溯源 |
| `qd_agent_resolutions`（`chain/resolver.py` 阶段A代码算数 + 阶段B judge） | T+N 真值判定 |
| 追责四表：`qd_agent_decisions / qd_agent_claims / qd_agent_resolutions / qd_domain_resolvers` | 判定留档 |
| `tools/finance/backtest_tools.py` 回测引擎 | 掐尖筛选 + L4 策略经验生成 |
| `chain/evaluator.py` 的 `return_per_day` 口径 | 核心指标已是期望收益率而非胜率，与 label_spec 同向，不改 |

### 需要改造（真实落点）

| 改动 | 说明 |
|------|------|
| `qd_agent_decisions` 加 `symbol/theme/regime` 三列（DDL 落 `migrations/agent_v5_experience.sql`；`chain/account_store.save_decision` 增参透传） | 数据模型地基，必须现在做。反面教材就在本仓：`chain/weight_feed.py` 的 P0-1 教训——decision 只带 domain/intent 时最细只能诚实做到 domain 粒度，硬反推就是伪精确 |
| `utils/case_memory.py` 接线改造为经验库写入端 | 现状：`qd_agent_cases` 已有 record/retrieve/backfill + label 权重（incorrect 硬排除、pending 0.5×）——**这正是 §5.7 呈现权重的雏形**，已实现一半；改造点：挂 situation 上下文、写 exp_claims 关联、四态 status（待验证/有效/退化中/已退役）接上现有 label |
| `chain/claims.py` 提取增加 situation 上下文 | 记录当前市场状态/题材阶段；两条红线保持：confidence 抽不到一律 None（绝不填 0.5）、提取器复用 trace_collector 口径不搞两套 |
| 校准职责分层 | 本仓已有三层校准，必须分清不混：`utils/calibration.py`（isotonic score→hit_rate，桶级）、`app/services/ai_calibration.py`（market 级买卖阈值）、`chain/weight_feed.py`（domain×EMA 慢调）。exp_calibration 是第四层（claim 级战绩校准），不替代前三层，但共用 qd_agent_weights 存储时用独立 layer 隔离 |
| 统计契约接线到存量代码 | `utils/calibration.py` 的 `_MIN_SAMPLES=10` 偏低 → 收紧到契约口径（n_eff≥30 才出命中率，输出带 CI + 收缩）；`backfill_by_root` 的 P1 写保护语义（human_reviewed 不覆盖）直接复用为契约条款 7 的实现基础 |

### 需要新建

| 组件 | 作用 |
|------|------|
| `experience_search()` | 两阶段混合检索 |
| `build_digest()` | 经验摘要压缩（claim 引用为主版） |
| `exp_claims` 账本 + 生命周期状态机 | 任何经验可入库、凭战绩升格/退役 |
| `label_spec` 标签口径规格书 | 每类 judgment 的命中判定式 + 观察窗口 |
| `effective_sample_size()` | date×theme 聚类折算有效样本 |
| `pattern_miner()` | 定期提炼模式——**仅产出"待验证"条目，升格需 out-of-time + FDR** |
| `situation_template()` | 情境特征标准化模板（特征空间开放） |
| `backfill_outcome()` | T+N 结果回填（含未出手/undecidable） |

---

## 12. 实施优先级

```
立即可做（1-2周）：
├── ① qd_agent_decisions 加 symbol/theme/regime（migrations/agent_v5_experience.sql
│      + chain/account_store.py save_decision 透传）
├── ② 修标签口径三条缺陷（涨跌停/交易成本/相对基准；
│      与 chain/evaluator.py classify_return / DIRECTION_THRESHOLD 同口径）
├── ③ 五个统计指纹计数器（chain/judge_stats.py 形态扩展 + exp 侧有效性指纹）
└── ④ 统计契约 + label_spec 规格书 + exp_claims 建表
    ⚠️ 与②同批落地；存量 utils/calibration.py 的 _MIN_SAMPLES 同步收紧

短期见效（2-4周）：
├── ⑤ utils/case_memory.py 改造为经验库写入端（含 exp_claims 写入与关联，
│      沿用 fail-open + P1 写保护）
├── ⑥ tools/finance/backtest_tools.py 按 regime × 板块输出统计（冷启动 L1/L4）
├── ⑦ situation_template 模板化生成（特征空间开放，不设受控枚举）
└── ⑧ experience_search 两阶段检索（向量层复用 rag/pg_vector_store.py 形态）

中期建设（1-3月）：
├── ⑨ build_digest 经验摘要压缩（claim 引用为主版）
├── ⑩ exp_calibration 置信度校准（claim 级，受最小样本约束，独立 layer 隔离）
└── ⑪ 归因标注但不动作（攒一个月定枚举；参考 judge_stats.py 的"先观测后校准"）
    ⚠️ pattern_miner 依赖④统计契约：契约未落地前不启动；
       启动后产物只进"待验证"赛道，不得直接升格为有效经验

长期演进（3-6月）：
├── ⑫ 跨 regime 样本积累后开个股级经验
├── ⑬ L2 题材生命周期经验库
├── ⑭ 策略经验（L4）回测 AB 对比自动化
└── ⑮ pattern_miner 周期提炼 + FDR / out-of-time 升格流水线
```

---

## 13. 风险与注意事项

| 风险 | 对策 |
|------|------|
| **沉积是放大器不是纠偏器**：采集有偏则固化偏差 | 先建统计指纹监测，再谈沉积 |
| **X 空间不稳定**：问法/路径/结论每次不同 | 投影层做确定性映射（代码做，不用LLM） |
| **有效样本 ≠ 名义样本**：同题材同天高度相关 | 按题材×日期聚类折算，分 regime 分桶 |
| **保守化退化**：越校准越不敢说话 | 监控出声率，设下限告警 |
| **归因不能信**：LLM 几乎总能给出合理解释 | 归因落在受控枚举 + 客观锚点；digest 中共性归纳降级为"提示性观察" |
| **label 口径缺陷**：不可成交/未扣成本/无基准 | 前置修完再开始沉积 |
| **多重比较/模式过拟合**：在海量条件组合里挖掘，纯运气也能挖出"胜率80%"模式 | FDR 控制 + out-of-time 升格 + miner 产物只进"待验证"赛道 |
| **自反馈闭环**：经验影响判断 → 判断产生新案例 → 反过来强化旧经验 | out-of-time 验证 + 记录未出手样本 + 漏斗版本号 |
| **经验过期（对手盘进化）**：跟庄痕迹被反向利用，A 股经验保质期短 | decay + regime 分桶 + 近期样本加权 + "退化中"状态机真执行 |

---

## 14. 总结

> **Agent 在 A 股中短线的定位是"越来越有经验的老股民 + 有纪律的分析师"。**
>
> - **量化**做筛选，从全市场掐出 50-100 只候选
> - **经验库**做判断，按决策链六层（市场→板块→题材→个股→策略→交易）逐层检索相似案例
> - **校准**做修正，知道每类判断历史上命中率多少、该信几分
> - **真值**做沉淀，T+N 回填结果，提炼模式，持续进化
>
> **预测不是目标，经验积累才是。通过校准从经验中分析和提高未来的命中率，在量化和纯主观之间找到属于自己的位置。**
>
> **引擎不预设哪些经验是真的**：任何经验都可入库并自带战绩账本（n、置信区间、最近灵验/失效时间），门槛设在呈现权重而非准入。直觉与挖掘同赛道竞争，统计契约对所有经验一视同仁地记账，让市场来投票哪些经验活下来。

---

## 15. 落地映射总表（本仓代码索引）

> 本节是 v2.1 针对 QuantDinger `backend_api_python` 的定制内容：设计组件 → 真实代码位置 → 动作。

| 设计组件 | 真实代码位置 | 动作 | 优先级 |
|---|---|---|---|
| 经验案例表 exp_cases | 新表，`migrations/agent_v5_experience.sql`（对齐 agent_v4_trace.sql 幂等风格） | 新建（向量存储照抄 `rag/pg_vector_store.py` JSONB+Python 余弦，不依赖 pgvector） | P0-④ |
| 经验条目账本 exp_claims | 同上，新表 | 新建 | P0-④ |
| label_spec 规格书 | 新 `app/agent/chain/label_spec.py`（纯函数，对齐 claims.py 不碰 DB 的分层） | 新建 | P0-④ |
| situation 上下文写入 | `chain/account_store.save_decision` + `chain/claims.py` | 改造（decisions 加 symbol/theme/regime 三列） | P0-① |
| 案例写入端 | `utils/case_memory.py`（record_case 钩子） | 改造（挂 situation + exp_claims 关联 + 四态 status） | P1-⑤ |
| T+N outcome 回填 | `chain/resolver.py` + `chain/evaluator.py`（backfill_by_root 写保护复用） | 改造（含未出手/undecidable 回填） | P1-⑤ |
| 两阶段检索 experience_search | 新 `app/agent/utils/experience_search.py` | 新建（硬过滤 SQL + 余弦排序 + 二次排序） | P1-⑧ |
| 摘要压缩 build_digest | 新 `app/agent/utils/experience_digest.py` | 新建（claim 引用为主版） | P2-⑨ |
| 置信度校准 exp_calibration | `qd_agent_weights` 新 layer 或 exp 侧新表；拟合逻辑参照 `utils/calibration.py` | 新建（claim 级，独立 layer 与 domain/calibration 隔离） | P2-⑩ |
| 统计指纹 | `chain/judge_stats.py` 形态扩展（只观测不改行为） | 新建/扩展 | P0-③ |
| 有效样本折算 effective_sample_size | 新 `app/agent/utils/exp_stats.py`（Wilson CI / beta-binomial 收缩 / 聚类折算） | 新建 | P0-④ |
| 模式挖掘 pattern_miner | 新 `app/agent/cron/` 定时任务 | 新建（受统计契约门控） | P3-⑮ |
| L4 策略经验冷启动 | `tools/finance/backtest_tools.py` | 改造（regime × 板块统计输出） | P1-⑥ |

**开发纪律（沿用本仓既有红线）**：

- fail-open：经验库是增益层，绝不阻断规划/收尾主链，但失败必须 warning 可见（本模块高发"声明了没接线"，静默=断链复发）；
- 一切判定口径单一事实源：命中判定只在 label_spec 一处定义，禁止各工具自己算（同 calibration.py 的项目红线）；
- confidence 抽不到一律 None，绝不填 0.5（旧表校准曲线退化成常数的根因）；
- 提取器复用 `trace_collector` 口径，不搞两套方向判定；
- 人工反馈（human_reviewed）任何自动流程不得覆盖（backfill_by_root P1 写保护同款语义）。
