# -*- coding: utf-8 -*-
"""provider_pool / sina —— 榜单族新浪 HTTP 源（hot_rank）。

替代 akshare（东财 push2 被出口拦截）。直连新浪 Market_Center，纯 HTTP，零额外依赖。

数据口径:
  新浪按成交额排序的活跃股 ≈ 人气榜代理指标。
  返回字段与 akshare 版 _normalize_hot_rank 对齐（rank/stock_code/stock_name/price/change_percent）。
  popularity_score 新浪无此字段 → 置 0（诚实缺失，不伪造）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def fetch_hot_rank() -> Dict[str, Any]:
    """人气榜（新浪 Market_Center 按成交额排序，top 100）。

    返回格式与 akshare 版一致: {"rows": [{rank, stock_code, stock_name, ...}, ...]}
    """
    import urllib.request

    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeDataSimple"
        "?page=1&num=100&sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=init"
    )

    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk")
    except Exception as e:
        logger.warning("[sina] hot_rank 请求失败: %s", e)
        return {"rows": [], "error": str(e)}

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("[sina] hot_rank JSON 解析失败: %s", e)
        return {"rows": [], "error": f"JSON parse: {e}"}

    if not isinstance(data, list):
        return {"rows": [], "error": "unexpected response type"}

    rows = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "") or item.get("code", ""))
        # 提取纯 6 位代码
        m = re.search(r"(\d{6})", symbol)
        code = m.group(1) if m else symbol
        if not code:
            continue

        try:
            price = float(item.get("trade", 0) or 0)
            change_pct = float(item.get("changepercent", 0) or 0)
            amount = float(item.get("amount", 0) or 0)
            volume = float(item.get("volume", 0) or 0)
            turnover = float(item.get("turnoverratio", 0) or 0)
        except (ValueError, TypeError):
            continue

        rows.append({
            "rank": i + 1,
            "stock_code": code,
            "stock_name": str(item.get("name", "") or ""),
            "price": price,
            "change_percent": change_pct,
            "popularity_score": 0,  # 新浪无人气值字段，诚实置 0
            "current_rank_change": "",
            "amount": amount,
            "volume": volume,
            "turnover_rate": turnover,
        })

    return {"rows": rows}


from app.data_sources.provider_pool import register  # noqa: E402


@register
class SinaPoolProvider:
    name = "sina"
    priority = 10  # 优先于 akshare (50)
    markets = {"CNStock"}
    capabilities = {
        "hot_rank": fetch_hot_rank,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
