"""core/runtime/gate_stdlib.py — 门表 DSL 标准库（跨策略共享的通用门函数）。

这些是真正被多个策略门表共用的通用判定/特征取值函数（过热滤波/因子/板块结构/参数
解析），与 strategies/<key>.py 里的策略私有门函数分离。经 register_function 挂入门表
求值器，由 functions.build_funcs 按名解析（策略私有函数优先，stdlib 兜底）。

as-of 纪律：所有函数只用 ctx.bars[0..i]（≤ 决策日），绝不读未来。
"""
from __future__ import annotations

from typing import Any

from app.market_cn.auto.core.market import is_limit_up
from app.market_cn.auto.core.runtime.functions import (
    Ctx, _closes_upto, register_function,
)


def obv_rising(ctx: Ctx, window: int = 5) -> bool:
    """OBV 近 window 日趋势上升（镜像 v1 scan_signals 的 OBV 块）。

    v1：累计 OBV 自 max(0,i-20) 起；obv_list[-1]-obv_list[-window] > 0 才通过（上升）；
    长度不足 window → 不拦截（fail-open，与 v1 一致，且 i>=25 时必满足）。
    """
    bars = ctx.bars
    i = ctx.i
    start = max(0, i - 20)
    obv = 0.0
    obv_list: list = []
    for j in range(start, i + 1):
        if j > 0:
            c0 = Ctx._f(bars[j], "close")
            c1 = Ctx._f(bars[j - 1], "close")
            if c0 > c1:
                obv += Ctx._f(bars[j], "volume")
            elif c0 < c1:
                obv -= Ctx._f(bars[j], "volume")
        obv_list.append(obv)
    if len(obv_list) < window:
        return True
    return (obv_list[-1] - obv_list[-window]) > 0


def no_lu_last(ctx: Ctx, days: int = 10) -> bool:
    """前 days 日（不含决策日 i，区间 (i-days, i)）无涨停 —— 镜像 v1 的 has_recent_lu 取反。"""
    bars = ctx.bars
    i = ctx.i
    bt = ctx.board_type
    mk = ctx.market
    return not any(
        is_limit_up(float(bars[j]["close"]), float(bars[j - 1]["close"]), bt, mk)
        for j in range(max(1, i - days), i)
    )


def macd_hist_lt(ctx: Ctx, thresh: float = 2.0) -> bool:
    """MACD柱 < thresh（过热过滤的 MACD 半边）。

    镜像 v1：closes=closes[:i+1]；calc_macd 长度守卫 `n < 35 → None` → 不拦截（fail-open）。
    计算与 calc_macd 逐字一致（from-0 EMA，柱=2*(DIF-DEA)）。返回 True = 未过热（放行）。
    """
    closes = _closes_upto(ctx)
    m = len(closes)
    if m < 35:                      # 慢线26+信号9；不足 → None → fail-open
        return True
    k_f, k_s, k_sig = 2 / 13.0, 2 / 27.0, 2 / 10.0
    ef = [0.0] * m
    es = [0.0] * m
    ef[0] = es[0] = closes[0]
    for j in range(1, m):
        ef[j] = closes[j] * k_f + ef[j - 1] * (1 - k_f)
        es[j] = closes[j] * k_s + es[j - 1] * (1 - k_s)
    dif = [ef[j] - es[j] for j in range(m)]
    dea = [0.0] * m
    dea[0] = dif[0]
    for j in range(1, m):
        dea[j] = dif[j] * k_sig + dea[j - 1] * (1 - k_sig)
    hist = 2.0 * (dif[m - 1] - dea[m - 1])
    return hist < thresh


def boll_bw_ok(ctx: Ctx, thresh: float = 45.0, n: int = 20, mult: float = 2.0) -> bool:
    """布林带宽 < thresh%（过热过滤的 BOLL 半边）。

    镜像 v1：closes=closes[:i+1]；calc_bollinger_bw 长度守卫 `len < period → None` 或
    mid<=0 → None → 不拦截（fail-open）。带宽 = (upper-lower)/mid*100，总体标准差（/n）。
    返回 True = 未过热（放行）。
    """
    closes = _closes_upto(ctx)
    m = len(closes)
    if m < n:
        return True
    window = closes[-n:]
    mid = sum(window) / n
    if mid <= 0:
        return True
    var = sum((x - mid) ** 2 for x in window) / n
    std = var ** 0.5
    bw = (2.0 * mult * std) / mid * 100.0
    return bw < thresh


def ret_n(ctx: Ctx, n: int = 20) -> float:
    """截至决策日 i 的 n 日累计收益% = (close(i)/close(i-n) - 1)*100。

    镜像 v1 因子2: ret_20d = (d0.close/bars[i-20].close - 1)*100，且
    `i<20 或 bars[i-20].close<=0 → 拦截`。本函数在这些边界返回 -1e9（使
    `ret_n(n) >= ret_20d_min` 自然失败 = 拦截），与 v1 逐笔等价。
    """
    i = ctx.i
    if i - n < 0:
        return -1e9
    c = Ctx._f(ctx.bars[i], "close")
    pc = Ctx._f(ctx.bars[i - n], "close")
    if c <= 0 or pc <= 0:
        return -1e9
    return (c / pc - 1) * 100


def d1_vol_ratio(ctx: Ctx, n: int = 5) -> float:
    """D-1 量 / 前 n 日均量（镜像 v1 因子4 的 vol_ma5_d1 口径）。

    v1：vol_ma5_d1 = mean(volume[i-6..i-2])（前5日，不含D-1）；d_1.volume/ma5 >= 1.5 → 拦截。
    `i>=6` 才计（否则跳过=放行）；ma<=0 → 放行（分母非正不拦截）。
    返回比值；边界（i<n+1 或 ma<=0）返回 0.0（< d_1_vol_max → 放行）。
    """
    i = ctx.i
    if i < n + 1:
        return 0.0
    window = [Ctx._f(ctx.bars[j], "volume") for j in range(i - n - 1, i - 1)]
    ma = sum(window) / n if window else 0.0
    if ma <= 0:
        return 0.0
    return Ctx._f(ctx.bars[i - 1], "volume") / ma


def is_approx_limit_up(ctx: Ctx) -> bool:
    """D0 近似涨停判定（镜像 v1 因子1 的 0.98x 板块阈值近似）。

    v1：threshold = 板块名义幅度 × 容差；通过 = (d0.close/d_1.close-1) >= threshold。
    d_1.close<=0 或 d0.close<=0 → 拦截（与 v1 顶部 d_1/d_2 close<=0 早返回同效，这里只判 d_1）。
    阈值**只从 ctx.market 取**（core 不写死市场常量）；口径与 `core.market.is_limit_up` 同源，
    故本函数直接委托 —— 避免出现第二份涨跌停实现。
    """
    i = ctx.i
    if i < 1:
        return False
    c = Ctx._f(ctx.bars[i], "close")
    pc = Ctx._f(ctx.bars[i - 1], "close")
    if pc <= 0 or c <= 0:
        return False
    return is_limit_up(c, pc, ctx.board_type, ctx.market)


def board_height(ctx: Ctx) -> int:
    """截至决策日 i 的连续涨停天数（板高）。镜像 relay3.consecutive_limit_ups。"""
    bars = ctx.bars
    i = ctx.i
    bt = ctx.board_type
    mk = ctx.market
    h = 0
    j = i
    while j >= 1:
        if is_limit_up(float(bars[j]["close"]), float(bars[j - 1]["close"]), bt, mk):
            h += 1
            j -= 1
        else:
            break
    return h


def ma_bull(ctx: Ctx) -> bool:
    """MA5>MA10>MA20>MA60（镜像 relay3.ma_bull_arrangement，截至决策日 i）。"""
    from app.market_cn.auto.core.indicators import ma as _ma
    closes = _closes_upto(ctx)
    m5, m10, m20, m60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
    if not (m5 and m10 and m20 and m60):
        return False
    return m5 > m10 > m20 > m60


def is_bse(ctx: Ctx) -> bool:
    """是否北交所 / 新三板代码（8/4/92 开头）—— 镜像 relay3.scan_signals 的池子排除。"""
    return str(ctx.code or "").startswith(("8", "4", "92"))


def pk(ctx: Ctx, name: str):
    """板块感知参数取值：params[name] 为 {board: 值} → 按 ctx.board_type 取；标量原样返回。

    镜像 entry_modes._resolve 的 dict 语义，使同一份门表可对主板/创业板给出不同阈值
    （break 的 vol_max/drawdown_max/stop_loss/... 分板块）。
    """
    v = ctx.params.get(name)
    if isinstance(v, dict):
        if ctx.board_type in v:
            return v[ctx.board_type]
        return v.get("default")
    return v


# ---- 标准库注册（与原始 strategy_funcs 的 needs_d0 声明一致）----
register_function("obv_rising", obv_rising)                          # 非偏移
register_function("no_lu_last", no_lu_last, needs_d0=0)                # (i-days, i), 不含决策日
register_function("macd_hist_lt", macd_hist_lt)                        # 非偏移
register_function("boll_bw_ok", boll_bw_ok)                            # 非偏移
register_function("board_height", board_height)                        # 非偏移
register_function("ma_bull", ma_bull)                                  # 非偏移
register_function("is_bse", is_bse, needs_d0=0)                        # 只看代码交易所
register_function("ret_n", ret_n)                                      # 非偏移
register_function("d1_vol_ratio", d1_vol_ratio, needs_d0=0)            # D-1 量/前5日均量, 不含决策日
register_function("is_approx_limit_up", is_approx_limit_up)            # 非偏移
register_function("pk", pk, needs_d0=0)                                # 只看 params/board_type
