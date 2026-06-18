# -*- coding: utf-8 -*-
"""
QuantDinger → nanobot 桥接层（精简版）

职责：
1. 构建 config（传给 nanobot）
2. 注入 QuantDinger 工具（_FuncToolBridge 包装函数为 Tool）
3. 注入 persona/weights（system_prompt_extra）
4. 注入 hook（闭环逻辑）

不管 session、不管上下文、不造轮子。
nanobot 自己处理 session 管理、上下文构建、consolidation。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from nanobot.agent.tools.base import Tool

from app.agent.config import (
    DEFAULT_BASE_URLS,
    DEFAULT_CONTEXT_BLOCK_LIMIT,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_DREAM_ENABLED,
    DEFAULT_DREAM_INTERVAL_H,
    DEFAULT_MAX_CONCURRENT_SUBAGENTS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOOL_ITERATIONS,
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    DEFAULT_MODELS,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEZONE,
    EXCLUDED_TOOL_NAMES,
    NANOBOT_PROVIDER_MAP,
    PROVIDER_ENV_MAP,
    SKIP_MODULES,
    TYPE_MAP,
)

logger = logging.getLogger(__name__)

_loop_ref: Optional[Any] = None


# ═══════════════════════════════════════════════════════════════
# 1. Config 构建（唯一需要的配置逻辑）
# ═══════════════════════════════════════════════════════════════

def _resolve_provider() -> str:
    """从 .env 推断当前 provider。"""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    for prov, (key_env, _, _) in PROVIDER_ENV_MAP.items():
        if key_env and os.getenv(key_env, "").strip():
            return prov
    return "openrouter"


def build_nanobot_config(
    workspace: str | Path | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    max_tool_iterations: int | None = None,
    max_concurrent_subagents: int | None = None,
    context_window_tokens: int | None = None,
    context_block_limit: int | None = None,
    model_preset: str | None = None,
    timezone: str | None = None,
    dream_enabled: bool | None = None,
    dream_interval_h: float | None = None,
) -> dict:
    """构建 nanobot config dict。参数优先级：kwargs > env > config.py 常量 > nanobot 默认值。"""
    raw_provider = provider or _resolve_provider()
    key_env, url_env, model_env = PROVIDER_ENV_MAP.get(raw_provider, ("", "", ""))

    api_key = os.getenv(key_env, "").strip() if key_env else ""
    api_base = os.getenv(url_env, "").strip() if url_env else ""
    if not api_base:
        api_base = DEFAULT_BASE_URLS.get(raw_provider, "")

    if model is None:
        model = os.getenv(model_env, "").strip() if model_env else ""
    if not model:
        model = DEFAULT_MODELS.get(raw_provider, "gpt-4o")

    nanobot_provider = NANOBOT_PROVIDER_MAP.get(raw_provider, "openai")

    provider_cfg: dict[str, Any] = {}
    if api_key:
        provider_cfg["apiKey"] = api_key
    if api_base and nanobot_provider in ("openai",):
        provider_cfg["apiBase"] = api_base

    if workspace is None:
        workspace = str(Path(__file__).resolve().parent.parent.parent)

    defaults: dict[str, Any] = {
        "workspace": str(workspace),
        "provider": nanobot_provider,
        "timezone": timezone or DEFAULT_TIMEZONE,
    }
    if model:
        defaults["model"] = model

    # Generation
    defaults["maxTokens"] = max_tokens or int(os.getenv("OPENROUTER_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    defaults["temperature"] = temperature if temperature is not None else float(os.getenv("OPENROUTER_TEMPERATURE", DEFAULT_TEMPERATURE))

    # Agent loop
    defaults["maxToolIterations"] = max_tool_iterations or int(os.getenv("AGENT_MAX_STEPS", DEFAULT_MAX_TOOL_ITERATIONS))
    defaults["maxConcurrentSubagents"] = max_concurrent_subagents or DEFAULT_MAX_CONCURRENT_SUBAGENTS

    # Context（session 不 replay，上下文从 history.jsonl 注入）
    defaults["maxMessages"] = 0
    defaults["contextWindowTokens"] = context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
    defaults["maxToolResultChars"] = DEFAULT_MAX_TOOL_RESULT_CHARS

    if context_block_limit is not None:
        defaults["contextBlockLimit"] = context_block_limit
    elif DEFAULT_CONTEXT_BLOCK_LIMIT is not None:
        defaults["contextBlockLimit"] = DEFAULT_CONTEXT_BLOCK_LIMIT

    if reasoning_effort is not None:
        defaults["reasoningEffort"] = reasoning_effort
    elif DEFAULT_REASONING_EFFORT is not None:
        defaults["reasoningEffort"] = DEFAULT_REASONING_EFFORT

    if model_preset is not None:
        defaults["modelPreset"] = model_preset

    defaults["dream"] = {
        "enabled": dream_enabled if dream_enabled is not None else DEFAULT_DREAM_ENABLED,
        "intervalH": dream_interval_h if dream_interval_h is not None else DEFAULT_DREAM_INTERVAL_H,
    }

    config: dict[str, Any] = {
        "agents": {"defaults": defaults},
        "providers": {nanobot_provider: provider_cfg},
        "tools": {"restrictToWorkspace": False},
    }

    if raw_provider in ("deepseek", "grok", "ollama") and api_base:
        config["providers"]["custom"] = {"apiKey": api_key, "apiBase": api_base}
        config["agents"]["defaults"]["provider"] = "custom"

    return config


# ═══════════════════════════════════════════════════════════════
# 2. 工具包装（函数 → nanobot Tool，必须的适配层）
# ═══════════════════════════════════════════════════════════════

def _python_type_to_str(tp) -> str:
    """Python 类型 → JSON Schema 类型字符串。"""
    if tp is inspect.Parameter.empty:
        return "string"
    # 基本类型
    for base, name in TYPE_MAP.items():
        if tp is base:
            return name
    # 特殊类型（datetime、Path 等）统一当 string
    import datetime as _dt
    if tp in (_dt.datetime, _dt.date, _dt.time, Path):
        return "string"
    # 类型字符串匹配（处理 from typing import ... 的情况）
    tp_str = str(tp).lower()
    if "datetime" in tp_str or "date" in tp_str or "path" in tp_str:
        return "string"
    return "string"


def _extract_param_desc(fn: Callable, param_name: str) -> str:
    """从 Google-style docstring 提取参数描述。"""
    doc = inspect.getdoc(fn) or ""
    in_args = False
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped.lower().rstrip(":") in ("args", "arguments", "parameters"):
            in_args = True
            continue
        if in_args and stripped and not stripped[0].isspace() and stripped.endswith(":"):
            break
        if in_args and ":" in stripped:
            name_part, _, desc_part = stripped.partition(":")
            if name_part.strip().split()[0] == param_name:
                return desc_part.strip()
    return ""


class _FuncToolBridge(Tool):
    """将普通 Python 函数包装为 nanobot Tool。"""

    def __init__(self, fn: Callable, name: str, description: str):
        self._fn = fn
        self._name = name
        self._description = description
        self._parameters = self._build_schema()

    def _build_schema(self) -> dict:
        try:
            sig = inspect.signature(self._fn)
        except (ValueError, TypeError):
            # 内置类型没有签名，返回空 schema
            return {"type": "object", "properties": {}}
        try:
            hints = {k: v for k, v in __import__('typing').get_type_hints(self._fn).items()}
        except Exception:
            hints = {}
        props, required = {}, []
        for pname, param in sig.parameters.items():
            tp = hints.get(pname, param.annotation)
            try:
                type_str = _python_type_to_str(tp)
            except Exception:
                type_str = "string"
            desc = _extract_param_desc(self._fn, pname)
            prop: dict[str, Any] = {"type": type_str}
            if desc:
                prop["description"] = desc
            props[pname] = prop
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs) -> Any:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._fn(**kwargs))


def _discover_tools(tools_dir: Path, deny: Set[str] | None = None) -> list[Tool]:
    """扫描 tools 目录，将公开函数包装为 nanobot Tool。"""
    import pkgutil
    import importlib

    deny = deny or EXCLUDED_TOOL_NAMES
    results: list[Tool] = []
    seen: set[str] = set()

    for _importer, module_name, _ispkg in pkgutil.iter_modules([str(tools_dir)]):
        if module_name.startswith("_") or module_name in SKIP_MODULES:
            continue
        try:
            mod = importlib.import_module(f"app.agent.tools.{module_name}")
        except Exception:
            logger.exception("[Bridge] Failed to import tool module: %s", module_name)
            continue

        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if not callable(obj):
                continue
            doc = inspect.getdoc(obj)
            if not doc:
                continue
            name = attr_name
            if name in deny or name in seen:
                continue
            seen.add(name)
            description = doc.split("\n")[0][:500]
            results.append(_FuncToolBridge(obj, name, description))

    results.sort(key=lambda t: t.name)
    return results


# ═══════════════════════════════════════════════════════════════
# 3. 构建 nanobot 实例（注入 tools + persona + weights）
# ═══════════════════════════════════════════════════════════════

def _is_local_provider(provider) -> bool:
    """判断是否为本地模型 provider（不支持 function calling）。"""
    name = type(provider).__name__.lower()
    # Provider 名称匹配
    if any(k in name for k in ("ollama", "lmstudio", "local")):
        return True
    # API base URL 匹配（localhost / 127.0.0.1）
    base_url = getattr(provider, "base_url", None) or getattr(provider, "api_base", None) or ""
    if any(k in str(base_url).lower() for k in ("localhost", "127.0.0.1")):
        return True
    return False


class _TextToolCallProviderWrapper:
    """包装 provider，从 content 中解析工具调用 JSON（用于不支持 function calling 的本地模型）。

    同时负责将结构化 content block 拍平为纯文本——部分本地模型（Ollama、LM Studio、
    自定义代理）不支持 ``[{"type": "text", "text": "..."}]`` 格式，只接受 string content。
    """

    def __init__(self, inner, tool_names: set):
        self._inner = inner
        self._tool_names = tool_names
        for attr in ("model", "base_url", "api_key"):
            if hasattr(inner, attr):
                setattr(self, attr, getattr(inner, attr))

    @staticmethod
    def _flatten_messages(messages: list[dict]) -> list[dict]:
        """将结构化 content block 列表拍平为纯文本（本地模型兼容）。"""
        result = []
        for msg in messages:
            clean = dict(msg)
            content = clean.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        itype = item.get("type", "")
                        if itype in ("text", "input_text", "output_text"):
                            text = item.get("text", "")
                            if text:
                                parts.append(text)
                        elif itype == "image_url":
                            # 本地模型不支持 image_url，跳过
                            continue
                        elif isinstance(item.get("text"), str):
                            parts.append(item["text"])
                    elif isinstance(item, str):
                        parts.append(item)
                clean["content"] = "\n".join(parts) if parts else ""
            result.append(clean)
        return result

    def _inject_tool_calls(self, response):
        if response.tool_calls or not response.content:
            return response
        parsed = _parse_tool_calls_from_text(response.content, self._tool_names)
        if not parsed:
            return response
        from nanobot.providers.base import LLMResponse
        return LLMResponse(
            content=response.content,
            tool_calls=parsed,
            finish_reason="tool_calls",
            usage=response.usage,
            reasoning_content=response.reasoning_content,
        )

    async def chat(self, *args, **kwargs):
        if "messages" in kwargs:
            kwargs["messages"] = self._flatten_messages(kwargs["messages"])
        return self._inject_tool_calls(await self._inner.chat(*args, **kwargs))

    async def chat_stream(self, *args, **kwargs):
        if "messages" in kwargs:
            kwargs["messages"] = self._flatten_messages(kwargs["messages"])
        async for chunk in self._inner.chat_stream(*args, **kwargs):
            yield chunk

    async def chat_with_retry(self, *args, **kwargs):
        if "messages" in kwargs:
            kwargs["messages"] = self._flatten_messages(kwargs["messages"])
        return self._inject_tool_calls(await self._inner.chat_with_retry(*args, **kwargs))

    async def chat_stream_with_retry(self, *args, **kwargs):
        if "messages" in kwargs:
            kwargs["messages"] = self._flatten_messages(kwargs["messages"])
        async for chunk in self._inner.chat_stream_with_retry(*args, **kwargs):
            yield chunk

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _parse_tool_calls_from_text(content: str, available_tools: set) -> list:
    """从文本中解析工具调用 JSON（支持单个对象和数组）。"""
    from nanobot.providers.base import ToolCallRequest

    if not content:
        logger.debug("[Bridge] _parse_tool_calls_from_text: empty content")
        return []

    results = []
    logger.debug("[Bridge] _parse_tool_calls_from_text: content_len=%s, available_tools=%s", len(content), len(available_tools))

    # 格式 1: ```json ... ``` 块
    json_blocks = re.findall(r'```(?:json)?\s*\n?(.*?)\n?\s*```', content, re.DOTALL)
    for raw in json_blocks:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            if not name or name not in available_tools:
                continue
            args = item.get("arguments") or item.get("parameters") or {}
            results.append(ToolCallRequest(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name=name,
                arguments=args if isinstance(args, dict) else {},
            ))

    # 格式 2/3: 裸 JSON 对象/数组
    bare_content = re.sub(r'```(?:json)?\s*\n?.*?\n?\s*```', '', content, flags=re.DOTALL)
    i = 0
    while i < len(bare_content):
        if bare_content[i] not in ('{', '['):
            i += 1
            continue
        open_char = bare_content[i]
        close_char = '}' if open_char == '{' else ']'
        depth = 1
        j = i + 1
        while j < len(bare_content) and depth > 0:
            if bare_content[j] == open_char:
                depth += 1
            elif bare_content[j] == close_char:
                depth -= 1
            j += 1
        if depth != 0:
            i += 1
            continue

        json_str = bare_content[i:j]
        if not _is_standalone_json(bare_content, i):
            i = j
            continue
        if '"name"' not in json_str:
            i = j
            continue
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            i = j
            continue
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            if name in available_tools:
                args = item.get("arguments") or item.get("parameters") or {}
                results.append(ToolCallRequest(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    name=name,
                    arguments=args if isinstance(args, dict) else {},
                ))
        i = j

    return results


def _is_standalone_json(content: str, match_start: int) -> bool:
    """判断 JSON 匹配是独立的工具调用还是嵌在散文中的 false positive。"""
    if match_start <= 0:
        return True
    i = match_start - 1
    while i >= 0 and content[i] in ' \t\n\r':
        i -= 1
    if i < 0:
        return True
    char_before = content[i]
    if char_before.isalnum() or char_before in {')', ']', '}', '>', '"', "'", ':'}:
        return False
    return True


def build_nanobot(
    skills=None, user_id=1, extra_instructions="",
    **config_overrides,
):
    """构建 Nanobot 实例，注入 QuantDinger 工具/技能/人设。"""
    from nanobot import Nanobot
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.loader import resolve_config_env_vars
    from nanobot.config.schema import Config
    from nanobot.providers.image_generation import image_gen_provider_configs

    # 1. Config
    config = Config.model_validate(build_nanobot_config(**config_overrides))
    config = resolve_config_env_vars(config)

    # 2. AgentLoop
    loop = AgentLoop.from_config(
        config,
        image_generation_provider_configs=image_gen_provider_configs(config),
    )

    # 3. 注入 QuantDinger 工具
    tools_dir = Path(__file__).parent / "tools"
    for tool in _discover_tools(tools_dir):
        if not loop.tools.has(tool.name):
            loop.tools.register(tool)

    # 4. 本地模型：包装 provider
    if _is_local_provider(loop.provider):
        tool_names = set(loop.tools.tool_names)
        loop.provider = _TextToolCallProviderWrapper(loop.provider, tool_names)
        loop.runner.provider = loop.provider
        logger.info("[Bridge] Wrapped provider with text tool-call parser (local model, %s tools)", len(tool_names))
    else:
        logger.info("[Bridge] Provider %s does not need text tool-call parser", type(loop.provider).__name__)

    # 5. 注入 persona + weights（system_prompt_extra）
    persona = _load_persona()
    weight_text = _load_weights()
    parts = [p for p in [persona, weight_text, extra_instructions] if p]
    if parts:
        loop.context.system_prompt_extra = "\n\n".join(parts)

    # 6. 指向 agent/skills 目录
    agent_skills_dir = Path(__file__).parent / "skills"
    if agent_skills_dir.exists():
        loop._skills_dir = agent_skills_dir

    return Nanobot(loop)


def _load_persona() -> str:
    """加载 persona.md。"""
    persona_path = Path(__file__).parent / "semantics" / "persona.md"
    if persona_path.exists():
        try:
            return open(persona_path, encoding="utf-8").read().strip()
        except Exception:
            pass
    return ""


def _load_weights() -> str:
    """加载 skill 权重。"""
    try:
        from app.agent.trace import get_skill_weights_text
        return get_skill_weights_text()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# 4. Executor（对接 agent_blueprint.py）
# ═══════════════════════════════════════════════════════════════

class AgentResult:
    def __init__(self, success=False, content="", tool_calls_log=None,
                 total_steps=0, total_tokens=0, model="", error=None, charts=None):
        self.success = success
        self.content = content
        self.tool_calls_log = tool_calls_log or []
        self.total_steps = total_steps
        self.total_tokens = total_tokens
        self.model = model
        self.error = error
        self.charts = charts or []


class NanobotExecutor:
    """QuantDinger Agent 执行器。"""

    def __init__(
        self,
        skills=None,
        user_id=1,
        model=None,
        provider=None,
        temperature=None,
        max_tokens=None,
        max_steps=None,
        timeout_seconds=None,  # 保留兼容，但不使用（nanobot 自己管理超时）
        context_window_tokens=None,
        context_block_limit=None,
        reasoning_effort=None,
        model_preset=None,
        max_concurrent_subagents=None,
        dream_enabled=None,
        dream_interval_h=None,
        timezone=None,
    ):
        self.skills = skills
        self.user_id = user_id
        self.model = model
        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.context_window_tokens = context_window_tokens
        self.context_block_limit = context_block_limit
        self.reasoning_effort = reasoning_effort
        self.model_preset = model_preset
        self.max_concurrent_subagents = max_concurrent_subagents
        self.dream_enabled = dream_enabled
        self.dream_interval_h = dream_interval_h
        self.timezone = timezone
        self._bot = None
        import threading
        self._agent_ready_event = threading.Event()

    def _ensure_bot(self):
        if self._bot is None:
            overrides = {}
            for key, attr in [
                ("model", "model"), ("provider", "provider"),
                ("temperature", "temperature"), ("max_tokens", "max_tokens"),
                ("max_tool_iterations", "max_steps"),
                ("context_window_tokens", "context_window_tokens"),
                ("context_block_limit", "context_block_limit"),
                ("reasoning_effort", "reasoning_effort"),
                ("model_preset", "model_preset"),
                ("max_concurrent_subagents", "max_concurrent_subagents"),
                ("dream_enabled", "dream_enabled"),
                ("dream_interval_h", "dream_interval_h"),
                ("timezone", "timezone"),
            ]:
                val = getattr(self, attr, None)
                if val is not None:
                    overrides[key] = val
            self._bot = build_nanobot(
                skills=self.skills, user_id=self.user_id,
                **overrides,
            )
            global _loop_ref
            _loop_ref = self._bot._loop
        return self._bot

    def _enrich_message(self, message, context):
        """拼接 context 信息到消息。"""
        if not context:
            return message
        parts = [message]
        if context.get("stock_code"):
            parts.append(f"[stock_code={context['stock_code']}]")
        if context.get("stock_name"):
            parts.append(f"[stock_name={context['stock_name']}]")
        if context.get("realtime_quote"):
            parts.append(f"[realtime_quote={json.dumps(context['realtime_quote'], ensure_ascii=False)}]")
        return "\n".join(parts)

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._achat(message, session_id, context, user_id))
        finally:
            loop.close()

    async def _achat(self, message, session_id, context, user_id) -> AgentResult:
        import time as _time
        enriched = self._enrich_message(message, context)
        bot = self._ensure_bot()
        t0 = _time.time()
        try:
            result = await bot.run(enriched, session_key=session_id)
            self._agent_ready_event.set()
            content = result.content or ""
            elapsed_ms = (_time.time() - t0) * 1000

            # EvalNode 树 + DecisionCard
            charts = []
            card = content
            try:
                from app.agent.trace import build_eval_tree, save_tree, extract_agent_json, format_decision_card
                tree = build_eval_tree(
                    answer=content, session_id=session_id,
                    user_query=message, tools_used=result.tools_used,
                    elapsed_ms=elapsed_ms, model=self.model or "",
                )
                save_tree(tree, session_id=session_id, user_query=message, model=self.model or "")
                data = extract_agent_json(content)
                if data:
                    card = format_decision_card(data)
                charts = [m.group(1) for m in re.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', content)]
                card = re.sub(r'__CHART_B64__[A-Za-z0-9+/=]+__END_CHART__', '', card).strip()
            except Exception as e:
                logger.warning("[Bridge] trace/card failed: %s", e)

            return AgentResult(
                success=bool(content), content=card,
                tool_calls_log=[{"tool": t, "success": True} for t in result.tools_used],
                total_steps=len(result.messages), model=self.model or "", charts=charts,
            )
        except Exception as e:
            logger.error("[Bridge] chat failed: %s", e, exc_info=True)
            return AgentResult(success=False, error=str(e))

    def chat_stream(self, message, session_id, context=None,
                    progress_callback=None, user_id=1):
        import queue
        import threading
        import asyncio

        event_queue: queue.Queue = queue.Queue()

        def _run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    enriched = self._enrich_message(message, context)
                    bot = self._ensure_bot()

                    async def _on_stream(delta: str):
                        if delta:
                            event_queue.put({"type": "generating", "step": 1, "message": delta[:500]})

                    result = loop.run_until_complete(
                        bot.run(enriched, session_key=session_id, hooks=[])
                    )
                    self._agent_ready_event.set()
                    content = result.content or ""

                    charts = []
                    card = content
                    try:
                        from app.agent.trace import build_eval_tree, save_tree, extract_agent_json, format_decision_card
                        tree = build_eval_tree(
                            answer=content, session_id=session_id,
                            user_query=message, tools_used=result.tools_used,
                            elapsed_ms=0, model=self.model or "",
                        )
                        save_tree(tree, session_id=session_id, user_query=message, model=self.model or "")
                        data = extract_agent_json(content)
                        if data:
                            card = format_decision_card(data)
                        charts = [m.group(1) for m in re.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', content)]
                        card = re.sub(r'__CHART_B64__[A-Za-z0-9+/=]+__END_CHART__', '', card).strip()
                    except Exception as e:
                        logger.warning("[Bridge] trace/card failed: %s", e)

                    event_queue.put({
                        "type": "done", "success": bool(content), "content": card,
                        "error": None, "total_steps": len(result.messages),
                        "model": self.model or "", "session_id": session_id, "charts": charts,
                    })
                finally:
                    loop.close()
            except Exception as e:
                logger.error("[Bridge] chat_stream failed: %s", e, exc_info=True)
                event_queue.put({"type": "done", "success": False, "content": "", "error": str(e)})

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        while True:
            try:
                event = event_queue.get(timeout=180)
            except queue.Empty:
                yield {"type": "done", "success": False, "content": "", "error": "timeout"}
                break
            yield event
            if event.get("type") == "done":
                break

    def chat_with_retry(self, message, session_id, context=None, max_retries=2):
        for attempt in range(max_retries + 1):
            result = self.chat(message, session_id, context)
            if result.success or attempt == max_retries:
                return result
        return result

    def chat_stream_with_retry(self, message, session_id, context=None):
        yield from self.chat_stream(message, session_id, context)

    def interrupt(self):
        self._agent_ready_event.set()

    @property
    def is_ready(self):
        return self._agent_ready_event.is_set()


# ═══════════════════════════════════════════════════════════════
# 5. 全局实例
# ═══════════════════════════════════════════════════════════════

_global_executor: Optional[NanobotExecutor] = None


def get_nanobot_loop():
    global _loop_ref
    if _loop_ref is None:
        bot = build_nanobot()
        _loop_ref = bot._loop
    return _loop_ref


def build_agent_executor(
    skills=None, user_id=1, max_steps=10, timeout_seconds=180,
    model=None, provider=None, **kwargs,
):
    return NanobotExecutor(
        skills=skills, user_id=user_id, model=model, provider=provider,
        max_steps=max_steps, **kwargs,
    )
