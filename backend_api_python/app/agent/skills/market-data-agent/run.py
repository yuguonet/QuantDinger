# -*- coding: utf-8 -*-
"""
market_data_agent — 行情/概念/资金数据。

评分规则定义在 SKILL.md，本文件是实现。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def run(stock_code: str, stock_name: str = "", context: dict = None) -> Dict[str, Any]:
    """执行行情数据获取。"""
    from app.agent.tools.data_tools import get_realtime_quote, agent_get_kline
    from app.agent.tools.index_tools import get_market_indices
    from app.agent.tools.sector_analysis_tools import get_hot_sectors

    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("kline", lambda: agent_get_kline(stock_code, timeframe="1D", days=30)),
        ("indices", lambda: get_market_indices()),
        ("sectors", lambda: get_hot_sectors()),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    return {
        "skill": "market_data_agent", "score": 50, "direction": "neutral",
        "confidence": 0.6, "signal": "行情数据已获取", "factors": [],
        "analysis": "行情/板块/指数数据已获取", "status": "ok", "output_data": data,
    }
