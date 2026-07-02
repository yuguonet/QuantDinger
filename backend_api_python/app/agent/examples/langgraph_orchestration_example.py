"""
示例 6: LangGraph 编排 Agent

演示如何把当前模板中的 LLM、Tool、RAG 组件接入 LangGraph。
流程: retrieve -> plan -> optional tool -> answer

运行:
    python -m examples.langgraph_orchestration_example
"""

import asyncio
from typing import Any, TypedDict

from agents import AgentResponse
from config.loader import get_settings
from llm import ChatMessage, LLMFactory
from rag import KeywordRetriever, MultiRouteRetriever, Retriever, RetrieverRoute
from tools import ToolRegistry
from tools.builtin import CodeExecutorTool
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict, messages_to_dict


try:
    from langgraph.graph import END, START, StateGraph
except ImportError as exc:
    raise SystemExit(
        "未安装 langgraph，请先运行: pip install langgraph"
    ) from exc


class GraphState(TypedDict, total=False):
    """LangGraph 状态对象，节点之间通过它传递数据。"""

    user_input: str
    session_id: str
    context: str
    sources: list[dict]
    plan: dict[str, Any]
    tool_result: str
    final_answer: str
    tokens_used: int
    model: str


def build_demo_retriever() -> MultiRouteRetriever:
    """构造一个不依赖外部向量库的多路召回示例。"""
    docs = [
        {
            "content": "Agent 模板把能力拆为 LLM、Tools、Skills、Memory、RAG、Config 六层。",
            "metadata": {"source": "architecture"},
        },
        {
            "content": "TaskAgent 通过 ReAct 循环处理 tool_calls，适合需要工具调用的任务。",
            "metadata": {"source": "agent"},
        },
        {
            "content": "MultiRouteRetriever 支持多路召回，并使用 RRF 对不同检索路线的结果融合排序。",
            "metadata": {"source": "rag"},
        },
    ]
    keyword = KeywordRetriever(docs, top_k=4)
    return MultiRouteRetriever(
        routes=[RetrieverRoute("keyword", keyword, weight=1.0)],
        top_k=4,
    )


async def run_graph(question: str, session_id: str = "langgraph_demo") -> AgentResponse:
    trace = AgentTraceRecorder(
        agent_type="LangGraphOrchestration",
        session_id=session_id,
        user_input=question,
    )
    settings = get_settings(config_dir="config")
    llm = LLMFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        temperature=0.3,
        max_tokens=settings.llm.max_tokens,
    )

    retriever = build_demo_retriever()
    registry = ToolRegistry()
    registry.register(CodeExecutorTool())

    async def retrieve_node(state: GraphState) -> GraphState:
        docs = await retriever.retrieve(state["user_input"])
        context = Retriever.format_context(docs)
        trace.record(
            "rag_retrieve",
            {
                "doc_count": len(docs),
                "docs": docs,
            },
        )
        return {
            "context": context,
            "sources": [
                {
                    "content": doc.get("content", "")[:200],
                    "score": doc.get("score", 0),
                    "metadata": doc.get("metadata", {}),
                }
                for doc in docs
            ],
        }

    async def plan_node(state: GraphState) -> GraphState:
        prompt = f"""
请判断用户问题是否需要执行 Python 代码工具。
只返回 JSON，不要解释。

可用工具:
- execute_python: 执行 Python 代码

返回格式:
{{
  "need_tool": true/false,
  "tool_name": "execute_python",
  "arguments": {{"code": "print(1 + 1)"}},
  "reason": "简短原因"
}}

参考资料:
{state.get("context", "")}

用户问题:
{state["user_input"]}
"""
        messages = [
            ChatMessage(role="system", content="你是一个严谨的任务规划器。"),
            ChatMessage(role="user", content=prompt),
        ]
        trace.record(
            "llm_request",
            {
                "node": "plan",
                "model": getattr(llm, "model", ""),
                "messages": messages_to_dict(messages),
                "tools": [],
            },
        )
        response = await llm.generate(messages)
        trace.record(
            "llm_response",
            {"node": "plan", **llm_response_to_dict(response)},
        )
        plan = safe_parse_json(response.content, default={"need_tool": False})
        return {
            "plan": plan,
            "tokens_used": response.tokens_used,
            "model": response.model,
        }

    async def tool_node(state: GraphState) -> GraphState:
        plan = state.get("plan") or {}
        trace.record(
            "tool_request",
            {
                "node": "tool",
                "tool": plan.get("tool_name", ""),
                "arguments": plan.get("arguments") or {},
            },
        )
        result = await registry.call(
            plan.get("tool_name", ""),
            **(plan.get("arguments") or {}),
        )
        trace.record(
            "tool_response",
            {
                "node": "tool",
                "tool": plan.get("tool_name", ""),
                "success": result.success,
                "output": result.output,
                "error": result.error,
            },
        )
        return {"tool_result": result.to_str()}

    async def answer_node(state: GraphState) -> GraphState:
        tool_part = ""
        if state.get("tool_result"):
            tool_part = f"\n\n【工具执行结果】\n{state['tool_result']}"

        messages = [
            ChatMessage(
                role="system",
                content="你是一个中文 AI 助手。请结合参考资料和工具结果回答，缺失信息要说明。",
            ),
            ChatMessage(
                role="user",
                content=(
                    f"【参考资料】\n{state.get('context', '')}"
                    f"{tool_part}\n\n【用户问题】\n{state['user_input']}"
                ),
            ),
        ]
        trace.record(
            "llm_request",
            {
                "node": "answer",
                "model": getattr(llm, "model", ""),
                "messages": messages_to_dict(messages),
                "tools": [],
            },
        )
        response = await llm.generate(messages)
        trace.record(
            "llm_response",
            {"node": "answer", **llm_response_to_dict(response)},
        )
        return {
            "final_answer": response.content,
            "tokens_used": state.get("tokens_used", 0) + response.tokens_used,
            "model": response.model,
        }

    def should_use_tool(state: GraphState) -> str:
        plan = state.get("plan") or {}
        return "tool" if plan.get("need_tool") else "answer"

    graph = StateGraph(GraphState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("plan", plan_node)
    graph.add_node("tool", tool_node)
    graph.add_node("answer", answer_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "plan")
    graph.add_conditional_edges(
        "plan",
        should_use_tool,
        {"tool": "tool", "answer": "answer"},
    )
    graph.add_edge("tool", "answer")
    graph.add_edge("answer", END)

    compiled = graph.compile()
    try:
        final_state = await compiled.ainvoke({
            "user_input": question,
            "session_id": session_id,
        })

        response = AgentResponse(
            content=final_state.get("final_answer", ""),
            sources=final_state.get("sources", []),
            tokens_used=final_state.get("tokens_used", 0),
            model=final_state.get("model", ""),
            session_id=session_id,
            metadata={"trace_id": trace.trace_id},
        )
        trace.finish(response=response.to_dict())
        return response
    except Exception as e:
        trace.fail(e)
        raise


async def main():
    response = await run_graph("请说明这个模板的 RAG 多路召回是什么，并计算 12 * 23")
    print(response.content)
    print("\n来源:")
    for source in response.sources:
        print(source)


if __name__ == "__main__":
    asyncio.run(main())
