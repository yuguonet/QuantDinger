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


@skill("backtest_agent", auto_load=True)
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
