# -*- coding: utf-8 -*-
"""
除权除息因子模块 — 独立模块，不依赖项目其他文件

因子来源: 新浪财经 qfq.js / hfq.js
  - qfq (前复权): fwd_price = unadj_price / qfq_factor
  - hfq (后复权): hfq_price = unadj_price * hfq_factor

对外暴露:
  - fetch_qfq_factors(code) — 获取前复权因子 [(date, factor), ...]
  - reverse_fwd_adjust(klines, code) — 将前复权K线还原为不复权
  - unadj_to_qfq(klines, code) — 不复权 → 前复权
  - unadj_to_hfq(klines, code) — 不复权 → 后复权

因子性质: 只增不改的历史档案 — 已有记录永远不变，只追加新除权日。
"""

from __future__ import annotations

import json
import os
import re
import ssl as _ssl
import threading
import time
import urllib.request as _urllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ================================================================
# 配置
# ================================================================

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
_CACHE_FILE = os.path.join(_CACHE_DIR, "adjustment_factors.json")

# ================================================================
# HTTP
# ================================================================

_SSL_CTX = _ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = _ssl.CERT_NONE

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}


def _http_get(url: str, timeout: int = 6) -> Optional[str]:
    try:
        req = _urllib.Request(url, headers=_HEADERS)
        with _urllib.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


# ================================================================
# 代码转换
# ================================================================

def _to_sina_code(code: str) -> Optional[str]:
    """将任意格式股票代码转为新浪格式 (sz000001 / sh600519)。"""
    c = code.strip().upper().replace(".", "").replace("SH", "").replace("SZ", "").replace("BJ", "")
    if not c.isdigit() or len(c) != 6:
        return None
    prefix = code.strip()[:2].upper()
    if prefix == "SH":
        return "sh" + c
    elif prefix == "SZ":
        return "sz" + c
    elif prefix == "BJ":
        return "bj" + c
    if c.startswith(("6", "9")):
        return "sh" + c
    return "sz" + c


# ================================================================
# 缓存
# ================================================================
#
# 设计原则:
#   - 文件是持久层，内存是快速层
#   - 启动时: 文件 → 内存 (全量加载)
#   - 新数据时: 内存 → 文件 (异步全量写入)
#   - 读取只查内存，不碰磁盘
#   - 因子只增不改，永不"过期"

# 内存缓存: sina_code → [(date, factor), ...]
_mem: Dict[str, List[Tuple[str, float]]] = {}
_mem_lock = threading.Lock()
_write_lock = threading.Lock()


def _load():
    """启动时从文件全量加载到内存。"""
    try:
        if not os.path.exists(_CACHE_FILE):
            return
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        n = 0
        for code, entry in raw.items():
            if isinstance(entry, dict) and "factors" in entry:
                _mem[code] = [(d, float(v)) for d, v in entry["factors"]]
                n += 1
            elif isinstance(entry, list):
                _mem[code] = [(d, float(v)) for d, v in entry]
                n += 1
    except Exception:
        pass


def _save():
    """异步全量写入。调用方已在后台线程中。"""
    with _write_lock:
        with _mem_lock:
            snapshot = dict(_mem)
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            # 先写临时文件再原子替换，避免写入中途崩溃导致文件损坏
            tmp = _CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.replace(tmp, _CACHE_FILE)
        except Exception:
            pass


def _save_async():
    """触发一次异步写盘。"""
    threading.Thread(target=_save, daemon=True).start()


_load()


# ================================================================
# 因子解析 & 合并
# ================================================================

def _parse_sina_factor(text: str) -> Optional[List[Tuple[str, float]]]:
    """解析新浪因子 JS 返回。

    格式: var sz301128qfq={"total":N,"data":[{"d":"2026-05-19","f":"1.0000000000000000"},...]}
    """
    m = re.search(r'"total":\s*(\d+),\s*"data":\s*\[(.*?)\]', text)
    if not m:
        return None
    items = re.findall(r'\{"d":"([\d-]+)",\s*"f":"([\d.]+)"\}', m.group(2))
    if not items:
        return None
    factors = [(d, float(f)) for d, f in items if d > "1900-01-01"]
    return factors if factors else None


def _merge(existing: List[Tuple[str, float]],
           remote: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """增量合并: 保留已有，追加新日期，按日期降序排列。"""
    known = {d for d, _ in existing}
    new = [(d, f) for d, f in remote if d not in known]
    if not new:
        return existing
    merged = existing + new
    merged.sort(key=lambda x: x[0], reverse=True)
    return merged


# ================================================================
# 因子获取
# ================================================================

def _fetch_remote(sina_code: str) -> Optional[List[Tuple[str, float]]]:
    """从远端拉取因子，写入内存+触发异步写盘。"""
    text = _http_get(f"https://finance.sina.com.cn/realstock/company/{sina_code}/qfq.js")
    if not text:
        return None
    remote = _parse_sina_factor(text)
    if not remote:
        return None

    with _mem_lock:
        old = _mem.get(sina_code)
        if old is None:
            _mem[sina_code] = remote
        else:
            _mem[sina_code] = _merge(old, remote)
    _save_async()
    return _mem.get(sina_code)


def fetch_qfq_factors(code: str) -> Optional[List[Tuple[str, float]]]:
    """获取前复权因子。

    因子含义:
      fwd_price = unadj_price / qfq_factor
      unadj_price = fwd_price * qfq_factor
      最新除权日 factor=1.0，越早的日期 factor 越大。

    策略: 有缓存直接返回，无缓存同步拉取。

    Args:
        code: 股票代码 (任意格式)

    Returns:
        [(date_str, factor), ...] 按日期降序，失败返回 None
    """
    sina_code = _to_sina_code(code)
    if not sina_code:
        return None

    with _mem_lock:
        entry = _mem.get(sina_code)
    if entry:
        return entry

    return _fetch_remote(sina_code)


# ================================================================
# 因子查找
# ================================================================

def _find_factor(sorted_dates: List[str], factor_map: Dict[str, float],
                 bar_date: str, latest_ex: Optional[str]) -> float:
    """查找 bar_date 对应的因子。"""
    if latest_ex and bar_date >= latest_ex:
        return 1.0
    for d in reversed(sorted_dates):
        if d <= bar_date:
            return factor_map[d]
    return 1.0


def _build_factor_lookup(factors: Optional[List[Tuple[str, float]]]):
    """构建因子查找结构。"""
    if not factors:
        return None, None, None
    factor_map = {d: f for d, f in factors}
    sorted_dates = sorted(factor_map.keys())
    latest_ex = sorted_dates[-1] if sorted_dates else None
    return factor_map, sorted_dates, latest_ex


def _extract_date(bar_time) -> str:
    """从 bar['time'] 提取 YYYY-MM-DD。

    支持格式:
      - int/float Unix 时间戳 (如 1717200000)
      - datetime 对象
      - 字符串 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM:SS"
    """
    if isinstance(bar_time, (int, float)):
        return datetime.fromtimestamp(bar_time).strftime("%Y-%m-%d")
    if isinstance(bar_time, datetime):
        return bar_time.strftime("%Y-%m-%d")
    t = str(bar_time or "")
    return t[:10]


# ================================================================
# 复权计算
# ================================================================

def reverse_fwd_adjust(klines: list, code: str) -> list:
    """将前复权K线还原为不复权。公式: unadj_price = fwd_price * qfq_factor"""
    if not klines:
        return klines
    factors = fetch_qfq_factors(code)
    factor_map, sorted_dates, latest_ex = _build_factor_lookup(factors)
    if not factor_map:
        return klines
    result = []
    for bar in klines:
        bar_date = _extract_date(bar.get("time", ""))
        factor = _find_factor(sorted_dates, factor_map, bar_date, latest_ex)
        if factor != 1.0:
            result.append({
                "time": bar["time"],
                "open": round(bar["open"] * factor, 4),
                "high": round(bar["high"] * factor, 4),
                "low": round(bar["low"] * factor, 4),
                "close": round(bar["close"] * factor, 4),
                "volume": bar["volume"],
            })
        else:
            result.append(bar)
    return result


def unadj_to_qfq(klines: list, code: str) -> list:
    """不复权 → 前复权。公式: fwd_price = unadj_price / qfq_factor"""
    if not klines:
        return klines
    factors = fetch_qfq_factors(code)
    factor_map, sorted_dates, latest_ex = _build_factor_lookup(factors)
    if not factor_map:
        return klines
    result = []
    for bar in klines:
        bar_date = _extract_date(bar.get("time", ""))
        factor = _find_factor(sorted_dates, factor_map, bar_date, latest_ex)
        if factor != 1.0:
            result.append({
                "time": bar["time"],
                "open": round(bar["open"] / factor, 4),
                "high": round(bar["high"] / factor, 4),
                "low": round(bar["low"] / factor, 4),
                "close": round(bar["close"] / factor, 4),
                "volume": bar["volume"],
            })
        else:
            result.append(bar)
    return result


def unadj_to_hfq(klines: list, code: str) -> list:
    """不复权 → 后复权。公式: hfq_price = unadj_price * hfq_factor"""
    if not klines:
        return klines
    factors = fetch_qfq_factors(code)
    factor_map, sorted_dates, latest_ex = _build_factor_lookup(factors)
    if not factor_map:
        return klines
    earliest_qfq = factor_map.get(sorted_dates[0], 1.0) if sorted_dates else 1.0
    result = []
    for bar in klines:
        bar_date = _extract_date(bar.get("time", ""))
        qfq_factor = _find_factor(sorted_dates, factor_map, bar_date, latest_ex)
        if qfq_factor != 1.0:
            hfq_factor = earliest_qfq / qfq_factor
            result.append({
                "time": bar["time"],
                "open": round(bar["open"] * hfq_factor, 4),
                "high": round(bar["high"] * hfq_factor, 4),
                "low": round(bar["low"] * hfq_factor, 4),
                "close": round(bar["close"] * hfq_factor, 4),
                "volume": bar["volume"],
            })
        else:
            result.append(bar)
    return result


# ================================================================
# 全量更新 — 供 backfill_db 调度器调用
# ================================================================

def update_all_factors(max_workers: int = 16) -> int:
    """拉取所有活跃股票的因子。已有缓存的跳过，只拉缺失的，16 并发。

    Returns:
        新增缓存的股票数量
    """
    from app.utils.basicinfo_db import get_stock_basic_db

    codes = get_stock_basic_db().market_all_codes(status="active")
    if not codes:
        return 0

    # 只收集内存中没有的
    to_fetch = []
    for code in codes:
        sina_code = _to_sina_code(code)
        if not sina_code:
            continue
        with _mem_lock:
            if sina_code not in _mem:
                to_fetch.append(code)

    if not to_fetch:
        return 0

    updated = 0

    def _fetch_one(code):
        return fetch_qfq_factors(code) is not None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, code): code for code in to_fetch}
        for future in as_completed(futures):
            try:
                if future.result():
                    updated += 1
            except Exception:
                pass

    return updated
