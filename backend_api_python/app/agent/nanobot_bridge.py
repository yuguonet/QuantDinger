# -*- coding: utf-8 -*-
"""
Nanobot Bridge — 用 nanobot 替换 smolagents 作为 QuantDinger 的 Agent 内核。

职责：
  1. 从 .env 构建 nanobot Config（零配置重复）
  2. 将 agent/tools/ 下的函数直接包装为 nanobot Tool（不依赖 registry.py）
  3. 将 agent/skills/ 指向 nanobot SkillsLoader
  4. 提供 build_nanobot() → Nanobot 实例
  5. 提供 get_nanobot_loop() → AgentLoop（供 session_store 等使用）
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import os
import pkgutil
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from typing import get_type_hints

from nanobot.agent.tools.base import Tool

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 全局 AgentLoop 引用（供 session_store 等外部模块使用）
# ═══════════════════════════════════════════════════════════════

_loop_ref: Optional[Any] = None


def get_nanobot_loop():
    """获取全局 AgentLoop 实例。首次调用时自动构建。"""
    global _loop_ref
    if _loop_ref is None:
        _loop_ref = _build_loop()
    return _loop_ref


# ═══════════════════════════════════════════════════════════════
# 1. .env → nanobot Config
# ═══════════════════════════════════════════════════════════════

_PROVIDER_ENV_MAP = {
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL"),
    "openai":     ("OPENAI_API_KEY",     "OPENAI_BASE_URL",     "OPENAI_MODEL"),
    "deepseek":   ("DEEPSEEK_API_KEY",   "DEEPSEEK_BASE_URL",   "DEEPSEEK_MODEL"),
    "google":     ("GOOGLE_API_KEY",     "",                     "GOOGLE_MODEL"),
    "grok":       ("GROK_API_KEY",       "GROK_BASE_URL",       "GROK_MODEL"),
    "ollama":     ("",                   "OLLAMA_BASE_URL",     "OLLAMA_MODEL"),
}

_DEFAULT_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai":     "https://api.openai.com/v1",
    "deepseek":   "https://api.deepseek.com/v1",
    "google":     "https://generativelanguage.googleapis.com/v1beta",
    "grok":       "https://api.x.ai/v1",
    "ollama":     "http://localhost:11434/v1",
}

_DEFAULT_MODELS = {
    "openrouter": "openai/gpt-4o",
    "openai":     "gpt-4o",
    "deepseek":   "deepseek-chat",
    "google":     "gemini-1.5-flash",
    "grok":       "grok-beta",
    "ollama":     "qwen2.5:7b",
}

# 扫描工具模块时跳过的文件名
_SKIP_MODULES = frozenset({
    "__init__", "pagination", "screener_filters", "tool_chain_tools",
    "registry", "base", "context",
})


def _resolve_provider() -> str:
    """从 .env 推断当前 provider。"""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    for prov, (key_env, _, _) in _PROVIDER_ENV_MAP.items():
        if key_env and os.getenv(key_env, "").strip():
            return prov
    return "openrouter"


def build_nanobot_config(workspace: str | Path | None = None) -> dict:
    """从 .env 构建 nanobot config.json 结构（dict 形式）。"""
    provider = _resolve_provider()
    key_env, url_env, model_env = _PROVIDER_ENV_MAP.get(provider, ("", "", ""))

    api_key = os.getenv(key_env, "").strip() if key_env else ""
    api_base = os.getenv(url_env, "").strip() if url_env else ""
    if not api_base:
        api_base = _DEFAULT_BASE_URLS.get(provider, "")
    model = os.getenv(model_env, "").strip() if model_env else ""
    if not model:
        model = _DEFAULT_MODELS.get(provider, "gpt-4o")

    _nanobot_provider_map = {
        "openrouter": "openrouter", "openai": "openai",
        "deepseek": "openai", "google": "google",
        "grok": "openai", "ollama": "openai",
    }
    nanobot_provider = _nanobot_provider_map.get(provider, "openai")

    provider_cfg: dict[str, Any] = {}
    if api_key:
        provider_cfg["apiKey"] = api_key
    if api_base and nanobot_provider in ("openai",):
        provider_cfg["apiBase"] = api_base

    if workspace is None:
        workspace = str(Path(__file__).resolve().parent.parent.parent)

    max_steps = int(os.getenv("AGENT_MAX_STEPS", "10"))

    config = {
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "model": model,
                "provider": nanobot_provider,
                "maxToolIterations": max_steps,
                "maxTokens": int(os.getenv("OPENROUTER_MAX_TOKENS", "8192")),
                "temperature": float(os.getenv("OPENROUTER_TEMPERATURE", "0.1")),
                "timezone": "Asia/Shanghai",
            }
        },
        "providers": {nanobot_provider: provider_cfg},
        "tools": {"restrictToWorkspace": False},
    }

    if provider in ("deepseek", "grok", "ollama") and api_base:
        config["providers"]["custom"] = {"apiKey": api_key, "apiBase": api_base}
        config["agents"]["defaults"]["provider"] = "custom"

    return config


# ═══════════════════════════════════════════════════════════════
# 2. 函数 → nanobot Tool 直接包装（不依赖 registry.py）
# ═══════════════════════════════════════════════════════════════

# Python 类型 → JSON Schema 类型
_TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _python_type_to_str(tp) -> str:
    if tp is inspect.Parameter.empty:
        return "string"
    for base, name in _TYPE_MAP.items():
        if tp is base:
            return name
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


def _auto_paginate(result: Any, tool_name: str, kwargs: dict) -> Any:
    """大数据自动分页（列表>20条 / 文本>2000字符）。"""
    if result is None:
        return result
    if isinstance(result, dict) and (result.get("error") or "_pagination" in result):
        return result

    # 大文本截断
    if isinstance(result, str) and len(result) > 2000:
        from app.agent.tools.pagination import paginate_text
        cache_key = hashlib.md5(f"{tool_name}:{json.dumps(kwargs, sort_keys=True, default=str)}".encode()).hexdigest()[:12]
        return paginate_text(cache_key=cache_key, text=result, chunk_size=4000, data_key="text")

    # 大列表分页
    items, data_key = _probe_list(result)
    if items and len(items) > 20:
        from app.agent.tools.pagination import paginate_result
        cache_key = hashlib.md5(f"{tool_name}:{json.dumps(kwargs, sort_keys=True, default=str)}".encode()).hexdigest()[:12]
        return paginate_result(cache_key=cache_key, data=result, page=1, page_size=20, data_key=data_key)

    return result


def _probe_list(data: Any) -> tuple:
    if isinstance(data, list):
        return data, ""
    if not isinstance(data, dict):
        return [], ""
    for k in ["stocks", "records", "flows", "sectors", "results", "items"]:
        v = data.get(k)
        if isinstance(v, list) and v:
            return v, k
    for k, v in data.items():
        if isinstance(v, list) and v:
            return v, k
    return [], ""


class _FuncToolBridge(Tool):
    """将普通 Python 函数包装为 nanobot Tool。"""

    def __init__(self, fn: Callable, name: str, description: str):
        self._fn = fn
        self._name = name
        self._description = description
        self._parameters = self._build_schema()

    def _build_schema(self) -> dict:
        sig = inspect.signature(self._fn)
        try:
            hints = get_type_hints(self._fn)
        except Exception:
            hints = {}
        props, required = {}, []
        for pname, param in sig.parameters.items():
            tp = hints.get(pname, param.annotation)
            desc = _extract_param_desc(self._fn, pname)
            prop: dict[str, Any] = {"type": _python_type_to_str(tp)}
            if desc:
                prop["description"] = desc
            if param.default is not inspect.Parameter.empty:
                prop["default"] = param.default
            else:
                required.append(pname)
            props[pname] = prop
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
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: self._fn(**kwargs))
            result = _auto_paginate(result, self._name, kwargs)
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False, default=str)
            return str(result) if result is not None else "OK"
        except Exception as e:
            return f"Error: {e}"


# 排除的工具名
_EXCLUDED_TOOL_NAMES: Set[str] = {
    "screen_stocks", "smart_screen",
    "get_stock_fund_flow", "batch_get_stock_fund_flow",
    "get_dragon_tiger_stocks", "get_dragon_tiger_by_stock",
    "get_hot_rank_stocks", "get_zt_pool_stocks",
    "get_limit_down_stocks", "get_broken_board_stocks",
}


def _discover_tools(tools_dir: Path, deny: Set[str] | None = None) -> list[Tool]:
    """扫描 agent/tools/ 目录，发现所有可调用函数，直接包装为 nanobot Tool。

    策略：导入模块，找到所有公开的、有 docstring 的、不是内部函数的 callable。
    不依赖任何 @tool 装饰器或 registry。
    """
    deny = deny or _EXCLUDED_TOOL_NAMES
    tools: list[Tool] = []
    seen: set[str] = set()
    pkg_path = str(tools_dir)

    for _importer, module_name, _ispkg in pkgutil.iter_modules([pkg_path]):
        if module_name.startswith("_") or module_name in _SKIP_MODULES:
            continue
        try:
            mod = importlib.import_module(f"app.agent.tools.{module_name}")
        except Exception as e:
            logger.warning("[Bridge] Failed to import tool module %s: %s", module_name, e)
            continue

        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if not callable(obj):
                continue
            # 必须有 docstring 才当作工具
            if not inspect.getdoc(obj):
                continue
            # 跳过类和模块
            if inspect.isclass(obj) or inspect.ismodule(obj):
                continue
            name = attr_name
            if name in deny or name in seen:
                continue
            seen.add(name)
            desc = inspect.getdoc(obj).split("\n")[0][:300]
            tools.append(_FuncToolBridge(obj, name, desc))

    logger.info("[Bridge] Discovered %d tools from agent/tools/", len(tools))
    return tools


# ═══════════════════════════════════════════════════════════════
# 3. QuantDinger persona / system prompt 注入
# ═══════════════════════════════════════════════════════════════

def _load_persona() -> str:
    try:
        from app.agent.semantics import get_persona_body
        body = get_persona_body()
        if body:
            return body
    except Exception:
        pass
    return "你是 QuantDinger 量化分析助手，专注于 A 股量化交易分析。"


def _load_skill_instructions(skills: list[str] | None = None, user_id: int = 1) -> str:
    try:
        from app.agent.skills.indicator_skills import get_indicator_skill_instructions
        return get_indicator_skill_instructions(skills, user_id)
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# 4. AgentLoop 构建（内部）
# ═══════════════════════════════════════════════════════════════

def _build_loop(skills=None, user_id=1, extra_instructions=""):
    """构建 nanobot AgentLoop，注入 QuantDinger 工具/技能/人设。"""
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.loader import resolve_config_env_vars
    from nanobot.config.schema import Config
    from nanobot.providers.image_generation import image_gen_provider_configs

    workspace = str(Path(__file__).resolve().parent.parent.parent)
    config = Config.model_validate(build_nanobot_config(workspace))
    config = resolve_config_env_vars(config)

    loop = AgentLoop.from_config(
        config,
        image_generation_provider_configs=image_gen_provider_configs(config),
    )

    # 注入 QuantDinger 工具
    tools_dir = Path(__file__).parent / "tools"
    for tool in _discover_tools(tools_dir):
        if not loop.tools.has(tool.name):
            loop.tools.register(tool)

    # 注入人设
    persona = _load_persona()
    skill_text = _load_skill_instructions(skills, user_id)
    weight_text = ""
    try:
        from app.agent.trace import get_skill_weights_text
        weight_text = get_skill_weights_text()
    except Exception:
        pass
    parts = [p for p in [persona, skill_text, weight_text, extra_instructions] if p]
    if parts:
        loop._system_prompt_extra = "\n\n".join(parts)

    # 指向 agent/skills 目录
    agent_skills_dir = Path(__file__).parent / "skills"
    if agent_skills_dir.exists():
        loop._skills_dir = agent_skills_dir

    return loop


# ═══════════════════════════════════════════════════════════════
# 5. Nanobot 实例构建（公开）
# ═══════════════════════════════════════════════════════════════

def build_nanobot(skills=None, user_id=1, extra_instructions=""):
    """构建 Nanobot 实例。"""
    from nanobot import Nanobot
    loop = _build_loop(skills, user_id, extra_instructions)
    return Nanobot(loop)


# ═══════════════════════════════════════════════════════════════
# 6. Executor — 对接 agent_blueprint.py
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
    def __init__(self, skills=None, user_id=1, max_steps=10,
                 timeout_seconds=None, model=None, provider=None):
        self.skills = skills
        self.user_id = user_id
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds or 180
        self.model = model
        self.provider = provider
        self._bot = None
        self._current_agent = None
        self._interrupted = False
        import threading
        self._agent_ready_event = threading.Event()

    def _ensure_bot(self):
        if self._bot is None:
            self._bot = build_nanobot(skills=self.skills, user_id=self.user_id)
            global _loop_ref
            _loop_ref = self._bot._loop
        return self._bot

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._achat(message, session_id, context, user_id))
        finally:
            loop.close()

    async def _achat(self, message, session_id, context, user_id) -> AgentResult:
        enriched = self._enrich_message(message, context)
        bot = self._ensure_bot()
        t0 = __import__("time").time()
        try:
            result = await bot.run(enriched, session_key=session_id)
            self._agent_ready_event.set()
            content = result.content or ""
            elapsed_ms = (__import__("time").time() - t0) * 1000

            # 构建 EvalNode 树 + 存库
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
                # DecisionCard 格式化
                data = extract_agent_json(content)
                if data:
                    card = format_decision_card(data)
                # 提取图表
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
        event_queue: queue.Queue = queue.Queue()

        def _run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    enriched = self._enrich_message(message, context)
                    bot = self._ensure_bot()
                    event_queue.put({"type": "thinking", "step": 0, "message": "正在分析..."})
                    t0 = __import__("time").time()
                    result = loop.run_until_complete(bot.run(enriched, session_key=session_id))
                    elapsed_ms = (__import__("time").time() - t0) * 1000
                    content = result.content or ""

                    # Trace + Card
                    charts = []
                    card = content
                    try:
                        from app.agent.trace import build_eval_tree, save_tree, extract_agent_json, format_decision_card
                        tree = build_eval_tree(answer=content, session_id=session_id,
                                              user_query=message, tools_used=result.tools_used,
                                              elapsed_ms=elapsed_ms, model=self.model or "")
                        save_tree(tree, session_id=session_id, user_query=message, model=self.model or "")
                        data = extract_agent_json(content)
                        if data:
                            card = format_decision_card(data)
                        charts = [m.group(1) for m in re.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', content)]
                        card = re.sub(r'__CHART_B64__[A-Za-z0-9+/=]+__END_CHART__', '', card).strip()
                    except Exception as e:
                        logger.warning("[Bridge] trace/card failed: %s", e)

                    event_queue.put({"type": "generating", "step": 1, "message": card[:500] or "分析完成"})
                    event_queue.put({
                        "type": "done", "success": bool(content), "content": card,
                        "error": None if content else "No response",
                        "total_steps": len(result.tools_used), "model": self.model or "",
                        "session_id": session_id, "charts": charts,
                    })
                finally:
                    loop.close()
            except Exception as e:
                logger.error("[Bridge] chat_stream failed: %s", e, exc_info=True)
                event_queue.put({"type": "error", "message": str(e)})

        threading.Thread(target=_run, daemon=True).start()
        while True:
            try:
                ev = event_queue.get(timeout=self.timeout_seconds)
                yield ev
                if ev.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield {"type": "error", "message": "分析超时"}
                break

    def _enrich_message(self, message, context) -> str:
        parts = []
        if context:
            if context.get("stock_code"):
                parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                parts.append(f"股票名称: {context['stock_name']}")
            if context.get("realtime_quote"):
                parts.append(f"[实时行情]\n{json.dumps(context['realtime_quote'], ensure_ascii=False)[:2000]}")
            if context.get("chip_distribution"):
                parts.append(f"[筹码分布]\n{json.dumps(context['chip_distribution'], ensure_ascii=False)[:2000]}")
        return "\n".join(parts) + "\n\n" + message if parts else message


def build_nanobot_executor(skills=None, user_id=1, max_steps=10,
                           timeout_seconds=None, model=None, provider=None,
                           domain=None) -> NanobotExecutor:
    return NanobotExecutor(
        skills=skills, user_id=user_id, max_steps=max_steps,
        timeout_seconds=timeout_seconds, model=model, provider=provider,
    )
