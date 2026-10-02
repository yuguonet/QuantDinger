# -*- coding: utf-8 -*-
"""工具可用性探测 —— 启动期自检，把"能不能用"写进工具地图（2026-10-01）。

背景（根因）：web_search 依赖 BOCHA_AI_API_KEY / TAVILY_API_KEYS / baidusearch /
SEARXNG_BASE_URL 四路引擎之一。KEY 全缺时，**工具面依然挂着 web_search**，
模型照常调用、次次返回"所有引擎均失败"——它被放鸽子之后只能瞎答（天气案例）。
模型侧无从预知，只能先浪费一轮调用、甚至据此编造。

解法：**启动期探测一次**（只做本地配置检查，不发网络请求），把结论注入系统提示的
工具地图。模型从一开始就知道"web_search 不可用"，从而直接改口如实说明，
而不是先撞墙再瞎编。

设计约束：
  - 本模块**不是工具面**：已在 tools/base.py 的 `_SKIP_FILES` 里排除扫描，
    其公开函数不会被注册成模型可调用的工具（避免白占 token / 被误调）。
  - 探测只判"配置是否具备"，不判"网络是否可达"（后者时延不可控且会拖慢启动）。
    需要真实连通性验证时设 `QD_TOOL_PROBE_LIVE=1`，会真实发一次 1 条的搜索。
  - 结果进程内缓存（默认 10 分钟），`probe_tools(force=True)` 可强制重探。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CACHE: Optional[Dict[str, Any]] = None
_CACHE_TS = 0.0
_CACHE_TTL = float(os.getenv("QD_TOOL_PROBE_TTL", "600"))  # 秒


def _load_web_search_module():
    """按两种导入根（app.agent.tools.* / tools.*）取 web_search_tools 模块。"""
    try:
        from . import web_search_tools as m  # type: ignore
        return m
    except Exception:
        pass
    # agent 包统一**裸名**导入（移植性：包名不硬编码进 300+ 处 import），
    # 长名仅作兜底，顺序不能反——先短名，命中就不会再试长名。
    for mod_name in ("tools.web_search_tools", "app.agent.tools.web_search_tools"):
        try:
            return importlib.import_module(mod_name)
        except Exception:
            continue
    return None


def _probe_web_search() -> Dict[str, Any]:
    """web_search 可用性：四引擎任一可用即整体可用。"""
    m = _load_web_search_module()
    if m is None:
        return {"available": False, "reason": "web_search_tools 模块不可用", "engines": {}}

    engines = {
        # bocha / tavily 看 KEY 是否配置；baidu 看包是否安装；searxng 看 URL 是否配置
        "bocha": bool(getattr(m, "_BOCHA_API_KEY", "")),
        "tavily": bool(getattr(m, "_TAVILY_API_KEYS", [])),
        "baidu": importlib.util.find_spec("baidusearch") is not None,
        "searxng": bool(getattr(m, "_SEARXNG_URL", "")),
    }
    usable = [k for k, v in engines.items() if v]
    info: Dict[str, Any] = {
        "available": bool(usable),
        "engines": engines,
        "reason": ("可用引擎: " + ", ".join(usable)) if usable else "所有引擎均未配置（BOCHA/TAVILY KEY 缺失，baidusearch 未安装，SEARXNG 未配置）",
    }

    # 真实连通性探测（可选）：配置说可用 ≠ 打得通（KEY 失效/欠费/被墙都是常见态）
    if usable and os.getenv("QD_TOOL_PROBE_LIVE", "").lower() in ("1", "true", "yes"):
        try:
            r = m.web_search("QuantDinger 连通性自检", count=1, freshness="")
            live_ok = bool(r.get("success"))
            info["live"] = live_ok
            info["available"] = live_ok
            if not live_ok:
                info["reason"] = f"实探失败: {str(r.get('error', ''))[:120]}"
        except Exception as e:  # 探测失败不改变"配置可用"结论，只记录
            info["live"] = False
            info["reason"] = f"实探异常: {e}"
    return info


# 探测表：工具名 → 探测函数。新增依赖外部凭据的工具，在这里加一行即可。
_PROBES = {
    "web_search": _probe_web_search,
}


def probe_tools(force: bool = False) -> Dict[str, Any]:
    """探测全部登记工具的可用性（带进程内缓存）。

    Args:
        force: True 强制重探（忽略缓存）。

    Returns:
        {tool_name: {"available": bool, "reason": str, ...}, ...}
    """
    global _CACHE, _CACHE_TS
    if not force and _CACHE is not None and (time.time() - _CACHE_TS) < _CACHE_TTL:
        return dict(_CACHE)
    out: Dict[str, Any] = {}
    for name, fn in _PROBES.items():
        try:
            out[name] = fn()
        except Exception as e:  # 探测本身炸了 → 判不可用但不拖垮启动
            logger.warning("[可用性探测] %s 探测异常: %s", name, e)
            out[name] = {"available": False, "reason": f"探测异常: {e}"}
    _CACHE, _CACHE_TS = out, time.time()
    return dict(out)


def availability_text(tools: Dict[str, Any] | None = None) -> str:
    """把探测结果渲染成给模型看的一行文本（进系统提示的工具地图）。"""
    info = tools if tools is not None else probe_tools()
    if not info:
        return "（未探测）"
    lines = []
    for name, d in sorted(info.items()):
        flag = "可用" if d.get("available") else "不可用"
        lines.append(f"- {name}: {flag} —— {d.get('reason', '')}")
    return "\n".join(lines)
