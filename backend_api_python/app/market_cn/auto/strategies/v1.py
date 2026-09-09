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

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        """D0 四因子 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1], 末根为 D0。"""
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
        if (d0["close"] / d_1["close"] - 1) < threshold * 0.98:
            return result

        # === 因子1: 强趋势 20日涨>ret_20d_min% ===
        if i < 20 or bars[i - 20]["close"] <= 0:
            return result
        ret_20d = (d0["close"] / bars[i - 20]["close"] - 1) * 100
        if ret_20d < p["ret_20d_min"]:
            return result

        # === 因子2: D-1回调 [d_1_pullback_min, d_1_pullback_max) ===
        d_1_change = (d_1["close"] / d_2["close"] - 1) * 100
        if d_1_change < p["d_1_pullback_min"] or d_1_change >= p["d_1_pullback_max"]:
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
                return result

        # === 因子4: D-1非放量 < d_1_vol_max x 5日均量 ===
        if i >= 6:
            vol_ma5_d1 = sum(bars[j]["volume"] for j in range(i - 6, i - 1)) / 5
            if vol_ma5_d1 > 0 and d_1["volume"] / vol_ma5_d1 >= p["d_1_vol_max"]:
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
                return result
            if boll_bw is not None and boll_bw >= 45:
                return result

        circ = float((params.get("stock_info") or {}).get("circ_shares") or 0)
        total = float((params.get("stock_info") or {}).get("total_shares") or 0)
        d0_close = round(d0["close"], 3)
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
