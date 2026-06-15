# -*- coding: utf-8 -*-
"""
Screening skill — 选股专家（A股动量+概念筛选特化）。

负责：条件选股、动量筛选、概念选股、龙虎榜、涨停池、热榜。
A股选股核心：先看概念热度和资金方向，再用技术指标验证。
"""
import logging
from typing import Any, Dict, List, Optional

from app.agent.skills.registry import skill

logger = logging.getLogger(__name__)

# review_stocks_with_indicator 需要 stock_codes(list) + indicator_id，
# 不能在通用 analyze() 中用 stock_code 单参调用，需跳过。
_TOOLS_NEED_SPECIAL = {"review_stocks_with_indicator"}


@skill("screening_agent", auto_load=True)
class ScreeningSkill:
    """选股专家子 Agent。"""

    def analyze(
        self,
        stock_code: str,
        stock_name: str,
        context: Dict[str, Any],
        call_llm=None,
        call_tool_fn=None,
        _tool_calls=None,
        _tool_nodes=None,
        _missing_data=None,
        **kwargs,
    ):
        """覆盖 base.analyze()：跳过需要特殊参数的工具。

        base.analyze() 盲调 self.tools 全部工具，但
        review_stocks_with_indicator 需要 stock_codes(list)+indicator_id，
        无法用 stock_code 单参调用，由 LLM 按需自行调用。
        """
        if not call_tool_fn:
            from app.agent.chain.schema import SkillReport
            return SkillReport(
                skill_name=self.name, status="failed",
                error="call_tool_fn 未提供",
            )

        # Step 1: 调用可直接调的工具
        tool_results = {}
        for tool_name in self.tools:
            if tool_name in _TOOLS_NEED_SPECIAL:
                continue  # 跳过，由 LLM 或后续逻辑处理
            try:
                result = self.call_tool(
                    tool_name=tool_name,
                    call_tool_fn=call_tool_fn,
                    stock_code=stock_code,
                    _tool_calls=_tool_calls,
                    _tool_nodes=_tool_nodes,
                    _missing_data=_missing_data,
                )
                if result is not None:
                    tool_results[tool_name] = result
            except Exception as e:
                logger.warning("[Skill:%s] 工具 %s 调用失败: %s", self.name, tool_name, e)

        # Step 2: algo_analyze（如有）
        algo_report = self.algo_analyze(
            stock_code, stock_name, tool_results,
            call_tool_fn=call_tool_fn,
            _tool_calls=_tool_calls,
            _tool_nodes=_tool_nodes,
            _missing_data=_missing_data,
        )
        if algo_report is not None:
            return algo_report

        # Step 3: LLM 补位
        from app.agent.chain.schema import SkillReport
        prompt = self.build_prompt(stock_code, stock_name, context, tool_results)
        if not prompt or not call_llm:
            return SkillReport(
                skill_name=self.name, status="missing",
                signal="无数据且无 LLM",
                missing_data=list(tool_results.keys()) or self.tools[:],
            )

        try:
            llm_text = call_llm(prompt)
            return self.parse_output(stock_code, stock_name, llm_text, tool_results)
        except Exception as e:
            logger.warning("[Skill:%s] LLM 调用失败: %s", self.name, e)
            return SkillReport(
                skill_name=self.name, status="failed",
                signal=f"LLM 调用失败: {e}",
            )
