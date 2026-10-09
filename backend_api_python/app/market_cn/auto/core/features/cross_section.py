"""横截面 regime 门 + 单票 G1 特征 (2026-09-26 从 strategies/g56.py 下沉)。

为什么下沉: core/runtime/evaluate.py 和已退役的 core/present/pipeline.py 曾各自有惰性
`from ...strategies.g56 import _ensure_pool_daily, _g1_arrays` —— 违反 core 零策略知识
红线。这两个函数是通用横截面/特征计算, 不依赖 g56 的评分/门表规则, 独立出来后 core
和 strategies 都从这里导入, 层清零。

包含:
  - 纯 numpy helper: _roll_sum / _sma_np / _rsi / _atr / _boll_pctb / _pctl_roll
  - 单票特征: _g1_arrays(bars) → 全序列 dict (rma / rma_chg / atr / rsi / big20 / ma20 / rhist_chg / dif0 / pctb / dist_ma20 / dates)
  - G1池 mask: _g1_mask(f, board) → 全D-1判定
  - 横截面聚合: _aggregate(by_date) + _ensure_pool_daily(pool_target, bars_batch=None)
  - 常量: ATR_Q5 (G1池 ATR14% 板块Q5下限) / ROLL / MIN_HIST (score_r 滚动窗口)

依赖: calc_macd (app.utils.indicators) / hub (core.data) / get_board_type (core.market) / numpy
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from app.utils.indicators import calc_macd
from app.market_cn.auto.core.market import get_board_type
# ★ 播种/接力只从基座叶子层取 (macd_state/macd_core 直接取自本文件顶层导入)
from app.utils.indicators import macd_core, macd_state

logger = logging.getLogger("auto")

# ---- 横截面 regime 门参数 (从 g56.py 下沉) ----
ATR_Q5 = {"main": 5.07, "gem_star": 6.42}   # G1池 ATR14% 下限 = 板块池内Q5
ROLL = 20                                    # score_r 滚动分位窗口 (交易日)
MIN_HIST = 5                                 # 分位最少历史天数 (不足为 None → 20cm 不产信号)

# ---- 池缓存 ----
_POOL_LOCK = threading.Lock()
_POOL = {"target": None, "main": {}, "gem_star": {}}


# ================================================================
# 通用滚动核 —— 2026-10-07 已提炼到 `core/increm.py`（唯一增量形态，单一 home）。
# 本文件只保留**业务参数化包装**：G1 的窗口常量 (ROLL/MIN_HIST/MACD_*) 属于本层，
# 不搬进 core（core 零业务常量）。纯 numpy 实体见 increm.roll_sum / sma / rsi /
# atr_pct / boll_pctb / pctl_roll / window_of / anchor_step。
# ================================================================

from app.market_cn.auto.core import increm  # noqa: E402
from app.market_cn.auto.core.increm import (  # noqa: E402
    anchor_step,
    atr_pct as _atr,
    boll_pctb as _boll_pctb,
    roll_sum as _roll_sum,
    rsi as _rsi,
    sma as _sma_np,
    window_of as _window_of,
)


def _pctl_roll(day_val, w=ROLL, min_hist=MIN_HIST):
    """G1 口径的滚动分位 (业务窗口默认 ROLL/MIN_HIST)；实体在 increm.pctl_roll。"""
    return increm.pctl_roll(day_val, w, min_hist)


# ================================================================
# 单票 G1 特征 (全序列, 信号判定与池聚合共用同一实现 — 单一事实源)
# ================================================================

def _g1_arrays(bars, macd_anchor=None):
    """日线 → G1特征序列 dict (warmup 段 NaN, 由门比较自然过滤)。

    特征: rma/rma_chg (MA5/MA10 收敛), atr (ATR14%), rsi (研究口径), big20
    (前20日≥5%大涨日数), rhist_chg (MACD柱日差/前收), dif0 (MACD柱/现收),
    pctb (布林%b), ma20 (MA20, 评分用 dist_ma20 的分母), dates (YYYY-MM-DD)。

    macd_anchor: **(2026-10-05 新增, 可选)** MACD 初值播种 (ef, es, dea) = 本窗口
      首根**前一根**结束时的状态。传了它, 本窗口的 dif/dea 与"从上市首日算下来"
      **浮点等价** (EMA 无限记忆被锚补偿), 且**不再有 len>=35 门槛**。
      缺省 None = 原朴素播种路径, 与历史口径**逐位一致**, 仍需 len>=35。

    ⚠ 不传锚时 len<35: `calc_macd` 返回 None ⇒ 下游 IndexError (dummy MACD 退化成
      0 维数组)。这是**历史契约**, 未改; 播种路径无此限制 (见 `G1_WIN_MIN`)。

    ⚠ 传 list[float] 给 calc_macd (纯 Python 循环, ndarray 每次 numpy 标量装箱慢 1 量级,
    二者同为 IEEE-754 double, 结果逐位一致 — tmp/_macd_exact_test.py: 400 组 0 不一致)。
    """
    c = np.array([float(b["close"]) for b in bars])
    h = np.array([float(b["high"]) for b in bars])
    l = np.array([float(b["low"]) for b in bars])
    n = len(c)
    ma5, ma10, ma20 = _sma_np(c, 5), _sma_np(c, 10), _sma_np(c, 20)
    if macd_anchor is None:
        dif, dea, _ = calc_macd(c.tolist())
    else:
        # 锚定接力: 无长度门槛, 无瞬态 —— 与长序列"算下来"浮点等价
        dif, dea, _ = macd_core(c.tolist(), anchor=macd_anchor)
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


#: 暖机下限 (数据年龄): **全量口径下 特征日 k >= 68**（下方 `m[:G1_WARMUP]=False`）。
#: ⚠ 实现口径是「特征日 k >= 68」，对应入场日 s = k+1 >= 69。历史上本行曾写「k>=67」
#:   （按研究 s>=68 ⇒ k=s-1=67 推），但**代码与全系统**（`_g1_mask` / `g56_warmup` /
#:   `scan_*` / 旧 `_g56_gate`）一致采用 k>=68 —— 以代码为准（2026-10-09 澄清，修 g56
#:   折叠路径误按「计数」判暖机早放行一天的 bug，见 docs/终态②问题A-g56门诊断对齐修复.md）。
G1_WARMUP = 68


def _g1_mask(f, board, age=None):
    """G1池成员 mask (全D-1判定, 同 g1deep3 行127-131): NaN 比较为 False 自然暖机。

    age: **逻辑数据年龄** = 该票自数据起点累计的日线根数 (2026-10-05 新增, 可选)。
      ★ 暖机约束的是"这票有多少历史", **不是"这次传进来几根"** ——
        截窗 + 播种后窗口可能只有 21~35 根, 但票的实际年龄是几百根。
      ⚠ 不传 = 视作 `age == len(f)` (旧语义, 逐位一致) —— 只有**播种路径**才该传。
        传了却给错 (小于真实年龄) 会让暖机失效 ⇒ 宁可不传。
    """
    m = ((np.abs(f["rma"]) <= 2.5) & (f["rma_chg"] > 0)
         & (f["atr"] > ATR_Q5[board]) & (f["big20"] >= 2))
    for key in ("rma", "rma_chg", "atr", "rhist_chg", "dif0", "pctb", "rsi", "ma20"):
        m &= np.isfinite(f[key])
    n = len(f["rma"])
    cut = G1_WARMUP if age is None else max(0, G1_WARMUP - (int(age) - n))
    if cut:
        m[:cut] = False
    return m


# ================================================================
# G1 增量状态 (2026-10-05): 「预处理 = 回测同一套数据流 + 每天推进一格」
# ----------------------------------------------------------------
# 为什么需要: 上面 `_g1_arrays` 吃的是**窗口**, 而 EMA 是无限记忆 ⇒ 窗口不够长就
#   与全量不等价 (实测: 无播种时 `dif0` 要 240 根才浮点等价)。把"窗口首根之前的
#   EMA 状态"固化成 3 个 float, 窗口就能压到滑窗宽度。
#
# 状态 = {date, age, head, closes}:
#   head    倒数第 win 根的状态 = 「窗口首根的前一根」→ 供**本次算特征**播种
#   closes  窗口内的 win 个收盘价 (队列) → ★ **head 推进必须用它**:
#           head 落后末根 win 格, 推进一格要喂的是「**即将滑出窗口的那根**」
#           (= 队首), 不是新 bar! 喂错会发散 (实测第一步差 6e-3, 之后指数放大)。
#   age     逻辑数据年龄 (累计根数) → 暖机 `G1_WARMUP` 判的是它, 不是窗口长度
#   ★ 末根状态 (tail) 不存: 可由 head + closes 推 win 格得到, 存了反而多一处
#     要同步的东西。⇒ 每天 O(1), 状态 = 3 个 float + win 个 float + 2 个标量。
#
# ⚠ 除权/数据修正**不在这里处理**: 价格体系一变, 锚就作废。约定是预处理检测到
#   除权即在**新除权口径下重建全量** (用户 2026-10-05 裁定), 本模块只负责
#   **发现不一致就 fail-fast**(见 `G1_STATE_STATS`), 绝不静默降级。
# ================================================================

#: 播种后的窗口下界: big20 = cb[i]-cb[i-20] 需 i>=20 ⇒ 末根需 21 根;
#: 其余滑窗 (ma20 / pctb / rsi14 / atr14) 均 <=20 ⇒ 21 够。
#: ⚠ 这是**下界不是推荐值**; 生产取值应 >= 35 (给表达式回看留余量)。
G1_WIN_MIN = 21

#: MACD 参数 —— 与 `calc_macd` / `macd_state` 的默认值一致 (g56 从未传过自定义值)。
#: ⚠ 改这里等于改全市场的 MACD 口径, 必须同步 `g56` 的门阈值拟合。
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

#: 状态机可观测计数 (★ 静默降级是头号敌人: 任何作废/错位必须在这里留痕)
G1_STATE_STATS = {"init": 0, "step": 0, "reject": 0, "date_mismatch": 0}

# `_window_of` 已从 increm 导入（模块顶部 import 块），此处不再本地定义。


def g1_state_window_bars(state):
    """状态自带窗口 → bars dict 列表 (喂给 `_g1_arrays`)。长度 == state["win"]。"""
    return [{"time": w[0], "high": w[1], "low": w[2], "close": w[3]}
            for w in state["window"]]


def g1_state_init(bars, win=G1_WIN_MIN, keep_window=False):
    """全量 bars → 断点状态 (建状态只在**首次**或**除权重建**时做一次, O(n))。

    keep_window: True ⇒ 状态额外携带 win 根 OHLC 微缩窗口 (推进时它是**唯一**的历史
      来源)。False (默认) = 旧行为, 逐位一致。
    """
    n = len(bars)
    win = int(win)
    if win < G1_WIN_MIN:
        raise ValueError("win=%d < G1_WIN_MIN=%d" % (win, G1_WIN_MIN))
    if n < win + 1:
        raise ValueError("bars=%d 不足以建 win=%d 的状态" % (n, win))
    cl = [float(b["close"]) for b in bars]
    # ⚠ 锚的**构成**是 (ema_fast, ema_slow, dea) —— 不是 (dif, dea, hist)!
    #   `macd_core` 返回后者, 拿它当锚播种会得到**完全错误**的结果 (实测 dif0 差 20+)。
    #   取锚必须用 `macd_state` (单一实现, 朴素播种, 与 calc_macd 同源)。
    G1_STATE_STATS["init"] += 1
    st = {"v": 2, "win": win,
          "date": str(bars[-1]["time"])[:10],
          "age": n,
          "head": macd_state(cl, upto=n - win - 1),   # = 窗口首根的前一根
          "closes": cl[-win:]}                         # 窗口价格队列 (供 head 推进)
    if keep_window:
        st["window"] = _window_of(bars, win)
    return st


def _anchor_step(anchor, closes):
    """G1 口径的 MACD 锚推进（业务参数 MACD_FAST/SLOW/SIGNAL）。

    ★ 递推本体唯一实现在 `app.utils.indicators.macd_anchor_step`（经 increm.anchor_step
      转发）。此前本文件另写一份 ef/es/dif/dea = **第二份 MACD**，改内核忘改这里
      ⇒ 特征层 MACD 静默分叉且不报错。现仅保留业务参数包装。
    ⚠ 必须从**相对下标 0** 起推，不能写成"继承第 n-1 个"（错位 n-1 根且不报错）。
    """
    return anchor_step(anchor, closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)


def g1_state_step(state, bars_new):
    """状态推进: D 日末状态 + D+1.. 的新 bar → 新状态 (**每天只推进一格**, O(1))。

    ★ 每推进一格: 弹出**队首**价格去推进 `head`, 新价**入队尾** —— 队首正是
      "即将滑出窗口的那根"。这一步写反 (拿新 bar 推进 head) 会让锚**发散**,
      且不会报错 (实测 2026-10-05: 第一步差 6e-3, 20 步后差 1.6e-1)。

    ⚠ fail-fast: 新数据必须严格晚于状态日期; 否则抛 ValueError 并计数 ——
      除权/重发/乱序都会撞这条, **禁止**用 try/except 吞掉继续跑。
    """
    if not bars_new:
        return state
    d0 = str(bars_new[0]["time"])[:10]
    if d0 <= state["date"]:
        G1_STATE_STATS["reject"] += 1
        raise ValueError("状态日期 %s 不早于新数据首日 %s (重发/乱序/除权重建?)"
                         % (state["date"], d0))
    q = list(state["closes"])
    w = list(state["window"]) if state.get("window") else None
    head = state["head"]
    for b in bars_new:
        head = _anchor_step(head, [q[0]])       # ★ 队首, 不是新 bar
        q = q[1:] + [float(b["close"])]
        if w is not None:                       # 微缩窗口**同一次弹出**入队, 不可能漂移
            w = w[1:] + [[str(b["time"])[:10], float(b["high"]),
                          float(b["low"]), float(b["close"])]]
    G1_STATE_STATS["step"] += 1
    out = {"v": 2, "win": state["win"],
           "date": str(bars_new[-1]["time"])[:10],
           "age": state["age"] + len(bars_new),
           "head": head, "closes": q}
    if w is not None:
        out["window"] = w
    return out


def g1_state_features(state, bars_window=None):
    """状态 + 窗口 → 特征序列 (末位与全量**浮点等价**)。

    `bars_window`:
      None (推荐, 需 `keep_window=True` 建的状态) → 取**状态自带**窗口。
        ★ 只有这一份拷贝 ⇒ 不存在"状态与窗口不同步"这种失效模式。
      传值 → 外部窗口走强校验 (旧行为)。

    ⚠ 双重对齐校验 (错位是这套机制最危险的失效模式, 且**不报错**):
      ① 窗口长度必须 == state["win"] (否则 head 锚对不上首根前一根);
      ② 窗口末根日期必须 == state["date"] (否则状态与数据差了若干天)。
    """
    if bars_window is None:
        if not state.get("window"):
            G1_STATE_STATS["reject"] += 1
            raise ValueError("状态未携带 window (建时要 keep_window=True)")
        bars_window = g1_state_window_bars(state)
    if len(bars_window) != state["win"]:
        G1_STATE_STATS["reject"] += 1
        raise ValueError("窗口 %d 根 != state.win %d" % (len(bars_window), state["win"]))
    d = str(bars_window[-1]["time"])[:10]
    if d != state["date"]:
        G1_STATE_STATS["date_mismatch"] += 1
        raise ValueError("窗口末日 %s != 状态日期 %s" % (d, state["date"]))
    # 廉价强校验: 末根价格必须一致 (差一天/串票 99% 会撞这条, O(1))
    if state.get("closes") and float(bars_window[-1]["close"]) != state["closes"][-1]:
        G1_STATE_STATS["date_mismatch"] += 1
        raise ValueError("窗口末根价格 %s != 状态队尾 %s (窗口与状态不同步)"
                         % (bars_window[-1]["close"], state["closes"][-1]))
    return _g1_arrays(bars_window, macd_anchor=state["head"])


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
            # M19 同款: 命中路径也不交出共享引用; 且只交三键(src 是内部标记)
            return {k: _POOL[k] for k in ("target", "main", "gem_star")}
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
        _POOL["src"] = "full"
        # M19 (2026-09-28): 返回**新** dict, 不返回共享 _POOL —— 原实现把共享单槽缓存
        # 直接交出, 下一个 pool_target _POOL.update 原地改写时, 调用方手里的旧引用
        # (上一决策日的池统计) 被静默换成新日数据 → 跨日重放中先前日期的门判定漂移。
        return {"target": pool_target, "main": main_map, "gem_star": gem_map}
