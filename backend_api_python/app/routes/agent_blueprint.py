# -*- coding: utf-8 -*-
"""
/api/agent/* — AI Agent 聊天 & 流式接口
Nanobot 内核 — 直接使用 nanobot SessionManager + AgentLoop
"""
import os
import json
import logging
import uuid
import threading
import queue
from typing import Any, Dict, Optional

from flask import Blueprint, request, jsonify, Response
from app.utils.auth import login_required

MAX_MESSAGE_LENGTH = int(os.getenv("AGENT_MAX_MESSAGE_LENGTH", "4000"))

logger = logging.getLogger(__name__)

agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

# ── Per-user 互斥锁 ──────────────────────────────────────────
_user_agent_locks: Dict[str, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def _acquire_user_lock(user_id: int) -> bool:
    key = str(user_id)
    with _user_locks_guard:
        if key not in _user_agent_locks:
            _user_agent_locks[key] = threading.Lock()
    return _user_agent_locks[key].acquire(blocking=False)


def _release_user_lock(user_id: int):
    key = str(user_id)
    with _user_locks_guard:
        lock = _user_agent_locks.get(key)
    if lock and lock.locked():
        lock.release()


# ── nanobot SessionManager ───────────────────────────────────

def _sessions():
    """获取 nanobot SessionManager。"""
    from app.agent.nanobot_bridge import get_nanobot_loop
    return get_nanobot_loop().sessions


def _extract_stock_code(msg: str, ctx: Optional[Dict], session) -> Optional[str]:
    import re
    if ctx and ctx.get("stock_code"):
        return ctx["stock_code"]
    m = re.search(r"\b(\d{6})\b", msg)
    if m:
        return m.group(1)
    return session.metadata.get("stock_code") if session else None


def _build_context(data: Dict, session, message: str) -> tuple:
    session_id = data.get("session_id") or str(uuid.uuid4())
    context = data.get("context") or {}
    stock_code = _extract_stock_code(message, context, session)
    if stock_code:
        from app.agent.utils import detect_market
        market = detect_market(stock_code)
        context["stock_code"] = stock_code
        # 预取行情
        try:
            from app.data_sources.factory import DataSourceFactory
            ds = DataSourceFactory.get_source(market)
            ticker = ds.get_ticker(stock_code)
            if isinstance(ticker, dict) and "error" not in ticker:
                context["realtime_quote"] = ticker
        except Exception:
            pass
        # 查股票名称
        if not context.get("stock_name"):
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                matches = get_stock_basic_db().search_stocks(stock_code, limit=1)
                if matches:
                    context["stock_name"] = matches[0].get("name", "")
            except Exception:
                pass
        session.metadata["stock_code"] = stock_code
    return session_id, context, stock_code


def _build_executor(skills, user_id):
    from app.agent.agent import build_agent_executor
    return build_agent_executor(
        skills=skills, user_id=user_id,
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "10")),
        timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
    )


def _parse_request(data: Dict) -> tuple:
    message = data.get("message")
    if not message or not isinstance(message, str):
        return None, None, None, "Message is required and must be a string"
    if len(message) > MAX_MESSAGE_LENGTH:
        return None, None, None, f"Message too long (max {MAX_MESSAGE_LENGTH})"
    session_id = data.get("session_id") or str(uuid.uuid4())
    skills = data.get("skills")
    if not skills:
        strategy_id = data.get("strategy_id")
        if strategy_id is not None:
            skills = [str(strategy_id)]
    return message, skills, session_id, None


def _load_strategies(user_id: int = 1):
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, description, is_buy FROM qd_indicator_codes "
                "WHERE user_id = ? OR publish_to_community = 1 ORDER BY id DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [{"id": str(r["id"]), "name": r.get("name") or f"Indicator#{r['id']}",
                 "description": r.get("description") or "", "category": "indicator"}
                for r in rows]
    except Exception as e:
        logger.warning("策略加载失败: %s", e)
        return []


# ── 中断注册表 ───────────────────────────────────────────────

_interrupt_registry: Dict[str, Any] = {}
_interrupt_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/strategies", methods=["GET"])
@login_required
def get_strategies():
    try:
        from flask import g
        return jsonify({"strategies": _load_strategies(g.user_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat", methods=["POST"])
@login_required
def agent_chat():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400
        message, skills, session_id, err = _parse_request(data)
        if err:
            return jsonify({"error": err}), 400

        from flask import g
        if not _acquire_user_lock(g.user_id):
            return jsonify({"error": "当前有分析任务运行中", "code": "BUSY"}), 429
        try:
            session = _sessions().get_or_create(session_id)
            session_id, context, _ = _build_context(data, session, message)
            executor = _build_executor(skills, g.user_id)
            with _interrupt_lock:
                _interrupt_registry[session_id] = executor
            try:
                result = executor.chat(message=message, session_id=session_id,
                                       context=context, user_id=g.user_id)
            finally:
                with _interrupt_lock:
                    _interrupt_registry.pop(session_id, None)

            return jsonify({
                "success": result.success, "content": result.content,
                "session_id": session_id, "error": result.error,
                "total_steps": result.total_steps, "model": result.model,
                "tool_calls_log": result.tool_calls_log, "charts": result.charts,
            })
        finally:
            _release_user_lock(g.user_id)
    except Exception as e:
        logger.error("Agent chat failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat/stream", methods=["POST"])
@login_required
def agent_chat_stream():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400
        message, skills, session_id, err = _parse_request(data)
        if err:
            return jsonify({"error": err}), 400

        from flask import g
        if not _acquire_user_lock(g.user_id):
            return jsonify({"error": "当前有分析任务运行中", "code": "BUSY"}), 429

        session = _sessions().get_or_create(session_id)
        session_id, context, _ = _build_context(data, session, message)
        uid = g.user_id  # 捕获到局部变量，避免在 generator 线程中访问 g

        def _sse():
            event_queue: queue.Queue = queue.Queue()

            def _run():
                try:
                    executor = _build_executor(skills, uid)
                    for ev in executor.chat_stream(message=message, session_id=session_id,
                                                   context=context, user_id=uid):
                        event_queue.put(ev)
                except Exception as exc:
                    event_queue.put({"type": "error", "message": str(exc)})

            threading.Thread(target=_run, daemon=True).start()
            try:
                while True:
                    try:
                        ev = event_queue.get(timeout=300)
                        try:
                            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            # 客户端已断开，停止推送
                            logger.info("[SSE] Client disconnected, stopping stream")
                            break
                        if ev.get("type") in ("done", "error"):
                            break
                    except queue.Empty:
                        try:
                            yield f"data: {json.dumps({'type': 'error', 'message': '超时'}, ensure_ascii=False)}\n\n"
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        break
            except GeneratorExit:
                logger.info("[SSE] Client disconnected (GeneratorExit)")
            finally:
                _release_user_lock(uid)

        return Response(_sse(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat/sessions", methods=["GET"])
@login_required
def list_chat_sessions():
    limit = int(request.args.get("limit", 50))
    raw = _sessions().list_sessions()
    return jsonify({
        "sessions": [{
            "session_id": s.get("key") or s.get("session_id"),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "message_count": s.get("message_count", 0),
        } for s in (raw[:limit] if isinstance(raw, list) else [])]
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["GET"])
@login_required
def get_chat_session_messages(session_id: str):
    session = _sessions().get_or_create(session_id)
    return jsonify({
        "session_id": session_id,
        "messages": session.messages,
        "stock_code": session.metadata.get("stock_code", ""),
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_chat_session(session_id: str):
    deleted = _sessions().delete_session(session_id)
    return jsonify({"deleted": 1 if deleted else 0})


@agent_bp.route("/interrupt/<session_id>", methods=["POST"])
@login_required
def interrupt_agent(session_id: str):
    with _interrupt_lock:
        executor = _interrupt_registry.get(session_id)
    if executor and hasattr(executor, '_impl'):
        executor._impl._interrupted = True
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "No running agent found"})
