#!/usr/bin/env python3
"""tail_oversold.py — 尾盘超卖超短策略 (14:56 尾盘买 → D1 开盘卖)

超卖反弹: 当日深跌 + 尾盘适度回落 + 贴近日内低位 + 近5日深度超卖 + 振幅大 → 次日开盘
高概率反弹。D0 14:56~14:57 买入, D1 开盘卖 (出场终审结构最优)。规则来源 test_v2_tail_buy.py。
回测 (2026-09-09~10 终审): N=275 胜率80.7% 均收+2.74%; 50天 56笔/87.5%/+5.28%/PL3.31。

入场五条件 (归一化 nf: 创/科板 0.5, 主板 1.0): ①非ST/非北交所/未封板 ②score>=8
③pre5*nf<=-10 ④amplitude*nf>=10 ⑤tail_ret*nf ∈ [-2.8,-0.5] (14:20~14:40 均价→14:56 现价)。

流程: 14:30 预热 → 14:50 起每分钟滚动预览 (用户提前准备) → 14:56 终审 (等新鲜快照 ≤45s)
→ 15:01 确认持有 → D1 开盘卖。快速判定=两级管线: shortlist 用最新快照必要条件预筛
(score>=8 数学蕴含 day_gain*nf<=-5; 盘中 low 只会更低 → 预览口径是终审超集), 幸存股
(约10~50只) 才拉序列+日线完整判定。规则改动验证: `python -m app.market_cn.auto.backtest`
框架对数 (test_dragon/test_v2 双同步约定已于 09-09 B 阶段对数 PASS 后作废)。

易错点: 快照 open/high/low=当日累计值非分钟bar; bars[-1]=昨日 (1D 盘中未回填);
分子原始价 vs 分母 qfq 日线除权日有偏差 (盘后以 kline_1m 复权口径为准);
tail_ret 需 mi 199~219 槽位 ≥15 个; T+1 当日不可卖。
"""

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "tail_oversold"
STRATEGY_LABEL = "尾盘超卖超短"

PARAMS = {
    "score_min": 8.0,          # V2 评分下限 (归一化后)
    "pre5_max": -10.0,         # pre5_gain*nf 上限 (深度超卖)
    "amp_min": 10.0,           # amplitude*nf 下限 (弹性)
    "tail_lo": -2.8,           # tail_ret*nf 下限
    "tail_hi": -0.5,           # tail_ret*nf 上限 (跌太多=还在崩)
    "min_hhmm": "14:50",       # 快照时间下限 (= 滚动预览窗口起点, 早于此不出信号)
    "daily_limit": 0,          # 0=不截断 (信号本就少, 全部展示)
    "stop_pct": -8.0,          # 止损 % (仅信息展示, T+1 当日不可卖, D1 开盘卖)
    "hold_days": 1,            # 持有1天 (D1开盘卖)
}
_SHORTLIST_SLACK_PCT = 0.15   # 预筛容差(百分点): 吸收原始价/复权价微差, 放宽保超集

# 分钟序列标准化已上收 data/hub.py (prep_minutes, D1), 别名引用保持原名
from app.market_cn.auto.data.hub import prep_minutes as _prep_minutes  # noqa: E402


def _hhmm(s):
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _is_3038(code: str) -> bool:
    return str(code)[:3].startswith(("30", "68"))


def _norm_factor(code: str) -> float:
    """归一化系数: 创/科板×0.5, 主板不变 (与 test_v2_tail_buy.norm_factor 一致)。"""
    return 0.5 if _is_3038(code) else 1.0


def _limit_pct(code: str) -> float:
    return 0.20 if _is_3038(code) else 0.10


def _calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf):
    """V2 评分系统 — 与 test_v2_tail_buy.calc_score 逐分支一致 (勿单独改动)。"""
    score = 0.0
    dg = day_gain * nf
    if dg <= -8:   score += 4.0
    elif dg <= -5: score += 3.0
    elif dg <= -2: score += 1.2
    elif dg <= 0:  score += 0.5
    tr = tail_ret * nf
    if tr <= -2:   score += 3.0
    elif tr <= -1: score += 2.5
    elif tr <= -0.3: score += 1.5
    if pos_range <= 0.2:  score += 2.0
    elif pos_range <= 0.4: score += 1.0
    if amplitude * nf >= 5 and tr <= -0.3:
        score += 1.0
    p5 = pre5_gain * nf
    if p5 <= -10:  score += 0.3
    elif p5 <= -5: score += 0.1
    return round(score, 2)


def _tail_ret_v2(series_rows):
    """V2 尾盘回落 %: 14:56 现价 vs 14:20~14:40 (mi 199~219) 分钟收盘均价。

    快照序列 prep_minutes 差分后按 mi 对齐; 槽位 <15 (21 槽缺口过多) 返回 (None, None)。
    """
    if not series_rows:
        return None, None
    mins = _prep_minutes(
        [{"time": str(r.get("time") or ""), "open": r.get("open") or 0,
          "high": r.get("high") or 0, "low": r.get("low") or 0,
          "close": r.get("last") or 0, "volume": r.get("volume") or 0}
         for r in series_rows], volume_cumulative=True)
    by_mi = {b["mi"]: float(b["c"]) for b in mins if float(b["c"]) > 0}
    tail = [by_mi[mi] for mi in range(199, 220) if mi in by_mi]
    tail_avg = sum(tail) / len(tail) if len(tail) >= 15 else 0
    last_px = by_mi.get(max(by_mi)) if by_mi else 0
    if not last_px or last_px <= 0 or tail_avg <= 0:
        return None, None
    return (last_px / tail_avg - 1) * 100, tail_avg


@register
class TailOversoldStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "v2t"
    scan_spec = ScanSpec(kind="intraday_window", windows=("14:50", "15:00"), interval_sec=60,
                         entry_at="14:56")   # 终审语义: 仅 14:56 成交, 窗口内其余触发=预览
    default_params = dict(PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近)
    PROBE_STAGE_RANK = {"window": 1, "limit": 2, "data": 3, "v2": 4, "signal": 5}
    # 契约: 回测未含 U1~U4 / 14:56 尾盘入场 (T+1) / D1 开盘卖当日平账 / 滚动预览
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    rolling_preview = True
    data_needs = ("daily", "snapshot", "minute_live")

    def intraday_shortlist(self, snaps, mkt_gain, **params):
        """盘中便宜预筛 (必要条件超集, 免拉全市场序列/日线; 推导见文件头)。

        snaps: {code: latest_snapshot_row} → 通过必要条件的 {code: snap}。
        无市场门控 (V2 规则不含大盘条件, mkt_gain 仅记录不拦截)。
        """
        out = {}
        for code, snap in snaps.items():
            if code.startswith(("8", "4", "92")):       # 北交所排除 (v2 回测口径)
                continue
            try:
                last = float(snap.get("last") or 0)
                high = float(snap.get("high") or 0)
                low = float(snap.get("low") or 0)
                pc = float(snap.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or pc <= 0 or high <= 0 or low <= 0 or high <= low:
                continue
            nf = _norm_factor(code)
            # P1: score>=8 ⇒ day_gain*nf<=-5; P2: amp*nf>=10 (盘中只会更差 → 超集);
            # P3: 封板排除 (现价口径, 买不进)
            if (low / pc - 1) * 100 * nf > -5 + _SHORTLIST_SLACK_PCT:
                continue
            if (high - low) / pc * 100 * nf < 10 - _SHORTLIST_SLACK_PCT:
                continue
            if last >= round(pc * (1 + _limit_pct(code)), 2) * 0.998:
                continue
            out[code] = snap
        return out

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """盘中判定 (滚动预览 14:50 起 / 终审 14:56+)。必须 ctx={"latest","series"}。

        probe: 调试探针 (None=零开销) — 门级 TRACE 打点, 存档供 AI 离线分析。"""
        p = self.merged_params(params or None)
        ctx = ctx or {}
        snap, series = ctx.get("latest"), ctx.get("series") or []
        if not snap or not series:
            return []
        last = float(snap.get("last") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        pc = float(snap.get("previousClose") or 0)
        if last <= 0 or pc <= 0 or high <= 0 or low <= 0 or high <= low:
            return []
        _tr = None
        if probe is not None:
            def _tr(stage, **kw):
                probe.trace(stage, code=code, d0_date=str(snap.get("time") or "")[:10],
                            **kw)
        if _hhmm(snap.get("time") or "") < p["min_hhmm"]:    # 预览窗口起点前不出信号
            if _tr:
                _tr("window", hhmm=_hhmm(snap.get("time") or ""))
            return []
        if last >= round(pc * (1 + _limit_pct(code)), 2) * 0.998:   # 封板买不进
            if _tr:
                _tr("limit", last=round(last, 3))
            return []

        nf = _norm_factor(code)
        day_gain = (last / pc - 1) * 100                    # 快照口径: high/low=当日累计极值
        amplitude = (high - low) / pc * 100
        pos_range = (last - low) / (high - low)
        tail_ret, _tail_avg = _tail_ret_v2(series)
        if tail_ret is None:
            if _tr:
                _tr("data", reason="tail_ret")
            return []
        closes = [float(b["close"]) for b in (bars or [])]
        if len(closes) < 6 or closes[-5] <= 0:              # bars[-1]=昨日, 分母=D-5收盘
            if _tr:
                _tr("data", reason="bars_short")
            return []
        pre5_gain = (last / closes[-5] - 1) * 100
        # V2 精掐五条件 (全部归一化)
        score = _calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf)
        if score < p["score_min"] or pre5_gain * nf > p["pre5_max"] \
                or amplitude * nf < p["amp_min"] \
                or not (p["tail_lo"] <= tail_ret * nf <= p["tail_hi"]):
            if _tr:
                _tr("v2", score=round(score, 2), pre5_gain=round(pre5_gain, 2),
                    amplitude=round(amplitude, 2), tail_ret=round(tail_ret, 2),
                    pos_range=round(pos_range, 3))
            return []
        if _tr:
            _tr("signal", score=round(score, 2))
        return [Signal(
            code=code,
            time=str(snap.get("time") or "")[:10],
            score=min(95, int(round(score * 9))),           # 8~10.3 分映射 72~93 展示分
            price=last,
            label=(f"尾盘超卖 gain={day_gain:.1f}% tail={tail_ret:+.2f}% "
                   f"pos={pos_range:.2f} score={score:.1f}"),
            extra={"gain": round(day_gain, 2), "amplitude": round(amplitude, 2),
                   "pos_range": round(pos_range, 3), "tail_ret": round(tail_ret, 2),
                   "pre5_gain": round(pre5_gain, 2)},
        )]

    # ---- 三决策 (与 knife_catch 同生命周期: 14:56 已买 → 隔夜 → D1 开盘卖) ----
    def entry_decision(self, row, snap=None, **params):
        return EntryDecision(True, "尾盘超卖超短 已入场, 无开盘步骤")

    def confirm_decision(self, row, snap=None, **params):
        """D0 收盘确认: 隔夜持有到 D1 开盘卖。"""
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        entry = float(row.get("entry_price") or 0)
        if not series or entry <= 0:
            return None
        last_px = float(series[-1].get("last") or 0)
        d1_chg = round((last_px / entry - 1) * 100, 2) if last_px > 0 else None
        return ConfirmDecision(True, "hold_to_D1_open", d1_chg=d1_chg)

    def exit_decision(self, row, snap=None, **params):
        """出场: D1 开盘卖。live: entry_date < today → 开盘即标记卖出;
        day_close 重放: 按 D1 开盘价出场 (与回测口径一致)。"""
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        mode = snap.get("mode")
        entry_date = str(row.get("entry_date") or "")[:10]
        if mode == "live":
            today = str(snap.get("today") or "")
            if entry_date and today and entry_date < today:
                px = float(snap.get("last") or 0)
                return ExitDecision("exit", reason="D1开盘卖出(超卖反弹兑现)",
                                    price=px if px > 0 else 0)
            return ExitDecision("hold")
        if mode == "day_close":
            bars, entry_idx = snap.get("bars") or [], snap.get("entry_idx")
            if bars and entry_idx is not None and entry_idx + 1 < len(bars):
                d1_bar = bars[entry_idx + 1]
                return ExitDecision("exit", reason="D1开盘卖",
                                    price=float(d1_bar.get("open") or d1_bar.get("close") or 0))
        return ExitDecision("hold")

    def initial_stop(self, code, entry_price):
        return round(entry_price * (1 + self.merged_params()["stop_pct"] / 100), 3)
