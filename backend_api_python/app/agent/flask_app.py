# -*- coding: utf-8 -*-
"""
Flask 壳 — 共用 CLI 链路（QDAgent）。

路由：
  POST /api/agent-v2/chat          — 普通对话（SSE 流式）
  POST /api/agent-v2/task          — 带工具调用的 ReAct 任务
  GET  /api/agent-v2/tools         — 列出可用工具
  GET  /api/agent-v2/skills        — 列出可用技能
  GET  /api/agent-v2/health        — 健康检查
  GET  /api/agent-v2/info          — 配置信息

使用方式：
    from app.agent.flask_app import register_agent_routes
    register_agent_routes(app)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import uuid

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

# ── 路径设置 ──────────────────────────────────────────────────
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_agent_dir = os.path.abspath(os.path.dirname(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _ensure_agent_path():
    if sys.path[0] != _agent_dir:
        try:
            sys.path.remove(_agent_dir)
        except ValueError:
            pass
        sys.path.insert(0, _agent_dir)


# ── 工具/技能适配器（仅用于信息展示）──
def _get_tool_adapter():
    _ensure_agent_path()
    from llm import QDToolAdapter
    return QDToolAdapter()


def _get_skill_adapter():
    _ensure_agent_path()
    from llm import QDSkillAdapter
    return QDSkillAdapter()


# ── Blueprint ─────────────────────────────────────────────────
agent_v2_bp = Blueprint("agent_v2", __name__, url_prefix="/api/agent-v2")


def _run_agent(message: str, session_id: str) -> str:
    """同步执行 QDAgent.chat()（在后台线程中调用）。"""
    _ensure_agent_path()
    from agent import QDAgent

    agent = QDAgent()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        response = loop.run_until_complete(agent.chat(message, session_id=session_id))
        return response.content
    finally:
        loop.close()


def _sse_stream(message: str, session_id: str, timeout: int = 300):
    """SSE 生成器：在线程中运行 agent，通过队列推送结果。"""
    q: queue.Queue = queue.Queue()

    def _run():
        try:
            result = _run_agent(message, session_id)
            q.put({"type": "done", "content": result, "session_id": session_id})
        except Exception as exc:
            logger.error("Agent 异常: %s", exc, exc_info=True)
            q.put({"type": "error", "message": str(exc)})

    threading.Thread(target=_run, daemon=True).start()
    while True:
        try:
            ev = q.get(timeout=timeout)
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") in ("done", "error"):
                break
        except queue.Empty:
            yield f'data: {json.dumps({"type": "error", "message": "超时"}, ensure_ascii=False)}\n\n'
            break


# ── 路由 ──────────────────────────────────────────────────────


@agent_v2_bp.route("/health", methods=["GET"])
def health():
    try:
        _ensure_agent_path()
        from app.agent.config.loader import get_settings
        s = get_settings()
        return jsonify({"status": "ok", "version": s.version, "env": s.env})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@agent_v2_bp.route("/info", methods=["GET"])
def info():
    try:
        _ensure_agent_path()
        from app.agent.config.loader import get_settings
        s = get_settings()
        tools = _get_tool_adapter()
        skills = _get_skill_adapter()
        return jsonify({
            "version": s.version,
            "env": s.env,
            "llm": {"provider": s.llm.provider, "qd_provider": s.llm.qd_provider, "model": s.llm.model},
            "tools_count": len(tools),
            "skills_count": len(skills),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/tools", methods=["GET"])
def list_tools():
    try:
        tools = _get_tool_adapter()
        result = []
        for name in tools.list_tools():
            schema = tools.get_schema(name)
            desc = schema.get("function", {}).get("description", "")[:200] if schema else ""
            result.append({"name": name, "description": desc})
        return jsonify({"total": len(result), "tools": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/skills", methods=["GET"])
def list_skills():
    try:
        skills = _get_skill_adapter()
        return jsonify({"total": len(skills), "skills": skills.list_skills()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/chat", methods=["POST"])
def chat():
    """普通对话（SSE）。共用 CLI 链路，QDAgent 内部决定是否调用工具。"""
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
    """带工具调用的 ReAct 任务（SSE）。共用 CLI 链路。"""
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
