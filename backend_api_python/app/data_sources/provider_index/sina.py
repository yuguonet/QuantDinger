# -*- coding: utf-8 -*-
"""provider_index / sina —— 指数族新浪源（index_kline, timeframe=1D/5m）。

API: quotes.sina.cn/cn/api/jsonp_v2.php/CN_MarketDataService.getKLineData
scale=240 日线 / scale=5 五分钟线，JSONP 格式。
volume 均为官方"股"口径（与 kline_index_5m 库表一致，不做换算）。
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


def _idx_prefix(code: str) -> str:
    """指数代码加 sh/sz 前缀（000/88/99→sh，其余→sz）。"""
    return "sh" if code[:3] in ("000", "88", "99") else "sz"


def _fetch_daily(code: str, limit: int) -> Dict[str, Any]:
    """指数日K线（新浪 JSONP, scale=240）。返回 {"rows": [{date, open, high, low, close, volume}, ...]}"""
    import urllib.request

    sc = _idx_prefix(code) + code
    url = (
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php"
        f"/var%20_{sc}=/CN_MarketDataService.getKLineData"
        f"?symbol={sc}&scale=240&ma=no&datalen={limit}"
    )
    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("[sina] index_kline %s 请求失败: %s", code, e)
        return {"rows": [], "error": str(e)}

    # 清理 JS 包裹
    clean = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", "", clean)
    m = re.search(r"=\s*\(?\s*(\[.*\])\s*\)?\s*;?\s*$", clean, re.DOTALL)
    if not m:
        return {"rows": [], "error": "no JSON array"}

    try:
        arr = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError) as e:
        return {"rows": [], "error": f"JSON parse: {e}"}

    rows = []
    for r in arr:
        try:
            rows.append({
                "date": str(r.get("day", "")),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("volume", 0)),  # 股（对齐 kline_index_5m）
            })
        except (ValueError, TypeError):
            continue

    return {"rows": rows}


def _fetch_5m(code: str, limit: int) -> Dict[str, Any]:
    """指数 5m K线（新浪 CN_MarketData.getKLineData, scale=5）。

    返回 {"rows": [{time, open, high, low, close, volume}, ...]}，time 为 bar
    **结束时刻**（"YYYY-MM-DD HH:MM"，09:35 起，与 kline_index_5m 同口径），
    volume=股（官方口径，与 kline_1D 日线精确一致，勿做倍数换算）。

    易错点（2026-09-12 sync_index_minute 实测，原样继承）：
      - 5001 根大响应偶发读超时 → timeout=40 + 4 次重试
      - 无效代码返回非列表 JSON → 校验 isinstance(list)
      - 服务器硬顶 datalen=5001（≈104 个交易日）
    """
    import json
    import time as _time
    import urllib.request

    sc = _idx_prefix(code) + code
    limit = max(1, min(int(limit or 5001), 5001))
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sc}&scale=5&ma=no&datalen={limit}"
    )
    data = None
    last_err = ""
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last_err = str(e)
            if attempt == 3:
                logger.warning("[sina] index_kline 5m %s 请求失败: %s", code, e)
                return {"rows": [], "error": last_err}
            _time.sleep(3)
    if not isinstance(data, list) or not data:
        return {"rows": [], "error": "no list data"}

    rows = []
    for r in data:
        try:
            day = str(r.get("day", r.get("d", "")))
            rows.append({
                "time": day[:16],  # "YYYY-MM-DD HH:MM"（bar 结束时刻）
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("volume", 0)),  # 股（对齐 kline_index_5m）
            })
        except (ValueError, TypeError):
            continue
    return {"rows": rows[-limit:]}


def fetch_index_kline(code: str, timeframe: str = "1D", limit: int = 250) -> Dict[str, Any]:
    """指数K线（timeframe 分发：1D → scale=240 / 5m → scale=5）。"""
    from app.data_sources.normalizer import normalize_chart_timeframe
    from app.data_sources.dispatch import NotSupported

    tf = normalize_chart_timeframe(timeframe)
    if tf == "1D":
        return _fetch_daily(code, limit)
    if tf == "5m":
        return _fetch_5m(code, limit)
    return NotSupported("index_kline/%s" % tf)


from app.data_sources.provider_index import register  # noqa: E402


@register
class SinaIndexProvider:
    name = "sina"
    priority = 15
    markets = {"CNStock"}
    capabilities = {
        "index_kline": fetch_index_kline,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
