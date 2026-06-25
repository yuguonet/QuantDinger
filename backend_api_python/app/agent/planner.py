# -*- coding: utf-8 -*-
"""
Planner — LLM 单步决策器。

职责：每次只输出一步执行指令，执行完后根据结果决定下一步。

流程：
  1. 接收：用户消息 + Judge 上下文摘要
  2. 输出：下一步执行指令（工具选择）

设计原则：
  - 每次只输出一步，不规划多步
  - 根据 Judge 摘要决定下一步工具
  - 不判断任务是否完成（由 Judge 负责）
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

# 工具数量限制
MAX_TOOLS_PER_STEP = 5




# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class StepResult:
    """单步执行结果。"""
    success: bool = False
    skill: Optional[str] = None
    description: str = ""
    tools: List[str] = field(default_factory=list)
    rules: str = ""
    summary: str = ""
    stocks: List[str] = field(default_factory=list)
    reasoning: str = ""
    confidence: float = 0.0
    elapsed_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Skill 描述（给 LLM 看的，精简版）
# ═══════════════════════════════════════════════════════════════

def _get_skill_catalog() -> str:
    """从 agent/skills/*/SKILL.md 动态生成技能目录（单一信源）。"""
    from app.agent.semantics import get_all_skill_metas
    metas = get_all_skill_metas()
    if not metas:
        # fallback: 硬编码兜底（semantics 加载失败时）
        return "可用技能：market-screener(短线选股)"
    lines = ["可用技能（每次选择 1 个最合适的）：", ""]
    # 按 priority 降序排列
    sorted_skills = sorted(metas.items(), key=lambda x: x[1].priority, reverse=True)
    for i, (name, meta) in enumerate(sorted_skills, 1):
        desc = meta.description or meta.name
        tags_str = f" [{','.join(meta.tags)}]" if meta.tags else ""
        lines.append(f"{i}. {name} — {desc}{tags_str}")
    return "\n".join(lines)

# 懒加载缓存
_SKILL_CATALOG_CACHE: str = ""

def _ensure_skill_catalog() -> str:
    global _SKILL_CATALOG_CACHE
    if not _SKILL_CATALOG_CACHE:
        _SKILL_CATALOG_CACHE = _get_skill_catalog()
    return _SKILL_CATALOG_CACHE


# ═══════════════════════════════════════════════════════════════
# Planner
# ═══════════════════════════════════════════════════════════════

class Planner:
    """LLM 单步决策器。"""

    def __init__(self, call_llm: Callable[[str], str] = None):
        self._call_llm = call_llm

    def plan_next_step(
        self,
        query: str,
        judge_context: str = "",
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
        context_summary: str = "",
    ) -> StepResult:
        """根据用户消息和 Judge 上下文摘要，决定下一步。

        Args:
            query: 用户原始消息
            judge_context: Judge 产出的上下文摘要（替代 previous_results）
            intent: IntentResult 对象（含 domain/verb/noun/confidence）
            stock_code: 股票代码（可选）
            stock_name: 股票名称（可选）
            context_summary: 对话历史摘要（可选）

        Returns:
            StepResult
        """
        t0 = time.time()

        # LLM 决策
        if not self._call_llm:
            logger.warning("[Planner] LLM 不可用，返回失败")
            return StepResult(success=False, reasoning="LLM 不可用", elapsed_ms=(time.time() - t0) * 1000)

        try:
            step_data = self._llm_decide_next_step(query, judge_context, intent, stock_code, stock_name, context_summary)
            # ── 调试日志：Planner 输出 JSON ──
            print(f"[DEBUG] Planner 输出 JSON: {json.dumps(step_data, ensure_ascii=False, indent=2)}")
        except Exception as e:
            logger.warning("[Planner] LLM 决策失败: %s", e)
            return StepResult(success=False, reasoning=f"LLM 决策异常: {e}", elapsed_ms=(time.time() - t0) * 1000)

        # 校验
        validated = self._validate_step(step_data)
        if validated is not None:
            logger.warning("[Planner] 步骤校验失败: %s", validated)
            return StepResult(success=False, reasoning=f"步骤校验失败: {validated}", elapsed_ms=(time.time() - t0) * 1000)

        elapsed = (time.time() - t0) * 1000
        logger.info("[Planner] 决策完成: %.0fms", elapsed)

        confidence = step_data.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)):
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        return StepResult(
            success=True,
            skill=step_data.get("skill"),
            description=step_data.get("description", ""),
            tools=step_data.get("tools", []),
            rules=step_data.get("rules", ""),
            summary=step_data.get("summary", ""),
            stocks=step_data.get("stocks", []),
            reasoning=step_data.get("reasoning", ""),
            confidence=confidence,
            elapsed_ms=elapsed,
        )

    def _llm_decide_next_step(
        self,
        query: str,
        judge_context: str = "",
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
        context_summary: str = "",
    ) -> Dict[str, Any]:
        """调用 LLM 决策下一步。返回原始 JSON dict。

        注入上下文：人设 + Judge 摘要 + 规则 + 全量 skill + 全量 tool
        """
        # 1. 人设
        persona_section = ""
        try:
            from app.agent.semantics import get_persona
            p = get_persona()
            if p:
                persona_section = f"你是{p.role}。{p.identity}"
        except Exception as e:
            logger.debug("[Planner] persona 加载失败: %s", e)

        # 2. 股票 + 意图
        stock_info = ""
        if stock_code:
            stock_info = f"\n股票: {stock_name or '未知'}（{stock_code}）"
        intent_info = ""
        if intent and (intent.verb or intent.noun or intent.domain):
            intent_info = f"\n意图: verb={intent.verb or '-'}, noun={intent.noun or '-'}, domain={intent.domain or '-'}"

        # 3. Judge 上下文摘要（替代 previous_results）
        judge_section = ""
        if judge_context:
            judge_section = f"\n## 上一步结论（来自 Judge）\n{judge_context}\n"

        # 4. 全量 skill 摘要
        skills_section = ""
        try:
            from app.agent.semantics import get_skills_summary_xml
            skills_section = get_skills_summary_xml()
        except Exception as e:
            logger.debug("[Planner] skills XML 加载失败，用 fallback: %s", e)
            skills_section = _ensure_skill_catalog()

        # 5. 全量 tool 摘要
        tools_section = ""
        try:
            from app.agent.semantics import get_tools_summary_xml
            tools_section = get_tools_summary_xml()
        except Exception as e:
            logger.debug("[Planner] tools XML 加载失败: %s", e)

        # 6. 规则
        planner_section = ""
        try:
            from app.agent.semantics import get_planner_text
            planner_section = get_planner_text()
        except Exception as e:
            logger.debug("[Planner] planner 规则加载失败: %s", e)

        # 7. 对话上下文
        context_section = ""
        if context_summary:
            context_section = f"\n## 对话历史\n{context_summary}\n"

        prompt = (
            f"{persona_section}\n\n"
            "你是量化分析单步决策器。根据用户问题和上一步结论，选出最相关的工具。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"{judge_section}"
            f"{context_section}\n"
            f"## 可用技能\n{skills_section}\n\n"
            f"## 可用工具\n{tools_section}\n\n"
            f"{planner_section}\n"
        )

        # ── LLM #2 输入日志 ──
        print("[Planner] ═══ LLM #2 输入 ═══")
        print(f"[Planner] 股票: {stock_info}")
        print(f"[Planner] 意图: {intent_info.strip()}")
        print(f"[Planner] Judge 上下文: {judge_context[:100]}")
        print(f"[Planner] 对话历史: {context_section[:100]}")
        print(f"[Planner] prompt 总长度: {len(prompt)} 字符")
        print("[Planner] ═══════════════════")

        raw = self._call_llm(prompt)
        result = self._parse_step_json(raw)
        return result

    def _parse_step_json(self, raw: str) -> Dict[str, Any]:
        """解析 LLM 输出的步骤 JSON。容错处理各种 LLM 输出格式。"""
        import re

        # 1. 清理 think 标签
        cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

        # 2. 去掉 <code>...</code> 包装
        cleaned = re.sub(r'</?code>', '', cleaned).strip()

        # 3. 去掉 final_answer() 包装
        m = re.search(r'final_answer\s*\(\s*(.+)\s*\)', cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()

        # 4. 去掉 markdown 代码块
        cleaned = re.sub(r'```json\s*', '', cleaned).strip()
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()

        # 5. 尝试直接解析
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # 6. 提取最外层 JSON 对象
        brace_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法解析步骤 JSON: {raw[:300]}")

    def _validate_step(self, step_data: Dict[str, Any]) -> Optional[str]:
        """校验步骤。返回 None 表示通过，返回字符串表示失败原因。"""
        # 工具数量限制
        tools = step_data.get("tools", [])
        if len(tools) > MAX_TOOLS_PER_STEP:
            step_data["tools"] = tools[:MAX_TOOLS_PER_STEP]
            logger.info("[Planner] 工具数量超限，截断到 %d 个", MAX_TOOLS_PER_STEP)

        return None  # 通过



