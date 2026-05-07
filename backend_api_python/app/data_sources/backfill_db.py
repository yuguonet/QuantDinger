"""
backfill_db.py — 全市场 15m K 线后台回填

职责:
  后台线程定时拉取全市场 A 股的 15m K 线数据，写入 DB。
  与 cn_stock.py 的"惰性单只刷新"互补 —— 那里是被动触发，
  这里是主动全量覆盖，确保 DB 里始终有最新的全市场数据。

写入保障:
  1. 时间校准 — bar.time 必须对齐到 15m 边界（整除 900）
  2. 先删后写 — 写入前删除该 symbol 当日/该时段的旧数据，避免脏数据残留
  3. 删除未来数据 — time > now 的错误数据一律清除
  4. 唯一性 — 依赖 (symbol, time) 唯一约束 + 先删后写双重保障
  5. 字段补全 — volume 缺失时填 0，OHLC 缺失时跳过该 bar
  6. 合理性校验 — high >= low, high >= open/close, low <= open/close

启动方式:
    from app.data_sources.backfill_db import start_backfill
    start_backfill()   # 启动后台守护线程

    # 或手动跑一轮:
    from app.data_sources.backfill_db import run_once
    run_once()
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.coordinator import get_coordinator
from app.data_sources.normalizer import normalize_cn_code
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ================================================================
# 配置
# ================================================================

# 每批拉取的股票数量
BATCH_SIZE = 400

# 每只股票拉取的 15m bar 数量（16 根 ≈ 1 个交易日，4h×4×1）
BARS_PER_STOCK = 16

# 盘中刷新间隔（秒）— 15 分钟，与 15m bar 周期对齐
INTERVAL_TRADING = 15 * 60

# 盘后刷新间隔（秒）— 2 小时，只需跑一轮历史补全
INTERVAL_CLOSED = 2 * 3600

# Coordinator 总超时（秒）
COORD_TIMEOUT = 25

# 启动后首次延迟（秒）— 等系统初始化完成
STARTUP_DELAY = 30

# 15m 周期秒数
_15M_SEC = 900

_TZ_CN = timezone(timedelta(hours=8))

_MORNING_BARS = [(9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30)]
_AFTERNOON_BARS = [(13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45), (15, 0)]
# ================================================================
# 工具函数
# ================================================================

def _is_market_hours() -> bool:
    """判断当前是否在 A 股交易时段（9:15 ~ 15:00）。"""
    try:
        from app.utils.trading_calendar import is_trading_day_today
        if not is_trading_day_today():
            return False
    except Exception:
        pass
    now = datetime.now(_TZ_CN)
    t = now.time()
    return dt_time(9, 15) <= t <= dt_time(15, 0)


def _today_ts() -> int:
    """今天 00:00 的 Unix 时间戳（秒）。"""
    dt = datetime.now(_TZ_CN).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


# ================================================================
# 时间校准 + 数据清洗
# ================================================================

def _align_to_15m(ts: int) -> int:
    """将时间戳向下对齐到 15 分钟边界。

    例: 1715040300 (10:05:xx) → 1715040000 (10:00:00)
         1715041200 (10:20:xx) → 1715041200 (10:20:00, 已对齐)
    """
    return ts - (ts % _15M_SEC)


def _validate_bar(bar: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验并清洗单根 K 线。

    校验规则:
      - time > 0 且已对齐到 15m 边界
      - open/high/low/close > 0（缺失则跳过该 bar）
      - high >= low
      - high >= max(open, close)
      - low <= min(open, close)
      - volume 缺失时填 0

    Returns:
        清洗后的 bar，校验失败返回 None。
    """
    ts = bar.get("time", 0)
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None

    # 对齐到 15m 边界
    ts = _align_to_15m(int(ts))
    if ts <= 0:
        return None

    o = bar.get("open", 0)
    h = bar.get("high", 0)
    l = bar.get("low", 0)
    c = bar.get("close", 0)
    v = bar.get("volume", 0)

    # 转 float，缺失/异常值处理
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
        v = float(v) if v is not None and str(v).strip() not in ("", "-", "nan") else 0.0
    except (TypeError, ValueError):
        return None

    # OHLC 必须 > 0
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return None

    # 合理性校验: high >= low, high >= max(o,c), low <= min(o,c)
    if h < l:
        h, l = l, h  # 自动修正，不丢弃
    if h < max(o, c):
        h = max(o, c)
    if l > min(o, c):
        l = min(o, c)

    return {
        "time": ts,
        "open": round(o, 4),
        "high": round(h, 4),
        "low": round(l, 4),
        "close": round(c, 4),
        "volume": round(max(v, 0), 2),  # volume 不能为负
    }


def _clean_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量清洗 K 线: 校验 + 去重 + 排序。

    去重策略: 同一 timestamp 出现多次，保留最后一条（最新数据覆盖旧数据）。
    """
    seen: Dict[int, Dict[str, Any]] = {}
    for bar in bars:
        cleaned = _validate_bar(bar)
        if cleaned is None:
            continue
        ts = cleaned["time"]
        # 同 timestamp 覆盖（后到的视为更新）
        seen[ts] = cleaned

    result = sorted(seen.values(), key=lambda b: b["time"])
    return result


# ================================================================
# DB 操作
# ================================================================

def _init_writer():
    """惰性初始化 db_market writer，失败返回 None。"""
    try:
        from app.utils.db_market import get_market_db_manager, get_market_kline_writer
        mgr = get_market_db_manager()
        mgr.ensure_market_db("CNStock")
        return get_market_kline_writer()
    except Exception as e:
        logger.error(f"[全量回填] DB 初始化失败: {e}")
        return None


def _get_pool(writer):
    """从 writer 获取连接池。"""
    return writer._mgr._get_pool("CNStock")


def _table_name_for_year(year: int) -> str:
    """15m 分区表名。"""
    return f"kline_15m_{year}"


def _delete_range(writer, symbol: str, start_ts: int, end_ts: int):
    """删除指定 symbol 在 [start_ts, end_ts) 时间范围内的旧数据。

    写入前调用，确保不会有脏数据残留。
    """
    pool = _get_pool(writer)
    start_dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts)

    # 可能跨年，按年分表删除
    years = set()
    y = start_dt.year
    while y <= end_dt.year:
        years.add(y)
        y += 1

    total_deleted = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        for year in years:
            table = _table_name_for_year(year)
            try:
                cur.execute(f"""
                    DELETE FROM "{table}"
                    WHERE symbol = %s AND time >= %s AND time < %s
                """, (symbol, start_dt, end_dt))
                total_deleted += cur.rowcount
            except Exception:
                pass  # 表不存在时忽略
        conn.commit()

    if total_deleted > 0:
        logger.debug(f"[全量回填] 删除旧数据 {symbol}: {total_deleted} 条")


def _delete_future_data(writer, symbol: str):
    """删除该 symbol 中 time > 当前时间的错误数据。

    远端数据偶尔会返回未来时间戳（时区错误等），必须清理。
    """
    pool = _get_pool(writer)
    now_dt = datetime.now()

    years = set()
    y = now_dt.year - 1
    while y <= now_dt.year + 1:
        years.add(y)
        y += 1

    total_deleted = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        for year in years:
            table = _table_name_for_year(year)
            try:
                cur.execute(f"""
                    DELETE FROM "{table}"
                    WHERE symbol = %s AND time > %s
                """, (symbol, now_dt))
                total_deleted += cur.rowcount
            except Exception:
                pass
        conn.commit()

    if total_deleted > 0:
        logger.debug(f"[全量回填] 删除未来数据 {symbol}: {total_deleted} 条")


def _safe_upsert(writer, symbol: str, bars: List[Dict[str, Any]]):
    """安全写入: 先删后写，保证唯一性和准确性。

    流程:
      1. 清洗 + 校验 bars
      2. 计算这批 bars 的时间范围 [min_ts, max_ts]
      3. 删除 DB 中该 symbol 在此范围内的旧数据
      4. 删除该 symbol 的未来数据（time > now）
      5. upsert 写入新数据

    这样即使远端返回的 bar 时间范围与 DB 中有重叠，
    也不会出现重复或残留。
    """
    cleaned = _clean_bars(bars)
    if not cleaned:
        return {"inserted": 0, "cleaned": 0, "raw": len(bars)}

    min_ts = cleaned[0]["time"]
    max_ts = cleaned[-1]["time"]

    # 构造 datetime records（DB 列是 TIMESTAMP WITHOUT TIME ZONE）
    records = []
    for b in cleaned:
        records.append({
            "time": datetime.fromtimestamp(b["time"]),
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b["volume"],
        })

    # Step 1: 删除该时间范围内的旧数据
    _delete_range(writer, symbol, min_ts, max_ts + _15M_SEC)

    # Step 2: 删除未来数据
    _delete_future_data(writer, symbol)

    # Step 3: 写入新数据
    try:
        result = writer.upsert("CNStock", symbol, "15m", records)
        return {
            "inserted": result.get("inserted", 0),
            "updated": result.get("updated", 0),
            "errors": result.get("errors", 0),
            "cleaned": len(cleaned),
            "raw": len(bars),
        }
    except Exception as e:
        logger.warning(f"[全量回填] upsert 失败 {symbol}: {e}")
        return {"inserted": 0, "cleaned": len(cleaned), "raw": len(bars), "error": str(e)}


# ================================================================
# 全市场股票代码获取
# ================================================================

def _get_all_codes() -> List[str]:
    """获取全市场 A 股纯 6 位数字代码列表。

    优先从 AStockDataSource 走缓存（24h TTL），
    失败则直接调东财 API。
    """
    try:
        from app.data_sources.a_stock import AStockDataSource
        ds = AStockDataSource()
        raw_list = ds.get_all_stock_codes()
        codes = []
        for item in raw_list:
            code = str(item.get("stock_code", "")).strip()
            if code and len(code) == 6 and code.isdigit():
                codes.append(code)
        if codes:
            logger.info(f"[全量回填] 获取股票列表: {len(codes)} 只")
            return codes
    except Exception as e:
        logger.warning(f"[全量回填] AStockDataSource 获取列表失败: {e}")

    # fallback: 直接调东财
    try:
        import requests
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": 1, "pz": 6000, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f12",
            },
            timeout=15,
        )
        items = ((resp.json() or {}).get("data") or {}).get("diff", [])
        codes = []
        for item in items:
            c = str(item.get("f12", "")).strip()
            if c and len(c) == 6 and c.isdigit():
                codes.append(c)
        logger.info(f"[全量回填] 东财直接获取股票列表: {len(codes)} 只")
        return codes
    except Exception as e:
        logger.error(f"[全量回填] 东财获取列表失败: {e}")
        return []


# ================================================================
# 单批拉取 + 写入
# ================================================================

def _backfill_batch(
    codes: List[str],
    writer,
    cb,
    in_trading: bool,
) -> Tuple[int, int, int, int]:
    """拉取一批股票的 15m 数据并写入 DB。

    Returns:
        (成功数, 失败数, 跳过数, 清洗丢弃数)
    """
    coord = get_coordinator()
    normalized = [normalize_cn_code(c) for c in codes]

    coord_results, failed = coord.coordinate_kline(
        symbols=normalized,
        timeframe="15m",
        limit=BARS_PER_STOCK,
        cb=cb,
        market="CNStock",
        timeout=COORD_TIMEOUT,
        adj="qfq",
    )

    success = 0
    skip = 0
    dirty = 0
    today_start = _today_ts() if in_trading else 0

    for code in codes:
        raw = code  # 已是纯 6 位
        normalized_code = normalize_cn_code(code)
        bars = coord_results.get(normalized_code, [])

        if not bars:
            skip += 1
            continue

        # 盘中只写当日 bar；盘后写全量
        if in_trading:
            bars = [b for b in bars if b.get("time", 0) >= today_start]
            if not bars:
                skip += 1
                continue

        raw_count = len(bars)
        result = _safe_upsert(writer, raw, bars)
        cleaned_count = result.get("cleaned", 0)
        dropped = raw_count - cleaned_count
        if dropped > 0:
            dirty += dropped

        if cleaned_count > 0:
            success += 1
        else:
            skip += 1

    fail_count = len(failed)
    return success, fail_count, skip, dirty


# ================================================================
# 主循环
# ================================================================

def run_once() -> Dict[str, int]:
    """执行一轮全市场 15m 回填。

    Returns:
        {"total": 总数, "success": 成功, "failed": 失败, "skipped": 跳过,
         "dirty": 清洗丢弃, "batches": 批次数, "elapsed": 耗时秒数}
    """
    start_time = time.time()

    writer = _init_writer()
    if not writer:
        logger.error("[全量回填] DB 不可用，跳过本轮")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                "dirty": 0, "batches": 0, "elapsed": 0}

    codes = _get_all_codes()
    if not codes:
        logger.warning("[全量回填] 无股票代码，跳过本轮")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                "dirty": 0, "batches": 0, "elapsed": 0}

    cb = get_realtime_circuit_breaker()
    in_trading = _is_market_hours()
    total_success = 0
    total_failed = 0
    total_skipped = 0
    total_dirty = 0
    batch_count = 0

    total = len(codes)
    logger.info(
        f"[全量回填] 开始: {total} 只, 批次大小={BATCH_SIZE}, "
        f"{'盘中' if in_trading else '盘后'}"
    )

    for i in range(0, total, BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        try:
            s, f, sk, d = _backfill_batch(batch, writer, cb, in_trading)
            total_success += s
            total_failed += f
            total_skipped += sk
            total_dirty += d
            batch_count += 1

            logger.info(
                f"[全量回填] 批次 {batch_num}/{total_batches}: "
                f"成功={s} 失败={f} 跳过={sk} 脏数据={d}"
            )
        except Exception as e:
            logger.error(
                f"[全量回填] 批次 {batch_num}/{total_batches} 异常: {e}"
            )
            total_failed += len(batch)

        # 批次间短暂休息，避免打满远端
        if i + BATCH_SIZE < total:
            time.sleep(1)

    elapsed = round(time.time() - start_time, 1)
    logger.info(
        f"[全量回填] 完成: 总={total} 成功={total_success} "
        f"失败={total_failed} 跳过={total_skipped} 脏数据={total_dirty} "
        f"批次={batch_count} 耗时={elapsed}s"
    )

    return {
        "total": total,
        "success": total_success,
        "failed": total_failed,
        "skipped": total_skipped,
        "dirty": total_dirty,
        "batches": batch_count,
        "elapsed": elapsed,
    }


# ================================================================
# 后台线程
# ================================================================

_backfill_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _backfill_loop():
    """后台循环主逻辑。"""
    logger.info("[全量回填] 后台线程启动，等待 %ds...", STARTUP_DELAY)
    _stop_event.wait(timeout=STARTUP_DELAY)

    while not _stop_event.is_set():
        try:
            run_once()
        except Exception as e:
            logger.error(f"[全量回填] 主循环异常: {e}")

        # 根据盘中/盘后选择刷新间隔
        interval = INTERVAL_TRADING if _is_market_hours() else INTERVAL_CLOSED
        logger.info(f"[全量回填] 下一轮: {interval}s 后")
        _stop_event.wait(timeout=interval)

    logger.info("[全量回填] 后台线程已停止")


def start_backfill():
    """启动全市场 15m 后台回填线程（守护线程，进程退出自动终止）。"""
    global _backfill_thread
    if _backfill_thread and _backfill_thread.is_alive():
        logger.warning("[全量回填] 后台线程已在运行")
        return

    _stop_event.clear()
    _backfill_thread = threading.Thread(
        target=_backfill_loop,
        name="backfill-15m",
        daemon=True,
    )
    _backfill_thread.start()
    logger.info("[全量回填] 后台线程已启动")


def stop_backfill():
    """停止后台回填线程。"""
    _stop_event.set()
    if _backfill_thread and _backfill_thread.is_alive():
        _backfill_thread.join(timeout=10)
    logger.info("[全量回填] 已请求停止")
