# -*- coding: utf-8 -*-
"""
资金筹码工具 — 融资融券、大宗交易、股东户数、分红送转、财报三表。

数据来源：东财 datacenter / market_cn.finance / 新浪财报
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

from app.data_sources.normalizer import safe_float as _safe_float
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _stock_code_normalize(code: str) -> str:
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code


def _market_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


def _em_datacenter(report_name: str, columns: str = "ALL",
                   filter_str: str = "", page_size: int = 50,
                   sort_columns: str = "", sort_types: str = "-1") -> list:
    """东财数据中心统一查询。"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = requests.get(_DATACENTER_URL, params=params,
                     headers={"User-Agent": _UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ══════════════════════════════════════════════════════════════
# 融资融券
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
            "rzye": _safe_float(row.get("RZYE")),
            "rzmre": _safe_float(row.get("RZMRE")),
            "rzche": _safe_float(row.get("RZCHE")),
            "rqye": _safe_float(row.get("RQYE")),
            "rqmcl": _safe_float(row.get("RQMCL")),
            "rqchl": _safe_float(row.get("RQCHL")),
            "rzrqye": _safe_float(row.get("RZRQYE")),
        })
    return {"stock_code": code, "records": rows}


# ══════════════════════════════════════════════════════════════
# 大宗交易
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
# 股东户数
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
# 分红送转
# ══════════════════════════════════════════════════════════════

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
            "bonus_rmb": _safe_float(row.get("PRETAX_BONUS_RMB")),
            "transfer_ratio": _safe_float(row.get("TRANSFER_RATIO")),
            "bonus_ratio": _safe_float(row.get("BONUS_RATIO")),
            "plan": row.get("ASSIGN_PROGRESS", ""),
        })
    return {"stock_code": code, "records": rows}


# ══════════════════════════════════════════════════════════════
# 财报三表
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

            import pandas as pd
            from io import StringIO
            dfs = pd.read_html(StringIO(r.text))
            if not dfs:
                continue

            df = dfs[0]
            records = []
            cols = df.columns.tolist()
            for _, row in df.iterrows():
                item = {"item": str(row.iloc[0]) if len(row) > 0 else ""}
                for col in cols[1:5]:
                    item[str(col)] = row[col] if pd.notna(row[col]) else None
                records.append(item)

            result[key] = records[:30]
        except Exception as e:
            logger.warning("get_financial_statements(%s) %s failed: %s", code, table_name, e)
            result[key] = []

    return result
