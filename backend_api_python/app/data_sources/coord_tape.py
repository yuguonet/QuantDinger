# -*- coding: utf-8 -*-
"""coord_tape — 分笔/盘口族协调器（ticks / orderbook）。

【职责】声明本族能力表 → 取候选源 → 交给 dispatch（同 coord_pool，不重复实现调度）。

【多源】`orderbook` 有 4 个上游（mootdx 主、sina/tencent/eastmoney_dc 兜底）——本族是
少数真多源之一。mootdx 挂了后面顶上，这就是"源坏了随时换"的落点。
`ticks_today` 同为 4 源；`ticks_history` 2026-10-29 起只剩 sina（东财
RPT_TRANSACTION_DET 报表下线、腾讯无历史通道，两者原恒空 ⇒ 已摘，见各自 docstring）。

【口径】`orderbook` 行结构 = {code, name, price, open, high, low, last_close, change,
change_percent, volume(股), amount(元), bid/ask}（2026-10-29 统一：sina/eastmoney_dc
原输出 normalize_quote 形状 last/changePercent/previousClose，与外围 tape.py 消费的
price/last_close/change_percent 对不上，主源命中时 7 个字段恒 0）。

【模式】全 sequential（与生产 `tape.py` 的回退顺序一致：mootdx → tencent）。
tick 级 race 会把 TCP 连接数翻倍，收益极小。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from app.data_sources.dispatch import is_object, is_rows, run, run_batch
from app.data_sources.provider_tape import get_providers

DEFAULT_TIMEOUT = 8.0

# 批量分发节奏（限频上游防突发：新浪/东财连发会进 RST 突发窗；tencent 为非 RST 并发源，
# 串行回退态仍按码间 pause 限频。可被 env 覆盖，见 §红线「阈值走 env」）。
BATCH_PAUSE = float(os.getenv("QD_BATCH_PAUSE_TAPE", "2.0"))            # item 间歇秒
BATCH_RETRY_PAUSE = float(os.getenv("QD_BATCH_RETRY_PAUSE_TAPE", "8.0"))  # 重试池翻窗等待秒


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
    "orderbook": _spec("orderbook", ("code", "codes"), "QD_EXT_TIMEOUT_ORDERBOOK", is_object),
    "ticks_today": _spec("ticks_today", ("code", "limit", "codes"),
                         "QD_EXT_TIMEOUT_TICKS_TODAY", is_rows),
    "ticks_history": _spec("ticks_history", ("code", "date", "limit", "codes"),
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

    # 批量入口: codes=[...] → dispatch.run_batch（主池+重试池；tencent 声明 batch_kinds
    # 走 fanout_codes 多股并发，sina/eastmoney/mootdx 为限频 RST 源保持逐码串行）。
    codes = params.pop("codes", None)
    if codes is not None:
        if isinstance(codes, (str, bytes)):
            codes = [codes]
        out = run_batch(spec.mode, get_providers(kind), kind, list(codes), params, "code",
                        spec.validator, timeout=_timeout(spec), page_size=0, max_pages=1,
                        pause=BATCH_PAUSE, retry_pause=BATCH_RETRY_PAUSE, max_retries=1,
                        batch_item_key="codes")
        rows = (out.data or {}).get("rows") or []
        count = sum(1 for r in rows if r.get("rows"))
        return {"kind": kind, "ok": bool(out.ok), "source": out.source, "mode": out.mode,
                "count": count, "data": out.data, "error": out.error}

    out = run(spec.mode, get_providers(kind), kind, params,
              spec.validator, timeout=_timeout(spec))

    data = out.data
    # 单码返回信封：rows 类 data={"rows":[...]} ⇒ 计行数；object 类 data={"object":...}
    # ⇒ object 为 dict 计 1、为 None（合法「暂无」）计 0。按 validator 判定，避免把扁平
    # 数据行当成需再解包的嵌套信封（此前错把每行 e.get("rows") 判空导致 count 恒 0）。
    if spec.validator is is_rows:
        count = len((data or {}).get("rows") or [])
    else:
        count = 1 if isinstance((data or {}).get("object"), dict) else 0

    return {
        "kind": kind,
        "ok": bool(out.ok),
        "source": out.source,
        "mode": out.mode,
        "count": count,
        "data": data,
        "error": out.error,
    }
