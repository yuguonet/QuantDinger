# -*- coding: utf-8 -*-
"""
Decision Schema — 链路决策评估系统的标准化数据结构。

设计原则：
1. 价格折扣一切 — K线是唯一能回测验证的地基
2. 分项记分制 — 每个子项0-100打分，-1000否决，加权求和
3. 缺失≠0 — 缺失项从权重池剔除，剩余项归一化
4. 决策输出 — 最终输出是可执行的决策建议，不是报告
5. 每个组件可验证可迭代 — 工具/步骤/链路都有闭环评估

数据流：
  executor._execute_step() → StepOutput（每步结果）
  executor._build_decision_card() → DecisionCard（最终决策卡）
  evaluator.evaluate_pending() → qd_agent_decision_results（T+N 验证）
  evaluator.update_factor_weights() → qd_agent_factor_weights（因子权重）

枚举：
  StepStatus  — 步骤执行状态（ok/missing/failed/skipped/veto）
  Action      — 决策动作（buy/sell/hold/skip）
  Confidence  — 置信等级（high/medium/low/reject）
  Direction   — 方向判断（bullish/bearish/neutral）

核心数据结构：
  FactorScore     — 单因子评分
  StepOutput      — 步骤标准化输出（从 skill 解析）
  Coverage        — 覆盖度指标
  BreakdownItem   — 决策卡分项明细
  Gap             — 缺失项描述
  Blockers        — 决策阻断器（has_veto / coverage_too_low / sample_too_low）
  Decision        — 最终决策（execute / action / reason）
  DecisionSummary — 决策摘要（score / coverage / confidence）
  DecisionCard    — 完整决策卡（汇总所有结构）

常量：
  VETO_SCORE = -1000         — 否决阈值
  COVERAGE_THRESHOLD = 0.4   — 覆盖度最低阈值
  STEP_NAME_CN               — 步骤名→中文名映射
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════

class StepStatus(str, Enum):
    """步骤执行状态。"""
    OK = "ok"              # 正常完成
    MISSING = "missing"    # 数据缺失
    FAILED = "failed"      # 执行失败
    SKIPPED = "skipped"    # 被跳过
    VETO = "veto"          # 否决（-1000分，终止决策）


class Action(str, Enum):
    """决策动作。"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    SKIP = "skip"


class Confidence(str, Enum):
    """置信等级。"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    REJECT = "reject"


class Direction(str, Enum):
    """方向判断。"""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


# ═══════════════════════════════════════════════════════════════
# 核心数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class FactorScore:
    """单个因子的评分结果。"""
    name: str                       # 因子名，如 "MACD金叉"
    value: str = ""                 # 因子值，如 "DIF=0.5"
    score: Optional[float] = None   # 0-100 评分，None 表示缺失
    status: str = "ok"              # ok / missing
    detail: str = ""                # 补充说明

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "score": self.score,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class StepOutput:
    """单个步骤的标准化输出（从 skill 解析）。"""
    step_name: str                          # 步骤名
    agent_name: str = ""                    # agent 名
    status: StepStatus = StepStatus.OK      # 执行状态
    direction: Direction = Direction.NEUTRAL
    confidence: float = 0.0                 # 0.0-1.0
    score: Optional[float] = None           # 0-100，None=缺失
    signal: str = ""                        # 一句话信号摘要
    factors: List[FactorScore] = field(default_factory=list)
    raw_output: str = ""                    # 原始输出
    error: str = ""                         # 错误信息
    elapsed_ms: float = 0.0                 # 耗时
    tools_called: List[str] = field(default_factory=list)
    tools_detail: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """步骤是否提供了有效数据（非缺失、非失败、非跳过）。"""
        return self.status == StepStatus.OK and self.score is not None

    @property
    def is_veto(self) -> bool:
        """是否被否决。"""
        return self.status == StepStatus.VETO

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_name": self.step_name,
            "agent_name": self.agent_name,
            "status": self.status.value,
            "direction": self.direction.value,
            "confidence": self.confidence,
            "score": self.score,
            "signal": self.signal,
            "factors": [f.to_dict() for f in self.factors],
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "tools_called": self.tools_called,
        }


@dataclass
class Coverage:
    """覆盖度指标。"""
    total_steps: int = 0        # 总步骤数
    valid_steps: int = 0        # 有数据的步骤数
    missing_steps: int = 0      # 缺失的步骤数
    failed_steps: int = 0       # 失败的步骤数
    veto_steps: int = 0         # 被否决的步骤数

    @property
    def ratio(self) -> float:
        """覆盖度比率 0-1。"""
        if self.total_steps == 0:
            return 0.0
        return self.valid_steps / self.total_steps

    @property
    def label(self) -> str:
        """覆盖度标签。"""
        return f"{self.valid_steps}/{self.total_steps}项有数据"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total_steps,
            "valid": self.valid_steps,
            "missing": self.missing_steps,
            "failed": self.failed_steps,
            "veto": self.veto_steps,
            "ratio": round(self.ratio, 3),
            "label": self.label,
        }


@dataclass
class BreakdownItem:
    """决策卡中的分项明细。"""
    name: str                       # 步骤名（中文）
    score: Optional[float] = None   # 0-100 评分
    weight: float = 1.0             # 权重
    weighted: Optional[float] = None  # 加权分
    status: str = "ok"              # ok/missing/failed/skipped/veto
    signal: str = ""                # 信号摘要
    reason: str = ""                # 缺失/失败原因

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "score": self.score,
            "weight": round(self.weight, 4),
            "weighted": self.weighted,
            "status": self.status,
            "signal": self.signal,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


@dataclass
class Gap:
    """缺失项描述。"""
    what: str       # 缺失什么
    why: str        # 为什么缺失
    impact: str     # 对决策的影响

    def to_dict(self) -> Dict[str, Any]:
        return {"what": self.what, "why": self.why, "impact": self.impact}


@dataclass
class Blockers:
    """决策阻断器。

    三重门控（优先级从高到低）：
      1. has_veto — 一票否决（SKIP）
      2. coverage_too_low — 覆盖度 < 40%（HOLD）
      3. sample_too_low — 无评估历史数据（HOLD）
         注意：低样本量不再硬阻断，改为通过 confidence_multiplier 渐进打折。
         仅当 evaluated_count == 0 时才触发此阻断。
    """
    has_veto: bool = False              # 是否有否决项
    coverage_too_low: bool = False      # 覆盖度是否过低
    sample_too_low: bool = False        # 是否完全无评估历史数据

    @property
    def blocked(self) -> bool:
        return self.has_veto or self.coverage_too_low or self.sample_too_low

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_veto": self.has_veto,
            "coverage_too_low": self.coverage_too_low,
            "sample_too_low": self.sample_too_low,
        }


@dataclass
class Decision:
    """最终决策。"""
    execute: bool = False           # 是否执行
    action: Action = Action.HOLD    # 动作
    reason: str = ""                # 决策理由
    fallback_action: Action = Action.HOLD  # 兜底动作

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execute": self.execute,
            "action": self.action.value,
            "reason": self.reason,
            "fallback_action": self.fallback_action.value,
        }


@dataclass
class DecisionSummary:
    """决策摘要。"""
    score: float = 0.0              # 最终加权得分
    coverage: str = ""              # 覆盖度标签
    confidence: str = "low"         # 置信等级
    key_signal: str = ""            # 关键信号

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "coverage": self.coverage,
            "confidence": self.confidence,
            "key_signal": self.key_signal,
        }


@dataclass
class DecisionCard:
    """最终决策卡 — 完整的决策输出结构。"""
    recommendation: str = ""                    # 中文建议
    action: Action = Action.HOLD                # 动作
    stock_code: str = ""
    stock_name: str = ""
    chain_id: str = ""
    summary: DecisionSummary = field(default_factory=DecisionSummary)
    breakdown: List[BreakdownItem] = field(default_factory=list)
    gaps: List[Gap] = field(default_factory=list)
    blockers: Blockers = field(default_factory=Blockers)
    decision: Decision = field(default_factory=Decision)
    human_note: str = ""                        # 人工复核提示

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "action": self.action.value,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "chain_id": self.chain_id,
            "summary": self.summary.to_dict(),
            "breakdown": [b.to_dict() for b in self.breakdown],
            "gaps": [g.to_dict() for g in self.gaps],
            "blockers": self.blockers.to_dict(),
            "decision": self.decision.to_dict(),
            "human_note": self.human_note,
        }

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_markdown(self) -> str:
        """生成可读的 Markdown 报告。"""
        lines = []
        action_cn = {
            Action.BUY: "建议买入", Action.SELL: "建议卖出",
            Action.HOLD: "建议观望", Action.SKIP: "建议跳过",
        }
        lines.append(f"## {action_cn.get(self.action, '建议观望')}: {self.stock_name}({self.stock_code})")
        lines.append(f"**综合评分**: {self.summary.score:.1f}/100 | "
                      f"**覆盖度**: {self.summary.coverage} | "
                      f"**置信度**: {self.summary.confidence}")
        if self.summary.key_signal:
            lines.append(f"**关键信号**: {self.summary.key_signal}")
        lines.append("")

        if self.breakdown:
            lines.append("### 分项明细")
            lines.append("| 维度 | 评分 | 权重 | 加权分 | 状态 | 信号 |")
            lines.append("|------|------|------|--------|------|------|")
            for b in self.breakdown:
                score_str = f"{b.score:.0f}" if b.score is not None else "—"
                weighted_str = f"{b.weighted:.1f}" if b.weighted is not None else "—"
                lines.append(f"| {b.name} | {score_str} | {b.weight:.2f} | {weighted_str} | {b.status} | {b.signal} |")
            lines.append("")

        if self.gaps:
            lines.append("### 数据缺口")
            for g in self.gaps:
                lines.append(f"- **{g.what}**: {g.why} → {g.impact}")
            lines.append("")

        if self.human_note:
            lines.append(f"> 💡 **人工复核提示**: {self.human_note}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

# 步骤名 → 中文名映射
STEP_NAME_CN: Dict[str, str] = {
    "policy": "政策面",
    "hot_money": "游资追踪",
    "lockup": "解禁监控",
    "concept": "概念追踪",
    "momentum": "动量分析",
    "intelligence": "情报分析",
    "technical": "技术面",
    "indicator": "指标信号",
    "screening": "选股验证",
    "fund_flow": "资金流向",
    "backtest": "策略回测",
    "bull_bear_debate": "多头论证",
    "bear_rebuttal": "空头反驳",
    "market_overview": "大盘概览",
    "hotspots": "热点追踪",
}

# 默认步骤权重（均匀分配）
DEFAULT_STEP_WEIGHT: float = 1.0

# 否决阈值
VETO_SCORE: float = -1000.0

# 覆盖度最低阈值
COVERAGE_THRESHOLD: float = 0.4


def get_step_cn_name(step_name: str) -> str:
    """获取步骤的中文名。"""
    return STEP_NAME_CN.get(step_name, step_name)
