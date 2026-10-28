# -*- coding: utf-8 -*-
"""provider_index / akshare —— 指数族 akshare 源
（index_quote / index_kline / northbound_summary / northbound_daily）。

ak 调用点与 `market_cn/index.py` 一致（index spot、北向汇总、北向日级 hist_em）；
index_kline 走 `stock_zh_index_daily`（全历史），是同族的**大窗口回填兜底**
（sina/腾讯在约 1000 根上限、mootdx 单次 800 根，只有它能覆盖 10 年）。
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


def fetch_index_kline(code: str = "000001", timeframe: str = "1D", limit: int = 250) -> Dict[str, Any]:
    """指数日K线（ak.stock_zh_index_daily，全历史；只支持 1D）。

    为什么需要它：sina（datalen）/腾讯（param 上限）在 ~1000 根附近就返回空，
    指数日线**回填**（10 年 ≈ 2500 根）只有 akshare 这条全历史通道能覆盖，故作为
    同族的"大窗口兜底源"排在最后（priority 50）。
    volume 为官方"股"口径，不做换算。
    """
    from app.data_sources.normalizer import normalize_chart_timeframe, split_index_code
    from app.data_sources.dispatch import NotSupported

    if normalize_chart_timeframe(timeframe) != "1D":
        return NotSupported("index_kline/%s（akshare 仅日线）" % timeframe)

    # split_index_code：入参可能是 "000001" 也可能是 "000001.SH"，旧实现直接
    # prefix + code 会拼成 "sh000001.SH"（非法），akshare 抛 KeyError: 'date'。
    base, exch = split_index_code(code)
    if not base or not exch:
        raise ValueError("akshare index_kline 无法解析指数代码: %r" % (code,))

    get_akshare_limiter().wait()
    df = _ak().stock_zh_index_daily(symbol=exch.lower() + base)
    if df is None or getattr(df, "empty", True):
        raise RuntimeError("akshare index_kline 返回空（%s）" % code)

    rows = []
    for rec in df.to_dict("records"):
        try:
            rows.append({
                "date": str(rec.get("date"))[:10],
                "open": float(rec.get("open") or 0),
                "high": float(rec.get("high") or 0),
                "low": float(rec.get("low") or 0),
                "close": float(rec.get("close") or 0),
                "volume": float(rec.get("volume") or 0),
            })
        except (TypeError, ValueError):
            continue
    if not rows:
        raise RuntimeError("akshare index_kline 解析后为空（%s）" % code)
    return {"rows": rows[-max(1, int(limit or 250)):]}


def fetch_northbound_daily(days: int = 120) -> Dict[str, Any]:
    """北向历史日级净买入（ak.stock_hsgt_hist_em 沪股通+深股通两腿合并）。

    单位：上游「当日成交净买额」为**亿元**，原样透出，不做换算。
    返回 {"rows": [{date, hgt_yi, sgt_yi, total_yi}, ...]}（按日期升序）。

    无数据/取数异常一律 raise：本 kind 有 hexin 备用源，抛出让 dispatch 换源，
    不能返回空信封（空信封按协议算"成功"，会把备用源挡在门外）。
    """
    import pandas as pd

    get_akshare_limiter().wait()
    ak = _ak()
    hgt_df = ak.stock_hsgt_hist_em(symbol="沪股通")
    sgt_df = ak.stock_hsgt_hist_em(symbol="深股通")

    def _col_map(df):
        out = {}
        if df is None or getattr(df, "empty", True):
            return out
        for _, row in df.iterrows():
            date_val = str(row.get("日期", ""))[:10]
            net = row.get("当日成交净买额")
            if pd.notna(net):
                out[date_val] = float(net)
        return out

    hgt_map = _col_map(hgt_df)
    sgt_map = _col_map(sgt_df)
    if not hgt_map and not sgt_map:
        raise ValueError("akshare 北向日级返回空")

    rows = []
    for date_val in sorted(set(hgt_map) | set(sgt_map)):
        h = hgt_map.get(date_val, 0.0)
        s = sgt_map.get(date_val, 0.0)
        rows.append({
            "date": date_val,
            "hgt_yi": round(h, 2),
            "sgt_yi": round(s, 2),
            "total_yi": round(h + s, 2),
        })
    return {"rows": rows[-max(1, int(days or 120)):]}


from app.data_sources.provider_index import register   # noqa: E402


@register
class AkshareIndexProvider:
    name = "akshare"
    priority = 50
    markets = {"CNStock"}
    # northbound_daily: akshare 历史最长（2000+ 交易日），排在 hexin(25) 之前；
    # hexin 仅约 30 个交易日，只作备用。
    kind_priority = {"northbound_daily": 10}
    capabilities = {
        "index_quote": fetch_index_quote,
        "index_kline": fetch_index_kline,
        "northbound_summary": fetch_northbound_summary,
        "northbound_daily": fetch_northbound_daily,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
