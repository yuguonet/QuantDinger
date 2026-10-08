# -*- coding: utf-8 -*-
"""领涨追击 (lead_chase) v5 —— 「日线前置池 + 早盘时段量比 + T+1 出场」

★ 2026-09-28 按用户指定的**结果先行**方法论 + 逐轮口径修正重做 (v2/v3/v4 均已作废)。

━━ 用户口径修正 (按时间顺序) ━━
M1 量比 = **今日截止当前时刻累计量 / 昨日截止同一时刻累计量** (同时间截面),
   早盘误差大 ⇒ 再引入"昨日全日量/前5日均量"作参考基准防过拟合。
M2 出场只要符合 T+1 即可; 评估入场规则时用"次日收盘卖"最简单直接。
M3 入场位置**不要求涨停附近** —— 回踩支撑企稳 / 站稳压力位 都是好切入点。
M4 **只做 T+1**: 今天买明天卖, 不考虑 D+2 以上 (v4 的"持3日/持8日"正收益全部作废)。
M5 经验: 早盘涨停 > 下午涨停; 9:30~9:40 冲高回落是最大噪音; 9:30~10:00 涨停也强。
M6 **9:30~10:00 涨停基本靠历史日线分析 ⇒ 前置筛选必须用日线把 5000 只缩到几百只**。
M7 量比不是固定值, 会随时间变化 ⇒ 早盘量能应跟**最近几天同一时段**比, 不能用昨日全日量。

━━ 实证结论 (完整过程见 analysis_output/领涨追击_重设计_v6_20260928.md) ━━

★★ [F] 前视偏差 —— v5 之前的「09:40 买入 +3.13%」是假的
    事件集按「当日盘中拉升涨停」事后定义, 在其上回看 09:40 买入必然吃到已发生的拉升。
    实测(38,057 事件): 09:40 买 -> T+1 收盘卖 总收益 +353bp, 其中
      当日段(09:40->收盘) +356bp (t=142)   次日段(收盘->T+1收盘) −3.2bp (t=−1.21)
    ⇒ 收益 100% 在当日段, 次日段为零。而 09:40 前**尚未**拉升的组次日段超额 −10.1bp
    (t=−2.98) —— 拉升越大, 过夜回吐越多。

★★ [G] A股 T+1 制度 —— 当日段浮盈不可兑现 (这是本策略的结构性死结)
    09:40 买入的票当日不能卖, 唯一可兑现口径是 T+1 开盘卖 / T+1 收盘卖。
    全市场无偏基线(567,648 行 / 136 日 / 5,219 票):
      T+1开盘卖 −6.2bp | T+1收盘卖 +2.9bp | 当日浮盈段 +8.7bp | 过夜段 −5.4bp
    ⇒ 「追涨」赚的本来是当日拉升, 但 T+1 强制你把它带过夜, 而过夜恰好回吐。

[H] M5 经验验证成立, 且是**负向**规则(用于剔除, 不是买入):
    早盘冲高回落>3% 组 −52.2bp (t=−17.17); 剔除后 +7.5bp。
[G'] 近期涨停过 ⇒ 次日显著为负: zt20>=1 组 −17.0bp (t=−9.78), 次日段超额 −12.3bp
    (t=−9.67)。与 g56 结论同源, 可作**通用排除条件**。

★ [I] M7 时段量比被证伪: 全市场无偏 corr(vr_slot_0940, 次日段) = −0.0026,
    与总收益 −0.0021。分档无单调性, |t|<3。⇒ 早盘量能不能预测 T+1。

★ [J] 样本外: 前半段(68日)网格选参 2,422 组合 -> 后半段(68日)验证
    corr(in_bp, out_bp) = −0.3119, corr(in_t, out_bp) = −0.3731
    ⇒ **样本内挑得越狠, 样本外越差** —— 收益来自参数选择偏差。
    in-sample 最优组(+137bp) 的 OOS 仅 +34.8bp; 按 t 选的最优组 OOS 仅 +6.1bp
    (OOS 期全市场基线 +6.8bp ⇒ **零超额**)。

★★ [K] 最后一刀 (固定规则, 只用 OOS 期, 日度口径):
    日度 alpha = −27.9 bp/日 (t=−1.36, 不显著); beta = 1.211 (t=11.75)
      ↑ 注意: 逐笔 pooled t=4.27 是虚高的(同日收益高度相关, 非独立观测),
        日度回归才是正确口径 —— 68 个独立日观测。
    市场上涨日超额 −11.5bp / 下跌日超额 −49.3bp ⇒ **两头都跑输**, 典型高 beta 劣势。
    公平安慰剂(同日 + 09:40已知涨幅带分层): 观测 +34.8bp vs 安慰剂 +38.3±6.1 ⇒ **z=−0.58**。
    成本: T+1收盘卖 双边20bp 即 −5.2bp; T+1开盘卖 双边10bp 即 −10.9bp。

⚠ **终审结论: 本策略在「今天买明天卖」(M4) 约束下结构性不成立, 保持 enabled=false**。
  死结不是参数没调好, 而是: 收益全部产生在当日拉升段, 而 A股 T+1 禁止当日卖出,
  必须过夜; 拉升股的过夜回吐恰好最强(见 [F])。追涨在 T+1 下是负和游戏。
  若要保留该方向, 只能改口径 —— 尾盘(已知拉升)选股 + 次日开盘买, 而这条已被
  g56(17:25 dragon_scan 选当日涨停 -> 次日开盘入)覆盖, 无需另立门户。
  本文件保留完整实现与全部实证记录, 供后续接入封单/逐笔等更细粒度数据后重评。

━━ 2026-09-28 追加: v7 —— 上面的"结构性不成立"**被推翻一半** ━━

v6 的死结是**样本**: 它用事后定义的"当日盘中拉升涨停"事件集。换成**全市场无偏样本 +
09:40 固定入场 + T+1 收盘出场**后, 日内均线类因子成立。完整过程见
`analysis_output/领涨追击_重设计_v7_20260928.md`。

[v7-0] 为什么必须 09:40 入场而不能更早
  dev_40 = 09:40价 / (09:31~09:40 分时VWAP) − 1, 用到直到 09:40 的量价。
  ⇒ **09:40 之前成交即前视**。延伸到更晚虽不再前视, 但信息已被定价:
    alpha 09:40 +25.8bp/日(t=2.63) → 09:45 +16.7(1.75) → 09:50 +17.1(1.77)
         → 09:55 +16.5(1.72) → 10:00 +13.6(1.41)
  ⇒ 成交时点固定 **09:40 单点** (ScanSpec windows=("09:40","09:40"))。

[v7-A] 规则 (全市场无偏, 09:40 已知, 无前视)
  dev_40 同日截面排名前 N%  AND  近20日涨停次数=0  AND  早盘回撤<=1%
  入场 09:40, 出场 D+1 收盘。三个输入在 09:40 时点全部可得。

[v7-B] 切点定 60% (不是报告里的 40%)
  按 dev_40 切前10%/20%/40%/60% 都显著, 但**把成交时点往后挪**时衰减速度不同:
    前40%: 09:45 t=1.75 → 10:00 t=1.41 (掉出显著)
    前60%: 09:45 t=2.47 → 10:00 t=1.93 (仍显著)
  ⇒ 60% 抗时点漂移最强, beta 0.891 (前40% 0.993, 前10% 1.259 偏高)。
  ⇒ dev40_pctile = 60.0

[v7-C] 大盘门定为 mkt_gate = -0.3 (全市场等权均涨幅, 与 scan._mkt_gain 同口径)
   无门   +39.6bp  日胜率63.2  t=3.10  跌日27
   -0.5%  +50.0bp  日胜率67.4  t=2.61  跌日17
   -0.3%  +53.6bp  日胜率66.7  t=2.54  跌日14   ← 取
    0.0%  +45.9bp  日胜率67.7  t=2.18  跌日12
  ⇒ 门控后**日超额几乎不变(22~24bp)**, 但绝对收益改善 = 纯粹避损, 值得做。

[v7-D] 诚实预期 (不要拿 54.3% 当承诺)
  54.3% 是 09:40 单点的峰值。同一套规则只改成成交时点: 09:45 52.5% / 10:00 51.5% /
  09:35(公平口径) 50.8% ⇒ **可执行区间取 51.5%~52.5%**, 54.3% 只能当上限引用。
  且所有报数是**毛口径**, 扣双边 20bp 后收益减 20bp、逐笔胜率降 2~3pp。

[v7-E] daily_limit = 30
  候选池(Top60% & zt20=0 & fade<=1% & 大盘门)后按 dev_40 排序:
    Top10 +128.4bp(t=1.84) | Top20 +92.6(t=1.20) | Top30 +88.9(t=1.28) | Top50 +76.0(t=1.00)
  TopK 越小单笔收益越高但天数越少、显著性越差; Top20~30 接近顶值且样本最多。

[v7-F] ★ 流动性门槛必须关 (amt_lo = 0)
  v7 报告 §4.4 的"成交额>=1亿更好(+68.2bp)"是**事后口径**: close*volume 要收盘才知道。
  09:40 可用的任何代理都失效 (验证段 68 日, 同一套规则):
    当日全天额>=1亿 [事后不可用]  +46.3bp  t=2.88
    早盘09:40累计额>=2000万       +24.2bp  t=0.98
    昨日全天额>=1亿               +25.3bp  t=1.19
    前5日均额>=1亿                +25.5bp  t=1.24
  ⇒ 真实判别力来自**当日**资金关注度, 盘中不可得。早盘累计额还会把样本推向"开盘爆量"
    的消息票。⇒ v7 一律不加流动性门槛。

[v7-G] v7 模式不套用旧规则的 gain_lo(当日涨幅>=2%) 预筛
  v7 样本是**全市场无偏**(含下跌票)。加当日涨幅预筛会把样本退化成"已经涨过的票",
  与验证口径不符 (且结构上接近 v6 被判伪的那条路)。实测其单笔收益更高(+60.3bp),
  但样本只剩 11%、t 降到 1.62 ⇒ 不作为默认口径。

⚠ 仍未启用 (enabled=false): ① 验证段仅 68 个交易日, 建议再攒 3~6 个月;
  ② 纯多头无择时, 下跌日即使加门控仍可能绝对亏损; ③ **执行通道未就绪** ——
  现有 auto/scan.py::run_scan_knife 是下午滚动设计 (终端 15:00 快照 + 每轮 purge),
  早盘单点策略即便 enabled=true 也不会出信号, 需先解决编排 (见 2026-09-28 日志)。
"""
from __future__ import annotations

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import ExitDecision, ScanSpec, Signal, StrategyBase
from app.market_cn.auto.core.data.hub import minute_1m, stock_info
from app.market_cn.auto.core.market import get_board_type

STRATEGY_KEY = "lead_chase"
STRATEGY_LABEL = "领涨追击"

PARAMS = {
    # ── ① 入场时间窗 (M4/M5: 只做早盘, 9:30~10:00) ──
    "min_hhmm": "09:35",        # 旧规则窗口下界 (rule_mode!=v7_dev40 时使用)
    "max_hhmm": "10:00",        # 旧规则窗口上界 (M5: 早盘涨停优于下午)
    "win_lo": "09:40",          # ★ v7 成交时刻下界 (见 [v7-0]: 早于此即前视)
    "win_hi": "09:40",          # ★ v7 成交时刻上界 (晚于此 alpha 单调衰减)
    "noise_spike_pct": 3.0,     # 早盘冲高阈值 % (相对前收)
    "noise_fade_pct": 3.0,      # 从早盘高点回落超过该值 ⇒ 判定为冲高回落噪音, 剔除
    "noise_window_hhmm": "09:40",   # 早盘噪音观察截止时点
    # ── ② 盘中确认 ──
    "gain_lo": 2.0,             # 当日涨幅下限 % (领涨追击本义: 不追未启动)
    # ── ③ 时段量比 (M7: 与最近N日同一时刻比, 不用昨日全日量) ──
    "vol_slot_lo": 0.8,         # 今日同一时刻累计量 / 最近N日同一时刻累计量均值 下限
    "vol_slot_hi": 3.0,         # 上限
    "vol_slot_days": 5,         # 回看交易日数
    "vol_slot_min_days": 3,     # 有效基准最少天数, 不足则跳过该票
    # ── ④ 日线前置池 (M6: T-1 收盘算, 把全市场缩到几百只) ──
    "pool_enabled": True,
    "pool_d_ma20_lo": 0.0,      # 收盘站上 MA20 的幅度 % 下限 (实证 [3,10] 最好, 放宽到>=0)
    "pool_d_ma20_hi": 12.0,     # 上限 (过高=过热)
    "pool_bull": True,          # 要求 MA5>MA10>MA20 多头排列
    "pool_amp20_lo": 20.0,      # 20日振幅 % 下限 (要活跃)
    "pool_vr1_lo": 1.0,         # 昨日量/5日均量 下限 (温和放量)
    "pool_vr1_hi": 3.0,         # 上限
    "pool_pos20_lo": 40.0,      # 收盘处于20日区间的位置 % 下限
    "pool_ret60_hi": 100.0,     # 前60日涨幅 % 上限 (防过热)
    # ── ⑤ 大盘 / 流动性 ──
    "mkt_gate": -0.3,           # 大盘当日均涨幅下限 % (见 [v7-C]: -0.5~-0.3 为稳健区间)
    "cap_lo": 20e8,             # 流通市值下限(元); 数据缺失 fail-open
    # ── 门控 / 生命周期 ──
    "daily_limit": 30,          # 见 [v7-E]: Top20~30 接近顶值且样本最多
    # ── ⑥ 出场 (M2/M4: T+1, 默认次日收盘卖) ──
    "exit_mode": "close",       # close=次日收盘 | open=次日开盘 | limit=挂单止盈
    "exit_limit_pct": 3.0,      # exit_mode=limit 时的挂单止盈 %
    "stop_pct": -6.0,           # 次日盘中硬止损 %
    # ── ⑦ v7 日内均线规则 (2026-09-28 实证, analysis_output/领涨追击_重设计_v7) ──
    # 只做 T+1; 09:40 口径; 推荐切点 前40%~60% (beta≈1, alpha t 最高)
    "rule_mode": "v7_dev40",   # off=旧规则 | v7_dev40=日内均线截面
    "dev40_hhmm": "09:40",     # 取分时均线截止时刻 (VWAP 窗口 09:31~09:40)
    "dev40_pctile": 60.0,      # ★ 同日截面取前 N% —— 定 60 的依据见 [v7-B] (抗时点漂移)
    "zt20_max": 0,             # 近20日涨停次数上限 (0=不许有)
    "fade_max": 1.0,           # 早盘冲高回撤上限 % ((早盘高-现价)/昨收)
    "amt_lo": 0,               # ★ 必须保持 0 —— 依据见 [v7-F] (全天额是事后变量)
    "gain_lo_v7": False,       # ★ v7 是否套用当日涨幅>=gain_lo 预筛 (默认否, 见 [v7-G])
}

#: 创/科板涨跌幅缩放
GAIN_SCALE_GEM = 2.0

#: 近封板判定系数 (与 tail 口径一致: 名义涨停价×0.995)
_AT_LIMIT = 0.995

#: 分钟槽映射用到的时点 (与 frames.MI_HHMM 同构)
_SLOT_CACHE: dict = {}
# 前置池缓存 (日度)
_POOL_CACHE = {"date": None, "set": None}
# 市值缓存 (日度)
_STOCK_CACHE = {"date": None, "info": None}


def _hhmm(s) -> str:
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _gain_scale(code: str) -> float:
    return GAIN_SCALE_GEM if get_board_type(code) == "gem_star" else 1.0


def _limit_pct(code: str) -> float:
    """名义涨停幅度 (可买入判定用)。

    ⚠️ 同 tail._limit_pct 口径 —— 不要用 MarketSpec.nominal_up_pct (已折 up_tol,
    是 is_limit_up 的判别阈值), 两者语义不同。
    """
    return 0.20 if _gain_scale(code) == GAIN_SCALE_GEM else 0.10


def _minute_pos(hhmm: str) -> int:
    """'HH:MM' → 0~239 (09:30 起); 非交易时段返回 -1。"""
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:5])
    except (TypeError, ValueError):
        return -1
    minutes = h * 60 + m
    if minutes <= 570:          # <= 09:30
        return -1
    if minutes <= 690:          # 09:31~11:30
        return minutes - 571
    if minutes < 780:           # 午休
        return -1
    if minutes <= 900:          # 13:00~15:00
        return 119 + (minutes - 780)
    return -1




def _vwap_dev(series, hhmm: str = "09:40") -> float:
    """dev: 现价/分时VWAP − 1 (%, 09:31~hhmm)。v7 核心因子。

    series: 快照序列 [{time, last, volume}, ...] (volume 累计, 与 day_series 一致)。
    无数据 → nan。
    """
    try:
        import math
        cut = hhmm
        pv = vol = 0.0
        prev_v = 0.0
        last_px = None
        # ★ 2026-09-28 修: 旧实现在 `hm < "09:31"` 分支里仍把该 bar 的 last 当现价
        #   ⇒ 缺 09:40 bar 时会静默用 09:30 的价格。改为: 窗口外只更新"量基线",
        #   不碰 last_px; 且窗口起点基线必须是 09:30 收盘累计量 (volume 是当日累计值,
        #   若从 0 起算会把 09:30 的量按 09:31 的价计入 VWAP)。
        for r in series or []:
            ts = str(r.get("time") or "")
            hm = ts[11:16] if len(ts) >= 16 else ""
            if not hm:
                continue
            v = float(r.get("volume") or 0)
            if hm < "09:31":
                if v > 0:
                    prev_v = v                  # 仅作窗口起点基线
                continue
            if hm > cut:
                break
            last_px = float(r.get("last") or 0) or last_px
            dv = max(0.0, v - prev_v)
            prev_v = v
            if dv > 0 and last_px > 0:
                pv += last_px * dv
                vol += dv
        if vol <= 0 or not last_px or last_px <= 0:
            return float("nan")
        vwap = pv / vol
        return (last_px / vwap - 1) * 100
    except Exception:
        return float("nan")


def _fade_pct(series, snap, hhmm: str = "09:40") -> float:
    """早盘回撤 %: (09:40前最高 − 现价) / 昨收。noise: 越大越像冲高回落。"""
    try:
        pc = float((snap or {}).get("previousClose") or 0)
        last = float((snap or {}).get("last") or 0)
        if pc <= 0 or last <= 0:
            return float("nan")
        hi = last
        for r in series or []:
            hm = str(r.get("time") or "")[11:16]
            if hm and hm <= hhmm:
                h = float(r.get("high") or r.get("last") or 0)
                if h > hi:
                    hi = h
        return (hi - last) / pc * 100
    except Exception:
        return float("nan")


def _zt_count(bars, code: str, n: int = 20) -> int:
    """近 n 日涨停次数 (窗口 = 末根 bar 及其前 n-1 根)。

    调用方传的 `bars` 末根应为 **T-1 及更早** (盘中路径的 bars 到昨收为止), 故本函数
    含末根即等价于"T-1 及更早", 无前视。

    ⚠ 2026-09-28 审计 A4: 原实现把 `code` 写死 None 后**直接 `return 0`** ⇒ 死函数
    (任何输入恒 0), 而唯一用到 zt20 的地方当时内联了同一段循环 ⇒ 同一口径两份实现。
    现统一到本函数 (共享 `is_limit_up`), 行为与那处内联完全一致。
    """
    if not bars:
        return 0
    from app.market_cn.auto.core.market import get_board_type, is_limit_up
    bt = get_board_type(code)
    b = list(bars)
    end = len(b) - 1
    zt = 0
    for j in range(max(1, end - n + 1), end + 1):
        if j > 0 and is_limit_up(float(b[j]["close"]), float(b[j - 1]["close"]), bt):
            zt += 1
    return zt



def _hist_slot_volume(code: str, end_day: str, days: int, slot: int):
    """最近 `days` 个交易日、截至同一分钟槽 `slot` 的累计成交量列表。

    M7: 量比随时间变化 ⇒ 早盘量能的合理基准是**最近几天同一时段**的量,
    而不是昨日全日量。数据来自 kline_1m_YYYY (T-1 及更早由 post_market_batch 回填)。
    """
    if slot < 0 or days <= 0:
        return []
    key = (code, end_day, days, slot)
    if key in _SLOT_CACHE:
        return _SLOT_CACHE[key]
    # 2026-09-29 审计修复 (P1): 原先 start=end=end_day 只取当日, 再过滤 d < end_day
    # 必为空 ⇒ 恒返回 []、时段量比门永远"基准不足"、旧规则模式零信号。
    # 回看窗口取 days×1.6+15 日历日 (交易日→日历日放大 + 假期裕量, 同 cleanup_cutoff
    # 思路), 多取的旧日由下方 [-days:] 截断。
    from datetime import datetime, timedelta
    try:
        start = (datetime.strptime(end_day, "%Y-%m-%d")
                 - timedelta(days=int(days * 1.6) + 15)).strftime("%Y-%m-%d")
    except Exception:
        start = end_day
    try:
        rows = minute_1m(code, start=start, end=end_day)
    except Exception:
        rows = []
    per_day: dict = {}
    for r in rows:
        d = str(r.get("time") or "")[:10]
        p = _minute_pos(str(r.get("time") or "")[11:16])
        if p < 0 or p > slot:
            continue
        per_day[d] = per_day.get(d, 0.0) + float(r.get("volume") or 0)
    # 只要 end_day 之前的交易日
    vals = [v for d, v in sorted(per_day.items()) if d < end_day][-days:]
    _SLOT_CACHE[key] = vals
    return vals


def _cap_of(code: str, day: str, last: float):
    """流通市值(元); 基础数据缺失返回 0 ⇒ 调用方 fail-open。"""
    if _STOCK_CACHE["date"] != day:
        _STOCK_CACHE["info"] = stock_info()
        _STOCK_CACHE["date"] = day
    si = (_STOCK_CACHE.get("info") or {}).get(code) or {}
    try:
        return float(si.get("circ_shares") or 0) * last
    except (TypeError, ValueError):
        return 0.0


@register
class LeadChaseStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "lc"
    # ★ 单点窗口: dev_40 含到 09:40 的量价, 早成交=前视; 晚成交 alpha 衰减 (见 [v7-0])
    scan_spec = ScanSpec(kind="intraday_window", windows=("09:40", "09:40"), interval_sec=60)
    default_params = dict(PARAMS)
    # 框架契约: 回测未含 U1~U4 (自实现门); 盘中即买 → T+1 当日不可卖
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    data_needs = ("daily", "snapshot", "minute_live")
    market_env = "trend"   # 追涨顺势: 弱市 reduce/halt (2026-09-28)

    # ── 第一段: 全市场快照便宜预筛 (时间窗 + 可买入 + 涨幅 + 早盘噪音) ──
    def intraday_shortlist(self, snaps, mkt_gain, **params):
        p = self.params(params or None)
        if mkt_gain is not None and mkt_gain < p["mkt_gate"]:
            return {}
        hhmm = ""
        for snap in snaps.values():
            hhmm = _hhmm(snap.get("time") or "")
            break
        # 窗口: v7 用单点 win_lo~win_hi; 旧规则沿用 min_hhmm~max_hhmm
        _v7 = str(p.get("rule_mode", "")).lower() == "v7_dev40"
        lo, hi = (str(p.get("win_lo", "09:40")), str(p.get("win_hi", "09:40"))) \
            if _v7 else (p["min_hhmm"], p["max_hhmm"])
        if not (lo <= hhmm <= hi):
            return {}
        # [v7-G] v7 是全市场无偏样本, 不能用当日涨幅先把下跌票筛掉
        _skip_gain = _v7 and not bool(p.get("gain_lo_v7", False))
        out = {}
        for code, snap in snaps.items():
            if code.startswith(("8", "4", "92")):            # 北交所排除
                continue
            try:
                last = float(snap.get("last") or 0)
                pc = float(snap.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or pc <= 0:
                continue
            # 已封死 ⇒ 买不进
            if last >= round(pc * (1 + _limit_pct(code)), 2) * _AT_LIMIT:
                continue
            if not _skip_gain and (last / pc - 1) * 100 < p["gain_lo"] * _gain_scale(code):
                continue
            out[code] = snap

        # v7: 全市场 dev_40 截面前 N% (日内均线偏离高优先)
        if str(p.get("rule_mode", "")).lower() == "v7_dev40" and out:
            try:
                from app.market_cn.auto.core.data.hub import day_series
                date = ""
                for s in snaps.values():
                    date = str(s.get("time") or "")[:10]
                    break
                series_map = day_series(list(out.keys()), date=date) if date else {}
            except Exception:
                series_map = {}
            scored = []
            for code, snap in out.items():
                ser = series_map.get(code) or []
                dev = _vwap_dev(ser, p.get("dev40_hhmm", "09:40"))
                if dev != dev:
                    continue
                fade = _fade_pct(ser, snap, p.get("dev40_hhmm", "09:40"))
                if fade == fade and fade > float(p.get("fade_max", 1.0)):
                    continue
                scored.append((dev, code, snap, fade))
            scored.sort(key=lambda x: -x[0])
            pct = float(p.get("dev40_pctile", 40.0) or 40)
            k = max(1, int(len(scored) * pct / 100)) if scored else 0
            out = {c: s for _, c, s, _ in scored[:k]}
        return out

    # ── 第二段: 单票深判 (日线前置池 + 早盘噪音剔除 + 时段量比) ──
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        p = self.params(params or None)
        ctx = ctx or {}
        snap, series = ctx.get("latest"), ctx.get("series") or []
        if not snap or not series:
            return []
        last = float(snap.get("last") or 0)
        pc = float(snap.get("previousClose") or 0)
        if last <= 0 or pc <= 0 or len(bars or []) < 21:
            return []
        _tr = None
        if probe is not None:
            def _tr(stage, **kw):
                probe.trace(stage, code=code, d0_date=str(snap.get("time") or "")[:10], **kw)

        hhmm = _hhmm(snap.get("time") or "")
        day = str(snap.get("time") or "")[:10]
        if not (p["min_hhmm"] <= hhmm <= p["max_hhmm"]):
            if _tr:
                _tr("window", hhmm=hhmm)
            return []
        mkt_gain = ctx.get("mkt_gain")
        if mkt_gain is not None and mkt_gain < p["mkt_gate"]:
            if _tr:
                _tr("mkt", mkt_gain=mkt_gain)
            return []


        # ⑦ v7 日内均线规则 (2026-09-28): 只做 T+1; 09:40 口径; 无前视
        #   dev_40 = 价/分时VWAP−1; 同日截面前 N% (推荐 40~60); zt20=0; fade<=1
        if str(p.get("rule_mode", "")).lower() == "v7_dev40":
            if not series:
                return []
            dev = _vwap_dev(series, p.get("dev40_hhmm", "09:40"))
            if dev != dev:  # nan
                if _tr:
                    _tr("signal", reason="dev40_nan")
                return []
            fade = _fade_pct(series, snap, p.get("dev40_hhmm", "09:40"))
            # 噪音: 早盘冲高回撤
            try:
                if fade == fade and fade > float(p.get("fade_max", 1.0)):
                    if _tr:
                        _tr("noise", fade=round(float(fade), 2))
                    return []
            except Exception:
                pass
            # 近20日涨停 —— 口径统一走 _zt_count。
            # ★ 守卫: _zt_count 统计末根及其前 19 根, 若盘中路径把当日半成品日线一并给了
            #   bars, 末根就是今天 ⇒ zt20 会含今日涨停 (既前视又与实证口径不符)。
            #   实证 zt20 窗口是 T-1 及更早, 故此处显式剔掉当日 bar。
            try:
                b20 = bars
                if b20 and str(b20[-1].get("time") or "")[:10] == day:
                    b20 = b20[:-1]
                zt = _zt_count(b20, code, 20)
                if zt > int(p.get("zt20_max", 0)):
                    if _tr:
                        _tr("signal", zt20=zt)
                    return []
            except Exception:
                pass
            # ★ 流动性: 一律不过滤。见 [v7-F] —— 实证的"全天成交额>=1亿"是事后变量,
            #   09:40 拿不到; 早盘累计额/昨日额/前5日均额等事前代理实测都把日 alpha
            #   t 从 2.54 打到 0.7~1.2 (不显著)。amt_lo 仅保留参数位。
            try:
                if _tr:
                    _tr("signal", amt40=round(last * float(snap.get("volume") or 0), 0))
            except Exception:
                pass
            # 截面切点: 优先 ctx["dev40_cut"]; 否则只记录 dev (单票无法自证分位)
            cut = ctx.get("dev40_cut")
            if cut is not None:
                try:
                    if dev < float(cut):
                        if _tr:
                            _tr("signal", dev40=round(dev, 3), cut=float(cut))
                        return []
                except Exception:
                    pass
            if _tr:
                _tr("signal", dev40=round(dev, 3), fade=round(float(fade or 0), 2))
            return [Signal(
                code=code,
                time=str(snap.get("time") or "")[:10],
                score=min(100, max(0, int(round(50 + dev * 10)))),
                price=last,
                label=f"领涨v7 dev40={dev:.2f}% fade={float(fade or 0):.1f}",
                extra={"dev40": round(dev, 3), "fade": round(float(fade or 0), 2),
                       "rule": "v7_dev40", "hhmm": hhmm},
            )]

        # ④ 日线前置池 (M6: 昨天收盘就能算, 把全市场缩到几百只)
        if bool(p.get("pool_enabled", True)) and code not in self._ensure_pool(day, p):
            if _tr:
                _tr("pool", in_pool=False)
            return []

        # ③ 早盘冲高回落噪音剔除 (M5)
        hi_early = 0.0
        cut = p["noise_window_hhmm"]
        for s in series:
            if _hhmm(s.get("time") or "") > cut:
                break
            try:
                hi_early = max(hi_early, float(s.get("high") or 0))
            except (TypeError, ValueError):
                continue
        if hi_early > 0:
            spike = (hi_early / pc - 1) * 100
            fade = (last / hi_early - 1) * 100
            if spike >= p["noise_spike_pct"] and fade <= -abs(p["noise_fade_pct"]):
                if _tr:
                    _tr("noise", spike=round(spike, 2), fade=round(fade, 2))
                return []
        else:
            spike, fade = 0.0, 0.0

        # 可买入 / 涨幅
        lim_px = round(pc * (1 + _limit_pct(code)), 2)
        if last >= lim_px * _AT_LIMIT:
            if _tr:
                _tr("board", reason="已封死买不进")
            return []
        day_gain = (last / pc - 1) * 100
        if day_gain < p["gain_lo"] * _gain_scale(code):
            if _tr:
                _tr("board", gain=round(day_gain, 2))
            return []

        # ② 时段量比 (M7: 与最近N日同一时刻比)
        vcum = float(series[-1].get("volume") or 0)
        pos = _minute_pos(hhmm)
        if vcum <= 0 or pos < 0:
            if _tr:
                _tr("volume", vol_slot=None, reason="无累计量")
            return []
        hist = _hist_slot_volume(code, day, int(p["vol_slot_days"]), pos)
        if len(hist) < int(p["vol_slot_min_days"]):
            if _tr:
                _tr("volume", vol_slot=None, reason="基准不足")
            return []
        base = sum(hist) / len(hist)
        if base <= 0:
            if _tr:
                _tr("volume", vol_slot=None, reason="基准为0")
            return []
        vslot = vcum / base
        if not (p["vol_slot_lo"] <= vslot <= p["vol_slot_hi"]):
            if _tr:
                _tr("volume", vol_slot=round(vslot, 2))
            return []

        # ⑤ 市值 (数据缺失 fail-open)
        cap = _cap_of(code, day, last)
        if cap > 0 and cap < p["cap_lo"]:
            if _tr:
                _tr("signal", cap_yi=round(cap / 1e8, 1))
            return []

        if _tr:
            _tr("signal", gain=round(day_gain, 2), vol_slot=round(vslot, 2),
                spike=round(spike, 2), fade=round(fade, 2))

        # 展示分 0~100 (按实证方向: 早盘温和放量/未冲高回落/涨幅适中 分越高)
        score = 50.0
        score += 15.0 * max(0.0, 1.0 - abs(vslot - 1.5) / 1.5)      # 时段量比靠近 1.5
        score += 12.0 if fade > -1.0 else 0.0                       # 未冲高回落
        score += 10.0 * min(1.0, day_gain / 6.0)                    # 已有拉升
        score += 8.0 if (mkt_gain is not None and mkt_gain > 0) else 0.0
        score += 5.0 if hi_early <= 0 or spike < p["noise_spike_pct"] else 0.0
        score = max(0.0, min(95.0, score))
        return [Signal(
            code=code,
            time=str(snap.get("time") or "")[:10],
            score=score,
            price=last,
            label=(f"早盘领涨 gain={day_gain:.1f}% 时段量比{vslot:.2f} "
                   f"冲高{spike:.1f}%回落{fade:.1f}%"),
            extra={
                "gain": round(day_gain, 2),
                "vol_slot": round(vslot, 2),
                "vol_slot_base_days": len(hist),
                "spike_pct": round(spike, 2),
                "fade_pct": round(fade, 2),
                "cap_yi": round(cap / 1e8, 1) if cap > 0 else None,
            },
        )]

    # ── 日线前置池 (日度缓存) ──
    def _ensure_pool(self, day: str, p: dict) -> set:
        """用**截至前一交易日收盘**的日线把全市场缩到几百只 (M6)。

        实证: 单因子区分度都弱 (t<1.3), 故这里只做"强势活跃 + 不过热"的范围收敛,
        不声称 alpha。取不到数据时返回空集 ⇒ 调用方自然不放行 (宁缺勿假)。
        """
        if _POOL_CACHE["date"] == day and _POOL_CACHE["set"] is not None:
            return _POOL_CACHE["set"]
        from app.market_cn.auto.core.data.hub import all_codes, daily
        codes = [c for c in (all_codes() or []) if not c.startswith(("8", "4", "92"))]
        keep = set()
        for c in codes:
            try:
                b = daily(c, 80, as_of=day)
            except Exception:
                continue
            if not b or len(b) < 21:
                continue
            cl = [float(x.get("close") or 0) for x in b]
            hi = [float(x.get("high") or 0) for x in b]
            lo = [float(x.get("low") or 0) for x in b]
            vo = [float(x.get("volume") or 0) for x in b]
            c1 = cl[-1]
            if c1 <= 0:
                continue
            ma5 = sum(cl[-5:]) / 5
            ma10 = sum(cl[-10:]) / 10
            ma20 = sum(cl[-20:]) / 20
            if ma20 <= 0:
                continue
            d_ma20 = (c1 / ma20 - 1) * 100
            if not (p["pool_d_ma20_lo"] <= d_ma20 <= p["pool_d_ma20_hi"]):
                continue
            if p["pool_bull"] and not (ma5 > ma10 > ma20):
                continue
            h20, l20 = max(hi[-20:]), min(lo[-20:])
            if l20 <= 0:
                continue
            amp20 = (h20 - l20) / l20 * 100
            if amp20 < p["pool_amp20_lo"]:
                continue
            pos20 = (c1 - l20) / max(h20 - l20, 1e-9) * 100
            if pos20 < p["pool_pos20_lo"]:
                continue
            v5 = [v for v in vo[-5:] if v > 0]
            if not v5 or vo[-1] <= 0:
                continue
            vr1 = vo[-1] / (sum(v5) / len(v5))
            if not (p["pool_vr1_lo"] <= vr1 <= p["pool_vr1_hi"]):
                continue
            if len(cl) >= 61 and cl[-61] > 0:
                ret60 = (c1 / cl[-61] - 1) * 100
                if ret60 > p["pool_ret60_hi"]:
                    continue
            keep.add(c)
        _POOL_CACHE["date"] = day
        _POOL_CACHE["set"] = keep
        return keep

    # ── 出场: T+1 (今天买明天卖) ──
    def intraday_exit(self, bars, code, entry_date, entry_price, entry_idx=None, **params):
        """T+1 出场: close=次日收盘 | open=次日开盘 | limit=次日挂单止盈(未触则收盘)。

        盘中止损优先 (保守: 同一根 bar 内先判止损)。
        """
        p = self.params(params or None)
        mode = str(p.get("exit_mode", "close"))
        lim = float(p.get("exit_limit_pct", 3.0))
        stop = float(p.get("stop_pct", -6.0))
        if entry_price <= 0 or not bars:
            return None
        if entry_idx is None:
            cand = [k for k, b in enumerate(bars) if str(b.get("time"))[:10] == str(entry_date)[:10]]
            if not cand:
                return None
            entry_idx = cand[0]
        j = entry_idx + 1                      # ★ T+1: 只有一天, 不考虑 D+2
        if j >= len(bars):
            return None
        b = bars[j]
        lo = float(b.get("low") or 0)
        op = float(b.get("open") or 0)
        sl_line = entry_price * (1 + stop / 100.0)
        if lo > 0 and lo <= sl_line:
            px = min(op, sl_line) if op > 0 else sl_line
            return {"exit_date": str(b.get("time"))[:10], "exit_price": round(px, 3),
                    "exit_day": 1, "exit_reason": f"止损{stop:.0f}%",
                    "return_pct": round((px / entry_price - 1) * 100, 2)}
        if mode == "open" and op > 0:
            return {"exit_date": str(b.get("time"))[:10], "exit_price": round(op, 3),
                    "exit_day": 1, "exit_reason": "T+1开盘",
                    "return_pct": round((op / entry_price - 1) * 100, 2)}
        if mode == "limit":
            tp = entry_price * (1 + lim / 100.0)
            if op > 0 and op >= tp:
                return {"exit_date": str(b.get("time"))[:10], "exit_price": round(op, 3),
                        "exit_day": 1, "exit_reason": f"开盘止盈+{lim:.0f}%",
                        "return_pct": round((op / entry_price - 1) * 100, 2)}
            hi = float(b.get("high") or 0)
            if hi > 0 and hi >= tp:
                return {"exit_date": str(b.get("time"))[:10], "exit_price": round(tp, 3),
                        "exit_day": 1, "exit_reason": f"触+{lim:.0f}%止盈",
                        "return_pct": round((tp / entry_price - 1) * 100, 2)}
        px = float(b.get("close") or 0)
        if px <= 0:
            return None
        return {"exit_date": str(b.get("time"))[:10], "exit_price": round(px, 3),
                "exit_day": 1, "exit_reason": "T+1收盘",
                "return_pct": round((px / entry_price - 1) * 100, 2)}

    # ── 出场: 实盘/收盘重放 (与 intraday_exit 同判定) ──
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: T+1 到期即卖。盘中模式一律 hold。"""
        p = self.params(params or None)
        mode = str(p.get("exit_mode", "close"))
        lim = float(p.get("exit_limit_pct", 3.0))
        stop = float(p.get("stop_pct", -6.0))
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        held = len(bars) - 1 - int(entry_idx)
        if held < 1:
            # T+1: 买入当日不可卖 (与 intraday_exit 的 d=1 起点对齐)
            return ExitDecision("hold")
        last = bars[-1]
        lo = float(last.get("low") or 0)
        op = float(last.get("open") or 0)
        sl_line = entry_price * (1 + stop / 100.0)
        if lo > 0 and lo <= sl_line:
            px = min(op, sl_line) if op > 0 else sl_line
            return ExitDecision("exit", reason=f"止损{stop:.0f}%", price=px)
        # ★ T+1: held>=1 即出场, 不再等 hold_days
        if mode == "open" and op > 0:
            return ExitDecision("exit", reason="T+1开盘", price=op)
        if mode == "limit":
            tp = entry_price * (1 + lim / 100.0)
            if op > 0 and op >= tp:
                return ExitDecision("exit", reason=f"开盘止盈+{lim:.0f}%", price=op)
            hi = float(last.get("high") or 0)
            if hi > 0 and hi >= tp:
                return ExitDecision("exit", reason=f"触+{lim:.0f}%止盈", price=tp)
        return ExitDecision("exit", reason="T+1收盘", price=float(last.get("close") or 0))
