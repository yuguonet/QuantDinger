# -*- coding: utf-8 -*-
"""search_capabilities / call_capability —— 能力层低优先级派发工具。

能力层语义（沿旧设计，2026-09-30 接入 mimoagent 形态）：
  - **工具层 > 能力层**：能力函数不进主工具面（不全量下发），只经本模块按需派发；
  - 准入事实源仍是 capabilities/admission.json（人工过目固化；缺失 = 0 能力，不报错）；
  - 三闸门保留：写操作前缀硬复核 + 超时护栏 + 结果体积护栏（loader._wrap_guards）；
  - 与工具层同名/近重名的能力让位：**同名**直接执行工具层实现（F4，2026-10-17，
    不再让模型白跑一轮）；**近重名**无法安全代执行（入参口径可能不同），仍硬拦
    并提示改用工具层工具。

admission.json 不随仓库分发（人工审核件）：把它放回 capabilities/ 即接入。
"""
from __future__ import annotations

from typing import Any, Dict, List


def _load_loader():
    from ..capabilities import loader as _l
    return _l


def _admitted_meta() -> List[dict]:
    try:
        return _load_loader().load_admitted_meta() or []
    except Exception:
        return []


def list_capabilities() -> Dict[str, Any]:
    """列出全部准入能力（名称 + 模块 + 简介）。低优先级：仅按需调用。

    Returns:
        {"count": N, "capabilities": [{name, module, description}, ...]}
    """
    metas = _admitted_meta()
    out = []
    for m in metas:
        out.append({
            "name": m.get("name", ""),
            "module": m.get("module", m.get("mod_path", "")),
            "description": (m.get("description") or m.get("doc") or "")[:200],
        })
    return {"count": len(out), "capabilities": out}


def search_capabilities(query: str, count: int = 8) -> Dict[str, Any]:
    """按关键词搜索能力层函数（名称/模块/简介模糊匹配）。

    Args:
        query: 搜索关键词
        count: 最多返回条数

    Returns:
        {"count": N, "matches": [{name, module, description}, ...]}
    """
    metas = _admitted_meta()
    keys = [k.lower() for k in (query or "").split() if k]
    scored = []
    for m in metas:
        hay = f"{m.get('name', '')} {m.get('module', m.get('mod_path', ''))} {m.get('description', m.get('doc', ''))}".lower()
        score = sum(1 for k in keys if k in hay)
        if score or not keys:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    matches = [{
        "name": m.get("name", ""),
        "module": m.get("module", m.get("mod_path", "")),
        "description": (m.get("description") or m.get("doc") or "")[:200],
    } for _, m in scored[:count]]
    return {"count": len(matches), "matches": matches}


def _active_tool_face():
    """当前 run 的活跃 ToolFace（QDAgent）；无 → None（能力层可脱离 agent 直连调用）。

    经线程局部钩子表取 `active_agent`（`qd_agent.run()` 在 run 开始写入、`finally` 清）。
    取不到不是异常：脚本/测试直连能力层时本就不该有面上 agent。
    """
    try:
        from ..qd_hooks import _hooks_now
        return _hooks_now().get("active_agent")
    except Exception:
        return None


def _call_tool_layer(provider, name: str, arguments: dict) -> Dict[str, Any]:
    """F4（2026-10-17）：同名遮蔽 —— **直接激活并执行工具层实现**，不再让模型白跑一轮。

    「工具层 > 能力层」的语义就是"用工具层那份"：既然同名工具存在，派发层就把它
    执行掉、把结果原样交给模型（与工具面执行的是同一实现），而不是回一句错误让
    模型再发一次 tool call（run10 实测：模型据此绕了 3 轮空转）。
    顺带把该工具激活进本轮工具面（`activate_tools`），使后续该工具**直调**也可用；
    无活跃 agent（脚本直连）时跳过激活，仅执行。
    执行失败/取不到实现才退为可执行指引（fail-open，不阻断）。
    """
    agent = _active_tool_face()
    if agent is not None:
        try:
            agent.activate_tools([name])      # 让同名工具进本轮工具面（后续直调）
        except Exception:
            pass
    getter = getattr(provider, "get", None)
    fn = getter(name) if getter is not None else None
    if fn is None:
        return {"error": f"工具层同名工具 {name} 不可通过派发层执行，"
                         f"请用 search_tools 激活后直接调用"}
    try:
        result = fn(**(arguments or {}))
    except Exception as e:
        return {"error": f"工具层同名工具 {name} 执行失败: {e}（请直接调用该工具）"}
    if isinstance(result, dict):
        # ★ 与 G7② 同口径：标注键 name 在后，不被工具返回里的 name 覆盖（留痕不失真）
        return {**result, "name": name}
    return {"name": name, "result": result}


def call_capability(name: str, arguments: dict = None) -> Dict[str, Any]:
    """调用一个准入能力函数（经护栏：写前缀复核/超时/结果体积）。低优先级派发。

    Args:
        name: 能力函数名（list_capabilities/search_capabilities 返回的 name）
        arguments: 传给函数的关键字参数

    Returns:
        {"name": ..., "result": ...} 或 {"error": ...}
    """
    import importlib
    from ..modpath import import_module

    loader = _load_loader()
    # 工具层 > 能力层：同名工具存在时**直接执行工具层实现**（F4，2026-10-17）。
    # 原实现回一句错误让模型改用工具层 ⇒ 模型需多花一轮重发 tool call（run10 实证绕 3 轮）。
    from ._base import ToolProvider
    provider = ToolProvider.get_default()
    if provider is not None and name in provider:
        return _call_tool_layer(provider, name, arguments)

    # ★ 2026-10-14 G1：上面**只判同名**，与另两个执行点不同口径 ——
    #   注册期 `capabilities/loader.py::register_capabilities` 用「同名 + 近重名」
    #   （`_shadows_domain_tool`）、预选期 `lint_selection` R0 用 `near_dup_tool_names`。
    #   后果：名字不同但功能近重的能力（如 `get_fund_flow_daily` 对上域内
    #   `get_fund_flow`）在注册期被判让位（根本不注册），派发期却能照常执行 ——
    #   让位裁定被旁路，工具层 > 能力层形同虚设。此处补上近重名判据，三处同口径。
    if provider is not None:
        shadow = loader._shadows_domain_tool(name, provider)
        if shadow:
            # ⚠ `(generic)` 是 `_shadows_domain_tool` 对通用名（daily/quote…）的返回值，
            #   不是工具名 —— 判 truthiness 即可，别去工具表里比对它。
            where = "" if shadow == "(generic)" else f" {shadow}"
            return {"error": f"能力 {name} 已让位给工具层{where}（同名/近重名，"
                             f"工具层优先），请改用工具层工具"}

    entry = None
    for mod_path, fn_name, timeout_s, max_chars in (loader.load_admitted() or []):
        if fn_name == name:
            entry = (mod_path, fn_name, timeout_s, max_chars)
            break
    if entry is None:
        return {"error": f"能力不存在或未准入: {name}", "available": [m.get("name") for m in _admitted_meta()][:20]}

    mod_path, fn_name, timeout_s, max_chars = entry
    try:
        fn = getattr(import_module(mod_path), fn_name)
    except Exception as e:
        return {"error": f"能力导入失败: {e}"}
    wrapped = loader._wrap_guards(fn, timeout_s, max_chars, fn_name)
    try:
        result = wrapped(**(arguments or {}))
    except Exception as e:
        return {"error": f"能力执行失败: {e}"}
    if isinstance(result, dict):
        # ★ 2026-10-14 G7②：展开顺序必须是 **result 在前、标注键在后**。
        #   原写法 `{"name": fn_name, **result}` 让能力返回值里的 `name` **赢**，
        #   trace/追责里记到的就成了能力内部自报的名字（留痕失真）。
        #   其余字段仍原样透传，契约形状不变。
        return {**result, "name": fn_name}
    return {"name": fn_name, "result": result}
