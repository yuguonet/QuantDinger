# -*- coding: utf-8 -*-
"""app/watchlist/overlay.py — 实时薄覆盖（方案 §8）

原则：**盘后事实 + 请求时薄覆盖层**，不是"整段改成请求时重算"。

| 字段 | 覆盖 | 依据 |
|---|---|---|
| `price` / `change_pct` | ✅ | 实时快照（与 `/watchlist/prices` 同源，带 TTL） |
| 关键位 `dist_pct` | ✅ | `f(实时现价, 库内关键位)` —— 纯 O(1) 算术 |
| 支撑/压力位**位置本身** | ❌ | 慢变量（盘后事实），盘中不改 |
| `score` | ❌ | **评分必须稳定才能回归**；口径各异更不该盘中混 |
| 扩展段 | ❌ | 上级/自算的事实，盘中不改 |

⚠️ **本批未覆盖 `当日量比`（已知缺口，非遗忘）**：它需要「既往均量」这一基线，
而基线目前**没有持久化载体**（label 表无对应列，`extras` 契约里只有展示单元；
实时快照只有当日累计 volume，无基线）。为不擅自扩张 schema/契约，此处**显式留缺**并记录在案，
待基线落点定下后再补。**不得**用"盘中重算整段序列"绕开（违反 §8 纪律）。

纪律：本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)


def fetch_quotes(pairs: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """批量取实时快照。失败的市场/票**静默缺省**（读路径回落到盘后事实，不阻断）。"""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_market: Dict[str, List[str]] = {}
    for market, symbol in pairs:
        by_market.setdefault(market, []).append(symbol)

    for market, symbols in by_market.items():
        try:
            if market == "CNStock":
                from app.data_sources.cn_stock import CNStockDataSource
                quotes = CNStockDataSource().get_tickers(symbols) or []
                found = {q.get("symbol"): q for q in quotes}
                for sym in symbols:
                    q = found.get(sym)
                    if q:
                        out[(market, sym)] = q
            else:
                from app.data_sources import DataSourceFactory
                for sym in symbols:
                    try:
                        q = DataSourceFactory.get_ticker(market, sym)
                        if q:
                            out[(market, sym)] = q
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("[label] 实时快照失败 %s: %s", market, e)
    return out


def quote_view(quote: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """实时快照 → 覆盖字段（现价 / 涨跌% / 时点）。缺价 ⇒ None（= 用盘后事实）。"""
    if not quote:
        return None
    price = quote.get("last") or quote.get("close")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    cp = quote.get("changePercent")
    try:
        cp = round(float(cp), 4) if cp is not None else None
    except (TypeError, ValueError):
        cp = None
    return {"price": price, "change_pct": cp, "quote_asof": datetime.now()}


def overlay_levels(items: Iterable[Dict[str, Any]], price: float, *, is_support: bool
                   ) -> List[Dict[str, Any]]:
    """用实时现价重算每个关键位的 `dist_pct`（**位置本身不动**）。

    不变式：`price` 不变时输出与输入逐位相同。
    """
    out: List[Dict[str, Any]] = []
    for it in items:
        d = dict(it)
        try:
            p = float(d["price"])
            d["dist_pct"] = round(((price - p) / price) if is_support else ((p - price) / price), 4)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
        out.append(d)
    return out
