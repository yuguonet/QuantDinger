"""
五档盘口 / 当日分笔 / 历史分笔 / 个股资金流向 — mootdx + 东财

功能:
  1. 五档实时行情 — 买一~买五 / 卖一~卖五 + 实时快照
  2. 当日分笔成交 — 逐笔成交明细（仅交易时段可用）
  3. 历史分笔成交 — 指定日期的逐笔成交明细
  4. 个股资金流向 — 当日分钟级 + 近120日日级（东财 push2）

依赖: pip install mootdx
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端（复用 index 模块的单例，或自建）
# ══════════════════════════════════════════════════════════════

_client = None
_client_ts = 0
_CLIENT_TTL = 3600


def _get_client():
    global _client, _client_ts
    if _client is not None and (time.time() - _client_ts) < _CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None

    try:
        from mootdx.quotes import Quotes
        _client = Quotes.factory(market='std', timeout=10, heartbeat=True)
        _client_ts = time.time()
        logger.info("[mootdx:tape] 连接成功")
        return _client
    except Exception as e:
        logger.warning("[mootdx:tape] 连接失败: %s", e)
        _client = None
        return None


def _market(code: str) -> int:
    """代码 → 通达信市场号 (1=沪 0=深)。"""
    return 1 if code[:3] in ("000", "88", "99") else 0


# ══════════════════════════════════════════════════════════════
#  1. 五档实时行情
# ══════════════════════════════════════════════════════════════

def _quote_mootdx(code: str) -> Optional[Dict[str, Any]]:
    """mootdx: 五档盘口 + 实时快照。"""
    cli = _get_client()
    if cli is None:
        return None
    try:
        df = cli.quotes(symbol=[code])
        if df is None or df.empty:
            return None
        r = df.iloc[0]
        price = float(r.get("price", 0))
        last_close = float(r.get("last_close", 0))
        return {
            "code": str(r.get("code", code)),
            "name": str(r.get("name", "")),
            "price": price,
            "open": float(r.get("open", 0)),
            "high": float(r.get("high", 0)),
            "low": float(r.get("low", 0)),
            "last_close": last_close,
            "change": round(price - last_close, 4),
            "change_percent": float(r.get("percent", 0)),
            "volume": float(r.get("vol", 0)),
            "amount": float(r.get("amount", 0)),
            "bid": {
                "bid1": {"price": float(r.get("bid1", 0)), "vol": float(r.get("bid_vol1", 0))},
                "bid2": {"price": float(r.get("bid2", 0)), "vol": float(r.get("bid_vol2", 0))},
                "bid3": {"price": float(r.get("bid3", 0)), "vol": float(r.get("bid_vol3", 0))},
                "bid4": {"price": float(r.get("bid4", 0)), "vol": float(r.get("bid_vol4", 0))},
                "bid5": {"price": float(r.get("bid5", 0)), "vol": float(r.get("bid_vol5", 0))},
            },
            "ask": {
                "ask1": {"price": float(r.get("ask1", 0)), "vol": float(r.get("ask_vol1", 0))},
                "ask2": {"price": float(r.get("ask2", 0)), "vol": float(r.get("ask_vol2", 0))},
                "ask3": {"price": float(r.get("ask3", 0)), "vol": float(r.get("ask_vol3", 0))},
                "ask4": {"price": float(r.get("ask4", 0)), "vol": float(r.get("ask_vol4", 0))},
                "ask5": {"price": float(r.get("ask5", 0)), "vol": float(r.get("ask_vol5", 0))},
            },
            "source": "mootdx",
        }
    except Exception as e:
        logger.warning("[mootdx] 五档行情失败(%s): %s", code, e)
        return None


def _quote_tencent(code: str) -> Optional[Dict[str, Any]]:
    """腾讯财经: 五档盘口 + 实时快照。"""
    import urllib.request
    pfx = "sh" if _market(code) == 1 else "sz"
    try:
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={pfx}{code}")
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    except Exception as e:
        logger.warning("[tencent] 五档行情请求失败: %s", e)
        return None

    line = raw.strip().split(";")[0]
    if '"' not in line:
        return None
    v = line.split('"')[1].split("~")
    if len(v) < 50:
        return None

    price = float(v[3]) if v[3] else 0
    last_close = float(v[4]) if v[4] else 0
    return {
        "code": code, "name": v[1],
        "price": price, "open": float(v[5]) if v[5] else 0,
        "high": float(v[33]) if v[33] else 0, "low": float(v[34]) if v[34] else 0,
        "last_close": last_close,
        "change": float(v[31]) if v[31] else 0,
        "change_percent": float(v[32]) if v[32] else 0,
        "volume": float(v[36]) if v[36] else 0,
        "amount": float(v[37]) if v[37] else 0,
        "bid": {
            "bid1": {"price": float(v[9]) if v[9] else 0, "vol": float(v[10]) if v[10] else 0},
            "bid2": {"price": float(v[11]) if v[11] else 0, "vol": float(v[12]) if v[12] else 0},
            "bid3": {"price": float(v[13]) if v[13] else 0, "vol": float(v[14]) if v[14] else 0},
            "bid4": {"price": float(v[15]) if v[15] else 0, "vol": float(v[16]) if v[16] else 0},
            "bid5": {"price": float(v[17]) if v[17] else 0, "vol": float(v[18]) if v[18] else 0},
        },
        "ask": {
            "ask1": {"price": float(v[19]) if v[19] else 0, "vol": float(v[20]) if v[20] else 0},
            "ask2": {"price": float(v[21]) if v[21] else 0, "vol": float(v[22]) if v[22] else 0},
            "ask3": {"price": float(v[23]) if v[23] else 0, "vol": float(v[24]) if v[24] else 0},
            "ask4": {"price": float(v[25]) if v[25] else 0, "vol": float(v[26]) if v[26] else 0},
            "ask5": {"price": float(v[27]) if v[27] else 0, "vol": float(v[28]) if v[28] else 0},
        },
        "source": "tencent",
    }


# ══════════════════════════════════════════════════════════════
#  2. 当日分笔成交
# ══════════════════════════════════════════════════════════════

def _ticks_today_mootdx(code: str, limit: int = 2000) -> Optional[List[Dict[str, Any]]]:
    """mootdx: 当日分笔成交（仅交易时段可用）。"""
    cli = _get_client()
    if cli is None:
        return None
    try:
        mkt = _market(code)
        result = cli.client.get_transaction_data(mkt, code, 0, min(limit, 2000))
        if not result:
            return None
        from mootdx.utils import to_data
        df = to_data(result)
        if df is None or df.empty:
            return None
        out = []
        for _, r in df.iterrows():
            out.append({
                "time": str(r.get("datetime", r.get("time", ""))),
                "price": float(r.get("price", 0)),
                "vol": float(r.get("vol", 0)),
                "amount": float(r.get("amount", 0)),
                "buy_sell": str(r.get("buyorsell", "")),  # 0=买 1=卖 2=中性
            })
        logger.info("[mootdx] 当日分笔 %s: %d 条", code, len(out))
        return out
    except Exception as e:
        logger.warning("[mootdx] 当日分笔失败(%s): %s", code, e)
        return None


# ══════════════════════════════════════════════════════════════
#  3. 历史分笔成交
# ══════════════════════════════════════════════════════════════

def _ticks_history_mootdx(code: str, date: str, limit: int = 2000) -> Optional[List[Dict[str, Any]]]:
    """mootdx: 历史分笔成交。date 格式 YYYYMMDD。"""
    cli = _get_client()
    if cli is None:
        return None
    try:
        mkt = _market(code)
        date_str = date.replace("-", "")
        result = cli.client.get_history_transaction_data(mkt, code, 0, min(limit, 2000), int(date_str))
        if not result:
            return None
        from mootdx.utils import to_data
        df = to_data(result)
        if df is None or df.empty:
            return None
        out = []
        for _, r in df.iterrows():
            out.append({
                "time": str(r.get("datetime", r.get("time", ""))),
                "price": float(r.get("price", 0)),
                "vol": float(r.get("vol", 0)),
                "amount": float(r.get("amount", 0)),
                "buy_sell": str(r.get("buyorsell", "")),
            })
        logger.info("[mootdx] 历史分笔 %s %s: %d 条", code, date, len(out))
        return out
    except Exception as e:
        logger.warning("[mootdx] 历史分笔失败(%s, %s): %s", code, date, e)
        return None


# ══════════════════════════════════════════════════════════════
#  对外接口
# ══════════════════════════════════════════════════════════════

def get_order_book(code: str) -> Dict[str, Any]:
    """获取五档盘口 + 实时行情快照

    Args:
        code: 股票/指数代码，如 "600519"、"000001"

    Returns:
        {code, name, price, open, high, low, last_close, change, change_percent,
         volume, amount, bid: {bid1~bid5: {price, vol}}, ask: {ask1~ask5: {price, vol}}}
    """
    data = _quote_mootdx(code)
    if data:
        return data

    data = _quote_tencent(code)
    if data:
        return data

    logger.error("所有数据源获取五档行情均失败: %s", code)
    return {"code": code, "error": "获取失败"}


def get_ticks_today(code: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """获取当日分笔成交（仅交易时段可用）

    Args:
        code: 股票代码，如 "600519"
        limit: 最大条数，默认 2000

    Returns:
        [{time, price, vol, amount, buy_sell}, ...]
        buy_sell: "0"=主动买 "1"=主动卖 "2"=中性
    """
    data = _ticks_today_mootdx(code, limit)
    if data:
        return data

    logger.error("获取当日分笔失败: %s", code)
    return []


def get_ticks_history(code: str, date: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """获取历史分笔成交

    Args:
        code: 股票代码，如 "600519"
        date: 日期，"YYYY-MM-DD" 或 "YYYYMMDD"
        limit: 最大条数，默认 2000

    Returns:
        [{time, price, vol, amount, buy_sell}, ...]
    """
    data = _ticks_history_mootdx(code, date, limit)
    if data:
        return data

    logger.error("获取历史分笔失败: %s %s", code, date)
    return []


# ══════════════════════════════════════════════════════════════
#  4. 个股资金流向 — mootdx 分笔计算 + 东财降级
# ══════════════════════════════════════════════════════════════

# 单笔金额阈值 (元)
_SUPER_THRESHOLD = 2_000_000   # 超大单 >= 200万
_LARGE_THRESHOLD = 200_000     # 大单   >= 20万
_MID_THRESHOLD = 40_000        # 中单   >= 4万
# 小单 < 4万


def _classify_order(amount: float) -> str:
    """单笔成交金额 → 订单类型。"""
    if amount >= _SUPER_THRESHOLD:
        return "super"
    if amount >= _LARGE_THRESHOLD:
        return "large"
    if amount >= _MID_THRESHOLD:
        return "mid"
    return "small"


def _calc_flow_from_ticks(ticks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从分笔数据计算资金流向。

    每笔成交按 buy_sell 分主动买/卖，按金额分超大/大/中/小单。
    净流入 = 主动买入金额 - 主动卖出金额
    """
    flow = {
        "super_net": 0.0, "super_buy": 0.0, "super_sell": 0.0,
        "large_net": 0.0,  "large_buy": 0.0,  "large_sell": 0.0,
        "mid_net": 0.0,    "mid_buy": 0.0,    "mid_sell": 0.0,
        "small_net": 0.0,  "small_buy": 0.0,  "small_sell": 0.0,
    }
    total_buy = 0.0
    total_sell = 0.0

    for t in ticks:
        amount = float(t.get("amount", 0))
        buy_sell = str(t.get("buy_sell", "2"))
        cat = _classify_order(amount)

        if buy_sell == "0":  # 主动买
            flow[f"{cat}_buy"] += amount
            flow[f"{cat}_net"] += amount
            total_buy += amount
        elif buy_sell == "1":  # 主动卖
            flow[f"{cat}_sell"] += amount
            flow[f"{cat}_net"] -= amount
            total_sell += amount
        # "2" 中性不计入

    main_net = flow["super_net"] + flow["large_net"]
    return {
        "ticks_count": len(ticks),
        "total_buy": round(total_buy, 2),
        "total_sell": round(total_sell, 2),
        "total_net": round(total_buy - total_sell, 2),
        "main_net": round(main_net, 2),          # 主力净流入 = 超大+大单
        "super_net": round(flow["super_net"], 2),
        "large_net": round(flow["large_net"], 2),
        "mid_net": round(flow["mid_net"], 2),
        "small_net": round(flow["small_net"], 2),
        "detail": {
            "super": {"buy": round(flow["super_buy"], 2), "sell": round(flow["super_sell"], 2), "net": round(flow["super_net"], 2)},
            "large": {"buy": round(flow["large_buy"], 2), "sell": round(flow["large_sell"], 2), "net": round(flow["large_net"], 2)},
            "mid":   {"buy": round(flow["mid_buy"], 2),   "sell": round(flow["mid_sell"], 2),   "net": round(flow["mid_net"], 2)},
            "small": {"buy": round(flow["small_buy"], 2), "sell": round(flow["small_sell"], 2), "net": round(flow["small_net"], 2)},
        },
    }


def get_fund_flow_from_ticks(code: str, limit: int = 2000) -> Dict[str, Any]:
    """通过 mootdx 分笔数据计算当日资金流向（主源）

    从 get_transaction_data 拉取当日分笔，按单笔金额分类统计：
      超大单 >= 200万 | 大单 >= 20万 | 中单 >= 4万 | 小单 < 4万
    主力净流入 = 超大单净流入 + 大单净流入

    Args:
        code: 股票代码，如 "600519"
        limit: 最大分笔条数，默认 2000

    Returns:
        {code, source, ticks_count, total_buy, total_sell, total_net,
         main_net, super_net, large_net, mid_net, small_net, detail}
    """
    ticks = _ticks_today_mootdx(code, limit)
    if not ticks:
        return {"code": code, "source": "mootdx", "error": "非交易时段或无数据"}

    result = _calc_flow_from_ticks(ticks)
    result["code"] = code
    result["source"] = "mootdx"
    return result


def get_fund_flow_history_from_ticks(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """通过 mootdx 历史分笔计算指定日期资金流向

    Args:
        code: 股票代码
        date: 日期 "YYYY-MM-DD" 或 "YYYYMMDD"
        limit: 最大分笔条数

    Returns:
        同 get_fund_flow_from_ticks
    """
    ticks = _ticks_history_mootdx(code, date, limit)
    if not ticks:
        return {"code": code, "date": date, "source": "mootdx", "error": "无数据"}

    result = _calc_flow_from_ticks(ticks)
    result["code"] = code
    result["date"] = date
    result["source"] = "mootdx"
    return result

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _secid(code: str) -> str:
    """代码 → 东财 secid（1.600519 / 0.000858）。"""
    return f"1.{code}" if _market(code) == 1 else f"0.{code}"


def _safe_float(v) -> float:
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return 0.0


def get_fund_flow_realtime(code: str) -> Dict[str, Any]:
    """获取当日分钟级资金流向（东财 push2）

    Args:
        code: 股票代码，如 "600519"

    Returns:
        {
            code, points, total_main_net,
            data: [{time, main_net, small_net, mid_net, large_net, super_net}, ...]
        }
        主力=超大单+大单, 单位: 元
    """
    import requests
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "secid": _secid(code), "klt": 1,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        d = r.json()
        rows = []
        for line in d.get("data", {}).get("klines", []):
            p = line.split(",")
            if len(p) >= 6:
                rows.append({
                    "time": p[0],
                    "main_net": _safe_float(p[1]),      # 主力净流入
                    "small_net": _safe_float(p[2]),      # 小单净流入
                    "mid_net": _safe_float(p[3]),        # 中单净流入
                    "large_net": _safe_float(p[4]),      # 大单净流入
                    "super_net": _safe_float(p[5]),      # 超大单净流入
                })
        total_main = sum(r["main_net"] for r in rows)
        return {
            "code": code,
            "points": len(rows),
            "total_main_net": round(total_main, 2),
            "data": rows,
        }
    except Exception as e:
        logger.warning("[eastmoney] 分钟资金流失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}


def get_fund_flow_daily(code: str, days: int = 120) -> Dict[str, Any]:
    """获取近 N 日日级资金流向（东财 push2his）

    Args:
        code: 股票代码，如 "600519"
        days: 获取天数，默认 120

    Returns:
        {
            code, total_days, recent_20d_main_net,
            data: [{date, main_net, small_net, mid_net, large_net, super_net}, ...]
        }
    """
    import requests
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": str(days),
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        rows = []
        for line in d.get("data", {}).get("klines", []):
            p = line.split(",")
            if len(p) >= 6:
                rows.append({
                    "date": p[0],
                    "main_net": _safe_float(p[1]),
                    "small_net": _safe_float(p[2]),
                    "mid_net": _safe_float(p[3]),
                    "large_net": _safe_float(p[4]),
                    "super_net": _safe_float(p[5]),
                })
        recent_20 = rows[-20:] if len(rows) >= 20 else rows
        return {
            "code": code,
            "total_days": len(rows),
            "recent_20d_main_net": round(sum(r["main_net"] for r in recent_20), 2),
            "data": rows,
        }
    except Exception as e:
        logger.warning("[eastmoney] 日级资金流失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}
