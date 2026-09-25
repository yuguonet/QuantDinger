# -*- coding: utf-8 -*-
"""tools/finance/kline_tools.py — 日线 K 包装（compact list + 统一信封）

红字治理：能力层 `daily` 大结果会被落盘成 `{note,file,preview}`，
模型写 `kline[0]['c']` 直接 KeyError。本包装返回**可下标的 bars 列表**。
"""
from __future__ import annotations

from typing import Any, Dict, List


def daily(code: str, days: int = 120, as_of: str = None) -> Dict[str, Any]:
    """历史日线（qfq），返回统一信封 + 可直接下标的 bars。

    Returns:
        {count, data:{code:[{t,o,h,l,c,v},...]}, bars:[...]} 单码顶层镜像。
        `bars` 按 time 升序；字段短键 t/o/h/l/c/v（兼容 time/open/... 别名）。
        失败 → {"error": ...}
    """
    try:
        from app.market_cn.auto.core.data.hub import daily as _hub_daily
    except Exception as e:
        return {"error": f"hub.daily 不可用: {e}", "retriable": True}

    codes = [c.strip() for c in str(code).split(",") if c.strip()][:5]
    out: Dict[str, Any] = {}
    data: Dict[str, Any] = {}
    for c in codes:
        try:
            raw = _hub_daily(c, int(days) or 120, as_of) or []
        except Exception as e:
            data[c] = {"error": str(e)}
            continue
        bars = []
        for b in raw:
            if not isinstance(b, dict):
                continue
            t = b.get("time") or b.get("t") or b.get("date") or b.get("day")
            o, h, l, cl = b.get("open", b.get("o")), b.get("high", b.get("h")), b.get("low", b.get("l")), b.get("close", b.get("c"))
            v = b.get("volume", b.get("v"))
            if cl is None:
                continue
            bars.append({
                "t": t, "o": float(o or cl), "h": float(h or cl),
                "l": float(l or cl), "c": float(cl), "v": float(v or 0),
                "time": t, "open": float(o or cl), "high": float(h or cl),
                "low": float(l or cl), "close": float(cl), "volume": float(v or 0),
            })
        data[c] = bars

    if len(codes) == 1:
        c = codes[0]
        bars = data.get(c) or []
        out = {"count": len(bars), "data": {c: bars}, "bars": bars}
        # 顶层镜像，兼容 kline[0]['c']
        return out
    return {"count": len(data), "data": data}
