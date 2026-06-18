# -*- coding: utf-8 -*-
"""
Chain Executor — 链路决策执行器。

职责：按链路定义依次调度 Skill，构建 EvalNode 决策树，输出决策。

核心流程：
  execute() → 遍历 ChainStep → _execute_skill() → _build_decision()

每步执行：
  1. Skill.run() → (SkillReport, EvalNode)
  2. 挂载到决策树

决策构建：
  1. 收集各 Skill 的 SkillReport
  2. 加权求和（缺失项从权重池剔除）
  3. 渐进门控（样本量不足 → 分数向50收缩）
  4. 三重阻断（veto / 覆盖度不足 / 无数据）
  5. 输出决策

与旧版区别：
  - 旧版：StepOutput → DecisionCard → 5张表
  - 新版：SkillReport → EvalNode 树 → 1张表
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.schema import (
    Action, Direction, EvalNode, Layer, SkillReport, Status,
    VETO_SCORE, COVERAGE_THRESHOLD, get_skill_cn_name,
)
from app.agent.chain import store
from app.agent.chain.contract import parse_skill_output
from app.agent.chain.chains import ChainDef, ChainStep, get_chain

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 渐进门控
# ═══════════════════════════════════════════════════════════════

def _sample_confidence(evaluated_count: int) -> float:
    """根据已评估决策数返回置信度乘数（0.0~1.0）。

    样本越少，分数越趋近50（中性）。
    """
    import math
    if evaluated_count <= 0:
        return 0.0
    if evaluated_count >= 50:
        return 1.0
    return round(0.2 + 0.8 * (1 - math.exp(-evaluated_count / 15)), 3)


# ═══════════════════════════════════════════════════════════════
# 决策结果
# ═══════════════════════════════════════════════════════════════

@dataclass
class DecisionResult:
    """执行结果。"""
    chain_id: str
    stock_code: str
    stock_name: str
    action: str = "hold"
    score: float = 50.0
    confidence: str = "low"
    direction: str = "neutral"
    reason: str = ""
    recommendation: str = ""
    human_note: str = ""
    root_node: Optional[EvalNode] = None
    success: bool = False
    execution_id: Optional[int] = None
    elapsed_ms: float = 0.0

    @property
    def content(self) -> str:
        """返回可读的决策报告（精简版）。"""
        if not self.root_node:
            return "链路执行未产生决策。"
        return self._to_compact()

    def _to_compact(self) -> str:
        """精简输出：只输出核心决策数据 + 分项明细表。"""
        action_cn = {"buy": "买入", "sell": "卖出", "hold": "观望", "skip": "跳过"}
        lines = [
            f"**{action_cn.get(self.action, '观望')}** {self.stock_name}({self.stock_code})",
            f"评分:{self.score:.0f} 方向:{self.direction} 置信:{self.confidence}",
        ]

        # 分项明细（紧凑格式）
        if self.root_node and self.root_node.children:
            parts = []
            for child in self.root_node.children:
                if child.layer == Layer.SKILL.value:
                    cn = get_skill_cn_name(child.name)
                    s = f"{child.score:.0f}" if child.score is not None else "—"
                    parts.append(f"{cn}:{s}/{child.direction}")
            if parts:
                lines.append(" | ".join(parts))

        if self.human_note:
            lines.append(f"⚠ {self.human_note}")

        return "\n".join(lines)

    def _to_markdown(self) -> str:
        """兼容旧接口，实际调用精简版。"""
        return self._to_compact()

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "action": self.action,
            "score": self.score,
            "direction": self.direction,
            "confidence": self.confidence,
            "reason": self.reason,
            "success": self.success,
            "elapsed_ms": self.elapsed_ms,
        }
        # 分项明细（精简）
        if self.root_node and self.root_node.children:
            d["breakdown"] = [
                {
                    "skill": get_skill_cn_name(c.name),
                    "score": c.score,
                    "direction": c.direction,
                    "signal": c.signal,
                }
                for c in self.root_node.children
                if c.layer == Layer.SKILL.value
            ]
        if self.human_note:
            d["human_note"] = self.human_note
        if self.execution_id:
            d["execution_id"] = self.execution_id
        return d


# ═══════════════════════════════════════════════════════════════
# 链路执行器
# ═══════════════════════════════════════════════════════════════

class ChainExecutor:
    """链路执行器。

    Usage:
        executor = ChainExecutor(chain_id="evaluate+stock", stock_code="600519")
        result = executor.execute(run_skill_fn=my_run_skill, call_llm=my_llm)
    """

    def __init__(
        self,
        chain_id: str,
        stock_code: str,
        stock_name: str = "",
        user_id: int = 1,
    ):
        self.chain_id = chain_id
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.user_id = user_id
        self.chain_def = get_chain(chain_id)
        if not self.chain_def:
            raise ValueError(f"Unknown chain: {chain_id}")
        self._skill_weights = self._load_skill_weights()

    def _load_skill_weights(self) -> Dict[str, float]:
        """从评估系统加载历史 skill 准确率权重。

        优先用历史评估数据，无历史时用 skill 的 default_weight（出厂权重）。
        """
        try:
            weights = store.get_skill_weights()
            if weights:
                logger.info("[ChainExecutor] 加载历史权重 %s: %s", self.chain_id, weights)
                return weights
        except Exception:
            pass

        # 无历史数据，从 semantics/skills/*.md 的 default_weight 构建
        try:
            from app.agent.semantics import get_all_skill_metas
            metas = get_all_skill_metas()
            factory_weights = {}
            for name, meta in metas.items():
                if meta.default_weight and meta.default_weight != 1.0:
                    factory_weights[name] = meta.default_weight
            if factory_weights:
                logger.info("[ChainExecutor] 使用出厂权重: %s", factory_weights)
            return factory_weights
        except Exception:
            return {}

    def execute(
        self,
        run_skill_fn: Callable[[str, str, str, dict], tuple],
        context: Dict[str, Any] = None,
        call_llm: Callable[[str], str] = None,
    ) -> DecisionResult:
        """执行链路。

        Args:
            run_skill_fn: 调用 Skill 的函数。
                签名: run_skill_fn(skill_name, stock_code, stock_name, context) -> (SkillReport, EvalNode)
            context: 传递给每个 skill 的上下文。
            call_llm: LLM 调用函数，Chain 层跨维度推理用。
                签名: call_llm(prompt: str) -> str

        Returns:
            DecisionResult
        """
        t0 = time.time()
        context = context or {}

        # 创建根节点（chain 层）
        root = EvalNode(
            layer=Layer.CHAIN.value,
            name=self.chain_id,
            exec_date=date.today(),
            stock_code=self.stock_code,
            stock_name=self.stock_name,
            input_params={"user_query": context.get("user_query", ""), **context},
        )

        logger.info("[ChainExecutor] 开始执行链路 %s | 股票=%s",
                     self.chain_id, self.stock_code)

        # 依次执行各 skill
        skill_reports: List[SkillReport] = []
        previous_outputs = []

        for step in sorted(self.chain_def.steps, key=lambda s: s.order):
            report, skill_node = self._execute_skill(
                step, previous_outputs, context, run_skill_fn,
            )
            root.add_child(skill_node)
            skill_reports.append(report)
            previous_outputs.append(report)

            # 必须步骤被否决，终止
            if step.required and skill_node.is_veto:
                logger.warning("[ChainExecutor] 必须步骤 %s 被否决，终止", step.name)
                break

        # 构建决策（用 LLM 跨维度推理）
        result = self._build_decision(root, skill_reports, call_llm=call_llm)
        result.elapsed_ms = (time.time() - t0) * 1000
        result.success = any(s.status == "ok" for s in skill_reports)

        # 持久化
        result.execution_id = store.save_tree(root)
        result.root_node = root

        logger.info("[ChainExecutor] 链路 %s 完成 | %s %s | 评分=%.1f | %.0fms",
                     self.chain_id, result.action, self.stock_code,
                     result.score, result.elapsed_ms)

        return result

    def _execute_skill(
        self,
        step: ChainStep,
        previous_outputs: List[SkillReport],
        context: Dict[str, Any],
        run_skill_fn: Callable,
    ) -> tuple:
        """执行单个 Skill。"""
        try:
            # 构造上下文（包含前序 skill 结果）
            step_context = dict(context)
            if previous_outputs:
                step_context["previous_results"] = [
                    {"skill": r.skill_name, "signal": r.signal, "direction": r.direction}
                    for r in previous_outputs if r.status == "ok"
                ]

            report, skill_node = run_skill_fn(
                step.agent, self.stock_code, self.stock_name, step_context,
            )
            return report, skill_node

        except Exception as e:
            logger.warning("[ChainExecutor] Skill %s 失败: %s", step.name, e)
            report = SkillReport(
                skill_name=step.agent, status="failed", error=str(e),
            )
            skill_node = EvalNode(
                layer=Layer.SKILL.value,
                name=step.agent,
                status=Status.FAILED.value,
                error=str(e),
            )
            return report, skill_node

    def _build_decision(
        self,
        root: EvalNode,
        skill_reports: List[SkillReport],
        call_llm: Callable[[str], str] = None,
    ) -> DecisionResult:
        """构建决策 — LLM 跨维度推理。

        Chain 层的核心价值：把各 Skill 的报告放在一起，让 LLM 做跨维度综合研判。
        不是加权求和，而是理解"MACD看多 + RSI超买 + 游资出货 = 什么"。

        流程：
          1. 检查前置条件（覆盖度、否决项）
          2. 格式化各 Skill 报告
          3. LLM 跨维度推理
          4. 解析 LLM 输出 → DecisionResult
          5. 回退：LLM 失败时降级为加权计算
        """
        result = DecisionResult(
            chain_id=self.chain_id,
            stock_code=self.stock_code,
            stock_name=self.stock_name,
        )

        # ── 前置检查 ──
        valid_reports = [r for r in skill_reports if r.status == "ok" and r.score is not None]
        total_steps = len(skill_reports)
        valid_steps = len(valid_reports)
        has_veto = any(r.score <= VETO_SCORE for r in valid_reports)
        coverage_ratio = valid_steps / total_steps if total_steps > 0 else 0
        gaps = []
        for r in skill_reports:
            if r.status == "missing":
                gaps.append(f"{get_skill_cn_name(r.skill_name)}: 数据缺失")
            elif r.status == "failed":
                gaps.append(f"{get_skill_cn_name(r.skill_name)}: {r.error}")

        # 否决 → 直接 skip
        if has_veto:
            result.action = "skip"
            result.direction = "neutral"
            result.score = 0.0
            result.confidence = "reject"
            result.reason = "存在否决项，不执行"
            root.score = 0.0
            root.direction = "neutral"
            root.action = "skip"
            root.confidence = 0.0
            result.human_note = f"否决项: {', '.join(gaps[:3])}" if gaps else ""
            return result

        # 覆盖度不足 → hold
        if coverage_ratio < COVERAGE_THRESHOLD:
            result.action = "hold"
            result.direction = "neutral"
            result.score = 50.0
            result.confidence = "low"
            result.reason = f"覆盖度不足（{valid_steps}/{total_steps}），数据不充分"
            root.score = 50.0
            root.direction = "neutral"
            root.action = "hold"
            root.confidence = 0.2
            result.human_note = f"缺失: {', '.join(gaps[:3])}" if gaps else ""
            return result

        # ── 加权计算（作为 LLM 推理的参考基线）──
        weighted_result = self._weighted_fallback(valid_reports)

        # ── LLM 跨维度推理 ──
        llm_decision = None
        if call_llm:
            try:
                llm_decision = self._llm_synthesize(
                    valid_reports, call_llm, weighted_baseline=weighted_result,
                )
            except Exception as e:
                logger.warning("[ChainExecutor] LLM 推理失败，使用加权结果: %s", e)

        # ── 回退：LLM 不可用时用加权结果 ──
        if llm_decision is None:
            llm_decision = weighted_result

        # ── 填充结果 ──
        result.action = llm_decision.get("action", "hold")
        result.score = max(0, min(100, float(llm_decision.get("score", 50))))
        result.direction = llm_decision.get("direction", "neutral")
        result.reason = llm_decision.get("reasoning", "")
        result.recommendation = {
            "buy": "建议买入", "sell": "建议卖出",
            "hold": "建议观望", "skip": "建议跳过",
        }.get(result.action, "建议观望")

        # 置信等级
        confidence_val = llm_decision.get("confidence", 0.5)
        if isinstance(confidence_val, str):
            result.confidence = confidence_val
        else:
            if confidence_val >= 0.7:
                result.confidence = "high"
            elif confidence_val >= 0.4:
                result.confidence = "medium"
            else:
                result.confidence = "low"

        # 填充根节点
        root.score = result.score
        root.direction = result.direction
        root.action = result.action
        root.confidence = confidence_val if isinstance(confidence_val, float) else 0.5
        root.analysis = result.reason

        # 人工复核提示
        notes = []
        if gaps:
            notes.append(f"缺失数据: {', '.join(gaps[:3])}")
        if coverage_ratio < 0.6:
            notes.append(f"覆盖度仅 {valid_steps}/{total_steps}，结论可靠性较低")
        result.human_note = "；".join(notes) if notes else ""

        return result

    def _llm_synthesize(
        self,
        skill_reports: List[SkillReport],
        call_llm: Callable[[str], str],
        weighted_baseline: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """LLM 跨维度综合研判。

        把各 Skill 的结构化报告格式化后交给 LLM，让 LLM 做跨维度推理。
        加权基线分数作为参考点提供给 LLM，LLM 可以同意或修正。

        Args:
            skill_reports: 各 Skill 的标准化报告
            call_llm: LLM 调用函数
            weighted_baseline: 加权计算的基线结果（action/score/direction）
        """
        # 格式化各 skill 报告
        reports_text = self._format_reports_for_llm(skill_reports)

        # 加权基线参考
        baseline_section = ""
        if weighted_baseline:
            baseline_section = (
                f"\n## 量化基线（加权计算，仅供参考）\n"
                f"- 加权评分: {weighted_baseline.get('score', 50):.1f}\n"
                f"- 加权方向: {weighted_baseline.get('direction', 'neutral')}\n"
                f"- 加权建议: {weighted_baseline.get('action', 'hold')}\n"
                f"- 各维度: {weighted_baseline.get('reasoning', '')}\n\n"
                "你可以同意基线判断，也可以基于跨维度关联修正它。"
                "如果修正，必须在 reasoning 中解释为什么基线不够准确。\n"
            )

        # 从 semantics/judgment.md 加载核心原则和输出格式
        judgment_section = ""
        try:
            from app.agent.semantics import get_judgment_text
            judgment_section = get_judgment_text()
        except Exception:
            pass
        if not judgment_section:
            judgment_section = (
                "## 核心原则\n"
                "- 价格折扣一切：技术面是地基，其他维度用来验证和解释\n"
                "- 数据陷阱：龙虎榜(盘后+游资一日游)、资金流向(滞后)、新闻(你看到时市场已反应)\n"
                "- 多维度矛盾时，优先相信量价关系\n"
                "- A股只能做多，空头信号意味着回避而非做空\n"
            )

        prompt = (
            "你是 A 股量化决策分析师。基于以下各维度分析报告，做跨维度综合研判。\n\n"
            f"{judgment_section}\n\n"
            f"## 分析目标\n股票: {self.stock_name or self.stock_code}（{self.stock_code}）\n\n"
            f"## 各维度分析报告\n{reports_text}\n"
            f"{baseline_section}\n"
        )

        raw = call_llm(prompt)
        return self._parse_llm_decision(raw)

    def _format_reports_for_llm(self, skill_reports: List[SkillReport]) -> str:
        """将各 Skill 报告格式化为 LLM 可读的文本。"""
        parts = []
        for r in skill_reports:
            cn_name = get_skill_cn_name(r.skill_name)
            direction_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(r.direction, "中性")

            part = f"### {cn_name}（{r.skill_name}）\n"
            part += f"- 方向: {direction_cn} | 评分: {r.score:.0f} | 置信: {r.confidence:.2f}\n"

            if r.signal:
                part += f"- 信号: {r.signal}\n"

            if r.factors:
                factor_lines = []
                for f in r.factors:
                    s = f"{f.score:.0f}" if f.score is not None else "—"
                    factor_lines.append(f"  - {f.name}: {f.value} ({s}分)")
                part += "- 因子:\n" + "\n".join(factor_lines) + "\n"

            if r.analysis:
                # 截断过长的分析文字
                analysis = r.analysis[:500]
                if len(r.analysis) > 500:
                    analysis += "..."
                part += f"- 分析: {analysis}\n"

            parts.append(part)

        return "\n".join(parts)

    def _parse_llm_decision(self, raw: str) -> Dict[str, Any]:
        """解析 LLM 输出的决策 JSON。"""
        import json as _json
        import re as _re

        # 尝试提取 JSON 块
        patterns = [
            r'```json\s*\n?(.*?)\n?\s*```',
            r'```\s*\n?(.*?)\n?\s*```',
            r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',
            r'(\{[^{}]*"score"[^{}]*"action"[^{}]*\})',
        ]

        for pattern in patterns:
            match = _re.search(pattern, raw, _re.DOTALL | _re.IGNORECASE)
            if match:
                try:
                    data = _json.loads(match.group(1).strip())
                    if isinstance(data, dict) and "action" in data:
                        # 校验 action
                        if data["action"] not in ("buy", "sell", "hold", "skip"):
                            data["action"] = "hold"
                        # 校验 score
                        data["score"] = max(0, min(100, float(data.get("score", 50))))
                        # 校验 direction
                        if data.get("direction") not in ("bullish", "bearish", "neutral"):
                            data["direction"] = "neutral"
                        return data
                except (_json.JSONDecodeError, TypeError, ValueError):
                    continue

        # JSON 解析失败 → 关键词匹配
        logger.warning("[ChainExecutor] LLM 输出解析失败，尝试关键词匹配")
        return self._keyword_fallback(raw)

    def _keyword_fallback(self, raw: str) -> Dict[str, Any]:
        """关键词匹配兜底。"""
        raw_lower = raw.lower()

        # action
        action = "hold"
        if any(kw in raw_lower for kw in ["买入", "buy", "建议买"]):
            action = "buy"
        elif any(kw in raw_lower for kw in ["卖出", "sell", "建议卖"]):
            action = "sell"
        elif any(kw in raw_lower for kw in ["跳过", "skip", "回避"]):
            action = "skip"

        # direction
        direction = "neutral"
        if any(kw in raw_lower for kw in ["看多", "bullish", "偏多"]):
            direction = "bullish"
        elif any(kw in raw_lower for kw in ["看空", "bearish", "偏空"]):
            direction = "bearish"

        # score（从 direction 推断）
        if direction == "bullish":
            score = 65.0
        elif direction == "bearish":
            score = 35.0
        else:
            score = 50.0

        return {
            "action": action,
            "score": score,
            "direction": direction,
            "confidence": 0.3,
            "reasoning": raw[:300],
            "key_factors": [],
        }

    def _weighted_fallback(self, skill_reports: List[SkillReport]) -> Dict[str, Any]:
        """加权计算回退（LLM 不可用时）。"""
        total_weight = 0.0
        weighted_score = 0.0

        for r in skill_reports:
            weight = self._skill_weights.get(r.skill_name, 1.0)
            weighted_score += r.score * weight
            total_weight += weight

        if total_weight > 0:
            score = round(weighted_score / total_weight, 1)
        else:
            score = 50.0

        # 方向
        valid_dirs = [r.direction for r in skill_reports if r.direction != "neutral"]
        bull = sum(1 for d in valid_dirs if d == "bullish")
        bear = sum(1 for d in valid_dirs if d == "bearish")

        if bull > bear:
            direction = "bullish"
        elif bear > bull:
            direction = "bearish"
        else:
            direction = "neutral"

        # action
        if score >= 60:
            action = "buy"
        elif score <= 40:
            action = "sell"
        else:
            action = "hold"

        # 构造推理摘要
        parts = []
        for r in skill_reports:
            cn = get_skill_cn_name(r.skill_name)
            parts.append(f"{cn}:{r.direction}/{r.score:.0f}")
        reasoning = f"加权评分 {score:.1f}。各维度: {', '.join(parts)}"

        return {
            "action": action,
            "score": score,
            "direction": direction,
            "confidence": 0.4,
            "reasoning": reasoning,
            "key_factors": [],
        }
