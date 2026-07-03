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

## 评分权重参考

### 盘中策略（intraday）评分细则

| 因子 | 分值 | 说明 |
|------|------|------|
| 基础分 | 55 | 每只候选股起始分 |
| 弱转强 | +15 | 价格站稳MA5上方，之前均线附近震荡 |
| 强转弱 | -15 | 价格跌破MA5无法收复 |
| 放量上涨 | +12 | 量比>1.5且收盘上涨 |
| 缩量拉升 | +10 | 量比<0.7且收盘上涨（主力控盘） |
| 放量下跌 | -10 | 量比>1.5且收盘下跌（真实抛压） |
| 连板 | +12 | 连续涨停天数加分 |
| 龙回头 | +10 | 涨停回调后弱转强 |
| 站上MA5 | +5 | 收盘价在MA5上方 |
| 站上MA20 | +5 | 收盘价在MA20上方 |
| MA5上升 | +5 | MA5斜率>0.5% |
| MACD红柱 | +3 | 日线MACD为红柱 |
| MACD绿柱+MA5上方 | +5 | MACD绿柱但价格稳在MA5上方（弱转强信号） |
| RSI中性 | +3 | RSI在40-60区间 |
| RSI偏高 | -5 | RSI>75 |
| 多周期共振 | +5~+18 | 日线+15m+5m多周期一致 |
| 资金净流入 | +5 | 主力资金净流入 |

### 尾盘策略（eod）评分细则

| 因子 | 分值 | 说明 |
|------|------|------|
| 基础分 | 50 | 起始分，基于尾盘特征验证 |
| 收盘=最高价 | +18 | 收盘价与最高价差<0.3% |
| 收盘接近最高价 | +12 | 价差<0.8% |
| 大幅放量 | +12 | 量比>2.5 |
| 放量 | +8 | 量比>1.5 |
| 收盘在日内高位 | +10 | 收盘位置在日内高85%分位以上 |
| 涨幅适中 | +8 | 涨幅4-6% |
| 主线题材 | +10 | 踩中当日主线热点 |
| RSI超买 | -10 | RSI>80 |
| MACD金叉 | +8 | 日线MACD金叉 |
| MACD死叉 | -5 | 日线MACD死叉 |
| 主力净流入 | +6 | 主力资金净流入为正 |
| 主力净流出 | -4 | 主力净流出>500万 |

### 盘后策略（post_market）评分细则

| 因子 | 分值 | 说明 |
|------|------|------|
| 基础分 | 形态检测得分 | 6种技术形态检测的累计分 |
| RSI>80 | -10 | 超买风险 |
| RSI>70 | -3 | 偏高预警 |
| RSI 40-60 | +3 | 中性区间加分 |
| KDJ金叉 | +5 | 日线KDJ金叉 |
| 多头排列 | +5 | MA5>MA10>MA20 |
| 形态确认后MACD金叉 | +5 | 快照确认金叉 |
| 形态确认后MACD死叉 | -5 | 快照确认死叉 |
| 主力净流入 | +5 | 确认日主力资金净流入 |
| 主力净流出>500万 | -3 | 资金流出预警 |

各策略最终评分统一按以下标准映射方向：
- **score ≥ 55**: bullish（看多）
- **45 ≤ score < 55**: neutral（中性）
- **score < 45**: bearish（看空）

Phase 2 最终结果中，评分 < 60 或方向不为 bullish 的标的自动淘汰。

## 失败处理

- Phase 1 失败 → 不重试，告知用户"选股执行失败"
- Phase 2 单只分析失败 → 跳过该股，继续下一只

## 参考资料

按需查阅:
- `references/trading-logic.md` — 核心交易逻辑（弱转强、放量、MACD 等）
- `references/market-sentiment.md` — 市场情绪判断标准
- `references/limit-rules.md` — 涨跌停规则和过滤规则
- `references/strategy-notes.md` — 已验证策略参考（BB 超卖、龙回头等）
