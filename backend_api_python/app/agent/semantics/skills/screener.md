---
# 选股技能（统一调度，按时间自动切换盘中/尾盘/盘后策略）
names:
  - market_screener
tags: [finance, screener]
priority: 9
default_weight: 1.0
standard_output: true
tools:
  - search_stocks
  - get_indicator_snapshot
  - get_fund_flow
  - get_fund_flow_minute
  - get_market_indices
  - get_limit_pool
  - get_hot_sectors
  - get_hot_stocks_with_reason
  - get_realtime_quote
  - agent_get_kline
  - search_stock_by_name
  - get_order_book
---

# 选股技能

统一的 A 股选股技能，根据当前交易时间段自动切换策略（盘中短线/尾盘隔夜/盘后复盘）。

## 自动策略调度

系统按当前时间自动选择策略
## 各策略说明

### 盘中短线（intraday）

- **适用场景**：今天买什么、短线机会、涨停股追板、找短线标的
- **核心数据源**：涨停池、同花顺强势股题材归因、热门板块、资金流
- **Phase 1**：涨停池连板筛选 + 主线题材强势股 + 龙回头弱转强扫描
- **Phase 2**：技术面验证 + 工具深入分析（资金流、指标快照）

### 尾盘隔夜（eod）

- **适用场景**：尾盘买什么、隔夜持仓、次日计划
- **核心特征**：收盘接近最高价、尾盘放量抢筹、主线题材、涨幅适中
- **Phase 1**：search_stocks 条件初筛 + Python 尾盘特征验证 + 尾盘封板识别
- **Phase 2**：工具深入分析 + 隔夜风险评估（RSI偏高/涨幅过大）

### 盘后复盘（post_market）

- **适用场景**：收盘后明天买什么、盘后复盘、短线技术选股
- **形态识别**：平台突破 / 底部放量启动 / 均线支撑回踩 / MACD 金叉 / 缩量回调放量突破 / 突破前高
- **Phase 1**：全市场形态扫描（强势股池 + 条件选股池）
- **Phase 2**：工具深入分析 + 次日介入点计算（入场价/止损/目标位/盈亏比）
