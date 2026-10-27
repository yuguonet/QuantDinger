# -*- coding: utf-8 -*-
"""provider_index / mootdx —— 指数族 TDX 直连源（index_kline 1D/5m + index_quote）。

mootdx index_bars() 拉指数K线（frequency=4 日线 / 0 五分钟），index 实时行情走 quotes()。
volume 单位: TDX 指数日线 vol 为"手"，与 tencent/sina 对齐。

⚠ 5m 的 vol 字段错位（2026-09-12 定案, tmp/_em_crosscheck.out）：
  pytdx GetIndexBars 对指数分钟包解码 "vol" 实际是 成交额(元)/100，**不是成交量**。
  OHLC/涨跌家数解码正确 ⇒ 5m 只作 sina 死亡时的价格兜底（tf_priority 5m=40，
  必须排在 sina 之后），volume 值带系统性偏差，后续 sina 同步窗口 UPSERT 会归一。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.mootdx_client import get_client as _get_client

logger = get_logger(__name__)


def _fetch_daily(code: str, limit: int) -> Dict[str, Any]:
    """指数日K线（mootdx index_bars，frequency=4=日线）。"""
    cli = _get_client()
    if cli is None:
        return {"rows": [], "error": "mootdx 不可用"}
    try:
        df = cli.index_bars(symbol=code, frequency=4, start=0, offset=min(limit, 800))
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
        return {"rows": rows[-limit:]}
    except Exception as e:
        logger.warning("[mootdx] index_kline %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}


def _fetch_5m(code: str, limit: int) -> Dict[str, Any]:
    """指数 5m K线（mootdx index_bars, frequency=0）。time=bar 结束时刻。

    ⚠ vol 字段错位=成交额/100（见文件头），只作价格兜底；up/down_count 解码正确。
    单次上限 800 根 ≈ 17 个交易日（TDX 协议限制）。
    """
    cli = _get_client()
    if cli is None:
        return {"rows": [], "error": "mootdx 不可用"}
    limit = min(int(limit or 800), 800)
    try:
        df = cli.index_bars(symbol=code, frequency=0, start=0, offset=limit)
        if df is None or df.empty:
            return {"rows": [], "error": "empty"}
        # 列名防御: 原始列可能同时含 vol 与 volume（2026-09-11 实测）
        if "volume" not in df.columns and "vol" in df.columns:
            df = df.rename(columns={"vol": "volume"})
        tcol = "datetime" if "datetime" in df.columns else df.columns[0]
        rows = []
        for _, r in df.iterrows():
            t = str(r.get(tcol, ""))[:16].replace("T", " ")
            try:
                row = {
                    "time": t,
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "close": float(r.get("close", 0)),
                    "volume": float(r.get("volume", r.get("vol", 0)) or 0),  # ⚠ vol 错位，见文件头
                }
            except (ValueError, TypeError):
                continue
            for c in ("up_count", "down_count"):
                if c in df.columns and r.get(c) is not None:
                    try:
                        row[c] = int(r.get(c))
                    except (ValueError, TypeError):
                        pass
            rows.append(row)
        return {"rows": rows[-limit:]}
    except Exception as e:
        logger.warning("[mootdx] index_kline 5m %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}


def fetch_index_kline(code: str, timeframe: str = "1D", limit: int = 250) -> Dict[str, Any]:
    """指数K线（timeframe 分发：1D → frequency=4 / 5m → frequency=0）。"""
    from app.data_sources.normalizer import normalize_chart_timeframe
    from app.data_sources.dispatch import NotSupported

    tf = normalize_chart_timeframe(timeframe)
    if tf == "1D":
        return _fetch_daily(code, limit)
    if tf == "5m":
        return _fetch_5m(code, limit)
    return NotSupported("index_kline/%s" % tf)


def fetch_index_quote(code: Optional[str] = None,
                     codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """指数实时行情（mootdx quotes）。

    接受 codes=[...] 或 code=单码（run_batch 串行分支按 code= 注入）；不传返回
    默认 4 指数。code=/codes= 双入口与腾讯同构，确保 index_quote 批量路由下
    mootdx 作串行兜底时不被 TypeError 打断（2026-10-27 接通）。
    """
    cli = _get_client()
    if cli is None:
        return {"rows": [], "error": "mootdx 不可用"}
    if not codes:
        codes = [code] if code else None
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
    # 5m vol 错位 ⇒ 必须排在 sina(priority=15) 之后作兜底，见文件头
    tf_priority = {"5m": 40}
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
