# -*- coding: utf-8 -*-
"""
TraceCollector — Agent 执行追踪器。

在 agent 执行过程中自动收集信息，构建 EvalNode 树。
对 agent 透明，agent 不需要知道它的存在。

提取策略：JSON 优先，正则降级。
Agent 被强制输出标准 JSON（instructions + final_answer_checks），
TraceCollector 直接 json.loads 取字段，100% 可靠。
正则匹配仅作 fallback——万一 Agent 违规输出非 JSON 时兜底。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import (
    EvalNode, FactorItem, Layer, SkillReport, Status,
)

logger = logging.getLogger(__name__)


class TraceCollector:
    """Agent 执行过程中的自动追踪器。

    职责：
    1. 拦截 tool_call，自动创建 Tool 层 EvalNode
    2. 拦截 skill 调用，自动创建 Skill 层 EvalNode + SkillReport
    3. Agent 结束时，创建 Chain 层根节点，组装完整 EvalNode 树
    4. 存库
    """

    def __init__(self, session_id: str, user_query: str):
        self.session_id = session_id
        self.user_query = user_query
        self._stock_code = ""
        self._stock_name = ""
        self.tool_nodes: List[EvalNode] = []
        self.skill_nodes: List[EvalNode] = []
        self.skill_reports: List[SkillReport] = []
        self.start_time = time.time()
        self.intent_verb = ""
        self.intent_noun = ""
        self.domain = ""

    # ── stock_code / stock_name 属性，自动规范化 dict → str ──
    @property
    def stock_code(self) -> str:
        return self._stock_code

    @stock_code.setter
    def stock_code(self, value):
        if isinstance(value, dict):
            results = value.get("results", [])
            self._stock_code = results[0]["code"] if results else ""
        elif value is not None:
            self._stock_code = str(value).strip()
        else:
            self._stock_code = ""

    @property
    def stock_name(self) -> str:
        return self._stock_name

    @stock_name.setter
    def stock_name(self, value):
        if isinstance(value, dict):
            results = value.get("results", [])
            self._stock_name = results[0]["name"] if results else ""
        elif value is not None:
            self._stock_name = str(value).strip()
        else:
            self._stock_name = ""

    # ── 回调方法 ──────────────────────────────────────────────

    @staticmethod
    def _summarize_for_storage(data: Any, max_items: int = 10) -> dict:
        """将工具返回数据压缩为摘要（原 BaseSkill._summarize_for_storage）。"""
        if data is None:
            return {}
        if isinstance(data, dict):
            summary = {}
            for k, v in data.items():
                if isinstance(v, list) and len(v) > max_items:
                    summary[k] = v[:max_items]
                    summary[f"{k}_total"] = len(v)
                else:
                    summary[k] = v
            return summary
        if isinstance(data, list):
            return {"items": data[:max_items], "total": len(data)}
        return {"raw": str(data)[:1000]}

    def on_tool_call(self, tool_name: str, arguments: dict, result: Any,
                     elapsed_ms: float, error: str = None):
        """普通工具调用回调。由 TracedTool 自动触发。"""
        node = EvalNode(
            layer=Layer.TOOL.value,
            name=tool_name,
            input_params=arguments,
            output_data=self._summarize_for_storage(result),
            elapsed_ms=elapsed_ms,
            status=Status.FAILED.value if error else Status.OK.value,
            error=error or "",
        )
        # 自动提取 stock_code
        if not self.stock_code:
            for key in ("stock_code", "stock", "symbol", "code"):
                if key in arguments and arguments[key]:
                    self.stock_code = str(arguments[key])
                    break
        self.tool_nodes.append(node)

    def on_skill_call(self, skill_name: str, report: SkillReport,
                      skill_node: EvalNode):
        """Skill 调用回调。"""
        self.skill_nodes.append(skill_node)
        self.skill_reports.append(report)

    def on_agent_finish(self, final_answer: str, total_steps: int,
                        total_tokens: int, model: str) -> EvalNode:
        """Agent 结束，构建完整 EvalNode 树并存库。"""
        # 从 JSON 提取结构化数据（优先），正则 fallback
        extracted = self._extract_from_json(final_answer)

        # 构建根节点
        root = EvalNode(
            layer=Layer.CHAIN.value,
            name=f"{self.intent_verb}+{self.intent_noun}" if self.intent_verb else "agent",
            exec_date=date.today(),
            stock_code=self.stock_code or extracted.get("stock_code", ""),
            stock_name=self.stock_name or extracted.get("stock_name", ""),
            input_params={"user_query": self.user_query},
            analysis=extracted.get("analysis", final_answer[:2000]),
            # 从 JSON 直接取
            score=extracted.get("score"),
            direction=extracted.get("direction", ""),
            action=extracted.get("action", ""),
            signal=extracted.get("signal", ""),
            confidence=(
                extracted.get("confidence")
                if isinstance(extracted.get("confidence"), (int, float))
                else {"high": 0.8, "medium": 0.5, "low": 0.3}.get(
                    extracted.get("confidence", ""), 0.5
                )
            ),
            timeframe=extracted.get("timeframe", ""),
        )

        # 挂载 skill 节点
        for skill_node in self.skill_nodes:
            root.add_child(skill_node)

        # 挂载不属于任何 skill 的工具节点（agent 直接调用的）
        skill_tool_names = set()
        for sn in self.skill_nodes:
            skill_tool_names.update(sn.tools_called)
        orphan_tools = [
            tn for tn in self.tool_nodes if tn.name not in skill_tool_names
        ]
        for tool_node in orphan_tools:
            root.add_child(tool_node)

        # JSON 提取失败时，降级到正则提取（fallback）
        if root.score is None:
            root.score = self._extract_score_from_answer(final_answer)
        if not root.direction:
            root.direction = self._extract_direction_from_answer(final_answer)
        if not root.action:
            root.action = self._extract_action_from_answer(final_answer)
        if not root.signal:
            root.signal = extracted.get("signal", "")
        if root.confidence is None:
            root.confidence = self._extract_confidence_from_answer(final_answer)

        root.elapsed_ms = (time.time() - self.start_time) * 1000

        # 存库
        from app.agent.chain import store
        execution_id = store.save_tree(root)
        root.id = execution_id

        return root

    # ── JSON 提取（主路径）────────────────────────────────────

    def _extract_from_json(self, answer: str) -> dict:
        """优先从 Agent 输出的 JSON 中直接提取字段（100% 可靠）。

        返回 dict，字段缺失时返回空 dict（触发 fallback）。
        """
        from app.agent.json_extractor import extract_decision
        result = extract_decision(answer)
        return result if result else {}

    # ── 正则 fallback（降级路径）───────────────────────────────

    def _extract_score_from_answer(self, answer: str) -> float:
        """从 agent 最终回复中提取评分。

        优先级：JSON 直接取 > 正则 fallback > 从 action 推断
        """
        data = self._extract_from_json(answer)
        if data and "score" in data:
            return max(0, min(100, float(data["score"])))

        m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
        if m:
            return max(0, min(100, float(m.group(1))))

        action = self._extract_action_from_answer(answer)
        return {"buy": 70, "sell": 30, "hold": 50, "skip": 20}.get(action, 50)

    def _extract_direction_from_answer(self, answer: str) -> str:
        data = self._extract_from_json(answer)
        if data and "direction" in data:
            d = data["direction"]
            if d in ("bullish", "bearish", "neutral"):
                return d

        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "看多", "bullish", "建议买"]):
            return "bullish"
        if any(kw in answer_lower for kw in ["卖出", "sell", "看空", "bearish", "建议卖"]):
            return "bearish"
        return "neutral"

    def _extract_action_from_answer(self, answer: str) -> str:
        data = self._extract_from_json(answer)
        if data and "action" in data:
            a = data["action"]
            if a in ("buy", "sell", "hold", "skip"):
                return a

        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "建议买"]):
            return "buy"
        if any(kw in answer_lower for kw in ["卖出", "sell", "建议卖"]):
            return "sell"
        if any(kw in answer_lower for kw in ["跳过", "skip", "回避"]):
            return "skip"
        return "hold"

    def _extract_confidence_from_answer(self, answer: str) -> float:
        data = self._extract_from_json(answer)
        if data and "confidence" in data:
            c = data["confidence"]
            if isinstance(c, (int, float)):
                return max(0.0, min(1.0, float(c)))
            if isinstance(c, str):
                return {"high": 0.8, "medium": 0.5, "low": 0.3}.get(c, 0.5)

        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["高度确信", "非常确定", "high confidence"]):
            return 0.8
        if any(kw in answer_lower for kw in ["不太确定", "有风险", "low confidence"]):
            return 0.3
        return 0.5


# BaseSkill 已移除，_lazy_base_skill 不再需要
