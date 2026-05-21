"""
大盘基准模块 — 按市场分基准 + 情绪评分

═══════════════════════════════════════════════════════════════════════
  定位
═══════════════════════════════════════════════════════════════════════

  1. beta 基准收益（去噪用）
     创业板股票 → 创业板指为主，上证为辅
     科创板股票 → 科创50为主，上证为辅
     沪主板股票 → 上证为主，深证为辅
     ...

     用途：alpha = 个股收益 - 对应市场基准收益

  2. 情绪评分（保留，可作他用）
     5 指标加权 → 0~100 分 + 标签

═══════════════════════════════════════════════════════════════════════
  市场 → 指数映射
═══════════════════════════════════════════════════════════════════════

  沪主板 (60xxxx.SH)  → 上证指数(主 0.7) + 深证成指(辅 0.3)
  深主板 (00xxxx.SZ)  → 深证成指(主 0.7) + 上证指数(辅 0.3)
  创业板 (30xxxx.SZ)  → 创业板指(主 0.7) + 上证指数(辅 0.3)
  科创板 (68xxxx.SH)  → 科创50(主 0.7)   + 上证指数(辅 0.3)
  北交所 (8xxxxx/4x)  → 北证50(主 0.7)   + 上证指数(辅 0.3)

  上证作为全局因子占 30%，反映整个 A 股的系统性风险。
═══════════════════════════════════════════════════════════════════════

  用法
═══════════════════════════════════════════════════════════════════════

  from optimizer.market_sentiment import MarketBenchmark

  mb = MarketBenchmark()

  # 指定标的的基准收益（自动判断市场）
  ret = mb.get_benchmark_return("300750.SZ", "2026-05-20")
  # → 创业板指*0.7 + 上证*0.3 的当日收益

  # 算 alpha
  alpha = mb.get_alpha("300750.SZ", stock_return=0.05, date="2026-05-20")

  # 批量（回测用）
  returns = mb.get_benchmark_returns("300750.SZ", "2024-01-01", "2026-05-20")

  # 情绪评分（保留）
  score = mb.get_sentiment_score("2026-05-20")  # → 62.3
  label = mb.get_sentiment_label("2026-05-20")  # → "偏多"
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# ================================================================
#  配置
# ================================================================

INDICES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50":  "sh000688",
    "北证50":  "bj899050",
}

# 市场 → 基准指数权重
# 格式: {市场标识: {指数名: 权重}}
# 上证作为全局因子占 30%，本地市场指数占 70%
MARKET_BENCHMARKS = {
    "SH_MAIN":  {"上证指数": 0.70, "深证成指": 0.30},   # 沪主板
    "SZ_MAIN":  {"深证成指": 0.70, "上证指数": 0.30},   # 深主板
    "CHI_NEXT": {"创业板指": 0.70, "上证指数": 0.30},   # 创业板
    "STAR":     {"科创50":  0.70, "上证指数": 0.30},    # 科创板
    "BSE":      {"北证50":  0.70, "上证指数": 0.30},    # 北交所
}

# 情绪评分用的全局权重（5指数加权，不区分市场）
SENTIMENT_WEIGHTS = {
    "上证指数": 0.30,
    "深证成指": 0.25,
    "创业板指": 0.20,
    "科创50":  0.13,
    "北证50":  0.12,
}

# 子指标权重（情绪评分用）
FACTOR_WEIGHTS = {
    "ma_score":   0.30,
    "momentum":   0.25,
    "volume":     0.25,
    "volatility": 0.20,
}

MA_SHORT, MA_LONG = 20, 60
MOM_SHORT, MOM_LONG = 5, 20
VOL_SHORT, VOL_LONG = 5, 20
VOL_WINDOW = 20

LABEL_THRESHOLDS = [
    (70, "强势"), (60, "偏多"), (45, "中性"), (35, "偏空"), (0, "弱势"),
]

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sentiment_cache")
_HTTP_TIMEOUT = 15
_API_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
)


# ================================================================
#  市场判断
# ================================================================

def _detect_market(symbol: str) -> str:
    """
    根据股票代码判断所属市场。

    Returns: MARKET_BENCHMARKS 的 key
    """
    # 去掉后缀
    code = symbol.split(".")[0] if "." in symbol else symbol

    if code.startswith("68"):
        return "STAR"       # 科创板
    elif code.startswith("30"):
        return "CHI_NEXT"   # 创业板
    elif code.startswith(("8", "4")):
        return "BSE"        # 北交所
    elif code.startswith("6"):
        return "SH_MAIN"    # 沪主板
    elif code.startswith(("0", "2")):
        return "SZ_MAIN"    # 深主板
    return "SH_MAIN"  # 默认沪主板


# ================================================================
#  数据拉取
# ================================================================

def _fetch_index_daily(symbol: str, datalen: int = 2000) -> List[dict]:
    import urllib.request
    url = _API_URL.format(symbol=symbol, datalen=datalen)
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected response for {symbol}: {raw[:200]}")
    data.sort(key=lambda x: x["day"])
    return data


def _load_cached_data(symbol: str, datalen: int = 2000, max_age_hours: int = 12) -> List[dict]:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, f"{symbol}_{datalen}.json")
    if os.path.isfile(cache_file):
        age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    data = _fetch_index_daily(symbol, datalen)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


# ================================================================
#  指标计算（情绪评分用）
# ================================================================

def _to_float_array(records: List[dict], key: str) -> np.ndarray:
    return np.array([float(r[key]) for r in records])


def _compute_ma(values: np.ndarray, period: int) -> np.ndarray:
    ma = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return ma
    cumsum = np.cumsum(values)
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


def _compute_returns(close: np.ndarray, period: int) -> np.ndarray:
    ret = np.full_like(close, np.nan, dtype=float)
    if len(close) <= period:
        return ret
    ret[period:] = (close[period:] - close[:-period]) / close[:-period]
    return ret


def _compute_vol_ratio(volume: np.ndarray, short: int, long: int) -> np.ndarray:
    ratio = np.full_like(volume, np.nan, dtype=float)
    if len(volume) < long:
        return ratio
    ma_short = _compute_ma(volume.astype(float), short)
    ma_long = _compute_ma(volume.astype(float), long)
    valid = ma_long > 0
    ratio[valid] = ma_short[valid] / ma_long[valid]
    return ratio


def _compute_volatility(close: np.ndarray, window: int) -> np.ndarray:
    returns = np.diff(close) / close[:-1]
    vol = np.full(len(close), np.nan, dtype=float)
    if len(returns) < window:
        return vol
    for i in range(window, len(returns) + 1):
        vol[i] = np.std(returns[i - window:i])
    result = np.full(len(close), np.nan, dtype=float)
    result[1:] = vol[1:]
    return result


def _normalize_0_100(value: float, low: float, high: float) -> float:
    if high <= low:
        return 50.0
    return max(0.0, min(100.0, (value - low) / (high - low) * 100))


def _compute_index_sentiment(records: List[dict]) -> List[Dict[str, Any]]:
    """单指数逐日情绪分"""
    if len(records) < MA_LONG + 5:
        return []

    close = _to_float_array(records, "close")
    volume_arr = _to_float_array(records, "volume")

    ma20 = _compute_ma(close, MA_SHORT)
    ma60 = _compute_ma(close, MA_LONG)
    mom5 = _compute_returns(close, MOM_SHORT)
    mom20 = _compute_returns(close, MOM_LONG)
    vol_ratio = _compute_vol_ratio(volume_arr, VOL_SHORT, VOL_LONG)
    vol20 = _compute_volatility(close, VOL_WINDOW)

    results = []
    for i in range(MA_LONG + 5, len(records)):
        c = close[i]
        ma20_pct = (c - ma20[i]) / ma20[i] if ma20[i] > 0 else 0
        ma60_pct = (c - ma60[i]) / ma60[i] if ma60[i] > 0 else 0
        ma_score = _normalize_0_100(ma20_pct, -0.05, 0.05) * 0.6 + \
                   _normalize_0_100(ma60_pct, -0.10, 0.10) * 0.4

        m5 = mom5[i] if not np.isnan(mom5[i]) else 0
        m20 = mom20[i] if not np.isnan(mom20[i]) else 0
        momentum = _normalize_0_100(m5, -0.08, 0.08) * 0.5 + \
                   _normalize_0_100(m20, -0.15, 0.15) * 0.5

        vr = vol_ratio[i] if not np.isnan(vol_ratio[i]) else 1.0
        volume_score = _normalize_0_100(vr, 0.5, 2.0)

        v20 = vol20[i] if not np.isnan(vol20[i]) else 0.02
        volatility_score = 100 - _normalize_0_100(v20, 0.005, 0.03)

        score = (
            ma_score * FACTOR_WEIGHTS["ma_score"]
            + momentum * FACTOR_WEIGHTS["momentum"]
            + volume_score * FACTOR_WEIGHTS["volume"]
            + volatility_score * FACTOR_WEIGHTS["volatility"]
        )
        score = max(0.0, min(100.0, score))

        label = "中性"
        for threshold, lbl in LABEL_THRESHOLDS:
            if score >= threshold:
                label = lbl
                break

        results.append({
            "date": records[i]["day"],
            "close": round(c, 3),
            "ma_score": round(ma_score, 1),
            "momentum": round(momentum, 1),
            "volume": round(volume_score, 1),
            "volatility": round(volatility_score, 1),
            "score": round(score, 1),
            "label": label,
        })
    return results


# ================================================================
#  主类
# ================================================================

class MarketBenchmark:
    """
    大盘基准 + 情绪评分。

    两套 API：
      1. get_benchmark_return(symbol, date) — 按市场分的基准收益（去噪用）
      2. get_sentiment_score(date) — 情绪评分（可作他用）
    """

    def __init__(self, datalen: int = 2000, max_cache_age_hours: int = 12):
        self._datalen = datalen
        self._max_cache_age = max_cache_age_hours
        # 原始数据: index_name → {date: float}
        self._index_closes: Dict[str, Dict[str, float]] = {}
        self._index_returns: Dict[str, Dict[str, float]] = {}
        # 情绪评分: index_name → [records]
        self._index_sentiments: Dict[str, List[Dict[str, Any]]] = {}
        # 综合情绪: date → record
        self._sentiment_by_date: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        for index_name, symbol in INDICES.items():
            try:
                raw = _load_cached_data(symbol, self._datalen, self._max_cache_age)
                self._index_closes[index_name] = {r["day"]: float(r["close"]) for r in raw}
                # 计算日收益率
                returns = {}
                for i in range(1, len(raw)):
                    prev = float(raw[i - 1]["close"])
                    curr = float(raw[i]["close"])
                    if prev > 0:
                        returns[raw[i]["day"]] = (curr - prev) / prev
                self._index_returns[index_name] = returns
                # 情绪评分
                self._index_sentiments[index_name] = _compute_index_sentiment(raw)
            except Exception as e:
                print(f"⚠️ 拉取 {index_name} ({symbol}) 失败: {e}")
                self._index_returns[index_name] = {}
                self._index_closes[index_name] = {}
                self._index_sentiments[index_name] = []

        self._compute_overall_sentiment()
        self._loaded = True

    def _compute_overall_sentiment(self):
        """计算综合情绪分（5指数加权）"""
        all_dates = set()
        for records in self._index_sentiments.values():
            for r in records:
                all_dates.add(r["date"])

        index_maps = {
            name: {r["date"]: r for r in records}
            for name, records in self._index_sentiments.items()
        }

        for date in sorted(all_dates):
            weighted_sum = 0.0
            weight_total = 0.0
            details = {}
            for name in INDICES:
                w = SENTIMENT_WEIGHTS.get(name, 0.1)
                rec = index_maps.get(name, {}).get(date)
                if rec:
                    weighted_sum += rec["score"] * w
                    weight_total += w
                    details[name] = {
                        "score": rec["score"], "label": rec["label"],
                        "close": rec["close"], "ma_score": rec["ma_score"],
                        "momentum": rec["momentum"], "volume": rec["volume"],
                        "volatility": rec["volatility"],
                    }
            overall = weighted_sum / weight_total if weight_total > 0 else 50.0
            overall = max(0.0, min(100.0, overall))
            label = "中性"
            for threshold, lbl in LABEL_THRESHOLDS:
                if overall >= threshold:
                    label = lbl
                    break
            self._sentiment_by_date[date] = {
                "date": date, "overall": round(overall, 1),
                "label": label, "indices": details,
            }

    # ── 基准收益 API（按市场分）──────────────────────────────

    def get_benchmark_return(self, symbol: str, date: str) -> float:
        """
        获取某只股票对应市场的基准收益率。

        创业板股票 → 创业板指*0.7 + 上证*0.3
        科创板股票 → 科创50*0.7 + 上证*0.3
        ...

        Args:
            symbol: 股票代码，如 "300750.SZ"
            date: "YYYY-MM-DD"

        Returns:
            float, 基准收益率
        """
        self._ensure_loaded()
        market = _detect_market(symbol)
        weights = MARKET_BENCHMARKS.get(market, MARKET_BENCHMARKS["SH_MAIN"])

        weighted_return = 0.0
        weight_total = 0.0
        for index_name, w in weights.items():
            ret = self._index_returns.get(index_name, {}).get(date, 0.0)
            weighted_return += ret * w
            weight_total += w

        return weighted_return / weight_total if weight_total > 0 else 0.0

    def get_benchmark_returns(
        self, symbol: str, start_date: str, end_date: str
    ) -> Dict[str, float]:
        """批量获取基准收益（回测用）"""
        self._ensure_loaded()
        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d")

        market = _detect_market(symbol)
        weights = MARKET_BENCHMARKS.get(market, MARKET_BENCHMARKS["SH_MAIN"])

        # 取所有指数的日期并集
        all_dates = set()
        for index_name in weights:
            all_dates.update(self._index_returns.get(index_name, {}).keys())

        result = {}
        for date in sorted(all_dates):
            dt = datetime.strptime(date, "%Y-%m-%d")
            if not (dt_start <= dt <= dt_end):
                continue
            weighted_return = 0.0
            weight_total = 0.0
            for index_name, w in weights.items():
                ret = self._index_returns.get(index_name, {}).get(date, 0.0)
                weighted_return += ret * w
                weight_total += w
            result[date] = weighted_return / weight_total if weight_total > 0 else 0.0

        return result

    def get_benchmark_cumulative(
        self, symbol: str, date: str, days: int = 20
    ) -> float:
        """截至 date 的近 N 个交易日累计基准收益"""
        returns = self.get_benchmark_returns(
            symbol,
            (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days * 2)).strftime("%Y-%m-%d"),
            date,
        )
        sorted_dates = sorted(returns.keys())
        if not sorted_dates:
            return 0.0

        # 取最近 days 个
        window = sorted_dates[-days:]
        cum = 1.0
        for d in window:
            cum *= (1 + returns[d])
        return cum - 1.0

    def get_alpha(self, symbol: str, stock_return: float, date: str) -> float:
        """个股 alpha = 个股收益 - 对应市场基准收益"""
        return stock_return - self.get_benchmark_return(symbol, date)

    def get_regime(self, symbol: str, date: str) -> Dict[str, Any]:
        """市场状态（基于对应市场基准）"""
        self._ensure_loaded()
        cum_20 = self.get_benchmark_cumulative(symbol, date, 20)
        cum_5 = self.get_benchmark_cumulative(symbol, date, 5)

        if cum_20 > 0.02:
            trend = "up"
        elif cum_20 < -0.015:
            trend = "down"
        else:
            trend = "flat"

        # 波动率
        returns = self.get_benchmark_returns(
            symbol,
            (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d"),
            date,
        )
        sorted_dates = sorted(returns.keys())
        window_dates = sorted_dates[-20:] if len(sorted_dates) >= 20 else sorted_dates
        returns_window = [returns[d] for d in window_dates]
        vol = float(np.std(returns_window)) if returns_window else 0.01

        if vol < 0.008:
            volatility = "low"
        elif vol > 0.02:
            volatility = "high"
        else:
            volatility = "normal"

        strength = min(1.0, abs(cum_20) / 0.10)
        if (cum_20 > 0 and cum_5 > 0) or (cum_20 < 0 and cum_5 < 0):
            strength = min(1.0, strength * 1.2)

        return {"trend": trend, "volatility": volatility, "strength": round(strength, 2)}

    # ── 情绪评分 API（保留）──────────────────────────────────

    def get_sentiment_score(self, date: str) -> float:
        """综合情绪分 0~100"""
        self._ensure_loaded()
        rec = self._sentiment_by_date.get(date)
        if rec:
            return rec["overall"]
        # 找最近交易日
        dt = datetime.strptime(date, "%Y-%m-%d")
        for i in range(1, 11):
            prev = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
            rec = self._sentiment_by_date.get(prev)
            if rec:
                return rec["overall"]
        return 50.0

    def get_sentiment_label(self, date: str) -> str:
        """情绪标签"""
        score = self.get_sentiment_score(date)
        for threshold, lbl in LABEL_THRESHOLDS:
            if score >= threshold:
                return lbl
        return "中性"

    def get_sentiment_detail(self, date: str) -> Optional[Dict[str, Any]]:
        """情绪详情（含各指数子指标）"""
        self._ensure_loaded()
        return self._sentiment_by_date.get(date)

    def get_index_sentiment(self, index_name: str, date: str) -> Optional[Dict[str, Any]]:
        """单个指数的情绪详情"""
        self._ensure_loaded()
        for r in self._index_sentiments.get(index_name, []):
            if r["date"] == date:
                return r
        return None

    # ── 通用 ─────────────────────────────────────────────────

    def get_all_dates(self) -> List[str]:
        self._ensure_loaded()
        all_dates = set()
        for returns in self._index_returns.values():
            all_dates.update(returns.keys())
        return sorted(all_dates)

    def summary(self) -> Dict[str, Any]:
        self._ensure_loaded()
        dates = self.get_all_dates()
        if not dates:
            return {"error": "无数据"}
        return {
            "date_range": f"{dates[0]} ~ {dates[-1]}",
            "total_days": len(dates),
            "indices": {
                name: len(rets) for name, rets in self._index_returns.items()
            },
            "sentiment_latest": self._sentiment_by_date.get(dates[-1]),
        }


# ================================================================
#  CLI
# ================================================================

def main():
    import sys

    print("🚀 大盘基准模块")
    print("=" * 60)

    mb = MarketBenchmark()
    s = mb.summary()

    print(f"\n📊 数据概况:")
    print(f"   日期范围: {s['date_range']}")
    print(f"   交易日数: {s['total_days']}")
    print(f"   各指数数据量:")
    for name, count in s["indices"].items():
        print(f"     {name}: {count} 条")

    # 最新一天各市场基准
    latest = s["date_range"].split(" ~ ")[-1]
    print(f"\n📅 最新: {latest}")
    for label, market_key, test_sym in [
        ("沪主板", "SH_MAIN", "600519.SH"),
        ("深主板", "SZ_MAIN", "000001.SZ"),
        ("创业板", "CHI_NEXT", "300750.SZ"),
        ("科创板", "STAR", "688001.SH"),
        ("北交所", "BSE", "830799.BJ"),
    ]:
        ret = mb.get_benchmark_return(test_sym, latest)
        print(f"   {label} 基准: {ret*100:+.4f}%")

    # 情绪
    score = mb.get_sentiment_score(latest)
    lbl = mb.get_sentiment_label(latest)
    print(f"\n   综合情绪: {score} ({lbl})")

    # 指定日期
    if len(sys.argv) > 1:
        q = sys.argv[1]
        print(f"\n📅 {q}:")
        for label, test_sym in [
            ("沪主板", "600519.SH"), ("创业板", "300750.SZ"), ("科创", "688001.SH"),
        ]:
            ret = mb.get_benchmark_return(test_sym, q)
            print(f"   {label} 基准: {ret*100:+.4f}%")
        print(f"   情绪: {mb.get_sentiment_score(q)} ({mb.get_sentiment_label(q)})")

    print("\n✅ 完成")


if __name__ == "__main__":
    main()
