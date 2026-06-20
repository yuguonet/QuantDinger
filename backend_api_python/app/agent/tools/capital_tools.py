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

from app.agent.tools.em_utils import em_datacenter
from app.data_sources.normalizer import strip_market_prefix as _strip_prefix

import logging
from typing import Any, Dict, List

import requests

from app.data_sources.normalizer import safe_float as _safe_float

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"




def _market_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"



def _get_margin_trading(code: str, days: int = 60) -> Dict[str, Any]:
    """融资融券明细。日级融资余额/买入/偿还+融券余额。"""
    data = em_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=days,
        sort_columns="DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
        })
    return {"records": rows}


def _get_block_trades(code: str, page_size: int = 20) -> Dict[str, Any]:
    """大宗交易记录。成交价/量+买卖方营业部+溢价率。"""
    data = em_datacenter(
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
        })
    return {"records": rows}


def _get_holder_count(code: str) -> Dict[str, Any]:
    """股东户数变化。季度股东数+环比变化+户均持股。"""
    data = em_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=10,
        sort_columns="END_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("END_DATE", ""))[:10],
        })
    return {"records": rows}


def _get_dividend_history(code: str) -> Dict[str, Any]:
    """分红送转历史。每股派息/送股/转增+进度状态。"""
    data = em_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=20,
        sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("EX_DIVIDEND_DATE", ""))[:10],
        })
    return {"records": rows}


def _get_financial_statements(code: str) -> Dict[str, Any]:
    """财报三表（资产负债表/利润表/现金流量表，最近4个报告期）。"""
    prefix = _market_prefix(code)
    symbol = f"{prefix}{code}"

    result = {"balance_sheet": [], "income_statement": [], "cash_flow": []}
    table_map = {
        "balance_sheet": ("zcfzb", "资产负债表"),
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

def get_capital_summary(codes: str) -> Dict[str, Any]:
    """中长线基本面综合摘要分析，支持多股批量获取。

    一次调用聚合融资融券、大宗交易、股东户数、分红送转、财报三表五大维度数据，
    并生成结构化摘要供中长线持仓决策参考。

    Args:
        codes: 逗号分隔的股票代码，如 "600519" 或 "600519,000001"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        code = _strip_prefix(stock_code)

        # ── 并行采集五维数据（单源超时不阻断整体）────────────────────
        def _safe(fn, label):
            try:
                return fn()
            except Exception as e:
                logger.warning("[Capital] %s 超时/失败: %s", label, e)
                return {}

        margin = _safe(lambda: _get_margin_trading(code, days=60), "margin")
        block = _safe(lambda: _get_block_trades(code, page_size=20), "block")
        holders = _safe(lambda: _get_holder_count(code), "holders")
        dividend = _safe(lambda: _get_dividend_history(code), "dividend")
        financials = _safe(lambda: _get_financial_statements(code), "financials")

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
            }
        else:
            summary["margin"] = {"signal": "无数据"}

        # 2) 大宗交易摘要
        block_records = block.get("records", [])
        if block_records:
            premium_avg = sum(r.get("premium_pct", 0) for r in block_records) / len(block_records)
            inst_buy = sum(1 for r in block_records if "机构" in str(r.get("buyer", "")))
            signal = "溢价成交（正面）" if premium_avg > 5 else "折价成交（负面）" if premium_avg < -5 else "中性"
            summary["block_trade"] = {
                "recent_count": len(block_records),
                "signal": signal,
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
            }
        else:
            summary["dividend"] = {"signal": "无分红记录"}

        # 5) 财报三表摘要
        balance = financials.get("balance_sheet", [])
        income = financials.get("income_statement", [])
        cash = financials.get("cash_flow", [])
        summary["financials"] = {
            "balance_sheet_items": len(balance),
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
        return {"summary": summary}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
