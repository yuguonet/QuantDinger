#!/usr/bin/env python3
"""core/exit_engines.py — 参数化出场引擎 (2026-09-26 P1-7)

用途: 把散落在 strategies/*.py 的出场循环 (T+1 / 止损 / 追踪 / 到期 / 跌停顺延)
     收敛为**市场无关**的参数化引擎。策略只声明阈值, 不再自带循环骨架。

★ core 零市场局限 (用户 2026-09-26 硬约束):
  - **不写死** long_only / T+1 / A股涨跌停 —— 一律读 MarketSpec:
      spec.intraday_t0   当日新开仓可否当日平 (A=false → 最早 d=2 卖; HK/US=true → d=1 可卖)
      spec.direction     long_only | long_short (short 路径预留, 由 side/挂载点表达)
      spec.short_rule    做空约束声明 (core 不解释语义, 仅透传给适配层)
      spec.price_band / limit_dn_price / is_one_word_limit_dn  走 core.exec 原语
  - 做T (t_legs) / 实时交易: 本模块只做**原子出场腿**; 状态机、部分减仓、
    当日回转的持仓簿记归上层 (monitor / 未来的 exec_engine)。禁止在此假设
    "回测路径 = 实盘路径"。

成交语义唯一实现 = core/exec.py (fill_on_gap / fill_blocked_by_limit_dn /
is_one_word_limit_dn)。本模块只做模式编排 + 状态机 (pending_dn / 顺延)。

Returns (统一 dict, 与历史策略引擎同构):
    {"exit_day", "exit_price", "return_pct", "peak_return_pct", ...}
  数据不足 / 无法成交视野 → None (调用方跳过该笔)。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.market_cn.auto.core.exec import (
    fill_blocked_by_limit_dn, fill_on_gap, is_one_word_limit_dn,
)
from app.market_cn.auto.core.market import default_market, limit_dn_price


def _spec_or_default(spec=None):
    return spec if spec is not None else default_market()


def _min_sell_day(spec) -> int:
    """最早可卖持仓日 (d 口径: d=1 是入场当日)。A股 T+1 → 2; t0 市场 → 1。"""
    return 1 if getattr(spec, "intraday_t0", False) else 2


def _dn_at(bars, idx, board_type, spec):
    """bars[idx] 的跌停价; 无昨收/无涨跌停市场 → None (不判定)。"""
    if idx <= 0:
        return None
    prev_close = float(bars[idx - 1].get("close") or 0)
    if prev_close <= 0:
        return None
    return limit_dn_price(prev_close, board_type, spec)


def _can_short(spec) -> bool:
    return str(getattr(spec, "direction", "long_only")) == "long_short"


# ================================================================
# 1. hold + stop (g56 形态: 无追踪)
# ================================================================

def run_hold_stop(bars, entry_idx, entry_price, *, hold_days, stop_loss,
                  board_type="main", spec=None, side="long"):
    """出场 = min(止损触发, 到期收盘)。无追踪。

    与 g56._exit_no_trail 逐位同构 (2026-09-26 迁出):
      - 最早可卖日 = spec.intraday_t0 ? d=1 : d=2 (A股 T+1);
      - 止损: low ≤ 止损线 → fill = min(open, 止损线) (跳空穿越按开盘, 同 fill_on_gap);
      - 到期: hold_days 收盘; 视野不足 → None;
      - peak_return_pct 自入场日起累计 (统计口径, 不影响成交)。

    side="short" 预留 (direction=long_short 市场): 止损线反向, 涨停不可买不影响卖。
    """
    if entry_price <= 0 or entry_idx >= len(bars) or hold_days is None:
        return None
    sp = _spec_or_default(spec)
    n = len(bars)
    hold_days = int(hold_days)
    stop_loss = float(stop_loss)
    if side == "short" and not _can_short(sp):
        raise ValueError(f"side=short 但市场 direction={sp.direction!r} 不允许做空")
    # 止损线: long = entry*(1+stop/100); short = entry*(1-stop/100) (stop 为负数阈值时同公式)
    stop_line = entry_price * (1 + stop_loss / 100)
    peak = float(bars[entry_idx].get("high") or entry_price)
    min_d = _min_sell_day(sp)   # A股 T+1 → 2; t0 市场 → 1 (可当日平)
    for d in range(min_d, hold_days + 1):
        i = entry_idx + d - 1
        if i >= n:
            return None
        peak = max(peak, float(bars[i].get("high") or 0))
        low = float(bars[i].get("low") or 0)
        if low <= stop_line:
            fill = min(float(bars[i].get("open") or 0), stop_line)
            return {"exit_day": d, "exit_price": round(fill, 3),
                    "return_pct": round((fill / entry_price - 1) * 100, 2),
                    "peak_return_pct": round((peak / entry_price - 1) * 100, 2)}
    i = entry_idx + hold_days - 1
    if i >= n:
        return None
    px = float(bars[i].get("close") or 0)
    return {"exit_day": hold_days, "exit_price": round(px, 3),
            "return_pct": round((px / entry_price - 1) * 100, 2),
            "peak_return_pct": round((peak / entry_price - 1) * 100, 2)}


# ================================================================
# 2. trail + stop (v1 / dragon 形态: 合并触发 + 跌停顺延)
# ================================================================

def run_trail_stop(bars, entry_idx, entry_price, *, hold_days, stop_loss,
                   trailing_stop=None, trails=None, board_type="main",
                   peak_exit=False, pre_exit=None, spec=None, side="long",
                   extra_diag=None, stop_at_idx=None, use_trig_prev=True,
                   with_reason=False):
    """止损/追踪合并触发 + 峰值逃顶(可选) + 到期收盘 + 跌停顺延。

    与 v1._run_backtest 主循环同构 (2026-09-26 参数化迁出):
      - T+1: 最早可卖日 = spec.intraday_t0 ? 1 : 2;
      - 追踪线用 **开盘时已知峰值** peak_prev 防日内先视 (fill_on_gap 误用修正);
      - 触发成交贴跌停 → pending_dn 顺延次日开盘; 一字跌停整日跳过;
      - 到期日一字跌停 → last_unfilled 顺延强平。

    Args:
        pre_exit: 可选早退钩子 (V1 日内动量 D2 清仓等)。
            签名: pre_exit(d, bar, peak, peak_prev) -> None | "open" | "close"
            返回 "open"/"close" = 以该价清仓后结束; None = 继续常规判定。
        peak_exit: 涨幅>7% 且上影>30% → 收盘逃顶 (dragon/v1 可选)。
        extra_diag: 额外字段并入返回 dict (如 d1_limit_up)。

    Returns:
        dict | None
    """
    if entry_price <= 0 or entry_idx >= len(bars) or hold_days is None:
        return None
    sp = _spec_or_default(spec)
    n = len(bars)
    hold_days = int(hold_days)
    stop_loss = float(stop_loss)
    trailing_stop = float(trailing_stop) if trailing_stop is not None else stop_loss
    trail_hi = trail_lo = trailing_stop
    trail_switch = None
    if trails:
        trail_hi = float(trails.get("hi", trailing_stop))
        trail_lo = float(trails.get("lo", trailing_stop))
        trail_switch = trails.get("switch_pct")
    peak_exit_ret = 7.0
    peak_exit_upper = 30.0
    if isinstance(peak_exit, dict):
        peak_exit_ret = float(peak_exit.get("ret", 7.0))
        peak_exit_upper = float(peak_exit.get("upper", 30.0))
        peak_exit = True
    if side == "short" and not _can_short(sp):
        raise ValueError(f"side=short 但市场 direction={sp.direction!r} 不允许做空")
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False
    last_unfilled = False
    _exit_reason = ""
    capped = False
    min_d = _min_sell_day(sp)

    if entry_idx < n:
        h = float(bars[entry_idx].get("high") or 0)
        if h > peak:
            peak = h

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        peak_prev = peak
        h = float(b.get("high") or 0)
        if h > peak:
            peak = h
        dn = _dn_at(bars, idx, board_type, sp)

        if pending_dn:
            exit_p, exit_d, _exit_reason = float(b.get("open") or 0), d, "跌停顺延开盘"
            break

        # 策略专属早退 (V1 动量等) — 不在本引擎解释语义
        early = pre_exit(d, b, peak, peak_prev) if pre_exit is not None else None

        if is_one_word_limit_dn(b, dn, sp):
            pending_dn = (early is not None)
            last_unfilled = True
            continue
        last_unfilled = False

        if early == "open":
            exit_p, exit_d, _exit_reason = float(b.get("open") or 0), d, "早退开盘"
            break
        if early == "close":
            exit_p, exit_d, _exit_reason = float(b.get("close") or 0), d, "早退收盘"
            break

        if d >= min_d:
            if peak_exit:
                ret = (float(b.get("close") or 0) / entry_price - 1) * 100
                if ret > peak_exit_ret:
                    bar_range = float(b.get("high") or 0) - float(b.get("low") or 0)
                    upper = (float(b.get("high") or 0) - max(float(b.get("open") or 0),
                            float(b.get("close") or 0))) / bar_range * 100 if bar_range > 0 else 0
                    if upper > peak_exit_upper and float(b.get("close") or 0) < float(b.get("high") or 0) * 0.98:
                        exit_p, exit_d, _exit_reason = float(b.get("close") or 0), d, "峰值逃顶"
                        break

            trail_pct = trail_hi
            if trail_switch is not None and (peak / entry_price - 1) * 100 >= float(trail_switch):
                trail_pct = trail_hi
            elif trail_switch is not None:
                trail_pct = trail_lo
            trig_t = peak * (1 + trail_pct / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            low = float(b.get("low") or 0)
            if low <= trig:
                fill = fill_on_gap(float(b.get("open") or 0), trig)
                if use_trig_prev:
                    # 开盘时已存在的线: open<=trig_prev 才按开盘 (防日内先视; v1 口径)
                    trig_prev = max(peak_prev * (1 + trail_pct / 100), trig_s)
                    if float(b.get("open") or 0) > trig_prev:
                        fill = trig
                if fill_blocked_by_limit_dn(fill, dn, sp):
                    pending_dn = True
                    continue
                exit_p, exit_d = fill, d
                _exit_reason = (f"追踪止损{trail_pct}%" if trig_t >= trig_s else f"止损{stop_loss}%")
                break

        exit_p = float(b.get("close") or 0)
        exit_d = d
        _exit_reason = "持仓到期"

    if (last_unfilled or pending_dn) and not capped:
        nxt = entry_idx + exit_d + 1
        while nxt < n:
            nb = bars[nxt]
            dn2 = _dn_at(bars, nxt, board_type, sp)
            if dn2 is not None and is_one_word_limit_dn(nb, dn2, sp):
                last_unfilled, pending_dn = True, False
                nxt += 1
                continue
            exit_p, exit_d, _exit_reason = float(nb.get("open") or 0), nxt - entry_idx + 1, "跌停顺延开盘"
            break

    result = {
        "exit_price": round(exit_p, 3), "exit_day": exit_d,
        "return_pct": round((exit_p / entry_price - 1) * 100, 2),
        "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
    }
    if with_reason:
        result["exit_reason"] = _exit_reason or ("持仓到期" if exit_d else "")
    if stop_at_idx is not None:
        result["open"] = bool(capped)
    if extra_diag:
        for k, v in extra_diag.items():
            if v is not None:
                result[k] = v
    return result


# ================================================================
# 3. 共用: 跌停顺延强平 (break / dragon / v1 尾段同构)
# ================================================================

def defer_force_open(bars, entry_idx, exit_d, *, last_unfilled, pending_dn,
                     board_type="main", stop_at_idx=None, spec=None):
    """末日无法卖出 (一字跌停/触发触跌停) → 顺延下一可交易日开盘强平。

    Returns:
        (exit_price, exit_day, exit_reason) 或 None (无顺延 / 视野不足)
    """
    if not (last_unfilled or pending_dn):
        return None
    sp = _spec_or_default(spec)
    n = len(bars)
    nxt = entry_idx + exit_d + 1
    while nxt < n:
        if stop_at_idx is not None and nxt > stop_at_idx:
            return None
        nb = bars[nxt]
        dn2 = _dn_at(bars, nxt, board_type, sp)
        if is_one_word_limit_dn(nb, dn2, sp):
            last_unfilled, pending_dn = True, False
            nxt += 1
            continue
        return (float(nb.get("open") or 0), nxt - entry_idx + 1, "跌停顺延开盘")
    return None


# ================================================================
# 4. 连板封住延续 (relay3 形态: D1 触板判定 + 封住后追踪)
# ================================================================

def run_limit_seal(bars, entry_idx, entry_price, *, board_type="main",
                   break_sell_ratio=0.995, trail_after_limit=-8.0,
                   hold_days_max=3, spec=None):
    """D1 触板/封板判定 + 封住延续追踪 (relay3 S4 日线近似, 2026-09-26 迁出)。

    语义 (与 relay3.run_backtest_relay3 逐位同构):
      - D1 未触涨停 → S4 尾盘卖 (D1 close);
      - D1 触板未封 (close < limit*ratio) → 炸板卖 (D1 close);
      - D1 封住 → D2+ 追踪 (close/peak-1 <= trail_after_limit) / 到期 hold_days_max。
    ⚠️ 保留历史口径 (含 D1 收盘卖): relay3 已停用, 迁移**不改行为**。
    """
    if entry_idx >= len(bars) or entry_price <= 0:
        return None
    sp = _spec_or_default(spec)
    # 名义涨停阈值 (与原 th=0.098/0.198 同); 无涨跌停市场 → 不判触板, 直接按到期逻辑
    nominal = 0.198 if board_type == "gem_star" else 0.098
    limit_price = round(entry_price * (1 + nominal), 2)
    seal_th = limit_price * float(break_sell_ratio)
    n = len(bars)
    d1 = bars[entry_idx]
    d1_high = float(d1.get("high") or 0)
    d1_close = float(d1.get("close") or 0)
    d1_touched = d1_high >= limit_price - 0.001
    d1_sealed = d1_touched and d1_close >= seal_th

    if not d1_touched:
        return {"exit_price": round(d1_close, 3), "exit_day": 1,
                "exit_reason": "S4未封板尾盘卖",
                "return_pct": round((d1_close / entry_price - 1) * 100, 2),
                "peak_return_pct": round((d1_high / entry_price - 1) * 100, 2),
                "open": False}
    if not d1_sealed:
        return {"exit_price": round(d1_close, 3), "exit_day": 1,
                "exit_reason": "炸板卖出(S4)",
                "return_pct": round((d1_close / entry_price - 1) * 100, 2),
                "peak_return_pct": round((d1_high / entry_price - 1) * 100, 2),
                "open": False}
    peak_high = d1_high
    max_days = int(hold_days_max)
    for day in range(2, max_days + 1):
        idx = entry_idx + day - 1
        if idx >= n:
            return {"open": True, "exit_day": day}
        bar = bars[idx]
        hi, cl = float(bar.get("high") or 0), float(bar.get("close") or 0)
        peak_high = max(peak_high, hi)
        if (cl / peak_high - 1) * 100 <= float(trail_after_limit):
            return {"exit_price": round(cl, 3), "exit_day": day,
                    "exit_reason": f"追踪止损{trail_after_limit}%",
                    "return_pct": round((cl / entry_price - 1) * 100, 2),
                    "peak_return_pct": round((peak_high / entry_price - 1) * 100, 2),
                    "open": False}
    last_idx = entry_idx + max_days - 1
    if last_idx >= n:
        return {"open": True, "exit_day": max_days}
    last_close = float(bars[last_idx].get("close") or 0)
    return {"exit_price": round(last_close, 3), "exit_day": max_days,
            "exit_reason": f"到期{max_days}天",
            "return_pct": round((last_close / entry_price - 1) * 100, 2),
            "peak_return_pct": round((peak_high / entry_price - 1) * 100, 2),
            "open": False}
