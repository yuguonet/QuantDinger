# -*- coding: utf-8 -*-
"""
Planner — LLM 单步决策器。

职责：每次只输出一步执行指令，执行完后根据结果决定下一步。

流程：
  1. 接收：用户消息 + 已执行步骤的结果
  2. 判断：任务是否完成？
  3. 如果未完成：输出下一步指令
  4. 如果已完成：输出完成总结

设计原则：
  - 每次只输出一步，不规划多步
  - 根据执行结果决定下一步
  - 任务完成时输出 done=true
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
    done: bool = False
    summary: str = ""
    stocks: List[str] = field(default_factory=list)
    reasoning: str = ""
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
        previous_results: List[Dict[str, Any]] = None,
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
        context: Dict[str, Any] = None,
        context_summary: str = "",
    ) -> StepResult:
        """根据用户消息和已执行结果，决定下一步。

        Args:
            query: 用户原始消息
            previous_results: 已执行步骤的结果列表
            intent: IntentResult 对象（含 domain/verb/noun/confidence）
            stock_code: 股票代码（可选）
            stock_name: 股票名称（可选）
            context: 额外上下文
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
            step_data = self._llm_decide_next_step(query, previous_results, intent, stock_code, stock_name, context_summary)
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
        logger.info("[Planner] 决策完成: done=%s, %.0fms", step_data.get("done", False), elapsed)

        return StepResult(
            success=True,
            skill=step_data.get("skill"),
            description=step_data.get("description", ""),
            tools=step_data.get("tools", []),
            rules=step_data.get("rules", ""),
            done=step_data.get("done", False),
            summary=step_data.get("summary", ""),
            stocks=step_data.get("stocks", []),
            reasoning=step_data.get("reasoning", ""),
            elapsed_ms=elapsed,
        )

    def _llm_decide_next_step(
        self,
        query: str,
        previous_results: List[Dict[str, Any]] = None,
        intent=None,
        stock_code: str = "",
        stock_name: str = "",
        context_summary: str = "",
    ) -> Dict[str, Any]:
        """调用 LLM 决策下一步。返回原始 JSON dict。

        注入完整上下文：人设 + 已执行结果 + 规则 + 全量 skill + 全量 tool
        """
        # 1. 人设
        persona_section = ""
        try:
            from app.agent.semantics import get_persona
            p = get_persona()
            if p:
                persona_section = f"你是{p.role}。{p.identity}"
        except Exception:
            pass

        # 2. 股票 + 意图
        stock_info = ""
        if stock_code:
            stock_info = f"\n股票: {stock_name or '未知'}（{stock_code}）"
        intent_info = ""
        if intent and (intent.verb or intent.noun or intent.domain):
            intent_info = f"\n意图: verb={intent.verb or '-'}, noun={intent.noun or '-'}, domain={intent.domain or '-'}"

        # 3. 已执行结果
        previous_results_section = ""
        if previous_results:
            previous_results_section = "\n## 已执行步骤结果\n"
            for i, result in enumerate(previous_results, 1):
                previous_results_section += f"\n### 步骤 {i}\n"
                previous_results_section += f"- 技能: {result.get('skill', '无')}\n"
                previous_results_section += f"- 描述: {result.get('description', '无')}\n"
                previous_results_section += f"- 工具: {', '.join(result.get('tools', []))}\n"
                previous_results_section += f"- 结果: {result.get('content', '无')[:500]}\n"


        # 4. 全量 skill 摘要
        skills_section = ""
        try:
            from app.agent.semantics import get_skills_summary_xml
            skills_section = get_skills_summary_xml()
        except Exception:
            skills_section = _ensure_skill_catalog()  # fallback

        # 5. 全量 tool 摘要
        tools_section = ""
        try:
            from app.agent.semantics import get_tools_summary_xml
            tools_section = get_tools_summary_xml()
        except Exception:
            pass

        # 6. 规则 + 输出格式
        planner_section = ""
        try:
            from app.agent.semantics import get_planner_text
            planner_section = get_planner_text()
        except Exception:
            pass
        if not planner_section:
            planner_section = ""

        # 7. 对话上下文
        context_section = ""
        if context_summary:
            context_section = f"\n## 对话历史\n{context_summary}\n"

        prompt = (
            f"{persona_section}\n\n"
            "你是量化分析单步决策器。根据用户问题和已执行结果，决定下一步。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"{previous_results_section}\n"
            f"{context_section}\n"
            f"## 可用技能\n{skills_section}\n\n"
            f"## 可用工具\n{tools_section}\n\n"
            f"{planner_section}\n"
        )

        # ── LLM #2 输入日志 ──
        print("[Planner] ═══ LLM #2 输入 ═══")
        print(f"[Planner] 人设: {persona_section[:200]}")
        print(f"[Planner] 股票: {stock_info}")
        print(f"[Planner] 意图: {intent_info.strip()}")
        print(f"[Planner] 已执行结果: {previous_results_section[:500]}")
        print(f"[Planner] 对话历史: {context_section[:500]}")
        print(f"[Planner] 可用技能:\n{skills_section[:2000]}")
        print(f"[Planner] 可用工具:\n{tools_section[:2000]}")
        print(f"[Planner] 规则:\n{planner_section[:1000]}")
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
            if isinstance(data, dict) and "done" in data:
                return data
        except json.JSONDecodeError:
            pass

        # 6. 提取最外层 JSON 对象（含 done）
        match = re.search(r'\{[^{}]*"done"\s*:\s*(?:true|false)[^{}]*\}', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # 7. 提取任意最外层 {...}
        brace_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group())
                if isinstance(data, dict) and "done" in data:
                    return data
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法解析步骤 JSON: {raw[:300]}")

    def _validate_step(self, step_data: Dict[str, Any]) -> Optional[str]:
        """校验步骤。返回 None 表示通过，返回字符串表示失败原因。"""
        # 检查必要字段
        if "done" not in step_data:
            return "缺少 done 字段"

        # 如果任务未完成，检查必要字段
        if not step_data.get("done"):
            if not step_data.get("skill") and not step_data.get("tools"):
                return "任务未完成时必须提供 skill 或 tools"
            if not step_data.get("rules"):
                return "任务未完成时必须提供 rules"

        # 如果任务完成，检查必要字段
        if step_data.get("done"):
            if not step_data.get("summary"):
                return "任务完成时必须提供 summary"

        # 工具数量限制
        tools = step_data.get("tools", [])
        if len(tools) > MAX_TOOLS_PER_STEP:
            # 截断而非失败
            step_data["tools"] = tools[:MAX_TOOLS_PER_STEP]
            logger.info("[Planner] 工具数量超限，截断到 %d 个", MAX_TOOLS_PER_STEP)

        return None  # 通过



