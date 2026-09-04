"""dragon_scan.py — 龙回头Pro 盘后全市场扫描

触发: scheduler Task "dragon_scan" (once_per_day, 16:30, 在 post_market_batch 1D 回填之后)
职责:
  1. 数据就绪检测 (当日 1D bar 是否已回填, 未就绪则轮询等待)
  2. 全市场逐股跑 dragon2 判定 (与回测同一份 dragon_core)
  3. 结果写 qd_dragon_signals (state=watch_pending, 待次日 D1 确认)
  4. 历史清理 + 组对账 (组内活跃集不变, 防漂移)

手动运行:
  python -m app.market_cn.auto.dragon_scan --run [--days 320]
"""
from __future__ import annotations

import os
import time
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

_BACKEND_ROOT_DEFAULT = None  # 由 app 包上下文提供


# ================================================================
# 数据加载 (与 test_dragon.fetch_kline_db 同口径: DB 1D + 前复权)
# ================================================================

def fetch_kline_db(code, days=300):
    """从 DB 加载日K (前复权), 返回 list[dict] (time/open/high/low/close/volume)。"""
    from datetime import datetime as _dt
    end = (_dt.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (_dt.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        from app.utils.db_market import get_market_kline_writer
        from app.data_sources.provider.adjustment import unadj_to_qfq
        writer = get_market_kline_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        return unadj_to_qfq([{
            "time": str(r["time"])[:10],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
        } for r in data], code)
    except Exception as e:
        logger.debug("[dragon_scan] kline %s 加载失败: %s", code, e)
        return []


def fetch_stock_info_db():
    """全量 stock_basic_info: {symbol: {name, circ_shares, ...}} (换手率/市值/ST过滤用)。"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:   # 注意: 该 pool 返回元组行 (与 test_dragon 原实现一致)
        cur.execute(
            "SELECT symbol, name, circ_shares FROM stock_basic_info WHERE status='active'"
        )
        rows = cur.fetchall()
    out = {}
    for row in rows:
        out[row[0]] = {"name": row[1] or "", "circ_shares": float(row[2] or 0)}
    return out


def all_codes():
    from app.utils.basicinfo_db import get_stock_basic_db
    return get_stock_basic_db().market_all_codes(status="active")


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
    """盘后全市场扫描。返回摘要 dict。

    - trade_date = last_finish_trading_day()
    - 信号写 signals 表 (state=watch_pending, 待次日 D1 9:26/15:00 由 monitor 处置)
    - 组对账 (活跃集不变时无操作, 防漂移)
    """
    from app.market_cn.auto.dragon_core import (
        DRAGON2_PARAMS, dragon2_today_d0_signals,
        v1_today_d0_signals, break_today_d0_signals, find_limit_ups, get_board_type,
    )
    from app.market_cn.auto import dragon_store

    dragon_store.ensure_tables()
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
        stock_info = fetch_stock_info_db()
    except Exception as e:
        logger.warning("[dragon_scan] stock_basic_info 加载失败(%s), 换手/市值过滤降级", e)
        stock_info = {}

    params = dict(DRAGON2_PARAMS)   # 自动化固定形态: 判定仅到 D0, 入场模式由 monitor 状态机执行
    rows = []
    t0 = time.time()
    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, days)
        if not bars or len(bars) < 67:
            continue
        # 只判定 target 日 (as-of: 用到 target 收盘为止的数据)
        if bars[-1]["time"] > target:
            bars = [b for b in bars if b["time"] <= target]
        if not bars:
            continue
        try:
            sigs = dragon2_today_d0_signals(bars, code, stock_info=stock_info.get(code), params=params)
        except Exception as e:
            logger.debug("[dragon_scan] %s 判定异常: %s", code, e)
            continue
        for s in sigs:
            s["strategy"] = "dragon2"
            s["name"] = (stock_info.get(code) or {}).get("name", "")
        rows.extend(sigs)

        # ── V1: D0 四因子 (涨停+强趋势+回踩+OBV+非放量) ──
        try:
            v1s = v1_today_d0_signals(bars, code)
            for s in v1s:
                s["strategy"] = "v1"
                s["style"] = "v1"
                s["score"] = int(min(99, max(0, s.get("ret_20d", 0) or 0)))
                s["signal_date"] = s.get("d0_date")
                s["signal_price"] = s.get("d0_close")
                s["name"] = (stock_info.get(code) or {}).get("name", "")
            rows.extend(v1s)
        except Exception as e:
            logger.debug("[dragon_scan] %s V1判定异常: %s", code, e)

        # ── 断板: 连板≥2 → 断板期确认 (确认日=断板期最后一天) ──
        try:
            brks = break_today_d0_signals(bars, code)
            for s in brks:
                s["strategy"] = "break"
                s["style"] = "brk"
                s["score"] = int(s.get("confirm_chg", 0) or 0) + 10
                s["signal_price"] = None
                s["name"] = (stock_info.get(code) or {}).get("name", "")
            rows.extend(brks)
        except Exception as e:
            logger.debug("[dragon_scan] %s 断板判定异常: %s", code, e)
        if (i + 1) % 500 == 0:
            logger.info("[dragon_scan] 进度 %d/%d, 信号 %d, 用时 %.0fs",
                        i + 1, len(codes), len(rows), time.time() - t0)

    result = dragon_store.upsert_scan_signals(target, rows)
    dragon_store.sync_watchlist_group(dragon_store.get_active_signals())
    dragon_store.cleanup_old(days=15)
    logger.info("[dragon_scan] 完成: 全市场 %d 只, 信号 %d 笔 (%.0fs)",
                len(codes), result.get("written", 0), time.time() - t0)
    return {"status": "ok", "target": target, "codes": len(codes), "signals": result.get("written", 0)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="龙回头Pro 盘后扫描 (手动)")
    parser.add_argument("--run", action="store_true", help="执行扫描")
    parser.add_argument("--days", type=int, default=320, help="向前取N个交易日")
    parser.add_argument("--no-wait", action="store_true", help="不等待数据就绪")
    args = parser.parse_args()
    if args.run:
        summary = run_scan(days=args.days, wait_data=not args.no_wait)
        print(summary)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
