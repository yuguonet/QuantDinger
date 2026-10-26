# -*- coding: utf-8 -*-
"""provider_index / sina —— 指数族新浪源（index_kline）。

API: quotes.sina.cn/cn/api/jsonp_v2.php/CN_MarketDataService.getKLineData
scale=240 表示日线，JSONP 格式。volume 原始"股"→÷100 转"手"对齐 mootdx 口径。
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


def fetch_index_kline(code: str, days: int = 250) -> Dict[str, Any]:
    """指数日K线（新浪 JSONP）。返回 {"rows": [{date, open, high, low, close, volume}, ...]}"""
    import urllib.request

    sc = _idx_prefix(code) + code
    url = (
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php"
        f"/var%20_{sc}=/CN_MarketDataService.getKLineData"
        f"?symbol={sc}&scale=240&ma=no&datalen={days}"
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
