---
name: intelligence-agent
description: "个股情报+政策面。新闻/事件/舆情/解禁/减持/质押，RMS评分+一票否决。"
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "emoji": "🕵️"}}
---

# 个股情报 + 政策面分析

当需要分析新闻、事件驱动、政策影响、舆情、公告解读、解禁减持质押时，用此技能。

## 用法

```bash
python {baseDir}/run.py <stock_code> [--name <stock_name>]
```

## 输出

JSON 格式，包含：
- score: 综合评分 (0-100)
- direction: bullish / bearish / neutral
- veto: 是否一票否决
- stock_score: 个股情报分 (5分制, -5~+5)
- policy_score: 政策面分 (5分制, -5~+5)
- stock_signals: 个股信号列表 (1-20字 + 日期)
- policy_signals: 政策信号列表 (1-20字 + 日期)

## 评分规则

- 个股情报（权重 0.7）：search_stock_intel() → composite_score()（RMS + 时间衰减 + 一票否决）
- 政策面（权重 0.3）：search_policy_intel() → composite_score()
- 只输出有实质影响的内容（|score| > 3），中性不显示
- 一票否决：score=-999 → veto=True, stock_score=-5.0
- 否决源头：⚠否决:某某公司大额减持(6月18日)

## A 股特性

- 弱有效市场，信息不对称是核心 alpha 来源
- 政策驱动明显，产业政策影响巨大
- 新闻时效性极强，盘中消息 > 盘后消息
