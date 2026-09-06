"""dragon_monitor.py — 自动策略组盘中状态机 (60s tick, scheduler 调度)

覆盖三策略: dragon_callback(龙回头·方案2) / v1 / break(断板) / relay3(3板接力)。
注: 龙回头Pro(dragon2, 确认制)已于 2026-09-06 下线 (信号太多无法人工复核)。

各策略入场 (09:25~09:35 开盘窗口, 用 9:26 集合竞价快照):
  dragon_callback: gap ∈ (-3%, +2%] (高开>2%不追, 低开<-3%不接; 与回测 D1 过滤一致)
  v1:      主板 gap>=-3 且 非[3,5)高开区间; 创科板 -5<=gap<5
  break:   无开盘过滤 (断板确认日本身已过 5a~5f 检查)
各策略确认 (15:01+, 当日快照收盘):
  dragon_callback: 无确认步骤 → 买入日收盘直接转持仓
  v1:      日内动量 = D1涨幅-D1开盘涨幅 < 3% 或 收阴 → 卖出(D2开盘清仓); 否则持仓
  break:   无确认步骤 → 买入次日直接持仓
各策略出场 (14:58 收盘重放 + 盘中硬止损):
  dragon_callback: 止损-8%, 分段追踪(峰值收益>=3%→-3%, 否则-8%), 峰值逃顶(涨>7%上影>30%), 到期7天
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
    from app.market_cn.auto.dragon_core import DRAGON_CB_PARAMS, get_board_type
    gem = get_board_type(code) == "gem_star"
    if strategy == "relay3":
        from app.market_cn.auto.relay3 import PARAMS as R3P
        return round(entry_price * (1 + R3P["stop_pct"] / 100), 3)
    if strategy == "v1":
        stop = -10.0
    elif strategy == "break":
        stop = -10.0 if gem else -8.0
    elif strategy == "dragon_callback":
        stop = DRAGON_CB_PARAMS["stop_loss"]   # -8%, 板块不分档 (与回测一致)
    else:
        stop = -8.0
    return round(entry_price * (1 + stop / 100), 3)


def _gap_buyable(code, strategy, style, gap):
    """开盘 gap 是否可买 (与各策略回测的 D1 过滤一致)。"""
    from app.market_cn.auto.dragon_core import DRAGON_CB_PARAMS, get_board_type
    if strategy == "relay3":
        from app.market_cn.auto.relay3 import gap_buyable as r3_gap
        return r3_gap(gap)
    if strategy == "break":
        return True
    if strategy == "v1":
        if get_board_type(code) == "gem_star":
            return -5.0 <= gap < 5.0
        return gap >= -3.0 and not (3.0 <= gap < 5.0)
    # dragon_callback: gap ∈ [d1_gap_lo, d1_gap_hi] = (-3%, +2%]
    return DRAGON_CB_PARAMS["d1_gap_lo"] <= gap <= DRAGON_CB_PARAMS["d1_gap_hi"]


def evaluate_confirm(row, series_rows):
    """15:00 正式确认: dragon_callback 无确认步骤, v1 用日内动量, break 无确认。"""
    strat = row.get("strategy", "dragon_callback")
    if not series_rows:
        return None, None, None
    last_r = series_rows[-1]
    prev_close = float(row.get("signal_price") or 0)
    if prev_close <= 0:
        return None, None, None
    d1_chg = (float(last_r["last"] or 0) / prev_close - 1) * 100
    if strat == "dragon_callback":
        # 方案2无确认步骤: 买入日收盘直接持仓 (出场由收盘重放判定)
        return "ok", round(d1_chg, 2), None
    if strat == "v1":
        extra = row.get("extra") or {}
        entry_gap = float(extra.get("entry_gap") or 0)
        intraday = d1_chg - entry_gap
        level = "weak" if (d1_chg < 0 or intraday < 3.0) else "ok"
        return level, round(d1_chg, 2), round(intraday, 2)
    # break: 无确认
    return "ok", round(d1_chg, 2), None


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
        BOARD_PARAMS, run_backtest_dragon_callback, get_board_type,
    )
    code = row["code"]
    strategy = row.get("strategy", "dragon_callback")
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

    # relay3: 收盘重放分支 (D1: 未封板→尾盘卖; 封板→持有到到期/追踪)
    if strategy == "relay3":
        from app.market_cn.auto.relay3 import PARAMS as R3P, eval_exit_day_close as r3_close
        return r3_close(bars, idx, entry_price, code, row.get("entry_date"))

    if strategy == "dragon_callback":
        # 出场重放: 复用回测出场模拟 run_backtest_dragon_callback
        # (止损-8 / 分段追踪-8/-3 / 峰值逃顶 / 到期7天; stop_at_idx=今日截断)
        r = run_backtest_dragon_callback(bars, idx, entry_price, board, stop_at_idx=today_idx)
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
                strat = r.get("strategy", "dragon_callback")
                if not _gap_buyable(code, strat, r.get("entry_style", "a"), gap):
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2),
                                         "reason": f"{ds.STRATEGY_LABELS.get(strat, strat)}开盘gap超出可买区间"})
                    n_exp += 1
                    continue
                # 质量排序键 (越 besar 越优先)
                extra = r.get("extra") or {}
                if strat == "dragon_callback":
                    # 方案2 质量排序: tech_score(参考) -> 涨停日换手率; 技术分无判别力, 主要按换手热度
                    qkey = (extra.get("tech_score") or 0, extra.get("turnover_anchor") or 0)
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

    # ── 2. 盘中硬止损保护 (buy_today/holding) + relay3 炸板即卖 ──
    if "09:35" <= hm < "15:00":
        guard_rows = buy_rows + hold_rows
        if guard_rows:
            snaps = latest_snapshot([r["code"] for r in guard_rows])
            # relay3 需要当日全天快照序列判定封板/炸板
            relay3_codes = [r["code"] for r in guard_rows if r.get("strategy") == "relay3"]
            relay3_series = fetch_day_snapshots(relay3_codes) if relay3_codes else {}
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
                    continue
                # relay3 S4: 当日曾触涨停后炸板 → 立即卖
                if r.get("strategy") == "relay3":
                    from app.market_cn.auto.relay3 import eval_exit_live
                    entry = float(r.get("entry_price") or 0)
                    series = relay3_series.get(r["code"]) or []
                    reason, price = eval_exit_live(r, series, entry)
                    if reason and price:
                        ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=reason,
                                     exit_price=round(float(price), 3),
                                     detail={"marked": today, "intraday": True})
                        stats["relay3_break_sell"] = stats.get("relay3_break_sell", 0) + 1

    # ── 3. 14:30 预确认 (v1 的今日买入行; dragon_callback/break 无确认步骤) ──
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
                strat = r.get("strategy", "dragon_callback")
                # relay3 无确认步骤: D1 封板守住 → holding (炸板/未封板已在盘中/重放转卖出)
                if strat == "relay3":
                    if r.get("exit_reason"):
                        continue
                    last_px = float(rows_[-1].get("last") or 0)
                    entry = float(r.get("entry_price") or 0)
                    from app.market_cn.auto.dragon_core import get_board_type
                    th = 0.198 if get_board_type(r["code"]) == "gem_star" else 0.098
                    limit_price = round(entry * (1 + th), 2)
                    sealed = any(float(x.get("high") or 0) >= limit_price - 0.001 for x in rows_)
                    if sealed and last_px >= limit_price * 0.995:
                        ds.set_state(r["id"], ds.S_HOLDING, confirm_date=today,
                                     d1_chg=round((last_px / entry - 1) * 100, 2),
                                     detail={"confirm": "sealed_hold"})
                    # 未封板且未触发出场: 兑底转 exit_today (尾盘卖漏网)
                    else:
                        ds.set_state(r["id"], ds.S_EXIT_TODAY, confirm_date=today,
                                     d1_chg=round((last_px / entry - 1) * 100, 2),
                                     exit_reason="S4未封板尾盘卖",
                                     exit_price=round(last_px, 3),
                                     detail={"marked": today})
                    continue
                level, chg, vr = evaluate_confirm(r, rows_)
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
