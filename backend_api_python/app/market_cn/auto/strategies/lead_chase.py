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
    "min_hhmm": "09:35",        # 早于此时噪音未散 (9:30~9:40 冲高回落最多)
    "max_hhmm": "10:00",        # M5: 早盘涨停优于下午
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
    "mkt_gate": 0.0,            # 大盘当日涨幅下限 %
    "cap_lo": 20e8,             # 流通市值下限(元); 数据缺失 fail-open
    # ── 门控 / 生命周期 ──
    "daily_limit": 10,
    # ── ⑥ 出场 (M2/M4: T+1, 默认次日收盘卖) ──
    "exit_mode": "close",       # close=次日收盘 | open=次日开盘 | limit=挂单止盈
    "exit_limit_pct": 3.0,      # exit_mode=limit 时的挂单止盈 %
    "stop_pct": -6.0,           # 次日盘中硬止损 %
    # ── ⑦ v7 日内均线规则 (2026-09-28 实证, analysis_output/领涨追击_重设计_v7) ──
    # 只做 T+1; 09:40 口径; 推荐切点 前40%~60% (beta≈1, alpha t 最高)
    "rule_mode": "v7_dev40",   # off=旧规则 | v7_dev40=日内均线截面
    "dev40_hhmm": "09:40",     # 取分时均线截止时刻
    "dev40_pctile": 40.0,      # 同日截面取前 N% (按偏离从高到低)
    "zt20_max": 0,             # 近20日涨停次数上限 (0=不许有)
    "fade_max": 1.0,           # 早盘冲高回撤上限 %
    "amt_lo": 1e8,             # 成交额下限 (元, 可选流动性)
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
        for r in series or []:
            ts = str(r.get("time") or "")
            if len(ts) >= 16:
                hm = ts[11:16]
            else:
                hm = ""
            if hm < "09:31" or hm > cut:
                # 仍取 09:40 前最后一笔 last
                if hm and hm <= cut:
                    last_px = float(r.get("last") or 0) or last_px
                continue
            last_px = float(r.get("last") or 0) or last_px
            v = float(r.get("volume") or 0)
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
    try:
        rows = minute_1m(code, start=end_day, end=end_day)
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
    scan_spec = ScanSpec(kind="intraday_window", windows=("09:35", "10:00"), interval_sec=60)
    default_params = dict(PARAMS)
    PROBE_STAGE_RANK = {"window": 1, "mkt": 2, "pool": 3, "noise": 4,
                        "volume": 5, "board": 6, "signal": 7}
    # 框架契约: 回测未含 U1~U4 (自实现门); 盘中即买 → T+1 当日不可卖
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    data_needs = ("daily", "snapshot", "minute_live")

    # ── 第一段: 全市场快照便宜预筛 (时间窗 + 可买入 + 涨幅 + 早盘噪音) ──
    def intraday_shortlist(self, snaps, mkt_gain, **params):
        p = self.merged_params(params or None)
        if mkt_gain is not None and mkt_gain < p["mkt_gate"]:
            return {}
        hhmm = ""
        for snap in snaps.values():
            hhmm = _hhmm(snap.get("time") or "")
            break
        if not (p["min_hhmm"] <= hhmm <= p["max_hhmm"]):
            return {}
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
            if (last / pc - 1) * 100 < p["gain_lo"] * _gain_scale(code):
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
        p = self.merged_params(params or None)
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
            # 近20日涨停 (bars 末根 = T-1, 避免用今日未完行情) —— 口径统一走 _zt_count
            try:
                zt = _zt_count(bars, code, 20)
                if zt > int(p.get("zt20_max", 0)):
                    if _tr:
                        _tr("signal", zt20=zt)
                    return []
            except Exception:
                pass
            # 流动性 (可选)
            try:
                amt = last * float(snap.get("volume") or 0)
                if float(p.get("amt_lo", 0) or 0) > 0 and amt > 0 and amt < float(p["amt_lo"]):
                    if _tr:
                        _tr("signal", amt=amt)
                    return []
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
        p = self.merged_params(params or None)
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
        p = self.merged_params(params or None)
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
