# -*- coding: utf-8 -*-
"""coord_pool — 榜单族协调器（龙虎榜 / 涨跌停池 / 炸板池 / 人气榜）。

【职责】
  只做三件事：**声明本族能力表（POOL_CAPS）→ 取本族候选源 → 交给 dispatch**。
  顺序回退 / 超时分配 / 空信封判定 / 失败计数全在 `dispatch.py`，本文件不重复实现
  （用户裁定 2026-10-26：允许一个 dispatch.py；五族只声明"哪个 kind 用哪个模式"）。

【加一个新 kind = 改两个文件】
  1. 本文件 `POOL_CAPS` 加一行；
  2. `provider_pool/<上游>.py` 的 `capabilities` 加一行 + 一个 fetch 方法。
  `coordinator.py`、其它族文件、其它 provider 目录 **零改动**。

【当前不接入系统】
  本文件不被任何生产模块 import；`market_cn/dragon_limit.py` 原样保留。
  替换期只需把调用方从 `dragon_limit.get_zt_pool(...)` 改成
  `coord_pool.coordinate("limit_up_pool", trade_date=...)`，然后删旧实现。

【模式说明】
  本族上游（akshare）一次返回全量列表，没有真实分页 ⇒ 全部用 `sequential`。
  若将来某上游改为分页，把对应 CapSpec 的 mode 改成 "paged" 并给 page_size/max_pages。
  单源时 dispatch 走单源短路：一次调用 + 整段预算，不进入候选循环。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import NotSupported, is_rows, run
from app.data_sources.provider_pool import get_providers

DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class CapSpec:
    """一个 kind 的调度声明。"""

    kind: str
    mode: str                       # sequential | race | paged
    params: Tuple[str, ...]         # 对外参数名（全部有默认值）
    timeout_env: str                # 超时环境变量名（不硬编码在逻辑里）
    validator: Callable[[Any], bool] = is_rows
    timeout: float = DEFAULT_TIMEOUT
    page_size: int = 0
    max_pages: int = 1


def _spec(kind: str, params: Tuple[str, ...], env: str, mode: str = "sequential") -> CapSpec:
    return CapSpec(kind=kind, mode=mode, params=params, timeout_env=env)


POOL_CAPS: Dict[str, CapSpec] = {
    "dragon_tiger": _spec("dragon_tiger", ("start_date", "end_date"),
                          "QD_EXT_TIMEOUT_DRAGON_TIGER"),
    "limit_up_pool": _spec("limit_up_pool", ("trade_date",),
                           "QD_EXT_TIMEOUT_LIMIT_UP_POOL"),
    "limit_down_pool": _spec("limit_down_pool", ("trade_date",),
                             "QD_EXT_TIMEOUT_LIMIT_DOWN_POOL"),
    "broken_board_pool": _spec("broken_board_pool", ("trade_date",),
                               "QD_EXT_TIMEOUT_BROKEN_BOARD_POOL"),
    "hot_rank": _spec("hot_rank", (), "QD_EXT_TIMEOUT_HOT_RANK"),
}


def _timeout(spec: CapSpec) -> float:
    try:
        return float(os.getenv(spec.timeout_env, "") or spec.timeout)
    except ValueError:
        return spec.timeout


def list_kinds() -> list:
    return sorted(POOL_CAPS)


def coordinate(kind: str, **params) -> Dict[str, Any]:
    """榜单族统一入口。

    Returns:
        {"kind", "ok", "source", "mode", "count", "data", "error"}
        data 固定为 {"rows": [...]}；ok=False 时 error 说明全部源为何失败。
    """
    spec = POOL_CAPS.get(kind)
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

    out = run(
        spec.mode,
        get_providers(kind),
        kind,
        params,
        spec.validator,
        timeout=_timeout(spec),
        page_size=spec.page_size,
        max_pages=spec.max_pages,
    )

    rows = (out.data or {}).get("rows") or []
    return {
        "kind": kind,
        "ok": bool(out.ok),
        "source": out.source,
        "mode": out.mode,
        "count": len(rows),
        "data": out.data,
        "error": out.error,
    }
