# -*- coding: utf-8 -*-
"""
Flask 集成壳 — 将 agent 模板接入 QuantDinger Flask 应用。

路由（与 agent_blueprint.py 并存，不冲突）：
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
from typing import Dict, Optional

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
    """确保 app/agent/ 在 sys.path 头部。"""
    if sys.path[0] != _agent_dir:
        try:
            sys.path.remove(_agent_dir)
        except ValueError:
            pass
        sys.path.insert(0, _agent_dir)


# ── 懒加载组件 ────────────────────────────────────────────────
_cache: Dict[str, object] = {}


def _get_llm():
    if "llm" not in _cache:
        _ensure_agent_path()
        from llm import create_llm
        _cache["llm"] = create_llm()
    return _cache["llm"]


def _get_tool_adapter():
    if "tools" not in _cache:
        _ensure_agent_path()
        from llm import QDToolAdapter
        _cache["tools"] = QDToolAdapter()
    return _cache["tools"]


def _get_skill_adapter():
    if "skills" not in _cache:
        _ensure_agent_path()
        from llm import QDSkillAdapter
        _cache["skills"] = QDSkillAdapter()
    return _cache["skills"]


# ── Blueprint ─────────────────────────────────────────────────
agent_v2_bp = Blueprint("agent_v2", __name__, url_prefix="/api/agent-v2")


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
    """列出所有可用工具。"""
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
    """列出所有可用技能。"""
    try:
        skills = _get_skill_adapter()
        return jsonify({"total": len(skills), "skills": skills.list_skills()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/chat", methods=["POST"])
def chat():
    """普通对话（无工具，SSE）。"""
    try:
        data = request.get_json() or {}
        message = data.get("message", "").strip()
        session_id = data.get("session_id") or str(uuid.uuid4())
        if not message:
            return jsonify({"error": "message 不能为空"}), 400

        llm = _get_llm()

        def _sse():
            q: queue.Queue = queue.Queue()

            def _run():
                try:
                    from llm import run_with_tools
                    from utils.prompt_loader import load_prompt

                    system_prompt = load_prompt("chat_system.txt")
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ]
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    answer = loop.run_until_complete(run_with_tools(llm, messages, adapter=None))
                    q.put({"type": "done", "content": answer, "session_id": session_id})
                except Exception as exc:
                    q.put({"type": "error", "message": str(exc)})

            threading.Thread(target=_run, daemon=True).start()
            while True:
                try:
                    ev = q.get(timeout=120)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") in ("done", "error"):
                        break
                except queue.Empty:
                    yield f'data: {json.dumps({"type": "error", "message": "超时"}, ensure_ascii=False)}\n\n'
                    break

        return Response(_sse(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_v2_bp.route("/task", methods=["POST"])
def task():
    """带工具调用的 ReAct 任务（SSE）。"""
    try:
        data = request.get_json() or {}
        message = data.get("message", "").strip()
        session_id = data.get("session_id") or str(uuid.uuid4())
        if not message:
            return jsonify({"error": "message 不能为空"}), 400

        llm = _get_llm()
        tools = _get_tool_adapter()
        skills = _get_skill_adapter()

        def _sse():
            q: queue.Queue = queue.Queue()

            def _run():
                try:
                    from llm import run_with_tools
                    from utils.prompt_loader import load_prompt

                    tool_names = tools.list_tools()
                    tool_catalog = ", ".join(tool_names[:30])
                    if len(tool_names) > 30:
                        tool_catalog += f" ... 共 {len(tool_names)} 个"

                    system_prompt = load_prompt(
                        "tool_system.txt",
                        tool_count=len(tools),
                        tool_catalog=tool_catalog,
                        skill_catalog=skills.get_catalog_text(),
                    )

                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ]

                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    answer = loop.run_until_complete(
                        run_with_tools(llm, messages, tools)
                    )
                    q.put({"type": "done", "content": answer, "session_id": session_id})
                except Exception as exc:
                    logger.error("Task 异常: %s", exc, exc_info=True)
                    q.put({"type": "error", "message": str(exc)})

            threading.Thread(target=_run, daemon=True).start()
            while True:
                try:
                    ev = q.get(timeout=300)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") in ("done", "error"):
                        break
                except queue.Empty:
                    yield f'data: {json.dumps({"type": "error", "message": "超时"}, ensure_ascii=False)}\n\n'
                    break

        return Response(_sse(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def register_agent_routes(app):
    """注册 agent_v2 路由到 Flask app。"""
    app.register_blueprint(agent_v2_bp)
    logger.info("[AgentV2] 路由已注册: /api/agent-v2/*")


def create_agent_blueprint() -> Blueprint:
    return agent_v2_bp
