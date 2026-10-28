# -*- coding: utf-8 -*-
"""coord_misc — 交易日历 / 个股资金流族协调器

【模式】全部 sequential（各上游一次返回所需粒度；paged 留给真分页上游）。
【状态】只实现、不接入；PENDING_KINDS 中的 kind 尚未迁移 provider。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import NotSupported, is_object, is_rows, run
from app.data_sources.provider_misc import get_providers

DEFAULT_TIMEOUT = 8.0

#: 尚未迁移 provider 的 kind（coordinate 会显式报"未迁移"，不静默）
PENDING_KINDS: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CapSpec:
    kind: str
    mode: str
    params: Tuple[str, ...]
    timeout_env: str
    validator: Callable[[Any], bool] = is_rows
    timeout: float = DEFAULT_TIMEOUT
    page_size: int = 0
    max_pages: int = 1


CAPS: Dict[str, CapSpec] = {
    # trading_calendar: 交易日历整表（akshare trade_date_hist_sina，1990 起全量）。
    # validator=is_rows：上游给的是"日期列表"，不是单对象；早期写成 is_object
    # 导致 akshare provider 的信封恒判不合协议（只有 delegating 的 sina 源能过）。
    # params=()：能力是"整表"，不是"某一天" —— provider 签名无参，早前声明的
    # `date` 会让 dispatch 传参 TypeError（2026-10-28 实测）。单日判定由调用方
    # 在本层返回的集合上做（本地集合查询，零额外取数）。
    "trading_calendar": CapSpec("trading_calendar", "sequential", (),
                                "QD_EXT_TIMEOUT_CALENDAR", is_rows, timeout=30.0),
    "stock_flow_daily": CapSpec("stock_flow_daily", "sequential", ("code", "days"),
                                "QD_EXT_TIMEOUT_STOCK_FLOW_DAILY", is_rows),
    # stock_flow_rt: 当日个股资金流（新浪 1 点近似 → 东财 push2 分钟级 240 点兜底）。
    # 两个 provider 的**行数/粒度不同**（1 行 vs 240 行），但行键同构
    # {time, main_net, small_net, mid_net, large_net, super_net}，消费方按 data 自行解释。
    # timeout=90: 东财 push2 限频时 _em_get 要跑 2 host × 2 重试 × 3s 退避。
    "stock_flow_rt": CapSpec("stock_flow_rt", "sequential", ("code",),
                             "QD_EXT_TIMEOUT_STOCK_FLOW_RT", is_rows, timeout=90.0),

}


def _timeout(spec: CapSpec) -> float:
    try:
        return float(os.getenv(spec.timeout_env, "") or spec.timeout)
    except ValueError:
        return spec.timeout


def list_kinds() -> list:
    return sorted(CAPS)


def coordinate(kind: str, **params) -> Dict[str, Any]:
    if kind in PENDING_KINDS:
        return {"kind": kind, "ok": False, "source": "", "mode": "", "count": 0,
                "data": None, "error": "kind 未迁移 provider（见方案 §10-C 替换顺序）"}
    spec = CAPS.get(kind)
    if spec is None:
        return {"kind": kind, "ok": False, "source": "", "mode": "", "count": 0,
                "data": None,
                "error": "unknown kind: %s（已注册: %s）" % (kind, ", ".join(list_kinds()))}
    unknown = [k for k in params if k not in spec.params]
    if unknown:
        return {"kind": kind, "ok": False, "source": "", "mode": spec.mode, "count": 0,
                "data": None,
                "error": "unexpected params: %s（该 kind 接受: %s）"
                         % (", ".join(unknown), ", ".join(spec.params) or "无")}
    out = run(spec.mode, get_providers(kind), kind, params, spec.validator,
              timeout=_timeout(spec), page_size=spec.page_size, max_pages=spec.max_pages)
    data = out.data
    count = len(data.get("rows") or []) if isinstance(data, dict) and "rows" in data \
        else (1 if isinstance(data, dict) and data else 0)
    return {"kind": kind, "ok": bool(out.ok), "source": out.source, "mode": out.mode,
            "count": count, "data": data, "error": out.error}
