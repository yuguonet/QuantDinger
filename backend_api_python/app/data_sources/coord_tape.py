# -*- coding: utf-8 -*-
"""coord_tape — 分笔/盘口族协调器（ticks / orderbook）。

【职责】声明本族能力表 → 取候选源 → 交给 dispatch（同 coord_pool，不重复实现调度）。

【多源】`orderbook` 有 2 个上游（mootdx 主、tencent 兜底）——本族是 20 个 kind 里
少数真多源之一。mootdx 挂了 tencent 顶上，这就是"源坏了随时换"的落点。

【模式】全 sequential（与生产 `tape.py` 的回退顺序一致：mootdx → tencent）。
tick 级 race 会把 TCP 连接数翻倍，收益极小。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import is_object, is_rows, run
from app.data_sources.provider_tape import get_providers

DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class CapSpec:
    """一个 kind 的调度声明（validator 也进声明，族自包含）。"""

    kind: str
    mode: str
    params: Tuple[str, ...]
    timeout_env: str
    validator: Callable[[Any], bool]
    timeout: float = DEFAULT_TIMEOUT


def _spec(kind: str, params: Tuple[str, ...], env: str,
          validator: Callable[[Any], bool]) -> CapSpec:
    return CapSpec(kind=kind, mode="sequential", params=params,
                   timeout_env=env, validator=validator)


TAPE_CAPS: Dict[str, CapSpec] = {
    "orderbook": _spec("orderbook", ("code",), "QD_EXT_TIMEOUT_ORDERBOOK", is_object),
    "ticks_today": _spec("ticks_today", ("code", "limit"),
                         "QD_EXT_TIMEOUT_TICKS_TODAY", is_rows),
    "ticks_history": _spec("ticks_history", ("code", "date", "limit"),
                           "QD_EXT_TIMEOUT_TICKS_HISTORY", is_rows),
}


def _timeout(spec: CapSpec) -> float:
    try:
        return float(os.getenv(spec.timeout_env, "") or spec.timeout)
    except ValueError:
        return spec.timeout


def list_kinds() -> list:
    return sorted(TAPE_CAPS)


def coordinate(kind: str, **params) -> Dict[str, Any]:
    """分笔/盘口族统一入口。返回结构与 coord_pool.coordinate 完全一致。"""
    spec = TAPE_CAPS.get(kind)
    if spec is None:
        return {"kind": kind, "ok": False, "source": "", "mode": "",
                "count": 0, "data": None,
                "error": "unknown kind: %s（已注册: %s）" % (kind, ", ".join(list_kinds()))}

    unknown = [k for k in params if k not in spec.params]
    if unknown:
        return {"kind": kind, "ok": False, "source": "", "mode": spec.mode,
                "count": 0, "data": None,
                "error": "unexpected params: %s（该 kind 接受: %s）"
                         % (", ".join(unknown), ", ".join(spec.params) or "无")}

    out = run(spec.mode, get_providers(kind), kind, params,
              spec.validator, timeout=_timeout(spec))

    data = out.data
    if isinstance(data, dict) and "object" in data:
        count = 1 if isinstance(data.get("object"), dict) else 0
    elif isinstance(data, dict):
        count = len(data.get("rows") or [])
    else:
        count = 0

    return {
        "kind": kind,
        "ok": bool(out.ok),
        "source": out.source,
        "mode": out.mode,
        "count": count,
        "data": data,
        "error": out.error,
    }
