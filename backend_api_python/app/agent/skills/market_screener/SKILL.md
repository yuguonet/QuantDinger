---
name: market_screener
description: "短线选股首选。全市场扫描，不需要 stock_code。盘中/尾盘/盘后三套策略自动切换。"
tags: [screener, finance]
priority: 5
default_weight: 1.0
tools: [search_stocks, get_market_overview, get_hot_rank, get_sector_fund_flow]
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "🎯"}}
---

# 全市场短线选股

当用户问"有什么好股票"、"推荐股票"、"短线机会"时，用此技能。不需要 stock_code。

## 用法

```bash
python {baseDir}/run.py [stock_code] [--name <stock_name>]
```
