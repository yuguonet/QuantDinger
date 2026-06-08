# -*- coding: utf-8 -*-
"""
Schema — 三层统一评估树数据结构。

核心设计：
  一棵树 = 一个根节点(chain) + N 个子节点(skill) + N*N 个叶子节点(tool)
  用 parent_id 自引用，一张 qd_evaluations 表存整棵树。

层次：
  chain  — "大领导/CEO"，理解意图、组装链路、汇总评分、输出决策
  skill  — "部门领导"，调用 Tools 获取数据、做专业分析、输出标准化报告
  tool   — "干活的"，取数据、算指标、跑回测，搬运工不做校验

原则：
  - 内容主体 + 叠加buff：分数不是唯一产出，分析文字/推理过程/因子明细才是核心
  - 正向不校验，回溯才验证
  - 三层各负其责，互不背锅
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════

class Layer(str, Enum):
    """评估树层级。"""
    CHAIN = "chain"
    SKILL = "skill"
    TOOL = "tool"


class Status(str, Enum):
    """节点状态。"""
    OK = "ok"
    MISSING = "missing"
    FAILED = "failed"
    SKIPPED = "skipped"
    VETO = "veto"


class Action(str, Enum):
    """决策动作（仅 chain 层）。"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    SKIP = "skip"


class Direction(str, Enum):
    """方向判断。"""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


# ═══════════════════════════════════════════════════════════════
# 因子项（Skill 层输出的评分明细）
# ═══════════════════════════════════════════════════════════════

@dataclass
class FactorItem:
    """单个因子的评分结果。"""
    name: str
    value: str = ""
    score: Optional[float] = None   # 0-100, None=缺失
    weight: float = 1.0
    status: str = "ok"              # ok / missing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "value": self.value,
            "score": self.score, "weight": self.weight, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FactorItem":
        return cls(
            name=d.get("name", ""),
            value=str(d.get("value", "")),
            score=d.get("score"),
            weight=d.get("weight", 1.0),
            status=d.get("status", "ok"),
        )


# ═══════════════════════════════════════════════════════════════
# SkillReport — Skill 层标准化输出
# ═══════════════════════════════════════════════════════════════

@dataclass
class SkillReport:
    """Skill 的标准化输出。

    内容主体 + 叠加buff：
    - 内容主体: analysis 文字、factors 因子明细、output_data 完整报告
    - 叠加buff: score / confidence / direction / signal
    """
    skill_name: str
    # 叠加buff
    score: float = 50.0             # 0-100, 50=中性
    confidence: float = 0.0         # 0.0-1.0, 数据充分度
    direction: str = "neutral"      # bullish / bearish / neutral
    signal: str = ""                # 一句话信号
    # 内容主体
    factors: List[FactorItem] = field(default_factory=list)
    analysis: str = ""              # 分析文字（核心内容）
    output_data: Dict[str, Any] = field(default_factory=dict)
    # 调用记录
    tools_called: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    # 状态
    status: str = "ok"              # ok / missing / failed
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "score": self.score, "confidence": self.confidence,
            "direction": self.direction, "signal": self.signal,
            "factors": [f.to_dict() for f in self.factors],
            "analysis": self.analysis, "output_data": self.output_data,
            "tools_called": self.tools_called, "missing_data": self.missing_data,
            "status": self.status, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillReport":
        return cls(
            skill_name=d.get("skill_name", ""),
            score=d.get("score", 50.0),
            confidence=d.get("confidence", 0.0),
            direction=d.get("direction", "neutral"),
            signal=d.get("signal", ""),
            factors=[FactorItem.from_dict(f) for f in d.get("factors", [])],
            analysis=d.get("analysis", ""),
            output_data=d.get("output_data", {}),
            tools_called=d.get("tools_called", []),
            missing_data=d.get("missing_data", []),
            status=d.get("status", "ok"),
            error=d.get("error", ""),
        )


# ═══════════════════════════════════════════════════════════════
# EvalNode — 评估树节点（qd_evaluations 表的一行）
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvalNode:
    """评估树节点。

    三层统一结构，对应 qd_evaluations 表的一行。
    用 parent_id 构建树形关系。
    """
    # 身份
    id: Optional[int] = None
    parent_id: Optional[int] = None
    root_id: Optional[int] = None
    layer: str = Layer.CHAIN.value     # chain / skill / tool
    name: str = ""                     # chain_id / skill_name / tool_name
    step_order: int = 0                # 在父节点中的执行顺序

    # 时间/标的（根节点填写，子节点继承）
    exec_date: Optional[date] = None
    stock_code: str = ""
    stock_name: str = ""

    # 正向: 内容主体 + 数值buff叠加
    score: Optional[float] = None      # 0-100 叠加buff
    direction: str = ""                # bullish / bearish / neutral
    action: str = ""                   # buy / sell / hold / skip（仅 chain 层）
    signal: str = ""                   # 一句话信号
    confidence: Optional[float] = None # 0.0-1.0 叠加buff

    # 正向: 内容（全量记录）
    factors: List[FactorItem] = field(default_factory=list)
    output_data: Dict[str, Any] = field(default_factory=dict)
    analysis: str = ""                 # 分析文字（内容主体）

    # 正向: 调用信息
    input_params: Dict[str, Any] = field(default_factory=dict)
    tools_called: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    data_source: str = ""              # tool 实际命中的数据源

    # 执行信息
    status: str = Status.OK.value
    error: str = ""
    elapsed_ms: float = 0.0

    # 反向: 回测验证（回溯时写入）
    actual_return_1d: Optional[float] = None
    actual_return_3d: Optional[float] = None
    actual_return_5d: Optional[float] = None
    actual_direction_3d: str = ""
    correct_3d: Optional[bool] = None
    calibration: float = 1.0

    # 人工介入
    human_reviewed: bool = False
    human_verdict: str = ""

    # 子节点（内存中构建，不直接存储）
    children: List["EvalNode"] = field(default_factory=list)

    # ── 便捷属性 ──

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def is_valid(self) -> bool:
        return self.status == Status.OK.value and self.score is not None

    @property
    def is_veto(self) -> bool:
        return self.status == Status.VETO.value

    @property
    def skill_reports(self) -> List["EvalNode"]:
        """获取所有 skill 子节点。"""
        return [c for c in self.children if c.layer == Layer.SKILL.value]

    @property
    def tool_nodes(self) -> List["EvalNode"]:
        """获取所有 tool 子节点（递归）。"""
        result = []
        for c in self.children:
            if c.layer == Layer.TOOL.value:
                result.append(c)
            result.extend(c.tool_nodes)
        return result

    def get_skill_report(self, skill_name: str) -> Optional["EvalNode"]:
        """按名称查找 skill 子节点。"""
        for c in self.children:
            if c.layer == Layer.SKILL.value and c.name == skill_name:
                return c
        return None

    def add_child(self, child: "EvalNode"):
        """添加子节点。"""
        child.parent_id = self.id
        child.root_id = self.root_id or self.id
        child.exec_date = self.exec_date
        child.stock_code = self.stock_code
        child.stock_name = self.stock_name
        self.children.append(child)

    def to_skill_report(self) -> SkillReport:
        """将 skill 节点转为 SkillReport。"""
        return SkillReport(
            skill_name=self.name,
            score=self.score or 50.0,
            confidence=self.confidence or 0.0,
            direction=self.direction or "neutral",
            signal=self.signal,
            factors=list(self.factors),
            analysis=self.analysis,
            output_data=self.output_data,
            tools_called=list(self.tools_called),
            missing_data=list(self.missing_data),
            status=self.status,
            error=self.error,
        )

    # ── 序列化 ──

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id, "parent_id": self.parent_id, "root_id": self.root_id,
            "layer": self.layer, "name": self.name, "step_order": self.step_order,
            "exec_date": self.exec_date.isoformat() if self.exec_date else None,
            "stock_code": self.stock_code, "stock_name": self.stock_name,
            "score": self.score, "direction": self.direction, "action": self.action,
            "signal": self.signal, "confidence": self.confidence,
            "factors": [f.to_dict() for f in self.factors],
            "output_data": self.output_data, "analysis": self.analysis,
            "input_params": self.input_params,
            "tools_called": self.tools_called,
            "missing_data": self.missing_data, "data_source": self.data_source,
            "status": self.status, "error": self.error, "elapsed_ms": self.elapsed_ms,
            "actual_return_1d": self.actual_return_1d,
            "actual_return_3d": self.actual_return_3d,
            "actual_return_5d": self.actual_return_5d,
            "actual_direction_3d": self.actual_direction_3d,
            "correct_3d": self.correct_3d, "calibration": self.calibration,
            "human_reviewed": self.human_reviewed, "human_verdict": self.human_verdict,
        }
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvalNode":
        """从字典构建（不含 children，children 由 store.load_tree 处理）。"""
        exec_date = d.get("exec_date")
        if isinstance(exec_date, str):
            from datetime import date as _date
            try:
                exec_date = _date.fromisoformat(exec_date)
            except (ValueError, TypeError):
                exec_date = None

        return cls(
            id=d.get("id"), parent_id=d.get("parent_id"), root_id=d.get("root_id"),
            layer=d.get("layer", Layer.CHAIN.value),
            name=d.get("name", ""), step_order=d.get("step_order", 0),
            exec_date=exec_date,
            stock_code=d.get("stock_code", ""), stock_name=d.get("stock_name", ""),
            score=d.get("score"), direction=d.get("direction", ""),
            action=d.get("action", ""), signal=d.get("signal", ""),
            confidence=d.get("confidence"),
            factors=[FactorItem.from_dict(f) for f in d.get("factors", [])],
            output_data=d.get("output_data", {}), analysis=d.get("analysis", ""),
            input_params=d.get("input_params", {}),
            tools_called=d.get("tools_called", []),
            missing_data=d.get("missing_data", []),
            data_source=d.get("data_source", ""),
            status=d.get("status", Status.OK.value),
            error=d.get("error", ""), elapsed_ms=d.get("elapsed_ms", 0.0),
            actual_return_1d=d.get("actual_return_1d"),
            actual_return_3d=d.get("actual_return_3d"),
            actual_return_5d=d.get("actual_return_5d"),
            actual_direction_3d=d.get("actual_direction_3d", ""),
            correct_3d=d.get("correct_3d"), calibration=d.get("calibration", 1.0),
            human_reviewed=d.get("human_reviewed", False),
            human_verdict=d.get("human_verdict", ""),
        )


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

# 技能中文名映射
SKILL_NAME_CN: Dict[str, str] = {
    "technical_agent": "技术面",
    "momentum_tracker": "动量分析",
    "indicator_agent": "指标信号",
    "intelligence_agent": "情报分析",
    "policy_analyst": "政策面",
    "hot_money_tracker": "游资追踪",
    "lockup_watcher": "解禁监控",
    "concept_tracker": "概念追踪",
    "market_data_agent": "大盘概览",
    "screening_agent": "选股验证",
    "backtest_agent": "策略回测",
    "bull_researcher": "多头论证",
    "bear_researcher": "空头反驳",
    "trading_agent": "交易执行",
    "data_engineer": "数据工程",
    "analysis_agent": "综合分析",
}

# 否决阈值
VETO_SCORE: float = -1000.0

# 覆盖度最低阈值
COVERAGE_THRESHOLD: float = 0.4

# 方向判定阈值（收益率 > 0.3% 才算有方向）
DIRECTION_THRESHOLD: float = 0.003


def get_skill_cn_name(skill_name: str) -> str:
    """获取技能中文名。"""
    return SKILL_NAME_CN.get(skill_name, skill_name)


def classify_return(ret: float, threshold: float = DIRECTION_THRESHOLD) -> str:
    """根据收益率判断方向。"""
    if ret > threshold:
        return Direction.BULLISH.value
    elif ret < -threshold:
        return Direction.BEARISH.value
    return Direction.NEUTRAL.value


def is_direction_correct(predicted: str, actual: str) -> Optional[str]:
    """判断方向预测是否正确（三值）。

    返回:
        "correct" — 方向一致
        "wrong"   — 方向相反（bullish→bearish 或 bearish→bullish）
        "neutral" — 预测为 neutral（放弃判断），不参与奖惩
        None      — 数据缺失，无法判断
    """
    if not predicted or not actual:
        return None
    # neutral = 放弃判断，不参与奖惩
    if predicted == Direction.NEUTRAL.value:
        return "neutral"
    if predicted == actual:
        return "correct"
    return "wrong"
