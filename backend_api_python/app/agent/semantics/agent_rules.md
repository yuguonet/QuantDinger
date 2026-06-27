---
description: Agent 执行规范
---

## 执行纪律

1. **必须用 final_answer() 返回结果** — 完成后调用 `final_answer(你的回复)` 结束。
2. **必须调用工具获取真实数据** — 绝不编造数字。
3. **工具失败** — 记录原因，用已有数据继续，不重复调用。关键错误快速退出并在结论中说明。
4. **数据缺失** — 结论中注明"XX数据缺失"，不用想象填补。
5. **中文回答**。

## 输出格式(金融领域,有个股时)

```json
{
  "stock_code": "从工具返回值中获取的真实代码",
  "stock_name": "从工具返回值中获取的真实名称",
  "action": "根据分析结果选择: buy/sell/hold/skip",
  "score": "根据多维度分析计算的真实评分(0-100)",
  "direction": "根据评分判断: bullish/bearish/neutral",
  "confidence": "根据数据完整度判断: high/medium/low",
  "timeframe": "根据分析周期判断: T+1/T+3/T+5/1W/1M",
  "signal": "根据真实数据总结的核心逻辑",
  "analysis": "基于真实数据的完整分析过程"
}
```

### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| action | ✅ | buy=建议买入, sell=建议卖出, hold=持有观望, skip=回避 |
| score | ✅ | 综合评分 0-100，>70 偏多，<30 偏空 |
| direction | ✅ | 与 score 一致 |
| confidence | ✅ | high=多维共振, medium=部分数据, low=数据缺失或矛盾 |
| timeframe | ✅ | 建议持仓周期 |
| signal | ✅ | 一句话总结核心逻辑 |
| analysis |  | 完整分析过程 |
