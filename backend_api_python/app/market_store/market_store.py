#!/usr/bin/env python3
"""
market_store.py — 纯存储层：Pickle 格式行情数据持久化

职责:
  1. append()   — 追加新采集的行情（含完整性检查、价格校验、缺失回填、异常检测）
  2. query()    — 按时间范围 + 类别/标的检索
  3. detect_anomalies() — 时间加权斜率急剧变化检测（% change / min）
  4. prune()    — 清理过期数据
  5. stats()    — 存储统计

数据源已上移到 plugin_api.py，本文件不负责 fetch。

存储格式:
  单个 Pickle 文件 (data/market_store.pkl)，内含完整 DataFrame。
  原子写入: 先写 .tmp 再 rename，防止写中断导致损坏。
  自动备份: 写入前将旧文件保留为 .bak。

用法:
  from market_store import MarketStore
  store = MarketStore()
  store.append(df)
  df = store.query(hours=24)
"""

from __future__ import annotations

import os
import pickle
import logging
import shutil
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env"):
    """简易 .env 加载，不依赖 python-dotenv。"""
    for search in [path, os.path.join(os.path.dirname(__file__), path)]:
        if os.path.isfile(search):
            with open(search) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("\"'")
                        os.environ.setdefault(k, v)
            break

# _load_dotenv()

RETENTION_DAYS = int(os.getenv("MARKET_RETENTION_DAYS", "15"))
DATA_DIR = Path(os.getenv("MARKET_DATA_DIR", "./data"))
STORE_FILE = "market_store.pkl"
VERBOSE = os.getenv("MARKET_VERBOSE", "1") == "1"

# 急剧变化检测配置
ANOMALY_WINDOW       = int(os.getenv("MARKET_ANOMALY_WINDOW", "15"))
ANOMALY_ZSCORE       = float(os.getenv("MARKET_ANOMALY_ZSCORE", "2.5"))
ANOMALY_MIN_PCT      = float(os.getenv("MARKET_ANOMALY_MIN_PCT", "2.0"))
ANOMALY_COOLDOWN_SEC = int(os.getenv("MARKET_ANOMALY_COOLDOWN", "600"))

# 容错 & 数据质量配置
SANITY_MAX_PCT  = float(os.getenv("MARKET_SANITY_MAX_PCT", "50.0"))
MIN_FETCH_RATIO = float(os.getenv("MARKET_MIN_FETCH_RATIO", "0.5"))
FILL_MISSING    = os.getenv("MARKET_FILL_MISSING", "1") == "1"

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("market_store")

# ---------------------------------------------------------------------------
# Schema — 统一扁平表
# ---------------------------------------------------------------------------

COLUMNS = ["timestamp", "category", "symbol", "name", "name_en",
           "price", "change_pct", "extra"]

DTYPES = {
    "category":  "string",
    "symbol":    "string",
    "name":      "string",
    "name_en":   "string",
    "price":     "float64",
    "change_pct": "float64",
    "extra":     "string",
}

EXPECTED_COUNTS = {
    "indices":     10,
    "crypto":      12,
    "forex":        8,
    "commodities":  6,
    "sentiment":    3,
}

# ---------------------------------------------------------------------------
# 数据质量检查工具函数
# ---------------------------------------------------------------------------

def _check_sanity_price(category: str, symbol: str, price: float) -> bool:
    if price is None or pd.isna(price):
        return False
    if price <= 0 or not np.isfinite(price):
        return False
    ranges = {
        "indices":     (100,     100_000),
        "crypto":      (0.0001,  500_000),
        "forex":       (0.0001,  1_000),
        "commodities": (0.01,    100_000),
        "sentiment":   (0,       500),
    }
    lo, hi = ranges.get(category, (0, 1e12))
    if price < lo or price > hi:
        log.debug("sanity reject: %s %s price=%.4f out of range [%s, %s]",
                  category, symbol, price, lo, hi)
        return False
    return True


def _check_sanity_jump(
    category: str, symbol: str,
    old_price: float, new_price: float,
) -> bool:
    if old_price <= 0 or new_price <= 0:
        return False
    pct = abs((new_price - old_price) / old_price * 100)
    if pct > SANITY_MAX_PCT:
        log.warning(
            "sanity reject: %s %s jump %.1f%% (%.4f → %.4f) exceeds %.0f%% cap",
            category, symbol, pct, old_price, new_price, SANITY_MAX_PCT,
        )
        return False
    return True


# ===================================================================
# MarketStore — 纯存储层 (Pickle)
# ===================================================================

class MarketStore:

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._store_path = self.data_dir / STORE_FILE
        self._backup_path = self.data_dir / f"{STORE_FILE}.bak"
        self._anomaly_cooldown: Dict[str, datetime] = {}
        self._last_known: Dict[str, Dict[str, Any]] = {}
        # 内存缓存: 避免重复读盘
        self._cache: Optional[pd.DataFrame] = None
        log.debug("MarketStore init, store=%s", self._store_path.resolve())

    # ----------------------------------------------------------------
    # 底层读写 — 单 Pickle 文件，原子写入 + 自动备份
    # ----------------------------------------------------------------

    def _load(self) -> pd.DataFrame:
        """从 pickle 文件加载全量数据。带内存缓存。"""
        if self._cache is not None:
            return self._cache
        if not self._store_path.exists():
            self._cache = pd.DataFrame(columns=COLUMNS)
            return self._cache
        try:
            with open(self._store_path, "rb") as f:
                df = pickle.load(f)
            # 兼容性校验
            if not isinstance(df, pd.DataFrame):
                raise ValueError(f"expected DataFrame, got {type(df)}")
            expected = set(COLUMNS)
            if not expected.issubset(set(df.columns)):
                raise ValueError(f"missing columns: {expected - set(df.columns)}")
            if len(df) > 0 and df["timestamp"].isna().all():
                raise ValueError("all timestamps are NaT")
            self._cache = df
            log.debug("loaded %d rows from %s", len(df), self._store_path.name)
            return self._cache
        except Exception as e:
            log.warning("pickle file corrupted (%s): %s — attempting backup",
                        self._store_path.name, e)
            # 尝试从备份恢复
            if self._backup_path.exists():
                try:
                    with open(self._backup_path, "rb") as f:
                        df = pickle.load(f)
                    if isinstance(df, pd.DataFrame) and set(COLUMNS).issubset(set(df.columns)):
                        self._cache = df
                        log.info("restored from backup: %d rows", len(df))
                        # 用备份覆盖损坏的主文件
                        shutil.copy2(self._backup_path, self._store_path)
                        return self._cache
                except Exception as e2:
                    log.error("backup also corrupted: %s", e2)
            # 都坏了，返回空
            self._cache = pd.DataFrame(columns=COLUMNS)
            return self._cache

    def _save(self, df: pd.DataFrame):
        """原子写入 pickle 文件。写入前备份旧文件。"""
        # 备份旧文件
        if self._store_path.exists():
            try:
                shutil.copy2(self._store_path, self._backup_path)
            except Exception as e:
                log.warning("backup failed: %s", e)

        # 原子写入: .tmp → rename
        tmp_path = self._store_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "wb") as f:
                pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp_path.replace(self._store_path)
            self._cache = df  # 更新内存缓存
            log.debug("saved %d rows to %s", len(df), self._store_path.name)
        except Exception as e:
            log.error("save failed: %s", e)
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def _invalidate_cache(self):
        """使内存缓存失效，下次 _load() 会重新读盘。"""
        self._cache = None

    # ----------------------------------------------------------------
    # 急剧变化检测
    # ----------------------------------------------------------------

    def _load_history_for_symbol(
        self, category: str, symbol: str, limit: int = 20,
    ) -> pd.DataFrame:
        """从全量数据中提取某标的的最近 N 条记录。"""
        df = self._load()
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        sub = df[(df["category"] == category) & (df["symbol"] == symbol)]
        if sub.empty:
            return pd.DataFrame(columns=COLUMNS)
        sub = sub.sort_values("timestamp")
        return sub.tail(limit).reset_index(drop=True)

    def detect_anomalies(self, new_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        基于时间加权斜率的急剧变化检测。

        核心思想:
          旧版只看 abs(pct_change)，完全忽略时间维度——数据断片时会误报。
          新版用「速度 = 涨跌幅% / 经过分钟数」作为检测指标。
          这样数据断片完全不需要特殊处理：间隔大则斜率自然低，不会误报。

        检测流程:
          1. 取每个标的的最近 N 条历史记录（含时间戳）
          2. 算历史每对相邻点的速度: speed = |Δp%| / Δt_min，建立基线 (μ, σ)
          3. 算当前新数据点的速度（相对最后一条历史记录）
          4. 两层过滤：
             - 绝对阈值: speed < 0.05 %/min → 太慢，跳过
             - 相对阈值: z = (current_speed - μ) / σ > ANOMALY_ZSCORE → 报警
        """
        if new_df.empty:
            return []

        alerts: List[Dict[str, Any]] = []
        now = datetime.now()

        # 按 (category, symbol) 分组，取每组最新一条记录待检测
        latest = (
            new_df.sort_values("timestamp")
            .groupby(["category", "symbol"])
            .last()
            .reset_index()
        )

        for _, row in latest.iterrows():
            cat  = row["category"]
            sym  = row["symbol"]
            name = row["name"]
            new_price = row["price"]

            if pd.isna(new_price) or new_price == 0:
                continue

            # === 第一步：加载历史数据，清洗时间戳和价格 ===

            hist = self._load_history_for_symbol(cat, sym, limit=ANOMALY_WINDOW)
            if len(hist) < 3:
                continue

            hist = hist.copy()
            hist["timestamp"] = pd.to_datetime(hist["timestamp"], errors="coerce")
            hist["price"] = hist["price"].dropna().astype(float)
            hist = hist.dropna(subset=["timestamp", "price"])
            if len(hist) < 2:
                continue

            hist = hist.sort_values("timestamp").reset_index(drop=True)
            prices = hist["price"].values
            timestamps = hist["timestamp"].values

            # === 第二步：算历史相邻点的速度，建立基线 ===
            #
            # 速度 = |Δp%| / Δt_minutes
            # 例：5 分钟涨 2% → speed = 0.4 %/min
            #     2 小时涨 2% → speed = 0.017 %/min（自然低，不会触发）
            #
            # 斜率本身就是时间归一化的，不需要断片过滤

            speeds: List[float] = []

            for i in range(1, len(prices)):
                prev_p = prices[i - 1]
                curr_p = prices[i]
                if prev_p == 0 or pd.isna(prev_p):
                    continue

                t_prev = pd.Timestamp(timestamps[i - 1]).timestamp()
                t_curr = pd.Timestamp(timestamps[i]).timestamp()
                elapsed_min = (t_curr - t_prev) / 60.0

                if elapsed_min <= 0:
                    continue  # 时间戳异常，跳过

                pct_chg = abs((curr_p - prev_p) / prev_p * 100.0)
                speed = pct_chg / elapsed_min
                speeds.append(speed)

            # === 第三步：算当前新数据点的速度 ===

            last_hist_price = float(prices[-1])
            if last_hist_price == 0:
                continue

            t_last_hist = pd.Timestamp(timestamps[-1]).timestamp()
            elapsed_min = (now.timestamp() - t_last_hist) / 60.0

            if elapsed_min <= 0:
                continue

            current_pct = (new_price - last_hist_price) / last_hist_price * 100
            current_speed = abs(current_pct) / elapsed_min

            # 绝对门槛：低于 0.05 %/min 不值得报警（如隔 6h 涨 0.1%）
            ABSOLUTE_SPEED_THRESHOLD = 0.05
            if current_speed < ABSOLUTE_SPEED_THRESHOLD:
                continue

            # === 第四步：Z 分数判断是否显著偏离历史基线 ===

            if len(speeds) < 2:
                # 历史样本不足，无法算基线，仅靠绝对阈值（已通过）
                z = 0.0
                mu = 0.0
                sig = 0.0
            else:
                mu  = float(np.mean(speeds))
                sig = float(np.std(speeds, ddof=1))

                if sig > 0.0001:
                    z = (current_speed - mu) / sig
                else:
                    # 历史波动极小，当前速度是均值的 2 倍以上才报
                    z = 0.0 if current_speed < mu * 2 else 99.0

                if z < ANOMALY_ZSCORE:
                    continue

            # === 第五步：冷却检查 → 生成警报 ===

            cooldown_key = f"{cat}:{sym}"
            last_alert = self._anomaly_cooldown.get(cooldown_key)
            if last_alert and (now - last_alert).total_seconds() < ANOMALY_COOLDOWN_SEC:
                continue
            self._anomaly_cooldown[cooldown_key] = now

            direction = "🔺暴涨" if current_pct > 0 else "🔻暴跌"
            severity  = "🔴" if abs(current_pct) >= ANOMALY_MIN_PCT * 3 else "🟡"
            alerts.append({
                "category":   cat, "symbol": sym, "name": name,
                "old_price":  round(last_hist_price, 6),
                "new_price":  round(new_price, 6),
                "change_pct": round(current_pct, 3),
                "speed_pct_per_min": round(current_speed, 4),
                "z_score":    round(z, 2),
                "mean_speed": round(mu, 4),
                "std_speed":  round(sig, 4),
                "direction":  direction, "severity": severity,
                "message": (
                    f"{severity} {direction} | [{cat}] {sym} ({name}) | "
                    f"{last_hist_price:.4f} → {new_price:.4f} | "
                    f"变动 {current_pct:+.3f}% | "
                    f"速度 {current_speed:.4f}%/min | "
                    f"z={z:.1f} (μ={mu:.4f} σ={sig:.4f})"
                ),
            })
        return alerts

    # ----------------------------------------------------------------
    # 内部辅助
    # ----------------------------------------------------------------

    def _build_last_known_cache(self):
        """从全量数据构建每个标的的最新值缓存。"""
        df = self._load()
        if df.empty:
            self._last_known = {}
            return
        df = df.sort_values("timestamp")
        self._last_known = {}
        for _, row in df.iterrows():
            key = f"{row['category']}:{row['symbol']}"
            self._last_known[key] = row.to_dict()
        log.debug("last_known cache: %d entries", len(self._last_known))

    def _fill_missing_symbols(
        self, df: pd.DataFrame, ts: pd.Timestamp,
    ) -> pd.DataFrame:
        if not FILL_MISSING or df.empty:
            return df
        filled_rows = []
        for category, expected_count in EXPECTED_COUNTS.items():
            cat_df = df[df["category"] == category]
            got_symbols = set(cat_df["symbol"].tolist())
            for key, last_row in self._last_known.items():
                if not key.startswith(category + ":"):
                    continue
                sym = key.split(":", 1)[1]
                if sym in got_symbols:
                    continue
                filled_rows.append({
                    "timestamp":  ts,
                    "category":   category,
                    "symbol":     sym,
                    "name":       last_row.get("name", ""),
                    "name_en":    last_row.get("name_en", ""),
                    "price":      last_row.get("price", 0),
                    "change_pct": 0,
                    "extra":      last_row.get("extra", ""),
                    "_filled":    True,
                })
        if filled_rows:
            log.info("fill_missing: backfilled %d symbols with last known values",
                     len(filled_rows))
            fill_df = pd.DataFrame(filled_rows, columns=COLUMNS + ["_filled"])
            df = df.copy()
            df["_filled"] = False
            df = pd.concat([df, fill_df], ignore_index=True)
        else:
            df = df.copy()
            df["_filled"] = False
        return df

    def _check_fetch_completeness(self, df: pd.DataFrame) -> Dict[str, Any]:
        report: Dict[str, Any] = {"by_category": {}}
        total_expected = 0
        total_got = 0
        for cat, exp in EXPECTED_COUNTS.items():
            got = len(df[df["category"] == cat]["symbol"].unique())
            ratio = got / exp if exp > 0 else 1.0
            total_expected += exp
            total_got += got
            report["by_category"][cat] = {"expected": exp, "got": got, "ratio": round(ratio, 2)}
        overall_ratio = total_got / total_expected if total_expected > 0 else 0
        report.update(total_expected=total_expected, total_got=total_got,
                      ratio=round(overall_ratio, 3),
                      ok=overall_ratio >= MIN_FETCH_RATIO)
        return report

    def _validate_and_clean(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        valid_mask = pd.Series(True, index=df.index)
        rejected = 0
        for idx, row in df.iterrows():
            cat, sym, price = row["category"], row["symbol"], row["price"]
            if not _check_sanity_price(cat, sym, price):
                valid_mask[idx] = False
                rejected += 1
                continue
            if not row.get("_filled", False):
                key = f"{cat}:{sym}"
                last = self._last_known.get(key)
                if last and last.get("price", 0) > 0:
                    if not _check_sanity_jump(cat, sym, last["price"], price):
                        valid_mask[idx] = False
                        rejected += 1
        if rejected > 0:
            log.warning("validate: rejected %d / %d rows", rejected, len(df))
        return df[valid_mask].copy()

    # ----------------------------------------------------------------
    # 核心写入
    # ----------------------------------------------------------------

    def append(self, df: pd.DataFrame):
        """
        追加新采集的行情数据。

        流程:
          1. 采集完整性检查
          2. 价格合理性 + 跳变校验
          3. 缺失标的回填
          4. 急剧变化检测
          5. 合并去重 → 写入 pickle
        """
        if df.empty:
            log.warning("append: empty dataframe, skip")
            return

        ts = pd.Timestamp.now()

        # 0. 加载历史基线
        self._build_last_known_cache()

        # 1. 完整性检查
        quality = self._check_fetch_completeness(df)
        if not quality["ok"]:
            log.warning(
                "FETCH QUALITY LOW: got %d/%d (%.0f%%) — min required %.0f%%",
                quality["total_got"], quality["total_expected"],
                quality["ratio"] * 100, MIN_FETCH_RATIO * 100,
            )

        # 2. 价格校验
        df = self._validate_and_clean(df)
        if df.empty:
            log.error("append: all rows rejected by sanity checks — skip write")
            return

        # 3. 缺失回填
        df = self._fill_missing_symbols(df, ts)

        # 4. 异常检测
        real_df = df[~df["_filled"]].copy() if "_filled" in df.columns else df
        alerts: List[Dict[str, Any]] = []
        if quality["ok"] and not real_df.empty:
            alerts = self.detect_anomalies(real_df[COLUMNS])
            for a in alerts:
                log.warning("ANOMALY >>> %s", a["message"])
        elif not quality["ok"]:
            log.info("anomaly detection SKIPPED — fetch quality too low (%.0f%%)",
                     quality["ratio"] * 100)

        # 5. 合并去重 → 写入 pickle
        df_to_write = df[COLUMNS].copy()
        df_to_write["timestamp"] = pd.to_datetime(df_to_write["timestamp"])

        existing = self._load()
        if existing.empty:
            combined = df_to_write
        else:
            combined = pd.concat([existing, df_to_write], ignore_index=True)

        combined.sort_values("timestamp", inplace=True)
        combined.drop_duplicates(
            subset=["timestamp", "category", "symbol"],
            keep="last", inplace=True,
        )
        combined.reset_index(drop=True, inplace=True)

        self._save(combined)

        # 6. 更新内存缓存
        for _, row in df_to_write.iterrows():
            key = f"{row['category']}:{row['symbol']}"
            self._last_known[key] = row.to_dict()

        filled_n = int(df["_filled"].sum()) if "_filled" in df.columns else 0
        real_n   = len(df) - filled_n
        parts = [f"{real_n} real"]
        if filled_n:
            parts.append(f"{filled_n} filled")
        if alerts:
            parts.append(f"{len(alerts)} alerts")
        log.info("append: %s | quality=%.0f%% | total=%d rows",
                 " + ".join(parts), quality["ratio"] * 100, len(combined))

    # ----------------------------------------------------------------
    # 查询
    # ----------------------------------------------------------------

    def query(
        self,
        start: str | datetime | date | None = None,
        end: str | datetime | date | None = None,
        category: str | None = None,
        symbol: str | None = None,
        hours: float | None = None,
    ) -> pd.DataFrame:
        now = datetime.now()
        if hours is not None:
            start = now - timedelta(hours=hours)
        if start is None:
            start = now - timedelta(days=RETENTION_DAYS)
        if end is None:
            end = now
        if isinstance(start, str):
            start = pd.to_datetime(start).to_pydatetime()
        if isinstance(end, str):
            end = pd.to_datetime(end).to_pydatetime()

        df = self._load()
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)

        result = df[
            (df["timestamp"] >= pd.Timestamp(start)) &
            (df["timestamp"] <= pd.Timestamp(end))
        ]
        if category:
            result = result[result["category"] == category]
        if symbol:
            result = result[result["symbol"] == symbol]
        result = result.sort_values("timestamp").reset_index(drop=True)
        return result

    # ----------------------------------------------------------------
    # 清理
    # ----------------------------------------------------------------

    def prune(self, retention_days: int | None = None) -> int:
        """清理过期数据：从 DataFrame 中删除超出保留天数的行。"""
        days = retention_days if retention_days is not None else RETENTION_DAYS
        cutoff = pd.Timestamp.now() - timedelta(days=days)

        df = self._load()
        if df.empty:
            return 0

        before_count = len(df)
        df = df[df["timestamp"] >= cutoff].reset_index(drop=True)
        pruned = before_count - len(df)

        if pruned > 0:
            self._save(df)
            log.info("pruned %d rows older than %d days (remaining: %d)",
                     pruned, days, len(df))
        else:
            log.debug("nothing to prune (retention=%d days)", days)
        return pruned

    # ----------------------------------------------------------------
    # 统计
    # ----------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        df = self._load()
        store_size = self._store_path.stat().st_size if self._store_path.exists() else 0
        backup_size = self._backup_path.stat().st_size if self._backup_path.exists() else 0

        date_min = None
        date_max = None
        if not df.empty and "timestamp" in df.columns:
            ts = df["timestamp"]
            if hasattr(ts.min(), "isoformat"):
                date_min = ts.min().isoformat()
                date_max = ts.max().isoformat()

        return {
            "data_dir": str(self.data_dir.resolve()),
            "store_file": str(self._store_path.resolve()),
            "store_size_bytes": store_size,
            "backup_size_bytes": backup_size,
            "total_rows": len(df),
            "date_min": date_min,
            "date_max": date_max,
            "retention_days": RETENTION_DAYS,
            "categories": df["category"].value_counts().to_dict() if not df.empty else {},
        }
