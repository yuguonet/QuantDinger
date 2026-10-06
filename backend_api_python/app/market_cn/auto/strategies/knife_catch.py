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
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.core.market import get_board_type, is_limit_up
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)
from app.market_cn.auto.slice.contract import (          # slice 契约（递推展示层）
    InsufficientHistory, Progress, Stage, StrategyBase as SliceStrategyBase,
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

_WIN = 8        # 切片窗口 (pre5/vol5 最多用 5，余量给 probe 锚)
_MIN_AGE = 8    # 旧 _daily_feats 口径: len(bars) < 8 → 无特征


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


def _daily_feats(bars, code, market=None):
    """日线特征 (bars[-1]=昨日, 盘中当日 1D 未回填)。

    2026-10-06: 委托**切片** (`KnifeCatchStrategy.init_state` + `_feats_hist`) ——
    特征只有一份实现。历史上本函数是全量重算版，与展示层的递推版并存，
    属"两套实现可给出相反结果"的雷区：改任一侧都要回头对账另一侧。
    """
    try:
        return KnifeCatchStrategy._feats_hist(
            KnifeCatchStrategy.init_state(None, code, bars or [], market))
    except InsufficientHistory:
        return None


# SliceStrategyBase: slice 展示契约 (递推状态机/门)。放在**第二**位 ——
# 生产 StrategyBase 在前, 其 merged_params/scan_signals 等不被 slice 基类遮盖;
# slice 侧只有 params()/init_shared() 等生产基类没有的方法会落到 SliceStrategyBase。
@register
class KnifeCatchStrategy(StrategyBase, SliceStrategyBase):
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
    # slice 展示阶段表（展示层只按此表呈现，不认识门细节）
    stages = (
        Stage("watch", "候选观察", realtime="14:56-15:00", visible=False),
        Stage("ready", "D0尾盘触发·准备", realtime="09:31"),
        Stage("exec", "D1开盘卖出"),
    )
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

    def day_prefilter(self, frame, pc_map):
        """日级必要条件超集 (B 档回测提速; 阈值与 intraday_shortlist 同源)。

        只用 frame.day_extremes() 通用统计推导 shortlist 的必要条件:
          - 板块排除 8/4/92 (shortlist 同口径);
          - pc>0 (shortlist 拒 pc<=0);
          - 日高>日低 (日内全平 → 任意前缀 high<=low 必拒);
          - 日低 <= pc*(1+gain_max) (槽位 last=bar 开盘 ≥ 日低, 深跌槽位存在的必要条件);
          - 日振幅 >= amp_min (前缀振幅 ≤ 日振幅, 前缀达标必须日振幅先达标)。
        pos 上限是槽位特征无法日级化 → 留给 shortlist (超集方向安全)。
        """
        import numpy as np
        p = self.merged_params()
        _dopen, dhigh, dlow = frame.day_extremes()
        pcs = np.asarray([pc_map.get(c) or 0 for c in frame.codes], dtype=float)
        ok = (pcs > 0) & (dhigh > dlow) \
            & (dlow <= pcs * (1.0 + p["gain_max"] / 100.0)) \
            & ((dhigh - dlow) / np.where(pcs > 0, pcs, 1.0) * 100.0 >= p["amp_min"])
        return [c for c, k in zip(frame.codes, ok)
                if k and not c.startswith(("8", "4", "92"))]

    # ══ slice 契约：递推状态机 + 门（**门逻辑唯一实现**）═════════════
    # ⚠ 展示/预处理 evaluate 与盘中实时 scan_signals **共用同一份 _gates**。
    #   两入口只差"取哪个快照": scan_signals 判 ctx["latest"]（此刻）；
    #   evaluate 回扫当日 14:56~15:00 序列取首次触发。公式不得再写第二份。
    def init_state(self, code, bars, market=None):
        """seed: 截至昨日的全量历史 → 切片 (win/streak/lups/age/board)。"""
        if len(bars) < _MIN_AGE:
            raise InsufficientHistory(f"{code}: bars={len(bars)} < {_MIN_AGE}")
        board = get_board_type(code, market)
        win = [{"d": b["time"], "c": float(b["close"]), "v": float(b["volume"])}
               for b in bars[-_WIN:]]
        closes = [float(b["close"]) for b in bars]
        streak = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                streak += 1
            else:
                break
        lups = [1 if is_limit_up(closes[d], closes[d - 1], board, market) else 0
                for d in range(len(closes) - 1, max(len(closes) - 6, 0), -1)]
        lups.reverse()
        return {"v": 1, "date": win[-1]["d"], "age": len(bars), "board": board,
                "win": win, "streak": streak, "lups": lups[-5:]}

    def step(self, state, bar):
        """推进一根 (O(win))。纯函数: 返回新 state, 不改入参。"""
        win = list(state["win"])
        prev_c = win[-1]["c"]
        board = state["board"]
        c = float(bar["close"])
        streak = state["streak"] + 1 if c < prev_c else 0
        lups = list(state["lups"])[-4:] + [1 if is_limit_up(c, prev_c, board) else 0]
        win = (win + [{"d": bar["time"], "c": c, "v": float(bar["volume"])}])[-_WIN:]
        return {"v": 1, "date": bar["time"], "age": state["age"] + 1,
                "board": board, "win": win, "streak": streak, "lups": lups}

    def probe(self, state):
        """除权探针: 窗口首尾 (date, close) —— 历史被复权/订正则不等 ⇒ 整票重建。"""
        w = state["win"]
        return [(w[0]["d"], w[0]["c"]), (w[-1]["d"], w[-1]["c"])]

    def evaluate(self, state, inp, prev):
        """预处理/回测/实时共用: 先结算上一阶段, 再判今日触发或明日观察。"""
        p = self.params()
        events = []
        # 上一阶段结算: D1 开盘卖 (纯日线)
        if prev is not None and prev.stage == "ready" \
                and inp.bar.get("time", "") > prev.date:
            open_px = float(inp.bar.get("open") or 0)
            entry = float(prev.payload.get("price") or 0)
            if open_px > 0:
                events.append(Progress(
                    stage="exec", date=inp.bar["time"],
                    payload={
                        "entry_date": prev.date, "entry_price": entry,
                        "exit_price": open_px,
                        "exit_ret": round((open_px / entry - 1) * 100, 2) if entry > 0 else None,
                        "label": "D1开盘卖出(隔夜反弹兑现)",
                    }, next_realtime=None))
            else:
                return events   # 开盘价缺失(停牌/竞价未出): 保持 prev 不推进

        if inp.code.startswith(("8", "4", "92")):
            return events       # 北交所排除 (旧回测口径, shortlist 同款)
        hist = self._feats_hist(state)
        if hist is None:
            return events
        ctx = inp.ctx or {}
        snap_rows = ctx.get("series") or []
        mkt_gain = ctx.get("mkt_gain")
        fired = None
        for i, row in enumerate(snap_rows):
            hh = _hhmm(row.get("time") or "")
            if hh < "14:56" or hh > "15:00":
                continue
            fired = self._gates(p, hist, row, snap_rows[:i + 1], mkt_gain)
            if fired:
                break
        if fired:
            events.append(Progress(
                stage="ready", date=fired["time"],
                payload={"price": fired["price"], "score": fired["score"],
                         "label": fired["label"], "extra": fired["extra"]},
                next_realtime="09:31"))
            return events

        # 明日观察预筛: 只用历史可判的门 (superset, 宁多勿漏)
        t = self._feats_today(state, inp.bar)
        if t["age"] >= _MIN_AGE and t["down_streak"] >= p["streak_min"] - 1 \
                and t["lu_recent"] == 0:
            events.append(Progress(stage="watch", date=inp.bar.get("time", ""),
                                   payload={}, next_realtime="14:56-15:00"))
        return events

    def _gates(self, p, hist, snap, series, mkt_gain, probe=None):
        """触发门 + 评分 —— **唯一实现** (scan_signals 与 evaluate 共用)。

        probe: 可选门级 TRACE 回调 `probe(stage, **kw)`, 仅 scan_signals 传。
        返回 None=未触发; 否则 {"time","price","score","label","extra"}。
        """
        last = float(snap.get("last") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        pc = float(snap.get("previousClose") or 0)
        last_time = str(snap.get("time") or "")
        if last <= 0 or pc <= 0 or high <= low:
            return None
        # 窗口保护: 14:56 之后才出信号 (14:30 启动仅为预热, 判定不变)
        if _hhmm(last_time) < "14:56":
            if probe:
                probe("window", hhmm=_hhmm(last_time))
            return None
        if mkt_gain is None or mkt_gain > p["mkt_gate"]:
            if probe:
                probe("mkt", mkt_gain=round(mkt_gain, 2) if mkt_gain is not None else None)
            return None
        gain = (last / pc - 1) * 100
        amp = (high - low) / pc * 100
        pos = (last - low) / (high - low)
        if gain > p["gain_max"] or amp < p["amp_min"] or pos > p["pos_max"]:
            if probe:
                probe("feat", gain=round(gain, 2), amp=round(amp, 2), pos=round(pos, 3))
            return None
        tail = _tail_ret(series, last, last_time, minutes=20)
        vw = _vw_frac(series)
        if tail is None or vw is None:
            if probe:
                probe("data", reason="tail_or_vw")
            return None
        if tail < p["tail_min"] or vw > p["vw_max"]:
            if probe:
                probe("tail_vw", tail=round(tail, 2), vw=round(vw, 3))
            return None
        # 日线特征 (bars 不足 ⇒ 无特征; 位置与旧实现一致)
        if hist is None:
            if probe:
                probe("daily", reason="bars_short")
            return None
        vol_ratio = (float(snap.get("volume") or 0) / hist["vol5"]) if hist["vol5"] > 0 else 99.0
        if vol_ratio > p["vol_max"]:
            if probe:
                probe("vol", vol_ratio=round(vol_ratio, 3))
            return None
        # down_streak 含当日 (当日必跌): live口径 = 1 + 昨日往前连跌
        streak = 1 + hist["down_streak"]
        if streak < p["streak_min"]:
            if probe:
                probe("streak", streak=streak)
            return None
        if hist["pre5"] > p["pre5_max"]:
            if probe:
                probe("pre5", pre5=round(hist["pre5"], 2))
            return None
        if hist["lu_recent"] > 0:
            if probe:
                probe("lu_recent", lu_recent=hist["lu_recent"])
            return None

        # 评分: 仅作展示排序 (不截断); 连跌深+前期弱+量能适中优先
        score = 60
        if streak >= 3:
            score += 10
        if hist["pre5"] <= -20:
            score += 10
        elif hist["pre5"] <= -15:
            score += 5
        if 1.0 <= vol_ratio <= 1.5:
            score += 5
        if amp <= 15:
            score += 5
        score = min(90, score)
        if probe:
            probe("signal", streak=streak, gain=round(gain, 2), tail=round(tail, 2))
        return {
            "time": last_time[:10], "price": last, "score": score,
            "label": f"反向接刀 gain={gain:.1f}% tail=+{tail:.1f}% streak={streak}",
            "extra": {
                "gain": round(gain, 2), "amplitude": round(amp, 2),
                "pos_range": round(pos, 3), "tail_ret": round(tail, 2),
                "vw_frac": round(vw, 3), "vol_ratio": round(vol_ratio, 3),
                "down_streak": streak, "pre5_gain": round(hist["pre5"], 2),
                "lu_recent": hist["lu_recent"],
                "mkt_gain": round(mkt_gain, 3) if mkt_gain is not None else None,
            },
        }

    @staticmethod
    def _feats_hist(state):
        """截至切片日 (= 昨日) 的特征 —— 与旧 _daily_feats 逐字段对齐。"""
        if state["age"] < _MIN_AGE:
            return None
        win = state["win"]
        closes = [w["c"] for w in win]
        pre5 = (closes[-1] / closes[-5] - 1) * 100 if closes[-5] > 0 else 0
        vol5 = sum(w["v"] for w in win[-5:]) / 5
        return {"down_streak": state["streak"], "pre5": pre5,
                "vol5": vol5, "lu_recent": int(sum(state["lups"]))}

    @staticmethod
    def _feats_today(state, bar):
        """截至今日 (含 bar) —— 仅供明日 watch 预筛 (超集方向)。"""
        c = float(bar.get("close") or 0)
        pclose = state["win"][-1]["c"]
        board = state["board"]
        streak = state["streak"] + 1 if (c > 0 and c < pclose) else 0
        lups = (list(state["lups"])[1:]
                + [1 if (c > 0 and is_limit_up(c, pclose, board)) else 0])
        return {"down_streak": streak, "lu_recent": int(sum(lups)),
                "age": state["age"] + 1}

    def realtime_shortlist(self, codes, snaps, mkt_gain=None, stage=None):
        """实时旁支便宜预筛 (与 scan_signals 门控同源, 只取快照场)。"""
        if stage not in (None, "watch"):
            return list(codes)      # 阶段转换票 (exec 结算) 不得被触发门拦截
        p = self.params()
        if mkt_gain is None or mkt_gain > p["mkt_gate"]:
            return []
        out = []
        for code in codes:
            if code.startswith(("8", "4", "92")):
                continue
            snap = snaps.get(code) or {}
            last = float(snap.get("last") or 0)
            high = float(snap.get("high") or 0)
            low = float(snap.get("low") or 0)
            pc = float(snap.get("previousClose") or 0)
            if last <= 0 or pc <= 0 or high <= low:
                continue
            gain = (last / pc - 1) * 100
            amp = (high - low) / pc * 100
            pos = (last - low) / (high - low)
            if gain > p["gain_max"] or amp < p["amp_min"] or pos > p["pos_max"]:
                continue
            out.append(code)
        return out

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """14:56 盘中判定 —— 判**此刻** (ctx["latest"])。

        门逻辑全部委托 `_gates` (与展示层 evaluate 同一实现), 本方法只负责:
        取快照 → 建切片 → 组装 Signal。probe: 门级 TRACE, 仅本入口传。
        """
        p = self.merged_params(params or None)
        ctx = ctx or {}
        snap = ctx.get("latest")
        series = ctx.get("series") or []
        if not snap or not series:
            return []
        _tr = None
        if probe is not None:
            def _tr(stage, **kw):
                probe.trace(stage, code=code,
                            d0_date=str(snap.get("time") or "")[:10], **kw)
        try:
            hist = self._feats_hist(self.init_state(code, bars or []))
        except InsufficientHistory:
            hist = None
        fired = self._gates(p, hist, snap, series, ctx.get("mkt_gain"), probe=_tr)
        if not fired:
            return []
        return [Signal(code=code, time=fired["time"], score=fired["score"],
                       price=fired["price"], label=fired["label"],
                       extra=fired["extra"])]

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

        live 模式: entry_date < today → 开盘即标记卖出;
        day_close 重放: entry_idx+1 存在 → 按 D1 开盘价出场 (与回测 X0 口径一致)。

        ⚠️ 记账价必须取 `snap["open"]` 而不是 `snap["last"]` (2026-10-04 A3 资金红线):
          本策略是 `entry_at_close` + `exit_exec_same_day` 超短 (D0 14:56买 → D1 开盘卖),
          **整个 alpha 就是隔夜跳空一两个点**; 而 monitor step2 的 live 出场窗起点是
          09:35 (monitor.W_OPEN_HI), 若取 `last` 则记账价 = 09:35 后首拍最新价 ≠ 开盘价,
          对超短策略是系统性偏离。day_close 重放分支取的是 `d1_bar["open"]`,
          两口径必须对齐, 否则实盘与回测逐笔对不上。
          `last` 只作 `open` 缺失时的退路 (停牌/集合竞价未出)。
        """
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        mode = snap.get("mode")
        entry_date = str(row.get("entry_date") or "")[:10]
        if mode == "live":
            today = str(snap.get("today") or "")
            if entry_date and today and entry_date < today:
                px = float(snap.get("open") or snap.get("last") or 0)
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


# ================================================================
# 门表 DSL 私有函数 (2026-09-26 P1-6: 消灭「逐字镜像」, 统一调上方实现)
# ================================================================

def _kc_hhmm(s) -> str:
    return _hhmm(s)


def _kc_tail_ret(series_rows, last_px, last_time, minutes=20):
    return _tail_ret(series_rows, last_px, last_time, minutes)


def _kc_vw_frac(series_rows):
    return _vw_frac(series_rows)


def _kc_daily_feats(bars, code, market=None):
    return _daily_feats(bars, code, market)


_KC_NAN_KEYS = ("gain", "amp", "pos", "tail", "vw", "vol_ratio", "streak",
                "pre5", "lu_recent")


def _kc_cache(ctx: Ctx) -> dict:
    """knife 盘中判定中间量（每 Ctx 记忆化）。覆盖 scan_signals + 3 个数据助手。"""
    cache = ctx.__dict__.get("_kc_cache")
    if cache is not None:
        return cache
    cache = {k: float("nan") for k in _KC_NAN_KEYS}
    snap = ctx.latest or {}
    last = Ctx._f(snap, "last")
    high = Ctx._f(snap, "high")
    low = Ctx._f(snap, "low")
    pc = Ctx._f(snap, "previousClose")
    last_time = str(snap.get("time") or "")
    cache["hhmm"] = _kc_hhmm(last_time)
    if last > 0 and pc > 0 and high > low:
        cache["gain"] = (last / pc - 1) * 100
        cache["amp"] = (high - low) / pc * 100
        cache["pos"] = (last - low) / (high - low)
        tail = _kc_tail_ret(ctx.series or [], last, last_time, 20)
        vw = _kc_vw_frac(ctx.series or [])
        cache["tail"] = float("nan") if tail is None else tail
        cache["vw"] = float("nan") if vw is None else vw
        df = _kc_daily_feats(ctx.bars or [], ctx.code, ctx.market)
        if df is not None:
            cache["vol_ratio"] = (Ctx._f(snap, "volume") / df["vol5"]) if df["vol5"] > 0 else 99.0
            cache["streak"] = 1 + df["down_streak"]
            cache["pre5"] = df["pre5"]
            cache["lu_recent"] = df["lu_recent"]
    ctx.__dict__["_kc_cache"] = cache
    return cache


def kc_metric(ctx: Ctx, name: str) -> float:
    """knife 特征取值（缺少/无效 → nan，数值门自然失败）。"""
    return float(_kc_cache(ctx).get(name, float("nan")))


def kc_hhmm(ctx: Ctx) -> str:
    """触发槽位的 HH:MM（'' = 时间串非法）。"""
    return _kc_cache(ctx).get("hhmm", "")


def kc_mkt_gain(ctx: Ctx) -> float:
    """全市场均涨幅%（ctx.mkt_gain；None → nan → 市场门失败，镜像参考版 mkt_gain is None）。"""
    return float("nan") if ctx.mkt_gain is None else float(ctx.mkt_gain)

register_strategy_funcs(
    'knife_catch',
    {"feat": kc_metric, "hhmm": kc_hhmm, "mkt_gain": kc_mkt_gain},
)
