#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# bat_in_kline.py — BaoStock 前复权日线 vs DB 数据比对 & 修正 & 增量更新
# ============================================================================
#
# 功能:
#   1. 加载 BaoStock 前复权日线 CSV（optimizer_output/CNStock/daily/）
#   2. 与 db_market 中的 1D 数据逐 bar 比对
#   3. 识别: 缺失日期 / 价格偏差 / OHLC 质量问题
#   4. 用 BaoStock 前复权数据修正 db 中的错误和缺失
#   5. 15m 质量检查 — 从 DB 批量读取，检查坏数据/停牌/逻辑矛盾
#   6. 增量更新 — 读取 last_update 表，从上次更新时间强制全量刷新（删除后重写）
#
# 运行时间限制:
#   - 交易日: 17:00 以后 或 08:30 以前
#   - 非交易日: 全天可运行
#   - 使用 trading_calendar.py 判断交易日
#
# BaoStock CSV 格式（tdx_download.py 产出，adjustflag="1" 前复权）:
#   date,open,close,high,low,volume,amount
#
# DB 表结构（kline_1D_YYYY / kline_15m_YYYY）:
#   symbol, time(TIMESTAMP), open, high, low, close, volume
#
# 用法:
#   python bat_in_kline.py                          # 全量比对 1D（原有功能）
#   python bat_in_kline.py -T 15m                   # 15m DB 质量检查
#   python bat_in_kline.py -T all                   # 1D 比对 + 15m 质量检查
#   python bat_in_kline.py --symbol 600519           # 单只股票
#   python bat_in_kline.py --dry-run                 # 只报告不修正
#   python bat_in_kline.py --fix                     # 比对 + 修正
#   python bat_in_kline.py --csv report.csv          # 导出报告
#   python bat_in_kline.py --tolerance 0.02          # 价格容差 2%
#   python bat_in_kline.py --csv-dir /path/to/csv    # 指定 CSV 目录
#   python bat_in_kline.py --update                  # 增量/全量更新（含时间窗口检查）
#   python bat_in_kline.py --update --force-full     # 强制全量更新（忽略时间窗口）
#
# 依赖:
#   - db_market.py / db_multi.py（backend_api_python/app/utils/）
#   - trading_calendar.py（backend_api_python/app/utils/）
#   - DataSourceFactory（backend_api_python/app/data_sources/，15m 更新时使用）
#
# 创建时间: 2026-05-04
# 修改时间: 2026-05-06 — 增加增量更新 & 时间窗口控制
# ============================================================================

from __future__ import annotations

import os
import sys
import csv
import json
import math
import signal
import logging
import argparse
import traceback
import multiprocessing as mp
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径 & 环境
# ---------------------------------------------------------------------------

def _find_project_root() -> str:
    """向上查找包含 backend_api_python/ 的项目根目录"""
    # 从本文件位置向上搜索（最多 5 层）
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if os.path.isdir(os.path.join(d, "backend_api_python")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # fallback: CWD
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "backend_api_python")):
        return cwd
    # 最后尝试 CWD 的父目录
    parent_cwd = os.path.dirname(cwd)
    if os.path.isdir(os.path.join(parent_cwd, "backend_api_python")):
        return parent_cwd
    return cwd

PROJECT_ROOT = _find_project_root()
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend_api_python"))

# NOTE: 不要把本文件所在目录 (app/utils/) 加入 sys.path，
# 否则 http.py 会遮蔽标准库 http 模块，导致 Flask/werkzeug 导入崩溃。


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
# 时间常量
# ---------------------------------------------------------------------------

TZ_SH = timezone(timedelta(hours=8))

# 交易日历缓存（模块级，子进程 fork 后自动为 None，会重新构建）
_TRADING_DAY_SET: frozenset[str] | None = None
_TRADING_DAY_SILENT: bool = False  # 子进程构建时不打印


def _build_trading_day_cache(market: str = "CNStock", silent: bool = False):
    global _TRADING_DAY_SET, _TRADING_DAY_SILENT
    if _TRADING_DAY_SET is not None:
        return
    from app.utils.trading_calendar import trade_date_range
    end_year = datetime.now(TZ_SH).year + 1
    dates = trade_date_range("2015-01-01", f"{end_year}-12-31")
    _TRADING_DAY_SET = frozenset(dates)
    if not silent:
        print(f"📅 交易日历: {len(_TRADING_DAY_SET)} 天")


def _is_trading_day(d: str) -> bool:
    if _TRADING_DAY_SET is None:
        _build_trading_day_cache(silent=_TRADING_DAY_SILENT)
    return d in _TRADING_DAY_SET


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def _ts_to_date(ts) -> str:
    """从 datetime / unix timestamp / ISO 字符串提取 YYYY-MM-DD"""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
        return dt.strftime("%Y-%m-%d")
    if isinstance(ts, str):
        return ts[:10]
    return datetime.fromtimestamp(ts, tz=TZ_SH).strftime("%Y-%m-%d")


def _date_to_ts_midnight(d: str) -> datetime:
    return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH)


def _safe_float(v, default: float = 0.0) -> float:
    """安全转 float，处理 None / nan / inf / 空字符串"""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# BaoStock CSV 加载
# ---------------------------------------------------------------------------

def load_baostock_csv(csv_dir: str, code: str) -> List[Dict[str, Any]]:
    """
    加载单只股票的 BaoStock CSV 文件。

    CSV 格式: date,open,close,high,low,volume,amount
    注意: BaoStock 的列顺序是 open,close,high,low（不是 open,high,low,close）

    Returns:
        [{"date": str, "open": float, "high": float, "low": float,
          "close": float, "volume": float}, ...]
    """
    path = os.path.join(csv_dir, f"{code}.csv")
    if not os.path.isfile(path):
        return []

    records = []
    try:
        for enc in ("utf-8-sig", "gbk"):
            try:
                with open(path, "r", encoding=enc) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            d = row.get("date", "").strip()
                            if not d or len(d) < 10:
                                continue
                            o = _safe_float(row.get("open"))
                            c = _safe_float(row.get("close"))
                            h = _safe_float(row.get("high"))
                            lo = _safe_float(row.get("low"))
                            v = _safe_float(row.get("volume"))
                            if o == 0 and c == 0:
                                continue
                            records.append({
                                "date": d[:10],
                                "open": o, "high": h, "low": lo, "close": c,
                                "volume": v,
                            })
                        except (ValueError, KeyError):
                            continue
                break
            except UnicodeDecodeError:
                continue
    except Exception as e:
        logger.warning(f"加载 CSV 失败 {code}: {e}")

    return records


def list_csv_codes(csv_dir: str) -> Set[str]:
    """列出 CSV 目录下所有股票代码"""
    codes = set()
    if not os.path.isdir(csv_dir):
        return codes
    for fname in os.listdir(csv_dir):
        if fname.endswith(".csv") and not fname.startswith("_"):
            codes.add(fname[:-4])
    return codes


# ---------------------------------------------------------------------------
# DB 数据加载
# ---------------------------------------------------------------------------

def load_db_data(writer, market: str, code: str, timeframe: str) -> List[Dict[str, Any]]:
    """从 db_market 加载全部 K 线数据"""
    try:
        records = writer.query(market, code, timeframe, limit=0)
        return records or []
    except Exception as e:
        logger.warning(f"DB 查询失败 {code}/{timeframe}: {e}")
        return []


def get_db_symbols(writer, market: str) -> Set[str]:
    """从 DB 获取指定市场所有 symbol（只查 kline_1D_* 表，比 stats() 快）"""
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool(market)
        with pool.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'kline_1D_%'
                  AND table_name NOT LIKE '%%_from_%%'
            """)
            tables = [r[0] for r in cur.fetchall()]
        if not tables:
            return set()
        syms: Set[str] = set()
        with pool.cursor() as cur:
            for tbl in tables:
                try:
                    cur.execute(f'SELECT DISTINCT symbol FROM "{tbl}"')
                    for r in cur.fetchall():
                        syms.add(r[0])
                except Exception:
                    pass
        return syms
    except Exception as e:
        logger.warning(f"获取 DB symbol 列表失败: {e}")
        return set()


# ---------------------------------------------------------------------------
# 价格偏差检测
# ---------------------------------------------------------------------------

def _price_diff_pct(ref: float, db: float) -> float:
    """计算价格偏差百分比（相对参考值）"""
    if ref == 0:
        return 0.0 if db == 0 else 999.0
    return abs(db - ref) / ref


def classify_bar_quality(bar: Dict[str, Any]) -> str:
    """
    对单根 K 线做质量分类。

    Returns: "ok" / "bad" / "suspended" / "incomplete"
    """
    o = _safe_float(bar.get("open"))
    h = _safe_float(bar.get("high"))
    lo = _safe_float(bar.get("low"))
    c = _safe_float(bar.get("close"))
    v = _safe_float(bar.get("volume"))

    if o == 0 and h == 0 and lo == 0 and c == 0:
        return "bad"
    if v == 0 and o == h == lo == c and o > 0:
        return "suspended"
    if h > 0 and lo > 0 and (h < lo or (o > 0 and (o > h or o < lo)) or (c > 0 and (c > h or c < lo))):
        return "incomplete"
    if v == 0 and not (o == h == lo == c):
        return "incomplete"
    if (o == 0) != (h == 0) or (h == 0) != (lo == 0) or (lo == 0) != (c == 0):
        return "incomplete"
    return "ok"


# ---------------------------------------------------------------------------
# 1D 比对核心
# ---------------------------------------------------------------------------

def _calc_return(prev_close: float, curr_close: float) -> float:
    """计算日涨跌幅"""
    if prev_close == 0:
        return 0.0
    return (curr_close - prev_close) / prev_close


def compare_1d(
    code: str,
    csv_records: List[Dict[str, Any]],
    db_records: List[Dict[str, Any]],
    tolerance: float = 0.01,
) -> Dict[str, Any]:
    """
    比对 BaoStock CSV（前复权）与 DB 中的 1D 数据。

    复权处理策略:
      - 绝对价格偏差大 但 涨跌幅一致 → 复权差异（不修正，标记为 qfq_diff）
      - 涨跌幅也偏差 → 真数据错误（修正）

    Returns:
        {
            "code": str,
            "csv_count": int, "db_count": int,
            "missing_in_db": [str],
            "extra_in_db": [str],
            "price_mismatch": [...],     # 真错误（涨跌幅也偏差）
            "qfq_diff": [...],           # 复权差异（涨跌幅一致）
            "quality_issues": [...],
            "fix_records": [...],
        }
    """
    result = {
        "code": code,
        "csv_count": len(csv_records),
        "db_count": len(db_records),
        "missing_in_db": [],
        "extra_in_db": [],
        "price_mismatch": [],
        "qfq_diff": [],
        "quality_issues": [],
        "fix_records": [],
    }

    # 构建日期索引
    csv_by_date: Dict[str, Dict] = {}
    for r in csv_records:
        d = r["date"][:10]
        csv_by_date[d] = r

    db_by_date: Dict[str, Dict] = {}
    for r in db_records:
        d = _ts_to_date(r["time"])
        db_by_date[d] = r

    csv_dates = set(csv_by_date.keys())
    db_dates = set(db_by_date.keys())

    # 1. CSV 有但 DB 没有 → 缺失（只报告交易日）
    missing = sorted(csv_dates - db_dates)
    result["missing_in_db"] = [d for d in missing if _is_trading_day(d)]

    # 2. DB 有但 CSV 没有 → 多余（仅记录）
    result["extra_in_db"] = sorted(db_dates - csv_dates)

    # 3. 两边都有 → 比对
    #    先按日期排序，用于计算涨跌幅
    common_dates = sorted(csv_dates & db_dates)

    for i, d in enumerate(common_dates):
        csv_r = csv_by_date[d]
        db_r = db_by_date[d]

        # 3a. 质量检查（不受复权影响）
        quality = classify_bar_quality(db_r)
        if quality != "ok":
            result["quality_issues"].append({
                "date": d,
                "quality": quality,
                "db_ohlc": {
                    "open": _safe_float(db_r.get("open")),
                    "high": _safe_float(db_r.get("high")),
                    "low": _safe_float(db_r.get("low")),
                    "close": _safe_float(db_r.get("close")),
                    "volume": _safe_float(db_r.get("volume")),
                },
            })

        # 3b. 价格比对（区分复权差异 vs 真错误）
        csv_close = csv_r["close"]
        db_close = _safe_float(db_r.get("close"))
        price_diff = _price_diff_pct(csv_close, db_close)

        is_real_error = False
        reason = ""

        if quality != "ok":
            # 质量问题直接标记
            is_real_error = True
            reason = quality
        elif price_diff > tolerance:
            # 价格偏差大 → 检查涨跌幅是否一致
            if i > 0:
                prev_csv = csv_by_date[common_dates[i - 1]]["close"]
                prev_db = _safe_float(db_by_date[common_dates[i - 1]].get("close"))
                csv_ret = _calc_return(prev_csv, csv_close)
                db_ret = _calc_return(prev_db, db_close)
                ret_diff = abs(csv_ret - db_ret)

                if ret_diff > 0.005:
                    # 涨跌幅偏差 > 0.5% → 真数据错误
                    is_real_error = True
                    reason = "price_mismatch"
                else:
                    # 涨跌幅一致 → 复权差异
                    result["qfq_diff"].append({
                        "date": d,
                        "price_diff_pct": round(price_diff * 100, 2),
                        "csv_close": csv_close,
                        "db_close": db_close,
                    })
            else:
                # 第一天无法算涨跌幅，用 volume 做辅助判断
                csv_vol = csv_r.get("volume", 0)
                db_vol = _safe_float(db_r.get("volume"))
                vol_diff = _price_diff_pct(csv_vol, db_vol) if csv_vol > 0 else 0

                if price_diff > 0.5 and vol_diff < 0.1:
                    # 价格差 >50% 但 volume 一致 → 大概率复权差异
                    result["qfq_diff"].append({
                        "date": d,
                        "price_diff_pct": round(price_diff * 100, 2),
                        "csv_close": csv_close,
                        "db_close": db_close,
                    })
                else:
                    # 无法确定，保守标记为真错误
                    is_real_error = True
                    reason = "price_mismatch"

        if is_real_error:
            result["price_mismatch"].append({
                "date": d,
                "csv": {
                    "open": csv_r["open"], "high": csv_r["high"],
                    "low": csv_r["low"], "close": csv_r["close"],
                    "volume": csv_r["volume"],
                },
                "db": {
                    "open": _safe_float(db_r.get("open")),
                    "high": _safe_float(db_r.get("high")),
                    "low": _safe_float(db_r.get("low")),
                    "close": _safe_float(db_r.get("close")),
                    "volume": _safe_float(db_r.get("volume")),
                },
                "diff_pct": round(price_diff * 100, 2),
            })
            # 用前复权数据修正
            result["fix_records"].append({
                "date": d,
                "open": csv_r["open"], "high": csv_r["high"],
                "low": csv_r["low"], "close": csv_r["close"],
                "volume": csv_r["volume"],
                "reason": reason,
            })

    # 4. 缺失日期生成 fix_records
    for d in result["missing_in_db"]:
        csv_r = csv_by_date[d]
        result["fix_records"].append({
            "date": d,
            "open": csv_r["open"], "high": csv_r["high"],
            "low": csv_r["low"], "close": csv_r["close"],
            "volume": csv_r["volume"],
            "reason": "missing",
        })

    return result


# ---------------------------------------------------------------------------
# 15m 比对核心
# ---------------------------------------------------------------------------

def compare_15m(
    code: str,
    db_records: List[Dict[str, Any]],
    factory_bars: List[Dict[str, Any]],
    tolerance: float = 0.01,
) -> Dict[str, Any]:
    """
    比对 DataSourceFactory 的 15m 前复权数据与 DB 中的 15m 数据。

    Args:
        code: 股票代码
        db_records: DB 中的 15m 数据
        factory_bars: DataSourceFactory 返回的 15m 数据（time 为 unix timestamp 或 datetime）
        tolerance: 价格容差

    Returns:
        与 compare_1d 相同结构
    """
    result = {
        "code": code,
        "csv_count": len(factory_bars),
        "db_count": len(db_records),
        "missing_in_db": [],
        "extra_in_db": [],
        "price_mismatch": [],
        "quality_issues": [],
        "fix_records": [],
    }

    def _bar_key(ts) -> Tuple[str, int, int]:
        if isinstance(ts, datetime):
            dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
        elif isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=TZ_SH)
        elif isinstance(ts, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                         "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                dt = datetime.fromtimestamp(0, tz=TZ_SH)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_SH)
        else:
            dt = datetime.fromtimestamp(0, tz=TZ_SH)
        return (dt.strftime("%Y-%m-%d"), dt.hour, dt.minute)

    factory_by_key: Dict[Tuple[str, int, int], Dict] = {}
    for b in factory_bars:
        key = _bar_key(b["time"])
        factory_by_key[key] = b

    db_by_key: Dict[Tuple[str, int, int], Dict] = {}
    for r in db_records:
        key = _bar_key(r["time"])
        db_by_key[key] = r

    factory_keys = set(factory_by_key.keys())
    db_keys = set(db_by_key.keys())

    # 1. Factory 有但 DB 没有 → 缺失
    for key in sorted(factory_keys - db_keys):
        d, h, m = key
        if _is_trading_day(d):
            b = factory_by_key[key]
            result["missing_in_db"].append(f"{d} {h:02d}:{m:02d}")
            result["fix_records"].append({
                "date": d, "hour": h, "minute": m,
                "open": _safe_float(b.get("open")),
                "high": _safe_float(b.get("high")),
                "low": _safe_float(b.get("low")),
                "close": _safe_float(b.get("close")),
                "volume": _safe_float(b.get("volume")),
                "reason": "missing",
            })

    # 2. DB 有但 Factory 没有 → 多余
    for key in sorted(db_keys - factory_keys):
        d, h, m = key
        result["extra_in_db"].append(f"{d} {h:02d}:{m:02d}")

    # 3. 两边都有 → 比对
    for key in sorted(factory_keys & db_keys):
        f_b = factory_by_key[key]
        db_r = db_by_key[key]
        d, h, m = key

        quality = classify_bar_quality(db_r)
        if quality != "ok":
            result["quality_issues"].append({
                "date": f"{d} {h:02d}:{m:02d}",
                "quality": quality,
                "db_ohlc": {
                    "open": _safe_float(db_r.get("open")),
                    "high": _safe_float(db_r.get("high")),
                    "low": _safe_float(db_r.get("low")),
                    "close": _safe_float(db_r.get("close")),
                    "volume": _safe_float(db_r.get("volume")),
                },
            })

        f_close = _safe_float(f_b.get("close"))
        db_close = _safe_float(db_r.get("close"))
        diff = _price_diff_pct(f_close, db_close)

        if diff > tolerance:
            result["price_mismatch"].append({
                "date": f"{d} {h:02d}:{m:02d}",
                "csv": {
                    "open": _safe_float(f_b.get("open")),
                    "high": _safe_float(f_b.get("high")),
                    "low": _safe_float(f_b.get("low")),
                    "close": _safe_float(f_b.get("close")),
                    "volume": _safe_float(f_b.get("volume")),
                },
                "db": {
                    "open": _safe_float(db_r.get("open")),
                    "high": _safe_float(db_r.get("high")),
                    "low": _safe_float(db_r.get("low")),
                    "close": _safe_float(db_r.get("close")),
                    "volume": _safe_float(db_r.get("volume")),
                },
                "diff_pct": round(diff * 100, 2),
            })

        if quality != "ok" or diff > tolerance:
            result["fix_records"].append({
                "date": d, "hour": h, "minute": m,
                "open": _safe_float(f_b.get("open")),
                "high": _safe_float(f_b.get("high")),
                "low": _safe_float(f_b.get("low")),
                "close": _safe_float(f_b.get("close")),
                "volume": _safe_float(f_b.get("volume")),
                "reason": quality if quality != "ok" else "price_mismatch",
            })

    return result


# ---------------------------------------------------------------------------
# 15m 数据获取（按需，仅用于有差异的股票补校验）
# ---------------------------------------------------------------------------

def fetch_15m_from_factory(code: str, market: str = "CNStock") -> List[Dict[str, Any]]:
    """
    通过 DataSourceFactory 获取 15m 前复权数据（单只，按需调用）。

    Returns:
        [{"time": unix_timestamp, "open": float, ...}, ...]
    """
    try:
        from app.data_sources.factory import DataSourceFactory
        source = DataSourceFactory.get_source(market)
        bars = source.get_kline(code, "15m", 5000)
        return bars or []
    except Exception as e:
        logger.warning(f"[Factory] 获取 15m 失败 {code}: {e}")
        return []


# ---------------------------------------------------------------------------
# DB 写入修正
# ---------------------------------------------------------------------------

def write_fixes_1d(
    writer,
    market: str,
    code: str,
    fix_records: List[Dict[str, Any]],
    dry_run: bool = False,
) -> int:
    """将 1D 修正记录写入 DB（UPSERT）。返回写入数。"""
    if not fix_records:
        return 0
    if dry_run:
        return len(fix_records)

    records = []
    for fr in fix_records:
        dt = _date_to_ts_midnight(fr["date"])
        records.append({
            "symbol": code,
            "timeframe": "1D",
            "time": dt,
            "open": _safe_float(fr.get("open")),
            "high": _safe_float(fr.get("high")),
            "low": _safe_float(fr.get("low")),
            "close": _safe_float(fr.get("close")),
            "volume": _safe_float(fr.get("volume")),
        })

    try:
        result = writer.bulk_write(market, records, batch_size=5000)
        return result.get("inserted", 0)
    except Exception as e:
        logger.warning(f"写入 1D 修正失败 {code}: {e}")
        return 0


def write_fixes_15m(
    writer,
    market: str,
    code: str,
    fix_records: List[Dict[str, Any]],
    dry_run: bool = False,
) -> int:
    """将 15m 修正记录写入 DB（UPSERT）。返回写入数。"""
    if not fix_records:
        return 0
    if dry_run:
        return len(fix_records)

    records = []
    for fr in fix_records:
        dt = datetime.strptime(fr["date"], "%Y-%m-%d").replace(
            hour=fr["hour"], minute=fr["minute"], tzinfo=TZ_SH
        )
        records.append({
            "symbol": code,
            "timeframe": "15m",
            "time": dt,
            "open": _safe_float(fr.get("open")),
            "high": _safe_float(fr.get("high")),
            "low": _safe_float(fr.get("low")),
            "close": _safe_float(fr.get("close")),
            "volume": _safe_float(fr.get("volume")),
        })

    try:
        result = writer.bulk_write(market, records, batch_size=5000)
        return result.get("inserted", 0)
    except Exception as e:
        logger.warning(f"写入 15m 修正失败 {code}: {e}")
        return 0


# ---------------------------------------------------------------------------
# 子进程工作函数
# ---------------------------------------------------------------------------

def _worker_compare_1d(args: Tuple) -> Dict[str, Any]:
    """子进程：比对一批股票的 1D 数据（纯 CSV vs DB，无网络请求）"""
    codes, market, csv_dir, tolerance = args

    # fork 后重置全局单例
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

    global _TRADING_DAY_SILENT
    _TRADING_DAY_SILENT = True
    if _TRADING_DAY_SET is None:
        _build_trading_day_cache(market, silent=True)

    results = []
    errors = []
    stats = {"total": 0, "has_diff": 0, "missing": 0,
             "price_mismatch": 0, "quality_issues": 0, "qfq_diff": 0}

    for code in codes:
        stats["total"] += 1
        try:
            csv_data = load_baostock_csv(csv_dir, code)
            db_data = load_db_data(writer, market, code, "1D")
            if not csv_data and not db_data:
                continue

            cmp = compare_1d(code, csv_data, db_data, tolerance)
            results.append({"timeframe": "1D", **cmp})

            if cmp["missing_in_db"]:
                stats["missing"] += len(cmp["missing_in_db"])
            if cmp["price_mismatch"]:
                stats["price_mismatch"] += len(cmp["price_mismatch"])
            if cmp["quality_issues"]:
                stats["quality_issues"] += len(cmp["quality_issues"])
            if cmp.get("qfq_diff"):
                stats["qfq_diff"] += len(cmp["qfq_diff"])
            if cmp["fix_records"]:
                stats["has_diff"] += 1
        except Exception as e:
            errors.append((code, f"{type(e).__name__}: {e}"))

    try:
        _dbm._manager.close_all_pools()
    except Exception:
        pass

    return {"results": results, "stats": stats, "errors": errors}


def _worker_check_15m_quality(args: Tuple) -> Dict[str, Any]:
    """
    子进程：检查一批股票的 15m DB 数据质量（纯本地，无网络请求）。

    只做质量检查（bad/suspended/incomplete），不做价格比对（无本地参考数据）。
    """
    codes, market = args

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

    results = []
    errors = []
    stats = {"total": 0, "has_diff": 0, "missing": 0,
             "price_mismatch": 0, "quality_issues": 0}

    for code in codes:
        stats["total"] += 1
        try:
            db_15m = load_db_data(writer, market, code, "15m")
            if not db_15m:
                continue

            quality_issues = []
            for r in db_15m:
                q = classify_bar_quality(r)
                if q != "ok":
                    dt = _ts_to_date(r["time"])
                    quality_issues.append({
                        "date": dt,
                        "quality": q,
                        "db_ohlc": {
                            "open": _safe_float(r.get("open")),
                            "high": _safe_float(r.get("high")),
                            "low": _safe_float(r.get("low")),
                            "close": _safe_float(r.get("close")),
                            "volume": _safe_float(r.get("volume")),
                        },
                    })

            cmp_result = {
                "timeframe": "15m",
                "code": code,
                "csv_count": 0,
                "db_count": len(db_15m),
                "missing_in_db": [],
                "extra_in_db": [],
                "price_mismatch": [],
                "quality_issues": quality_issues,
                "fix_records": [],
            }

            if quality_issues:
                stats["quality_issues"] += len(quality_issues)
                stats["has_diff"] += 1
            results.append(cmp_result)

        except Exception as e:
            errors.append((code, f"{type(e).__name__}: {e}"))

    try:
        _dbm._manager.close_all_pools()
    except Exception:
        pass

    return {"results": results, "stats": stats, "errors": errors}


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
    print("\n⚠️  收到中断信号，正在保存已比对的结果...")


# ---------------------------------------------------------------------------
# CSV 报告导出
# ---------------------------------------------------------------------------

def export_csv(all_results: List[Dict[str, Any]], path: str):
    """导出比对报告到 CSV"""
    rows = []
    for r in all_results:
        code = r["code"]
        tf = r.get("timeframe", "1D")

        for m in r.get("missing_in_db", []):
            rows.append({
                "code": code, "timeframe": tf, "issue": "missing_in_db",
                "date": m, "detail": "", "diff_pct": "",
            })
        for pm in r.get("price_mismatch", []):
            rows.append({
                "code": code, "timeframe": tf, "issue": "price_mismatch",
                "date": pm["date"],
                "detail": json.dumps(pm, ensure_ascii=False, separators=(",", ":")),
                "diff_pct": pm.get("diff_pct", ""),
            })
        for qi in r.get("quality_issues", []):
            rows.append({
                "code": code, "timeframe": tf, "issue": f"quality_{qi['quality']}",
                "date": qi["date"],
                "detail": json.dumps(qi, ensure_ascii=False, separators=(",", ":")),
                "diff_pct": "",
            })
        for qd in r.get("qfq_diff", []):
            rows.append({
                "code": code, "timeframe": tf, "issue": "qfq_diff",
                "date": qd["date"],
                "detail": f"csv={qd['csv_close']:.4f} db={qd['db_close']:.4f}",
                "diff_pct": qd.get("price_diff_pct", ""),
            })

    if not rows:
        print("无差异数据，跳过 CSV 导出")
        return

    fields = ["code", "timeframe", "issue", "date", "detail", "diff_pct"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"✅ CSV 报告: {path}（{len(rows)} 条）")


# ---------------------------------------------------------------------------
# 进度打印
# ---------------------------------------------------------------------------

def _print_progress(done: int, total: int, agg_stats: dict, errors: int):
    print(f"  [{done}/{total}] 真错误={agg_stats['price_mismatch']} "
          f"复权差异={agg_stats['qfq_diff']} "
          f"缺失={agg_stats['missing']} "
          f"质量问题={agg_stats['quality_issues']} "
          f"错误={errors}")


# ---------------------------------------------------------------------------
# 时间窗口检查
# ---------------------------------------------------------------------------

def _check_time_window() -> Tuple[bool, str]:
    """
    检查当前时间是否在允许运行窗口内。

    规则:
      - 交易日: 17:00 以后 或 08:30 以前
      - 非交易日: 全天可运行

    Returns:
        (allowed: bool, message: str)
    """
    now = datetime.now(TZ_SH)
    today = now.strftime("%Y-%m-%d")
    is_td = _is_trading_day(today)
    hour, minute = now.hour, now.minute
    current_minutes = hour * 60 + minute

    if not is_td:
        return True, f"非交易日 {today}，当前 {now.strftime('%H:%M')}，允许运行"

    # 交易日: 08:30 (510min) 之前 或 17:00 (1020min) 之后
    if current_minutes < 510:
        return True, f"交易日 {today}，当前 {now.strftime('%H:%M')}（08:30 前），允许运行"
    elif current_minutes >= 1020:
        return True, f"交易日 {today}，当前 {now.strftime('%H:%M')}（17:00 后），允许运行"
    else:
        return False, (
            f"交易日 {today}，当前 {now.strftime('%H:%M')}，"
            f"不在允许窗口（需 17:00 后或 08:30 前）"
        )


# ---------------------------------------------------------------------------
# last_update 表管理
# ---------------------------------------------------------------------------

def _ensure_last_update_table(pool) -> None:
    """如果 last_update 表不存在则创建"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS last_update (
                    id          SERIAL PRIMARY KEY,
                    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    report      TEXT,
                    batch_size  INTEGER NOT NULL DEFAULT 0
                )
            """)
    print("✅ last_update 表已就绪")


def _get_last_update_time(pool) -> Optional[datetime]:
    """读取 last_update 表中最后一条记录的 updated_at"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM last_update ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                return row[0] if isinstance(row[0], datetime) else row[0]
    return None


def _insert_last_update(pool, report: str, batch_size: int) -> None:
    """向 last_update 表插入一条记录"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO last_update (updated_at, report, batch_size) VALUES (NOW(), %s, %s)",
                (report, batch_size),
            )
    print(f"📝 last_update 已写入: {report}")


# ---------------------------------------------------------------------------
# DB 删除指定日期范围数据
# ---------------------------------------------------------------------------

def _delete_db_range(pool, market: str, code: str, timeframe: str,
                     start_dt: datetime, end_dt: datetime) -> int:
    """
    删除 DB 中指定 symbol + timeframe + 时间范围的数据。

    根据 timeframe 自动定位 kline_{tf}_{year} 表，跨年时删除多张表。
    返回删除总行数。
    """
    total_deleted = 0
    start_year = start_dt.year
    end_year = end_dt.year

    for year in range(start_year, end_year + 1):
        table = f"kline_{timeframe}_{year}"
        # 该年的删除范围：取 start_dt/end_dt 与年份交集
        # DB 用 TIMESTAMP WITHOUT TIME ZONE（存上海时间 naive），传入 naive datetime 避免时区偏移
        y_start = max(start_dt.replace(tzinfo=None), datetime(year, 1, 1))
        y_end = min(end_dt.replace(tzinfo=None), datetime(year, 12, 31, 23, 59, 59))
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'DELETE FROM "{table}" WHERE symbol = %s AND time >= %s AND time <= %s',
                        (code, y_start, y_end),
                    )
                    deleted = cur.rowcount
                    if deleted > 0:
                        total_deleted += deleted
        except Exception:
            # 表不存在则跳过
            pass

    return total_deleted


# ---------------------------------------------------------------------------
# 增量 / 全量更新核心
# ---------------------------------------------------------------------------

def _do_update(
    market: str,
    csv_dir: str,
    batch_size: int = 5000,
    force_full: bool = False,
) -> Tuple[str, int, int]:
    """
    执行数据更新。

    逻辑:
      1. 如果 last_update 表不存在 → 创建 + 全量更新
      2. 如果表存在但无记录 → 全量更新（1D 5年, 15m 3年）
      3. 如果表有记录 → 读取最后 updated_at，从此时间到现在增量更新
      4. 增量更新时先删除旧数据再写入新数据（强一致）

    Returns:
        (mode: str, deleted_total: int, written_total: int)
    """
    from app.utils.db_market import get_market_kline_writer, get_market_db_manager

    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    pool = mgr._get_pool(market)

    try:
        # 1. 确保 last_update 表存在
        _ensure_last_update_table(pool)

        # 2. 读取上次更新时间
        last_updated = _get_last_update_time(pool)
        now = datetime.now(TZ_SH)

        if force_full or last_updated is None:
            # ── 全量更新 ──
            mode = "全量"
            start_1d = now - timedelta(days=5 * 365)   # 1D: 5 年
            start_15m = now - timedelta(days=3 * 365)   # 15m: 3 年
            print(f"\n{'='*60}")
            print(f"📦 全量更新")
            print(f"   1D  范围: {start_1d.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}（5 年）")
            print(f"   15m 范围: {start_15m.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}（3 年）")
            print(f"{'='*60}")
        else:
            # ── 增量更新 ──
            mode = "增量"
            start_1d = last_updated.replace(tzinfo=TZ_SH) if last_updated.tzinfo is None else last_updated
            start_15m = start_1d
            print(f"\n{'='*60}")
            print(f"📦 增量更新")
            print(f"   上次更新: {start_1d.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"   更新至:   {now.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")

        end_dt = now
        csv_syms = list_csv_codes(csv_dir)
        if not csv_syms:
            print(f"❌ CSV 目录下无数据: {csv_dir}")
            _insert_last_update(pool, f"{mode}更新失败: CSV 目录无数据", 0)
            return mode, 0, 0

        syms = sorted(csv_syms)
        total = len(syms)
        print(f"   股票数: {total}")
        print(f"   CSV 目录: {csv_dir}")
        print(f"   PROJECT_ROOT: {PROJECT_ROOT}\n")

        start_date_1d = start_1d.strftime("%Y-%m-%d")
        end_date_1d = end_dt.strftime("%Y-%m-%d")

        # 诊断: 抽样检查前 3 只股票的 CSV 加载情况
        for probe_code in syms[:3]:
            probe_data = load_baostock_csv(csv_dir, probe_code)
            probe_filtered = [r for r in probe_data if start_date_1d <= r["date"] <= end_date_1d]
            print(f"   🔍 {probe_code}: CSV 共 {len(probe_data)} 条, 筛选后 {len(probe_filtered)} 条"
                  f"（{start_date_1d} ~ {end_date_1d}）")
            if probe_data:
                print(f"      日期范围: {probe_data[0]['date']} ~ {probe_data[-1]['date']}")
        print()

        # ── 1D 更新 ──
        print(f"── 1D 更新（{mode}）──")
        deleted_1d = 0
        written_1d = 0

        for i, code in enumerate(syms):
            try:
                csv_data = load_baostock_csv(csv_dir, code)
                # 按日期筛选
                filtered = [r for r in csv_data if start_date_1d <= r["date"] <= end_date_1d]
                if not filtered:
                    continue

                # 先删除旧数据
                d = _delete_db_range(pool, market, code, "1D", start_1d, end_dt)
                deleted_1d += d

                # 再写入新数据
                records = []
                for r in filtered:
                    records.append({
                        "symbol": code,
                        "timeframe": "1D",
                        "time": _date_to_ts_midnight(r["date"]),
                        "open": r["open"],
                        "high": r["high"],
                        "low": r["low"],
                        "close": r["close"],
                        "volume": r["volume"],
                    })
                if records:
                    result = writer.bulk_write(market, records, on_conflict="update",
                                               batch_size=batch_size)
                    written_1d += result.get("inserted", 0)

            except Exception as e:
                logger.warning(f"1D 更新失败 {code}: {e}")

            if (i + 1) % 200 == 0 or i + 1 == total:
                print(f"  [{i+1}/{total}] 已删除={deleted_1d} 已写入={written_1d}")

        # ── 15m 更新 ──
        print(f"\n── 15m 更新（{mode}）──")
        print(f"   15m 需通过 DataSourceFactory 获取数据...")
        deleted_15m = 0
        written_15m = 0

        try:
            from app.data_sources.factory import DataSourceFactory
            source = DataSourceFactory.get_source(market)

            for i, code in enumerate(syms):
                try:
                    bars = source.get_kline(code, "15m", 5000)
                    if not bars:
                        continue

                    # 筛选时间范围
                    filtered = []
                    for b in bars:
                        bt = b["time"]
                        if isinstance(bt, (int, float)):
                            bt = datetime.fromtimestamp(bt, tz=TZ_SH)
                        elif isinstance(bt, str):
                            bt = datetime.fromisoformat(bt)
                        if bt.tzinfo is None:
                            bt = bt.replace(tzinfo=TZ_SH)
                        if start_15m <= bt <= end_dt:
                            filtered.append((bt, b))

                    if not filtered:
                        continue

                    # 删除旧数据
                    d = _delete_db_range(pool, market, code, "15m", start_15m, end_dt)
                    deleted_15m += d

                    # 写入新数据
                    records = []
                    for bt, b in filtered:
                        records.append({
                            "symbol": code,
                            "timeframe": "15m",
                            "time": bt,
                            "open": _safe_float(b.get("open")),
                            "high": _safe_float(b.get("high")),
                            "low": _safe_float(b.get("low")),
                            "close": _safe_float(b.get("close")),
                            "volume": _safe_float(b.get("volume")),
                        })
                    if records:
                        result = writer.bulk_write(market, records, on_conflict="update",
                                                   batch_size=batch_size)
                        written_15m += result.get("inserted", 0)

                except Exception as e:
                    logger.warning(f"15m 更新失败 {code}: {e}")

                if (i + 1) % 200 == 0 or i + 1 == total:
                    print(f"  [{i+1}/{total}] 已删除={deleted_15m} 已写入={written_15m}")

        except ImportError:
            print("   ⚠️  DataSourceFactory 不可用，跳过 15m 更新")
        except Exception as e:
            print(f"   ⚠️  15m 更新异常: {e}")

        # ── 写入 last_update ──
        report = (
            f"{mode}更新完成 | "
            f"1D: 删除={deleted_1d} 写入={written_1d} | "
            f"15m: 删除={deleted_15m} 写入={written_15m} | "
            f"股票数={total}"
        )
        _insert_last_update(pool, report, batch_size)
        print(f"\n{'='*60}")
        print(f"✅ {report}")
        print(f"{'='*60}")

        return mode, deleted_1d + deleted_15m, written_1d + written_15m

    finally:
        mgr.close_all_pools()


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------

def main():
    global _INTERRUPTED

    parser = argparse.ArgumentParser(
        description="BaoStock 前复权日线 vs DB 比对 & 修正（支持 1D + 15m）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-T", "--type",
        choices=["1D", "15m", "all"], default="1D",
        help="比对类型: 1D(日线) / 15m(15分钟线) / all(两者都比)")
    parser.add_argument("--symbol", help="只比对指定股票")
    parser.add_argument("--market", default="CNStock", help="市场（默认 CNStock）")
    parser.add_argument("--csv-dir", default=None,
        help="BaoStock CSV 目录（默认 optimizer_output/CNStock/daily，相对项目根目录）")
    parser.add_argument("--tolerance", type=float, default=0.01,
        help="价格容差（默认 0.01 = 1%%）")
    parser.add_argument("--dry-run", action="store_true",
        help="只报告不修正 DB")
    parser.add_argument("--fix", action="store_true",
        help="比对 + 修正 DB 中的错误和缺失")
    parser.add_argument("--csv", help="导出 CSV 报告路径")
    parser.add_argument("-w", "--workers", type=int, default=None,
        help="进程数（默认 CPU 核数，上限 8）")
    parser.add_argument("--batch-size", type=int, default=50,
        help="每个子进程一次处理的股票数（默认 50）")
    parser.add_argument("--update", action="store_true",
        help="增量/全量更新模式（含时间窗口检查）")
    parser.add_argument("--force-full", action="store_true",
        help="强制全量更新（配合 --update 使用，忽略时间窗口）")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ============================================================
    # 更新模式: --update
    # ============================================================
    if args.update:
        _build_trading_day_cache(args.market)

        # 时间窗口检查
        if not args.force_full:
            allowed, msg = _check_time_window()
            if not allowed:
                print(f"❌ {msg}")
                print(f"   使用 --force-full 可跳过时间窗口检查")
                return 1
            print(f"✅ {msg}")
        else:
            print(f"⚡ 强制模式，跳过时间窗口检查")

        # CSV 目录
        csv_dir = args.csv_dir
        if not csv_dir:
            csv_dir = os.path.join(PROJECT_ROOT, "optimizer_output", "CNStock", "daily")
        if not os.path.isdir(csv_dir):
            print(f"❌ CSV 目录不存在: {csv_dir}")
            return 1

        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        if not mgr.market_db_exists(args.market):
            print(f"❌ {args.market}_db 不存在")
            return 1

        try:
            _do_update(
                market=args.market,
                csv_dir=csv_dir,
                batch_size=args.batch_size * 100,  # batch_size 这里指每次 DB 写入条数
                force_full=args.force_full,
            )
        except Exception as e:
            print(f"❌ 更新失败: {e}")
            traceback.print_exc()
            return 1
        return 0

    # ============================================================
    # 原有功能: 比对 & 修正
    # ============================================================

    # CSV 目录
    csv_dir = args.csv_dir
    if not csv_dir:
        csv_dir = os.path.join(PROJECT_ROOT, "optimizer_output", "CNStock", "daily")
    if not os.path.isdir(csv_dir):
        print(f"❌ CSV 目录不存在: {csv_dir}")
        print(f"   PROJECT_ROOT={PROJECT_ROOT}")
        print(f"   请用 --csv-dir 指定路径，或在项目根目录运行: python optimizer/verify_and_fix.py")
        return 1

    # 时间框架
    if args.type == "all":
        timeframes = ["1D", "15m"]
    else:
        timeframes = [args.type]

    from app.utils.db_market import get_market_kline_writer, get_market_db_manager
    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    market = args.market

    if not mgr.market_db_exists(market):
        print(f"❌ {market}_db 不存在")
        return 1

    # 获取 DB 中的股票列表（只查 1D 表，比 stats() 快）
    db_syms = get_db_symbols(writer, market)

    # 获取 CSV 中的股票列表
    csv_syms = list_csv_codes(csv_dir)
    if not csv_syms:
        print(f"❌ CSV 目录下无数据: {csv_dir}")
        return 1

    if args.symbol:
        syms = [args.symbol]
        if args.symbol not in csv_syms and args.symbol not in db_syms:
            print(f"❌ {args.symbol} 在 CSV 和 DB 中都不存在")
            return 1
    else:
        syms = sorted(csv_syms | db_syms)

    total = len(syms)
    print(f"\n{'='*60}")
    print(f"📊 数据比对 & 修正")
    print(f"   CSV 目录: {csv_dir}")
    print(f"   DB 市场:  {market}")
    print(f"   股票数:   {total}（CSV={len(csv_syms)} DB={len(db_syms)}）")
    print(f"   比对类型: {', '.join(timeframes)}")
    print(f"   价格容差: {args.tolerance * 100:.1f}%")
    print(f"   模式:     {'dry-run（只报告）' if args.dry_run else '修正 DB'}")
    print(f"{'='*60}\n")

    _build_trading_day_cache(market)

    cpu_count = mp.cpu_count()
    n_workers = min(args.workers or cpu_count, 8, total)
    if args.symbol:
        n_workers = 1
    batch_size = max(1, min(args.batch_size, total // n_workers + 1))
    batches = [syms[i:i + batch_size] for i in range(0, total, batch_size)]

    all_results: List[Dict[str, Any]] = []
    all_errors: List[Tuple[str, str]] = []
    agg_stats = {"total": 0, "has_diff": 0, "missing": 0,
                 "price_mismatch": 0, "quality_issues": 0, "qfq_diff": 0}

    # ============================================================
    # 1D 比对（纯 CSV vs DB，无网络，可多进程）
    # ============================================================
    if "1D" in timeframes:
        print(f"\n── 1D 比对 ──")

        if n_workers <= 1:
            for code in syms:
                if _INTERRUPTED:
                    break
                try:
                    csv_data = load_baostock_csv(csv_dir, code)
                    db_data = load_db_data(writer, market, code, "1D")
                    if not csv_data and not db_data:
                        continue
                    cmp = compare_1d(code, csv_data, db_data, args.tolerance)
                    all_results.append({"timeframe": "1D", **cmp})
                    agg_stats["total"] += 1
                    if cmp["missing_in_db"]:
                        agg_stats["missing"] += len(cmp["missing_in_db"])
                    if cmp["price_mismatch"]:
                        agg_stats["price_mismatch"] += len(cmp["price_mismatch"])
                    if cmp["quality_issues"]:
                        agg_stats["quality_issues"] += len(cmp["quality_issues"])
                    if cmp.get("qfq_diff"):
                        agg_stats["qfq_diff"] += len(cmp["qfq_diff"])
                    if cmp["fix_records"]:
                        agg_stats["has_diff"] += 1
                except Exception as e:
                    all_errors.append((code, f"{type(e).__name__}: {e}"))

                done = agg_stats["total"]
                if done % 200 == 0 or done == total:
                    _print_progress(done, total, agg_stats, len(all_errors))
        else:
            task_args = [(batch, market, csv_dir, args.tolerance) for batch in batches]
            pool = mp.Pool(n_workers)
            try:
                results_iter = pool.imap_unordered(_worker_compare_1d, task_args, chunksize=1)
                done_batches = 0
                for batch_result in results_iter:
                    if _INTERRUPTED:
                        break
                    all_results.extend(batch_result["results"])
                    all_errors.extend(batch_result["errors"])
                    for k in agg_stats:
                        agg_stats[k] += batch_result["stats"].get(k, 0)
                    done_batches += 1
                    processed = min(done_batches * batch_size, total)
                    if done_batches % max(1, len(batches) // 20) == 0 or done_batches == len(batches):
                        _print_progress(processed, total, agg_stats, len(all_errors))
            except KeyboardInterrupt:
                _INTERRUPTED = True
            finally:
                pool.terminate()
                pool.join()

    # ============================================================
    # 15m 比对（仅 DB 质量检查，无本地参考数据）
    # ============================================================
    if "15m" in timeframes and not _INTERRUPTED:
        print(f"\n── 15m 质量检查 ──")
        print(f"   模式: 仅检查 DB 数据质量（无本地参考数据，不做价格比对）")

        if args.symbol:
            code = args.symbol
            try:
                db_15m = load_db_data(writer, market, code, "15m")
                if db_15m:
                    quality_issues = []
                    for r in db_15m:
                        q = classify_bar_quality(r)
                        if q != "ok":
                            quality_issues.append({
                                "date": _ts_to_date(r["time"]),
                                "quality": q,
                                "db_ohlc": {
                                    "open": _safe_float(r.get("open")),
                                    "high": _safe_float(r.get("high")),
                                    "low": _safe_float(r.get("low")),
                                    "close": _safe_float(r.get("close")),
                                    "volume": _safe_float(r.get("volume")),
                                },
                            })
                    all_results.append({
                        "timeframe": "15m", "code": code,
                        "csv_count": 0, "db_count": len(db_15m),
                        "missing_in_db": [], "extra_in_db": [],
                        "price_mismatch": [],
                        "quality_issues": quality_issues,
                        "fix_records": [],
                    })
                    agg_stats["total"] += 1
                    if quality_issues:
                        agg_stats["quality_issues"] += len(quality_issues)
                        agg_stats["has_diff"] += 1
            except Exception as e:
                all_errors.append((code, f"{type(e).__name__}: {e}"))
        else:
            task_args = [(batch, market) for batch in batches]
            pool = mp.Pool(n_workers)
            try:
                results_iter = pool.imap_unordered(
                    _worker_check_15m_quality, task_args, chunksize=1)
                done_batches = 0
                for batch_result in results_iter:
                    if _INTERRUPTED:
                        break
                    all_results.extend(batch_result["results"])
                    all_errors.extend(batch_result["errors"])
                    for k in agg_stats:
                        agg_stats[k] += batch_result["stats"].get(k, 0)
                    done_batches += 1
                    processed = min(done_batches * batch_size, total)
                    if done_batches % max(1, len(batches) // 20) == 0 or done_batches == len(batches):
                        _print_progress(processed, total, agg_stats, len(all_errors))
            except KeyboardInterrupt:
                _INTERRUPTED = True
            finally:
                pool.terminate()
                pool.join()

    # ============================================================
    # 汇总
    # ============================================================
    print(f"\n{'='*60}")
    status = "中断" if _INTERRUPTED else "完成"
    print(f"比对{status}: {agg_stats['total']} 只")
    print(f"  有差异: {agg_stats['has_diff']} 只")
    print(f"  缺失 bar: {agg_stats['missing']}")
    print(f"  真错误（需修正）: {agg_stats['price_mismatch']}")
    print(f"  复权差异（不修正）: {agg_stats['qfq_diff']}")
    print(f"  质量问题: {agg_stats['quality_issues']}")
    print(f"  查询错误: {len(all_errors)} 只")

    # 缺失最多 top 10
    missing_top = sorted(
        [r for r in all_results if r.get("missing_in_db")],
        key=lambda r: len(r["missing_in_db"]),
        reverse=True,
    )
    if missing_top:
        print(f"\n缺失最多的 10 只:")
        for r in missing_top[:10]:
            print(f"  {r['code']:>8} | {r.get('timeframe', '1D'):>3} | "
                  f"缺失 {len(r['missing_in_db'])} 根 | "
                  f"CSV={r['csv_count']} DB={r['db_count']}")

    # 价格偏差最多 top 10
    mismatch_top = sorted(
        [r for r in all_results if r.get("price_mismatch")],
        key=lambda r: len(r["price_mismatch"]),
        reverse=True,
    )
    if mismatch_top:
        print(f"\n价格偏差最多的 10 只:")
        for r in mismatch_top[:10]:
            print(f"  {r['code']:>8} | {r.get('timeframe', '1D'):>3} | "
                  f"偏差 {len(r['price_mismatch'])} 根")

    # 质量问题最多 top 10
    quality_top = sorted(
        [r for r in all_results if r.get("quality_issues")],
        key=lambda r: len(r["quality_issues"]),
        reverse=True,
    )
    if quality_top:
        print(f"\n质量问题最多的 10 只:")
        for r in quality_top[:10]:
            print(f"  {r['code']:>8} | {r.get('timeframe', '1D'):>3} | "
                  f"问题 {len(r['quality_issues'])} 根")

    # ============================================================
    # 修正写入
    # ============================================================
    if args.fix and not _INTERRUPTED and not args.dry_run:
        print(f"\n{'='*60}")
        print(f"🔧 开始修正 DB...")

        fix_count_1d = 0
        fix_count_15m = 0

        for r in all_results:
            if _INTERRUPTED:
                break
            code = r["code"]
            tf = r.get("timeframe", "1D")
            fix_records = r.get("fix_records", [])
            if not fix_records:
                continue

            if tf == "1D":
                n = write_fixes_1d(writer, market, code, fix_records, dry_run=False)
                fix_count_1d += n
            elif tf == "15m":
                n = write_fixes_15m(writer, market, code, fix_records, dry_run=False)
                fix_count_15m += n

        print(f"  1D 修正: {fix_count_1d} 根")
        print(f"  15m 修正: {fix_count_15m} 根")

    elif args.fix and args.dry_run:
        fix_total = sum(len(r.get("fix_records", [])) for r in all_results)
        print(f"\n🔍 dry-run: 共 {fix_total} 根需要修正（未写入 DB）")

    # ============================================================
    # 错误列表
    # ============================================================
    if all_errors:
        print(f"\n⚠️  查询失败（前 10 只）:")
        for code, msg in all_errors[:10]:
            print(f"  {code}: {msg}")
        if len(all_errors) > 10:
            print(f"  ... 还有 {len(all_errors) - 10} 只")

    # ============================================================
    # CSV 导出
    # ============================================================
    if args.csv:
        export_csv(all_results, args.csv)

    mgr.close_all_pools()
    return 1 if (all_errors or _INTERRUPTED) else 0


if __name__ == "__main__":
    sys.exit(main())
