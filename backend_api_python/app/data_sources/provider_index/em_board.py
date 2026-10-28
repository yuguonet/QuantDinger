# -*- coding: utf-8 -*-
"""provider_index / em_board —— 指数族东财源（大盘资金流 / 板块资金流 / 板块行情 / 板块成分股）。

四个 kind 全部落在 push2 系（push2 / push2his / push2delay），共用同一个
curl_cffi edge101 TLS 指纹封装（见 `fflow_em._em_get`，不要在别处重写 HTTP 层：
东财 CDN 对 urllib/requests 的 JA3 指纹直接掐连接）。

【字段口径（东财资金流标准布局，成对出现「净额, 占比」）】
  今日:  主力 f62/f184 | 超大单 f66/f69 | 大单 f72/f75 | 中单 f78/f81 | 小单 f84/f87
  3日:   主力 f267 | 超大单 f269 | 大单 f271 | 中单 f273 | 小单 f275
  5日:   主力 f164 | 超大单 f166 | 大单 f168 | 中单 f170 | 小单 f172
  10日:  主力 f174 | 超大单 f176 | 大单 f178 | 中单 f180 | 小单 f182
  ⚠ 旧实现（收敛前 `index._sector_flow_eastmoney`）取连续四字段 f66/f69/f72/f75
    当「超大/大/中/小净额」，其中 f69/f75 是**占比**，f72 是**大单** —— 三处错位。
    本层按标准布局取值，并做「主力 = 超大单 + 大单」自洽校验，漂移即告警。
    自洽校验是运行期探针，不是兜底：校验失败只记日志，不改写数值。

单位：资金净额一律**元**；涨跌幅/占比为百分数。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.data_sources.provider_index import fflow_em
from app.utils.logger import get_logger

logger = get_logger(__name__)

_CLIST_HOSTS = (
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
)
_SLIST_HOSTS = (
    "https://push2.eastmoney.com/api/qt/slist/get",
    "https://push2delay.eastmoney.com/api/qt/slist/get",
)
_FFLOW_RT_HOSTS = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
)
_FFLOW_DAILY_HOST = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

# 大盘资金流用上证指数近似代表沪深整体（与收敛前 index.py 同一近似口径）
_MARKET_SECID = "1.000001"

# 板块类型 → clist 过滤串
_BOARD_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3"}

# 统计周期 → (排序字段, 主力, 超大单, 大单, 中单, 小单, 主力占比或 None)
_FLOW_FIELDS = {
    "今日": ("f62", "f62", "f66", "f72", "f78", "f84", "f184"),
    "3日": ("f267", "f267", "f269", "f271", "f273", "f275", None),
    "5日": ("f164", "f164", "f166", "f168", "f170", "f172", None),
    "10日": ("f174", "f174", "f176", "f178", "f180", "f182", None),
}


def _num(v: Any, default: float = 0.0) -> float:
    if v is None or v == "" or v == "-":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_json(hosts, params: Dict[str, Any]) -> Dict[str, Any]:
    """按 host 顺序取 JSON（hosts 是同一接口的镜像，非不同数据源）。全失败抛异常。"""
    if isinstance(hosts, str):
        hosts = (hosts,)
    last_err = "no host"
    query = "&".join("%s=%s" % (k, v) for k, v in params.items())
    for host in hosts:
        txt = fflow_em._em_get("%s?%s" % (host, query), timeout=15, retries=2)
        if not txt:
            last_err = "empty response"
            continue
        try:
            return json.loads(txt)
        except Exception as e:
            last_err = "JSON 解析失败: %s" % e
    raise RuntimeError("东财接口全 host 无响应: %s" % last_err)


# ── 大盘资金流 ────────────────────────────────────────────────

def _fflow_rows(secid: str, klt: int, limit: int = 0) -> List[Dict[str, Any]]:
    """东财资金流 klines → 行；f52~f56 = 主力/小单/中单/大单/超大单（元）。

    klt=1 返当日分钟**累计**值（键 time）；klt=101 + limit 返日线（键 date）。
    """
    params = {"secid": secid, "klt": klt,
              "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57"}
    if limit:
        params["lmt"] = limit
        hosts = _FFLOW_DAILY_HOST
    else:
        hosts = _FFLOW_RT_HOSTS
    d = _get_json(hosts, params)
    key = "date" if limit else "time"
    rows = []
    for line in ((d.get("data") or {}).get("klines") or []):
        p = str(line).split(",")
        if len(p) < 6:
            continue
        rows.append({
            key: p[0],
            "main_net": _num(p[1]),
            "small_net": _num(p[2]),
            "mid_net": _num(p[3]),
            "large_net": _num(p[4]),
            "super_net": _num(p[5]),
        })
    return rows


def fetch_market_flow_rt() -> Dict[str, Any]:
    """大盘实时资金流（上证指数分钟级主力净流入序列汇总）。

    返回 {"rows": [{source, timestamp, main_net, main_pct, in_net, out_net,
                    points, data}]}（单条快照，data=最近 10 个分钟点）。
    main_net 为当日累计主力净流入（元）；东财分钟序列是**累计值**，
    直接取末点而非求平均，避免与"每日流量"混口径。
    """
    rows = _fflow_rows(_MARKET_SECID, klt=1)
    if not rows:
        raise RuntimeError("东财大盘实时资金流返回空")
    return {"rows": [{
        "source": "eastmoney",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "main_net": rows[-1]["main_net"],
        "main_pct": 0.0,
        "in_net": 0.0,
        "out_net": 0.0,
        "points": len(rows),
        "data": rows[-10:],
    }]}


def fetch_market_flow_daily(days: int = 120) -> Dict[str, Any]:
    """大盘资金流日线（push2his fflow daykline, klt=101）。

    返回 {"rows": [{date, main_net, main_pct, super_net, large_net, mid_net, small_net}]}
    main_pct = 主力净额 / 五档绝对值之和（与收敛前 index.py 同算法）。
    """
    rows = _fflow_rows(_MARKET_SECID, klt=101, limit=max(1, int(days or 120)))
    if not rows:
        raise RuntimeError("东财大盘资金流日线返回空")
    out = []
    for r in rows:
        total = (abs(r["main_net"]) + abs(r["small_net"]) + abs(r["mid_net"])
                 + abs(r["large_net"]) + abs(r["super_net"]))
        out.append({
            "date": r["date"],
            "main_net": r["main_net"],
            "main_pct": round(r["main_net"] / total * 100, 2) if total > 0 else 0.0,
            "super_net": r["super_net"],
            "large_net": r["large_net"],
            "mid_net": r["mid_net"],
            "small_net": r["small_net"],
        })
    return {"rows": out}


# ── 板块族 ────────────────────────────────────────────────────

def fetch_sector_flow(indicator: str = "今日", board_type: str = "industry") -> Dict[str, Any]:
    """板块资金流排名（东财 push2 clist；行业/概念 × 今日/3日/5日/10日全支持）。

    返回 {"rows": [{name, code, change_pct, main_net, main_pct, super_net,
                    large_net, mid_net, small_net, in_net, out_net, turnover,
                    lead_stock, lead_pct}, ...]} 按主力净流入降序。
    """
    if indicator not in _FLOW_FIELDS:
        raise ValueError("不支持的统计周期: %s" % indicator)
    fs = _BOARD_FS.get(board_type)
    if not fs:
        raise ValueError("不支持的板块类型: %s" % board_type)

    fid, f_main, f_super, f_large, f_mid, f_small, f_main_pct = _FLOW_FIELDS[indicator]
    fields = ["f12", "f14", "f3", f_main, f_super, f_large, f_mid, f_small,
              "f204", "f205"]
    if f_main_pct:
        fields.append(f_main_pct)

    d = _get_json(_CLIST_HOSTS, {
        "fid": fid, "po": 1, "pz": 100, "pn": 1, "np": 1, "fltt": 2, "invt": 2,
        "fs": fs, "fields": ",".join(fields),
    })
    items = ((d.get("data") or {}).get("diff")) or []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        main_net = _num(item.get(f_main))
        super_net = _num(item.get(f_super))
        large_net = _num(item.get(f_large))
        mid_net = _num(item.get(f_mid))
        small_net = _num(item.get(f_small))

        # 自洽探针：东财口径 主力 = 超大单 + 大单（偏差 >2% 说明字段漂移，只告警不改值）
        base = max(abs(main_net), 1.0)
        if abs(main_net - (super_net + large_net)) / base > 0.02:
            logger.warning("[em_board] %s 字段自洽失败(主力≠超大+大): %s %.0f vs %.0f+%.0f",
                           indicator, item.get("f14"), main_net, super_net, large_net)

        total = abs(main_net) + abs(super_net) + abs(large_net) + abs(mid_net) + abs(small_net)
        rows.append({
            "name": item.get("f14", ""),
            "code": item.get("f12", ""),
            "change_pct": _num(item.get("f3")),
            "main_net": main_net,
            "main_pct": (round(_num(item.get(f_main_pct)), 2) if f_main_pct
                         else (round(main_net / total * 100, 2) if total > 0 else 0.0)),
            "super_net": super_net,
            "large_net": large_net,
            "mid_net": mid_net,
            "small_net": small_net,
            "in_net": 0.0,
            "out_net": 0.0,
            "turnover": 0.0,
            "lead_stock": item.get("f204", ""),
            "lead_pct": _num(item.get("f205")),
        })
    if not rows:
        raise RuntimeError("东财板块资金流返回空（%s/%s）" % (board_type, indicator))
    rows.sort(key=lambda x: x["main_net"], reverse=True)
    return {"rows": rows}


def fetch_sector_list(board_type: str = "industry", limit: int = 30,
                      sort_by: str = "f3", sort_dir: str = "desc") -> Dict[str, Any]:
    """板块行情排名（东财 push2 clist；行业/概念/地域全支持）。

    返回 {"rows": [{name, code, change_pct, price, volume, amount, turnover,
                    up_count, down_count, lead_stock, lead_stock_code,
                    lead_stock_pct, limit_up_count, total_mv, pe_ratio}, ...]}

    字段：f2 现价 / f3 涨跌幅 / f5 成交量(手) / f6 成交额(元) / f8 换手率 /
    f9 市盈率 / f20 总市值(元) / f100 涨停标记 / f104 上涨家数 / f105 下跌家数 /
    f115 领涨股涨跌幅 / f128 领涨股 / f136 领涨股代码。
    """
    fs = {"industry": "b:BK0475", "concept": "b:BK0815", "area": "b:BK0813"}.get(board_type)
    if not fs:
        raise ValueError("不支持的板块类型: %s" % board_type)

    d = _get_json(_CLIST_HOSTS, {
        "pn": 1, "pz": max(1, int(limit or 30)), "po": 1 if sort_dir != "asc" else 0,
        "np": 1, "fltt": 2, "invt": 2, "fid": sort_by, "fs": fs,
        "fields": ("f2,f3,f5,f6,f8,f9,f12,f14,f20,f100,f104,f105,f115,f128,f136"),
    })
    items = ((d.get("data") or {}).get("diff")) or []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": item.get("f14", "") or "",
            "code": item.get("f12", "") or "",
            "change_pct": _num(item.get("f3")),
            "price": _num(item.get("f2")),
            "volume": _num(item.get("f5")),
            "amount": _num(item.get("f6")),
            "turnover": _num(item.get("f8")),
            "up_count": int(_num(item.get("f104"))),
            "down_count": int(_num(item.get("f105"))),
            "lead_stock": item.get("f128", "") or "",
            "lead_stock_code": item.get("f136", "") or "",
            "lead_stock_pct": _num(item.get("f115")),
            "limit_up_count": int(_num(item.get("f100"))),
            "total_mv": _num(item.get("f20")),
            "pe_ratio": _num(item.get("f9")),
        })
    if not rows:
        raise RuntimeError("东财板块行情返回空（%s）" % board_type)
    return {"rows": rows}


def fetch_sector_stocks(board_code: str = "", limit: int = 10) -> Dict[str, Any]:
    """板块成分股行情（东财 push2 clist, fs=b:{board_code}；按涨跌幅降序）。

    返回 {"rows": [{code, name, price, change_pct, amount, turnover, is_limit_up}]}
    """
    if not board_code:
        raise ValueError("sector_stocks 需要 board_code")
    d = _get_json(_CLIST_HOSTS, {
        "pn": 1, "pz": max(1, int(limit or 10)), "po": 1, "np": 1, "fltt": 2,
        "invt": 2, "fid": "f3", "fs": "b:%s" % board_code,
        "fields": "f2,f3,f4,f5,f6,f8,f12,f14,f15,f16,f17,f18,f62,f100,f115",
    })
    items = ((d.get("data") or {}).get("diff")) or []
    rows = [{
        "code": item.get("f12", "") or "",
        "name": item.get("f14", "") or "",
        "price": _num(item.get("f2")),
        "change_pct": _num(item.get("f3")),
        "amount": _num(item.get("f6")),
        "turnover": _num(item.get("f8")),
        "is_limit_up": item.get("f100") == 1,
    } for item in items if isinstance(item, dict)]
    if not rows:
        raise RuntimeError("东财板块成分股返回空（%s）" % board_code)
    return {"rows": rows}


def fetch_stock_boards(code: str = "") -> Dict[str, Any]:
    """个股所属板块（东财 push2 slist，spt=3 板块列表）。

    返回 {"rows": [{name, code, change_pct, lead_stock}]}；仅东财有这个"个股→板块"
    反查通道（sina 板块页无成分股反向索引）。
    ⚠ 该接口 data.diff 是 dict（key=序号）而非 list，需按值取。
    """
    code = str(code or "").strip()
    if not code:
        raise ValueError("stock_boards 需要 code")
    market = 1 if code.startswith(("6", "9")) else 0
    d = _get_json(_SLIST_HOSTS, {
        "fltt": 2, "invt": 2, "secid": "%d.%s" % (market, code),
        "spt": 3, "pi": 0, "pz": 200, "po": 1,
        "fields": "f12,f14,f3,f128",
    })
    diff = (d.get("data") or {}).get("diff") or {}
    items = list(diff.values()) if isinstance(diff, dict) else list(diff)
    rows = [{
        "name": it.get("f14", "") or "",
        "code": it.get("f12", "") or "",
        "change_pct": it.get("f3", ""),
        "lead_stock": it.get("f128", "") or "",
    } for it in items if isinstance(it, dict)]
    if not rows:
        raise RuntimeError("东财个股板块归属返回空（%s）" % code)
    return {"rows": rows}


from app.data_sources.provider_index import register  # noqa: E402


@register
class EastmoneyIndexProvider:
    name = "eastmoney"
    priority = 40
    markets = {"CNStock"}
    capabilities = {
        "market_flow_rt": fetch_market_flow_rt,
        "market_flow_daily": fetch_market_flow_daily,
        "sector_flow": fetch_sector_flow,
        "sector_list": fetch_sector_list,
        "sector_stocks": fetch_sector_stocks,
        "stock_boards": fetch_stock_boards,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
