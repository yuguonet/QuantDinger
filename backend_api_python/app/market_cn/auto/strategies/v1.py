"""strategies/v1.py — V1 追板策略 (StrategyBase 插件实现, Phase 2 迁移)

实现已迁移至本文件; core.v1_today_d0_signals 为 facade 转发到这里。

入场 (D0 盘后扫描 → D1 竞价):
  D0 四因子: 涨停(0.98x阈值) + 20日涨>30% + D-1回调[-10%,-3%) + OBV 5日上升 + D-1非放量(<1.5x 5日均量)
  因子5: 纯单板过热过滤 (前10天无涨停时: MACD柱<2 且 布林带宽<45%, 剔除过热)
  竞价: main: gap>=-3% 且非[3%,5%); gem: -5%<=gap<5% (monitor._gap_buyable v1 分支)
  U1~U4: prefilter_anchor='signal' (D0 即涨停日, 锚定信号日评估)

出场 (收盘重放, monitor v1 分支):
  止损-10% / 追踪-5%(自入场日峰值, held>1) / 到期7天

易错点:
  - D-1回调区间是 [-10%, -3%) 左闭右开; D0 涨停判定是 0.98x 板块阈值 (近似涨停);
  - 回测 strategy_v1 的 D1 过滤含 d1_change<0 (收盘), 属回测引擎 D1 口径, 不在 entry_decision;
  - OBV 从 i-20 起累计且 j=0 不计 — 勿"优化"起始点, 会改变边界信号。
"""
from __future__ import annotations

from app.market_cn.auto.common.indicators import calc_bollinger_bw, calc_macd
from app.market_cn.auto.common.market import get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "v1"
STRATEGY_LABEL = "V1"

PARAMS = {
    "ret_20d_min": 30.0,
    "d_1_pullback_min": -10.0,
    "d_1_pullback_max": -3.0,
    "obv_filter": True,
    "d_1_vol_max": 1.5,
    # 出场 (monitor v1 分支)
    "stop": -10.0,
    "trail": -5.0,
    "hold": 7,
    # 竞价 gap (monitor._gap_buyable v1 分支)
    "min_gap_main": -3.0,
    "min_gap_gem": -5.0,
    "gem_gap_max": 5.0,
    "main_gap_band_lo": 3.0,     # 主板高开 3~5% 不入场 (v4数据驱动)
    "main_gap_band_hi": 5.0,
}


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 v1_today_d0_signals 的 dict 形态 (facade 兼容层)。"""
    ex = sig.extra or {}
    return {
        "code": code,
        "board": get_board_name(code),
        "path": "v1",
        "path_label": "V1",
        "d0_date": sig.time,
        "d0_close": ex.get("d0_close"),
        "ret_20d": ex.get("ret_20d"),
        "d_1_change": ex.get("d_1_change"),
        "turnover_anchor": ex.get("turnover_anchor"),
        "turnover_anchor_total": ex.get("turnover_anchor_total"),
        "buy_mode": "next_open",
    }


@register
class V1Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"        # D0 即涨停日, U1~U4 锚定信号日评估
    entry_style = "v1"
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近)
    PROBE_STAGE_RANK = {"lu": 1, "ret20": 2, "pullback": 3, "obv": 4, "vol": 5,
                        "overheat": 6, "prefilter": 7, "d1_gap": 8, "d1_chg": 8,
                        "d1_band": 8, "engine_skip": 9, "signal": 10}

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """D0 四因子 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1], 末根为 D0。

        probe: 调试探针 (probe.Probe / DayTrace shim), None=零开销 —
        各过滤门 TRACE 式打点, 存档供 AI 离线分析, 与判定行为无关。"""
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 26:
            return result
        i = n - 1
        if i < 2:
            return result
        board_type = get_board_type(code)
        threshold = 0.098 if board_type == "main" else 0.198
        d0 = bars[i]
        d_1 = bars[i - 1]
        d_2 = bars[i - 2]
        if d_2["close"] <= 0 or d_1["close"] <= 0:
            return result
        # 探针 shim (TRACE 宏语义): probe=None 时零开销
        if probe is not None:
            _pd = str(d0["time"])[:10]

            def _tr(stage, **kw):
                probe.trace(stage, code=code, d0_date=_pd, **kw)
        else:
            _tr = None

        if (d0["close"] / d_1["close"] - 1) < threshold * 0.98:
            if _tr:
                _tr("lu", d0_pct_chg=round((d0["close"] / d_1["close"] - 1) * 100, 2))
            return result

        # === 因子1: 强趋势 20日涨>ret_20d_min% ===
        if i < 20 or bars[i - 20]["close"] <= 0:
            return result
        ret_20d = (d0["close"] / bars[i - 20]["close"] - 1) * 100
        if ret_20d < p["ret_20d_min"]:
            if _tr:
                _tr("ret20", ret_20d=round(ret_20d, 2))
            return result

        # === 因子2: D-1回调 [d_1_pullback_min, d_1_pullback_max) ===
        d_1_change = (d_1["close"] / d_2["close"] - 1) * 100
        if d_1_change < p["d_1_pullback_min"] or d_1_change >= p["d_1_pullback_max"]:
            if _tr:
                _tr("pullback", d_1_change=round(d_1_change, 2))
            return result

        # === 因子3: OBV 5日趋势上升 ===
        if p["obv_filter"]:
            obv = 0
            obv_list = []
            for j in range(max(0, i - 20), i + 1):
                if j > 0:
                    if bars[j]["close"] > bars[j - 1]["close"]:
                        obv += bars[j]["volume"]
                    elif bars[j]["close"] < bars[j - 1]["close"]:
                        obv -= bars[j]["volume"]
                obv_list.append(obv)
            if len(obv_list) >= 5 and obv_list[-1] - obv_list[-5] <= 0:
                if _tr:
                    _tr("obv")
                return result

        # === 因子4: D-1非放量 < d_1_vol_max x 5日均量 ===
        if i >= 6:
            vol_ma5_d1 = sum(bars[j]["volume"] for j in range(i - 6, i - 1)) / 5
            if vol_ma5_d1 > 0 and d_1["volume"] / vol_ma5_d1 >= p["d_1_vol_max"]:
                if _tr:
                    _tr("vol", vol_r=round(d_1["volume"] / vol_ma5_d1, 2))
                return result

        # === 因子5: 纯单板过热过滤 (仅当前10天无涨停时生效) ===
        has_recent_lu = False
        for j in range(max(1, i - 10), i):
            if j >= 1 and is_limit_up(bars[j]["close"], bars[j - 1]["close"], board_type):
                has_recent_lu = True
                break
        if not has_recent_lu:
            closes = [bars[j]["close"] for j in range(i + 1)]
            _, _, hist = calc_macd(closes)
            macd_h = hist[-1] if hist else None
            boll_bw = calc_bollinger_bw(closes)
            if macd_h is not None and macd_h >= 2:
                if _tr:
                    _tr("overheat", reason="macd_hist", macd_h=round(macd_h, 2))
                return result
            if boll_bw is not None and boll_bw >= 45:
                if _tr:
                    _tr("overheat", reason="boll_bw", boll_bw=round(boll_bw, 2))
                return result

        circ = float((params.get("stock_info") or {}).get("circ_shares") or 0)
        total = float((params.get("stock_info") or {}).get("total_shares") or 0)
        d0_close = round(d0["close"], 3)
        if _tr:
            _tr("signal", ret_20d=round(ret_20d, 2), d_1_change=round(d_1_change, 2))
        result.append(Signal(
            code=code,
            time=d0["time"],
            score=int(min(99, max(0, ret_20d))),
            price=d0_close,
            label="V1",
            extra={
                "path": "v1",
                "path_label": "V1",
                "d0_date": d0["time"],
                "d0_close": d0_close,
                "ret_20d": round(ret_20d, 2),
                "d_1_change": round(d_1_change, 2),
                "turnover_anchor": round(d0["volume"] / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(d0["volume"] / total * 100, 2) if total > 0 else None,
                "buy_mode": "next_open",
            },
        ))
        return result

    # ---- D1 竞价处置 (monitor ~09:25) ----
    def entry_decision(self, row, snap=None, **params):
        """v1 竞价规则: main: gap>=-3% 且非[3%,5%); gem: -5%<=gap<5%。

        gap = (open/prev_close-1)*100, prev_close 取快照 previousClose 兜底 signal_price。
        """
        p = self.merged_params(params or None)
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        gem = get_board_type(row.get("code", "")) == "gem_star"
        if gem:
            ok = p["min_gap_gem"] <= gap < p["gem_gap_max"]
        else:
            ok = gap >= p["min_gap_main"] and not (p["main_gap_band_lo"] <= gap < p["main_gap_band_hi"])
        if ok:
            return EntryDecision(True, f"gap={gap:.2f}% 可买")
        return EntryDecision(False, f"gap={gap:.2f}% 越界")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """v1 日内动量确认: d1_chg<0 或 日内动量(entry_gap 后)<3% → weak (不确认)。

        snap={"series":[...当日快照序列]}; d1_chg 按 signal_price 基准, d1_vol_r=日内动量
        (旧 evaluate_confirm 口径)。返回 None = 无法判定, monitor 不转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        last_r = series[-1]
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            return None
        d1_chg = (float(last_r["last"] or 0) / prev_close - 1) * 100
        entry_gap = float((row.get("extra") or {}).get("entry_gap") or 0)
        intraday = d1_chg - entry_gap
        if d1_chg < 0 or intraday < 3.0:
            return ConfirmDecision(False, "D1日内动量<3%,D2开盘清仓",
                                   d1_chg=round(d1_chg, 2), detail={"confirm": "weak"})
        return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                               d1_vol_r=round(intraday, 2),
                               detail={"confirm": "ok", "confirm_strong": False})

    def quality_key(self, row):
        """V1 质量排序: 20日涨幅越大越优先。"""
        extra = row.get("extra") or {}
        return (extra.get("ret_20d") or 0,)

    def initial_stop(self, code, entry_price):
        """-10% (板块不分档)。"""
        return round(entry_price * (1 - 10.0 / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: 止损-10 / 追踪-5(自入场日峰值, held>1) / 到期7天。

        snap={"mode":"day_close","bars":[...],"entry_idx":int}; live 模式无特殊规则 → hold
        (盘中硬止损兜底在 monitor 主循环)。"""
        p = self.merged_params(params or None)
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        stop, trail, hold = p["stop"], p["trail"], p["hold"]
        today_idx = len(bars) - 1
        held = today_idx - entry_idx + 1
        entry_seg = bars[entry_idx:today_idx + 1]
        peak = max(float(b["high"]) for b in entry_seg)
        last_bar = bars[-1]
        if last_bar["low"] <= entry_price * (1 + stop / 100):
            return ExitDecision("exit", reason=f"止损{stop}%", price=entry_price * (1 + stop / 100))
        if held > 1 and last_bar["low"] <= peak * (1 + trail / 100):
            return ExitDecision("exit", reason=f"追踪止损{trail}%", price=peak * (1 + trail / 100))
        if held >= hold:
            return ExitDecision("exit", reason=f"持仓到期{hold}天", price=float(last_bar["close"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_v1_stock 逐字搬入, 对数零差异) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                      probe=None):
        """单股 V1 全历史回测 (D0四因子判定, 次日开盘买, D1入场过滤)。

        D1 过滤 (gap/change/高开区间) 属回测引擎 D1 口径, 不在 entry_decision — 勿合并;
        出场模拟 _run_backtest 在本文件 (策略专用出场规则, 2026-09-10 晚下沉)。
        """
        from app.market_cn.auto.common.filters import unified_prefilter
        from app.market_cn.auto.probe import DayTrace
        board_type = get_board_type(code)
        n = len(bars)
        if n < 30:
            return []
        trades = []

        for i in range(25, n - 1):
            # debug 模式: day_tr 聚合该日判定门落点 (probe=None 零开销)
            day_tr = DayTrace() if probe is not None else None
            # 逐日候选判定: 与实盘 scan 完全同一函数 (切片 as_of 语义; 经 facade 等价路径)
            sigs = [_signal_to_legacy_dict(s, code) for s in self.scan_signals(
                bars[:i + 1], code,
                ret_20d_min=30.0, d_1_pullback_min=-10.0, d_1_pullback_max=-3.0,
                obv_filter=True, d_1_vol_max=1.5, stock_info=stock_info,
                probe=day_tr)]
            if not sigs:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info)
                continue
            sig = sigs[0]

            # U1~U4 (信号日D0收盘可知; 20日涨幅>=30%已隐含U4)
            if use_prefilter:
                ok, fails = unified_prefilter(bars, i, code, stock_info)
                if not ok:
                    if probe is not None:
                        self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                        stage="prefilter", sig=sig, u_fails=fails)
                    continue

            # 入场: 次日开盘价 + D1当日过滤
            d0 = bars[i]
            d1 = bars[i + 1]
            entry_price = d1["open"]
            if entry_price <= 0:
                continue
            entry_idx = i + 1
            entry_date = d1["time"]
            d1_change = (d1["close"] / d0["close"] - 1) * 100
            d1_gap = (d1["open"] / d0["close"] - 1) * 100
            min_d1_gap = -3.0 if board_type == "main" else -5.0
            if d1_gap < min_d1_gap:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="d1_gap", sig=sig,
                                    extra={"d1_gap": round(d1_gap, 2)})
                continue
            if d1_change < 0:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="d1_chg", sig=sig,
                                    extra={"d1_change": round(d1_change, 2)})
                continue
            if board_type == "gem_star" and d1_gap >= 5.0:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="d1_band", sig=sig,
                                    extra={"d1_gap": round(d1_gap, 2), "board": "gem_star"})
                continue
            # 主板高开3%~5%不入场 (v4数据驱动)
            if board_type == "main" and 3.0 <= d1_gap < 5.0:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="d1_band", sig=sig,
                                    extra={"d1_gap": round(d1_gap, 2), "board": "main"})
                continue

            d1_limit_up_val = is_limit_up(d1["close"], d0["close"], board_type)
            bt = _run_backtest(bars, entry_idx, entry_price, 7, -10.0,
                              -5.0, board_type, is_v1=True,
                              d1_limit_up=d1_limit_up_val, d1_change=d1_change,
                              d1_gap=d1_gap)
            if not bt:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="engine_skip", sig=sig)
                continue

            if probe is not None:
                self._probe_day(
                    probe, day_tr, bars, i, code, stock_info, stage="signal",
                    sig=sig, extra={"engine": {k: bt.get(k) for k in
                                               ("return_pct", "peak_return_pct",
                                                "exit_reason", "exit_day")}})
            trades.append({
                **sig,
                "entry_date": entry_date,
                "entry_price": round(entry_price, 3),
                "buy_mode": "next_open",
                "d1_change": round(d1_change, 2),
                "d1_gap": round(d1_gap, 2),
                "intraday": round(d1_change - d1_gap, 2),
                **bt,
            })

        return trades


# ================================================================
# 出场模拟 (2026-09-10 晚自 backtest.py 下沉回归本文件 — 出场规则是策略专用,
# 通用流水线不承载策略专属出场; 逐字搬运, 回归以三策略对数验证)
# ================================================================
from app.market_cn.auto.common.exec_cn import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)


def _run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None):
    """V1/通用出场模拟 (现实化 2026-09-09, 与 test_dragon.py 逐字同步):

    现实约束: ① T+1 — 买入当日(d=1)不可卖出, 全部出场判定从 d=2 起 (仅更新峰值/估值);
    ② 跳空穿越 — 触发日开盘低于触发价按开盘价成交;
    ③ 跌停无法卖出 — 一字跌停整日跳过 (V1 的 D2 开盘清仓若遇一字跌停顺延次日开盘),
    触发成交触及跌停顺延次日开盘; 到期日一字跌停顺延次日开盘强平。
    V1 日内动量规则 (D1收盘判定→D2开盘执行) 本就满足 T+1, 判定逻辑未改动。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False        # 触发成交触及跌停 / 清仓日一字跌停 → 次日开盘强平
    last_unfilled = False     # 末日一字跌停 → 到期顺延

    # 如果外部未传入 d1_limit_up, 则在回测内计算 (兼容旧调用)
    # 注意: next_open 模式下 entry_idx=pullback_end+1, d=1 访问的是 D2
    # 因此推荐由调用方预计算并传入
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx + 1 < len(bars):
            d1_bar = bars[entry_idx + 1]
            d1_ret = (d1_bar['close'] / entry_price - 1)
            if d1_ret >= limit_threshold * 0.98:
                d1_limit_up = True

    # next_open模式: entry_idx=D1(D+1开盘买入)
    # 循环d=1应指向D1(第一个持仓日), d=2指向D2, 以此类推
    # 先用D1的high更新peak
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1), d=2 → entry_idx+1(D2)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d = b['open'], d
            break

        # V1出场 (v3): D1日内动量<3% → D2开盘清仓
        # 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)
        #   <0: 盘中出货, D2大概率续跌, 100%捕获D2跌>3%的信号
        #   >=0: 盘中有买盘承接, 继续持有
        # 注: -10%止损已移除, 日内动量规则在D2开盘即清仓, 不需要等止损位
        v1_momentum_exit = False
        if is_v1 and d == 2:
            # 日内动量 = D1收盘涨幅 - D1开盘涨幅 = (D1 close - D1 open) / D0 close
            # d1_change 和 d1_gap 由调用方传入, 也可从bars计算
            if d1_change is not None and d1_gap is not None:
                intraday = d1_change - d1_gap
            else:
                # fallback: 从bars计算
                d1_bar = bars[entry_idx]
                d0_close = bars[entry_idx - 1]['close'] if entry_idx > 0 else entry_price
                intraday = (d1_bar['close'] - d1_bar['open']) / d0_close * 100 if d0_close > 0 else 0
            v1_momentum_exit = intraday < 3

        # 一字跌停: 全天无成交可能 (D2开盘清仓同样无法成交 → 顺延次日开盘)
        if is_one_word_limit_dn(b, dn):
            pending_dn = v1_momentum_exit
            last_unfilled = True
            continue
        last_unfilled = False

        if v1_momentum_exit:
            # D2开盘直接清仓, 不等止损位
            exit_p, exit_d = b['open'], d
            break

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 1 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
            if peak_exit:
                ret = (b['close'] / entry_price - 1) * 100
                if ret > 7:
                    bar_range = b['high'] - b['low']
                    upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                    if upper > 30 and b['close'] < b['high'] * 0.98:
                        exit_p, exit_d = b['close'], d
                        break

            # 2/3 追踪+止损 (合并: 价格连续先穿过更高触发线; 跳空按开盘; 触跌停顺延)
            trig_t = peak * (1 + trailing_stop / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            if b['low'] <= trig:
                fill = fill_on_gap(b['open'], trig)
                if fill_blocked_by_limit_dn(fill, dn):
                    pending_dn = True   # 成交价触及跌停 → 卖不出
                    continue
                exit_p, exit_d = fill, d
                break

        # 4 兜底: 持仓到期收盘走
        exit_p = b['close']; exit_d = d

    # 末日落入无法卖出状态 (一字跌停/触发触跌停) → 顺延下一可交易日开盘强平
    # (连续一字逐日跳过; nxt 指向未成交日的下一日)
    if last_unfilled or pending_dn:
        nxt = entry_idx + exit_d + 1
        while nxt < len(bars):
            nb = bars[nxt]
            pc = bars[nxt - 1]['close']
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb['low'] == nb['high'] and abs(nb['low'] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False
                nxt += 1
                continue
            exit_p, exit_d = nb['open'], nxt - entry_idx + 1
            break

    result = {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }
    if d1_limit_up:
        result['d1_limit_up'] = d1_limit_up
    return result
