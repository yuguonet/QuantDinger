---
name: stock-analyzer
description: 个股分析标准化输出格式。用户指定股票代码或名称时，分析结果必须按此格式输出。
tags: [finance, stock, analysis]
tools:
  - agent_get_kline
  - analyze_trend
  - get_indicator_snapshot
  - get_volume_analysis
  - analyze_pattern
  - get_chip_distribution
  - get_realtime_quote
  - get_stock_info
  - get_fund_flow
  - search_stock_intel
  - get_consensus_eps
---

# 个股分析输出格式

## 使用场景

用户指定股票代码或名称（"怎么样""能买吗""分析一下"等）时，`final_answer()` 必须包含以下 JSON。

## 输出格式

```json
{
  "stock_code": "6位代码",
  "stock_name": "股票名称",
  "action": "buy | sell | hold | skip",
  "score": 0-100,
  "direction": "bullish | bearish | neutral",
  "confidence": "high | medium | low",
  "timeframe": "T+1 | T+3 | T+5 | 1W | 1M",
  "signal": "一句话信号摘要",
  "factors": [
    {"name": "维度名", "score": 0-100, "direction": "bullish | bearish | neutral"}
  ],
  "analysis": "完整分析文字"
}
```

## 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| action | ✅ | buy=建议买入, sell=建议卖出, hold=持有观望, skip=回避 |
| score | ✅ | 综合评分 0-100，>70 偏多，<30 偏空 |
| direction | ✅ | 与 score 一致 |
| confidence | ✅ | high=多维共振, medium=部分数据, low=数据缺失或矛盾 |
| timeframe | ✅ | 建议持仓周期 |
| signal | ✅ | 一句话总结核心逻辑 |
| factors |  | 每个分析维度独立评分（维度名自定义） |
| analysis |  | 完整分析过程 |
