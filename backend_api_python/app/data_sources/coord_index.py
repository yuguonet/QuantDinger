# -*- coding: utf-8 -*-
"""coord_index — 指数/北向族协调器（7 kind 待迁）

【模式】全部 sequential（各上游一次返回所需粒度；paged 留给真分页上游）。
【状态】只实现、不接入；PENDING_KINDS 中的 kind 尚未迁移 provider。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import NotSupported, is_object, is_rows, run, run_batch
from app.data_sources.provider_index import get_providers

DEFAULT_TIMEOUT = 8.0

# 批量分发节奏（限频上游防突发：新浪/东财连发会进 RST 突发窗，2026-09-12/10-27 实测）
BATCH_PAUSE = 2.0        # item 间歇秒
BATCH_RETRY_PAUSE = 8.0  # 重试池翻窗等待秒

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
    # index_kline: 指数K线统 kind，周期走 timeframe 参数（对齐 coordinator 的
    # fetch_kline(symbol, timeframe, limit) 语义）：timeframe="1D"/"5m"，
    # limit=该周期 bar 条数。row 口径随周期不同：1D→{date,...}，5m→{time,...}。
    # codes=[...] 批量入口（6 位码列表）→ dispatch.run_batch 逐码多源分发。
    "index_kline": CapSpec("index_kline", "sequential", ("code", "codes", "timeframe", "limit"),
                           "QD_EXT_TIMEOUT_INDEX_KLINE", is_rows),
    # index_fflow: 指数当日分钟资金流（净额累计、元、bar 起始时刻）。
    # 仅当日 240 根 ⇒ limit=240 即当日全量，断采不可回补。
    # timeout=90: EM push2 IP 限频 RST 突发窗下 _em_get 要跑 2 host × 3 退避重试，
    # 默认 8s 预算会在重试链跑完前把 future 判超时（2026-10-27 实测）。
    "index_fflow": CapSpec("index_fflow", "sequential", ("code", "codes", "limit"),
                           "QD_EXT_TIMEOUT_INDEX_FFLOW", is_rows, timeout=90.0),
    # index_quote: 指数实时快照（腾讯 qt.gtimg.cn 真·多股单请求批量；mootdx 兜底）。
    # 接受 codes=[...]（一次性批量）或 code=单码；不传返回 provider 默认主要指数。
    # codes 入口 → dispatch.run_batch：腾讯声明 batch_kinds 走并发批量分支（单 HTTP 多股），
    # mootdx 不声明 ⇒ 作为串行兜底按 code= 逐码补（2026-10-27 接通，此前 params 为空、无批量入口）。
    "index_quote": CapSpec("index_quote", "sequential", ("code", "codes"),
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
    # 批量入口: codes=[...] → dispatch.run_batch 逐码完整多源链（主池+重试池）
    codes = params.pop("codes", None)
    if codes is not None:
        if isinstance(codes, (str, bytes)):
            codes = [codes]
        out = run_batch(spec.mode, get_providers(kind, timeframe=params.get("timeframe")),
                        kind, list(codes), params, "code", spec.validator,
                        timeout=_timeout(spec), page_size=spec.page_size,
                        max_pages=spec.max_pages,
                        pause=BATCH_PAUSE, retry_pause=BATCH_RETRY_PAUSE, max_retries=1,
                        batch_item_key="codes")
        rows = (out.data or {}).get("rows") or []
        count = sum(len(r.get("rows") or []) for r in rows)
        return {"kind": kind, "ok": bool(out.ok), "source": out.source, "mode": out.mode,
                "count": count, "data": out.data, "error": out.error}

    out = run(spec.mode, get_providers(kind, timeframe=params.get("timeframe")),
              kind, params, spec.validator,
              timeout=_timeout(spec), page_size=spec.page_size, max_pages=spec.max_pages)
    data = out.data
    count = len(data.get("rows") or []) if isinstance(data, dict) and "rows" in data \
        else (1 if isinstance(data, dict) and data else 0)
    return {"kind": kind, "ok": bool(out.ok), "source": out.source, "mode": out.mode,
            "count": count, "data": data, "error": out.error}
