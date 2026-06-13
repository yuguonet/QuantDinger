# -*- coding: utf-8 -*-
"""
Context Manager — 多用户会话上下文管理。

职责：
- 跟踪每个 session 的当前 domain（用于上下文加成）
- 记录最近的路由历史（用于意图切换检测）
- 检测话题漂移（domain 切换时清理旧上下文）
- 线程安全（多用户并发访问）
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """单个会话的路由状态。"""
    session_id: str
    current_domain: str = ""
    current_intent: str = ""
    last_route_time: float = 0.0
    # 最近 N 次路由记录（用于检测意图切换）
    history: List[Dict] = field(default_factory=list)
    # 该会话的路由统计
    route_counts: Dict[str, int] = field(default_factory=dict)
    # 用户偏好（可选，如常用股票代码）
    user_prefs: Dict = field(default_factory=dict)

    def add_route(self, domain: str, intent: str, confidence: float, query: str):
        """记录一次路由结果。"""
        self.current_domain = domain
        self.current_intent = intent
        self.last_route_time = time.time()
        self.history.append({
            "domain": domain,
            "intent": intent,
            "confidence": confidence,
            "query": query[:100],
            "time": self.last_route_time,
        })
        # 只保留最近 20 条
        if len(self.history) > 20:
            self.history = self.history[-20:]
        # 统计
        key = f"{domain}/{intent}"
        self.route_counts[key] = self.route_counts.get(key, 0) + 1

    def detect_domain_switch(self, new_domain: str, window: int = 3) -> bool:
        """检测是否发生了领域切换。

        如果最近 N 次路由都是同一个 domain，突然切到新 domain，
        则认为是话题切换。
        """
        if not self.history or not self.current_domain:
            return False
        if new_domain == self.current_domain:
            return False
        recent = self.history[-window:]
        same_domain_count = sum(1 for h in recent if h["domain"] == self.current_domain)
        return same_domain_count >= window - 1

    def get_context_domain(self) -> str:
        """获取当前上下文 domain。"""
        # 超过 5 分钟没有交互，清除上下文
        if time.time() - self.last_route_time > 300:
            return ""
        return self.current_domain

    @property
    def turn_count(self) -> int:
        return len(self.history)


class ContextManager:
    """多用户会话上下文管理器。

    线程安全：支持多用户并发访问。

    Args:
        session_ttl: 会话超时时间（秒），超时后自动清理
        max_sessions: 最大活跃会话数
    """

    def __init__(self, session_ttl: int = 3600, max_sessions: int = 500):
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self._session_ttl = session_ttl
        self._max_sessions = max_sessions

    def get_state(self, session_id: str) -> SessionState:
        """获取或创建会话状态。"""
        with self._lock:
            if session_id not in self._sessions:
                self._maybe_cleanup()
                self._sessions[session_id] = SessionState(session_id=session_id)
            return self._sessions[session_id]

    def get_context_domain(self, session_id: str) -> str:
        """获取会话的当前上下文 domain。"""
        state = self.get_state(session_id)
        return state.get_context_domain()

    def record_route(
        self,
        session_id: str,
        domain: str,
        intent: str,
        confidence: float,
        query: str,
    ):
        """记录一次路由结果到会话历史。"""
        state = self.get_state(session_id)
        state.add_route(domain, intent, confidence, query)

    def clear_session(self, session_id: str):
        """清空指定会话。"""
        with self._lock:
            self._sessions.pop(session_id, None)

    def get_session_stats(self, session_id: str) -> Dict:
        """获取会话统计信息。"""
        state = self.get_state(session_id)
        return {
            "session_id": session_id,
            "current_domain": state.current_domain,
            "current_intent": state.current_intent,
            "turn_count": state.turn_count,
            "route_counts": dict(state.route_counts),
            "last_active": state.last_route_time,
        }

    def list_active_sessions(self) -> List[str]:
        """列出所有活跃会话 ID。"""
        with self._lock:
            now = time.time()
            return [
                sid for sid, s in self._sessions.items()
                if now - s.last_route_time < self._session_ttl
            ]

    def _maybe_cleanup(self):
        """清理过期会话（在锁内调用）。"""
        if len(self._sessions) < self._max_sessions:
            return
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_route_time > self._session_ttl
        ]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            logger.info("[Context] 清理 %d 个过期会话", len(expired))

    def cleanup_all_expired(self):
        """手动触发全量过期清理。"""
        with self._lock:
            now = time.time()
            expired = [
                sid for sid, s in self._sessions.items()
                if now - s.last_route_time > self._session_ttl
            ]
            for sid in expired:
                del self._sessions[sid]
            if expired:
                logger.info("[Context] 清理 %d 个过期会话", len(expired))
