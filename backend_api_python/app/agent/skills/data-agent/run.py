# -*- coding: utf-8 -*-
"""
data_agent — 数据工程（通用数据获取）。

评分规则定义在 SKILL.md，本文件是实现。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def run(stock_code: str, stock_name: str = "", context: dict = None) -> Dict[str, Any]:
    """执行数据获取。"""
    from app.agent.tools.data_tools import agent_get_kline

    data = {}
    try:
        data["kline"] = agent_get_kline(stock_code, timeframe="1D", days=60)
    except Exception as e:
        data["kline"] = {"error": str(e)}

    return {
        "skill": "data_agent", "score": 50, "direction": "neutral",
        "confidence": 0.5, "signal": "数据已获取", "factors": [],
        "analysis": "K线数据已获取", "status": "ok", "output_data": data,
    }
