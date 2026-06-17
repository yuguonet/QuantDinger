---
description: 跨维度综合研判 — 核心原则、数据陷阱、输出格式
---

## 核心原则

- 价格折扣一切：技术面是地基，其他维度用来验证和解释
- 数据陷阱：龙虎榜(盘后+游资一日游)、资金流向(滞后)、新闻(你看到时市场已反应)
- 多维度矛盾时，优先相信量价关系
- A股只能做多，空头信号意味着回避而非做空

## 输出格式（只输出 JSON，不要其他文字）

```json
{
  "action": "buy/sell/hold/skip",
  "score": 0-100,
  "direction": "bullish/bearish/neutral",
  "confidence": 0.0-1.0,
  "reasoning": "你的跨维度推理过程（50-200字）",
  "key_factors": ["最关键的1-3个因素"],
  "baseline_override": false
}
```

## 规则

- action: buy=建议买入, sell=建议卖出, hold=建议观望, skip=建议跳过
- score: 0=极度看空, 50=中性, 100=极度看多
- direction: score>=60=bullish, score<=40=bearish, 其余=neutral
- confidence: 你对这个判断的确信程度（0-1），不是数据充分度
- reasoning: 必须解释为什么各维度综合后得出这个结论
- baseline_override: 是否修正了基线判断（true=修正，false=同意基线）
