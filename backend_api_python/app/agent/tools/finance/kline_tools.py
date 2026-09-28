# -*- coding: utf-8 -*-
"""tools/finance/kline_tools.py — 日线 K 包装（compact list + 统一信封）

红字治理：能力层 `daily` 大结果会被落盘成 `{note,file,preview}`，
模型写 `kline[0]['c']` 直接 KeyError。本包装返回**可下标的 bars 列表**。
"""
from __future__ import annotations

from typing import Any, Dict, List


def daily(code: str, days: int = 120, as_of: str = None) -> Dict[str, Any]:
    """历史日线（前复权）。

    Args:
        code: 股票代码，如 "600000"。多码用逗号分隔，最多 5 个。
        days: 最近多少天，默认 120。
        as_of: 截止日期（YYYY-MM-DD），可选。

    Returns:
        dict 信封格式（不是 DataFrame）。
        单码 → {"bars": list[dict], "count": int, "data": {CODE: bars}}
        多码 → {"data": {CODE1: list, CODE2: list}}  （无顶层 bars）
        失败 → {"error": str, "retriable": bool}

        bars 元素: {"t","o","h","l","c","v"} 短键 + {"time","open",...} 长键别名。
        切片/迭代用 result["bars"] — 顶层 dict 不能直接切片。
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
