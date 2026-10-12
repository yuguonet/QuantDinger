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

====================================================================
出场逻辑登记表 (2026-10-07) —— 改出场先看这张表
====================================================================
旧文档"改出场逻辑必须逐份核对"是口头纪律; 这里有索引: 活跃策略的出场**全部**是本模块
的参数化包装 (P1-7/P1-7b 收编后无独立骨架), 差异只在参数与钩子:

  dragon_callback.run_backtest_dragon_callback → run_trail_stop
      ★ 唯一保留**语义差异**的一条:
        ① 分段追踪 trails{hi, lo, switch_pct}   ② peak_exit{ret, upper} 峰值逃顶
        ③ stop_at_idx 重放截断                  ④ **use_trig_prev=False**
          (dragon 历史口径: 追踪线用当日 peak; 2026-10-11 P0-1 该开关语义已正名为
           **追踪线基准** —— True=peak_prev 防先视 / False=peak 历史口径。
           dragon 切 True 是**改回测输出**的口径变更, 须 golden 重冻后再翻)
      ★ 单实现**双消费**: 折叠 evaluate(实时/预处理) 与回测共用它 ⇒ 改一处两边同时变。
  v1 (exit.mode=v1_combo)           → run_d1_limit_up_trail → run_trail_stop
      (2026-10-12 §13.1 A-1b: 原 v1._run_backtest 参数化迁 core, 策略侧只剩适配器;
       无分段追踪, **启用** trig_prev 守卫
                                          ⇒ 追踪线走 peak_prev, 已无日内先视)
  relay3.run_backtest_relay3          → run_limit_seal
      ⚠ relay3 已归档 (strategies/_archive/, autodiscover 不递归) ⇒ 本引擎**当前零调用方**;
        2026-10-12 P1-8 为其补上 `_min_sell_day` 守卫 (原 D1 收盘卖在 A股= T+0 违规),
        t0 市场行为逐位不变。恢复使用前请先确认 T+1 语义是否符合预期。
  g56 (exit.mode=g56_no_trail)     → 适配器逐日 exit_decision 循环 (折叠/回测同源;
      2026-10-12 A-1 接线后 `_exit_no_trail` → run_hold_stop 暂无生产调用方, 见 g56.py 头注)
  break                               → defer_force_open (跌停顺延)

⚠ `strategies/_archive/` 下的 run_backtest_* (dragon_v2 / dragon_callback_legacy) 是
  **收编前的历史快照**, 已停更、不参与任何路径 —— 不得照它们改活跃代码。
"""
from __future__ import annotations

from app.market_cn.auto.core.exec import (
    fill_blocked_by_limit_dn, fill_on_gap, is_one_word_limit_dn,
)
from app.market_cn.auto.core.market import default_market, is_limit_up, limit_dn_price


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
                  board_type="main", spec=None, side="long", with_reason=False):
    """出场 = min(止损触发, 到期收盘)。无追踪。

    with_reason=True: 结果附 exit_reason 标签 (对齐 run_trail_stop; 日线等价对拍用,
    默认 False 不改任何判定/成交字段)。

    与 g56._exit_no_trail 逐位同构 (2026-09-26 迁出):
      - 最早可卖日 = spec.intraday_t0 ? d=1 : d=2 (A股 T+1);
      - 止损: low ≤ 止损线 → fill = min(open, 止损线) (跳空穿越按开盘, 同 fill_on_gap);
      - 到期: hold_days 收盘; 视野不足 → None;
      - peak_return_pct 自入场日起累计 (统计口径, 不影响成交)。

    2026-10-07 补 T+1/跌停对齐 (此前本引擎是登记表里唯一**不齐**的一个,
    run_trail_stop 早已有这三条, g56 独缺 ⇒ 回测按"跌停价成交", 实盘卖不掉):
      ① 一字跌停日整日不可成交 → pending_dn, 顺延次日开盘卖;
      ② 止损成交价贴跌停 → 同样顺延 (走 core.exec 原语, 与 run_trail_stop 同源);
      ③ 到期块补 min_d 守卫 —— hold_days < min_d 时原实现会按入场当日收盘卖出
         (违反 T+1; run_trail_stop 的 A7a 已修, 本引擎漏修)。g56 默认 HOLD_DAYS=7
         不触发, 但 params 覆盖 hold_days=1 即踩中。
      ④ 顺延到数据尽头仍无处可卖 → None (与 run_trail_stop A7b 同语义, 不把
         "无处可卖"当正常到期计入统计)。

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
    pending_dn = False
    exit_p, exit_d = 0.0, 0
    data_exhausted = False
    _reason = ""

    for d in range(min_d, hold_days + 1):
        i = entry_idx + d - 1
        if i >= n:
            data_exhausted = True
            break
        b = bars[i]
        peak = max(peak, float(b.get("high") or 0))
        dn = _dn_at(bars, i, board_type, sp)
        if pending_dn:                       # 前一日封死 → 次日开盘卖 (①)
            exit_p, exit_d = float(b.get("open") or 0), d
            _reason = "跌停顺延开盘"
            pending_dn = False
            break
        if is_one_word_limit_dn(b, dn, sp):  # ① 一字跌停: 整日不可成交
            pending_dn = True
            continue
        low = float(b.get("low") or 0)
        if low <= stop_line:
            fill = fill_on_gap(float(b.get("open") or 0), stop_line)
            if fill_blocked_by_limit_dn(fill, dn, sp):   # ② 成交价贴跌停 → 顺延
                pending_dn = True
                continue
            exit_p, exit_d = fill, d
            _reason = "止损%g%%" % stop_loss
            break

    if not exit_d and not pending_dn and not data_exhausted:
        if hold_days < min_d:                # ③ T+1: 整个视野都不可卖
            return None
        i = entry_idx + hold_days - 1
        if i >= n:
            return None
        dn = _dn_at(bars, i, board_type, sp)
        if is_one_word_limit_dn(bars[i], dn, sp):   # 到期日封死 → 顺延
            pending_dn = True
            exit_d = hold_days
        else:
            px = float(bars[i].get("close") or 0)
            out = {"exit_day": hold_days, "exit_price": round(px, 3),
                   "return_pct": round((px / entry_price - 1) * 100, 2),
                   "peak_return_pct": round((peak / entry_price - 1) * 100, 2)}
            if with_reason:
                out["exit_reason"] = "持仓到期"
            return out

    if pending_dn:                           # 尾块: 找第一个非一字跌停日开盘卖出
        # 起点 = **阻塞日的次日** (2026-10-10 P1-3 专项修正)。
        # 论证: 本引擎无逐日占位 (与 run_trail_stop 不同), 能带着 pending_dn 走到
        #   这里的阻塞日**只可能是到期日 d=hold_days** —— 更早的阻塞在下一循环
        #   迭代开头 (pending 分支) 就地顺延成交, 除非数据尽头 (下方 ④ 返回 None)。
        #   到期日索引 = entry_idx + hold_days - 1 ⇒ 次日 = entry_idx + hold_days。
        # ⚠ 旧公式 `entry_idx + exit_d + 1` 是已知异常的成因 (test_exit_rollover_
        #   real_behavior 锁过): loop 尾阻塞时 exit_d 恒为 0 ⇒ 退化成 entry_idx+1,
        #   在阻塞日**之前**就平仓 (到期日封跌停 → exit_day=2/price=10.0);
        #   而到期块分支 (exit_d=hold_days) 又会多跳一天。两处都按阻塞日换算才自洽。
        nxt = entry_idx + hold_days
        while nxt < n:
            nb = bars[nxt]
            dn2 = _dn_at(bars, nxt, board_type, sp)
            if dn2 is not None and is_one_word_limit_dn(nb, dn2, sp):
                nxt += 1
                continue
            exit_p, exit_d = float(nb.get("open") or 0), nxt - entry_idx + 1
            _reason = "跌停顺延开盘"
            pending_dn = False
            break

    if pending_dn or data_exhausted or not exit_d:   # ④ 视野不足 → None
        return None
    out = {"exit_day": exit_d, "exit_price": round(exit_p, 3),
           "return_pct": round((exit_p / entry_price - 1) * 100, 2),
           "peak_return_pct": round((peak / entry_price - 1) * 100, 2)}
    if with_reason:
        out["exit_reason"] = _reason
    return out


# ================================================================
# 2. trail + stop (v1 / dragon 形态: 合并触发 + 跌停顺延)
# ================================================================

def run_trail_stop(bars, entry_idx, entry_price, *, hold_days, stop_loss,
                   trailing_stop=None, trails=None, board_type="main",
                   peak_exit=False, pre_exit=None, spec=None, side="long",
                   extra_diag=None, stop_at_idx=None, use_trig_prev=True,
                   with_reason=False):
    """止损/追踪合并触发 + 峰值逃顶(可选) + 到期收盘 + 跌停顺延。

    与 v1 出场主循环同构 (2026-09-26 参数化迁出; 2026-10-12 A-1b 后其包装已并入
    run_d1_limit_up_trail):
      - T+1: 最早可卖日 = spec.intraday_t0 ? 1 : 2;
      - 追踪线基准由 use_trig_prev 决定 (2026-10-11 P0-1 根因修复):
          True  → 开盘时已知峰值 peak_prev, **触发与成交同源**, 防日内先视;
          False → peak (含当日 high), dragon 历史口径;
      - 触发成交贴跌停 → pending_dn 顺延次日开盘; 一字跌停整日跳过;
      - 到期日一字跌停 → last_unfilled 顺延强平。

    Args:
        pre_exit: 可选早退钩子 (V1 日内动量 D2 清仓等)。
            签名: pre_exit(d, bar, peak, peak_prev) -> None | "open" | "close"
            返回 "open"/"close" = 以该价清仓后结束; None = 继续常规判定。
        peak_exit: 涨幅>7% 且上影>30% → 收盘逃顶 (dragon/v1 可选)。
        extra_diag: 额外字段并入返回 dict (如 d1_limit_up)。
        use_trig_prev: **追踪线基准开关** (2026-10-11 P0-1 语义正名)。
            True  (默认) → 线用 peak_prev: 日线无法分辨 high/low 先后, 用当日 high
              抬线再判当日 low 触线 = 日内先视 (真实"先跌穿后冲高"不应触发却触发,
              出场系统性偏早)。peak_prev 是开盘时已知量, 无此偏误。
            False → 线用 peak: **dragon 历史口径**, 与其历史回测逐笔一致。
               ⚠ 该口径仍含上述先视。切 True 只需翻转 dragon_callback.py 的实参,
               但属**改回测输出的口径变更**, 须先 golden 重冻 (见
               `修复核对与偏离度_20261010.md` P1-4 —— 该条被推迟的原因)。

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

    data_exhausted = False
    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            data_exhausted = True
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
            # A7 (2026-09-28): 此分支已实际成交 —— 必须清 pending_dn/last_unfilled,
            # 否则尾块 (last_unfilled or pending_dn) 视为仍未成交再顺延一次,
            # 出场推后 2 个交易日 (break.py 自研引擎同病灶, 其 :774 注释早有记载)。
            pending_dn = last_unfilled = False
            break

        # 策略专属早退 (V1 动量等) — 不在本引擎解释语义
        early = pre_exit(d, b, peak, peak_prev) if pre_exit is not None else None

        # A8 (2026-09-29 审计修复 P1): 一字跌停日**照常评估触发**, 只是当日不可成交
        # → 触发一律置 pending_dn 次日开盘卖 (框架不变量 4"触发成交触及跌停→顺延",
        # 与正常路径 fill_blocked_by_limit_dn 的顺延语义逐位一致)。原实现直接 continue
        # 且只挂钩 early 钩子, 常规止损/追踪/峰值逃顶/到期触发整日丢失 —— 次日跳空
        # 高开即永不卖出 (实盘启用的 dragon_callback 受影响)。
        blocked = is_one_word_limit_dn(b, dn, sp)
        if blocked:
            last_unfilled = True
            if early is not None:
                pending_dn = True
                continue
        else:
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
                        if blocked:          # 跌停封死不可成交 → 顺延次日开盘
                            pending_dn = True
                            continue
                        exit_p, exit_d, _exit_reason = float(b.get("close") or 0), d, "峰值逃顶"
                        break

            # ---- 追踪线基准 (2026-10-11 P0-1 根因修复) ----
            # 原实现: 触发判定恒用 peak (已含**当日** high), 而 use_trig_prev 只在
            # 触发后二次修正成交价 ⇒ 触发本身仍是"先跌穿旧线、后冲高"的日线不可分辨
            # 场景下的**日内先视** (出场系统性偏早)。docstring 一直声明"追踪线用开盘时
            # 已知峰值 peak_prev", 实现与契约不符。
            # 根因修法: 让 use_trig_prev 真正控制**基准峰值**, 触发与成交同源:
            #   True  → peak_prev (开盘时已知) —— 防先视 (v1 口径)
            #   False → peak (含当日 high)     —— dragon 历史口径, 逐笔保持不变
            base_peak = peak_prev if use_trig_prev else peak
            if trail_switch is not None:
                gain_pct = (base_peak / entry_price - 1) * 100
                trail_pct = trail_hi if gain_pct >= float(trail_switch) else trail_lo
            else:
                trail_pct = trail_hi          # == trailing_stop (无分段追踪)
            trig_t = base_peak * (1 + trail_pct / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            low = float(b.get("low") or 0)
            if low <= trig:
                if blocked:                  # 跌停封死不可成交 → 顺延次日开盘 (A8)
                    pending_dn = True
                    continue
                # trig 已是 base_peak 口径 ⇒ fill_on_gap 天然区分"开盘就在线下(跳空)
                # → open" 与 "盘中跌破 → trig"。原 use_trig_prev 的 trig_prev 二次
                # 修正属补丁 (只修成交价不修触发), 已由 base_peak 在源头统一, 故删。
                fill = fill_on_gap(float(b.get("open") or 0), trig)
                if fill_blocked_by_limit_dn(fill, dn, sp):
                    pending_dn = True
                    continue
                exit_p, exit_d = fill, d
                _exit_reason = (f"追踪止损{trail_pct}%" if trig_t >= trig_s else f"止损{stop_loss}%")
                break

            # A7a (2026-09-28): 到期收盘出场同样受 min_d 守卫 —— 原来在 `if d >= min_d`
            # 之外, d<min_d 也会按当日收盘卖 (hold_days=1 即入场当日卖出),
            # 违反 spec.intraday_t0 语义 (A股 T+1: 最早 d=2 可卖)。
            if blocked:                      # 到期日跌停封死 → 顺延次日开盘 (A8)
                pending_dn = True
                continue
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
            # A7: 尾块顺延成交后同样清标志, 供下方最终校验识别"已实际出场"。
            last_unfilled, pending_dn = False, False
            break

    # A7b (2026-09-28): 统一"视野不足 → None"语义 (模块头契约 + run_hold_stop 的
    # `if i>=n: return None`)。视野尽头未实际成交的三种情形 —— 数据尽头假到期
    # (data_exhausted)、末段连续一字跌停无处可卖 (标志仍置位)、hold_days<min_d
    # 整个视野不可卖 (_exit_reason 为空) —— 一律返回 None 由调用方跳过该笔,
    # 不再把数据尽头当正常到期计入统计 (胜率/均收被污染且与 g56 口径不可比)。
    # capped 是调用方主动截断重放 (stop_at_idx), 保留原 open=True 语义不受影响。
    if not capped and (data_exhausted or _exit_reason == "" or last_unfilled or pending_dn):
        return None

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
# 2b. d1 涨停确认 + trail (1进2 形态: v1 参数化, 2026-10-12 §13.1 A-1b 迁 core)
# ================================================================

def run_d1_limit_up_trail(bars, entry_idx, entry_price, *, hold_days, stop_loss,
                          trailing_stop=None, board_type="main", peak_exit=False,
                          let_profits_run=False, d1_limit_up=None, d1_change=None,
                          d1_gap=None, stop_at_idx=None, spec=None, side="long",
                          with_reason=False):
    """「1进2」参数化出场: D1(入场日)收盘未达涨停 ⇒ D2 开盘清仓; 连板/让利润奔跑则
    trail/stop/expiry —— 骨架复用 `run_trail_stop`（pre_exit 钩子）。

    2026-10-12 §13.1 A-1b: 原 `strategies/v1._run_backtest` 参数化迁 core
    （策略文件回归「文档」）; 消费方 = v1 的 `v1_combo` 适配器与 `backtest_stock`，
    两者均经 `exit_modes.run_exit` 分派。

    语义 (1进2 设计, 与 v1 monitor 分支同口径):
      - d1_limit_up 锚 = 「进二日(D1=入场日)收盘 相对 首板收盘(D0) 是否涨停」。
        ⚠ 旧实现误用 bars[entry_idx+1](=D2) 相对 D1 开盘, 既错 bar 又错基准 ⇒ 已修。
      - D1 未涨停 ⇒ 非连板 ⇒ D2 开盘清仓 (T+1: 入场当日不可卖)。不用「D1 日内动量<3%」
        旧启发式 —— 会误清一字板(开=收=涨停, 动量≈0)且误持高开非涨停(动量≥3%)。
      - let_profits_run=True: 去掉 D2 一刀切, 全程交 run_trail_stop 的 trail/stop/expiry
        (让利润奔跑: 破峰回撤 trail% 才走 ≈ “破分时均线才走”的日线代理)。
      - d1_change/d1_gap: 供给路径(diag)时用 D1收盘/D0收盘 比值经 is_limit_up 判连板
        (与扫描同口径); 缺省则从 bars 取 (兼容旧调用)。
      - d1_limit_up: 外部未传入则由 bars 推断; 命中时并入返回 dict (d1_limit_up=True)。
    现实约束 (T+1 / 跳空 / 跌停顺延) 已在 run_trail_stop 内按 MarketSpec 处理。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    # d1_limit_up: 若外部未传入则在回测内计算 (兼容旧调用)。
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx > 0 and entry_idx < len(bars):
            d1_close = float(bars[entry_idx]["close"])
            d0_close = float(bars[entry_idx - 1]["close"])
            if d0_close > 0 and is_limit_up(d1_close, d0_close, board_type):
                d1_limit_up = True

    def _pre_exit(d, b, peak, peak_prev):
        # let_profits_run: 无 D2 一刀切 —— 交给 run_trail_stop 的 trail/stop/expiry 出场(让利润跑)
        if let_profits_run:
            return None
        if d != 2:
            return None
        if d1_change is not None and d1_gap is not None:
            # 供给路径(diag): 用 D1收盘/D0收盘 比值经 is_limit_up 判连板 (与扫描同口径)
            _d0 = 100.0
            _d1c = _d0 * (1 + d1_change / 100.0)
            return "open" if not is_limit_up(_d1c, _d0, board_type) else None
        if entry_idx <= 0 or entry_idx >= len(bars):
            return None
        d1_close = float(bars[entry_idx]["close"])
        d0_close = float(bars[entry_idx - 1]["close"])
        if d0_close <= 0:
            return None
        return "open" if not is_limit_up(d1_close, d0_close, board_type) else None

    result = run_trail_stop(
        bars, entry_idx, entry_price,
        hold_days=hold_days, stop_loss=stop_loss, trailing_stop=trailing_stop,
        board_type=board_type, peak_exit=peak_exit, spec=spec, side=side,
        pre_exit=_pre_exit, stop_at_idx=stop_at_idx, with_reason=with_reason,
    )
    if result is not None and d1_limit_up:
        result["d1_limit_up"] = d1_limit_up
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
        # 2026-10-12 P2: 补 `dn2 is not None` 前置, 与同文件 :186/:385 两处写法对齐
        #   (行为同 —— `is_one_word_limit_dn` 内部对 None 已返回 False; 此处只消样子差异)
        if dn2 is not None and is_one_word_limit_dn(nb, dn2, sp):
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

    ⚠️ 2026-10-12 P1-8 修正 —— T+1 守卫:
      上述"D1 收盘卖"在 A 股是 **T+0 违规** (入场当日不可卖, 见本模块登记表第 8 行)，
      与本模块其余引擎 (`run_trail_stop` / `run_hold_stop` 的 `_min_sell_day`) 不一致。
      原先刻意"保留历史口径不改行为"的前提是 relay3 迁移期的逐位同构; 而 relay3
      已于 2026-10-09 归档 (`strategies/_archive/`)、`autodiscover` 不递归 ⇒ 本引擎
      **当前零调用方**, 继续保留缺陷已无意义。
      修正后: 判定仍读 D1 数据 (触板/封板是**判定依据**, 不是卖出动作), 但**卖出日**
      推迟到 `_min_sell_day(spec)` (A股=2 / t0 市场=1 ⇒ t0 市场行为与原实现逐位一致)。
      到期分支同样取 `max(hold_days_max, min_d)`, 避免 hold_days_max=1 时又漏一道。
    """
    if entry_idx >= len(bars) or entry_price <= 0:
        return None
    sp = _spec_or_default(spec)
    # 名义涨停阈值来自 MarketSpec (2026-09-28 审计 P2: 原先硬编码 0.098/0.198 —— 违反
    # "core 零市场常量"硬规则, 接港/美股或增设新档位时会静默算错)。
    # 档位值与原硬编码一致 (main 0.098 / 未命中回落 band_default=gem_star 0.198)。
    nominal = sp.nominal_up_pct(board_type)
    min_d = _min_sell_day(sp)      # 2026-10-12 P1-8: A股 T+1 → 2; t0 市场 → 1
    if not nominal or nominal <= 0:
        # 无涨跌停市场: 不判触板/封板, 直接按到期逻辑 (docstring 许诺的语义, 原实现漏)
        # P1-8: 到期日不得早于最早可卖日 (hold_days_max=1 且 A股时原实现会当日买当日卖)
        max_days0 = max(int(hold_days_max), min_d)
        last_idx0 = min(entry_idx + max_days0 - 1, len(bars) - 1)
        if last_idx0 < entry_idx:
            return {"open": True, "exit_day": max_days0}
        last_close0 = float(bars[last_idx0].get("close") or 0)
        return {"exit_price": round(last_close0, 3), "exit_day": max_days0,
                "exit_reason": f"到期{max_days0}天(无涨跌停市场)",
                "return_pct": round((last_close0 / entry_price - 1) * 100, 2),
                "peak_return_pct": round((last_close0 / entry_price - 1) * 100, 2),
                "open": False}
    limit_price = round(entry_price * (1 + nominal), 2)
    seal_th = limit_price * float(break_sell_ratio)
    n = len(bars)
    d1 = bars[entry_idx]
    d1_high = float(d1.get("high") or 0)
    d1_close = float(d1.get("close") or 0)
    d1_touched = d1_high >= limit_price - 0.001
    d1_sealed = d1_touched and d1_close >= seal_th

    # P1-8 (2026-10-12): 判定读 D1 数据, **卖出**不早于最早可卖日。
    #   A股 (min_d=2) → 顺延到 D2 收盘; t0 市场 (min_d=1) → 仍是 D1 收盘, 与原实现逐位一致。
    sell_day = max(1, min_d)
    sell_idx = entry_idx + sell_day - 1
    if sell_idx >= n:
        return {"open": True, "exit_day": sell_day}
    sell_close = float(bars[sell_idx].get("close") or 0)
    sell_peak = max((float(bars[j].get("high") or 0)
                     for j in range(entry_idx, sell_idx + 1)), default=d1_high)

    if not d1_touched:
        return {"exit_price": round(sell_close, 3), "exit_day": sell_day,
                "exit_reason": "S4未封板尾盘卖",
                "return_pct": round((sell_close / entry_price - 1) * 100, 2),
                "peak_return_pct": round((sell_peak / entry_price - 1) * 100, 2),
                "open": False}
    if not d1_sealed:
        return {"exit_price": round(sell_close, 3), "exit_day": sell_day,
                "exit_reason": "炸板卖出(S4)",
                "return_pct": round((sell_close / entry_price - 1) * 100, 2),
                "peak_return_pct": round((sell_peak / entry_price - 1) * 100, 2),
                "open": False}
    peak_high = d1_high
    max_days = max(int(hold_days_max), min_d)   # P1-8: 到期日同样不早于最早可卖日
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
