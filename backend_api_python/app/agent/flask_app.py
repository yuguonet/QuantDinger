# -*- coding: utf-8 -*-
"""
Flask 壳 — 共用 agent.py 全局组件。

路由：
  POST /api/agent-v2/chat          — 普通对话（SSE 流式）
  POST /api/agent-v2/task          — 带工具调用的任务
  GET  /api/agent-v2/tools         — 列出可用工具
  GET  /api/agent-v2/skills        — 列出可用技能
  GET  /api/agent-v2/health        — 健康检查
  GET  /api/agent-v2/info          — 配置信息

使用方式：
    from app.agent.flask_app import register_agent_routes
    register_agent_routes(app)
"""
from __future__ import annotations

import json
import logging
import uuid

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

# ── Blueprint ─────────────────────────────────────────────────
agent_v2_bp = Blueprint("agent_v2", __name__, url_prefix="/api/agent-v2")


def _sse_stream(message: str, session_id: str, timeout: int = 300):
    """SSE 生成器（2026-09-11 重写）：过程事件真流式。

    机制：event_cb 把 agent 过程事件（节点生命周期/工具步骤）入队；
    本生成器 0.3s 粒度排空队列 yield 给前端，15s 无事件发心跳注释行保活
    （防 nginx/代理空闲断连）。future 完成后补排残余事件，最后发 done
    （content=AgentResponse.content，与前端 onDone 契约一致）。
    前端 agent.js 已实现 node/tool/step/progress 全部回调，事件契约不变。
    """
    import queue as _queue
    from message_queue import submit

    ev_queue: _queue.Queue = _queue.Queue()

    def _event_cb(ev: dict):
        """agent 线程 -> SSE 通道的唯一入口。绝不抛异常、绝不阻塞。"""
        try:
            ev_queue.put({"type": "event", **ev}, timeout=1)
        except Exception:
            pass  # 流式通道故障不影响主任务

    future = submit(message, session_id=session_id, timeout=timeout, event_cb=_event_cb)

    ticks = 0
    while True:
        try:
            ev = ev_queue.get(timeout=0.3)
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            continue
        except _queue.Empty:
            pass
        if future.done():
            # 排空残余事件再收尾（事件与 done 可能乱序到达队列）
            while True:
                try:
                    ev = ev_queue.get_nowait()
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except _queue.Empty:
                    break
            break
        ticks += 1
        if ticks >= 50:  # 0.3s * 50 = 15s 心跳
            yield ": ping\n\n"
            ticks = 0

    try:
        result = future.result(timeout=1)
        yield f"data: {json.dumps({'type': 'done', 'content': result, 'session_id': session_id}, ensure_ascii=False)}\n\n"
    except Exception as exc:
        logger.error("Agent 异常: %s", exc, exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


# ── 路由 ──────────────────────────────────────────────────────


@agent_v2_bp.route("/health", methods=["GET"])
def health():
    try:
        from agent import settings, skills
        return jsonify({
            "status": "ok",
            "version": settings.version,
            "env": settings.env,
            "skills": len(skills),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@agent_v2_bp.route("/info", methods=["GET"])
def info():
    try:
        from agent import settings, skills
        return jsonify({
            "version": settings.version,
            "env": settings.env,
            "llm": {"provider": settings.llm.provider, "qd_provider": settings.llm.qd_provider, "model": settings.llm.model},
            "skills_count": len(skills),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/tools", methods=["GET"])
def list_tools():
    return jsonify({"total": 0, "tools": [], "note": "工具通过 list_tools/search_tools 动态发现"})


@agent_v2_bp.route("/skills", methods=["GET"])
def list_skills():
    try:
        from agent import skills
        return jsonify({"total": len(skills), "skills": skills.list_skills()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _resolve_user_ns() -> str:
    """多用户上下文命名空间（2026-09-11）。

    JWT 有效 -> "user:<id>"；否则回退 "anon:<ip>"。
    session_id 最终形如 "user:3:session_xxx"，memory/事件总线/trace 全链路按此 key
    隔离 —— 上下文在后端按用户隔离，前端仅缓存显示层。
    """
    try:
        from app.utils.auth import verify_token
        auth = (request.headers.get("Authorization") or request.headers.get("token") or "").strip()
        token = auth[7:] if auth.lower().startswith("bearer ") else auth
        if token:
            payload = verify_token(token)
            if payload and payload.get("user_id") is not None:
                return f"user:{payload['user_id']}"
    except Exception:
        pass
    return f"anon:{request.remote_addr or 'unknown'}"


@agent_v2_bp.route("/chat", methods=["POST"])
def chat():
    """普通对话（SSE）。TaskAgent 内部决定是否调用工具。"""
    try:
        data = request.get_json() or {}
        message = data.get("message", "").strip()
        session_id = data.get("session_id") or str(uuid.uuid4())
        # 多用户隔离（2026-09-11）：按 JWT user_id 加命名空间前缀
        session_id = f"{_resolve_user_ns()}:{session_id}"
        if not message:
            return jsonify({"error": "message 不能为空"}), 400

        return Response(
            _sse_stream(message, session_id, timeout=120),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/task", methods=["POST"])
def task():
    """带工具调用的任务（SSE）。"""
    try:
        data = request.get_json() or {}
        message = data.get("message", "").strip()
        session_id = data.get("session_id") or str(uuid.uuid4())
        # 多用户隔离（2026-09-11）：按 JWT user_id 加命名空间前缀
        session_id = f"{_resolve_user_ns()}:{session_id}"
        if not message:
            return jsonify({"error": "message 不能为空"}), 400

        return Response(
            _sse_stream(message, session_id, timeout=300),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def register_agent_routes(app):
    app.register_blueprint(agent_v2_bp)
    logger.info("[AgentV2] 路由已注册: /api/agent-v2/*")


def create_agent_blueprint() -> Blueprint:
    return agent_v2_bp
