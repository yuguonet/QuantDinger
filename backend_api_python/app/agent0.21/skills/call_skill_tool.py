# -*- coding: utf-8 -*-
"""
CallSkillTool — 统一技能调用工具。

替代 managed_agents 机制，让 smolagents Agent 能调用 BaseSkill。
所有 Skill 调用都走 BaseSkill.run()，保证结构化输出。

使用方式：
  call_skill = CallSkillTool(model=smol_model, user_id=user_id)
  tools.append(call_skill)

Agent 调用示例：
  result = call_skill(skill_name="technical_agent", stock_code="600519", stock_name="贵州茅台")
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from smolagents import Tool

logger = logging.getLogger(__name__)


class CallSkillTool(Tool):
    """统一技能调用工具。"""

    name = "call_skill"
    description = (
        "调用分析技能对股票进行专业分析。传入技能名和股票代码，"
        "返回结构化分析报告（评分/方向/信号/因子明细）。"
        "适用于需要专业维度分析时，如技术面、动量、情报、政策等。"
    )
    inputs = {
        "skill_name": {
            "type": "string",
            "description": (
                "技能名。可选值: "
                "technical_agent(技术面+动量), indicator_agent(指标), "
                "bb_screener(BB超卖全市场扫描+深入分析), "
                "intelligence_agent(情报+政策), hot_money_tracker(游资), "
                "lockup_watcher(解禁), market_data_agent(行情+概念+资金), "
                "screening_agent(选股), backtest_agent(回测), "
                "bull_researcher(多头), bear_researcher(空头), "
                "data_agent(数据工程), trading_agent(交易)"
            ),
        },
        "stock_code": {
            "type": "string",
            "description": "股票代码，如 600519、000858",
        },
        "stock_name": {
            "type": "string",
            "description": "股票名称（可选），如 贵州茅台",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, model, user_id: int = 1, collector=None):
        super().__init__()
        self._model = model
        self._user_id = user_id
        self._collector = collector  # TraceCollector（可选，金融领域注入）

    def forward(self, skill_name: str, stock_code: str, stock_name: str = None) -> str:
        """调用指定的 BaseSkill，返回结构化报告。"""
        from app.agent.skills.registry import skill_registry
        from app.agent.tool_adapter import build_all_tools

        skill_registry.discover()
        sk = skill_registry.get(skill_name)
        if not sk:
            available = ", ".join(skill_registry.all_names)
            return f"未知技能: {skill_name}。可用技能: {available}"

        # 构建工具集（复用全局缓存）
        all_tools = build_all_tools()
        tool_map = {t.name: t for t in all_tools}

        # 验证 Skill 的工具是否可用
        missing_tools = [t for t in sk.tools if t not in tool_map]
        if missing_tools:
            logger.warning("[CallSkill] Skill %s 缺少工具: %s", skill_name, missing_tools)

        # LLM 调用函数
        def call_llm(prompt: str) -> str:
            messages = [{"role": "user", "content": prompt}]
            response = self._model(messages)
            return response.content if hasattr(response, "content") else str(response)

        # 工具调用函数
        def call_tool_fn(tool_name: str, **kwargs) -> Any:
            t = tool_map.get(tool_name)
            if not t:
                raise ValueError(f"Unknown tool: {tool_name}")
            return t(**kwargs)

        # 执行 Skill（走 BaseSkill.run()）
        try:
            report, eval_node = sk.run(
                stock_code=stock_code,
                stock_name=stock_name or "",
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
            )
        except Exception as e:
            logger.error("[CallSkill] Skill %s 执行异常: %s", skill_name, e)
            return f"技能 {skill_name} 执行失败: {e}"

        # 通知 TraceCollector（如果注入了的话）
        if self._collector:
            self._collector.on_skill_call(skill_name, report, eval_node)

        # 持久化 EvalNode（仅非金融领域，金融领域由 TraceCollector 统一持久化）
        if not self._collector:
            try:
                from app.agent.chain import store as chain_store
                from datetime import date
                from app.agent.chain.schema import EvalNode, Layer

                root = EvalNode(
                    layer=Layer.CHAIN.value,
                    name=f"call_skill+{skill_name}",
                    exec_date=date.today(),
                    stock_code=stock_code,
                    stock_name=stock_name or "",
                    score=report.score,
                    direction=report.direction,
                    action=_score_to_action(report.score),
                    signal=report.signal,
                    confidence=report.confidence,
                )
                root.add_child(eval_node)
                root_id = chain_store.save_tree(root)
                if root_id:
                    logger.info("[CallSkill] 写库成功 root_id=%d skill=%s stock=%s",
                                root_id, skill_name, stock_code)
            except Exception as e:
                logger.warning("[CallSkill] 写库失败（不影响返回）: %s", e)

        # 格式化返回给 Agent
        return self._format_report(report)

    def _format_report(self, report) -> str:
        """将 SkillReport 格式化为 Agent 可读的文本。"""
        lines = [
            f"## {report.skill_name}",
            f"评分: {report.score:.0f} | 方向: {report.direction} | 信号: {report.signal}",
            f"置信: {report.confidence:.2f} | 状态: {report.status}",
        ]

        if report.factors:
            lines.append("### 因子明细")
            for f in report.factors:
                s = f"{f.score:.0f}" if f.score is not None else "—"
                lines.append(f"- {f.name}: {f.value} ({s}分)")

        if report.analysis:
            lines.append(f"\n{report.analysis[:800]}")

        if report.error:
            lines.append(f"\n⚠️ 错误: {report.error}")

        return "\n".join(lines)


def _score_to_action(score: float) -> str:
    """分数 → 决策动作。"""
    if score is None:
        return "hold"
    if score >= 60:
        return "buy"
    if score <= 40:
        return "sell"
    return "hold"
