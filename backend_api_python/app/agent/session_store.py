# -*- coding: utf-8 -*-
"""
Session Store — Redis-backed session storage with in-memory fallback.

Provides thread-safe session management with:
- Redis persistence (survives process restarts, enables multi-worker)
- In-memory fallback (single-worker / no-Redis environments)
- TTL-based auto-cleanup
- Thread-safe conversation history
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Redis keys
_SESSION_PREFIX = "agent:session:"
_CONVERSATION_PREFIX = "agent:conv:"
_TOOL_RESULTS_PREFIX = "agent:tool_results:"


def _get_redis():
    """Lazy Redis connection (returns None if unavailable)."""
    try:
        import redis
        from app.config import RedisConfig
        client = redis.Redis(
            host=RedisConfig.HOST,
            port=RedisConfig.PORT,
            db=RedisConfig.DB,
            password=RedisConfig.PASSWORD,
            socket_connect_timeout=RedisConfig.CONNECT_TIMEOUT,
            socket_timeout=RedisConfig.SOCKET_TIMEOUT,
            decode_responses=True,
        )
        client.ping()
        return client
    except Exception as e:
        logger.debug("Redis not available, using in-memory store: %s", e)
        return None


# ── In-memory fallback ───────────────────────────────────────

class _InMemoryStore:
    """Thread-safe in-memory session store."""

    def __init__(self, max_sessions: int = 200, session_ttl: int = 7200):
        self._sessions: Dict[str, Dict] = {}
        self._conversations: Dict[str, List[Dict]] = {}
        self._tool_results: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl

    # ── Session CRUD ─────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Dict]:
        with self._lock:
            s = self._sessions.get(session_id)
            if s and time.time() - s.get("updated_at", 0) > self._session_ttl:
                self._sessions.pop(session_id, None)
                self._conversations.pop(session_id, None)
                return None
            return s

    def create_session(self, session_id: str, data: Dict) -> Dict:
        with self._lock:
            self._maybe_cleanup()
            session = {
                "messages": data.get("messages", []),
                "created_at": data.get("created_at", time.time()),
                "updated_at": time.time(),
                "stock_code": data.get("stock_code"),
            }
            self._sessions[session_id] = session
            return session

    def update_session(self, session_id: str, **fields) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].update(fields)
                self._sessions[session_id]["updated_at"] = time.time()

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            self._conversations.pop(session_id, None)
            self._tool_results.pop(session_id, None)
            return self._sessions.pop(session_id, None) is not None

    def list_sessions(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            items = sorted(self._sessions.items(),
                           key=lambda x: x[1].get("updated_at", 0), reverse=True)[:limit]
            return [{"session_id": sid, **s} for sid, s in items]

    # ── Conversation history ─────────────────────────────────

    def get_history(self, session_id: str) -> List[Dict]:
        with self._lock:
            return list(self._conversations.get(session_id, []))

    def add_message(self, session_id: str, role: str, content: str, max_turns: int = 20):
        with self._lock:
            if session_id not in self._conversations:
                self._conversations[session_id] = []
            self._conversations[session_id].append({"role": role, "content": content})
            max_msgs = max_turns * 2
            if len(self._conversations[session_id]) > max_msgs:
                self._conversations[session_id] = self._conversations[session_id][-max_msgs:]

    def clear_history(self, session_id: str):
        with self._lock:
            self._conversations.pop(session_id, None)

    # ── Tool results (cross-turn context) ─────────────────────

    def save_tool_results(self, session_id: str, results: Dict[str, Any]) -> None:
        """Persist tool call results for reuse in subsequent turns.

        Args:
            session_id: Session identifier.
            results: Dict mapping stock_code → {quote, trend, news, ...}.
        """
        with self._lock:
            existing = self._tool_results.get(session_id, {})
            for stock_code, data in results.items():
                if stock_code in existing and isinstance(existing[stock_code], dict) and isinstance(data, dict):
                    existing[stock_code].update(data)
                else:
                    existing[stock_code] = data
            self._tool_results[session_id] = existing

    def get_tool_results(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._tool_results.get(session_id, {}))

    def clear_tool_results(self, session_id: str) -> None:
        with self._lock:
            self._tool_results.pop(session_id, None)

    # ── Maintenance ──────────────────────────────────────────

    def _maybe_cleanup(self):
        if len(self._sessions) >= self._max_sessions:
            oldest = min(self._sessions, key=lambda s: self._sessions[s].get("updated_at", 0))
            self._sessions.pop(oldest, None)
            self._conversations.pop(oldest, None)
            self._tool_results.pop(oldest, None)

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            expired = [sid for sid, s in self._sessions.items()
                       if now - s.get("updated_at", 0) > self._session_ttl]
            for sid in expired:
                self._sessions.pop(sid, None)
                self._conversations.pop(sid, None)
                self._tool_results.pop(sid, None)
            if expired:
                logger.info("Cleaned up %d expired sessions (memory)", len(expired))


# ── Redis store ──────────────────────────────────────────────

class _RedisStore:
    """Redis-backed session store."""

    def __init__(self, redis_client, session_ttl: int = 7200):
        self._r = redis_client
        self._ttl = session_ttl

    def _session_key(self, session_id: str) -> str:
        return f"{_SESSION_PREFIX}{session_id}"

    def _conv_key(self, session_id: str) -> str:
        return f"{_CONVERSATION_PREFIX}{session_id}"

    # ── Session CRUD ─────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Dict]:
        raw = self._r.get(self._session_key(session_id))
        if raw:
            return json.loads(raw)
        return None

    def create_session(self, session_id: str, data: Dict) -> Dict:
        session = {
            "messages": data.get("messages", []),
            "created_at": data.get("created_at", time.time()),
            "updated_at": time.time(),
            "stock_code": data.get("stock_code"),
        }
        self._r.setex(self._session_key(session_id), self._ttl, json.dumps(session, ensure_ascii=False))
        return session

    def update_session(self, session_id: str, **fields) -> None:
        raw = self._r.get(self._session_key(session_id))
        if raw:
            session = json.loads(raw)
            session.update(fields)
            session["updated_at"] = time.time()
            self._r.setex(self._session_key(session_id), self._ttl, json.dumps(session, ensure_ascii=False))

    def delete_session(self, session_id: str) -> bool:
        pipe = self._r.pipeline()
        pipe.delete(self._session_key(session_id))
        pipe.delete(self._conv_key(session_id))
        pipe.delete(self._tool_results_key(session_id))
        results = pipe.execute()
        return results[0] > 0

    def list_sessions(self, limit: int = 50) -> List[Dict]:
        keys = self._r.keys(f"{_SESSION_PREFIX}*")
        sessions = []
        for key in keys:
            raw = self._r.get(key)
            if raw:
                s = json.loads(raw)
                sid = key.replace(_SESSION_PREFIX, "")
                sessions.append({"session_id": sid, **s})
        sessions.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return sessions[:limit]

    # ── Conversation history ─────────────────────────────────

    def get_history(self, session_id: str) -> List[Dict]:
        raw = self._r.get(self._conv_key(session_id))
        if raw:
            return json.loads(raw)
        return []

    def add_message(self, session_id: str, role: str, content: str, max_turns: int = 20):
        key = self._conv_key(session_id)
        raw = self._r.get(key)
        history = json.loads(raw) if raw else []
        history.append({"role": role, "content": content})
        max_msgs = max_turns * 2
        if len(history) > max_msgs:
            history = history[-max_msgs:]
        self._r.setex(key, self._ttl, json.dumps(history, ensure_ascii=False))

    def clear_history(self, session_id: str):
        self._r.delete(self._conv_key(session_id))

    # ── Tool results (cross-turn context) ─────────────────────

    def _tool_results_key(self, session_id: str) -> str:
        return f"{_TOOL_RESULTS_PREFIX}{session_id}"

    def save_tool_results(self, session_id: str, results: Dict[str, Any]) -> None:
        key = self._tool_results_key(session_id)
        raw = self._r.get(key)
        existing = json.loads(raw) if raw else {}
        for stock_code, data in results.items():
            if stock_code in existing and isinstance(existing[stock_code], dict) and isinstance(data, dict):
                existing[stock_code].update(data)
            else:
                existing[stock_code] = data
        self._r.setex(key, self._ttl, json.dumps(existing, ensure_ascii=False))

    def get_tool_results(self, session_id: str) -> Dict[str, Any]:
        raw = self._r.get(self._tool_results_key(session_id))
        return json.loads(raw) if raw else {}

    def clear_tool_results(self, session_id: str) -> None:
        self._r.delete(self._tool_results_key(session_id))

    # ── Maintenance (Redis handles TTL natively) ─────────────

    def cleanup_expired(self):
        pass  # Redis EXPIRE handles this automatically


# ── Public API ───────────────────────────────────────────────

_store = None
_store_lock = threading.Lock()


def get_session_store():
    """Get or initialize the session store (Redis if available, memory otherwise)."""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        import os
        session_ttl = int(os.getenv("AGENT_SESSION_TTL", "7200"))
        max_sessions = int(os.getenv("AGENT_MAX_SESSIONS", "200"))

        redis_client = _get_redis()
        if redis_client:
            _store = _RedisStore(redis_client, session_ttl=session_ttl)
            logger.info("Session store: Redis (TTL=%ds)", session_ttl)
        else:
            _store = _InMemoryStore(max_sessions=max_sessions, session_ttl=session_ttl)
            logger.info("Session store: In-memory (max=%d, TTL=%ds)", max_sessions, session_ttl)
        return _store
