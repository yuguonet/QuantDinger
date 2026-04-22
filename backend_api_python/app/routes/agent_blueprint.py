# -*- coding: utf-8 -*-
"""
/api/agent/* — AI Agent 聊天 & 流式接口
v3 — 真正的 ReAct Tool-Calling Agent，基于 app/agent 模块。

对标 daily_stock_analysis 的 Agent 架构：
- OpenAI 原生 function calling（非文本模拟）
- 多步 ReAct 循环（可配置 max_steps）
- 工具并行执行
- SSE 流式进度推送
"""
import os
import json
import logging
import uuid
import threading
import queue
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, request, jsonify, Response

# ── 输入限制 ────────────────────────────────────────────────
MAX_MESSAGE_LENGTH = int(os.getenv("AGENT_MAX_MESSAGE_LENGTH", "4000"))

logger = logging.getLogger(__name__)

# ── 策略加载 ──────────────────────────────────────────────────
def _load_strategies():
    try:
        from app.services.strategy_loader import load_strategies
        return load_strategies()
    except Exception as e:
        logger.warning("策略加载器不可用: %s", e)
        return []


# ── Blueprint ─────────────────────────────────────────────────
agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

# ── 工具中文名映射（前端显示用）───────────────────────────────
TOOL_DISPLAY_NAMES: Dict[str, str] = {
    "get_realtime_quote": "获取实时行情",
    "get_daily_history": "获取历史K线",
    "get_chip_distribution": "分析筹码分布",
    "get_stock_info": "获取股票基本面",
    "search_stock_news": "搜索股票新闻",
    "search_comprehensive_intel": "综合情报搜索",
    "analyze_trend": "分析技术趋势",
    "calculate_ma": "计算均线系统",
    "get_volume_analysis": "分析量能变化",
    "analyze_pattern": "识别K线形态",
    "get_market_indices": "获取市场指数",
    "get_sector_rankings": "分析行业板块",
}


# ── 共享市场检测 ─────────────────────────────────────────────
from app.agent.utils import detect_market as _detect_market


# ── 股票代码提取 ─────────────────────────────────────────────

def _extract_stock_code(msg: str, ctx: Optional[Dict], session: Dict) -> Optional[str]:
    """从上下文、消息中提取股票代码。"""
    import re
    if ctx and ctx.get("stock_code"):
        return ctx["stock_code"]
    m = re.search(r"\b(\d{6})\b", msg)
    if m:
        return m.group(1)
    return session.get("stock_code")


# ── 会话存储（Redis / 内存自动降级）──────────────────────────
from app.agent.session_store import get_session_store

MAX_HISTORY_TURNS = 20


def _get_session(session_id: str) -> Dict:
    store = get_session_store()
    session = store.get_session(session_id)
    if not session:
        session = store.create_session(session_id, {})
    return session


def _touch_session(session_id: str, **fields):
    store = get_session_store()
    store.update_session(session_id, **fields)


# ── 预取数据（避免 Agent 重复调用工具）────────────────────────

def _prefetch_context(stock_code: str, market: str) -> Dict[str, Any]:
    """预先获取行情 + 筹码数据，注入 Agent 上下文避免重复工具调用。"""
    context = {}
    try:
        from app.data_sources.factory import DataSourceFactory
        ds = DataSourceFactory.get_source(market)

        # 实时行情
        try:
            ticker = ds.get_ticker(stock_code)
            if isinstance(ticker, dict) and "error" not in ticker:
                context["realtime_quote"] = ticker
        except Exception:
            pass

        # 筹码分布（仅A股）
        if market == "CNStock" and hasattr(ds, "get_chip_distribution"):
            try:
                chip = ds.get_chip_distribution(stock_code)
                if isinstance(chip, dict) and "error" not in chip:
                    context["chip_distribution"] = chip
            except Exception:
                pass
    except Exception as e:
        logger.debug("Prefetch failed for %s: %s", stock_code, e)

    return context


# ── 路由 ──────────────────────────────────────────────────────

@agent_bp.route("/strategies", methods=["GET"])
def get_strategies():
    """获取可用 Agent 策略。"""
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400

        strategies = _load_strategies()
        result = [
            {
                "id": s["id"],
                "name": s["name"],
                "description": s.get("description", ""),
                "category": s.get("category", "general"),
            }
            for s in strategies
        ]
        return jsonify({"strategies": result})
    except Exception as e:
        logger.error("Get strategies failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat", methods=["POST"])
def agent_chat():
    """普通聊天接口（同步，阻塞等待结果）。"""
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message = data.get("message")
        if not message:
            return jsonify({"error": "Message is required"}), 400
        if not isinstance(message, str):
            return jsonify({"error": "Message must be a string"}), 400
        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({"error": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"}), 400

        session_id = data.get("session_id") or str(uuid.uuid4())
        if not isinstance(session_id, str) or len(session_id) > 128:
            return jsonify({"error": "Invalid session_id"}), 400
        skills = data.get("skills")
        if skills is not None and not isinstance(skills, (list, str)):
            return jsonify({"error": "skills must be a list or string"}), 400
        context = data.get("context")
        if context is not None and not isinstance(context, dict):
            return jsonify({"error": "context must be a dict"}), 400

        # Build executor via factory
        from app.agent.factory import build_agent_executor
        executor = build_agent_executor(
            skills=skills,
            max_steps=int(os.getenv("AGENT_MAX_STEPS", "10")),
            timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
        )

        # Extract stock code and prefetch
        session = _get_session(session_id)
        stock_code = _extract_stock_code(message, context, session)
        if stock_code:
            market = _detect_market(stock_code)
            prefetch = _prefetch_context(stock_code, market)
            if context:
                context.update(prefetch)
            else:
                context = prefetch
            context["stock_code"] = stock_code
            _touch_session(session_id, stock_code=stock_code)

        result = executor.chat(
            message=message,
            session_id=session_id,
            context=context,
        )

        return jsonify({
            "success": result.success,
            "content": result.content,
            "session_id": session_id,
            "error": result.error,
            "total_steps": result.total_steps,
            "total_tokens": result.total_tokens,
            "model": result.model,
            "tool_calls_log": result.tool_calls_log,
        })
    except Exception as e:
        logger.error("Agent chat failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat/stream", methods=["POST"])
def agent_chat_stream():
    """流式聊天（SSE），实时推送工具调用进度。"""
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message = data.get("message")
        if not message:
            return jsonify({"error": "Message is required"}), 400
        if not isinstance(message, str):
            return jsonify({"error": "Message must be a string"}), 400
        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({"error": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"}), 400

        session_id = data.get("session_id") or str(uuid.uuid4())
        if not isinstance(session_id, str) or len(session_id) > 128:
            return jsonify({"error": "Invalid session_id"}), 400
        skills = data.get("skills")
        if skills is not None and not isinstance(skills, (list, str)):
            return jsonify({"error": "skills must be a list or string"}), 400
        context = data.get("context")
        if context is not None and not isinstance(context, dict):
            return jsonify({"error": "context must be a dict"}), 400

        event_queue: queue.Queue = queue.Queue()

        def _cb(event: dict):
            if event.get("type") in ("tool_start", "tool_done"):
                tool = event.get("tool", "")
                event["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
            event_queue.put(event)

        def _run():
            try:
                from app.agent.factory import build_agent_executor

                # Extract stock code and prefetch
                session = _get_session(session_id)
                stock_code = _extract_stock_code(message, context, session)
                enriched_ctx = dict(context) if context else {}
                if stock_code:
                    market = _detect_market(stock_code)
                    prefetch = _prefetch_context(stock_code, market)
                    enriched_ctx.update(prefetch)
                    enriched_ctx["stock_code"] = stock_code
                    _touch_session(session_id, stock_code=stock_code)

                executor = build_agent_executor(
                    skills=skills,
                    max_steps=int(os.getenv("AGENT_MAX_STEPS", "10")),
                    timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
                )
                r = executor.chat(
                    message=message,
                    session_id=session_id,
                    context=enriched_ctx,
                    progress_callback=_cb,
                )
                event_queue.put({
                    "type": "done",
                    "success": r.success,
                    "content": r.content,
                    "error": r.error,
                    "total_steps": r.total_steps,
                    "total_tokens": r.total_tokens,
                    "model": r.model,
                    "session_id": session_id,
                })
            except Exception as exc:
                logger.error("Agent stream error: %s", exc, exc_info=True)
                event_queue.put({"type": "error", "message": str(exc)})

        def _sse():
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            while True:
                try:
                    ev = event_queue.get(timeout=300)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") in ("done", "error"):
                        break
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'error', 'message': '分析超时'}, ensure_ascii=False)}\n\n"
                    break

        return Response(
            _sse(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    except Exception as e:
        logger.error("Agent stream failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat/sessions", methods=["GET"])
def list_chat_sessions():
    """获取会话列表。"""
    limit = int(request.args.get("limit", 50))
    store = get_session_store()
    raw = store.list_sessions(limit)
    sessions = [{
        "session_id": s["session_id"],
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "message_count": len(s.get("messages", [])),
        "stock_code": s.get("stock_code"),
    } for s in raw]
    return jsonify({"sessions": sessions})


@agent_bp.route("/chat/sessions/<session_id>", methods=["GET"])
def get_chat_session_messages(session_id: str):
    """获取会话消息。"""
    store = get_session_store()
    session = store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({
        "session_id": session_id,
        "messages": session.get("messages", []),
        "stock_code": session.get("stock_code"),
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["DELETE"])
def delete_chat_session(session_id: str):
    """删除会话。"""
    store = get_session_store()
    store.clear_history(session_id)
    deleted = store.delete_session(session_id)
    return jsonify({"deleted": 1 if deleted else 0})
