# -*- coding: utf-8 -*-
"""
Graph — LangGraph 图定义 + 编译。

图结构：prepare → planner → agent → finalize
上下文：LangGraph Checkpointer（SQLite 持久化）
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from langgraph.graph import StateGraph, END

from app.agent.state import AgentState, AgentResult, create_initial_state
from app.agent.nodes import (
    prepare_node, route_after_prepare,
    planner_node,
    agent_node,
    finalize_node,
)

logger = logging.getLogger(__name__)


def _build_checkpointer():
    """构建 SQLite checkpointer，跨重启持久化。"""
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
        saver = SqliteSaver(conn)
        logger.info("[Graph] SQLite checkpointer 就绪")
        return saver
    except ImportError:
        logger.warning("[Graph] langgraph-checkpoint-sqlite 未安装，降级 MemorySaver")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("prepare", prepare_node)
    graph.add_node("planner", planner_node)
    graph.add_node("agent", agent_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("prepare")
    graph.add_conditional_edges("prepare", route_after_prepare, {
        "skip": "finalize",
        "plan": "planner",
    })
    graph.add_edge("planner", "agent")
    graph.add_edge("agent", "finalize")
    graph.add_edge("finalize", END)

    return graph


# ── 全局单例 ─────────────────────────────────────────────────
_app = None
_checkpointer = _build_checkpointer()


def get_graph_app():
    global _app
    if _app is None:
        _app = build_graph().compile(checkpointer=_checkpointer)
    return _app


def get_checkpointer():
    return _checkpointer


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


# ═══════════════════════════════════════════════════════════════
#  公开接口
# ═══════════════════════════════════════════════════════════════

def chat(
    message: str,
    session_id: str,
    context: Optional[Dict] = None,
    user_id: str = "1",
    max_loop_steps: int = 10,
) -> AgentResult:
    app = get_graph_app()

    stock_code = (context or {}).get("stock_code", "")
    stock_name = (context or {}).get("stock_name", "")

    initial_state = create_initial_state(
        query=message, session_id=session_id, user_id=user_id,
        stock_code=stock_code, stock_name=stock_name,
        max_loop_steps=max_loop_steps,
    )

    config = {"configurable": {"thread_id": session_id}}

    try:
        final_state = app.invoke(initial_state, config=config)
    except Exception as e:
        logger.error("[Graph] 执行失败: %s", e, exc_info=True)
        return AgentResult(success=False, error=str(e))

    final_output = final_state.get("final_output", {})
    content = json.dumps(final_output, ensure_ascii=False) if final_output else ""
    if not content:
        contents = [r.get("step_content", "") for r in final_state.get("step_records", []) if r.get("step_content")]
        content = "\n\n".join(contents)

    return AgentResult(
        success=bool(content), content=content,
        tool_calls_log=final_state.get("tool_calls_log", []),
        total_steps=final_state.get("total_steps", 0),
        total_tokens=final_state.get("total_tokens", 0),
        model="langgraph", charts=final_state.get("charts", []),
    )


def chat_stream(
    message: str,
    session_id: str,
    context: Optional[Dict] = None,
    user_id: str = "1",
    max_loop_steps: int = 10,
):
    app = get_graph_app()

    stock_code = (context or {}).get("stock_code", "")
    stock_name = (context or {}).get("stock_name", "")

    initial_state = create_initial_state(
        query=message, session_id=session_id, user_id=user_id,
        stock_code=stock_code, stock_name=stock_name,
        max_loop_steps=max_loop_steps,
    )

    config = {"configurable": {"thread_id": session_id}}

    try:
        for event in app.stream(initial_state, config=config):
            for node_name, output in event.items():
                if node_name == "prepare":
                    if not output.get("should_continue", True):
                        yield {"type": "generating", "message": output.get("enriched", "")}
                elif node_name == "planner":
                    tools = output.get("current_tools", [])
                    if tools:
                        yield {"type": "tool_info", "tool": "", "message": f"── 规划: {', '.join(tools)} ──"}
                elif node_name == "agent":
                    for tc in output.get("tool_calls_log", []):
                        yield {"type": "tool_start", "tool": tc.get("tool", "")}
                        yield {"type": "tool_done", "tool": tc.get("tool", ""), "success": tc.get("success", True)}
                    step_content = output.get("step_content", "")
                    if step_content:
                        yield {"type": "tool_info", "tool": "", "message": step_content[:500]}
                elif node_name == "finalize":
                    final_output = output.get("final_output", {})
                    content = json.dumps(final_output, ensure_ascii=False) if final_output else ""
                    yield {"type": "done", "success": bool(content), "content": content}
    except Exception as e:
        logger.error("[Graph] 流式执行失败: %s", e, exc_info=True)
        yield {"type": "error", "message": str(e)}
