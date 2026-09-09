"""strategies/dragon_callback.py — 龙回头策略 ("方案2", StrategyBase 插件实现, Phase 2 迁移)

实现已迁移至本文件; dragon_core.dragon_cb_today_d0_signals / run_backtest_dragon_callback /
DRAGON_CB_PARAMS 为 facade 转发 (test_dragon.py / dragon_scan / dragon_monitor 共用)。

规则框架 (2026-09-06 与 test_dragon.py 同步, 依据 龙回头优化分析_20260906/):
  找龙(滑动窗口涨停占比>=70%) → 回调 gap[5,6] → 拐点OR(MA20支撑 | 深跌释放 | 买盘承接)
  → 信号质量排除(阴线>=60% / RSI6<30 / 距MA20<-8%) → U1~U4(@涨停日) → D1开盘gap(-3,+2]买
  出场: 分段追踪(-8/-3) + 固定止损-8 + 峰值逃顶 + 到期7天

易错点:
  - U1~U4 锚定涨停日 (@D0 评估换手会误杀 — D0 是缩量小阴日);
  - 找龙窗口 start=max(1, lu_idx-window), total_days<3 跳过 — 边界勿动;
  - exit 重放 stop_at_idx 语义: idx>stop_at_idx 即截断 open=True (盘中重放当天未收盘);
  - tech_score 仅输出参考 (评分门槛已关闭, 实验结论无判别力), 不参与过滤。
"""
from __future__ import annotations

from app.market_cn.auto.common.indicators import (
    calc_macd, calc_psy, calc_roc, is_macd_golden_cross,
    is_macd_hist_shrinking_negative, is_macd_hist_turning_positive, rsi,
)
from app.market_cn.auto.common.market import find_limit_ups, get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "dragon_callback"
STRATEGY_LABEL = "龙回头"

DRAGON_CB_PARAMS = dict(
    # --- 找龙: 滑动窗口涨停占比 ---
    dragon_ratio=0.7,
    dragon_windows=[4, 5, 7, 10, 15, 20],
    # --- 回调窗口 ---
    gap_min=5, gap_max=6,
    # --- 拐点过滤 (或关系) ---
    ma20_lo=-10.0, ma20_hi=-5.0,
    depth_max=-30.0,
    yin_ratio_max=0.5,
    # --- 信号质量排除 ---
    yin_ratio_exclude=0.6,
    rsi6_exclude_lt=30.0,
    d0_ma20_exclude_lt=-8.0,
    # --- 入场 ---
    # (2026-09-07 用户裁定: 移除 D1 gap 范围过滤 [-3,+2] — 信号本身已筛选,
    #  高开/低开由用户自行判断, 展示更多股票; 旧引擎口径 114笔/74.6% 已废弃 —
    #  2026-09-09 出场引擎现实化(T+1/跳空/跌停)后 116笔/50.9%/+0.21%)
    # --- 出场 ---
    hold_days=7,
    stop_loss=-8.0,
    trail_lo=-8.0,
    trail_hi=-3.0,
    trail_switch_pct=3.0,
    peak_exit_ret=7.0,
    peak_exit_upper=30.0,
)


# ================================================================
# 出场模拟 (原 dragon_core.run_backtest_dragon_callback, 原样移植)
# 2026-09-09 现实化修正 (tmp/_dragon_intraday_exit.py E1 口径):
#   ① T+1: 买入当日(d=1)不可卖出 — 仅更新峰值/估值, 全部出场判定从 d=2 起;
#   ② 跳空穿越: 触发日开盘价低于触发价 → 按开盘价成交 (跳空低开只能按开盘卖);
#   ③ 跌停无法卖出: 一字跌停整日跳过; 成交价触及跌停 → 顺延次日开盘强平。
#   注意: 追踪线与止损线同日双触发取 max(价格连续, 先穿过更高触发线);
#         峰值逃顶仍是收盘判定优先 — 若当日盘中已触及追踪线, 现实中会先按
#         追踪线成交, 此处保留"收盘逃顶优先"的原设计语义 (已知理想化)。
# ================================================================

# 跌停价原语收编至 common/exec_cn.py (C 阶段); 别名保持调用点不变
from app.market_cn.auto.common.exec_cn import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None, **params):
    """龙回头出场模拟: 分段追踪止损 (as-of安全, 供回测与盘中持仓重放共用)。

    出场判定顺序 (每日, d>=2): 1)峰值逃顶 2)分段追踪+固定止损(合并, 先触发者成交)
    3)到期/stop_at_idx截断。
    现实约束见上方修正注释 (T+1 / 跳空按开盘 / 跌停顺延)。
    stop_at_idx: 只模拟到该bar索引(盘中重放用); 未触发出场 → open=True。
    跌停顺延成交价=次日开盘, 仅当次日 bar 存在且不超过 stop_at_idx (重放 as-of 安全)。
    """
    p = {**DRAGON_CB_PARAMS, **(params or {})}
    hold_days = p["hold_days"] if hold_days is None else hold_days
    stop_loss = p["stop_loss"] if stop_loss is None else stop_loss
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    n = len(bars)
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ""
    capped = False
    pending_dn = False        # 触发成交价触及跌停 → 次日开盘强平
    last_unfilled = False     # 最后一日为一字跌停(整日无法卖出) → 到期顺延

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        if b["high"] > peak:
            peak = b["high"]
        prev_close = bars[idx - 1]["close"] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d, exit_reason = b["open"], d, "跌停顺延开盘"
            break

        # 一字跌停: 全天无成交可能, 持仓顺延 (不更新估值标记)
        if is_one_word_limit_dn(b, dn):
            last_unfilled = True
            continue
        last_unfilled = False

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 1. 峰值逃顶 (收盘判定收盘卖)
            ret = (b["close"] / entry_price - 1) * 100
            if ret > p["peak_exit_ret"]:
                rng = b["high"] - b["low"]
                upper = (b["high"] - max(b["open"], b["close"])) / rng * 100 if rng > 0 else 0
                if upper > p["peak_exit_upper"] and b["close"] < b["high"] * 0.98:
                    exit_p, exit_d, exit_reason = b["close"], d, "峰值逃顶"
                    break

            # 2/3. 分段追踪 + 固定止损 (合并: 价格连续, 先穿过更高触发线)
            peak_ret = (peak / entry_price - 1) * 100
            trail = p["trail_hi"] if peak_ret >= p["trail_switch_pct"] else p["trail_lo"]
            trig_t = peak * (1 + trail / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            if b["low"] <= trig:
                # 跳空穿越: 开盘已低于触发价 → 只能按开盘价成交
                fill = fill_on_gap(b["open"], trig)
                reason = f"追踪止损{trail}%" if trig_t >= trig_s else f"止损{stop_loss}%"
                if fill_blocked_by_limit_dn(fill, dn):
                    pending_dn = True   # 成交价触及跌停 → 卖不出
                    continue
                exit_p, exit_d, exit_reason = fill, d, reason
                break

        exit_p, exit_d = b["close"], d

    if exit_reason == "" and not capped:
        # 末日落入无法卖出状态 (一字跌停 / 触发成交触及跌停) → 顺延至下一可交易日
        # 开盘强平; 连续一字跌停逐日跳过。注意 nxt 必须指向"未成交日的下一日":
        # exit_d 是最后标记估值日(1-based), 未成交日 = exit_d+1, 顺延日 = exit_d+2。
        nxt = entry_idx + exit_d + 1
        while (last_unfilled or pending_dn) and nxt < n \
                and (stop_at_idx is None or nxt <= stop_at_idx):
            nb = bars[nxt]
            pc = bars[nxt - 1]["close"]
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb["low"] == nb["high"] \
                    and abs(nb["low"] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False   # 顺延日仍一字跌停, 再顺延
                nxt += 1
                continue
            exit_p, exit_d, exit_reason = nb["open"], nxt - entry_idx + 1, "跌停顺延开盘"
            break
        if exit_reason == "":
            exit_reason = "持仓到期"
    return {
        "exit_price": round(exit_p, 3), "exit_day": exit_d,
        "exit_reason": exit_reason,
        "return_pct": round((exit_p / entry_price - 1) * 100, 2),
        "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
        "open": bool(capped),
    }


# ================================================================
# StrategyBase 插件实现
# ================================================================

# 旧输出字段 (facade 兼容层精确对齐; 多键/少键都会破坏逐笔对数)
_LEGACY_FIELDS = (
    "code", "board", "path", "path_label", "lu_date", "pullback_days", "signal_date",
    "signal_chg", "signal_vol_r", "signal_price", "entry_vol_r", "buy_mode",
    "gap_from_peak", "d0_vs_ma20", "pullback_depth", "yin_ratio",
    "tech_score", "tech_rsi", "tech_roc", "tech_psy",
)


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 dragon_cb_today_d0_signals 的 dict 形态。"""
    ex = sig.extra or {}
    return {k: ex.get(k) for k in _LEGACY_FIELDS if k not in ("code", "board", "path", "path_label", "signal_date")} | {
        "code": code,
        "board": ex.get("board"),
        "path": "dragon_callback",
        "path_label": "龙回头",
        "signal_date": sig.time,
    }


@register
class DragonCallbackStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "limit_up"     # U1~U4 锚定涨停日 (D0 缩量小阴日评估会误杀)
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DRAGON_CB_PARAMS)

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     use_tech_score=True, **params):
        """龙回头 D0 信号 ("方案2") → Signal (至多1笔)。

        as_of=k: 只用 bars[:k+1] 判定; limit_ups: 预计算涨停索引 (回测优化, None 则现算)。
        params 覆盖 DRAGON_CB_PARAMS 键 (dragon_scan 传 params=dict)。
        """
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 3:
            return result
        i = n - 1
        if i < 2:
            return result
        board_type = get_board_type(code)

        d0 = bars[i]
        prev_c = bars[i - 1]["close"]
        if prev_c <= 0:
            return result
        last_chg = (d0["close"] / prev_c - 1) * 100
        prev_vol = bars[i - 1]["volume"]
        entry_vol_r = d0["volume"] / prev_vol if prev_vol > 0 else 0

        closes = [bars[j]["close"] for j in range(i + 1)]

        # ── tech_score 加分制 (仅参考输出; RSI 值供质量排除使用) ──
        score = 0
        rsi_val = roc = psy = None
        if use_tech_score:
            dif, dea, hist = calc_macd(closes)
            if hist is not None and len(hist) >= 2:
                if is_macd_golden_cross(dif, dea, lookback=5):
                    score += 3
                elif is_macd_hist_turning_positive(hist, lookback=5):
                    score += 2
                elif is_macd_hist_shrinking_negative(hist, lookback=5):
                    score += 1
                n_h = len(hist)
                if n_h >= 2 and abs(dif[n_h - 1]) < abs(dea[n_h - 1]) * 0.5:
                    score += 1
                if dif[n_h - 1] < dea[n_h - 1] and dif[n_h - 2] >= dea[n_h - 2]:
                    score -= 2
            rsi_val = rsi(closes, period=6)
            if rsi_val is not None:
                if rsi_val < 30:
                    score += 2
                elif rsi_val < 40:
                    score += 1
                elif rsi_val < 60:
                    score -= 1
                else:
                    score -= 2
            roc = calc_roc(closes, period=5)
            if roc is not None:
                if -10 <= roc < 0 or 0 <= roc < 5:
                    score += 1
                elif roc < -15 or roc >= 5:
                    score -= 1
            psy = calc_psy(closes, period=10)
            if psy is not None:
                if psy < 30:
                    score += 2
                elif psy < 40:
                    score += 1
                elif psy >= 50:
                    score -= 1

        # ── 方案2 主判定 ──
        for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
            lu_close = bars[lu_idx]["close"]
            if lu_close <= 0:
                continue

            # 当前日(i)收盘必须仍低于涨停收盘 (仍在回调中)
            if bars[i]["close"] >= lu_close:
                continue

            pullback_days = i - lu_idx

            # ── Step1: 找龙 — 滑动窗口内涨停占比>=70% ──
            dragon_found = False
            for window in p["dragon_windows"]:
                start = max(1, lu_idx - window)
                total_days = lu_idx - start
                if total_days < 3:
                    continue
                lu_count = sum(1 for k in range(start, lu_idx)
                               if k > 0 and is_limit_up(bars[k]["close"], bars[k - 1]["close"], board_type))
                if lu_count / total_days >= p["dragon_ratio"]:
                    dragon_found = True
                    break
            if not dragon_found:
                continue

            # ── Step2: gap [gap_min, gap_max] ──
            gap_from_peak = i - lu_idx
            if gap_from_peak < p["gap_min"] or gap_from_peak > p["gap_max"]:
                continue

            # ── 回调期特征 ──
            if i >= 19:
                ma20 = sum(bars[j]["close"] for j in range(i - 19, i + 1)) / 20
                d0_vs_ma20 = (d0["close"] / ma20 - 1) * 100 if ma20 > 0 else None
            else:
                d0_vs_ma20 = None

            min_low = min(bars[j]["low"] for j in range(lu_idx + 1, i + 1))
            pullback_depth = (min_low / lu_close - 1) * 100

            pb_yin = sum(1 for j in range(lu_idx + 1, i + 1) if bars[j]["close"] < bars[j]["open"])
            pb_total = i - lu_idx
            yin_ratio = pb_yin / pb_total if pb_total > 0 else 1.0

            # ── 拐点过滤 (或关系) ──
            cond_ma20 = d0_vs_ma20 is not None and p["ma20_lo"] <= d0_vs_ma20 < p["ma20_hi"]
            cond_depth = pullback_depth <= p["depth_max"]
            cond_yin = yin_ratio < p["yin_ratio_max"]
            if not (cond_ma20 or cond_depth or cond_yin):
                continue

            # ── 信号质量排除 ──
            if yin_ratio >= p["yin_ratio_exclude"]:
                continue
            if rsi_val is not None and rsi_val < p["rsi6_exclude_lt"]:
                continue
            if d0_vs_ma20 is not None and d0_vs_ma20 < p["d0_ma20_exclude_lt"]:
                continue

            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=0,   # 历史口径: 方案2无评分体系, 库内 score 恒0 (与旧 dragon_scan 后处理一致)
                price=round(d0["close"], 3),
                label="龙回头",
                extra={
                    "board": get_board_name(code),
                    "lu_date": bars[lu_idx]["time"],
                    "pullback_days": pullback_days,
                    "signal_chg": round(last_chg, 2),
                    "signal_vol_r": round(entry_vol_r, 2),
                    "signal_price": round(d0["close"], 3),
                    "entry_vol_r": round(entry_vol_r, 2),
                    "buy_mode": "next_open",
                    "gap_from_peak": gap_from_peak,
                    "d0_vs_ma20": round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
                    "pullback_depth": round(pullback_depth, 2),
                    "yin_ratio": round(yin_ratio, 2),
                    "tech_score": score,
                    "tech_rsi": round(rsi_val, 1) if rsi_val else None,
                    "tech_roc": round(roc, 1) if roc else None,
                    "tech_psy": round(psy, 1) if psy else None,
                },
            ))
            break
        return result

    # ---- D1 竞价处置 ----
    def entry_decision(self, row, snap=None, **params):
        """D1 开盘一律可买 (2026-09-07 移除 gap∈[-3,+2] 范围过滤)。

        gap 仅记录在 reason 供用户参考, 高开/低开由用户自行取舍。"""
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        tag = "高开" if gap > 2 else ("低开" if gap < -3 else "")
        return EntryDecision(True, f"gap={gap:.2f}% 可买{tag}")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """无确认步骤: 买入日收盘直接持仓 (出场由收盘重放判定)。

        snap={"series":[...当日快照序列]}; d1_chg 按 signal_price 基准 (旧 evaluate_confirm 口径)。
        返回 None = 无法判定 (快照缺失), monitor 不做状态转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            return None
        d1_chg = (float(series[-1]["last"] or 0) / prev_close - 1) * 100
        return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                               detail={"confirm": "ok", "confirm_strong": False})

    def quality_key(self, row):
        """方案2 质量排序: tech_score(参考) -> 涨停日换手率 (技术分无判别力, 主要按换手热度)。"""
        extra = row.get("extra") or {}
        return (extra.get("tech_score") or 0, extra.get("turnover_anchor") or 0)

    def initial_stop(self, code, entry_price):
        """-8%, 板块不分档 (与回测一致)。"""
        return round(entry_price * (1 + DRAGON_CB_PARAMS["stop_loss"] / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: 复用 run_backtest_dragon_callback (stop_at_idx=今日截断语义)。

        snap={"mode":"day_close","bars":[...],"entry_idx":int}; live 模式 → hold (硬止损在 monitor 主循环)。"""
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        board = get_board_type(row.get("code", ""))
        today_idx = len(bars) - 1
        r = run_backtest_dragon_callback(bars, entry_idx, entry_price, board_type=board,
                                         stop_at_idx=today_idx)
        if r and not r.get("open"):
            exit_idx = entry_idx + r["exit_day"] - 1
            if exit_idx == today_idx and r.get("exit_reason"):
                return ExitDecision("exit", reason=r["exit_reason"], price=float(r["exit_price"]))
        return ExitDecision("hold")
