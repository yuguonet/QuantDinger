#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# backfill_1m_mootdx.py — mootdx 直连下载 1 分钟 K 线 → kline_1m_YYYY 表
# ============================================================================
#
# 与 source_sync.py 的区别:
#   - source_sync: 走 Coordinator，数据源只保留 ~7 天 1m 数据
#   - 本脚本:     mootdx 直连通达信，通过递增 offset 翻页拉取更长历史
#
# mootdx bars() 机制:
#   - 每次返回最近 N 条 bar（offset=N，上限 800/次）
#   - offset=800 → 最近 ~3 天 1m 数据
#   - offset=800*N → 最近 ~3N 天（需要多次调用，逐次增大 offset）
#   - 没有 start_date/end_date 参数，只能靠 start 翻页
#
# 通达信服务器限制:
#   - 1m 数据仅保留约 4~5 个月（实测最早可到 ~4 个月前）
#   - 超出范围的数据服务器不返回，脚本会自动停止
#
# 用法:
# python fix/backfill_1m_mootdx.py                          # 默认全量（自动从服务器最早可用日期开始）
# python fix/backfill_1m_mootdx.py --start-date 2025-06-01  # 指定起始日
# python fix/backfill_1m_mootdx.py --end-date 2025-07-01    # 指定截止日
# python fix/backfill_1m_mootdx.py --batch-size 50          # 每批50只
# python fix/backfill_1m_mootdx.py --code 600519            # 单股票
# python fix/backfill_1m_mootdx.py --dry-run                # 只拉取不写库
# python fix/backfill_1m_mootdx.py --incremental            # 增量合并
#
# ============================================================================

from __future__ import annotations

import os
import sys
import json
import time
import signal
import logging
import argparse
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径 & 环境（同 source_sync.py）
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(PROJECT_ROOT, "backend_api_python")):
    _cwd = os.getcwd()
    if os.path.isdir(os.path.join(_cwd, "backend_api_python")):
        PROJECT_ROOT = _cwd
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend_api_python"))

_OPTIMIZER_DIR = os.path.dirname(os.path.abspath(__file__))
if _OPTIMIZER_DIR not in sys.path:
    sys.path.insert(0, _OPTIMIZER_DIR)


def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [
            os.path.join(PROJECT_ROOT, "backend_api_python", ".env"),
            os.path.join(PROJECT_ROOT, ".env"),
        ]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass


_load_env()

# ---------------------------------------------------------------------------
# 全局 socket 超时
# ---------------------------------------------------------------------------
import socket as _socket
_socket.setdefaulttimeout(120)

# ---------------------------------------------------------------------------
# 时间常量
# ---------------------------------------------------------------------------

TZ_SH = timezone(timedelta(hours=8))

# 通达信频率编码
_TDX_FREQ_1M = 7   # 1分钟线

# mootdx 每次最多拉取条数
_FETCH_BATCH_SIZE = 800

# 翻页最大 offset（防止拉到太早的数据撑爆内存）
# 800 * 60 ≈ 200 交易日 ≈ 1 年
_MAX_OFFSET = 800 * 60


# ---------------------------------------------------------------------------
# 交易日历（复用 source_sync.py 的逻辑）
# ---------------------------------------------------------------------------

_TRADING_DAYS_SORTED: List[str] = []
_TRADING_DAY_SET: Set[str] = set()


def _init_trading_calendar(silent: bool = False):
    global _TRADING_DAYS_SORTED, _TRADING_DAY_SET
    if _TRADING_DAY_SET:
        return
    from app.utils.trading_calendar import _load
    _TRADING_DAY_SET = _load()
    _TRADING_DAYS_SORTED = sorted(_TRADING_DAY_SET)
    if not silent:
        print(f"📅 交易日历: {len(_TRADING_DAY_SET)} 天")


def _is_trading_day(d: str) -> bool:
    if not _TRADING_DAY_SET:
        _init_trading_calendar(silent=True)
    return d in _TRADING_DAY_SET


# ---------------------------------------------------------------------------
# 板块判断（复用 source_sync.py）
# ---------------------------------------------------------------------------

def _detect_board(code: str) -> str:
    c = code[:3]
    if c in ("600", "601", "603", "605"):
        return "main_sh"
    if c in ("000", "001", "002", "003"):
        return "main_sz"
    if c in ("300", "301"):
        return "gem"
    if c in ("688", "689"):
        return "star"
    if code[:2] in ("43", "82", "83", "87", "88"):
        return "bj"
    return "unknown"


# ---------------------------------------------------------------------------
# 数据转换: mootdx DataFrame → 标准记录
# ---------------------------------------------------------------------------

def _safe_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        f = float(v)
        if f != f or f == float('inf'):  # NaN / Inf
            return default
        return f
    except (ValueError, TypeError):
        return default


def _df_to_records(df, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """mootdx DataFrame → 标准记录列表，过滤日期范围，去重"""
    if df is None or df.empty:
        return []

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=TZ_SH)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=TZ_SH)

    seen: Dict[datetime, Dict[str, Any]] = {}

    for _, row in df.iterrows():
        dt_val = row.get("datetime") or row.get("date")
        if dt_val is None:
            continue

        # 统一转为 datetime
        if isinstance(dt_val, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt_val = datetime.strptime(dt_val, fmt).replace(tzinfo=TZ_SH)
                    break
                except ValueError:
                    continue
        if not isinstance(dt_val, datetime):
            continue
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=TZ_SH)

        # 日期范围过滤
        if dt_val < start_dt or dt_val > end_dt:
            continue

        # 9:30 集合竞价丢弃
        if dt_val.hour == 9 and dt_val.minute == 30:
            continue

        # 非交易日丢弃（数据源可能返回周末/节假日占位 bar）
        if not _is_trading_day(dt_val.strftime("%Y-%m-%d")):
            continue

        o = _safe_float(row.get("open"))
        h = _safe_float(row.get("high"))
        l = _safe_float(row.get("low"))
        c = _safe_float(row.get("close"))
        v = _safe_float(row.get("vol", 0) or row.get("volume", 0))

        if o <= 0 and c <= 0:
            continue

        # OHLC 越界修正
        prices = [p for p in (o, h, l, c) if p > 0]
        if prices:
            if h > 0:
                h = max(h, *prices)
            if l > 0:
                l = min(l, *prices)

        # 去重
        if dt_val not in seen:
            seen[dt_val] = {
                "time": dt_val, "open": o, "high": h,
                "low": l, "close": c, "volume": v,
            }
        else:
            prev = seen[dt_val]
            prev_v = _safe_float(prev.get("volume"))
            if prev_v > 0 and v == 0:
                continue
            seen[dt_val] = {
                "time": dt_val, "open": o, "high": h,
                "low": l, "close": c, "volume": v,
            }

    return sorted(seen.values(), key=lambda r: r["time"])


# ---------------------------------------------------------------------------
# mootdx 数据拉取（翻页）
# ---------------------------------------------------------------------------

def _fetch_1m_paged(
    cli,
    code: str,
    start_date: str,
    end_date: str,
) -> Optional[List[Dict[str, Any]]]:
    """通过 mootdx bars(start=N) 翻页拉取 1m K 线，直到覆盖 start_date。

    mootdx bars() 参数:
      - start: 起始偏移（0=最新，800=往前跳800条）
      - offset: 每次条数（上限800）

    策略: start 从 0 递增 800，每次拉800条，直到最早 bar ≤ start_date 或无数据。

    Returns:
        标准记录列表，或 None（拉取失败）
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    _executor = ThreadPoolExecutor(max_workers=1)

    all_records: Dict[datetime, Dict[str, Any]] = {}
    page_start = 0
    reached_start = False
    max_pages = _MAX_OFFSET // _FETCH_BATCH_SIZE  # 最大翻页数
    page = 0

    while page < max_pages:
        page += 1
        try:
            def _do_fetch(s=page_start):
                return cli.bars(symbol=code, frequency=_TDX_FREQ_1M,
                                start=s, offset=_FETCH_BATCH_SIZE)

            future = _executor.submit(_do_fetch)
            try:
                df = future.result(timeout=30)
            except FuturesTimeoutError:
                logger.warning(f"[mootdx:1m] {code} start={page_start} 超时30s，停止翻页")
                break

            if df is None or df.empty:
                break

            # 转为记录（已过滤日期范围 + 去重）
            records = _df_to_records(df, start_date, end_date)

            # 检查原始数据的最早时间（用于判断是否已超出范围）
            raw_earliest = None
            first_raw = df.iloc[0].get("datetime") or df.iloc[0].get("date")
            if isinstance(first_raw, str):
                try:
                    raw_earliest = datetime.strptime(
                        first_raw[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    pass

            if not records:
                # 无目标范围数据：原始数据已早于 start_date → 到了
                if raw_earliest and raw_earliest < start_date:
                    reached_start = True
                break

            # 合并到总结果
            new_count = 0
            for rec in records:
                dt = rec["time"]
                if dt not in all_records:
                    all_records[dt] = rec
                    new_count += 1

            earliest = records[0]["time"]
            latest = records[-1]["time"]
            earliest_str = earliest.strftime("%Y-%m-%d %H:%M")
            logger.debug(f"[mootdx:1m] {code} page={page} start={page_start}: "
                         f"{len(records)}条(新增{new_count}), {earliest_str} ~ "
                         f"{latest.strftime('%Y-%m-%d %H:%M')}")

            # 检查是否已覆盖 start_date
            if earliest.strftime("%Y-%m-%d") <= start_date:
                reached_start = True
                break

            # 本页全是目标范围外的数据 → 到了
            if new_count == 0:
                break

            page_start += _FETCH_BATCH_SIZE

        except Exception as e:
            logger.warning(f"[mootdx:1m] {code} start={page_start} 拉取异常: {e}")
            if "connection" in str(e).lower() or "timeout" in str(e).lower():
                from app.utils.mootdx_client import reset_client as _reset
                _reset()
            break

    _executor.shutdown(wait=False)

    if not all_records:
        return None

    result = sorted(all_records.values(), key=lambda r: r["time"])
    earliest_dt = result[0]["time"].strftime("%Y-%m-%d")
    if not reached_start:
        logger.info(f"[mootdx:1m] {code} 翻页到头，最早={earliest_dt}（目标 start={start_date}）")

    return result


# ═══════════════════════════════════════════════════════
# DB 写入（复用 source_sync.py 的模式）
# ═══════════════════════════════════════════════════════

def write_batch_data(
    pool,
    timeframe: str,
    stock_records: Dict[str, List[Dict[str, Any]]],
    start_date: str,
    end_date: str,
    dry_run: bool = False,
) -> Dict[str, int]:
    """批量写入: 逐只 DELETE + INSERT（同 source_sync.py）"""
    if dry_run or not stock_records:
        return {}

    start_year = int(start_date[:4])
    end_year = int(end_date[:4])
    _VALID_TABLES = {f"kline_{tf}_{y}" for tf in ("1D", "15m", "1m") for y in range(2000, 2035)}

    # 预构建 db_records，按 (code, year) 分组
    records_by_year: Dict[str, Dict[int, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    code_row_counts: Dict[str, int] = {}

    start_cutoff = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=None)
    end_cutoff = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=None)

    for code, records in stock_records.items():
        count = 0
        for rec in records:
            ts = rec.get("time")
            if isinstance(ts, datetime):
                dt = ts
            elif isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts, tz=TZ_SH)
            else:
                continue
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            if dt < start_cutoff or dt > end_cutoff:
                continue
            records_by_year[code][dt.year].append({
                "symbol": code,
                "timeframe": timeframe,
                "time": dt,
                "open": _safe_float(rec.get("open")),
                "high": _safe_float(rec.get("high")),
                "low": _safe_float(rec.get("low")),
                "close": _safe_float(rec.get("close")),
                "volume": _safe_float(rec.get("volume")),
            })
            count += 1
        code_row_counts[code] = count

    all_codes = list(stock_records.keys())
    total_deleted = 0
    total_inserted = 0
    failed_codes: List[str] = []
    no_data_codes: List[str] = []

    for code in all_codes:
        code_rows = records_by_year.get(code, {})
        if not code_rows:
            no_data_codes.append(code)
            continue
        try:
            with pool.connection() as conn:
                cur = conn.cursor()
                for year, year_batch in code_rows.items():
                    table = f"kline_{timeframe}_{year}"
                    if table not in _VALID_TABLES:
                        continue
                    cur.execute(f"""
                        DELETE FROM "{table}"
                        WHERE symbol = %s
                          AND time >= %s AND time <= %s
                    """, (code, f"{start_date} 00:00:00", f"{end_date} 23:59:59"))
                    total_deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    for i in range(0, len(year_batch), 5000):
                        batch = year_batch[i:i + 5000]
                        cur.executemany(
                            f'INSERT INTO "{table}" '
                            f'(symbol, time, open, high, low, close, volume) '
                            f'VALUES (%(symbol)s, %(time)s, '
                            f'%(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)',
                            batch,
                        )
                        total_inserted += len(batch)
                conn.commit()
                cur.close()
        except Exception as e:
            failed_codes.append(code)
            logger.error("股票 %s 写入失败 (已回滚): %s", code, e)

    logger.info("批量写库: %d 只, 删除 %d, 插入 %d, 失败 %d, 无数据 %d",
                len(all_codes), total_deleted, total_inserted,
                len(failed_codes), len(no_data_codes))

    result = {code: code_row_counts.get(code, 0) for code in all_codes}
    for fc in failed_codes:
        result[fc] = -1
    for nc in no_data_codes:
        result[nc] = -2
    return result


# ═══════════════════════════════════════════════════════
# 增量合并（复用 source_sync.py）
# ═══════════════════════════════════════════════════════

def query_batch_existing(
    pool,
    symbols: List[str],
    timeframe: str,
    start_date: str,
    end_date: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """批量查询 DB 已有数据"""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    result: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    try:
        with pool.connection() as conn:
            cur = conn.cursor()
            for year in range(start_dt.year, end_dt.year + 1):
                table = f"kline_{timeframe}_{year}"
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                """, (table,))
                if cur.fetchone() is None:
                    continue
                cur.execute(f"""
                    SELECT symbol, time, open, high, low, close, volume
                    FROM "{table}"
                    WHERE symbol = ANY(%s)
                      AND time >= %s AND time <= %s
                """, (symbols, start_dt, end_dt))
                for row in cur.fetchall():
                    result[row[0]].append({
                        "time": row[1], "open": float(row[2] or 0),
                        "high": float(row[3] or 0), "low": float(row[4] or 0),
                        "close": float(row[5] or 0), "volume": float(row[6] or 0),
                    })
            cur.close()
    except Exception as e:
        logger.warning("查询 DB 已有数据失败: %s", e)
    return dict(result)


def merge_records(
    remote_recs: List[Dict[str, Any]],
    db_recs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """归一化合并: 同时间戳 remote volume>0 用 remote，否则保留 DB"""
    def _naive(dt):
        return dt.replace(tzinfo=None) if isinstance(dt, datetime) and dt.tzinfo else dt

    by_time: Dict[datetime, Dict[str, Any]] = {}
    for rec in db_recs:
        dt = _naive(rec.get("time"))
        if dt is not None:
            by_time[dt] = rec
    for rec in remote_recs:
        dt = _naive(rec.get("time"))
        if dt is None:
            continue
        if dt in by_time:
            if _safe_float(rec.get("volume")) > 0:
                by_time[dt] = rec
        else:
            by_time[dt] = rec
    return sorted(by_time.values(), key=lambda r: r["time"])


# ═══════════════════════════════════════════════════════
# 中断信号
# ═══════════════════════════════════════════════════════

_INTERRUPTED = False


def _signal_handler(signum, frame):
    global _INTERRUPTED
    if _INTERRUPTED:
        os._exit(130)
    _INTERRUPTED = True
    print("\n⚠️  收到中断信号，正在保存进度...")


# ═══════════════════════════════════════════════════════
# 检查点
# ═══════════════════════════════════════════════════════

def _checkpoint_path() -> str:
    return os.path.join(PROJECT_ROOT, "optimizer", ".checkpoint_backfill_1m.json")


def _load_checkpoint() -> Dict[str, Any]:
    path = _checkpoint_path()
    if not os.path.isfile(path):
        return {"processed_codes": [], "stats": {}}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"processed_codes": [], "stats": {}}


def _save_checkpoint(processed: list, stats: dict):
    path = _checkpoint_path()
    data = {"processed_codes": processed, "stats": stats,
            "saved_at": datetime.now(TZ_SH).isoformat()}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _remove_checkpoint():
    path = _checkpoint_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
# 重传文件
# ═══════════════════════════════════════════════════════

def _retry_path() -> str:
    return os.path.join(PROJECT_ROOT, "optimizer", ".retry_backfill_1m.json")


def _load_retry_codes() -> Dict[str, Dict[str, Any]]:
    path = _retry_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_retry_codes(data: Dict[str, Dict[str, Any]]):
    path = _retry_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
# 核心处理: 单批
# ═══════════════════════════════════════════════════════

def process_batch(
    symbols: List[str],
    cli,
    pool,
    start_date: str,
    end_date: str,
    dry_run: bool = False,
    incremental: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """处理一批股票: mootdx 拉取 → 写入"""
    stats = {"total": len(symbols), "fetched": 0, "written": 0, "failed": 0, "no_data": 0}
    results = []

    # 增量模式: 批量查 DB
    db_existing: Dict[str, List[Dict[str, Any]]] = {}
    if incremental:
        db_existing = query_batch_existing(pool, symbols, "1m", start_date, end_date)
        if db_existing:
            print(f"  📦 增量: 已查到 {len(db_existing)} 只 DB 数据")

    # 重传文件
    retry_data = _load_retry_codes()

    for i, code in enumerate(symbols):
        if _INTERRUPTED:
            break

        board = _detect_board(code)

        # 拉取
        records = _fetch_1m_paged(cli, code, start_date, end_date)

        if records is None:
            stats["no_data"] += 1
            retry_data[code] = {"errors": ["mootdx 无数据"], "retries": 0}
            results.append({
                "code": code, "board": board, "bars": 0,
                "first_bar": "", "last_bar": "",
                "written": 0, "status": "no_data", "errors": "mootdx 无数据",
            })
            continue

        stats["fetched"] += 1
        first_bar = records[0]["time"].strftime("%Y-%m-%d %H:%M")
        last_bar = records[-1]["time"].strftime("%Y-%m-%d %H:%M")

        # 增量合并
        if incremental and code in db_existing:
            before = len(records)
            records = merge_records(records, db_existing[code])
            logger.debug("[增量] %s: remote=%d + db=%d → merged=%d",
                         code, before, len(db_existing[code]), len(records))

        if not records:
            stats["no_data"] += 1
            results.append({
                "code": code, "board": board, "bars": 0,
                "first_bar": first_bar, "last_bar": last_bar,
                "written": 0, "status": "no_data", "errors": "合并后无数据",
            })
            continue

        # 写库
        if dry_run:
            stats["written"] += len(records)
            retry_data.pop(code, None)
            results.append({
                "code": code, "board": board, "bars": len(records),
                "first_bar": first_bar, "last_bar": last_bar,
                "written": len(records), "status": "ok", "errors": "",
            })
        else:
            written_map = write_batch_data(
                pool, "1m", {code: records}, start_date, end_date, dry_run)
            n = written_map.get(code, 0)
            if n < 0:
                stats["failed"] += 1
                retry_data[code] = {"errors": ["写入失败"], "retries": 0}
                results.append({
                    "code": code, "board": board, "bars": len(records),
                    "first_bar": first_bar, "last_bar": last_bar,
                    "written": 0, "status": "error", "errors": "写入失败",
                })
            else:
                stats["written"] += n
                retry_data.pop(code, None)
                results.append({
                    "code": code, "board": board, "bars": len(records),
                    "first_bar": first_bar, "last_bar": last_bar,
                    "written": n, "status": "ok", "errors": "",
                })

        # 进度
        if (i + 1) % 50 == 0 or (i + 1) == len(symbols):
            print(f"\r  [{i+1}/{len(symbols)}] "
                  f"拉取={stats['fetched']} 写入={stats['written']:,} "
                  f"失败={stats['failed']} 无数据={stats['no_data']}",
                  end='', flush=True)

    # 保存重传
    _save_retry_codes(retry_data)

    return results, stats


# ═══════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════

def main():
    global _INTERRUPTED

    parser = argparse.ArgumentParser(
        description="mootdx 直连下载 1 分钟 K 线 → kline_1m_YYYY",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--market", default="CNStock", help="市场（默认 CNStock）")
    parser.add_argument("--batch-size", type=int, default=100,
        help="每批处理股票数（默认 100）")
    parser.add_argument("--start-date", default="",
        help="数据起始日期 (YYYY-MM-DD)，默认自动探测服务器最早可用数据")
    parser.add_argument("--end-date", default="",
        help="数据截止日期 (YYYY-MM-DD)，默认为当天")
    parser.add_argument("--dry-run", action="store_true",
        help="只拉取不写库")
    parser.add_argument("--resume", action="store_true",
        help="断点续传：跳过已处理的股票")
    parser.add_argument("--retry-only", action="store_true",
        help="只重试重传文件中的股票")
    parser.add_argument("--incremental", action="store_true",
        help="增量模式: 与 DB 归一化合并后写入")
    parser.add_argument("--code", default="",
        help="单只股票代码 (如 600519)")

    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    now_date = datetime.now(TZ_SH).strftime('%Y-%m-%d')
    start_date = args.start_date if args.start_date else "2020-01-01"  # 通达信1m只保留~4个月，会自动停
    end_date = args.end_date if args.end_date else now_date

    # 导入 DB 模块
    from app.utils.db_market import get_market_db_manager
    from app.utils.mootdx_client import get_client as _get_mootdx_client

    mgr = get_market_db_manager()
    if not args.dry_run:
        if not mgr.market_db_exists(args.market):
            mgr.ensure_market_db(args.market)
    pool = mgr._get_pool(args.market)

    # 确保表存在
    for y in range(int(start_date[:4]), int(end_date[:4]) + 1):
        mgr.ensure_year_table(args.market, "1m", y)

    _init_trading_calendar()

    # 连接 mootdx
    print("\n[1/5] 连接 mootdx...")
    cli = _get_mootdx_client()
    if cli is None:
        print("❌ mootdx 连接失败")
        mgr.close_all_pools()
        return 1
    print("  ✅ mootdx 已连接")

    # 获取股票列表
    if args.code:
        code = args.code.strip().replace("SH", "").replace("SZ", "").replace(".", "")
        all_codes = [code]
        print(f"\n[2/5] 单股票模式: {code}")
    else:
        print("\n[2/5] 获取股票列表...")
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            db = get_stock_basic_db()
            all_stocks = db.get_all_stocks(status="active")
        except Exception as e:
            logger.error("获取股票列表失败: %s", e)
            mgr.close_all_pools()
            return 1
        all_codes = sorted(s["symbol"] for s in all_stocks)
        print(f"  共 {len(all_codes)} 只A股")

    # 断点续传
    processed_set: set = set()
    if args.resume and not args.retry_only:
        ckpt = _load_checkpoint()
        processed_set = set(ckpt.get("processed_codes", []))
        if processed_set:
            all_codes = [c for c in all_codes if c not in processed_set]
            print(f"  📂 断点续传: 已处理 {len(processed_set)} 只，剩余 {len(all_codes)} 只")

    # 重试模式
    if args.retry_only:
        retry_data = _load_retry_codes()
        all_codes = sorted(retry_data.keys())
        print(f"  🔄 重试模式: {len(all_codes)} 只")

    if not all_codes:
        print("  无需处理")
        _remove_checkpoint()
        mgr.close_all_pools()
        return 0

    total = len(all_codes)
    batch_size = min(args.batch_size, total)

    print(f"""
╔═══════════════════════════════════════════════════════╗
║  📡 mootdx 直连下载 1 分钟 K 线                       ║
╠═══════════════════════════════════════════════════════╣
║  日期: {start_date} → {end_date}                     ║
║  股票: {total} 只  批次: {batch_size}                        ║
║  模式: {'重试' if args.retry_only else '主循环'}{'  dry-run' if args.dry_run else ''}{'  增量' if args.incremental else ''}                         ║
╚═══════════════════════════════════════════════════════╝
""")

    print(f"\n[3/5] 拉取 + 写入...")

    all_results: List[Dict[str, Any]] = []
    agg_stats = {"total": 0, "fetched": 0, "written": 0, "failed": 0, "no_data": 0}
    t0 = time.time()
    batches = [all_codes[i:i + batch_size] for i in range(0, len(all_codes), batch_size)]

    for batch_idx, batch_codes in enumerate(batches):
        if _INTERRUPTED:
            break

        results, stats = process_batch(
            symbols=batch_codes,
            cli=cli,
            pool=pool,
            start_date=start_date,
            end_date=end_date,
            dry_run=args.dry_run,
            incremental=args.incremental,
        )

        all_results.extend(results)
        for k in agg_stats:
            agg_stats[k] += stats.get(k, 0)

        for code in batch_codes:
            processed_set.add(code)

        if (batch_idx + 1) % 5 == 0:
            _save_checkpoint(list(processed_set), agg_stats)

        print()  # 换行

    elapsed = time.time() - t0

    # 保存检查点
    _save_checkpoint(list(processed_set), agg_stats)

    # 汇总
    print(f"\n[4/5] 汇总统计")
    print(f"总耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  总计:   {agg_stats['total']}")
    print(f"  拉取:   {agg_stats['fetched']}")
    print(f"  写入:   {agg_stats['written']:,}")
    print(f"  失败:   {agg_stats['failed']}")
    print(f"  无数据: {agg_stats['no_data']}")

    # CSV 报告
    import csv
    csv_path = os.path.join(PROJECT_ROOT, "optimizer",
                            f"report_backfill_1m_{start_date}_{end_date}.csv")
    fields = ["code", "board", "bars", "first_bar", "last_bar",
              "written", "status", "errors"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_results:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"\n✅ CSV 报告: {csv_path}（{len(all_results)} 条）")

    # 清理
    remaining_retry = _load_retry_codes()
    if not remaining_retry and not _INTERRUPTED:
        _remove_checkpoint()
        try:
            if os.path.isfile(_retry_path()):
                os.remove(_retry_path())
        except Exception:
            pass

    print(f"\n[5/5] 完成")
    print(f"{'='*60}")
    if remaining_retry:
        print(f"  ⚠️  {len(remaining_retry)} 只失败，用 --retry-only 重试")
    elif _INTERRUPTED:
        print(f"  ⏸️  已中断，用 --resume 继续")
    else:
        print(f"  ✅ 全部完成!")
    print(f"{'='*60}")

    # 关闭连接（避免退出卡住）
    try:
        cli.close()
    except Exception:
        pass
    try:
        mgr.close_all_pools()
    except Exception:
        pass

    return 1 if (agg_stats['failed'] > 0 or _INTERRUPTED) else 0


def _force_exit():
    """强制退出，不等线程/连接清理"""
    os._exit(0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出。用 --resume 继续。")
        sys.exit(1)
    finally:
        # 强制退出：mootdx TCP / 线程池 / DB 连接池 可能卡住
        import threading as _t
        _t.Timer(3.0, _force_exit).start()
