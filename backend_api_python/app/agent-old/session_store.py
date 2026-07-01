# -*- coding: utf-8 -*-
"""
Session Store — 会话元数据 + 跨轮上下文存储。

存储方式：Redis 优先 → File → 内存降级

职责（仅元数据和跨轮上下文）：
  - 会话 CRUD（create/get/clear/list）→ session metadata（stock_code 等）
  - 工具结果缓存（save_tool_results/get_tool_results）→ 跨轮复用
  - 上下文摘要（save_context_summary/get_context_summary）→ 跨轮压缩
  - TTL 自动清理
  - 线程安全（per-session lock）

⚠️ 消息历史由 LangGraph Checkpointer 统一管理，本模块不处理消息。
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


# ── Context summary helpers ──────────────────────────────────

def _append_summary_round(summaries: dict, key: str, summary: str, max_rounds: int = 5):
    """Append a summary round (dict mutation)."""
    rounds = summaries.get(key)
    if not isinstance(rounds, list):
        rounds = []
    _next = (rounds[-1]["round"] + 1) if rounds else 1
    rounds.append({"round": _next, "summary": summary})
    if len(rounds) > max_rounds:
        rounds = rounds[-max_rounds:]
    summaries[key] = rounds


def _format_context_rounds(rounds, with_age: bool = False):
    """Format rounds list → str, or (str, round_count) when with_age=True."""
    if isinstance(rounds, str) and rounds:
        return (rounds, 0) if with_age else rounds
    if not isinstance(rounds, list) or not rounds:
        return ("", 0) if with_age else ""
    parts = [f"--- R{r['round']} ---\n{r['summary']}" for r in rounds]
    joined = "\n".join(parts)
    return (joined, len(rounds)) if with_age else joined


def _redis_parse_rounds(raw) -> list:
    """Parse a raw Redis hash value into a rounds list."""
    if raw is None:
        return []
    raw = raw.decode() if isinstance(raw, bytes) else raw
    if not raw:
        return []
    try:
        rounds = json.loads(raw)
        return rounds if isinstance(rounds, list) else []
    except (ValueError, TypeError):
        return []


# Redis keys
_SESSION_PREFIX = "agent:session:"
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


# ═══════════════════════════════════════════════════════════════
#  In-memory fallback
# ═══════════════════════════════════════════════════════════════

class _InMemoryStore:
    """Thread-safe in-memory session store (metadata + tool results + context)."""

    def __init__(self, max_sessions: int = 200, session_ttl: int = 7200):
        self._sessions: Dict[str, Dict] = {}
        self._tool_results: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_master = threading.Lock()
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_master:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def session_lock(self, session_id: str):
        return self._get_session_lock(session_id)

    # ── Session CRUD ─────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Dict]:
        with self._lock:
            s = self._sessions.get(session_id)
            if s and time.time() - s.get("updated_at", 0) > self._session_ttl:
                self._sessions.pop(session_id, None)
                self._tool_results.pop(session_id, None)
                return None
            return s

    def create_session(self, session_id: str, data: Dict) -> Dict:
        with self._lock:
            self._maybe_cleanup()
            session = {
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
            self._tool_results.pop(session_id, None)
            return self._sessions.pop(session_id, None) is not None

    def list_sessions(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            items = sorted(self._sessions.items(),
                           key=lambda x: x[1].get("updated_at", 0), reverse=True)[:limit]
            return [{"session_id": sid, **s} for sid, s in items]

    # ── Tool results (cross-turn context) ─────────────────────

    def save_tool_results(self, session_id: str, results: Dict[str, Any]) -> None:
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
        with self._lock:
            s = self._sessions.setdefault(session_id, {})
            if domain:
                summaries = s.setdefault("context_summaries", {})
                if summary:
                    _append_summary_round(summaries, domain, summary)
                elif domain not in summaries:
                    summaries[domain] = []
                s["current_domain"] = domain
            elif summary:
                cur = s.get("current_domain", "")
                if cur:
                    _append_summary_round(s.setdefault("context_summaries", {}), cur, summary)
            s["updated_at"] = time.time()

    def get_context_summary(self, session_id: str, current_domain: str = "", with_age: bool = False):
        with self._lock:
            if not current_domain:
                return ("", 0) if with_age else ""
            s = self._sessions.get(session_id, {})
            rounds = s.get("context_summaries", {}).get(current_domain, "")
            return _format_context_rounds(rounds, with_age)

    # ── Maintenance ──────────────────────────────────────────

    def _maybe_cleanup(self):
        if len(self._sessions) >= self._max_sessions:
            oldest = min(self._sessions, key=lambda s: self._sessions[s].get("updated_at", 0))
            self._sessions.pop(oldest, None)
            self._tool_results.pop(oldest, None)

    def cleanup_expired(self):
        now = time.time()
        with self._lock:
            expired = [sid for sid, s in self._sessions.items()
                       if now - s.get("updated_at", 0) > self._session_ttl]
            for sid in expired:
                self._sessions.pop(sid, None)
                self._tool_results.pop(sid, None)
            if expired:
                logger.info("Cleaned up %d expired sessions (memory)", len(expired))


# ═══════════════════════════════════════════════════════════════
#  Redis store
# ═══════════════════════════════════════════════════════════════

class _RedisStore:
    """Redis-backed session store (metadata + tool results + context)."""

    def __init__(self, redis_client, session_ttl: int = 7200):
        self._r = redis_client
        self._ttl = session_ttl
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_master = threading.Lock()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_master:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def session_lock(self, session_id: str):
        return self._get_session_lock(session_id)

    def _session_key(self, session_id: str) -> str:
        return f"{_SESSION_PREFIX}{session_id}"

    def _tool_results_key(self, session_id: str) -> str:
        return f"{_TOOL_RESULTS_PREFIX}{session_id}"

    def _context_summaries_key(self, session_id: str) -> str:
        return f"quantdinger:ctx_summaries:{session_id}"

    def _context_domain_key(self, session_id: str) -> str:
        return f"quantdinger:ctx_domain:{session_id}"

    # ── Session CRUD ─────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Dict]:
        raw = self._r.get(self._session_key(session_id))
        return json.loads(raw) if raw else None

    def create_session(self, session_id: str, data: Dict) -> Dict:
        session = {
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

    # ── Tool results (cross-turn context) ─────────────────────

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

    def save_context_summary(self, session_id: str, summary: str, domain: str = "") -> None:
        key = self._context_summaries_key(session_id)
        if domain:
            if summary:
                rounds = _redis_parse_rounds(self._r.hget(key, domain))
                _next = (rounds[-1]["round"] + 1) if rounds else 1
                rounds.append({"round": _next, "summary": summary})
                if len(rounds) > 5:
                    rounds = rounds[-5:]
                self._r.hset(key, domain, json.dumps(rounds, ensure_ascii=False))
            self._r.expire(key, 3600)
            self._r.set(self._context_domain_key(session_id), domain, ex=3600)
        elif summary:
            cur = self._r.get(self._context_domain_key(session_id))
            cur = cur.decode() if isinstance(cur, bytes) else (cur or "")
            if cur:
                rounds = _redis_parse_rounds(self._r.hget(key, cur))
                _next = (rounds[-1]["round"] + 1) if rounds else 1
                rounds.append({"round": _next, "summary": summary})
                if len(rounds) > 5:
                    rounds = rounds[-5:]
                self._r.hset(key, cur, json.dumps(rounds, ensure_ascii=False))
                self._r.expire(key, 3600)

    def get_context_summary(self, session_id: str, current_domain: str = "", with_age: bool = False):
        if not current_domain:
            return ("", 0) if with_age else ""
        raw = self._r.hget(self._context_summaries_key(session_id), current_domain)
        data = raw.decode() if isinstance(raw, bytes) else (raw or "")
        if not data:
            return ("", 0) if with_age else ""
        try:
            rounds = json.loads(data)
            if isinstance(rounds, list):
                return _format_context_rounds(rounds, with_age)
        except (ValueError, TypeError):
            pass
        return (data, 0) if with_age else data

    def cleanup_expired(self):
        pass  # Redis EXPIRE handles this automatically


# ═══════════════════════════════════════════════════════════════
#  File store
# ═══════════════════════════════════════════════════════════════

class _FileStore:
    """File-backed session store (metadata + tool results + context).

    Each session → {SESSION_DIR}/{session_id}.json
    File structure:
        {
            "session": { ... metadata (stock_code, created_at, ...) ... },
            "tool_results": { stock_code: { ... } },
            "context_summaries": { domain: [{round, summary}, ...] },
            "current_domain": "..."
        }
    """

    def __init__(self, session_dir: str, session_ttl: int = 7200, max_sessions: int = 200):
        self._dir = Path(session_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = session_ttl
        self._max = max_sessions
        self._lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._session_locks_master = threading.Lock()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_master:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def session_lock(self, session_id: str):
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
                "created_at": data.get("created_at", time.time()),
                "updated_at": time.time(),
                "stock_code": data.get("stock_code"),
            }
            full = {
                "session": session,
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
                data = {
                    "session": {"updated_at": time.time()},
                    "tool_results": {},
                    "context_summaries": {},
                    "current_domain": "",
                }
            if domain:
                summaries = data.setdefault("context_summaries", {})
                if summary:
                    _append_summary_round(summaries, domain, summary)
                elif domain not in summaries:
                    summaries[domain] = []
                data["current_domain"] = domain
            elif summary:
                cur = data.get("current_domain", "")
                if cur:
                    _append_summary_round(data.setdefault("context_summaries", {}), cur, summary)
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
            return _format_context_rounds(rounds, with_age)

    def _maybe_cleanup(self):
        files = sorted(self._dir.glob("*.json"), key=lambda x: x.stat().st_mtime)
        if len(files) >= self._max:
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


# ═══════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════

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

        session_ttl = int(os.getenv("AGENT_SESSION_TTL", "7200"))
        max_sessions = int(os.getenv("AGENT_MAX_SESSIONS", "200"))
        backend = os.getenv("SESSION_BACKEND", "").strip().lower()

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
