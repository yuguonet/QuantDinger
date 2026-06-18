# -*- coding: utf-8 -*-
"""
资金筹码工具 — 中长线基本面综合摘要分析。

内部函数（_前缀）：
  _get_margin_trading  — 融资融券明细
  _get_block_trades    — 大宗交易记录
  _get_holder_count    — 股东户数变化
  _get_dividend_history — 分红送转历史
  _get_financial_statements — 财报三表

对外暴露：
  get_capital_summary  — 中长线基本面综合摘要（一次调用聚合全部数据）
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
# 内部函数（_前缀，不暴露为独立工具）
# ══════════════════════════════════════════════════════════════

def _get_margin_trading(code: str, days: int = 60) -> Dict[str, Any]:
    """融资融券明细。日级融资余额/买入/偿还+融券余额。"""
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
    return {"records": rows}


def _get_block_trades(code: str, page_size: int = 20) -> Dict[str, Any]:
    """大宗交易记录。成交价/量+买卖方营业部+溢价率。"""
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
    return {"records": rows}


def _get_holder_count(code: str) -> Dict[str, Any]:
    """股东户数变化。季度股东数+环比变化+户均持股。"""
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
    return {"records": rows}


def _get_dividend_history(code: str) -> Dict[str, Any]:
    """分红送转历史。每股派息/送股/转增+进度状态。"""
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
    return {"records": rows}


def _get_financial_statements(code: str) -> Dict[str, Any]:
    """财报三表（资产负债表/利润表/现金流量表，最近4个报告期）。"""
    prefix = _market_prefix(code)
    symbol = f"{prefix}{code}"

    result = {"balance_sheet": [], "income_statement": [], "cash_flow": []}
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
            logger.warning("_get_financial_statements(%s) %s failed: %s", code, table_name, e)
            result[key] = []

    return result


# ══════════════════════════════════════════════════════════════
# 对外工具 — 中长线基本面综合摘要
# ══════════════════════════════════════════════════════════════

@tool(
    description=(
        "[中长线核心] 个股基本面综合摘要分析。一次调用聚合五大维度数据：\n"
        "① 融资融券 — 杠杆资金情绪（融资余额趋势、融券异动）\n"
        "② 大宗交易 — 机构态度（溢价/折价率、机构专用席位动向）\n"
        "③ 股东户数 — 筹码集中度（户数变化趋势、户均持股）\n"
        "④ 分红送转 — 股东回报（连续分红、股息率）\n"
        "⑤ 财报三表 — 财务健康度（资产负债率、现金流、成长性）\n"
        "返回结构化摘要+各维度原始数据，适合中长线持仓决策。"
    ),
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_capital_summary(stock_code: str) -> Dict[str, Any]:
    """中长线基本面综合摘要分析。

    一次调用聚合融资融券、大宗交易、股东户数、分红送转、财报三表五大维度数据，
    并生成结构化摘要供中长线持仓决策参考。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)

    # ── 并行采集五维数据 ──────────────────────────────────────
    margin = _get_margin_trading(code, days=60)
    block = _get_block_trades(code, page_size=20)
    holders = _get_holder_count(code)
    dividend = _get_dividend_history(code)
    financials = _get_financial_statements(code)

    # ── 摘要计算 ─────────────────────────────────────────────
    summary: Dict[str, Any] = {"stock_code": code}

    # 1) 融资融券摘要
    margin_records = margin.get("records", [])
    if margin_records:
        latest = margin_records[0]
        rz_trend = [r.get("rzye", 0) for r in margin_records[:10]]
        rz_direction = "上升" if len(rz_trend) >= 2 and rz_trend[0] > rz_trend[-1] else "下降" if len(rz_trend) >= 2 else "持平"
        summary["margin"] = {
            "latest_date": latest.get("date"),
            "financing_balance": latest.get("rzye"),
            "financing_buy": latest.get("rzmre"),
            "short_balance": latest.get("rqye"),
            "financing_trend": rz_direction,
            "signal": "杠杆资金看多" if rz_direction == "上升" else "杠杆资金撤退" if rz_direction == "下降" else "中性",
        }
    else:
        summary["margin"] = {"signal": "无数据"}

    # 2) 大宗交易摘要
    block_records = block.get("records", [])
    if block_records:
        premium_avg = sum(r.get("premium_pct", 0) for r in block_records) / len(block_records)
        inst_buy = sum(1 for r in block_records if "机构" in str(r.get("buyer", "")))
        summary["block_trade"] = {
            "recent_count": len(block_records),
            "avg_premium_pct": round(premium_avg, 2),
            "inst_buy_count": inst_buy,
            "signal": "机构溢价买入（正面）" if premium_avg > 0 and inst_buy > 0
                      else "折价成交（负面）" if premium_avg < -5
                      else "中性",
        }
    else:
        summary["block_trade"] = {"signal": "无大宗交易记录"}

    # 3) 股东户数摘要
    holder_records = holders.get("records", [])
    if holder_records:
        latest_h = holder_records[0]
        prev_h = holder_records[1] if len(holder_records) > 1 else None
        trend = "减少" if prev_h and latest_h.get("holder_num", 0) < prev_h.get("holder_num", 0) \
                else "增加" if prev_h and latest_h.get("holder_num", 0) > prev_h.get("holder_num", 0) \
                else "未知"
        summary["holders"] = {
            "latest_date": latest_h.get("date"),
            "holder_num": latest_h.get("holder_num"),
            "change_ratio": latest_h.get("change_ratio"),
            "avg_shares": latest_h.get("avg_shares"),
            "trend": trend,
            "signal": "主力吸筹（正面）" if trend == "减少" else "散户接盘（负面）" if trend == "增加" else "中性",
        }
    else:
        summary["holders"] = {"signal": "无数据"}

    # 4) 分红送转摘要
    div_records = dividend.get("records", [])
    if div_records:
        total_bonus = sum(r.get("bonus_rmb", 0) or 0 for r in div_records if r.get("bonus_rmb"))
        continuous_years = len(set(str(r.get("date", ""))[:4] for r in div_records if r.get("bonus_rmb", 0) and r.get("bonus_rmb", 0) > 0))
        summary["dividend"] = {
            "record_count": len(div_records),
            "total_bonus_per_share": round(total_bonus, 4),
            "continuous_dividend_years": continuous_years,
            "latest_plan": div_records[0].get("plan", ""),
            "signal": f"连续{continuous_years}年分红（股东友好）" if continuous_years >= 3 else "分红不稳定",
        }
    else:
        summary["dividend"] = {"signal": "无分红记录"}

    # 5) 财报三表摘要
    balance = financials.get("balance_sheet", [])
    income = financials.get("income_statement", [])
    cash = financials.get("cash_flow", [])
    summary["financials"] = {
        "balance_sheet_items": len(balance),
        "income_items": len(income),
        "cash_flow_items": len(cash),
        "signal": "数据完整" if (balance and income and cash) else "部分数据缺失",
    }

    # ── 综合信号 ─────────────────────────────────────────────
    signals = [
        summary.get("margin", {}).get("signal", ""),
        summary.get("block_trade", {}).get("signal", ""),
        summary.get("holders", {}).get("signal", ""),
        summary.get("dividend", {}).get("signal", ""),
    ]
    positive = sum(1 for s in signals if "正面" in s or "看多" in s or "吸筹" in s or "友好" in s)
    negative = sum(1 for s in signals if "负面" in s or "撤退" in s or "接盘" in s)

    if positive > negative:
        summary["overall_signal"] = "中长线偏多"
    elif negative > positive:
        summary["overall_signal"] = "中长线偏空"
    else:
        summary["overall_signal"] = "中性"

    # ── 返回（含原始数据供深度分析）─────────────────────────
    return {
        "summary": summary,
        "raw": {
            "margin_trading": margin,
            "block_trades": block,
            "holders": holders,
            "dividend": dividend,
            "financials": financials,
        },
    }
