# -*- coding: utf-8 -*-
"""
Checkpoint Manager — 断点续传管理器。

每步执行后保存 checkpoint，支持从任意步恢复执行。
用于定位"从哪一步开始发生偏差"。

公开接口：
  CheckpointManager(session_id) → manager
  manager.save(step, ...) → None
  manager.load(step) → Checkpoint | None
  manager.list() → List[Checkpoint]
  manager.latest() → Checkpoint | None
  manager.clear() → None
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StepRecord:
    """单步执行记录。"""
    step: int
    # Planner 决策
    skill: Optional[str] = None
    description: str = ""
    tools: List[str] = field(default_factory=list)
    rules: str = ""
    planner_reasoning: str = ""
    # Agent 执行结果
    step_content: str = ""
    step_success: bool = False
    steps_used: int = 0
    step_tokens: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    charts: List[str] = field(default_factory=list)
    # Judge 审查
    judge_summary: str = ""
    judge_corrections: Optional[str] = None
    judge_continue: bool = True
    judge_reasoning: str = ""
    # 时间
    saved_at: float = 0.0


class LoopState:
    """Step Loop 全量状态。"""

    def __init__(self):
        # 用户问题
        self.query: str = ""
        self.stock_code: str = ""
        self.stock_name: str = ""
        # Planner 规划进度
        self.planned_steps: List[Dict[str, Any]] = []  # Planner 的完整规划 [{step, description, tools, status}]
        self.current_step: int = 0                     # 当前执行到第几步
        self.plan_complete: bool = False                # 规划是否完成
        # 执行记录
        self.step_records: List[StepRecord] = []
        # Judge 状态
        self.judge_veto: bool = False
        self.redo_from: int = -1
        self.judge_stop: bool = False
        self.final_output: Optional[Dict[str, Any]] = None
        # 累积统计
        self.total_steps: int = 0
        self.total_tokens: int = 0
        # 循环控制
        self.all_phases_completed: bool = False


class LoopStateStore:
    """基于 checkpoint 的循环状态管理器。"""

    def __init__(self, ckpt_mgr: CheckpointManager):
        self._mgr = ckpt_mgr
        self._state = LoopState()

    @property
    def state(self) -> LoopState:
        return self._state

    def save(self):
        """将当前状态写入 checkpoint（step=0 作为循环状态槽）。"""
        import json as _json
        data = {
            "query": self._state.query,
            "stock_code": self._state.stock_code,
            "stock_name": self._state.stock_name,
            "planned_steps": self._state.planned_steps,
            "current_step": self._state.current_step,
            "plan_complete": self._state.plan_complete,
            "step_records": [self._record_to_dict(r) for r in self._state.step_records],
            "judge_veto": self._state.judge_veto,
            "redo_from": self._state.redo_from,
            "judge_stop": self._state.judge_stop,
            "total_steps": self._state.total_steps,
            "total_tokens": self._state.total_tokens,
            "all_phases_completed": self._state.all_phases_completed,
        }
        self._mgr.save(
            step=0,
            skill=None, description="__loop_state__", tools=[], rules="",
            step_content=_json.dumps(data, ensure_ascii=False),
            step_success=False, steps_used=0,
            all_content=[], previous_results=[],
            total_steps=self._state.total_steps, total_tokens=self._state.total_tokens,
            stock_code=self._state.stock_code, stock_name=self._state.stock_name,
            intent_data={},
        )

    def load(self) -> bool:
        """从 checkpoint 恢复状态。返回 True 表示有恢复数据。"""
        import json as _json
        cp = self._mgr.load(0)
        if not cp or cp.description != "__loop_state__":
            return False
        try:
            data = _json.loads(cp.step_content)
        except (json.JSONDecodeError, TypeError):
            return False
        self._state.query = data.get("query", "")
        self._state.stock_code = data.get("stock_code", "")
        self._state.stock_name = data.get("stock_name", "")
        self._state.planned_steps = data.get("planned_steps", [])
        self._state.current_step = data.get("current_step", 0)
        self._state.plan_complete = data.get("plan_complete", False)
        self._state.step_records = [self._dict_to_record(r) for r in data.get("step_records", [])]
        self._state.judge_veto = data.get("judge_veto", False)
        self._state.redo_from = data.get("redo_from", -1)
        self._state.judge_stop = data.get("judge_stop", False)
        self._state.total_steps = data.get("total_steps", 0)
        self._state.total_tokens = data.get("total_tokens", 0)
        self._state.all_phases_completed = data.get("all_phases_completed", False)
        logger.info("[LoopState] 恢复: step=%d records=%d", self._state.current_step, len(self._state.step_records))
        return True

    def clear(self):
        """销毁状态。"""
        self._state = LoopState()
        self._mgr.clear()

    @staticmethod
    def _record_to_dict(r: StepRecord) -> Dict[str, Any]:
        return {
            "step": r.step, "skill": r.skill, "description": r.description,
            "tools": r.tools, "rules": r.rules, "planner_reasoning": r.planner_reasoning,
            "step_content": r.step_content, "step_success": r.step_success,
            "steps_used": r.steps_used, "step_tokens": r.step_tokens,
            "tool_calls": r.tool_calls, "charts": r.charts,
            "judge_summary": r.judge_summary, "judge_corrections": r.judge_corrections,
            "judge_continue": r.judge_continue, "judge_reasoning": r.judge_reasoning,
            "saved_at": r.saved_at,
        }

    @staticmethod
    def _dict_to_record(d: Dict[str, Any]) -> StepRecord:
        return StepRecord(
            step=d.get("step", 0), skill=d.get("skill"), description=d.get("description", ""),
            tools=d.get("tools", []), rules=d.get("rules", ""), planner_reasoning=d.get("planner_reasoning", ""),
            step_content=d.get("step_content", ""), step_success=d.get("step_success", False),
            steps_used=d.get("steps_used", 0), step_tokens=d.get("step_tokens", 0),
            tool_calls=d.get("tool_calls", []), charts=d.get("charts", []),
            judge_summary=d.get("judge_summary", ""), judge_corrections=d.get("judge_corrections"),
            judge_continue=d.get("judge_continue", True), judge_reasoning=d.get("judge_reasoning", ""),
            saved_at=d.get("saved_at", 0.0),
        )


@dataclass
class Checkpoint:
    """单步 checkpoint。"""
    step: int
    # Planner 决策
    skill: Optional[str] = None
    description: str = ""
    tools: List[str] = field(default_factory=list)
    rules: str = ""
    # 执行结果
    step_content: str = ""
    step_success: bool = False
    steps_used: int = 0
    # 累积状态
    all_content: List[str] = field(default_factory=list)
    previous_results: List[Dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0
    total_tokens: int = 0
    # 上下文（恢复用）
    stock_code: str = ""
    stock_name: str = ""
    intent_data: Dict[str, Any] = field(default_factory=dict)
    meta_keys: List[str] = field(default_factory=list)
    # Judge 状态（断点续传恢复用）
    used_tools: List[str] = field(default_factory=list)
    judge_context: str = ""
    judge_summaries: List[str] = field(default_factory=list)
    # 时间戳
    saved_at: float = 0.0

    def summary(self) -> Dict[str, Any]:
        """返回摘要（供前端展示）。"""
        return {
            "step": self.step,
            "skill": self.skill,
            "description": self.description,
            "success": self.step_success,
            "steps_used": self.steps_used,
            "content_preview": self.step_content[:200] if self.step_content else "",
            "accumulated_steps": len(self.previous_results),
            "total_steps": self.total_steps,
            "saved_at": self.saved_at,
        }


class CheckpointManager:
    """断点续传管理器。按 session 管理 checkpoint。"""

    def __init__(self, session_id: str):
        self._session_id = session_id
        # step → Checkpoint
        self._checkpoints: Dict[int, Checkpoint] = {}

    def save(
        self,
        step: int,
        skill: Optional[str],
        description: str,
        tools: List[str],
        rules: str,
        step_content: str,
        step_success: bool,
        steps_used: int,
        all_content: List[str],
        previous_results: List[Dict[str, Any]],
        total_steps: int,
        total_tokens: int,
        stock_code: str,
        stock_name: str,
        intent_data: Dict[str, Any],
        used_tools: List[str] = None,
        judge_context: str = "",
        judge_summaries: List[str] = None,
    ):
        """保存当前步的 checkpoint。"""
        cp = Checkpoint(
            step=step,
            skill=skill,
            description=description,
            tools=list(tools),
            rules=rules,
            step_content=step_content,
            step_success=step_success,
            steps_used=steps_used,
            all_content=list(all_content),
            previous_results=[dict(r) for r in previous_results],
            total_steps=total_steps,
            total_tokens=total_tokens,
            stock_code=stock_code,
            stock_name=stock_name,
            intent_data=dict(intent_data) if intent_data else {},
            used_tools=list(used_tools) if used_tools else [],
            judge_context=judge_context,
            judge_summaries=list(judge_summaries) if judge_summaries else [],
            saved_at=time.time(),
        )
        self._checkpoints[step] = cp
        logger.info(
            "[Checkpoint] 保存 session=%s step=%d skill=%s success=%s",
            self._session_id, step, skill, step_success,
        )

    def load(self, step: int) -> Optional[Checkpoint]:
        """加载指定步的 checkpoint。"""
        return self._checkpoints.get(step)

    def list(self) -> List[Dict[str, Any]]:
        """列出所有 checkpoint 的摘要。"""
        return [cp.summary() for cp in sorted(self._checkpoints.values(), key=lambda c: c.step)]

    def latest(self) -> Optional[Checkpoint]:
        """返回最新的 checkpoint。"""
        if not self._checkpoints:
            return None
        return max(self._checkpoints.values(), key=lambda c: c.step)

    def clear(self):
        """清除所有 checkpoint。"""
        count = len(self._checkpoints)
        self._checkpoints.clear()
        if count:
            logger.info("[Checkpoint] 清除 %d 个 checkpoint (session=%s)", count, self._session_id)

    @property
    def has_checkpoints(self) -> bool:
        return bool(self._checkpoints)


# ── 全局管理器池（按 session_id）──────────────────────────
_managers: Dict[str, CheckpointManager] = {}


def get_checkpoint_manager(session_id: str) -> CheckpointManager:
    """获取或创建 session 对应的 CheckpointManager。"""
    if session_id not in _managers:
        _managers[session_id] = CheckpointManager(session_id)
    return _managers[session_id]


def clear_checkpoint_manager(session_id: str):
    """清除 session 的 CheckpointManager。"""
    mgr = _managers.pop(session_id, None)
    if mgr:
        mgr.clear()
