# -*- coding: utf-8 -*-
"""
信号层工具 — 热点题材归因、北向资金、概念板块、限售解禁、行业排名、龙虎榜详情。

数据来源：同花顺 / 东财 push2 / 东财 datacenter / market_cn.index / market_cn.dragon_limit
"""
from __future__ import annotations

from app.agent.tools.finance.em_utils import em_datacenter
def _strip_prefix(s):
    from app.data_sources.normalizer import strip_market_prefix
    return strip_market_prefix(s)

from app.agent.log import logger
from datetime import datetime, timedelta
from typing import Any, Dict, List

import requests
from app.agent.utils.md_format import _batch_execute, _to_md

def _safe_float(v, default=0.0):
    from app.data_sources.normalizer import safe_float
    return safe_float(v, default)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _resolve_data_date(requested: str) -> tuple[str, str]:
    """把请求日期解析为真实数据日期（2026-09-16 盘前事故修复）。

    数据源（10jqka 等）对未收盘日期的请求返回**最近收盘日**的数据但响应不含真实日期，
    旧代码把请求日期当数据日期返回 → 模型把昨日收盘数据当成今日实时（盘前'今日涨停'幻觉）。
    规则：请求日期是交易日且已过 15:00 → 原样；否则回落到最近已收盘交易日。
    返回 (data_date, market_state)：market_state ∈ {closed_today, intraday, pre_open, non_trading_day}
    """
    import os as _os
    from datetime import datetime as _dt
    try:
        from app.utils.trading_calendar import is_trading_day
    except Exception:
        is_trading_day = lambda d: _dt.strptime(d, '%Y-%m-%d').weekday() < 5

    today = _dt.now().strftime('%Y-%m-%d')
    if requested != today:
        # 历史日期：数据即该日（不再校验盘面状态）
        return requested, ('trading_day' if is_trading_day(requested) else 'non_trading_day')
    # 请求的就是今天
    if not is_trading_day(today):
        return requested, 'non_trading_day'
    now = _dt.now()
    if now.hour >= 15:
        return today, 'closed_today'
    if 9 <= now.hour < 15:
        return today, 'intraday'
    return today, 'pre_open'  # 开盘前（<9:00）：当日数据未产生，数据源会给最近收盘日


def get_hot_stocks_with_reason(date: str = "") -> dict:
    """当日强势股：同花顺数据源，返回涨幅居前个股及其涨停/强势的题材归因。

    Args:
        date: 日期 YYYY-MM-DD，默认今天

    Returns:
        {"date": str, "total": int, "stocks": [{code, name, change_pct, reason, ...}],
         "hot_tags": [(题材, 次数), ...]} —— dict，股票列表在二级键 stocks 下，
        不要对返回值直接切片/迭代；接口异常时返回 {"error": ...}
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    # 盘前/非交易日：请求日期无当日数据，数据源会返回最近收盘日数据但响应不带真实日期
    # → 必须把返回值的 date 标成真实数据日期，否则模型把昨日数据当今日实时（2026-09-16 盘前事故）
    data_date, market_state = _resolve_data_date(date)

    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            return {"error": f"同花顺热点错误: {data.get('errormsg', '')}"}

        rows = data.get("data") or []
        stocks = []
        for row in rows:
            stocks.append({
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "reason": row.get("reason", ""),
                "close": _safe_float(row.get("close")),
                "change_pct": _safe_float(row.get("zhangfu")),
                "turnover_pct": _safe_float(row.get("huanshou")),
                "amount": _safe_float(row.get("chengjiaoe")),
                "dde_net": _safe_float(row.get("ddejingliang")),
            })

        from collections import Counter
        tag_counter: Counter = Counter()
        for s in stocks:
            if s["reason"]:
                tags = [t.strip() for t in s["reason"].split("+") if t.strip()]
                tag_counter.update(tags)

        # ── 精简字段：去掉 close/turnover/amount/dde，保留 code/name/change_pct/reason ──
        hot_tags = tag_counter.most_common(15)

        compact_stocks = []
        for s in stocks[:50]:
            compact_stocks.append({
                "code": s["code"],
                "name": s["name"],
                "change_pct": s["change_pct"],
                "reason": s["reason"],
            })

        _r = {
            "date": data_date,          # 真实数据日期（≠请求日期，盘前/非交易日时回落）
            "market_state": market_state,
            "requested_date": date,
            "total": len(stocks),
            "stocks": compact_stocks,
            "hot_tags": hot_tags,
        }
        return _r
    except Exception as e:
        logger.warning("get_hot_stocks_with_reason(%s) failed: %s", date, e)
        return {"error": str(e)}
# ══════════════════════════════════════════════════════════════
# 概念板块归属
# ══════════════════════════════════════════════════════════════

def get_stock_concept_blocks(codes: str) -> dict:
    """个股概念归属：返回股票所属的行业板块和概念板块列表。

    Returns:
        统一结构（单/多股一致，2026-09-22 起）：{"count": N, "data": {代码: 单股结果},
        "error": None}；失败 → {"error": "...", "retriable": False}。单股结果字段：{stock_code, total, boards:[{name,code,change_pct,lead_stock}],
        concept_tags:[...]}（板块列表在 boards）。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        from app.market_cn.eastmoney_search import _em_get

        code = _strip_prefix(stock_code)
        market_code = 1 if code.startswith("6") else 0
        params = {
            "fltt": "2", "invt": "2",
            "secid": f"{market_code}.{code}",
            "spt": "3", "pi": "0", "pz": "200", "po": "1",
            "fields": "f12,f14,f3,f128",
        }
        headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
        try:
            r = _em_get("https://push2.eastmoney.com/api/qt/slist/get",
                         params=params, headers=headers, timeout=15)
            d = r.json()
            diff = (d.get("data") or {}).get("diff") or {}
            items = diff.values() if isinstance(diff, dict) else diff
            boards = []
            for it in items:
                boards.append({
                    "name": it.get("f14", ""),
                    "code": it.get("f12", ""),
                    "change_pct": it.get("f3", ""),
                    "lead_stock": it.get("f128", ""),
                })
            return {
                "stock_code": code,
                "total": len(boards),
                "boards": boards,
                "concept_tags": [b["name"] for b in boards],
            }
        except Exception as e:
            logger.warning("get_stock_concept_blocks(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    return _batch_execute(_one, code_list)
# ══════════════════════════════════════════════════════════════
# 限售解禁
# ══════════════════════════════════════════════════════════════

def get_lockup_expiry(codes: str, forward_days: int = 90) -> dict:
    """限售解禁日历：返回指定股票未来解禁日期、解禁数量、占总股本比例。

    Args:
        codes: 逗号分隔的股票代码，如 "002475" 或 "002475,600519"
        forward_days: 向前看的天数，默认90天
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        code = _strip_prefix(stock_code)
        today = datetime.now().strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=forward_days)).strftime("%Y-%m-%d")

        history_data = em_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f'(SECURITY_CODE="{code}")',
            page_size=15,
            sort_columns="FREE_DATE", sort_types="-1",
        )
        history = []
        for row in history_data:
            history.append({
                "date": str(row.get("FREE_DATE", ""))[:10],
                "type": row.get("LIMITED_STOCK_TYPE", ""),
                "shares": row.get("FREE_SHARES_NUM", 0),
                "ratio": _safe_float(row.get("FREE_RATIO")),
            })

        upcoming_data = em_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{today}\')(FREE_DATE<=\'{end_date}\')',
            page_size=20,
            sort_columns="FREE_DATE", sort_types="1",
        )
        upcoming = []
        for row in upcoming_data:
            upcoming.append({
                "date": str(row.get("FREE_DATE", ""))[:10],
                "type": row.get("LIMITED_STOCK_TYPE", ""),
                "shares": row.get("FREE_SHARES_NUM", 0),
                "ratio": _safe_float(row.get("FREE_RATIO")),
            })

        _r = {
            "stock_code": code,
            "history": history,
            "upcoming": upcoming,
            "upcoming_count": len(upcoming),
        }
        return _r

    return _batch_execute(_one, code_list)
# ══════════════════════════════════════════════════════════════
# 行业排名
# ══════════════════════════════════════════════════════════════

def get_industry_ranking(top_n: int = 20) -> dict:
    """行业涨跌幅排名：返回当日各行业板块涨跌幅、领涨股、成交额排名。

    数据源：market_cn.hot_sectors（东财 + 新浪双源）

    Returns:
        dict: {top:[{name,code,change_pct,lead_stock,limit_up_count,...}], total}。行业列表在 r['top']（list）；异常含 error。

    Args:
        top_n: 返回前N个行业，默认20
    """
    from app.market_cn.hot_sectors import get_hot_industry_boards as _get
    try:
        data = _get(limit=top_n)
        return {"top": data[:top_n], "total": len(data)}
    except Exception as e:
        logger.warning("get_industry_ranking failed: %s", e)
        return {"error": str(e)}
# ══════════════════════════════════════════════════════════════
# 龙虎榜详情
# ══════════════════════════════════════════════════════════════

def get_dragon_tiger_detail(codes: str, look_back_days: int = 30) -> dict:
    """龙虎榜详情：返回个股上榜日期、买卖席位明细、机构/游资动向。

    Args:
        codes: 逗号分隔的股票代码，如 "002475" 或 "002475,600519"
        look_back_days: 回看天数，默认30
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        code = _strip_prefix(stock_code)
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

        data = em_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{today}')(SECURITY_CODE=\"{code}\")",
            page_size=50,
            sort_columns="TRADE_DATE", sort_types="-1",
        )
        records = []
        for row in data:
            records.append({
                "date": str(row.get("TRADE_DATE", ""))[:10],
                "reason": row.get("EXPLANATION", ""),
                "net_buy_wan": round(_safe_float(row.get("BILLBOARD_NET_AMT")) / 10000, 1),
                "turnover_pct": round(_safe_float(row.get("TURNOVERRATE")), 2),
            })

        seats = {"buy": [], "sell": []}
        buy_data = []
        sell_data = []
        if records:
            latest_date = records[0]["date"]
            buy_data = em_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10, sort_columns="BUY", sort_types="-1",
            )
            for row in buy_data[:5]:
                seats["buy"].append({
                    "name": row.get("OPERATEDEPT_NAME", ""),
                    "buy_wan": round(_safe_float(row.get("BUY")) / 10000, 1),
                    "sell_wan": round(_safe_float(row.get("SELL")) / 10000, 1),
                    "net_wan": round(_safe_float(row.get("NET")) / 10000, 1),
                })

            sell_data = em_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10, sort_columns="SELL", sort_types="-1",
            )
            for row in sell_data[:5]:
                seats["sell"].append({
                    "name": row.get("OPERATEDEPT_NAME", ""),
                    "buy_wan": round(_safe_float(row.get("BUY")) / 10000, 1),
                    "sell_wan": round(_safe_float(row.get("SELL")) / 10000, 1),
                    "net_wan": round(_safe_float(row.get("NET")) / 10000, 1),
                })

        institution = {"buy_wan": 0, "sell_wan": 0, "net_wan": 0}
        for detail_data, side in [(buy_data, "buy"), (sell_data, "sell")]:
            for row in detail_data:
                if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                    amt = _safe_float(row.get("BUY") if side == "buy" else row.get("SELL"))
                    if side == "buy":
                        institution["buy_wan"] += amt
                    else:
                        institution["sell_wan"] += amt
        institution["buy_wan"] = round(institution["buy_wan"] / 10000, 1)
        institution["sell_wan"] = round(institution["sell_wan"] / 10000, 1)
        institution["net_wan"] = round(institution["buy_wan"] - institution["sell_wan"], 1)

        _r = {
            "stock_code": code,
            "records": records,
            "seats": seats,
            "institution": institution,
        }
        return _r

    return _batch_execute(_one, code_list)
