"""
backfill_db.py — A 股 K 线盘后覆写（mootdx 直连）

═══════════════════════════════════════════════════════════════
  架构: mootdx 直连通达信 → 写 kline 表
═══════════════════════════════════════════════════════════════

核心职责:
  1. 交易日 15:05 后覆写当日 1m bar（每标的 240 条）
  2. 交易日 15:05 后覆写当日 15m bar（每标的 16 条）
  3. 交易日 17:00 后覆写当日 1D bar（每标的 1 条）
  4. 首次运行时做历史回填

设计原则:
  1. mootdx 直连，不走 coordinator
  2. 盘后全量覆写：先拉取 → 有数据才 DELETE → INSERT
  3. 无 cn_last_update，无修复循环
  4. 对外接口: run_1d() / run_15m() / run_1m()（调度由 scheduler.py 管理）

数据说明:
  - mootdx bars() 返回的是不复权原始数据，与 kline 表存储方式一致
  - 复权因子由 app/data_sources/provider/adjustment.py 单独维护，查询时按需复权
"""

import os
from concurrent.futures import ThreadPoolExecutor, wait as _wait
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.utils.db_market import get_market_db_manager
from app.utils.logger import get_logger
from app.utils.mootdx_client import (
    get_thread_client as _get_fetch_client,
    reset_thread_client as _reset_fetch_client,
)

logger = get_logger(__name__)


TZ_CN = timezone(timedelta(hours=8))

# ================================================================
# 抓取线程池：并发拉取 + 僵尸连接隔离
# ================================================================
# 每个 worker 线程持有一条独立 mootdx 连接（thread-local），
# 单条卡死只影响该 worker，不会拖垮整个 run；卡死过多时 _recreate_executor() 重建。

def _backfill_fetch_workers() -> int:
    """并发抓取 worker 数（默认 3）。

    保守取值：通达信对单 IP 并发连接/请求频率敏感，过高可能被限流封 IP。
    3 个 worker 每条连接顺次拉 ~1700 只（全市场分割），总请求量不变、
    单台服务器只见 1~3 条连接（服务器轮转打散）。如需加速可经
    BACKFILL_FETCH_WORKERS 上调并在 .env 中持久化。
    """
    try:
        v = int(os.getenv("BACKFILL_FETCH_WORKERS", "3"))
        return v if v > 0 else 3
    except Exception:
        return 3


def _backfill_sync_timeout() -> int:
    """一次 sync_tf 全集抓取的总预算（秒），超预算未返回的 worker 按失败计。"""
    try:
        v = int(os.getenv("BACKFILL_SYNC_TIMEOUT", "1800"))
        return v if v > 0 else 1800
    except Exception:
        return 1800


_executor = ThreadPoolExecutor(max_workers=_backfill_fetch_workers(), thread_name_prefix="backfill-fetch")


def _recreate_executor() -> None:
    """重建抓取线程池：丢弃可能被卡死的旧 worker，隔离僵尸连接。"""
    global _executor
    _executor.shutdown(wait=False, cancel_futures=True)
    _executor = ThreadPoolExecutor(max_workers=_backfill_fetch_workers(), thread_name_prefix="backfill-fetch")


# ================================================================
# 通达信频率编码
# ================================================================

_TDX_FREQ = {
    "1D": 4,    # 日线
    "15m": 1,   # 15分钟
    "1m": 7,    # 1分钟 (pytdx frequency=7)
}

# 每标的一个完整交易日应有的 bar 数（用于拉取数量 + 完整性校验）
_TF_DAILY_BAR_COUNT = {
    "1D": 1,
    "15m": 16,
    "1m": 240,
}


# ================================================================
# 数据拉取
# ================================================================

def _fetch_kline(code: str, tf: str, count: int = 800) -> Optional[list]:
    """从 mootdx 拉取单标的 K 线（在 worker 线程内执行，使用线程本地连接）。

    Args:
        code: 股票代码
        tf: 周期
        count: 数量

    Returns:
        [{"time": datetime, "open": float, "high": float,
          "low": float, "close": float, "volume": float}, ...]
        失败返回 None
    """
    cli = _get_fetch_client()
    if cli is None:
        return None

    freq = _TDX_FREQ.get(tf)
    if freq is None:
        logger.error(f"[mootdx] 不支持的周期: {tf}")
        return None

    try:
        # socket 层自带 10s 超时（mootdx_client._create_client），单次 recv 停滞会自行抛错，
        # 无需外层 future 超时；卡死只会占用本 worker，不影响其它并发拉取。
        df = cli.bars(symbol=code, frequency=freq, offset=min(count, 800))
        if df is None or df.empty:
            return None

        records = []
        for _, row in df.iterrows():
            dt_val = row.get("datetime") or row.get("date")
            if dt_val is None:
                continue
            # 统一转为 datetime
            if isinstance(dt_val, str):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        dt_val = datetime.strptime(dt_val, fmt)
                        break
                    except ValueError:
                        continue
            if not isinstance(dt_val, datetime):
                continue

            o = float(row.get("open", 0))
            h = float(row.get("high", 0))
            l = float(row.get("low", 0))
            c = float(row.get("close", 0))
            v = float(row.get("vol", 0) or row.get("volume", 0))

            if o <= 0 and c <= 0:
                continue

            records.append({
                "time": dt_val,
                "open": o, "high": h, "low": l, "close": c,
                "volume": v,
            })

        return records if records else None

    except Exception as e:
        logger.warning(f"[mootdx] 拉取 {code}/{tf} 失败: {e}")
        # 连接异常时重置本线程客户端，下次自动重连
        if "connection" in str(e).lower() or "timeout" in str(e).lower():
            _reset_fetch_client()
        return None


# ================================================================
# DB 操作：DELETE + INSERT
# ================================================================

def _delete_day(bar_time: datetime, tf: str) -> int:
    """删除指定日期的 bar 数据（所有 symbol）。
    
    使用时间范围查询确保删除当日所有时间点的数据，包括 00:00:00。
    """
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    # 计算当日时间范围 [start_of_day, start_of_next_day)
    start_of_day = naive.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_next_day = start_of_day + timedelta(days=1)
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    DELETE FROM {table}
                    WHERE time >= %s AND time < %s
                """, (start_of_day, start_of_next_day))
                deleted = cur.rowcount
                conn.commit()
                return deleted
    except Exception as e:
        logger.warning(f"[同步] 删除 {tf} {naive} 失败: {e}")
        return 0


def _normalize_bar_time(dt: datetime, tf: str) -> datetime:
    """bar 时间归一化：1D 统一为当天 15:00:00（收盘时间），其它周期保留原值。"""
    if tf == "1D":
        from app.utils.db_market import normalize_1d_time
        return normalize_1d_time(dt)
    return dt


def _batch_insert(records: list, tf: str) -> int:
    """原生批量 INSERT，按年分表。"""
    if not records:
        return 0

    from app.utils.db_multi import _table_name
    from app.utils.db_market import _ensure_datetime
    from psycopg2.extras import execute_values

    # 1D bar 时间统一归一到 15:00:00（收盘时间），避免同一天 00:00/15:00 并存
    for r in records:
        r["time"] = _normalize_bar_time(r["time"], tf)

    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")

    # 按 (symbol, year) 分组
    by_sym_year = {}
    for r in records:
        sym = r.get("symbol", "_unknown")
        year = r["time"].year
        by_sym_year.setdefault((sym, year), []).append(r)

    total = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        for (sym, year), recs in by_sym_year.items():
            table = _table_name(tf, year)
            mgr.ensure_year_table("CNStock", tf, year)

            sql = f"""
                INSERT INTO "{table}" (symbol, time, open, high, low, close, volume)
                VALUES %s
                ON CONFLICT (symbol, time) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """
            values = [
                (sym, _ensure_datetime(r["time"]),
                 r["open"], r["high"], r["low"], r["close"], r["volume"])
                for r in recs
            ]
            try:
                execute_values(cur, sql, values, page_size=5000)
                total += len(values)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"[同步] 写入 {table} 失败: {e}")
        cur.close()

    return total


# ================================================================
# 同步主逻辑
# ================================================================

def _count_existing(tf: str, bar_time: datetime) -> int:
    """查询目标日期已写入的 bar 总数（用于按周期折算完整性校验）。

    按 COUNT(*)（bar 总数）而非 COUNT(DISTINCT symbol) 统计：
    若某标的只写了 1 根（老 bug 产物）也算 symbol 已覆盖，会误跳过覆写。
    """
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    # 计算当日时间范围 [start_of_day, start_of_next_day)
    start_of_day = naive.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_next_day = start_of_day + timedelta(days=1)
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE time >= %s AND time < %s
                """, (start_of_day, start_of_next_day))
                return cur.fetchone()[0]
    except Exception as e:
        logger.warning(f"[同步] 查询 {tf} 已有数据失败: {e}")
        return 0


def _get_all_symbols() -> List[str]:
    """获取全市场活跃股票代码。"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        return get_stock_basic_db().market_all_codes(status="active")
    except Exception as e:
        logger.error(f"[同步] 获取股票列表失败: {e}")
        return []


def _target_trading_day() -> str:
    """计算目标交易日（最近一个已收盘的交易日）。"""
    from app.utils.trading_calendar import last_finish_trading_day
    return last_finish_trading_day()


def sync_tf(tf: str, symbols: Optional[List[str]] = None) -> dict:
    """覆写指定周期的当日 K 线。

    流程:
      1. 计算目标交易日
      2. 检查 DB 已有数据量，> 90% 则跳过
      3. 逐标的从 mootdx 拉取
      4. 有数据才 DELETE + INSERT

    Returns:
        {"status": "ok"|"error", "written": int, "failed": int, "report": str}
    """
    if symbols is None:
        symbols = _get_all_symbols()
    if not symbols:
        return {"status": "error", "written": 0, "failed": 0, "report": "无股票列表"}

    target_td = _target_trading_day()
    bar_time = datetime.strptime(target_td, "%Y-%m-%d").replace(
        hour=15,
        minute=0,
        second=0, tzinfo=TZ_CN
    )

    total = len(symbols)
    per_day = _TF_DAILY_BAR_COUNT.get(tf, 1)
    expected = total * per_day

    # 完整性检查：按 bar 总数（symbols × 每标当日 bar 数）折算，>90% 才跳过
    # 避免「只写了 1 根/标的」的不完整数据被误判为已完成，导致 240 根覆写永不生效
    existing = _count_existing(tf, bar_time)
    if expected > 0 and existing / expected > 0.9:
        logger.info(f"[同步] {tf} 目标={target_td} 已有 {existing}/{expected} 条，跳过")
        return {"status": "ok", "written": 0, "failed": 0, "report": f"已有 {existing}/{expected}，跳过"}

    logger.info(f"[同步] {tf} 目标={target_td} 标的={total} 已有={existing} 开始覆写")

    # 逐标的拉取 + 收集（先拉完再删写，避免中途失败丢数据）
    all_records = []
    failed = 0
    failed_list = []

    # 每标的当日 bar 数：1D=1, 15m=16, 1m=240（mootdx offset 上限 800，均在其内）
    count = _TF_DAILY_BAR_COUNT.get(tf, 1)
    target_date = bar_time.strftime("%Y-%m-%d")

    # 并行提交全部标的抓取任务（worker 各自持线程本地连接，互不阻塞）
    futures = {_executor.submit(_fetch_kline, sym, tf, count): sym for sym in symbols}
    done, pending = _wait(futures, timeout=_backfill_sync_timeout())

    # 全局预算耗尽仍没返回的 worker（socket 10s 超时下极罕见）→ 按失败计，重建线程池隔离僵尸
    if pending:
        logger.error(f"[同步] {tf} {len(pending)} 个拉取超预算未返回，放弃等待并重建线程池")
        for fut in pending:
            failed += 1
            failed_list.append(futures[fut])
        _recreate_executor()

    processed = 0
    for fut in done:
        sym = futures[fut]
        try:
            records = fut.result()
        except Exception:
            records = None
        if records:
            # 过滤：只保留目标日期的数据
            filtered = [r for r in records if r["time"].strftime("%Y-%m-%d") == target_date]
            if filtered:
                for r in filtered:
                    r["symbol"] = sym
                all_records.extend(filtered)
            # 返回了数据但不是目标日期 → 停牌/退市，不算失败
        else:
            failed += 1
            failed_list.append(sym)
            # 前 5 只失败打详情，便于排查
            if failed <= 5:
                logger.warning(f"[同步] {tf} {sym} 拉取失败 (第{failed}只)")

        processed += 1
        # 进度日志（每 100 只打一次，避免长时间无输出）
        if processed % 100 == 0 or processed == total:
            logger.info(f"[同步] {tf} 进度 {processed}/{total} (成功={processed-failed} 失败={failed})")

    # 有数据才删写，避免拉取失败导致丢数据
    if not all_records:
        logger.error(f"[同步] {tf} 拉取到 0 条记录，跳过删除和写入")
        return {
            "status": "error", "written": 0, "failed": failed,
            "report": f"拉取到 0 条数据（目标 {target_td}），未删除旧数据",
        }

    deleted = _delete_day(bar_time, tf)
    if deleted > 0:
        logger.info(f"[同步] {tf} 已删除 {deleted} 条旧数据")
    written = _batch_insert(all_records, tf)

    # 统计
    ok = total - failed
    sync_rate = ok / total if total > 0 else 0
    status = "ok" if sync_rate > 0.9 else "error"

    report = f"已同步 {ok}/{total}"
    if failed_list:
        report += f"; 失败 {failed}: {','.join(failed_list[:20])}"

    logger.info(f"[同步] {tf} 完成: written={written} failed={failed} status={status}")

    return {
        "status": status,
        "written": written,
        "failed": failed,
        "report": report,
    }


# ================================================================
# 对外接口（保持兼容）
# ================================================================

def run_1d(symbols: Optional[List[str]] = None) -> dict:
    """覆写 1D，返回 {status, written, skipped}。
    
    - status: "ok" / "error"
    - written: 实际写入的记录数
    - skipped: 是否因已有数据而跳过 (>90%)
    """
    result = sync_tf("1D", symbols)
    return {
        "status": result["status"],
        "written": result["written"],
        "skipped": result["written"] == 0 and result["status"] == "ok",
    }


def run_15m(symbols: Optional[List[str]] = None) -> str:
    """覆写 15m，返回 "ok" / "error"。"""
    result = sync_tf("15m", symbols)
    return result["status"]


def run_1m(symbols: Optional[List[str]] = None) -> dict:
    """覆写 1m，返回 {status, written, skipped}。

    盘后调用，从 mootdx 拉取当日 1m K 线覆写到 kline_1m_YYYY 表。
    每标的 240 条 (4小时 × 60分钟)。
    """
    result = sync_tf("1m", symbols)
    return {
        "status": result["status"],
        "written": result["written"],
        "skipped": result["written"] == 0 and result["status"] == "ok",
    }



