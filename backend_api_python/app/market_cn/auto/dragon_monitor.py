"""dragon_monitor.py — 龙回头Pro 盘中状态机 (60s tick, scheduler 调度)

职责 (d1open 线上模式, 对齐回测口径):
  1. 09:25~09:35 开盘窗口: watch_pending(昨日信号) 用集合竞价价判 gap
     → 通过: state=buy_today (记 entry_price=开盘价/stop_price)  |  不通过: expired
  2. 09:35~15:00 连续 tick: 持仓/买入的盘中硬止损保护 (现价 <= stop_price → exit_today)
  3. 14:30 预确认: 用当日快照(近似收盘) 预判 强/中/弱 → extra.pre_confirm (前端"预"角标)
  4. 14:50~15:05 收盘窗口: 用 1D bars+当日合成bar 重放 run_backtest_dragon2
     → 今日触发出场 → exit_today (用户可尾盘执行); 次日开盘补执行 closed
  5. 15:01~15:20 正式确认: 当日最终快照算 d1_chg/d1_vol_r → 强/中→holding, 弱→exit_today(D2开盘清仓)
  6. exit_today 隔日 09:30+: 按当日开盘价补执行 → closed (退组)
  7. 每 tick 末尾: sync_watchlist_group 对账 (失效/平仓自动出组)

与回测的差异 (诚实标注): 回测"尾盘卖/断板卖"为当日收盘价成交,
自动化在 14:50 给出"卖出"提示供人工尾盘执行, 未执行者次日开盘按开盘价记账。
"""
from __future__ import annotations

import os
import json
from datetime import datetime, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 手动独立运行时加载 .env (应用内运行由 app 初始化加载, 幂等无害)
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

# 时间窗 (本地时间, 分钟粒度)
W_OPEN_LO, W_OPEN_HI = "09:25", "09:35"       # 开盘 gap 判定窗 (用 9:26 集合竞价快照)
W_PRECONF_LO, W_PRECONF_HI = "14:25", "14:45"  # 预确认窗 (约 14:30)
W_CLOSESIM_LO, W_CLOSESIM_HI = "14:58", "15:06"  # 收盘出场重放窗 (防抖动: 近收盘才判)
W_CONFIRM_LO = "15:01"                          # 正式确认窗起点 (需 snapshot_day_done)
TICK_YEAR_GUARD = 1  # 快照表年份


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _last_trade_day() -> str:
    try:
        from app.utils.trading_calendar import last_finish_trading_day
        return last_finish_trading_day()
    except Exception:
        return (_today())

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


def snapshot_freshness() -> float:
    """距最新快照的秒数; 无数据返回 1e9。"""
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(time) FROM \"{_snapshot_table()}\" "
                            "WHERE time::date = CURRENT_DATE")
                r = cur.fetchone()
        if not r or r[0] is None:
            return 1e9
        delta = datetime.now() - r[0]
        return delta.total_seconds() if hasattr(r[0], "replace") else 1e9
    except Exception as e:
        logger.debug("[dragon_monitor] freshness 查询失败: %s", e)
        return 1e9


def fetch_day_snapshots(codes):
    """取 codes 当日全部快照行 (分时重构: open/high/low/last/volume 累计)。"""
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


def snapshot_day_done() -> bool:
    """当日快照是否已到收盘 (>= 15:00) —— 正式确认的前置条件 (盘后新鲜度会自然变差, 不能用 freshness)。"""
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


def latest_snapshot(codes):
    """每 code 最新一行 (含当日累计 volume)。"""
    if not codes:
        return {}
    series = fetch_day_snapshots(codes)
    out = {}
    for code, rows in series.items():
        if rows:
            out[code] = rows[-1]
    return out


# ================================================================
# 状态转移执行
# ================================================================

def _board_stop(code, entry_price):
    from app.market_cn.auto.dragon_core import DRAGON2_PARAMS, get_board_type
    p = DRAGON2_PARAMS
    gem = get_board_type(code) == "gem_star"
    stop = p["stop_gem"] if gem else p["stop_main"]
    return round(entry_price * (1 + stop / 100), 3)


def _run_day_close_sim(row):
    """用 1D bars + 当日合成bar 重放出场规则。
    返回 (exit_reason, exit_price) 当今日触发出场; 否则 (None, None)。
    合成bar: open=当日首个快照open, high/low=max/min, close=最后last, volume=累计。
    """
    from app.market_cn.auto.dragon_core import (
        DRAGON2_PARAMS, run_backtest_dragon2, get_board_type,
    )
    from app.market_cn.auto.dragon_scan import fetch_kline_db

    code = row["code"]
    bars = fetch_kline_db(code, 200)
    if not bars:
        return None, None
    series = fetch_day_snapshots([code]).get(code)
    if not series:
        return None, None
    # 当日合成 bar (若 1D 已含当日则跳过合成)
    today = _today()
    if bars[-1]["time"] >= today:
        return None, None
    day_open = series[0]["open"]
    day_high = max(float(r["high"] or day_open) for r in series)
    day_low = min(float(r["low"] or day_open) for r in series)
    last_r = series[-1]
    day_close = float(last_r["last"] or day_open)
    day_vol = float(last_r["volume"] or 0)
    bars.append({"time": today, "open": float(day_open), "high": float(day_high),
                 "low": float(day_low), "close": day_close, "volume": day_vol})

    entry_date = row.get("entry_date")
    idx = None
    for i, b in enumerate(bars):
        if b["time"] == entry_date:
            idx = i
            break
    if idx is None:
        return None, None
    extra = row.get("extra") or {}
    p = DRAGON2_PARAMS
    sig_vol = float(extra.get("sig_vol") or 0)
    # 若用 1D 系列已含 entry 日之前数据, sig_close 取 entry 前一根 close
    sig_close = bars[idx - 1]["close"] if idx > 0 else float(row.get("signal_price") or 0)
    r = run_backtest_dragon2(
        bars, idx, float(row["entry_price"]), get_board_type(code),
        sig_close=sig_close, sig_vol=sig_vol,
        entry_style=row.get("entry_style", "a"), params=p,
        entry_mode="confirm",
        pre_d1_chg=row.get("d1_chg"), pre_d1_vol_r=row.get("d1_vol_r"),
        pre_d1_confirm="strong" if (row.get("extra") or {}).get("confirm_strong") else "ok",
    )
    if r is None or r.get("open"):
        return None, None
    exit_idx = idx + r["exit_day"] - 1
    if exit_idx == len(bars) - 1 and r.get("exit_reason"):
        return r["exit_reason"], r["exit_price"]
    return None, None


def evaluate_confirm(row, series_rows):
    """正式/预确认判定 (与 _dragon2_d1_confirm 同阈值)。
    series_rows: 当日快照序列 (可含截至当前)。返回 (level, d1_chg, d1_vol_r)。
    """
    from app.market_cn.auto.dragon_core import DRAGON2_PARAMS
    if not series_rows:
        return None, None, None
    p = DRAGON2_PARAMS
    last_r = series_rows[-1]
    prev_close = float(row.get("signal_price") or 0)
    if prev_close <= 0:
        return None, None, None
    d1_chg = (float(last_r["last"] or 0) / prev_close - 1) * 100
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
# 主 tick
# ================================================================

def run_monitor():
    """盘中 tick 主入口 (scheduler 调用)。幂等: 任何窗口重复执行不重复转移。"""
    ds.ensure_tables()
    hm = _now_hm()
    today = _today()

    # ── 1. 取候选行 ──
    pending = ds.list_signals(states=(ds.S_WATCH_PENDING,), days=8)
    buy_rows = ds.list_signals(states=(ds.S_BUY_TODAY,), days=8)
    hold_rows = ds.list_signals(states=(ds.S_HOLDING,), days=8)
    exit_rows = ds.list_signals(states=(ds.S_EXIT_TODAY,), days=8)

    stats = {"pending": len(pending), "buy": len(buy_rows), "holding": len(hold_rows),
             "exit": len(exit_rows)}

    # ── 2. 开盘窗口: gap 判定 → buy_today / expired; 隔日 pending 过期清理 ──
    if in_window(W_OPEN_LO, W_OPEN_HI, hm) and pending:
        target = _last_trade_day()
        cand = [r for r in pending if str(r.get("trade_date"))[:10] == target]
        stale = [r for r in pending if str(r.get("trade_date"))[:10] < target]
        for r in stale:   # 错过处理窗口的观察票 → 过期 (不追买)
            ds.set_state(r["id"], ds.S_EXPIRED, detail={"reason": "隔日未处理,过期"})
        if cand:
            snaps = latest_snapshot([r["code"] for r in cand])
            n_buy = n_exp = 0
            for r in cand:
                code = r["code"]
                snap = snaps.get(code)
                if not snap:
                    continue
                open_px = float(snap.get("open") or snap.get("last") or 0)
                if open_px <= 0:
                    open_px = float(snap.get("last") or 0)
                if open_px <= 0:
                    continue
                prev_close = float(snap.get("previousClose") or r.get("signal_price") or 0)
                if prev_close <= 0:
                    continue
                gap = (open_px / prev_close - 1) * 100
                style = r.get("entry_style", "a")
                from app.market_cn.auto.dragon_core import DRAGON2_PARAMS
                g_lo, g_hi = (DRAGON2_PARAMS["entry_gap_a"] if style == "a"
                              else DRAGON2_PARAMS["entry_gap_b"])
                if g_lo <= gap <= g_hi:
                    ds.set_state(r["id"], ds.S_BUY_TODAY, detail={"entry_gap": round(gap, 2)},
                                 entry_date=today, entry_price=round(open_px, 3))
                    # 补 stop_price
                    ds.update_stop_price(r["id"], _board_stop(code, open_px))
                    n_buy += 1
                else:
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2), "reason": "开盘gap超出可买区间"})
                    n_exp += 1
            stats["open_buy"] = n_buy
            stats["open_expired"] = n_exp

    # ── 3. 盘中硬止损保护 (buy_today/holding) ──
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
                    stats.setdefault("intraday_stop", 0)
                    stats["intraday_stop"] += 1

    # ── 4. 14:30 预确认 (buy_today, 今日的) ──
    if in_window(W_PRECONF_LO, W_PRECONF_HI, hm):
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys:
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            for r in today_buys:
                if (r.get("extra") or {}).get("pre_confirm"):
                    continue
                rows = series.get(r["code"])
                if not rows:
                    continue
                level, chg, vr = evaluate_confirm(r, rows)
                if level:
                    ds.set_state(r["id"], r["state"],
                                 detail={"pre_confirm": level, "pre_ts": hm})

    # ── 5. 收盘窗口: 出场重放 (holding) → exit_today ──
    if in_window(W_CLOSESIM_LO, W_CLOSESIM_HI, hm):
        if hold_rows:
            for r in hold_rows:
                if r.get("exit_reason"):
                    continue
                reason, price = _run_day_close_sim(r)
                if reason:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=reason,
                                 exit_price=round(float(price), 3) if price else None,
                                 detail={"marked": today})

    # ── 6. 正式确认 (buy_today → holding / exit_today), 需当日快照已到收盘 ──
    if hm >= W_CONFIRM_LO:
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys and snapshot_day_done():
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            for r in today_buys:
                rows = series.get(r["code"])
                if not rows:
                    continue
                level, chg, vr = evaluate_confirm(r, rows)
                if level == "weak":
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, confirm_date=today,
                                 d1_chg=chg, d1_vol_r=vr, exit_reason="D1弱确认,D2开盘清仓",
                                 detail={"marked": today, "confirm": level})
                else:
                    ds.set_state(r["id"], ds.S_HOLDING, confirm_date=today,
                                 d1_chg=chg, d1_vol_r=vr,
                                 detail={"confirm": level, "confirm_strong": level == "strong"})

    # ── 7. exit_today 隔日开盘执行 (补记账) → closed ──
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

    # ── 8. 组对账 (自动入组/出组) ──
    ds.sync_watchlist_group(ds.get_active_signals())
    return stats


# 供 scheduler 使用的无返回值包装
def run_monitor_safe():
    try:
        stats = run_monitor()
        logger.info("[dragon_monitor] tick: %s", stats)
    except Exception as e:
        logger.error("[dragon_monitor] tick 异常: %s", e, exc_info=True)


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(run_monitor(), ensure_ascii=False, indent=2))
