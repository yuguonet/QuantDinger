# -*- coding: utf-8 -*-
"""provider_tape — 分笔/盘口族（ticks / orderbook）的 provider 目录。

【与 legacy `provider/` 的唯一差别：作用域】
  legacy 的 `autodiscover()` 扫整个 `provider/`，`get_providers()` 返回**个股域**全部源。
  本包只扫自己这一层目录，`get_providers(kind)` 只返回**本族**声明了该 kind 的源
  ⇒ 族与族之间零交叉依赖，删一个族不波及别的族。

【加源 = 加一个文件】
  在本目录放 `xxx.py`，里面 `@register` 一个带 `name/priority/capabilities` 的类即可。
  不需要改本文件，也不需要改任何协调器。
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any, Dict, List

_REGISTRY: Dict[str, Any] = {}
_LOCK = threading.Lock()
_DISCOVERED = False


def register(cls):
    """类装饰器：import 即注册。同名后者覆盖前者（与 legacy 行为一致）。"""
    with _LOCK:
        _REGISTRY[cls.name] = cls
    return cls


def autodiscover() -> None:
    """只扫本包目录；幂等。"""
    global _DISCOVERED
    if _DISCOVERED:
        return
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name == "__init__":
            continue
        importlib.import_module("%s.%s" % (__name__, mod.name))
    _DISCOVERED = True


def get_providers(kind: str) -> List[Any]:
    """返回**本族**声明了该 kind 的 provider 实例，按 priority 升序。

    未知 kind 返回 []（调用方据此返回 NotSupported，而不是抛异常）。
    """
    autodiscover()
    out = []
    for cls in list(_REGISTRY.values()):
        caps = getattr(cls, "capabilities", None) or {}
        if kind in caps:
            out.append(cls())
    out.sort(key=lambda p: int(getattr(p, "priority", 100) or 100))
    return out


def all_providers() -> List[Any]:
    autodiscover()
    return list(_REGISTRY.values())
