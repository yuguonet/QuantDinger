# -*- coding: utf-8 -*-
"""
任务型 Agent — 统一多阶段

流程：
  user input
    -> 负面反馈检测
    -> plan: LLM 选择技能、划分阶段（不选具体工具）
    -> 统一多阶段循环：
       - skill 阶段：加载技能指令 + 工具 → CodeAgent 执行
       - execute 阶段：MCP 通用工具 → CodeAgent 执行
       - direct 阶段：LLM 直接回答
    -> 规则 eval（结果非空 → passed）
    -> response

设计要点：
  - MCP 常驻连接，首次启动后复用，避免每次请求 2-3s 启动开销
  - _plan() 只看技能列表，不看 MCP 全量工具（工具在执行阶段自动注入）
  - 统一多阶段：所有任务走同一套循环，无单阶段/多阶段分支
  - eval 用规则判断，不调 LLM，省 token
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

import yaml

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

# CodeAgent YAML 提示词模板（缓存）
_CODE_AGENT_YAML: dict | None = None
_CODE_AGENT_YAML_PATH = os.path.join(
    os.path.dirname(__file__), "..", "prompts", "code_agent.yaml"
)


def _load_plan_template() -> str:
    global _PLAN_TEMPLATE
    if _PLAN_TEMPLATE is None:
        with open(_PLAN_TEMPLATE_PATH, encoding="utf-8") as f:
            _PLAN_TEMPLATE = f.read()
    return _PLAN_TEMPLATE


def _load_code_agent_yaml() -> dict:
    global _CODE_AGENT_YAML
    if _CODE_AGENT_YAML is None:
        with open(_CODE_AGENT_YAML_PATH, encoding="utf-8") as f:
            _CODE_AGENT_YAML = yaml.safe_load(f)
    return _CODE_AGENT_YAML


# ═══════════════════════════════════════════════════════════════
#  smolagents 适配层
# ═══════════════════════════════════════════════════════════════

class _LLMAdapter:
    """把 LLMBase 包装为 smolagents Model 接口。"""

    def __init__(self, llm: LLMBase):
        self._llm = llm
        self.model_id = getattr(llm, "model", "unknown")

    @staticmethod
    def _normalize_code_blocks(content: str) -> str:
        """将 markdown 代码块转为 smolagents <code>...</code> 格式。"""
        import re
        # ```python ... ``` → <code>...</code>
        content = re.sub(r'```python\s*\n(.*?)```', r'<code>\n\1</code>', content, flags=re.DOTALL)
        # ``` ... ``` → <code>...</code>（排除已处理的）
        content = re.sub(r'```\s*\n(.*?)```', r'<code>\n\1</code>', content, flags=re.DOTALL)
        # 裸 final_answer() 没有 <code> 包裹 → 包裹它
        if 'final_answer(' in content and '<code>' not in content:
            content = f'<code>\n{content}\n</code>'
        return content

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
        except KeyboardInterrupt:
            logger.warning("[LLMAdapter] LLM 调用被中断")
            raise
        except Exception as e:
            logger.error("[TaskAgent] LLM 调用失败: %s", e)
            raise

        if resp.finish_reason == "error":
            raise RuntimeError(f"LLM 调用失败: {resp.content}")

        # 规范化：markdown 代码块 → <code>...</code>
        raw_content = resp.content or ""
        normalized = self._normalize_code_blocks(raw_content)
        if normalized != raw_content:
            logger.debug("[LLMAdapter] 代码块格式已规范化")

        smol_msg = SmolChatMessage(role="assistant", content=normalized)

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
        self._lock = threading.Lock()

    def _ensure(self) -> bool:
        if self._tools is not None:
            return True
        with self._lock:
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
                import threading
                # Windows ProactorEventLoop 下 __exit__ 可能挂住，加超时保护
                done = threading.Event()
                def _do_close():
                    try:
                        self._ctx.__exit__(None, None, None)
                    except Exception:
                        pass
                    done.set()
                t = threading.Thread(target=_do_close, daemon=True)
                t.start()
                done.wait(timeout=3)
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
    任务型 Agent — 统一多阶段

    1. plan: LLM 选择技能、划分阶段
    2. 统一多阶段循环：skill/execute/direct
    3. 规则 eval
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
    ) -> dict:
        """Plan 节点：选择技能、划分执行阶段。

        设计决策：
          - _plan() 只看技能列表，不看 MCP 全量工具（工具在执行阶段自动注入）
          - 输出统一格式：phases + planning_interval
          - phase.type=skill → 加载该技能的 SKILL.md + 工具
          - phase.type=execute → CodeAgent + MCP 通用工具
          - phase.type=direct → LLM 直接回答（不调工具）

        Returns:
            {
              "phases": list[dict],    # [{id, name, type, skill?, goal}]
              "planning_interval": int,
              "expanded_query": str,
            }
        """
        # 构建技能描述（含权重 + SKILL.md 执行流程）
        skills_desc = []
        if self.skill_adapter:
            # 从数据库读取技能历史权重
            skill_weights = {}
            try:
                from chain.store import get_skill_weights
                skill_weights = get_skill_weights()
            except Exception as e:
                logger.debug("[Plan] 获取技能权重失败: %s", e)

            skills = self.skill_adapter.list_skills()
            # 按权重降序排列（无权重默认 0.5，排在已验证技能之后）
            skills.sort(key=lambda s: skill_weights.get(s['name'], 0.5), reverse=True)

            for s in skills:
                name = s['name']
                desc = s.get('description', '')[:150]
                weight = skill_weights.get(name)
                weight_tag = f" [权重:{weight:.2f}]" if weight is not None else ""

                # 加载 SKILL.md 的执行流程段落
                flow = ""
                try:
                    body = self.skill_adapter.load_body(name)
                    if body:
                        lines = body.split('\n')
                        in_flow = False
                        flow_lines = []
                        for line in lines:
                            if '执行流程' in line or '执行步骤' in line:
                                in_flow = True
                            elif in_flow and line.startswith('#') and '执行' not in line:
                                break
                            if in_flow:
                                flow_lines.append(line)
                        if flow_lines:
                            flow = '\n'.join(flow_lines[:30])
                except Exception:
                    pass
                if flow:
                    skills_desc.append(f"- {name}{weight_tag}: {desc}\n{flow}")
                else:
                    skills_desc.append(f"- {name}{weight_tag}: {desc}")
        skills_text = "\n".join(skills_desc) if skills_desc else "(无可用技能)"

        # 注入 MCP 工具名列表，让规划器知道 CodeAgent 能调什么
        tool_names = []
        try:
            from agents.task_agent import _mcp
            if _mcp.available:
                tool_names = [t.name for t in _mcp.tools]
        except Exception:
            pass
        tools_hint = f"\n\n可用工具（CodeAgent 可直接调用，无需拆分阶段）：{', '.join(tool_names[:30])}..." if tool_names else ""

        template = _load_plan_template()
        # completed_phases_text: 已完成阶段的摘要（用于多轮规划），首次调用为空
        prompt = template.format(
            skills_text=skills_text,
            user_input=user_input,
            completed_phases_text=getattr(self, '_completed_phases_text', '') or '',
        ) + tools_hint

        messages = [
            ChatMessage(role="system", content="你是任务规划器。只输出 JSON。"),
            ChatMessage(role="user", content=prompt),
        ]

        trace.record("plan_request", {
            "model": getattr(llm, "model", ""),
            "skills_available": [s["name"] for s in self.skill_adapter.list_skills()] if self.skill_adapter else [],
        })

        plan_start = time.time()
        response = await llm.generate(messages=messages)
        trace.record("plan_response", {
            "elapsed_seconds": round(time.time() - plan_start, 3),
            **llm_response_to_dict(response),
        })

        text = (response.content or "").strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()

        plan = safe_parse_json(text, default={})

        task = plan.get("task", "") or plan.get("expanded_query", "") or user_input
        step_budget = plan.get("step_budget", 10) or 10
        planning_interval = max(step_budget // 2 + 1, 6)

        logger.info("[TaskAgent] plan: task=%s..., step_budget=%d, planning_interval=%d",
                     task[:80], step_budget, planning_interval)
        trace.record("plan_result", {
            "route": "plan",
            "task": task,
            "step_budget": step_budget,
            "planning_interval": planning_interval,
        })

        return {
            "task": task,
            "step_budget": step_budget,
            "planning_interval": planning_interval,
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
            check_negative_feedback(user_input, session_id=session_id)
        except Exception:
            pass
        return await self._chat_plan_graph(user_input, session_id, use_rag)

    # ── 阶段执行与评估 ──────────────────────────────────────

    def _build_code_agent(
        self,
        model,
        mcp_tool_list: list,
        skill_tools: list,
        planning_interval: int | None = None,
        phase_id: int = 0,
    ):
        """构建 smolagents CodeAgent 实例。

        每个阶段独立构建，避免状态污染。
        planning_interval: None=不 replan，3~5=每 N 步 replan。
        phase_id: 阶段ID，用于缓存key前缀。
        """
        from smolagents import CodeAgent as SmolCodeAgent
        from smolagents.memory import ActionStep
        from pathlib import Path

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
            additional_authorized_imports=["json", "datetime", "math", "re", "collections", "itertools", "concurrent.futures"],
        )

        # tools=[] 是故意的：MCP 工具通过 MCPExecutor router 模式注入，不走 smolagents 原生 Tool。
        # 原因：MCP 工具数量大（70+），注册为 smolagents Tool 会全量写入 system prompt，token 爆炸。
        # router 模式只注册 mcp 一个工具，LLM 通过 mcp(action="list") 动态发现，mcp(action="call") 调用。
        # 不要把 MCP 工具往 tools=[] 里塞，那是打补丁，不是正路。详见 mcp_executor.py 注释。

        # 阶段内 observations 截断（保留最近 2 步完整，防止 token 爆炸）
        keep_recent = 2

        def _truncate_observations(memory_step: ActionStep, agent: SmolCodeAgent) -> None:
            for step in agent.memory.steps:
                if isinstance(step, ActionStep) and step.step_number is not None:
                    if step.step_number <= memory_step.step_number - keep_recent:
                        if step.observations and len(str(step.observations)) > 200:
                            step.observations = str(step.observations)[:200] + "...(truncated)"
                        if hasattr(step, 'observations_images') and step.observations_images:
                            step.observations_images = None

        agent = SmolCodeAgent(
            tools=[],
            model=model,
            max_steps=self.max_tool_rounds,
            executor=mcp_executor,
            planning_interval=planning_interval,
            step_callbacks=[_truncate_observations],
            instructions=(
                "调用工具获取数据后，必须基于实际返回的数据评估，用 final_answer() 输出以下格式：\n\n"
                "**股票名称**: 股票名称 (股票代码)\n"
                "**操作建议**: 买入/卖出/持有/跳过\n"
                "**评    分**: 0-100\n"
                "**方    向**: 看涨/看跌/中性\n"
                "**置 信 度**: 高/中/低\n"
                "**时间窗口**: T+1/T+3/T+5\n"
                "**信    号**: 核心逻辑一句话\n"
                "**分    析**: 怎么分析的数据(100字内)\n\n"
                "【硬规则 - 违反即错误】\n"
                "- final_answer 中的所有数据必须来自 print 输出的实际结果，禁止编造任何数字\n"
                "- score 直接使用 technical_analysis 返回的 score 值（如返回52就写52）\n"
                "- direction 直接使用 technical_analysis 返回的 direction（如返回neutral就写中性）\n"
                "- signal 必须引用 technical_analysis.signal 的原文（如'强空头排列'）\n"
                "- 如果 technical_analysis.score < 55，action 不能是买入，应该持有或跳过\n"
                "- 如果 technical_analysis.direction 是 neutral，不要写看涨，应该写中性\n"
                "- 如果 technical_analysis.direction 是 bearish，不要写看涨或中性，应该写看跌\n"
                "- PE 为负数表示亏损，不要写'估值合理'\n"
                "- 工具返回 error 时，不要编造数据，该维度写'数据获取失败'\n"
                "- 用 final_answer() 输出，不要直接 print\n\n"
                "【数据补充策略】\n"
                "- 当关键工具返回 error 或数据为空时，使用 web_search 搜索最新信息补充\n"
                "- web_search 搜索关键词示例：'{股票名称} {股票代码} 最新消息 分析'\n"
                "- 将 web_search 结果作为参考信息，结合已有数据分析\n"
                "- web_search 结果用于补充新闻面、政策面、市场情绪等实时信息"
            ),
        )

        # 覆盖 smolagents 默认 prompt_templates，使用自定义 YAML 模板
        try:
            custom_templates = _load_code_agent_yaml()

            # 调试：检查 YAML 加载的 planning 内容
            yaml_planning = custom_templates.get("planning", {})
            logger.info("[TaskAgent] YAML planning 类型: %s, keys: %s", type(yaml_planning), list(yaml_planning.keys()) if isinstance(yaml_planning, dict) else "非字典")
            if isinstance(yaml_planning, dict):
                for k, v in yaml_planning.items():
                    logger.info("[TaskAgent] YAML planning['%s'] 前100字符: %s", k, repr(str(v)[:100]))

            agent.prompt_templates.update(custom_templates)
            logger.info("[TaskAgent] 已加载自定义 prompt_templates (YAML)")

            # 调试：检查 update 后的 planning 内容
            after_planning = agent.prompt_templates.get("planning", {})
            logger.info("[TaskAgent] update 后 planning 类型: %s", type(after_planning))
            if isinstance(after_planning, dict):
                for k, v in after_planning.items():
                    logger.info("[TaskAgent] update 后 planning['%s'] 前100字符: %s", k, repr(str(v)[:100]))
        except Exception as e:
            logger.warning("[TaskAgent] 自定义 prompt_templates 加载失败: %s，使用默认", e)

        return agent

    def _inject_tools_to_planning(self, agent, mcp_tool_list: list):
        """注入工具列表（含权重）到 smolagents 的 planning prompts。"""
        # 获取低权重工具集合
        low_weight_tools = set()
        try:
            from chain.store import query_low_weight_tools
            low_weight_tools = query_low_weight_tools()
        except Exception as e:
            logger.debug("[Inject] 获取工具权重失败: %s", e)

        tool_lines = []
        for t in mcp_tool_list:
            inputs = getattr(t, 'inputs', {}) or {}
            params = ', '.join(f"{p}: {i.get('type', 'string')}" for p, i in inputs.items()) if inputs else ''
            desc = (getattr(t, 'description', '') or '')[:80]
            tag = " ⚠️低权重" if t.name in low_weight_tools else ""
            tool_lines.append(f"  {t.name}({params}) — {desc}{tag}")
        tools_text = '\n'.join(tool_lines)

        low_weight_note = ""
        if low_weight_tools:
            low_weight_note = f"\n\n⚠️ 标记「低权重」的工具历史胜率低，优先用未标记的工具。"

        placeholder = "可用工具见系统提示中的「可用工具」部分。"
        tool_block = f"可用工具（通过 mcp(action='call', tool_name='...', args={{...}}) 调用）：\n{tools_text}{low_weight_note}"

        # 检查 prompt_templates 结构
        logger.info("[Inject] prompt_templates 类型: %s", type(agent.prompt_templates))
        logger.info("[Inject] prompt_templates 顶层 keys: %s", list(agent.prompt_templates.keys()) if isinstance(agent.prompt_templates, dict) else "非字典")

        planning = agent.prompt_templates.get("planning", {})
        logger.info("[Inject] planning 类型: %s, keys: %s", type(planning), list(planning.keys()) if isinstance(planning, dict) else "非字典")

        if not isinstance(planning, dict):
            logger.error("[Inject] planning 不是字典，无法注入工具！实际类型: %s", type(planning))
            # 尝试直接修改字符串模板
            if isinstance(agent.prompt_templates, dict) and isinstance(agent.prompt_templates.get("planning"), str):
                logger.info("[Inject] planning 是字符串，尝试直接替换...")
                planning_str = agent.prompt_templates["planning"]
                if placeholder in planning_str:
                    agent.prompt_templates["planning"] = planning_str.replace(placeholder, tool_block)
                    logger.info("[Inject] 字符串 planning 已注入 %d 个工具", len(mcp_tool_list))
                    return True
            return False

        injected = False
        for key in ("initial_plan", "update_plan_pre_messages", "update_plan_post_messages"):
            if key not in planning:
                logger.warning("[Inject] planning 中无 key '%s'，跳过", key)
                continue
            val = planning[key]
            logger.info("[Inject] planning['%s'] 类型: %s, 前80字符: %s", key, type(val), repr(str(val)[:80]))
            if not isinstance(val, str):
                logger.warning("[Inject] planning['%s'] 不是字符串，跳过", key)
                continue
            if placeholder not in val:
                logger.warning("[Inject] planning['%s'] 中未找到 placeholder", key)
                continue
            planning[key] = val.replace(placeholder, tool_block)
            injected = True
            logger.info("[TaskAgent] planning[%s] 已注入 %d 个工具", key, len(mcp_tool_list))
        if not injected:
            logger.error("[Inject] 工具注入失败！")
        return injected

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

        try:
            result = agent.run(task)
        except KeyboardInterrupt:
            logger.warning("[TaskAgent] 阶段 %d 被用户中断", phase_id)
            return f"[中断] 阶段 {phase_name} 被用户中断", {}
        except Exception as e:
            logger.error("[TaskAgent] 阶段 %d 执行异常: %s", phase_id, e)
            trace.record("phase_error", {"phase_id": phase_id, "error": str(e)})
            return f"[错误] 阶段 {phase_name} 执行失败: {e}", {}

        react_elapsed = round(time.time() - react_start, 2)

        trace.record("phase_done", {
            "phase_id": phase_id,
            "elapsed_seconds": react_elapsed,
            "result_preview": str(result)[:200],
        })

        return str(result) if result else ""

    # ── 主对话入口（统一多阶段）──────────────────────────────
    async def _chat_plan_graph(
        self,
        user_input: str,
        session_id: str,
        use_rag: bool,
    ) -> AgentResponse:
        """主对话流程：基于 StateGraph 的编排。

        用 graph.py 的 StateGraph 替代手搓 for 循环：
          - 状态持久化（Checkpointer）
          - 节点隔离（每个节点独立可测）
          - 条件路由（错误时走 fallback）
          - 流式输出（astream）
        """
        from graph import StateGraph, END
        from nodes import (
            AgentState, NodeContext,
            make_chat_node, make_plan_node,
            make_execute_node, make_finalize_node,
            route_after_chat, route_after_plan, route_after_execute,
        )

        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=type(self).__name__,
            session_id=session_id,
            user_input=user_input,
            metadata={"mode": "graph", "use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            # 创建运行时上下文
            ctx = NodeContext(
                llm=self.llm,
                memory=self.memory,
                retriever=self.retriever,
                skill_adapter=self.skill_adapter,
                system_prompt=self.system_prompt,
                memory_window_size=self.memory_window_size,
                max_tool_rounds=self.max_tool_rounds,
            )
            ctx.agent = self  # 传递 TaskAgent 实例，供节点调用 _build_code_agent 等方法
            # MCP 延迟初始化：chat_node 不需要 MCP，只在 plan/execute 路径才初始化

            # 构建图
            graph = StateGraph(AgentState)
            graph.add_node("chat", make_chat_node(ctx))
            graph.add_node("plan", make_plan_node(ctx))
            graph.add_node("execute", make_execute_node(ctx))
            graph.add_node("finalize", make_finalize_node(ctx))

            graph.set_entry_point("chat")
            graph.add_conditional_edges("chat", route_after_chat, {
                "plan": "plan",
                "finalize": "finalize",
            })
            graph.add_conditional_edges("plan", route_after_plan, {
                "execute": "execute",
                "finalize": "finalize",
            })
            graph.add_conditional_edges("execute", route_after_execute, {
                "plan": "plan",
                "finalize": "finalize",
            })
            graph.add_edge("finalize", END)

            # 编译（暂不启用 checkpointer，需要数据库连接池）
            compiled = graph.compile()

            # 执行
            initial_state = {
                "user_input": user_input,
                "session_id": session_id,
                "use_rag": use_rag,
                "_start_time": start_time,
                "_trace": trace,
            }

            result = await compiled.ainvoke(initial_state)

            final_output = result.get("final_output", {})
            response = AgentResponse(
                content=result.get("result_raw", ""),
                sources=result.get("sources", []),
                session_id=session_id,
                elapsed_seconds=result.get("elapsed", 0),
                metadata={
                    "trace_id": trace.trace_id,
                    "phase_count": len(result.get("phases", [])),
                    "phase_types": [p.get("type") for p in result.get("phases", [])],
                    "final_output": final_output,
                },
            )
            trace.finish(response=response.to_dict())
            return response

        except Exception as e:
            trace.fail(e)
            raise

    @staticmethod
    def _infer_var_type(value) -> str:
        """推断变量类型摘要（用于注入 phase_task）。"""
        if isinstance(value, dict):
            keys = list(value.keys())[:5]
            return f"dict({', '.join(keys)}{'...' if len(value) > 5 else ''})"
        if isinstance(value, (list, tuple)):
            return f"list[{len(value)}]"
        if isinstance(value, str):
            return f"str[{len(value)}字符]" if len(value) > 50 else repr(value[:50])
        if isinstance(value, (int, float, bool)):
            return repr(value)
        return type(value).__name__

    @staticmethod
    def _is_serializable(value) -> bool:
        """判断变量是否可序列化（过滤模块、函数、类等）。"""
        import types
        if isinstance(value, (types.ModuleType, types.FunctionType, types.MethodType, type)):
            return False
        try:
            import json
            json.dumps(value, default=str)
            return True
        except (TypeError, ValueError):
            return False

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
