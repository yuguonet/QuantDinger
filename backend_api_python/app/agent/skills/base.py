# -*- coding: utf-8 -*-
"""
BaseSkill — Skill 层基类。

职责：
  - 定义 Skill 的标准接口（analyze → SkillReport）
  - call_tool 自动记录入参出参到 EvalNode 子树
  - 从 LLM 输出中解析 SkillReport

使用：
  class MySkill(BaseSkill):
      name = "my_skill"
      description = "..."
      tools = ["tool_a", "tool_b"]

      def build_prompt(self, stock_code, stock_name, context):
          return f"分析 {stock_name} 的..."

      async def analyze(self, stock_code, stock_name, context) -> SkillReport:
          data = await self.call_tool("tool_a", stock_code=stock_code)
          ...
          return SkillReport(...)
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.schema import (
    EvalNode, FactorItem, Layer, SkillReport, Status,
)

logger = logging.getLogger(__name__)


class BaseSkill(ABC):
    """Skill 基类。

    子类必须定义：
      name: str — 技能名（唯一标识）
      description: str — 技能描述
      tools: list[str] — 依赖的工具名列表

    子类可选覆盖：
      build_prompt() — 构造给 LLM 的 prompt
      analyze() — 执行分析（如果覆盖，需自行返回 SkillReport）
    """

    name: str = ""
    description: str = ""
    tools: List[str] = []
    priority: int = 0

    def __init__(self):
        self._tool_calls: List[str] = []
        self._tool_nodes: List[EvalNode] = []
        self._missing_data: List[str] = []
        self._start_time: float = 0.0

    async def run(
        self,
        stock_code: str,
        stock_name: str = "",
        context: Dict[str, Any] = None,
        call_llm: Callable = None,
        call_tool_fn: Callable = None,
    ) -> tuple[SkillReport, EvalNode]:
        """执行 Skill，返回 (SkillReport, EvalNode)。

        EvalNode 包含该 Skill 的完整执行信息及其 tool 子节点。

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            context: 上下文（前序 Skill 结果等）
            call_llm: LLM 调用函数 (prompt: str) -> str
            call_tool_fn: 工具调用函数 (tool_name: str, **kwargs) -> Any

        Returns:
            (SkillReport, EvalNode)
        """
        self._tool_calls = []
        self._tool_nodes = []
        self._missing_data = []
        self._start_time = time.time()

        context = context or {}

        # 创建 Skill 层 EvalNode
        skill_node = EvalNode(
            layer=Layer.SKILL.value,
            name=self.name,
            stock_code=stock_code,
            stock_name=stock_name,
            input_params={"stock_code": stock_code, "stock_name": stock_name, **context},
        )

        try:
            report = await self.analyze(
                stock_code=stock_code,
                stock_name=stock_name,
                context=context,
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
            )
        except Exception as e:
            logger.error("[Skill:%s] 执行失败: %s", self.name, e)
            report = SkillReport(
                skill_name=self.name,
                status="failed",
                error=str(e),
            )

        # 填充 EvalNode
        elapsed = (time.time() - self._start_time) * 1000
        skill_node.score = report.score
        skill_node.direction = report.direction
        skill_node.signal = report.signal
        skill_node.confidence = report.confidence
        skill_node.factors = report.factors
        skill_node.analysis = report.analysis
        skill_node.output_data = report.output_data
        skill_node.tools_called = self._tool_calls
        skill_node.missing_data = self._missing_data
        skill_node.status = report.status
        skill_node.error = report.error
        skill_node.elapsed_ms = elapsed

        # 挂载 tool 子节点
        for tool_node in self._tool_nodes:
            skill_node.add_child(tool_node)

        return report, skill_node

    @abstractmethod
    async def analyze(
        self,
        stock_code: str,
        stock_name: str,
        context: Dict[str, Any],
        call_llm: Callable = None,
        call_tool_fn: Callable = None,
    ) -> SkillReport:
        """执行分析，返回 SkillReport。

        子类实现此方法，使用 self.call_tool() 调用工具。
        """
        ...

    async def call_tool(
        self,
        tool_name: str,
        call_tool_fn: Callable,
        **kwargs,
    ) -> Any:
        """调用工具并自动记录。

        记录入参出参到 EvalNode 子树，供回溯时验证数据准确率。

        Args:
            tool_name: 工具名
            call_tool_fn: 工具调用函数
            **kwargs: 工具参数

        Returns:
            工具返回值
        """
        tool_node = EvalNode(
            layer=Layer.TOOL.value,
            name=tool_name,
            input_params=dict(kwargs),
        )

        t0 = time.time()
        try:
            result = call_tool_fn(tool_name, **kwargs)
            elapsed = (time.time() - t0) * 1000

            tool_node.elapsed_ms = elapsed
            tool_node.status = Status.OK.value

            # 记录输出（工具返回的是原始数据，1~10 条 dict）
            if isinstance(result, (list, dict)):
                tool_node.output_data = result if isinstance(result, dict) else {"items": result}
            elif result is None:
                tool_node.output_data = {}
                tool_node.status = Status.MISSING.value
                self._missing_data.append(tool_name)
            else:
                tool_node.output_data = {"raw": str(result)[:1000]}

            if tool_name not in self._tool_calls:
                self._tool_calls.append(tool_name)

            self._tool_nodes.append(tool_node)
            return result

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            tool_node.elapsed_ms = elapsed
            tool_node.status = Status.FAILED.value
            tool_node.error = str(e)

            if tool_name not in self._tool_calls:
                self._tool_calls.append(tool_name)
            self._tool_nodes.append(tool_node)

            logger.warning("[Skill:%s] 工具 %s 调用失败: %s", self.name, tool_name, e)
            return None

    def build_prompt(self, stock_code: str, stock_name: str, context: Dict[str, Any]) -> str:
        """构造给 LLM 的 prompt。子类可覆盖。"""
        return f"请分析 {stock_name or stock_code}（{stock_code}）的{self.description}。"
