---
name: indicator_agent
description: "指标策略执行。最近5天有买卖点评分，历史胜率加权，延迟信号衰减。"
tags: [indicator, finance]
priority: 6
default_weight: 0.8
tools: [run_indicator_signal, list_indicators, get_indicator_params]
---

# 指标策略执行

## 评分规则

### 核心：有信号才评分

- 最近5天有买卖点 → 按信号方向评分（含衰减）
- 无近期信号 → 50分观望

### 评分公式

```
base_score = 60（有信号起步）
if trades >= 5:
    胜率 >= 70% → score=80, conf=0.9
    胜率 >= 60% → score=70, conf=0.75
    胜率 >= 50% → score=65, conf=0.6
    胜率 >= 40% → score=55, conf=0.45
    胜率 < 40%  → score=45, conf=0.3

收益加成: +20%→+10分, +10%→+5分, -10%→-10分
盈亏比加成: >=2.0→conf+0.1, >=1.5→conf+0.05
```

### 衰减机制

```
延迟信号: 今天=1.0, 昨天=0.95, 前天=0.90, 3天前=0.85, 4天前=0.80
价格偏差: >10%→×0.6, >5%→×0.8
买卖冲突: 5天内同时有买卖→×0.5

score *= decay
conf  *= decay
```

### 买卖对称

买入和卖出使用完全相同的评分逻辑。

## 数据来源

- 沙箱执行：indicator_analyzer.analyze_indicator()（300根K线）
- 实时信号：indicator_tools.run_indicator_signal()（last5_buy/last5_sell/last5_close）
- 回测数据：_lightweight_backtest()（胜率/收益/盈亏比/最大回撤）
