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


def _translate_agent_event(ev: dict) -> list:
    """agent 语义事件 -> 前端 wire 协议帧（按 type 分发）。

    前端 agent.js 的分发契约：node_start / node_done / progress /
    step_content / tool_start / tool_done / done / error。
    agent 侧以 kind 标记语义，在此翻译——保持前端契约不变。
    （2026-09-12 修复：原实现直接包 {"type":"event"}，与前端分发不匹配，
    过程事件被静默丢弃。）
    """
    kind = ev.get("kind")
    if kind == "action_step":
        # smolagents 步骤完成 -> 工具卡片（start+done 一对）
        n = ev.get("step_number")
        n = n if n is not None else "?"
        tools = ev.get("tools") or []
        tool_id = "step_%s" % n
        if tools:
            disp = "步骤 %s · %s" % (n, "、".join(str(t) for t in tools[:3]))
        else:
            disp = "分析步骤 %s" % n
        info = ""
        for _ln in str(ev.get("code_action") or "").splitlines():
            _ln = _ln.strip()
            if _ln and not _ln.startswith("#") and not _ln.startswith("```"):
                info = _ln[:60]
                break
        frames = [{"type": "tool_start", "tool": tool_id,
                   "display_name": disp, "info": info}]
        done_ev = {"type": "tool_done", "tool": tool_id,
                   "success": not ev.get("error")}
        if ev.get("error"):
            done_ev["recovery"] = str(ev.get("error"))[:160]
        frames.append(done_ev)
        return frames
    if kind in ("node_start", "node_done"):
        return [{"type": kind, "node": ev.get("node"), "label": ev.get("label")}]
    if kind == "node_error":
        label = ev.get("label") or ev.get("node") or "节点"
        return [{"type": "progress", "message": "⚠ %s 执行异常，正在恢复" % label}]
    if kind == "step_content":
        return [{"type": "step_content", "content": ev.get("content")}]
    if kind == "progress":
        return [{"type": "progress", "message": ev.get("message")}]
    # 未知事件原样透传（向前兼容）
    return [{"type": "event", **ev}]


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
            for _frame in _translate_agent_event(ev):
                ev_queue.put(_frame, timeout=1)
        except Exception:
            pass  # 流式通道故障不影响主任务

    future = submit(message, session_id=session_id, timeout=timeout, event_cb=_event_cb)

    # 硬超时（2026-09-12 卡死事故防线）：worker 可能被卡死（调试器挂起/底层调用永久阻塞），
    # future 永不 done → 原实现无限心跳、前端永久转圈。到点必须发终帧收尾。
    import os as _os
    import time as _time
    _t0 = _time.monotonic()
    _hard_limit = timeout + float(_os.getenv("SSE_HARD_TIMEOUT_EXTRA", "600"))

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
        if _time.monotonic() - _t0 > _hard_limit:
            logger.error("[SSE] 硬超时触发: session=%s 已等待 %.0fs 仍未完成，判定 worker 卡死",
                         session_id, _time.monotonic() - _t0)
            yield f"data: {json.dumps({'type': 'error', 'message': '处理超时：任务长时间无响应，已停止等待。请重试；若连续出现请重启后端服务。'}, ensure_ascii=False)}\n\n"
            return
        ticks += 1
        if ticks >= 50:  # 0.3s * 50 = 15s 心跳
            yield ": ping\n\n"
            ticks = 0

    try:
        result = future.result(timeout=1)
        yield f"data: {json.dumps({'type': 'done', 'content': result, 'session_id': session_id}, ensure_ascii=False)}\n\n"
    except Exception as exc:
        logger.error("Agent 异常: %s", exc, exc_info=True)
        # 超时文案（2026-09-12）：asyncio.wait_for 抛出的 TimeoutError 其 str 为空，
        # 前端只能拿到空错误；这里给出可读说明（阶段模式长任务被服务端等待上限截断）
        if isinstance(exc, TimeoutError):
            _err_msg = "处理超时：任务耗时超过服务端等待上限（%ss），请重试；长任务建议缩小范围" % timeout
        else:
            _err_msg = str(exc) or "服务端处理异常"
        yield f"data: {json.dumps({'type': 'error', 'message': _err_msg}, ensure_ascii=False)}\n\n"


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
            # 阶段模式实测（2026-09-12）：完整多阶段管线 7~10 分钟，300s 常在收尾前截断
        _sse_stream(message, session_id, timeout=600),
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
