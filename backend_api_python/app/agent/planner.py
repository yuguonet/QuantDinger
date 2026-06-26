# -*- coding: utf-8 -*-
"""
Planner — LLM 单步决策器。

职责：每次只输出一步执行指令，执行完后根据结果决定下一步。

流程：
  1. 接收：用户消息 + 上下文摘要
  2. 输出：下一步执行指令（工具选择）

设计原则：
  - 每次只输出一步，不规划多步
  - 根据上下文和已获取数据决定下一步工具
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════




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
    """LLM 规划器。一次规划全部步骤。"""

    def __init__(self, call_llm: Callable[[str], str] = None):
        self._call_llm = call_llm

    def plan_next_step(
        self,
        query: str,
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
        context_summary: str = "",
        step_records: list = None,
        **kwargs,
    ) -> StepResult:
        """根据用户消息和已有步骤记录，决定下一步工具。

        Args:
            query: 用户原始消息
            intent: IntentResult 对象
            stock_code: 股票代码
            stock_name: 股票名称
            context_summary: 对话历史摘要
            step_records: list of dict（来自 State.step_records）

        Returns:
            StepResult
        """
        t0 = time.time()

        if not self._call_llm:
            logger.warning("[Planner] LLM 不可用，返回失败")
            return StepResult(success=False, reasoning="LLM 不可用", elapsed_ms=(time.time() - t0) * 1000)

        step_records = step_records or []

        # 从 step_records 提取进度信息
        already_used_tools = []
        already_fetched_data = ""
        for rec in step_records:
            for tc in rec.get("tool_calls", []):
                name = tc.get("tool", "")
                if name and name not in already_used_tools:
                    already_used_tools.append(name)
            if not rec.get("tool_calls"):
                for t in rec.get("tools", []):
                    if t and t not in already_used_tools:
                        already_used_tools.append(t)
        if step_records:
            recent = step_records[-3:]
            parts = []
            for r in recent:
                step = r.get("step", "?")
                desc = r.get("description", "")
                content = r.get("step_content", "")
                parts.append(f"- 步骤{step}({desc}): {content[:200]}" if content else f"- 步骤{step}: 无数据")
            already_fetched_data = "\n".join(parts)

            done_steps = len(step_records)
            done_tools = len(already_used_tools)
            print(f"[Planner] 进度: 已完成{done_steps}步, 已用{done_tools}个工具: {already_used_tools}")

        try:
            step_data = self._llm_decide_next_step(
                query, intent, stock_code, stock_name, context_summary,
                already_used_tools=already_used_tools,
                already_fetched_data=already_fetched_data,
            )
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
        tools = step_data.get("tools", [])
        skill = step_data.get("skill")
        print(f"[Planner] 选了 {len(tools)} 个工具: {tools}")
        if skill:
            print(f"[Planner] 用 skill: {skill}")

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
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
        context_summary: str = "",
        already_used_tools: List[str] = None,
        already_fetched_data: str = "",
    ) -> Dict[str, Any]:
        """调用 LLM 决策下一步。返回原始 JSON dict。

        注入上下文：人设 + 规则 + 全量 skill + 全量 tool + 已用工具 + 已获取数据
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

        # 3. 已获取数据
        data_section = ""
        if already_fetched_data:
            data_section = f"\n## 已获取的数据\n{already_fetched_data}\n"

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

        # 4. 已用工具
        used_tools_section = ""
        if already_used_tools:
            unique_tools = list(dict.fromkeys(already_used_tools))
            used_tools_section = f"\n## 已调用的工具\n{', '.join(unique_tools)}\n\n⚠️ 不要重复调用，除非需要不同参数。\n"

        # 9. 已获取数据摘要
        fetched_data_section = ""
        if already_fetched_data:
            fetched_data_section = f"\n## 前序步骤已获取的数据\n{already_fetched_data}\n\n请基于已有数据决定下一步，不要重复获取相同数据。\n"

        prompt = (
            f"{persona_section}\n\n"
            "你是量化分析规划器。根据用户问题，规划分析步骤并选出本步工具。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"{data_section}"
            f"{used_tools_section}"
            f"{fetched_data_section}"
            f"## 可用技能\n{skills_section}\n\n"
            f"## 可用工具\n{tools_section}\n\n"
            f"{planner_section}\n\n"
            "## 输出格式（只输出 JSON）\n"
            "```json\n"
            "{\n"
            '  "skill": null,\n'
            '  "tools": ["工具1", "工具2"],\n'
            '  "description": "步骤描述",\n'
            '  "rules": "执行规则",\n'
            '  "reasoning": "理由"\n'
            "}\n"
            "```\n\n"
            "## 规则\n"
            "- 简单任务一把工具全选，不要拆步\n"
            "- 不要重复已调用的工具\n"
        )

        print(f"[Planner] prompt 长度: {len(prompt)} 字符")
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
        """校验步骤。返回 None 表示通过。"""
        return None



