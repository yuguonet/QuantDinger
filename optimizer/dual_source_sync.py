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
#   python dual_source_sync.py -T 1D --resume                 # 断点续传
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
import json
import math
import time
import signal
import logging
import argparse
import multiprocessing as mp
from bisect import bisect_left, bisect_right
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

# 通达信服务器（可通过环境变量 TDX_SERVERS 覆盖，格式: ip1:port1,ip2:port2,...）
_DEFAULT_TDX_SERVERS = [
    ('218.75.126.9', 7709),
    ('115.238.56.198', 7709),
    ('124.160.88.183', 7709),
    ('60.12.136.250', 7709),
    ('218.108.98.244', 7709),
    ('218.108.47.69', 7709),
    ('180.153.39.51', 7709),
]


def _load_tdx_servers() -> List[Tuple[str, int]]:
    """加载 TDX 服务器列表，支持环境变量覆盖"""
    env = os.environ.get("TDX_SERVERS", "").strip()
    if env:
        servers = []
        for item in env.split(","):
            item = item.strip()
            if ":" in item:
                ip, port = item.rsplit(":", 1)
                try:
                    servers.append((ip.strip(), int(port)))
                except ValueError:
                    pass
        if servers:
            return servers
    return _DEFAULT_TDX_SERVERS


TDX_SERVERS = _load_tdx_servers()

# 15m 标准 bar 时间（16 根，不含 9:30 开盘集合竞价）
_BAR_TIMES_15M = [
    (9, 45), (10, 0), (10, 15), (10, 30), (10, 45),
    (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15),
    (14, 30), (14, 45), (15, 0),
]
_BAR_SET_15M: Set[Tuple[int, int]] = set(_BAR_TIMES_15M)

# 交易日历：直接调用 backend_api_python/app/utils/trading_calendar.py
# 内部用 pickle 缓存，内存中有 frozenset，O(1) 判断 + bisect 计算间隔
_TRADING_DAYS_SORTED: List[str] = []  # 排序后的交易日列表，用于 bisect
_TRADING_DAY_SET: Set[str] = set()    # O(1) 查找集合


def _init_trading_calendar(silent: bool = False):
    """初始化交易日历（从 trading_calendar 模块加载）"""
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


def _trading_days_between(d1: str, d2: str) -> int:
    """用 bisect 计算 d1 和 d2 之间的交易日数量（不含两端），O(log n)"""
    if d1 >= d2:
        return 0
    if not _TRADING_DAY_SET:
        _init_trading_calendar(silent=True)
    # 从 d1 的下一天开始算（不含 d1 本身）
    d1_next = (datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    left = bisect_left(_TRADING_DAYS_SORTED, d1_next)
    # 到 d2 为止（不含 d2 本身）
    right = bisect_left(_TRADING_DAYS_SORTED, d2)
    return max(0, right - left)


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
        orig_dt = dt
        dt = normalize_15m_time(dt)
        if dt is None:
            return None
        # 标记：午休时段(11:30~13:00)归一化到 11:30 的记录
        # _build_key_index 会用此标记决定是否覆盖真正的 11:30 bar
        result = {**rec, "time": dt}
        if orig_dt != dt:
            result["_normalized_from_lunch"] = True
        return result

    return {**rec, "time": dt}


# ---------------------------------------------------------------------------
# 数据质量分类
# ---------------------------------------------------------------------------

def classify_bar(bar: Dict[str, Any]) -> str:
    """
    对单根 K 线做质量分类。
    Returns: "ok" / "bad" / "suspended" / "incomplete" / "bad_vol"
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
    # volume 为负或异常大（超过 1 亿手 = 1e10 股）视为脏数据
    if v < 0:
        return "bad_vol"
    if v > 1e10:
        return "bad_vol"
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


def _patch_pytdx_security_list():
    """
    Monkey-patch pytdx GetSecurityList.parseResponse，修复 offset>=8000 时解析崩溃的 bug。
    仅在模块加载时执行一次，后续调用直接跳过。
    """
    if getattr(_patch_pytdx_security_list, '_done', False):
        return
    try:
        from pytdx.parser.get_security_list import GetSecurityList
        import struct as _struct
        from pytdx.helper import get_volume
    except ImportError:
        return  # pytdx 未安装，跳过 patch

    _orig_parse = GetSecurityList.parseResponse

    def _robust_parse(self, body_buf):
        """优先走原始解析，失败时用 fallback 手动拆包"""
        result = None
        try:
            result = _orig_parse(self, body_buf)
        except Exception:
            pass
        if result and len(result) > 0:
            return result

        # Fallback 1: 标准 29 字节结构
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
            if stocks:
                return stocks
        except Exception:
            pass

        # Fallback 2: 暴力扫描——逐字节找可识别的股票代码
        try:
            if len(body_buf) < 2:
                return None
            num2, = _struct.unpack("<H", body_buf[:2])
            stocks2 = []
            scan = 2
            while scan < len(body_buf) - 29 and len(stocks2) < num2:
                chunk = body_buf[scan:scan + 6]
                code_try = chunk.decode("ascii", errors="ignore").strip('\x00').strip()
                if code_try.isdigit() and len(code_try) >= 4:
                    name_chunk = body_buf[scan + 8:scan + 24]
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
            if stocks2:
                return stocks2
        except Exception:
            pass

        return None

    GetSecurityList.parseResponse = _robust_parse
    _patch_pytdx_security_list._done = True


# 模块加载时立即 patch（只执行一次，避免每次函数调用时重复 import）
_patch_pytdx_security_list()


def _fetch_security_list(api, market, offset):
    """获取证券列表（已 patch pytdx 解析 bug）"""
    return api.get_security_list(market, offset)


def get_stock_list_from_db() -> List[Tuple[int, str, str]]:
    """从 basicinfo_db 读取全市场 A 股列表（优先），TDX 兜底"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        stocks = db.get_all_stocks(status="active")
        if stocks:
            result = []
            for s in stocks:
                code = s["symbol"]
                name = s["name"]
                market_cn = s.get("market_cn", "")
                # market_code: 0=深圳, 1=上海（兼容 TDX 格式）
                market_code = 1 if market_cn == "SH" else 0
                result.append((market_code, code, name))
            print(f"  📦 从 basicinfo_db 读取: {len(result)} 只")
            return result
    except Exception as e:
        logger.warning("basicinfo_db 读取失败，回退到 TDX: %s", e)

    # TDX 兜底
    print("  ⚠️  basicinfo_db 无数据，回退到通达信获取列表...")
    return get_stock_list_tdx()


def get_stock_list_tdx() -> List[Tuple[int, str, str]]:
    """从通达信获取全部A股代码（basicinfo_db 的兜底方案）"""
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
            except (ConnectionError, OSError, TimeoutError) as conn_err:
                # 连接断了，重连一次，且把新 api 赋回同一变量
                logger.debug("[TDX] %s 连接断开: %s，尝试重连...", code, conn_err)
                try:
                    api.disconnect()
                except Exception:
                    pass
                try:
                    api = _connect_tdx(worker_id)  # 赋回 api，后续循环继续用新连接
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
            # 第一次请求就无数据 → 正常（可能是新股/停牌/代码错误）
            if offset == 0:
                break
            # 非首次请求无数据 → 可能是连接断了，抛异常触发重连
            raise ConnectionError(f"TDX {code} offset={offset} 返回空，疑似连接断开")
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
            if i == 0:
                break
            raise ConnectionError(f"TDX {code} 15m offset={i * 800} 返回空，疑似连接断开")
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
_bs_pid: int = 0  # 记录登录时的 pid，用于检测 fork


def _ensure_bs_login():
    """确保 BaoStock 已登录（进程级单例，非线程安全）。
    fork/spawn 后自动检测 pid 变化，强制重新登录。
    """
    global _bs_logged_in, _bs_pid
    current_pid = os.getpid()
    # fork 后 pid 变化 → 旧连接已失效，强制重置
    if _bs_logged_in and _bs_pid != current_pid:
        _bs_logged_in = False
        _bs_pid = 0
    if _bs_logged_in:
        return
    import baostock as bs
    # 不要设 socket.setdefaulttimeout —— 会干扰 BaoStock 内部 socket
    # 不要在新进程中调 bs.logout() —— Windows spawn 模式下会搞坏连接状态
    lg = bs.login()
    if lg.error_code != '0':
        raise ConnectionError(f"BaoStock 登录失败: {lg.error_msg}")
    _bs_logged_in = True
    _bs_pid = current_pid


def _bs_logout():
    """登出 BaoStock"""
    global _bs_logged_in, _bs_pid
    if not _bs_logged_in:
        return
    try:
        import baostock as bs
        bs.logout()
    except Exception:
        pass
    _bs_logged_in = False
    _bs_pid = 0


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
    带单股超时保护（60s），防止单个卡住拖慢整个 worker。
    """
    bs = _import_baostock()

    # 每次查询前检查连接，fork 后的首次调用会自动重新登录
    try:
        _ensure_bs_login()
    except Exception as e:
        logger.warning("[BaoStock] Worker-%d 登录失败: %s", worker_id, e)
        return []

    bs_code = f"{'sh' if market_code == 1 else 'sz'}.{code}"

    for retry in range(3):
        try:
            # 带超时的查询：用线程包装
            result_holder: Dict[str, Any] = {}
            def _query():
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start_date,
                        end_date=end_date,
                        frequency="d",
                        adjustflag="1",
                    )
                    result_holder["rs"] = rs
                except Exception as e:
                    result_holder["error"] = e

            import threading as _th
            t = _th.Thread(target=_query, daemon=True)
            t.start()
            t.join(timeout=60)  # 单股最多等 60s

            if t.is_alive():
                # 超时 → 重连
                logger.warning("[BaoStock] %s 查询超时(60s)，重连...", code)
                _bs_logout()
                try:
                    _ensure_bs_login()
                except Exception:
                    pass
                continue

            if "error" in result_holder:
                raise result_holder["error"]

            rs = result_holder.get("rs")
            if rs is None:
                return []
            if rs.error_code != '0':
                # "you don't login" → 强制重新登录
                if 'login' in str(rs.error_msg).lower() or 'login' in str(getattr(rs, 'error_code', '')).lower():
                    logger.warning("[BaoStock] %s 会话失效，重新登录...", code)
                    _bs_logout()
                    try:
                        _ensure_bs_login()
                    except Exception:
                        pass
                    continue
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
    """构建 key → record 的索引，自动去重。

    优先级规则（15m）：
      - 真正的 11:30 bar 优先于午休归一化过来的 13:00 bar
      - 标记了 _normalized_from_lunch 的记录不覆盖未标记的同 key 记录
    """
    key_fn = _key_1d if timeframe == "1D" else _key_15m
    index: Dict = {}
    for r in records:
        k = key_fn(r)
        if not k or k == ("", 0, 0) or k == "":
            continue
        if timeframe == "15m" and r.get("_normalized_from_lunch"):
            # 午休归一化的记录：只在没有真正 11:30 时才写入
            if k not in index:
                index[k] = r
            # 已有记录则跳过（不覆盖真正的 11:30 bar）
        else:
            # 正常记录或 1D：直接写入，后出现的覆盖先出现的
            index[k] = r
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
                # 价格偏差大 → 用前一根涨跌幅判断是真实错误还是复权差异
                is_real_error = True
                if prev_close_a is not None and prev_close_b is not None:
                    ret_a = _calc_return(prev_close_a, close_a)
                    ret_b = _calc_return(prev_close_b, close_b)
                    if abs(ret_a - ret_b) <= 0.005:
                        is_real_error = False  # 复权差异：涨跌幅一致

                if is_real_error:
                    # 真实价格错误 → 记录并以质量更优者为准
                    result["price_mismatch"].append({
                        "key": str(k),
                        f"{name_a}_close": close_a,
                        f"{name_b}_close": close_b,
                        "diff_pct": round(diff * 100, 2),
                    })
                    # 两个源都有数据但价格不一致，优先选质量 ok 的
                    if quality_a == "ok" and quality_b != "ok":
                        chosen = ra
                        chosen_source = name_a
                    elif quality_b == "ok" and quality_a != "ok":
                        chosen = rb
                        chosen_source = name_b
                    else:
                        # 都 ok 或都有问题 → 取均值写入，减少单源偏差
                        merged_rec = {k: v for k, v in rb.items() if not k.startswith("_")}
                        merged_rec["open"] = round((_safe_float(ra.get("open")) + _safe_float(rb.get("open"))) / 2, 4)
                        merged_rec["high"] = round(max(_safe_float(ra.get("high")), _safe_float(rb.get("high"))), 4)
                        merged_rec["low"] = round(min(_safe_float(ra.get("low")), _safe_float(rb.get("low"))), 4)
                        merged_rec["close"] = round((close_a + close_b) / 2, 4)
                        merged_rec["volume"] = round((_safe_float(ra.get("volume")) + _safe_float(rb.get("volume"))) / 2, 2)
                        chosen = merged_rec
                        chosen_source = f"{name_a}+{name_b}(merged)"
                else:
                    # 复权差异（涨跌幅一致但绝对价格不同）→ 取均值平滑
                    merged_rec = {k: v for k, v in rb.items() if not k.startswith("_")}
                    merged_rec["open"] = round((_safe_float(ra.get("open")) + _safe_float(rb.get("open"))) / 2, 4)
                    merged_rec["high"] = round(max(_safe_float(ra.get("high")), _safe_float(rb.get("high"))), 4)
                    merged_rec["low"] = round(min(_safe_float(ra.get("low")), _safe_float(rb.get("low"))), 4)
                    merged_rec["close"] = round((close_a + close_b) / 2, 4)
                    merged_rec["volume"] = round((_safe_float(ra.get("volume")) + _safe_float(rb.get("volume"))) / 2, 2)
                    chosen = merged_rec
                    chosen_source = f"{name_a}+{name_b}(adj_merged)"
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
            if quality in ("bad", "bad_vol"):
                result["quality_issues"].append({
                    "key": str(k), "source": name_a, "quality": "bad",
                })
                continue
            merged.append(ra)

        # 仅 B 有的数据（A 缺失）→ 自校验后保留
        for k in sorted(only_b):
            rb = b_by_key[k]
            quality = classify_bar(rb)
            if quality in ("bad", "bad_vol"):
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
            if quality in ("bad", "bad_vol"):
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
            if quality in ("bad", "bad_vol"):
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
    """将比对后的最终数据写入 db_market。

    注意: DB schema 仅有 (symbol, time, open, high, low, close, volume) 六列，
    不含 amount（成交额）。下载时获取的 amount 仅用于本地校验，不入库。
    如需 amount，需先 ALTER TABLE 添加该列。
    """
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
            # 如果今天还在交易时间内（未收盘），不把今天算作尾部断裂
            now_dt = datetime.now(TZ_SH)
            if _is_trading_day(today) and now_dt.hour < 15:
                # 今天未收盘，尾部只算到昨天
                if last_date < _prev_day(today):
                    trailing = _trading_days_between(last_date, _prev_day(today))
                    if trailing > 0:
                        gaps.append({
                            "symbol": code, "timeframe": "1D", "gap_type": "tail",
                            "start_date": _next_day(last_date),
                            "end_date": _prev_day(today),
                            "skipped": trailing,
                        })
            else:
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

        # 尾部（考虑当前时间：交易时间内不报告当天缺失）
        if sorted_dates:
            last_date = sorted_dates[-1]
            now_dt = datetime.now(TZ_SH)
            effective_today = today
            # 如果今天还在交易时间内（未收盘），尾部只算到昨天
            if _is_trading_day(today) and now_dt.hour < 15:
                effective_today = _prev_day(today)
            if last_date < effective_today:
                trailing = _trading_days_between(last_date, effective_today)
                if _is_trading_day(effective_today):
                    trailing += 1
                if trailing > 0:
                    gaps.append({
                        "symbol": code, "timeframe": "15m", "gap_type": "tail",
                        "start_date": _next_day(last_date),
                        "end_date": effective_today,
                        "skipped": trailing,
                    })

    return gaps


# ═══════════════════════════════════════════════════════
# 子进程工作函数
# ═══════════════════════════════════════════════════════

def _worker_init():
    import signal as _sig
    # 忽略 SIGINT，让主进程统一处理 Ctrl+C
    _sig.signal(_sig.SIGINT, _sig.SIG_IGN)
    # SIGTERM 保持默认（终止进程），这样 pool.terminate() 能生效
    # 不要设 socket.setdefaulttimeout —— TDX 有自己的 time_out 参数，
    # BaoStock 内部管理自己的 socket，全局 timeout 会干扰它

    # fork 后 BaoStock 继承了父进程的 TCP 连接，已失效，强制重置
    global _bs_logged_in, _bs_pid
    _bs_logged_in = False
    _bs_pid = 0


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

    global _TRADING_DAY_SET
    if not _TRADING_DAY_SET:
        _init_trading_calendar(silent=True)

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
# 数据清洗（下载完成后执行）
# ═══════════════════════════════════════════════════════

def post_process(
    market: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    tolerance: float = 0.01,
) -> Dict[str, int]:
    """
    下载完成后的数据清洗步骤：
      1. 删除非交易时间的 bar（非交易日的日线、非标准时间的 15m bar）
      2. 删除最近 1 年无数据的股票（可能已退市）
      3. 对有 0 值 OHLC 的股票，重新双源读取，取全非零版本
    """
    from app.utils.db_market import get_market_kline_writer, get_market_db_manager
    mgr = get_market_db_manager()
    writer = get_market_kline_writer()
    pool = mgr._get_pool(market)

    stats = {
        "non_trading_deleted": 0,
        "stale_deleted": 0,
        "zero_refetched": 0,
        "zero_fixed": 0,
    }

    _init_trading_calendar(silent=True)

    # ── 发现所有分区表 ──
    with pool.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
              AND table_name LIKE %s
            ORDER BY table_name
        """, (f'kline_{timeframe}_%',))
        tables = [r[0] for r in cur.fetchall()]

    if not tables:
        print("  ⚠️  未找到分区表，跳过清洗")
        return stats

    print(f"\n  数据清洗（{len(tables)} 张表）...")

    # ── Step 1: 删除非交易时间的 bar ──
    print("  [1/3] 清除非交易时间 bar...")
    for tbl in tables:
        with pool.connection() as conn:
            cur = conn.cursor()
            if timeframe == "1D":
                # 日线：删除非交易日的 bar
                # 取表中所有不重复日期，与交易日历比对
                cur.execute(f"""
                    SELECT DISTINCT TO_CHAR(time, 'YYYY-MM-DD') AS d
                    FROM "{tbl}"
                """)
                all_dates = {r[0] for r in cur.fetchall()}
                non_trading = sorted(all_dates - _TRADING_DAY_SET)
                if non_trading:
                    # 批量删除
                    cur.execute(f"""
                        DELETE FROM "{tbl}"
                        WHERE TO_CHAR(time, 'YYYY-MM-DD') = ANY(%s)
                    """, (non_trading,))
                    stats["non_trading_deleted"] += cur.rowcount
                    conn.commit()
            else:
                # 15m：删除非标准 bar 时间的记录
                # 标准时间集合已在 _BAR_SET_15M
                # 用 EXTRACT 提取 hour/minute，与标准时间比对
                cur.execute(f"""
                    SELECT DISTINCT
                        EXTRACT(HOUR FROM time)::int AS h,
                        EXTRACT(MINUTE FROM time)::int AS m
                    FROM "{tbl}"
                """)
                all_hm = {(r[0], r[1]) for r in cur.fetchall()}
                non_standard = sorted(all_hm - _BAR_SET_15M)
                if non_standard:
                    # 构建 OR 条件
                    conditions = []
                    params = []
                    for h, m in non_standard:
                        conditions.append(
                            f"(EXTRACT(HOUR FROM time) = %s AND EXTRACT(MINUTE FROM time) = %s)"
                        )
                        params.extend([h, m])
                    where = " OR ".join(conditions)
                    cur.execute(f'DELETE FROM "{tbl}" WHERE {where}', params)
                    stats["non_trading_deleted"] += cur.rowcount
                    conn.commit()
            cur.close()

    print(f"    删除非交易 bar: {stats['non_trading_deleted']} 条")

    # ── Step 2: 删除最近 1 年无数据的股票 ──
    print("  [2/3] 清理长期无数据股票...")
    one_year_ago = (datetime.now(TZ_SH) - timedelta(days=365)).strftime("%Y-%m-%d")
    stale_symbols = set()

    for tbl in tables:
        with pool.cursor() as cur:
            # 找出该表中最新数据在 1 年前之前的 symbol
            cur.execute(f"""
                SELECT symbol, MAX(time) AS last_time
                FROM "{tbl}"
                GROUP BY symbol
                HAVING MAX(time) < %s
            """, (one_year_ago,))
            for row in cur.fetchall():
                stale_symbols.add(row[0])

    if stale_symbols:
        for tbl in tables:
            with pool.connection() as conn:
                cur = conn.cursor()
                cur.execute(f"""
                    DELETE FROM "{tbl}" WHERE symbol = ANY(%s)
                """, (sorted(stale_symbols),))
                stats["stale_deleted"] += cur.rowcount
                conn.commit()
                cur.close()
        print(f"    清理 {len(stale_symbols)} 只过期股票，删除 {stats['stale_deleted']} 条")
    else:
        print("    无过期股票")

    # ── Step 3: 对有 0 值 OHLC 的股票重新双源读取 ──
    print("  [3/3] 修复 0 值 OHLC...")
    zero_symbols = set()

    for tbl in tables:
        with pool.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT symbol FROM "{tbl}"
                WHERE open = 0 OR high = 0 OR low = 0 OR close = 0
            """)
            for row in cur.fetchall():
                zero_symbols.add(row[0])

    if zero_symbols:
        print(f"    发现 {len(zero_symbols)} 只有 0 值的股票，重新读取...")
        zero_list = sorted(zero_symbols)

        # 获取 market_code 映射
        symbol_mc: Dict[str, int] = {}
        for mc, code, name in get_stock_list_from_db():
            if code in zero_symbols:
                symbol_mc[code] = mc

        name_a = "TDX"
        name_b = "BaoStock" if timeframe == "1D" else "AKShare"

        for code in zero_list:
            mc = symbol_mc.get(code, 0)
            try:
                # 重新双源下载
                if timeframe == "1D":
                    recs_a = batch_download_tdx([(mc, code, code)], "1D", start_date, end_date, 0).get(code, [])
                    recs_b = batch_download_baostock([(mc, code, code)], start_date, end_date, 0).get(code, [])
                else:
                    recs_a = batch_download_tdx([(mc, code, code)], "15m", start_date, end_date, 0).get(code, [])
                    recs_b = download_akshare_15m(mc, code, start_date, end_date, 0)

                if not recs_a and not recs_b:
                    continue

                # 比对
                cmp = compare_and_verify(code, timeframe, recs_a, recs_b, name_a, name_b, tolerance)
                merged = cmp["merged_records"]

                if not merged:
                    continue

                # 检查合并后是否还有 0 值
                has_zero = False
                for r in merged:
                    o = _safe_float(r.get("open"))
                    h = _safe_float(r.get("high"))
                    l = _safe_float(r.get("low"))
                    c = _safe_float(r.get("close"))
                    if o == 0 or h == 0 or l == 0 or c == 0:
                        has_zero = True
                        break

                if not has_zero:
                    # 全非零 → 覆盖写入
                    n = write_to_db(writer, market, code, timeframe, merged)
                    stats["zero_fixed"] += n
                    stats["zero_refetched"] += 1
                else:
                    stats["zero_refetched"] += 1

            except Exception as e:
                logger.debug("[修复] %s 失败: %s", code, e)

        print(f"    重新读取 {stats['zero_refetched']} 只，修复写入 {stats['zero_fixed']} 条")
    else:
        print("    无 0 值问题")

    return stats


# ═══════════════════════════════════════════════════════
# 断点续传
# ═══════════════════════════════════════════════════════

def _checkpoint_path(timeframe: str, start_date: str, end_date: str) -> str:
    """生成 checkpoint 文件路径"""
    return os.path.join(
        PROJECT_ROOT, "optimizer",
        f".checkpoint_{timeframe}_{start_date}_{end_date}.json"
    )


def _load_checkpoint(path: str) -> Dict[str, Any]:
    """加载断点，返回已处理的 code 集合和累计统计"""
    if not os.path.isfile(path):
        return {"processed_codes": set(), "stats": {}, "results": [], "errors": []}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        data["processed_codes"] = set(data.get("processed_codes", []))
        return data
    except Exception as e:
        logger.warning("checkpoint 加载失败，从头开始: %s", e)
        return {"processed_codes": set(), "stats": {}, "results": [], "errors": []}


def _save_checkpoint(path: str, processed_codes: set, stats: dict,
                     results: list, errors: list):
    """保存断点（原子写入）"""
    data = {
        "processed_codes": sorted(processed_codes),
        "stats": stats,
        "results": results[-200:],  # 只保留最近 200 条，避免文件过大
        "errors": errors[-100:],
        "saved_at": datetime.now(TZ_SH).isoformat(),
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("checkpoint 保存失败: %s", e)


def _remove_checkpoint(path: str):
    """任务完成后清理 checkpoint"""
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


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
    parser.add_argument("--batch-size", type=int, default=50,
        help="每个子进程一次处理的股票数 (默认 50)")
    parser.add_argument("--resume", action="store_true",
        help="断点续传：跳过已处理的股票，从上次中断处继续")
    parser.add_argument("--checkpoint", default="",
        help="自定义 checkpoint 文件路径（默认自动生成）")

    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # signal.set_wakeup_fd 需要 socket（Windows 不支持 pipe fd）
    if sys.platform != 'win32':
        _wakeup_r, _wakeup_w = os.pipe()
        os.set_blocking(_wakeup_r, False)
        signal.set_wakeup_fd(_wakeup_w)

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

    if not args.dry_run:
        if not mgr.market_db_exists(market):
            mgr.ensure_market_db(market)

    # 获取股票列表
    if args.symbol:
        mc = _lookup_market_code(args.symbol)
        stocks = [(mc, args.symbol, args.symbol)]
        print(f"\n[1/5] 单只模式: {args.symbol} (market={mc})")
    else:
        print("\n[1/5] 获取A股列表...")
        stocks = get_stock_list_from_db()
        print(f"  共 {len(stocks)} 只A股")

    # ── 断点续传 ──
    ckpt_path = args.checkpoint or _checkpoint_path(args.type, start_date, end_date)
    processed_codes: set = set()
    if args.resume and not args.symbol:
        ckpt = _load_checkpoint(ckpt_path)
        processed_codes = ckpt["processed_codes"]
        if processed_codes:
            before = len(stocks)
            stocks = [s for s in stocks if s[1] not in processed_codes]
            print(f"  📂 断点续传: 已处理 {len(processed_codes)} 只，剩余 {len(stocks)} 只")
            if not stocks:
                print("  ✅ 所有股票已处理完毕，无需继续")
                mgr.close_all_pools()
                return 0

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
║  续传: {'是 (' + ckpt_path + ')' if args.resume else '否':<40}║
╚═══════════════════════════════════════════════════════╝
""")

    _init_trading_calendar()

    today = datetime.now(TZ_SH).strftime("%Y-%m-%d")

    # 分批
    batches = [stocks[i:i + batch_size] for i in range(0, total, batch_size)]

    all_results: List[Dict[str, Any]] = []
    all_errors: List[Tuple[str, str]] = []
    agg_stats = {
        "total": 0, "dual_ok": 0, "single_source": 0, "no_data": 0,
        "written": 0, "price_mismatch": 0, "quality_issues": 0, "gaps": 0,
    }

    print(f"\n[2/5] 双源并发下载 + 比对...")

    t0 = time.time()

    if n_workers <= 1:
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
            # 记录已处理的 codes
            for r in batch_result["results"]:
                processed_codes.add(r["code"])
            # 边下边保存 checkpoint
            if not args.symbol:
                _save_checkpoint(ckpt_path, processed_codes, agg_stats, all_results, all_errors)
            processed = min((i + 1) * batch_size, total)
            print(f"\r  [{processed}/{total}] "
                  f"双源={agg_stats['dual_ok']} 单源={agg_stats['single_source']} "
                  f"无数据={agg_stats['no_data']} 写入={agg_stats['written']} "
                  f"偏差={agg_stats['price_mismatch']} 质量={agg_stats['quality_issues']}",
                  end='', flush=True)
        print()
    else:
        task_args = [
            (batch, i % n_workers, args.type, start_date, end_date,
             market, args.tolerance, args.dry_run, today)
            for i, batch in enumerate(batches)
        ]

        pool = mp.Pool(n_workers, initializer=_worker_init)

        async_results = []
        for ta in task_args:
            ar = pool.apply_async(_worker_batch, (ta,))
            async_results.append(ar)

        pool.close()

        done_set = set()
        try:
            while len(done_set) < len(async_results):
                time.sleep(0.5)
                for idx, ar in enumerate(async_results):
                    if idx in done_set:
                        continue
                    if ar.ready():
                        try:
                            batch_result = ar.get(timeout=0)
                            all_results.extend(batch_result["results"])
                            all_errors.extend(batch_result["errors"])
                            for k in agg_stats:
                                agg_stats[k] += batch_result["stats"].get(k, 0)
                            for r in batch_result["results"]:
                                processed_codes.add(r["code"])
                        except Exception:
                            pass
                        done_set.add(idx)

                done = len(done_set)
                processed = min(done * batch_size, total)
                # 有新 batch 完成时保存 checkpoint
                if done_set and not args.symbol and done % max(1, len(batches) // 10) == 0:
                    _save_checkpoint(ckpt_path, processed_codes, agg_stats, all_results, all_errors)
                if done % max(1, len(batches) // 20) == 0 or done == len(batches):
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
            print("\n\n⚠️  收到中断信号，正在保存 checkpoint...")
            _save_checkpoint(ckpt_path, processed_codes, agg_stats, all_results, all_errors)
            print(f"  📂 checkpoint 已保存: {ckpt_path}")
            print(f"  💡 下次运行加 --resume 从断点继续")
        finally:
            pool.terminate()
            pool.join()
            # 确保所有子进程已终止（SIGKILL 兜底）
            for p in pool._pool:
                if p.is_alive():
                    try:
                        import signal as _sig
                        os.kill(p.pid, _sig.SIGKILL)
                    except (OSError, AttributeError):
                        pass
            if _INTERRUPTED:
                sys.exit(1)

    elapsed = time.time() - t0

    # ── 数据清洗 ──
    if not args.dry_run and not args.symbol and not _INTERRUPTED:
        clean_stats = post_process(market, args.type, start_date, end_date, args.tolerance)
    else:
        clean_stats = {}

    # ── 汇总 ──
    print(f"\n[3/5] 汇总统计")
    status = "中断" if _INTERRUPTED else "完成"
    print(f"处理{status}: {agg_stats['total']}/{total + len(processed_codes)} 只  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  双源成功: {agg_stats['dual_ok']}")
    print(f"  单源回退: {agg_stats['single_source']}")
    print(f"  无数据:   {agg_stats['no_data']}")
    print(f"  写入行数: {agg_stats['written']:,}")
    print(f"  价格偏差: {agg_stats['price_mismatch']} 条")
    print(f"  质量问题: {agg_stats['quality_issues']} 条")
    print(f"  连贯断裂: {agg_stats['gaps']} 条")
    print(f"  查询错误: {len(all_errors)} 只")

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

    no_data_list = [r for r in all_results if r["merged_count"] == 0]
    if no_data_list:
        print(f"\n无数据股票 (前 10 只):")
        for r in no_data_list[:10]:
            print(f"  {r['code']:>8} | {r['name']:<8} | "
                  f"A={r['source_a_count']} B={r['source_b_count']}")

    if all_errors:
        print(f"\n⚠️  查询失败（前 10 只）:")
        for code, msg in all_errors[:10]:
            print(f"  {code}: {msg}")
        if len(all_errors) > 10:
            print(f"  ... 还有 {len(all_errors) - 10} 只")

    # 清洗统计
    if clean_stats:
        print(f"\n  数据清洗:")
        print(f"    非交易 bar 删除: {clean_stats.get('non_trading_deleted', 0):,} 条")
        print(f"    过期股票删除:    {clean_stats.get('stale_deleted', 0):,} 条")
        print(f"    0 值修复:        {clean_stats.get('zero_fixed', 0):,} 条 ({clean_stats.get('zero_refetched', 0)} 只)")

    # CSV 报告（必出）
    print(f"\n[4/5] 导出报告")
    csv_path = os.path.join(PROJECT_ROOT, "optimizer",
                            f"report_{args.type}_{start_date}_{end_date}.csv")
    export_csv(all_results, csv_path)

    # 任务完成，清理 checkpoint
    if not _INTERRUPTED and not args.symbol:
        _remove_checkpoint(ckpt_path)

    print(f"\n[5/5] 完成")
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
