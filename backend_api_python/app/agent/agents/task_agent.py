# -*- coding: utf-8 -*-
"""
任务型 Agent — Plan 写流程，Executor 按流程执行

流程：
  user input
    -> plan: LLM 生成 SKILL.md 格式的工作流文档
    -> executor: 加载技能工具 + 指定工具 → CodeAgent 按 SKILL.md 执行
    -> response

Skill 是预制的 SKILL.md，plan 生成的是临时 SKILL.md。统一概念，统一执行方式。
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
from tools.registry import ToolRegistry
from tools.base import Tool, _is_nested, _to_tsv
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

logger = logging.getLogger(__name__)

# ── MCP 配置 ──────────────────────────────────────────────────
_MCP_ENABLED = os.getenv("AGENT_MCP_ENABLED", "false").lower() in ("1", "true", "yes")
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
#  SKILL.md 解析（标准 frontmatter + body）
# ═══════════════════════════════════════════════════════════════

def _parse_frontmatter(markdown: str) -> tuple[dict, str]:
    """解析 SKILL.md → (frontmatter_dict, body_str)。标准 Anthropic 格式。"""
    meta = {}
    body = markdown
    if markdown.startswith("---"):
        parts = markdown.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                meta = yaml.safe_load(parts[1]) or {}
            except Exception:
                # 降级：逐行解析
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
            body = parts[2].strip()
    return meta, body


def _detect_referenced_skills(body: str, skill_names: List[str]) -> List[str]:
    """从 SKILL.md body 中检测被引用的技能名。"""
    found = []
    body_lower = body.lower()
    for name in skill_names:
        # 匹配 "调用 xxx 技能" 或 "skill:xxx" 或直接出现技能名
        if name.lower() in body_lower or name.replace("-", "_").lower() in body_lower:
            found.append(name)
    return found


def _extract_headings(body: str) -> List[str]:
    """提取 SKILL.md body 中所有 ## 标题。"""
    headings = []
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            headings.append(stripped[3:].strip())
    return headings


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


# ── _SmartDict / _SmartList：在保留 dict/list 接口的同时实现 to_str 格式化 ──

class _SmartDict(dict):
    """Dict 子类，str/repr 使用 to_str 的 token 高效格式，同时支持 key 索引。"""

    def __repr__(self):
        return _fmt_tool_output(self)

    def __str__(self):
        return _fmt_tool_output(self)


class _SmartList(list):
    """List 子类，str/repr 使用 to_str 的 token 高效格式。"""

    def __repr__(self):
        return _fmt_tool_output(self)

    def __str__(self):
        return _fmt_tool_output(self)


def _fmt_tool_output(data) -> str:
    """对数据应用 ToolResult.to_str() 的同款格式化。"""
    import json

    if isinstance(data, dict) and not _is_nested(data):
        return "\n".join(f"- {k}: {v}" for k, v in data.items())
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        return _to_tsv(data)
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return str(data)


class _SmolTool(SmolToolBase):
    """把 Tool 包装为 smolagents Tool 子类。"""
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

    def forward(self, **kwargs):
        try:
            result = asyncio.run(self._wrapped.safe_execute(**kwargs))
        except Exception as e:
            return f"[工具执行失败] {self.name}: {e}"
        if result.success:
            if isinstance(result.output, dict):
                return _SmartDict(result.output)      # 保留 dict 接口 + 漂亮格式化
            if isinstance(result.output, list):
                return _SmartList(result.output)      # 保留 list 接口 + 漂亮格式化
        return result.to_str()

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _SkillSectionTool(SmolToolBase):
    """让 CodeAgent 按需加载 SKILL.md 的段落（渐进式加载）。"""
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


class _WorkflowSectionTool(SmolToolBase):
    """让 CodeAgent 按需加载工作流 SKILL.md 的段落（渐进式加载）。"""
    skip_forward_signature_validation = True

    def __init__(self, body: str):
        super().__init__()
        self._body = body
        self._sections = self._split_sections(body)
        self.name = "read_workflow_section"
        headings = [s[0] for s in self._sections]
        headings_text = ", ".join(headings) if headings else "(无段落)"
        self.description = (
            f"读取当前工作流的某个步骤的详细指令。\n"
            f"可用步骤: {headings_text}\n"
            f"参数: heading（步骤标题关键词）"
        )
        self.output_type = "string"
        self.inputs = {
            "heading": {
                "type": "string",
                "description": f"步骤标题关键词，可用: {headings_text}",
            }
        }

    @staticmethod
    def _split_sections(body: str) -> List[tuple]:
        """按 ## 标题切分，返回 [(heading, content), ...]。"""
        sections = []
        pattern = re.compile(r"^##\s+(.+)$", re.MULTILINE)
        matches = list(pattern.finditer(body))
        if not matches:
            return [("(全文)", body)]
        for i, m in enumerate(matches):
            heading = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            sections.append((heading, body[start:end].strip()))
        return sections

    def forward(self, heading: str = "", **kwargs) -> str:
        kw = (heading or kwargs.get("section", "")).lower()
        if not kw:
            headings = [s[0] for s in self._sections]
            return f"[错误] 未指定步骤。可用步骤: {headings}"
        for h, content in self._sections:
            if kw in h.lower():
                return content
        headings = [s[0] for s in self._sections]
        return f"[错误] 未找到步骤 '{kw}'。可用步骤: {headings}"

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
#  MCP 工具加载
# ═══════════════════════════════════════════════════════════════

def _load_mcp_tools() -> list:
    try:
        from smolagents import ToolCollection
        from mcp import StdioServerParameters

        server = StdioServerParameters(
            command=_MCP_SERVER_CMD,
            args=_MCP_SERVER_ARGS,
        )
        return server
    except ImportError:
        logger.warning("[TaskAgent] MCP 依赖未安装 (pip install mcp)")
        return None
    except Exception as e:
        logger.warning("[TaskAgent] MCP 初始化失败: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════
#  TaskAgent
# ═══════════════════════════════════════════════════════════════

class TaskAgent(AgentBase):
    """
    任务型 Agent — plan 写流程，executor 按流程执行

    1. plan 阶段：LLM 生成 SKILL.md 格式的工作流
    2. executor 阶段：加载技能工具 + 指定工具 → CodeAgent 按 SKILL.md 执行
    3. 无工具 → 委托 AgentBase.chat()
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
        return self.skill_adapter

    # ── plan: 生成工作流 SKILL.md ─────────────────────────────

    async def _plan(
        self,
        user_input: str,
        llm: LLMBase,
        trace: AgentTraceRecorder,
    ) -> str:
        """Plan 节点：LLM 生成 SKILL.md 格式的工作流文档。

        Returns:
            工作流 Markdown 文本。空字符串表示无需工具。
        """
        # 构建技能描述
        skills_desc = []
        if self.skill_adapter:
            for s in self.skill_adapter.list_skills():
                skills_desc.append(f"- {s['name']}: {s.get('description', '')[:150]}")
        skills_text = "\n".join(skills_desc) if skills_desc else "(无可用技能)"

        # 构建工具描述
        tools_desc = []
        if self.tool_registry:
            for name in self.tool_registry.list_tools():
                tool = self.tool_registry.get(name)
                if tool:
                    tools_desc.append(f"- {name}: {tool.description[:100]}")
        tools_text = "\n".join(tools_desc) if tools_desc else "(无可用工具)"

        template = _load_plan_template()
        prompt = template.format(
            skills_text=skills_text,
            tools_text=tools_text,
            user_input=user_input,
        )

        messages = [
            ChatMessage(role="system", content="你是任务规划专家。只输出 SKILL.md 文档，不要输出其他内容。"),
            ChatMessage(role="user", content=prompt),
        ]

        trace.record("plan_request", {
            "model": getattr(llm, "model", ""),
            "skills_available": [s["name"] for s in (self.skill_adapter.list_skills() if self.skill_adapter else [])],
            "tools_available": self.tool_registry.list_tools() if self.tool_registry else [],
        })

        plan_start = time.time()
        try:
            # plan 专用超时：30s，失败立即降级到 chat
            response = await asyncio.wait_for(
                llm.generate(messages=messages),
                timeout=30,
            )
        except Exception as e:
            # plan 超时/失败 → 快速降级到 chat，不阻塞用户
            logger.warning("[TaskAgent] plan 失败，降级到 chat: %s", e)
            trace.record("plan_error", {"error": str(e), "fallback": "chat"})
            return ""

        trace.record("plan_response", {
            "elapsed_seconds": round(time.time() - plan_start, 3),
            **llm_response_to_dict(response),
        })

        markdown = response.content.strip()

        # 清理代码块包裹
        if markdown.startswith("```"):
            first_newline = markdown.index("\n")
            markdown = markdown[first_newline + 1:]
        if markdown.endswith("```"):
            markdown = markdown[:-3].rstrip()

        # 判断是否需要工具：检查 body 中有没有 ## 步骤
        _, body = _parse_frontmatter(markdown)
        has_steps = bool(re.search(r"^##\s+", body, re.MULTILINE))

        if not has_steps:
            logger.info("[TaskAgent] plan: 无步骤，直接对话")
            trace.record("plan_result", {"has_workflow": False, "reason": "无步骤"})
            return ""

        logger.info("[TaskAgent] plan: 生成工作流，body 长度 %d", len(body))
        trace.record("plan_result", {"has_workflow": True, "body_len": len(body)})

        return markdown

    # ── 快速通道：明显的非工具场景，跳过 plan ───────────────

    _FAST_CHAT_RE = re.compile(
        r"^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo"
        r"|再见|拜拜|bye|88|886|晚安|回见"
        r"|谢谢|感谢|多谢|thanks|thank\s*you|thx|3q"
        r"|好的?|ok|okay|嗯|哦|知道了|明白|收到"
        r"|哈哈哈+|233+|666+|\.\.\."
        r"|的确|确实|有道理|没错|对的?|是的?|嗯嗯)[\s\?\!\,\~\。\，\！\？\…]*$",
        re.IGNORECASE,
    )

    def _is_fast_chat(self, user_input: str) -> bool:
        """判断是否走快速通道（跳过 plan，直接对话）。"""
        text = user_input.strip()
        # 问候/告别/确认等固定短语
        if self._FAST_CHAT_RE.match(text):
            return True
        # 太短（< 6 字），plan 没有意义
        if len(text) < 6:
            return True
        return False

    # ── 主对话入口 ────────────────────────────────────────────

    async def chat(
        self,
        user_input: str,
        session_id: str = "default",
        use_rag: bool = True,
    ) -> AgentResponse:
        if _MCP_ENABLED:
            return await self._chat_mcp(user_input, session_id, use_rag)
        # 快速通道：明显的非工具场景，跳过 plan
        if self._is_fast_chat(user_input):
            logger.info("[TaskAgent] 快速通道，跳过 plan")
            return await super().chat(user_input, session_id=session_id, use_rag=use_rag)
        return await self._chat_plan(user_input, session_id, use_rag)

    # ── MCP 模式（不变）──────────────────────────────────────

    async def _chat_mcp(
        self,
        user_input: str,
        session_id: str,
        use_rag: bool,
    ) -> AgentResponse:
        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=f"{type(self).__name__}(MCP)",
            session_id=session_id,
            user_input=user_input,
            metadata={"mode": "mcp", "use_rag": use_rag},
        )

        try:
            sources = []
            context = ""
            if use_rag and self.retriever:
                docs = await self.retriever.retrieve(user_input)
                if docs:
                    context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]

            task_parts = []
            if context:
                task_parts.append(f"【参考资料】\n{context}")
            task_parts.append(f"【任务】\n{user_input}")
            task = "\n\n".join(task_parts)

            from smolagents import ToolCollection, CodeAgent as SmolCodeAgent
            from pathlib import Path
            import yaml

            mcp_server = _load_mcp_tools()
            if not mcp_server:
                logger.info("[TaskAgent] MCP 不可用，降级到传统模式")
                return await self._chat_plan(user_input, session_id, use_rag)

            model = _LLMAdapter(self.llm)

            with ToolCollection.from_mcp(mcp_server) as mcp_tools:
                tool_list = list(mcp_tools)
                logger.info("[TaskAgent] MCP 加载 %d 个工具", len(tool_list))
                trace.record("mcp_tools_loaded", {"count": len(tool_list)})

                agent = SmolCodeAgent(
                    tools=tool_list,
                    model=model,
                    max_steps=self.max_tool_rounds,
                )

                _rule_path = Path(__file__).resolve().parent.parent / "prompts" / "format_rules.yaml"
                format_rules = yaml.safe_load(_rule_path.read_text(encoding="utf-8"))
                agent.prompt_templates["system_prompt"] += "\n" + format_rules["system_prompt_suffix"]

                react_start = time.time()
                result = agent.run(task)
                react_elapsed = round(time.time() - react_start, 2)
                trace.record("mcp_agent_done", {"elapsed_seconds": react_elapsed, "result_preview": str(result)[:200]})

            result_raw = str(result)

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
                content=result_raw, sources=sources, session_id=session_id,
                elapsed_seconds=elapsed, metadata={"trace_id": trace.trace_id, "mode": "mcp"},
            )
            trace.finish(response=response.to_dict())
            return response

        except Exception as e:
            trace.fail(e)
            raise

    # ── 传统模式：plan 写流程 + executor 执行 ─────────────────

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
            metadata={"mode": "workflow", "use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            # 1. Plan 生成工作流 SKILL.md
            workflow_md = await self._plan(user_input, self.llm, trace)

            # 2. 无工作流 → 直接对话
            if not workflow_md:
                trace.record("delegate_chat", {"reason": "plan: 无需工具"})
                trace.finish(response={"delegated_to": "AgentBase.chat"})
                return await super().chat(user_input, session_id=session_id, use_rag=use_rag)

            # 3. RAG 检索
            sources = []
            rag_context = ""
            if use_rag and self.retriever:
                rag_start = time.time()
                docs = await self.retriever.retrieve(user_input)
                trace.record("rag_retrieve", {
                    "elapsed_seconds": round(time.time() - rag_start, 3),
                    "doc_count": len(docs),
                    "docs": docs,
                })
                if docs:
                    rag_context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]

            # 4. 执行工作流
            result_raw = await self._execute_workflow(workflow_md, user_input, rag_context, trace)

            # 5. 保存对话历史
            if self.memory:
                await self.memory.add(session_id, "user", user_input)
                await self.memory.add(session_id, "assistant", result_raw)
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

    # ── Workflow Executor ─────────────────────────────────────

    async def _execute_workflow(
        self,
        workflow_md: str,
        user_input: str,
        rag_context: str,
        trace: AgentTraceRecorder,
    ) -> str:
        """
        执行工作流 SKILL.md。

        统一执行方式：加载技能工具 + 指定工具 → CodeAgent 按 SKILL.md 执行。
        不区分 step 类型，CodeAgent 自己读指令、调工具。
        """
        meta, body = _parse_frontmatter(workflow_md)
        workflow_name = meta.get("name", "unnamed")
        declared_tools = meta.get("tools", [])

        logger.info("[Executor] 执行工作流: %s", workflow_name)
        trace.record("workflow_start", {"name": workflow_name, "body_len": len(body)})

        # ── 构建工具集 ──

        smol_tools = []

        # 1. frontmatter 中声明的普通工具
        if declared_tools and self.tool_registry:
            smol_tools.extend(_build_smol_tools(self.tool_registry, declared_tools))
            logger.info("[Executor] 加载声明工具: %s", declared_tools)

        # 2. 从 body 中检测引用的技能，加载技能函数工具
        loader = self._get_skill_loader()
        if loader:
            all_skill_names = [s["name"] for s in loader.list_skills()]
            referenced_skills = _detect_referenced_skills(body, all_skill_names)

            for skill_name in referenced_skills:
                # 技能的 Python 函数 → CodeAgent 工具
                func_tools = _load_skill_functions(skill_name)
                smol_tools.extend(func_tools)
                # 渐进式加载工具
                smol_tools.append(_SkillSectionTool(loader, skill_name))
                smol_tools.append(_SkillResourceTool(loader, skill_name))
                logger.info("[Executor] 加载技能 '%s': %d 个函数工具", skill_name, len(func_tools))

            trace.record("skills_loaded", {
                "referenced": referenced_skills,
                "tool_count": len(smol_tools),
            })

        # ── 构建任务指令（渐进式加载）──

        task_parts = []

        # 工作流 body：短的全量注入，长的只注入段落目录
        _PROGRESSIVE_THRESHOLD = 500  # 词数阈值
        word_count = len(body.split())
        if word_count > _PROGRESSIVE_THRESHOLD:
            # 只注入段落目录 + 原始 body 存到工具里
            headings = _extract_headings(body)
            headings_text = "\n".join(f"  - {h}" for h in headings)
            task_parts.append(
                f"【工作流指令】\n"
                f"使用 read_workflow_section 工具按需加载各步骤的详细指令。\n"
                f"可用步骤:\n{headings_text}"
            )
            # 加一个读取工作流段落的工具
            smol_tools.append(_WorkflowSectionTool(body))
            logger.info("[Executor] 工作流 body 过长(%d 词)，启用渐进式加载", word_count)
        else:
            task_parts.append(f"【工作流指令】\n{body}")

        if rag_context:
            task_parts.append(f"【参考资料】\n{rag_context}")

        task_parts.append(f"【原始用户问题】\n{user_input}")

        task = "\n\n".join(task_parts)

        # ── CodeAgent 执行 ──

        logger.info("[Executor] CodeAgent 执行，工具: %s", [t.name for t in smol_tools])
        trace.record("code_agent_setup", {"tools": [t.name for t in smol_tools]})

        result = await self._run_code_agent(smol_tools, task)

        trace.record("workflow_done", {"result_preview": result[:300]})
        return result

    # ── CodeAgent 执行器 ──────────────────────────────────────

    async def _run_code_agent(self, smol_tools: list, task: str) -> str:
        """用 smolagents CodeAgent 执行任务。"""
        from smolagents import CodeAgent as SmolCodeAgent
        from pathlib import Path
        import yaml

        model = _LLMAdapter(self.llm)
        agent = SmolCodeAgent(
            tools=smol_tools,
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

        result = agent.run(task)
        return str(result)
