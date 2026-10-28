# -*- coding: utf-8 -*-
"""provider_misc / em_stock_flow —— 个股资金流东财源（stock_flow_rt / stock_flow_daily）。

两个接口同在东财 push2 系，共用 fflow_em 的 curl_cffi edge101 指纹封装：
  · push2    fflow/kline/get      klt=1   → 当日**分钟级累计**净流入（240 根）
  · push2his fflow/daykline/get   klt=101 → 日级净流入历史

字段：f51 时刻/日期，f52~f56 = 主力/小单/中单/大单/超大单（元）。
行键沿用 data_sources 统一五列约定（同 FFLOW_NET_COLS）：
  main_net / small_net / mid_net / big_net / super_net
本层刻意不用 tape 的槽位名 large_net —— 槽位改名是消费方的事，源层只按库列名出数。

⚠ IP 限频（2026-10-14 实证）：push2 / push2his 会对连续请求的 IP 掐连接
（RemoteDisconnected）。源层只做「短退避重试」，熔断/降级顺序由 dispatch 按
provider priority 决定（coord_misc 里 sina 优先、本源兜底）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.data_sources.provider_index import fflow_em
from app.utils.logger import get_logger

logger = get_logger(__name__)

_RT_HOSTS = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
)
_DAILY_HOST = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"


def _secid(code: str) -> str:
    """6 位代码 → 东财 secid（沪市 1.xxxx，其余 0.xxxx）。"""
    c = str(code).strip().split(".")[0]
    return ("1." if c.startswith(("6", "9")) else "0.") + c


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fetch(hosts, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按 host 顺序取 klines；全 host 失败的抛异常（交 dispatch 换源）。"""
    if isinstance(hosts, str):
        hosts = (hosts,)
    query = "&".join("%s=%s" % (k, v) for k, v in params.items())
    last_err = "no host"
    for host in hosts:
        txt = fflow_em._em_get("%s?%s" % (host, query), timeout=15, retries=2)
        if not txt:
            last_err = "empty response"
            continue
        try:
            d = json.loads(txt)
        except Exception as e:
            last_err = "JSON 解析失败: %s" % e
            continue
        return ((d.get("data") or {}).get("klines")) or []
    raise RuntimeError("东财个股资金流全 host 无响应: %s" % last_err)


def fetch_stock_flow_rt(code: str = "") -> Dict[str, Any]:
    """当日个股分钟级资金流（push2 klt=1）。

    返回 {"rows": [{time, main_net, small_net, mid_net, big_net, super_net}, ...]}
    time="YYYY-MM-DD HH:MM"，净额为**当日累计值**（元）。
    ⚠ 累计值不是单分钟流量；跨点差分才是分钟流量，由消费方按需处理。
    """
    if not code:
        raise ValueError("stock_flow_rt 需要 code")
    klines = _fetch(_RT_HOSTS, {
        "secid": _secid(code), "klt": 1,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    })
    rows = []
    for line in klines:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        rows.append({
            "time": p[0],
            "main_net": _num(p[1]),
            "small_net": _num(p[2]),
            "mid_net": _num(p[3]),
            "big_net": _num(p[4]),
            "super_net": _num(p[5]),
        })
    if not rows:
        raise RuntimeError("东财个股分钟资金流返回空（%s）" % code)
    return {"rows": rows}


def fetch_stock_flow_daily(code: str = "", days: int = 120) -> Dict[str, Any]:
    """个股日级资金流（push2his daykline, klt=101）。

    返回 {"rows": [{date, main_net, small_net, mid_net, big_net, super_net}, ...]}
    按日期**升序**（旧→新），与 provider_misc/sina 的 stock_flow_daily 同序，
    便于消费方统一取尾部 N 条。
    """
    if not code:
        raise ValueError("stock_flow_daily 需要 code")
    klines = _fetch(_DAILY_HOST, {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "lmt": max(1, int(days or 120)),
    })
    rows = []
    for line in klines:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        rows.append({
            "date": p[0],
            "main_net": _num(p[1]),
            "small_net": _num(p[2]),
            "mid_net": _num(p[3]),
            "big_net": _num(p[4]),
            "super_net": _num(p[5]),
        })
    if not rows:
        raise RuntimeError("东财个股日级资金流返回空（%s）" % code)
    rows.sort(key=lambda x: x["date"])
    return {"rows": rows[-max(1, int(days or 120)):]}


from app.data_sources.provider_misc import register  # noqa: E402


@register
class EastmoneyStockFlowProvider:
    name = "eastmoney"
    # 60 > sina 的 50：同 kind 时新浪优先、东财兜底（2026-09-22 主备翻转的依据是
    # 东财 push2 对连续请求掐连接，新浪无反爬；provider_misc 排序只看类级 priority）
    priority = 60
    markets = {"CNStock"}
    capabilities = {
        "stock_flow_rt": fetch_stock_flow_rt,
        "stock_flow_daily": fetch_stock_flow_daily,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
