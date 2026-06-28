# -*- coding: utf-8 -*-
"""
/api/agent/* — AI Agent 聊天 & 流式接口
LangGraph 内核 — StateGraph + smolagents CodeAgent + PostgreSQL Checkpointer
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
from app.agent.graph import (
    get_session_messages, list_checkpointer_sessions, delete_checkpointer_session,
    get_previous_state,
)


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


def _build_chat_hybrid():
    from app.agent.graph import chat_hybrid
    max_steps = int(os.getenv("AGENT_MAX_STEPS", "10"))

    def _chat(message, session_id, context, user_id):
        return chat_hybrid(
            message=message, session_id=session_id,
            context=context, user_id=user_id,
            max_loop_steps=max_steps,
        )
    return _chat


def _parse_request(data: Dict) -> tuple:
    message = data.get("message")
    if not message or not isinstance(message, str):
        return None, None, "Message is required and must be a string"
    if len(message) > MAX_MESSAGE_LENGTH:
        return None, None, f"Message too long (max {MAX_MESSAGE_LENGTH})"
    session_id = data.get("session_id") or str(uuid.uuid4())
    return message, session_id, None


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
# 路由: 聊天（统一混合模式 — 内部 stream，SSE 输出）
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chat", methods=["POST"])
@login_required
def agent_chat():
    """统一聊天端点：内部 stream 执行，SSE 推送进度 + 最终结果。"""
    try:
        if os.getenv("AGENT_MODE", "true").lower() != "true":
            return jsonify({"error": "Agent mode is not enabled"}), 400
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message, session_id, err = _parse_request(data)
        if err:
            return jsonify({"error": err}), 400

        from flask import g
        user_id = g.user_id
        if not _acquire_user_lock(user_id):
            return jsonify({"error": "当前有分析任务运行中，请等待完成后再试", "code": "BUSY"}), 429

        session = _get_session(session_id)
        session_id, context, _ = _build_context(data, session, message)

        chat_fn = _build_chat_hybrid()

        def _sse():
            event_queue: queue.Queue = queue.Queue()

            def _run():
                try:
                    for ev in chat_fn(
                        message=message, session_id=session_id,
                        context=context, user_id=user_id,
                    ):
                        event_queue.put(ev)
                except Exception as exc:
                    logger.error("Agent 执行异常: %s", exc, exc_info=True)
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
        logger.error("Agent chat failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: 会话管理
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chat/sessions", methods=["GET"])
@login_required
def list_chat_sessions():
    limit = int(request.args.get("limit", 50))
    sessions = list_checkpointer_sessions(limit)
    # 补充 session_store 里的元数据（stock_code 等）
    store = get_session_store()
    for s in sessions:
        meta = store.get_session(s["session_id"]) or {}
        s["stock_code"] = meta.get("stock_code", "")
        s["created_at"] = meta.get("created_at")
        # 从 checkpointer 读消息数
        state = get_previous_state(s["session_id"])
        s["message_count"] = len(state.get("messages", [])) if state else 0
    return jsonify({"sessions": sessions})


@agent_bp.route("/chat/sessions/<session_id>", methods=["GET"])
@login_required
def get_chat_session_messages(session_id: str):
    messages = get_session_messages(session_id)
    if not messages:
        # 可能是空会话或不存在
        state = get_previous_state(session_id)
        if not state:
            return jsonify({"error": "Session not found"}), 404
    meta = get_session_store().get_session(session_id) or {}
    return jsonify({
        "session_id": session_id,
        "messages": messages,
        "stock_code": meta.get("stock_code", ""),
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_chat_session(session_id: str):
    # 清 checkpointer（消息+state）
    deleted_cp = delete_checkpointer_session(session_id)
    # 清 session_store（元数据）
    store = get_session_store()
    store.delete_session(session_id)
    # 清意图路由上下文
    try:
        from app.agent.intent_analyzer import _get_context_manager
        ctx_mgr = _get_context_manager()
        ctx_mgr.clear_session(session_id)
    except Exception:
        pass
    return jsonify({"deleted": 1 if deleted_cp else 0})


# ═══════════════════════════════════════════════════════════════
# 路由: Agent 可视化 (visualize)
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/visualize", methods=["GET"])
@login_required
def visualize_agent():
    """返回 Agent 结构树（工具列表、managed agents、配置）。"""
    try:
        from flask import g

        from app.agent.agent import get_smolagent
        agent = get_smolagent(user_id=g.user_id)

        # Collect agent structure
        tools_info = []
        for name, tool in agent.tools.items():
            tools_info.append({
                "name": name,
                "description": tool.description[:200],
                "inputs": tool.inputs,
                "output_type": tool.output_type,
            })

        managed_info = []
        for name, ma in agent.managed_agents.items():
            managed_info.append({
                "name": name,
                "description": ma.description,
                "tools": list(ma.tools.keys()) if hasattr(ma, "tools") else [],
            })

        return jsonify({
            "agent_type": type(agent).__name__,
            "model": str(getattr(agent.model, "model_id", "")),
            "max_steps": agent.max_steps,
            "planning_interval": agent.planning_interval,
            "tools_count": len(tools_info),
            "tools": tools_info,
            "managed_agents": managed_info,
            "instructions_preview": (agent.instructions or "")[:500],
        })
    except Exception as e:
        logger.error("Visualize failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: Agent 保存 (save)
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/save", methods=["POST"])
@login_required
def save_agent():
    """保存当前 Agent 配置到磁盘（可复现部署）。

    Body:
        name (str): Agent 保存名称
    """
    try:
        from flask import g
        data = request.get_json() or {}
        name = data.get("name", f"agent_{uuid.uuid4().hex[:8]}")
        from app.agent.agent import get_smolagent
        agent = get_smolagent(user_id=g.user_id)

        save_dir = os.path.join(AGENT_SAVE_DIR, name)
        os.makedirs(save_dir, exist_ok=True)

        agent.save(save_dir)

        return jsonify({
            "success": True,
            "path": save_dir,
            "files": os.listdir(save_dir),
        })
    except Exception as e:
        logger.error("Save agent failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/saved", methods=["GET"])
@login_required
def list_saved_agents():
    """列出已保存的 Agent。"""
    try:
        if not os.path.exists(AGENT_SAVE_DIR):
            return jsonify({"agents": []})
        agents = []
        for name in sorted(os.listdir(AGENT_SAVE_DIR)):
            agent_dir = os.path.join(AGENT_SAVE_DIR, name)
            if os.path.isdir(agent_dir):
                meta = {}
                agent_json = os.path.join(agent_dir, "agent.json")
                if os.path.exists(agent_json):
                    with open(agent_json) as f:
                        meta = json.load(f)
                agents.append({
                    "name": name,
                    "path": agent_dir,
                    "tools_count": len(meta.get("tools", [])),
                    "class": meta.get("class", ""),
                    "model": meta.get("model", {}).get("data", {}).get("model_id", ""),
                })
        return jsonify({"agents": agents})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: Agent 回放 (replay)
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/replay/<session_id>", methods=["GET"])
@login_required
def replay_session(session_id: str):
    """回放指定会话的 Agent 执行过程（从 LangGraph checkpointer 读取）。

    Query params:
        detailed (bool): 是否包含每步的完整内存状态
    """
    try:
        detailed = request.args.get("detailed", "false").lower() == "true"

        state = get_previous_state(session_id)
        if not state:
            return jsonify({"error": "Session not found"}), 404

        messages = state.get("messages", [])
        step_records = state.get("step_records", [])

        replay = []
        for i, msg in enumerate(messages):
            entry = {
                "step": i,
                "role": msg.get("role"),
                "content": msg.get("content", "")[:2000] if not detailed else msg.get("content", ""),
            }
            replay.append(entry)

        # 附加 step_records（工具调用详情）
        if detailed and step_records:
            for sr in step_records:
                replay.append({
                    "step": sr.get("step", "?"),
                    "role": "tool_calls",
                    "description": sr.get("description", ""),
                    "tools": sr.get("tools", []),
                    "tool_calls": sr.get("tool_calls", []),
                })

        meta = get_session_store().get_session(session_id) or {}
        return jsonify({
            "session_id": session_id,
            "stock_code": meta.get("stock_code", ""),
            "loop_step": state.get("loop_step", 0),
            "total_steps": state.get("total_steps", 0),
            "replay": replay,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: Agent 中断 (interrupt)
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/interrupt/<session_id>", methods=["POST"])
@login_required
def interrupt_agent(session_id: str):
    """中断正在运行的 Agent。"""
    from app.agent.nodes import get_active_agent
    agent = get_active_agent(session_id)
    if agent:
        agent.interrupt()
        return jsonify({"success": True, "message": "Agent interrupt signal sent"})
    return jsonify({"success": False, "message": "No running agent found for this session"})


# ═══════════════════════════════════════════════════════════════
# 路由: 工具列表 (含 Hub/MCP 来源标记)
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/tools", methods=["GET"])
@login_required
def list_tools():
    """列出所有可用工具（含来源分类）。"""
    try:
        from app.agent.tool_adapter import build_all_tools
        tools = build_all_tools()

        qd_tools = []
        builtin_tools = []
        hub_tools = []
        mcp_tools = []

        qd_names = set()
        from app.agent.tools.registry import get_local_registry as _get_registry
        qd_names.update(_get_registry().all_names)

        for t in tools:
            info = {"name": t.name, "description": t.description[:150]}
            if t.name in qd_names:
                qd_tools.append(info)
            elif t.name in {"duckduckgo_search", "google_search", "web_search",
                            "visit_webpage", "wikipedia_search", "user_input"}:
                builtin_tools.append(info)
            else:
                hub_tools.append(info)

        return jsonify({
            "total": len(tools),
            "quantdinger": qd_tools,
            "builtin": builtin_tools,
            "hub_mcp": hub_tools,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 路由: Chain 决策评估（手动触发 + 状态查询）
# ═══════════════════════════════════════════════════════════════

@agent_bp.route("/chain/evaluate", methods=["POST"])
@login_required
def trigger_chain_evaluation():
    """手动触发 Chain 决策评估闭环。

    Body (JSON, all optional):
        days_old: int = 1       — 只评估至少 N 天前的决策
        market: str = "CNStock" — 市场类型
    """
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
    """查询 Chain 决策评估统计。

    Query params:
        chain_id: str (optional) — 指定链路 ID
    """
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
    """查询 Chain 评估 Worker 健康状态。

    返回：
        is_alive: bool — worker 线程是否存活
        last_run_at: str — 上次运行时间
        last_success_at: str — 上次成功时间
        last_error: str — 上次错误信息
        consecutive_failures: int — 连续失败次数
        total_runs / total_successes / total_failures — 累计统计
        current_interval: int — 当前等待间隔（秒）
    """
    try:
        from app.agent.chain.evaluator import get_worker_health
        return jsonify(get_worker_health())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
