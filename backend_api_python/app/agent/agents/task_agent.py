# -*- coding: utf-8 -*-
"""
任务型 Agent

流程（对应 README 图）：
  user input
    -> plan: LLM 判断需要哪些工具
    -> 无工具 → AgentBase.chat()（RAG + Memory + LLM）
    -> 有工具 → RAG + Memory + smolagents CodeAgent（筛选后的工具）
    -> response
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import time
from typing import Dict, List, Optional

import nest_asyncio
nest_asyncio.apply()

from agents.base import AgentBase, AgentResponse
from llm.base import ChatMessage, LLMBase
from memory.base import MemoryBase
from rag.retriever import Retriever
from smolagents import Tool as SmolToolBase
from tools.registry import ToolRegistry
from tools.base import Tool
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

logger = logging.getLogger(__name__)

# Plan 提示词模板
_PLAN_TEMPLATE: str | None = None
_PLAN_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "prompts", "plan_system.txt"
)


def _load_plan_template() -> str:
    global _PLAN_TEMPLATE
    if _PLAN_TEMPLATE is None:
        with open(_PLAN_TEMPLATE_PATH, encoding="utf-8") as f:
            _PLAN_TEMPLATE = f.read()
    return _PLAN_TEMPLATE


# ═══════════════════════════════════════════════════════════════
#  smolagents 适配层
# ═══════════════════════════════════════════════════════════════

class _LLMAdapter:
    """把 Agent Template 的 LLMBase 包装为 smolagents Model 接口。"""

    def __init__(self, llm: LLMBase):
        self._llm = llm
        self.model_id = getattr(llm, "model", "unknown")

    def generate(
        self,
        messages: list,
        stop_sequences: list | None = None,
        response_format: dict | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ):
        """smolagents 调用入口 → 转发到 LLMBase.generate()。"""
        from smolagents.models import ChatMessage as SmolChatMessage

        chat_messages = []
        for m in messages:
            if isinstance(m, dict):
                role, content = m.get("role", "user"), m.get("content", "")
            else:
                role = getattr(m, "role", "user")
                content = getattr(m, "content", "")
            chat_messages.append(ChatMessage(role=role, content=content))

        # smolagents 通过 prompt 传递工具描述，不走 function calling
        try:
            resp = asyncio.run(self._llm.generate(chat_messages))
        except Exception as e:
            logger.error("[TaskAgent] LLM 调用失败: %s", e)
            raise

        smol_msg = SmolChatMessage(role="assistant", content=resp.content or "")

        class _TokenUsage:
            def __init__(self, r):
                self.input_tokens = r.prompt_tokens if r else 0
                self.output_tokens = r.completion_tokens if r else 0
                self.total_tokens = r.tokens_used if r else 0

        smol_msg.token_usage = _TokenUsage(resp)
        return smol_msg


class _SmolTool(SmolToolBase):
    """把 Agent Template 的 Tool 包装为 smolagents Tool 子类。"""
    skip_forward_signature_validation = True

    def __init__(self, wrapped: Tool):
        self._wrapped = wrapped
        self.name = wrapped.name
        self.description = wrapped.description
        self.output_type = "string"

        props = wrapped.parameters.get("properties", {})
        required = wrapped.parameters.get("required", [])
        self.inputs = {}
        for pname, pdef in props.items():
            entry = {
                "type": pdef.get("type", "string"),
                "description": pdef.get("description", ""),
            }
            if pname not in required:
                entry["nullable"] = True
            self.inputs[pname] = entry

    def forward(self, **kwargs) -> str:
        try:
            result = asyncio.run(self._wrapped.safe_execute(**kwargs))
        except Exception as e:
            return f"[工具执行失败] {self.name}: {e}"
        return result.to_str()

    def __call__(self, *args, **kwargs):
        return self.forward(**kwargs)


def _build_smol_tools(tool_registry: ToolRegistry, selected_names: List[str]) -> list:
    """将选中的 Tool 实例转换为 smolagents 兼容工具列表。"""
    tools = []
    for name in selected_names:
        tool = tool_registry.get(name)
        if tool:
            try:
                tools.append(_SmolTool(tool))
            except Exception as e:
                logger.warning("[TaskAgent] 包装 %s 失败: %s", name, e, exc_info=True)
    return tools

# ═══════════════════════════════════════════════════════════════
#  TaskAgent
# ═══════════════════════════════════════════════════════════════

class TaskAgent(AgentBase):
    """
    任务型 Agent — plan + smolagents CodeAgent

    1. plan 阶段：LLM 根据用户意图筛选需要的工具
    2. 无工具 → 委托 AgentBase.chat()（完整 RAG + Memory 链路）
    3. 有工具 → RAG + Memory + smolagents CodeAgent 执行
    """

    def __init__(
        self,
        llm: LLMBase,
        memory: Optional[MemoryBase] = None,
        retriever: Optional[Retriever] = None,
        tool_registry: Optional[ToolRegistry] = None,
        system_prompt: str = "你是一个智能助手，可以使用工具来完成任务。",
        memory_window_size: int = 10,
        max_tool_rounds: int = 10,
        skill_adapter=None,
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
        self.skill_adapter = skill_adapter

    # ── plan: 工具筛选 ────────────────────────────────────────

    async def _plan(
        self,
        user_input: str,
        llm: LLMBase,
        trace: AgentTraceRecorder,
    ) -> List[str]:
        """Plan 节点：让 LLM 根据用户意图选择需要的工具。"""
        if not self.tool_registry or len(self.tool_registry) == 0:
            return []

        all_names = self.tool_registry.list_tools()
        tools_desc = []
        for name in all_names:
            tool = self.tool_registry.get(name)
            if tool:
                tools_desc.append(f"- {name}: {tool.description[:100]}")

        if self.skill_adapter:
            for s in self.skill_adapter.list_skills():
                tools_desc.append(f"- skill:{s['name']}: {s.get('description', '')[:100]}")

        tools_text = "\n".join(tools_desc)

        template = _load_plan_template()
        prompt = template.format(tools_text=tools_text, user_input=user_input)

        messages = [
            ChatMessage(role="system", content="你是任务规划器。只输出 JSON。"),
            ChatMessage(role="user", content=prompt),
        ]

        trace.record(
            "plan_request",
            {"model": getattr(llm, "model", ""), "tools_available": all_names},
        )

        plan_start = time.time()
        response = await llm.generate(messages=messages)
        trace.record(
            "plan_response",
            {
                "elapsed_seconds": round(time.time() - plan_start, 3),
                **llm_response_to_dict(response),
            },
        )

        text = response.content.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        plan = safe_parse_json(text, default={"tools": []})
        selected = plan.get("tools", [])

        all_tool_names = set(self.tool_registry.list_tools()) if self.tool_registry else set()
        all_skill_names = {s["name"] for s in self.skill_adapter.list_skills()} if self.skill_adapter else set()

        valid = []
        for t in selected:
            if t in all_tool_names:
                valid.append(t)
            elif t.startswith("skill:") and t[6:] in all_skill_names:
                valid.append(t)
            elif t in all_skill_names:
                valid.append(f"skill:{t}")

        if len(valid) != len(selected):
            unknown = set(selected) - set(valid)
            logger.warning("[TaskAgent] plan 选了不存在的工具/技能: %s", unknown)

        logger.info("[TaskAgent] plan: 选了 %d 项 %s, 原因: %s",
                     len(valid), valid, plan.get("reason", ""))
        trace.record("plan_result", {"selected_tools": valid, "reason": plan.get("reason", "")})

        return valid

    # ── 主对话入口 ────────────────────────────────────────────

    async def chat(
        self,
        user_input: str,
        session_id: str = "default",
        use_rag: bool = True,
    ) -> AgentResponse:
        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=type(self).__name__,
            session_id=session_id,
            user_input=user_input,
            metadata={"use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            selected_tools = await self._plan(user_input, self.llm, trace)

            if not selected_tools:
                trace.record("delegate_chat", {"reason": "plan: 无需工具"})
                trace.finish(response={"delegated_to": "AgentBase.chat"})
                return await super().chat(user_input, session_id=session_id, use_rag=use_rag)

            # RAG 检索
            sources = []
            context = ""
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
                    sources = [
                        {"content": d["content"][:200], "score": d.get("score", 0)}
                        for d in docs
                    ]

            # 加载对话历史（截断长消息，避免 prompt 过长）
            history_text = ""
            if self.memory:
                history = await self.memory.get_history(session_id, limit=self.memory_window_size)
                trace.record(
                    "memory_load",
                    {"history_count": len(history), "limit": self.memory_window_size},
                )
                if history:
                    lines = []
                    for msg in history:
                        lines.append(f"{msg.role}: {msg.content}")
                    history_text = "\n".join(lines)

            # 构建 task prompt
            task_parts = []
            if context:
                task_parts.append(f"【参考资料】\n{context}")
            if history_text:
                task_parts.append(f"【对话历史】\n{history_text}")

            # 加载计划选中的技能：先执行 skill，结果 + SKILL.md 一起注入
            skill_names = [t[6:] for t in selected_tools if t.startswith("skill:")]
            if skill_names and self.skill_adapter:
                for sname in skill_names:
                    # ① 执行 skill，获取结构化结果
                    skill_result_text = ""
                    try:
                        skill_info = self.skill_adapter.get(sname)
                        if skill_info:
                            # 优先尝试执行 run.py（无论 skill_type）
                            # skill 名可能带连字符（market-screener），模块名用下划线
                            module_name = sname.replace("-", "_")
                            mod = importlib.import_module(f"skills.{module_name}.run")
                            if hasattr(mod, "run"):
                                raw = mod.run()
                                if isinstance(raw, dict):
                                    skill_result_text = _format_skill_result(raw)
                                else:
                                    skill_result_text = str(raw)
                                logger.info("[TaskAgent] skill %s 执行成功，结果 %d 字符", sname, len(skill_result_text))
                                trace.record("skill_executed", {"skill": sname, "result_len": len(skill_result_text)})
                    except Exception as e:
                        logger.warning("[TaskAgent] skill %s 执行失败: %s", sname, e)
                        skill_result_text = f"(skill 执行失败: {e})"

                    # ② 注入执行结果（SKILL.md 执行指令不再注入，已外部执行）
                    if skill_result_text:
                        task_parts.append(f"【技能执行结果: {sname}】\n{skill_result_text}")
                        logger.info("[TaskAgent] 注入 skill 结果: %s", sname)

            task_parts.append(f"【任务】\n{user_input}")
            task = "\n\n".join(task_parts)

            # 构建工具（只用 plan 筛选的工具，技能不注入 CodeAgent）
            tool_names = [t for t in selected_tools if not t.startswith("skill:")]
            smol_tools = _build_smol_tools(self.tool_registry, tool_names)
            logger.info("[TaskAgent] 工具: %s", [t.name for t in smol_tools])

            trace.record("smolagents_setup", {"tools": [t.name for t in smol_tools]})

            # CodeAgent 执行
            model = _LLMAdapter(self.llm)
            from smolagents import CodeAgent as SmolCodeAgent

            agent = SmolCodeAgent(
                tools=smol_tools,
                model=model,
                max_steps=self.max_tool_rounds,
            )

            logger.info("[TaskAgent] CodeAgent 执行: %s (工具: %s)",
                        user_input[:60], [t.name for t in smol_tools])

            react_start = time.time()
            result = agent.run(task)
            react_elapsed = round(time.time() - react_start, 2)

            trace.record(
                "smolagents_done",
                {"elapsed_seconds": react_elapsed, "result_preview": str(result)[:200]},
            )

            # 保存对话历史（含 CodeAgent 中间步骤）
            if self.memory:
                await self.memory.add(session_id, "user", user_input)

                # 从 CodeAgent memory 提取完整步骤
                try:
                    full_messages = agent.write_memory_to_messages(summary_mode=True)
                    steps_text = []
                    for msg in full_messages:
                        role = getattr(msg, "role", "")
                        content = getattr(msg, "content", "")
                        if role == "assistant" and content:
                            steps_text.append(content)
                    if steps_text:
                        await self.memory.add(session_id, "assistant", "\n".join(steps_text))
                    else:
                        await self.memory.add(session_id, "assistant", str(result))
                except Exception:
                    await self.memory.add(session_id, "assistant", str(result))

                trace.record("memory_save", {"messages_saved": 2})

            elapsed = round(time.time() - start_time, 2)

            response = AgentResponse(
                content=str(result),
                sources=sources,
                session_id=session_id,
                elapsed_seconds=elapsed,
                metadata={"trace_id": trace.trace_id},
            )
            trace.finish(response=response.to_dict())
            return response

        except Exception as e:
            trace.fail(e)
            raise
