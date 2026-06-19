---
name: intelligence_agent
description: "个股情报+政策面。新闻/事件/舆情/解禁/减持/质押，RMS评分+一票否决。"
tags: [news, finance]
priority: 7
default_weight: 0.8
tools: [search_stock_intel, search_policy_intel]
---

# 个股情报 + 政策面分析

## 评分规则

### 个股情报（权重 0.7）
- 数据源：search_stock_intel()
- 综合评分：composite_score()（RMS 聚合 + 时间衰减 + 一票否决）
- 范围：-5 ~ +5（5分制）

### 政策面（权重 0.3）
- 数据源：search_policy_intel()
- 综合评分：composite_score()
- 范围：-5 ~ +5（5分制）

## 一票否决逻辑

- composite_score() 检测到 score=-999 的文章 → veto=True
- stock_score = -5.0（5分制最低）
- 否决源头插入信号最前面：⚠否决:某某公司大额减持(6月18日)

## 输出规则

- 只输出有实质影响的内容（|score| > 3），中性不显示
- 每条信号 1-20 字 + 日期（M月D日格式）
- 政策利好/利空哪个行业，1-20字说明
- 行业新闻评分和政策评分分开

## 输出格式

标准 SkillReport + veto/stock_score/policy_score/stock_signals/policy_signals。
