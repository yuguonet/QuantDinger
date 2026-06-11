# -*- coding: utf-8 -*-
"""
SkillExecutionTool — 子 Agent 内部工具，包装 BaseSkill.run()。

每个子 agent 只有这一个工具，调用它来执行完整的 skill 分析流程。
algo_analyze() 零 token 路径在此保留（通过 BaseSkill.run() → analyze() → algo_analyze()）。

被调用方：
  agent.py → _build_managed_agents() → 每个子 agent 持有一个 SkillExecutionTool

使用示例（子 agent 内部）：
  execute_skill(stock_code="600519", stock_name="贵州茅台")
  → 返回格式化 SkillReport / 结构化 JSON
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from smolagents import Tool

logger = logging.getLogger(__name__)


class SkillExecutionTool(Tool):
    """子 agent 内部工具：执行指定 skill 的完整分析流程。

    构造时绑定一个具体的 BaseSkill 实例，forward() 调用 BaseSkill.run()。
    """

    name = "execute_skill"
    description = (
        "执行技能分析，传入股票代码，返回结构化分析报告。"
        "包含评分、方向、信号、因子明细等字段。"
        "调用此工具后会获取真实数据并完成分析，不需要再调用其他工具。"
    )
    inputs = {
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

    def __init__(self, skill, model, collector=None):
        """
        Args:
            skill: BaseSkill 实例
            model: smolagents Model 实例（用于 call_llm）
            collector: TraceCollector 实例（可选）
        """
        super().__init__()
        self._skill = skill
        self._model = model
        self._collector = collector

    def forward(self, stock_code: str, stock_name: str = None) -> str:
        """执行 skill 分析，返回格式化报告。"""
        from app.agent.skills.registry import skill_registry
        from app.agent.tool_adapter import build_all_tools

        skill_registry.discover()
        sk = skill_registry.get(self._skill.name)
        if not sk:
            return f"技能 {self._skill.name} 不可用"

        # 构建工具集
        all_tools = build_all_tools()
        tool_map = {t.name: t for t in all_tools}

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

        # 执行 Skill（走 BaseSkill.run()，保留 algo_analyze 零 token 路径）
        try:
            report, eval_node = sk.run(
                stock_code=stock_code,
                stock_name=stock_name or "",
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
            )
        except Exception as e:
            logger.error("[SkillExecution] Skill %s 执行异常: %s", self._skill.name, e)
            return json.dumps({
                "skill_name": self._skill.name,
                "status": "failed",
                "error": str(e),
            }, ensure_ascii=False)

        # 通知 TraceCollector
        if self._collector:
            self._collector.on_skill_call(self._skill.name, report, eval_node)

        # 持久化 EvalNode（仅非金融领域）
        if not self._collector:
            try:
                from app.agent.chain import store as chain_store
                from datetime import date
                from app.agent.chain.schema import EvalNode, Layer

                root = EvalNode(
                    layer=Layer.CHAIN.value,
                    name=f"call_skill+{self._skill.name}",
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
                    logger.info("[SkillExecution] 写库成功 root_id=%d skill=%s stock=%s",
                                root_id, self._skill.name, stock_code)
            except Exception as e:
                logger.warning("[SkillExecution] 写库失败（不影响返回）: %s", e)

        # 返回格式化报告
        return _format_report(report)


def _format_report(report) -> str:
    """将 SkillReport 格式化为 JSON 字符串，方便主 agent 解析。"""
    d = {
        "skill_name": report.skill_name,
        "score": report.score,
        "direction": report.direction,
        "signal": report.signal,
        "confidence": report.confidence,
        "status": report.status,
        "factors": [
            {
                "name": f.name,
                "value": f.value,
                "score": f.score,
                "weight": f.weight,
                "status": f.status,
            }
            for f in (report.factors or [])
        ],
        "analysis": report.analysis[:800] if report.analysis else "",
    }
    if report.error:
        d["error"] = report.error
    try:
        return json.dumps(d, ensure_ascii=False)
    except Exception:
        return json.dumps({
            "skill_name": report.skill_name,
            "score": report.score,
            "direction": report.direction,
            "signal": report.signal,
            "status": report.status,
        }, ensure_ascii=False)


def _score_to_action(score: float) -> str:
    """分数 → 决策动作。"""
    if score is None:
        return "hold"
    if score >= 60:
        return "buy"
    if score <= 40:
        return "sell"
    return "hold"
