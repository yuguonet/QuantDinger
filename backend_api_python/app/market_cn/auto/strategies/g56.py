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

from app.utils.indicators import calc_macd
from app.market_cn.auto.core.market import default_market, get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
    data_end_close,
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
# 规则门 (2026-09-25): 信号日收盘相对 MA20 下限 %。与 score **无关** ——
# score 只做排序/展示 (随日变动); 本阈值是硬规则。300d 对照 (n=1191 基线):
#   ma20>=-4 → n=748 / 胜率80.7%(+1.4pp) / 均收8.52(基本持平) / 盈亏比1.83
#   背景: 近期 600613/002437/002412 反抽失败票 dist_ma20 多在 -3.4~-6.8。
#   trail/峰值逃顶经对照 **否决** (均收 8.69→2.5, 大肉被砍; 见 tmp/g56_round1.json)。
DIST_MA20_MIN = -4.0
# D-1 涨停子集紧止损 (2026-09-25): 归因确认信号日涨停呈两极 (大肉/大血), 46 笔中
# 14 笔≤-5% / 13 笔≥+10%。对该子集用 -5% 止损, 其余仍 -8%。
# 320d 对照 (含 DIST_MA20_MIN): 深亏≤-7.5% 103→91, 胜率 80.7→80.2, 均收持平。
# ⚠️ 不是改全体止损 (全体 -5% 胜率掉到 75%); 只收 D-1 涨停尾部。
# 依据: docs/analysis_output/g56_涨停与龙虎榜事件归因_20260924.md §A5
STOP_LOSS_LU = -5.0
R56 = {"main": 0.51, "gem_star": 0.66}        # rhist_chg 门 = 150天池内Q5
# ATR_Q5 / ROLL / MIN_HIST 2026-09-26 下沉 core/features/cross_section.py (层清零)
from app.market_cn.auto.core.features.cross_section import (  # noqa: E402  (L73 下方, 常量区之后)
    ATR_Q5, ROLL, MIN_HIST, _aggregate, _g1_arrays, _g1_mask, _ensure_pool_daily,
    g1_state_features, g1_state_init, g1_state_step,
)
from app.market_cn.auto.core.present.contract import (   # 展示层折叠契约
    InsufficientHistory, Progress, Stage,
)
MAIN_RMED_MIN = 0.25     # 主板 regime 门: 池 rhist_chg 中位数 (raw)
MAIN_PCTB_MAX = 49.87    # 主板 boll %b 上限 = 56%池内 P40 (g1deep3 桶边界)
GEM_SCORE_MIN = 0.65     # 20cm regime 门: score_r (滚动相对热度)
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

DEFAULT_PARAMS = {
    "dist_ma20_min": -4.0,   # 规则门: 信号日收盘相对 MA20 下限 % (2026-09-26 迁入; 原硬编码 DIST_MA20_MIN=-4.0; 300d 对照 n=748 胜率80.7%)
}

G56_WIN = 35        # 递推切片窗口 (= 生产口径 DEFAULT_WIN)
GAP_LIM = {"main": 0.098, "gem_star": 0.198}   # entry 过滤硬编码口径 (非 up_eff)
   # 2026-10-06: 原散落在 entry_decision / backtest 两处硬编码,
   # 展示层 evaluate 亦抄了一份 ⇒ 收敛为单一常量。
# score 相关阈值 (SCORE_* / R56 / MAIN_RMED_MIN / GEM_SCORE_MIN 等) 仍冻结为模块常量 ——
# 样本内拟合产物, 不开放 config 覆盖以防误调 (调参须走 tmp 研究链路重验)。


# ================================================================
# 指标 / 单票 G1 特征 / 横截面聚合 —— 2026-09-26 整体下沉 core/features/cross_section.py
#   _g1_arrays / _g1_mask / _ensure_pool_daily 均已从上导入, 此处不再重复定义
# ================================================================

def _g56_gate(f, pool, board, k, date_k, mask=None, p=None, age=None):
    """五重共振门判定 (给定预计算特征 f@k / 池统计 pool / 板块 board / 信号日索引 k)。

    mask: 可选预计算的 `_g1_mask(f, board)` 结果。**同一 (f, board) 下 mask 恒定**,
      逐日循环里若不传入则每次重算整条序列 —— 实测占 _g56_gate 单次耗时 **95%**
      (0.0225 / 0.0238 ms, `tmp/_g56_hotspot.py`), 抹掉后单次降到 0.0013 ms (**18x**)。
      逐日/批量调用方务必预算一次并传入, 见 `_iter_gate_days`。

    p: 可选 params() dict, 2026-09-26 迁入 dist_ma20_min 规则门, 其他 score
      阈值仍冻结常量; p=None 或缺键时 fallback 到 DEFAULT_PARAMS["dist_ma20_min"]。

    返回 (bool_pass, st): st=该日横截面统计 (None=池缺失)。scan_signals 与
    backtest_stock 共用此单一判定事实源 — 修复 backtest 逐日重算 _g1_arrays 的 O(n^2)
    坑 (原 backtest 每历史日调 scan_signals 重算全序列指标); 改规则务必同步此处。
    """
    # age: **逻辑数据年龄** (递推/播种路径必传, 见 _g1_mask 文档)。
    #   全量路径 f 是整条序列 ⇒ age=None (cut=G1_WARMUP) 与旧行为逐位一致;
    #   递推路径 f 只有窗口 ⇒ 必须传真实 age, 否则暖机会误杀/误放。
    #   k=-1 是递推路径的"末位"用法 (与全量 k=末日索引 同义)。
    mk = _g1_mask(f, board, age=age) if mask is None else mask
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
    # 规则门: 信号日不得深跌于 MA20 (2026-09-25; dist_ma20 是特征阈值, 非 score)
    # 2026-09-26 迁入 config: 优先从 p 读, fallback 模块常量 (兼容旧调用方)
    d20 = f.get("dist_ma20")
    lo = (p or {}).get("dist_ma20_min", DIST_MA20_MIN)
    if d20 is not None and k < len(d20):
        dv = float(d20[k])
        if not (dv == dv) or dv < lo:          # nan → 不放行
            return False, None
    return True, st


def _iter_gate_days(bars, board, pool, lo_k, hi_k, p=None):
    """单票逐日列出通过五重共振门的 (k, f, st) —— 共用事实源, 一次预计算 + 逐日 O(1)。

    2026-09-26 新增 p 参数透传 dist_ma20_min 规则门阈值 (见 _g56_gate 签名)。
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
        ok, st = _g56_gate(f, pool, board, k, str(bars[k]["time"])[:10], mask=mask, p=p)
        if ok:
            yield k, f, st


# ================================================================
# 横截面 regime 门: 2026-09-26 下沉 core/features/cross_section.py
#   _ensure_pool_daily 由 cross_section 模块提供, 本文件顶部已导入
# ================================================================


def _exit_no_trail(bars, s, entry, hold_days=None, stop_loss=None, code=None):
    """无追踪出场模拟 — 骨架上收 core.exit_engines.run_hold_stop。

    hold_days / stop_loss: 由调用方注入 (g56.yaml); 传 None 回落模块常量。
    code: 可选, 用于判断信号日是否涨停 (D-1 板) —— 该子集用 STOP_LOSS_LU 紧止损,
        降低 -8% 深亏触发 (2026-09-25)。缺 code 时行为与历史逐笔一致。
    返回 {'exit_day','exit_price','return_pct','peak_return_pct'}。
    """
    _hold = HOLD_DAYS if hold_days is None else int(hold_days)
    _stop = STOP_LOSS if stop_loss is None else float(stop_loss)
    if code and s >= 1:
        try:
            from app.market_cn.auto.core.market import get_board_type, is_limit_up
            bt = get_board_type(code)
            if is_limit_up(float(bars[s - 1]["close"]), float(bars[s - 2]["close"]), bt):
                _stop = max(_stop, STOP_LOSS_LU)  # -5 大于 -8 → 更紧
        except Exception:
            pass
    from app.market_cn.auto.core.exit_engines import run_hold_stop
    # with_reason=True: day_flow/trade 需带 exit_reason (对齐 replay canonical, 草案①等价)
    return run_hold_stop(bars, s, entry, hold_days=_hold, stop_loss=_stop, with_reason=True)


def _score_of(dist_ma20, dif0):
    """**预测分** 0~100 (2026-09-26 用户定名) — 唯一构造点 (口径见 SCORE_* 常量)。

    = W·归一(dist_ma20) + (1−W)·归一(−dif0), 两段各 clip [0,1] 后线性加权 ×100 取整。

    语义 (勿混用):
      - 只对 **下一交易日 T+1** 负责 (T 日收盘后算出的分 → 预期 T+1);
      - 持仓期内 **每日更新**, 形成「D0分→D1 / D5分→D6」的心里预期链;
      - **出场后不再适用**本套评分; **出池无效** (全市场任意日 ≈ 抛硬币);
      - 用途 = 排序 / 展示 / 次日胜率校准, **不是**入场规则门。

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

# 2026-10-06: 单继承 StrategyBase —— 折叠契约已并入生产基类，参数合并口径唯一 = params()。
# 折叠契约已并入生产 StrategyBase（单继承）。
class PoolLedger:
    """策略级台账：每 (board, date) 横截面四元组 {date, n, rmed, dmed, smed}。

    score_r 的滚动分位只回看 20 日 ⇒ 台账保留最近 30 条即够（聚合用常数数组
    重建桶，与原始桶逐位等价 —— `_aggregate` 单一实现）。

    ⚠ **不自己落盘**（2026-10-06）：展示层落盘出口唯一 = StateStore（每策略一个
    文件、每轮一次）。共享状态经 `init_shared` / `shared_snapshot` 契约随**本策略
    的切片文件**一起落盘。旧实现自带 `path` 且每次 append 就写一次盘，而默认
    `path=None` 又**从不落盘** ⇒ 池台账与每票 state 生命周期不一致：重启后池
    静默丢失、分位照算不报错 —— 最难发现的一类错误。
    """

    def __init__(self, quads: dict | None = None, keep: int = 30):
        self.keep = keep
        self.quads: dict[str, list] = {
            "main": list((quads or {}).get("main") or []),
            "gem_star": list((quads or {}).get("gem_star") or []),
        }

    def append(self, board: str, quad: dict) -> None:
        qs = self.quads.setdefault(board, [])
        if qs and qs[-1]["date"] >= quad["date"]:
            qs[-1] = quad                       # 同日重跑幂等覆盖
        else:
            qs.append(quad)
        del qs[:-self.keep]

    def window(self, board: str) -> list:
        return self.quads.get(board) or []

    def snapshot(self) -> dict:
        return {b: list(q) for b, q in self.quads.items()}


@register
class G56Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    entry_style = "g56"
    family = "g56"                     # 自成一族, 不与 triple_resonance 链去重
    scan_spec = ScanSpec(kind="daily_close", after_events=("daily_1d", "lhb"))
    default_params = dict(DEFAULT_PARAMS)
    use_unified_prefilter = False      # 与 tmp 回测口径一致 (无 U1~U4)
    # 展示阶段表（展示层只按此表呈现，不认识门细节）
    stages = (
        Stage("ready", "五重共振·准备", realtime="09:25"),
        Stage("exec", "D0开盘买入"),
        Stage("exit", "出场结算"),
    )

    #: ⚠ 窗口硬下界 35: 低于此 `_g1_arrays` 直接 IndexError (dummy MACD 退化为 0 维数组)。
    #:   (原 `warmup` / `resume_points` 声明于 2026-10-06 P6 随展示层断点机制一并移除;
    #:    三档下界 35/40/240 的实测记录见 .workbuddy/memory)

    # ---- 信号判定: 只判末根bar (D-1); as_of=k 切片用于回测逐日枚举 ----
    # ══ 展示层折叠契约：递推状态机 + 池台账（门/信号构造委托生产唯一实现）══
    def __init__(self, pool_ledger=None):
        self.ledger = pool_ledger or PoolLedger()

    def init_shared(self, shared):
        """用持久化的策略级状态恢复台账（每轮开头调用，幂等）。"""
        if shared:
            self.ledger = PoolLedger(shared.get("quads"))

    def shared_snapshot(self):
        return {"quads": self.ledger.snapshot()}

    def init_state(self, code, bars):
        """seed: 全量历史 → G1 增量状态（MACD 锚 head + win 根 OHLC 窗口 + age）。"""
        if len(bars) < G56_WIN + 1:
            raise InsufficientHistory(f"{code}: bars={len(bars)} < {G56_WIN + 1}")
        return g1_state_init(bars, win=G56_WIN, keep_window=True)

    def step(self, state, bar):
        return g1_state_step(state, [bar])

    def probe(self, state):
        """除权探针: 窗口首尾 (date, close)。"""
        w = state["window"]
        return [(w[0][0], w[0][3]), (w[-1][0], w[-1][3])]

    def begin_day(self, date, states, bars):
        """跨票横截面池（展示层只透传 ctx["_day"]，不认识池内容）。"""
        buckets = {"main": {}, "gem_star": {}}
        for code, st0 in states.items():
            if code.startswith(("8", "4", "92")):
                continue
            board = get_board_type(code)
            if board not in buckets:
                continue
            st_day = self.step(st0, bars[code])
            f = g1_state_features(st_day)
            if _g1_mask(f, board, age=st_day["age"])[-1]:
                b = buckets[board].setdefault(date, [[], [], []])
                b[0].append(float(f["rhist_chg"][-1]))
                b[1].append(float(f["dif0"][-1]))
                b[2].append(float(f["rsi"][-1]))
        pool = {}
        for board in ("main", "gem_star"):
            bucket = buckets[board].get(date)
            if bucket:
                self.ledger.append(board, {
                    "date": date, "n": len(bucket[0]),
                    "rmed": float(np.median(bucket[0])),
                    "dmed": float(np.median(bucket[1])),
                    "smed": float(np.median(bucket[2])),
                })
            by_date = {q["date"]: [[q["rmed"]] * q["n"], [q["dmed"]] * q["n"],
                                   [q["smed"]] * q["n"]]
                       for q in self.ledger.window(board)}
            pool[board] = _aggregate(by_date)
        return {"pool": pool}

    def evaluate(self, state, inp, prev):
        """预处理/回测/实时共用：持仓出场 → D0 入场 → 新信号（五重共振）。"""
        p = self.params()
        bar, code = inp.bar, inp.code
        events = []
        holding = None

        # ── 持仓出场（d=2..7；T+1 不可卖）──
        if prev is not None and prev.stage == "exec" and prev.payload.get("buyable") \
                and bar.get("time", "") > prev.date:
            entry_price = float(prev.payload["entry_price"])
            entry_age = int(prev.payload["entry_age"])
            d = (state["age"] + 1) - entry_age + 1       # 持仓日（d=1 入场日）
            if d >= 2:
                stop_use = float(prev.payload["stop_use"])
                stop_line = entry_price * (1 + stop_use / 100)
                highs = [w[1] for w in state["window"][-(d - 1):]] if d > 1 else []
                peak = max(highs + [float(bar.get("high") or 0)])
                exit_ev = None
                if float(bar.get("low") or 0) <= stop_line:
                    fill = min(float(bar.get("open") or 0), stop_line)
                    exit_ev = self._exit_event(prev, bar, d, round(fill, 3),
                                               f"止损{stop_use:g}%", peak)
                elif d >= HOLD_DAYS:
                    exit_ev = self._exit_event(
                        prev, bar, d, round(float(bar.get("close") or 0), 3),
                        f"到期{HOLD_DAYS}天", peak)
                if exit_ev is not None:
                    events.append(exit_ev)
                else:
                    holding = prev
            else:
                holding = prev
        # ── D0 入场（gap 过滤 = 旧 entry_decision/backtest 同式）──
        elif prev is not None and prev.stage == "ready" \
                and bar.get("time", "") > prev.date:
            open_px = float(bar.get("open") or 0)
            pc = float(prev.payload.get("price") or 0)   # 信号日收盘
            gap = (open_px / pc - 1) if (open_px > 0 and pc > 0) else None
            buyable = gap is not None and gap < GAP_LIM[get_board_type(code)]
            ev = Progress(stage="exec", date=bar.get("time", ""), payload={
                "entry_date": bar.get("time", ""), "entry_price": open_px,
                "signal_date": prev.date, "gap": None if gap is None else round(gap * 100, 2),
                "buyable": buyable,
                "stop_use": STOP_LOSS_LU if self._lu_subset(state, code) else STOP_LOSS,
                "entry_age": state["age"] + 1,
                # P5-④ 前置 (2026-10-08): 买入当日 15:01 实时确认（持仓 / 当日出场）。
            }, next_realtime="15:01")
            events.append(ev)
            if buyable:
                holding = ev

        # ── 新信号（T 日盘后五重共振；持仓中不重复入场）──
        if holding is None and not code.startswith(("8", "4", "92")):
            st_day = self.step(state, bar)
            f = g1_state_features(st_day)
            board = get_board_type(code)
            day_pool = ((inp.ctx or {}).get("_day") or {}).get("pool") or {}
            ok, st = self._gate(f, st_day, board, day_pool, bar.get("time", ""), p)
            if (tr := (inp.ctx or {}).get("_trace")) is not None:   # 门原因通道（契约约定）
                tr.gate(ok, st, date=str(bar.get("time", ""))[:10])
            if ok:
                events.append(self._mk_ready(code, bar, f, st))
        return events

    def _gate(self, f, st_day, board, day_pool, date, p):
        """五重共振门 —— **委托 `_g56_gate`**（唯一实现）。

        递推路径：k=-1（窗口末位）+ 必须传真实 age（窗口短于真实历史）。
        """
        return _g56_gate(f, day_pool, board, -1, date, p=p, age=st_day["age"])

    def _mk_ready(self, code, bar, f, st):
        """ready 事件 —— **委托 `_mk_signal`**（唯一信号构造点, k=-1 取末位）。"""
        sig = _mk_signal(code, [bar], -1, f, st)
        return Progress(stage="ready", date=bar["time"], payload={
            "price": sig.price, "score": sig.score, "label": sig.label,
            "extra": sig.extra}, next_realtime="09:25")

    @staticmethod
    def _lu_subset(state, code):
        """D-1 涨停子集紧止损（信号日 T 收盘较 T-1 涨停）。"""
        closes = state["closes"]
        if len(closes) < 2 or closes[-2] <= 0:
            return False
        return is_limit_up(closes[-1], closes[-2], get_board_type(code))

    def _exit_event(self, prev, bar, d, price, reason, peak):
        entry = float(prev.payload["entry_price"])
        return Progress(stage="exit", date=bar.get("time", ""), payload={
            "entry_date": prev.payload["entry_date"], "entry_price": entry,
            "exit_date": bar.get("time", ""), "exit_price": price,
            "exit_day": d, "reason": reason,
            "return_pct": round((price / entry - 1) * 100, 2) if entry > 0 else None,
            "peak_return_pct": round((peak / entry - 1) * 100, 2) if entry > 0 else None,
        }, next_realtime=None)


    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        if not bars:
            return []
        if code.startswith(("8", "4", "92")):
            return []
        # 板块守卫 (2026-09-28 审计 P2): scan_days 版本有、这里缺 ⇒ 两条路径判据不等价,
        # 且非 A 市场会让 R56[board] 直接 KeyError (而非返回空)。
        board = get_board_type(code)
        if board not in ("main", "gem_star"):
            return []
        # 聚合锚=切片前末根 (回测=快照末日); as_of 只决定取哪一根, 不再真的切片
        pool_target = str(bars[-1]["time"])[:10]
        p = self.params(params)
        # 暖机/越界: 原实现对小 as_of 会因 np.convolve 广播失败而崩溃 (切片不足 20 根),
        # 现按 _g1_mask 的 m[:68]=False 口径静默返回空 —— 该区间本就不可能出信号
        if len(bars) < 68:
            return []
        k = len(bars) - 1 if as_of is None else as_of
        if not (67 <= k < len(bars)):
            return []
        # 全序列一次算 f, 不再 _g1_arrays(bars[:k+1]): 指标全部因果, f_full[k] 逐位
        # == 截断重算的第 k 个值 (实证 29360 点 0 不一致, tmp/_g56_cost.py P1)
        f = _g1_arrays(bars)
        date_k = str(bars[k]["time"])[:10]
        ok, st = _g56_gate(f, _ensure_pool_daily(pool_target), board, k, date_k, p=p)
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
        # (2026-10-06) 池一律由批量路径自建。曾优先读 `g1_pool_source` 的台账池历史
        #   (3.41s → 0.1s), 但该旁路需 g1_* 三件套 1531 行支撑, 已随 P6 清理删除。
        #   全量建池 3.4s 是**一次性**成本, 按 10-05 裁定「速度非第一诉求」接受。
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
        p = self.params(params)
        for k, f, st in _iter_gate_days(bars, board, pool, 0, len(bars) - 1, p=p):
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
        # D-1 涨停子集紧止损 (2026-09-25, STOP_LOSS_LU); 信号日 = entry_idx-1
        stop_use = STOP_LOSS
        try:
            ei = snap.get("entry_idx")
            bars_chk = snap.get("bars") or []
            if row.get("code") and isinstance(ei, int) and 2 <= ei < len(bars_chk):
                from app.market_cn.auto.core.market import get_board_type, is_limit_up
                if is_limit_up(float(bars_chk[ei - 1]["close"]),
                               float(bars_chk[ei - 2]["close"]),
                               get_board_type(row["code"])):
                    stop_use = max(STOP_LOSS, STOP_LOSS_LU)
        except Exception:
            pass
        if mode == "live":
            # 硬止损兜底 (框架 stop_price 守卫为主; 此处防其缺失)
            series = snap.get("series") or []
            if series:
                last = float(series[-1].get("last") or 0)
                if 0 < last <= entry_price * (1 + stop_use / 100):
                    return ExitDecision("exit", reason=f"硬止损{stop_use:g}%",
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
        stop_line = entry_price * (1 + stop_use / 100)
        if float(b["low"]) <= stop_line:
            fill = min(float(b["open"]), stop_line)   # 跳空穿越按开盘 (模拟同式)
            return ExitDecision("exit", reason=f"止损{stop_use:g}%",
                                price=round(fill, 3))
        if d >= HOLD_DAYS:
            return ExitDecision("exit", reason=f"到期{HOLD_DAYS}天",
                                price=round(float(b["close"]), 3))
        return ExitDecision("hold")

    # ---- 回测钩子 (信号判定走 _g56_gate 统一路径, 指标一次预计算; 出场 _exit_no_trail 无追踪 2026-09-17) ----


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
    """暖机下限：镜像 _g1_mask 的 m[:68]=False → 决策日索引 i 必须 >= 68。

    ★ `ctx.i_age` (2026-10-05): 增量/播种路径下 bars 只是**窗口** (可短至
    `G1_WIN_MIN`=21 根), 此时 `ctx.i` 是窗口内下标、恒 < 68 ⇒ 用它会让全市场
    **静默归零** (门判 False 却无报错)。真实数据年龄由 `ctx.i_age` 给出。
    ⚠ i_age 为 None (生产现状) → 退化为 ctx.i, 与历史逐位一致。
    """
    age = ctx.i_age if getattr(ctx, "i_age", None) is not None else ctx.i
    return 1 if age >= 68 else 0


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
# 「明日操作分」(v2, 2026-09-24 重标定) —— 展示用独立分, 不参与任何门/评分/截断
# ----------------------------------------------------------------
# 语义: 用 **T 日收盘后可知**的信息, 估算 **T+1 开盘买 → T+1 收盘卖** 的相对强弱。
#       50 = 明日全市场平均。偏离 50 的幅度 ∝ 预期超额收益 (加权 1pp ↔ 100 分)。
#
# ★★ v1 为什么是错的 (必须记录, 否则会再犯) ★★
#   v1 按 r1 = T收盘→T+1收盘 标定, 而该口径的 alpha **100% 在隔夜跳空**:
#     偏多档 +2.155pp = 跳空 +2.409 + 盘中 -0.254。
#   g56 信号 17:25 才产出、最早 T+1 开盘成交 ⇒ **跳空拿不到**。
#   ⇒ v1 把「今天涨停」打成**高分(偏多)**, 与可操作口径**完全相反** —— 可操作口径下,
#     今天涨停 ⇒ 明天开盘买是**负向**的 (首板 -0.196%/胜44.8%, 一字板 -0.641%/胜37.1%)。
#
# 标定 (可复跑 tmp/_nd2_final.py, 产物 tmp/nd2_final.json):
#   样本: 全市场 2025-09-01~2026-09-22, 1,332,513 个 (票×交易日), 258 日, 5229 票
#   目标: r1_oc = T+1收盘 / T+1开盘 - 1 ; 全市场均值 +0.095%, 上涨占比 48.4%
#   Δ   = 各档**绝对超额** (档内 r1_oc − 同日全市场均值), 单位 pp, 相对各因子 0 点
#   验收 (5 档按分值硬切; 全/seg1/seg2 **三段完全单调**):
#     <=45    偏空    n=  5422  r1_oc -0.455%  胜41.7%  (seg1 -0.427 / seg2 -0.490)
#     45~49   中性偏空 n=  6541  -0.270%       44.3%   (seg1 -0.256 / seg2 -0.281)
#     49~50.5 中性    n=1033716 +0.072%       48.6%
#     50.5~55 中性偏多 n=262421 +0.171%       48.0%
#     >55     偏多    n= 24413  +0.479%       48.0%  (seg1 +0.501 / seg2 +0.454)
#   逐日方向成立率 (防「少数极端日撑起来」): 偏空 64.2%(t=-5.69) / 中性偏空 60.9%(t=-3.59)
#     / 中性偏多 53.9%(t=+2.95) / 偏多 65.9%(t=+5.87) —— 四档均成立。
#   典型日排序力弱 (同日截面 IC≈-0.011) ⇒ **定位 = 事件条件期望, 不是全市场排序器**:
#     93% 的票落在中性 (那里没有可靠的边际信息, 不制造假分辨率), 分数只在有事件时离开 50。
# 因子 (三重筛选: 两段同向不缩水 + 逐日方向成立率 + 池化 t; 权重等权):
#   zt   涨停状态   首板 -0.276 / 连板 -0.446 / 触板未封 +0.387
#   seal 封板强度   一字板 -0.403 / 强封 -0.135 / 烂板 +0.121 (相对涨停加权平均)
#   lhbz 上榜未涨停 +0.543  ← 最强 (t=+6.05, 63.2% 日成立)
#   vr   量比       单调 -0.041 … +0.183
#   ev20 前20日上榜次数 (含 T) 单调 0 / +0.017 / +0.167 / +0.445
#   剔除: crash (两段反向, 逐日 48.1% t=+1.08 不成立) / zt20 (t<=1.8) / turn (98.8% 缺失)
#         / close_pos, ma20_dist (两段反向) / 上榜且涨停 (两段反向)
#   ⚠ 「上榜但没涨停」在 g56 出场收益口径是**最差象限**(57.5%), 在 T+1 口径是最强**正向**
#     (+0.539pp) ⇒ 又一次印证: **跨目标口径不可迁移, 换目标必须重新标定**。
# 纪律: 只读 —— 不改门/出场/截断; 涨跌停阈值只从 MarketSpec 取; 龙虎榜只经 dragon_tiger_store;
#       **fail-open** —— 数据不足/除权日 ⇒ 返回 None (宁可不展示, 也不给错的数)。
# ================================================================

#: 各因子各档 Δ (pp, 相对该因子 0 点)。唯一事实源, 标定脚本原样搬运。
ND_D = {
    "zt": {"streak2": -0.4455, "first": -0.2759, "touch": 0.3871, "none": 0.0},
    "seal": {"lt05": -0.4029, "0509": -0.1351, "ge09": 0.1209, "na": 0.0},
    "lhbz": {"yes": 0.5427, "no": 0.0},
    "vr": {"0508": -0.0321, "0810": -0.0171, "1013": 0.0191, "1316": 0.0451,
           "1620": 0.0598, "2030": 0.1152, "ge30": 0.1826, "lt05": -0.0412},
    "ev20": {"0": 0.0, "1-2": 0.017, "3-5": 0.1673, "6+": 0.4445},
}
#: 等权 (v1 已证: z 归一下「按跨度加权」不如随机; Δ 口径下等权 = 每因子同权)
ND_W = {"zt": 0.2, "seal": 0.2, "lhbz": 0.2, "vr": 0.2, "ev20": 0.2}
#: 分值 = 50 + ND_SCALE * Σ(W·Δ), 即加权 1pp ↔ 100 分
ND_SCALE = 100.0
#: 展示分档 (P(涨)×100, 下界降序匹配) → 标签 — 见 `_nd_tag_of`
ND_BANDS = ((52.0, "偏多"), (50.0, "略偏多"), (47.0, "中性"),
            (44.0, "略偏空"), (0.0, "偏空"))

# ================================================================
# 「明日操作分」v3 主分: P(T+1 开→收上涨) —— 与自选股同一思想, 事件分桶 logit
# ----------------------------------------------------------------
# 标定 (tmp/nd_pup_v1.json · 2026-09-25):
#   样本 1,332,513 (票×日), y=1{r1_oc>0}, 基率 48.43%
#   模型: 5 因子分桶 one-hot 逻辑回归 (参考档 logit=0)
#   AUC 0.5063 / 五分位 win 47.4→49.1 (弱但同向) —— 日频方向本身难;
#   事件因子真实信息在**超额期望** (辅分 ND_D, 五档 r1_oc 单调 -0.46%→+0.48%)。
#   ⚠ lhbz:yes 对 P(涨) 略负、对超额最强正 ⇒ 概率与收益**不可互相替代**, 主/辅并存。
# 参考档 (logit 贡献 0): zt=none, seal=na, lhbz=no, vr=1013, ev20=0
# 本分**不参与**门/排序/截断; 重标定后更新本表与版本注释。
# ================================================================

ND_PUP_BIAS = -0.050268
ND_PUP_W = {
    "zt:first": -0.104292,
    "zt:streak2": -0.182289,
    "zt:touch": 0.045615,
    "seal:0509": -0.044101,
    "seal:ge09": 0.008754,
    "seal:lt05": -0.251234,
    "lhbz:yes": -0.027848,
    "vr:0508": -0.011778,
    "vr:0810": -0.004606,
    "vr:1316": -0.005490,
    "vr:1620": -0.022160,
    "vr:2030": -0.049833,
    "vr:ge30": -0.029548,
    "vr:lt05": 0.024119,
    "ev20:1-2": -0.041406,
    "ev20:3-5": -0.026978,
    "ev20:6+": 0.071617,
}
#: 除权/异常保护: |单日涨跌幅| 超过此值 ⇒ 比值失真, 不产出
ND_EXDAY_CHG = 0.35
#: 量比 = 当日量 / 前 ND_VOL_WIN 个交易日均量
ND_VOL_WIN = 20
#: ev20 回看窗口 (交易日, 含 T)
ND_EV20_WIN = 20
#: 连板回溯上限
ND_STREAK_MAX = 10
_ND_LHB_CACHE: dict = {}
_ND_LHB_CACHE_MAX = 4096


def _nd_bucket_zt(zt, touch, streak):
    if zt:
        return "streak2" if (streak or 0) >= 2 else "first"
    return "touch" if touch else "none"


def _nd_bucket_seal(zt, amp):
    """封板强度: 仅涨停票有意义。非涨停 ⇒ `na` 中性档 (Δ=0, 不惩罚)。"""
    if not zt:
        return "na"
    if amp is None or amp != amp:            # None / NaN
        return "ge09"
    if amp < 0.05:
        return "lt05"
    if amp < 0.09:
        return "0509"
    return "ge09"


def _nd_bucket_ev20(ev20):
    if ev20 is None or ev20 != ev20:
        return "0"
    ev20 = int(ev20)
    if ev20 <= 0:
        return "0"
    if ev20 <= 2:
        return "1-2"
    return "3-5" if ev20 <= 5 else "6+"


def _nd_bucket_vr(vr):
    if vr is None or vr != vr:
        return "1013"
    for lab, lo, hi in (("lt05", 0.0, 0.5), ("0508", 0.5, 0.8), ("0810", 0.8, 1.0),
                        ("1013", 1.0, 1.3), ("1316", 1.3, 1.6), ("1620", 1.6, 2.0),
                        ("2030", 2.0, 3.0)):
        if lo <= vr < hi:
            return lab
    return "ge30"


def _nd_buckets(zt=False, touch=False, streak=0, amp=None, lhb=False, vr=None, ev20=None):
    """五因子分桶（主/辅共用，禁止两处各拼一套）。"""
    return {
        "zt": _nd_bucket_zt(zt, touch, streak),
        "seal": _nd_bucket_seal(zt, amp),
        "lhbz": "yes" if (lhb and not zt) else "no",
        "vr": _nd_bucket_vr(vr),
        "ev20": _nd_bucket_ev20(ev20),
    }


def _nd_score_of(zt=False, touch=False, streak=0, amp=None, lhb=False, vr=None, ev20=None):
    """明日操作分 = **P(T+1 开→收上涨)×100**（与自选股评分同一思想）。

    ⚠ 与策略质量分 `_score_of` 是两个东西: 后者是 scan.py:254 的每日限额截断键,
    本分**只作展示**, 不参与任何门/排序/截断 —— 禁止混用。

    口径 v3（2026-09-25）:
      主分 = σ(Σ 分桶 logit)×100 —— 用户裁定「上涨概率为主」
      辅  = 事件超额期望 (原 ND_D，`_nd_exp_of`) —— 事件因子对**超额**更有效
      实现与自选股不同: **事件分桶 logit** (zt/seal/lhbz/vr/ev20)，不是连续技术特征回归。
    缺特征 ⇒ 该子项取参考档 (logit 0)。fail-open: 全缺 → 返回 None 由上游处理。
    """
    b = _nd_buckets(zt=zt, touch=touch, streak=streak, amp=amp, lhb=lhb, vr=vr, ev20=ev20)
    z = ND_PUP_BIAS
    for fac, cat in b.items():
        z += ND_PUP_W.get(f"{fac}:{cat}", 0.0)
    z = max(-8.0, min(8.0, z))
    p = 1.0 / (1.0 + __import__("math").exp(-z))
    return round(p * 100.0, 1)


def _nd_exp_of(zt=False, touch=False, streak=0, amp=None, lhb=False, vr=None, ev20=None):
    """辅助: 明日开→收**超额期望** (pp, 50=全市场均值的旧口径换算) — 只作辅展示。"""
    b = _nd_buckets(zt=zt, touch=touch, streak=streak, amp=amp, lhb=lhb, vr=vr, ev20=ev20)
    mu = sum(ND_W[k] * ND_D[k][b[k]] for k in ND_W)
    return round(max(0.0, min(100.0, 50.0 + ND_SCALE * mu)), 1)


def _nd_tag_of(score):
    """P(涨)×100 → 操作读法（与自选股同一套语义；因基率≈48%，档位更贴事件分布）。"""
    if score is None:
        return "—"
    if score >= 52.0:
        return "偏多"
    if score >= 50.0:
        return "略偏多"
    if score > 47.0:
        return "中性"
    if score > 44.0:
        return "略偏空"
    return "偏空"


def _nd_parts(bars, i, board, market):
    """(zt, touch, streak, amp, vr, chg) —— 只用 bars[:i+1] (as-of 安全)。

    None ⇒ 数据不足 / 除权日 (不产出)。涨停阈值**只从 MarketSpec 取**。
    """
    if i < ND_VOL_WIN + 1:
        return None
    prev = float(bars[i - 1]["close"])
    if prev <= 0:
        return None
    chg = float(bars[i]["close"]) / prev - 1.0
    if abs(chg) > ND_EXDAY_CHG:              # 除权/异常: 比值失真, 判定不可信
        return None
    spec = market if market is not None else default_market()
    band = spec._band(board)
    if band is None:
        return None
    lim = band[0]
    zt = is_limit_up(float(bars[i]["close"]), prev, board, spec)
    touch = (not zt) and (float(bars[i]["high"]) / prev - 1.0 >= lim)
    streak, j = 0, i
    while j >= 1 and streak < ND_STREAK_MAX:
        pc = float(bars[j - 1]["close"])
        if pc <= 0:
            break
        if float(bars[j]["close"]) / pc - 1.0 >= lim:
            streak += 1
            j -= 1
        else:
            break
    amp = (float(bars[i]["high"]) - float(bars[i]["low"])) / prev
    vs = [float(bars[t].get("volume") or 0) for t in range(i - ND_VOL_WIN, i)]
    vr = None
    if vs and sum(vs) > 0:
        vr = float(bars[i].get("volume") or 0) / (sum(vs) / len(vs))
    return zt, touch, streak, amp, vr, chg


def _nd_lhb2(code, dates):
    """(T 日是否上榜, 前 ND_EV20_WIN 个交易日上榜次数——含 T)。

    该票榜史只查一次 (倒序全量) 后按票缓存, 再与 dates 求交 ⇒ 每票至多一次查询。
    查询异常 ⇒ (False, 0): 全落中性档, **fail-open 不阻信号**。
    """
    c = str(code)[:6].zfill(6)
    hist = _ND_LHB_CACHE.get(c)
    if hist is None:
        try:
            from app.market_cn.dragon_tiger_store import query_dragon_tiger

            rows = query_dragon_tiger(stock_code=c) or []
            hist = {str(r.get("trade_date"))[:10] for r in rows if r.get("trade_date")}
        except Exception as e:
            logger.debug("[g56.nd] 龙虎榜查询失败 %s: %s", c, e)
            hist = set()
        if len(_ND_LHB_CACHE) >= _ND_LHB_CACHE_MAX:
            _ND_LHB_CACHE.clear()
        _ND_LHB_CACHE[c] = hist
    ds = [str(d)[:10] for d in dates]
    return (bool(ds) and ds[-1] in hist), sum(1 for d in ds if d in hist)


def g56_nd_score(ctx: Ctx):
    """门表私有函数: 明日操作分 = P(T+1 涨)×100 (signal.fields 用; 缺数据 ⇒ None)。"""
    p = _nd_parts(ctx.bars, ctx.i, ctx.board_type, ctx.market)
    if p is None:
        return None
    zt, touch, streak, amp, vr, _chg = p
    i = ctx.i
    if i < ND_EV20_WIN - 1:
        return None
    dates = [str(ctx.bars[j]["time"])[:10] for j in range(i - ND_EV20_WIN + 1, i + 1)]
    lhb_today, ev20 = _nd_lhb2(ctx.code, dates)
    return _nd_score_of(zt=zt, touch=touch, streak=streak, amp=amp,
                        lhb=lhb_today, vr=vr, ev20=ev20)


def g56_nd_tag(ctx: Ctx):
    """门表私有函数: 操作分档标签 (偏多/略偏多/中性/略偏空/偏空, 按 P(涨)×100)。"""
    s = g56_nd_score(ctx)
    return None if s is None else _nd_tag_of(s)


def g56_nd_exp(ctx: Ctx):
    """门表私有函数: 明日开→收**超额期望**展示辅分 (50=全市场均值; 缺数据 ⇒ None)。"""
    p = _nd_parts(ctx.bars, ctx.i, ctx.board_type, ctx.market)
    if p is None:
        return None
    zt, touch, streak, amp, vr, _chg = p
    i = ctx.i
    if i < ND_EV20_WIN - 1:
        return None
    dates = [str(ctx.bars[j]["time"])[:10] for j in range(i - ND_EV20_WIN + 1, i + 1)]
    lhb_today, ev20 = _nd_lhb2(ctx.code, dates)
    return _nd_exp_of(zt=zt, touch=touch, streak=streak, amp=amp,
                      lhb=lhb_today, vr=vr, ev20=ev20)


register_strategy_funcs(
    'g56',
    {"feat": g56_feat, "finite": g56_finite, "warmup": g56_warmup, "pool_stat": g56_pool_stat, "pool_field": g56_pool_field, "board_is_main": board_is_main, "board_is_gem": board_is_gem, "nd_score": g56_nd_score, "nd_tag": g56_nd_tag, "nd_exp": g56_nd_exp},
    d0={"feat": 0, "finite": 0, "warmup": 0, "pool_stat": 0, "pool_field": 0, "board_is_main": 0, "board_is_gem": 0, "nd_score": 0, "nd_tag": 0, "nd_exp": 0},
)


# ---- exit_modes 注册 (2026-09-26 P1-9 层反转) ----
def _exit_g56_no_trail(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """g56 无追踪 7d/-8% 出场 — 供 YAML exit.mode=g56_no_trail。"""
    from app.market_cn.auto.core.exit_modes import _bp
    return _exit_no_trail(bars, entry_idx, entry_price,
                          _bp(params, board_type, "hold_days"),
                          _bp(params, board_type, "stop_loss"),
                          code=code)


from app.market_cn.auto.core.exit_modes import register_exit as _register_exit
_register_exit("g56_no_trail", _exit_g56_no_trail)


# ================================================================
# 门表回测编排 (2026-09-28 分层改造: 自 core/runtime/evaluate.py **纯搬运**下沉)
# ----------------------------------------------------------------
# 为什么搬回来: 编排是策略的一部分 (枚举顺序/去重键/展示字段集/入场腿), 放在 core
# 会让"改 g56 口径"变成改架构层, 且 core 反过来惰性 import strategies.* (层反转)。
# 自注册到 core/runtime/flows 注册表 → core 只查表, 未登记即 fail-fast (不再静默落 v1)。
# ⚠ 搬运要求: 签名与语义**逐字不变**; 逐笔等价回归见
#    analysis_output/auto架构分层_20260928.md
# ================================================================

from typing import Any, Dict, List

from app.market_cn.auto.core.entry_modes import resolve_entry
from app.market_cn.auto.core.exit_modes import run_exit
from app.market_cn.auto.core.runtime.flows import register_day_flow

def _backtest_day_flow(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版 g56(五重共振) 全历史回测，返回 trades 列表（与 g56.backtest_stock 逐笔等价）。

    编排逐字镜像 backtest_stock：北交所/长度早返回 → 特征 O(n) 一次预计算 → 横截面池聚合
    → 逐日 s（信号日 k=s-1）→ 锁仓去重（s <= last_exit_idx 跳过，**在门表之前**）→ 门表求值
    → 入场=entry_modes(open + gap_max 分板块) → 出场=exit_modes(g56_no_trail 无追踪 7d/-8%)
    → 锁仓至退出日。g56 无 U1~U4（use_unified_prefilter=False）。
    起点/终点由 meta.day_start(68) / day_end(9) 声明（镜像 range(68, n-9)）。
    """
    from app.market_cn.auto.core.features.cross_section import _ensure_pool_daily, _g1_arrays
    # build_signal 属 core.runtime.evaluate; 此处**必须**函数体内 import —— 顶层 import 会
    # 在 evaluate 半初始化 (ensure_gate_init → autodiscover → 本模块) 时取不到该名字而成环。
    from app.market_cn.auto.core.runtime.evaluate import build_signal

    if str(code).startswith(("8", "4", "92")) or len(bars) < 68:
        return []
    _p = spec.params
    n = len(bars)
    # 特征一次预计算（镜像修复① O(n^2)→O(n)）；池按 pool_target 跨股缓存复用
    ext = {"g56_feats": _g1_arrays(bars),
           "g56_pool": _ensure_pool_daily(str(bars[-1]["time"])[:10])}
    trades: List[Dict[str, Any]] = []
    last_exit_idx = -1

    for s in range(int(spec.meta.get("day_start", 68)), n - int(spec.meta.get("day_end", 9))):
        if float(bars[s].get("open") or 0) <= 0 or s <= last_exit_idx:
            continue                        # 锁仓去重（镜像修复②：未退出前不重复入场）
        i = s - 1                           # 信号日 D-1
        ctx = Ctx(bars, i, lu_idx=0, params=_p, board_type=board_type, code=code,
                  stock_info=stock_info, ext=ext, market=spec.market_spec)
        ok, _ = ev.evaluate_all(bars, i, _p, ctx=ctx)
        if not ok:
            continue

        # 入场 = entry_modes（open + gap_max；gap >= 涨停幅度 → 一字/触板不可买）
        site, _reason = resolve_entry(spec.entry, bars, i, board_type, _p)
        if site is None:
            continue

        # 出场 = exit_modes（g56_no_trail，无追踪 7d/-8%）
        result = run_exit(spec.exit.get("mode", "g56_no_trail"), bars=bars,
                          entry_idx=site["entry_idx"], entry_price=site["entry_price"],
                          code=code, board_type=board_type, params=_p, diag=site["diag"])
        if not result:
            # R1: 数据结束未平 → 末日收盘平仓（与主回测路径同口径）
            result = data_end_close(bars, site["entry_idx"], site["entry_price"])
        if not result:
            continue

        sig = build_signal(ctx, spec)
        ed = s + int(result["exit_day"]) - 1
        trades.append({
            "code": code,
            "board": board_type,
            "strategy": spec.key,
            "signal_date": str(bars[i]["time"])[:10],
            "entry_date": str(bars[s]["time"])[:10],
            "entry_price": round(float(bars[s]["open"]), 3),
            "entry_gap": round(float(site["diag"]["d1_gap"]), 2),
            "exit_date": str(bars[ed]["time"])[:10]
            if 0 < int(result["exit_day"]) and ed < n else None,
            "exit_price": result["exit_price"],
            "exit_day": result["exit_day"],
            "return_pct": result["return_pct"],
            "peak_return_pct": result["peak_return_pct"],
            "exit_reason": result.get("exit_reason"),
            **sig,
            "buy_mode": "next_open",
        })
        last_exit_idx = ed
    return trades

register_day_flow("g56", _backtest_day_flow)
