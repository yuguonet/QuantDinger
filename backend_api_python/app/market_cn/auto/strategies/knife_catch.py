"""strategies/knife_catch.py — 反向接刀策略 (StrategyBase 插件实现, 14:56 盘中窗口扫描版)

核心逻辑: 大盘下跌日 + 个股深跌 + 尾盘卖盘枯竭(尾盘回升) + 全天压制VWAP下方
          + 收盘贴低点 = 次日高概率反弹 → D0 尾盘(14:56)买入 → D1 开盘卖出

回测口径 (2026-09-08 终审, tmp/_knife_plugin_result.json / _knife_plugin_backtest2.log):
  ⚠️ 时间线是生死线: alpha 在 "D0尾盘→D1开盘" 的隔夜反弹段
    - D0 14:56买→D1开盘卖 (本策略): 门控内 72.2%/+2.06% 两段稳定(73.0/71.3)
    - D1开盘买→D2开盘卖 (旧版, 已废弃): 35%/-2.93% — 晚买一晚 alpha 消失, 任何过滤/排名都救不回
  出场终审: D1开盘卖 = 现实最优 (+2.06%); "挂涨停价未成交尾盘卖"现实口径 -0.61% (弃用)
  宁缺勿滥质量过滤 (各单过滤方向一致, 叠加更强, 门控内 230→107笔):
    lu_recent==0 (81.7 vs 72.2) / down_streak>=2 (80.3) / vol_ratio<=1.5 (77.8) / pre5<=-15 (81.9)
    → 四过滤叠加 87.9%/+4.13%/PL1.16
  ⚠️ regime 集中: 信号集中在恐慌期 (2026-06~09 样本集中在7月), 上线后需持续跟踪
  ⚠️ 截断裁定 (用户 2026-09-08): >3只不截断 — "全拿"73.8%/+2.25% 好于任何Top3排名
    (跌最深/位置最低选出的最容易继续崩); daily_limit=0, 全部展示由用户自行取舍

执行流程:
  14:30  scheduler Task "knife_scan" 启动 (预热窗口, 用户要求不过早占用资源)
  14:56  全市场快照就绪 → intraday_shortlist 便宜预筛(gain/amp/pos/门控)
         → 候选股补拉当日快照序列+日线 → scan_signals 完整判定 → 落库 buy_today
  14:56+ 用户按信号买入 (signal_price = 14:56 最新价)
  15:01  confirm_decision → holding (隔夜持有)
  D1     exit_decision live 模式开盘即标记卖出 (exit_exec_same_day, 当日14:55后平账)

易错点:
  - 必须走 ctx (latest/series/mkt_gain), 无盘中快照时返回空 (回测重放/盘后调用安全)
  - tail_ret = 最新价 vs 20分钟前价 (分钟回测口径 14:36→14:56)
  - vw_frac 用 60s 快照序列近似 1m bar 的 VWAP 上方占比 (Δvolume 累计算 VWAP)
  - 日线 bars[-1] 是昨日 (盘中 1D 未回填), down_streak/pre5/lu_recent 的口径见各函数注释
  - T+1: 14:56 买入当日不可卖, monitor 止损守卫对 entry_at_close 策略跳过当日
"""
from __future__ import annotations

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "knife_catch"
STRATEGY_LABEL = "反向接刀"

PARAMS = {
    "gain_max": -8.0,           # 当日涨幅上限 % (深跌)
    "amp_min": 12.0,            # 当日振幅下限 % (排除窄幅阴跌)
    "pos_max": 0.1,             # 收盘位置上限 (0=最低点, 1=最高点)
    "tail_min": 0.5,            # 尾盘20分钟回升下限 % (卖盘枯竭)
    "vw_max": 0.2,              # 全天 VWAP 上方占比上限 (全天被压制)
    "mkt_gate": -1.0,           # 市场门控: 全市场均涨幅 <= 此值才扫描 (由 ctx.mkt_gain 提供)
    "vol_max": 1.5,             # 量比上限 (14:56量/昨日量; >=1.5 过度恐慌继续崩)
    "pre5_max": -15.0,          # 近5日涨幅上限 % (前期已走弱)
    "streak_min": 2,            # 连跌天数下限 (含当日; 单日深跌的接刀差)
    "daily_limit": 0,           # 0=不截断 (用户裁定: 全拿优于任何Top3排名)
    "stop_pct": -5.0,           # 止损 % (仅 D1 有意义, T+1 当日不可卖)
    "hold_days": 1,             # 持有1天 (D1开盘卖)
}


def _hhmm(s):
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _tail_ret(series_rows, last_px, last_time, minutes=20):
    """尾盘回升 %: 最新价 vs ~minutes 分钟前的最新价 (分钟回测口径 14:36→14:56)。"""
    if not series_rows or last_px <= 0:
        return None
    from datetime import datetime, timedelta
    try:
        t_cut = (datetime.strptime(str(last_time)[:19], "%Y-%m-%d %H:%M:%S")
                 - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    ref = None
    for r in series_rows:
        ts = str(r["time"])[:19]
        if ts <= t_cut:
            ref = r
        else:
            break
    if ref is None:
        return None
    ref_px = float(ref.get("last") or 0)
    if ref_px <= 0:
        return None
    return (last_px / ref_px - 1) * 100


def _vw_frac(series_rows):
    """全天 VWAP 上方占比: 60s 快照近似 1m bar (Δvolume 累计算 running VWAP)。"""
    if not series_rows:
        return None
    cum_pv = cum_v = 0.0
    above = total = 0
    prev_v = 0.0
    for r in series_rows:
        px = float(r.get("last") or 0)
        v = float(r.get("volume") or 0)
        if px <= 0:
            continue
        dv = max(0.0, v - prev_v)
        prev_v = v
        if cum_v > 0:
            total += 1
            if px > cum_pv / cum_v:
                above += 1
        cum_pv += px * dv
        cum_v += dv
    return above / total if total >= 30 else None


def _daily_feats(bars, code):
    """日线特征 (bars[-1]=昨日, 盘中当日 1D 未回填)。"""
    if len(bars) < 8:
        return None
    closes = [float(b["close"]) for b in bars]
    # down_streak 不含当日 (当日下跌已由 gain<0 保证, live 口径 = 1 + 昨日往前连跌数)
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            streak += 1
        else:
            break
    pre5 = (closes[-1] / closes[-5] - 1) * 100 if closes[-5] > 0 else 0
    vol5 = sum(float(b["volume"]) for b in bars[-5:]) / 5
    lu_recent = 0
    from app.market_cn.auto.common.market import get_board_type, is_limit_up
    bt = get_board_type(code)
    for d in range(len(bars) - 1, max(len(bars) - 6, 0), -1):
        cl, pc = closes[d], closes[d - 1]
        if pc > 0 and is_limit_up(cl, pc, bt):
            lu_recent += 1
    return {"down_streak": streak, "pre5": pre5, "vol5": vol5, "lu_recent": lu_recent}


@register
class KnifeCatchStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "kc"
    scan_spec = ScanSpec(kind="intraday_window", windows=("14:30", "15:00"), interval_sec=60)
    default_params = dict(PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近)
    PROBE_STAGE_RANK = {"window": 1, "mkt": 2, "feat": 3, "data": 4, "tail_vw": 5,
                        "daily": 6, "vol": 7, "streak": 8, "pre5": 8,
                        "lu_recent": 9, "signal": 10}
    # 框架契约扩展 (base.py 文档): 回测未含 U1~U4, 不做统一预过滤;
    # 14:56 入场当日不可卖 (T+1); 出场当日执行并当日平账
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    data_needs = ("daily", "snapshot", "minute_live")

    # ---- 盘中便宜预筛 (仅用最新快照, 免拉全市场序列/日线; 阈值唯一来源在本策略) ----
    def intraday_shortlist(self, snaps, mkt_gain, **params):
        """snaps: {code: latest_snapshot_row}; 返回 {code: snap} 通过便宜预筛的候选。"""
        p = self.merged_params(params or None)
        if mkt_gain is None or mkt_gain > p["mkt_gate"]:
            return {}
        out = {}
        for code, snap in snaps.items():
            if code.startswith(("8", "4", "92")):
                continue
            try:
                last = float(snap.get("last") or 0)
                high = float(snap.get("high") or 0)
                low = float(snap.get("low") or 0)
                pc = float(snap.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or pc <= 0 or high <= low:
                continue
            gain = (last / pc - 1) * 100
            amp = (high - low) / pc * 100
            pos = (last - low) / (high - low)
            if gain > p["gain_max"] or amp < p["amp_min"] or pos > p["pos_max"]:
                continue
            out[code] = snap
        return out

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """14:56 盘中判定。必须 ctx={"latest","series","mkt_gain"}; 无盘中数据返回空。

        probe: 调试探针 (None=零开销) — 门级 TRACE 打点, 存档供 AI 离线分析。"""
        p = self.merged_params(params or None)
        ctx = ctx or {}
        snap = ctx.get("latest")
        series = ctx.get("series") or []
        mkt_gain = ctx.get("mkt_gain")
        if not snap or not series:
            return []
        last = float(snap.get("last") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        pc = float(snap.get("previousClose") or 0)
        last_time = str(snap.get("time") or "")
        if last <= 0 or pc <= 0 or high <= low:
            return []
        _tr = None
        if probe is not None:
            def _tr(stage, **kw):
                probe.trace(stage, code=code, d0_date=last_time[:10], **kw)
        # 窗口保护: 14:56 之后才出信号 (用户要求 14:30 启动仅为预热, 判定不变)
        if _hhmm(last_time) < "14:56":
            if _tr:
                _tr("window", hhmm=_hhmm(last_time))
            return []
        # 市场门控 (分钟回测核心条件之一)
        if mkt_gain is None or mkt_gain > p["mkt_gate"]:
            if _tr:
                _tr("mkt", mkt_gain=round(mkt_gain, 2) if mkt_gain is not None else None)
            return []

        gain = (last / pc - 1) * 100
        amp = (high - low) / pc * 100
        pos = (last - low) / (high - low)
        if gain > p["gain_max"] or amp < p["amp_min"] or pos > p["pos_max"]:
            if _tr:
                _tr("feat", gain=round(gain, 2), amp=round(amp, 2), pos=round(pos, 3))
            return []

        tail = _tail_ret(series, last, last_time, minutes=20)
        vw = _vw_frac(series)
        if tail is None or vw is None:
            if _tr:
                _tr("data", reason="tail_or_vw")
            return []
        if tail < p["tail_min"] or vw > p["vw_max"]:
            if _tr:
                _tr("tail_vw", tail=round(tail, 2), vw=round(vw, 3))
            return []

        df = _daily_feats(bars or [], code)
        if df is None:
            if _tr:
                _tr("daily", reason="bars_short")
            return []
        vol_ratio = (float(snap.get("volume") or 0) / df["vol5"]) if df["vol5"] > 0 else 99.0
        if vol_ratio > p["vol_max"]:
            if _tr:
                _tr("vol", vol_ratio=round(vol_ratio, 3))
            return []
        # down_streak 含当日 (当日必跌): live口径 = 1 + 昨日往前连跌
        streak = 1 + df["down_streak"]
        if streak < p["streak_min"]:
            if _tr:
                _tr("streak", streak=streak)
            return []
        if df["pre5"] > p["pre5_max"]:
            if _tr:
                _tr("pre5", pre5=round(df["pre5"], 2))
            return []
        if df["lu_recent"] > 0:
            if _tr:
                _tr("lu_recent", lu_recent=df["lu_recent"])
            return []

        # 评分: 仅作展示排序 (不截断, 全部展示); 连跌深+前期弱+量能适中优先
        score = 60
        if streak >= 3:
            score += 10
        if df["pre5"] <= -20:
            score += 10
        elif df["pre5"] <= -15:
            score += 5
        if 1.0 <= vol_ratio <= 1.5:
            score += 5
        if amp <= 15:
            score += 5
        score = min(90, score)

        trade_date = str(last_time)[:10]
        if _tr:
            _tr("signal", streak=streak, gain=round(gain, 2), tail=round(tail, 2))
        return [Signal(
            code=code,
            time=trade_date,
            score=score,
            price=last,
            label=(f"反向接刀 gain={gain:.1f}% tail=+{tail:.1f}% streak={streak}"),
            extra={
                "gain": round(gain, 2),
                "amplitude": round(amp, 2),
                "pos_range": round(pos, 3),
                "tail_ret": round(tail, 2),
                "vw_frac": round(vw, 3),
                "vol_ratio": round(vol_ratio, 3),
                "down_streak": streak,
                "pre5_gain": round(df["pre5"], 2),
                "lu_recent": df["lu_recent"],
                "mkt_gain": round(mkt_gain, 3) if mkt_gain is not None else None,
            },
        )]

    # ---- 三决策 ----
    def entry_decision(self, row, snap=None, **params):
        """无开盘入场步骤 (14:56 尾盘直接买入, 信号即 buy_today)。兜底可买。"""
        return EntryDecision(True, "kc 尾盘已入场, 无开盘步骤")

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
        """出场: D1 开盘卖 (现实口径终审最优, 见文件头)。

        live 模式: entry_date < today → 开盘即标记卖出 (exit_price=最新价≈开盘);
        day_close 重放: entry_idx+1 存在 → 按 D1 开盘价出场 (与回测 X0 口径一致)。
        """
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        mode = snap.get("mode")
        entry_date = str(row.get("entry_date") or "")[:10]
        if mode == "live":
            today = str(snap.get("today") or "")
            if entry_date and today and entry_date < today:
                px = float(snap.get("last") or 0)
                return ExitDecision("exit", reason="D1开盘卖出(隔夜反弹兑现)",
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
