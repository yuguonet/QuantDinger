# -*- coding: utf-8 -*-
"""
Market Data Tools — 龙虎榜、热榜、涨跌停池、资金流向。

从 stock_screener_tools.py 拆分而来，专注于市场数据查询工具。
数据源：东财搜索 (eastmoney_search) + dragon_limit (HTTP+AkShare)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  通用辅助
# ══════════════════════════════════════════════════════════════

def _validate_date(date_str: str, param_name: str = "date") -> str:
    """验证日期格式，支持 'today'/'yesterday' 快捷写法。"""
    if not date_str:
        return date_str
    from datetime import datetime, timedelta

    # 支持快捷写法
    lower = date_str.strip().lower()
    if lower in ("today", "今天", "今日"):
        return datetime.now().strftime("%Y-%m-%d")
    if lower in ("yesterday", "昨天", "昨日"):
        d = datetime.now() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise ValueError(f"参数 {param_name} 格式错误: '{date_str}'，应为 YYYY-MM-DD")

def _today_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")

def _yesterday_str() -> str:
    from datetime import datetime, timedelta
    d = datetime.now() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def _em_search(keyword: str, page_size: int = 100) -> List[Dict[str, Any]]:
    """东财搜索封装，返回股票列表或空列表。"""
    from app.market_cn.eastmoney_search import search_stocks
    try:
        raw = search_stocks(keyword=keyword, page_size=page_size)
        return raw.get("stocks", []) if raw.get("code") == 1 else []
    except Exception as e:
        logger.warning("[东财搜索] '%s' 失败: %s", keyword, e)
        return []

# ── 统一数据源: dragon_limit (HTTP 优先, AkShare 兜底) ──

def _dl_dragon_tiger(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    from app.market_cn.dragon_limit import get_dragon_tiger
    return get_dragon_tiger(start_date, end_date)

def _dl_zt_pool(trade_date: str) -> List[Dict[str, Any]]:
    from app.market_cn.dragon_limit import get_zt_pool
    return get_zt_pool(trade_date)

def _dl_dt_pool(trade_date: str) -> List[Dict[str, Any]]:
    from app.market_cn.dragon_limit import get_dt_pool
    return get_dt_pool(trade_date)

def _dl_broken_board(trade_date: str) -> List[Dict[str, Any]]:
    from app.market_cn.dragon_limit import get_broken_board
    return get_broken_board(trade_date)

def _dl_hot_rank() -> List[Dict[str, Any]]:
    from app.market_cn.dragon_limit import get_hot_rank
    return get_hot_rank()

# ══════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════

@tool(
    description="获取龙虎榜数据。stock_code为空返回全市场龙虎榜，非空返回该股票的历史龙虎榜。包含上榜股票代码、名称、买卖金额、净买入额、涨跌幅、上榜原因。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_dragon_tiger(stock_code: str = "", date: str = "", days: int = 30) -> Dict[str, Any]:
    """获取龙虎榜数据。

    stock_code 为空时返回全市场龙虎榜；非空时返回该股票的历史龙虎榜记录。

    Args:
        stock_code: 股票代码（可选，空=全市场）
        date: 查询日期 YYYY-MM-DD，默认最近交易日
        days: 回溯天数，默认30
    """
    from datetime import datetime, timedelta

    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if stock_code and stock_code.strip():
        # 个股龙虎榜历史
        if not date:
            date = _today_str()
        days = max(1, min(days, 365))
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        all_data = _dl_dragon_tiger(start_date, date)
        code = stock_code.strip().replace(".", "").upper()
        data = [r for r in all_data if str(r.get("stock_code", "")).replace(".", "").upper() == code
                or stock_code.strip() in str(r.get("code", ""))]

        return {"stock_code": stock_code, "days": days, "count": len(data), "records": data}
    else:
        # 全市场龙虎榜
        if not date:
            date = _yesterday_str()
        days = max(1, min(days, 30))
        start_date = date
        if days > 1:
            start_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")

        data = _dl_dragon_tiger(start_date, date)
        return {"date": date, "days": days, "count": len(data), "stocks": data}

@tool(
    description="获取实时股票热榜/人气榜：排名、代码、名称、人气分数、价格、涨跌幅。反映市场关注度最高的个股。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_hot_rank(top_n: int = 30) -> Dict[str, Any]:
    """获取实时股票热榜/人气榜。

    Args:
        top_n: 返回前N名，默认30，最大100
    """
    top_n = min(max(int(top_n or 30), 1), 100)
    data = _dl_hot_rank()
    return {"count": len(data[:top_n]), "stocks": data[:top_n]}

@tool(
    description="获取涨跌停/炸板股票池。pool_type: zt=涨停池（可筛连板）、dt=跌停池、broken=炸板池（曾封涨停被打开，资金分歧信号）。一次调用可同时获取三类数据。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_limit_pool(date: str = "", pool_type: str = "zt", min_continuous_days: int = 0) -> Dict[str, Any]:
    """获取涨跌停/炸板股票池。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
        pool_type: zt=涨停池, dt=跌停池, broken=炸板池, all=全部
        min_continuous_days: 仅 zt 有效，最少连板天数，0=全部
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}
    if not date:
        date = _today_str()

    pool_type = (pool_type or "zt").strip().lower()
    valid_types = {"zt", "dt", "broken", "all"}
    if pool_type not in valid_types:
        return {"error": f"pool_type 必须是 {'/'.join(valid_types)}，收到: {pool_type}", "retriable": False}

    result: Dict[str, Any] = {"date": date}

    # 涨停池
    if pool_type in ("zt", "all"):
        min_days = max(0, int(min_continuous_days or 0))
        zt = _dl_zt_pool(date)
        if min_days > 0:
            zt = [r for r in zt if int(r.get("continuous_zt_days", 0) or 0) >= min_days]
        lines = [f"涨停池 {date}  共{len(zt)}只"]
        if min_days > 0:
            lines[0] += f"  (连板≥{min_days})"
        lines.append("")
        lines.append(f"{'代码':<8} {'名称':<8} {'连板':>4} {'涨停时间':<10} {'封板资金(万)':>12} {'换手率%':>7} {'涨停原因'}")
        lines.append("-" * 80)
        for s in zt:
            code = s.get("stock_code", "")
            name = s.get("stock_name", "")
            days = s.get("continuous_zt_days", 1) or 1
            zt_t = s.get("zt_time", "") or ""
            seal = s.get("seal_amount", 0) or 0
            seal_wan = f"{seal / 10000:.1f}" if seal else "-"
            turnover = s.get("turnover_rate", 0) or 0
            to_str = f"{turnover:.1f}" if turnover else "-"
            reason = s.get("reason", "") or ""
            lines.append(f"{code:<8} {name:<8} {days:>4} {zt_t:<10} {seal_wan:>12} {to_str:>7} {reason}")
        result["zt"] = {"count": len(zt), "stocks": zt, "text": "\n".join(lines)}

    # 跌停池
    if pool_type in ("dt", "all"):
        dt = _dl_dt_pool(date)
        result["dt"] = {"count": len(dt), "stocks": dt}

    # 炸板池
    if pool_type in ("broken", "all"):
        broken = _dl_broken_board(date)
        result["broken"] = {"count": len(broken), "stocks": broken}

    return result

@tool(
    description="获取全市场涨跌统计快照：上涨/下跌家数、情绪指标。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_market_overview() -> Dict[str, Any]:
    """获取全市场涨跌统计快照：上涨/下跌家数、资金流向、情绪指标。"""
    up = down = 0
    north_net = 0.0
    emotion = 50
    main_net_yi = 0.0
    main_pct = 0.0

    # 涨跌家数: 从指数实时行情获取
    try:
        from app.market_cn.index import get_index_realtime
        indices = get_index_realtime(["000001", "399001"])
        for item in (indices or []):
            if item.get("change_percent", 0) > 0:
                up += 1
            elif item.get("change_percent", 0) < 0:
                down += 1
    except Exception:
        pass

    # 北向资金
    try:
        from app.market_cn.index import get_northbound_realtime
        nb = get_northbound_realtime()
        north_net = round(nb.get("total_latest_yi", 0), 2)
    except Exception:
        pass

    # 情绪指数
    try:
        from app.market_cn.fear_greed_index import fear_greed_index
        fg = fear_greed_index()
        emotion = int(fg.get("composite_score", 50))
    except Exception:
        pass

    # 大盘资金流向
    try:
        from app.market_cn.index import get_market_fund_flow_realtime
        mf = get_market_fund_flow_realtime()
        main_net_yi = round(mf.get("main_net", 0) / 1e8, 2)
        main_pct = round(mf.get("main_pct", 0), 2)
    except Exception:
        pass

    return {
        "up_count": up, "down_count": down,
        "north_net_flow": north_net, "emotion": emotion,
        "main_net_yi": main_net_yi, "main_pct": main_pct,
    }

@tool(
    description="获取个股资金流向：主力/大单/中单/小单的净流入额。支持单只（传一个代码）或批量（逗号分隔，最多20只）。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_fund_flow(stock_codes: str = "") -> Dict[str, Any]:
    """获取个股资金流向。支持单只或批量（逗号分隔），单次最多20只。

    Args:
        stock_codes: 股票代码，如 "000001" 或 "000001,600519"
    """
    if not stock_codes or not stock_codes.strip():
        return {"error": "stock_codes 不能为空", "retriable": False}

    codes = [c.strip() for c in stock_codes.split(",") if c.strip()]
    if len(codes) > 20:
        return {"error": f"单次最多20只，当前 {len(codes)} 只", "retriable": False}

    try:
        from app.market_cn.tape import get_fund_flow_realtime
    except ImportError:
        return {"error": "tape 模块不可用", "retriable": False}

    if len(codes) == 1:
        code = codes[0]
        try:
            result = get_fund_flow_realtime(code)
            if "error" in result:
                return {"code": code, "name": "", "net_flow": 0, "main_flow": 0, "retail_flow": 0}
            return {
                "code": code,
                "name": result.get("name", ""),
                "net_flow": result.get("net_flow", 0),
                "main_flow": result.get("main_net", 0),
                "retail_flow": result.get("retail_net", 0),
            }
        except Exception:
            return {"code": code, "name": "", "net_flow": 0, "main_flow": 0, "retail_flow": 0}
    else:
        result = {}
        for code in codes:
            try:
                r = get_fund_flow_realtime(code)
                if "error" in r:
                    result[code] = {"code": code, "name": "", "net_flow": 0, "main_flow": 0, "retail_flow": 0}
                else:
                    result[code] = {
                        "code": code,
                        "name": r.get("name", ""),
                        "net_flow": r.get("net_flow", 0),
                        "main_flow": r.get("main_net", 0),
                        "retail_flow": r.get("retail_net", 0),
                    }
            except Exception:
                result[code] = {"code": code, "name": "", "net_flow": 0, "main_flow": 0, "retail_flow": 0}
        return {"count": len(result), "flows": result, "failed": []}

@tool(
    description="获取行业板块资金流向排名。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_sector_fund_flow(date: str = "") -> Dict[str, Any]:
    """获取行业板块资金流向排名。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}
    if not date:
        date = _today_str()

    from app.market_cn.index import get_sector_fund_flow as _get_sector_flow
    data = _get_sector_flow("今日")
    return {"date": date, "count": len(data), "sectors": data}

@tool(
    description="获取概念板块资金流向排名。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_concept_fund_flow(date: str = "") -> Dict[str, Any]:
    """获取概念板块资金流向排名。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}
    if not date:
        date = _today_str()

    from app.market_cn.index import get_sector_fund_flow as _get_sector_flow
    data = _get_sector_flow("今日")  # 概念板块同源
    return {"date": date, "count": len(data), "concepts": data}


# ══════════════════════════════════════════════════════════════
# 资金流（个股）
# ══════════════════════════════════════════════════════════════

def _normalize_code(code: str) -> str:
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code


@tool(
    description="[中线] 个股资金流120日日级数据。主力/大单/中单/小单净流入。近20日主力累计净流入=资金在建仓，持续净流出=资金在撤退。配合筹码分析。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_fund_flow_120d(stock_code: str) -> Dict[str, Any]:
    """获取个股资金流120日日级数据。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _normalize_code(stock_code)
    try:
        from app.market_cn.tape import get_fund_flow_daily
        result = get_fund_flow_daily(code, 120)
        if "error" in result:
            return {"stock_code": code, "error": result["error"]}
        return {
            "stock_code": code,
            "total_days": result.get("total_days", 0),
            "recent_20d_main_net": result.get("recent_20d_main_net", 0),
            "data": result.get("data", [])[-30:],
        }
    except Exception as e:
        logger.warning("get_fund_flow_120d(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


@tool(
    description="[短线] 个股资金流分钟级实时数据。当日盘中主力/大单/超大单实时净流入。盘中盯资金用：超大单突然大幅流入=可能有消息或主力进场。",
    category="行情数据",
    layer="分析层",
    domain=["finance"],
)
def get_fund_flow_minute(stock_code: str) -> Dict[str, Any]:
    """获取个股资金流向分钟级。

    Args:
        stock_code: 股票代码（如 000858）
    """
    code = _normalize_code(stock_code)
    try:
        from app.market_cn.tape import get_fund_flow_realtime
        result = get_fund_flow_realtime(code)
        if "error" in result:
            return {"stock_code": code, "error": result["error"]}
        return {
            "stock_code": code,
            "points": result.get("points", 0),
            "total_main_net": result.get("total_main_net", 0),
            "data": result.get("data", []),
        }
    except Exception as e:
        logger.warning("get_fund_flow_minute(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}
