# -*- coding: utf-8 -*-
"""
Dragon Tools — 龙虎榜/涨跌停/热榜。

数据源：market_cn.dragon_limit（HTTP优先 + AkShare兜底，已有多源容灾）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def get_dragon_tiger(stock_code: str = "", date: str = "", days: int = 30) -> Dict[str, Any]:
    """获取龙虎榜数据。

    stock_code 为空时返回全市场龙虎榜；非空时返回该股票的历史龙虎榜记录。

    Args:
        stock_code: 股票代码（可选，空=全市场）
        date: 查询日期 YYYY-MM-DD，默认最近交易日
        days: 回溯天数，默认30
    """
    from datetime import datetime, timedelta
    from app.market_cn.dragon_limit import get_dragon_tiger as _get

    if stock_code and stock_code.strip():
        # 个股历史
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=max(1, min(days, 365)))).strftime("%Y-%m-%d")
        all_data = _get(start_date, date)
        code = stock_code.strip().replace(".", "").upper()
        data = [r for r in all_data if str(r.get("stock_code", "")).replace(".", "").upper() == code
                or stock_code.strip() in str(r.get("code", ""))]
        return {"stock_code": stock_code, "days": days, "count": len(data), "records": data}
    else:
        # 全市场
        if not date:
            date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        data = _get(date, date)
        return {"date": date, "days": 1, "count": len(data), "stocks": data}


def get_hot_rank(top_n: int = 30) -> Dict[str, Any]:
    """获取实时股票热榜/人气榜。

    Args:
        top_n: 返回前N名，默认30，最大100
    """
    from app.market_cn.dragon_limit import get_hot_rank as _get
    top_n = min(max(int(top_n or 30), 1), 100)
    data = _get()
    return {"count": len(data[:top_n]), "stocks": data[:top_n]}


def get_limit_pool(date: str = "", pool_type: str = "zt", min_continuous_days: int = 0) -> Dict[str, Any]:
    """获取涨跌停/炸板股票池。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
        pool_type: zt=涨停池, dt=跌停池, broken=炸板池, all=全部
        min_continuous_days: 仅 zt 有效，最少连板天数，0=全部
    """
    from datetime import datetime
    from app.market_cn.dragon_limit import (
        get_zt_pool as _zt, get_dt_pool as _dt, get_broken_board as _broken,
    )

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    pool_type = (pool_type or "zt").strip().lower()
    result: Dict[str, Any] = {"date": date}

    if pool_type in ("zt", "all"):
        zt = _zt(date)
        if min_continuous_days > 0:
            zt = [r for r in zt if int(r.get("continuous_zt_days", 0) or 0) >= min_continuous_days]
        result["zt"] = {"count": len(zt), "stocks": zt}

    if pool_type in ("dt", "all"):
        dt = _dt(date)
        result["dt"] = {"count": len(dt), "stocks": dt}

    if pool_type in ("broken", "all"):
        broken = _broken(date)
        result["broken"] = {"count": len(broken), "stocks": broken}

    return result
