# -*- coding: utf-8 -*-
"""search_capabilities / call_capability —— 能力层低优先级派发工具。

能力层语义（沿旧设计，2026-09-30 接入 mimoagent 形态）：
  - **工具层 > 能力层**：能力函数不进主工具面（不全量下发），只经本模块按需派发；
  - 准入事实源仍是 capabilities/admission.json（人工过目固化；缺失 = 0 能力，不报错）；
  - 三闸门保留：写操作前缀硬复核 + 超时护栏 + 结果体积护栏（loader._wrap_guards）；
  - 与工具层同名/近重名的能力让位（call_capability 内硬拦，提示用工具层工具）。

admission.json 不随仓库分发（人工审核件）：把它放回 capabilities/ 即接入。
"""
from __future__ import annotations

from typing import Any, Dict, List


def _load_loader():
    from capabilities import loader as _l
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


def call_capability(name: str, arguments: dict = None) -> Dict[str, Any]:
    """调用一个准入能力函数（经护栏：写前缀复核/超时/结果体积）。低优先级派发。

    Args:
        name: 能力函数名（list_capabilities/search_capabilities 返回的 name）
        arguments: 传给函数的关键字参数

    Returns:
        {"name": ..., "result": ...} 或 {"error": ...}
    """
    import importlib

    loader = _load_loader()
    # 工具层 > 能力层：同名工具存在时硬拦，指向工具层
    from tools.base import ToolProvider
    provider = ToolProvider.get_default()
    if provider is not None and name in provider:
        return {"error": f"工具层已有同名工具 {name}（工具层优先），请直接调用该工具"}

    entry = None
    for mod_path, fn_name, timeout_s, max_chars in (loader.load_admitted() or []):
        if fn_name == name:
            entry = (mod_path, fn_name, timeout_s, max_chars)
            break
    if entry is None:
        return {"error": f"能力不存在或未准入: {name}", "available": [m.get("name") for m in _admitted_meta()][:20]}

    mod_path, fn_name, timeout_s, max_chars = entry
    try:
        fn = getattr(importlib.import_module(mod_path), fn_name)
    except Exception as e:
        return {"error": f"能力导入失败: {e}"}
    wrapped = loader._wrap_guards(fn, timeout_s, max_chars, fn_name)
    try:
        result = wrapped(**(arguments or {}))
    except Exception as e:
        return {"error": f"能力执行失败: {e}"}
    if isinstance(result, dict):
        return {"name": fn_name, **result}
    return {"name": fn_name, "result": result}
