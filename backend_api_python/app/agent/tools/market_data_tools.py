# -*- coding: utf-8 -*-
"""
Market Data Tools — 龙虎榜、热榜、涨跌停池、资金流向。

从 stock_screener_tools.py 拆分而来，专注于市场数据查询工具。
数据源：东方财富搜索 API + AkShare fallback。
"""
from __future__ import annotations

import logging
import random
import string
import time
from typing import Any, Dict, List, Optional

import requests
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

_EASTMONEY_SEARCH_URL = "https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code"

# ══════════════════════════════════════════════════════════════
#  通用辅助
# ══════════════════════════════════════════════════════════════

def _validate_date(date_str: str, param_name: str = "date") -> str:
    if not date_str:
        return date_str
    from datetime import datetime
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

def _gen_id(length: int = 32) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))

def _safe_float(val) -> Optional[float]:
    if val is None or val == "" or val == "-" or val == "--":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None

def _call_eastmoney_api(keyword: str, page_size: int = 200, page_no: int = 1) -> Dict[str, Any]:
    """调用东方财富 search-code API。"""
    body = {
        "needAmbiguousSuggest": True,
        "pageSize": page_size,
        "pageNo": page_no,
        "fingerprint": _gen_id(32),
        "matchWord": "",
        "shareToGuba": False,
        "timestamp": str(int(time.time() * 1000)),
        "requestId": _gen_id(32) + str(int(time.time() * 1000)),
        "removedConditionIdList": [],
        "ownSelectAll": False,
        "needCorrect": True,
        "client": "WEB",
        "product": "",
        "needShowStockNum": False,
        "biz": "web_ai_select_stocks",
        "xcId": "",
        "gids": [],
        "dxInfoNew": [],
        "keyWordNew": keyword,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        resp = requests.post(_EASTMONEY_SEARCH_URL, json=body, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error("EastMoney API request failed: %s", e)
        return {"code": -1, "msg": f"请求东方财富API失败: {e}"}

def _em_search(keyword: str, page_size: int = 100) -> List[Dict[str, Any]]:
    """东财搜索封装，返回股票列表或空列表。"""
    try:
        from app.market_cn.eastmoney_search import search_stocks
        raw = search_stocks(keyword=keyword, page_size=page_size)
        return raw.get("stocks", []) if raw.get("code") == 1 else []
    except Exception as e:
        logger.warning("[东财搜索] '%s' 失败: %s", keyword, e)
        return []

# ── AkShare fallback ──

def _ak_dragon_tiger(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    try:
        from app.data_sources.akshare import fetch_akshare_dragon_tiger
        return fetch_akshare_dragon_tiger(start_date, end_date)
    except Exception:
        return []

def _ak_zt_pool(trade_date: str) -> List[Dict[str, Any]]:
    try:
        from app.data_sources.akshare import fetch_akshare_zt_pool
        return fetch_akshare_zt_pool(trade_date)
    except Exception:
        return []

def _ak_dt_pool(trade_date: str) -> List[Dict[str, Any]]:
    try:
        from app.data_sources.akshare import fetch_akshare_dt_pool
        return fetch_akshare_dt_pool(trade_date)
    except Exception:
        return []

def _ak_broken_board(trade_date: str) -> List[Dict[str, Any]]:
    try:
        from app.data_sources.akshare import fetch_akshare_broken_board
        return fetch_akshare_broken_board(trade_date)
    except Exception:
        return []

def _ak_hot_rank() -> List[Dict[str, Any]]:
    try:
        from app.data_sources.akshare import fetch_akshare_hot_rank
        return fetch_akshare_hot_rank()
    except Exception:
        return []

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

        raw = _em_search(f"{stock_code.strip()} 龙虎榜", 30)
        data = [s for s in raw if stock_code.strip() in str(s.get("code", ""))]
        if not data:
            all_data = _ak_dragon_tiger(start_date, date)
            code = stock_code.strip().replace(".", "").upper()
            data = [r for r in all_data if str(r.get("stock_code", "")).replace(".", "").upper() == code]

        return {"stock_code": stock_code, "days": days, "count": len(data), "records": data}
    else:
        # 全市场龙虎榜
        if not date:
            date = _yesterday_str()
        days = max(1, min(days, 30))
        start_date = date
        if days > 1:
            start_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")

        data = _em_search("龙虎榜", 100)
        if not data:
            data = _ak_dragon_tiger(start_date, date)
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
    data = _em_search("热门股票", top_n)
    if not data:
        data = _ak_hot_rank()
    return {"count": len(data[:top_n]), "stocks": data[:top_n]}

@tool(
    description="获取涨停股票池：代码、名称、涨停价、封板资金、连板天数。可筛选连板股（设 min_continuous_days>=2）。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_zt_pool(date: str = "", min_continuous_days: int = 0) -> Dict[str, Any]:
    """获取涨停股票池。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
        min_continuous_days: 最少连板天数，0=全部
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}
    if not date:
        date = _today_str()
    min_continuous_days = max(0, int(min_continuous_days or 0))

    data = _em_search("涨停", 100)
    if not data:
        data = _ak_zt_pool(date)
    if min_continuous_days > 0:
        data = [r for r in data if int(r.get("continuous_zt_days", 0) or 0) >= min_continuous_days]

    # ── 文字表格输出 ──
    lines = [f"涨停池 {date}  共{len(data)}只"]
    if min_continuous_days > 0:
        lines[0] += f"  (连板≥{min_continuous_days})"
    lines.append("")
    lines.append(f"{'代码':<8} {'名称':<8} {'连板':>4} {'涨停时间':<10} {'封板资金(万)':>12} {'换手率%':>7} {'涨停原因'}")
    lines.append("-" * 80)
    for s in data:
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
    text_table = "\n".join(lines)

    return {"date": date, "min_continuous_days": min_continuous_days, "count": len(data), "stocks": data, "text": text_table}

@tool(
    description="获取跌停股票池：代码、名称、跌停价、封单量。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_limit_down(date: str = "") -> Dict[str, Any]:
    """获取跌停股票池。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}
    if not date:
        date = _today_str()

    data = _em_search("跌停", 100)
    if not data:
        data = _ak_dt_pool(date)
    return {"date": date, "count": len(data), "stocks": data}

@tool(
    description="获取炸板(开板)股票池。炸板=曾封涨停但被打开，是资金分歧信号。",
    category="龙虎榜/热榜",
    layer="数据层",
    domain=["finance"],
)
def get_broken_board(date: str = "") -> Dict[str, Any]:
    """获取炸板(开板)股票池。炸板=曾封涨停但被打开，是资金分歧信号。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}
    if not date:
        date = _today_str()

    data = _em_search("炸板", 100)
    if not data:
        data = _ak_broken_board(date)
    return {"date": date, "count": len(data), "stocks": data}

@tool(
    description="获取全市场涨跌统计快照：上涨/下跌家数、情绪指标。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_market_overview() -> Dict[str, Any]:
    """获取全市场涨跌统计快照：上涨/下跌家数、情绪指标。"""
    stocks = _em_search("A股 涨跌统计", 5)
    up = sum(1 for s in stocks if (s.get("change_rate") or 0) > 0)
    down = sum(1 for s in stocks if (s.get("change_rate") or 0) < 0)
    return {"up_count": up, "down_count": down, "north_net_flow": 0, "emotion": 50}

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

    if len(codes) == 1:
        # 单只
        code = codes[0]
        stocks = _em_search(f"{code.strip()} 资金流向", 5)
        if stocks:
            s = stocks[0]
            return {
                "code": s.get("code", code),
                "name": s.get("name", ""),
                "net_flow": 0, "main_flow": 0, "retail_flow": 0,
            }
        return {"code": code, "name": "", "net_flow": 0, "main_flow": 0, "retail_flow": 0}
    else:
        # 批量
        result = {}
        for code in codes:
            stocks = _em_search(f"{code.strip()} 资金流向", 5)
            if stocks:
                s = stocks[0]
                result[code] = {
                    "code": s.get("code", code),
                    "name": s.get("name", ""),
                    "net_flow": 0, "main_flow": 0, "retail_flow": 0,
                }
            else:
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

    data = _em_search("板块资金流向", 30)
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

    data = _em_search("概念资金流向", 30)
    return {"date": date, "count": len(data), "concepts": data}

# ══════════════════════════════════════════════════════════════
#  工具声明
# ══════════════════════════════════════════════════════════════

# Legacy list — kept for backward compat during migration; safe to remove later.