#!/usr/bin/env python3
"""
🔧 通达信修复模式 - A股数据断裂自动修复

与 check_continuity.py 联动，逐只股票修复已下载数据中的断裂和坏数据。
数据直接写入 db_market，不做 CSV 写操作。

流程: 检测断裂 → 逐只股票处理（停牌填充→akshare/通达信下载→验证→清理报表）

数据源策略:
  日线 gap 优先用 akshare 前复权数据（时间戳匹配更可靠），
  分钟线 gap 仍走通达信（akshare 不支持分钟线前复权）。
  加 --no-fallback 可强制全部走通达信。

用法:
  python tdx_repair.py -T 1D                        # 检测+修复日线断裂
  python tdx_repair.py -T 15m                       # 检测+修复15分钟线断裂
  python tdx_repair.py -T 1D --dry-run              # 仅检测，不下载不写库
  python tdx_repair.py -T 1D --symbol 600519        # 单只股票修复
  python tdx_repair.py -T 1D --workers 10           # 10进程并行修复
  python tdx_repair.py -T 1D --no-fallback          # 禁用akshare（仅用通达信）

依赖:
  - check_continuity.py (同目录)
  - db_market.py / db_multi.py（backend_api_python/app/utils/）
  - pytdx
  - akshare（可选，用于换源补数和停牌检测）
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pytdx.hq import TdxHq_API
import logging
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 公共常量（与 tdx_download.py 保持一致）
# ═══════════════════════════════════════════════════════

CONNECT_TIMEOUT = 5

SERVERS = [
    ('218.75.126.9', 7709),
    ('115.238.56.198', 7709),
    ('124.160.88.183', 7709),
    ('60.12.136.250', 7709),
    ('218.108.98.244', 7709),
    ('218.108.47.69', 7709),
    ('180.153.39.51', 7709),
]

_TZ_SH = timezone(timedelta(hours=8))

PERIOD_DIR = {
    '1D': 'daily', '1m': '1m', '5m': '5m',
    '15m': '15m', '30m': '30m', '60m': '1h',
}


# ═══════════════════════════════════════════════════════
# 15m 标准 bar 时间表 & 时间校验
# ═══════════════════════════════════════════════════════

_BAR_TIMES_15M = [
    (9, 30), (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45), (15, 0),
]


def validate_and_calibrate_time(dt, timeframe: str, tolerance_min: int = 1):
    """校验写入数据库的时间戳，对齐到标准 bar 时间"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_SH)

    if timeframe != "15m":
        return dt, "ok"

    total_min = dt.hour * 60 + dt.minute
    tolerance = tolerance_min

    if 570 <= total_min <= 690:
        best_diff = 999
        best_bar = None
        for h, m in _BAR_TIMES_15M:
            if h >= 12:
                continue
            bar_min = h * 60 + m
            diff = abs(total_min - bar_min)
            if diff < best_diff:
                best_diff = diff
                best_bar = (h, m)

        if best_diff <= tolerance:
            if best_diff == 0:
                return dt, "ok"
            calibrated = dt.replace(hour=best_bar[0], minute=best_bar[1], second=0, microsecond=0)
            return calibrated, "calibrated"
        else:
            logger.warning(
                f"15m 时间偏差过大（{best_diff}分钟 > 容差{tolerance}分钟），丢弃: "
                f"{dt.strftime('%Y-%m-%d %H:%M:%S')} 最近标准bar={best_bar[0]:02d}:{best_bar[1]:02d}"
            )
            return None, "discarded"

    elif 780 <= total_min <= 900:
        best_diff = 999
        best_bar = None
        for h, m in _BAR_TIMES_15M:
            if h < 12:
                continue
            bar_min = h * 60 + m
            diff = abs(total_min - bar_min)
            if diff < best_diff:
                best_diff = diff
                best_bar = (h, m)

        if best_diff <= tolerance:
            if best_diff == 0:
                return dt, "ok"
            calibrated = dt.replace(hour=best_bar[0], minute=best_bar[1], second=0, microsecond=0)
            return calibrated, "calibrated"
        else:
            logger.warning(
                f"15m 时间偏差过大（{best_diff}分钟 > 容差{tolerance}分钟），丢弃: "
                f"{dt.strftime('%Y-%m-%d %H:%M:%S')} 最近标准bar={best_bar[0]:02d}:{best_bar[1]:02d}"
            )
            return None, "discarded"

    else:
        logger.warning(
            f"15m 时间不在交易时段，丢弃: {dt.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(有效时段 09:30-11:30, 13:00-14:45)"
        )
        return None, "discarded"


# ═══════════════════════════════════════════════════════
# 连接通达信服务器
# ═══════════════════════════════════════════════════════

def _connect_api(worker_id):
    """连接通达信服务器，带超时，自动切换备用服务器"""
    for attempt in range(len(SERVERS)):
        idx = (worker_id + attempt) % len(SERVERS)
        srv = SERVERS[idx]
        try:
            api = TdxHq_API()
            api.connect(srv[0], srv[1], time_out=CONNECT_TIMEOUT)
            return api
        except Exception:
            continue
    raise ConnectionError(f"Worker-{worker_id}: 所有服务器连接失败")


# ═══════════════════════════════════════════════════════
# 修复所需的工具函数
# ═══════════════════════════════════════════════════════

def _truncate_ts_to_minute(ts) -> int:
    """时间戳截断到分钟（秒归零），用于 15m 比对"""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=_TZ_SH)
    elif isinstance(ts, str):
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                     '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(ts.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            dt = datetime.fromtimestamp(0, tz=_TZ_SH)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_SH)
    else:
        dt = datetime.fromtimestamp(ts, tz=_TZ_SH)
    dt = dt.replace(second=0, microsecond=0)
    return int(dt.timestamp())


def _ensure_datetime_repair(value):
    """将各种时间格式统一转为 datetime 对象（TIMESTAMP 列兼容）"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=_TZ_SH)
    if isinstance(value, str):
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                     '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_TZ_SH)
                return dt
            except ValueError:
                continue
    raise ValueError(f"无法解析时间值: {value!r}")


def _repair_ts_to_date(ts):
    """从 datetime 对象或整数时间戳提取日期字符串"""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=_TZ_SH)
        return dt.strftime("%Y-%m-%d")
    return datetime.fromtimestamp(ts, tz=_TZ_SH).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════
# 从 db 读取已有 gap 报告
# ═══════════════════════════════════════════════════════

def _repair_load_gaps_from_db(market, timeframe_filter=None, symbol_filter=None):
    """从 data_issue_report 表读取未修复的 gap 记录"""
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)

    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()

    if not mgr.market_db_exists(market):
        raise ConnectionError(
            f"无法连接 {market}_db，请检查 DATABASE_URL 或 backend_api_python/.env 配置"
        )

    pool = mgr._get_pool(market)

    sql = """
        SELECT symbol, timeframe, issue_type, start_date, end_date, expected_ts
        FROM data_issue_report
        WHERE issue_type IN ('middle', 'tail', 'intraday')
    """
    params = []
    conditions = []

    if timeframe_filter:
        conditions.append("timeframe = %s")
        params.append(timeframe_filter)
    if symbol_filter:
        conditions.append("symbol = %s")
        params.append(symbol_filter)

    if conditions:
        sql += " AND " + " AND ".join(conditions)

    sql += " ORDER BY jsonb_array_length(expected_ts) DESC"

    gaps = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                if isinstance(rec.get("expected_ts"), str):
                    rec["expected_ts"] = json.loads(rec["expected_ts"])
                rec["gap_type"] = rec.get("issue_type", rec.get("gap_type", ""))
                gaps.append(rec)

    mgr.close_all_pools()
    return gaps


# ═══════════════════════════════════════════════════════
# 从通达信拉取 K 线数据
# ═══════════════════════════════════════════════════════

def _repair_fetch_bars(api, market, code, freq, start_date, end_date):
    """从通达信拉取指定日期范围的 K 线数据"""
    all_bars = []
    max_offset = 800 * 10

    for offset in range(0, max_offset, 800):
        bars = api.get_security_bars(freq, market, code, offset, 800)
        if not bars:
            break
        all_bars = bars + all_bars
        if len(bars) < 800:
            break
        try:
            first_dt = bars[0]["datetime"][:10]
            if first_dt <= start_date:
                break
        except (KeyError, IndexError):
            pass

    is_daily = (freq == 9)
    filtered = []
    for b in all_bars:
        try:
            dt_str = b["datetime"][:10]
            if start_date <= dt_str <= end_date:
                dt = datetime.strptime(b["datetime"][:16], "%Y-%m-%d %H:%M")
                if is_daily:
                    dt = dt.replace(hour=0, minute=0, second=0)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_TZ_SH)
                filtered.append({
                    "time": dt,
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": int(b["vol"]),
                    "amount": b["amount"],
                })
        except (KeyError, ValueError):
            continue

    return filtered


# ═══════════════════════════════════════════════════════
# 修复工作进程
# ═══════════════════════════════════════════════════════

def _repair_worker(args):
    """修复工作进程 — 始终写入 db_market"""
    task_list, worker_id, freq = args

    api = _connect_api(worker_id)

    repaired = 0
    failed = 0
    total_bars = 0
    errors = []
    result_gaps_remaining = []

    import sys as _sys
    import os as _os
    _proj = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _be = _os.path.join(_proj, "backend_api_python")
    if _be not in _sys.path:
        _sys.path.insert(0, _be)
    from app.utils.db_market import get_market_kline_writer
    writer = get_market_kline_writer()

    for code, market_int, gaps in task_list:
        try:
            all_start = min(g["start_date"] for g in gaps)
            all_end = max(g["end_date"] for g in gaps)

            expected_ts_set = set()
            for g in gaps:
                for ts in g.get("expected_ts", []):
                    expected_ts_set.add(_truncate_ts_to_minute(ts))

            bars = _repair_fetch_bars(api, market_int, code, freq, all_start, all_end)

            if not bars:
                failed += 1
                errors.append(f"{code}: 数据源缺失 ({all_start}~{all_end})")
                for g in gaps:
                    result_gaps_remaining.append(g)
                continue

            if expected_ts_set:
                bars = [b for b in bars if _truncate_ts_to_minute(b["time"]) in expected_ts_set]

            if not bars:
                failed += 1
                errors.append(f"{code}: 数据源缺失（时间戳无匹配）")
                for g in gaps:
                    result_gaps_remaining.append(g)
                continue

            timeframe_map = {9: "1D", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
            tf = timeframe_map.get(freq, f"{freq}m")
            records = []
            discarded = 0
            for b in bars:
                dt_cal, action = validate_and_calibrate_time(b["time"], tf)
                if dt_cal is None:
                    discarded += 1
                    continue
                records.append({
                    "symbol": code,
                    "timeframe": tf,
                    "time": dt_cal,
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b["volume"],
                })
            if discarded > 0:
                logger.warning(f"{code}: 丢弃 {discarded} 条时间校验失败的 {tf} 记录")
            result = writer.bulk_write("CNStock", records, batch_size=5000)
            total_bars += result.get("inserted", 0)

            repaired += 1

        except (ConnectionError, OSError, TimeoutError):
            try:
                api = _connect_api(worker_id)
            except ConnectionError:
                pass
            failed += 1
            errors.append(f"{code}: 连接失败")
        except Exception as e:
            failed += 1
            errors.append(f"{code}: {type(e).__name__}: {e}")

    try:
        api.disconnect()
    except Exception:
        pass

    return (worker_id, repaired, failed, total_bars, errors, result_gaps_remaining)


# ═══════════════════════════════════════════════════════
# akshare 换源补数
# ═══════════════════════════════════════════════════════

def _repair_fetch_akshare(code, start_date, end_date, timeframe="1D"):
    """从 akshare 获取前复权 K 线数据"""
    try:
        import akshare as ak
    except ImportError:
        return []

    try:
        sym = code.strip()
        df = ak.stock_zh_a_hist(
            symbol=sym,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.iterrows():
            dt_str = str(row["日期"])[:10]
            dt = datetime.strptime(dt_str, "%Y-%m-%d")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_TZ_SH)
            records.append({
                "time": dt,
                "open": float(row["开盘"]),
                "high": float(row["最高"]),
                "low": float(row["最低"]),
                "close": float(row["收盘"]),
                "volume": int(row["成交量"]),
            })
        return records
    except Exception:
        return []


def _repair_fallback_akshare(remaining_gaps, data_type, writer, market="CNStock", today=None):
    """对通达信修不了的 gap，用 akshare 前复权数据补数，直接写入 db"""
    if today is None:
        today = datetime.now(_TZ_SH).strftime("%Y-%m-%d")

    gaps_to_fix = [g for g in remaining_gaps if g["timeframe"] == "1D"]
    other_gaps = [g for g in remaining_gaps if g["timeframe"] != "1D"]

    if not gaps_to_fix:
        return 0, len(remaining_gaps), [], remaining_gaps

    symbol_gaps = {}
    for g in gaps_to_fix:
        code = g["symbol"]
        if code not in symbol_gaps:
            symbol_gaps[code] = []
        symbol_gaps[code].append(g)

    fixed = []
    still_remaining = list(other_gaps)

    for code, gaps in symbol_gaps.items():
        all_start = min(g["start_date"] for g in gaps)
        all_end = max(g["end_date"] for g in gaps)

        expected_ts_set = set()
        for g in gaps:
            for ts in g.get("expected_ts", []):
                expected_ts_set.add(_truncate_ts_to_minute(ts))

        bars = _repair_fetch_akshare(code, all_start, all_end, "1D")
        if not bars:
            still_remaining.extend(gaps)
            continue

        if expected_ts_set:
            bars = [b for b in bars if _truncate_ts_to_minute(b["time"]) in expected_ts_set]

        if not bars:
            still_remaining.extend(gaps)
            continue

        records = []
        discarded = 0
        for b in bars:
            dt_cal, action = validate_and_calibrate_time(b["time"], "1D")
            if dt_cal is None:
                discarded += 1
                continue
            records.append({
                "symbol": code,
                "timeframe": "1D",
                "time": dt_cal,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            })
        if discarded > 0:
            logger.warning(f"{code}: 丢弃 {discarded} 条时间校验失败的 1D 记录")
        result = writer.bulk_write(market, records, batch_size=5000)
        if result.get("inserted", 0) > 0:
            fixed.extend(gaps)
        else:
            still_remaining.extend(gaps)

    return len(fixed), len(still_remaining), fixed, still_remaining


# ═══════════════════════════════════════════════════════
# 验证修复结果
# ═══════════════════════════════════════════════════════

def _repair_verify_fixed(writer, market, data_type, original_gaps, today=None):
    """修复后从 db 重新检测，与原始 gap 对比，返回确实被修复的 gap 列表"""
    if today is None:
        today = datetime.now(_TZ_SH).strftime("%Y-%m-%d")

    affected_symbols = set(g["symbol"] for g in original_gaps)

    _co_dir = os.path.dirname(os.path.abspath(__file__))
    if _co_dir not in sys.path:
        sys.path.insert(0, _co_dir)
    from check_continuity import check_1d_gaps, check_15m_gaps, _build_trading_day_cache
    _build_trading_day_cache(market)

    new_gap_keys = set()
    verified_symbols = set()

    for code in affected_symbols:
        try:
            if data_type == "1D":
                records = writer.query(market, code, "1D", limit=0)
                if records:
                    for ng in check_1d_gaps(code, records, today):
                        new_gap_keys.add((ng["symbol"], ng["timeframe"], ng["gap_type"],
                                          ng["start_date"], ng["end_date"]))
                    verified_symbols.add(code)
            elif data_type == "15m":
                records = writer.query(market, code, "15m", limit=0)
                if records:
                    for ng in check_15m_gaps(code, records, today):
                        new_gap_keys.add((ng["symbol"], ng["timeframe"], ng["gap_type"],
                                          ng["start_date"], ng["end_date"]))
                    verified_symbols.add(code)
        except Exception:
            pass

    fixed = []
    remaining = []
    for g in original_gaps:
        if g["symbol"] not in verified_symbols:
            remaining.append(g)
            continue
        key = (g["symbol"], g["timeframe"], g["gap_type"], g["start_date"], g["end_date"])
        if key in new_gap_keys:
            remaining.append(g)
        else:
            fixed.append(g)

    return fixed, remaining


# ═══════════════════════════════════════════════════════
# 清理已修复的报表记录
# ═══════════════════════════════════════════════════════

def _repair_cleanup_reports(market, fixed_gaps):
    """修复成功后，从报表中删除已确认修复的记录"""
    if not fixed_gaps:
        return 0

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)

    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool(market)

    deleted = 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            sql = """
                DELETE FROM data_issue_report
                WHERE symbol = %s AND timeframe = %s AND issue_type = %s
                  AND start_date = %s AND end_date = %s
            """
            for g in fixed_gaps:
                cur.execute(sql, (
                    g["symbol"], g["timeframe"], g.get("gap_type", g.get("issue_type", "")),
                    g["start_date"], g["end_date"],
                ))
                deleted += cur.rowcount

        conn.commit()

    mgr.close_all_pools()

    return deleted


# ═══════════════════════════════════════════════════════
# 停牌检测与填充
# ═══════════════════════════════════════════════════════

def _repair_check_suspend_baidu(code, gap_start, gap_end):
    """查询百度停牌数据，确认股票在 gap 期间是否停牌"""
    try:
        import akshare as ak

        gap_dt = datetime.strptime(gap_start, "%Y-%m-%d")
        search_dates = []
        for i in range(0, 31):
            d = gap_dt - timedelta(days=i)
            search_dates.append(d.strftime("%Y%m%d"))

        for date_str in search_dates:
            try:
                df = ak.news_trade_notify_suspend_baidu(date=date_str)
            except Exception:
                continue
            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                row_code = str(row.get("股票代码", "")).strip()
                if row_code != code:
                    continue
                suspend_start = str(row.get("停牌时间", ""))[:10]
                resume_date = str(row.get("复牌时间", ""))[:10]
                reason = str(row.get("停牌事项说明", ""))
                if not suspend_start:
                    continue
                if suspend_start <= gap_end:
                    if not resume_date or resume_date >= gap_start:
                        return {
                            "suspend_start": suspend_start,
                            "resume_date": resume_date or "",
                            "reason": reason,
                        }
        return None
    except Exception:
        return None


def _repair_fill_suspend(code, gaps, freq, writer, market="CNStock"):
    """用停牌前最后收盘价填充停牌期间的数据，直接写入数据库"""
    if not writer:
        return 0, 0

    timeframe_map = {9: "1D", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
    timeframe = timeframe_map.get(freq, f"{freq}m")

    existing = {}
    try:
        rows = writer.query(market, code, timeframe, limit=10)
        if rows:
            for r in rows:
                existing[r["time"]] = r.get("close", 0)
    except Exception:
        pass

    if not existing:
        return 0, 0

    _naive_fixed = {}
    for _k, _v in existing.items():
        if isinstance(_k, datetime) and _k.tzinfo is None:
            _k = _k.replace(tzinfo=_TZ_SH)
        _naive_fixed[_k] = _v
    existing = _naive_fixed

    sorted_ts = sorted(existing.keys())

    filled_count = 0
    total_bars = 0
    all_records = []

    for g in gaps:
        expected_ts_list = g.get("expected_ts", [])
        if not expected_ts_list:
            continue

        expected_ts_list = [_ensure_datetime_repair(t) for t in expected_ts_list]

        gap_first_ts = min(expected_ts_list)
        last_close = None
        for ts in reversed(sorted_ts):
            if ts < gap_first_ts:
                last_close = existing[ts]
                break
        if last_close is None:
            for ts in sorted_ts:
                if ts > max(expected_ts_list):
                    last_close = existing[ts]
                    break
        if last_close is None:
            continue

        for ts in expected_ts_list:
            all_records.append({
                "symbol": code,
                "timeframe": timeframe,
                "time": ts,
                "open": last_close,
                "high": last_close,
                "low": last_close,
                "close": last_close,
                "volume": 0,
            })
            total_bars += 1
        filled_count += 1

    if all_records:
        result = writer.bulk_write(market, all_records, batch_size=5000)
        inserted = result.get("inserted", 0)
        if inserted == 0:
            return 0, 0

    return filled_count, total_bars


# ═══════════════════════════════════════════════════════
# 单只股票修复主流程
# ═══════════════════════════════════════════════════════

def _repair_single_stock(code, gaps, freq, out_dir, market, no_fallback, dry_run, today):
    """逐只股票修复：先查停牌 → 停牌补数据 → 非停牌走 akshare/tdx → 验证 → 清理"""
    result = {
        "code": code,
        "repaired": 0,
        "failed": 0,
        "bars": 0,
        "fixed_gaps": [],
        "remaining_gaps": [],
        "errors": [],
        "suspended": False,
    }

    if dry_run:
        result["remaining_gaps"] = gaps
        return result

    freq_to_dir = {9: "daily", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
    subdir = freq_to_dir.get(freq, f"{freq}m")
    data_type = subdir

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)
    from app.utils.db_market import get_market_kline_writer
    writer = get_market_kline_writer()

    # 第一步: 逐个 gap 查停牌
    suspend_gaps = []
    normal_gaps = []

    for g in gaps:
        suspend_info = _repair_check_suspend_baidu(code, g["start_date"], g["end_date"])
        if suspend_info:
            suspend_gaps.append(g)
            result["suspended"] = True
        else:
            normal_gaps.append(g)

    # 第二步: 停牌 gap → 用最后收盘价填充
    if suspend_gaps:
        filled, bars = _repair_fill_suspend(code, suspend_gaps, freq,
                                             writer=writer, market=market)
        if filled > 0:
            result["repaired"] += filled
            result["bars"] += bars
            result["fixed_gaps"].extend(suspend_gaps)
        else:
            normal_gaps.extend(suspend_gaps)

    # 第三步: 非停牌 gap → akshare / 通达信修复
    gaps_1d = [g for g in normal_gaps if g["timeframe"] == "1D"]
    gaps_min = [g for g in normal_gaps if g["timeframe"] != "1D"]
    remaining_after_repair = []

    # 日线 → akshare 优先
    if gaps_1d and not no_fallback:
        ak_fixed_n, ak_remaining_n, ak_fixed_g, ak_remaining_g = \
            _repair_fallback_akshare(gaps_1d, "1D", writer, market, today)
        if ak_fixed_n > 0:
            result["repaired"] += ak_fixed_n
            result["fixed_gaps"].extend(ak_fixed_g)
            result["bars"] += sum(len(g.get("expected_ts", [])) for g in ak_fixed_g)
        if ak_remaining_g:
            remaining_after_repair.extend(ak_remaining_g)
    elif gaps_1d and no_fallback:
        remaining_after_repair.extend(gaps_1d)

    # 分钟线 + akshare 没修好的日线 → 通达信
    tdx_gaps = gaps_min + remaining_after_repair
    remaining_after_repair = []

    if tdx_gaps:
        market_int = 1 if code.startswith(("60", "68")) else 0
        task_list = [(code, market_int, tdx_gaps)]
        worker_args = (task_list, 0, freq)
        _, repaired, failed, total_bars, errors, tdx_remaining = _repair_worker(worker_args)

        result["repaired"] += repaired
        result["failed"] += failed
        result["bars"] += total_bars
        result["errors"].extend(errors)

        attempted_gaps = [g for g in tdx_gaps if g not in tdx_remaining]
        if attempted_gaps:
            fixed_gaps, still_broken = _repair_verify_fixed(writer, market, data_type, attempted_gaps, today)
            result["fixed_gaps"].extend(fixed_gaps)
            remaining_after_repair = tdx_remaining + still_broken
        else:
            remaining_after_repair = tdx_remaining

    result["remaining_gaps"] = remaining_after_repair

    # 第四步: 清理已修复的报表记录
    if result["fixed_gaps"]:
        try:
            gap_del = _repair_cleanup_reports(market, result["fixed_gaps"])
            result["gap_deleted"] = gap_del
        except Exception as e:
            result["errors"].append(f"清理报表失败: {e}")

    return result


# ═══════════════════════════════════════════════════════
# 主修复入口
# ═══════════════════════════════════════════════════════

def run_repair(data_type, out_dir, workers=5, dry_run=False, symbol=None,
               market="CNStock", db_url=None, no_fallback=False):
    """主修复入口 — 逐只股票模式"""
    if db_url:
        os.environ["DATABASE_URL"] = db_url
    else:
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _backend_root = os.path.join(_project_root, "backend_api_python")
        try:
            from dotenv import load_dotenv
            for env_path in [
                os.path.join(_backend_root, '.env'),
                os.path.join(_project_root, '.env'),
            ]:
                if os.path.isfile(env_path):
                    load_dotenv(env_path, override=False)
                    break
        except Exception:
            pass

    freq_map = {"1D": 9, "15m": 1, "1m": 4, "5m": 0, "30m": 5, "60m": 6}
    freq = freq_map.get(data_type)
    if freq is None:
        print(f"❌ 不支持的时间框架: {data_type}")
        return

    today = datetime.now(_TZ_SH).strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  🔧 修复模式（逐只股票） | {data_type} | {market}")
    print(f"  dry-run={dry_run}  symbol={symbol or '全部'}  换源={'关' if no_fallback else '开'}")
    print(f"{'='*55}")

    # 阶段 1: 从 db 读取 gap 列表
    gaps = []

    print("\n[1/3] 从 data_issue_report 读取 gap...")
    try:
        gaps = _repair_load_gaps_from_db(market, timeframe_filter=data_type, symbol_filter=symbol)
        print(f"  读取到 {len(gaps)} 条未修复的 gap")
    except Exception as e:
        print(f"  ❌ db 读取失败: {e}")
        print(f"  💡 请先运行 check_continuity.py 写入 gap 报告")
        return

    if not gaps:
        print("\n  ✅ 无断裂数据，无需修复")
        return

    # 统计
    by_type = {}
    total_missing = 0
    for g in gaps:
        by_type[g["gap_type"]] = by_type.get(g["gap_type"], 0) + 1
        total_missing += len(g.get("expected_ts", []))

    print(f"\n  📊 断裂统计:")
    print(f"     总 gap: {len(gaps)} 条 | 缺失 bar: {total_missing}")
    for gt, cnt in sorted(by_type.items()):
        print(f"     {gt}: {cnt} 条")

    gaps_sorted = sorted(gaps, key=lambda g: len(g.get("expected_ts", [])), reverse=True)
    print(f"\n  最严重的 10 条:")
    for g in gaps_sorted[:10]:
        print(f"    {g['symbol']:>8} | {g['timeframe']:>3} | {g['gap_type']:>8} | "
              f"{g['start_date']}~{g['end_date']} | 缺 {len(g.get('expected_ts', []))} 根")

    if dry_run:
        print(f"\n  🔍 dry-run 模式，不执行下载")
        return

    # 阶段 2: 按股票分组
    print(f"\n[2/3] 按股票分组...")
    symbol_gaps = {}
    for g in gaps:
        code = g["symbol"]
        if code not in symbol_gaps:
            symbol_gaps[code] = []
        symbol_gaps[code].append(g)

    symbols = sorted(symbol_gaps.keys())
    total_symbols = len(symbols)
    print(f"  需修复 {total_symbols} 只股票")

    # 阶段 3: 逐只股票修复
    print(f"\n[3/3] 开始逐只修复...\n")

    t0 = time.time()

    all_fixed_gaps = []
    all_remaining_gaps = []
    total_repaired = 0
    total_failed = 0
    total_bars = 0
    all_errors = []

    for idx, code in enumerate(symbols, 1):
        stock_gaps = symbol_gaps[code]
        gap_count = len(stock_gaps)
        missing_bars = sum(len(g.get("expected_ts", [])) for g in stock_gaps)

        print(f"  [{idx}/{total_symbols}] {code}  ({gap_count} gap, 缺 {missing_bars} 根)", end="", flush=True)

        result = _repair_single_stock(
            code=code,
            gaps=stock_gaps,
            freq=freq,
            out_dir=out_dir,
            market=market,
            no_fallback=no_fallback,
            dry_run=dry_run,
            today=today,
        )

        total_repaired += result["repaired"]
        total_failed += result["failed"]
        total_bars += result["bars"]
        all_errors.extend(result["errors"])
        all_fixed_gaps.extend(result["fixed_gaps"])
        all_remaining_gaps.extend(result["remaining_gaps"])

        fixed_n = len(result["fixed_gaps"])
        remain_n = len(result["remaining_gaps"])
        if result.get("suspended"):
            status = f"  🔄 停牌填充 {fixed_n} gap (停牌期间价不变量为0)"
        elif fixed_n > 0 and remain_n == 0:
            status = f"  ✅ 已修复 {fixed_n} gap"
        elif fixed_n > 0 and remain_n > 0:
            status = f"  ⚠️  修复 {fixed_n}, 剩余 {remain_n} (数据源缺失)"
        elif fixed_n == 0 and result["repaired"] == 0:
            status = f"  ❌ 下载失败"
        else:
            status = f"  ⚠️  未修复 (数据源缺失)"

        if result.get("gap_deleted"):
            status += f"  [报表清理: {result['gap_deleted']}条]"
        if result["errors"]:
            status += f"  [错误: {result['errors'][0]}]"

        print(status)

    elapsed = time.time() - t0

    # 最终汇总
    print(f"\n{'='*55}")
    print(f"  🔧 逐只修复完成!")
    print(f"     股票: {total_symbols} 只")
    print(f"     成功: {total_repaired}  失败: {total_failed}")
    print(f"     补充 bar: {total_bars}")
    print(f"     已修复 gap: {len(all_fixed_gaps)} 条")
    print(f"     未修复 gap: {len(all_remaining_gaps)} 条 (数据源缺失)")
    print(f"     耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")

    if all_remaining_gaps:
        print(f"\n  ⚠️  最终未修复的 gap（数据源缺失）:")
        for g in all_remaining_gaps[:10]:
            print(f"    {g['symbol']:>8} | {g['timeframe']:>3} | {g['gap_type']:>8} | "
                  f"{g['start_date']}~{g['end_date']}")
        if len(all_remaining_gaps) > 10:
            print(f"    ... 还有 {len(all_remaining_gaps) - 10} 条")

    if all_errors:
        print(f"\n  ⚠️  错误 (前 10 条):")
        for msg in all_errors[:10]:
            print(f"    {msg}")

    print(f"\n  💡 建议运行验证: python check_continuity.py --symbol {symbol or '全市场'}")
    print(f"{'='*55}")


# ═══════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='🔧 通达信修复模式 - A股数据断裂自动修复',
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument('--type', '-T',
        choices=['1D', '1m', '5m', '15m', '30m', '60m'],
        default='1D',
        help='''数据类型 (默认1D):
  1D      - 日线
  1m      - 1分钟线
  5m      - 5分钟线
  15m     - 15分钟线
  30m     - 30分钟线
  60m     - 60分钟线''')
    ap.add_argument('--workers', '-w', type=int, default=5, help='并行进程数 (默认5)')
    ap.add_argument('--output', '-o', default='optimizer_output/CNStock', help='数据目录 (默认 optimizer_output/CNStock)')
    ap.add_argument('--db-url', type=str, default=None,
        help='数据库连接 URL (默认从 DATABASE_URL 环境变量读取)')
    ap.add_argument('--dry-run', action='store_true',
        help='仅检测断裂，不执行下载和写库。')
    ap.add_argument('--no-fallback', action='store_true',
        help='禁用 akshare 换源补数，仅用通达信修复。')
    ap.add_argument('--symbol', type=str, default=None,
        help='只修复指定股票代码，如 600519。')
    ap.add_argument('--market', type=str, default='CNStock',
        help='市场名 (默认 CNStock)')

    args = ap.parse_args()

    print(f"""
╔═══════════════════════════════════════════════════╗
║  🔧 修复模式 - 通达信补数                          ║
║  与 check_continuity.py 联动，自动修复数据断裂      ║
╠═══════════════════════════════════════════════════╣
║  类型: {args.type:<10}  进程: {args.workers:<6}               ║
║  输出: {args.output:<38}║
╚═══════════════════════════════════════════════════╝
""")

    run_repair(
        data_type=args.type,
        out_dir=args.output,
        workers=args.workers,
        dry_run=args.dry_run,
        symbol=args.symbol,
        market=args.market,
        db_url=args.db_url,
        no_fallback=args.no_fallback,
    )


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出。")
        sys.exit(1)
