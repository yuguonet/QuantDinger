"""
任务型 Agent

支持 Function Calling 的 Agent，实现 ReAct 循环：
1. 用户输入 -> LLM 思考
2. LLM 决定调用工具 -> 执行工具 -> 将结果返回给 LLM
3. 重复步骤 2 直到 LLM 给出最终回答
"""
import time
import logging
from typing import Optional

from llm.base import LLMBase, ChatMessage
from memory.base import MemoryBase
from rag.retriever import Retriever
from tools.registry import ToolRegistry
from agents.base import AgentBase, AgentResponse
from utils.tracing import AgentTraceRecorder, llm_response_to_dict, messages_to_dict

logger = logging.getLogger(__name__)


class TaskAgent(AgentBase):
    """
    任务型 Agent（支持 Tool Calling）

    实现 ReAct 循环：思考 -> 调用工具 -> 观察 -> 再思考 -> 最终回答

    使用示例：
        registry = ToolRegistry()
        registry.add(WebSearchTool())
        registry.add(CodeExecutorTool())

        agent = TaskAgent(
            llm=llm,
            memory=LocalMemory(),
            tool_registry=registry,
            system_prompt="你是一个能使用工具的智能助手。",
            max_tool_rounds=5,
        )
        response = await agent.chat("帮我搜索今天的新闻")
    """

    def __init__(
        self,
        llm: LLMBase,
        memory: Optional[MemoryBase] = None,
        retriever: Optional[Retriever] = None,
        tool_registry: Optional[ToolRegistry] = None,
        system_prompt: str = "你是一个智能助手，可以使用工具来完成任务。",
        memory_window_size: int = 10,
        max_tool_rounds: int = 5,
    ):
        super().__init__(
            llm=llm,
            memory=memory,
            retriever=retriever,
            tool_registry=tool_registry,
            system_prompt=system_prompt,
            memory_window_size=memory_window_size,
        )
        self.max_tool_rounds = max_tool_rounds

    async def chat(
        self,
        user_input: str,
        session_id: str = "default",
        use_rag: bool = True,
    ) -> AgentResponse:
        """
        带工具调用的对话入口

        实现 ReAct 循环：
        LLM 回复 -> 检查是否有 tool_calls -> 执行工具 -> 将结果传回 LLM -> 循环
        """
        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=type(self).__name__,
            session_id=session_id,
            user_input=user_input,
            metadata={"use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            # 1. 构建消息列表
            messages = [ChatMessage(role="system", content=self.system_prompt)]

            # 2. RAG 检索
            sources = []
            if use_rag and self.retriever:
                rag_start = time.time()
                docs = await self.retriever.retrieve(user_input)
                trace.record(
                    "rag_retrieve",
                    {
                        "elapsed_seconds": round(time.time() - rag_start, 3),
                        "doc_count": len(docs),
                        "docs": docs,
                    },
                )
                if docs:
                    context = Retriever.format_context(docs)
                    messages.append(ChatMessage(
                        role="system",
                        content=f"【参考资料】\n{context}\n\n请结合参考资料回答。",
                    ))
                    sources = [
                        {
                            "content": d["content"][:200],
                            "score": d.get("score", 0),
                            "metadata": d.get("metadata", {}),
                        }
                        for d in docs
                    ]

            # 3. 加载对话历史
            if self.memory:
                history = await self.memory.get_history(session_id, limit=self.memory_window_size)
                trace.record(
                    "memory_load",
                    {
                        "history_count": len(history),
                        "limit": self.memory_window_size,
                        "messages": [
                            {"role": msg.role, "content": msg.content}
                            for msg in history
                        ],
                    },
                )
                for msg in history:
                    messages.append(ChatMessage(role=msg.role, content=msg.content))

            # 4. 添加用户输入
            messages.append(ChatMessage(role="user", content=user_input))

            # 5. 获取工具 Schema
            tools = self.tool_registry.get_function_schemas() if self.tool_registry else None
            trace.record("tools_available", {"tools": tools or []})

            # 6. ReAct 循环
            all_tool_calls = []
            llm_response = None
            for round_idx in range(self.max_tool_rounds):
                trace.record(
                    "llm_request",
                    {
                        "round": round_idx + 1,
                        "model": getattr(self.llm, "model", ""),
                        "message_count": len(messages),
                        "messages": messages_to_dict(messages),
                        "tools": tools or [],
                    },
                )
                llm_start = time.time()
                llm_response = await self.llm.generate(messages=messages, tools=tools)
                trace.record(
                    "llm_response",
                    {
                        "round": round_idx + 1,
                        "elapsed_seconds": round(time.time() - llm_start, 3),
                        **llm_response_to_dict(llm_response),
                    },
                )

                if not llm_response.is_tool_call:
                    # LLM 给出了最终回答
                    break

                # 处理工具调用
                logger.info(f"[ReAct 第{round_idx + 1}轮] 工具调用: {len(llm_response.tool_calls)} 个")

                # 将 assistant 的 tool_calls 消息加入历史
                messages.append(ChatMessage(
                    role="assistant",
                    content=llm_response.content or "",
                    tool_calls=llm_response.tool_calls,
                ))

                # 逐个执行工具
                for tc in llm_response.tool_calls:
                    func_name = tc.get("function", {}).get("name", "")
                    func_args = tc.get("function", {}).get("arguments", "{}")
                    logger.info(f"  执行工具: {func_name}")
                    trace.record(
                        "tool_request",
                        {
                            "round": round_idx + 1,
                            "tool_call_id": tc.get("id", ""),
                            "tool": func_name,
                            "arguments": func_args,
                        },
                    )

                    tool_start = time.time()
                    result = await self.tool_registry.call_from_llm_response(tc)
                    trace.record(
                        "tool_response",
                        {
                            "round": round_idx + 1,
                            "tool_call_id": tc.get("id", ""),
                            "tool": func_name,
                            "elapsed_seconds": round(time.time() - tool_start, 3),
                            "success": result.success,
                            "output": result.output,
                            "error": result.error,
                        },
                    )
                    all_tool_calls.append({
                        "tool": func_name,
                        "result": result.to_str()[:500],
                        "success": result.success,
                    })

                    # 将工具结果传回 LLM
                    messages.append(ChatMessage(
                        role="tool",
                        content=result.to_str(),
                        tool_call_id=tc.get("id", ""),
                        name=func_name,
                    ))
            else:
                # 达到最大轮次仍未结束
                logger.warning(f"达到最大工具调用轮次 ({self.max_tool_rounds})")
                trace.record("tool_round_limit_reached", {"max_tool_rounds": self.max_tool_rounds})

            if llm_response is None:
                raise RuntimeError("LLM 未返回响应")

            # 7. 保存对话历史
            if self.memory:
                await self.memory.add(session_id, "user", user_input)
                await self.memory.add(session_id, "assistant", llm_response.content)
                trace.record("memory_save", {"messages_saved": 2})

            elapsed = time.time() - start_time

            response = AgentResponse(
                content=llm_response.content,
                tool_calls=all_tool_calls,
                sources=sources,
                tokens_used=llm_response.tokens_used,
                model=llm_response.model,
                session_id=session_id,
                elapsed_seconds=round(elapsed, 2),
                metadata={"trace_id": trace.trace_id},
            )
            trace.finish(response=response.to_dict())
            return response
        except Exception as e:
            trace.fail(e)
            raise
