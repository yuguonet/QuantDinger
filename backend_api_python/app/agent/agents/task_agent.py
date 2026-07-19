# -*- coding: utf-8 -*-
"""
任务型 Agent — 统一多阶段

流程：
  user input
    -> 负面反馈检测
    -> plan: LLM 选择技能、划分阶段（不选具体工具）
    -> 统一多阶段循环：
       - skill 阶段：加载技能指令 + 工具 → CodeAgent 执行
       - execute 阶段：通用工具 → CodeAgent 执行
       - direct 阶段：LLM 直接回答
    -> 规则 eval（结果非空 → passed）
    -> response

设计要点：
  - ToolProvider 统一管理所有工具（本地扫描 + skill 动态注册）
  - _plan() 只看技能列表，工具在执行阶段通过 ToolProvider 注入
  - 必选工具（list_tools/search_tools/format_result/web_search）通过 smolagents tools=[] 注入
  - 统一多阶段：所有任务走同一套循环，无单阶段/多阶段分支
  - eval 用规则判断，不调 LLM，省 token
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import time
from typing import Dict, List, Optional

import yaml



from agents.base import AgentBase, AgentResponse
from llm.base import ChatMessage, LLMBase
from memory.base import MemoryBase
from rag.retriever import Retriever
from smolagents import Tool as SmolToolBase
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

# 使用 app.agent logger（与 log.py 配置一致，确保日志写入文件）
try:
    from log import logger
except ImportError:
    logger = logging.getLogger(__name__)

# ── 模块级 TraceCollector 存储 ──
_collectors: Dict[str, "TraceCollector"] = {}

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
    """把 LLMBase 包装为 smolagents Model 接口。

    直接使用同步 OpenAI client 调用 LLM，避免 asyncio.run() 的
    事件循环开销和 nest_asyncio 依赖。
    """

    def __init__(self, llm: LLMBase):
        self._llm = llm
        self.model_id = getattr(llm, "model", "unknown")
        self._sync_client = None

    def _get_sync_client(self):
        """惰性创建同步 OpenAI client（复用 _llm 的连接配置）。"""
        if self._sync_client is not None:
            return self._sync_client
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai 未安装，请运行: pip install openai")
        client_kwargs = {
            "api_key": self._llm.api_key,
            "timeout": self._llm.timeout,
            "max_retries": self._llm.max_retries,
        }
        base_url = getattr(self._llm, "base_url", None)
        if base_url:
            client_kwargs["base_url"] = base_url
        self._sync_client = OpenAI(**client_kwargs)
        return self._sync_client

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
            client = self._get_sync_client()
            formatted_messages = [m.to_dict() for m in chat_messages]
            response = client.chat.completions.create(
                model=self._llm.model,
                messages=formatted_messages,
                temperature=self._llm.temperature,
                max_tokens=self._llm.max_tokens,
                top_p=self._llm.top_p,
            )
            choice = response.choices[0]
            content = choice.message.content or ""
            finish_reason = choice.finish_reason or "stop"
            tool_calls = []
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    })
                finish_reason = "tool_calls"
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
            from llm.base import LLMResponse
            resp = LLMResponse(
                content=content,
                tool_calls=tool_calls,
                model=self._llm.model,
                finish_reason=finish_reason,
                tokens_used=prompt_tokens + completion_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
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


def _load_skill_functions(skill_name: str, skill_adapter=None) -> list:
    """加载 skill 的 run.py 中的公开函数，包装为 CodeAgent 工具。

    约定：skill 目录名使用下划线（如 stock_evaluation），不用连字符。
    """
    import importlib
    module_name = skill_name.replace("-", "_")
    try:
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
        tool_provider=None,
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
        self._tool_provider = tool_provider

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
          - _plan() 只看技能列表，不看全量工具（工具在执行阶段通过 ToolProvider 注入）
          - 输出统一格式：task + selected_skill + step_budget
          - selected_skill → 加载该技能的 SKILL.md + 工具
          - 无技能 → CodeAgent + 通用工具

        Returns:
            {
              "task": str,
              "selected_skill": str | None,
              "selected_domain": str,
              "step_budget": int,
              "planning_interval": int,
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
                skills_desc.append(f"- {name}{weight_tag}: {desc}")
        skills_text = "\n".join(skills_desc) if skills_desc else "(无可用技能)"

        # 注入可用域和工具名列表，让规划器知道 CodeAgent 能调什么
        tools_hint = ""
        if self._tool_provider:
            # 可用域列表（子目录名）
            domains = sorted(set(
                d for d in self._tool_provider._domains.values() if d != "common"
            ))
            if domains:
                tools_hint += f"\n\n可用工具域：{', '.join(domains)}"
                tools_hint += "\n（domain 为空时仅加载通用工具，指定域时加载域+通用工具）"
            tool_names = self._tool_provider.get_tool_names(limit=30)
            if tool_names:
                tools_hint += f"\n\n可用工具（CodeAgent 可直接调用）：{', '.join(tool_names)}..."

        template = _load_plan_template()
        # completed_phases_text: 已完成阶段的摘要（用于多轮规划），首次调用为空
        prompt = template.format(
            skills_text=skills_text,
            user_input=user_input,
            entity_info=getattr(self, '_plan_entity_info', '') or '',
            task_type_info=getattr(self, '_plan_task_type_info', '') or '',
            rag_context=getattr(self, '_plan_rag_context', '') or '',
            history_context=getattr(self, '_plan_history_context', '') or '',
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

        # 从 plan 结果中提取选中的技能名
        selected_skill = plan.get("selected_skill") or plan.get("skill") or None
        # 校验技能名是否真实存在
        if selected_skill and self.skill_adapter:
            if not self.skill_adapter.get(selected_skill):
                logger.warning("[TaskAgent] plan 选择了不存在的技能 '%s'，忽略", selected_skill)
                selected_skill = None

        # 从 plan 结果中提取选中的域
        selected_domain = ""
        if not selected_skill:  # 技能模式不加载域工具
            selected_domain = plan.get("selected_domain") or plan.get("domain") or ""
            # 校验域是否真实存在
            if selected_domain and self._tool_provider:
                available_domains = set(
                    d for d in self._tool_provider._domains.values() if d != "common"
                )
                if selected_domain not in available_domains:
                    logger.warning("[TaskAgent] plan 选择了不存在的域 '%s'，忽略", selected_domain)
                    selected_domain = ""

        logger.info("[TaskAgent] plan: task=%s..., skill=%s, domain=%s, step_budget=%d",
                     task[:80], selected_skill, selected_domain or "(通用)", step_budget)
        trace.record("plan_result", {
            "route": "plan",
            "task": task,
            "selected_skill": selected_skill,
            "selected_domain": selected_domain,
            "step_budget": step_budget,
            "planning_interval": planning_interval,
        })

        return {
            "task": task,
            "selected_skill": selected_skill,
            "selected_domain": selected_domain,
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
        provider,
        skill_tools: list,
        planning_interval: int | None = None,
        phase_id: int = 0,
        domain: str = "",
    ):
        """构建 smolagents CodeAgent 实例。

        每个阶段独立构建，避免状态污染。
        planning_interval: None=不 replan，3~5=每 N 步 replan。
        phase_id: 阶段ID，用于缓存key前缀。
        domain: 领域名，用于过滤工具。

        工具架构：
          - 必选工具（list_tools/search_tools/format_result/web_search）→ smolagents tools=[]
          - 领域工具 + 通用工具 → executor.custom_tools（通过 ToolProvider 注入）
          - 技能工具 → executor.custom_tools
          - 全量工具 schema → planning YAML {{tool_list}}（供 smolagents 内部 planning 选工具）
        """
        from smolagents import CodeAgent as SmolCodeAgent
        from smolagents.local_python_executor import LocalPythonExecutor
        from smolagents.memory import ActionStep

        # ── 工具函数：按 domain 过滤 + 技能工具 ──
        if domain:
            # 指定域：域工具 + 通用工具
            allowed = set(provider.list_by_domain("common") + provider.list_by_domain(domain))
            tool_functions = {n: f for n, f in provider.get_functions().items() if n in allowed}
            logger.info("[TaskAgent] domain='%s'，加载 %d 个工具（通用+%s）", domain, len(tool_functions), domain)
        else:
            # 无域：仅通用工具
            allowed = set(provider.list_by_domain("common"))
            tool_functions = {n: f for n, f in provider.get_functions().items() if n in allowed}
            logger.info("[TaskAgent] 无域，加载 %d 个通用工具", len(tool_functions))

        # 技能工具（私有，不和 tools/ 通用）
        for st in skill_tools:
            sname = getattr(st, "name", "unknown")
            tool_functions[sname] = st

        # final_answer
        def _final_answer(answer=None, **kwargs):
            return answer if answer is not None else kwargs

        # executor
        executor = LocalPythonExecutor(
            additional_authorized_imports=[
                "json", "datetime", "math", "re", "collections", "itertools",
                "concurrent.futures", "queue", "time", "unicodedata", "stat",
                "statistics", "random", "os", "sys", "pathlib", "importlib",
                "skills", "skills.*",
            ],
            additional_functions={"final_answer": _final_answer},
        )
        executor.custom_tools = tool_functions
        logger.info("[TaskAgent] executor 已注入 %d 个工具函数", len(tool_functions))

        # ── 必选工具：注册为 smolagents Tool，放入 tools=[] ──
        # smolagents 自动在 system prompt 中描述这些工具，LLM 天然知道可以用
        class _SearchToolsTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "search_tools"
            description = "按关键词搜索可用工具。返回匹配的工具名、参数和描述。用于不确定工具名时快速定位。"
            output_type = "string"
            inputs = {
                "query": {"type": "string", "description": "搜索关键词（如 资金流、K线、选股）"},
                "domain": {"type": "string", "description": "领域过滤（可选）", "nullable": True},
            }
            def forward(self, query: str = "", domain: str = "", **kwargs):
                return provider.search_tools(query, domain)

        class _ListToolsTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "list_tools"
            description = "列出所有可用工具。可按领域过滤。用于了解当前有哪些工具可用。"
            output_type = "string"
            inputs = {
                "domain": {"type": "string", "description": "领域名称（可选，空=全部）", "nullable": True},
            }
            def forward(self, domain: str = "", **kwargs):
                return provider.list_tools(domain)

        class _FormatResultTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "format_result"
            description = "把任意格式的数据转换为 LLM 容易理解的字符串。用于格式化工具返回的结果。"
            output_type = "string"
            inputs = {
                "result": {"type": "object", "description": "任意格式的数据"},
                "max_depth": {"type": "integer", "description": "最大递归深度", "nullable": True},
                "max_items": {"type": "integer", "description": "最多显示的项数", "nullable": True},
            }
            def forward(self, result=None, max_depth: int = 3, max_items: int = 20, **kwargs):
                from tools.format_utils import format_result
                return format_result(result, max_depth, max_items)

        class _WebSearchTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "web_search"
            description = "联网搜索最新信息。用于补充新闻面、政策面、市场情绪等实时数据。"
            output_type = "object"
            inputs = {
                "query": {"type": "string", "description": "搜索关键词"},
                "count": {"type": "integer", "description": "结果数量", "nullable": True},
                "freshness": {"type": "string", "description": "时效性过滤", "nullable": True},
            }
            def forward(self, query: str = "", count: int = 8, freshness: str = "", **kwargs):
                from tools.web_search_tools import web_search
                return web_search(query, count, freshness)

        smol_tools = [_SearchToolsTool(), _ListToolsTool(), _FormatResultTool(), _WebSearchTool()]

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
            tools=smol_tools,
            model=model,
            max_steps=self.max_tool_rounds,
            executor=executor,
            planning_interval=planning_interval,
            step_callbacks=[_truncate_observations],
            instructions=(
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
            import copy
            custom_templates = copy.deepcopy(custom_templates)

            # 替换 {{tool_list}} 占位符：注入 domain 相关工具 schema，供 smolagents 内部 planning 选工具
            planning = custom_templates.get("planning", {})
            if isinstance(planning, dict) and provider:
                if domain:
                    allowed_names = set(provider.list_by_domain("common") + provider.list_by_domain(domain))
                    tools_text = provider.get_schemas_text(names_filter=allowed_names)
                else:
                    tools_text = provider.get_schemas_text(names_filter=set(provider.list_by_domain("common")))
                for key in ("initial_plan", "update_plan_pre_messages", "update_plan_post_messages"):
                    val = planning.get(key, "")
                    if isinstance(val, str) and "{{tool_list}}" in val:
                        planning[key] = val.replace("{{tool_list}}", tools_text)
                        logger.info("[TaskAgent] YAML planning['%s'] 已注入 %d 个工具 schema", key, len(provider))

            agent.prompt_templates.update(custom_templates)
            logger.info("[TaskAgent] 已加载自定义 prompt_templates (YAML)")
        except Exception as e:
            logger.warning("[TaskAgent] 自定义 prompt_templates 加载失败: %s，使用默认", e)

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
            from resolvers.stock import StockResolver
            ctx = NodeContext(
                llm=self.llm,
                memory=self.memory,
                retriever=self.retriever,
                skill_adapter=self.skill_adapter,
                system_prompt=self.system_prompt,
                memory_window_size=self.memory_window_size,
                max_tool_rounds=self.max_tool_rounds,
                entity_resolver=StockResolver(),
            )
            ctx.agent = self  # 传递 TaskAgent 实例，供节点调用 _build_code_agent 等方法

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
