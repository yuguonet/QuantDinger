# -*- coding: utf-8 -*-
"""provider_tape / sina —— 分笔与盘口族新浪源（orderbook + ticks_today）。

新浪 hq.sinajs.cn 五档盘口 + stock.gtimg.cn 分笔。
字段与 mootdx/tencent 对齐: orderbook → 标准行情字段（last/change/changePercent/… + bid/ask）;
ticks → {time,price,vol,amount,buy_sell}
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def _sina_code(code: str) -> str:
    """纯代码 → 新浪前缀（sh600519 / sz000001）。"""
    c = (code or "").strip()
    if c[:1] in ("6", "9"):
        return f"sh{c}"
    return f"sz{c}"


# ── orderbook (五档盘口) ────────────────────────────────────

def _orderbook(code: str) -> Dict[str, Any]:
    """五档盘口（新浪 hq.sinajs.cn）。

    返回 normalize_quote 格式: {last, change, changePercent, high, low, open,
    previousClose, volume, time, name, symbol, bid, ask}
    """
    import urllib.request

    sc = _sina_code(code)
    url = f"https://hq.sinajs.cn/list={sc}"
    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("[sina] orderbook %s 失败: %s", code, e)
        return {"object": None}

    # 解析: var hq_str_sh600519="贵州茅台,1349.98,...";
    m = re.search(r'"([^"]+)"', raw)
    if not m:
        return {"object": None}
    parts = m.group(1).split(",")
    if len(parts) < 32:
        return {"object": None}

    try:
        name = parts[0]
        open_p = float(parts[1])
        prev = float(parts[2])
        price = float(parts[3])
        high = float(parts[4])
        low = float(parts[5])
        vol = float(parts[8])  # 成交量(股)
        amount = float(parts[9])  # 成交额(元)
        # 五档: 买一~五档 (parts[10:20]), 卖一~五档 (parts[20:30])
        # ⚠ 新浪是「量在前、价在后」：parts[10]=买一量 parts[11]=买一价，
        #   parts[20]=卖一量 parts[21]=卖一价（2026-10-28 原始串实测复核）。
        #   早期实现把二者取反，导致 price 字段是股数、vol 字段是价格。
        bid = {}
        ask = {}
        for i in range(5):
            bv = float(parts[10 + i * 2] or 0)
            bp = float(parts[11 + i * 2] or 0)
            sv = float(parts[20 + i * 2] or 0)
            sp = float(parts[21 + i * 2] or 0)
            bid[f"bid{i+1}"] = {"price": bp, "vol": bv}
            ask[f"ask{i+1}"] = {"price": sp, "vol": sv}
    except (ValueError, IndexError):
        return {"object": None}

    # 标准字段（normalize_quote 的目标形状）此处全部显式给齐，无需再归一化
    result = {
        "last": price,
        "change": round(price - prev, 4) if prev else 0,
        "changePercent": round((price - prev) / prev * 100, 2) if prev else 0,
        "high": high, "low": low, "open": open_p,
        "previousClose": prev,
        "volume": vol,
        "time": "",
        "name": name,
        "symbol": code,
        "bid": bid,
        "ask": ask,
    }
    return {"object": result}


# ── ticks_today (当日分笔) ─────────────────────────────────

def fetch_ticks_today(code: str, limit: int = 2000) -> Dict[str, Any]:
    """当日分笔成交（新浪 stock.gtimg.cn，与 tencent 同格式）。

    每条: index/time/price/change/vol/amount/direction
    归一化: buy_sell "0"=买 "1"=卖 "2"=中性
    """
    import urllib.request

    sc = _sina_code(code)
    url = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={sc}&p=0&l={min(limit, 2000)}"

    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("[sina] ticks_today %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}

    m = re.search(r'"([^"]+)"', raw)
    if not m:
        return {"rows": [], "error": "no data"}

    out = []
    for seg in m.group(1).split("|"):
        parts = seg.split("/")
        if len(parts) < 6:
            continue
        try:
            t = parts[1]
            p = float(parts[2])
            v = float(parts[4])
            a = float(parts[5])
            bs_raw = parts[6] if len(parts) > 6 else ""
        except (ValueError, IndexError):
            continue

        bs_upper = str(bs_raw).upper()
        if bs_upper == "B":
            buy_sell = "0"
        elif bs_upper == "S":
            buy_sell = "1"
        else:
            buy_sell = "2"

        out.append({"time": t, "price": p, "vol": v, "amount": a, "buy_sell": buy_sell})

    return {"rows": out[:limit]}


def fetch_ticks_history(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """历史分笔（新浪无此 API）。"""
    return {"rows": [], "error": "新浪无历史分笔 API"}


from app.data_sources.provider_tape import register  # noqa: E402


@register
class SinaTapeProvider:
    name = "sina"
    priority = 15
    markets = {"CNStock"}
    capabilities = {
        "orderbook": _orderbook,
        "ticks_today": fetch_ticks_today,
        "ticks_history": fetch_ticks_history,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
