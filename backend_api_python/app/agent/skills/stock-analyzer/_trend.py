# -*- coding: utf-8 -*-
"""T+10 / T+20 长档的纯日线趋势描述。

长档只做日线趋势分析，不复用短档的盘中量价票、不给概率、不给触发/证伪；
horizons 五档计算保留（不删能力），写作时以本字段为准。输入只有日线 bars，
无取数、无网络、无跨源拼接，判据全部为显式纯函数。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._inner import num as _num


def _ma(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return round(sum(vals[-n:]) / n, 3)


def build(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从日线算 MA20/MA60、20/60 日区间与趋势方向；缺数显式 None。"""
    closes = [c for c in (_num((b or {}).get("close")) for b in (bars or [])) if c is not None]
    if len(closes) < 60:
        return {"ok": False, "reason": f"日线不足 60 根（现 {len(closes)} 根），趋势未知",
                "trend": "未知"}
    last = closes[-1]
    ma20, ma60 = _ma(closes, 20), _ma(closes, 60)
    ma20_prev = _ma(closes[:-5], 20) if len(closes) >= 25 else None
    lo20, hi20 = min(closes[-20:]), max(closes[-20:])
    lo60, hi60 = min(closes[-60:]), max(closes[-60:])
    if None in (ma20, ma60, ma20_prev):
        return {"ok": False, "reason": "均线计算缺数", "trend": "未知"}
    pos20 = round((last / ma20 - 1) * 100, 2)
    pos60 = round((last / ma60 - 1) * 100, 2)
    slope_up = ma20 > ma20_prev
    if last > ma20 > ma60 and slope_up:
        trend = "上行"
    elif last < ma20 < ma60 and not slope_up:
        trend = "下行"
    else:
        trend = "震荡"
    basis = (f"日线收盘 {last:g}，MA20 {ma20:g}（偏离 {pos20:+}%）、MA60 {ma60:g}"
             f"（偏离 {pos60:+}%）；MA20 较 5 日前{'上行' if slope_up else '下行'}；"
             f"20 日区间 {lo20:g}~{hi20:g}，60 日区间 {lo60:g}~{hi60:g}。")
    return {"ok": True, "trend": trend, "last_close": last,
            "ma20": ma20, "ma60": ma60, "pos_vs_ma20_pct": pos20,
            "pos_vs_ma60_pct": pos60, "lo20": lo20, "hi20": hi20,
            "lo60": lo60, "hi60": hi60, "basis": basis}
