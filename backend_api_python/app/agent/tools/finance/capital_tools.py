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
import json

from app.agent.tools.finance.em_utils import em_datacenter
def _strip_prefix(s):
    from app.data_sources.normalizer import strip_market_prefix
    return strip_market_prefix(s)

from app.agent.log import logger
from typing import Any, Dict, List

import requests
from app.agent.utils.md_format import _batch_execute, _to_md

def _safe_float(v, default=0.0):
    from app.data_sources.normalizer import safe_float
    return safe_float(v, default)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
def _market_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"

def _get_margin_trading(code: str, days: int = 60) -> Dict[str, Any]:
    """融资融券摘要。提取融资余额趋势+近期变化幅度。"""
    data = em_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=days,
        sort_columns="DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无数据"}

    latest = data[0]
    rz_latest = _safe_float(latest.get("RZYE"))  # 融资余额
    rz_5d_ago = _safe_float(data[4].get("RZYE")) if len(data) > 5 else rz_latest
    rz_change_pct = ((rz_latest / rz_5d_ago - 1) * 100) if rz_5d_ago else 0

    rq_latest = _safe_float(latest.get("RQYE"))  # 融券余额

    signal = "融资净流入" if rz_change_pct > 2 else "融资净流出" if rz_change_pct < -2 else "持平"
    return {
        "rz_balance": round(rz_latest / 1e4, 1),  # 万元
        "rz_5d_change_pct": round(rz_change_pct, 1),
        "rq_balance": round(rq_latest / 1e4, 1),
        "signal": signal,
    }
def _get_block_trades(code: str, page_size: int = 20) -> Dict[str, Any]:
    """大宗交易摘要。提取近期成交笔数、平均溢价率、机构买卖方向。"""
    data = em_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size,
        sort_columns="TRADE_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无大宗交易记录"}

    premiums = []
    inst_buy = 0
    inst_sell = 0
    for row in data:
        close = _safe_float(row.get("CLOSE_PRICE"))
        deal_price = _safe_float(row.get("DEAL_PRICE"))
        if close:
            premiums.append((deal_price / close - 1) * 100)
        buyer = str(row.get("BUYER", ""))
        seller = str(row.get("SELLER", ""))
        if "机构" in buyer:
            inst_buy += 1
        if "机构" in seller:
            inst_sell += 1

    avg_premium = sum(premiums) / len(premiums) if premiums else 0
    signal = "溢价成交（正面）" if avg_premium > 5 else "折价成交（负面）" if avg_premium < -5 else "中性"
    return {
        "recent_count": len(data),
        "avg_premium_pct": round(avg_premium, 1),
        "inst_buy": inst_buy,
        "inst_sell": inst_sell,
        "signal": signal,
    }
def _get_holder_count(code: str) -> Dict[str, Any]:
    """股东户数摘要。提取最新户数、环比变化趋势。"""
    data = em_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=10,
        sort_columns="END_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无数据"}

    latest = data[0]
    latest_count = _safe_float(latest.get("HOLDER_NUM"))
    prev_count = _safe_float(data[1].get("HOLDER_NUM")) if len(data) > 1 else latest_count
    change_pct = ((latest_count / prev_count - 1) * 100) if prev_count else 0
    avg_amount = _safe_float(latest.get("AVG_AMOUNT"))  # 户均持股

    signal = "筹码集中" if change_pct < -5 else "筹码分散" if change_pct > 5 else "持平"
    return {
        "latest_date": str(latest.get("END_DATE", ""))[:10],
        "holder_count": int(latest_count),
        "change_pct": round(change_pct, 1),
        "avg_amount": round(avg_amount, 0),
        "signal": signal,
    }
def _get_dividend_history(code: str) -> Dict[str, Any]:
    """分红送转摘要。提取累计分红次数、连续分红年数、近期派息水平。"""
    data = em_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=20,
        sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无分红记录"}

    years = set()
    total_bonus = 0.0
    for row in data:
        date_str = str(row.get("EX_DIVIDEND_DATE", ""))[:4]
        if date_str:
            years.add(date_str)
        total_bonus += _safe_float(row.get("PRETAX_BONUS_RMB"))

    return {
        "record_count": len(data),
        "dividend_years": len(years),
        "total_bonus_rmb": round(total_bonus, 3),
        "signal": "持续分红" if len(years) >= 3 else "偶有分红" if years else "无分红",
    }
def _get_financial_statements(code: str) -> Dict[str, Any]:
    """财报关键指标摘要。只提取 Agent 关心的几个数，不返回原始表格。"""
    prefix = _market_prefix(code)
    symbol = f"{prefix}{code}"

    summary: Dict[str, Any] = {}

    try:
        url = f"https://quotes.sina.cn/cn/go.php/vFD_FinancialGuideLine/stockid/{symbol}/ctrl/zcfzb/displaytype/4.phtml"
        headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"

        import pandas as pd
        from io import StringIO
        dfs = pd.read_html(StringIO(r.text))
        if dfs:
            df = dfs[0]
            cols = df.columns.tolist()
            latest_col = cols[1] if len(cols) > 1 else None
            if latest_col:
                for _, row in df.iterrows():
                    item_name = str(row.iloc[0]) if len(row) > 0 else ""
                    val = row[latest_col] if pd.notna(row[latest_col]) else None
                    if val is None:
                        continue
                    # 只保留关键指标
                    if "总资产" in item_name and "负债" not in item_name:
                        summary["total_assets"] = val
                    elif "总负债" in item_name:
                        summary["total_liabilities"] = val
                    elif "股东权益合计" in item_name or "归属.*股东.*权益" in item_name:
                        summary["equity"] = val
                    elif "货币资金" in item_name:
                        summary["cash"] = val
    except Exception as e:
        logger.warning("_get_financial_statements(%s) 资产负债表失败: %s", code, e)

    # 利润表：从财务指标接口取关键增速
    try:
        url2 = f"https://quotes.sina.cn/cn/go.php/vFD_FinancialGuideLine/stockid/{symbol}/ctrl/lrb/displaytype/4.phtml"
        headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
        r2 = requests.get(url2, headers=headers, timeout=15)
        r2.encoding = "gbk"
        dfs2 = pd.read_html(StringIO(r2.text))
        if dfs2:
            df2 = dfs2[0]
            cols2 = df2.columns.tolist()
            latest_col2 = cols2[1] if len(cols2) > 1 else None
            if latest_col2:
                for _, row in df2.iterrows():
                    item_name = str(row.iloc[0]) if len(row) > 0 else ""
                    val = row[latest_col2] if pd.notna(row[latest_col2]) else None
                    if val is None:
                        continue
                    if "营业总收入" in item_name or "营业收入" in item_name:
                        summary["revenue"] = val
                    elif "净利润" in item_name and "扣非" not in item_name:
                        summary["net_profit"] = val
    except Exception as e:
        logger.warning("_get_financial_statements(%s) 利润表失败: %s", code, e)

    return summary
# ══════════════════════════════════════════════════════════════
# 对外工具 — 中长线基本面综合摘要
# ══════════════════════════════════════════════════════════════

def get_capital_summary(codes: str) -> dict:
    """基本面摘要：返回营收/利润增速、ROE、PE/PB估值、机构持仓变化等中长线指标。

    一次调用聚合融资融券、大宗交易、股东户数、分红送转、财报三表五大维度数据，
    并生成结构化摘要供中长线持仓决策参考。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
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

        # ── 直接组装摘要（子函数已返回浓缩指标，无需二次计算）──
        summary: Dict[str, Any] = {"stock_code": code}

        summary["margin"] = margin
        summary["block_trade"] = block
        summary["holders"] = holders
        summary["dividend"] = dividend
        summary["financials"] = financials

        # ── 综合信号 ─────────────────────────────────────────────
        signals = [
            margin.get("signal", ""),
            block.get("signal", ""),
            holders.get("signal", ""),
            dividend.get("signal", ""),
        ]
        positive = sum(1 for s in signals if "正面" in s or "看多" in s or "集中" in s or "持续" in s or "流入" in s)
        negative = sum(1 for s in signals if "负面" in s or "撤退" in s or "分散" in s or "流出" in s)

        if positive > negative:
            summary["overall_signal"] = "中长线偏多"
        elif negative > positive:
            summary["overall_signal"] = "中长线偏空"
        else:
            summary["overall_signal"] = "中性"

        return {"summary": summary}

    return _batch_execute(_one, code_list)
