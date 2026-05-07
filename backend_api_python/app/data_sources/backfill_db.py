"""
backfill_db.py — 全市场 15m/1D K 线惰性回填

═══════════════════════════════════════════════════════════════
  职责: 被调用时智能判断该更新什么，执行完即退出，不驻留线程
═══════════════════════════════════════════════════════════════

核心设计:
  1. 真实 bar 时间表对齐 — (9,45), (10,0), ..., (15,0) 共 16 根
     与 optimizer/check_continuity.py 一致，不是简单整除 900
  2. cn_last_update 表 — 记录每轮更新状态（进度/锁/失败列表）
     支持并发保护（乐观锁 + 超时抢占）和失败自动重试
  3. 只更新当天 — 盘中只拉当日 15m bar，数据量小
  4. 收盘后拉 1D — 17:00 后从远端拉取当日 1D（比 15m 聚合可靠）
  5. volume 校验 — 缺失/零 volume 标记统计（部分源确实没有）
  6. 用完即走 — 无常驻线程，调用方按需触发（cron / heartbeat / API）

数据流:
  远端数据源 → Coordinator → _backfill_batch/_update_daily_bars
    → _validate_bar（时间标准化 + OHLC 校验）
    → _safe_upsert（先删后写，保证唯一性）
    → cn_last_update（回写进度 + 失败列表）

并发模型:
  多个进程/线程可能同时调用 run_once()。
  cn_last_update.status='running' 作为乐观锁：
    - UPDATE ... RETURNING 原子操作，只有一个进程能拿到锁
    - 拿到锁的进程执行更新，完成后 _release_lock 回写状态
    - 超过 5 分钟的 running 视为死锁，可被抢占
    - 异常时 _release_lock 标记 failed，下次自动重试

对外接口:
    run_once()        — 主入口，惰性智能回填，执行完退出
    start_backfill()  — 兼容旧接口，等同 run_once()
    stop_backfill()   — 无操作（惰性模式无常驻线程）

内部也被 cn_stock.py 的 DBKlineBridge._backfill_db() 调用:
    _align_to_bar_schedule(ts) — 时间标准化函数

用法示例:
    # 作为 cron 任务（推荐）
    */15 9-15 * * 1-5  python -c "from app.data_sources.backfill_db import run_once; run_once()"

    # 作为 heartbeat 任务
    from app.data_sources.backfill_db import run_once
    result = run_once()
    # result = {"action": "15m", "15m": {...}, "1d": {...}, "elapsed": 3.2}

    # 兼容旧代码（cn_stock.py 的 backfill_all_market）
    from app.data_sources.backfill_db import start_backfill
    start_backfill()
"""

from __future__ import annotations

import bisect
import json
import time
from datetime import datetime, timedelta, timezone, time as dt_time, date as dt_date
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.coordinator import get_coordinator
from app.data_sources.normalizer import normalize_cn_code
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ================================================================
# 配置
# ================================================================

# 每批拉取的股票数量（Coordinator 批量接口上限）
BATCH_SIZE = 400

# 每只股票拉取的 15m bar 数量（16 根 ≈ 1 个交易日全量）
BARS_PER_STOCK = 16

# Coordinator 拉取超时（秒）
COORD_TIMEOUT = 25

# bar 完成后的更新延迟（秒）—— 给远端数据源留出写入时间
# 例: 10:00 的 bar，会在 10:00:30 之后才去拉取
_UPDATE_LAG_SECONDS = 30

# 15m 周期秒数（用于 delete_range 的 end_ts 偏移）
_15M_SEC = 900

# 北京时间时区
_TZ_CN = timezone(timedelta(hours=8))


# ================================================================
# A 股 15m bar 真实时间表
# ================================================================
#
# 上午 8 根: 9:45, 10:00, 10:15, 10:30, 10:45, 11:00, 11:15, 11:30
# 下午 8 根: 13:15, 13:30, 13:45, 14:00, 14:15, 14:30, 14:45, 15:00
#
# 注意:
#   - 9:30 的 bar 不存在（集合竞价，不计入）
#   - 11:30~13:15 是午休，没有 bar
#   - 与 optimizer/check_continuity.py 的 _ALL_BAR_TIMES 完全一致
#   - bar 的 time 字段 = 该 15 分钟区间的结束时间（收盘时间）

_MORNING_BARS = [
    (9, 45), (10, 0), (10, 15), (10, 30),
    (10, 45), (11, 0), (11, 15), (11, 30),
]
_AFTERNOON_BARS = [
    (13, 15), (13, 30), (13, 45), (14, 0),
    (14, 15), (14, 30), (14, 45), (15, 0),
]
_ALL_BAR_TIMES = _MORNING_BARS + _AFTERNOON_BARS  # 共 16 根

# 从午夜算起的分钟数，用于 _align_to_bar_schedule 的二分查找
_BAR_MINUTES = sorted(h * 60 + m for h, m in _ALL_BAR_TIMES)

# 上午最后一根 bar 的分钟数（11:30 = 690）
_MORNING_END_MIN = _MORNING_BARS[-1][0] * 60 + _MORNING_BARS[-1][1]

# 下午第一根 bar 的分钟数（13:15 = 795）
_AFTERNOON_START_MIN = _AFTERNOON_BARS[0][0] * 60 + _AFTERNOON_BARS[0][1]


# ================================================================
# cn_last_update 表 — 更新状态追踪
# ================================================================
#
# 表结构:
#   timeframe   — '15m' 或 '1D'
#   trade_date  — 交易日（DATE 类型）
#   bar_index   — 15m 专用：已更新到第几根 bar（0~15），-1 表示未开始
#   status      — 状态机：idle → running → ok / partial / failed
#   failed_codes — 失败的股票代码列表（JSONB 数组），下次 run_once 自动重试
#   updated_at  — 最后更新时间（用于超时检测）
#
# 状态流转:
#   首次:  无记录 → INSERT idle → UPDATE running → 执行 → ok/partial/failed
#   重入:  ok/partial/failed → UPDATE running → 执行 → ok/partial/failed
#   并发:  running（未超时）→ 拿锁失败，skip
#   超时:  running（>5min）→ 抢占 → 执行

_DDL_LAST_UPDATE = """
CREATE TABLE IF NOT EXISTS cn_last_update (
    id          SERIAL PRIMARY KEY,
    timeframe   VARCHAR(10)  NOT NULL,
    trade_date  DATE         NOT NULL,
    bar_index   SMALLINT     DEFAULT -1,
    status      VARCHAR(20)  DEFAULT 'ok',
    failed_codes JSONB,
    updated_at  TIMESTAMP    DEFAULT NOW(),
    UNIQUE (timeframe, trade_date)
)
"""


def _ensure_last_update_table(pool):
    """确保 cn_last_update 表存在（幂等，可并发调用）。"""
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(_DDL_LAST_UPDATE)
        conn.commit()


def _acquire_lock(pool, tf: str, trade_date: dt_date) -> bool:
    """尝试获取更新锁（乐观锁，非阻塞）。

    流程:
      1. INSERT ... ON CONFLICT DO NOTHING — 确保记录存在
      2. UPDATE ... SET status='running' WHERE status != 'running' RETURNING id
         - 拿到 id → 拿锁成功
         - 没拿到 → 检查是否超时
      3. 超时抢占: WHERE status='running' AND updated_at < NOW() - 5min

    Args:
        pool: 数据库连接池
        tf: 时间周期，'15m' 或 '1D'
        trade_date: 交易日

    Returns:
        True = 拿到锁，可以执行更新
        False = 别人正在跑，本次 skip
    """
    with pool.connection() as conn:
        cur = conn.cursor()

        # Step 1: 确保有记录（幂等）
        cur.execute("""
            INSERT INTO cn_last_update (timeframe, trade_date, bar_index, status)
            VALUES (%s, %s, -1, 'idle')
            ON CONFLICT (timeframe, trade_date) DO NOTHING
        """, (tf, trade_date))
        conn.commit()

        # Step 2: 尝试拿锁（idle / ok / failed / partial 都可以拿）
        cur.execute("""
            UPDATE cn_last_update
            SET status = 'running', updated_at = NOW()
            WHERE timeframe = %s AND trade_date = %s
              AND status != 'running'
            RETURNING id
        """, (tf, trade_date))
        if cur.fetchone():
            conn.commit()
            return True

        # Step 3: 超时抢占（running 超过 5 分钟视为死锁）
        cur.execute("""
            UPDATE cn_last_update
            SET status = 'running', updated_at = NOW()
            WHERE timeframe = %s AND trade_date = %s
              AND status = 'running'
              AND updated_at < NOW() - INTERVAL '5 minutes'
            RETURNING id
        """, (tf, trade_date))
        if cur.fetchone():
            conn.commit()
            logger.warning(f"[智能回填] {tf} 锁超时，已抢占")
            return True

        conn.commit()
        return False


def _release_lock(pool, tf: str, trade_date: dt_date,
                  status: str, bar_index: int = -1, failed_codes: list = None):
    """释放锁并回写更新状态。

    调用时机:
      - 正常完成: status='ok'（无失败）或 'partial'（有失败待重试）
      - 异常中断: status='failed'（保留上次的 failed_codes，下次重试）

    Args:
        pool: 数据库连接池
        tf: 时间周期
        trade_date: 交易日
        status: 'ok' / 'partial' / 'failed'
        bar_index: 15m 更新到的 bar 序号（1D 不关心，传 -1）
        failed_codes: 失败的股票代码列表（存入 JSONB，下次 run_once 自动重试）
    """
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE cn_last_update
            SET status = %s, bar_index = %s, failed_codes = %s, updated_at = NOW()
            WHERE timeframe = %s AND trade_date = %s
        """, (status, bar_index,
              json.dumps(failed_codes, ensure_ascii=False) if failed_codes else None,
              tf, trade_date))
        conn.commit()


def _read_state(pool, tf: str, trade_date: dt_date) -> Optional[Dict[str, Any]]:
    """读取 cn_last_update 的当前状态。

    Returns:
        None — 无记录（首次运行）
        {"bar_index": int, "status": str, "failed_codes": list}
    """
    with pool.cursor() as cur:
        cur.execute("""
            SELECT bar_index, status, failed_codes
            FROM cn_last_update
            WHERE timeframe = %s AND trade_date = %s
        """, (tf, trade_date))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "bar_index": row[0],
            "status": row[1],
            "failed_codes": json.loads(row[2]) if row[2] else [],
        }


# ================================================================
# 工具函数
# ================================================================

def _cn_now() -> datetime:
    """当前北京时间。"""
    return datetime.now(_TZ_CN)


def _is_trading_day() -> bool:
    """判断今天是否是 A 股交易日。

    优先使用 trading_calendar 模块（feather 文件 + akshare），
    模块不可用时保守返回 True（避免漏更新）。
    """
    try:
        from app.utils.trading_calendar import is_trading_day_today
        return is_trading_day_today()
    except Exception:
        return True


def _today_ts() -> int:
    """今天 00:00:00 的 Unix 时间戳（秒，北京时间）。"""
    dt = _cn_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


# ================================================================
# 时间标准化
# ================================================================

def _align_to_bar_schedule(ts: int) -> int:
    """将任意时间戳标准化到最近的 A 股 15m bar 收盘时间。

    这是写入 DB 的时间 key 的唯一来源。保证同一个 15 分钟窗口内
    的所有时间戳都映射到同一个 bar time，避免重复写入。

    映射规则:
      - 盘前 (< 9:45) → 9:45
      - 交易时段 → 最近的 bar 收盘时间（二分查找）
      - 午休 (11:30~13:15) → 11:30
      - 盘后 (≥ 15:00) → 15:00

    例:
      9:44:59 → 9:45    9:45:01 → 9:45
      10:07:30 → 10:00  10:08:00 → 10:15
      12:00:00 → 11:30  15:05:00 → 15:00

    Args:
        ts: Unix 时间戳（秒）

    Returns:
        标准化后的 Unix 时间戳（秒），对齐到 bar 收盘时间
    """
    dt = datetime.fromtimestamp(ts, tz=_TZ_CN)
    total_min = dt.hour * 60 + dt.minute

    # 盘前 → 当天第一根 bar
    if total_min < _BAR_MINUTES[0]:
        target = _BAR_MINUTES[0]
    # 午休区间 → 上午最后一根
    elif _MORNING_END_MIN <= total_min < _AFTERNOON_START_MIN:
        target = _MORNING_END_MIN
    # 盘后 → 下午最后一根
    elif total_min >= _BAR_MINUTES[-1]:
        target = _BAR_MINUTES[-1]
    else:
        # 正常交易时段，二分查找最近的 bar
        idx = bisect.bisect_right(_BAR_MINUTES, total_min)
        if idx == 0:
            target = _BAR_MINUTES[0]
        elif idx >= len(_BAR_MINUTES):
            target = _BAR_MINUTES[-1]
        else:
            # 取更接近的那个（相等时取前一个）
            prev_diff = total_min - _BAR_MINUTES[idx - 1]
            next_diff = _BAR_MINUTES[idx] - total_min
            target = _BAR_MINUTES[idx - 1] if prev_diff <= next_diff else _BAR_MINUTES[idx]

    target_h, target_m = divmod(target, 60)
    aligned = dt.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    return int(aligned.timestamp())


# ================================================================
# 数据清洗
# ================================================================

def _validate_bar(bar: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验并清洗单根 15m K 线。

    校验流程:
      1. time 字段必须是正数
      2. _align_to_bar_schedule 标准化到 bar 收盘时间
      3. OHLC 必须全部 > 0（缺失则丢弃整根 bar）
      4. high/low 自动修正（high < low 时交换）
      5. volume 缺失时填 0（部分数据源不提供 volume）

    Args:
        bar: 原始 bar，至少包含 time/open/high/low/close，volume 可选

    Returns:
        清洗后的 bar dict，校验失败返回 None
    """
    ts = bar.get("time", 0)
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None

    ts = _align_to_bar_schedule(int(ts))
    if ts <= 0:
        return None

    o = bar.get("open", 0)
    h = bar.get("high", 0)
    l = bar.get("low", 0)
    c = bar.get("close", 0)
    v = bar.get("volume", 0)

    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
        v = float(v) if v is not None and str(v).strip() not in ("", "-", "nan") else 0.0
    except (TypeError, ValueError):
        return None

    # OHLC 必须 > 0（真实股价不可能为零）
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return None

    # 合理性修正（不丢弃，自动修正逻辑矛盾）
    if h < l:
        h, l = l, h
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

    去重策略: 同一 timestamp 出现多次，保留最后一条（后到的覆盖先到的）。
    排序: 按 time 升序。

    Args:
        bars: 原始 bar 列表

    Returns:
        清洗后的 bar 列表（已排序、去重）
    """
    seen: Dict[int, Dict[str, Any]] = {}
    for bar in bars:
        cleaned = _validate_bar(bar)
        if cleaned is None:
            continue
        seen[cleaned["time"]] = cleaned
    return sorted(seen.values(), key=lambda b: b["time"])


# ================================================================
# DB 读写操作
# ================================================================

def _init_writer():
    """惰性初始化 db_market writer。

    首次调用时创建 MarketDBManager + MarketKlineWriter，
    确保 CNStock 库存在。失败返回 None。

    Returns:
        MarketKlineWriter 实例，或 None（初始化失败）
    """
    try:
        from app.utils.db_market import get_market_db_manager, get_market_kline_writer
        mgr = get_market_db_manager()
        mgr.ensure_market_db("CNStock")
        return get_market_kline_writer()
    except Exception as e:
        logger.error(f"[智能回填] DB 初始化失败: {e}")
        return None


def _get_pool(writer):
    """从 writer 获取 CNStock 连接池。"""
    return writer._mgr._get_pool("CNStock")


def _delete_range(writer, symbol: str, start_ts: int, end_ts: int):
    """删除指定 symbol 在 [start_ts, end_ts) 时间范围内的 15m 旧数据。

    写入前调用，确保不会有脏数据残留。可能跨年，按年分表删除。
    表不存在时静默忽略。
    """
    pool = _get_pool(writer)
    start_dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts)
    years = set(range(start_dt.year, end_dt.year + 1))

    with pool.connection() as conn:
        cur = conn.cursor()
        for year in years:
            table = f"kline_15m_{year}"
            try:
                cur.execute(f"""
                    DELETE FROM "{table}"
                    WHERE symbol = %s AND time >= %s AND time < %s
                """, (symbol, start_dt, end_dt))
            except Exception:
                pass  # 表不存在
        conn.commit()


def _delete_future_data(writer, symbol: str):
    """删除该 symbol 中 time > 当前时间的错误数据。

    远端数据偶尔会返回未来时间戳（时区错误等），必须清理。
    """
    pool = _get_pool(writer)
    now_dt = datetime.now()
    years = set(range(now_dt.year - 1, now_dt.year + 2))

    with pool.connection() as conn:
        cur = conn.cursor()
        for year in years:
            table = f"kline_15m_{year}"
            try:
                cur.execute(f"""
                    DELETE FROM "{table}"
                    WHERE symbol = %s AND time > %s
                """, (symbol, now_dt))
            except Exception:
                pass
        conn.commit()


def _safe_upsert(writer, symbol: str, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """安全写入 15m K 线: 先删后写，保证唯一性和准确性。

    流程:
      1. _clean_bars 清洗 + 校验 + 标准化时间
      2. _delete_range 删除该 symbol + 时间范围旧数据
      3. _delete_future_data 删除未来时间的错误数据
      4. writer.upsert 写入新数据（ON CONFLICT DO UPDATE）

    Args:
        writer: MarketKlineWriter 实例
        symbol: 纯 6 位股票代码（如 "600519"）
        bars: 原始 bar 列表

    Returns:
        {"inserted": int, "updated": int, "errors": int,
         "cleaned": int, "raw": int, "error": str（可选）}
    """
    cleaned = _clean_bars(bars)
    if not cleaned:
        return {"inserted": 0, "cleaned": 0, "raw": len(bars)}

    min_ts = cleaned[0]["time"]
    max_ts = cleaned[-1]["time"]

    records = [{
        "time": datetime.fromtimestamp(b["time"]),
        "open": b["open"], "high": b["high"],
        "low": b["low"], "close": b["close"],
        "volume": b["volume"],
    } for b in cleaned]

    _delete_range(writer, symbol, min_ts, max_ts + _15M_SEC)
    _delete_future_data(writer, symbol)

    try:
        result = writer.upsert("CNStock", symbol, "15m", records)
        return {
            "inserted": result.get("inserted", 0),
            "updated": result.get("updated", 0),
            "errors": result.get("errors", 0),
            "cleaned": len(cleaned), "raw": len(bars),
        }
    except Exception as e:
        logger.warning(f"[智能回填] upsert 失败 {symbol}: {e}")
        return {"inserted": 0, "cleaned": len(cleaned), "raw": len(bars), "error": str(e)}


# ================================================================
# 调度判断
# ================================================================

def _completed_bars_since(last_bar_idx: int, now_ts: int) -> List[Tuple[int, int]]:
    """返回已完成但尚未更新的 15m bar 列表。

    判断逻辑:
      - bar 已完成 = 当前时间 >= bar 收盘时间 + _UPDATE_LAG_SECONDS
      - 尚未更新 = bar 序号 > last_bar_idx

    Args:
        last_bar_idx: 上次更新到的 bar 序号（0~15），-1 表示未开始
        now_ts: 当前 Unix 时间戳（秒）

    Returns:
        [(bar_time_ts, bar_index), ...] 按时间升序
        例: [(1715040000, 2), (1715040900, 3)] 表示第 3、4 根 bar 需要更新
    """
    now_dt = datetime.fromtimestamp(now_ts, tz=_TZ_CN)
    base = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    result = []
    for i, (h, m) in enumerate(_ALL_BAR_TIMES):
        if i <= last_bar_idx:
            continue  # 已更新过，跳过
        bar_ts = int(base.replace(hour=h, minute=m, tzinfo=_TZ_CN).timestamp())
        effective_ts = bar_ts + _UPDATE_LAG_SECONDS
        if now_ts >= effective_ts:
            result.append((bar_ts, i))
    return result


# ================================================================
# 全市场股票代码获取
# ================================================================

def _get_all_codes() -> List[str]:
    """获取全市场 A 股纯 6 位数字代码列表。

    优先级:
      1. AStockDataSource（有 24h 缓存）
      2. 东财 push2 API（fallback）

    Returns:
        ["600000", "000001", ...] 纯数字代码列表
    """
    try:
        from app.data_sources.a_stock import AStockDataSource
        ds = AStockDataSource()
        raw_list = ds.get_all_stock_codes()
        codes = [str(item.get("stock_code", "")).strip() for item in raw_list
                 if len(str(item.get("stock_code", "")).strip()) == 6
                 and str(item.get("stock_code", "")).strip().isdigit()]
        if codes:
            logger.info(f"[智能回填] 获取股票列表: {len(codes)} 只")
            return codes
    except Exception as e:
        logger.warning(f"[智能回填] AStockDataSource 获取列表失败: {e}")

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
        codes = [str(item.get("f12", "")).strip() for item in items
                 if len(str(item.get("f12", "")).strip()) == 6
                 and str(item.get("f12", "")).strip().isdigit()]
        logger.info(f"[智能回填] 东财直接获取股票列表: {len(codes)} 只")
        return codes
    except Exception as e:
        logger.error(f"[智能回填] 东财获取列表失败: {e}")
        return []


# ================================================================
# 15m 批量拉取
# ================================================================

def _backfill_batch(
    codes: List[str], writer, cb, today_start_ts: int
) -> Tuple[int, int, int, int, int, List[str]]:
    """拉取一批股票的当日 15m 数据并写入 DB。

    流程:
      1. Coordinator.coordinate_kline 批量拉取 15m（一次 HTTP 覆盖多只）
      2. 过滤出 today_start_ts 之后的 bar（只保留当日）
      3. 统计 volume 缺失情况
      4. _safe_upsert 写入 DB
      5. 统计成功/失败/跳过

    Args:
        codes: 纯 6 位股票代码列表
        writer: MarketKlineWriter 实例
        cb: CircuitBreaker 实例（熔断器，远端不可用时快速失败）
        today_start_ts: 今天 00:00 的 Unix 时间戳

    Returns:
        (成功数, coordinator失败数, 跳过数, 脏数据数, vol缺失数, 失败代码列表)
    """
    coord = get_coordinator()
    normalized = [normalize_cn_code(c) for c in codes]

    coord_results, failed = coord.coordinate_kline(
        symbols=normalized, timeframe="15m", limit=BARS_PER_STOCK,
        cb=cb, market="CNStock", timeout=COORD_TIMEOUT, adj="qfq",
    )

    success = skip = dirty = vol_missing = 0
    failed_codes = list(failed) if failed else []

    for code in codes:
        bars = coord_results.get(normalize_cn_code(code), [])
        if not bars:
            skip += 1
            continue

        # 只取当日 bar
        bars = [b for b in bars if b.get("time", 0) >= today_start_ts]
        if not bars:
            skip += 1
            continue

        # 统计 volume 缺失（用于监控数据源质量）
        for b in bars:
            v = b.get("volume", 0)
            if v is None or str(v).strip() in ("", "-", "nan") or float(v or 0) <= 0:
                vol_missing += 1

        raw_count = len(bars)
        result = _safe_upsert(writer, code, bars)
        cleaned_count = result.get("cleaned", 0)
        dirty += raw_count - cleaned_count

        # 成功条件: 有清洗后的数据写入 且 无错误
        if cleaned_count > 0 and "error" not in result and result.get("errors", 0) == 0:
            success += 1
        else:
            skip += 1
            if "error" in result:
                failed_codes.append(code)

    return success, len(failed or []), skip, dirty, vol_missing, failed_codes


# ================================================================
# 1D 远端拉取
# ================================================================

def _upsert_daily(writer, symbol: str, bars: List[Dict[str, Any]]) -> bool:
    """将单只股票的当日 1D bar 写入 DB。

    流程:
      1. 从 bars 中过滤出当日的 bar
      2. 校验 OHLC > 0
      3. 事务内先 DELETE 当日旧数据，再 INSERT 新数据

    注意: DELETE + INSERT 在同一个 pool.connection() 事务内，
    中间崩了会回滚，不会丢数据。

    Args:
        writer: MarketKlineWriter 实例
        symbol: 纯 6 位股票代码
        bars: 远端返回的 1D bar 列表（可能包含多日，只取当日）

    Returns:
        True = 写入成功，False = 无有效数据或写入失败
    """
    today_start = _today_ts()
    today_dt = datetime.fromtimestamp(today_start, tz=_TZ_CN)
    year = today_dt.year
    today_midnight = today_dt.replace(tzinfo=None)

    # 过滤当日 bar + 校验 OHLC
    today_bars = []
    for b in bars:
        ts = b.get("time", 0)
        if isinstance(ts, datetime):
            ts = int(ts.timestamp())
        if ts >= today_start:
            try:
                o, h, l, c = float(b.get("open", 0)), float(b.get("high", 0)), float(b.get("low", 0)), float(b.get("close", 0))
                v = b.get("volume", 0)
                v = float(v) if v is not None and str(v).strip() not in ("", "-", "nan") else 0.0
            except (TypeError, ValueError):
                continue
            if o > 0 and h > 0 and l > 0 and c > 0:
                if h < l:
                    h, l = l, h
                today_bars.append({
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(l, 4), "close": round(c, 4),
                    "volume": round(max(v, 0), 2),
                })

    if not today_bars:
        return False

    # 远端 1D 通常只返回一根当日 bar
    bar = today_bars[-1]

    try:
        pool = _get_pool(writer)
        table = f"kline_1D_{year}"
        with pool.connection() as conn:
            cur = conn.cursor()
            # 先删当日旧数据
            cur.execute(f'DELETE FROM "{table}" WHERE symbol = %s AND time = %s',
                        (symbol, today_midnight))
            # 写入新数据
            cur.execute(f"""
                INSERT INTO "{table}" (symbol, time, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (symbol, today_midnight,
                  bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]))
        return True
    except Exception as e:
        logger.debug(f"[智能回填] 1D 写入失败 {symbol}: {e}")
        return False


def _update_daily_bars(writer, codes: List[str]) -> List[str]:
    """从远端批量拉取当日 1D 数据并写入 DB。

    使用 Coordinator.coordinate_kline(timeframe="1D") 批量拉取，
    比从 15m 聚合更可靠 — 数据源的官方日 OHLCV 不会有聚合误差。

    Args:
        writer: MarketKlineWriter 实例
        codes: 全市场股票代码列表

    Returns:
        失败的股票代码列表（用于写入 cn_last_update.failed_codes 重试）
    """
    today_dt = datetime.fromtimestamp(_today_ts(), tz=_TZ_CN)
    year = today_dt.year

    # 确保 1D 分区表存在
    try:
        from app.utils.db_market import get_market_db_manager
        get_market_db_manager().ensure_year_table("CNStock", "1D", year)
    except Exception as e:
        logger.warning(f"[智能回填] 确保 1D 表失败: {e}")
        return codes  # 全部视为失败

    coord = get_coordinator()
    cb = get_realtime_circuit_breaker()
    total = len(codes)
    success = skip = fail = 0
    all_failed_codes = []

    logger.info(f"[智能回填] 开始远端 1D 拉取: {total} 只")

    for i in range(0, total, BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        try:
            normalized = [normalize_cn_code(c) for c in batch]
            coord_results, failed = coord.coordinate_kline(
                symbols=normalized, timeframe="1D", limit=5,
                cb=cb, market="CNStock", timeout=COORD_TIMEOUT, adj="qfq",
            )

            if failed:
                all_failed_codes.extend(failed)

            for code in batch:
                bars = coord_results.get(normalize_cn_code(code), [])
                if not bars:
                    skip += 1
                elif _upsert_daily(writer, code, bars):
                    success += 1
                else:
                    skip += 1

            fail += len(failed)
            logger.info(f"[智能回填] 1D 批次 {batch_num}/{total_batches}: 成功={success} 失败={fail} 跳过={skip}")
        except Exception as e:
            logger.error(f"[智能回填] 1D 批次 {batch_num} 异常: {e}")
            fail += len(batch)
            all_failed_codes.extend(batch)

        if i + BATCH_SIZE < total:
            time.sleep(1)

    logger.info(f"[智能回填] 1D 远端拉取完成: 成功={success} 失败={fail} 跳过={skip}")
    return all_failed_codes


# ================================================================
# 对外接口
# ================================================================

def run_once() -> Dict[str, Any]:
    """惰性智能回填 — 主入口。自动判断该更新什么，执行完即退出。

    ═══════════════════════════════════════════════════════════
      这是唯一的对外接口（除了兼容旧接口的 start_backfill）。
      由 cron / heartbeat / API 调用，不需要关心内部细节。
    ═══════════════════════════════════════════════════════════

    执行逻辑:
      1. 非交易日 → 直接 skip
      2. 查 cn_last_update 获取上次进度（bar_index / failed_codes）
      3. 盘中 (9:44~15:00):
         - 计算哪些 bar 已完成但未更新（bar_index 之后的）
         - 先重试 failed_codes 中的失败股票
         - 再拉取新完成的 bar
         - 回写 cn_last_update（新的 bar_index + 失败列表）
      4. 收盘后 (15:00~17:00):
         - 拉最后一轮 15m（确保 15:00 的 bar 完整）
      5. 17:00 后:
         - 从远端拉取当日 1D 数据
         - 有失败记录则自动重试
      6. 释放锁，退出

    并发保护:
      - cn_last_update.status='running' 乐观锁
      - UPDATE ... RETURNING 原子操作，只有一个进程能拿到
      - 超过 5 分钟的 running 视为死锁，可被抢占

    Returns:
        {
            "action": "15m" | "1d" | "both" | "skip" | "locked",
            "15m": {                      # 15m 更新详情（如有）
                "success": int,           # 成功写入的股票数
                "failed": int,            # Coordinator 失败数
                "skipped": int,           # 跳过数（无数据/upsert 失败）
                "dirty": int,             # 清洗丢弃的 bar 数
                "vol_missing": int,       # volume 缺失的 bar 数
                "retried": int,           # 本次重试的失败股票数
                "pending_failures": int,  # 仍失败待下次重试的股票数
            },
            "1d": {                       # 1D 更新详情（如有）
                "done": True,
                "pending_failures": int,
            },
            "elapsed": float,             # 总耗时（秒）
        }
    """
    start_time = time.time()

    # ── 非交易日直接跳过 ──
    if not _is_trading_day():
        logger.info("[智能回填] 非交易日，跳过")
        return {"action": "skip", "elapsed": 0}

    # ── 初始化 DB ──
    writer = _init_writer()
    if not writer:
        logger.error("[智能回填] DB 不可用")
        return {"action": "skip", "elapsed": 0}

    pool = _get_pool(writer)
    _ensure_last_update_table(pool)

    # ── 获取全市场代码 ──
    codes = _get_all_codes()
    if not codes:
        logger.warning("[智能回填] 无股票代码")
        return {"action": "skip", "elapsed": 0}

    now_ts = int(time.time())
    now_dt = datetime.fromtimestamp(now_ts, tz=_TZ_CN)
    now_total_min = now_dt.hour * 60 + now_dt.minute
    today = now_dt.date()
    today_start = _today_ts()

    result_15m = None
    result_1d = None
    action = "skip"

    # ════════════════════════════════════════════════════════
    # 15m 更新（盘中 + 收盘后）
    # ════════════════════════════════════════════════════════
    if now_total_min >= _BAR_MINUTES[0] - 1:
        # 读取上次状态
        state_15m = _read_state(pool, "15m", today)
        last_bar_idx = state_15m["bar_index"] if state_15m else -1
        prev_failed = state_15m.get("failed_codes", []) if state_15m else []

        # 检查是否有新 bar 或待重试
        completed = _completed_bars_since(last_bar_idx, now_ts)
        has_new_bars = len(completed) > 0
        has_retries = len(prev_failed) > 0

        if has_new_bars or has_retries:
            # 尝试拿锁（拿不到说明别人在跑）
            if not _acquire_lock(pool, "15m", today):
                logger.info("[智能回填] 15m 已有进程在跑，跳过")
                return {"action": "locked", "elapsed": 0}

            try:
                cb = get_realtime_circuit_breaker()
                s_total = f_total = sk_total = d_total = vm_total = 0
                all_failed_codes = []

                # ── Step 1: 重试上次失败的股票 ──
                if has_retries:
                    logger.info(f"[智能回填] 重试 {len(prev_failed)} 只失败股票...")
                    retry_codes = [c for c in prev_failed if c in codes]
                    if retry_codes:
                        s, f, sk, d, vm, fc = _backfill_batch(retry_codes, writer, cb, today_start)
                        s_total += s; f_total += f; sk_total += sk
                        d_total += d; vm_total += vm
                        all_failed_codes.extend(fc)

                # ── Step 2: 拉取新完成的 bar ──
                if has_new_bars:
                    bar_names = [f"{_ALL_BAR_TIMES[i][0]}:{_ALL_BAR_TIMES[i][1]:02d}" for _, i in completed]
                    logger.info(f"[智能回填] {len(completed)} 根新 bar: {', '.join(bar_names)}")

                    # 排除已重试过的 codes，避免重复拉取
                    pull_codes = codes
                    if has_retries:
                        retry_set = set(prev_failed)
                        pull_codes = [c for c in codes if c not in retry_set]

                    for idx in range(0, len(pull_codes), BATCH_SIZE):
                        batch = pull_codes[idx:idx + BATCH_SIZE]
                        try:
                            s, f, sk, d, vm, fc = _backfill_batch(batch, writer, cb, today_start)
                            s_total += s; f_total += f; sk_total += sk
                            d_total += d; vm_total += vm
                            all_failed_codes.extend(fc)
                        except Exception as e:
                            logger.error(f"[智能回填] 批次异常: {e}")
                            f_total += len(batch)
                            all_failed_codes.extend(batch)
                        if idx + BATCH_SIZE < len(pull_codes):
                            time.sleep(0.5)

                # ── Step 3: 回写状态 ──
                new_bar_idx = max(last_bar_idx, max(i for _, i in completed)) if completed else last_bar_idx
                status = "ok" if not all_failed_codes else "partial"
                _release_lock(pool, "15m", today, status, new_bar_idx, all_failed_codes)

                result_15m = {
                    "success": s_total, "failed": f_total,
                    "skipped": sk_total, "dirty": d_total,
                    "vol_missing": vm_total, "retried": len(prev_failed),
                    "pending_failures": len(all_failed_codes),
                }
                action = "15m"
                logger.info(
                    f"[智能回填] 15m 完成: 成功={s_total} 失败={f_total} "
                    f"脏数据={d_total} bar_idx={new_bar_idx} "
                    f"待重试={len(all_failed_codes)}"
                )
            except Exception as e:
                # 异常时释放锁，保留上次的 failed_codes 供下次重试
                _release_lock(pool, "15m", today, "failed", last_bar_idx, prev_failed)
                logger.error(f"[智能回填] 15m 异常: {e}")
                raise
        else:
            logger.info("[智能回填] 15m 已是最新，无需更新")

    # ════════════════════════════════════════════════════════
    # 1D 更新（17:00 后）
    # ════════════════════════════════════════════════════════
    if now_total_min >= 17 * 60:
        state_1d = _read_state(pool, "1D", today)
        prev_failed_1d = state_1d.get("failed_codes", []) if state_1d else []
        need_update = state_1d is None or state_1d["status"] != "ok"

        if need_update:
            if not _acquire_lock(pool, "1D", today):
                logger.info("[智能回填] 1D 已有进程在跑，跳过")
                if action == "skip":
                    action = "locked"
            else:
                try:
                    # 合并重试列表和全量列表
                    retry_1d = [c for c in prev_failed_1d if c in codes]
                    pull_1d = codes
                    if retry_1d:
                        logger.info(f"[智能回填] 1D 重试 {len(retry_1d)} 只...")
                        pull_1d = list(set(codes) - set(retry_1d))
                        retry_result = _update_daily_bars(writer, retry_1d)
                        # 重试成功的从失败列表移除
                        retry_failed_set = set(retry_result)
                        prev_failed_1d = [c for c in prev_failed_1d if c in retry_failed_set]

                    failed_1d = _update_daily_bars(writer, pull_1d)
                    all_failed_1d = list(set(prev_failed_1d + failed_1d))

                    status = "ok" if not all_failed_1d else "partial"
                    _release_lock(pool, "1D", today, status, -1, all_failed_1d)

                    result_1d = {
                        "done": True,
                        "pending_failures": len(all_failed_1d),
                    }
                    action = "both" if action == "15m" else "1d"
                    logger.info(f"[智能回填] 1D 完成: 待重试={len(all_failed_1d)}")
                except Exception as e:
                    _release_lock(pool, "1D", today, "failed", -1, prev_failed_1d)
                    logger.error(f"[智能回填] 1D 异常: {e}")
                    raise
        else:
            logger.info("[智能回填] 1D 已是最新，无需更新")

    elapsed = round(time.time() - start_time, 1)
    logger.info(f"[智能回填] 完成: action={action} 耗时={elapsed}s")

    return {"action": action, "15m": result_15m, "1d": result_1d, "elapsed": elapsed}


def start_backfill():
    """兼容旧接口 — 等同 run_once()。

    原设计是启动后台常驻线程，现改为惰性一次性执行。
    cn_stock.py 的 DBKlineBridge.backfill_all_market() 调用此函数。
    """
    return run_once()


def stop_backfill():
    """无操作。

    惰性模式无常驻线程，无需 stop。
    保留此函数是为了向后兼容（如果有外部代码调用 stop_backfill）。
    """
    pass
