# -*- coding: utf-8 -*-
"""
任务型 Agent

流程（对应 README 图）：
  user input
    -> plan: LLM 判断需要哪些工具
    -> 无工具 → AgentBase.chat()（RAG + Memory + LLM）
    -> 有工具 → RAG + Memory + react_engine CodeAgent（筛选后的工具）
    -> response
"""
from __future__ import annotations

import asyncio
import inspect
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
from react_engine import Tool as SmolToolBase
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
#  react_engine 适配层
# ═══════════════════════════════════════════════════════════════

class _LLMAdapter:
    """把 Agent Template 的 LLMBase 包装为 react_engine Model 接口。"""

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
        """react_engine 调用入口 → 转发到 LLMBase.generate()。"""
        from react_engine import ChatMessage as SmolChatMessage

        chat_messages = []
        for m in messages:
            if isinstance(m, dict):
                role, content = m.get("role", "user"), m.get("content", "")
            else:
                role = getattr(m, "role", "user")
                content = getattr(m, "content", "")
            chat_messages.append(ChatMessage(role=role, content=content))

        # react_engine 通过 prompt 传递工具描述，不走 function calling
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
        super().__init__()
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
                "nullable": pname not in required,
            }
            self.inputs[pname] = entry

    def forward(self, **kwargs) -> str:
        try:
            result = asyncio.run(self._wrapped.safe_execute(**kwargs))
        except Exception as e:
            return f"[工具执行失败] {self.name}: {e}"
        return result.to_str()

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _SkillSectionTool(SmolToolBase):
    """Level 2 工具：让 CodeAgent 按需加载 SKILL.md 的段落。"""
    skip_forward_signature_validation = True

    def __init__(self, loader, skill_name: str):
        super().__init__()
        self._loader = loader
        self._skill_name = skill_name
        self.name = f"read_skill_section"
        headings = loader.get_section_headings(skill_name)
        headings_text = ", ".join(headings) if headings else "(无段落)"
        self.description = (
            f"读取 skill '{skill_name}' 的 SKILL.md 指令段落。"
            f"可用段落: {headings_text}\n"
            f"参数: heading（段落标题关键词，如 'Phase 1'、'调用方式'）"
        )
        self.output_type = "string"
        self.inputs = {
            "heading": {
                "type": "string",
                "description": f"段落标题关键词，可用: {headings_text}",
            }
        }

    def forward(self, heading: str = "", **kwargs) -> str:
        kw = heading or kwargs.get("section", "")
        if not kw:
            headings = self._loader.get_section_headings(self._skill_name)
            return f"[错误] 未指定段落。可用段落: {headings}"
        content = self._loader.load_section(self._skill_name, kw)
        if content is None:
            headings = self._loader.get_section_headings(self._skill_name)
            return f"[错误] 未找到段落 '{kw}'。可用段落: {headings}"
        return content

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _SkillResourceTool(SmolToolBase):
    """Level 3 工具：让 CodeAgent 按需加载 skill 目录下的资源文件。"""
    skip_forward_signature_validation = True

    def __init__(self, loader, skill_name: str):
        super().__init__()
        self._loader = loader
        self._skill_name = skill_name
        self.name = f"read_skill_resource"
        resources = loader.list_resources(skill_name)
        if resources:
            res_list = ", ".join(resources)
            desc_suffix = f"\n可用资源: {res_list}"
        else:
            desc_suffix = "\n该 skill 无额外资源文件。"
        self.description = (
            f"读取 skill '{skill_name}' 的资源文件。"
            f"参数: relative_path（相对路径）"
            f"{desc_suffix}"
        )
        self.output_type = "string"
        self.inputs = {
            "relative_path": {
                "type": "string",
                "description": f"资源文件路径。{desc_suffix}",
            }
        }

    def forward(self, relative_path: str = "", **kwargs) -> str:
        path = relative_path or kwargs.get("file_path", "")
        if not path:
            return "[错误] 未指定资源路径"
        content = self._loader.load_resource(self._skill_name, path)
        if content is None:
            available = self._loader.list_resources(self._skill_name)
            return f"[错误] 资源不存在: {path}\n可用资源: {available}"
        return content

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _SkillFuncTool(SmolToolBase):
    """通用 skill 函数包装器：把 skill 的 Python 函数暴露为 CodeAgent 工具。"""
    skip_forward_signature_validation = True

    def __init__(self, func, module_path: str):
        super().__init__()
        self._func = func
        self._module_path = module_path
        self.name = func.__name__
        self.output_type = "string"

        # 从函数 docstring 提取描述和参数说明
        doc = func.__doc__ or ""
        doc_lines = doc.strip().split("\n")
        func_desc = doc_lines[0][:200] if doc_lines[0] else f"调用 {func.__name__}()"

        # 从 docstring 提取参数描述（格式: "param: 说明" 或 "param – 说明"）
        param_docs = {}
        for line in doc_lines:
            line = line.strip()
            for sep in [":", " – "]:
                if sep in line:
                    key, val = line.split(sep, 1)
                    key = key.strip()
                    if key and not key.startswith(" ") and " " not in key:
                        param_docs[key] = val.strip()[:100]
                    break

        # 从函数签名推断 inputs
        self.inputs = {}
        param_parts = []
        try:
            sig = inspect.signature(func)
            for pname, param in sig.parameters.items():
                # 类型映射
                type_map = {dict: "object", list: "array", str: "string", int: "integer", float: "number", bool: "boolean"}
                ptype = "string"
                if param.annotation != inspect.Parameter.empty:
                    ptype = type_map.get(param.annotation, "string")

                desc = param_docs.get(pname, "")
                has_default = param.default != inspect.Parameter.empty
                entry = {"type": ptype, "description": desc, "nullable": has_default}

                if has_default:
                    param_parts.append(f"{pname}={param.default}")
                else:
                    param_parts.append(pname)

                self.inputs[pname] = entry
        except Exception:
            pass

        # 工具描述：函数签名 + 一句话说明
        params_str = ", ".join(param_parts)
        self.description = f"{func.__name__}({params_str}) — {func_desc}"

    def forward(self, **kwargs):
        try:
            return self._func(**kwargs)
        except Exception as e:
            return {"error": f"{self.name} 执行失败: {e}"}

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


def _load_skill_functions(skill_name: str) -> list:
    """加载 skill 的 run.py 中的公开函数，包装为 CodeAgent 工具。"""
    module_name = skill_name.replace("-", "_")
    try:
        import importlib
        mod = importlib.import_module(f"skills.{module_name}.run")
    except Exception:
        return []

    tools = []
    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        func = getattr(mod, attr_name)
        if not callable(func) or inspect.isclass(func):
            continue
        # 只保留模块内定义的函数，跳过 import 进来的名字（如 Dict, List, Optional）
        if getattr(func, "__module__", "") != mod.__name__:
            continue
        try:
            tools.append(_SkillFuncTool(func, f"skills.{module_name}.run"))
        except Exception:
            pass
    return tools


def _build_smol_tools(tool_registry: ToolRegistry, selected_names: List[str]) -> list:
    """将选中的 Tool 实例转换为 react_engine 兼容工具列表。"""
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
    任务型 Agent — plan + react_engine CodeAgent

    1. plan 阶段：LLM 根据用户意图筛选需要的工具
    2. 无工具 → 委托 AgentBase.chat()（完整 RAG + Memory 链路）
    3. 有工具 → RAG + Memory + react_engine CodeAgent 执行
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

    def _get_skill_loader(self):
        """返回 skill_adapter（原 SkillLoader 功能已合入 QDSkillAdapter）。"""
        return self.skill_adapter

    # ── plan: 工具筛选 ────────────────────────────────────────

    async def _plan(
        self,
        user_input: str,
        llm: LLMBase,
        trace: AgentTraceRecorder,
    ) -> tuple:
        """Plan 节点：让 LLM 根据用户意图选择需要的工具，并扩展用户消息。

        Returns:
            (selected_tools, expanded_query) 元组
        """
        if not self.tool_registry or len(self.tool_registry) == 0:
            return [], user_input

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

        expanded_query = plan.get("expanded_query", user_input) or user_input
        logger.info("[TaskAgent] plan: 选了 %d 项 %s, 原因: %s",
                     len(valid), valid, plan.get("reason", ""))
        trace.record("plan_result", {
            "selected_tools": valid,
            "expanded_query": expanded_query,
            "reason": plan.get("reason", ""),
        })

        return valid, expanded_query

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
            selected_tools, expanded_query = await self._plan(user_input, self.llm, trace)

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

            # 不加载原始历史，plan 已将上下文信息扩展到 expanded_query 中
            # 构建 task prompt
            task_parts = []
            if context:
                task_parts.append(f"【参考资料】\n{context}")

            # 加载计划选中的技能：SKILL.md 渐进式披露（Anthropic 标准）
            # SKILL.md body（≤500 词）全量注入，超过则只注入段落目录
            skill_names = [t[6:] for t in selected_tools if t.startswith("skill:")]
            if skill_names and self.skill_adapter:
                loader = self._get_skill_loader()
                for sname in skill_names:
                    try:
                        body = loader.load_body(sname)
                        if body:
                            # 超过 500 词只注入目录，agent 用 read_skill_section 按需加载
                            if len(body.split()) > 500:
                                headings = loader.get_section_headings(sname)
                                catalog = "\n".join(f"  - {h}" for h in headings)
                                task_parts.append(
                                    f"【技能: {sname}】\n"
                                    f"使用 read_skill_section 工具按需加载指令段落。\n"
                                    f"可用段落:\n{catalog}"
                                )
                                logger.info("[TaskAgent] SKILL.md 过长(%d 词)，注入段落目录", len(body.split()))
                            else:
                                task_parts.append(f"【技能指令: {sname}】\n{body}")
                                logger.info("[TaskAgent] 注入 SKILL.md body: %s (%d 字符)", sname, len(body))
                            trace.record("skill_loaded", {"skill": sname, "body_len": len(body)})
                        else:
                            logger.warning("[TaskAgent] skill %s 无 SKILL.md body", sname)
                    except Exception as e:
                        logger.warning("[TaskAgent] skill %s 加载失败: %s", sname, e)

            task_parts.append(f"【任务】\n{expanded_query}")
            task = "\n\n".join(task_parts)

            # 构建工具（plan 筛选的工具 + skill 资源读取工具）
            tool_names = [t for t in selected_tools if not t.startswith("skill:")]
            smol_tools = _build_smol_tools(self.tool_registry, tool_names)
            # skill 函数包装为 CodeAgent 工具（agent 按 SKILL.md 指令调用）
            if skill_names:
                loader = self._get_skill_loader()
                for sname in skill_names:
                    # skill 的 Python 函数 → 工具
                    func_tools = _load_skill_functions(sname)
                    smol_tools.extend(func_tools)
                    logger.info("[TaskAgent] skill %s 注入函数工具: %s", sname, [t.name for t in func_tools])
                    # Level 2: 段落读取
                    smol_tools.append(_SkillSectionTool(loader, sname))
                    # Level 3: 资源读取
                    smol_tools.append(_SkillResourceTool(loader, sname))
            logger.info("[TaskAgent] 工具: %s", [t.name for t in smol_tools])

            trace.record("react_engine_setup", {"tools": [t.name for t in smol_tools]})

            # CodeAgent 执行
            model = _LLMAdapter(self.llm)
            from react_engine import CodeAgent as SmolCodeAgent

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
                "react_engine_done",
                {"elapsed_seconds": react_elapsed, "result_preview": str(result)[:200]},
            )

            # ── 使用 CodeAgent 原始输出 ──
            result_raw = str(result)

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
                content=result_raw,
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
