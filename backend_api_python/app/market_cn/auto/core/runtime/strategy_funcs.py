"""ide/strategy_funcs.py — 策略专属函数/指标（经 register_function 挂入门表求值器）。

这些函数与 strategies/*.py 的逐字逻辑一致（对数基准），但把"策略专属、难以单表达式
表达"的判定收编为可注册函数，使门表 YAML 只写"给定 (决策日 i) 是否放行"的薄表达式。

as-of 纪律：所有函数只用 ctx.bars[0..i]（≤ 决策日），绝不读未来；非偏移函数签名
def fn(ctx, *args)，由引擎注入 ctx。

注：v1 的 OBV/MACD柱/BOLL带宽 在"信号判定"语境下是按切片 closes[0..i] 计算的，
且 MACD 有 `len < slow+signal(35)` → None 的长度守卫（过热门 fail-open）。本模块
逐字镜像这些守卫，确保门表版与 v1.backtest_stock 逐笔等价。
"""

from __future__ import annotations

from typing import Any

from app.market_cn.auto.core.market import default_market, get_board_type, is_limit_up
from app.market_cn.auto.core.runtime.functions import Ctx, register_function

# ================================================================
# V1 追板策略专属函数（镜像 strategies/v1.py + common/indicators.py）
# ================================================================


def _closes_upto(ctx: Ctx):
    """决策日因果切片 [0..i] 的收盘价序列（与 v1 的 closes=closes[:i+1] 一致）。"""
    return [Ctx._f(ctx.bars[j], "close") for j in range(ctx.i + 1)]


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


# ----------------------------------------------------------------
# V1 专属辅助（门表表达式难以单写，收编为可注册函数；逻辑与 v1.scan_signals 逐字一致）
# ----------------------------------------------------------------
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


# ================================================================
# relay3 3板接力专属函数（镜像 strategies/relay3.py，供后续转写使用）
# ================================================================


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


def relay3_features(ctx: Ctx) -> dict:
    """relay3 日线特征（逐字镜像 relay3.calc_features，截至决策日 i）。

    门表用 board_height()/ma_bull() 判资格；本函数额外给出**信号展示字段**
    （lu_vol_ratio / rsi），保证与 python 参考版 trades 逐字一致。逻辑单点维护于此。
    """
    i = ctx.i
    closes = _closes_upto(ctx)
    vols = [Ctx._f(ctx.bars[j], "volume") for j in range(i + 1)]
    feats = {"board_height": board_height(ctx), "ma_bull": 1 if ma_bull(ctx) else 0}
    # 涨停日量比 (昨日量 / 前5日均量)
    if len(vols) >= 6:
        avg5 = sum(vols[-6:-1]) / 5
        feats["lu_vol_ratio"] = round(vols[-1] / avg5, 2) if avg5 > 0 else None
    # RSI14 (relay3 口径: 取末尾 15 个收盘的 14 段差分)
    if len(closes) >= 15:
        gains, losses = [], []
        for k in range(len(closes) - 15, len(closes)):
            dd = closes[k] - closes[k - 1]
            gains.append(max(dd, 0))
            losses.append(max(-dd, 0))
        ag, al = sum(gains) / 14, sum(losses) / 14
        feats["rsi"] = round(100 - 100 / (1 + ag / al), 1) if al > 0 else 100.0
    return feats


# ================================================================
# break 断板接力策略专属函数（镜像 strategies/break_buy.py，供门表转写使用）
# ----------------------------------------------------------------
# design: 断板期的"结构"（连板→断板期→确认日）无法用单表达式表达，收编为结构计算函数
#   bk_struct/bk_feat；而 5a~5g 的**判定**（缩量/涨跌/回撤/增强过滤/均线）全部上提为
#   门表表达式（见 strategies/break.yaml）—— 使 IDE 能逐门展示"为何放行/拦截"。
# as-of：结构只读 bars[0..i]（确认日 i 之前的最后一个涨停日 + 断板期，绝不读 > i）。
# ================================================================
def _bk_ma_bull_at(bars, idx: int):
    """确认日均线多头排列 MA5>MA10>MA20（镜像 break_buy._ma_bull_at）。不足 20 日 → None。"""
    if idx + 1 < 20:
        return None
    c = [Ctx._f(bars[j], "close") for j in range(idx - 19, idx + 1)]
    m5 = sum(c[-5:]) / 5.0
    m10 = sum(c[-10:]) / 10.0
    m20 = sum(c) / 20.0
    return m5 > m10 > m20


def _bk_raw(bars, bt: str, streak_start: int, streak_end: int,
            min_streak: int, max_break_gap: int, asof: int, market=None):
    """断板期『原始结构』—— 镜像 break_buy._break_signal_at 的**结构部分**（不含 5a~5g 判定）。

    返回 None = 结构不成立（连板不足 / 断板期为空 / 越界）。判定（缩量/涨跌/回撤/增强/
    均线）由门表完成；本函数只产出结构量，使门表与参考版逐笔等价且逐门可解释。

    asof: 决策日 i —— 参考版在 bars[:i+1] 上计算（scan_signals 切片），故断板期**只扫到 i**；
          故本函数绝不能用完整 bars 向后扫（否则读到未来 bar，且 break_days 会偏大 → 翻转判定）。
    """
    streak_len = streak_end - streak_start + 1
    if streak_len < min_streak:
        return None
    break_idx = streak_end + 1
    if break_idx >= asof + 1:            # 参考版: break_idx >= len(bars[:i+1])
        return None
    limit_bar = bars[streak_end]
    limit_open = Ctx._f(limit_bar, "open")
    limit_close = Ctx._f(limit_bar, "close")
    limit_vol = Ctx._f(limit_bar, "volume")
    break_days = 0
    # 切片上界 = min(break_idx+max_break_gap+1, asof+1)，与 bars[:i+1] 完全一致
    for j in range(break_idx, min(break_idx + max_break_gap + 1, asof + 1)):
        if is_limit_up(Ctx._f(bars[j], "close"), Ctx._f(bars[j - 1], "close"), bt, market):
            break
        break_days += 1
    if break_days == 0:
        return None
    break_bars = bars[break_idx:break_idx + break_days]
    first_break = break_bars[0]
    break_low = min(Ctx._f(b, "low") for b in break_bars)
    break_vol_avg = sum(Ctx._f(b, "volume") for b in break_bars) / len(break_bars)
    break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0.0
    first_break_chg = (Ctx._f(first_break, "close") / limit_close - 1) * 100 if limit_close > 0 else 0.0
    first_break_gap = (Ctx._f(first_break, "open") / limit_close - 1) * 100 if limit_close > 0 else 0.0
    break_drawdown = (break_low / limit_close - 1) * 100 if limit_close > 0 else 0.0
    confirm_bar = break_bars[-1]
    confirm_prev = break_bars[-2] if len(break_bars) >= 2 else limit_bar
    c_pc = Ctx._f(confirm_prev, "close")
    confirm_chg = (Ctx._f(confirm_bar, "close") / c_pc - 1) * 100 if c_pc > 0 else 0.0
    confirm_gap = (Ctx._f(confirm_bar, "open") / c_pc - 1) * 100 if c_pc > 0 else 0.0
    pre20_gain = None
    if streak_start >= 20:
        _ref = Ctx._f(bars[streak_start - 20], "close")
        if _ref > 0:
            pre20_gain = (limit_close / _ref - 1) * 100
    ma_bull = _bk_ma_bull_at(bars, break_idx + break_days - 1)
    return {
        "streak_len": streak_len, "streak_start_idx": streak_start, "streak_end_idx": streak_end,
        "break_idx": break_idx, "break_days": break_days,
        "limit_open": limit_open, "limit_close": limit_close, "limit_vol": limit_vol,
        "break_low": break_low, "break_vol_r": break_vol_r,
        "first_break_chg": first_break_chg, "first_break_gap": first_break_gap,
        "break_drawdown": break_drawdown,
        "confirm_chg": confirm_chg, "confirm_gap": confirm_gap,
        "pre20_gain": pre20_gain, "ma_bull": ma_bull,
        "break_date": bars[break_idx]["time"],
        "streak_start_date": bars[streak_start]["time"],
        "streak_end_date": bars[streak_end]["time"],
    }


def _bk_compute(ctx: Ctx):
    """决策日 i 的断板期候选结构（镜像 break_buy.scan_signals 的 lu_idx 搜索 + 对齐）。

    候选唯一：streak_end = i 之前**最后一个涨停日**（若距 i 超过 max_break_gap 则断板期过长
    → 无候选）；streak_start = 该连板首板（须 is_first：其前 10 日内无涨停）；断板期
    = [streak_end+1, i]，长度须 ≤ max_break_gap 且恰好终止于 i。只读 ≤ i 的 bar（as-of 安全）。
    """
    bars = ctx.bars
    i = ctx.i
    n = ctx.n
    bt = ctx.board_type
    mk = ctx.market
    p = ctx.params
    if i < 2 or i >= n:
        return None
    min_streak = int(p.get("min_streak", 2))
    max_break_gap = int(p.get("max_break_gap", 5))
    # 确认日必为非涨停日（断板期最后一天）
    if is_limit_up(Ctx._f(bars[i], "close"), Ctx._f(bars[i - 1], "close"), bt, mk):
        return None
    # i 之前最后一个涨停日（= streak_end）；断板期 ≤ max_break_gap，故仅需回看该窗口
    streak_end = -1
    j = i - 1
    steps = 0
    while j >= 1 and steps <= max_break_gap:
        if is_limit_up(Ctx._f(bars[j], "close"), Ctx._f(bars[j - 1], "close"), bt, mk):
            streak_end = j
            break
        j -= 1
        steps += 1
    if streak_end < 0:
        return None
    break_days = i - streak_end
    if break_days < 1 or break_days > max_break_gap:
        return None
    # 连板首板（向前回看连续涨停）：仅当前一根 bar 也是涨停时才纳入（与参考版正向延伸对称）
    streak_start = streak_end
    while streak_start - 2 >= 0 and is_limit_up(
            Ctx._f(bars[streak_start - 1], "close"), Ctx._f(bars[streak_start - 2], "close"), bt, mk):
        streak_start -= 1
    # is_first：首板前 10 日内不得有涨停（镜像 break_buy.scan_signals）
    for k in range(1, min(11, streak_start + 1)):
        idx = streak_start - k
        if idx - 1 >= 0 and is_limit_up(Ctx._f(bars[idx], "close"),
                                       Ctx._f(bars[idx - 1], "close"), bt, mk):
            return None
    return _bk_raw(bars, bt, streak_start, streak_end, min_streak, max_break_gap, i, mk)


def bk_struct(ctx: Ctx):
    """决策日 i 的断板期结构（每 Ctx 记忆化：同一天的多个门共享一次计算）。None=非确认日。"""
    cache = ctx.__dict__.setdefault("_bk_cache", {})
    if "s" not in cache:
        cache["s"] = _bk_compute(ctx)
    return cache["s"]


# 非候选日的缺省特征（使各判定门自然失败；候选门 g_candidate 才是真正的闸）
_BK_MISS = {
    "streak_len": 0.0, "break_days": 0.0, "break_vol_r": 0.0,
    "first_break_chg": -1e18, "first_break_gap": -1e18, "break_drawdown": -1e18,
    "confirm_chg": -1e18, "confirm_gap": -1e18, "pre20_gain": -1e18,
    "ma_bull": 0.0, "limit_open": 0.0, "break_low": 0.0, "is_candidate": 0,
}


def bk_feat(ctx: Ctx, name: str):
    """断板期结构特征（供门表表达式引用）。非候选日 → 返回使各门自然失败的缺省值。

    ma_bull 编码: True→1 / None(数据不足)→-1 / False→0 —— 参考版"仅 False 拦截"由门
    表达式 `not ma_bull_filter or bk_feat('ma_bull') != 0` 表达 (None 亦放行)。
    """
    s = bk_struct(ctx)
    if name == "is_candidate":
        return 1 if s is not None else 0
    if s is None:
        return _BK_MISS.get(name, 0.0)
    if name == "pre20_gain":
        v = s["pre20_gain"]
        return v if v is not None else -1e18
    if name == "ma_bull":
        v = s["ma_bull"]
        return 1 if v is True else (-1 if v is None else 0)
    return s.get(name, _BK_MISS.get(name, 0.0))


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


def turnover_sig(ctx: Ctx) -> float:
    """确认日换手率%(= D0成交量/流通股本*100; 镜像 break_buy 的 turnover_sig 口径)。

    流通股本缺失 (circ<=0) → 返回极大值 = 该门 fail-open 放行（参考版 circ<=0 时跳过该门）。
    """
    circ = float((ctx.stock_info or {}).get("circ_shares") or 0)
    if circ <= 0:
        return 1e18
    return Ctx._f(ctx.bars[ctx.i], "volume") / circ * 100


def break_features(ctx: Ctx, stock_info=None) -> dict:
    """break 信号展示字段（逐字镜像 break_buy._signal_to_legacy_dict + scan_signals 的 extra）。

    门表用 bk_feat 判资格；本函数额外给出**信号展示字段**（连板/断板期/换手率），
    保证与 python 参考版 trades 逐字一致。逻辑单点维护于此。
    """
    s = bk_struct(ctx)
    if s is None:
        return {}
    i = ctx.i
    si = stock_info or {}
    circ = float(si.get("circ_shares") or 0)
    total = float(si.get("total_shares") or 0)
    se = s["streak_end_idx"]
    vol_se = Ctx._f(ctx.bars[se], "volume")
    vol_i = Ctx._f(ctx.bars[i], "volume")
    return {
        "streak_len": s["streak_len"],
        "streak_start": s["streak_start_date"],
        "streak_end": s["streak_end_date"],
        "break_date": s["break_date"],
        "break_days": s["break_days"],
        "break_chg": round(s["first_break_chg"], 2),
        "break_gap": round(s["first_break_gap"], 2),
        "break_vol_r": round(s["break_vol_r"], 2),
        "confirm_chg": round(s["confirm_chg"], 2),
        "confirm_gap": round(s["confirm_gap"], 2),
        "pre20_gain": round(s["pre20_gain"], 2) if s["pre20_gain"] is not None else None,
        "ma_bull": s["ma_bull"],
        "turnover_anchor": round(vol_se / circ * 100, 2) if circ > 0 else None,
        "turnover_sig": round(vol_i / circ * 100, 2) if circ > 0 else None,
        "turnover_anchor_total": round(vol_se / total * 100, 2) if total > 0 else None,
        "turnover_sig_total": round(vol_i / total * 100, 2) if total > 0 else None,
    }


# ================================================================
# g56 五重共振专属函数（镜像 strategies/g56.py，供门表转写使用）
# ----------------------------------------------------------------
# 判定 = G1 池成员(全 D-1 判定) + rhist_chg 门槛 + 横截面 regime 门。
#   - 特征 f=_g1_arrays(bars) 由编排层 O(n) 一次算好 → ctx.ext['g56_feats']
#     （镜像参考版 backtest_stock 的"预计算一次"修复，避免逐日重算 O(n^2)）。
#   - 横截面池由 ctx.ext['g56_pool'] 注入（引擎 reuse g56._ensure_pool_daily，按
#     pool_target 缓存；池统计是市场级数据通道，非策略规则）。
#   本模块把 _g1_mask 逐条拆成**独立可解释的门函数**；**NaN 作为"缺值/暖机"哨兵**，
#   使数值门自然失败（与 _g1_mask 的 np.isfinite 过滤 + 暖机 m[:68]=False 同效）。
# as-of：f 只由 bars[0..] 因果递推（EMA/rolling 皆因果），读取只取 index=i；池按 date_k 查表。
# ================================================================

_G56_KEYS = ("rma", "rma_chg", "atr", "rhist_chg", "dif0", "pctb", "rsi")


def g56_feat(ctx: Ctx, name: str) -> float:
    """g56 G1 特征取值（决策日 i）。缺值/暖机 → nan（数值门自然失败）。"""
    f = ctx.ext.get("g56_feats")
    if f is None:
        raise RuntimeError("g56 特征未注入 ctx.ext['g56_feats']（编排层缺失）")
    arr = f.get(name)
    if arr is None:
        raise KeyError(f"未知 g56 特征: {name}")
    j = ctx.i
    if j < 0 or j >= len(arr):
        return float("nan")
    return float(arr[j])


def g56_finite(ctx: Ctx) -> int:
    """G1 判定要求的"全部特征有限"（镜像 _g1_mask 的 np.isfinite 循环）。"""
    import math
    f = ctx.ext.get("g56_feats") or {}
    j = ctx.i
    for k in _G56_KEYS:
        arr = f.get(k)
        if arr is None or j < 0 or j >= len(arr) or not math.isfinite(float(arr[j])):
            return 0
    return 1


def g56_warmup(ctx: Ctx) -> int:
    """暖机下限：镜像 _g1_mask 的 m[:68]=False → 决策日索引 i 必须 >= 68。"""
    return 1 if ctx.i >= 68 else 0


def g56_pool_stat(ctx: Ctx, name: str) -> float:
    """横截面 regime 池统计（决策日 date_k 的板块池值）—— 门用（缺失 → nan → 门失败）。

    镜像参考版 `st = pool.get(board,{}).get(date_k); if st is None: 拦截`。
    """
    pool = ctx.ext.get("g56_pool") or {}
    st = (pool.get(ctx.board_type) or {}).get(str(ctx.bars[ctx.i]["time"])[:10])
    if st is None:
        return float("nan")
    v = st.get(name)
    return float("nan") if v is None else float(v)


def g56_pool_field(ctx: Ctx, name: str):
    """横截面池统计原值 —— 信号展示字段用（缺失/None → None，与参考版 trades 同语义）。"""
    pool = ctx.ext.get("g56_pool") or {}
    st = (pool.get(ctx.board_type) or {}).get(str(ctx.bars[ctx.i]["time"])[:10])
    if st is None:
        return None
    v = st.get(name)
    return None if v is None else float(v)


def board_is_main(ctx: Ctx) -> int:
    """是否主板（镜像参考版 regime 门的 board == 'main' 分支）。"""
    return 1 if ctx.board_type == "main" else 0


def board_is_gem(ctx: Ctx) -> int:
    """是否 20cm（创业板/科创板）。"""
    return 1 if ctx.board_type != "main" else 0


# ================================================================
# knife_catch 反向接刀专属函数（镜像 strategies/knife_catch.py）
# ----------------------------------------------------------------
# "14:56 尾盘买入 → D1 开盘卖" 的盘中策略。判定基于触发槽位快照 (ctx.latest) +
# 快照序列 (ctx.series) + 日线上下文 (ctx.bars, as-of 昨日)。
# 本模块把 scan_signals 的中间量收编为**每 Ctx 记忆化**的取值函数 (一次计算多门共享)；
# 缺值/无效 → NaN 哨兵 (数值门自然失败，与参考版早返回同效)。_tail_ret/_vw_frac/
# _daily_feats 逐字镜像（纯数据派生，as-of 安全：只读 ≤ 槽位/昨日的序列）。
# ================================================================

def _kc_hhmm(s) -> str:
    """'YYYY-MM-DD HH:MM:SS' → 'HH:MM'（镜像 knife_catch._hhmm）。"""
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _kc_tail_ret(series_rows, last_px, last_time, minutes=20):
    """尾盘 20 分钟回升%（逐字镜像 knife_catch._tail_ret）。无法计算 → None。"""
    if not series_rows or last_px <= 0:
        return None
    from datetime import datetime, timedelta
    try:
        t_cut = (datetime.strptime(str(last_time)[:19], "%Y-%m-%d %H:%M:%S")
                 - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    ref = None
    for r in series_rows:
        ts = str(r["time"])[:19]
        if ts <= t_cut:
            ref = r
        else:
            break
    if ref is None:
        return None
    ref_px = float(ref.get("last") or 0)
    if ref_px <= 0:
        return None
    return (last_px / ref_px - 1) * 100


def _kc_vw_frac(series_rows):
    """全天 VWAP 上方占比（逐字镜像 knife_catch._vw_frac；样本<30 → None）。"""
    if not series_rows:
        return None
    cum_pv = cum_v = 0.0
    above = total = 0
    prev_v = 0.0
    for r in series_rows:
        px = float(r.get("last") or 0)
        v = float(r.get("volume") or 0)
        if px <= 0:
            continue
        dv = max(0.0, v - prev_v)
        prev_v = v
        if cum_v > 0:
            total += 1
            if px > cum_pv / cum_v:
                above += 1
        cum_pv += px * dv
        cum_v += dv
    return above / total if total >= 30 else None


def _kc_daily_feats(bars, code, market=None):
    """日线特征（逐字镜像 knife_catch._daily_feats；bars[-1]=昨日；len<8 → None）。"""
    if len(bars) < 8:
        return None
    closes = [float(b["close"]) for b in bars]
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            streak += 1
        else:
            break
    pre5 = (closes[-1] / closes[-5] - 1) * 100 if closes[-5] > 0 else 0
    vol5 = sum(float(b["volume"]) for b in bars[-5:]) / 5
    lu_recent = 0
    bt = get_board_type(code, market)
    for d in range(len(bars) - 1, max(len(bars) - 6, 0), -1):
        cl, pc = closes[d], closes[d - 1]
        if pc > 0 and is_limit_up(cl, pc, bt, market):
            lu_recent += 1
    return {"down_streak": streak, "pre5": pre5, "vol5": vol5, "lu_recent": lu_recent}


_KC_NAN_KEYS = ("gain", "amp", "pos", "tail", "vw", "vol_ratio", "streak",
                "pre5", "lu_recent")


def _kc_cache(ctx: Ctx) -> dict:
    """knife 盘中判定中间量（每 Ctx 记忆化）。覆盖 scan_signals + 3 个数据助手。"""
    cache = ctx.__dict__.get("_kc_cache")
    if cache is not None:
        return cache
    cache = {k: float("nan") for k in _KC_NAN_KEYS}
    snap = ctx.latest or {}
    last = Ctx._f(snap, "last")
    high = Ctx._f(snap, "high")
    low = Ctx._f(snap, "low")
    pc = Ctx._f(snap, "previousClose")
    last_time = str(snap.get("time") or "")
    cache["hhmm"] = _kc_hhmm(last_time)
    if last > 0 and pc > 0 and high > low:
        cache["gain"] = (last / pc - 1) * 100
        cache["amp"] = (high - low) / pc * 100
        cache["pos"] = (last - low) / (high - low)
        tail = _kc_tail_ret(ctx.series or [], last, last_time, 20)
        vw = _kc_vw_frac(ctx.series or [])
        cache["tail"] = float("nan") if tail is None else tail
        cache["vw"] = float("nan") if vw is None else vw
        df = _kc_daily_feats(ctx.bars or [], ctx.code, ctx.market)
        if df is not None:
            cache["vol_ratio"] = (Ctx._f(snap, "volume") / df["vol5"]) if df["vol5"] > 0 else 99.0
            cache["streak"] = 1 + df["down_streak"]
            cache["pre5"] = df["pre5"]
            cache["lu_recent"] = df["lu_recent"]
    ctx.__dict__["_kc_cache"] = cache
    return cache


def kc_metric(ctx: Ctx, name: str) -> float:
    """knife 特征取值（缺少/无效 → nan，数值门自然失败）。"""
    return float(_kc_cache(ctx).get(name, float("nan")))


def kc_hhmm(ctx: Ctx) -> str:
    """触发槽位的 HH:MM（'' = 时间串非法）。"""
    return _kc_cache(ctx).get("hhmm", "")


def kc_mkt_gain(ctx: Ctx) -> float:
    """全市场均涨幅%（ctx.mkt_gain；None → nan → 市场门失败，镜像参考版 mkt_gain is None）。"""
    return float("nan") if ctx.mkt_gain is None else float(ctx.mkt_gain)


# ================================================================
# tail_oversold 尾盘超卖超短专属函数（镜像 strategies/tail_oversold.py）
# ----------------------------------------------------------------
# 与 knife 同生命周期（14:56 买 → D1 开盘卖），但归一化口径不同：
# nf = 0.5(创/科板) / 1.0(主板)；tail_ret = 14:56 现价 vs 14:20~14:40 分钟均价。
# ================================================================

def _to_is_gem_star(code, market=None) -> bool:
    """是否高波动板（创业板/科创板）。

    口径 = MarketSpec 的分板规则（**不是**代码前缀字面量）—— 换市场时自动跟随。
    与旧 `str(code)[:3].startswith(("30","68"))` 对 A 股逐位等价（见 a.yaml board_rules）。
    """
    return get_board_type(code, market) == "gem_star"


def to_nf(ctx: Ctx) -> float:
    """归一化系数：高波动板 0.5，其余 1.0（镜像 tail_oversold._norm_factor）。

    0.5/1.0 是**策略自己的归一化惯例**（高波动板幅度翻倍故减半），不是市场规则常量，
    故留在策略函数里；判定用的"是否高波动板"则来自 MarketSpec。
    """
    return 0.5 if _to_is_gem_star(ctx.code, ctx.market) else 1.0


def _to_limit_pct(code, market=None) -> float:
    """该股的市场名义涨停幅度（供 tail 的涨停触板判定）—— 取自 MarketSpec。"""
    m = market if market is not None else default_market()
    return m.nominal_up_pct(get_board_type(code, m))


def _to_calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf) -> float:
    """V2 评分（逐字镜像 tail_oversold._calc_score；nan 输入 → 该分支不计分）。"""
    score = 0.0
    dg = day_gain * nf
    if dg <= -8:
        score += 4.0
    elif dg <= -5:
        score += 3.0
    elif dg <= -2:
        score += 1.2
    elif dg <= 0:
        score += 0.5
    tr = tail_ret * nf
    if tr <= -2:
        score += 3.0
    elif tr <= -1:
        score += 2.5
    elif tr <= -0.3:
        score += 1.5
    if pos_range <= 0.2:
        score += 2.0
    elif pos_range <= 0.4:
        score += 1.0
    if amplitude * nf >= 5 and tr <= -0.3:
        score += 1.0
    p5 = pre5_gain * nf
    if p5 <= -10:
        score += 0.3
    elif p5 <= -5:
        score += 0.1
    return round(score, 2)


def _to_tail_ret_v2(series_rows):
    """V2 尾盘回落%（逐字镜像 tail_oversold._tail_ret_v2；槽位<15 → None）。

    prep_minutes 是 data/hub 的分钟标准化通道（数据层，非策略规则）。
    """
    if not series_rows:
        return None
    from app.market_cn.auto.core.data.hub import prep_minutes
    mins = prep_minutes(
        [{"time": str(r.get("time") or ""), "open": r.get("open") or 0,
          "high": r.get("high") or 0, "low": r.get("low") or 0,
          "close": r.get("last") or 0, "volume": r.get("volume") or 0}
         for r in series_rows], volume_cumulative=True)
    by_mi = {b["mi"]: float(b["c"]) for b in mins if float(b["c"]) > 0}
    tail = [by_mi[mi] for mi in range(199, 220) if mi in by_mi]
    tail_avg = sum(tail) / len(tail) if len(tail) >= 15 else 0
    last_px = by_mi.get(max(by_mi)) if by_mi else 0
    if not last_px or last_px <= 0 or tail_avg <= 0:
        return None
    return (last_px / tail_avg - 1) * 100


def _to_cache(ctx: Ctx) -> dict:
    """tail 盘中判定中间量（每 Ctx 记忆化）。覆盖 scan_signals + 数据助手。"""
    cache = ctx.__dict__.get("_to_cache")
    if cache is not None:
        return cache
    snap = ctx.latest or {}
    last = Ctx._f(snap, "last")
    high = Ctx._f(snap, "high")
    low = Ctx._f(snap, "low")
    pc = Ctx._f(snap, "previousClose")
    cache = {"hhmm": _kc_hhmm(str(snap.get("time") or "")), "nf": to_nf(ctx),
             "day_gain": float("nan"), "amplitude": float("nan"),
             "pos_range": float("nan"), "tail_ret": float("nan"),
             "pre5_gain": float("nan"), "score": float("nan"), "limit_hit": 0}
    if last > 0 and pc > 0 and high > 0 and low > 0 and high > low:
        cache["limit_hit"] = 1 if last >= round(
            pc * (1 + _to_limit_pct(ctx.code, ctx.market)), 2) * 0.998 else 0
        cache["day_gain"] = (last / pc - 1) * 100
        cache["amplitude"] = (high - low) / pc * 100
        cache["pos_range"] = (last - low) / (high - low)
        tr = _to_tail_ret_v2(ctx.series or [])
        cache["tail_ret"] = float("nan") if tr is None else tr
        closes = [float(b["close"]) for b in (ctx.bars or [])]
        if len(closes) >= 6 and closes[-5] > 0:
            cache["pre5_gain"] = (last / closes[-5] - 1) * 100
            cache["score"] = _to_calc_score(cache["day_gain"], cache["tail_ret"],
                                            cache["pos_range"], cache["amplitude"],
                                            cache["pre5_gain"], cache["nf"])
    ctx.__dict__["_to_cache"] = cache
    return cache


def to_metric(ctx: Ctx, name: str) -> float:
    """tail 特征取值（缺少/无效 → nan，数值门自然失败）。"""
    return float(_to_cache(ctx).get(name, float("nan")))


def to_hhmm(ctx: Ctx) -> str:
    """触发时刻 HH:MM（'' = 时间串非法）。"""
    return _to_cache(ctx).get("hhmm", "")


def to_limit_hit(ctx: Ctx) -> int:
    """是否封板买不进（1=封板，镜像参考版 last >= 涨停价*0.998）。"""
    return int(_to_cache(ctx).get("limit_hit", 0))


def to_ok(ctx: Ctx, name: str) -> int:
    """该特征是否可用（1=非 nan）—— 镜像参考版 "tail_ret is None / bars 不足 → 早返回"。"""
    v = float(_to_cache(ctx).get(name, float("nan")))
    return 0 if v != v else 1        # nan != nan → 不可用


# ---- 注册（引擎注入 ctx，门表按名直接调用）----
# needs_d0: 该函数是否读「决策日 i」的数据 (供展示端 T-1 夜预计算静态推导, 见
# ide/functions.D0_DEPS 与 ide/present.reads_decision_bar)。只声明可证明为 0 的;
# 不声明 = 保守为 1 (会被划到盘中求值, 只影响速度不影响正确性)。
register_function("obv_rising", obv_rising)          # 非偏移
register_function("no_lu_last", no_lu_last, needs_d0=0)          # 非偏移 (区间 (i-days, i), 不含决策日)
register_function("macd_hist_lt", macd_hist_lt)      # 非偏移
register_function("boll_bw_ok", boll_bw_ok)          # 非偏移
register_function("board_height", board_height)      # 非偏移
register_function("ma_bull", ma_bull)                # 非偏移
register_function("is_bse", is_bse, needs_d0=0)                  # 非偏移 (只看代码交易所)
register_function("ret_n", ret_n)                    # 非偏移
register_function("d1_vol_ratio", d1_vol_ratio, needs_d0=0)  # 非偏移 (D-1 量/前5日均量, 不含决策日)
register_function("is_approx_limit_up", is_approx_limit_up)  # 非偏移
register_function("bk_feat", bk_feat)                # 非偏移 (断板期结构特征)
register_function("pk", pk, needs_d0=0)                          # 非偏移 (只看 params/board_type)
register_function("turnover_sig", turnover_sig)      # 非偏移 (确认日换手率)
register_function("g56_feat", g56_feat)              # 非偏移 (g56 G1 特征)
register_function("g56_finite", g56_finite)          # 非偏移
register_function("g56_warmup", g56_warmup)          # 非偏移
register_function("g56_pool_stat", g56_pool_stat)    # 非偏移 (横截面池统计, 门用)
register_function("g56_pool_field", g56_pool_field)  # 非偏移 (横截面池统计, 字段用)
register_function("board_is_main", board_is_main, needs_d0=0)    # 非偏移 (只看 board_type)
register_function("board_is_gem", board_is_gem, needs_d0=0)      # 非偏移 (只看 board_type)
register_function("kc_metric", kc_metric)            # 非偏移 (knife 特征)
register_function("kc_hhmm", kc_hhmm)                # 非偏移 (触发时刻)
register_function("kc_mkt_gain", kc_mkt_gain)        # 非偏移 (全市场均涨幅)
register_function("to_metric", to_metric)            # 非偏移 (tail 特征)
register_function("to_hhmm", to_hhmm)                # 非偏移 (触发时刻)
register_function("to_limit_hit", to_limit_hit)      # 非偏移 (封板判定)
register_function("to_ok", to_ok)                    # 非偏移 (特征可用性)
register_function("to_nf", to_nf, needs_d0=0)                    # 非偏移 (只看代码 → 归一化系数)
