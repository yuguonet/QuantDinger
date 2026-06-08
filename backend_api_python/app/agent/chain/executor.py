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
        """返回可读的决策报告。"""
        if not self.root_node:
            return "链路执行未产生决策。"
        return self._to_markdown()

    def _to_markdown(self) -> str:
        lines = []
        action_cn = {
            "buy": "建议买入", "sell": "建议卖出",
            "hold": "建议观望", "skip": "建议跳过",
        }
        lines.append(f"## {action_cn.get(self.action, '建议观望')}: {self.stock_name}({self.stock_code})")
        lines.append(f"**综合评分**: {self.score:.1f}/100 | **置信度**: {self.confidence}")
        lines.append(f"**理由**: {self.reason}")
        lines.append("")

        if self.root_node and self.root_node.children:
            lines.append("### 分项明细")
            lines.append("| 维度 | 评分 | 方向 | 信号 |")
            lines.append("|------|------|------|------|")
            for child in self.root_node.children:
                if child.layer == Layer.SKILL.value:
                    cn = get_skill_cn_name(child.name)
                    score_str = f"{child.score:.0f}" if child.score is not None else "—"
                    lines.append(f"| {cn} | {score_str} | {child.direction} | {child.signal} |")
            lines.append("")

        if self.human_note:
            lines.append(f"> 💡 **人工复核提示**: {self.human_note}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "chain_id": self.chain_id, "stock_code": self.stock_code,
            "stock_name": self.stock_name, "action": self.action,
            "score": self.score, "confidence": self.confidence,
            "direction": self.direction, "reason": self.reason,
            "recommendation": self.recommendation,
            "success": self.success, "execution_id": self.execution_id,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.root_node:
            d["root_node"] = self.root_node.to_dict()
        return d


# ═══════════════════════════════════════════════════════════════
# 链路执行器
# ═══════════════════════════════════════════════════════════════

class ChainExecutor:
    """链路执行器。

    Usage:
        executor = ChainExecutor(chain_id="evaluate+stock", stock_code="600519")
        result = executor.execute(run_skill_fn=my_run_skill)
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
            weights = store.get_skill_weights(self.chain_id)
            if weights:
                logger.info("[ChainExecutor] 加载历史权重 %s: %s", self.chain_id, weights)
                return weights
        except Exception:
            pass

        # 无历史数据，从 skill 的出厂权重构建
        try:
            from app.agent.skills.registry import skill_registry
            skill_registry.discover()
            defaults = {}
            for step in self.chain_def.steps:
                sk = skill_registry.get(step.agent)
                if sk and hasattr(sk, "default_weight"):
                    defaults[step.agent] = sk.default_weight
            if defaults:
                logger.info("[ChainExecutor] 使用出厂权重 %s: %s", self.chain_id, defaults)
            return defaults
        except Exception:
            return {}

    def execute(
        self,
        run_skill_fn: Callable[[str, str, str, dict], tuple],
        context: Dict[str, Any] = None,
    ) -> DecisionResult:
        """执行链路。

        Args:
            run_skill_fn: 调用 Skill 的函数。
                签名: run_skill_fn(skill_name, stock_code, stock_name, context) -> (SkillReport, EvalNode)
            context: 传递给每个 skill 的上下文。

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

        # 构建决策
        result = self._build_decision(root, skill_reports)
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
    ) -> DecisionResult:
        """构建决策。"""
        result = DecisionResult(
            chain_id=self.chain_id,
            stock_code=self.stock_code,
            stock_name=self.stock_name,
        )

        # ── 分项打分 ──
        valid_items = []  # (score, weight)
        has_veto = False
        total_steps = len(skill_reports)
        valid_steps = 0
        missing_steps = 0
        gaps = []

        for report in skill_reports:
            if report.status == "ok" and report.score is not None:
                # 有效数据
                weight = self._skill_weights.get(report.skill_name, 1.0)
                valid_items.append((report.score, weight))
                valid_steps += 1

                # 否决检测
                if report.score <= VETO_SCORE:
                    has_veto = True
            elif report.status == "missing":
                missing_steps += 1
                gaps.append(f"{get_skill_cn_name(report.skill_name)}: 数据缺失")
            elif report.status == "failed":
                missing_steps += 1
                gaps.append(f"{get_skill_cn_name(report.skill_name)}: {report.error}")

        # ── 覆盖度 ──
        coverage_ratio = valid_steps / total_steps if total_steps > 0 else 0

        # ── 样本量 ──
        eval_stats = store.get_eval_stats(self.chain_id)
        evaluated_count = eval_stats.get("evaluated_decisions", 0)

        # ── 加权评分 ──
        if valid_items:
            total_weight = sum(w for _, w in valid_items)
            if total_weight > 0:
                weighted_score = sum(s * w for s, w in valid_items) / total_weight
                confidence_mult = _sample_confidence(evaluated_count)
                adjusted_score = 50 + (weighted_score - 50) * confidence_mult
                result.score = round(adjusted_score, 1)
            else:
                result.score = 50.0
        else:
            result.score = 50.0

        # ── 置信等级 ──
        if has_veto:
            result.confidence = "reject"
        elif coverage_ratio < 0.4:
            result.confidence = "low"
        else:
            valid_dirs = [r.direction for r in skill_reports
                          if r.status == "ok" and r.direction != "neutral"]
            if valid_dirs:
                bull = sum(1 for d in valid_dirs if d == "bullish")
                bear = sum(1 for d in valid_dirs if d == "bearish")
                consistency = max(bull, bear) / len(valid_dirs)
            else:
                consistency = 0

            if coverage_ratio >= 0.7 and consistency >= 0.7:
                result.confidence = "high"
            elif coverage_ratio >= 0.5 and consistency >= 0.5:
                result.confidence = "medium"
            else:
                result.confidence = "low"

        # ── 方向判断 ──
        direction = self._determine_direction(skill_reports, result.score)

        # ── 决策 ──
        if has_veto:
            result.action = "skip"
            result.reason = "存在否决项，不执行"
        elif coverage_ratio < COVERAGE_THRESHOLD:
            result.action = "hold"
            result.reason = f"覆盖度不足（{valid_steps}/{total_steps}），数据不充分"
        elif evaluated_count == 0:
            result.action = "hold"
            result.reason = "无评估历史数据，系统尚未运行过评估闭环"
        elif direction == "conflict":
            result.action = "hold"
            result.reason = "多空信号冲突，建议观望"
        elif direction == "bullish":
            result.action = "buy"
            result.reason = f"综合评分 {result.score:.1f}，方向看多"
        elif direction == "bearish":
            result.action = "sell"
            result.reason = f"综合评分 {result.score:.1f}，方向看空"
        else:
            result.action = "hold"
            result.reason = f"综合评分 {result.score:.1f}，方向中性"

        result.direction = direction if direction != "conflict" else "neutral"
        result.recommendation = {
            "buy": "建议买入", "sell": "建议卖出",
            "hold": "建议观望", "skip": "建议跳过",
        }.get(result.action, "建议观望")

        # 填充根节点
        root.score = result.score
        root.direction = result.direction
        root.action = result.action
        root.confidence = 0.8 if result.confidence == "high" else (0.5 if result.confidence == "medium" else 0.2)

        # ── 人工复核提示 ──
        notes = []
        if gaps:
            notes.append(f"缺失数据: {', '.join(gaps[:3])}")
        if coverage_ratio < 0.6:
            notes.append(f"覆盖度仅 {valid_steps}/{total_steps}，结论可靠性较低")
        if direction == "conflict":
            notes.append("多空信号冲突，建议结合盘面判断")
        result.human_note = "；".join(notes) if notes else ""

        return result

    def _determine_direction(
        self,
        skill_reports: List[SkillReport],
        score: float,
    ) -> str:
        """判断综合方向。"""
        valid = [r for r in skill_reports if r.status == "ok" and r.direction != "neutral"]

        if not valid:
            if score >= 65:
                return "bullish"
            elif score <= 35:
                return "bearish"
            return "neutral"

        bull = sum(1 for r in valid if r.direction == "bullish")
        bear = sum(1 for r in valid if r.direction == "bearish")

        total = bull + bear
        if total > 0:
            bull_ratio = bull / total
            if 0.4 <= bull_ratio <= 0.6:
                return "conflict"

        if bull > bear:
            return "bullish"
        elif bear > bull:
            return "bearish"
        return "neutral"
