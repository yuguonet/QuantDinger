---
name: technical_agent
description: "技术面/动量分析。趋势+指标+量价+形态+筹码+流通盘修正。"
tags: [analysis, finance]
priority: 8
default_weight: 1.0
tools: [analyze_trend, get_indicator_snapshot, get_volume_analysis, analyze_pattern, get_chip_distribution, get_realtime_quote]
---

# 技术面/动量分析

## 评分规则

五维加权 + 流通盘修正：

```
基础维度：
  趋势 40%  动量 25%  量价 20%  形态 10%  筹码 5%

流通盘修正（score = 50 + (原始-50) × reliability）：
  超小盘 <30亿  ：趋势0.6  量价0.5  形态0.4  ← 信号不可靠
  小盘 30-100亿 ：趋势0.8  量价0.7  形态0.6
  中盘 100-500亿：趋势1.0  量价1.0  形态1.0  ← 基准
  大盘 500-2000亿：趋势1.05 量价1.1  形态1.1
  超大盘 >2000亿：趋势1.1  量价1.2  形态1.15 ← 信号最可靠
```

## 方向判定

- score >= 60 → bullish
- score <= 40 → bearish
- 其他 → neutral

## 输出格式

标准 SkillReport + float_mcap_yi + float_size_tier。
