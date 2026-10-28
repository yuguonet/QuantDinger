# -*- coding: utf-8 -*-
"""provider / mootdx —— 个股域 TDX 直连源（kline + quote）。

⚠ 2026-10-28 停用：TDX 行情数据面已断（9 台 HQ 候选全「协议通、bars 恒空」），
本类 capabilities 的 kline/quote 已置 False，不再进任何候选链；实现原样保留，
改回 True 即恢复（见类 docstring）。

与 market_cn/index.py、coordinate_market_kline（provider/mootdx）同一 mootdx_client 管理点。
volume 单位: TDX K 线 vol 为"股"（与 kline_1m/kline_1D 口径一致）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.mootdx_client import get_client as _get_client

logger = get_logger(__name__)

# 通达信频率编码
_TDX_FREQ = {"1m": 7, "5m": 8, "15m": 9, "30m": 10, "1H": 11, "1D": 4, "1W": 5}


def _fetch_kline(code: str, timeframe: str = "1D", count: int = 250,
                 timeout: int = 10, start_date: str = "", end_date: str = "",
                 **kwargs) -> Dict[str, Any]:
    """个股 K 线（mootdx bars）。volume 为"股"。"""
    cli = _get_client()
    if cli is None:
        return {}
    freq = _TDX_FREQ.get(timeframe)
    if freq is None:
        return {}
    try:
        df = cli.bars(symbol=code, frequency=freq, start=0, offset=min(count, 800))
        if df is None or df.empty:
            return {}
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "time": str(r.get("datetime", r.get("date", ""))),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("vol", r.get("volume", 0))),  # 股
            })
        return {"bars": rows[-count:], "count": len(rows[-count:])}
    except Exception as e:
        logger.warning("[mootdx] kline %s/%s 失败: %s", code, timeframe, e)
        return {}


def _fetch_quote(code: str, timeout: int = 8, **kwargs) -> Optional[Dict[str, Any]]:
    """个股实时行情（mootdx quotes）。"""
    cli = _get_client()
    if cli is None:
        return None
    try:
        df = cli.quotes(symbol=code)
        if df is None or df.empty:
            return None
        r = df.iloc[0]
        return {
            "code": code,
            "name": str(r.get("name", "")),
            "price": float(r.get("price", 0)),
            "last_close": float(r.get("last_close", 0)),
            "open": float(r.get("open", 0)),
            "high": float(r.get("high", 0)),
            "low": float(r.get("low", 0)),
            "volume": float(r.get("vol", 0)),  # 股
        }
    except Exception as e:
        logger.warning("[mootdx] quote %s 失败: %s", code, e)
        return None


from app.data_sources.provider import register  # noqa: E402


@register
class MootdxDataSource:
    """个股域 TDX 直连源（kline + quote）。

    2026-10-28 起 **kline/quote 声明为 False（停用）**：TDX 行情数据面已断 ——
    本机实测 9 台 HQ 候选全部「协议通、bars 恒空」，`mootdx_client.get_client()`
    冷启 43.2s 后返 None（受 `_probe_data_plane` 的 bars 探活门控）。
    留着 True 的代价是链路里每条单标的取数都先白等一轮判死。

    取数实现（`_fetch_kline` / `_fetch_quote`）**原样保留**：本目录是快速拔插结构，
    把下面两处 False 改回 True 即恢复接线，无需改任何其它文件。
    """

    name = "mootdx"
    priority = 5
    max_concurrency = 1
    min_interval = 0.0
    jitter_min = 0.0
    jitter_max = 0.0

    capabilities = {
        # 停用（见类 docstring）：get_providers(capability="kline") 按真值过滤，
        # False ⇒ 本类不进候选链，链路直接从 sina/tencent 起跑。
        "kline": False,
        "kline_priority": 5,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "quote": False,
        "quote_priority": 5,
        "batch_quote": False,
        "hk": False,
        "markets": {"CNStock"},
    }

    def prepare(self) -> bool:
        """恒 False：行情已停用，不再建连探活。

        不能保留 `_get_client() is not None`：`Coordinator.prepare()`（不按
        capability 过滤）会把它当候选调一次，白付一轮 40s 判死扫描。
        """
        return False

    def fetch_kline(self, code: str, timeframe: str = "1D", count: int = 250,
                    timeout: int = 10, start_date: str = "", end_date: str = "",
                    **kwargs) -> Dict[str, Any]:
        return _fetch_kline(code, timeframe, count, timeout, start_date, end_date)

    def fetch_ticker(self, code: str, timeout: int = 8, **kwargs) -> Optional[Dict[str, Any]]:
        return _fetch_quote(code, timeout)

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10, **kwargs) -> Dict[str, Dict[str, Any]]:
        out = {}
        for c in codes:
            q = _fetch_quote(c, timeout)
            if q:
                out[c] = q
        return out
