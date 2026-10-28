# -*- coding: utf-8 -*-
"""provider_index / sina —— 指数族新浪源（index_kline, timeframe=1D/5m）。

API: quotes.sina.cn/cn/api/jsonp_v2.php/CN_MarketDataService.getKLineData
scale=240 日线 / scale=5 五分钟线，JSONP 格式。
volume 均为官方"股"口径（与 kline_index_5m 库表一致，不做换算）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def _sina_index_symbol(code: str) -> str:
    """指数代码 → 新浪带前缀符号（sh000001 / sz399001）。

    解析统一走 normalizer.split_index_code：入参可能是裸码，也可能是
    "000001.SH"（cn_stock_ext 那种形式）。旧实现直接 `code[:3]` 判前缀再拼接，
    带后缀时会得到 "sh000001.SH"（非法）。
    """
    from app.data_sources.normalizer import split_index_code

    base, exch = split_index_code(code)
    return exch.lower() + base


def _fetch_daily(code: str, limit: int) -> Dict[str, Any]:
    """指数日K线（新浪 JSONP, scale=240）。返回 {"rows": [{date, open, high, low, close, volume}, ...]}"""
    import urllib.request

    sc = _sina_index_symbol(code)
    url = (
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php"
        f"/var%20_{sc}=/CN_MarketDataService.getKLineData"
        f"?symbol={sc}&scale=240&ma=no&datalen={limit}"
    )
    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
    except Exception as e:
        # 抛出让 dispatch 换源（同族 tencent/baostock/akshare）；空信封会被
        # is_rows 判「成功」，把备用源全挡死（2026-10-28 1.log 事故同型）。
        logger.warning("[sina] index_kline %s 请求失败: %s", code, e)
        raise

    # 清理 JS 包裹
    clean = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", "", clean)
    m = re.search(r"=\s*\(?\s*(\[.*\])\s*\)?\s*;?\s*$", clean, re.DOTALL)
    if not m:
        # 抛出让 dispatch 换源（同族有 tencent/akshare）
        raise RuntimeError("sina index_kline 无 JSON 数组（datalen=%s 可能超上游上限）" % limit)

    try:
        arr = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError("sina index_kline JSON 解析失败: %s" % e)

    rows = []
    for r in arr:
        try:
            rows.append({
                "date": str(r.get("day", "")),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("volume", 0)),  # 股（对齐 kline_index_5m）
            })
        except (ValueError, TypeError):
            continue

    if not rows:
        raise RuntimeError("sina index_kline 解析后为空（code=%s limit=%s）" % (code, limit))
    return {"rows": rows}


def _fetch_5m(code: str, limit: int) -> Dict[str, Any]:
    """指数 5m K线（新浪 CN_MarketData.getKLineData, scale=5）。

    返回 {"rows": [{time, open, high, low, close, volume}, ...]}，time 为 bar
    **结束时刻**（"YYYY-MM-DD HH:MM"，09:35 起，与 kline_index_5m 同口径），
    volume=股（官方口径，与 kline_1D 日线精确一致，勿做倍数换算）。

    易错点（2026-09-12 sync_index_minute 实测，原样继承）：
      - 5001 根大响应偶发读超时 → timeout=40 + 4 次重试
      - 无效代码返回非列表 JSON → 校验 isinstance(list)
      - 服务器硬顶 datalen=5001（≈104 个交易日）
    """
    import json
    import time as _time
    import urllib.request

    sc = _sina_index_symbol(code)
    limit = max(1, min(int(limit or 5001), 5001))
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sc}&scale=5&ma=no&datalen={limit}"
    )
    data = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            if attempt == 3:
                logger.warning("[sina] index_kline 5m %s 请求失败: %s", code, e)
                raise
            _time.sleep(3)
    if not isinstance(data, list) or not data:
        # 同 _fetch_daily：空信封会挡死同族源（mootdx 是 5m 的价格兜底）
        raise RuntimeError("sina index_kline 5m 无列表数据（code=%s limit=%s）" % (code, limit))

    rows = []
    for r in data:
        try:
            day = str(r.get("day", r.get("d", "")))
            rows.append({
                "time": day[:16],  # "YYYY-MM-DD HH:MM"（bar 结束时刻）
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("volume", 0)),  # 股（对齐 kline_index_5m）
            })
        except (ValueError, TypeError):
            continue
    return {"rows": rows[-limit:]}


def fetch_index_kline(code: str, timeframe: str = "1D", limit: int = 250) -> Dict[str, Any]:
    """指数K线（timeframe 分发：1D → scale=240 / 5m → scale=5）。"""
    from app.data_sources.normalizer import normalize_chart_timeframe
    from app.data_sources.dispatch import NotSupported

    tf = normalize_chart_timeframe(timeframe)
    if tf == "1D":
        return _fetch_daily(code, limit)
    if tf == "5m":
        return _fetch_5m(code, limit)
    return NotSupported("index_kline/%s" % tf)


# ── 板块族（sector_flow / sector_list）────────────────────────────
# 新浪只有**行业**资金流与行业/概念板块行情，无概念资金流、无地域板块行情
# ⇒ 不支持的组合返 NotSupported（dispatch 不计失败，直接换下一个源）。

_FLOW_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
             "/MoneyFlow.ssl_bkzj_bk")
_INDUSTRY_BOARD_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_CONCEPT_BOARD_URL = "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class"


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _get_text(url: str, timeout: float = 15) -> str:
    """GBK 文本响应（板块页是 JS 变量包裹的 GBK 内容）。"""
    import urllib.request

    req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def fetch_sector_flow(indicator: str = "今日", board_type: str = "industry") -> Dict[str, Any]:
    """行业板块资金流排名（新浪 ssl_bkzj_bk，当日；按主力净流入降序）。

    返回 {"rows": [{name, code, change_pct, turnover, in_net, out_net,
                    main_net, main_pct, lead_stock, lead_pct}, ...]}
    单位：净额/流入/流出均为**元**；change_pct/lead_pct 为百分数（已 ×100）。
    """
    from app.data_sources.dispatch import NotSupported

    if board_type != "industry" or indicator != "今日":
        return NotSupported("sector_flow/%s/%s（新浪仅当日行业）" % (board_type, indicator))

    text = _get_text("%s?page=1&num=100&sort=netamount&asc=0&fenlei=0" % _FLOW_URL)
    data = json.loads(text)
    if not isinstance(data, list):
        # 抛出让 dispatch 换源（同族有 ths/em）：空信封会被判"成功"，把备用源挡在门外
        raise ValueError("sina sector_flow 返回非列表")

    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        inamount = _f(item.get("inamount"))
        outamount = _f(item.get("outamount"))
        netamount = _f(item.get("netamount"))
        total = inamount + outamount
        rows.append({
            "name": item.get("name", ""),
            "code": item.get("category", ""),
            "change_pct": round(_f(item.get("avg_changeratio")) * 100, 2),
            "turnover": _f(item.get("turnover")),
            "in_net": inamount,
            "out_net": outamount,
            "main_net": netamount,
            "main_pct": round(netamount / total * 100, 2) if total > 0 else 0.0,
            "lead_stock": item.get("ts_name", ""),
            "lead_pct": round(_f(item.get("ts_changeratio")) * 100, 2),
        })
    rows.sort(key=lambda x: x["main_net"], reverse=True)
    return {"rows": rows}


def fetch_sector_list(board_type: str = "industry", limit: int = 30,
                      sort_by: str = "f3", sort_dir: str = "desc") -> Dict[str, Any]:
    """板块行情排名（新浪 newSinaHy 行业 / newFLJK 概念；按涨跌幅降序）。

    字段（13 段逗号拼接）实证 2026-09-21：
      [0]code [1]name [2]stock_count [3]avg_price [4]平均价格变动(元，**非**涨跌幅)
      [5]涨跌幅(%) [6]volume [7]amount(元) [8]lead_code [9]lead_pct(%)
      [10]lead_price [11]lead_change [12]lead_name
    旧实现误用 [4]*100 当涨跌幅（产出 281.6% 乱值），此处取 [5]。

    返回 {"rows": [{name, code, change_pct, price, volume, amount, turnover,
                    up_count, down_count, lead_stock, lead_stock_code,
                    lead_stock_pct, limit_up_count, total_mv, pe_ratio}, ...]}
    新浪无涨跌家数 / 涨停家数 / 市值 / PE ⇒ 对应字段为 0（不是 0 值数据，是口径缺口）。
    """
    from app.data_sources.dispatch import NotSupported

    if board_type == "industry":
        text = _get_text(_INDUSTRY_BOARD_URL)
        key_prefix = ""
    elif board_type == "concept":
        text = _get_text(_CONCEPT_BOARD_URL)
        key_prefix = "gn_"
    else:
        return NotSupported("sector_list/%s（新浪无地域板块行情）" % board_type)

    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= 0:
        raise ValueError("sina sector_list 返回格式异常（无 JS 对象）")
    data = json.loads(text[start:end])

    rows = []
    for key, val in data.items():
        if key_prefix and not key.startswith(key_prefix):
            continue
        parts = str(val).split(",")
        if len(parts) < 13:
            continue
        rows.append({
            "name": parts[1],
            "code": parts[0],
            "change_pct": round(_f(parts[5]), 2),
            "price": _f(parts[3]),
            "volume": _f(parts[6]),
            "amount": _f(parts[7]),
            "turnover": 0.0,
            "up_count": 0,
            "down_count": 0,
            "lead_stock": parts[12],
            "lead_stock_code": parts[8],
            "lead_stock_pct": round(_f(parts[9]), 2),
            "limit_up_count": 0,
            "total_mv": 0.0,
            "pe_ratio": 0.0,
        })
    # 排序键与东财 fid 对齐（hot_sectors 传的是东财字段名，跨源同参）
    key_map = {"f3": "change_pct", "f6": "amount", "f8": "turnover", "f20": "total_mv"}
    rows.sort(key=lambda x: _f(x.get(key_map.get(sort_by, "change_pct"))),
              reverse=(sort_dir != "asc"))
    return {"rows": rows[:max(1, int(limit or 30))]}

def fetch_market_flow_rt() -> Dict[str, Any]:
    """大盘实时资金流（新浪无大盘接口 ⇒ 由全部行业板块流入/流出汇总）。

    返回 {"rows": [{source, timestamp, main_net, main_pct, in_net, out_net,
                    sectors_count, data}]}（单条快照，data=板块明细行）。
    单位：元。main_net = Σin - Σout（与逐板块 netamount 求和等价）。
    """
    from datetime import datetime

    rows = fetch_sector_flow("今日", "industry")["rows"]
    if not rows:
        raise RuntimeError("新浪行业板块资金流为空，无法汇总大盘资金流")

    total_in = sum(r["in_net"] for r in rows)
    total_out = sum(r["out_net"] for r in rows)
    total_net = total_in - total_out
    total = total_in + total_out
    return {"rows": [{
        "source": "sina",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "main_net": total_net,
        "main_pct": round(total_net / total * 100, 2) if total > 0 else 0.0,
        "in_net": total_in,
        "out_net": total_out,
        "sectors_count": len(rows),
        "data": rows,
    }]}


from app.data_sources.provider_index import register  # noqa: E402


@register
class SinaIndexProvider:
    name = "sina"
    priority = 15
    markets = {"CNStock"}
    capabilities = {
        "index_kline": fetch_index_kline,
        "sector_flow": fetch_sector_flow,
        "sector_list": fetch_sector_list,
        "market_flow_rt": fetch_market_flow_rt,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
