# -*- coding: utf-8 -*-
"""
Graph — LangGraph 图定义 + 编译。

图结构：prepare → planner → agent → finalize
上下文：LangGraph Checkpointer（PostgreSQL 持久化）
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

from langgraph.graph import StateGraph, END

from app.agent.state import AgentState, AgentResult, create_initial_state
from app.agent.nodes import (
    prepare_node,
    planner_node, route_after_planner,
    agent_node, route_after_agent,
    finalize_node,
)

logger = logging.getLogger(__name__)


def _build_checkpointer():
    """构建 PostgreSQL checkpointer，跨重启持久化。"""
    database_url = os.getenv("DATABASE_URL", "")

    if not database_url:
        logger.warning("[Graph] DATABASE_URL 未设置，降级 MemorySaver")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()

    # 优先: langgraph-checkpoint-postgres（官方包）
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError:
        logger.warning(
            "[Graph] langgraph-checkpoint-postgres 未安装，降级 MemorySaver。"
            "安装: pip install langgraph-checkpoint-postgres"
        )
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()

    try:
        saver = PostgresSaver.from_conn_string(database_url)
        saver.setup()  # 自动建表
        logger.info("[Graph] PostgreSQL checkpointer 就绪")
        return saver
    except Exception as e:
        logger.error("[Graph] PostgresSaver 连接失败: %s，降级 MemorySaver", e)
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("prepare", prepare_node)
    graph.add_node("planner", planner_node)
    graph.add_node("agent", agent_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("prepare")
    graph.add_edge("prepare", "planner")
    graph.add_conditional_edges("planner", route_after_planner, {
        "skip": "finalize",
        "run": "agent",
    })
    graph.add_conditional_edges("agent", route_after_agent, {
        "continue": "planner",
        "finish": "finalize",
    })
    graph.add_edge("finalize", END)

    return graph


# ── 全局单例 ─────────────────────────────────────────────────
_app = None
_checkpointer = _build_checkpointer()


def get_graph_app():
    global _app
    if _app is None:
        _app = build_graph().compile(checkpointer=_checkpointer)
        # 启动时清理 7 天前的 checkpointer 数据
        cleanup_old_checkpoints(days=7)
    return _app


def get_checkpointer():
    return _checkpointer

def cleanup_old_checkpoints(days: int = 7) -> int:
    """清理超过 N 天的 checkpointer session state。返回清理条数。"""
    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            return 0
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                # langgraph-checkpoint-postgres 的表结构
                cur.execute("""
                    DELETE FROM checkpoint_writes
                    WHERE thread_id IN (
                        SELECT DISTINCT thread_id FROM checkpoints
                        WHERE checkpoint_ts < NOW() - INTERVAL '%s days'
                    )
                """, (days,))
                deleted_writes = cur.rowcount
                cur.execute("""
                    DELETE FROM checkpoints
                    WHERE checkpoint_ts < NOW() - INTERVAL '%s days'
                """, (days,))
                deleted_checkpoints = cur.rowcount
                conn.commit()
                total = deleted_writes + deleted_checkpoints
                if total > 0:
                    logger.info("[Checkpointer] 清理 %d 天前数据: %d 条", days, total)
                return total
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[Checkpointer] 清理跳过: %s", e)
        return 0


def get_previous_state(session_id: str):
    """从 checkpointer 读取上一轮的 State。"""
    try:
        config = {"configurable": {"thread_id": session_id}}
        snapshot = _checkpointer.get(config)
        if snapshot and snapshot.values:
            return snapshot.values
    except Exception as e:
        logger.debug("[Graph] 读取历史 state 失败: %s", e)
    return None


def get_session_messages(session_id: str) -> list:
    """从 checkpointer 读取会话的完整消息历史。"""
    state = get_previous_state(session_id)
    if state:
        return state.get("messages", [])
    return []


def list_checkpointer_sessions(limit: int = 50) -> list:
    """从 PostgreSQL checkpointer 列出所有会话（按最近更新排序）。"""
    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            return []
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT thread_id, MAX(checkpoint_ts) AS latest
                    FROM checkpoints
                    GROUP BY thread_id
                    ORDER BY latest DESC
                    LIMIT %s
                """, (limit,))
                return [{"session_id": row[0], "updated_at": row[1].isoformat() if row[1] else None}
                        for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[Checkpointer] 列出会话失败: %s", e)
        return []


def delete_checkpointer_session(session_id: str) -> bool:
    """从 PostgreSQL checkpointer 删除指定会话。"""
    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            return False
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (session_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (session_id,))
                conn.commit()
                return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[Checkpointer] 删除会话失败: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════
#  公开接口（统一混合模式）
# ═══════════════════════════════════════════════════════════════

def _create_initial_state(
    message: str, session_id: str, user_id: str,
    context: Optional[Dict], max_loop_steps: int,
) -> tuple:
    """构建初始状态和 config。"""
    stock_code = (context or {}).get("stock_code", "")
    stock_name = (context or {}).get("stock_name", "")
    initial_state = create_initial_state(
        query=message, session_id=session_id, user_id=user_id,
        stock_code=stock_code, stock_name=stock_name,
        max_loop_steps=max_loop_steps,
    )
    config = {"configurable": {"thread_id": session_id}}
    return initial_state, config


def chat_hybrid(
    message: str,
    session_id: str,
    context: Optional[Dict] = None,
    user_id: str = "1",
    max_loop_steps: int = 10,
):
    """统一混合模式：内部 stream 执行，逐节点 yield SSE 事件。

    事件格式:
      {type: "node_start",  node: "prepare|planner|agent|finalize"}
      {type: "node_done",   node: "...", data: {...}}
      {type: "tool_start",  tool: "get_kline"}
      {type: "tool_done",   tool: "get_kline", success: true}
      {type: "progress",    message: "规划: get_kline, analyze_trend"}
      {type: "step_content", content: "..."}
      {type: "done",        success: true, content: "...", result: AgentResult}
      {type: "error",       message: "..."}
    """
    app = get_graph_app()
    initial_state, config = _create_initial_state(
        message, session_id, user_id, context, max_loop_steps,
    )

    final_state = None
    try:
        for event in app.stream(initial_state, config=config):
            for node_name, output in event.items():
                # ── 节点开始 ──
                yield {"type": "node_start", "node": node_name}

                if node_name == "planner":
                    tools = output.get("current_tools", [])
                    skill = output.get("current_skill")
                    parts = []
                    if tools:
                        parts.append(f"工具: {', '.join(tools)}")
                    if skill:
                        parts.append(f"技能: {skill}")
                    if parts:
                        yield {"type": "progress", "message": f"── {' | '.join(parts)} ──"}

                elif node_name == "agent":
                    for tc in output.get("tool_calls_log", []):
                        yield {"type": "tool_start", "tool": tc.get("tool", "")}
                        yield {"type": "tool_done", "tool": tc.get("tool", ""), "success": tc.get("success", True)}
                    # 从 step_records 读取，不依赖顶层 step_content
                    _records = output.get("step_records", [])
                    if _records:
                        _sc = _records[-1].get("step_content", "")
                        if _sc:
                            yield {"type": "step_content", "content": _sc[:2000]}

                elif node_name == "finalize":
                    final_state = output

                yield {"type": "node_done", "node": node_name}

    except Exception as e:
        logger.error("[Graph] 执行失败: %s", e, exc_info=True)
        yield {"type": "error", "message": str(e)}
        return

    # ── 最终结果 ──
    if final_state is None:
        # finalize 未执行到（理论上不会发生）
        yield {"type": "error", "message": "执行异常：finalize 节点未执行"}
        return

    final_output = final_state.get("final_output", {})
    # 避免双重 JSON 序列化：如果 final_output 已经是 str 直接用，否则 json.dumps
    if isinstance(final_output, str):
        content = final_output
    elif isinstance(final_output, dict):
        content = json.dumps(final_output, ensure_ascii=False)
    else:
        content = str(final_output) if final_output else ""
    if not content:
        contents = [
            r.get("step_content", "")
            for r in final_state.get("step_records", [])
            if r.get("step_content")
        ]
        content = "\n\n".join(contents)

    result = AgentResult(
        success=bool(content), content=content,
        tool_calls_log=final_state.get("tool_calls_log", []),
        total_steps=final_state.get("total_steps", 0),
        total_tokens=final_state.get("total_tokens", 0),
        model="langgraph", charts=final_state.get("charts", []),
    )
    yield {
        "type": "done",
        "success": result.success,
        "content": content,
        "total_steps": result.total_steps,
        "total_tokens": result.total_tokens,
        "tool_calls_log": result.tool_calls_log,
        "charts": result.charts,
    }


def chat(
    message: str,
    session_id: str,
    context: Optional[Dict] = None,
    user_id: str = "1",
    max_loop_steps: int = 10,
) -> AgentResult:
    """同步调用（内部走 chat_hybrid，收集最终结果）。"""
    result = None
    for ev in chat_hybrid(message, session_id, context, user_id, max_loop_steps):
        if ev["type"] == "done":
            result = AgentResult(
                success=ev.get("success", False),
                content=ev.get("content", ""),
                tool_calls_log=ev.get("tool_calls_log", []),
                total_steps=ev.get("total_steps", 0),
                total_tokens=ev.get("total_tokens", 0),
                model="langgraph", charts=ev.get("charts", []),
            )
        elif ev["type"] == "error":
            return AgentResult(success=False, error=ev.get("message", ""))
    return result or AgentResult(success=False, error="执行未完成")
