"""monitor.py (原 dragon_monitor.py) — 自动策略组盘中状态机 (60s tick, scheduler 调度)

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

from app.market_cn.auto import store as ds
from app.market_cn.auto import strategies as strat_reg
# 展示档位归一 (非判定): 单独模块, 不进判定指纹 —— 见 core/display_meta.py 头注
from app.market_cn.auto.core.display_meta import confirm_level_of

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
# 快照读取 (market DB, realtime_snapshot 单表)
# ================================================================

def _snapshot_pool():
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()._get_pool("CNStock")


# 2026-09-26 由按年分表改为单表
_SNAPSHOT_TABLE = "realtime_snapshot"


def snapshot_day_done() -> bool:
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(time) FROM \"{_SNAPSHOT_TABLE}\" "
                            "WHERE time::date = CURRENT_DATE")
                r = cur.fetchone()
        if not r or r[0] is None:
            return False
        return r[0].strftime("%H:%M") >= "15:00"
    except Exception:
        return False


def fetch_day_snapshots(codes):
    """当日快照序列 {code: [rows]} —— D1 起委托 data/hub (唯一实现, 口径逐字一致)。"""
    from app.market_cn.auto.core.data.hub import day_series
    return day_series(codes)


def latest_snapshot(codes):
    """最新一拍 {code: row} —— D1 起委托 data/hub。"""
    from app.market_cn.auto.core.data.hub import market_snapshot
    return market_snapshot(codes)


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
    return strat_reg.get_strategy(row.get("strategy") or ds.DRAGON_STRATEGY)


def _hold_day(row, today):
    """持仓第几日 (1-based; 入场当日=1)。按日历日近似, 交易日精确版待 exec 层。"""
    ed = str(row.get("entry_date") or "")[:10]
    if not ed:
        return 0
    try:
        from datetime import date
        y1, m1, d1 = (int(x) for x in ed.split("-"))
        y2, m2, d2 = (int(x) for x in str(today)[:10].split("-"))
        return max(1, (date(y2, m2, d2) - date(y1, m1, d1)).days + 1)
    except Exception:
        return 2


def _t_leg_position(row, today, spec=None):
    """持仓行 → t_legs.eval_t_legs 的 position dict (含 sellable_qty)。

    A股 T+1: 入场当日 sellable=0; 其后=仓位 (signals 表无数量列, 用 extra.position_qty
    或默认 1.0 归一化仓位)。t0 市场 (spec.intraday_t0) 当日即可卖。
    """
    extra = row.get("extra") or {}
    qty = float(extra.get("position_qty") or 1.0)
    hold = _hold_day(row, today)
    entry_today = str(row.get("entry_date") or "")[:10] == str(today)[:10]
    try:
        from app.market_cn.auto.core.market import default_market
        sp = spec or default_market()
        t0 = bool(getattr(sp, "intraday_t0", False))
    except Exception:
        t0 = False
    sellable = qty if (t0 or not entry_today) else 0.0
    return {"code": row.get("code"), "qty": qty, "sellable_qty": sellable,
            "entry_price": float(row.get("entry_price") or 0), "hold_day": hold}


def _eval_t_legs(row, s_obj, today, snap, series_rows):
    """评估做T 腿 → list[dict] 意图 (只记不执行; 每日每行最多求值一轮)。

    返回空列表表示无腿/不适用。结果写入 caller (extra.t_legs_today)。
    幂等: extra.t_legs_today.date == today 时不重复求值 (60s tick 防刷)。
    """
    if s_obj is None or not getattr(s_obj, "t_legs", None) and \
            not hasattr(s_obj, "t_leg_intents"):
        return []
    extra = row.get("extra") or {}
    prev = extra.get("t_legs_today") or {}
    if str(prev.get("date") or "")[:10] == str(today)[:10]:
        return []   # 今日已评估
    hold = _hold_day(row, today)
    if hold < 1:
        return []
    pos = _t_leg_position(row, today)
    if pos["sellable_qty"] <= 0 and hold <= 1:
        return []
    # ctx: 当日快照统计 (t_hilo 等示例约定)
    from app.market_cn.auto.core.t_legs import t_constraints
    stats = None
    if snap:
        try:
            last = float(snap.get("last") or 0)
            high = float(snap.get("high") or 0)
            low = float(snap.get("low") or 0)
            pc = float(snap.get("previousClose") or 0)
            if last > 0 and pc > 0 and high > 0:
                stats = {
                    "last": last, "high": high, "low": low, "prev_close": pc,
                    "day_gain_pct": (last / pc - 1) * 100,
                    "pullback_from_high_pct": (last / high - 1) * 100 if high > 0 else 0.0,
                }
        except (TypeError, ValueError):
            stats = None
    ctx = {"snap": snap, "day": stats, "series": series_rows or [],
           "sold_today_pct": float(extra.get("t_sold_today_pct") or 0)}
    try:
        intents = s_obj.t_leg_intents(pos, ctx, hold_day=hold) or []
    except Exception as e:
        logger.warning("[dragon_monitor] t_legs 求值失败 %s/%s: %s",
                       row.get("code"), row.get("strategy"), e)
        return []
    out = [i.as_dict() if hasattr(i, "as_dict") else dict(i) for i in intents]
    return out


def evaluate_confirm(row, series_rows):
    """确认判定 (通用): 注册表分发 confirm_decision, snap={"series": [...]}。

    返回 (level, reason, chg, vr):
      level  = 展示档位 strong/ok/weak (经 core.display_meta.confirm_level_of 归一;
               None=无法判定)
      reason = 策略原始语义串 (落 extra.pre_reason 作审计, **不当档位用**)
      chg/vr = d1_chg / d1_vol_r
    """
    s_obj = _strategy_of(row)
    if s_obj is None:
        return None, None, None, None
    dec = s_obj.confirm_decision(row, {"series": series_rows})
    if dec is None:
        return None, None, None, None
    return confirm_level_of(dec), dec.reason, dec.d1_chg, dec.d1_vol_r


# ================================================================
# 出场重放 (14:58): bars + 当日合成bar, 按策略各自规则
# ================================================================

def _bars_with_synth(code, entry_date):
    """1D bars + 当日合成bar; 返回 (bars, entry_idx) 或 (None, None)。

    D1: 合成口径已上收 data/hub.daily_live (逐字一致), 此处仅保留 entry_idx 定位。
    """
    from app.market_cn.auto.core.data.hub import daily_live
    bars = daily_live(code, days=200)
    if not bars:
        return None, None
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
        # 禁用策略的存量 pending 直接过期 (09-15 事故修复: 停扫只断新信号,
        # 已入库的 pending 行此前仍会在开盘窗口被买入)
        # 2026-09-26: 批量走 store.retire_unfilled (唯一实现)。
        disabled_codes = []
        disabled_ids = []
        reason_by_key = {}
        for r in list(cand):
            strat = r.get("strategy") or ds.DRAGON_STRATEGY
            if not strat_reg.is_enabled(strat):
                disabled_ids.append(r["id"])
                disabled_codes.append(r.get("code"))
                reason_by_key[strat] = f"策略已禁用({strat}), 信号作废"
                cand.remove(r)
        if disabled_ids:
            swept = ds.retire_unfilled(ids=disabled_ids,
                                       reason_by_key=reason_by_key)
            logger.warning("[dragon_monitor] 禁用策略存量信号作废: %s (n=%d)",
                           disabled_codes, len(swept))
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
                strat = r.get("strategy") or ds.DRAGON_STRATEGY
                s_obj = strat_reg.get_strategy(strat)
                if s_obj is None:
                    logger.warning("[dragon_monitor] 未知策略 %s (row %s), 跳过", strat, r.get("id"))
                    continue
                if not s_obj.entry_decision(r, snap).buyable:
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2),
                                         "reason": f"{ds.strategy_labels().get(strat, strat)}开盘gap超出可买区间"})
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
                s_obj = _strategy_of(r)
                if s_obj is None:
                    continue
                # T+1 保护: 尾盘入场策略 (knife_catch 14:56买) 当日不可卖, 跳过当日止损/出场
                if getattr(s_obj, "entry_at_close", False) and \
                        str(r.get("entry_date") or "")[:10] == today:
                    continue
                snap = snaps.get(r["code"])
                if not snap:
                    continue
                # ── 做T 腿 (T16): 已持仓日评估, 只记意图不执行; 先于 exit 判定 ──
                if r.get("state") == ds.S_HOLDING:
                    t_intents = _eval_t_legs(
                        r, s_obj, today, snap,
                        series_all.get(r["code"]) or [])
                    if t_intents:
                        detail = {"t_legs_today": {
                            "date": today, "hm": hm, "intents": t_intents}}
                        ds.set_state(r["id"], r["state"], detail=detail)
                        stats["t_legs"] = stats.get("t_legs", 0) + len(t_intents)
                        logger.info("[dragon_monitor] 做T意图 %s/%s n=%d: %s",
                                    r.get("code"), r.get("strategy"),
                                    len(t_intents),
                                    [x.get("label") for x in t_intents])
                px = float(snap.get("last") or 0)
                stop_px = float(r.get("stop_price") or 0)
                if px > 0 and stop_px > 0 and px <= stop_px:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason="盘中止损",
                                 detail={"marked": today, "stop_price": stop_px})
                    stats["intraday_stop"] = stats.get("intraday_stop", 0) + 1
                    continue
                # 策略 live 出场 (relay3 S4 炸板即卖 / knife_catch D1开盘卖; 其它策略 live → hold)
                dec = s_obj.exit_decision(r, snap={"mode": "live",
                                                   "series": series_all.get(r["code"]) or [],
                                                   "today": today})
                if dec.action == "exit" and dec.price:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=dec.reason,
                                 exit_price=round(float(dec.price), 3),
                                 detail={"marked": today, "intraday": True})
                    stats["live_exit"] = stats.get("live_exit", 0) + 1

    # ── 3. 14:25~14:45 预确认 ("当日买入行"通用, 无策略过滤; 各策略 confirm_decision 给档位) ──
    #      pre_confirm = 归一档位 strong/ok/weak (展示用, 前端 pcMap);
    #      pre_reason  = 策略原始语义串 (仅审计, 前端不展示 —— 防 g56_hold 之类内部 token 漏进 UI)。
    #      该标记只在当日买入窗口有意义 —— 15:00 正式确认后由 step 7 统一清除, 勿在此清。
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
                level, reason, chg, vr = evaluate_confirm(r, rows_)
                if level:
                    detail = {"pre_confirm": level, "pre_ts": hm}
                    if reason:
                        detail["pre_reason"] = reason
                    ds.set_state(r["id"], r["state"], detail=detail)

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

    # ── 6. exit_today 执行平账 → closed ──
    #    默认: 隔日开盘执行 (补记账, exit_price 覆写为实际开盘价);
    #    exit_exec_same_day 策略 (knife_catch D1当日卖): 当日 14:55 后平账, 保留标记时价格
    if hm >= "09:30":
        for r in exit_rows:
            if r.get("exit_date"):
                continue
            marked = (r.get("extra") or {}).get("marked") or str(r.get("updated_at"))[:10]
            s_obj = _strategy_of(r)
            same_day = s_obj is not None and getattr(s_obj, "exit_exec_same_day", False)
            if same_day:
                if marked >= today and hm < "14:55":
                    continue      # 当日执行的行, 等到尾盘再平账
                keep_px = float(r.get("exit_price") or 0)
                ds.set_state(r["id"], ds.S_CLOSED, exit_date=today,
                             exit_price=round(keep_px, 3) if keep_px > 0 else None)
                continue
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

    # ── 7. 清理过期瞬时标记: pre_confirm/pre_ts/pre_reason 只在"当日买入行"期间有意义 ──
    #      设计口径: 14:25 加"预"角标 → **15:00 正式确认覆盖** (docs/龙回头自动化设计方案.md:92/154)。
    #      extra 是增量合并 (只加不减), 上面各出口 —— 盘中硬止损 / live 出场 / 15:01 正式确认 /
    #      未确认跨日 —— 都可能把标记留下 ⇒ 统一在此按 "state=buy_today 且 entry_date=今天"
    #      保留、其余清除。幂等 (已清理的不再匹配), 亦自愈历史脏数据。
    #      漏清的后果: 持仓行带着 pre_confirm 过夜, 显示层把它当"当前预判"渲染成"预持"。
    ds.purge_stale_detail(("pre_confirm", "pre_ts", "pre_reason"), ds.S_BUY_TODAY, today)

    # ── 8. 组对账 ──
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
