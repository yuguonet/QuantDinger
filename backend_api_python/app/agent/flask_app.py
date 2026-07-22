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
    """SSE 生成器：通过统一消息队列执行 agent，推送结果。"""
    from message_queue import submit

    try:
        future = submit(message, session_id=session_id, timeout=timeout)
        result = future.result(timeout=timeout)
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


@agent_v2_bp.route("/chat", methods=["POST"])
def chat():
    """普通对话（SSE）。TaskAgent 内部决定是否调用工具。"""
    try:
        data = request.get_json() or {}
        message = data.get("message", "").strip()
        session_id = data.get("session_id") or str(uuid.uuid4())
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
