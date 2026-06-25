# -*- coding: utf-8 -*-
"""
Judge — LLM #4 循环控制器。

职责：
  1. 总结 — 提炼当前步骤的关键结论（一句话）
  2. 纠错 — 发现数据矛盾/缺失，标记修正建议
  3. 控循环 — 判断是否继续下一步，或停止进入最终输出
  4. 最终输出 — 所有步骤结束后，读取 checkpoint 全量数据，输出结构化结果

设计原则：
  - 每步结束后调用 judge_step() → 产出摘要 + 继续/停止决策
  - 循环结束后调用 judge_final() → 产出最终金融分析 JSON
  - 不执行工具，只做判断和总结
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StepJudgeResult:
    """单步判断结果。"""
    continue_loop: bool = True
    summary: str = ""               # 一句话关键结论
    corrections: Optional[str] = None  # 纠错建议（None=无问题）
    next_context: str = ""          # 传给下一轮 Planner 的上下文摘要
    reasoning: str = ""             # 判断理由
    elapsed_ms: float = 0.0


@dataclass
class FinalJudgeResult:
    """最终输出结果。"""
    output: Dict[str, Any] = field(default_factory=dict)  # 结构化金融 JSON
    corrections: Optional[str] = None  # 最终纠错
    need_rerun: bool = False           # 是否需要补跑
    rerun_hint: str = ""               # 补跑提示
    reasoning: str = ""
    elapsed_ms: float = 0.0


class Judge:
    """LLM #4 循环控制器。"""

    def __init__(self, call_llm: Callable[[str], str] = None):
        self._call_llm = call_llm
        self._judge_rules = ""

    def _get_judge_rules(self) -> str:
        """从 semantics/judge.md 加载规则（懒加载）。"""
        if not self._judge_rules:
            try:
                from app.agent.semantics import get_judge_text
                self._judge_rules = get_judge_text()
            except Exception as e:
                logger.debug("[Judge] judge.md 加载失败: %s", e)
        return self._judge_rules

    # ── 单步判断 ──────────────────────────────────────────────

    def judge_step(
        self,
        query: str,
        step_number: int,
        step_description: str,
        step_content: str,
        step_success: bool,
        previous_summaries: List[str],
        step_queue_remaining: int,
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
    ) -> StepJudgeResult:
        """每步结束后调用，判断下一步行为。

        Args:
            query: 用户原始消息
            step_number: 当前步骤编号
            step_description: 当前步骤描述（Planner 给的）
            step_content: Agent 返回的原始数据
            step_success: 步骤是否执行成功
            previous_summaries: 之前步骤的摘要列表（Judge 产出的）
            step_queue_remaining: 剩余步骤数
            intent: IntentResult 对象
            stock_code: 股票代码
            stock_name: 股票名称

        Returns:
            StepJudgeResult
        """
        t0 = time.time()

        if not self._call_llm:
            # LLM 不可用，用简单逻辑兜底
            return StepJudgeResult(
                continue_loop=step_success and step_queue_remaining > 0,
                summary=step_content[:100] if step_content else "执行完成",
                next_context=step_content[:200] if step_content else "",
                reasoning="LLM 不可用，使用简单逻辑",
                elapsed_ms=(time.time() - t0) * 1000,
            )

        try:
            raw = self._llm_judge_step(
                query, step_number, step_description, step_content,
                step_success, previous_summaries, step_queue_remaining,
                intent, stock_code, stock_name,
            )
            result = self._parse_step_judge(raw, step_success, step_queue_remaining)
            result.elapsed_ms = (time.time() - t0) * 1000
            return result
        except Exception as e:
            logger.warning("[Judge] 步骤判断失败: %s", e)
            return StepJudgeResult(
                continue_loop=step_success and step_queue_remaining > 0,
                summary=step_content[:100] if step_content else "执行完成",
                reasoning=f"Judge 异常: {e}",
                elapsed_ms=(time.time() - t0) * 1000,
            )

    def _llm_judge_step(
        self,
        query: str,
        step_number: int,
        step_description: str,
        step_content: str,
        step_success: bool,
        previous_summaries: List[str],
        step_queue_remaining: int,
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
    ) -> str:
        """构建 prompt 并调用 LLM。"""
        # 意图信息
        intent_info = ""
        if intent and (intent.verb or intent.noun or intent.domain):
            intent_info = f"\n意图: verb={intent.verb or '-'}, noun={intent.noun or '-'}, domain={intent.domain or '-'}"

        # 股票信息
        stock_info = ""
        if stock_code:
            stock_info = f"\n标的: {stock_name or '未知'}（{stock_code}）"

        # 前序摘要
        summaries_section = ""
        if previous_summaries:
            summaries_section = "\n## 前序步骤结论\n"
            for i, s in enumerate(previous_summaries, 1):
                summaries_section += f"- 步骤{i}: {s}\n"

        # 当前步骤结果
        content_preview = step_content[:1500] if step_content else "无数据"

        # 从 judge.md 加载规则
        judge_rules = self._get_judge_rules()

        prompt = (
            f"你是量化分析循环控制器。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"{summaries_section}\n"
            f"## 当前步骤（第{step_number}步）\n"
            f"- 描述: {step_description}\n"
            f"- 成功: {step_success}\n"
            f"- 原始数据:\n{content_preview}\n\n"
            f"## 输出格式（只输出 JSON）\n"
            f"```json\n"
            f'{{\n'
            f'  "continue": true/false,\n'
            f'  "summary": "一句话关键结论，30字以内",\n'
            f'  "corrections": null 或 "纠错建议",\n'
            f'  "next_context": "传给下一步的上下文，50字以内",\n'
            f'  "reasoning": "判断理由，20字以内"\n'
            f'}}\n'
            f"```\n\n"
            f"{judge_rules}\n"
        )

        return self._call_llm(prompt)

    def _parse_step_judge(self, raw: str, step_success: bool, step_queue_remaining: int) -> StepJudgeResult:
        """解析 LLM 输出。"""
        import re

        cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r'```json\s*', '', cleaned).strip()
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()

        # 提取 JSON
        brace_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group())
                return StepJudgeResult(
                    continue_loop=bool(data.get("continue", True)),
                    summary=str(data.get("summary", ""))[:100],
                    corrections=data.get("corrections"),
                    next_context=str(data.get("next_context", ""))[:200],
                    reasoning=str(data.get("reasoning", "")),
                )
            except json.JSONDecodeError:
                pass

        # 解析失败，用默认逻辑
        return StepJudgeResult(
            continue_loop=step_success and step_queue_remaining > 0,
            summary="步骤完成（Judge 解析失败）",
            reasoning="JSON 解析失败，使用默认逻辑",
        )

    # ── 最终输出 ──────────────────────────────────────────────

    def judge_final(
        self,
        query: str,
        all_summaries: List[str],
        all_contents: List[str],
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
    ) -> FinalJudgeResult:
        """所有步骤结束后调用，产出最终结构化输出。

        Args:
            query: 用户原始消息
            all_summaries: 所有步骤的摘要列表
            all_contents: 所有步骤的原始数据列表
            intent: IntentResult 对象
            stock_code: 股票代码
            stock_name: 股票名称

        Returns:
            FinalJudgeResult
        """
        t0 = time.time()

        if not self._call_llm:
            return FinalJudgeResult(
                output={"analysis": "\n\n".join(all_contents), "signal": "数据已获取"},
                reasoning="LLM 不可用，返回原始数据",
                elapsed_ms=(time.time() - t0) * 1000,
            )

        try:
            raw = self._llm_judge_final(
                query, all_summaries, all_contents, intent, stock_code, stock_name,
            )
            result = self._parse_final_judge(raw)
            result.elapsed_ms = (time.time() - t0) * 1000
            return result
        except Exception as e:
            logger.warning("[Judge] 最终输出失败: %s", e)
            return FinalJudgeResult(
                output={"analysis": "\n\n".join(all_contents), "signal": "Judge 异常"},
                reasoning=f"Judge 异常: {e}",
                elapsed_ms=(time.time() - t0) * 1000,
            )

    def _llm_judge_final(
        self,
        query: str,
        all_summaries: List[str],
        all_contents: List[str],
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
    ) -> str:
        """构建最终输出 prompt。"""
        intent_info = ""
        if intent and (intent.verb or intent.noun or intent.domain):
            intent_info = f"\n意图: verb={intent.verb or '-'}, noun={intent.noun or '-'}, domain={intent.domain or '-'}"

        stock_info = ""
        if stock_code:
            stock_info = f"\n标的: {stock_name or '未知'}（{stock_code}）"

        # 汇总所有步骤数据
        steps_section = ""
        for i, (summary, content) in enumerate(zip(all_summaries, all_contents), 1):
            steps_section += f"\n### 步骤 {i}\n"
            steps_section += f"- 结论: {summary}\n"
            steps_section += f"- 原始数据: {content[:800]}\n"

        # 从 judge.md 加载规则
        judge_rules = self._get_judge_rules()

        prompt = (
            f"你是量化分析结果汇总器。根据所有步骤的数据，输出最终金融分析。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"## 各步骤数据\n{steps_section}\n\n"
            f"## 输出格式（只输出 JSON）\n"
            f"```json\n"
            f'{{\n'
            f'  "action": "buy/sell/hold/skip",\n'
            f'  "score": 0-100,\n'
            f'  "direction": "bullish/bearish/neutral",\n'
            f'  "confidence": "high/medium/low",\n'
            f'  "timeframe": "T+3",\n'
            f'  "timeframe_reason": "时间维度理由",\n'
            f'  "stock_code": "股票代码",\n'
            f'  "stock_name": "股票名称",\n'
            f'  "signal": "一句话信号摘要",\n'
            f'  "factors": [\n'
            f'    {{"name": "维度名", "score": 0-100, "direction": "bullish/bearish/neutral"}}\n'
            f'  ],\n'
            f'  "analysis": "完整分析文字"\n'
            f'}}\n'
            f"```\n\n"
            f"{judge_rules}\n"
        )

        return self._call_llm(prompt)

    def _parse_final_judge(self, raw: str) -> FinalJudgeResult:
        """解析最终输出。"""
        import re

        cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r'```json\s*', '', cleaned).strip()
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()

        brace_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group())
                return FinalJudgeResult(output=data, reasoning="解析成功")
            except json.JSONDecodeError:
                pass

        return FinalJudgeResult(
            output={"analysis": cleaned[:500], "signal": "JSON 解析失败"},
            reasoning="JSON 解析失败，返回原始文本",
        )
