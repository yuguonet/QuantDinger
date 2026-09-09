#!/usr/bin/env python3
"""strategies/v2tail.py — V2 尾盘超卖买入 (StrategyBase 插件, 14:56 尾盘买 → D1 开盘卖)

核心逻辑: 超卖反弹 — 当日深跌 + 尾盘适度回落 + 贴近日内低位 + 近5日深度超卖
          + 振幅大(弹性足) → 次日开盘高概率反弹 → D0 14:56~14:57 买入 → D1 开盘卖出

规则来源: test_v2_tail_buy.py (V2 精掐规则, 与 test_limitnext.py 同一套评分系统)
  ⚠ 规则改动须两处同步: 本文件 ↔ test_v2_tail_buy.py (同 test_dragon 的手工同步约定)
  回测口径 (2026-09-09~10 终审, 90天/50天全市场):
    N=275(3个月) 胜率80.7% 均收+2.74%; 50天窗口 56笔/87.5%/+5.28%/盈亏比3.31
    出场终审: D1开盘全卖 = 结构最优 (六方向验证, 见 test_v2_tail_buy.py 文件头否决清单)

入场规则 (14:56, 全部归一化: 创/科板 nf=0.5, 主板 nf=1.0):
  ① 非ST/非北交所 (扫描层通用排除), 14:56 未封板 (买不进)
  ② score >= 8   (评分: day_gain 4分/tail_ret 3分/pos_range 2分/组合1分/pre5 0.3分)
  ③ pre5_gain*nf <= -10   (近5日深度超卖; 分子=14:56实价, 分母=D-5收盘)
  ④ amplitude*nf >= 10    (振幅大=弹性足)
  ⑤ tail_ret*nf ∈ [-2.8, -0.5]  (尾盘适度回落: 14:20~14:40均价 → 14:56价)

执行流程 (与 knife_catch 同框架, 增加滚动预览):
  14:30  scheduler Task "knife_scan" 启动 (预热)
  14:50  滚动预览开始: 每分钟一轮 快照预筛→完整判定→落库(幂等更新+落选清理),
         前端自选组实时刷新, 用户提前准备 (rolling_preview=True)
  14:56  终审: 等待 14:56 快照落地 (采集60s一拍, 上限45s) → 最终信号集
  14:56+ 用户按信号买入 (signal_price = 最新快照价)
  15:01  confirm_decision → holding (隔夜持有)
  D1     exit_decision: 开盘即标记卖出 (exit_exec_same_day, 当日14:55后平账)

快速判定原理 (14:56 后秒级出结果的关键):
  两级管线: ① intraday_shortlist 仅用最新快照的必要条件预筛 (免拉全市场序列/日线):
     P1 score>=8 数学上蕴含 day_gain*nf <= -5 (否则满分 1.2+3+2+1+0.3=7.5<8)
        → 必要: (low/pc-1)*100*nf <= -5
     P2 amplitude*nf >= 10 → 必要: (high-low)/pc*100*nf >= 10
     P3 封板排除 (现价 >= 涨停价*0.998 买不进)
     盘中 low 只会更低/振幅只会更大 → 14:50 预览口径天然是 14:56 的超集, 不误杀
   ② 幸存股(全市场约10~50只)才拉当日快照序列+日线 → 完整五条件判定

易错点:
  - 快照 open/high/low 是当日累计值 (非分钟bar), c=last 是分钟级现价;
    tail_ret/pos_range 用 last 与分钟收盘序列, amplitude 用累计高低点 (as-of 正确)
  - 日线 bars[-1] 是昨日 (盘中 1D 未回填): pre5 分母 = closes[-5] (D-5收盘)
  - 分子=快照原始价 vs 分母=qfq日线: 除权除息当日有偏差 (knife 同款已知限度),
    盘后以 kline_1m 复权口径为准
  - tail_ret 需要 mi 199~219 (14:20~14:40) 分钟槽位, 缺口过多返回空 (无法判定)
  - T+1: 14:56 买入当日不可卖, monitor 止损守卫对 entry_at_close 策略跳过当日
"""
from __future__ import annotations

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "v2tail"
STRATEGY_LABEL = "尾盘超卖"

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

# 快照预筛容差 (百分点): 与 test_v2_tail_buy_fast_v4.PRESCREEN_SLACK_PCT 同款,
# 吸收原始价/复权价微差, 方向=放宽条件保超集
_SHORTLIST_SLACK_PCT = 0.15


def _hhmm(s):
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _norm_factor(code: str) -> float:
    """归一化系数: 创/科板×0.5, 主板不变 (与 test_v2_tail_buy.norm_factor 一致)。"""
    c = str(code)[:3]
    return 0.5 if c.startswith("30") or c.startswith("68") else 1.0


def _limit_pct(code: str) -> float:
    c = str(code)[:3]
    return 0.20 if c.startswith("30") or c.startswith("68") else 0.10


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

    pr = pos_range
    if pr <= 0.2:  score += 2.0
    elif pr <= 0.4: score += 1.0

    if amplitude * nf >= 5 and tr <= -0.3:
        score += 1.0

    p5 = pre5_gain * nf
    if p5 <= -10:  score += 0.3
    elif p5 <= -5: score += 0.1
    return round(score, 2)


def _tail_ret_v2(series_rows):
    """V2 尾盘回落 %: 14:56现价 vs 14:20~14:40 (mi 199~219) 分钟收盘均价。

    快照序列 prep_minutes 差分后按 mi 对齐 (与 kline 1m 第199~219根同索引)。
    槽位缺失 >6 个 (约1/3) 视为数据不足, 返回 None。
    返回 (tail_ret, tail_avg) 或 (None, None)。
    """
    if not series_rows:
        return None, None
    from app.market_cn.auto.intraday_core import prep_minutes  # 纯函数, 零IO
    mins = prep_minutes(
        [{"time": str(r.get("time") or ""), "open": r.get("open") or 0,
          "high": r.get("high") or 0, "low": r.get("low") or 0,
          "close": r.get("last") or 0, "volume": r.get("volume") or 0}
         for r in series_rows], volume_cumulative=True)
    by_mi = {b["mi"]: float(b["c"]) for b in mins if float(b["c"]) > 0}
    tail_closes = [by_mi[mi] for mi in range(199, 220) if mi in by_mi]
    if len(tail_closes) < 15:          # 21 槽至少 15 个, 缺口过多无法判定
        return None, None
    tail_avg = sum(tail_closes) / len(tail_closes)
    last_px = by_mi.get(max(by_mi))
    if not last_px or last_px <= 0 or tail_avg <= 0:
        return None, None
    return (last_px / tail_avg - 1) * 100, tail_avg


@register
class V2TailStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "v2t"
    scan_spec = ScanSpec(kind="intraday_window", windows=("14:50", "15:00"), interval_sec=60)
    default_params = dict(PARAMS)
    # 框架契约扩展: 回测未含 U1~U4; 14:56 尾盘入场 (T+1); D1 开盘卖当日平账;
    # 14:50 起每分钟滚动预览 (用户提前准备, 14:56 终审)
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    rolling_preview = True

    # ---- 盘中便宜预筛 (必要条件超集, 免拉全市场序列/日线; 推导见文件头) ----
    def intraday_shortlist(self, snaps, mkt_gain, **params):
        """snaps: {code: latest_snapshot_row}; 返回 {code: snap} 通过必要条件的候选。

        无市场门控 (V2 规则不含大盘条件, mkt_gain 仅记录不拦截)。
        """
        out = {}
        for code, snap in snaps.items():
            if code.startswith(("8", "4", "92")):      # 北交所排除 (v2 回测口径)
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
            # P1: score>=8 ⇒ day_gain*nf<=-5; low 只会更低 → 预览口径是终审超集
            if (low / pc - 1) * 100 * nf > -5 + _SHORTLIST_SLACK_PCT:
                continue
            # P2: amp*nf>=10; 振幅只会更大 → 超集
            if (high - low) / pc * 100 * nf < 10 - _SHORTLIST_SLACK_PCT:
                continue
            # P3: 封板排除 (现价口径)
            limit_price = round(pc * (1 + _limit_pct(code)), 2)
            if last >= limit_price * 0.998:
                continue
            out[code] = snap
        return out

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        """盘中判定 (滚动预览 14:50 起 / 终审 14:56+)。必须 ctx={"latest","series"}。"""
        p = self.merged_params(params or None)
        ctx = ctx or {}
        snap = ctx.get("latest")
        series = ctx.get("series") or []
        if not snap or not series:
            return []
        last = float(snap.get("last") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        pc = float(snap.get("previousClose") or 0)
        last_time = str(snap.get("time") or "")
        if last <= 0 or pc <= 0 or high <= 0 or low <= 0 or high <= low:
            return []
        # 窗口保护: 预览窗口起点前不出信号 (框架契约: 14:30 预热不判定)
        if _hhmm(last_time) < p["min_hhmm"]:
            return []

        nf = _norm_factor(code)
        limit_pct = _limit_pct(code)
        limit_price = round(pc * (1 + limit_pct), 2)

        # 涨停封板排除 (14:56 仍封板 → 买不进)
        if last >= limit_price * 0.998:
            return []

        # --- 当日因子 (快照口径: high/low=当日累计极值, last=现价) ---
        day_gain = (last / pc - 1) * 100
        amplitude = (high - low) / pc * 100
        pos_range = (last - low) / (high - low)

        # --- tail_ret (14:20~14:40 均价 → 现价) ---
        tail_ret, _tail_avg = _tail_ret_v2(series)
        if tail_ret is None:
            return []

        # --- pre5_gain (分子=现价, 分母=D-5收盘 qfq; bars[-1]=昨日) ---
        closes = [float(b["close"]) for b in (bars or [])]
        if len(closes) < 6 or closes[-5] <= 0:
            return []
        pre5_gain = (last / closes[-5] - 1) * 100

        # --- V2 精掐五条件 (全部归一化) ---
        score = _calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf)
        if score < p["score_min"]:
            return []
        if pre5_gain * nf > p["pre5_max"]:
            return []
        if amplitude * nf < p["amp_min"]:
            return []
        tail_n = tail_ret * nf
        if tail_n < p["tail_lo"] or tail_n > p["tail_hi"]:
            return []

        # 展示分: v2 评分 8~10.3 映射到 0~100 (72~93, 分越高=超卖越深)
        disp_score = min(95, int(round(score * 9)))

        trade_date = str(last_time)[:10]
        return [Signal(
            code=code,
            time=trade_date,
            score=disp_score,
            price=last,
            label=(f"尾盘超卖 gain={day_gain:.1f}% tail={tail_ret:+.2f}% "
                   f"pos={pos_range:.2f} score={score:.1f}"),
            extra={
                "gain": round(day_gain, 2),
                "amplitude": round(amplitude, 2),
                "pos_range": round(pos_range, 3),
                "tail_ret": round(tail_ret, 2),
                "pre5_gain": round(pre5_gain, 2),
            },
        )]

    # ---- 三决策 (与 knife_catch 同生命周期: 14:56 已买 → 隔夜 → D1 开盘卖) ----
    def entry_decision(self, row, snap=None, **params):
        return EntryDecision(True, "v2tail 尾盘已入场, 无开盘步骤")

    def confirm_decision(self, row, snap=None, **params):
        """D0 收盘确认: 隔夜持有到 D1 开盘卖。"""
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        entry = float(row.get("entry_price") or 0)
        if entry <= 0:
            return None
        last_px = float(series[-1].get("last") or 0)
        d1_chg = round((last_px / entry - 1) * 100, 2) if last_px > 0 else None
        return ConfirmDecision(True, "hold_to_D1_open", d1_chg=d1_chg)

    def exit_decision(self, row, snap=None, **params):
        """出场: D1 开盘卖 (出场终审结构最优, 见文件头)。

        live 模式: entry_date < today → 开盘即标记卖出 (exit_price=最新价≈开盘);
        day_close 重放: entry_idx+1 存在 → 按 D1 开盘价出场 (与回测口径一致)。
        """
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
            bars = snap.get("bars") or []
            entry_idx = snap.get("entry_idx")
            if bars and entry_idx is not None and entry_idx + 1 < len(bars):
                d1_bar = bars[entry_idx + 1]
                return ExitDecision("exit", reason="D1开盘卖",
                                    price=float(d1_bar.get("open") or d1_bar.get("close") or 0))
        return ExitDecision("hold")

    def initial_stop(self, code, entry_price):
        return round(entry_price * (1 + self.merged_params()["stop_pct"] / 100), 3)
