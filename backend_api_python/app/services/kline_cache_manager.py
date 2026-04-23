# -*- coding: utf-8 -*-
"""
K线 Feather 缓存管理器

按日/周/月分别存储 K 线数据为本地 feather 文件。

核心特性：
  - 惰性加载：优先读本地 feather 缓存，缓存未命中才走远程
  - 增量更新：只拉取缺失的股票，完全过期才全量拉取
  - 批量预热：遍历所有用户自选股去重后批量拉取
  - 市场时段合成：用 1m/5m/15m/30m/1H 合成当日未完成 K 线
  - 预热频率：日线1天1次 / 周线1周1次 / 月线1月1次
"""

import os
import shutil
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import pandas as pd
import pyarrow.feather as feather

from app.utils.logger import get_logger
from app.interfaces.trading_calendar import is_trading_day_today, prev_trading_day

logger = get_logger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────────

DAILY_LIMIT = 1500       # 日线：约 5 年
WEEKLY_LIMIT = 520       # 周线：约 10 年
MONTHLY_LIMIT = 240      # 月线：约 20 年

STALE_THRESHOLD = {
    "1D": 1 * 86400,
    "1W": 7 * 86400,
    "1M": 30 * 86400,
}

KLINE_COLUMNS = ["time", "open", "high", "low", "close", "volume"]
VALID_TIMEFRAMES = {"1D", "1W", "1M"}

# 预热锁：独立于文件锁，避免预热期间阻塞读操作
_prewarm_lock = threading.Lock()


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


# ─── 聚合函数（不修改原列表） ───────────────────────────────────────

def aggregate_daily_to_weekly(daily_bars: List[Dict[str, Any]], limit: int = WEEKLY_LIMIT) -> List[Dict[str, Any]]:
    if not daily_bars:
        return []
    bars = sorted(daily_bars, key=lambda x: x.get("time", 0))
    groups: Dict[int, List[Dict]] = {}
    order: List[int] = []
    for bar in bars:
        t = bar.get("time", 0)
        if not t:
            continue
        wk = _iso_week_start(t)
        if wk not in groups:
            groups[wk] = []
            order.append(wk)
        groups[wk].append(bar)
    result = []
    for wk in order:
        chunk = groups[wk]
        if not chunk:
            continue
        result.append({
            "time": wk,
            "open": _bar_field(chunk[0], "open"),
            "high": max(_bar_field(b, "high") for b in chunk),
            "low": min(_bar_field(b, "low") for b in chunk),
            "close": _bar_field(chunk[-1], "close"),
            "volume": round(sum(_bar_field(b, "volume") for b in chunk), 2),
        })
    return result[-limit:] if len(result) > limit else result


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
    K线本地 feather 缓存管理器。

    文件布局：
      kline_1D_{YYYY-MM-DD}.feather   日线快照
      kline_1W_{YYYY-MM-DD}.feather   周线快照
      kline_1M_{YYYY-MM-DD}.feather   月线快照

    锁策略：
      _file_lock  — 保护文件读写（短期持有）
      _prewarm_lock（模块级） — 保护预热流程（长期持有，不阻塞读）
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "kline_cache")
        os.makedirs(self.data_dir, exist_ok=True)
        self._file_lock = threading.Lock()

    # ═══════════════════════════════════════════════════════════════════
    #  文件路径 & 读写
    # ═══════════════════════════════════════════════════════════════════

    def _feather_path(self, tf: str, date_str: str) -> str:
        return os.path.join(self.data_dir, f"kline_{tf}_{date_str}.feather")

    def _list_feather_files(self, tf: str) -> List[str]:
        """安全列出指定周期的 feather 文件名"""
        prefix = f"kline_{tf}_"
        try:
            return [f for f in os.listdir(self.data_dir)
                    if f.startswith(prefix) and f.endswith(".feather")]
        except OSError as e:
            logger.warning(f"[KlineCache] 列目录失败 {self.data_dir}: {e}")
            return []

    def _find_latest_file(self, tf: str) -> Optional[str]:
        best_date, best_path = "", None
        for fname in self._list_feather_files(tf):
            d = fname[len(f"kline_{tf}_"):-8]
            if d > best_date:
                best_date = d
                best_path = os.path.join(self.data_dir, fname)
        return best_path

    def _find_latest_date(self, tf: str) -> Optional[str]:
        best = ""
        for fname in self._list_feather_files(tf):
            d = fname[len(f"kline_{tf}_"):-8]
            if d > best:
                best = d
        return best or None

    def _read_feather(self, path: str) -> pd.DataFrame:
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

    def _read_latest(self, tf: str) -> pd.DataFrame:
        return self._read_feather(self._find_latest_file(tf))

    def _write_feather(self, tf: str, date_str: str, df: pd.DataFrame):
        """原子写入（先备份 → 写 tmp → 验证 → rename）。调用方需确保线程安全。"""
        path = self._feather_path(tf, date_str)
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
            self._cleanup_old(tf, keep=3)

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

    def _cleanup_old(self, tf: str, keep: int = 3):
        files = []
        for fname in self._list_feather_files(tf):
            fpath = os.path.join(self.data_dir, fname)
            try:
                files.append((os.path.getmtime(fpath), fpath))
            except OSError:
                continue
        files.sort(reverse=True)
        for _, fpath in files[keep:]:
            try:
                os.remove(fpath)
            except OSError:
                pass

    # ═══════════════════════════════════════════════════════════════════
    #  缓存查询（只读，不持锁 — 依赖原子文件操作）
    # ═══════════════════════════════════════════════════════════════════

    def get_cached(self, tf: str, symbol: str) -> Optional[List[Dict[str, Any]]]:
        if tf not in VALID_TIMEFRAMES:
            return None
        df = self._read_latest(tf)
        if df.empty or "symbol" not in df.columns:
            return None
        try:
            sym_df = df[df["symbol"] == symbol].sort_values("time")
        except Exception:
            return None
        if sym_df.empty:
            return None
        return sym_df[KLINE_COLUMNS].to_dict("records")

    def get_cached_symbols(self, tf: str) -> Set[str]:
        if tf not in VALID_TIMEFRAMES:
            return set()
        df = self._read_latest(tf)
        if df.empty or "symbol" not in df.columns:
            return set()
        try:
            return set(df["symbol"].dropna().unique())
        except Exception:
            return set()

    def is_stale(self, tf: str) -> bool:
        """根据交易日历判断缓存是否过期。

        日线：缓存日期不是今天（或上一个交易日）→ 过期
        周线：缓存日期早于本周第一个交易日 → 过期
        月线：缓存日期早于本月第一个交易日 → 过期
        """
        if tf not in VALID_TIMEFRAMES:
            return True
        cached_date = self._find_latest_date(tf)
        if not cached_date:
            return True

        today = _today_str()

        if tf == "1D":
            # 盘中或盘前：缓存日期不是今天 → 需要增量更新
            # 盘后：缓存日期不是今天 → 需要增量更新（收盘数据）
            if cached_date < today:
                return True
            return False

        if tf == "1W":
            # 本周第一个交易日
            try:
                this_week_start = _iso_week_start(_now_ts())
                this_week_start_str = datetime.fromtimestamp(
                    this_week_start, tz=timezone(timedelta(hours=8))
                ).strftime("%Y-%m-%d")
                # 用实际交易日来判断：找到本周一之后的第一个交易日
                # 简化：如果缓存日期早于本周一 → 过期
                if cached_date < this_week_start_str:
                    return True
                return False
            except Exception:
                return True

        if tf == "1M":
            try:
                this_month_start = _month_start(_now_ts())
                this_month_start_str = datetime.fromtimestamp(
                    this_month_start, tz=timezone(timedelta(hours=8))
                ).strftime("%Y-%m-%d")
                if cached_date < this_month_start_str:
                    return True
                return False
            except Exception:
                return True

        # fallback：使用 STALE_THRESHOLD
        try:
            cached_ts = _ts_from_date(cached_date)
        except Exception:
            return True
        return (_now_ts() - cached_ts) > STALE_THRESHOLD.get(tf, 86400)

    # ═══════════════════════════════════════════════════════════════════
    #  预热核心（使用 _prewarm_lock，不阻塞读操作）
    # ═══════════════════════════════════════════════════════════════════

    def prewarm(
        self,
        tf: str,
        all_symbols: List[str],
        fetch_func,
        market: str = "CNStock",
    ) -> bool:
        if tf not in VALID_TIMEFRAMES:
            logger.warning(f"[KlineCache] 无效时间框架: {tf}")
            return False
        if not all_symbols:
            return False

        # 预热锁：防止并发预热，但不阻塞 get_cached 读操作
        with _prewarm_lock:
            if self.is_stale(tf):
                return self._full_fetch(tf, all_symbols, fetch_func, market)
            else:
                return self._incremental_fetch(tf, all_symbols, fetch_func, market)

    def _full_fetch(self, tf, symbols, fetch_func, market):
        limit = {"1D": DAILY_LIMIT, "1W": WEEKLY_LIMIT, "1M": MONTHLY_LIMIT}.get(tf, DAILY_LIMIT)
        logger.info(f"[KlineCache] 全量拉取 {tf} {len(symbols)} 只股票")

        # 盘中：过滤当天未完成数据，只存历史已完成 K 线
        in_market = tf == "1D" and _is_market_hours()
        today_start = _ts_from_date(_today_str()) if in_market else 0

        all_rows = []
        ok = 0
        for sym in symbols:
            try:
                bars = fetch_func(market, sym, tf, limit)
                if bars:
                    for bar in bars:
                        # 盘中跳过当天未完成 bar
                        if in_market and bar.get("time", 0) >= today_start:
                            continue
                        row = {k: bar.get(k, 0) for k in KLINE_COLUMNS}
                        row["symbol"] = sym
                        all_rows.append(row)
                    ok += 1
            except Exception as e:
                logger.warning(f"[KlineCache] 拉取 {sym} {tf} 失败: {e}")

        if not all_rows:
            logger.warning(f"[KlineCache] 全量拉取 {tf} 无数据")
            return False

        df = pd.DataFrame(all_rows)
        for col in KLINE_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["symbol"] = df["symbol"].astype(str)

        with self._file_lock:
            self._write_feather(tf, _today_str(), df)
        logger.info(f"[KlineCache] 全量拉取 {tf} 完成: {ok}/{len(symbols)}, {len(df)} 行")
        return True

    def _incremental_fetch(self, tf, symbols, fetch_func, market):
        cached = self.get_cached_symbols(tf)
        missing = [s for s in symbols if s not in cached]

        if not missing:
            logger.debug(f"[KlineCache] {tf} 缓存完整，无需拉取")
            return True

        limit = {"1D": DAILY_LIMIT, "1W": WEEKLY_LIMIT, "1M": MONTHLY_LIMIT}.get(tf, DAILY_LIMIT)
        logger.info(f"[KlineCache] 增量拉取 {tf} {len(missing)} 只缺失股票")

        # 盘中：过滤当天未完成数据，与 _full_fetch 行为一致
        in_market = tf == "1D" and _is_market_hours()
        today_start = _ts_from_date(_today_str()) if in_market else 0

        # 锁外拉取：逐只获取，按 symbol 分组收集成功行
        fetched_rows: List[Dict[str, Any]] = []    # 所有成功拉到的原始行
        fetched_symbols: List[str] = []             # 成功拉到数据的 symbol 列表
        fail_count = 0

        for sym in missing:
            try:
                bars = fetch_func(market, sym, tf, limit)
                if bars:
                    for bar in bars:
                        # 盘中跳过当天未完成 bar
                        if in_market and bar.get("time", 0) >= today_start:
                            continue
                        row = {k: bar.get(k, 0) for k in KLINE_COLUMNS}
                        row["symbol"] = sym
                        fetched_rows.append(row)
                    fetched_symbols.append(sym)
                else:
                    # 空数据不算成功，下次预热仍会重试
                    logger.debug(f"[KlineCache] {sym} {tf} 返回空数据，跳过")
            except Exception as e:
                fail_count += 1
                logger.warning(f"[KlineCache] 拉取 {sym} {tf} 失败: {e}")

        if not fetched_rows:
            logger.info(f"[KlineCache] 增量拉取 {tf} 无新数据 (失败 {fail_count} 只)")
            # 全部失败 → 返回 False，让上层降级
            return fail_count < len(missing)

        new_df = pd.DataFrame(fetched_rows)
        for col in KLINE_COLUMNS:
            if col in new_df.columns:
                new_df[col] = pd.to_numeric(new_df[col], errors="coerce")
        new_df["symbol"] = new_df["symbol"].astype(str)

        # 锁内：读 → 合并 → 写（原子操作，避免竞态）
        with self._file_lock:
            existing_df = self._read_latest(tf)
            if not existing_df.empty and "symbol" in existing_df.columns:
                # 先去掉本次已成功拉取的 symbol 的旧数据，再合并
                # 避免同一个 symbol 在新旧 DataFrame 中产生重复
                if fetched_symbols:
                    existing_df = existing_df[~existing_df["symbol"].isin(fetched_symbols)]
                merged = pd.concat([existing_df, new_df], ignore_index=True)
            else:
                merged = new_df
            merged = merged.drop_duplicates(subset=["time", "symbol"], keep="last")
            merged = merged.sort_values(["symbol", "time"])
            self._write_feather(tf, _today_str(), merged)

        logger.info(
            f"[KlineCache] 增量拉取 {tf} 完成: "
            f"成功 {len(fetched_symbols)}/{len(missing)} 只, "
            f"失败 {fail_count} 只, {len(fetched_rows)} 行"
        )
        return True

    # ═══════════════════════════════════════════════════════════════════
    #  写入单只（降级用，线程安全）
    # ═══════════════════════════════════════════════════════════════════

    def store_single(self, tf: str, symbol: str, bars: List[Dict[str, Any]]):
        """将单只股票的 K 线写入缓存（预热失败降级用）。

        盘中时段：去掉当天未完成数据，只存历史已完成 K 线
        盘后/非交易日：保留全部数据（当天已收盘）
        """
        if not bars or tf not in VALID_TIMEFRAMES:
            return

        try:
            today_start = _ts_from_date(_today_str())
        except Exception:
            return

        if _is_market_hours():
            # 盘中：去掉当天数据（未完成），只存历史
            bars_clean = [b for b in bars if b.get("time", 0) < today_start]
        else:
            # 盘后或非交易日：保留全部（含当天已完成 K 线）
            bars_clean = list(bars)
        if not bars_clean:
            return

        new_rows = []
        for bar in bars_clean:
            row = {k: bar.get(k, 0) for k in KLINE_COLUMNS}
            row["symbol"] = symbol
            new_rows.append(row)
        new_df = pd.DataFrame(new_rows)
        for col in KLINE_COLUMNS:
            if col in new_df.columns:
                new_df[col] = pd.to_numeric(new_df[col], errors="coerce")
        new_df["symbol"] = new_df["symbol"].astype(str)

        with self._file_lock:
            existing_df = self._read_latest(tf)
            if existing_df.empty or "symbol" not in existing_df.columns:
                merged = new_df
            else:
                old_without = existing_df[existing_df["symbol"] != symbol]
                merged = pd.concat([old_without, new_df], ignore_index=True)
            merged = merged.drop_duplicates(subset=["time", "symbol"], keep="last")
            merged = merged.sort_values(["symbol", "time"])
            self._write_feather(tf, _today_str(), merged)

        logger.debug(f"[KlineCache] 降级存入 {symbol} {tf} {len(bars_clean)} 行")

    # ═══════════════════════════════════════════════════════════════════
    #  市场时段合成当日 K 线
    # ═══════════════════════════════════════════════════════════════════

    def synthesize_today_candle(
        self,
        symbol: str,
        fetch_func,
        market: str = "CNStock",
    ) -> Optional[Dict[str, Any]]:
        if not _is_market_hours():
            return None

        try:
            now = datetime.now(timezone(timedelta(hours=8)))
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed_min = (now - market_open).total_seconds() / 60

            # 午休时段（11:30-13:00）按上午收盘处理
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
            logger.warning(f"[KlineCache] 合成当日失败 {symbol}: {e}")
            return None
