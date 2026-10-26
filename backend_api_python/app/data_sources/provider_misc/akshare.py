# -*- coding: utf-8 -*-
"""provider_misc / akshare —— 杂项族 akshare 源（trading_calendar）。

ak.trade_date_hist_sina() 获取交易日历，与 app.utils.trading_calendar 对齐。
"""
from __future__ import annotations

from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)


def fetch_trading_calendar() -> Dict[str, Any]:
    """交易日历（akshare trade_date_hist_sina）。

    返回: {"rows": [{date, is_trading_day}, ...]}
    """
    try:
        import akshare as ak
    except ImportError:
        return {"rows": [], "error": "akshare 未安装"}

    try:
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return {"rows": [], "error": "empty"}

        rows = []
        for _, r in df.iterrows():
            d = str(r.get("trade_date", ""))
            if d:
                rows.append({"date": d, "is_trading_day": True})

        return {"rows": rows}
    except Exception as e:
        logger.warning("[akshare] trading_calendar 失败: %s", e)
        return {"rows": [], "error": str(e)}


from app.data_sources.provider_misc import register  # noqa: E402


@register
class AkshareMiscProvider:
    name = "akshare"
    priority = 20
    markets = {"CNStock"}
    capabilities = {
        "trading_calendar": fetch_trading_calendar,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
