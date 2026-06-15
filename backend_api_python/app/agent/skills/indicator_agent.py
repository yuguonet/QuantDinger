# -*- coding: utf-8 -*-
"""
Indicator Agent — 用户自定义指标策略执行器。

从指标 IDE 中加载用户创建的策略代码，对目标股票执行，
提取 buy/sell 信号，作为链路中的一环供后续步骤参考。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill

logger = logging.getLogger(__name__)


@skill("indicator_agent", auto_load=True)
class IndicatorAgent:
    """用户自定义指标策略执行 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法指标信号分析。

        流程：
          1. list_indicators 获取用户指标列表
          2. 逐个 run_indicator_signal 执行（最多 3 个）
          3. 汇总 buy/sell 信号
        """
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls")
        _tool_nodes = kwargs.get("_tool_nodes")
        _missing_data = kwargs.get("_missing_data")

        factors = []
        signals = []
        buy_count = 0
        sell_count = 0
        total_run = 0

        # 获取指标列表
        indicators = tool_results.get("list_indicators", {})
        indicator_list = []
        if isinstance(indicators, dict):
            indicator_list = indicators.get("indicators", [])
        elif isinstance(indicators, list):
            indicator_list = indicators

        if not indicator_list:
            return SkillReport(
                skill_name=self.name,
                score=50.0,
                direction="neutral",
                signal="无用户自定义指标",
                confidence=0.0,
                factors=[FactorItem(name="指标", value="无自定义指标", score=50, status="missing")],
                status="ok",
            )

        # 执行指标（最多 3 个）
        for ind in indicator_list[:3]:
            ind_id = ind.get("id")
            ind_name = ind.get("name", f"指标{ind_id}")

            if not ind_id:
                continue

            # 自己调 run_indicator_signal，传入正确的 indicator_id
            signal_result = None
            if call_tool_fn:
                try:
                    signal_result = self.call_tool(
                        "run_indicator_signal",
                        call_tool_fn=call_tool_fn,
                        indicator_id=ind_id,
                        stock_code=stock_code,
                        _tool_calls=_tool_calls,
                        _tool_nodes=_tool_nodes,
                        _missing_data=_missing_data,
                    )
                except Exception as e:
                    logger.warning("[Skill:%s] run_indicator_signal(%d, %s) 失败: %s",
                                   self.name, ind_id, stock_code, e)
            else:
                signal_result = tool_results.get("run_indicator_signal", {})

            if isinstance(signal_result, dict) and "error" not in signal_result:
                latest_signal = signal_result.get("latest_signal", "")
                signal_type = signal_result.get("signal_type", "")

                if signal_type == "buy" or "买入" in str(latest_signal):
                    buy_count += 1
                    score = 75
                    signals.append(f"{ind_name}:买入")
                elif signal_type == "sell" or "卖出" in str(latest_signal):
                    sell_count += 1
                    score = 25
                    signals.append(f"{ind_name}:卖出")
                else:
                    score = 50

                total_run += 1
                factors.append(FactorItem(
                    name=ind_name,
                    value=str(latest_signal or "无信号"),
                    score=score,
                    status="ok",
                ))

        if total_run == 0:
            return SkillReport(
                skill_name=self.name,
                score=50.0,
                direction="neutral",
                signal="指标执行未产生结果",
                confidence=0.1,
                factors=[FactorItem(name="指标", value="执行失败", score=50, status="missing")],
                status="ok",
            )

        # 综合评分
        if buy_count > sell_count:
            final_score = 50 + (buy_count - sell_count) * 15
            direction = "bullish"
        elif sell_count > buy_count:
            final_score = 50 - (sell_count - buy_count) * 15
            direction = "bearish"
        else:
            final_score = 50
            direction = "neutral"

        final_score = max(0, min(100, final_score))
        confidence = round(min(total_run / 3, 1.0), 2)

        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无信号"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"执行{total_run}个指标，买入信号{buy_count}个，卖出信号{sell_count}个",
            status="ok",
        )
