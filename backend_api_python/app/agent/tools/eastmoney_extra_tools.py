# -*- coding: utf-8 -*-
"""
EastMoney Extra Tools — 补充 QuantDinger 缺失的 A 股数据端点。

数据来源：a-stock-data (https://github.com/simonlin1212/a-stock-data) V3.2.2
覆盖层级：
  - 研报层：研报列表+评级+EPS预测、一致预期EPS
  - 信号层：同花顺热点+题材归因、北向资金、概念板块归属、限售解禁、行业排名
  - 资金面：融资融券、大宗交易、股东户数、分红送转、资金流120日
  - 新闻层：东财个股新闻、全球资讯
  - 基础数据：新浪财报三表

所有东财请求走 em_get() 内置限流防封（串行 ≥1s + 随机抖动 + 会话复用）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import safe_float as _safe_float
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# 东财防封：全局节流 + 会话复用
# ══════════════════════════════════════════════════════════════

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": _UA})
_EM_MIN_INTERVAL = float(os.getenv("EM_MIN_INTERVAL", "1.0"))
_em_last_call = [0.0]


def _em_get(url: str, params: dict = None, headers: dict = None,
            timeout: int = 15, **kwargs) -> requests.Response:
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA。"""
    wait = _EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + __import__("random").uniform(0.1, 0.5))
    try:
        return _EM_SESSION.get(url, params=params, headers=headers,
                               timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()


def _em_datacenter(report_name: str, columns: str = "ALL",
                   filter_str: str = "", page_size: int = 50,
                   sort_columns: str = "", sort_types: str = "-1") -> list:
    """东财数据中心统一查询（龙虎榜/解禁/融资融券/大宗/股东户数/分红共用）。"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = _em_get(_DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


def _stock_code_normalize(code: str) -> str:
    """归一化股票代码为纯 6 位数字。"""
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code


def _market_prefix(code: str) -> str:
    """6位代码 → sh/sz/bj 前缀。"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


# ══════════════════════════════════════════════════════════════
# Layer 2: 研报层
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线] 研报评级+EPS预测。机构对个股的评级(买入/增持)和未来三年EPS预测。中线持仓必查：机构覆盖密度高=共识强，评级上调=预期改善。配合一致预期EPS做估值。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_stock_reports(stock_code: str, max_pages: int = 3) -> Dict[str, Any]:
    """获取个股研报列表（东财 reportapi）。

    Args:
        stock_code: 股票代码（如 600519）
        max_pages: 最大页数，默认3
    """
    code = _stock_code_normalize(stock_code)
    report_api = "https://reportapi.eastmoney.com/report/list"
    all_records = []

    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page),
        }
        try:
            r = _em_get(report_api, params=params,
                        headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            all_records.extend(rows)
            if page >= (d.get("TotalPage", 1) or 1):
                break
        except Exception as e:
            logger.warning("get_stock_reports(%s) page %d failed: %s", code, page, e)
            break

    reports = []
    for row in all_records[:50]:  # 最多返回50条
        reports.append({
            "title": row.get("title", ""),
            "date": (row.get("publishDate") or "")[:10],
            "org": row.get("orgSName", ""),
            "rating": row.get("emRatingName", ""),
            "industry": row.get("indvInduName", ""),
            "eps_this_year": row.get("predictThisYearEps"),
            "eps_next_year": row.get("predictNextYearEps"),
            "eps_next2_year": row.get("predictNextTwoYearEps"),
            "info_code": row.get("infoCode", ""),
        })

    return {
        "stock_code": code,
        "total": len(all_records),
        "reports": reports,
    }


@tool(
    description="[中线] 机构一致预期EPS。预测机构数、均值、最大最小值。算前向PE/PEG的核心输入：当前价÷一致预期EPS=前向PE。机构数<3的要谨慎。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_consensus_eps(stock_code: str) -> Dict[str, Any]:
    """获取同花顺机构一致预期EPS。

    Args:
        stock_code: 股票代码（如 688017）
    """
    code = _stock_code_normalize(stock_code)
    try:
        import pandas as pd
        from io import StringIO

        url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
        headers = {
            "User-Agent": _UA,
            "Referer": "https://basic.10jqka.com.cn/",
        }
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        dfs = pd.read_html(StringIO(r.text))

        # 找含"每股收益"或"均值"的表格
        target_df = None
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                target_df = df
                break
        if target_df is None and dfs:
            target_df = dfs[0]

        if target_df is not None:
            records = target_df.to_dict(orient="records")
            return {
                "stock_code": code,
                "consensus": records,
                "source": "同花顺",
            }
        return {"stock_code": code, "consensus": [], "message": "未找到一致预期数据"}
    except Exception as e:
        logger.warning("get_consensus_eps(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# Layer 3: 信号层
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线核心] 当日强势股+题材归因。同花顺编辑部人工标注的reason tags（如「算力租赁+Token工厂」）。短线打板/跟题材必用：先看哪些题材在发酵，再找龙头。含涨幅、换手、大单净量。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_hot_stocks_with_reason(date: str = "") -> Dict[str, Any]:
    """获取同花顺当日强势股+题材归因。

    Args:
        date: 日期 YYYY-MM-DD，默认今天
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

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

        # 题材热度统计
        from collections import Counter
        tag_counter: Counter = Counter()
        for s in stocks:
            if s["reason"]:
                tags = [t.strip() for t in s["reason"].split("+") if t.strip()]
                tag_counter.update(tags)

        return {
            "date": date,
            "total": len(stocks),
            "stocks": stocks[:50],
            "hot_tags": tag_counter.most_common(15),
        }
    except Exception as e:
        logger.warning("get_hot_stocks_with_reason(%s) failed: %s", date, e)
        return {"error": str(e)}


@tool(
    description="[短线+中线] 北向资金实时流向。沪股通/深股通分钟级净买入（262个时间点）。外资是A股边际定价力量：北向大幅流入=市场偏强，持续流出=谨慎。盘中看实时，收盘看累计。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_northbound_flow() -> Dict[str, Any]:
    """获取北向资金实时分钟流向（同花顺 hsgtApi）。"""
    try:
        from app.market_cn.index import get_northbound_realtime
        return get_northbound_realtime()
    except Exception as e:
        logger.warning("get_northbound_flow failed: %s", e)
        return {"error": str(e)}


@tool(
    description="[短线+中线] 个股概念板块归属。一次拿全所属板块（行业/概念/地域）+板块涨跌幅+龙头股。短线看题材联动（板块涨=个股跟涨概率大），中线看行业赛道。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_stock_concept_blocks(stock_code: str) -> Dict[str, Any]:
    """获取个股所属板块/概念归属（东财 slist）。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
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


@tool(
    description="[中线] 限售解禁日历。历史解禁+未来90天待解禁。中线持仓必查风险项：大比例解禁前1-2周通常有抛压，首发原股东解禁尤其注意。解禁比例>5%需警惕。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_lockup_expiry(stock_code: str, forward_days: int = 90) -> Dict[str, Any]:
    """获取限售解禁日历。

    Args:
        stock_code: 股票代码（如 002475）
        forward_days: 向前看的天数，默认90天
    """
    code = _stock_code_normalize(stock_code)
    today = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=forward_days)).strftime("%Y-%m-%d")

    # 历史解禁
    history_data = _em_datacenter(
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

    # 未来待解禁
    upcoming_data = _em_datacenter(
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

    return {
        "stock_code": code,
        "history": history,
        "upcoming": upcoming,
        "upcoming_count": len(upcoming),
    }


@tool(
    description="[短线+中线] 行业板块涨跌排名（补充 get_sector_rankings：本工具直连东财push2，返回约100个行业的涨跌幅+上涨下跌家数+领涨股，数据更全）。约100个行业的涨跌幅+上涨下跌家数+领涨股。短线看当日资金主线（哪个行业在涨），中线看行业轮动趋势（连续走强的行业）。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_industry_ranking(top_n: int = 20) -> Dict[str, Any]:
    """获取行业板块涨跌幅排名（东财行业板块）。

    Args:
        top_n: 返回前N个行业，默认20
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    headers = {"User-Agent": _UA}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return {"top": [], "bottom": [], "total": 0}

        rows = []
        for i, item in enumerate(items):
            rows.append({
                "rank": i + 1,
                "name": item.get("f14", ""),
                "change_pct": _safe_float(item.get("f3")),
                "code": item.get("f12", ""),
                "up_count": item.get("f104", 0),
                "down_count": item.get("f105", 0),
                "leader": item.get("f140", ""),
                "leader_change": _safe_float(item.get("f136")),
            })

        n = min(top_n, len(rows))
        return {
            "top": rows[:n],
            "bottom": rows[-n:],
            "total": len(rows),
        }
    except Exception as e:
        logger.warning("get_industry_ranking failed: %s", e)
        return {"error": str(e)}


@tool(
    description="[短线核心] 龙虎榜席位详情（补充 get_dragon_tiger：现有工具返回基础数据，本工具返回买卖席位TOP5+机构专用席位动向）。上榜记录+买卖席位TOP5营业部+机构专用席位动向。短线追踪游资必用：知名游资席位买入=短期有溢价预期，机构专用净买入=中线信号。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_dragon_tiger_detail(stock_code: str, look_back_days: int = 30) -> Dict[str, Any]:
    """获取个股龙虎榜详情（席位+机构）。

    Args:
        stock_code: 股票代码（如 002475）
        look_back_days: 回看天数，默认30
    """
    code = _stock_code_normalize(stock_code)
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

    # 1. 上榜记录
    data = _em_datacenter(
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

    # 2. 最近上榜的买卖席位
    seats = {"buy": [], "sell": []}
    if records:
        latest_date = records[0]["date"]
        buy_data = _em_datacenter(
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

        sell_data = _em_datacenter(
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

    # 3. 机构买卖统计（OPERATEDEPT_CODE="0" 即机构专用席位）
    institution = {"buy_wan": 0, "sell_wan": 0, "net_wan": 0}
    for detail_data, side in [(buy_data if records else [], "buy"),
                               (sell_data if records else [], "sell")]:
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

    return {
        "stock_code": code,
        "records": records,
        "seats": seats,
        "institution": institution,
    }



# ══════════════════════════════════════════════════════════════
# Layer 4: 资金面 / 筹码层
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线] 融资融券明细。日级融资余额/买入/偿还+融券余额。融资余额持续增加=杠杆资金看多，融券余额突增=空头力量增强。两融是中线情绪指标。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_margin_trading(stock_code: str, days: int = 30) -> Dict[str, Any]:
    """获取融资融券明细。

    Args:
        stock_code: 股票代码（如 600519）
        days: 返回天数，默认30
    """
    code = _stock_code_normalize(stock_code)
    data = _em_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=days,
        sort_columns="DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
            "rzye": _safe_float(row.get("RZYE")),          # 融资余额(元)
            "rzmre": _safe_float(row.get("RZMRE")),        # 融资买入额
            "rzche": _safe_float(row.get("RZCHE")),        # 融资偿还额
            "rqye": _safe_float(row.get("RQYE")),          # 融券余额(元)
            "rqmcl": _safe_float(row.get("RQMCL")),        # 融券卖出量
            "rqchl": _safe_float(row.get("RQCHL")),        # 融券偿还量
            "rzrqye": _safe_float(row.get("RZRQYE")),      # 融资融券余额合计
        })
    return {"stock_code": code, "records": rows}


@tool(
    description="[中线] 大宗交易记录。成交价/量+买卖方营业部+溢价率。溢价成交=买方看好（正面信号），折价成交=卖方急出（负面信号）。机构专用买入=中线建仓信号。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_block_trades(stock_code: str, page_size: int = 20) -> Dict[str, Any]:
    """获取大宗交易记录。

    Args:
        stock_code: 股票代码（如 600519）
        page_size: 返回条数，默认20
    """
    code = _stock_code_normalize(stock_code)
    data = _em_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size,
        sort_columns="TRADE_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        close = _safe_float(row.get("CLOSE_PRICE"))
        deal_price = _safe_float(row.get("DEAL_PRICE"))
        premium = ((deal_price / close - 1) * 100) if close else 0
        rows.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "price": deal_price,
            "close": close,
            "premium_pct": round(premium, 2),
            "vol": _safe_float(row.get("DEAL_VOLUME")),
            "amount": _safe_float(row.get("DEAL_AMT")),
            "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        })
    return {"stock_code": code, "records": rows}


@tool(
    description="[中线核心] 股东户数变化。季度股东数+环比变化+户均持股。筹码集中度核心指标：户数持续减少=主力吸筹，户数暴增=散户接盘。配合十大流通股东看机构进出。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_holder_count(stock_code: str) -> Dict[str, Any]:
    """获取股东户数变化。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    data = _em_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=10,
        sort_columns="END_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("END_DATE", ""))[:10],
            "holder_num": row.get("HOLDER_NUM", 0),
            "change_num": row.get("HOLDER_NUM_CHANGE", 0),
            "change_ratio": _safe_float(row.get("HOLDER_NUM_RATIO")),
            "avg_shares": _safe_float(row.get("AVG_FREE_SHARES")),
        })
    return {"stock_code": code, "records": rows}


@tool(
    description="[中线] 分红送转历史。每股派息/送股/转增+进度状态。高股息策略核心数据：连续高分红=现金流好、股东友好。股息率=每股派息÷股价，>3%有吸引力。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_dividend_history(stock_code: str) -> Dict[str, Any]:
    """获取分红送转历史。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    data = _em_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=20,
        sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("EX_DIVIDEND_DATE", ""))[:10],
            "bonus_rmb": _safe_float(row.get("PRETAX_BONUS_RMB")),  # 每股派息(税前)
            "transfer_ratio": _safe_float(row.get("TRANSFER_RATIO")),  # 每10股转增
            "bonus_ratio": _safe_float(row.get("BONUS_RATIO")),      # 每10股送股
            "plan": row.get("ASSIGN_PROGRESS", ""),
        })
    return {"stock_code": code, "records": rows}


@tool(
    description="[中线] 个股资金流120日（补充 get_fund_flow：本工具走push2his获取最近120个交易日的日级主力/大单净流入）。日级主力/大单/中单/小单净流入。中线趋势判断：近20日主力累计净流入=资金在建仓，持续净流出=资金在撤退。配合筹码分析。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_fund_flow_120d(stock_code: str) -> Dict[str, Any]:
    """获取个股资金流120日日级数据。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    try:
        from app.market_cn.tape import get_fund_flow_daily
        result = get_fund_flow_daily(code, 120)
        if "error" in result:
            return {"stock_code": code, "error": result["error"]}
        return {
            "stock_code": code,
            "total_days": result.get("total_days", 0),
            "recent_20d_main_net": result.get("recent_20d_main_net", 0),
            "data": result.get("data", [])[-30:],
        }
    except Exception as e:
        logger.warning("get_fund_flow_120d(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


@tool(
    description="[短线] 个股资金流分钟级（补充 get_fund_flow：现有工具走东财搜索API，本工具走push2获取当日盘中分钟级实时数据）。当日盘中主力/大单/超大单实时净流入。盘中盯资金用：超大单突然大幅流入=可能有消息或主力进场，配合盘口和成交量综合判断。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_fund_flow_minute(stock_code: str) -> Dict[str, Any]:
    """获取个股资金流向分钟级。

    Args:
        stock_code: 股票代码（如 000858）
    """
    code = _stock_code_normalize(stock_code)
    try:
        from app.market_cn.tape import get_fund_flow_realtime
        result = get_fund_flow_realtime(code)
        if "error" in result:
            return {"stock_code": code, "error": result["error"]}
        return {
            "stock_code": code,
            "points": result.get("points", 0),
            "total_main_net": result.get("total_main_net", 0),
            "data": result.get("data", []),
        }
    except Exception as e:
        logger.warning("get_fund_flow_minute(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# Layer 5: 新闻层
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线+中线] 个股新闻（补充 search_stock_news：本工具直连东财JSONP，作为 news_search_service 的备用数据源）。东财个股相关新闻流。突发利好/利空消息第一时间获取，配合热点题材判断消息面催化。新闻+概念板块交叉验证。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_eastmoney_stock_news(stock_code: str, page_size: int = 20) -> Dict[str, Any]:
    """获取东财个股新闻。

    Args:
        stock_code: 股票代码（如 688017）
        page_size: 返回条数，默认20
    """
    code = _stock_code_normalize(stock_code)
    cb = "jQuery_news"
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner_params = json.dumps({
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default",
            "pageIndex": 1, "pageSize": page_size,
            "preTag": "", "postTag": "",
        }},
    }, separators=(',', ':'))
    params = {"cb": cb, "param": inner_params}
    headers = {"User-Agent": _UA, "Referer": "https://so.eastmoney.com/"}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        text = r.text
        json_str = text[text.index("(") + 1: text.rindex(")")]
        d = json.loads(json_str)

        articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
        rows = []
        for a in articles:
            rows.append({
                "title": re.sub(r'<[^>]+>', '', a.get("title", "")),
                "content": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                "time": a.get("date", ""),
                "source": a.get("mediaName", ""),
                "url": a.get("url", ""),
            })
        return {"stock_code": code, "total": len(rows), "news": rows}
    except Exception as e:
        logger.warning("get_eastmoney_stock_news(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


@tool(
    description="[短线] 全球财经资讯。东财7×24滚动快讯。盘中突发事件监控：政策利好、外围异动、行业突发新闻。短线事件驱动策略必看。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_global_finance_news(page_size: int = 30) -> Dict[str, Any]:
    """获取东财全球财经资讯。

    Args:
        page_size: 返回条数，默认30
    """
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    params = {
        "client": "web", "biz": "web_724",
        "fastColumn": "102", "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("listData", []) or d.get("data", []) or []
        rows = []
        for item in items:
            rows.append({
                "title": item.get("title", ""),
                "summary": item.get("digest", "") or item.get("content", "")[:200],
                "time": item.get("showTime", "") or item.get("date", ""),
            })
        return {"total": len(rows), "news": rows}
    except Exception as e:
        logger.warning("get_global_finance_news failed: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# Layer 6: 基础数据层
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线] 财报三表。资产负债表/利润表/现金流量表（最近4个报告期）。中线基本面分析基础：看资产负债率判断财务安全，看现金流判断盈利质量，看利润表判断成长性。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_financial_statements(stock_code: str) -> Dict[str, Any]:
    """获取新浪财报三表。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    prefix = _market_prefix(code)
    symbol = f"{prefix}{code}"

    result = {"stock_code": code, "balance_sheet": [], "income_statement": [], "cash_flow": []}

    table_map = {
        "balance_sheet": ("zcfzb", "资产负债表"),
        "income_statement": ("lrb", "利润表"),
        "cash_flow": ("xjllb", "现金流量表"),
    }

    for key, (table_id, table_name) in table_map.items():
        try:
            url = f"https://quotes.sina.cn/cn/go.php/vFD_FinancialGuideLine/stockid/{symbol}/ctrl/{table_id}/displaytype/4.phtml"
            headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
            r = requests.get(url, headers=headers, timeout=15)
            r.encoding = "gbk"

            # 解析HTML表格
            import pandas as pd
            from io import StringIO
            dfs = pd.read_html(StringIO(r.text))
            if not dfs:
                continue

            # 新浪实际结构: report_list 按报告期为键的 dict，每期 data 是行项列表
            df = dfs[0]
            records = []
            # 取最近4列（报告期）
            cols = df.columns.tolist()
            for _, row in df.iterrows():
                item = {"item": str(row.iloc[0]) if len(row) > 0 else ""}
                for col in cols[1:5]:  # 最多4个报告期
                    item[str(col)] = row[col] if pd.notna(row[col]) else None
                records.append(item)

            result[key] = records[:30]  # 限制条数
        except Exception as e:
            logger.warning("get_financial_statements(%s) %s failed: %s", code, table_name, e)
            result[key] = []

    return result


# ══════════════════════════════════════════════════════════════
# Layer 7: 公告层
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线+中线] 个股公告。巨潮全量公告（沪深北交所）。利好公告（业绩预增/回购/增持）=短线催化，定期报告=中线基本面更新。解禁/减持公告=风险预警。",
    category="情报搜索",
    layer="分析层",
    domain=["finance"],
)
def get_stock_filings(stock_code: str, page_size: int = 20) -> Dict[str, Any]:
    """获取个股公告列表（巨潮 cninfo）。

    Args:
        stock_code: 股票代码（如 600519）
        page_size: 返回条数，默认20
    """
    code = _stock_code_normalize(stock_code)

    # 动态获取 orgId（巨潮公告需要）
    org_id = _cninfo_orgid(code)

    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    data = {
        "stock": f"{code},{org_id}" if org_id else code,
        "tabName": "fulltext",
        "pageSize": str(page_size),
        "pageNum": "1",
        "column": "sse" if code.startswith("6") else "szse",
        "category": "",
        "plate": "",
        "seDate": "",
        "searchkey": "",
        "secid": "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": _UA,
        "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        r = requests.post(url, data=data, headers=headers, timeout=15)
        d = r.json()
        announcements = d.get("announcements") or []
        rows = []
        for ann in announcements:
            rows.append({
                "title": ann.get("announcementTitle", ""),
                "date": ann.get("announcementTime", ""),
                "type": ann.get("announcementTypeName", ""),
                "url": f"http://static.cninfo.com.cn/{ann['adjunctUrl']}" if ann.get("adjunctUrl") else "",
            })
        return {"stock_code": code, "total": d.get("totalAnnouncement", 0), "filings": rows}
    except Exception as e:
        logger.warning("get_stock_filings(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


def _cninfo_orgid(code: str) -> str:
    """动态获取巨潮公告 orgId（从官方映射表 szse_stock.json）。"""
    try:
        url = "http://www.cninfo.com.cn/new/data/szse_stock.json"
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=10)
        data = r.json()
        for stock in data.get("stockList", []):
            if stock.get("code") == code:
                return stock.get("orgId", "")
    except Exception:
        pass
    # fallback: 硬编码模式
    if code.startswith("6"):
        return f"gssh0{code}"
    return f"gssz0{code}"


# ══════════════════════════════════════════════════════════════
# 行情层补充：五档盘口 + PE/PB/市值 + 指数/ETF
# 数据源：腾讯财经（HTTP，不封IP）+ mootdx（TCP）
# ══════════════════════════════════════════════════════════════

def _tencent_quote_raw(codes: list) -> dict:
    """腾讯财经批量行情原始接口（内部共用）。返回 {code: {全部字段dict}}。"""
    import urllib.request

    prefixed = []
    for c in codes:
        c = _stock_code_normalize(c)
        # 指数: 000xxx(上证指数系列) → sh, 399xxx(深证指数系列) → sz
        # ETF:  510xxx/515xxx/513xxx → sh, 159xxx → sz
        # 股票: 60xxxx → sh, 00xxxx/30xxxx → sz, 8xxxxx → bj
        if c.startswith(("6", "9", "5", "000")):
            # 000开头: 指数(000001/000300)→sh, 股票(000858)→sz
            # 通过长度和范围区分: 000xxx指数(sh) vs 000xxx股票(sz)
            if c.startswith("000") and not c.startswith(("002", "003")):
                prefixed.append(f"sh{c}")  # 000xxx 指数 → 上海
            elif c.startswith(("6", "9", "5")):
                prefixed.append(f"sh{c}")
            else:
                prefixed.append(f"sz{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name":           vals[1],
            "price":          float(vals[3]) if vals[3] else 0,
            "last_close":     float(vals[4]) if vals[4] else 0,
            "open":           float(vals[5]) if vals[5] else 0,
            "buy_vol":        float(vals[6]) if vals[6] else 0,
            "sell_vol":       float(vals[7]) if vals[7] else 0,
            # 买一~买五（价+量）
            "bid1_price":     float(vals[9]) if vals[9] else 0,
            "bid1_vol":       float(vals[10]) if vals[10] else 0,
            "bid2_price":     float(vals[11]) if vals[11] else 0,
            "bid2_vol":       float(vals[12]) if vals[12] else 0,
            "bid3_price":     float(vals[13]) if vals[13] else 0,
            "bid3_vol":       float(vals[14]) if vals[14] else 0,
            "bid4_price":     float(vals[15]) if vals[15] else 0,
            "bid4_vol":       float(vals[16]) if vals[16] else 0,
            "bid5_price":     float(vals[17]) if vals[17] else 0,
            "bid5_vol":       float(vals[18]) if vals[18] else 0,
            # 卖一~卖五（价+量）
            "ask1_price":     float(vals[19]) if vals[19] else 0,
            "ask1_vol":       float(vals[20]) if vals[20] else 0,
            "ask2_price":     float(vals[21]) if vals[21] else 0,
            "ask2_vol":       float(vals[22]) if vals[22] else 0,
            "ask3_price":     float(vals[23]) if vals[23] else 0,
            "ask3_vol":       float(vals[24]) if vals[24] else 0,
            "ask4_price":     float(vals[25]) if vals[25] else 0,
            "ask4_vol":       float(vals[26]) if vals[26] else 0,
            "ask5_price":     float(vals[27]) if vals[27] else 0,
            "ask5_vol":       float(vals[28]) if vals[28] else 0,
            # 其他字段
            "change_amt":     float(vals[31]) if vals[31] else 0,
            "change_pct":     float(vals[32]) if vals[32] else 0,
            "high":           float(vals[33]) if vals[33] else 0,
            "low":            float(vals[34]) if vals[34] else 0,
            "amount_wan":     float(vals[37]) if vals[37] else 0,
            "turnover_pct":   float(vals[38]) if vals[38] else 0,
            "pe_ttm":         float(vals[39]) if vals[39] else 0,
            "amplitude_pct":  float(vals[43]) if vals[43] else 0,
            "mcap_yi":        float(vals[44]) if vals[44] else 0,
            "float_mcap_yi":  float(vals[45]) if vals[45] else 0,
            "pb":             float(vals[46]) if vals[46] else 0,
            "limit_up":       float(vals[47]) if vals[47] else 0,
            "limit_down":     float(vals[48]) if vals[48] else 0,
            "vol_ratio":      float(vals[49]) if vals[49] else 0,
            "pe_static":      float(vals[52]) if vals[52] else 0,
        }
    return result


@tool(
    description="[短线] 五档盘口。买一~买五/卖一~卖五价格+挂单量+实时行情。短线盘口语言：买盘挂单大=支撑强，卖盘挂单大=压力大。大单托底可能是诱多，大单压顶可能是洗盘。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_order_book(stock_code: str) -> Dict[str, Any]:
    """获取五档盘口+实时行情。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    try:
        from app.market_cn.tape import get_order_book as _get_order_book
        result = _get_order_book(code)
        if "error" in result:
            return {"stock_code": code, "error": result["error"]}
        return result
    except Exception as e:
        logger.warning("get_order_book(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


@tool(
    description="[中线核心] 估值指标（优选数据源：走腾讯财经，PE(TTM)/PB/市值/涨跌停价比 get_stock_info 更准更快，不封IP）。PE(TTM)/PE(静)/PB/总市值/流通市值/换手率/涨跌停价/量比。中线选股核心：PE<行业均值=低估，PB<1=破净，量比>2=异动。配合一致预期算前向PE。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_valuation_metrics(stock_code: str) -> Dict[str, Any]:
    """获取估值指标（腾讯财经）。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    try:
        data = _tencent_quote_raw([code])
        q = data.get(code)
        if not q:
            return {"stock_code": code, "error": "未获取到数据"}
        return {
            "stock_code": code,
            "name": q["name"],
            "price": q["price"],
            "change_pct": q["change_pct"],
            "pe_ttm": q["pe_ttm"],
            "pe_static": q["pe_static"],
            "pb": q["pb"],
            "mcap_yi": q["mcap_yi"],
            "float_mcap_yi": q["float_mcap_yi"],
            "turnover_pct": q["turnover_pct"],
            "amplitude_pct": q["amplitude_pct"],
            "limit_up": q["limit_up"],
            "limit_down": q["limit_down"],
            "vol_ratio": q["vol_ratio"],
            "amount_wan": q["amount_wan"],
        }
    except Exception as e:
        logger.warning("get_valuation_metrics(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


@tool(
    description="[短线+中线] 指数/ETF行情（扩展 get_market_indices：现有工具仅覆盖3大指数，本工具支持任意指数+ETF代码，如510050/510300/159919等）。上证/深证/沪深300/创业板指+主流ETF实时行情。看大盘方向用：指数涨跌判断市场情绪，ETF跟踪行业/宽基趋势。短线看情绪，中线看趋势。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_index_etf_quote(codes: str) -> Dict[str, Any]:
    """获取指数/ETF实时行情（腾讯财经）。

    Args:
        codes: 逗号分隔的代码，如 "000001,000300,399006,510050"
               指数：000001(上证) 399001(深证) 000300(沪深300) 399006(创业板)
               ETF：510050(上证50) 510300(沪深300) 159919(沪深300) 512880(证券)
    """
    code_list = [_stock_code_normalize(c.strip()) for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供至少一个代码"}

    try:
        data = _tencent_quote_raw(code_list)
        results = []
        for code in code_list:
            q = data.get(code)
            if q:
                results.append({
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "change_amt": q["change_amt"],
                    "high": q["high"],
                    "low": q["low"],
                    "amount_wan": q["amount_wan"],
                    "open": q["open"],
                    "last_close": q["last_close"],
                })
            else:
                results.append({"code": code, "error": "未获取到数据"})
        return {"total": len(results), "quotes": results}
    except Exception as e:
        logger.warning("get_index_etf_quote(%s) failed: %s", codes, e)
        return {"error": str(e)}


@tool(
    description="[中线] 批量估值对比。多只股票PE/PB/市值/涨跌幅横向排列。同行业选股用：PE最低+PB最低=潜在低估标的。最多20只同时对比。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def batch_valuation_compare(stock_codes: str) -> Dict[str, Any]:
    """批量估值对比（腾讯财经）。

    Args:
        stock_codes: 逗号分隔的股票代码，如 "600519,000858,688017"
    """
    code_list = [_stock_code_normalize(c.strip()) for c in stock_codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供至少一个股票代码"}
    if len(code_list) > 20:
        return {"error": "单次最多对比20只股票"}

    try:
        data = _tencent_quote_raw(code_list)
        results = []
        for code in code_list:
            q = data.get(code)
            if q:
                results.append({
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "pe_ttm": q["pe_ttm"],
                    "pb": q["pb"],
                    "mcap_yi": q["mcap_yi"],
                    "float_mcap_yi": q["float_mcap_yi"],
                    "turnover_pct": q["turnover_pct"],
                })
            else:
                results.append({"code": code, "error": "未获取到数据"})

        # 按 PE(TTM) 排序
        valid = [r for r in results if r.get("pe_ttm") and r["pe_ttm"] > 0]
        valid.sort(key=lambda x: x["pe_ttm"])

        return {
            "total": len(results),
            "stocks": results,
            "pe_sorted": [r["code"] for r in valid],
        }
    except Exception as e:
        logger.warning("batch_valuation_compare(%s) failed: %s", stock_codes, e)
        return {"error": str(e)}
