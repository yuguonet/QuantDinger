#!/usr/bin/env python3
"""技术指标库 (基座共享叶子层) —— 2026-09-23 自 app/market_cn/auto/core/indicators.py 上移

上移原因: 该库原位于 auto/ 内部, 且全仓 auto 之外零引用; 而展示层 (app/watchlist) 需要
与策略判定同一份指标口径 ⇒ 上移为共享叶子层 (与 db_market / trading_calendar / basicinfo_db 同层)。
2026-10-07: 原路径的 re-export shim (app/market_cn/auto/core/indicators.py) **已删除** ——
auto 内 10 处引用已统一改指本模块, 指标口径只有一处定义 (无第二跳)。
本文件内容与原文件逐字一致 (含全部数值口径), 上移不改变任何计算结果。

用途: 策略共用的纯指标计算, 与 test_dragon.py 同名函数逐字一致 (对数基准)。
关键设计点:
  - ema/rsi 返回单值或 None (长度不足); calc_macd 返回 (dif, dea, hist) 三序列或 (None,None,None);
  - MACD柱 = 2*(DIF-DEA) (国内行情软件口径, 与普通教科书的 DIF-DEA 不同, 勿"修正");
  - 形态族 (golden_cross/turning_positive/shrinking_negative) 只做布尔判定。
  - ma/kdj (2026-09-09 D2 新增, 非 test_dragon 对数成员): ma 与 relay3._ma 逐字等价;
    kdj 采用国内行情软件口径 (RSV 9日, K/D 三分之一平滑, 首个有效值前 K=D=50, J=3K-2D)。
易错点: 全部函数只读入参序列尾部, 无未来函数问题; 但回测切片语义由调用方 (as_of) 保证。
"""
from __future__ import annotations


def ma(closes, n):
    """简单均线 SMA 末值。与 strategies/relay3._ma 逐字等价 (D2 收编自该处)。"""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def kdj(highs, lows, closes, period=9):
    """KDJ 随机指标, 国内行情软件口径, 返回 (k, d, j) 三序列或 (None, None, None)。

    口径: RSV[i]=(C-Ln)/(Hn-Ln)*100 (n=period 窗口含当日);
          K=(2*K'+RSV)/3, D=(2*D'+K)/3, 首个有效 bar 前初始 K=D=50;
          J=3K-2D。前 period-1 根输出 50/50/50 占位 (与国内软件"未走满不画线"不同,
          调用方若只要末值请取 [-1]; 形态判定请自行跳过占位段)。
    易错点: Hn==Ln (连续一字板) 时 RSV 取 50 (中性), 避免除零。
    """
    n = len(closes)
    if n < period or len(highs) != n or len(lows) != n:
        return None, None, None
    k_s, d_s, j_s = [], [], []
    k_prev = d_prev = 50.0
    for i in range(n):
        if i < period - 1:
            k_s.append(50.0); d_s.append(50.0); j_s.append(50.0)
            continue
        hn = max(highs[i - period + 1:i + 1])
        ln = min(lows[i - period + 1:i + 1])
        rsv = 50.0 if hn <= ln else (closes[i] - ln) / (hn - ln) * 100
        k_prev = (2 * k_prev + rsv) / 3
        d_prev = (2 * d_prev + k_prev) / 3
        k_s.append(k_prev); d_s.append(d_prev); j_s.append(3 * k_prev - 2 * d_prev)
    return k_s, d_s, j_s


def ema(values, period):
    """计算EMA (指数移动平均)"""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # 初始值用SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    """计算RSI (相对强弱指数)"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 初始SMA
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # EMA平滑
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


# ================================================================
# MACD 单一内核 (B6, 2026-10-04)
# ================================================================
# 背景: 展示层此前有**两套** MACD 实现 ——
#   ① 本文件的 calc_macd  (cross_section._g1_arrays → g56 特征)
#   ② core/runtime/functions.py 的 Ctx._macd (门表 DSL macd_dif/dea/hist)
# 靠注释约定"逐值一致"维持, 正是 core/market.py:213 自己点名的
# 「内联出第二份 = 漂移温床」。现统一到 `macd_core` 一处, ①② 都委托它。
#
# ★ 锚点 (anchor) —— 精确截窗, 不是估计 (2026-10-04 实测, 见 tmp/macd_state_anchor_probe.py):
#   EMA 是 Markov 递推 e[i] = α·c[i] + (1-α)·e[i-1], **整个历史只通过 e[i-1] 一个数传递**。
#   只要把锚点状态 (ema_fast[a], ema_slow[a], dea[a]) 传进来, 短窗口往前递推得到的序列
#   与"从无穷远历史一路算下来"**逐位相同**。实测 20 根窗口即 100% 精确 (误差 0)。
#   用途: 展示层窗口可从 300 根压到 40 根, 而 MACD 值不变 ⇒ g56 阈值零影响。
#
# ⚠️ 两个实现坑 (本轮都踩过, 改这里必看):
#   ① 系数: signal=9 → k = 2/(signal+1) = 2/10。写成 2/11 会得到"锚定比朴素更差"的假象。
#   ② **不能先跑完递推再覆写 e[0]**: 后续元素已在覆写前用错的 e[0] 算过,
#      残余误差按 (1-α)^i 衰减, 表现成"窗口越长误差越小"的假象, 极具欺骗性。
#      必须用 `ema_fwd` 从递推起点就接 e_prev。
# ================================================================


def _ema_naive_series(values, period):
    """朴素播种的 EMA 序列: out[0] = values[0], 之后递推。

    ⚠️ **必须逐字保持这个形态**, 不要改成 `ema_fwd(values, period, values[0])`:
       后者首元素是 `v*a + v*(1-a)`, 浮点上**不严格等于** `v` (差 1 ULP)。
       g56 的阈值是按本口径样本内拟合的, 改了会在边界上门判定漂移。
       (2026-10-04 实测: 300 组随机序列里出现 MISMATCH, 即由此来)

    ★ 2026-10-05: 本函数是 **EMA 的全局单一实现** (基座叶子层)。
      `auto/core/runtime/resume.py` 的 `ema_naive` 已改为委托本函数 ——
      依赖方向必须是 auto → utils, 反过来会让基座依赖业务层。
    """
    k = 2.0 / (period + 1)
    out = [0.0] * len(values)
    if not values:
        return out
    out[0] = values[0]
    for j in range(1, len(values)):
        out[j] = values[j] * k + out[j - 1] * (1 - k)
    return out


# 公开别名: 供 auto/core/runtime/resume.py 委托 (原名保留以兼容既有调用方)
ema_naive = _ema_naive_series


def ema_fwd(values, period, e_prev):
    """从**前一根的状态** e_prev 往后递推 EMA。

    e_prev = e[a], values[0] = c[a+1]; 返回的 out[i] = e[a+1+i]。
    与 `ema(values, period)` 的区别: 后者用 values[0] 作初值 (朴素播种, 有暖机误差);
    本函数接收精确初值, 无瞬态。
    """
    a = 2.0 / (period + 1)
    out = [0.0] * len(values)
    prev = float(e_prev)
    for j, v in enumerate(values):
        prev = v * a + prev * (1.0 - a)
        out[j] = prev
    return out


def macd_state(closes, fast=12, slow=26, signal=9, upto=None):
    """长序列前缀 → 锚点状态 (ema_fast[a], ema_slow[a], dea[a])。

    upto: 取哪一根的状态 (默认末根)。a = 短窗口首根的**前一根** ⇒ 调用方传
    `upto = len(长窗) - len(短窗) - 1`。
    朴素播种 (closes[0]) 起算 —— 前提是长窗已足够收敛 (>=150 根实测即 0 误差)。
    """
    n = len(closes)
    if n == 0:
        return (0.0, 0.0, 0.0)
    a = n - 1 if upto is None else max(0, min(int(upto), n - 1))
    kf, ks, ks9 = 2.0 / (fast + 1), 2.0 / (slow + 1), 2.0 / (signal + 1)
    ef = _ema_naive_series(closes[: a + 1], fast)
    es = _ema_naive_series(closes[: a + 1], slow)
    dif = [ef[j] - es[j] for j in range(a + 1)]
    dea = _ema_naive_series(dif, signal)
    return (ef[a], es[a], dea[a])


def macd_core(closes, fast=12, slow=26, signal=9, anchor=None):
    """MACD 单一内核, 永远返回三序列 (不设长度门槛)。

    anchor=None → 朴素播种 (closes[0]), 与历史口径逐位一致;
    anchor=(ef_a, es_a, dea_a) → 锚定接力, 与"长序列算下来"逐位相同。

    ⚠️ 调用方不得混用两种口径比较数值 —— 同一段数据两种锚点会得到不同结果是**预期**,
    不是 bug; 同一锚点下必须逐位稳定。
    """
    n = len(closes)
    if n == 0:
        return [], [], []
    kf, ks, ks9 = 2.0 / (fast + 1), 2.0 / (slow + 1), 2.0 / (signal + 1)
    if anchor is None:
        # 朴素播种: 必须走 _ema_naive_series, 与历史 calc_macd **逐位**一致 (见其注释)
        ef = _ema_naive_series(closes, fast)
        es = _ema_naive_series(closes, slow)
        dif = [ef[j] - es[j] for j in range(n)]
        dea = _ema_naive_series(dif, signal)
    else:
        ef_a, es_a, dea_a = (float(x) for x in anchor)
        ef = ema_fwd(closes, fast, ef_a)     # 从"前一根状态"接力, 无瞬态
        es = ema_fwd(closes, slow, es_a)
        dif = [ef[j] - es[j] for j in range(n)]
        dea = ema_fwd(dif, signal, dea_a)
    hist = [2.0 * (dif[j] - dea[j]) for j in range(n)]
    return dif, dea, hist


def macd_anchor_step(anchor, closes, fast=12, slow=26, signal=9):
    """把锚点 `(ef, es, dea)` 沿 `closes` 往后推进, 返回推进后的末根状态。

    与 `macd_core(anchor=...)` 的 anchor 分支**同一递推** (ef/es → dif → dea),
    只是这里只要末根 (供状态机逐格推进, O(1) 摊销)。

    ★ 2026-10-07: 递推本体收敛到此处 —— 此前 `core/features/cross_section._anchor_step`
      另写了一份 ef/es/dif/dea, 是"第二份 MACD"。两侧算法一度相同但无人保证继续相同:
      改一处忘另一处 ⇒ 特征层 MACD 与指标层 MACD 静默分叉, 且**不报错**。
      现在特征层委托本函数, 口径只有一份。
    """
    n = len(closes)
    if n == 0:
        return tuple(float(x) for x in anchor)
    ef_a, es_a, dea_a = (float(x) for x in anchor)
    ef = ema_fwd(closes, fast, ef_a)
    es = ema_fwd(closes, slow, es_a)
    dif = [ef[j] - es[j] for j in range(n)]
    dea = ema_fwd(dif, signal, dea_a)
    return (ef[-1], es[-1], dea[-1])


def calc_macd(closes, fast=12, slow=26, signal=9, anchor=None):
    """计算MACD, 返回 (dif, dea, macd_hist) 三个序列

    MACD柱 = 2*(DIF-DEA), DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal)

    anchor: 可选锚点 (ema_fast, ema_slow, dea) —— 用于短窗口精确接力长窗口,
    见 `macd_state` / `macd_core`。缺省 None = 朴素播种 (与历史口径逐位一致)。
    长度门槛 (n < slow+signal → 全 None) 是本公开口径的既有契约, 保持不变。
    """
    n = len(closes)
    if n < slow + signal:
        return None, None, None
    return macd_core(closes, fast, slow, signal, anchor=anchor)


def calc_bollinger_bw(closes, period=20, num_std=2):
    """计算布林带宽百分比 = (upper-lower)/middle*100, 仅返回带宽值"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    if mid <= 0:
        return None
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (upper - lower) / mid * 100


def calc_roc(closes, period=10):
    """计算变动率 ROC = (close[i]-close[i-period])/close[i-period]*100"""
    if len(closes) < period + 1:
        return None
    ref = closes[-1 - period]
    if ref <= 0:
        return None
    return (closes[-1] - ref) / ref * 100


def calc_psy(closes, period=12):
    """计算心理线 PSY = 过去period天中上涨天数/period*100"""
    if len(closes) < period + 1:
        return None
    up_days = 0
    for i in range(-period, 0):
        if closes[i] > closes[i-1]:
            up_days += 1
    return up_days / period * 100


def is_macd_golden_cross(dif, dea, lookback=3):
    """判断MACD是否在最近lookback根K线内发生金叉 (DIF上穿DEA)"""
    if dif is None or dea is None or len(dif) < lookback + 1:
        return False
    n = len(dif)
    if dif[n-1] < dea[n-1]:
        return False  # 当前DIF在DEA下方
    for i in range(max(0, n - lookback - 1), n - 1):
        if dif[i] < dea[i]:
            return True
    return False


def is_macd_hist_turning_positive(hist, lookback=3):
    """判断MACD柱是否在最近lookback根内由负转正 (绿柱缩短→红柱)"""
    if hist is None or len(hist) < lookback + 1:
        return False
    n = len(hist)
    if hist[n-1] <= 0:
        return False  # 当前柱还是负的
    for i in range(max(0, n - lookback - 1), n - 1):
        if hist[i] < 0:
            return True
    return False


def is_macd_hist_shrinking_negative(hist, lookback=5):
    """判断MACD绿柱是否在缩短 (负柱绝对值在减小)"""
    if hist is None or len(hist) < lookback:
        return False
    n = len(hist)
    recent = hist[n - lookback:]
    if any(h >= 0 for h in recent):
        return False
    abs_vals = [abs(h) for h in recent]
    return abs_vals[-1] < abs_vals[-2] < abs_vals[-3] if len(abs_vals) >= 3 else abs_vals[-1] < abs_vals[-2]
