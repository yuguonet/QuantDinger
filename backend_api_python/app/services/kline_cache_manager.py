# -*- coding: utf-8 -*-
"""
K线 Feather 缓存管理器

仅缓存日线数据（周线/月线由日线实时聚合）。

存储布局（每只股票独立一个文件）：
  data/kline_cache/1D/{market}_{symbol}.feather

核心特性：
  - 惰性加载：优先读本地 feather 缓存，缓存未命中才走远程
  - 增量更新：只拉取缺失的股票，完全过期才全量拉取
  - 批量预热：遍历所有用户自选股去重后批量拉取
  - 市场时段合成：用 1m/5m/15m/30m/1H 合成当日未完成 K 线
  - 周线/月线由日线缓存实时聚合，不单独存储
"""

import os
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import pandas as pd
import pyarrow.feather as feather

from app.utils.logger import get_logger
from app.interfaces.trading_calendar import (
    is_trading_day_today,
    prev_trading_day,
)

logger = get_logger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────────

DAILY_LIMIT = 1500       # 日线：约 5 年
MONTHLY_LIMIT = 240      # 月线聚合：约 20 年

KLINE_COLUMNS = ["time", "open", "high", "low", "close", "volume"]
VALID_TIMEFRAMES = {"1D"}

# 预热锁：防止并发预热
_prewarm_lock = threading.Lock()

# 失败 symbol 记录：{symbol: 失败时间戳}，避免无限重试拉取失败的股票
_failed_symbols: Dict[str, float] = {}
_failed_lock = threading.Lock()
FAILED_COOLDOWN = {
    "1D": 6 * 3600,    # 日线失败后冷却 6 小时
}


# ─── 失败 symbol 管理 ──────────────────────────────────────────────

def _get_failed_symbols(tf: str) -> Set[str]:
    """获取当前冷却期内的失败 symbol 集合"""
    cooldown = FAILED_COOLDOWN.get(tf, 6 * 3600)
    now = time.time()
    with _failed_lock:
        expired = [s for s, ts in _failed_symbols.items() if now - ts > cooldown]
        for s in expired:
            del _failed_symbols[s]
        return set(_failed_symbols.keys())


def _record_failed_symbols(symbols: List[str]):
    """记录拉取失败的 symbol 及其时间戳"""
    if not symbols:
        return
    now = time.time()
    with _failed_lock:
        for s in symbols:
            _failed_symbols[s] = now
        # 防止无限增长：超过 2000 条时清理最旧的
        if len(_failed_symbols) > 2000:
            sorted_items = sorted(_failed_symbols.items(), key=lambda x: x[1])
            for s, _ in sorted_items[:len(sorted_items) // 2]:
                del _failed_symbols[s]
    logger.info(f"[KlineCache] 记录 {len(symbols)} 只失败 symbol（冷却期内不再重试）: {symbols[:10]}")


def _clear_failed_symbols(symbols: List[str]):
    """清除成功拉取的 symbol 的失败记录"""
    if not symbols:
        return
    with _failed_lock:
        for s in symbols:
            _failed_symbols.pop(s, None)


# ─── 工具函数 ───────────────────────────────────────────────────────

def _now_ts() -> int:
    return int(time.time())


def _dt_from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))


def _today_str() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _ts_from_date(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(dt.timestamp())


def _is_market_hours() -> bool:
    """判断当前是否为 A 股交易时段（使用交易日历精确判断）"""
    if not is_trading_day_today():
        return False
    from datetime import time as dt_time
    now = datetime.now(timezone(timedelta(hours=8)))
    t = now.time()
    return (dt_time(9, 15) <= t <= dt_time(11, 30)) or (dt_time(13, 0) <= t <= dt_time(15, 0))


def _iso_week_start(ts: int) -> int:
    dt = _dt_from_ts(ts)
    monday = dt - timedelta(days=dt.isoweekday() - 1)
    return int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _month_start(ts: int) -> int:
    dt = _dt_from_ts(ts)
    return int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def _bar_field(bar: Dict[str, Any], field: str, default: float = 0.0) -> float:
    try:
        return float(bar.get(field, default))
    except (TypeError, ValueError):
        return default


def _sanitize_symbol(symbol: str) -> str:
    """将 symbol 中不适合做文件名的字符替换为下划线"""
    return symbol.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")


# ─── 聚合函数（不修改原列表） ───────────────────────────────────────

def aggregate_daily_to_monthly(daily_bars: List[Dict[str, Any]], limit: int = MONTHLY_LIMIT) -> List[Dict[str, Any]]:
    if not daily_bars:
        return []
    bars = sorted(daily_bars, key=lambda x: x.get("time", 0))
    groups: Dict[int, List[Dict]] = {}
    order: List[int] = []
    for bar in bars:
        t = bar.get("time", 0)
        if not t:
            continue
        ms = _month_start(t)
        if ms not in groups:
            groups[ms] = []
            order.append(ms)
        groups[ms].append(bar)
    result = []
    for ms in order:
        chunk = groups[ms]
        if not chunk:
            continue
        result.append({
            "time": ms,
            "open": _bar_field(chunk[0], "open"),
            "high": max(_bar_field(b, "high") for b in chunk),
            "low": min(_bar_field(b, "low") for b in chunk),
            "close": _bar_field(chunk[-1], "close"),
            "volume": round(sum(_bar_field(b, "volume") for b in chunk), 2),
        })
    return result[-limit:] if len(result) > limit else result


# ─── 缓存管理器 ─────────────────────────────────────────────────────

class KlineCacheManager:
    """
    K线本地 feather 缓存管理器（仅日线，每只股票独立一个文件）。

    文件布局：
      {data_dir}/1D/{market}_{symbol}.feather

    周线/月线由日线缓存实时聚合，不单独存储。
    无 market 的股票不写入缓存，仅记录日志。

    锁策略：
      _prewarm_lock（模块级） — 保护预热流程，不阻塞读操作
      无需文件级锁 — 每只股票独立文件，原子写入天然无竞态
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "kline_cache")
        os.makedirs(os.path.join(self.data_dir, "1D"), exist_ok=True)

    # ═══════════════════════════════════════════════════════════════════
    #  文件路径 & 读写
    # ═══════════════════════════════════════════════════════════════════

    def _feather_path(self, tf: str, market: str, symbol: str) -> str:
        """获取单只股票的 feather 文件路径: {data_dir}/{tf}/{market}_{symbol}.feather"""
        safe_symbol = _sanitize_symbol(symbol)
        safe_market = _sanitize_symbol(market)
        return os.path.join(self.data_dir, tf, f"{safe_market}_{safe_symbol}.feather")

    def _list_symbol_files(self, tf: str) -> List[str]:
        """列出指定 timeframe 下的所有 feather 文件名"""
        tf_dir = os.path.join(self.data_dir, tf)
        try:
            return [f for f in os.listdir(tf_dir) if f.endswith(".feather")]
        except OSError as e:
            logger.warning(f"[KlineCache] 列目录失败 {tf_dir}: {e}")
            return []

    def _read_feather(self, path: str) -> pd.DataFrame:
        """读取 feather 文件，带容错恢复"""
        if not path or not os.path.exists(path):
            return pd.DataFrame()
        try:
            df = feather.read_feather(path)
            if df.empty:
                return pd.DataFrame()
            return df
        except Exception as e:
            logger.warning(f"[KlineCache] 读取失败 {path}: {e}")
            # 尝试从备份恢复
            bak = path + ".bak"
            if os.path.exists(bak):
                try:
                    return feather.read_feather(bak)
                except Exception:
                    pass
            return pd.DataFrame()

    def _write_feather(self, path: str, df: pd.DataFrame):
        """原子写入单只股票的 feather 文件（先备份 → 写 tmp → 验证 → rename）。"""
        bak = path + ".bak"
        tmp = path + f".tmp.{os.getpid()}.{int(time.time()*1000)}"

        try:
            # 备份旧文件
            if os.path.exists(path):
                try:
                    shutil.copy2(path, bak)
                except OSError:
                    pass

            df.to_feather(tmp)

            # 验证写入完整性
            try:
                verify = feather.read_feather(tmp)
                if len(verify) != len(df):
                    raise ValueError(f"验证失败: 预期 {len(df)} 行, 实际 {len(verify)} 行")
            except Exception as ve:
                raise ve

            os.replace(tmp, path)

        except Exception as e:
            logger.error(f"[KlineCache] 写入失败 {path}: {e}")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not os.path.exists(path) and os.path.exists(bak):
                try:
                    shutil.copy2(bak, path)
                except OSError:
                    pass

    # ═══════════════════════════════════════════════════════════════════
    #  缓存查询（只读，依赖原子文件操作）
    # ═══════════════════════════════════════════════════════════════════

    def get_cached(self, tf: str, symbol: str, market: str = "") -> Optional[List[Dict[str, Any]]]:
        """读取单只股票的缓存（直接读对应文件，无需全量过滤）"""
        if tf not in VALID_TIMEFRAMES:
            return None
        if not market:
            return None
        path = self._feather_path(tf, market, symbol)
        df = self._read_feather(path)
        if df.empty:
            return None
        try:
            for col in KLINE_COLUMNS:
                if col not in df.columns:
                    df[col] = 0
            return df[KLINE_COLUMNS].to_dict("records")
        except Exception:
            return None

    def get_cached_symbols(self, tf: str) -> Set[str]:
        """获取指定 timeframe 下已缓存的所有 symbol（从文件名解析）"""
        if tf not in VALID_TIMEFRAMES:
            return set()
        symbols = set()
        for fname in self._list_symbol_files(tf):
            # 格式: {market}_{symbol}.feather
            name = fname[:-8]  # 去掉 .feather
            parts = name.split("_", 1)
            if len(parts) == 2:
                symbols.add(parts[1])  # symbol 部分
        return symbols

    @staticmethod
    def clear_failed(symbols: Optional[List[str]] = None):
        """清除失败 symbol 记录，允许重新拉取。"""
        with _failed_lock:
            if symbols is None:
                count = len(_failed_symbols)
                _failed_symbols.clear()
                logger.info(f"[KlineCache] 清除全部 {count} 条失败记录")
            else:
                for s in symbols:
                    _failed_symbols.pop(s, None)
                logger.info(f"[KlineCache] 清除 {len(symbols)} 条失败记录")

    @staticmethod
    def get_failed_info() -> Dict[str, float]:
        """获取当前失败 symbol 及其冷却剩余秒数（供调试/监控用）"""
        now = time.time()
        with _failed_lock:
            return {s: max(0, ts + 6 * 3600 - now) for s, ts in _failed_symbols.items()}

    def is_stale(self, tf: str, symbol: str, market: str) -> bool:
        """根据文件修改时间和交易日历判断单只股票的日线缓存是否过期。

        日线：
          - 文件不存在 → 过期
          - 文件 mtime < 上一个交易日收盘时间 → 过期
          - 盘中建的文件（mtime < 15:00），当前已收盘 → 过期
        """
        if tf != "1D":
            return True
        if not market:
            return True

        path = self._feather_path(tf, market, symbol)
        if not os.path.exists(path):
            return True

        try:
            mtime_dt = datetime.fromtimestamp(
                os.path.getmtime(path),
                tz=timezone(timedelta(hours=8))
            )
            mtime_date = mtime_dt.strftime("%Y-%m-%d")
        except OSError:
            return True

        last_td = prev_trading_day()
        if mtime_date < last_td:
            return True
        # 盘中建的文件，盘后需刷新以包含当日确认数据
        try:
            now = datetime.now(timezone(timedelta(hours=8)))
            from datetime import time as dt_time
            if (mtime_date == _today_str()
                    and mtime_dt.time() < dt_time(15, 0)
                    and now.time() >= dt_time(15, 0)):
                return True
        except Exception:
            pass
        return False

    # ═══════════════════════════════════════════════════════════════════
    #  预热核心（使用 _prewarm_lock，不阻塞读操作）
    # ═══════════════════════════════════════════════════════════════════

    def prewarm(
        self,
        tf: str,
        all_symbols: List[str],
        fetch_func,
        market: str = "CNStock",
        batch_fetch_func=None,
    ) -> bool:
        if tf not in VALID_TIMEFRAMES:
            logger.warning(f"[KlineCache] 无效时间框架: {tf}")
            return False
        if not all_symbols:
            return False
        if not market:
            logger.warning(f"[KlineCache] 预热跳过: 无 market 信息")
            return False

        # 预热锁：防止并发预热，但不阻塞 get_cached 读操作
        with _prewarm_lock:
            return self._fetch_missing(tf, all_symbols, fetch_func, market, batch_fetch_func)

    def _concurrent_fetch_klines(
        self,
        market: str,
        symbols: List[str],
        tf: str,
        limit: int,
        fetch_func,
        max_workers: int = 8,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """并发拉取多只股票 K 线（ThreadPoolExecutor）。"""
        if not symbols:
            return {}

        result: Dict[str, List[Dict[str, Any]]] = {}
        workers = min(max_workers, len(symbols))
        lock = threading.Lock()

        def _fetch_one(sym: str) -> Optional[tuple]:
            try:
                bars = fetch_func(market, sym, tf, limit)
                if bars:
                    bars.sort(key=lambda x: x.get("time", 0))
                    return (sym, bars)
            except Exception as e:
                logger.warning(f"[KlineCache] 并发拉取 {sym} {tf} 失败: {e}")
            return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, sym): sym for sym in symbols}
            for future in as_completed(futures):
                try:
                    res = future.result()
                except Exception as e:
                    logger.warning(f"[KlineCache] 并发 future 异常: {e}")
                    continue
                if res:
                    sym, bars = res
                    with lock:
                        result[sym] = bars

        logger.info(
            f"[KlineCache] 并发拉取 {tf} 完成: "
            f"{len(result)}/{len(symbols)} 只成功 (workers={workers})"
        )
        return result

    def _fetch_missing(self, tf, symbols, fetch_func, market, batch_fetch_func=None):
        """统一拉取逻辑：按 symbol 独立判断过期 → 拉取日线 → 写入各自文件。"""
        limit = DAILY_LIMIT

        # 1) 去重
        symbols = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))

        # 2) 过滤：排除冷却期内失败的；排除有新鲜缓存的
        failed = _get_failed_symbols(tf)
        to_fetch: List[str] = []
        for sym in symbols:
            if sym in failed:
                continue
            if self.is_stale(tf, sym, market):
                to_fetch.append(sym)

        if not to_fetch:
            logger.debug(f"[KlineCache] {tf} 所有 {len(symbols)} 只缓存新鲜，无需拉取")
            return True

        logger.info(f"[KlineCache] 需拉取 {tf} {len(to_fetch)}/{len(symbols)} 只: {to_fetch[:5]}{'...' if len(to_fetch) > 5 else ''}")

        # 过滤当日未确认数据（当日 K 线在 serve 时实时合成）
        cutoff = _ts_from_date(_today_str())

        # 3) 批量拉取：batch API → 并发逐只
        raw_data: Dict[str, List[Dict[str, Any]]] = {}
        if batch_fetch_func:
            try:
                raw_data = batch_fetch_func(market, to_fetch, tf, limit)
            except Exception as e:
                logger.warning(f"[KlineCache] batch_fetch_func 失败，降级并发: {e}")

        batch_missed = [s for s in to_fetch if s not in raw_data]
        if batch_missed:
            logger.info(f"[KlineCache] batch 未覆盖 {len(batch_missed)} 只，并发补拉")
            raw_data.update(
                self._concurrent_fetch_klines(market, batch_missed, tf, limit, fetch_func)
            )

        raw_data = {s: b for s, b in raw_data.items() if b}

        # 4) 逐只写入各自文件
        success_count = 0
        for sym, bars in raw_data.items():
            filtered = []
            for bar in bars:
                if cutoff and bar.get("time", 0) >= cutoff:
                    continue
                row = {k: bar.get(k, 0) for k in KLINE_COLUMNS}
                filtered.append(row)

            if not filtered:
                continue

            df = pd.DataFrame(filtered)
            for col in KLINE_COLUMNS:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            path = self._feather_path(tf, market, sym)
            try:
                self._write_feather(path, df)
                success_count += 1
            except Exception as e:
                logger.error(f"[KlineCache] 写入 {sym} {tf} 失败: {e}")

        # 记录失败 / 清除成功
        fetched_symbols = set(raw_data.keys())
        failed_this_round = [s for s in to_fetch if s not in fetched_symbols]
        if failed_this_round:
            _record_failed_symbols(failed_this_round)
        _clear_failed_symbols(list(fetched_symbols))

        fail_count = len(to_fetch) - success_count
        logger.info(
            f"[KlineCache] 拉取 {tf} 完成: "
            f"成功 {success_count}/{len(to_fetch)} 只, 失败 {fail_count} 只"
        )
        return success_count > 0

    # ═══════════════════════════════════════════════════════════════════
    #  写入单只（降级用）
    # ═══════════════════════════════════════════════════════════════════

    def store_single(self, tf: str, market: str, symbol: str, bars: List[Dict[str, Any]]):
        """将单只股票的日线 K 线写入缓存（预热失败降级用）。

        过滤当日未确认数据：当日 K 线在 serve 时实时合成。
        无 market 时不写入，仅记录日志。
        """
        if not bars or tf != "1D":
            return
        if not market:
            logger.warning(f"[KlineCache] 跳过写入 {symbol} {tf}: 无 market 信息")
            return

        try:
            cutoff = _ts_from_date(_today_str())
        except Exception:
            return

        if cutoff:
            bars_clean = [b for b in bars if b.get("time", 0) < cutoff]
        else:
            bars_clean = list(bars)
        if not bars_clean:
            return

        new_rows = []
        for bar in bars_clean:
            row = {k: bar.get(k, 0) for k in KLINE_COLUMNS}
            new_rows.append(row)

        df = pd.DataFrame(new_rows)
        for col in KLINE_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        path = self._feather_path(tf, market, symbol)
        try:
            self._write_feather(path, df)
            logger.debug(f"[KlineCache] 降级存入 {market}:{symbol} {tf} {len(bars_clean)} 行")
        except Exception as e:
            logger.error(f"[KlineCache] 降级写入 {market}:{symbol} {tf} 失败: {e}")

    # ═══════════════════════════════════════════════════════════════════
    #  市场时段合成当日 K 线
    # ═══════════════════════════════════════════════════════════════════

    def synthesize_today_candle(
        self,
        symbol: str,
        fetch_func,
        market: str = "CNStock",
    ) -> Optional[Dict[str, Any]]:
        if not market:
            return None
        if not _is_market_hours():
            return None

        try:
            now = datetime.now(timezone(timedelta(hours=8)))
            market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
            elapsed_min = (now - market_open).total_seconds() / 60

            # 午休时段（11:30-13:00）使用上午已有的分钟线合成
            if elapsed_min < 0:
                return None

            if elapsed_min < 5:
                tf, limit = "1m", 100
            elif elapsed_min < 30:
                tf, limit = "5m", 100
            elif elapsed_min < 120:
                tf, limit = "15m", 100
            elif elapsed_min < 240:
                tf, limit = "30m", 50
            else:
                tf, limit = "1H", 20

            bars = fetch_func(market, symbol, tf, limit)
            if not bars:
                return None

            today_ts = _ts_from_date(_today_str())
            today_bars = [b for b in bars if b.get("time", 0) >= today_ts]
            if not today_bars:
                return None

            today_bars.sort(key=lambda x: x.get("time", 0))
            return {
                "time": today_ts,
                "open": _bar_field(today_bars[0], "open"),
                "high": max(_bar_field(b, "high") for b in today_bars),
                "low": min(_bar_field(b, "low") for b in today_bars),
                "close": _bar_field(today_bars[-1], "close"),
                "volume": round(sum(_bar_field(b, "volume") for b in today_bars), 2),
            }
        except Exception as e:
            logger.warning(f"[KlineCache] 合成当日失败 {market}:{symbol}: {e}")
            return None
