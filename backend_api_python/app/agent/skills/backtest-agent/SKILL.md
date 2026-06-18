---
name: backtest-agent
description: "回测专家。执行策略回测、分析历史绩效。回测遵守 A 股规则（T+1、涨跌停、印花税）。"
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "📈"}}
---

# 回测专家

当需要回测策略、查看历史绩效时，用此技能。

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```

## 输出

JSON 格式的 SkillReport，包含各策略的回测结果（胜率/盈亏比/回撤/夏普）。

## 回测指标

- 胜率：盈利交易占比
- 盈亏比：平均盈利 / 平均亏损
- 最大回撤：最大亏损幅度
- 夏普比率：风险调整后收益

## A 股规则

- T+1 交易制度
- 涨跌停板限制
- 印花税 + 佣金
