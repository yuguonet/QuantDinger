# -*- coding: utf-8 -*-
"""
/api/agent/* — AI Agent 聊天 & 流式接口
Nanobot 内核 — AgentLoop + ToolRegistry + SkillsLoader
"""
import os
import json
import logging
import uuid
import threading
import queue
import tempfile
import time
from typing import Any, Dict, Optional
from pathlib import Path

from flask import Blueprint, request, jsonify, Response, send_file
from app.utils.auth import login_required

MAX_MESSAGE_LENGTH = int(os.getenv("AGENT_MAX_MESSAGE_LENGTH", "4000"))
AGENT_SAVE_DIR = os.getenv("AGENT_SAVE_DIR", os.path.join(tempfile.gettempdir(), "qd_agents"))

logger = logging.getLogger(__name__)


# ── 策略加载 ─────────────────────────────────────────────────
def _load_strategies(user_id: int = 1):
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, description, is_buy, publish_to_community "
                "FROM qd_indicator_codes "
                "WHERE user_id = ? OR publish_to_community = 1 "
                "ORDER BY id DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            {
                "id": str(r["id"]),
                "name": r.get("name") or f"Indicator#{r['id']}",
                "description": r.get("description") or "",
                "category": "indicator",
                "is_purchased": bool(r.get("is_buy")),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("策略加载器不可用: %s", e)
        return []


# ── Blueprint ─────────────────────────────────────────────────
agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

# ── Per-user 互斥锁：同一用户同时只能有一个 agent 在跑 ────────
_user_agent_locks: Dict[str, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def _acquire_user_lock(user_id: int) -> bool:
    """尝试获取用户锁，已占用则返回 False。"""
    key = str(user_id)
    with _user_locks_guard:
        if key not in _user_agent_locks:
            _user_agent_locks[key] = threading.Lock()
        lock = _user_agent_locks[key]
    return lock.acquire(blocking=False)


def _release_user_lock(user_id: int):
    """释放用户锁。"""
    key = str(user_id)
    with _user_locks_guard:
        lock = _user_agent_locks.get(key)
    if lock and lock.locked():
        lock.release()


# ── 共享工具 ─────────────────────────────────────────────────
from app.agent.utils import detect_market as _detect_market
from app.agent.session_store import get_session_store


def _extract_stock_code(msg: str, ctx: Optional[Dict], session: Dict) -> Optional[str]:
    import re
    if ctx and ctx.get("stock_code"):
        return ctx["stock_code"]
    m = re.search(r"\b(\d{6})\b", msg)
    if m:
        return m.group(1)
    return session.get("stock_code")


MAX_HISTORY_TURNS = 20


def _get_session(session_id: str) -> Dict:
    store = get_session_store()
    session = store.get_session(session_id)
    if not session:
        session = store.create_session(session_id, {})
    return session


def _touch_session(session_id: str, **fields):
    get_session_store().update_session(session_id, **fields)


def _prefetch_context(stock_code: str, market: str) -> Dict[str, Any]:
    context = {}
    try:
        from app.data_sources.factory import DataSourceFactory
        ds = DataSourceFactory.get_source(market)
        try:
            ticker = ds.get_ticker(stock_code)
            if isinstance(ticker, dict) and "error" not in ticker:
                context["realtime_quote"] = ticker
        except Exception:
            pass
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


def _build_context(data: Dict, session: Dict, message: str) -> tuple:
    session_id = data.get("session_id") or str(uuid.uuid4())
    context = data.get("context") or {}
    stock_code = _extract_stock_code(message, context, session)
    if stock_code:
        market = _detect_market(stock_code)
        prefetch = _prefetch_context(stock_code, market)
        context.update(prefetch)
        context["stock_code"] = stock_code
        # 查股票名称（避免 LLM 瞎编）
        if not context.get("stock_name"):
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                _matches = get_stock_basic_db().search_stocks(stock_code, limit=1)
                if _matches:
                    context["stock_name"] = _matches[0].get("name", "")
            except Exception:
                pass
        _touch_session(session_id, stock_code=stock_code)
    return session_id, context, stock_code


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


# ═══════════════════════════════════════════════════════════════
# 路由: 策略列表
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/strategies", methods=["GET"])
@login_required
def get_strategies():
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400
        from flask import g
        strategies = _load_strategies(user_id=g.user_id)
        return jsonify({
            "strategies": [
                {"id": s["id"], "name": s["name"],
                 "description": s.get("description", ""),
                 "category": s.get("category", "indicator")}
                for s in strategies
            ]
        })
    except Exception as e:
        logger.error("Get strategies failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: 同步聊天
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chat", methods=["POST"])
@login_required
def agent_chat():
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message, skills, session_id, err = _parse_request(data)
        if err:
            return jsonify({"error": err}), 400

        from flask import g
        if not _acquire_user_lock(g.user_id):
            return jsonify({"error": "当前有分析任务运行中，请等待完成后再试", "code": "BUSY"}), 429

        try:
            session = _get_session(session_id)
            session_id, context, _ = _build_context(data, session, message)

            # ── Nanobot Agent（替代 smolagents）──
            from app.agent.nanobot_agent import get_nanobot_agent
            agent = get_nanobot_agent()
            result = agent.chat(
                message=message, session_id=session_id,
                context=context, user_id=g.user_id,
            )

            return jsonify({
                "success": result.success, "content": result.content,
                "session_id": session_id, "error": result.error,
                "total_steps": result.total_steps, "total_tokens": result.total_tokens,
                "model": result.model, "tool_calls_log": result.tool_calls_log,
                "charts": result.charts,
            })
        finally:
            _release_user_lock(g.user_id)
    except Exception as e:
        logger.error("Agent chat failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: 流式聊天 (SSE)
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chat/stream", methods=["POST"])
@login_required
def agent_chat_stream():
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message, skills, session_id, err = _parse_request(data)
        if err:
            return jsonify({"error": err}), 400

        from flask import g
        user_id = g.user_id
        if not _acquire_user_lock(user_id):
            return jsonify({"error": "当前有分析任务运行中，请等待完成后再试", "code": "BUSY"}), 429

        session = _get_session(session_id)
        session_id, context, _ = _build_context(data, session, message)

        def _sse():
            event_queue: queue.Queue = queue.Queue()

            def _run():
                try:
                    # ── Nanobot Agent 流式（替代 smolagents）──
                    from app.agent.nanobot_agent import get_nanobot_agent
                    agent = get_nanobot_agent()
                    for ev in agent.chat_stream(
                        message=message, session_id=session_id,
                        context=context, user_id=user_id,
                    ):
                        event_queue.put(ev)
                except Exception as exc:
                    logger.error("Agent stream error: %s", exc, exc_info=True)
                    event_queue.put({"type": "error", "message": str(exc)})

            t = threading.Thread(target=_run, daemon=True)
            t.start()

            try:
                while True:
                    try:
                        ev = event_queue.get(timeout=300)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        if ev.get("type") in ("done", "error"):
                            break
                    except queue.Empty:
                        yield f"data: {json.dumps({'type': 'error', 'message': '分析超时'}, ensure_ascii=False)}\n\n"
                        break
            finally:
                _release_user_lock(user_id)

        return Response(
            _sse(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
    except Exception as e:
        logger.error("Agent stream failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: 会话管理
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chat/sessions", methods=["GET"])
@login_required
def list_chat_sessions():
    limit = int(request.args.get("limit", 50))
    raw = get_session_store().list_sessions(limit)
    return jsonify({
        "sessions": [
            {"session_id": s["session_id"], "created_at": s.get("created_at"),
             "updated_at": s.get("updated_at"),
             "message_count": len(s.get("messages", [])),
             "stock_code": s.get("stock_code")}
            for s in raw
        ]
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["GET"])
@login_required
def get_chat_session_messages(session_id: str):
    session = get_session_store().get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({
        "session_id": session_id,
        "messages": session.get("messages", []),
        "stock_code": session.get("stock_code"),
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_chat_session(session_id: str):
    store = get_session_store()
    store.clear_history(session_id)
    store.clear_tool_results(session_id)
    deleted = store.delete_session(session_id)
    # 清除意图路由上下文
    try:
        from app.agent.intent_analyzer import _get_context_manager
        ctx_mgr = _get_context_manager()
        ctx_mgr.clear_session(session_id)
    except Exception:
        pass
    return jsonify({"deleted": 1 if deleted else 0})


# ═══════════════════════════════════════════════════════════════
# 路由: 工具列表（纯 QuantDinger 工具，无 smolagents 依赖）
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/tools", methods=["GET"])
@login_required
def list_tools():
    """列出所有可用工具。"""
    try:
        from app.agent.tools.registry import registry as tool_registry
        tool_registry.discover()

        tools_info = []
        for name, spec in sorted(tool_registry._tools.items()):
            tools_info.append({
                "name": name,
                "description": spec.description[:150],
                "category": spec.category,
                "layer": spec.layer,
                "domain": spec.domain,
            })

        return jsonify({
            "total": len(tools_info),
            "tools": tools_info,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: Chain 决策评估（手动触发 + 状态查询）
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chain/evaluate", methods=["POST"])
@login_required
def trigger_chain_evaluation():
    """手动触发 Chain 决策评估闭环。"""
    try:
        data = request.get_json(silent=True) or {}
        days_old = int(data.get("days_old", 1))
        market = data.get("market", "CNStock")

        from app.agent.chain.evaluator import auto_evaluate
        result = auto_evaluate(days_old=days_old, market=market)
        return jsonify(result)
    except Exception as e:
        logger.error("[API] 手动评估失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chain/eval-stats", methods=["GET"])
@login_required
def get_chain_eval_status():
    """查询 Chain 决策评估统计。"""
    try:
        chain_id = request.args.get("chain_id")
        from app.agent.chain.store import get_eval_stats
        stats = get_eval_stats(chain_id)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chain/worker-health", methods=["GET"])
@login_required
def get_chain_worker_health():
    """查询 Chain 评估 Worker 健康状态。"""
    try:
        from app.agent.chain.evaluator import get_worker_health
        return jsonify(get_worker_health())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
