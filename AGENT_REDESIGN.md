# Agent 架构重设计 — 三层决策树

> 日期: 2026-06-07（初版）→ 2026-06-08（重写）
> 状态: P0 完成，P1 进行中
> 关联项目: https://github.com/yuguonet/QuantDinger


## 二、新架构：三层决策树

### 核心哲学
1. **价格折扣一切**（Dow Theory）— 所有信息最终反映在价格上
2. **能回测验证的才是可靠的** — K线/技术指标可回测，新闻/政策不可回测
3. **分项独立打分，无优先级** — 每项独立贡献，缺了就是0分，不影响其他项
4. **内容主体 + 叠加buff** — 分数不是唯一产出，分析文字/推理过程/因子明细才是核心
5. **正向不校验，回溯才验证** — Tool 从源拿数据，源给什么用什么，不自欺欺人
6. **三层各负其责** — Chain 管决策方向，Skill 管分析方向，Tool 管数据准确率

### 架构总览

```
用户消息
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

- **正向流**: 用户 → Chain → Skill → Tools
- **反向流**: 结果 → Chain → Skill → Tools（回溯评估）
- 允许向下越级（Chain 直接调 Tool）
- **禁止**下级调上级、平级相互调用

### 设计规则

```
每个子项: 0-100 分（正常），-1000 分（极度偏差/一票否决）
最终得分: 各子项分数 × 权重，加权求和（缺失项从权重池剔除）
权重迭代: 用实际结果验证，自动调整权重
否决机制: -1000 在体系内，不用额外层
```

### 三层定义

#### Chain 层 — "大领导/CEO"
- **职责**: 理解用户意图，组装 Skill 链路，汇总各 Skill 报告，输出最终决策
- **输入**: 用户消息 + 上下文
- **输出**: DecisionResult（action/score/direction/reason + 整棵 EvalNode 树）
- **与盈亏的关系**: A股只能做多，Chain 层决策可与盈亏挂钩，评判标准是方向准确率

#### Skill 层 — "部门领导"
- **职责**: 调用 Tools 获取数据，做专业分析，输出标准化 SkillReport
- **输入**: 分析对象（stock_code）+ 前序 Skill 的报告（可选）
- **输出**: SkillReport — 内容主体（analysis/factors/output_data）+ 叠加buff（score/confidence/direction/signal）
- **核心**: 每个 Skill 是 BaseSkill 子类，call_tool 自动记录入参出参

### Tool 层 — "干活的"
- **职责**: 取数据、算指标、跑回测。搬运工，不做校验
- **输入**: 结构化参数（stock_code, timeframe, days...）
- **输出**: 原始数据,存表时只保留摘要(1~10条)，分析用的数据不受影响
- **记录策略**: 入口参数**记录**、出口数据**记录**、数据源信息**记录**、异常**记录**

#### Tool 分类（80+ tools，按职责分组）

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

### Skill 层 — "部门领导"
- **职责**: 调用 Tools 获取数据，做专业分析，输出标准化 SkillReport
- **输入**: 分析对象（stock_code）+ 前序 Skill 的报告（可选）
- **输出**: SkillReport — 内容主体（analysis/factors/output_data）+ 叠加buff（score/confidence/direction/signal）
- **核心**: 每个 Skill 是 BaseSkill 子类，call_tool 自动记录入参出参
- **记录策略**: 入口参数**记录**、出口报告**完整记录**（SkillReport 全量持久化，是回溯的核心证据）

#### 16 个内置 Skill

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
| data_engineer | 数据工程 | (数据清洗/转换) |
| trading_agent | 交易执行 | list_strategies, start_strategy, stop_strategy |
| analysis_agent | 通用分析 | (组合) |

### 示例
```
技术面 Skill:  score=75, direction=bullish  × weight=1.0 = 75.0
动量 Skill:    score=80, direction=bullish  × weight=0.8 = 64.0
情报 Skill:    score=45, direction=neutral  × weight=1.0 = 45.0
───────────────────────────────────────────────────────────
加权总分: (75×1.0 + 80×0.8 + 45×1.0) / (1.0+0.8+1.0) = 65.4
渐进门控: 50 + (65.4 - 50) × 0.7 = 60.8
决策: BUY（score ≥ 60, direction = bullish）
```

### 架构优势
- **无优先级** — 不依赖任何单一数据源
- **可回测** — 每个子项可单独验证胜率
- **可迭代** — 只调权重，不改架构
- **可审计** — 用户看到分数明细就知道为什么买/卖
- **一票否决内建** — -1000就是否决
- **缺数据不致命** — 缺了就是0分，其他项正常工作
- **责任清晰** — 三层各负其责，数据错了不怪Chain，决策错了不怪Skill

## 三、分析层次

```
市场分析（大盘/板块/宏观）   ← 锦上添花的花
个股分析（基本面/消息/资金）  ← 锦上添花
K线技术分析（价格/量/指标）  ← 蛋糕本身
```

- K线技术分析是**唯一能回测验证**的地基
- 个股分析用来验证和增强信心
- 市场分析用来解释异常

## 四、数据陷阱警告

| 数据源 | 陷阱 | 正确用法 |
|--------|------|----------|
| 龙虎榜 | 盘后公布，游资一日游 | 风险警示，不是买入信号 |
| 资金流向 | 滞后，主力可对倒 | 验证工具，不单独决策 |
| 新闻/舆情 | 你看到时市场已反应 | 解释工具，不预测方向 |
| 北向资金 | 有时滞 | 连续大幅流入/流出才值得关注 |
| 解禁/减持 | 提前公布，市场可能已消化 | 中长线风险，短线可忽略 |

## 五、数据库设计

### 两张表，一棵树

```
qd_evaluations — 三层统一评估树（自引用 parent_id）
qd_factor_weights — 因子权重聚合（跨决策独立）
```

#### qd_evaluations（主表）
一棵树存一张表，用 parent_id 自引用：
```
id=1  parent=NULL  root=1  layer=chain   name=evaluate+stock  score=68  action=buy
id=2  parent=1    root=1  layer=skill   name=technical_agent  score=75  direction=bullish
id=3  parent=2    root=1  layer=tool    name=agent_get_kline  data_source=eastmoney
id=4  parent=2    root=1  layer=tool    name=analyze_trend
id=5  parent=1    root=1  layer=skill   name=momentum_tracker score=80  direction=bullish
id=6  parent=5    root=1  layer=tool    name=get_indicator_snapshot
```

关键字段：
- `factors JSONB` — skill: [{name, value, score, weight, status}]
- `output_data JSONB` — chain: 决策卡, skill: 分析报告, tool: 1~10条dict
- `analysis TEXT` — 分析文字（内容主体）
- `correct_3d BOOLEAN` — 回溯验证结果
- `calibration REAL` — 校准因子 1.00~1.05

#### qd_factor_weights（因子权重表）
```sql
PRIMARY KEY (chain_id, skill_name, factor_name)
weight REAL DEFAULT 1.0
accuracy_3d REAL
sample_count INTEGER DEFAULT 0
decay_half_life INTEGER DEFAULT 30  -- 因子级半衰期
```

因子类型 → 半衰期映射：
- 政策/消息类 → 7天（信息快速消化）
- 游资/资金类 → 14天（资金流向变化快）
- 概念/板块类 → 21天（题材生命周期）
- 技术指标类 → 30天（市场风格切换）
- 量价关系类 → 60天（较稳定统计规律）

## 六、代码结构

```
backend_api_python/app/agent/
├── agent.py              # 入口（保留，_try_chain 已适配新架构）
├── model.py              # LLM 适配（保留）
├── intent_analyzer.py    # 意图分析（保留）
├── domain_registry.py    # 领域注册（保留）
├── session_store.py      # 会话管理（保留）
├── context_compressor.py # 上下文压缩（保留）
│
├── chain/                # Chain 层 — "大领导"
│   ├── schema.py         # ✅ EvalNode + SkillReport + FactorItem
│   ├── store.py          # ✅ 持久化（save_tree / load_tree / query）
│   ├── contract.py       # ✅ SkillReport 解析契约（三重降级）
│   ├── chains.py         # 链路定义（保留）
│   ├── executor.py       # ✅ Chain 执行器（构建 EvalNode 树）
│   └── evaluator.py      # ✅ 回溯评估引擎（单表 + 因子权重）
│
├── skills/               # Skill 层 — "部门领导"
│   ├── __init__.py       # ✅ 模块说明
│   ├── base.py           # ✅ BaseSkill 基类（call_tool 自动记录）
│   ├── registry.py       # ✅ 自动发现 + 注册
│   ├── technical.py      # ✅ 技术面（完整示例）
│   ├── momentum.py       # ⬜ 待迁移
│   ├── indicator_agent.py # ⬜ 待迁移
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
├── tools/                # Tool 层 — "干活的"（保留现有 80+ tools）
│   ├── registry.py       # 工具注册
│   └── ...               # 保留不变
│
└── evaluator.py          # 旧版在线评估器（保留，与 chain/evaluator.py 并存）
```

## 七、回溯评估机制

### 核心原则

```
结果出来了 → 逐层检查 → 谁对谁加分，谁错谁扣分
```

**三层统一评估机制**: 每层交的东西 vs 实际发生的 → 准确率 → 奖惩

| 层 | 它"交"了什么 | 回测时验证什么 | 奖惩依据 |
|---|---|---|---|
| **Chain** | action (buy/sell/hold) | 实际涨跌方向 | 方向准确率 → Skill 权重调整 |
| **Skill** | score + direction | 实际涨跌方向 | 因子级方向准确率 → 因子权重调整 |
| **Tool** | 数据 (隐含状态) | 历史数据偏差 | 数据准确率 → 数据源权重调整（TODO） |

- **三层都没有"特权"在正向链路中自我验证**，都得等回测才知道对错
- **各负其责**: 数据错了不怪 Chain, 决策错了不怪 Skill, 各管各的准确率
- **缺失如实**: 下层没上班（missing），上层如实报告，回溯时跳过它（不奖不惩）
- **A股只能做多**: 只有 Chain 层的决策和盈亏有直接关系，但评判标准仍然是方向准确率

### 回溯流程示例

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

深层回溯 (数据偏差检测 — TODO):
  agent_get_kline: 本次数据 vs 历史数据 → 偏差 500% → ⚠️ 数据源异常!
    → 可能是源返回了错误标的的数据
    → 标记该 source 在该时间段内不可信
    → 可能需要人工介入确认
```

### 评估时间窗口

| 窗口 | 用途 | 说明 |
|------|------|------|
| T+1 | 短线验证 | 次日涨跌 |
| T+3 | 中线验证 | 3日涨跌（**主验证基准**） |
| T+5 | 中线验证 | 5日涨跌 |

方向判定阈值: 0.3%（A股日均波动2-3%，0.3%是有效信号下限）
中性结果: actual == neutral 仍参与计分（避免震荡市样本流失）
校准因子: score 越偏离50（越自信），权重更新幅度越大（±5%）

### 评估流程（每日盘后自动运行）

```
1. evaluate_pending():
   - 查找 correct_3d IS NULL 的根节点
   - 获取 T+1/3/5 实际涨跌
   - 写回根节点 + skill 子节点的 correct_3d + calibration

2. update_factor_weights():
   - 从已验证的 skill 节点提取因子（factors JSONB）
   - 带时间衰减聚合准确率（因子级半衰期）
   - 校准因子微调（±5%）
   - 写入 qd_factor_weights

3. 下次执行:
   - _load_skill_weights() → 从 evaluator 读取新权重
   - 权重自动迭代生效
```

## 八、出厂权重

系统冷启动时无历史数据，所有权重默认 1.0（均匀分配）。但不同 Skill/Chain 的可靠性天然不同，应设置出厂权重作为初始值，后续由回溯评估覆盖。

### Skill 出厂权重

| Skill | 初始权重 | 理由 |
|-------|---------|------|
| technical_agent | 1.2 | K线地基，最可回测 |
| momentum_tracker | 1.1 | 趋势持续性可统计 |
| indicator_agent | 1.1 | 技术指标可回测 |
| backtest_agent | 1.0 | 策略回测验证 |
| screening_agent | 1.0 | 选股验证 |
| bull_researcher | 1.0 | 多头论证，对冲 |
| bear_researcher | 1.0 | 空头反驳，对冲 |
| trading_agent | 1.0 | 交易执行 |
| market_data_agent | 0.9 | 锦上添花 |
| concept_tracker | 0.9 | 题材生命周期不稳定 |
| lockup_watcher | 0.8 | 提前公布，市场可能已消化 |
| intelligence_agent | 0.8 | 信息滞后，你看到时市场已反应 |
| data_engineer | 0.8 | 辅助角色 |
| policy_analyst | 0.7 | 99%交易日无重大政策，浪费步骤 |
| hot_money_tracker | 0.7 | 龙虎榜盘后+游资一日游，追买=接盘 |

实现：`skills/base.py` 加 `default_weight` 属性，`store.get_skill_weights()` 读不到历史数据时 fallback 到出厂值。

### Chain 出厂权重

| Chain | 初始权重 | 理由 |
|-------|---------|------|
| evaluate+stock | 1.2 | 13步完整分析，最全面 |
| screen+stock | 1.0 | 3步选股，轻量但实用 |
| scan+market | 0.9 | 3步大盘，辅助决策 |

实现：`chains.py` 的 `ChainDef` 加 `default_weight` 字段，`executor._load_skill_weights()` 读不到历史数据时 fallback。

### Tool 出厂权重

Tool 权重含义不同于 Skill/Chain——不是"评分贡献"，而是"数据源可靠度"。

| Tool 分组 | 初始权重 | 理由 |
|-----------|---------|------|
| 行情数据 | 1.0 | 基础数据，必须准确 |
| 指标计算 | 1.0 | 本地计算，不依赖源 |
| 选股 | 1.0 | 本地筛选 |
| 回测 | 1.0 | 本地计算 |
| 图表 | 1.0 | 本地渲染 |
| 交易 | 1.0 | 执行层，必须可靠 |
| 基本面 | 0.9 | 季报数据，相对稳定 |
| 市场数据 | 0.9 | 多源降级，偶尔不准 |
| 板块分析 | 0.8 | 题材不稳定 |
| 资金面 | 0.8 | 滞后+主力可对倒 |
| 龙虎榜 | 0.7 | 盘后公布，游资一日游 |
| 情报 | 0.7 | 你看到时市场已反应 |

实现：`tools/` 下按分组加 `default_reliability` 属性，`store.py` 新增 `get_tool_weights()` 供回溯时对比验证。

## 九、实施状态

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 数据库 schema (agent_v2.sql) | ✅ |
| P0 | EvalNode 三层统一数据结构 (chain/schema.py) | ✅ |
| P0 | 持久化 (chain/store.py) | ✅ |
| P0 | SkillReport 契约 (chain/contract.py) | ✅ |
| P0 | BaseSkill 基类 (skills/base.py) | ✅ |
| P0 | Skill 注册表 (skills/registry.py) | ✅ |
| P0 | 示例 Skill (skills/technical.py) | ✅ |
| P0 | 回溯评估引擎 (chain/evaluator.py) | ✅ |
| P0 | Chain executor 适配 (chain/executor.py) | ✅ |
| P0 | agent.py / routes 适配 | ✅ |
| P1 | 剩余 15 个 Skill 迁移到 BaseSkill | ⬜ |
| P1 | Tools 适配（记录数据源） | ⬜ |
| P2 | 前端 DecisionResult 可视化 | ⬜ |
| P2 | 数据偏差检测（Tool 层回溯验证） | ⬜ |

## 十、当前缺陷与待修复

### P0（必须修）

#### 1. 两套 Skill 体系并存，无桥接
- 旧体系：`@skill` 装饰器 → SkillRegistry → smolagents ManagedAgent
- 新体系：BaseSkill 子类 → skill_registry.discover() → 直接实例化
- 15 个旧 Skill（momentum, bear, hot_money...）还是 `@skill` 空壳 class
- 只有 `technical.py` 迁移到了 BaseSkill
- **`agent.py` 的 `_try_chain` 还在用旧的 `run_agent_fn`**（调 smolagents ManagedAgent），没有适配 `BaseSkill.run()`
- executor 期望 `run_skill_fn → (SkillReport, EvalNode)`，agent.py 传入的是旧签名
答:我倾向用@skill装饰器的方式

#### 2. async/sync 不匹配
- `BaseSkill.run()` 和 `BaseSkill.analyze()` 是 `async def`
- `agent.py` 和 `executor.execute()` 是同步的
- **async 的 BaseSkill 塞不进同步的 executor**

#### 3. 遗留文件未清理
- `chain/skill_contract.py` — 旧版解析器，已被 `chain/contract.py` 替代，还留着
- `evaluator.py`（agent 根目录）— 旧版在线评估器（598行），引用旧5表 schema，和 `chain/evaluator.py` 功能重叠
- 旧 skills 目录下的 `@skill` 装饰器 class 没清理
答:清理
### P1（应该修）

#### 4. 两套 evaluator 职责混乱
- `agent/evaluator.py` — 在线执行质量评估（每次 agent.run() 后 <1ms）
- `agent/chain/evaluator.py` — 盘后回溯评估（T+N 验证 + 因子权重更新）
- 名字一样，职责完全不同；agent/evaluator.py 引用旧5表 schema，已不兼容
答:不太懂如何实现在线执行质量评估
#### 5. guidance.py / context_compressor.py 没接入新架构
- `guidance.py` — 数据优先框架 + 数据陷阱警告，没注入到 BaseSkill 的 prompt
- `context_compressor.py` — 方向性关键词净化，没接入新的 Skill 执行流
- 这两个是旧架构的"补丁"，新架构应内化到 BaseSkill 或 contract 层

#### 6. contract.py 解析不够健壮
- `_try_parse_json_block` 的正则匹配不到嵌套 JSON
- 没有处理 LLM 输出中多个 JSON 块的情况
- `extract_tools_called` 的正则太简单，漏掉很多工具调用模式

### P2（可以后做）

#### 7. Tool 层完全没动
- `tools/registry.py` 还是 smolagents Tool 模式
- 没有 `default_reliability` 属性
- `store.py` 没有 `get_tool_weights()` 函数
- AGENT_REDESIGN 写了 Tool 出厂权重，代码未实现

#### 8. store.py 缺少增量更新
- `save_tree()` 是全量覆盖（DELETE old children + INSERT new）
- 应该支持增量：只更新变化的节点

#### 9. DecisionResult 和 EvalNode 是两套结构
- `DecisionResult` 是独立 dataclass，和 `EvalNode` 树并存
- 应该直接用 `root_node`（EvalNode）作为结果载体，不需要额外包装

#### 10. 缺少错误恢复机制
- Skill 执行超时/崩溃，直接标记 failed，没有重试
- LLM 返回格式不合规，降级到兜底 neutral，不会触发重试
- 应该有：失败重试（最多1次）+ 降级策略

## 十一、与旧版的关键差异

| 维度 | 旧版 | 新版 |
|------|------|------|
| 架构 | 扁平 scoring/items | 三层树 Chain→Skill→Tool |
| Skill 接口 | smolagents ManagedAgent, 自由文本 | BaseSkill 基类, 统一 SkillReport |
| Tool 接口 | smolagents Tool | 纯函数, call_tool 自动记录 |
| 数据库 | 5张表混用 | 1棵树 (qd_evaluations) + 1张因子权重表 |
| 评估粒度 | 决策级 + 因子级混合 | 三层统一: Chain→决策级, Skill→方向级, Tool→数据偏差 |
| 内容记录 | 只记分数 | 入参+出参+内容+分数/置信度 全量记录 |
| 责任归属 | 混合 | 清晰: 每层只对自己交的报告负责 |
