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


@skill(
    name="indicator_agent",
    description=(
        "指标策略执行专家。从指标 IDE 加载用户自定义策略代码，"
        "对目标股票执行指标计算，提取 buy/sell 交易信号。"
        "当需要验证用户自定义指标信号、或用户提到某个指标策略时调用。"
    ),
    instructions=(
        "你是指标策略执行专家。你的职责是：\n\n"
        "## 工作流程\n\n"
        "1. **加载用户指标** — 调用 `list_indicators` 获取用户的所有指标策略列表。\n"
        "2. **选择相关指标** — 如果用户指定了指标 ID，直接用；否则从列表中选择最近创建的、"
        "或与当前分析场景相关的指标（通常 1~3 个就够了，不需要全跑）。\n"
        "3. **执行指标** — 对目标股票调用 `run_indicator_signal`，传入指标 ID 和股票代码。\n"
        "4. **汇总信号** — 把每个指标的 buy/sell 信号、当前价格、信号状态整理成简洁报告。\n\n"
        "## 输出格式\n\n"
        "对每个执行的指标，报告：\n"
        "- 指标名称\n"
        "- 信号状态（买入/卖出/无信号）\n"
        "- 当前价格 vs 信号价格\n"
        "- 最近的关键信号点\n\n"
        "## 注意\n\n"
        "- 用户通常有 5~10 个指标，不需要全部执行，选择最相关的即可\n"
        "- 如果某个指标执行失败，跳过它，不要卡住\n"
        "- 重点关注最近一根 K 线是否有信号（即当前是否触发）\n"
        "- 你的输出会被后续的选股、回测、辩论步骤参考\n"
        "\n\n## 输出格式（必须遵守）\n"
        "你的 final_answer 必须包含以下JSON结构（嵌在正文中即可）：\n"
        "\n"
        "```json\n"
        "{\n"
        "  \"direction\": \"bullish/bearish/neutral\",\n"
        "  \"confidence\": 0.0-1.0,\n"
        "  \"score\": 0-100,\n"
        "  \"signal\": \"一句话信号摘要\",\n"
        "  \"factors\": [\n"
        "    {\"name\": \"因子名\", \"value\": \"值\", \"score\": 0-100, \"status\": \"ok\"}\n"
        "  ]\n"
        "}\n"
        "```\n"
        "\n"
        "规则：\n"
        "- score: 0=极度看空, 50=中性, 100=极度看多。基于数据客观打分。\n"
        "- confidence: 数据充分程度（0=完全没数据, 1=数据非常充分）。不是方向确定性。\n"
        "- direction: 基于score判断。score>=60=bullish, score<=40=bearish, 其余=neutral。\n"
        "- status: ok=有数据, missing=数据缺失。缺失的因子必须标missing，不能编造。\n"
        "- signal: 一句话总结关键信号。\n"
        "- factors: 每个分析维度一行。包含你调用工具获取的所有关键数据点。",
    ),
    tools=[
        "list_indicators",
        # get_indicator_params / run_indicator_signal 由 algo_analyze 自行调用
        #（需要先 list_indicators 获取 indicator_id）
    ],
    priority=7,
    default_weight=1.1,
)
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
