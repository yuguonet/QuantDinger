# -*- coding: utf-8 -*-
"""provider_basic / cninfo —— 基本面族巨潮源（stock_basic）。

巨潮资讯 all_stock.json，单次请求返回全量 A 股（沪深北交所），官方数据。
与 akshare.stock_info_a_code_name / sina Market_Center 对齐字段。
"""
from __future__ import annotations

from typing import Any, Dict

from app.data_sources.normalizer import build_stock_basic_row
from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _detect_market(code: str) -> str:
    c = code[:3]
    if c in ("600", "601", "603", "605", "688", "689"):
        return "SH"
    return "SZ"


def fetch_stock_basic() -> Dict[str, Any]:
    """A股代码-名称清单（巨潮 all_stock.json）。

    返回: {"rows": [{code, name}, ...]}（与 akshare 版字段一致）
    """
    import requests

    try:
        resp = requests.get(
            "http://www.cninfo.com.cn/new/data/all_stock.json",
            headers=dict(_HTTP_HEADERS),
            timeout=20,
        )
        data = resp.json()
        stocks = data.get("stockList") or []
    except Exception as e:
        logger.warning("[cninfo] stock_basic 失败: %s", e)
        raise RuntimeError("巨潮 stock_basic 失败: %s" % e) from e

    rows = []
    for item in stocks:
        if not isinstance(item, dict):
            continue
        if item.get("category") != "A股":
            continue
        code = str(item.get("code", "")).strip()
        if code and len(code) == 6 and code.isdigit():
            row = build_stock_basic_row(code, item.get("zwjc"))
            if row["name"]:
                rows.append(row)

    if not rows:
        raise RuntimeError("巨潮 stock_basic 解析后为空")
    return {"rows": rows}


from app.data_sources.provider_basic import register  # noqa: E402


@register
class CninfoBasicProvider:
    name = "cninfo"
    priority = 15
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
