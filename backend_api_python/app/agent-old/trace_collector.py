# -*- coding: utf-8 -*-
"""
TraceCollector — Agent 执行追踪器。

在 agent 执行过程中自动收集信息，构建 EvalNode 树。
对 agent 透明，agent 不需要知道它的存在。

树形结构（skill 是 tool 的容器，tool 不包含 skill）：
  chain (根)
  ├── skill: market_screener
  │   ├── tool: search_stocks
  │   └── tool: get_realtime_quote
  ├── skill: stock_analysis
  │   └── tool: get_kline
  └── tool: get_market_overview    ← 无 skill 归属，直接挂 chain

提取策略：JSON 优先，正则降级。
"""
from __future__ import annotations

from app.agent.log import logger
import re
import time
from datetime import date
from typing import Any, List, Optional

from app.agent.chain.schema import (
    EvalNode, Layer, Status,
)
from app.agent.json_extractor import extract_decision
class TraceCollector:
    """Agent 执行过程中的自动追踪器。

    职责：
    1. 通过 begin_skill() 标记当前 skill（由 planner 输出驱动）
    2. 拦截 tool_call，自动归属到当前 skill 或作为 orphan 挂 chain
    3. Agent 结束时，组装完整 EvalNode 树（纯内存）
    4. flush() 由调用方在确认成功后写入 SQL
    """

    def __init__(self, session_id: str, user_query: str):
        self.session_id = session_id
        self.user_query = user_query
        self._stock_code = ""
        self._stock_name = ""
        self.start_time = time.time()
        self.intent_verb = ""
        self.intent_noun = ""
        self.domain = ""
        self._root: Optional[EvalNode] = None

        # ── 统一树结构 ──
        self._current_skill_node: Optional[EvalNode] = None
        self._skill_nodes: List[EvalNode] = []      # 所有 skill 节点
        self._orphan_tools: List[EvalNode] = []     # 无 skill 归属的 tool 节点
        self._all_tools_called: List[str] = []      # 聚合所有工具名（去重保序）

    # ── stock_code / stock_name 属性，自动规范化 dict → str ──

    @property
    def stock_code(self) -> str:
        return self._stock_code

    @stock_code.setter
    def stock_code(self, value):
        if isinstance(value, dict):
            results = value.get("results", [])
            self._stock_code = results[0].get("code", "") if results else ""
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
            self._stock_name = results[0].get("name", "") if results else ""
        elif value is not None:
            self._stock_name = str(value).strip()
        else:
            self._stock_name = ""

    # ── Skill 生命周期（由 planner 输出驱动）──────────────────

    def begin_skill(self, skill_name: str, tools: List[str] = None):
        """标记当前开始执行的 skill。

        由 planner_node 输出 current_skill 后，在 agent_node 创建 agent 前调用。
        后续所有 tool_call 自动归属到此 skill，直到 begin_skill 再次调用或
        agent 结束。
        """
        node = EvalNode(
            layer=Layer.SKILL.value,
            name=skill_name,
        )
        self._current_skill_node = node
        self._skill_nodes.append(node)
        logger.debug("[Trace] begin_skill: %s, tools=%s", skill_name, tools)

    def end_skill(self):
        """标记当前 skill 执行结束。"""
        self._current_skill_node = None

    # ── Tool 调用回调（由 TracedTool 自动触发）─────────────────

    @staticmethod
    def _summarize_for_storage(data: Any, max_items: int = 10) -> dict:
        """将工具返回数据压缩为摘要。"""
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
        """工具调用回调。自动归属到当前 skill 或作为 orphan。"""
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
        if not self._stock_code:
            for key in ("stock_code", "stock", "symbol", "code"):
                if key in arguments and arguments[key]:
                    self._stock_code = str(arguments[key])
                    break

        # 归属到当前 skill 或 orphan
        if self._current_skill_node:
            self._current_skill_node.add_child(node)
            if tool_name not in self._current_skill_node.tools_called:
                self._current_skill_node.tools_called.append(tool_name)
        else:
            self._orphan_tools.append(node)

        # 聚合工具名
        if tool_name not in self._all_tools_called:
            self._all_tools_called.append(tool_name)

    # ── Agent 结束，组装树并存库 ─────────────────────────────

    def on_agent_finish(self, final_answer: str, total_steps: int,
                        total_tokens: int, model: str) -> EvalNode:
        """Agent 结束，构建完整 EvalNode 树并存库。"""
        # 防空
        if not final_answer:
            final_answer = ""

        # 只解析一次 JSON
        extracted = self._extract_from_json(final_answer)

        # 从 extracted 取值，缺失则正则 fallback
        score = extracted.get("score")
        if score is not None:
            score = max(0, min(100, float(score)))
        else:
            score = self._extract_score_from_regex(final_answer)

        direction = extracted.get("direction", "")
        if not direction:
            direction = self._extract_direction_from_text(final_answer)

        action = extracted.get("action", "")
        if not action:
            action = self._extract_action_from_text(final_answer)

        signal = extracted.get("signal", "")
        if not signal:
            signal = self._extract_signal_from_regex(final_answer)

        confidence = extracted.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence = max(0.0, min(1.0, float(confidence)))
        elif isinstance(confidence, str):
            confidence = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(confidence, 0.5)
        elif confidence is None:
            confidence = self._extract_confidence_from_text(final_answer)

        # 构建根节点
        root = EvalNode(
            layer=Layer.CHAIN.value,
            name=f"{self.domain}+{self.intent_verb}+{self.intent_noun}" if self.intent_verb else "agent",
            exec_date=date.today(),
            stock_code=self._stock_code or extracted.get("stock_code", ""),
            stock_name=self._stock_name or extracted.get("stock_name", ""),
            input_params={"user_query": self.user_query},
            analysis=extracted.get("analysis", final_answer[:2000]),
            score=score,
            direction=direction,
            action=action,
            signal=signal,
            confidence=confidence,
            timeframe=extracted.get("timeframe", ""),
            elapsed_ms=(time.time() - self.start_time) * 1000,
        )

        # 挂载 skill 节点（每个 skill 已包含其 tool 子节点）
        for skill_node in self._skill_nodes:
            root.add_child(skill_node)

        # 挂载无 skill 归属的 tool 节点
        for tool_node in self._orphan_tools:
            root.add_child(tool_node)

        # 聚合 tools_called（缓存查询依赖此字段）
        root.tools_called = list(self._all_tools_called)

        self._root = root
        return root

    def flush(self) -> Optional[int]:
        """将组装好的 EvalNode 树写入 SQL。调用方在确认成功后调用。"""
        if not self._root:
            return None
        from app.agent.chain import store
        execution_id = store.save_tree(self._root)
        self._root.id = execution_id
        return execution_id

    # ── JSON 提取（主路径）────────────────────────────────────

    @staticmethod
    def _extract_from_json(answer: str) -> dict:
        """从 Agent 输出的 JSON 中提取字段。返回空 dict 表示未命中。"""
        result = extract_decision(answer)
        return result if result else {}

    # ── 正则 fallback（降级路径）───────────────────────────────

    @staticmethod
    def _extract_score_from_regex(answer: str) -> Optional[float]:
        m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
        if m:
            return max(0, min(100, float(m.group(1))))
        return None

    @staticmethod
    def _extract_direction_from_text(answer: str) -> str:
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "看多", "bullish", "建议买"]):
            return "bullish"
        if any(kw in answer_lower for kw in ["卖出", "sell", "看空", "bearish", "建议卖"]):
            return "bearish"
        return "neutral"

    @staticmethod
    def _extract_action_from_text(answer: str) -> str:
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["买入", "buy", "建议买"]):
            return "buy"
        if any(kw in answer_lower for kw in ["卖出", "sell", "建议卖"]):
            return "sell"
        if any(kw in answer_lower for kw in ["跳过", "skip", "回避"]):
            return "skip"
        return "hold"

    @staticmethod
    def _extract_signal_from_regex(answer: str) -> str:
        m = re.search(r'(?:signal|信号)[：:\s]*(.+?)(?:\n|$)', answer, re.I)
        if m:
            return m.group(1).strip()[:200]
        return ""

    @staticmethod
    def _extract_confidence_from_text(answer: str) -> float:
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["高度确信", "非常确定", "high confidence"]):
            return 0.8
        if any(kw in answer_lower for kw in ["不太确定", "有风险", "low confidence"]):
            return 0.3
        return 0.5
