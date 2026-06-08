# -*- coding: utf-8 -*-
"""
Trading skill — 交易执行专家。

负责：策略启停、持仓管理、交易执行、交易记录查询。


## 输出格式（必须遵守）

你的 final_answer 必须包含以下JSON结构（嵌在正文中即可）：

```json
{
  "direction": "bullish/bearish/neutral",
  "confidence": 0.0-1.0,
  "score": 0-100,
  "signal": "一句话信号摘要",
  "factors": [
    {"name": "因子名", "value": "值", "score": 0-100, "status": "ok"}
  ]
}
```

规则：
- score: 0=极度看空, 50=中性, 100=极度看多。基于数据客观打分。
- confidence: 数据充分程度（0=完全没数据, 1=数据非常充分）。不是方向确定性。
- direction: 基于score判断。score>=60=bullish, score<=40=bearish, 其余=neutral。
- status: ok=有数据, missing=数据缺失。缺失的因子必须标missing，不能编造。
- signal: 一句话总结关键信号。
- factors: 每个分析维度一行。包含你调用工具获取的所有关键数据点。
"""
from app.agent.skills.registry import skill


@skill(
    name="trading_agent",
    description="交易执行专家。负责策略启动/停止、持仓管理、交易记录查询。当用户要求启动策略、停止策略、查看持仓、执行交易时调用。",
    instructions="你是交易执行专家。启动策略前必须先确认行情和信号状态。先用 list_strategies 列出可用策略，get_strategy_detail 查看详情，确认后再 start_strategy。停止策略用 stop_strategy。始终优先考虑风险控制。",
    tools=[
        "list_strategies", "get_strategy_detail",
        "start_strategy", "stop_strategy",
        "get_strategy_trades",
        "get_realtime_quote",
    ],
    priority=5,
    default_weight=1.0,
)
class TradingSkill:
    """交易执行专家子 Agent。"""
    pass
