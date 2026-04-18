#!/usr/bin/env python3
"""
market_store.py — 纯存储层：Feather 格式行情数据持久化

职责:
  1. append()   — 追加新采集的行情（含完整性检查、价格校验、缺失回填、异常检测）
  2. query()    — 按时间范围 + 类别/标的检索
  3. detect_anomalies() — z-score 急剧变化检测
  4. prune()    — 清理过期数据
  5. stats()    — 存储统计

数据源已上移到 plugin_api.py，本文件不负责 fetch。

用法:
  from market_store import MarketStore
  store = MarketStore()
  store.append(df)
  df = store.query(hours=24)
"""

from __future__ import annotations

import os
import json
import logging
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

RETENTION_DAYS = int(os.getenv("FEATHER_RETENTION_DAYS", "15"))
DATA_DIR = Path(os.getenv("FEATHER_DATA_DIR", "./data/feather"))
VERBOSE = os.getenv("FEATHER_VERBOSE", "1") == "1"

# 急剧变化检测配置
ANOMALY_WINDOW       = int(os.getenv("FEATHER_ANOMALY_WINDOW", "15"))
ANOMALY_ZSCORE       = float(os.getenv("FEATHER_ANOMALY_ZSCORE", "2.5"))
ANOMALY_MIN_PCT      = float(os.getenv("FEATHER_ANOMALY_MIN_PCT", "2.0"))
ANOMALY_COOLDOWN_SEC = int(os.getenv("FEATHER_ANOMALY_COOLDOWN", "600"))

# 容错 & 数据质量配置
SANITY_MAX_PCT  = float(os.getenv("FEATHER_SANITY_MAX_PCT", "50.0"))
MIN_FETCH_RATIO = float(os.getenv("FEATHER_MIN_FETCH_RATIO", "0.5"))
FILL_MISSING    = os.getenv("FEATHER_FILL_MISSING", "1") == "1"

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
# Feather Schema — 统一扁平表
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
# MarketStore — 纯存储层
# ===================================================================

class MarketStore:

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._anomaly_cooldown: Dict[str, datetime] = {}
        self._last_known: Dict[str, Dict[str, Any]] = {}
        log.debug("MarketStore init, data_dir=%s", self.data_dir.resolve())

    # ---- 文件路径 ----

    def _file_for_date(self, d: date) -> Path:
        return self.data_dir / f"market_{d.isoformat()}.feather"

    def _date_from_file(self, p: Path) -> Optional[date]:
        try:
            ds = p.stem.replace("market_", "")
            return date.fromisoformat(ds)
        except Exception:
            return None

    # ---- 读写 ----

    def _read_file(self, path: Path) -> Optional[pd.DataFrame]:
        if not path.exists():
            return None
        try:
            df = pd.read_feather(path)
            expected = set(COLUMNS)
            if not expected.issubset(set(df.columns)):
                raise ValueError(f"missing columns: {expected - set(df.columns)}")
            if len(df) > 0 and df["timestamp"].isna().all():
                raise ValueError("all timestamps are NaT")
            return df
        except Exception as e:
            log.warning("feather file corrupted (%s): %s — deleting & rebuilding", path.name, e)
            try:
                path.unlink()
            except Exception:
                pass
            return None

    def _write_file(self, path: Path, df: pd.DataFrame):
        tmp = path.with_suffix(".tmp")
        df.to_feather(tmp)
        tmp.replace(path)
        log.debug("wrote %s (%d rows)", path.name, len(df))

    def _load_day(self, d: date) -> pd.DataFrame:
        path = self._file_for_date(d)
        df = self._read_file(path)
        return df if df is not None else pd.DataFrame(columns=COLUMNS)

    def _save_day(self, d: date, df: pd.DataFrame):
        self._write_file(self._file_for_date(d), df)

    # ---- 急剧变化检测 ----

    def _load_history_for_symbol(
        self, category: str, symbol: str, limit: int = 20,
    ) -> pd.DataFrame:
        today = date.today()
        frames: List[pd.DataFrame] = []
        collected = 0
        for offset in range(RETENTION_DAYS + 1):
            d = today - timedelta(days=offset)
            df = self._load_day(d)
            if df.empty:
                continue
            sub = df[(df["category"] == category) & (df["symbol"] == symbol)]
            if sub.empty:
                continue
            frames.append(sub)
            collected += len(sub)
            if collected >= limit:
                break
        if not frames:
            return pd.DataFrame(columns=COLUMNS)
        result = pd.concat(frames, ignore_index=True)
        result.sort_values("timestamp", inplace=True)
        return result.tail(limit).reset_index(drop=True)

    def detect_anomalies(self, new_df: pd.DataFrame) -> List[Dict[str, Any]]:
        if new_df.empty:
            return []
        alerts: List[Dict[str, Any]] = []
        now = datetime.now()
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
            hist = self._load_history_for_symbol(cat, sym, limit=ANOMALY_WINDOW)
            if len(hist) < 3:
                continue
            prices = hist["price"].dropna().astype(float).values
            if len(prices) < 2:
                continue
            pct_changes = np.diff(prices) / prices[:-1] * 100
            pct_changes = pct_changes[np.isfinite(pct_changes)]
            if len(pct_changes) < 2:
                continue
            mu  = float(np.mean(pct_changes))
            sig = float(np.std(pct_changes, ddof=1))
            last_hist_price = float(prices[-1])
            current_pct = (new_price - last_hist_price) / last_hist_price * 100
            if sig > 0.001:
                z = abs(current_pct - mu) / sig
            else:
                z = 0.0 if abs(current_pct) < ANOMALY_MIN_PCT else 99.0
            if z < ANOMALY_ZSCORE or abs(current_pct) < ANOMALY_MIN_PCT:
                continue
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
                "z_score":    round(z, 2),
                "mean_pct":   round(mu, 4),
                "std_pct":    round(sig, 4),
                "direction":  direction, "severity": severity,
                "message": (
                    f"{severity} {direction} | [{cat}] {sym} ({name}) | "
                    f"{last_hist_price:.4f} → {new_price:.4f} | "
                    f"变动 {current_pct:+.3f}% | z={z:.1f} (μ={mu:.4f} σ={sig:.4f})"
                ),
            })
        return alerts

    # ---- 内部辅助 ----

    def _build_last_known_cache(self):
        today = date.today()
        self._last_known = {}
        for offset in range(3):
            d = today - timedelta(days=offset)
            df = self._load_day(d)
            if df.empty:
                continue
            df = df.sort_values("timestamp")
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

    # ---- 核心写入 ----

    def append(self, df: pd.DataFrame):
        """
        追加新采集的行情数据。

        流程:
          1. 采集完整性检查
          2. 价格合理性 + 跳变校验
          3. 缺失标的回填
          4. 急剧变化检测
          5. 写入 feather 文件
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

        # 5. 写入 feather
        df_to_write = df[COLUMNS].copy()
        df_to_write["timestamp"] = pd.to_datetime(df_to_write["timestamp"])
        for day, group in df_to_write.groupby(df_to_write["timestamp"].dt.date):
            existing = self._load_day(day)
            combined = pd.concat([existing, group], ignore_index=True)
            combined.sort_values("timestamp", inplace=True)
            combined.drop_duplicates(
                subset=["timestamp", "category", "symbol"],
                keep="last", inplace=True,
            )
            combined.reset_index(drop=True, inplace=True)
            self._save_day(day, combined)

        # 6. 更新缓存
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
        log.info("append: %s | quality=%.0f%%", " + ".join(parts),
                 quality["ratio"] * 100)

    # ---- 查询 ----

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
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end

        frames = []
        cur = start_d
        while cur <= end_d:
            df = self._load_day(cur)
            if not df.empty:
                frames.append(df)
            cur += timedelta(days=1)
        if not frames:
            return pd.DataFrame(columns=COLUMNS)

        result = pd.concat(frames, ignore_index=True)
        result = result[result["timestamp"] >= pd.Timestamp(start)]
        result = result[result["timestamp"] <= pd.Timestamp(end)]
        if category:
            result = result[result["category"] == category]
        if symbol:
            result = result[result["symbol"] == symbol]
        result.sort_values("timestamp", inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    # ---- 清理 ----

    def prune(self, retention_days: int | None = None) -> int:
        days = retention_days if retention_days is not None else RETENTION_DAYS
        cutoff = date.today() - timedelta(days=days)
        deleted = 0
        for f in sorted(self.data_dir.glob("market_*.feather")):
            d = self._date_from_file(f)
            if d and d < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                    log.info("pruned old file: %s", f.name)
                except Exception as e:
                    log.warning("failed to delete %s: %s", f.name, e)
        if deleted:
            log.info("pruned %d files older than %d days", deleted, days)
        return deleted

    # ---- 统计 ----

    def stats(self) -> Dict[str, Any]:
        files = sorted(self.data_dir.glob("market_*.feather"))
        total_rows = 0
        date_range = []
        for f in files:
            df = self._read_file(f)
            if df is not None:
                total_rows += len(df)
                d = self._date_from_file(f)
                if d:
                    date_range.append(d)
        return {
            "data_dir": str(self.data_dir.resolve()),
            "file_count": len(files),
            "total_rows": total_rows,
            "date_min": min(date_range).isoformat() if date_range else None,
            "date_max": max(date_range).isoformat() if date_range else None,
            "retention_days": RETENTION_DAYS,
        }
