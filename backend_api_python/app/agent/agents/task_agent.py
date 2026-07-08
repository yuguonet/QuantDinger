# -*- coding: utf-8 -*-
"""
任务型 Agent — MCP + Plan

流程：
  user input
    -> 负面反馈检测
    -> plan: LLM 决定用哪些技能/工具
    -> 有技能 → 已有技能渐进式加载
    -> 无技能 → 自动生成临时 SKILL.md
    -> MCPExecutor 注入 MCP 工具到代码执行命名空间 → CodeAgent(tools=[]) 执行
    -> response

设计要点：
  - MCP 常驻连接，首次启动后复用，避免每次请求 2-3s 启动开销
  - _plan() 同时选技能和工具：有技能走技能，没技能走工具+自动生成SKILL.md
  - CodeAgent 传 tools=[]，工具描述 0 token；MCP 工具通过 MCPExecutor 注入 static_tools
  - LLM 生成的 Python 代码直接调用 MCP 工具名，由 MCPExecutor 路由到 MCP bridge
  - 临时 SKILL.md 存内存，后续可持久化为编排路径，重复任务直接加载跳过 plan
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
from executor.mcp_executor import MCPRouterTool, MCPExecutor, build_tool_catalog
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

# 使用 app.agent logger（与 log.py 配置一致，确保日志写入文件）
try:
    from log import logger
except ImportError:
    logger = logging.getLogger(__name__)

# ── 模块级 TraceCollector 存储 ──
_collectors: Dict[str, "TraceCollector"] = {}

# ── MCP 配置 ──────────────────────────────────────────────────
_MCP_SERVER_CMD = os.getenv("AGENT_MCP_SERVER_CMD", "python")
# 默认路径：相对于本文件(app/agent/)定位 mcp_bridge.py
# 从 backend_api_python/ 运行时，子进程 cwd 是 backend_api_python/
# 所以需要用 app/agent/tools/mcp_bridge.py 而非 tools/mcp_bridge.py
_MCP_SERVER_ARGS = os.getenv(
    "AGENT_MCP_SERVER_ARGS",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools", "mcp_bridge.py")),
).split()

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


class _TempSkillSectionTool(SmolToolBase):
    """从内存 SKILL.md body 按需加载段落（临时技能用）。

    与 _SkillSectionTool 的区别：
      - _SkillSectionTool：从 QDSkillAdapter 加载（文件系统）
      - _TempSkillSectionTool：从内存字符串加载（临时生成）
    接口一致，CodeAgent 无感知，统一用 read_skill_section 调用。

    兼容性：
      - 段落切分逻辑与 QDSkillAdapter._split_sections 一致（按 ## 标题）
      - 支持 # ~ #### 四级标题
      - Anthropic SKILL.md 标准兼容

    扩展点：
      - 持久化时，body 直接写入 skills/auto_xxx/SKILL.md
      - 持久化后可改用 _SkillSectionTool 加载（从文件系统）
    """
    skip_forward_signature_validation = True

    def __init__(self, body: str, skill_name: str):
        super().__init__()
        self._body = body
        self._skill_name = skill_name
        self.name = "read_skill_section"

        # 按 ## 标题切分段落（re.finditer 方式，避免 split 的分组问题）
        self._sections = {}
        heading_pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
        matches = list(heading_pattern.finditer(body))
        if matches:
            # 前言（第一个标题之前的内容）
            preamble = body[:matches[0].start()].strip()
            if preamble:
                self._sections["(前言)"] = preamble
            # 各段落
            for i, m in enumerate(matches):
                heading = m.group(2).strip()
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
                content = body[start:end].strip()
                if content:
                    self._sections[heading] = content
        else:
            self._sections["(全文)"] = body

        headings = list(self._sections.keys())
        headings_text = ", ".join(headings) if headings else "(无段落)"
        self.description = (
            f"读取技能 '{skill_name}' 的指令段落。"
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
            return f"[错误] 未指定段落。可用段落: {list(self._sections.keys())}"
        kw_lower = kw.lower()
        for h, content in self._sections.items():
            if kw_lower in h.lower():
                return content
        return f"[错误] 未找到段落 '{kw}'。可用段落: {list(self._sections.keys())}"

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
    """MCP 连接单例 — 常驻子进程，避免每次请求重复启动。

    设计原因：
      - MCP 子进程启动 + 工具注册需要 2-3s，每条消息都启动一次浪费严重
      - 常驻连接后，首次 2-3s，后续 0 开销
      - smolagents ToolCollection 上下文管理器保持连接存活

    生命周期：
      - 进程启动 → _mcp = _MCPSingleton()（不启动子进程）
      - 首次 _mcp.tools / _mcp.available → 触发 _ensure() → 启动子进程
      - 进程退出 → 自动回收（daemon 线程）
      - 需要手动关闭时 → _mcp.close()

    扩展点：
      - 未来可加健康检查、自动重连、工具热更新
      - 未来可改为 SSE 模式连接远程 MCP server
    """

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


def _generate_temp_skill(steps: list, task_hint: str = "") -> dict | None:
    """从选定的执行步骤生成临时 SKILL.md（Anthropic 标准格式）。

    返回 {name, body}。body 符合 SKILL.md 规范：YAML frontmatter + markdown 标题结构。
    """
    if not steps:
        return None

    skill_name = "auto_skill"

    # 构建执行步骤 markdown（不标注串行/并行，LLM 自行决定代码块结构）
    steps_lines = []
    for i, step in enumerate(steps, 1):
        names = [t.name for t in step["tools"]]
        steps_lines.append(f"## 步骤 {i}\n")
        steps_lines.append(f"调用: {', '.join(names)}\n")
    steps_md = "\n".join(steps_lines)

    # 从 steps 中收集工具签名（去重）
    seen = set()
    tool_sigs = []
    for step in steps:
        for t in step["tools"]:
            tname = getattr(t, "name", "unknown")
            if tname in seen:
                continue
            seen.add(tname)
            tinputs = getattr(t, "inputs", {}) or {}
            params = ", ".join(f"{p}: {i.get('type', 'string')}" for p, i in tinputs.items())
            tool_sigs.append(f"  {tname}({params})")

    body = f"""# {skill_name}

{steps_md}
## 工具签名

"""
    body += "\n".join(tool_sigs)
    body += """

"""

    return {"name": skill_name, "body": body}


    # ── 主对话入口 ────────────────────────────────────────────


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
    ) -> dict:
        """Plan 节点：选择技能、划分执行阶段。

        设计决策：
          - _plan() 同时看到 MCP 工具列表和已有技能列表
          - 输出统一格式：skills + phases + planning_interval
          - phase.type=direct → LLM 直接回答（RAG + 知识）
          - phase.type=execute → CodeAgent + 工具执行
          - 单阶段 → planning_interval=None，跳过 eval
          - 多阶段 → planning_interval=动态值，eval + 重试
          - skills 和 tools 互斥：有技能走技能，没技能走工具

        Returns:
            {
              "skills": list[str],     # 已有技能名列表
              "phases": list[dict],    # 阶段列表 [{id, name, type, goal, tools}]
              "planning_interval": int | None,
              "expanded_query": str,
            }
        """
        # 构建工具描述
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

        plan = safe_parse_json(text, default={"skills": [], "phases": []})

        # 解析选定的技能
        selected_skill_names = plan.get("skills", [])
        all_skill_names = {s["name"] for s in self.skill_adapter.list_skills()} if self.skill_adapter else set()
        selected_skills = []
        for name in selected_skill_names:
            if name.startswith("skill:"):
                name = name[6:]
            if name in all_skill_names:
                selected_skills.append(name)

        # 解析阶段
        phases = plan.get("phases", [])
        if not phases:
            # 兜底：至少一个 direct 阶段
            phases = [{"id": 0, "name": "回答", "type": "direct", "goal": plan.get("expanded_query", user_input), "tools": []}]

        # 补全阶段字段
        for i, phase in enumerate(phases):
            phase.setdefault("id", i)
            phase.setdefault("name", "执行")
            phase.setdefault("type", "execute")
            phase.setdefault("goal", "")
            phase.setdefault("tools", [])

        # 解析 planning_interval
        planning_interval = plan.get("planning_interval", None)

        # 单阶段不需要 planning_interval
        if len(phases) <= 1:
            planning_interval = None

        expanded_query = plan.get("expanded_query", user_input) or user_input
        logger.info("[TaskAgent] plan: %d 技能 %s, %d 阶段 %s, planning_interval=%s",
                     len(selected_skills), selected_skills,
                     len(phases), [(p["name"], p["type"]) for p in phases],
                     planning_interval)
        trace.record("plan_result", {
            "route": "mcp+plan",
            "selected_skills": selected_skills,
            "phases": phases,
            "planning_interval": planning_interval,
        })

        return {
            "skills": selected_skills,
            "phases": phases,
            "planning_interval": planning_interval,
            "expanded_query": expanded_query,
        }


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

    # ── 阶段执行与评估 ──────────────────────────────────────

    def _build_code_agent(
        self,
        model,
        mcp_tool_list: list,
        skill_tools: list,
        planning_interval: int | None = None,
    ):
        """构建 smolagents CodeAgent 实例。

        每个阶段独立构建，避免状态污染。
        planning_interval: None=不 replan，3~5=每 N 步 replan。
        """
        from smolagents import CodeAgent as SmolCodeAgent
        from pathlib import Path
        import yaml

        catalog = build_tool_catalog(mcp_tool_list)

        router_tool = MCPRouterTool(
            tool_map={},
            tool_catalog=catalog,
        )

        full_tool_map = {t.name: t for t in mcp_tool_list}
        for st in skill_tools:
            sname = getattr(st, "name", "unknown")
            full_tool_map[sname] = st

        mcp_executor = MCPExecutor(
            router_tool=router_tool,
            full_tool_map=full_tool_map,
            additional_authorized_imports=["json", "datetime", "math", "re", "collections", "itertools"],
        )

        agent = SmolCodeAgent(
            tools=[],
            model=model,
            max_steps=self.max_tool_rounds,
            executor=mcp_executor,
            planning_interval=planning_interval,
        )

        # 注入工具使用说明（从文件加载）
        _tool_usage_path = Path(__file__).resolve().parent.parent / "prompts" / "tool_usage.txt"
        try:
            router_usage = "\n\n" + _tool_usage_path.read_text(encoding="utf-8")
            agent.prompt_templates["system_prompt"] += router_usage
        except Exception:
            pass

        # 注入格式规则
        _rule_path = Path(__file__).resolve().parent.parent / "prompts" / "format_rules.yaml"
        try:
            format_rules = yaml.safe_load(_rule_path.read_text(encoding="utf-8"))
            agent.prompt_templates["system_prompt"] += "\n" + format_rules["system_prompt_suffix"]
        except Exception:
            pass

        # 注入自定义 planning prompt（如果有 replan 间隔）
        if planning_interval:
            _replan_path = Path(__file__).resolve().parent.parent / "prompts" / "replan_system.txt"
            try:
                replan_prompt = _replan_path.read_text(encoding="utf-8")
                agent.prompt_templates["planning"]["initial_plan"] = replan_prompt
                agent.prompt_templates["planning"]["update_plan_pre_messages"] = replan_prompt
            except Exception:
                pass

        return agent

    async def _execute_phase(
        self,
        task: str,
        agent,
        phase: dict,
        trace: AgentTraceRecorder,
    ) -> str:
        """执行单个阶段。

        phase 格式: {id, name, goal, tools}
        返回: 阶段执行结果字符串
        """
        phase_id = phase.get("id", 0)
        phase_name = phase.get("name", "执行")
        phase_goal = phase.get("goal", "")

        logger.info("[TaskAgent] 执行阶段 %d: %s — %s", phase_id, phase_name, phase_goal)

        react_start = time.time()
        result = agent.run(task)
        react_elapsed = round(time.time() - react_start, 2)

        trace.record("phase_done", {
            "phase_id": phase_id,
            "elapsed_seconds": react_elapsed,
            "result_preview": str(result)[:200],
        })

        return str(result)

    async def _eval_phase(
        self,
        phase: dict,
        phase_result: str,
        llm: LLMBase,
    ) -> dict:
        """评估阶段是否通过。

        返回: {passed: bool, reason: str, suggestion: str}
        """
        from pathlib import Path

        _eval_path = Path(__file__).resolve().parent.parent / "prompts" / "phase_eval.txt"
        try:
            eval_template = _eval_path.read_text(encoding="utf-8")
        except Exception:
            # 兜底：无评估模板则默认通过
            return {"passed": True, "reason": "无评估模板，默认通过", "suggestion": ""}

        prompt = eval_template.format(
            phase_name=phase.get("name", ""),
            phase_goal=phase.get("goal", ""),
            phase_result=phase_result[:2000],  # 截断避免 token 爆炸
        )

        messages = [
            ChatMessage(role="system", content="你是评估器。只输出 JSON。"),
            ChatMessage(role="user", content=prompt),
        ]

        try:
            response = await llm.generate(messages=messages)
            text = response.content.strip()
            if "```" in text:
                m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
                if m:
                    text = m.group(1).strip()
            result = safe_parse_json(text, default={"passed": True, "reason": "解析失败，默认通过", "suggestion": ""})
            return result
        except Exception as e:
            logger.warning("[TaskAgent] 阶段评估异常: %s，默认通过", e)
            return {"passed": True, "reason": f"评估异常: {e}", "suggestion": ""}

    # ── 主对话入口（多阶段循环）──────────────────────────────

    async def _chat_plan(
        self,
        user_input: str,
        session_id: str,
        use_rag: bool,
    ) -> AgentResponse:
        """主对话流程：Plan → 多阶段执行 → 评估。

        量化任务: 1 个阶段，planning_interval=None
        编程任务: 多个阶段，planning_interval=动态值
        """
        print(f"[DEBUG] _chat_plan called: {user_input[:50]}", flush=True)
        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=type(self).__name__,
            session_id=session_id,
            user_input=user_input,
            metadata={"mode": "mcp+plan", "use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            mcp_ok = _mcp.available
            print(f"[DEBUG] MCP available: {mcp_ok}, tools: {len(_mcp.tools)}", flush=True)
            if not mcp_ok:
                logger.error("[TaskAgent] MCP 不可用")
                return await super().chat(user_input, session_id=session_id, use_rag=use_rag)

            mcp_tool_list = _mcp.tools
            model = _LLMAdapter(self.llm)

            # ── 1. Plan ──
            plan = await self._plan(user_input, self.llm, trace, mcp_tools=mcp_tool_list)
            selected_skills = plan["skills"]
            phases = plan["phases"]
            planning_interval = plan["planning_interval"]
            expanded_query = plan["expanded_query"]

            print(f"[DEBUG] plan: skills={selected_skills}, "
                  f"phases={[(p['name'], p['type']) for p in phases]}, pi={planning_interval}", flush=True)

            # ── 2. RAG 检索（所有阶段共享）──
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

            # ── 3. 构建技能指令 ──
            task_parts = []
            if context:
                task_parts.append(f"【参考资料】\n{context}")

            loader = self._get_skill_loader()
            temp_skill = None

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

            # 无匹配技能时，从阶段工具生成临时 SKILL.md
            if not selected_skills:
                all_tools = []
                for phase in phases:
                    all_tools.extend(phase.get("tools", []))
                if all_tools:
                    # _generate_temp_skill 期望 tools 是带 .name 属性的对象
                    _ToolStub = type("_ToolStub", (), {"__init__": lambda self, n: setattr(self, "name", n) or setattr(self, "inputs", {})})
                    temp_steps = [{"tools": [_ToolStub(t) for t in all_tools]}]
                    temp_skill = _generate_temp_skill(temp_steps, task_hint=user_input)
                    if temp_skill:
                        selected_skills = [temp_skill["name"]]
                        task_parts.append(f"【技能指令: {temp_skill['name']}】\n{temp_skill['body']}")
                        logger.info("[TaskAgent] 自动生成技能: %s", temp_skill["name"])
                        trace.record("temp_skill_generated", {"name": temp_skill["name"]})

            # ── 4. 加载技能工具 ──
            skill_tools = []
            for sname in selected_skills:
                if temp_skill and sname == temp_skill["name"]:
                    skill_tools.append(_TempSkillSectionTool(temp_skill["body"], sname))
                    continue
                func_tools = _load_skill_functions(sname)
                skill_tools.extend(func_tools)
                skill_tools.append(_SkillSectionTool(loader, sname))
                skill_tools.append(_SkillResourceTool(loader, sname))
                logger.info("[TaskAgent] skill %s 注入函数工具: %s", sname, [t.name for t in func_tools])

            # ── 5. 创建 TraceCollector ──
            collector = None
            try:
                from trace_collector import TraceCollector
                collector = TraceCollector(session_id=session_id, user_query=user_input)
                for sname in selected_skills:
                    collector.begin_skill(sname)
                _collectors[session_id] = collector
            except Exception as e:
                logger.debug("[Trace] TraceCollector 创建失败: %s", e)

            # ── 6. 阶段执行循环 ──
            phase_results = []
            max_retries = 1
            is_single_phase = len(phases) == 1

            for phase in phases:
                phase_id = phase.get("id", 0)
                phase_name = phase.get("name", "执行")
                phase_type = phase.get("type", "execute")
                phase_goal = phase.get("goal", "")

                trace.record("phase_start", {"phase_id": phase_id, "phase_name": phase_name, "phase_type": phase_type, "phase_goal": phase_goal})

                # ── direct 类型：RAG + Memory + LLM 直接回答 ──
                if phase_type == "direct":
                    logger.info("[TaskAgent] 阶段 %d: direct — %s", phase_id, phase_name)
                    messages = [ChatMessage(role="system", content=self.system_prompt)]
                    if context:
                        messages.append(ChatMessage(role="system", content=f"【参考资料】\n{context}"))
                    if self.memory:
                        history = await self.memory.get_history(session_id, limit=self.memory_window_size)
                        for msg in history:
                            messages.append(ChatMessage(role=msg.role, content=msg.content))
                    messages.append(ChatMessage(role="user", content=user_input))
                    llm_response = await self.llm.generate(messages=messages)
                    phase_result = llm_response.content
                    phase_results.append({
                        "phase": phase,
                        "result": phase_result,
                        "passed": True,
                        "retries": 0,
                    })
                    continue

                # ── execute 类型：CodeAgent + 工具 ──
                logger.info("[TaskAgent] 阶段 %d: execute — %s", phase_id, phase_name)

                # 构建阶段任务
                phase_roadmap = self._build_phase_roadmap(phases, phase_id)
                phase_task_parts = list(task_parts)
                phase_task_parts.append(phase_roadmap)
                phase_task_parts.append(f"【当前阶段】{phase_name} — {phase_goal}")
                phase_task_parts.append(f"【任务】\n{expanded_query}")
                phase_task = "\n\n".join(phase_task_parts)

                # 构建 CodeAgent（每阶段独立）
                agent = self._build_code_agent(
                    model=model,
                    mcp_tool_list=mcp_tool_list,
                    skill_tools=skill_tools,
                    planning_interval=planning_interval,
                )

                # 单阶段：直接执行，跳过 eval
                if is_single_phase:
                    phase_result = await self._execute_phase(phase_task, agent, phase, trace)
                    phase_results.append({
                        "phase": phase,
                        "result": phase_result,
                        "passed": True,
                        "retries": 0,
                    })
                    continue

                # 多阶段：执行 + eval + 重试
                retry_count = 0
                phase_passed = False
                phase_result = ""

                while retry_count <= max_retries:
                    phase_result = await self._execute_phase(phase_task, agent, phase, trace)

                    # 评估阶段
                    eval_result = await self._eval_phase(phase, phase_result, self.llm)
                    phase_passed = eval_result.get("passed", True)

                    if phase_passed:
                        logger.info("[TaskAgent] 阶段 %d 通过: %s", phase_id, phase_name)
                        break

                    retry_count += 1
                    if retry_count <= max_retries:
                        logger.info("[TaskAgent] 阶段 %d 未通过，重试 %d/%d: %s",
                                    phase_id, retry_count, max_retries, eval_result.get("reason", ""))
                        trace.record("phase_retry", {
                            "phase_id": phase_id,
                            "retry": retry_count,
                            "reason": eval_result.get("reason", ""),
                        })
                        # 重试时注入失败原因
                        phase_task += f"\n\n【上次执行问题】{eval_result.get('reason', '')}\n{eval_result.get('suggestion', '')}"
                    else:
                        logger.warning("[TaskAgent] 阶段 %d 重试耗尽，继续下一阶段", phase_id)
                        trace.record("phase_failed", {
                            "phase_id": phase_id,
                            "reason": eval_result.get("reason", ""),
                        })

                phase_results.append({
                    "phase": phase,
                    "result": phase_result,
                    "passed": phase_passed,
                    "retries": retry_count,
                })

            # ── 7. TraceCollector 存库 ──
            if collector:
                try:
                    collector.end_skill()
                    final_answer = "\n\n".join(r["result"] for r in phase_results)
                    collector.on_agent_finish(
                        final_answer=final_answer,
                        total_steps=len(phase_results),
                        total_tokens=0,
                        model=getattr(self.llm, "model", "unknown"),
                    )
                    collector.flush()
                except Exception as e:
                    logger.warning("[Trace] 存库失败: %s", e)
                finally:
                    _collectors.pop(session_id, None)

            # ── 8. 汇总结果 ──
            if len(phase_results) == 1:
                result_raw = phase_results[0]["result"]
            else:
                result_parts = []
                for pr in phase_results:
                    status = "✅" if pr["passed"] else "❌"
                    result_parts.append(f"{status} 阶段: {pr['phase'].get('name', '')}\n{pr['result']}")
                result_raw = "\n\n".join(result_parts)

            # ── 9. 保存对话历史 ──
            if self.memory:
                await self.memory.add(session_id, "user", user_input)
                await self.memory.add(session_id, "assistant", result_raw)

            elapsed = round(time.time() - start_time, 2)
            response = AgentResponse(
                content=result_raw,
                sources=sources,
                session_id=session_id,
                elapsed_seconds=elapsed,
                metadata={"trace_id": trace.trace_id, "phase_count": len(phases), "phase_types": [p.get("type") for p in phases]},
            )
            trace.finish(response=response.to_dict())
            return response

        except Exception as e:
            trace.fail(e)
            _collectors.pop(session_id, None)
            raise

    def _build_phase_roadmap(self, phases: list, current_phase_id: int) -> str:
        """构建阶段路线图提示（全量可见，标记当前阶段）。"""
        lines = ["【执行路线图】（仅作参考，不要跳过当前阶段）"]
        for phase in phases:
            pid = phase.get("id", 0)
            name = phase.get("name", "")
            goal = phase.get("goal", "")
            if pid == current_phase_id:
                lines.append(f"  → 阶段{pid}: {name} — {goal} ← 你在这里")
            elif pid < current_phase_id:
                lines.append(f"  ✓ 阶段{pid}: {name}")
            else:
                lines.append(f"  ○ 阶段{pid}: {name} — {goal}")
        lines.append("\n【约束】完成当前阶段后输出结果，不要自行进入下一阶段。")
        return "\n".join(lines)
