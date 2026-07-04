# -*- coding: utf-8 -*-
"""
Dragon Tools — 龙虎榜/涨跌停/热榜。

数据源：market_cn.dragon_limit（HTTP优先 + AkShare兜底，已有多源容灾）
"""
from __future__ import annotations
from app.agent.utils.md_format import _to_md
import json

from app.agent.log import logger
from typing import Any, Dict, List
def get_dragon_tiger(codes: str = "", date: str = "", days: int = 30, output: str = "markdown") -> str:
    """龙虎榜：返回上榜股票的买卖金额、上榜原因（涨幅/跌幅/换手异常等）、日期。

    codes 为空时返回全市场龙虎榜；非空时返回该股票的历史龙虎榜记录。

    Args:
        codes: 逗号分隔的股票代码（可选，空=全市场），如 "600519" 或 "600519,000001"
        date: 查询日期 YYYY-MM-DD，默认最近交易日
        days: 回溯天数，默认30
        output: "markdown"(默认) | "json"
    """
    def _one(stock_code: str, output: str = "markdown") -> str:
        from datetime import datetime, timedelta
        from app.market_cn.dragon_limit import get_dragon_tiger as _get

        if stock_code and stock_code.strip():
            if not date:
                _date = datetime.now().strftime("%Y-%m-%d")
            else:
                _date = date
            start_date = (datetime.now() - timedelta(days=max(1, min(days, 365)))).strftime("%Y-%m-%d")
            all_data = _get(start_date, _date)
            code = stock_code.strip().replace(".", "").upper()
            data = [r for r in all_data if str(r.get("stock_code", "")).replace(".", "").upper() == code
                    or stock_code.strip() in str(r.get("code", ""))]
            _r = {"stock_code": stock_code, "days": days, "count": len(data), "records": data}
            from app.agent.utils.md_format import _to_md
            return json.dumps(_r, ensure_ascii=False) if output == "json" else _to_md(_r)
        else:
            if not date:
                _date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                _date = date
            data = _get(_date, _date)
            return {"date": _date, "days": 1, "count": len(data), "stocks": data}

    # codes 为空时走全市场逻辑（不经过批量）
    if not codes or not codes.strip():
        return _one("")

    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
def get_hot_rank(top_n: int = 30, output: str = "markdown") -> str:
    """人气榜：返回当日市场关注度最高的股票排名及热度分数。

    Args:
        top_n: 返回前N名，默认30，最大100
        output: "markdown"(默认) | "json"
    """
    from app.market_cn.dragon_limit import get_hot_rank as _get
    top_n = min(max(int(top_n or 30), 1), 100)
    data = _get()
    _r = {"count": len(data[:top_n]), "stocks": data[:top_n]}
    from app.agent.utils.md_format import _to_md
    return json.dumps(_r, ensure_ascii=False) if output == "json" else _to_md(_r)
def get_limit_pool(date: str = "", pool_type: str = "zt", min_continuous_days: int = 0, output: str = "markdown") -> str:
    """涨跌停池：返回当日涨停/跌停/炸板股票列表，含封板时间、连板天数。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
        pool_type: zt=涨停池, dt=跌停池, broken=炸板池, all=全部
        min_continuous_days: 仅 zt 有效，最少连板天数，0=全部
        output: "markdown"(默认) | "json"
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

    from app.agent.utils.md_format import _to_md
    return json.dumps(result, ensure_ascii=False) if output == "json" else _to_md(result)
