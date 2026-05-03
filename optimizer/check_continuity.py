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
#   3. 退市股清理：检测最后数据距今超过阈值的股票，删除其 15m 和 1D 数据
#      - 阈值默认 60 天，可通过 --delist-threshold 调整
#      - 需配合 --clean-delist 开启
#
#   4. 1D 缺失补全：检查 1D 缺失日期是否有 15m 数据，有则聚合写入 1D 表
#      - 聚合规则：open=第一根, high=MAX, low=MIN, close=最后一根, volume=SUM
#      - 至少需要 8/16 根 15m bar 才算有效
#      - 需配合 --fill-1d 开启
#
#   5. 双缺失批量写入：15m 和 1D 都缺失的日期，批量写入 data_issue_report
#      - issue_type 为 "both_missing"，expected_action 为 "fetch"
#
# 时间格式:
#   - 数据库 time 列为 TIMESTAMP 类型
#   - expected_ts 存储 datetime 对象，序列化为 ISO 字符串写入 JSONB
#   - 通达信 CSV 标准格式：15m="YYYY-MM-DD HH:MM", 1D="YYYY-MM-DD"
#
# 输出:
#   - data_issue_report    — 统一问题表（断裂 + 质量问题合并存储）
#   - 可选 CSV 导出
#
# 多进程:
#   - 使用 multiprocessing.Pool 并行检查，默认 CPU 核数，上限 16
#   - 每个子进程独立建立 db_market 连接池，fork 后重置全局单例
#   - 支持 Ctrl+C 中断：已检查的部分正常写库
#
# 用法:
#   python optimizer/check_continuity.py                     # 全市场检查，只标记
#   python optimizer/check_continuity.py --fix               # 检查 + 删除坏数据行
#   python optimizer/check_continuity.py -w 8                # 指定 8 进程
#   python optimizer/check_continuity.py --symbol 600519     # 单只
#   python optimizer/check_continuity.py --dry-run            # 不写库
#   python optimizer/check_continuity.py --csv gaps.csv       # 导出 CSV
#   python optimizer/check_continuity.py --clear              # 写前清空旧报告数据
#   python optimizer/check_continuity.py --clean-delist       # 清理退市股（15m+1D）
#   python optimizer/check_continuity.py --fill-1d            # 用 15m 聚合补全 1D 缺失
#   python optimizer/check_continuity.py --clean-delist --fill-1d --fix  # 全量修复
#
# 依赖:
#   - db_market.py / db_multi.py（backend_api_python/app/utils/）
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
from typing import Any, Dict, List, Optional, Tuple
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

_TRADING_DAY_SET: frozenset[str] | None = None


def _fetch_trading_days_from_akshare() -> set[str]:
    """从 akshare 拉取沪深交易所真实交易日历（含调休日）"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = set()
        for v in df["trade_date"]:
            d = str(v)[:10]
            dates.add(d)
        return dates
    except Exception as e:
        print(f"⚠️  akshare 拉取交易日历失败: {e}")
        return set()


def _deduce_trading_days_from_db(market: str, min_stocks: int = 10) -> set[str]:
    """
    从数据库反推交易日历：统计每天有数据的股票数，>= min_stocks 的视为交易日。
    直接查 kline_1D_* 分区表，按日期分组计数。
    """
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool(market)

        with pool.connection() as conn:
            cur = conn.cursor()
            # 找所有 1D 分区表
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'kline_1D_%'
                  AND table_name NOT LIKE '%%_from_%%'
                ORDER BY table_name
            """)
            tables = [r[0] for r in cur.fetchall()]

            if not tables:
                return set()

            # union all 查所有日期的股票数
            union_parts = []
            for t in tables:
                union_parts.append(f'SELECT time FROM "{t}"')
            sql = " UNION ALL ".join(union_parts)
            # time 列为 TIMESTAMP 类型，直接用 AT TIME ZONE 转换
            cur.execute(f"""
                SELECT to_char(time AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') AS d, COUNT(*)
                FROM ({sql}) sub
                GROUP BY d
                HAVING COUNT(*) >= %s
            """, (min_stocks,))
            dates = {r[0] for r in cur.fetchall()}
            return dates
    except Exception as e:
        print(f"⚠️  数据库反推交易日历失败: {e}")
        return set()


def _build_trading_day_cache(market: str = "CNStock"):
    """
    构建交易日集合（两级 + 合并）:
      1. akshare — 新浪真实交易日历（含调休，排除假日）
      2. 数据库反推 — 统计每天有数据的股票数，>=10 只视为交易日

    如果 akshare 日历过期（不含数据最新日期），自动合并数据库反推的结果。

    Args:
        market: 市场标识，用于数据库反推
    """
    global _TRADING_DAY_SET
    if _TRADING_DAY_SET is not None:
        return

    akshare_dates = _fetch_trading_days_from_akshare()
    db_dates = _deduce_trading_days_from_db(market)

    # 检查 akshare 日历是否覆盖数据范围
    akshare_max = max(akshare_dates) if akshare_dates else ""
    db_max = max(db_dates) if db_dates else ""
    data_max = max(akshare_max, db_max)

    if akshare_dates and len(akshare_dates) > 100:
        if db_dates and db_max > akshare_max:
            # akshare 日历过期，合并数据库反推的日期
            merged = akshare_dates | db_dates
            _TRADING_DAY_SET = frozenset(merged)
            print(f"📅 交易日历: akshare + 数据库合并（{len(merged)} 天，"
                  f"akshare 截至 {akshare_max}，数据截至 {db_max}）")
        else:
            _TRADING_DAY_SET = frozenset(akshare_dates)
            print(f"📅 交易日历: akshare（{len(akshare_dates)} 天）")
        return

    if db_dates and len(db_dates) > 100:
        _TRADING_DAY_SET = frozenset(db_dates)
        print(f"📅 交易日历: 数据库反推（{len(db_dates)} 天）")
        return

    print("❌ 无法确定交易日历（akshare 不可用且数据库无足够数据）")
    _TRADING_DAY_SET = frozenset()


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


def _ts_to_date(ts) -> str:
    """从 datetime 对象或整数时间戳提取日期字符串"""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=TZ_SH)
        return ts.strftime("%Y-%m-%d")
    return datetime.fromtimestamp(ts, tz=TZ_SH).strftime("%Y-%m-%d")


def _next_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _date_to_ts(d: str, hour: int = 0, minute: int = 0) -> datetime:
    """日期字符串 → datetime 对象（TIMESTAMP 兼容）"""
    return datetime.strptime(d, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=TZ_SH
    )


# 15m bar 时间表
_MORNING_BARS = [(9, 30), (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15)]
_AFTERNOON_BARS = [(13, 0), (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45)]
_ALL_BAR_TIMES = _MORNING_BARS + _AFTERNOON_BARS



def _ts_to_dhm(ts) -> Tuple[str, int, int]:
    """时间戳 → (日期, 时, 分)，用于 15m 比对，忽略秒级精度
    支持 datetime 对象或整数时间戳"""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
    else:
        dt = datetime.fromtimestamp(ts, tz=TZ_SH)
    return (dt.strftime("%Y-%m-%d"), dt.hour, dt.minute)


def _dhm_to_bar_index(dhm: Tuple[str, int, int]) -> int:
    """(日期, 时, 分) → bar 序号，-1 表示不在交易时间表内"""
    try:
        return _ALL_BAR_TIMES.index((dhm[1], dhm[2]))
    except ValueError:
        return -1


def _expected_15m_ts_for_date(d: str) -> List[datetime]:
    """返回指定日期的 16 根 15m bar 的 datetime 对象列表"""
    base = datetime.strptime(d, "%Y-%m-%d")
    return [
        base.replace(hour=h, minute=m, tzinfo=TZ_SH)
        for h, m in _ALL_BAR_TIMES
    ]


def _expected_1d_ts_between(d1: str, d2: str) -> List[datetime]:
    """返回 d1 和 d2 之间所有交易日的 datetime 对象列表（午夜）"""
    if d1 >= d2:
        return []
    _build_trading_day_cache()
    result = []
    cur = datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(d2, "%Y-%m-%d")
    while cur < end:
        if cur.strftime("%Y-%m-%d") in _TRADING_DAY_SET:
            result.append(cur.replace(tzinfo=TZ_SH))
        cur += timedelta(days=1)
    return result


def _expected_15m_ts_between_dates(d1: str, d2: str) -> List[datetime]:
    """返回 d1 和 d2 之间所有交易日的 15m bar datetime 对象列表"""
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
    o = bar.get("open") or 0
    h = bar.get("high") or 0
    l = bar.get("low") or 0
    c = bar.get("close") or 0
    v = bar.get("volume") or 0

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
                "start_date": _next_day(dates[i - 1]),
                "end_date": _prev_day(dates[i]),
                "expected_ts": expected_ts,
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
                "start_date": _next_day(last_date),
                "end_date": today,
                "expected_ts": expected_ts,
            })
    return gaps


def check_15m_gaps(symbol: str, records: List[Dict[str, Any]], today: str) -> List[Dict[str, Any]]:
    if len(records) < 2:
        return []
    gaps = []
    # 按 (日期, 时, 分) 排序，忽略秒级精度
    dhm_list = sorted(_ts_to_dhm(r["time"]) for r in records)

    for i in range(1, len(dhm_list)):
        dhm_prev, dhm_curr = dhm_list[i - 1], dhm_list[i]
        d_prev, d_curr = dhm_prev[0], dhm_curr[0]

        if d_prev != d_curr:
            skipped = _trading_days_between(d_prev, d_curr)
            if skipped > 0:
                expected_ts = _expected_15m_ts_between_dates(d_prev, d_curr)
                gaps.append({
                    "symbol": symbol, "timeframe": "15m", "gap_type": "middle",
                    "start_date": _next_day(d_prev),
                    "end_date": _prev_day(d_curr),
                    "expected_ts": expected_ts,
                })
        else:
            idx_prev = _dhm_to_bar_index(dhm_prev)
            idx_curr = _dhm_to_bar_index(dhm_curr)
            if idx_prev < 0 or idx_curr < 0:
                continue
            gap_bars = idx_curr - idx_prev - 1
            if gap_bars > 0:
                all_ts = _expected_15m_ts_for_date(d_prev)
                expected_ts = all_ts[idx_prev + 1: idx_curr]
                gaps.append({
                    "symbol": symbol, "timeframe": "15m", "gap_type": "intraday",
                    "start_date": d_prev, "end_date": d_prev,
                    "expected_ts": expected_ts,
                })

    last_date = dhm_list[-1][0]
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
                "start_date": _next_day(last_date),
                "end_date": today,
                "expected_ts": expected_ts,
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
            "start_date": _ts_to_date(ts),
            "end_date": _ts_to_date(ts),
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
    # fork 后继承父进程的交易日缓存，仅在未构建时才重新拉取
    if _TRADING_DAY_SET is None:
        _build_trading_day_cache(market)

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

ISSUE_TABLE = "data_issue_report"

DDL_ISSUE = f"""
CREATE TABLE IF NOT EXISTS "{ISSUE_TABLE}" (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    timeframe       VARCHAR(10) NOT NULL,
    issue_type      VARCHAR(20) NOT NULL,
    start_date      VARCHAR(10) NOT NULL,
    end_date        VARCHAR(10) NOT NULL,
    expected_ts     JSONB,
    ohlc            JSONB,
    expected_action VARCHAR(20) NOT NULL DEFAULT 'review',
    checked_at      TIMESTAMP   DEFAULT NOW(),
    UNIQUE (symbol, timeframe, issue_type, start_date, end_date)
)
"""


def ensure_tables(pool):
    with pool.cursor() as cur:
        cur.execute(DDL_ISSUE)


def _serialize_expected_ts(ts_list):
    """将 datetime 对象列表序列化为 ISO 字符串列表（JSON 兼容）"""
    if ts_list is None:
        return None
    result = []
    for ts in ts_list:
        if isinstance(ts, datetime):
            result.append(ts.isoformat())
        else:
            result.append(ts)
    return result


def write_issues(pool, gaps: List[Dict[str, Any]], issues: List[Dict[str, Any]],
                  batch_size: int = 500):
    """将 gap 和 quality issues 统一写入 data_issue_report 表"""
    all_records = []

    for g in gaps:
        all_records.append({
            "symbol": g["symbol"],
            "timeframe": g["timeframe"],
            "issue_type": g["gap_type"],  # middle / tail / intraday
            "start_date": g["start_date"],
            "end_date": g["end_date"],
            "expected_ts": _serialize_expected_ts(g.get("expected_ts", [])),
            "ohlc": None,
            "expected_action": "insert",
        })

    for iss in issues:
        all_records.append({
            "symbol": iss["symbol"],
            "timeframe": iss["timeframe"],
            "issue_type": iss["issue_type"],  # bad / suspended / incomplete
            "start_date": iss["start_date"],
            "end_date": iss["end_date"],
            "expected_ts": None,
            "ohlc": iss["ohlc"],
            "expected_action": iss["expected_action"],
        })

    if not all_records:
        return 0

    sql = f"""
        INSERT INTO "{ISSUE_TABLE}"
            (symbol, timeframe, issue_type,
             start_date, end_date, expected_ts, ohlc, expected_action)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        ON CONFLICT (symbol, timeframe, issue_type, start_date, end_date)
        DO UPDATE SET
            expected_ts     = EXCLUDED.expected_ts,
            ohlc            = EXCLUDED.ohlc,
            expected_action = EXCLUDED.expected_action,
            checked_at      = NOW()
    """
    written = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            for start in range(0, len(all_records), batch_size):
                batch = all_records[start:start + batch_size]
                args_list = [
                    (r["symbol"], r["timeframe"], r["issue_type"],
                     r["start_date"], r["end_date"],
                     json.dumps(r["expected_ts"], separators=(",", ":")) if r["expected_ts"] is not None else None,
                     json.dumps(r["ohlc"], separators=(",", ":")) if r["ohlc"] is not None else None,
                     r["expected_action"])
                    for r in batch
                ]
                cur.executemany(sql, args_list)
                written += len(batch)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return written


# 兼容旧代码
def write_gaps(pool, gaps, batch_size=500):
    return write_issues(pool, gaps, [], batch_size)

def write_quality(pool, issues, batch_size=500):
    return write_issues(pool, [], issues, batch_size)


def delete_bad_records(pool, market: str, bad_records: List[Dict[str, Any]]) -> int:
    """
    从 kline 分区表中删除坏数据行（OHLC 全零）。
    按 (timeframe, year) 分组，批量删除。
    """
    if not bad_records:
        return 0

    # 按 (timeframe, year) 分组
    groups: Dict[Tuple[str, int], List[Tuple[str, Any]]] = defaultdict(list)
    for rec in bad_records:
        t = rec["time"]
        if isinstance(t, datetime):
            year = t.year
        else:
            year = datetime.fromtimestamp(t, tz=TZ_SH).year
        groups[(rec["timeframe"], year)].append((rec["symbol"], t))

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
    with pool.connection() as conn:
        conn.execute(f'DELETE FROM "{ISSUE_TABLE}"')


# ---------------------------------------------------------------------------
# 退市股检测 & 清理
# ---------------------------------------------------------------------------

def detect_delisted(
    market: str,
    symbol: str,
    writer,
    today: str,
    threshold_days: int = 90,
) -> bool:
    """
    判断股票是否已退市。

    策略：查询该股票在所有周期表中的最新时间戳，
    如果距今超过 threshold_days 个自然日，视为已退市。

    Args:
        market: 市场标识
        symbol: 股票代码
        writer: MarketKlineWriter 实例
        today: 今日日期字符串 YYYY-MM-DD
        threshold_days: 阈值天数（默认 60 天）

    Returns:
        True 表示已退市
    """
    try:
        # 查询最近的 1D 数据
        r1d = writer.query(market, symbol, "1D", limit=1)
        # 查询最近的 15m 数据
        r15m = writer.query(market, symbol, "15m", limit=1)

        latest_ts = 0
        if r1d:
            latest_ts = max(latest_ts, r1d[-1]["time"])
        if r15m:
            latest_ts = max(latest_ts, r15m[-1]["time"])

        if latest_ts == 0:
            # 完全没有数据，也视为空壳清理
            return True

        last_date = _ts_to_date(latest_ts)
        last_dt = datetime.strptime(last_date, "%Y-%m-%d")
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        gap_days = (today_dt - last_dt).days

        return gap_days > threshold_days
    except Exception:
        return False


def delete_stock_data(
    pool,
    market: str,
    symbol: str,
    timeframes: List[str] = None,
) -> int:
    """
    删除指定股票在所有年份分区表中的全部 K 线数据。

    Args:
        pool: 数据库连接池
        market: 市场标识
        symbol: 股票代码
        timeframes: 要删除的时间周期列表，默认 ["15m", "1D"]

    Returns:
        删除的总行数
    """
    if timeframes is None:
        timeframes = ["15m", "1D"]

    total_deleted = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        try:
            for tf in timeframes:
                # 查找所有该周期的分区表
                cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                      AND table_name LIKE %s
                """, (f'kline_{tf}_%',))
                tables = [r[0] for r in cur.fetchall()]

                for table in tables:
                    # 跳过聚合 view 对应的表名前缀
                    if '_from_' in table:
                        continue
                    cur.execute(
                        f'DELETE FROM "{table}" WHERE symbol = %s',
                        (symbol,)
                    )
                    total_deleted += cur.rowcount

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return total_deleted


# ---------------------------------------------------------------------------
# 1D 缺失补全：从 15m 聚合
# ---------------------------------------------------------------------------

def _aggregate_15m_to_1d(
    records_15m: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    将一天的 15m K 线数据聚合成一根 1D K 线。

    聚合规则：
      - open   = 第一根的 open
      - high   = 所有 high 的最大值
      - low    = 所有 low 的最小值
      - close  = 最后一根的 close
      - volume = 所有 volume 之和

    Args:
        records_15m: 该天所有 15m K 线记录（已按 time 排序）

    Returns:
        聚合后的 1D bar 字典，如果数据不足则返回 None
    """
    if not records_15m or len(records_15m) < 8:
        # 至少需要一半以上的 bar 才算有效
        return None

    sorted_recs = sorted(records_15m, key=lambda r: r["time"])

    open_price = sorted_recs[0]["open"]
    high_price = max(r["high"] for r in sorted_recs)
    low_price = min(r["low"] for r in sorted_recs)
    close_price = sorted_recs[-1]["close"]
    total_volume = sum(r.get("volume", 0) for r in sorted_recs)

    # 如果 OHLC 全为 0，认为数据无效
    if open_price == 0 and high_price == 0 and low_price == 0 and close_price == 0:
        return None

    return {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": total_volume,
    }


def fill_1d_gaps_from_15m(
    market: str,
    symbol: str,
    gaps_1d: List[Dict[str, Any]],
    writer,
    pool,
    dry_run: bool = False,
) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """
    尝试用 15m 数据聚合填补 1D 断裂。

    对于每个 1D gap 中的缺失日期：
      - 查询该日期是否有 15m 数据
      - 如果有，聚合成 1D 写入 kline_1D_YYYY 表
      - 如果 15m 也没有，保留为"双缺失"

    Args:
        market: 市场标识
        symbol: 股票代码
        gaps_1d: 1D 断裂列表（来自 check_1d_gaps）
        writer: MarketKlineWriter 实例
        pool: 数据库连接池
        dry_run: 是否只计算不写库

    Returns:
        (remaining_gaps, filled_count, both_missing)
        - remaining_gaps: 无法填补的 1D gap（去掉已填补的日期后）
        - filled_count: 成功填补的 1D bar 数量
        - both_missing: 15m 和 1D 都缺失的日期列表
    """
    if not gaps_1d:
        return [], 0, []

    # 收集所有 1D gap 中缺失的日期
    all_missing_dates = set()
    for g in gaps_1d:
        for ts in g.get("expected_ts", []):
            all_missing_dates.add(_ts_to_date(ts))

    if not all_missing_dates:
        return [], 0, []

    # 批量查询该股票的全部 15m 数据（一次查询，内存筛选）
    all_15m = writer.query(market, symbol, "15m", limit=0)
    if not all_15m:
        # 完全没有 15m 数据，全部都是双缺失
        both_missing = []
        for d in sorted(all_missing_dates):
            both_missing.append({
                "symbol": symbol,
                "date": d,
                "reason": "15m_data_not_found",
            })
        return gaps_1d, 0, both_missing

    # 按日期分组 15m 数据
    ts_to_date_cache: Dict[int, str] = {}
    for rec in all_15m:
        ts_to_date_cache[rec["time"]] = _ts_to_date(rec["time"])

    date_to_15m: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in all_15m:
        d = ts_to_date_cache[rec["time"]]
        if d in all_missing_dates:
            date_to_15m[d].append(rec)

    # 逐日判断
    filled_dates = set()
    both_missing = []

    # 预确保所有可能用到的 1D 年份表存在（避免在 cursor 上下文内嵌套取连接）
    if not dry_run:
        years_needed = {datetime.strptime(d, "%Y-%m-%d").year for d in all_missing_dates}
        for y in years_needed:
            writer._mgr.ensure_year_table(market, "1D", y)

    for d in sorted(all_missing_dates):
        recs_15m = date_to_15m.get(d, [])
        if len(recs_15m) >= 8:
            # 15m 数据充足，可以聚合
            agg_bar = _aggregate_15m_to_1d(recs_15m)
            if agg_bar is not None:
                if not dry_run:
                    year = datetime.strptime(d, "%Y-%m-%d").year
                    table = f"kline_1D_{year}"
                    ts_1d = _date_to_ts(d)  # 返回 datetime 对象
                    try:
                        with pool.cursor() as cur:
                            cur.execute(f"""
                                INSERT INTO "{table}"
                                    (symbol, time, open, high, low, close, volume)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (symbol, time) DO UPDATE SET
                                    open       = EXCLUDED.open,
                                    high       = EXCLUDED.high,
                                    low        = EXCLUDED.low,
                                    close      = EXCLUDED.close,
                                    volume     = EXCLUDED.volume
                            """, (
                                symbol,
                                ts_1d.replace(tzinfo=None),  # 去掉时区，避免 psycopg2 隐式转换
                                agg_bar["open"], agg_bar["high"],
                                agg_bar["low"], agg_bar["close"],
                                agg_bar["volume"],
                            ))
                    except Exception as e:
                        print(f"⚠️  写入聚合 1D 失败: {symbol} {d}: {e}")
                        both_missing.append({
                            "symbol": symbol,
                            "date": d,
                            "reason": f"write_failed: {e}",
                        })
                        continue

                filled_dates.add(d)
            else:
                # 15m 数据存在但 OHLC 全零
                both_missing.append({
                    "symbol": symbol,
                    "date": d,
                    "reason": "15m_data_all_zero",
                })
        elif recs_15m:
            # 有部分 15m 数据但不足 8 根
            both_missing.append({
                "symbol": symbol,
                "date": d,
                "reason": f"15m_partial({len(recs_15m)}/16)",
            })
        else:
            # 完全没有 15m 数据
            both_missing.append({
                "symbol": symbol,
                "date": d,
                "reason": "15m_data_not_found",
            })

    # 重建 remaining_gaps：只保留未填补的日期
    remaining_gaps = []
    for g in gaps_1d:
        new_expected = []
        for ts in g.get("expected_ts", []):
            d = _ts_to_date(ts)
            if d not in filled_dates:
                new_expected.append(ts)
        if new_expected:
            remaining_gaps.append({
                **g,
                "expected_ts": new_expected,
            })

    return remaining_gaps, len(filled_dates), both_missing


def export_csv(gaps, issues, path):
    if not gaps and not issues:
        print("无数据，跳过 CSV")
        return

    all_rows = []
    for g in gaps:
        all_rows.append({
            "symbol": g["symbol"], "timeframe": g["timeframe"],
            "issue_type": g["gap_type"],
            "start_date": g["start_date"], "end_date": g["end_date"],
            "expected_ts": json.dumps(
                _serialize_expected_ts(g.get("expected_ts", [])),
                separators=(",", ":")),
            "ohlc": "",
            "expected_action": "insert",
        })
    for iss in issues:
        all_rows.append({
            "symbol": iss["symbol"], "timeframe": iss["timeframe"],
            "issue_type": iss["issue_type"],
            "start_date": iss["start_date"], "end_date": iss["end_date"],
            "expected_ts": "",
            "ohlc": json.dumps(iss["ohlc"], separators=(",", ":")),
            "expected_action": iss["expected_action"],
        })

    fields = ["symbol", "timeframe", "issue_type", "start_date", "end_date",
              "expected_ts", "ohlc", "expected_action"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    print(f"✅ CSV: {path}（{len(all_rows)} 条）")


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
    parser.add_argument("--delist-threshold", type=int, default=60,
                        help="退市判定阈值：最后数据距今超过该天数视为退市（默认 60 天）")
    parser.add_argument("--clean-delist", action="store_true",
                        help="清理退市股票的 15m 和 1D 数据")
    parser.add_argument("--fill-1d", action="store_true",
                        help="用 15m 数据聚合填补 1D 缺失")
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

    if not mgr.market_db_exists(market):
        print(f"❌ {market}_db 不存在")
        return 1

    stats = writer.stats(market)
    all_syms = stats.get("symbol_list", [])
    if not all_syms:
        print(f"❌ 无股票数据")
        return 1

    # 以全库最新数据日期为基准（而非今天），避免尾部延迟被误报为断裂
    date_range = stats.get("date_range", {})
    raw_end = date_range.get("end")
    if raw_end:
        ref_dt = datetime.fromisoformat(raw_end)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        today = ref_dt.astimezone(TZ_SH).strftime("%Y-%m-%d")
    else:
        today = datetime.now(TZ_SH).strftime("%Y-%m-%d")
    print(f"📅 数据基准日期: {today}（全库最新数据日）")

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
    if args.clean_delist:
        print(f"   退市清理: 开启（阈值 {args.delist_threshold} 天）")
    if args.fill_1d:
        print(f"   1D 补全: 开启（从 15m 聚合）")
    print()

    _build_trading_day_cache(market)
    assert _TRADING_DAY_SET is not None, "交易日缓存必须在 fork 前构建完成"

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

        all_gaps.sort(key=lambda g: len(g.get("expected_ts", [])), reverse=True)
        print(f"\n断裂最严重的 10 条:")
        for g in all_gaps[:10]:
            print(f"  {g['symbol']:>8} | {g['timeframe']:>3} | {g['gap_type']:>8} | "
                  f"{g['start_date']}~{g['end_date']} | 缺 {len(g.get('expected_ts', []))} 根")

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
                      f"{iss['start_date']} | ohlc={iss['ohlc']}")

    # ---- 退市股检测 & 清理 ----
    delisted_syms: List[str] = []
    delisted_deleted = 0
    if args.clean_delist and not _INTERRUPTED:
        print(f"\n{'='*60}")
        print(f"🔍 检测退市股（阈值 {args.delist_threshold} 天）...")
        try:
            pool_db = mgr._get_pool(market)
            for sym in syms:
                if _INTERRUPTED:
                    break
                if detect_delisted(market, sym, writer, today, args.delist_threshold):
                    delisted_syms.append(sym)
                    if not args.dry_run:
                        n = delete_stock_data(pool_db, market, sym, ["15m", "1D"])
                        delisted_deleted += n

            print(f"  检测到退市股: {len(delisted_syms)} 只")
            if delisted_deleted:
                print(f"  已删除退市股数据: {delisted_deleted} 行")
            if delisted_syms and len(delisted_syms) <= 20:
                for s in delisted_syms:
                    print(f"    - {s}")
            elif delisted_syms:
                for s in delisted_syms[:10]:
                    print(f"    - {s}")
                print(f"    ... 还有 {len(delisted_syms) - 10} 只")
        except Exception as e:
            print(f"❌ 退市检测失败: {e}")
            traceback.print_exc()

    # ---- 1D 缺失补全：从 15m 聚合 ----
    filled_1d_count = 0
    both_missing_records: List[Dict[str, Any]] = []
    if args.fill_1d and not _INTERRUPTED:
        print(f"\n{'='*60}")
        print(f"🔄 用 15m 数据聚合填补 1D 缺失...")
        try:
            pool_db = mgr._get_pool(market)
            # 按 symbol 分组 1D gaps
            gaps_by_sym: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for g in all_gaps:
                if g["timeframe"] == "1D":
                    gaps_by_sym[g["symbol"]].append(g)

            remaining_1d_gaps: List[Dict[str, Any]] = []
            syms_to_fill = [s for s in gaps_by_sym if s not in set(delisted_syms)]

            for idx, sym in enumerate(syms_to_fill):
                if _INTERRUPTED:
                    break
                sym_gaps = gaps_by_sym[sym]
                remaining, filled, missing = fill_1d_gaps_from_15m(
                    market, sym, sym_gaps, writer, pool_db, args.dry_run
                )
                remaining_1d_gaps.extend(remaining)
                filled_1d_count += filled
                both_missing_records.extend(missing)

                if (idx + 1) % 200 == 0 or (idx + 1) == len(syms_to_fill):
                    print(f"  [{idx + 1}/{len(syms_to_fill)}] "
                          f"已补全={filled_1d_count} 双缺失={len(both_missing_records)}")

            # 从 all_gaps 中移除已全部填补的 1D gap
            if not args.dry_run:
                all_gaps = [g for g in all_gaps if g["timeframe"] != "1D"] + remaining_1d_gaps

            print(f"  补全结果: 填充 {filled_1d_count} 根 1D bar")
            print(f"  双缺失（15m+1D 都没有）: {len(both_missing_records)} 天")
        except Exception as e:
            print(f"❌ 1D 补全失败: {e}")
            traceback.print_exc()

    # ---- 双缺失批量写入问题表 ----
    if both_missing_records and not args.dry_run:
        try:
            pool_db = mgr._get_pool(market)
            ensure_tables(pool_db)
            # 将双缺失转为 issue 格式，一次性批量写入
            both_missing_issues = []
            for rec in both_missing_records:
                both_missing_issues.append({
                    "symbol": rec["symbol"],
                    "timeframe": "1D",
                    "issue_type": "both_missing",
                    "start_date": rec["date"],
                    "end_date": rec["date"],
                    "expected_ts": None,
                    "ohlc": None,
                    "expected_action": "fetch",
                })
            n = write_issues(pool_db, [], both_missing_issues)
            print(f"✅ 双缺失批量写入 {ISSUE_TABLE}: {n} 条")
        except Exception as e:
            print(f"❌ 双缺失写入失败: {e}")
            traceback.print_exc()

    # ---- 写库（原有逻辑） ----
    if not args.dry_run:
        try:
            pool_db = mgr._get_pool(market)
            ensure_tables(pool_db)

            if args.clear:
                clear_tables(pool_db)
                print(f"\n🗑️  已清空旧报告数据")

            if all_gaps or all_issues:
                n = write_issues(pool_db, all_gaps, all_issues)
                print(f"✅ 写入 {ISSUE_TABLE}: {n} 条")

            # 删除坏数据
            if args.fix and all_bad:
                deleted = delete_bad_records(pool_db, market, all_bad)
                print(f"🗑️  已删除坏数据行: {deleted} 条")

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
