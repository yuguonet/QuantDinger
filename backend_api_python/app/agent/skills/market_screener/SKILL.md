---
name: market-screener
version: 5.0.0
description: 从A股全市场筛选短线标的。用户问"今天买什么股""有什么好股票""短线选什么"时使用。不含个股分析。
tags: [market, screener, short_term, a_share]
tools:
  - get_fund_flow
  - get_indicator_snapshot
  - search_stocks
  - agent_get_kline
---

# 全市场短线选股 (market-screener)

## 使用场景

用户询问"今天买什么股""有什么好股票""短线选什么"时使用。不含个股分析。

如果用户指定了具体股票代码，不要调用本技能。

## 执行流程

只调一个函数，拿到文本直接输出：

```python
result = run()
final_answer(result)
```

`run()` 内部已完成：候选获取 → 筛选 → 深入分析 → 格式化。
返回 markdown 文本，直接传给 `final_answer()`。


