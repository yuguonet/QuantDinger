"""
东财智能选股搜索 — 外围门面（纯接线，不含任何 HTTP）

远端实现在 `app/data_sources/provider_pool/em_search.py`，本文件只做：
  关键词校验 → 一次 `coord_pool("em_stock_search")` → 按历史出参形状包装。

所有调用方（dragon_limit / cards.* / agent screener_tools）的 import 与返回结构不变：
  {"code": 1, "keyword", "total", "page_no", "page_size", "stocks": [...]}
失败: {"code": 0, "msg", "stocks": []}
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def search_stocks(
    keyword: str,
    page_size: int = 200,
    page_no: int = 1,
    timeout: int = 15,
) -> Dict[str, Any]:
    """
    调东财智能选股接口，返回标准化结果。

    Args:
        keyword:   搜索关键词（自然语言，如 "市盈率低于20的科技股"）
        page_size: 每页条数，最大 200
        page_no:   页码
        timeout:   历史参数，**已不生效** —— 超时统一由
                   `QD_EXT_TIMEOUT_EM_STOCK_SEARCH` 控制（data_sources 层）。

    Returns:
        {
            "code": 1,
            "keyword": str,
            "total": int,
            "page_no": int,
            "page_size": int,
            "stocks": [
                {
                    "code": "600519", "name": "贵州茅台",
                    "industry": "", "concept": "",
                    "new_price": 1800.0, "change_rate": 1.25,
                    "high_price": 1810.0, "low_price": 1790.0,
                    "pre_close_price": 1780.0,
                    "volume": 123456.0, "deal_amount": "223456789",
                    "volume_ratio": 1.2, "turnoverrate": 0.35,
                    "amplitude": 1.12, "pe9": 33.5, "pbnewmrq": 10.2,
                    "total_market_cap": "2.26e12", "free_cap": "2.26e12",
                },
                ...
            ]
        }
    """
    if not keyword or not keyword.strip():
        return {"code": 0, "msg": "keyword 不能为空", "stocks": []}

    from app.data_sources.coord_pool import coordinate

    out = coordinate("em_stock_search", keyword=keyword.strip(),
                     page_size=page_size, page_no=page_no)
    data = out.get("data") or {}
    stocks = [s for s in (data.get("rows") or []) if isinstance(s, dict)]
    if not out.get("ok"):
        logger.warning("[东财搜索] 失败: %s", out.get("error"))
        return {"code": 0, "msg": out.get("error") or "搜索失败", "stocks": []}

    return {
        "code": 1,
        "keyword": keyword.strip(),
        "total": data.get("total") or len(stocks),
        "page_no": page_no,
        "page_size": page_size,
        "stocks": stocks,
    }
