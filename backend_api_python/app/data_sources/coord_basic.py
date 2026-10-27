# -*- coding: utf-8 -*-
"""coord_basic — 基本面/基础信息族协调器

【模式】全部 sequential（各上游一次返回所需粒度；paged 留给真分页上游）。
【状态】只实现、不接入；PENDING_KINDS 中的 kind 尚未迁移 provider。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import NotSupported, is_object, is_rows, run, run_batch
from app.data_sources.provider_basic import get_providers

DEFAULT_TIMEOUT = 8.0

# 批量分发节奏（限频上游防突发：新浪/东财连发会进 RST 突发窗；basic 看板/财务为逐码场景，
# 默认比指数更轻，且可被 env 覆盖，见 §红线「阈值走 env」）。
BATCH_PAUSE = float(os.getenv("QD_BATCH_PAUSE_BASIC", "1.0"))          # item 间歇秒
BATCH_RETRY_PAUSE = float(os.getenv("QD_BATCH_RETRY_PAUSE_BASIC", "6.0"))  # 重试池翻窗等待秒

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
    "cn_fundamental": CapSpec("cn_fundamental", "sequential", ("code", "codes"),
                              "QD_EXT_TIMEOUT_CN_FUNDAMENTAL", is_object),
    "cn_financial": CapSpec("cn_financial", "sequential", ("code", "codes"),
                            "QD_EXT_TIMEOUT_CN_FINANCIAL", is_object),
    "stock_basic": CapSpec("stock_basic", "sequential", (),
                           "QD_EXT_TIMEOUT_STOCK_BASIC", is_rows),

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
    # 批量入口: codes=[...] → dispatch.run_batch（主池+重试池；basic 的财务/基本面源为
    # 限频 RST 上游，保持逐码串行 item_key="code"，batch_item_key 留待非 RST 源声明 batch_kinds）。
    codes = params.pop("codes", None)
    if codes is not None:
        if isinstance(codes, (str, bytes)):
            codes = [codes]
        out = run_batch(spec.mode, get_providers(kind), kind, list(codes), params, "code",
                        spec.validator, timeout=_timeout(spec), page_size=spec.page_size,
                        max_pages=spec.max_pages, pause=BATCH_PAUSE,
                        retry_pause=BATCH_RETRY_PAUSE, max_retries=1, batch_item_key="codes")
        rows = (out.data or {}).get("rows") or []
        count = sum(1 for r in rows if r.get("rows"))
        return {"kind": kind, "ok": bool(out.ok), "source": out.source, "mode": out.mode,
                "count": count, "data": out.data, "error": out.error}
    out = run(spec.mode, get_providers(kind), kind, params, spec.validator,
              timeout=_timeout(spec), page_size=spec.page_size, max_pages=spec.max_pages)
    data = out.data
    count = len(data.get("rows") or []) if isinstance(data, dict) and "rows" in data \
        else (1 if isinstance(data, dict) and data else 0)
    return {"kind": kind, "ok": bool(out.ok), "source": out.source, "mode": out.mode,
            "count": count, "data": data, "error": out.error}
