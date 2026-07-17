"""
backfill_db.py — A 股 K 线盘后覆写（mootdx 直连）

═══════════════════════════════════════════════════════════════
  架构: mootdx 直连通达信 → 写 kline 表
═══════════════════════════════════════════════════════════════

核心职责:
  1. 交易日 15:05 后覆写当日 15m bar（每标的 16 条）
  2. 交易日 17:00 后覆写当日 1D bar（每标的 1 条）
  3. 首次运行时做历史回填

设计原则:
  1. mootdx 直连，不走 coordinator
  2. 盘后全量覆写：先拉取 → 有数据才 DELETE → INSERT
  3. 无 cn_last_update，无修复循环
  4. 对外接口: run_1d() / run_15m()（调度由 scheduler.py 管理）

数据说明:
  - mootdx bars() 返回的是不复权原始数据，与 kline 表存储方式一致
  - 复权因子由 app/data_sources/provider/adjustment.py 单独维护，查询时按需复权
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.utils.db_market import get_market_db_manager
from app.utils.logger import get_logger

logger = get_logger(__name__)


TZ_CN = timezone(timedelta(hours=8))

# ================================================================
# mootdx 客户端（单例，复用连接）
# ================================================================

_client = None
_client_lock = threading.Lock()


def _get_client():
    """获取 mootdx 客户端单例。"""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from mootdx.quotes import Quotes
            _client = Quotes.factory(market='std', bestip=True)
            logger.info("[mootdx] 连接成功")
        except Exception as e:
            logger.error(f"[mootdx] 连接失败: {e}")
            return None
    return _client


# ================================================================
# 通达信频率编码
# ================================================================

_TDX_FREQ = {
    "1D": 4,    # 日线
    "15m": 1,   # 15分钟
}


# ================================================================
# 数据拉取
# ================================================================

def _fetch_kline(code: str, tf: str, count: int = 800) -> Optional[list]:
    """从 mootdx 拉取单标的 K 线。

    Returns:
        [{"time": datetime, "open": float, "high": float,
          "low": float, "close": float, "volume": float}, ...]
        失败返回 None
    """
    cli = _get_client()
    if cli is None:
        return None

    freq = _TDX_FREQ.get(tf)
    if freq is None:
        logger.error(f"[mootdx] 不支持的周期: {tf}")
        return None

    try:
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
        # 连接异常时重置客户端，下次自动重连
        global _client
        if "connection" in str(e).lower() or "timeout" in str(e).lower():
            _client = None
        return None


# ================================================================
# DB 操作：DELETE + INSERT
# ================================================================

def _delete_day(bar_time: datetime, tf: str) -> int:
    """删除指定日期的 bar 数据（所有 symbol）。"""
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    DELETE FROM {table}
                    WHERE time::date = %s::date
                """, (naive,))
                deleted = cur.rowcount
                conn.commit()
                return deleted
    except Exception as e:
        logger.warning(f"[同步] 删除 {tf} {naive} 失败: {e}")
        return 0


def _batch_insert(records: list, tf: str) -> int:
    """原生批量 INSERT，按年分表。"""
    if not records:
        return 0

    from app.utils.db_multi import _table_name
    from app.utils.db_market import _ensure_datetime
    from psycopg2.extras import execute_values

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
    """查询目标日期已有多少个 symbol 的数据。"""
    table = f'"kline_{tf}_{bar_time.year}"'
    naive = bar_time.replace(tzinfo=None) if bar_time.tzinfo else bar_time
    try:
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT COUNT(DISTINCT symbol) FROM {table}
                    WHERE time::date = %s::date
                """, (naive,))
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
        hour=15 if tf == "15m" else 0,
        minute=0 if tf == "15m" else 0,
        second=0, tzinfo=TZ_CN
    )

    total = len(symbols)

    # 检查是否已写入，>90% 跳过，避免节假日重复覆写
    existing = _count_existing(tf, bar_time)
    if total > 0 and existing / total > 0.9:
        logger.info(f"[同步] {tf} 目标={target_td} 已有 {existing}/{total} 条，跳过")
        return {"status": "ok", "written": 0, "failed": 0, "report": f"已有 {existing}/{total}，跳过"}

    logger.info(f"[同步] {tf} 目标={target_td} 标的={total} 已有={existing} 开始覆写")

    # 逐标的拉取 + 收集（先拉完再删写，避免中途失败丢数据）
    all_records = []
    failed = 0
    failed_list = []

    count = 16 if tf == "15m" else 1
    target_date = bar_time.strftime("%Y-%m-%d")
    for i, sym in enumerate(symbols):
        records = _fetch_kline(sym, tf, count=count)
        if records:
            # 过滤：只保留目标日期的数据
            filtered = [r for r in records if r["time"].strftime("%Y-%m-%d") == target_date]
            if filtered:
                for r in filtered:
                    r["symbol"] = sym
                all_records.extend(filtered)
            else:
                # mootdx 返回了数据但不是目标日期 → 停牌/退市，跳过
                failed += 1
                failed_list.append(sym)
        else:
            failed += 1
            failed_list.append(sym)

        # 进度日志
        if (i + 1) % 500 == 0:
            logger.info(f"[同步] {tf} 进度 {i+1}/{total}")

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

def run_1d(symbols: Optional[List[str]] = None) -> str:
    """覆写 1D，返回 "ok" / "error"。"""
    result = sync_tf("1D", symbols)
    return result["status"]


def run_15m(symbols: Optional[List[str]] = None) -> str:
    """覆写 15m，返回 "ok" / "error"。"""
    result = sync_tf("15m", symbols)
    return result["status"]



