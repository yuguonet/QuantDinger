---
name: market-screener
description: "短线选股首选。全市场扫描，不需要 stock_code。盘中/尾盘/盘后三套策略自动切换。"
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "🎯"}}
---

# 全市场短线选股

当用户问"有什么好股票"、"推荐股票"、"短线机会"时，用此技能。不需要 stock_code。

## 用法

```bash
python {baseDir}/run.py [stock_code] [--name <stock_name>]
```

stock_code 可为空。此技能扫描全市场。

## 输出

JSON 格式，包含市场情绪、主线题材、候选股票及深入分析结果。

## 策略自动切换

- **盘中**（9:30-14:30）：连板龙头 + 龙回头弱转强 + 主线题材
- **尾盘**（14:30-15:00）：尾盘异动 + 封板确认
- **盘后**（15:00+）：涨停复盘 + 次日策略

## 选股逻辑

1. Python 预筛选（0 token）— 涨停池/连板/题材/资金流
2. 候选股深入分析 — 技术面 + 情报面
3. 综合评分排序
