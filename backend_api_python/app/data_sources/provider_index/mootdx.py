# -*- coding: utf-8 -*-
"""provider_index / mootdx —— 指数族 TDX 直连源（index_kline + index_quote）。

mootdx index_bars() 拉取指数日线，index 实时行情走 quotes()。
volume 单位: TDX 指数 vol 为"手"，与 tencent/sina 对齐。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.mootdx_client import get_client as _get_client

logger = get_logger(__name__)


def fetch_index_kline(code: str, days: int = 250) -> Dict[str, Any]:
    """指数日K线（mootdx index_bars，frequency=4=日线）。"""
    cli = _get_client()
    if cli is None:
        return {"rows": [], "error": "mootdx 不可用"}
    try:
        df = cli.index_bars(symbol=code, frequency=4, start=0, offset=min(days, 800))
        if df is None or df.empty:
            return {"rows": [], "error": "empty"}
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "date": str(r.get("datetime", r.get("date", "")))[:10],
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("vol", r.get("volume", 0))),
            })
        return {"rows": rows[-days:]}
    except Exception as e:
        logger.warning("[mootdx] index_kline %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}


def fetch_index_quote(codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """指数实时行情（mootdx quotes）。"""
    cli = _get_client()
    if cli is None:
        return {"rows": [], "error": "mootdx 不可用"}
    if not codes:
        codes = ["000001", "399001", "399006", "000300"]
    rows = []
    for c in codes:
        try:
            df = cli.quotes(symbol=c)
            if df is not None and not df.empty:
                r = df.iloc[0]
                rows.append({
                    "code": c,
                    "name": str(r.get("name", "")),
                    "price": float(r.get("price", 0)),
                    "last_close": float(r.get("last_close", 0)),
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "volume": float(r.get("vol", 0)),
                })
        except Exception as e:
            logger.debug("[mootdx] index_quote %s: %s", c, e)
    return {"rows": rows}


from app.data_sources.provider_index import register  # noqa: E402


@register
class MootdxIndexProvider:
    name = "mootdx"
    priority = 10
    markets = {"CNStock"}
    capabilities = {
        "index_kline": fetch_index_kline,
        "index_quote": fetch_index_quote,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
