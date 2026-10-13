#!/usr/bin/env python3
"""技术指标库 (基座共享叶子层) —— 2026-09-23 自 app/market_cn/auto/core/indicators.py 上移

上移原因: 该库原位于 auto/ 内部, 且全仓 auto 之外零引用; 而展示层 (app/watchlist) 需要
与策略判定同一份指标口径 ⇒ 上移为共享叶子层 (与 db_market / trading_calendar / basicinfo_db 同层)。
2026-10-07: 原路径的 re-export shim (app/market_cn/auto/core/indicators.py) **已删除** ——
auto 内 10 处引用已统一改指本模块, 指标口径只有一处定义 (无第二跳)。
本文件内容与原文件逐字一致 (含全部数值口径), 上移不改变任何计算结果。

2026-10-13 P7/P8: 本模块升格为**全仓指标算法唯一真源** ——
  P7 把 atr/mfi/cmf/hist_volatility/ma_convergence/obv/detect_divergence/safe_round
  从 agent 侧物理收编进来; P8 收编布林三轨 (bollinger), 并把 legacy
  `analysis_tools._calc_*` 全部降级为语义包装/一行转调。
  ⚠ 新增或修改算法时, 调用方不得在别处另起一份实现 —— 防回流守卫见
  tests/test_v7_indicator_single_source.py 与 tests/test_v8_legacy_indicator_delegation.py。

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

import math
from typing import Any, Dict, List


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


# ================================================================
# RS(相对强弱) —— Wilder 递推, 单一实现 (2026-10-07)
#   state = [avg_g, avg_l] (Wilder 平滑的均涨幅/均跌幅) —— 有界摘要 (2 个 float)。
#   三种口径共用同一份递推:
#       rsi_state(closes,p)    全量 → state (seed, O(n))
#       rsi_step(state,pc,c,p) 逐格推进 (O(1))
#       rsi_value(state)       state → RSI 数值
#       rsi(closes,p)          终端值 = rsi_value(rsi_state(closes, p))
#   ★ 此前 `strategies/dragon_callback.rsi_init/rsi_step/rsi_value` 另写一份**同式**
#     实现 (周期 6): 与 `rsi()` 逐位同源却各改各的 = 漂移温床。现收敛到本处,
#     dragon 改为委托 (见 core/increm.RsiKernel / dragon 顶部 import)。
# ⚠ 与 `core/features/cross_section._rsi` **不是**同一指标: 后者用**滑窗均值**
#   (SMA of gains/losses, period=14, 序列口径), 本处是 **Wilder 平滑**。二者数值
#   不同, 各有其门阈值 —— **不要互换**。
# ================================================================

def rsi_state(closes, period=14):
    """全量 closes → Wilder 状态 [avg_g, avg_l]; len < period+1 → None。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 初始 SMA
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # Wilder 平滑
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return [avg_g, avg_l]


def rsi_step(state, prev_close, close, period=14):
    """Wilder 递推一步 (state None → None)。"""
    if state is None:
        return None
    d = close - prev_close
    return [(state[0] * (period - 1) + max(d, 0)) / period,
            (state[1] * (period - 1) + max(-d, 0)) / period]


def rsi_value(state):
    """Wilder 状态 → RSI 数值 (state None → None)。"""
    if state is None:
        return None
    avg_g, avg_l = state
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def rsi(closes, period=14):
    """计算RSI (相对强弱指数) —— 终端值 = rsi_value(rsi_state(closes, period))。"""
    return rsi_value(rsi_state(closes, period))


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


def bollinger(closes, period=20, num_std=2):
    """布林带三轨 —— 全仓唯一真源（2026-10-13 P8 收编自 agent/tools/finance）。

    中轨 = SMA(period)，上下轨 = 中轨 ± num_std × 总体标准差（除以 n 而非 n-1，
    与国内行情软件口径一致，**勿"修正"成样本标准差**）。

    Returns:
        {upper, mid, lower, bandwidth(%)} 或 None（长度不足）
    ★ mid==0 的退化情形仍返回结果（bandwidth 取 0），因为"带宽 0"是有效结论；
      只有长度不足才是失败 —— 调用方据此区分"算不出"与"算出来是 0"。
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return {
        "upper": upper,
        "mid": mid,
        "lower": lower,
        "bandwidth": ((upper - lower) / mid * 100) if mid else 0,
    }


def calc_bollinger_bw(closes, period=20, num_std=2):
    """计算布林带宽百分比 = (upper-lower)/middle*100, 仅返回带宽值

    ★ 2026-10-13 P8：改为复用 `bollinger`，带宽算法不再有第二份。
    """
    b = bollinger(closes, period, num_std)
    if b is None or b["mid"] <= 0:
        return None
    return b["bandwidth"]


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


# ═══════════════════════════════════════════════════════════════════
# 2026-10-13 · P7 单一真源收编
#   以下 8 个函数原为 `app/agentcore/tools/indicators.py` 的 B 类搬运副本，
#   （源头是 legacy `analysis_tools._calc_*` / `_analysis_utils._calc_obv`）。
#   三处并存 ⇒ 改一个指标要改三处、漏一处就是口径分歧。
#
#   现收编进本模块（回测/策略/展示层共用的叶子层），**算法逐行不变**：
#     safe_round / atr / mfi / cmf / hist_volatility /
#     detect_divergence / ma_convergence / obv
#   收编前已做数值对拍：与 legacy _calc_* **完全一致**（见
#   tests/test_v7_indicator_single_source.py），所以这次搬迁不改变任何计算结果。
#
#   ★ 中文研判（signals/zone/volatility_level）是这批资产的价值所在，
#     不是噪音 —— 模型拿到 "MFI超买（>80）" 比拿到 82.3 更有判断力，别删。
# ═══════════════════════════════════════════════════════════════════

def safe_round(v: float, n: int = 4) -> float:
    """安全四舍五入，处理 NaN/Inf/None。

    ★ NaN 不是合法 JSON（json.dumps 默认会吐出 `NaN`，多数 LLM 接口直接拒收）
    ⇒ 一律归 0，让调用方看得见"这里算不出来"。
    """
    if v is None or math.isnan(v) or math.isinf(v):
        return 0.0
    return round(v, n)


def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> Dict[str, Any]:
    """ATR（真实波幅均值）— 波动率指标。

    TR = max(high-low, |high-prev_close|, |low-prev_close|)，Wilder 平滑
    （不是简单算术平均）：atr = (atr*(n-1) + tr) / n。

    Returns:
        {atr, atr_pct, atr_trend, volatility_level} 或 {"error": str}
    """
    n = len(closes)
    if n < period + 1:
        return {"error": f"数据不足，ATR需要至少{period + 1}根K线"}

    tr_list = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    # Wilder 平滑
    atr_val = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period

    atr_pct = safe_round(atr_val / closes[-1] * 100, 2) if closes[-1] else 0

    # ATR 趋势（近 5 日）
    atr_recent = []
    temp_atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        temp_atr = (temp_atr * (period - 1) + tr_list[i]) / period
        if i >= len(tr_list) - 5:
            atr_recent.append(temp_atr)

    if len(atr_recent) >= 2:
        atr_trend = "扩大" if atr_recent[-1] > atr_recent[0] * 1.1 else (
            "收缩" if atr_recent[-1] < atr_recent[0] * 0.9 else "平稳")
    else:
        atr_trend = "数据不足"

    # 波动率等级（A 股经验分档，改动等于改判据）
    if atr_pct >= 5:
        vol_level = "极高波动（日内振幅大，适合短线）"
    elif atr_pct >= 3:
        vol_level = "高波动"
    elif atr_pct >= 2:
        vol_level = "中等波动"
    elif atr_pct >= 1:
        vol_level = "低波动（横盘整理）"
    else:
        vol_level = "极低波动（变盘前兆）"

    return {
        "atr": safe_round(atr_val),
        "atr_pct": atr_pct,
        "atr_trend": atr_trend,
        "volatility_level": vol_level,
    }


def mfi(highs: List[float], lows: List[float], closes: List[float],
        volumes: List[float], period: int = 14) -> Dict[str, Any]:
    """MFI（资金流量指标）— 量价结合的 RSI。

    典型价格 tp=(h+l+c)/3，资金流 mf=tp*vol；tp 上升记正流、下降记负流，
    Wilder 平滑后取 MFR = 正/负，MFI = 100 - 100/(1+MFR)。

    Returns:
        {mfi, signals: [...]} 或 {"error": str}
    """
    n = len(closes)
    if n < period + 1:
        return {"error": f"数据不足，MFI需要至少{period + 1}根K线"}

    # 典型价格
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    mf = [tp[i] * volumes[i] for i in range(n)]

    pos_mf, neg_mf = [], []
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            pos_mf.append(mf[i])
            neg_mf.append(0)
        elif tp[i] < tp[i - 1]:
            pos_mf.append(0)
            neg_mf.append(mf[i])
        else:
            pos_mf.append(0)
            neg_mf.append(0)

    # Wilder 平滑
    avg_pos = sum(pos_mf[:period]) / period
    avg_neg = sum(neg_mf[:period]) / period
    for i in range(period, len(pos_mf)):
        avg_pos = (avg_pos * (period - 1) + pos_mf[i]) / period
        avg_neg = (avg_neg * (period - 1) + neg_mf[i]) / period

    if avg_neg == 0:
        mfi_val = 100.0
    else:
        mfr = avg_pos / avg_neg
        mfi_val = 100 - 100 / (1 + mfr)

    mfi_val = safe_round(mfi_val, 2)

    signals = []
    if mfi_val >= 80:
        signals.append("MFI超买（>80），资金流入过热")
    elif mfi_val <= 20:
        signals.append("MFI超卖（<20），资金流出过度")

    return {
        "mfi": mfi_val,
        "signals": signals,
    }


def cmf(highs: List[float], lows: List[float], closes: List[float],
        volumes: List[float], period: int = 20) -> Dict[str, Any]:
    """CMF（蔡金资金流量）— 衡量一段时间内资金流入/流出强度。

    CLV = ((close-low) - (high-close)) / (high-low)，取值 [-1, +1]；
    CMF = sum(CLV*vol, period) / sum(vol, period)，也是 [-1, +1]。

    Returns:
        {cmf, signals: [...]} 或 {"error": str}
    """
    n = len(closes)
    if n < period:
        return {"error": f"数据不足，CMF需要至少{period}根K线"}

    # CLV = ((close - low) - (high - close)) / (high - low)
    clv = []
    for i in range(n):
        hl_range = highs[i] - lows[i]
        if hl_range == 0:
            clv.append(0)
        else:
            clv.append(((closes[i] - lows[i]) - (highs[i] - closes[i])) / hl_range)

    ad_line = [clv[i] * volumes[i] for i in range(n)]

    # CMF = sum(AD, period) / sum(Volume, period)
    cmf_vals = []
    for i in range(period - 1, n):
        ad_sum = sum(ad_line[i - period + 1:i + 1])
        vol_sum = sum(volumes[i - period + 1:i + 1])
        if vol_sum == 0:
            cmf_vals.append(0)
        else:
            cmf_vals.append(ad_sum / vol_sum)

    cmf_val = safe_round(cmf_vals[-1], 4) if cmf_vals else 0

    signals = []
    if cmf_val > 0.1:
        signals.append("CMF强正值，持续资金流入")
    elif cmf_val > 0:
        signals.append("CMF正值，资金温和流入")
    elif cmf_val < -0.1:
        signals.append("CMF强负值，持续资金流出")
    elif cmf_val < 0:
        signals.append("CMF负值，资金温和流出")

    return {
        "cmf": cmf_val,
        "signals": signals,
    }


def hist_volatility(closes: List[float], period: int = 20) -> Dict[str, Any]:
    """历史波动率（基于对数收益率标准差，年化）。

    年化系数 **242**（A 股年交易日数），不是 252（美股）也不是 365。

    Returns:
        {daily_volatility, annualized_volatility, short_term_volatility,
         vol_trend}（单位 %）或 {"error": str}
    """
    n = len(closes)
    if n < period + 1:
        return {"error": f"数据不足，波动率需要至少{period + 1}根K线"}

    log_returns = []
    for i in range(1, n):
        if closes[i] > 0 and closes[i - 1] > 0:
            log_returns.append(math.log(closes[i] / closes[i - 1]))
        else:
            log_returns.append(0)

    # 滚动计算
    if len(log_returns) < period:
        return {"error": "收益率数据不足"}

    recent = log_returns[-period:]
    mean_r = sum(recent) / period
    variance = sum((r - mean_r) ** 2 for r in recent) / (period - 1)
    daily_vol = math.sqrt(variance)
    annual_vol = daily_vol * math.sqrt(242)  # A股约 242 个交易日

    # 短期波动率（5日）
    short_period = min(5, len(log_returns))
    short_recent = log_returns[-short_period:]
    short_mean = sum(short_recent) / short_period
    short_var = sum((r - short_mean) ** 2 for r in short_recent) / max(short_period - 1, 1)
    short_vol = math.sqrt(short_var) * math.sqrt(242)

    # 波动率变化趋势
    if short_vol > annual_vol * 1.3:
        vol_trend = "短期波动率放大（情绪升温）"
    elif short_vol < annual_vol * 0.7:
        vol_trend = "短期波动率收缩（可能变盘）"
    else:
        vol_trend = "波动率平稳"

    return {
        "daily_volatility": safe_round(daily_vol * 100, 2),
        "annualized_volatility": safe_round(annual_vol * 100, 2),
        "short_term_volatility": safe_round(short_vol * 100, 2),
        "vol_trend": vol_trend,
    }


def detect_divergence(closes: List[float], indicator: List[float],
                      lookback: int = 20) -> str:
    """检测价格与指标的背离。

    返回: 'bullish_div' / 'bearish_div' / 'none'
    - 底背离(bullish_div): 价格创新低但指标未新低
    - 顶背离(bearish_div): 价格创新高但指标未新高

    判据是**位置**而非数值：价格极值落在窗口后 40%、指标极值落在前 40%。
    """
    if len(closes) < lookback or len(indicator) < lookback:
        return "none"

    price_window = closes[-lookback:]
    ind_window = indicator[-lookback:]

    # 找价格和指标各自的极值
    price_min_idx = price_window.index(min(price_window))
    price_max_idx = price_window.index(max(price_window))
    ind_min_idx = ind_window.index(min(ind_window))
    ind_max_idx = ind_window.index(max(ind_window))

    # 底背离：价格在后半段创新低，但指标低点在前半段
    if price_min_idx >= lookback * 0.6 and ind_min_idx < lookback * 0.4:
        if min(ind_window[-lookback // 3:]) > min(ind_window[:lookback // 3]):
            return "bullish_div"

    # 顶背离：价格在后半段创新高，但指标高点在前半段
    if price_max_idx >= lookback * 0.6 and ind_max_idx < lookback * 0.4:
        if max(ind_window[-lookback // 3:]) < max(ind_window[:lookback // 3]):
            return "bearish_div"

    return "none"


def ma_convergence(closes: List[float]) -> Dict[str, Any]:
    """均线收敛度检测 — 多条均线粘合程度。

    ★ 不足 60 根时返回 `{"converged": False}` 而**不是** `{"error":...}`：
    序列短 ⇒ 结论就是"不粘合"，这是有效判断而非失败。调用方据此区分
    "取数失败"与"判断未成立"。

    Returns:
        {converged, max_deviation_pct, description} 或 {"converged": False}
    """
    if len(closes) < 60:
        return {"converged": False}

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60

    avg = (ma5 + ma10 + ma20 + ma60) / 4
    if avg == 0:
        return {"converged": False}

    # 各均线偏离平均值的最大幅度
    max_dev = max(
        abs(ma5 - avg) / avg,
        abs(ma10 - avg) / avg,
        abs(ma20 - avg) / avg,
        abs(ma60 - avg) / avg
    ) * 100

    converged = max_dev < 2.0  # 所有均线在 2% 以内 = 粘合

    return {
        "converged": converged,
        "max_deviation_pct": safe_round(max_dev, 2),
        "description": "均线高度粘合，即将变盘" if converged else "均线发散中",
    }


def obv(closes: List[float], volumes: List[float]) -> Dict[str, Any]:
    """OBV（能量潮）指标计算。

    涨加量、跌减量、平不变，累积成一条曲线；再看它与价格的背离。

    Returns:
        {obv, obv_prev, obv_slope, obv_trend, obv_vs_ma, divergence,
         signals: [...]} 或 {"error": str}
    """
    if len(closes) < 3 or len(closes) != len(volumes):
        return {"error": "数据不足"}

    obv_series = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv_series.append(obv_series[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv_series.append(obv_series[-1] - volumes[i])
        else:
            obv_series.append(obv_series[-1])

    # OBV 趋势：近 5 日斜率
    window = min(5, len(obv_series))
    if window >= 2:
        obv_slope = (obv_series[-1] - obv_series[-window]) / (window - 1)
    else:
        obv_slope = 0

    # OBV 与价格背离检测
    divergence = "无"
    if len(closes) >= 10:
        price_up = closes[-1] > closes[-10]
        obv_up = obv_series[-1] > obv_series[-10]
        if price_up and not obv_up:
            divergence = "顶背离（价格新高但OBV未新高，量价背离看空）"
        elif not price_up and obv_up:
            divergence = "底背离（价格新低但OBV未新低，量价背离看多）"

    # OBV 均线
    obv_ma = sum(obv_series[-10:]) / min(10, len(obv_series))
    obv_vs_ma = "OBV在均线上方（多头量能）" if obv_series[-1] > obv_ma else "OBV在均线下方（空头量能）"

    signals = []
    if divergence != "无":
        signals.append(divergence)
    if obv_slope > 0 and closes[-1] > closes[-2]:
        signals.append("OBV上升+价格上涨，量价配合良好")
    elif obv_slope < 0 and closes[-1] < closes[-2]:
        signals.append("OBV下降+价格下跌，空头量能释放")
    elif obv_slope > 0 and closes[-1] < closes[-2]:
        signals.append("OBV上升但价格下跌，可能有资金吸筹")
    elif obv_slope < 0 and closes[-1] > closes[-2]:
        signals.append("OBV下降但价格上涨，上涨缺乏量能支撑")

    return {
        "obv": safe_round(obv_series[-1], 0),
        "obv_prev": safe_round(obv_series[-2], 0),
        "obv_slope": safe_round(obv_slope, 0),
        "obv_trend": "上升" if obv_slope > 0 else "下降",
        "obv_vs_ma": obv_vs_ma,
        "divergence": divergence,
        "signals": signals,
    }
