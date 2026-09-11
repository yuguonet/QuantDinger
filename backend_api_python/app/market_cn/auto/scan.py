"""scan.py (原 dragon_scan.py) — 自动策略组盘后全市场扫描

触发: scheduler Task "dragon_scan" (once_per_day, 16:30, 在 post_market_batch 1D 回填之后)
职责:
  1. 数据就绪检测 (当日 1D bar 是否已回填, 未就绪则轮询等待)
  2. 全市场逐股跑策略判定 (与回测同一份判定, core facade):
     dragon_callback(龙回头·方案2) / v1 / break(断板) / relay3(3板接力)
  3. 结果写 qd_dragon_signals (state=watch_pending, 待次日 D1 开盘处置)
  4. 历史清理 + 组对账 (组内活跃集不变, 防漂移)

手动运行:
  python -m app.market_cn.auto.scan --run [--days 320]
"""
from __future__ import annotations

import os
import time

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

_BACKEND_ROOT_DEFAULT = None  # 由 app 包上下文提供


# ================================================================
# 数据加载 (已迁 data/, 此处 import 保持调用点名字不变)
# 2026-09-10: stock_info 改走 hub (fetch_stock_info_db 已归位 data/hub.py)
# ================================================================
from app.market_cn.auto.data.hub import all_codes, stock_info as _stock_info  # noqa: E402,F401
from app.market_cn.auto.data.kline import fetch_kline_db  # noqa: E402,F401


# ================================================================
# 数据就绪检测
# ================================================================

def _target_date():
    from app.utils.trading_calendar import last_finish_trading_day
    return last_finish_trading_day()


def _data_ready(target: str) -> bool:
    """参考股 (000001) 最新 1D bar 是否已到 target 日。"""
    bars = fetch_kline_db("000001", days=20)
    return bool(bars) and bars[-1]["time"] >= target


# ================================================================
# 主扫描
# ================================================================

def run_scan(days=320, wait_data=True, max_wait_sec=3600):
    """盘后全市场扫描 (Phase 3: 注册表分发, 策略增删不改本函数)。返回摘要 dict。

    - trade_date = last_finish_trading_day()
    - 遍历注册表中 enabled 且 kind=daily_close 的策略, 统一: 判定 → U1~U4 预过滤
      (锚点=策略 prefilter_anchor) → daily_limit 截断(score降序) → 标准化行落库
    - 组对账 (活跃集不变时无操作, 防漂移)
    """
    from app.market_cn.auto import store
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.common.filters import unified_prefilter
    from app.market_cn.auto.common.market import is_limit_up, get_board_type

    strat_reg.autodiscover()
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "daily_close"}
    logger.info("[dragon_scan] 活跃策略: %s", sorted(active))

    # M1 实盘采集 (2026-09-11): 每策略一份探针存档, 判定同步过 DayTrace shim 产 sample。
    # 只记判定步落点非空的 (code,day) (纯噪声不采, 同 probe.py 约定); 标签 censored
    # (D+1 bar 当时不存在, 离线回填)。relay3 暂不支持 (scan_signals 无 probe 形参,
    # **params 静默吞掉 → 无 trace 无采样, 判定行为不受影响)。
    live_probes = None
    if strat_reg.live_probe_enabled():
        import inspect
        from app.market_cn.auto.probe import DayTrace as _DayTrace, Probe as _Probe
        live_probes = {k: _Probe(k, tag="live") for k in active}
        # probe 形参显式支持才传 (relay3 scan_signals 无 probe 形参 — **params 会静默吞掉,
        # 探针对象混进 params 有隐患; 未支持策略不传, 判定行为零变化)
        _probe_ok = {k: "probe" in inspect.signature(s.scan_signals).parameters
                     for k, s in active.items()}

    store.ensure_tables()
    target = _target_date()

    # 数据就绪等待 (仿 post_market_batch)
    if wait_data:
        waited = 0
        while not _data_ready(target):
            if waited >= max_wait_sec:
                logger.warning("[dragon_scan] 数据未就绪, 放弃本次 (target=%s)", target)
                return {"status": "data_not_ready", "target": target}
            time.sleep(300)
            waited += 300
        logger.info("[dragon_scan] 数据就绪 (target=%s)", target)

    codes = all_codes()
    try:
        stock_info = _stock_info()
    except Exception as e:
        logger.warning("[dragon_scan] stock_basic_info 加载失败(%s), 换手/市值过滤降级", e)
        stock_info = {}

    def _anchor_idx(bars, sig, strat):
        """U1~U4 锚定日索引: 'signal'=末根bar; 'limit_up'=信号 extra lu_date, 兜底最近涨停日。"""
        n = len(bars)
        if strat.prefilter_anchor == "limit_up":
            lu_date = (sig.extra or {}).get("lu_date")
            if lu_date:
                j = next((j for j, b in enumerate(bars) if b["time"] == lu_date), None)
                if j is not None:
                    return j
            board_type = get_board_type(sig.code)
            for j in range(n - 1, 0, -1):
                if is_limit_up(bars[j]["close"], bars[j - 1]["close"], board_type):
                    return j
            return None
        return n - 1

    rows = []
    t0 = time.time()
    try:
        for i, code in enumerate(codes):
            bars = fetch_kline_db(code, days)
            if not bars or len(bars) < 30:
                continue
            # 只判定 target 日 (as-of: 用到 target 收盘为止的数据)
            if bars[-1]["time"] > target:
                bars = [b for b in bars if b["time"] <= target]
            if not bars:
                continue
            name = (stock_info.get(code) or {}).get("name", "")
            for key, strat in active.items():
                day_tr = _DayTrace() if (live_probes is not None and _probe_ok.get(key)) else None
                try:
                    _kw = {"probe": day_tr} if day_tr is not None else {}
                    sigs = strat.scan_signals(bars, code, **_kw,
                                              **strat_reg.params_override(key))
                except Exception as e:
                    logger.debug("[dragon_scan] %s %s 判定异常: %s", code, key, e)
                    continue
                # U1~U4 统一预过滤 (锚点由策略声明; 易错点: 龙回头不能用缩量信号日评估, 会误杀)
                kept = []
                last_u_fails = None
                if not getattr(strat, "use_unified_prefilter", True):
                    kept = list(sigs)
                else:
                    for s in sigs:
                        idx = _anchor_idx(bars, s, strat)
                        if idx is None:
                            continue
                        ok, _fails = unified_prefilter(bars, idx, code, stock_info.get(code))
                        if ok:
                            kept.append(s)
                        else:
                            last_u_fails = _fails
                # M1 采样: 判定步有落点才记 (stage 口径镜像回测 — U1~U4 拒=prefilter,
                # 全过=signal, 其余取当日最深判定步); sig 传 dict (Signal dataclass 落盘可读)
                if live_probes is not None and _probe_ok.get(key) and (day_tr.items or sigs):
                    if sigs and not kept:
                        stage, u_fails = "prefilter", last_u_fails
                    elif kept:
                        stage, u_fails = "signal", None
                    else:
                        stage, u_fails = None, None
                    from dataclasses import asdict as _asdict
                    strat._probe_day(
                        live_probes[key], day_tr, bars, len(bars) - 1, code,
                        stock_info.get(code), stage=stage, u_fails=u_fails,
                        sig=_asdict(kept[0] if kept else sigs[0]) if sigs else None)
                rows.extend(store.signal_row(key, s, name) for s in kept)
            if (i + 1) % 500 == 0:
                logger.info("[dragon_scan] 进度 %d/%d, 信号 %d, 用时 %.0fs",
                            i + 1, len(codes), len(rows), time.time() - t0)
    finally:
        for _pr in (live_probes or {}).values():
            _pr.close()

    # 每日信号入库上限 (per-strategy 全市场口径, config.json daily_limit; score 降序截断)
    capped = []
    for key in active:
        grp = [r for r in rows if r["strategy"] == key]
        cap = strat_reg.daily_limit(key)
        if cap and len(grp) > cap:
            logger.info("[dragon_scan] %s 信号 %d 笔超限额, 截断至 %d (score降序)", key, len(grp), cap)
            grp = sorted(grp, key=lambda r: r["score"], reverse=True)[:cap]
        capped.extend(grp)
    rows = capped

    result = store.upsert_scan_signals(target, rows)
    store.sync_watchlist_group(store.get_active_signals())
    store.cleanup_old(days=15)
    logger.info("[dragon_scan] 完成: 全市场 %d 只, 信号 %d 笔 (%.0fs)",
                len(codes), result.get("written", 0), time.time() - t0)
    return {"status": "ok", "target": target, "codes": len(codes), "signals": result.get("written", 0)}


def run_scan_knife(max_wait_sec=2400, wait_data=True):
    """盘中窗口扫描 (kind=intraday_window 策略: knife_catch / tail_oversold)。

    调度: scheduler Task "knife_scan", 14:30 触发 (trading_only)。
    流程:
      1. 等待到滚动起点 (有 rolling_preview 策略=tail_oversold 时 14:50, 否则 14:56 保持旧行为)
      2. 滚动预览 (14:50~14:55): 每分钟一轮 preview 策略的完整判定
         (幂等 upsert + 本轮落选 buy_today 清理), 前端自选组实时刷新, 用户提前准备
      3. 14:56 终审: 等待 14:56 快照落地 (采集 60s 一拍, 上限 45s) → 全部策略一轮
      单轮: 全市场最新快照 → 策略 intraday_shortlist 必要条件预筛 →
            候选股补拉当日快照序列+日线 → scan_signals 完整判定 →
            落库 state=buy_today, entry_date/price=快照价, 止损价
    幂等: upsert ON CONFLICT (trade_date, strategy, code, entry_style)。
    手动: python -m ...scan --knife [--no-wait]
    """
    from app.market_cn.auto import store
    from app.market_cn.auto import strategies as strat_reg

    strat_reg.autodiscover()
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "intraday_window"}
    if not active:
        return {"status": "no_intraday_strategy"}

    store.ensure_tables()
    from app.market_cn.auto.monitor import (
        latest_snapshot, fetch_day_snapshots, _today,
    )

    # 滚动预览策略 (14:50 起每分钟重判; 无则等待起点=14:56, 与旧行为一致)
    preview = {k: s for k, s in active.items() if getattr(s, "rolling_preview", False)}
    # 起点与 scheduler 触发同源: config.json schedule 段覆盖优先 (resolve_schedule),
    # ScanSpec 默认兜底 — 否则改 config 窗口后两处事实源分叉
    from app.market_cn.auto.sched import resolve_schedule
    starts = []
    for k in preview:
        decl = resolve_schedule(k)
        if decl and decl.get("windows"):
            starts.append(decl["windows"][0])
        else:
            starts.append(active[k].scan_spec.windows[0])
    start_hm = min(starts, default="14:56")

    # ST / 北交所 通用排除 (knife 回测口径)
    try:
        stock_info = _stock_info()
    except Exception:
        stock_info = {}

    def _st_ok(code):
        nm = (stock_info.get(code) or {}).get("name", "") or ""
        return "ST" not in nm.upper()

    def _mkt_gain(snaps):
        """市场均涨幅 (as-of 最新快照; tail_oversold 仅记录不门控, knife 用作门控)。"""
        gains = []
        for s in snaps.values():
            try:
                last, pc = float(s.get("last") or 0), float(s.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last > 0 and pc > 0:
                gains.append((last / pc - 1) * 100)
        return sum(gains) / len(gains) if gains else 0.0

    def _scan_cycle(cycle_strats, snaps, preview_cycle=False):
        """一轮完整判定+落库。preview_cycle=True 时清该批策略本轮落选的 buy_today 行。"""
        today = _today()
        mkt = _mkt_gain(snaps)
        rows = []
        for key, strat in cycle_strats.items():
            params = strat_reg.params_override(key)
            shortlist = strat.intraday_shortlist(snaps, mkt, **params)
            logger.info("[knife_scan] %s 便宜预筛: %d/%d%s (mkt=%.2f%%)",
                        key, len(shortlist), len(snaps),
                        " [预览]" if preview_cycle else "", mkt)
            for code, snap in shortlist.items():
                if not _st_ok(code):
                    continue
                name = (stock_info.get(code) or {}).get("name", "")
                bars = fetch_kline_db(code, days=60)
                series = fetch_day_snapshots([code]).get(code) or []
                try:
                    sigs = strat.scan_signals(bars, code, ctx={
                        "latest": snap, "series": series, "mkt_gain": mkt,
                    }, **params)
                except Exception as e:
                    logger.debug("[knife_scan] %s %s 判定异常: %s", code, key, e)
                    continue
                for s in sigs:
                    row = store.signal_row(key, s, name)
                    row["state"] = getattr(strat, "signal_state", "watch_pending")
                    if row["state"] == "buy_today":
                        row["entry_date"] = today
                        row["entry_price"] = float(s.price or 0) or None
                        row["stop_price"] = strat.initial_stop(code, float(s.price or 0))
                    rows.append(row)
            # daily_limit (config.json; 0=不截断 — 用户裁定: 全拿优于Top3截断)
            grp = [r for r in rows if r["strategy"] == key]
            cap = strat_reg.daily_limit(key)
            if cap and len(grp) > cap:
                logger.info("[knife_scan] %s 信号 %d 笔超限额, 截断至 %d", key, len(grp), cap)
                rows = [r for r in rows if r["strategy"] != key] + \
                    sorted(grp, key=lambda r: r["score"], reverse=True)[:cap]
        # 滚动重判: 清掉本批策略上一轮命中本轮落选的 buy_today 行 (防残留误导);
        # 仅清 buy_today 态, 不碰 15:01 确认后已转移的 holding/exit 等状态
        result = store.upsert_scan_signals(
            today, rows, purge_buy_today=tuple(cycle_strats.keys()))
        store.sync_watchlist_group(store.get_active_signals())
        return result

    # 等待到滚动起点 (14:30 触发后预热等待; --no-wait 手动立即跑)
    if wait_data:
        deadline = time.time() + max_wait_sec
        while _now_hm_str() < start_hm:
            if time.time() > deadline:
                logger.warning("[knife_scan] 等待超时, 放弃本次")
                return {"status": "timeout"}
            time.sleep(30)

    today = _today()
    all_codes_list = all_codes()

    # ── 滚动预览: 14:50~14:55 每分钟一轮 (仅 preview 策略), 用户提前准备 ──
    if wait_data and preview:
        while _now_hm_str() < "14:56":
            snaps = latest_snapshot(all_codes_list)
            if snaps:
                try:
                    r = _scan_cycle(preview, snaps, preview_cycle=True)
                    logger.info("[knife_scan] 预览轮完成: %s 信号 %d 笔",
                                ",".join(preview), r.get("written", 0))
                except Exception as e:
                    logger.warning("[knife_scan] 预览轮异常(下一轮重试): %s", e)
            # 对齐到下一整分钟
            time.sleep(max(5, 60 - time.time() % 60))

    # ── 终审: 14:56 后等待新鲜快照落地 (采集 60s 一拍, 一般 <=15s, 上限 45s) ──
    snaps = latest_snapshot(all_codes_list)
    if wait_data and preview and snaps:
        fresh_cut = f"{today} 14:56"
        deadline = time.time() + 45
        while time.time() < deadline:
            latest_ts = max((str(s.get("time") or "") for s in snaps.values()), default="")
            if latest_ts >= fresh_cut:
                break
            time.sleep(5)
            snaps = latest_snapshot(all_codes_list)
    if not snaps:
        logger.warning("[knife_scan] 无快照数据, 放弃")
        return {"status": "no_snapshot"}

    t0 = time.time()
    result = _scan_cycle(active, snaps)

    logger.info("[knife_scan] 完成: 快照 %d, 信号 %d 笔 (%.0fs)",
                len(snaps), result.get("written", 0), time.time() - t0)
    return {"status": "ok", "target": today, "signals": result.get("written", 0),
            "mkt_gain": round(_mkt_gain(snaps), 3)}


def _now_hm_str():
    from app.market_cn.auto.monitor import _now_hm
    return _now_hm()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘后全市场扫描 (注册表分发, 手动)")
    parser.add_argument("--run", action="store_true", help="执行扫描")
    parser.add_argument("--days", type=int, default=320, help="向前取N个交易日")
    parser.add_argument("--no-wait", action="store_true", help="不等待数据就绪")
    parser.add_argument("--knife", action="store_true", help="执行盘中接刀扫描 (手动)")
    args = parser.parse_args()
    if args.run:
        summary = run_scan(days=args.days, wait_data=not args.no_wait)
        print(summary)
    elif args.knife:
        summary = run_scan_knife(wait_data=not args.no_wait)
        print(summary)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
