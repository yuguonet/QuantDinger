# -*- coding: utf-8 -*-
"""coord_index — 指数/北向/大盘/板块族协调器

【模式】除 index_kline/index_quote/sector_* 走 sequential（各上游一次返回所需粒度；
paged 留给真分页上游）外，全部 sequential。
【状态】11 个 kind 全部有 provider 落地（2026-10-28 外围接线批次 2），
PENDING_KINDS 已清空 —— 保留该常量是给将来"先声明 kind、后补 provider"的迁移用。
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
    # index_kline: 指数K线统 kind，周期走 timeframe 参数（对齐 coordinator 的
    # fetch_kline(symbol, timeframe, limit) 语义）：timeframe="1D"/"5m"，
    # limit=该周期 bar 条数。row 口径随周期不同：1D→{date,...}，5m→{time,...}。
    # codes=[...] 批量入口（6 位码列表）→ dispatch.run_batch 逐码多源分发。
    # timeout=60: 候选 4 源按 timeout/len 分预算（≈15s/源）。指数日线**回填**要走
    # akshare 全历史（限流排队 + 解析常 5~10s），默认 8s 会在 akshare 前就判超时
    # （2026-10-28 index_daily 回填实测）。成功路径单源短路，不受此值影响。
    "index_kline": CapSpec("index_kline", "sequential", ("code", "codes", "timeframe", "limit"),
                           "QD_EXT_TIMEOUT_INDEX_KLINE", is_rows, timeout=60.0),
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
    # northbound_flow: 当日北向**分钟级累计**净买入（hexin dayChart，262 点）。
    # 与 northbound_summary（当日**单点汇总**，akshare）粒度不同，不可互替。
    "northbound_flow": CapSpec("northbound_flow", "sequential", (),
                               "QD_EXT_TIMEOUT_NORTHBOUND_FLOW", is_rows, timeout=15.0),
    # northbound_daily: 北向**历史日级**（akshare hist_em 两腿合并 → hexin dayKline 备用）。
    # timeout=60: akshare 两腿各自带限流排队，默认 8s 会在排队阶段就被判超时。
    "northbound_daily": CapSpec("northbound_daily", "sequential", ("days",),
                                "QD_EXT_TIMEOUT_NORTHBOUND_DAILY", is_rows, timeout=60.0),
    "northbound_holdings": CapSpec("northbound_holdings", "sequential", ("top",),
                                   "QD_EXT_TIMEOUT_NORTHBOUND_HOLDINGS", is_rows, timeout=15.0),
    # market_flow_rt: 大盘实时资金流单条快照（sina 板块汇总 → ths 板块汇总 → 东财指数分钟）。
    # timeout=90: sina 需先拉 100 个行业板块、ths 要翻 10 页，逐源预算按 30s 计。
    "market_flow_rt": CapSpec("market_flow_rt", "sequential", (),
                              "QD_EXT_TIMEOUT_MARKET_FLOW_RT", is_rows, timeout=90.0),
    "market_flow_daily": CapSpec("market_flow_daily", "sequential", ("days",),
                                 "QD_EXT_TIMEOUT_MARKET_FLOW_DAILY", is_rows, timeout=30.0),
    # sector_flow: 板块资金流排名（新浪/同花顺仅「今日+行业」→ 东财覆盖概念与多周期）。
    "sector_flow": CapSpec("sector_flow", "sequential", ("indicator", "board_type"),
                           "QD_EXT_TIMEOUT_SECTOR_FLOW", is_rows, timeout=90.0),
    "sector_list": CapSpec("sector_list", "sequential",
                           ("board_type", "limit", "sort_by", "sort_dir"),
                           "QD_EXT_TIMEOUT_SECTOR_LIST", is_rows, timeout=45.0),
    "sector_stocks": CapSpec("sector_stocks", "sequential", ("board_code", "limit"),
                             "QD_EXT_TIMEOUT_SECTOR_STOCKS", is_rows, timeout=30.0),
    # stock_boards: 个股 → 所属板块反查（东财 push2 slist，sina 无等价通道）。
    "stock_boards": CapSpec("stock_boards", "sequential", ("code",),
                            "QD_EXT_TIMEOUT_STOCK_BOARDS", is_rows, timeout=30.0),
    # northbound_stock_hold: 个股北向持股历史（东财数据中心，按交易日聚合）。
    "northbound_stock_hold": CapSpec("northbound_stock_hold", "sequential", ("code", "days"),
                                     "QD_EXT_TIMEOUT_NORTHBOUND_STOCK_HOLD", is_rows, timeout=30.0),
    # emotion_cycle: 市场情绪周期整表（stockapi.com.cn，单源；快照落盘在 market_cn/emotion.py）。
    "emotion_cycle": CapSpec("emotion_cycle", "sequential", (),
                              "QD_EXT_TIMEOUT_EMOTION_CYCLE", is_rows, timeout=20.0),

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
