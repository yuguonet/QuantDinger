#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# check_continuity.py — A股全市场数据连贯性 + 质量检查（多进程版）
# ============================================================================
#
# 功能:
#   1. 连贯性检查：扫描每个股票的全部时间戳，检测整段时间轴上的断裂
#      - 1D: 相邻两根之间跳过的交易日 > 0 → 中间断
#      - 15m: 跨天缺失交易日 → 中间断；同一天内 bar 序号不连续 → 日内断
#      - 末尾断：最后一根数据到今天之间还有交易日没数据
#
#   2. 质量检查：遍历每根 bar 的 OHLC，分类标记
#      - bad:        OHLC 全为 0（真实股价不可能为零，必须删除后重新拉取）
#      - suspended:  OHLC 全等 > 0 且 volume = 0（停牌，数据源用昨收填充）
#      - incomplete: OHLC 逻辑矛盾 / 部分字段为零 / 有价无量
#
# 输出:
#   - data_gap_report      — 断裂记录（含期望时间戳 JSONB，可供补数脚本使用）
#   - data_quality_report  — 质量问题记录（含 OHLC 原始值、处理建议、是否已解决）
#   - 可选 CSV 导出
#
# 多进程:
#   - 使用 multiprocessing.Pool 并行检查，默认 CPU 核数，上限 16
#   - 每个子进程独立建立 db_market 连接池，fork 后重置全局单例
#   - 支持 Ctrl+C 中断：已检查的部分正常写库
#
# 补数支持:
#   - gap_report 的 expected_ts 字段存储缺失的精确时间戳列表
#   - 1D: 期望的交易日 00:00 时间戳
#   - 15m 整日缺失: 该日 16 根 bar 的时间戳
#   - 15m 日内缺失: 精确缺失的那几根 bar 的时间戳
#
# 用法:
#   python optimizer/check_continuity.py                     # 全市场检查，只标记
#   python optimizer/check_continuity.py --fix               # 检查 + 删除坏数据行
#   python optimizer/check_continuity.py -w 8                # 指定 8 进程
#   python optimizer/check_continuity.py --symbol 600519     # 单只
#   python optimizer/check_continuity.py --dry-run            # 不写库
#   python optimizer/check_continuity.py --csv gaps.csv       # 导出 CSV
#   python optimizer/check_continuity.py --clear              # 写前清空旧报告数据
#
# 依赖:
#   - db_market.py（backend_api_python/app/utils/）
#   - psycopg2
#   - python-dotenv（可选，用于加载 .env）
#
# 创建时间: 2026-05-02
# ============================================================================

from __future__ import annotations

import os
import sys
import csv
import json
import signal
import argparse
import traceback
import multiprocessing as mp
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple
from collections import defaultdict

# ---------------------------------------------------------------------------
# 路径 & 环境
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend_api_python"))


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
# 时间常量 & 工具
# ---------------------------------------------------------------------------

TZ_SH = timezone(timedelta(hours=8))

HOLIDAYS = {
    "2021-01-01","2021-02-11","2021-02-12","2021-02-15","2021-02-16","2021-02-17",
    "2021-04-05","2021-05-03","2021-05-04","2021-05-05","2021-06-14",
    "2021-09-20","2021-09-21","2021-10-01","2021-10-04","2021-10-05","2021-10-06","2021-10-07",
    "2022-01-03","2022-01-31","2022-02-01","2022-02-02","2022-02-03","2022-02-04",
    "2022-04-04","2022-04-05","2022-05-02","2022-05-03","2022-05-04",
    "2022-06-03","2022-09-12","2022-10-03","2022-10-04","2022-10-05","2022-10-06","2022-10-07",
    "2023-01-02","2023-01-23","2023-01-24","2023-01-25","2023-01-26","2023-01-27",
    "2023-04-05","2023-05-01","2023-05-02","2023-05-03","2023-06-22","2023-06-23",
    "2023-09-29","2023-10-02","2023-10-03","2023-10-04","2023-10-05","2023-10-06",
    "2024-01-01","2024-02-09","2024-02-12","2024-02-13","2024-02-14","2024-02-15","2024-02-16",
    "2024-04-04","2024-04-05","2024-05-01","2024-05-02","2024-05-03",
    "2024-06-10","2024-09-16","2024-09-17","2024-10-01","2024-10-02","2024-10-03","2024-10-04","2024-10-07",
    "2025-01-01","2025-01-28","2025-01-29","2025-01-30","2025-01-31",
    "2025-02-03","2025-02-04","2025-04-04","2025-05-01","2025-05-02","2025-05-05",
    "2025-06-02","2025-10-01","2025-10-02","2025-10-03","2025-10-06","2025-10-07",
    "2026-01-01","2026-01-02","2026-02-17","2026-02-18","2026-02-19","2026-02-20","2026-02-23",
    "2026-04-06","2026-05-01","2026-06-19",
}

_TRADING_DAY_SET: frozenset[str] | None = None


def _build_trading_day_cache():
    global _TRADING_DAY_SET
    if _TRADING_DAY_SET is not None:
        return
    s = set()
    cur = datetime(2020, 1, 1)
    end = datetime(2027, 12, 31)
    while cur <= end:
        d = cur.strftime("%Y-%m-%d")
        if cur.weekday() < 5 and d not in HOLIDAYS:
            s.add(d)
        cur += timedelta(days=1)
    _TRADING_DAY_SET = frozenset(s)


def _is_trading_day(d: str) -> bool:
    _build_trading_day_cache()
    return d in _TRADING_DAY_SET


def _trading_days_between(d1: str, d2: str) -> int:
    if d1 >= d2:
        return 0
    _build_trading_day_cache()
    cur = datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(d2, "%Y-%m-%d")
    count = 0
    while cur < end:
        if cur.strftime("%Y-%m-%d") in _TRADING_DAY_SET:
            count += 1
        cur += timedelta(days=1)
    return count


def _ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=TZ_SH).strftime("%Y-%m-%d")


def _next_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _date_to_ts(d: str, hour: int = 0, minute: int = 0) -> int:
    dt = datetime.strptime(d, "%Y-%m-%d").replace(hour=hour, minute=minute, tzinfo=TZ_SH)
    return int(dt.timestamp())


# 15m bar 时间表
_MORNING_BARS = [(9, 30), (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15)]
_AFTERNOON_BARS = [(13, 0), (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45)]
_ALL_BAR_TIMES = _MORNING_BARS + _AFTERNOON_BARS


def _bar_index_in_day(ts: int) -> int:
    dt = datetime.fromtimestamp(ts, tz=TZ_SH)
    hm = (dt.hour, dt.minute)
    try:
        return _ALL_BAR_TIMES.index(hm)
    except ValueError:
        return -1


def _expected_15m_ts_for_date(d: str) -> List[int]:
    base = datetime.strptime(d, "%Y-%m-%d")
    return [
        int(base.replace(hour=h, minute=m, tzinfo=TZ_SH).timestamp())
        for h, m in _ALL_BAR_TIMES
    ]


def _expected_1d_ts_between(d1: str, d2: str) -> List[int]:
    if d1 >= d2:
        return []
    _build_trading_day_cache()
    result = []
    cur = datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(d2, "%Y-%m-%d")
    while cur < end:
        if cur.strftime("%Y-%m-%d") in _TRADING_DAY_SET:
            result.append(int(cur.replace(tzinfo=TZ_SH).timestamp()))
        cur += timedelta(days=1)
    return result


def _expected_15m_ts_between_dates(d1: str, d2: str) -> List[int]:
    if d1 >= d2:
        return []
    _build_trading_day_cache()
    result = []
    cur = datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(d2, "%Y-%m-%d")
    while cur < end:
        ds = cur.strftime("%Y-%m-%d")
        if ds in _TRADING_DAY_SET:
            result.extend(_expected_15m_ts_for_date(ds))
        cur += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# 数据质量分类
# ---------------------------------------------------------------------------

def classify_bar(bar: Dict[str, Any]) -> str:
    """
    对单根 K 线做质量分类。

    Returns:
        "ok"         — 正常
        "suspended"  — 停牌（OHLC 全等且 > 0，volume = 0）
        "bad"        — 坏数据（OHLC 全为 0，必须删除后重新拉取）
        "incomplete" — 数据不完整（volume 缺失 / 部分字段为 0 / OHLC 逻辑矛盾）
    """
    o = bar.get("open", 0)
    h = bar.get("high", 0)
    l = bar.get("low", 0)
    c = bar.get("close", 0)
    v = bar.get("volume", 0)

    # 全零 → 坏数据（真实股价不可能为 0）
    if o == 0 and h == 0 and l == 0 and c == 0:
        return "bad"

    # OHLC 全等且 volume=0 → 停牌（数据源用昨收填充）
    if v == 0 and o == h == l == c and o > 0:
        return "suspended"

    # OHLC 逻辑矛盾
    if h > 0 and l > 0 and (h < l or (o > 0 and (o > h or o < l)) or (c > 0 and (c > h or c < l))):
        return "incomplete"

    # volume=0 但价格有变化（不是停牌）→ 可能 volume 缺失
    if v == 0 and not (o == h == l == c):
        return "incomplete"

    # 部分字段为 0（有价格但不完整）
    if (o == 0) != (h == 0) or (h == 0) != (l == 0) or (l == 0) != (c == 0):
        return "incomplete"

    return "ok"


# ---------------------------------------------------------------------------
# 连贯性检测
# ---------------------------------------------------------------------------

def check_1d_gaps(symbol: str, records: List[Dict[str, Any]], today: str) -> List[Dict[str, Any]]:
    if len(records) < 2:
        return []
    gaps = []
    ts_list = sorted(r["time"] for r in records)
    dates = [_ts_to_date(t) for t in ts_list]

    for i in range(1, len(dates)):
        skipped = _trading_days_between(dates[i - 1], dates[i])
        if skipped > 0:
            expected_ts = _expected_1d_ts_between(dates[i - 1], dates[i])
            gaps.append({
                "symbol": symbol, "timeframe": "1D", "gap_type": "middle",
                "gap_start_date": _next_day(dates[i - 1]),
                "gap_end_date": _prev_day(dates[i]),
                "missing_bars": skipped, "expected_ts": expected_ts,
            })

    last_date = dates[-1]
    if last_date < today:
        trailing = _trading_days_between(last_date, today)
        if _is_trading_day(today):
            trailing += 1
        if trailing > 0:
            expected_ts = _expected_1d_ts_between(last_date, today)
            if _is_trading_day(today):
                expected_ts.append(_date_to_ts(today))
            gaps.append({
                "symbol": symbol, "timeframe": "1D", "gap_type": "tail",
                "gap_start_date": _next_day(last_date),
                "gap_end_date": today,
                "missing_bars": trailing, "expected_ts": expected_ts,
            })
    return gaps


def check_15m_gaps(symbol: str, records: List[Dict[str, Any]], today: str) -> List[Dict[str, Any]]:
    if len(records) < 2:
        return []
    gaps = []
    ts_list = sorted(r["time"] for r in records)

    for i in range(1, len(ts_list)):
        t_prev, t_curr = ts_list[i - 1], ts_list[i]
        d_prev = _ts_to_date(t_prev)
        d_curr = _ts_to_date(t_curr)

        if d_prev != d_curr:
            skipped = _trading_days_between(d_prev, d_curr)
            if skipped > 0:
                expected_ts = _expected_15m_ts_between_dates(d_prev, d_curr)
                gaps.append({
                    "symbol": symbol, "timeframe": "15m", "gap_type": "middle",
                    "gap_start_date": _next_day(d_prev),
                    "gap_end_date": _prev_day(d_curr),
                    "missing_bars": skipped * 16, "expected_ts": expected_ts,
                })
        else:
            idx_prev = _bar_index_in_day(t_prev)
            idx_curr = _bar_index_in_day(t_curr)
            if idx_prev < 0 or idx_curr < 0:
                continue
            gap_bars = idx_curr - idx_prev - 1
            if gap_bars > 0:
                all_ts = _expected_15m_ts_for_date(d_prev)
                expected_ts = all_ts[idx_prev + 1: idx_curr]
                gaps.append({
                    "symbol": symbol, "timeframe": "15m", "gap_type": "intraday",
                    "gap_start_date": d_prev, "gap_end_date": d_prev,
                    "missing_bars": gap_bars, "expected_ts": expected_ts,
                })

    last_date = _ts_to_date(ts_list[-1])
    if last_date < today:
        trailing = _trading_days_between(last_date, today)
        if _is_trading_day(today):
            trailing += 1
        if trailing > 0:
            expected_ts = _expected_15m_ts_between_dates(last_date, today)
            if _is_trading_day(today):
                expected_ts.extend(_expected_15m_ts_for_date(today))
            gaps.append({
                "symbol": symbol, "timeframe": "15m", "gap_type": "tail",
                "gap_start_date": _next_day(last_date),
                "gap_end_date": today,
                "missing_bars": trailing * 16, "expected_ts": expected_ts,
            })
    return gaps


def check_quality(
    symbol: str, timeframe: str, records: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    质量检查：遍历每根 bar，分类并收集问题。

    Returns:
        (issues, bad_records)
        - issues: 质量问题列表（写入 quality_report）
        - bad_records: 坏数据记录（OHLC 全零，需要删除）
    """
    issues = []
    bad_records = []

    for rec in records:
        cls = classify_bar(rec)
        if cls == "ok":
            continue

        ts = rec["time"]
        ohlc = {
            "open": rec.get("open", 0),
            "high": rec.get("high", 0),
            "low": rec.get("low", 0),
            "close": rec.get("close", 0),
            "volume": rec.get("volume", 0),
        }

        if cls == "bad":
            expected_action = "delete"
        elif cls == "suspended":
            expected_action = "skip"
        else:  # incomplete
            expected_action = "review"

        issues.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_time": ts,
            "bar_date": _ts_to_date(ts),
            "issue_type": cls,
            "ohlc": ohlc,
            "expected_action": expected_action,
        })

        if cls == "bad":
            bad_records.append({"symbol": symbol, "timeframe": timeframe, "time": ts})

    return issues, bad_records


# ---------------------------------------------------------------------------
# 子进程工作函数
# ---------------------------------------------------------------------------

def _worker_check_batch(args: Tuple) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[str, str]]
]:
    """
    子进程入口。
    Returns: (gap_list, quality_issues, bad_records, errors)
    """
    symbols, market, today = args

    # fork 后重置全局单例，建立独立连接池
    import app.utils.db_market as _dbm
    if _dbm._manager is not None:
        try:
            _dbm._manager.close_all_pools()
        except Exception:
            pass
    _dbm._manager = None
    _dbm._writer = None

    from app.utils.db_market import get_market_kline_writer
    writer = get_market_kline_writer()

    local_gaps: List[Dict[str, Any]] = []
    local_issues: List[Dict[str, Any]] = []
    local_bad: List[Dict[str, Any]] = []
    local_errors: List[Tuple[str, str]] = []

    for sym in symbols:
        try:
            # 1D
            r1d = writer.query(market, sym, "1D", limit=0)
            if r1d:
                local_gaps.extend(check_1d_gaps(sym, r1d, today))
                issues, bad = check_quality(sym, "1D", r1d)
                local_issues.extend(issues)
                local_bad.extend(bad)

            # 15m
            r15m = writer.query(market, sym, "15m", limit=0)
            if r15m:
                local_gaps.extend(check_15m_gaps(sym, r15m, today))
                issues, bad = check_quality(sym, "15m", r15m)
                local_issues.extend(issues)
                local_bad.extend(bad)
        except Exception as e:
            local_errors.append((sym, f"{type(e).__name__}: {e}"))

    try:
        _dbm._manager.close_all_pools()
    except Exception:
        pass

    return local_gaps, local_issues, local_bad, local_errors


# ---------------------------------------------------------------------------
# 数据库操作
# ---------------------------------------------------------------------------

GAP_TABLE = "data_gap_report"
QUALITY_TABLE = "data_quality_report"

DDL_GAP = f"""
CREATE TABLE IF NOT EXISTS "{GAP_TABLE}" (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    timeframe       VARCHAR(10) NOT NULL,
    gap_type        VARCHAR(20) NOT NULL,
    gap_start_date  VARCHAR(10) NOT NULL,
    gap_end_date    VARCHAR(10) NOT NULL,
    missing_bars    INTEGER     NOT NULL DEFAULT 0,
    expected_ts     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    checked_at      TIMESTAMP   DEFAULT NOW(),
    UNIQUE (symbol, timeframe, gap_type, gap_start_date, gap_end_date)
)
"""

DDL_QUALITY = f"""
CREATE TABLE IF NOT EXISTS "{QUALITY_TABLE}" (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    timeframe       VARCHAR(10) NOT NULL,
    bar_time        BIGINT      NOT NULL,
    bar_date        VARCHAR(10) NOT NULL,
    issue_type      VARCHAR(20) NOT NULL,
    ohlc            JSONB,
    expected_action VARCHAR(20) NOT NULL DEFAULT 'review',
    resolved        BOOLEAN     DEFAULT FALSE,
    checked_at      TIMESTAMP   DEFAULT NOW(),
    UNIQUE (symbol, timeframe, bar_time, issue_type)
)
"""


def ensure_tables(pool):
    with pool.cursor() as cur:
        cur.execute(DDL_GAP)
        cur.execute(DDL_QUALITY)
        # 兼容旧表升级
        cur.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = '{GAP_TABLE}' AND column_name = 'expected_ts'
                ) THEN
                    ALTER TABLE "{GAP_TABLE}"
                    ADD COLUMN expected_ts JSONB NOT NULL DEFAULT '[]'::jsonb;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = '{QUALITY_TABLE}' AND column_name = 'resolved'
                ) THEN
                    ALTER TABLE "{QUALITY_TABLE}"
                    ADD COLUMN resolved BOOLEAN DEFAULT FALSE;
                END IF;
            END$$;
        """)


def write_gaps(pool, gaps: List[Dict[str, Any]], batch_size: int = 500):
    if not gaps:
        return 0
    sql = f"""
        INSERT INTO "{GAP_TABLE}"
            (symbol, timeframe, gap_type, gap_start_date, gap_end_date,
             missing_bars, expected_ts)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (symbol, timeframe, gap_type, gap_start_date, gap_end_date)
        DO UPDATE SET
            missing_bars = EXCLUDED.missing_bars,
            expected_ts  = EXCLUDED.expected_ts,
            checked_at   = NOW()
    """
    written = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            for start in range(0, len(gaps), batch_size):
                batch = gaps[start:start + batch_size]
                args_list = [
                    (g["symbol"], g["timeframe"], g["gap_type"],
                     g["gap_start_date"], g["gap_end_date"],
                     g["missing_bars"], json.dumps(g.get("expected_ts", []), separators=(",", ":")))
                    for g in batch
                ]
                cur.executemany(sql, args_list)
                written += len(batch)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return written


def write_quality(pool, issues: List[Dict[str, Any]], batch_size: int = 500):
    if not issues:
        return 0
    sql = f"""
        INSERT INTO "{QUALITY_TABLE}"
            (symbol, timeframe, bar_time, bar_date, issue_type, ohlc, expected_action)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (symbol, timeframe, bar_time, issue_type)
        DO UPDATE SET
            ohlc            = EXCLUDED.ohlc,
            expected_action = EXCLUDED.expected_action,
            resolved        = FALSE,
            checked_at      = NOW()
    """
    written = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            for start in range(0, len(issues), batch_size):
                batch = issues[start:start + batch_size]
                args_list = [
                    (iss["symbol"], iss["timeframe"], iss["bar_time"],
                     iss["bar_date"], iss["issue_type"],
                     json.dumps(iss["ohlc"], separators=(",", ":")),
                     iss["expected_action"])
                    for iss in batch
                ]
                cur.executemany(sql, args_list)
                written += len(batch)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return written


def delete_bad_records(pool, market: str, bad_records: List[Dict[str, Any]]) -> int:
    """
    从 kline 分区表中删除坏数据行（OHLC 全零）。
    按 (timeframe, year) 分组，批量删除。
    """
    if not bad_records:
        return 0

    # 按 (timeframe, year) 分组
    groups: Dict[Tuple[str, int], List[Tuple[str, int]]] = defaultdict(list)
    for rec in bad_records:
        year = datetime.fromtimestamp(rec["time"], tz=TZ_SH).year
        groups[(rec["timeframe"], year)].append((rec["symbol"], rec["time"]))

    total_deleted = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            for (tf, year), entries in groups.items():
                table = f"kline_{tf}_{year}"
                # 检查表是否存在
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                """, (table,))
                if cur.fetchone() is None:
                    continue

                # 批量删除（每批 500 条）
                for start in range(0, len(entries), 500):
                    batch = entries[start:start + 500]
                    conditions = []
                    params = []
                    for sym, ts in batch:
                        conditions.append("(symbol = %s AND time = %s)")
                        params.extend([sym, ts])
                    where = " OR ".join(conditions)
                    cur.execute(f'DELETE FROM "{table}" WHERE {where}', params)
                    total_deleted += cur.rowcount

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return total_deleted


def clear_tables(pool):
    with pool.cursor() as cur:
        cur.execute(f'DELETE FROM "{GAP_TABLE}"')
        cur.execute(f'DELETE FROM "{QUALITY_TABLE}"')


def export_csv(gaps, issues, path):
    if not gaps and not issues:
        print("无数据，跳过 CSV")
        return

    # gaps
    gap_fields = ["symbol", "timeframe", "gap_type", "gap_start_date",
                  "gap_end_date", "missing_bars", "expected_ts"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=gap_fields, extrasaction="ignore")
        w.writeheader()
        for g in gaps:
            row = dict(g)
            row["expected_ts"] = json.dumps(row.get("expected_ts", []), separators=(",", ":"))
            w.writerow(row)

    # quality issues
    q_path = path.replace(".csv", "_quality.csv")
    if issues:
        q_fields = ["symbol", "timeframe", "bar_time", "bar_date",
                     "issue_type", "ohlc", "expected_action"]
        with open(q_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=q_fields, extrasaction="ignore")
            w.writeheader()
            for iss in issues:
                row = dict(iss)
                row["ohlc"] = json.dumps(row["ohlc"], separators=(",", ":"))
                w.writerow(row)

    print(f"✅ CSV: {path}（{len(gaps)} 条断裂）")
    if issues:
        print(f"✅ CSV: {q_path}（{len(issues)} 条质量问题）")


# ---------------------------------------------------------------------------
# 中断信号
# ---------------------------------------------------------------------------

_INTERRUPTED = False


def _signal_handler(signum, frame):
    global _INTERRUPTED
    if _INTERRUPTED:
        print("\n⚡ 强制退出")
        sys.exit(1)
    _INTERRUPTED = True
    print("\n⚠️  收到中断信号，正在保存已检查的结果...")


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------

def main():
    global _INTERRUPTED

    parser = argparse.ArgumentParser(description="A股全市场数据连贯性 + 质量检查（多进程）")
    parser.add_argument("--symbol", help="只检查指定股票")
    parser.add_argument("--market", default="CNStock", help="市场（默认 CNStock）")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="进程数（默认 CPU 核数，上限 16）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    parser.add_argument("--fix", action="store_true",
                        help="删除坏数据行（OHLC 全零），默认只标记不删")
    parser.add_argument("--csv", help="导出 CSV 路径")
    parser.add_argument("--clear", action="store_true", help="写入前清空旧报告数据")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="每个子进程一次处理的股票数（默认 50）")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    from app.utils.db_market import get_market_kline_writer, get_market_db_manager
    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    market = args.market
    today = datetime.now(TZ_SH).strftime("%Y-%m-%d")

    if not mgr.market_db_exists(market):
        print(f"❌ {market}_db 不存在")
        return 1

    stats = writer.stats(market)
    all_syms = stats.get("symbol_list", [])
    if not all_syms:
        print(f"❌ 无股票数据")
        return 1

    if args.symbol:
        if args.symbol not in all_syms:
            print(f"❌ {args.symbol} 不在 {market}_db 中（共 {len(all_syms)} 只）")
            return 1
        syms = [args.symbol]
    else:
        syms = all_syms

    total = len(syms)
    cpu_count = mp.cpu_count()
    n_workers = min(args.workers or cpu_count, 16, total)
    if args.symbol:
        n_workers = 1
    batch_size = max(1, min(args.batch_size, total // n_workers + 1))

    print(f"📊 连贯性 + 质量检查 | {market} | {total} 只 | {today}")
    print(f"   进程: {n_workers} | 每批: {batch_size} | "
          f"模式: {'dry-run' if args.dry_run else '写库'} | "
          f"坏数据: {'删除' if args.fix else '仅标记'}")
    print()

    _build_trading_day_cache()

    batches = [syms[i:i + batch_size] for i in range(0, total, batch_size)]

    all_gaps: List[Dict[str, Any]] = []
    all_issues: List[Dict[str, Any]] = []
    all_bad: List[Dict[str, Any]] = []
    all_errors: List[Tuple[str, str]] = []
    processed_syms = 0
    done_batches = 0

    if n_workers <= 1:
        for sym in syms:
            if _INTERRUPTED:
                break
            try:
                r1d = writer.query(market, sym, "1D", limit=0)
                if r1d:
                    all_gaps.extend(check_1d_gaps(sym, r1d, today))
                    iss, bad = check_quality(sym, "1D", r1d)
                    all_issues.extend(iss)
                    all_bad.extend(bad)

                r15m = writer.query(market, sym, "15m", limit=0)
                if r15m:
                    all_gaps.extend(check_15m_gaps(sym, r15m, today))
                    iss, bad = check_quality(sym, "15m", r15m)
                    all_issues.extend(iss)
                    all_bad.extend(bad)
            except Exception as e:
                all_errors.append((sym, f"{type(e).__name__}: {e}"))
            processed_syms += 1
            if processed_syms % 200 == 0 or processed_syms == total:
                print(f"  [{processed_syms}/{total}] 断裂={len(all_gaps)} "
                      f"质量问题={len(all_issues)} 坏数据={len(all_bad)} "
                      f"错误={len(all_errors)}")
    else:
        task_args = [(batch, market, today) for batch in batches]
        pool = mp.Pool(n_workers)
        try:
            results = pool.imap_unordered(_worker_check_batch, task_args, chunksize=1)
            for batch_gaps, batch_issues, batch_bad, batch_errors in results:
                if _INTERRUPTED:
                    break
                all_gaps.extend(batch_gaps)
                all_issues.extend(batch_issues)
                all_bad.extend(batch_bad)
                all_errors.extend(batch_errors)
                done_batches += 1
                processed_syms = min(done_batches * batch_size, total)
                if done_batches % max(1, len(batches) // 20) == 0 or done_batches == len(batches):
                    print(f"  [{processed_syms}/{total}] 断裂={len(all_gaps)} "
                          f"质量问题={len(all_issues)} 坏数据={len(all_bad)} "
                          f"错误={len(all_errors)}")
        except KeyboardInterrupt:
            _INTERRUPTED = True
        finally:
            pool.terminate()
            pool.join()

    # ---- 汇总 ----
    print(f"\n{'='*60}")
    status = "中断" if _INTERRUPTED else "完成"
    print(f"检查{status}: {processed_syms}/{total} 只")
    print(f"  断裂: {len(all_gaps)} 条")
    print(f"  质量问题: {len(all_issues)} 条（坏数据={len(all_bad)}）")
    print(f"  查询错误: {len(all_errors)} 只")

    if all_errors:
        print(f"\n⚠️  查询失败（前 10 只）:")
        for sym, msg in all_errors[:10]:
            print(f"  {sym}: {msg}")
        if len(all_errors) > 10:
            print(f"  ... 还有 {len(all_errors) - 10} 只")

    if all_gaps:
        by_tf = defaultdict(int)
        by_type = defaultdict(int)
        for g in all_gaps:
            by_tf[g["timeframe"]] += 1
            by_type[g["gap_type"]] += 1
        print(f"\n断裂分布:")
        print(f"  15m: {by_tf['15m']} 条 | 1D: {by_tf['1D']} 条")
        print(f"  middle: {by_type['middle']} | tail: {by_type['tail']} | intraday: {by_type['intraday']}")

        all_gaps.sort(key=lambda g: g["missing_bars"], reverse=True)
        print(f"\n断裂最严重的 10 条:")
        for g in all_gaps[:10]:
            print(f"  {g['symbol']:>8} | {g['timeframe']:>3} | {g['gap_type']:>8} | "
                  f"{g['gap_start_date']}~{g['gap_end_date']} | 缺 {g['missing_bars']} 根")

    if all_issues:
        by_itype = defaultdict(int)
        by_action = defaultdict(int)
        for iss in all_issues:
            by_itype[iss["issue_type"]] += 1
            by_action[iss["expected_action"]] += 1
        print(f"\n质量问题分布:")
        print(f"  bad(全零): {by_itype['bad']} | suspended(停牌): {by_itype['suspended']} | "
              f"incomplete: {by_itype['incomplete']}")
        print(f"  需删除: {by_action['delete']} | 跳过: {by_action['skip']} | 待确认: {by_action['review']}")

        # bad 数据示例
        bad_samples = [i for i in all_issues if i["issue_type"] == "bad"]
        if bad_samples:
            print(f"\n坏数据示例（前 5 条）:")
            for iss in bad_samples[:5]:
                print(f"  {iss['symbol']:>8} | {iss['timeframe']:>3} | "
                      f"{iss['bar_date']} | ohlc={iss['ohlc']}")

    # ---- 写库 ----
    if not args.dry_run:
        try:
            pool_db = mgr._get_pool(market)
            ensure_tables(pool_db)

            if args.clear:
                clear_tables(pool_db)
                print(f"\n🗑️  已清空旧报告数据")

            if all_gaps:
                n = write_gaps(pool_db, all_gaps)
                print(f"✅ 写入 {GAP_TABLE}: {n} 条")

            if all_issues:
                n = write_quality(pool_db, all_issues)
                print(f"✅ 写入 {QUALITY_TABLE}: {n} 条")

            # 删除坏数据
            if args.fix and all_bad:
                deleted = delete_bad_records(pool_db, market, all_bad)
                print(f"🗑️  已删除坏数据行: {deleted} 条")
                # 标记为已解决
                with pool_db.cursor() as cur:
                    cur.execute(f"""
                        UPDATE "{QUALITY_TABLE}"
                        SET resolved = TRUE
                        WHERE issue_type = 'bad' AND expected_action = 'delete'
                    """)
                print(f"✅ 已标记坏数据为 resolved")

        except Exception as e:
            print(f"\n❌ 数据库操作失败: {e}")
            traceback.print_exc()
            fallback = args.csv or "gaps_fallback.csv"
            export_csv(all_gaps, all_issues, fallback)
            print(f"⚠️  已回退导出到 {fallback}")

    if args.csv:
        export_csv(all_gaps, all_issues, args.csv)

    mgr.close_all_pools()
    return 1 if (all_errors or _INTERRUPTED) else 0


if __name__ == "__main__":
    sys.exit(main())
