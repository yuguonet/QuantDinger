"""dragon_monitor.py — 自动策略组盘中状态机 (60s tick, scheduler 调度)

覆盖三策略: dragon2(龙回头Pro) / v1 / break(断板) —— d1open 线上模式。

各策略入场 (09:25~09:35 开盘窗口, 用 9:26 集合竞价快照):
  dragon2: gap ∈ (a)-3~+2 / (b)-3.5~+5.5
  v1:      主板 gap>=-3 且 非[3,5)高开区间; 创科板 -5<=gap<5
  break:   无开盘过滤 (断板确认日本身已过 5a~5f 检查)
各策略确认 (15:01+, 当日快照收盘):
  dragon2: 强(涨>=3%且量>=1.5x)/中/弱(收阴或无力) → 持仓/卖出
  v1:      日内动量 = D1涨幅-D1开盘涨幅 < 3% 或 收阴 → 卖出(D2开盘清仓); 否则持仓
  break:   无确认步骤 → 买入次日直接持仓
各策略出场 (14:58 收盘重放 + 盘中硬止损):
  dragon2: 止损-5/-7, 追踪-5/-7(涨停日豁免), 连板>=2断板尾盘卖, 巨量出货, 峰值逃顶, 到期7/10
  v1:      止损-10, 追踪-5, 到期7 (D1弱确认已在15:00转卖出)
  break:   止损-8/-10(收盘价), 追踪-6/-8(收盘价,盈利时), 峰值逃顶(涨>10%上影>40%), 到期20/15
卖出执行: 次日开盘按开盘价记账 closed 并出组 (建议人工尾盘/次日开盘执行)。

与回测的已知差异: 回测"尾盘卖"按当日收盘成交; 自动化在 14:58 提示、
未执行者次日开盘记账。盘中追踪止损不做分钟级模拟 (日线粒度)。
"""
from __future__ import annotations

import os
import json
from datetime import datetime

from app.utils.logger import get_logger

logger = get_logger(__name__)

try:
    from dotenv import load_dotenv
    for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '.env'),
               os.path.join(os.getcwd(), '.env')):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

from app.market_cn.auto import dragon_store as ds

W_OPEN_LO, W_OPEN_HI = "09:25", "09:35"
W_PRECONF_LO, W_PRECONF_HI = "14:25", "14:45"
W_CLOSESIM_LO, W_CLOSESIM_HI = "14:58", "15:06"
W_CONFIRM_LO = "15:01"


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _last_trade_day() -> str:
    try:
        from app.utils.trading_calendar import last_finish_trading_day
        return last_finish_trading_day()
    except Exception:
        return _today()


def in_window(lo, hi, hm=None):
    hm = hm or _now_hm()
    return lo <= hm < hi


# ================================================================
# 快照读取 (market DB, realtime_snapshot_YYYY)
# ================================================================

def _snapshot_pool():
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()._get_pool("CNStock")


def _rows(cur):
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _snapshot_table():
    return f"realtime_snapshot_{datetime.now().year}"


def snapshot_day_done() -> bool:
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(time) FROM \"{_snapshot_table()}\" "
                            "WHERE time::date = CURRENT_DATE")
                r = cur.fetchone()
        if not r or r[0] is None:
            return False
        return r[0].strftime("%H:%M") >= "15:00"
    except Exception:
        return False


def fetch_day_snapshots(codes):
    if not codes:
        return {}
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT symbol, time, open, high, low, \"last\", \"previousClose\", volume "
                    f"FROM \"{_snapshot_table()}\" "
                    f"WHERE symbol = ANY(%s) AND time >= %s ORDER BY symbol, time",
                    (list(codes), f"{_today()} 09:00:00"),
                )
                rows = _rows(cur)
    except Exception as e:
        logger.warning("[dragon_monitor] 快照读取失败: %s", e)
        return {}
    out = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(r)
    return out


def latest_snapshot(codes):
    series = fetch_day_snapshots(codes)
    return {code: rows[-1] for code, rows in series.items() if rows}


# ================================================================
# 策略参数
# ================================================================

def _entry_stop(code, entry_price, strategy):
    from app.market_cn.auto.dragon_core import DRAGON2_PARAMS, get_board_type
    gem = get_board_type(code) == "gem_star"
    if strategy == "v1":
        stop = -10.0
    elif strategy == "break":
        stop = -10.0 if gem else -8.0
    else:
        stop = DRAGON2_PARAMS["stop_gem"] if gem else DRAGON2_PARAMS["stop_main"]
    return round(entry_price * (1 + stop / 100), 3)


def _gap_buyable(code, strategy, style, gap):
    """开盘 gap 是否可买 (与各策略回测的 D1 过滤一致)。"""
    from app.market_cn.auto.dragon_core import DRAGON2_PARAMS, get_board_type
    if strategy == "break":
        return True
    if strategy == "v1":
        if get_board_type(code) == "gem_star":
            return -5.0 <= gap < 5.0
        return gap >= -3.0 and not (3.0 <= gap < 5.0)
    # dragon2
    g_lo, g_hi = (DRAGON2_PARAMS["entry_gap_a"] if style == "a" else DRAGON2_PARAMS["entry_gap_b"])
    return g_lo <= gap <= g_hi


def evaluate_confirm(row, series_rows):
    """15:00 正式确认: dragon2 用量价, v1 用日内动量, break 无确认。"""
    strat = row.get("strategy", "dragon2")
    if not series_rows:
        return None, None, None
    last_r = series_rows[-1]
    prev_close = float(row.get("signal_price") or 0)
    if prev_close <= 0:
        return None, None, None
    d1_chg = (float(last_r["last"] or 0) / prev_close - 1) * 100
    if strat == "v1":
        extra = row.get("extra") or {}
        entry_gap = float(extra.get("entry_gap") or 0)
        intraday = d1_chg - entry_gap
        level = "weak" if (d1_chg < 0 or intraday < 3.0) else "ok"
        return level, round(d1_chg, 2), round(intraday, 2)
    # dragon2
    from app.market_cn.auto.dragon_core import DRAGON2_PARAMS
    p = DRAGON2_PARAMS
    sig_vol = float((row.get("extra") or {}).get("sig_vol") or 0)
    day_vol = float(last_r["volume"] or 0)
    d1_vol_r = day_vol / sig_vol if sig_vol > 0 else None
    vr = d1_vol_r if d1_vol_r is not None else 9.9
    if d1_chg >= p["d1_strong_chg"] and vr >= p["d1_strong_vol"]:
        level = "strong"
    elif d1_chg < 0 or (d1_chg < p["d1_weak_chg"] and vr < p["d1_weak_vol"]):
        level = "weak"
    else:
        level = "ok"
    return level, round(d1_chg, 2), round(d1_vol_r, 2) if d1_vol_r is not None else None


# ================================================================
# 出场重放 (14:58): bars + 当日合成bar, 按策略各自规则
# ================================================================

def _bars_with_synth(code, entry_date):
    """1D bars + 当日合成bar; 返回 (bars, entry_idx) 或 (None, None)。"""
    from app.market_cn.auto.dragon_scan import fetch_kline_db
    bars = fetch_kline_db(code, 200)
    if not bars:
        return None, None
    series = fetch_day_snapshots([code]).get(code)
    if not series:
        return None, None
    today = _today()
    if bars[-1]["time"] < today:
        day_open = series[0]["open"]
        day_high = max(float(r["high"] or day_open) for r in series)
        day_low = min(float(r["low"] or day_open) for r in series)
        last_r = series[-1]
        bars.append({"time": today, "open": float(day_open),
                     "high": float(day_high), "low": float(day_low),
                     "close": float(last_r["last"] or day_open),
                     "volume": float(last_r["volume"] or 0)})
    idx = None
    for i, b in enumerate(bars):
        if b["time"] == entry_date:
            idx = i
            break
    if idx is None:
        return None, None
    return bars, idx


def _eval_exit_day_close(row):
    """收盘窗口出场判定 (按策略)。返回 (reason, exit_price) 当今日触发; 否则 (None, None)。"""
    from app.market_cn.auto.dragon_core import (
        DRAGON2_PARAMS, BOARD_PARAMS, run_backtest_dragon2, get_board_type,
    )
    code = row["code"]
    strategy = row.get("strategy", "dragon2")
    board = get_board_type(code)
    is_gem = board == "gem_star"
    entry_price = float(row.get("entry_price") or 0)
    if entry_price <= 0:
        return None, None
    bars, idx = _bars_with_synth(code, row.get("entry_date"))
    if bars is None:
        return None, None
    today = _today()
    today_idx = len(bars) - 1
    held = today_idx - idx + 1
    entry_seg = bars[idx:today_idx + 1]
    peak = max(float(b["high"]) for b in entry_seg)
    last_bar = bars[-1]
    prev_bar = bars[-2] if len(bars) >= 2 else last_bar

    if strategy == "dragon2":
        p = DRAGON2_PARAMS
        stop = p["stop_gem"] if is_gem else p["stop_main"]
        trail = p["trail_gem"] if is_gem else p["trail_main"]
        extra = row.get("extra") or {}
        strong = bool(extra.get("confirm_strong"))
        hold = max(p["hold_days"], p["hold_strong"]) if strong else p["hold_days"]
        if held >= hold:
            return f"持仓到期{hold}天", last_bar["close"]
        if last_bar["low"] <= entry_price * (1 + stop / 100):
            return f"止损{stop}%", entry_price * (1 + stop / 100)
        sig_vol = float((row.get("extra") or {}).get("sig_vol") or 0)
        r = run_backtest_dragon2(
            bars, idx, entry_price, board,
            sig_close=bars[idx - 1]["close"] if idx > 0 else entry_price,
            sig_vol=sig_vol, entry_style=row.get("entry_style", "a"), params=p,
            entry_mode="confirm",
            pre_d1_chg=row.get("d1_chg"), pre_d1_vol_r=row.get("d1_vol_r"),
            pre_d1_confirm="strong" if extra.get("confirm_strong") else "ok",
            stop_at_idx=today_idx,
        )
        if r and not r.get("open"):
            exit_idx = idx + r["exit_day"] - 1
            if exit_idx == today_idx and r.get("exit_reason"):
                return r["exit_reason"], r["exit_price"]
        return None, None

    if strategy == "v1":
        stop, trail, hold = -10.0, -5.0, 7
        if last_bar["low"] <= entry_price * (1 + stop / 100):
            return f"止损{stop}%", entry_price * (1 + stop / 100)
        if held > 1 and last_bar["low"] <= peak * (1 + trail / 100):
            return f"追踪止损{trail}%", peak * (1 + trail / 100)
        if held >= hold:
            return f"持仓到期{hold}天", last_bar["close"]
        return None, None

    # break: run_backtest_breakbuy 语义 (收盘价判定)
    p = BOARD_PARAMS["gem_star" if is_gem else "main"]
    stop, trail, hold = p["stop_loss"], p["trailing_stop"], p["hold_days"]
    ret = (last_bar["close"] / entry_price - 1) * 100
    ret_from_high = (last_bar["close"] / peak - 1) * 100 if peak > 0 else 0
    if ret <= stop:
        return f"止损{stop}%", entry_price * (1 + stop / 100)
    if ret_from_high <= trail and ret > 0:
        return f"追踪止损{trail}%", peak * (1 + trail / 100)
    if ret > 10:
        bar_range = last_bar["high"] - last_bar["low"]
        upper = (last_bar["high"] - max(last_bar["open"], last_bar["close"])) / bar_range * 100 if bar_range > 0 else 0
        if upper > 40 and last_bar["close"] < last_bar["high"] * 0.98:
            return "峰值逃顶", last_bar["close"]
    if held >= hold:
        return f"持仓到期{hold}天", last_bar["close"]
    return None, None


# ================================================================
# 主 tick
# ================================================================

def run_monitor():
    """盘中 tick 主入口 (scheduler 调用)。幂等: 任何窗口重复执行不重复转移。"""
    ds.ensure_tables()
    hm = _now_hm()
    today = _today()

    pending = ds.list_signals(states=(ds.S_WATCH_PENDING,), days=8)
    buy_rows = ds.list_signals(states=(ds.S_BUY_TODAY,), days=8)
    hold_rows = ds.list_signals(states=(ds.S_HOLDING,), days=8)
    exit_rows = ds.list_signals(states=(ds.S_EXIT_TODAY,), days=8)

    stats = {"pending": len(pending), "buy": len(buy_rows),
             "holding": len(hold_rows), "exit": len(exit_rows)}

    # ── 1. 开盘窗口: 各策略 gap 过滤 → 质量排名 → 每日名额 → buy_today / expired; 隔日 pending 过期 ──
    if in_window(W_OPEN_LO, W_OPEN_HI, hm) and pending:
        target = _last_trade_day()
        cand = [r for r in pending if str(r.get("trade_date"))[:10] == target]
        stale = [r for r in pending if str(r.get("trade_date"))[:10] < target]
        for r in stale:
            ds.set_state(r["id"], ds.S_EXPIRED, detail={"reason": "隔日未处理,过期"})
        if cand:
            snaps = latest_snapshot([r["code"] for r in cand])
            from collections import defaultdict as _dd
            qualified = _dd(list)      # strategy → [(quality_key, row, open_px, gap)]
            n_exp = 0
            for r in cand:
                code = r["code"]
                snap = snaps.get(code)
                if not snap:
                    continue
                open_px = float(snap.get("open") or snap.get("last") or 0)
                if open_px <= 0:
                    continue
                prev_close = float(snap.get("previousClose") or r.get("signal_price") or 0)
                if prev_close <= 0:
                    continue
                gap = (open_px / prev_close - 1) * 100
                strat = r.get("strategy", "dragon2")
                if not _gap_buyable(code, strat, r.get("entry_style", "a"), gap):
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2),
                                         "reason": f"{ds.STRATEGY_LABELS.get(strat, strat)}开盘gap超出可买区间"})
                    n_exp += 1
                    continue
                # 质量排序键 (越 besar 越优先)
                extra = r.get("extra") or {}
                if strat == "dragon2":
                    qkey = (r.get("score", 0), extra.get("turnover_anchor") or 0,
                            1 if (gap <= -1.5 or gap >= 3.0) else 0)
                elif strat == "v1":
                    qkey = (extra.get("ret_20d") or 0,)
                else:
                    qkey = (extra.get("confirm_chg") or 0,)
                qualified[strat].append((qkey, gap, r, open_px))
            # 各策略按名额买入, 超出名额 → expired (质量排名末位淘汰)
            n_buy = 0
            for strat, lst in qualified.items():
                lst.sort(key=lambda x: x[0], reverse=True)
                limit = ds.DAILY_LIMIT_PER_STRATEGY.get(strat, 5)
                for i, (qkey, gap, r, open_px) in enumerate(lst):
                    if i < limit:
                        ds.set_state(r["id"], ds.S_BUY_TODAY,
                                     detail={"entry_gap": round(gap, 2), "rank": i + 1},
                                     entry_date=today, entry_price=round(open_px, 3))
                        ds.update_stop_price(r["id"], _entry_stop(r["code"], open_px, strat))
                        n_buy += 1
                    else:
                        ds.set_state(r["id"], ds.S_EXPIRED,
                                     detail={"reason": f"当日名额已满(质量排名第{i+1})"})
                        n_exp += 1
            stats["open_buy"] = n_buy
            stats["open_expired"] = n_exp

    # ── 2. 盘中硬止损保护 (buy_today/holding) ──
    if "09:35" <= hm < "15:00":
        guard_rows = buy_rows + hold_rows
        if guard_rows:
            snaps = latest_snapshot([r["code"] for r in guard_rows])
            for r in guard_rows:
                if r.get("exit_reason"):
                    continue
                snap = snaps.get(r["code"])
                if not snap:
                    continue
                px = float(snap.get("last") or 0)
                stop_px = float(r.get("stop_price") or 0)
                if px > 0 and stop_px > 0 and px <= stop_px:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason="盘中止损",
                                 detail={"marked": today, "stop_price": stop_px})
                    stats["intraday_stop"] = stats.get("intraday_stop", 0) + 1

    # ── 3. 14:30 预确认 (dragon2/v1 的今日买入行) ──
    if in_window(W_PRECONF_LO, W_PRECONF_HI, hm):
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys:
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            for r in today_buys:
                if (r.get("extra") or {}).get("pre_confirm"):
                    continue
                rows_ = series.get(r["code"])
                if not rows_:
                    continue
                level, chg, vr = evaluate_confirm(r, rows_)
                if level:
                    ds.set_state(r["id"], r["state"], detail={"pre_confirm": level, "pre_ts": hm})

    # ── 4. 收盘窗口: 出场重放 (holding, 按策略) ──
    if in_window(W_CLOSESIM_LO, W_CLOSESIM_HI, hm):
        for r in hold_rows:
            if r.get("exit_reason"):
                continue
            reason, price = _eval_exit_day_close(r)
            if reason:
                ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=reason,
                             exit_price=round(float(price), 3) if price else None,
                             detail={"marked": today})

    # ── 5. 正式确认 15:01+ (当日 buy_today → holding / exit_today) ──
    if hm >= W_CONFIRM_LO:
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys and snapshot_day_done():
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            for r in today_buys:
                rows_ = series.get(r["code"])
                if not rows_:
                    continue
                level, chg, vr = evaluate_confirm(r, rows_)
                strat = r.get("strategy", "dragon2")
                if level == "weak":
                    reason = ("D1日内动量<3%,D2开盘清仓" if strat == "v1"
                              else "D1弱确认,D2开盘清仓")
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, confirm_date=today,
                                 d1_chg=chg, exit_reason=reason,
                                 detail={"marked": today, "confirm": level})
                else:
                    ds.set_state(r["id"], ds.S_HOLDING, confirm_date=today,
                                 d1_chg=chg, d1_vol_r=vr,
                                 detail={"confirm": level, "confirm_strong": level == "strong"})

    # ── 6. exit_today 隔日开盘执行 (补记账) → closed ──
    if hm >= "09:30":
        for r in exit_rows:
            if r.get("exit_date"):
                continue
            marked = (r.get("extra") or {}).get("marked") or str(r.get("updated_at"))[:10]
            if marked >= today:
                continue
            snaps = latest_snapshot([r["code"]])
            snap = snaps.get(r["code"])
            if not snap:
                continue
            open_px = float(snap.get("open") or snap.get("last") or 0)
            if open_px <= 0:
                continue
            ds.set_state(r["id"], ds.S_CLOSED, exit_date=today, exit_price=round(open_px, 3))

    # ── 7. 组对账 ──
    ds.sync_watchlist_group(ds.get_active_signals())
    return stats


def run_monitor_safe():
    try:
        stats = run_monitor()
        logger.info("[dragon_monitor] tick: %s", stats)
    except Exception as e:
        logger.error("[dragon_monitor] tick 异常: %s", e, exc_info=True)


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(run_monitor(), ensure_ascii=False, indent=2))
