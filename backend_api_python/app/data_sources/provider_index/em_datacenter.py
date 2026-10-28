# -*- coding: utf-8 -*-
"""provider_index / em_datacenter —— 指数族东财数据中心源（northbound_stock_hold）。

接口: datacenter-web.eastmoney.com/api/data/v1/get
报表: RPT_MUTUAL_HOLD_DET（北向持股明细）

⚠ 该报表粒度是「交易日 × 托管机构」（一行一个 ORG_CODE），不是「一日一行」。
  实测 2026-10-28：600519 单日期返回 40+ 行机构明细，单行 HOLD_NUM 仅数百股。
  因此本层按 HOLD_DATE 聚合（持股数/市值求和），否则 hold_vol 会变成"某一家的持股"
  而非"北向合计持股"（收敛前 `services/ext_params/northbound.py` 即取前 N 行当日期序列）。

⚠ 上游披露制度（2024-08 起）：北向**个股持股明细**改为低频快照，最新 HOLD_DATE
  可能落后当期数月；`HOLD_SHARES_RATIO` 恒 0、无 `HOLD_SHARES_CHG` 字段。
  本层如实透出（缺失给 0），是否可用由调用方的"新鲜度校验"决定，不在此伪造。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_northbound_stock_hold(code: str = "", days: int = 10) -> Dict[str, Any]:
    """个股北向持股历史（按交易日聚合，最近 days 个披露日）。

    返回 {"rows": [{date, hold_vol, hold_pct, hold_vol_chg, hold_market_value}, ...]}
      hold_vol        持股数量（股，当日各托管机构合计）
      hold_pct        占流通股比（%）；上游该报表恒 0
      hold_vol_chg    持股变化（股）；上游无该字段，固定 0
      hold_market_value 持股市值（元，当日合计）
    按 date 降序（最新在前），与收敛前调用方的取用顺序一致。
    """
    import requests

    code = str(code or "").strip()
    if not code:
        raise ValueError("northbound_stock_hold 需要 code")
    # 每日期有多个托管机构 ⇒ 多取几倍行，聚合后再截断到 days 个日期
    page_size = max(100, int(days or 10) * 50)
    try:
        resp = requests.get(_URL, params={
            "sortColumns": "HOLD_DATE",
            "sortTypes": "-1",
            "pageSize": page_size,
            "pageNumber": 1,
            "reportName": "RPT_MUTUAL_HOLD_DET",
            "columns": "ALL",
            "filter": '(SECURITY_CODE="%s")' % code,
        }, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("[em_datacenter] northbound_stock_hold(%s) 失败: %s", code, e)
        raise

    items = ((payload.get("result") or {}).get("data")) or []
    if not items:
        return {"rows": []}

    agg: Dict[str, Dict[str, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        date = str(item.get("HOLD_DATE", ""))[:10]
        if not date:
            continue
        slot = agg.setdefault(date, {"hold_vol": 0.0, "hold_market_value": 0.0})
        slot["hold_vol"] += _num(item.get("HOLD_NUM"))
        slot["hold_market_value"] += _num(item.get("HOLD_MARKET_CAP"))

    rows: List[Dict[str, Any]] = [{
        "date": date,
        "hold_vol": round(v["hold_vol"], 2),
        "hold_pct": 0.0,          # 上游该报表恒 0（无量，非"算出来是 0"）
        "hold_vol_chg": 0.0,      # 上游无 HOLD_SHARES_CHG 字段
        "hold_market_value": round(v["hold_market_value"], 2),
    } for date, v in sorted(agg.items(), reverse=True)]

    return {"rows": rows[:max(1, int(days or 10))]}


from app.data_sources.provider_index import register  # noqa: E402


@register
class EastmoneyDatacenterProvider:
    name = "eastmoney_datacenter"
    priority = 40
    markets = {"CNStock"}
    capabilities = {
        "northbound_stock_hold": fetch_northbound_stock_hold,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
