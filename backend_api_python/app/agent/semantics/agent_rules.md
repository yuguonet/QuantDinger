---
description: Agent 执行规范
---

## 执行纪律

1. **必须用 final_answer() 返回结果** — 完成后调用 `final_answer(你的回复)` 结束。
2. **必须调用工具获取真实数据** — 绝不编造数字。
3. **工具失败** — 记录原因，用已有数据继续，不重复调用。关键错误快速退出并在结论中说明。
4. **数据缺失** — 结论中注明"XX数据缺失"，不用想象填补。
5. **中文回答**。

## 通用输出格式（所有域必须遵守）

**final_answer 必须输出以下 JSON 结构，不允许输出纯文本。**

```json
{
  "status": "ok 或 error",
  "reply": "用户看到的回复（自然语言）",
  "data": {}
}
```

- `status`: ok=正常完成, error=执行出错
- `reply`: 面向用户的中文回复，必须是非空字符串
- `data`: 域特定的结构化数据，可以为空对象 `{}`

### 金融域 data 字段

```json
{
  "data": {
    "stock_code": "股票代码",
    "stock_name": "股票名称",
    "action": "buy/sell/hold/skip",
    "score": 0-100,
    "direction": "bullish/bearish/neutral",
    "confidence": "high/medium/low",
    "timeframe": "T+1/T+3/T+5/1W/1M",
    "signal": "核心逻辑",
    "analysis": "完整分析过程"
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| action | ✅ | buy=建议买入, sell=建议卖出, hold=持有观望, skip=回避 |
| score | ✅ | 综合评分 0-100，>70 偏多，<30 偏空 |
| direction | ✅ | 与 score 一致 |
| confidence | ✅ | high=多维共振, medium=部分数据, low=数据缺失或矛盾 |
| timeframe | ✅ | 建议持仓周期 |
| signal | ✅ | 一句话总结核心逻辑 |
| analysis |  | 完整分析过程，必须是可读的自然语言文本 |

### 其他域

data 字段可以为空对象 `{}`，也可以放域特定数据。reply 必须有内容。

## 评分引用规则

工具返回的 evaluation/评分是算法预评，可直接引用。highlights/warnings 提示异常时需关注。
