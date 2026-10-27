# -*- coding: utf-8 -*-
"""provider_index —— 指数/北向/大盘/板块族 的 provider 目录（族内作用域，同 provider_pool 结构）。"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any, Dict, List, Optional

_REGISTRY: Dict[str, Any] = {}
_LOCK = threading.Lock()
_DISCOVERED = False


def register(cls):
    with _LOCK:
        _REGISTRY[cls.name] = cls
    return cls


def autodiscover() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name == "__init__":
            continue
        importlib.import_module("%s.%s" % (__name__, mod.name))
    _DISCOVERED = True


def _kind_priority(provider: Any, kind: str, timeframe: Optional[str] = None) -> int:
    """优先级：tf_priority[timeframe] > kind_priority[kind] > 类级 priority。

    为什么需要 per-timeframe 覆盖：同一 provider 对不同周期的可信度不同
    （例：mootdx 日线 vol 可用、5m vol 字段错位 ⇒ 5m 必须排在 sina 之后），
    仅靠类级 priority 无法表达。timeframe 仅用于排序，不改变 fetch 签名。
    """
    tfp = getattr(provider, "tf_priority", None) or {}
    if timeframe and timeframe in tfp:
        return int(tfp[timeframe])
    kp = getattr(provider, "kind_priority", None) or {}
    if kind in kp:
        return int(kp[kind])
    return int(getattr(provider, "priority", 100) or 100)


def get_providers(kind: str, timeframe: Optional[str] = None) -> List[Any]:
    autodiscover()
    out = []
    for cls in list(_REGISTRY.values()):
        if kind in (getattr(cls, "capabilities", None) or {}):
            out.append(cls())
    out.sort(key=lambda p: _kind_priority(p, kind, timeframe))
    return out


def all_providers() -> List[Any]:
    autodiscover()
    return list(_REGISTRY.values())
