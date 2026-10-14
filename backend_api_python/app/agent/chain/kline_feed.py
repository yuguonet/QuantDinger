"""K 线统一取数（v5 P2 接线，2026-10-13）

## 为什么要有这个模块

项目约定：**重复调用代码段 2 处以上 ⇒ 生成统一接口**。K 线取数此前已有 3 处：

1. `chain/evaluator._get_actual_return` —— 内联 `CNStockDataSource`，要 `t` 为**日期串**
2. `skills/market-screener/common.py::fetch_kline` —— 技能侧
3. 本阶段 C3P 的 `p_base` —— 需要 `time` 为 **int 时间戳**

⇒ 本模块**同时给 `t`（`YYYY-MM-DD`）与 `time`（int 时间戳）两个键**，两个消费方都能吃。
   只给一个键必然导致某一侧在调用点偷偷转换 —— 而偷偷转换正是"静默漂移"的老套路。

## 易错处

· ★ `app.watchlist.predict._by_day` 用 `datetime.fromtimestamp(int(k["time"]))`
  ⇒ `time` **必须**是 int 时间戳。给成日期串不会报错，只会在 `beta_regime` 里
  静默返回 `{"beta": None, "corr": None, "regime": "unknown"}` —— 大盘权重退化，看不出来。
· ★ `predict.MIN_BARS = 60` ⇒ 根数不足时 `predict_next_day` 直接返回 `None`（**不报错**）。
  所以本模块的默认根数（120）远大于下限，且调用方**不许**把 `None` 兜底成 0.5。
· 取不到日期的行**整行丢弃**，不猜、不填占位符（占位日期会污染 base_idx 定位）。
· 大盘 K 线走 `app.watchlist.predict.load_market_klines`（已是 int 时间戳形状），
  **不要自己再取一遍指数** —— 那是第二条口径，两边必然漂移。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..log import logger

DEFAULT_BARS = 120          # 默认取数根数（远大于 predict.MIN_BARS=60，留余量）
_INDEX_CODE = "000001"      # 上证，与 context_frame._INDEX_CODE 同口径


def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def bars_default() -> int:
    """默认取数根数（env `QD_C3P_BARS`，脏值回落默认，不崩）。"""
    return _env_int("QD_C3P_BARS", DEFAULT_BARS)


def _to_date_str(v: Any) -> Optional[str]:
    """把 cn_stock 行的 `date` / `time` 统一成 `YYYY-MM-DD`；无法解析返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # 纯数字 ⇒ 当时间戳
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s)).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None
    s = s[:10]
    try:
        # 校验能被 datetime 解析（拒绝 "09-28" 这类缺年份的串）
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def _to_ts(date_str: str) -> Optional[int]:
    """`YYYY-MM-DD` → int 时间戳（本地时区，与 `load_market_klines` 同口径）。"""
    try:
        return int(datetime.strptime(date_str[:10], "%Y-%m-%d").timestamp())
    except (ValueError, TypeError):
        return None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x else default      # NaN 归默认


def daily_bars(code: str, n: Optional[int] = None) -> List[Dict[str, Any]]:
    """个股日线，返回 `[{t, time, open, high, low, close, volume}, ...]`（按时间升序）。

    - `t`   : `YYYY-MM-DD`（给 `evaluator` 这类按日期定位的消费方）
    - `time`: int 时间戳（给 `predict_next_day` 这类按时间戳解析的消费方）
    - 失败/无数据返回 `[]`（**不抛**），调用方按"取不到"处理。
    """
    code = str(code or "").strip()
    if not code:
        return []
    _n = int(n or bars_default())
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        raw = CNStockDataSource().get_kline(code, "1D", _n)
    except Exception as e:
        logger.debug("[KlineFeed] 取 %s 日线失败(fail-open): %s: %s", code, type(e).__name__, e)
        return []

    out: List[Dict[str, Any]] = []
    for k in (raw or []):
        if not isinstance(k, dict):
            continue
        d = _to_date_str(k.get("date") if k.get("date") else k.get("time"))
        if not d:
            continue                      # ★ 日期解析不出 ⇒ 整行丢，不猜
        ts = _to_ts(d)
        if ts is None:
            continue
        out.append({
            "t": d,
            "time": ts,
            "open": _f(k.get("open")),
            "high": _f(k.get("high")),
            "low": _f(k.get("low")),
            "close": _f(k.get("close")),
            "volume": _f(k.get("volume")),
        })
    return out


def market_bars(n: Optional[int] = None) -> List[Dict[str, Any]]:
    """大盘日线（上证 `000001`），形状已是 `predict` 要的 `time`(int ts) + OHLC。

    失败返回 `[]` ⇒ `predict_next_day` 会退化成"仅个股独立分"，**不会崩**（设计如此）。
    """
    _n = int(n or bars_default())
    try:
        from app.watchlist.predict import load_market_klines
        return list(load_market_klines(_n) or [])
    except Exception as e:
        logger.debug("[KlineFeed] 取大盘日线失败(fail-open): %s: %s", type(e).__name__, e)
        return []


def enough(bars: Optional[Sequence[Any]], minimum: int = 60) -> bool:
    """根数是否够模型用。单独拎出来是因为「够不够」这个判断散在各处会漂移。"""
    return bool(bars) and len(bars) >= int(minimum)
