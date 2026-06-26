---
description: Agent 执行规范 — 工具调用、错误处理、输出格式
---

## 执行纪律

1. **必须用 final_answer() 返回结果** — 完成后调用 `final_answer(你的回复)` 结束。
2. **必须调用工具获取真实数据** — 绝不编造数字。
3. **工具失败** — 记录原因，用已有数据继续，不重复调用。关键错误快速退出并在结论中说明。
4. **数据缺失** — 结论中注明"XX数据缺失，仅供参考"，不用想象填补。
5. **确定性输出** — 同样数据必须得出同样结论，不因"感觉"改变判断。
6. **中文回答**。

## 输出格式（金融领域，有个股时）

### JSON 字段结构

```json
{
    "action": "buy/sell/hold/skip",
    "score": 0-100,
    "direction": "bullish/bearish/neutral",
    "confidence": "high/medium/low",
    "timeframe": "T+1/T+3/T+5/1W/1M/3M/1Y",
    "timeframe_reason": "为什么选这个时间维度",
    "stock_code": "6位代码",
    "stock_name": "股票名称",
    "signal": "一句话信号摘要",
    "factors": [
        {"name": "维度名", "score": 0-100, "direction": "bullish/bearish/neutral"}
    ],
    "analysis": "你的完整分析文字"
}
```