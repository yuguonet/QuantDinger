"""strategies/g56.py — 五重共振 (原"56%规则G+", 三金叉共振G+链收窄版, 2026-09-17 定稿上线)

策略来源: tmp/regime3~5_150.py / g1deep3_150.py (近150天研究, MEMORY.md 行55-63/67)。

规则总览 (人类可读版; 精确阈值=下方冻结常量, 证据=MEMORY.md 行55-67):
入场 = D-1 盘后判定, 五重共振缺一不可 → D0 开盘买 (gap≥涨停幅度=一字/触板剔除):
  维度        信号                        判定什么
  ① 趋势     MA5/MA10 收敛 + rma_chg>0   短期均线靠拢、趋势向上, 不是下跌中继
  ② 波动     ATR14% > 板块Q5             波动率够, 有爆发空间 (太稳的股不动)
  ③ 基因     前20日≥5%大涨≥2次           有涨停/大阳基因, 不是慢牛型
  ④ 金叉预测 rhist_chg 板块池内前5%      MACD 柱加速变长, 金叉正在形成
  ⑤ 板块状态 板块动量为正 + 未超买        个股+板块同向, 不是个股独立异动
    (⑤ = 横截面 regime 门: 主板 rmed>0.25 & 布林%b≤49.87 / 20cm score_r>0.65 & dif0≤0)
评分 (展示 + **实盘每日限额截断键**) = 0.5·归一(dist_ma20) + 0.5·归一(−dif0),
  2026-09-24 由 rhist_chg 换键而来, 依据与阈值见 SCORE_* 常量处注释。
出场 = 纯 7d/-8% 无追踪 (2026-09-17 出场研究定稿: trail 保留系数全区间单调, 紧追踪
  系统性打断启动初期动量): 持有期任一日 (d≥2, T+1) low≤入场价×0.92 → 止损卖
  (跳空穿越按开盘价成交); 否则第 7 个交易日收盘卖。
分板块冻结口径 (2026-09-17, ①②③=G1池):
  主板 = G1池 & ④rhist_chg>0.51 & ⑤rmed>0.25 & %b≤49.87 (150天 +7.51%/81.0% 正3/4月)
  20cm = G1池 & ④rhist_chg>0.66 & ⑤score_r>0.65 & dif0≤0   (150天 +9.95%/83.9% 正4/5月)

关键设计:
  - 横截面 regime 门 (rmed/score_r 是 G1池级统计量) 在单票回调架构下的实现:
    模块级惰性聚合缓存 _ensure_pool_daily(pool_target) — 首次调用拉全市场日线
    (hub.all_codes + hub.daily 逐票200根, as_of=pool_target 截断防前视), 逐票逐日算
    G1特征 → 按板块分池日度聚合 (n/rmed/dmed/smed) → 滚动20日分位 (不含当日,
    min_hist=5) → {board: {date: {rmed, score_r}}}。key=pool_target 跨日自动失效;
    聚合失败 → 当日不产信号 (宁缺勿滥, 与"09月自动空仓"精神一致)。
  - 回测=实盘同链路: scan_signals 只判末根bar (D-1); 聚合锚 pool_target 取**切片前**
    bars 末根日期 (实盘=扫描日, 回测=快照末日), 查表 date=切片后末根日期 (D-1) —
    同一 date 的池统计只由 ≤该日 数据算出 (逐日特征因果), 回测=实盘同 key 同值。
  - 特征公式与 tmp/migrate_150.py 逐字一致 (_sma_np/_rsi/_atr/_boll_np 本地移植 —
    indicators.rsi 是 Wilder EMA 口径, 与研究成果不一致, 勿替换; MACD 复用
    indicators.calc_macd 与研究同源)。
  - 逐日/批量路径统一走 `_iter_gate_days` (2026-09-23 性能改动): f 与 mask 对同一
    (bars, board) 恒定, 抽出为生成器后一次 O(n) 预计算 + 逐日 O(1) 查表; 原实现虽已
    预计算 f, 仍每历史日重算整条 _g1_mask (占单次 gating 95%) ⇒ 回测全历史 **5.3x**
    (6.69→1.26 ms/票, 全市场 244 天 35.0s→6.6s; tmp/_g56_speed.py)。等价性由
    tmp/_g56_diff.py 证实: 20440 项逐字段 0 不一致。

易错点:
  - **全部 G1 指标因果** (实证: f_full[k] 逐位 == _g1_arrays(bars[:k+1])[k],
    29360 点 0 不一致, tmp/_g56_cost.py P1) ⇒ 可用**未切片**全序列一次算出第 k 日
    特征, 与"按 as_of 切片后重算"完全相同 ⇒ scan_signals 已不再真的切片。
    ⚠️ 若将来引入非因果/双向平滑指标 (如 centered MA / 用未来值回填), 此前提失效,
    必须回退切片, 否则会引入前视。
  - `_g56_gate` 的 mask 参数应传预算值: 不传则内部重算整条 mask (95% 开销)。目前仅
    单次调用的 scan_signals 路径允许省略; 一切循环/批量路径必须走 _iter_gate_days。
  - 前视防护: 聚合每票 bars 必须 as_of=pool_target 截断; 池统计按特征日(D-1)为 key,
    score_r 滚动分位不含当日 (同 tmp/regime3_150._pctl_roll)。
  - 与研究的已知口径差 (仅边缘日微差): 研究池统计来自 g1deep3 缓存 — 行级隐含
    D0-gap 过滤与引擎完成度条件 (D0 信息回流进池); 生产池=纯 G1 成员 (零前视,
    更严格), 验收按"量级一致"而非逐笔一致。
  - 出场 = 无追踪纯 7d/-8% (2026-09-17 出场研究定稿: trail 保留系数 0.85~0.97 全区间
    单调, 0.75~0.85 平台化≈无追踪 → 纯 7d/-8% 是结构终点非阈值拟合; 月度全向改善,
    仅主板 06 月 -0.48pp)。day_close 重放 = _exit_no_trail 同式: 止损线 low≤
    entry*0.92 按 min(open, 止损线) 成交, d≥7 到期收盘。**勿改回调 _run_backtest**
    — 其含追踪逻辑且 exit_day 是"兜底残影"与"触发"的混合值, 截断重放下无法区分。
    一字跌停日误标出场无实害: 框架 exit 次日开盘执行, 天然等价于"跌停顺延次日开盘强平"。
  - live 模式只做 -8% 硬止损兜底 (框架 stop_price 守卫已覆盖, 此处防 stop_price
    缺失); D0 当日跌破止损由框架标记 → 次日开盘执行 (回测引擎 T+1 忽略 D0 破位,
    live 更保守, 全框架策略同此口径)。
  - 阈值 (R56/ATR_Q5/RMED/SCORE/PCTB) 全部为150天窗口样本内拟合, 08月样本占比
    偏高 (20cm 69%) — 纸面跟踪期持续看月度结构, 见 MEMORY.md 行62-63。
"""
from __future__ import annotations

import threading

import numpy as np

from app.market_cn.auto.core.indicators import calc_macd
from app.market_cn.auto.core.market import get_board_type
from app.market_cn.auto.strategies import register
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

STRATEGY_KEY = "g56"
STRATEGY_LABEL = "五重共振"   # 2026-09-17 用户命名 (原"56%规则G+"); 5重硬条件:
# 开盘区间指引 (2026-09-22 回测结论, 仅展示/排序用, 不强制过滤信号集):
#   次日开盘跳空 gap ∈ [-3%,+3%] 最优 (甜区 [0%,+2%], 76%~100% 胜率/+8~+10.6% 均收);
#   回避 ≥+4% 高开 (尤其 [4%,5%] 样本全止损 -8%)。gap 在 D0 开盘才可知, 故此处为静态指引。
SIGNAL_OPEN_RANGE_HINT = " · 开[-3%,+3%]优先·回避≥+4%高开"
# |MA5-MA10|≤2.5% / rma_chg>0 / ATR>板块Q5 / 前20日大涨日≥2 / rhist_chg>板块Q5,
# regime 门为横截面环境门不占位; 与祖先策略"三金叉共振"成谱系。key=g56 不变。

# ================================================================
# 冻结参数 (150天研究定稿, 2026-09-17 用户拍板; 证据见 MEMORY.md 行55-63)
# ================================================================
HOLD_DAYS = 7          # 最长持有交易日 (含入场日, 出场模拟 d=1..7)
STOP_LOSS = -8.0       # 硬止损 % (框架 initial_stop 默认同值, 双保险)
R56 = {"main": 0.51, "gem_star": 0.66}        # rhist_chg 门 = 150天池内Q5
ATR_Q5 = {"main": 5.07, "gem_star": 6.42}     # G1池 ATR14% 下限 = 板块池内Q5
MAIN_RMED_MIN = 0.25     # 主板 regime 门: 池 rhist_chg 中位数 (raw)
MAIN_PCTB_MAX = 49.87    # 主板 boll %b 上限 = 56%池内 P40 (g1deep3 桶边界)
GEM_SCORE_MIN = 0.65     # 20cm regime 门: score_r (滚动相对热度)
ROLL = 20                # score_r 滚动分位窗口 (交易日)
MIN_HIST = 5             # 分位最少历史天数 (不足为 None → 20cm 不产信号)
# ================================================================
# 评分口径 (2026-09-24 换键: rhist_chg → dist_ma20 / dif0 组合)
# ----------------------------------------------------------------
# score 有两个消费者: ① 前端「评分」展示 ② **实盘每日限额截断键**
#   (scan.py:254 按 score 降序截断到 daily_limit)。② 是要害 —— 旧键 rhist_chg
#   在 1594 笔样本上实盘口径仅 **65.6%**, 低于随机截断×300 的 90% 区间下沿
#   **66.8%** (随机中位 68.4%) ⇒ 不只是"无判别力", 是系统性挑到较差的一批。
#
# 换键依据 (tmp/_g56_factor_ic.py · _g56_key_sim.py · _g56_key_robust.py):
#   · 主样本 1594笔/34信号日 **同日截面** Spearman IC: dist_ma20 +0.173 (t=3.30),
#     ret5 +0.178, pos20 +0.136, big20 +0.145 ; **rhist_chg +0.057 (t=1.00)**
#   · 早期段 87笔 (entry_date<2026-04-20, 与主样本不重叠) 分桶 Δ:
#     dist_ma20 +21.4pp / combo +16.8pp / **现用 score −1.6pp**
#   · ⚠ 陷阱: atr14/amp20/big20/dif0 **全样本 IC 很强但两时段反向** ⇒ 全样本 IC
#     会被"大日子效应"污染, 选因子必须看**同日截面 IC** (本轮踩过, 勿再犯)
#   · 实盘口径(每日Top30) 模拟: 现用 65.6%/+6.39% → dist_ma20 70.9%/+7.09%
#     → 本 combo 72.5%/+8.11% ; 参数 48 组网格全部落在 72.1~73.3% ⇒ 非阈值拟合
#
# 两段含义: dist_ma20 = 收盘相对 MA20 偏离% (位置/动量, 越大越强); −dif0 = MACD
#   柱深度 (越负=柱在零轴下越深=越接近金叉, 与 g56「④金叉预测」维度同向)。
#   二者均**个股级 D-1 已知**, 无需当日全市场截面 ⇒ 与池统计解耦, 单票可算。
SCORE_DIST_LO = -10.0    # dist_ma20 归一下界 (%)
SCORE_DIST_HI = 5.0      # dist_ma20 归一上界 (%)
SCORE_DIF0_LO = 0.0      # −dif0 归一下界 (对应 dif0 = 0)
SCORE_DIF0_HI = 16.0     # −dif0 归一上界 (对应 dif0 = −16)
SCORE_W_DIST = 0.5       # dist_ma20 权重 (dif0 占 1 − w)

DEFAULT_PARAMS = {}      # 阈值全部冻结为模块常量 (样本内拟合产物, 不开放 config 覆盖
                         # 以防误调 — 调参须走 tmp 研究链路重验)


# ================================================================
# 指标 (逐字移植 tmp/migrate_150.py 行49-73 + g1deep3_150._boll_np, 保证对账一致)
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
    """
    c = np.array([float(b["close"]) for b in bars])
    h = np.array([float(b["high"]) for b in bars])
    l = np.array([float(b["low"]) for b in bars])
    n = len(c)
    ma5, ma10, ma20 = _sma_np(c, 5), _sma_np(c, 10), _sma_np(c, 20)
    # 传 list[float] 而非 ndarray: calc_macd 是纯 Python 循环, 用 ndarray 会取到
    # np.float64 标量, 每次算术都要 numpy 标量装箱 (比 Python float 慢一个量级)。
    # 二者同为 IEEE-754 double 运算, 结果**逐位一致** (tmp/_macd_exact_test.py: 400 组
    # 随机序列 0 不一致; 提速 2.1×)。
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
    }


def _g1_mask(f, board):
    """G1池成员 mask (全D-1判定, 同 g1deep3 行127-131): NaN 比较为 False 自然暖机。"""
    m = ((np.abs(f["rma"]) <= 2.5) & (f["rma_chg"] > 0)
         & (f["atr"] > ATR_Q5[board]) & (f["big20"] >= 2))
    for key in ("rma", "rma_chg", "atr", "rhist_chg", "dif0", "pctb", "rsi", "ma20"):
        m &= np.isfinite(f[key])
    m[:68] = False       # 暖机下限 (同研究 s>=68 → 特征日 k>=67)
    return m


def _g56_gate(f, pool, board, k, date_k, mask=None):
    """五重共振门判定 (给定预计算特征 f@k / 池统计 pool / 板块 board / 信号日索引 k)。

    mask: 可选预计算的 `_g1_mask(f, board)` 结果。**同一 (f, board) 下 mask 恒定**,
      逐日循环里若不传入则每次重算整条序列 —— 实测占 _g56_gate 单次耗时 **95%**
      (0.0225 / 0.0238 ms, `tmp/_g56_hotspot.py`), 抹掉后单次降到 0.0013 ms (**18x**)。
      逐日/批量调用方务必预算一次并传入, 见 `_iter_gate_days`。

    返回 (bool_pass, st): st=该日横截面统计 (None=池缺失)。scan_signals 与
    backtest_stock 共用此单一判定事实源 — 修复 backtest 逐日重算 _g1_arrays 的 O(n^2)
    坑 (原 backtest 每历史日调 scan_signals 重算全序列指标); 改规则务必同步此处。
    """
    mk = _g1_mask(f, board) if mask is None else mask
    if not mk[k] or not f["rhist_chg"][k] > R56[board]:
        return False, None
    st = pool.get(board, {}).get(date_k)
    if st is None:
        return False, None                          # 池统计缺失 (聚合失败/暖机/空池)
    if board == "main":
        if not (st["rmed"] > MAIN_RMED_MIN and f["pctb"][k] <= MAIN_PCTB_MAX):
            return False, None
    else:
        if not (st["score_r"] is not None and st["score_r"] > GEM_SCORE_MIN
                and f["dif0"][k] <= 0):
            return False, None
    return True, st


def _iter_gate_days(bars, board, pool, lo_k, hi_k):
    """单票逐日列出通过五重共振门的 (k, f, st) —— 共用事实源, 一次预计算 + 逐日 O(1)。

    从 backtest_stock 抽出: `f` 与 `mask` 对同一 (bars, board) 恒定, 但原实现在每个
    历史日都重算 `_g1_mask` 整条序列 (占 _g56_gate 95%)。此处一次算完, 逐日只做标量
    索引与标量比较 ⇒ 单次 gating 0.0238 → 0.0013 ms (18x, `tmp/_g56_hotspot.py`)。

    前视安全: 所有 G1 指标**因果**(实证: 29360 点逐位比对, `f_full[k]` ==
    `_g1_arrays(bars[:k+1])[k]`, 0 不一致, `tmp/_g56_cost.py` P1) ⇒ 可以用未切片
    的全序列一次算出 k 日特征, 与逐日截断重算结果完全相同。

    索引: lo_k..hi_k **含端点**, 越界自动裁剪; 与 `_g1_mask` 的 `m[:68]=False`
    一致, k<68 恒不通过, 无需调用方过滤。
    """
    n = len(bars)
    lo = max(0, lo_k)
    hi = min(n - 1, hi_k)
    if hi < lo:
        return
    f = _g1_arrays(bars)
    mask = _g1_mask(f, board)
    for k in range(lo, hi + 1):
        if not mask[k]:
            continue
        ok, st = _g56_gate(f, pool, board, k, str(bars[k]["time"])[:10], mask=mask)
        if ok:
            yield k, f, st


# ================================================================
# 横截面 regime 门: 惰性聚合缓存 (单票回调架构下的池级统计量解法)
# ================================================================

_POOL_LOCK = threading.Lock()
_POOL = {"target": None, "main": {}, "gem_star": {}}


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
    """返回 {board: {date: {rmed, score_r}}} (key=pool_target 跨日失效; 失败不缓存)。

    前视防护: 每票日线截断到 ≤pool_target — 实盘扫描日=快照末日; 回测时
    pool_target=快照末日(今日), 历史 date 的统计仅由 ≤该日 数据构成。

    bars_batch: 可选 `{code: bars}`。由调用方**批量预加载**(窗口/复权/截断必须与
      `hub.daily(code, 200, as_of=pool_target)` 完全一致, 见 data.kline.fetch_klines_batch),
      用于消除逐票 `hub.daily` 的 O(N) 往返 —— 横截面池是展示管线夜间的主要超支源。
      缺省 None → 保持原逐票路径 (单票调用方/实盘扫描零改动)。
    """
    with _POOL_LOCK:
        if _POOL["target"] == pool_target:
            return _POOL
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
            # 不缓存失败结果: 下次调用重试; 本次返回空表 → 当日不产信号 (宁缺勿滥)
            logger.error("[g56] 池聚合失败, 当日横截面门不可用: %s", e)
            return {"target": pool_target, "main": {}, "gem_star": {}}
        logger.info("[g56] 池聚合完成 target=%s 主板%d日/20cm%d日",
                    pool_target, len(main_map), len(gem_map))
        _POOL.update({"target": pool_target, "main": main_map, "gem_star": gem_map})
        return _POOL


def _exit_no_trail(bars, s, entry, hold_days=None, stop_loss=None):
    """无追踪出场模拟 (2026-09-17 出场研究定稿): 出场 = min(止损, hold_days 到期收盘)。

    hold_days / stop_loss (2026-09-23): 出场阈值改由调用方注入 (源 = g56.yaml 声明);
    传 None -> 回落模块常量 HOLD_DAYS / STOP_LOSS, 即不传参时逐笔等价不破。

    返回与 v1._run_backtest 同构 {'exit_day','exit_price','return_pct','peak_return_pct'}:
      - d=1 入场日 T+1 不可卖, 峰值自入场日 high 起累计 (统计口径, 不影响成交);
      - d≥2 止损触发日 low≤止损线 按 min(open, 止损线) 成交 (跳空穿越按开盘);
      - peak_return_pct = 截至出场日的峰值 high 收益 (统计用)。
    依据: trail 保留系数 0.85~0.97 全区间单调 → 纯 7d/-8% 最优, 月度全向改善
    (主板 +1.45→+7.64 / 20cm +2.12→+10.22)。数据不足返回 None (调用方跳过该笔)。
    """
    n = len(bars)
    _hold = HOLD_DAYS if hold_days is None else int(hold_days)
    _stop = STOP_LOSS if stop_loss is None else float(stop_loss)
    stop_line = entry * (1 + _stop / 100)
    peak = float(bars[s]["high"])
    for d in range(2, _hold + 1):
        i = s + d - 1
        if i >= n:
            return None
        peak = max(peak, float(bars[i]["high"]))
        if float(bars[i]["low"]) <= stop_line:
            fill = min(float(bars[i]["open"]), stop_line)
            return {"exit_day": d, "exit_price": round(fill, 3),
                    "return_pct": round((fill / entry - 1) * 100, 2),
                    "peak_return_pct": round((peak / entry - 1) * 100, 2)}
    i = s + _hold - 1
    if i >= n:
        return None
    px = float(bars[i]["close"])
    return {"exit_day": _hold, "exit_price": round(px, 3),
            "return_pct": round((px / entry - 1) * 100, 2),
            "peak_return_pct": round((peak / entry - 1) * 100, 2)}


def _score_of(dist_ma20, dif0):
    """信号评分 0~100 —— 换键后唯一构造点 (口径见 SCORE_* 常量处注释)。

    = W·归一(dist_ma20) + (1−W)·归一(−dif0), 两段各 clip [0,1] 后线性加权 ×100 取整。

    NaN 防御: ma20/dif0 在暖机段为 NaN, 比较 `> 0` 为 False ⇒ 落 0.0 (最低分),
    不会污染排序。正常路径下暖机 `m[:68]=False` 已保证 k≥68, 不会走到。
    """
    s1 = (dist_ma20 - SCORE_DIST_LO) / (SCORE_DIST_HI - SCORE_DIST_LO)
    s2 = ((-dif0) - SCORE_DIF0_LO) / (SCORE_DIF0_HI - SCORE_DIF0_LO)
    s1 = 0.0 if not (s1 > 0) else (1.0 if s1 > 1 else s1)
    s2 = 0.0 if not (s2 > 0) else (1.0 if s2 > 1 else s2)
    v = 100 * (SCORE_W_DIST * s1 + (1 - SCORE_W_DIST) * s2)
    return int(min(100, max(0, round(v))))


def _mk_signal(code, bars, k, f, st):
    """命中日 k → Signal (单日 scan_signals 与批量 scan_days 共用的唯一构造点)。

    抽出的理由: 两处若各写一份, 字段口径迟早分叉 (score 取整/pctb 位数/score_r None
    处理都是易错点)。改任一字段必须只改这里。
    """
    rhc = float(f["rhist_chg"][k])
    dist = (float(bars[k]["close"]) / float(f["ma20"][k]) - 1) * 100
    return Signal(
        code=code,
        time=bars[k]["time"],
        score=_score_of(dist, float(f["dif0"][k])),
        price=float(bars[k]["close"]),
        label=STRATEGY_LABEL + SIGNAL_OPEN_RANGE_HINT,
        extra={
            # 不写 "board" 键: store.signal_row 回退 get_board_name (中文板块名,
            # 与全表落库口径一致); 板块类型由 code 前缀可逆推导
            "rhist_chg": round(rhc, 3),
            "boll_pctb": round(float(f["pctb"][k]), 2),
            "dif0": round(float(f["dif0"][k]), 3),
            "dist_ma20": round(dist, 2),
            "rmed": round(st["rmed"], 3),
            "score_r": None if st["score_r"] is None else round(st["score_r"], 3),
            "buy_mode": "next_open",
        },
    )


# ================================================================
# 策略插件
# ================================================================

@register
class G56Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    entry_style = "g56"
    family = "g56"                     # 自成一族, 不与 triple_resonance 链去重
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DEFAULT_PARAMS)
    use_unified_prefilter = False      # 与 tmp 回测口径一致 (无 U1~U4)

    # ---- 信号判定: 只判末根bar (D-1); as_of=k 切片用于回测逐日枚举 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        if not bars or len(bars) < 68:
            return []
        if code.startswith(("8", "4", "92")):
            return []
        # 聚合锚=切片前末根 (回测=快照末日); as_of 只决定取哪一根, 不再真的切片
        pool_target = str(bars[-1]["time"])[:10]
        k = len(bars) - 1 if as_of is None else as_of
        # 暖机/越界: 原实现对小 as_of 会因 np.convolve 广播失败而崩溃 (切片不足 20 根),
        # 现按 _g1_mask 的 m[:68]=False 口径静默返回空 —— 该区间本就不可能出信号
        if not (67 <= k < len(bars)):
            return []
        board = get_board_type(code)
        # 全序列一次算 f, 不再 _g1_arrays(bars[:k+1]): 指标全部因果, f_full[k] 逐位
        # == 截断重算的第 k 个值 (实证 29360 点 0 不一致, tmp/_g56_cost.py P1)
        f = _g1_arrays(bars)
        date_k = str(bars[k]["time"])[:10]
        ok, st = _g56_gate(f, _ensure_pool_daily(pool_target), board, k, date_k)
        if not ok:
            return []                              # G1池 & 56%门 & 横截面 regime 门
        return [_mk_signal(code, bars, k, f, st)]

    # ---- 横截面预热 (声明制; 编排层调 prewarm 一次, 不硬编码策略 key) ----
    def prewarm(self, bars_map, hi_date):
        """一次建好横截面池 (锚=hi_date), 供本批所有票的 scan_days 复用。

        不预热的后果: `_POOL` 是单槽缓存, 若每票各自调用 `_ensure_pool_daily`, 只要
        target 相同仍会命中 —— 但**停牌票末根早于全市场末日**会让锚跳变 ⇒ 反复重建
        全市场池。编排层统一预热 + `scan_days` 锚取 hi_date ⇒ 全批只建一次。
        """
        _ensure_pool_daily(hi_date, bars_batch=bars_map)

    # ---- 覆盖基类 scan_days (批量契约): 与逐日调 scan_signals 等价, 但 O(n) 而非 O(n²) ----
    def scan_days(self, bars, code, *, lo_date=None, hi_date=None, pool=None, **params):
        """返回该票在 [lo_date, hi_date] 内**所有**命中日的 Signal (不只末根)。

        为什么必须覆盖基类的默认实现: 默认实现逐日调 scan_signals, 而单次调用就是一次
        O(n) `_g1_arrays` ⇒ 逐日枚举 O(n²); 更致命的是 scan_signals 内部按 `bars[-1]`
        取池锚, 逐日截断会让锚每天都变 ⇒ `_POOL` 单槽缓存**每票每天都重建全市场池**
        (5235 票 × 114s)。本方法一次 f+mask 预计算 + 池锚固定 ⇒ 逐日 O(1)。

        等价实证 (`tmp/_g56_pool_anchor.py`, 500 票分层抽样):
          - 池锚: 一次建池(锚=末日) 的第 i 天 vs 逐日建池(锚=i) 的第 i 天 →
            **232 个 (板,日) 点 |Δrmed|=0 |Δscore_r|=0, 阈值翻转 0**
            (`_pctl_roll` 零前视 + EMA/ATR/RSI 因果 ⇒ 池可一次建好逐日查)
          - 端到端: 逐日切片 scan_signals vs 本路径 → **103 个 (code,date) 命中逐位一致, 96.4x**

        ⚠️ 池锚取 hi_date 而非 bars[-1]: 同一批票必须锚在同一天, 停牌票的末根早于全市场
        末日时会让锚跳变 ⇒ 单槽缓存 thrash。调用方显式传 pool 可完全跳过这一步。
        """
        if not bars or len(bars) < 68:
            return []
        if code.startswith(("8", "4", "92")):
            return []
        board = get_board_type(code)
        if board not in ("main", "gem_star"):
            return []
        if pool is None:
            pool = _ensure_pool_daily(hi_date or str(bars[-1]["time"])[:10])
        out = []
        for k, f, st in _iter_gate_days(bars, board, pool, 0, len(bars) - 1):
            d = str(bars[k]["time"])[:10]
            if lo_date and d < lo_date:
                continue
            if hi_date and d > hi_date:
                continue
            out.append(_mk_signal(code, bars, k, f, st))
        return out

    # ---- D0 竞价处置 (monitor ~09:25): gap≥涨停幅度 → 不可买 (同回测 gap 过滤) ----
    def entry_decision(self, row, snap=None, **params):
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        lim = 0.198 if get_board_type(row.get("code", "")) == "gem_star" else 0.098
        gap = open_px / prev_close - 1
        if gap >= lim:
            return EntryDecision(False, f"gap={gap * 100:.2f}%≥涨停幅度, 一字/触板不可买")
        return EntryDecision(True, f"gap={gap * 100:.2f}% 可买")

    # ---- 15:00 收盘确认: v1 引擎无 D1 确认逻辑, 恒持有 ----
    def confirm_decision(self, row, snap=None, **params):
        d1_chg = None
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        entry = float(row.get("entry_price") or 0)
        if series and entry > 0:
            last_px = float(series[-1].get("last") or 0)
            if last_px > 0:
                d1_chg = round((last_px / entry - 1) * 100, 2)
        return ConfirmDecision(True, "g56_hold", d1_chg=d1_chg,
                               detail={"confirm": "always"})

    def quality_key(self, row):
        """开盘窗口质量排序键 —— **与入库截断键同源于 Signal.score**。

        2026-09-24 修正: 旧实现用 `extra.rhist_chg`, 那正是当日换键前的**旧键**
        (实盘口径仅 65.6%, 低于随机截断×300 的 90% 区间下沿 66.8% ⇒ 有害非无效)。
        当日 `Signal.score` 已换成 dist_ma20/dif0 组合(见文件头), 但本方法**未同步** ⇒
        出现「`scan.py:254` 用新键截断入库、`monitor.py:245` 用旧键分配开盘名额」的
        **两套口径并存**。现统一回落到 `row["score"]`, 两环节完全一致。

        row 是 signals 行(dict), `score` 由 `_score_of` 产出并落库; `or 0` 兜底历史空值。
        """
        return (row.get("score") or 0,)

    # ---- 出场判定 (day_close 重放 = _exit_no_trail 同式, 见文件头"易错点") ----
    def exit_decision(self, row, snap=None, **params):
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        entry_price = float(row.get("entry_price") or 0)
        if entry_price <= 0:
            return ExitDecision("hold")
        mode = snap.get("mode")
        if mode == "live":
            # -8% 硬止损兜底 (框架 stop_price 守卫为主; 此处防其缺失)
            series = snap.get("series") or []
            if series:
                last = float(series[-1].get("last") or 0)
                if 0 < last <= entry_price * (1 + STOP_LOSS / 100):
                    return ExitDecision("exit", reason=f"硬止损{STOP_LOSS}%",
                                        price=last)
            return ExitDecision("hold")
        if mode != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        if not bars or entry_idx is None or entry_idx >= len(bars):
            return ExitDecision("hold")
        today_idx = len(bars) - 1
        d = today_idx - entry_idx + 1          # 持仓日序号 (d=1=入场日, 引擎同口径)
        if d <= 1:
            return ExitDecision("hold")        # T+1: 入场日不可卖
        b = bars[today_idx]
        stop_line = entry_price * (1 + STOP_LOSS / 100)
        if float(b["low"]) <= stop_line:
            fill = min(float(b["open"]), stop_line)   # 跳空穿越按开盘 (模拟同式)
            return ExitDecision("exit", reason=f"止损{STOP_LOSS:g}%",
                                price=round(fill, 3))
        if d >= HOLD_DAYS:
            return ExitDecision("exit", reason=f"到期{HOLD_DAYS}天",
                                price=round(float(b["close"]), 3))
        return ExitDecision("hold")

    # ---- 回测钩子 (信号判定走 _g56_gate 统一路径, 指标一次预计算; 出场 _exit_no_trail 无追踪 2026-09-17) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=False,
                       probe=None):
        if code.startswith(("8", "4", "92")) or len(bars) < 68:
            return []
        board = get_board_type(code)
        lim = 0.098 if board == "main" else 0.198
        n = len(bars)
        o = np.array([float(b["open"]) for b in bars])
        c = np.array([float(b["close"]) for b in bars])
        # 横截面 regime 池: 锚=快照末日, 一次聚合后按 target 跨股/逐日缓存复用
        pool = _ensure_pool_daily(str(bars[-1]["time"])[:10])
        trades = []
        last_exit_idx = -1   # 修复② 持仓窗口去重: 上一笔未退出前不重复入场
                            # (同共振段连续成信号时锁仓至退出日, 防 000070 连日重复建仓)
        # 修复① O(n^2): 全序列指标一次 O(n) 预计算, 不再逐日 scan_signals 重算 _g1_arrays
        # 修复③: 顺带消除逐日重算 _g1_mask (单次 gating 的 95%, 见 _iter_gate_days)
        # 索引换算: 原 `for s in range(68, n - 9)` ⇒ s∈[68, n-10] ⇒ k=s-1∈[67, n-11]
        for k, f, st in _iter_gate_days(bars, board, pool, 67, n - 11):
            s = k + 1                          # 入场日 D0 = 信号日 D-1 的下一交易日
            if s <= last_exit_idx or o[s] <= 0:
                continue
            gap = o[s] / c[s - 1] - 1
            if gap >= lim:
                continue                        # D0 开盘不可买 (一字/触板)
            r = _exit_no_trail(bars, s, float(o[s]))
            if not r:
                continue
            rhc = float(f["rhist_chg"][k])
            trades.append({
                "code": code,
                "board": board,
                "strategy": STRATEGY_KEY,
                "signal_date": str(bars[k]["time"])[:10],
                "entry_date": str(bars[s]["time"])[:10],
                "entry_price": round(float(o[s]), 3),
                "entry_gap": round(gap * 100, 2),
                "exit_date": str(bars[s + r["exit_day"] - 1]["time"])[:10]
                if 0 < r["exit_day"] and s + r["exit_day"] - 1 < n else None,
                "exit_price": r["exit_price"],
                "exit_day": r["exit_day"],
                "return_pct": r["return_pct"],
                "peak_return_pct": r["peak_return_pct"],
                "rhist_chg": round(rhc, 3),
                "boll_pctb": round(float(f["pctb"][k]), 2),
                "rmed": round(st["rmed"], 3),
                "score_r": None if st["score_r"] is None else round(st["score_r"], 3),
                "buy_mode": "next_open",
            })
            last_exit_idx = s + r["exit_day"] - 1   # 锁仓至退出日 (含), 期间不重复入场
        return trades


# ================================================================
# 以下门表 DSL 私有函数由 strategies 重构从 strategy_funcs 迁入（逐字等价）
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

register_strategy_funcs(
    'g56',
    {"feat": g56_feat, "finite": g56_finite, "warmup": g56_warmup, "pool_stat": g56_pool_stat, "pool_field": g56_pool_field, "board_is_main": board_is_main, "board_is_gem": board_is_gem},
    d0={"feat": 0, "finite": 0, "warmup": 0, "pool_stat": 0, "pool_field": 0, "board_is_main": 0, "board_is_gem": 0},
)
