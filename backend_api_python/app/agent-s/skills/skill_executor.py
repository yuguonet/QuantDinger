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
    """统一技能调用工具。

    两种模式：
      1. 绑定单个 skill（skill=实例）— forward(stock_code, stock_name)
      2. 动态选择 skill（skill=None）— forward(skill_name, stock_code, stock_name)
    """

    name = "call_skill"
    description = (
        "调用分析技能对股票进行专业分析。传入技能名和股票代码，"
        "返回结构化分析报告（评分/方向/信号/因子明细）。"
        "适用于需要专业维度分析时，如技术面、动量、情报、政策等。"
    )
    inputs = {
        "skill_name": {
            "type": "string",
            "description": "技能名",
            "nullable": True,
        },
        "stock_code": {
            "type": "string",
            "description": "股票代码，如 600519、000858",
            "nullable": True,
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
            skill: BaseSkill 实例，或 None（动态模式，从 skill_name 参数查找）
            model: smolagents Model 实例（用于 call_llm）
            collector: TraceCollector 实例（可选）
        """
        super().__init__()
        self._skill = skill
        self._model = model
        self._collector = collector

    # 工具集缓存（避免每次 forward() 重建 96 个工具）
    _tool_cache = None
    _tool_cache_time = 0
    _TOOL_CACHE_TTL = 300  # 5 分钟刷新一次

    def forward(self, skill_name: str = "", stock_code: str = "", stock_name: str = None) -> str:
        """执行 skill 分析，返回格式化报告。"""
        import time as _time
        from app.agent.skills.registry import skill_registry
        from app.agent.tool_adapter import build_all_tools

        # 防御
        if stock_name in (None, "None", "null", "undefined"):
            stock_name = ""
        if stock_code in (None, "None", "null", "undefined"):
            stock_code = ""

        skill_registry.discover()

        # 动态模式：从 skill_name 查找；绑定模式：用 self._skill
        if self._skill is not None:
            sk = self._skill
        else:
            sk = skill_registry.get(skill_name)
            if not sk:
                available = ", ".join(skill_registry.all_names)
                return f"未知技能: {skill_name}。可用技能: {available}"

        # 构建工具集（带缓存）
        now = _time.time()
        if SkillExecutionTool._tool_cache is None or (now - SkillExecutionTool._tool_cache_time) > SkillExecutionTool._TOOL_CACHE_TTL:
            SkillExecutionTool._tool_cache = build_all_tools()
            SkillExecutionTool._tool_cache_time = now
        tool_map = {t.name: t for t in SkillExecutionTool._tool_cache}

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
            logger.error("[SkillExecution] Skill %s 执行异常: %s", sk.name, e)
            return json.dumps({
                "skill_name": sk.name,
                "status": "failed",
                "error": str(e),
            }, ensure_ascii=False)

        # 通知 TraceCollector
        if self._collector:
            self._collector.on_skill_call(sk.name, report, eval_node)

        # 持久化 EvalNode（仅非金融领域）
        if not self._collector:
            try:
                from app.agent.chain import store as chain_store
                from datetime import date
                from app.agent.chain.schema import EvalNode, Layer

                root = EvalNode(
                    layer=Layer.CHAIN.value,
                    name=f"call_skill+{sk.name}",
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
                                root_id, sk.name, stock_code)
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
