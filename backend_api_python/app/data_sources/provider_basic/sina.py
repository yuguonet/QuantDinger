# -*- coding: utf-8 -*-
"""provider_basic / sina —— 基本面族新浪源（stock_basic）。

新浪 A 股代码-名称清单，与 akshare.stock_info_a_code_name 对齐字段。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def fetch_stock_basic() -> Dict[str, Any]:
    """A股代码-名称清单（新浪 Market_Center 全量）。

    返回: {"rows": [{code, name}, ...]}（与 akshare 版字段一致）
    """
    import urllib.request

    rows = []
    page = 1
    while True:
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeDataSimple"
            f"?page={page}&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=init"
        )
        try:
            req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
            raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="replace")
            data = json.loads(raw)
        except Exception as e:
            logger.warning("[sina] stock_basic page=%d 失败: %s", page, e)
            break

        if not isinstance(data, list) or not data:
            break

        for item in data:
            symbol = str(item.get("symbol", "") or item.get("code", ""))
            if len(symbol) == 6 and symbol.isdigit():
                rows.append({
                    "code": symbol,
                    "name": str(item.get("name", "") or ""),
                })

        if len(data) < 100:
            break
        page += 1
        if page > 60:  # 安全上限，防止死循环
            break

    return {"rows": rows}


from app.data_sources.provider_basic import register  # noqa: E402


@register
class SinaBasicProvider:
    name = "sina"
    priority = 10
    markets = {"CNStock"}
    capabilities = {
        "stock_basic": fetch_stock_basic,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
