# Agent 三层架构重构方案

> 日期: 2026-06-08
> 状态: P0 完成 (schema/skill/evaluator 已实现)
> 原则: 不考虑旧版兼容，数据库重新设计

## 一、架构总览

```
甲方(用户消息)
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Chain 层 (编排/决策) — "大领导/CEO"              │
│  职责: 理解意图, 组装链路, 汇总评分, 输出决策      │
└──────────┬──────────┬──────────┬────────────────┘
           │          │          │
    ┌──────▼──┐ ┌─────▼────┐ ┌──▼──────────┐
    │ Skill A │ │ Skill B  │ │ Skill C ... │ ← 部门领导
    │ (技术面) │ │ (资金面) │ │ (情报面)    │
    └────┬────┘ └────┬─────┘ └────┬────────┘
         │           │            │
    ┌────▼────┐ ┌────▼────┐ ┌────▼────┐
    │ Tools   │ │ Tools   │ │ Tools   │ ← 干活的
    │ (取数据) │ │ (算指标) │ │ (查新闻) │
    └─────────┘ └─────────┘ └─────────┘
```

### 正向流: 用户 → Chain → Skill → Tools
- 一个 Chain 由 1~N 个 Skill 组成
- 一个 Skill 由 1~N 个 Tool 组成
- 允许向下越级（Chain 直接调 Tool）
- **禁止**下级调上级、平级相互调用

### 反向流: 结果 → Chain → Skill → Tools (回溯)
- 结果出来后，逐层检查每人的报告是否准确
- 只评估**准确率**，不评估快慢/功能多少
- 每层只对自己交的报告负责
- 缺失的下层如实报告，不替它背锅

## 二、三层定义

### 2.1 Tools 层 — "干活的"

**职责**: 取数据、算指标、跑回测。**搬运工，不做校验。**

**现实**: Tools 有多个数据源降级链路（腾讯→东财→新浪→AkShare），每个源的数据可能不一样。Tool 自己分不清谁对谁错——它只是从源拿数据，源给什么就用什么。要上证300，某个源返回了上证1000的数据，Tool 照单全收。

**输入**: 结构化参数（stock_code, timeframe, days...）
**输出**: 原始数据（1~10 条 dict, 数据量很小）

**记录策略** (入参出参全量记录):
- 入口参数: **记录** (完整记录, 供回溯复现)
- 出口数据: **记录** (1~10 条 dict, 数据量很小, 没理由不记)
- 数据源信息: **记录** (用了哪个源)
- 异常: **记录** (error 类型 + message)

**不做任何校验**:
- Tool 无法判断数据对错 (源给什么就是什么)
- 所谓"校验"在正向链路中都是自欺欺人
- 真正的验证只在回测时: 本次数据 vs 历史数据偏差 → 发现异常 → 可能需要人工介入

**分类**（保留现有 80+ tools，按职责分组）:

| 分组 | Tools | 职责 |
|------|-------|------|
| 行情数据 | search_stock_by_name, get_realtime_quote, agent_get_kline, get_stock_info, get_order_book | 取K线/实时行情/盘口 |
| 指标计算 | analyze_trend, calculate_ma, get_volume_analysis, analyze_pattern, get_indicator_snapshot, run_indicator_signal, get_chip_distribution | 技术指标/形态/筹码 |
| 市场数据 | get_market_overview, get_market_indices, get_sector_rankings, get_hot_sectors, get_sector_fund_flow, get_concept_fund_flow, get_northbound_flow | 大盘/板块/北向 |
| 资金面 | get_fund_flow, get_fund_flow_120d, get_fund_flow_minute, get_margin_trading, get_block_trades | 资金流向/融资融券 |
| 龙虎榜 | get_dragon_tiger, get_dragon_tiger_detail, get_hot_rank, get_zt_pool, get_limit_down, get_broken_board | 龙虎榜/涨停池 |
| 情报 | search_stock_news, search_comprehensive_intel, get_eastmoney_stock_news, get_global_finance_news, get_stock_filings | 新闻/公告/舆情 |
| 基本面 | get_valuation_metrics, batch_valuation_compare, get_financial_statements, get_stock_reports, get_consensus_eps, get_holder_count, get_dividend_history | 估值/财报/股东 |
| 选股 | search_stocks, review_stocks_with_indicator, get_screener_presets, list_user_selection_strategies | 条件选股 |
| 回测 | run_backtest, get_backtest_history | 策略回测 |
| 图表 | render_candlestick, render_candlestick_mini, generate_kline_chart | K线图 |
| 交易 | list_strategies, get_strategy_detail, start_strategy, stop_strategy, get_strategy_trades | 策略启停 |
| 板块分析 | get_hot_sectors, get_sector_trend_analysis, get_sector_history_data, get_sector_prediction, get_sector_cycle, get_stock_sector_info, get_sector_stocks, get_hot_stocks_with_reason, get_stock_concept_blocks, get_lockup_expiry, get_industry_ranking | 板块/概念/解禁 |
| 指标配置 | list_indicators, get_indicator_params | 指标参数查询 |

### 2.2 Skill 层 — "部门领导"

**职责**: 调用 Tools 获取数据，做专业分析，输出**标准化报告**。

**输入**: 分析对象（stock_code / market context）+ 前序 Skill 的报告（可选）
**输出**: **SkillReport** — 标准化结构，每个 Skill 必须交这个报告

**核心概念: 内容主体 + 叠加buff**

每层交的东西不是只有分数。分数和置信度是**叠加在分析内容上的数值标签**，不是唯一产出：
- **内容主体**: 分析文字、推理过程、证据、因子明细 — 这是核心
- **叠加buff**: score (0-100)、confidence (0.0-1.0)、direction (bullish/bearish/neutral)

```python
@dataclass
class SkillReport:
    skill_name: str
    # 叠加buff
    score: float              # 0-100 (50=中性)
    confidence: float         # 0.0-1.0 (数据充分度)
    direction: str            # "bullish" / "bearish" / "neutral"
    signal: str               # 一句话信号
    # 内容主体
    factors: list[FactorItem] # 评分明细
    output_data: dict         # 完整分析报告
    # 调用记录
    tools_called: list[str]
    missing_data: list[str]   # 缺失的数据（如实报告）
    input_params: dict
```

**记录策略**:
- 入口参数: **记录**
- 出口报告: **完整记录**（SkillReport 全量持久化）
- 这是回溯的**核心证据**——谁对谁错，看它交的报告

**现有 16 个 Skill**:

| Skill | 职责 | 依赖 Tools |
|-------|------|-----------|
| technical_agent | 技术面综合分析 | analyze_trend, calculate_ma, get_volume_analysis, analyze_pattern |
| momentum_tracker | 动量/趋势强度 | analyze_trend, get_indicator_snapshot |
| indicator_agent | 指标信号 | run_indicator_signal, get_indicator_snapshot |
| intelligence_agent | 新闻/事件/概念催化 | search_stock_news, search_comprehensive_intel |
| policy_analyst | 政策面分析 | search_comprehensive_intel, get_global_finance_news |
| hot_money_tracker | 游资追踪 | get_dragon_tiger, get_dragon_tiger_detail, get_hot_rank |
| lockup_watcher | 解禁监控 | get_lockup_expiry |
| concept_tracker | 概念/板块追踪 | get_stock_concept_blocks, get_hot_sectors, get_sector_stocks |
| market_data_agent | 市场数据概览 | get_market_overview, get_market_indices, get_sector_rankings |
| screening_agent | 选股筛选 | search_stocks, review_stocks_with_indicator |
| backtest_agent | 策略回测 | run_backtest, get_backtest_history |
| bull_researcher | 多头论证 | (组合多个分析 tools) |
| bear_researcher | 空头反驳 | (组合多个分析 tools) |
| data_agent | 数据工程 | (数据清洗/转换) |
| trading_agent | 交易执行 | list_strategies, start_strategy, stop_strategy |
| analysis_agent | 通用分析 | (组合) |

### 2.3 Chain 层 — "大领导/CEO"

**职责**: 理解用户意图，组装 Skill 链路，汇总各 Skill 报告，输出最终决策。

**输入**: 用户消息 + 上下文
**输出**: **DecisionCard** — 最终决策

```python
@dataclass
class DecisionCard:
    action: str               # "buy" / "sell" / "hold" / "skip"
    score: float              # 加权总分
    confidence: str           # "high" / "medium" / "low"
    stock_code: str
    stock_name: str
    chain_id: str
    skill_reports: list[SkillReport]  # 各 Skill 的原始报告
    breakdown: list[BreakdownItem]    # 分项明细
    gaps: list[Gap]           # 缺失项
    blockers: Blockers        # 阻断器
    human_note: str           # 人工复核提示
    recommendation: str       # 中文建议
```

**记录策略**:
- 入口: 用户消息 + 意图分析结果 — **记录**
- 出口: DecisionCard 全量 — **记录** (Chain 也有内容: 合成的建议文本)
- Chain 没有"原创分析"，但有"合成内容"——把各 Skill 报告汇总成最终建议

**与盈亏的关系**:
- Chain 层的决策可以和盈亏挂钩（A股只能做多，赚了说明方向对）
- 但评判标准仍然是**方向准确率**，不是绝对收益
- 只有 Chain 层有这个"特权"

## 三、回溯评估机制

### 3.1 核心原则

```
结果出来了 → 逐层检查 → 谁对谁加分，谁错谁扣分
```

**三层统一评估机制**: 每层交的东西 vs 实际发生的 → 准确率 → 奖惩

| 层 | 它"交"了什么 | 回测时验证什么 |
|---|---|---|
| Chain | "买" / "卖" / "持有" | 实际涨跌 → 方向准确率 |
| Skill | "看多 score=75, direction=bullish" | 实际涨跌 → 方向准确率 |
| Tool | 数据（隐含了某个状态） | 实际数据偏差 → 数据准确率 |

- **三层都没有"特权"在正向链路中自我验证**，都得等回测才知道对错
- **各负其责**: 数据错了不怪 Chain, 决策错了不怪 Skill, 各管各的准确率
- **缺失如实**: 下层没上班（missing），上层如实报告，回溯时跳过它（不奖不惩）
- **A股只能做多**: 只有 Chain 层的决策和盈亏有直接关系，但评判标准仍然是方向准确率

### 3.2 三层回溯流程

```
T日: 用户问"600519能不能买"
  → Chain 组装 [technical, momentum, intelligence, ...]
  → technical 调用 agent_get_kline(source=eastmoney) → 拿到数据 → 交报告 score=75, direction=bullish
  → momentum 调用 get_indicator_snapshot(source=tencent) → 拿到数据 → 交报告 score=80, direction=bullish
  → intelligence 调用 search_stock_news → 拿到3条新闻 → 交报告 score=45, direction=neutral
  → Chain 汇总: score=68, action=buy

T+1/T+3/T+5: 获取 600519 实际涨跌
  → 实际 direction_3d = bearish (-3.2%)

回溯:
  Chain 层: predicted=buy, actual=bearish → ❌ 错误 → 权重 -
  technical: predicted=bullish, actual=bearish → ❌ 错误 → 权重 -
  momentum: predicted=bullish, actual=bearish → ❌ 错误 → 权重 -
  intelligence: predicted=neutral, actual=bearish → ❌ 错误 (但差距较小) → 轻微 -

深层回溯 (数据偏差检测):
  agent_get_kline: 本次数据 vs 历史数据 → 偏差 500% → ⚠️ 数据源异常!
    → 可能是源返回了错误标的的数据
    → 标记该 source 在该时间段内不可信
    → 可能需要人工介入确认
```

**数据偏差检测** (回溯时才有意义):
- 同一 tool 同一参数, 不同时间拿到的数据偏差巨大 → 数据源异常
- 例如: 某天的成交量和前后几天差 10 倍 → 源可能给了错误数据
- 检测方法: 对比历史数据的统计分布, 超出 3σ 标记异常
- 处理: 标记数据源不可信 + 通知人工确认 + 该次决策结果作废

### 3.3 评估维度

| 层级 | 交了什么 | 验证什么 | 奖惩依据 |
|------|----------|----------|----------|
| **Chain** | action (buy/sell/hold) | 实际涨跌方向 | 方向准确率 → Skill 权重调整 |
| **Skill** | score + direction | 实际涨跌方向 | 因子级方向准确率 → 因子权重调整 |
| **Tool** | 数据 (隐含状态) | 历史数据偏差 | 数据准确率 → 数据源权重调整 |

### 3.4 评估时间窗口

| 窗口 | 用途 | 说明 |
|------|------|------|
| T+1 | 短线验证 | 次日涨跌 |
| T+3 | 中线验证 | 3日涨跌（主验证基准） |
| T+5 | 中线验证 | 5日涨跌 |

方向判定阈值: 0.3%（A股日均波动2-3%，0.3%是有效信号下限）

## 四、数据库设计

### 4.1 核心思想

三层是**同一棵树**的节点，用一张表 + 自引用 parent_id 表达整棵树。

```
Chain (决策)          ← 树根
├── Skill (技术面)    ← 子节点
│   ├── Tool (get_kline)     ← 叶子
│   └── Tool (analyze_trend) ← 叶子
├── Skill (动量)
│   ├── Tool (get_indicator)
│   └── Tool (calculate_ma)
└── Skill (情报)
    └── Tool (search_news)
```

### 4.2 一张表

```sql
CREATE TABLE qd_evaluations (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES qd_evaluations(id) ON DELETE CASCADE,
    root_id         INTEGER,                        -- 根节点 id (方便查整棵树)
    layer           VARCHAR(10) NOT NULL,           -- 'chain' / 'skill' / 'tool'
    name            VARCHAR(80) NOT NULL,           -- chain_id / skill_name / tool_name
    step_order      INTEGER,                        -- 在父节点中的执行顺序

    -- 时间/标的 (根节点填写, 子节点继承)
    exec_date       DATE,
    stock_code      VARCHAR(10),
    stock_name      VARCHAR(50),

    -- 正向: 内容主体 + 数值buff叠加
    score           REAL,                           -- 0-100 叠加buff
    direction       VARCHAR(10),                    -- bullish/bearish/neutral 叠加buff
    action          VARCHAR(10),                    -- buy/sell/hold/skip (仅 chain 层)
    signal          TEXT,                           -- 一句话信号
    confidence      REAL,                           -- 0.0-1.0 叠加buff

    -- 正向: 内容 (全量记录)
    factors         JSONB,                          -- skill: [{name, value, score, weight, status}]
    output_data     JSONB,                          -- chain: 决策卡+建议, skill: 分析报告, tool: 1~10条dict

    -- 正向: 调用信息 (入参出参全量记录)
    input_params    JSONB,                          -- 入口参数 (chain: 用户消息+意图, skill: 分析对象, tool: 完整参数)
    tools_called    JSONB,                          -- skill 调用了哪些 tools
    missing_data    JSONB,                          -- 缺失的数据
    data_source     VARCHAR(50),                    -- tool 实际命中的数据源

    -- 执行信息
    status          VARCHAR(10) DEFAULT 'ok',       -- ok/missing/failed/skipped/veto
    error           TEXT,
    elapsed_ms      REAL,

    -- 反向: 回测验证 (回溯时写入)
    actual_return_1d    REAL,
    actual_return_3d    REAL,
    actual_return_5d    REAL,
    actual_direction_3d VARCHAR(10),                 -- 实际方向
    correct_3d      BOOLEAN,                        -- predicted direction vs actual
    calibration     REAL DEFAULT 1.0,               -- 校准因子 1.00~1.05

    -- 人工介入 (数据异常时)
    human_reviewed  BOOLEAN DEFAULT FALSE,
    human_verdict   VARCHAR(20),                    -- confirmed_anomaly / false_positive / null

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_eval_root ON qd_evaluations(root_id);
CREATE INDEX idx_eval_layer ON qd_evaluations(layer, exec_date);
CREATE INDEX idx_eval_stock ON qd_evaluations(stock_code, exec_date);
CREATE INDEX idx_eval_verify ON qd_evaluations(layer, correct_3d) WHERE correct_3d IS NOT NULL;
CREATE INDEX idx_eval_parent ON qd_evaluations(parent_id);
```

### 4.3 一棵树的存储示例

```
id=1  parent=NULL  root=1  layer=chain   name=evaluate+stock  score=68  action=buy   direction=bullish
id=2  parent=1    root=1  layer=skill   name=technical_agent  score=75  direction=bullish
id=3  parent=2    root=1  layer=tool    name=agent_get_kline  data_source=eastmoney  output_data={...}
id=4  parent=2    root=1  layer=tool    name=analyze_trend    output_data={...}
id=5  parent=1    root=1  layer=skill   name=momentum_tracker score=80  direction=bullish
id=6  parent=5    root=1  layer=tool    name=get_indicator_snapshot  data_source=tencent
id=7  parent=1    root=1  layer=skill   name=intelligence_agent  score=45  direction=neutral  missing_data=["筹码分布"]
id=8  parent=7    root=1  layer=tool    name=search_stock_news  data_source=eastmoney
```

### 4.4 因子权重表 (独立, 跨决策的聚合)

```sql
CREATE TABLE qd_factor_weights (
    chain_id        VARCHAR(50) NOT NULL,
    skill_name      VARCHAR(50) NOT NULL,
    factor_name     VARCHAR(100) NOT NULL,
    weight          REAL DEFAULT 1.0,
    accuracy_3d     REAL,
    sample_count    INTEGER DEFAULT 0,
    decay_half_life INTEGER DEFAULT 30,
    last_updated    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (chain_id, skill_name, factor_name)
);
```

## 五、代码结构

```
backend_api_python/app/agent/
├── agent.py              # 入口 (保留, 重构内部逻辑)
├── model.py              # LLM 适配 (保留)
├── intent_analyzer.py    # 意图分析 (保留)
├── domain_registry.py    # 领域注册 (保留)
├── session_store.py      # 会话管理 (保留)
├── context_compressor.py # 上下文压缩 (保留)
├── schema.py             # 🆕 EvalNode 三层统一数据结构
├── store.py              # 🆕 持久化 (save_tree/load_tree)
│
├── chain/                # Chain 层 — "大领导"
│   ├── chains.py         # 链路定义 (保留, 简化)
│   ├── executor.py       # 🔴 重构: Chain 执行器
│   └── decision.py       # 🆕 决策卡构建逻辑
│
├── skill/                # Skill 层 — "部门领导"
│   ├── __init__.py
│   ├── base.py           # 🆕 BaseSkill 基类 (analyze→SkillReport, call_tool自动记录)
│   ├── contract.py       # 🆕 SkillReport 标准化输出
│   ├── registry.py       # 🆕 自动发现+注册
│   ├── technical.py      # 🆕 技术面 Skill (示例实现)
│   ├── momentum.py       # ⬜ 待迁移
│   ├── indicator.py      # ⬜ 待迁移
│   ├── intelligence.py   # ⬜ 待迁移
│   ├── policy.py         # ⬜ 待迁移
│   ├── hot_money.py      # ⬜ 待迁移
│   ├── lockup.py         # ⬜ 待迁移
│   ├── concept.py        # ⬜ 待迁移
│   ├── market_data.py    # ⬜ 待迁移
│   ├── screening.py      # ⬜ 待迁移
│   ├── backtest.py       # ⬜ 待迁移
│   ├── bull.py           # ⬜ 待迁移
│   ├── bear.py           # ⬜ 待迁移
│   └── trading.py        # ⬜ 待迁移
│
├── tools/                # Tools 层 — "干活的" (保留现有, 适配新接口)
│   ├── registry.py       # 🔴 重构: 去掉 smolagents 依赖
│   └── ...               # 80+ tools 保留
│
├── evaluator/            # 🆕 回溯评估引擎
│   ├── __init__.py
│   └── engine.py         # 🆕 evaluate_pending + update_factor_weights
│
└── router/               # 路由 (保留, 简化)
```

## 六、执行流程 (重构后)

### 6.1 正向: 用户提问 → 决策输出

```
1. 用户: "帮我分析600519"
2. intent_analyzer: verb=analyze, noun=stock → 匹配 chain "evaluate+stock"
3. ChainExecutor.execute():
   a. 遍历 chain.steps (N 个 skill)
   b. 对每个 skill:
      - skill.run(stock_code="600519"):
        - skill.call_tool("agent_get_kline", ...) → 拿到数据, 自动记录入参出参
        - skill.call_tool("analyze_trend", ...) → 拿到数据, 自动记录入参出参
        - tool 从源拿数据, 源给什么就是什么 (tool 不判断对错)
        - 汇总 tool 结果 → SkillReport(score=75, direction="bullish", factors=[...])
      - 返回 (SkillReport, EvalNode[含 tool 子节点])
   c. DecisionBuilder.build():
      - 收集所有 SkillReport
      - 加权求和 (缺失项从权重池剔除)
      - 渐进门控 (样本量不足 → 分数向50收缩)
      - 三重阻断 (veto / 覆盖度不足 / 无数据)
      - 输出 DecisionCard
   d. save_tree(chain_node) → 持久化整棵树到 qd_evaluations
4. 返回 DecisionCard 给用户
```

### 6.2 反向: 结果回溯

```
[每日盘后 worker]

1. evaluate_pending():
   - 查找 correct_3d IS NULL 的 chain 节点
   - 获取 T+3 实际涨跌
   - 逐层写回 correct_3d + calibration

2. update_factor_weights():
   - 从已验证的 skill 节点提取因子
   - 带时间衰减聚合准确率
   - 写入 qd_factor_weights

3. 数据偏差检测 (TODO):
   - 同 tool 同参数不同时间的数据偏差
   - 超出 3σ 标记异常
   - 人工介入确认
```

## 七、与旧版的关键差异

| 维度 | 旧版 | 新版 |
|------|------|------|
| Skill 接口 | smolagents ManagedAgent, 自由文本输出 | BaseSkill 基类, 统一 SkillReport |
| Tool 接口 | smolagents Tool, 自动分页 | 纯函数, call_tool 自动记录入参出参 |
| Tool 校验 | 无 | 正向不校验 (源给什么用什么), 回溯时偏差检测 |
| 评估粒度 | 决策级 + 因子级混合 | 三层统一: Chain→决策级, Skill→方向级, Tool→数据偏差 |
| 责任归属 | 混合 | 清晰: 每层只对自己交的报告负责 |
| 缺失处理 | 统一标记 missing | 每层如实上报 missing_data, 回溯时跳过 |
| 数据库 | 5张表混用 | 1棵树 (qd_evaluations) + 1张因子权重表 |
| 内容记录 | 只记分数 | 入参+出参+内容+分数/置信度(叠加buff) 全量记录 |

## 八、实施状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 数据库 schema (agent_v2.sql) | ✅ |
| P0 | EvalNode 三层统一数据结构 (schema.py) | ✅ |
| P0 | 持久化 (store.py) | ✅ |
| P0 | SkillReport 契约 (skill/contract.py) | ✅ |
| P0 | BaseSkill 基类 (skill/base.py) | ✅ |
| P0 | Skill 注册表 (skill/registry.py) | ✅ |
| P0 | 示例 Skill (skill/technical.py) | ✅ |
| P0 | 回溯评估引擎 (evaluator/engine.py) | ✅ |
| P1 | Chain executor 重构 | ⬜ |
| P1 | 剩余 Skill 迁移 | ⬜ |
| P1 | Tools 适配 (记录数据源) | ⬜ |
| P2 | 后台 worker (每日盘后自动评估) | ⬜ |
| P2 | 数据偏差检测 | ⬜ |
| P3 | 前端 DecisionCard 可视化 | ⬜ |
