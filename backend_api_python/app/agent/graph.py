# -*- coding: utf-8 -*-
"""
graph.py — 轻量状态机（移植 LangGraph 核心设计模式）

不引入 langgraph 依赖，自己实现：
  - StateGraph：节点 + 边 + 条件边
  - CompiledGraph：ainvoke（同步执行）+ astream（流式执行）
  - Checkpointer 接口：状态持久化

用法：
    from graph import StateGraph, END

    graph = StateGraph(AgentState)
    graph.add_node("chat", chat_node)
    graph.add_node("plan", plan_node)
    graph.add_conditional_edges("chat", route_after_chat, {"plan": "plan", "finalize": "finalize"})
    graph.add_conditional_edges("plan", route_after_plan, {"execute": "execute", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    graph.set_entry_point("chat")

    compiled = graph.compile(checkpointer=my_checkpointer)
    result = await compiled.ainvoke({"user_input": "分析300129"})

    # 或流式执行
    async for event in compiled.astream({"user_input": "分析300129"}):
        print(event["node"], event["state"])
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, TypedDict

logger = logging.getLogger(__name__)

# 终止标记
END = "__end__"


class StateGraph:
    """状态图定义。

    节点是 async 函数，签名为 async def node(state: dict) -> dict | None：
      - 输入：当前完整状态
      - 输出：partial state（只返回需要更新的字段，自动合并到主状态）
      - 返回 None 表示不更新状态
    """

    def __init__(self, state_type: type = None):
        """
        Args:
            state_type: 状态类型（TypedDict），用于类型提示，运行时不强制校验。
        """
        self._state_type = state_type
        self._nodes: Dict[str, Callable] = {}
        self._edges: Dict[str, str] = {}
        self._conditional: Dict[str, tuple[Callable, Dict[str, str]]] = {}
        self._entry: str = ""

    def add_node(self, name: str, func: Callable):
        """注册节点。

        Args:
            name: 节点名（唯一）
            func: async def node(state: dict) -> dict | None
        """
        if name in (END,):
            raise ValueError(f"节点名 '{name}' 是保留字")
        self._nodes[name] = func

    def add_edge(self, source: str, target: str):
        """添加固定边：source 执行完后一定走 target。

        Args:
            source: 源节点名
            target: 目标节点名或 END
        """
        self._edges[source] = target

    def add_conditional_edges(self, source: str, condition: Callable, mapping: Dict[str, str]):
        """添加条件边：source 执行完后，根据 condition(state) 的返回值选择目标。

        Args:
            source: 源节点名
            condition: def condition(state: dict) -> str，返回 mapping 中的 key
            mapping: {route_name: target_node_name}
        """
        self._conditional[source] = (condition, mapping)

    def set_entry_point(self, name: str):
        """设置入口节点。"""
        if name not in self._nodes:
            raise ValueError(f"入口节点 '{name}' 未注册")
        self._entry = name

    def compile(self, checkpointer=None) -> "CompiledGraph":
        """编译为可执行图。

        Args:
            checkpointer: 可选，Checkpointer 实例，用于状态持久化。

        Returns:
            CompiledGraph 实例，可调用 ainvoke / astream。
        """
        if not self._entry:
            raise ValueError("未设置入口节点，请调用 set_entry_point()")
        return CompiledGraph(self, checkpointer)


class CompiledGraph:
    """编译后的状态图，可执行。"""

    def __init__(self, graph: StateGraph, checkpointer=None):
        self._graph = graph
        self._checkpointer = checkpointer

    async def ainvoke(self, state: dict, config: dict = None) -> dict:
        """同步执行（阻塞到完成）。

        Args:
            state: 初始状态
            config: 可选配置（如 thread_id）

        Returns:
            最终状态
        """
        thread_id = state.get("session_id", "default")

        # 从 checkpointer 恢复状态（如果有）
        if self._checkpointer and config and config.get("resume"):
            saved = await self._checkpointer.load(thread_id)
            if saved:
                state.update(saved)
                logger.info("[Graph] 从 checkpoint 恢复: thread=%s", thread_id)

        current = self._graph._entry
        while current != END:
            node_func = self._graph._nodes.get(current)
            if not node_func:
                raise ValueError(f"节点 '{current}' 未注册")

            # 执行节点
            logger.info("[Graph] 执行节点: %s", current)
            try:
                partial = await node_func(state)
                if partial and isinstance(partial, dict):
                    state.update(partial)
            except KeyboardInterrupt:
                logger.warning("[Graph] 节点 '%s' 被用户中断", current)
                raise
            except Exception as e:
                logger.error("[Graph] 节点 '%s' 异常: %s", current, e)
                state["error"] = str(e)
                state["failed_node"] = current
                # 如果有错误处理边，走错误处理
                if "on_error" in self._graph._edges:
                    current = self._graph._edges["on_error"]
                    continue
                raise

            # 持久化
            if self._checkpointer:
                await self._checkpointer.save(state, current)

            # 路由
            current = self._route(current, state)

        return state

    async def astream(self, state: dict, config: dict = None):
        """流式执行，每完成一个节点 yield 事件。

        Yields:
            {"node": str, "state": dict, "done": bool}
        """
        thread_id = state.get("session_id", "default")

        # 从 checkpointer 恢复
        if self._checkpointer and config and config.get("resume"):
            saved = await self._checkpointer.load(thread_id)
            if saved:
                state.update(saved)

        current = self._graph._entry
        while current != END:
            node_func = self._graph._nodes.get(current)
            if not node_func:
                raise ValueError(f"节点 '{current}' 未注册")

            logger.info("[Graph] 执行节点: %s", current)
            try:
                partial = await node_func(state)
                if partial and isinstance(partial, dict):
                    state.update(partial)
            except Exception as e:
                logger.error("[Graph] 节点 '%s' 异常: %s", current, e)
                state["error"] = str(e)
                state["failed_node"] = current
                yield {"node": current, "state": dict(state), "error": str(e)}
                if "on_error" in self._graph._edges:
                    current = self._graph._edges["on_error"]
                    continue
                raise

            # 持久化
            if self._checkpointer:
                await self._checkpointer.save(state, current)

            yield {"node": current, "state": dict(state), "done": False}

            # 路由
            current = self._route(current, state)

        yield {"node": END, "state": dict(state), "done": True}

    def _route(self, current: str, state: dict) -> str:
        """根据当前节点和状态，决定下一个节点。"""
        # 条件边优先
        if current in self._graph._conditional:
            condition, mapping = self._graph._conditional[current]
            route = condition(state)
            if route not in mapping:
                raise ValueError(
                    f"节点 '{current}' 的条件路由返回 '{route}'，"
                    f"但 mapping 中只有 {list(mapping.keys())}"
                )
            return mapping[route]

        # 固定边
        if current in self._graph._edges:
            return self._graph._edges[current]

        # 无边可走，结束
        logger.warning("[Graph] 节点 '%s' 无出边，自动结束", current)
        return END
