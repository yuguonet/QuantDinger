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
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill

logger = logging.getLogger(__name__)


@skill("trading_agent", auto_load=True)
class TradingSkill:
    """交易执行专家子 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法交易状态汇总（只读，不自动执行交易）。

        流程：
          1. list_strategies → 获取策略列表及状态
          2. get_strategy_trades → 最近交易记录（最多 3 个策略）
          3. 汇总策略运行状态
        """
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls")
        _tool_nodes = kwargs.get("_tool_nodes")
        _missing_data = kwargs.get("_missing_data")

        if not call_tool_fn:
            return None

        factors = []
        running_count = 0
        total_count = 0

        # 获取策略列表
        strategies = tool_results.get("list_strategies", {})
        strat_list = []
        if isinstance(strategies, dict):
            strat_list = strategies.get("strategies", [])
        elif isinstance(strategies, list):
            strat_list = strategies

        if not strat_list:
            return SkillReport(
                skill_name=self.name, score=50.0, direction="neutral",
                signal="无交易策略", confidence=0.0,
                factors=[FactorItem(name="策略", value="无策略", score=50, status="missing")],
                status="ok",
            )

        for strat in strat_list[:5]:
            strat_id = strat.get("id")
            strat_name = strat.get("name", f"策略{strat_id}")
            is_running = strat.get("status") == "running" or strat.get("is_running")
            total_count += 1
            if is_running:
                running_count += 1

            # 获取最近交易记录
            try:
                trades = self.call_tool(
                    "get_strategy_trades", call_tool_fn=call_tool_fn,
                    strategy_id=strat_id, limit=5,
                    _tool_calls=_tool_calls, _tool_nodes=_tool_nodes, _missing_data=_missing_data,
                )
                trade_count = 0
                if isinstance(trades, dict):
                    trade_count = len(trades.get("trades", []))
                elif isinstance(trades, list):
                    trade_count = len(trades)
                status_str = "运行中" if is_running else "已停止"
                factors.append(FactorItem(
                    name=f"策略:{strat_name}",
                    value=f"{status_str}, 最近{trade_count}笔交易",
                    score=60 if is_running else 50, status="ok",
                ))
            except Exception as e:
                logger.warning("[Skill:%s] get_strategy_trades(%s) 失败: %s", self.name, strat_id, e)
                factors.append(FactorItem(
                    name=f"策略:{strat_name}",
                    value="交易记录获取失败", score=50, status="missing",
                ))

        score = 50 + running_count * 5  # 运行中的策略越多越积极
        score = min(score, 80)
        direction = "bullish" if score >= 60 else "neutral"

        return SkillReport(
            skill_name=self.name, score=float(score), direction=direction,
            signal=f"{running_count}/{total_count} 策略运行中",
            confidence=round(min(total_count / 3, 1.0), 2),
            factors=factors, status="ok",
        )
