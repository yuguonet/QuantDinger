"""ide/intraday.py — IDE 侧盘中快照 (intraday_window) 回测通道 (M4)。

镜像 `backtest.run_all_intraday` 的**时间线编排**（交易日 × 触发槽位 × 全市场快照帧），
但把策略判定 `scan_signals` 替换为**门表求值** —— 使 knife_catch / tail_oversold 等
"14:56 尾盘买入 → D1 开盘卖" 的盘中策略也可由 YAML 表达（不改生产引擎）。

复用（数据/性能层，非策略规则；均为契约内的"必要条件超集"，只影响枚举规模、不影响 trades）：
  - data.frames 快照帧（kline_1m 重建）+ data.hub.daily（as-of D-1 日线上下文）；
  - 策略的 `day_prefilter`（日级必要条件超集 → 整日跳过）与 `intraday_shortlist`
    （槽位级便宜预筛）—— 契约见 base.py（预筛必须数学必要；误杀会破坏等价）。
    二者与实盘无关（实盘全市场快照本就现成），仅回测提速用。

替换（策略规则层）：
  - `scan_signals` → 门表求值：`role: qualify` 门用 **快照 Ctx**（latest/mkt_gain）、
    `role: required` 门用 **日线+序列 Ctx**（bars as-of D-1 + series）。
  - `Signal.extra` → `spec.signal.fields`（门通过后用同一 Ctx 求值），保证 trades 逐字段一致。

YAML 契约（meta.intraday）：
    intraday:
      windows: ["14:30", "15:00"]   # 触发窗口 (entry_at 缺省时按此展开)
      interval_sec: 60
      entry_at: "14:56"             # 可选: 终审语义, 只在该时刻成交 (优先于 windows)

as-of 纪律：日线上下文 `bars` 截至**前一交易日**（bars[-1]=昨日），与参考版同源。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.market_cn.auto.core.market import get_board_type
from app.market_cn.auto.core.runtime.evaluate import build_signal, evaluate_gates
from app.market_cn.auto.core.exit_modes import run_exit
from app.market_cn.auto.core.runtime.functions import Ctx


def _trigger_mis(entry_at: Optional[str], windows, interval_sec: int) -> List[int]:
    """ScanSpec 语义 → 成交触发槽位列表（镜像 backtest._exec_trigger_mis）。

    entry_at 优先（终审语义：只回该时刻）；否则按 windows/interval_sec 展开。
    """
    from app.market_cn.auto.core.data.frames import hhmm_to_pos
    if entry_at:
        mi = hhmm_to_pos(entry_at)
        return [mi] if mi >= 0 else []
    from app.market_cn.auto.sched import expand_times
    mis: List[int] = []
    for t in expand_times(windows, int(interval_sec)):
        mi = hhmm_to_pos(t)
        if mi >= 0 and mi not in mis:
            mis.append(mi)
    return sorted(mis)


def _rollover_pc(frame, pc_map):
    """pc_map 结转: 当日 1m 最后一根 close（跳过的日级预筛日也必须结转）。"""
    for code in frame.codes:
        lc = frame.last_close(code)
        if lc > 0:
            pc_map[code] = lc
    return pc_map


def run_all_intraday_ide(spec, days: int = 120, codes=None,
                         start_date=None, end_date=None,
                         progress_every: int = 1, quiet: bool = False) -> Dict[str, Any]:
    """门表版 intraday_window 全市场回测（时间线引擎，镜像 run_all_intraday）。

    返回 {"trades": [...], "codes_ok": n, "elapsed": s}。逐笔等价于
    `backtest.run_all_intraday(<同策略>)`（见 tmp/_knife_equivalence.py / _tail_equivalence.py）。
    """
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.core.data import frames as fr
    from app.market_cn.auto.core.data.frames import MI_HHMM
    from app.market_cn.auto.core.data.hub import daily, stock_info as _hub_stock_info

    conf = spec.meta.get("intraday") or {}
    mis = _trigger_mis(conf.get("entry_at"), conf.get("windows"),
                       int(conf.get("interval_sec", 60)))
    if not mis:
        raise ValueError(f"{spec.key}: meta.intraday 无有效成交触发时刻 (entry_at={conf.get('entry_at')!r} "
                         f"windows={conf.get('windows')!r})")

    # 复用策略的 day_prefilter / intraday_shortlist（性能钩子，必要条件超集；见模块头）
    strat_reg.autodiscover()
    strat = strat_reg.get_strategy(spec.key)

    _p = spec.params
    qual_gates = spec.prefilter_gates()
    req_gates = spec.decision_gates()
    t0 = time.time()

    all_dates = fr.trading_dates(days_back=days, end=end_date)
    first_1m = fr.first_1m_date()
    dates = [d for d in all_dates if not first_1m or d >= first_1m]
    if start_date:
        dates = [d for d in dates if d >= str(start_date)[:10]]
    if not dates:
        return {"trades": [], "codes_ok": 0, "elapsed": 0}
    _i0 = all_dates.index(dates[0]) if dates[0] in all_dates else -1
    _first_prev = all_dates[_i0 - 1] if _i0 > 0 else None
    code_set = set(codes) if codes else None
    pc_map = fr.prev_closes(dates[0])

    try:
        _si = _hub_stock_info()
    except Exception:
        _si = {}

    def _st_ok(code):
        nm = (_si.get(code) or {}).get("name", "") or ""
        return "ST" not in nm.upper()

    _daily_memo: Dict[str, Any] = {}

    def _daily_asof(code, prev_date):
        bars = _daily_memo.get(code)
        if bars is None:
            bars = _daily_memo.setdefault(code, daily(code, 300))
        if prev_date:
            return [b for b in bars if str(b["time"])[:10] <= str(prev_date)[:10]]
        return bars

    trades: List[Dict[str, Any]] = []
    seen = set()
    for di, date in enumerate(dates):
        frame = fr.build_frame(date)
        if len(frame) == 0:
            continue
        prev_date = dates[di - 1] if di > 0 else _first_prev
        if prev_date is None:            # 覆盖起点前无交易日 → 无日线上下文
            continue
        day_sel = strat.day_prefilter(frame, pc_map) if strat is not None else None
        if day_sel is not None:
            day_sel = set(day_sel)
            if code_set is not None:
                day_sel &= code_set
            if not day_sel:
                pc_map = _rollover_pc(frame, pc_map)
                continue
        else:
            day_sel = code_set
        n_sig_day = 0
        for mi in mis:
            snaps = frame.snaps_at(mi, pc_map, codes=day_sel)
            if not snaps:
                continue
            mkt = frame.mkt_gain(mi, pc_map)
            short = strat.intraday_shortlist(snaps, mkt) if strat is not None else snaps
            if not short:
                continue
            for code, snap in short.items():
                if (code, date) in seen or not _st_ok(code):
                    continue
                bt = get_board_type(code, spec.market_spec)
                sinfo = _si.get(code) if _si else None
                # ① 快照级门 (qualify): 只需 latest/mkt_gain → 空 bars 的 Ctx 即可判
                c1 = Ctx([], 0, 0, _p, board_type=bt, code=code, stock_info=sinfo,
                         latest=snap, mkt_gain=mkt, market=spec.market_spec)
                ok, _ = evaluate_gates(spec, qual_gates, c1)
                if not ok:
                    continue
                # ② 日线+序列门 (required): bars as-of D-1 + series(截至槽位前)
                bars = _daily_asof(code, prev_date)
                series = fr_minute_series(frame, code, mi)
                i = len(bars) - 1
                c2 = Ctx(bars, i, 0, _p, board_type=bt, code=code, stock_info=sinfo,
                         latest=snap, series=series, mkt_gain=mkt,
                         market=spec.market_spec)
                ok, _ = evaluate_gates(spec, req_gates, c2)
                if not ok:
                    continue
                seen.add((code, date))                 # 每股每日首信号成交（与参考版同序）
                entry_price = float(snap["last"])
                if entry_price <= 0:
                    continue
                full = _daily_asof(code, None)
                ei = next((j for j, b in enumerate(full)
                           if str(b["time"])[:10] == str(date)[:10]), None)
                if ei is None:
                    continue
                ex = run_exit(spec.exit.get("mode", "d1_open"), bars=full, entry_idx=ei,
                              entry_price=entry_price, code=code, board_type=bt,
                              params=_p, diag={})
                if not ex:
                    continue
                sig = build_signal(c2, spec)
                _tr = {
                    **sig,
                    "code": code, "signal_date": date, "entry_date": date,
                    "entry_price": round(entry_price, 3), "buy_mode": "intraday_trigger",
                    "trigger": conf.get("entry_at") or MI_HHMM[mi],
                    "exit_date": ex.get("exit_date"), "exit_price": ex.get("exit_price"),
                    "exit_day": ex.get("exit_day"), "exit_reason": ex.get("exit_reason"),
                    "return_pct": ex.get("return_pct"),
                }
                if ex.get("peak_return_pct") is not None:
                    _tr["peak_return_pct"] = ex["peak_return_pct"]
                trades.append(_tr)
                n_sig_day += 1
        pc_map = _rollover_pc(frame, pc_map)
        if progress_every and (di + 1) % progress_every == 0 and not quiet:
            print(f"[{di + 1}/{len(dates)}] {date} 信号={n_sig_day} 累计={len(trades)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    return {"trades": trades, "codes_ok": len(dates), "elapsed": round(time.time() - t0, 1)}


def fr_minute_series(frame, code, mi):
    """槽位 mi 之前的快照形序列（= frame.series(code, mi)）。独立函数便于测试/替换。"""
    return frame.series(code, mi)
