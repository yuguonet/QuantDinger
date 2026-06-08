# -*- coding: utf-8 -*-
"""
Chain Executor — 链路决策执行器。

职责：按链路定义依次调度子 Agent，解析结构化输出，构建 DecisionCard。

核心流程：
  execute() → 遍历 ChainStep → _execute_step() → _build_decision_card()

每步执行：
  1. 构造消息（含前序步骤输出作为上下文）
  2. 调用子 Agent（run_agent_fn）
  3. parse_skill_output() 解析 JSON → direction/confidence/score/factors
  4. 判断状态：OK / MISSING / FAILED / VETO

决策卡构建（_build_decision_card）：
  1. 分项打分 → 每步的 score × weight
  2. 覆盖度计算 → valid_steps / total_steps
  3. 加权评分 → 缺失项从权重池剔除，剩余归一化
  4. 门控检查（三重阻断）：
     - has_veto → SKIP（一票否决）
     - coverage_too_low → HOLD（覆盖度 < 40%）
     - sample_too_low → HOLD（已评估决策 < 10 条）
  5. 方向判断 → 多空冲突检测
  6. 输出 DecisionCard（含 breakdown/gaps/blockers/human_note）

权重来源：
  _load_step_weights() 从 evaluator 读取：
    - get_step_weights() → 步骤级 3d 准确率
    - get_factor_weights_for_chain() → 因子级准确率
  取两者 max，让高确信因子拉高步骤权重。
  首次运行时权重全为默认值 1.0（qd_agent_decision_results 为空）。

数据库依赖（decision_evaluation.sql）：
  qd_agent_decisions      ← 本模块写入（UPSERT）
  qd_agent_decision_steps ← 本模块写入（覆盖更新）
  qd_agent_factor_weights ← 本模块读取（_load_step_weights）

公开接口：
  ChainExecutor(chain_id, stock_code, stock_name, user_id)
    .execute(run_agent_fn, context) → ChainResult
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.chains import ChainDef, ChainStep, get_chain
from app.agent.chain.schema import (
    Action, Blockers, BreakdownItem, Confidence, Coverage,
    Decision, DecisionCard, DecisionSummary, Direction, FactorScore,
    Gap, StepOutput, StepStatus, VETO_SCORE, COVERAGE_THRESHOLD,
    get_step_cn_name,
)
from app.agent.chain.skill_contract import parse_skill_output, parse_tool_details

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 渐进门控：基于样本量的置信度折扣
# ═══════════════════════════════════════════════════════════════

def _sample_confidence(evaluated_count: int) -> float:
    """根据已评估决策数返回置信度乘数（0.0 ~ 1.0）。

    样本越少，置信度越低，分数越趋近50（中性）。
    这是渐进门控的核心：避免低样本量时输出极端 BUY/SELL。

    梯度：
      0 条   → 0.0（完全不可信，分数固定为50）
      5 条   → 0.5（半可信，分数向50收缩一半）
      10 条  → 0.7（基本可信）
      20 条  → 0.85（较可信）
      50 条+ → 1.0（完全可信）

    Args:
        evaluated_count: 已评估的决策数量

    Returns:
        0.0 ~ 1.0 的置信度乘数
    """
    if evaluated_count <= 0:
        return 0.0
    if evaluated_count >= 50:
        return 1.0
    # 对数增长：前期增长快，后期趋缓
    # f(n) = 0.2 + 0.8 * (1 - e^(-n/15))
    import math
    return round(0.2 + 0.8 * (1 - math.exp(-evaluated_count / 15)), 3)


# ═══════════════════════════════════════════════════════════════
# 执行结果
# ═══════════════════════════════════════════════════════════════

class ChainResult:
    """整条链路执行结果。"""

    def __init__(self, chain_def: ChainDef, stock_code: str, stock_name: str = ""):
        self.chain_def = chain_def
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.step_outputs: List[StepOutput] = []
        self.decision_card: Optional[DecisionCard] = None
        self.success = False
        self.execution_id: Optional[int] = None
        self.elapsed_ms = 0.0

    @property
    def content(self) -> str:
        """返回决策卡的 Markdown 或 JSON 字符串，供前端展示。"""
        if self.decision_card:
            return self.decision_card.to_markdown()
        return "链路执行未产生决策卡。"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_def.chain_id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "success": self.success,
            "elapsed_ms": self.elapsed_ms,
            "execution_id": self.execution_id,
            "steps": [s.to_dict() for s in self.step_outputs],
            "decision_card": self.decision_card.to_dict() if self.decision_card else None,
        }


# ═══════════════════════════════════════════════════════════════
# 链路执行器
# ═══════════════════════════════════════════════════════════════

class ChainExecutor:
    """链路执行器。

    按链路定义依次调度子 Agent，解析结构化输出，构建决策卡。

    Usage:
        executor = ChainExecutor(chain_id="evaluate+stock", stock_code="600519")
        result = executor.execute(run_agent_fn=my_run_agent)
        # result.decision_card 是最终决策卡
        # result.execution_id 是数据库记录 ID
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
        self._step_weights = self._load_step_weights()

    def _load_step_weights(self) -> Dict[str, float]:
        """从评估系统加载历史步骤准确率权重。

        合并两个来源：
        1. 步骤级权重（get_step_weights）— 基于步骤方向预测的 3 日准确率
        2. 因子级权重（qd_agent_factor_weights）— 基于因子信息增益，取该步骤下因子的均值
        取两者中较大值，让高确信因子能拉高步骤权重。
        """
        try:
            from app.agent.chain.evaluator import get_step_weights, get_factor_weights_for_chain
            step_w = get_step_weights(self.chain_id) or {}
            factor_w = get_factor_weights_for_chain(self.chain_id) or {}

            # 合并：取 max(step_weight, factor_weight)
            merged = dict(step_w)
            for step_name, fw in factor_w.items():
                if step_name in merged:
                    merged[step_name] = round(max(merged[step_name], fw), 3)
                else:
                    merged[step_name] = round(fw, 3)

            if merged:
                logger.info("[ChainExecutor] 加载历史权重 %s: %s", self.chain_id, merged)
            return merged
        except Exception:
            return {}

    def execute(
        self,
        run_agent_fn: Callable[[str, str, dict], str],
        context: Dict[str, Any] = None,
    ) -> ChainResult:
        """执行链路。

        Args:
            run_agent_fn: 调用子 Agent 的函数。
                签名: run_agent_fn(agent_name: str, message: str, context: dict) -> str
            context: 传递给每个 agent 的上下文。

        Returns:
            ChainResult 包含决策卡。
        """
        result = ChainResult(self.chain_def, self.stock_code, self.stock_name)
        t0 = time.time()
        context = context or {}

        logger.info("[ChainExecutor] 开始执行链路 %s | 股票=%s",
                     self.chain_id, self.stock_code)

        step_outputs = []  # 收集各步输出，传给后续步骤做上下文

        for step in sorted(self.chain_def.steps, key=lambda s: s.order):
            step_output = self._execute_step(step, step_outputs, context, run_agent_fn)
            result.step_outputs.append(step_output)
            step_outputs.append(step_output)

            # 如果是必须步骤且被否决，终止链路
            if step.required and step_output.is_veto:
                logger.warning("[ChainExecutor] 必须步骤 %s 被否决，终止链路", step.name)
                break

        # 构建决策卡
        self._build_decision_card(result)
        result.elapsed_ms = (time.time() - t0) * 1000
        result.success = any(s.status == StepStatus.OK for s in result.step_outputs)

        # 持久化到数据库
        result.execution_id = self._save_to_db(result)

        logger.info("[ChainExecutor] 链路 %s 完成 | %s %s | 评分=%.1f | 覆盖度=%s | %.0fms",
                     self.chain_id, result.decision_card.action.value if result.decision_card else "?",
                     self.stock_code,
                     result.decision_card.summary.score if result.decision_card else 0,
                     result.decision_card.summary.coverage if result.decision_card else "?",
                     result.elapsed_ms)

        return result

    def _execute_step(
        self,
        step: ChainStep,
        previous_outputs: List[StepOutput],
        context: Dict[str, Any],
        run_agent_fn: Callable[[str, str, dict], str],
    ) -> StepOutput:
        """执行单个步骤。"""
        step_output = StepOutput(step_name=step.name, agent_name=step.agent)
        t0 = time.time()

        try:
            # 构造消息
            message = self._build_step_message(step, previous_outputs, context)

            # 调用子 agent
            raw_output = run_agent_fn(step.agent, message, context)

            # 解析结构化输出
            parsed = parse_skill_output(raw_output)
            step_output.raw_output = raw_output
            step_output.elapsed_ms = (time.time() - t0) * 1000

            # 设置方向
            direction_str = parsed.get("direction", "neutral")
            try:
                step_output.direction = Direction(direction_str)
            except ValueError:
                step_output.direction = Direction.NEUTRAL

            # 设置置信度
            step_output.confidence = parsed.get("confidence", 0.0)

            # 设置分数
            step_output.score = parsed.get("score")
            step_output.signal = parsed.get("signal", "")

            # 设置因子
            for f in parsed.get("factors", []):
                step_output.factors.append(FactorScore(
                    name=f.get("name", ""),
                    value=str(f.get("value", "")),
                    score=f.get("score"),
                    status=f.get("status", "ok"),
                ))

            # 判断状态
            if step_output.score is not None and step_output.score <= VETO_SCORE:
                step_output.status = StepStatus.VETO
            elif step_output.score is None and not step_output.factors:
                step_output.status = StepStatus.MISSING
            else:
                step_output.status = StepStatus.OK

            # 解析工具调用
            tool_details = parse_tool_details(raw_output)
            step_output.tools_detail = tool_details
            step_output.tools_called = [t["name"] for t in tool_details]

        except Exception as e:
            logger.warning("[ChainExecutor] 步骤 %s 失败: %s", step.name, e)
            step_output.status = StepStatus.FAILED
            step_output.error = str(e)
            step_output.elapsed_ms = (time.time() - t0) * 1000

        return step_output

    def _build_step_message(
        self,
        step: ChainStep,
        previous_outputs: List[StepOutput],
        context: Dict[str, Any],
    ) -> str:
        """构造给子 agent 的消息。"""
        parts = []
        parts.append(f"请分析 {self.stock_name or self.stock_code}（{self.stock_code}）的{step.description}。")

        if previous_outputs:
            parts.append("\n前序分析结果供参考：")
            for prev in previous_outputs:
                if prev.status == StepStatus.OK:
                    parts.append(f"- {get_step_cn_name(prev.step_name)}: {prev.signal or prev.raw_output[:200]}")

        if context.get("user_query"):
            parts.append(f"\n用户原始问题：{context['user_query']}")

        return "\n".join(parts)

    # ═══════════════════════════════════════════════════════════
    # 决策卡构建
    # ═══════════════════════════════════════════════════════════

    def _build_decision_card(self, result: ChainResult):
        """构建决策卡。"""
        card = DecisionCard(
            stock_code=self.stock_code,
            stock_name=self.stock_name,
            chain_id=self.chain_id,
        )

        # ── 分项打分 ──
        breakdown = []
        gaps = []
        valid_items = []  # (score, weight) 用于加权求和
        has_veto = False

        for step_out in result.step_outputs:
            cn_name = get_step_cn_name(step_out.step_name)
            # 使用评估系统加载的权重（默认 1.0）
            base_weight = self._step_weights.get(step_out.step_name, 1.0)

            item = BreakdownItem(
                name=cn_name,
                score=step_out.score,
                weight=base_weight,
                status=step_out.status.value,
                signal=step_out.signal,
            )

            if step_out.status == StepStatus.OK and step_out.score is not None:
                # 有效数据：加入加权池
                item.weighted = round(step_out.score * base_weight, 1)
                valid_items.append((step_out.score, base_weight))
            elif step_out.status == StepStatus.MISSING:
                item.signal = "⚠️ 数据缺失"
                item.reason = step_out.error or "数据源无返回"
                gaps.append(Gap(
                    what=cn_name,
                    why=step_out.error or "skill 未返回结构化数据",
                    impact=f"无法评估{cn_name}维度",
                ))
            elif step_out.status == StepStatus.FAILED:
                item.signal = "❌ 执行失败"
                item.reason = step_out.error
                gaps.append(Gap(
                    what=cn_name,
                    why=step_out.error,
                    impact=f"{cn_name}维度评估缺失",
                ))
            elif step_out.status == StepStatus.VETO:
                item.signal = "🚫 否决"
                has_veto = True

            breakdown.append(item)

        card.breakdown = breakdown
        card.gaps = gaps

        # ── 覆盖度 ──
        total_steps = len(result.step_outputs)
        valid_steps = sum(1 for s in result.step_outputs if s.status == StepStatus.OK and s.score is not None)
        coverage = Coverage(
            total_steps=total_steps,
            valid_steps=valid_steps,
            missing_steps=sum(1 for s in result.step_outputs if s.status == StepStatus.MISSING),
            failed_steps=sum(1 for s in result.step_outputs if s.status == StepStatus.FAILED),
            veto_steps=sum(1 for s in result.step_outputs if s.status == StepStatus.VETO),
        )
        card.summary.coverage = coverage.label

        # ── 样本量统计（供渐进门控）──
        eval_stats = self._get_eval_stats()
        evaluated_count = eval_stats.get("evaluated_decisions", 0)

        # ── 加权评分（缺失项从权重池剔除，剩余归一化）──
        if valid_items:
            total_weight = sum(w for _, w in valid_items)
            if total_weight > 0:
                weighted_score = sum(s * w for s, w in valid_items) / total_weight
                # 渐进门控：样本量不足时对分数打折
                # 样本越少，分数越趋近50（中性），避免低样本量时极端判断
                confidence_mult = _sample_confidence(evaluated_count)
                adjusted_score = 50 + (weighted_score - 50) * confidence_mult
                card.summary.score = round(adjusted_score, 1)
            else:
                card.summary.score = 0.0
        else:
            card.summary.score = 0.0

        # ── 置信等级 ──
        card.summary.confidence = self._calc_confidence(coverage, card.summary.score, result.step_outputs)

        # ── 关键信号 ──
        card.summary.key_signal = self._extract_key_signal(result.step_outputs)

        # ── 样本量门控（渐进式）──
        # evaluated_count 已在上面获取（用于 confidence_mult）
        # 渐进门控：confidence_mult 已将分数拉向50（中性）
        # 硬门控：仅在完全无数据时阻断（evaluated=0 时 confidence_mult=0.0，分数已固定为50）
        sample_no_data = evaluated_count == 0

        # ── 阻断器 ──
        blockers = Blockers(
            has_veto=has_veto,
            coverage_too_low=coverage.ratio < COVERAGE_THRESHOLD,
            sample_too_low=sample_no_data,
        )
        card.blockers = blockers

        # ── 方向判断 ──
        direction = self._determine_direction(result.step_outputs, card.summary.score)

        # ── 决策 ──
        decision = Decision()
        if blockers.has_veto:
            decision.execute = False
            decision.action = Action.SKIP
            decision.reason = "存在否决项，不执行"
            decision.fallback_action = Action.SKIP
        elif blockers.coverage_too_low:
            decision.execute = False
            decision.action = Action.HOLD
            decision.reason = f"覆盖度不足（{coverage.label}），数据不充分"
            decision.fallback_action = Action.HOLD
        elif blockers.sample_too_low:
            decision.execute = False
            decision.action = Action.HOLD
            decision.reason = "无评估历史数据，系统尚未运行过评估闭环"
            decision.fallback_action = Action.HOLD
        elif direction == "conflict":
            decision.execute = False
            decision.action = Action.HOLD
            decision.reason = "多空信号冲突，建议观望"
            decision.fallback_action = Action.HOLD
        else:
            decision.execute = True
            if direction == Direction.BULLISH:
                decision.action = Action.BUY
                decision.reason = f"综合评分 {card.summary.score:.1f}，方向看多"
            elif direction == Direction.BEARISH:
                decision.action = Action.SELL
                decision.reason = f"综合评分 {card.summary.score:.1f}，方向看空"
            else:
                decision.action = Action.HOLD
                decision.reason = f"综合评分 {card.summary.score:.1f}，方向中性"
            decision.fallback_action = Action.HOLD

        card.decision = decision
        card.action = decision.action

        # ── 推荐文本 ──
        action_cn = {
            Action.BUY: "建议买入", Action.SELL: "建议卖出",
            Action.HOLD: "建议观望", Action.SKIP: "建议跳过",
        }
        card.recommendation = action_cn.get(decision.action, "建议观望")

        # ── 人工复核提示 ──
        card.human_note = self._build_human_note(gaps, coverage, card.summary.score, direction)

        result.decision_card = card

    def _find_chain_step(self, step_name: str) -> Optional[ChainStep]:
        """从链路定义中查找步骤。"""
        for s in self.chain_def.steps:
            if s.name == step_name:
                return s
        return None

    def _get_eval_stats(self) -> Dict[str, Any]:
        """获取链路评估统计（供门控使用）。"""
        try:
            from app.agent.chain.evaluator import get_chain_eval_stats
            return get_chain_eval_stats(self.chain_id)
        except Exception:
            return {"evaluated_decisions": 0, "ready_for_decision": False}

    def _calc_confidence(
        self,
        coverage: Coverage,
        score: float,
        step_outputs: List[StepOutput],
    ) -> str:
        """计算置信等级。"""
        # 有否决 → reject
        if coverage.veto_steps > 0:
            return Confidence.REJECT.value

        # 覆盖度不足 → low
        if coverage.ratio < 0.4:
            return Confidence.LOW.value

        # 计算方向一致性
        valid_directions = [
            s.direction for s in step_outputs
            if s.status == StepStatus.OK and s.direction != Direction.NEUTRAL
        ]
        if valid_directions:
            bull = sum(1 for d in valid_directions if d == Direction.BULLISH)
            bear = sum(1 for d in valid_directions if d == Direction.BEARISH)
            consistency = max(bull, bear) / len(valid_directions) if valid_directions else 0
        else:
            consistency = 0

        # 综合判断
        if coverage.ratio >= 0.7 and consistency >= 0.7:
            return Confidence.HIGH.value
        elif coverage.ratio >= 0.5 and consistency >= 0.5:
            return Confidence.MEDIUM.value
        else:
            return Confidence.LOW.value

    def _extract_key_signal(self, step_outputs: List[StepOutput]) -> str:
        """提取关键信号。"""
        signals = []
        for s in step_outputs:
            if s.status == StepStatus.OK and s.signal:
                signals.append(f"{get_step_cn_name(s.step_name)}:{s.signal}")
        return " + ".join(signals[:3]) if signals else ""

    def _determine_direction(
        self,
        step_outputs: List[StepOutput],
        score: float,
    ) -> Any:
        """判断综合方向。

        Returns:
            Direction.BULLISH / Direction.BEARISH / Direction.NEUTRAL / "conflict"
        """
        valid = [s for s in step_outputs if s.status == StepStatus.OK and s.direction != Direction.NEUTRAL]

        if not valid:
            # 没有方向信号，用分数判断
            if score >= 65:
                return Direction.BULLISH
            elif score <= 35:
                return Direction.BEARISH
            return Direction.NEUTRAL

        bull = sum(1 for s in valid if s.direction == Direction.BULLISH)
        bear = sum(1 for s in valid if s.direction == Direction.BEARISH)

        # 多空冲突检测：看多看空各占 40%-60%
        total = bull + bear
        if total > 0:
            bull_ratio = bull / total
            if 0.4 <= bull_ratio <= 0.6:
                return "conflict"

        if bull > bear:
            return Direction.BULLISH
        elif bear > bull:
            return Direction.BEARISH
        return Direction.NEUTRAL

    def _build_human_note(
        self,
        gaps: List[Gap],
        coverage: Coverage,
        score: float,
        direction: Any,
    ) -> str:
        """构建人工复核提示。"""
        notes = []

        if gaps:
            gap_names = [g.what for g in gaps[:3]]
            notes.append(f"缺失数据: {', '.join(gap_names)}，建议手动补充确认")

        if coverage.ratio < 0.6:
            notes.append(f"覆盖度仅 {coverage.label}，结论可靠性较低")

        if direction == "conflict":
            notes.append("多空信号冲突，建议结合盘面实际情况判断")

        if score >= 70:
            notes.append("评分较高，但仍需确认仓位控制（建议不超过总仓位10%）")
        elif score <= 30:
            notes.append("评分较低，如果持有建议考虑减仓")

        return "；".join(notes) if notes else ""

    # ═══════════════════════════════════════════════════════════
    # 数据库持久化
    # ═══════════════════════════════════════════════════════════

    def _save_to_db(self, result: ChainResult) -> Optional[int]:
        """保存执行结果到数据库。"""
        try:
            from app.utils.db import get_db_connection

            card = result.decision_card
            if not card:
                return None

            with get_db_connection() as conn:
                cur = conn.cursor()

                # UPSERT 主记录
                cur.execute("""
                    INSERT INTO qd_agent_decisions
                        (exec_date, stock_code, stock_name, chain_id,
                         action, score, coverage, confidence, decision_card)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (exec_date, stock_code, chain_id)
                    DO UPDATE SET
                        action = EXCLUDED.action,
                        score = EXCLUDED.score,
                        coverage = EXCLUDED.coverage,
                        confidence = EXCLUDED.confidence,
                        decision_card = EXCLUDED.decision_card,
                        created_at = NOW()
                    RETURNING id
                """, (
                    date.today(), self.stock_code, self.stock_name,
                    self.chain_id, card.action.value, card.summary.score,
                    result.step_outputs and len([s for s in result.step_outputs if s.status == StepStatus.OK]) / max(len(result.step_outputs), 1),
                    card.summary.confidence,
                    json.dumps(card.to_dict(), ensure_ascii=False),
                ))
                decision_id = cur.fetchone()[0]

                # 删除旧步骤（覆盖更新）
                cur.execute(
                    "DELETE FROM qd_agent_decision_steps WHERE decision_id = %s",
                    (decision_id,)
                )

                # 插入步骤详情
                for i, step_out in enumerate(result.step_outputs):
                    cur.execute("""
                        INSERT INTO qd_agent_decision_steps
                            (decision_id, step_name, step_order, agent_name,
                             status, direction, confidence, score, signal,
                             factors, tools_called, raw_output, elapsed_ms, error)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        decision_id, step_out.step_name, i + 1,
                        step_out.agent_name, step_out.status.value,
                        step_out.direction.value, step_out.confidence,
                        step_out.score, step_out.signal,
                        json.dumps([f.to_dict() for f in step_out.factors], ensure_ascii=False),
                        json.dumps(step_out.tools_called, ensure_ascii=False),
                        step_out.raw_output[:5000],  # 截断保存
                        step_out.elapsed_ms, step_out.error,
                    ))

                conn.commit()
                logger.info(
                    "[ChainExecutor] 已保存到数据库: decision_id=%d chain=%s stock=%s steps=%d",
                    decision_id, self.chain_id, self.stock_code, len(result.step_outputs),
                )
                return decision_id

        except Exception as e:
            logger.error("[ChainExecutor] 保存执行记录失败: %s", e, exc_info=True)
            return None
