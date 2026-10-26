# -*- coding: utf-8 -*-
"""coord_index — 指数/北向族协调器（7 kind 待迁）

【模式】全部 sequential（各上游一次返回所需粒度；paged 留给真分页上游）。
【状态】只实现、不接入；PENDING_KINDS 中的 kind 尚未迁移 provider。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import NotSupported, is_object, is_rows, run
from app.data_sources.provider_index import get_providers

DEFAULT_TIMEOUT = 8.0

#: 尚未迁移 provider 的 kind（coordinate 会显式报"未迁移"，不静默）
PENDING_KINDS: Tuple[str, ...] = ("northbound_flow", "northbound_daily", "northbound_holdings", "market_flow_rt", "market_flow_daily", "sector_flow", "sector_list")


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
    "index_kline": CapSpec("index_kline", "sequential", ("code", "days"),
                           "QD_EXT_TIMEOUT_INDEX_KLINE", is_rows),
    "index_quote": CapSpec("index_quote", "sequential", (),
                           "QD_EXT_TIMEOUT_INDEX_QUOTE", is_rows),
    "northbound_summary": CapSpec("northbound_summary", "sequential", (),
                                  "QD_EXT_TIMEOUT_NORTHBOUND_SUMMARY", is_rows),

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
