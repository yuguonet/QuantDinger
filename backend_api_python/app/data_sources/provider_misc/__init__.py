# -*- coding: utf-8 -*-
"""provider_misc —— 交易日历 / 个股资金流 的 provider 目录（族内作用域，同 provider_pool 结构）。"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any, Dict, List

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


def get_providers(kind: str) -> List[Any]:
    autodiscover()
    out = []
    for cls in list(_REGISTRY.values()):
        if kind in (getattr(cls, "capabilities", None) or {}):
            out.append(cls())
    out.sort(key=lambda p: int(getattr(p, "priority", 100) or 100))
    return out


def all_providers() -> List[Any]:
    autodiscover()
    return list(_REGISTRY.values())
