# -*- coding: utf-8 -*-
"""market_data_api.py — 行情事件/资金流 **数据层公共 API** (2026-09-28)

定位:
  auto 系统经本模块取 **龙虎榜 / 个股资金流 / 大盘资金流**;
  资金流本体的**唯一出入口是 `fund_flow_api.py`** (2026-09-29 收口:
  实时←realtime_snapshot, 日线←kline_1m, 板块←成分聚合)。本文件对资金流
  只做薄转发, 勿在这里加取数逻辑; 龙虎榜仍直接读 dragon_tiger_store。

  大盘资金流 → fund_flow_api.market_flow_summary / market_fund_flow_*
  个股资金流 → fund_flow_api.stock_fund_flow_realtime / history
  龙虎榜     → dragon_tiger_store.query_dragon_tiger (只读)

Returns 一律 dict/list, 失败返回空并 log, 调用方 fail-open。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── 大盘资金流 (公共接口已在 fund_flow_api) ──

def market_flow(days: int = 5, as_of: Optional[str] = None) -> Dict[str, Any]:
    """大盘资金方向摘要 (fund_flow_api)。"""
    try:
        from app.market_cn.fund_flow_api import market_flow_summary
        return market_flow_summary(days=days, as_of=as_of) or {}
    except Exception as e:
        logger.warning("[market_data] market_flow 失败: %s", e)
        return {"direction": "unknown", "error": str(e)}


# ── 个股资金流 ──

def stock_flow(code: str, date: Optional[str] = None,
               prefer: str = "snapshot") -> Dict[str, Any]:
    """个股当日/指定日资金流 (量价近似或远端)。"""
    try:
        from app.market_cn.fund_flow_api import stock_fund_flow_realtime
        return stock_fund_flow_realtime(code, date=date, prefer=prefer) or {}
    except Exception as e:
        logger.warning("[market_data] stock_flow(%s) 失败: %s", code, e)
        return {"code": code, "error": str(e)}


def stock_flow_history(code: str, days: int = 30) -> List[Dict[str, Any]]:
    """个股历史日资金流 (库 → 1m/远端)。"""
    try:
        from app.market_cn.fund_flow_api import stock_fund_flow_history
        return stock_fund_flow_history(code, days=days) or []
    except Exception as e:
        logger.warning("[market_data] stock_flow_history(%s) 失败: %s", code, e)
        return []


# ── 龙虎榜 (事件表, 只读) ──

def lhb_events(trade_date: str = "", stock_code: str = "",
               days: int = 30) -> List[Dict[str, Any]]:
    """龙虎榜事件列表。

    trade_date: 指定日; 空=最近 days 日
    stock_code: 指定票
    """
    try:
        from app.market_cn.dragon_tiger_store import query_dragon_tiger
        return query_dragon_tiger(trade_date=trade_date, stock_code=stock_code,
                                  days=days) or []
    except Exception as e:
        logger.warning("[market_data] lhb_events 失败: %s", e)
        return []


def lhb_on_date(code: str, trade_date: str) -> List[Dict[str, Any]]:
    """某票某日上榜记录 (空=未上榜)。"""
    return lhb_events(trade_date=trade_date, stock_code=code, days=1)


def lhb_recent_count(code: str, days: int = 20, as_of: Optional[str] = None) -> int:
    """近 N 日上榜次数 (含 as_of 当日; as_of 空=今天)。"""
    rows = lhb_events(stock_code=code, days=max(1, int(days)))
    if not rows:
        return 0
    if as_of:
        d0 = str(as_of)[:10]
        rows = [r for r in rows if str(r.get("trade_date") or "")[:10] <= d0]
    return len(rows)


def lhb_net_amount(code: str, trade_date: str) -> Optional[float]:
    """某票某日龙虎榜净额 (元); 无记录 None。"""
    rows = lhb_on_date(code, trade_date)
    if not rows:
        return None
    try:
        return float(rows[-1].get("net_amount") or 0.0)
    except Exception:
        return None
