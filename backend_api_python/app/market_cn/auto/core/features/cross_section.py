"""横截面 regime 门 + 单票 G1 特征 (2026-09-26 从 strategies/g56.py 下沉)。

为什么下沉: core/runtime/evaluate.py 和 core/present/pipeline.py 各自有惰性
`from ...strategies.g56 import _ensure_pool_daily, _g1_arrays` —— 违反 core 零策略知识
红线。这两个函数是通用横截面/特征计算, 不依赖 g56 的评分/门表规则, 独立出来后 core
和 strategies 都从这里导入, 层清零。

包含:
  - 纯 numpy helper: _roll_sum / _sma_np / _rsi / _atr / _boll_pctb / _pctl_roll
  - 单票特征: _g1_arrays(bars) → 全序列 dict (rma / rma_chg / atr / rsi / big20 / ma20 / rhist_chg / dif0 / pctb / dist_ma20 / dates)
  - G1池 mask: _g1_mask(f, board) → 全D-1判定
  - 横截面聚合: _aggregate(by_date) + _ensure_pool_daily(pool_target, bars_batch=None)
  - 常量: ATR_Q5 (G1池 ATR14% 板块Q5下限) / ROLL / MIN_HIST (score_r 滚动窗口)

依赖: calc_macd (core.indicators) / hub (core.data) / get_board_type (core.market) / numpy
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from app.market_cn.auto.core.indicators import calc_macd
from app.market_cn.auto.core.market import get_board_type

logger = logging.getLogger("auto")

# ---- 横截面 regime 门参数 (从 g56.py 下沉) ----
ATR_Q5 = {"main": 5.07, "gem_star": 6.42}   # G1池 ATR14% 下限 = 板块池内Q5
ROLL = 20                                    # score_r 滚动分位窗口 (交易日)
MIN_HIST = 5                                 # 分位最少历史天数 (不足为 None → 20cm 不产信号)

# ---- 池缓存 ----
_POOL_LOCK = threading.Lock()
_POOL = {"target": None, "main": {}, "gem_star": {}}


# ================================================================
# 纯 numpy helper (逐字移植 tmp/migrate_150.py 行49-73, 保证对账一致)
# ================================================================

def _roll_sum(x, n):
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    out[n - 1:] = cs[n:] - cs[:-n]
    return out


def _sma_np(x, n):
    return _roll_sum(x, n) / n


def _rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    g = _roll_sum(np.clip(d, 0, None)[1:], n)
    lo = _roll_sum(np.clip(-d, 0, None)[1:], n)
    out = np.full(len(c), np.nan)
    out[1:] = 100 - 100 / (1 + (g / n) / np.where(lo == 0, np.nan, lo / n))
    return out


def _atr(h, l, c, n=14):
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return _roll_sum(tr, n) / n / c * 100


def _boll_pctb(c, n=20, k=2.0):
    """布林 %b (0-100 口径: lo=0, up=100); 前 n-1 根 NaN; 带退化记 50 中性。"""
    m = len(c)
    pctb = np.full(m, np.nan)
    w = np.ones(n) / n
    ma = np.convolve(c, w, "valid")
    c2 = np.convolve(c * c, w, "valid")
    sd = np.sqrt(np.maximum(c2 - ma * ma, 0.0))
    lo = ma - k * sd
    up = ma + k * sd
    denom = up - lo
    pctb[n - 1:] = np.where(denom > 0, (c[n - 1:] - lo) / denom * 100, 50.0)
    return pctb


def _pctl_roll(day_val, w=ROLL, min_hist=MIN_HIST):
    """日度序列滚动分位 (不含当日, 零前视); 历史不足 min_hist 为 nan — 同 regime3。"""
    out = np.full(len(day_val), np.nan)
    for i in range(len(day_val)):
        hist = day_val[max(0, i - w):i]
        if len(hist) >= min_hist:
            out[i] = (hist < day_val[i]).mean()
    return out


# ================================================================
# 单票 G1 特征 (全序列, 信号判定与池聚合共用同一实现 — 单一事实源)
# ================================================================

def _g1_arrays(bars):
    """日线 → G1特征序列 dict (warmup 段 NaN, 由门比较自然过滤)。

    特征: rma/rma_chg (MA5/MA10 收敛), atr (ATR14%), rsi (研究口径), big20
    (前20日≥5%大涨日数), rhist_chg (MACD柱日差/前收), dif0 (MACD柱/现收),
    pctb (布林%b), ma20 (MA20, 评分用 dist_ma20 的分母), dates (YYYY-MM-DD)。
    需要 len>=35 (calc_macd 下限), 调用方保证。

    ⚠ 传 list[float] 给 calc_macd (纯 Python 循环, ndarray 每次 numpy 标量装箱慢 1 量级,
    二者同为 IEEE-754 double, 结果逐位一致 — tmp/_macd_exact_test.py: 400 组 0 不一致)。
    """
    c = np.array([float(b["close"]) for b in bars])
    h = np.array([float(b["high"]) for b in bars])
    l = np.array([float(b["low"]) for b in bars])
    n = len(c)
    ma5, ma10, ma20 = _sma_np(c, 5), _sma_np(c, 10), _sma_np(c, 20)
    dif, dea, _ = calc_macd(c.tolist())
    dif, dea = np.asarray(dif, float), np.asarray(dea, float)
    rma = (ma5 / ma10 - 1) * 100
    rma_chg = np.full(n, np.nan)
    rma_chg[1:] = rma[1:] - rma[:-1]
    rhist = (dif - dea) / c * 100
    rh_prev = np.full(n, np.nan)
    rh_prev[1:] = (dif[:-1] - dea[:-1]) / c[:-1] * 100
    pct = np.zeros(n)
    pct[1:] = c[1:] / c[:-1] - 1
    cb = np.cumsum(pct >= 0.05)
    cb_prev = np.zeros(n)
    cb_prev[20:] = cb[:-20]
    return {
        "rma": rma, "rma_chg": rma_chg, "atr": _atr(h, l, c),
        "rsi": _rsi(c), "big20": cb - cb_prev, "ma20": ma20,
        "rhist_chg": rhist - rh_prev, "dif0": dif / c * 100,
        "pctb": _boll_pctb(c), "dates": [str(b["time"])[:10] for b in bars],
        "dist_ma20": (c / ma20 - 1) * 100,
    }


def _g1_mask(f, board):
    """G1池成员 mask (全D-1判定, 同 g1deep3 行127-131): NaN 比较为 False 自然暖机。"""
    m = ((np.abs(f["rma"]) <= 2.5) & (f["rma_chg"] > 0)
         & (f["atr"] > ATR_Q5[board]) & (f["big20"] >= 2))
    for key in ("rma", "rma_chg", "atr", "rhist_chg", "dif0", "pctb", "rsi", "ma20"):
        m &= np.isfinite(f[key])
    m[:68] = False       # 暖机下限 (同研究 s>=68 → 特征日 k>=67)
    return m


# ================================================================
# 横截面 regime 门: 惰性聚合缓存 (单票回调架构下的池级统计量解法)
# ================================================================

def _aggregate(by_date):
    """日度桶 → {date: {rmed, score_r}}; score_r = 四项等权滚动分位 (同 regime3/5)。"""
    dates = sorted(by_date)
    if not dates:
        return {}
    n_ = np.array([len(by_date[d][0]) for d in dates], dtype=float)
    rmed = np.array([np.median(by_date[d][0]) for d in dates])
    dmed = np.array([np.median(by_date[d][1]) for d in dates])
    smed = np.array([np.median(by_date[d][2]) for d in dates])
    score = np.nanmean(np.vstack([
        _pctl_roll(n_), _pctl_roll(rmed),
        1 - _pctl_roll(dmed), 1 - _pctl_roll(smed)]), axis=0)
    return {d: {"rmed": float(rmed[i]),
                "score_r": float(score[i]) if np.isfinite(score[i]) else None}
            for i, d in enumerate(dates)}


def _ensure_pool_daily(pool_target, bars_batch=None):
    """返回 {target, main:{date:{rmed,score_r}}, gem_star:{...}} (key=pool_target 跨日失效; 失败不缓存)。

    前视防护: 每票日线截断到 ≤pool_target — 实盘扫描日=快照末日; 回测时
    pool_target=快照末日(今日), 历史 date 的统计仅由 ≤该日 数据构成。

    bars_batch: 可选 `{code: bars}`。由调用方**批量预加载**(窗口/复权/截断必须与
      `hub.daily(code, 200, as_of=pool_target)` 完全一致, 见 data.kline.fetch_klines_batch),
      用于消除逐票 `hub.daily` 的 O(N) 往返 —— 横截面池是展示管线夜间的主要超支源。
      缺省 None → 保持原逐票路径 (单票调用方/实盘扫描零改动)。
    """
    with _POOL_LOCK:
        if _POOL["target"] == pool_target:
            return dict(_POOL)               # M19 同款: 命中路径也不交出共享引用
        from app.market_cn.auto.core.data import hub
        try:
            buckets = {"main": {}, "gem_star": {}}
            codes = hub.all_codes()
            for code in codes:
                if code.startswith(("8", "4", "92")):   # 北交所/老三板 (同研究口径)
                    continue
                if bars_batch is not None:
                    bars = bars_batch.get(code) or []
                else:
                    bars = hub.daily(code, 200, as_of=pool_target)
                if len(bars) < 68:
                    continue
                board = get_board_type(code)
                if board not in buckets:
                    continue
                f = _g1_arrays(bars)
                for k in np.nonzero(_g1_mask(f, board))[0]:
                    b = buckets[board].setdefault(f["dates"][k], [[], [], []])
                    b[0].append(f["rhist_chg"][k])
                    b[1].append(f["dif0"][k])
                    b[2].append(f["rsi"][k])
            main_map, gem_map = _aggregate(buckets["main"]), _aggregate(buckets["gem_star"])
        except Exception as e:
            logger.error("[g56] 池聚合失败, 当日横截面门不可用: %s", e)
            return {"target": pool_target, "main": {}, "gem_star": {}}
        logger.info("[g56] 池聚合完成 target=%s 主板%d日/20cm%d日",
                    pool_target, len(main_map), len(gem_map))
        _POOL.update({"target": pool_target, "main": main_map, "gem_star": gem_map})
        # M19 (2026-09-28): 返回**新** dict, 不返回共享 _POOL —— 原实现把共享单槽缓存
        # 直接交出, 下一个 pool_target _POOL.update 原地改写时, 调用方手里的旧引用
        # (上一决策日的池统计) 被静默换成新日数据 → 跨日重放中先前日期的门判定漂移。
        return {"target": pool_target, "main": main_map, "gem_star": gem_map}
