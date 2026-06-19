# -*- coding: utf-8 -*-
"""
Index Tools — 指数行情/市场概览/情绪。

数据源优先级：腾讯 > 新浪 > 同花顺 > AKshare > 东财
（market_cn.index / market_cn.fear_greed_index 已内置多源容灾）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def get_market_indices() -> Dict[str, Any]:
    """获取主要指数实时行情（上证/深证/创业板/科创/北证）。"""
    from app.market_cn.index import get_index_realtime as _get
    try:
        data = _get()
        return {"count": len(data), "indices": data}
    except Exception as e:
        logger.warning("get_market_indices failed: %s", e)
        return {"error": str(e)}


def get_market_overview() -> Dict[str, Any]:
    """获取全市场涨跌统计快照：涨跌家数、北向资金、情绪指数、主力资金流。"""
    result = {}

    # 指数行情 → 涨跌家数
    try:
        from app.market_cn.index import get_index_realtime
        indices = get_index_realtime(["000001", "399001"])
        up = sum(1 for i in (indices or []) if i.get("change_percent", 0) > 0)
        down = sum(1 for i in (indices or []) if i.get("change_percent", 0) < 0)
        result["up_count"] = up
        result["down_count"] = down
    except Exception:
        pass

    # 北向资金
    try:
        from app.market_cn.index import get_northbound_realtime
        nb = get_northbound_realtime()
        result["north_net_flow"] = round(nb.get("total_latest_yi", 0), 2)
    except Exception:
        pass

    # 情绪指数
    try:
        from app.market_cn.fear_greed_index import fear_greed_index
        fg = fear_greed_index()
        result["emotion"] = int(fg.get("composite_score", 50))
    except Exception:
        pass

    # 主力资金流
    try:
        from app.market_cn.index import get_market_fund_flow_realtime
        mf = get_market_fund_flow_realtime()
        result["main_net_yi"] = round(mf.get("main_net", 0) / 1e8, 2)
        result["main_pct"] = round(mf.get("main_pct", 0), 2)
    except Exception:
        pass

    return result
