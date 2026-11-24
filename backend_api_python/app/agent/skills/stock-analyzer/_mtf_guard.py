# -*- coding: utf-8 -*-
"""stock-analyzer/_mtf_guard.py —— 多周期（15m/5m）与日线的日期对齐守卫。

底层 multitimeframe 把 1m 聚合为 15m/5m 后只输出 close/rsi/trend，丢掉了 time；
当 1m 源停在更早交易日时，短周期「上行」会被误读成当日分时转暖，与日线收盘矛盾。
本模块在 SA 侧补日期对齐：取日线最后一根的目标日与 1m 最后一根的日期比对，
不一致则标注 stale 及各自日期，不改原值、不删字段，由 _mtf_line 显式提示不采信。

口径：日线目标日取 bars[-1]["time"] 前 10 位；1m 日期取 _deps.intraday_bars
最后一根 time 前 10 位。任一取不到则 aligned=None（未知），不猜。
"""
from typing import Any, Dict, List, Optional

from . import _deps


def _day(value: Any) -> Optional[str]:
    s = str(value or "").strip()[:10]
    return s or None


def _daily_day(bars: Any) -> Optional[str]:
    if not isinstance(bars, (list, tuple)) or not bars:
        return None
    last = bars[-1]
    if not isinstance(last, dict):
        return None
    return _day(last.get("time") or last.get("date"))


def _minute_day(code: str) -> Optional[str]:
    try:
        bars = _deps.intraday_bars(code)
    except Exception:
        return None
    if not isinstance(bars, (list, tuple)) or not bars:
        return None
    last = bars[-1]
    return _day(last.get("time")) if isinstance(last, dict) else None


def guard(mtf: Any, code: str, bars: Any) -> Any:
    """返回带日期对齐标记的 mtf 副本；入参不被修改。"""
    if not isinstance(mtf, dict):
        return mtf
    out: Dict[str, Any] = dict(mtf)
    daily = _daily_day(bars)
    minute = _minute_day(code)
    aligned = None if (daily is None or minute is None) else (daily == minute)
    out["caliber_check"] = {
        "daily_target": daily,
        "minute_last": minute,
        "aligned": aligned,
    }
    out["stale"] = (aligned is False)
    if aligned is False:
        out["stale_reason"] = (
            f"分钟线停在 {minute}，日线目标日 {daily}；"
            "15m/5m 的 close/trend 属更早交易日，不作为当日分时信号"
        )
    return out
