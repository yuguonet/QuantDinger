# -*- coding: utf-8 -*-
"""
Fund Flow Tools — 资金流向（个股/板块/大盘）。

数据源优先级：腾讯 > 新浪 > 同花顺 > 东财
（market_cn.tape / market_cn.index 已内置多源容灾）
"""
from __future__ import annotations
import json

from app.agent.log import logger
from typing import Any, Dict, List
from app.agent.utils.md_format import _batch_execute, _to_md
def get_fund_flow(codes: str = "") -> dict:
    """个股资金流向：返回主力/散户/净流入金额、资金流向趋势。

    Args:
        codes: 股票代码，如 "000001" 或 "000001,600519"
    """
    if not codes or not codes.strip():
        return {"error": "codes 不能为空", "retriable": False}

    codes = [c.strip() for c in codes.split(",") if c.strip()][:20]
    from app.market_cn.tape import get_fund_flow_realtime

    results = {}
    for code in codes:
        try:
            results[code] = get_fund_flow_realtime(code)
        except Exception as e:
            results[code] = {"error": str(e)}

    return {"count": len(results), "data": results}
def get_sector_fund_flow(indicator: str = "今日") -> dict:
    """行业资金流向：返回各行业板块主力资金净流入排名。

    Args:
        indicator: 时间维度，可选 "今日" "5日" "10日"
    """
    from app.market_cn.index import get_sector_fund_flow as _get
    try:
        data = _get(indicator=indicator)
        return {"indicator": indicator, "count": len(data), "sectors": data}
    except Exception as e:
        logger.warning("get_sector_fund_flow failed: %s", e)
        return {"error": str(e)}
def get_concept_fund_flow(indicator: str = "今日") -> dict:
    """概念资金流向：返回各概念板块主力资金净流入排名。

    Args:
        indicator: 时间维度，可选 "今日" "5日" "10日"
    """
    from app.market_cn.index import get_sector_fund_flow as _get
    try:
        data = _get(indicator=indicator, board_type="concept")
        return {"indicator": indicator, "count": len(data), "concepts": data}
    except Exception as e:
        logger.warning("get_concept_fund_flow failed: %s", e)
        return {"error": str(e)}
def get_fund_flow_daily(codes: str, days: int = 120) -> dict:
    """个股历史资金流向：返回近N天每日主力/散户净流入金额。

    Args:
        codes: 多股用逗号分隔
        days: 回溯天数，默认120
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        from app.market_cn.tape import get_fund_flow_daily as _get
        try:
            return _get(stock_code, days=days)
        except Exception as e:
            logger.warning("get_fund_flow_daily(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    return _batch_execute(_one, code_list)
def get_market_fund_flow() -> dict:
    """大盘资金流向：返回全市场主力/散户实时净流入金额。"""
    from app.market_cn.index import get_market_fund_flow_realtime as _get
    try:
        return _get()
    except Exception as e:
        logger.warning("get_market_fund_flow failed: %s", e)
        return {"error": str(e)}
# 2026-09-14 移除 `get_northbound_flow`：上游（同花顺 hexin 实时接口）已不可用，
# 调用恒返回 {"error": ...}。留在工具面只会诱导模型反复试探、白烧步数与 token。
# 底层 `app.market_cn.index.get_northbound_realtime` 仍被 fear_greed_index 与
# cards/overview 依赖，故只摘除 agent 工具层的暴露，不动底层实现。
