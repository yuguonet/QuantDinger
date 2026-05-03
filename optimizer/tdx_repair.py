#!/usr/bin/env python3
"""
🔧 数据断裂自动修复

与 check_continuity.py 联动，逐只股票修复已下载数据中的断裂和坏数据。
数据直接写入 db_market，不做 CSV 写操作。

流程: 检测断裂 → 逐只股票处理（DataSourceFactory 获取前复权数据→写入→验证→清理报表）

数据源策略:
  统一通过 DataSourceFactory 获取数据（自动 provider fallback + 熔断），
  再由 adjust_kline 计算前复权，写入 db_market。

用法:
  python tdx_repair.py -T 1D                        # 检测+修复日线断裂
  python tdx_repair.py -T 15m                       # 检测+修复15分钟线断裂
  python tdx_repair.py -T 1D --dry-run              # 仅检测，不下载不写库
  python tdx_repair.py -T 1D --symbol 600519        # 单只股票修复
  python tdx_repair.py -T 1D --workers 10           # 10进程并行修复

依赖:
  - check_continuity.py (同目录)
  - db_market.py / db_multi.py（backend_api_python/app/utils/）
  - DataSourceFactory（backend_api_python/app/data_sources/）
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import logging
logger = logging.getLogger(__name__)

# 延迟导入 DataSourceFactory（避免循环依赖）
_factory = None

def _get_factory():
    """获取全局 DataSourceFactory 单例（延迟导入）"""
    global _factory
    if _factory is None:
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _backend_root = os.path.join(_project_root, "backend_api_python")
        if _backend_root not in sys.path:
            sys.path.insert(0, _backend_root)
        from app.data_sources.factory import get_factory
        _factory = get_factory()
    return _factory


# ═══════════════════════════════════════════════════════
# 公共常量（与 tdx_download.py 保持一致）
# ═══════════════════════════════════════════════════════

_TZ_SH = timezone(timedelta(hours=8))


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
        # 15:00~15:59 的数据视为收盘 bar，校准到 15:00
        if total_min >= 900 and total_min < 960:
            calibrated = dt.replace(hour=15, minute=0, second=0, microsecond=0)
            return calibrated, "calibrated"

        logger.warning(
            f"15m 时间不在交易时段，丢弃: {dt.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(有效时段 09:30-11:30, 13:00-15:00)"
        )
        return None, "discarded"


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
# 通过 DataSourceFactory 获取前复权数据
# ═══════════════════════════════════════════════════════

def _calc_fetch_count(start_date: str, end_date: str, timeframe: str) -> int:
    """根据日期范围和周期估算需要拉取的 bar 数量（留 2 倍余量）"""
    try:
        d0 = datetime.strptime(start_date, "%Y-%m-%d")
        d1 = datetime.strptime(end_date, "%Y-%m-%d")
        days = max((d1 - d0).days, 1)
    except ValueError:
        days = 30

    bars_per_day = {
        "1D": 1, "15m": 16, "1m": 240, "5m": 48, "30m": 8, "60m": 4,
    }
    per_day = bars_per_day.get(timeframe, 16)
    return max(days * per_day * 2, 100)


def _repair_fetch_from_factory(code, gaps, timeframe, market="CNStock"):
    """
    通过 DataSourceFactory 获取前复权 K 线数据，按 expected_ts 过滤。

    流程:
      1. 估算 fetch count，调用 SourceAdapter.get_kline(adj="qfq")
      2. 按 gap 的 expected_ts 时间戳集合过滤
      3. 返回匹配的 bars 列表
    """
    all_start = min(g["start_date"] for g in gaps)
    all_end = max(g["end_date"] for g in gaps)

    # 收集所有需要的时间戳（截断到分钟用于比对）
    expected_ts_set = set()
    for g in gaps:
        for ts in g.get("expected_ts", []):
            expected_ts_set.add(_truncate_ts_to_minute(ts))

    count = _calc_fetch_count(all_start, all_end, timeframe)

    try:
        factory = _get_factory()
        source = factory.get_source(market)
        # SourceAdapter.get_kline 内部: fetch_kline_raw → adjust_kline(qfq)
        # 返回前复权后的 bars，time 为 unix timestamp (int)
        bars = source.get_kline(code, timeframe, count)
    except Exception as e:
        logger.warning(f"[Factory] 获取 K 线失败 {code} tf={timeframe}: {e}")
        return []

    if not bars:
        return []

    # 按日期范围粗筛
    start_dt = datetime.strptime(all_start, "%Y-%m-%d").replace(tzinfo=_TZ_SH)
    end_dt = datetime.strptime(all_end, "%Y-%m-%d").replace(hour=23, minute=59, tzinfo=_TZ_SH)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    bars = [b for b in bars if start_ts <= b.get("time", 0) <= end_ts]

    if not bars:
        return []

    # 按 expected_ts 精确匹配（如果有）
    if expected_ts_set:
        bars = [b for b in bars if _truncate_ts_to_minute(b["time"]) in expected_ts_set]

    return bars


def _repair_aggregate_15m_to_1d(code, gaps, market="CNStock"):
    """
    当 DataSourceFactory 取 1D 数据失败时，从 15m 数据聚合补数。

    流程:
      1. 从 DataSourceFactory 获取 15m 前复权数据
      2. 按日期分组，每天 ≥8 根 bar 才算有效
      3. 聚合: open=第一根, high=MAX, low=MIN, close=最后一根, volume=SUM
      4. 返回 1D bars（时间戳为当天午夜）
    """
    missing_dates = set()
    for g in gaps:
        for ts in g.get("expected_ts", []):
            d = _repair_ts_to_date(ts)
            missing_dates.add(d)

    if not missing_dates:
        return []

    # 计算需要多少 15m bar（每天 16 根，留 2 倍余量）
    count = max(len(missing_dates) * 16 * 2, 200)

    try:
        factory = _get_factory()
        source = factory.get_source(market)
        bars_15m = source.get_kline(code, "15m", count)
    except Exception as e:
        logger.warning(f"[Factory] 15m 聚合获取失败 {code}: {e}")
        return []

    if not bars_15m:
        return []

    # 按日期分组
    date_to_bars = defaultdict(list)
    for b in bars_15m:
        ts = b.get("time", 0)
        if isinstance(ts, (int, float)):
            d = datetime.fromtimestamp(ts, tz=_TZ_SH).strftime("%Y-%m-%d")
        elif isinstance(ts, datetime):
            d = (ts if ts.tzinfo else ts.replace(tzinfo=_TZ_SH)).strftime("%Y-%m-%d")
        else:
            continue
        if d in missing_dates:
            date_to_bars[d].append(b)

    # 逐日聚合
    result = []
    for d, day_bars in sorted(date_to_bars.items()):
        if len(day_bars) < 8:
            continue

        day_bars.sort(key=lambda x: x.get("time", 0))
        o = day_bars[0]["open"]
        h = max(b["high"] for b in day_bars)
        lo = min(b["low"] for b in day_bars)
        c = day_bars[-1]["close"]
        vol = sum(b.get("volume", 0) for b in day_bars)

        if o == 0 and h == 0 and lo == 0 and c == 0:
            continue

        # 1D bar 时间戳统一用当天午夜（与 expected_ts / 数据库格式一致）
        dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=_TZ_SH)

        result.append({
            "time": dt,
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": vol,
        })

    return result


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
# 单只股票修复主流程
# ═══════════════════════════════════════════════════════

def _repair_single_stock(code, gaps, freq, out_dir, market, dry_run, today):
    """逐只股票修复：DataSourceFactory 获取前复权数据 → 写入 → 验证 → 清理"""
    result = {
        "code": code,
        "repaired": 0,
        "failed": 0,
        "bars": 0,
        "fixed_gaps": [],
        "remaining_gaps": [],
        "errors": [],
    }

    if dry_run:
        result["remaining_gaps"] = gaps
        return result

    freq_to_dir = {9: "daily", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
    subdir = freq_to_dir.get(freq, f"{freq}m")
    data_type = subdir

    timeframe_map = {9: "1D", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
    timeframe = timeframe_map.get(freq, f"{freq}m")

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)
    from app.utils.db_market import get_market_kline_writer
    writer = get_market_kline_writer()

    # 第一步: 通过 DataSourceFactory 获取前复权数据
    bars = _repair_fetch_from_factory(code, gaps, timeframe, market)

    # 1D 失败 → 尝试从 15m 聚合
    if not bars and timeframe == "1D":
        bars = _repair_aggregate_15m_to_1d(code, gaps, market)
        if bars:
            logger.info(f"{code}: DataSourceFactory 1D 失败，从 15m 聚合 {len(bars)} 根")

    if not bars:
        result["failed"] = 1
        result["errors"].append(f"{code}: DataSourceFactory 数据缺失")
        result["remaining_gaps"] = gaps
        return result

    # 第二步: 校验时间戳 + 构造写入记录
    records = []
    discarded = 0
    for b in bars:
        # DataSourceFactory 返回 time 为 unix timestamp (int)，转为 datetime
        ts = b["time"]
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=_TZ_SH)
        elif isinstance(ts, datetime):
            dt = ts if ts.tzinfo else ts.replace(tzinfo=_TZ_SH)
        else:
            discarded += 1
            continue

        dt_cal, action = validate_and_calibrate_time(dt, timeframe)
        if dt_cal is None:
            discarded += 1
            continue
        records.append({
            "symbol": code,
            "timeframe": timeframe,
            "time": dt_cal,
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b.get("volume", 0),
        })
    if discarded > 0:
        logger.warning(f"{code}: 丢弃 {discarded} 条时间校验失败的 {timeframe} 记录")

    if not records:
        result["failed"] = 1
        result["errors"].append(f"{code}: 所有记录时间校验失败")
        result["remaining_gaps"] = gaps
        return result

    # 第三步: 写入 db
    try:
        write_result = writer.bulk_write(market, records, batch_size=5000)
        result["bars"] = write_result.get("inserted", 0)
        result["repaired"] = 1
    except Exception as e:
        result["failed"] = 1
        result["errors"].append(f"{code}: 写入失败: {e}")
        result["remaining_gaps"] = gaps
        return result

    # 第四步: 验证修复结果
    try:
        fixed_gaps, still_broken = _repair_verify_fixed(writer, market, data_type, gaps, today)
        result["fixed_gaps"] = fixed_gaps
        result["remaining_gaps"] = still_broken
    except Exception as e:
        result["errors"].append(f"{code}: 验证失败: {e}")
        result["remaining_gaps"] = gaps

    # 第五步: 清理已修复的报表记录
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
               market="CNStock", db_url=None):
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
    print(f"  dry-run={dry_run}  symbol={symbol or '全部'}  数据源=DataSourceFactory")
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
        if fixed_n > 0 and remain_n == 0:
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
        description='🔧 数据断裂自动修复 - 通过 DataSourceFactory 获取前复权数据',
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
    ap.add_argument('--symbol', type=str, default=None,
        help='只修复指定股票代码，如 600519。')
    ap.add_argument('--market', type=str, default='CNStock',
        help='市场名 (默认 CNStock)')

    args = ap.parse_args()

    print(f"""
╔═══════════════════════════════════════════════════╗
║  🔧 修复模式 - DataSourceFactory 前复权补数        ║
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
    )


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出。")
        sys.exit(1)
