# -*- coding: utf-8 -*-
"""
BaseSkill — Skill 层基类 + 默认分析流程。

使用方式（二选一，都是同一个 BaseSkill 体系）：

  方式 1: @skill 装饰器（推荐，简洁）
    @skill(name="xxx", description="...", tools=[...], instructions="...", priority=5)
    class MySkill:
        pass
    → 装饰器自动生成 BaseSkill 子类，使用 instructions 作为 prompt

  方式 2: 直接继承（需要自定义分析逻辑时）
    class MySkill(BaseSkill):
        name = "xxx"
        tools = [...]
        def analyze(self, stock_code, stock_name, context, call_llm, call_tool_fn):
            ...  # 自定义逻辑
"""
from __future__ import annotations

import json
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

    属性（子类或装饰器定义）：
      name: str — 技能唯一标识
      description: str — 技能描述
      tools: list[str] — 依赖的工具名列表
      instructions: str — 给 LLM 的指令（装饰器方式使用）
      priority: int — 优先级（越高越先执行）

    方法：
      run() — 执行 Skill，返回 (SkillReport, EvalNode)
      analyze() — 执行分析（可覆盖，默认走 build_prompt + call_llm + parse）
      algo_analyze() — 纯算法分析（覆盖此方法实现算法逻辑，返回 None 则走 LLM）
      build_prompt() — 构造 prompt（可覆盖）
      call_tool() — 调用工具并自动记录入参出参
    """

    name: str = ""
    description: str = ""
    tools: List[str] = []
    instructions: str = ""   # @skill 装饰器注入的 LLM 指令
    priority: int = 0
    default_weight: float = 1.0  # 出厂权重，无历史数据时 fallback

    def __init__(self):
        pass  # 无实例状态，run() 使用局部变量

    def run(
        self,
        stock_code: str,
        stock_name: str = "",
        context: Dict[str, Any] = None,
        call_llm: Callable = None,
        call_tool_fn: Callable = None,
    ) -> tuple[SkillReport, EvalNode]:
        """执行 Skill，返回 (SkillReport, EvalNode)。

        注意：使用局部状态而非实例变量，避免单例并发问题。

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            context: 上下文（前序 Skill 结果等）
            call_llm: LLM 调用函数 (prompt: str) -> str
            call_tool_fn: 工具调用函数 (tool_name: str, **kwargs) -> Any

        Returns:
            (SkillReport, EvalNode)
        """
        # 局部状态，不污染实例
        tool_calls: List[str] = []
        tool_nodes: List[EvalNode] = []
        missing_data: List[str] = []
        start_time = time.time()

        context = context or {}

        skill_node = EvalNode(
            layer=Layer.SKILL.value,
            name=self.name,
            stock_code=stock_code,
            stock_name=stock_name,
            input_params={"stock_code": stock_code, "stock_name": stock_name, **context},
        )

        try:
            report = self.analyze(
                stock_code=stock_code,
                stock_name=stock_name,
                context=context,
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
                _tool_calls=tool_calls,
                _tool_nodes=tool_nodes,
                _missing_data=missing_data,
            )
        except Exception as e:
            logger.error("[Skill:%s] 执行失败: %s", self.name, e)
            report = SkillReport(
                skill_name=self.name, status="failed", error=str(e),
            )

        # 填充 EvalNode
        elapsed = (time.time() - start_time) * 1000
        skill_node.score = report.score
        skill_node.direction = report.direction
        skill_node.signal = report.signal
        skill_node.confidence = report.confidence
        skill_node.factors = report.factors
        skill_node.analysis = report.analysis
        skill_node.output_data = report.output_data
        skill_node.tools_called = tool_calls
        skill_node.missing_data = missing_data
        skill_node.status = report.status
        skill_node.error = report.error
        skill_node.elapsed_ms = elapsed

        for tool_node in tool_nodes:
            skill_node.add_child(tool_node)

        return report, skill_node

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
    ) -> Optional["SkillReport"]:
        """纯算法分析。返回 SkillReport 则跳过 LLM，返回 None 走 LLM。

        子类覆盖此方法实现算法逻辑。
        默认返回 None（全部走 LLM，向后兼容）。

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            tool_results: 工具返回数据 {tool_name: result}

        Returns:
            SkillReport 或 None
        """
        return None

    def analyze(
        self,
        stock_code: str,
        stock_name: str,
        context: Dict[str, Any],
        call_llm: Callable = None,
        call_tool_fn: Callable = None,
        _tool_calls: List[str] = None,
        _tool_nodes: List[EvalNode] = None,
        _missing_data: List[str] = None,
    ) -> SkillReport:
        """默认分析流程：算法优先，LLM 补位。

        执行顺序：
          1. 调用工具获取数据
          2. algo_analyze（纯算法，0 token）
          3. 若 algo 返回 None → build_prompt + call_llm + parse

        子类可覆盖 analyze() 实现完全自定义逻辑（跳过 algo 和默认流程）。
        """
        if not call_tool_fn:
            return SkillReport(
                skill_name=self.name, status="failed",
                error="call_tool_fn 未提供",
            )

        # Step 1: 调用工具获取数据
        tool_results = {}
        for tool_name in self.tools:
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

        if not tool_results:
            return SkillReport(
                skill_name=self.name, status="missing",
                signal="所有工具均无数据",
                missing_data=self.tools[:],
            )

        # Step 2: 算法引擎（algo 优先，0 token）
        algo_report = self.algo_analyze(stock_code, stock_name, tool_results)
        if algo_report is not None:
            # 算法搞定，跳过 LLM
            algo_report.tools_called = list(tool_results.keys())
            algo_report.missing_data = list(_missing_data or [])
            logger.info("[Skill:%s] 算法分析完成 score=%.1f direction=%s",
                        self.name, algo_report.score, algo_report.direction)
            return algo_report

        # Step 3: LLM 补位（算法无法处理时）
        if not call_llm:
            return SkillReport(
                skill_name=self.name, status="failed",
                error="algo_analyze 返回 None 且 call_llm 未提供",
            )

        prompt = self.build_prompt(
            stock_code, stock_name, context,
            tool_results=tool_results,
        )

        try:
            raw_output = call_llm(prompt)
        except Exception as e:
            return SkillReport(
                skill_name=self.name, status="failed",
                error=f"LLM 调用失败: {e}",
            )

        from app.agent.chain.contract import parse_skill_output, extract_tools_called
        report = parse_skill_output(raw_output, skill_name=self.name)

        extra_tools = extract_tools_called(raw_output)
        if _tool_calls is not None:
            for t in extra_tools:
                if t not in _tool_calls:
                    _tool_calls.append(t)

        if not report.analysis:
            report.analysis = raw_output[:2000]

        return report

    def call_tool(
        self,
        tool_name: str,
        call_tool_fn: Callable,
        _tool_calls: List[str] = None,
        _tool_nodes: List[EvalNode] = None,
        _missing_data: List[str] = None,
        **kwargs,
    ) -> Any:
        """调用工具并自动记录入参出参到 EvalNode 子树。

        注意：返回值是完整数据（给 LLM 分析用），
        但 EvalNode.output_data 只存摘要（1~10 条样本，给回溯/审查用）。
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

            # 存摘要到 EvalNode（1~10条样本 + 元数据），不是全量
            tool_node.output_data = self._summarize_for_storage(result)

            if result is None:
                tool_node.status = Status.MISSING.value
                if _missing_data is not None:
                    _missing_data.append(tool_name)

            if _tool_calls is not None and tool_name not in _tool_calls:
                _tool_calls.append(tool_name)
            if _tool_nodes is not None:
                _tool_nodes.append(tool_node)
            return result

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            tool_node.elapsed_ms = elapsed
            tool_node.status = Status.FAILED.value
            tool_node.error = str(e)

            if _tool_calls is not None and tool_name not in _tool_calls:
                _tool_calls.append(tool_name)
            if _tool_nodes is not None:
                _tool_nodes.append(tool_node)

            logger.warning("[Skill:%s] 工具 %s 调用失败: %s", self.name, tool_name, e)
            return None

    @staticmethod
    def _summarize_for_storage(data: Any, max_items: int = 10) -> Dict[str, Any]:
        """将工具返回数据压缩为摘要，用于 EvalNode 存储。

        规则：
          - dict: 直接保留（已经是摘要结构）
          - list: 取前 max_items 条 + 总数
          - 其他: 转字符串截断
        """
        if data is None:
            return {}
        if isinstance(data, dict):
            # 如果 dict 内含大列表（如 records/stocks/items），截断它们
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

    def build_prompt(
        self,
        stock_code: str,
        stock_name: str,
        context: Dict[str, Any],
        tool_results: Dict[str, Any] = None,
    ) -> str:
        """构造给 LLM 的 prompt。

        优先使用 self.instructions（@skill 装饰器注入），
        否则用 description 生成简单 prompt。

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            context: 上下文（含 previous_results）
            tool_results: 工具返回数据（注入到 prompt 中）
        """
        parts = []

        # 主指令
        if self.instructions:
            parts.append(self.instructions)
        else:
            parts.append(f"请分析 {stock_name or stock_code}（{stock_code}）的{self.description}。")

        # 分析目标
        parts.append(f"\n## 分析目标\n股票: {stock_name or stock_code}（{stock_code}）")

        # 前序 Skill 结果摘要（如果有）
        prev = context.get("previous_results", [])
        if prev:
            parts.append("\n## 前序分析摘要\n")
            for r in prev:
                parts.append(f"- **{r.get('skill', '?')}**: {r.get('direction', '?')} | {r.get('signal', '')}")

        # 工具返回数据（控制总量）
        if tool_results:
            parts.append("\n## 工具返回数据\n")
            total_chars = 0
            max_total = 12000  # 工具数据总量上限
            for tool_name, data in tool_results.items():
                data_str = json.dumps(data, ensure_ascii=False, default=str)
                remaining = max_total - total_chars
                if remaining <= 0:
                    parts.append(f"### {tool_name}\n(数据量超限，已跳过)\n")
                    continue
                if len(data_str) > remaining:
                    data_str = data_str[:remaining] + "...(截断)"
                parts.append(f"### {tool_name}\n```json\n{data_str}\n```\n")
                total_chars += len(data_str)

        parts.append("\n必须调用工具获取真实数据，绝不编造。")

        # Skill 层职责约束 — 只输出数据，不输出建议
        parts.append(
            "\n## ⚠️ 输出规范（必须遵守）\n"
            "你是分析层，不是决策层。只输出JSON，不输出分析文字。\n"
            "✅ 允许: score / direction / confidence / signal / factors（数据事实）\n"
            "❌ 禁止: 操作建议（买入/卖出/持有/观望）/ 冗长分析文字 / action 字段\n"
            "signal 和 factors.value 用最简短的词组，不要写句子。\n"
        )

        return "\n".join(parts)
