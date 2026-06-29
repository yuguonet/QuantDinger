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

**最终输出时**，`final_answer` 必须套以下 JSON 壳。内部循环中间步骤不套壳。

```json
{
  "reply": "用户看到的回复（自然语言）",
  "conclusion": true,
  "errors": [],
  "data": {}
}
```

- `reply`: 面向用户的中文回复，必须是非空字符串
- `conclusion`: true=分析完成可以结束, false=还需要更多数据继续执行。缺省为 true（结束）
- `errors`: 执行过程中 tool/skill 发生的错误列表，无错误时为空数组 `[]`
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

## 评分引用规则

工具返回的 evaluation/评分是算法预评，可直接引用。highlights/warnings 提示异常时需关注。
