# -*- coding: utf-8 -*-
"""provider_index / hexin —— 指数族同花顺 hexin 源（northbound_flow / northbound_daily / northbound_holdings）。

三个接口同在 data.hexin.cn，共用 headers 与「utf-8 失败回落 gbk」解码容错：

  · dayChart   —— 当日北向累计净买入**分钟序列**（262 点，9:15-15:00 每分钟）
  · dayKline   —— 北向**历史日级**净买入（约 30 个交易日）
  · dayKline   —— 同 URL 的 `list` 段是北向**持股明细**（按持仓市值降序）

单位：hgt/sgt 为**亿元**（上游原值），不做换算；成交净买额 2024-08 起沪深港通
停止实时披露，上游可能恒 0（口径事实，非本层 bug）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

_DAY_CHART_URL = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
_DAY_KLINE_URL = "https://data.hexin.cn/market/hsgtApi/dayKline/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
    "Referer": "https://data.hexin.cn/",
}


def _get_json(url: str, timeout: float = 10) -> Any:
    """GET + JSON 解析。响应可能是 GBK 或含非法 UTF-8 字节，故两级解码容错。"""
    import urllib.request

    req = urllib.request.Request(url)
    for k, v in _HEADERS.items():
        req.add_header(k, v)
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    return json.loads(text)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_northbound_flow() -> Dict[str, Any]:
    """当日北向分钟级累计净买入。

    返回 {"rows": [{time, hgt_yi, sgt_yi}, ...]}；三列长度可能不一致，
    按 time 长度对齐，缺位留 None（缺位≠0，宁缺勿假）。
    """
    try:
        d = _get_json(_DAY_CHART_URL)
    except Exception as e:
        logger.warning("[hexin] northbound_flow 失败: %s", e)
        raise

    times = d.get("time") or []
    hgt = d.get("hgt") or []
    sgt = d.get("sgt") or []
    rows = [{
        "time": times[i],
        "hgt_yi": hgt[i] if i < len(hgt) else None,
        "sgt_yi": sgt[i] if i < len(sgt) else None,
    } for i in range(len(times))]
    return {"rows": rows}


def fetch_northbound_daily(days: int = 120) -> Dict[str, Any]:
    """北向历史日级净买入（hexin 仅约 30 个交易日，作为 akshare 之后的备用源）。"""
    try:
        d = _get_json(_DAY_KLINE_URL)
    except Exception as e:
        logger.warning("[hexin] northbound_daily 失败: %s", e)
        raise

    chart = d.get("chart") or {}
    times = chart.get("time") or []
    hgt = chart.get("hgt") or []
    sgt = chart.get("sgt") or []

    rows: List[Dict[str, Any]] = []
    for i in range(len(times)):
        h = _num(hgt[i]) if i < len(hgt) else 0.0
        s = _num(sgt[i]) if i < len(sgt) else 0.0
        rows.append({
            "date": str(times[i]),
            "hgt_yi": round(h, 2),
            "sgt_yi": round(s, 2),
            "total_yi": round(h + s, 2),
        })
    if not rows:
        raise ValueError("hexin northbound_daily 返回空 chart")
    return {"rows": rows[-max(1, int(days or 120)):]}


def fetch_northbound_holdings(top: int = 50) -> Dict[str, Any]:
    """北向持股明细（同一 dayKline 接口的 `list` 段），按持仓市值降序。

    字段口径（上游原值，勿换算）：zxj=最新价, cgcb=持仓成本, cgyk=盈亏(%),
    cgl=持股数(股), cgsz=持仓市值(元), ltb=占流通股比(%)。
    """
    try:
        d = _get_json(_DAY_KLINE_URL)
    except Exception as e:
        logger.warning("[hexin] northbound_holdings 失败: %s", e)
        raise

    rows: List[Dict[str, Any]] = []
    for item in (d.get("list") or []):
        if not isinstance(item, dict):
            continue
        try:
            rows.append({
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "price": _num(item.get("zxj")),
                "hold_cost": _num(item.get("cgcb")),
                "profit_pct": _num(item.get("cgyk")),
                "hold_shares": int(_num(item.get("cgl"))),
                "hold_market": _num(item.get("cgsz")),
                "hold_ratio": _num(item.get("ltb")),
            })
        except (ValueError, TypeError):
            continue

    rows.sort(key=lambda x: x["hold_market"], reverse=True)
    return {"rows": rows[:max(1, int(top or 50))]}


from app.data_sources.provider_index import register  # noqa: E402


@register
class HexinIndexProvider:
    name = "hexin"
    priority = 25
    markets = {"CNStock"}
    capabilities = {
        "northbound_flow": fetch_northbound_flow,
        "northbound_daily": fetch_northbound_daily,
        "northbound_holdings": fetch_northbound_holdings,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
