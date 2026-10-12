"""strategies/common/timebox.py — 时间窗判定工具（公共接口）。

P0 搬移（2026-10-12 策略接口统一方案）：收拢 knife_catch._hhmm / tail_oversold._hhmm
同源实现。纯搬移，零行为改。
"""


def hhmm(time_str: str) -> str:
    """从时间字符串提取 'HH:MM'（支持 'YYYY-MM-DD HH:MM:SS' / 'HH:MM:SS' / 'HH:MM'）。"""
    s = str(time_str)
    if len(s) >= 16:
        return s[11:16]
    return s[:5]


def in_window(hhmm_val: str, lo: str, hi: str) -> bool:
    """判断 HH:MM 是否在 [lo, hi] 闭区间内。"""
    return lo <= hhmm_val <= hi
