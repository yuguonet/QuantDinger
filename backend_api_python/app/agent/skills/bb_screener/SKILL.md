---
name: bb_screener
description: "BB超卖全市场扫描。布林带下轨突破策略筛选全市场，再对候选股做技术面深入分析。"
tags: [screener, finance]
priority: 3
default_weight: 0.8
tools: [search_stocks, analyze_trend, get_indicator_snapshot]
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "📉"}}
---

# BB 超卖扫描

当需要全市场扫描 BB 超卖信号时，用此技能。

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```
