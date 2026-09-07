"""dragon_scan.py — 龙回头/盘后全市场扫描

触发: scheduler Task "dragon_scan" (once_per_day, 16:30, 在 post_market_batch 1D 回填之后)
职责:
  1. 数据就绪检测 (当日 1D bar 是否已回填, 未就绪则轮询等待)
  2. 全市场逐股跑策略判定 (与回测同一份 dragon_core):
     dragon_callback(龙回头·方案2) / v1 / break(断板) / relay3(3板接力)
  3. 结果写 qd_dragon_signals (state=watch_pending, 待次日 D1 开盘处置)
  4. 历史清理 + 组对账 (组内活跃集不变, 防漂移)

手动运行:
  python -m app.market_cn.auto.dragon_scan --run [--days 320]
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
# 数据加载 (已迁 data/kline.py, 此处 re-export 保持外部 import 路径不变)
# ================================================================
from app.market_cn.auto.data.kline import (  # noqa: E402,F401
    fetch_kline_db, fetch_stock_info_db, all_codes,
)


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
    from app.market_cn.auto import dragon_store
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.common.filters import unified_prefilter
    from app.market_cn.auto.common.market import is_limit_up, get_board_type

    strat_reg.autodiscover()
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "daily_close"}
    logger.info("[dragon_scan] 活跃策略: %s", sorted(active))

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
            try:
                sigs = strat.scan_signals(bars, code, **strat_reg.params_override(key))
            except Exception as e:
                logger.debug("[dragon_scan] %s %s 判定异常: %s", code, key, e)
                continue
            # U1~U4 统一预过滤 (锚点由策略声明; 易错点: 龙回头不能用缩量信号日评估, 会误杀)
            kept = []
            for s in sigs:
                idx = _anchor_idx(bars, s, strat)
                if idx is None:
                    continue
                ok, _fails = unified_prefilter(bars, idx, code, stock_info.get(code))
                if ok:
                    kept.append(s)
            rows.extend(dragon_store.signal_row(key, s, name) for s in kept)
        if (i + 1) % 500 == 0:
            logger.info("[dragon_scan] 进度 %d/%d, 信号 %d, 用时 %.0fs",
                        i + 1, len(codes), len(rows), time.time() - t0)

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

    result = dragon_store.upsert_scan_signals(target, rows)
    dragon_store.sync_watchlist_group(dragon_store.get_active_signals())
    dragon_store.cleanup_old(days=15)
    logger.info("[dragon_scan] 完成: 全市场 %d 只, 信号 %d 笔 (%.0fs)",
                len(codes), result.get("written", 0), time.time() - t0)
    return {"status": "ok", "target": target, "codes": len(codes), "signals": result.get("written", 0)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘后全市场扫描 (注册表分发, 手动)")
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
