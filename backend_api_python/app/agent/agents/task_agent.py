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
from utils.prescan import prescan_skill_funcs, prescan_tools  # 预扫（2026-09-12 Q4）
from tools.breaker import ToolCircuitBreaker  # 工具失败熔断（2026-09-12）
from tools.guided_executor import GuidedPythonExecutor  # 幻觉调用纠正（2026-09-12）
from tools.resilient_parse import apply as _apply_resilient_parse  # 代码提取加固（2026-09-12）
_apply_resilient_parse()
from rag.retriever import Retriever
from smolagents import Tool as SmolToolBase
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

# 使用 app.agent logger（与 log.py 配置一致，确保日志写入文件）
try:
    from log import logger
except ImportError:
    logger = logging.getLogger(__name__)



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

# Phase 契约常量（2026-09-12 B 阶段接线）：
# 外部 planner 产出 phases[] 契约，execute_node 降为单 phase 轮询执行
# （route_after_execute 条件边形成循环，全部完成才进 finalize）。
PLAN_MAX_PHASES = 5          # 单次 plan 的阶段数上限（超出截断）
PLAN_PHASE_MAX_RETRIES = 1   # 单阶段默认重试上限（phase.max_retries 可覆盖，钳制 [0,3]）


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


_VALID_ON_FAIL = {"retry", "replan", "abort"}


def _normalize_phases(raw, available_names: set) -> list:
    """把 _plan 输出的 phases[] 规格化为契约结构（防御性解析，2026-09-12 B 阶段）。

    设计点：
      - id 重排为 1..n；总数为 PLAN_MAX_PHASES 截断
      - tools 只保留 provider 中真实存在的名字（LLM 幻觉名丢弃并记入 tools_dropped）
      - on_fail ∈ {retry,replan,abort}（默认 retry）；max_retries 钳制 [0,3]
      - goal 为空的条目跳过；非 list / 全空 → 返回 []（调用方回退单段执行旧路径）
    易错点：
      - 纯函数（不 import provider），名称集合由调用方传入，便于单测
      - 每个 phase 输出必带 tools 键（可能为空 list）：执行侧语义为
        "非空 = 严格白名单；空/None = 回退 domain 逻辑"
    """
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw[:PLAN_MAX_PHASES]:
        if not isinstance(p, dict):
            continue
        goal = str(p.get("goal") or "").strip()
        if not goal:
            continue
        tools_raw = p.get("tools")
        if isinstance(tools_raw, str):
            tools_raw = [tools_raw]
        tools, dropped = [], []
        if isinstance(tools_raw, list):
            for t in tools_raw:
                t = str(t).strip()
                if not t:
                    continue
                if t in available_names:
                    if t not in tools:
                        tools.append(t)
                else:
                    dropped.append(t)
        on_fail = str(p.get("on_fail") or "retry").strip().lower()
        if on_fail not in _VALID_ON_FAIL:
            on_fail = "retry"
        try:
            mr = int(p.get("max_retries", PLAN_PHASE_MAX_RETRIES))
        except (TypeError, ValueError):
            mr = PLAN_PHASE_MAX_RETRIES
        mr = max(0, min(3, mr))
        acc = p.get("acceptance") or []
        if isinstance(acc, str):
            acc = [acc]
        if not isinstance(acc, list):
            acc = [str(acc)]
        out.append({
            "id": len(out) + 1,
            "name": (str(p.get("name") or "").strip() or f"阶段{len(out) + 1}")[:40],
            "goal": goal[:800],
            "tools": tools,
            "tools_dropped": dropped,
            "deliverable": p.get("deliverable") or "",
            "acceptance": [str(a)[:300] for a in acc][:8],
            "on_fail": on_fail,
            "max_retries": mr,
        })
    return out


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
        # 期望的同步客户端超时（2026-09-12）：_set_llm_timeout 在客户端未创建时
        # 记录于此，惰性创建时补挂——修复"复用 Agent 重试时 180s 超时丢失"。
        self._desired_timeout = None

    def close(self):
        """关闭同步 OpenAI 客户端，释放 httpx 连接池。

        每次构建 CodeAgent 都会新建 _LLMAdapter（随之新建同步客户端），
        不关闭会在长运行进程中持续泄漏连接（审计 P2）。在 execute_node 收尾调用。
        """
        if self._sync_client is not None:
            try:
                self._sync_client.close()
            except Exception:
                pass
            self._sync_client = None

    def _get_sync_client(self):
        """惰性创建同步 OpenAI client（复用 _llm 的连接配置）。"""
        if self._sync_client is not None:
            return self._sync_client
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai 未安装，请运行: pip install openai")
        # 远端网关（如 g2claw）偶发 5xx 时 SDK 内置重试只有 1 次，快速抖动期不够用；
        # 本地 llama 稳定无需多试。允许用 LLM_MAX_RETRIES 按部署环境调整。
        import os as _os
        try:
            max_retries = int(_os.getenv("LLM_MAX_RETRIES", str(self._llm.max_retries)))
        except ValueError:
            max_retries = self._llm.max_retries
        client_kwargs = {
            "api_key": self._llm.api_key,
            "timeout": self._llm.timeout,
            "max_retries": max_retries,
        }
        base_url = getattr(self._llm, "base_url", None)
        if base_url:
            client_kwargs["base_url"] = base_url
        self._sync_client = OpenAI(**client_kwargs)
        # 补挂期望超时（2026-09-12）：跳过则执行期沿用工厂默认超时
        if getattr(self, "_desired_timeout", None):
            try:
                self._sync_client.timeout = self._desired_timeout
            except Exception:
                pass
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

        # 非标 role 转换（与 smolagents 官方 get_clean_message_list 的
        # tool_role_conversions 完全一致）：CodeAgent memory 里的 ActionStep 会产出
        # role="tool-call"/"tool-response"，OpenAI 协议只认 user/assistant/system/tool，
        # 直接透传会被远端网关拒 500（本地 llama.cpp 宽容解析所以不报错——
        # 这就是"本地正常、远端 500"的根因，2026-09-10 实证定位）。
        _ROLE_CONVERSIONS = {
            "tool-call": "assistant",
            "tool-response": "user",
        }

        chat_messages = []
        for m in messages:
            if isinstance(m, dict):
                role, content = m.get("role", "user"), m.get("content", "")
            else:
                role = getattr(m, "role", "user")
                content = getattr(m, "content", "")
            # role 归一化：smolagents 的 role 是 MessageRole 枚举（str 子类），但 str() 产出
            # "MessageRole.TOOL_CALL" 而非 "tool-call"——必须先取 .value 再查表，
            # 否则转换永远 miss，非标 role 原样透传（2026-09-10 二次定位）。
            if not isinstance(role, str):
                role = getattr(role, "value", str(role))
            role = _ROLE_CONVERSIONS.get(role, role)
            chat_messages.append(ChatMessage(role=role, content=content))

        # 5xx/连接错误退避重试（有界循环，不递归）：
        # 旧实现 except 内递归调用 self.generate()，网关持续 5xx 时每次重试失败
        # 都会再次进入同一 except → 无限递归（2026-09-10 22:09 日志实证）。
        # 现改为固定次数的退避循环：SDK 内置重试（max_retries，默认 1）负责秒级抖动，
        # 本循环负责"网关重启中"的分钟级窗口，总尝试 = (LLM_MAX_RETRIES+1) × LLM_RETRY_ROUNDS。
        import time as _time
        import os as _os

        def _is_retriable(err: Exception) -> bool:
            t = str(err).lower()
            return ("500" in t or "502" in t or "503" in t or "429" in t
                    or "connection" in t or "timeout" in t)

        try:
            client = self._get_sync_client()
            formatted_messages = [m.to_dict() for m in chat_messages]
            response = None
            for attempt in range(int(_os.getenv("LLM_RETRY_ROUNDS", "3"))):
                try:
                    response = client.chat.completions.create(
                        model=self._llm.model,
                        messages=formatted_messages,
                        temperature=self._llm.temperature,
                        max_tokens=self._llm.max_tokens,
                        top_p=self._llm.top_p,
                    )
                    break  # 成功即退出重试循环
                except KeyboardInterrupt:
                    raise
                except Exception as retry_err:
                    rounds_left = int(_os.getenv("LLM_RETRY_ROUNDS", "3")) - attempt - 1
                    if rounds_left <= 0 or not _is_retriable(retry_err):
                        raise
                    # 指数退避：2s → 4s → 8s（封顶 15s）
                    delay = min(2.0 * (2 ** attempt), 15.0)
                    logger.warning(
                        "[TaskAgent] LLM 网关抖动（%s），%.0fs 后第 %d 次重试（剩 %d 轮）",
                        str(retry_err)[:120], delay, attempt + 1, rounds_left,
                    )
                    _time.sleep(delay)
            if response is None:
                raise RuntimeError("LLM 调用失败：重试轮次耗尽且无响应")
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
            logger.error("[TaskAgent] LLM 调用失败（重试已耗尽）: %s", e)
            raise

        if resp.finish_reason == "error":
            raise RuntimeError(f"LLM 调用失败: {resp.content}")

        # 规范化：markdown 代码块 → <code>...</code>
        # ???????2026-09-12??length=?????????????????
        # ?????????????????????????????????
        if getattr(resp, "finish_reason", "") == "length":
            raise RuntimeError(
                "????????????????????????????????????"
                "????????????????? CODE_AGENT_MAX_TOKENS ?????"
            )
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
            _avail = self._loader.list_resources(self._skill_name)
            return (f"[错误] 未指定资源路径。用法: read_skill_resource(relative_path='references/xxx.md')；"
                    f"可用资源: {_avail}")
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
        _no_arg_hint = "（无参数，直接调用）" if not param_parts else ""
        self.description = f"{func.__name__}({params_str}) — {func_desc}{_no_arg_hint}"

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


def _list_skill_func_names(skill_name: str) -> set:
    """列出技能 run.py 的公开函数名集合（发现规则与 _load_skill_functions 一致）。

    2026-09-12 阶段清单升级：_normalize_phases 的合法工具名集合要并入这些名字，
    planner 在 phases[].tools 里点名技能函数（如 pre_screen / deep_analyze）时不被丢弃。
    """
    import importlib
    module_name = skill_name.replace("-", "_")
    try:
        mod = importlib.import_module(f"skills.{module_name}.run")
    except Exception:
        return set()
    out = set()
    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or inspect.isclass(obj):
            continue
        if getattr(obj, "__module__", "") != mod.__name__:
            continue
        out.add(attr_name)
    return out


def _load_skill_stages(skill_name: str, skill_adapter=None) -> list:
    """解析 SKILL.md 中可选的「## stages」段（阶段清单，2026-09-12）。

    约定：`## stages` 小节内放置 YAML 列表，每项字段 name / goal / tools /
    deliverable / acceptance（均容错，缺省即空）。解析失败或未定义 → []
    （向后兼容：旧技能不写该段则行为完全不变）。
    """
    if not skill_adapter:
        return []
    try:
        body = skill_adapter.load_body(skill_name)
    except Exception:
        return []
    if not body:
        return []
    m = re.search(r"^#{1,3}\s*stages\s*$", body, re.MULTILINE | re.IGNORECASE)
    if not m:
        return []
    rest = body[m.end():]
    m2 = re.search(r"^#{1,2}\s+", rest, re.MULTILINE)
    section = rest[:m2.start()] if m2 else rest
    section = re.sub(r"^\s*```[a-zA-Z]*\s*$", "", section, flags=re.MULTILINE).strip()
    if not section:
        return []
    try:
        data = yaml.safe_load(section)
    except Exception as e:
        logger.warning("[Plan] 技能 %s 的 stages 段解析失败: %s", skill_name, e)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            out.append(item)
    return out


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
        plan_ctx=None,
    ) -> dict:
        """plan_ctx: 本次请求的 NodeContext。

        plan 阶段的实体/RAG/历史上下文由 plan_node 写在 ctx 上（每请求独立，并发安全）。
        兼容旧调用方：plan_ctx=None 时回退读实例属性（单线程 CLI 场景）。
        """
        src = plan_ctx if plan_ctx is not None else self
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
                # 预扫（2026-09-12 Q4）：静态提取 run.py 函数签名+文档 → 规划器直接按
                # 真实接口编排（替代模型试错）；阶段流并入（v1 的 _load_skill_stages 已覆盖）
                _funcs = prescan_skill_funcs(name.replace("-", "_"))
                for _f in _funcs[:8]:
                    _line = "    · " + _f["sig"] + (" — " + _f["doc"] if _f["doc"] else "")
                    skills_desc.append(_line[:150])
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
            # 上限 30→60（2026-09-12 B 阶段）：phase.tools 白名单要求 planner 看到足量
            # 工具名——截断会让清单外的真实工具被误判为不存在；名称短，token 开销可控。
            # 预扫（2026-09-12 Q4）：签名清单替代裸名列表——planner 直接写对参数
            _sig_text = prescan_tools(self._tool_provider, limit=60)
            if _sig_text:
                tools_hint += "\n\n可用工具（CodeAgent 可直接调用，含参数签名）：\n" + _sig_text
            # 能力视图（2026-09-12 Q5）：准入数据能力单独成段——能力清单是外部 planner
            # 做细致分析的原料；段内排序稳定，planner 可直接把能力名写进 phases[].tools
            try:
                _cap_names = sorted(
                    n for n in self._tool_provider.get_tool_names()
                    if self._tool_provider.get_domain(n) == "quant"
                )
                if _cap_names:
                    _cap_text = prescan_tools(self._tool_provider, limit=0, per_item=140)
                    # prescan_tools 不支持按域过滤——手动组装 quant 段
                    _cap_lines = []
                    for _n in _cap_names:
                        _fn = self._tool_provider.get(_n)
                        _sig = ""
                        try:
                            import inspect as _ins
                            _ps = [p2.name for p2 in _ins.signature(_fn).parameters.values()
                                   if not p2.name.startswith("_")]
                            _sig = "%s(%s)" % (_n, ", ".join(_ps))
                        except Exception:
                            _sig = _n + "(…)"
                        _doc = (inspect.getdoc(_fn) or "").strip().split("\n")[0][:100]
                        _cap_lines.append("  %s — %s" % (_sig, _doc))
                    tools_hint += ("\n\n数据能力（domain=quant，准入数据接口，可直接调用）：\n"
                                   + "\n".join(_cap_lines))
            except Exception as _e:
                logger.debug("[Plan] 能力视图注入跳过: %s", _e)

        template = _load_plan_template()
        # completed_phases_text: 已完成阶段的摘要（用于多轮规划），首次调用为空
        prompt = template.format(
            skills_text=skills_text,
            user_input=user_input,
            entity_info=getattr(src, '_plan_entity_info', '') or '',
            task_type_info=getattr(src, '_plan_task_type_info', '') or '',
            rag_context=getattr(src, '_plan_rag_context', '') or '',
            history_context=getattr(src, '_plan_history_context', '') or '',
            completed_phases_text=getattr(src, '_completed_phases_text', '') or '',
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
        # step_budget 钳制：LLM 输出不可信，范围 [1,20] + int 强转。
        # 旧实现仅 `or 10` 兜底：字符串 "10" 在 smolagents 步数比较时会炸；
        # 无上限时 LLM 可自定 50 步，AGENT_MAX_STEPS 环境变量形同虚设（审计 P1-6）。
        try:
            step_budget = int(plan.get("step_budget") or 10)
        except (TypeError, ValueError):
            step_budget = 10
        step_budget = max(1, min(20, step_budget))
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

        # ── Phase 契约（2026-09-12 B 阶段）：外部 planner 产出 phases[] 驱动单 phase 轮询 ──
        # 无 phases 输出（简单任务/旧模板）→ []，execute 走单段旧路径，行为兼容。
        available_names = set()
        if self._tool_provider:
            available_names = set(self._tool_provider.get_tool_names())
        if selected_skill:
            # 技能工具名加入合法集（2026-09-12 阶段清单升级）：phases[].tools 可直接
            # 点名技能函数（如 pre_screen / deep_analyze）而不被当作幻觉名丢弃
            available_names |= _list_skill_func_names(selected_skill)
            available_names |= {"read_skill_resource", "read_skill_section"}
        raw_phases = plan.get("phases")
        phases = _normalize_phases(raw_phases or [], available_names)
        if raw_phases and not phases:
            logger.warning("[TaskAgent] phases 输出未通过规格化（%r），回退单段执行",
                           str(raw_phases)[:120])
        if phases:
            logger.info("[TaskAgent] plan: %d 个阶段 %s", len(phases),
                        ", ".join(f"#{p['id']}{p['name']}[{len(p['tools'])}工具]" for p in phases))
            dropped_total = sum(len(p["tools_dropped"]) for p in phases)
            if dropped_total:
                logger.warning("[TaskAgent] phase 白名单丢弃不存在的工具名 %d 个: %s",
                               dropped_total, [t for p in phases for t in p["tools_dropped"]][:10])

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
        if phases:
            # 契约全文入 trace（供晋升闭环聚合与 T+N 对账）
            trace.record("plan_phases", {"phases": phases})

        return {
            "task": task,
            "selected_skill": selected_skill,
            "selected_domain": selected_domain,
            "step_budget": step_budget,
            "planning_interval": planning_interval,
            "phases": phases,
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

        return await self._chat_plan_graph(user_input, session_id, use_rag, event_cb=getattr(self, "_current_event_cb", None))

    # ── 定时任务拦截 ──────────────────────────────────────

    @staticmethod
    def _try_intercept_cron(user_input: str, session_id: str) -> Optional[AgentResponse]:
        """检测调度意图，匹配到则直接创建定时任务并返回，跳过 agent 流程。

        匹配模式：
          - 开盘期间/交易时间/盘中,每N分钟<动作>
          - 每N分钟/小时/天<动作>
          - 每天/每周X/每月X号 HH:MM<动作>
          - HH:MM<动作> / 明天HH:MM<动作>
          - 提醒我<动作>

        Returns:
            AgentResponse if intercepted, None if not a cron request.
        """
        import re
        from datetime import datetime, timedelta, timezone

        text = user_input.strip()
        if not text or len(text) > 200:
            return None

        TZ_CN = timezone(timedelta(hours=8))

        # ── 模式匹配 ──────────────────────────────────────
        at = ""
        cron_expr = ""
        content = ""
        is_cron = False

        # 模式1: 开盘期间/交易时间/盘中,每N分钟<动作>
        m = re.match(r'(?:开盘期间|交易时间|盘中)\s*,?\s*每(\d+)分钟\s*(.+)', text)
        if m:
            step = m.group(1)
            content = m.group(2).strip()
            at = f"开盘期间,每{step}分钟"
            is_cron = True

        # 模式2: 每N分钟<动作>
        if not is_cron:
            m = re.match(r'每(\d+)分钟\s*(.+)', text)
            if m:
                step = m.group(1)
                content = m.group(2).strip()
                at = f"每天"
                # 用 cron_expr 而非 at
                at = ""
                is_cron = True
                cron_expr = f"*/{step} * * * *"

        # 模式3: 每N小时<动作>
        if not is_cron:
            m = re.match(r'每(\d+)小时\s*(.+)', text)
            if m:
                step = m.group(1)
                content = m.group(2).strip()
                cron_expr = f"0 */{step} * * *"
                at = ""
                is_cron = True

        # 模式4: 每天HH:MM<动作>
        if not is_cron:
            m = re.match(r'每天\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                h, mi = m.group(1), m.group(2)
                content = m.group(3).strip()
                at = f"每天 {h}:{mi}"
                is_cron = True

        # 模式5: 每周X HH:MM<动作>
        if not is_cron:
            m = re.match(r'每(?:周|星期)([一二三四五六日天])\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(4).strip()
                at = f"每{m.group(1)} {m.group(2)}:{m.group(3)}"
                is_cron = True

        # 模式6: 每月X号 HH:MM<动作>
        if not is_cron:
            m = re.match(r'每月(\d{1,2})[号日]\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(4).strip()
                at = f"每月{m.group(1)}号 {m.group(2)}:{m.group(3)}"
                is_cron = True

        # 模式7: HH:MM<动作>（一次性）
        if not is_cron:
            m = re.match(r'(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(3).strip()
                at = f"{m.group(1)}:{m.group(2)}"
                is_cron = True

        # 模式8: 明天HH:MM<动作>（一次性）
        if not is_cron:
            m = re.match(r'明天\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(3).strip()
                at = f"tomorrow {m.group(1)}:{m.group(2)}"
                is_cron = True

        # 模式9: N分钟/小时/秒以后<动作>（一次性）
        if not is_cron:
            m = re.match(r'(\d+)\s*(?:分钟|min)\s*(?:以后|后|之后)\s*(.+)', text)
            if m:
                delay = int(m.group(1))
                content = m.group(2).strip()
                now = datetime.now(TZ_CN)
                target = now + timedelta(minutes=delay)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True
        if not is_cron:
            m = re.match(r'(\d+)\s*(?:小时|hour)\s*(?:以后|后|之后)\s*(.+)', text)
            if m:
                delay = int(m.group(1))
                content = m.group(2).strip()
                now = datetime.now(TZ_CN)
                target = now + timedelta(hours=delay)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True
        if not is_cron:
            m = re.match(r'(\d+)\s*(?:秒|sec)\s*(?:以后|后|之后)\s*(.+)', text)
            if m:
                delay = int(m.group(1))
                content = m.group(2).strip()
                now = datetime.now(TZ_CN)
                target = now + timedelta(seconds=delay)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True

        # 模式10: 提醒我<动作>（默认1分钟后执行）
        if not is_cron:
            m = re.match(r'提醒我\s*(.+)', text)
            if m:
                content = f"提醒：{m.group(1).strip()}"
                now = datetime.now(TZ_CN)
                target = now + timedelta(minutes=1)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True

        if not is_cron or not content:
            return None

        # ── 创建定时任务 ──────────────────────────────────
        try:
            from cron.cron_tools import create_cron_job

            # 提取任务名（取前20字）
            name = content[:20]

            result = create_cron_job(
                name=name,
                prompt=content,
                cron_expr=cron_expr,
                at=at,
                one_shot=False,
            )

            if "error" in result:
                return None  # 创建失败，走正常流程

            job_id = result.get("job_id")
            next_run = result.get("next_run", "")
            one_shot = result.get("one_shot", False)

            if one_shot:
                reply = f"✅ 已创建一次性任务 #{job_id}，将在 {next_run} 执行：{content}"
            else:
                cron = result.get("cron_expr", "")
                reply = f"✅ 已创建定时任务 #{job_id}（{cron}），下次执行：{next_run}\n执行内容：{content}"

            return AgentResponse(
                content=reply,
                session_id=session_id,
                metadata={"intercepted": "cron", "job_id": job_id},
            )

        except Exception as e:
            logger.warning("[TaskAgent] Cron 拦截失败: %s", e)
            return None  # 失败走正常流程

    # ── 阶段执行与评估 ──────────────────────────────────────

    def _build_code_agent(
        self,
        model,
        provider,
        skill_tools: list,
        planning_interval: int | None = None,
        phase_id: int = 0,
        domain: str = "",
        tools: list | None = None,
        step_event_cb=None,
    ):
        """构建 smolagents CodeAgent 实例。

        每个阶段独立构建，避免状态污染。
        planning_interval: None=不 replan，3~5=每 N 步 replan。
        phase_id: 阶段ID（trace 与日志标记）。
        domain: 领域名，用于过滤工具。
        tools: phase 工具白名单（2026-09-12 B 阶段）。非空→只注入白名单内的 provider 工具；
            空或 None→回退 domain 逻辑（调用方传 domain="" 时回退为仅通用工具）。

        工具架构：
          - 必选工具（list_tools/search_tools/format_result/web_search）→ smolagents tools=[]
          - 领域工具 + 通用工具 → executor.custom_tools（通过 ToolProvider 注入）
          - 技能工具 → executor.custom_tools
          - phase 白名单（tools 非空）→ 只注入白名单内的 provider 工具
          - 全量工具 schema → planning YAML {{tool_list}}（供 smolagents 内部 planning 选工具）
        """
        from smolagents import CodeAgent as SmolCodeAgent
        from smolagents.local_python_executor import LocalPythonExecutor
        from smolagents.memory import ActionStep

        # ── 工具函数：phase 白名单 / domain 过滤 + 技能工具 ──
        # phase 白名单（2026-09-12 B 阶段，审计自 qd_traces）：tools 非空时只注入白名单工具，
        # 补救通道 = smol_tools 的 search_tools/list_tools（只读探查，不产生调用能力）。
        if tools:
            allowed = set(str(t) for t in tools)
            tool_functions = {n: f for n, f in provider.get_functions().items() if n in allowed}
            logger.info("[TaskAgent] phase 白名单：加载 %d 个工具 %s", len(tool_functions),
                        sorted(tool_functions)[:12])
        elif domain:
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
        # 幻觉调用纠正（2026-09-12）：执行器错误信息带可用工具清单与修复指令
        # （Forbidden function evaluation → [幻觉调用拦截]+可用清单+二选一处理指引）
        # allowed 名单延迟解析：__call__ 出错时从 static_tools 动态收集（构造期
        # smol_tools 尚未定义——run5 教训）
        executor = GuidedPythonExecutor(
            # 生成代码可 import 的模块白名单（最小授权，2026-09-11 收紧，审计 P2）：
            # - os/sys/pathlib/importlib 移除：web_search 等工具返回的不可信文本可注入指令，
            #   让生成代码读文件/环境变量/任意模块。实证依据：agent_runs.jsonl 全量 1.4MB
            #   轨迹零命中 import os/sys/importlib/pathlib；生成代码的约定范式是"调用注入的
            #   工具函数"而非直接 I/O。skills 内部 import 发生在宿主进程，不受本表约束。
            # - stat 保留：A 股工具链生成代码常用 st.* 判别文件属性（若后续零使用可再收）。
            # - 收紧后若出现 "is not authorized" 类执行错误：先核对轨迹确认真实需求，
            #   按最小需要加回，禁止整表回滚。
            additional_authorized_imports=[
                "json", "datetime", "math", "re", "collections", "itertools",
                "concurrent.futures", "queue", "time", "unicodedata", "stat",
                "statistics", "random",
                "skills", "skills.*",
            ],
            additional_functions={
            "final_answer": _final_answer,
            # ?????????2026-09-12??smolagents ??????? repr?
            # ??? repr(x) ? Forbidden??repr ??????/??????
            # ?????????????run6 ?????
            "repr": repr,
        },
        )
        executor.custom_tools = tool_functions

        # 过程事件钩子（2026-09-11 SSE 改造）：smolagents 在每个 step 结束时调用
        # callback(memory_step, agent=self)。把 ActionStep 的代码动作/工具产出转成
        # 轻量事件 dict 交给 cb（SSE 层入队）。cb 必须绝不抛异常、不阻塞执行——
        # 钩子内部捕获一切异常，cb 异常也吞掉（流式通道故障不能影响主任务）。
        def _make_step_event_hook(cb):
            if cb is None:
                return None

            def _hook(memory_step, agent):
                try:
                    if type(memory_step).__name__ != "ActionStep":
                        return
                    ev = {
                        "kind": "action_step",
                        "step_number": getattr(memory_step, "step_number", None),
                        "is_final": bool(getattr(memory_step, "is_final_answer", False)),
                    }
                    code_action = getattr(memory_step, "code_action", None)
                    if code_action:
                        ev["code_action"] = str(code_action)[:600]
                    obs = getattr(memory_step, "observations", None)
                    if obs:
                        ev["observations"] = str(obs)[:1200]
                    err = getattr(memory_step, "error", None)
                    if err:
                        ev["error"] = str(err)[:300]
                    # 从 CodeAgent 的工具调用日志提取工具名（若有）
                    tools = []
                    tc = getattr(memory_step, "tool_calls", None)
                    if tc:
                        try:
                            for t in tc:
                                name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                                if name:
                                    tools.append(str(name))
                        except Exception:
                            pass
                    if tools:
                        ev["tools"] = tools
                    if ev.get("code_action") or ev.get("observations") or ev.get("error") or tools:
                        try:
                            cb(ev)
                        except Exception:
                            pass
                except Exception:
                    pass

            return _hook

        _evt_hook = _make_step_event_hook(step_event_cb)
        # 注意：_truncate_observations 定义在本函数后段（SmolCodeAgent 构造前），
        # 此处不能引用——回调列表在构造参数处内联组装。
        # 工具失败熔断（2026-09-12）：坏工具/坏数据源连续失败 ≥2 次 → 短路，
        # 防止执行器反复重试同一坑烧爆步数（run3/run4 实证：资金流接口宕机时
        # 每轮 5~10 步耗在注定失败的调用上）。断路器按 agent 实例隔离，
        # 阶段重试复用同一实例 → 熔断状态延续。
        if not hasattr(self, "_tool_breaker"):
            self._tool_breaker = ToolCircuitBreaker(threshold=2)
        tool_functions = {
            name: self._tool_breaker.wrap(name, fn)
            for name, fn in tool_functions.items()
        }

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
            description = "列出所有可用工具（默认全部，可按领域过滤）。用于了解当前有哪些工具可用。"
            output_type = "string"
            inputs = {
                "domain": {"type": "string", "description": "领域名称（可选，空=全部）", "nullable": True},
            }
            def forward(self, domain: str = "", **kwargs):
                # phase 白名单模式（2026-09-12）：只展示本阶段可见工具
                # （E2E 实证：列出白名单外工具 → 执行器反复试探"搜到但调不动"）
                if tools is not None:
                    if not tools:
                        return "本阶段无数据工具（仅计算能力与 search_tools 查询）。"
                    lines = [f"本阶段可用工具 ({len(tools)})："]
                    for _n in tools:
                        _fn = provider.get(_n)
                        _desc = (getattr(_fn, "__doc__", "") or "").strip().split("\n")[0][:100]
                        lines.append(f"  - {_n} — {_desc}" if _desc else f"  - {_n}")
                    return "\n".join(lines)
                if not domain:
                    # 空 domain 在 provider 语义里=仅通用工具（E2E 实证误导）；默认列全部
                    domain = "all"
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

        # 技能工具并入 smol_tools（2026-09-12）：custom_tools 不进入模型可见提示，
        # 执行器对技能函数只能"猜参数"（CLI 实测连环 TypeError 烧步数）。
        # 放入 tools= 后，名称/签名/说明随系统提示下发（与 search_tools 等并列）。
        smol_tools = [_SearchToolsTool(), _ListToolsTool(), _FormatResultTool(), _WebSearchTool()]
        smol_tools += list(skill_tools or [])

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
            step_callbacks=([_truncate_observations] + ([_evt_hook] if _evt_hook is not None else [])),
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
                if tools:
                    allowed_names = set(str(t) for t in tools)
                elif domain:
                    allowed_names = set(provider.list_by_domain("common") + provider.list_by_domain(domain))
                else:
                    allowed_names = set(provider.list_by_domain("common"))
                tools_text = provider.get_schemas_text(names_filter=allowed_names)
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
        event_cb=None,
    ) -> AgentResponse:
        """主对话流程：基于 StateGraph 的编排。

        用 graph.py 的 StateGraph 替代手搓 for 循环：
          - 状态持久化（Checkpointer）
          - 节点隔离（每个节点独立可测）
          - 条件路由（错误时走 fallback）
          - 流式输出（astream）
        """
        from graph import StateGraph, END
        END_SENTINEL = END  # astream 结束事件 node==END，事件回调跳过它
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
            # 实体解析组合器（2026-09-12）：StockResolver（实体扩写）+ TimeResolver
            # （时间标定，交易日历口径）串联——各产出 effective_input 增量，依次追加。
            from resolvers.stock import StockResolver
            from resolvers.time import TimeResolver

            def _combined_resolver(user_input: str):
                results = []
                for _resolver in (StockResolver(), TimeResolver()):
                    try:
                        results.append(_resolver.resolve(user_input))
                    except Exception:
                        continue
                if not results:
                    return None
                primary = next((r for r in results if r and r.effective_input), None)
                if primary is None:
                    return None
                extras = []
                for r in results:
                    if r is primary or not r:
                        continue
                    if r.effective_input and r.effective_input != user_input:
                        extras.append(r.effective_input[len(user_input):].lstrip("，, "))
                merged = primary.effective_input
                for extra in extras:
                    merged = f"{merged}；{extra}"
                from resolvers.base import ResolveResult
                return ResolveResult(
                    entities=sum((r.entities for r in results if r), []),
                    entity_code=primary.entity_code,
                    entity_name=primary.entity_name,
                    entity_type=primary.entity_type,
                    effective_input=merged,
                )

            ctx = NodeContext(
                llm=self.llm,
                memory=self.memory,
                retriever=self.retriever,
                skill_adapter=self.skill_adapter,
                system_prompt=self.system_prompt,
                memory_window_size=self.memory_window_size,
                max_tool_rounds=self.max_tool_rounds,
                entity_resolver=_combined_resolver,
            )
            ctx.event_cb = event_cb  # SSE 过程事件回调（None=非流式路径零开销）
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
                "execute": "execute",
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

            # 逐节点流式执行（2026-09-11 SSE 改造）：每个节点完成时经 event_cb 播报
            # 节点生命周期，执行节点产出 step/tool 事件由 _build_code_agent 的钩子上报。
            # node_start 播报：astream 仅按"节点完成"粒度回调，故入口节点先行播报、
            # 后续节点在上一节点完成且路由确定时播报（路由函数与图内共用、纯函数）。
            # event_cb 为 None 时行为与原 ainvoke 等价（只是换用 astream 驱动）。
            result = {}
            _node_cn = {"chat": "意图解析", "plan": "任务规划", "execute": "工具执行", "finalize": "结果整理"}
            _node_next = {"chat": route_after_chat, "plan": route_after_plan, "execute": route_after_execute}

            def _emit_node_start(_n):
                if event_cb is not None and _n in _node_cn:
                    try:
                        event_cb({"kind": "node_start", "node": _n, "label": _node_cn.get(_n, _n)})
                    except Exception:
                        pass

            _emit_node_start("chat")
            async for _evt in compiled.astream(initial_state):
                result = _evt.get("state", result)
                _node = _evt.get("node")
                if event_cb is None or _node == END_SENTINEL:
                    continue
                try:
                    if _evt.get("error"):
                        event_cb({"kind": "node_error", "node": _node,
                                  "error": str(_evt.get("error"))[:300]})
                        continue
                    event_cb({"kind": "node_done", "node": _node,
                              "label": _node_cn.get(_node, _node)})
                    if _node == "execute":
                        _obs = str(result.get("result_raw", "") or "")
                        if _obs:
                            event_cb({"kind": "step_content",
                                      "content": _obs[:1500], "stream": True})
                    elif _node == "plan":
                        _plan = result.get("_agent_plan", "")
                        if _plan:
                            event_cb({"kind": "progress",
                                      "message": "规划完成：" + str(_plan)[:200]})
                    _router = _node_next.get(_node)
                    if _router is not None:
                        _emit_node_start(_router(result))
                except Exception:
                    pass  # 事件通道故障不阻断主流程

            final_output = result.get("final_output", {})
            response = AgentResponse(
                content=result.get("result_raw", ""),
                sources=result.get("sources", []),
                session_id=session_id,
                elapsed_seconds=result.get("elapsed", 0),
                metadata={
                    "trace_id": trace.trace_id,
                    "phase_count": len(result.get("phases", [])),
                    # B 阶段（2026-09-12）：契约阶段无 "type" 键，退化为阶段名列表
                    # （旧实现取 p["type"] 会得到一串 None；保持字符串数组的对外契约）
                    "phase_types": [str(p.get("name") or "phase")
                                    for p in result.get("phases", []) if isinstance(p, dict)],
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
