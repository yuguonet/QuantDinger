# -*- coding: utf-8 -*-
"""OpenAI Function Calling schema 生成 + 「什么是工具函数」判据。

★ 来源：从 `tools/_base.py`（1025 行）拆出（2026-10-13，P4）。

★ 为什么这两件事放同一个文件
  `_is_tool_function` 是**注册表与元工具表共用的唯一判据**——单独抽出来就是为了让
  两处不漂移（2026-09-21 判据漂移事故：`_load_meta_tools` 与
  `_register_module_functions` 各写一份，导致元工具整体不可用）。
  它和 schema 生成同属「工具面元信息」层，放一起便于一眼看全判据。

★ 易错点（都踩过）
  1. `func_to_openai_schema` 必须**保留 Returns 段**、**排除 Args 段**：
     2026-09-15 前旧解析器把空行后仍当 args、把 'Returns:' 当参数行跳过 ⇒
     工具返回结构永远进不了 schema，模型只能猜返回形态（实测对 dict 切片烧步数）。
  2. 本模块的 `func_to_openai_schema` 是"公开 + 有 docstring" ⇒ **完全符合工具函数
     判据**。故本文件名为 `_schema_gen.py`（下划线前缀）：扫描器天然跳过，
     不依赖任何名单。
  3. `_is_tool_function` 的判据**只看函数本身**，不看模块级名单。
     ★ 2026-10-13 P10：此前两套模块级逃生门均已删除 —— `_NOT_TOOLS`（P9 后
     零声明者）与 `_ENTRY_NAME_DENY`（唯一命中在被跳过的模块）。非工具模块靠
     `_` 前缀退出扫描，而不是靠名单：名单要手工同步，忘了是静默的。
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, Dict, get_type_hints


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
    if origin is type(None):
        return {"type": "string"}
    if origin is dict:
        return {"type": "object"}
    if origin is list:
        args = getattr(tp, "__args__", None)
        if args:
            return {"type": "array", "items": _python_type_to_json(args[0])}
        return {"type": "array"}
    if origin is typing.Union:
        args = getattr(tp, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _python_type_to_json(non_none[0])
        return {"type": "string"}
    return {"type": _TYPE_MAP.get(tp, "string")}


def _parse_docstring_params(doc: str) -> Dict[str, str]:
    """解析 docstring 中的参数描述。"""
    if not doc:
        return {}
    param_descs: Dict[str, str] = {}
    in_args = False
    for line in doc.strip().split("\n"):
        stripped = line.strip()
        if stripped.lower().rstrip(":") in ("args", "parameters", "参数"):
            in_args = True
            continue
        if in_args:
            if stripped and not line[0].isspace() and ":" not in stripped:
                in_args = False
                continue
            if stripped and ":" in stripped:
                parts = stripped.split(":", 1)
                pname = parts[0].strip().split("(")[0].strip()
                pdesc = parts[1].strip()
                if pname:
                    param_descs[pname] = pdesc
                continue
    return param_descs


def func_to_openai_schema(func: Callable) -> Dict[str, Any]:
    """函数 → OpenAI Function Calling JSON Schema。"""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""

    # 解析描述（排除 Args 段落；Returns 段是模型需要的返回结构契约，保留在描述里）
    # 2026-09-15 修复：旧解析器把空行后仍当 args、把 'Returns:' 行当参数行跳过 →
    # 工具返回结构永远进不了 schema，模型只能猜返回形态（实测对 dict 切片烧步数）。
    desc_lines = []
    in_args = False
    for line in doc.strip().split("\n") if doc else []:
        stripped = line.strip()
        header = stripped.lower().rstrip(":")
        if header in ("args", "parameters", "参数"):
            in_args = True
            continue
        if header in ("returns", "return", "返回"):
            in_args = False          # Returns 段退出 args；内容并入描述（返回结构契约）
            continue
        if in_args:
            if stripped and not line[0].isspace() and ":" not in stripped:
                in_args = False
                desc_lines.append(line)
                continue
            if stripped and ":" in stripped:
                continue
        if not in_args:
            desc_lines.append(line)
    description = "\n".join(desc_lines).strip()
    if not description:
        description = doc.split("\n")[0][:500] if doc else func.__name__

    param_descs = _parse_docstring_params(doc)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties = {}
    required = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if pname.startswith("_"):
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue

        ptype = hints.get(pname, str)
        prop = _python_type_to_json(ptype)
        if pname in param_descs:
            prop["description"] = param_descs[pname]
        # 枚举值检测（Literal 注解）
        origin = getattr(ptype, "__origin__", None)
        if origin is typing.Literal:
            prop["enum"] = list(getattr(ptype, "__args__", ()))
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    parameters = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description[:1024],
            "parameters": parameters,
        },
    }


def _is_tool_function(obj, module) -> bool:
    """模块里哪些公开函数算"工具函数"——注册表与元工具表**共用同一判据**。

    单独抽成函数是为了两处判据不漂移：_register_module_functions（注册表）
    与 _load_meta_tools（元工具表）。

    【判据只剩"函数本身"三条·2026-10-13 P10】
    此前还有两条**模块级逃生门**，都已作废：

      - `_NOT_TOOLS`（模块级退出注册名单）：P9 把预选管线（tool_preselect /
        preselect_cooc / preselect_stats）迁出 `tools/` 后，**零模块声明它** ——
        纯读取代码在空转。
      - `_ENTRY_NAME_DENY`（入口名黑名单 main/cli/serve…）：全 `tools/` 树唯一
        命中是 `mcp_bridge.serve`，而 mcp_bridge 本就在 `_SKIP_FILES` 里 ⇒
        双重冗余，挡了个寂寞。P10 连 mcp_bridge 一并删了。

    为什么不再补新名单：**"非工具模块不该住在自发现目录里"应该由目录解决**
    （P10 起非工具模块改 `_` 前缀，扫描器天然跳过），而不是靠函数级名单 ——
    名单要手工同步，忘了是静默的（2026-10-13 实证：`_SKIP_FILES` 两份已漂移，
    mcp 侧漏登记 availability ⇒ 启动期自检函数被暴露成 MCP 工具）。
    """
    if not inspect.isfunction(obj) or not inspect.getdoc(obj):
        return False
    if getattr(obj, "__module__", "") != module.__name__:
        return False
    return not getattr(obj, "__name__", "_").startswith("_")
