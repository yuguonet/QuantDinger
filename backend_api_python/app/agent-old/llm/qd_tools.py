# -*- coding: utf-8 -*-
"""
QuantDinger 工具桥接层 — 不依赖 smolagents。

扫描 app/agent/tools/*.py 中的公开函数，自动生成 OpenAI Function Calling schema，
通过 LLMService.call_with_tools() 执行。

使用方式：
    from app.agent.llm.qd_tools import QDToolAdapter, run_with_tools

    adapter = QDToolAdapter()
    schemas = adapter.get_schemas()          # OpenAI Function Calling 格式
    result = adapter.execute("get_realtime_quote", {"codes": "600519"})

    # 一步到位：ReAct 循环
    answer = await run_with_tools(llm, messages, adapter)
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints

logger = logging.getLogger(__name__)

# ── 类型映射 ──────────────────────────────────────────────────
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json(tp) -> Dict[str, Any]:
    """Python 类型 → JSON Schema type。"""
    origin = getattr(tp, "__origin__", None)

    # Optional[X] → X, 但 required=False（由调用方处理）
    if origin is type(None):
        return {"type": "string"}

    # Dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # List[X]
    if origin is list:
        args = getattr(tp, "__args__", None)
        if args:
            return {"type": "array", "items": _python_type_to_json(args[0])}
        return {"type": "array"}

    # Union (包含 Optional)
    import typing
    if origin is typing.Union:
        args = getattr(tp, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _python_type_to_json(non_none[0])
        return {"type": "string"}

    # 基本类型
    return {"type": _TYPE_MAP.get(tp, "string")}


def _parse_docstring(doc: str) -> tuple:
    """解析 docstring → (description, {param: description})。"""
    if not doc:
        return "", {}

    lines = doc.strip().split("\n")
    desc_lines = []
    param_descs = {}
    in_args = False

    for line in lines:
        stripped = line.strip()

        # 检测 Args: / Parameters: / 参数: 段落
        if stripped.lower().rstrip(":") in ("args", "parameters", "参数"):
            in_args = True
            continue

        if in_args:
            # 新段落开始（非缩进、非空行）→ 结束 Args
            if stripped and not line[0].isspace() and ":" not in stripped:
                in_args = False
                desc_lines.append(line)
                continue

            # 参数行: "name: description" 或 "name (type): description"
            if stripped and ":" in stripped:
                parts = stripped.split(":", 1)
                pname = parts[0].strip().split("(")[0].strip()  # 去掉 (type)
                pdesc = parts[1].strip()
                if pname:
                    param_descs[pname] = pdesc
                continue

        if not in_args:
            desc_lines.append(line)

    description = "\n".join(desc_lines).strip()
    return description, param_descs


def _func_to_schema(func: Callable) -> Dict[str, Any]:
    """函数 → OpenAI Function Calling JSON Schema。"""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    description, param_descs = _parse_docstring(doc)

    # 获取类型注解
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties = {}
    required = []

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue

        # 跳过 **kwargs
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue

        ptype = hints.get(pname, str)
        prop = _python_type_to_json(ptype)

        if pname in param_descs:
            prop["description"] = param_descs[pname]

        # 枚举值检测（如果有 Literal 或 Enum）
        origin = getattr(ptype, "__origin__", None)
        import typing
        if origin is typing.Literal:
            prop["enum"] = list(getattr(ptype, "__args__", ()))

        properties[pname] = prop

        # 无默认值 → required
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    # 取 docstring 第一行作 fallback description
    if not description:
        description = doc.split("\n")[0][:500] if doc else func.__name__

    schema = {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description[:1024],
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        },
    }
    if required:
        schema["function"]["parameters"]["required"] = required

    return schema


# ── 工具发现 ──────────────────────────────────────────────────

# 跳过的文件名（框架文件，非工具）
_SKIP_FILES = {"__init__", "base", "registry", "em_utils", "pagination", "screener_config"}


def discover_tools(tools_dir: str = None) -> Dict[str, Callable]:
    """扫描 tools/*.py，发现所有公开工具函数。"""
    if tools_dir is None:
        tools_dir = str(Path(__file__).resolve().parent.parent / "tools")

    tools = {}
    tools_path = Path(tools_dir)
    if not tools_path.exists():
        logger.warning("[QDTools] 目录不存在: %s", tools_dir)
        return tools

    for py_file in sorted(tools_path.glob("*.py")):
        module_name = py_file.stem
        if module_name.startswith("_") or module_name in _SKIP_FILES:
            continue

        try:
            mod = importlib.import_module(f"app.agent.tools.{module_name}")
        except Exception:
            logger.debug("[QDTools] 跳过模块 %s", module_name, exc_info=True)
            continue

        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if not callable(obj) or not inspect.isfunction(obj):
                continue
            # 只要当前模块定义的函数
            if getattr(obj, "__module__", "") != mod.__name__:
                continue
            # 必须有 docstring
            if not inspect.getdoc(obj):
                continue
            tools[attr_name] = obj

    logger.info("[QDTools] 发现 %d 个工具函数", len(tools))
    return tools


# ── 工具适配器 ────────────────────────────────────────────────

class QDToolAdapter:
    """QuantDinger 工具适配器 — 管理工具发现、schema 生成、执行。"""

    def __init__(self, tools_dir: str = None):
        self._tools: Dict[str, Callable] = {}
        self._schemas: List[Dict[str, Any]] = []
        self._schema_map: Dict[str, Dict[str, Any]] = {}
        self._tools_dir = tools_dir
        self._discover()

    def _discover(self):
        self._tools = discover_tools(self._tools_dir)
        for name, func in self._tools.items():
            try:
                schema = _func_to_schema(func)
                self._schemas.append(schema)
                self._schema_map[name] = schema
            except Exception as e:
                logger.warning("[QDTools] schema 生成失败 %s: %s", name, e)

    def get_schemas(self) -> List[Dict[str, Any]]:
        """返回所有工具的 OpenAI Function Calling schema。"""
        return list(self._schemas)

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        """返回指定工具的 schema。"""
        return self._schema_map.get(name)

    def execute(self, name: str, arguments: Dict[str, Any]) -> Any:
        """执行工具，返回结果（dict 或 str）。"""
        func = self._tools.get(name)
        if func is None:
            return {"error": f"工具 '{name}' 不存在"}

        try:
            # 过滤掉函数不接受的参数
            sig = inspect.signature(func)
            valid_params = set(sig.parameters.keys())
            filtered_args = {k: v for k, v in arguments.items() if k in valid_params}

            result = func(**filtered_args)

            # 确保返回值可序列化
            if isinstance(result, (dict, list)):
                return result
            if isinstance(result, str):
                return result
            return {"result": result}
        except Exception as e:
            logger.error("[QDTools] 执行 %s 失败: %s", name, e, exc_info=True)
            return {"error": str(e)}

    def list_tools(self) -> List[str]:
        """列出所有工具名。"""
        return sorted(self._tools.keys())

    def __len__(self):
        return len(self._tools)


# ── ReAct 循环 ────────────────────────────────────────────────

def _to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确保消息格式兼容 OpenAI API。"""
    result = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "tool":
            # tool 消息格式已经是正确的
            result.append(msg)
        elif role == "assistant" and "tool_calls" in msg:
            # assistant 消息带 tool_calls
            result.append(msg)
        else:
            result.append({"role": role, "content": msg.get("content", "")})
    return result


async def run_with_tools(
    llm,
    messages: List[Dict[str, Any]],
    adapter: QDToolAdapter = None,
    max_iterations: int = 10,
    temperature: float = 0.3,
) -> str:
    """统一 Agent 入口：有工具走 ReAct，无工具走纯对话。

    Args:
        llm: QDLLM 实例
        messages: 初始消息列表 [{"role": "user", "content": "..."}]
        adapter: QDToolAdapter 实例（None 或空则走纯对话）
        max_iterations: 最大循环次数
        temperature: LLM 温度

    Returns:
        最终回答文本
    """
    from .base import ChatMessage

    tools = adapter.get_schemas() if adapter else []
    chat_messages = [ChatMessage(role=m["role"], content=m.get("content", "")) for m in messages]

    # 无工具 → 纯对话
    if not tools:
        response = await llm.generate(chat_messages, temperature=temperature)
        return response.content

    # 有工具 → ReAct 循环
    current_messages = _to_openai_messages(messages)

    for iteration in range(max_iterations):
        response = await llm.generate(
            [ChatMessage(role=m["role"], content=m.get("content", "")) for m in current_messages],
            tools=tools,
            temperature=temperature,
        )

        if response.is_error:
            return f"LLM 调用失败: {response.content}"

        # 调试：显示 tool_calls 状态
        logger.info("[ReAct] 第%d轮: tool_calls=%s, content=%s",
                    iteration + 1,
                    f"{len(response.tool_calls)} 个" if response.tool_calls else "无",
                    (response.content or "")[:100])

        # 无 tool_calls → 最终回答
        if not response.tool_calls:
            return response.content

        # 构造 assistant 消息（带 tool_calls）
        assistant_msg = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False)
                            if isinstance(tc.get("arguments"), dict)
                            else str(tc.get("arguments", "{}")),
                    },
                }
                for i, tc in enumerate(response.tool_calls)
            ],
        }
        current_messages.append(assistant_msg)

        # 执行每个 tool_call
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc.get("arguments", {})
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except (json.JSONDecodeError, TypeError):
                    tool_args = {}

            logger.info("[ReAct] 执行工具: %s(%s)", tool_name, list(tool_args.keys()))
            tool_result = adapter.execute(tool_name, tool_args)

            if isinstance(tool_result, str):
                content = tool_result
            else:
                try:
                    content = json.dumps(tool_result, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    content = str(tool_result)

            if len(content) > 8000:
                content = content[:8000] + "\n...(结果过长，已截断)"

            current_messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{i}"),
                "content": content,
            })

    return "达到最大迭代次数，任务未完成。"
