# -*- coding: utf-8 -*-
"""provider_tape / sina —— 分笔与盘口族新浪源（orderbook + ticks_today + ticks_history）。

新浪 hq.sinajs.cn 五档盘口 + stock.gtimg.cn 当日分笔 + CN_Bill.GetBillList 历史逐笔。
字段与 mootdx/tencent 对齐:
  orderbook → {code, name, price, open, high, low, last_close, change, change_percent,
               volume(股), amount(元), bid, ask}
  ticks     → {time, price, vol(手), amount(元), buy_sell}
历史逐笔（ticks_history）2026-10-29 由恒空假源改为真实现，详见 fetch_ticks_history。
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


def _sina_code(code: str) -> str:
    """纯代码 → 新浪前缀（sh600519 / sz000001）。"""
    c = (code or "").strip()
    if c[:1] in ("6", "9"):
        return f"sh{c}"
    return f"sz{c}"


# ── orderbook (五档盘口) ────────────────────────────────────

def _orderbook(code: str) -> Dict[str, Any]:
    """五档盘口（新浪 hq.sinajs.cn）。

    返回 normalize_quote 格式: {last, change, changePercent, high, low, open,
    previousClose, volume, time, name, symbol, bid, ask}
    """
    import urllib.request

    sc = _sina_code(code)
    url = f"https://hq.sinajs.cn/list={sc}"
    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("[sina] orderbook %s 失败: %s", code, e)
        return {"object": None}

    # 解析: var hq_str_sh600519="贵州茅台,1349.98,...";
    m = re.search(r'"([^"]+)"', raw)
    if not m:
        return {"object": None}
    parts = m.group(1).split(",")
    if len(parts) < 32:
        return {"object": None}

    try:
        name = parts[0]
        open_p = float(parts[1])
        prev = float(parts[2])
        price = float(parts[3])
        high = float(parts[4])
        low = float(parts[5])
        vol = float(parts[8])  # 成交量(股)
        amount = float(parts[9])  # 成交额(元)
        # 五档: 买一~五档 (parts[10:20]), 卖一~五档 (parts[20:30])
        # ⚠ 新浪是「量在前、价在后」：parts[10]=买一量 parts[11]=买一价，
        #   parts[20]=卖一量 parts[21]=卖一价（2026-10-28 原始串实测复核）。
        #   早期实现把二者取反，导致 price 字段是股数、vol 字段是价格。
        bid = {}
        ask = {}
        for i in range(5):
            bv = float(parts[10 + i * 2] or 0)
            bp = float(parts[11 + i * 2] or 0)
            sv = float(parts[20 + i * 2] or 0)
            sp = float(parts[21 + i * 2] or 0)
            bid[f"bid{i+1}"] = {"price": bp, "vol": bv}
            ask[f"ask{i+1}"] = {"price": sp, "vol": sv}
    except (ValueError, IndexError):
        return {"object": None}

    # 字段口径 = 族内 mootdx/tencent 的形状（外围 tape.py 与 agent 工具只读这套）：
    #   price/open/high/low/last_close/change/change_percent/code/name/volume/amount
    # 2026-10-29 前本函数输出的是 normalize_quote 形状（last/changePercent/previousClose
    # /symbol），而 orderbook 的主消费方 `market_cn/tape.py:597-609` 读的是
    # price/open/high/low/last_close/change_percent/amount ⇒ 主源命中时七个字段恒 0，
    # 换手率与快照行情全废。按「多源归一化铁律：以库表/族内消费口径为基准」改为本形状。
    # 单位（2026-10-29 实测 600519: vol=3880493 / amount=5187320000）：
    #   parts[8] 成交量=股、parts[9] 成交额=元 —— 与 mootdx/tencent 源标定后一致。
    result = {
        "code": code,
        "name": name,
        "price": price,
        "open": open_p,
        "high": high,
        "low": low,
        "last_close": prev,
        "change": round(price - prev, 4) if prev else 0,
        "change_percent": round((price - prev) / prev * 100, 2) if prev else 0,
        "volume": vol,
        "amount": amount,
        "bid": bid,
        "ask": ask,
    }
    return {"object": result}


# ── ticks_today (当日分笔) ─────────────────────────────────

def fetch_ticks_today(code: str, limit: int = 2000) -> Dict[str, Any]:
    """当日分笔成交（新浪 stock.gtimg.cn，与 tencent 同格式）。

    每条: index/time/price/change/vol/amount/direction
    归一化: buy_sell "0"=买 "1"=卖 "2"=中性
    """
    import urllib.request

    sc = _sina_code(code)
    url = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={sc}&p=0&l={min(limit, 2000)}"

    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("[sina] ticks_today %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}

    m = re.search(r'"([^"]+)"', raw)
    if not m:
        return {"rows": [], "error": "no data"}

    out = []
    for seg in m.group(1).split("|"):
        parts = seg.split("/")
        if len(parts) < 6:
            continue
        try:
            t = parts[1]
            p = float(parts[2])
            v = float(parts[4])
            a = float(parts[5])
            bs_raw = parts[6] if len(parts) > 6 else ""
        except (ValueError, IndexError):
            continue

        bs_upper = str(bs_raw).upper()
        if bs_upper == "B":
            buy_sell = "0"
        elif bs_upper == "S":
            buy_sell = "1"
        else:
            buy_sell = "2"

        out.append({"time": t, "price": p, "vol": v, "amount": a, "buy_sell": buy_sell})

    return {"rows": out[:limit]}


_BILL_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "CN_Bill.GetBillList")
_BILL_MAX_NUM = 20000   # 上游 num 上限实测：num ≥ 当日总笔数时一页返回全天（600519=3708 笔）


def _bill_day(date: str) -> str:
    """入参日期 → 上游 day= 要求的 "YYYY-MM-DD"（兼容 "YYYYMMDD"）。"""
    s = (date or "").strip()
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s


def fetch_ticks_history(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """历史逐笔成交（新浪 CN_Bill.GetBillList，支持指定交易日）。

    ⚠ 2026-10-29 前本函数恒返回 `{"rows": [], "error": "新浪无历史分笔 API"}`，
    而信封校验器 `is_rows` 对**空 list 判 True** ⇒ 这个恒空假源被 dispatch 判为
    "成功"，把排在其后的 eastmoney_dc 真源永久挡在门外（P0 缺陷）。本次改为真实实现。

    上游：`vip.stock.finance.sina.com.cn/.../CN_Bill.GetBillList?symbol=sh600519
    &num=N&sort=ticktime&asc=1&volume=0&amount=0&type=0&day=YYYY-MM-DD`
    （akshare `stock_intraday_sina` 用的同一通道，不另造轮子）。
    一次请求即可取全天：num ≥ 当日总笔数时返回全部（实测 num=5000/20000 均返回 3708）。

    归一化（与 tencent/mootdx/eastmoney_dc 的 ticks 契约一致）:
        time=ticktime(HH:MM:SS) / price=成交价 / vol=成交量(**手**，上游股÷100)
        / amount=成交额(**元**，价×股，数学恒等非估算) / buy_sell: "0"买 "1"卖 "2"中性
    ⚠ buy_sell 由上游 `kind` 映射，实测（2026-10-29，600519/000001/300750 各 1000 笔
    按 (time,price) 与腾讯 detail 逐笔对拍，**命中 792 笔零反例**）：
        U→主动买(B) / D→主动卖(S) / E→中性(M) ⇒ 与腾讯 B/S/M、东财 BUY_SELL 同维度。

    边界（2026-10-29 实测）: 上游仅保留**最近约 2~3 周**（10-13 可取、10-06 及更早
    返回 0 条）；超出窗口或非交易日 ⇒ 0 条 ⇒ **抛异常**（空信封会挡住后续源）。

    Returns:
        {"rows": [...]}（按时间升序，截断到 limit）
    """
    import json
    import urllib.request

    day = _bill_day(date)
    if not day:
        raise ValueError("sina ticks_history 缺少 date")

    sc = _sina_code(code)
    url = ("%s?symbol=%s&num=%d&sort=ticktime&asc=1&volume=0&amount=0&type=0&day=%s&page=1"
           % (_BILL_URL, sc, _BILL_MAX_NUM, day))
    try:
        req = urllib.request.Request(url, headers=dict(
            _HTTP_HEADERS,
            Referer="https://vip.stock.finance.sina.com.cn/quotes_service/view/"
                    "cn_bill.php?symbol=%s" % sc))
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception as e:
        logger.warning("[sina] ticks_history %s %s 失败: %s", code, day, e)
        raise RuntimeError("sina ticks_history 请求失败: %s" % e)

    try:
        items = json.loads(raw)
    except ValueError as e:
        raise RuntimeError("sina ticks_history 响应非 JSON: %s" % e)
    if not isinstance(items, list) or not items:
        # 空/超窗口：抛出让 dispatch 换源，不返回空信封
        raise RuntimeError("sina ticks_history 无数据（%s %s，超保留窗口或非交易日）"
                           % (code, day))

    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            price = float(it.get("price"))
            shares = float(it.get("volume"))       # 上游单位：股
        except (TypeError, ValueError):
            continue
        kind = str(it.get("kind", "") or "").upper()
        buy_sell = "0" if kind == "U" else ("1" if kind == "D" else "2")
        rows.append({
            "time": str(it.get("ticktime", "")),
            "price": price,
            "vol": round(shares / 100.0, 2),        # 股 → 手
            "amount": round(price * shares, 2),     # 元
            "buy_sell": buy_sell,
        })

    if not rows:
        raise RuntimeError("sina ticks_history 解析后为空（%s %s）" % (code, day))
    return {"rows": rows[:max(1, int(limit or 2000))]}


from app.data_sources.provider_tape import register  # noqa: E402


@register
class SinaTapeProvider:
    name = "sina"
    priority = 15
    markets = {"CNStock"}
    capabilities = {
        "orderbook": _orderbook,
        "ticks_today": fetch_ticks_today,
        "ticks_history": fetch_ticks_history,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
