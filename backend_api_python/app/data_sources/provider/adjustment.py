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

缓存策略 (增量):
  - 因子是只增不减的历史数据，适合增量合并
  - 缓存文件格式: {code: {"factors": [[date, factor], ...], "synced_at": ts}}
  - synced_at 持久化到文件，重启后仍可判断是否需要刷新
  - synced_at 未过期 → 直接返回内存缓存，不发网络请求
  - synced_at 已过期或无缓存 → 远端获取 → 增量合并 → 更新 synced_at
"""

from __future__ import annotations

import json
import os
import re
import ssl as _ssl
import threading
import time
import urllib.request as _urllib
from typing import Dict, List, Optional, Tuple

# ================================================================
# 缓存配置
# ================================================================

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
_CACHE_FILE = os.path.join(_CACHE_DIR, "adjustment_factors.json")
_CACHE_TTL = 3600  # 缓存有效期（秒），1 小时

# ================================================================
# HTTP 工具
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
# 缓存 — 增量式，带持久化的同步时间戳
# ================================================================

# 内存缓存: code → {"factors": [(date, factor), ...], "synced_at": float}
_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()
_write_lock = threading.Lock()
_dirty: set = set()


def _load_cache_file():
    """import 时从文件加载到内存。"""
    try:
        if not os.path.exists(_CACHE_FILE):
            return
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for code, entry in raw.items():
            if isinstance(entry, dict) and "factors" in entry:
                _cache[code] = {
                    "factors": [(d, float(v)) for d, v in entry["factors"]],
                    "synced_at": float(entry.get("synced_at", 0)),
                }
            elif isinstance(entry, list):
                # 兼容旧格式: {code: [[date, factor], ...]}
                _cache[code] = {
                    "factors": [(d, float(v)) for d, v in entry],
                    "synced_at": 0,
                }
    except Exception:
        pass


def _save_cache_file():
    """仅将有增量更新的股票写回文件。"""
    with _write_lock:
        with _cache_lock:
            to_save = list(_dirty)
            _dirty.clear()
        if not to_save:
            return
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            existing: Dict[str, dict] = {}
            if os.path.exists(_CACHE_FILE):
                try:
                    with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass
            with _cache_lock:
                for code in to_save:
                    if code in _cache:
                        existing[code] = {
                            "factors": list(_cache[code]["factors"]),
                            "synced_at": _cache[code]["synced_at"],
                        }
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
        except Exception:
            pass


_load_cache_file()


# ================================================================
# 因子获取
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


def _merge_factors(existing: List[Tuple[str, float]],
                   remote: List[Tuple[str, float]]) -> Tuple[List[Tuple[str, float]], bool]:
    """增量合并因子: 保留已有，仅追加新日期。

    Returns:
        (merged_factors, has_new_data)
    """
    existing_dates = {d for d, _ in existing}
    new_items = [(d, f) for d, f in remote if d not in existing_dates]
    if not new_items:
        return existing, False
    merged = list(existing) + new_items
    merged.sort(key=lambda x: x[0], reverse=True)
    return merged, True


def fetch_qfq_factors(code: str, force: bool = False) -> Optional[List[Tuple[str, float]]]:
    """获取前复权因子 (从新浪 qfq.js)。

    因子含义:
      fwd_price = unadj_price / qfq_factor
      unadj_price = fwd_price * qfq_factor
      最新除权日 factor=1.0，越早的日期 factor 越大。

    缓存策略:
      1. 缓存存在且 synced_at 未过期 → 直接返回
      2. 缓存不存在或已过期 → 远端获取 → 增量合并 → 更新 synced_at
      3. force=True → 跳过缓存，强制拉取

    Args:
        code: 股票代码
        force: 是否强制刷新（忽略缓存时效）

    Returns:
        [(date_str, factor), ...] 按日期降序，失败返回 None
    """
    sina_code = _to_sina_code(code)
    if not sina_code:
        return None

    # 1. 缓存命中且未过期
    if not force:
        with _cache_lock:
            entry = _cache.get(sina_code)
        if entry and (time.time() - entry["synced_at"] < _CACHE_TTL):
            return entry["factors"]

    # 2. 远端获取 + 增量合并
    text = _http_get(f"https://finance.sina.com.cn/realstock/company/{sina_code}/qfq.js")
    if not text:
        return None

    remote_factors = _parse_sina_factor(text)
    if not remote_factors:
        return None

    with _cache_lock:
        entry = _cache.get(sina_code)
        if entry is None:
            _cache[sina_code] = {
                "factors": remote_factors,
                "synced_at": time.time(),
            }
            _dirty.add(sina_code)
        else:
            merged, has_new = _merge_factors(entry["factors"], remote_factors)
            entry["factors"] = merged
            entry["synced_at"] = time.time()
            # synced_at 已更新，需要写回文件（即使无新因子数据）
            _dirty.add(sina_code)

    threading.Thread(target=_save_cache_file, daemon=True).start()
    with _cache_lock:
        return _cache.get(sina_code, {}).get("factors")


# ================================================================
# 因子查找
# ================================================================

def _find_factor(sorted_dates: List[str], factor_map: Dict[str, float],
                 bar_date: str, latest_ex: Optional[str]) -> float:
    """查找 bar_date 对应的因子。

    - bar_date >= latest_ex → 返回 1.0 (无除权，无需调整)
    - bar_date < latest_ex  → 返回 <= bar_date 的最大日期的因子
    """
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


# ================================================================
# K线时间提取
# ================================================================

def _extract_date(bar_time) -> str:
    """从 bar['time'] 提取 YYYY-MM-DD。"""
    t = str(bar_time or "")
    return t[:10]


# ================================================================
# 复权计算
# ================================================================

def reverse_fwd_adjust(klines: list, code: str) -> list:
    """将前复权K线还原为不复权。

    公式: unadj_price = fwd_price * qfq_factor
    """
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
    """不复权 → 前复权。

    公式: fwd_price = unadj_price / qfq_factor
    """
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
    """不复权 → 后复权。

    公式: hfq_price = unadj_price * hfq_factor
    hfq_factor = earliest_qfq / qfq_factor (最早除权日基准=1.0)
    """
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

def update_all_factors() -> int:
    """增量更新所有股票的前复权因子。

    复用 fetch_qfq_factors(force=True)，避免重复 fetch/merge 逻辑。

    Returns:
        有增量更新的股票数量
    """
    from app.utils.basicinfo_db import get_stock_basic_db

    codes = get_stock_basic_db().market_all_codes(status="active")
    if not codes:
        return 0

    updated = 0
    for code in codes:
        sina_code = _to_sina_code(code)
        if not sina_code:
            continue

        # 记录更新前的因子数量
        with _cache_lock:
            old_count = len(_cache.get(sina_code, {}).get("factors", []))

        factors = fetch_qfq_factors(code, force=True)
        if factors:
            with _cache_lock:
                new_count = len(_cache.get(sina_code, {}).get("factors", []))
            if new_count > old_count:
                updated += 1

    return updated
