# -*- coding: utf-8 -*-
"""
Backtest skill — 策略回测验证专家（A股规则特化）。

负责：策略回测、历史绩效分析。回测必须遵守A股交易规则。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill

logger = logging.getLogger(__name__)


@skill(
    name="backtest_agent",
    description="回测专家。负责执行策略回测、分析历史绩效。回测遵守A股规则（T+1、涨跌停、印花税）。当用户要求回测、验证策略时调用。",
    instructions=(
        "你是A股回测专家。\n\n"
        "回测必须遵守A股规则：\n"
        "- **T+1**：当日买入不能当日卖出\n"
        "- **涨跌停**：不能在涨停价买入、不能在跌停价卖出\n"
        "- **手续费**：佣金万2.5（买卖双向）+ 印花税千一（仅卖出）\n"
        "- **最小单位**：100股（1手）\n"
        "- **停牌处理**：停牌期间不能交易\n\n"
        "回测流程：\n"
        "1. **确认策略** — 用 list_strategies 列出可用策略，get_strategy_detail 查看详情。\n"
        "2. **执行回测** — 用 run_backtest 执行，注意设置合理的回测区间（建议近 6 个月到 1 年）。\n"
        "3. **绩效分析** — 重点分析：\n"
        "   - 胜率 > 50% 才有实战价值\n"
        "   - 盈亏比 > 2:1（平均盈利/平均亏损）\n"
        "   - 最大回撤 < 20%（超过说明风控有问题）\n"
        "   - 夏普比率 > 1（风险调整后收益）\n"
        "   - 收益率 vs 沪深300（超额收益）\n"
        "4. **风险提示** — 回测不等于实盘，注意过拟合风险。\n\n"
        "中短线策略回测建议用 20-60 个交易日区间，不要用太长周期（A股风格切换快）。\n"
        "必须调用工具获取真实数据，绝不编造。"
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
        "list_strategies",
        # run_backtest / get_backtest_history / get_strategy_detail 需要 strategy_id，
        # 由 algo_analyze 先 list_strategies 再逐个调用
        "list_indicators",
        # get_indicator_params 需要 indicator_id，由 algo_analyze 自行调用
    ],
    priority=6,
    default_weight=1.0,
)
class BacktestSkill:
    """回测专家子 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法回测分析。

        流程：
          1. list_strategies → 获取策略列表
          2. 逐个 get_strategy_detail + run_backtest（最多 3 个）
          3. list_indicators → get_indicator_params（补充指标信息）
          4. 汇总回测结果
        """
        call_tool_fn = kwargs.get("call_tool_fn")
        _tool_calls = kwargs.get("_tool_calls")
        _tool_nodes = kwargs.get("_tool_nodes")
        _missing_data = kwargs.get("_missing_data")

        if not call_tool_fn:
            return None  # 降级走 LLM

        factors = []
        best_score = 50.0
        best_strategy = None

        # 获取策略列表
        strategies = tool_results.get("list_strategies", {})
        strat_list = []
        if isinstance(strategies, dict):
            strat_list = strategies.get("strategies", [])
        elif isinstance(strategies, list):
            strat_list = strategies

        if not strat_list:
            return SkillReport(
                skill_name=self.name,
                score=50.0,
                direction="neutral",
                signal="无用户策略",
                confidence=0.0,
                factors=[FactorItem(name="策略", value="无自定义策略", score=50, status="missing")],
                status="ok",
            )

        # 执行回测（最多 3 个策略）
        for strat in strat_list[:3]:
            strat_id = strat.get("id")
            strat_name = strat.get("name", f"策略{strat_id}")
            if not strat_id:
                continue

            # 获取策略详情
            detail = None
            try:
                detail = self.call_tool(
                    "get_strategy_detail", call_tool_fn=call_tool_fn,
                    strategy_id=strat_id,
                    _tool_calls=_tool_calls, _tool_nodes=_tool_nodes, _missing_data=_missing_data,
                )
            except Exception as e:
                logger.warning("[Skill:%s] get_strategy_detail(%s) 失败: %s", self.name, strat_id, e)

            # 执行回测
            bt_result = None
            try:
                bt_result = self.call_tool(
                    "run_backtest", call_tool_fn=call_tool_fn,
                    strategy_id=strat_id, stock_code=stock_code,
                    _tool_calls=_tool_calls, _tool_nodes=_tool_nodes, _missing_data=_missing_data,
                )
            except Exception as e:
                logger.warning("[Skill:%s] run_backtest(%s, %s) 失败: %s", self.name, strat_id, stock_code, e)

            if isinstance(bt_result, dict) and "error" not in bt_result:
                win_rate = bt_result.get("win_rate", 0)
                profit_loss_ratio = bt_result.get("profit_loss_ratio", 0)
                max_drawdown = bt_result.get("max_drawdown", 0)
                sharpe = bt_result.get("sharpe_ratio", 0)

                # 评分逻辑
                score = 50
                if win_rate >= 60 and profit_loss_ratio >= 2:
                    score = 75
                elif win_rate >= 50 and profit_loss_ratio >= 1.5:
                    score = 60
                elif win_rate < 40 or max_drawdown > 30:
                    score = 30

                if score > best_score:
                    best_score = score
                    best_strategy = strat_name

                factors.append(FactorItem(
                    name=f"回测:{strat_name}",
                    value=f"胜率{win_rate:.0%} 盈亏比{profit_loss_ratio:.1f} 回撤{max_drawdown:.0%}",
                    score=score, status="ok",
                ))

        # 获取指标参数信息
        indicators = tool_results.get("list_indicators", {})
        ind_list = []
        if isinstance(indicators, dict):
            ind_list = indicators.get("indicators", [])
        elif isinstance(indicators, list):
            ind_list = indicators

        for ind in ind_list[:2]:
            ind_id = ind.get("id")
            ind_name = ind.get("name", f"指标{ind_id}")
            if not ind_id:
                continue
            try:
                params = self.call_tool(
                    "get_indicator_params", call_tool_fn=call_tool_fn,
                    indicator_id=ind_id,
                    _tool_calls=_tool_calls, _tool_nodes=_tool_nodes, _missing_data=_missing_data,
                )
                if isinstance(params, dict) and params.get("params"):
                    factors.append(FactorItem(
                        name=f"指标:{ind_name}",
                        value=str(params["params"])[:100],
                        score=50, status="ok",
                    ))
            except Exception:
                pass

        if not factors:
            return SkillReport(
                skill_name=self.name, score=50.0, direction="neutral",
                signal="回测未产生结果", confidence=0.1,
                factors=[FactorItem(name="回测", value="无数据", score=50, status="missing")],
                status="ok",
            )

        direction = "bullish" if best_score >= 60 else ("bearish" if best_score <= 40 else "neutral")
        return SkillReport(
            skill_name=self.name, score=float(best_score), direction=direction,
            signal=f"最佳策略:{best_strategy}" if best_strategy else "回测完成",
            confidence=round(min(len(factors) / 3, 1.0), 2),
            factors=factors, status="ok",
        )
