# -*- coding: utf-8 -*-
"""
任务型 Agent — MCP + Plan

流程：
  user input
    -> 负面反馈检测
    -> plan: LLM 决定用哪些技能
    -> MCP 加载全部工具 + 技能加载
    -> CodeAgent 执行
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
from smolagents import Tool as SmolToolBase
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

logger = logging.getLogger(__name__)

# ── 模块级 TraceCollector 存储 ──
_collectors: Dict[str, "TraceCollector"] = {}

# ── MCP 配置 ──────────────────────────────────────────────────
_MCP_SERVER_CMD = os.getenv("AGENT_MCP_SERVER_CMD", "python")
_MCP_SERVER_ARGS = os.getenv("AGENT_MCP_SERVER_ARGS", "tools/mcp_bridge.py").split()

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
    """把 LLMBase 包装为 smolagents Model 接口。"""

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
        from smolagents import ChatMessage as SmolChatMessage

        chat_messages = []
        for m in messages:
            if isinstance(m, dict):
                role, content = m.get("role", "user"), m.get("content", "")
            else:
                role = getattr(m, "role", "user")
                content = getattr(m, "content", "")
            chat_messages.append(ChatMessage(role=role, content=content))

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


class _SkillSectionTool(SmolToolBase):
    """让 CodeAgent 按需加载 SKILL.md 的段落。"""
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
            f"参数: heading（段落标题关键词）"
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
    """让 CodeAgent 按需加载 skill 目录下的资源文件。"""
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
    """把 skill 的 Python 函数暴露为 CodeAgent 工具。"""
    skip_forward_signature_validation = True

    def __init__(self, func, module_path: str):
        super().__init__()
        self._func = func
        self._module_path = module_path
        self.name = func.__name__
        self.output_type = "string"

        doc = func.__doc__ or ""
        doc_lines = doc.strip().split("\n")
        func_desc = doc_lines[0][:200] if doc_lines[0] else f"调用 {func.__name__}()"

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

        self.inputs = {}
        param_parts = []
        try:
            sig = inspect.signature(func)
            for pname, param in sig.parameters.items():
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
        if getattr(func, "__module__", "") != mod.__name__:
            continue
        try:
            tools.append(_SkillFuncTool(func, f"skills.{module_name}.run"))
        except Exception:
            pass
    return tools


# ═══════════════════════════════════════════════════════════════
#  MCP 常驻连接（单例，首次调用启动，之后复用）
# ═══════════════════════════════════════════════════════════════

class _MCPSingleton:
    """MCP 连接单例 — 常驻子进程，避免每次请求重复启动。"""

    def __init__(self):
        self._tools: list | None = None
        self._ctx = None  # ToolCollection 上下文管理器
        self._collection = None

    def _ensure(self) -> bool:
        if self._tools is not None:
            return True
        try:
            from mcp import StdioServerParameters
            from smolagents import ToolCollection

            server = StdioServerParameters(
                command=_MCP_SERVER_CMD,
                args=_MCP_SERVER_ARGS,
            )
            self._ctx = ToolCollection.from_mcp(server, trust_remote_code=True)
            self._collection = self._ctx.__enter__()
            self._tools = list(self._collection.tools) if hasattr(self._collection, 'tools') else list(self._collection)
            logger.info("[MCP] 常驻连接已建立，%d 个工具", len(self._tools))
            return True
        except ImportError:
            logger.warning("[TaskAgent] MCP 依赖未安装 (pip install mcp)")
            return False
        except Exception as e:
            logger.warning("[TaskAgent] MCP 初始化失败: %s", e)
            return False

    @property
    def tools(self) -> list:
        self._ensure()
        return self._tools or []

    @property
    def available(self) -> bool:
        return self._ensure()

    def close(self):
        if self._ctx:
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:
                pass
            self._ctx = None
            self._collection = None
            self._tools = None
            logger.info("[MCP] 连接已关闭")


_mcp = _MCPSingleton()


# ═══════════════════════════════════════════════════════════════
#  TaskAgent
# ═══════════════════════════════════════════════════════════════

class TaskAgent(AgentBase):
    """
    任务型 Agent — MCP + Plan

    1. plan: LLM 决定用哪些技能
    2. MCP 加载全部工具，技能按需加载
    3. CodeAgent 执行
    """

    def __init__(
        self,
        llm: LLMBase,
        memory: Optional[MemoryBase] = None,
        retriever: Optional[Retriever] = None,
        system_prompt: str = "你是一个智能助手，可以使用工具来完成任务。",
        memory_window_size: int = 10,
        max_tool_rounds: int = 10,
        skill_adapter=None,
    ):
        super().__init__(
            llm=llm,
            memory=memory,
            retriever=retriever,
            system_prompt=system_prompt,
            memory_window_size=memory_window_size,
        )
        self.max_tool_rounds = max_tool_rounds
        self.skill_adapter = skill_adapter

    def _get_skill_loader(self):
        return self.skill_adapter

    # ── plan: 决定用哪些技能 ──────────────────────────────────

    async def _plan(
        self,
        user_input: str,
        llm: LLMBase,
        trace: AgentTraceRecorder,
        mcp_tools: list | None = None,
    ) -> tuple:
        """Plan 节点：判断是否需要工具，选择需要的技能。

        Args:
            mcp_tools: MCP 工具列表

        Returns:
            (selected_skills, expanded_query, need_tools) 三元组。
            need_tools=false → 纯对话
            need_tools=true, skills=[] → 走 MCP，CodeAgent 自选工具
            need_tools=true, skills=[...] → 走 MCP + 技能
        """
        # 构建工具描述（供 LLM 判断是否需要工具能力）
        tools_desc = []
        if mcp_tools:
            for tool in mcp_tools:
                desc = getattr(tool, 'description', '') or ''
                tools_desc.append(f"- {tool.name}: {desc[:80]}")
        tools_text = "\n".join(tools_desc) if tools_desc else "(无可用工具)"

        # 构建技能描述
        skills_desc = []
        if self.skill_adapter:
            for s in self.skill_adapter.list_skills():
                skills_desc.append(f"- {s['name']}: {s.get('description', '')[:150]}")
        skills_text = "\n".join(skills_desc) if skills_desc else "(无可用技能)"

        template = _load_plan_template()
        prompt = template.format(tools_text=tools_text, skills_text=skills_text, user_input=user_input)

        messages = [
            ChatMessage(role="system", content="你是任务规划器。只输出 JSON。"),
            ChatMessage(role="user", content=prompt),
        ]

        trace.record("plan_request", {
            "model": getattr(llm, "model", ""),
            "tools_available": [t.name for t in mcp_tools] if mcp_tools else [],
            "skills_available": [s["name"] for s in self.skill_adapter.list_skills()] if self.skill_adapter else [],
        })

        plan_start = time.time()
        response = await llm.generate(messages=messages)
        trace.record("plan_response", {
            "elapsed_seconds": round(time.time() - plan_start, 3),
            **llm_response_to_dict(response),
        })

        text = response.content.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        plan = safe_parse_json(text, default={"skills": []})

        # 路由判断：need_tools=false → 纯对话
        need_tools = plan.get("need_tools", True)
        if not need_tools:
            logger.info("[TaskAgent] plan: 不需要工具，纯对话")
            trace.record("plan_result", {"need_tools": False, "route": "chat"})
            return [], user_input, False

        selected = plan.get("skills", plan.get("tools", []))  # 兼容旧格式
        all_skill_names = {s["name"] for s in self.skill_adapter.list_skills()} if self.skill_adapter else set()
        selected_skills = []
        for name in selected:
            if name.startswith("skill:"):
                name = name[6:]
            if name in all_skill_names:
                selected_skills.append(name)

        expanded_query = plan.get("expanded_query", user_input) or user_input
        logger.info("[TaskAgent] plan: need_tools=%s, 选了 %d 个技能 %s",
                     need_tools, len(selected_skills), selected_skills)
        trace.record("plan_result", {
            "need_tools": True,
            "route": "mcp+plan",
            "selected_skills": selected_skills,
        })

        return selected_skills, expanded_query, True

    # ── 主对话入口 ────────────────────────────────────────────

    async def chat(
        self,
        user_input: str,
        session_id: str = "default",
        use_rag: bool = True,
    ) -> AgentResponse:
        # 负面反馈检测
        try:
            from feedback import check_negative_feedback
            check_negative_feedback(user_input)
        except Exception:
            pass
        return await self._chat_plan(user_input, session_id, use_rag)

    # ── MCP + Plan 执行 ──────────────────────────────────────

    async def _chat_plan(
        self,
        user_input: str,
        session_id: str,
        use_rag: bool,
    ) -> AgentResponse:
        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=type(self).__name__,
            session_id=session_id,
            user_input=user_input,
            metadata={"mode": "mcp+plan", "use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            # 1. MCP 常驻连接（首次启动，之后复用）
            from smolagents import CodeAgent as SmolCodeAgent
            from pathlib import Path
            import yaml

            if not _mcp.available:
                logger.error("[TaskAgent] MCP 不可用")
                return await super().chat(user_input, session_id=session_id, use_rag=use_rag)

            mcp_tool_list = _mcp.tools
            model = _LLMAdapter(self.llm)

            # 2. Plan 选技能（MCP 工具列表传入，LLM 看到完整工具集）
            selected_skills, expanded_query, need_tools = await self._plan(
                user_input, self.llm, trace, mcp_tools=mcp_tool_list,
            )

            # need_tools=false 且无技能 → 纯对话
            if not need_tools and not selected_skills:
                trace.record("delegate_chat", {"reason": "plan: 不需要工具"})
                trace.finish(response={"delegated_to": "AgentBase.chat"})
                return await super().chat(user_input, session_id=session_id, use_rag=use_rag)

            # 3. RAG 检索
            sources = []
            context = ""
            if use_rag and self.retriever:
                rag_start = time.time()
                docs = await self.retriever.retrieve(user_input)
                trace.record("rag_retrieve", {
                    "elapsed_seconds": round(time.time() - rag_start, 3),
                    "doc_count": len(docs),
                })
                if docs:
                    context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]

            # 4. 构建 task prompt（技能指令）
            task_parts = []
            if context:
                task_parts.append(f"【参考资料】\n{context}")

            loader = self._get_skill_loader()
            for sname in selected_skills:
                try:
                    body = loader.load_body(sname)
                    if body:
                        if len(body.split()) > 500:
                            headings = loader.get_section_headings(sname)
                            catalog = "\n".join(f"  - {h}" for h in headings)
                            task_parts.append(
                                f"【技能: {sname}】\n"
                                f"使用 read_skill_section 工具按需加载指令段落。\n"
                                f"可用段落:\n{catalog}"
                            )
                        else:
                            task_parts.append(f"【技能指令: {sname}】\n{body}")
                        trace.record("skill_loaded", {"skill": sname, "body_len": len(body)})
                except Exception as e:
                    logger.warning("[TaskAgent] skill %s 加载失败: %s", sname, e)

            task_parts.append(f"【任务】\n{expanded_query}")
            task = "\n\n".join(task_parts)

            # 5. 加载技能工具（函数 + 渐进式加载）
            skill_tools = []
            for sname in selected_skills:
                func_tools = _load_skill_functions(sname)
                skill_tools.extend(func_tools)
                skill_tools.append(_SkillSectionTool(loader, sname))
                skill_tools.append(_SkillResourceTool(loader, sname))
                logger.info("[TaskAgent] skill %s 注入函数工具: %s", sname, [t.name for t in func_tools])

            # 6. 创建 TraceCollector
            collector = None
            try:
                from trace_collector import TraceCollector
                collector = TraceCollector(session_id=session_id, user_query=user_input)
                for sname in selected_skills:
                    collector.begin_skill(sname)
                _collectors[session_id] = collector
            except Exception as e:
                logger.debug("[Trace] TraceCollector 创建失败: %s", e)

            # 7. CodeAgent 执行
            all_tools = mcp_tool_list + skill_tools
            logger.info("[TaskAgent] MCP %d 工具 + %d 技能工具", len(mcp_tool_list), len(skill_tools))
            trace.record("code_agent_setup", {
                "mcp_tools": len(mcp_tool_list),
                "skill_tools": len(skill_tools),
                "tools": [t.name for t in skill_tools],
            })

            agent = SmolCodeAgent(
                tools=all_tools,
                model=model,
                max_steps=self.max_tool_rounds,
            )

            # 追加格式规则
            _rule_path = Path(__file__).resolve().parent.parent / "prompts" / "format_rules.yaml"
            try:
                format_rules = yaml.safe_load(_rule_path.read_text(encoding="utf-8"))
                agent.prompt_templates["system_prompt"] += "\n" + format_rules["system_prompt_suffix"]
            except Exception:
                pass

            react_start = time.time()
            result = agent.run(task)
            react_elapsed = round(time.time() - react_start, 2)
            trace.record("code_agent_done", {"elapsed_seconds": react_elapsed, "result_preview": str(result)[:200]})

            # 8. TraceCollector 存库
            if collector:
                try:
                    collector.end_skill()
                    collector.on_agent_finish(
                        final_answer=str(result),
                        total_steps=1,
                        total_tokens=0,
                        model=getattr(self.llm, "model", "unknown"),
                    )
                    collector.flush()
                except Exception as e:
                    logger.warning("[Trace] 存库失败: %s", e)
                finally:
                    _collectors.pop(session_id, None)

            result_raw = str(result)

            # 9. 保存对话历史
            if self.memory:
                await self.memory.add(session_id, "user", user_input)
                try:
                    full_messages = agent.write_memory_to_messages(summary_mode=True)
                    steps_text = [m.content for m in full_messages if getattr(m, "role", "") == "assistant" and m.content]
                    await self.memory.add(session_id, "assistant", "\n".join(steps_text) if steps_text else result_raw)
                except Exception:
                    await self.memory.add(session_id, "assistant", result_raw)

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
            _collectors.pop(session_id, None)
            raise
