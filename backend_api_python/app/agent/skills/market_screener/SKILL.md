---
name: market-screener
version: 4.0.0
description: 从A股全市场筛选短线标的。用户问"今天买什么股""有什么好股票""短线选什么"时使用。不含个股分析。
tags: [market, screener, short_term, a_share]
tools:
  - get_fund_flow
  - get_indicator_snapshot
  - search_stocks
  - agent_get_kline
---

# 全市场短线选股 (market-screener)

根据当前交易时间自动切换策略，从A股全市场筛选短线标的。

## 使用场景

- 用户询问"今天买什么股""有什么好股票""短线选什么"
- 盘后复盘、尾盘隔夜选股、盘中热点追踪
- 全市场系统性筛选，非单只股票分析

如果用户指定了具体股票代码，不要调用本技能，使用其他个股分析技能。

## 执行流程

### Phase 1: 市场状态评估 + 候选池构建

调用 `pre_screen()` 获取市场状态和候选股：

```python
from app.agent.skills.market_screener.run import pre_screen
phase1 = pre_screen()
```

`pre_screen()` 不接受任何参数。返回:
- `phase1["main_themes"]` — 主线题材
- `phase1["candidates"]` — 候选股列表，每个候选股结构:
  - `code` — 股票代码（如 "002168"）
  - `name` — 股票名称
  - `score` — 评分（0-100）
  - `reason` — 入选原因
  - `signals` — 信号列表
  - `source` — 来源
- `phase1["strategy"]` — 当前策略（intraday/eod/post_market）

如果 `phase1["error"]` 存在，Phase 1 失败，直接告知用户，不重试。

### Phase 2: 逐只深入分析

调用 `deep_analyze(prescreen_result=phase1)` 对候选股逐只分析：

参数:
- `prescreen_result`: Phase 1 的返回值（必须传入）

返回:
- `result["output_data"]["analyzed"]` — 深入分析结果，每个元素结构:
  - `code` — 股票代码
  - `name` — 股票名称
  - `score` — 综合评分（0-100）
  - `direction` — 方向（bullish/neutral/bearish）
  - `confidence` — 置信度（0-1）
  - `signals` — 信号列表
  - `reason` — 入选原因
- `result["score"]` — 综合评分（0-100）
- `result["direction"]` — 方向（bullish/neutral/bearish）
- `result["confidence"]` — 置信度（0-1）
- `result["strategy"]` — 当前策略（intraday/eod/post_market）
- `result["analysis"]` — 完整报告

Phase 2 内部自动调用工具（`get_fund_flow`、`get_indicator_snapshot`、`search_stocks`），无需手动调用。

### 结果汇报

直接用 Phase 2 的 result 汇报，不要手动拼接 Phase 1 数据。向用户展示:
1. 策略和综合评分
2. analyzed 列表中的标的（code、name、score、direction、signals）
3. 操作建议

## 失败处理

- Phase 1 失败 → 不重试，告知用户"选股执行失败"
- Phase 2 单只分析失败 → 跳过该股，继续下一只

## 参考资料

按需查阅:
- `references/trading-logic.md` — 核心交易逻辑（弱转强、放量、MACD 等）
- `references/market-sentiment.md` — 市场情绪判断标准
- `references/limit-rules.md` — 涨跌停规则和过滤规则
- `references/strategy-notes.md` — 已验证策略参考（BB 超卖、龙回头等）
