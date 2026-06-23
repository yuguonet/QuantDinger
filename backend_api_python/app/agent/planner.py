# -*- coding: utf-8 -*-
"""
Planner — LLM 规划器。

职责：当无固定链路匹配时，用轻量 LLM 调用规划 Skill 执行方案。

流程：
  1. 缓存查询已移至 _try_chain() Layer 0（tool_chains.py get_chain_plan）
  2. 缓存未命中 → LLM 规划（只选 Skill，不执行）
  3. 校验规划（步数、必选 Skill、去重）
  4. 返回 ChainDef 供 _execute_plan() 执行
  5. 执行成功 + 质量门通过后，由 evaluator._writeback_chain() 写入 tool_chains.json

设计原则：
  - LLM 只做选择题（从 15 个 Skill 中选 1~5 个），不做开放题
  - 规划必须可回测（存 query + 选择 + reasoning）
  - 失败必须告知用户
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.chains import ChainDef, ChainStep, register_chain

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

# 规划步数限制
MIN_STEPS = 1
MAX_STEPS = 5




# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class PlanResult:
    """规划结果。"""
    success: bool = False
    chain_def: Optional[ChainDef] = None
    reasoning: str = ""
    from_cache: bool = False
    degraded: bool = False          # 是否降级
    degrade_reason: str = ""        # 降级原因
    stocks: List[str] = field(default_factory=list)
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
        return (
            "可用技能：technical_agent(技术面), intelligence_agent(情报), "
            "market-screener(选股), backtest_agent(回测), "
            "researcher(多空)"
        )
    lines = ["可用技能（从下列中选择 1~5 个，按执行顺序排列）：", ""]
    # 按 priority 降序排列
    sorted_skills = sorted(metas.items(), key=lambda x: x[1].priority, reverse=True)
    for i, (name, meta) in enumerate(sorted_skills, 1):
        desc = meta.description or meta.name
        tags_str = f" [{','.join(meta.tags)}]" if meta.tags else ""
        lines.append(f"{i}. {name} — {desc}{tags_str}")
    lines.append("")
    lines.append("大多数场景必须包含 technical_agent（技术面地基）。")
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
    """LLM 规划器。"""

    def __init__(self, call_llm: Callable[[str], str] = None):
        self._call_llm = call_llm

    def plan(
        self,
        query: str,
        stock_code: str = "",
        stock_name: str = "",
        verb: str = "",
        noun: str = "",
        context: Dict[str, Any] = None,
        context_summary: str = "",
    ) -> PlanResult:
        """为用户 query 生成执行规划。

        Args:
            query: 用户原始消息
            stock_code: 股票代码（可选）
            stock_name: 股票名称（可选）
            verb: 意图动词（可选）
            noun: 意图对象（可选）
            context: 额外上下文
            context_summary: 对话历史摘要（可选）

        Returns:
            PlanResult
        """
        t0 = time.time()

        # 1. 缓存查询已移至 _try_chain() Layer 0（tool_chains.py）

        # 2. LLM 规划
        if not self._call_llm:
            logger.warning("[Planner] LLM 不可用，返回失败")
            return PlanResult(success=False, reasoning="LLM 不可用", elapsed_ms=(time.time() - t0) * 1000)

        try:
            plan_data = self._llm_plan(query, stock_code, stock_name, verb, noun, context_summary)
            # ── 调试日志：Planner 输出 JSON ──
            print(f"[DEBUG] Planner 输出 JSON: {json.dumps(plan_data, ensure_ascii=False, indent=2)}")
        except Exception as e:
            logger.warning("[Planner] LLM 规划失败: %s", e)
            return PlanResult(success=False, reasoning=f"LLM 规划异常: {e}", elapsed_ms=(time.time() - t0) * 1000)

        # 3. 校验
        validated = self._validate(plan_data)
        if validated is not None:
            logger.warning("[Planner] 规划校验失败: %s", validated)
            return PlanResult(success=False, reasoning=f"规划校验失败: {validated}", elapsed_ms=(time.time() - t0) * 1000)

        # 4. 构建 ChainDef
        chain_def = self._build_chain_def(plan_data, stock_code)

        # 5. 不在此处存储 — plan 须经 agent 执行 + 质量门验证后
        #    由 evaluator._writeback_chain() 统一写入 tool_chains.json

        elapsed = (time.time() - t0) * 1000
        logger.info("[Planner] 规划完成: %d 步, %.0fms", len(plan_data.get("phases", plan_data.get("steps", []))), elapsed)

        return PlanResult(
            success=True,
            chain_def=chain_def,
            reasoning=plan_data.get("reasoning", ""),
            stocks=plan_data.get("stocks", []),
            elapsed_ms=elapsed,
        )

    def _llm_plan(
        self,
        query: str,
        stock_code: str,
        stock_name: str,
        verb: str,
        noun: str,
        context_summary: str = "",
    ) -> Dict[str, Any]:
        """调用 LLM 生成规划。返回原始 JSON dict。

        注入完整上下文：人设 + 对话历史 + 规则 + 全量 skill + 全量 tool
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
        if verb or noun:
            intent_info = f"\n意图: verb={verb or '-'}, noun={noun or '-'}"

        # 3. 全量 skill 摘要
        skills_section = ""
        try:
            from app.agent.semantics import get_skills_summary_xml
            skills_section = get_skills_summary_xml()
        except Exception:
            skills_section = _ensure_skill_catalog()  # fallback

        # 4. 全量 tool 摘要
        tools_section = ""
        try:
            from app.agent.semantics import get_tools_summary_xml
            tools_section = get_tools_summary_xml()
        except Exception:
            pass

        # 5. 规则 + 输出格式
        planner_section = ""
        try:
            from app.agent.semantics import get_planner_text
            planner_section = get_planner_text()
        except Exception:
            pass
        if not planner_section:
            planner_section = (
                "## 规则\n"
                "- 从上述技能中选择 1~5 个，按执行顺序排列\n"
                "- 不要选择与问题无关的技能\n"
                "- 如果涉及股票但未提供代码，在 stocks 中列出需要的代码\n"
                "- ⚠️ 如果用户明确指定了步骤（如'第一步'、'第二步'、'首先'、'然后'），必须严格按用户指定的步骤拆分为多个 phase\n"
                "- 不要过度拆分：相关操作（如多个数据获取、多个指标计算）合并到一个 phase，简单股票分析通常 2~3 个 phase 足够\n"
            )

        # 6. 对话上下文
        context_section = ""
        if context_summary:
            context_section = f"\n## 对话历史\n{context_summary}\n"

        prompt = (
            f"{persona_section}\n\n"
            "你是量化分析规划器。根据用户问题，制定执行计划。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"{context_section}\n"
            f"## 可用技能\n{skills_section}\n\n"
            f"## 可用工具\n{tools_section}\n\n"
            f"{planner_section}\n"
        )

        raw = self._call_llm(prompt)
        result = self._parse_plan_json(raw)
        # ── 调试日志：Planner 输出 JSON ──
        print(f"[DEBUG] Planner 输出 JSON: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return result

    def _parse_plan_json(self, raw: str) -> Dict[str, Any]:
        """解析 LLM 输出的规划 JSON。容错处理各种 LLM 输出格式。"""
        import re

        # 1. 清理 think 标签
        cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

        # 2. 去掉 <code>...</code> 包装（必须在 final_answer 之前）
        cleaned = re.sub(r'</?code>', '', cleaned).strip()

        # 3. 去掉 final_answer() 包装（qwen-coder 常见格式）
        m = re.search(r'final_answer\s*\(\s*(.+)\s*\)', cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()

        # 3. 去掉 markdown 代码块
        cleaned = re.sub(r'```json\s*', '', cleaned).strip()
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()

        # 4. 尝试直接解析
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and ("phases" in data or "steps" in data):
                return data
        except json.JSONDecodeError:
            pass

        # 5. 提取最外层 JSON 对象（含 phases 或 steps）
        match = re.search(r'\{[^{}]*"(?:phases|steps)"\s*:\s*\[.*?\][^{}]*\}', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # 6. 提取任意最外层 {...}
        brace_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group())
                if isinstance(data, dict) and ("phases" in data or "steps" in data):
                    return data
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法解析规划 JSON: {raw[:300]}")

    def _validate(self, plan_data: Dict[str, Any]) -> Optional[str]:
        """校验规划。返回 None 表示通过，返回字符串表示失败原因。"""
        # 兼容 phases 和 steps 两个字段名
        phases = plan_data.get("phases", plan_data.get("steps", []))
        if not phases:
            return "phases 为空"

        if len(phases) < MIN_STEPS:
            return f"步数不足（{len(phases)} < {MIN_STEPS}）"

        if len(phases) > MAX_STEPS:
            # 截断而非失败
            phases = phases[:MAX_STEPS]
            logger.info("[Planner] 步数超限，截断到 %d 步", MAX_STEPS)

        # 去重（保留描述不同的步骤，即使 skill 相同）
        seen = set()
        deduped = []
        for step in phases:
            agent = step.get("agent", step.get("skill", ""))
            desc = step.get("description", "")
            # 用 skill+description 作为去重 key，描述不同的步骤保留
            dedup_key = f"{agent}:{desc}" if desc else agent
            if agent and dedup_key not in seen:
                seen.add(dedup_key)
                deduped.append(step)
        phases = deduped

        # 统一存为 phases
        plan_data["phases"] = phases
        plan_data.pop("steps", None)

        # 新架构：从 agent/skills/*/SKILL.md 校验 Skill 是否存在
        # 注：未知的 skill 不再移除，因为可能是 tool 而不是 skill
        from app.agent.semantics import get_all_skill_metas
        known_skills = set(get_all_skill_metas().keys())
        if known_skills:
            selected = {s.get("agent", s.get("skill", "")) for s in phases}
            unknown = selected - known_skills - {""}
            if unknown:
                logger.info("[Planner] 发现未知 Skill（可能是 tool）: %s (已知 Skill: %s)", unknown, known_skills)
                # 不再移除未知 skill，让 agent.py 处理（作为 tool 直接调用）
        return None  # 通过

    def _build_chain_def(self, plan_data: Dict[str, Any], stock_code: str) -> ChainDef:
        """从规划数据构建 ChainDef。"""
        phases = plan_data.get("phases", plan_data.get("steps", []))
        steps = []
        for i, step_data in enumerate(phases, 1):
            # 兼容 "skill" 和 "agent" 两个字段名
            agent = step_data.get("skill", "") or step_data.get("agent", "")
            steps.append(ChainStep(
                name=agent,
                agent=agent,
                order=i,
                description=step_data.get("description", ""),
                required=(i == 1),
                rules=step_data.get("rules", ""),
            ))

        # 收集 Planner 上下文（关键信息传递给各 Skill Agent）
        planner_context = plan_data.get("context", {})
        # 兼容旧链路 "规则" 字段
        if "规则" in plan_data and "rules" not in planner_context:
            planner_context["rules"] = plan_data["规则"]

        chain_id = f"planned+{hashlib.md5(json.dumps(plan_data, sort_keys=True).encode()).hexdigest()[:8]}"

        # progressive: phase 间是否递进关系（后一步依赖前一步结论）
        progressive = plan_data.get("progressive", True)

        chain_def = ChainDef(
            chain_id=chain_id,
            name="LLM 规划链路",
            description=plan_data.get("reasoning", ""),
            steps=steps,
            trigger_verbs=[],
            trigger_nouns=[],
            context=planner_context,
            progressive=progressive,
        )
        register_chain(chain_def)  # 动态链路注册到全局表
        return chain_def



