---
# 选股技能统一定义（盘中/尾盘/盘后三个场景）
names:
  - short_term_screener
  - eod_screener
  - post_market_screener
tags: [finance, screener]
priority: 8
default_weight: 1.0
standard_output: true
tools:
  - search_stocks
  - get_indicator_snapshot
  - get_fund_flow_realtime
  - get_fund_flow_minute
  - get_market_indices
  - get_limit_pool
  - get_hot_sectors
  - get_hot_stocks_with_reason
  - agent_technical_analysis
  - get_realtime_quote
  - agent_get_kline
  - search_stock_by_name
  - get_order_book
---

# 选股技能

统一的选股技能，根据当前时间比对时间段范围自动切换策略。三个场景共享工具集，分析逻辑各有侧重。

## 场景选择

| 场景 | Skill Name | 时间 | 优先级 | 核心策略 |
|------|-----------|------|--------|---------|
| 盘中 | short_term_screener | 9:30-14:30 | 9 | 涨停池连板 + 热门板块龙头 + 题材归因 |
| 尾盘 | eod_screener | 14:30-15:00 | 8 | 条件初筛 + 尾盘特征验证 + 收盘抢筹 |
| 盘后 | post_market_screener | 15:00+ | 7 | 技术形态筛选 + 介入点计算 + 次日计划 |

---

## 盘中选股（short_term_screener）

适用于盘中实时选股，9:30-14:30。

### 选股流程
1. **全市场预筛选** — 涨停池连板 + 热门板块龙头 + 强势股题材归因
2. **候选股深入分析** — 技术面 + 资金流逐只分析
3. **综合排序** — 按短线强度和资金面排序

### 短线核心工具
- get_limit_pool — 涨停池连板股（短线核心）
- get_hot_sectors — 热门板块（板块主线）
- get_hot_stocks_with_reason — 强势股 + 题材归因（短线打板必用）
- get_fund_flow_minute — 分钟级资金流（盘中盯资金）
- get_order_book — 五档盘口（盘口语言）

### 适用场景
- 今天买什么
- 短线机会
- 涨停股里哪些能追
- 找短线标的

---

## 尾盘选股（eod_screener）

适用于 14:30 后尾盘选股。

### 选股流程
1. **条件初筛** — search_stocks 条件选股
2. **尾盘特征验证** — Python 验证：收盘接近最高价 + 放量 + 主线题材
3. **深入分析** — 技术面 + 资金流逐只分析

### 尾盘选股特征
- 收盘价接近当日最高价（尾盘拉升）
- 尾盘放量（资金抢筹）
- 属于当日主线题材（板块联动）
- 主力资金净流入（非散户追高）

### 适用场景
- 14:30 后尾盘买什么
- 隔夜持仓标的
- 次日计划

---

## 盘后选股（post_market_screener）

适用于收盘后复盘选股。

### 选股流程
1. **技术形态筛选** — 平台突破 / 底部放量启动 / 均线支撑回踩 / MACD 金叉 / 缩量回调放量突破 / 突破前高
2. **候选股深入分析** — 技术面 + 资金流逐只分析
3. **计算介入点** — 入场价 / 止损位 / 目标位

### 盘后分析特点
- 用全天 K 线数据（非盘中实时）
- 重点关注尾盘异动（收盘接近最高价 + 放量）
- 结合全天资金流向判断主力意图
- 次日介入点计算（开盘预期区间）

### 适用场景
- 收盘后明天买什么
- 盘后复盘选股
- 短线技术选股
