# -*- coding: utf-8 -*-
"""
Session Store — 会话持久化存储。

存储方式：Redis 优先 + 内存降级
  - Redis：进程重启不丢失，支持多 worker
  - 内存：单 worker / 无 Redis 环境自动降级

功能：
  - 会话 CRUD（create/get/clear/list）
  - 对话历史管理（add_message/get_history）
  - 上下文摘要（save_context_summary/get_context_summary）
  - 领域隔离（domain 切换时自动丢弃旧领域上下文）
  - TTL 自动清理
  - 线程安全（per-session lock）

被调用方：
  agent.py → get_session_store() → 所有会话操作
  agent.py → store.session_lock(session_id) → 防止并发交错

公开接口：
  get_session_store() → SessionStore（单例）
  SessionStore.create_session(session_id, metadata) → dict
  SessionStore.get_session(session_id) → Optional[dict]
  SessionStore.add_message(session_id, role, content) → None
  SessionStore.get_history(session_id, limit) → List[dict]
  SessionStore.save_context_summary(session_id, summary, domain) → None
  SessionStore.get_context_summary(session_id, current_domain, with_age) → str | tuple
  SessionStore.session_lock(session_id) → context manager
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
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
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_master = threading.Lock()
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """Get or create a per-session lock."""
        with self._session_locks_master:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def session_lock(self, session_id: str):
        """Return a context manager that locks a specific session.

        Usage:
            with store.session_lock(sid):
                history = store.get_history(sid)
                # ... process ...
                store.add_message(sid, "assistant", reply)
        """
        return self._get_session_lock(session_id)

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

    # ── Compressed context (跨轮上下文压缩) ──────────────────

    def save_context_summary(self, session_id: str, summary: str, domain: str = "") -> None:
        """保存压缩上下文摘要。按领域分别存储，追加轮次而非覆盖。空summary只确保key存在，不清除已有轮次。"""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {}
            s = self._sessions[session_id]
            if domain:
                if "context_summaries" not in s:
                    s["context_summaries"] = {}
                if summary:
                    _rounds = s["context_summaries"].get(domain)
                    if not isinstance(_rounds, list):
                        _rounds = []
                    _next_round = (_rounds[-1]["round"] + 1) if _rounds else 1
                    _rounds.append({"round": _next_round, "summary": summary})
                    # 最多保留最近 5 轮
                    if len(_rounds) > 5:
                        _rounds = _rounds[-5:]
                    s["context_summaries"][domain] = _rounds
                else:
                    # 空 summary → 确保 key 存在，不清除已有轮次
                    if domain not in s["context_summaries"]:
                        s["context_summaries"][domain] = []
                s["current_domain"] = domain
            elif summary:
                cur = s.get("current_domain", "")
                if cur:
                    if "context_summaries" not in s:
                        s["context_summaries"] = {}
                    _rounds = s["context_summaries"].get(cur)
                    if not isinstance(_rounds, list):
                        _rounds = []
                    _next_round = (_rounds[-1]["round"] + 1) if _rounds else 1
                    _rounds.append({"round": _next_round, "summary": summary})
                    if len(_rounds) > 5:
                        _rounds = _rounds[-5:]
                    s["context_summaries"][cur] = _rounds
            s["updated_at"] = time.time()

    def get_context_summary(self, session_id: str, current_domain: str = "", with_age: bool = False):
        """获取指定领域的压缩上下文摘要。

        Returns:
            with_age=False: 拼接后的摘要字符串（带轮次分隔符）
            with_age=True: (拼接摘要, 总轮次数) 元组
        """
        with self._lock:
            s = self._sessions.get(session_id, {})
            if current_domain:
                rounds = s.get("context_summaries", {}).get(current_domain, "")
                # 兼容旧格式（纯字符串）
                if isinstance(rounds, str) and rounds:
                    if with_age:
                        return rounds, 0
                    return rounds
                if not isinstance(rounds, list) or not rounds:
                    if with_age:
                        return "", 0
                    return ""
                # 拼接轮次，带分隔符
                parts = []
                for r in rounds:
                    parts.append(f"--- R{r['round']} ---\n{r['summary']}")
                joined = "\n".join(parts)
                if with_age:
                    return joined, len(rounds)
                return joined
            if with_age:
                return "", 0
            return ""

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

    # Lua script: atomic read-append-trim for conversation history.
    # KEYS[1] = conversation key
    # ARGV[1] = message JSON (role+content)
    # ARGV[2] = max messages to keep
    # ARGV[3] = TTL seconds
    _LUA_ADD_MESSAGE = """
    local key = KEYS[1]
    local msg = ARGV[1]
    local max_msgs = tonumber(ARGV[2])
    local ttl = tonumber(ARGV[3])
    local raw = redis.call('GET', key)
    local history = {}
    if raw then
        history = cjson.decode(raw)
    end
    table.insert(history, cjson.decode(msg))
    if #history > max_msgs then
        local trimmed = {}
        for i = #history - max_msgs + 1, #history do
            trimmed[#trimmed + 1] = history[i]
        end
        history = trimmed
    end
    local out = cjson.encode(history)
    redis.call('SETEX', key, ttl, out)
    return out
    """

    def __init__(self, redis_client, session_ttl: int = 7200):
        self._r = redis_client
        self._ttl = session_ttl
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_master = threading.Lock()
        # Register Lua script
        self._lua_add = self._r.register_script(self._LUA_ADD_MESSAGE)

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """Get or create a per-session lock for this worker process."""
        with self._session_locks_master:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def session_lock(self, session_id: str):
        """Return a context manager that locks a specific session.

        Note: This is per-process locking. For multi-worker deployments,
        Redis Lua scripts provide atomicity for add_message().
        """
        return self._get_session_lock(session_id)

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
        pipe.delete(self._context_summaries_key(session_id))
        pipe.delete(self._context_domain_key(session_id))
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
        """Atomically append a message using Lua script (no TOCTOU race)."""
        key = self._conv_key(session_id)
        msg = json.dumps({"role": role, "content": content}, ensure_ascii=False)
        max_msgs = max_turns * 2
        try:
            self._lua_add(keys=[key], args=[msg, max_msgs, self._ttl])
        except Exception:
            # Fallback: non-atomic (Lua script failure, e.g. Redis < 2.6)
            raw = self._r.get(key)
            history = json.loads(raw) if raw else []
            history.append({"role": role, "content": content})
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

    # ── Compressed context ───────────────────────────────────

    def _context_summaries_key(self, session_id: str) -> str:
        return f"quantdinger:ctx_summaries:{session_id}"

    def _context_domain_key(self, session_id: str) -> str:
        return f"quantdinger:ctx_domain:{session_id}"

    def save_context_summary(self, session_id: str, summary: str, domain: str = "") -> None:
        if domain:
            key = self._context_summaries_key(session_id)
            if summary:
                raw = self._r.hget(key, domain)
                existing = raw.decode() if isinstance(raw, bytes) else (raw or "")
                rounds = []
                if existing:
                    try:
                        import json as _json
                        rounds = _json.loads(existing)
                        if not isinstance(rounds, list):
                            rounds = []
                    except (ValueError, TypeError):
                        rounds = []
                _next_round = (rounds[-1]["round"] + 1) if rounds else 1
                rounds.append({"round": _next_round, "summary": summary})
                if len(rounds) > 5:
                    rounds = rounds[-5:]
                import json as _json
                self._r.hset(key, domain, _json.dumps(rounds, ensure_ascii=False))
            else:
                # 空 summary → 不处理（不清除已有轮次）
                pass
            self._r.expire(key, 3600)
            self._r.set(self._context_domain_key(session_id), domain, ex=3600)
        elif summary:
            cur = self._r.get(self._context_domain_key(session_id))
            cur = cur.decode() if isinstance(cur, bytes) else (cur or "")
            if cur:
                key = self._context_summaries_key(session_id)
                raw = self._r.hget(key, cur)
                existing = raw.decode() if isinstance(raw, bytes) else (raw or "")
                rounds = []
                if existing:
                    try:
                        import json as _json
                        rounds = _json.loads(existing)
                        if not isinstance(rounds, list):
                            rounds = []
                    except (ValueError, TypeError):
                        rounds = []
                _next_round = (rounds[-1]["round"] + 1) if rounds else 1
                rounds.append({"round": _next_round, "summary": summary})
                if len(rounds) > 5:
                    rounds = rounds[-5:]
                import json as _json
                self._r.hset(key, cur, _json.dumps(rounds, ensure_ascii=False))
                self._r.expire(key, 3600)

    def _context_ages_key(self, session_id: str) -> str:
        return f"quantdinger:ctx_ages:{session_id}"

    def get_context_summary(self, session_id: str, current_domain: str = "", with_age: bool = False):
        if not current_domain:
            return ("", 0) if with_age else ""
        raw = self._r.hget(self._context_summaries_key(session_id), current_domain)
        data = raw.decode() if isinstance(raw, bytes) else (raw or "")
        if not data:
            return ("", 0) if with_age else ""
        # 尝试解析 JSON 轮次列表
        try:
            import json as _json
            rounds = _json.loads(data)
            if isinstance(rounds, list) and rounds:
                parts = [f"--- R{r['round']} ---\n{r['summary']}" for r in rounds]
                joined = "\n".join(parts)
                return (joined, len(rounds)) if with_age else joined
        except (ValueError, TypeError):
            pass
        # 兼容旧格式（纯字符串）
        return (data, 0) if with_age else data

    # ── Maintenance (Redis handles TTL natively) ─────────────

    def cleanup_expired(self):
        pass  # Redis EXPIRE handles this automatically


# ── File store ───────────────────────────────────────────────

class _FileStore:
    """File-backed session store.

    Each session is persisted as a JSON file under SESSION_DIR:
        {SESSION_DIR}/{session_id}.json

    File structure:
        {
            "session": { ... session metadata ... },
            "history": [ {role, content}, ... ],
            "tool_results": { stock_code: { ... } },
            "context_summaries": { domain: summary },
            "current_domain": "..."
        }

    Survives process restarts. No external dependencies.
    """

    def __init__(self, session_dir: str, session_ttl: int = 7200, max_sessions: int = 200):
        import pathlib
        self._dir = pathlib.Path(session_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = session_ttl
        self._max = max_sessions
        self._lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_master = threading.Lock()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """Get or create a per-session lock."""
        with self._session_locks_master:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def session_lock(self, session_id: str):
        """Return a context manager that locks a specific session."""
        return self._get_session_lock(session_id)

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _read(self, session_id: str) -> Optional[Dict]:
        p = self._path(session_id)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[FileStore] Failed to read session %s: %s", session_id, e)
            return None

    def _write(self, session_id: str, data: Dict) -> None:
        """Atomic write: write to temp file, then rename."""
        import tempfile
        p = self._path(session_id)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except OSError as e:
            logger.error("[FileStore] Failed to write session %s: %s", session_id, e)
            # Clean up temp file on failure
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _is_expired(self, data: Dict) -> bool:
        s = data.get("session", {})
        return time.time() - s.get("updated_at", 0) > self._ttl

    # ── Session CRUD ─────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Dict]:
        with self._lock:
            data = self._read(session_id)
            if data is None:
                return None
            if self._is_expired(data):
                self._path(session_id).unlink(missing_ok=True)
                return None
            return data.get("session", {})

    def create_session(self, session_id: str, data: Dict) -> Dict:
        with self._lock:
            self._maybe_cleanup()
            session = {
                "messages": data.get("messages", []),
                "created_at": data.get("created_at", time.time()),
                "updated_at": time.time(),
                "stock_code": data.get("stock_code"),
            }
            full = {
                "session": session,
                "history": [],
                "tool_results": {},
                "context_summaries": {},
                "current_domain": "",
            }
            self._write(session_id, full)
            return session

    def update_session(self, session_id: str, **fields) -> None:
        with self._lock:
            data = self._read(session_id)
            if data:
                data["session"].update(fields)
                data["session"]["updated_at"] = time.time()
                self._write(session_id, data)

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            p = self._path(session_id)
            if p.is_file():
                p.unlink()
                return True
            return False

    def list_sessions(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            sessions = []
            for p in sorted(self._dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                if len(sessions) >= limit:
                    break
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if self._is_expired(data):
                    p.unlink(missing_ok=True)
                    continue
                sid = p.stem
                s = data.get("session", {})
                sessions.append({"session_id": sid, **s})
            return sessions

    # ── Conversation history ─────────────────────────────────

    def get_history(self, session_id: str) -> List[Dict]:
        with self._lock:
            data = self._read(session_id)
            return list(data.get("history", [])) if data else []

    def add_message(self, session_id: str, role: str, content: str, max_turns: int = 20):
        with self._lock:
            data = self._read(session_id)
            if not data:
                data = {
                    "session": {"messages": [], "created_at": time.time(), "updated_at": time.time()},
                    "history": [],
                    "tool_results": {},
                    "context_summaries": {},
                    "current_domain": "",
                }
            history = data.setdefault("history", [])
            history.append({"role": role, "content": content})
            max_msgs = max_turns * 2
            if len(history) > max_msgs:
                data["history"] = history[-max_msgs:]
            # Also update session.messages for compatibility
            data["session"]["messages"] = data["history"]
            data["session"]["updated_at"] = time.time()
            self._write(session_id, data)

    def clear_history(self, session_id: str):
        with self._lock:
            data = self._read(session_id)
            if data:
                data["history"] = []
                data["session"]["messages"] = []
                self._write(session_id, data)

    # ── Tool results (cross-turn context) ─────────────────────

    def save_tool_results(self, session_id: str, results: Dict[str, Any]) -> None:
        with self._lock:
            data = self._read(session_id)
            if not data:
                return
            existing = data.setdefault("tool_results", {})
            for stock_code, rdata in results.items():
                if stock_code in existing and isinstance(existing[stock_code], dict) and isinstance(rdata, dict):
                    existing[stock_code].update(rdata)
                else:
                    existing[stock_code] = rdata
            self._write(session_id, data)

    def get_tool_results(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._read(session_id)
            return dict(data.get("tool_results", {})) if data else {}

    def clear_tool_results(self, session_id: str) -> None:
        with self._lock:
            data = self._read(session_id)
            if data:
                data["tool_results"] = {}
                self._write(session_id, data)

    # ── Compressed context ───────────────────────────────────

    def save_context_summary(self, session_id: str, summary: str, domain: str = "") -> None:
        with self._lock:
            data = self._read(session_id)
            if not data:
                # 自动创建最小 session 结构
                data = {
                    "session": {"updated_at": time.time()},
                    "history": [],
                    "tool_results": {},
                    "context_summaries": {},
                    "current_domain": "",
                }
            if domain:
                summaries = data.setdefault("context_summaries", {})
                if summary:
                    rounds = summaries.get(domain)
                    if not isinstance(rounds, list):
                        rounds = []
                    _next_round = (rounds[-1]["round"] + 1) if rounds else 1
                    rounds.append({"round": _next_round, "summary": summary})
                    if len(rounds) > 5:
                        rounds = rounds[-5:]
                    summaries[domain] = rounds
                else:
                    # 空 summary → 确保 key 存在，不清除已有轮次
                    if domain not in summaries:
                        summaries[domain] = []
                data["current_domain"] = domain
            elif summary:
                if cur:
                    summaries = data.setdefault("context_summaries", {})
                    rounds = summaries.get(cur)
                    if not isinstance(rounds, list):
                        rounds = []
                    _next_round = (rounds[-1]["round"] + 1) if rounds else 1
                    rounds.append({"round": _next_round, "summary": summary})
                    if len(rounds) > 5:
                        rounds = rounds[-5:]
                    summaries[cur] = rounds
            data.setdefault("session", {})["updated_at"] = time.time()
            self._write(session_id, data)

    def get_context_summary(self, session_id: str, current_domain: str = "", with_age: bool = False):
        if not current_domain:
            return ("", 0) if with_age else ""
        with self._lock:
            data = self._read(session_id)
            if not data:
                return ("", 0) if with_age else ""
            rounds = data.get("context_summaries", {}).get(current_domain, "")
            # 兼容旧格式（纯字符串）
            if isinstance(rounds, str) and rounds:
                return (rounds, 0) if with_age else rounds
            if not isinstance(rounds, list) or not rounds:
                return ("", 0) if with_age else ""
            parts = [f"--- R{r['round']} ---\n{r['summary']}" for r in rounds]
            joined = "\n".join(parts)
            return (joined, len(rounds)) if with_age else joined

    # ── Maintenance ──────────────────────────────────────────

    def _maybe_cleanup(self):
        files = sorted(self._dir.glob("*.json"), key=lambda x: x.stat().st_mtime)
        if len(files) >= self._max:
            # Remove oldest files
            for f in files[: len(files) - self._max + 1]:
                f.unlink(missing_ok=True)

    def cleanup_expired(self):
        now = time.time()
        removed = 0
        for p in self._dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if now - data.get("session", {}).get("updated_at", 0) > self._ttl:
                    p.unlink()
                    removed += 1
            except (json.JSONDecodeError, OSError):
                pass
        if removed:
            logger.info("[FileStore] Cleaned up %d expired sessions", removed)


# ── Public API ───────────────────────────────────────────────

_store = None
_store_lock = threading.Lock()


def get_session_store():
    """Get or initialize the session store.

    Priority: SESSION_BACKEND env var > Redis auto-detect > file > memory.

    SESSION_BACKEND values:
        "redis"  — force Redis (fail if unavailable)
        "file"   — file-backed persistence (JSON files on disk)
        "memory" — in-memory only (default fallback)
        (unset)  — auto: Redis → file → memory
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        import os
        session_ttl = int(os.getenv("AGENT_SESSION_TTL", "7200"))
        max_sessions = int(os.getenv("AGENT_MAX_SESSIONS", "200"))
        backend = os.getenv("SESSION_BACKEND", "").strip().lower()

        # Explicit backend selection
        if backend == "redis":
            redis_client = _get_redis()
            if redis_client:
                _store = _RedisStore(redis_client, session_ttl=session_ttl)
                logger.info("Session store: Redis (TTL=%ds)", session_ttl)
                return _store
            logger.warning("SESSION_BACKEND=redis but Redis unavailable, falling back to file")

        if backend == "file":
            session_dir = os.getenv("SESSION_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "workspaces", "sessions"))
            _store = _FileStore(session_dir, session_ttl=session_ttl, max_sessions=max_sessions)
            logger.info("Session store: File (%s)", session_dir)
            return _store

        if backend == "memory":
            _store = _InMemoryStore(max_sessions=max_sessions, session_ttl=session_ttl)
            logger.info("Session store: In-memory (max=%d, TTL=%ds)", max_sessions, session_ttl)
            return _store

        # Auto-detect: Redis → file → memory
        redis_client = _get_redis()
        if redis_client:
            _store = _RedisStore(redis_client, session_ttl=session_ttl)
            logger.info("Session store: Redis (TTL=%ds)", session_ttl)
            return _store

        session_dir = os.getenv("SESSION_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "workspaces", "sessions"))
        _store = _FileStore(session_dir, session_ttl=session_ttl, max_sessions=max_sessions)
        logger.info("Session store: File (%s)", session_dir)
        return _store
