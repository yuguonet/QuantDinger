"""strategies/common/entry.py — 入场语义工具（公共接口）。

P0 搬移（2026-10-12 策略接口统一方案）：收拢 strategies/base.py 中的
entry_fill_price / entry_gap_ok / entry_mode_is_close 纯函数。
纯搬移，零行为改。
"""


def entry_mode_is_close(entry: dict) -> bool | None:
    """entry.mode → 是否收盘/盘中入场 (True) vs 次日开盘 (False); 未声明 → None。"""
    m = (entry or {}).get("mode")
    if m in ("close", "intraday_window"):
        return True
    if m == "next_open":
        return False
    return None


def entry_fill_price(entry: dict, bar: dict) -> float:
    """按 entry.price 基准取入场价。

    price=open  → bar.open
    price=close → bar.close / bar.last (盘中快照的 close 等价物)
    缺省        → 有 close/last 用之, 否则 open (盘中策略天然 close, 日线策略天然 open)
    """
    p = (entry or {}).get("price")
    if p == "open":
        return float(bar.get("open") or 0)
    if p == "close":
        return float(bar.get("close") or bar.get("last") or 0)
    return float(bar.get("close") or bar.get("last") or bar.get("open") or 0)


def entry_gap_ok(entry: dict, gap_pct: float) -> bool:
    """按 entry.gap_min / gap_max 检查跳空 % (gap_pct 为正=高开, 负=低开)。

    未设的边界 = 不限; 两键都未设 = 恒 True (等价于无 gap 过滤)。
    """
    e = entry or {}
    lo, hi = e.get("gap_min"), e.get("gap_max")
    if lo is not None and gap_pct < float(lo):
        return False
    if hi is not None and gap_pct > float(hi):
        return False
    return True
