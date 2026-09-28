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
def _fund_flow_stock_realtime(codes: str) -> dict:
    """个股实时资金流 + 5日/20日累计（2026-09-28 补全多日汇总）。

    私有函数不注册（_ 前缀），仅供 get_fund_flow(scope="stock") 分发。
    """
    if not codes or not codes.strip():
        return {"error": "codes 不能为空", "retriable": False}

    codes = [c.strip() for c in codes.split(",") if c.strip()][:20]
    from app.market_cn.tape import get_fund_flow_realtime, get_fund_flow_daily

    results = {}
    for code in codes:
        try:
            rt = get_fund_flow_realtime(code)
            # 补全多日累计（daily 有就合并进 realtime 结果）
            if isinstance(rt, dict) and "error" not in rt:
                try:
                    daily = get_fund_flow_daily(code, days=30)
                    if isinstance(daily, dict) and "error" not in daily:
                        rt["recent_5d_main_net"] = daily.get("recent_5d_main_net")
                        rt["recent_20d_main_net"] = daily.get("recent_20d_main_net")
                except Exception as e:
                    logger.debug("_fund_flow_stock_realtime(%s) daily 汇总失败: %s", code, e)
            results[code] = rt
        except Exception as e:
            results[code] = {"error": str(e)}

    return {"count": len(results), "data": results}


def _fund_flow_trend(rows: List[Dict[str, Any]]) -> dict:
    """资金趋势判定（中间结果直出）：从日频 main_net 序列算连续净流入/流出天数+方向。

    口径：取末日符号为方向，向前数连续同号天数；末日为 0 或无数据 → direction=none。
    易错点：空序列/全 0 序列要给 none 而不是 inflow（勿把无数据当净流入）。
    """
    if not rows:
        return {"direction": "none", "streak_days": 0, "as_of": ""}
    as_of = str(rows[-1].get("date", ""))
    signs = [1 if (r.get("main_net") or 0) > 0 else (-1 if (r.get("main_net") or 0) < 0 else 0)
             for r in rows]
    if signs[-1] == 0:
        return {"direction": "none", "streak_days": 0, "as_of": as_of}
    last = signs[-1]
    streak = 0
    for s in reversed(signs):
        if s != last:
            break
        streak += 1
    return {"direction": "inflow" if last > 0 else "outflow",
            "streak_days": streak, "as_of": as_of}


def _fund_flow_boards_view(src: dict) -> dict:
    """sectors/concepts 键统一为 boards（2026-09-27 归组契约）。

    破坏点：旧键不再出现（方案 §3.1 已明写）；只改键名不改行结构。
    """
    if not isinstance(src, dict):
        return src
    out = dict(src)
    for old in ("sectors", "concepts"):
        if old in out:
            out["boards"] = out.pop(old)
    return out


def get_fund_flow(scope: str = "stock", codes: str = "", days: int = 120,
                  indicator: str = "今日") -> dict:
    """资金流向（统一入口，2026-09-27 工具归组化）。

    Args:
        scope: 资金流范围，stock | stock_daily | market | sector | concept
        codes: 股票代码，多股逗号分隔（scope=stock/stock_daily 用），如 "000001,600519"
        days: 历史回溯天数（scope=stock_daily 用），默认120
        indicator: 时间维度（scope=sector/concept 用），"今日"|"5日"|"10日"

    Returns:
        信封格式（scope 决定结构）：
        stock       → {"count": N, "data": {CODE: {主力净流入, 趋势, ...}}}
        stock_daily → {"count": N, "data": {CODE: {data:[{date, main_net, ...}]}}}
        market      → {"source","main_net","in_net","out_net",...}
        sector/concept → {"indicator","count","boards":[{name, main_net, ...}]}
        统一顶层键: "data"/"count"/"error"(失败)。切片迭代先取 result["data"]。
    """
    s = (scope or "stock").strip().lower()
    if s == "stock":
        return _fund_flow_stock_realtime(codes)
    if s == "stock_daily":
        out = get_fund_flow_daily(codes, days)
        data = out.get("data") if isinstance(out, dict) else None
        if isinstance(data, dict):
            for one in data.values():
                if isinstance(one, dict) and "error" not in one:
                    one["资金趋势判定"] = _fund_flow_trend(one.get("data") or [])
        return out
    if s == "market":
        return get_market_fund_flow()
    if s == "sector":
        return _fund_flow_boards_view(get_sector_fund_flow(indicator))
    if s == "concept":
        return _fund_flow_boards_view(get_concept_fund_flow(indicator))
    return {"error": f"scope 无效: {scope!r}，可选 stock|stock_daily|market|sector|concept",
            "retriable": False}
def get_sector_fund_flow(indicator: str = "今日") -> dict:
    """行业资金流向：返回各行业板块主力资金净流入排名。

    Returns:
        dict: {indicator, count, sectors:[{name,change_pct,main_net,...}]}。行业列表在 sf['sectors']（list）；异常含 error。

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

    Returns:
        dict: {indicator, count, concepts:[{name,change_pct,main_net,...}]}。概念列表在 cf['concepts']（list，非 'sectors'）。

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

    Returns:
        统一结构（单/多股一致，2026-09-22 起）：{"count": N, "data": {代码: 单股结果},
        "error": None}；失败 → {"error": "...", "retriable": False}。单股结果字段：{code, total_days, recent_20d_main_net, data:[...]}（日线列表在 data）。

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
    """大盘资金流向：返回全市场主力/散户实时净流入金额。

    Returns:
        dict: {source,timestamp,main_net,main_pct,in_net,out_net,data}。main_net 元；data 明细 list；sectors_count/points 或缺；异常含 error。
    """
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
