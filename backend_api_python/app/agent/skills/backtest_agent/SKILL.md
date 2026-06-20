---
name: backtest_agent
description: "回测专家。执行策略回测、分析历史绩效。回测遵守 A 股规则（T+1、涨跌停、印花税）。"
tags: [backtest, finance]
priority: 4
default_weight: 0.8
tools: [run_backtest, get_backtest_result]
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "📈"}}
---

# 回测专家

当需要回测策略、查看历史绩效时，用此技能。

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```
