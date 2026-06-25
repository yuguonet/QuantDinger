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
