"""dragon_monitor.py — 自动策略组盘中状态机 (60s tick, scheduler 调度)

Phase 3: 策略判定全部经 strategies 注册表分发, 本文件不含策略名分支:
  开盘窗口: entry_decision (gap 过滤) + quality_key (排名) + daily_limit (名额)
  盘中:     stop_price 硬止损 (通用) + exit_decision live 模式 (relay3 炸板即卖)
  15:00:    confirm_decision (dragon/break 无确认直持仓; v1 日内动量; relay3 封板判定)
  14:58:    exit_decision day_close 模式 (收盘重放, 与回测同一路径)
各策略入场/确认/出场规则详见 strategies/*.py 模块头注释。
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
from app.market_cn.auto import strategies as strat_reg

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
    """入场止损价 (通用): 注册表分发 initial_stop。"""
    s_obj = strat_reg.get_strategy(strategy)
    if s_obj is None:
        return round(entry_price * (1 - 8.0 / 100), 3)
    return s_obj.initial_stop(code, entry_price)


def _strategy_of(row):
    """取持仓行对应策略实例 (未知策略返回 None, 调用方跳过并告警)。"""
    return strat_reg.get_strategy(row.get("strategy", "dragon_callback"))


def evaluate_confirm(row, series_rows):
    """确认判定 (通用, 兼容旧签名): 注册表分发 confirm_decision, snap={"series": [...]}。

    返回 (level, chg, vr); level=None 表示无法判定。
    """
    s_obj = _strategy_of(row)
    if s_obj is None:
        return None, None, None
    dec = s_obj.confirm_decision(row, {"series": series_rows})
    if dec is None:
        return None, None, None
    level = dec.reason if dec.confirmed else "weak"
    return level, dec.d1_chg, dec.d1_vol_r


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
    """收盘窗口出场判定 (通用): 构造 day_close snap → 注册表分发 exit_decision。

    返回 ExitDecision; 数据缺失 (bars/entry_idx 拿不到) 返回 None。
    """
    s_obj = _strategy_of(row)
    if s_obj is None:
        return None
    code = row["code"]
    bars, idx = _bars_with_synth(code, row.get("entry_date"))
    if bars is None:
        return None
    return s_obj.exit_decision(row, snap={"mode": "day_close", "bars": bars, "entry_idx": idx})


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
                s_obj = strat_reg.get_strategy(strat)
                if s_obj is None:
                    logger.warning("[dragon_monitor] 未知策略 %s (row %s), 跳过", strat, r.get("id"))
                    continue
                if not s_obj.entry_decision(r, snap).buyable:
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2),
                                         "reason": f"{ds.STRATEGY_LABELS.get(strat, strat)}开盘gap超出可买区间"})
                    n_exp += 1
                    continue
                # 质量排序键 (越大越优先, 策略自定义)
                qualified[strat].append((s_obj.quality_key(r), gap, r, open_px))
            # 各策略按名额买入, 超出名额 → expired (质量排名末位淘汰)
            n_buy = 0
            for strat, lst in qualified.items():
                lst.sort(key=lambda x: x[0], reverse=True)
                limit = strat_reg.daily_limit(strat)
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

    # ── 2. 盘中硬止损保护 (buy_today/holding) + 策略盘中出场 (relay3 炸板即卖等, live 模式) ──
    if "09:35" <= hm < "15:00":
        guard_rows = buy_rows + hold_rows
        if guard_rows:
            snaps = latest_snapshot([r["code"] for r in guard_rows])
            # live 模式出场需要当日全天快照序列 (relay3 封板/炸板判定)
            series_all = fetch_day_snapshots([r["code"] for r in guard_rows])
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
                # 策略 live 出场 (relay3 S4 炸板即卖; 其它策略 live → hold)
                s_obj = _strategy_of(r)
                if s_obj is None:
                    continue
                dec = s_obj.exit_decision(r, snap={"mode": "live",
                                                   "series": series_all.get(r["code"]) or []})
                if dec.action == "exit" and dec.price:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=dec.reason,
                                 exit_price=round(float(dec.price), 3),
                                 detail={"marked": today, "intraday": True})
                    stats["live_exit"] = stats.get("live_exit", 0) + 1

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

    # ── 4. 收盘窗口: 出场重放 (holding, 注册表分发 day_close 模式) ──
    if in_window(W_CLOSESIM_LO, W_CLOSESIM_HI, hm):
        for r in hold_rows:
            if r.get("exit_reason"):
                continue
            dec = _eval_exit_day_close(r)
            if dec is not None and dec.action == "exit":
                ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=dec.reason,
                             exit_price=round(float(dec.price), 3) if dec.price else None,
                             detail={"marked": today})

    # ── 5. 正式确认 15:01+ (当日 buy_today → holding / exit_today, 注册表分发) ──
    if hm >= W_CONFIRM_LO:
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys and snapshot_day_done():
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            for r in today_buys:
                rows_ = series.get(r["code"])
                if not rows_:
                    continue
                s_obj = _strategy_of(r)
                if s_obj is None:
                    continue
                if r.get("exit_reason"):
                    # 盘中已标记出场 (止损/live) 的行不再确认 (防误转 holding)
                    continue
                dec = s_obj.confirm_decision(r, {"series": rows_})
                if dec is None:
                    continue
                if not dec.confirmed:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, confirm_date=today,
                                 d1_chg=dec.d1_chg, exit_reason=dec.reason,
                                 exit_price=round(float(dec.exit_price), 3) if dec.exit_price else None,
                                 detail={"marked": today, **(dec.detail or {})})
                else:
                    ds.set_state(r["id"], ds.S_HOLDING, confirm_date=today,
                                 d1_chg=dec.d1_chg, d1_vol_r=dec.d1_vol_r,
                                 detail=dec.detail or {})

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
