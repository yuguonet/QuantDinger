#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# source_sync.py — Coordinator.market_kline 数据源 + 完整性校验 + 写库
# ============================================================================
#
# 核心流程:
#   1. 从 basicinfo_db 获取全市场股票列表
#   2. 每批 50 只交给 Coordinator.coordinate_market_kline()
#   3. 逐只做完整性校验:
#      - 交易日历对比（缺失日检测）
#      - 停复牌检测（vol=0 且 OHLC 相同）
#      - volume > 0（非停牌 bar）
#      - 涨跌幅限制: 沪深主板<11%, 创业板<21%, 科创/北证<31%
#      - 复牌首日/起始日前几日无涨跌幅限制
#      - 15m: 每天 16 bar 检查
#   4. 无错误 → 先删旧数据再写入
#      有错误 → 写 log + 记录进重传文件
#   5. 循环直到全部完成
#   6. 最后重试重传文件一次，正确的从中删除
#
# 用法:
# python optimizer/source_sync.py -T 1D                    # 1D: 2021-01 起
# python optimizer/source_sync.py -T 15m                   # 15m: 2024-01-01 起
# python optimizer/source_sync.py -T 1D --resume           # 断点续传
# python optimizer/source_sync.py -T 1D --retry-only       # 只重试错误股票
# python optimizer/source_sync.py -T 1D --dry-run          # 只校验不写库
#
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
from bisect import bisect_left
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
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
# 时间常量
# ---------------------------------------------------------------------------

TZ_SH = timezone(timedelta(hours=8))

# 15m 标准 bar 时间（16 根，不含 9:30 开盘集合竞价）
_BAR_TIMES_15M = [
    (9, 45), (10, 0), (10, 15), (10, 30), (10, 45),
    (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15),
    (14, 30), (14, 45), (15, 0),
]
_BAR_SET_15M: Set[Tuple[int, int]] = set(_BAR_TIMES_15M)

# 涨跌幅限制（小数形式，0.10 = 10%）
_PRICE_LIMITS = {
    "main_sh":   0.10,   # 沪市主板 600/601/603/605
    "main_sz":   0.10,   # 深市主板 000/001/002/003
    "gem":       0.20,   # 创业板 300/301
    "star":      0.20,   # 科创板 688/689 (注册制后20%)
    "bj":        0.30,   # 北交所 43/82/83/87/88 (30%)
}

# 复牌首日/起始日前几日不检查涨跌幅的天数
_NO_LIMIT_DAYS_AFTER_RESUME = 1   # 复牌首日不限
_NO_LIMIT_DAYS_BEFORE_START = 2   # 起始日前 2 天不限

# 交易日历
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


def _trading_days_between(d1: str, d2: str) -> int:
    if d1 >= d2:
        return 0
    if not _TRADING_DAY_SET:
        _init_trading_calendar(silent=True)
    d1_next = (datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    left = bisect_left(_TRADING_DAYS_SORTED, d1_next)
    right = bisect_left(_TRADING_DAYS_SORTED, d2)
    return max(0, right - left)


def _next_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


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


# ---------------------------------------------------------------------------
# 板块判断
# ---------------------------------------------------------------------------

def _detect_board(code: str) -> str:
    """根据代码判断板块: main_sh/main_sz/gem/star/bj/unknown"""
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
# 数据转换: Coordinator bar → 标准记录
# ---------------------------------------------------------------------------

def _parse_bar_time(bar: Dict[str, Any]) -> Optional[datetime]:
    ts = bar.get("time")
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=TZ_SH)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=TZ_SH)
    dt_str = bar.get("date") or bar.get("datetime") or ""
    if dt_str:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(str(dt_str).strip(), fmt).replace(tzinfo=TZ_SH)
            except ValueError:
                continue
    return None


def _bars_to_records(bars: List[Dict[str, Any]], timeframe: str) -> List[Dict[str, Any]]:
    """将 Coordinator bars 转为标准记录，过滤无效 bar，去重"""
    seen: Dict[datetime, Dict[str, Any]] = {}
    for bar in bars:
        dt = _parse_bar_time(bar)
        if dt is None:
            continue
        if timeframe == "15m":
            total_min = dt.hour * 60 + dt.minute
            if total_min == 570:  # 9:30 丢弃
                continue
            if 690 <= total_min < 780:  # 午休 → 11:30
                dt = dt.replace(hour=11, minute=30, second=0, microsecond=0)
            elif total_min >= 900:  # 15:00+
                dt = dt.replace(hour=15, minute=0, second=0, microsecond=0)
        o = _safe_float(bar.get("open"))
        h = _safe_float(bar.get("high"))
        l = _safe_float(bar.get("low"))
        c = _safe_float(bar.get("close"))
        v = _safe_float(bar.get("volume"))
        # 去重: 同时间戳取后出现的（通常更完整）
        if dt in seen:
            prev = seen[dt]
            # 如果已有记录 volume>0 而新的 volume=0，保留旧的
            if _safe_float(prev.get("volume")) > 0 and v == 0:
                continue
        seen[dt] = {"time": dt, "open": o, "high": h, "low": l, "close": c, "volume": v}
    return sorted(seen.values(), key=lambda r: r["time"])


# ═══════════════════════════════════════════════════════
# DB 写入（删旧 + 写新，同一事务）
# ═══════════════════════════════════════════════════════

class ValidationResult:
    """单只股票的校验结果"""
    __slots__ = ("code", "errors", "warnings", "bar_count", "suspension_dates")

    def __init__(self, code: str):
        self.code = code
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.bar_count: int = 0
        self.suspension_dates: Set[str] = set()

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def add_error(self, msg: str):
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)


def validate_stock(
    code: str,
    records: List[Dict[str, Any]],
    timeframe: str,
    start_date: str,
    end_date: str,
) -> ValidationResult:
    """对单只股票做完整性校验"""
    result = ValidationResult(code)
    result.bar_count = len(records)

    if not records:
        result.add_error("无数据")
        return result

    board = _detect_board(code)
    price_limit = _PRICE_LIMITS.get(board, 0.11)

    # ── 按日聚合: 用于停牌检测和涨跌幅检查 ──
    # daily_agg: {date: {open, high, low, close, volume, bar_count}}
    daily_agg: Dict[str, Dict[str, Any]] = {}
    date_records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for rec in records:
        dt = rec.get("time")
        if not isinstance(dt, datetime):
            continue
        d = dt.strftime("%Y-%m-%d")
        date_records[d].append(rec)
        o = _safe_float(rec.get("open"))
        h = _safe_float(rec.get("high"))
        l = _safe_float(rec.get("low"))
        c = _safe_float(rec.get("close"))
        v = _safe_float(rec.get("volume"))

        if d not in daily_agg:
            daily_agg[d] = {"open": o, "high": h, "low": l, "close": c,
                            "volume": v, "bar_count": 1}
        else:
            agg = daily_agg[d]
            # 日 open = 第一根 bar 的 open
            # 日 high/low = 所有 bar 的 max/min
            # 日 close = 最后一根 bar 的 close
            agg["high"] = max(agg["high"], h)
            agg["low"] = min(agg["low"], l) if agg["low"] > 0 else l
            agg["close"] = c  # 后出现的覆盖，最终是最后一根
            agg["volume"] += v
            agg["bar_count"] += 1

    sorted_dates = sorted(date_records.keys())
    if not sorted_dates:
        result.add_error("无有效日期")
        return result

    actual_start = sorted_dates[0]
    actual_end = sorted_dates[-1]

    # ── 停牌检测: 基于日级聚合 ──
    # 停牌日: 当天所有 bar 的 volume=0 且 OHLC 全相同且 > 0
    suspension_dates: Set[str] = set()
    for d, agg in daily_agg.items():
        if agg["volume"] == 0 and agg["open"] > 0 and \
           agg["open"] == agg["high"] == agg["low"] == agg["close"]:
            suspension_dates.add(d)
    result.suspension_dates = suspension_dates

    # ── 0. 数据量检查: 实际有效日 < 请求范围交易日的 80% 视为坏数据 ──
    range_start = max(actual_start, start_date)
    range_end = min(actual_end, end_date)
    expected_trading_days = [d for d in _TRADING_DAYS_SORTED if range_start <= d <= range_end]
    expected_count = len(expected_trading_days)
    # 有效日 = 数据中有记录且非停牌的日期
    effective_dates = {d for d in sorted_dates if d not in suspension_dates and range_start <= d <= range_end}
    actual_count = len(effective_dates)
    if expected_count > 0:
        coverage = actual_count / expected_count
        if coverage < 0.80:
            result.add_error(
                f"数据覆盖率不足: {actual_count}/{expected_count} "
                f"({coverage*100:.1f}%) < 80%"
            )
            return result  # 覆盖率太低，后续检查无意义

    # ── 1. 交易日历对比: 检测缺失的交易日（仅 1D） ──
    if timeframe == "1D":
        for d in expected_trading_days:
            if d not in date_records and d not in suspension_dates:
                result.add_warning(f"交易日缺失: {d}")

    # ── 2. 每日数据校验 ──
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    no_limit_before: Set[str] = set()
    for i in range(1, _NO_LIMIT_DAYS_BEFORE_START + 1):
        no_limit_before.add((start_dt - timedelta(days=i)).strftime("%Y-%m-%d"))

    prev_close: Optional[float] = None  # 前一个非停牌日的收盘价

    for d in sorted_dates:
        day_records = date_records[d]
        is_suspend = d in suspension_dates
        # 复牌日: 当天非停牌 且 前一个交易日是停牌日或数据缺失
        is_resume = False
        if not is_suspend:
            prev_td = _prev_trading_day(d)
            if prev_td and prev_td >= start_date:
                if prev_td in suspension_dates or prev_td not in date_records:
                    is_resume = True
        is_no_limit = is_resume or (d in no_limit_before)

        # ── 15m: 检查每天 bar 数 ──
        if timeframe == "15m" and not is_suspend and _is_trading_day(d):
            bar_times = set()
            for rec in day_records:
                dt = rec.get("time")
                if isinstance(dt, datetime):
                    bar_times.add((dt.hour, dt.minute))
            missing_bars = _BAR_SET_15M - bar_times
            if missing_bars:
                result.add_warning(
                    f"15m bar 缺失 {d}: {len(missing_bars)} 根 "
                    f"({sorted(missing_bars)[:3]}...)"
                )

        # ── 停牌日跳过逐 bar 校验 ──
        if is_suspend:
            continue

        # ── 逐 bar 校验 ──
        for rec in day_records:
            o = _safe_float(rec.get("open"))
            h = _safe_float(rec.get("high"))
            l = _safe_float(rec.get("low"))
            c = _safe_float(rec.get("close"))
            v = _safe_float(rec.get("volume"))

            # vol > 0（非停牌 bar 必须有成交量）
            if v <= 0:
                result.add_error(f"volume<=0: {d} vol={v}")

            # OHLC 全零
            if o == 0 and h == 0 and l == 0 and c == 0:
                result.add_error(f"OHLC 全零: {d}")
                continue

            # OHLC 合理性
            if h > 0 and l > 0 and h < l:
                result.add_error(f"high<low: {d} H={h} L={l}")
                continue
            if o > 0 and h > 0 and (o > h or o < l):
                result.add_error(f"open 越界: {d} O={o} H={h} L={l}")
            if c > 0 and h > 0 and (c > h or c < l):
                result.add_error(f"close 越界: {d} C={c} H={h} L={l}")

            # ── 涨跌幅检查（基于日级 close） ──
            # 只用每天最后一根 bar 的 close 来检查涨跌幅
            day_agg = daily_agg.get(d)
            if day_agg and not is_no_limit and prev_close is not None and prev_close > 0:
                day_close = day_agg["close"]
                if day_close > 0:
                    change_pct = abs(day_close - prev_close) / prev_close
                    if change_pct > price_limit + 0.015:  # 1.5% 容差
                        direction = "+" if day_close >= prev_close else "-"
                        result.add_error(
                            f"涨跌幅超限: {d} "
                            f"prev={prev_close:.2f} cur={day_close:.2f} "
                            f"pct={direction}{change_pct*100:.2f}% limit={price_limit*100:.0f}%"
                        )

        # 更新 prev_close（用日级 close）
        day_agg = daily_agg.get(d)
        if day_agg and day_agg["close"] > 0:
            prev_close = day_agg["close"]

    # ── 3. 尾部检查 ──
    if actual_end < end_date and _is_trading_day(end_date):
        trailing = _trading_days_between(actual_end, end_date)
        if trailing > 0:
            result.add_warning(f"尾部缺失 {trailing} 天: {actual_end} → {end_date}")

    return result


def _prev_trading_day(d: str) -> Optional[str]:
    """获取 d 之前的最近一个交易日（不含 d 本身）"""
    if not _TRADING_DAY_SET:
        _init_trading_calendar(silent=True)
    idx = bisect_left(_TRADING_DAYS_SORTED, d)
    if idx > 0:
        return _TRADING_DAYS_SORTED[idx - 1]
    return None


# ═══════════════════════════════════════════════════════
# DB 写入（删旧 + 写新）
# ═══════════════════════════════════════════════════════

def write_stock_data(
    writer,
    pool,
    market: str,
    code: str,
    timeframe: str,
    records: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
    dry_run: bool = False,
) -> int:
    """先删旧数据，再写入新数据（同一事务）"""
    if dry_run or not records:
        return len(records)

    # 转为 DB 格式
    db_records = []
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
        db_records.append({
            "symbol": code,
            "timeframe": timeframe,
            "time": dt,
            "open": _safe_float(rec.get("open")),
            "high": _safe_float(rec.get("high")),
            "low": _safe_float(rec.get("low")),
            "close": _safe_float(rec.get("close")),
            "volume": _safe_float(rec.get("volume")),
        })

    if not db_records:
        return 0

    # 同一事务: 先删旧数据，再写新数据
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])
    years = list(range(start_year, end_year + 1))

    try:
        with pool.connection() as conn:
            cur = conn.cursor()
            # 删除旧数据
            for year in years:
                table = f"kline_{timeframe}_{year}"
                try:
                    cur.execute(f"""
                        DELETE FROM "{table}"
                        WHERE symbol = %s
                          AND time >= %s
                          AND time <= %s
                    """, (code, f"{start_date} 00:00:00", f"{end_date} 23:59:59"))
                except Exception:
                    pass  # 表可能不存在
            conn.commit()
            cur.close()
    except Exception as e:
        logger.warning("删旧数据失败 %s/%s: %s", code, timeframe, e)

    # 写入新数据（bulk_write 内部有自己的事务管理）
    try:
        result = writer.bulk_write(market, db_records, batch_size=5000)
        return result.get("inserted", 0)
    except Exception as e:
        logger.warning("写库失败 %s/%s: %s", code, timeframe, e)
        return 0


# ═══════════════════════════════════════════════════════
# 重传文件管理
# ═══════════════════════════════════════════════════════

def _retry_path(timeframe: str) -> str:
    return os.path.join(PROJECT_ROOT, "optimizer", f".retry_{timeframe}.json")


def _load_retry_codes(path: str) -> Dict[str, Dict[str, Any]]:
    """加载重传文件: {code: {errors: [...], retries: n}}"""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_retry_codes(path: str, data: Dict[str, Dict[str, Any]]):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("保存重传文件失败: %s", e)


def _batch_update_retry(path: str, add: Dict[str, List[str]], remove: List[str]):
    """批量更新重传文件: add={code: errors}, remove=[code, ...]"""
    data = _load_retry_codes(path)
    for code, errors in add.items():
        data[code] = {"errors": errors, "retries": data.get(code, {}).get("retries", 0)}
    for code in remove:
        data.pop(code, None)
    _save_retry_codes(path, data)


# ═══════════════════════════════════════════════════════
# 检查点
# ═══════════════════════════════════════════════════════

def _checkpoint_path(timeframe: str) -> str:
    return os.path.join(PROJECT_ROOT, "optimizer", f".checkpoint_source_{timeframe}.json")


def _load_checkpoint(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {"processed_codes": [], "stats": {}}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"processed_codes": [], "stats": {}}


def _save_checkpoint(path: str, processed: list, stats: dict):
    data = {"processed_codes": processed, "stats": stats,
            "saved_at": datetime.now(TZ_SH).isoformat()}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _remove_checkpoint(path: str):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
# 中断信号
# ═══════════════════════════════════════════════════════

_INTERRUPTED = False


def _signal_handler(signum, frame):
    global _INTERRUPTED
    if _INTERRUPTED:
        print("\n⚡ 再次收到中断，强制退出")
        sys.exit(1)
    _INTERRUPTED = True
    print("\n⚠️  收到中断信号，正在保存进度...")


# ═══════════════════════════════════════════════════════
# CSV 报告
# ═══════════════════════════════════════════════════════

def export_csv(results: List[Dict[str, Any]], path: str):
    if not results:
        return
    fields = ["code", "board", "bars", "written", "status", "errors"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"✅ CSV 报告: {path}（{len(results)} 条）")


# ═══════════════════════════════════════════════════════
# 核心处理: 单批
# ═══════════════════════════════════════════════════════

def process_batch(
    symbols: List[str],
    coordinator,
    cb,
    writer,
    pool,
    market: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    count: int,
    timeout: float,
    preferred_source: str,
    adj: str,
    dry_run: bool,
    retry_path: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    处理一批股票: 拉取 → 校验 → 写入/记录错误

    Returns:
        (results_list, stats_dict)
    """
    stats = {"fetched": 0, "passed": 0, "failed": 0, "written": 0, "no_data": 0}
    results = []
    to_retry: Dict[str, List[str]] = {}   # code → errors（待加入重传）
    to_remove: List[str] = []             # 成功的 code（待从重传移除）

    # 拉取数据
    try:
        raw_data = coordinator.coordinate_market_kline(
            cb=cb,
            market=market,
            timeframe=timeframe,
            count=count,
            adj=adj,
            timeout=timeout,
            preferred_source=preferred_source,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
        )
    except Exception as e:
        logger.error("Coordinator 调用失败: %s", e)
        for code in symbols:
            to_retry[code] = [f"Coordinator 异常: {e}"]
            results.append({"code": code, "board": _detect_board(code),
                           "bars": 0, "written": 0, "status": "error",
                           "errors": f"Coordinator 异常: {e}"})
            stats["failed"] += 1
        _batch_update_retry(retry_path, to_retry, [])
        return results, stats

    # 逐只处理
    for code in symbols:
        if _INTERRUPTED:
            break

        bars = raw_data.get(code, [])
        if not bars:
            stats["no_data"] += 1
            to_retry[code] = ["无数据"]
            results.append({"code": code, "board": _detect_board(code),
                           "bars": 0, "written": 0, "status": "no_data",
                           "errors": "无数据"})
            continue

        stats["fetched"] += 1

        # 转为标准记录（已去重）
        records = _bars_to_records(bars, timeframe)
        if not records:
            stats["no_data"] += 1
            to_retry[code] = ["转换后无有效记录"]
            results.append({"code": code, "board": _detect_board(code),
                           "bars": len(bars), "written": 0, "status": "no_data",
                           "errors": "转换后无有效记录"})
            continue

        # 完整性校验
        vr = validate_stock(code, records, timeframe, start_date, end_date)

        if vr.has_errors:
            stats["failed"] += 1
            to_retry[code] = vr.errors
            err_summary = "; ".join(vr.errors[:5])
            if len(vr.errors) > 5:
                err_summary += f" (+{len(vr.errors)-5})"
            logger.warning("[校验失败] %s (%s): %s", code, _detect_board(code), err_summary)
            results.append({"code": code, "board": _detect_board(code),
                           "bars": len(records), "written": 0, "status": "error",
                           "errors": err_summary})
        else:
            stats["passed"] += 1
            n = write_stock_data(writer, pool, market, code, timeframe,
                                records, start_date, end_date, dry_run)
            stats["written"] += n
            to_remove.append(code)
            warn_summary = "; ".join(vr.warnings[:3]) if vr.warnings else ""
            results.append({"code": code, "board": _detect_board(code),
                           "bars": len(records), "written": n, "status": "ok",
                           "errors": warn_summary})

    # 批量更新重传文件（一次 IO）
    _batch_update_retry(retry_path, to_retry, to_remove)

    return results, stats


# ═══════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════

def main():
    global _INTERRUPTED

    parser = argparse.ArgumentParser(
        description="Coordinator.market_kline 数据源 + 完整性校验 + 写库",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-T", "--type",
        choices=["1D", "15m"], default="1D",
        help="数据类型: 1D(日线) / 15m(15分钟线)")
    parser.add_argument("--market", default="CNStock", help="市场（默认 CNStock）")
    parser.add_argument("--batch-size", type=int, default=50,
        help="每批处理股票数（默认 50）")
    parser.add_argument("--count", type=int, default=0,
        help="每只股票拉取条数（0=自动计算）")
    parser.add_argument("--timeout", type=float, default=600,
        help="Coordinator 全局超时秒数（默认 600）")
    parser.add_argument("--preferred-source", default="",
        help="指定首选数据源")
    parser.add_argument("--adj", default="qfq", choices=["qfq", "hfq", ""],
        help="复权方式")
    parser.add_argument("--dry-run", action="store_true",
        help="只拉取校验，不写库")
    parser.add_argument("--resume", action="store_true",
        help="断点续传：跳过已处理的股票")
    parser.add_argument("--retry-only", action="store_true",
        help="只重试重传文件中的股票")

    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if sys.platform != 'win32':
        _wakeup_r, _wakeup_w = os.pipe()
        os.set_blocking(_wakeup_w, False)
        signal.set_wakeup_fd(_wakeup_w)

    # 日期范围
    now_date = datetime.now(TZ_SH).strftime('%Y-%m-%d')

    if args.type == "15m":
        start_date = "2024-01-01"
    else:
        start_date = "2021-01-01"
    end_date = now_date

    from app.utils.db_market import get_market_kline_writer, get_market_db_manager
    from app.data_sources.coordinator import get_coordinator, CircuitBreaker

    writer = get_market_kline_writer()
    mgr = get_market_db_manager()
    coordinator = get_coordinator()
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=120.0, name="source_sync")
    market = args.market

    if not args.dry_run:
        if not mgr.market_db_exists(market):
            mgr.ensure_market_db(market)

    pool = mgr._get_pool(market)

    _init_trading_calendar()

    # 自动计算 count
    count = args.count
    if count <= 0:
        from app.data_sources.provider import calc_kline_count
        count = calc_kline_count(args.type, start_date, end_date)

    retry_path = _retry_path(args.type)
    ckpt_path = _checkpoint_path(args.type)

    # ── 获取股票列表 ──
    print("\n[1/4] 获取股票列表...")
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        all_stocks = db.get_all_stocks(status="active")
    except Exception as e:
        logger.error("获取股票列表失败: %s", e)
        return 1

    all_codes = sorted(s["symbol"] for s in all_stocks)
    print(f"  共 {len(all_codes)} 只A股")

    # ── 断点续传 ──
    processed_set: set = set()
    if args.resume and not args.retry_only:
        ckpt = _load_checkpoint(ckpt_path)
        processed_set = set(ckpt.get("processed_codes", []))
        if processed_set:
            before = len(all_codes)
            all_codes = [c for c in all_codes if c not in processed_set]
            print(f"  📂 断点续传: 已处理 {len(processed_set)} 只，剩余 {len(all_codes)} 只")
            if not all_codes:
                print("  ✅ 所有股票已处理完毕")

    # ── 重试模式 ──
    if args.retry_only:
        retry_data = _load_retry_codes(retry_path)
        all_codes = sorted(retry_data.keys())
        print(f"  🔄 重试模式: {len(all_codes)} 只待重试")

    if not all_codes:
        # 即使主列表为空，也可能需要最终重试
        retry_data = _load_retry_codes(retry_path)
        if retry_data and not args.retry_only:
            all_codes = sorted(retry_data.keys())
            print(f"  🔄 进入最终重试: {len(all_codes)} 只")
        else:
            print("  无需处理")
            _remove_checkpoint(ckpt_path)
            mgr.close_all_pools()
            return 0

    total = len(all_codes)
    batch_size = min(args.batch_size, total)

    print(f"""
╔═══════════════════════════════════════════════════════╗
║  📡 Coordinator.market_kline + 完整性校验 + 写库       ║
╠═══════════════════════════════════════════════════════╣
║  类型: {args.type:<8}  市场: {market:<12}                ║
║  日期: {start_date} → {end_date}                     ║
║  股票: {total} 只  批次: {batch_size}  条数: {count:<8}          ║
║  复权: {args.adj or '不复权':<8}  超时: {args.timeout:.0f}s                   ║
║  模式: {'重试' if args.retry_only else '主循环'}{'  dry-run' if args.dry_run else ''}                         ║
╚═══════════════════════════════════════════════════════╝
""")

    # ── 分批处理（支持中断后交互式续传）──
    print(f"\n[2/4] 拉取 + 校验 + 写入...")

    all_results: List[Dict[str, Any]] = []
    agg_stats = {
        "total": 0, "fetched": 0, "passed": 0, "failed": 0,
        "no_data": 0, "written": 0,
    }

    t0 = time.time()
    batches = [all_codes[i:i + batch_size] for i in range(0, len(all_codes), batch_size)]

    while True:
        for batch_idx, batch_codes in enumerate(batches):
            if _INTERRUPTED:
                break

            batch_start = time.time()
            results, stats = process_batch(
                symbols=batch_codes,
                coordinator=coordinator,
                cb=cb,
                writer=writer,
                pool=pool,
                market=market,
                timeframe=args.type,
                start_date=start_date,
                end_date=end_date,
                count=count,
                timeout=args.timeout,
                preferred_source=args.preferred_source,
                adj=args.adj,
                dry_run=args.dry_run,
                retry_path=retry_path,
            )

            all_results.extend(results)
            for k in agg_stats:
                agg_stats[k] += stats.get(k, 0)

            # 更新已处理列表
            for code in batch_codes:
                processed_set.add(code)

            batch_elapsed = time.time() - batch_start
            total_elapsed = time.time() - t0
            done = min((batch_idx + 1) * batch_size, total)

            print(f"\r  [{done}/{total}] "
                  f"拉取={agg_stats['fetched']} 通过={agg_stats['passed']} "
                  f"失败={agg_stats['failed']} 无数据={agg_stats['no_data']} "
                  f"写入={agg_stats['written']:,} "
                  f"耗时={total_elapsed:.0f}s",
                  end='', flush=True)

            # 定期保存检查点
            if (batch_idx + 1) % 5 == 0:
                _save_checkpoint(ckpt_path, list(processed_set), agg_stats)

        print()

        # 始终保存检查点（无论是否中断）
        _save_checkpoint(ckpt_path, list(processed_set), agg_stats)

        # 正常完成或 dry-run → 退出循环
        if not _INTERRUPTED:
            break

        # ── 中断交互模式 ──
        remaining_codes = [c for c in all_codes if c not in processed_set]
        print(f"\n  ⏸️  已中断")
        print(f"  已处理: {len(processed_set)}/{total}")
        print(f"  剩余:   {len(remaining_codes)} 只")
        print(f"  进度已保存至检查点文件")

        while True:
            try:
                choice = input("\n  输入 'r' 续传 / 'q' 退出: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = 'q'
            if choice == 'r':
                _INTERRUPTED = False
                if not remaining_codes:
                    print("  ✅ 所有股票已处理完毕")
                    break
                total = len(remaining_codes)
                batches = [remaining_codes[i:i + batch_size]
                           for i in range(0, len(remaining_codes), batch_size)]
                print(f"  ▶️  续传: 剩余 {total} 只，{len(batches)} 批\n")
                print(f"[2/4] 拉取 + 校验 + 写入（续传）...")
                break  # 跳出内层 while，回到外层 for
            elif choice == 'q':
                break  # 跳出内层 while
            else:
                print("  无效输入，请输入 'r' 或 'q'")

        if choice == 'q':
            break
        # choice == 'r' → 继续外层 while True 循环

    elapsed_main = time.time() - t0

    # ── 最终重试（已禁用，由用户通过 --retry-only 手动重试）──
    retry_data = _load_retry_codes(retry_path)
    retry_codes = sorted(retry_data.keys())

    if retry_codes and not args.dry_run:
        print(f"\n[3/4] 跳过自动重试: {len(retry_codes)} 只待修复（使用 --retry-only 手动重试）")
    else:
        print(f"\n[3/4] 无需重试")

    elapsed_total = time.time() - t0

    # ── 汇总 ──
    print(f"\n[4/4] 汇总统计")
    print(f"总耗时: {elapsed_total:.1f}s ({elapsed_total/60:.1f}分钟)")
    print(f"  总计:   {agg_stats['total']}")
    print(f"  拉取:   {agg_stats['fetched']}")
    print(f"  校验通过: {agg_stats['passed']}")
    print(f"  校验失败: {agg_stats['failed']}")
    print(f"  无数据:   {agg_stats['no_data']}")
    print(f"  写入行数: {agg_stats['written']:,}")

    # 错误统计
    error_results = [r for r in all_results if r.get("status") == "error"]
    if error_results:
        # 按错误类型聚合
        error_types: Dict[str, int] = defaultdict(int)
        for r in error_results:
            for err in r.get("errors", "").split("; "):
                err_type = err.split(":")[0].strip() if ":" in err else err
                error_types[err_type] += 1
        print(f"\n错误类型分布:")
        for etype, cnt in sorted(error_types.items(), key=lambda x: -x[1])[:10]:
            print(f"  {etype}: {cnt}")

    # 按板块统计
    board_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"ok": 0, "fail": 0})
    for r in all_results:
        board = r.get("board", "unknown")
        if r.get("status") == "ok":
            board_stats[board]["ok"] += 1
        else:
            board_stats[board]["fail"] += 1
    if board_stats:
        print(f"\n板块统计:")
        for board, st in sorted(board_stats.items()):
            print(f"  {board}: 通过={st['ok']} 失败={st['fail']}")

    # CSV 报告
    csv_path = os.path.join(PROJECT_ROOT, "optimizer",
                            f"report_source_{args.type}_{start_date}_{end_date}.csv")
    export_csv(all_results, csv_path)

    # 清理检查点（仅全部完成且无错误时）
    remaining_retry = _load_retry_codes(retry_path)
    if not remaining_retry and not _INTERRUPTED:
        _remove_checkpoint(ckpt_path)
        # 清理重传文件
        try:
            if os.path.isfile(retry_path):
                os.remove(retry_path)
        except Exception:
            pass

    print(f"\n{'='*60}")
    if remaining_retry:
        print(f"  ⚠️  {len(remaining_retry)} 只仍有错误，详见: {retry_path}")
    elif _INTERRUPTED:
        print(f"  ⏸️  已退出，进度已保存。下次用 --resume 继续")
    else:
        print(f"  ✅ 全部完成!")
    print(f"{'='*60}")

    mgr.close_all_pools()
    return 1 if (error_results or _INTERRUPTED) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出。进度已保存，下次用 --resume 继续。")
        sys.exit(1)
