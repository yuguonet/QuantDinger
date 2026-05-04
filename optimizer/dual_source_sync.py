#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# dual_source_sync.py — 双源并发下载 + 实时比对 + 校验修复 + 写库（同步流）
# ============================================================================
#
# 合并自: tdx_download.py / check_continuity.py / verify_and_fix.py
#
# 核心流程（同步、非流水线）:
#   1. 双源并发: 对每只股票，同时从两个数据源下载 1D 或 15m 数据
#   2. 实时比对: 单条数据回来后，立即与另一源比对或自校验
#   3. 时间标准化: 统一时间戳格式（15m 校准到标准 bar 时间）
#   4. 修正写库: 比对结果（缺失/错误/质量问题）修正后写入 db_market
#
# 数据源:
#   1D:  通达信 (TDX) + BaoStock
#   15m: 通达信 (TDX) + AKShare (东方财富, 前复权)
#
# 用法:
#   python dual_source_sync.py -T 1D                          # 日线双源
#   python dual_source_sync.py -T 15m                         # 15分钟线双源
#   python dual_source_sync.py -T 1D -s 2023-01-01 -e 2026-05-01
#   python dual_source_sync.py -T 1D -w 8                     # 8进程
#   python dual_source_sync.py -T 1D --symbol 600519          # 单只
#   python dual_source_sync.py -T 1D --dry-run                # 只比对不写库
#   python dual_source_sync.py -T 1D --tolerance 0.02         # 价格容差2%
#   python dual_source_sync.py -T 1D --csv report.csv         # 导出报告
#
# 依赖:
#   - db_market.py / db_multi.py（backend_api_python/app/utils/）
#   - pytdx (通达信)
#   - baostock (BaoStock, 1D)
#   - akshare (AKShare, 15m)
#   - psycopg2
#
# 创建时间: 2026-05-04
# ============================================================================

from __future__ import annotations

import os
import sys
import csv
import math
import time
import signal
import logging
import argparse
import multiprocessing as mp
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径 & 环境
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
# 时间常量 & 工具
# ---------------------------------------------------------------------------

TZ_SH = timezone(timedelta(hours=8))
CONNECT_TIMEOUT = 5

# 通达信服务器
TDX_SERVERS = [
    ('218.75.126.9', 7709),
    ('115.238.56.198', 7709),
    ('124.160.88.183', 7709),
    ('60.12.136.250', 7709),
    ('218.108.98.244', 7709),
    ('218.108.47.69', 7709),
    ('180.153.39.51', 7709),
]

# 15m 标准 bar 时间（16 根，不含 9:30 开盘集合竞价）
_BAR_TIMES_15M = [
    (9, 45), (10, 0), (10, 15), (10, 30), (10, 45),
    (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15),
    (14, 30), (14, 45), (15, 0),
]
_BAR_SET_15M: Set[Tuple[int, int]] = set(_BAR_TIMES_15M)

# 交易日历缓存
_TRADING_DAY_SET: frozenset[str] | None = None
_TRADING_DAY_SILENT: bool = False


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


def _trading_days_between(d1: str, d2: str) -> int:
    if d1 >= d2:
        return 0
    if _TRADING_DAY_SET is None:
        _build_trading_day_cache(silent=True)
    cur = datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(d2, "%Y-%m-%d")
    count = 0
    while cur < end:
        if cur.strftime("%Y-%m-%d") in _TRADING_DAY_SET:
            count += 1
        cur += timedelta(days=1)
    return count


def _ts_to_date(ts) -> str:
    """从 datetime / unix timestamp / ISO 字符串提取 YYYY-MM-DD"""
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
        return dt.strftime("%Y-%m-%d")
    if isinstance(ts, str):
        return ts[:10]
    return datetime.fromtimestamp(ts, tz=TZ_SH).strftime("%Y-%m-%d")


def _safe_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _date_to_ts_midnight(d: str) -> datetime:
    return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_SH)


def _next_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 索引 key 提取（模块级函数，避免闭包重复创建）
# ---------------------------------------------------------------------------

def _key_1d(rec: Dict[str, Any]) -> str:
    """1D 记录的索引 key: YYYY-MM-DD"""
    ts = rec.get("time")
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
        return dt.strftime("%Y-%m-%d")
    if isinstance(ts, str):
        return ts[:10]
    return str(rec.get("date", ""))[:10]


def _key_15m(rec: Dict[str, Any]) -> Tuple[str, int, int]:
    """15m 记录的索引 key: (YYYY-MM-DD, hour, minute)"""
    ts = rec.get("time")
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
        return (dt.strftime("%Y-%m-%d"), dt.hour, dt.minute)
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=TZ_SH)
        return (dt.strftime("%Y-%m-%d"), dt.hour, dt.minute)
    return ("", 0, 0)


# ---------------------------------------------------------------------------
# 时间标准化
# ---------------------------------------------------------------------------

def normalize_15m_time(dt: datetime) -> Optional[datetime]:
    """
    标准化 15m 时间戳:
      - 9:30 → 丢弃（返回 None）
      - 11:30~13:00 → 11:30
      - 15:00~23:59 → 15:00
      - 其他时间保持原样
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SH)
    total_min = dt.hour * 60 + dt.minute

    # 9:30 → 丢弃
    if total_min == 570:
        return None

    # 11:30~13:00 → 11:30
    if 690 <= total_min < 780:
        return dt.replace(hour=11, minute=30, second=0, microsecond=0)

    # 15:00~23:59 → 15:00
    if total_min >= 900:
        return dt.replace(hour=15, minute=0, second=0, microsecond=0)

    return dt


def normalize_record_time(rec: Dict[str, Any], timeframe: str) -> Optional[Dict[str, Any]]:
    """
    标准化单条记录的时间戳。返回 None 表示该记录应丢弃。
    """
    ts = rec.get("time")
    if ts is None:
        # 从 date/datetime 字段解析
        dt_str = rec.get("date") or rec.get("datetime") or ""
        if not dt_str:
            return None
        try:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                try:
                    dt = datetime.strptime(str(dt_str).strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        except Exception:
            return None
    elif isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=TZ_SH)
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SH)

    if timeframe == "15m":
        dt = normalize_15m_time(dt)
        if dt is None:
            return None

    return {**rec, "time": dt}


# ---------------------------------------------------------------------------
# 数据质量分类
# ---------------------------------------------------------------------------

def classify_bar(bar: Dict[str, Any]) -> str:
    """
    对单根 K 线做质量分类。
    Returns: "ok" / "bad" / "suspended" / "incomplete"
    """
    o = _safe_float(bar.get("open"))
    h = _safe_float(bar.get("high"))
    l = _safe_float(bar.get("low"))
    c = _safe_float(bar.get("close"))
    v = _safe_float(bar.get("volume"))

    if o == 0 and h == 0 and l == 0 and c == 0:
        return "bad"
    if v == 0 and o == h == l == c and o > 0:
        return "suspended"
    if h > 0 and l > 0 and (h < l or (o > 0 and (o > h or o < l)) or (c > 0 and (c > h or c < l))):
        return "incomplete"
    if v == 0 and not (o == h == l == c):
        return "incomplete"
    if (o == 0) != (h == 0) or (h == 0) != (l == 0) or (l == 0) != (c == 0):
        return "incomplete"
    return "ok"


# ---------------------------------------------------------------------------
# 价格偏差检测
# ---------------------------------------------------------------------------

def _price_diff_pct(ref: float, db: float) -> float:
    if ref == 0:
        return 0.0 if db == 0 else 999.0
    return abs(db - ref) / ref


def _calc_return(prev_close: float, curr_close: float) -> float:
    if prev_close == 0:
        return 0.0
    return (curr_close - prev_close) / prev_close


# ═══════════════════════════════════════════════════════
# 数据源: 通达信 (TDX)
# ═══════════════════════════════════════════════════════

def _import_tdx():
    try:
        from pytdx.hq import TdxHq_API
        return TdxHq_API
    except ImportError:
        raise ImportError("pytdx 未安装: pip install pytdx")


def _connect_tdx(worker_id: int):
    TdxHq_API = _import_tdx()
    for attempt in range(len(TDX_SERVERS)):
        idx = (worker_id + attempt) % len(TDX_SERVERS)
        srv = TDX_SERVERS[idx]
        try:
            api = TdxHq_API()
            api.connect(srv[0], srv[1], time_out=CONNECT_TIMEOUT)
            return api
        except Exception:
            continue
    raise ConnectionError(f"TDX Worker-{worker_id}: 所有服务器连接失败")


def _fetch_security_list(api, market, offset):
    """获取证券列表，自动处理 pytdx offset>=8000 解析失败的 bug"""
    if not hasattr(_fetch_security_list, '_patched'):
        from pytdx.parser.get_security_list import GetSecurityList
        import struct as _struct
        from pytdx.helper import get_volume

        _orig_parse = GetSecurityList.parseResponse

        def _robust_parse(self, body_buf):
            result = None
            try:
                result = _orig_parse(self, body_buf)
            except Exception:
                pass
            if result and len(result) > 0:
                return result
            try:
                if len(body_buf) < 2:
                    return None
                num, = _struct.unpack("<H", body_buf[:2])
                pos = 2
                stocks = []
                for _ in range(num):
                    if pos + 29 > len(body_buf):
                        break
                    one_bytes = body_buf[pos:pos + 29]
                    code_bytes, volunit, name_bytes, _, decimal_point, pre_close_raw, _ = \
                        _struct.unpack("<6sH8s4sBI4s", one_bytes)
                    code = code_bytes.decode("utf-8", errors="ignore").strip('\x00').strip()
                    if not code:
                        pos += 29
                        continue
                    name = name_bytes.decode("gbk", errors="ignore").rstrip("\x00")
                    pre_close = get_volume(pre_close_raw)
                    stocks.append({
                        'code': code, 'volunit': volunit,
                        'decimal_point': decimal_point, 'name': name,
                        'pre_close': pre_close,
                    })
                    pos += 29
                return stocks if stocks else None
            except Exception:
                pass
            try:
                num2, = _struct.unpack("<H", body_buf[:2])
                stocks2 = []
                scan = 2
                while scan < len(body_buf) - 29 and len(stocks2) < num2:
                    chunk = body_buf[scan:scan+6]
                    code_try = chunk.decode("ascii", errors="ignore").strip('\x00').strip()
                    if code_try.isdigit() and len(code_try) >= 4:
                        name_chunk = body_buf[scan+8:scan+24]
                        name_try = name_chunk.decode("gbk", errors="ignore").rstrip("\x00")
                        if name_try:
                            stocks2.append({
                                'code': code_try, 'volunit': 100,
                                'decimal_point': 2, 'name': name_try,
                                'pre_close': 0,
                            })
                            scan += 29
                            continue
                    scan += 1
                return stocks2 if stocks2 else None
            except Exception:
                return None

        GetSecurityList.parseResponse = _robust_parse
        _fetch_security_list._patched = True

    return api.get_security_list(market, offset)


def get_stock_list_tdx() -> List[Tuple[int, str, str]]:
    """从通达信获取全部A股代码"""
    api = _connect_tdx(0)
    a_shares = []

    for offset in range(0, 40000, 1000):
        batch = _fetch_security_list(api, 0, offset)
        if not batch:
            break
        for s in batch:
            c = s['code']
            if c.startswith(('00', '30', '83', '87', '43')):
                a_shares.append((0, c, s['name']))

    for offset in range(0, 40000, 1000):
        batch = _fetch_security_list(api, 1, offset)
        if not batch:
            break
        for s in batch:
            c = s['code']
            if c.startswith(('60', '68')):
                a_shares.append((1, c, s['name']))

    api.disconnect()

    seen = set()
    unique = []
    for m, c, n in a_shares:
        if c not in seen:
            seen.add(c)
            unique.append((m, c, n))
    return unique


# ---------------------------------------------------------------------------
# TDX 批量下载（复用连接，worker 级别）
# ---------------------------------------------------------------------------

def batch_download_tdx(
    stocks: List[Tuple[int, str, str]],
    timeframe: str,
    start_date: str,
    end_date: str,
    worker_id: int = 0,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    通达信批量下载，复用单个 TCP 连接。
    返回 {code: [records, ...], ...}
    """
    api = _connect_tdx(worker_id)
    results: Dict[str, List[Dict[str, Any]]] = {}

    try:
        for market_code, code, name in stocks:
            try:
                if timeframe == "1D":
                    results[code] = _download_tdx_1d(api, market_code, code, start_date, end_date)
                else:
                    results[code] = _download_tdx_15m(api, market_code, code, start_date, end_date)
            except (ConnectionError, OSError, TimeoutError):
                # 连接断了，重连一次
                try:
                    api.disconnect()
                except Exception:
                    pass
                try:
                    api = _connect_tdx(worker_id)
                    if timeframe == "1D":
                        results[code] = _download_tdx_1d(api, market_code, code, start_date, end_date)
                    else:
                        results[code] = _download_tdx_15m(api, market_code, code, start_date, end_date)
                except Exception as e:
                    logger.debug("[TDX] %s 重连后仍失败: %s", code, e)
                    results[code] = []
            except Exception as e:
                logger.debug("[TDX] %s 异常: %s", code, e)
                results[code] = []
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    return results


def _download_tdx_1d(api, market_code: int, code: str,
                      start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """从已连接的 TDX api 下载 1D 数据"""
    all_bars = []
    for offset in range(0, 800 * 10, 800):
        bars = api.get_security_bars(9, market_code, code, offset, 800)
        if not bars:
            break
        all_bars = bars + all_bars
        if len(bars) < 800:
            break
        if bars[0]['datetime'][:10] <= start_date:
            break

    records = []
    for b in all_bars:
        dt_str = b['datetime'][:10]
        if start_date <= dt_str <= end_date:
            records.append({
                "date": dt_str,
                "open": float(b['open']),
                "high": float(b['high']),
                "low": float(b['low']),
                "close": float(b['close']),
                "volume": float(b['vol']),
                "amount": float(b['amount']),
            })
    return records


def _download_tdx_15m(api, market_code: int, code: str,
                       start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """从已连接的 TDX api 下载 15m 数据"""
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    span_days = (end_dt - start_dt).days
    max_req = int(span_days / 33) + 3

    all_bars = []
    for i in range(max_req):
        bars = api.get_security_bars(1, market_code, code, i * 800, 800)
        if not bars:
            break
        all_bars = bars + all_bars
        if len(bars) < 800:
            break
        try:
            first_dt = datetime.strptime(bars[0]['datetime'][:10], '%Y-%m-%d')
            if first_dt <= start_dt:
                break
        except Exception:
            pass

    records = []
    for b in all_bars:
        dt_str = b['datetime'][:10]
        if start_date <= dt_str <= end_date:
            try:
                dt = datetime.strptime(b['datetime'], '%Y-%m-%d %H:%M')
            except ValueError:
                dt = datetime.strptime(dt_str, '%Y-%m-%d')
            records.append({
                "time": dt.replace(tzinfo=TZ_SH),
                "open": float(b['open']),
                "high": float(b['high']),
                "low": float(b['low']),
                "close": float(b['close']),
                "volume": float(b['vol']),
                "amount": float(b['amount']),
            })
    return records


# ---------------------------------------------------------------------------
# 单只 TDX 下载（用于 symbol 模式，走批量接口的单元素 batch）
# ---------------------------------------------------------------------------

def _lookup_market_code(code: str) -> int:
    """从通达信查找股票的 market_code (0=深圳, 1=上海)，找不到返回 0"""
    try:
        api = _connect_tdx(0)
        for m in (0, 1):
            for offset in range(0, 40000, 1000):
                batch = _fetch_security_list(api, m, offset)
                if not batch:
                    break
                for s in batch:
                    if s['code'] == code:
                        api.disconnect()
                        return m
        api.disconnect()
    except Exception:
        pass
    return 0


# ═══════════════════════════════════════════════════════
# 数据源: BaoStock (1D) — 进程内单例连接
# ═══════════════════════════════════════════════════════

# BaoStock 全局连接状态（进程级单例，非线程安全）
_bs_logged_in: bool = False


def _ensure_bs_login():
    """确保 BaoStock 已登录（进程级单例，非线程安全）"""
    global _bs_logged_in
    if _bs_logged_in:
        return
    import baostock as bs
    lg = bs.login()
    if lg.error_code != '0':
        raise ConnectionError(f"BaoStock 登录失败: {lg.error_msg}")
    _bs_logged_in = True


def _bs_logout():
    """登出 BaoStock"""
    global _bs_logged_in
    if not _bs_logged_in:
        return
    try:
        import baostock as bs
        bs.logout()
    except Exception:
        pass
    _bs_logged_in = False


def _import_baostock():
    try:
        import baostock as bs
        return bs
    except ImportError:
        raise ImportError("baostock 未安装: pip install baostock")


def download_baostock_1d(market_code: int, code: str, start_date: str, end_date: str,
                          worker_id: int = 0) -> List[Dict[str, Any]]:
    """
    从 BaoStock 下载单只股票的 1D 前复权数据。

    注意: BaoStock 是全局单例 TCP 连接，非线程安全。
    调用方应确保同一进程内不并发调用此函数。
    """
    bs = _import_baostock()
    _ensure_bs_login()

    bs_code = f"{'sh' if market_code == 1 else 'sz'}.{code}"

    for retry in range(3):
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="1",  # 前复权
            )
            if rs.error_code != '0':
                if retry == 2:
                    return []
                continue

            records = []
            while rs.next():
                row = rs.get_row_data()
                if len(row) < 7:
                    continue
                dt_str = str(row[0]).strip()
                if not dt_str:
                    continue
                try:
                    o = float(row[1]) if row[1] else 0
                    h = float(row[2]) if row[2] else 0
                    l = float(row[3]) if row[3] else 0
                    c = float(row[4]) if row[4] else 0
                    v = float(row[5]) if row[5] else 0
                    amount = float(row[6]) if row[6] else 0
                except (ValueError, TypeError):
                    continue
                if o == 0 and c == 0:
                    continue
                records.append({
                    "date": dt_str,
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": v, "amount": amount,
                })
            return records
        except (ConnectionError, OSError, TimeoutError):
            if retry == 2:
                return []
            # 重连
            _bs_logout()
            try:
                _ensure_bs_login()
            except Exception:
                pass
        except Exception as e:
            logger.debug("[BaoStock] %s 异常: %s", code, e)
            return []

    return []


def batch_download_baostock(
    stocks: List[Tuple[int, str, str]],
    start_date: str,
    end_date: str,
    worker_id: int = 0,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    BaoStock 批量下载，复用进程级单例连接。
    注意: 不可在线程内并发调用。
    """
    results: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for market_code, code, name in stocks:
            results[code] = download_baostock_1d(market_code, code, start_date, end_date, worker_id)
    finally:
        _bs_logout()
    return results


# ═══════════════════════════════════════════════════════
# 数据源: AKShare (15m, 前复权)
# ═══════════════════════════════════════════════════════

def _import_akshare():
    try:
        import akshare as ak
        return ak
    except ImportError:
        raise ImportError("akshare 未安装: pip install akshare")


def download_akshare_15m(market_code: int, code: str, start_date: str, end_date: str,
                          worker_id: int = 0) -> List[Dict[str, Any]]:
    """从 AKShare 下载单只股票的 15m 前复权数据"""
    ak = _import_akshare()
    ak_start = f"{start_date} 09:30:00"
    ak_end = f"{end_date} 15:00:00"

    for retry in range(3):
        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=ak_start,
                end_date=ak_end,
                period="15",
                adjust="qfq",
            )
            if df is None or df.empty:
                return []

            records = []
            for _, row in df.iterrows():
                try:
                    dt_str = str(row['时间']).strip()
                    o = float(row['开盘'])
                    h = float(row['最高'])
                    l = float(row['最低'])
                    c = float(row['收盘'])
                    v = float(row['成交量'])
                    amount = float(row['成交额'])
                    if o == 0 and c == 0:
                        continue
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                        try:
                            dt = datetime.strptime(dt_str, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                    records.append({
                        "time": dt.replace(tzinfo=TZ_SH),
                        "open": o, "high": h, "low": l, "close": c,
                        "volume": v, "amount": amount,
                    })
                except (ValueError, TypeError, KeyError):
                    continue
            return records
        except Exception as e:
            err_msg = str(e)
            if '该股票' in err_msg or '暂无数据' in err_msg or 'None' in err_msg:
                return []
            if retry == 2:
                logger.warning("[AKShare] %s 失败: %s", code, e)
                return []
            time.sleep(2 * (retry + 1))
    return []


# ═══════════════════════════════════════════════════════
# 比对 & 校验核心（修复: O(n²) → O(n), 去重, amount 保留）
# ═══════════════════════════════════════════════════════

def _build_key_index(records: List[Dict[str, Any]], timeframe: str) -> Dict:
    """构建 key → record 的索引，自动去重（后出现的覆盖先出现的）"""
    key_fn = _key_1d if timeframe == "1D" else _key_15m
    index: Dict = {}
    for r in records:
        k = key_fn(r)
        if k and k != ("", 0, 0) and k != "":
            index[k] = r  # 后出现的覆盖先出现的（去重）
    return index


def compare_and_verify(
    code: str,
    timeframe: str,
    recs_a: List[Dict[str, Any]],
    recs_b: List[Dict[str, Any]],
    name_a: str,
    name_b: str,
    tolerance: float = 0.01,
) -> Dict[str, Any]:
    """
    双源比对 + 自校验。

    逻辑:
      1. 两个源都有数据 → 逐 bar 比对，取更优者
      2. 只有一个源有数据 → 对该源做自校验（质量检查）
      3. 两个源都没有 → 记录为空
    """
    result = {
        "code": code,
        "timeframe": timeframe,
        "merged_records": [],
        "source_a_count": len(recs_a),
        "source_b_count": len(recs_b),
        "missing_in_b": [],
        "missing_in_a": [],
        "price_mismatch": [],
        "quality_issues": [],
        "fix_from": "",
    }

    # 标准化时间
    norm_a = [nr for r in recs_a if (nr := normalize_record_time(r, timeframe)) is not None]
    norm_b = [nr for r in recs_b if (nr := normalize_record_time(r, timeframe)) is not None]

    # 构建索引（自动去重）
    a_by_key = _build_key_index(norm_a, timeframe)
    b_by_key = _build_key_index(norm_b, timeframe)

    a_keys = set(a_by_key.keys())
    b_keys = set(b_by_key.keys())

    # ── 场景1: 两个源都有数据 → 逐 bar 比对 ──
    if a_keys and b_keys:
        common = a_keys & b_keys
        only_a = a_keys - b_keys
        only_b = b_keys - a_keys

        # A有B没有
        for k in sorted(only_a):
            d = k[0] if isinstance(k, tuple) else k
            if _is_trading_day(d):
                result["missing_in_b"].append(d)

        # B有A没有
        for k in sorted(only_b):
            d = k[0] if isinstance(k, tuple) else k
            if _is_trading_day(d):
                result["missing_in_a"].append(d)

        # 共有部分比对（预排序一次，O(n) 遍历）
        sorted_common = sorted(common)
        # 预计算前一根的 close，用于涨跌幅判断
        prev_close_a: Optional[float] = None
        prev_close_b: Optional[float] = None

        merged = []
        for k in sorted_common:
            ra = a_by_key[k]
            rb = b_by_key[k]

            quality_a = classify_bar(ra)
            quality_b = classify_bar(rb)

            close_a = _safe_float(ra.get("close"))
            close_b = _safe_float(rb.get("close"))
            diff = _price_diff_pct(close_a, close_b)

            chosen = ra  # 默认
            chosen_source = name_a

            # 选择更优的数据源
            if quality_a == "ok" and quality_b != "ok":
                chosen = ra
                chosen_source = name_a
                result["quality_issues"].append({
                    "key": str(k), "source": name_b, "quality": quality_b,
                })
            elif quality_b == "ok" and quality_a != "ok":
                chosen = rb
                chosen_source = name_b
                result["quality_issues"].append({
                    "key": str(k), "source": name_a, "quality": quality_a,
                })
            elif quality_a != "ok" and quality_b != "ok":
                chosen = rb  # 都有问题，以 B 为准
                chosen_source = name_b
                result["quality_issues"].append({
                    "key": str(k), "source": f"{name_a}+{name_b}",
                    "quality": f"{quality_a}/{quality_b}",
                })
            elif diff > tolerance:
                # 价格偏差大 → 用前一根涨跌幅判断是否复权差异
                is_real_error = True
                if prev_close_a is not None and prev_close_b is not None:
                    ret_a = _calc_return(prev_close_a, close_a)
                    ret_b = _calc_return(prev_close_b, close_b)
                    if abs(ret_a - ret_b) <= 0.005:
                        is_real_error = False  # 复权差异

                if is_real_error:
                    result["price_mismatch"].append({
                        "key": str(k),
                        f"{name_a}_close": close_a,
                        f"{name_b}_close": close_b,
                        "diff_pct": round(diff * 100, 2),
                    })
                    chosen = rb  # 以 B（BaoStock/AKShare）修正
                    chosen_source = name_b
                else:
                    chosen = rb  # 复权差异，以 B 为准
                    chosen_source = name_b
            else:
                chosen = ra  # 价格接近，取 A
                chosen_source = name_a

            merged.append(chosen)
            prev_close_a = close_a
            prev_close_b = close_b

        # 仅 A 有的数据（B 缺失）→ 自校验后保留
        for k in sorted(only_a):
            ra = a_by_key[k]
            quality = classify_bar(ra)
            if quality == "bad":
                result["quality_issues"].append({
                    "key": str(k), "source": name_a, "quality": "bad",
                })
                continue
            merged.append(ra)

        # 仅 B 有的数据（A 缺失）→ 自校验后保留
        for k in sorted(only_b):
            rb = b_by_key[k]
            quality = classify_bar(rb)
            if quality == "bad":
                result["quality_issues"].append({
                    "key": str(k), "source": name_b, "quality": "bad",
                })
                continue
            merged.append(rb)

        result["merged_records"] = merged
        result["fix_from"] = f"{name_a}+{name_b}"

    # ── 场景2: 只有一个源有数据 → 自校验 ──
    elif a_keys and not b_keys:
        merged = []
        for k in sorted(a_keys):
            ra = a_by_key[k]
            quality = classify_bar(ra)
            if quality == "bad":
                result["quality_issues"].append({
                    "key": str(k), "source": name_a, "quality": "bad",
                })
                continue
            if quality != "ok":
                result["quality_issues"].append({
                    "key": str(k), "source": name_a, "quality": quality,
                })
            merged.append(ra)
        result["merged_records"] = merged
        result["fix_from"] = name_a

    elif b_keys and not a_keys:
        merged = []
        for k in sorted(b_keys):
            rb = b_by_key[k]
            quality = classify_bar(rb)
            if quality == "bad":
                result["quality_issues"].append({
                    "key": str(k), "source": name_b, "quality": "bad",
                })
                continue
            if quality != "ok":
                result["quality_issues"].append({
                    "key": str(k), "source": name_b, "quality": quality,
                })
            merged.append(rb)
        result["merged_records"] = merged
        result["fix_from"] = name_b

    # ── 场景3: 两个源都没有 ──
    else:
        result["fix_from"] = "none"

    return result


# ═══════════════════════════════════════════════════════
# DB 写入（修复: 保留 amount, 无法解析时间时记录日志）
# ═══════════════════════════════════════════════════════

def write_to_db(
    writer,
    market: str,
    code: str,
    timeframe: str,
    records: List[Dict[str, Any]],
    dry_run: bool = False,
) -> int:
    """将比对后的最终数据写入 db_market"""
    if not records or dry_run:
        return len(records)

    db_records = []
    skipped = 0
    for rec in records:
        ts = rec.get("time")
        if ts is None:
            dt_str = rec.get("date") or rec.get("datetime") or ""
            if not dt_str:
                skipped += 1
                continue
            try:
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                    try:
                        dt = datetime.strptime(str(dt_str).strip(), fmt)
                        break
                    except ValueError:
                        continue
                else:
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue
        elif isinstance(ts, datetime):
            dt = ts
        elif isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=TZ_SH)
        else:
            skipped += 1
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_SH)

        dt_naive = dt.replace(tzinfo=None)

        db_records.append({
            "symbol": code,
            "timeframe": timeframe,
            "time": dt_naive,
            "open": _safe_float(rec.get("open")),
            "high": _safe_float(rec.get("high")),
            "low": _safe_float(rec.get("low")),
            "close": _safe_float(rec.get("close")),
            "volume": _safe_float(rec.get("volume")),
        })

    if skipped > 0:
        logger.debug("[write_to_db] %s/%s: 跳过 %d 条无法解析时间的记录", code, timeframe, skipped)

    if not db_records:
        return 0

    try:
        result = writer.bulk_write(market, db_records, batch_size=5000)
        return result.get("inserted", 0)
    except Exception as e:
        logger.warning("写库失败 %s/%s/%s: %s", market, code, timeframe, e)
        return 0


# ═══════════════════════════════════════════════════════
# 连贯性检查（修复: 15m 日内检查 + 重复检测）
# ═══════════════════════════════════════════════════════

def check_continuity_simple(
    code: str,
    timeframe: str,
    records: List[Dict[str, Any]],
    today: str,
) -> List[Dict[str, Any]]:
    """简化版连贯性检查，返回断裂列表"""
    if len(records) < 2:
        return []

    gaps = []

    if timeframe == "1D":
        dates = sorted(set(_ts_to_date(r.get("time") or r.get("date", "")) for r in records))
        for i in range(1, len(dates)):
            skipped = _trading_days_between(dates[i - 1], dates[i])
            if skipped > 0:
                gaps.append({
                    "symbol": code, "timeframe": "1D", "gap_type": "middle",
                    "start_date": _next_day(dates[i - 1]),
                    "end_date": _prev_day(dates[i]),
                    "skipped": skipped,
                })
        last_date = dates[-1]
        if last_date < today:
            trailing = _trading_days_between(last_date, today)
            if _is_trading_day(today):
                trailing += 1
            if trailing > 0:
                gaps.append({
                    "symbol": code, "timeframe": "1D", "gap_type": "tail",
                    "start_date": _next_day(last_date),
                    "end_date": today,
                    "skipped": trailing,
                })
    else:  # 15m
        # 按日期分组
        date_groups: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
        for r in records:
            ts = r.get("time")
            if isinstance(ts, datetime):
                dt = ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
                d = dt.strftime("%Y-%m-%d")
                date_groups[d].add((dt.hour, dt.minute))

        sorted_dates = sorted(date_groups.keys())

        # 跨天断裂
        for i in range(1, len(sorted_dates)):
            skipped = _trading_days_between(sorted_dates[i - 1], sorted_dates[i])
            if skipped > 0:
                gaps.append({
                    "symbol": code, "timeframe": "15m", "gap_type": "middle",
                    "start_date": _next_day(sorted_dates[i - 1]),
                    "end_date": _prev_day(sorted_dates[i]),
                    "skipped": skipped,
                })

        # 日内缺失检查
        for d in sorted_dates:
            bars = date_groups[d]
            missing_bars = _BAR_SET_15M - bars
            if missing_bars:
                # 只报告连续缺失 > 1 根的情况（单根缺失可能是数据源差异）
                sorted_missing = sorted(missing_bars)
                # 检查是否在交易时间内（排除非交易日的 bar）
                if _is_trading_day(d) and len(sorted_missing) >= 2:
                    gaps.append({
                        "symbol": code, "timeframe": "15m", "gap_type": "intraday",
                        "start_date": d, "end_date": d,
                        "missing_bars": len(sorted_missing),
                        "detail": f"{sorted_missing[0][0]:02d}:{sorted_missing[0][1]:02d}~"
                                  f"{sorted_missing[-1][0]:02d}:{sorted_missing[-1][1]:02d}",
                    })

        # 尾部
        if sorted_dates:
            last_date = sorted_dates[-1]
            if last_date < today:
                trailing = _trading_days_between(last_date, today)
                if _is_trading_day(today):
                    trailing += 1
                if trailing > 0:
                    gaps.append({
                        "symbol": code, "timeframe": "15m", "gap_type": "tail",
                        "start_date": _next_day(last_date),
                        "end_date": today,
                        "skipped": trailing,
                    })

    return gaps


# ═══════════════════════════════════════════════════════
# 子进程工作函数
# ═══════════════════════════════════════════════════════

def _worker_init():
    import signal as _sig
    _sig.signal(_sig.SIGINT, _sig.SIG_IGN)


def _worker_batch(args: Tuple) -> Dict[str, Any]:
    """
    子进程入口: 处理一批股票的双源下载 + 比对 + 写库。

    TDX: 批量复用连接。
    BaoStock: 进程级单例连接（非线程安全，但此处单线程使用）。
    AKShare: 无状态 HTTP 调用。
    """
    (stocks, worker_id, timeframe, start_date, end_date,
     market, tolerance, dry_run, today) = args

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
    stats = {
        "total": 0, "dual_ok": 0, "single_source": 0, "no_data": 0,
        "written": 0, "price_mismatch": 0, "quality_issues": 0, "gaps": 0,
    }

    # ── TDX 批量下载（复用连接） ──
    tdx_data: Dict[str, List[Dict[str, Any]]] = {}
    try:
        tdx_data = batch_download_tdx(stocks, timeframe, start_date, end_date, worker_id)
    except Exception as e:
        logger.warning("[TDX Worker-%d] 批量下载失败: %s", worker_id, e)

    # ── BaoStock/AKShare 下载 ──
    other_data: Dict[str, List[Dict[str, Any]]] = {}
    if timeframe == "1D":
        # BaoStock: 进程级单例，串行下载
        try:
            other_data = batch_download_baostock(stocks, start_date, end_date, worker_id)
        except Exception as e:
            logger.warning("[BaoStock Worker-%d] 批量下载失败: %s", worker_id, e)
    else:
        # AKShare: HTTP 无状态，可并发
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for market_code, code, name in stocks:
                f = executor.submit(download_akshare_15m, market_code, code, start_date, end_date, worker_id)
                futures[f] = code
            for f in futures:
                code = futures[f]
                try:
                    other_data[code] = f.result(timeout=180)
                except Exception as e:
                    logger.debug("[AKShare] %s 失败: %s", code, e)
                    other_data[code] = []

    name_a = "TDX"
    name_b = "BaoStock" if timeframe == "1D" else "AKShare"

    # ── 逐只比对 + 写库 ──
    for market_code, code, name in stocks:
        stats["total"] += 1
        try:
            recs_a = tdx_data.get(code, [])
            recs_b = other_data.get(code, [])

            # 比对 & 校验
            cmp = compare_and_verify(
                code, timeframe, recs_a, recs_b, name_a, name_b, tolerance
            )

            # 统计
            if recs_a and recs_b:
                stats["dual_ok"] += 1
            elif recs_a or recs_b:
                stats["single_source"] += 1
            else:
                stats["no_data"] += 1

            stats["price_mismatch"] += len(cmp["price_mismatch"])
            stats["quality_issues"] += len(cmp["quality_issues"])

            # 写库
            if cmp["merged_records"]:
                n = write_to_db(writer, market, code, timeframe, cmp["merged_records"], dry_run)
                stats["written"] += n

                # 连贯性检查
                gaps = check_continuity_simple(code, timeframe, cmp["merged_records"], today)
                stats["gaps"] += len(gaps)

            results.append({
                "code": code, "name": name,
                "source_a_count": cmp["source_a_count"],
                "source_b_count": cmp["source_b_count"],
                "merged_count": len(cmp["merged_records"]),
                "fix_from": cmp["fix_from"],
                "price_mismatch": len(cmp["price_mismatch"]),
                "quality_issues": len(cmp["quality_issues"]),
            })

        except Exception as e:
            errors.append((code, f"{type(e).__name__}: {e}"))

    # 清理 BaoStock 连接
    if timeframe == "1D":
        _bs_logout()

    try:
        _dbm._manager.close_all_pools()
    except Exception:
        pass

    return {"results": results, "stats": stats, "errors": errors}


# ═══════════════════════════════════════════════════════
# 报告导出
# ═══════════════════════════════════════════════════════

def export_csv(all_results: List[Dict[str, Any]], path: str):
    """导出比对报告到 CSV"""
    if not all_results:
        print("无数据，跳过 CSV")
        return

    fields = ["code", "name", "source_a_count", "source_b_count",
              "merged_count", "fix_from", "price_mismatch", "quality_issues"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_results:
            w.writerow({k: r.get(k, "") for k in fields})

    print(f"✅ CSV 报告: {path}（{len(all_results)} 条）")


# ═══════════════════════════════════════════════════════
# 中断信号
# ═══════════════════════════════════════════════════════

_INTERRUPTED = False


def _signal_handler(signum, frame):
    global _INTERRUPTED
    if _INTERRUPTED:
        print("\n⚡ 强制退出")
        sys.exit(1)
    _INTERRUPTED = True
    print("\n⚠️  收到中断信号，正在保存已处理的结果...")


# ═══════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════

def main():
    global _INTERRUPTED

    parser = argparse.ArgumentParser(
        description="双源并发下载 + 实时比对 + 校验修复 + 写库",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-T", "--type",
        choices=["1D", "15m"], default="1D",
        help="数据类型: 1D(日线) / 15m(15分钟线)")
    parser.add_argument("--symbol", help="只处理指定股票")
    parser.add_argument("--market", default="CNStock", help="市场（默认 CNStock）")
    parser.add_argument("-s", "--start", default="", help="起始日期 (如 2023-01-01)")
    parser.add_argument("-e", "--end", default="", help="截止日期 (如 2026-05-01)")
    parser.add_argument("-y", "--years", type=int, default=5, help="年限 (默认5)")
    parser.add_argument("-w", "--workers", type=int, default=5, help="进程数 (默认5)")
    parser.add_argument("--tolerance", type=float, default=0.01,
        help="价格容差 (默认 0.01 = 1%%)")
    parser.add_argument("--dry-run", action="store_true",
        help="只比对不写库")
    parser.add_argument("--csv", help="导出 CSV 报告路径")
    parser.add_argument("--batch-size", type=int, default=50,
        help="每个子进程一次处理的股票数 (默认 50)")

    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # 日期范围
    end_date = args.end or datetime.now().strftime('%Y-%m-%d')
    if args.start:
        start_date = args.start
    else:
        start_date = (datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=args.years * 365)).strftime('%Y-%m-%d')

    from app.utils.db_market import get_market_kline_writer, get_market_db_manager
    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    market = args.market

    # 确保 DB 存在
    if not args.dry_run:
        if not mgr.market_db_exists(market):
            mgr.ensure_market_db(market)

    # 获取股票列表（统一格式: [(market_code, code, name), ...]）
    if args.symbol:
        mc = _lookup_market_code(args.symbol)
        stocks = [(mc, args.symbol, args.symbol)]
        print(f"\n[1/4] 单只模式: {args.symbol} (market={mc})")
    else:
        print("\n[1/4] 获取A股列表 (通达信)...")
        stocks = get_stock_list_tdx()
        print(f"  共 {len(stocks)} 只A股")

    total = len(stocks)
    n_workers = min(args.workers, 16, total)
    if args.symbol:
        n_workers = 1
    batch_size = max(1, min(args.batch_size, total // n_workers + 1))

    source_label = "TDX+BaoStock" if args.type == "1D" else "TDX+AKShare"
    print(f"""
╔═══════════════════════════════════════════════════════╗
║  🔄 双源并发下载 + 实时比对 + 校验修复 + 写库          ║
╠═══════════════════════════════════════════════════════╣
║  类型: {args.type:<8}  数据源: {source_label:<20}       ║
║  日期: {start_date} → {end_date}                     ║
║  股票: {total} 只  进程: {n_workers}  容差: {args.tolerance*100:.1f}%     ║
║  模式: {'dry-run（只比对）' if args.dry_run else '比对 + 写库':<40}║
╚═══════════════════════════════════════════════════════╝
""")

    _build_trading_day_cache(market)
    assert _TRADING_DAY_SET is not None, "交易日缓存必须在 fork 前构建完成"

    today = datetime.now(TZ_SH).strftime("%Y-%m-%d")

    # 分批
    batches = [stocks[i:i + batch_size] for i in range(0, total, batch_size)]

    all_results: List[Dict[str, Any]] = []
    all_errors: List[Tuple[str, str]] = []
    agg_stats = {
        "total": 0, "dual_ok": 0, "single_source": 0, "no_data": 0,
        "written": 0, "price_mismatch": 0, "quality_issues": 0, "gaps": 0,
    }

    print(f"\n[2/4] 双源并发下载 + 比对...")

    t0 = time.time()

    if n_workers <= 1:
        # 单进程模式
        for i, batch in enumerate(batches):
            if _INTERRUPTED:
                break
            batch_args = (
                batch, 0, args.type, start_date, end_date,
                market, args.tolerance, args.dry_run, today
            )
            batch_result = _worker_batch(batch_args)
            all_results.extend(batch_result["results"])
            all_errors.extend(batch_result["errors"])
            for k in agg_stats:
                agg_stats[k] += batch_result["stats"].get(k, 0)
            processed = min((i + 1) * batch_size, total)
            print(f"\r  [{processed}/{total}] "
                  f"双源={agg_stats['dual_ok']} 单源={agg_stats['single_source']} "
                  f"无数据={agg_stats['no_data']} 写入={agg_stats['written']} "
                  f"偏差={agg_stats['price_mismatch']} 质量={agg_stats['quality_issues']}",
                  end='', flush=True)
        print()
    else:
        # 多进程模式
        task_args = [
            (batch, i % n_workers, args.type, start_date, end_date,
             market, args.tolerance, args.dry_run, today)
            for i, batch in enumerate(batches)
        ]

        pool = mp.Pool(n_workers, initializer=_worker_init)
        try:
            results_iter = pool.imap_unordered(_worker_batch, task_args, chunksize=1)
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
                    elapsed_so_far = time.time() - t0
                    print(f"\r  [{processed}/{total}] "
                          f"双源={agg_stats['dual_ok']} 单源={agg_stats['single_source']} "
                          f"无数据={agg_stats['no_data']} 写入={agg_stats['written']} "
                          f"偏差={agg_stats['price_mismatch']} 质量={agg_stats['quality_issues']} "
                          f"耗时={elapsed_so_far:.0f}s",
                          end='', flush=True)
            print()
        except KeyboardInterrupt:
            _INTERRUPTED = True
        finally:
            pool.terminate()
            pool.join()

    elapsed = time.time() - t0

    # ── 汇总 ──
    print(f"\n[3/4] 汇总统计")
    status = "中断" if _INTERRUPTED else "完成"
    print(f"处理{status}: {agg_stats['total']}/{total} 只  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  双源成功: {agg_stats['dual_ok']}")
    print(f"  单源回退: {agg_stats['single_source']}")
    print(f"  无数据:   {agg_stats['no_data']}")
    print(f"  写入行数: {agg_stats['written']:,}")
    print(f"  价格偏差: {agg_stats['price_mismatch']} 条")
    print(f"  质量问题: {agg_stats['quality_issues']} 条")
    print(f"  连贯断裂: {agg_stats['gaps']} 条")
    print(f"  查询错误: {len(all_errors)} 只")

    # 价格偏差 top 10
    mismatch_top = sorted(
        [r for r in all_results if r.get("price_mismatch", 0) > 0],
        key=lambda r: r["price_mismatch"],
        reverse=True,
    )
    if mismatch_top:
        print(f"\n价格偏差最多的 10 只:")
        for r in mismatch_top[:10]:
            print(f"  {r['code']:>8} | {r['name']:<8} | "
                  f"A={r['source_a_count']} B={r['source_b_count']} → {r['merged_count']} | "
                  f"偏差={r['price_mismatch']} 质量={r['quality_issues']}")

    # 无数据 top 10
    no_data_list = [r for r in all_results if r["merged_count"] == 0]
    if no_data_list:
        print(f"\n无数据股票 (前 10 只):")
        for r in no_data_list[:10]:
            print(f"  {r['code']:>8} | {r['name']:<8} | "
                  f"A={r['source_a_count']} B={r['source_b_count']}")

    # 错误
    if all_errors:
        print(f"\n⚠️  查询失败（前 10 只）:")
        for code, msg in all_errors[:10]:
            print(f"  {code}: {msg}")
        if len(all_errors) > 10:
            print(f"  ... 还有 {len(all_errors) - 10} 只")

    # CSV 导出
    print(f"\n[4/4] 导出报告")
    if args.csv:
        export_csv(all_results, args.csv)
    else:
        print("  未指定 --csv，跳过")

    print(f"\n{'='*60}")
    print(f"  ✅ 全部完成!")
    print(f"{'='*60}")

    mgr.close_all_pools()
    return 1 if (all_errors or _INTERRUPTED) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出。")
        sys.exit(1)
