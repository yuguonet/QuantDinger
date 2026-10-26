# -*- coding: utf-8 -*-
"""provider_index / akshare —— 指数族（当前仅 2 个 kind 已迁移）。

ak 调用点与 `market_cn/index.py` 一致（:245 index spot、:891 北向汇总）。
其余 7 个 kind（index_kline / sector_flow / northbound_flow …）待迁，
`coord_index.PENDING_KINDS` 会显式报"未迁移"。
"""
from __future__ import annotations

from typing import Any, Dict

from app.data_sources.rate_limiter import get_akshare_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _rows(df) -> Dict[str, Any]:
    if df is None or getattr(df, "empty", True):
        return {"rows": []}
    return {"rows": df.to_dict("records")}


def _ak():
    import akshare as ak
    return ak


def fetch_index_quote() -> Dict[str, Any]:
    """全市场指数实时快照（ak.stock_zh_index_spot_em，同 index.py:245）。"""
    get_akshare_limiter().wait()
    env = _rows(_ak().stock_zh_index_spot_em())
    env["rows"] = [{k: v for k, v in r.items() if k} for r in env["rows"]]
    return env


def fetch_northbound_summary() -> Dict[str, Any]:
    """北向资金汇总（ak.stock_hsgt_fund_flow_summary_em，同 index.py:891）。"""
    get_akshare_limiter().wait()
    return _rows(_ak().stock_hsgt_fund_flow_summary_em())


from app.data_sources.provider_index import register   # noqa: E402


@register
class AkshareIndexProvider:
    name = "akshare"
    priority = 50
    markets = {"CNStock"}
    capabilities = {
        "index_quote": fetch_index_quote,
        "northbound_summary": fetch_northbound_summary,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
